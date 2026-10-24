# -*- coding: utf-8 -*-
"""逐镜写代码：主对话只交 `brief`，每一镜的 TSX 由这里**单独一次短调用**写出来。

**它治的是什么。** 画面只有「代码逐帧画」一条路，而整片四十镜的代码让主对话一次写完，
两件事一起坏：

* 主对话的上下文被几十段代码撑大，**70 万字节的历史**换来的是缓存全 miss（真机 run
  32affa23c933 两次烧掉十块钱）；
* 长上下文里模型会开始自我复制——同一天的真机 run 里，同一段代码被写进 45 个文件，
  而 lint 已经明确报错的那一次调用又被原样重发。这不是提示词能劝住的，是上下文太长
  之后的退化。

所以把「写代码」从主对话里**拿出去**：主模型只决定这一镜画什么（`brief` + `text`），
写代码的那次调用上下文很短、任务只有一镜、拿到 lint 的报错可以当场改。这条分工与
`selfcheck.style_review` 同一条纪律——**让判断发生在它还能改得动的地方**。

**为什么正文从 SKILL.md 读，不在这里抄一份。** 规则只有一份（技能正文），这里抄第二份
就是「工具说绿、闸说红」那种漂移的标准形状。`_contract()` 按标题与 `<!-- section: … -->`
标记截取，正文改了这里自动跟着变。

**为什么整片设定在 system、单镜任务在 user。** 同一条片子的每一镜共享同一段前缀
（技能正文 + 整片设定 + 全片镜号清单），DeepSeek 的前缀缓存按字节命中——二十镜的写手
调用里，只有最后那一小段是 miss。顺序一反过来（每镜把设定塞在末尾），缓存一次都命不中。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import code_spec as cspec
from . import components
from . import recipe_index
from . import style_sections
from . import remotion_client as rc

__all__ = ["write_shot_code", "rewrite_shot_code", "WRITER_PARALLEL", "WRITER_ATTEMPTS",
           "WRITER_MOTION_CHECK", "WRITER_MOTION_RETRIES"]

#: 同时写几镜。写手调用是纯网络请求（不占渲染串行链）。
#: 2026-10-18 从 5 提到 8：一条 28 镜的片子，写码是计划阶段最长的纯等待段；
#: 8 路在真机上没有触发供应商限流，墙钟约省三分之一。撞限流就设
#: `MOTION_WRITER_PARALLEL=5` 退回去。
WRITER_PARALLEL = max(1, int(os.environ.get("MOTION_WRITER_PARALLEL", "8") or 8))

#: 一镜最多写几次（首次 + 两次照 lint 报错重写）。两次修不好通常不是手滑，而是 brief
#: 本身没画法——继续重试只会烧钱，交给调用方如实报出来。
WRITER_ATTEMPTS = 3

#: 单次编写的输出上限。一镜代码 2~5KB，给 8192 足够，且不让一次跑飞。
#: 2026-10-20 提到 12000：带「本片第一镜已定画法」的整份代码当基准时，模型会把
#: 人物的路径代码写得比平时长（实测 470~520 行），8192 会在收尾处截断，表现是
#: 连续的 `[parse-error] '>' expected`——不是它不会写，是没写完。供应商实测
#: max_tokens 到 32000 都收；仍一镜 40KB 上限挡在 lint 前面。
WRITER_MAX_TOKENS = max(4096, int(os.environ.get("MOTION_WRITER_MAX_TOKENS", "12000") or 12000))

#: 单次编写超时（秒）。一镜 TSX 常要写四五十秒，60 秒的默认预算是给「一次工具决策」的；
#: 这个值要装下「思考 + 几千 token 输出」。
WRITER_TIMEOUT_SEC = 240.0

#: 写代码用哪个模型：**跟随用户在页面「设置」里配的那一个**（`_resolve_request` 每次
#: 现读，改完不用重启）。
#:
#: 2026-10-22 撤掉 `MOTION_WRITER_MODEL`：它是 2026-10-18 为 A/B 加的口子，结果在
#: `.env` 里被钉成 `deepseek-reasoner` 之后，页面上配的 flash 一次都没用上——**用户在
#: 前端的配置是后端所有 LLM 调用的唯一来源**，不允许任何环境变量在背后覆盖它。要换
#: 写手模型，去页面上换；要 A/B，在页面上切一次再跑。

#: 写完一镜、过完闸① 之后，要不要再**真渲一版草稿**数闸⑭（动效事件密度）。
#:
#: 为什么必须在这里判：闸⑭ 判的是成片像素，而不是代码里 `interpolate()` 的声明——
#: 2026-10-18 真机那一版（spec5）三镜分镜全绿、预览静帧全绿，成片渲染时三镜一起
#: 因闸⑭ 退回。线稿的整幅平均差只有 0.05~0.3，要过 1.5 必须做**大块面**的动，
#: 而这条事实只有渲出来才看得见。放在写手这里，模型拿到的是「这一镜数到几段、
#: 两段隔多少帧」的实测读数，还能当场重写；放到成片渲染那里，代价是三镜全片白烧。
#:
#: 草稿档（scale 0.25）**不换判据**：闸⑭ 读 64 px 宽灰度差，与输出分辨率无关
#: （`sampleGray` 先 `scale=64:-1` 再转灰度；0.25 档的 480×270 与 0.4 档的 768×432
#: 缩到 64 宽之后是同一条 64×36 灰度序列）。0.4 → 0.25 把每版草稿的像素少画 60%，
#: 2026-10-18 真机 32 镜那条片子因此把写手阶段的渲染时间从 ~30 s/版压到 ~15 s/版。
WRITER_MOTION_CHECK = os.environ.get(
    "MOTION_WRITER_MOTION_CHECK", "1") not in ("0", "false", "no")

#: 草稿渲**抽检间隔**：每 N 镜验一次（第一镜必验）。0 ＝ 每镜都验（旧行为）。
#:
#: 2026-10-21 真机量到的账：一镜草稿是**整镜 270 帧**的渲染（不是一帧），8 路写手
#: 同时跑时渲染容器稳定在 1892% CPU（20 核里 19 核）、8.3 GB 内存，写手阶段每镜
#: 58 秒里有一半是在排队等渲。更糟的是这把渲染槽整个占住：同一条命令里另发一张
#: 预览静帧，**120 秒超时没渲出来**——预览、成片、草稿排的是同一个队。
#:
#: 抽检的口径：第一镜（定全片画法基准的那一镜）**永远验**，其余镜每 N 镜验一镜。
#: 没被抽到的镜不是「通过」，回执与日志里记成 `skip`；成片渲染那一趟的闸⑭ 仍会
#: 逐镜判（`MOTION_QC=preview` 只提醒、严格档打回），所以这不是「不判了」，
#: 而是把「每镜都现场停一下」换成「抽样现场停、其余交给成片闸」。
WRITER_MOTION_EVERY = int(os.environ.get("MOTION_WRITER_MOTION_EVERY", "4") or 4)

#: 草稿档参数。`scale` 只影响产物体积，不影响闸⑭ 读数（见上）。
WRITER_DRAFT: dict[str, Any] = {"draft": True, "scale": 0.25}

#: 闸⑭ 红之后**最多重写几稿**。与 `WRITER_ATTEMPTS`（闸① 的总预算）分开：
#: 2026-10-18 真机 32 镜那条片子里，第三稿的读数与第二稿几乎一样（都是「1 个变化段、
#: 峰值 13~14」）——模型已经把一个巨大的连续位移当成两拍，再写一稿也不会自己拆开。
#: 每多一稿就是一次 270 帧真渲（2 路并发 ≈ 30 s），换不来判据的改善。留 1 次重写，
#: 红着就照收、把读数写进回执，让成片闸和局部改去收尾最便宜。
WRITER_MOTION_RETRIES = 1

#: 闸⑭ 动效密度在**写镜阶段**要不要触发重写。
#:
#: **2026-10-23 白天关掉。** 用户的原话：「我要的是 llm 加 skill 加 remotion 是不是发挥最大
#: 能力，你这样用闸或者其他东西限制，会影响效果懂不懂」——他说得对，而且这轮有硬证据：
#:
#: * 闸⑭ 要求「≥2 段先后变化、每段要够 1.5 的整幅像素差」，唯一的达标手段是**满宽色带**；
#: * 而剪影/平涂这类形式里，参考片从头到尾没有一条横贯画面的纯色条——**加了就是一眼假**；
#: * 结果：写手被逼着往片子里塞它本来不该有的东西，s07 卡了三轮，最后系统自己写下
#:   「照收这一稿——**这不是通过**」。
#:
#: **2026-10-23 晚上重新打开。** 前提是判据先改了：`motionsample.sampleGray` 从
#: 「整幅平均差」改成「**区块内平均差的最大值**」（4×3 分块）。原来「一根 2px 线从左
#: 长到右」在 64×36 采样图上整幅差只有 0.05~0.3，永远过不了 1.5——这才是逼出满宽色带
#: 的真因；分块之后同一个镜头它所在那一块的差是整幅的 12 倍，1.5 就够得着，而压缩噪声
#: 在区块内被平均掉（静帧区块差 <0.5）。**判据变了，这条闸才敢重新拦人。**
#:
#: 想恢复旧行为：把它设回 False。
MOTION_DENSITY_BLOCKS = True

#: 写手侧自查的**余量**：草稿档每段的峰值要高出地板线这么多倍才算稳。
#:
#: 2026-10-18 真机测出来的那条：s03 的大面板在草稿档峰值 1.63（地板线 1.5，刚过），
#: 同一段代码在成片档是 1.484——**差一点就从「过」翻成「退」**，整份分镜在烧完
#: 两镜之后被拦。草稿档与成片档的像素基数不同（scale 0.4 vs 1），灰度的边缘与
#: 编码噪声都会动，压线的读数不能当通过。1.35 让 s03 那种 1.6 左右的段触发重写，
#: 而真正的大块面（4 以上）一次就收。
WRITER_MOTION_MARGIN = 1.35


def _read_skill() -> str:
    path = cspec._SKILL_DIR / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i:j if j >= 0 else len(text)].strip()


def _section(text: str, name: str) -> str:
    m = re.search(rf"<!--\s*section:\s*{re.escape(name)}\s*-->(.*?)<!--\s*/section\s*-->",
                  text, re.S)
    return m.group(1).strip() if m else ""


#: 按标题截取的几段（技能正文里没有 `<!-- section -->` 标记的那些）。
_HEADING_SPANS: tuple[tuple[str, str], ...] = (
    # 2026-10-19 改过标题：旧标题「镜头代码：你自己写」会让模型以为交付物是
    # 工作区里的 shots/*.tsx，预览完反复重写草稿。旧标题保留一条，老的技能副本
    # （用户自己存过的那份）仍能截到这一段。
    # 2026-10-20：标题第三次改词（「代码由系统逐镜写」）。旧标题都保留——用户自己
    # 存过的技能副本、镜像里的旧种子仍能截到这一段；新标题不登记时**整段静默丢失**，
    # 写手就收不到「你交 brief、系统写码」这条最重要的分工。
    ("# 这一镜画什么：你交 `brief`，代码由系统逐镜写", "# 一镜有哪些栏"),
    ("# 这一镜画什么：你交 `brief`（代码由系统写，也可以自己写）", "# 一镜有哪些栏"),
    ("# 镜头代码：你自己写", "# 一镜有哪些栏"),
    ("# 字自己排", "# 版式档：满幅 bleed 与底色 bg"),
    ("# 版式档：满幅 bleed 与底色 bg", "# 转场与进出场"),
)

#: 带标记的几段。
#:
#: **2026-10-23 清空。** 这五段原本是「解释闸」的文字，闸撤了（见
#: `code_spec.WHOLE_FILM_GATES` 那一段），它们就没有对象了：
#:
#: · `subject-naming`（1,486 B）闸⑯ 主体具名；
#: · `motion-density-gate`（6,874 B）闸⑭ 动效密度；
#: · `motion-anchors`（1,445 B）闸㉑ 动效锚定；
#: · `draw-floor`（1,393 B）闸⑦ 每镜必画；
#: · `draw-parts`（10,492 B）零件库——技能自己写着「用不上就忘掉」，
#:   却在每一镜里占 10.5 KB。用户判断：模型自己画得更好，不必给零件表。
#:
#: 合计每镜省 **21,690 字节**。段本身在 `SKILL.md` 里**不删**——它们仍是有用的
#: 说明，只是不再无条件塞给写手；将来若要做「按镜取段」，`style_sections.section_text`
#: 按名字取得到。
_MARKED_SECTIONS: tuple[str, ...] = ()

_contract_cache: dict[str, tuple[float, str]] = {}

#: 技能正文里「两处满宽色带」那一段（`<!-- bands-only -->` 包着）。风格包
#: 写了 `fullwidth_bands: false` 时整段抽掉，换成 `_NO_BANDS_NOTE`。
_BANDS_ONLY_RE = re.compile(r"\n?<!--\s*bands-only\s*-->.*?<!--\s*/bands-only\s*-->", re.S)

#: **2026-10-23：`art-direction`（11,230 B）不进写手提示词。**
#:
#: 那一段是**计划期的字段说明**——8 套色板的取值表、`art` 六个字段怎么写、
#: `protagonist` 怎么描述、3 档风格组合。写手要的是**值**不是**定义**：
#: 整片色板、`art`、`protagonist` 的最终取值由 `_film_block` 的
#: `_art_block` / `_protagonist_block` 单独注入，那一段在这里纯属重复。
#:
#: 为什么用「整段抽掉」而不是改 `_HEADING_SPANS`：那一段落在
#: 「# 这一镜画什么」到「# 一镜有哪些栏」的大切片**内部**，而 `_between`
#: 是纯文本切片，改边界会把同一切片里的 `code-rules` 一起切掉。抽段正则
#: 只删这一段，其余字节不变。
_ART_DIRECTION_RE = re.compile(
    r"\n?<!--\s*section:\s*art-direction\s*-->.*?<!--\s*/section\s*-->", re.S)

#: **按需加载（批 2）**：这几块默认**不给**，`spec["skill_sections"]` 点名才给。
#:
#: 判据是「这一块讲的是**定义**还是**这一镜要用的东西**」：
#: · `chart` / `three` —— 只有图表镜、三维镜需要，别的镜白读；
#: · `transition` —— **写手压根不管接缝**：转场跑在拼接层（`mediaops.concat` 的
#:   xfade），镜头代码里没有转场可写。这一块是给「有人手搓 spec」的说明，不是
#:   写手的输入；
#: · `caption-matrix` —— 字幕样式 × 高亮样式的组合表是**定义**，写手拿到的
#:   是**值**（`_film_block` 的 `caption_block` 直接写「字幕：outlined × plain」），
#:   而落地只发生在 `<CaptionText/>` 这一件里；
#: · `caption-hard` —— 「闸会逐条判」的说明，闸撤了就没有对象。
#:
#: 合计 9,406 字节。要恢复某一块：在计划卡/测试里写 `skill_sections: ["chart"]`。
_RULES_OPTIONAL: dict[str, str] = {
    "chart": "## 4 图表与路径",
    "transition": "## 4.5 转场",
    "three": "## 5 三维",
}
_SKILL_OPTIONAL: dict[str, str] = {
    "caption-matrix": "## 字幕两维",
    "caption-hard": "## 硬约束",
}


def _drop_md_block(text: str, heading: str) -> str:
    """删掉一个 markdown 块：从 ``heading`` 那一行起，到下一个 H1/H2 之前。

    为什么按行扫而不是正则：`## 4 图表与路径` 与 `## 4.5 转场` 只差一个字符，
    正则里 ``## 4`` 会把 4.5 一起吃掉；按行做前缀匹配再判「下一个标题」，
    两块各删各的。删不到（标题改过词）就原样返回——宁可多给，不能少给。
    """
    out: list[str] = []
    dropping = False
    hit = False
    for line in text.splitlines():
        if not dropping and line.startswith(heading):
            dropping = True
            hit = True
            continue
        if dropping and re.match(r"^#{1,2} ", line):
            dropping = False
        if not dropping:
            out.append(line)
    return "\n".join(out) if hit else text


def _extra_sections(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """`spec["skill_sections"]` → 认识的按需块名（不认识的一律忽略）。

    认哪几个名字**只有一份清单**（`code_spec.SKILL_SECTION_NAMES`）：schema 的 enum、
    `validate_code_spec` 的过滤、这里，三处同源。写手的两张表按名字挂上去，
    键与那份清单必须逐值相等（`tests/test_shot_writer.py` 有一条断言钉住）。
    """
    raw = spec.get("skill_sections") or ()
    if isinstance(raw, str):
        raw = [raw]
    known = set(cspec.SKILL_SECTION_NAMES)
    out: list[str] = []
    for x in raw:
        name = str(x or "").strip().lower()
        if name in known and name not in out:
            out.append(name)
    return tuple(out)

_NO_BANDS_NOTE = """# 动效密度（闸⑭）：本片不用覆盖层

判据不变：**≥2 段先后变化**，相邻两段起点相隔 **≥0.4 s**。但本片**不要满宽色带、
满宽面板、任何横贯画面的纯色条**——那是平面版式的做法，在任何有场景的片子里都一眼假
（写实场景里不会有一条横贯画面的纯色条；剪影场景里，参考片从头到尾没有一条色带）。

