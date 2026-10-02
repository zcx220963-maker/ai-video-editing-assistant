"""Agent 循环核心：AgentOnceRun（内层 ReAct）与 Agent（外层 Main Loop）。

设计对应：
- Main Loop：``while True`` 常驻，持续消费各 Session 的用户请求。
- AgentOnceRun：单次请求内、带最大迭代保护的 ReAct 循环
  （LLM → 需要工具？→ Registry 执行 → 回填 → 再 LLM …… → 最终结果）。
- 工具并发：被 LLM 选中的工具二次判定 concurrency_safe，只读安全的进 Batch
  用 asyncio.gather 并发，其余按序独占执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

from .ask_gate import NO_POPUP_NOTE, looks_like_asking_user, popup_nudge
from .checkpoint import Checkpoint, CheckpointManager
from .catalog import get_catalog
from .compress import _repair_orphans
from .context import ContextBuilder
from .hooks import AgentHook, AgentHookContext, CompositeHook, _current_hook_ctx, _current_hooks
from .identity import (rebind_artifact_scope, storyline_session_id,
                         use_identity_or_inherit)
from .llm import LLMClient, LLMResponse
from .messages import ToolCall, assistant, system, tool_result, user
from .plan_gate import (SubmitPlanTool, ConfirmPlanTool, claims_plan_card, claims_step_executed,
                        pending_continuation, planning_section, preload_skills,
                        reconcile, render_injections)
from .render_gate import (ADJUST_OPTION, CONFIRM_OPTION, KEEP_FULL_OPTION,
                          RENDER_NODE, TRUNCATE_OPTION, build_render_ask,
                          decision_is_confirm, should_gate_render)
from .session import Session, SessionManager
from .tool import ToolError, ToolRegistry, is_tool_error, plan_batches


# 同一个工具连续失败多少次后，本轮不再真执行（成功一次即清零）。
# 真机规划轮靠「猜键名读产物」连着错六十次烧穿迭代预算，提示词里的「请勿重试」拦不住。
_MAX_CONSECUTIVE_FAILURES = 4

# 单次工具调用硬超时（秒）：MCP tool_timeout=600s 在此之前先触发并给出更具体的错误；
# 这里兜底的是本地工具或 MCP 传输层自身卡死的情况——不给这层就会永远挂住。
_TOOL_CALL_TIMEOUT = 650.0

# 渲染代查期间的心跳间隔（秒）：租约 600s，留出足够余量再续一次，
# 避免长渲染代查跑到一半被别的实例判定为「租约过期」而抢走同一条 run。
_RENDER_BEAT_SEC = 120.0

# 规划轮里假称「计划卡已提交」时的补救：循环先追一句核对、再给它一次真出卡的机会；
# 预算用尽仍在声称，就在终答末尾补上服务端核对到的事实。
_NO_CARD_NUDGE = (
    "服务端核对：本轮**没有** submit_plan 成功记录，界面上不会出现任何计划卡，"
    "用户无从点确认，这一轮就断在这里。上面那句「卡已提交」不成立。二选一："
    "① 现在就调用 submit_plan 把计划交上来（node 只能取白名单里的名字，参数按真实 schema）；"
    "② 确实给不出计划，就如实说明原因，不要再提计划卡已提交。")
_NO_CARD_NOTE = (
    "\n\n（服务端核对：本轮没有生成计划卡，上面提到的「计划卡」不会出现在界面上。"
    "请把诉求说清楚再发起一次，或直接告诉我要哪几步。）")

# 上面那条按**措辞**判「假称已出卡」，真机换一种说法就绕过去了。实测两种变体都判不中：
# 「已为你准备好 3 个候选方案……你在计划卡上选一版确认即可」
# 「已经给你出了 3 个候选方案……你在计划卡上选一版确认即可」
# 结果：规划轮以一句口头方案收尾、服务端零张卡、用户点不到确认 —— 剪辑流程死在这里。
# 所以再加一条**与措辞无关**的结构判据：规划轮要收尾，就必须有 submit_plan 的成功记录，
# 或者明确说清「给不出计划」。否则退回一次，措辞再花也算不过去。
_NO_CARD_STRUCTURAL_NUDGE = (
    "服务端核对：本轮**没有** submit_plan 的成功记录，因此界面上**没有**任何计划卡，"
    "用户无从点确认——不管上面的措辞是「已准备好方案」「已给出候选」还是别的说法，"
    "服务端和界面上都只有这一条事实：零张卡。现在二选一：\n"
    "① 调用 submit_plan 把 1~3 个候选计划真正交上来（node 只能取白名单里的名字，"
    "参数按真实 schema，不要再去查 Store 键名）；\n"
    "② 确实给不出计划（例如素材还没到位、诉求还不明确），就**明确说明**为什么给不出、"
    "以及需要用户补什么，并且不要提「计划卡」二字。\n"
    "只描述方案而不调用 submit_plan，用户拿到的是一句无法点击的话，这一轮就是白跑。")
# 给不出的正路：模型明确说明原因时不该被反复打回。命中这些词就认它是在如实回绝。
_DECLINES_PLAN = re.compile(
    r"(给不出|无法(给出|生成|提交)?(计划|方案)|不能(给出|生成|提交)(计划|方案)"
    r"|需要你(先)?(提供|补充|确认|明确|告诉)|请(先)?(提供|上传|补充|明确)"
    r"|素材(还)?没|还没有素材|没有素材|无法完成|做不到)")

# 「卡面形状」的答复：列了多个带标号的可选方案，并让用户挑一个确认。
# 这是真机那种「口头交了计划卡」的形态——模型把卡的内容全说出来、却一次 submit_plan
# 都没调，界面上零张卡，用户无从点确认。措辞千变万化（「候选方案」/「候选计划」/
# 「版本」…），所以判据取**结构**：≥2 个方案标号 + 让用户挑选/确认的措辞。
_PLAN_OPTION_LABEL = re.compile(r"(方案\s*[一二三四1-9]|[pP][1-9]\b|版本\s*[一二三四1-9])")
_PLAN_ASK_TO_PICK = re.compile(r"(选一(个|版)|选哪(个|一)|确认哪|挑一(个|版)|选择一(个|版)"
                               r"|确认即可|点确认|告诉我(你)?选|你(倾向|想要)哪)")


def looks_like_plan_card(text: Any) -> bool:
    """这句答复是不是「把计划卡的内容用文字说了一遍」而没有真出卡。

    与 ``claims_plan_card`` 的分工：那个认「已提交/已生成」这类**完成措辞**，
    容易被换词绕开；这个认**卡面形状**（多个带标号的方案 + 让用户挑一个）。

    为什么不能放宽到「规划轮没出卡就不许收尾」：规划轮的出口本来就允许是
    **直接回答**（用户只是问「这条素材能用吗」「需要我出计划卡吗」）。那种轮次里
    模型一次 submit_plan 都不调是完全正确的，硬打回只会把正常咨询也变成多跑一轮。
    所以只针对「看起来已经把卡说完了」的答复。
    """
    body = "" if text is None else str(text)
    if len(_PLAN_OPTION_LABEL.findall(body)) < 2:
        return False
    return bool(_PLAN_ASK_TO_PICK.search(body))


# 执行轮的同一副面孔：确认帧落地后本轮**一次计划步骤都没调用**（Storyline 没收到任何
# 请求，界面上不会多出新的产物），终答却写着「时间线重排完成，时长 8.64 秒」——那是会话
# 历史里上一轮的旧产物被复述成本轮产出（真机实测：2 秒收尾、iteration 0、零工具调用）。
_STEP_NUDGE = (
    "服务端核对：本轮到目前为止**一次计划步骤都没有调用**（批准计划的第 1 步是 "
    "`{first}`），Storyline 没有收到任何请求，界面上不会多出新的产物。上面写的「已完成」"
    "是会话历史里的旧产物，不是这次的结果。二选一："
    "① 现在就调用计划里的步骤工具（参数按节点 schema，用户在卡上选定的值照传）；"
    "② 确实跑不了，就如实说明为什么（例如要先问用户一个问题），不要把旧产物当本轮产出。")
_STEP_NOTE = (
    "\n\n（服务端核对：本轮没有调用任何计划步骤，Storyline 未收到请求，界面上不会多出新的"
    "产物；上面提到的「已完成」是历史轮次的旧结果。要真重排或出片，再发起一次执行即可。）")

# 上面这几段提示文本对外暴露一份名字，供启动期一致性检查读取（run_server 的 (f) 段）：
# 它们都会整段进模型上下文，写错工具名同样会让模型去调一个不存在的工具。
#
# ``_refresh_prompt_texts`` 会把它们换成磁盘上的版本（prompts/*.md）——默认走仓库自带
# prompts/ 目录，改文案不必再动代码。内联的这几段是**回落值**：文件缺失时行为不变。
def _refresh_prompt_texts(library: Any) -> None:
    """按提示词库刷新本模块的提示文本（就地改模块全局）。"""
    if library is None:
        return
    global NO_CARD_NUDGE_TEXT, NO_CARD_NOTE_TEXT, NO_CARD_STRUCTURAL_TEXT
    global STEP_NUDGE_TEXT, STEP_NOTE_TEXT
    NO_CARD_NUDGE_TEXT = library.text("no_card_nudge.md", NO_CARD_NUDGE_TEXT)
    NO_CARD_NOTE_TEXT = library.text("no_card_note.md", NO_CARD_NOTE_TEXT)
    NO_CARD_STRUCTURAL_TEXT = library.text("no_card_structural_nudge.md",
                                           NO_CARD_STRUCTURAL_TEXT)
    STEP_NUDGE_TEXT = library.text("step_nudge.md", STEP_NUDGE_TEXT)
    STEP_NOTE_TEXT = library.text("step_note.md", STEP_NOTE_TEXT)


NO_CARD_NUDGE_TEXT = _NO_CARD_NUDGE
NO_CARD_NOTE_TEXT = _NO_CARD_NOTE
NO_CARD_STRUCTURAL_TEXT = _NO_CARD_STRUCTURAL_NUDGE
STEP_NUDGE_TEXT = _STEP_NUDGE
STEP_NOTE_TEXT = _STEP_NOTE

# HITL 审批断点：执行撞上「需人工审批」的工具时挂起，本轮先结束、不发终答（status 停
# awaiting_approval）；用户批准/拒绝后再从同一一致点续跑。这句是给前端看的过渡说明。
_APPROVAL_PAUSE_ANSWER = "已暂停：以下操作需要你确认后才会执行。"
# 用户决策回喂时的口径：批准就照常跑，拒绝要模型换方案或跳过（别原样重试同一工具）。
_APPROVAL_DECISION_TEXT = {
    "approve": "用户已批准执行，继续跑下去。",
    "reject": "用户拒绝了这一步，请换方案或跳过，不要再原样重试同一工具。",
}
# 渲染前确认门的决策（见 render_gate）。这四个 key 必须与 render_gate 里的常量一致，
# 所以直接 import 过来拼装，不写重复字面量——写重复了会静默失配（用户点了没反应）。
_APPROVAL_DECISION_TEXT.update({
    CONFIRM_OPTION: "用户已确认编排结果，可以开始渲染。",
    ADJUST_OPTION: ("用户选择「我还要改」，尚未确认渲染。**不要渲染**，"
                    "先按用户的要求重做编排，做完把新的编排结果再交用户确认。"),
    KEEP_FULL_OPTION: ("用户要求保内容完整、不要把话截断。请把成片时长放宽到能容纳全部内容，"
                       "重做时间线让画面补足，再把新的编排结果交用户确认。"),
    TRUNCATE_OPTION: ("用户接受按原定时长截断末尾。按原时长重做/确认时间线即可；"
                      "末尾少一句话这件事用户已拍板，不必再问。"),
})


def _detect_fallback_options(messages: list, tool_calls: list) -> dict | None:
    """检查最近一批工具结果里有没有 __fallback_options__ 标记。"""
    import json
    tc_ids = {tc.id for tc in tool_calls}
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if msg.get("tool_call_id") not in tc_ids:
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or "__fallback_options__" not in content:
            continue
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("__fallback_options__"):
                return data
        except Exception:  # noqa: BLE001
            pass
    return None


def _no_step_called(plan: Any, calls: Sequence[str]) -> bool:
    """本轮是否一次计划步骤都没调用（与对账钩子同一套判据：计划 − 实际 = 全部未履行）。"""
    steps = (plan or {}).get("steps") or []
    if not steps:
        return False
    return len(reconcile(steps, calls)["unfulfilled"]) == len(steps)


def declines_plan(text: Any) -> bool:
    """这句答复是否在**如实说明给不出计划**（而不是换个说法假称已出卡）。

    结构性守卫（``_NO_CARD_STRUCTURAL_NUDGE``）要把「没出卡」的轮次退回去，
    但必须放过真正的回绝：素材没到位、诉求还不明确时，模型说清楚原因是对的结局，
    反复打回只会烧完迭代预算、还让用户看不到那句该看到的追问。
    """
    return bool(_DECLINES_PLAN.search("" if text is None else str(text)))


@dataclass
class AgentConfig:
    max_iterations: int = 10
    # 「渲染未达终态不许收尾」的预算：模型收到 queued/running 后不续查时，本循环自己
    # 按 render_poll_sec 轮询 render_status，最多再等 render_follow_max_sec 秒。
    render_follow_max_sec: float = 1800.0
    render_poll_sec: float = 3.0
    # 规划轮声称已出卡、实际没有 submit_plan 成功记录时，循环把这句核对退回去的次数。
    plan_claim_nudges: int = 1
    # 执行轮声称某步已跑完、实际本轮一次计划步骤都没调用时，同样的退回次数。
    step_claim_nudges: int = 1
    # 渲染前确认门：编排就绪、即将渲染时先把编排结果给用户看，确认后才渲。
    # 用户明确要求「不确认绝不渲染」，所以默认为 True；置 False 即回到直接渲染的老行为。
    gate_render: bool = True
    # 「向用户提问必须走弹窗」的硬保证：模型准备收尾时若检测到它在向用户提问、
    # 却没调 ask_user，就打回一次要求它改成弹窗（见 ask_gate）。
    # 置 False 即关闭这道保证（回到「模型自觉决定要不要弹窗」的老行为）。
    require_popup_questions: bool = True
    popup_question_nudges: int = 1


@dataclass
class _Request:
    session: Session
    message: str
    attachments: list[str] = field(default_factory=list)
    run_id: str | None = None


class AgentOnceRun:
    """处理单个用户请求的内层 ReAct 循环。"""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        context_builder: ContextBuilder | None = None,
        hooks: AgentHook | None = None,
        config: AgentConfig | None = None,
        checkpoint: CheckpointManager | None = None,
        storage: Any | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.context_builder = context_builder or ContextBuilder()
        self.hooks = hooks or CompositeHook([])
        self.config = config or AgentConfig()
        self.checkpoint = checkpoint
        # storage：附件鉴权与 messages 落库的入口；未注入时附件一律视为不可用（D1：无本地兜底）。
        self.storage = storage
        # 已经过用户「确认渲染」的 run：同一条 run 内不再重复拦渲染。
        # 放内存即可——它只影响「同一进程、同一条 run 内要不要再弹一次」，
        # 跨重启后重弹一次无害（用户顶多再看一眼同一份编排），不值得为它加一列。
        self._render_confirmed: set[str] = set()

    async def run(
        self, session: Session, message: str, *, run_id: str | None = None,
        stream: bool = False, attachments: Sequence[str] = (),
        registry: ToolRegistry | None = None,
        extra_sections: Sequence[str] = (),
        approved_plan: dict[str, Any] | None = None,
        plan_run_id: str | None = None,
        planning: bool = False,
    ) -> str:
        """正常发起一次执行；配置了 checkpoint 时先落一个初始快照。

        计划门把这一入口分出三种轮次，差别只在这几个参数上，循环本身不感知：
        ``registry`` 换成规划轮的过滤注册表（剪辑节点物理不在里面）、
        ``extra_sections`` 带进 ``<planning_round>`` 或 ``<approved_plan>`` 等本轮专属段、
        ``approved_plan`` 让对账钩子有本、``plan_run_id`` 记执行轮从哪次规划来、
        ``planning`` 让循环认领规划轮的那条硬保证（没出卡就不许声称已出卡）。
        """
        ids = [str(m) for m in attachments if str(m).strip()]
        rows, rejected = await self._resolve_attachments(session, ids)
        messages = await self.context_builder.build(
            session, message, attachments=rows, rejected_attachments=rejected,
            extra_sections=extra_sections,
        )
        # 附件随 user 行入库（spec §5 步骤 7）：先落库再进循环，崩溃恢复时这轮信息不丢。
        await self._persist_user_message(session, message, ids)
        cp: Checkpoint | None = None
        if self.checkpoint is not None:
            cp = await self.checkpoint.begin(
                session.session_id, message, messages, run_id,
                scope={"storyline_session": storyline_session_id(
                    session.user_id, session.conversation_id), "artifact_id": ""},
                plan_run_id=plan_run_id,
            )
        # 身份对整条 await 链生效：素材按 owner 过滤要靠它，不能让模型自报
        with use_identity_or_inherit(session.user_id, session.conversation_id):
            return await self._drive(
                session, message, messages, 0, cp, stream, resuming=False, run_id=run_id,
                registry=registry, approved_plan=approved_plan, planning=planning,
            )

    async def _resolve_attachments(
        self, session: Session, ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """附件 id → (可见素材行, 无法使用的 id)；越权 id 不报错，交给上下文如实说明。"""
        if not ids:
            return [], []
        if self.storage is None:
            return [], ids
        return await self.storage.materials.resolve(
            ids, user_id=session.user_id, conv_id=session.conversation_id or None
        )

    async def _persist_timeline(self, ctx: AgentHookContext, timeline: Any) -> None:
        """LLM 修正后的时间线存回 plan_timeline artifact，让后续轮次能读到修正版本。

        执行轮里 LLM 经常人工修正时间线（加采访画面穿插、补字幕等），但修正只存在
        checkpoint 的 LLM 对话记录里，没存回 artifact 表。新轮的 Agent 用
        read_node_history 读 plan_timeline 时读到的是自动生成的原始版本，于是误判
        「没有采访画面」。这里在工具调用成功后把入参里的 timeline 存回 artifact。
        """
        if self.storage is None or not timeline:
            return
        try:
            sid = storyline_session_id(
                ctx.session.user_id, ctx.session.conversation_id)
            repo = self.storage.artifacts(sid)
            await repo.put("plan_timeline", {"timeline": timeline})
        except Exception:
            pass

    async def _persist_user_message(
        self, session: Session, message: str, ids: list[str]
    ) -> None:
        if self.storage is None or not session.user_id or not session.conversation_id:
            return
        await self.storage.conversations.ensure(session.user_id, session.conversation_id)
        await self.storage.messages.append(
            session.user_id, session.conversation_id, "user", content=message,
            attachments=ids)

    async def resume(self, cp: Checkpoint, session: Session, *, stream: bool = False) -> str:
        """续跑一个快照：只重新发起 LLM 调用，不重放已提交工具。

        快照从哪来不在这里管——崩溃恢复取最新一致点，分叉重跑取历史某一界且已带着
        新的产物作用域，两者到这儿都是「一个 Checkpoint」。

        待批的 tool_calls 要作为 keep 传进 ``_repair_orphans``：resume 也可能落到一个
        「等确认」的快照上，那时它们同样没有 tool 结果（详见 ``approve`` 的说明）。
        """
        pending_ids = [str(c.get("id") or "")
                       for c in ((cp.approval or {}).get("pending_calls") or [])]
        with use_identity_or_inherit(session.user_id, session.conversation_id,
                                     cp.artifact_id):
            return await self._drive(
                session, cp.message,
                _repair_orphans(list(cp.messages), keep_ids=pending_ids),
                cp.iteration, cp, stream,
                resuming=True, run_id=cp.run_id,
            )

    async def approve(
        self, cp: Checkpoint, session: Session, *, decision: str, stream: bool = False,
        note: str = "",
    ) -> str:
        """从审批断点续跑：按用户决策把挂起的那批工具收口，再继续正常循环。

        ``decision`` 是 "approve"/"reject"、兜底方案的 key、或弹窗选项的 key。
        ``note`` 是用户在弹窗里选/写的原话（多题汇总或自定义文本），一并带进上下文。

        注意这里**不能**用 ``_repair_orphans`` 裸修：挂起点上待批的 tool_calls 本来
        就没有 tool 结果，会被当成孤儿剥掉，续跑时 ``_settle_pending`` 再补一条 tool
        回执就成了孤儿 tool 消息，LLM API 直接 400。所以把待批 id 作为 keep 传进去。
        """
        pending_ids = [str(c.get("id") or "")
                       for c in ((cp.approval or {}).get("pending_calls") or [])]
        with use_identity_or_inherit(session.user_id, session.conversation_id,
                                     cp.artifact_id):
            return await self._drive(
                session, cp.message,
                _repair_orphans(list(cp.messages), keep_ids=pending_ids),
                cp.iteration, cp, stream,
                resuming=True, run_id=cp.run_id, approval_decision=decision,
                approval_note=note,
            )

    async def _drive(
        self,
        session: Session,
        message: str,
        messages: list,
        start_iteration: int,
        cp: Checkpoint | None,
        stream: bool,
        resuming: bool,
        run_id: str | None = None,
        registry: ToolRegistry | None = None,
        approved_plan: dict[str, Any] | None = None,
        planning: bool = False,
        approval_decision: str | None = None,
        approval_note: str = "",
    ) -> str:
        reg = registry if registry is not None else self.registry
        ctx = AgentHookContext(session=session, messages=messages)
        # Hook（如 OutboundStreamHook）按 run_id 标注这段流属于哪次执行，供回投/前端聚合。
        ctx.extras["run_id"] = run_id
        # 分叉工具（rerun_from）要能读到「当前这个 run 走到哪了、快照管理器是谁」，
        # 才能就地开一条新 run 并把本轮接过去。没有 checkpoint 时这两项为空，工具会如实回绝。
        ctx.extras["checkpoint"] = cp
        ctx.extras["checkpoint_manager"] = self.checkpoint
        # 执行轮带着批准计划进来：对账钩子据此算「计划外 / 未履行」，只观测不拦截。
        if approved_plan is not None:
            ctx.extras["approved_plan"] = approved_plan
        # 暴露当前 ctx 与 hooks 给工具（如 SpawnTool）触发子 Agent 生命周期节点。
        hooks_token = _current_hooks.set(self.hooks)
        ctx_token = _current_hook_ctx.set(ctx)

        final: str | None = None
        iteration = start_iteration
        reached_limit = True
        card_nudges = max(0, self.config.plan_claim_nudges)
        step_nudges = max(0, self.config.step_claim_nudges)
        # 「向用户提问必须走弹窗」的打回预算（见 ask_gate）。默认 1 次。
        popup_nudges = max(0, self.config.popup_question_nudges)
        # 本轮真发生过的调用名（含失败的那几次）：执行轮的「一次步骤都没调用」判据用它算，
        # 不借对账钩子的 tool_calls_seen——那道保证不该取决于装配时少没少挂一个钩子。
        calls_this_run: list[str] = []
        # 本轮**是否已经用过 ask_user**：必须按「整轮」而不是「当前这一批」算。
        # 反例（真机实测）：模型调 ask_user 提问 → 循环挂起并把「已暂停等你选择」写成终答 →
        # 那句解释里带着「本轮不可用，你希望怎么处理」的措辞，会被判据认成「在提问却没调
        # ask_user」，于是又逼它问一遍，用户看到两张一模一样的卡。
        asked_this_run = False
        qa_parts: list[dict] = []  # 本轮 think / tool call 片段（QA .jsonl 历史用）
        # 首条记 prompt 指纹：eval 回归据此把结果归到具体版本的 prompt。
        qa_parts.append({"type": "prompt_fingerprint", "fingerprint": self.context_builder.fingerprint()})
        # 收尾阶段的对账钩子要往这里追加一条 plan reconciliation（在 assistant 行落库之前）。
        ctx.extras["qa_parts"] = qa_parts
        try:
            try:
                while iteration < self.config.max_iterations:
                    # HITL：从审批断点续跑——先把挂起的那批 tool_calls 按用户决策收口。
                    if approval_decision is not None:
                        await self._settle_pending(ctx, cp, reg, approval_decision,
                                                   approval_note)
                        approval_decision = None
                        approval_note = ""
                        iteration += 1
                        continue
                    # 一致点：即将调用 LLM 之前落盘，恢复从这里续跑即可避免重放工具。
                    if cp is not None:
                        await self.checkpoint.save_progress(cp, iteration=iteration, messages=messages)
                    ctx.iteration = iteration
                    await self.hooks.before_iteration(ctx)

                    resp = await self._invoke(ctx, messages, reg.get_definitions(), stream, resuming)

                    if resp.wants_tools:
                        if resp.content:
                            qa_parts.append({"type": "think", "content": resp.content})
                        for tc in resp.tool_calls:
                            qa_parts.append({
                                "type": "tool call",
                                "name": tc.name,
                                "arguments": tc.arguments,
                            })
                            calls_this_run.append(tc.name)
                        messages.append(assistant(resp.content, resp.tool_calls))
                        if cp is not None:
                            # 确认过渲染之后，模型若又发起一次渲染：如实告诉它「已经在渲了」，
                            # 不重复提交。真机里模型收尾时不会这么干，但一次误判就是白烧几分钟
                            # 算力，而且两次渲染写同一个 artifact 作用域，代价不对称。
                            repeat = [tc for tc in resp.tool_calls
                                      if tc.name == RENDER_NODE
                                      and run_id in self._render_confirmed]
                            if repeat:
                                for tc in resp.tool_calls:
                                    if tc.name == RENDER_NODE:
                                        messages.append(tool_result(
                                            tc.id, tc.name,
                                            "本次编排已经确认并提交渲染，无需再次提交。"
                                            "请直接向用户汇报渲染已开始。"))
                                    else:
                                        messages.append(tool_result(
                                            tc.id, tc.name,
                                            "本次渲染已提交，这一条随渲染一并跳过。"))
                                await self.hooks.after_execute_tools(ctx)
                                iteration += 1
                                continue
                            gated = [tc for tc in resp.tool_calls if reg.needs_approval(tc.name)]
                            if gated:
                                # 撞上需人工审批的工具：整批挂起（挂起时 assistant(tool_calls)
                                # 已入 messages、工具结果未回填；批准后从同一候选点续跑）。
                                pending = [{"id": tc.id, "name": tc.name,
                                            "arguments": tc.arguments} for tc in resp.tool_calls]
                                await self.checkpoint.await_approval(
                                    cp, iteration=iteration, messages=messages,
                                    pending_calls=pending,
                                    reason="以下操作需要你确认后才会执行")
                                await self.hooks.on_approval_required(
                                    ctx, pending, "以下操作需要你确认后才会执行")
                                ctx.extras["approval_paused"] = True
                                return _APPROVAL_PAUSE_ANSWER
                            # 渲染前确认门：编排已就绪、马上要渲染时，先把编排结果给用户看，
                            # 等确认再渲。用户明确要求「不确认绝不渲染」。
                            # 确认过一次就整条 run 不再拦：同一个 run 里再拦一次只是重复打扰
                            # （模型收尾时偶尔会再调一次渲染）。
                            if (should_gate_render(resp.tool_calls,
                                                   enabled=self.config.gate_render)
                                    and run_id not in self._render_confirmed):
                                preview = await self._load_preview(session)
                                ask = build_render_ask(preview)
                                pending = [{"id": tc.id, "name": tc.name,
                                            "arguments": tc.arguments}
                                           for tc in resp.tool_calls]
                                logger.info(
                                    "渲染确认门：挂起 iteration=%s，待批 %s，"
                                    "messages 末条 role=%s 有 tool_calls=%s，ask.preview=%s",
                                    iteration, [c["name"] for c in pending],
                                    (messages[-1] or {}).get("role") if messages else None,
                                    bool((messages[-1] or {}).get("tool_calls")) if messages else None,
                                    bool(ask.get("preview")))
                                await self.checkpoint.await_approval(
                                    cp, iteration=iteration, messages=messages,
                                    pending_calls=pending, reason=ask["title"], ask=ask)
                                await self.hooks.on_approval_required(
                                    ctx, pending, ask["title"], ask=ask)
                                ctx.extras["approval_paused"] = True
                                return _APPROVAL_PAUSE_ANSWER
                        await self._execute_tool_calls(ctx, resp.tool_calls, reg)
                        # 主动提问：模型调了 ask_user 就把本轮停在这里，等问题卡片
                        # 上用户点选之后从同一断点续跑（与审批共用挂起/续跑机制）。
                        # 为什么在这里拦而不是在工具里抛：工具只负责把问题整理成形，
                        # 「停下来」是循环的职责——工具不该自己决定一整个轮次的生命周期。
                        asked = self._take_asked_question(reg, resp.tool_calls)
                        if asked is not None:
                            asked_this_run = True
                        if asked is not None and cp is not None:
                            pending = [{"id": tc.id, "name": tc.name,
                                        "arguments": tc.arguments}
                                       for tc in resp.tool_calls]
                            reason = asked["title"]
                            await self.checkpoint.await_approval(
                                cp, iteration=iteration, messages=messages,
                                pending_calls=pending, reason=reason, ask=asked)
                            await self.hooks.on_approval_required(
                                ctx, pending, reason, ask=asked)
                            ctx.extras["approval_paused"] = True
                            return _APPROVAL_PAUSE_ANSWER
                        fb = _detect_fallback_options(ctx.messages, resp.tool_calls)
                        if fb is not None and cp is not None:
                            pending = [{"id": tc.id, "name": tc.name,
                                        "arguments": tc.arguments} for tc in resp.tool_calls]
                            await self.checkpoint.await_approval(
                                cp, iteration=iteration, messages=messages,
                                pending_calls=pending,
                                reason=fb.get("error", "渲染需要你选择方案"),
                                fallback_options=fb.get("options", []))
                            await self.hooks.on_approval_required(
                                ctx, pending, fb.get("error", "渲染需要你选择方案"),
                                fallback_options=fb.get("options", []))
                            ctx.extras["approval_paused"] = True
                            return _APPROVAL_PAUSE_ANSWER
                        handover = ctx.extras.pop("handover", None)
                        if handover is not None:
                            cp, messages, iteration = await self._adopt_fork(ctx, handover)
                            reg = self.registry   # 交接后是正常执行轮，用回全量注册表
                        else:
                            iteration += 1
                        continue

                    if (planning and not ctx.extras.get("plan_candidates")
                            and claims_plan_card(resp.content) and card_nudges > 0):
                        # 规划轮里「卡已提交」而没有 submit_plan 成功记录＝界面上没有卡：
                        # 用户点不到确认，这一轮是死胡同。先给它一次真出卡或改口的机会。
                        card_nudges -= 1
                        messages.append(assistant(resp.content))
                        messages.append(system(NO_CARD_NUDGE_TEXT))
                        iteration += 1
                        continue

                    if (planning and not ctx.extras.get("plan_candidates")
                            and looks_like_plan_card(resp.content)
                            and not declines_plan(resp.content) and card_nudges > 0):
                        # 上面那条措辞守卫按**文本**判「假称已出卡」，而真机上模型换一种说法
                        # 就绕过去了：实测「已为你准备好 3 个候选方案……你在计划卡上选一版
                        # 确认即可」两种变体都判不中 → 守卫不触发 → 规划轮以一句口头方案收尾、
                        # 服务端零张卡、用户永远点不到确认。这就是「剪辑流程跑不起来」的断点。
                        # 这条按**卡面形状**判（≥2 个方案标号 + 让用户挑），与具体措辞无关；
                        # 纯咨询回答（问素材能不能用、要不要出卡）不具这个形状，不会误伤。
                        card_nudges -= 1
                        messages.append(assistant(resp.content))
                        messages.append(system(NO_CARD_STRUCTURAL_TEXT))
                        iteration += 1
                        continue

                    approved = ctx.extras.get("approved_plan")
                    if (approved and _no_step_called(approved, calls_this_run)
                            and claims_step_executed(resp.content)
                            and step_nudges > 0):
                        # 执行轮里「某步已跑完」而本轮零步骤调用＝Storyline 没收到请求，
                        # 界面上不会多出任何新产物：那句「已完成」是从历史里复述的旧结果。
                        # 先退回去要一次真调用（或一次如实说明），别把旧产物当本轮产出交出去。
                        step_nudges -= 1
                        first = (approved.get("steps") or [{}])[0].get("node") or "?"
                        messages.append(assistant(resp.content))
                        # 用 replace 而不是 str.format：提示词是磁盘文件，里面可能
                        # 出现 JSON 示例（{"plans": …}），format 会拿它当占位符炸掉。
                        messages.append(system(STEP_NUDGE_TEXT.replace(
                            "{first}", str(first))))
                        iteration += 1
                        continue
                    if (self.config.require_popup_questions and popup_nudges > 0
                            and not asked_this_run
                            and looks_like_asking_user(resp.content)):
                        # 硬保证：向用户提问必须走弹窗。判据与守卫都在 ask_gate，
                        # 只打回一次（用尽后如实交付并在末尾附服务端核对）。
                        # 为什么放在最后一道（收尾前）而不是每轮都查：只有「准备收尾」
                        # 才是真的把问题交给用户；中途文字里带个问号只是思考过程。
                        popup_nudges -= 1
                        messages.append(assistant(resp.content))
                        messages.append(system(popup_nudge(planning)))
                        iteration += 1
                        continue
                    final = resp.content
                    reached_limit = False
                    break
            except BaseException:
                # 真实进程被 kill 时此处不执行，但上一致点已落盘，仍可恢复。
                if cp is not None:
                    await self.checkpoint.mark_failed(cp, iteration=iteration, messages=messages)
                raise

            if reached_limit:
                final = f"[已达最大迭代次数 {self.config.max_iterations}，提前结束]"

            final = self.hooks.finalize_content(ctx, final)

            # 「提问必须是弹窗」的打回机会用尽仍在提问：如实交付，但补一句服务端核对到的事实，
            # 让用户知道这句本该是个弹窗（与 NO_CARD_NOTE_TEXT 同一套兜底思路）。
            if (self.config.require_popup_questions and not asked_this_run
                    and looks_like_asking_user(final)):
                final = (final or "") + NO_POPUP_NOTE

            if (planning and not ctx.extras.get("plan_candidates")
                    and (claims_plan_card(final) or looks_like_plan_card(final))):
                # 核对机会用尽还在声称/还在把卡面内容当交付：界面上不会多出卡，这句不能
                # 原样进历史——末尾补一条服务端核到的事实，让用户知道该重新发起。
                # 判据与循环里那条守卫同源：纯咨询回答（问素材能不能用）不具卡面形状，
                # 原样交付，不被这句多余的事实更正污染。
                final = (final or "") + NO_CARD_NOTE_TEXT

            approved = ctx.extras.get("approved_plan")
            if (approved and _no_step_called(approved, calls_this_run)
                    and claims_step_executed(final)):
                # 同一件事在执行轮的版本：核对机会用尽还在声称某步跑完了，就把「本轮零调用、
                # 界面上不会多出任何新产物」这条事实补在答复末尾——留原样进历史，
                # 用户读到的是上一轮的旧时长冒充这一轮的产出。
                final = (final or "") + STEP_NOTE_TEXT

            # 终答进收尾一致点：回看/分叉取这条 run 时，缺最后一句就不是一轮的终态。
            if cp is not None:
                if reached_limit:
                    # 撞迭代上限是**被打断**，不是成功收尾。原先这里照样 complete()，
                    # 于是 checkpoint 落成 completed、_UNFINISHED={running,failed} 把它排除，
                    # /runs/active 查不到、consumer 的自动续跑也捞不回来——用户看到
                    # 「[已达最大迭代次数 N，提前结束]」，刷新后这条 run 再也接不上。
                    # 真机会话里已经有一批这样收尾的记录。标记成 failed 让「继续」能接。
                    await self.checkpoint.mark_failed(
                        cp, iteration=iteration,
                        messages=[*messages, assistant(final or "")])
                else:
                    await self.checkpoint.complete(cp, messages=[*messages, assistant(final or "")])

            # 落回会话历史：流式模式下这也是把攒出的完整答复写进会话的最简单情形。
            qa_parts.append({"type": "answer", "content": final or ""})
            session.add(user(message))
            session.add(assistant(final))
            session.add_qa(message, qa_parts)
            await self._persist_answer_message(session, final or "", qa_parts)
            return final or ""
        finally:
            _current_hooks.reset(hooks_token)
            _current_hook_ctx.reset(ctx_token)

    async def _adopt_fork(self, ctx: AgentHookContext,
                          child: Checkpoint) -> tuple[Checkpoint, list, int]:
        """把本轮执行接到分叉出的新 run 上：上下文回退、作用域换绑、迭代计数归位。

        为什么在这里做而不是在工具里：cp 的所有权属于本循环，工具只能**申请**分叉；
        换 run、换产物作用域、改迭代计数这三件事必须同时发生，否则快照与身份会错位。
        （父 run 的让位由 ``CheckpointManager.fork`` 就地做，这里不必重复置状态。）
        """
        messages = _repair_orphans(list(child.messages))
        rebind_artifact_scope(child.artifact_id)
        ctx.extras["checkpoint"] = child
        ctx.extras["run_id"] = child.run_id
        ctx.messages = messages
        messages.append(system(
            f"（已回到一致点 seq={child.forked_at_seq} 并开新执行 run={child.run_id}："
            f"该 run 之前的步骤结果保持有效，剪辑产物已换到新作用域 "
            f"{child.artifact_id or '（未分叉）'}，其后的步骤结果已作废。"
            f"请按用户最新的诉求从那里继续。）"))
        return child, messages, child.iteration

    async def _persist_answer_message(
        self, session: Session, content: str, qa_parts: list[dict[str, Any]]
    ) -> None:
        """assistant 行与 user 行配对入库；恢复路径同样补齐，所以崩溃那轮不会缺最终答复。"""
        if self.storage is None or not session.user_id or not session.conversation_id:
            return
        # 恢复路径没有第二条 user 消息可顺带建会话，这里补幂等 ensure：
        # 否则 owner 校验抛 IntegrityConflict，答复会静默不进历史。
        await self.storage.conversations.ensure(session.user_id, session.conversation_id)
        await self.storage.messages.append(
            session.user_id, session.conversation_id, "assistant", content=content,
            qa={"parts": qa_parts})

    async def _invoke(
        self,
        ctx: AgentHookContext,
        messages: list,
        tools: list,
        stream: bool,
        resuming: bool,
    ) -> LLMResponse:
        """发起一次 LLM 调用。stream 且后端支持流式时逐块回调 on_stream，并聚合为整条响应。"""
        if stream and hasattr(self.llm, "complete_stream"):
            parts: list[str] = []
            tool_calls: list[ToolCall] | None = None
            usage: dict[str, int] | None = None
            async for chunk in self.llm.complete_stream(messages, tools):
                if chunk.delta:
                    parts.append(chunk.delta)
                    await self.hooks.on_stream(ctx, chunk.delta)
                if chunk.tool_calls is not None:
                    tool_calls = chunk.tool_calls
                if chunk.usage is not None:
                    usage = chunk.usage
            await self.hooks.on_stream_end(ctx, resuming=resuming)
            resp = LLMResponse(content="".join(parts) or None, tool_calls=tool_calls or [],
                               usage=usage)
            if usage is not None:
                await self.hooks.on_llm_usage(ctx, usage)
            return resp

        resp = await self.llm.complete(messages, tools)
        if stream:
            # 后端不支持流式：整段作为一次 delta，保证 hook 语义一致。
            if resp.content:
                await self.hooks.on_stream(ctx, resp.content)
            await self.hooks.on_stream_end(ctx, resuming=resuming)
        if resp.usage is not None:
            await self.hooks.on_llm_usage(ctx, resp.usage)
        return resp

    async def _settle_pending(
        self, ctx: AgentHookContext, cp: Checkpoint | None, reg: ToolRegistry,
        decision: str, note: str = "",
    ) -> None:
        """把挂起的那批 tool_calls 按用户决策收口（批准=执行，拒绝=回喂拒绝结果）。

        恢复路径专用：挂起时 messages 末尾已是 assistant(tool_calls)，这里只补它的
        tool 回执——之后循环自然进入下一轮 LLM，既不重发工具、也不丢这批调用。

        fallback_options 场景：decision 是用户选的方案 key（如 "ffmpeg"），
        注入 render_mode=decision 到工具参数后重新执行。

        ``note`` 是用户在弹窗里选/写的**原话**（多题时是「题 → 选择」汇总，
        自定义时是他打的那句）。渲染确认门与主动提问靠它把用户意图带进上下文；
        原先只转发 decision，用户填的自定义内容会被丢掉——而「选项之外还能说人话」
        正是这套弹窗必须留的一条路，所以必须传到底。
        """
        if cp is None:
            return
        approval = cp.approval or {}
        pending = list(approval.get("pending_calls") or [])
        fallback_options = approval.get("fallback_options") or []
        asked = approval.get("ask")
        cp.approval = {}
        calls = [ToolCall(id=str(c.get("id") or ""), name=str(c.get("name") or ""),
                          arguments=c.get("arguments") or {}) for c in pending]
        # 主动提问 / 渲染确认门的收口方式取决于用户选了什么：
        #
        # · 确认类决策（渲染确认 / 批准）→ **执行**挂起的那批调用。
        #   为什么不能只回喂一句话：模型看到「用户已确认」后会**再调一次**渲染，
        #   于是又被门拦住，弹第二张一模一样的卡——真机实测就这样循环。
        #   确认之后直接执行原批调用，才是「用户点了一下，片子开始渲」。
        # · 其余（要改 / 自定义 / 未识别）→ 回喂用户原话，让模型重做编排，
        #   绝不执行（render_video 就在这批里，跑掉就等于没确认就渲了）。
        if asked is not None and not fallback_options:
            # 只给「还没有回执」的那几条补结果。
            #
            # 为什么必须查重：主动提问那一路（ask_user）在挂起**之前**就已经执行过了，
            # 它的 tool 回执已经在 messages 里。这时再补一条同 tool_call_id 的结果，
            # 就成了「一个 id 两条回执」——LLM API 直接 400，整条 run failed。
            # （渲染确认门那一路是先拦后执行，所以它确实需要补；两种情形在这里统一处理。）
            already = {m.get("tool_call_id") for m in ctx.messages
                       if isinstance(m, dict) and m.get("role") == "tool"}
            remaining = [tc for tc in calls if tc.id not in already]
            if decision_is_confirm(decision) and calls:
                # 记下「这条 run 的渲染已确认」：之后再出现渲染调用不再拦。
                self._render_confirmed.add(str(cp.run_id or ""))
                if remaining:
                    await self._execute_tool_calls(ctx, remaining, reg)
                return
            text = note or decision_text(decision, "")
            if remaining:
                for tc in remaining:
                    ctx.messages.append(tool_result(tc.id, tc.name, text))
                await self.hooks.after_execute_tools(ctx)
            return
        if fallback_options and decision in {o.get("key") for o in fallback_options}:
            calls = [ToolCall(id=tc.id, name=tc.name,
                              arguments={**tc.arguments, "render_mode": decision})
                     for tc in calls]
            await self._execute_tool_calls(ctx, calls, reg)
            return
        if decision == "approve":
            await self._execute_tool_calls(ctx, calls, reg)
            return
        for tc in calls:
            ctx.messages.append(tool_result(
                tc.id, tc.name, ToolError(tc.name, _APPROVAL_DECISION_TEXT["reject"])))
        await self.hooks.after_execute_tools(ctx)

    async def _execute_tool_calls(
        self, ctx: AgentHookContext, tool_calls: list[ToolCall],
        registry: ToolRegistry | None = None,
    ) -> None:
        results = await self._run_tool_round(ctx, tool_calls, registry)
        # 分叉交接（rerun_from）已经把本轮搬到新 run 上，旧作用域的在途渲染不该再由
        # 这条循环追着查——新 run 的上下文里会有它自己的渲染。
        if ctx.extras.get("handover") is None:
            await self._follow_inflight_renders(
                ctx, [(tc.name, results.get(tc.id)) for tc in tool_calls], registry)

    async def _run_tool_round(
        self, ctx: AgentHookContext, tool_calls: list[ToolCall],
        registry: ToolRegistry | None = None,
    ) -> dict[str, Any]:
        """按状态键冲突分批：组内 gather 并发，组间按序，回填仍按 LLM 给的原始顺序。

        分批规则在 ``tool.plan_batches``：两个调用只要写集相交、或一方写集撞上另一方
        读集，就不能同批——这是工具自己声明的契约（``Tool.reads`` / ``Tool.writes``），
        不再靠「这批里出现过被依赖的节点名就整批串行」那种一次性推理。
        """
        reg = registry if registry is not None else self.registry
        fail_counts: dict[str, int] = ctx.extras.setdefault("_fail_counts", {})

        def _refused(tc: ToolCall, times: int) -> ToolError:
            return ToolError(
                tc.name,
                f"Error: 「{tc.name}」已连续失败 {times} 次，这次没有再执行——"
                f"原样重试只会得到同一个错误。换参数或换工具，"
                f"或者直接告诉用户卡在哪、给个方案（规划轮就调用 submit_plan）。")

        async def _exec_one(tc: ToolCall) -> Any:
            await self.hooks.before_tool_call(ctx, tc.name, tc.arguments, call_id=tc.id)
            t0 = time.monotonic()
            if fail_counts.get(tc.name, 0) >= _MAX_CONSECUTIVE_FAILURES:
                # 撞上限还重试：不再打后端。一次规划轮里「猜键名读产物」能连着错六十次，
                # 光靠提示词里的「请勿重试」拦不住（真机实测就是这么烧穿迭代预算的）。
                r = _refused(tc, fail_counts[tc.name])
                await self.hooks.after_tool_call(
                    ctx, tc.name, tc.arguments, None, 0.0, error=r, call_id=tc.id)
                return r
            try:
                r = await asyncio.wait_for(
                    reg.execute(tc.name, tc.arguments), timeout=_TOOL_CALL_TIMEOUT)
            except TimeoutError:
                r = ToolError(
                    tc.name,
                    f"Error: 工具「{tc.name}」执行超时（{_TOOL_CALL_TIMEOUT:.0f}s）——"
                    f"可能是下游服务卡死或 I/O 阻塞。请换参数或换工具，"
                    f"或告诉用户卡在哪、给个方案。")
            except ToolError as e:  # 已经定性过的失败，原样回喂
                r = e
            except Exception as e:  # noqa: BLE001 - Registry 之外再兜一层，循环不该炸
                r = ToolError(tc.name, f"Error executing {tc.name}: {e}")
            err = r if is_tool_error(r) else None
            if err is None and isinstance(tc.arguments, dict) and "timeline" in tc.arguments:
                await self._persist_timeline(ctx, tc.arguments["timeline"])
            await self.hooks.after_tool_call(
                ctx, tc.name, tc.arguments, None if err else r,
                time.monotonic() - t0, error=err, call_id=tc.id
            )
            return r

        results: dict[str, object] = {}
        for batch in plan_batches(tool_calls, reg.get):
            gathered = await asyncio.gather(*(_exec_one(tc) for tc in batch))
            results.update({tc.id: r for tc, r in zip(batch, gathered)})

        # 按 LLM 给出的原始顺序回填，保证上下文一致性。
        # 守卫提示必须排在本批**全部**回执之后：assistant(tool_calls) 与它的逐条
        # tool 回执之间插任何别的角色，OpenAI 兼容口径直接 400（tool_calls 未收口）。
        nagging: list[tuple[str, int]] = []
        for tc in tool_calls:
            r = results[tc.id]
            if is_tool_error(r):
                fail_counts[tc.name] = fail_counts.get(tc.name, 0) + 1
                if fail_counts[tc.name] >= 2 and tc.name not in {n for n, _ in nagging}:
                    nagging.append((tc.name, fail_counts[tc.name]))
            else:
                fail_counts[tc.name] = 0
            ctx.messages.append(tool_result(tc.id, tc.name, r))
        for name, times in nagging:
            ctx.messages.append(system(
                f"⚠️ 工具「{name}」已连续失败 {times} 次。请立即停止重试该工具，"
                f"向用户说明失败原因并询问如何处理（换素材、换参数、跳过这步或换方案），"
                f"不要继续重试。连续失败到 {_MAX_CONSECUTIVE_FAILURES} 次后该工具不再执行。"
            ))

        await self.hooks.after_execute_tools(ctx)
        return results

    async def _load_preview(self, session: Session) -> dict[str, Any] | None:
        """读本次会话已产出的剪辑产物，整理成「编排预览」。

        渲染前确认门用它把编排结果摆给用户看。取不到（没接存储 / 还没产物）返回 None，
        门照样拦——只是弹窗里少一块摘要，**不会因为读不到预览就放行渲染**。

        作用域键必须用 ``storyline_session_id``（``u:{user}:c:{conv}``）：
        剪辑产物存在 Storyline 那一侧，键由那个函数统一拼装。用 ``session.session_id``
        （``{user}:{conv}``）会查不到任何产物，表现是「预览永远为空」而没有任何报错。
        """
        if self.storage is None:
            return None
        try:
            from .preview import build_preview
            sid = storyline_session_id(session.user_id, session.conversation_id)
            arts = await self.storage.artifacts(sid).snapshot()
            return build_preview(arts)
        except Exception:  # noqa: BLE001 - 预览是增强信息，读失败不该阻断确认流程
            logger.exception("读取编排预览失败（渲染确认门继续，只是少一块摘要）")
            return None

    def _take_asked_question(
        self, reg: ToolRegistry, tool_calls: Sequence[Any],
    ) -> dict[str, Any] | None:
        """本轮有没有 ``ask_user`` 调用；有就把它整理好的问题取出来。

        取一次就清掉：同一道问题不该在续跑后又被当成新问题弹一遍。
        """
        for tc in tool_calls:
            if tc.name != "ask_user":
                continue
            tool = reg.get("ask_user")
            question = getattr(tool, "last_question", None)
            if question:
                tool.last_question = None
                return dict(question)
        return None

    async def _follow_inflight_renders(
        self, ctx: AgentHookContext, calls: Sequence[tuple[str, Any]],
        registry: ToolRegistry | None = None,
    ) -> None:
        """渲染未达终态不许收尾：模型拿到 queued/running 就收尾时，由本循环自己轮到 done/failed。

        为什么在循环里做而不是靠提示词：成片卡片的两处出口（``MediaCardHook`` 的回投与
        ``record_rendered_media`` 的 qa.parts 落库）都只认**工具结果里出现的 media_url**，
        而渲染改成提交+轮询之后，「查到 done 才收尾」变成对模型情绪的依赖（README §6 记着
        这条代价）。这里把同一件事改成硬保证：轮询仍走真实工具调用（回投、进度、历史落库
        与前几轮的工具帧完全同形），只是发起方从模型换成循环本身。

        预算用尽仍没跑完时**不假称成功**：追加一条 system 说明渲染仍在进行，让模型如实
        告知用户稍后用 render_status 追，而不是回一句看不出破绽的收尾。
        """
        reg = registry if registry is not None else self.registry
        poll_name = next((n for n in reg.tool_names
                          if n == "render_status" or n.endswith("render_status")), "")
        if not poll_name:
            return                      # 无剪辑装配：没有可轮的对象，行为与改动前一致
        pending: list[str] = []
        for _, result in calls:
            art = _inflight_render_id(result)
            if art is not None and art not in pending:
                pending.append(art)
        if not pending:
            return

        deadline = time.monotonic() + max(0.0, self.config.render_follow_max_sec)
        unresolved: list[str] = []
        n_poll = 0
        last_beat = time.monotonic()
        cp = ctx.extras.get("checkpoint")
        while pending:
            art = pending.pop(0)
            if time.monotonic() >= deadline:
                unresolved.append(art)
                continue
            await asyncio.sleep(max(0.1, self.config.render_poll_sec))
            n_poll += 1
            # 心跳：一次长渲染代查可以持续几十分钟，而**租约只有 10 分钟**
            # （CheckpointManager.lease_sec）——不在这里续租，多副本部署下这条 run
            # 会被另一个实例判定为「无主/租约过期」而抢走，同一部片子被渲两遍。
            # 顺带把当前消息链落一次盘：中途被 kill 时恢复点不至于退回到代查之前。
            now = time.monotonic()
            if cp is not None and (now - last_beat) >= _RENDER_BEAT_SEC:
                last_beat = now
                try:
                    await self.checkpoint.save_progress(
                        cp, iteration=ctx.iteration, messages=ctx.messages)
                except Exception:  # noqa: BLE001 - 续租失败不该炸掉整轮执行
                    pass
            tc = ToolCall(id=f"render_follow_{n_poll}", name=poll_name,
                          arguments={"artifact_id": art})
            # 中间态只发进度帧、不进上下文：一次长渲染按秒级轮询，把每次「还在跑」都
            # 拼进 messages 会撑爆上下文，也只会让模型复读同一句话。
            await self.hooks.before_tool_call(ctx, tc.name, tc.arguments, call_id=tc.id)
            t0 = time.monotonic()
            try:
                # 必须打在本轮**实际**用的注册表上：poll_name 是从 reg 里挑出来的，
                # 而规划轮/子 Agent 用的是一张过滤过的注册表。打到 self.registry 上，
                # 子 Agent 与团队路径的「渲染未达终态不许收尾」会静默失效。
                # 超时兜底同 _execute_tool_calls：轮询挂死不能连整条分区一起挂住。
                r = await asyncio.wait_for(
                    reg.execute(tc.name, tc.arguments), timeout=_TOOL_CALL_TIMEOUT)
            except ToolError as e:
                r = e
            except asyncio.TimeoutError:
                r = ToolError(tc.name, f"Error executing {tc.name}: 超时"
                                       f"（{_TOOL_CALL_TIMEOUT:g}s 未返回）")
            except Exception as e:  # noqa: BLE001 - 轮询失败不该炸掉整轮执行
                r = ToolError(tc.name, f"Error executing {tc.name}: {e}")
            err = r if is_tool_error(r) else None
            await self.hooks.after_tool_call(
                ctx, tc.name, tc.arguments, None if err else r,
                time.monotonic() - t0, error=err, call_id=tc.id)
            again = _inflight_render_id(r)
            if again is not None:
                pending.append(again)   # 仍在跑：下一轮接着查这一片
                continue
            # 终态才进上下文：MediaCardHook 只认 messages 里的工具结果，成片卡片与
            # qa.parts 的持久链接都在这一步落地（与模型自己调 render_status 时同形）。
            ctx.messages.append(assistant(None, [tc]))
            ctx.messages.append(tool_result(tc.id, tc.name, r))
            await self.hooks.after_execute_tools(ctx)
        if unresolved:
            ctx.messages.append(system(
                f"⏳ 渲染在预算的 {self.config.render_follow_max_sec:g} 秒内仍未达终态"
                f"（artifact_id={', '.join(unresolved)}）。"
                f"不要声称成片已完成：如实告诉用户渲染仍在进行，"
                f"可稍后用 render_status 查这条 artifact_id。"))


def _inflight_render_id(result: Any) -> str | None:
    """工具结果 → 在途渲染的 artifact_id；不是「未达终态的渲染视图」就回 None。

    判据只认 ``render.status`` 为 queued/running 的形状（``render_jobs.tool_view`` 的
    非终态输出）：done/failed 带的是 output 终态产物，此时回 None，调用方按收尾处理。
    """
    if isinstance(result, dict):
        view = result
    elif isinstance(result, str):
        try:
            view = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        return None
    if not isinstance(view, dict):
        return None
    render = view.get("render")
    if not isinstance(render, dict):
        return None
    if str(render.get("status") or "") not in ("queued", "running"):
        return None
    art = view.get("artifact_id")
    return str(art) if art else "_default"


class Agent:
    """外层常驻循环：调度 AgentOnceRun，并隔离各 Session 的执行。

    同一 Session 串行、不同 Session 并行的顺序保证，在生产环境由 MQ 的
    session_id → Partition 路由提供；此处用 per-session 锁近似该语义。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        session_manager: SessionManager | None = None,
        context_builder: ContextBuilder | None = None,
        hooks: AgentHook | None = None,
        config: AgentConfig | None = None,
        checkpoint: CheckpointManager | None = None,
        storage: Any | None = None,
        plan_gate: Any | None = None,
        skill_loader: Any | None = None,
    ) -> None:
        # 注意：SessionManager 定义了 __len__，空实例是 falsy，必须用 is None 判断。
        self.session_manager = (
            session_manager if session_manager is not None else SessionManager(storage)
        )
        self.runner = AgentOnceRun(
            llm, registry, context_builder, hooks, config, checkpoint, storage=storage
        )
        self.storage = storage
        # 计划门（块 B）：没接上就是普通服务，plan() 如实退回 handle()。
        self.plan_gate = plan_gate
        self.skill_loader = skill_loader
        self._planning: ToolRegistry | None = None
        self._planning_sig: tuple[str, ...] | None = None
        self._queue: asyncio.Queue[_Request] = asyncio.Queue()
        self._locks: dict[str, asyncio.Lock] = {}
        self._running = False

    def _planning_registry(self, gate: Any) -> ToolRegistry:
        """规划轮注册表：按主注册表的名字清单当签名缓存，外部工具接入后自动换一张。

        MCP/Storyline 的工具要到 ``_startup`` 才注册进来，「启动后第一句话」和
        「接入之后」的规划轮工具集本来就不该是同一份——签名一变了就重建，
        否则会出现「节点已接入、规划轮里却查不到」。
        """
        signature = tuple(self.runner.registry.tool_names)
        if self._planning is None or self._planning_sig != signature:
            self._planning = gate.planning_registry(
                submit_tool=SubmitPlanTool(gate),
                confirm_tool=ConfirmPlanTool())
            self._planning_sig = signature
            # submit_plan 只活在这张独立注册表里，主注册表那次同步看不见它：
            # 不并进来，出口替换器换不出中文，界面上就是一句机器名。
            get_catalog().update(self._planning.displays())
        return self._planning

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def handle(
        self, user_id: str, conversation_id: str, message: str, *, run_id: str | None = None,
        stream: bool = False, attachments: Sequence[str] = (), resume: bool = False,
    ) -> str:
        """直接处理一条消息并返回结果（同步语义，便于调用与测试）。

        ``resume=True`` 才续跑该会话最近一个未完成的 run；默认不劫持——带进来的
        是一条新消息，就按新消息开新 run。「这条消息是不是『继续』」是入口处的
        意图判断（consumer / server），不是一个未落盘的快照该替用户决定的事。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        async with self._lock_for(session.session_id):
            if resume and self.runner.checkpoint is not None:
                cp = await self.runner.checkpoint.pending_for_session(session.session_id)
                if cp is not None:
                    return await self.runner.resume(cp, session, stream=stream)
            return await self.runner.run(
                session, message, run_id=run_id, stream=stream, attachments=attachments
            )

    async def plan(
        self, user_id: str, conversation_id: str, message: str, *,
        run_id: str | None = None, stream: bool = False,
        attachments: Sequence[str] = (), feedback: str = "",
        revise_of: str = "",
    ) -> str:
        """Run A 规划轮：注册表里物理不含剪辑节点，出口只有 ``submit_plan`` 或直接回答。

        没接计划门、或当前进程一个剪辑节点都没有（Storyline 未连通）时退回 ``handle``——
        没有可规划的节点，硬走规划轮只会白拦一道。

        ``revise_of`` 是「换一版」指回来的那条旧规划 run：卡面步骤只活在旧 run 的指针行里
        （会话历史中只有那一轮的文字摘要），不带上它，新一轮既无从做出「与旧卡可辨别的
        差异」，也往往干脆口头描述另一版而不再出卡。
        """
        gate = self.plan_gate
        if gate is None or not gate.whitelist():
            return await self.handle(user_id, conversation_id, message, run_id=run_id,
                                     stream=stream, attachments=attachments)
        prior = await self._prior_plans(revise_of)
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        # 续跑依据：上一条确认过的计划有没有跑完。「换一版」那一轮本来就在重做卡，
        # 再叠一段「立刻出续跑卡」只会两张卡打架。
        pending = None if (revise_of or feedback) else await pending_continuation(
            self.runner.checkpoint, session.session_id)
        # 同会话 checkpoint 历史：让新轮的 LLM 看到之前的计划卡和执行状态，
        # 不再说「计划没落到服务端」「素材没到位」。
        session_history = await self._session_checkpoint_brief(session.session_id)
        session_artifacts = await self._session_artifacts_brief(user_id, conversation_id)
        has_pending_plan = False
        if self.runner.checkpoint is not None:
            try:
                has_pending_plan = (
                    await self.runner.checkpoint.pending_plan_for_session(
                        session.session_id) is not None
                )
            except Exception:
                has_pending_plan = False
        async with self._lock_for(session.session_id):
            return await self.runner.run(
                session, message, run_id=run_id, stream=stream, attachments=attachments,
                registry=self._planning_registry(gate), planning=True,
                extra_sections=[await planning_section(gate, feedback=feedback,
                                                       prior=prior, pending=pending,
                                                       session_history=session_history,
                                                       session_artifacts=session_artifacts,
                                                       has_pending_plan=has_pending_plan)],
            )

    async def _prior_plans(self, revise_of: str) -> list[dict[str, Any]]:
        """换一版时要给模型看的旧卡：只有真跑过的那一份（指针行里已校验过的候选）。"""
        if not revise_of or self.runner.checkpoint is None:
            return []
        cp = await self.runner.checkpoint.load(revise_of)
        return list((cp.plan or {}).get("candidates") or []) if cp else []

    async def _session_checkpoint_brief(self, session_id: str) -> list[dict[str, Any]]:
        """同会话最近几条 checkpoint 的摘要，让新轮的 LLM 看到之前的规划与执行历史。

        checkpoint 按 ``session_id`` 绑定了会话窗口，但新轮的 LLM 只看得到 messages 表的
        对话文字，看不到 checkpoint 里的计划卡内容和执行状态。这里把最近 6 条 checkpoint
        的关键信息提出来，由 ``planning_section`` 注入 system 段。
        """
        if self.runner.checkpoint is None:
            return []
        rows = await self.runner.checkpoint.list_for_session(session_id)
        briefs: list[dict[str, Any]] = []
        for row in rows[:6]:
            plan = row.get("plan") or {}
            candidates = plan.get("candidates") or []
            audit = plan.get("audit") or {}
            briefs.append({
                "run_id": row.get("run_id", ""),
                "status": row.get("status", ""),
                "message": (row.get("message") or "")[:80],
                "iteration": row.get("iteration", 0),
                "is_planning": bool(candidates) and not row.get("plan_run_id"),
                "is_execution": bool(row.get("plan_run_id")),
                "plan_run_id": row.get("plan_run_id") or "",
                "candidates": [
                    {"plan_id": c.get("plan_id", ""), "label": c.get("label", ""),
                     "steps": [s.get("node", "") for s in (c.get("steps") or [])]}
                    for c in candidates
                ],
                "audit_plan_id": audit.get("plan_id", ""),
                "audit_unfulfilled": audit.get("unfulfilled") or [],
            })
        return briefs

    async def _session_artifacts_brief(self, user_id: str, conversation_id: str) -> list[str]:
        """同会话已产出的节点名列表，让新轮的 LLM 知道可以用 read_node_history 读什么。"""
        if self.storage is None:
            return []
        try:
            sid = storyline_session_id(user_id, conversation_id)
            repo = self.storage.artifacts(sid)
            return await repo.executed()
        except Exception:
            return []

    async def execute_plan(
        self, user_id: str, conversation_id: str, plan_run_id: str,
        frame: Mapping[str, Any], *, run_id: str | None = None,
        stream: bool = False, message: str = "",
    ) -> str:
        """Run B 执行轮：把计划卡上的点击帧编译成两段注入，然后是一次普通 ReAct。

        计划本体只认服务端自己存的那一份：按 ``plan_run_id`` 从指针行取回
        ``submit_plan`` 当轮通过四重校验的候选，浏览器回传的只有「选了哪张卡、
        点了哪些开关、另填了什么」。选不到对应卡就是错——用户无从伪造承诺。
        """
        gate = self.plan_gate
        if gate is None:
            raise RuntimeError("未启用计划门，无法按确认帧执行")
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，取不回候选计划")
        row = await self.runner.checkpoint.row(plan_run_id)
        candidates = ((row or {}).get("plan") or {}).get("candidates") or []
        wanted = str(frame.get("selected_plan") or "").strip()
        plan = next((c for c in candidates
                     if str(c.get("plan_id") or "") == wanted), None)
        if plan is None:
            raise KeyError(
                f"计划 {plan_run_id} 里没有选中的那张卡「{wanted or '(空)'}」"
                f"（可选：{[c.get('plan_id') for c in candidates] or '无'}）。")
        compiled, issues = gate.validate_execute(plan, frame)
        if not issues.ok:
            raise ValueError("确认帧未通过校验：" + "；".join(issues.errors))
        sections = list(render_injections(compiled))
        sections += await preload_skills(self.skill_loader, compiled.get("skills_hint") or ())
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        # 从规划轮的用户消息中提取附件 ID，传递到执行轮——
        # 否则 Agent 会忘记用户已上传的素材（如配乐），跑去网上重新下载。
        attachments: list[str] = []
        if self.storage is not None:
            try:
                history = await self.storage.messages.history(user_id, conversation_id, limit=20)
                for msg in reversed(history):
                    if msg.get("role") == "user" and msg.get("attachments"):
                        attachments = list(msg["attachments"])
                        break
            except Exception:
                pass
        async with self._lock_for(session.session_id):
            return await self.runner.run(
                session, message or f"按已确认的计划执行：{compiled.get('label') or wanted}",
                run_id=run_id, stream=stream, attachments=attachments,
                extra_sections=sections,
                approved_plan=compiled, plan_run_id=plan_run_id,
            )

    async def resume(
        self, run_id: str, user_id: str, conversation_id: str, *, stream: bool = False,
        at_seq: int | None = None,
    ) -> str:
        """恢复一次未完成的执行（崩溃恢复入口）。

        at_seq 给定时回到该 run 的某个一致点再续跑——同一把入口既做崩溃恢复
        也做时间旅行，区别只在调用方要不要指到某一界。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，无法恢复执行")
        async with self._lock_for(session.session_id):
            cp = await self.runner.checkpoint.resume(run_id, at_seq)
            if cp is None:
                raise KeyError(f"没有可恢复的 checkpoint: {run_id}")
            return await self.runner.resume(cp, session, stream=stream)

    async def approve(
        self, run_id: str, user_id: str, conversation_id: str, *, decision: str,
        stream: bool = False, note: str = "",
    ) -> str:
        """用户对一条挂起审批的执行做出批准/拒绝，从断点续跑。

        ``decision`` 为 "approve" / "reject" / 兜底方案 key / 弹窗选项 key；
        ``note`` 是用户在弹窗里选或写的原话，带进上下文供模型据此继续。
        计划门产出的执行轮（Run B）与普通执行轮都可能挂起，这里不再分轮次
        ——只认那条 awaiting_approval 的 run。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，无法处理审批")
        async with self._lock_for(session.session_id):
            cp = await self.runner.checkpoint.pending_approval(run_id)
            if cp is None:
                raise KeyError(f"没有待审批的 checkpoint: {run_id}")
            return await self.runner.approve(cp, session, decision=decision,
                                            stream=stream, note=note)

    async def fork(
        self, run_id: str, at_seq: int, user_id: str, conversation_id: str, *,
        stream: bool = False, run_id_new: str | None = None,
        invalidate: Sequence[str] = (), message: str = "",
    ) -> str:
        """从某个一致点分叉重跑：换一份剪辑产物作用域，上游产物整份复用。

        典型用法是「换 BGM 从 plan_timeline 重跑」：只重放分叉点之后的节点，
        切镜/ASR/画面理解的结果从父 run 复制过来，服务端拦截器据此跳过补齐。
        ``invalidate`` 是这次要真重做的节点集（``checkpoint.fork`` 会把这些产物删掉，
        否则拦截器认为已完成、恰好跳过用户要求重跑的那一步）。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，无法分叉执行")
        async with self._lock_for(session.session_id):
            child = await self.runner.checkpoint.fork(
                run_id, at_seq, run_id_new=run_id_new, invalidate=invalidate,
                message=message)
            return await self.runner.resume(child, session, stream=stream)

    async def pending_runs(self) -> list[Checkpoint]:
        """所有未正常结束的执行（服务启动时补跑或对账用）。"""
        if self.runner.checkpoint is None:
            return []
        return await self.runner.checkpoint.pending()

    async def submit(
        self, user_id: str, conversation_id: str, message: str,
        *, attachments: Sequence[str] = (), run_id: str | None = None,
    ) -> None:
        """投递到 Main Loop 队列，由 worker 异步消费。"""
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        await self._queue.put(
            _Request(session, message, [str(m) for m in attachments], run_id)
        )

    async def run_forever(self) -> None:
        """常驻消费队列；不同 Session 并发、同一 Session 串行。"""
        self._running = True
        while self._running:
            req = await self._queue.get()
            asyncio.create_task(self._process(req))

    async def _process(self, req: _Request) -> None:
        async with self._lock_for(req.session.session_id):
            await self.runner.run(
                req.session, req.message, attachments=req.attachments, run_id=req.run_id
            )

    def stop(self) -> None:
        self._running = False
