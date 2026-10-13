"""压缩摘要的持久化（R10-T47 / Q64）：复用 ``checkpoint_entries`` 这条既有通道，不新建表。

为什么复用而不是新开一张 ``context_summaries``
--------------------------------------------
这张表已经解决了这件事的全部难点：jsonb 载荷、按 ``(run_id, seq)`` 定序、
按 ``run_id`` 的索引、随会话回收（``session_gc`` 按会话清行）、以及 PG / 内存
两套引擎各自支持一遍。新开一张表等于把这五件事各抄一遍，而抄漏任何一件的表现都是
「摘要行没被回收、越攒越多」这种要过很久才被发现的病。Q64 的原话是「复用也适用于
自己的表」，指的就是这里。

**摘要没有自己的指针行**：``checkpoints`` 那一行回答的是「这次 run 走到哪了」，
摘要是「一段旧历史被压成了什么」，两者不是一回事——给摘要硬塞一条指针行，等于让
``list_unfinished`` / ``claim_recoverable`` / 设置页的 run 清单都多出一批永远不是 run
的东西。所以这里**只用** ``checkpoint_entries``，并用一个**合成 run_id** 把同一会话的
摘要行圈在一起。

合成 id 不会与真 run 撞：真 run 是 ``uuid4().hex``（32 位十六进制、无冒号），
合成 id 带 ``summary:`` 前缀。``rebuild()`` 也永远不会看到摘要行——它按 run_id 取链，
而摘要行的 run_id 只有这一个模块会生成。

key 取「被摘要那一段的指纹」而不是「第几轮」
------------------------------------------
压缩器手上只有 ``messages`` 与 ``query``（``ContextCompressor`` 协议是冻结的），
拿不到 ``head_seq``。所以命中判据落在**内容**上：同一段前缀 → 同一个 key → 直接用
存好的摘要，一个模型调用都不发。前缀变长（又淘汰了几条）时 key 变、重算一次是对的：
更长的一段本来就该有一份覆盖它的摘要，拿旧的那份当前缀才是少给了内容。

每轮重算一次摘要的成本是**一次真模型调用**，这是这条通道存在的全部理由——
省的是钱，不是优雅度。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from .messages import Message

#: 合成 run_id 的前缀。真 run 不带它，所以两类行永远分得开。
SUMMARY_PREFIX = "summary:"
#: 进 ``checkpoint_entries.kind`` 的取值（与 schema.sql 的 CHECK 逐项相等）。
SUMMARY_KIND = "summary"
#: 每个会话最多留几份摘要。窗口的切点只会往后走，所以更新的那几份覆盖的范围**包含**
#: 更旧的那几份——留最新的两三份够用，其余删掉，残留上界因此是常数。
KEEP_PER_SCOPE = 2


def summary_run_id(scope: str) -> str:
    """会话作用域 → 合成 run_id。同一个会话的摘要行全落在它下面。

    用 sha1 而不是原名：``checkpoint_entries.run_id`` 是 text，塞原始的
    ``{user}:{conv}`` 也能跑，但那样这个模块就与「会话键怎么拼」绑死了——
    而会话键在库里有两副面孔（Agent 侧 ``{user}:{conv}`` 与剪辑侧 ``u:…:c:…``，
    见 ``identity.session_forms``）。指纹只认传进来的那一个字符串，
    回收时按会话的**两种**拼法各算一次，两种都覆盖得到。
    """
    return SUMMARY_PREFIX + hashlib.sha1(str(scope).encode("utf-8")).hexdigest()[:16]


def summary_key(covered: Sequence[Message]) -> str:
    """被摘要那一段消息 → 指纹 key。内容相同则 key 相同（这就是「命中」）。

    口径逐项写死、不依赖 dict 顺序：``role`` + ``content`` + ``tool_calls``。
    ``tool_calls`` 参与是因为同一句 assistant 正文跟不同工具调用是两段不同的历史；
    别的键（``name``、``tool_call_id``）不进 key——它们在压缩后会跟着
    ``_repair_orphans`` 变，进了 key 只会让同一个前缀算出两个指纹、白丢一次命中。
    """
    parts: list[str] = []
    for m in covered:
        parts.append(str(m.get("role") or ""))
        parts.append(str(m.get("content") or ""))
        calls = m.get("tool_calls")
        if calls:
            parts.append(json.dumps(calls, ensure_ascii=False, sort_keys=True))
        parts.append("\x00")
    raw = "\x01".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


class SummaryStore:
    """某个会话名下压缩摘要的存取面（``get`` / ``put`` / ``latest``）。"""

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self._repo = storage.checkpoints

    async def get(self, scope: str, key: str) -> str | None:
        """按指纹取摘要；没有就回 None（调用方据此决定要不要真去总结）。

        同一指纹理论上只会有一条（``put`` 幂等），返的是 seq 最大的那条——
        真出现重复时，后写的那份才是这一轮该用的。
        """
        rid = summary_run_id(scope)
        for entry in reversed(await self._repo.load_entries(rid)):
            payload = entry.get("payload")
            if isinstance(payload, dict) and payload.get("key") == key:
                return str(payload.get("summary") or "") or None
        return None

    async def put(self, scope: str, key: str, summary: str, *,
                  covered: int = 0) -> None:
        """落一条摘要。同一 (会话, 指纹) 已有就**不重复写**（幂等）。

        幂等不是洁癖：会话里两条路径同时驱动同一次压缩（自动续跑 + 用户此刻发消息）
        时，不幂等就写两条一模一样的行，而 ``get`` 只能返回一条。
        """
        if not str(summary or "").strip():
            return
        rid = summary_run_id(scope)
        if await self.get(scope, key) is not None:
            return
        await self._repo.append_entry(
            rid, kind=SUMMARY_KIND,
            payload={"key": key, "summary": str(summary), "covered": int(covered),
                     "scope": str(scope)},
            iteration=0, parent_seq=None)
        await self._trim(rid)

    async def latest(self, scope: str) -> str | None:
        """该会话最新的一份摘要（历史窗口的前缀就是它）。没有就回 None。"""
        entries = await self._repo.load_entries(summary_run_id(scope))
        for entry in reversed(entries):
            payload = entry.get("payload")
            if isinstance(payload, dict) and payload.get("summary"):
                return str(payload["summary"])
        return None

    async def count(self, scope: str) -> int:
        """该会话名下现存几份摘要（账面 / 测试用，不参与运行期判断）。"""
        return len(await self._repo.load_entries(summary_run_id(scope)))

    async def _trim(self, run_id: str) -> int:
        """只留最新的 ``KEEP_PER_SCOPE`` 份：更新的摘要覆盖的范围包含更旧的。"""
        entries = await self._repo.load_entries(run_id)
        extra = len(entries) - KEEP_PER_SCOPE
        if extra <= 0:
            return 0
        cutoff = int(entries[extra]["seq"])       # 保住这一条及其之后的
        return await self._repo.drop_entries_before(run_id, cutoff)