整幅变化由**场景自己**做，它们在 64 px 灰度差上比色带还大：

1. **光扫过整幅**（最优先，剪影系与写实系都适用）：火塘的火忽然亮一档、天光从深蓝
   推到暖橙、探照灯横着扫过、油灯被点亮、屏幕由暗转亮。整帧的亮度与色温一起动，
   读数常常是 10 以上——**参考片每一镜的「世界在动」都是这一种**。
2. **雾、烟、雪、火星的浓度从无到有**：整幅的粒子层，不是几根线。
3. **相机推近或横移**：整帧都在变（本片有 5% 缓推，但单靠它不够，读数太小）。
4. **主体的大动作**：大物件的位移/展开/倒下。

两处分在不同时间：第一处在前 40%，第二处在 55%~80%，中间至少隔 0.4 s。

**不要把力气花在「让人走出去」上。** 参考片里那个人是原地踏步的：整镜只挪几个
百分点，换时代靠硬切。整幅的变化要由光和天气来给。"""


def _contract(bands: bool = True, extra: Sequence[str] = ()) -> str:
    """技能正文里与「写一镜代码」直接相关的那几段 + Remotion 写法手册。

    ``extra`` 是**按需块**的名字（见 `_RULES_OPTIONAL` / `_SKILL_OPTIONAL`）：
    默认什么都不给，只留每一镜都用得上的部分。图表镜传 ``("chart",)``、
    三维镜传 ``("three",)``，由 `_extra_sections(spec)` 从
    ``spec["skill_sections"]`` 读出来。

    按两份文件的 mtime 缓存，改了任一一份就自动重读。手册
    （``remotion-rules.md``）是官方规则的摘编，与技能正文分开：正文讲**这条产品**
    要什么，手册讲**Remotion 本身**怎么写对。

    ``bands=False``（风格包写 `fullwidth_bands: false`）时，正文里「两处满宽色带」
    那一段整段抽掉、换成「动效由相机与光来做」。为什么不靠风格包那一段去压：
    2026-10-22 的三维片里，风格包已经写了不要色带、正文的示例更具体，23 镜照抄了
    示例。**互相矛盾的指令，模型总会挑更像"示例"的那一条**——所以这里直接不给它。
    """
    path = cspec._SKILL_DIR / "SKILL.md"
    rules = cspec._SKILL_DIR / "remotion-rules.md"
    try:
        mtime = (path.stat().st_mtime, rules.stat().st_mtime)
    except OSError:
        return ""
    key = ("c" if bands else "c-nobands") + "|" + ",".join(sorted(extra))
    hit = _contract_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    text = _read_skill()
    # 2026-10-23：计划期字段说明（色板表 / art / protagonist 的写法）不进写手提示词，
    # 那些值由 `_film_block` 的 `_art_block` / `_protagonist_block` 单独给。
    text = _ART_DIRECTION_RE.sub("", text)
    # 批 2：按需块。没点名就整块抽掉（见 `_SKILL_OPTIONAL` 那张表）。
    for name, heading in _SKILL_OPTIONAL.items():
        if name not in extra:
            text = _drop_md_block(text, heading)
    if not bands:
        text = _BANDS_ONLY_RE.sub("", text)
    parts: list[str] = []
    for start, end in _HEADING_SPANS:
        got = _between(text, start, end)
        if got:
            parts.append(got)
    for name in _MARKED_SECTIONS:
        got = _section(text, name)
        if got:
            parts.append(got)
    try:
        rules_text = rules.read_text(encoding="utf-8").strip()
    except OSError:
        rules_text = ""
    for name, heading in _RULES_OPTIONAL.items():
        if name not in extra:
            rules_text = _drop_md_block(rules_text, heading)
    if rules_text:
        parts.append(rules_text)
    out = "\n\n".join(parts)
    if not bands:
        out += "\n\n" + _NO_BANDS_NOTE
    _contract_cache[key] = (mtime, out)
    return out


_SYSTEM_HEAD = """你是这条「图形科普片」的镜头画师。你一次只写**一镜**的 React/TSX 代码。

你画的不是「卡片 + 文字」，是**一幅会动的插画**：成片里这一镜单独截一帧，就该是一张
能拿出去的画。落笔之前先把三件事想清楚——画什么、光从哪来、字放哪。

**先读文案，圈实体**：这一镜文案里出现的人、物、地点、动作的结果，每一个都是要画进
画面的**实体**；一镜通常 2~4 个。brief 里若给了清单，清单里的每一件都要在；
文案只点到一件时就画一件，**别硬凑**。画面不是「选一个画」，而是让这几件东西
**同处一个场景**：谁在前谁在后、谁被谁挡、谁朝向谁、光从哪来、各自落地影落在哪。
整帧只有字和色块、或把实体并排摆成一排图标＝这一镜没画。
用户或整片给了风格方向，那只是起点：在它之上自己组合媒介、构图、色彩与细节，
把这一镜该有的东西长出来。**照单执行是下限，不是标准**；用户只给一点点方向时，
剩下的由你定（媒介可以从各家的画法里各取一点，组合成这一片自己的语言，只要整片一致）。
**整片设定里的风格、色板、美术方向与贯穿主体是用户的选择**：按它们画，让整片像一条
片子。但它们只定「这一片长什么样」，不定「这一镜画什么」——内容、构图与画法由你按
文案自己决定。本片**不用任何模板**：舞台、人物、字幕、光，全部由你按这一镜的需要画。

纪律（先读一遍再落笔）：
· 画面全部由代码逐帧画出来：不许联网、不许引用任何外部图片/字体/脚本，只用下面技能正文
  白名单里的 import。
· **画法不限**：裸 SVG（path / 渐变 / 滤镜 / mask）、`<canvas>` 上手写、`three`、
  `roughjs`、`katex` 都可以，按这一镜的题材挑。`@dg/lib/draw/*` 与 `@dg/lib/brush`
  是**可选参考件**——用得上就用，用不上就把它们忘掉，自己画。
· 每一镜要把 brief 里的**实体都画出来**（不是只挑一个）。先分层（远/中/近）、
  再定主体（占画面 40%~70%）、其余实体按远近与遮挡落位，最后补细节；不要把元素
  平均摊在画面上，那既不是构图，也不是一个场景。
· **画出来 = 有体积，不是有轮廓。** 每个实体的外轮廓里要填实（色块/体块），至少分出
  受光面与背光面两档明度，落地要有影子；只有一圈描边/线框、里面和背景一个明度，
  等于没画（真机上的「霓虹线框骨架」「空心方框钱包」就是这么被判红的）。像素风也一样：
  用几档像素色块堆出体积与材质，不是只用亮线勾边。
· **识别细节要落到代码里。** 每件实体除了外轮廓，还要有「少了它就不像」的标志物与
  2~4 个部件——钱包的拉链齿与探出的钞票边角、CRT 的屏幕内容与旋钮、骨架的肋骨/脊柱/
  骨盆、机器人的关节与液压管。细节用与主体**明显不同**的色阶画，别用同色细线糊在
  深色块上（深底上的深线＝看不见）。
· **缩到 1/3 看一遍**：这一帧缩到 640px 宽，实体还认得出吗？细节还分得开吗？糊成
  一团就加大明暗差、加粗、把主体放大——评审看的正是这一档尺寸。
· 质感决定「像手画的」还是「像 PPT」：笔触要有粗细变化，画面要有纸纹/颗粒/柔光/暗角，
  物件落地要有影子。纹理用固定 seed（`feTurbulence` 的 `seed` 必须写死），
  **不许 `Math.random`**——逐帧重跑同一份代码，随机数会让画面抖，两次渲染也不是同一部片。
  颗粒用现成的 `<Grain opacity={0.5} seed={17} blend="normal" />`，别自己写一块铺满
  画幅、挂 `feTurbulence` 的 `<rect>`：那张噪声每帧重算一遍，一帧要多花两三倍时间。
· 字幕是**叠在画面上**的一行小字，不是画面下方的一条横幅。每一镜都要把这一镜的整句
  排进构图，缺省写法：`<CaptionText/>` 套一个定位盒子放在画面下沿（`bottom` 约画面高
  3%~5%），`fitWidth` 取画面宽的 0.58~0.66——参考片里那条中文字幕只占画面高约 4%，
  **别用 0.84~0.88 的满宽**，那会把字放大成横幅。字压在图上，所以画面下沿约 12% 要
  留出相对干净的一带（用渐隐/暗部/地面兜住），别把主体或高对比花纹塞在那里；字色取
  `film.ink` 或这一镜的亮色，必要时用 `caption_style: outlined` 的黑描边读出来。
  **画/字分带是可选项，不是缺省**：只有这一镜确实需要一条独立字幕带（图解、表格、
  竖屏信息页）时才用 `<CaptionFrame>{(art) => …}</CaptionFrame>`——它**自己就会画这条
  字幕**，不要再在它外面/里面另摆一份 `<CaptionText/>` 或 `<CaptionLayer/>`，否则同屏
  两条字会叠成重影（真机上出现过）。用分带时按 `art.w × art.h` 排画面、底色写在
  `<CaptionFrame style={{background}}>` 上。
  字要当图注、贴着图表或物体摆时，仍用 `<CaptionText/>` 套定位盒子自己摆，但要保证
  字盒与主体不撞。字进画里（招牌、卡片、跟着 SVG 坐标走）也是这一件：放进
  `<foreignObject>` 或直接定位在画里，**别手写 `<text>` 当字幕**——用户选的那一档样式
  只在这一件里落地。整屏都是画面、确实找不到能排字的干净区域时，才用整条
  `<CaptionLayer/>`（同样只能有一条）。
· 只回一段 ```tsx 代码块，第一行是 import，最后一行是 `export default Shot;`。
  不要解释、不要复述要求、不要贴多个版本。
"""


#: 整片美术方向的字段 → 写给镜头画师看的那句话。**只有写了才出现**：没写 `art`
#: 的片子，这一整段是空串，`_film_block` 的字节与旧版一致（前缀缓存不受影响）。
_ART_LABELS: tuple[tuple[str, str], ...] = (
    ("look", "画风"),
    ("brush", "笔触"),
    ("light", "光源"),
    ("texture", "画面质感"),
    ("type", "字体气质"),
    ("motion", "动效气质"),
    ("palette_note", "配色提示"),
)


def _prop_block(spec: Mapping[str, Any]) -> str:
    """整片贯穿装置（`spec["recurring_prop"]`）。没写就返回空串——老片子逐字不变。

    为什么值得单列一段：一帧的高级感常常不在主体画得多细，而在**同一件东西反复出现、
    状态随叙述变化**（参考片里那枚「本能读数」仪表）。它不是 UI 贴片，是画里世界的一件
    道具；写手需要知道它长什么样、该摆在哪儿，而不是每镜临时发明一个。
    """
    prop = str(spec.get("recurring_prop") or "").strip()
    if not prop:
        return ""
    return ("整片贯穿装置（只有这一件，反复用）：" + prop
            + "。它属于画面里的世界（墙上的仪表、玻璃反光、器械面板），不是 UI 贴片；"
              "适合这一镜时放在同一个位置，让它的读数/状态随叙述变化。"
              "不要每一镜都硬塞，更不要每镜换一个新的。\n")


#: `protagonist.render` 的值 → 写给镜头画师看的一句话。
_PROTAGONIST_RENDERS: dict[str, str] = {
    "silhouette": "剪影（单色实心，靠轮廓与勾边光认人；缺省）",
    "flat": "平涂上色（主体有固有色，明暗只分两档）",
    "lit": "受光/背光两档以上（画出体积与材质，接近「真实的样子」）",
    "line": "线稿（只画轮廓与结构线）",
}


def _protagonist_block(spec: Mapping[str, Any]) -> str:
    """整片贯穿主体（`spec["protagonist"]`）。没写就返回空串——老片子逐字不变。

    **不按物种/物件分类**：人、猫、鸟、鱼、车、器物、一块发光的矩形都由写手按
    这一镜的 brief 画。系统只给连续性事实（比例、基准线、朝向、运动节奏），不给
    「你能画哪些类别」的清单——走路的猫、飞行的鸟、游泳的鱼，这张表永远写不完。

    为什么单列一段而不是塞进 brief：二十几镜各自写码时，「同一个东西」靠每镜的
    brief 是守不住的（每个人都按自己的理解画一个「大概这么高」的主体）。这一段
    给的是**同一组数**（身高/基准线/朝向/节奏），写手照这组数每镜自己画。
    **怎么画不在这里规定**：2026-10-20 用户否掉了「把人物锁进一套共享骨架」，
    理由很直接——背景一直是模型自己画的，人也一样画得出来。
    """
    p = spec.get("protagonist")
    if not isinstance(p, Mapping):
        return ""
    look = str(p.get("look") or "").strip()
    if not look:
        return ""
    try:
        height = float(p.get("height", 0.46))
    except (TypeError, ValueError):
        height = 0.46
    try:
        foot = float(p.get("foot_y", 0.88))
    except (TypeError, ValueError):
        foot = 0.88
    try:
        stride = float(p.get("stride_sec", 1.0))
    except (TypeError, ValueError):
        stride = 1.0
    facing = str(p.get("facing") or "right").strip() or "right"
    # `kind` 是自由文本，缺省空；**不把空值猜成「人」**——那会让走路的猫、飞行的鸟
    # 都被塞进人的骨架。
    kind = str(p.get("kind") or "").strip()
    render = str(p.get("render") or "silhouette").strip()
    render_text = _PROTAGONIST_RENDERS.get(render, _PROTAGONIST_RENDERS["silhouette"])
    wardrobe = str(p.get("wardrobe") or "").strip()
    light = str(p.get("light") or "").strip()
    return (
        "整片贯穿主体（**同一个东西**穿行各场景，每一镜都要在；"
        "外形按这一镜的 brief 变，比例/朝向/运动节奏不变）：" + look + "\n"
        + (f"主体类型（自由文本）：{kind}。\n" if kind else "")
        + f"画法方向：{render_text}。\n"
        + (f"外观/材质随时代换：{wardrobe}。这一镜按自己的时代画；"
           "**身份不变，外形按 brief 变**——轮廓与服饰/羽毛/鳞片/配件/持物都属于内容。"
           "衣服、发型、头饰、持物逐镜按时代换；不要每镜另画一个不同的主体。\n"
           if wardrobe else "")
        + f"连续性（每一镜都守）：主体高约 {height:.2f}×画面高、"
          f"底缘/活动基准线 {foot:.2f}×画面高、朝 {facing}。相机可以横移、背景可以"
          "换时代、外形可以按时代换，但主体在画面里的大小、基准线、朝向与运动节奏不变。\n"
        # 朝向与位移方向必须说死：真机 2026-10-19 的三镜里，人物 `facing="right"`，
        # 但 `figX` 从 0.80 插值到 0.32——面朝右、人往左移，成片里是倒着走。
        f"**朝向就是行进方向**：`facing=\"{facing}\"` 就必须朝画面{'右' if facing == 'right' else '左'}"
        f"动，x 随时间{'增大' if facing == 'right' else '减小'}。别让它面向一边、位置却往另一边移。\n"
        f"运动节奏：约 {stride:.3g} 秒一个周期/一拍；有周期运动的主体照它，"
        "没有周期的主体按 brief 的节奏，不必套这个数。\n"
        # 「一直在动」是参考片的核心语法，必须写成缺省而不是可选项。真机里三镜都
        # 写了 `walking = t < 0.62/0.66/0.58`：每镜走一段就站住，三镜拼起来是
        # 「走 → 站 → 切 → 又走 → 站」，不是「同一个东西一直在动」。
        + ("**默认整镜都在动**：除非这一镜的文案或 brief 明确写「停下 / 站住 / 驻足 / "
           "悬停」，就让主体一直动，不要走两步就站住——那会把「同一个东西穿过时代」断掉。\n"
           "确实要停一拍时，就让动作停在那一拍——**停的那一镜也别把它画小、画丢**。\n"
           # 2026-10-20 真机片：简报里写「站着看戏」的那几镜，写手把主体停住甚至画成
           # 另一个姿态；用户要的是「一直在走」。简报决定它面对什么，不决定它还走不走。
           "**走不停**：用户要的是同一副身体从第一镜走到最后一镜。brief 写「站在 / 坐着 / "
           "看着」时，让它**边走边看、走到那里停半步再走**，不要整镜站死，也不要在这一镜"
           "换一副身体。只有用户原话明确说「停下来」才停。\n"
           # 同一副填色：真机片里主体有的镜是深色实心、有的镜只剩一条浅色轮廓。
           "**画法整片一致**：剪影就整片都是剪影、同一个填色；能变的只有服饰/持物/边光"
           "颜色这些内容。不要一镜是实心剪影、下一镜变成浅色实心或只剩轮廓线。\n")
        + (f"跟着主体走的那束光：{light}。用这一镜的光色（火把橙/车窗暖黄/"
           "路灯/手机屏冷蓝），按时代换，不要所有时代都用整片 accent。"
           "剪影色用 `fill`，缺省跟随整片墨色。\n" if light else
           "勾边光用 `rim`，按这一镜的时代给颜色（火把橙/车窗暖黄/路灯/手机屏冷蓝），"
           "不要所有时代都用整片 accent。主体固有色用 `fill`，缺省跟随整片墨色。\n")
    )


