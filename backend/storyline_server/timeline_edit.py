# -*- coding: utf-8 -*-
"""口播/素材链的「选区改」合同：命中表 + 指针补丁 + 受影响段判定。

为什么单独一个模块：``RenderVideoNode`` 那一份已经三千行，而这三件事（量一张表、
按指针改一处、判断哪些段必须重烧）全是**纯函数**——不碰对象存储、不碰 MoviePy，
能离线逐条断言。出片链的复杂度和判定逻辑混在一起，就没有一样东西能单独测。

与图形科普片那条链（``motion/hitmap.py``）的分工：那边量的是**浏览器里的框**
（版式是 CSS 定位算出来的，Python 算不出真实像素盒），所以有探针、有按文本反查字段、
有 confidence=contains/contained 那一套。这边没有版式可量：时间线里每一段本来就是
「第 s 秒到第 e 秒的一整幅画面」或「屏幕底部的一条字幕带」，框与字段都由数据直接给出，
所以**指针是精确的**（confidence 恒为 exact）；只有字幕带的高度是排版估算，单独标在
``box_precision`` 上，不冒充量过的数字。

贯穿全部设计的两条红线：
① **改错了地方还不报错**是这条链最贵的失败模式——指针必须逐字取自表、末端必须是
   白名单里的已有字段、段 id 与指针不符就整批拒收。
② **一次改动不许悄悄改掉总长与音画对应**——所以这里只接受「不改长度」的编辑：
   改字、改样式、改盖层、换画面取的区间、换素材。要改顺序或删段，会把画面挪到
   别的口播句子底下（声音是不动的基准），那是重新规划，不是选区改，明确拒收。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

#: 缓存格式版本。窗口输入的清单变了就 +1，让旧键整体失效——留着旧对象不会报错，
#: 只会让「选着改」读到一份对不上版的表、复用一段对不上内容的字节。
CACHE_VERSION = "1"

TRACK_LABELS = {
    "events": "画面",
    "audio_events": "声音",
    "subtitles": "字幕",
    "overlay_events": "覆盖层",
}

_NUM, _STR = "number", "string"

#: 每条轨的可改字段白名单（字段 → 类型）。不在名单里的字段一律拒收，理由见模块开头。
#:
#: 为什么 ``events`` 没有 ``start``/``end``：画面段的输出区间是「排在前面的段一共多长」
#: 推出来的**结果**，不是可以直接拧的旋钮；拧它等于把后面所有段连声轨一起挪走。
#: 为什么 ``events`` 的 ``src_start``/``src_end`` 成对生效：见 :func:`apply_patch`——
#: 选的是「取源片哪一截」，长度不变，所以不牵连任何别的段。
#: 为什么 ``audio_events`` 只能换 ``path``：口播声音是整条时间线的时钟基准，
#: 改它的长度/区间就要重排画面，那是重新规划。
EDITABLE: dict[str, dict[str, str]] = {
    "events": {"path": _STR, "src_start": _NUM, "src_end": _NUM, "kind": _STR},
    "audio_events": {"path": _STR},
    "subtitles": {"text": _STR, "style": _STR, "start": _NUM, "end": _NUM},
    "overlay_events": {"path": _STR, "start": _NUM, "end": _NUM,
                       "src_start": _NUM, "src_end": _NUM, "fit": _STR,
                       "text": _STR},
}

#: 影响一段画面**像素与声音**的顶层旋钮。BGM 刻意不在其中：配乐混在母带那一层
#: （见 :func:`window_inputs`），换一首曲子不该让几十段全部重烧。
_CACHE_KNOBS = ("width", "height", "fps", "mode")

_POINTER_RE = re.compile(r"^/(events|audio_events|subtitles|overlay_events)/(\d+)(/[^/]+)?$")

_ROLE = {"events": "segment-window", "audio_events": "voice-window",
         "subtitles": "caption", "overlay_events": "overlay-window"}


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(doc: Mapping[str, Any]) -> str:
    """这一版时间线的指纹（16 位）。命中表必须知道自己量的是**哪一版**。

    表与版本必须成对使用：用户对着旧表选了一块，而时间线在这中间被改过，按指针落笔
    就会改到别的段上。所以把指纹写进表里，补丁那一头比对不上就拒收，让重新出片成为
    唯一出路（前端自律不管用——它会拿着查到的表就往里写）。
    """
    return hashlib.sha256(_dumps(doc).encode("utf-8")).hexdigest()[:16]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _round(v: Any) -> Any:
    return None if v is None else round(float(v), 3)


def _shift(v: Any, delta: float) -> Any:
    """源时刻平移 ``delta`` 秒；没写源时刻的（配音整文件播）原样返回 None。"""
    return None if v is None else round(float(v) + delta, 3)


def _seg_end(item: Mapping[str, Any]) -> float:
    """段的输出终点：没有 ``end`` 时按配音段的写法退到 ``start + duration``。"""
    if item.get("end") is not None:
        return _num(item.get("end"))
    return _num(item.get("start")) + _num(item.get("duration"))


# ---------------------------------------------------------------------------
# 缓存键：一段画面的字节只烧一次
# ---------------------------------------------------------------------------

def window_boundaries(doc: Mapping[str, Any]) -> list[tuple[float, float]]:
    """把画面轨切成「这一段时间线上没有任何段起止」的窗口。

    边界只取 **events** 的 start/end：窗口是用户点的那一块，缓存也按这一块复用。
    字幕/覆盖层/声音跨边界时**不切窗口**——它们被算进两侧窗口的输入清单
    （见 :func:`window_inputs`），所以它们变了两侧都重烧，接缝不会错开一拍。
    """
    marks = {round(_num(ev.get(k)), 3) for ev in (doc.get("events") or [])
             if isinstance(ev, Mapping) for k in ("start", "end")
             if ev.get(k) is not None}
    ordered = sorted(m for m in marks if m >= 0)
    total = _num(doc.get("duration"))
    if total and (not ordered or ordered[-1] < total - 0.001):
        ordered.append(round(total, 3))
    return [(a, b) for a, b in zip(ordered, ordered[1:]) if b - a > 0.02]


def window_id_at(doc: Mapping[str, Any], start: float) -> str:
    """窗口的名字 = 落在这一秒起点上的那个画面段 id（找不到就退成时间标号）。"""
    for ev in doc.get("events") or []:
        if isinstance(ev, Mapping) and abs(_num(ev.get("start")) - start) < 0.02:
            return str(ev.get("id") or f"w{start:.3f}")
    return f"w{start:.3f}"


def window_inputs(doc: Mapping[str, Any], start: float, end: float,
                  *, fade_style: str = "") -> dict[str, Any]:
    """这一段要烧出像素**真正吃进去**的全部输入（规范化后的纯数据）。

    这是缓存键的判据本体，所以宁可多算不能少算：少算一个输入 = 拿旧片冒充新片。
    各层的输出坐标一律换成「相对本窗口起点」的偏移，源区间用绝对值——于是这一段
    挪到片子的别处、或别的段改了自己的长度，都不牵连它重烧。
    """
    def _layers(key: str) -> list[dict[str, Any]]:
        out = []
        for item in doc.get(key) or []:
            if not isinstance(item, Mapping) or item.get("start") is None:
                continue
            s = round(_num(item.get("start")), 3)
            e = round(_seg_end(item), 3)
            if e <= start or s >= end:
                continue
            at, to = max(s, start) - start, min(e, end) - start
            shift = max(s, start) - s
            out.append({"id": item.get("id"), "path": item.get("path"),
                        "text": item.get("text"), "style": item.get("style"),
                        "fit": item.get("fit"),
                        "src_start": _shift(item.get("src_start"), shift),
                        "src_end": _shift(item.get("src_end"), shift),
                        "duration": _round(_num(item.get("duration")) - shift
                                           if item.get("duration") is not None else None),
                        "at": round(at, 3), "to": round(to, 3)})
        return sorted(out, key=lambda x: str(x["id"]))

    pictures = []
    for item in doc.get("events") or []:
        if not isinstance(item, Mapping):
            continue
        s = round(_num(item.get("start")), 3)
        e = round(_seg_end(item), 3)
        if e <= start or s >= end:
            continue
        shift = max(s, start) - s
        pictures.append({"id": item.get("id"), "path": item.get("path"),
                         "src_start": _shift(item.get("src_start"), shift),
                         "src_end": _shift(item.get("src_end"), shift),
                         "kind": item.get("kind"),
                         "at": round(max(s, start) - start, 3),
                         "to": round(min(e, end) - start, 3)})
    bgm = doc.get("bgm") if isinstance(doc.get("bgm"), Mapping) else None
    return {"v": CACHE_VERSION, "sec": round(end - start, 3),
            "knobs": {k: doc.get(k) for k in _CACHE_KNOBS},
            "fade": fade_style,
            # 配乐混在每一段自己的声音里（见 _slice_window 的 offset_sec），所以它
            # 必须是缓存输入的一部分：漏算就等于拿旧配乐冒充新配乐。
            "bgm": ({"path": bgm.get("path"), "volume": _round(bgm.get("volume")),
                     "at": round(_num(bgm.get("offset_sec")) + start, 3)}
                    if bgm else None),
            "pictures": sorted(pictures, key=lambda x: str(x["id"])),
            "audio": _layers("audio_events"), "subs": _layers("subtitles"),
            "overlays": _layers("overlay_events")}


def segment_cache_key(window_id: str, inputs: Mapping[str, Any]) -> str:
    """→ 这一段的缓存键（16 位十六进制）。判据见 :func:`window_inputs`。

    键里带段 id 是为了让表与日志能按行找回自己的键；命中判定本身只看输入字节。
    """
    return hashlib.sha256(_dumps({"id": window_id, **inputs})
                          .encode("utf-8")).hexdigest()[:16]


def windows(doc: Mapping[str, Any], *, fade: Mapping[Any, Any] | None = None
            ) -> list[tuple[str, float, float, dict[str, Any], str]]:
    """→ [(窗口 id, 起, 止, 输入清单, 缓存键)]，按时间顺序。

    缓存渲染与「哪些段必须重烧」的判定共用这一份，两条口径不可能各说各话。
    """
    out = []
    for i, (s, e) in enumerate(window_boundaries(doc)):
        sid = window_id_at(doc, s)
        inputs = window_inputs(doc, s, e, fade_style=str((fade or {}).get(i) or ""))
        out.append((sid, s, e, inputs, segment_cache_key(sid, inputs)))
    return out


# ---------------------------------------------------------------------------
# 出口：命中表（给前端画选区、给补丁验指纹的自足文件）
# ---------------------------------------------------------------------------

def subtitle_box(doc: Mapping[str, Any], sub: Mapping[str, Any]) -> dict[str, float]:
    """字幕带的**估算**框：按排版公式算，不声称量过（box_precision=estimated）。

    公式与 ``core_nodes._subtitle_layers`` 一一对应：字号 ``max(20, h//22)``、宽 ``0.9w``
    居中、底边留 ``fs//2``、行数按「每行大约 ``w*0.9/fs`` 个字」估（TextClip 用
    method=caption 自己折行，Python 侧拿不到真实行高）。这张框用来「让你点中这一条字幕」，
    不用来宣称像素边界在哪。
    """
    w, h = _num(doc.get("width"), 1280), _num(doc.get("height"), 720)
    if h <= 0:
        h = 720.0
    fs = max(20.0, h // 22)
    per_line = max(1.0, (w * 0.9) / fs if w else 20.0)
    lines = max(1, -(-len(str(sub.get("text") or "")[:40]) // int(per_line)))
    band = lines * fs * 1.3 + fs / 2
    y = max(0.0, min((h - band) / h, 0.99))
    return {"x": 0.05, "y": round(y, 4), "w": 0.9,
            "h": round(min(band / h, 1 - y), 4)}


def _full_box() -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def item_name(item: Mapping[str, Any]) -> str:
    """给这一行一个人在看的名字（素材文件名，没有就用它的文字）。"""
    path = str(item.get("path") or "")
    if path:
        return Path(path).name
    text = str(item.get("text") or "").strip()
    return text[:16] if text else "—"


def entries_for_item(doc: Mapping[str, Any], track: str, index: int,
                     item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一段 → 条目清单（每个可改字段一条）。

    一行一个字段是刻意的：前端点中一块区域后要问的是「这个值改成多少」，
    一条多字段的记录就得再拆一次表单，而拆开的位置在两端各写一遍必然对不齐。
    """
    box = (subtitle_box(doc, item) if track == "subtitles" else _full_box())
    precision = "estimated" if track == "subtitles" else "derived"
    out = []
    for field in EDITABLE.get(track, {}):
        out.append({
            "id": f"{track}-{index}-{field}", "segment_id": item.get("id"),
            "track": track, "index": index, "role": _ROLE[track],
            "field": f"/{track}/{index}/{field}",
            "label": f"{TRACK_LABELS[track]} {index + 1} · {item_name(item)} · {field}",
            "text": str(item.get("text") or ""),
            "value": item.get(field), "box": box, "box_precision": precision,
            "confidence": "exact"})
    return out


def build_table(doc: Mapping[str, Any], *, frames: Mapping[str, str] | None = None,
                cached: Mapping[str, bool] | None = None,
                cache_keys: Mapping[str, str] | None = None,
                fade_styles: Mapping[Any, Any] | None = None,
                errors: list[str] | None = None) -> dict[str, Any]:
    """时间线 → 自足命中表（``segment-hitmap/1``）。

    文件必须**自足**：除了行与指针，还带上**这一版时间线本身**与它的指纹。少了指纹与
    doc，局部改就无从判断这张表是不是当前这版量出来的，只能回头拿会话产物猜「这一版
    当初渲的是什么」——而那份可能被后来的时间线覆盖过。

    ``timeline`` / ``shots`` 两个键名与图形科普片那条链的表**刻意保持一致**：前端的
    时间线与条目面板只认这两个名字，两条链因此共用同一个编辑器，差别只在有没有
    空间命中层（那份由 ``box_precision`` 与 ``schema`` 告诉它）。
    """
    rows: list[dict[str, Any]] = []
    shots: dict[str, Any] = {}
    for track in TRACK_LABELS:
        for i, item in enumerate(doc.get(track) or []):
            if not isinstance(item, Mapping):
                continue
            sid = str(item.get("id") or f"{track[:2]}{i + 1:03d}")
            start = _num(item.get("start"))
            rows.append({"id": sid, "track": track, "kind": track, "index": i,
                         "start_sec": round(start, 3),
                         "sec": round(_seg_end(item) - start, 3),
                         "label": item_name(item),
                         "frame": (frames or {}).get(sid) or "",
                         "cached": bool((cached or {}).get(sid)),
                         "cache_key": (cache_keys or {}).get(sid) or ""})
            entries = entries_for_item(doc, track, i, item)
            shots[sid] = {"shot": sid, "track": track, "entries": entries,
                          "resolved": len(entries)}
    payload = {
        "schema": "segment-hitmap/1",
        "doc": doc,
        "doc_fingerprint": fingerprint(doc),
        "width": int(_num(doc.get("width"), 1280)),
        "height": int(_num(doc.get("height"), 720)),
        "fps": _num(doc.get("fps"), 25.0),
        "duration": _num(doc.get("duration")),
        "box_unit": "normalized_stage",
        "hit_mode": "track",                 # 前端据此关掉空间命中层
        "tracks": [{"key": t, "label": TRACK_LABELS[t],
                    "rows": sum(1 for r in rows if r["track"] == t)}
                   for t in TRACK_LABELS],
        "timeline": sorted(rows, key=lambda r: (r["start_sec"], r["track"])),
        "shots": shots,
    }
    if fade_styles:
        payload["fade_styles"] = {str(k): v for k, v in fade_styles.items()}
    if errors:
        payload["errors"] = list(errors)
    return payload


# ---------------------------------------------------------------------------
# 补丁：按指针落笔
# ---------------------------------------------------------------------------

def read_pointer(doc: Mapping[str, Any], pointer: str) -> Any:
    """取指针当前值（表单要显示「现在是什么」）。不存在就抛，不静默回 None。"""
    track, idx, field = _parse_pointer(pointer)
    items = doc.get(track) or []
    if idx >= len(items):
        raise KeyError(f"{pointer}：{TRACK_LABELS[track]}只有 {len(items)} 段")
    if field is None:
        return items[idx]
    if field not in (items[idx] or {}):
        raise KeyError(f"{pointer}：这一段没有 {field} 字段")
    return items[idx][field]


def _parse_pointer(pointer: str) -> tuple[str, int, str | None]:
    m = _POINTER_RE.match(str(pointer or ""))
    if not m:
        raise ValueError(
            f"指针不合法：{pointer!r}（必须逐字取自命中表条目的 field，"
            "形如 /events/2/src_end 或 /subtitles/7/text）")
    return m.group(1), int(m.group(2)), (m.group(3) or "")[1:] or None


def _type_check(field: str, value: Any, kind: str, pointer: str) -> None:
    if kind == _NUM:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{field} 必须是数字：{pointer} 传了 {type(value).__name__}")
        try:
            float(value)
        except ValueError:
            raise ValueError(f"{field} 必须是数字：{pointer} 传了 {value!r}") from None
    elif not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串：{pointer}")


def apply_patch(doc: dict[str, Any], pointer: str, value: Any, *,
                expect_id: str | None = None) -> dict[str, Any]:
    """按 JSON 指针改时间线，返回**新对象**（不改入参——时间线是已入库的产物）。

    画面段改 ``src_start``/``src_end`` 时**成对生效、长度不变**：选的是「取源片哪一截」，
    而不是「这一段在成片里占多长」。理由是输出长度一变，后面每一段（以及钉在它们
    上面的口播声音）都要重排——那不是选区改，是重新规划。所以改一端时另一端按原长度
    跟着平移，用户拿到的是「同样的时长、换了取景位置」。
    """
    track, idx, field = _parse_pointer(pointer)
    doc = copy.deepcopy(doc)
    items = doc.get(track) or []
    if idx >= len(items):
        raise ValueError(
            f"指针指向不存在的第 {idx + 1} 段{TRACK_LABELS[track]}"
            f"（这条时间线只有 {len(items)} 段）：{pointer}")
    item = items[idx]
    if expect_id and str(item.get("id") or "") != expect_id:
        raise ValueError(
            f"指针与段号对不上：{pointer} 落在 id {item.get('id') or '（没有 id）'} 上，"
            f"而你说要改 {expect_id}——这张命中表量的不是当前这一版，请重新出片")
    if field is None:
        raise ValueError(f"指针没有落到字段上：{pointer}")
    rules = (EDITABLE.get(track) or {}).get(field)
    if rules is None:
        raise ValueError(
            f"{TRACK_LABELS[track]}的 {field} 不在可改白名单里：{pointer}"
            f"（可改：{', '.join(EDITABLE.get(track) or {})}。"
            "输出区间 start/end 由排列推出，要改顺序/删段请回计划卡重新规划——"
            "那会把画面挪到别的口播句子底下）")
    _type_check(field, value, rules, pointer)
    if field not in item and field != "kind":
        raise ValueError(
            f"指针末端不是已有字段（新增字段请整段改写，别蒙一个不存在的键）：{pointer}")
    value = float(value) if rules == _NUM else value
    item[field] = value
    if track == "events" and field in ("src_start", "src_end"):
        length = _num(item.get("src_end"), 0.0) - _num(item.get("src_start"), 0.0)
        if length > 0.02:
            keep = _num(item["end"]) - _num(item["start"])
            if abs(length - keep) > 0.005:      # 另一端按原长度平移回去
                if field == "src_start":
                    item["src_end"] = round(_num(item["src_start"]) + keep, 3)
                else:
                    item["src_start"] = round(_num(item["src_end"]) - keep, 3)
    return doc


def plan_patch(doc: dict[str, Any], *, edits: Any = ()) -> tuple[dict[str, Any],
                 dict[str, Any]]:
    """一批指针补丁 → 新时间线 + 改动账。

    与图形科普片那条链同一条口径：**一次说完**、空补丁拒。空补丁要是放过，
    「点了一下没改任何字」也会分叉出一版新片子，而用户以为改好了。
    """
    edits = list(edits or ())
    if not edits:
        raise ValueError(
            "局部改：edits 一条都没给。没有改动就不必分叉出一版新片子；"
            "要改顺序或删段，请回计划卡重新规划（那会打乱画面与口播句子的对应）")
    out = copy.deepcopy(doc)
    changed: dict[str, set[str]] = {t: set() for t in TRACK_LABELS}
    touched: dict[str, set[str]] = {t: set() for t in TRACK_LABELS}
    for e in edits:
        if not isinstance(e, Mapping):
            raise ValueError(f"补丁条目必须是对象，收到 {type(e).__name__}")
        pointer = str(e.get("pointer") or "")
        track, idx, _field = _parse_pointer(pointer)
        items = out.get(track) or []
        if idx >= len(items):
            raise ValueError(f"指针指向不存在的段：{pointer}")
        touched[track].add(str(items[idx].get("id") or ""))
        out = apply_patch(out, pointer, e.get("value"),
                          expect_id=str(e.get("segment_id") or "") or None)
        changed[track].add(str(items[idx].get("id") or ""))
    report = {"changed": {k: sorted(v) for k, v in changed.items() if v},
              "changed_ids": sorted(x for v in changed.values() for x in v),
              "touched_ids": sorted(x for v in touched.values() for x in v),
              "edits": len(edits), "reordered": False, "removed_ids": []}
    return out, report


def expected_rebuild_ids(old: Mapping[str, Any], new: Mapping[str, Any], *,
                         fade: Mapping[Any, Any] | None = None,
                         new_fade: Mapping[Any, Any] | None = None) -> list[str]:
    """→ 输入真的变了、因而必须重烧的窗口 id 清单。

    判据不是「哪些段被点名改过」，而是**逐窗口比对输入清单**（与渲染器算缓存键用的是
    同一个 :func:`window_inputs`）。所以改一句字幕时，这里给出的不是那条字幕本身，
    而是它压在的那一（跨边界就两）个画面窗口——这正是「重烧范围与改动等价」的
    可核对说法，也是证据里那条「其余段直接复用已有切片」的出处。
    """
    before = {sid: inputs for sid, _s, _e, inputs, _k in windows(old, fade=fade)}
    return [sid for sid, _s, _e, inputs, _k in windows(new, fade=new_fade)
            if before.get(sid) != inputs]
