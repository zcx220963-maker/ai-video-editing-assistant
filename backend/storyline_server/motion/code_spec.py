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
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

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

# R1: card / derived_from 契约栏——模型必须声明选了哪张卡、代码来自哪些 demo
_SKILL_DIR = Path(__file__).resolve().parents[2] / "examples/skills/motion_explainer"

@lru_cache(maxsize=1)
def motion_skill_name() -> str:
    """本技能在 ``skills`` 表里的名字（= ``SKILL.md`` frontmatter 的 ``name``）。

    为什么要读而不是写死一个常量：取材轮给模型的提示里要出现
    ``load_skill(name=…)``，而 ``load_skill`` 只认 frontmatter 那个名字——它**不等于**
    目录名（目录叫 ``motion_explainer``，技能叫 ``motion_explainer_skill``）。在这里
    再抄一份常量，改 frontmatter 时就漂，而漂移的表现是「提示让模型去 load_skill，
    模型照做却得到『未找到技能』」。

    读不到返回空串：调用方据此**不说**「用这个名字」——说错名字比不说更坏。
    """
    md = _SKILL_DIR / "SKILL.md"
    if not md.is_file():
        return ""
    try:
        text = md.read_text("utf-8")
    except OSError:
        return ""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return ""
    nm = re.search(r"^name:\s*(\S+)\s*$", m.group(1), re.M)   # 只在 frontmatter 里找
    return nm.group(1) if nm else ""


@lru_cache(maxsize=1)
def _known_card_ids() -> frozenset[str]:
    shots_dir = _SKILL_DIR / "shots"
    if not shots_dir.is_dir():
        return frozenset()
    ids = set()
    for cat in shots_dir.iterdir():
        if cat.is_dir() and not cat.name.startswith((".", "_")):
            for card in cat.glob("*.md"):
                ids.add(f"{cat.name}/{card.stem}")
    return frozenset(ids)

@lru_cache(maxsize=1)
def _known_demo_relpaths() -> frozenset[str]:
    man = _SKILL_DIR / "demos/MANIFEST.json"
    if not man.is_file():
        return frozenset()
    try:
        return frozenset(m["relpath"] for m in json.loads(man.read_text("utf-8")))
    except Exception:
        return frozenset()

#: 相似度阈值。**现值由 `scripts/calibrate_derived_sim.py` 复现**（Q7 的要求：标定脚本
#: 在容器内跑不出来这个数，闸⑫ 就算未完成）。2026-10-13 全语料实测（158 份可比 demo、
#: 12403 对负样本、扰动种子 20261012）：
#:
#:   正样本（一份 demo ↔ 换掉文案/色号/import 的它自己）  158/158 = 1.000
#:   负样本（两份不同 demo）  min 0.028  median 0.400  p95 0.578  p98 0.605  max 0.736
#:   在 0.61 上的负样本通过率 = 1.690%（线：≤ 2%）
#:
#: 选阈值的规则是「满足正样本召回 ≥95% 且负样本通过 ≤2% 的**最小**格点」，扫下来
#: 0.60 → 2.400%（超线）、0.61 → 1.690%，所以 0.61 就是那个边界上的最小值，不是拍的。
#: 取最小的理由：这条闸打回的是**声明造假**，阈值抬高会误伤真派生的镜，而误伤的代价
#: 是模型把 `derived_from` 当成形式主义填。
#:
#: 上面这几个十进制数是**容器里**跑出来的（`docker run --rm --entrypoint sh
#: creation-app:latest -c 'cd /app && python scripts/calibrate_derived_sim.py'`）。
#: 拿 Windows 工作区（`core.autocrlf=true`，检出来是 CRLF）跑同一个脚本，通过率会是
#: 1.685%，分位点一模一样——差别只在 LF/CRLF 让少数几份 demo 的 token 串动了几个，
#: 所以**发布口径认容器那一组数**。别照着本机屏幕上那个数改这里。
#:
#: 换种说法：这一组数不是「某次跑出来的」，是「在 sanctioned 环境里每次跑都一样的」。
DERIVED_MIN_SIM = 0.61

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

# ---------------------------------------------------------------------------
# 转场与入场/出场（R7-T22 / §3.1 / §9-Q11）
# ---------------------------------------------------------------------------
#: 镜**之间**的转场风格。落点是**拼接层**（ffmpeg `xfade`），不是镜头层——
#: `@remotion/transitions` 的 `<TransitionSeries>` 要求相邻片段在**同一棵 React 树**里，
#: 而我们逐镜独立渲、再拼（D 面那条逐镜缓存的保证全靠这个）。硬套那套包 = 逐镜缓存
#: 直接作废，所以这里连 import 名单都不放它进（§3.1 已冻结）。
#:
#: 取值就是 ffmpeg `xfade` 的 `transition=` 名（`mediaops._XFADE` 那张表的键），
#: 不另起一套别名：多一层映射就多一处「界面写 fade、命令行写别的」的机会。
#: `none` 是默认——不选转场的片子逐字节不变，回归就永远有那一侧可以比。
TRANSITION_STYLES = ("none", "fade", "dissolve", "wipeleft", "wiperight",
                     "slideup", "circleopen")

#: 转场时长（秒）。**做成枚举而不是自由数字**：计划卡的开关模型是「枚举 / 布尔 /
#: 带界数值」三选一，而每一项都要能反查到取值来源、都要有一句中文标签
#: （``plan/vocab.py::knob_facts`` 与 tests/test_motion_channel.py 第 ① 节按此断言）。
#: 档位只留四档，四档之间是「切得干不干净」的观感差别，再细分没有意义。
#: 上界 0.9 s 也不能乱给：真判它合不合法的是渲染期那条 :func:`check_transition`
#: ——转场时长不得长过**相邻较短那一镜的一半**（xfade 拿两边的重叠区做混合，
#: 重叠超过半镜就不是转场而是把这一镜换掉），而一镜多长要等 TTS 出来才知道。
TRANSITION_SEC_CHOICES = ("0.2", "0.4", "0.6", "0.9")
TRANSITION_SEC_DEFAULT = "0.4"

#: 转场时长还不得长过**相邻较短那一镜的一半**：xfade 是拿两边的重叠区做混合的，
#: 重叠超过半镜就不是转场而是「把这一镜换掉」。判据在 :func:`check_transition`，
#: 边界取严格小于（等于半镜时第一段的 offset+duration 正好顶到它自己的末尾）。
TRANSITION_SHOT_SHARE = 0.5

#: 母版（`remotion/src/FilmLayout.tsx`）按注入的档名 + 帧数演的入场/出场。四档动作都是
#: 「已经落定」之前/之后的那一小段：淡、上浮、下落、轻推近。工艺上全是
#: `useCurrentFrame()+interpolate()`（§3.2：CSS `transition`/`animation` 在本链
#: 逐帧 seek 下渲不正确）。同一个取值在入场与出场里是**互为反向**的两个动作
#: （rise：自下浮入 ↔ 向上浮出），标签因此分两张表（见 ``KNOB_VALUE_LABELS``）。
SHOT_MOTION_STYLES = ("none", "fade", "rise", "drop", "zoom")

#: 入场/出场窗口的上限：这一镜的 10%，且不超过 15 帧。两条各自有理由——
#: ① 10% 是为了**不碰闸④ 的采样点**：`EMPTY_SAMPLES=0.15/0.5/0.85`，入场窗口收在
#:    0.15 之前，那一帧读到的就还是「落定后」的画面；② 15 帧（@15fps 是 1 秒、
#:    @30fps 是半秒）是「入场抢的戏不该多过一镜的十分之一」在长镜上的兜底。
#: **这两个数只在这一处**：窗口按帧数注入母版（`shot_input_props` 的
#: `entranceFrames`/`exitFrames`），TS 那侧只负责按收到的帧数插值。母版是 TS，跨不了
#: 语言，所以「TS 里别再算一遍」这件事靠的是**不把常量交到那边去**，而不是靠对账；
#: tests/test_code_transition.py ⑤ 逐帧扫 2~600 帧，把「窗口从不碰 0.15」这条关系
#: 钉成算术（不是注释里的一句好听的话）。
SHOT_MOTION_MAX_SHARE = 0.10
SHOT_MOTION_MAX_FRAMES = 15

# ---------------------------------------------------------------------------
# ㉑ 动效锚定（R8-T33）：「每个动效事件必须说清它为哪条数据服务」
# ---------------------------------------------------------------------------
# 「动效有没有意义」这句话没法判——谁也写不出一条能报红的规则。这一道把它换成可测的
# 另一句：**这个事件为哪条数据服务**（一个字段 / 一个字幕词 / 一个音乐节拍），而判据只
# 取机器判得动的那一半：**事件触发的那一帧，被锚的那条数据是不是正好可以读了**。
# 「这个数据值不值得动」仍然归人眼——见 §10 的措辞表，不许把这一道说成「动效已被理解」。

#: 一个动效事件最多挂几个锚。上限不为省字节，为**堵住一条绕法**：锚是「所有锚都必须在
#: 窗口内」才算数（见 :func:`check_motion_windows`），把整句话二十个词全锚上就等于
#: 「总有一个词在附近」，紧跟着的上限让这条绕法在字数上先走不通。
MOTION_MAX_ANCHORS = 3

#: 锚的三种写法。**这不是给人看的语法介绍，是白名单**——认不出的写法一律打回，
#: 因为一个写错的锚（比如 `field.x` 少了个 s）在回执上看起来和真锚一模一样。
MOTION_ANCHOR_FORMS = ("fields.", "subtitle:", "beat:")

#: 窗口容差 k = fps // 这个数（@30fps 是 10 帧 ≈ 0.33 秒）。
#: **为什么是 fps 的函数而不是一个帧数**：这条容差量的是「人对齐得准不准」，而人对齐的
#: 精度是按**时间**走的（三百毫秒左右），把帧数写死会让 15fps 的片子容差翻倍、60fps 的
#: 片子容差减半。附录 B 要求这个数由真渲分布标定（取 95 分位），**标定脚本还不存在**——
#: 这一点记在 §9-Q98，不许把「3」当成标定结果。
MOTION_ANCHOR_TOLERANCE_DIV = 3

#: 母版 `<FilmLayout>` 真认的字幕形态。旧链那四档（torn/bottom…）随模板一起消失：
#: 新链的字幕带只画在母版里，母版没实现的形态就不该出现在可选项里。
#:
#: R14 加第四档 `inline`：**母版不画字幕条**，文字由镜头代码自己排进画面。
#: 前三档都是「母版在画面带下方那条固定白底圆角条里画」或「不画」，只有 `inline`
#: 能做到「这一镜的字压在画面上、随构图摆」——参考片普遍这么做，而这也是
#: 成片不再长得像 PPT 的关键一档（每镜一条同款字幕带正是那个观感的来源）。
SUBTITLE_MODES = ("full", "torn_highlight", "none", "inline")
#: 字幕位置与入场动画（2026-10-15）：模型按构图与片子气质选，整片一档 + 逐镜可覆盖。
#: 与 remotion/src/tokens.ts 的 CAPTION_POSITIONS / CAPTION_ANIMS 同一份枚举。
CAPTION_POSITIONS = ("bottom", "center", "top")
CAPTION_ANIMS = ("fade", "rise", "pop", "karaoke")

#: 一段镜头代码的体积上限。lint 与 bundle 都按字节走，超了不是"风格问题"而是
#: 一次请求把容器内存吃掉一截——这条链一镜一次浏览器，宁可在这里拦。
MAX_CODE_BYTES = 40_000

# ---------------------------------------------------------------------------
# 字段参数通道（spec §12）：把"可改的东西"从代码里提出来当数据
# ---------------------------------------------------------------------------

#: 只有这五种，因为前端只有这五种控件。第六种类型的字段会落成一个没人认识的输入框
#: ——控件的单一来源是这份 type（旧链 #127「补丁口把字段类型也发出去」那条纪律的延续）。
FIELD_TYPES = ("number", "color", "text", "bool", "select", "image")

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
#:
#: R7-T22 加的 `entrance`/`exit` 也在这里，理由与 `subtitle_mode` 同一条：入场/出场是
#: **母版**在切片里画的（`FilmLayout.tsx` 按 token 逐帧插值），所以它换的是切片像素。
#: 而同一批加进来的 `transition`/`transition_sec` **故意不在**：转场跑在**拼接**那一趟
#: （`mediaops.concat` → ffmpeg `xfade`），逐镜切片一个字节都不动——与 `texture` 同族，
#: 进键的代价是「整片把淡出换成擦除」要重烧 20 镜的 Remotion 帧，而它本来只是一趟
#: 拼接。tests/test_code_transition.py 对这两侧各有一支断言（在键 / 不在键）。
FILM_KNOBS = ("fps", "aspect", "narration", "voice", "rate", "subtitle_mode",
              "entrance", "exit", "caption_position", "caption_anim")

#: 选区改（P3）对代码镜只开这五条指针。`code` 的改动必须重过闸① 才允许落盘；
#: `fields/{name}/value` 不动代码字节，改的是注入母版的值，所以不重跑 lint，
#: 只按 `check_field_value` 那份与声明期同源的规则校验（越界打回，不钳值）；
#: `texture` 既不动代码也不动像素来源，改的是缓存之后的那一趟滤镜。
EDITABLE_POINTERS = ("/shots/{i}/code", "/shots/{i}/text", "/shots/{i}/source",
                     "/shots/{i}/fields/{name}/value", "/shots/{i}/texture")


def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def _norm_assets(raw: Any, sid: str, errors: list[str]) -> list[dict[str, Any]]:
    """归一化 assets 栏。每项必须有 name/sha256/ext，sha256 是完整 64 位十六进制。"""
    if not raw:
        return []
    if not isinstance(raw, list):
        _err(errors, f"{sid}：assets 不是数组")
        return []
    out: list[dict[str, Any]] = []
    for j, a in enumerate(raw):
        if not isinstance(a, dict):
            _err(errors, f"{sid}：assets[{j}] 不是对象")
            continue
        name = str(a.get("name") or "").strip()
        sha256 = str(a.get("sha256") or "").strip().lower()
        ext = str(a.get("ext") or "").strip().lstrip(".")
        if not name:
            _err(errors, f"{sid}：assets[{j}].name 为空——代码里 staticFile(\"<name>\") 引用需要文件名")
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            _err(errors, f"{sid}：assets[{j}].sha256={sha256!r} 不是完整 64 位十六进制")
            sha256 = ""
        if not ext:
            _err(errors, f"{sid}：assets[{j}].ext 为空——渲染容器要按扩展名落盘")
        item: dict[str, Any] = {"name": name, "sha256": sha256, "ext": ext}
        if isinstance(a.get("source"), dict):
            item["source"] = a["source"]
        if isinstance(a.get("alpha"), bool):
            item["alpha"] = a["alpha"]
        layer = str(a.get("layer") or "").strip()
        if layer in ("fg", "bg", "cutout"):
            item["layer"] = layer
        out.append(item)
    return out


#: ``derived_from`` 的两种写法：``MANIFEST.json`` 里的 relpath 是**相对 ``demos/``**的
#: （``opening/fracture/Fracture.tsx``），而 ``load_skill(name, path=…)`` 要的是**相对技能
#: 目录**的（``demos/opening/fracture/Fracture.tsx``）。**同一份文件两个写法**，而模型
#: 看到的契约里两处都写着（schema 的示例写带前缀的，白名单是不带的）——只认一边的话，
#: 照着另一边抄的模型会收到一整片「不在白名单中」，而它抄的明明是同一份文件。
#: 所以这里归一：两种都收，**存下来的只有一种**（不然闸⑫ 会拿到两种拼法的同一个 relpath，
#: 逐镜账里也就分不清「引用的是哪一份」）。
DEMO_PREFIX = "demos/"


def _norm_demo_relpath(d: str) -> str:
    """``demos/x/y.tsx`` / ``./demos/x/y.tsx`` / ``x/y.tsx`` → ``x/y.tsx``（其余原样返回）。"""
    d = d.strip()
    while d.startswith("./"):
        d = d[2:]
    return d[len(DEMO_PREFIX):] if d.startswith(DEMO_PREFIX) else d


#: 打回时最多点几份真文件名（一份一行的写法是文件小，读起来才知道是几份）。
_DEMO_SUGGEST_MAX = 6


def demo_dir_files(rel: str, known: Any = None) -> tuple[str, list[str]]:
    """``derived_from`` 写错时，盘上真正能写的是哪几份 → ``(目录, 文件名列表)``。

    真机实测（14 镜那条 run，``034d6b4201ea``）：模型照「卡名 → `<卡名驼峰>.tsx`」的写法
    填 ``derived_from``，14 镜里 **11 镜命中**——这个规律在语料里多数卡上成立，所以它不是
    乱猜，是照多数派归纳出来的。落空的那 3 镜恰好是「卡名 ≠ demo 文件名」：
    ``data/chart-live-moves`` 下真正的三份叫 AxisRescaleShockV2 / OscilloscopeStreamV2 /
    UnitDotSwarmRegroupV2，而 INDEX.md 那一行给的是**卡名**（末尾的 46,666 B 是该目录
    三份之和，不是某一份）。它拿不到名字，只能猜；猜错之后原先的回执又只叫它「从
    demos/MANIFEST.json 里选」——那是一个 10 KB 的附件，又是一次往返。所以名字直接给。

    判据只有「同一个目录」这一条（字符串前缀），**不做相似度猜名字**：模型要的是真的那
    几份，给它一个「看起来像」的反而引出下一轮瞎猜。两种写法都认——给了目录名本身
    （``data/chart-live-moves``）与给了目录下的错文件名（``data/chart-live-moves/X.tsx``）。
    目录也对不上（比如整个类别名写错）就返回空：那时能给的只有整份清单，而清单本来就在
    INDEX.md / MANIFEST.json 里，回执不必再抄一遍（抄了也只是把 10 KB 挪个地方）。
    """
    rels = tuple(known if known is not None else _known_demo_relpaths())
    if not rels:
        return "", []
    rel = _norm_demo_relpath(rel).rstrip("/")
    dirs = {r.rsplit("/", 1)[0] for r in rels if "/" in r}
    d = rel if rel in dirs else (rel.rsplit("/", 1)[0] if "/" in rel else "")
    if d not in dirs:
        return "", []
    return d, sorted(r.rsplit("/", 1)[1] for r in rels if r.startswith(d + "/"))


def _norm_cite(raw: Mapping[str, Any], sid: str,
               errors: list[str]) -> tuple[str, list[str]]:
    """``card`` / ``derived_from`` 两栏的归一与白名单校验——**单源**。

    为什么抽出来：这两栏在**两个地方**被读——分镜轮（``_norm_shot`` 经
    ``validate_code_spec``）与取材轮（``collect_demos``）。两处各写一遍「从哪张卡来、
    派生自哪几份 demo」的判断，就会出现「取材轮放行了分镜轮不认的 relpath」这种
    「取材白取了」的形状：模型拿到全文、写完代码、再被同一个白名单打回。
    所以白名单只有这里一份，两轮共用。

    「为空」也要在这里报：取材轮不经过 ``validate_code_spec``，不在这里拦，
    一份没声明出处的骨架会被当成取材成功——那正是「手搓」的入口。
    """
    card = str(raw.get("card") or "").strip()
    if not card:
        _err(errors, f"{sid}：card 为空——每一镜必须声明选了哪张 shot 配方卡（格式：<类别>/<卡名>）")
    else:
        known = _known_card_ids()
        if known and card not in known:
            _err(errors, f"{sid}：card={card!r} 不在卡库中（共 {len(known)} 张卡），"
                         f"模型不能自创卡名，必须从 shots/INDEX.md 里选")

    derived_from = raw.get("derived_from") or []
    if not isinstance(derived_from, list):
        derived_from = [str(derived_from)]
    listed = [str(d).strip() for d in derived_from if str(d).strip()]
    # 闸⑳ 的哨兵（R19）走**另一条路**：它不是一个 relpath，所以不能进 demo 白名单比对
    # （`_norm_demo_relpath` 会把它当路径改写，白名单那一段也会报「不在语料里」——
    # 而那两句都不是事实）。哨兵只认**单独出现**：与真 demo 混在一张表里就分不清
    # 这一镜到底算不算原创，而「每镜都挂哨兵 + 顺手抄一份 demo」正是这条口子最省事的绕法。
    if ORIGINAL_SENTINEL in listed:
        others = [d for d in listed if d != ORIGINAL_SENTINEL]
        if others:
            _err(errors, f"{sid}：derived_from 里既有原创哨兵 {ORIGINAL_SENTINEL} 又列了 demo"
                         f"（{'、'.join(others)}）——哨兵必须**单独**出现："
                         "「这一镜从某份 demo 演化而来」与「这一镜不派生自任何 demo」"
                         "是互斥的两句话，混在一张表里，闸⑳ 的配额就数不准，"
                         "闸⑫ 也不知道该不该比骨架")
        return card, [ORIGINAL_SENTINEL]

    derived_from = [_norm_demo_relpath(d) for d in listed]
    derived_from = [d for d in derived_from if d]
    if not derived_from:
        _err(errors, f"{sid}：derived_from 为空——每一镜必须声明代码来自哪些 demo（relpath 列表）")
    else:
        known_demos = _known_demo_relpaths()
        if known_demos:
            for d in derived_from:
                if d in known_demos:
                    continue
                # 打回要**能照着改**。旧措辞只叫它「从 demos/MANIFEST.json 里选」——那是一份
                # 10 KB 的附件：模型得再取一次、再读一遍，才拿到「这一个目录下真正叫什么」。
                # 真机那条 run（034d6b4201ea）就卡在这里收尾：3 镜被同一条打回，它没有名字
                # 可抄，只能改用「换成同族的卡 / 砍成 11 镜」去问用户。名字给出来，就够了。
                d_dir, files = demo_dir_files(d, known_demos)
                if files:
                    shown = files[:_DEMO_SUGGEST_MAX]
                    tail = "" if len(shown) == len(files) else f"…（共 {len(files)} 份）"
                    _err(errors, f"{sid}：derived_from={d!r} 不在 demo 语料白名单中"
                                 f"（共 {len(known_demos)} 个 demo）——{d_dir}/ 下真有的几份："
                                 + "、".join(f"{d_dir}/{f}" for f in shown) + tail
                                 + "。**照抄其中一份**：这一栏要的是具体某一份文件，"
                                   "不是目录名，也不是照卡名拼出来的 <卡名>.tsx"
                                   "——卡名与 demo 的文件名常常不是一回事")
                else:
                    _err(errors, f"{sid}：derived_from={d!r} 不在 demo 语料白名单中"
                                 f"（共 {len(known_demos)} 个 demo）——照 shots/INDEX.md 那一行"
                                 f"的 demo 栏抄（那一栏给的就是真文件名），"
                                 f"或从 demos/MANIFEST.json 里选")
    return card, derived_from


def _norm_shot(raw: dict[str, Any], i: int, errors: list[str], *, max_chars: int) -> dict[str, Any]:
    sid = str(raw.get("id") or f"s{i + 1:02d}")
    code = str(raw.get("code") or "")
    if not code.strip():
        # 提示按「怎么改」写而不是按「错在哪」写：真机上模型交一份没带 code 的骨架时，
        # 它想要的多半是取材（见 ``forage_scan``），而这句话是它唯一能看到的岔路口。
        _err(errors, f"{sid}：code 为空——新链每一镜必须交一段真实代码，没有默认版式可退"
                     "（想先读被引用 demo 的全文再写码，就把这一轮交成**取材轮**："
                     "所有镜都带上 card 与 derived_from、都**不带 code**，本工具会把那几份"
                     "全文回给你；只带一部分镜是分不清意图的写法，会被打回）")
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

    # R14 风格档（每镜）：满幅与底色。**留空 = 不申报**——母版据此用缺省（有安全区、
    # 底色 TOKENS.bg），也就是今天的行为。这两项改的是像素，所以进缓存键；但它们
    # **不进 `SHOT_KNOBS`**，而是像 `reveal` 那样只在真有值时进键（见 `shot_cache_key`：
    # 无条件加会让 `None` 也成为一个值，所有既有镜的键全变、整片白烧一次）。
    #
    # `bleed` 只收布尔：满幅是「是/否」，写成 `"true"` / `1` / `"yes"` 在母版那侧
    # 全落 false（`resolveFilmStyle` 判的是 `=== true`），静默不生效——那正是这条链
    # 最该避免的失败模式，所以在这里当场打回。
    bleed = raw.get("bleed")
    if bleed is not None and not isinstance(bleed, bool):
        _err(errors, f"{sid}：bleed={bleed!r} 不是布尔——满幅是「是/否」，"
                     "写字符串或数字母版认不出（会静默当成不满幅）")
        bleed = None
    # `bg` 只收 #rrggbb：母版拿它当底色，**闸④ 还拿它当判空的基准色**
    # （`code_render._bg_of` 读不到合法基准色就拒绝判空）。`rgb()`/命名色/渐变
    # 一律不收——收了等于让闸④ 在这一镜上闭嘴。
    bg = str(raw.get("bg") or "").strip()
    if bg:
        if _HEX_RE.match(bg):
            bg = _expand_hex(bg)
        else:
            _err(errors, f"{sid}：bg={bg!r} 不是 #rrggbb —— 母版拿它当底色、"
                         "闸④ 拿它当判空的基准色，只收 6 位十六进制（如 #0b1620）")
            bg = ""

    # 层E1 逐接缝转场（R19）：这一镜**到下一镜**那一处接缝的档。省略＝跟整片
    # `transition`（也就是今天的行为）。最后一镜写它无效——它后面没有接缝，
    # 所以不报错也不打回，只是不落账。
    #
    # 为什么这一栏**不进** `SHOT_KNOBS`：转场跑在拼接那一趟（ffmpeg `xfade`），
    # 逐镜切片一个字节都不动，进缓存键就是「整片把淡出换成擦除要重烧 20 镜」。
    # 与顶层那两栏同族，见 `FILM_KNOBS` 那段注释。
    seam = str(raw.get("transition") or "").strip().lower()
    if seam and seam not in TRANSITION_STYLES:
        _err(errors, f"{sid}：transition={seam!r} 没有对应的转场"
                     f"（可用：{', '.join(TRANSITION_STYLES)}）")
        seam = ""
    # 层E2 逐镜字幕档（R19）：省略＝跟整片 `subtitle_mode`。**进缓存键**——字幕带是
    # 母版逐帧画进切片里的（与 `entrance`/`exit` 同族），不进键就会拿旧切片冒充新片。
    sub = str(raw.get("subtitle_mode") or "").strip()
    if sub and sub not in SUBTITLE_MODES:
        _err(errors, f"{sid}：subtitle_mode={sub!r} 不支持"
                     f"（母版只认：{', '.join(SUBTITLE_MODES)}）")
        sub = ""
    # 逐镜字幕位置 / 入场动画：省略＝跟整片那一档。与 subtitle_mode 同族，覆盖了才进缓存键。
    cap_pos = str(raw.get("caption_position") or "").strip().lower()
    if cap_pos and cap_pos not in CAPTION_POSITIONS:
        _err(errors, f"{sid}：caption_position={cap_pos!r} 不支持"
                     f"（可用：{', '.join(CAPTION_POSITIONS)}）")
        cap_pos = ""
    cap_anim = str(raw.get("caption_anim") or "").strip().lower()
    if cap_anim and cap_anim not in CAPTION_ANIMS:
        _err(errors, f"{sid}：caption_anim={cap_anim!r} 不支持"
                     f"（可用：{', '.join(CAPTION_ANIMS)}）")
        cap_anim = ""

    # R1: card / derived_from 契约栏
    card, derived_from = _norm_cite(raw, sid, errors)

    return {
        "id": sid,
        "code": code,
        "text": text,
        "highlight": highlights,
        "fields": _norm_fields(raw.get("fields"), sid, errors),
        "min_duration_sec": min_sec,
        "texture": tex,
        # R14 风格档（每镜）。这里归一（`None` / 空串 = 没申报），但**只有真申报了的**
        # 才进缓存键与注入——见 `film_style_of`。`None`/空串与「申报了 false/一个色」
        # 在母版那侧是两件事：前者落缺省，后者落申报值。
        "bleed": bleed,
        "bg": bg,
        # 层E1/E2（R19）两栏：省略记 `None`。`None` 与「申报了 none」在**母版那侧**
        # 是同一件事（都落整片档），但在**账面**上要分得开——回执与补丁链读的是这里。
        "transition": seam or None,
        "subtitle_mode": sub or None,
        "caption_position": cap_pos or None,
        "caption_anim": cap_anim or None,
        "source": {str(k): str(v) for k, v in src.items()},
        "card": card,
        "derived_from": derived_from,
        "assets": _norm_assets(raw.get("assets"), sid, errors),
        # 闸⑯ 的两栏（R5-T15）。归一在这里、判据在整片层（check_subject_named）——
        # 与 card/derived_from 同一分工：这一层只把形状收干净（去空白、通道名统一小写），
        # 「填没填、有没有证据」是那一层的事，因为它要读**代码**算通道。
        "subject": str(raw.get("subject") or "").strip(),
        "subject_channel": str(raw.get("subject_channel") or "").strip().lower(),
        # ㉑ 的申报（R8-T33）。**这一栏刻意不在这里归一**，与上面那两栏反着来：
        # `check_motion_anchors` 是它形状的唯一主人，而它要判的正是「写法对不对」——
        # 在这里顺手把非字符串的锚丢掉、把认不出的写法清成空表，都会把「写了但认不出」
        # 悄悄修成「没写」，也就是把 ㉑ 的红样②（空锚打回）变成一条永远不响的路。
        # 所以原样带着走，连大小写与空白都不动：回执上要能拿着锚串逐字核对。
        # 它**不进缓存键**（`SHOT_KNOBS` 里没有它，刻意）：改申报不改变任何像素，
        # 改了就该重烧一次是浪费——代价是命中缓存的镜必须按**当前**申报重判，见
        # `code_render` 缓存路径上那一段。
        "motion": raw.get("motion"),
        # 闸㉘ 的申报（R12-T59）。与 `motion` 同一套分工：形状的唯一主人是
        # `check_reveal_regions`，所以这里**原样带着走**，连大小写与空白都不动
        # （在归一里把认不出的写法清成空表，就是把「写了但写错了」修成「没写」）。
        # **但它与 `motion` 有一处决定性的不同：它进缓存键**（见 `SHOT_KNOBS` 那条注释）
        # ——`motion` 只是「这条动效为谁动」的申报，改它一个像素都不变；而 `reveal`
        # 是母版**照着它逐帧算 mask** 的输入，改一次就是改像素。
        "reveal": raw.get("reveal"),
    }


