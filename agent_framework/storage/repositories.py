"""表级仓储层：原来 13 处「读文件/写文件」的语义，逐个搬到 PG 接口上。

每个 repo 只做两件事：把业务动作翻译成 Datastore 调用、把权限边界（owner / scope）
钉死在查询条件里。引擎无关——`pg_minio` 与 `memory` 两个后端跑的是同一份代码，
所以契约测试能在离线跑通全部语义，联机再跑一遍真的。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..media_replay import drain_rendered_media, reset_rendered_media
from ..plan_replay import drain_plan_round, reset_plan_cards
from .db import Cond, Datastore, IntegrityConflict, in_, is_null, le, not_null


def new_id(prefix: str, n: int = 8) -> str:
    return f"{prefix}-{secrets.token_hex(n // 2)}"


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _grams(text: str) -> set[str]:
    """中文按 2-gram 拆：\\W+ 分词会把整句汉字当一个词，匹配不了曲名/素材名。"""
    return {text[i:i + 2] for i in range(len(text) - 1) if text[i:i + 2].strip()}


def rank_by_request(rows: Sequence[Mapping[str, Any]], request: str,
                    field: str = "filename") -> list[Mapping[str, Any]]:
    """按「请求文本命中文件名的 2-gram 数」降序；同分保持传入顺序（稳定）。"""
    grams = _grams(request or "")
    if not grams:
        return list(rows)
    scored = [(-sum(1 for g in grams if g in (r.get(field) or "")), i, r)
              for i, r in enumerate(rows)]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in scored]


class _Repo:
    table = ""

    def __init__(self, db: Datastore) -> None:
        self.db = db


# --------------------------------------------------------------------------
# 身份与会话
# --------------------------------------------------------------------------


class UsersRepo(_Repo):
    table = "users"

    async def register(self, device_name: str = "") -> tuple[str, str]:
        """签发新身份：返回 (user_id, 明文 token)。明文只在这里出现一次，库里只有哈希。

        device_name 是 spec §7 的请求字段，§3.1 的 users 表没有对应列 —— 收下但不落盘，
        不做「顺手加一列」的越权改动。
        """
        uid, tok = new_id("u", 12), secrets.token_urlsafe(32)
        await self.db.insert(self.table, {"id": uid, "token_hash": token_hash(tok)})
        return uid, tok

    async def verify(self, plain_token: str) -> str | None:
        """明文 token → user_id；不匹配返回 None（不区分「用户不存在」与「token 错」）。"""
        if not plain_token:
            return None
        rows = await self.db.select(self.table, where={"token_hash": token_hash(plain_token)},
                                    limit=1)
        return rows[0]["id"] if rows else None

    async def get(self, user_id: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"id": user_id})

    async def provision(self, user_id: str) -> str:
        """登记一个**代码写死的内部身份**（定时任务、缺省作用域），幂等。

        与 ``register`` 的差别只在来路：这里的 id 不来自客户端传值，补的行带一个随机
        token_hash——不对应任何可用凭证，所以「身份存在」不等于「可登录」。业务写入路径
        一律不补行（spec §7：user_id 只由 token 反查），来路不明的 id 会被
        conversations/materials/memories 的外键直接拒绝，而不是自我加冕。
        """
        if not user_id:
            raise IntegrityConflict("user_id 为空，无法登记身份")
        if await self.get(user_id) is None:
            await self.db.insert(self.table, {
                "id": user_id, "token_hash": token_hash(new_id("unclaimed", 24))})
        return user_id


class ConversationsRepo(_Repo):
    table = "conversations"

    async def ensure(self, user_id: str, conv_id: str, title: str | None = None) -> dict[str, Any]:
        """保证会话行存在并归属该用户。title 只在显式传入时改标题——
        写入方每轮都 ensure，默认标题会把用户改过的名字冲掉。
        """
        got = await self.db.get_by_pk(self.table, {"id": conv_id})
        if got and got["user_id"] != user_id:
            raise IntegrityConflict(f"会话 {conv_id} 不属于该用户")
        upd = {"updated_at": _now()}
        if title:
            upd["title"] = title
        return await self.db.upsert(
            self.table,
            {"id": conv_id, "user_id": user_id, "title": title or "新对话"},
            update_values=upd if got else None,
        )

    async def get(self, conv_id: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"id": conv_id})

    async def claim(self, user_id: str, conv_id: str) -> None:
        """这个会话 id 能不能归你：空闲或已属你 → 通过；落在别人名下 → 抛错。

        只读不建行——真正创建仍由写入方（Agent / ingest）在落第一行时做。
        """
        got = await self.get(conv_id)
        if got and got["user_id"] != user_id:
            raise IntegrityConflict(f"会话 {conv_id} 已属于其他用户")

    async def list_for(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self.db.select(self.table, where={"user_id": user_id},
                                    order_by=["-updated_at"], limit=limit)

    async def touch(self, user_id: str, conv_id: str) -> None:
        await self.db.update(self.table, {"updated_at": _now()},
                             where={"id": conv_id, "user_id": user_id})

    async def drop(self, user_id: str, conv_id: str) -> int:
        return await self.db.delete(self.table, where={"id": conv_id, "user_id": user_id})


def _merge_render_media(qa: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """把本轮挂起的成片链接并入 assistant 的 qa.parts（新增 type=media 片段）。

    无挂起链接时原样返回 qa，保证普通消息零改动。存的是渲染对象键（稳定指针），
    presigned 直链在 /convs/{id}/messages 读取时现签，与附件展示同构。
    """
    cards = drain_rendered_media()
    if not cards:
        return qa
    merged = dict(qa or {})
    parts = list(merged.get("parts") or [])
    parts.extend({"type": "media", **card} for card in cards)
    merged["parts"] = parts
    return merged


def _merge_plan_cards(qa: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """把本轮挂起的候选计划并入 assistant 的 qa.parts（新增 type=plan 片段）。

    计划卡在「用户还没点确认」时就必须能刷新重放，否则刷新等于丢掉这一步的入口：
    存的是 ``submit_plan`` 当轮通过四重校验的那一份（与指针行 ``plan.candidates`` 同源），
    前端按同一形状渲染，不需要第二套数据。
    """
    cards, warnings = drain_plan_round()
    if not cards:
        return qa
    merged = dict(qa or {})
    parts = list(merged.get("parts") or [])
    parts.append({"type": "plan",
                  "plan_run_id": cards[0].get("plan_run_id"),
                  "plans": cards,
                  "warnings": warnings})
    merged["parts"] = parts
    return merged


class MessagesRepo(_Repo):
    table = "messages"

    async def append(self, user_id: str, conv_id: str, role: str, content: str = "",
                     attachments: Sequence[str] = (), qa: Mapping[str, Any] | None = None,
                     ) -> dict[str, Any]:
        """追加一条消息。seq 在会话内单调，取代 .jsonl 的行序。

        成片持久链接在这里落库：本轮 MediaCardHook 检出的成功渲染（README §6 的缺口）以
        contextvar 传到这里，assistant 行把它的对象键并入 ``qa.parts`` 的 ``media`` 片段，
        于是刷新后 /convs/{id}/messages 能重放出播放卡；user 行开始新一轮时清掉残留。
        计划卡走同一条通道（``type=plan`` 片段）：待确认的那一份也重得起来。
        """
        if role == "user":
            reset_rendered_media()
            reset_plan_cards()
        elif role == "assistant":
            qa = _merge_plan_cards(_merge_render_media(qa))
        # 归属校验放在取号之前：越权就别浪费一次 seq 计算，也别让失败路径留下空洞。
        if await self._owner(conv_id) != user_id:
            raise IntegrityConflict(f"会话 {conv_id} 不属于用户 {user_id}")
        # seq 是 (conv_id, seq) 唯一键的一半，「先读 max 再插」在并发下会撞号
        # （同一会话两条消息同时落库）。撞了就重读 max 再来——追加语义本身幂等，
        # 重试不会写重复内容。
        last_err: Exception | None = None
        for _ in range(5):
            top = await self.db.max_value(self.table, "seq", where={"conv_id": conv_id})
            row = {"conv_id": conv_id, "seq": (top or 0) + 1, "role": role,
                   "content": content, "attachments": list(attachments), "qa": qa}
            try:
                return await self.db.insert(self.table, row)
            except IntegrityConflict as exc:
                last_err = exc
                continue
        raise IntegrityConflict(
            f"消息 seq 连续 5 次撞号（会话 {conv_id}）：{last_err}")

    async def history(self, user_id: str, conv_id: str,
                      limit: int | None = None) -> list[dict[str, Any]]:
        if await self._owner(conv_id) != user_id:
            return []
        return await self.db.select(self.table, where={"conv_id": conv_id},
                                    order_by=["seq"], limit=limit)

    async def set_qa(self, message_id: int, qa: Mapping[str, Any]) -> None:
        await self.db.update(self.table, {"qa": dict(qa)}, where={"id": message_id})

    async def _owner(self, conv_id: str) -> str | None:
        conv = await self.db.get_by_pk("conversations", {"id": conv_id})
        return conv["user_id"] if conv else None


class MaterialsRepo(_Repo):
    table = "materials"

    async def register(self, owner_user_id: str, conv_id: str | None, object_key: str,
                       filename: str, kind: str, *, bytes_: int, sha256: str,
                       mime: str = "", duration_sec: float | None = None,
                       width: int | None = None, height: int | None = None,
                       has_audio: bool = False, origin: str = "upload",
                       material_id: str | None = None) -> dict[str, Any]:
        """登记一份素材。material_id 可预分配：对象键里含它，上传前就得定下 id。"""
        return await self.db.insert(self.table, {
            "id": material_id or new_id("mat", 6), "owner_user_id": owner_user_id,
            "conv_id": conv_id,
            "object_key": object_key, "filename": filename, "kind": kind, "mime": mime,
            "bytes": bytes_, "sha256": sha256, "duration_sec": duration_sec,
            "width": width, "height": height, "has_audio": has_audio, "origin": origin})

    async def get(self, material_id: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"id": material_id})

    async def _visible(self, row: Mapping[str, Any], user_id: str,
                       conv_id: str | None) -> bool:
        """可见性判定：本人所有，或落在**本人拥有的**会话里。

        只比 `row.conv_id == 传入 conv_id` 是不够的——conv_id 由调用方自报，
        任何人都能报别人的会话 id。所以会话内共享必须先确认会话归属调用者。

        这是**单行**判据，会按需发一次会话查询。批处理场景请走
        ``list_visible``（它预先一次性算好归属，不逐行查库）。
        """
        if row["owner_user_id"] == user_id:
            return True
        if not conv_id or row["conv_id"] != conv_id:
            return False
        return await self._owns_conv(conv_id, user_id)

    async def _owns_conv(self, conv_id: str, user_id: str) -> bool:
        conv = await self.db.get_by_pk("conversations", {"id": conv_id})
        return bool(conv and conv["user_id"] == user_id)

    @staticmethod
    def _visible_with(row: Mapping[str, Any], user_id: str, conv_id: str | None,
                      owns_conv: bool) -> bool:
        """不带 I/O 的可见性判定：``owns_conv`` 由调用方预先算好。

        判据与 ``_visible`` 完全一致（本人所有 → 可见；落在本人拥有的会话里 → 可见），
        只是把「这个会话是不是我的」提前到循环外算一次。
        """
        if row["owner_user_id"] == user_id:
            return True
        if not conv_id or row["conv_id"] != conv_id:
            return False
        return owns_conv

    async def resolve(self, material_ids: Sequence[str], *, user_id: str,
                      conv_id: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """把 id 解析成可见素材：返回 (命中, 被拒的 id)。越权 id 不报错，只跳过并回报。"""
        rows = await self.db.select(self.table, where={"id": in_(list(material_ids))})
        # 会话归属只可能涉及一个 conv_id，循环外查一次即可（原来是逐行查 → N+1）
        owns = bool(conv_id) and await self._owns_conv(str(conv_id), user_id)
        ok = [r for r in rows
              if r.get("origin") == "bgm"
              or self._visible_with(r, user_id, conv_id, owns)]
        kept = {r["id"] for r in ok}
        return ok, [m for m in material_ids if m not in kept]

    async def list_visible(self, user_id: str, conv_id: str | None = None, *,
                           origin: str | None = None,
                           kinds: Sequence[str] = ()) -> list[dict[str, Any]]:
        """本人可见的素材。``origin='bgm'`` 直接回全量（曲库是共享资源）。

        可见性过滤在**内存里一次做完**，但会话归属只查一次：
        原先逐行 ``await self._visible(...)``，每行撞到「不是本人所有」就再发一次
        conversations 查询——素材越多查询越多（N+1），而这是 ``search_media``
        （剪辑链第一步）与 ``/materials``、``/bgm`` 共同的入口。
        """
        where: dict[str, Any] = {}
        if origin:
            where["origin"] = origin
        if kinds:
            where["kind"] = in_(list(kinds))
        rows = await self.db.select(self.table, where=where, order_by=["-created_at"])
        if origin == "bgm":
            return list(rows)
        owns = bool(conv_id) and await self._owns_conv(str(conv_id), user_id)
        return [r for r in rows if self._visible_with(r, user_id, conv_id, owns)]

    async def search(self, user_id: str, query: str, *, conv_id: str | None = None,
                     kinds: Sequence[str] = ("video", "audio"), origin: str | None = None,
                     limit: int = 20) -> list[dict[str, Any]]:
        """本地素材库检索（原 rglob 扫盘的替身）：可见性过滤 + 2-gram 文件名排序。"""
        pool = await self.list_visible(user_id, conv_id, origin=origin, kinds=kinds)
        if query:
            q = query.lower()
            pool = [r for r in pool if q in (r["filename"] or "").lower()
                    or _grams(q) & _grams((r["filename"] or "").lower())]
        return rank_by_request(pool, query)[:limit]

    async def drop(self, user_id: str, material_id: str) -> int:
        return await self.db.delete(self.table, where={"id": material_id,
                                                       "owner_user_id": user_id})


class UploadSessionsRepo(_Repo):
    """分片续传的账本行：只记「打算怎么切」，不记「收到哪了」。

    进度不写这张表是刻意的：分片就是对象存储里的对象，「已有哪几片」问一次
    ``list_prefix`` 就有唯一答案。把进度也记进表里，就有了两处真相——某个副本收到
    半片崩了，表里那一行会谎报一个桶里不存在的进度，客户端照着它跳过重传，最后拼出
    一条坏素材。
    """

    table = "upload_sessions"

    async def open(self, owner_user_id: str, conv_id: str, filename: str,
                   total_bytes: int, part_size: int, part_count: int,
                   sha256: str = "") -> dict[str, Any]:
        return await self.db.insert(self.table, {
            "id": new_id("up", 8), "owner_user_id": owner_user_id, "conv_id": conv_id,
            "filename": filename, "total_bytes": total_bytes, "part_size": part_size,
            "part_count": part_count, "sha256": sha256})

    async def find_live(self, owner_user_id: str, conv_id: str, filename: str,
                        total_bytes: int, part_size: int, sha256: str) -> dict[str, Any] | None:
        """同一人、同一会话、同名同切法同指纹且还在传 → 复用这条，别另起一份账。

        刷新页面与断网重连都会重发一次 init，不复用就得把已传的分片丢在桶里等扫。
        """
        rows = await self.db.select(self.table, where={
            "owner_user_id": owner_user_id, "conv_id": conv_id, "filename": filename,
            "total_bytes": total_bytes, "part_size": part_size, "sha256": sha256,
            "status": "uploading"}, order_by=["-created_at"], limit=1)
        return rows[0] if rows else None

    async def get(self, upload_id: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"id": upload_id})

    async def touch(self, upload_id: str) -> None:
        """每收一片推一次 updated_at：几十分钟的大文件不该在半路被过期清扫收走。"""
        await self.db.update(self.table, {"updated_at": _now()}, where={"id": upload_id})

    async def finalize(self, upload_id: str, material_id: str) -> None:
        await self.db.update(self.table, {"status": "completed", "material_id": material_id,
                                          "updated_at": _now()}, where={"id": upload_id})

    async def drop(self, upload_id: str) -> int:
        return await self.db.delete(self.table, where={"id": upload_id})

    async def expired(self, max_age_sec: float) -> list[dict[str, Any]]:
        """久没动静的会话（含已完成的）：交给服务层清掉 uploads/ 前缀下的字节与本行。

        completed 也一起扫：拼装成功后分片本就作废，但若那次删除半途失败（MinIO 抖
        一下）就没有别的场合会再碰它——没有兜底就会永久漏一片孤儿字节。
        """
        cutoff = _now() - timedelta(seconds=max_age_sec)
        return await self.db.select(self.table, where=[Cond("updated_at", "lt", cutoff)],
                                    order_by=["updated_at"])


# --------------------------------------------------------------------------
# 剪辑产物与渲染
# --------------------------------------------------------------------------


class ArtifactsRepo(_Repo):
    """FileStore 的替身：{会话}/{节点}/{产物}.json 目录树 → 一张表。"""

    table = "artifacts"

    def __init__(self, db: Datastore, session_id: str, artifact_id: str = "") -> None:
        super().__init__(db)
        self.session_id = session_id
        self.artifact_id = artifact_id or "_default"

    def _key(self, node: str) -> dict[str, str]:
        return {"session_id": self.session_id, "node": node, "artifact_id": self.artifact_id}

    async def put(self, node: str, payload: Any) -> dict[str, Any]:
        return await self.db.upsert(self.table, {**self._key(node), "payload": payload,
                                                 "updated_at": _now()})

    async def get(self, node: str, default: Any = None) -> Any:
        row = await self.db.get_by_pk(self.table, self._key(node))
        return row["payload"] if row else default

    async def has(self, node: str) -> bool:
        return await self.db.get_by_pk(self.table, self._key(node)) is not None

    async def executed(self) -> list[str]:
        return [r["node"] for r in await self.db.select(
            self.table, where={"session_id": self.session_id,
                               "artifact_id": self.artifact_id}, order_by=["node"])]

    async def snapshot(self) -> dict[str, Any]:
        rows = await self.db.select(self.table, where={"session_id": self.session_id,
                                                       "artifact_id": self.artifact_id})
        return {r["node"]: r["payload"] for r in rows}

    async def meta(self, node: str) -> dict[str, Any] | None:
        row = await self.db.get_by_pk(self.table, self._key(node))
        if row is None:
            return None
        return {k: v for k, v in row.items() if k != "payload"}

    async def rows(self) -> list[dict[str, Any]]:
        """本作用域的完整产物行（含 payload），供 fork 整份复制。"""
        return await self.db.select(self.table, where={"session_id": self.session_id,
                                                       "artifact_id": self.artifact_id},
                                    order_by=["node"])

    async def clone_from(self, src: "ArtifactsRepo") -> list[str]:
        """把 ``src`` 作用域的产物逐节点复制进本作用域，返回复制到的节点名。

        fork 的复用就靠这一步：新产物集起手就是父 run 的全部上游产物，
        拦截器看到 ``store.has(dep)`` 命中，于是只重跑分叉点之后的节点。
        """
        copied: list[str] = []
        for row in await src.rows():
            await self.db.upsert(self.table, {
                **self._key(row["node"]), "payload": row["payload"], "updated_at": _now()})
            copied.append(row["node"])
        return copied

    async def delete_nodes(self, nodes: Iterable[str]) -> int:
        """删掉本作用域里这些节点的产物行。

        「从 plan_timeline 重跑」必须把它**自己**和下游的产物一起作废：只换作用域不作废，
        拦截器会照样判定上游已满足，于是用户要求重做的那步被跳过。
        """
        names = [n for n in nodes]
        if not names:
            return 0
        return await self.db.delete(self.table, where={
            "session_id": self.session_id, "artifact_id": self.artifact_id,
            "node": in_(names)})


class RenderJobsRepo(_Repo):
    table = "render_jobs"

    async def enqueue(self, session_id: str, artifact_id: str) -> dict[str, Any]:
        """提交瞬间的占位行：让轮询端立刻查得到东西，且不把正在跑的那次复位掉。

        已有非终态行（queued/running）时原样返回——重复提交同一产物不该重开渲染；
        终态行（done/failed）才复位成 queued，因为那是「同一产物再渲一次」。

        复位时**换一个 attempt 令牌**：旧那次尝试的线程可能还活着（MoviePy 跑在
        ``to_thread`` 里，Python 杀不掉），不换令牌它跑完就会把新一次的状态改写成
        done，出现「failed → 过一会儿又 done」的翻转。
        """
        existing = await self.get(session_id, artifact_id)
        if existing:
            if existing["status"] in ("queued", "running"):
                return existing
            await self.db.update(self.table, {
                "status": "queued", "stage": "queued", "percent": 0, "error": None,
                "video_object_key": None, "duration_sec": None, "result": None,
                "attempt": new_id("at", 8),
                "updated_at": _now()}, where={"id": existing["id"]})
            return await self.db.get_by_pk(self.table, {"id": existing["id"]})
        return await self.db.insert(self.table, {
            "id": new_id("rj", 8), "session_id": session_id, "artifact_id": artifact_id,
            "status": "queued", "stage": "queued", "percent": 0,
            "attempt": new_id("at", 8)})

    async def open(self, session_id: str, artifact_id: str) -> dict[str, Any]:
        """开一次渲染：同一 (会话, 产物) 复跑时复位原行，而不是插第二条。

        每次 open 都换 attempt 令牌并把新令牌回给调用方：渲染体拿它写进度与终态，
        只有令牌仍与行上一致才写得进去（见 ``_fenced``）。这样「被看门狗判死后
        又起了新一次」与「旧线程姗姗来迟」不会互相覆盖状态。
        """
        existing = await self.get(session_id, artifact_id)
        token = new_id("at", 8)
        reset = {"status": "running", "stage": "", "percent": 0, "error": None,
                 "video_object_key": None, "duration_sec": None, "result": None,
                 "attempt": token, "updated_at": _now()}
        if existing:
            await self.db.update(self.table, reset, where={"id": existing["id"]})
            return await self.db.get_by_pk(self.table, {"id": existing["id"]})
        return await self.db.insert(self.table, {
            "id": new_id("rj", 8), "session_id": session_id, "artifact_id": artifact_id,
            **reset})

    async def _fenced(self, session_id: str, artifact_id: str,
                      values: Mapping[str, Any], attempt: str | None) -> bool:
        """带令牌的写：令牌不匹配说明这行已属于另一次尝试，一个字都不写。

        令牌为 None 时不做围栏（保持直写场景的原语义）。
        """
        if attempt:
            row = await self.get(session_id, artifact_id)
            if row is None or str(row.get("attempt") or "") != str(attempt):
                return False
        await self.db.update(self.table, {**values, "updated_at": _now()},
                             where={"session_id": session_id, "artifact_id": artifact_id})
        return True

    async def progress(self, session_id: str, artifact_id: str, stage: str,
                       percent: int, attempt: str | None = None) -> None:
        await self._fenced(session_id, artifact_id,
                           {"stage": stage, "percent": percent}, attempt)

    async def touch(self, session_id: str, artifact_id: str) -> None:
        """只续期不改口径：看门狗据此把「还在排队等槽」与「活儿死了」区分开。

        这条**不带令牌**：续期表示「这行还有人认领」，与是哪一次尝试无关。
        """
        await self.db.update(self.table, {"updated_at": _now()},
                             where={"session_id": session_id, "artifact_id": artifact_id})

    async def succeed(self, session_id: str, artifact_id: str, video_object_key: str,
                      duration_sec: float | None,
                      result: Mapping[str, Any] | None = None,
                      attempt: str | None = None) -> None:
        """置 done 并把终态产物整份存下：轮询端据此重建结果，不必回头再算。"""
        await self._fenced(session_id, artifact_id, {
            "status": "done", "stage": "done", "percent": 100,
            "video_object_key": video_object_key,
            "duration_sec": duration_sec, "error": None,
            "result": dict(result or {})}, attempt)

    async def fail(self, session_id: str, artifact_id: str, error: str,
                   attempt: str | None = None,
                   result: Mapping[str, Any] | None = None) -> None:
        """收口成 failed。``result`` 给「失败也要带信息给模型」的场景留位。

        渲染失败时会回一组**可选回退方案**（简化渲染/降分辨率/分段渲染）。原先 fail
        只写 error，那组方案就丢在节点返回值里——走「提交 + 轮询」的真实路径时，
        模型只看到一句错误，"失败就给出可选项让用户选"的设计永远走不到。
        """
        values: dict[str, Any] = {"status": "failed", "error": error}
        if result is not None:
            values["result"] = dict(result)
        await self._fenced(session_id, artifact_id, values, attempt)

    async def get(self, session_id: str, artifact_id: str) -> dict[str, Any] | None:
        rows = await self.db.select(self.table, where={"session_id": session_id,
                                                       "artifact_id": artifact_id},
                                    limit=1)
        return rows[0] if rows else None

    async def ready(self, session_id: str, artifact_id: str) -> bool:
        row = await self.get(session_id, artifact_id)
        return bool(row and row["status"] == "done" and row["video_object_key"])

    async def reap_stalled(self, timeout_sec: float, *, reason: str) -> list[dict[str, Any]]:
        """把「非终态 + 已经 N 秒没写过」的行收口成 failed，并回这些行。

        回行而不是只回计数：调用方（渲染看门狗）要靠 session_id/artifact_id 去取消
        本地那个卡住的后台任务，不然槽位永远被占着、行虽然收了活儿还在漏。

        收口时**并换 attempt 令牌**：被判死的那次尝试线程还在跑，换令牌后它跑完
        也写不进这条行了（否则会出现 failed → done 的翻转）。
        """
        cutoff = _now() - timedelta(seconds=timeout_sec)
        rows = await self.db.select(self.table, where=[Cond("status", "in",
                                                            ["queued", "running"]),
                                                       Cond("updated_at", "lt", cutoff)])
        for r in rows:
            await self.db.update(self.table, {"status": "failed", "error": reason,
                                              "attempt": new_id("at", 8),
                                              "updated_at": _now()},
                                 where={"id": r["id"]})
        return rows

    async def reap_hanging(self, timeout_sec: float) -> int:
        """进程崩溃留下的 running 悬挂态置 failed（spec §9 的对账后台任务）。

        含 ``queued``：提交后还没起跑就崩了，那一行同样在谎报「有活儿在干」；
        「提交 + 轮询」让 queued 成了真实存在的状态，不收就等于让轮询端空等。
        """
        return len(await self.reap_stalled(
            timeout_sec, reason="进程中断，状态悬挂已回收"))


# --------------------------------------------------------------------------
# 运行时状态
# --------------------------------------------------------------------------


class CheckpointsRepo(_Repo):
    """run 指针行（``checkpoints``）+ 一致点增量链（``checkpoint_entries``）。

    指针行只回答「这个 run 到哪了、成没成、占的是哪份产物作用域」；正文在 entries 里，
    一行一个一致点，只存自上一致点**新增**的消息。写放大从「每轮整条上下文」降到「每轮增量」。
    """

    table = "checkpoints"
    entry_table = "checkpoint_entries"

    async def save(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """写指针行。正文不在这张表里，所以每轮的成本是一个常数大小的小行。"""
        now = int(_now().timestamp() * 1000)
        row = {"run_id": run["run_id"], "session_id": run["session_id"],
               "message": run.get("message", ""), "iteration": run.get("iteration", 0),
               "head_seq": run.get("head_seq", 0),
               "status": run.get("status", "running"),
               "scope": dict(run.get("scope") or {}),
               "forked_from": run.get("forked_from"),
               "forked_at_seq": run.get("forked_at_seq"),
               "plan": dict(run.get("plan") or {}),
               "plan_run_id": run.get("plan_run_id"),
               "approval": dict(run.get("approval") or {}),
               "owner_instance_id": run.get("owner_instance_id"),
               "lease_expires_at": run.get("lease_expires_at"),
               "created_at_ms": run.get("created_at_ms", now), "updated_at_ms": now}
        # owner/lease 不入 update_values：它们由 claim/renew/release 独立管理，
        # 若随每次 save 回写，前进中的续租会被内存里那份旧租约覆盖掉。
        return await self.db.upsert(
            self.table, row,
            update_values={k: v for k, v in row.items()
                           if k not in ("run_id", "created_at_ms",
                                        "owner_instance_id", "lease_expires_at")})

    async def append_entry(self, run_id: str, *, kind: str, payload: Any,
                           iteration: int, parent_seq: int | None) -> dict[str, Any]:
        """追加一个一致点。seq 由链尾自增，调用方不必自己数。

        ``seq`` 是 ``(run_id, seq)`` 唯一键的一半，而取号是「先读 max 再插」——
        两条路径同时驱动同一条 run 时（启动恢复 + 用户此时发消息触发的续跑；
        或 ``/runs/{id}/resume`` 连点两次）会算出同一个 seq，后插的撞唯一约束。

        这里对撞号做**重试**：撞了就重读 max 再试。重试是安全的，因为 append 是
        幂等追加语义（写的是「下一个一致点」，不是「第 N 个」）；重试上限内仍撞
        说明有更严重的问题，如实抛出而不是静默吞掉。
        """
        last_err: Exception | None = None
        for _ in range(5):
            top = await self.db.max_value(self.entry_table, "seq",
                                          where={"run_id": run_id})
            seq = 0 if top is None else int(top) + 1
            try:
                return await self.db.insert(self.entry_table, {
                    "run_id": run_id, "seq": seq, "parent_seq": parent_seq,
                    "kind": kind, "payload": payload, "iteration": iteration,
                    "created_at_ms": int(_now().timestamp() * 1000)})
            except IntegrityConflict as exc:
                # 另一个驱动者刚插了同一个 seq：重读 max 再来一次
                last_err = exc
                continue
        raise IntegrityConflict(
            f"一致点 seq 连续 5 次撞号（run={run_id}）：{last_err}")

    async def load(self, run_id: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"run_id": run_id})

    async def load_entries(self, run_id: str,
                           upto_seq: int | None = None) -> list[dict[str, Any]]:
        """按 seq 升序取一致点链；upto_seq 是时间旅行的截断点（含）。"""
        where: list[Cond] = [Cond("run_id", "eq", run_id)]
        if upto_seq is not None:
            where.append(Cond("seq", "le", upto_seq))
        return await self.db.select(self.entry_table, where=where, order_by=["seq"])

    async def list_all(self) -> list[dict[str, Any]]:
        return await self.db.select(self.table, order_by=["-updated_at_ms"])

    async def list_unfinished(self) -> list[dict[str, Any]]:
        return await self.db.select(
            self.table, where=[Cond("status", "in", ["running", "failed"])],
            order_by=["updated_at_ms"])

    async def list_unfinished_by_session(self, session_id: str) -> list[dict[str, Any]]:
        return await self.db.select(
            self.table, where=[Cond("status", "eq", "running"),
                               Cond("session_id", "eq", session_id)],
            order_by=["-updated_at_ms"])

    async def list_interrupted_executions_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """同会话被打断的执行轮（status running/failed 且 plan_run_id 非空），最近在前。"""
        return await self.db.select(
            self.table, where=[Cond("status", "in", ["running", "failed"]),
                               Cond("session_id", "eq", session_id),
                               Cond("plan_run_id", "not_null", None)],
            order_by=["-updated_at_ms"])

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """本会话的全部 run（含已完成与分叉出的），最近的在前。"""
        return await self.db.select(self.table, where={"session_id": session_id},
                                    order_by=["-updated_at_ms"])

    async def list_awaiting_approval_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """本会话里正等着用户确认的 run，最近的在前。

        挂起态（``awaiting_approval``）不在 running/failed 里，所以 ``list_unfinished*``
        都查不到它。前端刷新后要把选项卡重新弹出来，就得单独按这个状态查一次。
        """
        return await self.db.select(
            self.table,
            where=[Cond("status", "eq", "awaiting_approval"),
                   Cond("session_id", "eq", session_id)],
            order_by=["-updated_at_ms"])

    async def claim_recoverable(self, instance_id: str, lease_expires_at: Any,
                                *, limit: int = 100) -> list[dict[str, Any]]:
        """原子认领可接手的 run：先「无主」，再「租约过期」。

        两步都是纯 AND 条件（条件 DSL 不支持 OR）：第一步领 owner IS NULL 的，
        第二步领 owner 非空但 lease < now 的。别的实例刚领走且租约未到期的 run，
        两步都命中不了——恢复权因此从人工约定变成了服务端判据。
        """
        set_values = {"owner_instance_id": instance_id,
                      "lease_expires_at": lease_expires_at}
        base = [Cond("status", "in", ["running", "failed"])]
        out = await self.db.claim(
            self.table, set_values,
            where=[*base, Cond("owner_instance_id", "is_null", None)],
            order_by=["updated_at_ms"], limit=limit)
        out += await self.db.claim(
            self.table, set_values,
            where=[*base, Cond("owner_instance_id", "not_null", None),
                   Cond("lease_expires_at", "lt", _now())],
            order_by=["updated_at_ms"], limit=limit)
        return out

    async def renew_lease(self, run_id: str, instance_id: str,
                          lease_expires_at: Any) -> bool:
        """只续自己持有的那条：owner 不是自己则改不动（不会替别人续租）。"""
        rows = await self.db.update(
            self.table, {"lease_expires_at": lease_expires_at},
            where=[Cond("run_id", "eq", run_id),
                   Cond("owner_instance_id", "eq", instance_id)])
        return bool(rows)

    async def release_lease(self, run_id: str, instance_id: str) -> int:
        """归还认领：清空 owner/lease，让该 run 不再被当成在途。"""
        rows = await self.db.update(
            self.table, {"owner_instance_id": None, "lease_expires_at": None},
            where=[Cond("run_id", "eq", run_id),
                   Cond("owner_instance_id", "eq", instance_id)])
        return len(rows)

    async def mark_running_as_failed(self) -> int:
        """把所有 status='running' 的 checkpoint 标记为 failed（服务重启时清理幽灵任务）。"""
        rows = await self.db.update(
            self.table, {"status": "failed"},
            where=[Cond("status", "eq", "running")])
        return len(rows)

    async def list_forks(self, run_id: str) -> list[dict[str, Any]]:
        return await self.db.select(self.table, where={"forked_from": run_id},
                                    order_by=["created_at_ms"])

    async def drop(self, run_id: str) -> int:
        n = await self.db.delete(self.entry_table, where={"run_id": run_id})
        return n + await self.db.delete(self.table, where={"run_id": run_id})

    async def drop_entries_after(self, run_id: str, seq: int) -> int:
        """砍掉某个一致点之后的链（原地回退，不新建 run 时用）。"""
        return await self.db.delete(self.entry_table,
                                    where=[Cond("run_id", "eq", run_id),
                                           Cond("seq", "gt", seq)])

    async def prune(self, keep: int = 200) -> int:
        """已收尾（成功/失败/让位）的旧 run 按量截断——替掉原来「永不删除、只增不减」的毛病。

        连带删 entries：指针行留下而正文留下都是白占空间。
        """
        rows = await self.db.select(
            self.table, where=[Cond("status", "in",
                                    ["completed", "failed", "superseded"])],
            order_by=["-updated_at_ms"])
        gone = 0
        for r in rows[keep:]:
            gone += await self.drop(r["run_id"])
        return gone


class InboxRepo(_Repo):
    """A2A 收件箱：原来是「读文件后清空」，现在是「标记已读」，可重放可审计。"""

    table = "inbox_messages"

    async def send(self, user_id: str, conv_id: str, agent: str, content: Any,
                   *, type_: str = "message", sender: str = "") -> dict[str, Any]:
        # consumed_at 显式写 NULL：两引擎返回的行都带这一列，调用方不必猜 schema 默认值
        return await self.db.insert(self.table, {
            "user_id": user_id, "conv_id": conv_id, "agent": agent, "type": type_,
            "sender": sender, "content": content, "consumed_at": None})

    async def read(self, user_id: str, conv_id: str, agent: str) -> list[dict[str, Any]]:
        rows = await self.peek(user_id, conv_id, agent)
        for r in rows:
            await self.db.update(self.table, {"consumed_at": _now()}, where={"id": r["id"]})
        return rows

    async def peek(self, user_id: str, conv_id: str, agent: str) -> list[dict[str, Any]]:
        return await self.db.select(self.table, where={"user_id": user_id, "conv_id": conv_id,
                                                       "agent": agent, "consumed_at": is_null()},
                                    order_by=["id"])

    async def agents(self, user_id: str, conv_id: str) -> list[str]:
        rows = await self.db.select(self.table, where={"user_id": user_id, "conv_id": conv_id},
                                    order_by=["agent"])
        return sorted({r["agent"] for r in rows})

    async def purge_consumed(self, user_id: str, conv_id: str) -> int:
        return await self.db.delete(self.table, where={"user_id": user_id, "conv_id": conv_id,
                                                       "consumed_at": not_null()})


class TasksRepo(_Repo):
    """任务板：双向边从「读-改-写多个文件」变成一张边表 + 一次事务。"""

    table = "tasks"

    async def create(self, scope: str, name: str, description: str = "", *,
                     blocked_by: Iterable[int] = ()) -> dict[str, Any]:
        """建任务并登记前置。blocks（下游）是同一批边的反向视图，由 get() 现算。"""
        deps = [int(d) for d in blocked_by]
        for dep in deps:
            row = await self.db.get_by_pk(self.table, {"id": dep})
            if row is None or row["scope"] != scope:
                raise IntegrityConflict(f"前置任务 #{dep} 不在 scope {scope} 内")
        tid = await self.db.next_id("task_seq")
        # status/owner/description 显式写值：两引擎返回的行同形，
        # 且 where={"status": "pending"} 这类过滤在内存替身里不会因缺键而漏行。
        row = await self.db.insert(self.table, {"id": tid, "scope": scope, "name": name,
                                                "description": description,
                                                "status": "pending", "owner": ""})
        for dep in deps:
            await self.db.upsert("task_edges", {"task_id": tid, "depends_on": dep})
        return row

    async def get(self, task_id: int) -> dict[str, Any] | None:
        row = await self.db.get_by_pk(self.table, {"id": task_id})
        if row:
            row["blockedBy"] = [e["depends_on"] for e in await self.db.select(
                "task_edges", where={"task_id": task_id}, order_by=["depends_on"])]
            row["blocks"] = [e["task_id"] for e in await self.db.select(
                "task_edges", where={"depends_on": task_id}, order_by=["task_id"])]
        return row

    async def list_all(self, scope: str) -> list[dict[str, Any]]:
        rows = await self.db.select(self.table, where={"scope": scope}, order_by=["id"])
        return [await self.get(r["id"]) for r in rows]

    async def claim(self, scope: str, owner: str) -> dict[str, Any] | None:
        """领取「可开工」的最小编号任务：pending 且前置都已完成。"""
        unlocked = [r["id"] for r in await self.ready(scope)]
        if not unlocked:
            return None
        got = await self.db.claim(self.table, {"status": "claimed", "owner": owner},
                                  where={"scope": scope, "status": "pending",
                                         "id": in_(unlocked)},
                                  order_by=["id"], limit=1)
        return await self.get(got[0]["id"]) if got else None

    async def claim_one(self, scope: str, owner: str, task_id: int) -> dict[str, Any] | None:
        """认领指定任务：仅当它属于本 scope 且仍是 pending 时原子改写，抢不到返回 None。"""
        got = await self.db.claim(self.table, {"status": "claimed", "owner": owner},
                                  where={"scope": scope, "id": task_id, "status": "pending"},
                                  order_by=["id"], limit=1)
        return await self.get(task_id) if got else None

    async def complete(self, scope: str, task_id: int) -> dict[str, Any] | None:
        await self.db.update(self.table, {"status": "completed"},
                             where={"id": task_id, "scope": scope})
        return await self.get(task_id)

    async def ready(self, scope: str) -> list[dict[str, Any]]:
        done = {r["id"] for r in await self.db.select(self.table,
                                                      where={"scope": scope,
                                                             "status": "completed"})}
        out = []
        for r in await self.db.select(self.table, where={"scope": scope, "status": "pending"},
                                      order_by=["id"]):
            deps = [e["depends_on"] for e in await self.db.select("task_edges",
                                                                  where={"task_id": r["id"]})]
            if all(d in done for d in deps):
                out.append(r)
        return out


class SubagentsRepo(_Repo):
    table = "subagents"

    async def register(self, scope: str, name: str, prompt: str,
                       lease_sec: float = 600, status: str = "idle") -> dict[str, Any]:
        # 租约只属于 working：idle/shutdown 行没有「我还在干」需要证明，
        # 留个未来的到期时间只会让 reset_expired 去动不该动的行。
        lease = _now() + timedelta(seconds=lease_sec) if status == "working" else None
        return await self.db.upsert(
            self.table,
            {"scope": scope, "name": name, "prompt": prompt, "status": status,
             "lease_expires_at": lease})

    async def set_status(self, scope: str, name: str, status: str,
                         lease_sec: float = 600) -> None:
        lease = _now() + timedelta(seconds=lease_sec) if status == "working" else None
        await self.db.upsert(self.table, {"scope": scope, "name": name, "status": status,
                                          "prompt": "", "lease_expires_at": lease},
                             update_values={"status": status, "lease_expires_at": lease})

    async def list(self, scope: str) -> list[dict[str, Any]]:
        return await self.db.select(self.table, where={"scope": scope}, order_by=["name"])

    async def reset_expired(self, scope: str | None = None) -> int:
        """租约到期即视为已死：把谎报 working 的行改回 idle（原来重启后状态永久失真）。

        scope=None 表示全作用域扫一遍 —— 服务启动时没人知道自己属于哪个会话。
        """
        where: dict[str, Any] = {"status": "working", "lease_expires_at": le(_now())}
        if scope is not None:
            where["scope"] = scope
        rows = await self.db.select(self.table, where=where)
        for r in rows:
            await self.set_status(r["scope"], r["name"], "idle")
        return len(rows)


class JobsRepo(_Repo):
    """定时任务 + 心跳合并同表。

    领取一轮用 ``state`` 的比较-交换（``claim_run``）而不是拿 ``enabled`` 当锁：领取时就把
    ``next_run_at_ms`` 推进到下一轮，事务结束后该行本身已不再到期，``enabled`` 因此只表达
    用户意图（多副本不重复触发，spec §7）。
    """

    table = "scheduled_jobs"

    async def upsert(self, job: Mapping[str, Any]) -> dict[str, Any]:
        row = {"id": job.get("id") or new_id("job", 8), "name": job["name"],
               "task": job.get("task", ""), "enabled": job.get("enabled", True),
               "delete_after_run": job.get("delete_after_run", False),
               "schedule": job.get("schedule", {}), "state": job.get("state", {}),
               "updated_at": _now()}
        return await self.db.upsert(self.table, row)

    async def list(self) -> list[dict[str, Any]]:
        return await self.db.select(self.table, order_by=["name"])

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"id": job_id})

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        rows = await self.db.select(self.table, where={"name": name}, limit=1)
        return rows[0] if rows else None

    async def drop(self, name: str) -> int:
        return await self.db.delete(self.table, where={"name": name})

    async def drop_id(self, job_id: str) -> int:
        return await self.db.delete(self.table, where={"id": job_id})

    async def set_state(self, job_id: str, state: Mapping[str, Any], *,
                        enabled: bool = True) -> None:
        await self.db.update(self.table, {"state": dict(state), "enabled": enabled,
                                          "updated_at": _now()}, where={"id": job_id})

    async def due(self, now_ms: int) -> list[dict[str, Any]]:
        """启用中、且 next_run_at_ms 已到点的 job。

        到期时间藏在 jsonb 里，两个引擎都不下推：按 enabled 取回后在 Python 比——
        这张表行数是个位数（定时任务 + 心跳），扫得起。
        """
        rows = await self.db.select(self.table, where={"enabled": True}, order_by=["name"])
        out = []
        for r in rows:
            nxt = (r.get("state") or {}).get("next_run_at_ms")
            if nxt is not None and nxt <= now_ms:
                out.append(r)
        return out

    async def claim_run(self, job_id: str, expect_state: Mapping[str, Any], *,
                        new_state: Mapping[str, Any],
                        enabled: bool = True) -> dict[str, Any] | None:
        """比较-交换领取：state 没被别人动过才写入新一轮时间，赢家独占这一轮执行。"""
        # 与 memories.append 同理：CAS 要用条件 UPDATE。claim 的 SKIP LOCKED 会把
        # 「行正被别人写」也报成「我没领到」，于是别人只是改了个无关字段（比如在页面
        # 上勾掉 enabled）就能悄悄吃掉这一轮。
        rows = await self.db.update(
            self.table,
            {"state": dict(new_state), "enabled": enabled, "updated_at": _now()},
            where=[Cond("id", "eq", job_id), Cond("enabled", "eq", True),
                   Cond("state", "eq", dict(expect_state))],
        )
        return rows[0] if rows else None


class MemoriesRepo(_Repo):
    table = "memories"

    async def read(self, user_id: str, category: str) -> str:
        row = await self.db.get_by_pk(self.table, {"user_id": user_id, "category": category})
        return row["content"] if row else ""

    async def write(self, user_id: str, category: str, content: str) -> None:
        await self.db.upsert(self.table, {"user_id": user_id, "category": category,
                                          "content": content, "updated_at": _now()})

    async def append(self, user_id: str, category: str, block: str, *,
                     retries: int = 16) -> str:
        """比较-交换追加：只有旧内容没被别人改过才写入合并结果，改过就重读重试。

        曾经的写法是 read → 拼接 → 全量覆盖，两个进程同时追加时后者冲掉前者，
        即 spec 附带缺陷清单里的「记忆 append 丢更新」。追加不丢是本表的底线，
        所以宁可在重试耗尽后抛错，也不静默写回一份旧内容。

        重试预算为什么按并发数而不是按运气算：CAS 失败**必然**意味着别人真的改了内容
        （不是「行被人锁住」那种假失败），所以每一轮都在往前走，一个写者需要的轮数
        约等于同 ``(user_id, category)`` 上的并发写者数。8 在十几个并发会话下会
        真的耗尽，故给 16。
        """
        for _ in range(retries):
            row = await self.db.get_by_pk(self.table, {"user_id": user_id,
                                                       "category": category})
            if row is None:
                try:
                    await self.db.insert(self.table, {"user_id": user_id, "category": category,
                                                      "content": block, "updated_at": _now()})
                except IntegrityConflict:
                    continue          # 并发里别人刚建了同一行：下一轮改走 CAS
                return block
            merged = f"{row['content']}\n{block}".strip()
            # 比较-交换必须用**条件 UPDATE**，不能用 claim。
            # 为什么：PG 的 claim 是 FOR UPDATE SKIP LOCKED——它专为「多个 worker
            # 抢不同行」设计，遇到被别人锁住的行会**跳过**而不是等。用在 CAS 上就变成
            # 「行只是被别人锁住」也被当成「我输了这一轮」，于是并发追加会互相跳过、
            # 8 次重试全数白烧（真机复现：5 路并发 gather 抛 IntegrityConflict，约 2/3 次）。
            # 条件 UPDATE 会先在行锁上等住，拿到锁后按新快照重算 WHERE：内容真被改过
            # 才返回 0 行——这才是 CAS 要的判据。
            won = await self.db.update(
                self.table, {"content": merged, "updated_at": _now()},
                where=[Cond("user_id", "eq", user_id), Cond("category", "eq", category),
                       Cond("content", "eq", row["content"])])
            if won:
                return merged
        raise IntegrityConflict(
            f"memories 追加竞争 {retries} 次未胜出，放弃写入（user_id={user_id}, category={category}）")

    async def entries(self, user_id: str) -> dict[str, str]:
        rows = await self.db.select(self.table, where={"user_id": user_id},
                                    order_by=["category"])
        return {r["category"]: r["content"] for r in rows}

    async def drop_category(self, user_id: str, category: str) -> int:
        return await self.db.delete(self.table, where={"user_id": user_id,
                                                       "category": category})


class SkillsRepo(_Repo):
    table = "skills"

    async def upsert(self, name: str, body: str, *, description: str = "",
                     frontmatter: Mapping[str, Any] | None = None,
                     files: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        row = await self.db.upsert(self.table, {
            "name": name, "description": description, "body": body,
            "frontmatter": dict(frontmatter or {}), "updated_at": _now()})
        await self.db.delete("skill_files", where={"skill": name})
        for f in files:
            await self.db.insert("skill_files", {"skill": name, "relpath": f["relpath"],
                                                 "object_key": f["object_key"],
                                                 "bytes": f.get("bytes", 0)})
        return row

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.select(self.table, order_by=["name"])
        for r in rows:
            r["files"] = await self.db.select("skill_files", where={"skill": r["name"]},
                                              order_by=["relpath"])
        return rows

    async def get(self, name: str) -> dict[str, Any] | None:
        row = await self.db.get_by_pk(self.table, {"name": name})
        if row:
            row["files"] = await self.db.select("skill_files", where={"skill": name},
                                                order_by=["relpath"])
        return row

    async def drop(self, name: str) -> int:
        await self.db.delete("skill_files", where={"skill": name})
        return await self.db.delete(self.table, where={"name": name})


def mask(value: str) -> str:
    """密钥的对外形态：只留前缀与末 4 位，认得出「是哪一把」，抄不走。

    明文只从 SecretsRepo.get() 出门，且唯一的去处是签名发给模型服务。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:3]}…{'*' * 4}{v[-4:]}"


