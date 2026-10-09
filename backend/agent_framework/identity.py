"""当前执行身份（user_id + conversation_id）：一次 Agent run 之内有效，工具侧读取。

为什么用 contextvar 而不是工具入参：工具实例是进程级单例（一次注册、所有会话共用），
而剪辑节点查素材必须带 owner 条件（spec §5 步骤 10）——身份属于「这次是谁在跑」，
不属于「这个工具是什么」。把它塞进 LLM 可见的 schema 等于请模型自己编一个 user_id，
所以由 Agent 在 run 入口写入、MCP 包装层在发出请求前覆写，模型看不到也改不动。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    user_id: str
    conversation_id: str
    # 一次剪辑产物集的作用域。空串 = 不注入，由剪辑服务端落到缺省 '_default'。
    # 只有分叉重跑（checkpoint.fork）会显式给定：它要在一份新的产物集上重跑，
    # 又不必重做上游的切镜/ASR/画面理解。
    artifact_id: str = ""

    @property
    def session_id(self) -> str:
        return f"{self.user_id}:{self.conversation_id}"

    @property
    def storyline_session(self) -> str:
        """剪辑服务端为这次会话建的 Store 作用域键（见 ``storyline_session_id``）。"""
        return storyline_session_id(self.user_id, self.conversation_id)


def storyline_session_id(user_id: str, conversation_id: str) -> str:
    """剪辑节点服务端的会话作用域键。

    主服务与 Storyline server 必须算出同一个串，否则 fork 时复制不到正确的产物集，
    所以这个拼法只留这一处。
    """
    return f"u:{user_id}:c:{conversation_id}"


def parse_storyline_session(sid: str) -> tuple[str, str]:
    """``storyline_session_id`` 的反向：拆回 (user_id, conversation_id)。

    拆不开就回 ``("", "")``——离线直调的会话键（``sess-xxxx``）与旧格式都属于这一类，
    调用方据此判断「这不是某个用户某个对话的产物作用域」，而不是硬猜一个归属。
    """
    if sid.startswith("u:") and ":c:" in sid:
        user_id, _, conv_id = sid[2:].partition(":c:")
        if user_id and conv_id:
            return user_id, conv_id
    return "", ""


def parse_session(sid: str) -> tuple[str, str]:
    """拆**任一**拼法回 (user_id, conversation_id)，拆不开回 ``("", "")``。

    库里同一个会话有两副面孔：Agent 侧（``checkpoints`` / ``token_usage``）用
    ``Identity.session_id`` 的 ``{user}:{conv}``，剪辑侧（``artifacts`` /
    ``render_jobs`` 与对象键）用 ``u:{user}:c:{conv}``。按会话回收必须两副都认得，
    否则只会清掉一半账。
    """
    user_id, conv_id = parse_storyline_session(sid)
    if user_id:
        return user_id, conv_id
    parts = sid.split(":")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return "", ""


def session_forms(sid: str) -> list[str]:
    """一个会话在库里的全部作用域拼法（拆不出归属时原样回一条）。"""
    user_id, conv_id = parse_session(sid)
    if not user_id:
        return [sid]
    return sorted({f"{user_id}:{conv_id}", storyline_session_id(user_id, conv_id)})


_current: ContextVar[Identity | None] = ContextVar("creation_assistant_identity",
                                                   default=None)


def current_identity() -> Identity | None:
    """本协程链上生效的身份；不在任何 run 之内（离线直调、单测）返回 None。"""
    return _current.get()


def rebind_artifact_scope(artifact_id: str) -> None:
    """就地换掉本上下文链上的产物作用域（用户与会话不变）。

    用于 run 中途的分叉接管：新 run 有新的 artifact_id，此后本 turn 的节点调用都写到
    新产物集上。外层 ``use_identity*`` 退出时仍会 reset 它自己设的值，所以不会外泄。
    """
    cur = _current.get()
    if cur is not None and artifact_id and artifact_id != cur.artifact_id:
        _current.set(Identity(cur.user_id, cur.conversation_id, artifact_id))


def current_identity_or(user_id: str = "default",
                        conversation_id: str = "default") -> Identity:
    """同上，但离线直调（未进入任何 run）时退回调用方给的缺省作用域。

    协作状态（收件箱 / 任务板 / 子 Agent 登记）按此划 scope：生产里必然落在
    ``{user}:{conv}``，测试与 demo 则落在缺省作用域，互不串台。
    """
    return _current.get() or Identity(user_id, conversation_id)


@contextmanager
def use_identity(user_id: str, conversation_id: str, artifact_id: str = ""):
    """在 with 块内把身份绑定到当前执行上下文（含其 await 出去的所有层）。"""
    token = _current.set(Identity(user_id, conversation_id, artifact_id))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


@contextmanager
def use_identity_or_inherit(user_id: str, conversation_id: str, artifact_id: str = ""):
    """同上，但已在某次 run 之内时**继承**外层身份而不覆写。

    常驻子 Agent / spawn 出来的下游 Agent 有自己的会话键（各自的历史分片），
    但它们干的是同一位用户在同一会话里的活：素材 owner、收件箱与任务板作用域
    必须跟着外层，否则队友会查不到用户的素材、读不到发给它的消息。

    唯一的例外是 ``artifact_id``：外层没有它（空串）而本次显式给了（分叉重跑带着
    新产物集），则以本次为准——这不影响用户与会话作用域。
    """
    cur = _current.get()
    if cur is not None:
        if artifact_id and not cur.artifact_id:
            cur = Identity(cur.user_id, cur.conversation_id, artifact_id)
        yield cur
        return
    with use_identity(user_id, conversation_id, artifact_id) as ident:
        yield ident
