"""SessionConsumer：把 MQ 消费端接到 Agent（架构图「MQ → 会话 → Session Manager → AgentLoop」）。

订阅聊天 topic，收到消息后按 (user_id, conversation_id) 交给 Agent.handle 执行。
「同会话串行、异会话并行」的顺序由 MQ 的 key=session_id 分区语义保证（见 mq.py），
Agent 内部的 per-session 锁是同一语义的兜底。
"""

from __future__ import annotations

from typing import Any

from .agent import Agent
from .connection_manager import OUTBOUND_TOPIC
from .mq import MessageQueue

CHAT_TOPIC = "chat"
CONSUMER_GROUP = "agent-workers"


def session_key(user_id: str, conversation_id: str) -> str:
    """MQ 分区 key：同一 (user, conversation) 落到同一分区，保证有序。"""
    return f"{user_id}:{conversation_id}"


def _same_intent(previous: str | None, incoming: str) -> bool:
    """这条消息是不是「接着跑被打断的那次」，而不是一条新诉求。

    自动续跑的触发条件必须带这一道：执行轮的 checkpoint 记着**上次**的消息，
    用户这次说的是不是同一件事，代码得看一眼。原先不看，于是同一会话里任何新指令
    都会被静默换成「重跑上一条」——用户的新话既没被执行也没落库，界面上那条气泡
    永远等不到回复（真机复现：发「把它改成竖屏，重做时间线」，后端跑的是旧 run）。

    判据取「归一化后是否同源」：点「继续」、刷新后重发同一句 → 续跑；
    换了内容 → 按新诉求走规划轮。归一化只压空白与标点，不改语义。
    """
    norm = lambda s: "".join(ch for ch in str(s or "") if not ch.isspace())
    prev, cur = norm(previous), norm(incoming)
    if not prev or not cur:
        return False
    if prev == cur:
        return True
    # 「继续」这类纯推进口令不重复描述诉求，也算续跑意图。
    return cur in _CONTINUE_WORDS


_CONTINUE_WORDS = frozenset({
    "继续", "接着", "接着跑", "继续吧", "继续执行", "接着来", "往下", "goon", "continue",
})


