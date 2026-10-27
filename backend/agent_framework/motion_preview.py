# -*- coding: utf-8 -*-
"""调参台的预览一步：拿一版分镜，改几个字段值，**只渲这一镜的几帧**。

为什么单独一份而不是塞进 server.py：这里的三件事——「这一镜有哪些旋钮」「改值要过
同一份校验」「全片时钟算到第几秒」——都是能离线验的纯逻辑。放进 HTTP 处理函数里，
它们就只剩真机一条路可验，而这条路上每次验证要烧几百毫秒像素。

与 ``code_render.preview_stills`` 的关系：那一条**整片**每镜出一张（计划卡用），这一条
**一镜**出两三张（改一个值看一眼用）。两者共用同一套 props 组装与帧数估算，所以
「调参台上看到的」与「出片时渲的」不会漂成两件事。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

# ``open_storyline`` 的导入**必须在函数里**：`open_storyline.providers` 会回头
# 导入 `agent_framework.llm_openai`，而后者在 `agent_framework/__init__` 里、与
# `server.py`（本模块的调用方）同一条导入链上。写在文件顶部就是一个循环导入——
# 2026-10-21 重建容器时 editor 起不来，报的正是
# `cannot import name 'Providers' from partially initialized module`。
# 与本仓 `agent.py` 里那处 ``from open_storyline.motion.shot_writer import …``
# 同一写法：真正用到的那一刻才导入。


def _cspec():
    from open_storyline.motion import code_spec as m
    return m


def _crender():
    from open_storyline.motion import code_render as m
    return m


def _rrender():
    from open_storyline.motion import render as m
    return m


def _mediaops():
    from open_storyline import mediaops as m
    return m


def _rclient():
    from open_storyline.motion import remotion_client as m
    return m



class PreviewError(RuntimeError):
    """预览这一步失败时给人看的一句话；``rejected`` 里是逐字段的拒收原因。"""

    def __init__(self, message: str, *, rejected: Sequence[Mapping[str, Any]] = ()):
        super().__init__(message)
        self.rejected = [dict(r) for r in rejected]


def shot_list(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """界面要的逐镜清单：镜号、这句话、以及这一镜交出来的旋钮（声明原样）。

    旋钮的字段名与范围**只从分镜里读**，不在这里编：同一份 ``fields`` 也是出片时
    ``fields.x`` 取值的来源，另立一张表就会出现「台上能调、画面上没这个值」。
    """
    out: list[dict[str, Any]] = []
    for i, shot in enumerate(spec.get("shots") or []):
        if not isinstance(shot, Mapping):
            continue
        sid = str(shot.get("id") or f"s{i + 1:02d}")
        knobs: list[dict[str, Any]] = []
        for f in (shot.get("fields") or []):
            if not isinstance(f, Mapping) or not f.get("name"):
                continue
            row: dict[str, Any] = {
                "name": str(f["name"]),
                "type": str(f.get("type") or "text"),
                "value": f.get("value"),
            }
            for k in ("label", "min", "max", "step", "options"):
                if f.get(k) is not None:
                    row[k] = f[k]
            knobs.append(row)
        out.append({"id": sid, "text": str(shot.get("text") or ""),
                    "fields": knobs})
    return out


def find_shot(spec: Mapping[str, Any], shot_id: str) -> tuple[int, dict[str, Any]]:
    """按镜号找这一镜 → (下标, 副本)。找不到时说清「这一版有哪些镜号」。"""
    want = str(shot_id or "").strip()
    shots = spec.get("shots") or []
    for i, shot in enumerate(shots):
        if not isinstance(shot, Mapping):
            continue
        sid = str(shot.get("id") or f"s{i + 1:02d}")
        if sid == want:
            return i, copy.deepcopy(dict(shot))
    known = "、".join(str((s or {}).get("id") or f"s{i + 1:02d}")
                      for i, s in enumerate(shots) if isinstance(s, Mapping))
    raise PreviewError(f"这一版里没有镜号「{want or '空'}」；有这些：{known}")


def apply_overrides(shot: dict[str, Any],
                    overrides: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any],
                                                           list[dict[str, str]]]:
    """在**副本**上改字段值 → (改过的这一镜, 生效的, 被拒的)。

    校验与入库同源（``cspec.check_field_value`` + ``normalize_field_value``）：
    台上放过一个越界值、到了补丁口才被打回，是最难解释的一种失败——人已经按这个值
    看过三帧、以为成了。归一只做一次，故「41」与「41.0」在台上也是同一件事。
    """
    fields = [dict(f) for f in (shot.get("fields") or [])
              if isinstance(f, Mapping) and f.get("name")]
    by_name = {str(f["name"]): f for f in fields}
    applied: dict[str, Any] = {}
    rejected: list[dict[str, str]] = []
    for name, value in (overrides or {}).items():
        name = str(name)
        field = by_name.get(name)
        if field is None:
            rejected.append({"name": name,
                             "why": "这一镜没有声明这个旋钮（只认分镜里 fields 的名字）"})
            continue
        why = _cspec().check_field_value(field, value)
        if why:
            rejected.append({"name": name, "why": why})
            continue
        field["value"] = _cspec().normalize_field_value(str(field.get("type") or ""), value)
        applied[name] = field["value"]
    shot = {**shot, "fields": fields}
    return shot, applied, rejected


#: 台子上那几栏**整片**的旋钮。只收「改了一眼能看出来、且回执能说清代价」的四个：
#: `palette` 经 `film_style` 进每一镜的底/字/强调色；`caption_style` 与 `highlight_style`
#: 由片壳画字幕；`texture` 跑在切片之后的滤镜上。画幅/帧率/旁白开关不在这里——
#: 改它们等于换一条片子，那是计划卡的事。
#:
#: 为什么不在服务端另编一套控件（范围、默认值、候选）：命中表的 `controls` 就是
#: 计划卡那套旋钮的同一份声明，这里只挑四栏出来照着发。另立一张表，就会在
#: 「卡上能选、台上没有」或反过来「台上有、出片不认」处漂一次。
CONSOLE_FILM_KNOBS = ("palette", "caption_style", "highlight_style", "texture")


def film_controls(spec: Mapping[str, Any],
                  controls: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """台子上的整片旋钮 → 逐栏一条（键、当前值、候选、给值的名字）。

    ``controls`` 是命中表自带的控件声明；缺它时只回当前值、不给候选——前端据此
    还能显示，但不会凭空摆出一个下拉。当前值一律取**这一版分镜**的：拿控件声明的
    缺省值来顶，会把「这版没选」显示成一个它其实没有的选择。
    """
    controls = controls if isinstance(controls, Mapping) else {}
    out: list[dict[str, Any]] = []
    for key in CONSOLE_FILM_KNOBS:
        c = controls.get(key) if isinstance(controls.get(key), Mapping) else {}
        raw = spec.get(key)
        value = str(raw) if raw not in (None, "") else str(c.get("default") or "")
        row: dict[str, Any] = {"key": key, "type": str(c.get("type") or "string"),
                               "value": value or ("none" if key == "texture" else "")}
        opts = c.get("enum")
        if isinstance(opts, (list, tuple)) and opts:
            row["options"] = [str(o) for o in opts]
        labels = c.get("value_labels")
        if isinstance(labels, Mapping) and labels:
            row["labels"] = {str(k): str(v) for k, v in labels.items()}
        out.append(row)
    return out


def apply_film_overrides(spec: Mapping[str, Any], film: Mapping[str, Any] | None,
                         controls: Mapping[str, Any] | None = None
                         ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    """在**副本**上改整片那几栏 → (改过的 spec, 生效的, 被拒的)。

    校验只做一件事：值在不在控件声明的候选里。候选缺这一栏时**放行**（照
    「堵死比白烧更坏」：真正把关的是落补丁那一趟的 `validate_code_spec`，
    台上多拦一次只会让一处可读的原因变成两处不一致的文案）。但不在候选里的值
    在这里就拒：台上放过一个不存在的色板，等于让人对着没变的画面调十分钟。
    """
    spec2 = copy.deepcopy(dict(spec))
    applied: dict[str, Any] = {}
    rejected: list[dict[str, str]] = []
    controls = controls if isinstance(controls, Mapping) else {}
    for key, value in (film or {}).items():
        key = str(key)
        if key not in CONSOLE_FILM_KNOBS:
            rejected.append({"name": key, "why": "台子上没有这一栏"})
            continue
        c = controls.get(key) if isinstance(controls.get(key), Mapping) else {}
        opts = [str(o) for o in (c.get("enum") or []) if str(o).strip()]
        text = str(value)
        if opts and text not in opts:
            rejected.append({"name": key,
                             "why": f"只能在这几档里选：{'、'.join(opts)}"})
            continue
        spec2[key] = text
        applied[key] = text
    return spec2, applied, rejected


def _look_chain(texture: str, art: Any, *, luma: float) -> str:
    """整片观感（质感 + 统一调色 + 提亮）拼成一趟滤镜链，与出片那趟逐条同一出处。

    为什么预览也要跑这一趟：质感与统一调色跑在**切片之后**，静帧只渲了代码那一层。
    不在这里补上，「台上选 film、屏幕上一点颗粒都没有」——那不是预览，是骗人。
    亮度按**没加质感的那张**求，再与质感合成同一趟（与 `render.apply_texture`
    同一顺序：先质感、再调色、最后按需要提亮），否则预览会比成片亮一档。
    """
    lift = _rrender().luma_lift_chain(luma)
    return _rrender()._compose_chains(
        _rrender().TEXTURE_FILTERS.get(str(texture or "").strip().lower(), ""),
        _rrender().art_grade(art), lift[0] if lift else "")


def _apply_look(jpeg: bytes, chain: str) -> bytes:
    """一张静帧过一遍观感滤镜 → 新的 JPEG 字节（链为空时原样返回，一个字节不动）。

    滤镜作用在**缩放后的预览图**上，所以颗粒的粒径与成片不完全一样（颗粒是按像素
    算的）；颜色、暗角、对比、提亮这几样与成片一致。
    """
    if not chain or not jpeg:
        return jpeg
    with tempfile.TemporaryDirectory(prefix="prev-look-") as tmp:
        src = Path(tmp) / "in.jpg"
        dst = Path(tmp) / "out.jpg"
        src.write_bytes(jpeg)
        _mediaops().ffmpeg("-i", str(src), "-filter_complex", chain,
                        "-map", "[v]", "-frames:v", "1",
                        "-q:v", "3", str(dst), timeout=120)
        return dst.read_bytes() if dst.exists() else jpeg


def _planned_start_sec(spec: Mapping[str, Any], index: int) -> float:
    """这一镜在整片时间轴上的起点（秒），与预览静帧同一把尺。

    「一直在走的人」这类 film 作用域的轨道要的是绝对时间；每镜从 0 算会在切点处
    换脚。估出来的值与计划卡上那几张预览一致——不一样的话，台上调好的相位到了
    成片又变一步。
    """
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    overlaps = _crender()._seam_overlaps(spec, shots)
    acc = 0.0
    fps = float(spec.get("fps") or 30)
    for i, shot in enumerate(shots):
        if i == index:
            return round(acc, 3)
        acc += _crender()._preview_frames(shot, spec) / fps
        if i < len(overlaps):
            acc -= overlaps[i]
    return round(acc, 3)


async def render_preview(spec: Mapping[str, Any], *, shot_id: str,
                         overrides: Mapping[str, Any] | None = None,
                         film: Mapping[str, Any] | None = None,
                         controls: Mapping[str, Any] | None = None,
                         at: Sequence[float] = (0.2, 0.5, 0.8),
                         scale: float = 0.6,
                         client: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """改几个值、渲这一镜的三帧 → 给界面的一份回执。

    只读不写：这一趟不落盘、不动分镜、不烧视频。要真的改，走局部改那条路
    （``/motion/patch``）——那一条会按镜缓存只重烧受影响的镜，而这里花的是它
    百分之一的时间。

    ``film`` 是整片那几栏（见 `CONSOLE_FILM_KNOBS`）：它们改在**这一份副本**上，
    所以「配色/字幕样式换一档长什么样」也能在这三帧里看到，而不必先烧一整片。
    """
    index, shot = find_shot(spec, shot_id)
    shot, applied, rejected = apply_overrides(shot, overrides or {})
    spec_view, film_applied, film_rejected = apply_film_overrides(spec, film, controls)
    spec = spec_view
    rejected = list(rejected) + list(film_rejected)
    frames = _crender()._preview_frames(shot, spec)
    props = _crender().shot_input_props(
        shot, spec, frames=frames, frame_words=[],
        film_start_sec=_planned_start_sec(spec, index))
    picks = sorted({max(0, min(frames - 1, int(round(frames * float(f)))))
                    for f in at if 0.0 <= float(f) <= 1.0}) or [max(0, frames // 2)]
    code = str(shot.get("code") or "")
    if not code.strip():
        raise PreviewError(f"{shot_id} 这一镜没有代码，渲不出预览")
    fn = (client or _rclient().endpoints()).get("stills_layout")
    try:
        if fn is not None:
            got = await asyncio.to_thread(fn, code, props, picks, scale=scale)
            stills = list((got or {}).get("stills") or [])
            raw_layout = (got or {}).get("layout")
        else:
            stills = await asyncio.to_thread(
                (client or _rclient().endpoints())["stills"], code, props, picks,
                scale=scale)
            raw_layout = None
    except Exception as exc:  # noqa: BLE001 - 外呼失败压成一句话
        raise PreviewError(f"{shot_id} 的预览没渲出来（{type(exc).__name__}: "
                           f"{str(exc)[:200]}）") from exc
    layout = _crender().summarize_layout(shot_id, raw_layout) if raw_layout else None
    rows = [{"frame": int((s or {}).get("frame") or 0),
             "jpeg_b64": str((s or {}).get("jpeg_b64") or "")}
            for s in stills if (s or {}).get("jpeg_b64")]
    # 整片观感（质感/调色/提亮）跑在切片之后，静帧只渲了代码那一层——不在这里补，
    # 「台上选 film、屏幕上没颗粒」。链为空时原样返回，一个字节都不动。
    texture = _cspec().shot_texture(shot, spec)
    art = spec.get("art")
    look = await asyncio.to_thread(
        lambda: _apply_look_to_rows(rows, texture=texture, art=art))
    return {
        "shot": shot_id,
        "applied": applied,
        "film": film_applied,
        "rejected": rejected,
        "frames": picks,
        "texture": texture,
        "stills": look,
        "layout": layout,
    }


def _apply_look_to_rows(rows: list[dict[str, Any]], *, texture: str,
                        art: Any) -> list[dict[str, Any]]:
    """逐张静帧补上整片观感；亮度按**没加质感的那张**求，与出片同一顺序。

    量亮度失败（滤镜不可用/解码失败）时 `mean_luma` 回 0，`luma_lift_chain`
    会跳过提亮——那是增益缺失，不是错误；画面照出，只是没补那一段 gamma。
    每张回一行 ``look``：这一张到底过了哪几档，回执要能回答「没变是没选对
    还是滤镜没跑」。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = base64.b64decode(row.get("jpeg_b64") or "")
        if not raw:
            continue
        with tempfile.TemporaryDirectory(prefix="prev-luma-") as tmp:
            f = Path(tmp) / "in.jpg"
            f.write_bytes(raw)
            luma = _rrender().mean_luma(f)
        chain = _look_chain(texture, art, luma=luma)
        bits: list[str] = []
        if chain and texture and texture != "none":
            bits.append(f"质感 {texture}")
        if chain and _rrender().art_grade(art):
            bits.append("整片调色")
        if chain and _rrender().luma_lift_chain(luma):
            bits.append("补亮")
        out.append({
            "frame": row.get("frame"),
            "jpeg_b64": (base64.b64encode(_apply_look(raw, chain)).decode("ascii")
                         if chain else row["jpeg_b64"]),
            "look": "、".join(bits) if chain else "无",
        })
    return out