class SecretsRepo(_Repo):
    """模型密钥：前端配一次就生效。get() 给明文（唯一去处是签发出网请求），展示一律走 mask()。"""

    table = "app_secrets"

    async def row(self, user_id: str, key_name: str) -> dict[str, Any] | None:
        return await self.db.get_by_pk(self.table, {"user_id": user_id,
                                                    "key_name": key_name})

    async def get(self, user_id: str, key_name: str) -> str:
        row = await self.row(user_id, key_name)
        return (row or {}).get("value", "")

    async def put(self, user_id: str, key_name: str, value: str) -> None:
        await self.db.upsert(self.table, {"user_id": user_id, "key_name": key_name,
                                          "value": value, "updated_at": _now()})

    async def drop(self, user_id: str, key_name: str) -> int:
        return await self.db.delete(self.table, where={"user_id": user_id,
                                                       "key_name": key_name})


class TimelinesRepo(_Repo):
    """时间线编辑器保存的条目（前端「时间线」面板）。

    为什么要有这一层：``/timelines*`` 与 ``/latest_timeline`` 原先直接调
    ``storage.db._run("SELECT … extract(epoch from updated_at) … CAST(:payload AS jsonb) …")``，
    而 ``_run`` 只定义在 ``PgDatastore`` 上——内存引擎没有这个方法，
    于是 ``--storage memory``（离线演示、单机跑、测试）下这几个端点一律
    ``AttributeError: 'MemoryDatastore' object has no attribute '_run'`` 直接 500。

    走通用 repo 后两个引擎行为一致；``updated_at`` 转 epoch 浮点由调用方按需做
    （PG 是 ``timestamptz``，内存是 aware datetime，两者都能 ``.timestamp()``）。
    """

    table = "timelines"

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return await self.db.select(self.table, where={"user_id": user_id},
                                    order_by=["-updated_at"])

    async def get_for_user(self, tl_id: str, user_id: str) -> dict[str, Any] | None:
        row = await self.db.get_by_pk(self.table, {"id": tl_id})
        if row is None or row.get("user_id") != user_id:
            return None            # 越权视同不存在：不泄漏「这条属于别人」
        return row

    async def put(self, *, tl_id: str, user_id: str, conv_id: str | None, name: str,
                  payload: Mapping[str, Any], video_url: str | None,
                  duration_sec: float | None) -> dict[str, Any]:
        """按 id 覆盖保存；video_url 为 None 时保留旧值（与原先的 COALESCE 语义一致）。"""
        existing = await self.db.get_by_pk(self.table, {"id": tl_id})
        values = {"user_id": user_id, "conv_id": conv_id, "name": name,
                  "payload": dict(payload or {}), "duration_sec": duration_sec,
                  "updated_at": _now()}
        if video_url is not None:
            values["video_url"] = video_url
        if existing is None:
            values.setdefault("video_url", video_url)
            return await self.db.insert(self.table, {"id": tl_id, **values})
        # 注意括号：`await f(...)[0]` 会先取下标再 await（update 是协程，取不到下标）
        rows = await self.db.update(self.table, values, where={"id": tl_id})
        return rows[0] if rows else existing

    async def drop_for_user(self, tl_id: str, user_id: str) -> int:
        return await self.db.delete(self.table, where={"id": tl_id, "user_id": user_id})
