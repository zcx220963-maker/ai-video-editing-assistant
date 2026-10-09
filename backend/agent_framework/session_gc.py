"""按会话回收：对话没了，它名下的产物、缓存与账要跟着走；素材库登记过的字节留下。

为什么需要这一层：``artifacts`` / ``render_jobs`` / ``checkpoints`` / ``token_usage``
都只挂 ``session_id``，桶里的对象键也以会话为前缀（``renders/{会话}/``、
``motion-shots/{会话}/``、``render-windows/{会话}/``），而 ``DELETE /convs/{id}`` 过去
只删 ``messages`` 与 ``conversations`` 两行——于是每条被删掉的对话都在库里留下永远
没人再来清的行和字节。

两条调用路径共用这一份口径：
1. 删对话时即时回收（``purge_conversation``）；
2. 启动时对账扫孤儿（``sweep_orphans``）——既兜「库里有行、对话已不在」的历史遗留，
   也兜「字节落了桶、行却没落」的崩溃残留（成片先 publish、artifacts 行后写，中间
   进程没了就只剩字节）。

保护名单是这张表上唯一的例外：``materials.object_key`` 指向的字节不删。素材是用户级
资产（``conv_id`` 外键在对话删除时置 NULL 而不是级联删），成片入库后也走同一条规则，
所以「删对话」清掉的是**工作区**，不是**用户已经拥有的东西**。
"""

from __future__ import annotations

from typing import Any

from .identity import parse_session, session_forms, storyline_session_id
from .ingest import safe_segment
from .orchestration import _safe
from .storage import Cond

#: 只认 session_id 的账本表（删对话要一并清掉的行）
SESSION_TABLES = ("artifacts", "render_jobs", "checkpoints", "token_usage")
#: 会话前缀下的对象目录：成片与逐帧/命中表（renders/）、节点自产中间件（derived/）、
#: 逐镜切片缓存（motion-shots/）、逐窗切片缓存（render-windows/）。
#: 素材字节在 users/{用户}/convs/{对话}/ 下，归用户且随素材行留存，这里不列它。
OBJECT_PREFIXES = ("renders/", "derived/", "motion-shots/", "render-windows/")


async def _run_ids_of(storage: Any, forms: list[str]) -> list[str]:
    """该会话名下的所有 run：删 checkpoints 行之前先取，之后就查不到了。

    只取 run_id 这一列（下推聚合）：checkpoints 一行装着 plan/approval 两个 jsonb，
    为拿几个 id 把正文读回来，等于把这次回收最贵的一次 IO 花在记账上。
    """
    runs: set[str] = set()
    for table in ("checkpoints", "token_usage"):
        for rid in await storage.db.distinct_values(
                table, "run_id", where=[Cond("session_id", "in", forms)]):
            if rid:
                runs.add(str(rid))
    return sorted(runs)


async def _protected_keys(storage: Any) -> set[str]:
    """素材库指向的对象键：它们归用户，不随会话回收。"""
    return {str(k) for k in await storage.db.distinct_values("materials", "object_key")}


async def _drop_objects(storage: Any, prefixes: list[str], *,
                        out: dict[str, Any]) -> None:
    """逐前缀删字节：保护名单里的留下并计数，删失败的记账但不中断回收。"""
    keep = await _protected_keys(storage)
    for prefix in prefixes:
        for info in await storage.objects.list_prefix(prefix):
            if info.key in keep:
                out["kept"] += 1                # 已入库的成片：字节归用户，留着
                continue
            try:
                await storage.objects.delete(info.key)
                out["objects"] += 1
            except Exception as exc:            # noqa: BLE001 - 字节删不掉不该回滚账
                out["failed"].append(f"{info.key}：{type(exc).__name__}")


def _scope_prefixes(scope: str) -> list[str]:
    """会话作用域下的对象前缀（一律带尾部斜杠）。

    不带斜杠就会把 ``sess-1`` 误配到 ``sess-12`` 头上；作用域段就是写入时那一次净化
    （``_safe`` 与 workspace 的 ``_seg`` 同一正则），所以两侧天然对得上。
    """
    return [f"{p}{scope}/" for p in OBJECT_PREFIXES]


