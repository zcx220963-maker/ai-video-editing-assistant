# -*- coding: utf-8 -*-
"""把落盘的 mp4 送进对象存储，换回一个能直接播的 presigned URL。

只做一件事，而且**只借不抄**：`storage.objects.put` + `presign_get` 都是平台现成的，
这里只是把"一个文件"包成"一个 key + 一个链接"。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

#: 成片在桶里的前缀。与主链的 `renders/` 分开，便于按线统计与清理。
PREFIX = "simple"


def _chunks(path: Path, size: int = 1 << 20):
    async def gen():
        with path.open("rb") as fh:
            while True:
                buf = fh.read(size)
                if not buf:
                    return
                yield buf
    return gen()


async def publish(path: Path, *, storage: Any, user_id: str = "",
                  ttl_sec: int = 6 * 3600) -> dict[str, Any]:
    """上传并签名。返回 ``{url, object_key, bytes}``。

    `ttl_sec` 缺省 6 小时：够用户看完、转发、下载；过期后重新调 `/simple/job/{id}`
    会重新签一次（作业表里留着 object_key）。
    """
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    key = f"{PREFIX}/{user_id or 'anon'}/{digest}.mp4"

    await storage.objects.put(key, _chunks(path), content_type="video/mp4", size=len(data))
    url = await storage.objects.presign_get(key, ttl_sec=ttl_sec)
    return {"url": url, "object_key": key, "bytes": len(data)}


async def republish(object_key: str, *, storage: Any, ttl_sec: int = 6 * 3600) -> str:
    """只重签，不上传。作业表里存了 key 时用这个——比再传一遍便宜得多。"""
    return await storage.objects.presign_get(object_key, ttl_sec=ttl_sec)


def _iter(data: bytes, size: int = 1 << 20):
    async def gen():
        for i in range(0, len(data), size):
            yield data[i:i + size]
    return gen()


async def publish_bytes(data: bytes, *, suffix: str, content_type: str,
                        storage: Any, user_id: str = "",
                        ttl_sec: int = 6 * 3600) -> dict[str, Any]:
    """把一段内存里的字节传上去并签名（静帧用）。

    为什么静帧也要进对象存储：它要出现在**用户的对话里**（一张能看的图），
    而工具回执只能带文本或 data URL。data URL 几十 KB 塞进消息体不划算，
    签名链接还带缓存。与成片同一个前缀，便于按线清理。
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    key = f"{PREFIX}/{user_id or 'anon'}/{digest}.{suffix}"
    await storage.objects.put(key, _iter(data), content_type=content_type, size=len(data))
    url = await storage.objects.presign_get(key, ttl_sec=ttl_sec)
    return {"url": url, "object_key": key, "bytes": len(data)}
