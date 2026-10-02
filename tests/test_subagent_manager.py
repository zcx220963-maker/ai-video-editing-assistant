"""SubAgent Manager 验证（不联网）：权限收敛、PG 状态与租约、WORK/IDLE 双唤醒、异常/超时转 shutdown。

对应设计文档「SubAgent Manager」四个关键设计，状态落在 PG ``subagents`` 表
（spec §3.10，主键 ``(scope, name)``，scope = ``{user}:{conv}``）：
prompt 与工作状态跨实例可读回，working 行带租约，进程被 kill 后由 ``reset_expired`` 归位。

运行：  python tests/test_subagent_manager.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun
from agent_framework.identity import use_identity
from agent_framework.llm import ScriptedLLM
from agent_framework.message_center import MessageCenter
from agent_framework.session import Session
from agent_framework.storage import build_storage
from agent_framework.subagent_manager import (
    IDLE,
    SHUTDOWN,
    WORKING,
    ManagedSubAgent,
    SubAgentManager,
    build_subagent_registry,
)
from agent_framework.task_manager import TaskManager
from agent_framework.tool import Tool, ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def _noop_sleep(_seconds: float) -> None:
    return None


class _Echo(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"echo tool {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> str:  # noqa: D401
        return self._name


class _BoomLLM:
    """complete 直接抛异常，用于验证 WORK 阶段 LLM 失败 → shutdown。"""

    async def complete(self, messages, tools=None):  # noqa: ARG002
        raise RuntimeError("LLM down")


def _mk_agent(steps):
    return AgentOnceRun(ScriptedLLM(steps), ToolRegistry(), config=AgentConfig(max_iterations=3))


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = build_storage("memory")
        await storage.start()
        mgr = SubAgentManager(storage, user_id="u1", conversation_id="c1")

        # ---- 权限收敛：子 Agent 工具集 = 父集白名单子集 ----
        parent = ToolRegistry()
        for nm in ("read_inbox", "send_message", "split_shots", "spawn_subagent"):
            parent.register(_Echo(nm))
        sub = build_subagent_registry(parent, allow=["read_inbox", "split_shots"])
        check(set(sub.tool_names) == {"read_inbox", "split_shots"}, "子集只保留白名单工具")
        check("spawn_subagent" not in sub.tool_names, "spawn 未进子集：权限收敛、杜绝再派生")

        # ---- 状态入表：register 写 subagents，跨实例可读回 ----
        await mgr.register("sub_a", "负责粗剪", status=IDLE)
        rows = await storage.db.select("subagents", order_by=["name"])
        check(len(rows) == 1 and rows[0]["scope"] == "u1:c1" and rows[0]["prompt"] == "负责粗剪",
              f"子 Agent 登记进 subagents 表并带作用域/prompt：{rows}")
        check(rows[0]["status"] == IDLE and rows[0]["lease_expires_at"] is None,
              "idle 行无租约（只有 working 才需要证明自己还活着）")
        reloaded = SubAgentManager(storage, user_id="u1", conversation_id="c1")
        check(await reloaded.get_status("sub_a") == IDLE, "重启后可读回 prompt/状态")
        check({"sub_a"} <= {r["name"] for r in await reloaded.list_agents()},
              "list_agents 含登记的成员")

        # ---- 租约对账：working 行过期后归位 idle（原来谎报到永远）----
        await mgr.set_status("sub_a", WORKING)
        r = (await storage.db.select("subagents", where={"name": "sub_a"}, limit=1))[0]
        check(r["lease_expires_at"] is not None, "working 行带租约到期时间")
        check(await mgr.reset_expired() == 0, "租约未到期时不动它")
        expired = SubAgentManager(storage, user_id="u1", conversation_id="c1", lease_sec=-1)
        await expired.set_status("sub_a", WORKING)
        check(await mgr.reset_expired() == 1, "租约到期的 working 被 reset_expired 归位")
        check(await mgr.get_status("sub_a") == IDLE, "对账后状态是 idle")
        check((await storage.db.select("subagents", where={"name": "sub_a"}, limit=1))[0]["prompt"]
              == "负责粗剪", "改状态只改状态列，登记的 prompt 不被覆写")

        # ---- 作用域隔离：别的会话看不到本会话的队友 ----
        other = SubAgentManager(storage, user_id="u2", conversation_id="c9")
        check(await other.list_agents() == [], "不同 用户:会话 各一组队友")
        with use_identity("u3", "c3"):
            check(await mgr.list_agents() == [], "run 内取的是当前身份那组，不是实例缺省")

        # ---- 双唤醒生命周期 ----
        mc = MessageCenter(storage, user_id="u1", conversation_id="c1")
        tm = TaskManager(storage, user_id="u1", conversation_id="c1")
        head = await tm.create("链首任务：整理素材")  # pending、无依赖 → 可认领（被动唤醒源）

        agent = _mk_agent([("answer", "粗剪完成"), ("answer", "配音完成"), ("answer", "导出完成")])
        # 先塞一条主动消息：第一轮 WORK 后 idle 会读到它 → 唤醒第二轮。
        await mc.send("main_agent", "sub_b", "请处理任务 id=1")
        sub_b = ManagedSubAgent(
            "sub_b", agent, mgr,
            message_center=mc, task_manager=tm,
            session=Session(user_id="u", conversation_id="team"),
            loop_times=2, poll_interval=0, sleep=_noop_sleep,
        )
        await mgr.register("sub_b", "剪辑工", status=WORKING)
        result = await sub_b.run("开始剪辑")

        check(result["final_status"] == SHUTDOWN, f"轮询耗尽后终局 shutdown：{result['final_status']}")
        # 预期 3 轮 WORK：round1(初始指令)→idle被主动消息唤醒→round2→idle被动认领任务→round3→idle无任务→shutdown
        check(result["worked_rounds"] == 3, f"WORK 轮数符合预期：{result['worked_rounds']}")
        check(result["replies"] == ["粗剪完成", "配音完成", "导出完成"], f"每轮答复：{result['replies']}")
        check(await mgr.get_status("sub_b") == SHUTDOWN, "管理器状态同步为 shutdown")
        # 被动唤醒确实认领了任务：#1 被 sub_b 认领（claimed）。
        check((await tm.get(head["id"]))["owner"] == "sub_b",
              f"被动唤醒自动认领了可领任务：owner={(await tm.get(head['id']))['owner']}")
        # 主动消息被消费（再读为空），但行留在表里可审计。
        check(await mc.read_inbox("sub_b") == [], "唤醒用的收件箱消息已被消费（不再重复投递）")
        consumed = [x for x in await storage.db.select("inbox_messages")
                    if x["agent"] == "sub_b"]
        check(len(consumed) == 1 and consumed[0]["consumed_at"] is not None,
              "已读消息仍在表里（标记已读而非物理删除）")

        # ---- LLM 异常 → 立即 shutdown（WORK 阶段失败）----
        boom = ManagedSubAgent(
            "sub_c", AgentOnceRun(_BoomLLM(), ToolRegistry(), config=AgentConfig(max_iterations=2)),
            mgr, session=Session(user_id="u", conversation_id="team"),
            loop_times=1, poll_interval=0, sleep=_noop_sleep,
        )
        await mgr.register("sub_c", "会挂掉的工", status=WORKING)
        res_c = await boom.run("干活")
        check(res_c["final_status"] == SHUTDOWN and res_c["worked_rounds"] == 1,
              f"LLM 异常首轮即 shutdown：{res_c}")
        check(await mgr.get_status("sub_c") == SHUTDOWN, "异常 shutdown 状态入表")

        # ---- 纯超时：无任何唤醒源 → 一轮 WORK 后 idle 轮询耗尽 → shutdown ----
        mc2 = MessageCenter(storage, user_id="u9", conversation_id="c9")
        tm2 = TaskManager(storage, user_id="u9", conversation_id="c9")  # 空，无可认领任务
        idle_only = ManagedSubAgent(
            "sub_d", _mk_agent([("answer", "完成")]),
            mgr, message_center=mc2, task_manager=tm2,
            session=Session(user_id="u", conversation_id="team"),
            loop_times=2, poll_interval=0, sleep=_noop_sleep,
        )
        res_d = await idle_only.run("干活")
        check(res_d["worked_rounds"] == 1 and res_d["final_status"] == SHUTDOWN,
              f"无唤醒源时一轮即收尾：{res_d['worked_rounds']} 轮 / {res_d['final_status']}")
        check(await mgr.working() == [], "全部成员都不再谎报 working")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
