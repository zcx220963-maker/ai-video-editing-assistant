"""素材侧的媒体判定：扩展名白名单 → `materials.kind`，以及 ffprobe 元数据探测。

`materials.kind` 的 CHECK 枚举（video/audio/image）就是这里的白名单来源，二者必须同步，
否则上传成功却插不进行。探测走系统 PATH 上的 ffprobe（spec §8 把 ffmpeg/ffprobe 列为
「留在本地」项），阻塞调用一律经 `asyncio.to_thread`，不卡事件循环。

与 `storyline_server/mediaops.py` 的关系：那一层是渲染原子的 ffprobe 封装（节点侧用），
本模块只管入库判定——主 Agent 侧不 import 节点包，依赖方向保持 storyline → agent_framework。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"}

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

KIND_BY_EXT: dict[str, str] = {
    **{e: "video" for e in VIDEO_EXTS},
    **{e: "audio" for e in AUDIO_EXTS},
    **{e: "image" for e in IMAGE_EXTS},
}

# 前端 accept 早已允许选图，后端此前没有图片白名单（spec §4 要补的一处）。
MIME_BY_EXT: dict[str, str] = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".webm": "video/webm",
    ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".aac": "audio/aac",
    ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".heic": "image/heic",
}


def kind_of(filename: str) -> str | None:
    """扩展名 → 'video'/'audio'/'image'；不在白名单内返回 None（调用方据此拒绝上传）。"""
    return KIND_BY_EXT.get(Path(filename or "").suffix.lower())


def mime_of(filename: str) -> str:
    return MIME_BY_EXT.get(Path(filename or "").suffix.lower(), "application/octet-stream")


class ProbeError(RuntimeError):
    pass


# 归一化白名单：MoviePy/时间线渲染链路只对 H.264 + yuv420p 稳定
# （iPhone/剪映导出的 HEVC 会静默丢帧或抽错帧），其余编码入库时统一转 H.264。
H264_CODEC = "h264"
PIXFMT_OK = "yuv420p"


def needs_normalize(meta: Mapping[str, Any] | dict[str, Any]) -> bool:
    """probe 元数据判定是否需要归一化：探测不出 codec 时一律不转（保持旧行为）。"""
    codec = (meta.get("codec") or "").strip()
    if not codec:
        return False
    if codec != H264_CODEC:
        return True
    pix = (meta.get("pix_fmt") or "").strip()
    return bool(pix) and pix != PIXFMT_OK


def probe_sync(path: str | Path) -> dict[str, Any]:
    """ffprobe → {duration_sec, width, height, has_audio, codec, pix_fmt}；失败抛 ProbeError。

    图片没有时长与音轨，字段按 0/False 返回，调用方照原样入库。
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise ProbeError((proc.stderr or proc.stdout or "")[-300:])
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or (v or {}).get("duration") or 0.0)
    return {"duration_sec": round(duration, 3),
            "width": int((v or {}).get("width") or 0),
            "height": int((v or {}).get("height") or 0),
            "has_audio": a is not None,
            "codec": (v or {}).get("codec_name") or "",
            "pix_fmt": (v or {}).get("pix_fmt") or ""}


def normalize_to_h264_sync(src: str | Path, dst: str | Path,
                           *, timeout: float = 3600.0) -> Path:
    """全片重编码为 H.264/AAC + faststart（入库归一化，一条素材只转这一次）。

    超时按调用方给的音频/视频时长伸缩（CPU 上 16 分钟 1080p 约几分钟）。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-i", str(src),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "")[-300:]
        raise ProbeError(f"归一化转码失败: {tail}")
    return dst


async def normalize_to_h264(src: str | Path, dst: str | Path,
                            *, timeout: float = 3600.0) -> Path:
    return await asyncio.to_thread(normalize_to_h264_sync, src, dst, timeout=timeout)


async def probe(path: str | Path) -> dict[str, Any]:
    return await asyncio.to_thread(probe_sync, path)
