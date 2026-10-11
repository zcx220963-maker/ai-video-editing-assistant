# -*- coding: utf-8 -*-
"""图形科普片的分镜契约 —— 模型逐镜交 React/TSX，本模块校验并算缓存键。

这里曾经是两条链共用一批整片开关（画幅/帧率/音色/语速/质感/BGM），旧的那份叫
`spec.py`。P4 起旧链下线，那批常量搬进本文件，**图形科普片只剩这一条链**：
同一件事在两处各列一遍会出现「界面上有这档、渲出来没有」，那正是旧链被删掉的原因之一。

一镜只剩五个字段，因为排版权已经交给了母版 `<FilmLayout>`：

    code              完整 .tsx，顶层必须 import 并包裹 <FilmLayout>（闸① 由容器侧 lint 判）
    text              这一镜要说的一句话 —— 只进字幕带，不进镜头
    highlight         逐词点亮的词，必须是 text 的子串，≤4 个
    fields            这一镜交出去的旋钮（可改的值），≤12 个；代码从 `fields.x` 读，
                      界面按这份清单出表单 —— 改一个值不必动代码，也不必重过闸①
    source            出处，对得上本会话检索账（出处闸在 motion_plan，不在这里）
    min_duration_sec  只当下限参考；真正的时长由 TTS 实测回填（闸②）

旧链的 `card` / `visual` / `theme` / `overlay` 四栏没有搬过来，因为**能力换了位置**：
版式与样式来自母版 token，画面来自代码。所以旧链那些字段级死键闸、空壳闸在这里
都没有对应物——空画面改由闸④ 在像素上判，样式漂移改由闸⑤ 在回读的 token 上判。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 整片开关（自旧 `spec.py` 搬入：P4 起这里就是唯一出处）
# ---------------------------------------------------------------------------

#: 画面比例 → 像素尺寸。竖屏与方图共用竖构图，方图只是四周多留纸；横屏是另一套设计空间。
ASPECTS: dict[str, tuple[int, int]] = {
    "portrait": (1080, 1920),
    "landscape": (1920, 1080),
    "square": (1080, 1080),
}

#: 帧率白名单：不给连续值——帧数直接决定母带编码量与逐镜账里的每帧毫秒
FPS_CHOICES = (10, 15, 24, 30)

MAX_SHOTS = 120
MAX_TEXT_CHARS = 60          # 竖屏字幕带两行的容量
MAX_TEXT_CHARS_WIDE = 110    # 横屏那条居中横排字幕：一行更宽，两行装得下更多
MAX_HIGHLIGHTS_PER_SHOT = 4

#: 旁白音色（edge-tts 中文音色，实测 14 个；值随服务商变，故也放行合规格式）
VOICES = (
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiNeural", "zh-CN-YunxiaNeural",
    "zh-CN-YunyangNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-HsiaoYuNeural", "zh-TW-YunJheNeural",
)

#: 质感档名。**每一镜都能带自己的一档**，顶层那档是「整片默认」：滤镜跑在拼接之前
#: 的那一趟逐镜片段上，所以「第 3 镜要老电视、其余整片柔光」是合法形状。
#: 与 BGM 不同层（BGM 在混音那一趟），但它照样**不进** ``FILM_KNOBS``——理由见那里的
#: 注释：切片缓存存的是**加质感之前**的像素，换档只重跑滤镜，不重烧任何镜头。
#: 滤镜串单源在 ``render.TEXTURE_FILTERS``——这里只列名与人话说明，
#: 两边各写一遍就会出现「界面上有这档、渲出来是原片」。
TEXTURES = ("none", "film", "tv", "glow", "bleach")

#: 母版 `<FilmLayout>` 真认的字幕形态。旧链那四档（torn/bottom…）随模板一起消失：
#: 新链的字幕带只画在母版里，母版没实现的形态就不该出现在可选项里。
SUBTITLE_MODES = ("full", "torn_highlight", "none")

#: 一段镜头代码的体积上限。lint 与 bundle 都按字节走，超了不是"风格问题"而是
#: 一次请求把容器内存吃掉一截——这条链一镜一次浏览器，宁可在这里拦。
MAX_CODE_BYTES = 40_000

# ---------------------------------------------------------------------------
# 字段参数通道（spec §12）：把"可改的东西"从代码里提出来当数据
# ---------------------------------------------------------------------------

#: 只有这五种，因为前端只有这五种控件。第六种类型的字段会落成一个没人认识的输入框
#: ——控件的单一来源是这份 type（旧链 #127「补丁口把字段类型也发出去」那条纪律的延续）。
FIELD_TYPES = ("number", "color", "text", "bool", "select")

#: 每镜最多开几个旋钮。上限不为性能，为**表单可读性**：放开了让模型声明，它会把每个
#: 字面量都开成字段，一镜四十行表单没人改得动，而那些值本来就在代码里看得见。
MAX_FIELDS_PER_SHOT = 12

#: 字段名要能当 JS 标识符用（代码里写 `fields.<name>`），也要能安全放进 JSON pointer。
FIELD_NAME_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: 母版经 `inputProps` 注入的键。**字段撞这些名字的结果是在 props 里覆盖掉字幕带要的
#: 那个键**（`{"fields": {"text": …}}` 这一路），画面上少一行字而 lint 只看代码看不见。
RESERVED_FIELD_NAMES = frozenset({
    "text", "highlight", "words", "fps", "durationInFrames", "width", "height",
    "aspect", "subtitleMode", "fields", "children",
})

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
MAX_FIELD_TEXT_CHARS = 120
MAX_OPTIONS = 12


def _expand_hex(color: str) -> str:
    """`#abc` → `#aabbcc`，统一小写。归一是必须的：`#F6F1E4` 与 `#f6f1e4` 是同一个
    颜色但不同字节，缓存键与闸⑥ 的比对都不该为这种写法差异重烧一镜。"""
    body = color[1:]
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    return "#" + body.lower()


def _is_num(v: Any) -> bool:
    # bool 是 int 的子类，但不许当数字旋钮用：`True` 进 min/max 比对会静默通过
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _norm_field(item: Any, sid: str, pos: int, errors: list[str]) -> dict[str, Any] | None:
    """一个字段声明 → 归一化后的控件描述。错误进 errors，返回 None 表示这一条丢掉。"""
    tag = f"{sid}：fields[{pos}]"
    if not isinstance(item, dict):
        _err(errors, f"{tag} 不是对象（每项写成 {{name, type, label, value, …}}）")
        return None

    name = str(item.get("name") or "").strip()
    if not FIELD_NAME_RE.match(name):
        _err(errors, f"{tag} 的 name={name!r} 不是合法 JS 标识符——代码里写不出 "
                     f"`fields.{name or '?'}`，闸① 也查不了它有没有被读")
        return None
    if name in RESERVED_FIELD_NAMES:
        _err(errors, f"{tag} 的 name={name!r} 撞了母版注入的保留键——"
                     f"结果是在 inputProps 里覆盖掉母版自己的 `{name}`，画面上少一样东西"
                     "而 lint 看不见。换个名字（如 radius、accentHex）")
        return None

    label = str(item.get("label") or "").strip()
    if not label:
        _err(errors, f"{tag}（{name}）没有 label——表单的标题只能用 name，"
                     "那是机器名，界面上不该露出来")
        return None
    label = label[:24]

    ftype = str(item.get("type") or "").strip()
    if ftype not in FIELD_TYPES:
        _err(errors, f"{tag}（{name}）的 type={ftype!r} 不在白名单 "
                     f"{', '.join(FIELD_TYPES)}——第六种类型会落成一个没人认识的输入框")
        return None

    value = item.get("value")
    out: dict[str, Any] = {"name": name, "type": ftype, "label": label, "value": value}

    if ftype == "number":
        if not _is_num(value):
            _err(errors, f"{tag}（{name}）的 value={value!r} 不是数字")
            return None
        bounds: dict[str, float] = {}
        for k in ("min", "max", "step"):
            raw = item.get(k)
            if raw is None:
                continue
            if not _is_num(raw):
                _err(errors, f"{tag}（{name}）的 {k}={raw!r} 不是数字")
                return None
            if k == "step" and float(raw) <= 0:
                _err(errors, f"{tag}（{name}）的 step={raw!r} 要大于 0")
                return None
            bounds[k] = float(raw)
        if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
            _err(errors, f"{tag}（{name}）的 min={bounds['min']} > max={bounds['max']}，"
                         "这样的区间一个值都收不下")
            return None
        out.update(bounds)
        # 声明里的 value 越界要当场打回，不钳：钳了就是"账上写 120、画上是 80"，
        # 而这条通道存在的理由恰恰是消灭这种不一致（闸⑥ 只是它的动态那一半）。
        if "min" in bounds and float(value) < bounds["min"]:
            _err(errors, f"{tag}（{name}）的 value={value} 低于 min={bounds['min']}"
                         "——不钳值，请改声明")
            return None
        if "max" in bounds and float(value) > bounds["max"]:
            _err(errors, f"{tag}（{name}）的 value={value} 高于 max={bounds['max']}"
                         "——不钳值，请改声明")
            return None
        unit = str(item.get("unit") or "").strip()[:12]
        if unit:
            out["unit"] = unit
        out["value"] = normalize_field_value("number", value)
    elif ftype == "color":
        if not isinstance(value, str) or not _HEX_RE.match(value.strip()):
            _err(errors, f"{tag}（{name}）的 value={value!r} 要写成 #rgb 或 #rrggbb——"
                         "取色控件与闸⑥ 的比对都只认十六进制，颜色名在这里没有对应字节")
            return None
        out["value"] = normalize_field_value("color", value)
    elif ftype == "text":
        if not isinstance(value, str):
            _err(errors, f"{tag}（{name}）的 value={value!r} 不是字符串")
            return None
        if len(value) > MAX_FIELD_TEXT_CHARS:
            _err(errors, f"{tag}（{name}）的文案 {len(value)} 字 > "
                         f"{MAX_FIELD_TEXT_CHARS}，这个控件塞不下，请拆成镜头代码里的两段")
            return None
        out["value"] = value
    elif ftype == "bool":
        if not isinstance(value, bool):
            _err(errors, f"{tag}（{name}）的 value={value!r} 要写成 true/false")
            return None
    else:  # select
        opts = [str(o) for o in (item.get("options") or []) if str(o).strip()]
        if not opts:
            _err(errors, f"{tag}（{name}）是 select 但没有 options——下拉框会是空的")
            return None
        if len(opts) > MAX_OPTIONS:
            opts = opts[:MAX_OPTIONS]
        if value not in opts:
            _err(errors, f"{tag}（{name}）的 value={value!r} 不在 options 里，"
                         f"可选：{', '.join(opts)}")
            return None
        out["options"] = opts
    return out


def _norm_fields(raw: Any, sid: str, errors: list[str]) -> list[dict[str, Any]]:
    """一镜的字段清单 → 归一化。顺序保留（界面按这个顺序出表单）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        _err(errors, f"{sid}：fields 必须是数组（每项一个旋钮声明），"
                     "写成对象的话顺序就没了，表单顺序不能靠 JSON 的键序")
        return []
    if len(raw) > MAX_FIELDS_PER_SHOT:
        _err(errors, f"{sid}：字段 {len(raw)} 个 > {MAX_FIELDS_PER_SHOT}——"
                     "一屏放不下的表单没人改得动，只留真要调的那几个")
        raw = raw[:MAX_FIELDS_PER_SHOT]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pos, item in enumerate(raw):
        field = _norm_field(item, sid, pos, errors)
        if field is None:
            continue
        if field["name"] in seen:
            _err(errors, f"{sid}：字段名 {field['name']!r} 出现两次——同名后一个会"
                         "覆盖前一个，表单上只剩一行")
            continue
        seen.add(field["name"])
        out.append(field)
    return out


