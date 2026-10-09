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

贯穿全部设计的三条红线：
① **改错了地方还不报错**是这条链最贵的失败模式——指针必须逐字取自表、末端必须是
   白名单里的已有字段、段 id 与指针不符就整批拒收。
② **一次改动不许悄悄改掉总长与音画对应**——所以这里只接受「不改长度」的编辑：
   改字、改样式（字幕的颜色/字号/位置）、改盖层、换画面取的区间、换素材、
   逐段音量、配乐音量与起播偏移。要改顺序或删段，会把画面挪到别的口播句子底下
   （声音是不动的基准），那是重新规划，不是选区改，明确拒收。
③ 用户**亲手拧过**的那一段（:data:`MANUAL_FIELDS`）会带上 ``manual`` 标记：渲染前的
   自动校准不再把它钉回声音的位置，同步硬闸也不再判它——「按用户手改」这件事
   必须在账上看得见，而不是靠闸替用户解释。
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
#: "2"：窗口输入里长了每段音量（audio_events.volume）与字幕样式三件套
#: （color/font_size/position）。它们都真的改变字节，而旧键存的切片是按
#: 「这些旋钮不存在」的默认值烧的——不失效就等于拿默认样子的旧片冒充改过样式的新片。
CACHE_VERSION = "2"

TRACK_LABELS = {
    "events": "画面",
    "audio_events": "声音",
    "subtitles": "字幕",
    "overlay_events": "覆盖层",
}

#: 配乐不是列表轨：整片只有一份 ``tl["bgm"] = {path, volume, offset_sec}``，可它要在
#: 时间线上和别的轨并列点选、要能按指针改，所以给它一个**伪轨**（下标恒 0、段号恒 "bgm"）。
#: 指针写法因此少一段：``/bgm/volume`` 而不是 ``/bgm/0/volume``。
BGM_TRACK = "bgm"
BGM_LABEL = "配乐"

_NUM, _STR = "number", "string"
_COLOR, _ENUM = "color", "enum"

#: 音量是**倍率**不是百分比：1.0 = 原样，2.0 已够顶到人声之上，再高只会削顶爆音。
_VOLUME_MAX = 2.0
#: 字号只收这个区间：更小的看不见，更大的渲染端还要按画面高度再夹一次，
#: 与其让两处各说各话，不如在入口就拒。
_FONT_SIZE_RANGE = (8.0, 400.0)
SUBTITLE_POSITIONS = ("bottom", "center", "top")
_ENUMS: dict[str, tuple[str, ...]] = {"position": SUBTITLE_POSITIONS}

#: 颜色只认两种写法：#RGB/#RGBA/#RRGGBB/#RRGGBBAA，以及这一小撮 X11 名字。
#: 别的字符串会原样进 Pillow 的 color 参数，而 Pillow 对认不出的颜色**抛异常**——
#: 那会让整条字幕链一条都生成不出来（见 core_nodes._subtitle_layers 的教训）。
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_COLOR_WORDS = frozenset({
    "white", "black", "yellow", "red", "green", "blue", "orange", "pink", "purple",
    "cyan", "magenta", "gray", "grey", "silver", "gold", "beige", "brown", "lime",
    "teal", "navy", "violet", "ivory", "khaki", "salmon", "turquoise", "wheat",
})

#: 每条轨的可改字段白名单（字段 → 类型）。不在名单里的字段一律拒收，理由见模块开头。
#:
#: 为什么 ``events`` 没有 ``start``/``end``：画面段的输出区间是「排在前面的段一共多长」
#: 推出来的**结果**，不是可以直接拧的旋钮；拧它等于把后面所有段连声轨一起挪走。
#: 为什么 ``events`` 的 ``src_start``/``src_end`` 成对生效：见 :func:`apply_patch`——
#: 选的是「取源片哪一截」，长度不变，所以不牵连任何别的段。
#: 为什么 ``audio_events`` 也能裁源区间：用户要的「逐段裁切素材原声」正是这件事。
#: 它同样**不改输出长度**，所以时钟基准没动；动了的是这一段播的是源文件的哪一截，
#: 而画面会按新声音重新钉上去（resync）——嘴还是对得上，只是话说的是那一段。
#: ``volume`` 同理：只改这一段多响，不改它在哪一秒。
EDITABLE: dict[str, dict[str, str]] = {
    "events": {"path": _STR, "src_start": _NUM, "src_end": _NUM, "kind": _STR},
    "audio_events": {"path": _STR, "src_start": _NUM, "src_end": _NUM,
                     "volume": _NUM},
    "subtitles": {"text": _STR, "style": _STR, "start": _NUM, "end": _NUM,
                  "color": _COLOR, "font_size": _NUM, "position": _ENUM},
    "overlay_events": {"path": _STR, "start": _NUM, "end": _NUM,
                       "src_start": _NUM, "src_end": _NUM, "fit": _STR,
                       "text": _STR},
}