def expand_code_refs(raw: Mapping[str, Any]) -> dict[str, Any]:
    """`code_from`：一镜复用另一镜的代码（只换文案 / 字段 / 色板）→ 展开后的 spec 副本。

    为什么要有：长片（有旁白的 4 分钟要四十多镜）每镜都交一整段 TSX，一次工具调用装不下，
    模型只能「说加到 46 镜」却原样重交 14 镜（真机 run 79dcdc0ec0a8）。版式本来就该复用——
    十几种版式撑几十镜，靠 `fields` 与 `text` 换内容。展开在校验之前做，下游（lint、缓存键、
    渲染）看到的都是完整代码，与手写一份没有区别。`card` / `derived_from` 没写就跟着源镜。
    引用不存在或源镜也没代码时原样留着，由校验报「这一镜没有代码」。
    """
    shots = raw.get("shots")
    if not isinstance(shots, list):
        return dict(raw)
    by_id = {str(s.get("id") or ""): s for s in shots if isinstance(s, Mapping)}
    out: list[Any] = []
    for s in shots:
        if not isinstance(s, Mapping) or s.get("code") or not s.get("code_from"):
            out.append(s)
            continue
        src = by_id.get(str(s.get("code_from")))
        if not src or not src.get("code"):
            out.append(s)
            continue
        row = {k: v for k, v in s.items() if k != "code_from"}
        row["code"] = src["code"]
        for k in ("card", "derived_from", "subject_channel"):
            if not row.get(k) and src.get(k):
                row[k] = src[k]
        out.append(row)
    return {**raw, "shots": out}