def field_values(shot: Mapping[str, Any]) -> dict[str, Any]:
    """字段的 **name → value 扁表**。三处共用：缓存键、注入母版的 props、闸⑥ 的比对。
    扁表而不是整份声明进缓存键，是因为 `label`/`min`/`step` 换一个写法不改像素。"""
    return {str(f["name"]): f["value"] for f in (shot.get("fields") or [])}


def field_types(shot: Mapping[str, Any]) -> dict[str, str]:
    """字段的 **name → type 扁表**。闸⑥ 的比对口径按类型分（number 容差 0、color 归一
    写法差异、text/bool/select 严格相等），而类型只在声明里有一份——与 `field_values`
    同源，别让比对那侧再去猜「这个值看起来像不像颜色」。"""
    return {str(f["name"]): str(f["type"]) for f in (shot.get("fields") or [])}


def shot_texture(shot: Mapping[str, Any], film: Mapping[str, Any]) -> str:
    """这一镜**真正要跑的**质感档：自己声明的算，没声明的跟随整片默认。

    渲染层与记账层都从这里取，别在两处各写一遍「镜头有就用、没有就问整片」——那两条
    else 链一旦漂了，账面写 film 而画面上是原片，正是这条通道存在的理由要消灭的东西。
    未识别的值退回整片默认（``_norm_shot`` 早就把非法档名连镜号一起退回了，走到这里
    只剩没归一的旧产物，那时按默认比按"猜一个档"诚实）。
    """
    own = str((shot or {}).get("texture") or "").strip().lower()
    if own in TEXTURES:
        return own
    default = str((film or {}).get("texture") or "none").strip().lower()
    return default if default in TEXTURES else "none"


def normalize_field_value(ftype: str, value: Any) -> Any:
    """把一个字段值归一成**唯一字节形态**。声明期与补丁口必须走这同一条，否则缓存键
    会为一个渲出同样像素的写法换值而换键、白烧一镜：颜色 `#F6F1E4` 与 `#f6f1e4`、
    数字 `41` 与 `41.0`（这两对像素完全相同）。"""
    if ftype == "color" and isinstance(value, str):
        return _expand_hex(value.strip())
    if ftype == "number" and _is_num(value):
        return float(value)
    return value


