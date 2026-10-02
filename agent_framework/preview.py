"""渲染前的「编排预览」读模型：把 Store 里的产物整理成一张可给用户看的编排结果。

**为什么需要它。** 真机暴露的流程缺口：用户确认计划之后，模型一路跑到渲染完成，
用户这才第一次看到成片——此时才发现「最后一句被截断了」「字幕没去掉」。
用户的原话是：希望**渲染之前**就能看到编排结果（提取内容、切分、分镜、画面、声音怎么排的），
决定是改还是渲；改的话重做计划再弹一次，直到满意为止。

这个模块只做**只读整理**：输入是 Storyline 的产物字典（``node -> payload``），
输出是一份前端直接可渲染的结构。它不碰渲染、不写数据、不调 LLM，
所以可以独立测、也不影响任何既有链路。

数据来源就是既有节点产物，没有新增采集：
  · ``plan_timeline*``  → 时间线（画面事件 / 音轨 / 字幕 / BGM）
  · ``group_clips``     → 叙事分组
  · ``understand_clips``→ 每个镜头的画面描述（caption）
  · ``asr``             → 逐句转写（人声内容）
  · ``generate_voiceover`` / ``select_BGM`` → 声音编排
  · ``filter_clips`` / ``split_shots`` / ``load_media`` → 切分与素材事实
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# 分组/事件列表的长度上限：预览是给人看的，不是全量导出。
MAX_GROUPS = 40
MAX_SEGMENTS = 60
MAX_SHOTS = 200


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clips_of(groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in groups or []:
        for c in (g.get("clips") or []):
            if isinstance(c, Mapping):
                out.append(dict(c))
    return out


def _caption_map(understood: Mapping[str, Any]) -> dict[str, str]:
    """clip_id → 画面描述。key 在不同版本里叫过 clip/clip_id，两个都认。"""
    out: dict[str, str] = {}
    for row in (understood.get("clip_captions") or []):
        if not isinstance(row, Mapping):
            continue
        cid = str(row.get("clip") or row.get("clip_id") or "")
        if cid:
            out[cid] = str(row.get("caption") or row.get("description") or "")
    return out


def _asr_map(asr: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """clip_id → 该镜头的转写句子（按 start 排序）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for seg in (asr.get("asr_segments") or []):
        if not isinstance(seg, Mapping):
            continue
        cid = str(seg.get("clip") or seg.get("clip_id") or "")
        if not cid:
            continue
        out.setdefault(cid, []).append({
            "start": round(_num(seg.get("start")), 2),
            "end": round(_num(seg.get("end")), 2),
            "text": str(seg.get("text") or "").strip(),
        })
    for v in out.values():
        v.sort(key=lambda s: s["start"])
    return out