def _art_block(spec: Mapping[str, Any]) -> str:
    """整片美术方向（2026-10-18）。同一段逐字进每一镜的系统提示，命中前缀缓存。"""
    art = spec.get("art")
    if not isinstance(art, Mapping):
        return ""
    parts = [f"{label}：{str(art.get(key) or '').strip()}"
             for key, label in _ART_LABELS if str(art.get(key) or "").strip()]
    if not parts:
        return ""
    return ("整片美术方向（每一镜都照着这一套画，别自己另起）："
            + "；".join(parts) + "。\n")



_style_sections = style_sections


def _caps_index_block(spec: Mapping[str, Any]) -> str:
    """风格包正文的能力索引（只在 manifest 标了 ``progressive`` 时出现）。"""
    pack = spec.get("skill_contract")
    if not isinstance(pack, Mapping):
        return ""
    index = _style_sections.index(str(pack.get("body") or ""))
    if not index:
        return ""
    lines = ["=== 这一套风格能用的能力（索引；用到哪几件，正文会随该镜一起给你）==="]
    for name, first, size in index:
        lines.append(f"- `{name}`：{first or '（这一件没写摘要）'}")
    lines.append("这几件是**做法**，不是内容清单：用不用由这一镜的 brief 定；"
                 "brief 没点名时，照风格不变量自己画，不要为了用某件能力去改内容。")
    return "\n".join(lines) + "\n"