def check_field_value(field: Mapping[str, Any], value: Any) -> str | None:
    """补丁口改字段值时的同一份校验（与声明期同源，两处规则漂过一次就是
    「新建时被拒的值，改回来却能写进去」）。返回错误文本，None 表示通过。"""
    ftype = str(field.get("type") or "")
    name = str(field.get("name") or "")
    if ftype == "number":
        if not _is_num(value):
            return f"字段 {name} 要数字，收到 {value!r}"
        if "min" in field and float(value) < float(field["min"]):
            return f"字段 {name} 的 {value} 低于 min={field['min']}——不钳值"
        if "max" in field and float(value) > float(field["max"]):
            return f"字段 {name} 的 {value} 高于 max={field['max']}——不钳值"
        return None
    if ftype == "color":
        if not isinstance(value, str) or not _HEX_RE.match(value.strip()):
            return f"字段 {name} 要写成 #rgb 或 #rrggbb，收到 {value!r}"
        return None
    if ftype == "text":
        if not isinstance(value, str):
            return f"字段 {name} 要字符串，收到 {value!r}"
        if len(value) > MAX_FIELD_TEXT_CHARS:
            return f"字段 {name} 的文案 {len(value)} 字 > {MAX_FIELD_TEXT_CHARS}"
        return None
    if ftype == "bool":
        return None if isinstance(value, bool) else f"字段 {name} 要 true/false，收到 {value!r}"
    if ftype == "select":
        opts = [str(o) for o in (field.get("options") or [])]
        return None if str(value) in opts else f"字段 {name} 的 {value!r} 不在 options：{opts}"
    return f"字段 {name} 的类型 {ftype!r} 不认识（白名单：{', '.join(FIELD_TYPES)}）"


#: 进缓存键的"该镜旋钮"（spec §5.2）。`source` **故意不在其中**：改出处不改像素，
#: 进了键就等于换一条引用要重烧一镜。
#: `fields` 也在其中，但**进键的是 `field_values()` 那份 name→value 扁表**而不是整份声明：
#: `label`/`min`/`step` 换一个写法不改像素，整份进键就会为一句话术重烧一镜。
#: `texture` 也**不在**其中，虽然它现在真的逐镜生效：缓存存的是**加质感之前**的切片，
#: 滤镜是取到切片之后的一趟 ffmpeg（见 :func:`code_render.render_code_shot` 末段）。
#: 进键的话「整片从 film 换成 tv」会让 20 镜的 Remotion 帧全部重烧，而那件事的代价
#: 本来就只是一趟每镜不到一秒的滤镜。
SHOT_KNOBS = ("text", "highlight", "min_duration_sec", "fields")

#: 进缓存键的"整片开关"。**这是对 spec §5.2 的加项**，那条清单只写了 fps/aspect，
#: 漏了这四个真换像素的：`narration`/`voice`/`rate` 改的是切片里的**声音字节**（切片
#: 自带音轨，命中即整段跳过、连 TTS 都不重发），`subtitle_mode` 改的是母版字幕带的
#: 画法。不进键的后果与旧链那条注释写过的同族——「少算一个键就是拿旧片冒充新片」。
#: 反过来 `texture` **仍然不进**：它跑在缓存之后的那一趟滤镜上，换档 0 镜重烧（§5.3）。
FILM_KNOBS = ("fps", "aspect", "narration", "voice", "rate", "subtitle_mode")

#: 选区改（P3）对代码镜只开这五条指针。`code` 的改动必须重过闸① 才允许落盘；
#: `fields/{name}/value` 不动代码字节，改的是注入母版的值，所以不重跑 lint，
#: 只按 `check_field_value` 那份与声明期同源的规则校验（越界打回，不钳值）；
#: `texture` 既不动代码也不动像素来源，改的是缓存之后的那一趟滤镜。
EDITABLE_POINTERS = ("/shots/{i}/code", "/shots/{i}/text", "/shots/{i}/source",
                     "/shots/{i}/fields/{name}/value", "/shots/{i}/texture")


def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def _norm_shot(raw: dict[str, Any], i: int, errors: list[str], *, max_chars: int) -> dict[str, Any]:
    sid = str(raw.get("id") or f"s{i + 1:02d}")

    code = str(raw.get("code") or "")
    if not code.strip():
        _err(errors, f"{sid}：code 为空——新链每一镜必须交一段真实代码，没有默认版式可退")
    n = len(code.encode("utf-8"))
    if n > MAX_CODE_BYTES:
        _err(errors, f"{sid}：代码 {n} 字节 > {MAX_CODE_BYTES}，一镜一次浏览器，这个体积不接")

    text = str(raw.get("text") or "").strip()
    if not text:
        _err(errors, f"{sid}：text 为空——每一镜必须有一句要说的话")
    if len(text) > max_chars:
        _err(errors, f"{sid}：文案 {len(text)} 字 > {max_chars}，这一档画幅的字幕条放不下，请拆镜")

    highlights = [str(h) for h in (raw.get("highlight") or []) if str(h).strip()]
    if len(highlights) > MAX_HIGHLIGHTS_PER_SHOT:
        _err(errors, f"{sid}：高亮词 {len(highlights)} 个 > {MAX_HIGHLIGHTS_PER_SHOT}")
        highlights = highlights[:MAX_HIGHLIGHTS_PER_SHOT]
    for h in highlights:
        if h not in text:
            _err(errors, f"{sid}：高亮词 {h!r} 不是文案的子串，画面上永远亮不起来")

    want = raw.get("duration_sec")
    if want is None:
        want = raw.get("min_duration_sec")
    try:
        min_sec = float(want) if want is not None else 0.0
    except (TypeError, ValueError):
        _err(errors, f"{sid}：min_duration_sec={want!r} 不是数字")
        min_sec = 0.0
    min_sec = max(0.0, min(min_sec, 60.0))

    src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    # 质感：留空 = 没表态，跟随整片默认那一档；显式写 "none" = 这一镜**不要**质感，
    # 即使整片加了。两种写法在界面上是两个选择，不能合并成一个。
    tex = str(raw.get("texture") or "").strip().lower()
    if tex and tex not in TEXTURES:
        _err(errors, f"{sid}：texture={tex!r} 没有对应的滤镜链"
                     f"（可用：{', '.join(TEXTURES)}，留空表示跟整片默认）")
        tex = ""
    return {
        "id": sid,
        "code": code,
        "text": text,
        "highlight": highlights,
        "fields": _norm_fields(raw.get("fields"), sid, errors),
        "min_duration_sec": min_sec,
        "texture": tex,
        "source": {str(k): str(v) for k, v in src.items()},
    }


