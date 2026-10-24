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


def _solid(png: Path, w: int, h: int, color: str) -> None:
    """出一张纯色帧。用足整幅画布：生产里 chrome 截图就是 --window-size 的精确尺寸，
    probe 回读的宽高因此应与入参一致（这条对齐正是本测试要钉的）。"""
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}",
          "-frames:v", "1", str(png)], timeout=60)


def fake_capture(target: str, t_ms: int, png: Path, w: int, h: int) -> None:
    """替身采集器：**每一帧都不一样**、且都远离纯黑。

    颜色直接由毫秒数编码，所以三个取样点（15%/50%/85%）拿到的必然互不相同——
    这样「画面在动」这条读数才是真的被验到，而不是碰巧撞上同一个循环相位。
    亮度也避开了那个 6.0 的黑屏闸：整片黑是另一条用例专门构造的。
    """
    r = 40 + int(t_ms) % 200
    g = 40 + (int(t_ms) // 7) % 200
    _solid(png, w, h, f"0x{r:02x}{g:02x}80")


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

    # 10 帧而不是 3 帧：三个取样点（15%/50%/85%）才会落在**三张不同的帧**上。
    # ffmpeg 的输入定位给的是"第一个 PTS ≥ 该时刻"的帧，3 帧的片上后两个点会撞到同一张。
    packed = await node(state, html=html, duration_sec=1.0, fps=10,
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
    meta0 = out["web_render"]
    check(meta0["frames"] == 10 and meta0["fps"] == 10,
          f"帧数/帧率进 meta：{ {k: meta0[k] for k in ('frames', 'fps')} }")
    check(meta0["planned_sec"] == 1.0 and meta0["requested_sec"] == 1.0,
          f"planned_sec 报的是拍板那个数：{meta0['planned_sec']}")
    check(meta0["capped"] == [], f"没夹取就不吭声：{meta0['capped']}")

    # 像素级读数：成片**真被量过**，且这一次是干净的（既不黑、也不是定格）。
    pixel = meta0.get("pixel")
    check(isinstance(pixel, dict) and pixel.get("luma"),
          f"成片真被抽帧量过亮度：{pixel}")
    if isinstance(pixel, dict) and pixel.get("luma"):
        check(pixel["black_indices"] == [], f"取样点都不黑：{pixel['luma']}")
        check(pixel["distinct"] is True and len(pixel["luma"]) >= 2,
              f"几帧互不相同（画面在动）：{pixel['luma']}")
        check(meta0.get("pixel_note") == "",
              f"一切正常时不唠叨：{meta0.get('pixel_note')!r}")

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
    check(any("fps" in c for c in meta["capped"]),
          f"夹了 fps 就写进回执：{meta['capped']}")

    # ★ 真机事故那一笔（2026-10-16，run b7df827ebed4）：模型要 152 秒，
    #   这里先夹到 60、再被 600 帧夹成 20 秒，而回执写着 planned_sec=60.0 配 frames=600
    #   ——**两个数自相矛盾**，且只字不提「你要的 152 被砍了」。模型只能从成片时长反推。
    #
    #   600 帧真跑一遍要一分钟，所以这里把天花板**临时压低**到 12 帧：要验的是
    #   「撞上限时怎么报」，不是那个数字本身（`frames == WEB_MAX_FRAMES` 同形）。
    ceiling, web_nodes.WEB_MAX_FRAMES = web_nodes.WEB_MAX_FRAMES, 12
    try:
        packed2 = await node(state, html="<html><body>x</body></html>",
                             duration_sec=152, fps=30)
    finally:
        web_nodes.WEB_MAX_FRAMES = ceiling
    meta2 = packed2["output"]["web_render"]
    check(meta2["frames"] == 12, f"帧数封顶：{meta2['frames']}")
    check(meta2["planned_sec"] == round(12 / 30, 3)
          and abs(float(packed2["output"]["duration"]) - 0.4) < 0.2,
          f"★ planned_sec 说的是**成片真实时长**（{meta2['planned_sec']}s），"
          f"不是夹取前的 dur——修复前这里报 60.0，跟 20 秒的片子打架")
    check(meta2["requested_sec"] == 152.0 and meta2["requested_fps"] == 30,
          f"原样回报要的 152 秒 / 30fps（实际 {meta2['requested_sec']} / "
          f"{meta2['requested_fps']}）")
    check(any("152" in c for c in meta2["capped"]),
          f"★ 夹取必须说出来：{meta2['capped']}")
    check(any("60" in c for c in meta2["capped"]),
          "也说清先被 0.5~60 秒这一档夹过（不是一声不吭就按 60 算）")
    check(any("12" in c for c in meta2["capped"]),
          "也点明是撞到了帧数上限，不是「就只有 60 秒」")
    check(meta2["pixel"] is not None and meta2["pixel"].get("luma"),
          f"长片的读数同样拿到了亮度：{meta2['pixel']}")


def _black_capture(target: str, t_ms: int, png: Path, w: int, h: int) -> None:
    """整片全黑——真机那条 20 秒黑片的像素形态。"""
    _solid(png, w, h, "black")


def _frozen_capture(target: str, t_ms: int, png: Path, w: int, h: int) -> None:
    """每一帧一模一样（亮度正常）——rAF 饿死、整片停在第一帧的形态。"""
    _solid(png, w, h, "0x4080c0")


async def part5_wrong_html(tmp: Path) -> None:
    """★ 真机事故的入口：html 填成**文件路径**，这里必须当场拒绝。

    2026-10-16 run b7df827ebed4：模型把 `thin-muscle/index.html`（自己刚 write_file
    落盘的那个文件**路径**）填进了 html。节点一字不检地 write_text，22 个字符成了整个
    "网页" → 空白页 → 透明 PNG → format=yuv420p 把透明转成黑 → **600 帧全黑**。
    全程零报错，回执还写着 source=html。
    """
    print("\n[5] html 要的是文档本身：填文件路径当场拒绝（真机黑片那次的入口）")
    storage = build_storage("memory", workspace_root=tmp / "ws5",
                            cache_root=tmp / "cache5")
    reg = NodeRegistry()
    RenderWebNode(None, None, storage, registry=reg)
    node = reg.get("render_web")
    sid, aid = "t:web", "aw5"
    store = await ArtifactStore.open(storage.artifacts(sid, aid), sid, aid)
    state = NodeState(session_id=sid, artifact_id=aid, user_request="", store=store)

    # 先钉住：schema 自己就得说清它要什么（模型是照 schema 填的）
    desc = node.input_schema["properties"]["html"]["description"]
    check("HTML 文档本身" in desc and "不是文件路径" in desc,
          f"入参说明写明「文档本身、不是路径」：{desc[:40]}…")
    check("600" in node.input_schema["properties"]["duration_sec"]["description"]
          and "600" in node.input_schema["properties"]["fps"]["description"],
          "★ 两条时长/帧率说明都点明了 600 帧这条**乘起来**的约束"
          "（修复前各写各的区间，schema 承诺了它给不出的组合）")

    try:
        await node(state, html="thin-muscle/index.html")
        check(False, "填文件路径竟被放行（这正是黑片那次）")
    except ValueError as e:
        msg = str(e)
        check("HTML 文档本身" in msg, f"当场拒绝并说清要的是什么：{msg[:60]}…")
        check("thin-muscle/index.html" in msg, "把收到的内容原样报回去（模型一眼认出）")
        check("read_file" in msg and "url" in msg, "给了两条出路：读出来再传 / 用 url")

    # 反向：真文档（以 < 开头）不该被这条误伤
    ok = await node(state, html="<html><body>x</body></html>",
                    duration_sec=0.5, fps=2)
    check(ok["output"]["web_render"]["source"] == "html", "真文档照旧放行")


async def part6_black_and_frozen(tmp: Path) -> None:
    """★ 像素级读数：全黑 / 定格 必须**报出来**，不能当"完成"静默出片。"""
    print("\n[6] 成片读数的三档：正常不唠叨、全黑点名、定格标未判")
    storage = build_storage("memory", workspace_root=tmp / "ws6",
                            cache_root=tmp / "cache6")
    reg = NodeRegistry()
    RenderWebNode(None, None, storage, registry=reg)
    node = reg.get("render_web")
    sid, aid = "t:web", "aw6"
    store = await ArtifactStore.open(storage.artifacts(sid, aid), sid, aid)
    state = NodeState(session_id=sid, artifact_id=aid, user_request="", store=store)
    html = "<html><body>x</body></html>"

    real = RenderWebNode.capture_backend
    try:
        RenderWebNode.capture_backend = _black_capture
        out = (await node(state, html=html, duration_sec=1.0, fps=3))["output"]
        meta = out["web_render"]
        check(meta["pixel"] is not None and meta["pixel"]["black_indices"] == [0, 1, 2],
              f"全黑的片子取样点全被判黑：{meta.get('pixel')}")
        check(float(meta["pixel"]["min_luma"]) < 6.0,
              f"最低亮度确实在闸下：{meta['pixel']['min_luma']}")
        note = meta["pixel_note"]
        check("全黑" in note, f"★ 回执点名全黑：{note[:50]}…")
        check("别把它当成做完了" in note,
              "明确不许拿它交差——这正是那次一声不吭出片的反面")

        RenderWebNode.capture_backend = _frozen_capture
        out2 = (await node(state, html=html, duration_sec=1.0, fps=3))["output"]
        meta2 = out2["web_render"]
        check(meta2["pixel"]["black_indices"] == [],
              f"定格的片子亮度正常（不走黑屏那条）：{meta2['pixel']['luma']}")
        check(meta2["pixel"]["distinct"] is False,
              "几帧完全相同 → distinct=False")
        note2 = meta2["pixel_note"]
        check("完全相同" in note2 and "未判" in note2,
              f"★ 定格标成**未判**（读数，不是断言）：{note2[:50]}…")
        check("纯函数" in note2 and "rAF" in note2,
              "并指出最可能的成因（rAF 在无头虚拟时钟下饿死）")
    finally:
        RenderWebNode.capture_backend = real


def part4_container_flags() -> None:
    """容器里 headless 的两条豁免参数：丢了就在 root 下起不来、或 /dev/shm 撑爆。"""
    print("\n[4] 容器内截图参数：root 才放宽沙箱，/dev/shm 一律不赌 64MB")
    import os as _os
    flags = web_nodes.chrome_container_flags()
    check("--disable-dev-shm-usage" in flags, "恒定带 --disable-dev-shm-usage")
    root = hasattr(_os, "geteuid") and _os.geteuid() == 0
    check(("--no-sandbox" in flags) is root,
          f"--no-sandbox 只跟着 root 走（当前 root={root}）")

    seen: list[list[str]] = []

    def spy(cmd: list[str], timeout: float = 0.0) -> None:
        seen.append(cmd)
        for arg in cmd:
            if arg.startswith("--screenshot="):
                Path(arg.split("=", 1)[1]).write_bytes(b"\x89PNG fake")

    real = web_nodes._run
    web_nodes._run = spy
    try:
        with tempfile.TemporaryDirectory() as td:
            web_nodes._chrome_frame("chromium", "file:///x.html", 500,
                                    Path(td) / "f%04d.png", 1920, 1080)
    finally:
        web_nodes._run = real
    check(bool(seen), "截图命令确实发出去了")
    if seen:
        missing = [f for f in flags if f not in seen[0]]
        check(not missing, f"截图命令带上容器豁免参数（缺：{missing or '无'}）")
        check("--headless=new" in seen[0], "新 headless 优先")

    # 退出码 0 但没落盘：不能当成功返回，否则几分钟后 ffmpeg 才炸，且报不出时刻
    landed: list[list[str]] = []

    def silent(cmd: list[str], timeout: float = 0.0) -> None:
        landed.append(cmd)

    web_nodes._run = silent
    try:
        with tempfile.TemporaryDirectory() as td:
            try:
                web_nodes._chrome_frame("chromium", "file:///x.html", 500,
                                        Path(td) / "f%04d.png", 1920, 1080)
                ok, err = False, "没抛错，静默当截到了"
            except web_nodes.MediaError as e:
                ok, err = True, str(e)
    finally:
        web_nodes._run = real
    check(ok, f"PNG 没落盘时按失败收口（{err}）")
    check(len(landed) == 2, f"两种 headless 都试过才认输（实发 {len(landed)} 次）")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="web_render_test_") as td:
        tmp = Path(td)
        web_nodes.RenderWebNode.capture_backend = fake_capture
        try:
            await part1_contract(build_storage("memory"))
            await part2_pipeline(tmp)
            await part3_clamps_and_errors(tmp)
            part4_container_flags()
            await part5_wrong_html(tmp)
            await part6_black_and_frozen(tmp)
        finally:
            web_nodes.RenderWebNode.capture_backend = None   # 还原类属性,不污染其他用例
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
