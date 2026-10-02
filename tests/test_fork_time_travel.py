"""分叉与时间旅行验证（不联网）：checkpoint 增量链 + fork 原语 + rerun_from 接线。

覆盖四件事（都是「换 BGM 重渲染、上游产物全复用」这条真实诉求的组成部分）：
  1. 一致点链：指针行 checkpoints + 增量 checkpoint_entries，``load(at_seq)`` 能截断重建；
  2. ``fork``：新产物作用域 + 父产物集复制 + 按契约下游作废，父 run 让位 superseded；
  3. ``rerun_from`` 工具：模型只报节点名，本轮执行就地接到子 run（上下文回退、作用域换绑）；
  4. ``rerun_from(run_id=…)``：跨轮回溯到**本会话更早的一次执行**，含前缀解析、歧义回绝、
     跨会话回绝、找不到节点时把历史执行当路标交出来，以及本轮那条被弃用的 run 也得让位。

运行：  python tests/test_fork_time_travel.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun
from agent_framework.checkpoint import STATUS_SUPERSEDED, CheckpointManager, rebuild
from agent_framework.context import ContextBuilder
from agent_framework.editing_contract import ContractSlot, EditingContract, NodeContract
from agent_framework.hooks import AgentHookContext, _current_hook_ctx
from agent_framework.identity import current_identity
from agent_framework.llm import ScriptedLLM
from agent_framework.session import Session
from agent_framework.storage import build_storage
from agent_framework.tool import Tool, ToolError, ToolRegistry
from agent_framework.tools.runs import register_run_tools

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# 一段最小剪辑 DAG：只有 select_BGM 及其下游会在分叉时被作废。
UPSTREAM = ["load_media", "asr", "generate_script"]
RERUN = ["select_BGM", "plan_timeline", "render_video"]
CHAIN = UPSTREAM + RERUN
CONTRACT = EditingContract(nodes={
    **{n: NodeContract(name=n, requires=()) for n in UPSTREAM},
    "select_BGM": NodeContract("select_BGM", ("generate_script",)),
    "plan_timeline": NodeContract("plan_timeline", ("select_BGM", "asr")),
    "render_video": NodeContract("render_video", ("plan_timeline",)),
})


def steps(nodes: list[str]) -> list[dict]:
    """节点名 → 该节点在消息流里留下的两条（assistant 调用 + tool 结果）。"""
    out: list[dict] = []
    for n in nodes:
        out += [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": f"c-{n}", "type": "function",
                             "function": {"name": n, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c-{n}", "name": n,
             "content": json.dumps({"node": n})},
        ]
    return out


def tools_of(messages: list[dict]) -> list[str]:
    return [m.get("name", "") for m in messages if m.get("role") == "tool"]


async def part1_chain() -> None:
    print("\n[1] 指针行 + 增量链：每轮只写新增段，at_seq 可截断重建")
    st = build_storage("memory")
    mgr = CheckpointManager(st)
    head = [{"role": "system", "content": "S"}, {"role": "user", "content": "剪个旅行vlog"}]
    cp = await mgr.begin("u1:c1", "剪个旅行vlog", head, "run-p")
    msgs = list(head)
    for i, node in enumerate(CHAIN):
        msgs = [*msgs, *steps([node])]
        await mgr.save_progress(cp, iteration=i + 1, messages=msgs)
    await mgr.complete(cp, messages=[*msgs, {"role": "assistant", "content": "已出片"}])

    row = (await st.db.select("checkpoints"))[0]
    entries = sorted(await st.db.select("checkpoint_entries"), key=lambda e: e["seq"])
    check(row["head_seq"] == len(entries) - 1 and row["status"] == "completed",
          f"指针行只记链尾 seq={row['head_seq']}")
    check("messages" not in row, "checkpoints 行里不再有整条 messages（写放大已消除）")
    check([e["kind"] for e in entries] == ["full"] + ["delta"] * (len(entries) - 1),
          f"链首一条 full、其后全是 delta：{ {e['kind'] for e in entries} }")
    check(max(len(e["payload"]) for e in entries[1:]) == 2,
          "每条 delta 只装本轮新增的两条消息，不是整条历史")
    check([h["seq"] for h in await mgr.history("run-p")] == [e["seq"] for e in entries],
          "history 逐项列出走过的一致点")

    early = await mgr.load("run-p", at_seq=2)
    check(early is not None and len(early.messages) == len(head) + 2 * 2,
          f"时间旅行取 seq<=2 只剩 {(early and len(early.messages))} 条消息")
    check(tools_of(early.messages) == CHAIN[:2], f"截断点上的工具结果：{tools_of(early.messages)}")
    check(rebuild([{"seq": 0, "kind": "full", "payload": head}]) == head,
          "rebuild 纯函数：full 重起基准、delta 追加")


async def part2_fork() -> None:
    print("\n[2] fork：新产物作用域 + 父产物集复用 + 下游作废")
    st = build_storage("memory")
    mgr = CheckpointManager(st)
    sid = "u1:c1"
    arts = st.artifacts(sid, "")        # 父 run 未显式换作用域 → _default 集
    for node in CHAIN:
        await arts.put(node, {"node": node})

    head = [{"role": "system", "content": "S"}, {"role": "user", "content": "换个BGM重新出片"}]
    cp = await mgr.begin(sid, head[1]["content"], head, "run-p",
                         scope={"storyline_session": sid, "artifact_id": ""})
    msgs = list(head)
    for node in CHAIN:
        msgs = [*msgs, *steps([node])]
        await mgr.save_progress(cp, iteration=len(msgs), messages=msgs)

    seq = await mgr.seq_before_tool("run-p", "select_BGM")
    check(seq == len(UPSTREAM), f"分叉点落在 select_BGM 之前（seq={seq}）")
    try:
        await mgr.seq_before_tool("run-p", "no_such_node")
        check(False, "未知节点应当回绝")
    except KeyError as e:
        check("没有" in str(e.args[0]) and "load_media" in str(e.args[0]),
              "未知节点如实回绝，并列出这个 run 已有的工具记录")

    dropped = sorted(CONTRACT.downstream("select_BGM"))
    child = await mgr.fork("run-p", seq, invalidate=dropped)
    check(child.forked_from == "run-p" and child.forked_at_seq == seq,
          f"子 run 记下来源：{child.forked_from} @ seq={child.forked_at_seq}")
    check(child.artifact_id and child.artifact_id != "art-p",
          f"子 run 换了产物作用域：{child.artifact_id}")
    check(tools_of(child.messages) == UPSTREAM, f"只带走分叉点之前的结果：{tools_of(child.messages)}")
    try:
        await mgr.seq_before_tool(child.run_id, "load_media")
        check(False, "链首结果不能当分叉点")
    except KeyError as e:
        check("链首" in str(e.args[0]),
              "子 run 的链首整段是复制来的结果 → 再分叉只能回绝（不是静默开空分支）")

    got = await st.artifacts(sid, child.artifact_id).executed()
    check(set(UPSTREAM) <= set(got), f"上游产物整份复制过来可复用：{got}")
    check(not set(RERUN) & set(got), "重跑点及其下游已作废，拦截器会真把它们重跑一遍")
    check(await arts.has("select_BGM") and await arts.has("render_video"),
          "父 run 的产物集一个字没动（旧版本仍可回看）")

    prow = await st.db.get_by_pk("checkpoints", {"run_id": "run-p"})
    check(prow["status"] == STATUS_SUPERSEDED, "fork 已就地让父 run 让位 superseded（不靠调用方补做）")
    check(all(c.run_id != "run-p" for c in await mgr.pending()),
          "superseded 不进崩溃恢复候选（不会重播用户不要的分支）")
    resume_pick = await mgr.pending_for_session("u1:c1")
    check(resume_pick is not None and resume_pick.run_id == child.run_id,
          f"「继续」挑中的是分叉出的子 run，不是让位的父 run：{resume_pick and resume_pick.run_id}")

    # 分叉带新诉求：回退后的上下文末尾若还是父 run 那句旧话，模型只会照旧话再演一遍。
    reask = "背景音乐改用另一首，然后重做时间线与出片"
    again = await mgr.fork("run-p", seq, message=reask)
    check(again.message == reask, "子 run 记下这次的新诉求")
    last = again.messages[-1]
    get = lambda m, k: (m.get(k) if isinstance(m, dict) else getattr(m, k))
    check(get(last, "role") == "user" and get(last, "content") == reask,
          f"新诉求接在回退点之后当最新一条用户指令：{get(last, 'content')}")
    row = await st.db.get_by_pk("checkpoints", {"run_id": again.run_id})
    check(row["message"] == reask, "新诉求随指针行落库（崩溃恢复后仍在）")
    check(tools_of(again.messages) == UPSTREAM,
          "带新诉求不改动复用范围：仍只带走分叉点之前的结果")


class SelectBGMStub(Tool):
    """最小剪辑节点工具：只记录「这次调用落在哪个产物作用域上」。"""

    def __init__(self, scopes: list[str]) -> None:
        self._scopes = scopes

    @property
    def name(self) -> str:
        return "select_BGM"

    @property
    def description(self) -> str:
        return "从曲库选配乐"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self) -> str:
        self._scopes.append(current_identity().artifact_id)
        return json.dumps({"node": "select_BGM", "bgm": "夏日小曲"}, ensure_ascii=False)


async def part3_rerun_tool() -> None:
    print("\n[3] rerun_from 工具：模型只报节点名，本轮就地接到子 run")
    st = build_storage("memory")
    mgr = CheckpointManager(st)
    registry = ToolRegistry()
    scopes: list[str] = []
    registry.register(SelectBGMStub(scopes))
    # 装配期发出去的是引用：契约在 Storyline 连上后才 fill，工具每次现读。
    slot = ContractSlot()
    register_run_tools(registry, slot)
    ask = "背景音乐改用《秋日小曲》，select_BGM 的 query 填秋日小曲"

    async def run_once(user: str, instruction: str = "") -> str:
        agent = AgentOnceRun(ScriptedLLM([
            ("tool", "select_BGM", {}),
            ("tool", "rerun_from", {"node": "select_BGM", "instruction": instruction}),
            ("tool", "select_BGM", {}),
            ("answer", "已换 BGM 重新出片"),
        ]), registry, context_builder=ContextBuilder("S"),
            config=AgentConfig(max_iterations=8), checkpoint=mgr, storage=st)
        return await agent.run(Session(user_id=user, conversation_id=user),
                               "换个BGM重新出片")

    answer = await run_once("u2")
    check(answer == "已换 BGM 重新出片", "契约未接入时 rerun_from 只回绝，循环照常收尾")
    check(scopes and len(set(scopes)) == 1, f"未分叉则不会偷偷换作用域：{scopes}")
    n_before = len(scopes)

    slot.fill(CONTRACT)
    answer = await run_once("u3", ask)
    check(answer == "已换 BGM 重新出片", "分叉后本轮继续跑到最终答复")
    new_scopes = scopes[n_before:]
    check(len(new_scopes) == 2 and new_scopes[0] != new_scopes[1],
          f"同一轮里产物作用域被换绑：{new_scopes}")

    runs = [r for r in await st.db.select("checkpoints") if r["session_id"] == "u3:u3"]
    parent = next(r for r in runs if not r["forked_from"])
    kid = next(r for r in runs if r["forked_from"])
    check(kid["forked_from"] == parent["run_id"], "父 run 分出一条子 run")
    check(parent["status"] == STATUS_SUPERSEDED and kid["status"] == "completed",
          f"父让位、子收尾：{parent['status']} / {kid['status']}")
    kid_cp = await mgr.load(kid["run_id"])
    check(any("已回到一致点" in str(m.get("content", "")) for m in kid_cp.messages),
          "回退事实以 system 消息告知模型（不是静默改写历史）")
    # 回退之后最后一条诉求必须是这次的新诉求，否则模型只会照旧话再演一遍
    # （真机上出现过：分叉不带话，子 run 连着把同一首 BGM 又选了好几遍）。
    check(kid["message"] == ask, f"新诉求随 rerun_from 落进子 run 的指针行：{kid['message']}")
    check(any(m.get("role") == "user" and ask in str(m.get("content", ""))
              for m in kid_cp.messages), "新诉求以用户指令进子 run 上下文（回退点之后）")
    check(tools_of(kid_cp.messages) == ["select_BGM"],
          f"子 run 只重跑了 select_BGM：{tools_of(kid_cp.messages)}")
    check(tools_of((await mgr.load(parent["run_id"])).messages) == ["select_BGM"],
          "父 run 落库的那一版原样保留（select_BGM 还在），分叉没有改写它的历史")


async def _seed_run(mgr: CheckpointManager, st, sid: str, run_id: str,
                    nodes: list[str], message: str, *, finish: bool = True):
    """造一条走过 nodes 的执行：产物落它的 ``_default`` 作用域，链按一致点增量落盘。"""
    head = [{"role": "system", "content": "S"}, {"role": "user", "content": message}]
    cp = await mgr.begin(sid, message, head, run_id,
                         scope={"storyline_session": sid, "artifact_id": ""})
    arts = st.artifacts(sid, "")
    msgs = list(head)
    for i, node in enumerate(nodes):
        await arts.put(node, {"node": node})
        msgs = [*msgs, *steps([node])]
        await mgr.save_progress(cp, iteration=i + 1, messages=msgs)
    if finish:
        await mgr.complete(cp, messages=[*msgs, {"role": "assistant", "content": "已出片"}])
    return cp


async def part4_cross_run() -> None:
    print("\n[4] rerun_from(run_id=…)：跨轮回溯到本会话更早的一次执行")
    st = build_storage("memory")
    mgr = CheckpointManager(st)
    slot = ContractSlot()
    slot.fill(CONTRACT)
    registry = ToolRegistry()
    register_run_tools(registry, slot)
    tool = registry.get("rerun_from")
    sid = "u5:c5"
    ask = "背景音乐改用《秋日小曲》，query 填秋日小曲，然后重做时间线与出片"

    await _seed_run(mgr, st, sid, "run-old", CHAIN, "第一版：配乐用夏日小曲")
    cur = await _seed_run(mgr, st, sid, "run-cur", UPSTREAM, "这一条只想改文案", finish=False)
    ctx = AgentHookContext(
        session=Session(user_id="u5", conversation_id="c5"),
        messages=list(cur.messages), iteration=len(UPSTREAM),
        extras={"checkpoint_manager": mgr, "checkpoint": cur, "run_id": cur.run_id})

    async def call(**kw) -> str:
        tok = _current_hook_ctx.set(ctx)
        try:
            return await tool.execute(**kw)
        finally:
            _current_hook_ctx.reset(tok)

    check("run_id" in tool.parameters["properties"]
          and "run_id" not in tool.parameters["required"]
          and "run_id" in tool.description,
          "run_id 进得了 schema 与描述，且仍是可选项（默认回溯本轮）")

    # 本轮没走过那一步：不能只回一句「没有记录」，得把「哪几条历史执行走过」交出来，
    # 否则模型手上没有 run_id 可填，跨轮回溯这条入口等于不可达。
    try:
        await call(node="select_BGM")
        check(False, "本轮链上没有 select_BGM 时应回绝")
    except ToolError as e:
        check("run-old" in str(e) and "跨轮回溯" in str(e),
              f"回绝文本把历史执行当路标交出来：{str(e)[:88]}")

    # 前缀解析：唯一命中才敢分叉，命中多条就让人给更长前缀（不许挑一个猜）
    await _seed_run(mgr, st, sid, "run-alpha", CHAIN, "第三版")
    await _seed_run(mgr, st, sid, "run-amber", CHAIN, "第四版")
    try:
        await call(node="select_BGM", run_id="run-a")
        check(False, "前缀命中两条时应回绝")
    except ToolError as e:
        check("命中 2 条" in str(e) and "run-alpha" in str(e) and "run-amber" in str(e),
              f"歧义前缀回绝并列出可区分的前缀：{str(e)[:80]}")

    await _seed_run(mgr, st, "u6:c6", "run-other", CHAIN, "别人的会话")
    try:
        await call(node="select_BGM", run_id="run-other")
        check(False, "别的会话的 run 不该被回溯到")
    except ToolError as e:
        check("本会话没有以「run-other」开头" in str(e) and "run-old" in str(e),
              f"跨会话回绝（与不存在同一条口径），并附上本会话最近的执行：{str(e)[:80]}")

    body = json.loads(await call(node="select_BGM", run_id="run-o", instruction=ask,
                                 reason="换一首试试"))
    check(body["cross_run"] is True and body["forked_from"] == "run-old",
          f"跨轮回溯命中的是老那条：{body['forked_from']} / cross_run={body['cross_run']}")
    check(body["run_id"] not in ("run-old", "run-cur") and body["artifact_id"],
          f"分出新 run 与新产物集：{body['run_id'][:8]} / {body['artifact_id']}")
    check(ctx.extras.get("handover") is not None
          and ctx.extras["handover"].run_id == body["run_id"],
          "handover 已挂上，本轮执行由循环接手到那条子 run")

    kid = await mgr.load(body["run_id"])
    check(tools_of(kid.messages) == UPSTREAM,
          f"带走的是老执行分叉点之前的结果：{tools_of(kid.messages)}")
    check(kid.message == ask and kid.messages[-1].get("role") == "user"
          and kid.messages[-1].get("content") == ask,
          "跨轮回溯同样把新诉求接到回退点之后（否则模型照老那句再演一遍）")
    got = await st.artifacts(sid, body["artifact_id"]).executed()
    check(set(UPSTREAM) <= set(got) and not set(RERUN) & set(got),
          f"上游产物整份复用、重跑点及其下游作废：{sorted(got)}")

    old = await st.db.get_by_pk("checkpoints", {"run_id": "run-old"})
    check(old["status"] == STATUS_SUPERSEDED, "被回溯的老 run 让位")
    crow = await st.db.get_by_pk("checkpoints", {"run_id": "run-cur"})
    check(crow["status"] == STATUS_SUPERSEDED,
          "本轮原来那条也让位：跨轮回溯弃用了它，留在 running 会被崩溃恢复当在途分支重播")
    pick = await mgr.pending_for_session(sid)
    check(pick is not None and pick.run_id == body["run_id"],
          f"「继续」挑中跨轮分出的子 run：{pick and pick.run_id}")
    old_arts = st.artifacts(sid, "")
    check(await old_arts.has("select_BGM") and await old_arts.has("render_video"),
          "老那一版的产物集原样保留（跨轮回溯没把它改花）")


async def main() -> None:
    await part1_chain()
    await part2_fork()
    await part3_rerun_tool()
    await part4_cross_run()
    print(f"\n{_checks - _fails}/{_checks} 通过" + ("" if _fails == 0 else f"，{_fails} 失败"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