def validate_code_spec(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """→ (规范化后的 spec, 错误清单)。错误清单非空即**不可渲染**。

    这一层管两类事：① 逐镜「一定会把画面写坏」的几件事（代码在不在、文案超不超
    字幕容量、高亮词是不是子串、枚举在不在白名单内）；② **整片层**的闸⑦ 硬判据
    （:func:`check_style_quota`：风格配额与纯文字镜占比）。代码**内容**对不对归闸①
    （容器里的 lint）与闸④（画面上有没有东西），史实对不对归出处闸与证据账。
    闸⑦ 只出**提醒**的那一份（文案具体项）不从本函数返回——它不拦，由 ``style_facts``
    在计划卡与出片回执那两处点名。
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["spec 必须是对象"]

    aspect = str(raw.get("aspect") or "portrait")
    if aspect not in ASPECTS:
        _err(errors, f"aspect={aspect!r} 不支持（可用：{', '.join(ASPECTS)}）")
        aspect = "portrait"
    width, height = ASPECTS[aspect]
    max_chars = MAX_TEXT_CHARS_WIDE if width > height else MAX_TEXT_CHARS

    try:
        fps = int(raw.get("fps") or 30)
    except (TypeError, ValueError):
        fps = 30
        _err(errors, "fps 不是整数")
    if fps not in FPS_CHOICES:
        _err(errors, f"fps={fps} 不在白名单 {FPS_CHOICES}")
        fps = 30

    narration = bool(raw.get("narration", True))
    voice = str(raw.get("voice") or "zh-CN-YunjianNeural")
    rate = str(raw.get("rate") or "+0%")
    if narration:
        if voice not in VOICES and not _VOICE_RE.match(voice):
            _err(errors, f"voice={voice!r} 不是合法音色名")
        if not _RATE_RE.match(rate):
            _err(errors, f"rate={rate!r} 要写成 +10% / -20% 这种形式")
        else:
            pct = int(rate[:-1])
            if not -50 <= pct <= 50:
                _err(errors, f"rate={rate} 超出 ±50%")

    sub_mode = str(raw.get("subtitle_mode") or "torn_highlight")
    if sub_mode not in SUBTITLE_MODES:
        _err(errors, f"subtitle_mode={sub_mode!r} 不支持（母版只认：{', '.join(SUBTITLE_MODES)}）")
        sub_mode = "torn_highlight"

    texture = str(raw.get("texture") or "none").strip().lower()
    if texture not in TEXTURES:
        _err(errors, f"texture={texture!r} 没有对应的滤镜链（可用：{', '.join(TEXTURES)}）")
        texture = "none"

    raw_shots = raw.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        _err(errors, "shots 为空——没有镜头就没有片子")
        raw_shots = []
    if len(raw_shots) > MAX_SHOTS:
        _err(errors, f"镜头数 {len(raw_shots)} > {MAX_SHOTS}，超出单条片子上限")
        raw_shots = raw_shots[:MAX_SHOTS]

    shots = [_norm_shot(s, i, errors, max_chars=max_chars)
             for i, s in enumerate(raw_shots) if isinstance(s, dict)]
    if len(shots) != len(raw_shots):
        _err(errors, "有镜头不是对象，已丢弃")

    # 闸⑦ 的硬判据（整片风格配额与纯文字镜占比）也挂在这里：三个调用点——plan_motion
    # 入库前、render_motion_code 烧像素前、patch_motion_code 落盘前——调的都是这一个
    # 函数，规则因此不可能只在一路上生效。提醒那一份从这里单独取（见 style_facts）。
    quota_errors, _warns = check_style_quota(shots)
    errors.extend(quota_errors)

    bgm = raw.get("bgm") if isinstance(raw.get("bgm"), dict) else {}
    try:
        bgm_volume = float(bgm.get("volume", 0.18))
    except (TypeError, ValueError):
        bgm_volume = 0.18
    bgm_volume = max(0.0, min(bgm_volume, 0.6))

    spec = {
        "chain": "remotion",       # 逐镜账上要能看出这一镜是哪条链烧的
        "aspect": aspect,
        "width": width,
        "height": height,
        "fps": fps,
        "narration": narration,
        "voice": voice,
        "rate": rate,
        "subtitle_mode": sub_mode,
        "texture": texture,
        "shots": shots,
        "bgm": {"ref": str(bgm.get("ref") or ""), "volume": bgm_volume,
                "duck": bool(bgm.get("duck", True))},
        "title": str(raw.get("title") or "")[:80],
    }
    return spec, errors


def shot_cache_key(shot: dict[str, Any], *, film: Mapping[str, Any],
                   master_hash: str, lockfile_hash: str) -> str:
    """一镜的缓存键（spec §5.2，加上面 FILM_KNOBS 那条加项）。

    四段身份缺一不可：**代码字节**（内容）、**该镜旋钮**（文案、点亮词，以及字段的
    name→value 扁表）、**整片开关**（fps/画幅/旁白/音色/语速/字幕形态）、
    **母版哈希 + 依赖锁哈希**。
    后两项不能省：改一次母版 token 或换一版依赖＝全片换像素，不进键就会命中旧母版
    渲出来的切片，而改动账和逐镜比较全都看不出来——与 `44` / `44.0` 那次键漂移同族。
    所以按字节算的键要按字节断言（见 tests/test_code_spec.py）。
    """
    knobs = {k: shot.get(k) for k in SHOT_KNOBS}
    # 见 SHOT_KNOBS 那条注释：进键的是值扁表，不是整份声明。
    knobs["fields"] = field_values(shot)
    payload = {
        "code": str(shot.get("code") or ""),
        "knobs": knobs,
        "film": {k: film.get(k) for k in FILM_KNOBS},
        "master": master_hash,
        "deps": lockfile_hash,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 闸⑦ 整片层：风格配额与文案具体项（在烧像素之前判，一次都不花）
# ---------------------------------------------------------------------------

#: 四条画法通道 → 认的 import 包名。**与技能正文「画面丰富度合同与风格配额」那张表
#: 逐行同源**（``tests/test_code_spec.py`` 第 ⑦ 节拿正文比对这些名字）。
#: 为什么判据是 import 而不是模型自报的一栏：自报会漂（写了 `style: rough` 而代码里
#: 一个 roughjs 都没有），而这条闸要拦的恰恰就是「说用了但其实没有」。
STYLE_CHANNELS: dict[str, tuple[str, ...]] = {
    "rough": ("roughjs",),
    "three": ("three", "@react-three/fiber", "@remotion/three"),
    "katex": ("katex",),
    "vector": ("@remotion/paths", "@remotion/shapes"),
}

#: 归类顺序：既上 rough 又写裸 SVG 的那一镜算 rough（专门通道优先，配额才不会被
#: 「一条 three 镜 + 十一条 SVG 镜」蒙成两种通道各半）。
_CHANNEL_ORDER = ("rough", "three", "katex", "vector")

#: 没有专门通道、但代码里真在画几何图形 —— 技能正文那句「（或裸 SVG）」的机器形态。
#: 判据是**写了画形状的东西**，不是 import：`<svg>`/`<path>`/`rough` 之外的
#: `<div>` 加 `background` 只是色块，色块不算画面主体（「卡片+文字」正是这么写的）。
_VECTOR_MARKUP_RE = re.compile(
    r"<\s*(svg|path|rect|circle|ellipse|line|polyline|polygon|g)\b"
    r"|\b(toPaths|toCurves|Path2D|linePath|polylinePath|shapeArc|shapeRect|"
    r"shapeCircle|shapePolygon|E\()")

#: 四条通道之外的那一类：只有文字与色块。技能正文里的「纯文字/纯色块的那一类镜头」。
TEXT_CHANNEL = "text"

#: 配额表：镜数 → 至少几种**非文字**通道。与正文「≥6 镜至少两种、≥10 镜至少三种」
#: 同一份数；6 镜以下不设硬闸（三镜片要求两种通道是逼模型塞一个用不上的立方体，
#: 正文自己就写了「通道不是装饰」）。
CHANNEL_QUOTA: tuple[tuple[int, int], ...] = ((6, 2), (10, 3))

#: 两条硬闸共同的起步镜数（正文那句「首尾镜不计入分子也不计入分母」也要有分母可算）。
MIN_SHOTS_FOR_QUOTA = 6

#: 纯文字/纯色块镜的上限：占**中间镜**（去掉首尾）的三分之一。
MAX_TEXT_ONLY_SHARE = 1.0 / 3.0

#: 文案具体项的达标线（低于它只出提醒，理由见 :func:`check_style_quota` 末段与
#: :func:`has_concrete_item` 那份判据的覆盖面）。
CONCRETE_SHARE_TARGET = 0.40

_IMPORT_RE = re.compile(r"""(?:from|import)\s*['"]([^'"]+)['"]""")

#: 词法看得见的「一个具体项」：数字（数量/年份/比例都从它露出来）、引号或书名号里的
#: 专名、拉丁缩写与外来词。地名、人名、器物名**机器认不出来**（那要 NER），
#: 所以这一路判据是偏窄的，只用来出提醒。
_CONCRETE_RES = (
    re.compile(r"[0-9０-９]"),
    re.compile(r"[《「『“][^》」』”]{1,20}[》」』”]"),
    re.compile(r"[A-Za-z]{2,}"),
)


def code_imports(code: str) -> list[str]:
    """一段镜头代码里出现过的包名（按首次出现去重）。"""
    out: list[str] = []
    for name in _IMPORT_RE.findall(code or ""):
        if name not in out:
            out.append(name)
    return out


def shot_style_channel(code: str) -> str:
    """一镜归一条画法通道：`rough` / `three` / `katex` / `vector` / `text`。"""
    imps = set(code_imports(code))
    for name in _CHANNEL_ORDER:
        if imps & set(STYLE_CHANNELS[name]):
            return name
    return "vector" if _VECTOR_MARKUP_RE.search(code or "") else TEXT_CHANNEL


def has_concrete_item(text: str) -> bool:
    """这一镜的文案里有没有一个**词法可见**的具体项。"""
    return any(r.search(text or "") for r in _CONCRETE_RES)


def style_facts(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """整片的风格与文案账（确认卡、出片回执、闸⑦ 三处读同一份，不各算一遍）。"""
    channels = {str(s.get("id") or f"s{i + 1:02d}"): shot_style_channel(str(s.get("code") or ""))
                for i, s in enumerate(shots)}
    variety = {c for c in channels.values() if c != TEXT_CHANNEL}
    middle = list(channels.items())[1:-1] if len(shots) >= 3 else []
    return {
        "channels": channels,
        "variety": sorted(variety),
        "text_only": [sid for sid, c in channels.items() if c == TEXT_CHANNEL],
        "text_only_middle": [sid for sid, c in middle if c == TEXT_CHANNEL],
        "hollow": [str(s.get("id") or f"i{i + 1}") for i, s in enumerate(shots)
                   if not has_concrete_item(str(s.get("text") or ""))],
    }


def check_style_quota(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸⑦：整片层的风格配额与文案具体项。→ (打回清单, 提醒清单)。

    三条硬判据全部读**代码字节**，都在烧像素之前判（这一层红一次 = 一次分镜重做，
    不是几十分钟白烧）：

    1. 通道种数不够（≥6 镜要 2 种、≥10 镜要 3 种，见 ``CHANNEL_QUOTA``）。这条同时
       拦住正文点名的那种写法——全片只有裸 SVG/图示一种通道，就是「卡片+文字」换了个
       马甲：variety=1 < 2。
    2. 全片一条图形通道都没有（≥6 镜时）：这是判据 1 的特例，单独留一条是因为退回理由
       要能指名「一镜图形都没画」，而不是笼统说「种类不够」。
    3. 纯文字/纯色块镜超过中间镜的三分之一（首尾不计入分子也不计入分母，与正文同一口径）。

    **文案具体项那一条只出提醒，不打回**，这是判过的而不是偷懒：词法只看得见数字、
    引号术语、拉丁缩写，看不见「毕昇」「沈括」「《梦溪笔谈》」之外的地名人名器物名——
    一条讲活字印刷的片子完全可以一句数字都不写。把 40% 这条线做成硬闸，误伤的就是
    这种纯专名文案，而误判的代价是一整轮重规划。所以它落在两处实在的地方：技能正文
    「文案合同」（模型写之前就要求到）+ 确认卡与出片回执上逐镜点名的提醒（用户看得见，
    可以直接说「这几镜太空，重写」）。
    """
    errors: list[str] = []
    warns: list[str] = []
    n = len(shots)
    if n < MIN_SHOTS_FOR_QUOTA:
        return errors, warns
    facts = style_facts(shots)
    variety = facts["variety"]
    want = max(k for limit, k in CHANNEL_QUOTA if n >= limit)
    if not variety:
        errors.append(
            f"闸⑦ 风格配额：{n} 镜没有一镜画出图形（全是文字与色块）——"
            f"至少要 {want} 种画法通道。按题材选：器物/手用 roughjs，真三维形体用 "
            "three + @react-three/fiber，式子用 katex，流程与曲线用 @remotion/shapes "
            "或裸 SVG。技能正文「画面丰富度合同」那一节逐条写着适合画什么")
    elif len(variety) < want:
        errors.append(
            f"闸⑦ 风格配额：{n} 镜只用了 {len(variety)} 种画法通道（{'、'.join(variety)}），"
            f"这一档镜数至少要 {want} 种。当前分布："
            + "、".join(f"{sid}={ch}" for sid, ch in facts["channels"].items())
            + "——把最该有形体的那几镜换成对应的通道，别为了凑数给用不上的镜塞立方体")
    text_mid = facts["text_only_middle"]
    if len(text_mid) > (n - 2) * MAX_TEXT_ONLY_SHARE:
        errors.append(
            f"闸⑦ 纯文字镜超标：中间 {n - 2} 镜（首尾除外）里 {len(text_mid)} 镜只有文字"
            f"与色块，上限是三分之一（最多 {int((n - 2) * MAX_TEXT_ONLY_SHARE)} 镜）——"
            f"这些镜要补画面主体：{'、'.join(text_mid)}")
    hollow = facts["hollow"]
    if hollow and (n - len(hollow)) / n < CONCRETE_SHARE_TARGET:
        warns.append(
            f"文案提醒：{n} 镜里只有 {n - len(hollow)} 镜的文案带着机器认得出的具体项"
            f"（数字/引号术语/拉丁缩写），低于 {CONCRETE_SHARE_TARGET:.0%} 这条线。"
            f"这些镜没查到：{'、'.join(hollow)}。地名人名机器认不出，所以这一条只提醒不拦——"
            "但「每镜至少落一个具体项」是文案合同写的，逐镜看一眼再出片")
    return errors, warns


# ---------------------------------------------------------------------------
# 用户开关（计划卡上那一排 radio）——自旧 `spec.py` 搬入
# ---------------------------------------------------------------------------

#: 顶层入参名 = 落进 spec 的字段名。为什么要复制到**顶层**再合回来：计划卡的开关
#: 只能从节点顶层入参反查枚举源（``plan/vocab.py`` 的 ``props_for`` 只看
#: ``input_schema.properties``），嵌在 spec 里的字段对卡面不存在——模型写了卡也出不来。
#:
#: ``texture`` 在这里：它是**整片的默认质感档**（用户在计划卡上勾的那一格），
#: 每一镜要不一样的观感就在自己的 `shots[].texture` 里覆盖它——卡面只问一次，
#: 不该逼模型替每一镜决定。也正因为质感跑在切片缓存之后的那一趟滤镜上，
#: 换档不重烧任何一镜——所以它**不进** ``FILM_KNOBS``。
KNOB_FIELDS = ("aspect", "fps", "narration", "voice", "rate", "subtitle_mode",
               "texture")

_VOICE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,})?-[A-Za-z]+Neural$")
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")

