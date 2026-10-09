"""Hook 可观测生命周期接缝。

对应设计文档「Hook 可观测机制」：AgentHook 定义 Agent 执行链路的关键节点，
CompositeHook 负责把这些节点统一转发给一组 Hook，单个 Hook 异常不影响其它。

除基类与组合器外，本模块还内置两个可复用的观测实现：
- LoggingHook：把每个生命周期节点写成结构化日志（可观测/排障）。
- MetricsHook：累计迭代数、工具调用数、流式块数、起止时延，供快照与指标上报。
需要更多观测（追踪、计费等）只需继承 AgentHook 覆写对应节点，再传给 CompositeHook。
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .connection_manager import OUTBOUND_TOPIC
from .media_replay import record_rendered_media
from .messages import Message
from .mq import MessageQueue
from .run_state import RunState
from .session import Session
from .storage.db import Cond
from .tool import UnknownToolError

logger = logging.getLogger("agent_framework")

# 工具执行期间暴露当前 Hook 上下文与 Hook 链，供 SpawnTool 等工具触发子 Agent 生命周期节点。
_current_hook_ctx: contextvars.ContextVar[AgentHookContext | None] = contextvars.ContextVar(
    "_current_hook_ctx", default=None
)
_current_hooks: contextvars.ContextVar[AgentHook | None] = contextvars.ContextVar(
    "_current_hooks", default=None
)


@dataclass
class AgentHookContext:
    """一次 AgentOnceRun 期间共享的上下文快照。

    ``state`` 是**跨挂起仍然成立**的那部分事实（批准的计划、已发生的调用、候选计划、
    对账结论、当轮 qa 片段），由 ``_drive`` 从 checkpoint 的指针行恢复、并在每个一致点
    写回——所以续跑之后对账不会从零开始。``extras`` 只放**本轮调用私有**的注入
    （run_id / checkpoint / checkpoint_manager / handover 等），刻意不落盘。
    """

    session: Session
    messages: list[Message] = field(default_factory=list)
    iteration: int = 0
    state: RunState = field(default_factory=RunState)
    extras: dict[str, Any] = field(default_factory=dict)


class AgentHook:
    """Agent 生命周期节点基类，全部默认 no-op。"""

    async def before_iteration(self, context: AgentHookContext) -> None:
        """每轮调用 LLM 之前。"""

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """LLM 流式输出每一段时。"""

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """本次流式输出结束时。"""

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        """工具执行完成之后。"""

    async def on_approval_required(
        self, context: AgentHookContext, calls: list[dict], reason: str = "",
        fallback_options: list[dict] | None = None,
        ask: dict | None = None,
    ) -> None:
        """执行撞上「需人工审批」的工具、或模型主动提问、或需要用户确认编排时。

        ``calls`` 是待批的工具调用描述（主动提问时可以为空）。

        ``fallback_options`` 非空时，是工具返回的可选方案列表（如渲染兜底选项），
        前端据此渲染为选项按钮而非批准/拒绝二选一。

        ``ask`` 非空时是**结构化提问**：``{title, options:[{key,label,description,
        recommended}], allow_custom, preview}``。前端把它渲染成编号选项卡弹窗
        （与截图同一形态），用户选中的 key 作为 decision 回喂。
        ``preview`` 可选，用来在弹窗里附带「编排结果」摘要，供用户看着决定。
        """

    async def on_llm_usage(
        self, context: AgentHookContext, usage: dict[str, int],
    ) -> None:
        """一次 LLM 调用返回的 token 用量。usage = {prompt_tokens, completion_tokens, total_tokens}。"""

    async def before_tool_call(
        self, context: AgentHookContext, tool_name: str, arguments: dict, *,
        call_id: str = "",
    ) -> None:
        """单个工具即将执行。

        ``call_id`` 是这次调用的编号（模型给的 tool_call_id）：同一批里两个同名调用
        并发跑，只有它能把参数与回执配回同一次调用。
        """

    async def after_tool_call(
        self, context: AgentHookContext, tool_name: str, arguments: dict,
        result: Any, elapsed: float, *, error: Exception | None = None,
        call_id: str = "",
    ) -> None:
        """单个工具执行完成（或出错）。``call_id`` 同 before_tool_call。"""

    async def on_subagent_spawn(self, context: AgentHookContext, task: str) -> None:
        """子 Agent 即将启动。"""

    async def on_subagent_end(self, context: AgentHookContext, task: str, result: str) -> None:
        """子 Agent 执行完成。"""

    def finalize_content(
        self, context: AgentHookContext, content: str | None
    ) -> str | None:
        """最终内容生成后，可修改并返回。"""
        return content


class CompositeHook(AgentHook):
    """把生命周期节点转发给一组 Hook；async 节点吞掉单个异常并记录日志。"""

    def __init__(self, hooks: list[AgentHook]) -> None:
        self._hooks = list(hooks)

    def add(self, hook: AgentHook) -> None:
        self._hooks.append(hook)

    async def before_iteration(self, context: AgentHookContext) -> None:
        for h in self._hooks:
            try:
                await h.before_iteration(context)
            except QuotaExceeded:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.before_iteration error in %s", type(h).__name__)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        for h in self._hooks:
            try:
                await h.on_stream(context, delta)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.on_stream error in %s", type(h).__name__)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        for h in self._hooks:
            try:
                await h.on_stream_end(context, resuming=resuming)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.on_stream_end error in %s", type(h).__name__)

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        for h in self._hooks:
            try:
                await h.after_execute_tools(context)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.after_execute_tools error in %s", type(h).__name__)

    async def on_approval_required(
        self, context: AgentHookContext, calls: list[dict], reason: str = "",
        fallback_options: list[dict] | None = None,
        ask: dict | None = None,
    ) -> None:
        for h in self._hooks:
            try:
                await h.on_approval_required(context, calls, reason,
                                            fallback_options=fallback_options, ask=ask)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.on_approval_required error in %s", type(h).__name__)

    async def on_llm_usage(
        self, context: AgentHookContext, usage: dict[str, int],
    ) -> None:
        for h in self._hooks:
            try:
                await h.on_llm_usage(context, usage)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.on_llm_usage error in %s", type(h).__name__)

    async def before_tool_call(
        self, context: AgentHookContext, tool_name: str, arguments: dict, *,
        call_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                await h.before_tool_call(context, tool_name, arguments, call_id=call_id)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.before_tool_call error in %s", type(h).__name__)

    async def after_tool_call(
        self, context: AgentHookContext, tool_name: str, arguments: dict,
        result: Any, elapsed: float, *, error: Exception | None = None,
        call_id: str = "",
    ) -> None:
        for h in self._hooks:
            try:
                await h.after_tool_call(context, tool_name, arguments, result, elapsed,
                                        error=error, call_id=call_id)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.after_tool_call error in %s", type(h).__name__)

    async def on_subagent_spawn(self, context: AgentHookContext, task: str) -> None:
        for h in self._hooks:
            try:
                await h.on_subagent_spawn(context, task)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.on_subagent_spawn error in %s", type(h).__name__)

    async def on_subagent_end(self, context: AgentHookContext, task: str, result: str) -> None:
        for h in self._hooks:
            try:
                await h.on_subagent_end(context, task, result)
            except Exception:  # noqa: BLE001
                logger.exception("AgentHook.on_subagent_end error in %s", type(h).__name__)

    def finalize_content(
        self, context: AgentHookContext, content: str | None
    ) -> str | None:
        # 唯一可改内容的节点：串接每个 Hook 的输出。
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content


# --------------------------------------------------------------------------
# 可复用的观测实现
# --------------------------------------------------------------------------


class LoggingHook(AgentHook):
    """把生命周期节点写成结构化日志，用于可观测与排障。"""

    def __init__(self, logger_: logging.Logger | None = None, level: int = logging.DEBUG) -> None:
        self._log = logger_ or logger
        self._level = level

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._log.log(
            self._level,
            "iter %s start (session=%s, messages=%d)",
            context.iteration,
            context.session.session_id,
            len(context.messages),
        )

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        self._log.log(self._level, "stream delta %d chars", len(delta))

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        self._log.log(self._level, "stream end (resuming=%s)", resuming)

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        self._log.log(
            self._level,
            "tools done (session=%s, messages=%d)",
            context.session.session_id,
            len(context.messages),
        )


class MetricsHook(AgentHook):
    """累计一次执行的关键指标，暴露 snapshot() 供上报/断言。"""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.iterations = 0
        self.tool_rounds = 0
        self.stream_chunks = 0
        self.stream_ends = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self._started_at: float | None = None
        self._ended_at: float | None = None

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._started_at is None:
            self._started_at = self._clock()
        self.iterations += 1

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        self.stream_chunks += 1

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        self.stream_ends += 1

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        self.tool_rounds += 1

    async def on_llm_usage(
        self, context: AgentHookContext, usage: dict[str, int],
    ) -> None:
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.total_tokens += usage.get("total_tokens", 0)

    def finalize_content(
        self, context: AgentHookContext, content: str | None
    ) -> str | None:
        self._ended_at = self._clock()
        return content

    def snapshot(self) -> dict[str, Any]:
        elapsed = None
        if self._started_at is not None and self._ended_at is not None:
            elapsed = self._ended_at - self._started_at
        return {
            "iterations": self.iterations,
            "tool_rounds": self.tool_rounds,
            "stream_chunks": self.stream_chunks,
            "stream_ends": self.stream_ends,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "elapsed": elapsed,
        }


class OutboundStreamHook(AgentHook):
    """文档数据流的「流式拦截 → MQ OutBound」节点。

    on_stream 每收到一段 LLM delta，就以 key=session_id 发布 delta 消息到
    OutBound topic；Connection Manager 消费后按 session_id 找回对应 Web
    连接推送（见 connection_manager.py）。on_stream_end 发结束标记，供前
    端区分两轮流式输出。run_id 来自 ctx.extras，用于前端聚合同一次执行。
    """

    def __init__(self, mq: MessageQueue, *, topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._topic = topic

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        session_id = context.session.session_id
        await self._mq.publish(
            self._topic,
            session_id,
            {
                "type": "delta",
                "session_id": session_id,
                "run_id": context.extras.get("run_id"),
                "iteration": context.iteration,
                "text": delta,
            },
        )

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        session_id = context.session.session_id
        await self._mq.publish(
            self._topic,
            session_id,
            {
                "type": "stream_end",
                "session_id": session_id,
                "run_id": context.extras.get("run_id"),
                "iteration": context.iteration,
                "resuming": resuming,
            },
        )


class ApprovalHook(AgentHook):
    """HITL：撞上「需人工审批」的工具、或模型主动提问时 → MQ OutBound 一条交互帧。

    前端据此弹出选项卡；用户的选择经 ``action.op="approve"`` 回到 consumer，
    再从挂起的 checkpoint 续跑（见 ``agent.AgentOnceRun.approve``）。

    帧里的 ``ask`` 是这一轮加的**结构化提问**（可选）：``title`` 是问题本身，
    ``options[]`` 是编号可选项，``allow_custom`` 表示还允许用户自己写一条。
    没有 ``ask`` 时，前端退回原来的「批准 / 拒绝 + fallback_options」形态——
    两种形态都在用同一个弹窗组件，所以老路径不会被砍掉。
    """

    def __init__(self, mq: MessageQueue, *, topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._topic = topic

    async def on_approval_required(
        self, context: AgentHookContext, calls: list[dict], reason: str = "",
        fallback_options: list[dict] | None = None,
        ask: dict | None = None,
    ) -> None:
        session_id = context.session.session_id
        payload: dict = {
            "type": "approval",
            "session_id": session_id,
            "run_id": context.extras.get("run_id"),
            "calls": calls,
            "reason": reason,
            "fallback_options": fallback_options or [],
        }
        if ask:
            payload["ask"] = ask
        await self._mq.publish(self._topic, session_id, payload)


class QuotaExceeded(Exception):
    """用户在配额窗口内的 token 用量超限。Agent 收到后以此收尾，不再调 LLM。"""

    def __init__(self, user_id: str, used: int, limit: int, window_label: str) -> None:
        self.user_id = user_id
        self.used = used
        self.limit = limit
        self.window_label = window_label
        super().__init__(
            f"配额已用尽：{user_id} 在{window_label}内已用 {used} token，上限 {limit}。"
        )


class UsageHook(AgentHook):
    """把每次 LLM 调用的 token 用量落 token_usage 表（配额观测 + 成本核算的数据源）。"""

    def __init__(self, db: Any, *, model: str = "") -> None:
        self._db = db
        self._model = model

    async def on_llm_usage(
        self, context: AgentHookContext, usage: dict[str, int],
    ) -> None:
        sid = context.session.session_id
        user_id = context.session.user_id
        run_id = context.extras.get("run_id") or ""
        await self._db.insert("token_usage", {
            "user_id": user_id,
            "session_id": sid,
            "run_id": run_id,
            "model": self._model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        })


class QuotaHook(AgentHook):
    """每轮调 LLM 之前查近 window 秒的用量合计，超 limit 抛 QuotaExceeded。

    limit=0 表示不限制（默认）。window 以秒计，常见值：3600（小时）、86400（天）。
    查询走 token_usage(user_id, created_at) 索引，单行聚合，开销可忽略。
    """

    def __init__(self, db: Any, *, limit: int = 0, window: int = 86400) -> None:
        self._db = db
        self.limit = limit
        self.window = window

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self.limit <= 0:
            return
        user_id = context.session.user_id
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.window)
        rows = await self._db.select(
            "token_usage",
            where=[Cond("user_id", "eq", user_id), Cond("created_at", "ge", cutoff)],
        )
        used = sum(int(r.get("total_tokens") or 0) for r in rows)
        if used >= self.limit:
            label = (f"{self.window // 3600} 小时" if self.window >= 3600
                     else f"{self.window} 秒")
            raise QuotaExceeded(user_id, used, self.limit, label)


def _find_media(obj: Any) -> dict[str, Any] | None:
    """递归在工具结果 JSON 里找带 media_url 的 dict（mock / 远程打包契约都适用）。

    判据 scheme 无关：真链路是 MinIO presigned ``http(s)://``，离线替身是 ``memory://``。
    """
    if isinstance(obj, dict):
        url = obj.get("media_url")
        if isinstance(url, str) and "://" in url:
            return obj
        for v in obj.values():
            hit = _find_media(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_media(v)
            if hit:
                return hit
    return None


def _evidence_view(raw: Any) -> list[dict[str, Any]]:
    """渲染产物里的证据账 → 卡片用的最小形状（主张 / 级别 / 验没验）。

    只挑这三样：`proof`（怎么验的）在工具结果里模型已经读过一份，卡片不复述全文；
    用户在界面上要的是「这一条到底验过没有」这一眼。
    """
    out: list[dict[str, Any]] = []
    for e in (raw if isinstance(raw, list) else []):
        if not isinstance(e, dict) or not e.get("claim"):
            continue
        out.append({"claim": str(e["claim"]),
                    "label": str(e.get("label") or e.get("level") or ""),
                    "verified": str(e.get("status") or "") == "verified"})
    return out


class MediaCardHook(AgentHook):
    """after_execute_tools 观测节点 → 成片播放卡片回投 MQ OutBound。

    工具（render_video，本地 mock 或远程 Storyline）结果 JSON 中只要出现
    media_url，就以 type=media 发布到 OutBound topic；Connection Manager
    按 session_id 找回对应会话窗口，前端据此渲染 <video> 播放卡片。
    不依赖 LLM 在最终回答里复述链接——链路确定性由 Hook 保证。
    """

    def __init__(self, mq: MessageQueue, *, topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._topic = topic

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        session_id = context.session.session_id
        seen: set[str] = context.extras.setdefault("_media_urls_pushed", set())
        for msg in context.messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or '"media_url"' not in content:
                continue
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                continue
            item = _find_media(data)
            if not item or item["media_url"] in seen:
                continue
            seen.add(item["media_url"])
            # 证据分级（真节点在终态产物里带一份）：卡片要说清哪几条是机器算过/抽帧看过、
            # 哪几条压根没验。没有这个字段的链路（离线替身、旧数据）不硬造一条空账。
            evidence = _evidence_view(item.get("evidence"))
            frame = {
                "type": "media",
                "session_id": session_id,
                "run_id": context.extras.get("run_id"),
                "media_url": item["media_url"],
                "title": item.get("title") or "",
                "duration": item.get("duration"),
                # 前端「选区改」按钮的开关：只有图形科普片那条路的终态带 hitmap
                # （屏幕上这一块 ↔ 分镜那一格的凭据）。没有表就只能整片重做，
                # 按钮摆上去点了也是 404——所以判据从产物里带出来，不由前端猜。
                "artifact_id": data.get("artifact_id") if isinstance(data, dict) else None,
                "hitmap": bool(item.get("hitmap")),
            }
            if evidence:
                frame["evidence"] = evidence
            await self._mq.publish(self._topic, session_id, frame)
            # 除了当轮回投，再落一条**持久链接**到本轮 assistant 行的 qa.parts（README §6）：
            # media_url 是会过期的 presigned 直链，不能当持久键；用渲染的对象键（真节点回在
            # video 字段）+ artifact_id 才是稳定指针，读取历史时再现签一条直链。
            object_key = item.get("video")
            if isinstance(object_key, str) and object_key:
                record_rendered_media({
                    "artifact_id": data.get("artifact_id") if isinstance(data, dict) else None,
                    "video_object_key": object_key,
                    "title": item.get("title") or "",
                    "duration": item.get("duration"),
                    "hitmap": bool(item.get("hitmap")),
                    **({"evidence": evidence} if evidence else {}),
                })

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _render_view(result: Any) -> dict[str, Any] | None:
    """工具结果里的渲染任务视图（``render`` 映射）单独摘出来，别跟着 result 一起截断。

    render_video 的结果把 ``render`` 块排在 presigned 直链之后，``_truncate(result, 600)``
    正好把它削掉——前端进度条拿不到起步的那一帧（真机：整条渲染链路跑完，聊天窗一根进度条
    都没出现）。摘出来只带四个进度字段，帧体积不因此涨起来。
    """
    data = result
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    view = data.get("render")
    if not isinstance(view, dict):
        return None
    out = {"artifact_id": data.get("artifact_id") or view.get("artifact_id") or ""}
    for key in ("status", "stage", "percent", "error"):
        if view.get(key) is not None:
            out[key] = view[key]
    return out if out.get("status") else None


def _safe_json(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


class ToolTraceHook(AgentHook):
    """工具调用追踪 → MQ OutBound → 前端实时展示。

    before_tool_call 发布 type=tool_call，after_tool_call 发布 type=tool_result，
    on_subagent_spawn / on_subagent_end 发布子 Agent 生命周期标记。
    子 Agent 场景下通过 session_override 把子 Agent 的工具调用回投到父会话窗口。
    """

    def __init__(self, mq: MessageQueue, *, topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._topic = topic
        self.session_override: str | None = None

    def _sid(self, context: AgentHookContext) -> str:
        return self.session_override or context.session.session_id

    async def before_tool_call(
        self, context: AgentHookContext, tool_name: str, arguments: dict, *,
        call_id: str = "",
    ) -> None:
        sid = self._sid(context)
        await self._mq.publish(
            self._topic, sid,
            {
                "type": "tool_call",
                "session_id": sid,
                "run_id": context.extras.get("run_id"),
                "iteration": context.iteration,
                "tool": tool_name,
                "call_id": call_id,
                "arguments": _safe_json(arguments),
            },
        )

    async def after_tool_call(
        self, context: AgentHookContext, tool_name: str, arguments: dict,
        result: Any, elapsed: float, *, error: Exception | None = None,
        call_id: str = "",
    ) -> None:
        sid = self._sid(context)
        await self._mq.publish(
            self._topic, sid,
            {
                "type": "tool_result",
                "session_id": sid,
                "run_id": context.extras.get("run_id"),
                "iteration": context.iteration,
                "tool": tool_name,
                "call_id": call_id,
                # 注册表里没有这个工具 = 调用从未打到后端。前端拿它决定这一步该不该
                # 记进剪辑进度：规划轮挡下的一次试差不算「剪辑受阻」。
                "invoked": not isinstance(error, UnknownToolError),
                "elapsed": round(elapsed, 3),
                "ok": error is None,
                "error": str(error) if error else None,
                "result": _truncate(str(result), 600),
                # 渲染进度字段独立带上：result 截断后前端仍要能起步（见 _render_view）。
                "render": _render_view(result),
            },
        )

    async def on_subagent_spawn(self, context: AgentHookContext, task: str) -> None:
        sid = self._sid(context)
        await self._mq.publish(
            self._topic, sid,
            {
                "type": "subagent_spawn",
                "session_id": sid,
                "run_id": context.extras.get("run_id"),
                "task": _truncate(task, 200),
            },
        )

    async def on_subagent_end(self, context: AgentHookContext, task: str, result: str) -> None:
        sid = self._sid(context)
        await self._mq.publish(
            self._topic, sid,
            {
                "type": "subagent_end",
                "session_id": sid,
                "run_id": context.extras.get("run_id"),
                "task": _truncate(task, 200),
                "result": _truncate(result, 300),
            },
        )