def build_preview(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    """把节点产物整理成「编排预览」。

    返回结构（前端按这个渲染，缺哪段就少画哪块——不会因为某个节点没跑就报错）::

        {
          "ready": bool,              # 有没有可预览的编排（至少要有一条时间线）
          "summary": {...},           # 一句话级别的总览数字
          "material": {...},          # 素材事实
          "shots": {...},             # 切分结果
          "story": [...],             # 叙事分组（含画面描述与人声内容）
          "timeline": {...},          # 时间线：画面轨 / 音轨 / 字幕 / BGM
          "audio": {...},             # 声音编排
          "warnings": [...],          # 值得让用户先看一眼的偏差
        }
    """
    arts = dict(artifacts or {})
    warnings: list[str] = []

    # ---- 时间线：优先用带转场的版本，其次 pro，最后基础版 ----
    tl: dict[str, Any] = {}
    tl_node = ""
    for name in ("plan_timeline_ai_transition", "plan_timeline_pro", "plan_timeline"):
        payload = arts.get(name)
        if isinstance(payload, Mapping):
            candidate = payload.get("timeline") or payload
            if isinstance(candidate, Mapping) and candidate.get("events"):
                tl, tl_node = dict(candidate), name
                break

    groups = ((arts.get("group_clips") or {}).get("groups")
              if isinstance(arts.get("group_clips"), Mapping) else None) or []
    clips = _clips_of(groups)
    caps = _caption_map(arts.get("understand_clips") or {})
    asr_map = _asr_map(arts.get("asr") or {})
    media = ((arts.get("load_media") or {}).get("media")
             if isinstance(arts.get("load_media"), Mapping) else None) or []

    # ---- 素材事实 ----
    material = {
        "count": len(media),
        "items": [{
            "id": str(m.get("id") or m.get("material_id") or ""),
            "material_id": str(m.get("material_id") or ""),
            "duration": round(_num(m.get("duration")), 2),
            "resolution": (f"{m.get('width')}x{m.get('height')}"
                           if m.get("width") and m.get("height") else ""),
            "has_audio": bool(m.get("has_audio")),
        } for m in media if isinstance(m, Mapping)][:MAX_GROUPS],
    }

    # ---- 切分结果 ----
    split = arts.get("split_shots") or {}
    keep = arts.get("filter_clips") or {}
    kept_ids = {str(c.get("clip") or c.get("id") or "")
                for c in (keep.get("clips") or []) if isinstance(c, Mapping)}
    shots = [c for c in (split.get("clips") or []) if isinstance(c, Mapping)]
    shot_rows = [{
        "id": str(c.get("id") or c.get("clip") or ""),
        "start": round(_num(c.get("start")), 2),
        "end": round(_num(c.get("end")), 2),
        "duration": round(_num(c.get("duration")), 2),
        "caption": caps.get(str(c.get("id") or c.get("clip") or ""), ""),
        "kept": (not kept_ids) or (str(c.get("id") or c.get("clip") or "") in kept_ids),
    } for c in shots][:MAX_SHOTS]
    shots_block = {
        "total": len(shots),
        "kept": sum(1 for r in shot_rows if r["kept"]),
        "items": shot_rows,
    }
    dropped = keep.get("dropped") if isinstance(keep, Mapping) else None
    if dropped:
        shots_block["dropped"] = len(dropped) if isinstance(dropped, (list, tuple)) else dropped

    # ---- 叙事分组：每组带画面描述与人声内容（用户最需要看的） ----
    story: list[dict[str, Any]] = []
    for g in groups[:MAX_GROUPS]:
        gid = str(g.get("group_id") or "")
        rows = [c for c in (g.get("clips") or []) if isinstance(c, Mapping)]
        dur = sum(_num(c.get("duration")) for c in rows)
        segs: list[dict[str, Any]] = []
        for c in rows:
            cid = str(c.get("clip") or c.get("id") or "")
            segs.extend(asr_map.get(cid, []))
        segs.sort(key=lambda s: s["start"])
        story.append({
            "group_id": gid,
            "summary": str(g.get("summary") or ""),
            "clip_count": len(rows),
            "duration": round(dur, 2),
            "captions": [caps.get(str(c.get("clip") or c.get("id") or ""), "")
                         for c in rows][:8],
            "speech": [s["text"] for s in segs if s["text"]][:12],
        })

    # ---- 声音编排 ----
    vo = arts.get("generate_voiceover") or {}
    vo_tracks = [t for t in (vo.get("voiceover") or []) if isinstance(t, Mapping)]
    bgm = arts.get("select_BGM") or {}
    audio = {
        "mode": str(tl.get("mode") or ""),
        "voiceover_count": len(vo_tracks),
        "voiceover_seconds": round(sum(_num(t.get("duration")) for t in vo_tracks), 2),
        "bgm": ({
            "filename": str(bgm.get("filename") or ""),
            "source": str(bgm.get("source") or ""),
            "volume": round(_num((tl.get("bgm") or {}).get("volume")), 2),
        } if isinstance(bgm, Mapping) and (bgm.get("bgm") or bgm.get("material_id"))
            else None),
        "original_audio": bool(tl.get("mode") == "original_audio"),
    }

    # ---- 时间线（画面轨 / 音轨 / 字幕） ----
    events = [e for e in (tl.get("events") or []) if isinstance(e, Mapping)]
    audio_events = [a for a in (tl.get("audio_events") or []) if isinstance(a, Mapping)]
    subs = [s for s in (tl.get("subtitles") or []) if isinstance(s, Mapping)]
    plays = sum(_num(e.get("end")) - _num(e.get("start")) for e in events)
    timeline = {
        "node": tl_node,
        "duration": round(_num(tl.get("duration")), 2),
        "resolution": (f"{tl.get('width')}x{tl.get('height')}"
                       if tl.get("width") and tl.get("height") else ""),
        "fps": round(_num(tl.get("fps")), 2),
        "video_events": len(events),
        "video_seconds": round(plays, 2),
        "audio_events": len(audio_events),
        "subtitles": len(subs),
        "speaker_ratio": round(_num(tl.get("speaker_ratio")), 2),
        "events": [{
            "start": round(_num(e.get("start")), 2),
            "end": round(_num(e.get("end")), 2),
            "kind": str(e.get("kind") or ""),
            "caption": caps.get(str(e.get("clip") or ""), ""),
        } for e in events][:MAX_SEGMENTS],
        "subtitle_preview": [str(s.get("text") or "") for s in subs][:20],
    }

    # ---- 值得先看一眼的偏差 ----
    if timeline["duration"] and timeline["video_seconds"] + 0.05 < timeline["duration"]:
        warnings.append(
            f"画面轨只排到 {timeline['video_seconds']:.1f}s，而时间线总长 "
            f"{timeline['duration']:.1f}s——末尾会定格。")
    if timeline["duration"] and timeline["audio_events"] == 0 and not audio["original_audio"]:
        warnings.append("这条时间线没有音轨事件，成片可能是静音的。")
    kept_n = shots_block["kept"]
    if shots_block["total"] and kept_n == 0:
        warnings.append("筛选后一个片段都没保留——请检查筛选条件。")
    # 人声被截断的迹象：最后一句的结束时间超过成片时长
    all_speech_end = max(
        (_num(s["end"]) for segs in asr_map.values() for s in segs), default=0.0)
    if timeline["duration"] and all_speech_end > timeline["duration"] + 0.3:
        warnings.append(
            f"转写内容到 {all_speech_end:.1f}s，但成片只到 {timeline['duration']:.1f}s"
            f"——末尾的话会被截断。")

    summary = {
        "groups": len(story),
        "clips": shots_block["kept"] or len(clips),
        "duration": timeline["duration"],
        "resolution": timeline["resolution"],
        "speech_seconds": round(sum(s["duration"] for s in story), 2),
    }
    return {
        "ready": bool(timeline["events"]),
        "summary": summary,
        "material": material,
        "shots": shots_block,
        "story": story,
        "timeline": timeline,
        "audio": audio,
        "warnings": warnings,
    }