def _caps_block(shot: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    """这一镜点名要的能力正文（第二层）。brief/分镜没点名就返回空串。

    为什么挂在**这一镜的用户消息**而不是系统提示：系统提示是全片共用的前缀
    （命中前缀缓存），逐镜不同的能力段放进去会把它打成每镜一份。放这里还有一层
    语义上的好处——能力只在**点名它的那一镜**里给出，模型不必自己去判断该忘掉哪一段。
    """
    pack = spec.get("skill_contract")
    if not isinstance(pack, Mapping):
        return ""
    body = str(pack.get("body") or "")
    caps = [str(c).strip() for c in (shot.get("caps") or []) if str(c).strip()]
    if not caps or not body:
        return ""
    got: list[str] = []
    missing: list[str] = []
    for cap in caps[:6]:
        text = _style_sections.section_text(body, cap)
        (got if text else missing).append(f"### {cap}\n{text}" if text else cap)
    if not got and not missing:
        return ""
    out = ["=== 这一镜加载的能力（正文只给这一镜点名的几件）===",
           "这几件是这一镜真的要用的画法：代码里要 import 对应模块并让组件出现在画面上，"
           "不是只在注释里提一句。"]
    out.extend(got)
    if missing:
        # 点了一个不存在的名字不报错、也不拦：风格包是数据，能力清单会随用户编辑变；
        # 报一句就够，写手照样按风格不变量画。
        out.append("（这一镜点名但这套风格里没有的能力：" + "、".join(missing) + "——按不变量自己画。）")
    return "\n".join(out) + "\n"


def _skill_contract_block(spec: Mapping[str, Any]) -> str:
    """用户导入的 skill 作为这一片的画面契约（`spec["skill_contract"]`）。

    2026-10-24：这里原来叫 `_skill_contract_block`，同时服务「风格包」与「导入的 skill」。
    风格包通道整条删除之后，只剩导入的 skill 走这条路——所以**只渲染它的正文**，
    不再渲染 style manifest（配色/构图/模板那些键已经不产出了）。

    正文是**写给别的环境**的说明书（HTML/Canvas、命令行、生图、剪映……）。这里只取
    它关于画面的部分：能落成代码的只有白名单里的件——这条边界对所有导入技能一样，
    不是给某一份技能写的。
    """
    pack = spec.get("skill_contract")
    if not isinstance(pack, Mapping):
        return ""
    name = str(pack.get("name") or "").strip()
    if not name:
        return ""
    display = str(pack.get("display") or name).strip()
    body = str(pack.get("body") or "").strip()
    lines = [
        f"=== 导入的技能（这一片的画面契约）：{display}（{name}）===",
        "本片视觉由这一节定；技能正文里的默认建议让位给这里——"
        "尤其是构图、字幕位置、人物姿势、贯穿道具与配色。",
        "这份技能是**从外部导入的**，只取它关于画面与动画的部分。它提到的外部工具、"
        "依赖安装、命令行步骤、AI 生图、素材下载、成片交付方式**一律不执行**：画面"
        "只能由白名单里的代码逐帧画出来。它点名的技术在内部组件库里有对应件就照用"
        "（技能正文里列了组件与 import）；库里没有的，用最接近的画法做出同样的"
        "视觉效果，整片保持一致，不要为了它去装东西或联网。"
        # 2026-10-24 真机：模型把技能正文里的外部参数当成本片参数用了——
        # 拿 MiniMax 的音色名 `male-qn-jingying` 去填 voice，被 schema 打回。
        "**它提到的参数名与取值（音色名、模型名、命令行开关、API 字段）都不适用于本片**："
        "本片能填什么值只由工具 schema 决定，拿不准就不传、走缺省。",
    ]
    if body:
        lines += ["", body]
    return "\n".join(lines) + "\n"


def _readable_block(spec: Mapping[str, Any]) -> str:
    """这一片挂载的 skill 里，写手**可以点名读**的文件清单。

    没有它，写手就算有 `read_skill_file` 工具也不知道该读什么。只列挂上那几份、
    各前若干条——全库 257 条每镜都列一遍会把 brief 挤掉。
    """
    from . import recipe_index as ri

    contract = spec.get("skill_contract")
    parts = ([str(x) for x in (contract.get("parts") or []) if str(x).strip()]
             if isinstance(contract, Mapping) else [])

    lines = ri.readable_lines(parts)
    if lines:
        return ("\n=== 可用参考（动笔前可以点名读原文）===\n"
                "下面这些是本片挂载技能里的文件。**不确定该怎么画时，先调 "
                "`read_skill_file(path=…)` 读它**，读完再写代码；路径要逐字照抄。\n"
                + lines + "\n")

    # 没挂 skill 时也要让它知道**库里有这些技能**（Agent Skills 的 Level 1）：
    # 否则「用户没点名」就等于「模型永远不知道有这些东西」，自动发现无从谈起。
    index = ri.skill_lines()
    if not index:
        return ""
    return ("\n=== 库里可用的技能（本片没指定，你可以自己决定要不要用）===\n"
            "要参考其中某一份时：先 `list_skill_files(skill=名字)` 看它有哪些文件，"
            "再 `read_skill_file(path=…)` 读具体那一篇。**不用就不用，不要为了用而用。**\n"
            + index + "\n")


def _shell_block(spec: Mapping[str, Any]) -> str:
    """片壳段（2026-10-20）。没选壳时返回空串——老片子的提示词逐字不变。

    为什么写成「结构约定」而不是又一段风格建议：外框是 entry 层套在每一镜外面的，
    镜头代码本来就没有画它的机会（画在画面带里会被裁，画在带外不存在）。这里说清楚
    边界，是为了让模型把力气花在主体上，而不是花在重复画纸底和钟上。
    """
    shell = str(spec.get("shell") or "").strip()
    if not shell:
        return ""
    label = str(spec.get("shell_label") or "").strip()
    return (
        "=== 片壳（整片统一外框，已经是画面事实）===\n"
        f"这一片选了 `{shell}`：暖纸底、顶部彩色细条、左上角固定圆钟、"
        + (f"右上角小字「{label}」、" if label else "右上角期号小字（没给就不出现）、")
        + "底部字幕，全部由片壳统一画、全片位置不变。\n"
        "**你的代码只画画面带里的东西**：这一镜旁白对应的主体物件与必要场景。\n"
        "· 不要 import 或使用 `FilmLayout`；\n"
        "· 不要写 `CaptionLayer` / `CaptionFrame`（渲染入口会直接拒绝这一镜）；\n"
        "· 不要自己画底色、顶部色条、圆钟、期号或底部字幕；\n"
        "· 画布用 `useBody()` 报的 `w`×`h`，原点就是带子左上角（`x`/`y` 是带子在整帧"
        "里的位置，别再加进坐标）；越界会被裁掉。\n"
        "画面带之外的一切都不归这一镜：外框画在母版之后，镜头代码也盖不住。\n"
    )


def _component_block() -> str:
    """内部组件库的**签名**目录（从 `remotion/src/lib` 现读，见 `components.py`）。

    放在技能正文之前：正文里那些手写示例是「怎么用」，这份是「能传什么」。
    源码读不到时整段不发——少一段参考不该让写镜失败。

    """
    catalog = components.component_catalog()
    if not catalog:
        return ""
    return catalog + "\n\n"


def _motion_samples_block() -> str:
    """十种动效手法的**可抄骨架**（2026-10-23）。

    为什么要有它：`demos/` 下躺着 **165 个动效件**、`shots/` 下躺着 **138 篇做法文档**，
    但 SKILL.md 第 11 行是 `exclude: demos/, shots/` —— 全被排除在技能附件之外
    （2026-10-18 的决定，理由正当：一条 run 光 demo 就读了 53 份、还编出两个不存在的
    名字卡死，照抄 demo 又把 PPT 表格/霓虹字一起抄进来）。

    结果是：**技能正文只有「原则」（要成组、用 spring、要隔开），没有「手法」。**
    写手知道「该有动效」，但手上没有一条能抄的写法，于是写出「所有元素第 0 帧就已
    到位」的静帧——闸⑭ 在《一秒》实测到 s01/s09 **0 个变化段**。

    所以这一块给的是**手法**，不是**文件**：十段骨架，每段 6~10 行，全部取自
    `demos/` 里的真实实现（描线那段的 `pathLength={1}` + `strokeDashoffset` 逐字来自
    `ui-entrance/draw-svg-trace/DrawSvgTrace.tsx:221`），缓动统一用项目惯用的
    `E.standard` / `E.spring200`。

    **不给完整文件**：那正是 2026-10-18 踩过的坑（读 53 份、抄外观）。给片段，写手
    自己组合。
    """
    return (
        "\n=== 动效手法：十段可抄的骨架 ===\n"
        "**动效是这一镜的骨头，不是装饰。** 全部由 `useCurrentFrame()` + `interpolate()` 驱动，\n"
        "**别用 CSS `transition`/`animation`**（逐帧 seek 下算不对）。缓动统一用 `@dg/motion` 的\n"
        "`E.standard`（0.16,1,0.3,1）与 `E.spring200`，别自选曲线。\n\n"
        "① **错峰入场**（同一层元素依次出现，最常用）\n"
        "```tsx\n"
        "{items.map((it, i) => {\n"
        "  const p = interpolate(frame, [6 + i * 3, 18 + i * 3], [0, 1],\n"
        "    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: E.standard });\n"
        "  return <div key={i} style={{ opacity: p, transform: `translateY(${(1 - p) * 18}px)` }}>…</div>;\n"
        "})}\n"
        "```\n"
        "每项隔 2~4 帧；间距太小读作「一起出现」，太大读作「卡顿」。\n\n"
        "② **数字滚动**（读数 / 计数 / 百分比）\n"
        "```tsx\n"
        "const v = interpolate(frame, [0, 24], [0, target],\n"
        "  { extrapolateRight: 'clamp', easing: E.standard });\n"
        "<span style={{ fontVariantNumeric: 'tabular-nums' }}>{Math.round(v).toLocaleString()}</span>\n"
        "```\n"
        "`tabular-nums` 必加，否则数字宽度变化会让整行左右抖。\n\n"
        "③ **描线生长**（轮廓 / 下划线 / 曲线「被画出来」）\n"
        "```tsx\n"
        "const p = interpolate(frame, [0, 40], [0, 1],\n"
        "  { extrapolateRight: 'clamp', easing: E.standard });\n"
        "<path d={D} fill=\"none\" stroke=\"#ffd7a8\" strokeWidth={6} strokeLinecap=\"round\"\n"
        "      pathLength={1} strokeDasharray=\"1\" strokeDashoffset={1 - p} />\n"
        "```\n"
        "`pathLength={1}` 让你不用 `getTotalLength()`——描任何形状都是这一套。\n\n"
        "④ **擦入**（块面从一侧推出 / 收进）\n"
        "```tsx\n"
        "const w = interpolate(frame, [0, 20], [0, 100],\n"
        "  { extrapolateRight: 'clamp', easing: E.standard });\n"
        "<div style={{ clipPath: `inset(0 ${100 - w}% 0 0)` }}>…</div>\n"
        "```\n"
        "改 `inset(0 0 ${100 - w}% 0)` 就是自上而下落。\n\n"
        "⑤ **弹出回弹**（一个东西「落定」）\n"
        "```tsx\n"
        "const s = spring({ frame, fps, config: { damping: 200, mass: 0.6 } });\n"
        "<div style={{ transform: `scale(${0.92 + s * 0.08})`, opacity: s }}>…</div>\n"
        "```\n"
        "`damping: 200` 是项目标准（`E.spring200`）；调成 12 那种大回弹会像果冻。\n\n"
        "⑥ **圆形揭示**（遮罩从某点扩散）\n"
        "```tsx\n"
        "const r = interpolate(frame, [0, 26], [0, 140],\n"
        "  { extrapolateRight: 'clamp', easing: E.standard });\n"
        "<div style={{ clipPath: `circle(${r}% at 50% 46%)` }}>…</div>\n"
        "```\n\n"
        "⑦ **字符级入场**（一句短文案逐字浮现）\n"
        "```tsx\n"
        "{chars.map((c, i) => {\n"
        "  const p = interpolate(frame, [i * 1.6, i * 1.6 + 10], [0, 1],\n"
        "    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: E.standard });\n"
        "  return <span key={i} style={{ opacity: p, filter: `blur(${(1 - p) * 6}px)` }}>{c}</span>;\n"
        "})}\n"
        "```\n\n"
        "⑧ **视差**（多层不同速度位移，造纵深）\n"
        "```tsx\n"
        "const far = interpolate(frame, [0, dur], [0, -12], { extrapolateRight: 'clamp' });\n"
        "const near = interpolate(frame, [0, dur], [0, -34], { extrapolateRight: 'clamp' });\n"
        "```\n"
        "近层走得快、远层走得慢，两层的比值就是纵深。\n\n"
        "⑨ **呼吸 / 脉动**（光斑、指示点、警示）\n"
        "```tsx\n"
        "const b = 0.5 + 0.5 * Math.sin((frame / fps) * Math.PI * 2 * 0.8);   // 0.8 Hz\n"
        "<div style={{ opacity: 0.35 + b * 0.5, transform: `scale(${1 + b * 0.04})` }} />\n"
        "```\n"
        "**先 `frame / fps` 换成秒再乘频率**；直接拿 frame 当相位，换个 fps 就变味。\n\n"
        "⑩ **冲击抖动**（撞击 / 切点的那一下）\n"
        "```tsx\n"
        "const hit = interpolate(frame, [0, 3, 10], [0, 1, 0], { extrapolateRight: 'clamp' });\n"
        "<div style={{ transform: `translate(${Math.sin(frame * 2.4) * 6 * hit}px, 0)` }}>…</div>\n"
        "```\n\n"
        "**两条纪律比手法本身更重要**：\n"
        "· **两处变化要隔开 ≥0.4s**：一整段匀速动效只数得出 1 段，闸⑭ 要 ≥2 段。\n"
        "  把动效排成「先入场 → 停一下 → 再有一次变化」，别从头动到尾。\n"
        "· **变化要够大**：闸⑭ 量的是**整帧**平均差，只动一小块时读数极低。要么让动的\n"
        "  东西占画面足够大，要么让亮度/色温也一起变（背景明暗呼吸、整幅色温推移）。\n"
    )


def _channel_samples_block() -> str:
    """另外四条画法通道的**可抄骨架**（2026-10-23）。

    为什么要有它：`_component_block` 列的 23 件全是 2D/CSS（brush / scene / walk /
    Caption / ClipCard / DigitRoll / FlashCut / PageCam / VerticalTicker / FlatPanel /
    draw×13），**一件 3D 都没有**。写手看得见的范本只有手写 SVG，于是它当然写 SVG。

    2026-10-23《一秒》实跑账：12 镜里 8 镜申报了 `subject_channel=three/katex/rough/
    canvas`，而代码里这四个词一次都没出现，全片 `style_channels` 只剩 `["vector"]`。
    评审对 s07 心脏的原话是「扁平卡通剪影，只有一圈暗描边和几块模糊色斑，没有厚度、
    粗糙度、接触阴影」——**那不是模型不会画 3D，是它没有范本、顺手用了最熟的一条路。**

    所以这一块给的是**能直接抄的最小骨架**，不是组件签名。三段取自本仓
    `remotion/src/user-shots/fixture-{three,katex,rough}.tsx`（P4 夹具，真机渲过）；
    three 那一帧 2026-10-23 又验了一次：`render_ms: 1258`，真透视真光照。

    与闸⑯ 配套：⑯ 同日从 `SOFT_GATES` 移出改回硬闸，**只硬拦不给范本会让那几镜
    反复退到超时**，两处必须一起上。
    """
    return (
        "\n=== 另外四条画法通道：能抄的骨架 ===\n"
        "**这是参考，不是任务清单**：按这一镜的画面需要选一条，不需要就用手写 SVG。\n"
        "**申报纪律**：`subject_channel` 填哪一条，代码里就必须真 import 并用上那一条的\n"
        "API。填了 three 而代码里只有 `<svg>`，闸⑯ 会**打回重写**（2026-10-23 起是硬闸，\n"
        "不再是「建议」）。反过来，这一镜真用 three 画主体就填 three，别填 vector。\n"
        "下面 `cw`/`ch` 一律取 `const body = useBody(aspect); const cw = Math.round(body.w)`。\n\n"
        # ---- three ----
        "① **three**：真 3D —— 有厚度、有光照、有材质、有透视遮挡。单帧约 1.2s，\n"
        "   只在「这东西必须是立体的」时用（心脏、地球、机构、容器、雕塑）。\n"
        "```tsx\n"
        "import { ThreeCanvas } from '@remotion/three';\n\n"
        "<ThreeCanvas width={cw} height={ch} camera={{ position: [0, 1.6, 5], fov: 45 }}\n"
        "  style={{ background: 'transparent' }}>\n"
        "  <ambientLight intensity={0.5} />\n"
        "  <directionalLight position={[4, 6, 5]} intensity={2.2} color=\"#ffffff\" />\n"
        "  <mesh rotation={[rx, ry, 0]}>\n"
        "    <coneGeometry args={[1.5, 2.8, 4]} />\n"
        "    <meshStandardMaterial color=\"#c8a24a\" roughness={0.45} metalness={0.1} />\n"
        "  </mesh>\n"
        "</ThreeCanvas>\n"
        "```\n"
        "几何件就用 three 的名字：`coneGeometry` `boxGeometry` `sphereGeometry`\n"
        "`cylinderGeometry` `torusGeometry`。材质**必须**用 `meshStandardMaterial`\n"
        "（带粗糙度/金属度，会被光影响）；用 `meshBasicMaterial` 等于没开灯，画出来\n"
        "还是纯色块，白用 3D。\n"
        "**只放主体，绝对不要放地面。** 不许写 `planeGeometry` 的「阴影承接平面 / 地面 /\n"
        "背景板」——`<ThreeCanvas>` 是**叠在 2D 画面上的一块透明画布**，那块平面在 3D 空间里\n"
        "不透明、又没有东西替它混色，渲出来就是画面正中一块边缘笔直的深色方块。\n"
        "2026-10-24 实测：12 镜里 7 镜这么写，s05 那块方块把超新星核心遮掉一半，\n"
        "同一份代码把那一块删掉就干净了（A/B 真渲比对过）。\n"
        "接触阴影改用 **2D**：在 `<ThreeCanvas>` **外面**叠一个椭圆 `<ellipse>` + 径向渐变，\n"
        "对到主体脚下；深底上本来就看不出影，也可以干脆不给。\n"
        "**想要 3D 主体带真光晕**（亮核外面一圈柔光）：`@remotion/effects/glow` **管不到**\n"
        "`<ThreeCanvas>` 里的东西——它只认 `<Img>/<Solid>/<HtmlInCanvas>`。three 场景要走后处理：\n"
        "```tsx\n"
        "import { EffectComposer, Bloom } from '@react-three/postprocessing';\n\n"
        "<ThreeCanvas …>\n"
        "  {/* 主体 */}\n"
        "  <EffectComposer>\n"
        "    <Bloom intensity={0.9} luminanceThreshold={0.6} luminanceSmoothing={0.25}\n"
        "      mipmapBlur radius={0.45} />\n"
        "  </EffectComposer>\n"
        "</ThreeCanvas>\n"
        "```\n"
        "参数照这组写（1920×1080 / 软 GL 真渲量的）：**intensity 0.8~1.0、threshold 0.55~0.65、\n"
        "radius 0.35~0.5**。`intensity` 到 1.6 会把整幅糊成一团（实测整幅平均 49→70，\n"
        "亮核边缘直接没了）——别照官方示例的默认值抄。每帧多 0.19 秒，比 HtmlInCanvas 便宜，\n"
        "但整片仍控制在 1/3 以内，**只给「发光体是主角」的镜挂**。\n\n"
        # ---- katex ----
        "② **katex**：公式字形正确（不是手搓的上下标字符）。\n"
        "```tsx\n"
        "import katex from 'katex';\n"
        "import 'katex/dist/katex.min.css';\n\n"
        "const html = katex.renderToString(\n"
        "  '\\\\Delta\\\\nu_{Cs} = 9\\\\,192\\\\,631\\\\,770\\\\ \\\\text{Hz}',\n"
        "  { displayMode: true, throwOnError: false });\n"
        "<div style={{ color: '#f2efe6' }} dangerouslySetInnerHTML={{ __html: html }} />\n"
        "```\n"
        "`displayMode: true` 是独立公式（居中大号），`false` 是行内。**必须 import 那个\n"
        "css**，否则字形全崩成豆腐块。`throwOnError: false` 让写错的公式降级成红字而\n"
        "不是整镜渲不出来。\n\n"
        # ---- rough ----
        "③ **rough**：手绘线 —— 线头有抖动、填充是斜纹，像铅笔而不是尺子。\n"
        "```tsx\n"
        "import rough from 'roughjs';\n\n"
        "const rc = rough.canvas(canvas);   // canvas 来自 useRef<HTMLCanvasElement>\n"
        "rc.circle(cx, cy, r, { roughness: 1.6, stroke: '#2b2b2b', strokeWidth: 1.4,\n"
        "                       fill: '#8a6a3a', fillStyle: 'hachure', hachureGap: 6 });\n"
        "```\n"
        "画在 canvas 上（`rough.canvas`），或 `rough.svg(svgEl)` 画成 SVG。手绘感主要\n"
        "来自 `fillStyle: 'hachure'` 的斜纹填充，**不是**来自把线抖一点。\n\n"
        # ---- canvas ----
        "④ **canvas**：自己逐帧画 —— 适合点阵、粒子、几百个小元素的场合。\n"
        "```tsx\n"
        "const ref = useRef<HTMLCanvasElement>(null);\n"
        "useEffect(() => {\n"
        "  const cv = ref.current; const ctx = cv?.getContext('2d');\n"
        "  if (!cv || !ctx) return;\n"
        "  ctx.clearRect(0, 0, cv.width, cv.height);\n"
        "  // 全部从 frame 算出来；不许 Math.random / Date.now\n"
        "}, [frame]);\n"
        "```\n"
        "它最划算的场合是「一条 canvas 里几百个点」，但**它不会自己动**，每帧都要重画。\n\n"
        # ---- css 3d ----
        "⑤ **CSS perspective**（伪 3D，一帧成本几乎为零）：卡片在空间里飞、版面整体倾斜。\n"
        "```tsx\n"
        "<div style={{ perspective: 2600, perspectiveOrigin: '50% 50%' }}>\n"
        "  <div style={{ transform: `translate3d(${x}px, ${y}px, ${z}px) rotateY(${ry}deg)`,\n"
        "                transformStyle: 'preserve-3d' }}>…卡片内容…</div>\n"
        "</div>\n"
        "```\n"
        "要真实光照、真实遮挡、真实材质，用 ①；只是要「有空间感」，用它更便宜。\n\n"
    )


def _film_block(spec: Mapping[str, Any]) -> str:
    """整片设定：同一部片子的每一镜**逐字相同**（前缀缓存就是靠这个命中的）。"""
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    aspect = str(spec.get("aspect") or "landscape")
    zone = cspec.CAPTION_ZONE_PX.get(aspect, 300)
    style = str(spec.get("style") or "").strip() or "（用户没指定，按选题自己定一种克制的版式）"
    grammar = str(spec.get("style_grammar") or "").strip()
    palette = str(spec.get("palette") or cspec.PALETTE_NONE)
    texture = str(spec.get("texture") or "none")
    cap = str(spec.get("caption_style") or "outlined")
    hl = str(spec.get("highlight_style") or "plain")
    hl_color = str(spec.get("highlight_color") or "").strip() or "跟色板的 accent"
    # **全片画面表（批 3，2026-10-23）**：原来只有每镜 60 字文案。文案说的是
    # 「讲什么」，不是「画什么」——写手一次只看一镜，这一栏是它唯一能知道
    # 「别人画了什么」的地方。加一栏 brief，12 镜多约 1.2 KB，换掉的是
    # 「不知道上一镜是俯视还是平视、暖还是冷」这个结构性问题。
    listing = "\n".join(_frame_row(i, s) for i, s in enumerate(shots))
    repeat = False
    bands = True
    continuity = ("整片所有镜保持同一种语言，但相邻镜别用同一种版式。"
                  if not repeat else
                  "本片**刻意重复同一构图**：主体位置、贯穿光块位置、地平线、字幕位置"
                  "每一镜都一样；只换时代背景、服装、道具内容与光色。"
                  "不要为了「变化」而换机位、换主体位置或换字幕位置。")
    # 片壳模式下字幕归壳：不能再说「字幕由这一镜的代码排」——两句话同时出现，模型
    # 一定会照更像「技能正文」的那一句办，于是同屏两条字幕。这一段是唯一的出处。
    shell = str(spec.get("shell") or "").strip()
    if shell:
        caption_block = (
            f"字幕：{cap} × {hl}，高亮色 {hl_color}。**字幕由片壳统一排在底部**，"
            "全片同一位置、同一字号、同一描边；镜头代码**不画字幕**，"
            "也不要把主体或高对比花纹压进最底下那一条。\n"
            f"字幕安全区：画面最底下 {zone}px 留给片壳那条字幕，"
            "主体与场景到那里为止。\n"
            "画面里的招牌、门牌、图表标注这类**画中的字**可以用 `<CaptionText/>` 或 "
            "`<text>`——它们不是字幕，但同样要自己保证不与主体相撞。\n"
        )
    elif cap == "by_shot":
        # 2026-10-23：字幕交给这一镜的代码自己设计。母版那条不画、`<CaptionLayer/>`
        # 不被自动补上、写手画在字幕带里的字也不被删（`code_spec._autofix_caption_layer`）。
        #
        # 为什么必须点名「别用 CaptionText/CaptionLayer 的缺省形态」：那两件的字号、
        # 描边、字色全部从 `CAPTION_LOOK` 取（0.44 系数 + 黑描边白字），所以哪怕写手
        # 换了位置，字还是「永远那一种」。要让字幕真的属于这一镜，就得让它自己写样式。
        caption_block = (
            f"字幕：**这一片把字幕交给了你**（caption_style=by_shot）。"
            f"高亮词用 {hl}，高亮色 {hl_color}。\n"
            "· 文案从 `getInputProps().text` 读（整句），`highlight` 是里面要被点出来的词。\n"
            "· **位置、字号、字重、字色、描边、入场，全归你定**——字幕是这一镜构图的一部分，"
            "不是贴在底部的一条固定装置。它可以贴着你画的主体摆、可以竖排、可以只露出半句、"
            "可以跟着画面一起动。\n"
            "· **别用 `<CaptionText/>` / `<CaptionLayer/>` 的缺省形态**：它们的字号与描边来自"
            "母版的 `CAPTION_LOOK`（0.44 系数 + 黑描边白字），换位置也换不掉那张脸——"
            "**那正是「所有片子的字幕长得一模一样」的原因**。要自己写样式就用裸 "
            "`<div>`/`<span>` 排字（自己写 fontFamily/fontSize/color/textShadow）；"
            "确实想复用那两件，就必须显式传 `sizeScale`/`outline`/`bottom`/`tone`，"
            "不要留空。\n"
            f"· 字幕**不要**只是换个位置塞在画面最底下 {zone}px 那一带——那是母版缺省的"
            "落点，不是唯一落点。让它跟画面咬合。\n"
            "· 但**可读性是底线**：字压在花纹上就自己加描边或暗角，别为了构图牺牲「一眼读得出来」。"
            "全片字幕要有统一的手感（同一字体、同一字号档、同一描边逻辑），"
            "位置可以逐镜不同——这叫设计，不叫随便。\n"
            "· 字盒先按行数估高：两行 ≈ 字号 × 行高 × 2；宁可写 `bottom: …` 兜住底边，"
            "不要从上面往下估一个会越界的 `top`。\n"
        )
    else:
        caption_block = (
            f"字幕：{cap} × {hl}，高亮色 {hl_color}。**字幕叠在画面上，由这一镜的代码排**。"
            "缺省写法：`<CaptionText/>` 套定位盒子放在画面下沿，`fitWidth` 取画面宽的 "
            "0.58~0.66，字号交给它自己求——参考片那条中文字幕只占画面高约 4%，"
            "**不要用 0.84~0.88 的满宽**（字会被放大成横幅）。画面下沿约 12% 要留出相对"
            "干净的一带（渐隐/暗部/地面兜住），别把主体或高对比花纹塞在那里。"
            "需要一条独立字幕带（图解、表格、竖屏信息页）时才用 "
            "`<CaptionFrame>{(art) => …}</CaptionFrame>`——它**自己已经画了这条字幕**，"
            "不要再另摆一份 `<CaptionText/>` 或 `<CaptionLayer/>`，否则同屏两条字会叠成"
            "重影；用分带时按 `art.w × art.h` 排画面，底色写在外框的 `style` 上。"
            "要与画面咬合（字当图注、贴着图表摆）时用 `<CaptionText/>` 套定位盒子，"
            "字进画里（招牌/卡片/跟着 SVG 坐标走）也用它（可放进 `<foreignObject>`），"
            "自己保证字盒与主体不撞；别手写 `<text>` 当字幕——用户选的样式只在这一件里落地；"
            "整屏都是画面、没有干净区域时才用整条 `<CaptionLayer/>`（同样只能有一条）。\n"
            f"字幕安全区：画面最底下 {zone}px 是字幕常落的一带；别让主体压进这一带，"
            "若摆 `<CaptionLayer/>`，那一条仍归它，别往里塞任何文字和主体。\n"
            "字盒先按行数估高：两行 ≈ 字号 × 行高 × 2；宁可写 `bottom: …` 兜住底边，"
            "不要从上面往下估一个会越界的 `top`。"
            "`<CaptionText/>` 会在渲染时把自己平移回画内，但挪进来的字往往压住主体——"
            "一开始就把位置算对。\n"
        )
    return (
        "=== 整片设定（二十镜共用，别每镜另起一套）===\n"
        f"画幅：{aspect} {spec.get('width')}x{spec.get('height')}，"
        f"{spec.get('fps')}fps。坐标单位就是设备像素。\n"
        f"整片风格：{style}\n"
        # 风格的**可执行拆解**（美术导演补的）。没写就一个字节都不多——老片子逐字不变。
        + (f"风格落地（照这几条具体做法执行，不是只换字体/贴一层网格）：{grammar}\n"
           if grammar else "")
        + _prop_block(spec)
        + _protagonist_block(spec)
        + _skill_contract_block(spec)
        + _shell_block(spec)
        +
        "这一句是起点，不是模板：在它之上把每一镜文案里的具体东西画出来，"
        "缺的媒介/构图/色彩由你补齐；" + continuity + "\n"
        f"整片色板：{palette}。页面底色、字色与高亮色从 `resolveFilmStyle(getInputProps())` 取，"
        "**不要把这四色写死**；只有这一镜确有理由（闪回/数据页/时代与时段变化）才绑 "
        "`palettes[\"…\"]` 或申报自己的 `bg` 覆盖。\n"
        "**光源色不归整片色板管**：火光、油灯、舞台灯、CRT、霓虹、手机屏各有自己的颜色，"
        "在这一镜代码里单独定（如 `const light = \"#ffb15c\"`），不要所有光都用 `film.accent`。"
        "风格里若写了色温弧线（从火光橙一路冷到屏幕蓝），它优先于整片色板的默认底色："
        "该换就逐镜换 `bg`，整片统一的是版式、笔触与光位，不是色温。\n"
        f"质感档：{texture}。渲染管线会在每一镜之后再统一加一层（"
        + ("这一档是 none，画面质感由你的代码负责：加纸纹/颗粒/暗角/柔光。"
           if texture in ("none", "", None) else
           "这一档已经带颗粒/暗角/调色；代码里就不要再叠一层同类的，避免压糊。")
        + "笔触的粗细变化、物件的柔影仍要在代码里画出来。\n"
        + _art_block(spec)
        + caption_block
        +
        "全片镜头（保持同一种视觉语言；"
        + ("相邻镜不要用同一种版式" if not repeat else "每镜同一构图，只换时代与道具")
        + "）：\n"
        f"{listing}\n\n"
        # 片壳这句放在技能正文之前**再提醒一次**：正文很长，模型常按最后读到的契约办，
        # 而「外框归片壳」是这一片最容易被正文里的旧写法盖掉的一条。
        + (f"片壳：{shell}——外框（纸底/色条/钟/期号）与底部字幕归片壳，"
           "你只画画面带里的主体。\n\n" if shell else "")
        + _component_block()
        # 另外四条通道的骨架放在组件目录之后：先看「有什么现成件」，再看「没有现成件
        # 时那几条路怎么写」。位置在技能正文之前——正文里全是 SVG 示例，放它后面会被
        # 盖掉（模型总按最后读到的契约办）。
        + _channel_samples_block()
        # 动效手法紧跟画法通道：先「用什么画」，再「怎么让它动」。这两块都排在技能正文
        # 之前——正文里全是原则与禁令，放它后面会被最后读到的契约盖掉。
        + _motion_samples_block()
        # 可读参考排在最后：它是「查资料」的入口，不是画法本身。
        + _readable_block(spec)
        + "=== 技能正文 ===\n"
        f"{_contract(bands, _extra_sections(spec))}\n"
    )


#: 用户原话进写手任务块的字符上限。真机上有用户会把整份分镜表贴进对话
#: （上万字），全量塞进每一镜会把 brief 挤到看不见；截断但保留开头——开头通常是
#: 「这条片子要什么」，正是最该被写手看到的那一段。
REQUEST_MAX_CHARS = 4000


#: 参考片静帧的进程内缓存：`{material_id}:{object_key} → [jpeg bytes]`。
#: 同一条片子的所有镜共享同一批参考帧；不缓存的话，每镜一次 ffmpeg 抽帧，
#: 二十镜就是二十倍的无谓读盘与解码。
_REFERENCE_CACHE: dict[str, list[bytes]] = {}
_REFERENCE_MAX = 6


def _reference_ids(spec: Mapping[str, Any]) -> list[str]:
    """这一片要看的参考素材：顶层 `reference_materials` 优先，其次风格包 manifest。

    素材 id 而不是路径：editor 与 app 是不同容器，只有对象存储里的字节是两边都
    摸得到的真相；路径在另一台机器上必然失效。
    """
    raw: Any = spec.get("reference_materials")
    if not raw:
        raw = None
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in (raw or [])
            if str(x).strip() and str(x).strip().startswith("mat-")][:4]