BGM_EDITABLE: dict[str, str] = {"path": _STR, "volume": _NUM, "offset_sec": _NUM}

#: 影响一段画面**像素与声音**的顶层旋钮。BGM 不在这里：它按窗口混进切片，
#: 所以走 :func:`window_inputs` 里的 bgm 输入项，而不是靠整片失效。
_CACHE_KNOBS = ("width", "height", "fps", "mode")

#: 这些字段允许「本来没有、改的时候才长出来」：缺省就是渲染端一直在用的默认值，
#: 所以补上它们不改变字节，只是把用户拧的那一格显式写进时间线。
CREATABLE: dict[str, frozenset[str]] = {
    "audio_events": frozenset({"src_start", "src_end", "volume"}),
    "subtitles": frozenset({"color", "font_size", "position"}),
    BGM_TRACK: frozenset(BGM_EDITABLE),
}

#: 动过这些字段 = 用户亲自动了这一段的声音或取景。渲染前的**自动校准**与**同步硬闸**
#: 都得对它让开（见 core_nodes 的 resync_original_audio_timeline / av_sync_check）：
#: 否则用户刚把画面挪到想看的机位，校准就把它钉回声音的位置，改动静默消失。
MANUAL_FIELDS: dict[str, frozenset[str]] = {
    "events": frozenset({"src_start", "src_end", "path"}),
    "audio_events": frozenset({"src_start", "src_end", "volume", "path"}),
    "overlay_events": frozenset({"src_start", "src_end", "path"}),
}
MANUAL_MARK = "按用户手改"

#: 数字字段的取值区间（None = 不设那一头的限）。入口就拒，不留到渲染端偷偷夹。
_NUMBER_RANGE: dict[str, tuple[float | None, float | None]] = {
    "volume": (0.0, _VOLUME_MAX),
    "font_size": _FONT_SIZE_RANGE,
    "offset_sec": (0.0, None),
    "src_start": (0.0, None),
    "src_end": (0.0, None),
    "start": (0.0, None),
    "end": (0.0, None),
}

_POINTER_RE = re.compile(r"^/(events|audio_events|subtitles|overlay_events)/(\d+)(/[^/]+)?$")
_BGM_POINTER_RE = re.compile(r"^/bgm/([^/]+)$")

_ROLE = {"events": "segment-window", "audio_events": "voice-window",
         "subtitles": "caption", "overlay_events": "overlay-window",
         BGM_TRACK: "bgm-control"}


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
                        # 音量与字幕样式三件套都会真的改变这一窗的字节，所以必须进输入；
                        # manual（按用户手改）只是给闸与账看的，不改变字节，刻意不算。
                        "volume": _round(item.get("volume")),
                        "color": item.get("color"),
                        "font_size": _round(item.get("font_size")),
                        "position": item.get("position"),
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