async def purge_session(storage: Any, session_id: str, *,
                        reason: str = "") -> dict[str, Any]:
    """清掉一个会话名下的行与对象，回一份可对账的账（删了几行、几个对象、护住几个）。

    传入任一种拼法都行：``session_forms`` 会把它展开成这个会话在库里的两副面孔
    （Agent 侧的 ``{user}:{conv}`` 与剪辑侧的 ``u:{user}:c:{conv}``），只按一种删
    就是永远只清掉一半账。

    在途渲染优先于回收：``queued``/``running`` 的行还在被后台线程写，此刻删字节会让
    那次渲染跑完时只剩一张空账——所以直接跳过并说明，由调用方决定要不要重试。
    """
    forms = session_forms(session_id)
    out: dict[str, Any] = {"session_id": session_id, "session_ids": forms,
                           "reason": reason,
                           "rows": {}, "objects": 0, "kept": 0, "failed": []}
    busy = await storage.db.select(
        "render_jobs", where=[Cond("session_id", "in", forms),
                              Cond("status", "in", ["queued", "running"])], limit=1)
    if busy:
        out["skipped"] = "该会话有渲染在途（queued/running），本次不回收"
        return out

    runs = await _run_ids_of(storage, forms)
    for table in SESSION_TABLES:
        out["rows"][table] = await storage.db.delete(
            table, where=[Cond("session_id", "in", forms)])
    if runs:
        out["rows"]["checkpoint_entries"] = await storage.db.delete(
            "checkpoint_entries", where=[Cond("run_id", "in", runs)])

    # 协作状态（任务板 + 子 Agent 登记）按 scope={user}:{conv} 划会话，对话没了它们
    # 就没有意义。依赖边在 PG 由外键级联带走，但内存引擎不认外键，所以按 id 显式删。
    task_ids = [r["id"] for r in await storage.db.select(
        "tasks", where=[Cond("scope", "in", forms)])]
    if task_ids:
        out["rows"]["task_edges"] = await storage.db.delete(
            "task_edges", where=[Cond("task_id", "in", task_ids)])
    out["rows"]["tasks"] = await storage.db.delete(
        "tasks", where=[Cond("scope", "in", forms)])
    out["rows"]["subagents"] = await storage.db.delete(
        "subagents", where=[Cond("scope", "in", forms)])

    prefixes = [p for form in forms for p in _scope_prefixes(_safe(form))]
    user_id, conv_id = parse_session(session_id)
    if conv_id:
        # 分片字节按「谁的哪个对话的哪次上传」分格。正常收片与作废都会在完成时删片，
        # 但 upload_sessions 行随对话 CASCADE 消失后，半途删掉的那次上传就没人认领了。
        prefixes.append(f"uploads/{safe_segment(user_id, 'u')}/convs/"
                        f"{safe_segment(conv_id, 'c')}/")
    await _drop_objects(storage, prefixes, out=out)
    return out


async def purge_conversation(storage: Any, user_id: str,
                             conversation_id: str) -> dict[str, Any]:
    """删对话的那一侧：把会话键拼法收在 identity 里，别让调用方自己拼串。"""
    return await purge_session(storage, storyline_session_id(user_id, conversation_id),
                               reason="对话已删除")


async def orphan_sessions(storage: Any) -> list[str]:
    """库里有行、但对应对话已经不在了的会话键（升序）。

    拆不出对话 id 的会话键（离线直调的 ``sess-*``）也算孤儿：那种形态本来就不可能
    有 ``conversations`` 行，留着就是纯垃圾。
    """
    conv_ids = {str(r["id"]) for r in await storage.db.select("conversations")}
    found: set[str] = set()
    for table in SESSION_TABLES:
        for sid in await storage.db.distinct_values(table, "session_id"):
            sid = str(sid)
            _, conv_id = parse_session(sid)
            if conv_id and conv_id in conv_ids:
                continue
            found.add(sid)
    return sorted(found)


async def orphan_scopes(storage: Any) -> list[str]:
    """桶里有**未登记**字节、但对应对话已经不在了的作用域段（升序，净化后的形态）。

    这一路兜的是「字节落了库、行却没落」的那种崩法：成片先 publish 进桶、artifacts 行
    随后才写，中间进程没了就只剩字节——只按库里的行扫永远看不到它。

    「只剩素材库登记过的字节」的作用域要排除在外：删对话后成片持久化是正常终态，
    那版字节归素材行管（删素材时由素材出口删）。把它列成孤儿，等于每次开机都重扫
    一个永远删不动的作用域——对账就不幂等了。
    """
    live: set[str] = set()
    for r in await storage.db.select("conversations"):
        for form in session_forms(
                storyline_session_id(str(r["user_id"]), str(r["id"]))):
            live.add(_safe(form))
    keep = await _protected_keys(storage)
    groups: dict[str, list[str]] = {}
    for prefix in OBJECT_PREFIXES:
        for info in await storage.objects.list_prefix(prefix):
            parts = info.key.split("/")
            if len(parts) >= 3 and parts[1] not in live:
                groups.setdefault(parts[1], []).append(info.key)
    return sorted(scope for scope, keys in groups.items()
                  if any(k not in keep for k in keys))


async def purge_scope(storage: Any, scope: str, *,
                      reason: str = "") -> dict[str, Any]:
    """只清字节：这个作用域在库里已经没有行（或有行的那条路已经处理过）。"""
    out: dict[str, Any] = {"scope": scope, "reason": reason,
                           "rows": {}, "objects": 0, "kept": 0, "failed": []}
    await _drop_objects(storage, _scope_prefixes(scope), out=out)
    return out


async def sweep_orphans(storage: Any) -> list[dict[str, Any]]:
    """启动对账：先按会话回收有行的孤儿，再清「只有字节、没有行」的孤儿作用域。

    ``orphan_sessions`` 是按行里的 session_id 列的，同一个会话的两副面孔各占一行，
    而 ``purge_session`` 一次就把两种拼法都清了——所以按拼法集合去重，同一会话只回收
    一次，账才不会把护住的字节重复计数。
    """
    out: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()
    for sid in await orphan_sessions(storage):
        forms = frozenset(session_forms(sid))
        if forms in seen:
            continue
        seen.add(forms)
        out.append(await purge_session(storage, sid, reason="启动对账：对话已不存在"))
    done = {_safe(form) for entry in out for form in entry.get("session_ids", [])}
    for scope in await orphan_scopes(storage):
        if scope in done:
            continue
        out.append(await purge_scope(storage, scope,
                                     reason="启动对账：桶里的字节没有对应对话"))
    return out