async def load_reference_images(storage: Any, spec: Mapping[str, Any],
                                user_id: str, *, per_video: int = 3,
                                total: int = _REFERENCE_MAX) -> list[bytes]:
    """把参考视频抽成静帧（JPEG 字节），给写手/评审当**看图**的输入。

    为什么必须给图：文字里的「剪影」只能让模型画一个大概的深色人形；参考片里
    真正要复现的是轮廓的转折、头身比、勾边光的方向、背景分层与字幕落点——这些
    是像素信息，不是词。用户要的是「LLM 自己画」，那至少得让它看见要画成什么样。

    只读本人（或系统共享）的素材；取不到就返回空，不拦写码——没有参考片的
    老片子逐字不变。
    """
    ids = _reference_ids(spec)
    if not ids or storage is None:
        return []
    from agent_framework import video_frames as sa

    out: list[bytes] = []
    for mid in ids:
        if len(out) >= total:
            break
        try:
            row = await storage.materials.get(mid)
        except Exception:  # noqa: BLE001 —— 素材服务抖动不该让写码失败
            continue
        if not row:
            continue
        owner = row.get("owner_user_id")
        if owner and user_id and owner != user_id:
            continue
        key = f"{mid}:{row.get('object_key') or ''}"
        cached = _REFERENCE_CACHE.get(key)
        if cached is not None:
            out.extend(cached[:max(0, total - len(out))])
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="ref-frames-") as tmp:
                path = await storage.objects.localize(
                    str(row.get("object_key") or ""), Path(tmp))
                meta = await asyncio.to_thread(sa.probe_video, path)
                times = sa.pick_times(float(meta["duration"]), per_video)
                frames = await asyncio.to_thread(
                    sa.extract_frames, path, Path(tmp) / "frames", times)
                got = [p.read_bytes() for p in frames]
        except Exception:  # noqa: BLE001 —— 一条坏片不该拖垮整片
            continue
        if not got:
            continue
        _REFERENCE_CACHE[key] = got
        out.extend(got[:max(0, total - len(out))])
    return out


def _reference_block() -> str:
    """参考帧随消息一起发时，说明「看什么、不要抄什么」。没有图就不发这一段。"""
    return (
        "=== 参考片静帧（目标观感，不是这一镜的内容）===\n"
        "消息里附了几张同一套视觉的参考片截图。看的是**画法**，不是内容：\n"
        "· 主体的轮廓与比例：头、颈、肩、躯干、四肢怎么连，边缘是什么形状；\n"
        "· 明暗关系：主体是最暗还是最亮，勾边光从哪一侧来、有多宽；\n"
        "· 背景分几层、层与层靠什么分开（明度、空气透视、遮挡），不是靠描边；\n"
        "· 构图：主体与光源各在画面什么位置、留白多少、字幕落在哪、字号多大。\n"
        "不要照抄图里的人物、场景、道具或文字——这一镜按自己的 brief 画，"
        "把上面这四件事做到。\n")


def _exemplar_block(code: str) -> str:
    """本片第一镜的代码：同一个模型自己的画法，交给后续镜照着画。

    这不是系统模板，也不是可 import 的组件——它只是「你刚才画的那个人长这样」。
    一致性靠模型复用自己写过的实现，而不是靠一份人手写的共享骨架。
    """
    src = str(code or "").strip()
    if not src:
        return ""
    if len(src) > 14000:
        src = src[:14000] + "\n// …（截断；照上面的画法写完整的一镜）"
    return (
        "=== 本片已经定下的画法（你自己写的第一镜）===\n"
        "下面是**你**为本片第一镜写的完整代码。后面的镜沿用这套画法：\n"
        "· 主体用同一副轮廓、同一个填色、同一个勾边光方向——不要重画一个不同的人；\n"
        "· 只换这一镜的时代背景、服装、道具、光色与动作；\n"
        "· 不要 import 它（它是那一镜的代码，不是组件）；照它的画法写这一镜自己的完整代码。\n"
        "```tsx\n" + src + "\n```\n")


def _request_block(user_request: str) -> str:
    """用户原话：整片每一镜都原样带一遍。

    为什么必须进写手的任务块（2026-10-20）：写手前面只有规划模型加工过的
    `brief`（≤400 字）。用户说的原话在规划那一轮之后就再没出现过——没被写进
    brief 的意思全部丢失，成片于是像「按流程能做的片子」，而不是用户要的片子。
    这一段放在这一镜任务块的最前面，让「用户要什么」比「技能默认怎么做」更近。
    """
    req = " ".join(str(user_request or "").split())
    if not req:
        return ""
    if len(req) > REQUEST_MAX_CHARS:
        req = req[:REQUEST_MAX_CHARS].rstrip() + "…（原话过长，已截断；开头这一段最要紧）"
    return (
        "=== 用户原话（这条片子的最高事实，逐字保留）===\n"
        f"{req}\n"
        "用户点名要的东西、点名不要的东西、说的是哪个年代/哪种语气，都以此为准。\n"
        "下面的分镜 brief 与技能正文是**执行细节**：与这段原话冲突时，按用户原话做；"
        "用户没提到的细节，才按 brief 与技能正文补。\n\n"
    )


#: 全片画面表里每一镜的截断长度。文案 40 字、画面 70 字：12 镜的整表约 2.4 KB，
#: 这是「写手知道自己排在第几镜、别人画了什么」的全部成本。
_FRAME_TEXT_CHARS = 40
_FRAME_BRIEF_CHARS = 70


def _frame_row(i: int, s: Mapping[str, Any]) -> str:
    """全片画面表的一行：``s01｜文案：…｜画面：…``。

    只给**已经有的**东西，不猜「与上一镜的关系」——那要靠模型自己从上下一行读出来，
    猜出来的关系写进提示词只会变成一条要它遵守的假约束。
    """
    sid = str(s.get("id") or f"s{i + 1:02d}")
    text = str(s.get("text") or "").strip().replace("\n", " ")[:_FRAME_TEXT_CHARS]
    brief = str(s.get("brief") or "").strip().replace("\n", " ")[:_FRAME_BRIEF_CHARS]
    row = f"{sid}｜文案：{text}"
    if brief:
        row += f"｜画面：{brief}"
    return row


def _neighbour_rows(spec: Mapping[str, Any], index: int) -> str:
    """上一镜 / 下一镜的画面（批 3）。空串＝首镜/末镜，或没有 spec。

    为什么要单列这一句而不只靠上面那张表：写手写一镜时真正要判断的是
    「接着画还是换机位」，那只需要**相邻两行**，而不是十二行里去找。
    """
    seq = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    if not seq or index < 0 or index >= len(seq):
        return ""
    out: list[str] = []
    if index > 0:
        out.append("上一镜（已经画过，除非 brief 要求重复构图，否则别用同一个机位与色调）："
                   + _frame_row(index - 1, seq[index - 1]))
    if index + 1 < len(seq):
        out.append("下一镜（这一镜要在视觉上接得住它）：" + _frame_row(index + 1, seq[index + 1]))
    return "\n".join(out) + ("\n" if out else "")


def _extract_md_block(text: str, heading: str) -> str:
    """取一个 markdown 块（`_drop_md_block` 的反向）：从 ``heading`` 起到下一个 H1/H2。

    与删除那一侧同一套「按行 + 前缀匹配」的理由：``## 4 图表与路径`` 与
    ``## 4.5 转场`` 只差一个字符，正则分不开。
    """
    out: list[str] = []
    keeping = False
    for line in text.splitlines():
        if not keeping:
            if line.startswith(heading):
                keeping = True
                out.append(line)
            continue
        if re.match(r"^#{1,2} ", line) and not line.startswith(heading):
            break
        out.append(line)
    return "\n".join(out).strip()