#: 语速档位（前端下拉直接给这几个，比让用户填百分号友好）
RATE_CHOICES = ("-20%", "-10%", "+0%", "+10%", "+20%", "+30%")

#: 开关取值的中文标签。**每个取值都要有一句**：卡面上的 radio 文字优先用模型写的
#: ``display``，模型没写就回落到这里（见 ``plan/validate.py::_check_params``）——
#: 漏一个取值，界面上就露出 ``portrait`` / ``zh-CN-XiaoxiaoNeural`` 这种机器名。
#: 措辞与主服务的确认卡标签同源（``agent_framework/render_gate.py``），两份漂了由
#: ``tests/test_code_spec.py`` 拦下。
KNOB_VALUE_LABELS: dict[str, dict[str, str]] = {
    "aspect": {"portrait": "竖屏", "landscape": "横屏", "square": "方屏"},
    # 帧率是这条链路最大的成本项，标签要把「贵在哪」一起说给人看
    "fps": {"10": "10 帧·最省", "15": "15 帧·推荐",
            "24": "24 帧·更顺", "30": "30 帧·最贵"},
    "narration": {"true": "有人声旁白", "false": "无旁白（只看字）"},
    "voice": {
        "zh-CN-XiaoxiaoNeural": "女声·普通话",
        "zh-CN-XiaoyiNeural": "女声·普通话（偏年轻）",
        "zh-CN-YunjianNeural": "男声·普通话",
        "zh-CN-YunxiNeural": "男声·普通话（偏年轻）",
        "zh-CN-YunxiaNeural": "男声·少年音",
        "zh-CN-YunyangNeural": "男声·播报腔",
        "zh-CN-liaoning-XiaobeiNeural": "女声·东北口音",
        "zh-CN-shaanxi-XiaoniNeural": "女声·陕西方言",
        "zh-HK-HiuGaaiNeural": "女声·粤语",
        "zh-HK-HiuMaanNeural": "女声·粤语（偏软）",
        "zh-HK-WanLungNeural": "男声·粤语",
        "zh-TW-HsiaoChenNeural": "女声·台湾口音",
        "zh-TW-HsiaoYuNeural": "女声·台湾口音（偏慢）",
        "zh-TW-YunJheNeural": "男声·台湾口音",
    },
    "rate": {"-20%": "很慢", "-10%": "稍慢", "+0%": "正常",
             "+10%": "稍快", "+20%": "较快", "+30%": "很快"},
    # 母版 <FilmLayout> 只认这三档；旧链的 torn / bottom 随模板一起消失
    "subtitle_mode": {"torn_highlight": "撕纸条·逐词点亮", "full": "整句字幕",
                      "none": "不上字幕"},
    # 质感每一镜都能带一档（顶层是整片默认），标签要说清「加的是哪种观感」，不然选的人只能猜
    "texture": {"none": "不加（原片直出）", "film": "老胶片·颗粒与暗角",
                "tv": "老电视·扫描线与色偏", "glow": "柔光·高光处泛开",
                "bleach": "漂白·硬对比低饱和"},
}

