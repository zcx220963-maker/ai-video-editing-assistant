# -*- coding: utf-8 -*-
"""成片播放卡「刷新后重放」离线验证（README §6 的缺口修复）。

运行：  python tests/test_media_replay.py    # 全离线：内存替身 + mock 节点，不起容器/不联网

钉住三件事：
① 一轮成功渲染后，MediaCardHook 除了当轮回投，还把**持久链接**（渲染对象键 + artifact_id）
   落进本轮 assistant 行的 ``qa.parts``（新增 type=media 片段）——刷新前这条链接已在库里；
② GET /convs/{id}/messages 读历史时把这条链接重放成与**实时 WS media 帧同形**的播放卡
   （media_url 现签、title/duration 随附），前端复用同一张卡片组件即可渲染；
③ mock RenderVideoNode 的产物键集合与真节点（storyline_server/nodes/core_nodes.py
   RenderVideoNode.process 返回的 video/media_url/duration/width/height/title）对齐，
   兜底路径也能产「形状正确」的可播卡片。
外加一条回归：无渲染的普通轮次不回放进片卡，且跨轮 contextvar 残留会被 user 行清干净。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework.agent import Agent, AgentConfig, AgentOnceRun
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.hooks import AgentHookContext, CompositeHook, MediaCardHook, _find_media
from agent_framework.llm import ScriptedLLM
from agent_framework.media_replay import record_rendered_media
from agent_framework.orchestration import Interceptor, NodeState
from agent_framework.session import Session, SessionManager
from agent_framework.server import create_app
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry
from agent_framework.video_editing import build_agent_registry, build_node_registry

# 真节点成片产物的**文档契约键**（对齐 storyline_server/nodes/core_nodes.py:1146-1151）。
REAL_NODE_CONTRACT_KEYS = {"video", "media_url", "duration", "width", "height", "title"}

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class _RecorderMQ:
    """只记录 OutBound 帧的假 MQ：既满足 MediaCardHook 的当轮回投，又便于断言实时那一路没退化。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        self.published.append((topic, payload))


async def _drive_render(storage, user_id: str, conv_id: str) -> dict:
    """把「一轮渲染」跑通到 assistant 行落库：mock 剪辑节点 + MediaCardHook，走真实 Agent 回合。"""
    await storage.conversations.ensure(user_id, conv_id)
    mq = _RecorderMQ()
    registry, _ = build_agent_registry(build_node_registry(),
                                       NodeState(session_id=f"{user_id}:{conv_id}"))
    llm = ScriptedLLM([("tool", "render_video", {}), ("answer", "已渲染成片")])
    agent = AgentOnceRun(
        llm, registry,
        hooks=CompositeHook([MediaCardHook(mq)]),
        config=AgentConfig(max_iterations=5),
        storage=storage,
    )
    await agent.run(Session(user_id=user_id, conversation_id=conv_id), "剪一支片子", run_id="runA")

    hist = await storage.messages.history(user_id, conv_id)
    assistant_row = next((r for r in hist if r["role"] == "assistant"), None)
    media_parts = [p for p in ((assistant_row or {}).get("qa") or {}).get("parts", [])
                   if isinstance(p, dict) and p.get("type") == "media"]
    object_key = media_parts[0]["video_object_key"] if media_parts else ""
    # 让 presign 指向真实字节（completed render 的语义），离线内存里放一小段假 mp4。
    if object_key:
        async def chunks():
            yield b"FAKE-MP4-BYTES"
        await storage.objects.put(object_key, chunks(), content_type="video/mp4")
    return {"hist": hist, "assistant_row": assistant_row, "media_parts": media_parts,
            "object_key": object_key, "published": mq.published}


async def _mock_render_output() -> dict:
    """直接跑 mock 渲染节点，拿到返回给客户端的产物（供契约键比对）。"""
    state = NodeState(session_id="u:mx:c:mc", artifact_id="artm", user_request="剪一支演示片")
    itp = Interceptor(build_node_registry())
    return await itp.invoke("render_video", state)