def subtitle_default_font_size(height: float) -> float:
    """渲染端一直在用的默认字号公式（``max(20, h//22)``）。

    单源在这里、``core_nodes._subtitle_layers`` 调它：命中表算的框与真的画出来的
    字号必须同一个公式，否则用户点中的是上一行还是下一行全靠运气。
    """
    return max(20.0, float(height) // 22)


def subtitle_font_size(doc: Mapping[str, Any], sub: Mapping[str, Any]) -> float:
    """这一条字幕**实际**用的字号：给了就夹到 ``[8, h]``，没给就退到默认公式。

    上限按画面高度夹：字号大过画布高度时 Pillow 会把文本盒撑爆，MoviePy 直接抛错，
    而那条错在 ``_subtitle_layers`` 里是「丢这一层」——不夹就等于悄悄丢字幕。
    """
    h = _num(doc.get("height"), 720)
    if h <= 0:
        h = 720.0
    got = sub.get("font_size")
    fs = subtitle_default_font_size(h) if got is None else _num(got, subtitle_default_font_size(h))
    return round(min(max(fs, 8.0), h), 1)


def subtitle_box(doc: Mapping[str, Any], sub: Mapping[str, Any]) -> dict[str, float]:
    """字幕带的**估算**框：按排版公式算，不声称量过（box_precision=estimated）。

    公式与 ``core_nodes._subtitle_layers`` 一一对应：字号取
    :func:`subtitle_font_size`（用户没改就是 ``max(20, h//22)``）、宽 ``0.9w`` 居中、
    底边留 ``fs//2``、行数按「每行大约 ``w*0.9/fs`` 个字」估（TextClip 用
    method=caption 自己折行，Python 侧拿不到真实行高）。``position`` 决定这条带落在
    屏幕的下/中/上。这张框用来「让你点中这一条字幕」，不用来宣称像素边界在哪。
    """
    w, h = _num(doc.get("width"), 1280), _num(doc.get("height"), 720)
    if h <= 0:
        h = 720.0
    fs = subtitle_font_size(doc, sub)
    per_line = max(1.0, (w * 0.9) / fs if w else 20.0)
    lines = max(1, -(-len(str(sub.get("text") or "")[:40]) // int(per_line)))
    band = lines * fs * 1.3 + fs / 2
    where = str(sub.get("position") or "bottom")
    if where == "top":
        top = h * 0.05
    elif where == "center":
        top = (h - band) / 2
    else:
        top = h - band
    top = max(0.0, min(top, h - 1.0))
    return {"x": 0.05, "y": round(top / h, 4), "w": 0.9,
            "h": round(min(band / h, 1 - top / h), 4)}


def _full_box() -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def bgm_present(doc: Mapping[str, Any]) -> bool:
    """这一版有没有**能改**的配乐：``bgm`` 存在且带着文件引用。"""
    bgm = doc.get(BGM_TRACK)
    return isinstance(bgm, Mapping) and bool(bgm.get("path"))


def field_default(doc: Mapping[str, Any], track: str, field: str,
                  item: Mapping[str, Any]) -> Any:
    """这一栏**没写值时渲染端实际吃进去的那个数**。

    CREATABLE 那几栏允许时间线里根本没有（缺省=渲染默认）。命中表若只发 ``value=None``，
    前端的滑杆和色块就只能拿最小值去画：音量标着 0、字幕色块标着黑，而片子里是 1.0× 白字——
    用户照着这个读数去改，改完发现「变大了」其实是回到了默认。所以默认值由这一处单源发出去，
    取的就是 ``core_nodes`` 渲染分支里那几个 ``or`` / ``get(..., 默认)``。
    """
    if track == "subtitles":
        if field == "color":
            return "white"
        if field == "position":
            return "bottom"
        if field == "font_size":
            return subtitle_font_size(doc, item)
    if track == "audio_events" and field == "volume":
        return 1.0
    if track == BGM_TRACK and field == "volume":
        return 0.2
    return None


def bgm_entries(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """配乐伪轨 → 条目清单（音量 / 起播偏移 / 曲子本身，一格一条）。

    段号恒为 ``bgm``：整片只有一份配乐，它没有「第几段」，也就没有按位置寻址的那层风险。
    框给满屏（``derived``）——它是时间轴上的一条带，不是屏幕上的一个区域，
    前端的配乐轨按行的起止画，读不到这个框。
    """
    if not bgm_present(doc):
        return []
    bgm = doc[BGM_TRACK]
    name = Path(str(bgm.get("path") or "")).name
    return [{"id": f"{BGM_TRACK}-0-{field}", "segment_id": BGM_TRACK,
             "track": BGM_TRACK, "index": 0, "role": _ROLE[BGM_TRACK],
             "field": f"/{BGM_TRACK}/{field}",
             "label": f"{BGM_LABEL} · {name} · {field}",
             "text": "", "value": bgm.get(field), "box": _full_box(),
             "default": field_default(doc, BGM_TRACK, field, bgm),
             "box_precision": "derived", "confidence": "exact"}
            for field in BGM_EDITABLE]


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
            "value": item.get(field), "default": field_default(doc, track, field, item),
            "box": box, "box_precision": precision,
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
    track_keys = list(TRACK_LABELS)
    if bgm_present(doc):
        # 配乐伪轨：一行从 offset_sec 铺到片尾，三条可点字段。没有这一行，前端的
        # 配乐 lane 就只能靠猜——而猜出来的指针落不到任何字段上（红线①的老路）。
        bgm = doc[BGM_TRACK]
        off = round(_num(bgm.get("offset_sec")), 3)
        entries = bgm_entries(doc)
        rows.append({"id": BGM_TRACK, "track": BGM_TRACK, "kind": BGM_TRACK,
                     "index": 0, "start_sec": off,
                     "sec": round(max(0.0, _num(doc.get("duration")) - off), 3),
                     "label": item_name(bgm), "frame": "", "cached": False,
                     "cache_key": ""})
        shots[BGM_TRACK] = {"shot": BGM_TRACK, "track": BGM_TRACK,
                            "entries": entries, "resolved": len(entries)}
        track_keys.append(BGM_TRACK)
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
        # 字段名**连类型**一起发：CREATABLE 那些栏缺省时原值是 None，前端只能猜，
        # 猜错就把音量写成字符串——入口拒收，用户看到的只是「这一格改不动」。
        # 类型本来就是 EDITABLE 的值，这里原样给出去，不另写一份。
        "editable_fields": {t: dict(EDITABLE.get(t) or {}) for t in TRACK_LABELS}
                           | {BGM_TRACK: dict(BGM_EDITABLE)},
        # 表单旋钮的取值范围与字段清单同源：前端重写一遍数字，迟早和入口校验对不上，
        # 而对不上的代价是「滑到 2.5 被拒收，界面却标着 3」。
        "controls": {"volume_max": _VOLUME_MAX,
                     "font_size_range": list(_FONT_SIZE_RANGE),
                     "subtitle_positions": list(SUBTITLE_POSITIONS)},
        "tracks": [{"key": t, "label": TRACK_LABELS.get(t, BGM_LABEL),
                    "rows": sum(1 for r in rows if r["track"] == t)}
                   for t in track_keys],
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
    """取指针当前值（表单要显示「现在是什么」）。段不存在就抛，不静默回 None。

    「这一格本来没写」不是错误：CREATABLE 里的字段缺省就是渲染端一直在用的默认值，
    表单要能显示空白并让人填，所以回 None（读整段仍照原样给对象）。
    """
    track, idx, field = _parse_pointer(pointer)
    if track != BGM_TRACK and idx >= len(doc.get(track) or []):
        raise KeyError(
            f"{pointer}：这条时间线没有第 {idx + 1} 段"
            f"{TRACK_LABELS.get(track, track)}（表单不能把它显示成「这一格是空的」）")
    _track, item, _field = _locate(doc, pointer)
    if field is None:
        return item
    if field not in item:
        if field in CREATABLE.get(track, frozenset()):
            return None
        raise KeyError(f"{pointer}：这一段没有 {field} 字段")
    return item[field]


def _locate(doc: Mapping[str, Any], pointer: str) -> tuple[str, Mapping[str, Any], str]:
    """指针 → (轨, 那一段本身, 字段)。列表轨与配乐伪轨共用这一个入口。

    返回的是 doc **内部那个对象**（apply_patch 先 deepcopy 再调它，所以改动会落到副本上）。
    """
    track, idx, field = _parse_pointer(pointer)
    if track == BGM_TRACK:
        bgm = doc.get(BGM_TRACK)
        if not isinstance(bgm, Mapping):
            raise ValueError(
                f"{pointer}：这一版时间线没有配乐。要加或换曲子请回计划卡选曲"
                "（select_BGM）或在出片时带 bgm——这里不凭空造一条声音")
        return track, bgm, field or ""
    items = doc.get(track) or []
    if idx >= len(items):
        raise ValueError(
            f"指针指向不存在的第 {idx + 1} 段{TRACK_LABELS[track]}"
            f"（这条时间线只有 {len(items)} 段）：{pointer}")
    item = items[idx]
    if not isinstance(item, Mapping):
        raise ValueError(f"指针指的不是一个对象：{pointer}")
    return track, item, field or ""


def _allowed(track: str) -> str:
    return ", ".join(sorted(BGM_EDITABLE if track == BGM_TRACK
                            else (EDITABLE.get(track) or {})))


def _type_check(field: str, value: Any, kind: str, pointer: str) -> None:
    """按字段类型校验取值，越界就拒。宁可在入口说清楚，也不让渲染端偷偷夹一个数。"""
    if kind == _NUM:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{field} 必须是数字：{pointer} 传了 {type(value).__name__}")
        try:
            num = float(value)
        except ValueError:
            raise ValueError(f"{field} 必须是数字：{pointer} 传了 {value!r}") from None
        lo, hi = _NUMBER_RANGE.get(field, (None, None))
        if (lo is not None and num < lo) or (hi is not None and num > hi):
            span = f"{lo if lo is not None else '不限'}~" \
                   f"{hi if hi is not None else '不限'}"
            raise ValueError(f"{field} 超出可取范围 {span}：{pointer} 传了 {num}")
        return
    if kind == _COLOR:
        text = str(value or "").strip()
        if not (_COLOR_RE.match(text) or text.lower() in _COLOR_WORDS):
            raise ValueError(
                f"{field} 只认 #RRGGBB（或 #RGB / 带透明度 / 常见颜色名）："
                f"{pointer} 传了 {value!r}——别的写法 Pillow 认不出会让整条字幕链"
                "一条都生成不出来")
        return
    if kind == _ENUM:
        allowed = _ENUMS.get(field, ())
        if str(value or "").strip() not in allowed:
            raise ValueError(
                f"{field} 只能是 {'/'.join(allowed)} 之一：{pointer} 传了 {value!r}")
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串：{pointer}")


def apply_patch(doc: dict[str, Any], pointer: str, value: Any, *,
                expect_id: str | None = None) -> dict[str, Any]:
    """按 JSON 指针改时间线，返回**新对象**（不改入参——时间线是已入库的产物）。

    画面/声音段改 ``src_start``/``src_end`` 时**成对生效、长度不变**：选的是「取源片
    哪一截」，而不是「这一段在成片里占多长」。理由是输出长度一变，后面每一段（以及
    钉在它们上面的口播声音）都要重排——那不是选区改，是重新规划。所以改一端时另一端
    按原长度跟着平移，用户拿到的是「同样的时长、换了取景位置」。

    动过 MANUAL_FIELDS 里的字段就给这一段打上 :data:`MANUAL_MARK`：那是「这一段的
    声音/取景由用户亲手拧过」的凭据，渲染前的自动校准与同步硬闸据此让路（见
    ``core_nodes.resync_original_audio_timeline`` / ``av_sync_check``）。不打这个标记，
    用户刚把画面挪到想看的机位，校准就把它钉回声音的位置——改动静默消失，而这正是
    「改错了地方还不报错」那一类最贵的失败。
    """
    track, idx, field = _parse_pointer(pointer)
    if field is None:
        raise ValueError(f"指针没有落到字段上：{pointer}")
    doc = copy.deepcopy(doc)
    track, item, _ = _locate(doc, pointer)
    if track == BGM_TRACK:
        if expect_id and str(item.get("id") or BGM_TRACK) != expect_id:
            raise ValueError(
                f"指针与段号对不上：{pointer} 指的是配乐，而你说的段号是 {expect_id}"
                "——这张命中表量的不是当前这一版，请重新出片")
    else:
        if expect_id and str(item.get("id") or "") != expect_id:
            raise ValueError(
                f"指针与段号对不上：{pointer} 落在 id {item.get('id') or '（没有 id）'} 上，"
                f"而你说要改 {expect_id}——这张命中表量的不是当前这一版，请重新出片")
    rules = (BGM_EDITABLE if track == BGM_TRACK
             else (EDITABLE.get(track) or {})).get(field)
    if rules is None:
        raise ValueError(
            f"{TRACK_LABELS.get(track, BGM_LABEL)}的 {field} 不在可改白名单里：{pointer}"
            f"（可改：{_allowed(track)}。"
            "画面/声音段的输出区间 start/end 由排列推出，要改顺序/删段请回计划卡重新规划——"
            "那会把画面挪到别的口播句子底下）")
    _type_check(field, value, rules, pointer)
    if field not in item and field != "kind" and field not in CREATABLE.get(track, frozenset()):
        raise ValueError(
            f"指针末端不是已有字段（新增字段请整段改写，别蒙一个不存在的键）：{pointer}")
    item[field] = float(value) if rules == _NUM else (
        str(value).strip() if rules in (_COLOR, _ENUM, _STR) else value)
    if track in ("events", "audio_events") and field in ("src_start", "src_end"):
        keep = round(_seg_end(item) - _num(item.get("start")), 3)
        if item.get("src_start") is None:
            item["src_start"] = round(_num(item["src_end"]) - keep, 3)
        elif item.get("src_end") is None:
            item["src_end"] = round(_num(item["src_start"]) + keep, 3)
        else:
            length = _num(item["src_end"]) - _num(item["src_start"])
            # 另一端按原长度平移回去。塌成零/负也算需要平移：编辑器拖一个把手只会发
            # **一格**指针（只给 src_start 或只给 src_end），这时新长度可能已经是 0 甚至
            # 负数——放过它就落出一段零长度的源段，渲到那儿 MoviePy 在合成音频时直接抛
            # `zero-size array to reduction operation minimum`（真跑出来的崩法）。
            if length <= 0.02 or abs(length - keep) > 0.005:
                if field == "src_start":
                    item["src_end"] = round(_num(item["src_start"]) + keep, 3)
                else:
                    item["src_start"] = round(_num(item["src_end"]) - keep, 3)
        if _num(item["src_start"]) < 0.0:
            # 另一端**没写过**时补出来的值也可能是负秒（只给 src_end=1.0、这段长 9 秒
            # → 补出 -8.0）。源片没有负时刻，负数只会让切片的取法不明；整窗顶回 0 起，
            # 长度仍是 keep。三条推导路径都要过这一道。
            item["src_start"] = 0.0
            item["src_end"] = keep
    if field in MANUAL_FIELDS.get(track, frozenset()):
        item["manual"] = MANUAL_MARK
    return doc


def _parse_pointer(pointer: str) -> tuple[str, int, str | None]:
    m = _POINTER_RE.match(str(pointer or ""))
    if not m:
        bgm = _BGM_POINTER_RE.match(str(pointer or ""))
        if bgm:
            return BGM_TRACK, 0, bgm.group(1)
        raise ValueError(
            f"指针不合法：{pointer!r}（必须逐字取自命中表条目的 field，"
            "形如 /events/2/src_end、/subtitles/7/text 或 /bgm/volume）")
    return m.group(1), int(m.group(2)), (m.group(3) or "")[1:] or None


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
    changed: dict[str, set[str]] = {t: set() for t in list(TRACK_LABELS) + [BGM_TRACK]}
    touched: dict[str, set[str]] = {t: set() for t in list(TRACK_LABELS) + [BGM_TRACK]}
    manual: set[str] = set()
    for e in edits:
        if not isinstance(e, Mapping):
            raise ValueError(f"补丁条目必须是对象，收到 {type(e).__name__}")
        pointer = str(e.get("pointer") or "")
        track, _idx, field = _parse_pointer(pointer)
        _t, item, _f = _locate(out, pointer)     # 不存在在这里就拒，不等到落笔才发现
        sid = BGM_TRACK if track == BGM_TRACK else str(item.get("id") or "")
        touched[track].add(sid)
        out = apply_patch(out, pointer, e.get("value"),
                          expect_id=str(e.get("segment_id") or "") or None)
        changed[track].add(sid)
        if field in MANUAL_FIELDS.get(track, frozenset()):
            manual.add(sid)
    report = {"changed": {k: sorted(v) for k, v in changed.items() if v},
              "changed_ids": sorted(x for v in changed.values() for x in v),
              "touched_ids": sorted(x for v in touched.values() for x in v),
              # 手改段单独一栏：闸与校准对它们是「让路」而不是「没活干」，这两件事
              # 必须能在账上分开说清楚（见 av_sync_check 的说明）。
              "manual_ids": sorted(manual),
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