#: 本链字幕档的单独视图（确认卡与母版参数都从这里取词）
SUBTITLE_LABELS = KNOB_VALUE_LABELS["subtitle_mode"]

#: 每字朗读秒数的经验区间：估算总时长用，不是承诺值。
#: 实测 zh-CN 男声 +0% 约 0.22~0.28 秒/字（含标点停顿），比 edge-tts 老兜底的
#: 0.22 宽一档，因为科普片会读标点。
SEC_PER_CHAR_LOW = 0.20
SEC_PER_CHAR_HIGH = 0.30


def knob_props() -> dict[str, Any]:
    """整片开关的 JSON Schema。枚举值取自本模块的清单，单源不再抄第二遍。

    末尾统一挂上 ``value_labels``：取值与它的人话标签同一处声明，随 DAG 契约一起
    带回主服务，卡面就不需要谁再抄一份对照表。**这两样必须留**——``enum`` 是校验器的
    判据，``value_labels`` 是计划卡 radio 的字面（计划卡的参数 schema 与工具 schema
    是同一份 input_schema，删了标签界面就露 ``portrait`` 这种机器名）。

    ``description`` 一律收成一行，因为**这段文字不是给模型读的**（真机测过：节点工具
    的 inputSchema 由 ``server.py::_signature_for`` 从 Python 签名生成，只取 ``type``，
    ``spec`` 到模型那侧就剩 ``additionalProperties: true``；``tools/list`` 载荷里
    ``plan_motion`` 三个图形节点合计 9499 字节，散文占零）。它真正的读者只有两处：
    计划卡的 ``vocab.knob_facts``（``note`` 取前 60 字，超了会截在半句话上）与
    前端 ``controls``（只读 type/enum/default/value_labels/范围，不读文字）。
    所以「怎么选、贵在哪、与旧链的差异」那套教程归技能正文
    （``motion_explainer`` 的「用户在计划卡上能改的那几件事」一节）——三个图形节点都挂在
    ``plan/vocab.py`` 的 ``_IMPLICIT_SKILLS`` 上，执行轮起步就按链预注入，
    这才是动态加载的那条通道；同一份教程在两处各写一遍必然漂，而漂的那一侧没人看得见。
    """
    props = {
        "aspect": {
            "type": "string", "enum": list(ASPECTS), "default": "portrait",
            "description": "画幅比例，决定成片像素尺寸与字幕带容量",
        },
        "fps": {
            "type": "integer", "enum": list(FPS_CHOICES), "default": 15,
            "description": "帧率，15 推荐；每帧毫秒与母带编码量随它线性涨",
        },
        "narration": {
            "type": "boolean", "default": True,
            "description": "是否有人声朗读；关了按阅读节奏驻留，voice/rate 失效",
        },
        "voice": {
            "type": "string", "enum": list(VOICES), "default": "zh-CN-YunjianNeural",
            "description": "旁白音色（edge-tts 中文音色，narration=true 才有意义）",
        },
        "rate": {
            "type": "string", "enum": list(RATE_CHOICES), "default": "+0%",
            "description": "语速档位，越快整片越短；与目标时长对账先看它",
        },
        "subtitle_mode": {
            "type": "string", "enum": list(SUBTITLE_MODES), "default": "torn_highlight",
            "description": "字幕形态，画在母版字幕带里，镜头代码碰不到它",
        },
        "texture": {
            "type": "string", "enum": list(TEXTURES), "default": "none",
            "description": "整片默认质感档；每一镜可用自己的 texture 覆盖它。换它不重烧镜头",
        },
    }
    for key, spec in props.items():
        spec["value_labels"] = dict(KNOB_VALUE_LABELS.get(key) or {})
    return props


