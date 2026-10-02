"""流式回投链路验证（不联网）：on_stream Hook → MQ OutBound → Connection Manager → WS。

覆盖文档第 6 节「结果发给谁」：Agent 处理结果携带 session_id，经 OutBound 由
Connection Manager 找回对应会话的 Web 连接；跨会话不串台。

运行：  python tests/test_connection_manager.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework.agent import Agent, AgentConfig
from agent_framework.connection_manager import CONNECTION_GROUP, OUTBOUND_TOPIC, ConnectionManager
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.hooks import CompositeHook, OutboundStreamHook
from agent_framework.llm import ScriptedLLM
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeWS:
    """替身 WebSocket：只收集 send_json 的消息。"""

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.received.append(payload)


def build_agent(mq: InMemoryMessageQueue, answer_text: str) -> Agent:
    llm = ScriptedLLM(steps=[("answer", answer_text)], stream_chunk_size=2)
    return Agent(
        llm=llm,
        registry=ToolRegistry(),
        session_manager=SessionManager(),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        hooks=CompositeHook([OutboundStreamHook(mq)]),
        config=AgentConfig(max_iterations=3),
    )


# ----------------------------------------------------------------------
# ① Connection Manager 单元路由：按 session_id 找回连接、多标签页、跨会话隔离
# ----------------------------------------------------------------------


async def part1() -> None:
    mq = InMemoryMessageQueue()
    cm = ConnectionManager(mq)
    cm.start()
    ws_a1_tab1, ws_a1_tab2, ws_b1 = FakeWS(), FakeWS(), FakeWS()
    cm.connect("A:1", ws_a1_tab1)
    cm.connect("A:1", ws_a1_tab2)
    cm.connect("B:1", ws_b1)
    await mq.start()
    await mq.publish(OUTBOUND_TOPIC, "A:1", {"type": "delta", "session_id": "A:1", "text": "你好"})
    await mq.drain()
    check(
        all(m["text"] == "你好" for m in ws_a1_tab1.received) and len(ws_a1_tab1.received) == 1,
        "session_id 命中 → 推送到对应连接",
    )
    check(len(ws_a1_tab2.received) == 1, "同一会话多标签页都收到")
    check(ws_b1.received == [], "其它会话不串台")
    cm.disconnect("A:1", ws_a1_tab1)
    check(cm.sockets_of("A:1") == 1, "disconnect 只摘掉该连接")
    await mq.stop()


# ----------------------------------------------------------------------
# ② Agent 流式执行 → delta / stream_end 消息带 run_id 落到 OutBound
# ----------------------------------------------------------------------


async def part2() -> None:
    mq = InMemoryMessageQueue()
    seen: list[dict] = []
    mq.subscribe(OUTBOUND_TOPIC, CONNECTION_GROUP, lambda p: seen.append(p))
    agent = build_agent(mq, "流式回复内容")
    await mq.start()
    answer = await agent.handle("u", "c9", "讲个故事", run_id="r-42", stream=True)
    await mq.drain()
    await mq.stop()
    deltas = [m for m in seen if m["type"] == "delta"]
    ends = [m for m in seen if m["type"] == "stream_end"]
    check(answer == "流式回复内容", "Agent 仍返回完整答案")
    check(len(deltas) >= 2, "on_stream 逐块落入 OutBound（多于 1 段）")
    check("".join(d["text"] for d in deltas) == "流式回复内容", "delta 拼接等于最终答案")
    check(all(d["session_id"] == "u:c9" for d in deltas), "delta 携带 session_id")
    check(all(d["run_id"] == "r-42" for d in deltas), "delta 携带 run_id（ctx.extras）")
    check(len(ends) == 1 and ends[0]["type"] == "stream_end", "stream_end 收尾标记")


# ----------------------------------------------------------------------
# ③ 端到端：WS /ws/{conv}?token= + 带凭证的 POST /chat → 前端收到 delta 与 answer
# ----------------------------------------------------------------------


def part3() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        mq = InMemoryMessageQueue()
        agent = build_agent(mq, "端到端流式答案验证")
        app = create_app(agent, mq, storage=storage)
        with TestClient(app) as client:
            tok = client.post("/register", json={}).json()["token"]
            headers = {"Authorization": f"Bearer {tok}"}
            with client.websocket_connect("/ws/ck?token=" + tok) as ws:
                first = ws.receive_json()
                check(first["type"] == "connected", "WS 建连回执 connected")
                r = client.post("/chat", json={"conversation_id": "ck", "message": "开始"},
                                headers=headers)
                run_id = r.json()["run_id"]

                received: list[dict] = []
                deadline = time.time() + 5
                while time.time() < deadline:
                    msg = ws.receive_json()
                    received.append(msg)
                    if msg["type"] == "answer":
                        break

                kinds = [m["type"] for m in received]
                check("delta" in kinds, "WS 实时收到 delta")
                check("stream_end" in kinds, "WS 收到 stream_end")
                check(kinds[-1] == "answer", "WS 最终收到 answer（收尾）")
                ans = received[-1]
                check(ans["answer"] == "端到端流式答案验证" and ans["run_id"] == run_id, "answer 内容与 run_id 正确")
                check(
                    "".join(m["text"] for m in received if m["type"] == "delta") == "端到端流式答案验证",
                    "delta 流拼接还原完整答案",
                )
                check(first["session_id"].startswith("u-") and first["session_id"].endswith(":ck"),
                      f"回投键的 user 段由 token 反查：{first['session_id']}")


# ----------------------------------------------------------------------
# ④ WS 出口的中文名词表：残片不发、双轨、缓冲里仍是原帧
# ----------------------------------------------------------------------


async def part4() -> None:
    from agent_framework.catalog import ToolCatalog, get_catalog, set_catalog

    set_catalog(ToolCatalog({"split_shots": "镜头切分", "render_video": "成片渲染"}))
    try:
        mq = InMemoryMessageQueue()
        cm = ConnectionManager(mq)
        cm.start()
        ws = FakeWS()
        cm.connect("A:1", ws)
        await mq.start()
        await mq.publish(OUTBOUND_TOPIC, "A:1",
                         {"type": "delta", "session_id": "A:1", "text": "先跑 split_sho"})
        await mq.drain()
        check(ws.received == [{"type": "delta", "session_id": "A:1", "text": "先跑 "}],
              "只扣住帧尾那半个机器名，安全前缀照常发（残片绝不出口）")
        await mq.publish(OUTBOUND_TOPIC, "A:1",
                         {"type": "delta", "session_id": "A:1", "text": "ts 再看成片"})
        await mq.drain()
        check("".join(m["text"] for m in ws.received) == "先跑 镜头切分 再看成片",
              "两帧拼起来才换完整中文名，且没有残片")
        check(all("split_sho" not in m["text"] for m in ws.received), "出去的字里找不到机器名残片")

        await mq.publish(OUTBOUND_TOPIC, "A:1", {
            "type": "tool_result", "session_id": "A:1", "tool": "split_shots",
            "arguments": {"node": "split_shots", "file_name": "split_shots.mp4"},
            "result": "split_shots 产出 12 个镜头", "session_hint": "A:1",
        })
        await mq.drain()
        frame = ws.received[-1]
        check(frame["tool"] == "split_shots" and frame["tool_display"] == "镜头切分",
              "键位双轨：tool 留机器名，另挂 tool_display")
        check(frame["arguments"]["node"] == "split_shots"
              and frame["arguments"]["node_display"] == "镜头切分", "嵌套 args 同样双轨")
        check(frame["arguments"]["file_name"] == "split_shots.mp4",
              "文件名是用户内容：一个字都不动")
        check(frame["result"] == "镜头切分 产出 12 个镜头", "自由文本整词替换")
        check(frame["session_id"] == "A:1" and frame["session_hint"] == "A:1",
              "id 类字段与查不到的串原样留着")

        await mq.publish(OUTBOUND_TOPIC, "A:1", {
            "type": "tool_call", "session_id": "A:1", "tool": "render_video",
            "arguments": {"wait_sec": 8, "target_duration_sec": 60, "nope": 1},
        })
        await mq.drain()
        call = ws.received[-1]
        check(call["arg_labels"] == {"wait_sec": "等待时长（秒）",
                                    "target_duration_sec": "目标时长（秒）"},
              "tool_call 帧在出口整帧挂 arg_labels：参数名的中文也来自服务端单源表")
        check(call["arguments"]["wait_sec"] == 8 and "nope" not in call["arg_labels"],
              "标签是旁路：参数键与值都不动，查不到的参数不占位")

        await mq.publish(OUTBOUND_TOPIC, "A:1",
                         {"type": "stream_end", "session_id": "A:1", "iteration": 1})
        await mq.drain()
        tail = "".join(m["text"] for m in ws.received if m["type"] == "delta")
        check(tail == "先跑 镜头切分 再看成片", "stream_end 前已 flush，没有字被扣死")

        raw = [m for m in cm.sent if m["type"] == "tool_result"][0]
        check(raw["tool"] == "split_shots" and raw["result"].startswith("split_shots"),
              "记账/环形缓冲里是原帧（出口只改写「发出去」的那一份）")
        buf = cm.get_buffer("A:1")
        check(any(m.get("tool") == "split_shots" for m in buf), "缓冲存机器名，重放走同一出口")
        ws2 = FakeWS()
        cm.connect("A:1", ws2)
        await cm.replay_to("A:1", ws2)
        replayed = [m for m in ws2.received if m.get("type") == "tool_result"]
        check(replayed and replayed[0]["tool_display"] == "镜头切分", "重放的帧同样带中文旁路")
        await mq.stop()

        set_catalog(ToolCatalog())
        mq = InMemoryMessageQueue()
        cm = ConnectionManager(mq)
        cm.start()
        ws = FakeWS()
        cm.connect("A:1", ws)
        await mq.start()
        await mq.publish(OUTBOUND_TOPIC, "A:1",
                         {"type": "delta", "session_id": "A:1", "text": "split_shots"})
        await mq.drain()
        await mq.publish(OUTBOUND_TOPIC, "A:1",
                         {"type": "tool_result", "session_id": "A:1", "tool": "split_shots"})
        await mq.drain()
        check([m["text"] for m in ws.received if m["type"] == "delta"] == ["split_shots"]
              and ws.received[-1].get("tool_display") is None,
              "词表为空（Storyline 未连通）→ 全直通，不静默改字")
        await mq.publish(OUTBOUND_TOPIC, "A:1",
                         {"type": "tool_call", "session_id": "A:1", "tool": "split_shots",
                          "arguments": {"material_ids": ["m1"]}})
        await mq.drain()
        bare = ws.received[-1]
        check(bare.get("tool_display") is None
              and bare["arg_labels"] == {"material_ids": "素材"},
              "参数标签那半张表不靠 Storyline：空词表照样挂，工具名照旧不编")
        await mq.stop()
    finally:
        set_catalog(ToolCatalog())


async def part5() -> None:
    print("\n[⑤ 答复落定即清缓冲：重放只补在途的那一轮]")
    mq = InMemoryMessageQueue()
    cm = ConnectionManager(mq)
    cm.start()
    ws = FakeWS()
    cm.connect("A:1", ws)
    cm.connect("A:2", FakeWS())
    cm.connect("A:3", FakeWS())
    await mq.start()

    async def send(session: str, typ: str, run: str, **extra: Any) -> None:
        await mq.publish(OUTBOUND_TOPIC, session,
                         {"type": typ, "session_id": session, "run_id": run, **extra})
        await mq.drain()

    await send("A:1", "delta", "r-1", text="第一段")
    await send("A:1", "plan", "r-1", plans=[{"plan_id": "p1"}])
    await send("A:1", "answer", "r-1", answer="这一轮答完了")
    # 新契约：这一轮的**中间帧**清掉，但终答保留。
    # 旧契约（answer 不留痕）只在前端真的会重读历史时成立；前端重连路径不重读历史，
    # 于是断连恰好落在收尾那几秒时这条答复永久消失（要整页刷新）。
    # 保留终答 + 重放打 replayed 标记交客户端去重，是「宁可让客户端有机会去重，
    # 也不要让答复永久消失」的取舍。
    buf1 = cm.get_buffer("A:1")
    check([m["type"] for m in buf1] == ["answer"],
          f"answer 落定＝只清中间帧、保留终答（实际 {[m['type'] for m in buf1]}）")
    check(buf1 and buf1[0].get("answer") == "这一轮答完了", "保留的终答内容完整")
    check([m["type"] for m in ws.received] == ["delta", "plan", "answer"],
          "清的是缓冲，正在看的连接一帧不少")

    await send("A:2", "delta", "r-2", text="还在跑")
    await send("A:2", "answer", "r-2", answer="r-2 收尾")
    await send("A:2", "delta", "r-3", text="下一条已经在写帧")
    await send("A:2", "answer", "r-2b", answer="不该动别人")
    buf2 = cm.get_buffer("A:2")
    check([m["run_id"] for m in buf2] == ["r-3", "r-2b"],
          f"只清已落定那几轮的中间帧，别条 run 的在途帧留着（实际 {[m['run_id'] for m in buf2]}）")
    check([m["type"] for m in buf2 if m["run_id"] == "r-3"] == ["delta"],
          "下一条 run 的在途进度照旧补看")
    check([m["type"] for m in buf2 if m["run_id"] == "r-2b"] == ["answer"],
          "后到的终答本身保留（它是「至少拿得到」的那一条）")

    await send("A:3", "delta", "A", text="分叉前的叙述")
    await send("A:3", "delta", "B", text="分叉后接着说")
    await send("A:3", "answer", "B", answer="同一条答复的收尾")
    buf3 = cm.get_buffer("A:3")
    check([(m["type"], m["run_id"]) for m in buf3] == [("delta", "A"), ("answer", "B")],
          "分叉前的叙述留着（它没被判成已落定），落定那一轮只留终答："
          f"实际 {[(m['type'], m['run_id']) for m in buf3]}")

    late = FakeWS()
    cm.connect("A:1", late)
    n = await cm.replay_to("A:1", late)
    check(n == 1 and [m["type"] for m in late.received] == ["answer"],
          f"已完成那一轮能重放出终答（实际 {n} 帧）")
    check(all(m.get("replayed") for m in late.received),
          "重放的帧带 replayed 标记——客户端据此与 REST 历史去重")
    await mq.stop()


def main() -> None:
    asyncio.run(part1())
    asyncio.run(part2())
    part3()
    asyncio.run(part4())
    asyncio.run(part5())
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
