"""上下文压缩：在窗口预算内分级裁剪历史。

对应设计文档「上下文压缩」的三级递进策略（仅当估算 token 超过预算时逐级触发）：
  第一级  折叠 Tool 的信息：把较旧的大段工具结果压成预览，只保留最近若干条完整结果。
  第二级  丢弃太过久远的消息：仍超预算时，从最早的历史消息开始淘汰。
  第三级  对过往消息做全量摘要：把被淘汰的消息压成一条摘要，保留要点。

被折叠/丢弃/摘要后始终保留：首条 system、最后一条当前 user 输入。

本模块产出的是 ContextBuilder 需要的 compressor 接缝：
    async def __call__(messages, query) -> messages
默认摘要器不依赖 LLM（确定性）；生产可用 make_llm_summarizer(llm) 换成模型摘要。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Iterable

from .llm import LLMClient
from .messages import Message

# token 计数器：Message -> int
TokenCounter = Callable[[Message], int]
# 摘要器：list[Message] -> 摘要文本（可异步）
Summarizer = Callable[[list[Message]], "Awaitable[str] | str"]

FOLD_MARKER = "已折叠工具结果"


def default_token_counter(message: Message) -> int:
    """无 tiktoken 依赖的估算：**中文按字数算**，西文按 ~4 字符/词元。

    原先一律 ``len(text) // 4``——对中文偏低 4~6 倍。真机后果是整个压缩闸门形同虚设：
    1360 个中文字被算成 344 token（实际 1~1.5 token/字），101 条消息 151844 字符被算成
    38305，而真实占用已过十万。于是「超预算就压缩」永不触发，直到模型自己报
    上下文超限——表现就是「聊着聊着/剪着剪着莫名其妙断了」。

    判据：CJK（含中日韩标点、全角字符）一个字≈1 token；其余按 4 字符≈1 token。
    比装 tiktoken 更省事，且对中英混排的实际偏差在可接受范围内。
    """
    text = str(message.get("content") or "")
    if message.get("tool_calls"):
        text += json.dumps(message["tool_calls"], ensure_ascii=False)
    return estimate_tokens(text) + 4


def estimate_tokens(text: str) -> int:
    """中英混排的 token 估算：CJK 逐字计 1，其余每 4 字符计 1。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        # CJK 统一表意文字 / 扩展A / 兼容表意 / 中日韩符号与标点 / 全角
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
                or 0xF900 <= code <= 0xFAFF or 0x3000 <= code <= 0x303F
                or 0xFF00 <= code <= 0xFFEF or 0x3040 <= code <= 0x30FF
                or 0xAC00 <= code <= 0xD7AF):
            cjk += 1
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk



async def default_summarizer(dropped: list[Message]) -> str:
    """无 LLM 的确定性摘要：概述被压缩的消息，保留最早的用户诉求片段。"""
    n = len(dropped)
    first_user = next(
        (str(m.get("content") or "") for m in dropped if m.get("role") == "user"), ""
    )
    return f"（历史摘要）先前 {n} 条对话已压缩省略。早期诉求：{first_user[:80]}"


def make_llm_summarizer(llm: LLMClient, max_chars: int = 500) -> Summarizer:
    """用 LLM 对淘汰消息做全量摘要（生产用）。"""

    async def summarize(dropped: list[Message]) -> str:
        transcript = "\n".join(
            f"{m.get('role')}: {str(m.get('content') or '')[:max_chars]}" for m in dropped
        )
        resp = await llm.complete(
            [
                {
                    "role": "system",
                    "content": "把以下对话历史压缩成一段要点摘要，保留关键事实与决定：",
                },
                {"role": "user", "content": transcript},
            ]
        )
        return resp.content or await default_summarizer(dropped)

    return summarize


