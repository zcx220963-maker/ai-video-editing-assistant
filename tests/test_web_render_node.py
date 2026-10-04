# -*- coding: utf-8 -*-
"""网页渲染出片节点 render_web（Hyperframes 通道）的离线验证。

运行：  python tests/test_web_render_node.py   # 全离线：内存替身 + 可注入采集器 + 真 ffmpeg

钉住四件事：
① 契约：独立终点节点（required_nodes 为空，起点即终点）、入参 schema 必填 html/url；
② 管线：注入的假采集器 → ffmpeg 帧序列合成 → probe → workspace.publish 进对象存储，
   输出形状与 render_video 同一条契约（video 对象键 / duration / width / height / title）；
③ ephemeral：media_url 是 presigned 直链，**只回给调用方**——入库的那份没有它；
④ 钳制与报错：fps/时长/画布超界被夹回、html 与 url 缺一不可。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, NodeRegistry, NodeState
from agent_framework.storage import build_storage
from storyline_server.nodes import web_nodes
from storyline_server.nodes.core_nodes import build_real_registry
from storyline_server.nodes.web_nodes import RenderWebNode, _run

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def fake_capture(target: str, t_ms: int, png: Path, w: int, h: int) -> None:
    """替身采集器：按虚拟时钟落到不同纯色帧（红→绿→蓝循环），验证帧序进片。"""
    color = ("red", "green", "blue")[int(t_ms) // 400 % 3]
    # 用足整幅画布：生产里 chrome 截图就是 --window-size 的精确尺寸，
    # probe 回读的宽高因此应与入参一致（这条对齐正是本测试要钉的）。
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}",
          "-frames:v", "1", str(png)], timeout=60)


async def part1_contract(storage) -> None:
    print("\n[1] 契约：独立终点节点，入参与产物键同 render_video 一族")
    reg = NodeRegistry()
    RenderWebNode(None, None, storage, registry=reg)
    node = reg.get("render_web")
    check(node is not None, "build_real_registry 白名单能取到 render_web")
    check(node.required_nodes == [], "required_nodes 为空（起点即终点，可与剪辑链并行）")
    check(node.ephemeral == ("media_url",), "media_url 标 ephemeral（不落共享库）")
    check("html" in node.input_schema["properties"]
          and "url" in node.input_schema["properties"], "入参 schema 含 html/url")


async def part2_pipeline(tmp: Path) -> None:
    print("\n[2] 管线：假采集器 → ffmpeg 合成 → probe → publish")
    storage = build_storage("memory", workspace_root=tmp / "ws",
                            cache_root=tmp / "cache")
    reg = NodeRegistry()
    RenderWebNode(None, None, storage, registry=reg)
    node = reg.get("render_web")

    sid, aid = "t:web", "aw1"
    store = await ArtifactStore.open(storage.artifacts(sid, aid), sid, aid)
    state = NodeState(session_id=sid, artifact_id=aid,
                      user_request="把这段大屏动画出成片", store=store)
    html = ("<html><style>@keyframes c{0%{background:red}50%{background:green}"
            "100%{background:blue}}body{animation:c 1s linear infinite;"
            "margin:0;height:100vh}</style><body></body></html>")

    packed = await node(state, html=html, duration_sec=1.0, fps=3,
                        width=256, height=144, title="大屏动画")
    await store.persist("render_web")   # 生产里由执行路径持久化;测试显式落一次
    out = packed["output"]
    check(out["video"].startswith("renders/"), f"video 是对象键：{out['video']}")
    check(abs(float(out["duration"]) - 1.0) < 0.3,
          f"probe 量出的时长接近设定值：{out['duration']}")
    check(out["width"] == 256 and out["height"] == 144, "画布尺寸来自入参")
    check(out["title"] == "大屏动画", "标题进产物")
    check(str(out["media_url"]).startswith("memory://"),
          f"media_url 是现签直链：{str(out['media_url'])[:30]}")
    check(out["web_render"]["frames"] == 3 and out["web_render"]["fps"] == 3,
          f"帧数/帧率进 meta：{out['web_render']}")

    # 入库的那份（ephemeral 剔除）与回传的那份不同形
    row = await storage.artifacts(sid, aid).get("render_web")
    payload = (row.get("payload") if isinstance(row, dict) and "payload" in row
               else row) or {}
    check(bool(payload) and "media_url" not in payload,
          "落库 payload 不含会过期的 media_url")
    check(payload.get("video") == out["video"],
          "落库 payload 的 video 对象键与回传一致")


async def part3_clamps_and_errors(tmp: Path) -> None:
    print("\n[3] 钳制与报错：超界参数被夹回、缺参当场 ValueError")
    storage = build_storage("memory", workspace_root=tmp / "ws3",
                            cache_root=tmp / "cache3")
    reg = NodeRegistry()
    RenderWebNode(None, None, storage, registry=reg)
    node = reg.get("render_web")
    sid, aid = "t:web", "aw3"
    store = await ArtifactStore.open(storage.artifacts(sid, aid), sid, aid)
    state = NodeState(session_id=sid, artifact_id=aid, user_request="", store=store)

    try:
        await node(state)
        check(False, "html/url 缺一不可（竟未报错）")
    except ValueError as e:
        check("至少给一个" in str(e), f"缺参当场报错：{e}")

    packed = await node(state, html="<html><body>x</body></html>",
                        duration_sec=0.5, fps=99, width=999999, height=1)
    meta = packed["output"]["web_render"]
    check(meta["fps"] == 30, f"fps 超界夹回 30：{meta['fps']}")
    check(packed["output"]["width"] == 3840 and packed["output"]["height"] == 64,
          f"画布夹回上限/下限：{packed['output']['width']}x{packed['output']['height']}")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="web_render_test_") as td:
        tmp = Path(td)
        web_nodes.RenderWebNode.capture_backend = fake_capture
        try:
            await part1_contract(build_storage("memory"))
            await part2_pipeline(tmp)
            await part3_clamps_and_errors(tmp)
        finally:
            web_nodes.RenderWebNode.capture_backend = None   # 还原类属性,不污染其他用例
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
