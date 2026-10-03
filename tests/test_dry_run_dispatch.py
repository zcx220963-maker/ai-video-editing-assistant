# -*- coding: utf-8 -*-
"""dry-run 走不走「提交 + 轮询」通道——这条路由分错，账就永远拿不到（真机踩过）。

``render_video`` 是唯一的长耗时节点，MCP 侧把它整体交给渲染分发器后台跑，回一张进度
视图让调用方轮询 ``render_status``。这套语义对真渲染是对的（一部片子几十秒到几分钟），
对 dry-run 是错的：

* 分发器一进门就 ``render_jobs.enqueue`` 落一条 queued 行；
* 而 dry-run 的节点体**刻意不开任务行**（它不是一次渲染，留下 running/queued 会被界面
  当成「正在出片」、被看门狗按停滞判死），于是也不推进度、不写终态；
* 结果那一行永远停在 queued，而 dry-run 真正该回的出片计划账被进度视图盖掉——
  调用方按提示轮询到天荒地老也拿不到账。

真机验证时正是这个形状：连续 20 次 ``render_status`` 全回 ``status=queued percent=0``。
同名的离线用例全绿着放过了它，因为它们测的是纯函数 ``render_plan``/``evidence_ledger``，
没有穿过 MCP 的路由层——这份文件补的就是那一层。

全程内存替身存储：不连 PG、不碰 MinIO、不烧 ffmpeg。

运行：  python tests/test_dry_run_dispatch.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import build_storage  # noqa: E402
from storyline_server.render_jobs import RenderDispatcher  # noqa: E402
from storyline_server.server import StorylineServer  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def timeline() -> dict:
    """一份过得去 dry-run 全部校验的时间线。对象键是假的没关系：dry-run 不取字节。"""
    return {
        "width": 640, "height": 360, "fps": 25.0, "duration": 10.0,
        "mode": "original_audio",
        "events": [{"path": "obj:users/u/convs/c/main.mp4", "start": 0.0, "end": 10.0}],
        "audio_events": [{"path": "obj:users/u/convs/c/main.mp4", "start": 0.0, "end": 10.0,
                          "src_start": 0.0, "src_end": 10.0}],
        "subtitles": [{"text": "第一句", "start": 0.0, "end": 4.0},
                      {"text": "第二句", "start": 4.0, "end": 8.0}],
        "overlay_events": [{"path": "obj:users/u/convs/c/broll.mp4",
                            "start": 4.0, "end": 8.0, "src_start": 0.0, "src_end": 4.0,
                            "fit": "cover"}],
        "bgm": None,
    }


async def seed(server: StorylineServer, sess: str, tl: dict) -> None:
    """把 render_video 的唯一前置（plan_timeline）预先摆进产物库。

    不摆不行：请求拦截器发现缺前置会**递归执行上游**（search_media → load_media…），
    而这条用例的存储是空的，于是一路报到「load_media 没有 material_ids」——那烧的是
    另一层的报错，跟这里要钉的路由分岔一点关系都没有。摆上之后拦截器认「前置已就绪」，
    调用就真的落到 MCP 路由层。
    """
    await server.storage.artifacts(sess, "_default").put("plan_timeline", {"timeline": tl})


async def settle(server: StorylineServer) -> None:
    """把后台排出去的活收干净：内存替身里真渲染必然快速失败，别留悬挂任务。"""
    for t in list(server.renders._tasks.values()):
        try:
            await asyncio.wait_for(t, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            t.cancel()
    server.renders._tasks.clear()


async def main() -> int:
    storage = build_storage("memory")
    server = StorylineServer(Settings(), storage=storage)
    handler = server.handlers["render_video"]
    sess = "sess-dryrun-route"
    await seed(server, sess, timeline())

    print("\n=== ① dry_run 必须内联回账，而不是回一张永远不动的进度视图 ===")
    j = json.loads(await handler(None, session_id=sess, dry_run=True, timeline=timeline()))
    out = j.get("output") or {}
    check(out.get("dry_run") is True, f"回的是出片计划的账（顶层键 {sorted(j)}）")
    check((j.get("render") or {}).get("status") is None,
          "没有把这张账包进进度视图里——真机踩的坑就是只剩 status=queued 可看")
    plan = out.get("plan") or {}
    check(plan.get("duration") == 10.0 and len(plan.get("overlays") or []) == 1,
          f"账里有总长与覆盖层落点（{plan.get('duration')}s / "
          f"{(plan.get('overlays') or [{}])[0].get('at')}）")
    check(bool(out.get("evidence")) and bool(out.get("not_checked")),
          "证据分级与「这次没检查什么」随账回来（缺一条就是空头承诺）")
    check(not any(k.startswith(sess + "|") for k in server.renders._tasks),
          f"分发器里没有这次 dry_run 的活（现有 {list(server.renders._tasks)}）")
    check(await storage.render_view(sess, "_default") is None,
          "dry-run 不落渲染任务行：留一行 queued 会被界面当成「正在出片」")
    check(not await storage.artifacts(sess, "_default").has("render_video"),
          "dry-run 也不落产物行：那是「将要渲成什么样」的答复，落成产出等于骗下游")

    print("\n=== ② 开关的其它写法同样走内联（模型爱写字符串） ===")
    for spelling in ("true", "1", "on"):
        s = f"sess-str-{spelling}"
        await seed(server, s, timeline())
        j2 = json.loads(await handler(None, session_id=s,
                                      dry_run=spelling, timeline=timeline()))
        check((j2.get("output") or {}).get("dry_run") is True,
              f"dry_run={spelling!r} 也内联回账")
        check(await storage.render_view(s, "_default") is None,
              f"dry_run={spelling!r} 同样不留任务行")
    # 反向：开关写成 "false" 不许被读成 dry（那等于偷偷绕过真渲染的整条链路）
    s = "sess-str-false"
    await seed(server, s, timeline())
    j3 = json.loads(await handler(None, session_id=s, wait_sec=0,
                                  dry_run="false", timeline=timeline()))
    check((j3.get("output") or {}).get("dry_run") is not True,
          'dry_run="false" 读成假：仍走真渲染那条路')
    check(await storage.render_view(s, "_default") is not None,
          'dry_run="false" 真落了任务行——它没被当成 dry 悄悄旁路掉')

    print("\n=== ③ 真渲染照旧走「提交 + 轮询」：旁路不能把它一起绕掉 ===")
    sess2 = "sess-real-route"
    await seed(server, sess2, timeline())
    j4 = json.loads(await handler(None, session_id=sess2, wait_sec=0, timeline=timeline()))
    rd = j4.get("render") or {}
    check(rd.get("status") in ("queued", "running", "done", "failed"),
          f"真渲染回的仍是一张进度视图（{rd.get('status')}）")
    check(j4.get("output") is None,
          "真渲染不把结果塞进这一次往返（分钟级活儿，靠 render_status 续问）")
    await asyncio.sleep(0.2)
    row = await storage.render_view(sess2, "_default")
    check(row is not None,
          "真渲染留下一条任务行——界面与看门狗都靠它，不能一并绕掉")
    check(j4.get("artifact_id") == (row or {}).get("artifact_id"),
          f"视图与行的作用域对得上（{(row or {}).get('artifact_id')}）")
    await settle(server)

    print("\n=== ④ 分发器落行这件事本身：判据得由路由层守，别指望节点体 ===")
    dsp = RenderDispatcher(storage)
    await dsp.submit("sess-hypothetical", "_default", lambda: asyncio.sleep(0))
    row = await storage.render_view("sess-hypothetical", "_default")
    check(row is not None and row.get("status") in ("queued", "running", "done", "failed"),
          f"submit 一定先落一条行（{row and row.get('status')}）——"
          "所以 dry-run 只要进了这条路，账就被进度视图盖掉了")

    print(f"\n{'ALL PASSED' if _fails == 0 else f'{_fails} FAILED'} ({_checks} checks)")
    await storage.close()
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