#: 镜头代码能 import 的包。**必须与 `remotion/lint-rules.mjs` 的 `ALLOWED_IMPORTS`
#: 逐项相等**——闸① 那侧的判据是「镜像里真装了」，模型这侧看到的是「我可以写」，
#: 两份漂了两种红法：名单少了，模型写出来被闸退回；名单多了，闸放行一个 npm 上
#: 不存在／没预装的包，报错发生在 lint 通过之后的打包期，模型拿到的是一句解析失败。
#: 真漂过一次（`@remotion/sequence` 这个包根本不存在，`<Sequence>` 是 `remotion`
#: 核心的导出；`@remotion/preload` 没装）。由 `tests/test_code_spec.py` 的第 ⑤ 节
#: 拿正则把 mjs 那份抠出来逐项比，第 ⑦ 节再比技能正文里有没有列全。
#:
#: 唯一的**子路径**例外是 `katex/dist/katex.min.css`。它不是「跨包摸内部文件」，
#: 而是那条样式表本身：`katex.renderToString` 离线就能出 HTML，**不 import CSS 也照样
#: 渲得出来、闸① 也过、打包也不报错**，只是画面上是一串没有上下标没有斜体的纯文本。
#: 这种「静默出丑」正是这条名单该拦住的东西，所以它必须出现在模型看得见的那一份里。
IMPORT_WHITELIST: tuple[str, ...] = (
    "react", "react-dom", "remotion", "three", "@react-three/fiber",
    "@remotion/three", "@remotion/paths", "@remotion/shapes",
    "roughjs", "katex", "katex/dist/katex.min.css", "@dg/film", "@dg/tokens",
)

#: ``code`` 这一栏的 schema 说明。**这里只留「机器会判什么」**，写法教程整段在技能正文
#: （``motion_explainer`` 的「镜头代码合同」「把…开成旋钮」「闸④ 判空壳」三节）。
#: 分层的依据是实测而不是偏好：工具 schema 的散文**送不到模型那里**——节点工具的
#: inputSchema 由 ``server.py::_signature_for`` 从 Python 签名生成，只取 ``type``，
#: ``spec`` 到那侧只剩 ``additionalProperties: true``。所以这一段过去那 2 KB 教程
#: 谁也没教过，而技能正文逐条写过同一套规则（还多教了 ``useBody()`` 那条母版几何）：
#: 两处各写一遍必然漂，漂的那一侧又没人会去读，于是成了假文档。
#: 留下的是**判据**——白名单、母版包裹、字段对账三条、体积与判空，它们与本模块的常量
#: 和容器里的 lint 一一对应；``tests/test_code_spec.py`` 同时核对这份文本与技能正文，
#: 谁单独改了哪一条都会红。
_CODE_DOC = (
    "这一镜的**完整 .tsx**（一个文件，顶层一个默认导出的镜头组件），由 creation-remotion "
    "逐帧渲染。闸①（容器里的 ESLint）在渲染前判，违规带镜号与行号退回，不烧像素。"
    "**写法纪律（画面带坐标系 `useBody()`、母题铺满带子、rough.js 离线形状与 seed、"
    "按墙钟随机的禁令、字体只用 Noto）见技能正文「镜头代码合同」一节**，这里只列会判红的：\n"
    "· 必须 `import { FilmLayout } from \"@dg/film\"` 并把顶层 JSX 包进 `<FilmLayout>`——"
    "字幕带、安全区、边距、字阶、配色全在母版里，镜头代码只填主体区；不包就没有字幕，"
    "且闸⑤ 读不到这一镜的 token\n"
    "· import 只许这些包（镜像里真装了的东西）：" + ", ".join(IMPORT_WHITELIST) +
    "；相对路径自由\n"
    "· 时刻只能来自 `useCurrentFrame()`：禁 `Math.random`（要随机用 remotion 的 "
    "`random(seed)`）、禁 `Date.now`/`new Date()`/`performance`、禁 "
    "`setTimeout`/`setInterval`/`requestAnimationFrame` 驱动动画、禁 SMIL——"
    "逐帧 seek 下按墙钟走的东西两次渲不同像素\n"
    "· 禁 `fetch`/`XMLHttpRequest`/`WebSocket`/`localStorage`/`eval`/`new Function`/"
    "`process`，禁任何 `http(s)://` 外链资源与 `@import`（容器离线，外链字体与图片会"
    "**静默回落**成别的东西）；字体只用镜像里已有的 Noto CJK 家族\n"
    "· **不要自己写时长**：`durationInFrames` 由闸② 按真实语音时长注入，"
    "代码里读它来自作主张就等于替声音定时长\n"
    "· 字幕内容不要画进代码——`text` 与 `highlight` 由母版排，画两遍是重影\n"
    "· 可改的值开成 `fields` 旋钮：代码里读 `fields.<name>`，"
    "并在真的把它画上那个位置时套一层 "
    "`<F name=\"…\" value={fields.…}>`（`F` 由 `@dg/film` 导出）。母版按这份标注回读"
    "「值 + 盒」给闸⑥ 对账，字段级命中表拿它定位。三条 lint 在烧像素之前判："
    "声明了却没人读、读了却没声明、`<F>` 标的 value 不是这个字段自己的值\n"
    "· 画法按题材选，不是装饰：SVG 关键帧用 `interpolate`；手绘质感用 `roughjs` 的"
    "**离线形状**那一路（`gen.toPaths(...)`，**seed 必须给**——不给时库内部走墙钟随机，"
    "同一镜两次渲不同像素，而 lint 只看你写的代码、看不到依赖里的 random）；"
    "立体几何用 `@remotion/three` + `@react-three/fiber`（本容器无 GPU，软件渲染，慢，"
    "逐镜账上的每帧毫秒会如实变贵）；公式用 `katex`\n"
    "· 画面里得有能认出来的东西：闸④ 在**母版画面带内**按像素判空"
    "（三帧带内非底色占比低于 2% 退回）")


