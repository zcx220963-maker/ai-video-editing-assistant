# -*- coding: utf-8 -*-
"""「局部改」的渲染任务行开在**哪一版**上——路由层这一钉，真机踩过一次假的失败。

局部改的产物一定落在一个新作用域（分叉）：沿用 base 的号等于把旧版覆盖掉，
「改坏了播回旧版」那条退路就没了。可是任务行的键不是在分叉时定的，而是在
**提交那一刻**定的——``RenderDispatcher.submit`` 拿 ``state.artifact_id`` 去 ``enqueue``。
模型走 MCP 时通常不传 ``artifact_id``，那个键于是是 ``_default``；而 base 常常就是
``_default``（会话当前那一版）。两边一错开，代价是三笔（真机现场：会话
``c_t6_1791680442``）：

* ``rj-8a5ffc03``（``_default``）被 ``enqueue`` 从 done 复位成 queued、
  ``video_object_key`` 抹成 NULL——旧版的出片账从此没了：下一次局部改借不到旧字节
  （整片重烧），旧版卡片的「已出片」也变成没出过；
* ``rj-a6112711``（``p1ab8d565``）才是那次局部改正经跑到 done 的那一行；
* ``_default`` 那一行则永远停在 queued，600 秒后被看门狗按停滞判死
  （``error=「渲染停滞超过 600 秒无进度，已按失败收口」``）。调用方按返回视图里的
  作用域轮询，等到的就是那条死行：**一次成功的局部改在界面上报 failed**
  ——硬保证「渲染未达终态不许收尾」被这条错位直接撞开。

前端那条路早就自己做对了：``agent_framework/server.py`` 的 ``/render`` 在提交前就分配好
产物号再交给工具，所以浏览器夹具一直是绿的；漏的是 MCP 入口。

这份用例钉的是**路由层**（键归谁），不重跑渲染管线（分叉与字节那一半在
``test_patch_video_render.py`` / 代码链那套 patch 用例里）：
① 两条链的 patch 节点都声明「本节点会分叉作用域」，新号由**单源**函数出产；
② MCP 入口在提交前把号预发出去：没传 / 传 ``_default`` / 传的就是 base 号，三种都要换；
   调用方显式指定新版号时不许改；
③ 执行位开的行、节点体写终态的行、返回视图回的作用域——三个键同一个，
   且 base 那一版的出片账一个字节都不动；
④ 修复前的形状在执行位层面复现一次，钉住病因（两行分开时的三个症状）。

全程内存替身存储 + 替身节点体：不连 PG、不碰 MinIO、不烧 ffmpeg。

运行：  python tests/test_patch_scope_dispatch.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import NodeState  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from storyline_server.nodes.core_nodes import (  # noqa: E402
    PatchVideoNode, new_patch_scope_id)
from storyline_server.nodes.motion_code_nodes import (  # noqa: E402
    PatchCodeMotionVideoNode)
from storyline_server.nodes.motion_nodes import MotionPatchBase  # noqa: E402
from storyline_server.render_jobs import TERMINAL  # noqa: E402
from storyline_server.server import StorylineServer  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

_checks = 0
_fails = 0
SCOPE_RE = re.compile(r"^p[0-9a-f]{8}$")


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class ForkingBody:
    """``Interceptor`` 的替身：只做真节点体与「任务行」有关的那两件事。

    为什么可以替：这份用例要钉的是**键归谁**——入口开行用的键、体里分叉产生的键、
    终态写入的键、返回视图回显的键，这四者必须是同一个。渲染本身（量表、重烧几窗）
    在 ``test_patch_video_render.py`` 那一套真字节用例里，这里重跑只会把分钟级活儿
    搬进离线套件。

    分叉那一条规则与真 ``_fork`` 一致（空 / ``_default`` / 等于 base 才另起一号），
    而「这个规则只有一份出处」由 ①③ 两段字节级钉住：真 ``_fork`` 里除了
    ``new_patch_scope_id()`` 不许有别的产号的人。
    """

    def __init__(self, storage) -> None:
        self.ws = storage
        self.calls: list[dict] = []

    async def invoke(self, name: str, state: NodeState, **args) -> dict:
        base = str(args.get("base_artifact_id") or "")
        if state.artifact_id in ("", "_default") or state.artifact_id == base:
            state.artifact_id = new_patch_scope_id()
        art = state.artifact_id
        key = f"renders/{state.session_id}/{art}.mp4"
        # 真节点体的收口（core_nodes.PatchVideoNode.process 末尾那句 succeed）：
        # 渲染前按**自己这一版**的键 open 一行、终态也写在同一个键上，不写回 base。
        await self.ws.render_jobs.open(state.session_id, art)
        await self.ws.render_jobs.succeed(state.session_id, art, key, 9.0,
                                          result={"artifact_id": art, "video": key},
                                          attempt=None)
        self.calls.append({"node": name, "artifact_id": art,
                           "session_id": state.session_id})
        return {"output": {"artifact_id": art, "video": key, "duration": 9.0},
                "artifact_id": art}


def view_artifact(j: dict) -> str:
    return (j.get("render") or {}).get("artifact_id") or j.get("artifact_id") or ""


async def settle(server: StorylineServer) -> None:
    """等执行位把刚排出去的活儿收干净。

    ``wait_sec=0`` 的调用只把渲染交给后台任务就返回视图（这正是「提交 + 轮询」的语义），
    所以断言之前必须等那一个任务落地，否则读到的全是「还没开始跑」的中间态。
    """
    for t in list(server.renders._tasks.values()):
        try:
            await asyncio.wait_for(t, timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            t.cancel()
    server.renders._tasks.clear()


async def main() -> int:
    storage = build_storage("memory")
    server = StorylineServer(Settings(), storage=storage)
    server.interceptor = ForkingBody(storage)
    body: ForkingBody = server.interceptor
    sess = "u:tester:c:scope-route"
    old = "a1b2c3d4"

    async def seed_version(art: str) -> str:
        """摆一版「已经出过片」的账：done + 成片对象键（局部改唯一不能动的东西）。"""
        key = f"renders/{sess}/{art}.mp4"
        await storage.render_jobs.enqueue(sess, art)
        await storage.render_jobs.succeed(sess, art, key, 12.0,
                                          result={"artifact_id": art, "video": key},
                                          attempt=None)
        return key

    # 会话当前那一版就在 ``_default`` 上：模型走 MCP 调 render_video 时通常不传
    # artifact_id，那一行于是落在 _default——真机现场（c_t6_1791680442）正是这个形状。
    cur_key = await seed_version("_default")
    old_key = await seed_version(old)

    print("\n=== ① 声明与单源：谁分叉谁就得报出来，号只能有一个出产的人 ===")
    for cls in (PatchVideoNode, MotionPatchBase, PatchCodeMotionVideoNode):
        check(getattr(cls, "forks_artifact_scope", False) is True,
              f"{cls.__name__} 声明 forks_artifact_scope（MCP 入口据此在提交前预发新号）")
    for name in ("patch_video", "patch_motion_code"):
        node = server.registry.get(name)
        check(node is not None and getattr(node, "forks_artifact_scope", False) is True,
              f"注册表里 {name} 那一份实例也带着分叉声明（不是只在类上写着）")
    ids = {new_patch_scope_id() for _ in range(200)}
    check(len(ids) == 200 and all(SCOPE_RE.match(s) for s in ids),
          f"新号形状稳定且不重复（样例 {sorted(ids)[:2]}）")
    for cls in (PatchVideoNode, MotionPatchBase):
        src = inspect.getsource(cls._fork)
        check("new_patch_scope_id()" in src and "uuid" not in src,
              f"{cls.__name__}._fork 的新号出自单源函数（不自己拼 uuid）——"
              "入口与体里漂开格式就是「任务行开在 A、终态写在 B」")

    print("\n=== ② 预发：四条「没给出新版号」的来路都要换号，且四处键一致 ===")
    cases = (("没传 artifact_id，base 就是会话当前那一版（真机现场）", "", "_default", cur_key),
             ("传 _default，而它正是 base", "_default", "_default", cur_key),
             ("传的就是 base 号", old, old, old_key),
             ("没传 artifact_id，base 是一个显式旧版号", "", old, old_key))
    for label, given, base, base_key in cases:
        # 每轮把两个版号重新摆成 done：红态下上一轮就可能把账打坏，坏账不该记到本轮头上
        await seed_version("_default")
        await seed_version(old)
        body.calls.clear()
        j = json.loads(await server.handlers["patch_video"](
            None, session_id=sess, wait_sec=0, base_artifact_id=base,
            artifact_id=given,
            edits=[{"pointer": "/subtitles/0/text", "value": "改过了"}]))
        await settle(server)
        if not body.calls:
            # 后台任务在节点体之前就收了口（入口自己抛了）——红要红在断言上，不是崩在半路
            stuck = await storage.render_jobs.get(sess, view_artifact(j)) or {}
            check(False, f"{label} → 节点体根本没被执行，行被提前收口："
                         f"status={stuck.get('status')} error="
                         f"{str(stuck.get('error') or '')[:180]}")
            continue
        art = body.calls[0]["artifact_id"]
        check(art not in ("", "_default", base) and bool(SCOPE_RE.match(art)),
              f"{label} → 入口预发出新号 {art}")
        check(view_artifact(j) == art,
              f"{label} → 返回视图回的作用域与那一行同一个（视图 {view_artifact(j)} / "
              f"行 {art}）：轮询端据此续问，不一致等于让它等一条永远不动的行")
        row = await storage.render_jobs.get(sess, art)
        check(row is not None and row["status"] in TERMINAL
              and row.get("video_object_key") == f"renders/{sess}/{art}.mp4",
              f"{label} → 新号那一行开到终态（{(row or {}).get('status')}）")
        base_row = await storage.render_jobs.get(sess, base)
        check(base_row is not None and base_row["status"] == "done"
              and base_row.get("video_object_key") == base_key,
              f"{label} → base（{base}）的出片账没被抹：仍 done、key="
              f"{base_row and base_row.get('video_object_key')}")

    # 反向：调用方明确点了新版号（前端「另存一版」那条路）时不许再换一遍
    body.calls.clear()
    wanted = "keepme01"
    await server.handlers["patch_video"](
        None, session_id=sess, wait_sec=0, base_artifact_id="_default",
        artifact_id=wanted, edits=[{"pointer": "/subtitles/0/text", "value": "又改一次"}])
    await settle(server)
    check(body.calls[0]["artifact_id"] == wanted,
          f"显式指定新版号时原样沿用（实得 {body.calls[0]['artifact_id']}）——"
          "换号是入口的事，体里认得的号不许另起，否则又回到两行分开")
    check((await storage.render_jobs.get(sess, "_default") or {}).get(
              "video_object_key") == cur_key,
          "这一趟的 base（_default）账同样没动")

    print("\n=== ③ 代码链同样走这一道：入口那段判断不是口播链专属 ===")
    body.calls.clear()
    j2 = json.loads(await server.handlers["patch_motion_code"](
        None, session_id=sess, wait_sec=0, base_artifact_id=old,
        edits=[{"pointer": "/shots/0/text", "value": "图形链改一格"}]))
    await settle(server)
    art2 = body.calls[0]["artifact_id"]
    check(art2 not in ("", "_default", old) and bool(SCOPE_RE.match(art2)),
          f"代码链局部改也预发新号（{art2}）")
    check(view_artifact(j2) == art2
          and (await storage.render_jobs.get(sess, art2) or {}).get("status") in TERMINAL,
          f"代码链的视图 / 行 / 终态三处同为 {art2}")
    check((await storage.render_jobs.get(sess, old) or {}).get("video_object_key")
          == old_key, "代码链这一趟同样没碰 base 的出片账")

    print("\n=== ④ 修复前的形状复现：两行分开时是那三个症状 ===")
    # 这一小节不测修复，是把**病因**钉住：日后谁把预发那段拆回节点体、或让分发器按
    # 别的键开行，这三条会当场红，不必再等一次真机的假失败。
    sess4 = "u:tester:c:scope-old-shape"
    await storage.render_jobs.enqueue(sess4, "_default")
    await storage.render_jobs.succeed(sess4, "_default",
                                      f"renders/{sess4}/_default.mp4", 12.0,
                                      result={}, attempt=None)
    gate = asyncio.Event()
    minted: dict[str, str] = {}

    async def old_body(state: NodeState) -> None:
        await gate.wait()                     # 停在「行已开、终态未写」那一档
        state.artifact_id = new_patch_scope_id()   # 修复前：号到分叉这一步才产生
        minted["id"] = state.artifact_id
        await storage.render_jobs.open(sess4, state.artifact_id)
        await storage.render_jobs.succeed(sess4, state.artifact_id,
                                          f"renders/{sess4}/{state.artifact_id}.mp4",
                                          9.0, result={}, attempt=None)

    st = NodeState(session_id=sess4, artifact_id="_default", user_request="旧形状")
    # 修复前入口做的两件事：按调用方给的号（不给 → _default）开行、把这一步交给执行位
    await storage.render_jobs.enqueue(sess4, st.artifact_id or "_default")
    task = asyncio.ensure_future(old_body(st))
    await asyncio.sleep(0)
    v = await storage.render_jobs.get(sess4, "_default") or {}
    check(v.get("status") not in TERMINAL and not v.get("video_object_key"),
          f"症状一：base（_default）那一行的 done 账被复位成 {v.get('status')}、"
          f"key={v.get('video_object_key') or '空'}")
    check(minted.get("id") is None,
          "症状二：真正会写终态的那个号此刻根本没有行——轮询端按视图里的作用域续问，"
          "等的是一个从来没人写的键")
    gate.set()
    await task
    done_row = await storage.render_jobs.get(sess4, minted["id"]) or {}
    caller_row = await storage.render_jobs.get(sess4, "_default") or {}
    check(done_row.get("status") == "done",
          f"症状三：片子其实渲成了——done 写在 {minted['id']} 那一行")
    check(caller_row.get("status") not in TERMINAL,
          f"症状三之二：而调用方手上的 _default 仍停在 {caller_row.get('status')}，"
          "轮询端据此永远等不到终态，最后由看门狗按停滞判死（真机那句 error 原文："
          "「渲染停滞超过 600 秒无进度，已按失败收口」）")

    print(f"\n{'ALL PASSED' if _fails == 0 else f'{_fails} FAILED'} ({_checks} checks)")
    await storage.close()
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
