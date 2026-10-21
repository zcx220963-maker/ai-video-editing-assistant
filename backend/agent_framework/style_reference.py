# -*- coding: utf-8 -*-
"""参考素材 → 风格包：聊天里唯一的那条路。

用户可以把参考视频、图片和一句话一起发进来。这里只做两件事：**读画面**
（视觉模型）与**写风格包**（`style_pack_gen` 的契约）。参考素材只用于理解，
不会出现在成片里；每一帧仍由代码绘制。

为什么不复用参考片沉淀那一套：那条链要把参考片变成**可执行 TSX 模板**，要写码、
过编译闸、真渲、验确定性，分钟级。用户现在要的是「贴进来，说一句，接着做」——
风格包本身就是数据，能直接落库、直接注入分镜，不必先有一份模板。有合适的现成
模板时，生成器会自己挂上；没有就按正文自己画。
"""
from __future__ import annotations

import asyncio
import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import style_authoring as sa
from . import style_pack_gen as gen

#: 一条参考视频最多看几帧。够认出底色/光位/材质/构图/节奏，又不至于把上下文塞满。
MAX_VIDEO_FRAMES = 6
#: 最多细读几条参考视频。两条以上时每条少看几帧——提炼共同语法的收益递减。
MAX_VIDEOS = 2
#: 最多几张参考图（与视频帧共用同一个上限）。
MAX_IMAGES = 6
#: 一次读画面最多发多少张图。
MAX_FRAMES = 8
#: 参考图缩到多宽：再大只是多耗 token，看不出更多视觉语法。
IMAGE_EDGE = 1280

_REFERENCE_NOTE = """
这一次还会给参考画面（视频关键帧或图片）。先读画面，再写风格包：
- 从画面里取底色、受光色、暗部色、强调色，写成小写 #rrggbb；取最接近的纯色，
  不要把渐变原样写成很多个色。
- 写清光位与明暗比、材质与表面反射、构图比例、镜头运动、质感（颗粒/暗角/雾）。
- 写清字幕落在画面的哪一处、占多宽、多大；字幕形态仍按硬约束，不许改。
- 参考里如果是真人、照片或复杂插画，只取它的视觉语法（光、色、构图、节奏），
  不要写成需要照片或写实人物的画法——最终每一帧仍由代码画。
- 参考与用户原话冲突时，以用户原话为准。
""".strip()


class ReferenceError(ValueError):
    """参考素材读不出可用的风格（取帧失败 / 模型回话不合法）。"""


def _jpeg_part(path: Path) -> dict[str, Any]:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _to_jpeg(src: Path, dst_dir: Path) -> Path:
    """上传的图片可能是 PNG/WebP/带透明通道；统一转成小 JPEG 再给视觉模型。

    直接拿原字节配 `image/jpeg` 的 data URL，遇到 PNG 时有的网关会拒、有的会
    按错误编码解出花屏——两种都表现为「模型读不懂这张图」，而不是一条可读错误。
    """
    out = dst_dir / "image.jpg"
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src),
         "-vf", f"scale='min({IMAGE_EDGE},iw)':-2",
         "-frames:v", "1", "-q:v", "3", "-y", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        raise ReferenceError("这张图片读不出来")
    return out


async def collect_frames(storage: Any, materials: Sequence[Mapping[str, Any]],
                         tmp: Path) -> tuple[list[Path], list[str]]:
    """参考素材 → 一组 JPEG 帧。取不出来的那条只记一句，不拖垮整次。"""
    frames: list[Path] = []
    notes: list[str] = []
    video_total = sum(1 for r in materials if str(r.get("kind") or "") == "video")
    videos_allowed = min(video_total, MAX_VIDEOS)
    per_video = 4 if videos_allowed > 1 else MAX_VIDEO_FRAMES
    videos = 0
    images = 0
    for i, row in enumerate(materials):
        if len(frames) >= MAX_FRAMES:
            break
        kind = str(row.get("kind") or "")
        name = str(row.get("filename") or row.get("id") or "参考素材")
        if kind == "video" and videos >= videos_allowed:
            continue
        if kind == "image" and images >= MAX_IMAGES:
            continue
        sub = tmp / f"m{i:02d}"
        sub.mkdir(parents=True, exist_ok=True)
        try:
            local = await storage.workspace.localize_material(dict(row), sub)
        except Exception as exc:  # noqa: BLE001 - 取不到字节 = 这条参考不可用
            notes.append(f"《{name}》取不下来：{str(exc)[:120]}")
            continue
        if kind == "video":
            videos += 1
            try:
                meta = await asyncio.to_thread(sa.probe_video, local)
                shots = await asyncio.to_thread(
                    sa.detect_shots, local, meta["duration"])
                times, _ = sa.pick_shot_times(
                    meta["duration"], per_video, shots)
                got = await asyncio.to_thread(
                    sa.extract_frames, local, sub / "frames", times)
                frames.extend(got[:MAX_FRAMES - len(frames)])
            except Exception as exc:  # noqa: BLE001 - 坏片跳过，原因带回去
                notes.append(f"《{name}》取帧失败：{str(exc)[:120]}")
        elif kind == "image":
            images += 1
            try:
                frames.append(await asyncio.to_thread(_to_jpeg, local, sub))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"《{name}》读不出来：{str(exc)[:120]}")
        else:
            notes.append(f"《{name}》不是视频或图片，已跳过")
    return frames[:MAX_FRAMES], notes


