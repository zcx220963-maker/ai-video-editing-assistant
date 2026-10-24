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

#: 画幅的**常见写法**（2026-10-18）。真机上模型把「横屏」写成了 `aspect: "16:9"`，
#: 一次校验打回就把整份 24 镜分镜重生成了一遍——那是这条链上最贵的一处纠错（整份
#: spec 的 token 白烧，用户看到的是「怎么又碰壁了」）。枚举值在 schema 里有，模型
#: 仍会按自己的习惯写比例；服务端认常见写法，比让它重写整份便宜得多。
#: 认不出的照旧打回——不把 `"banana"` 猜成横屏。
_ASPECT_ALIASES: dict[str, str] = {
    "landscape": "landscape", "横屏": "landscape", "横向": "landscape",
    "宽屏": "landscape", "horizontal": "landscape", "wide": "landscape",
    "16:9": "landscape", "16x9": "landscape", "1920x1080": "landscape",
    "portrait": "portrait", "竖屏": "portrait", "竖向": "portrait",
    "纵向": "portrait", "vertical": "portrait", "9:16": "portrait",
    "9x16": "portrait", "1080x1920": "portrait",
    "square": "square", "方屏": "square", "方形": "square", "1:1": "square",
    "1x1": "square", "1080x1080": "square",
}
_RATIO_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:：x×*]\s*(\d+(?:\.\d+)?)\s*$")


def norm_aspect(value: Any) -> str:
    """画幅写法 → `ASPECTS` 的键；认不出返回空串（调用方照旧打回，不猜）。

    三种来路都收：规范键（`landscape`）、中文习惯（`横屏` / `竖屏` / `方屏`）、
    比例与像素尺寸（`16:9` / `9x16` / `1920×1080`）。任意 `w:h` 也按长短边归一
    （`4:3` → 横屏、`2.39:1` → 横屏、`3:4` → 竖屏），比例相等容差 5%。
    2026-10-18 真机：`aspect: "16:9"` 一句打回，整份分镜重写一遍。
    """
    text = str(value or "").strip().lower().replace("×", "x").replace("：", ":")
    if not text:
        return ""
    if text in ASPECTS:
        return text
    if text in _ASPECT_ALIASES:
        return _ASPECT_ALIASES[text]
    compact = text.replace(" ", "")
    if compact in _ASPECT_ALIASES:
        return _ASPECT_ALIASES[compact]
    m = _RATIO_RE.match(text)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        if w > 0 and h > 0:
            if abs(w - h) / max(w, h) <= 0.05:
                return "square"
            return "landscape" if w > h else "portrait"
    return ""

#: 帧率白名单：不给连续值——帧数直接决定母带编码量与逐镜账里的每帧毫秒
FPS_CHOICES = (10, 15, 24, 30)

MAX_SHOTS = 120
MAX_TEXT_CHARS = 60          # 竖屏字幕带两行的容量
MAX_TEXT_CHARS_WIDE = 110    # 横屏那条居中横排字幕：一行更宽，两行装得下更多
MAX_HIGHLIGHTS_PER_SHOT = 4

#: 英文字幕（2026-10-21，中英双字幕的第二行）：一行 0.54 倍字号，横向按字距更宽。
#: 上限按中文那一栏的 1.6 倍取——同一句话的英文大致就是这个量级，再长就该拆镜了。
MAX_TEXT_EN_CHARS = 180

#: 字幕里不画的装饰括号（2026-10-21）。用户原话：「不需要 ⌈⌋ 这种符号」。
#: 这类符号在母版里既不参与排版也不表示语气，只是模型偶尔拿来「点睛」；画出来就是
#: 字面上的一对怪符号，还会把逐词高亮的子串对齐搞乱。**在这里剥掉、不进任何一帧**。
#: `《》` 不在此列——书名号是正经标点（片尾那句引书名要用）。
_DECO_BRACKETS = str.maketrans({c: None for c in "「」『』⌈⌉⌊⌋〈〉"})


def strip_caption_periods(s: str) -> str:
    """字幕不画句号（2026-10-21 用户点名：句尾不要句号，用两个空格替代）。

    句中的「。」换成两个空格（一句话接一句话，不画那个圈），句尾那个直接去掉。
    ASCII 句点**只去句尾**——`44.7%`、`3.6 倍`、`NHTSA` 里的点一个都不动；写成
    `r"."` 全删会把数字一起改坏，而数字是这条链上唯一不能改口径的东西。
    英文那一行同理：结尾的 `.` 去掉，词中间的保留。
    """
    if not s:
        return s
    out = s.replace("。", "  ").replace("．", "  ")
    out = re.sub(r"[ \t]{3,}", "  ", out).strip()
    out = out.rstrip(".").rstrip()
    return out

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

#: 旁白音色（edge-tts 中文音色；值随服务商变，故下游也放行合规格式）。
#:
#: **只列标准普通话六档**。服务商那里另有八档地方方言——粤语三档（``zh-HK-*``）、
#: 台湾三档（``zh-TW-*``）、东北口音与陕西方言各一档——它们从这份清单里撤掉了：
#: 一部中文科普片的旁白只有「普通话」一个答案，把方言摆在「挑一个音色」的十四个
#: 格子里，只会让人多花时间绕开它们。
#:
#: 撤的是**可选清单**，不是校验：老片子里已经写着粤语音色的那一镜仍然过
#: :data:`_VOICE_RE`（那条按形状认，不按名单认），不会因为名单变小而重烧或报错；
#: ``KNOB_VALUE_LABELS["voice"]`` 也逐档留着它们的名字，于是确认卡回读时仍是
#: 一句人话，而不是一个没人认识的机器名。
VOICES = (
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiNeural", "zh-CN-YunxiaNeural",
    "zh-CN-YunyangNeural",
)

#: 质感档名。**每一镜都能带自己的一档**，顶层那档是「整片默认」：滤镜跑在拼接之前
#: 的那一趟逐镜片段上，所以「第 3 镜要老电视、其余整片柔光」是合法形状。
#: 与 BGM 不同层（BGM 在混音那一趟），但它照样**不进** ``FILM_KNOBS``——理由见那里的
#: 注释：切片缓存存的是**加质感之前**的像素，换档只重跑滤镜，不重烧任何镜头。
#: 滤镜串单源在 ``render.TEXTURE_FILTERS``——这里只列名与人话说明，
#: 两边各写一遍就会出现「界面上有这档、渲出来是原片」。
TEXTURES = ("none", "film", "tv", "glow", "bleach")

# ---------------------------------------------------------------------------
# 整片美术方向（`art`，2026-10-18）
# ---------------------------------------------------------------------------
#: 整片**怎么画**的那一份共同约定：笔触、光源、纸纹、字体、动效质感。
#:
#: 为什么是整片一处、逐镜注入而不是计划卡上的一排档位：它是**画法**，不是「这条片子
#: 在哪儿播、多长、什么声音」——卡面那排 radio 每一个都要能反查到枚举源与中文标签，
#: 而「暗部偏暖一点」这种话说不出一个枚举；自由值塞进卡面参数的后果是整份计划被打回
#: （见 `COLOR_KNOBS` 那段注释）。所以它只出现在 `spec_schema()` 里：主对话读用户那句
#: 风格话之后写一次，写手把这一段逐字拼进每一镜的提示词，镜头代码照它画。
#:
#: 取值全是**小枚举**而不是散文：枚举才进得了缓存键、才验得出「写歪了」；
#: `palette_note` 是唯一自由的一句（截到 120 字，不报错）。
#: 五个字段都不带缺省值——「没写」与「写了缺省那一档」在画面上应当同义，
#: 但只有没写时不产生注入、不改变老片子的 props 字节。
ART_BRUSHES = ("graphite", "marker", "watercolor", "ink")
ART_LIGHTS = ("top-left", "top", "top-right", "left", "front",
              "right", "bottom-left", "bottom", "bottom-right", "soft")
#: 与 ``TEXTURES`` 不是同一件事：那一组是**切片上过的 FFmpeg 观感档**（老胶片/老电视…），
#: 这一组是画面里**纸纹与颗粒的画法**（笔刷的底材），由镜头代码与整片调色各自读。
ART_TEXTURES = ("none", "paper", "grain")
ART_TYPES = ("serif-display", "serif-editorial", "sans-display", "sans-editorial")
ART_MOTIONS = ("spring", "linear")

#: `palette_note` 一句最多多少字（超了截断，不报错——它是给人看的一句话）。
ART_NOTE_MAX_CHARS = 120
#: 整片画风那句自由描述（`art.look`）的长度上限。**自由值**：不进计划卡参数
#: （`art` 整块都不在 `knob_props` 里），所以可以放开写；它是让模型「自己组合审美」
#: 的那一处——枚举只是可选的下限，不是菜单。
ART_LOOK_MAX_CHARS = 160

# ---------------------------------------------------------------------------
# 主角（`protagonist`，2026-10-19）
# ---------------------------------------------------------------------------
#: 整片**同一个主角穿行各场景/各时代**的那一份约定。参考片（一个人从洞穴走到蒸汽、
#: 火箭与手机时代）的力量不在任何一镜的细节，而在**同一个人**：身高、步幅、朝向、
#: 脚底那条线始终是同一个，只有背景与衣服在换。
#:
#: 与 `art` 同一条摆放理由：它是**画法/演出**，不是计划卡上能勾的一档，所以只出现在
#: `spec_schema()` 里、不进 `knob_props`。但它是**结构化**的（不是散文），因为写手要
#: 把这些数原样交给 `<WalkingFigure>`（如果主体是走路的人）或按同一组比例自己画——
#: 身高 0.48 与 0.32 画出来是两个主体，靠一句「人物大一点」是守不住的。
#:
#: 只有 `look` 是真写的自由文本（≤160 字），其余是小枚举或带界数值；数值越界就地
#: 夹回范围、不报错（见 :func:`norm_protagonist`）——这一栏是增益，不该把整份分镜打回。
PROTAGONIST_FACINGS = ("right", "left")
#: 与 `remotion/src/lib/walk.tsx` 的 `WalkWardrobe` 逐项一致。
PROTAGONIST_WARDROBES = ("plain", "cloak", "cap", "coat", "hoodie")
#: 与 `remotion/src/lib/walk.tsx` 的 `WalkRender` 逐项一致。它不是「必须用组件」的
#: 开关，只是告诉写手这一件主体该往哪个方向画：剪影 / 平涂 / 受光两档 / 线稿。
#: 非人的主体（鹿、机器人、一块发光的矩形）也可以照这一档自己画，不套组件。
PROTAGONIST_RENDERS = ("silhouette", "flat", "lit", "line")
PROTAGONIST_LOOK_MAX_CHARS = 160
PROTAGONIST_WARDROBE_MAX_CHARS = 160
PROTAGONIST_LIGHT_MAX_CHARS = 80
#: 主体类型（人 / 鹿 / 机器人 / 发光的矩形…）。自由文本而不是枚举：题材是无限的，
#: 枚举只会逼模型把「一块会发光的矩形」硬说成人。
PROTAGONIST_KIND_MAX_CHARS = 40
#: 身高占画面高的比例。参考片是 0.40~0.55：**人够大**才是主角，0.15 是图标。
PROTAGONIST_HEIGHT_RANGE = (0.25, 0.75)
#: 脚底那条线在画面高的哪一档。0.98 是贴底，0.55 是站在画面上半。
PROTAGONIST_FOOT_RANGE = (0.55, 0.98)
#: 一个完整步态周期几秒。参考片约 0.8~1.1；每一镜同一个值，切点才落在同一相位。
PROTAGONIST_STRIDE_RANGE = (0.5, 1.8)


#: 五个枚举栏的「名字 → 取值表」：归一与报错共用一张表，免得两处各列一遍。
#: 认得出的近义写法 → 契约里的那一档。**只收无歧义的近义**，认不出的丢掉那一栏。
#: 真机 run 7dcaa7246e92（2026-10-19，13 镜娱乐史）：整份分镜只因两条一词之差被打回——
#: 把顶层的 `texture="film"` 写进了 `art.texture`，又自造了 `type="sans-caption"`。
#: 这不是「用户要的画风没画出来」，是同一档的另一种叫法；为一词之差打回 13 镜重交，
#: 代价是一整轮写码（§「堵死比白烧更坏」）。
#: 2026-10-20 第 001 期真机又撞一次：`brush="flat"`（把「扁平矢量」当笔触写）、
#: `motion="standard"`。同一份 30 多镜的计划被打回、模型重写一遍，白等两分钟。
#: 所以现在**认不出也只丢那一栏、不报错**：`art` 是画法增益，没有它写手照样按
#: `art.look` 画；而为一栏打回整份计划，代价远大于少一句笔触提示。丢掉是安全的
#: （不会出现「提示词写着石墨、画面是别的」那种静默错画），代价只是那一栏没生效。
ART_ALIASES: dict[str, dict[str, str]] = {
    "texture": {"film": "grain", "vhs": "grain", "old-film": "grain",
                "photo": "none", "clean": "none", "flat": "none"},
    "type": {"sans-caption": "sans-editorial", "sans": "sans-editorial",
             "sans-serif": "sans-editorial", "serif-caption": "serif-editorial",
             "serif": "serif-editorial"},
    "brush": {"pen": "ink", "pencil": "graphite", "graphite-pencil": "graphite",
              "flat": "ink", "flat-vector": "ink", "vector": "ink",
              "line": "ink", "outline": "ink"},
    "motion": {"ease": "linear", "easing": "linear", "bounce": "spring",
               "elastic": "spring", "standard": "linear", "default": "linear",
               "smooth": "linear", "ease-in": "linear", "ease-out": "linear",
               "ease-in-out": "linear"},
}

ART_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("brush", ART_BRUSHES),
    ("light", ART_LIGHTS),
    ("texture", ART_TEXTURES),
    ("type", ART_TYPES),
    ("motion", ART_MOTIONS),
)

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
TRANSITION_STYLES = ("none", "fade", "fadeblack", "dissolve", "wipeleft",
                     "wiperight", "slideup", "circleopen", "circleclose",
                     "zoomin", "hblur", "radial", "smoothleft")

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

#: **字幕样式**：这一行字长什么样、站在哪。
#:
#: 2026-10-16「文字库面板重做」那一轮把原先揉在 `subtitle_mode` / `caption_position` /
#: `caption_anim` 三个旋钮里的选择拆成两件本来就不相干的事——「字长什么样」（这里）
#: 与「被点名的词怎么被点出来」（:data:`HIGHLIGHT_STYLES`）。
#:
#: 拆的理由不是归类好看，是旧模型里有的组合**说不出来**：「要关键词换色、但不要纸条」
#: 是最常见的诉求，而旧的三旋钮把纸条底、逐词点亮、撕纸下划线捆在 `torn_highlight`
#: 一档上，想只要换色就只剩「整句字幕」这条路可退。
#:
#: 每一档落到画面上是什么样（有没有纸条底 / 站在哪 / 什么入场 / 字号档 / 要不要描边），
#: 由 `remotion/src/FilmLayout.tsx` 的 `CAPTION_LOOK` **一处**说了算；那边的枚举镜像
#: 与这里逐字相同，由 ``tests/test_code_spec.py`` 第 ⑤ 节拿正则比出来。
CAPTION_STYLES = ("on_picture", "outlined", "paper", "film_line",
                  "big_center", "top_banner", "none",
                  # 2026-10-23：字幕交给这一镜的代码自己设计。这一档下：
                  # · 母版那条不画、`<CaptionLayer/>` 不被自动补上、写手画在字幕带里的
                  #   字**不被 `_strip_zone_captions` 删掉**（见 `_autofix_caption_layer`）；
                  # · 写手拿 `getInputProps().text` 自己排位置/字号/入场，或摆带参数的
                  #   `<CaptionLayer bottom={…} captionStyle={…} />` 放进自己的盒子里。
                  # 其余七档全是母版 `CAPTION_LOOK` 里写死的常量，所以「字幕永远一个样」。
                  "by_shot")

#: **高亮样式**：被点名的词怎么被点出来。与上面那一维**正交**——「贴画面 + 关键词换色」
#: 与「纸条底 + 逐词点亮」都是合法组合，两维各有各的取值域。
#: 落到 `CaptionText` 上就是把 `emphasis` 与「要不要逐字点亮」两件事定下来
#: （`FilmLayout.tsx` 的 `HIGHLIGHT_LOOK` 一处说了算）。
HIGHLIGHT_STYLES = ("color", "karaoke", "block", "ink", "plain")

#: 前端「文字库」里那两排卡。**一档 = 一个取值 + 一句「什么时候该选它」**。
#:
#: `key` 就是枚举取值本身（不再另起 `torn-ink` 这种档名）：档名与取值是两套词汇时，
#: 界面上写着「撕纸高亮·逐词点亮」而渲染认的是另一个字符串——这种不一致不会红，
#: 只会渲成别的。中文标签由 :func:`library_props` 从 ``KNOB_VALUE_LABELS`` 现取，
#: 这里**一个中文词都不重复**（唯一那句 ``note`` 是给人挑的时候看的：说适用范围，
#: 不复述档名）。**每一档的 key 必须在自己那一维的枚举里**，由
#: ``tests/test_code_spec.py`` 第 ⑨ 节逐档反查（造一个写错的取值必须判红）。
#:
#: `group` 决定两排在面板上怎么分：前端按出现顺序取 unique group，连栏名都取
#: ``LIBRARY_DIM_TITLES[group]``。
CAPTION_PRESETS: tuple[dict[str, Any], ...] = (
    {"key": "by_shot", "group": "caption_style",
     "note": "字幕交给这一镜的代码自己设计：位置、字号、描边、入场全归它，跟着构图走。"
             "缺省这一档——七档固定样式是「所有片子字幕长得一样」的根源"},
    {"key": "on_picture", "group": "caption_style",
     "note": "字直接压在画面上，没有底条。不想让字幕成为构图的一部分时用它"},
    {"key": "outlined", "group": "caption_style",
     "note": "白字 + 黑描边压在画面上，亮底时自己压一层渐隐。画面深、字要立住时用它"},
    {"key": "paper", "group": "caption_style",
     "note": "白纸条压在画面下沿。最稳、最不挑画面的一档，也最容易让整片长得一样"},
    {"key": "film_line", "group": "caption_style",
     "note": "一整句淡淡浮在下沿，字号略小、行距松。适合句子短、画面本身在动的段落"},
    {"key": "big_center", "group": "caption_style",
     "note": "整句放大压在画面正中，弹出入场。适合一句话就是全片重点的镜头——用多了会腻"},
    {"key": "top_banner", "group": "caption_style",
     "note": "纸条挪到顶部安全区、上浮入场，把下半部分整个让出来。适合下半部有主体的镜头"},
    {"key": "none", "group": "caption_style",
     "note": "整片不画字幕，只看画面与旁白。适合纯视觉演示、或旁白本身就是字幕的片子"},
)

HIGHLIGHT_PRESETS: tuple[dict[str, Any], ...] = (
    {"key": "color", "group": "highlight_style",
     "note": "被点名的词换成这一镜的强调色，其余字不动。默认这一档"},
    {"key": "karaoke", "group": "highlight_style",
     "note": "念到哪个词哪个词从暗变亮，整句一直在、眼睛能提前扫读"},
    {"key": "block", "group": "highlight_style",
     "note": "被点名的词垫一块底色。最显眼，也最容易盖住画面上的东西"},
    {"key": "ink", "group": "highlight_style",
     "note": "词后拖一条纸感笔触，不给底。比色块轻，比换字色重"},
    {"key": "plain", "group": "highlight_style",
     "note": "一个字都不特殊对待，整句一个样。画面已经够闹时用它"},
)

#: 上面两排合成一份（前端一次拿走）。**顺序即面板上的顺序**。
LIBRARY_PRESETS: tuple[dict[str, Any], ...] = CAPTION_PRESETS + HIGHLIGHT_PRESETS

#: 音色分组的**前缀**与组名。分组是为了让人在清单里找得动，**不是**给音色重新起名——
#: 每一档自己的中文标签（``KNOB_VALUE_LABELS["voice"]``）仍然逐字保留。
#:
#: 今天只剩「普通话」一组（方言那两组连同它们的音色一起撤了，见 :data:`VOICES`），
#: 结构仍然留着：``library_props()`` 按前缀分组、断言每个音色都落在某一组里，
#: 「其他」那一组专收前缀对不上的新音色——服务商哪天加了新语区，面板上会多出一组
#: 而不是少一档，测试也会指出该给它补一个组名。
VOICE_GROUPS: tuple[tuple[str, str], ...] = (
    ("zh-CN", "普通话"),
)

#: 一段镜头代码的体积上限。lint 与 bundle 都按字节走，超了不是"风格问题"而是
#: 一次请求把容器内存吃掉一截——这条链一镜一次浏览器，宁可在这里拦。
MAX_CODE_BYTES = 40_000

# ---------------------------------------------------------------------------
# 字段参数通道（spec §12）：把"可改的东西"从代码里提出来当数据
# ---------------------------------------------------------------------------

#: 只有这五种，因为前端只有这五种控件。多加一种会落成一个没人认识的输入框
#: ——控件的单一来源是这份 type（旧链 #127「补丁口把字段类型也发出去」那条纪律的延续）。
#: 2026-10-16 摘掉 `image`：它存在的唯一理由是从素材库里挑一张图上屏（值落
#: `fields.<name>`，换图不必改代码）。取材那三条路拆掉之后它没有对象了，而这一栏
#: 一旦留着，模型会拿它当「这里可以摆图」的合法出口。
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
    "aspect", "captionStyle", "highlightStyle", "fields", "children",
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


def norm_highlight_color(value: Any) -> str:
    """自由色那一维（见 :data:`COLOR_KNOBS`）的归一：认得出就回小写六位 `#rrggbb`，
    **其余一律回空串**（＝没钉，跟色板走）。

    一个函数而不是三处各写一遍正则：校验（`validate_code_spec`）、注入
    （`code_render.shot_input_props`）、样张（`caption_preview`）读的都是它，
    于是「什么叫一条合法的强调色」只有一处说法。空串与 `None` 在这里同义。
    """
    text = str(value or "").strip()
    return _expand_hex(text) if text and _HEX_RE.match(text) else ""


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
                     f"{', '.join(FIELD_TYPES)}——白名单之外的类型会落成一个没人认识的输入框")
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
#: 自带音轨，命中即整段跳过、连 TTS 都不重发），`caption_style`/`highlight_style`
#: 改的是字幕的画法。不进键的后果与旧链那条注释写过的同族——「少算一个键就是拿旧片
#: 冒充新片」。反过来 `texture` **仍然不进**：它跑在缓存之后的那一趟滤镜上，
#: 换档 0 镜重烧（§5.3）。
#:
#: R7-T22 加的 `entrance`/`exit` 也在这里，理由与字幕两档同一条：入场/出场画在切片
#: 里（`FilmLayout.tsx` 按 token 逐帧插值），所以它换的是切片像素。
#: 2026-10-16 加的 `highlight_color`（用户钉的高亮词颜色）同理，而且更直白：它改的就是
#: 字幕那几个字的颜色，画进切片里。不进键的后果在这里最好认——换了颜色，整片仍是上一版
#: 的颜色，而「颜色没跟着变」看起来像没保存成功。
#: 而同一批加进来的 `transition`/`transition_sec` **故意不在**：转场跑在**拼接**那一趟
#: （`mediaops.concat` → ffmpeg `xfade`），逐镜切片一个字节都不动——与 `texture` 同族，
#: 进键的代价是「整片把淡出换成擦除」要重烧 20 镜的 Remotion 帧，而它本来只是一趟
#: 拼接。tests/test_code_transition.py 对这两侧各有一支断言（在键 / 不在键）。
#: 2026-10-17 加的整片 `palette`（用户要的「整片一个底色」）**在**：与 `texture` 正好
#: 相反，它改的就是**每一镜的像素**——认不出这一镜自己的色板时，整片那一档直接决定
#: 这一镜的底色与字幕三色（`film_style_of` 把它写进注入）。不进键的后果与
#: `highlight_color` 同族：用户在卡上把整片换成深海蓝，出来的还是上一版。
#: 2026-10-18 加的整片 `art`（美术方向）**在**：它同时改两处像素——写手按它重写
#: 每一镜的代码（笔触/光源/字体），以及切片上那一趟统一调色（`render.art_grade`）。
#: 不进键的后果与 `palette` 同族：改了美术方向，出来的还是上一版。
#: 2026-10-19 加的 `protagonist`（同一个主角的演出约定）**在**：身高/脚底线/朝向/步频
#: 都直接决定这一镜画出来的像素。不在键里的话，改了主角身高只会重写新镜，旧切片仍是
#: 上一版那个身材——一条片子里出现两种身高，正是这一栏要消灭的东西。
FILM_KNOBS = ("fps", "aspect", "narration", "voice", "rate", "caption_style",
              "highlight_style", "highlight_color", "palette", "entrance", "exit",
              "art", "protagonist")

#: 选区改（P3）对代码镜只开这五条指针。`code` 的改动必须重过闸① 才允许落盘；
#: `fields/{name}/value` 不动代码字节，改的是注入母版的值，所以不重跑 lint，
#: 只按 `check_field_value` 那份与声明期同源的规则校验（越界打回，不钳值）；
#: `texture` 既不动代码也不动像素来源，改的是缓存之后的那一趟滤镜。
EDITABLE_POINTERS = ("/shots/{i}/code", "/shots/{i}/text", "/shots/{i}/source",
                     "/shots/{i}/fields/{name}/value", "/shots/{i}/texture")


def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


# 2026-10-16 删掉了 `_norm_assets`：它归一的 `shots[].assets`（每项 `{name, sha256,
# ext}`，sha256 全 64 位）是「上传件/素材上屏」那条路的申报栏——渲染前按 sha256 从
# asset_library 取字节、投到渲染容器 `public/assets/<镜号>/<name>`。三条取材路拆掉之后
# 没有任何东西能填进这一栏，留着它等于给模型开一个「这里可以摆图」的合法出口。


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


#: 镜头代码还要不要从 demo / 卡库来（2026-10-18 用户决定：**不要**）。
#:
#: 用户原话：「我不想要 demo，需要模型 api 自己的能力，现在模型都很强」。demo 这一套是给
#: 写代码能力弱的模型搭的脚手架；这几天它反而成了主要的负担——一条 run 光 demo 就读了
#: 53 份、还编出两个不存在的名字卡死在上面；照抄 demo 又把 demo 的外观（PPT 表格、霓虹字）
#: 一起抄进来，用户要的风格只落到了换颜色。关掉之后：`card` / `derived_from` 两栏忽略，
#: 与 demo 绑定的三道整片判据（⑫ 骨架相似 / ⑳ 原创配额 / ⑮ 版式族配额）不跑，取材轮不再给
#: demo 全文，技能附件里不再登记 `demos/` 与 `shots/`（见 SKILL.md 的 `exclude`）。
#: demo 与卡库文件留在磁盘上没删：要恢复，这里改回 True、删掉那一行 `exclude` 即可。
DEMOS_ENABLED = False


