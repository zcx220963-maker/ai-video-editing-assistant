"""检索账：这条会话**真的查到过**哪些页面，以及「这条出处算不算查证过」。

为什么要有这张账（真机事故，2026-10-08）：免 key 检索被人机验证挡在门外之后，模型照旧
写了分镜，每镜的 ``source`` 填的是它自己脑子里的记忆。画面上、逐镜账上那些出处**看起来
像查过的**，观众与下游工具都分不出来源——「机器不判史实真伪，只如实转录出处」这条合同
因此被架空：转录的是猜的。

本模块只回答一个问题：这条出处在本次会话里被真打开过吗？
- 写入方：``tools/web.py`` 的 SearchTool / FetchTool（查到就算一条）；
- 读取方：``nodes/motion_plan.py`` 的出处闸（另一个进程，所以落 PG 而不是进程内存）。

判定标准刻意保守：宁可放行「未核实」这种诚实的自标，也不去猜一段文字是不是史实。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 模型自认「没查证」的写法。闸只认这几个字面，别的都当它声称查证过。
UNVERIFIED_MARKS = ("未核实", "未查证", "待核实", "未经核实", "凭记忆", "常识",
                    "assumed", "unverified")


def _storage_or_none() -> Any:
    from .secrets import bound_storage
    return bound_storage()


def _current_scope() -> tuple[str, str]:
    """(会话键, 用户键)；不在任何 run 之内时两个都空——账就不记，也不拦人。"""
    from .identity import current_identity
    ident = current_identity()
    if ident is None:
        return "", ""
    return ident.storyline_session, ident.user_id


async def note_hit(url: str, title: str = "", *, backend: str = "") -> None:
    """记一条「本次会话真打开过」的页面。写不进库（没绑存储/测试环境）就静默跳过。"""
    session_key, user_id = _current_scope()
    if not session_key or not url or not user_id:
        return
    storage = _storage_or_none()
    if storage is None:
        return
    try:
        await storage.retrieval.note(session_key, user_id, url, title=title,
                                     backend=backend)
    except Exception:  # noqa: BLE001 - 记账失败不能把检索本身弄挂
        pass


async def note_hits(results: Iterable[Mapping[str, Any]], *, backend: str = "") -> None:
    for r in results or []:
        url = str(r.get("url") or "").strip()
        if url:
            await note_hit(url, str(r.get("title") or ""), backend=backend)


async def ledger(session_key: str = "") -> list[dict[str, Any]] | None:
    """本会话查过的页面清单（``[{url, title, backend, checked_at}]``），空账回 ``[]``。

    返回 **None** 表示「根本没有账本可查」（存储没绑定、或读库失败）。闸在这种情形
    必须放行：没地方记命中，就不该拿「没记过」去判模型谎报——那会退化成
    「所有出处一律打回」，比原来更糟。
    """
    key = session_key or _current_scope()[0]
    storage = _storage_or_none()
    if storage is None or not key:
        return None
    try:
        return await storage.retrieval.ledger(key)
    except Exception:  # noqa: BLE001
        return None


def cite_fields(source: Any) -> tuple[str, str]:
    """从 spec 的 source 里取 (标题, 链接)。中英文键都收——契约写的是「标题/链接」。"""
    if isinstance(source, str):
        return source.strip(), ""
    if not isinstance(source, Mapping):
        return "", ""
    title = ""
    url = ""
    for k, v in source.items():
        name = str(k).strip().lower()
        text = str(v).strip() if v is not None else ""
        if not text:
            continue
        if name in ("title", "标题", "来源", "source") and not title:
            title = text
        elif name in ("url", "链接", "link", "地址", "href") and not url:
            url = text
    return title, url


def claims_verified(source: Any) -> bool:
    """这条出处是不是在**声称**「我查证过」。留空或自带未核实标记的不算。"""
    title, url = cite_fields(source)
    text = f"{title} {url}".strip().lower()
    if not text:
        return False
    return not any(mark in text for mark in UNVERIFIED_MARKS)


def _same_url(a: str, b: str) -> bool:
    norm = lambda s: (s or "").strip().rstrip("/").lower().removeprefix("https://").removeprefix("http://")  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def matches_ledger(source: Any, rows: list[dict[str, Any]]) -> bool:
    """这条出处能不能在账上指到某一次真检索：链接对上，或标题对上账里某条标题。"""
    title, url = cite_fields(source)
    for row in rows or []:
        if url and _same_url(url, str(row.get("url") or "")):
            return True
    if title:
        low = title.lower()
        for row in rows or []:
            hit = str(row.get("title") or "").lower()
            if hit and (hit in low or low in hit):
                return True
    return False


__all__ = ["UNVERIFIED_MARKS", "cite_fields", "claims_verified", "ledger", "matches_ledger",
           "note_hit", "note_hits"]