def _block_text(name: str) -> str:
    """按需块的正文。→ 空串＝没这一块（名字拼错、或标题改过词）。"""
    if name in _RULES_OPTIONAL:
        try:
            rules = (cspec._SKILL_DIR / "remotion-rules.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        return _extract_md_block(rules, _RULES_OPTIONAL[name])
    if name in _SKILL_OPTIONAL:
        return _extract_md_block(_ART_DIRECTION_RE.sub("", _read_skill()),
                                 _SKILL_OPTIONAL[name])
    return ""


def _shot_sections_block(shot: Mapping[str, Any]) -> str:
    """这一镜点名的按需块（批 4：渐进式加载的第三层）。空串＝没点名。

    与 ``_contract(extra=)`` 的区别：那个是**整片**一次定的，进 system 提示、
    吃前缀缓存；这个是**逐镜**的，只能进 user 消息——system 一镜一变，前缀缓存
    就整片作废（那比多读 2 KB 贵得多）。
    """
    names = shot.get("sections") or []
    if isinstance(names, str):
        names = [names]
    parts: list[str] = []
    for n in names:
        got = _block_text(str(n or "").strip().lower())
        if got:
            parts.append(got)
    if not parts:
        return ""
    return ("=== 这一镜额外需要的写法（计划期点名）===\n"
            + "\n\n".join(parts) + "\n")


#: 全片画法账（2026-10-23）：**所有角色读同一份、写手各写一行回填**。
#:
#: 为什么要它：写手是 N 个互不通信的并行调用，唯一的共享事实是 `_film_block` 里
#: 那张「计划」的画面表。但计划不等于画出来的样子——第一镜实际用了什么光位、
#: 主体占多大、字摆哪，只有它的**代码**知道。这一份账把那个事实变成全片可见。
#:
#: 为什么零成本：写手本来就被要求写一条 `// 实体：…｜光：…｜字：…` 注释
#: （见 `_shot_block` 的「落笔前先写一条注释」）。直接抽那一行，不需要额外 LLM 调用、
#: 不需要模型自己总结——**同一份事实只写一遍**。
#:
#: 为什么放 user 消息而不放 system：账随第一镜写完后才出现，放进 system 会让
#: 每一镜的 system 都不一样，前缀缓存整片作废。
_LOOK_RE = re.compile(r"^\s*//\s*(实体[:：].*)$", re.M)


def _look_from_code(code: str, limit: int = 300) -> str:
    """从一镜的代码里抽出它自报的画法那一行。→ 空串＝这一镜没写注释。"""
    m = _LOOK_RE.search(code or "")
    return (m.group(1).strip()[:limit] if m else "")


def _seed_ledger(spec: Mapping[str, Any], code: str,
                 shot: Mapping[str, Any] | None = None) -> None:
    """把一镜自报的画法写进全片账。**只补缺，不覆盖**——第一镜立的规矩最权威。"""
    if not isinstance(spec, dict):
        return
    led = spec.get("film_ledger")
    if not isinstance(led, dict):
        led = {}
        spec["film_ledger"] = led
    look = _look_from_code(code)
    if not look:
        return
    if not str(led.get("look") or "").strip():
        led["look"] = look
    if shot is not None:
        sid = str(shot.get("id") or "")
        if sid:
            done = led.get("shots")
            if not isinstance(done, dict):
                done = {}
                led["shots"] = done
            done[sid] = look[:120]


def _ledger_block(spec: Mapping[str, Any]) -> str:
    """全片画法账 → 给这一镜看的那一段。空串＝账还没立起来（第一镜之前）。"""
    led = spec.get("film_ledger")
    if not isinstance(led, Mapping):
        return ""
    parts: list[str] = []
    look = str(led.get("look") or "").strip()
    if look:
        parts.append("=== 全片画法账（第一镜立的规矩；照它画，别另起一套）===\n" + look)
    done = led.get("shots")
    if isinstance(done, Mapping) and done:
        rows = [f"{sid}：{str(v).strip()}" for sid, v in done.items() if str(v or "").strip()]
        if rows:
            parts.append("已经画完的镜（保持同一种画法，别跟它们打架）：\n" + "\n".join(rows[:12]))
    # 评审写回账的那几条（协作的「回程」）：重写任意一镜时都看得到——
    # 这样 s03 的毛病不会在 s07 上再犯一次。
    fixes = led.get("fixes")
    if isinstance(fixes, (list, tuple)) and fixes:
        rows = []
        for f in fixes[:8]:
            if not isinstance(f, Mapping):
                continue
            ids = "、".join(str(x) for x in (f.get("ids") or []) if str(x).strip())
            what = str(f.get("what") or "").strip()
            fix = str(f.get("fix") or "").strip()
            if what or fix:
                rows.append(f"· [{ids or '全片'}] {what} → {fix}")
        if rows:
            parts.append("全片复核挑出的问题（改这一镜时一并避开）：\n" + "\n".join(rows))
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


#: 全片复核（协作的「回程」）：写完一遍之后，一次调用看全片。
#:
#: 为什么要有它：写手是 N 个互不通信的并行调用，`film_ledger` 只能保证「照第一镜
#: 的画法画」，保证不了「这 12 镜合起来像一条片子」——配色跳、构图重、节奏平，
#: 只有把全片放在一起看才看得出来。原来这一维由闸⑨（跨镜同构）判，2026-10-23
#: 撤掉了（它判的是机器形态，不是画面）。
#:
#: 为什么只回 JSON、只挑 3 条：它不改代码、不复述要求，只给「哪几镜、改什么」。
#: 挑 3 条是因为一次改太多等于重写整片，预算与收益都不成立。
FILM_REVIEW_ENABLED = True
FILM_REVIEW_MIN_SHOTS = 4
FILM_REVIEW_MAX_ISSUES = 3

_FILM_REVIEW_SYSTEM = """你是这条片的剪辑师。你不改代码，只回答一件事：这 N 镜放在一起，
像不像一条片子。只看三件：**配色跳没跳、构图重没重、节奏平不平**。

只回一个 JSON，不要解释：
{"issues":[{"ids":["s03","s07"],"what":"一句话说清哪里不对","fix":"一句话说清怎么改"}]}

没有问题时回 {"issues":[]}。最多 3 条，每条只说一件可改的事；不要评价单镜画得好不好。
"""


def _film_review_input(spec: Mapping[str, Any]) -> str:
    """复核要看的东西：每镜的文案、brief、以及它**实际画成了什么样**（账里的那一行）。"""
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    led = spec.get("film_ledger") if isinstance(spec.get("film_ledger"), Mapping) else {}
    done = led.get("shots") if isinstance(led.get("shots"), Mapping) else {}
    rows = []
    for i, s in enumerate(shots):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        text = str(s.get("text") or "").strip()[:40]
        brief = str(s.get("brief") or "").strip()[:60]
        actual = str(done.get(sid) or "").strip()[:100]
        rows.append(f"{sid}｜文案：{text}｜计划：{brief}｜实际：{actual or '（没记账）'}")
    return "\n".join(rows)


def record_fix(spec: Mapping[str, Any], ids: Sequence[str], what: str, fix: str) -> None:
    """评审结论写回全片账（有上限，去重）。"""
    if not isinstance(spec, dict):
        return
    led = spec.get("film_ledger")
    if not isinstance(led, dict):
        led = {}
        spec["film_ledger"] = led
    fixes = led.get("fixes")
    if not isinstance(fixes, list):
        fixes = []
        led["fixes"] = fixes
    key = (tuple(sorted(str(x) for x in ids)), str(what or "").strip())
    if any((tuple(sorted(str(x) for x in (f.get("ids") or []))),
            str(f.get("what") or "").strip()) == key for f in fixes if isinstance(f, Mapping)):
        return
    fixes.append({"ids": [str(x) for x in ids], "what": str(what or "").strip(),
                  "fix": str(fix or "").strip()})
    del fixes[8:]


async def review_film(spec: Mapping[str, Any], *, llm: Any, user_id: str = "",
                      conversation_id: str = "") -> dict[str, Any]:
    """全片复核：一次调用看全片 → 把结论写进 `film_ledger["fixes"]`。

    → ``{"issues": [...], "cost_yuan": float}``；任何失败都返回空（**不拦出片**）。
    """
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    if not FILM_REVIEW_ENABLED or len(shots) < FILM_REVIEW_MIN_SHOTS:
        return {"issues": [], "cost_yuan": 0.0}
    messages = [{"role": "system", "content": _FILM_REVIEW_SYSTEM},
                {"role": "user", "content": _film_review_input(spec)}]
    try:
        resp = await _complete_with_identity(
            messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
            max_tokens=1200, timeout=WRITER_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 —— 复核是加分项，炸了不能拦出片
        return {"issues": [], "cost_yuan": 0.0}
    usage = getattr(resp, "usage", None)
    spent = cost_yuan(usage, model=str(usage.get("model") or "")) if usage else 0.0
    text = str(getattr(resp, "content", "") or "")
    obj = None
    try:
        obj = json.loads(text[text.find("{"): text.rfind("}") + 1]) if "{" in text else None
    except (ValueError, TypeError):
        obj = None
    raw = obj.get("issues") if isinstance(obj, Mapping) else None
    issues: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw[:FILM_REVIEW_MAX_ISSUES]:
            if not isinstance(item, Mapping):
                continue
            ids = [str(x) for x in (item.get("ids") or []) if str(x).strip()]
            what = str(item.get("what") or "").strip()
            fix = str(item.get("fix") or "").strip()
            if not (what or fix):
                continue
            issues.append({"ids": ids, "what": what, "fix": fix})
            record_fix(spec, ids, what, fix)
    return {"issues": issues, "cost_yuan": round(spent, 4)}


#: brief 里导演写的那段构图骨架的标记（`art_director._SYSTEM` 里定的）。
FRAME_MARK = "【构图】"


#: brief 里点到「发光」的词。命中才给 glow 的写法——它是**这一镜的画法**，
#: 不是整片纪律，塞进 `_SYSTEM_HEAD` 会让 12 镜的提示词每镜都长一段。
#: 为什么要这一段（2026-10-24 实测）：`@remotion/effects/glow` 早在 import 白名单里，
#: 但写手提示词从头到尾没提过它，只把「光晕」当背景层的一层渐变——成片 12 镜
#: 全是 `radialGradient`、glow 用 0 次，观众看到的就是「贴上去的一团白」。
#: 参数全部真渲量出来的（1920×1080 / #05070d 底 / 白圆半径 160 / 量圆外亮度）：
#:   radius 320 → 圆外 80px 81、240px **10**（光晕到不了远处，看着像描边）
#:   radius 560 → 圆外 80px 86、240px **30**（这才是「一圈柔光」）
#:   intensity 2.4 只把近处提到 102，远处不动 → 想铺开加 radius，想更亮加 intensity
#:   threshold 0.35→0.2 没有任何变化（源本来就白）→ 别指望它调软硬
#: 另测过两条死路：`<Img>` 装**透明底** SVG 挂 glow，光晕被 alpha 挡在形状里（圆外 60px 只到 4/255）；
#: `<Solid>` 是纯色矩形，只会整体提亮没有外溢。
_GLOW_HINTS: tuple[str, ...] = ("光晕", "辉光", "发光", "过曝", "glow", "柔光", "光核")


def _glow_block(shot: Mapping[str, Any]) -> str:
    """这一镜的点名光效怎么写。brief 没提发光就返回空串（老片子逐字不变）。"""
    brief = str(shot.get("brief") or "")
    if not any(h in brief for h in _GLOW_HINTS):
        return ""
    return (
        "**真光晕怎么写**（这一镜 brief 点了发光体）：别用 `radialGradient` 假装辉光——"
        "那是贴上去的一层渐变，边缘是硬过渡、暗部不会溢光，缩到 1/3 就露馅。"
        "用官方效果件 `@remotion/effects/glow`，把它包在 `<HtmlInCanvas>` 里"
        "（`glow` 只认 `<HtmlInCanvas>` / `<Img>` / `<Solid>`，写在 `<div>` 或裸 `<svg>` 上是"
        "**静默空操作**——不报错、也没效果）：\n"
        "```tsx\n"
        "import { AbsoluteFill, HtmlInCanvas, useCurrentFrame, useVideoConfig } from 'remotion';\n"
        "import { glow } from '@remotion/effects/glow';\n"
        "// …组件里：\n"
        "const { width, height } = useVideoConfig();\n"
        "const p = 0.85 + 0.15 * Math.sin(frame / 8);        // 只许用 frame，禁 Math.random\n"
        "<AbsoluteFill style={{ backgroundColor: '#05070d' }}>\n"
        "  <HtmlInCanvas width={width} height={height}\n"
        "    effects={[glow({ radius: 560, intensity: 2.2, threshold: 0.35, color: '#FFC93C' })]}>\n"
        "    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>\n"
        "      <rect width={width} height={height} fill=\"#05070d\" />   {/* 底色必须画在里面 */}\n"
        "      <circle cx={width / 2} cy={height / 2} r={150 * p} fill=\"#FFC93C\" />\n"
        "    </svg>\n"
        "  </HtmlInCanvas>\n"
        "  {/* HUD、刻度、读数、字幕、<F> 字段标注：DOM 叠在 HtmlInCanvas 外面 */}\n"
        "</AbsoluteFill>\n"
        "```\n"
        "四条参数硬约束（真渲量出来的，不是猜的）：\n"
        "① **radius 取 480~640**。320 太小：圆外 240px 只剩 10/255，看着像一圈描边而不是柔光；"
        "560 在圆外 240px 还有 30/255，才铺得开。\n"
        "② **intensity 1.8~2.4**。它只抬近处的亮度（2.4 时圆外 80px 到 102），铺开靠 radius；"
        "超过 2.6 橙色会烧成黄、青色会烧成白，颜色跑掉。\n"
        "③ **threshold 0.3~0.4 就够，别再往下调**。源本身是亮的时候，0.2 与 0.35 逐像素一样；"
        "调低只会把中间调也拖进光晕，画面发糊。\n"
        "④ **glow 是整幅染色**：`color` 写这一镜的主色，一镜只用一组 glow（一个色相）。"
        "第二个颜色的发光体改用「多层径向渐变 + screen 混合」手工近似，别塞第二组参数。\n"
        "**`<HtmlInCanvas>` 里面的画面必须是 `<svg>`**（`<rect>`/`<circle>`/`<path>` 那些），"
        "不许用带 `background` 的裸 `<div>` 画色块（闸① `dg/no-raw-styled-div` 会拦）。"
        "`width`/`height` 写画幅真实像素（`useVideoConfig()` 的），别只写 viewBox 缩放坐标。\n"
        "**HUD、刻度、读数、字幕与 `<F>` 字段标注一律放在 `<HtmlInCanvas>` 外面**（DOM 叠上去）："
        "写进 canvas 里就套不上字幕件，字段也对不上账。\n"
        "最后一条：`<HtmlInCanvas>` 每帧要把 HTML 栅格化一遍，比普通镜贵。"
        "**只给发光体是主角的镜用**，整片控制在 1/3 以内。\n")



def _frame_floor_block(shot: Mapping[str, Any]) -> str:
    """这一镜的**构图底线**。有导演写的 `【构图】` 就把它钉成底线，没有就给一份缺省的。

    为什么要这一层（2026-10-24）：2026-10-23《一秒》12 镜视觉评审**全不过**，看图
    确认是同一个病——每镜只有「一个主体 + 一块底色」，四角全空。根因不在通道、
    不在配方卡，而在 brief 只写「画面中央一颗心脏（占画面高 44%）」：**brief 里
    没写的东西，写手一律不画**，于是画面就只剩那一颗心脏。

    这件事以前没人负责：导演被明确要求「别硬凑实体」，写手被明确告知「brief 是
    最高事实」，评审看得出来但只能说、改不了 brief。所以把它落在这一层——
    导演写了就以导演为准（它看得到风格，知道该密还是该空）；导演没写（关掉了、
    或调用失败）就给一份缺省底线，不让画面退化成「一个东西漂在黑底上」。

    例外是 `【构图】极简：…`：那是导演明确判过「这一镜就该留白」（剪影、纯字、
    单线），照它走，不补 HUD。
    """
    brief = str(shot.get("brief") or "")
    # 评审打回「太空」之后由 `motion_plan._bump_density_by_review` 补上的那一份。
    # 单独一个键而不是塞进 brief：brief 有 400 字硬上限，末尾追加会被截掉——
    # 而被截掉的正好是这段，等于白补。
    note = str(shot.get("frame_note") or "").strip()
    if note:
        return (
            "上面 `【构图】` 那一行是这一轮**评审打回后补的构图底线**，六段都要真的落到画面上：\n"
            f"{note}\n"
            "省掉背景层或前景 HUD，渲出来还是「一个东西漂在黑底上」；"
            "配色只用一根细线或一个圆点，渲出来还是**黑白片**——两样都会再被打回。\n")
    if FRAME_MARK in brief:
        tail = brief.split(FRAME_MARK, 1)[1].strip()
        if "极简" in tail[:12]:
            return ("上面 `【构图】` 判的是**极简留白**：这一镜只有主体与底色，"
                    "不要加网格、HUD 与多余装饰——但要保证主体本身有体积、有受光面"
                    "与接触阴影，别退化成一根线。\n")
        return (
            "上面 `【构图】` 那一行是这一镜的**构图底线**，六段都要真的落到画面上：\n"
            "· **背景层**（底色 + 网格/暗角/雾/光晕）先画，它决定「看着满不满」；\n"
            "· **中景**按它给的主体群画，别只画一件；\n"
            "· **前景 HUD**（刻度尺 + 读数 + 标注线）是画面的一部分，不是装饰；\n"
            "· **配色**按它给的十六进制写，**饱和色块要真的占住它说的那个比例**——\n"
            "  颜色只出现在一根线或一个小圆点上，整幅就是灰的（实测饱和度只有 1.6/255，\n"
            "  等于黑白片）；\n"
            "· **运镜**要做出来——整幅在动，闸⑭ 才数得出变化段。\n"
            "省掉背景层或 HUD，渲出来就是「一个东西漂在黑底上」，会被评审打回。\n")
    return (
        "**构图底线**（brief 没给构图骨架，按这四条自己补，缺一条就算没画完）：\n"
        "· 背景层：底色（别用纯黑）+ 一层网格或暗角 + 一层雾/光晕，先画它；\n"
        "· 前景 HUD：一条刻度尺 + 两个读数 + 一条标注线/括号，压在主体之外；\n"
        "· 配色：三个**高饱和**色相（如 #FF6B2C / #22D3EE / #FFC93C），"
        "饱和色块合计占画面 ≥25%——粉彩的暖白与灰蓝渲出来是黑白片；\n"
        "· 镜头运动：推近 8~15% 或横移 6% 或绕转 8°，整幅在动。\n"
        "除非这一镜的形式本身就是极简留白（剪影/纯字/单线），否则不许只有主体与底色。\n")


def _shot_block(shot: Mapping[str, Any], *, index: int,
                spec: Mapping[str, Any] | None = None) -> str:
    fields = [f for f in (shot.get("fields") or []) if isinstance(f, Mapping)]
    if fields:
        flines = "\n".join(
            f"- {f.get('name')}（{f.get('type')}，当前 {f.get('value')!r}）"
            f"{'：' + str(f.get('label')) if f.get('label') else ''}" for f in fields)
        fields_text = ("本镜声明了可改字段——代码里必须在对应位置用 "
                       "`<F name=\"…\" value={fields.字段名}>` 标注（否则闸① field-drift 报错）：\n"
                       + flines)
    else:
        fields_text = "本镜没有可改字段。"
    sec = shot.get("min_duration_sec")
    try:
        sec_txt = f"{float(sec):.1f} 秒" if sec else "由系统按时长对齐决定"
    except (TypeError, ValueError):
        sec_txt = "由系统按时长对齐决定"
    highlights = "、".join(str(h) for h in (shot.get("highlight") or [])) or "（无）"
    entities = [str(e).strip() for e in (shot.get("entities") or []) if str(e).strip()]
    entities_text = ("、".join(entities) if entities
                     else "（清单没给，按文案圈能画的实体；只有一件就画一件，别硬凑）")
    # 分镜阶段申报的动效（`shot.motion`）：从前它只进闸㉑ 的对账，**从不进写手的
    # 提示词**——写手看不到「这一镜该动几拍、每拍是什么」，只能照 brief 自己猜，
    # 猜完再被草稿闸数出来「只有一段」，重写一遍。2026-10-20 那条 53 镜的片子：
    # 39 镜写了两遍，八成墙钟花在重写上。把它摆在 brief 后面，写手照着拍子画。
    beats = [m for m in (shot.get("motion") or []) if isinstance(m, Mapping)]
    if beats:
        lines = "\n".join(
            f"{i}. {str(m.get('kind') or '').strip() or '（这一条没写是什么动效）'}"
            f"（锚：{'、'.join(str(a) for a in (m.get('anchors') or [])) or '无'}）"
            for i, m in enumerate(beats, 1))
        motion_text = (
            "这一镜申报的动效（按发生顺序，**每一条都要真的在像素上发生**）：\n"
            + lines + "\n"
            "照这份清单排拍子，不要另发明一套：每一拍都要让画面相当一块地方变样"
            "（满宽或占画面三分之一以上的块面擦入/展开/位移；只有细线在动、只有淡入"
            "淡出，系统数不出变化段），相邻两拍的起点至少隔 0.4 秒（约 12 帧），"
            "最后一拍在 80% 时长之前收住。\n")
    else:
        motion_text = ""
    return (
        f"=== 这一镜 ===\n"
        f"镜号：{shot.get('id') or f's{index + 1:02d}'}\n"
        f"文案（观众会看到的字幕）：{str(shot.get('text') or '').strip()}\n"
        # 批 3：相邻两镜的画面。放在文案之后、brief 之前——先知道自己在整片里的
        # 位置，再读这一镜要画什么。
        f"{_neighbour_rows(spec, index) if spec else ''}"
        # 全片画法账：所有镜读同一份，第一镜写完才有。放在这里（brief 之前）
        # 是因为它定的是「怎么画」，brief 定的是「画什么」。
        f"{_ledger_block(spec) if spec else ''}"
        f"先圈实体：这句里有哪些人/物/地点/动作的结果能画进画面？"
        f"它们不是各画各的，而是**同处一个场景**——先想清谁前谁后、谁挡谁、光从哪来。"
        f"不是把文案排成一页。\n"
        "落笔前先在 import 之后、组件之前写一条注释，逐件拆到「少了它就不像」的细节："
        "`// 实体：<实体1>=<外轮廓>+<标志物>+<2 个部件>（受光面/背光面/影）；"
        "<实体2>=…｜光：<方向>｜字：<位置>`，再写实现。"
        "拆不出标志物与部件，说明这一镜还没想好，回到 brief 里找。\n"
        f"高亮词：{highlights}\n"
        f"画面 brief（这一镜画什么、怎么构图、怎么分几拍动）："
        f"{str(shot.get('brief') or '').strip() or '（没写，按文案画）'}\n"
        f"{_frame_floor_block(shot)}"
        f"{_glow_block(shot)}"
        f"{motion_text}"
        # 配方卡（2026-10-23）：`shots/` 下 138 篇做法文档被 SKILL.md 的
        # `exclude: demos/, shots/` 排除掉了，写手只看得到正文的**原则**、看不到**手法**。
        # 这里按这一镜的 subject / brief / 申报通道挑最接近的一篇，只塞「意图 / 动效核心 /
        # 参数表」三段（约 3 KB）。放在 motion_text 之后——先读「这一镜要动几拍」，
        # 再看「同类镜头别人是怎么拍的、参数取多少」。
        f"{recipe_index.block(shot, spec) if spec else ''}"
        # 这一镜点名的能力正文（渐进式加载的第二层）。空串＝这套风格不是渐进式，
        # 或者这一镜没点名——两种情况下这一句都不出现，老片子逐字不变。
        f"{_caps_block(shot, spec) if spec else ''}"
        # 批 4：这一镜自己点名的按需块（逐镜渐进式加载）。
        f"{_shot_sections_block(shot)}"
        # 真机 2026-10-19：brief 已写「全片无人物」，评审红镜的自动重写却把主体换回
        # 人影——重写读到的风格包段在说「主体用模板走路组件」，比 brief 那句更响。
        # brief 是这一镜的最高事实，模板只是它没排除那部分的默认画法。
        "**brief 是这一镜的最高事实**：brief 明确说不要的东西（例如「画面里不要人」"
        "「不要参考片里的人影」）这一镜就**不要画**——它优先于风格包模板的缺省零件。"
        "不要用 0 / 空串 / 透明把它藏进来。画面上的每一件东西都由你按 brief 和"
        "风格不变量自己画；模板是可选画法，不是必须套的零件。\n"
        f"本镜实体（每一个都要画出来、一眼认得出，且有空间关系）：{entities_text}\n"
        f"申报主体：{str(shot.get('subject') or '').strip() or '（没填）'}\n"
        f"本镜时长：{sec_txt}；代码里用 `useVideoConfig()` 的 durationInFrames，别写死帧数。\n"
        "跨镜连续的量（步态相位 `phase`、主体位移）按**全片时钟**算："
        "`const { filmStartSec = 0 } = getInputProps();`，"
        "把 `filmStartSec + frame/fps` 当成这一镜在整片里的绝对时间。"
        "**不要写死起点秒数**（真实时长由配音决定，渲染侧会注入），"
        "也不要每镜从 0 起——同一段连续行走被切开时，下一镜的位置与相位要接得上。\n"
        f"{fields_text}\n"
        "动起来别是「一帧定格再淡出」：至少两拍——实体先后入画（stagger）→ 主运动"
        "（视差/呼吸/跟随/转场）→ 最后一拍收住，铺满整镜。\n"
        # 闸⑭ 的实测口径写在第一枪里：真机上几乎所有镜都要写第二遍才过，因为写手把
        # 「两拍」理解成一根线/一个小圆点在动，而判据量的是**整幅灰度的大块面变化**。
        # 先把它写清楚，省掉一轮重写（37 镜 × 一次往返）。
        "系统会真渲一版草稿数变化段，判据是：整幅画面至少**两段大块面变化**，彼此相隔"
        "≥0.4 秒（约 12 帧），两段都要占画面相当一块——只有一个元素的小幅位移数不出来。"
        "第二段要在 80% 时长之前完成。\n"
        "交之前按这五条自检（美术评审看的也是这五条）："
        "① 文案点到的**每一件**实体都在画面里（不止一件时都要在），不是一排并排的小图标；"
        "② 主体占 40%~70%，不缩在角落；"
        "③ 有明确光源方向、亮面/暗面/落地影；④ 字号有阶梯、不是居中堆字；"
        "⑤ 不是卡片墙/表格框/色块+字。有一条明显没做到，先改代码再交。\n"
        "只回这一镜的 ```tsx 代码块。"
    )


_FENCE_RE = re.compile(r"```(?:tsx|ts|jsx|javascript|typescript)\s*\n(.*?)```", re.S)
_ANY_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    """从回话里取出那一份 TSX。优先语言标记明确的块，其次最长的块，最后从首个 import 起。"""
    blocks = [b.strip() for b in _FENCE_RE.findall(text or "") if b.strip()]
    if not blocks:
        blocks = [b.strip() for b in _ANY_FENCE_RE.findall(text or "") if b.strip()]
    if blocks:
        return max(blocks, key=len)
    src = (text or "").strip()
    m = re.search(r"^(?:import|const|type|interface)\b", src, re.M)
    if m:
        return src[m.start():].strip()
    return src


def _fmt_lint(e: Any) -> str:
    if not isinstance(e, Mapping):
        return str(e)
    rule = str(e.get("rule") or "")
    where = f"第 {e.get('line')} 行" if e.get("line") else ""
    msg = str(e.get("msg") or e.get("message") or e)
    return f"{'[' + rule + '] ' if rule else ''}{where} {msg}".strip()


def _lint(code: str, names: Sequence[str], client: Mapping[str, Any],
          timeout: int = 60) -> tuple[bool | None, list[str], str, list[str]]:
    """→ (过没过 / None=未判, 错误行, 未判原因, 提醒行)。

    **第四项是 2026-10-23 加的**：闸分两档之后，`warn` 档的规则不进 `errors`（不拦），
    但也不能悄悄丢掉——一条没人看得见的提醒等于把闸删了。调用方把它写进回执。
    """
    lint = client.get("lint")
    if lint is None:
        return None, [], "渲染容器没有 lint 口", []
    try:
        got = lint(code, list(names))
    except Exception as exc:  # noqa: BLE001 —— 外呼失败＝未判，不是通过
        return None, [], f"{type(exc).__name__}: {str(exc)[:160]}", []
    if not isinstance(got, Mapping) or "ok" not in got:
        return None, [], "lint 响应里没有 ok 键", []
    warns = [_fmt_lint(e) for e in (got.get("warnings") or [])]
    if got.get("ok"):
        return True, [], "", warns
    return False, [_fmt_lint(e) for e in (got.get("errors") or [])], "", warns


def _motion_feedback(account: Mapping[str, Any], *, weak: bool = False,
                     bands: bool = True) -> str:
    """闸⑭ 的账 → 给写手看的一句话（带它自己数到的段与间隔）。

    只报「密度不够」是没法改的：模型需要知道它**画出来的**是几段、两段起点隔多少、
    每段峰值多大，才能判断是「压根没动」还是「两件事挤在一起」，或者「动了但太淡」。
    所以段数、起点、峰值一定抄进来。
    """
    evs = [e for e in (account.get("events") or []) if isinstance(e, Mapping)]
    th = account.get("thresholds") if isinstance(account.get("thresholds"), Mapping) else {}
    want = th.get("min_events")
    gap = th.get("min_gap_sec")
    floor = th.get("diff_min")
    floor_txt = f"{floor}" if isinstance(floor, (int, float)) else "1.5"
    if weak:
        parts = [f"闸⑭ 草稿实测：段数够了，但峰值压线——成片档会掉到地板线 {floor_txt} 以下："]
    else:
        parts = [f"闸⑭ 动效密度没过（草稿真渲实测）："
                 f"{account.get('reason') or '容器没给理由'}"]
    if evs:
        parts.append("实测变化段：" + "；".join(
            f"第 {i + 1} 段起点 {e.get('start_sec')}s、峰值 {e.get('peak_diff')}"
            for i, e in enumerate(evs)))
    else:
        parts.append("实测变化段：一段都没有（整镜像素从头到尾没变过）。")
    # 亚门槛的「差一点」：真机里第二处动效经常**画了**但只到 1.2（地板线 1.5），
    # 而回执只说「数到 1 段」，写手于是重排时间轴，而不是把那一处做大。
    near = [n for n in (account.get("near") or []) if isinstance(n, Mapping)]
    if near:
        parts.append("差一点的动静（没过地板线，没算进段数）：" + "；".join(
            f"{n.get('start_sec')}s 处峰值 {n.get('peak_diff')}"
            for n in near))
    if weak:
        parts.append(
            f"峰值要高出地板线 {WRITER_MOTION_MARGIN} 倍才算稳（约 "
            f"{float(floor_txt) * WRITER_MOTION_MARGIN:.1f}）。把最淡的那一处块面"
            "**做大做实**：加宽到满宽或占画面三分之一以上、不透明度提到 0.6 以上、"
            "让它在几十帧里从 0 长到终态；不要只调位置或做淡入淡出。")
    if bands:
        parts.append(
            f"判据：≥{want if isinstance(want, int) else 2} 段先后变化，相邻段起点"
            f"≥{gap if gap is not None else 0.4}s。线稿本身整幅平均差只有 0.05~0.3，"
            "要过 1.5 必须做**满宽**的动：满宽色带擦入、或满宽面板自上/下展开/收拢。"
            "做两处，第二处起点比第一处晚 ≥0.4s（最好 1s 以上）；别把块面放进字幕安全区。"
            "**第二处要和第一处一样满**：满宽色带实测能到 10 以上，三分之一宽的小面板只有 1 左右"
            "（差一点就是过不了）——两处都用满宽，只是高度不同（如 6% 与 20%）、进场方向不同。"
            "只让细线移动、只做淡入淡出，读数到不了地板线。")
    else:
        # 不用色带顶过闸⑭（风格包 `fullwidth_bands=false`）。读数一样能达到：
        # **整幅的光**比色带更容易到线，而且它是场景里真的发生的事。
        parts.append(
            f"判据：≥{want if isinstance(want, int) else 2} 段先后变化，相邻段起点"
            f"≥{gap if gap is not None else 0.4}s。**本片不要满宽色带/满宽面板**——"
            "用**整幅的光**来顶（这一条最好用）：火忽然亮一档、天光从冷推到暖、"
            "探照灯横着扫过、灯被点亮、屏幕由暗转亮——整帧的亮度与色温一起动，"
            "读数常常 10 以上。其次：雾/烟/雪/火星的浓度从无到有、相机推近或横移、"
            "主体的大动作。两处要分开时间：第一处在前 40%，第二处在 55%~80%，"
            "中间至少隔 0.4s（最好 1s 以上）。只让细线或小零件移动、只做淡入淡出，"
            "读数到不了地板线。**别指望「让人走出去」**——原地踏步的人不改变整幅像素。")
    return "\n".join(parts)


async def _motion_check(shot: Mapping[str, Any], *, spec: Mapping[str, Any],
                        code: str, client: Mapping[str, Any], sid: str
                        ) -> tuple[bool | None, str, dict[str, Any] | None]:
    """真渲一版草稿数闸⑭ → (过没过 / None=未判, 给人话的那句, 账)。

    `None` 的语义与闸① 那条一致：**拿不到判据 ≠ 判过**。调用方照收代码、把这句
    写进回执，让成片渲染那一步再用同一道闸判一次；绝不在这里折成通过。
    第三项是 ``summarize_motion`` 那一份账（段数/起点/峰值），回执与验收都靠它。
    """
    render = client.get("render")
    if render is None:
        return None, "闸⑭ 未判：渲染容器没有 render 口。代码已收下，**这不是通过**。", None
    bands = True
    # 侦测用的镜头副本：`shot_input_props` 要读 `code`（判字幕是不是代码自己摆的），
    # 而调用方在通过之前不把 code 写回去——所以这里带上一份，别改调用方的状态。
    probe = dict(shot)
    probe["code"] = code
    try:
        from . import code_render as crender
        # 与预览静帧同一把「估帧数」的尺：写手量的时长就是预览/成片将要渲的时长。
        frames = crender._preview_frames(probe, spec)
        props = crender.shot_input_props(probe, spec, frames=frames, frame_words=[])
    except Exception as exc:  # noqa: BLE001 —— 组 props 失败＝未判
        return None, (f"闸⑭ 未判：没法组出这一镜的渲染入参"
                      f"（{type(exc).__name__}: {str(exc)[:120]}）。**这不是通过**。"), None
    key = "writer-motion-" + sid + "-" + hashlib.sha256(
        (code + "\x00" + json.dumps(props, sort_keys=True, ensure_ascii=False))
        .encode("utf-8")).hexdigest()[:20]
    try:
        resp = await asyncio.to_thread(
            render, code, key, props, None, WRITER_DRAFT)
    except Exception as exc:  # noqa: BLE001 —— 外呼失败＝未判，不是通过
        return None, (f"闸⑭ 未判：草稿真渲没跑成"
                      f"（{type(exc).__name__}: {str(exc)[:160]}）。**这不是通过**。"), None
    account = crender.summarize_motion(sid, (resp or {}).get("motion"))
    status = str(account.get("status") or "")
    if status == "rejected":
        msg = _motion_feedback(account, bands=bands)
        if MOTION_DENSITY_BLOCKS:
            return False, msg, account
        # 2026-10-23：降级为只记账。用户原话「你这样用闸限制，会影响效果懂不懂」——
        # 闸⑭ 逼着每一镜去做「满宽块面动」，而剪影/平涂这类形式里满宽色带一眼假：
        # **闸逼出来的画面比没有它更差**。读数照抄进回执（人还能看），但不再重写。
        return None, msg + "\n  · 闸⑭ 已降级为只记账：这一镜照收，不再为它重写。", account
    if status == "ok":
        # 压线的「过」不算过：草稿档与成片档的像素基数不同，1.6 这种读数会在成片档
        # 掉到 1.5 以下（真机 s03 实测 1.63 → 1.484）。留出余量再收。
        th = account.get("thresholds") if isinstance(account.get("thresholds"), Mapping) else {}
        floor = th.get("diff_min")
        peaks = [e.get("peak_diff") for e in (account.get("events") or [])
                 if isinstance(e, Mapping) and isinstance(e.get("peak_diff"), (int, float))]
        if isinstance(floor, (int, float)) and peaks:
            if min(peaks) < float(floor) * WRITER_MOTION_MARGIN:
                msg = _motion_feedback(account, weak=True, bands=bands)
                if MOTION_DENSITY_BLOCKS:
                    return False, msg, account
                return None, msg + "\n  · 闸⑭ 已降级为只记账：这一镜照收。", account
        return True, "", account
    return None, (f"闸⑭ 未判：{account.get('reason') or '容器没给出理由'}。"
                  "代码已收下，**这不是通过**。"), account


#: 写手「先读文件再动笔」的轮数（2026-10-24）。0 = 关掉。
WRITER_READ_ROUNDS = 3

#: 给写手的唯一工具：读一份导入技能里的文件。
#:
#: 为什么要这一步：写手是一次性调用，技能正文与配方卡都在 prompt 里，但**看不到**
#: 技能附件里的具体实现（`video-shotcraft` 有 1011 个文件、`huashu` 368 个）。
#: 给它一个读文件的工具，它就能在动笔前自己决定「这一镜该照抄哪一段」——
#: 这是「完整使用 skill」和「只看到我们筛过的那几篇」的区别。
_READ_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_skill_file",
        "description": "读一份导入技能里的文件（.md / .tsx / .js / .html 等）。"
                       "动笔前不确定该用哪套画法、或想照抄某个示例时先读它。"
                       "读完会拿到文件正文，然后再写代码。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "「技能名:相对路径」，必须逐字来自这一镜 prompt 里"
                                   "「可用参考」那一段列出的路径。",
                },
            },
            "required": ["path"],
        },
    },
}