async def draft_with_frames(llm: Any, frames: Sequence[Path], *,
                            words: str, templates: Sequence[Mapping[str, Any]],
                            identity_user: str = "",
                            conversation_id: str = "") -> dict[str, Any]:
    """读画面并写出风格包草稿（不落库）。"""
    if not frames:
        raise ReferenceError("没有可读的参考画面")
    prompt = (str(words or "").strip()
              or "按这些参考画面定一套整片统一的视觉。")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(_jpeg_part(p) for p in frames)
    messages = [
        {"role": "system", "content": gen.SYSTEM + "\n\n" + _REFERENCE_NOTE},
        {"role": "user", "content": content},
    ]
    try:
        from .identity import use_identity
        if identity_user:
            with use_identity(identity_user, conversation_id):
                resp = await llm.complete(messages, max_tokens=3200, timeout=240.0)
        else:
            resp = await llm.complete(messages, max_tokens=3200, timeout=240.0)
        raw = str(getattr(resp, "content", "") or "")
    except Exception as exc:  # noqa: BLE001 - 模型/密钥侧任何毛病都只说一句人话
        raise ReferenceError(f"读画面时模型调用失败：{str(exc)[:160]}") from exc
    draft = gen.parse_draft(raw)
    names = [str(t.get("name") or "") for t in templates]
    draft["manifest"] = gen.normalize_manifest(draft.get("manifest") or {}, names)
    return draft


async def make_style(storage: Any, llm: Any, *,
                     words: str, material_ids: Sequence[str],
                     templates: Sequence[Mapping[str, Any]],
                     user_id: str, conversation_id: str,
                     name: str) -> dict[str, Any]:
    """一句话 + 可选的参考素材 → 一份已落库的风格包。

    参考素材只用来读视觉；没有素材时退回「一句话生成」那条纯文本路径。
    """
    ids = [str(m).strip() for m in material_ids if str(m).strip()]
    materials: list[dict[str, Any]] = []
    notes: list[str] = []
    if ids:
        rows, denied = await storage.materials.resolve(
            ids, user_id=user_id, conv_id=conversation_id or None)
        if denied:
            notes.append("有参考素材不存在或无权使用，已跳过")
        materials = [dict(r) for r in rows]

    frames: list[Path] = []
    tmp_ctx = None
    try:
        if materials:
            tmp_ctx = tempfile.TemporaryDirectory(prefix="style-ref-")
            frames, more = await collect_frames(
                storage, materials, Path(tmp_ctx.name))
            notes.extend(more)
        if frames:
            draft = await draft_with_frames(
                llm, frames, words=words, templates=templates,
                identity_user=user_id, conversation_id=conversation_id)
        else:
            draft = await gen.generate(
                llm, description=words, templates=templates,
                identity_user=user_id, conversation_id=conversation_id)
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    display = str(draft.get("display") or "").strip() or "我的风格"
    description = str(draft.get("description") or "").strip()
    body = str(draft.get("body") or "")
    manifest = dict(draft.get("manifest") or {})
    await storage.skills.upsert(
        name, body, description=description,
        frontmatter={"name": name, "display": display, "kind": "style",
                     "description": description, "style_manifest": manifest},
        owner_user_id=user_id)
    return {"name": name, "display": display, "description": description,
            "body": body, "manifest": manifest, "notes": notes,
            "frames": len(frames)}
