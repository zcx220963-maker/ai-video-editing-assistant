"""R3 投递口：把内置素材库（``asset_library``，全局共享）的一条素材投进某会话的
``materials``（用户级），让「检索 → 选用 → 上屏」这条链真的闭环。

**为什么必须复制字节、不能共享 ``object_key``**：``materials.object_key`` 上挂着
唯一约束，而 ``asset_library`` 是全局共享的——两个用户选同一条素材、或同一个用户
在两个会话里选同一条，都会在同一把键上撞车（``IntegrityConflict``）。所以投递口把
库里那份字节**复制**到 ``users/<uid>/convs/<cid>/<mid><ext>``（与上传同一条键型），
再登记一行 ``origin='library'``。库里那份原样不动（它不属任何用户、是所有会话共用的
底本），会话里这份才是「这次要用的素材」；两份靠 ``sha256`` 对得上账（T45 的判据）。

**幂等**：同一 ``(owner, conv, sha256)`` 上已有一行 ``origin='library'`` 就复用那一行，
不再复制。重复选同一条素材是常态（改主意再选回来），不该每选一次就堆一份字节。

**失败不改判调用方**：形制与 ``render_library.register_render`` 一致——回
``(material_id, 说明)``，说明非空即这次没投进去，由调用方并进 ``notes`` 对用户明说，
不许静默吞掉，也不许把「库空了」报成「链坏了」。
"""

from __future__ import annotations

from typing import Any

from .identity import parse_storyline_session
from .ingest import safe_segment
from .storage import IntegrityConflict, StorageUnavailable, new_id
from .storage.media import kind_of, mime_of

ORIGIN = "library"


async def pick_library_asset(storage: Any, session_id: str, asset_id: str, *,
                             conversation_id: str | None = None,
                             ) -> tuple[str, str]:
    """把库素材 ``asset_id`` 投进 ``session_id`` 的素材库，回 ``(material_id, 说明)``。

    ``conversation_id`` 显式给定时压过 ``session_id`` 里拆出来的那个：HTTP 口拿的是
    查询参数与 token，不该再去拼一遍会话键。
    """
    user_id, conv_id = parse_storyline_session(session_id)
    if conversation_id is not None:
        conv_id = conversation_id or None
    if not user_id:
        return "", f"会话键 {session_id!r} 拆不出用户，这条库素材没投进素材库"

    try:
        asset = await storage.assets.get(str(asset_id or "").strip())
    except StorageUnavailable as exc:
        return "", f"素材库读不到（{type(exc).__name__}: {str(exc)[:120]}）"
    if not asset:
        return "", f"库素材 {asset_id!r} 不存在"
    if not asset.get("enabled", True):
        return "", f"库素材 {asset_id!r} 已停用"
    if asset.get("attribution_required"):
        # §5.2：需署名的资产不进检索结果；投递口照同一把尺——不然「检索里没有、
        # 但能拿 id 直接投」，那把尺就是摆设。
        return "", f"库素材 {asset_id!r} 需要署名（attribution_required），不进可选集"

    src_key = str(asset.get("object_key") or "")
    if not src_key:
        return "", f"库素材 {asset_id!r} 没有 object_key（库里只剩元数据行，字节不在）"

    sha = str(asset.get("sha256") or "")
    ext = str(asset.get("ext") or "").lstrip(".")
    fname = (f"{safe_segment(str(asset.get('src_id') or asset_id), 'asset')}"
             f"{('.' + ext) if ext else ''}")
    kind = kind_of(fname)
    if kind is None:
        return "", f"库素材 {asset_id!r} 的扩展名 {ext!r} 不在媒体白名单内，没法登记成素材"

    # 幂等：同一 (owner, conv, sha) 上已投过就复用——先查再复制，省掉一次无用的搬运。
    try:
        existing = await storage.materials.db.select(
            storage.materials.table,
            where={"owner_user_id": user_id, "conv_id": conv_id,
                   "sha256": sha, "origin": ORIGIN},
            limit=1)
    except StorageUnavailable:
        existing = []
    if existing:
        return str(existing[0]["id"]), ""

    mid = new_id("mat", 6)
    dst_key = (f"users/{safe_segment(user_id, 'u')}/convs/"
               f"{safe_segment(conv_id or '_', 'c')}/{mid}{('.' + ext) if ext else ''}")
    try:
        info = await storage.objects.put(
            dst_key, storage.objects.get_stream(src_key),
            content_type=str(asset.get("mime") or mime_of(fname)))
    except StorageUnavailable as exc:
        return "", f"库素材字节取不到（{type(exc).__name__}: {str(exc)[:120]}）"

    try:
        if conv_id:
            await storage.conversations.ensure(user_id, conv_id)
        row = await storage.materials.register(
            user_id, conv_id, dst_key, fname, kind,
            bytes_=info.bytes, sha256=info.sha256 or sha,
            mime=str(asset.get("mime") or mime_of(fname)),
            width=asset.get("width"), height=asset.get("height"),
            origin=ORIGIN, material_id=mid)
    except (IntegrityConflict, StorageUnavailable) as exc:
        await storage.objects.delete(dst_key)      # 登记不成立：不留孤儿字节
        return "", f"库素材入素材库失败（{type(exc).__name__}: {str(exc)[:120]}）"
    return str(row["id"]), ""