#: 第二个工具：翻目录。没有它，写手只能读我们挑的那 24 条。
_LIST_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "list_skill_files",
        "description": "列出某份导入技能里的文件路径（可加目录前缀缩小范围）。"
                       "想找「有没有讲过 X」但不知道文件名时先列目录，再读具体文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "技能名；留空列所有已挂载的"},
                "prefix": {"type": "string",
                           "description": "路径前缀，例如 demos/effects/"},
            },
        },
    },
}


async def _read_phase(messages: list[dict[str, Any]], *, llm: Any, user_id: str,
                      conversation_id: str, storage: Any,
                      rounds: int = WRITER_READ_ROUNDS
                      ) -> tuple[list[dict[str, Any]], float, list[str]]:
    """动笔**之前**让写手按需读技能文件 → (新 messages, 花费, 读过的路径)。

    放在写手这一侧而不是分配阶段：分配是「整片一次」，它只知道 brief；真正落代码的
    是写手，它才知道这一镜缺哪段实现。代价是每镜多 0~2 次小调用——**只在它主动
    要读时才发生**，不要读的镜一次都不会多花。

    失败一律降级：读不到就按「不读」继续写，绝不因为一个工具挂了拦住整镜。
    """
    from .selfcheck import _complete_with_identity
    from .recipe_index import list_files, read_paths
    from agent_framework.cost import cost_yuan

    spent = 0.0
    seen: list[str] = []
    for _ in range(max(0, rounds)):
        try:
            resp = await _complete_with_identity(
                messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
                tools=[_READ_TOOL, _LIST_TOOL], max_tokens=800, timeout=120)
        except Exception:      # noqa: BLE001 - 读不到就按「不读」继续，不拦写镜
            break
        usage = getattr(resp, "usage", None)
        if usage:
            spent += cost_yuan(usage, model=str(usage.get("model") or ""))
        calls = list(getattr(resp, "tool_calls", None) or [])
        if not calls:
            break
        messages = messages + [{
            "role": "assistant",
            "content": str(getattr(resp, "content", "") or ""),
            "tool_calls": [c.to_openai() for c in calls],
        }]
        got_any = False
        for c in calls:
            args = dict(c.arguments or {})
            if c.name == "list_skill_files":
                body = await list_files(storage, user_id,
                                        skill=str(args.get("skill") or ""),
                                        prefix=str(args.get("prefix") or ""))
                want = f"list:{args.get('skill') or '*'}/{args.get('prefix') or ''}"
            else:
                want = str(args.get("path") or "").strip()
                body = await read_paths(storage, user_id, [want]) if want else ""
            if body:
                got_any = True
                seen.append(want)
            messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                             "content": body or "（这个路径读不到，检查是否逐字来自清单）"})
        if not got_any:
            break
    return messages, spent, seen