async def _plain_turn_no_render(storage, user_id: str, conv_id: str) -> list:
    """一轮没有渲染的普通对话，验证历史里不会出现成片卡。"""
    await storage.conversations.ensure(user_id, conv_id)
    mq = _RecorderMQ()
    registry, _ = build_agent_registry(build_node_registry(), NodeState(session_id=f"{user_id}:{conv_id}"))
    llm = ScriptedLLM([("answer", "只是聊了聊")])
    agent = AgentOnceRun(llm, registry, hooks=CompositeHook([MediaCardHook(mq)]),
                         config=AgentConfig(max_iterations=3), storage=storage)
    await agent.run(Session(user_id=user_id, conversation_id=conv_id), "随便聊聊")
    return await storage.messages.history(user_id, conv_id)


async def _capture_real_shaped(storage, user_id: str, conv_id: str) -> dict:
    """喂给 MediaCardHook 一条**真远程节点形状**的工具结果（顶层带 artifact_id），
    验证同一套 record→assistant 落库路径把 artifact_id/duration/title 如实记下。"""
    await storage.conversations.ensure(user_id, conv_id)
    real_packed = {
        "node": "render_video", "artifact_id": "A9",
        "output": {"video": f"renders/{user_id}_{conv_id}/A9.mp4",
                   "media_url": "memory://creation-assets/x?ttl=3600",
                   "duration": 2.5, "width": 640, "height": 360, "title": "带产物名的成片"},
    }
    ctx = AgentHookContext(
        session=Session(user_id=user_id, conversation_id=conv_id),
        messages=[{"role": "tool", "tool_call_id": "t1", "name": "render_video",
                   "content": json.dumps(real_packed, ensure_ascii=False)}],
        extras={"run_id": "rA9"},
    )
    await MediaCardHook(_RecorderMQ()).after_execute_tools(ctx)
    return await storage.messages.append(
        user_id, conv_id, "assistant", content="好了",
        qa={"parts": [{"type": "answer", "content": "好了"}]})


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        # 只为端点提供鉴权与读历史；渲染回合由 AgentOnceRun 直接跑在同一份 storage 上。
        app_agent = Agent(
            llm=ScriptedLLM(steps=[("answer", "x")]),
            registry=ToolRegistry(),
            session_manager=SessionManager(storage),
            context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
            config=AgentConfig(max_iterations=2),
            storage=storage,
        )
        app = create_app(app_agent, InMemoryMessageQueue(), storage=storage)
        with TestClient(app) as client:
            j = client.post("/register", json={}).json()
            uid, hdr = j["user_id"], {"Authorization": f"Bearer {j['token']}"}

            # ---- ① 成功渲染：持久链接进了 assistant 行的 qa.parts ----
            res = asyncio.run(_drive_render(storage, uid, "cmr"))
            check(bool(res["media_parts"]), "渲染当轮把持久链接写进了 assistant 行（qa.parts 有 type=media）")
            mp = res["media_parts"][0] if res["media_parts"] else {}
            check(mp.get("video_object_key", "").startswith("renders/")
                  and mp["video_object_key"].endswith(".mp4"),
                  f"持久链接存的是渲染对象键（稳定指针，非临时直链）：{mp.get('video_object_key')}")
            check("video_object_key" in mp and "title" in mp and "duration" in mp,
                  f"持久链接带齐对象键与元信息（mock 兜底 artifact_id 为 None 无妨）：{mp}")
            # 当轮回投那条 WS 帧没退化：仍是 type=media、带临时 presigned media_url。
            live = [p for _t, p in res["published"] if p.get("type") == "media"]
            check(len(live) == 1 and live[0].get("media_url", "").startswith("memory://"),
                  "实时当轮 OutBound media 帧照旧回投（本次修复没动这条路）")

            # 真远程节点的 packed 结果（顶层带 artifact_id）经同一条路径被如实持久化。
            real_row = asyncio.run(_capture_real_shaped(storage, uid, "c_real"))
            rmp = next((p for p in (real_row.get("qa") or {}).get("parts", [])
                        if isinstance(p, dict) and p.get("type") == "media"), {})
            check(rmp.get("artifact_id") == "A9" and rmp.get("duration") == 2.5
                  and rmp.get("title") == "带产物名的成片",
                  f"真节点 packed 形状的 artifact_id/时长/标题被完整记录：{rmp}")

            # ---- ② 历史端点把持久链接重放成可播卡（与 WS media 帧同形）----
            got = client.get("/convs/cmr/messages", headers=hdr)
            check(got.status_code == 200, f"GET /convs/cmr/messages → 200（{got.status_code}）")
            msgs = got.json()["messages"]
            cards = [c for m in msgs for c in m.get("media", [])]
            check(len(cards) == 1, f"刷新重放出一条成片卡：{cards}")
            card = cards[0] if cards else {}
            check(isinstance(card.get("media_url"), str)
                  and card["media_url"].startswith("memory://")
                  and res["object_key"] in card["media_url"],
                  f"卡片带 media_url 形 presigned 直链：{card.get('media_url')}")
            check(set(card) >= {"media_url", "title", "duration"},
                  f"卡片形状与实时 WS media 帧同字段（前端复用同一组件）：{sorted(card)}")

            # ---- 回归：没有渲染的轮次不回放进片卡 ----
            plain = asyncio.run(_plain_turn_no_render(storage, uid, "cplain"))
            any_media = any(p.get("type") == "media"
                            for r in plain if r.get("qa")
                            for p in (r["qa"] or {}).get("parts", []))
            check(not any_media, "无渲染轮次：assistant 行里没有 media 片段")
            check(client.get("/convs/cplain/messages", headers=hdr).json()["messages"][1].get("media") is None,
                  "无渲染轮次：历史端点不吐出成片卡")

    # ---- ③ mock 渲染产物键 == 真节点文档契约键 ----
    packed = asyncio.run(_mock_render_output())
    out = packed["output"]
    check(set(out.keys()) == REAL_NODE_CONTRACT_KEYS,
          f"mock RenderVideoNode 产物键与真节点契约一致：{sorted(out.keys())}")
    check(isinstance(out.get("video"), str) and out["video"] == "renders/u_mx_c_mc/artm.mp4",
          f"mock 的 video 是会话/产物作用域下的对象键（同真节点布局）：{out.get('video')}")
    check("://" in (out.get("media_url") or ""),
          f"mock 的 media_url 是可被 _find_media 认出的链接形状：{out.get('media_url')}")
    found = _find_media(out)
    check(found is out and found.get("video") == out["video"],
          "MediaCardHook 的 _find_media 能从 mock 产物里抓到 media_url + 对象键")

    # ---- 跨轮残留回归：recorded 但本轮没渲染落库时，user 行把它清掉，不污染下一条 assistant ----
    with tempfile.TemporaryDirectory() as td:
        st = build_storage("memory", cache_root=Path(td) / "cache", workspace_root=Path(td) / "ws")

        async def _leak_guard():
            await st.users.provision("u_leak")
            await st.conversations.ensure("u_leak", "c_leak")
            record_rendered_media({"video_object_key": "renders/stale/x.mp4", "artifact_id": "x",
                                   "title": "残留", "duration": 1.0})
            await st.messages.append("u_leak", "c_leak", "user", content="新一轮")   # 起点：清空
            row = await st.messages.append("u_leak", "c_leak", "assistant", content="答复",
                                           qa={"parts": [{"type": "answer", "content": "答复"}]})
            return row
        row = asyncio.run(_leak_guard())
        leaked = [p for p in (row.get("qa") or {}).get("parts", []) if p.get("type") == "media"]
        check(not leaked, "上一轮未落库的成片残留不会串进下一轮 assistant 行")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
