"""素材入库的唯一实现：字节流 → 对象存储 → ffprobe 探测 → materials 登记 → presigned 回放。

三条入料路径（前端上传 /upload、前端链接 /fetch_media、Agent 工具 fetch_media）都必须
经过 `ingest_bytes`，这样「素材是什么、归谁、字节在哪」只有一处定义——否则链接取料
和上传取料会在 kind 判定、对象键布局、归属登记上慢慢走岔。

设计约束（spec §5）：本地磁盘不是链路的一环。这里唯一的本地足迹是 workspace 里给
ffprobe 用的探测临时文件，用完即删；对象存储才是字节的唯一源。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from .storage import IntegrityConflict, Storage, StorageUnavailable, new_id
from .storage.media import (
    ProbeError, kind_of, mime_of, needs_normalize, normalize_to_h264, probe,
)

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 一条素材允许的字节上限由调用方给（CLI --max-upload-mb / --max-fetch-mb 换算）。
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024


class IngestRejected(Exception):
    """入库被拒：带 HTTP 语义码，接口层据此翻译成响应，工具层据此回错误字符串。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def safe_segment(raw: str, fallback: str) -> str:
    """路径片段净化：去 Windows 非法字符与目录穿越，空则回退。"""
    name = _UNSAFE_NAME.sub("_", Path(raw or "").name).strip().strip(".")
    return name[:80] or fallback


def _chunks_from_file(path: Path, chunk: int = 1 << 20) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        with path.open("rb") as fh:
            while True:
                b = await asyncio.to_thread(fh.read, chunk)
                if not b:
                    break
                yield b

    return gen()


async def ingest_bytes(
    storage: Storage,
    chunks: AsyncIterator[bytes],
    *,
    filename: str,
    user_id: str,
    conversation_id: str | None,
    origin: str = "upload",
    max_bytes: int = DEFAULT_MAX_BYTES,
    content_type: str | None = None,
) -> dict[str, Any]:
    """字节流 → 一条 materials 行 + 对象存储里的字节，返回前端与工具共用的素材描述。

    `chunks` 是任意异步字节生成器（HTTP 请求体、本地临时文件都走这一条）；sha256 与
    字节数在收流过程中算出，因此**超上限是边收边断**，不会先把 20GB 落进桶里再拒。
    `origin` 进 materials 表（upload/url），用于以后区分「用户自己传的」与「爬来的」。
    `conversation_id` 为空表示这条素材不挂任何会话（BGM 曲库）：跳过 conversations 行
    登记，materials.conv_id 留 NULL，可见性只按 owner_user_id 判。
    """
    kind = kind_of(filename)
    if kind is None:
        raise IngestRejected(
            415, f"不支持的素材类型：{Path(filename).suffix or filename}")
    fname = safe_segment(filename, "file.bin")
    ext = Path(fname).suffix.lower()
    mid = new_id("mat", 6)
    # 对象键段净化（防穿越），DB 里的 id 用原值——两端语义不同是有意的
    key = (f"users/{safe_segment(user_id, 'u')}/convs/"
           f"{safe_segment(conversation_id, 'c')}/{mid}{ext}")

    async def guarded():
        written = 0
        async for chunk in chunks:
            if not chunk:
                continue
            written += len(chunk)
            if written > max_bytes:
                raise IngestRejected(
                    413, f"素材超过 {max_bytes // (1024 * 1024)}MB 上限")
            yield chunk
        if written == 0:
            raise IngestRejected(400, "空素材")

    try:
        info = await storage.objects.put(key, guarded(),
                                         content_type=content_type or mime_of(fname))
    except IntegrityConflict as e:
        raise IngestRejected(409, f"对象写入冲突：{e}") from e
    except StorageUnavailable as e:
        raise IngestRejected(502, f"对象存储不可用：{e}") from e

    # 元数据探测 + 视频归一化：localize 一份临时文件喂 ffprobe；探测失败不毁掉
    # 已入库的素材。HEVC/非常规编码在此统一转 H.264（一条素材只转这一次），否则
    # 后续 ffmpeg→MoviePy 链路会静默丢帧或抽错帧。归一化失败按原样入库不阻断。
    meta: Mapping[str, Any] = {}
    original_key: str | None = None
    normalized_from = ""
    normalize_warning = ""
    probe_dir = storage.workspace.dir_for(origin, "_probe")
    normalize_dir = storage.workspace.dir_for(origin, "_normalize")
    try:
        local = await storage.workspace.localize(key, probe_dir)
        meta = await probe(local)
        if kind == "video" and needs_normalize(meta):
            normalized_from = meta.get("codec") or ""
            try:
                timeout = max(300.0, float(meta.get("duration_sec") or 0) * 12.0)
                out = await normalize_to_h264(
                    local, normalize_dir / "normalized.mp4", timeout=timeout)
                nmeta = await probe(out)
                nkey = key[: -len(ext)] + "-n.mp4" if ext else key + "-n.mp4"
                ninfo = await storage.objects.put(
                    nkey, _chunks_from_file(out), content_type="video/mp4")
                # materials 指向归一化件；原件保留在桶里（original_object_key 可追溯）
                original_key, key, info, meta = key, nkey, ninfo, nmeta
                fname = Path(fname).stem + ".mp4"
            except (ProbeError, OSError, StorageUnavailable, TimeoutError) as e:
                normalize_warning = f"归一化失败（{type(e).__name__}），按原编码入库: {str(e)[:120]}"
    except (ProbeError, OSError, StorageUnavailable, TimeoutError):
        meta = {}
    finally:
        storage.workspace.cleanup(origin, "_probe")
        storage.workspace.cleanup(origin, "_normalize")

    try:
        if conversation_id:
            await storage.conversations.ensure(user_id, conversation_id)
        row = await storage.materials.register(
            user_id, conversation_id, key, fname, kind,
            bytes_=info.bytes, sha256=info.sha256, mime=mime_of(fname),
            duration_sec=meta.get("duration_sec") or None,
            width=meta.get("width") or None,
            height=meta.get("height") or None,
            has_audio=bool(meta.get("has_audio")),
            material_id=mid, origin=origin)
    except IntegrityConflict as e:
        await storage.objects.delete(key)      # 归属不成立：不留孤儿字节
        raise IngestRejected(409, f"素材归属登记失败：{e}") from e
    except StorageUnavailable as e:
        await storage.objects.delete(key)      # 元数据没登记成功就不留孤儿字节
        raise IngestRejected(502, f"materials 登记失败：{e}") from e

    url = await storage.objects.presign_get(key)
    out: dict[str, Any] = {
        "material_id": row["id"], "id": row["id"], "filename": fname,
        "name": fname, "kind": kind, "bytes": info.bytes,
        "duration": row["duration_sec"], "width": row["width"],
        "height": row["height"], "has_audio": row["has_audio"],
        "object_key": key, "url": url, "origin": origin,
    }
    if normalized_from:
        out["normalized"] = True
        out["normalized_from"] = normalized_from
        out["original_object_key"] = original_key
    if normalize_warning:
        out["normalize_warning"] = normalize_warning
    return out


async def ingest_local_file(
    storage: Storage,
    path: str | Path,
    *,
    filename: str | None = None,
    user_id: str,
    conversation_id: str,
    origin: str = "url",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """已落到临时工作区的文件 → 同一条入库路径（yt-dlp 兜底用），调用方负责删临时文件。"""
    p = Path(path)
    return await ingest_bytes(
        storage, _chunks_from_file(p),
        filename=filename or p.name, user_id=user_id, conversation_id=conversation_id,
        origin=origin, max_bytes=max_bytes)
