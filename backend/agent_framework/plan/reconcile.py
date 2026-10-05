"""事后对账与两个钩子：计划 vs 实际调用的偏差、计划卡回投 + 当轮落库。

只观测不拦截：偏差算成执行记录面板的角标数据与一帧 ``plan reconciliation``，
不改任何一次调用。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..connection_manager import OUTBOUND_TOPIC
from ..hooks import AgentHook, AgentHookContext
from ..mq import MessageQueue
from ..plan_replay import record_plan_card
from ..tool import UnknownToolError
from .support import MCP_PREFIX, clean

# 只读的记账查询不算「创作步骤」：渲染未达终态时 hint 明写「请调用 render_status 追」，
# 再把它标成「计划外一步」就是框架自己打自己的脸（真机踩到：角标写计划外 1 步 ·
# 渲染进度查询，用户读到的是「模型偷偷多做了一件事」）。
# ask_user 同一条路：弹窗提问是**交互动作**，不是多做出来的一步创作；
# 执行轮只要问过一个问题，角标就会挂一条「计划外 · 询问用户」，那是噪声不是偏差。
NON_STEP_TOOLS = frozenset({"render_status", "read_node_history", "ask_user"})

def reconcile(plan_steps: Sequence[Mapping[str, Any]],
              actual_calls: Sequence[str]) -> dict[str, list[str]]:
    """⑦ 对账：实际 − 计划 = 计划外步骤；计划 − 实际（跑完仍未调）= 未履行。

    **只观测不拦截**：返回的只是给执行记录面板的两列角标数据（机器名，出口换中文），
    不改任何一次调用。
    """
    planned = [clean(s.get("node")) for s in plan_steps]
    called = [clean(n) for n in actual_calls if clean(n)
              and clean(n).removeprefix(MCP_PREFIX) not in NON_STEP_TOOLS]
    extra = [n for i, n in enumerate(called) if n not in planned and n not in called[:i]]
    return {"extra": extra,
            "unfulfilled": [n for n in planned if n not in called]}

# ---- 计划卡回投 + 当轮落库（与 MediaCardHook 同一条通道）--------------------

class PlanCardHook(AgentHook):
    """把本轮 ``submit_plan`` 通过的候选计划回投成计划卡帧，并交给当轮落库通道。

    两件事都在 ``after_execute_tools`` 做，而不是在工具里做：工具跑在 ``gather`` 起的
    子 Task 里，那里 set 的 contextvar 不会回到主 Task，assistant 行落库时就 drain 不到。
    本钩子与 ``MessagesRepo.append`` 在同一条 await 链上（``_drive`` 内），所以传得过去。

    ``cp_mgr`` 让 ``confirm_plan`` 工具能找到同会话已有的待确认计划卡并重新推帧弹窗。
    """

    def __init__(self, mq: MessageQueue, cp_mgr: Any = None, *,
                 topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._cp_mgr = cp_mgr
        self._topic = topic

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        candidates = context.state.plan_candidates
        if candidates and not context.state.plan_card_pushed:
            # 这一位落在可恢复状态上：挂起→续跑之后不会又推一张一模一样的卡。
            context.state.plan_card_pushed = True
            session_id = context.session.session_id
            run_id = context.extras.get("run_id")
            # 落库那份多带一个 plan_run_id：历史重放时前端要靠它定位确认帧发往哪条 run
            # （实时那帧本身带 run_id，不必重复）。
            warnings = list(context.state.plan_warnings)
            record_plan_card([dict(p, plan_run_id=run_id) for p in candidates], warnings)
            await self._mq.publish(
                self._topic, session_id,
                {"type": "plan", "session_id": session_id,
                 "run_id": run_id,
                 "plans": [dict(p) for p in candidates],
                 "warnings": warnings},
            )
        # confirm_plan 工具被调用时，把同会话已有的待确认计划卡重新推给前端弹窗。
        if context.extras.get("confirm_plan_requested") and self._cp_mgr is not None:
            context.extras["confirm_plan_requested"] = False
            session_id = context.session.session_id
            try:
                pending = await self._cp_mgr.pending_plan_for_session(session_id)
            except Exception:
                pending = None
            if pending is not None:
                await self._mq.publish(
                    self._topic, session_id,
                    {"type": "plan", "session_id": session_id,
                     "run_id": pending["plan_run_id"],
                     "plans": [dict(p) for p in pending["candidates"]],
                     "warnings": []},
                )


class PlanReconcileHook(AgentHook):
    """事后对账：批准计划的步骤 vs 本轮实际调用，偏差落 run B 记录——只观测不拦截。

    三个节点各司其职：``after_tool_call`` 累积实际调用清单（含轮询渲染这类非模型发起的
    调用，它们同样真实发生过；注册表里没有的工具不计——那一步压根没跑）；
    ``after_execute_tools`` 每批工具后算一次当前偏差，
    只在变化时发一帧（前端按 run_id 覆盖角标，不堆重复帧）；``finalize_content`` 是
    唯一同时拿得到**终答原文**（偏离理由）与 qa_parts 的位置，完整结论在那里落库。
    """

    def __init__(self, mq: MessageQueue | None = None, *,
                 topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._topic = topic

    @staticmethod
    def _audit(context: AgentHookContext) -> dict[str, Any] | None:
        plan = context.state.approved_plan
        if not isinstance(plan, Mapping):
            return None                 # 普通轮 / 规划轮：没有批准计划可对账
        diff = reconcile(plan.get("steps") or [], context.state.calls_executed)
        diff["plan_id"] = plan.get("plan_id") or ""
        return diff

    async def after_tool_call(self, context: AgentHookContext, tool_name: str,
                              arguments: dict, result: Any, elapsed: float, *,
                              error: Exception | None = None,
                              call_id: str = "") -> None:
        if isinstance(error, UnknownToolError):
            return          # 调用从未发生：记进去等于把没跑的步当跑过
        context.state.note_executed(tool_name)

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        audit = self._audit(context)
        if audit is None or self._mq is None:
            return
        if audit == context.state.audit_pushed:
            return                      # 这批工具没改变偏差，不再发重复帧
        # 去重位也在可恢复状态上：挂起前推过的那一版偏差，续跑后不会又推一次。
        context.state.audit_pushed = dict(audit)
        session_id = context.session.session_id
        await self._mq.publish(
            self._topic, session_id,
            {"type": "plan reconciliation", "session_id": session_id,
             "run_id": context.extras.get("run_id"), **audit},
        )

    def finalize_content(self, context: AgentHookContext,
                         content: str | None) -> str | None:
        audit = self._audit(context)
        if audit is None:
            return content
        # 偏离理由同屏：终答原文就是对账那一句的交代，抓这一条胜过另起一套机制。
        audit["reason"] = clean(content)[:400]
        cp = context.extras.get("checkpoint")
        if cp is not None:
            cp.plan["audit"] = audit        # 指针行：执行记录面板按 run 取
        context.state.plan_audit = audit
        if isinstance(context.state.qa_parts, list):
            context.state.qa_parts.append({"type": "plan reconciliation", **audit})
        return content