class ContextCompressor:
    def __init__(
        self,
        max_tokens: int,
        *,
        keep_recent_tool_results: int = 2,
        fold_preview: int = 60,
        token_counter: TokenCounter | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.keep_recent_tool_results = keep_recent_tool_results
        self.fold_preview = fold_preview
        self.count = token_counter or default_token_counter
        self.summarizer = summarizer or default_summarizer

    # ---- 估算 ----

    def estimate(self, messages: list[Message]) -> int:
        return sum(self.count(m) for m in messages)

    # ---- 第一级：折叠工具结果 ----

    def _fold_tool_info(self, body: list[Message]) -> list[Message]:
        tool_idx = [i for i, m in enumerate(body) if m.get("role") == "tool"]
        keep = self.keep_recent_tool_results
        if len(tool_idx) <= keep:
            return body
        fold_set = set(tool_idx[: len(tool_idx) - keep])
        out: list[Message] = []
        for i, m in enumerate(body):
            content = str(m.get("content") or "")
            if i in fold_set and FOLD_MARKER not in content:
                preview = content[: self.fold_preview]
                out.append(
                    {
                        **m,
                        "content": f"[{FOLD_MARKER} {m.get('name','')}：{preview}…共 {len(content)} 字符]",
                    }
                )
            else:
                out.append(m)
        return out

    # ---- 第二级：从最早的 body 消息开始淘汰，保留能塞进预算的近期消息 ----

    def _split_to_fit(
        self, system: list[Message], body: list[Message], reserve: int
    ) -> tuple[list[Message], list[Message]]:
        """返回 (dropped, kept)：从后往前尽量多保留近期消息；至少保留最后一条。"""
        running = self.estimate(system) + reserve
        split = 0  # body[split:] 保留，body[:split] 淘汰
        for i in range(len(body) - 1, -1, -1):
            cost = self.count(body[i])
            # 最后一条（当前输入）无条件保留，不触发切分
            if i < len(body) - 1 and running + cost > self.max_tokens:
                split = i + 1
                break
            running += cost
        return body[:split], body[split:]

    # ---- 组装 ----

    async def __call__(self, messages: list[Message], query: str = "") -> list[Message]:
        if not messages or self.estimate(messages) <= self.max_tokens:
            return messages  # 预算内不压缩

        if messages[0].get("role") == "system":
            system, body = [messages[0]], list(messages[1:])
        else:
            system, body = [], list(messages)

        # 第一级：折叠工具信息
        body = self._fold_tool_info(body)
        if self.estimate(system + body) <= self.max_tokens:
            return system + body

        # 预留一条摘要消息的空间，再做第二/三级
        reserve = 32
        dropped, kept = self._split_to_fit(system, body, reserve)

        if dropped:
            text = self.summarizer(dropped)
            summary = await text if _is_awaitable(text) else text
            summary_msg: Message = {
                "role": "system",
                "content": f"<history_summary>\n{summary}\n</history_summary>",
            }
            return _repair_orphans(system + [summary_msg] + kept)
        return _repair_orphans(system + kept)


def _repair_orphans(messages: list[Message],
                    keep_ids: Iterable[str] = ()) -> list[Message]:
    """清理压缩后可能出现的孤儿 tool_calls 与孤儿 tool 消息，并重排错位消息。

    压缩器淘汰旧消息时可能只淘汰了 tool 结果但保留了发起调用的 assistant，
    或反之——这会让 LLM API 报 ``tool_calls must be followed by tool messages``。
    双向修复：
      1. assistant 带 tool_calls 但无对应 tool 消息 → 移除该 tool_call
         （全部孤儿则删 ``tool_calls`` 键；若 content 也为空则整条丢弃）
      2. tool 消息但前面无对应 assistant tool_calls → 移除该 tool 消息
      3. assistant(tool_calls) 与 tool 结果之间夹了非 tool 消息（如 system）
         → 把非 tool 消息移到 tool 结果之后，恢复 API 要求的连续性

    ``keep_ids`` 是**必须保留**的 tool_call id（HITL 挂起时那批待批调用）。
    没有这个参数就会踩一个很隐蔽的坑：审批挂起点上，待批的 tool_calls **本来就没有**
    tool 结果（结果要等用户批准后才回填），于是被判成孤儿全部剥掉——批准续跑时
    assistant 已无 tool_calls，而 ``_settle_pending`` 又补了一条 tool 回执，
    变成「孤儿 tool 消息」，LLM API 直接 400，整条 run 失败。
    """
    keep = {str(i) for i in keep_ids if i}
    tool_msg_ids = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    valid_tool_ids = tool_msg_ids | keep
    assistant_call_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                assistant_call_ids.add(tc.get("id"))

    # ---- Pass 1：清理孤儿 ----
    cleaned: list[Message] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in valid_tool_ids]
            if kept:
                cleaned.append({**m, "tool_calls": kept})
            else:
                stripped = {k: v for k, v in m.items() if k != "tool_calls"}
                if stripped.get("content"):
                    cleaned.append(stripped)
        elif role == "tool":
            if m.get("tool_call_id") in (assistant_call_ids | keep):
                cleaned.append(m)
        else:
            cleaned.append(m)

    # ---- Pass 2：重排——assistant(tool_calls) 后必须紧跟 tool 消息 ----
    out: list[Message] = []
    i = 0
    while i < len(cleaned):
        m = cleaned[i]
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            needed = {tc.get("id") for tc in m["tool_calls"]}
            collected: list[Message] = []
            deferred: list[Message] = []
            j = i + 1
            while j < len(cleaned) and needed:
                nxt = cleaned[j]
                if nxt.get("role") == "tool" and nxt.get("tool_call_id") in needed:
                    needed.discard(nxt["tool_call_id"])
                    collected.append(nxt)
                else:
                    deferred.append(nxt)
                j += 1
            out.extend(collected)
            out.extend(deferred)
            i = j
        else:
            i += 1
    return out


def _is_awaitable(x: Any) -> bool:
    import asyncio

    return asyncio.iscoroutine(x) or asyncio.isfuture(x)