def validate_code_spec(raw: dict[str, Any], *,
                       has_stage_leg: bool | None = None) -> tuple[dict[str, Any], list[str]]:
    """→ (规范化后的 spec, 错误清单)。错误清单非空即**不可渲染**。

    这一层管两类事：① 逐镜「一定会把画面写坏」的几件事（代码在不在、文案超不超
    字幕容量、高亮词是不是子串、枚举在不在白名单内）；② **整片层**的五道硬闸——
    :func:`check_style_quota`（风格配额与纯文字镜占比）、:func:`check_shot_structure`
    （闸⑨：跨镜骨架同构）、:func:`check_ink_contrast`（闸⑩：字色在底色上读不读得出）、
    :func:`check_layout_family`（闸⑮：版式族配额）、:func:`check_subject_named`
    （闸⑯：画面主体具名）。
    代码**内容**对不对归闸①（容器里的 lint）与闸④（画面上有没有东西），史实对不对归
    出处闸与证据账。这几道只出**提醒**的那几份不从本函数返回——它们不拦，由
    ``style_facts`` 在计划卡与出片回执那两处点名。
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["spec 必须是对象"]
    raw = expand_code_refs(raw)

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

    caption_position = str(raw.get("caption_position") or "bottom").strip().lower()
    if caption_position not in CAPTION_POSITIONS:
        _err(errors, f"caption_position={caption_position!r} 不支持"
                     f"（可用：{', '.join(CAPTION_POSITIONS)}）")
        caption_position = "bottom"
    caption_anim = str(raw.get("caption_anim") or "fade").strip().lower()
    if caption_anim not in CAPTION_ANIMS:
        _err(errors, f"caption_anim={caption_anim!r} 不支持"
                     f"（可用：{', '.join(CAPTION_ANIMS)}）")
        caption_anim = "fade"

    texture = str(raw.get("texture") or "none").strip().lower()
    if texture not in TEXTURES:
        _err(errors, f"texture={texture!r} 没有对应的滤镜链（可用：{', '.join(TEXTURES)}）")
        texture = "none"

    # R7-T22：镜间转场（拼接层）与母版进出场。三条都是**整片开关**，逐镜不覆盖——
    # 转场天生是「两个镜之间」的东西，进出场是母版给每一镜套的同一套动作；
    # 让它们逐镜可配就该配到「某两个镜之间」，那是另一件事（今天没有这个需求）。
    transition = str(raw.get("transition") or "none").strip().lower()
    if transition not in TRANSITION_STYLES:
        _err(errors, f"transition={transition!r} 没有对应的转场"
                     f"（可用：{', '.join(TRANSITION_STYLES)}）")
        transition = "none"
    # 时长按**档位**收（理由见 TRANSITION_SEC_CHOICES）。写成数字也收：
    # 模型按 schema 的 number 提示写 0.4 是自然的，而 0.4 与 "0.4" 是同一个决定。
    span = raw.get("transition_sec", TRANSITION_SEC_DEFAULT)
    if span is None or (isinstance(span, str) and not span.strip()):
        span = TRANSITION_SEC_DEFAULT
    span_text = f"{float(span):g}" if _is_num(span) else str(span).strip()
    normalized_none = transition == "none" and span_text == "0"
    if span_text not in TRANSITION_SEC_CHOICES and not normalized_none:
        _err(errors, f"transition_sec={span!r} 不在可选档位里"
                     f"（可用：{', '.join(TRANSITION_SEC_CHOICES)} 秒）")
        span_text = TRANSITION_SEC_DEFAULT
    if transition == "none":
        # 硬切＝重叠 0 秒。归一而不是留原值：留着的话，「none + 0.9」与「none + 0.4」
        # 会在各处被读成两种片子（对账、确认卡），而画面上它们是同一件事。
        #
        # 这一格必须**收得下自己写下的 "0"**（上面那个 ``normalized_none``）：校验是
        # 幂等的——补丁链会把一份过过校验的 spec 再校验一遍（`overlay_knobs` 那条
        # 路两步都要过闸），不认自己的归一结果，第二次就会拿「账面 0 秒」报一个
        # 「不在可选档位里」：校验器对自己刚写下的值报错，看起来像用户的错。
        span_text = "0"
    motions = {}
    for key in ("entrance", "exit"):
        value = str(raw.get(key) or "none").strip().lower()
        if value not in SHOT_MOTION_STYLES:
            _err(errors, f"{key}={value!r} 没有对应的动作"
                         f"（可用：{', '.join(SHOT_MOTION_STYLES)}）")
            value = "none"
        motions[key] = value

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

    # 闸⑦⑨⑩⑮⑯ 的硬判据（整片风格配额与纯文字镜占比 / 跨镜同构 / 前景底色对比度 /
    # 版式族配额 / 主体具名）都挂在这里：三个调用点——plan_motion 入库前、
    # render_motion_code 烧像素前、patch_motion_code 落盘前——调的都是这一个函数，
    # 规则因此不可能只在一路上生效。
    # 提醒那一份从这里单独取（见 style_facts）。
    qc = str(raw.get("qc") or QC_STRICT).strip().lower()
    if qc not in QC_MODES:
        _err(errors, f"qc={qc!r} 不支持（可用：{', '.join(QC_MODES)}）")
        qc = QC_STRICT
    for checker in WHOLE_FILM_GATES:
        errors.extend(run_film_gate(checker, shots, strict=qc == QC_STRICT)[0])
    # 闸㉑ 的**闸前**那一半（R8-T33）：动效事件的锚是纯契约，判它一次 ffmpeg 都不用开，
    # 所以它跟上面那批整片闸并排放在这里，三个调用点（plan 入库前 / 烧像素前 / 落盘前）
    # 一起生效。「事件触发帧在不在窗口里」那一半要像素，在 code_render 逐镜那一段。
    errors.extend(check_motion_anchors(shots))
    # 闸㉘（R12-T59）：镜级 `reveal` 栏的几何与时序合法性。**也放在这里**，理由与 ㉑ 的
    # 闸前那一半一字不差：它是纯 schema 校验，判一次一次请求都不用发；而它必须与
    # ㉑ 共享同一个调用点，否则「计划轮过了、烧像素那一轮没过」这种半合法状态会出现。
    # ㉘ 的**另一半**（像素与时序对不对得上）是 ㉗，那一条要真帧，在 code_render 里。
    for s in shots:
        errors.extend(check_reveal_regions(s))
    # 闸⑱：投递腿存在性。只有调用方**知道**客户端能力时才判（None = 不判）——
    # plan_motion 那一侧还没有客户端，硬塞一个 None 当 False 会把「没接腿」误判成「缺腿」。
    if has_stage_leg is not None:
        errors.extend(check_stage_leg_present(shots, has_stage_leg=has_stage_leg)[0])

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
        "qc": qc,
        # 有配乐时把镜头切点卡到拍上（缺省开；没有配乐或检不出稳定的拍时什么都不做）。
        "beat_sync": raw.get("beat_sync") is not False,
        "caption_position": caption_position,
        "caption_anim": caption_anim,
        "texture": texture,
        "transition": transition,
        "transition_sec": span_text,
        "entrance": motions["entrance"],
        "exit": motions["exit"],
        "shots": shots,
        "bgm": {"ref": str(bgm.get("ref") or ""), "volume": bgm_volume,
                "duck": bool(bgm.get("duck", True))},
        "title": str(raw.get("title") or "")[:80],
        "target_duration_sec": raw.get("target_duration_sec"),
    }
    return spec, errors


def film_style_of(shot: Mapping[str, Any]) -> dict[str, Any]:
    """这一镜的风格档（R14）：**只带真申报了的键**。

    → `{}`，或 `{"bleed": bool, "bg": "#rrggbb"}` 的任意子集。

    为什么只带申报了的键、不补全成完整一份：这一份要**进缓存键**、还要**逐镜过 HTTP**
    （`code_render.shot_input_props` → 渲染容器）。补全之后 `{bleed: false, bg: ""}`
    也成了一个值，于是所有既有镜的键全部改变、整片白烧一次 Remotion 帧，而它们
    一个字节都没变——与 `shot_cache_key` 里 `reveal` 那一段是同一个教训。

    也正因为「在 / 值不同 / 不在」三态两两算出不同的键，这一处省下的只是无谓的坏键，
    不是判据：申报了 `bleed: false` 与没申报在母版那侧落同一个缺省，键不同只是多烧一次，
    不会把两种画面混成一个。
    """
    out: dict[str, Any] = {}
    bleed = shot.get("bleed")
    if isinstance(bleed, bool):
        out["bleed"] = bleed
    bg = str(shot.get("bg") or "").strip().lower()
    if bg:
        out["bg"] = bg
    return out


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
    # R2: 资产字节进缓存键——换图必须重烧，改注释不重烧
    assets = shot.get("assets") or []
    assets_hash = ""
    if assets:
        asset_pairs = sorted(
            f"{a.get('sha256','')}/{a.get('name','')}"
            for a in assets if isinstance(a, dict)
        )
        assets_hash = hashlib.sha256(
            "|".join(asset_pairs).encode("utf-8")
        ).hexdigest()[:16]
    payload = {
        "code": str(shot.get("code") or ""),
        "knobs": knobs,
        "film": {k: film.get(k) for k in FILM_KNOBS},
        "master": master_hash,
        "deps": lockfile_hash,
        "assets_hash": assets_hash,
    }
    # R12-T59：`reveal` 是**母版逐帧算 mask 的输入**，所以它必须进键——与 `motion`
    # 恰好相反（那一条刻意不进，理由写在 `_norm_shot` 里）。不进键的后果是这条链最贵的
    # 那一种：改一个揭示时刻，命中缓存的镜原样返回旧切片，而账上写着「重烧过」。
    # **只在真有这一栏时才加这一项**：无条件加会改掉所有既有镜的键（`None` 也是一个
    # 值），整片换一次缓存＝每一镜白烧一次 Remotion 帧——而它们一个字节都没变。
    # 加了之后「有 → 改了 → 删了」三种状态两两算出的键都不同（在、值不同、不在），
    # 所以这一处省下的只是无谓的坏键，不是判据。
    if shot.get("reveal") is not None:
        payload["reveal"] = shot.get("reveal")
    # R14 风格档（满幅 / 底色）改的是**像素**，所以必须进键——写法与上面 `reveal`
    # 一字不差：**只在真有这一栏时才加**。塞进 `SHOT_KNOBS` 或无条件加，都会让
    # `None` 也成为一个值，于是所有既有镜的键全部改变、整片白烧一次 Remotion 帧，
    # 而它们一个字节都没变。
    style = film_style_of(shot)
    if style:
        payload["film_style"] = style
    # 层E2（R19）：这一镜**覆盖**了字幕档时也要进键。整片那一档本来就在 `FILM_KNOBS`
    # 里（`subtitle_mode`），所以只有「申报过覆盖」才加——不加的话「同一段代码、整片
    # full 而这一镜 inline」会共用一份切片，字幕带本该消失的那一镜会拿到带字幕带的
    # 旧切片，而账面上一切正常（这正是「少算一个键就是拿旧片冒充新片」）。
    sub_override = str(shot.get("subtitle_mode") or "")
    if sub_override:
        payload["subtitle_mode"] = sub_override
    # 卡拍补长改的是帧数（像素与声轨都变了），只在真补了时进键。
    if shot.get("_beat_pad"):
        payload["beat_pad"] = round(float(shot["_beat_pad"]), 3)
    for k in ("caption_position", "caption_anim"):
        if shot.get(k):
            payload[k] = str(shot[k])
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 闸⑦ 整片层：风格配额与文案具体项（在烧像素之前判，一次都不花）
# ---------------------------------------------------------------------------

#: **画法通道** → 认的 import 包名。只有「这一镜真的画了东西」的四条算通道：
#: 手绘斜纹（roughjs）、真三维（three / R3F）、公式（katex）、几何图形（裸 SVG 与
#: `@remotion/shapes`，判据在 markup 上而不是 import 上，见 ``_VECTOR_MARKUP_RE``）。
#:
#: ``@dg/motion``/``@dg/polish``/``@dg/lib``/``@dg/themes`` 那一类**不再算通道**：
#: 它们是动效件与色板，不是画面主体。「左边一张卡片 + 右边一段文字」配齐了
#: ``@dg/lib/ClipCard`` 与 ``@dg/themes``，画面上仍然什么都没有——旧表把它们计入
#: variety，正是「配额绿了而画面还是 PPT」的那条缝。它们照样在白名单里可以 import，
#: 只是撑不起配额。
#:
#: **与技能正文「通道表」那张表逐行同源**（``tests/test_code_spec.py`` 第 ⑧ 节拿正文
#: 比对这些名字）。为什么判据是 import 而不是模型自报的一栏：自报会漂（写了
#: `style: rough` 而代码里一个 roughjs 都没有），而这条闸要拦的恰恰就是
#: 「说用了但其实没有」。
PICTORIAL_CHANNELS: dict[str, tuple[str, ...]] = {
    "rough": ("roughjs",),
    "three": ("three", "@react-three/fiber", "@remotion/three"),
    "katex": ("katex",),
    "image": ("staticFile",),  # R2: 图像通道——本镜声明了 assets 且 staticFile 引用
}

#: 归类顺序：专门通道优先，配额才不会被蒙成两种通道各半。
CHANNEL_ORDER = ("rough", "three", "katex", "image")

#: 第四条通道：代码里真在画几何图形 —— 技能正文那句「（或裸 SVG）」的机器形态。
#: 判据**是写了画形状的东西**，不是 import：`<svg>`/`<path>` 之外的 `<div>` 加
#: `background` 只是色块，而色块正是「卡片+文字」的写法，所以它不算这一条。
_VECTOR_MARKUP_RE = re.compile(
    r"<\s*(svg|path|rect|circle|ellipse|line|polyline|polygon|g)\b"
    r"|\b(toPaths|toCurves|Path2D|linePath|polylinePath|shapeArc|shapeRect|"
    r"shapeCircle|shapePolygon)\(")

#: 四条通道都不命中的那一类：只有文字与色块。技能正文里的「纯文字/纯色块的那一类镜头」。
TEXT_CHANNEL = "text"

#: 配额表：镜数 → 至少几种**画法通道**。四条通道全用得上才算多风格：
#: 6 镜以上三种、10 镜以上四种（10 镜是常见片长，此时「三种通道」意味着有一条
#: 通道被三镜共用，仍可能全片一个母题——那由闸⑨ 在结构上拦，不归这一条）。
CHANNEL_QUOTA: tuple[tuple[int, int], ...] = ((6, 3), (10, 4))

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


#: 带绑定名的 import 子句：`import X from "s"` / `import { a, b as c } from "s"` /
#: `import * as ns from "s"`。只认静态写法（闸① 本就禁动态 import）。
_IMPORT_CLAUSE_RE = re.compile(
    r"""import\s+(?:(?P<clause>[^'"]+?)\s+from\s+)?['"](?P<src>[^'"]+)['"]""")

#: 一个绑定名短到什么程度就不认（`as e`、`t` 这种单字母别名在代码体里必然到处撞）。
_MIN_BINDING_LEN = 2


def code_imports(code: str) -> list[str]:
    """一段镜头代码里出现过的包名（按首次出现去重）。"""
    out: list[str] = []
    for name in _IMPORT_RE.findall(code or ""):
        if name not in out:
            out.append(name)
    return out


def imported_bindings(code: str) -> dict[str, list[str]]:
    """包名 → 这一句 import 在本文件里绑定的局部名（默认名、花括号名、命名空间名）。

    副作用 import（`import "katex/dist/katex.min.css"`）没有局部名，返回空列表。
    """
    out: dict[str, list[str]] = {}
    for m in _IMPORT_CLAUSE_RE.finditer(code or ""):
        clause, src = m.group("clause"), m.group("src")
        names: list[str] = []
        if clause:
            if "{" in clause:
                for part in clause[clause.index("{") + 1:clause.rindex("}")].split(","):
                    part = part.strip()
                    if not part:
                        continue
                    names.append(part.split(" as ")[-1].strip() if " as " in part else part)
                head = clause[:clause.index("{")].strip().rstrip(",").strip()
                if head.startswith("*"):
                    names.append(head.split(" as ")[-1].strip())
                elif head:
                    names.append(head.split(" as ")[-1].strip())
            else:
                names = [p.strip().split(" as ")[-1].strip()
                         for p in clause.replace("* as", "as").split(",") if p.strip()]
        out.setdefault(src, []).extend(n for n in names if n)
    return out


def _uses_binding(code: str, names: list[str]) -> bool:
    """import 之后的代码体里有没有真的用到这批局部名之一。"""
    body = _IMPORT_CLAUSE_RE.sub("", code or "")
    return any(len(n) >= _MIN_BINDING_LEN
               and re.search(rf"\b{re.escape(n)}\b", body) for n in names)


def shot_used_channels(code: str) -> set[str]:
    """这一镜**真的用了**哪些画法通道（import 了还得在代码体里用得上）。

    「写了 `import { Canvas } from "@react-three/fiber"` 而 JSX 里一个 `<Canvas>`
    都没有」是凑配额最省事的写法：只 import 一行，画面一片色块。旧判据只看包名，
    于是这一行就能把 three 记进 variety。
    """
    bindings = imported_bindings(code)
    body = code or ""
    found: set[str] = set()
    for name in CHANNEL_ORDER:
        for pkg in PICTORIAL_CHANNELS[name]:
            got = bindings.get(pkg)
            if got is None:
                if pkg in set(code_imports(body)):
                    found.add(name)      # 认不出绑定名的写法（`require`/动态）退回旧判据
                continue
            if not got or _uses_binding(body, got):
                found.add(name)
    if _VECTOR_MARKUP_RE.search(body):
        found.add("vector")
    return found


def shot_style_channel(code: str) -> str:
    """一镜归一条主画法通道：`rough` / `three` / `katex` / `vector` / `text`。"""
    all_ch = shot_used_channels(code)
    for name in CHANNEL_ORDER:
        if name in all_ch:
            return name
    return "vector" if "vector" in all_ch else TEXT_CHANNEL


def shot_all_channels(code: str) -> set[str]:
    """一镜用到的**所有**画法通道（不按优先级截断）。

    与 :func:`shot_style_channel` 的区别：那个按 ``CHANNEL_ORDER`` 只返回第一条
    匹配（主通道）。配额统计要的是「这一镜真用了哪几种」——一条 three 场景配一圈
    裸 SVG 引线，两种通道都算，``rough+vector`` 互融的镜正是技能正文要的那种。
    """
    return shot_used_channels(code)


def has_concrete_item(text: str) -> bool:
    """这一镜的文案里有没有一个**词法可见**的具体项。"""
    return any(r.search(text or "") for r in _CONCRETE_RES)


def style_facts(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """整片的风格与文案账（确认卡、出片回执、闸⑦⑨⑩ 三处读同一份，不各算一遍）。"""
    ids = [str(s.get("id") or f"s{i + 1:02d}") for i, s in enumerate(shots)]
    codes = [str(s.get("code") or "") for s in shots]
    channels = {sid: shot_style_channel(code) for sid, code in zip(ids, codes)}
    all_channels = {sid: shot_all_channels(code) for sid, code in zip(ids, codes)}
    variety: set[str] = set()
    for chs in all_channels.values():
        variety |= chs
    middle = list(channels.items())[1:-1] if len(shots) >= 3 else []
    same_groups = [[ids[i] for i in g] for g in structure_groups(codes)]
    contrast = []
    for sid, code in zip(ids, codes):
        c = shot_colors(code)
        if not c["surfaces"] or not c["inks"]:
            continue
        worst = min(max(contrast_ratio(ink, bg) for bg in c["surfaces"])
                    for ink in c["inks"])
        contrast.append({"id": sid, "ratio": round(worst, 2),
                         "inks": c["inks"], "surfaces": c["surfaces"]})
    return {
        "channels": channels,
        "all_channels": all_channels,
        "variety": sorted(variety),
        # 闸⑮ 的账：这一镜选的那张卡属于哪个族（`card` 空着的镜记空串，不当成「text 族」——
        # 「没选卡」与「选了某族」在账上是两件事）。
        "card_families": {sid: card_family(str(s.get("card") or ""))
                          for sid, s in zip(ids, shots)},
        # 闸⑯ 的账：逐镜把**申报的**通道与**代码里认得的**通道摆在一起。两栏都在这里出现，
        # 是为了让「申报 rough 而代码里是 vector」这种镜在账面上自己现形，而不是只留一句打回。
        "subject_channels": {
            sid: {"declared": str(s.get("subject_channel") or ""),
                  "detected": sorted(shot_all_channels(str(s.get("code") or "")))}
            for sid, s in zip(ids, shots)},
        "text_only": [sid for sid, c in channels.items() if c == TEXT_CHANNEL],
        "text_only_middle": [sid for sid, c in middle if c == TEXT_CHANNEL],
        # 闸⑲ 的三栏账（R19）：逐镜绑的色板名（认不出的记 None）、满幅镜、质感种数。
        # 色板读的是**代码里那句绑定**（`shot_colors`），不是模型自报的一栏——自报会漂，
        # 而这一道要拦的恰恰是「说换色板了但其实没有」。
        "palettes": {sid: shot_colors(code)["palette"] for sid, code in zip(ids, codes)},
        "bleed_shots": [sid for sid, s in zip(ids, shots) if s.get("bleed") is True],
        # 质感只数**申报过的**值；有镜没申报时补一个空串占一格——空串表示「跟整片默认」，
        # 它与任何具名档都不是同一个值，所以「一档都没申报」在这里读成 1 种（不是 0 种）。
        "textures": sorted({str(s.get("texture") or "").strip()
                            for s in shots if str(s.get("texture") or "").strip()}
                           | ({""} if any(not str(s.get("texture") or "").strip()
                                          for s in shots) else set())),
        # 闸⑳ 的账（R19）：哪几镜走了原创哨兵。计划卡与出片回执读这一栏——
        # 「1 镜原创」是**绿**（见 check_original_share），但用户仍该看得见是哪一镜。
        "original_shots": original_shot_ids(shots),
        "hollow": [str(s.get("id") or f"i{i + 1}") for i, s in enumerate(shots)
                   if not has_concrete_item(str(s.get("text") or ""))],
        # 闸⑨：≥2 镜的同构组（≥3 镜那一档在 validate 里已经打回了）
        "same_shape_groups": same_groups,
        # 闸⑩：判得到的那些镜，各自「在所有底色上最好的一档」比值
        "contrast": contrast,
        "contrast_unjudged": len(shots) - len(contrast),
    }


def check_stage_leg_present(shots: list[dict[str, Any]], *,
                            has_stage_leg: bool) -> tuple[list[str], list[str]]:
    """闸⑱：声明了 `assets` 就必须真有**投递腿**（渲染客户端暴露 `stage_asset`）。

    为什么把这条前移到这里：`code_render._stage_shot_assets` 里那条 ``MediaError``
    本身写得好，但它是**运行期**才响的——等到烧到那一镜才炸，前面几镜白烧，而失败
    消息还裹在渲染报错里。判据要的是**一帧都没烧之前**把话说清楚：要么补投递腿
    （R1 收的口），要么去掉 `assets` 声明。这样「缺腿」从运行期事故变成开始前的可报告状态。

    为什么不在 shots 里逐镜报：这是**整片级**能力缺失，不是某一镜写错了——报几十条
    同义错误只会淹掉真正要改的那一处，所以合并成一条并带上镜数。
    """
    if has_stage_leg:
        return [], []
    n = sum(1 for s in shots if s.get("assets"))
    if not n:
        return [], []
    return ([f"投递腿缺失：{n} 个镜头声明了 assets，但渲染客户端没有 stage_asset 口——"
             "素材字节投不进渲染容器，烧到那一镜必炸。先补投递腿，或去掉 assets 声明。"], [])


def check_style_quota(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸⑦：整片层的风格配额与文案具体项。→ (打回清单, 提醒清单)。

    三条硬判据全部读**代码字节**，都在烧像素之前判（这一层红一次 = 一次分镜重做，
    不是几十分钟白烧）：

    1. 通道种数不够（≥6 镜要 3 种、≥10 镜要 4 种，见 ``CHANNEL_QUOTA``）。这条同时
       拦住正文点名的那种写法——全片只有裸 SVG/图示一种通道，就是「卡片+文字」换了个
       马甲：variety=1 < 3。计入的只有四条**画法**通道（rough/three/katex/vector），
       ``@dg/motion`` 那一类动效件不算——它们不画画面主体。
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
            f"至少要 {want} 种画法通道。四条真通道按题材选：手绘斜纹用 `roughjs`"
            "（`rough.generator(...)` 画出的形要有 seed），立体形体用 `three` + "
            "`@react-three/fiber` + `@remotion/three`，式子用 `katex`（记得 "
            "`import \"katex/dist/katex.min.css\"`，不然渲出来是没上下标的纯文本），"
            "流程/曲线/几何用 `@remotion/shapes` 或裸 SVG。**只 import 不调用的那一行"
            "不算通道**（判据读的是代码体里真用得上的那个名字）。技能正文「画面丰富度"
            "合同」那一节逐条写着适合画什么")
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
# 闸⑲ 风格配额（R19）：色板 / 质感 / 满幅 / 底色——**逼丰富**的那一道
# ---------------------------------------------------------------------------
#
# 与 ⑦⑮⑯ 的分工：那三道判的是**反雷同**（通道别只一种、卡别同族扎堆、主体别写抽象词），
# 这一道判的是**逼丰富**。在它之前，色板 / 质感 / 满幅 / 底色这四条**没有任何闸在判**，
# 于是一条 20 镜片可以全用一套色板 + 一种质感 + 全常规版式而照样过闸——闸绿了，成片
# 却是「同一张 PPT 换了二十遍字」。
#
# 强度分两档，判据的性质不同（不是偷懒）：
#   · **结构判据（打回）**：色板种数、满幅镜数。这两件事在**代码字节**上就能数清
#     （`shot_colors()["palette"]` 已经能认出每镜绑的是哪套色板；`bleed` 是归一后的
#     布尔栏），而且「一镜都没满幅」「全片一个调子」是**确定的**退化形态。
#   · **程度判据（只提醒）**：质感种数、`bg` 申报率。质感有「跟整片默认」这一态
#     （没申报 ≠ 没质感），底色也不是每镜都该有——把这两条做成硬闸就会误伤
#     「整片一档柔光 + 几镜换底色」这种合法配法。与闸⑨「宁可漏报，不许误报」同一纪律。

#: 这一道从几镜起判。三镜片用三套色板是常态，不是纪律（与 ⑦ 的起步镜数同一个道理）。
VARIETY_MIN_SHOTS = 6
#: 色板种数下限 = ceil(n / 这个数)：镜数越多，越不该只用一个调子。
VARIETY_PALETTE_DIV = 4
#: 质感种数下限。
VARIETY_MIN_TEXTURES = 3
#: 满幅镜数下限 = ceil(n / 这个数)（20 镜 → 4 镜）。
VARIETY_BLEED_DIV = 5
#: `bg` 申报率下限（满幅镜另有一条更硬的：满幅**必须**给 `bg`，见判据 ④）。
VARIETY_BG_SHARE = 0.25


def check_style_variety(shots: list[dict[str, Any]]
                        ) -> tuple[list[str], list[str], list[str]]:
    """闸⑲ 风格配额：整片的**丰富度**够不够。→ (打回, 提醒, 未判)。

    四条判据，强度见上面那段：

    1. **色板种数** ≥ ``ceil(n / VARIETY_PALETTE_DIV)``（结构 → 打回）。读的是
       ``shot_colors(code)["palette"]``——代码里那句 ``const palette = palettes["…"]``。
       一镜都认不出色板时落**未判**（第三个出口），不装成通过：认不出与「只用了一套」
       在回执上是两句话。
    2. **满幅镜数** ≥ ``ceil(n / VARIETY_BLEED_DIV)``（结构 → 打回）。不写 `bleed` 时
       每一镜都被裁进同一条居中的画面带里，这正是成片「像 PPT」的结构原因（R14 把
       `bleed` 这条路开出来了，这一条是**逼它真的被用上**）。
    3. **质感种数** ≥ ``VARIETY_MIN_TEXTURES``（程度 → 只提醒）。质感有「跟整片默认」
       这一态，只数**申报过的**值，并把「没申报的镜数」写进提醒里——宁可漏报。
    4. **`bg` 申报率** ≥ ``VARIETY_BG_SHARE``，且**满幅镜必须给 `bg`**（程度 → 只提醒）。
       满幅不给底色时深色板的正文会压在母版奶油底上，而闸④ 还拿 `bg` 当判空的基准色。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    n = len(shots)
    if n < VARIETY_MIN_SHOTS:
        return errors, warns, undetermined
    facts = style_facts(shots)
    ids = [str(s.get("id") or f"s{i + 1:02d}") for i, s in enumerate(shots)]

    # ① 色板种数
    bound = facts["palettes"]
    known = sorted({v for v in bound.values() if v})
    want_pal = -(-n // VARIETY_PALETTE_DIV)
    if not known:
        undetermined.append(
            "闸⑲ 风格配额·色板这一档本片**没判**：一镜的色板都没能从代码里静态认出来"
            "（写法不是 `const palette = palettes[\"…\"]`，或者容器里读不到 "
            "`palettes.json`）。未判不是通过。")
    elif len(known) < want_pal:
        every = sorted(palettes())
        errors.append(
            f"闸⑲ 风格配额：{n} 镜只绑了 {len(known)} 套色板（{'、'.join(known)}），"
            f"这一档至少要 {want_pal} 套。色板的唯一出处是 "
            f"`remotion/src/themes/palettes.json` 的八套色板"
            f"（{'、'.join(every) if every else 'vintage-kraft / deep-ocean / … 共八套'}）"
            "——**逐镜按题材选**，别让二十镜共用一个调子：全片一个色正是成片"
            "「像同一张 PPT 换字」的原因之一。写法照技能正文的模板："
            "`const palette = palettes[\"<色板名>\"];` 写在代码顶部")

    # ② 满幅镜数
    bleeds = facts["bleed_shots"]
    want_bleed = -(-n // VARIETY_BLEED_DIV)
    if len(bleeds) < want_bleed:
        errors.append(
            f"闸⑲ 风格配额：{n} 镜只有 {len(bleeds)} 镜满幅（`bleed: true`），"
            f"这一档至少要 {want_bleed} 镜。不写 `bleed` 时每一镜都被裁进同一条居中的"
            "画面带里（左右留页边距、上下留安全区）——二十镜二十个母题共用同一个框，"
            "这正是成片**像 PPT** 的结构原因。画整屏插画、满版描线、大场景、"
            "背景要铺满的镜头就写 `bleed: true`（满幅镜记得同时给 `bg`）")

    # ③ 质感种数（只提醒）
    tex = facts["textures"]
    if len(tex) < VARIETY_MIN_TEXTURES:
        undeclared = sum(1 for s in shots if not str(s.get("texture") or "").strip())
        warns.append(
            f"质感提醒：{n} 镜只用了 {len(tex)} 种画面质感"
            f"（{'、'.join(sorted(tex)) or 'none'}），低于 {VARIETY_MIN_TEXTURES} 种这条线。"
            + (f"其中 {undeclared} 镜没写 `texture`（跟整片默认）。" if undeclared else "")
            + "质感是**打磨层**不是构图层（滤镜跑在拼接之前的那一趟逐镜切片上），"
              "换一档不重烧任何一镜的帧，所以「第 3 镜老电视、其余柔光」这种配法是免费的")

    # ④ 底色申报（只提醒；满幅那一条更硬）
    with_bg = [sid for sid, s in zip(ids, shots) if str(s.get("bg") or "").strip()]
    bleed_no_bg = [sid for sid, s in zip(ids, shots)
                   if s.get("bleed") is True and not str(s.get("bg") or "").strip()]
    if bleed_no_bg or len(with_bg) < n * VARIETY_BG_SHARE:
        warns.append(
            f"底色提醒：{len(with_bg)}/{n} 镜申报了 `bg`"
            + (f"，其中 {len(bleed_no_bg)} 个满幅镜没给（{'、'.join(bleed_no_bg)}）"
               if bleed_no_bg else "")
            + "——满幅镜**必须**给 `bg`：不给的话深色板的正文会压在母版的奶油底上"
              "（页边一圈奶油、中间一块深色），而闸④ 还拿 `bg` 当「这一镜画了东西没有」"
              "的基准色，读不到基准色它在这一镜上拒绝判空（未判不是通过）")
    return errors, warns, undetermined


# ---------------------------------------------------------------------------
# 闸⑳ 原创哨兵（R19）：`derived_from: ["<original>"]` 与它的配额
# ---------------------------------------------------------------------------
#
# 这一道拆的是 M1 那道结构性堵死（见 §B1）：`derived_from` **必填**且要求「代码必须
# 像声明的 demo」（`DERIVED_MIN_SIM` 那一档的负样本通过率只有 1.690%），于是**没有
# 「原创」这条路**——模型想画一个卡库里没有的东西，只能硬套一份 demo 的骨架，
# 而 demo 骨架是 video-shotcraft 自家的产品宣传片语汇库（M2）。两条合起来就是
# 「科普片画面上全是侧栏 / 看板 / Changelog / carousel」的机械原因。
#
# 为什么**不能简单降 DERIVED_MIN_SIM**：负样本（两份不同 demo）的 p98 = 0.605、
# max = 0.736——降到 0.61 以下会让「两份不同 demo」也通过（假阳性），那条闸就不再
# 拦「声明造假」了。所以正确做法是**加哨兵**：显式声明「这一镜不派生自任何 demo」，
# 闸跳过骨架比对，但**原创不等于免检**——lint（闸①）与闸④⑧⑩ 照样判它。
#
# 为什么配额 ≤20%：卡库是这条链的**风格上限**（M1+M2 的结论），原创是给「卡库表达
# 不了的那几镜」开的窄口子。不配配额，「每镜都挂哨兵」只差一步，整片立刻漂回
# 手搓——而那正是取材轮 + 124 张卡想消灭的东西。

#: 原创哨兵：写在 `derived_from` 里的这一个值表示「这一镜不从任何 demo 派生」。
ORIGINAL_SENTINEL = "<original>"
#: 这一道从几镜起判（与 ⑦ 同一档：镜数太少时「几镜原创」没有统计意义）。
ORIGINAL_MIN_SHOTS = 6
#: 原创镜的占比上限。
ORIGINAL_MAX_SHARE = 0.20


def original_shot_ids(shots: list[dict[str, Any]]) -> list[str]:
    """走原创哨兵的那几镜（``style_facts["original_shots"]`` 与配额闸共用这一份判据）。"""
    out: list[str] = []
    for i, s in enumerate(shots):
        if [str(d) for d in (s.get("derived_from") or [])] == [ORIGINAL_SENTINEL]:
            out.append(str(s.get("id") or f"s{i + 1:02d}"))
    return out


def check_original_share(shots: list[dict[str, Any]]
                         ) -> tuple[list[str], list[str]]:
    """闸⑳ 原创配额：走原创哨兵的镜不得超过整片的 ``ORIGINAL_MAX_SHARE``。

    → (打回清单, 提醒清单)。提醒恒空：**绿就是绿**——1 镜原创的片子不该在回执上
    多出一条「注意有几镜是原创」，那会把「通过了」读成「有问题但算了」。
    哪几镜走了原创由 ``style_facts["original_shots"]`` 如实报账（计划卡与回执都读它）。
    """
    errors: list[str] = []
    n = len(shots)
    if n < ORIGINAL_MIN_SHOTS:
        return errors, []
    ids = original_shot_ids(shots)
    quota = max(1, int(n * ORIGINAL_MAX_SHARE))
    if len(ids) > quota:
        errors.append(
            f"闸⑳ 原创配额：{n} 镜里有 {len(ids)} 镜声明了原创"
            f"（{ORIGINAL_SENTINEL}：{'、'.join(ids)}），上限是 {quota} 镜"
            f"（整片的 {ORIGINAL_MAX_SHARE:.0%}）——卡库是这条链的风格上限，"
            "原创哨兵是给「卡库表达不了的那几镜」开的窄口子，不是绕开取材的常规路。"
            "占多数的镜仍须声明它派生自哪份 demo（照 shots/INDEX.md 那一栏抄）")
    return errors, []


# ---------------------------------------------------------------------------
# 闸⑮ 版式族配额 / 闸⑯ 画面主体具名（R5-T15；与 ⑦ 一样判在烧像素之前）
# ---------------------------------------------------------------------------

#: 闸⑮：同一**族**最多几镜。族 = 卡库里那个类别目录名（`card` 写成 `<类别>/<卡名>`）。
LAYOUT_FAMILY_MAX_SHOTS = 2

#: ⑯ 的通道白名单。**与 :func:`shot_style_channel` 的返回值是同一套词**（不多不少）——
#: 两处对得上，才谈得上「申报的通道」与「代码里认得的通道」逐字对账。`text` 不在里面：
#: 那一档是 ⑦ 允许的纯文字/纯色块镜，⑯ 对它另有规矩（见 :func:`check_subject_named`）。
SUBJECT_CHANNELS: tuple[str, ...] = ("rough", "three", "katex", "vector", "image")

#: `subject` 的长度上下界（字符）。太短是「东西」「这个」这类指代，太长是整句话而不是
#: 名词短语——⑯ 要的是**能被认出的那个东西**的名字，不是论题。
SUBJECT_MIN_CHARS = 2
SUBJECT_MAX_CHARS = 24

#: 抽象词拒绝名单：⑯ 要的是画面上能被认出的**东西**，而这些词指的是观点而不是物件。
#: 名单**只认名单**，不做词法推断：中文里「理念」「趋势」是抽象词，「温度」「等高线」
#: 一样是抽象名词却是真东西——靠后缀（性/感/化）或词性去猜必然误伤后者。
#: 名单是**会长大的**（与 ⑳ 的坏形态清单同规）：真机上模型又用哪个抽象词蒙混，就往这里
#: 补一行，而不是把判据改成「更聪明的那个」。
ABSTRACT_SUBJECTS: frozenset[str] = frozenset({
    "理念", "概念", "观念", "意义", "价值", "影响", "趋势", "精神", "文化", "文明",
    "思想", "内涵", "本质", "特点", "特色", "作用", "功能", "优势", "劣势", "未来",
    "发展", "变化", "变革", "效率", "情感", "情绪", "氛围", "风格", "设计", "艺术",
    "科技", "技术", "创新", "智慧", "力量", "主题", "故事", "内容", "信息", "知识",
    "方法", "方式", "过程", "系统", "模式", "结构", "关系", "因素", "现象", "状态",
    "东西", "事物", "问题", "事情", "情况", "方面", "层面", "维度", "视角", "角度",
})


def card_family(card: str) -> str:
    """卡 id → 版式**族**：``data/chart-live-moves`` → ``data``（卡库的类别目录名）。"""
    return str(card or "").split("/", 1)[0].strip()


def check_layout_family(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸⑮：整片层的版式族配额。→ (打回清单, 提醒清单)。

    两条判据，读的都是**已经过白名单的** ``card``（见 :func:`_norm_cite`）：

    1. `card` **取值**（卡名）种类 ≥ ``ceil(镜数 / 4)``——整片至少要有那么多个不同的版式；
    2. 同一**族**（卡库里的类别，`<类别>/<卡名>` 的前半截）最多 ``LAYOUT_FAMILY_MAX_SHOTS`` 镜。

    第 2 条蕴含第 1 条（同族 ≤2 镜 ⇒ n 镜至少要 n/2 个族 ⇒ n/2 ≥ ceil(n/4)）。两条都判不是
    啰嗦：合同把两条都冻结了，而第 1 条是「族」这个口径万一被放松时还站着的那道地板。

    **为什么族取卡库目录名，而不是让模型自报一栏 `layout`**：自报的自由字符串没有任何
    白名单，写什么都能凑够「取值种类」——而这条闸要拦的恰恰是「每镜换一个名字、画面上
    还是同一张卡」。卡库的类别是**已经存在、已经被 `_known_card_ids()` 当白名单使的**
    东西，不新造字段：一份片子里 `data` 族摆三镜，观众看到的就是同一套版式在重复。
    §9-Q84 记了这条口径（同时也记了合同原话里那个不存在的 `layout` 栏）。

    纯文字/纯色块族受 ⑦ 的 1/3 约束（:func:`check_style_quota` 第 3 条），不在这里重算。
    镜数不到 ``MIN_SHOTS_FOR_QUOTA`` 不判——三镜片谈「族配额」连分母都不成立。
    `card` 为空的那几镜不在这里喊第二遍（``_norm_cite`` 已经逐镜报过），配额账只把
    **选定了卡**的镜算进分母与族账。
    """
    errors: list[str] = []
    n = len(shots)
    if n < MIN_SHOTS_FOR_QUOTA:
        return errors, []
    pairs = [(str(s.get("id") or f"s{i + 1:02d}"), str(s.get("card") or "").strip())
             for i, s in enumerate(shots)]
    pairs = [p for p in pairs if p[1]]
    if not pairs:
        return errors, []       # 一镜都没选卡：那是出处/卡名那一栏的错，不在这儿喊第二遍
    kinds: dict[str, list[str]] = {}
    fams: dict[str, list[str]] = {}
    for sid, card in pairs:
        kinds.setdefault(card, []).append(sid)
        fams.setdefault(card_family(card), []).append(sid)

    want = -(-n // 4)                                     # ceil(n/4)，整数运算不引 float
    if len(kinds) < want:
        errors.append(
            f"闸⑮ 版式族配额：{n} 镜只用了 {len(kinds)} 张不同的卡（"
            + "、".join(f"{c}×{len(v)}" for c, v in kinds.items())
            + f"），这一档至少要 {want} 张。同一张卡摆好几镜，画面上就是同一个母题换了文案"
            "——把重复的那几镜换成别的卡（卡库按类别分好了，见 shots/INDEX.md）")
    over = {f: v for f, v in fams.items() if len(v) > LAYOUT_FAMILY_MAX_SHOTS}
    if over:
        errors.append(
            f"闸⑮ 版式族配额：{n} 镜里 "
            + "；".join(f"{f} 族占了 {len(v)} 镜（{'、'.join(v)}）" for f, v in over.items())
            + f"——同一族最多 {LAYOUT_FAMILY_MAX_SHOTS} 镜。族是卡库的类别目录名，"
            "同一族连着摆好几镜，观众看到的是同一套版式在重复。当前族分布："
            + "、".join(f"{f}×{len(v)}" for f, v in fams.items()))
    return errors, []


def check_subject_named(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸⑯：每镜 `subject` 具名、`subject_channel` 有证据。→ (打回清单, 提醒清单)。

    ⑯ 判的是「这一镜画面上**能被认出的那个东西**有没有被说出来」——机器认证词、人认证物，
    而判据只取机器判得动的那一半：

    - ``subject``：**每镜必填**。非空、``SUBJECT_MIN_CHARS``~``SUBJECT_MAX_CHARS`` 字、
      不在 :data:`ABSTRACT_SUBJECTS` 里。抽象词机器认不出、画面上也认不出，等于没写。
    - ``subject_channel``：**有图形主体的镜必填**（取值 = :data:`SUBJECT_CHANNELS` 五选一），
      且必须是**这一镜代码里真用上的**通道之一（:func:`shot_all_channels`）。自报的通道
      必须有证据——「写着 rough 而代码里一个 roughjs 都没有」正是 ⑦ 要拦的那种形状，
      ⑯ 不再开一个能自报的后门。
    - 纯文字/纯色块镜（⑦ 允许的那 ≤1/3）：**不许**填 ``subject_channel``——它代码里一条
      画法通道都没有，填了就是自报无证据。这一档的 ``subject`` 照样要写（字也可以是主体，
      如「「毕昇」两个字」；连一个具体名词都写不出来，说明这一镜本来就该补画面）。

    ⑯ 判不了「画出来的那个东西像不像 ``subject`` 说的那个」——那是 ⑲-b（盲描述）的活。
    它判的是**有没有具名、名字有没有证据**，这两件事机器判得动，也正是「卡片+文字」那一类
    镜最缺的一格（合同 §5.3 把 ``subject``/``subject_channel`` 挂在 ``card`` 里描述，
    实现上它们是镜头上的两个平级键：``card`` 在本仓已是**字符串**卡 id，见 §9-Q85）。
    """
    errors: list[str] = []
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        head = f"闸⑯ 主体具名：{sid}："      # 与 ⑦⑨⑩ 同形：闸号 + 镜号，打回清单能直接点名
        subject = str(s.get("subject") or "").strip()
        if not subject:
            _err(errors, f"{head}subject 为空——每一镜要说清画面上那个「能被认出的东西」"
                         f"是什么（名词短语，如「活字盘」「三条等高线」「一张 1900 年的地图」），"
                         f"不是主题词。⑯ 判的就是这一格有没有填")
        elif subject in ABSTRACT_SUBJECTS:
            _err(errors, f"{head}subject={subject!r} 是抽象词（在拒绝名单里）——⑯ 要的是"
                         f"画面上能被认出的**东西**，抽象词既画不出来也认不出来；"
                         f"改写成具体器物的名词短语（如「{_SUBJECT_HINT}」）")
        elif not SUBJECT_MIN_CHARS <= len(subject) <= SUBJECT_MAX_CHARS:
            _err(errors, f"{head}subject={subject!r} 长度 {len(subject)} 字，"
                         f"要 {SUBJECT_MIN_CHARS}~{SUBJECT_MAX_CHARS} 字——太短是指代"
                         f"（「东西」「这个」），太长是整句话而不是主体名")
        # 通道这一栏两向都判：有图形主体的镜必填且要有证据，纯文字镜填了就是自报。
        declared = str(s.get("subject_channel") or "").strip().lower()
        detected = sorted(shot_all_channels(str(s.get("code") or "")))
        if not detected:
            if declared:
                _err(errors, f"{head}这一镜的代码里只有文字与色块（一条画法通道都没用上），"
                             f"却申报了 subject_channel={declared}——纯文字镜不填这一栏，"
                             f"填了就是自报无证据（⑦ 允许的这类镜有 1/3 的上限）")
            continue
        if not declared:
            _err(errors, f"{head}这一镜画了图形主体（代码里认得的通道：{'、'.join(detected)}），"
                         f"但没填 subject_channel——五选一：{'、'.join(SUBJECT_CHANNELS)}，"
                         f"填**主体**是用哪条通道画的")
            continue
        if declared not in SUBJECT_CHANNELS:
            _err(errors, f"{head}subject_channel={declared!r} 不在白名单"
                         f"（{'、'.join(SUBJECT_CHANNELS)}）")
        elif declared not in detected:
            _err(errors, f"{head}申报 subject_channel={declared}，而这一镜代码里用的是"
                         f"{'、'.join(detected)}——自报的通道必须有证据。要么把这一镜的主体"
                         f"改成用 {declared} 画，要么按代码里真用的那条申报")
    return errors, []


# ---------------------------------------------------------------------------
# 闸⑲-a 画面主体在场（T30）：几何那一半，不需要任何模型
# ---------------------------------------------------------------------------

#: 主体件（画得出**图形**的那几类元素：SVG 形状 / 位图 / 三维画布）的并集外接盒
#: 至少要占画面带的这一比例。数字的来源与 ⑳(a) 同一处：2026-10-11 那批片子上
#: 「画了个东西但画面仍旧是卡片+文字」的镜，主体件占比实测都在 0.1 以下。
SUBJECT_MIN_BBOX = 0.18

#: 文字占带的这一比例以上算「文字主导画面」。与 :data:`SUBJECT_MIN_BBOX` **联合**判
#: 才是「卡片+文字」那条退化形态（⑳(a)）：单独一条都拦不住合法片子——满屏大字配一只
#: 小图标的封面镜、以及只有一行字的过渡镜，各自只踩其中一条。
TEXT_DOMINANT_SHARE = 0.45


def check_subject_present(shots: list[dict[str, Any]], *,
                          scanned: Mapping[str, Any] | None = None
                          ) -> tuple[list[str], list[str], list[str]]:
    """闸⑲-a：这一镜画面上**真有一个铺得开的主体**吗。→ (打回, 提醒, 未判)。

    这一道判的是**几何**，读的是母版在探针帧上量的两个原始读数（闸⑧ 那条回读通道
    顺路带回来的）：``subject_share``（主体件并集外接盒 ∩ 画面带 ÷ 带面积）与
    ``text_share``（文字盒同理）。判据在**产物上**——代码里写了 `rough.generator(...)`
    与画面上真有一片占了 18% 的图形，是两件事（画到带外、尺寸算成 0、被裁光都算）。

    **为什么元素那一半不在这里重数一遍**：`shot_used_channels` 已经要求「import 了还得
    真用得上」，⑦ 与 ⑯ 因此早把「代码里一个 roughjs 都没有」这一档拦在烧像素之前了；
    在这里再数一遍等于把一个已有判据抄成第二个（本仓踩过三次的清单漂移）。
    ⑲-a 只补**没人管的那一格**：屏幕上那个东西到底铺开了多大一片。

    两条硬判据（都是**联合**判，单条都会误伤合法片子）：

    1. 主体件占比 < :data:`SUBJECT_MIN_BBOX` **且** 文字占比 > :data:`TEXT_DOMINANT_SHARE`
       → 「卡片+文字」。这是 ⑳(a) 那条退化形态，判据只此一处（⑳ 的账读它的结果）。
    2. 申报了 `subject_channel`（即模型说这镜画了图形主体）而屏幕上 ``subjects == 0``
       → 主体是**空壳**：代码写了、画面上没有。⑯ 查代码、⑲-b 查认不认得出，这一格
       是它们之间的缝。

    只踩第一条一半的（主体小、文字也不多）落**提醒**：那是「构图小气」，归 ④ 的
    `fill_span` 与技能正文的目标值，不由这里打回。

    读数取不到（母版没回读、或这一镜没扫）→ **未判**（第三个出口）。这条尤其重要：
    把「没量到」写成「量到了、很小」会让这条闸恰好在它该响的时候闭嘴。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    readings = scanned or {}
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        head = f"闸⑲-a 主体在场：{sid}："
        got = readings.get(sid) if isinstance(readings, Mapping) else None
        got = got if isinstance(got, Mapping) else None
        declared = str(s.get("subject_channel") or "").strip().lower()
        if got is None or got.get("status") != "scanned":
            undetermined.append(
                f"{head}这一镜的版面没回读到（闸⑧ 那一趟没扫成），⑲-a 的几何读数"
                f"因此**没有量过**——未判不是通过")
            continue
        sc = got.get("scanned") if isinstance(got.get("scanned"), Mapping) else {}
        subj = _as_float(sc.get("subject_share"))
        text = _as_float(sc.get("text_share"))
        count = sc.get("subjects")
        if subj is None or text is None:
            undetermined.append(
                f"{head}母版这一趟没带 ⑲-a 的两个读数（镜像里的母版比这条链旧），"
                f"这一格**没量过**——未判不是通过")
            continue
        if isinstance(count, int) and count == 0 and declared:
            _err(errors, f"{head}这一镜申报了 subject_channel={declared}，而**画面上**"
                         f"一个主体件都没有（母版数出来的图元数=0）——代码写了不等于画出来了，"
                         f"主体是空壳。把它画到 `useBody()` 报回来的那条带子里、给它一个"
                         f"能读出的尺寸，或者这一镜本来就没画图形，就把 subject_channel 去掉")
            continue
        if subj < SUBJECT_MIN_BBOX and text > TEXT_DOMINANT_SHARE:
            _err(errors, f"{head}画面主体只占了带的 {subj * 100:.0f}%（线 "
                         f"{SUBJECT_MIN_BBOX * 100:.0f}%），文字却铺了 {text * 100:.0f}%"
                         f"（线 {TEXT_DOMINANT_SHARE * 100:.0f}%）——这就是「卡片+文字」："
                         f"图元数 {count}，但画面上真正占地方的是字。把主体放大到撑起画面"
                         f"（技能正文「画面丰富度合同」给的目标是短边的六成，"
                         f"那个数在 `code_render.FILL_TARGET` 一处），"
                         f"或者把这一镜的分工改成「画图为主、字为辅」")
            continue
        if subj < SUBJECT_MIN_BBOX:
            warns.append(
                f"{head}主体件只占了带的 {subj * 100:.0f}%（目标 "
                f"{SUBJECT_MIN_BBOX * 100:.0f}% 以上），文字 {text * 100:.0f}%——"
                f"还没到「卡片+文字」那一档，但主体偏小，看一眼构图")
    return errors, warns, undetermined


# ---------------------------------------------------------------------------
# 闸㉕ 徽章构图（R11-T53/㉕）：一枚徽章撑满画面 ≠ 一个场景
# ---------------------------------------------------------------------------

#: 徽章那一档的**大小**线：单件主体铺到带的这一比例以上，才谈得上「它就是这个画面」。
#: 与 ⑲-a 的 ``SUBJECT_MIN_BBOX``（0.18）**不是同一个数，也不同一处判**：0.18 是「主体
#: 太小」的下限（四条判据联合里的一条），这一条判的是**反面**——单个件大到铺满足以冒充
#: 场景。取 0.22 而不是 0.18：0.18~0.22 那一小段里的是「一镜就画一个居中图形」的正常
#: 封面镜（技能正文允许），把线压到 0.18 会把它一起打回。
BADGE_MIN_SHARE = 0.22

#: 「居中」的容差：主体中心离带中心超过带（宽/高）的这一比例 → 不算居中。
#: 该值由 2026-10-13 的两支读数定：真 · 居中徽章的中心偏差 <3%，手摆歪的构图 >10%；
#: 取 0.08 让「手工摆的偏心」落在这一档之外。
BADGE_CENTER_TOL = 0.08

#: 「近方形」的容差：盒的长边 ÷ 短边超过这一比例就不算近方形。
#: 1.35 ≈ 4:3 略宽——4:3 已是「一张图」的形状，正圆徽章与正方徽章的实测比值都 <1.1。
BADGE_ASPECT_TOL = 1.35

#: 徽章那条打回理由里给的改写方向（技能正文「先取 `asset_role=part` 拼场景」那条）。
_BADGE_HINT = "先把 `asset_role='part'` 的部件取几件拼成一个场景"


def check_badge_composition(shots: list[dict[str, Any]], *,
                            scanned: Mapping[str, Any] | None = None
                            ) -> tuple[list[str], list[str], list[str]]:
    """闸㉕ 徽章构图：这一镜画面上是不是**一枚居中、近方形的单件撑满了画面**。
    → (打回, 提醒, 未判)。

    **它要拦的形状。** 把一个 24 px 的线图标放大到铺满画面，屏幕上出现的是一枚徽章
    （或一块色斑），不是「一个场景」——这件事 ⑲-a 拦不住：⑲-a 判的是「主体**太小**」，
    而徽章恰恰是**太大**（``subject_share`` 一路到 0.9 也不触发它）。⑳(c) 也拦不住：
    它判的是「两镜画面上是同一张卡」，一镜的徽章跟谁都不像。所以这是两条既有闸之间的
    一格真空，本闸只补这一格。

    **为什么判据要四条联合（单件 + 够大 + 居中 + 近方形）。** 每一条单独都会误伤合法
    构图：单件（一个居中的人像插画）、够大（一条横贯画面的时间轴）、居中（三件里居中
    的那一件）、近方形（一张占半屏的方框流程图）——四条同时成立，才是「一枚徽章」：
    画面上只有**一个**东西、它**铺得开**、它**摆在正当中**、它的形状**不指向任何内容**
    （正圆/正方/近似方形，没有细长结构、没有拼装感）。少任何一条，画面里就有别的信息，
    归技能正文的构图目标管，不由这里打回。

    **不判「小徽章」那一档。** 一枚小图标摆在角上做装饰是正常写法（⑲-a 那条「主体小、
    文字也不多 → 提醒」已经覆盖了）。这条闸只管「大」。

    读数（母版在探针帧上量的，㉕ 与 ⑲-a/⑳(c) **同一趟**回读）：``subject_boxes``
    （逐件主体盒，带内整数像素）＋ ``band``（带的宽高）＋ ``subjects``/``subject_share``。
    母版只报**原始读数**，四条判据与三个阈值全部在**这一处**——同一趟回读在两处各写一个
    0.25，就是两把尺（⑲-a 那条规矩的同一句话）。

    三个出口：过 / 打回 / **未判**。读数整栏取不到（母版旧、没带 ``subject_boxes``）、
    带尺寸读到 0、``subjects``/``subject_share`` 缺一栏 → 未判，逐镜说清缺的是哪一栏。
    「没量到」不许写成「量到了、没问题」（D-未判不绿）。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    head = "闸㉕ 徽章构图："
    readings = scanned if isinstance(scanned, Mapping) else {}
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        got = readings.get(sid)
        got = got if isinstance(got, Mapping) else None
        if got is None or got.get("status") != "scanned":
            undetermined.append(
                f"{head}{sid} 的版面没回读到（闸⑧ 那一趟没扫成），这一镜的构图"
                f"**没量过**——未判不是通过")
            continue
        sc = got.get("scanned") if isinstance(got.get("scanned"), Mapping) else {}
        count = sc.get("subjects") if isinstance(sc, Mapping) else None
        share = _as_float(sc.get("subject_share")) if isinstance(sc, Mapping) else None
        band = sc.get("band") if isinstance(sc, Mapping) else None
        boxes = sc.get("subject_boxes") if isinstance(sc, Mapping) else None
        if count is None or share is None or not isinstance(band, Mapping):
            undetermined.append(
                f"{head}{sid} 母版这一趟没带 ㉕ 要的读数（镜像里的母版比这条链旧："
                f"缺 subjects/subject_share/band 之一），这一格**没量过**——未判不是通过")
            continue
        if not isinstance(boxes, (list, tuple)):
            undetermined.append(
                f"{head}{sid} 母版没带**逐件**主体盒（`subject_boxes` 是这一闸新加的"
                f"回读栏，镜像里的母版比这条链旧），这一镜的构图**没量过**"
                f"——未判不是通过")
            continue
        bw, bh = _as_float(band.get("w")), _as_float(band.get("h"))
        if not bw or not bh or bw <= 0 or bh <= 0:
            undetermined.append(
                f"{head}{sid} 画面带的宽高读到 {band.get('w')}×{band.get('h')}"
                f"（算不出「居不居中」），这一格**没量过**——未判不是通过")
            continue
        if not isinstance(count, int) or count != 1:
            # 多件（或 0 件）不是徽章的形状：0 件归 ⑲-a 的空壳那条，多件本身就是「拼起来
            # 的场景」。这里**不记未判**——这是量到了、且量清楚了，只是不是这一格的问题。
            continue
        if len(boxes) != 1:
            undetermined.append(
                f"{head}{sid} 个数读数是 1，逐件盒却带了 {len(boxes)} 件"
                f"（母版两栏对不上），这一镜的构图**没判完**——未判不是通过")
            continue
        b = boxes[0] if isinstance(boxes[0], Mapping) else None
        x, y = (_as_float(b.get("x")), _as_float(b.get("y"))) if b else (None, None)
        w, h = (_as_float(b.get("w")), _as_float(b.get("h"))) if b else (None, None)
        if None in (x, y, w, h) or not w or not h or w <= 0 or h <= 0:
            undetermined.append(
                f"{head}{sid} 那一件主体的盒读不齐（{boxes[0]!r}），这一镜的构图"
                f"**没判完**——未判不是通过")
            continue
        if share < BADGE_MIN_SHARE:
            # 单件但没铺开：这是「一镜一个居中小图形」的正常构图（⑲-a 的提醒那一档管它）。
            continue
        centred = (abs((x + w / 2) - bw / 2) <= BADGE_CENTER_TOL * bw
                   and abs((y + h / 2) - bh / 2) <= BADGE_CENTER_TOL * bh)
        aspect = max(w, h) / min(w, h)
        square = aspect <= BADGE_ASPECT_TOL
        if centred and square:
            _err(errors,
                 f"{head}{sid} 画面上是**一枚徽章**，不是一个场景：整镜只有 1 件主体，"
                 f"它铺了带的 {share * 100:.0f}%（线 {BADGE_MIN_SHARE * 100:.0f}%）、"
                 f"中心就在带正中（偏差 "
                 f"{abs((x + w / 2) - bw / 2) / bw * 100:.1f}% ≤ {BADGE_CENTER_TOL * 100:.0f}%）、"
                 f"形状近似方形（长边/短边 {aspect:.2f} ≤ {BADGE_ASPECT_TOL:g}，"
                 f"盒 {int(round(w))}×{int(round(h))} px）——"
                 f"把一件件素材放大铺满，屏幕上就是一枚徽章或一块色斑，"
                 f"观众看到的是「图标」而不是「这一镜在讲什么」。"
                 f"这一镜要真的像场景：{_BADGE_HINT}（按技能正文那句，"
                 f"取 4~6 件摆到不同位置、大小错开、彼此有前后或相邻关系），"
                 f"或者换一个版式族（主体 + 标注、主体 + 局部放大、时间轴）；"
                 f"真就只需要一个图形时，把它缩到带的三成以内、挪离正中，"
                 f"让画面里还有别的东西可看")
            continue
        # 单件、够大，只是没居中或不够方：不到徽章那一档，但记一句——这类镜离徽章很近。
        why = []
        if not centred:
            why.append(f"中心偏带正中 "
                       f"{abs((x + w / 2) - bw / 2) / bw * 100:.1f}%"
                       f"（容差 {BADGE_CENTER_TOL * 100:.0f}%）")
        if not square:
            why.append(f"长边/短边 {aspect:.2f}（容差 {BADGE_ASPECT_TOL:g}）")
        warns.append(
            f"{head}{sid} 整镜只有 1 件主体、铺了带的 {share * 100:.0f}%"
            f"（{'；'.join(why)}）——还没到「居中近方形徽章」那一档，但离它很近，"
            f"看一眼构图：给它配几件部件或一条标注，画面才有信息")
    return errors, warns, undetermined


def _as_float(value: Any) -> float | None:
    """读数只认真数：``None``/字符串/``bool`` 一律算「没量到」（``bool`` 是 ``int`` 子类，
    真值会悄悄变成 1.0——那正是「没量到被写成量到了」的一种）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: Any) -> int | None:
    """位掩码那条读数只认真整数（``bool`` 不算：``True`` 掩码毫无意义，写成
    「量到了 1」正是 D-未判不绿要防的那种）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_motion_anchor(raw: Any) -> tuple[str, str] | None:
    """一个锚串 → ``("fields"|"subtitle"|"beat", 参数)``；认不出返回 ``None``。

    三种写法各只认**一种**拼法，因为这条通道的两头是两台机器：

    * ``fields.<key>``    —— 参数是**这一镜自己声明的**字段名（⑥ 的口径，闸前对账）。
    * ``subtitle:<i>``    —— 参数是**本镜旁白的第 i 个词**（1 起）。合同里写的「字幕窗」
      落成词的首次可读帧：这一镜只有一行字幕，整行都在窗口里，那等于「怎么锚都算对」。
    * ``beat:<n>``        —— 参数是 BGM 的第 n 个节拍（1 起）。

    ``int(...)`` 那几个分支**只认十进制整数串**（含 ``+1`` 那种写法一律不认）：
    ``"subtitle:1.0"`` 与 ``"subtitle: 1"`` 是两种写法，放进白名单就等于同一件事有
    两个名字——而这条闸的回执要能被人拿着锚串逐字核对。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("fields."):
        key = text[len("fields."):]
        return ("fields", key) if FIELD_NAME_RE.match(key) else None
    for prefix, form in (("subtitle:", "subtitle"), ("beat:", "beat")):
        if text.startswith(prefix):
            tail = text[len(prefix):]
            if not tail.isdigit() or (len(tail) > 1 and tail.startswith("0")):
                return None                  # 前导零/符号/小数一律不认：一个名字一种写法
            idx = int(tail)
            return (form, str(idx)) if idx >= 1 else None
    return None


def check_motion_anchors(shots: list[dict[str, Any]]) -> list[str]:
    """㉑ **闸前**：申报了的每个动效事件都必须挂上能解析的锚。→ 打回清单。

    这一半**不用烧像素**，所以它排在三处调用点（`validate_code_spec`）上：
    「写了动效却没说为谁动」这一次请求根本不该发出去。

    判四件事：

    1. ``motion`` 每一条都得是对象、且有非空的 ``kind``（说不出是什么动效）；
    2. ``anchors`` 非空——**空数组就是打回**（§8.3 第 7 支反例的正面：动效有先后、
       但不为任何数据服务）；
    3. 每个锚能解析成 :data:`MOTION_ANCHOR_FORMS` 三种之一；
    4. ``fields.<key>`` 的 key 必须是**这一镜自己声明过的**字段（⑥ 的白名单）——
       锚到一个不存在的字段，在这条闸眼里与「没锚」是同一件事。

    ``subtitle:<i>`` / ``beat:<n>`` 只在这里做**形式**检查：词表的长度要等 TTS 实测
    （`narration_for`）才知道，节拍表今天压根没有——那两件事的可用性在闸后那一半
    （:func:`check_motion_windows`）才算得出来，这里越界也判不了，不假装判过。
    """
    errors: list[str] = []
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        head = f"闸㉑ 动效锚定：{sid}："
        declared = s.get("motion")
        if declared is None:
            continue                     # 一条都没申报：那一格由闸后按像素数出来的段判
        if not isinstance(declared, (list, tuple)):
            _err(errors, f"{head}motion 不是清单（实得 {type(declared).__name__}）——"
                         f"每一条要写 {{kind, anchors}}")
            continue
        names = {str(f.get("name")) for f in (s.get("fields") or [])
                 if isinstance(f, Mapping)}
        for j, ev in enumerate(declared, 1):
            if not isinstance(ev, Mapping):
                _err(errors, f"{head}第 {j} 条动效不是对象")
                continue
            kind = str(ev.get("kind") or "").strip()
            if not kind:
                _err(errors, f"{head}第 {j} 条动效没写 kind——说不出是什么动效，"
                             f"就没法说它服务谁")
            raw_anchors = ev.get("anchors")
            if isinstance(raw_anchors, str) or not isinstance(raw_anchors, (list, tuple)):
                _err(errors, f"{head}第 {j} 条（{kind or '?'}）的 anchors 不是清单")
                continue
            if not raw_anchors:
                _err(errors, f"{head}第 {j} 条（{kind or '?'}）**没有锚**——"
                             f"这一条动效在说它不为任何数据服务，而那是 §8.3 里被驳回过的"
                             f"形态。挂上它为谁动：`fields.<本镜字段名>`（这一镜声明过的）、"
                             f"`subtitle:<本镜第几个词>`、或 `beat:<第几个节拍>`")
                continue
            if len(raw_anchors) > MOTION_MAX_ANCHORS:
                _err(errors, f"{head}第 {j} 条（{kind or '?'}）挂了 {len(raw_anchors)} 个锚，"
                             f"上限 {MOTION_MAX_ANCHORS}——锚是「**每一个**都必须在窗口内」"
                             f"才算数，挂满了等于没锚")
            for raw in raw_anchors:
                got = parse_motion_anchor(raw)
                if got is None:
                    _err(errors, f"{head}第 {j} 条（{kind or '?'}）的锚 {raw!r} 认不出——"
                                 f"只认三种写法：`fields.<字段名>` / `subtitle:<第几个词>`"
                                 f" / `beat:<第几个节拍>`（都从 1 起，不写零、不写小数）")
                    continue
                if got[0] == "fields" and got[1] not in names:
                    _err(errors, f"{head}第 {j} 条（{kind or '?'}）锚在 fields.{got[1]} 上，"
                                 f"而这一镜没声明过这个字段"
                                 + (f"（声明的是：{'、'.join(sorted(names))}）" if names
                                    else "（这一镜一个字段都没声明）")
                                 + "——锚到一个不存在的字段，与没锚是同一件事")
    return errors


# ---------------------------------------------------------------------------
# 闸㉘ 标注层几何与时序合法（R12-T59）：分区揭示的申报栏，一次请求都不发出就判完
# ---------------------------------------------------------------------------

#: 分区揭示的推进方向（`reveal.regions[].direction`）。
#: 四档都带一个「整块一次显示」的 `whole`：它落在两种正常写法上——① 一块没有推进感的
#: 元素（画面上直接出现的一面标注）；② 作者还没想好推进方向。**不给 `whole` 会逼人瞎填
#: 一个方向**，而瞎填的方向会直接决定 mask 的插值（母版按它逐帧扫），也就是决定像素。
REVEAL_DIRECTIONS = ("ltr", "rtl", "ttb", "btt", "whole")

#: 幕时长允许的超额（ms）：`scene_duration_ms` 与「末段结束」之间可以留的停留。
#: 判据不是「有停留」，而是**这个停留必须写在账上**（㉘ 的等式），0 也合法——
#: 一镜的最后一笔落定就是这一镜的结尾，那是最常见的写法。
REVEAL_HOLD_MAX_MS = 4000

#: `direction=whole` 时母版不做推进插值，所以它的 `duration_ms` 只用来占位。
#: 今天 **不额外校验**（写 0 也收）：一个「整块显示」的元素，谁也说不出它的时长该是多少，
#: 凭空定一个下限只是给模型加一条背不出理由的规矩。这一条如实写在这里，别当成漏。
_REVEAL_FIELDS = ("id", "label", "sequence", "narrative_role", "subtitle_ref",
                  "region", "direction", "start_ms", "duration_ms", "protected")


def _reveal_ints(rect: Any) -> tuple[dict[str, int] | None, str]:
    """`region{整数像素}` → 四个整数；不是整数就退回（连同理由）。"""
    if not isinstance(rect, Mapping):
        return None, f"region 不是对象（{type(rect).__name__}）"
    out: dict[str, int] = {}
    for k in ("x", "y", "w", "h"):
        v = rect.get(k)
        # `True` 是 int 的子类：`isinstance(True, int)` 为真，所以先排掉 bool，
        # 再要求值本身是 int 或「整数值的字符串/浮点」。坐标不许是 12.5——
        # 半像素坐标在不同画幅下取整方向不同，两处一取整就成了两把尺。
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None, f"region.{k}={v!r} 不是数字"
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None, f"region.{k}={v!r} 不是数字"
        if f != int(f):
            return None, f"region.{k}={v!r} 不是整数像素"
        out[k] = int(f)
    if out["w"] <= 0 or out["h"] <= 0:
        return None, f"region 的宽高读到 {out['w']}×{out['h']}（必须为正）"
    return out, ""


def _reveal_size(raw: Any) -> tuple[tuple[int, int] | None, str]:
    """`canvas` → `(w, h)`（正整数两栏）；缺栏或非整数退回。"""
    if not isinstance(raw, Mapping):
        return None, f"不是对象（{type(raw).__name__}）"
    pair: list[int] = []
    for k in ("w", "h"):
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None, f"{k}={v!r} 不是数字"
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None, f"{k}={v!r} 不是数字"
        if f != int(f) or int(f) <= 0:
            return None, f"{k}={v!r} 不是正整数像素"
        pair.append(int(f))
    return (pair[0], pair[1]), ""


def _reveal_ms(value: Any) -> int | None:
    """毫秒栏 → 非负整数；不是就 None（连同 NaN/负值一起退回）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != int(f) or f < 0:
        return None
    return int(f)


def reveal_canvas(raw: Mapping[str, Any]) -> tuple[int, int] | None:
    """`reveal.canvas` → `(w, h)`；缺这一栏或不是正整数像素时退回 `None`。

    单独开一个口子（而不是让调用方去碰 `_reveal_size` 那两个返回值），是为了让
    「canvas 怎么读」只有**一处**实现：尺寸对账（`code_render._check_reveal_canvas`）
    与 ㉘ 的形状校验必须用同一套读法，否则会出现一处认 `"1024"`、另一处不认这种
    「两边都说自己判了」的局面。非法那一档 ㉘ 会报（它才是形状的主人），这里不重复报。
    """
    size, _why = _reveal_size(raw.get("canvas"))
    return size


def reveal_probe_frames(shot: Mapping[str, Any], *, frames: int, fps: int) -> list[int]:
    """这一镜 ㉗ 要取证的**帧号**（升序去重；没申报 `reveal` 时是空表）。

    取法：每一块「**应该刚画满**」的那一帧 = `ceil((start_ms + duration_ms) / 1000 × fps)`。

    为什么只要这几个时刻（而不是每帧都取）：「到点必须已显示」与「未到点必须是纸底」
    这两句话在**同一帧**上同时判得动——在这一帧上，这一块必须已经画满，而它后面那些块
    必须还是纸底。于是 `n` 块只要 `n` 个时刻，㉗ 就同时把两个方向都看住了：
    某块提前露出来，会在**前一块**的那个时刻被抓到；某块到点没显示，在它自己的时刻被抓到。
    逐帧取是 `n × fps` 个采样，而多出来的那些帧说的还是同一件事。

    越界钳到 `[0, frames-1]`：申报写到这一幕之外（㉘ 不判这个——它是「这一幕多长」的
    问题，不是这一块的几何错）时，取这一镜**最后一帧**；那一帧仍然能证「到点没显示」。
    """
    raw = shot.get("reveal")
    if not isinstance(raw, Mapping):
        return []
    regions = raw.get("regions")
    if not isinstance(regions, (list, tuple)):
        return []
    last = max(0, int(frames) - 1)
    out: set[int] = set()
    for r in regions:
        if not isinstance(r, Mapping):
            continue
        start = _reveal_ms(r.get("start_ms"))
        dur = _reveal_ms(r.get("duration_ms"))
        if start is None or dur is None:
            continue
        at = int(-(-(start + dur) * int(fps) // 1000))       # 向上取整的整数写法
        out.add(max(0, min(last, at)))
    return sorted(out)


def check_reveal_regions(shot: Mapping[str, Any]) -> list[str]:
    """闸㉘ 镜级 `reveal` 栏的**形状合法性**（R12-T59），→ 错误清单（空＝过或没申报）。

    判六件事，全部是纯 schema 校验（不读像素、不读库，与 闸⑯ 的写法那一半同族）：

    1. `canvas` 两栏是**正整数**；每个 `region` 四栏是**整数像素**（浮点坐标打回：
       半像素在不同画幅下取整方向不同，两处一取整就成了两把尺）；
    2. `region ⊂ canvas`（越界的区域在画面上永远揭不开——母版按 canvas→盒 的比例换算，
       越界部分一乘就是画外，而账上它看着是合法的）；
    3. `sequence` **从 1 起、连续、不重复**（它是母版排揭示顺序、㉗ 找「后续区域」的唯一
       依据：有洞或有重号，两处就会各排出一个顺序）；
    4. **幕内 `start_ms` 两两不重叠**——按 sequence 排好的相邻两段，
       `前段 start_ms + duration_ms ≤ 后段 start_ms`；重叠的两段会同时推进两个 mask，
       画面上是两块一起揭开，而账上读作「依次」；
    5. `scene_duration_ms == 末段结束 + 停留`，停留 ∈ [0, :data:`REVEAL_HOLD_MAX_MS`]，
       **且等式必须写对**：账上的幕时长是拼接与验收读的那个数，差一毫秒就是两笔账；
    6. `protected[]` 里的每个 id 都在本镜区域里（保护一个不存在的区域＝这句话没有对象）。

    **拿不准的不许记成过**（D-未判不绿的反面用法）：这一条判的是**形状**，形状缺栏一律
    打回而不是「未判」——闸前这一层没有「未判」这个出口，未判属于需要真机读数的那几条
    （㉗）。所以这里宁可退回让模型补全，也不放一份读不懂的申报进渲染。
    """
    errors: list[str] = []
    raw = shot.get("reveal")
    if raw is None:
        return errors                       # 没申报＝这一镜不走这一族风格，㉘ 不适用
    sid = str(shot.get("id") or "")
    head = f"闸㉘ 标注层：{sid}："
    if not isinstance(raw, Mapping):
        return [f"{head}reveal 不是对象（{type(raw).__name__}）——"
                f"这一栏要么不写，要么写成 {{canvas, scene_duration_ms, regions[]}}"]

    cw = ch = None
    got_canvas, why = _reveal_size(raw.get("canvas"))
    if got_canvas is None:
        _err(errors, f"{head}reveal.canvas 读不出来：{why}——区域坐标以插画原图的像素"
                     f"为单位，没有 canvas 就说不清这四个数是画在哪张图上的")
    else:
        cw, ch = got_canvas

    regions = raw.get("regions")
    if not isinstance(regions, (list, tuple)) or not regions:
        return errors + [f"{head}reveal.regions 为空——申报了这一族风格就得说清有几块、"
                         f"按什么顺序揭"]

    seen_ids: set[str] = set()
    seqs: list[int] = []
    spans: list[tuple[int, int, int, str]] = []      # (sequence, start_ms, end_ms, id)
    prot: list[tuple[str, list[str]]] = []
    for i, r in enumerate(regions, 1):
        rid = str(r.get("id") or "").strip() if isinstance(r, Mapping) else ""
        if not rid:
            _err(errors, f"{head}第 {i} 块没有 id——它是顺序、保护栏与回执三处的指针")
            rid = f"#{i}"
        if rid in seen_ids:
            _err(errors, f"{head}id 重复：{rid!r}（回执与 protected 都按 id 指，重号就指不准）")
        seen_ids.add(rid)
        if not isinstance(r, Mapping):
            _err(errors, f"{head}第 {i} 块不是对象（{type(r).__name__}）")
            continue
        # 多出来的键**当场退回**，不静默忽略：这一栏的名字有两种写法的诱惑
        # （`startMs` 与 `start_ms` 只差一个下划线），静默忽略＝模型以为它申报了时序，
        # 而母版读到的是「没有开始时刻」——两边的账就这么分开了。
        extra = sorted(set(r) - set(_REVEAL_FIELDS))
        if extra:
            _err(errors, f"{head}{rid}：不认的栏位 {extra}——这一族只认 "
                         f"{', '.join(_REVEAL_FIELDS)}（毫秒与逐块栏位一律下划线写法，"
                         f"`startMs` 这种驼峰不认，别两处各写一个名字）")
        seq = r.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, int):
            _err(errors, f"{head}{rid}：sequence={seq!r} 不是整数")
            seq = None
        else:
            seqs.append(seq)
        start = _reveal_ms(r.get("start_ms"))
        dur = _reveal_ms(r.get("duration_ms"))
        if start is None:
            _err(errors, f"{head}{rid}：start_ms={r.get('start_ms')!r} 不是非负整数毫秒")
        if dur is None:
            _err(errors, f"{head}{rid}：duration_ms={r.get('duration_ms')!r} 不是非负整数毫秒")
        if start is not None and dur is not None and seq is not None:
            spans.append((seq, start, start + dur, rid))
        direction = str(r.get("direction") or "").strip().lower()
        if direction not in REVEAL_DIRECTIONS:
            _err(errors, f"{head}{rid}：direction={r.get('direction')!r} 不是一条推进方向"
                         f"（可用：{', '.join(REVEAL_DIRECTIONS)}）")
        canon, why = _reveal_ints(r.get("region"))
        if canon is None:
            _err(errors, f"{head}{rid}：{why}")
        elif cw is not None and ch is not None:
            x, y, w, h = canon["x"], canon["y"], canon["w"], canon["h"]
            if x < 0 or y < 0 or x + w > cw or y + h > ch:
                _err(errors, f"{head}{rid}：区域 {x},{y} {w}×{h} 超出了 canvas "
                             f"{cw}×{ch}——越界的那部分按比例换算到画面上是画外，"
                             f"永远揭不开，而账上它看着合法")
        sub = r.get("subtitle_ref")
        if sub is not None:
            idx = _reveal_ms(sub)
            if idx is None or idx < 1:
                _err(errors, f"{head}{rid}：subtitle_ref={sub!r} 不是「第几个词」"
                             f"（从 1 起的整数，与 `subtitle:<i>` 锚同一套序号）")
        got_prot = r.get("protected")
        if got_prot is not None:
            if not isinstance(got_prot, (list, tuple)):
                _err(errors, f"{head}{rid}：protected 不是数组")
            else:
                prot.append((rid, [str(p) for p in got_prot]))

    # ③ sequence 从 1 连续不重复：只在每一块都给出了整数 sequence 时才判——
    # 已有别的红在上面点了名，这里再报一次同一件事只是噪声。
    if seqs and len(spans) == len(seqs) and sorted(seqs) == list(range(1, len(seqs) + 1)):
        pass
    elif seqs:
        _err(errors, f"{head}sequence 必须从 1 起、连续、不重复，实得 {sorted(seqs)}"
                     f"（{len(regions)} 块）——母版排揭示顺序与 ㉗ 找「后续区域」都按它，"
                     f"有洞或有重号，两处会各排出一个顺序")

    # ④ 幕内 start_ms 两两不重叠（按 sequence 排序后判相邻）
    ordered = sorted(spans)
    for (s1, a1, e1, id1), (s2, a2, _e2, id2) in zip(ordered, ordered[1:]):
        if a2 < e1:
            _err(errors, f"{head}{id1}（第 {s1} 块，{a1}–{e1} ms）与 {id2}"
                         f"（第 {s2} 块，{a2} ms 起）**在时间上重叠**——两块会同时推进"
                         f"各自的 mask，画面上是一起揭开，而账上读作「依次」")

    # ⑤ 幕时长等式：末段结束 + 停留
    if ordered:
        last_end = ordered[-1][2]
        scene = _reveal_ms(raw.get("scene_duration_ms"))
        if scene is None:
            _err(errors, f"{head}scene_duration_ms={raw.get('scene_duration_ms')!r} "
                         f"不是非负整数毫秒——它是这一幕的总长，拼接按它对齐")
        else:
            hold = scene - last_end
            if hold < 0:
                _err(errors, f"{head}scene_duration_ms={scene} ms **短于**末段结束 "
                             f"{last_end} ms——最后一笔还没画完这一幕就结束了")
            elif hold > REVEAL_HOLD_MAX_MS:
                _err(errors, f"{head}scene_duration_ms={scene} ms 比末段结束 {last_end} ms "
                             f"多出 {hold} ms 的停留，超过 {REVEAL_HOLD_MAX_MS} ms 上限"
                             f"——这么长的空等要么是时长算错了，要么该拆成两幕")

    # ⑥ protected 指向本镜真有的区域
    for rid, ids in prot:
        ghost = [p for p in ids if p and p not in seen_ids]
        if ghost:
            _err(errors, f"{head}{rid}：protected 里的 {ghost} 不在本镜区域里"
                         f"——保护一个不存在的区域＝这句话没有对象")
    return errors


def check_motion_windows(shot: Mapping[str, Any], *, events: Sequence[Mapping[str, Any]],
                         word_frames: Sequence[int], fps: int, beats: Sequence[int] | None = None
                         ) -> tuple[list[str], list[str], list[str]]:
    """㉑ **闸后**：每个事件的触发帧，落在它锚的那条数据「首次可读」的 ±k 帧内吗。

    → (打回, 提醒, 未判)。`events` 是闸⑭ 从像素上量出来的那几段（`start_frame` 是
    触发帧）；`word_frames` 是本镜旁白逐词的帧刻度（= 母版 `props.words[].startFrame`
    的同一份，见 `code_render._to_frame_words`）。

    **对齐方式：按顺序一一对应**。申报的第 j 条 ↔ 数出来的第 j 段。这么对齐是因为两头
    都没有第二个身份可用（像素段没有名字，申报也没有帧号——**让模型自己报触发帧就是
    自报，不是证据**，与 §9-Q80 同一条理由）。所以：

    * 条数与段数不等 → **未判**，并说清两边各几条。不判成红：段数少一条完全可能是
      一次动画被像素切成了两段（反之亦然），那是读数的粒度问题，不是内容的错。
      **这条本身就是一条已知绕法**（把条数写错就永远不进窗口判），记在 §9-Q98。
    * 每个锚的「首次可读帧」：
        - `subtitle:<i>` → 第 i 个词的 `startFrame`（词表拿不到 / i 越界 → 未判）；
        - `beat:<n>`     → 节拍表今天**不存在**（这条链没做节拍检测）→ 未判；
        - `fields.<key>` → 字段的值从第 0 帧就注进去了，「它什么时候在画面上变得可读」
          不是这一层能反推的（反推它就是在做 ⑲-b 要模型做的那件事）→ 未判。
    * 全部锚都算得出窗口时：**每一个锚都必须落在 ±k 内**，有一个不在就打回
      （「每一个」而不是「其中一个」，见 :data:`MOTION_MAX_ANCHORS`：否则把所有词锚上
      就是必胜）。一个锚都算不出 → 未判。

    **提醒栏今天只有一条**：同一事件里挂了重复的锚（`["subtitle:3","subtitle:3"]`）——
    「每个锚都在窗口内」这句话的份量得按**不同数据**算，重复项在这句话上是零信息。它不拦
    是因为它没有错，只是这份申报比看上去薄。

    **未判不是通过**（D-未判不绿）：`fields.` 锚今天必然落进未判这一档，所以「把动效锚到
    必然存在的字段上」这条绕法今天只被**闸前那一半**挡着（该字段必须真在 fields 里），
    窗口那一半挡不住——§9-Q98 记了这一条，不许在交付语里被抹成「㉑ 已完全挡住」。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    sid = str(shot.get("id") or "")
    head = f"闸㉑ 动效锚定：{sid}："
    declared = shot.get("motion")
    declared = [e for e in declared if isinstance(e, Mapping)] \
        if isinstance(declared, (list, tuple)) else []
    fired = [e for e in events if isinstance(e.get("start_frame"), int)]
    if not declared:
        # 闸⑭ 保证了走到这里的片子每一镜都数得出 ≥2 段先后变化（数不出来的那几镜已经
        # 在 ⑭ 那里退回），所以「一条都没申报」到这里不再是「这一镜没有动效」，而是
        # 「有动效、一条都没说是为谁动的」——这正是 §8.3 第 7 支反例。
        if len(fired) >= 2:
            _err(errors, f"{head}像素上数得出 {len(fired)} 段先后变化，而这一镜"
                         f"**一条动效都没申报**——每一段都得说清它为哪条数据动"
                         f"（`fields.<字段名>` / `subtitle:<第几个词>` / `beat:<第几个节拍>`）")
        elif fired:
            undetermined.append(f"{head}没申报动效，像素也只数到 {len(fired)} 段——"
                                f"对应关系说不清，这一镜的锚定没判过")
        return errors, warns, undetermined
    if len(declared) != len(fired):
        undetermined.append(
            f"{head}申报了 {len(declared)} 条动效、像素上数到 {len(fired)} 段先后变化，"
            f"两边对不上——对不上就说不清哪一条对应哪一段，这一镜的窗口**没判过**"
            f"（未判不是通过）。把申报的条数改成与画面上真的动了几处一致")
        return errors, warns, undetermined

    tol = max(1, int(fps) // MOTION_ANCHOR_TOLERANCE_DIV)
    for j, (ev, seg) in enumerate(zip(declared, fired), 1):
        kind = str(ev.get("kind") or "?").strip() or "?"
        frame = int(seg["start_frame"])
        raw_anchors = ev.get("anchors")
        raws = [a for a in raw_anchors if isinstance(a, str)] \
            if isinstance(raw_anchors, (list, tuple)) else []
        # 重复的锚**只提醒不拦**，但必须出声：判据是「**每一个**锚都得落在窗口内」，
        # 于是 `["subtitle:3","subtitle:3","subtitle:3"]` 在回执上看起来是「三个锚都在
        # 窗口里」，实际只锚了一个数据——那是 :data:`MOTION_MAX_ANCHORS` 那条上限要堵的
        # 绕法的退化版。不拦是因为它没有错，只是这份申报比看上去薄。
        if len(set(raws)) != len(raws):
            warns.append(f"{head}第 {j} 条（{kind}）的锚里有重复项"
                         f"（{len(raws)} 个锚、{len(set(raws))} 个不同的数据）——"
                         f"重复不算多锚一个，「每个锚都在窗口内」这句话的份量按不同数据算")
        hits: list[tuple[str, int]] = []       # 算得出窗口的锚：(原文, 首次可读帧)
        blind: list[str] = []                  # 窗口算不出来的锚（连同理由）
        for raw in raws:
            got = parse_motion_anchor(raw)
            if got is None:
                blind.append(f"{raw!r}（写法认不出）")
                continue
            form, arg = got
            if form == "subtitle":
                idx = int(arg)
                if idx > len(word_frames):
                    blind.append(f"{raw}（这一镜旁白只有 {len(word_frames)} 个词）")
                    continue
                hits.append((raw, int(word_frames[idx - 1])))
            elif form == "beat":
                if not beats:
                    blind.append(f"{raw}（这条链没有节拍表，量不了）")
                    continue
                n = int(arg)
                if n > len(beats):
                    blind.append(f"{raw}（BGM 只量到 {len(beats)} 个节拍）")
                    continue
                hits.append((raw, int(beats[n - 1])))
            else:                                   # fields.<key>
                blind.append(f"{raw}（字段从第 0 帧就注进去了，「画面上的首次可读」"
                             f"这一层反推不出来）")
        if not hits:
            undetermined.append(
                f"{head}第 {j} 条（{kind}）的锚都没法算出「首次可读帧」——"
                + "；".join(blind) +
                f"。这一条**没判过**（未判不是通过）")
            continue
        off = [f"{raw}（差 {frame - at:+d} 帧，容差 ±{tol}）"
               for raw, at in hits if abs(frame - at) > tol]
        if off:
            _err(errors, f"{head}第 {j} 条（{kind}）触发的第 {frame} 帧，离它服务的"
                         f"数据**首次可读**那一帧太远：" + "；".join(off) +
                         f"。要么把这条动效挪到它所锚的那条数据真出现的那一刻，"
                         f"要么把它锚到那一刻真在的那条数据上")
        if blind:
            undetermined.append(
                f"{head}第 {j} 条（{kind}）有一部分锚算不出窗口：" + "；".join(blind) +
                "——那几条没判过")
    return errors, warns, undetermined


def check_degenerate_forms(shots: list[dict[str, Any]], *,
                           scanned: Mapping[str, Any] | None = None
                           ) -> tuple[list[str], list[str], list[str]]:
    """闸⑳ 退化形态：⑳(c) **画面同构**这一条。→ (打回, 提醒, 未判)。

    **这一道只实现 ⑳(c)，另两条已经各有唯一一处实现，这里不重写**（同一判据抄成
    第二份就是本仓踩过三次的清单漂移）：

    * ⑳(a)「卡片+文字」——`check_subject_present` 里那条**联合**判（主体 <18% 且
      文字 >45%）。⑳ 的账读它的结果，因为那一条判的就是 ⑳(a) 原本要判的形态。
    * ⑳(b)「一镜一模板的空转」——归闸④ 的 `fill_span` 与 R7-T18b 那条
      「模板套上去等于没画」，判在**代码**上，不在画面上。

    ⑳(c) 判的是**画面**：模型完全可以给两镜写不同的代码（于是闸⑨ 跨镜骨架同构那一条
    放过它——它还要求 ≥3 镜才定罪），而屏幕上出现的仍是同一张卡：同样的字压在同几个
    格子里、同样的图元铺在同一片。两个问题的答案不一样，所以两条闸并存。

    **两镜即算**（闸⑨ 那条要 ≥3 镜，是因为骨架同构得看整片才看得出是不是风格；这条要
    的只是「这里出现了一次重复」，所以整片两镜一样算），因此这里没有「至少 N 镜才
    定罪」那一档，也就不需要那个常量。

    读数（母版在探针帧上量的，闸⑧ 那条回读通道顺路带回）：
    ``grid_text`` / ``grid_drawn``（把 6×6 粗网格上被文字/图元压到的格子记成位掩码）与
    ``colors``（母版**从 DOM 里声明的**颜色字段收来的前四主色——不是对像素做直方图）。
    用粗网格而不是精确坐标：同一张卡换个写法坐标会差几像素，格子还是那几格。

    判一对：文字网格与图元网格**都相同且都非空**（0 是「这一镜没有这一类东西」，
    两个 0 相等毫无意义）→ 候选；再看配色：两边都认得出颜色且**颜色集合相同** → 定罪
    「两镜画面上是同一张卡」；两边都认得出而集合不同 → 配色不同，不是同一张卡，放行
    （按集合比而不是按频次序比：频次相近的两个色在两条片子上会互换名次）。
    颜色有一边认不出（母版读不到、或那一镜的颜色都不是字面值）→ 这一对**没判完**，
    落未判——只凭几何撞上就定罪会误伤「同版式不同配色」的合法系列镜头，而
    「没认出来」写成「不同」又会让这条闸恰好在它该响的时候闭嘴（D-未判不绿）。

    读数整栏取不到（母版旧、或这一镜压根没扫）→ 未判，逐镜记一条，说清是哪一栏缺。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    head = "闸⑳(c) 画面同构："
    readings = scanned if isinstance(scanned, Mapping) else {}

    # 逐镜取出三栏读数。`None` 一律是「没量到」，与「量到 0」分开记。
    prints: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        order.append(sid)
        got = readings.get(sid)
        got = got if isinstance(got, Mapping) else None
        scanned_row = got.get("scanned") if isinstance(got, Mapping) else None
        sc = scanned_row if isinstance(scanned_row, Mapping) else {}
        colors = sc.get("colors") if sc else None
        prints[sid] = {
            # 位掩码是整数；`bool` 会被判成「没量到」（`True` 掩码毫无意义）。
            "grid_text": _as_int(sc.get("grid_text")),
            "grid_drawn": _as_int(sc.get("grid_drawn")),
            # 认不出颜色时是空表，与「没读到这一栏」（None）不是一件事。
            "colors": ([str(c) for c in colors if str(c).strip()]
                       if isinstance(colors, (list, tuple)) else None),
        }
        # 整栏取不到就逐镜记未判：**不能靠「这一对跳过」悄悄放过**——那正是
        # 「没量到」被读成「量到了、没问题」的老路，而这一栏恰好在母版旧时最缺。
        if not sc or prints[sid]["grid_text"] is None or prints[sid]["grid_drawn"] is None:
            why = ("这一镜的版面压根没扫成（闸⑧ 那一趟落空）" if not sc else
                   "镜像里的母版比这条链旧，这一趟没带画面的粗网格指纹")
            prints[sid] = {"grid_text": None, "grid_drawn": None, "colors": None}
            undetermined.append(
                f"{head}{sid} 的**画面指纹**没读到（{why}），这一镜跟谁像**没判过**——"
                f"未判不是通过")

    for a in range(len(order)):
        for b in range(a + 1, len(order)):
            pa, pb = prints[order[a]], prints[order[b]]
            gta, gda = pa["grid_text"], pa["grid_drawn"]
            gtb, gdb = pb["grid_text"], pb["grid_drawn"]
            if gta is None or gtb is None:
                continue                              # 这一对里有镜没读到，上面已记未判
            if not gta or not gda:                    # 0 = 这镜没文字/没图元，不构成候选
                continue
            if gta != gtb or gda != gdb:
                continue
            ca, cb = pa["colors"], pb["colors"]
            if ca and cb:
                # 比的是**集合**而不是「谁排第一」：频次相近的两个色在两条片子上会互换
                # 名次，按位比就会把同一张卡判成不同。集合不同 = 配色不同，放行。
                if set(ca) != set(cb):
                    continue                          # 几何撞上、配色不同：不是同一张卡
                _err(errors,
                     f"{head}{order[a]} 与 {order[b]} 的**画面**是同一张卡：文字压在同样的"
                     f"格子里、图元铺在同一片（粗网格指纹 0x{gta:x}/0x{gda:x} 两镜相同）、"
                     f"配色也一样（{'、'.join(ca)}）——代码写得不同，屏幕上没区别。两镜的"
                     f"分工、构图、主色至少换一样（换主色只算换张皮，优先换构图：把主体"
                     f"挪到别的位置、换一个不同的版式族）")
                continue
            # 有一边认不出颜色：几何撞上但这一对没判完。
            who = order[a] if not ca else order[b]
            undetermined.append(
                f"{head}{order[a]} 与 {order[b]} 的几何指纹撞上了（0x{gta:x}/0x{gda:x}），"
                f"但 {who} 的主色**没认出来**（那一镜的颜色都不是字面值），这一对没判完——"
                f"未判不是「不同」，也不是「相同」")
    return errors, warns, undetermined


#: 抽象词那条打回理由里给的改写示范——**刻意给一个器物**，因为真机上模型被拦之后
#: 最常见的反应是把抽象词换个近义词（「理念」→「观念」），而不是去画一个东西。
_SUBJECT_HINT = "活字盘"


# ---------------------------------------------------------------------------
# R7-T22 转场（拼接层）与进出场（母版）的**单源出口**
# ---------------------------------------------------------------------------
# 三件事在这一节里各有唯一一处实现：转场档位 → 给 ffmpeg 的两个数、入场/出场窗口 →
# 给母版的帧数、以及「这个转场时长配得上这条片子的逐镜时长吗」这条判据。
# 为什么都要出口：渲染趟（`code_render`）与离线测试读的是同一份数字，
# 各转一遍 `transition_sec` 就会出现「回执写 0.4 秒、ffmpeg 收到 0」这种
# 只有抽帧才发现的分歧——与 ``shot_texture`` 那条「记账与渲染不许各写一遍」同族。


def transition_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    """整片转场 + 逐接缝覆盖（层E1）→ ``{"style", "duration", "seams"}``。

    ``style``/``duration`` 是**整片档**（不转场时 duration=0.0，与 R7-T22 逐字一致）；
    ``seams`` **只列被逐镜覆盖过的那几处**，每项 ``{"after": 镜号, "style", "duration"}``。
    没覆盖的接缝不在表里——它们跟整片档，这正是「旧片子」与「新片子」在这一栏上唯一的
    差别：一条没写逐镜 `transition` 的片子，`seams` 就是空表，读数一个字节都不变。

    为什么「只列覆盖」而不是「逐接缝铺平」：铺平之后每条接缝都有一个值，于是
    「整片 fade、这一处没覆盖」与「整片 none、这一处 fade」在账面上长得一样，
    而它们在画面上是两件事。空表 + 回落整片档，把这两件事分得开。

    覆盖的时长沿用整片那一档；**整片压根不转场**时取 :data:`TRANSITION_SEC_DEFAULT`
    （否则「整片硬接、这一处圆开」会落成一个 0 秒的转场，等于没写）。

    ``style`` 只可能是 :data:`TRANSITION_STYLES` 里的一个（越界按 none；调用它的那条
    路上 spec 已经过 ``validate_code_spec``，这里是给「有人手搓一份 spec 直接渲」兜底）。
    """
    style = str(spec.get("transition") or "none").strip().lower()
    if style not in TRANSITION_STYLES:
        style = "none"
    try:
        span = float(spec.get("transition_sec") or 0.0)
    except (TypeError, ValueError):
        span = 0.0
    base_span = round(span, 3) if (style != "none" and span > 0) else 0.0
    seams: list[dict[str, Any]] = []
    shots = list(spec.get("shots") or [])
    for i, shot in enumerate(shots[:-1]):
        row = shot if isinstance(shot, Mapping) else {}
        raw = str(row.get("transition") or "").strip().lower()
        if not raw or raw not in TRANSITION_STYLES:
            continue
        sid = str(row.get("id") or f"s{i + 1:02d}")
        dur = base_span if base_span > 0 else float(TRANSITION_SEC_DEFAULT)
        seams.append({"after": sid, "style": raw,
                      "duration": 0.0 if raw == "none" else round(dur, 3)})
    if style == "none" and not seams:
        return {"style": "none", "duration": 0.0, "seams": []}
    return {"style": style, "duration": base_span, "seams": seams}


def seams_to_per_index(seams: Sequence[Mapping[str, Any]],
                       pairs: Sequence[tuple[str, float]]
                       ) -> list[tuple[str, float]]:
    """把「只列覆盖」的 ``seams`` 摊成**逐接缝**的一栏（长度 = 接缝数 = 镜数 − 1）。

    → ``[(档名, 时长), …]``；没被覆盖的那几处记 ``("", 0.0)``。

    空串不是「硬接」，是「**没覆盖**」：调用方拿它去回落整片档。这两件事必须分得开，
    否则「整片 fade、这一处没覆盖」会被读成「这一处硬接」——而画面上它明明是淡入。
    """
    by_after = {str(s.get("after") or ""): s for s in (seams or [])}
    out: list[tuple[str, float]] = []
    for sid, _sec in list(pairs)[:-1]:
        row = by_after.get(str(sid))
        if row is None:
            out.append(("", 0.0))
        else:
            out.append((str(row.get("style") or ""), float(row.get("duration") or 0.0)))
    return out


def shot_motion_window(frames: int) -> int:
    """入场/出场窗口的帧数（母版按它逐帧插值）。→ 帧数，0 = 这一镜不动作。

    窗口 = ``min(这一镜的 10%, SHOT_MOTION_MAX_FRAMES)``，短到 2 帧以下就当没有
    ——1 帧的淡入不是「淡入」，是硬切上多一层闪。

    **在 Python 侧算、按帧数注入母版**（`shot_input_props` 的 `entranceFrames`/
    `exitFrames`），而不是在 TS 里再算一遍：闸④ 的采样点（0.15/0.5/0.85）与这个窗口
    是同一条算术关系（窗口收在 0.15 之前），两处各算一遍迟早出现「窗口涨进采样点、
    闸④ 开始误报空壳」这种只在真机上看得见的漂移。
    """
    span = min(int(round(int(frames) * SHOT_MOTION_MAX_SHARE)), SHOT_MOTION_MAX_FRAMES)
    return span if span >= 2 else 0


# ---- 闸⑰ 音画数据对账（R7-T23）---------------------------------------------
#
# P20 的原话是「声画只有形容词」：BGM 短成片、旁白关着却有旁白轨，都没人查。这一道把
# 其中**能量成数**的三件事判成数与字节，判在**合成之前**（那时一帧都还没拼，红了只是
# 几秒的代价）：
#   ① BGM 素材 ≥ 成片 × 0.98；
#   ② 旁白开着就得真有声、关着就得真静（判的是**逐镜切片**的音轨电平）；
#   ③ 接缝的字尾账：转场把两侧尾巴交叠的那个窗口（长度 = 每处转场时长 d）里，
#      语音会跟着淡出——所以 d 不得大于两侧各自的**语音尾余量**，否则话被吃掉半句。
#
# 三条都是「量出来的数对不上」才红，没有一条是形容词；量不到的（没有音轨、
# volumedetect 没吐数）**记提醒、不当通过**——「未判」在这一道同样不算绿。

#: BGM 素材至少要占成片时长的这个比例。为什么要这条而不是让混音那趟自己报错：
#: `render._mux_bgm` 用的是 `-stream_loop -1`——BGM 短了**不报错、不静音，而是从头
#: 反复**：10 秒成片配 3 秒曲子会循环三遍多，听感是「这段音乐怎么又来了」，而链上
#: 没有任何一层会说话。0.98 留的是收尾那点淡出余量（差 0.2 秒以内听不出来，
#: 也就没必要为这点差把好素材打回）。
AV_BGM_MIN_SHARE = 0.98
#: 静音线：低于 −50 dB 算听不见。`anullsrc` 编出来的静音垫片实测在 −91 dB 上下，
#: 真说话的旁白在 −20 ~ −30 dB——中间留得很宽，因为这一条判的是**有没有**，
#: 不是**多响**（响度是混音那趟的事）。
AV_SILENT_DB = -50.0
#: 接缝字尾账的容差：一帧。d 与尾余量相等时，最后那个字的淡出正好落在镜尾，
#: 听感上已经没有了；给一帧的余量，免得浮点把一次过得去的片子挡在门外。
AV_TAIL_SLACK_SEC = 1.0 / 30.0


def check_av_accounting(*, narration: bool, film_sec: float, clip_db: float | None,
                        clips_with_audio: int, bgm_sec: float | None, gaps: int,
                        overlap_sec: float,
                        voice_tails: Sequence[tuple[str, float]],
                        fps: int,
                        overlap_secs: Sequence[float] | None = None
                        ) -> tuple[list[str], list[str]]:
    """闸⑰：合成之前把声画对成数。→ (打回, 提醒)。

    * ``film_sec`` —— 成片该有多长，按**账**算：逐镜实测秒数之和 − 接缝数 × d
      （与 T22 的 ``check_transition_effect`` 判的是同一个数，那条用成片实测复核它）。
    * ``clip_db`` / ``clips_with_audio`` —— 逐镜切片里**最响的那一条**的峰值声压
      （dB）与带音轨的镜数。判在切片而不是成片上：这一步发生在拼接之前，
      红了连一帧都不用拼。
    * ``bgm_sec`` —— BGM 素材自己的实测时长；没选 BGM 时传 ``None``（那一档不判，
      不是「通过」）。
    * ``voice_tails`` —— ``[(镜号, 语音结束到镜尾的余量秒)]``，接缝字尾账读它。
      余量的算法在渲染那一层（``code_render``，用的是 ``render.BREATH_*`` 那几个常量），
      这里只判「够不够容下这个转场」——两处各算一遍那条呼吸算术就又有了第二个出处。
    * ``overlap_sec`` —— 每处转场的时长 d（``transition_plan`` 那一份），``gaps`` 是接缝数。
    * ``overlap_secs`` —— 层E1 的**逐接缝**时长（长度 = 接缝数，硬接那几处是 0.0）。
      给了就按它逐处判字尾；不给时按 ``overlap_sec`` 摊平成 ``gaps`` 处（＝从前的行为）。
      为什么要逐处：整片 fade 0.4s、某一处覆盖成 0.9s 时，「每处都按 0.4 判」会漏掉
      真会切到字的那一处，而「每处都按 0.9 判」会把另外几处误杀。
    """
    errors: list[str] = []
    warns: list[str] = []

    # ① BGM 够不够长（够不够在整片里只放一遍）。
    if bgm_sec is not None:
        need = film_sec * AV_BGM_MIN_SHARE
        if bgm_sec < need:
            # 2026-10-15 起降为提醒：配乐多半是用户自己指定的那一首，为它短了几秒
            # 就把整片打回、让模型去换曲，等于否掉用户的选择；循环混音本身能出片。
            times = film_sec / bgm_sec if bgm_sec > 0 else float("inf")
            warns.append(
                f"闸⑰ BGM 偏短：素材 {bgm_sec:.2f}s 不够成片 {film_sec:.2f}s 的"
                f" {AV_BGM_MIN_SHARE:g} 倍，混音会把这支曲子循环约 {times:.1f} 遍"
                "（交付时如实告诉用户）")

    # ② 旁白到底在不在。两个方向都判，缺一条就会有半边永远没人看：
    #    关着却有声 → 有东西混进了旁白轨（旧轨残留、静音垫片被换成了真音）；
    #    开着却是静 → 话根本没进片子（TTS 降级成一个空壳）。
    if not narration:
        # 「一条音轨都没有」是一个**确定的事实**（旁白关着，本来就该没有），不是未判：
        # 这一档在回执上就是「0 镜带音轨」。只有「有音轨却量不出电平」才记未判。
        if clips_with_audio == 0:
            pass
        elif clip_db is None:
            warns.append("闸⑰ 旁白关着，切片带着音轨，但电平没量出来（volumedetect 没吐数）"
                         "——这一档**不判**，不当通过：量不到与很安静在回执上是两句话")
        elif clip_db > AV_SILENT_DB:
            errors.append(
                f"闸⑰ 旁白关着，切片里却响着 {clip_db:.1f} dB（静音线 "
                f"{AV_SILENT_DB:.0f} dB，{clips_with_audio} 镜带音轨）——旁白轨没关干净")
    else:
        if clips_with_audio == 0:
            errors.append("闸⑰ 旁白开着，但没有一镜带音轨——话一帧都没进片子。"
                          "先查逐镜的 `degraded`（TTS 是不是降级成了静音垫片）")
        elif clip_db is None:
            warns.append("闸⑰ 旁白开着，却没量到电平——这一档**不判**，不当通过")
        elif clip_db <= AV_SILENT_DB:
            errors.append(
                f"闸⑰ 旁白开着，切片里最响也只有 {clip_db:.1f} dB（静音线 "
                f"{AV_SILENT_DB:.0f} dB）——这是一条静音垫片，不是旁白。"
                "「加了静音轨」不等于「有配音」，两者在时长上一模一样")

    # ③ 接缝的字尾账：转场把两侧各 d 秒的尾巴交叠起来淡出，而语音就住在尾巴前面。
    #    所以每一处接缝上，d 必须 ≤ 两侧**语音之后剩下的余量**——不然那个字还没说完
    #    就被淡掉了，而片子照样出得来、闸②③④ 全都绿。这一条是 T22 的 acrossfade
    #    那笔账的落点：`check_transition` 判的是「d 短于半镜」（会不会炸），
    #    这一条判的是「d 吃不吃得下这条片子自己的字尾」（会不会切到字）。
    if gaps > 0 and overlap_sec > 0 and len(voice_tails) >= 2:
        # 逐接缝的 d：给了 `overlap_secs` 就用它（层E1），否则按标量摊平（从前的行为）。
        # 硬接那一处 d=0，直接跳过——没有交叠就吃不到字尾。
        per_seam = (list(overlap_secs) if overlap_secs is not None
                    else [float(overlap_sec)] * int(gaps))
        for i, d in enumerate(per_seam[:max(0, len(voice_tails) - 1)]):
            if float(d) <= 0:
                continue
            sid_a, tail_a = voice_tails[i]
            sid_b, tail_b = voice_tails[i + 1]
            room = min(tail_a, tail_b)
            if float(d) > room + AV_TAIL_SLACK_SEC:
                errors.append(
                    f"闸⑰ 接缝 {i + 1}（{sid_a}→{sid_b}）的转场会切到字：d="
                    f"{float(d):.2f}s，而两侧语音之后的余量只有 {tail_a:.2f}s / "
                    f"{tail_b:.2f}s。转场是把两侧尾巴**交叠淡出**的，余量比它短的那一侧"
                    "会有半句话在淡出里——请把 d 降一档，或让这两镜各自长一点"
                    "（`min_duration_sec` 是一个下限，不是精确时长）")

    return errors, warns


def check_transition(pairs: Sequence[tuple[str, float]], *, style: str,
                     duration: float,
                     seams: Sequence[Mapping[str, Any]] | None = None
                     ) -> tuple[list[str], list[str]]:
    """转场的时长对账（T22；合成之前，与闸⑰ 同一档但不同事）。→ (打回, 提醒)。

    ``pairs`` 是**逐镜实测秒数** ``[(镜号, 秒), …]``，按播放顺序。为什么这条判据没有
    落在 ``validate_code_spec``（分镜期那一堆闸里）：**一镜多长是 TTS 说了算的**，
    分镜那会儿手里只有 ``min_duration_sec`` 这个下限，拿它判会把「真实时长本来够」的
    片子误杀。所以分镜期只收枚举与档位（见 ``TRANSITION_SEC_CHOICES``），
    这条在渲染趟拿到真数之后、拼接之前判——那时一帧都还没拼，代价是几秒不是几分钟。

    判据只有一条（但它是**会真的炸**的那条）：转场时长必须**短于**相邻较短那一镜的
    一半。xfade 是拿两段的重叠区做混合的，重叠超过半镜就不是「转场」而是「把这一镜
    换掉」——ffmpeg 到那时会报 ``Offset … is after the end`` 或悄悄吃掉整镜，
    两种都是失败。边界取**严格小于**：等于半镜时第一段的 ``offset + duration``
    正好顶到它的末尾，浮点误差会把一次好渲染挡在门外。

    层E1（R19）：``seams`` 是 ``transition_plan()["seams"]``（**只列覆盖**的那一份）。
    每一处接缝先查它有没有被覆盖——覆盖了就用覆盖的档与时长，没覆盖才回落
    ``style``/``duration``。**硬接的那一处不判**（``none`` 没有重叠可超），所以
    「整片 fade、中间硬切一刀」这种配法不会在硬切那一处报错。
    """
    errors: list[str] = []
    pairs = list(pairs)
    if len(pairs) < 2:
        return errors, []
    if style not in TRANSITION_STYLES:
        style = "none"
    # 逐接缝的**实际**档与时长：覆盖优先，没覆盖回落整片档。
    eff = [(st or style, (d if st else float(duration)))
           for st, d in seams_to_per_index(seams or [], pairs)]
    active = [(i, st, float(d)) for i, (st, d) in enumerate(eff)
              if st not in ("", "none") and float(d) > 0]
    if not active:
        if not (seams or []) and style != "none" and float(duration) <= 0:
            _err(errors, f"转场对账：transition={style} 但时长是 {duration}s——"
                         f"选了转场就得给一个正数（可选 {', '.join(TRANSITION_SEC_CHOICES)} 秒）")
        return errors, []
    # 时长没拿到就不许判（也不许当通过）：`sec=0` 只可能来自「这一镜压根没算过时长」，
    # 那种情况下任何结论都是猜的。
    blank = [sid for sid, sec in pairs if not (float(sec) > 0)]
    if blank:
        _err(errors, f"转场对账：{len(blank)} 个镜头没有实测时长（{'、'.join(blank[:6])}…）"
                     f"——这一趟拿不到时长就没法判转场合不合法，不许当通过")
        return errors, []
    violations: list[str] = []
    for i, _st, span in active:
        sid_a, sec_a = pairs[i]
        sid_b, sec_b = pairs[i + 1]
        limit = TRANSITION_SHOT_SHARE * min(float(sec_a), float(sec_b))
        if span < limit:
            continue
        short = sid_a if float(sec_a) <= float(sec_b) else sid_b
        short_sec = min(float(sec_a), float(sec_b))
        ok = [s for s in TRANSITION_SEC_CHOICES if float(s) < limit]
        advice = (f"把 transition_sec 降到 {', '.join(ok)} 秒里的一档"
                  if ok else
                  f"本片没有一档转场落得进这半镜（[{', '.join(TRANSITION_SEC_CHOICES)}] 秒）")
        violations.append(
            f"{sid_a}→{sid_b} 这一处：转场 {span:g}s ≥ 相邻较短那一镜（{short} "
            f"{short_sec:.2f}s）的一半 {limit:.2f}s——xfade 拿重叠区做混合，"
            f"重叠超过半镜就等于把这一镜换掉。{advice}，或把 {short} 这一镜加长"
            f"（min_duration_sec）")
    if violations:
        # 一处超限往往意味着**每一处**都超限（同一个档位配同一批短镜），
        # 逐处报会把打回清单淹掉：报第一处 + 总处数，其余是同一件事。
        extra = f"（另有 {len(violations) - 1} 处同样超限）" if len(violations) > 1 else ""
        _err(errors, f"转场对账：{len(violations)} 处接缝的转场时长超过半镜{extra}。"
                     f"{violations[0]}")
    return errors, []


def check_transition_effect(secs: Sequence[float], total_sec: float, *,
                            style: str, duration: float,
                            fps: int,
                            seams: Sequence[float] | None = None) -> list[str]:
    """转场**真的生效了**没有（T22 的机器判据）。→ 错误清单（空 = 生效或压根没选转场）。

    怎么判：「叠 n−1 次、每次 d 秒」在成片时长上是可以算出来的——成片应当比
    逐镜之和**短** ``Σd`` 秒。这不是间接指标，它就是这件事本身：xfade 把两段的
    重叠区合成一段，合掉了多少秒是量得到的。拼接层哪天退回硬接（或 filter 图写错了
    某一档），这条立刻红——而画面上「少了 4 秒」是看不出来的（这正是 P21 要的那种
    机器判据：断言「逐镜独立渲染仍命中缓存」管住上限，这条管住下限）。

    层E1（R19）：``seams`` 给了就按它算——它是**逐接缝的实际时长**（长度 = 接缝数，
    硬接那几处是 0.0，由 :func:`seams_to_per_index` + 整片档摊出来）。``Σ`` 为 0
    等于全片硬接，这一条不说话。不给 ``seams`` 时按老口径 ``(n−1) × duration``。

    容差按帧给而不是按秒给：实测每次合并有约 1 帧的取整差（逐镜切片的容器时长与
    ffmpeg 内部认为的那一段长度差一帧），所以 ``(接缝数 + 2) / fps``。
    按秒给一个常数容差会在大镜数下把「一次都没生效」也放过去
    （11 处接缝 × 0.4 s = 4.4 s 的缺口，容差只要小于它就能抓到）。
    """
    secs = list(secs)
    if len(secs) < 2:
        return []
    if seams is not None:
        want = sum(float(x) for x in seams)
    else:
        if style not in TRANSITION_STYLES or style == "none":
            return []
        want = (len(secs) - 1) * float(duration)
    if want <= 0:
        return []
    got = sum(float(x) for x in secs) - float(total_sec)
    tol = (len(secs) - 1 + 2) / max(1, int(fps))
    if abs(got - want) <= tol:
        return []
    return [f"转场没生效：选了 {style} {duration}s、{len(secs) - 1} 处接缝，"
            f"成片本该比逐镜之和短 {want:.2f}s，实测只短了 {got:.2f}s"
            f"（容差 {tol:.2f}s）——拼接层没按转场接，或那一档 filter 图没落进去。"
            f"逐镜切片是好的（缓存那边对得上），问题在拼接这一趟"]



# ---------------------------------------------------------------------------
# 闸⑨ 跨镜结构雷同（同一张卡复制一份只换色相 → 在烧像素之前打回）
# ---------------------------------------------------------------------------

#: 剥掉的内容：**注释、字符串字面量、import 语句、十六进制颜色**。留下的是
#: 「结构 + 数字」——时间轴上的秒数、translate 的像素、半径、透明度。
#: 于是这条判据切得干净：**只换文案与色相的两镜 → 相似度 1.0**（那就是缺陷本身），
#: 而换了参数与版式的两镜 → 相似度掉下来（那是正当的复用）。
_SHAPE_STR_RE = re.compile(
    r"""//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""",
    re.S)
_SHAPE_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_SHAPE_IMPORT_RE = re.compile(
    r"""^[ \t]*(?:import|export)[\s\S]{0,400}?from[ \t]+['"][^'"]+['"];?[ \t]*$""",
    re.M)
#: 上面那条的 `[\s\S]{0,400}?` 是一个**跨行窗口**：行首的 `export` 只要在 400 字节内
#: 能摸到一条 `from "…"`，中间整段都会被当成同一条 import 剥掉。实测 158 份 demo 里
#: 没有触发（真代码的 import 都在顶部，末尾不会紧跟一条 import），但**标定脚本里踩过**：
#: 扰动时把 import 追加到文件末尾，末尾那句 `export const X = 140;` 就跟它并成一条、
#: 一起被剥，正样本凭空掉到 0.92。这里记一笔是为了下一次有人看到「明明只加了 import，
#: 骨架怎么少了」时不用再查一遍。
_SHAPE_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\sA-Za-z0-9_$]")

#: 骨架短于这个 token 数的镜不参与比对（三五行代码怎么比都是噪声，宁可不判）。
_SHAPE_MIN_TOKENS = 40

#: 打回线 0.88 与提醒线 0.78 不是拍的：拿技能语料里剥后仍不少于 40 个 token 的
#: 参考实现两两比过，**互不相同的两份代码最高只到 0.736**（SlideSpotlightPan ↔
#: GlowWakeSleepPanel），而把其中任意一份只换字符串与颜色，相似度是 **1.000**。
#: 两条线都落在 0.736 之上，所以「真换了写法」的片子不会被这一条误伤。
#:
#: 这两个数**与 `DERIVED_MIN_SIM` 那段是同一张分布表**（都由
#: `scripts/calibrate_derived_sim.py` 的负样本那一栏产出：158 份可比 demo、12403 对、
#: max 0.736）。这段注释原先写的是「152 个 / 中位 0.619」——**今天复现不出来**，
#: 三种语料口径都试过：158 份技能语料与 158 份去 `_legacy` 的 `remotion/src/demos`
#: 都是中位 0.400 / max 0.736（两组是同一批文件），全量 226 份（含 `_legacy`）是
#: 中位 0.344 / max 1.000（max 到 1.000 正是 `_legacy` 里躺着字面重复件的证据，也是
#: 它不进口语料的原因）。**两处都复现不出来就不给它编一个原因**——按可复跑的那一份写。
#: 判据那两条线（0.88 / 0.78）没有变，而它们依赖的**事实**换了口径。
#:
#: 为什么这条必须做在契约层：画面语义没有机器判据（闸④ 只判「带子里有没有东西」，
#: 一个母题重复十二镜在它那里全是绿的），而 2026-10-11 那四条片子的观感恰恰是
#: 「同一个母题每镜重来一遍，只换色相」——那是**跨镜**的性质，任何单镜判据都看不见它。
STRUCT_SAME = 0.88
STRUCT_NEAR = 0.78

#: 几镜同构才算打回：两镜像可以是同一拍的两个镜头（正文允许），三镜起就是全片
#: 一个母题了——那正是用户驳回「拼贴」时指的那件事。
STRUCT_MIN_GROUP = 3


def canonical_shape(code: str) -> tuple[str, ...]:
    """一镜代码的**结构骨架**：换文案与换色相都不改变它。"""
    src = _SHAPE_IMPORT_RE.sub("", str(code or ""))
    src = _SHAPE_STR_RE.sub("S", src)
    src = _SHAPE_HEX_RE.sub("C", src)
    return tuple(_SHAPE_TOKEN_RE.findall(src))


def shape_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """两份骨架的 token **多重集** Jaccard（交/并）。"""
    ca, cb = Counter(a), Counter(b)
    union = sum((ca | cb).values())
    return sum((ca & cb).values()) / union if union else 0.0


@lru_cache(maxsize=256)
def _demo_shape(relpath: str) -> tuple[str, ...] | None:
    """读一份 demo 代码并算骨架。读不到返回 None（文件不在或读失败）。"""
    f = _SKILL_DIR / "demos" / relpath
    if not f.is_file():
        return None
    try:
        return canonical_shape(f.read_text("utf-8"))
    except Exception:
        return None


def check_derived_similarity(shots: list[dict[str, Any]]
                             ) -> tuple[list[str], list[str]]:
    """闸（derived_from）：本镜代码与声明的 demo 源文件骨架相似度 ≥ DERIVED_MIN_SIM。

    模型声明 ``derived_from: ["demos/opening/fracture/Fracture.tsx"]`` 但代码与该 demo
    的骨架相似度低于阈值 → 声明是假的，打回。

    两边的实测数都在 ``DERIVED_MIN_SIM`` 那段（158 份可比 demo、扰动种子 20261012）：
    正样本（一份 demo ↔ 换掉文案/色号/import 的它自己）全 1.000，负样本（两份不同 demo）
    p98 = 0.605、max = 0.736、在 0.61 上的通过率 1.690%。**别在这里另抄一份数**——
    那是这段注释上一次的样子（写着「最高 0.603」），而它早已不是语料的事实。

    只出打回不出提醒：这一条的性质是「声明造假」，没有「可疑」档。

    闸⑳ 的原创哨兵（R19）**跳过这一道**：`derived_from == ["<original>"]` 的镜声明里
    压根没有 demo，无从比起——那不是「声明造假」，是另一条合法的路。跳过它**不等于
    免检**：lint（闸①）与闸④⑧⑩ 照样判它，配额由 :func:`check_original_share` 管。
    """
    errors: list[str] = []
    warns: list[str] = []
    for shot in shots:
        sid = str(shot.get("id") or "")
        code = str(shot.get("code") or "")
        derived = shot.get("derived_from") or []
        if isinstance(derived, str):
            # 模型把这一栏写成裸字符串（``derived_from: "demos/x/Y.tsx"``）而不是列表时，
            # 下面那个 for 会**逐字符**遍历它：每个字符都当成一个 relpath 去读，全部读不到，
            # 于是每一镜都得到「最高 0.000（全部读不到）」的假打回——真机 run 97c1289eba35
            # 里骨架相似度实测 0.780/0.809/0.645 的三镜也被这一条打回。这里按字面归一。
            derived = [derived]
        if [str(d) for d in derived] == [ORIGINAL_SENTINEL]:
            continue
        if not code or not derived:
            continue
        shot_shape = canonical_shape(code)
        if len(shot_shape) < _SHAPE_MIN_TOKENS:
            continue
        best = 0.0
        best_rel = ""
        for rel in derived:
            ds = _demo_shape(str(rel))
            if ds is None or len(ds) < _SHAPE_MIN_TOKENS:
                continue
            sim = shape_similarity(shot_shape, ds)
            if sim > best:
                best = sim
                best_rel = str(rel)
        if best < DERIVED_MIN_SIM:
            errors.append(
                f"闸（derived_from）{sid}：代码与声明的 demo 源文件骨架相似度 "
                f"最高 {best:.3f}（{best_rel or '全部读不到'}）< {DERIVED_MIN_SIM}。"
                "声明 derived_from 但代码与该 demo 无结构关联——要么改代码让它真正"
                "从该 demo 演化而来，要么改 derived_from 指向真正来源的 demo")
    return errors, warns


# ---------------------------------------------------------------------------
# 取材轮（P9 / T20）：模型「读得到卡、读不到码」——先把被引用 demo 的**全文**交给它
# ---------------------------------------------------------------------------
#
# 为什么需要一轮单独的调用，而不是在分镜轮里顺手把 demo 附上：
# ``plan_motion`` 一次调用里模型**同时**写 ``code`` 与 ``derived_from``。也就是说，
# 等闸知道「这一镜引用的是哪几份 demo」时，代码那一段字节**已经写完了**——那时候再
# 注入就是事后补票，模型写码那一刻手里没有 demo 全文，它只能凭记忆手搓，而闸⑫ 还会
# 因为它照着 demo 的**样子**写而判它「真派生过」。所以顺序必须反过来：先声明引用哪几份
# （只有声明、没有代码），拿回全文，再写代码。这就是取材轮。
#
# 它与「让模型自己调 load_skill」的区别是**判定与取字节同源**：取材轮声明的 relpath
# 过的就是 ``_norm_cite`` 那把白名单尺（与闸⑫ 同一份 MANIFEST），取到的文本逐字节
# 等于盘上那份文件（见 ``collect_demos`` 的断言口径）。模型点 load_skill 也对，
# 但那要求它先猜到路径、再抄对；取材轮把「引用了什么」变成一次可校验的声明。

#: 取材轮一次回执最多带回多少字节的 demo 全文。
#:
#: **超线不截断**：一份断在中间的 TSX 比不给更坏——模型会顺着断口把剩下半段写完，
#: 而闸⑫ 比的正是骨架，「半份 + 自己补的另一半」照样能过线。所以超线的那几件进
#: ``forage_oversize`` 名单（点名 + 字节数），让模型用 ``load_skill(name, path=…)``
#: 逐件取回；**每一份要么全文要么不在**，这条不变式由 ``collect_demos`` 保证。
#:
#: 取值理由：必须**装得进主服务的单条工具结果闸**（`compress.DEFAULT_RESULT_MAX_BYTES`，
#: 60 KB）并给计划行与提示留出余量——原先是 120 KB，回执在主服务那一侧被从中间截掉，
#: 「要么全文要么不在」这条不变式在真机上破了（run d27b37bf1419：20 份 demo 落在被截的
#: 那一段里，模型只能逐件去取）。超线的照旧点名，用 load_skill 逐件取回（每件十几 KB）。
FORAGE_MAX_BYTES = 48_000

#: 取材轮判据的三种结果（外加「不像取材轮」）。做成常量是为了让「三态分清」这件事
#: 在测试里按**集合**断言，而不是在断言里再抄一遍字符串。
FORAGE_MODES = ("forage", "code", "mixed", "none")


def _demo_file(relpath: str) -> Path:
    return _SKILL_DIR / "demos" / str(relpath)


def demo_bytes(relpath: str) -> bytes | None:
    """盘上一份 demo 的原始字节；读不到返回 None（与 ``_demo_shape`` 同一把尺）。"""
    f = _demo_file(relpath)
    if not f.is_file():
        return None
    try:
        return f.read_bytes()
    except OSError:
        return None


def forage_scan(raw: Any) -> tuple[str, list[dict[str, Any]]]:
    """读一遍这次调用的形状，判它是不是**取材轮** → ``(mode, 逐镜清单)``。

    ``mode`` 取 ``FORAGE_MODES`` 之一：

    * ``forage``——**一镜都没交代码**，且至少一镜声明了 ``card``/``derived_from``。
      这就是取材轮：逐镜清单里带 id/card/derived_from，``collect_demos`` 据此取全文。
    * ``code``——每一镜都交了代码。正常的分镜轮，照老路走校验器。
    * ``mixed``——有的交了有的没交。**打回**：两件事（取材 / 出分镜）必须各调一次，
      因为「这一镜的代码怎么还没写」在服务端分不出来是漏填还是想先取材，而猜错的
      代价是整片烧完才现形。
    * ``none``——一镜没交码、也一镜没声明出处。不是取材轮，交给校验器照常报
      「code 为空」（那句话里写着取材轮怎么交）。

    判据刻意只认「有没有 code」这一件事：模型在骨架里多写一句 text、少写一个 highlight
    都不该改变这一次调用的性质。
    """
    shots = raw.get("shots") if isinstance(raw, Mapping) else None
    if not isinstance(shots, (list, tuple)) or not shots:
        return "none", []
    with_code = 0
    without: list[dict[str, Any]] = []
    for i, s in enumerate(shots):
        if not isinstance(s, Mapping):
            return "none", []                      # 形状坏到没法判，交给校验器报
        sid = str(s.get("id") or f"s{i + 1:02d}")
        derived = s.get("derived_from") or []
        if not isinstance(derived, (list, tuple)):
            derived = [derived]
        derived = [str(d).strip() for d in derived if str(d).strip()]
        if str(s.get("code") or "").strip():
            with_code += 1
        else:
            without.append({"id": sid, "card": str(s.get("card") or "").strip(),
                            "derived_from": derived})
    if with_code and without:
        return "mixed", without
    if with_code:
        return "code", []
    if any(row["derived_from"] or row["card"] for row in without):
        return "forage", without
    return "none", []


def collect_demos(plan: list[dict[str, Any]]
                  ) -> tuple[dict[str, Any], list[str]]:
    """取材轮取字节 → ``(取材账, 错误清单)``。错误非空即**没有回任何字节**。

    账的形状（``tests/test_code_forage.py`` 按它断言）：

    - ``demos``：``[{relpath, bytes, sha16, text}]``，**逐字节全文**——``text`` 按 UTF-8
      解码后编码回去必须等于盘上那份文件的字节（这条就是 T20 的判据：注入内容里含
      被引用 demo 的字节）。
    - ``oversize``：超出 ``FORAGE_MAX_BYTES`` 没随本轮回执给出的 ``[{relpath, bytes}]``。
      它们是**没给**，不是**没内容**——所以点名列出，让模型去 ``load_skill(path=…)`` 取。
    - ``demos_bytes``：本轮真给出去了多少字节（``demos`` 之和，不含 ``oversize``）。

    ``relpath`` 走 ``_norm_cite``——与闸⑫/分镜轮同一份白名单；不在册的当场打回，
    **不给字节**（给了就等于承认了一个不存在的出处，模型照着写出来的代码回头必被闸⑫ 打回）。
    **不在册的那几条不再在这里重复报**：``_norm_cite`` 刚逐条报过，而且报得比这里准（它点名了
    盘上真有的几份）。这里只报「**在册却读不到**」——那才是语料缺件，是模型该转告的事。
    """
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    for row in plan:
        sid = str(row.get("id") or "")
        card, derived = _norm_cite(row, sid, errors)
        rows.append({"id": sid, "card": card, "derived_from": derived})

    demos: list[dict[str, Any]] = []
    oversize: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    known = _known_demo_relpaths()
    for row in rows:
        for rel in row["derived_from"]:
            if rel in seen:
                continue
            seen.add(rel)
            data = demo_bytes(rel)
            if data is None:
                # 不在白名单的那几条，`_norm_cite` 刚刚逐条报过，而且报得比这里准（点名了
                # 盘上真有的那几份）。旧代码在这里**又报一次**，论断还是反的——它写着
                # 「在 MANIFEST 里」——于是同一条回执里两句话互相打脸（「不在白名单」与
                # 「在 MANIFEST 里」），模型读成「不是我写错名字，是语料缺件」，
                # 转手把这个不该它解决的问题弹给了用户。不清单里，就别替它下结论。
                if known and rel not in known:
                    continue
                if known:
                    errors.append(
                        f"取材轮：demo {rel!r} 在 MANIFEST 里，盘上却读不到"
                        "——这一份的字节取不回来，取材不算完成（先修语料，别拿记忆补）")
                else:
                    errors.append(
                        f"取材轮：demo {rel!r} 在盘上读不到，这一份的字节取不回来"
                        "——而 demos/MANIFEST.json 也没读出来，说不清它是清单里的还是"
                        "本来就缺件；先把这两样任一对上再调本工具")
                continue
            # 第一份无论多大都照给：一份全文都比「一份都不给」有用，而这个上限
            # 本来就是防「一轮塞太多」，不是防「单件太大」。
            if demos and total + len(data) > FORAGE_MAX_BYTES:
                oversize.append({"relpath": rel, "bytes": len(data)})
                continue
            total += len(data)
            demos.append({
                "relpath": rel,
                "bytes": len(data),
                # sha16 让模型（与逐镜账）能指名「我读的是这一版的这份文件」；
                # 与闸⑬ 给素材算 sha16 是同一套写法，两处不共享代码但共享口径。
                "sha16": hashlib.sha256(data).hexdigest()[:16],
                "text": data.decode("utf-8", "replace"),
            })
    return ({"forage_plan": rows, "forage_demos": demos,
             "forage_bytes": total, "forage_oversize": oversize,
             "forage_max_bytes": FORAGE_MAX_BYTES}, errors)


def structure_pairs(codes: list[str]) -> list[tuple[int, int, float]]:
    """所有可比镜对的骨架相似度（短的镜跳过，见 ``_SHAPE_MIN_TOKENS``）。"""
    shapes = [canonical_shape(c) for c in codes]
    out: list[tuple[int, int, float]] = []
    for i in range(len(codes)):
        if len(shapes[i]) < _SHAPE_MIN_TOKENS:
            continue
        for j in range(i + 1, len(codes)):
            if len(shapes[j]) >= _SHAPE_MIN_TOKENS:
                out.append((i, j, shape_similarity(shapes[i], shapes[j])))
    return out


def structure_groups(codes: list[str], *, threshold: float = STRUCT_SAME
                     ) -> list[list[int]]:
    """把镜序按「相似度 ≥ threshold」并成组（并查集，只返回 ≥2 镜的组）。"""
    parent = list(range(len(codes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j, s in structure_pairs(codes):
        if s >= threshold:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri
    groups: dict[int, list[int]] = {}
    for i, s in enumerate(codes):
        if len(canonical_shape(s)) >= _SHAPE_MIN_TOKENS:
            groups.setdefault(find(i), []).append(i)
    return sorted((sorted(v) for v in groups.values() if len(v) >= 2),
                  key=lambda g: (-len(g), g[0]))


def check_shot_structure(shots: list[dict[str, Any]]
                         ) -> tuple[list[str], list[str]]:
    """闸⑨：跨镜结构雷同。→ (打回清单, 提醒清单)。"""
    errors: list[str] = []
    warns: list[str] = []
    codes = [str(s.get("code") or "") for s in shots]
    ids = [str(s.get("id") or f"s{i + 1:02d}") for i, s in enumerate(shots)]
    if len(codes) < STRUCT_MIN_GROUP:
        return errors, warns
    for group in structure_groups(codes):
        label = "、".join(ids[i] for i in group)
        if len(group) >= STRUCT_MIN_GROUP:
            errors.append(
                f"闸⑨ 跨镜同构：{len(group)} 镜（{label}）的代码骨架一模一样——"
                "剥掉文案字符串与颜色之后，它们的标识符、数字与符号序列完全重合，"
                "也就是**只换了字与色相，没换画法**。这不叫多风格，叫拼贴："
                "让这些镜各用一种自己的母题（手绘 roughjs / 真三维 three / 公式 katex / "
                "几何图示四种里至少三种），并且每镜的版式、时间轴与图形参数都要不同。"
                "技能正文「画面丰富度合同与风格配额」那一节写着四类母题怎么选")
        else:
            warns.append(
                f"结构提醒：{label} 两镜的代码骨架相似度 ≥{STRUCT_SAME:.2f}"
                "（剥掉文案与颜色后几乎逐字相同）。两镜同构可以是同一拍的两个镜头，"
                "第三镜再照抄就是拼贴——出片前看一眼这两帧是不是一回事")
    near = [(i, j, s) for i, j, s in structure_pairs(codes)
            if STRUCT_NEAR <= s < STRUCT_SAME]
    if near:
        warns.append(
            f"结构提醒：{len(near)} 对镜的骨架相似度落在 {STRUCT_NEAR:.2f}~{STRUCT_SAME:.2f}"
            "（实测互不相同的参考实现最高只到 0.74，所以这一档只是可疑不是定罪）："
            + "、".join(f"{ids[i]}↔{ids[j]}" for i, j, _ in sorted(
                near, key=lambda x: -x[2])[:8]))
    return errors, warns


# ---------------------------------------------------------------------------
# 闸⑩ 前景色与底色的对比度（文字与底色同色 → 画面有了但读不出）
# ---------------------------------------------------------------------------

_COLOR_VALUE = (r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})"
                r"""|palettes\[\s*['"][^'"]+['"]\s*\]\s*\.\s*[A-Za-z]+"""
                r"|palette\s*\.\s*[A-Za-z]+|TOKENS\s*\.\s*[A-Za-z]+|[A-Za-z]{3,12}")
#: 承担「底」的角色：整页底、卡片底、面板底。`fill` 留给图形本体，不当底判。
_SURFACE_PROP_RE = re.compile(
    r"\b(?:background|backgroundColor|bgcolor|bg|page|surface|field)\s*[:=]\s*"
    r"""['"{]*\s*(""" + _COLOR_VALUE + r")")
#: 承担「字」的角色：`color:` 一类，加上 `<text>`/`<tspan>` 上的 `fill`（SVG 文字
#: 就靠这一个）。`<path fill=…>` 那种是图形本体的油漆，不参与「读不读得出」的判据。
_INK_PROP_RE = re.compile(
    r"\b(?:color|ink|textColor|foreground)\s*[:=]\s*['\"{]*\s*(" + _COLOR_VALUE + r")"
    r"|\bfill\s*[:=]\s*['\"{]*\s*(" + _COLOR_VALUE + r")")

#: 这些标签上的 `color=` 是材质色与灯光色（立方体、金属度、点光源），不是文字。
_MATERIAL_TAG_RE = re.compile(
    r"(?:material|light|mesh|geometry|sprite|shader|object|primitive|attach)\b", re.I)
_TEXT_TAG_RE = re.compile(r"^(text|tspan)$", re.I)
_TAG_OPEN_RE = re.compile(r"<\s*([A-Za-z][\w.]*)")

_PALETTE_REF_RE = re.compile(
    r"""palettes\[\s*['"]([^'"]+)['"]\s*\]\s*\.\s*([A-Za-z]+)""")
#: `const palette = palettes["deep-ocean"]` 之后，代码里就只剩 `palette.text` 这种写法
#: ——技能正文给的代码模板正是这一条路，认不出它等于这一档闸对模板渲出来的片子永不生效。
_PALETTE_BIND_RE = re.compile(
    r"""\bpalette\s*=\s*palettes\[\s*['"]([^'"]+)['"]\s*\]""")

#: 大字（画面母题的字阶都在 48 px 以上）的 WCAG 下限 3:1，正文 4.5:1。
#: 3:1 以下打回、3:1~4.5:1 只提醒——母版字幕带的颜色归母版，这里只判镜头代码自己写的字。
CONTRAST_MIN = 3.0
CONTRAST_READABLE = 4.5

_NAMED_COLORS = {"white": "#ffffff", "black": "#000000", "red": "#ff0000",
                 "crimson": "#dc143c", "gold": "#ffd700", "ivory": "#fffff0",
                 "beige": "#f5f5dc", "gray": "#808080", "grey": "#808080",
                 "silver": "#c0c0c0", "navy": "#000080", "teal": "#008080",
                 "orange": "#ffa500", "purple": "#800080", "yellow": "#ffff00",
                 "transparent": None}   # 显式认不出：透明底不参与对比


def _master_file(rel: str) -> Path | None:
    """母版侧那份事实文件的位置：容器里是 Dockerfile COPY 进来的 `/app/remotion`，
    本机在仓库根 `remotion/`，也允许 `REMOTION_ROOT` 指过去。

    都没有就返回 None，调用方据此**明说这一档没判**，而不是默默当成通过——
    「读不到就算过」正是本文件一贯要消灭的那种空挂闸。
    """
    env_root = os.environ.get("REMOTION_ROOT")
    cands = []
    if env_root:
        cands.append(Path(env_root) / rel)
    cands += [Path("/app/remotion") / rel,
              Path(__file__).resolve().parents[3] / "remotion" / rel]
    for p in cands:
        if p.is_file():
            return p
    return None


@lru_cache(maxsize=1)
def palettes() -> dict[str, dict[str, str]]:
    """`remotion/src/themes/palettes.json` 的内容（色板的唯一出处，Python 不重列一份）。

    读不到返回空表：那时 :func:`resolve_color` 认不出 `palettes["x"].text` 这种写法，
    闸⑩ 就退化成只判字面 hex，并在事实里记一笔「本片未判」——不装成通过。

    同一个文件里还住着 `preset` 段（三档风格组合，R4-T8-1 从 styles.json 并进来的）。
    它也是 dict，但**不是色板**：放行会让闸⑩ 把 `palettes["preset"].text` 当成一个合法
    颜色写法，而八色板的自检（T9）会数成九个。所以这里按名字剔除，不是按形状猜。
    """
    p = _master_file("src/themes/palettes.json")
    if not p:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and k != "preset"}


@lru_cache(maxsize=1)
def token_colors() -> dict[str, str]:
    """`remotion/src/tokens.ts` 里那几个字面颜色（bg/ink/accent/hl…）。"""
    p = _master_file("src/tokens.ts")
    if not p:
        return {}
    try:
        src = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {k: v for k, v in re.findall(
        r"\b(\w+)\s*:\s*['\"](#[0-9a-fA-F]{3,8})['\"]", src)}


def resolve_color(token: str, default_palette: str | None = None) -> str | None:
    """把一镜代码里的颜色写法换成 `#rrggbb`。

    认得出的写法：字面 hex（三位/四位/六位/八位都展开）、色名白名单、
    `palettes["solar-pop"].text`、`palette.text`（`palette` 是谁由调用方把
    :data:`_PALETTE_BIND_RE` 认出的绑定名传进 ``default_palette``）、`TOKENS.bg`。
    认不出返回 None——**不猜**。
    """
    t = (token or "").strip()
    if t.startswith("#"):
        h = t[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) in (6, 8):
            return "#" + h[:6].lower()
        return None
    low = t.lower()
    if low in _NAMED_COLORS:
        return _NAMED_COLORS[low]
    m = _PALETTE_REF_RE.search(t)
    if m:
        got = (palettes().get(m.group(1)) or {}).get(m.group(2))
        return resolve_color(got, default_palette) if got else None
    if t.startswith("TOKENS"):
        got = token_colors().get(t.rsplit(".", 1)[-1])
        return resolve_color(got, default_palette) if got else None
    if default_palette and re.match(r"^palette\.[A-Za-z]+$", t):
        got = (palettes().get(default_palette) or {}).get(t.split(".", 1)[1])
        return resolve_color(got) if got else None
    return None


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 相对亮度比（1.0~21.0）。两个参数都得是 `#rrggbb`。"""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def lum(hexs: str) -> float:
        r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _enclosing_tag(src: str, pos: int) -> str:
    """位置 `pos` 所在那个 JSX 开标签的标签名（回溯到最近的 `<`）。认不出返回空串。"""
    lt = src.rfind("<", 0, pos)
    if lt < 0:
        return ""
    m = _TAG_OPEN_RE.match(src[lt:])
    return m.group(1) if m else ""


def shot_colors(code: str) -> dict[str, Any]:
    """一镜代码里**静态可认**的底色与字色（认不出的计数，不猜值）。"""
    src = str(code or "")
    bind = _PALETTE_BIND_RE.search(src)
    palette_name = bind.group(1) if bind else None
    surfaces: list[str] = []
    inks: list[str] = []
    unresolved = 0

    def add(bucket: list[str], got: str | None) -> None:
        nonlocal unresolved
        if got:
            if got not in bucket:
                bucket.append(got)
        else:
            unresolved += 1

    for m in _SURFACE_PROP_RE.finditer(src):
        add(surfaces, resolve_color(m.group(1), palette_name))
    for m in _INK_PROP_RE.finditer(src):
        token, start = m.group(1) or m.group(2), m.start(1) if m.group(1) else m.start(2)
        tag = _enclosing_tag(src, start)
        if _MATERIAL_TAG_RE.search(tag):
            continue          # `<meshStandardMaterial color=…>` 是立方体的颜色，不是字
        if token is None:
            continue
        if m.group(2) and not _TEXT_TAG_RE.match(tag):
            continue          # fill 只在 <text>/<tspan> 上算字色
        add(inks, resolve_color(token, palette_name))
    return {"surfaces": surfaces, "inks": inks, "unresolved": unresolved,
            "palette": palette_name}


def check_ink_contrast(shots: list[dict[str, Any]]
                       ) -> tuple[list[str], list[str], list[str]]:
    """闸⑩：镜头代码自己写的字色，在它自己写的每一种底色上是否都读得出来。

    判据是「**最好的一档也低于 3:1 才打回**」：`#ffffff` 的字在深卡片上是对的、
    在浅页面上是错的，而那种片子里卡片就是它的底，不该拦。真正拦下的是「这一镜
    写了一种字色，而画面里所有可能的底都把它糊掉了」——2026-10-11 那四条片子上的
    「米白底 + 奶黄字」「深睡 → 生长激素」正是这一类。

    与闸④ 的分工：闸④ 在像素上判「带子里有没有东西」（糊成一片的浅色字在
    非底色占比那里是**有**东西的），这一条判「那点东西读不读得出来」。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    judged = 0
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        c = shot_colors(str(s.get("code") or ""))
        if not c["surfaces"] or not c["inks"]:
            continue                      # 底或字认不出：不判（末段统一交代未判）
        judged += 1
        for ink in c["inks"]:
            best_bg = max(c["surfaces"], key=lambda bg: contrast_ratio(ink, bg))
            best = contrast_ratio(ink, best_bg)
            if best < CONTRAST_MIN:
                errors.append(
                    f"闸⑩ 文字对比度：{sid} 的字色 {ink} 在这一镜写出的每一种底色上"
                    f"最好也只有 {best:.1f}:1（底色：{'、'.join(c['surfaces'])}），"
                    f"低于大字下限 {CONTRAST_MIN:.0f}:1——画面上有字而观众读不出。"
                    "换字色或换底色：底色走色板的 `page`/`surface`，字色走 `text`/"
                    "`muted`，别把 `accent` 当正文色写在浅底上")
            elif best < CONTRAST_READABLE:
                warns.append(
                    f"对比度提醒：{sid} 的字色 {ink} 在最好的底色 {best_bg} 上只有 "
                    f"{best:.1f}:1（大字勉强可读，正文偏暗，达标线 4.5:1）")
    if judged == 0 and shots:
        # ★ 这一条走的是**第三个出口**（`undetermined`），不是 `warns`——T28 判过的：
        #   它在提醒清单里待过一阵子，而提醒在回执上的读法是「不打回、只是顺口一提」，
        #   于是「本片这一档压根没量过」被读成了「量过、没问题，只提醒一下」。
        #   未判与通过在同一格 = D-未判不绿 说的那件事（§0.5-P5）。
        undetermined.append(
            "对比度这一档本片**未判**：镜头代码里的底色或字色没能静态认出来"
            "（写成变量、渐变、运行时算出来的颜色，或者容器里读不到 palettes.json）。"
            "未判不是通过。")
    return errors, warns, undetermined


#: 自动修字色时的两个候选：白与近黑。不取色板里的 `text`——那正是这类错的来源
#: （深色板的 text 是浅色，模型把它写到浅底上就糊了）。
_AUTOFIX_INKS = ("#ffffff", "#141414")


def autofix_ink_contrast(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """闸⑩ 的机械修复：读不出的字色就地换成白或近黑（哪个在这一镜的底上更清楚换哪个）。

    → (修过的 spec 副本, 逐镜说明)。只动**字色那几个位置**（与闸⑩ 认字色是同一套规则：
    `color:` 一类、`<text>`/`<tspan>` 上的 `fill`，材质色不算），底色与图形的填色一个不碰。
    为什么由系统修而不是打回：这是纯算术——真机 run d27b37bf1419 为了「浅底白字 / 深底深字」
    来回改了三轮、每轮重交 220 KB 代码，最后整条通道被熔断；而正确答案机器一步就算得出。
    """
    shots = raw.get("shots")
    if not isinstance(shots, list):
        return dict(raw), []
    notes: list[str] = []
    out: list[Any] = []
    for i, shot in enumerate(shots):
        if not isinstance(shot, Mapping) or not shot.get("code"):
            out.append(shot)
            continue
        code = str(shot["code"])
        c = shot_colors(code)
        if not c["surfaces"] or not c["inks"]:
            out.append(shot)
            continue
        bad: dict[str, str] = {}
        for ink in c["inks"]:
            if max(contrast_ratio(ink, bg) for bg in c["surfaces"]) >= CONTRAST_MIN:
                continue
            fix = max(_AUTOFIX_INKS,
                      key=lambda cand: max(contrast_ratio(cand, bg) for bg in c["surfaces"]))
            if max(contrast_ratio(fix, bg) for bg in c["surfaces"]) >= CONTRAST_MIN:
                bad[ink] = fix
        if not bad:
            out.append(shot)
            continue
        palette_name = c["palette"]
        spans: list[tuple[int, int, str]] = []
        for m in _INK_PROP_RE.finditer(code):
            grp = 1 if m.group(1) else 2
            token = m.group(grp)
            if token is None:
                continue
            start, end = m.start(grp), m.end(grp)
            tag = _enclosing_tag(code, start)
            if _MATERIAL_TAG_RE.search(tag):
                continue
            if grp == 2 and not _TEXT_TAG_RE.match(tag):
                continue
            got = resolve_color(token, palette_name)
            if got not in bad:
                continue
            quoted = start > 0 and code[start - 1] in "'\"`"
            spans.append((start, end, bad[got] if quoted else f'"{bad[got]}"'))
        if not spans:
            out.append(shot)
            continue
        for start, end, rep in sorted(spans, reverse=True):
            code = code[:start] + rep + code[end:]
        sid = str(shot.get("id") or f"s{i + 1:02d}")
        notes.append(f"{sid}：字色在本镜底色上读不出，已自动换成 "
                     + "、".join(f"{k}→{v}" for k, v in bad.items()))
        out.append({**shot, "code": code})
    return {**raw, "shots": out}, notes


#: 整片层的八道判据（闸⑦ 风格配额 / 闸⑲ 风格丰富度 / 闸⑫ 与 demo 骨架同构 /
#: 闸⑳ 原创配额 / 闸⑨ 跨镜同构 / 闸⑩ 文字对比度 / 闸⑮ 版式族配额 / 闸⑯ 主体具名）。
#: **三处调用点**（plan_motion 入库前、render 烧像素前、patch 落盘前）与**两处报账**
#: （计划卡、出片回执）都从这一个元组取，规则因此不可能只在某一路上生效；这也是
#: 「清单只有一个」那条纪律落在这一族判据上的样子（同一形状的清单漂移踩过三次，
#: 见 §9-Q90/Q91）。
#:
#: ⑦ 与 ⑲ 是**一对**，别把它们的职责搞混：⑦ 管**反雷同**（画法通道别只一种、
#: 纯文字镜别超三分之一），⑲ 管**逼丰富**（色板 / 满幅 / 质感 / 底色这四条
#: 在此之前没有任何闸在判）。⑫ 与 ⑳ 同理：⑫ 判「声明派生就要真像」，⑳ 判
#: 「走原创的那几镜别超过 20%」。
WHOLE_FILM_GATES = (check_style_quota, check_style_variety,
                    check_derived_similarity, check_original_share,
                    check_shot_structure, check_ink_contrast,
                    check_layout_family, check_subject_named)

#: 只提醒、不打回的那几道（2026-10-15）：色板多样⑲、卡族多样⑮、原创占比⑳。
#: 它们逼的是「多样」，但同时把「整片统一风格 / 统一色调 / 更多原创镜」这类有意为之的
#: 选择一并打回，换来的是整份分镜重写重提。降为建议后照样出现在渲染前的计划确认卡上
#: （style_warnings），由用户在那一眼里决定要不要改。
#: ⑦（画法通道 + 纯文字镜占比）**仍是硬闸**：它挡的是「一整条 PPT 配旁白」这种质量
#: 底线问题，不是风格偏好。对比度、结构、主体具名同样仍是硬闸。
SOFT_GATES = frozenset({check_style_variety, check_layout_family, check_original_share})
#: 预览档（qc=preview）再多放软的四道「品味型」契约闸：画法通道⑦、跨镜同构⑨、
#: 派生相似⑫、主体具名⑯。预览档的前提是**渲染前有人看静帧**（计划确认卡上的预览图），
#: 人眼替掉了这几道机器判据；无人值守的严格档（qc=strict）照旧硬拦。
#: 对比度⑩ 与结构合法性在两档都是硬闸。
PREVIEW_SOFT_GATES = SOFT_GATES | frozenset({check_style_quota, check_shot_structure,
                                             check_derived_similarity, check_subject_named})
SOFT_PREFIX = "（建议）"

#: 质检档：preview＝主流做法（渲染前人看预览静帧，渲后像素闸只提醒、不打回重烧）；
#: strict＝无人值守（机器判据全部硬拦，换「没人看也敢出片」的保证）。
#: 库层缺省是 strict（既有判据语义不变）；产品节点按 `default_qc()` 注入（缺省 preview）。
QC_PREVIEW = "preview"
QC_STRICT = "strict"
QC_MODES = (QC_PREVIEW, QC_STRICT)


def default_qc() -> str:
    """产品入口的质检档：环境变量 MOTION_QC（preview/strict），缺省 preview。"""
    v = os.environ.get("MOTION_QC", QC_PREVIEW).strip().lower()
    return v if v in QC_MODES else QC_PREVIEW


def with_default_qc(raw: Mapping[str, Any]) -> dict[str, Any]:
    """spec 副本；没写 qc（或写了空值）时补上产品缺省档。"""
    out = dict(raw)
    if not str(out.get("qc") or "").strip():
        out["qc"] = default_qc()
    return out


def run_film_gate(gate: Any, shots: list[dict[str, Any]], *, strict: bool = True) -> tuple:
    """跑一道整片闸；软闸的打回项降为提醒（加「（建议）」前缀），其余原样返回。"""
    got = gate(shots)
    if gate not in (SOFT_GATES if strict else PREVIEW_SOFT_GATES):
        return got
    return ([], [f"{SOFT_PREFIX}{e}" for e in got[0]] + list(got[1]), *got[2:])


def whole_film_gate_results(shots: list[dict[str, Any]], *, strict: bool = True
                            ) -> tuple[list[str], list[str], list[str]]:
    """把整片八道闸的三个出口合成一份：``(打回, 提醒, 未判)``。

    **三个出口是这一条的全部意思**（T28）：一道闸说不清的时候，它的答案既不是
    「过」也不是「不过」，而是「没判」——三者必须落在三个不同的格子里，因为回执、
    计划卡与模型读的都是格子名。判据自己带第三个出口时就用它；还没带第三态的那些
    闸按两态处理（``len(got) > 2`` 那一下），不逼它们假装有未判档。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    for gate in WHOLE_FILM_GATES:
        got = run_film_gate(gate, shots, strict=strict)
        errors.extend(got[0])
        warns.extend(got[1])
        if len(got) > 2:
            undetermined.extend(got[2])
    return errors, warns, undetermined


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
#:
#: R7-T22 的四条（转场 + 进出场）也在这里，理由同 ``texture``：**整片一个决定**。
#: 「第 3 镜和第 4 镜之间用圆开、其余硬切」不是没有意义的诉求，但它是另一件事
#: （逐接缝的转场表），今天没有这个需求；而进出场天生是母版给每一镜套的同一套动作。
#: 它们都在计划卡上（用户在批准之前就看得到「镜间怎么接、每镜怎么进」），
#: 而卡面的每一项都要能反查到来源 + 有中文标签，所以档位全是枚举（见 TRANSITION_SEC_CHOICES）。
KNOB_FIELDS = ("aspect", "fps", "narration", "voice", "rate", "subtitle_mode",
               "caption_position", "caption_anim",
               "texture", "transition", "transition_sec", "entrance", "exit")

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
    # 母版 <FilmLayout> 只认这四档；旧链的 torn / bottom 随模板一起消失。
    # `inline` 是唯一「文字进画面」的档（母版不画，交回镜头代码）——它的标签必须
    # 说清这一点，不然选的人会以为它和 `none` 一样是「不上字幕」。
    "subtitle_mode": {"torn_highlight": "撕纸条·逐词点亮", "full": "整句字幕",
                      "none": "不上字幕",
                      "inline": "文字进画面（母版不画，随构图摆）"},
    "caption_position": {"bottom": "底部字幕条", "center": "压在画面正中",
                         "top": "顶部安全区"},
    "caption_anim": {"fade": "淡入", "rise": "上浮入场", "pop": "弹出放大",
                     "karaoke": "逐字随语音点亮"},
    # 质感每一镜都能带一档（顶层是整片默认），标签要说清「加的是哪种观感」，不然选的人只能猜
    "texture": {"none": "不加（原片直出）", "film": "老胶片·颗粒与暗角",
                "tv": "老电视·扫描线与色偏", "glow": "柔光·高光处泛开",
                "bleach": "漂白·硬对比低饱和"},
    # R7-T22：转场与进出场。措辞要说清「观众看到的是什么」，而不是抄 ffmpeg 的档名
    # （wipeleft 这三个字母对人没有意义）。四条都在确认卡与计划卡两张卡上出现。
    "transition": {"none": "硬切（镜镜直接接）", "fade": "淡叠·前一镜淡出后一镜淡入",
                   "dissolve": "溶解·两边互相化开",
                   "wipeleft": "左擦·新画面从右往左推过来",
                   "wiperight": "右擦·新画面从左往右推过去",
                   "slideup": "上滑·新画面自下而上顶入",
                   "circleopen": "圆开·从中心扩开"},
    "transition_sec": {"0.2": "0.2 秒·一闪而过", "0.4": "0.4 秒·轻叠（默认）",
                       "0.6": "0.6 秒·看得出", "0.9": "0.9 秒·很缓"},
    # 同一个取值在两张表里是**互为反向**的一件事：入场是「进来」，出场是「出去」。
    # 共用一张表会把出场写成「自下浮入」——那正是确认卡上最容易被忽略的一种错话。
    "entrance": {"none": "直接开始（不动作）", "fade": "淡入",
                 "rise": "自下浮入", "drop": "自上落入",
                 "zoom": "轻推近·由小放大入画"},
    "exit": {"none": "直接结束（不动作）", "fade": "淡出",
             "rise": "向上浮出", "drop": "向下落出",
             "zoom": "轻拉远·缩小出画"},
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
            "description": "字幕形态。inline=母版不画、文字交回镜头代码排进画面",
        },
        "caption_position": {
            "type": "string", "enum": list(CAPTION_POSITIONS), "default": "bottom",
            "description": "字幕位置（整片默认，每镜可覆盖）：底部/画面正中/顶部",
        },
        "caption_anim": {
            "type": "string", "enum": list(CAPTION_ANIMS), "default": "fade",
            "description": "字幕入场动画（整片默认，每镜可覆盖）；karaoke=逐字随语音点亮",
        },
        "texture": {
            "type": "string", "enum": list(TEXTURES), "default": "none",
            "description": "整片默认质感档；每一镜可用自己的 texture 覆盖它。换它不重烧镜头",
        },
        # R7-T22 的四条。description 收成一行、≤60 字（`knob_facts` 的 note 取前 60 字，
        # 超了会截在半句话上），「怎么选」那套教程在技能正文里。
        "transition": {
            "type": "string", "enum": list(TRANSITION_STYLES), "default": "none",
            "description": "镜与镜之间怎么接（拼接层做，不重烧任何一镜）",
        },
        "transition_sec": {
            "type": "string", "enum": list(TRANSITION_SEC_CHOICES),
            "default": TRANSITION_SEC_DEFAULT,
            "description": "转场叠多久，四档；须短于相邻较短一镜的一半",
        },
        "entrance": {
            "type": "string", "enum": list(SHOT_MOTION_STYLES), "default": "none",
            "description": "每一镜怎么进（母版按 token 画，动作收在该镜前 10% 内）",
        },
        "exit": {
            "type": "string", "enum": list(SHOT_MOTION_STYLES), "default": "none",
            "description": "每一镜怎么出（母版按 token 画，动作收在该镜后 10% 内）",
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
    "react", "react-dom", "remotion",
    "@remotion/paths", "@remotion/shapes",
    "@remotion/motion-blur", "@remotion/layout-utils",
    # R5-T16：官方写法块点名的四件 + 字幕 token 化的那件。**顺序与
    # lint-rules.mjs 的 ALLOWED_IMPORTS 逐字相同**（两侧名单由 test_code_spec 第 ⑤ 节
    # 对账；fonts 治的是第六根因「硬写字族 → 静默回落」）。
    "@remotion/fonts", "@remotion/captions", "@remotion/rough-notation",
    "@remotion/noise", "@remotion/animation-utils",
    # R5-T16b：官方效果件。入场券是 `remotion/tests/effects-probe.mjs` 那三条真渲
    # 判据（不崩 / 参数外没有别的颜色 / 挂错宿主是静默空操作）。**只对
    # `<Img>/<Solid>/<HtmlInCanvas>` 生效**，写在别处是一声不响的空操作。
    #
    # 按**子路径逐件**列，不放行包名 `@remotion/effects`（§9-Q89）：4.0.534 的桶文件
    # 只重导 11 件，`paper`/`halftone` 都不在里面——写包名会放行一条**打包期不报错、
    # 渲染期才炸**的 import（`import { paper } from '@remotion/effects'` 拿到 undefined）。
    "@remotion/effects/paper", "@remotion/effects/halftone",
    # 画法通道：手绘斜纹与真三维。这两族在 package.json + lockfile 里躺着，
    # 2026-10-11 那批六支真渲夹具（roughjs / three / KaTeX × 两画幅）就是它们渲的。
    "three", "@react-three/fiber", "@remotion/three", "roughjs",
    "katex", "katex/dist/katex.min.css",
    # 母版与 token 走别名；镜头代码只能拿到这两个文件级入口
    "@dg/film", "@dg/tokens",
    # webfont 注册表（R5-T16）。import 它就是当场把那 101 片注册进 document.fonts；
    # 镜头代码写 `@dg/fonts` 导出的字族名才算「这个字族真有人加载」。
    "@dg/fonts",
    # 参考实现的动效件（_fixtures 是真实现，不是桩）。**只列文件级路径**：
    # 裸目录名（`@dg/lib`、`@dg/themes`、`@dg/demos`、`@dg/fixtures`）那些目录里
    # 没有 index 文件，Remotion 打包器的 resolve.extensions 不认目录——名单里写着它，
    # 闸① 放行、打包期才炸，而炸的是一句「解析失败」而不是一句人话。
    "@dg/motion", "@dg/polish", "@dg/brand",
    "@dg/fixtures/Motion", "@dg/fixtures/Polish",
    "@dg/fixtures/Brand", "@dg/fixtures/Fixtures", "@dg/fixtures/Look",
    "@dg/fixtures/PageCam2D",
    "@dg/lib/Caption", "@dg/lib/ClipCard", "@dg/lib/DigitRoll",
    "@dg/lib/FlashCut", "@dg/lib/PageCam", "@dg/lib/VerticalTicker",
    "@dg/lib/FlatPanel",
    "@dg/lib/helpers/motion", "@dg/lib/helpers/rand", "@dg/lib/helpers/shake",
    "@dg/lib/helpers/camera",
    "@dg/themes/palettes.json",
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
    "**写法纪律（画面带坐标系 `useBody()`、母题铺满带子、四条画法通道与 seed、"
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
    "· 画法按题材选，不是装饰。四条**画法通道**：手绘斜纹 `roughjs`、真三维 `three` + "
    "`@react-three/fiber` + `@remotion/three`、公式 `katex`（+ 它的样式表）、几何与图示"
    "（裸 `<svg>` 或 `@remotion/shapes`）。闸⑦ 数的是整片用了几种通道，而**只 import "
    "一行而代码体里没用那个名字**不算用了这一条\n"
    "· 整片层还有两道也在烧像素之前判的闸：闸⑨ 不许三镜以上的代码骨架一模一样"
    "（剥掉文案字符串与颜色之后逐字相同＝只换了色相，那叫拼贴），闸⑩ 不许字色在"
    "这一镜写出的每一种底色上都低于 3:1（画面有字而观众读不出）\n"
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
            "qc": {"type": "string", "enum": list(QC_MODES),
                   "description": "质检档：preview（缺省，渲染前人看预览静帧，渲后像素闸只提醒）/ "
                                  "strict（无人值守，机器判据全部硬拦）"},
            "beat_sync": {"type": "boolean", "default": True,
                          "description": "有配乐时把镜头切点卡到拍上（只往后补、最多约半拍）"},
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
                    "required": ["code", "text", "card", "derived_from", "subject"],
                    "properties": {
                        "id": {"type": "string",
                               "description": "镜号，省略则按顺序补 s01/s02…"},
                        "code": {"type": "string", "maxLength": MAX_CODE_BYTES,
                                 "description": _CODE_DOC},
                        "code_from": {"type": "string",
                                      "description": "复用另一镜的代码（填那一镜的 id，本镜不写 code），"
                                                     "只换 text / fields / 色板。长片用它：十几种版式"
                                                     "撑几十镜，不必每镜交一整段代码"},
                        "text": {"type": "string", "maxLength": MAX_TEXT_CHARS_WIDE,
                                 "description": f"这一镜要说的一句话，只进母版字幕带。"
                                                f"容量按画幅：竖屏 ≤{MAX_TEXT_CHARS} 字、"
                                                f"横屏 ≤{MAX_TEXT_CHARS_WIDE} 字"
                                                "（超了拆镜，不裁句子）"},
                        "card": {"type": "string",
                                 "description": "选了哪张 shot 配方卡，格式 <类别>/<卡名>，"
                                                "如 opening/fracture。必须命中卡库（见 shots/INDEX.md）"},
                        "derived_from": {"type": "array", "items": {"type": "string"},
                                         "description": "代码来自哪些 demo 的 relpath 列表，"
                                                        "如 [\"opening/fracture/Fracture.tsx\"]"
                                                        "（相对 demos/ 的路径，与 "
                                                        "demos/MANIFEST.json 里的写法一致；"
                                                        "带 demos/ 前缀的写法也收，会归一）。"
                                                        "每个都必须命中该白名单"},
                        # 闸⑯ 的两栏（R5-T15）。subject 进 required：它是「这一镜画的是
                        # 什么东西」那句白纸黑字的话，缺了这一格，整片就退回「卡片+文字」
                        # 那个在评审里被驳回过的形态而我们事后判不出来。subject_channel
                        # 不进 required——纯文字/纯色块镜（⑦ 允许的 ≤1/3）**不许**填它，
                        # 填了就是自报无证据，所以它的必填性由 check_subject_named 按
                        # 「这一镜代码里有没有画法通道」逐镜定，schema 这一层写不出来。
                        "subject": {"type": "string", "maxLength": SUBJECT_MAX_CHARS,
                                    "description": "这一镜画面上**能被认出的那个东西**"
                                                   "（名词短语，2~24 字），如「活字盘」"
                                                   "「三条等高线」「一张 1900 年的地图」。"
                                                   "不许写抽象词（理念/趋势/意义/文化…"
                                                   "既画不出来也认不出来）"},
                        "subject_channel": {"type": "string", "enum": list(SUBJECT_CHANNELS),
                                            "description": "主体是用哪条画法通道画的"
                                                           "（rough/three/katex/vector/image）。"
                                                           "必须与代码里真用上的通道一致"
                                                           "（闸⑯ 按代码对账）；这一镜只有"
                                                           "文字与色块时**不要填**"},
                        "assets": {"type": "array",
                                   "description": "本镜引用的素材资产。每个声明 name/sha256/ext/source/alpha/layer。"
                                                  "代码里用 staticFile(\"<name>\") 引用，lint 做双向对账。"
                                                  "sha256 是全 64 位十六进制——渲染前按 sha256 从 asset_library 取字节、"
                                                  "投到渲染容器 public/assets/<本镜镜号>/<name>",
                                   "items": {"type": "object",
                                             "required": ["name", "sha256", "ext"],
                                             "properties": {
                                                 "name": {"type": "string"},
                                                 "sha256": {"type": "string",
                                                            "description": "完整 sha256（64 位十六进制）"},
                                                 "ext": {"type": "string"},
                                                 "source": {"type": "object"},
                                                 "alpha": {"type": "boolean"},
                                                 "layer": {"type": "string", "enum": ["fg", "bg", "cutout"]},
                                             }}},
                        "highlight": {"type": "array", "items": {"type": "string"},
                                      "maxItems": MAX_HIGHLIGHTS_PER_SHOT,
                                      "description": "逐词点亮的词，每个都必须是 text 的子串，"
                                                     "每镜 ≤4 个；差一个字画面上永远不亮"},
                        # 闸㉑（R8-T33）。**不进 required**：这一格描述的是「你已经写好的
                        # 动效各为谁动」，而 schema 的 required 是「这条请求缺了就不合法」
                        # ——把没写当不合法，会把「这一镜压根没有动效」与「有动效却不说为
                        # 谁动」压成同一个错（前者由闸⑭ 按像素判、后者由 ㉑ 判，两句话）。
                        # 所以缺了这一栏时 ㉑ 走闸后：像素数得出 ≥2 段而一条都没申报 → 打回。
                        "motion": {
                            "type": "array", "maxItems": MAX_FIELDS_PER_SHOT,
                            "description": "这一镜的逐个动效事件，按发生顺序。每条说清"
                                           "**它服务哪条数据**——「有没有意义」判不了，"
                                           "「为哪条数据动」判得了：闸㉑ 会拿它和像素上"
                                           "数出来的那几段一一对上，再看触发帧在不在"
                                           "所锚数据**首次可读**的 ±"
                                           f"{MOTION_ANCHOR_TOLERANCE_DIV} 分之一个"
                                           "秒内。anchors 三种写法（都从 1 起）："
                                           "`fields.<本镜声明过的字段名>` / "
                                           "`subtitle:<本镜旁白的第几个词>` / "
                                           "`beat:<第几个节拍>`。**空数组等于这条动效"
                                           "不为任何数据服务**，闸前直接打回",
                            "items": {
                                "type": "object", "required": ["kind", "anchors"],
                                "properties": {
                                    "kind": {"type": "string",
                                             "description": "这是什么动效，如 标题浮入 / "
                                                            "数字滚动 / 光标指向"},
                                    "anchors": {
                                        "type": "array", "minItems": 1,
                                        "maxItems": MOTION_MAX_ANCHORS,
                                        "items": {"type": "string"},
                                        "description": "它服务的数据，每条动效都必须有；"
                                                       "每一个锚都得在触发帧的窗口内，"
                                                       "所以别把整句话的词都挂上来"},
                                }}},

                        # 闸㉘（R12-T59）。**挂在这一族里**（与 `motion` 并排，不另起
                        # 一个「元素申报」的顶层口）：两者回答的是同一个问题的两侧——
                        # `motion` 说「这条动效为哪条数据动」，`reveal` 说「这一块画面
                        # 什么时候揭」。分成两个口，模型迟早会只写一个。
                        #
                        # **不进 required**：这一族风格（分区揭示）是可选画法，绝大多数
                        # 片子不写它。真写了的那一镜，㉘ 逐条判它的几何与时序（判据在
                        # `check_reveal_regions`，一次请求都不发就判完）。
                        "reveal": {
                            "type": "object",
                            "description": "**分区揭示**：把画面当纸，一块一块按顺序画出来"
                                           "（白板讲解风）。只在你要这一镜真的分块揭示时写，"
                                           "不写＝这一镜不走这一族。写了就要写全三样："
                                           "`canvas`（插画原图的像素尺寸，区域坐标以它为"
                                           "单位）、`scene_duration_ms`（这一幕总长＝末段"
                                           "结束＋停留，停留 ≤ "
                                           f"{REVEAL_HOLD_MAX_MS} ms）、`regions[]`。"
                                           "闸㉘ 判：整数像素坐标、区域不出 canvas、"
                                           "sequence 从 1 连续不重复、幕内 start_ms 两两"
                                           "不重叠、幕时长等式成立、protected 指得着；"
                                           "闸㉗ 再拿真帧比色判「未开始的区域是不是还是"
                                           "纸底」",
                            "required": ["canvas", "scene_duration_ms", "regions"],
                            "properties": {
                                "canvas": {
                                    "type": "object", "required": ["w", "h"],
                                    "description": "插画**原图**的像素尺寸——区域坐标的"
                                                   "坐标系。必须与你 `assets` 里那张图的真"
                                                   "尺寸一致（渲染前按库里那一行核对，"
                                                   "对不上直接退回）",
                                    "properties": {"w": {"type": "integer"},
                                                   "h": {"type": "integer"}}},
                                "scene_duration_ms": {
                                    "type": "integer",
                                    "description": "这一幕的总长（毫秒）＝最后一段结束 + "
                                                   "停留。这个等式是 ㉘ 的一条判据："
                                                   "账上的时长要能一眼算出来"},
                                "regions": {
                                    "type": "array", "minItems": 1,
                                    "description": "按揭示顺序的一块块元素。"
                                                   "一块＝画面上一个矩形区域，"
                                                   "按 direction 推进地把纸画满",
                                    "items": {
                                        "type": "object",
                                        "required": ["id", "sequence", "region",
                                                     "direction", "start_ms",
                                                     "duration_ms"],
                                        "properties": {
                                            "id": {"type": "string",
                                                   "description": "本镜内唯一的名字"
                                                                  "（顺序、protected、回执"
                                                                  "三处都按它指）"},
                                            "label": {"type": "string",
                                                      "description": "这一块画的是什么"
                                                                     "（给人看的一句话）"},
                                            "sequence": {"type": "integer",
                                                         "description": "第几块，从 1 起、"
                                                                        "连续、不重复"},
                                            "narrative_role": {
                                                "type": "string",
                                                "description": "它在叙事里干什么"
                                                               "（建立 / 对比 / 结论…）"},
                                            "subtitle_ref": {
                                                "type": "integer",
                                                "description": "这一块与旁白第几个词对齐"
                                                               "（从 1 起，与 "
                                                               "`subtitle:<i>` 锚同一套"
                                                               "序号）"},
                                            "region": {
                                                "type": "object",
                                                "required": ["x", "y", "w", "h"],
                                                "description": "画在哪：**整数像素**，"
                                                               "坐标系是 `canvas`"
                                                               "（半像素不收——不同画幅下"
                                                               "取整方向不同，两处一取整"
                                                               "就成了两把尺）",
                                                "properties": {
                                                    "x": {"type": "integer"},
                                                    "y": {"type": "integer"},
                                                    "w": {"type": "integer"},
                                                    "h": {"type": "integer"}}},
                                            "direction": {
                                                "type": "string",
                                                "enum": list(REVEAL_DIRECTIONS),
                                                "description": "这一块往哪个方向推进："
                                                               "ltr 左→右 / rtl 右→左 / "
                                                               "ttb 上→下 / btt 下→上 / "
                                                               "whole 整块一次显示"},
                                            "start_ms": {"type": "integer",
                                                         "description": "这一块从这一幕的"
                                                                        "第几毫秒开始画"},
                                            "duration_ms": {
                                                "type": "integer",
                                                "description": "画这一块用多少毫秒"},
                                            "protected": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "这一块**不许覆盖**的"
                                                               "其它块 id——画到它们头上"
                                                               "就说明顺序或坐标错了"
                                                               "（㉗ 的允许掩码会把它们"
                                                               "扣掉）"},
                                        }}},
                            }},
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
                        # R14 版式档（每镜，与 texture 同族，但改的是**构图**不是滤镜）。
                        # 为什么要有这两栏：不写 `bleed` 时每一镜都被裁进同一条居中的
                        # 画面带里（左右留 gutter、上下留安全区），二十镜二十个母题共用
                        # 同一个框——这正是成片「像 PPT」的结构原因。`bleed` 让一镜真的
                        # 画满整帧，`bg` 让一镜有自己的底色，两者都省略＝今天的行为。
                        #
                        # R19 把「写不写」从**建议**改成**有闸在判**（闸⑲ 风格配额）：
                        # 一镜都不满幅会被打回，用深色板的镜不给 `bg` 会出一条提醒。
                        # 措辞必须与技能正文「版式档」那一节同源——正文是模型真正读到的
                        # 那一份，schema 的散文送不到它那里（见 test_code_spec ⑦ 节）。
                        #
                        # `bleed` 必须是布尔：母版判的是 `=== true`（`resolveFilmStyle`），
                        # 写成 `"true"` / `1` / `"yes"` 会静默落成「不满幅」——那正是这条链
                        # 最该避免的失败模式，所以校验期就当场打回（见 `_norm_shot`）。
                        "bleed": {"type": "boolean",
                                  "description": "**满幅**：画面带＝整帧，去掉左右页边距与"
                                                 "上下安全区。只在 `true` 时生效（写成"
                                                 "字符串或数字母版认不出）。**省略＝常规版式**"
                                                 "（有安全区）。**这是纪律不是可选项**："
                                                 "闸⑲ 要求整片至少 ceil(镜数/5) 镜满幅"
                                                 "（20 镜 → 4 镜），一镜都没有会被打回——"
                                                 "不写它就等于把每一镜都裁进同一个居中框，"
                                                 "那正是成片像 PPT 的结构原因。"
                                                 "**用深色板的镜必须同时给 `bg`**"
                                                 "（不给的话深色正文会压在母版奶油底上，"
                                                 "而闸④ 拿不到基准色就拒绝判空）。"
                                                 "满幅镜的字幕带会贴底（不再居中），母版按底色"
                                                 "亮度自动换字色"},
                        "bg": {"type": "string",
                               "description": "这一镜的**底色**（`#rgb` 或 `#rrggbb`，母版归一"
                                              "到 6 位）。**省略＝用母版的 `TOKENS.bg`**。只收"
                                              "十六进制：母版拿它当底色、闸④ 还拿它当"
                                              "「画面是不是空的」基准色，`rgb()`/命名色/"
                                              "渐变一律不收（收了等于让闸④ 在这一镜上闭嘴）。"
                                              "深底会让字幕带自动换成浅色字，不必自己管字幕。"
                                              "**满幅镜（`bleed: true`）必须给**——"
                                              "这也是闸⑲ 那条提醒判的东西"},
                        # 层E1/E2（R19）：两条**逐镜**的覆盖栏。默认都跟整片档，
                        # 所以「不写」与「从前」逐字节一致；写了的镜在缓存键上要进一项
                        # （字幕档是母版画进切片的；转场不进键——它跑在拼接那一趟）。
                        "transition": {"type": "string",
                                       "enum": list(TRANSITION_STYLES),
                                       "description": "**这一镜到下一镜**那一处接缝的转场"
                                                      "（可省略＝跟整片 `transition`）。"
                                                      "最后一镜写它无效（后面没有接缝）。"
                                                      "用途：整片统一淡入，但在某两镜之间"
                                                      "换成圆开 / 擦除——专业动效里转场本来就是"
                                                      "**逐接缝**的，不是整片一个档。"
                                                      "时长沿用整片 `transition_sec`。"
                                                      "**换它不重烧任何一镜**：转场跑在拼接"
                                                      "那一趟，逐镜切片一个字节都不动"},
                        "subtitle_mode": {"type": "string",
                                          "enum": list(SUBTITLE_MODES),
                                          "description": "**这一镜**的字幕形态"
                                                         "（可省略＝跟整片 `subtitle_mode`）。"
                                                         "用途：整片走 `torn_highlight`，但某几镜"
                                                         "要 `inline`（文字进画面、随构图摆）或"
                                                         "要 `none`（整屏插画，不想压字）。"
                                                         "**它与整片那一档都会进缓存键**"
                                                         "（字幕带是母版画进切片里的）"},
                        "caption_position": {"type": "string",
                                             "enum": list(CAPTION_POSITIONS),
                                             "description": "**这一镜**的字幕位置（可省略＝跟整片）。"
                                                            "主体在下半屏时放 top，整屏插画留白在中间时放 center"},
                        "caption_anim": {"type": "string",
                                         "enum": list(CAPTION_ANIMS),
                                         "description": "**这一镜**的字幕入场动画（可省略＝跟整片）。"
                                                        "金句/结论镜用 pop 或 karaoke 加强"},
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


#: 无旁白时一镜的时长**不是估算**：渲染层按 `max(min_duration_sec, 2.6 + 0.16 × 字数)` 定
#: （`render.DEFAULT_SHOT_SEC` 与 `code_render.narration_for` 那一行；两处漂移由
#: tests/test_code_spec.py 拦）。原先这里按「有旁白」的口径只数字数、完全不看
#: `min_duration_sec`——真机 run 79dcdc0ec0a8：模型给 14 镜写了合计 164 秒的驻留，
#: 估算却报 73~112 秒，把一条本来合格的片子打回去要它「加到 46 镜、放慢语速」。
NO_NARRATION_BASE_SEC = 2.6
NO_NARRATION_SEC_PER_CHAR = 0.16
#: 自动对齐目标时长时，一镜最多驻留多久（再长观众就开始等了，宁可加镜）。
MAX_DWELL_SEC = 20.0


def shot_sec_bounds(shot: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[float, float]:
    """一镜的时长区间（低, 高）。无旁白时低＝高＝渲染层那条确定的算式。"""
    text = str(shot.get("text") or "")
    floor = float(shot.get("min_duration_sec") or 0.0)
    if spec.get("narration") and text:
        try:
            rate_pct = int(str(spec.get("rate") or "+0%")[:-1])
        except ValueError:
            rate_pct = 0
        factor = max(0.4, 1.0 + rate_pct / 100.0)      # 语速越快，秒数越少
        low = len(text) * SEC_PER_CHAR_LOW / factor + 0.6
        high = len(text) * SEC_PER_CHAR_HIGH / factor + 1.2
        return max(floor, low), max(floor, high)
    sec = max(floor, NO_NARRATION_BASE_SEC + len(text) * NO_NARRATION_SEC_PER_CHAR)
    return sec, sec


def _overlap_total(spec: Mapping[str, Any]) -> float:
    """所有接缝转场交叠掉的秒数（拼接那一趟会从总长里减掉）。"""
    shots = list(spec.get("shots") or [])
    if len(shots) < 2:
        return 0.0
    try:
        plan = transition_plan(spec)
    except Exception:  # noqa: BLE001 - 估算不该因为转场档名写坏而炸
        return 0.0
    base = float(plan["duration"]) if str(plan["style"]) not in ("", "none") else 0.0
    over = {str(x.get("after")): (0.0 if str(x.get("style") or "none") == "none"
                                  else float(x.get("duration") or 0.0))
            for x in plan.get("seams") or []}
    return sum(over.get(str(s.get("id") or ""), base) for s in shots[:-1])


def estimate_sec(spec: dict[str, Any]) -> tuple[float, float]:
    """总时长区间（低, 高），用于出片前判断是否偏离目标。

    逐镜求和再减掉转场交叠；每镜都尊重 `min_duration_sec`。有旁白时是估算（真实时长由
    TTS 实测回填），无旁白时是确定值（低＝高）。
    """
    shots = spec.get("shots") or []
    pairs = [shot_sec_bounds(s, spec) for s in shots]
    lap = _overlap_total(spec)
    low = sum(p[0] for p in pairs) - lap
    high = sum(p[1] for p in pairs) - lap
    return round(max(0.0, low), 1), round(max(0.0, high), 1)


def fit_duration(spec: dict[str, Any], target: float | None) -> tuple[dict[str, Any], list[str]]:
    """无旁白时把各镜驻留按比例拉长/压短到目标时长 → (新 spec, 说明)。

    无旁白的时长完全由 `min_duration_sec` 决定，系统能精确算出来，就不该把「改几个秒数」
    这种纯算术丢回给模型重写整份分镜。拉长时每镜封顶 `MAX_DWELL_SEC`，压短时不低于
    这一镜字幕的阅读下限。有旁白时时长归声音，这里不动（由 `duration_block` 给建议）。
    """
    if not target or target <= 0 or spec.get("narration"):
        return spec, []
    shots = [dict(x) for x in spec.get("shots") or []]
    if not shots:
        return spec, []
    lap = _overlap_total(spec)
    secs = [shot_sec_bounds(x, spec)[0] for x in shots]
    total = sum(secs) - lap
    if total <= 0 or abs(total - target) <= target * 0.05:
        return spec, []
    floors = [NO_NARRATION_BASE_SEC + len(str(x.get("text") or "")) * NO_NARRATION_SEC_PER_CHAR
              for x in shots]
    want = target + lap
    new = list(secs)
    # 迭代按比例分配：封顶/触底的镜固定下来，剩余差额摊给其余镜
    for _ in range(8):
        diff = want - sum(new)
        # 只把差额摊给还能往那个方向动的镜：拉长时没封顶的，压短时没触底的
        free = [i for i in range(len(new))
                if (new[i] < MAX_DWELL_SEC - 1e-6 if diff > 0 else new[i] > floors[i] + 1e-6)]
        if abs(diff) < 0.05 or not free:
            break
        base = sum(new[i] for i in free) or 1.0
        for i in free:
            new[i] = min(MAX_DWELL_SEC, max(floors[i], new[i] + diff * new[i] / base))
    for x, sec in zip(shots, new):
        x["min_duration_sec"] = round(sec, 2)
    out = dict(spec, shots=shots)
    got = sum(new) - lap
    note = (f"无旁白：已把各镜驻留按比例从合计 {total:.0f}s 调到 {got:.0f}s 以对齐目标 "
            f"{target:.0f}s（每镜 {min(new):.1f}~{max(new):.1f}s）")
    if got < target * 0.9:
        note += f"；每镜最多驻留 {MAX_DWELL_SEC:.0f}s，仍短于目标——要更长就加镜"
    return out, [note]


def duration_block(spec: dict[str, Any], target: float | None, *,
                   long_factor: float = 1.6, short_factor: float = 0.5) -> str | None:
    """估算整体偏离目标到不值得开烧时 → 一句带具体改法的拒因；否则 None。

    改法按有无旁白分开给：无旁白说「加镜 / 加长驻留」（没有语速可调），有旁白说
    「加镜加文案 / 放慢语速」。镜数建议按**每镜平均时长**折算，而不是按字数。
    """
    if not target or target <= 0:
        return None
    low, high = estimate_sec(spec)
    n = len(spec.get("shots") or [])
    if n == 0 or not (low > target * long_factor or high < target * short_factor):
        return None
    too_long = low > target * long_factor
    per = max(0.5, (low + high) / 2 / n)
    if too_long:
        fix = (f"删到约 {max(1, round(target / per))} 镜，或把每镜文案缩短"
               + ("" if spec.get("narration") else "、驻留调短"))
    elif spec.get("narration"):
        fix = f"加到约 {max(n + 1, round(target / per))} 镜（每镜文案也可加长），或放慢语速"
    else:
        need = max(n + 1, round(target / min(MAX_DWELL_SEC, max(per, 12.0))))
        fix = (f"加到约 {need} 镜（每镜最多驻留 {MAX_DWELL_SEC:.0f}s 也只够 "
               f"{n * MAX_DWELL_SEC:.0f}s）")
    return (f"目标 {target:.0f}s，但这份分镜{'估' if spec.get('narration') else '算'}下来 "
            f"{low:.0f}~{high:.0f}s（{n} 镜），偏离超过闸值：{fix}。")


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