def spec_schema() -> dict[str, Any]:
    """``spec`` 入参的 JSON Schema（代码镜契约）。

    与旧链的 ``spec_schema`` 同一条理由：清单摆在 ``enum`` 上比写在散文里可靠，
    而且启动一致性检查按「schema 里出现过的字段名与枚举取值」判定技能正文里的标识符
    是不是造工具名——只有散文时假告警会教人忽略检查本身。
    """
    return {
        "type": "object",
        "properties": {
            **knob_props(),
            "title": {"type": "string", "maxLength": 80,
                      "description": "片子标题（出现在界面与素材库，不进画面）"},
            "bgm": {
                "type": "object",
                "description": "背景音乐：曲库/附件的 material_id 或 obj: 引用；"
                               "取不到字节直接失败，不会静默出成无声片",
                "properties": {
                    "ref": {"type": "string"},
                    "volume": {"type": "number", "default": 0.18,
                               "description": "配乐相对人声的音量，0~0.6"},
                    "duck": {"type": "boolean", "default": True,
                             "description": "有人声时压低配乐"},
                },
            },
            "shots": {
                "type": "array", "minItems": 2, "maxItems": MAX_SHOTS,
                "description": "镜头清单，按播放顺序。**首尾各留一镜**做开场与收尾呼吸"
                               "（min_duration_sec 给 1.5~2.5，代码画封面与落款就行）",
                "items": {
                    "type": "object",
                    "required": ["code", "text"],
                    "properties": {
                        "id": {"type": "string",
                               "description": "镜号，省略则按顺序补 s01/s02…"},
                        "code": {"type": "string", "maxLength": MAX_CODE_BYTES,
                                 "description": _CODE_DOC},
                        "text": {"type": "string", "maxLength": MAX_TEXT_CHARS_WIDE,
                                 "description": f"这一镜要说的一句话，只进母版字幕带。"
                                                f"容量按画幅：竖屏 ≤{MAX_TEXT_CHARS} 字、"
                                                f"横屏 ≤{MAX_TEXT_CHARS_WIDE} 字"
                                                "（超了拆镜，不裁句子）"},
                        "highlight": {"type": "array", "items": {"type": "string"},
                                      "maxItems": MAX_HIGHLIGHTS_PER_SHOT,
                                      "description": "逐词点亮的词，每个都必须是 text 的子串，"
                                                     "每镜 ≤4 个；差一个字画面上永远不亮"},
                        "fields": {
                            "type": "array", "maxItems": MAX_FIELDS_PER_SHOT,
                            "description": "这一镜**交出去给用户改的旋钮**（≤12 个）：把将来"
                                           "可能要调的值从代码里提出来当数据。代码里从 "
                                           "`inputProps.fields` 读，界面按这份清单出表单——"
                                           "改一个值不必动代码，也不必重过闸①。**自愿**："
                                           "没交旋钮的镜头，界面上〈字段〉页就是空的。"
                                           "name 要能当 JS 标识符用（代码里写 `fields.<name>`），"
                                           "且不许撞母版注入的保留键：" +
                                           ", ".join(sorted(RESERVED_FIELD_NAMES)) +
                                           "；撞了等于在 props 里覆盖掉母版自己要用的那个键，"
                                           "画面上少一样东西而 lint 只看代码看不见。"
                                           f"type 只有这五种，因为前端只有这五种控件："
                                           f"{', '.join(FIELD_TYPES)}",
                            "items": {
                                "type": "object",
                                "required": ["name", "type", "label", "value"],
                                "properties": {
                                    "name": {"type": "string",
                                             "description": "JS 标识符，代码里写 "
                                                            "`fields.<name>`"},
                                    "type": {"type": "string", "enum": list(FIELD_TYPES),
                                             "description": "number 数字滑杆 / color 取色 / "
                                                            "text 单行文案 / bool 开关 / "
                                                            "select 下拉"},
                                    "label": {"type": "string", "maxLength": 24,
                                              "description": "表单上给人看的那几个字；"
                                                             "别拿 name 当标题，那是机器名"},
                                    "value": {"description": "初值，类型要与 type 对得上："
                                                             "number 写数字、color 写 #rgb 或 "
                                                             "#rrggbb、text 写字符串"
                                                             f"（≤{MAX_FIELD_TEXT_CHARS} 字）、"
                                                             "bool 写 true/false、select 写的值"
                                                             "必须在 options 里。**声明期就校验，"
                                                             "越界打回不钳值**——钳了就是账上写"
                                                             "一个数、画上是另一个数，而这条通道"
                                                             "存在的理由恰恰是消灭这种不一致"},
                                    "min": {"type": "number",
                                            "description": "仅 number：可取下限"},
                                    "max": {"type": "number",
                                            "description": "仅 number：可取上限"},
                                    "step": {"type": "number",
                                             "description": "仅 number：步长，要大于 0"},
                                    "unit": {"type": "string", "maxLength": 12,
                                             "description": "仅 number：单位后缀，只上界面"},
                                    "options": {"type": "array", "items": {"type": "string"},
                                                "maxItems": MAX_OPTIONS,
                                                "description": "仅 select：候选值，非空"},
                                },
                            },
                        },
                        "min_duration_sec": {"type": "number",
                                             "description": "这一镜**至少**停多少秒（0~60）。"
                                                            "有旁白时真正的长度归 TTS 实测，"
                                                            "这里只当下限——别写成承诺值"},
                        "duration_sec": {"type": "number",
                                         "description": "**已废弃**：写了按 min_duration_sec "
                                                        "理解。时钟归声音，模型不定时长"},
                        "texture": {"type": "string", "enum": list(TEXTURES),
                                    "description": "这一镜的质感档，**可省略**：省略就跟随"
                                                   "整片默认那一档（顶层 texture）。显式写 "
                                                   "none 表示这一镜不加质感，即使整片加了。"
                                                   "滤镜跑在拼接之前的那一趟逐镜片段上，"
                                                   "所以换它**不重烧这一镜的帧**——切片缓存存"
                                                   "的是加质感之前的像素"},
                        "source": {"type": "object",
                                   "description": "出处 {\"标题\": …, \"链接\": …}。机器不判真伪，"
                                                  "它如实出现在分镜卡与逐镜账上（不进画面）。"
                                                  "**出处闸**：写「查证过」的出处必须对得上本会话"
                                                  "的检索账（web_search / fetch_url 真打开过的页面），"
                                                  "对不上就退回；凭记忆请写「未核实：…」"},
                    },
                },
            },
        },
        "required": ["shots"],
    }


def overlay_knobs(spec: dict[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """把顶层用户开关并进 spec，返回新副本；**卡上勾选的值赢过模型写在 spec 里的值**。

    为什么是覆盖而不是「spec 优先」：一次勾选是人对这条片子的决定（要竖屏就是要竖屏），
    而 spec 里那份是模型自己填的默认。两者不一致时按人的来，否则卡面就成了装饰。

    ``narration=false`` 必须照样生效，所以判「有没有传」用的是 None 哨兵而不是真值。
    值本身不在这儿校验——合完还要过 ``validate_code_spec``，越界照样回错误清单。
    """
    out = dict(spec)
    for key in KNOB_FIELDS:
        value = inputs.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key == "narration" and isinstance(value, str):
            # 卡面上的布尔终值是字符串 "true"/"false"，而 bool("false") 是 True——
            # 用户关掉旁白却渲出一整条配音片，这叫把开关读反了。
            text = value.strip().lower()
            if text in ("true", "false"):
                value = text == "true"
        out[key] = value
    return out


def estimate_sec(spec: dict[str, Any]) -> tuple[float, float]:
    """按字数估总时长区间（低, 高），用于出片前判断是否偏离目标。

    这是**估算**，只用来在计划阶段提示「比目标长一倍，删几镜吧」；真正落地的每镜
    时长由 TTS 实测回填。无旁白时按阅读速度（每字 0.16 秒 + 0.8 秒驻留）另算。
    """
    shots = spec.get("shots") or []
    chars = sum(len(str(s.get("text") or "")) for s in shots)
    if spec.get("narration"):
        rate_pct = 0
        try:
            rate_pct = int(str(spec.get("rate") or "+0%")[:-1])
        except ValueError:
            rate_pct = 0
        speed = 1.0 + rate_pct / 100.0          # 语速越快，秒数越少
        factor = max(0.4, speed)
        low = chars * SEC_PER_CHAR_LOW / factor + 0.6 * len(shots)
        high = chars * SEC_PER_CHAR_HIGH / factor + 1.2 * len(shots)
    else:
        low = chars * 0.16 + 0.8 * len(shots)
        high = chars * 0.24 + 1.4 * len(shots)
    return round(low, 1), round(high, 1)


def check_target(spec: dict[str, Any], target_sec: float | None) -> list[str]:
    """目标时长对账：只出提醒，不硬拒——删镜还是加速是人的决定。"""
    if not target_sec or target_sec <= 0:
        return []
    low, high = estimate_sec(spec)
    if high < target_sec * 0.75:
        return [f"按当前文案估算只有 {low:.0f}~{high:.0f} 秒，比目标 {target_sec:.0f} 秒短不少："
                f"加镜或放慢语速"]
    if low > target_sec * 1.35:
        return [f"按当前文案估算 {low:.0f}~{high:.0f} 秒，比目标 {target_sec:.0f} 秒长不少："
                f"删镜或缩短每镜文案（当前 {len(spec.get('shots') or [])} 镜）"]
    return []