def _norm_cite(raw: Mapping[str, Any], sid: str,
               errors: list[str]) -> tuple[str, list[str]]:
    """``card`` / ``derived_from`` 两栏的归一与白名单校验——**单源**。

    为什么抽出来：这两栏在**两个地方**被读——分镜轮（``_norm_shot`` 经
    ``validate_code_spec``）与取材轮（``collect_demos``）。两处各写一遍「从哪张卡来、
    派生自哪几份 demo」的判断，就会出现「取材轮放行了分镜轮不认的 relpath」这种
    「取材白取了」的形状：模型拿到全文、写完代码、再被同一个白名单打回。
    所以白名单只有这里一份，两轮共用。

    **两栏都是选填**（2026-10-16 改）。卡库与 demo 是**供给**，不是义务：整片不声明
    出处直接写代码是允许的，只声明一部分也是。改之前这两栏是必填、且「为空即报」，
    于是"不想照卡派生"的模型只有一个出路——挂 `ORIGINAL_SENTINEL` 这个哨兵。那正是
    「本来一句话就能做的事，非要用一条报错教会你」的形状：哨兵因此连同它那一整段
    特判一起删了（**哨兵现在与"留空"同义**，老写法照样收得下，只是不再需要）。

    **非空时白名单照旧**：写了卡名就得是库里真有的那张，写了 relpath 就得在册——
    取材轮与分镜轮仍然共用这一份尺，否则会出现「取材轮放行了分镜轮不认的 relpath」
    这种「取材白取了」的形状。
    """
    if not DEMOS_ENABLED:
        # 不用 demo 了（见 `DEMOS_ENABLED`）：这两栏写了什么都当没写——模型照旧 schema 的印象
        # 填一个卡名或 relpath，不该因此整份分镜被白名单打回。
        return "", []
    card = str(raw.get("card") or "").strip()
    if card:
        known = _known_card_ids()
        if known and card not in known:
            _err(errors, f"{sid}：card={card!r} 不在卡库中（共 {len(known)} 张卡），"
                         f"报了卡名就得报到库里真有的那一张（见 shots/INDEX.md）")

    derived_from = raw.get("derived_from") or []
    if not isinstance(derived_from, list):
        derived_from = [str(derived_from)]
    # 哨兵与"这一栏没写"是同一件事（不派生自任何 demo），所以先把它摘出来：剩下的才是
    # 真 relpath，空列表就表示"这一镜不派生"。混着真 demo 写也不再报错——真 demo 数数，
    # 哨兵那一项按没写处理（旧写法那条「哨兵必须单独出现」的报错随必填一起消失了）。
    _listed = [str(d).strip() for d in derived_from if str(d).strip()]
    _sentinel = ORIGINAL_SENTINEL in _listed
    listed = [d for d in _listed if d != ORIGINAL_SENTINEL]
    derived_from = [d for d in (_norm_demo_relpath(d) for d in listed) if d]
    if not derived_from and _sentinel:
        # 显式写了哨兵：语义与留空相同，但**保留这个写法**——闸⑳ 的配额数的是"明确
        # 说了这一镜是原创"的那几镜，`style_facts["original_shots"]` 也读它。留空是
        # "没表态"，写哨兵是"表态了"，闸要的是后者（不然它对着整片留空的片子报绿）。
        return card, [ORIGINAL_SENTINEL]
    known_demos = _known_demo_relpaths()
    if derived_from and known_demos:
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
    # 画面描述（2026-10-18）：主对话不再写每一镜的代码，只交这一句；代码由
    # `motion/shot_writer.py` 按它单独写。`code` 因此是可省的——但没有 `brief` 也没有
    # `code` 的镜仍然不成立（既没有画什么，也没有画）。
    brief = str(raw.get("brief") or "").strip()
    if len(brief) > SHOT_BRIEF_MAX_CHARS:
        _err(errors, f"{sid}：brief {len(brief)} 字 > {SHOT_BRIEF_MAX_CHARS}——"
                     "画面描述只写「画什么、怎么构图、怎么动」，细节留给写代码那一步")
        brief = brief[:SHOT_BRIEF_MAX_CHARS]
    # 实体清单（2026-10-18）：只喂写手与评审，**不进 SHOT_KNOBS**——它不直接改变像素，
    # 改了它不会让已经写好的代码重烧（要重画就交 shots_patch）。写得随意不报错：
    # 一个字符串按顿号/逗号拆开也收，模型偶尔会这么写。
    entities: list[str] = []
    raw_entities = raw.get("entities")
    if isinstance(raw_entities, str):
        raw_entities = re.split(r"[、,，;；/]", raw_entities)
    if isinstance(raw_entities, (list, tuple)):
        for item in raw_entities:
            name = str(item or "").strip()
            if not name or name in entities:
                continue
            entities.append(name[:ENTITY_MAX_CHARS])
            if len(entities) >= MAX_ENTITIES_PER_SHOT:
                break
    code = str(raw.get("code") or "")
    if not code.strip() and not brief:
        _err(errors, f"{sid}：既没有 brief 也没有 code——每一镜至少要交一句画面描述"
                     "（画什么、怎么构图、怎么动），系统按它写这一镜的代码；"
                     "也可以自己把 code 写全")
    n = len(code.encode("utf-8"))
    if n > MAX_CODE_BYTES:
        _err(errors, f"{sid}：代码 {n} 字节 > {MAX_CODE_BYTES}，一镜一次浏览器，这个体积不接")

    # 装饰括号（2026-10-21）：见 `_DECO_BRACKETS`。先剥再量字数、再判高亮子串——
    # 反过来的话，剥掉一个括号会让已经算过的高亮词落空。
    text = strip_caption_periods(
        str(raw.get("text") or "").translate(_DECO_BRACKETS).strip())
    if not text:
        _err(errors, f"{sid}：text 为空——每一镜必须有一句要说的话")
    if len(text) > max_chars:
        _err(errors, f"{sid}：文案 {len(text)} 字 > {max_chars}，这一档画幅的字幕条放不下，请拆镜")
    # 英文字幕：与 `text` 同一句话的英文，画在中文下面一行（字号 0.54 倍）。
    # **可省**——省了就只排中文那一行，老片子一个字节都不变。写长了只报不改句：
    # 这一栏是排版容量，不是画面内容，截断会留下半句话。
    text_en = strip_caption_periods(
        str(raw.get("text_en") or "").translate(_DECO_BRACKETS).strip())
    if len(text_en) > MAX_TEXT_EN_CHARS:
        _err(errors, f"{sid}：英文字幕 {len(text_en)} 字 > {MAX_TEXT_EN_CHARS}，"
                     "两行字幕放不下，请拆镜或压短这句英文")

    highlights = [str(h).translate(_DECO_BRACKETS) for h in (raw.get("highlight") or [])
                  if str(h).strip()]
    if len(highlights) > MAX_HIGHLIGHTS_PER_SHOT:
        # 2026-10-19：多出来的高亮词就地截掉、不再打回——真机上 24 镜一份分镜，
        # 一个高亮词写多就让整份重交；多一个「亮起来」的词是排版偏好，不是渲不出的错。
        # 代价：模型不会被告知哪几个被截；确认卡的高亮栏（plan_table.highlight）
        # 列的就是这一镜**实际生效**的那几个，对得出来。
        highlights = highlights[:MAX_HIGHLIGHTS_PER_SHOT]
    # 非子串的高亮词直接丢掉（同上：它只影响「这个词亮不亮」，不影响任何一帧别的像素）。
    # 留下的那些原样进 spec；被丢的从账面上自然消失，模型看确认卡就知道哪几个没生效。
    highlights = [h for h in highlights if h in text]

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
    # **不进 `SHOT_KNOBS`**，而是像 `shot_cache_key` 里 `film_style_of` 那一段那样
    # 只在真有值时进键（无条件加会让 `None` 也成为一个值，所有既有镜的键全变、
    # 整片白烧一次）。
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
    # 层E2 逐镜字幕两档（R19 / 2026-10-16 两维化）：省略＝跟整片那一档。
    # **进缓存键**——字幕是逐帧画进切片里的（与 `entrance`/`exit` 同族），
    # 不进键就会拿旧切片冒充新片。
    cap_style = str(raw.get("caption_style") or "").strip().lower()
    if cap_style and cap_style not in CAPTION_STYLES:
        _err(errors, f"{sid}：caption_style={cap_style!r} 不支持"
                     f"（可用：{', '.join(CAPTION_STYLES)}）")
        cap_style = ""
    hl_style = str(raw.get("highlight_style") or "").strip().lower()
    if hl_style and hl_style not in HIGHLIGHT_STYLES:
        _err(errors, f"{sid}：highlight_style={hl_style!r} 不支持"
                     f"（可用：{', '.join(HIGHLIGHT_STYLES)}）")
        hl_style = ""

    # R1: card / derived_from 契约栏
    card, derived_from = _norm_cite(raw, sid, errors)

    # 能力点名（2026-10-22）：这一镜要用风格包正文里的哪几件能力（`<!-- section: … -->`
    # 的段名）。渐进式加载的第二层——索引常驻系统提示，正文只随点名它的那一镜给出。
    # 与 `entities` 同一口径：**不进 `SHOT_KNOBS`**（它不直接改变像素，改的是给写手的
    # 上下文），写得随意也不拦：认不出的名字由 `_caps_block` 报一句、写手按不变量画。
    caps: list[str] = []
    raw_caps = raw.get("caps")
    if isinstance(raw_caps, str):
        raw_caps = re.split(r"[、,，;；/\s]+", raw_caps)
    if isinstance(raw_caps, (list, tuple)):
        for item in raw_caps:
            name = str(item or "").strip()
            if name and name not in caps:
                caps.append(name[:32])
            if len(caps) >= MAX_CAPS_PER_SHOT:
                break
    # 批 4（2026-10-23）：这一镜额外需要的**技能块**。与上面的 `caps` 是两层东西——
    # `caps` 点的是**风格包正文**里的能力件（随风格包变），这一栏点的是
    # **技能正文**里的按需块（`SKILL_SECTION_NAMES`，随技能变）。
    # 认不出的名字静默丢掉：它是「多给一点写法」的优化，不是契约。
    sections: list[str] = []
    _raw_secs = raw.get("sections")
    if isinstance(_raw_secs, str):
        _raw_secs = re.split(r"[、,，;；/\s]+", _raw_secs)
    if isinstance(_raw_secs, (list, tuple)):
        for item in _raw_secs:
            name = str(item or "").strip().lower()
            if name in SKILL_SECTION_NAMES and name not in sections:
                sections.append(name)
    return {
        "id": sid,
        "brief": brief,
        "entities": entities,
        "caps": caps,
        "sections": sections,
        "code": code,
        "text": text,
        # 英文字幕（2026-10-21）：只进第二行，不参与高亮。空串＝只排中文那一行。
        "text_en": text_en,
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
        "caption_style": cap_style or None,
        "highlight_style": hl_style or None,
        "source": {str(k): str(v) for k, v in src.items()},
        "card": card,
        "derived_from": derived_from,
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
        # 2026-10-16 拆掉了这里的 `reveal`（闸㉘/㉗ 那两栏）。它申报的是「整幅插画分几块、
        # 按什么次序露出来」——`reveal.canvas` 要与库行 width/height 对账、母版按它逐帧
        # 算掩码、㉗ 再到真帧上比色。整条链的主语是一张 raster 插画，而那条路没有了。
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


def norm_art(value: Any) -> tuple[dict[str, Any], list[str]]:
    """整片美术方向 → (归一的 `art`, 错误清单)。缺省 `{}`（＝没写，老片子字节不变）。

    认不出的枚举值**只丢掉这一栏、不报错**（2026-10-20 改；理由见 `ART_ALIASES` 上面
    那段注释）：`art` 是画法增益，一栏没生效最多是少一句提示，而报错会把整份三十多镜的
    计划打回重写。丢掉而不是留着，仍然守住原来那条底线——不会出现「提示词里写着石墨
    笔触、画面上是别的东西」这种静默错画。只有 `art` 本身不是对象时才报错。
    `look` 与 `palette_note` 是两句自由文本，截断不报错。

    归一必须**幂等**：校验会把一份过过校验的 spec 再校验一遍（补丁链，见
    `transition_sec` 那段注释），所以本函数写下的值必须自己再认一遍。
    """
    errs: list[str] = []
    if not isinstance(value, Mapping):
        if value not in (None, ""):
            errs.append("art 要写成对象（笔触/光源/纸纹/字体/动效/一句色板说明）")
        return {}, errs
    out: dict[str, Any] = {}
    for key, allowed in ART_FIELDS:
        raw = value.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        name = str(raw).strip().lower()
        # 近义写法先归一（见 `ART_ALIASES`）：`film` 与 `grain` 是同一档质感，
        # `sans-caption` 与 `sans-editorial` 是同一档字体气质。为叫法打回整份分镜，
        # 模型只会把同一件事重写一遍。归一后的值仍要在枚举里，递归一次也幂等。
        name = ART_ALIASES.get(key, {}).get(name, name)
        if name not in allowed:
            # 认不出就丢掉这一栏（见 docstring）：一栏没生效是小事，打回整份计划是大事。
            continue
        out[key] = name
    look = str(value.get("look") or "").strip()
    if look:
        out["look"] = look[:ART_LOOK_MAX_CHARS]
    note = str(value.get("palette_note") or "").strip()
    if note:
        out["palette_note"] = note[:ART_NOTE_MAX_CHARS]
    return out, errs


def norm_protagonist(value: Any) -> tuple[dict[str, Any], list[str]]:
    """整片主角 → (归一的 `protagonist`, 错误清单)。缺省 `{}`（＝没写，老片子字节不变）。

    **这一栏刻意比 `art` 宽**：它不报错，只做三件事——收下 `look`、把枚举外的值丢掉、
    把越界数字夹回范围。理由是它属于「画面增益」而不是契约：一个 `facing="右"` 或
    `height=0.9` 不该让二十几镜的分镜整份打回（真机上的重试已经够多了），夹回 0.75
    最多是人物小一点，读者一眼能看出来、下一版改一句就行。

    `look` 是**这一栏成立的前提**：只有身高/步频而没有「这个主体是什么、什么轮廓」
    时，写手仍然会每镜发明一个。所以没有 `look` 就返回 `{}`，当作没写。

    归一必须**幂等**：校验会把一份过过校验的 spec 再校验一遍（补丁链），这里写下的值
    必须自己再认一遍。数值统一保留三位小数。
    """
    errs: list[str] = []
    if not isinstance(value, Mapping):
        return {}, errs
    look = str(value.get("look") or "").strip()
    if not look:
        return {}, errs
    out: dict[str, Any] = {"look": look[:PROTAGONIST_LOOK_MAX_CHARS]}
    kind = str(value.get("kind") or "").strip()
    if kind:
        out["kind"] = kind[:PROTAGONIST_KIND_MAX_CHARS]
    render = str(value.get("render") or "").strip().lower()
    aliases = {
        "shadow": "silhouette", "剪影": "silhouette", "silhouette": "silhouette",
        "color": "flat", "colour": "flat", "平涂": "flat", "flat": "flat",
        "real": "lit", "realistic": "lit", "shaded": "lit", "写实": "lit", "lit": "lit",
        "outline": "line", "线稿": "line", "line": "line",
    }
    if render in aliases:
        out["render"] = aliases[render]
    # 2026-10-20 起不再有「用哪个共享组件画主体」这一栏：主体怎么画由写手按 brief
    # 自己决定（用户的原话是「llm 自己明明有能力的，为什么非要限制模板」）。
    # 连贯性靠下一组量化数（身高/基准线/朝向/步频）守，不靠锁死一个组件。
    # 旧 spec 里残留的 `component` 在这里被丢掉，不影响分镜成立。
    wardrobe = str(value.get("wardrobe") or "").strip()
    if wardrobe:
        out["wardrobe"] = wardrobe[:PROTAGONIST_WARDROBE_MAX_CHARS]
    light = str(value.get("light") or "").strip()
    if light:
        out["light"] = light[:PROTAGONIST_LIGHT_MAX_CHARS]
    facing = str(value.get("facing") or "").strip().lower()
    if facing in ("r", "→", "右"):
        facing = "right"
    elif facing in ("l", "←", "左"):
        facing = "left"
    if facing in PROTAGONIST_FACINGS:
        out["facing"] = facing
    for key, (lo, hi) in (("height", PROTAGONIST_HEIGHT_RANGE),
                          ("foot_y", PROTAGONIST_FOOT_RANGE),
                          ("stride_sec", PROTAGONIST_STRIDE_RANGE)):
        try:
            num = float(value.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(num):
            out[key] = round(min(hi, max(lo, num)), 3)
    return out, errs


def _variant_record(value: Any) -> dict[str, Any] | None:
    """一条替换件登记 → 白名单形状。模块说明符不合规就整条丢掉。

    写手会把 `module` 原样写进 import；一个自由值就是打包期才炸的路径，所以
    替换件与基模板同规：只认 `@dg/lib/style/generated/<name>`。
    """
    if not isinstance(value, Mapping):
        return None
    name = str(value.get("name") or "").strip()[:64]
    module = str(value.get("module") or "").strip()
    if not name or not STYLE_TEMPLATE_MODULE_RE.match(module):
        return None
    return {
        "name": name,
        "module": module[:STYLE_TEMPLATE_MODULE_MAX_CHARS],
        "label": str(value.get("label") or "")[:80],
        "contract": [str(p)[:64] for p in (value.get("contract") or [])
                     if str(p).strip()][:STYLE_SLOT_CONTRACT_MAX],
        # 替换件自己的 props 文本：它给联合类型加的取值（更多时代/场景）靠这一行
        # 才能到逐镜写手。白名单漏掉它，写手只能看见基模板那几档。
        "contract_text": str(value.get("contract_text") or "")[:600],
        "hash": str(value.get("hash") or "")[:64],
    }


def _slots_from_pack(tmpl: Mapping[str, Any]) -> list[dict[str, Any]]:
    """模板的槽位清单 → 白名单副本。

    槽位是「每个导出组件都能换一份同合约实现」的登记：`variants` 列出可换的
    实现，`bound` 是本片实际选中的那一件（没选就没有）。**不是旋钮**——不进
    `FILM_KNOBS`，只在执行期从服务端注入，写手据此决定 import 哪个模块。
    """
    out: list[dict[str, Any]] = []
    for item in (tmpl.get("slots") or [])[:STYLE_SLOTS_MAX]:
        if not isinstance(item, Mapping):
            continue
        comp = str(item.get("component") or "").strip()[:64]
        if not comp:
            continue
        slot: dict[str, Any] = {
            "component": comp,
            "label": str(item.get("label") or comp)[:80],
            "kind": str(item.get("kind") or "other")[:32],
            # 文字槽位的内容契约：`text_kind=caption` 的那件是模板自己的**主字幕**。
            # 2026-10-23 真机：这一栏原先被白名单丢掉了，于是
            # `template_caption_in_code` 永远为假——系统看不见 `StyleCaption`，
            # 又给每一镜补了一条 `<CaptionLayer/>`（10 镜 10 镜两条字幕），
            # 母版 `<Caption>` 也没让位。判据、组件名都不改，只把数据抄全。
            "text_kind": str(item.get("text_kind") or "")[:32],
            "content_prop": str(item.get("content_prop") or "")[:64],
            "position": str(item.get("position") or "")[:64],
            "default": str(item.get("default") or "")[:160],
            "contract": [str(p)[:64] for p in (item.get("contract") or [])
                         if str(p).strip()][:STYLE_SLOT_CONTRACT_MAX],
            "contract_text": str(item.get("contract_text") or "")[:600],
            # 这个零件「现在能画什么」：写手逐镜从这里选内容值。白名单漏掉它，
            # 内容档位就只停在计划期，真正写代码的那一轮根本看不见。
            "content": [
                {"prop": str(c.get("prop") or "")[:64],
                 "values": [str(x)[:64] for x in (c.get("values") or [])
                            if str(x).strip()][:12],
                 "reference": [str(x)[:64] for x in (c.get("reference") or [])
                               if str(x).strip()][:6]}
                for c in (item.get("content") or []) if isinstance(c, Mapping)
            ][:10],
            # 绑定了替换件时，真正在画的是它——合约以它为准。
            "contract_live": str(item.get("contract_live") or "")[:600],
            "variants": [],
        }
        for v in (item.get("variants") or [])[:STYLE_SLOT_VARIANTS_MAX]:
            rec = _variant_record(v)
            if rec is not None:
                slot["variants"].append(rec)
        bound = _variant_record(item.get("bound"))
        if bound is not None:
            bound["component"] = comp
            slot["bound"] = bound
        out.append(slot)
    return out


def validate_code_spec(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """→ (规范化后的 spec, 契约错误清单)。错误清单非空即**这份 spec 不成立**。

    **它只管契约**：逐镜「这份东西能不能用」的那几件事——代码在不在、文案超不超
    字幕容量、高亮词是不是子串、枚举在不在白名单内、字段声明的形状对不对。
    这些不满足时渲起来没有意义，所以照旧抛。

    **整片判据不在这一层**（2026-10-16 并档）。闸⑦⑨⑩⑫⑮⑯⑲⑳ 与动效锚㉑ 全部走
    :func:`whole_film_gate_results` 那**一个**出口，由三个调用点
    （`plan_motion` 入库前 / `render_motion_code` 烧像素前 / `patch_motion_code`
    落盘前）附进各自的正常返回，**只报不拦**。搬家的理由写在那个函数的 docstring 里；
    一句话：判据原来只有一个**拦人**的出路，模型因此看不见它，只能撞上去。
    谁想自查同一份判据，用 `motion/selfcheck.py` 的 `check_film`（模型手里那个工具
    包的就是它）。

    代码**内容**对不对归闸①（容器里的 lint）与闸④（画面上有没有东西），史实对不对归
    出处闸与证据账——两者都不在本函数。

    签名里曾经有一个 ``has_stage_leg``（闸⑱「投递腿存在性」要它：客户端没有
    ``stage_asset`` 口时，声明了 ``assets`` 的片子在一帧不烧之前就该打回）。
    2026-10-16 随 ``assets`` 栏一起删了——**判据的对象没了，参数就没有意义**：
    留着它，调用方还得去问客户端要一个再也不用判的能力。
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["spec 必须是对象"]
    raw = expand_code_refs(raw)

    aspect_raw = raw.get("aspect") or "portrait"
    aspect = norm_aspect(aspect_raw)
    if not aspect:
        _err(errors, f"aspect={aspect_raw!r} 不支持（可用：{', '.join(ASPECTS)}；"
                     f"也认 16:9 / 9:16 / 1:1 / 1920x1080 / 横屏 / 竖屏 / 方屏）")
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
            # 2026-10-23 真机：模型把界面上的**人话标签**（「沉稳男声」）当机器名传进来，
            # 这里直接报错 → 整次分镜作废、重来一遍（那次重试烧了 326 秒）。
            # 一个纯展示字段不该有这种权力：能认出来就认（标签反查 → 关键词兜底），
            # 认不出才报错。`rate` 那一档早就是这么做的（静默归到 +0%），两边口径统一。
            fixed = normalize_voice(voice)
            if fixed:
                voice = fixed
            else:
                # 回执里把六个合法名字连同人话标签一起摆出来：模型常把用户原话里的
                # 描述（「沉稳男声」）当成音色名传进来，旧的「不是合法音色名」没给出路，
                # 它只能再猜一轮或干脆删掉这一维——2026-10-22 真机就多烧了一次调用。
                labels = KNOB_VALUE_LABELS.get("voice") or {}
                choices = "、".join(f"{v}（{labels.get(v, '')}）" for v in VOICES)
                _err(errors, f"voice={voice!r} 不是合法音色名；它在 schema 的枚举里必须是"
                             f"下面六个机器名之一：{choices}。拿不准就不传 voice，"
                             f"缺省是 zh-CN-YunjianNeural（男声·普通话）。")
        if not _RATE_RE.match(rate):
            # 2026-10-23 真机：模型传 `1.0`（倍率）→ 旧代码直接报错、整次分镜作废，
            # 那一次烧了 407 秒。能算就算，算不出才报错。
            fixed_rate = normalize_rate(rate)
            if fixed_rate:
                rate = fixed_rate
        if not _RATE_RE.match(rate):
            _err(errors, f"rate={rate!r} 要写成 +10% / -20% 这种形式（或者 1.0 这样的倍率）")
        else:
            pct = int(rate[:-1])
            if not -50 <= pct <= 50:
                _err(errors, f"rate={rate} 超出 ±50%")
            elif pct < 0:
                # 正常语速是下限（2026-10-20 用户要求）。历史分镜里写过的 -4%/-8%
                # 在这里静默归到 +0%，而不是打回重写：降速只会让整片更闷，归零
                # 不会破坏画面与文案；要「慢」是靠 `min_duration_sec` 留白。
                rate = "+0%"

    # 字幕两维（2026-10-16 起）：整片一档 + 逐镜可覆盖。
    # **2026-10-23 缺省改成 `by_shot`**：用户要「整个画面都交给 agent」，字幕是最后一
    # 块不由它决定的地方（其余七档全是母版 `CAPTION_LOOK` 里写死的常量，所以所有片子
    # 的字幕长得一模一样）。`outlined`（白字黑边）仍是合法档，随时可显式选回。
    caption_style = str(raw.get("caption_style") or "by_shot").strip().lower()
    if caption_style not in CAPTION_STYLES:
        _err(errors, f"caption_style={caption_style!r} 不支持"
                     f"（可用：{', '.join(CAPTION_STYLES)}）")
        caption_style = "by_shot"
    highlight_style = str(raw.get("highlight_style") or "plain").strip().lower()
    if highlight_style not in HIGHLIGHT_STYLES:
        # 2026-10-23 真机：模型传「关键词换色」——那是卡面上的中文标签。同一个坑
        # 与 voice / rate 一样：能认就认，别为一句标签作废整次分镜。
        fixed_hl = normalize_highlight(highlight_style)
        if fixed_hl:
            highlight_style = fixed_hl
        else:
            _err(errors, f"highlight_style={highlight_style!r} 不支持"
                         f"（可用：{', '.join(HIGHLIGHT_STYLES)}）")
            highlight_style = "plain"
    # 高亮词的自定义色（2026-10-16）。空串＝没钉，跟色板走（`captionLook()` 的 accent）。
    # 非空就必须是一条真颜色：它最终落在 CSS 的 `color` 上，一个认不出的值**不报错、
    # 只是静默变回缺省色**——「面板上明明设了红，渲出来还是色板那个色」是最难查的一类，
    # 所以这里收窄口径、打回并把账面清成空串（与逐镜 `bg` 那一支同规）。
    raw_color = str(raw.get("highlight_color") or "").strip()
    highlight_color = norm_highlight_color(raw_color)
    if raw_color and not highlight_color:
        _err(errors, f"highlight_color={raw_color!r} 不是 #rrggbb —— "
                     "高亮词的颜色只收十六进制（如 #ff8a3d）；空串＝跟色板走")

    texture = str(raw.get("texture") or "none").strip().lower()
    if texture not in TEXTURES:
        _err(errors, f"texture={texture!r} 没有对应的滤镜链（可用：{', '.join(TEXTURES)}）")
        texture = "none"

    # 整片底色那一格（2026-10-17）：卡面发 `none`/色板名，文字库面板那条老路发空串，
    # 两者同义（`norm_palette`）。认不出的名字**报错并清成 `none`**——与 `texture`
    # 那一支同规：留着它下游只会「认不出就不生效」，而「卡上选了牛皮纸、出来还是米白」
    # 是最难查的一类静默。
    #
    # 只在 `palettes.json` **读得到**时判名字（`every` 非空）：读不到那份文件时
    # `palettes()` 返回空表，那时报错会把整片打回在一个跟用户无关的理由上——
    # 这一档的诚实落点是「没指定」（下游 `norm_palette` 同样返回空）。
    palette_raw = str(raw.get("palette") or "").strip().lower()
    palette = norm_palette(palette_raw)
    every = palettes()
    # `none` 必须**先**排掉：它是卡面那一格的默认值（「不选」），`norm_palette` 把它与空串
    # 一起归一成 `""`——按下面这条判据就成了「认不出的名字」。
    # 2026-10-17 真机前修：卡面**每次都发 `none`**，于是每一份从计划卡下来的 spec 都会被
    # 自己打回一句「palette='none' 不是一套色板」，而可用清单里第一个就写着 `none`
    # （自相矛盾到读的人会怀疑是配额，不是这一栏）。test_code_spec 里那一支
    # `palette="none"` 就是它的守卫。
    if palette_raw and palette_raw != PALETTE_NONE and not palette and every:
        # 2026-10-23 真机：模型把**风格包的名字**（「铜版咖啡档案」）填进 palette →
        # 报错 → 整次分镜作废重来（那一次 407 秒）。风格包本来就会覆盖底色，
        # 所以这一栏填了个中文名，正确的落点是「没指定」，不是打回。
        #
        # 判据用「像不像一个色板 slug」而不是「认不认识」：ASCII 的拼错（`warm-cream`）
        # 仍然报错——那才是真填错了、需要人看见；中文短语一律静默落到 none。
        if palette_raw.isascii():
            _err(errors, f"palette={palette_raw!r} 不是一套色板（可用："
                         f"{PALETTE_NONE}（不指定）、{', '.join(sorted(every))}）")
        palette = ""
    # 存 `none` 而不是空串：卡面/契约/回执三处读的是同一个字面量。空串只是文字库面板
    # 那条老路的写法，在**落盘**这一层归一掉，免得下游又要认两种。
    palette = palette or PALETTE_NONE

    # 整片美术方向（2026-10-18）：只抄真写了的字段，写歪的枚举报错并丢掉那一栏。
    # 它跑在**校验的同一层**而不是闸里：这是契约（下游要按它注入提示词与调色参数），
    # 不是「画面好不好看」那种只报不拦的判据。
    art, art_errors = norm_art(raw.get("art"))
    errors.extend(art_errors)

    # 整片主角（2026-10-19）：与 `art` 同一层、同一条「只抄真写了的」纪律，
    # 但它**不报错**——越界的数就地夹回范围，认不出的朝向丢掉（理由见 norm_protagonist）。
    protagonist, _protagonist_errors = norm_protagonist(raw.get("protagonist"))

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
    if _is_num(span) and span_text not in TRANSITION_SEC_CHOICES and not normalized_none:
        # 数字落在档位之间（0.3 / 0.5 这种）就地吸到最近一档，不再打回整份分镜：
        # 转场是 0.2/0.4/0.6/0.9 四选一，差 0.1 秒不是一条「渲不出来」的错。
        # 代价：模型不会被告知吸到了哪一档——确认卡的逐镜接缝栏列的是生效值。
        # 字符串仍按档位严格收：那是在说「这个档名」，写错档名才需要它看见。
        # 正好落在两档中间（0.3）时就近取默认档：转场默认值是整片最常见的那一档。
        span_text = min(TRANSITION_SEC_CHOICES,
                        key=lambda c: (round(abs(float(c) - float(span)), 6),
                                       c != TRANSITION_SEC_DEFAULT))
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

    # 这个函数的职责是**契约**：字段、容量、类型、归一——「这份 spec 能不能用」。
    # 整片判据（闸⑦⑨⑩⑫⑮⑯⑲⑳ 与动效锚㉑）**不在这里**（2026-10-16 改）。
    # 这里原来是 `for checker in WHOLE_FILM_GATES: errors.extend(run_film_gate(...)[0])`
    # 再单独 extend 一次动效锚㉑——判据只有一个**拦人**的出路，
    # 于是模型看不到它，只能撞上去，而一次打回的代价是整轮重来。现在它们全部走
    # `whole_film_gate_results` 那一个出口，由三个调用点附进各自的正常返回，只报不拦。
    # 契约不满足时**照旧抛**：那不是判据，是输入本身不成立，渲起来也没有意义。
    qc = str(raw.get("qc") or QC_STRICT).strip().lower()
    if qc not in QC_MODES:
        _err(errors, f"qc={qc!r} 不支持（可用：{', '.join(QC_MODES)}）")
        qc = QC_STRICT
    # 2026-10-16：这里原来还并排着 ㉘（`check_reveal_regions`，镜级 `reveal` 栏的几何
    # 与时序合法性）与 ⑱（`check_stage_leg_present`，投递腿存在性）。两道闸的主语都是
    # 「一整幅 raster 插画上屏」：㉘ 拿 `reveal.canvas` 与库行 width/height 对账，
    # ⑱ 管的是有没有把素材字节投进渲染容器的那条腿。取材三条路拆掉，两道闸一起消失。

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
        "caption_style": caption_style,
        "highlight_style": highlight_style,
        "highlight_color": highlight_color,
        "qc": qc,
        # 有配乐时把镜头切点卡到拍上（缺省开；没有配乐或检不出稳定的拍时什么都不做）。
        "beat_sync": raw.get("beat_sync") is not False,
        "texture": texture,
        "palette": palette,
        # 没写就不出现这一栏：老片子的 spec 逐字节不变，缓存键也不变。
        **({"art": art} if art else {}),
        **({"protagonist": protagonist} if protagonist else {}),
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
    # 那几档「一开就按镜花钱」的布尔开关（见 SWITCH_KNOBS）：**活着穿过校验**，
    # 只抄真开的那几个。这一栏漏了，写入方的话就成了空话——`spec["review"] = True`
    # 过一趟校验回来什么都没有，而回执上写着「这趟没开」，看的人会去查判据、
    # 查通道、查模型，唯独不会怀疑「开关在半路上掉了」。
    for key in SWITCH_KNOBS:
        if _switch_on(raw.get(key)):
            spec[key] = True
    # 整片风格那一句（见 `spec_schema` 的 `style`）。上面那份字面量只抄认识的键，
    # 不在这里抄一遍就会被静默删掉——与 SWITCH_KNOBS 同一个坑。超长截断、不报错：
    # 这一句只是给人看与给模型自己对照的，为它打回一整份分镜不值。
    style = str(raw.get("style") or "").strip()
    if style:
        spec["style"] = style[:STYLE_MAX_CHARS]
    # 风格的**可执行拆解**（2026-10-18）：由 `motion/art_director.py` 在逐镜写码前补，
    # 也可以由模型自己交。与 style 同一个坑：必须在这里抄一遍，否则写手永远收不到。
    grammar = str(raw.get("style_grammar") or "").strip()
    if grammar:
        spec["style_grammar"] = grammar[:STYLE_GRAMMAR_MAX_CHARS]
    # 整片贯穿装置（见 `spec_schema` 的 `recurring_prop`）：自由一句，超长截断不报错。
    # 与 style 同一个坑——不在这里抄一遍，写手那一侧就永远收不到这件道具。
    prop = str(raw.get("recurring_prop") or "").strip()
    if prop:
        spec["recurring_prop"] = prop[:RECURRING_PROP_MAX_CHARS]
    # 按需加载的技能块（2026-10-23，见 `spec_schema` 的 `skill_sections`）。
    # **与上面几栏同一个坑**：不在这里抄一遍，`shot_writer._extra_sections()` 就永远
    # 读到空表，写手永远只拿默认那部分——这一栏写了等于没写。
    # 认不出的名字**静默丢掉**而不打回：它是「多给一点说明」的优化，不是契约，
    # 为一个拼错的块名拦下一整份分镜不值（与 `style` / `recurring_prop` 同一条取舍）。
    _secs = raw.get("skill_sections") or []
    if isinstance(_secs, str):
        _secs = [_secs]
    _picked = [str(x or "").strip().lower() for x in _secs]
    _picked = [x for x in _picked if x in SKILL_SECTION_NAMES]
    if _picked:
        spec["skill_sections"] = list(dict.fromkeys(_picked))
    # 片壳（2026-10-20）。与 `style` 同一个坑：上面那份字面量只抄认识的键，不在这里
    # 抄一遍，注入到 `shot_input_props` 的那一栏就永远是空的——写进 spec 也到不了渲染。
    # 名单只认 `SHELLS`：一个没注册的名字如果活着穿过校验，entry 会拿不到壳，
    # 镜头代码又按「有壳」写，结果是没有外框的整帧——比缺省不选壳更坏。
    shell = str(raw.get("shell") or "").strip().lower()
    if shell:
        if shell not in SHELLS:
            _err(errors, f"shell={shell!r} 不认识（可用：{', '.join(SHELLS)}）")
        else:
            spec["shell"] = shell
            label = str(raw.get("shell_label") or "").strip()
            if label:
                spec["shell_label"] = label[:SHELL_LABEL_MAX_CHARS]
    # 参考片素材（2026-10-20）：用户传了参考片/参考图时，写手要**看着**参考帧画，
    # 而不是只读一句「剪影」。它不是卡面旋钮，也不进风格包——素材 id 属用户，
    # 系统风格包不能写死别人的 id。只收 `mat-` 前缀，最多 4 条（每条抽 3 帧）。
    refs = raw.get("reference_materials")
    if isinstance(refs, str):
        refs = [refs]
    if isinstance(refs, (list, tuple)):
        clean_refs = [str(x).strip() for x in refs
                      if str(x).strip().startswith("mat-")][:4]
        if clean_refs:
            spec["reference_materials"] = clean_refs
    # 风格包（2026-10-19）：由 `plan_motion._attach_style_pack` 从 PG 注入，**不是用户
    # 自由值**——所以不进 `FILM_KNOBS`/`knob_props`（自由值进卡面会被计划校验打回），
    # 但必须在这里抄一遍，否则写手、导演、评审三处都收不到（与 `style` 同一个坑）。
    # body 超长截断不报错：说明书写多了不该让整份分镜交不上。
    pack = raw.get("skill_contract")
    if isinstance(pack, Mapping):
        pack_name = str(pack.get("name") or "").strip()
        manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
        if pack_name:
            try:
                manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
            except (TypeError, ValueError):
                manifest = {}
            spec["skill_contract"] = {
                "name": pack_name[:STYLE_PACK_NAME_MAX_CHARS],
                "display": str(pack.get("display") or pack_name)[:STYLE_PACK_DISPLAY_MAX_CHARS],
                "body": str(pack.get("body") or "").strip()[:STYLE_PACK_BODY_MAX_CHARS],
                "manifest": manifest,
                # 导入的技能（kind 不是 style）也当画面契约用。写手要靠这个标记
                # 区分「本片自己的风格包」与「外面来的说明书」，措辞不一样。
                "imported": bool(pack.get("imported")),
                # 这一片挂载了哪几份技能：写手的「可用参考」清单按它筛文件。
                # 归一化是**重建字典**，漏掉这个键等于把上游写的东西悄悄丢了
                # （2026-10-24 实测：skill_contract 有、parts 没了）。
                "parts": [str(x) for x in (pack.get("parts") or []) if str(x).strip()],
            }
            # 模板是**可执行层**，只认库里登记过的 import 说明符。用户自由值不可能
            # 走到这里（`style_pack` 由服务端注入），但白名单仍然抄一遍：没有它，
            # 写手与缓存键都拿不到模块名，动态模板等于没接上。
            tmpl = pack.get("template")
            if isinstance(tmpl, Mapping):
                module = str(tmpl.get("module") or "").strip()
                if STYLE_TEMPLATE_MODULE_RE.match(module):
                    names = [str(c)[:64] for c in (tmpl.get("components") or [])
                             if isinstance(c, str) and c.strip()]
                    spec["skill_contract"]["template"] = {
                        "name": str(tmpl.get("name") or "")[:STYLE_PACK_NAME_MAX_CHARS],
                        "display": str(tmpl.get("display") or "")[:STYLE_PACK_DISPLAY_MAX_CHARS],
                        "module": module[:STYLE_TEMPLATE_MODULE_MAX_CHARS],
                        "components": names[:32],
                        "usage": str(tmpl.get("usage") or "")[:STYLE_TEMPLATE_USAGE_MAX_CHARS],
                        "responsible": str(tmpl.get("responsible") or "")[:200],
                        "hash": str(tmpl.get("hash") or "")[:64],
                    }
                    slots = _slots_from_pack(tmpl)
                    if slots:
                        spec["skill_contract"]["template"]["slots"] = slots
    # 槽位替换（模型在分镜里写「主体换成鹿」的那一项）。服务端已经把它解成
    # `template.slots[].bound`；这里再留一份原指令，重写/复用时看得到用户要的是
    # 哪一件。它同样不是卡面旋钮，键值只当标识符用。
    overrides = raw.get("style_overrides")
    known: set[str] = set()
    pack_spec = spec.get("skill_contract")
    tmpl_spec = pack_spec.get("template") if isinstance(pack_spec, Mapping) else None
    if isinstance(tmpl_spec, Mapping):
        known = {str(s.get("component") or "") for s in (tmpl_spec.get("slots") or [])
                 if isinstance(s, Mapping)}
    if isinstance(overrides, Mapping) and known:
        clean: dict[str, str] = {}
        for comp, want in list(overrides.items())[:STYLE_OVERRIDES_MAX]:
            comp = str(comp or "").strip()[:64]
            if isinstance(want, Mapping):
                want = (want.get("variant") or want.get("name")
                        or want.get("label") or "")
            want = str(want or "").strip()[:64]
            # `default` / 组件自己的名字＝不换件；与执行期 `_bind_slots` 同一口径。
            # 模板里没有的组件也丢掉——写手照它 import 只会得到一个不存在的零件。
            if comp and want and want not in (comp, "default") and comp in known:
                clean[comp] = want
        if clean:
            spec["style_overrides"] = clean
    return spec, errors


def _palette_for_bg(bg: str) -> dict[str, str] | None:
    """拿一镜申报的底色反查它属于哪套色板（`palettes.json` 是唯一出处）。

    page/surface/field 三个槽位都认——镜头代码常用 surface 当底；槽位按 page → surface
    → field 的次序试，命中唯一一套就用它。

    **多套撞上同一个颜色时不猜**：`#ffffff` 是 modern-light / solar-pop / coral-burst /
    color-play 四套的 `surface`，按表序取第一个会把 coral-burst 的砖红强调色发成
    modern-light 的蓝——一处错色，而回执上看起来和真命中一模一样。撞上就返回 None，
    与「没申报底色」同一条退路（字幕条退回母版按亮度的二档）。
    读不到色板文件（返回空表）或都不命中（自造底色）也返回 None。
    """
    every = palettes()
    for slot in ("page", "surface", "field"):
        hits = [pal for pal in every.values()
                if str(pal.get(slot) or "").lower() == bg]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def _palette_of(shot: Mapping[str, Any], film_palette: str | None = None
                ) -> dict[str, Any] | None:
    """这一镜算哪套色板——**三层，从最具体的往下认**。

    1. **这一镜的代码里绑了色板**（`const palette = palettes["…"]`）：这一层最具体，
       也正是用户要的「个别镜可覆盖」——覆盖写在那一镜自己的代码里。
    2. **这一镜申报的 `bg`** 反查得出唯一一套：也是覆盖，只是没写色板名。
    3. **整片默认**（spec 顶层的 `palette`，用户在计划卡上挑的那一格）。

    三层都认不出返回 None——**不猜**。

    为什么先认代码绑定而不是先认整片默认：`bg` 在镜头 schema 里**可省略**（只有
    `bleed: true` 的镜必须给，闸⑲ 的 :data:`VARIETY_BG_SHARE` 也只要求 ≥25% 的镜申报），
    所以「拿 bg 反查色板」对多数镜根本不触发——一部片子里会是一半镜头的字幕条跟着色板、
    一半还是老的白底黑字粉高亮，比统一的老样子更花。而权威来源一直都在同一份代码里：
    :func:`shot_colors` 静态认得出顶上那句 ``const palette = palettes["…"]``，
    闸⑩ 与闸⑲ 本来就照它工作。这里用的是同一处，不新增第二个真相源。

    第 3 层是 2026-10-16 加的（整片 `palette` 那一格）：没有它的时候「整片一个调子」
    只能靠模型在二十份代码里各写一遍同一句绑定来维持——那就是用户说的
    「底色和字幕不一致」。整片默认落在这一层，**认得出这一镜自己的选择时它一个字节
    也不生效**，所以「个别镜可覆盖」是结构上的，不是靠约定。
    """
    named = str(shot_colors(str(shot.get("code") or "")).get("palette") or "")
    if named:
        pal = palettes().get(named)
        if isinstance(pal, dict) and pal:
            return pal
    bg = str(shot.get("bg") or "").strip().lower()
    if bg:
        pal = _palette_for_bg(bg)
        if pal:
            return pal
    film = norm_palette(film_palette)
    if film:
        pal = palettes().get(film)
        if isinstance(pal, dict) and pal:
            return pal
    return None


def _palette_name_of(shot: Mapping[str, Any],
                     film_palette: str | None = None) -> str:
    """这一镜**实际用上的**那套色板叫什么（认不出的返回空串）。

    与 :func:`_palette_of` **同一处判据**（`palettes()` 带 `lru_cache`，三层拿到的
    都是同一个 dict 对象，所以这里按身份反查名字）：两处各认一遍名字，
    「账上一套、画面上另一套」就是迟早的事。名字只给**读得出名字**的那些镜——
    认不出返回空串，**不编**。
    """
    pal = _palette_of(shot, film_palette)
    if not pal:
        return ""
    for name, every in palettes().items():
        if every is pal:
            return name
    return ""


def film_style_of(shot: Mapping[str, Any], *,
                  film: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """这一镜的风格档（R14）：**只带真申报了的键**。

    → `{}`，或 `{"bleed": bool, "bg": "#rrggbb", "paper": …, "ink": …, "accent": …}`
    的子集。`paper/ink/accent/bg` 不必是镜申报的，而是 :func:`_palette_of` 认出
    色板后**自动带出**的（底色 + 条底/字色/强调色四件）——认得出色板时这一镜的底色与
    字幕条就跟着那套色板走，治的是「换任何色板字幕永远是白底黑字粉高亮」与
    「绑了深色板、底还是米白」。母版只认色值不认色板名，出处仍只有 `palettes.json` 一份。

    ``film`` 是**整片那一份 spec**（可省）：给了的话，这一镜自己没绑色板、底色也反查
    不出色板时，落回整片默认的那一档（用户要的「整片一个字段作默认」）。省掉它时
    行为与 2026-10-16 之前一致——既有调用点不改也不会悄悄换像素。

    `bg` 与三件套的优先级**不同**：`bg` 仍然**申报了就以申报的为准**（那是这一镜明写的
    底色），没申报才取认出的那套色板的 `page`；三件套则只要 :func:`_palette_of` 认得出
    色板就给——认的是色板本身，与申没申报底色无关。

    为什么只带申报了的键、不补全成完整一份：这一份要**进缓存键**、还要**逐镜过 HTTP**
    （`code_render.shot_input_props` → 渲染容器）。补全之后 `{bleed: false, bg: ""}`
    也成了一个值，于是所有既有镜的键全部改变、整片白烧一次 Remotion 帧，而它们
    一个字节都没变——与 `shot_cache_key` 里 `film_style_of` 那一段是同一个教训。

    也正因为「在 / 值不同 / 不在」三态两两算出不同的键，这一处省下的只是无谓的坏键，
    不是判据：申报了 `bleed: false` 与没申报在母版那侧落同一个缺省，键不同只是多烧一次，
    不会把两种画面混成一个。
    """
    out: dict[str, Any] = {}
    bleed = shot.get("bleed")
    if isinstance(bleed, bool):
        out["bleed"] = bleed
    pal = _palette_of(shot, (film or {}).get("palette"))
    bg = str(shot.get("bg") or "").strip().lower()
    if not bg and pal:
        # 这一镜没申明底色 → 用它认出的那套色板的 `page`。这是「整片底色一致」真正
        # 落地的那一行：同一套色板的镜拿到同一个底色，逐镜覆盖的镜拿到自己那一套的。
        bg = str(pal.get("page") or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", bg):
        out["bg"] = bg
    if pal:
        for key, slot in (("paper", "surface"), ("ink", "text"), ("accent", "accent")):
            color = str(pal.get(slot) or "").strip().lower()
            if re.fullmatch(r"#[0-9a-f]{6}", color):
                out[key] = color
    # 字幕的字号系数与中英次序（2026-10-21）：这是**风格包的决定**，母版不再写死。
    # manifest 的 `caption` 块可以写 `{"size": 0.44, "order": "en-zh"}`，也可以只写其中
    # 一个键。同样只带声明了的键——它要进缓存键、逐镜过 HTTP，补全会让没声明过的片子
    # 整片白烧一次（与上面 `bleed`/`bg` 同一个教训）。
    cap_cfg = _manifest_caption(film)
    if cap_cfg:
        size = cap_cfg.get("size")
        if isinstance(size, bool):
            size = None
        try:
            size = float(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        if size is not None and math.isfinite(size) \
                and CAPTION_SIZE_MIN <= size <= CAPTION_SIZE_MAX:
            out["captionSize"] = round(size, 3)
        order = str(cap_cfg.get("order") or "").strip().lower()
        if order in CAPTION_ORDERS:
            out["captionOrder"] = order
        # 字块离画面底多远、字号硬上限（2026-10-21）：与 size/order 同一规矩——只在
        # manifest 声明了才带，缺席时母版落几何表/0.036H，没声明过的片子像素不变。
        for key, lo, hi in (("bottom", CAPTION_BOTTOM_MIN, CAPTION_BOTTOM_MAX),
                            ("cap", CAPTION_CAP_MIN, CAPTION_CAP_MAX)):
            raw_num = cap_cfg.get(key)
            if isinstance(raw_num, bool) or raw_num is None:
                continue
            try:
                num = float(raw_num)
            except (TypeError, ValueError):
                continue
            if math.isfinite(num) and lo <= num <= hi:
                out["captionBottom" if key == "bottom" else "captionCap"] = round(num, 4)
    return out


def _manifest_caption(film: Mapping[str, Any] | None) -> dict[str, Any]:
    """风格包 manifest 里的 `caption` 块（认不出返回空 dict，不抛）。

    允许两种写法：dict（API 写入）与一行 JSON 字符串（磁盘 SKILL.md 的 frontmatter
    只能一行一值）——与 `skill._to_skill` 解 `style_manifest` 同一口径。
    """
    if not isinstance(film, Mapping):
        return {}
    pack = film.get("skill_contract")
    if not isinstance(pack, Mapping):
        return {}
    manifest = pack.get("manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError):
            return {}
    if not isinstance(manifest, Mapping):
        return {}
    cap = manifest.get("caption")
    if isinstance(cap, str):
        try:
            cap = json.loads(cap)
        except (TypeError, ValueError):
            return {}
    return dict(cap) if isinstance(cap, Mapping) else {}


def shot_cache_key(shot: dict[str, Any], *, film: Mapping[str, Any],
                   master_hash: str, lockfile_hash: str,
                   film_start_sec: float | None = None) -> str:
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
    # 2026-10-16：这里原来还有 `assets_hash`（R2：素材字节进键，换图必须重烧）与
    # `reveal`（R12-T59：母版逐帧算 mask 的输入，改一个揭示时刻就得重烧）。两栏都随
    # 取材那三条路删了；**键少了两个字段不等于坏键**——键只是「这一镜的身份」，
    # 少一维就是少一维，不会把两个不同的镜算成同一个（代码字节那一段仍然是主项）。
    payload = {
        "code": str(shot.get("code") or ""),
        "knobs": knobs,
        "film": {k: film.get(k) for k in FILM_KNOBS},
        "master": master_hash,
        "deps": lockfile_hash,
    }
    # R14 风格档（满幅 / 底色）改的是**像素**，所以必须进键——写法与原来 `reveal`
    # 那一处一字不差：**只在真有这一栏时才加**。塞进 `SHOT_KNOBS` 或无条件加，都会让
    # `None` 也成为一个值，于是所有既有镜的键全部改变、整片白烧一次 Remotion 帧，
    # 而它们一个字节都没变。
    style = film_style_of(shot, film=film)
    if style:
        payload["film_style"] = style
    # 英文字幕（2026-10-21）画进切片里的第二行，改一个词就换像素，所以必须进键。
    # 与上面 `film_style` 同一条纪律——**只在真有这一行时才加**：无条件把 `""`/`None`
    # 塞进去，会让所有既有镜的键整体漂移、整片白烧一次，而它们一个字节都没变。
    text_en = str(shot.get("text_en") or "").strip()
    if text_en:
        payload["text_en"] = text_en
    # 片壳（2026-10-20）：壳名与那行小字都画进切片（外框由 entry 套在每一镜外面），
    # 换壳/换期号＝换像素，必须进键。与上面 `film_style` 同一条纪律——**只在真选了
    # 壳时才加**：无条件把 `None` 放进键，所有老片子的键会整体漂移、整片白烧一次。
    shell = str(film.get("shell") or "").strip()
    if shell:
        payload["shell"] = [shell, str(film.get("shell_label") or "").strip()]
    # 层E2（R19）：这一镜**覆盖**了字幕两维时也要进键。整片那两档本来就在 `FILM_KNOBS`
    # 里，所以只有「申报过覆盖」才加——不加的话「同一段代码、整片 paper 而这一镜
    # on_picture」会共用一份切片，纸条本该消失的那一镜会拿到带纸条的旧切片，
    # 而账面上一切正常（这正是「少算一个键就是拿旧片冒充新片」）。
    # 卡拍补长改的是帧数（像素与声轨都变了），只在真补了时进键。
    if shot.get("_beat_pad"):
        payload["beat_pad"] = round(float(shot["_beat_pad"]), 3)
    # 风格包（用户选定的整片契约）：画法最终落在代码字节里，但**换包而代码恰好相同**
    # 时仍不能共用切片——两份代码的 import 相同不代表语义相同（比如模板从注册表
    # 换了实现）。只带 name + template 两样，且只在真选了包时加：与上面 `film_style`
    # 同一条纪律，`None` 一旦进键，所有老片子的键会全部漂移、整片白烧。
    pack = film.get("skill_contract") if isinstance(film, Mapping) else None
    if isinstance(pack, Mapping) and str(pack.get("name") or "").strip():
        manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
        tmpl = pack.get("template") if isinstance(pack.get("template"), Mapping) else {}
        # 模块名 + 源码指纹：用户把模板重新沉淀一版（同名、同风格包）时，代码字节
        # 可能一模一样，但 import 的实现换了——只带名字会让旧切片继续冒充新模板。
        payload["skill_contract"] = [str(pack["name"]).strip(),
                                 str(manifest.get("template") or "").strip(),
                                 str(tmpl.get("module") or "").strip(),
                                 str(tmpl.get("hash") or "").strip()]
        # 槽位换了件：基模板没变、代码字节也可能一模一样，但 import 的实现不同。
        # 只在真有 `bound` 时加——没有绑定的老片子一个字节都没变，键不该漂移。
        bound = [[str(s.get("component") or ""),
                  str((s.get("bound") or {}).get("module") or ""),
                  str((s.get("bound") or {}).get("hash") or "")]
                 for s in (tmpl.get("slots") or [])
                 if isinstance(s, Mapping) and isinstance(s.get("bound"), Mapping)
                 and str((s.get("bound") or {}).get("module") or "").strip()]
        if bound:
            payload["style_slots"] = bound
        # 全片时钟的起点：模板里 film 作用域的轨道（「一直在走的人」）按它算步态，
        # 起点变一秒，切点处的相位就变——像素真的不同，必须进键。只在有风格包时
        # 加，理由同上：`None` 进键会让所有老片子的缓存整体漂移。
        if film_start_sec is not None:
            payload["film_start_sec"] = round(float(film_start_sec), 3)
    for k in ("caption_style", "highlight_style"):
        if shot.get(k):
            payload[k] = str(shot[k])
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 闸⑦ 整片层：每镜必画与文案具体项（在烧像素之前判，一次都不花）
# ---------------------------------------------------------------------------

#: **画法通道** → 认的 import 包名。只有「这一镜真的画了东西」的四条算通道：
#: 手绘斜纹（roughjs）、真三维（three / R3F）、公式（katex）、几何图形（裸 SVG 与
#: `@remotion/shapes`，判据在 markup 上而不是 import 上，见 ``_VECTOR_MARKUP_RE``）。
#:
#: ``@dg/motion``/``@dg/polish``/``@dg/lib``/``@dg/themes`` 那一类**不再算通道**：
#: 它们是动效件与色板，不是画面主体。「左边一张卡片 + 右边一段文字」配齐了
#: ``@dg/lib/ClipCard`` 与 ``@dg/themes``，画面上仍然什么都没有——旧表把它们计入
#: 通道数，正是「闸绿了而画面还是 PPT」的那条缝。它们照样在白名单里可以 import，
#: 只是撑不起闸⑦。
#:
#: **与技能正文「通道表」那张表逐行同源**（``tests/test_code_spec.py`` 第 ⑧ 节拿正文
#: 比对这些名字）。为什么判据是 import 而不是模型自报的一栏：自报会漂（写了
#: `style: rough` 而代码里一个 roughjs 都没有），而这条闸要拦的恰恰就是
#: 「说用了但其实没有」。
#: 简笔画零件库的**件清单**（源码 `remotion/src/lib/draw/`，别名 `@dg/lib/draw/*`）。
#: 每件回答「一个能被认出的东西由哪几笔构成」——人、机器、厂房、齿轮、图表…
#: （`remotion/src/lib/draw/Figure.tsx` 起头那段注释解释了它们能做到什么、做不到什么）。
#:
#: **只有这一份**，两处都从这里取：
#:   · `IMPORT_WHITELIST` —— 模型能 import（也与 `lint-rules.mjs` 的 `ALLOWED_IMPORTS` 对账）；
#:   · `PICTORIAL_CHANNELS["rough"]` —— 闸⑦「每镜必画」与闸⑯「主体具名」认它们是
#:     **手绘通道**（这些件底下走的就是 roughjs）。
#: 两处各写一份必然漂，而漂的那一侧正好是最要命的一侧：名单更新了、通道没更新，
#: 于是每一镜都被判成「只画了文字与色块」。
_DRAW_PARTS: tuple[str, ...] = (
    "@dg/lib/draw/Figure", "@dg/lib/draw/Face", "@dg/lib/draw/Machine",
    "@dg/lib/draw/Building", "@dg/lib/draw/Vehicle", "@dg/lib/draw/Gear",
    "@dg/lib/draw/Chart", "@dg/lib/draw/Arrow", "@dg/lib/draw/Coin",
    "@dg/lib/draw/Book", "@dg/lib/draw/Scale", "@dg/lib/draw/Clock",
    "@dg/lib/draw/Crown",
)

PICTORIAL_CHANNELS: dict[str, tuple[str, ...]] = {
    # 简笔画零件库的每一件都记在这一条上（2026-10-17）：它们底下就是 roughjs 的手绘线，
    # 只是把「锅炉 + 烟囱 + 飞轮」这类拆法预先写好。不记在这里，`<Machine/>` 画出来的
    # 东西在闸⑦ 眼里是「只有文字与色块」——判据认的是 import 名，不是产物的形状。
    "rough": ("roughjs", *_DRAW_PARTS),
    # 三维模板也算三维通道（2026-10-21）：镜头代码只 import `StudioStage` 时，
    # 它自己不写 `three` 三包，但画面确实是逐帧渲出来的三维场景。不从这里认，
    # 每一镜都会被闸⑦ 判成「只画了文字与色块」。
    "three": ("three", "@react-three/fiber", "@remotion/three",
              "@dg/lib/style/cinema-studio", "@dg/lib/style/surreal"),
    "katex": ("katex",),
}

#: 归类顺序：专门通道优先，免得一镜里既有三维又有裸 SVG 引线时被归错主通道。
#: （`vector` 不在这条链上——它判在 markup 上、不判 import，见 ``_VECTOR_MARKUP_RE``，
#: 所以 ``shot_style_channel`` 结尾单独给它一档。）
CHANNEL_ORDER = ("rough", "three", "katex")

#: 第四条通道（几何）：代码里真在画图形 —— 技能正文那句「（或裸 SVG）」的机器形态。
#: 判据**是写了画形状的东西**，不是 import：`<svg>`/`<path>` 之外的 `<div>` 加
#: `background` 只是色块，而色块正是「卡片+文字」的写法，所以它不算这一条。
_VECTOR_MARKUP_RE = re.compile(
    r"<\s*(svg|path|rect|circle|ellipse|line|polyline|polygon|g)\b"
    r"|\b(toPaths|toCurves|Path2D|linePath|polylinePath|shapeArc|shapeRect|"
    r"shapeCircle|shapePolygon)\(")

#: 会输出矢量标记、但代码里看不到 `<path>` 字面量的组件包（2026-10-18）。
#: 它们与裸 SVG 同属 `vector` 那一条：笔刷、纸纹、光池、场景层最终都落成
#: `<path>/<rect>/<filter>`，只是渲染发生在组件内部。
_VECTOR_COMPONENT_PKGS: tuple[str, ...] = ("@dg/lib/brush", "@dg/lib/scene",
                                           "@dg/lib/walk",
                                           "@dg/lib/style/silhouette-history",
                                           "@dg/lib/style/cinema-studio",
                                           "@dg/lib/style/surreal")

#: 第五条通道（2026-10-18）：`<canvas>` 上手写绘制。判据是**真拿了 2D 上下文**——
#: 光写一个 `<canvas>` 空标签不算（空白画布与色块一样，画面里没东西）。技能正文允许
#: 模型用 canvas 画逐帧变化的笔触/粒子/复杂路径；它和裸 SVG 是同一档「自己画」，
#: 只是 API 不同，所以闸⑦ 必须认它，否则一镜手写 canvas 会被判成「只有文字与色块」。
_CANVAS_RE = re.compile(
    r"\bgetContext\s*\(\s*['\"](?:2d|webgl2?|bitmaprenderer)['\"]")

#: 一条画法通道都不命中的那一类：只有文字与色块。技能正文里的「纯文字/纯色块镜」。
TEXT_CHANNEL = "text"

#: **每镜必画**认的那五条通道（闸⑦ 的判据）。图形片的画面只有一路——模型逐镜写代码
#: 画出来：`rough` 手绘斜纹 / `three` 真三维 / `katex` 公式 / `vector` 几何图形 /
#: `canvas` 手写 2D/WebGL 绘制。通道是**证明确实画了**，不是指定画法：用 `@dg/lib/brush`
#: 或裸写 SVG 都落在 `vector` 上，用 canvas API 画就落在 `canvas` 上。
#:
#: `image`（`staticFile`）**不在里面**：它不是画出来的。2026-10-16 起图形片不再有取材
#: 通道（不检索素材库、不出图、上传件不上屏），所以「一镜只摆了一张图」等于没画。
DRAWN_CHANNELS: tuple[str, ...] = ("rough", "three", "katex", "vector", "canvas")

#: 闸⑮（版式族）的起步镜数：镜数不到这一档，谈「族配额」连分母都不成立。
MIN_SHOTS_FOR_QUOTA = 6

#: 文案具体项的达标线（低于它只出提醒，理由见 :func:`check_draw_floor` 末段与
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
    # 手绘/场景件也算 `vector`：它们底下输出的就是 `<path>`/`<rect>`/`<filter>`，
    # 但这些标记出现在**组件里**，不在这段代码的字节里。不认它们，一镜纯
    # `<BrushStroke/>` 的画面会被闸⑦ 判成「只有文字与色块」——而那正是 2026-10-18
    # 手绘路线的主路。口径与上面几条相同：import 了还得在代码体里真的用上。
    for pkg in _VECTOR_COMPONENT_PKGS:
        got = bindings.get(pkg)
        if got is None:
            if pkg in set(code_imports(body)):
                found.add("vector")
            continue
        if not got or _uses_binding(body, got):
            found.add("vector")
    if _CANVAS_RE.search(body):
        found.add("canvas")
    return found


def shot_style_channel(code: str) -> str:
    """一镜归一条主画法通道：`rough` / `three` / `katex` / `vector` / `canvas` / `text`。"""
    all_ch = shot_used_channels(code)
    for name in CHANNEL_ORDER:
        if name in all_ch:
            return name
    if "vector" in all_ch:
        return "vector"
    return "canvas" if "canvas" in all_ch else TEXT_CHANNEL


def shot_all_channels(code: str) -> set[str]:
    """一镜用到的**所有**画法通道（不按优先级截断）。

    与 :func:`shot_style_channel` 的区别：那个按 ``CHANNEL_ORDER`` 只返回第一条
    匹配（主通道）。要「这一镜真用了哪几种」的场合就读这个——一条 three 场景配一圈
    裸 SVG 引线，两种通道都算，``rough+vector`` 互融的镜正是技能正文要的那种。
    """
    return shot_used_channels(code)


def shot_drawn(code: str) -> bool:
    """这一镜有没有**用代码画东西**——闸⑦「每镜必画」的判据。

    判据是**集合相交**（``shot_used_channels(code) & DRAWN_CHANNELS``），不是
    ``shot_style_channel(code) != TEXT_CHANNEL``：后者只报**主通道**（按 ``CHANNEL_ORDER``
    取第一条匹配），要「只要命中过一条画法通道就算画了」的语义，就该对全集求交，
    而不是对主通道求不等。五条画法通道见 ``DRAWN_CHANNELS``。
    """
    return bool(shot_used_channels(code) & set(DRAWN_CHANNELS))


def has_concrete_item(text: str) -> bool:
    """这一镜的文案里有没有一个**词法可见**的具体项。"""
    return any(r.search(text or "") for r in _CONCRETE_RES)


def style_facts(shots: list[dict[str, Any]], *,
                film: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """整片的风格与文案账（确认卡、出片回执、闸⑦⑨⑩ 三处读同一份，不各算一遍）。

    ``film`` 是**整片那一份 spec**（可省）：给了的话，账里 `palettes` 那一栏记的是
    这一镜**实际用上的**那套色板（自己绑的 → 自己底色反查的 → 整片默认），而不是
    「代码里写了没有」。这一栏 2026-10-17 起不再有任何判据读它（闸⑲ 的色板配额那一档
    删了，理由见 `check_style_variety`），它现在是**给人看的一份读数**：
    「这条片子的每一镜到底落在哪套色板上」只有这一处答得出来，
    而「整片一个调子」这件事本来就是要用眼睛核的。
    """

    ids = [str(s.get("id") or f"s{i + 1:02d}") for i, s in enumerate(shots)]
    codes = [str(s.get("code") or "") for s in shots]
    channels = {sid: shot_style_channel(code) for sid, code in zip(ids, codes)}
    all_channels = {sid: shot_all_channels(code) for sid, code in zip(ids, codes)}
    variety: set[str] = set()
    for chs in all_channels.values():
        variety |= chs
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
        # 闸⑦ 的账：一条画法通道都没用上的镜。**每镜必画**之下这一栏必须恒为空数组——
        # 那个空就是「每一镜都画了」的证据，所以它不从账上删掉。
        #
        # 判据直接调 `shot_drawn`——**与闸⑦ 同一个谓词**。它与上面那句
        # `channels[sid] == TEXT_CHANNEL` 在今天恒等（`image` 已从 `PICTORIAL_CHANNELS`
        # 摘掉，非画法通道一条不剩），但账要跟闸共用判据而不是共用结果：哪天再冒出一条
        # 「认得出但不作数」的通道，这一栏不会与闸⑦ 分家。
        "text_only": [sid for sid, code in zip(ids, codes) if not shot_drawn(code)],
        # 逐镜**实际用上的**色板名（读不出名字的记 None）、满幅镜、质感种数。
        # 认名字的口子只有一个：`_palette_name_of`（自己绑的 → 自己底色反查的 → 整片默认），
        # 与 `film_style_of` 注入的是同一处判据——两处各认一遍，「账上一套、画面上另一套」
        # 就是迟早的事。**不读模型自报的那一栏**：自报会漂，而这一栏要答的正是
        # 「说换色板了但其实没有」。
        "palettes": {
            sid: (_palette_name_of(s, film and film.get("palette")) or None)
            for sid, s in zip(ids, shots)},
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


# 2026-10-16 删掉了闸⑱（`check_stage_leg_present`：「声明了 `assets` 就必须真有投递腿」）。
# 它判的是「`shots[].assets` 声明了，而渲染客户端没有 `stage_asset` 口」——两边一起没了：
# `assets` 栏删了，投递腿本身（`remotion_client.stage_asset` 那个口、`code_render` 里
# 按 sha256 取字节的那一段）也删了。**这道闸的闸号不再被任何判据占用**，与 ⑪/⑤/⑬/㉗/㉘/㉛
# 同一处置：删掉的是闸，不是编号本身——将来若要新增一条闸，照旧用没被占用的号。


def check_draw_floor(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸⑦：**每镜必画**与文案具体项。→ (打回清单, 提醒清单)。

    图形片的画面只有一路——模型逐镜写代码画出来。所以判据是**逐镜**的，不是整片
    「够不够几种」：任何一镜的代码里一条画法通道（``DRAWN_CHANNELS``：rough / three /
    katex / vector）都没用上，那一镜就是纯文字/纯色块，当场打回并点名。

    为什么从「整片配额」翻成「逐镜底线」：配额拦的是「一条线画到底」，底线拦的是
    「这一镜根本没画」。后者才是「画面里有东西」这件事的地板，而地板不该有镜数门槛——
    旧配额表在 6 镜以下**整档不判**，那种长度的片子等于没有这一道闸。

    判据读**代码字节**（import 了还得在代码体里用得上，见 :func:`shot_used_channels`），
    在烧像素之前判——这一层红一次 = 一次分镜重做，不是几十分钟白烧。

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
    if not n:
        return errors, warns
    facts = style_facts(shots)
    ids = [str(s.get("id") or f"s{i + 1:02d}") for i, s in enumerate(shots)]
    # 判据直接调 `shot_drawn`，不从 `facts["text_only"]` 转一手：那一栏与这里今天同源
    # （它调的也是 `shot_drawn`），但闸是执法点、facts 是给回执与卡片读的汇总——闸的判据
    # 不绕着汇总走，哪天汇总那一栏变了口径，这道闸也不会跟着变。
    bare = [sid for sid, s in zip(ids, shots)
            if not shot_drawn(str(s.get("code") or ""))]
    if bare:
        errors.append(
            f"闸⑦ 每镜必画：{len(bare)}/{n} 镜没有用代码画东西（只有文字与色块）："
            f"{'、'.join(bare)}。画法不限——裸 SVG / `<canvas>` 上手写 / `three` / "
            "`katex` / `roughjs` / `@dg/lib/brush` 的笔刷都算画；判据只是"
            "「代码里真出现了画形状或画布的调用」。**只 import 不调用的那一行不算**。"
            "技能正文「画面丰富度合同」与「视觉手艺」两节写着这类镜该怎么救")
    hollow = facts["hollow"]
    if hollow and (n - len(hollow)) / n < CONCRETE_SHARE_TARGET:
        warns.append(
            f"文案提醒：{n} 镜里只有 {n - len(hollow)} 镜的文案带着机器认得出的具体项"
            f"（数字/引号术语/拉丁缩写），低于 {CONCRETE_SHARE_TARGET:.0%} 这条线。"
            f"这些镜没查到：{'、'.join(hollow)}。地名人名机器认不出，所以这一条只提醒不拦——"
            "但「每镜至少落一个具体项」是文案合同写的，逐镜看一眼再出片")
    return errors, warns


# ---------------------------------------------------------------------------
# 闸⑲ 风格配额（R19）：质感 / 满幅 / 底色——**逼丰富**的那一道
# ---------------------------------------------------------------------------
#
# 与 ⑦⑮⑯ 的分工：那三道判的是**反雷同**（通道别只一种、卡别同族扎堆、主体别写抽象词），
# 这一道判的是**逼丰富**。在它之前，质感 / 满幅 / 底色这三条**没有任何闸在判**，
# 于是一条 20 镜片可以全用一种质感 + 全常规版式而照样过闸——闸绿了，成片却是
# 「同一张 PPT 换了二十遍字」。
#
# 强度分两档，判据的性质不同（不是偷懒）：
#   · **结构判据（打回）**：满幅镜数。这件事在**代码字节**上就能数清（`bleed` 是归一
#     后的布尔栏），而且「一镜都没满幅」是**确定的**退化形态。
#   · **程度判据（只提醒）**：质感种数、`bg` 申报率。质感有「跟整片默认」这一态
#     （没申报 ≠ 没质感），底色也不是每镜都该有——把这两条做成硬闸就会误伤
#     「整片一档柔光 + 几镜换底色」这种合法配法。与闸⑨「宁可漏报，不许误报」同一纪律。
#
# 2026-10-17 删掉了「色板种数 ≥ ceil(n/4)」那一档。它与这一道其余三条**方向相反**：
# 那三条逼的是「别二十镜一个框、一个质感」，而它逼的是「别二十镜一个色」——
# 于是系统会奖励一部片子铺开六套底色，而用户在真机上要的恰恰是**整片一个底色**
# （「怎么做让它默认的整体风格一致?就是起码的底色和字幕保持一致」，紧接着的
# 决定是「整片一个字段作默认，个别镜可覆盖」）。它逼出来的东西正是用户要消除的东西，
# 所以删的是**这一档**，不是这一道：质感 / 满幅 / 底色三条原样留着。
# 换掉它的新载体是 spec 顶层那个 `palette` 字段（见 `FILM_KNOBS`），
# 以及账里逐镜读得出的 `style_facts()["palettes"]`——那条读数现在答的是
# 「每一镜实际落在哪套色板上」，人一眼看得出整片是不是一个调子。

#: 这一道从几镜起判。三镜片各画各的是常态，不是纪律（与 ⑦ 的起步镜数同一个道理）。
VARIETY_MIN_SHOTS = 6
#: 质感种数下限。
VARIETY_MIN_TEXTURES = 3
#: 满幅镜数下限 = ceil(n / 这个数)（20 镜 → 4 镜）。
VARIETY_BLEED_DIV = 5
#: `bg` 申报率下限（满幅镜另有一条更硬的：满幅**必须**给 `bg`，见判据 ③）。
VARIETY_BG_SHARE = 0.25


def check_style_variety(shots: list[dict[str, Any]]
                        ) -> tuple[list[str], list[str], list[str]]:
    """闸⑲ 风格配额：整片的**丰富度**够不够。→ (打回, 提醒, 未判)。

    三条判据，强度见上面那段（原来的第一条「色板种数」删了，理由也写在那一段里）：

    1. **满幅镜数** ≥ ``ceil(n / VARIETY_BLEED_DIV)``（结构 → 打回）。不写 `bleed` 时
       每一镜都被裁进同一条居中的画面带里，这正是成片「像 PPT」的结构原因（R14 把
       `bleed` 这条路开出来了，这一条是**逼它真的被用上**）。
    2. **质感种数** ≥ ``VARIETY_MIN_TEXTURES``（程度 → 只提醒）。质感有「跟整片默认」
       这一态，只数**申报过的**值，并把「没申报的镜数」写进提醒里——宁可漏报。
    3. **`bg` 申报率** ≥ ``VARIETY_BG_SHARE``，且**满幅镜必须给 `bg`**（程度 → 只提醒）。
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

    # ① 满幅镜数
    bleeds = facts["bleed_shots"]
    want_bleed = -(-n // VARIETY_BLEED_DIV)
    if len(bleeds) < want_bleed:
        errors.append(
            f"闸⑲ 风格配额：{n} 镜只有 {len(bleeds)} 镜满幅（`bleed: true`），"
            f"这一档至少要 {want_bleed} 镜。不写 `bleed` 时每一镜都被裁进同一条居中的"
            "画面带里（左右留页边距、上下留安全区）——二十镜二十个母题共用同一个框，"
            "这正是成片**像 PPT** 的结构原因。画整屏插画、满版描线、大场景、"
            "背景要铺满的镜头就写 `bleed: true`（满幅镜记得同时给 `bg`）")

    # ② 质感种数（只提醒）
    tex = facts["textures"]
    if len(tex) < VARIETY_MIN_TEXTURES:
        undeclared = sum(1 for s in shots if not str(s.get("texture") or "").strip())
        shown = "、".join(sorted(t or "跟整片默认" for t in tex)) or "none"
        warns.append(
            f"质感提醒：{n} 镜只用了 {len(tex)} 种画面质感"
            f"（{shown}），低于 {VARIETY_MIN_TEXTURES} 种这条线。"
            + (f"其中 {undeclared} 镜没写 `texture`（跟整片默认）。" if undeclared else "")
            + "质感是**打磨层**不是构图层（滤镜跑在拼接之前的那一趟逐镜切片上），"
              "换一档不重烧任何一镜的帧，所以「第 3 镜老电视、其余柔光」这种配法是免费的")

    # ③ 底色申报（只提醒；满幅那一条更硬）
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

#: 闸⑮：同一**族**最多几镜的**下限**。族 = 卡库里那个类别目录名（`card` 写成 `<类别>/<卡名>`）。
#: 实际上限按镜数放宽，见 :func:`layout_family_cap`。
LAYOUT_FAMILY_MAX_SHOTS = 2


def layout_family_cap(n: int) -> int:
    """n 镜的片子里同一族最多几镜：``max(LAYOUT_FAMILY_MAX_SHOTS, ceil(n/4))``。

    原先恒为 2（2026-10-17 改）：20 镜的片子要摊进 10 个族，而卡库一共 11 个族——一条讲
    「薄肌理论」的科普片被逼着用上 `interaction`（光标/协作）、`ui-entrance`（卡片入场）这类
    与题材无关的族，模型在记录里原话是「I need exactly 2 per family across 10 families」，
    结果每一镜一套视觉语言，用户看到的是「画面不连贯」。按镜数放宽到四分之一：20 镜每族
    最多 5 镜，题材对口的那几族（science / typography / data）够用，又不至于一族包场。
    「不同的卡 ≥ ceil(n/4) 张」那一条不动——它防的是同一张卡反复用，与连贯不冲突。
    """
    return max(LAYOUT_FAMILY_MAX_SHOTS, -(-int(n) // 4))

#: ⑯ 的通道白名单。**与 :func:`shot_style_channel` 的返回值是同一套词**（不多不少）——
#: 两处对得上，才谈得上「申报的通道」与「代码里认得的通道」逐字对账。`text` 不在里面：
#: 那是「一条画法通道都没命中」，2026-10-16 起由 ⑦「每镜必画」逐镜打回，
#: ⑯ 对它另有规矩（见 :func:`check_subject_named`）。`image` 也不在里面——图形片不再有
#: 取材通道（不检索素材库、不出图、上传件不上屏），申报它等于申报一件不存在的东西。
SUBJECT_CHANNELS: tuple[str, ...] = ("rough", "three", "katex", "vector", "canvas")

#: `subject` 的长度上下界（字符）。太短是「东西」「这个」这类指代，太长是整句话而不是
#: 名词短语——⑯ 要的是**能被认出的那个东西**的名字，不是论题。
SUBJECT_MIN_CHARS = 2
SUBJECT_MAX_CHARS = 24

#: 一镜的**实体清单**（2026-10-18）。文案里点到的人/物/地点/动作结果，一件一个名字。
#: 为什么要有：参考片里一帧同时有鲣鸟、巨型蛋、三枚蛋、草丛、落日——不是「一个主体
#: 占满画面」，而是**几件东西同处一个场景、彼此有空间关系**。只画一件，画面就退回
#: 「图标 + 字」。上限 5：再多不是一幅画，是图标墙。
MAX_ENTITIES_PER_SHOT = 5
#: 一镜最多点名几件风格能力。上限按「一镜最多同时成立几种做法」定：光、粒子、
#: 镜头、材质四类各一件已经够；再多是把整份正文搬回来，等于没做渐进式加载。
MAX_CAPS_PER_SHOT = 6
ENTITY_MAX_CHARS = SUBJECT_MAX_CHARS

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

    纯文字/纯色块镜由闸⑦「每镜必画」逐镜拦掉（:func:`check_draw_floor`），不在这里重算。
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
    cap = layout_family_cap(n)
    over = {f: v for f, v in fams.items() if len(v) > cap}
    if over:
        errors.append(
            f"闸⑮ 版式族配额：{n} 镜里 "
            + "；".join(f"{f} 族占了 {len(v)} 镜（{'、'.join(v)}）" for f, v in over.items())
            + f"——{n} 镜的片子同一族最多 {cap} 镜。族是卡库的类别目录名，"
            "同一族连着摆好几镜，观众看到的是同一套版式在重复。当前族分布："
            + "、".join(f"{f}×{len(v)}" for f, v in fams.items()))
    return errors, []


def check_subject_named(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸⑯：每镜 `subject` 具名、`subject_channel` 有证据。→ (打回清单, 提醒清单)。

    ⑯ 判的是「这一镜画面上**能被认出的那个东西**有没有被说出来」——机器认证词、人认证物，
    而判据只取机器判得动的那一半：

    - ``subject``：**建议每镜填**（回执与视觉评审都读它），但不是交付的硬条件：
      空着只提醒。填了但落在 :data:`ABSTRACT_SUBJECTS` 或长度越界，也只提醒——
      抽象词画不出具体形状这件事，现在由「静帧 → 看 → 改」那条路（``design_review``）
      按画面判，比在代码字节上猜得准。
    - ``subject_channel``：**不再强制**。申报了就按代码对账（:func:`shot_all_channels`），
      申报的通道必须真用上——自报无证据仍然打回，因为那正是「写着 rough 而代码里
      一个 roughjs 都没有」的形状；没申报则只提醒补一句，不拦。
    - 纯文字/纯色块镜：**只提醒，不打回**（2026-10-23 晚）。闸⑦「每镜必画」当天撤出
      整片判据（「画没画」属于审美，交给 `design_review` 按画面判），这条同义判据就
      没有主人了——留着 `_err` 等于通过 ⑯ 把 ⑦ 偷偷复活。而 ⑯ 这次加回整片判据的
      唯一理由是「**账实不符**」（申报了哪条通道就必须真用上），不是「画没画」。

    2026-10-18 松绑的依据：旧的硬要求（每镜必填 ``subject`` + 填了就必须申报通道）
    在真机上换来的是「为了填一格而重交”；而闸⑦ 已经保证每镜真画了东西，剩下的
    「画得像不像」本来就判不了——把它降成提醒，交给看画面那一层。

    ⑲-b **不用** ``subject_terms``：盲描述那条路一个词族也不需要，所以那一栏仍**不在**
    schema 里（回看环 :data:`review_loop.SUBJECT_TERMS_FIELD` 那段注释记着这笔账）。
    """
    errors: list[str] = []
    warns: list[str] = []
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        head = f"闸⑯ 主体具名：{sid}："      # 与 ⑦⑨⑩ 同形：闸号 + 镜号，打回清单能直接点名
        subject = str(s.get("subject") or "").strip()
        if not subject:
            warns.append(f"{head}subject 没填——建议用一句名词短语说清画面上那个"
                         f"「能被认出的东西」（如「活字盘」「一张 1900 年的地图」）。"
                         f"不填不影响交付，但回执与视觉评审会少一条参照")
        elif subject in ABSTRACT_SUBJECTS:
            warns.append(f"{head}subject={subject!r} 是抽象词（在拒绝名单里）——"
                         f"画面里画不出「理念」这种形状；换成一个具体器物"
                         f"（如「{_SUBJECT_HINT}」）更好判也更好画")
        elif not SUBJECT_MIN_CHARS <= len(subject) <= SUBJECT_MAX_CHARS:
            warns.append(f"{head}subject={subject!r} 长度 {len(subject)} 字，"
                         f"建议 {SUBJECT_MIN_CHARS}~{SUBJECT_MAX_CHARS} 字——太短是指代"
                         f"（「东西」「这个」），太长是整句话而不是主体名")
        # 通道：申报了就验证据；没申报只提醒。
        declared = str(s.get("subject_channel") or "").strip().lower()
        detected = sorted(shot_all_channels(str(s.get("code") or "")))
        if not detected:
            # 两种情形必须分开，混在一起就会把「账实不符」放走：
            # · **申报了通道、代码里一条都没用** → 打回（自报无证据，这正是 ⑯ 的职责）；
            # · **没申报、代码里也没画** → 只提醒。闸⑦「每镜必画」2026-10-23 撤了
            #   （「画没画」属于审美，交给 `design_review` 按画面判），这条安全网就
            #   没有主人了——留着 `_err` 等于通过 ⑯ 把 ⑦ 偷偷复活。
            if declared:
                _err(errors, f"{head}申报了 subject_channel={declared}，而这一镜代码里"
                             f"一条画法通道都没用上——自报的通道必须有证据。要么把这一镜"
                             f"的主体改成用 {declared} 画，要么按代码里真用的那条申报"
                             f"（真没画就把这一栏去掉）")
            else:
                warns.append(f"{head}这一镜的代码里只有文字与色块（一条画法通道都没用上）"
                             f"——闸⑦ 撤了，这一条不再是打回，但画面多半是空的，"
                             f"评审看一眼更准")
            continue
        if not declared:
            warns.append(f"{head}这一镜画了图形主体（代码里认得的通道："
                         f"{'、'.join(detected)}），但没填 subject_channel——"
                         f"建议按实际画法补一个：{'、'.join(SUBJECT_CHANNELS)}")
            continue
        if declared not in SUBJECT_CHANNELS:
            warns.append(f"{head}subject_channel={declared!r} 不在白名单"
                         f"（{'、'.join(SUBJECT_CHANNELS)}），这一栏按未申报处理")
        elif declared not in detected:
            _err(errors, f"{head}申报 subject_channel={declared}，而这一镜代码里用的是"
                         f"{'、'.join(detected)}——自报的通道必须有证据。要么把这一镜的主体"
                         f"改成用 {declared} 画，要么按代码里真用的那条申报")
    return errors, warns


# ---------------------------------------------------------------------------
# 闸㉝ 具名件（2026-10-17）：每一镜至少用上简笔画零件库里的一件
# ---------------------------------------------------------------------------


def drawn_parts(code: str) -> list[str]:
    """这一镜的代码真用上了哪几件 `@dg/lib/draw/*`（升序）。

    「用上」与画法通道同一套证据口径（:func:`shot_used_channels`）：**import 了还得在
    代码体里出现那个绑定名**。「`import { Figure } from '@dg/lib/draw/Figure'` 而 JSX
    里一个 `<Figure/>` 都没有」是凑配额最省事的写法，这道闸不认。
    """
    bindings = imported_bindings(code)
    return sorted(p for p in _DRAW_PARTS
                  if (names := bindings.get(p)) is not None and _uses_binding(code, names))


def check_named_parts(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """闸㉝（**2026-10-18 起不在整片判据里**，只在回执里报）：每一镜至少用上一件
    **简笔画零件库**的件。→ (打回清单, 提醒清单)。

    退出的理由见 ``WHOLE_FILM_GATES`` 上方那段注释：机器形态只剩「import 名出现了」，
    而过法可以是在角落摆一支 `<Arrow/>`——它拦不住「画得不像」，却会逼所有镜都依赖
    一套固定模板。用户明确要求不要再给一堆模板；「像不像」改由静帧视觉评审按画面判。
    这个函数保留，便于回执继续报「哪几镜用了零件、哪几镜没用」，但不进 ``WHOLE_FILM_GATES``。

    **它治的是用户那句「画的一点也不写实」。** 零件库
    （`remotion/src/lib/draw/`，别名 `@dg/lib/draw/*`）把「一个能被认出的东西由哪几笔
    构成」预先写好了：人、机器、厂房、齿轮、图表、天平、钟…。库在那里而没人用，
    于是真机上出现的是「180 个点拼成的一个椭圆，标注『19 世纪·富态』」——形状随意、
    认不出是什么。这一道判据的机器形态就是**「这一镜有没有一件具名的东西」**。

    **判据是静态的，判在烧像素之前**（与 ⑦ 同一档），所以它**判不出「像不像」**：
    它只认「用没用上库里那一件」。一个 `<Figure/>` 摆在画面角落、主体仍旧是那团点阵，
    这一道不响——那一格是盲描述那一路的活（见 :func:`check_subject_named` 末段）。
    把这条说清楚，是为了不让读回执的人以为「㉝ 绿了＝画得像」。

    **为什么是硬闸（不在 `SOFT_GATES` 里）**：用户对这一条的原话是「必需像」，
    而它在代码层永远可满足——库里没有合用的那件时，仍可自己用 `<path>` 画，
    只要**同时**用上库里一件（哪怕只是一支 `<Arrow/>` 指着自己画的那个东西）。
    一道永远可满足、且直接对应用户那句抱怨的判据，做成「建议」就等于没做。

    没有 code 的镜**跳过**：取材轮落在 `plan_motion` 的入口就被拦下了（`forage_scan`），
    走不到这里；真走到这里的空代码由契约那一层报「code 为空」，不在这儿喊第二遍。
    """
    errors: list[str] = []
    for i, s in enumerate(shots):
        code = str(s.get("code") or "")
        if not code.strip():
            continue
        if drawn_parts(code):
            continue
        sid = str(s.get("id") or f"s{i + 1:02d}")
        _err(errors,
             f"闸㉝ 具名件：{sid}：这一镜一件简笔画零件都没用上——库里没有**能被认出的"
             f"东西**，画面上就只有随手拼的形状。把这一镜的 `subject`（"
             f"{str(s.get('subject') or '没填')}）照着「外轮廓 → 标志物 → 3~6 个部件」"
             f"拆成库里的件拼出来：{', '.join(_DRAW_PARTS)}。库里的件不够用时，"
             f"仍要自己用 `<path>`/`<circle>` 补，但**同时也要用上至少一件**"
             f"（哪怕只是一支 `<Arrow/>` 指着你画的那个东西）——这一道管的是"
             f"「画面上有没有一件具名的东西」。用上库里件的镜，"
             f"`subject_channel` 就填 `rough`")
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
# 闸⑧ 字压图：画出来的那个东西被自己的字幕盖住
#
# ⑧ 原有三类命中（叠字 / 被画面带裁掉 / 引线悬空），**第四类 `overlap_subject`
# 2026-10-16 才加**——在此之前全仓没有一处代码在问「这块字是不是压在图上」：
# 母版量的图形盒只当读数上报（`scanned.subjects` / `subject_boxes`），从不与文字盒相交。
# 用户从真机上指出来的正是这一条（「画出来的字还会遮住图画，说了也没有用」）。
#
# 分工照旧：读数是 TS 侧量的（`tokens.LAYOUT_PROBE` 的阈值随读数一起回传在
# `thresholds` 里），这一层只把读数**判成红**，不另写一个 0.3——两把尺迟早会分叉，
# 而分叉之后没有任何测试会红。
# ---------------------------------------------------------------------------

#: 字压图那条打回理由里给的改写方向。给的是**两条出路**而不是「改小一点」：
#: 这个判据只报「这块字被盖住多少」，盒子该往哪挪是构图的事，说死了等于替模型排版。
_TEXT_OVER_SUBJECT_HINT = "把这块字移开，或者给它一条自己的衬底（底色纸条）再压在图上"


def check_text_over_subject(shots: list[dict[str, Any]], *,
                            scanned: Mapping[str, Any] | None = None
                            ) -> tuple[list[str], list[str], list[str]]:
    """闸⑧ 字压图：这一镜的字幕是不是压在画出来的图形上。→ (打回, 提醒, 未判)。

    **为什么没有「提醒」那一档。** 这条判据是纯几何的（交叠 ÷ 文字盒面积过不过线，
    读数是 TS 侧算好的），过线就是过线，没有「偏了一点」的中间态。给一条「略微压住」
    的建议，等于让模型去调一个 0.05 的量而代价是一整镜的重烧——净亏。

    **为什么只认 `overlap_subject` 这一种命中。** ⑧ 另外三类是「几何猜的」，
    真画面上都可能是合法设计（刻意叠字、指向画面外的暗示），
    `code_render.summarize_layout` 那段注释专门写了「误报的代价是模型与用户开始无视
    整条通道」。字压图不一样：一块字被图形盖掉一半，就是读不出，没有「刻意的」那种读法。

    **未判只有一种**：这一镜的版面没回读到（`status != "scanned"`）。
    注意 `scanned.texts == 0`（纯图元镜）**不是**未判——那一趟真扫了，只是这一镜没有
    文字元素，它结构上不可能犯这个错。把它写成未判会让每一镜纯图元的片子进回看环重烧，
    而那正是「把未判当通过」的反面错误：把「验过、没事」写成「没验」。
    """
    errors: list[str] = []
    undetermined: list[str] = []
    readings = scanned or {}
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        head = f"闸⑧ 字压图：{sid}："
        got = readings.get(sid) if isinstance(readings, Mapping) else None
        got = got if isinstance(got, Mapping) else None
        if got is None or got.get("status") != "scanned":
            undetermined.append(
                f"{head}这一镜的版面没回读到（闸⑧ 那一趟没扫成），这一格**没有量过**"
                f"——未判不是通过")
            continue
        th = got.get("thresholds") if isinstance(got.get("thresholds"), Mapping) else {}
        line = th.get("subject_overlap_ratio", "未知")
        for f in (got.get("findings") or []):
            if not isinstance(f, Mapping) or str(f.get("kind") or "") != "overlap_subject":
                continue
            ts = f.get("texts") if isinstance(f.get("texts"), (list, tuple)) else []
            who = "、".join(f"「{str(t)[:16]}」" for t in ts) or "这一镜的一处文字"
            # 坐标只在**四栏都在**时印：缺一栏时印一个半张表，读者只会照着它去算，
            # 而算出来的位置是错的（缺数时宁可不给，与 `_reading` 同一条道理）。
            box = f.get("box") if isinstance(f.get("box"), Mapping) else {}
            where = ""
            if all(k in box for k in ("x", "y", "w", "h")):
                where = (f"（这块字在带内 x={box['x']} y={box['y']}"
                         f" w={box['w']} h={box['h']}）")
            _err(errors, f"{head}{who} 压在画出来的图形上（这块字被盖住 "
                         f"{_reading(f.get('ratio'))}，判据 {line}）{where}"
                         f"——{_TEXT_OVER_SUBJECT_HINT}")
    return errors, [], undetermined


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

#: 徽章那条打回理由里给的改写方向。**这一句 2026-10-16 改过口径**：它原来指向技能正文
#: 「先取 `asset_role='part'` 的部件拼场景」——那是取材通道还在时的写法。今天没有部件
#: 可取，能扩成场景的只有自己画的代码，所以给的出路改成「在同一段代码里加件」。
_BADGE_HINT = "在同一段代码里再加两三件（背景、标注、对照物），把这个单件扩成一个场景"


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
                 f"把一个单件放大铺满，屏幕上就是一枚徽章或一块色斑，"
                 f"观众看到的是「图标」而不是「这一镜在讲什么」。"
                 f"这一镜要真的像场景：{_BADGE_HINT}（几件摆到不同位置、大小错开、"
                 f"彼此有前后或相邻关系），"
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


def _reading(value: Any) -> str:
    """一条**读数**写进告警里的样子：两位小数；不是数就明写「拿不到读数」。

    为什么不在缺数时印 `None`：那句告警是要发给模型看的，`None` 会被读成「零」
    或「一个叫 None 的值」，而事实是这一栏没量到——两种意思差得很远，
    而这条链上「没量到被写成量到了」是反复踩过的那一类错。
    """
    got = _as_float(value)
    return "拿不到读数" if got is None else f"{got:.2f}"


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
# 2026-10-16 删掉了闸㉘ 那一族（`REVEAL_DIRECTIONS` / `REVEAL_HOLD_MAX_MS` /
# `_REVEAL_FIELDS` / `_reveal_ints` / `_reveal_size` / `_reveal_ms` /
# `reveal_canvas` / `reveal_probe_frames` / `check_reveal_regions`）。
#
# 它们判的都是镜级 `reveal` 申报栏：这一镜的画面分几块、按什么次序露出来。
# 这条链的主语是一张**真 raster 插画**——`reveal.canvas` 要与库行 width/height
# 对账、母版按 directions 逐帧算掩码、㉗ 再到真帧上比色。而画面上屏那条路
# （检索素材库 / 出图 API / 上传件）整条拆掉了：图形片的每一镜都由模型写 TSX
# 代码画出来（闸⑦「每镜必画」），画面里没有「一整幅插画」这个东西。
#
# **闸号 ㉘ 与它的下半场 ㉗ 一起不再被占用**，与 ⑪/⑬/⑱/㉛ 同一处置。
# ---------------------------------------------------------------------------



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
                # sha16 让模型（与逐镜账）能指名「我读的是这一版的这份文件」。
                # 短哈希（前 16 位）而不是全量：这一栏是**给人指名用**的，不是校验和
                # ——要校验字节，渲染侧那一趟再按整份算。
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
                "技能正文「画面丰富度合同」那一节写着四类母题怎么选")
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


#: 八套色板的**中文名**。上面的 JSON 是色板取值的唯一出处，这里补的只是「它叫什么」——
#: 「文字库」面板上那一排色块要有一行字，而面板上出现的每一个中文词都只有一处出处，
#: 所以前端不许自己把 `deep-ocean` 翻成「深海蓝」。
#:
#: 与 `palettes()` 的键**一一对应**，由 ``tests/test_code_spec.py`` 逐项断言：
#: JSON 里加了新色板而这里没加，面板上会显示一个机器名（不红但是丑）；这里多写了一个
#: 已经没有的色板，那一行就是永远选不到的死条目。
PALETTE_LABELS: dict[str, str] = {
    "vintage-kraft": "复古牛皮纸",
    "deep-ocean": "深海蓝",
    "obsidian-violet": "黑曜紫",
    "modern-light": "现代浅色",
    "midnight": "午夜暗场",
    "solar-pop": "阳光波普",
    "coral-burst": "珊瑚撞色",
    "color-play": "彩块拼接",
}

#: 「不指定色板」那一档的名字——即 `tokens.ts` 的底色与按亮度二档的字幕配色。
#: 单独一个常量而不是塞进 :data:`PALETTE_LABELS`：它不是一套色板，而正是
#: 「没有色板」这件事在面板上的说法。
#: 措辞不再提「母版」：样张渲的是自包含的镜头代码，成片里这一档也是镜头代码
#: 落回 `TOKENS`（母版下线那一轮之后，没有「母版底色」这回事了）。
PALETTE_DEFAULT_LABEL = "默认（片子的底色）"

#: 「不指定色板」在**卡面**上的取值字面量。为什么用 `none` 而不是空串（2026-10-16）：
#: 计划卡上的每个开关都要有一个模型能**照抄**的取值，而空串在提示里是一个看不见的
#: 东西——`texture`/`transition`/`entrance` 三档的「不选」都写作 `none`，同一张卡上
#: 第四维另立一个空串写法只会多一处抄错的机会。文字库面板那条老路仍旧发空串
#: （`motion_caption_preview(palette="")`），两者**同义**，由 :func:`norm_palette` 归一。
PALETTE_NONE = "none"

#: 整片色板那一格的候选值：`none` + 八套色板（顺序与 :data:`PALETTE_LABELS` 一致）。
#: 它与 `texture` 同一族——**整片一个默认，个别镜可覆盖**（覆盖写在那一镜的代码里：
#: `const palette = palettes["…"]`，或者它自己申报的 `bg`）。
PALETTE_CHOICES: tuple[str, ...] = (PALETTE_NONE, *PALETTE_LABELS)


def norm_palette(value: Any) -> str:
    """整片色板取值归一 → 色板名，或 ``""``（= 不指定）。

    两种写法同义：卡面发 ``none``、文字库面板发空串。认不出的名字也返回 ``""``——
    **这里不报错**，报错在 :func:`validate_code_spec`（契约层，那里才拿得到 errors）；
    这一层只是「读一个整片取值」，与 :func:`_palette_for_bg` 的「认不出就不猜」同规。

    读不到 `palettes.json` 时 :func:`palettes` 返回空表，于是任何名字都归一成 ``""``
    ——那正是「这一档今天没判」的诚实落点，而不是把一整片打回。
    """
    name = str(value or "").strip().lower()
    if not name or name == PALETTE_NONE:
        return ""
    return name if name in palettes() else ""


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


@lru_cache(maxsize=1)
def sample_shot_code() -> str:
    """字幕样式样张的**底稿镜头代码**（`remotion/src/preview/caption-sample.tsx`）。

    它是一份合法镜头代码（过与模型那些同一套闸①），但不参与成片：只为
    `caption_preview.preview_job()` 渲样张时当画面底稿——成片里那条字幕由母版壳或
    镜头代码自己摆，与这份底稿无关。
    为什么另存一份、不复用 `user-shots/shot.tsx`：那一份是渲染容器**每次出片覆写的
    落点文件**（`remotion/server.mjs` 的 `SHOT_FILE`），真机上一镜正在渲的时候读它，
    读到的是别人那一镜的代码——样张会随机画成别的片子的某一镜。

    读不到返回空串（镜像里没 COPY 这份文件、或本地跑在别的目录下），
    `caption_preview.preview_job()` 据此不渲（回 `None`）：预览是增益，不是出片链路的一环。
    """
    p = _master_file("src/preview/caption-sample.tsx")
    if not p:
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


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


#: `<CaptionLayer` 的出现。只认标签名这一件事——属性、空格、换行都可能有，
#: 但元件名不会变（它就是从 `@dg/film` import 进来的那个符号）。
_CAPTION_LAYER_RE = re.compile(r"<\s*CaptionLayer\b")
#: 单独一行的整条 `<CaptionLayer />`。只认自闭合、独占一行的写法（模型/补丁最常见的
#: 形状）；带子元素或跨多行 props 的不碰——宁可漏删，不许把画面里的东西一起删掉。
_CAPTION_LAYER_LINE_RE = re.compile(
    r"^[ \t]*<\s*CaptionLayer\b[^>]*?/>[ \t]*\r?\n?", re.M)
#: `<CaptionText`：只画字、位置由镜头代码给的那一件。它同样是「这一镜自己画了字幕」。
_CAPTION_TEXT_RE = re.compile(r"<\s*CaptionText\b")
#: `<CaptionFrame`：画/字分带的外框（2026-10-18）。它内部画的就是 `<CaptionText/>`，
#: 对「这一镜自己画了字幕吗」是同一个答案；少了这一条，用户这一镜会多出母版底部那条。
_CAPTION_FRAME_RE = re.compile(r"<\s*CaptionFrame\b")


def caption_by_code(code: str) -> bool:
    """这段镜头代码**自己摆了整条字幕**吗（`<CaptionLayer/>`）。

    有两处在读它，判的是同一件事：闸㉜（申报了 `none` 却摆着——那段代码静悄悄不生效），
    与 `code_render.shot_input_props`（扫到就注入 `captionFromCode`，**母版那条让位**，
    免得同屏两条）。判据只此一份：两处各写一条正则，迟早会有一处漏掉某种写法
    （换行、带属性、`<CaptionLayer >`），而漏掉的那一次表现为成片上一闪而过的双字幕。
    """
    return bool(_CAPTION_LAYER_RE.search(code or ""))


def style_template_slots(spec: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """整片 spec → 风格包模板的槽位清单（没有风格包/模板时返回空表）。

    槽位里带 `text_kind` / `content_prop`（见 `style_templates.template_slots`）：
    `text_kind == "caption"` 的那件就是模板自己的主字幕。下游用它回答
    「这一镜已经有字幕了吗」「这块字是不是必须显式传内容」。
    """
    pack = (spec or {}).get("style_pack") if isinstance(spec, Mapping) else None
    if not isinstance(pack, Mapping):
        return []
    template = pack.get("template")
    if not isinstance(template, Mapping):
        return []
    slots = template.get("slots")
    if not isinstance(slots, list):
        return []
    return [s for s in slots if isinstance(s, Mapping)]


def template_caption_in_code(code: str,
                             template_slots: Sequence[Mapping[str, Any]] | None = None
                             ) -> bool:
    """这段代码渲染了模板里承担**主字幕**的组件吗。

    为什么需要它：模板会把字幕做成一个导出组件（参考片沉淀下来的那件），它画的也是
    字幕；上面两条正则只认 `@dg/film` 的 `<CaptionLayer/>` / `<CaptionText/>`，
    看不见模板组件。于是 `autofix_caption_layer` 以为这一镜没字幕、再补一条底部字幕，
    而 `shot_input_props` 的 `captionFromCode` 也不知道该让位——同屏两条字幕。
    真机 2026-10-19 菊花镜：模型为了躲开重复，只好把整片字幕档改成 `none`，
    确认卡于是显示「不上字幕」，而画面上其实有模板那条（且是参考片原文）。

    判据只读槽位里的 `text_kind` 与 `component`，不写任何一套模板的组件名。
    """
    for slot in template_slots or ():
        if not isinstance(slot, Mapping):
            continue
        if str(slot.get("text_kind") or "").strip().lower() != "caption":
            continue
        comp = str(slot.get("component") or "").strip()
        if comp and re.search(rf"<\s*{re.escape(comp)}\b", code or ""):
            return True
    return False


def missing_template_content_props(
        code: str,
        template_slots: Sequence[Mapping[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    """模板的文字槽位被渲染、却没传内容 prop → ``[(组件, prop, text_kind)]``。

    模板组件的 prop 都是可选的，缺省值是**参考片那一镜的内容**。真机 2026-10-19
    菊花镜：写手渲染了 `<StyleCaption ... />` 却没传 `text`，预览左上角显示的是
    参考片台词「到山西，下雪了。新靴，还是没舍得穿。」。这一条只做点名，不做拦截：
    分镜已经成型，用户可以选择接受；但缺省值不是空的这件事必须说出来。

    判据只读槽位里的 `content_prop` / `component` 与代码字节，不写任何一套模板的
    组件名。写成展开 props（`{...props}`）时判不了，按「没漏」处理，避免误报。
    """
    out: list[tuple[str, str, str]] = []
    for slot in template_slots or ():
        if not isinstance(slot, Mapping):
            continue
        comp = str(slot.get("component") or "").strip()
        prop = str(slot.get("content_prop") or "").strip()
        if not (comp and prop):
            continue
        found = re.search(rf"<\s*{re.escape(comp)}\b", code or "")
        if not found:
            continue
        tail = code[found.end():]
        # 文字组件的 props 里不会有箭头函数，第一个 `>` 就是标签结束。
        end = tail.find(">")
        attrs = tail[:end] if end >= 0 else tail[:600]
        if "..." in attrs:
            continue
        if not re.search(rf"\b{re.escape(prop)}\s*=", attrs):
            out.append((comp, prop, str(slot.get("text_kind") or "")))
    return out


def caption_any_by_code(code: str,
                        template_slots: Sequence[Mapping[str, Any]] | None = None
                        ) -> bool:
    """这一段代码自己画了字幕吗：整条 `<CaptionLayer/>`、画面里的 `<CaptionText/>`，
    或模板里承担主字幕的那个组件。

    与 :func:`caption_by_code` 的差别只在用途：闸㉜ 只问「申报了不上字幕却摆了整条」，
    所以只认整条；而母版让位（`shot_input_props` 的 `captionFromCode`）问的是
    「画面上已经有一条字了吗」——`<CaptionText/>` 画的也是同一条字幕，母版再画一次
    就是同屏两条。2026-10-18 起自排字幕是缺省写法，这条区分因此必须存在。
    2026-10-19 起模板组件也算一条：不传 `template_slots` 时行为与从前逐字一致。
    """
    return (caption_by_code(code)
            or bool(_CAPTION_TEXT_RE.search(code or ""))
            or bool(_CAPTION_FRAME_RE.search(code or ""))
            or template_caption_in_code(code, template_slots))


def check_caption_ownership(shots: list[dict[str, Any]]
                            ) -> tuple[list[str], list[str], list[str]]:
    """闸㉜：声明了「不上字幕」的镜，代码里就别再摆整条字幕。

    2026-10-16 起字幕成了可 import 的元件（`@dg/film` 的 `CaptionLayer` /
    `CaptionText` / `useCaptionTiming`），摆放位置归镜头代码。同一天字幕两维化
    （`caption_style` × `highlight_style`），`<CaptionLayer/>` 的**自守规则**跟着变成
    「`caption_style === 'none'` 时不画」——因为那一档的意思就是「这一镜没有字幕」。

    于是有一种**静默**错误：这一镜声明了不上字幕，而代码里摆着 `<CaptionLayer/>`。
    它自守不画，那条字幕永远不会出现，而在此之前契约层一句话都不会说。判据就是这一句：
    **代码摆了 `<CaptionLayer/>` 而这一镜申报的字幕样式是 `none`**。

    **这一条只判「这一镜显式申报了 `none`」那一种**，因为本函数的入参只有 `shots`——
    整片那一档在这里读不到。而整片 `none` 时不算错：代码不带参数地摆 `<CaptionLayer/>`
    （推荐写法）本来就是**把决定权交给注入的那一档**，用户在上不上字幕之间选，选到
    `none` 就是他要的结果，不是静默失败。

    摆 `<CaptionLayer/>` 不需要任何申报：`shot_input_props` 扫到它就注入
    `captionFromCode`，母版那条**让位**（见 :func:`caption_by_code`）。所以「摆了字幕却
    同屏两条」从结构上不可能发生，这一条也不必判它。要判的只剩上面那一种自相矛盾。

    不判反向（`none` 之外却没画字）：文本扫描分不清「没画」与「用别的方式画了」
    （手搓文字一直是合法的）。那一条交给样张与每镜的预览静帧——文案有没有落到画面上，
    一眼就能看见。

    `<CaptionText/>`（只有字，不画纸条底）不进这条判据：摆一个**画面里的字**是合法的，
    `typography` 那一族正在干这个，禁掉它会误伤整族。
    """
    errors: list[str] = []
    for i, s in enumerate(shots):
        if not caption_by_code(str(s.get("code") or "")):
            continue
        style = str(s.get("caption_style") or "").strip()
        if style != "none":
            continue
        sid = str(s.get("id") or f"s{i + 1:02d}")
        errors.append(
            f"闸㉜ 字幕归属：{sid} 申报了 caption_style=\"none\"（这一镜不上字幕），"
            "代码里却摆着整条 <CaptionLayer/>——它自守不画，那条字幕永远不会出现。"
            "要么把这一镜的字幕样式改成要画的那一档（贴画面 / 白字黑边 / 纸条底…），"
            "要么改用 <CaptionText/> 只摆画面里的字，要么把这处删掉")
    return errors, [], []


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


#: 用户原话里点名了别的字幕形态时，系统不再把整片拉回白字黑边。
#: 这些是**用户自己说的话**，优先级高于缺省；模型写的 `on_picture` 不在此列。
#:
#: 2026-10-22：这里原先写的是裸的「无底」，于是「无底色」（只说不想要背景条、
#: 并没说不要描边）也会命中——v3 文档里一句「白色，无描边、无底色」让整份分镜
#: 跳过了缺省收敛。现在只认「无底条」这种整档说法，不认「无底色」。
_CAPTION_REQUESTS = ("无底条", "贴画面", "不要描边", "不要黑边", "没有描边", "纸条",
                     "纸带", "大字", "横幅", "不要字幕", "不上字幕", "on_picture")

#: 用户明确要「词被点出来」时才保留关键词换色。白字黑边是**读得出来**的一档，
#: 它的字色本来就统一；混进几枚强调色的词，整条字幕就不像同一套排版了。
_HIGHLIGHT_REQUESTS = ("关键词", "高亮", "换色", "重点词", "变色", "强调词")

#: 中文否定词：判断「这个要求是被点名要的，还是被点名**不要**的」。
_NEGATIONS = ("不要", "不需要", "不用", "无需", "别", "取消", "去掉", "没有",
              "不必", "不希望", "不想要", "不加", "不换")


#: 紧贴词前那一小句以这些字开头时，整句是否定（「不换色」里的「不」与「换色」
#: 连在一起，`_NEGATIONS` 里的多字否定抓不到——2026-10-22 v3 文档就是这么写的）。
_CLAUSE_NEG_HEADS = "不没别勿"
#: 以否定字开头、意思却不是否定的常见词；命中就不算否定。
_NOT_NEGATING = ("不同", "不少", "不过", "不仅", "不管", "不如", "不断", "不久", "不但")
_CLAUSE_SPLIT_RE = re.compile(r"[，。；、,.;:!?！？：\s（）()\[\]「」『』\"'“”‘’]")


def _negated_before(before: str) -> bool:
    if any(neg in before for neg in _NEGATIONS):
        return True
    clause = _CLAUSE_SPLIT_RE.split(before)[-1]
    if not clause or clause[0] not in _CLAUSE_NEG_HEADS:
        return False
    return not any(clause.startswith(w) for w in _NOT_NEGATING)


def _asked_for(req: str, words: Sequence[str]) -> bool:
    """用户原话里是不是**肯定地**点了这些词；紧贴在词前的否定不算。

    只做 8 个字以内的近邻判：中文否定几乎都贴在词前（「不要关键词」「不加高亮」）。
    再远就分不清「不要 A，但要 B」里的 B——那种句子交给整句判断，不在这里猜。
    """
    for word in words:
        at = req.find(word)
        while at >= 0:
            if not _negated_before(req[max(0, at - 8):at]):
                return True
            at = req.find(word, at + len(word))
    return False


def autofix_caption_default(raw: Mapping[str, Any],
                            user_request: str = "") -> tuple[dict[str, Any], list[str]]:
    """字幕缺省收敛到「白字黑边」（2026-10-20 用户点名：默认黑边白字）。

    为什么由系统统一、而不是再在技能正文里写一句：真机片《从篝火到屏幕》
    （40 镜）里模型把整片写成 `on_picture`，同时 `highlight_style: color` 让
    没被点名的字用深棕色——深底 + 深字，成片里大半句读不出来；而且有几镜自排、
    有几镜交给 `<CaptionLayer/>`，同一部片像三个人的字幕。这一栏是**整片级**的
    缺省，不是创作选择：用户没点名别的样子时，统一成白字黑边（描边把字从任何
    底色上摘出来），逐镜的 `on_picture` 覆盖也一并去掉，让它继承整片。

    用户原话里明确说了「无底 / 贴画面 / 不要黑边 / 纸条 / 大字 / 横幅 / 不要字幕」
    时**一个字节都不动**——那是用户的选择，不是模型跑偏。其他逐镜值（`paper`、
    `top_banner` 等）也保留：那些是有理由的版式决定，不是同一种「读不出来」。
    """
    req = str(user_request or "")
    notes: list[str] = []
    out = dict(raw)
    # 用户点名了别的字幕**形态**时，整片那一档不动（这是他的选择）。但「字幕形态」
    # 与「关键词要不要着色」是两件事：2026-10-22 的 v3 文档写了「无描边、无底色」，
    # 原先这里直接 return，连 `highlight_style: color` 也一起放过了，用户看到的
    # 就是「贴画面 + 关键词换色」。两栏从此各判各的。
    caption_override = any(k in req for k in _CAPTION_REQUESTS)
    if not caption_override:
        top = str(out.get("caption_style") or "").strip().lower()
        if top == "on_picture":
            out["caption_style"] = "outlined"
            notes.append("整片字幕：on_picture（无描边）→ outlined（白字黑边），"
                         "深底上才读得出来；要换回无底请在对话里点名")
        elif not top:
            out["caption_style"] = "by_shot"
    # 白字黑边这一档的字色本来就是统一的：模型常把整片写成 `highlight_style: color`
    # （技能正文里的示例就是 `emphasis="color"`），成片里几枚词突然变成强调色，
    # 用户看到的就是「字幕不统一」（2026-10-20 真机）。用户没**肯定地**点名要关键词
    # （「不要关键词」「不加高亮」不算）时，把这一档收回 plain。
    hl = str(out.get("highlight_style") or "").strip().lower()
    if hl != "plain" and not _asked_for(req, _HIGHLIGHT_REQUESTS):
        # 2026-10-21：用户看过成片后点名「不要这种带关键字的」——白字黑边这一档
        # **整条字同一个白色**。不只是换色那一档：逐词点亮（karaoke）会让没念到的字
        # 淡下去、底色块与撕纸笔触会在字后面加东西，三种在深底上都比纯白难读，
        # 也都破坏了「一条字幕一个样」。所以这一档下**所有**强化方式都收回 plain；
        # 用户明确点名了（「关键词换色 / 高亮 / 逐词点亮」）就一个字节都不动。
        out["highlight_style"] = "plain"
        notes.append("整片字幕：取消关键词强调（highlight_style → plain），"
                     "整条字同色；要给关键词上色请在对话里点名")
    shots = out.get("shots")
    if isinstance(shots, list) and not caption_override:
        fixed: list[Any] = []
        changed: list[str] = []
        for i, shot in enumerate(shots):
            if not isinstance(shot, Mapping):
                fixed.append(shot)
                continue
            style = str(shot.get("caption_style") or "").strip().lower()
            if style != "on_picture":
                fixed.append(shot)
                continue
            row = dict(shot)
            row.pop("caption_style", None)       # 去掉覆盖 → 继承整片的「白字黑边」
            fixed.append(row)
            changed.append(str(shot.get("id") or f"s{i + 1:02d}"))
        if changed:
            out["shots"] = fixed
            notes.append("逐镜字幕覆盖 on_picture 已去掉、跟随整片白字黑边："
                         + "、".join(changed))
    return out, notes


#: 字幕有没有被逐镜烧进画面（母版 `<Caption>` 或代码里的 `<CaptionLayer/>`）——
#: 叠化时两边各带一条，xfade 会把它们叠成两行。判在**分镜期**，因为这是结构性事实。
_CAPTION_IN_CODE_RE = re.compile(r"<\s*(CaptionLayer|CaptionText|CaptionFrame)\b")


def autofix_caption_transition(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """叠化与「逐镜烧进画面的字幕」不能共存：xfade 会把两镜画面叠在一起，两条字幕也一起叠。

    2026-10-22 真机：分镜写了 `transition: dissolve`、0.6 秒，字幕又由母版/`<CaptionLayer/>`
    逐镜烧进切片。拼接层用 ffmpeg `xfade`，交叠的 0.6 秒里上一镜的字还亮着、下一镜的字
    已经入画——用户看到的是「有的重叠」。这不是某一镜写错字，是转场与字幕所在的层次不对。

    所以只改**会有两条字幕同时出现**的接缝：两侧都有字的，改成硬接（`transition: none`），
    其余接缝照用户选的档。改在分镜期、写进逐镜覆盖，拼接层照旧读同一份 `transition_plan`。
    回执里点名改了哪几处——不静默改观感。
    """
    out = dict(raw)
    shots = out.get("shots")
    if not isinstance(shots, list) or len(shots) < 2:
        return out, []
    top = str(out.get("caption_style") or "").strip().lower()
    if top == "none":
        return out, []

    def has_caption(shot: Mapping[str, Any]) -> bool:
        style = str(shot.get("caption_style") or top or "").strip().lower()
        if style == "none":
            return False
        code = str(shot.get("code") or "")
        # 有字就有字幕：母版那条也会烧进每一镜的切片（`captionFromCode` 只决定让不让位）。
        return bool(str(shot.get("text") or "").strip()
                    or _CAPTION_IN_CODE_RE.search(code))

    top_style = str(out.get("transition") or "none").strip().lower()
    fixed: list[Any] = []
    demoted: list[str] = []
    for i, shot in enumerate(shots):
        if not isinstance(shot, Mapping):
            fixed.append(shot)
            continue
        row = dict(shot)
        if i < len(shots) - 1:
            nxt = shots[i + 1]
            # 没写逐镜覆盖时，这一处跟整片档——不能只看 row 自己的那一栏。
            eff = str(row.get("transition") or top_style).strip().lower()
            if (isinstance(nxt, Mapping) and has_caption(row) and has_caption(nxt)
                    and eff not in ("", "none")):
                row["transition"] = "none"
                demoted.append(str(shot.get("id") or f"s{i + 1:02d}"))
        fixed.append(row)
    if demoted:
        notes = [f"转场：{ '、'.join(demoted) } 之后这几处两侧都有字幕，叠化会把"
                 "上下两条字幕叠在一起；已改为硬接（其余接缝照原档）。"
                 "要整片叠化，得先把字幕从逐镜画面里拿出去。"]
        out["shots"] = fixed
        return out, notes
    return out, []


#: 画面最底下留给字幕的那一条（设备像素），与 `remotion/src/tokens.ts` 的
#: `FILM_GEOMETRY[*].bottomSafe` 同值。这里只用来**说给模型听**（回执里那句「别在这里放东西」），
#: 不参与任何判据——判「压没压」的是渲染侧探针量出来的真实盒子，不是这个数。
CAPTION_ZONE_PX = {"portrait": 300, "landscape": 300, "square": 260}

_FILM_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*(['\"])@dg/film\2")
_LAST_IMPORT_RE = re.compile(r"^import\b[^;]*?from\s*['\"][^'\"]+['\"]\s*;?[ \t]*$", re.M)
_ROOT_CLOSE_RE = re.compile(r"^([ \t]*)</AbsoluteFill>", re.M)


# style 对象里允许**一层**嵌套花括号（`interpolate(t, [0.9, 1], [0, 1], { extrapolateLeft: … })`
# 这种写法真机上就有）；再深就认不出、不动——宁可漏删。
_DIV_OPEN_RE = re.compile(r"<div\b[^>]*?style=\{\{((?:[^{}]|\{[^{}]*\})*)\}\}[^>]*?>")
_DIV_TOKEN_RE = re.compile(r"<div\b[^>]*?(/?)>|</div\s*>")
_ABS_RE = re.compile(r"position\s*:\s*['\"]absolute['\"]")
_BOTTOM_RE = re.compile(r"\bbottom\s*:\s*(\d+(?:\.\d+)?)\b")
#: 一段 JSX 里出现这些就**不是纯字**：画出来的东西（svg、简笔画零件、图片）一律不碰。
_DRAWN_IN_RE = re.compile(r"<\s*(svg|path|circle|rect|line|polyline|polygon|img|Img|canvas|"
                          r"ThreeCanvas|[A-Z][A-Za-z0-9]*)\b")


def _strip_zone_captions(code: str, zone_px: int) -> tuple[str, list[str]]:
    """删掉镜头代码**自己画在字幕带里的那一行字**（补了 `<CaptionLayer/>` 之后它就是第二条字幕）。

    → (新代码, 被删掉的那几行字)。2026-10-17 真机片：14 镜里 10 镜都有一个通栏、
    `bottom: 96` 的 `<div>`，里面是模型自己写的一行文案缩写——没有元件替它上字幕时，
    这是它自己补的字幕。系统补上 `<CaptionLayer/>` 以后，它正好压在字幕带里，
    与真字幕叠成两行，用户看到的就是「字幕和画面重叠」。

    只认很窄的一种形状，宁可漏删不许误删：绝对定位、`bottom` 小于字幕带高度、
    里面**只有字**（出现 svg / 简笔画零件 / 图片 / 任何大写开头的组件就不碰）。
    删完以后 `<div>` 必须仍然配平，否则整镜原样返回（插不准就不动，回执里点名）。
    """
    removed: list[str] = []
    out = code
    while True:
        hit = None
        for m in _DIV_OPEN_RE.finditer(out):
            style = m.group(1)
            b = _BOTTOM_RE.search(style)
            if not (_ABS_RE.search(style) and b and float(b.group(1)) < zone_px):
                continue
            if m.group(0).rstrip().endswith("/>"):
                continue
            depth, close = 1, None
            for t in _DIV_TOKEN_RE.finditer(out, m.end()):
                if t.group(0).startswith("</"):
                    depth -= 1
                    if depth == 0:
                        close = t
                        break
                elif not t.group(1):
                    depth += 1
            if close is None:
                continue
            inner = out[m.end():close.start()]
            # `<CaptionText/>` 也是字，不是画出来的东西：把它从「画」的判据里摘掉。
            # 否则「一个定位盒子只包着 <CaptionText/>」会被当成图形保留下来，
            # 与 `CaptionFrame`/`CaptionLayer` 那条真字幕叠成重影。
            drawn_inner = re.sub(r"<\s*CaptionText\b[^>]*?/\s*>", "", inner)
            if _DRAWN_IN_RE.search(drawn_inner):
                continue
            words = re.sub(r"\{[^{}]*\}", " ", re.sub(r"<[^>]*>", " ", drawn_inner)).strip()
            if not words and _CAPTION_TEXT_RE.search(inner):
                words = "（<CaptionText/> 字幕）"
            if not words:
                continue
            hit = (m, close, " ".join(words.split()))
            break
        if hit is None:
            break
        m, close, words = hit
        start = out.rfind("\n", 0, m.start()) + 1
        if out[start:m.start()].strip():          # 同一行前面还有别的东西：只删这个元素
            start = m.start()
        end = close.end()
        nl = out.find("\n", end)
        if nl != -1 and not out[end:nl].strip():
            end = nl + 1
        out = out[:start] + out[end:]
        removed.append(words[:40])
    opens = len([t for t in _DIV_TOKEN_RE.finditer(out)
                 if not t.group(0).startswith("</") and not t.group(1)])
    closes = len(re.findall(r"</div\s*>", out))
    if removed and opens != closes:
        return code, []
    return out, removed


def _shot_caption_style(shot: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    """这一镜**生效**的字幕样式：镜级覆盖 > 整片 > 缺省（与 `shot_input_props` 同一条回落）。"""
    return str(shot.get("caption_style") or spec.get("caption_style") or "by_shot")


def autofix_caption_layer(spec: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """要上字幕的镜没摆 `<CaptionLayer/>` → 系统就地补上（import + 顶层收尾前一行）。

    → (修过的 spec 副本, 说明)。为什么由系统补、不打回、也不靠技能正文提醒
    （2026-10-17 真机片 `rj-e0d34b53`）：用户在「文字库」选了白字黑边 + 关键词换色，
    参数一路注入到每一镜，但 14/14 镜都没摆 `<CaptionLayer/>`、各自手画了一行浅色小字，
    于是选的那一档**一次都没上屏**。而唯一会读那一档的就是这个元件——补上它是确定的
    一步，与 `autofix_ink_contrast` 同理。反过来，同一天把「不摆就逐镜打回」写进技能正文，
    模型被吓得不敢交分镜，去一份份翻 demo，一连八条 run 都撞到迭代上限、零产物。

    不动的镜：字幕样式是 `none`（不上字幕）或 `by_shot`（字幕由代码自己设计，2026-10-23）；
    代码里已经有 `<CaptionLayer` 或自己用 `<CaptionText` 排字（那也读注入，是「字自己排」
    那条正路）；找不到顶层 `</AbsoluteFill>`（插不准就不插，说明里点名，让模型自己摆）。

    **不删**模型手画的字：真机那几行是文案的缩写、是它的画面设计，不是重复字幕。
    它若正好摆在字幕带里，会与补上的这条撞——那由预览静帧的版面读数点名（字压字）。
    """
    shots = spec.get("shots")
    if not isinstance(shots, list):
        return dict(spec), []
    if str(spec.get("shell") or "").strip():
        # 片壳模式：底部字幕归片壳，镜头代码里不许出现字幕件（渲染入口有片壳合约）。
        # 这里再补 `<CaptionLayer/>` 会与壳的字幕叠成两条，也会让这一镜在出片时被拒。
        return dict(spec), []
    zone = CAPTION_ZONE_PX.get(str(spec.get("aspect") or "landscape"), 300)
    # 模板自己的主字幕组件也算「这一镜已经有字幕」：看不见它就会再补一条
    # `CaptionLayer`，同屏两条（真机 2026-10-19 菊花镜）。槽位来自沉淀数据，
    # 没有风格包时是空表，行为与从前逐字一致。
    template_slots = style_template_slots(spec)
    fixed: list[str] = []
    missed: list[str] = []
    stripped: list[str] = []
    out: list[Any] = []
    for i, shot in enumerate(shots):
        if not isinstance(shot, Mapping) or not shot.get("code"):
            out.append(shot)
            continue
        code = str(shot["code"])
        sid = str(shot.get("id") or f"s{i + 1:02d}")
        if (_shot_caption_style(shot, spec) in ("none", "by_shot")
                or caption_any_by_code(code, template_slots)):
            # `by_shot`（2026-10-23）：字幕交给这一镜的代码自己设计 —— 既不补
            # `<CaptionLayer/>`（母版那条也不画，见 `FilmLayout.tsx` 的
            # `NO_MOTHER_CAPTION`），也**不删**写手画在字幕带里的字。它画成什么样、
            # 站在哪、怎么入场，全归它自己；成片质量由视觉评审按画面判。
            out.append(shot)
            continue
        closes = list(_ROOT_CLOSE_RE.finditer(code))
        if not closes:
            missed.append(sid)
            out.append(shot)
            continue
        # 最后一个 `</AbsoluteFill>` 是导出那个组件的顶层（辅助组件写在它前面）。
        last = closes[-1]
        indent = last.group(1)
        code = (code[:last.start()] + f"{indent}  <CaptionLayer />\n" + code[last.start():])
        film = _FILM_IMPORT_RE.search(code)
        if film:
            names = [n.strip() for n in film.group(1).split(",") if n.strip()]
            if "CaptionLayer" not in names:
                names.append("CaptionLayer")
                q = film.group(2)
                code = (code[:film.start()] + "import { " + ", ".join(names) + " } from "
                        + f"{q}@dg/film{q}" + code[film.end():])
        else:
            imports = list(_LAST_IMPORT_RE.finditer(code))
            at = imports[-1].end() if imports else 0
            line = "import { CaptionLayer } from '@dg/film';"
            code = (code[:at] + ("\n" if at else "") + line + ("" if at else "\n") + code[at:])
        # 它自己画在字幕带里的那一行字，现在是第二条字幕——删掉（见 `_strip_zone_captions`）。
        code, gone = _strip_zone_captions(code, zone)
        if gone:
            stripped.append(f"{sid}「{gone[0]}」" + (f"等 {len(gone)} 行" if len(gone) > 1 else ""))
        fixed.append(sid)
        out.append({**shot, "code": code})
    notes: list[str] = []
    if fixed:
        notes.append(
            f"字幕：{'、'.join(fixed)} 没摆 <CaptionLayer />，系统已在顶层补上（用户选的字幕样式"
            f"「{spec.get('caption_style') or 'on_picture'}」由它上屏）。字幕占画面最底下 {zone}px，"
            "那一条里别再放字和主体")
    if stripped:
        notes.append(
            f"字幕：{'；'.join(stripped)}——这几镜在字幕带里自己画了一行字，与补上的字幕叠成两行，"
            "已删掉（整句现在由字幕出）。要保留那句话，就把它挪到画面上部、当成标题来排")
    if missed:
        notes.append(f"字幕：{'、'.join(missed)} 找不到顶层 </AbsoluteFill>，没法自动补 "
                     "<CaptionLayer />——请在这几镜的顶层容器里自己摆一个，否则选的字幕样式不会上屏")
    return {**spec, "shots": out}, notes


#: `<CaptionFrame ...>` 的开标签；补 `caption={false}` 时插在标签名后面。
_CAPTION_FRAME_OPEN_RE = re.compile(r"<\s*CaptionFrame\b")
#: 带 `text=` 的 `<CaptionText>` 是**另一行字**（图注、招牌），不是重复字幕；
#: 只有不带 `text` 的那一份才是把注入的整句又画了一遍。
_CAPTION_TEXT_WITH_TEXT_RE = re.compile(r"<\s*CaptionText\b[^>]*\btext\s*=")
#: 整个 `<CaptionText>` 元素（自闭合或空子元素）。只用来删**不带 `text=`** 的那一份
#: （判据见上面的 `_CAPTION_TEXT_WITH_TEXT_RE`）；带内容的写法不动。
_CAPTION_TEXT_ELEMENT_RE = re.compile(
    r"<\s*CaptionText\b(?P<attrs>[^>]*?)(?:/\s*>|>\s*</\s*CaptionText\s*>)", re.S)
#: 已经显式写了 `caption` 的不再插第二个。
_CAPTION_FRAME_CAPTION_PROP_RE = re.compile(r"<\s*CaptionFrame\b[^>]*\bcaption\s*=")


def _strip_bare_caption_text(code: str) -> tuple[str, int]:
    """删掉不带 `text=` 的 `<CaptionText/>`（把注入的整句又画了一遍的那一份）。

    只动元素本身；外面用来定位的 `<div>` 留着也无害（空盒不出像素）。
    带 `text=` 的是另一行字（图注、招牌），不在这里删。
    """
    gone = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal gone
        if re.search(r"\btext\s*=", m.group("attrs") or ""):
            return m.group(0)
        gone += 1
        return ""

    return _CAPTION_TEXT_ELEMENT_RE.sub(repl, code), gone


#: 整屏 `<feTurbulence>` 颗粒：一个 `<filter>` 里只有噪声、被一块铺满画幅的 `<rect>`
#: 引用。2026-10-20 实测（1080p、软件 GL、同一镜 4 帧）：带整屏噪声 39.2 秒，换成
#: 静态贴图 8.3 秒——噪声在**每一帧**都重算一遍，而它的输出一帧都没变过。
_TURB_FILTER_RE = re.compile(
    r'<filter\b[^>]*\bid="(?P<fid>[^"{}]+)"[^>]*>(?P<body>.*?)</filter>', re.S)
_RECT_EL_RE = re.compile(r"<rect\b[^>]*/>", re.S)
_IMPORT_LINE_RE = re.compile(r'^\s*import\s+[^;\n]+;\s*$', re.M)


#: 「铺满画幅」的宽／高只认这几种写法：`{width}`、`{W}`、`"100%"`。
#: 带算术的（`width * 0.2`、`width - 8`）一律不算——差一点也是差，宁可不换。
_FULL_W_RE = re.compile(
    r'\bwidth\s*=\s*(?:\{\s*(?:width|W|vw)\s*\}|\{\s*"100%"\s*\}|"100%")')


def _grain_rect_is_fullscreen(el: str) -> bool:
    """这块 rect 是不是铺满画幅（宽高就是画面宽高，而不是写死的数或算出来的比例）。"""
    return bool(_FULL_W_RE.search(el)
                and re.search(r'\bheight\s*=\s*(?:\{\s*(?:height|H|vh)\s*\}'
                              r'|"100%")', el))


def _grain_props(source: str, rect: str) -> str:
    """从原 rect / 噪声元素上抄下能保持画面的几个值（透明度、种子）。"""
    props: list[str] = []
    op = re.search(r'\bopacity=\{([^}]+)\}', rect) or re.search(
        r'\bopacity="([^"]+)"', rect)
    props.append(f"opacity={{{op.group(1).strip() if op else '0.5'}}}")
    seed = re.search(r'\bseed="(\d+)"', source)
    if seed:
        props.append(f"seed={{{seed.group(1)}}}")
    # 原来的整屏噪声没有混合模式（normal）；`Grain` 缺省是 overlay，照抄会变亮。
    props.append('blend="normal"')
    return " ".join(props)


def _grain_fix_code(code: str) -> tuple[str, int]:
    """把「整屏 feTurbulence 滤镜」换成一个静态贴图层 `<Grain/>` → (新代码, 换了几处)。

    只认很窄的一种形状：`<filter id=…>` 里**只有**噪声、且被一块铺满画幅的 `<rect>`
    引用，且这个 filter 没有被别处用到。动态噪声（参数里有 `{…}`）不动——那是模型
    刻意在动的纹理。名字已经被占（代码里自己定义了 `Grain`）也不动。
    """
    if "feTurbulence" not in code:
        return code, 0
    # 代码里自己定义了一个 `Grain`（或从别处引来同名），再加 import 就是重复声明。
    if re.search(r"\b(?:const|let|var|function|class)\s+Grain\b", code):
        return code, 0
    imported = bool(re.search(r'import\s+[^;]*\bGrain\b[^;]*;', code))
    done = 0
    guard = 0
    while guard < 4:
        guard += 1
        changed = False
        for fm in list(_TURB_FILTER_RE.finditer(code)):
            fid = fm.group("fid")
            body = fm.group("body")
            if "feTurbulence" not in body or "{" in body:
                continue
            rects = [m for m in _RECT_EL_RE.finditer(code)
                     if f'filter="url(#{fid})"' in m.group(0)
                     and _grain_rect_is_fullscreen(m.group(0))]
            uses = len(re.findall(
                rf'filter="url\(#{re.escape(fid)}\)"', code))
            if not rects or uses != len(rects):
                continue
            rect = rects[0].group(0)
            props = _grain_props(body, rect)
            at = code.find(rect)
            close = code.find("</svg>", at)
            if close < 0:
                continue
            # 先在 `</svg>` 后面放一个占位符，再删原件：用下标算位移，删两段就会错。
            close += len("</svg>")
            marker = "\u0000DG_GRAIN\u0000"
            code = code[:close] + marker + code[close:]
            code = code.replace(rect, "", 1)
            code = code.replace(fm.group(0), "", 1)
            # `<Grain/>` 是 AbsoluteFill，不能放进 svg 里——插在它所在的那个 svg 之后。
            code = code.replace(marker, f"\n      <Grain {props} />", 1)
            done += 1
            changed = True
            break
        if not changed:
            break
    if not done:
        return code, 0
    # 补 import：已经引了 Grain 就不动；引了 brush 但没引 Grain 就把名字加进去。
    if not imported:
        brush_import = re.search(
            r'import\s+\{([^}]*)\}\s+from\s+"@dg/lib/brush";', code)
        if brush_import:
            names = [n.strip() for n in brush_import.group(1).split(",") if n.strip()]
            names.append("Grain")
            code = code.replace(
                brush_import.group(0),
                'import { ' + ", ".join(names) + ' } from "@dg/lib/brush";', 1)
        else:
            imports = list(_IMPORT_LINE_RE.finditer(code))
            at = imports[-1].end() if imports else 0
            code = code[:at] + '\nimport { Grain } from "@dg/lib/brush";' + code[at:]
    return code, done


def autofix_film_grain(spec: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """整屏 `<feTurbulence>` 颗粒 → 静态贴图 `<Grain/>`（只换画法、不减质感）。

    2026-10-20 真机：36 镜每一镜都手写了一块铺满画幅、挂着噪声滤镜的 `<rect>`。
    噪声是静态的（固定 seed），但 Chromium 在逐帧渲染里**每帧重算一次**，1080p 下
    一帧要多花好几秒——实测同一镜 4 帧 39.2s vs 8.3s。整条片 36 镜就是四十多分钟
    里的一大半。`<Grain/>`（`@dg/lib/brush`）用的是一张固定 seed 的噪声贴图，
    浏览器只栅格化一次。

    与别的 autofix 一样：不改判据、不打回；认不出的形状原样留着。
    """
    shots = spec.get("shots")
    if not isinstance(shots, list):
        return dict(spec), []
    fixed: list[str] = []
    out: list[Any] = []
    for i, shot in enumerate(shots):
        if not isinstance(shot, Mapping) or not shot.get("code"):
            out.append(shot)
            continue
        code, n = _grain_fix_code(str(shot["code"]))
        if n:
            fixed.append(str(shot.get("id") or f"s{i + 1:02d}"))
            out.append({**shot, "code": code})
        else:
            out.append(shot)
    notes: list[str] = []
    if fixed:
        notes.append(
            f"颗粒：{'、'.join(fixed)} 的整屏噪声滤镜换成了 <Grain/>"
            "（同一张图每帧重算一遍噪声很慢；换成的贴图只栅格化一次，画面上仍是同样的颗粒）")
    return {**dict(spec), "shots": out}, notes


def autofix_duplicate_caption(spec: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """`CaptionFrame` 里已经有一条字幕，代码又摆了一份 `<CaptionText/>` → 删掉第二份。

    2026-10-19 真机：模型按旧技能正文用 `<CaptionFrame>` 分带，又在它外面套了一个
    `bottom: 0.035` 的盒子摆 `<CaptionText/>`；两条同一句字错开几像素，成片上就是
    「字幕重影」。提示词已经改成「叠在画面上」的缺省写法，但模型仍可能两种都写——
    这一步是**确定性的兜底**，与 `autofix_caption_layer` 同一套思路：不改判据、不打回。

    先尝试把字幕带里那个定位盒子整段删掉（`_strip_zone_captions`，只认「只包着
    `<CaptionText/>`」这一种很窄的形状）；删不掉时给 `<CaptionFrame>` 补
    `caption={false}`——画面区仍按分带排，但字只留模型自己摆的那一份。

    已经显式写了 `caption` 的镜整镜不动：`caption={false}` + 一份 `<CaptionText/>`
    是**模型自己选的一条字幕**，不是重影，删了就把字幕删没了。
    """
    shots = spec.get("shots")
    if not isinstance(shots, list):
        return dict(spec), []
    zone = CAPTION_ZONE_PX.get(str(spec.get("aspect") or "landscape"), 300)
    slots = style_template_slots(spec)
    frame_fixed: list[str] = []
    template_fixed: list[str] = []
    layer_fixed: list[str] = []
    out: list[Any] = []
    for i, shot in enumerate(shots):
        if not isinstance(shot, Mapping) or not shot.get("code"):
            out.append(shot)
            continue
        code = str(shot["code"])
        sid = str(shot.get("id") or f"s{i + 1:02d}")
        # ①′ 模板字幕 + `<CaptionLayer/>`。2026-10-22 真机：模板槽位没标
        # `text_kind=caption`，`autofix_caption_layer` 看不见 `StyleCaption`，
        # 又补了一条 `CaptionLayer`——10 镜里 6 镜同屏两条字。槽位数据已经补上
        # （`templates.json` 的 `facets.text`），这里再留一道确定性兜底：
        # 两件都在时只留模板那一条。
        if template_caption_in_code(code, slots) and _CAPTION_LAYER_LINE_RE.search(code):
            new_code = _CAPTION_LAYER_LINE_RE.sub("", code)
            if new_code != code:
                layer_fixed.append(sid)
                out.append({**shot, "code": new_code})
                continue
        # ① 模板字幕 + 另一份不带 `text=` 的 `<CaptionText/>`。
        # 模板里承担主字幕的那一件（`text_kind=caption` 的槽位）本身就是这一条字幕；
        # 写手再套一份 `<CaptionText/>`，两件都画字，位置差几像素就是重影。
        # 2026-10-19 真机：13 镜全部同时渲染了 `StyleCaption` 与 `<CaptionText/>`，
        # 左上角白字与橙字叠在一起。只删第二份；模板那件留着（它是风格契约）。
        if (template_caption_in_code(code, slots)
                and _CAPTION_TEXT_RE.search(code)
                and not _CAPTION_TEXT_WITH_TEXT_RE.search(code)):
            new_code, gone = _strip_bare_caption_text(code)
            if gone:
                template_fixed.append(sid)
                out.append({**shot, "code": new_code})
                continue
        if not (_CAPTION_FRAME_RE.search(code)
                and _CAPTION_TEXT_RE.search(code)
                and not _CAPTION_TEXT_WITH_TEXT_RE.search(code)):
            out.append(shot)
            continue
        # 已经显式写了 `caption` 的，说明模型自己决定谁出字幕：`caption={false}` 时
        # 分带不出字，那份 `<CaptionText/>` 就是**唯一**一条——这时候去删它会把字幕
        # 删没。宁可漏删不许误删，见到显式 `caption` 就整镜不动。
        if _CAPTION_FRAME_CAPTION_PROP_RE.search(code):
            out.append(shot)
            continue
        new_code, gone = _strip_zone_captions(code, zone)
        if gone:
            code = new_code
            note = "删掉了分带外那份 <CaptionText/>"
        else:
            m = _CAPTION_FRAME_OPEN_RE.search(code)
            if not m:
                out.append(shot)
                continue
            code = code[:m.end()] + " caption={false}" + code[m.end():]
            note = "分带外删不掉，已给 <CaptionFrame> 补 caption={false}"
        frame_fixed.append(f"{sid}（{note}）")
        out.append({**shot, "code": code})
    notes: list[str] = []
    if frame_fixed:
        notes.append(
            f"字幕：{'、'.join(frame_fixed)}——<CaptionFrame> 自己已经画了字幕，再摆一份就会叠成"
            "重影，系统只留了一条。要自定义位置就用 <CaptionText/> 叠在画面上，"
            "不要再用 <CaptionFrame> 分带")
    if template_fixed:
        comps = "、".join(
            f"`{str(s.get('component') or '').strip()}`"
            for s in slots
            if str(s.get("text_kind") or "").strip().lower() == "caption"
            and str(s.get("component") or "").strip())
        notes.append(
            f"字幕：{'、'.join(template_fixed)}——模板的主字幕（{comps or '文字组件'}）"
            "已经画了这一条，代码里又摆了一份 <CaptionText/>，同屏会叠成重影；"
            "系统删掉了后面那份。这一镜的字只写进模板组件的内容 prop")
    if layer_fixed:
        notes.append(
            f"字幕：{'、'.join(layer_fixed)}——模板的主字幕已经在画这一条，"
            "代码里又摆了一份 <CaptionLayer/>，同屏会叠成重影；系统删掉了后面那份")
    return {**spec, "shots": out}, notes


#: 整片层的判据（闸⑦ 每镜必画 / 闸⑲ 风格丰富度 / 闸⑫ 与 demo 骨架同构 /
#: 闸⑳ 原创配额 / 闸⑨ 跨镜同构 / 闸⑩ 文字对比度 / 闸⑮ 版式族配额 / 闸⑯ 主体具名 /
#: 闸㉜ 字幕归属）。
#: **三处调用点**（plan_motion 入库前、render 烧像素前、patch 落盘前）与**两处报账**
#: （计划卡、出片回执）都从这一个元组取，规则因此不可能只在某一路上生效；这也是
#: 「清单只有一个」那条纪律落在这一族判据上的样子（同一形状的清单漂移踩过三次，
#: 见 §9-Q90/Q91）。
#:
#: ⑦ 与 ⑲ 是**一对**，别把它们的职责搞混：⑦ 管**地板**（每一镜都得用代码画出东西来），
#: ⑲ 管**逼丰富**（色板 / 满幅 / 质感 / 底色这四条在此之前没有任何闸在判）。
#: ⑫ 与 ⑳ 同理：⑫ 判「声明派生就要真像」，⑳ 判「走原创的那几镜别超过 20%」。
#: **闸㉝（每镜至少用一件简笔画零件）2026-10-18 退出整片判据**。它当初要治的是
#: 「画出来认不出是什么」，但机器形态只剩「import 名出现了」——模型最省事的过法是
#: 在画面角落摆一支 `<Arrow/>`，主体照样是一团点阵。用户对这一条的判断是「以前设定
#: 有很多是错的，不要限制这么多、不要给一堆模板」：零件库降级为**可选参考件**，
#: 「像不像」交给静帧视觉评审（``design_review``）按画面判。函数与 ``drawn_parts``
#: 保留，回执可以继续报「这一镜用了哪几件」，但不再拦。
#: 只在用 demo 时才有意义的三道（⑫ 骨架相似 / ⑳ 原创配额 / ⑮ 版式族配额）：
#: `DEMOS_ENABLED` 关着时不进整片判据（见那个常量的注释）。
_DEMO_GATES = (check_derived_similarity, check_original_share, check_layout_family)

#: **2026-10-23：整片判据收成一道。** 用户判断「DS 10.17 的画图能力不需要这些约束，
#: 让模型按技能与提示词自由发挥」。撤掉的六道与理由：
#:
#: · ⑦ `check_draw_floor`「每镜必画」——判「画没画」，属于审美；
#: · ⑲ `check_style_variety`「风格配额」——逼丰富，恰好是限制；
#: · ⑨ `check_shot_structure`「跨镜同构」——判「像不像 PPT」，交给模型；
#: · ⑯ `check_subject_named`「主体具名」——判「画的是不是那个东西」；
#: · ⑩ `check_ink_contrast`「字色对比度」——读不清是审美，不是崩；
#: · ⑫⑳⑮（demo 三道）——`DEMOS_ENABLED` 本来就关着。
#:
#: **留下的这一道不是审美判据，是结构性 bug**：同屏两条字幕会叠成重影，
#: 那是代码里摆了两份字幕元件，不是「好不好看」。
#:
#: 六个函数**不删**（回执、`style_facts` 与测试仍在读它们的读数），只是不再进
#: `validate_code_spec` 的打回清单——「不拦 ≠ 不明说」。
#:
#: **⑯ 同日加回（2026-10-23 晚）。** 与上面撤掉的五道，区别在**判的是什么**：
#: ⑦⑨⑩⑲⑫⑳ 判的都是「画得好不好」——那是审美，交给 `design_review` 按画面判更准；
#: ⑯ 判的是「**说了有没有做到**」——申报 `subject_channel=three` 而代码里一个 three
#: 都没有，是账实不符，不是品味问题，没有「人眼替机器判」的余地。
#:
#: 允许它不判的实测后果（《一秒》20:36 那轮）：12 镜里 8 镜申报了
#: three/katex/rough/canvas，而代码里这四个词出现 **0 次**，全片 `style_channels`
#: 只剩 `["vector"]`；视觉评审把 s07 的心脏写成「扁平卡通剪影，只有一圈暗描边，
#: 没有厚度、粗糙度、接触阴影」——它申报的是 three。**申报与实现之间没有任何机制。**
#:
#: 配套：`shot_writer._channel_samples_block()` 同日给了 three/katex/rough/canvas 的
#: 可抄骨架。**只拦不给范本会让那几镜反复退到超时**，两处必须一起上。
#: 注意 ⑯ 里 `subject` 那一半（空着 / 抽象词 / 长度越界）**仍是提醒**，不进打回清单。
#:
#: ⑯ 排在 ㉜ **之前**：`test_caption_layer` 立了一条「新闸接在尾上、不插队」的约定，
#: 而 ㉜ 必须留在最后一位。⑯ 是「加回来的旧闸」，接在头部不违反那条约定。
WHOLE_FILM_GATES = (check_subject_named, check_caption_ownership)

#: 「建议」档：这几道的打回项降为提醒（加 `（建议）` 前缀），与"打回档"同在一条回执里，
#: 读的人（与模型）分得出轻重。
#:
#: **档是判据自己的属性，不再是质检档的函数**（2026-10-16 并档）。并档前这里是三份
#: ——`SOFT_GATES`（两档都软）、`PREVIEW_SOFT_GATES`（只在预览档软）、以及"两份都不在"
#: 的硬闸——由 `qc` 挑一份用。现在整片闸**一律只报不拦**（拦的位置在三个调用点，
#: 见 `whole_film_gate_results`），于是"哪一档该软"这个问题本身没有意义了：一份就够。
#:
#: ⑦（每镜必画）与 ⑩（字色对比度）**不放进这一份，是有意的**：它们说的不是品味，
#: 是「这一镜画面上根本没东西」和「字压字看不清」——没有"人眼替机器判"的余地。
#: 留着"打回档"这个轻重，模型读 `check_film` 时一眼能分出哪几条必须改、哪几条可商量。
#: **不拦 ≠ 不明说**：判据照旧说全，拦不拦由调用方定，这正是「闸变工具」那条线。
#:
#: **⑯（主体具名）2026-10-23 从这一份里移出，改回硬闸。** 依据是当天《一秒》那条片的
#: 实跑账：12 镜里 8 镜申报了 `subject_channel=three/katex/rough/canvas`，而代码里
#: `three`/`roughjs`/`katex`/`canvas` 一次都没出现——**全片 `style_channels` 只剩
#: `["vector"]`**。⑯ 明明判出了这 8 条（`:2741` 那处是真 `_err`），却因为被列在这里，
#: 降成「（建议）」一路放行，于是「申报高级通道、手上画 SVG」成了零成本的事。
#: 用户看到的后果是 s07 的心脏被评审写成「扁平卡通剪影，没有厚度、粗糙度、接触阴影」。
#:
#: 这一条与 ⑦⑩ 同类：**「自报的通道必须有证据」不是品味，是账实相符**。允许它软，
#: 等于允许模型用一句话换掉一条画法通道。`subject` 那一半（空着/抽象词/长度）仍是
#: 提醒，不进这里——那才是可以商量的。
#:
#: 配套：`shot_writer._channel_samples_block()` 同时给了 three/katex/rough/canvas 的
#: 可抄骨架。**只硬拦不给范本会让那几镜反复退到超时**，两处必须一起上。
SOFT_GATES = frozenset({check_style_variety, check_layout_family, check_original_share,
                        check_shot_structure, check_derived_similarity})
#: 旧名（并档前"只在预览档软"的那一份）。并档后与 `SOFT_GATES` 是**同一个集合**，
#: 留着只为不打断既有引用（`test_caption_layer.py` 按名字查过它）。
#: **别只往其中一个加东西**——加进任一个就等于加进另一个，那正是并档要消掉的东西。
PREVIEW_SOFT_GATES = SOFT_GATES
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


def run_film_gate(gate: Any, shots: list[dict[str, Any]]) -> tuple:
    """跑一道整片闸；建议档那几道的打回项降为提醒（加「（建议）」前缀），其余原样返回。

    **没有 `strict` 参数了**（2026-10-16 并档）：这一层只决定**轻重**，不决定拦不拦。
    拦的位置在三个调用点，而从并档那天起它们一个都不拦——所以"哪一档"这个自变量
    在这条路上已经不存在了，留着它就只是让人以为换个档会换个结果。
    """
    got = gate(shots)
    if gate not in SOFT_GATES:
        return got
    return ([], [f"{SOFT_PREFIX}{e}" for e in got[0]] + list(got[1]), *got[2:])


def whole_film_gate_results(shots: list[dict[str, Any]]
                            ) -> tuple[list[str], list[str], list[str]]:
    """整片判据**唯一的出口**：十道闸 + 动效锚定㉑ 合成一份 ``(打回, 提醒, 未判)``。

    **三个出口是这一条的全部意思**（T28）：一道闸说不清的时候，它的答案既不是
    「过」也不是「不过」，而是「没判」——三者必须落在三个不同的格子里，因为回执、
    计划卡与模型读的都是格子名。判据自己带第三个出口时就用它；还没带第三态的那些
    闸按两态处理（``len(got) > 2`` 那一下），不逼它们假装有未判档。

    **为什么整片判据全在这里、而不在 `validate_code_spec` 里**（2026-10-16 改）。
    并档前它只被"计划卡要看账"的那几处调用，真正拦人靠的是 `validate_code_spec`
    里那个循环——于是判据只有一个**拦人**的出路，模型看不见它，只能撞上去；一次
    打回的代价是整轮重来。现在分工反过来：

    * `validate_code_spec` 只管**契约**（字段、容量、类型、归一）——「这份 spec
      能不能用」。契约不满足时照旧抛，因为那不是判据，是输入本身不成立。
    * 整片判据全部走这里。三个调用点（`plan_motion` 入库前 / `render_motion_code`
      烧像素前 / `patch_motion_code` 落盘前）把三个出口**原样附进各自的正常返回**，
      一个都不抛。模型因此看得到自己撞上的是哪几条、轻重如何，用户也在计划卡上
      看得到同一份账。

    ㉑ 的**闸前**那一半并在这里（R8-T33）。它原来跟闸并排摆在 `validate_code_spec`
    里，是唯一绕过本函数、连轻重档都没有的一道：动效锚是纯契约，判它一次 ffmpeg
    都不用开。「事件触发帧在不在窗口里」那一半要像素，仍在 `code_render` 逐镜那一段。
    """
    errors: list[str] = []
    warns: list[str] = []
    undetermined: list[str] = []
    for gate in WHOLE_FILM_GATES:
        got = run_film_gate(gate, shots)
        errors.extend(got[0])
        warns.extend(got[1])
        if len(got) > 2:
            undetermined.extend(got[2])
    errors.extend(check_motion_anchors(shots))
    # 两道**刻意不进元组**的闸，但它们的非打回出口仍要进这三个格子（2026-10-24 修）。
    #
    # ⑦（`check_draw_floor`）与 ⑩（`check_ink_contrast`）的打回项不进这里是有意的：
    # 回执上各自有一行报它们（`style_facts` 的 `text_only` / `contrast`），再进 errors
    # 就是同一件事两处各写一遍。但并档那天它们的**提醒**与**未判**跟着元组一起被裁掉了，
    # 而这两个出口没有第二条路：
    #   · `check_draw_floor` 的「文案具体项低于 40%」——回执的 `style_warnings` 因此恒为
    #     空数组，模型与用户都看不到那条线（`test_code_gates` 的 4 条断言钉着它）；
    #   · `check_ink_contrast` 的「认不出底色/字色」——`style_unjudged` 恒为空，
    #     「未判档（阻断）」那一行于是永远显示「六道闸每一档都真量过」，
    #     把「没人量过」写成了「量过且通过」，正是 T28 要消掉的那种混法。
    # 所以这里只接那两个出口，errors 照旧不走这条路。
    warns.extend(check_draw_floor(shots)[1])
    ink = check_ink_contrast(shots)
    warns.extend(ink[1])
    if len(ink) > 2:
        undetermined.extend(ink[2])
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
#:
#: ``palette``（2026-10-17）与 ``texture`` 是同一族的第二条：**整片默认，个别镜可覆盖**。
#: 它不是自由值（八套色板 + `none`，见 PALETTE_CHOICES）——卡面的每一项都要能反查到
#: 枚举源，自由值进卡面会让整份计划被打回（`COLOR_KNOBS` 那条注释记着这笔账）。
#: 逐镜覆盖**不在卡上**：它就写在那一镜自己的代码里（`const palette = palettes["…"]`）
#: 或那一镜申报的 `bg` 里，所以卡面只问一次。
KNOB_FIELDS = ("aspect", "fps", "narration", "voice", "rate", "caption_style",
               "highlight_style", "palette",
               "texture", "transition", "transition_sec", "entrance", "exit")

#: 整片**布尔开关**——那几档「一开就按镜花钱」的能力：㉖ 回看环（``review``）、
#: 层 D2 静帧审美评审（``still_review``）、⑲-b 身份判据（``identity``）。
#:
#: 它们**不在 `KNOB_FIELDS` 里**（计划卡上没有位置）：卡面那排 radio 回答的是「这条片子
#: 长什么样」，而这三档回答的是「愿不愿意为每镜一次带图的多模态调用付钱」。把它们摊在
#: 卡上，等于让「想挑个竖屏」的人顺手替自己选一张最贵的账单。它们也不进 `FILM_KNOBS`
#: （不进缓存键）：它们不改像素，改的是「烧完像素之后再看一遍」这条腿。
#:
#: 那它们怎么活着走到渲染链上？——**靠这一栏**。`validate_code_spec` 返回的是一个**新**
#: 字典（逐栏抄已知字段），不在这里抄一遍的栏位就是被静默删掉：调用方写了
#: ``spec["identity"] = True``、回执上却写着「这趟没开」——「开关写了不生效」是最难查的
#: 一类形状，和自由色 `highlight_color`（见 :data:`COLOR_KNOBS`）当初掉进去的是同一个坑。
#: 打开它们的写法也与自由色同一条路：写进 ``args["spec"]``（`motion_prefs` 那句
#: 「值写进 spec 而不是顶层」是同一个理由）。它们**不进** `code_spec_schema`：那份 schema
#: 是给模型看的，而这三档花的是用户的钱——摆在那儿等于邀请模型替用户按下付款键。
#:
#: **只抄真开的那几个**：缺省不留这一栏，于是 `spec.get(...)` 得到 `None`，与「没写」
#: 逐字同义——回执里「这趟没开」那句措辞因此仍然成立。
SWITCH_KNOBS: tuple[str, ...] = ("review", "still_review", "identity")

#: 整片风格那一句（`spec["style"]`）的长度上限（字符）。一句话说清就够；再长就成了
#: 一段技能正文，而它要上计划卡给人看。
STYLE_MAX_CHARS = 160
#: 整片风格的可执行拆解（美术导演自动补）：把 `style` 那句落成网格/色数/边缘/光/材质
#: 这几条具体做法。它不进计划卡旋钮，只进逐镜写码的提示词；超长截断，不报错。
STYLE_GRAMMAR_MAX_CHARS = 220
#: 整片贯穿装置（`spec["recurring_prop"]`）的长度上限（字符）。只写**一件**在画面世界
#: 里反复出现的道具——参考片里那枚「本能读数」仪表就是它。自由文本，不进计划卡旋钮
#: （与 `style` 同规：写在这里是为了不给卡面加一道没法反查枚举的栏）。
RECURRING_PROP_MAX_CHARS = 60

#: 按需加载的技能块名（2026-10-23）。**唯一一份清单**：schema 的 enum、校验时的过滤、
#: 与 `shot_writer._extra_sections()` 都从这里取。写手那一侧的两张表
#: （`_RULES_OPTIONAL` / `_SKILL_OPTIONAL`）按名字挂上去，键必须与这里一致——
#: `tests/test_shot_writer.py` 有一条断言钉住这件事。
#:
#: 为什么要做成枚举而不是自由字符串：块名是**服务端与技能之间的约定**，
#: 模型在 schema 与白名单里都查不到它；给自由字符串，拼错一个字母就是静默不加载。
SKILL_SECTION_NAMES: tuple[str, ...] = (
    "chart", "three", "transition", "caption-matrix", "caption-hard",
)
#: 风格包（`spec["skill_contract"]`，见 `plan_motion._attach_style_pack`）的容量上限。
#: 它由系统从 PG 注入，不是模型写的，也不是卡面旋钮：名字/显示名只用来在回执与提示词里
#: 指认；正文是给写手/导演看的说明书，写长了截断——风格包啰嗦不该让分镜交不上。
STYLE_PACK_NAME_MAX_CHARS = 64
STYLE_PACK_DISPLAY_MAX_CHARS = 80
STYLE_PACK_BODY_MAX_CHARS = 12000
#: 字幕字号系数（manifest 的 `caption.size`，乘在母版算出的字号上）的放行区间。
#: 与 `FilmLayout.tsx` 的 `resolveFilmStyle` 同一条边界：两端都是**兜底**，不是设计建议。
#: 低于 0.2 在 1080p 上落到读不出的十几像素，高于 1.5 会顶穿母版那条 0.036H 的硬上限。
CAPTION_SIZE_MIN = 0.2
CAPTION_SIZE_MAX = 1.5
#: 中英两行的次序（manifest 的 `caption.order`）。缺省 `en-zh`＝英文在上。
CAPTION_ORDERS = ("en-zh", "zh-en")
#: 满幅字幕**字块底边到画面底**的距离（manifest 的 `caption.bottom`，占画面高的比例）。
#: 与 `FilmLayout.tsx` 的 `resolveFilmStyle` 同一条边界。缺省见 `FILM_GEOMETRY` 的
#: `bleedCaptionBottom`（横屏 48px ≈ 一行字）；写 0.045 就是「离底一行字」。
CAPTION_BOTTOM_MIN = 0.005
CAPTION_BOTTOM_MAX = 0.3
#: 字幕字号硬上限（manifest 的 `caption.cap`，占画面高的比例）。母版缺省 0.036
#: （中英两行时）与 0.04（只有中文）；这是「字是画面的一部分、但不吃掉画面」那条线。
CAPTION_CAP_MIN = 0.02
CAPTION_CAP_MAX = 0.08
#: 模板模块说明符：内置模板走 `@dg/lib/style/<name>`，用户沉淀的走
#: `@dg/lib/style/generated/<name>`。**只放行这两条前缀**——写手会把它原样写进
#: `import`，一个自由值就是一条打包期才炸的路径。
STYLE_TEMPLATE_MODULE_RE = re.compile(
    r"^@dg/lib/style/(?:generated/)?[a-z][a-z0-9-]{1,40}$")
STYLE_TEMPLATE_MODULE_MAX_CHARS = 80
#: 组件 props 签名（从模板源码抽的）。它是**提示词**不是代码：写手照它 import 与传参；
#: 超长截断，不让一份特别啰嗦的模板把每镜的提示词撑爆。
STYLE_TEMPLATE_USAGE_MAX_CHARS = 1800
#: 槽位清单（每个导出组件一个槽位）与替换件登记。也是系统注入：写手据此知道
#: 「哪个组件能换成另一份实现」，缓存键据此区分「同模板不同零件」。
STYLE_SLOTS_MAX = 24
STYLE_SLOT_CONTRACT_MAX = 32
STYLE_SLOT_VARIANTS_MAX = 12
STYLE_OVERRIDES_MAX = 16

#: 片壳（`spec["shell"]`）：整片不变的那一层外框，**由 Remotion 镜像在 entry 层套住
#: 每一镜**，镜头代码只往画面带里填主体。纸底、顶部色条、左上圆钟、期号小字、底部字幕
#: 全归它——于是「五十镜各画五十种外框」在结构上不可能发生，也就不需要出图后再评审。
#:
#: 名单与 `remotion/src/lib/shells.ts` 的 `SHELLS` 逐字相同（两处一起改；那边的启动一致性
#: 检查与这里对不上时，选了壳的片子会拿不到壳——这正是 `tests` 要盯的那条）。
#: 它**不进 FILM_KNOBS**：只在真选了壳时才进缓存键（见 `shot_cache_key`），否则所有老片
#: 子的键会漂移一次、整片白烧。
SHELLS: tuple[str, ...] = ("paper-flat",)
#: 片壳上那行整片不变的小字（如「第 001 期 · 主题」）。自由文本，超长截断不报错。
SHELL_LABEL_MAX_CHARS = 48
#: 每镜画面描述（`shots[].brief`，交给逐镜写代码的那次调用）的长度上限（字符）。
SHOT_BRIEF_MAX_CHARS = 1000


def _switch_on(value: Any) -> bool:
    """一个整片开关是不是真的被打开了。

    只认真开的那几种写法（``True`` / ``"true"`` / ``"1"`` / ``"yes"`` / ``"on"``）。
    其余一律当**没开**——尤其是 ``"false"``：它是个非空字符串，真值判断会把它读成「开」，
    而「用户关掉了回看，回执却说回看跑过」与 `narration` 那条注释记的是同一个坑
    （``bool("false") is True``）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False

#: 「文字库」面板上那几维里**自由取色**的：值不是「档」而是一条 ``#rrggbb``
#: （空串 = 没钉，跟色板走）。
#:
#: 它**不在 `KNOB_FIELDS` 里**，也不在 `knob_props()` 里——这一条是试出来的，不是设计
#: 偏好：卡面的每一项都要能反查到枚举源（`plan/vocab.py::enum_source`：枚举 / 开关 /
#: 带界数值 / 曲库标签组），自由色四样都不是，于是模型只要照 schema 写了这一栏，
#: **整份计划会被打回**（「参数「highlight_color」没有可反查的枚举源」）。
#: 所以它的路与上面那排开关不同：用户在面板上钉的值由主服务**写进 `spec`**
#: （`motion_prefs.SPEC_LEVEL_KEYS`），再由 `validate_code_spec` 归一、`shot_input_props`
#: 注入。进 `spec` 就够了——渲染链读的是那一份，而计划卡上不需要有它的位置。
#:
#: 单独一张表而不是在面板那侧按名字判：前端要「渲取色控件还是一排卡」这件事有个契约里的
#: 判据（`library_props()` 把它翻成 dims 的 ``kind``），与栏名同一条纪律——
#: 面板上那个控件长什么样，不该由前端自己按键名猜。
COLOR_KNOBS: tuple[str, ...] = ("highlight_color",)

_VOICE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,})?-[A-Za-z]+Neural$")
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")

#: 标签/人话 → 机器名。顺序有意义：先具体后笼统（「男声·少年音」里既有男也有少年）。
#:
#: 为什么需要这一层（2026-10-23 真机）：分镜卡上音色显示的是「沉稳男声」这种人话，
#: 模型看到什么就传什么，于是把标签当机器名传进来。旧行为是**硬报错**——一次分镜
#: 因此整份作废、重来一遍（326 秒）。音色是一个纯展示维，认错了顶多声音不对，
#: 不该有权否掉整次分镜。
_VOICE_HINTS: tuple[tuple[str, str], ...] = (
    ("播报", "zh-CN-YunyangNeural"),
    ("新闻", "zh-CN-YunyangNeural"),
    ("少年", "zh-CN-YunxiaNeural"),
    ("童", "zh-CN-YunxiaNeural"),
    ("年轻", "zh-CN-YunxiNeural"),
    ("女", "zh-CN-XiaoxiaoNeural"),
    ("男", "zh-CN-YunjianNeural"),
)


def normalize_voice(value: Any) -> str:
    """把一个「可能是人话」的音色名收敛到机器名；认不出回空串（由调用方报错）。

    三层，从严到松：① 已经是名单里的机器名 → 原样；② 是面板上的标签（逐字相等，
    走 `KNOB_VALUE_LABELS` 反查）→ 换成机器名；③ 只含关键词（「沉稳男声」）→ 按
    `_VOICE_HINTS` 猜。**猜的结果只用于「不报错」，不回写用户的选择**：计划卡上
    显示的还是模型传进来的原话，人一看就知道被归到了哪一档。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if s in VOICES:
        return s
    labels = KNOB_VALUE_LABELS.get("voice") or {}
    for machine, label in labels.items():
        if label and label == s and machine in VOICES:
            return machine
    for hint, machine in _VOICE_HINTS:
        if hint in s:
            return machine
    return ""

#: 语速档位（前端下拉直接给这几个，比让用户填百分号友好）。
#:
#: 2026-10-20 用户明确要求「人声朗读正常语速，不要降速」：负档位从可选清单里拿掉，
#: 正常语速（+0%）就是下限。要更慢的听感用更长的文案或 `min_duration_sec` 留白，
#: 不靠 TTS 降速——降速在真机 run 里被模型用来凑目标时长（实测出现过 -8%）。
RATE_CHOICES = ("+0%", "+10%", "+20%", "+30%")

#: 语速的**人话写法 → 档位**。2026-10-23 真机：模型把 `1.0` 当语速传进来，
#: `_RATE_RE` 认不出 → 整次分镜作废重来（那一次烧了 407 秒）。
#: 一个纯展示字段不该有这种权力：能算出来就算，认不出才报错。
_RATE_RATIO_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*[xX倍]?$")


def normalize_rate(value: Any) -> str:
    """把「可能是人话」的语速收敛到 ``+N%``；认不出回空串（由调用方报错）。"""
    s = str(value or "").strip()
    if not s:
        return "+0%"
    if _RATE_RE.match(s):
        return s
    # `1.0` / `1` / `1.05x` / `0.9倍` —— 当成倍率，换成百分比。
    m = _RATE_RATIO_RE.match(s)
    if m:
        try:
            pct = round((float(m.group(1)) - 1.0) * 100)
        except ValueError:
            return ""
        if not -50 <= pct <= 50:
            return ""
        # 与下面那条同规：正常语速是下限，负的一律归零。
        return f"+{max(0, pct)}%"
    return ""


#: 高亮样式的**人话写法 → 机器名**。同样是 2026-10-23 真机：模型传了「关键词换色」，
#: 那是卡面上的中文标签，不是枚举值——直接报错让整次分镜作废，代价与收益完全不成比例。
_HIGHLIGHT_ALIASES: tuple[tuple[str, str], ...] = (
    ("关键词换色", "color"), ("换色", "color"), ("关键词", "color"), ("高亮", "color"),
    ("卡拉", "karaoke"), ("逐字", "karaoke"),
    ("色块", "block"), ("块底", "block"), ("底块", "block"),
    ("墨", "ink"),
    ("不加", "plain"), ("普通", "plain"), ("无", "plain"),
)


def normalize_highlight(value: Any) -> str:
    """把「可能是人话」的高亮样式收敛到枚举值；认不出回空串。"""
    s = str(value or "").strip()
    low = s.lower()
    if low in HIGHLIGHT_STYLES:
        return low
    for hint, machine in _HIGHLIGHT_ALIASES:
        if hint in s:
            return machine
    return ""

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
        # 以下八档**不再出现在可选清单里**（见 ``VOICES``），标签留着只为回读：
        # 老片子里挑了方言音色的那一镜，确认卡上仍要写「女声·粤语」而不是
        # ``zh-HK-HiuGaaiNeural``。删掉它们才是真把老片子的回执弄难看。
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
    # 字幕两维（2026-10-16）：字长什么样 × 被点名的词怎么被点出来。旧的三旋钮
    # （subtitle_mode / caption_position / caption_anim）不在这一族里了。
    # 标签说清「观众看到的是什么」：写字纸/描边/位置/入场都归前者，换色/点亮/色块归后者。
    "caption_style": {"by_shot": "交给镜头自己设计", "on_picture": "贴画面·无底条",
                      "outlined": "白字黑边",
                      "paper": "纸条底·画面下沿", "film_line": "整句电影感·下沿淡浮",
                      "big_center": "大字压屏·画面正中", "top_banner": "顶部横幅",
                      "none": "不上字幕"},
    "highlight_style": {"color": "关键词换色", "karaoke": "逐词点亮",
                        "block": "底色块", "ink": "撕纸高亮",
                        "plain": "不强化·整句一个样"},
    # 质感每一镜都能带一档（顶层是整片默认），标签要说清「加的是哪种观感」，不然选的人只能猜
    "texture": {"none": "不加（原片直出）", "film": "老胶片·颗粒与暗角",
                "tv": "老电视·扫描线与色偏", "glow": "柔光·高光处泛开",
                "bleach": "漂白·硬对比低饱和"},
    # 整片色板（2026-10-17）。八套色板的名字**不在这里重列**——它们是 PALETTE_LABELS
    # 那一份（与 palettes.json 逐项对齐，由 tests/test_code_spec.py 断言），这里只把
    # 「不指定」那一档摆进去。`none` 的措辞就是 PALETTE_DEFAULT_LABEL，与文字库面板
    # 同源：同一件事在两个面板上不许显示成两副面孔。
    "palette": {PALETTE_NONE: PALETTE_DEFAULT_LABEL, **PALETTE_LABELS},
    # R7-T22：转场与进出场。措辞要说清「观众看到的是什么」，而不是抄 ffmpeg 的档名
    # （wipeleft 这三个字母对人没有意义）。四条都在确认卡与计划卡两张卡上出现。
    "transition": {"none": "硬切（镜镜直接接）", "fade": "淡叠·前一镜淡出后一镜淡入",
                   "fadeblack": "黑场·先暗到黑再从黑里亮起来",
                   "dissolve": "溶解·两边互相化开",
                   "wipeleft": "左擦·新画面从右往左推过来",
                   "wiperight": "右擦·新画面从左往右推过去",
                   "slideup": "上滑·新画面自下而上顶入",
                   "circleopen": "圆开·从中心扩开",
                   "circleclose": "圆收·旧画面向中心收拢",
                   "zoomin": "推进·画面放大着换过去",
                   "hblur": "横向拖影·画面被速度拉开",
                   "radial": "涟漪·圆形波纹从一点荡开",
                   "smoothleft": "柔推·缓动横移换过去"},
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

#: 本链字幕两维的单独视图（确认卡与母版参数都从这里取词）
CAPTION_STYLE_LABELS = KNOB_VALUE_LABELS["caption_style"]
HIGHLIGHT_STYLE_LABELS = KNOB_VALUE_LABELS["highlight_style"]

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
            "description": ("画幅比例，决定成片像素尺寸与字幕带容量。"
                            "取值就写这三个中的一个：landscape（横屏 1920×1080）、"
                            "portrait（竖屏 1080×1920）、square（方屏 1080×1080）。"
                            "服务端也认 16:9 / 9:16 / 1:1 / 1920x1080 / 横屏 / 竖屏 / 方屏，"
                            "但**首选填枚举值**"),
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
            "description": "语速档位。**缺省 +0% 就是正常朗读速度，也是下限**；"
                           "不支持放慢。时长不够先加文案、加画面驻留或加镜，"
                           "不要靠降速凑时长",
        },
        "caption_style": {
            "type": "string", "enum": list(CAPTION_STYLES), "default": "by_shot",
            "description": "字幕长什么样、站在哪（整片默认，每镜可覆盖）；"
                           "缺省 outlined＝白字黑边；none=不上字幕",
        },
        "highlight_style": {
            "type": "string", "enum": list(HIGHLIGHT_STYLES), "default": "color",
            "description": "被点名的词怎么被点出来（整片默认，每镜可覆盖）",
        },
        "texture": {
            "type": "string", "enum": list(TEXTURES), "default": "none",
            "description": "整片默认质感档；每一镜可用自己的 texture 覆盖它。换它不重烧镜头",
        },
        # 整片底色那一格（2026-10-17）。description 与 texture 那一条同形：说清「它是
        # 默认、谁能覆盖、换它的代价是什么」。**收在一行、60 字内**（`knob_facts` 的 note
        # 取前 60 字，超了会截在半句话上，而这里正好会截在 `palettes["…` 中间）。
        # 逐镜覆盖的写法归技能正文——模型看不到「色板写在这一镜代码里」这件事，
        # 而卡面只问一次。
        "palette": {
            "type": "string", "enum": list(PALETTE_CHOICES), "default": PALETTE_NONE,
            "description": "整片底色与字幕配色那一档；某一镜要另调就改那一镜的代码",
        },
        # R7-T22 的四条。description 收成一行、≤60 字（`knob_facts` 的 note 取前 60 字，
        # 超了会截在半句话上），「怎么选」那套教程在技能正文里。
        "transition": {
            "type": "string", "enum": list(TRANSITION_STYLES), "default": "none",
            "description": "镜与镜之间怎么接（拼接层做，不重烧任何一镜）",
        },
        "transition_sec": {
            # 两种类型都收（2026-10-19）：卡面与旧调用写字符串档位，模型按「秒」的
            # 直觉写 0.4 这种数字也是同一个决定——归一在 `validate_code_spec` 里把
            # 数字落成档位字符串。只收 string 时 pydantic 在工具入口就把 0.4 挡下了，
            # 一次本来能直接入库的分镜要重交一轮。
            "type": ["string", "number"], "enum": list(TRANSITION_SEC_CHOICES),
            "default": TRANSITION_SEC_DEFAULT,
            "description": "转场叠多久：0.2 / 0.4 / 0.6 / 0.9 秒四档"
                           "（写字符串或数字都行，数字落在两档之间会吸到最近一档）；"
                           "须短于相邻较短一镜的一半",
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


#: 「文字库」面板管得着的那几维。**与 `KNOB_FIELDS` 分开列**：那一个是整片开关的全集
#: （计划卡上每一项都能改），这一个只挑出与「声音和字」有关的几维——面板把 `voice`
#: 与 `rate` 摆在一起是给人「挑一个音色顺便调语速」，不是要把画幅帧率也塞进来。
#: 取值与标签**不在这里重列**，`library_props()` 从 `knob_props()` 现取。
#:
#: 2026-10-16 加 `highlight_color`（「自定义高亮词颜色」）：它与上面那几维的区别只在
#: 值的形状（自由色而非档位），在面板上仍是「用户挑、钉进这条对话」的同一件事，
#: 所以进这张表、由 dims 的 ``kind`` 分出控件形状。**它排在高亮样式后面**——
#: `library_props()` 按这张表的次序构造 dims，前端按 dims 的次序排，位置因此由这里定。
LIBRARY_DIMS: tuple[str, ...] = ("caption_style", "highlight_style", "highlight_color",
                                 "voice", "rate")

#: 上面那几维在面板上的**栏目名**。为什么这也归后端：与取值标签同一条纪律——
#: 面板上出现的每一个中文词都只有一处出处。前端自己写「高亮样式」，两边迟早对不上
#: 的是「这一栏管的是哪一维」这件事本身，而那种错不会红，只会让人改错东西。
#: 前两维**同时也是两排卡的组名**（见 :data:`CAPTION_PRESETS` 的 ``group``）：
#: 前端按 ``presets[].group`` 现取栏名，一个字符串都不自己拼。
LIBRARY_DIM_TITLES: dict[str, str] = {
    "caption_style": "字幕样式",
    "highlight_style": "高亮样式",
    # 这一维挨着「高亮样式」摆：它管的就是那几档（换色/底色块/撕纸）里被点亮的词
    # 用哪个颜色。空串＝跟随这一镜的色板。
    "highlight_color": "强调色",
    "voice": "音色",
    "rate": "语速",
}


def library_props() -> dict[str, Any]:
    """前端的「文字库」要的全部内容，一次打包。**前端零硬编码**。

    与计划卡那条纪律同源：卡面 radio 的文字来自契约的 ``value_labels``，这里也一样——
    取值清单与中文标签从 ``knob_props()``（它自己又取自 ``CAPTION_STYLES`` /
    ``HIGHLIGHT_STYLES`` / ``VOICES`` / ``KNOB_VALUE_LABELS``）现取，前端**一个取值、
    一个中文词都不许自己写**。前端自己列一遍的后果不是报错，是界面上写着「纸条底」
    而渲染认的是另一个字符串——那种不一致不会红，只会渲出来不一样。

    四块内容：

    * ``presets`` —— 面板上那两排卡，见 :data:`CAPTION_PRESETS` / :data:`HIGHLIGHT_PRESETS`
      （`key` 就是取值，`label` 在这里从 ``KNOB_VALUE_LABELS`` 现取，`group` 决定分排）；
    * ``dims`` —— 上面几维的 ``title`` / ``kind`` / ``enum`` / ``default`` /
      ``value_labels``（栏目名也在这儿，前端连「这一栏叫什么」都不自己写；
      ``kind`` 是**控件形状**——``enum`` 渲一排档、``color`` 渲一枚取色器，
      自由色那一维的 ``enum`` 因此是空表）；
    * ``voice_groups`` —— 音色按 :data:`VOICE_GROUPS` 的前缀分组，每档带它自己的标签；
    * ``palettes`` —— 预览样张用的色板清单（第一项是「不指定」那一档），
      每项带 ``label`` 与三色 ``swatch``（色块本身就是那套色板的样子，见下）。

    由 ``tests/test_code_spec.py`` 的第 ⑨ 节守着与 ``knob_props()`` 逐字一致。
    """
    knobs = knob_props()
    dims: dict[str, Any] = {}
    for key in LIBRARY_DIMS:
        spec = knobs.get(key) or {}
        free_color = key in COLOR_KNOBS
        dims[key] = {"title": LIBRARY_DIM_TITLES.get(key, key),
                     # 面板据此决定渲哪一种控件：`enum` = 一排档，`color` = 一枚取色器。
                     # 前端不按键名猜（猜错的表现是「点开一个空的档位列表」，而不是报错）。
                     "kind": "color" if free_color else "enum",
                     # 自由色那一维**没有 `knob_props()` 那一份 schema 可读**（它进不了
                     # 卡面，见 `COLOR_KNOBS`）：取值清单空、缺省是空串（＝没钉）。
                     # 这三栏因此在这里直接给，而不是从 `knobs` 取。
                     "enum": [] if free_color else list(spec.get("enum") or ()),
                     "default": "" if free_color else spec.get("default"),
                     "value_labels": {} if free_color
                     else dict(spec.get("value_labels") or {})}

    labels = dims["voice"]["value_labels"]
    grouped: list[dict[str, Any]] = []
    placed: set[str] = set()
    for prefix, group_label in VOICE_GROUPS:
        items = [{"value": v, "label": labels.get(v) or v}
                 for v in VOICES if v.startswith(prefix + "-")]
        placed.update(i["value"] for i in items)
        grouped.append({"key": prefix, "label": group_label, "items": items})
    # 不在任何前缀下的音色不静默丢掉：收进一个自成一组的「其他」，并在
    # `tests/test_code_spec.py` 第 ⑨ 节断言这一组为空——服务商加了新地区，
    # 界面该看得见它，而不是少一档而没人知道。
    rest = [{"value": v, "label": labels.get(v) or v}
            for v in VOICES if v not in placed]
    if rest:
        grouped.append({"key": "other", "label": "其他", "items": rest})

    # 样张色板。挑哪一套**只影响那张预览图**，不影响成片——成片里每一镜跟着它
    # 自己的色板走（`film_style_of`）。摆出来是因为「字幕条到底跟不跟色板走」这件事
    # 光看档名看不出来：同一档字幕，母版底色下是白底黑字，深海蓝下是深底浅字。
    # 第一项是空串（不指定），它那一组色块取自母版常量（`tokens.ts`）。
    token = token_colors()
    palette_options: list[dict[str, Any]] = [{
        "key": "", "label": PALETTE_DEFAULT_LABEL,
        "swatch": {"page": token.get("bg") or "", "text": token.get("ink") or "",
                   "accent": token.get("accent") or ""}}]
    for name, pal in palettes().items():
        palette_options.append({
            "key": name, "label": PALETTE_LABELS.get(name, name),
            "swatch": {slot: str(pal.get(slot) or "") for slot in ("page", "text", "accent")}})

    return {
        # 卡面文字从取值标签现取：档名就是取值，中文标签只有 `KNOB_VALUE_LABELS` 一处。
        "presets": [{"key": p["key"], "group": p["group"],
                     "label": (dims.get(p["group"], {}).get("value_labels") or {}).get(p["key"])
                              or p["key"],
                     "note": p["note"],
                     "knobs": {p["group"]: p["key"]}} for p in LIBRARY_PRESETS],
        "dims": dims,
        "voice_groups": grouped,
        "palettes": palette_options,
    }


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
    # 第二张表（中性色效果）：源图三块中性色，判据是「没崩 / 色度不跑 / 与不挂效果
    # 有差」——见 `remotion/tests/effects-probe.mjs` 的 `EFFECT_TABLE`。按设计改色相
    # 的四件（duotone / lut / light-leak / chromatic-aberration）不在其中。
    "@remotion/effects/glow", "@remotion/effects/vignette",
    "@remotion/effects/noise", "@remotion/effects/speckle",
    "@remotion/effects/roughen-edges", "@remotion/effects/shadows-highlights",
    "@remotion/effects/blur",
    # 画法通道：手绘斜纹与真三维。这两族在 package.json + lockfile 里躺着，
    # 2026-10-11 那批六支真渲夹具（roughjs / three / KaTeX × 两画幅）就是它们渲的。
    # 真三维场景里的后处理（2026-10-24 真渲验过）。`@remotion/effects/glow` 只能挂
    # `<Img>/<Solid>/<HtmlInCanvas>`，**管不到 `<ThreeCanvas>` 里的东西**——three 场景
    # 要 bloom 只能走 `EffectComposer`。实测每帧多 0.19 秒（比 HtmlInCanvas 的 0.64 便宜）。
    "@react-three/postprocessing",
    "three", "@react-three/fiber", "@remotion/three", "roughjs",
    "katex", "katex/dist/katex.min.css",
    # 2026-10-24：补齐与闸① 的差集。这 12 项**镜像里早装好了、闸① 也早放行了**，
    # 只有这份「模型看得见」的名单漏着——后果不是报错，是模型根本不知道能用：
    # 转场只能手搓、`d3-scale` 的比例尺只能自己算、`chroma-js` 的调色只能手写插值、
    # `flubber` 的形状补间用不上、`lucide-react` 的图标也只能自己画。
    # 判据是 `test_code_spec.py` 第 ⑤ 节「两侧逐项相等」，它一直在红（差集 12 项）。
    #
    # 刻意**不含** `@remotion/effects` 桶名（§9-Q89 那条仍然成立：桶里没有
    # paper/halftone，写包名打包期不报错、渲染期才炸）——闸① 那边放行了它是那边的漏洞，
    # 不该拿它当对齐目标。
    "@remotion/transitions", "@remotion/transitions/fade",
    "@remotion/transitions/slide", "@remotion/transitions/wipe",
    "@remotion/transitions/flip", "@remotion/transitions/clock-wipe",
    "chroma-js", "d3-scale", "d3-shape", "d3-interpolate-path",
    "flubber", "lucide-react",
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
    # 简笔画零件库（2026-10-17，清单见 `_DRAW_PARTS`）。**逐件列，与上面那几件同一口径**
    # ——每件是一个文件级入口，目录名（`@dg/lib/draw`）没有 index 文件，列了它闸① 放行、
    # 打包期才炸。
    *_DRAW_PARTS,
    "@dg/lib/helpers/motion", "@dg/lib/helpers/rand", "@dg/lib/helpers/shake",
    "@dg/lib/helpers/camera",
    # 手绘笔刷与场景底板（2026-10-18）。**目录名可列**：这两个目录各有 index 文件，
    # 打包器按 mainFiles+extensions 能解析到；`@dg/lib/draw` 那批没有 index，只能逐件列。
    # 它们是**可选参考件**，不是闸⑦ 的入场券——裸写 SVG/Canvas 同样算画。
    "@dg/lib/brush", "@dg/lib/scene",
    # 同一个主角的走路件（2026-10-19）。文件级入口（`src/lib/walk.tsx` 默认导出
    # `WalkingFigure`）；它输出矢量剪影，与上面两件同记在 `vector` 通道。
    "@dg/lib/walk",
    # 风格模板（2026-10-19）：系统注册的 `silhouette-history`。用户不能上传 TSX，
    # 所以这笔名单就是全部模板；新增模板时这里与 `remotion/lint-rules.mjs` 同步。
    "@dg/lib/style/silhouette-history",
    # 三维写实模板（2026-10-21）「暗房微距」。舞台/材质/灯光/相机/调色在模板里，
    # 镜头代码只摆这一镜的实物；名单与 `remotion/lint-rules.mjs` 同步。
    "@dg/lib/style/cinema-studio",
    # 超现实写实的能力库（2026-10-22）：光/烟/粒子/水面/辉光/三维世界。逐件导出，
    # 镜头代码按这一镜的 brief 自己挑零件——它不是模板，不规定构图。
    "@dg/lib/style/surreal",
    "@dg/themes/palettes.json",
)

#: ``code`` 这一栏的 schema 说明。**这里只留「机器会判什么」**，写法教程整段在技能正文
#: （``motion_explainer`` 的「镜头代码合同」「把…开成旋钮」「闸④ 判空壳」三节）。
#: 分层的依据是实测而不是偏好：工具 schema 的散文**送不到模型那里**——节点工具的
#: inputSchema 由 ``server.py::_signature_for`` 从 Python 签名生成，只取 ``type``，
#: ``spec`` 到那侧只剩 ``additionalProperties: true``。所以这一段过去那 2 KB 教程
#: 谁也没教过，而技能正文逐条写过同一套规则（从前还多教过 ``useBody()`` 那条母版几何，
#: 母版下线那一轮撤了）：两处各写一遍必然漂，漂的那一侧又没人会去读，于是成了假文档。
#: 留下的是**判据**——白名单、自包含构图、字段对账三条、体积与判空，它们与本模块的常量
#: 和容器里的 lint 一一对应；``tests/test_code_spec.py`` 同时核对这份文本与技能正文，
#: 谁单独改了哪一条都会红。
_CODE_DOC = (
    "这一镜的**完整 .tsx**（一个文件，顶层一个默认导出的镜头组件），由 creation-remotion "
    "逐帧渲染。闸①（容器里的 ESLint）在渲染前判，违规带镜号与行号退回，不烧像素。"
    "**写法纪律（自包含构图、母题铺满画面、画法通道与 seed、"
    "按墙钟随机的禁令、字体只用 Noto）见技能正文「镜头代码合同」一节**，这里只列会判红的：\n"
    "· **这一镜自包含**：顶层 `<AbsoluteFill>` 自己构图（底色、边距、字幕位都归你），"
    "画幅从 `useVideoConfig()` 拿。母版 `<FilmLayout>` 已下线，不要 import 它；"
    "`@dg/film` 现在是**零件包**（字幕三件 `CaptionLayer`/`CaptionText`/`useCaptionTiming`、"
    "字段标注 `F`）。全片一致靠 `@dg/tokens` 的 `TOKENS`（底色/墨色/强调色）与卡库配方，"
    "不靠一个壳替你排\n"
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
    "· **字幕由你排**：`text`（这一镜整句）、`highlight`（要强调的词）、`words`"
    "（TTS 逐词帧）由闸② 注入，你用 `@dg/film` 的字幕件把它们摆成画面里的字"
    "（推荐「关键词换色」：字压在画面上、高亮词换这一镜的强调色——见技能正文「字自己排」"
    "一节）。别自己再实现一遍片段切分与求字号（那是第二份实现，两处断行迟早不一样），"
    "也别把 `text` 当画面文案另画一遍（同一句话两处画＝重影）\n"
    "· 可改的值开成 `fields` 旋钮：代码里读 `fields.<name>`，"
    "并在真的把它画上那个位置时套一层 "
    "`<F name=\"…\" value={fields.…}>`（`F` 由 `@dg/film` 导出），字段级命中表拿它定位。"
    "三条 lint 在烧像素之前判：声明了却没人读、读了却没声明、`<F>` 标的 value 不是"
    "这个字段自己的值\n"
    "· 画法按题材选，不是装饰。下面五条是**机器识别「你真的画了东西」的痕迹，不是"
    "画法菜单、更不是模板**：手绘斜纹 `roughjs`、真三维 `three` + `@react-three/fiber` "
    "+ `@remotion/three`、公式 `katex`（+ 它的样式表）、矢量绘制（裸 `<svg>`/`<path>`/"
    "渐变/滤镜，或 `@dg/lib/brush`、`@dg/lib/scene`、`@dg/lib/walk` 的笔刷、场景件与"
    "主角走路件）、`<canvas>` 手写 "
    "`getContext`。画什么、怎么画完全由你按这一镜的题材决定；闸⑦ 只**逐镜**确认这五条"
    "痕迹里至少出现一条——只有文字与色块的镜打回。"
    "**只 import 一行而代码体里没用那个名字**不算\n"
    "· 整片层还有两道也在烧像素之前判的闸：闸⑨ 不许三镜以上的代码骨架一模一样"
    "（剥掉文案字符串与颜色之后逐字相同＝只换了色相，那叫拼贴），闸⑩ 不许字色在"
    "这一镜写出的每一种底色上都低于 3:1（画面有字而观众读不出）\n"
    "· 画面里得有能认出来的东西：闸④ 按像素判空（自包含镜按整帧，套母版的镜按母版"
    "画面带；三帧非底色占比低于 2% 且内容没铺开就退回）")


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
            # 自由取色的那一维（见 :data:`COLOR_KNOBS`）**只在这里露面，不进
            # :func:`knob_props`**。它是整片开关不假，但不属于「该摆在计划卡上让人勾」
            # 的那一类：卡面每一项都要能反查到枚举源，自由色四样都不是，写进卡面参数
            # 的后果是**整份计划被打回**。摆在 spec 这一份 schema 里有两个用处：
            # 模型看得见这一栏存在（写 `palette.accent` 就够，见技能正文），以及启动期
            # 那条一致性检查认得出它——技能正文点到 `highlight_color` 时不该被报成造工具名。
            "highlight_color": {
                "type": "string", "pattern": "^#[0-9a-f]{6}$",
                "description": "高亮词那一块的色（#rrggbb）。留空＝这一镜跟自己那套色板的 "
                               "accent；用户在「文字库」钉过时一律以用户的为准"},
            # 整片美术方向（2026-10-18）。**只在这里露面，不进 `knob_props`**：
            # 它不是计划卡上能勾的一档，而是主对话从用户那句风格话里读出来、
            # 写一次给整片用的画法约定。写手把它逐字拼进每一镜的提示词；
            # 切片末段那趟统一调色也读它（见 `render.art_grade`）。
            "art": {
                "type": "object",
                "description": "整片怎么画（一处，管所有镜）。**用户给了参考片、参考图，"
                               "或明确说了媒介/笔触/光/纸感/剪影这类画风时必须写**——"
                               "不写的话每一镜的写手只能各自猜，最后画风不统一；"
                               "没提就按选题自己定。不确定的枚举字段可以不写，"
                               "别写不属于枚举的值；`look` 那句写你自己组合出来的画风，"
                               "不受枚举限制",
                "properties": {
                    "look": {"type": "string", "maxLength": ART_LOOK_MAX_CHARS,
                             "description": "整片画风一句话，**自己组合**出来的那套视觉语言："
                                            "媒介/笔触/纸与光/构图习惯/色彩性格。"
                                            "不是从枚举里挑一档，也不是照抄用户那句；"
                                            "用户给了方向就在上面加。≤160 字"},
                    "brush": {"type": "string", "enum": list(ART_BRUSHES),
                              "description": "笔触质感：graphite 石墨/铅笔，marker 马克，"
                                             "watercolor 水彩，ink 墨线"},
                    "light": {"type": "string", "enum": list(ART_LIGHTS),
                              "description": "主光从哪来（top-left / left / soft…）；"
                                             "整片同一方向，镜头里的亮面暗面与落地影都照它"},
                    "texture": {"type": "string", "enum": list(ART_TEXTURES),
                                "description": "底材：paper 纸纹，grain 颗粒，none；"
                                               "与顶层 texture（老胶片/老电视）不是一回事"},
                    "type": {"type": "string", "enum": list(ART_TYPES),
                             "description": "字体气质：serif-display 衬线大标题 / "
                                            "sans-editorial 无衬线编辑风…"},
                    "motion": {"type": "string", "enum": list(ART_MOTIONS),
                               "description": "动效质感：spring 有回弹的落笔，linear 匀速"},
                    "palette_note": {"type": "string",
                                     "maxLength": ART_NOTE_MAX_CHARS,
                                     "description": "一句话说色板性格（如「牛皮纸底、墨黑、"
                                                    "一点朱砂」）；≤120 字"},
                },
            },
            # 同一个主体穿行各场景/各时代（2026-10-19）。与 `art` 一样**只在这里露面，
            # 不进 `knob_props`**：它不是卡面能勾的一档，而是整片的演出约定。
            # 为什么必须是结构化的一栏：连续性靠同一组数（比例、基准线、朝向、节奏），
            # 靠「画大一点」这种散文守不住二十几镜。
            # **不按物种/物件分类**：人、猫、鸟、鱼、车、器物、光块都走同一套字段；
            # 写手照 brief 自己画，系统不维护「允许画哪些类别」的清单。
            "protagonist": {
                "type": "object",
                "description": "整片的**同一件贯穿主体**：人、猫、鸟、鱼、车、器物、一块"
                               "发光的矩形都行——它在不同场景/时代里反复出现，观众每次"
                               "都能认出是同一个东西（参考片那种「同一主体走过文明史」的"
                               "讲法）。**这一栏只记连续性，不记「允许画哪些类别」**。"
                               "**用户给了这类参考片，或选题本身就是一条贯穿线索时写；"
                               "没有贯穿主体就别写**——这一栏一写，每一镜的写手都会把它"
                               "放进去，包括文案没提到它的镜。"
                               "**画法**：怎么动、画成什么样都由写手按 brief 决定；"
                               "这一栏只给连续性——height/foot_y/facing/stride_sec。"
                               "要写实/有体积就选 `render: lit`，不要默认剪影。"
                               "数值越界会被夹回范围，不影响分镜成立",
                "properties": {
                    "look": {"type": "string", "maxLength": PROTAGONIST_LOOK_MAX_CHARS,
                             "description": "这个主体是什么样：轮廓/体态/标志物/材质，写**同一个**"
                                            "主体能跨镜认出来的那几样（如「瘦高、圆头、宽肩的"
                                            "剪影，手里那盏灯跟着时代换」或「一块竖立的发光矩形，"
                                            "边缘有一圈暖光」）。≤160 字。"
                                            "**必写**——没有它这一栏不成立"},
                    "kind": {"type": "string", "maxLength": PROTAGONIST_KIND_MAX_CHARS,
                             "description": "主体是什么（人 / 猫 / 鸟 / 鱼 / 机器人 / "
                                            "发光的矩形 / 一辆车…）。自由文本，可空；"
                                            "**不要默认填「人」**，也不要把它当成枚举。"
                                            "它只帮写手理解 look，不决定画法类别"},
                    "render": {"type": "string", "enum": list(PROTAGONIST_RENDERS),
                               "description": "画法方向：silhouette 剪影（缺省）/ flat 平涂上色 / "
                                              "lit 受光面与背光面两档（更接近「真实的样子」）/ "
                                              "line 线稿。镜头代码按这一档决定用不用组件、"
                                              "以及给主体几档明度"},
                    "wardrobe": {"type": "string",
                                 "maxLength": PROTAGONIST_WARDROBE_MAX_CHARS,
                                 "description": "外观/材质随时代怎么换，写一条演进线，自由文本"
                                                "（如「岩洞：兽皮斗篷 → 蒸汽：工装帽 → 现代："
                                                "连帽衫」或「幼猫的绒毛 → 老猫的疤 → 铜像」）。"
                                                "每一镜由写手按 brief 画；**不是从固定枚举里挑**。"
                                                "轮廓（身高/体态/朝向）不随外观变。≤160 字"},
                    "height": {"type": "number",
                               "description": "主体高（人物是身高，动物是体长/身高）占画面高的"
                                              "比例，0.25~0.75。参考片是 0.40~0.55：**主体要够大**。"
                                              "每一镜同一个值，切点才不会忽大忽小。缺省 0.46"},
                    "foot_y": {"type": "number",
                               "description": "主体与地面的接触线／活动基准线在画面高的哪一档，"
                                              "0.55~0.98。参考片约 0.86（脚在下方、字幕带之上）；"
                                              "飞鸟、游鱼也可以用它固定活动带。每一镜同一个值。"
                                              "缺省 0.88"},
                    "facing": {"type": "string", "enum": list(PROTAGONIST_FACINGS),
                               "description": "朝画面哪边（行进/朝向）：right（缺省）或 left。"
                                              "一条片子里别来回换——换方向得有一个叙事理由"},
                    "stride_sec": {"type": "number",
                                   "description": "一个完整运动周期几秒（走、游、飞、摆动都可用），"
                                                  "0.5~1.8。每一镜同一个值＝节奏一致；没有周期"
                                                  "运动的主体可以不套这个数。缺省 1.0"},
                    "light": {"type": "string", "maxLength": PROTAGONIST_LIGHT_MAX_CHARS,
                              "description": "跟着这个主体走的那束光（火把橙 / 车窗暖黄 / "
                                             "路灯 / 手机屏冷蓝），≤80 字；每镜按时代给颜色，"
                                             "不要所有时代都用一个强调色"},
                },
            },
            "qc": {"type": "string", "enum": list(QC_MODES),
                   "description": "质检档：preview（缺省，渲染前人看预览静帧，渲后像素闸只提醒）/ "
                                  "strict（无人值守，机器判据全部硬拦）"},
            "beat_sync": {"type": "boolean", "default": True,
                          "description": "有配乐时把镜头切点卡到拍上（只往后补、最多约半拍）"},
            "title": {"type": "string", "maxLength": 80,
                      "description": "片子标题（出现在界面与素材库，不进画面）"},
            # 片壳（2026-10-20）：当用户要求「纸底/顶部色条/左上圆钟/期号这类整片不变的
            # 外框」，或风格本身就是一档固定的版式时写它。选了以后**外框与底部字幕由片壳
            # 统一画**，镜头代码只画主体——于是没有「这一镜字幕压住主体」「钟换了位置」
            # 这类需要出图后再评审的问题。不选＝维持原样，镜头代码自己画整帧。
            "shell": {
                "type": "string", "enum": list(SHELLS),
                "description": "整片统一外框（缺省不套）。`paper-flat`：暖纸底 + 顶部"
                               "彩色细条 + 左上角固定圆钟 + 右上角期号小字 + 底部字幕，"
                               "全片同一位置。**选了它，镜头代码只画这一镜的主体与场景，"
                               "不再画底色/色条/钟/字幕，也不要再套母版**；"
                               "画布尺寸用 `useBody()`。用户没要求整片固定外框就不要写"},
            "shell_label": {
                "type": "string", "maxLength": SHELL_LABEL_MAX_CHARS,
                "description": "片壳右上角那行整片不变的小字（如「第 001 期 · 主题」）。"
                               "只在写了 `shell` 时有效；用户没给期号/主题就不写"},

            # 风格**由用户的话决定**，不是枚举（2026-10-17）：这里只给一个地方把「你从
            # 用户那句话里读出来的风格」写成一句话——它上计划卡给人看（理解歪了一眼就看得
            # 出来），也是你写每一镜代码时的同一份依据，整片因此是一种语言而不是十四种。
            # 不参与任何判据：机器量不出「杂志感」，硬判只会误伤。
            "style": {"type": "string", "maxLength": STYLE_MAX_CHARS,
                      "description": "整片画面风格，一句话。把用户那句话读成一套**完整的美术语言**"
                                     "（如用户说「高级杂志风」→「杂志编辑风：左对齐网格、眉题+大标题、"
                                     "细栏线、大留白、单一强调色」）。用户只给了一点方向时，"
                                     "**在他说的基础上自己补齐并往上加**，不是照单执行；用户没提就按选题自己定。"
                                     "每一镜的版式、字体层级、配色、线条都照这一套来，整片保持同一种语言。"
                                     "会显示在计划卡上"},
            "style_grammar": {
                "type": "string", "maxLength": STYLE_GRAMMAR_MAX_CHARS,
                "description": "把 `style` 那句拆成 3~5 个**可执行做法**（如网格 8px、色数 ≤6、"
                               "边缘硬切、扫描线间隔、光源方向、材质）。系统的美术导演会在"
                               "逐镜写码前自动补；你写了就以你的为准。不进计划卡旋钮。"},
            "reference_materials": {
                "type": "array", "items": {"type": "string", "maxLength": 64},
                "maxItems": 4,
                "description": "参考片素材 id（mat-…），可空。用户给了参考片/参考图时，"
                               "把附件里的 material_id 原样写进来；系统会抽参考帧随每一镜"
                               "发给写手，让它照着参考的画法画。不是计划卡旋钮。",
            },
            "skill_sections": {
                "type": "array",
                "items": {"type": "string",
                          "enum": ["chart", "three", "transition",
                                   "caption-matrix", "caption-hard"]},
                "maxItems": 5,
                "description": "按需加载的技能块（2026-10-23）。默认**什么都不用写**："
                               "写手只拿每一镜都用得上的那部分。这一片里确实有图表镜就加 "
                               "`chart`，有三维镜就加 `three`；`transition`（转场写法）"
                               "写手根本用不到——转场在拼接层，加了只是浪费。"
                               "`caption-matrix` 是字幕样式组合表、`caption-hard` 是闸的"
                               "硬约束说明，一般不用加。不是计划卡旋钮。",
            },
            "recurring_prop": {
                "type": "string", "maxLength": RECURRING_PROP_MAX_CHARS,
                "description": "整片贯穿装置：**一件**在画面世界里反复出现的具体道具，"
                               "让抽象概念在它身上看得见（如一枚随叙述变化的读数表、"
                               "一面映出数字的镜子）。只写一件，写清它长什么样；"
                               "它会出现在写每一镜的提示词里，合适就摆、不硬塞。"
                               "用户没提就按选题自己定。≤60 字，不进计划卡旋钮。"
                               "它只在逐镜写码时生效；已有 code 的镜改了它不会自动重画"
                               "（要重画就用 shots_patch 只交那几镜的 brief）"},
            # 风格包由**系统**按用户在入口选的名字从 PG 注入（`plan_motion`），
            # 模型不要自己写这一栏：写它既不会被信任，也会把一份正文塞进你的上下文。
            "style_pack": {
                "type": "object",
                "description": "系统注入的风格包（name/display/body/manifest），只读。"
                               "你没有选中风格包时不要提供这一项。",
                "properties": {
                    "name": {"type": "string", "maxLength": STYLE_PACK_NAME_MAX_CHARS},
                    "display": {"type": "string", "maxLength": STYLE_PACK_DISPLAY_MAX_CHARS},
                    "body": {"type": "string", "maxLength": STYLE_PACK_BODY_MAX_CHARS},
                    "manifest": {"type": "object"},
                    # 可执行层：库里的模板登记。系统注入，模型不许自己编。
                    "template": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "maxLength": STYLE_PACK_NAME_MAX_CHARS},
                            "display": {"type": "string",
                                        "maxLength": STYLE_PACK_DISPLAY_MAX_CHARS},
                            "module": {"type": "string",
                                       "maxLength": STYLE_TEMPLATE_MODULE_MAX_CHARS},
                            "components": {"type": "array",
                                           "items": {"type": "string", "maxLength": 64}},
                            "responsible": {"type": "string", "maxLength": 200},
                            "hash": {"type": "string", "maxLength": 64},
                            # 槽位：每个导出组件都能换一份同合约实现；没绑定就用默认组件。
                            "slots": {"type": "array", "maxItems": STYLE_SLOTS_MAX,
                                      "items": {"type": "object"}},
                        },
                    },
                },
            },
            # 槽位替换：用户说「主体换成一头鹿」时写在这里，值是**已登记的替换件**
            # 名字（见 style_pack.template.slots[].variants）。没登记的替换件不会在
            # 分镜期现场生成——先调 /style/templates/{name}/variants 换件。
            "style_overrides": {
                "type": "object", "maxProperties": STYLE_OVERRIDES_MAX,
                "description": "整片槽位替换：{组件名: 替换件名或替换件展示名}。"
                               "只填模板槽位清单里 `variants` 列出的名字；没列出的"
                               "不写、也不自己编。不写＝全部用模板默认组件。"
                               "不进计划卡旋钮。",
                "additionalProperties": {"type": "string", "maxLength": 64},
            },

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
                    # 只有这两样是**这一镜成立**的必要条件（没有代码就没有画面，没有文案
                    # 就没有这一镜要说的话）。`card` / `derived_from` / `subject` 从
                    # 2026-10-16 起是**选填**：卡库与 demo 是**供给**，不是义务——
                    # 「提供 llm 所有的工具」与「逼 llm 每镜都用」是两件事，原来那个
                    # required 把它们混成了一件。想照卡派生、想声明出处、想报主体，
                    # 照旧写；不想，就不写。
                    # 2026-10-18：`code` 不再必填。每镜代码默认由系统按 `brief` 单独写
                    # （见 `motion/shot_writer.py`）——主对话里不再有代码，几十镜代码不会
                    # 把它的上下文撑大、也不会写到十几镜之后开始自我复制。
                    # `brief` 也**不写进 required**：schema 表达不了「brief 或 code 二选一」，
                    # 而写了 code 的镜（局部改、手写 spec）本来就不需要 brief。两者都缺时
                    # 由 `_norm_shot` 带镜号报出来，那句话比 schema 的 required 更可执行。
                    "required": ["text"],
                    "properties": {
                        "id": {"type": "string",
                               "description": "镜号，省略则按顺序补 s01/s02…"},
                        "brief": {"type": "string", "maxLength": SHOT_BRIEF_MAX_CHARS,
                                  "description": "这一镜的画面：画什么（文案点到的具体物件/人/动作、"
                                                 "它们的位置与相互关系）、怎么构图、怎么分几拍动起来。"
                                                 "1~3 句。系统按它、整片 style 与这一镜文案单独写这一镜的代码"},
                        "entities": {
                            "type": "array", "maxItems": MAX_ENTITIES_PER_SHOT,
                            "items": {"type": "string", "maxLength": ENTITY_MAX_CHARS},
                            "description": "这一镜文案里点到、要画进画面的**实体**（人/物/地点/"
                                           "动作的结果），一件一个名词短语，通常 2~4 个。"
                                           "它们同处一个场景、彼此有位置/遮挡/朝向关系，"
                                           "不是并排的小图标；文案只点到一件时就写一件，别硬凑。"
                                           "系统按这份清单检查「是不是每件都画了、都认得出来」。"
                                           "美术导演没补时可由系统补；不进计划卡旋钮"},
                        # 渐进式风格包的「这一镜点哪几件能力」。名字由风格包正文的
                        # `<!-- section: … -->` 块声明，模型在 schema 与导入白名单里
                        # 都查不到——所以索引由服务端注入（`_caps_index_block`），
                        # 这一栏只负责承载名字。不填＝由美术导演按 brief 补。
                        "caps": {
                            "type": "array", "maxItems": MAX_CAPS_PER_SHOT,
                            "items": {"type": "string"},
                            "description": "这一镜用到所选风格包里的哪几件能力（0–3 个；"
                                           "名字见风格包给出的能力索引）。选了就会把那一件"
                                           "的做法正文加载给写手，所以只写这一镜真会用的。"
                                           "不填由美术导演按 brief 补。不进计划卡旋钮"},
                        "sections": {
                            "type": "array", "maxItems": 2,
                            "items": {"type": "string",
                                      "enum": ["chart", "three", "transition",
                                               "caption-matrix", "caption-hard"]},
                            "description": "这一镜额外要加载的技能写法（0–2 个，一般不用写）。"
                                           "写手默认只拿每一镜都用得上的那部分；这一镜是图表"
                                           "就写 `chart`、是三维场景就写 `three`。"
                                           "`transition` 别写——转场在拼接层，写手用不到。"
                                           "不填就是默认那部分。不进计划卡旋钮"},
                        "code": {"type": "string", "maxLength": MAX_CODE_BYTES,
                                 "description":
                                     "**默认留空**：每一镜只交 brief，代码由逐镜写手写——"
                                     "它有专门的写作规则、动效骨架、配方卡与你挂的技能正文，"
                                     "在主对话里现写反而更差（真机 2026-10-24：模型自己写了"
                                     "12 镜，导演与写手全被跳过，brief 停在 195 字、没有"
                                     "构图骨架、光效只到渐变近似）。"
                                     "**只有用户明确要求你自己写代码时**才填。"
                                     + _CODE_DOC},
                        "code_from": {"type": "string",
                                      "description": "复用另一镜的代码（填那一镜的 id，本镜不写 code），"
                                                     "只换 text / fields / 色板。长片用它：十几种版式"
                                                     "撑几十镜，不必每镜交一整段代码"},
                        "text": {"type": "string", "maxLength": MAX_TEXT_CHARS_WIDE,
                                 "description": f"这一镜要说的一句话，只进母版字幕带。"
                                                f"容量按画幅：竖屏 ≤{MAX_TEXT_CHARS} 字、"
                                                f"横屏 ≤{MAX_TEXT_CHARS_WIDE} 字"
                                                "（超了拆镜，不裁句子）"},
                        "text_en": {"type": "string", "maxLength": MAX_TEXT_EN_CHARS,
                                    "description": "同一句话的英文，画在中文下面一行（中英双字幕）。"
                                                   "与 text 逐句对应、语气一致，不逐字硬译；"
                                                   "只写这句话本身，不加引号、方括号或 ⌈⌉ 这类装饰符号。"
                                                   "可省：省了就只排中文那一行"},
                        # 真机 run 7dcaa7246e92（2026-10-19，13 镜娱乐史）：模型只写了
                        # 14~17 字/镜的旁白，目标 170s 估出来 45~74s，被时长闸打回；它
                        # 看不到这一栏（schema 里没列），只能按报错去灌水加镜——13 个时代
                        # 被拆成 37 镜。时长不够时**先留长画面**才是这套慢节奏风格的做法。
                        "min_duration_sec": {
                            "type": "number",
                            "description": "这一镜**至少**留多久（秒）。旁白短、目标时长长时"
                                           "用它把画面留长（人继续原地走、背景继续滚），"
                                           "不要把 13 个时代灌水拆成几十镜来凑时长。"
                                           "不写＝按时长对齐自动决定。0~60"},

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
                        # 不进 required——每一镜都有自己的画法，填哪一条取决于**代码里
                        # 真用了哪条**，所以它的必填性由 check_subject_named 按
                        # 「这一镜代码里有没有画法通道」逐镜定，schema 这一层写不出来。
                        "subject": {"type": "string", "maxLength": SUBJECT_MAX_CHARS,
                                    "description": "这一镜画面上**能被认出的那个东西**"
                                                   "（名词短语，2~24 字），如「活字盘」"
                                                   "「三条等高线」「一张 1900 年的地图」。"
                                                   "建议写具体器物而不是抽象词"
                                                   "（理念/趋势/意义…画不出形状）"},
                        "subject_channel": {"type": "string", "enum": list(SUBJECT_CHANNELS),
                                            "description": "主体是用哪条画法通道画的"
                                                           "（rough/three/katex/vector/canvas）。"
                                                           "申报了就必须与代码里真用上的通道一致"
                                                           "（闸⑯ 按代码对账）；不填只提醒"},
                        # 2026-10-16 删掉了 `assets` 栏（每项 name/sha256/ext，
                        # 渲染前按 sha256 从 asset_library 取字节投进渲染容器）。
                        # 三条取材路拆掉之后没有任何东西能填进这一栏，而 schema 里
                        # 留着它，等于在契约上给模型开一个「这里可以摆图」的出口。
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
                        # 2026-10-16 删掉了 `reveal` 栏（闸㉘ 判它的几何与时序、
                        # ㉗ 拿真帧比色判「未开始的区域是不是还是纸底」）。它申报的
                        # 主语是**一整幅 raster 插画**：`canvas` 要与库行 width/height
                        # 对账、区域坐标以插画像素为单位。画面上屏那条路没了，这一栏
                        # 也就没有对象——所以整块摘掉，而不是留着让模型申报一个没人
                        # 读的栏。
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
                        "caption_style": {"type": "string",
                                          "enum": list(CAPTION_STYLES),
                                          "description": "**这一镜**的字幕样式"
                                                         "（可省略＝跟整片 `caption_style`）。"
                                                         "用途：整片走 `on_picture`（字贴画面、"
                                                         "无底条），但某几镜要 `none`（整屏插画、"
                                                         "不想压字）或 `big_center`（一句话就是"
                                                         "全片重点）。**它与整片那一档都会进"
                                                         "缓存键**（字幕是画进切片里的）"},
                        "highlight_style": {"type": "string",
                                            "enum": list(HIGHLIGHT_STYLES),
                                            "description": "**这一镜**的高亮样式（可省略＝跟整片）。"
                                                           "被点名的词怎么被点出来：换字色 / 逐词点亮 "
                                                           "/ 底色块 / 纸感笔触 / 不强化"},
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
#: 自动**拉长**时，一镜最多拉到它自己原来时长的多少倍。
#:
#: 2026-10-17 真机片（`rj-e0d34b53`）：模型给 14 镜写的是 6~11 秒，系统为了凑 210 秒
#: 把每一镜统一拉到 15.37 秒——而镜头代码的时间轴是按模型自己那几秒写的（s07 的动画
#: 止于第 136 帧，整镜 461 帧），于是每一镜的后半截都成了定格，用户看到的是「画面停很久」。
#: 拉长改的只是驻留、不改代码，所以拉得越多，定格越长。1.25 倍以内，多出来的那一截
#: 落在每镜收尾本来就有的静止段里还看不出来；再多就宁可片子短一点、回执里说「要更长就加镜」。
#: 压短不受这一条限制（压短只会让动画更紧，不会多出定格）。
MAX_STRETCH_RATIO = 1.25


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
    # 每镜拉长的封顶：绝对上限与「自己原长的 MAX_STRETCH_RATIO 倍」取小（见那个常量的注释）
    caps = [min(MAX_DWELL_SEC, max(s, s * MAX_STRETCH_RATIO)) for s in secs]
    # 迭代按比例分配：封顶/触底的镜固定下来，剩余差额摊给其余镜
    for _ in range(8):
        diff = want - sum(new)
        # 只把差额摊给还能往那个方向动的镜：拉长时没封顶的，压短时没触底的
        free = [i for i in range(len(new))
                if (new[i] < caps[i] - 1e-6 if diff > 0 else new[i] > floors[i] + 1e-6)]
        if abs(diff) < 0.05 or not free:
            break
        base = sum(new[i] for i in free) or 1.0
        for i in free:
            new[i] = min(caps[i], max(floors[i], new[i] + diff * new[i] / base))
    for x, sec in zip(shots, new):
        x["min_duration_sec"] = round(sec, 2)
    out = dict(spec, shots=shots)
    got = sum(new) - lap
    note = (f"无旁白：已把各镜驻留按比例从合计 {total:.0f}s 调到 {got:.0f}s 以对齐目标 "
            f"{target:.0f}s（每镜 {min(new):.1f}~{max(new):.1f}s）")
    if got < target * 0.9:
        # 镜数按「每镜约 9 秒」折算——那是 8~12 秒一镜的中间值，动画铺得满、也不赶。
        need = max(len(shots) + 1, round(target / 9.0))
        note += (f"；每镜最多拉到自己原长的 {MAX_STRETCH_RATIO:g} 倍（再拉后半截就是定格），"
                 f"仍短于目标——要更长就加镜（约 {need} 镜），别把单镜拉长")
    return out, [note]


def duration_block(spec: dict[str, Any], target: float | None, *,
                   long_factor: float = 1.6, short_factor: float = 0.5) -> str | None:
    """估算整体偏离目标到不值得开烧时 → 一句带具体改法的拒因；否则 None。

    改法按有无旁白分开给：无旁白说「加镜 / 加长驻留」（没有语速可调），有旁白说
    「加文案 / 加画面驻留 / 加镜」。**不把「放慢语速」当凑时长的办法**：正常语速是
    用户要的听感，降速只用于用户明确要求放慢的片子。镜数建议按**每镜平均时长**
    折算，而不是按字数。
    """
    if not target or target <= 0:
        return None
    low, high = estimate_sec(spec)
    n = len(spec.get("shots") or [])
    # **按中点判，不按下界**（2026-10-23）。下界是「文案刚好念完」的最乐观值，
    # 上界含驻留余量；用下界判等于把区间里一半的情形放过去——真机上 12 镜 ×30 字
    # 估成 79~122 秒、目标 60 秒，下界 79 < 81（60×1.35）居然不拦，于是照常开写，
    # 407 秒写完才发现超了一半。中点 100 > 81，这一道就响了。
    mid = (low + high) / 2.0
    if n == 0 or not (mid > target * long_factor or high < target * short_factor):
        return None
    too_long = mid > target * long_factor
    per = max(0.5, (low + high) / 2 / n)
    if too_long:
        fix = (f"删到约 {max(1, round(target / per))} 镜，或把每镜文案缩短"
               + ("" if spec.get("narration") else "、驻留调短"))
    elif spec.get("narration"):
        # 「加镜」放最后：慢节奏风格的时长不够时，先给每镜写 `min_duration_sec`
        # 把画面留长（旁白说完人还在走、背景还在滚），或把文案写长；灌水加镜会把
        # 一条 13 个时代的片子拆成几十镜。真机 run 7dcaa7246e92 的报错只给了
        # 「加镜/放慢语速」，模型于是准备把 13 镜加到 37 镜。
        # 语速不再作为凑时长的手段：用户要的是正常朗读速度（2026-10-20）。
        fix = ("先给每镜写 `min_duration_sec` 把画面留长（旁白说完继续走）、"
               "把每镜文案写长；仍不够再加镜"
               f"（约 {max(n + 1, round(target / per))} 镜）。不要靠放慢语速凑时长")
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
                f"加镜、把文案写长或给每镜留更长的画面；不要靠放慢语速凑时长"]
    if low > target_sec * 1.35:
        return [f"按当前文案估算 {low:.0f}~{high:.0f} 秒，比目标 {target_sec:.0f} 秒长不少："
                f"删镜或缩短每镜文案（当前 {len(spec.get('shots') or [])} 镜）"]
    return []
