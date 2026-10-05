"""大文件分片续传：一次上传切成等长分片，一片一个对象，收齐后按序拼成一条素材。

为什么不是「浏览器一次 POST 整个文件」：2GB 的片子传到一个 G 抖一下网络就得从 0 重来，
中间没有任何可续的断点，用户只能看着进度条归零。分片之后：

  · **续传状态以桶为准**。分片就是对象存储里 ``uploads/`` 前缀下的普通对象，
    「已有哪几片」问一次 ``objects.list_prefix`` 就有答案，不落在任何进程的内存里——
    刷新页面、断网重连、换一个副本接着传，看到的都是同一份事实。
  · 服务端不持有整文件缓冲。每一片独立 ``put``，收流时按账本推算的该片应有长度
    边收边断（多了、少了都当场拒），所以谎报 size 不会撑爆内存或留下坏片。
  · 内容校验有两道：客户端给每片算 sha256（``PUT part`` 上比对，算错当场退回重传），
    complete 时再把它本地算出的**按序逐片摘要**整列交回来（``parts_sha256``），与桶里
    真存下的字节逐片比——对不上只作废那几片，missing_parts 会把它们还给续传逻辑。
    脚本/工具类客户端还可以在 init 声明整文件 sha256，拼装时再核一遍整条指纹。
  · 收齐后把分片按序串成一条字节流，喂给 ``ingest.ingest_bytes``——与 ``/upload``、
    ``/fetch_media`` **同一条**入库路径。素材是什么、归谁、字节在哪仍然只有一处定义，
    分片只是搬运方式，不是第二种入库语义。

代价要说清楚：拼装期间桶里同时存在分片与成品（峰值占用约为文件大小的 2 倍），
成品写入成功后立刻删分片；删不净的（对象存储抖动）由 ``sweep_upload_sessions``
在会话过期后补删，不会永久漏成孤儿字节。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, AsyncIterator

from .ingest import IngestRejected, ingest_bytes, safe_segment
from .storage import Storage, StorageUnavailable
from .storage.media import kind_of

# 分片尺寸的三道闸：太小则请求数与对象数失控，太大则一次网络抖动的重传代价过高。
MIN_PART_BYTES = 1 * 1024 * 1024
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_PARTS = 10000

# 会话默认寿命：6 小时没动静的分片就是垃圾（人走开了、页面关了、传一半放弃了）。
DEFAULT_SESSION_TTL_SEC = 6 * 3600.0

DEFAULT_MAX_BYTES = 1024 * 1024 * 1024

_HEX64 = re.compile(r"[0-9a-f]{64}")
_PART_TAIL = re.compile(r"^part_(\d{5,})$")


class UploadRejected(IngestRejected):
    """分片续传被拒：继承入库拒绝，端点层因此只需一个 except 就能翻译成 HTTP 语义码。"""


def pick_part_size(total_bytes: int, requested: int | None = None) -> int:
    """定下这一趟的分片长度（服务端说了算，回给客户端照着切）。

    客户端可以不提（按文件大小自适应），提了则夹进 [MIN, MAX]；无论哪种，最后都保证
    分片数不超过 ``MAX_PARTS``——超过就把片长往上抬，而不是拒掉这次上传。
    """
    if requested and requested > 0:
        ps = max(MIN_PART_BYTES, min(MAX_PART_BYTES, int(requested)))
    else:
        # 小文件一片走完（不必动切法）；大文件按 16MB 起，兼顾进度粒度与请求数
        ps = MIN_PART_BYTES if total_bytes <= 64 * MIN_PART_BYTES else 16 * MIN_PART_BYTES
    while -(-total_bytes // ps) > MAX_PARTS:          # 分片数超限 → 片长翻倍再试
        ps = min(ps * 2, MAX_PART_BYTES)
        if ps == MAX_PART_BYTES:
            break
    return ps


def session_prefix(user_id: str, conv_id: str, upload_id: str) -> str:
    """本次会话全部分片的对象键前缀（键段净化，防目录穿越）。

    ``upload_id`` 已是服务端签发的随机 id，所以前缀天然按「谁的哪个会话的哪次上传」
    分格；清扫与作废都只需一次前缀列举，不必逐片记账。
    """
    return (f"uploads/{safe_segment(user_id, 'u')}/convs/"
            f"{safe_segment(conv_id, 'c')}/{upload_id}/")


def part_key(user_id: str, conv_id: str, upload_id: str, index: int) -> str:
    return f"{session_prefix(user_id, conv_id, upload_id)}part_{index:05d}"


def expected_part_bytes(row: dict[str, Any], index: int) -> int:
    """第 index 片应有的字节数：除末片外都等于片长，末片是余数。

    由账本推算而不是逐片记录——「该多大」是可算的，就没有一处需要人维护的进度状态。
    """
    if index == row["part_count"] - 1:
        return row["total_bytes"] - index * row["part_size"]
    return row["part_size"]


async def _received(storage: Storage, row: dict[str, Any]) -> dict[int, Any]:
    """桶里已有的分片：{片号: ObjectInfo}。续传进度的唯一出处。"""
    prefix = session_prefix(row["owner_user_id"], row["conv_id"], row["id"])
    out: dict[int, Any] = {}
    for info in await storage.objects.list_prefix(prefix):
        m = _PART_TAIL.match(info.key[len(prefix):])
        if m:
            out[int(m.group(1))] = info
    return out


async def _drop_parts(storage: Storage, row: dict[str, Any]) -> int:
    """删掉本次会话留在桶里的分片，返回删除的对象数。"""
    n = 0
    for info in (await _received(storage, row)).values():
        await storage.objects.delete(info.key)
        n += 1
    return n


async def _owned_row(storage: Storage, upload_id: str, user_id: str) -> dict[str, Any]:
    row = await storage.upload_sessions.get(upload_id)
    if row is None or row["owner_user_id"] != user_id:
        # 不区分「不存在」与「是别人的」：区分了就把「别人有这条会话」这件事说了出去
        raise UploadRejected(404, f"上传会话不存在：{upload_id}")
    return row


async def session_view(storage: Storage, row: dict[str, Any], *,
                       resumed: bool = False) -> dict[str, Any]:
    """客户端要的续传快照：切法 + 已有哪几片 + 还缺哪几片。

    已完成的会话不再列举分片（收齐即删，列出来只会得到一个谎报「全缺」的进度）。
    """
    view: dict[str, Any] = {
        "upload_id": row["id"], "conversation_id": row["conv_id"],
        "filename": row["filename"], "size": row["total_bytes"],
        "part_size": row["part_size"], "part_count": row["part_count"],
        "sha256": row.get("sha256") or "", "status": row["status"],
        "material_id": row.get("material_id"), "resumed": resumed,
    }
    if row["status"] == "completed":
        return {**view, "received_bytes": row["total_bytes"], "percent": 100,
                "parts": [], "missing_parts": []}
    got = await _received(storage, row)
    parts = [{"index": i, "bytes": got[i].bytes, "sha256": got[i].sha256}
             for i in sorted(got) if i < row["part_count"]]
    have = {p["index"] for p in parts}
    return {**view, "parts": parts,
            "received_bytes": sum(p["bytes"] for p in parts),
            "percent": round(sum(p["bytes"] for p in parts) * 100 / row["total_bytes"], 1),
            "missing_parts": [i for i in range(row["part_count"]) if i not in have]}


async def init_upload(storage: Storage, *, user_id: str, conversation_id: str,
                      filename: str, size: int, part_size: int | None = None,
                      sha256: str = "", max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """开一次分片上传（或接回上次那一次）。

    同一人、同一会话、同名同尺寸同指纹且还在传 → 复用同一条账：刷新页面与断网重连
    都会重发 init，不复用就得把已传的分片丢在桶里干等过期。
    """
    if kind_of(filename) is None:
        raise UploadRejected(415, f"不支持的素材类型：{Path(filename).suffix or filename}")
    if size <= 0:
        raise UploadRejected(400, "size 必须是正整数（分片上传前要定死总长）")
    if size > max_bytes:
        raise UploadRejected(413, f"素材超过 {max_bytes // (1024 * 1024)}MB 上限")
    sha = (sha256 or "").strip().lower()
    if sha and not _HEX64.fullmatch(sha):
        raise UploadRejected(400, "sha256 得是 64 位十六进制整文件指纹（不提供就留空）")
    ps = pick_part_size(size, part_size)
    fname = safe_segment(filename, "file.bin")
    row = await storage.upload_sessions.find_live(user_id, conversation_id, fname,
                                                  size, ps, sha)
    if row is not None:
        return await session_view(storage, row, resumed=True)
    # 归属先钉死：会话不是你的就当场拒，别收了半堆分片才发现无处登记
    await storage.conversations.ensure(user_id, conversation_id)
    count = -(-size // ps)
    if count > MAX_PARTS:
        raise UploadRejected(400, f"即便按 {MAX_PART_BYTES // (1024 * 1024)}MB 切片也要 "
                                 f"{count} 片，超过单次上传 {MAX_PARTS} 片的上限")
    row = await storage.upload_sessions.open(
        user_id, conversation_id, fname, size, ps, count, sha)
    return await session_view(storage, row)


async def put_part(storage: Storage, *, user_id: str, upload_id: str, part: int,
                   chunks: AsyncIterator[bytes], sha256: str = "") -> dict[str, Any]:
    """收第 ``part`` 片：字节直写对象存储，长度与指纹当场核。

    重复传同一片是允许的（后写覆盖前写）——续传客户端本就可能重发最后一片。
    """
    row = await _owned_row(storage, upload_id, user_id)
    if row["status"] != "uploading":
        raise UploadRejected(409, f"会话 {upload_id} 已入库完成，不再收片（要重传请重新 init）")
    if int(part) != part or not (0 <= part < row["part_count"]):
        raise UploadRejected(400, f"分片号越界：{part}（本次合法区间 0…{row['part_count'] - 1}）")
    want = expected_part_bytes(row, part)
    key = part_key(row["owner_user_id"], row["conv_id"], upload_id, part)
    declared = (sha256 or "").strip().lower()
    if declared and not _HEX64.fullmatch(declared):
        raise UploadRejected(400, f"分片 {part} 的 sha256 不是 64 位十六进制")

    async def guarded() -> AsyncIterator[bytes]:
        got = 0
        async for chunk in chunks:
            if not chunk:
                continue
            got += len(chunk)
            if got > want:
                raise UploadRejected(413, f"分片 {part} 超过 {want} 字节（与账本推算的片长不符）")
            yield chunk
        if got != want:
            # 短收必须拒：收下一条时才发现凑不满，等于把一条永远拼不齐的素材挂在那儿
            raise UploadRejected(400, f"分片 {part} 应有 {want} 字节，实收 {got}")

    try:
        info = await storage.objects.put(key, guarded(),
                                         content_type="application/octet-stream")
    except UploadRejected:
        await storage.objects.delete(key)      # 万一某引擎已经把半片留下，不留脏对象
        raise
    if declared and info.sha256 != declared:
        await storage.objects.delete(key)      # 内容不对的片子留着只会拼出坏素材
        raise UploadRejected(
            422, f"分片 {part} 落库后的 sha256 与客户端算出的不符，请重传这一片")
    await storage.upload_sessions.touch(upload_id)   # 推进 updated_at：别让清扫收走在传的会话
    return {"upload_id": upload_id, "part": part, "bytes": info.bytes,
            "sha256": info.sha256, "part_bytes": want}


async def upload_status(storage: Storage, *, user_id: str, upload_id: str) -> dict[str, Any]:
    """问一次进度（也是续传的起手式：拿到 missing_parts 就知道接着传哪几片）。"""
    row = await _owned_row(storage, upload_id, user_id)
    return await session_view(storage, row)


async def complete_upload(storage: Storage, *, user_id: str, upload_id: str,
                          parts_sha256: list[str] | None = None,
                          max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """收齐 → 按序比对每片内容 → 按序拼流 → 同一条 ingest_bytes 入库 → 删分片。

    任何一步失败都不动账本：分片还在、状态还是 uploading，客户端补齐后可以再 complete，
    不必从第一片重传。唯一例外是内容比对失败的**那几片**——它们会被删掉，
    于是 missing_parts 如实把它们还给客户端。
    """
    row = await _owned_row(storage, upload_id, user_id)
    if row["status"] == "completed":
        # 幂等：响应丢了的客户端重发一次 complete，应该拿回同一条素材而不是一个错误
        got = await storage.materials.get(row.get("material_id") or "")
        if got is None:
            raise UploadRejected(409, f"会话 {upload_id} 标记已完成但素材行不见了")
        return {**await _material_view(storage, got), "upload_id": upload_id,
                "already_completed": True}

    have = await _received(storage, row)
    missing = [i for i in range(row["part_count"]) if i not in have]
    if missing:
        raise UploadRejected(409, f"还差 {len(missing)} 片没到位（{missing[:10]}），"
                                  f"补齐再 complete")
    wrong = [i for i in range(row["part_count"])
             if have[i].bytes != expected_part_bytes(row, i)]
    if wrong:
        raise UploadRejected(409, f"{len(wrong)} 片字节数与切法不符，请重传这些片：{wrong[:10]}")

    # 第二道内容校验：客户端把它**本地这份文件**逐片算出的摘要按序交来，与桶里真正
    # 存下的字节的摘要逐片比。整文件 sha256 浏览器算不动（那要一次读满整个文件），
    # 而逐片摘要它在传每一片时本来就算过——比对因此是免费的，且比整文件指纹更有用：
    # 错哪一片就点名哪一片，删掉它，missing_parts 如实把它还给续传逻辑。
    # 这里抓到的典型场景是「同名同尺寸的另一份文件」：init 会复用同一条账（去重键里
    # 只有文件名/尺寸/切法），已传的分片属于另一个文件，逐片长度却完全合规。
    uid, cid = row["owner_user_id"], row["conv_id"]
    verified = 0
    if parts_sha256:
        if len(parts_sha256) != row["part_count"]:
            raise UploadRejected(400, f"parts_sha256 要恰好 {row['part_count']} 条"
                                      f"（每片一条，已传的也要给），实收 {len(parts_sha256)} 条")
        bad: list[int] = []
        for i, declared in enumerate(parts_sha256):
            ds = (declared or "").strip().lower()
            if not _HEX64.fullmatch(ds):
                raise UploadRejected(400, f"parts_sha256[{i}] 不是 64 位十六进制")
            if ds == have[i].sha256:
                verified += 1
            else:
                bad.append(i)
        if bad:
            for i in bad:
                await storage.objects.delete(part_key(uid, cid, upload_id, i))
            await storage.upload_sessions.touch(upload_id)
            raise UploadRejected(
                422, f"{len(bad)} 片内容与你本地算出的摘要不符，已作废这些片，"
                     f"请重新 init 补传：{bad[:10]}")

    total = row["total_bytes"]
    want_sha = row.get("sha256") or ""

    async def assembled() -> AsyncIterator[bytes]:
        h = hashlib.sha256() if want_sha else None
        got = 0
        for i in range(row["part_count"]):
            async for chunk in storage.objects.get_stream(part_key(uid, cid, upload_id, i)):
                got += len(chunk)
                if h is not None:
                    h.update(chunk)
                yield chunk
        if got != total:
            raise UploadRejected(409, f"拼装得到 {got} 字节，与账本上的 {total} 不符")
        if h is not None and h.hexdigest() != want_sha:
            # 逐片都合法但整文件指纹不对 = 传错文件/片序错乱，绝不能登记成素材
            raise UploadRejected(422, "整文件 sha256 与 init 时声明的不符，请重传分片")

    result = await ingest_bytes(storage, assembled(), filename=row["filename"],
                                user_id=uid, conversation_id=cid, origin="upload",
                                max_bytes=max_bytes)
    # 成品已经写进桶里并登记好了，这一步失败不能反过来把入库说成失败：
    # 删不净的分片由 sweep_upload_sessions 在会话过期后补删（它不看状态）。
    try:
        cleaned = await _drop_parts(storage, row)
        pending = False
    except StorageUnavailable:
        cleaned, pending = 0, True
    await storage.upload_sessions.finalize(upload_id, result["material_id"])
    return {**result, "upload_id": upload_id, "parts_cleaned": cleaned,
            "parts_verified": verified, "parts_cleanup_pending": pending}


async def abort_upload(storage: Storage, *, user_id: str, upload_id: str) -> dict[str, Any]:
    """用户取消：分片删净才删账本行；桶不通就留着这行，让过期清扫再来一遍。"""
    row = await _owned_row(storage, upload_id, user_id)
    try:
        cleaned = await _drop_parts(storage, row)
    except StorageUnavailable:
        return {"upload_id": upload_id, "parts_cleaned": 0, "cleanup_pending": True}
    await storage.upload_sessions.drop(upload_id)
    return {"upload_id": upload_id, "parts_cleaned": cleaned, "aborted": True}


async def _material_view(storage: Storage, row: dict[str, Any]) -> dict[str, Any]:
    """materials 行 → 与 ingest_bytes 同形状的返回（幂等 complete 用）。"""
    return {
        "material_id": row["id"], "id": row["id"], "filename": row["filename"],
        "name": row["filename"], "kind": row["kind"], "bytes": row["bytes"],
        "duration": row.get("duration_sec"), "width": row.get("width"),
        "height": row.get("height"), "has_audio": bool(row.get("has_audio")),
        "object_key": row["object_key"], "origin": row.get("origin", "upload"),
        "url": await storage.objects.presign_get(row["object_key"]),
    }


async def sweep_upload_sessions(storage: Storage, *,
                                max_age_sec: float = DEFAULT_SESSION_TTL_SEC) -> dict[str, int]:
    """回收没动静的上传会话：分片字节 + 账本行一起清（启动期与定时清扫共用）。

    完成态的行也扫：那是「成品已写、分片没删净」的唯一兜底（见 complete_upload）。
    桶报错时**不删行**——留着下一次清扫再来，避免把孤儿字节永久留在桶里。
    """
    swept = parts = 0
    for row in await storage.upload_sessions.expired(max_age_sec):
        try:
            n = await _drop_parts(storage, row)
        except StorageUnavailable:
            continue
        await storage.upload_sessions.drop(row["id"])
        swept += 1
        parts += n
    return {"swept": swept, "parts_cleaned": parts}