async def _one_shot(shot: dict[str, Any], *, index: int, system: str,
                    spec: Mapping[str, Any], llm: Any, user_id: str,
                    conversation_id: str, client: Mapping[str, Any],
                    user_request: str = "",
                    references: Sequence[bytes] = (),
                    exemplar: str = "",
                    check_motion: bool = True,
                    storage: Any = None,
                    ) -> tuple[dict[str, Any], str, float, int, dict[str, Any] | None]:
    """写一镜 → (这一镜的结果, 失败说明, 花费, 尝试次数, 闸⑭ 账)。成功时直接改 shot。

    ``references`` 是参考片静帧（JPEG 字节）：有图就随这一镜的 user 消息一起发，
    让写手「看着画」而不是「听完描述猜」。``exemplar`` 是本片第一镜的代码：
    后续镜照它的人物画法写，一致性来自模型复用自己的实现，不来自共享模板。

    ``check_motion=False`` 时**不渲草稿**、不收闸⑭ 的读数（见 ``WRITER_MOTION_EVERY``
    的抽检口径）：这一次调用只做「写 + 闸①」，代价从一镜整渲降到一次网络请求。
    """
    from .selfcheck import _complete_with_identity
    from agent_framework.cost import cost_yuan

    sid = str(shot.get("id") or f"s{index + 1:02d}")
    names = [str(f.get("name") or "") for f in (shot.get("fields") or [])
             if isinstance(f, Mapping) and f.get("name")]
    ask = (_request_block(user_request) + _shot_block(shot, index=index, spec=spec)
           + _exemplar_block(exemplar)
           + (_reference_block() if references else ""))
    content: Any = ask
    if references:
        content = [{"type": "text", "text": ask}] + [
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,"
                           + base64.b64encode(b).decode("ascii")}}
            for b in references]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": content}]
    spent = 0.0
    motion: dict[str, Any] | None = None
    last_errors: list[str] = []
    motion_fails = 0
    # 动笔前先让它按需读技能文件（0~2 次小调用，只在它主动要读时发生）。
    if storage is not None and WRITER_READ_ROUNDS > 0:
        messages, read_cost, read_seen = await _read_phase(
            messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
            storage=storage)
        spent += read_cost
        if read_seen:
            print(f"[writer] {sid} 动笔前读了 {len(read_seen)} 个技能文件："
                  + "、".join(p.split(":")[-1] for p in read_seen), flush=True)
    for attempt in range(1, WRITER_ATTEMPTS + 1):
        try:
            resp = await _complete_with_identity(
                messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
                max_tokens=WRITER_MAX_TOKENS, timeout=WRITER_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 —— 一次调用炸了按重试算
            last_errors = [f"写手调用炸了（{type(exc).__name__}: {str(exc)[:160]}）"]
            continue
        usage = getattr(resp, "usage", None)
        if usage:
            spent += cost_yuan(usage, model=str(usage.get("model") or ""))
        text = str(getattr(resp, "content", "") or "")
        code = extract_code(text)
        if not code.strip():
            last_errors = ["写手这一趟没吐代码"]
            messages = messages + [
                {"role": "assistant", "content": text or "（空）"},
                {"role": "user", "content": "没有收到代码块。只回一段 ```tsx 代码块。"}]
            continue
        ok, errors, undetermined, lint_warns = await asyncio.to_thread(
            _lint, code, names, client)
        if ok is None:
            # 未判**不是通过**，但对这一镜来说也没有可行动的报错：照收，让入库前的闸①
            # 再判一次。把它写成「失败」只会让模型重写一份同样没被量过的代码。
            shot["code"] = code
            return (shot, f"{sid}：闸① 未判（{undetermined}）。代码已收下，**这不是通过**。",
                    spent, attempt, motion)
        if ok:
            if not check_motion:
                shot["code"] = code
                # 提醒（warn 档规则）写进回执：不拦，但人得看得见。
                return shot, ("；".join(lint_warns) if lint_warns else ""), spent, attempt, motion
            # 闸① 过了还不够：闸⑭ 判的是像素。这一版草稿渲出来数一遍——数得过就收，
            # 数不过就把**它自己数到的段与间隔**交给模型重写（比只在正文里写「做两处
            # 大块面的动」有用得多：真机 spec5 照着正文写，三镜仍然全红）。
            passed, motion_note, account = await _motion_check(
                shot, spec=spec, code=code, client=client, sid=sid)
            motion = account or motion
            if passed:
                shot["code"] = code
                return shot, ("；".join(lint_warns) if lint_warns else ""), spent, attempt, motion
            if passed is None:
                # 未判＝没拿到判据，不是通过：代码照收，把「这不是通过」写进回执，
                # 成片渲染那一步会拿同一道闸再判一次。
                shot["code"] = code
                return shot, f"{sid}：{motion_note}", spent, attempt, motion
            motion_fails += 1
            if attempt >= WRITER_ATTEMPTS or motion_fails > WRITER_MOTION_RETRIES:
                # 重写预算用完仍然红：**照收**，把读数原样写进回执。这不是通过，但整份
                # 分镜为了一镜的动效被拦在门口更糟——成片渲染那一步还会拿同一道闸
                # 再判一次（预览档只提醒、严格档打回），模型也能只看这一镜的补丁重交。
                shot["code"] = code
                return (shot, f"{sid}：{motion_note}\n"
                              f"  · 写了 {attempt} 次仍红，照收这一稿——**这不是通过**；"
                              "要改就只重交这一镜（shots_patch）。",
                        spent, attempt, motion)
            last_errors = [motion_note]
            messages = messages + [
                {"role": "assistant", "content": f"```tsx\n{code}\n```"},
                {"role": "user", "content": (
                    motion_note
                    + "\n照这份实测读数改，重写**完整的一镜代码**（不要只给补丁），"
                      "只回代码块。闸① 那几条不许破。")}]
            continue
        last_errors = errors
        messages = messages + [
            {"role": "assistant", "content": f"```tsx\n{code}\n```"},
            {"role": "user", "content": (
                "闸① lint 没过：\n" + "\n".join(f"· {e}" for e in errors[:12])
                + "\n照这些报错改，重写**完整的一镜代码**（不要只给补丁），只回代码块。")}]
    return (shot, f"{sid}：写了 {WRITER_ATTEMPTS} 次仍没过：" + "；".join(last_errors[:2]),
            spent, WRITER_ATTEMPTS, motion)


async def write_shot_code(spec: dict[str, Any], *, user_id: str = "",
                          conversation_id: str = "", llm: Any = None,
                          client: Mapping[str, Any] | None = None,
                          parallel: int = WRITER_PARALLEL,
                          user_request: str = "",
                          storage: Any = None,
                          references: Sequence[bytes] | None = None,
                          ) -> dict[str, Any]:
    """把 spec 里**没有 code** 的镜逐镜写出来（原地填进 ``spec["shots"]``）。

    返回一份账：``{written, notes, unwritten, cost_yuan, calls, motion}``。不抛——调用方是分镜
    入库前的那一步，它要把「哪几镜没写成」连同 lint 原文一起交回主对话。

    写码分两段（2026-10-20）：先写第一镜，再把它当 ``exemplar`` 交给其余镜并行写。
    **一致性来自模型复用自己写的第一镜**，不是人手写的共享组件——用户要的是
    「LLM 自己画」，那就让它自己画一次，然后照着同一份实现画下去。

    这一版多一道**写手侧的自查**：闸① 过了之后先渲一版草稿数闸⑭，红了就把实测读数
    交给模型重写（见 ``WRITER_MOTION_CHECK``）。不想要这 30 s/镜的代价时把它关掉。

    2026-10-21 起自查改成**抽检**（``WRITER_MOTION_EVERY``，缺省每 4 镜一镜、第一镜
    必验）：每镜都渲一遍草稿会把渲染容器占满，连预览静帧都排不进去。
    """
    client = client or rc.endpoints()
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, dict)]
    todo = [(i, s) for i, s in enumerate(shots) if not str(s.get("code") or "").strip()]
    out: dict[str, Any] = {"written": {}, "notes": [], "unwritten": [],
                           "cost_yuan": 0.0, "calls": 0, "motion": {},
                           "reference_frames": 0}
    if not todo:
        return out
    if references is None:
        references = await load_reference_images(storage, spec, user_id)
    out["reference_frames"] = len(references)
    system = _SYSTEM_HEAD + "\n" + _film_block(spec)
    gate = asyncio.Semaphore(max(1, int(parallel)))

    async def run(i: int, s: dict[str, Any], *, exemplar: str = ""
                  ) -> tuple[dict[str, Any], str, float, int, dict[str, Any] | None]:
        # 抽检：第一镜（没有 exemplar，它定的就是全片画法基准）必验；其余镜每
        # `WRITER_MOTION_EVERY` 镜验一镜。见上方那个常量的注释——每镜一验是把渲染
        # 容器整个占住，连一张预览静帧都排不进去。
        do_check = WRITER_MOTION_CHECK and (not exemplar
                                            or WRITER_MOTION_EVERY <= 0
                                            or i % WRITER_MOTION_EVERY == 0)
        async with gate:
            r = await _one_shot(s, index=i, system=system, spec=spec, llm=llm,
                                user_id=user_id, conversation_id=conversation_id,
                                client=client, user_request=user_request,
                                references=references, exemplar=exemplar,
                                check_motion=do_check, storage=storage)
        # 逐镜留一行进度：整片三四十镜时，这一行是「还在动 vs 卡死」唯一的外部证据
        # （`plan_motion` 是一个工具调用，UI 在它返回之前没有任何中间态）。
        sid = str(s.get("id") or f"s{i + 1:02d}")
        comp = (r[4] or {})
        peaks = [round(float(e.get("peak_diff")), 2) for e in (comp.get("events") or [])
                 if isinstance(e, Mapping) and isinstance(e.get("peak_diff"), (int, float))]
        status = comp.get("status") or ("skip" if not do_check else "-")
        print(f"[writer] {sid} attempt={r[3]} motion={status} "
              f"events={len(comp.get('events') or [])} peaks={peaks} "
              f"cost=¥{r[2]:.4f}", flush=True)
        return r

    if len(todo) > 1:
        # 第一镜单独写：它的代码就是后面的「画法基准」。多等这一镜，换全片主体
        # 不漂；其余镜再并行，墙钟只多一镜的时间。
        #
        # 但「第一镜」不一定是本次待写的那一镜：重交时上一版第一镜可能已经带着
        # 代码进来（单镜先写好看过，再整片补齐）。那种情况下基准就是它——一致性
        # 的定义是「照第一镜的画法继续画」，不是「照本次碰巧排在最前的待写镜」。
        seeded = next((s for s in shots if str(s.get("code") or "").strip()), None)
        if seeded is not None and not any(s is seeded for _, s in todo):
            exemplar = str(seeded.get("code") or "")
            # 全片画法账：这一镜的画法就是基准，先立账再让其余镜开工。
            _seed_ledger(spec, exemplar, seeded)
            results = await asyncio.gather(*(run(i, s, exemplar=exemplar)
                                             for i, s in todo))
        else:
            first_i, first_shot = todo[0]
            first = await run(first_i, first_shot)
            exemplar = str(first_shot.get("code") or "")
            # **账在第一镜之后立起来**：它自报的那条 `// 实体：…｜光：…｜字：…`
            # 就是全片的画法基准。其余镜开工时读到的就是它——这就是「共享」。
            _seed_ledger(spec, exemplar, first_shot)
            rest = await asyncio.gather(*(run(i, s, exemplar=exemplar)
                                          for i, s in todo[1:]))
            results = [first, *rest]
    else:
        results = await asyncio.gather(*(run(i, s) for i, s in todo))
    for shot, note, cost, calls, motion in results:
        sid = str(shot.get("id") or "?")
        # 每写完一镜就回填一行账：后面重写/补写时，所有镜都看得到它画成了什么样。
        _seed_ledger(spec, str(shot.get("code") or ""), shot)
        out["cost_yuan"] += cost
        out["calls"] += calls
        if motion:
            # 回执要能回答「这一镜数到几段、最紧的一对隔多少帧」——闸⑭ 红的时候
            # 这些数就是模型下一稿的依据，绿的时候它们证明这一镜真被量过。
            out["motion"][sid] = motion
        if str(shot.get("code") or "").strip():
            out["written"][sid] = len(str(shot["code"]))
        else:
            out["unwritten"].append(sid)
        if note:
            out["notes"].append(note)
    # 全片复核（协作的**回程**）：写完一遍之后一次调用看全片，结论写进 film_ledger。
    # 不拦出片——失败、挑不出问题都返回空。挑出来的问题会随账进入后续任何一次重写，
    # 所以 s03 的毛病不会在 s07 上再犯一遍。
    if llm is not None:
        rv = await review_film(spec, llm=llm, user_id=user_id,
                               conversation_id=conversation_id)
        out["cost_yuan"] = round(float(out["cost_yuan"]) + float(rv["cost_yuan"]), 4)
        for item in rv["issues"]:
            ids = "、".join(item["ids"]) or "全片"
            out["notes"].append(
                f"全片复核：{ids} {item['what']} → {item['fix']}")
    out["cost_yuan"] = round(out["cost_yuan"], 4)
    return out


#: 重写一镜最多几稿。与 `WRITER_ATTEMPTS` 分开：重写的起点是「已经能跑的一版 +
#: 评审意见」，2 稿足够；再多是把一条片子的预算花在同一个镜头上。
REWRITE_ATTEMPTS = 2


async def rewrite_shot_code(shot: dict[str, Any], *, critique: str,
                            spec: Mapping[str, Any], user_id: str = "",
                            conversation_id: str = "", llm: Any = None,
                            client: Mapping[str, Any] | None = None,
                            still: str | Path | None = None,
                            user_request: str = "",
                            references: Sequence[bytes] = ()) -> dict[str, Any]:
    """带评审意见重写**已经有一版 code** 的一镜 → ``{ok, note, cost_yuan, calls}``。

    与 `_one_shot` 共用同一份 system 与单镜任务块，只在末尾追加「上一版静帧的问题」。
    这里**允许覆盖**已有 code（`write_shot_code` 只补空 code）；闸① 不过或未判时
    **保留上一版**——用一段没被 lint 过的代码换掉一版能跑的代码，不是重写的本意。

    ``still`` 是上一版渲出来的那张静帧。**把图给写手看**（2026-10-18）：以前只给一句
    文字批评，写手要从「钱包是空方框」这句话里猜自己画成了什么样；真机上 34 镜被评审
    判红、按文字意见重写后仍有 26 镜红——它改的是自己想象的那张图，不是真正渲出来的那张。
    带上图之后，「描边线框」「深底深线」「几件并排」这些毛病它自己就看得见。
    不抛：调用方是出片前一环，一镜失败不该带塌整份。
    """
    from .selfcheck import _complete_with_identity
    from agent_framework.cost import cost_yuan

    client = client or rc.endpoints()
    sid = str(shot.get("id") or "?")
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    index = 0
    for i, row in enumerate(shots):
        if row is shot or str(row.get("id")) == sid:
            index = i
            break
    names = [str(f.get("name") or "") for f in (shot.get("fields") or [])
             if isinstance(f, Mapping) and f.get("name")]
    old = str(shot.get("code") or "").strip()
    system = _SYSTEM_HEAD + "\n" + _film_block(spec)
    ask = _request_block(user_request) + _shot_block(shot, index=index, spec=spec)
    ask += ("\n\n=== 上一版静帧的问题（美术评审看的是一张真渲出来的图） ===\n"
            + str(critique or "").strip())
    if old:
        ask += ("\n\n=== 上一版代码 ===\n```tsx\n" + old + "\n```\n"
                "照评审意见重写**完整的一镜代码**（不要只给补丁），只回代码块。")
    # 上一版静帧：让写手看见自己真正画出来的那一帧。读不出来就退回纯文字，
    # 不让一张缺图带塌这次重写。
    picture = ""
    if still:
        try:
            picture = base64.b64encode(Path(still).read_bytes()).decode("ascii")
        except OSError:
            picture = ""
    if references:
        ask += ("\n\n先附参考片静帧（目标画法）"
                + ("，最后一张是你上一版渲出来的静帧。" if picture else "。")
                + "对照参考看轮廓、比例、明暗、勾边光方向与构图；"
                "这一版要把图上的毛病改掉，不是改文字描述。")
    if picture and not references:
        ask += ("\n\n下面这张图就是你上一版渲出来的静帧。对照评审指出的地方看："
                "实体是不是只剩一圈线、细节糊没糊、几件东西是不是并排摆着；"
                "这一版要把图上的毛病改掉，不是改文字描述。")
    if picture:
        content: Any = [{"type": "text", "text": ask}]
        content += [{"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64,"
                                   + base64.b64encode(b).decode("ascii")}}
                    for b in references]
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{picture}"}})
    elif references:
        content = [{"type": "text", "text": ask}] + [
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,"
                           + base64.b64encode(b).decode("ascii")}}
            for b in references]
    else:
        content = ask
    messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": content}]
    spent = 0.0
    calls = 0
    last_errors: list[str] = []
    for attempt in range(1, REWRITE_ATTEMPTS + 1):
        try:
            resp = await _complete_with_identity(
                messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
                max_tokens=WRITER_MAX_TOKENS, timeout=WRITER_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 —— 一次调用炸了按重试算
            last_errors = [f"重写调用炸了（{type(exc).__name__}: {str(exc)[:160]}）"]
            continue
        calls += 1
        usage = getattr(resp, "usage", None)
        if usage:
            spent += cost_yuan(usage, model=str(usage.get("model") or ""))
        code = extract_code(str(getattr(resp, "content", "") or ""))
        if not code.strip():
            last_errors = ["重写这一趟没吐代码"]
            messages = messages + [
                {"role": "assistant", "content": "（空）"},
                {"role": "user", "content": "没有收到代码块。只回一段 ```tsx 代码块。"}]
            continue
        ok, errors, undetermined, _warns = await asyncio.to_thread(
            _lint, code, names, client)
        if ok:
            shot["code"] = code
            return {"ok": True, "note": "",
                    "cost_yuan": round(spent, 4), "calls": calls}
        if ok is None:
            # 未判不是通过：保留上一版能跑的代码，把原因如实交回去。
            return {"ok": False,
                    "note": f"{sid}：重写的闸① 未判（{undetermined}），保留上一版。",
                    "cost_yuan": round(spent, 4), "calls": calls}
        last_errors = errors
        messages = messages + [
            {"role": "assistant", "content": f"```tsx\n{code}\n```"},
            {"role": "user", "content": (
                "闸① lint 没过：\n" + "\n".join(f"· {e}" for e in errors[:12])
                + "\n照这些报错改，重写**完整的一镜代码**（不要只给补丁），只回代码块。")}]
    return {"ok": False,
            "note": f"{sid}：重写 {REWRITE_ATTEMPTS} 次仍没过：" + "；".join(last_errors[:2]),
            "cost_yuan": round(spent, 4), "calls": calls}