class SessionConsumer:
    def __init__(
        self,
        agent: Agent,
        mq: MessageQueue,
        *,
        topic: str = CHAT_TOPIC,
        group: str = CONSUMER_GROUP,
    ) -> None:
        self.agent = agent
        self.mq = mq
        self.topic = topic
        self.group = group
        self.processed: list[str] = []  # 已处理的 run_id，观测 / 测试用
        self._subscribed = False

    def start(self) -> None:
        """注册消费回调（需在 mq.start() 之前调用）。"""
        if not self._subscribed:
            self.mq.subscribe(self.topic, self.group, self._handle)
            self._subscribed = True

    async def _handle(self, payload: dict[str, Any]) -> str:
        user_id = payload["user_id"]
        conversation_id = payload["conversation_id"]
        message = payload["message"]
        run_id = payload.get("run_id")
        attachments = payload.get("attachments") or []
        # 「继续」是入口处的意图：帧里显式写了 resume 才续跑在途 run，否则新消息开新 run。
        resume = bool(payload.get("resume"))
        action = payload.get("action") or {}
        sid = session_key(user_id, conversation_id)
        # 有效 run_id：正常情况就是帧里那个；自动续跑时会换成真正在跑的 cp.run_id，
        # 好让终答与回投帧挂在同一条 run 上（否则前端忙等状态对不上，答复被丢）。
        effective_run_id = run_id
        # stream=True：LLM delta 经 on_stream Hook → OutBound（见 OutboundStreamHook）。
        try:
            if action.get("op") == "fork":
                # 时间旅行重跑：从旧 run 的某个一致点开的新 run，run_id 就是分叉出来的那条。
                answer = await self.agent.fork(
                    action["run_id"], int(action["at_seq"]), user_id, conversation_id,
                    stream=True, run_id_new=run_id,
                    invalidate=action.get("invalidate") or (), message=message)
            elif action.get("op") == "resume":
                answer = await self.agent.resume(
                    action["run_id"], user_id, conversation_id, stream=True,
                    at_seq=action.get("at_seq"))
            elif action.get("op") == "approve":
                # HITL：用户对挂起审批的执行做出批准/拒绝，从断点续跑。
                # message 是用户在弹窗里选或写的原话（自定义文本 / 多题汇总），
                # 一并带进去——否则「选项之外自己写一句」这条路填了也白填。
                answer = await self.agent.approve(
                    action["run_id"], user_id, conversation_id,
                    decision=str(action.get("decision") or "approve"), stream=True,
                    note=str(message or ""),
                    answers=list(action.get("answers") or []))
            elif action.get("op") == "execute_plan":
                # 计划卡上的点击帧。计划本体不从浏览器来：agent 按 plan_run_id 取回
                # 服务端自己校验过的那一份，本帧只回答「选了哪张卡、动了哪些开关」。
                answer = await self.agent.execute_plan(
                    user_id, conversation_id, action["plan_run_id"],
                    action.get("frame") or {}, run_id=run_id, stream=True,
                    message=message)
            elif action.get("op") == "execute":
                # 明确跳过计划门的投递：只有「到点自动跑」的定时任务走这条——
                # 那条消息没有坐在屏幕前的用户来点确认，拦在规划轮里等于任务永不落地。
                # interactive=False 把同一件事贯彻到**所有**确认门（含渲染前确认）：
                # 只跳计划门是不够的——渲染门照样会拦，任务还是永久挂在等确认上
                # （user_id 是 "cron"，浏览器过不了归属校验，没人能来点）。
                answer = await self.agent.handle(
                    user_id, conversation_id, message, run_id=run_id, stream=True,
                    attachments=attachments, resume=resume, interactive=False
                )
            elif action.get("op") == "plan":
                answer = await self.agent.plan(
                    user_id, conversation_id, message, run_id=run_id, stream=True,
                    attachments=attachments, feedback=str(action.get("feedback") or ""),
                    revise_of=str(action.get("revise_of") or ""))
            else:
                # 默认入口就是规划轮（不是用户可关的开关）：剪辑诉求先出计划卡再执行。
                # 无剪辑节点 / 未接计划门时 plan() 自己退回 handle()，闲聊与咨询照原样回答。
                # 「继续」是唯一的例外：那是接着跑在途 run，不是又一条新诉求。
                if resume:
                    answer = await self.agent.handle(
                        user_id, conversation_id, message, run_id=run_id, stream=True,
                        attachments=attachments, resume=True)
                else:
                    # 同会话有被打断的执行轮时自动续跑，而不是开新规划轮。
                    # 执行轮的 checkpoint 里带着完整的执行上下文（已加载的素材、ASR 结果等），
                    # 续跑时 LLM 能直接接着干，不会再说「计划没落到服务端」「素材没到位」。
                    #
                    # 但**只有这一条消息确实是「接着跑」时才续**：原先无条件 resume，
                    # 于是同一会话里用户真正的新指令被整条吞掉（既不执行、也不落库），
                    # 界面上的气泡永远等不到回复。判据取「消息内容与被打断那次是否同源」——
                    # 同一句重发（点「继续」/刷新后重试）算续跑，换了内容就是新诉求。
                    runner = getattr(self.agent, 'runner', None)
                    cp_mgr = getattr(runner, 'checkpoint', None) if runner else None
                    cp = None
                    if cp_mgr is not None:
                        try:
                            cp = await cp_mgr.interrupted_execution_for_session(sid)
                        except Exception:
                            cp = None
                    if cp is not None and _same_intent(cp.message, message):
                        # 续跑：真正在跑的是 cp 那条 run。必须把这个事实回投给前端——
                        # 否则流式帧都挂 cp.run_id，而前端 busy 记的是 /chat 回的新 uuid，
                        # 逐帧守卫会把它们全丢掉。
                        run_id = cp.run_id
                        effective_run_id = cp.run_id
                        await self.mq.publish(OUTBOUND_TOPIC, sid, {
                            "type": "run_adopted", "session_id": sid,
                            "run_id": cp.run_id, "previous_run_id": payload.get("run_id"),
                        })
                        answer = await self.agent.resume(
                            cp.run_id, user_id, conversation_id, stream=True)
                    else:
                        answer = await self.agent.plan(
                            user_id, conversation_id, message, run_id=run_id,
                            stream=True, attachments=attachments)
        except Exception as e:  # noqa: BLE001  失败也要回执，前端不能干等
            await self.mq.publish(
                OUTBOUND_TOPIC,
                sid,
                {"type": "error", "session_id": sid,
                 "run_id": effective_run_id, "error": str(e)},
            )
            raise
        if effective_run_id:
            self.processed.append(effective_run_id)
        # 结果携带 session_id → OutBound → Connection Manager → 对应 Web 连接。
        await self.mq.publish(
            OUTBOUND_TOPIC,
            sid,
            {"type": "answer", "session_id": sid,
             "run_id": effective_run_id, "answer": answer},
        )
        return answer
