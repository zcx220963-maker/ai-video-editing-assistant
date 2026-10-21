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

#: 写代码用哪个模型（2026-10-18）。空串＝跟随用户设置（``_resolve_request`` 解析出来的
#: 那一个）。写镜头代码是整条链里最吃模型能力的一步：同一个 prompt，强模型能画出分层、
#: 光影、笔触，弱模型会退化成「色块 + 文字」。要 A/B 就设环境变量
#: ``MOTION_WRITER_MODEL``（如 ``deepseek-v4`` / ``claude-opus-5-5``），**密钥仍按用户配**
#: ——这里只覆盖模型名，不改变身份与计费归属。
#:
#: 分工建议（参考教程里的模型梯队）：**模板由强模型写一次，逐镜可以交给 Flash**。
#: 视觉一旦被风格包 + 模板钉死（人物、光块、构图、字幕），弱模型只需要画这一镜的
#: 时代背景与代价动作，退化空间小得多。反过来，只有 Flash 还让它「从零发明风格」，
#: 出来的一定是色块加文字——那是提示词救不回来的，不要在这里加长 prompt 硬顶。
WRITER_MODEL = os.environ.get("MOTION_WRITER_MODEL", "").strip()

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
_MARKED_SECTIONS: tuple[str, ...] = (
    "subject-naming",
    "draw-parts",
    "motion-density-gate",
    "motion-anchors",
    "draw-floor",
)

_contract_cache: dict[str, tuple[float, str]] = {}


def _contract() -> str:
    """技能正文里与「写一镜代码」直接相关的那几段 + Remotion 写法手册。

    按两份文件的 mtime 缓存，改了任一一份就自动重读。手册
    （``remotion-rules.md``）是官方规则的摘编，与技能正文分开：正文讲**这条产品**
    要什么，手册讲**Remotion 本身**怎么写对。
    """
    path = cspec._SKILL_DIR / "SKILL.md"
    rules = cspec._SKILL_DIR / "remotion-rules.md"
    try:
        mtime = (path.stat().st_mtime, rules.stat().st_mtime)
    except OSError:
        return ""
    hit = _contract_cache.get("c")
    if hit and hit[0] == mtime:
        return hit[1]
    text = _read_skill()
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
    if rules_text:
        parts.append(rules_text)
    out = "\n\n".join(parts)
    _contract_cache["c"] = (mtime, out)
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
**如果整片设定里有一节「风格包」，以它为准**：它覆盖本技能里的默认建议——尤其是
构图、字幕位置、人物姿势、贯穿道具与配色。画面内容由你自己画；那一节里的模板是
可参考/可 import 的现成画法，没有「必须用哪一档」的零件表。那一节之外的做法照旧。

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
    # 风格包自己规定了贯穿道具（且要求每镜同一位置）时，这一段的「不要每镜硬塞」
    # 正好是反话——让位给风格包那一节，别把两种口径同时喂给写手。
    pack = spec.get("style_pack")
    manifest = pack.get("manifest") if isinstance(pack, Mapping) else None
    if isinstance(manifest, Mapping) and str(manifest.get("recurring_prop") or "").strip():
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


def _protagonist_block(spec: Mapping[str, Any], *,
                       template_subject: bool = False) -> str:
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
    # 这里不再规定「怎么画」。2026-10-20 用户的原话是「llm 自己明明有能力的，
    # 为什么非要限制模板」——背景一直是模型自己画的，人也一样；把常识写成规定
    # 只会挤占它的注意力。只剩风格包模板这一层：选了模板，主体就由模板渲染。
    draw = (
        "  模板已经给了这一镜主体的画法：用模板的组件，不要另套通用人物骨架。\n"
        "  模板里带步态的组件如果接受 `phase` / `strideSec` 这类参数，**相位同样按"
        "全片时钟取**：`phase={((filmStartSec + frame / fps) / strideSec) % 1}`"
        "（`filmStartSec` 从 `getInputProps()` 读）；其余 props 用模板缺省。"
        "每镜从 0 起算会在切点处换脚。\n"
        if template_subject else "")
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
        + draw
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


#: 系统注册的模板名 → (import 路径, 可用组件清单, 模板负责的那几件事)。用户不能上传
#: TSX，所以这张表就是全部可用的模板；表外的名字一律不 import（写了会过不了闸①，
#: 那是白烧一次）。第三项只用于提示词里的「模板负责…」，不参与任何分支判断——
#: 换模板不改代码，只加一行。
_STYLE_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "silhouette-history": (
        "@dg/lib/style/silhouette-history",
        "SilhouetteStage / GlowRect / SilhouetteWalk / EraInscription / StyleCaption",
        "人物剪影、贯穿光块、年代铭文、贴画面字幕",
    ),
    # 三维写实（2026-10-21）：舞台/灯光/相机/材质/调色都在模板里，镜头代码只摆实物。
    "cinema-studio": (
        "@dg/lib/style/cinema-studio",
        "StudioStage / ContactShadow / Scatter / Dust / GlassMaterial / MetalMaterial / "
        "CeramicMaterial / WoodMaterial / GranularMaterial / Mark / LineDraw / Screen",
        "台面、灯光、相机、景深、材质与调色——一镜一件实物的舞台",
    ),
}

#: 风格包 manifest 里值得写进提示词的字段 → 中文标签。顺序固定（前缀缓存靠这个）；
#: `recurring_prop` 不在这里——它由 `_prop_block` 统一说，避免同一句话出现两遍。
_STYLE_MANIFEST_LABELS: tuple[tuple[str, str], ...] = (
    ("palette", "配色"),
    ("composition", "构图与相机"),
    ("repeat_composition", "重复构图"),
    ("recurring_pose", "贯穿姿势"),
    ("recurring_prop", "贯穿道具"),
    ("caption", "字幕"),
    ("inscription", "铭文"),
    ("motion", "运镜与质感"),
    ("avoid", "不要出现"),
    # 用户在前端风格工作室里写给这一层的话（2026-10-21）。它属于**这一份风格的数据**，
    # 不是某套风格的代码分支：换一份包，这段话自然跟着换；空着就整行不出现。
    # `review_note` / `art_note` 不在这里——写手不该看到评审与导演的补充要求。
    ("writer_note", "用户补充：写给逐镜写手"),
)


def _style_manifest_value(value: Any) -> str:
    """manifest 的一项 → 一行可读文本。dict/list 用紧凑 JSON，顺序保持写入顺序。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "是" if value else "否"
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _layers_block(manifest: Mapping[str, Any]) -> list[str]:
    """画面拆解：每一层在这一条参考片里是什么、缺省怎么画、哪些字段会变。

    层名/字段名都是读片模型自己定的，这里**只按结构渲染，不解释键名**。
    没有 `layers` 的新旧风格包都返回空，输出逐字不变。
    """
    layers = manifest.get("layers")
    if not isinstance(layers, (list, tuple)) or not layers:
        return []
    out = ["=== 参考片的画面分层（说明这一镜该有哪些层；画法按这一镜的 brief）==="]
    for layer in layers[:16]:
        if not isinstance(layer, Mapping):
            continue
        name = str(layer.get("name") or layer.get("component") or "").strip()
        if not name:
            continue
        head = f"- {name}"
        comp = str(layer.get("component") or "").strip()
        if comp:
            head += f"（组件 `{comp}`）"
        out.append(head + "。")
        default = str(layer.get("default") or "").strip()
        if default:
            out.append(f"  缺省画法：{default[:240]}")
        fields = layer.get("fields")
        if isinstance(fields, Mapping) and fields:
            pairs = "；".join(f"{k}={v}" for k, v in list(fields.items())[:8])
            out.append(f"  这条片里的取值：{pairs[:320]}")
        varies = [str(v) for v in (layer.get("varies") or []) if str(v).strip()]
        if varies:
            out.append("  逐镜可换：" + "、".join(f"`{v}`" for v in varies[:8]))
    return out


def _timeline_block(manifest: Mapping[str, Any]) -> list[str]:
    """参考片的时间线：先什么、后什么、镜头怎么动、在哪里切。

    复刻的是**编排**，不是参考片的内容：每一段画什么由这一镜的 brief 定。
    """
    timeline = manifest.get("timeline")
    if not isinstance(timeline, (list, tuple)) or not timeline:
        return []
    out = ["=== 参考片时间线（把编排缩放到这一镜，内容按 brief 换）===",
           "`t` 是参考片秒数。顺序、节奏、运镜方式、切点质感照它复刻；"
           "具体画什么由这一镜的 brief 决定，不要把参考片的对象或文字写死。"]
    for i, seg in enumerate(timeline[:12], 1):
        if not isinstance(seg, Mapping):
            continue
        head = f"- 段 {i}"
        t = seg.get("t")
        if isinstance(t, (int, float)):
            head += f" @ t={float(t):.2f}s"
            dur = seg.get("duration")
            if isinstance(dur, (int, float)) and float(dur) > 0:
                head += f"（{float(dur):.2f}s）"
        bits = [head]
        for key, label in (("camera", "运镜"), ("transition", "切到下一段")):
            val = str(seg.get(key) or "").strip()
            if val:
                bits.append(f"{label}：{val}")
        out.append("；".join(bits) + "。")
        elements = seg.get("elements")
        if not isinstance(elements, (list, tuple)):
            continue
        for el in elements[:8]:
            if not isinstance(el, Mapping):
                continue
            name = str(el.get("name") or el.get("component") or "").strip()
            fields = el.get("fields")
            if isinstance(fields, Mapping) and fields:
                detail = "；".join(f"{k}={v}" for k, v in list(fields.items())[:6])
            else:
                detail = str(el.get("action") or el.get("state") or "").strip()
            if name or detail:
                out.append(f"  - {name}" + (f"：{detail[:200]}" if detail else ""))
    return out


def _free_style_lines(manifest: Mapping[str, Any]) -> list[str]:
    """自由命名的风格参数：分析里 LLM 自己定的键，原样列出。

    `_STYLE_MANIFEST_LABELS` 只是旧沉淀的常见键；新分析可能叫 `color_script`、
    `rhythm`、`texture`……按白名单过滤会把它们全丢掉。
    """
    known = {key for key, _ in _STYLE_MANIFEST_LABELS}
    # 写手只该看到自己那一层的话：评审/导演的补充要求在别的节点各自渲染，
    # 落到这里只会变成写手眼里的第二套口径。
    known |= {"layers", "timeline", "facets", "motion_contract", "template",
              "slots", "blocks", "variants", "review_note", "art_note"}
    out: list[str] = []
    for key, value in manifest.items():
        if key in known:
            continue
        got = _style_manifest_value(value)
        if got:
            out.append(f"{key}：{got[:600]}")
    return out


def _style_invariant_lines(manifest: Mapping[str, Any]) -> list[str]:
    """把沉淀数据里的「不变量」列成可执行条目。

    `facets.<visual|motion|text>.invariants` 与旧形状 `motion_contract.invariants`
    都是分析阶段从参考片量出来的：名字 + 取值（+ 可选 prop 绑定）。这里只做形状
    归一与去重，**不对任何一套风格写分支**——换一条参考片，条目自然跟着变。

    为什么单列：模板代码把参考片里的内容也一起冻住了，写手要按提示词换内容时，
    唯一可靠的边界就是这份「什么不能换」。不把它交给写手，它只能从模板代码里
    猜，于是要么不敢换，要么把风格一起换掉。
    """
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def take(prefix: str, holder: Any) -> None:
        if not isinstance(holder, Mapping):
            return
        for item in (holder.get("invariants") or [])[:16]:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()[:40]
            value = str(item.get("value") or "").strip()[:160]
            if not (name or value):
                continue
            prop = str(item.get("prop") or "").strip()[:40]
            key = (name, value)
            if key in seen:
                continue
            seen.add(key)
            rows.append((prefix, name, value + (f"（prop: {prop}）" if prop else "")))

    facets = manifest.get("facets") if isinstance(manifest.get("facets"), Mapping) else {}
    for key, label in (("visual", "画面"), ("motion", "运动"), ("text", "文字")):
        take(label, facets.get(key))
    take("运动", manifest.get("motion_contract"))
    if not rows:
        return []
    out = ["=== 风格不变量（跨镜一致；内容可以换，这些不能换）==="]
    for prefix, name, value in rows:
        out.append(f"- {prefix}·{name}：{value}" if name else f"- {prefix}：{value}")
    out.append("这些是沉淀分析从参考片量出来的，不是建议。逐镜换内容时照它们执行；"
               "不管用不用模板，都按这些自己画，**不许改这些**。")
    return out


def _style_template_usable(spec: Mapping[str, Any]) -> bool:
    """风格包是否**真的带来了一份能 import 的模板**（模块名已解析）。

    只有这时才说「主体由模板画，不要另起一个」；风格包写了模板名但库里没登记 /
    不可用时，写手照旧自己画。两者用同一把尺，否则会出现「正文说别用、代码检查
    说必须用」的对打。
    """
    pack = spec.get("style_pack")
    if not isinstance(pack, Mapping):
        return False
    tpl = pack.get("template")
    return isinstance(tpl, Mapping) and bool(str(tpl.get("module") or "").strip())


def template_required(spec: Mapping[str, Any]) -> bool:
    """风格包是否**显式要求**这一镜必须用模板。

    默认 False：画面由写手自己画。模板是从参考片沉淀出来的一份**现成画法**，
    写手可以按需 import 省事，也可以只当参照、照风格不变量自己画。只有风格包
    明说 `use_template: true`（或 `draw_mode: "template"`）时，它才是硬要求。

    为什么默认放开：把每个世纪、每种服饰、每件道具都写成组件的枚举，永远写不完；
    而「模板画不出」正是用户用自然语言改内容时最常见的情形（2026-10-19）。
    模板负责的是**风格不变量**（配色、构图、光位、节奏、版式），内容归 brief。
    """
    pack = spec.get("style_pack")
    if not isinstance(pack, Mapping):
        return False
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    mode = str(manifest.get("draw_mode") or "").strip().lower()
    if mode == "template":
        return True
    if mode == "self":
        return False
    flag = manifest.get("use_template")
    if isinstance(flag, bool):
        return flag
    return str(flag or "").strip().lower() in ("1", "true", "yes", "on")


def _style_pack_block(spec: Mapping[str, Any]) -> str:
    """风格包（`spec["style_pack"]`）。没有就返回空串——老片子逐字不变。

    为什么单列一大段：风格包是**用户选的整片契约**，它与技能正文里的默认建议冲突时
    必须赢。写手一次只看一镜，最容易被正文里的「相邻镜要变化」「自由组合媒介」带偏；
    这里把模板、贯穿项、禁区一次讲清，正文再说一遍也盖不过它。
    """
    pack = spec.get("style_pack")
    if not isinstance(pack, Mapping):
        return ""
    name = str(pack.get("name") or "").strip()
    if not name:
        return ""
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    display = str(pack.get("display") or name).strip()
    required = template_required(spec)
    lines = [f"=== 风格包：{display}（{name}）===",
             "本片视觉由这一节定；技能正文里的默认建议让位给这里——"
             "尤其是构图、字幕位置、人物姿势、贯穿道具与配色。",
             ("本片风格包要求这一镜使用模板（`use_template` / `draw_mode` 显式指定）；"
              "模板负责的部分照下面的 import 与 props 执行，你只画它没覆盖的内容。"
              if required else
              "**画面内容由你自己画**：主体是谁、什么外形、穿什么、拿什么、背景里"
              "有什么，都按这一镜的 brief 和下面的风格不变量写代码画出来。"
              "下面提到的模板只是参考片沉淀下来的一份**现成画法**，可以按需参考或 "
              "import 省事，不用它、整镜自己画也完全可以。不要把模板里的取值当成"
              "内容清单——世界上的事物写不成一张枚举表。")]
    # 优先用模板库登记（含用户沉淀的模板）：模块名与组件清单由服务端注入，写手
    # 不猜。模板库没这一条时才退回随镜像发布的内置登记表——老片子逐字不变。
    record = pack.get("template") if isinstance(pack.get("template"), Mapping) else {}
    template = str(manifest.get("template") or record.get("name") or "").strip()
    if template:
        module = str(record.get("module") or "").strip()
        comps = " / ".join(str(c) for c in (record.get("components") or []) if str(c).strip())
        known = _STYLE_TEMPLATES.get(template)
        if module:
            lines.append(
                (f"模板（本片必用）：`{template}`，从 `{module}` import；"
                 if required else
                 f"参考模板（**可选**）：`{template}`，模块 `{module}`；"
                 "想省事就从这里 import，不想用就照下面的不变量自己画。")
                + f"参考实现里的组件：{comps or '（见模板导出）'}。")
            usage = str(record.get("usage") or "").strip()
            if usage:
                lines.append(
                    ("组件 props（本片必用，照这个传参，不要猜）：\n" if required
                     else "如果选择用这套参考实现，props 这样传（自己画就忽略）：\n")
                    + usage)
                # 正常路径下 usage（`style_templates.component_usage`）末尾已经带了
                # 单位契约；只有手工/旧记录没有时才补一句，避免同一段出现两遍。
                if "画面比例 0–1" not in usage:
                    # 写手的通用规则是「坐标单位就是设备像素」（见 `_shot_system`），
                    # 模板组件的约定恰好相反：props 收画面比例，内部自己乘 width/height。
                    # 真机 2026-10-19 的三镜正是把 `x={W*0.32}`、`groundY={H*0.82}`
                    # 传进了比例参数，人物与舞台被乘了第二遍、推出画外。
                    lines.append(
                        "单位边界：上面这些 props 里的位置/尺寸（x、y、groundY、"
                        "heightRatio、pan、size 等）是**画面比例 0–1**，不是像素——"
                        "`x={0.32}`、`groundY={0.82}`、`pan={0.02}`。"
                        "你自己的 `<svg>` 仍按设备像素画。颜色一律 `#rrggbb`。")
            # 可选 prop 的缺省值是**参考片那一镜的内容**，不是空。真机 2026-10-19
            # 菊花镜：`<StyleCaption>` 没传 text，预览左上角显示参考片台词
            # 「到山西，下雪了。新靴，还是没舍得穿。」。这条对任何模板都成立：
            # 模板是从参考片沉淀出来的，缺省值就是那一段的内容。
            lines.append(
                "**不传 ≠ 空**：这些 props 的缺省值来自参考片那一镜（文字组件不传 "
                "`text` 就会显示参考片原文）。每一镜必须显式传这一镜的值；"
                "这一镜不要的零件就不要渲染那个组件，也不要用 0 / 空串 / 透明"
                "把它藏起来——藏起来的零件仍然是参考片的一部分。")
            responsible = str(record.get("responsible") or "").strip().rstrip("。.；;，,")
            if required:
                lines.append(f"模板负责：{responsible or '画面的一致性；具体见模板导出'}。"
                             "你只画这一镜的时代背景与代价动作；**不要另画第二个人、"
                             "第二块光、第二条字幕**。"
                             "**这一镜的代码必须真的 import 上面那个模块并渲染它的组件**——"
                             "只读文字然后在代码里自己重画一遍，等于没有用风格模板。"
                             "但这一镜的 brief 明确说不要某个零件时（例如「画面里不要人」），"
                             "就不要渲染那个组件——不要用 0 / 空串 / 透明把它藏进来，"
                             "那仍然是参考片的一部分。")
            else:
                lines.append(
                    f"参考实现里的分工：{responsible or '画面的一致性；具体见模板导出'}。"
                    "**这一镜由你自己画**：照下面的风格不变量把主体、服装、道具、背景"
                    "与文字画出来。如果你选择 import 模板里的某件组件，那一件就不要再"
                    "自己画第二份（同一个人/光/字幕只画一次）；不 import、整镜自己画"
                    "也完全可以。brief 明确说不要的东西（例如「画面里不要人」）就不要画——"
                    "不要用 0 / 空串 / 透明把参考片里的缺省零件藏进来。")
            # 模板组件是**参考片内容的默认画法**，不是内容的上限。用户用自然
            # 语言换内容（主体、背景、道具、文字）时，模板的取值表经常画不出；
            # 允许写手照风格不变量自己画那一件，是把提示词接回画面的唯一通道。
            # 不变量由沉淀数据定义，不是后端为某套风格写死的。
            lines.append("参考实现里的取值（枚举）只是它**已经画好的档位**，不是内容清单，"
                         "也不是你能画的东西的上限。这一镜的内容（对象、外形、场景、"
                         "服饰、道具、文字）不在取值里时，照下面的**风格不变量**自己画"
                         "这一件——不要为了套一个取值去改 brief。"
                         "同一件东西（主体、光块、字幕）只画一次：换的是它的内容，"
                         "不是再加一份。这是内容按提示词变，不是换风格。")
        elif known:
            lines.append(
                (f"模板（本片必用）：`{template}`，从 `{known[0]}` import；"
                 f"参考实现里的组件：{known[1]}。")
                if required else
                (f"参考模板（**可选**）：`{template}`，模块 `{known[0]}`；"
                 f"参考实现里的组件：{known[1]}。可以 import 来用，也可以只照它的"
                 "剪影语法自己画。"))
            duty = known[2] if len(known) > 2 else "画面的一致性；具体见模板导出"
            if required:
                lines.append(f"模板负责：{duty}。"
                             "你只画这一镜的主体与它在画面里的动作；**不要另起第二套"
                             "舞台、第二块主光、第二条字幕**。"
                             "**这一镜的代码必须真的 import 上面那个模块并渲染它的"
                             "组件**——只读文字然后在代码里自己重画一遍，等于没有用"
                             "风格模板。")
            else:
                lines.append(f"参考实现里的分工：{duty}——这些是参考片的画法，"
                             "不是这一镜必须有的零件。**这一镜由你自己画**，照风格"
                             "不变量画主体、光块、文字与背景；import 其中某件就不要"
                             "自己再画第二份。")
            lines.append("**铭文与字幕的缺省值是参考片原文**——如果你用模板的文字组件，"
                         "每一镜必须显式传这一镜自己的文字；不传就会显示参考片那句。"
                         "这一镜不需要那块字，就不要渲染那个组件。")
        else:
            lines.append(f"模板「{template}」未在模板库登记，**不要 import 它**；"
                         "按下面的正文与 manifest 自己画，整片保持一致。")
    # 槽位：模板把每个导出组件都做成可替换零件。本片选了哪一件，在这里写死 import
    # 路径——写手不必知道「为什么换」；它只要别再去 import 基模板的那个组件。
    slots = [s for s in (record.get("slots") or []) if isinstance(s, Mapping)]
    if slots:
        lines.append("=== 槽位（模板的每个导出组件都是一个可替换零件；**可选**）==="
                     if not required else
                     "=== 槽位（模板的每个导出组件都是一个可替换零件）===")
        for s in slots[:12]:
            comp = str(s.get("component") or "").strip()
            if not comp:
                continue
            label = str(s.get("label") or comp).strip()
            bound = s.get("bound") if isinstance(s.get("bound"), Mapping) else {}
            module = str(bound.get("module") or "").strip()
            if module:
                lines.append(
                    f"- `{comp}`（{label}）→ 本片登记了替换件 `{bound.get('name') or ''}`"
                    + (f"「{bound.get('label')}」" if bound.get("label") else "")
                    + (f"：从 `{module}` import 同名导出 `{comp}` 来渲染，"
                       "**不要 import 基模板里的那一个**。"
                       if required else
                       f"：如果你选择用这个零件，从 `{module}` import 同名导出 "
                       f"`{comp}` 来渲染（不要 import 基模板里的那一个）；"
                       "不想 import、自己画这一件也完全可以。")
                    + "这一镜要的内容不在它的取值里时，照风格不变量自己画这一件"
                      "（见上面的规则）。")
                # 替换件可以给联合类型加取值（更多时代/场景/形态）。写手看不到
                # 替换件源码，这里必须把「这件能接受哪些值」交给它，否则逐镜
                # 只会沿用基模板那一档，用户的时代线推不动。
                contract_text = str(bound.get("contract_text") or "").strip()
                if contract_text:
                    lines.append(
                        (f"  `{comp}` 这件接受的取值（每一镜按时代/场景传不同的值，"
                         f"只传这里列出的取值）：{contract_text[:700]}"
                         if required else
                         f"  `{comp}` 这件已经画好的档位：{contract_text[:700]}"
                         "。这些只是参考实现的枚举，不是内容清单；这一镜要的东西"
                         "不在里面时，按风格不变量自己画。"))
            else:
                variants = [v for v in (s.get("variants") or [])
                            if isinstance(v, Mapping)]
                names = "、".join(
                    f"「{v.get('label') or v.get('name')}」" for v in variants[:6])
                lines.append(
                    f"- `{comp}`（{label}）："
                    + ("本片默认用模板自带的那一个" if required else
                       "如果你选择用模板，默认用它的这一件")
                    + (f"；已登记可换的替换件：{names}" if names else "")
                    + ("。提示里没点名换件就不要换；" if required else
                       "；提示里没点名换件就不要换；不 import、自己画也可以；")
                    + "这一镜要的内容它画不出时，照风格不变量自己画这一件。")
                # 模板自带件的内容取值：不写出来，写手只会沿用参考片那一档。
                # `values` 是能原样传给枚举 prop 的字面量；`reference` 只是参考片
                # 里的中文语义（「山峦」「军装」）。混着说，写手会把中文当参数值
                # 传进去，组件落到缺省分支，画面错了还不报错。
                bits: list[str] = []
                for c in (s.get("content") or []):
                    if not isinstance(c, Mapping):
                        continue
                    prop = str(c.get("prop") or "")
                    values = [str(x) for x in (c.get("values") or []) if str(x).strip()]
                    ref = [str(x) for x in (c.get("reference") or []) if str(x).strip()]
                    if values:
                        bit = f"`{prop}` 可传 {' / '.join(values)}"
                        if ref:
                            bit += f"（参考片里对应：{' / '.join(ref)}）"
                        bits.append(bit)
                    elif ref:
                        bits.append(f"`{prop}` 参考片用过 {' / '.join(ref)}"
                                    "（按这一镜的需要传合法值）")
                if bits:
                    lines.append(
                        f"  这个零件已经画好的档位：{'；'.join(bits)}。"
                        + ("每一镜按分镜选合适的一档，不要整片写死；"
                           if required else
                           "如果你用这个零件，每一镜按分镜选合适的一档；"
                           "它没有这一镜要的东西就照不变量自己画；")
                        + "括号里的中文只是参考片的叫法，传参时只用「可传」后面的取值。")
            # 文字槽位（主字幕 / 日期铭文）：内容 prop 不传就显示参考片原文。
            # 组件名与 prop 名从沉淀数据取，不写某一套模板的专有名。
            text_kind = str(s.get("text_kind") or "").strip()
            content_prop = str(s.get("content_prop") or "").strip()
            if text_kind and content_prop:
                role = {"caption": "这一镜的主字幕",
                        "inscription": "这一镜的日期/地点铭文"}.get(
                            text_kind, f"文字（{text_kind}）")
                lines.append(
                    (f"  `{comp}` 是{role}：`{content_prop}` 是**内容**，渲染它就必须"
                     f"显式传这一镜自己的文字；不传会沿用参考片原文。"
                     "这一镜不需要这块字，就不要渲染这个组件。"
                     if required else
                     f"  参考实现里 `{comp}` 是{role}：如果你用它，`{content_prop}` 是"
                     f"**内容**，必须显式传这一镜自己的文字（不传会沿用参考片原文）；"
                     "自己画这块字也可以，位置/字号/颜色照风格不变量。"))
                if text_kind == "caption":
                    # 2026-10-19 真机 13 镜全部同时渲染了模板的 `StyleCaption` 和
                    # 框架的 `<CaptionText/>`：两件都画字，左上角白字与橙字叠成重影。
                    # 模板的主字幕组件就是这一条字幕本身，不能再套第二件。
                    lines.append(
                        f"  **字幕只留一条**：渲染 `{comp}` 时**不要**再摆 "
                        "`<CaptionText/>`、`<CaptionLayer/>` 或 `<CaptionFrame>`——"
                        "它们都会再画一遍同一句，同屏就是重影。这一镜的字只写进 "
                        f"`{content_prop}`。")
        lines.append("换件的边界：替换件只换**这个组件画的东西**，外壳（构图、相机、"
                     "时间轴、字幕版式、配色）仍按基模板；它的 props 合约与基模板"
                     "同名组件相同，照上面的 props 传参即可。"
                     + ("" if required else "不换件、整镜自己画，也在这条边界之内。"))
        # 固定的是画法，不是内容：每一镜画的是山还是平地、士兵还是古人、哪句话，
        # 由写手按分镜传值。不明说这一句，写手会把「模板没点名换件」理解成
        # 「整片只能保持参考片那一档」，用户的时代线就推不动。
        lines.append("每个零件分两件事：**风格不变量**（配色、构图、光位、笔法、"
                     "运动节奏、字号与版式）跨镜固定；**内容**（对象是谁、外形、"
                     "场景、服饰、道具、文字）由你逐镜按 brief 决定，不要整片写死"
                     "成参考片里的同一档。参考实现的取值只是**已有档位**，不是"
                     "内容清单；内容不在取值里时，照风格不变量自己画，"
                     "别为了迁就取值而改内容。")
    lines.extend(_style_invariant_lines(manifest))
    for key, label in _STYLE_MANIFEST_LABELS:
        if key not in manifest:
            continue
        got = _style_manifest_value(manifest.get(key))
        if got:
            # 自由文本（*_note）是用户手写的，长度不受结构字段那套约束；
            # 截断是为了别让一段几千字的随笔把整镜的提示词挤掉，不是拦。
            lines.append(f"{label}：{got[:1200]}")
    # 新沉淀没有固定字段：风格参数、层、时间线都由读片模型自己命名。这三段按
    # **结构**渲染，不按某套风格的键名分支；没有它们的旧风格包输出逐字不变。
    lines.extend(_free_style_lines(manifest))
    lines.extend(_layers_block(manifest))
    lines.extend(_timeline_block(manifest))
    # 参考片的「动」契约：沉淀时把「什么变、什么不变」分开存下来。这里原样交给
    # 写手，而不是靠某套风格的固定字段——变化轴是分析出来的数据，不是后端写死的。
    contract = manifest.get("motion_contract")
    if isinstance(contract, Mapping):
        variations = [v for v in (contract.get("variations") or [])
                      if isinstance(v, Mapping) and v.get("prop")]
        if variations:
            lines.append("参考片里**会变**的维度（每一镜按时代/场景给不同的值，"
                         "**不能写死成其中一档**）：")
            for v in variations[:12]:
                values = " / ".join(str(x) for x in (v.get("values") or []) if str(x).strip())
                cadence = str(v.get("cadence") or "").strip()
                lines.append(
                    f"- {str(v.get('name') or v.get('prop'))}：用组件里收 "
                    f"`{v.get('prop')}` 这个 prop 的那一件来传"
                    + (f"；参考片里出现过：{values}" if values else "")
                    + (f"；变化节奏：{cadence}" if cadence else "")
                    + "。")
            lines.append("把这些变化**真的落进每一镜**；组件对这个 prop 声明了固定"
                         "取值（联合类型）时，传组件列出的取值，上面的词只作对应关系；"
                         "模板没有这个 prop 时按下面的兜底，不要为了迁就组件把变化轴"
                         "压成固定值。")
        cont = contract.get("continuity")
        if isinstance(cont, Mapping):
            subject = str(cont.get("subject_motion") or "").strip()
            if subject == "continuous":
                lines.append("连续性：参考片的主体**整片一直在动**——每一镜都保持走动/运动，"
                             "不要在镜中途停下；切点处的位置与相位尽量接得上。")
            elif subject == "stop_and_go":
                lines.append("连续性：参考片的主体是**走一段、停一下**的节奏；"
                             "停要停得干净，但别把它画小、画丢。")
            if str(cont.get("phase") or "").strip() == "global":
                lines.append("步态相位按**全片时钟**算（`filmStartSec + frame/fps`，"
                             "`filmStartSec` 从 `getInputProps()` 取），"
                             "不要每镜从 0 起，也不要写死估算的起点。")
    # 沉淀的三个角度：审美 / 动作编排 / 文字组件。参考片是**逐帧连续**的一条片子，
    # 而我们是逐镜写、逐镜烧、最后拼接——不把「哪一层属于整片、哪一层属于这一镜」
    # 说死，写手就会在每一镜里把主体、字幕各重画一遍，切点处人物换脚、停下，
    # 字幕每镜一个样。facets 是沉淀时从参考片里量出来的，不是后端的某套风格分支。
    facets = manifest.get("facets")
    if isinstance(facets, Mapping) and facets:
        lines.append("=== 沉淀下来的三层（模板已按这三层实现，你按边界画）===")
        visual = facets.get("visual")
        if isinstance(visual, Mapping):
            comps = " / ".join(f"`{c}`" for c in (visual.get("components") or []))
            lines.append(
                "- **画面层（visual）**" + (f"：由 {comps} 负责。" if comps else "：")
                + "配色、构图、质感、光位按上面的 manifest；这一层整片不变，"
                  "变的是背景时代、光色、道具种类；brief 明确不要的零件这一镜不渲染"
                  "（不画、也不用空值藏起来，见这一镜的 brief）。")
            vs = [v for v in (visual.get("variations") or [])
                  if isinstance(v, Mapping) and v.get("prop")]
            if vs:
                lines.append("  画面层里会变的维度："
                             + "；".join(
                                 f"`{v.get('prop')}`（{v.get('name') or ''}"
                                 + ("：" + " / ".join(str(x) for x in v.get("values") or [])
                                    if v.get("values") else "") + "）"
                                 for v in vs[:8]) + "。")
        motion = facets.get("motion")
        if isinstance(motion, Mapping):
            tracks = [t for t in (motion.get("tracks") or []) if isinstance(t, Mapping)]
            film_tracks = [t for t in tracks if t.get("scope") == "film"]
            shot_tracks = [t for t in tracks if t.get("scope") != "film"]
            if film_tracks:
                lines.append(
                    "- **整片连续层（motion · scope=film）**：这一层**贯穿整片**，"
                    "不是每镜各画一份。逐镜代码的职责是把它按**全片时钟**驱动：")
                for t in film_tracks[:6]:
                    comp = str(t.get("component") or "").strip()
                    clock = str(t.get("clock") or "filmTimeSec").strip()
                    lines.append(
                        f"  - `{comp}`：传 `{clock}={{filmStartSec + frame / fps}}`，"
                        f"`filmStartSec` 从 `getInputProps()` 取"
                        f"（`const {{ filmStartSec = 0 }} = getInputProps();`）。"
                        "**不要在镜头代码里从 0 重算相位或位置**——这一镜是整片时间轴"
                        "上的一段，切点处的步态与位移要接得上。")
            if shot_tracks:
                lines.append(
                    "- **每镜场景层（motion · scope=shot）**："
                    + " / ".join(f"`{t.get('component')}`" for t in shot_tracks[:6]
                                 if t.get("component"))
                    + "，可以按本镜 0 帧算；这一层随时代/场景换。")
            camera = str(motion.get("camera") or "").strip()
            cuts = " / ".join(str(c) for c in (motion.get("transitions") or [])
                              if str(c).strip())
            if camera or cuts:
                lines.append("  运镜/转场：" + "；".join(
                    x for x in (camera, ("切点用 " + cuts) if cuts else "") if x) + "。")
        text = facets.get("text")
        if isinstance(text, Mapping):
            items = [i for i in (text.get("items") or []) if isinstance(i, Mapping)]
            if items:
                lines.append(
                    ("- **文字层（text）**：这一镜的字必须交给模板的文字组件，"
                     "**不要自己画第二条字幕、也不要另起一套字体**："
                     if required else
                     "- **文字层（text）**：参考实现用这些组件画字（位置/字体/字号"
                     "照它们；你自己画这块字也可以，但**只有一条字幕、不要另起一套"
                     "字体**）："))
                for i in items[:6]:
                    comp = str(i.get("component") or "").strip()
                    prop = str(i.get("content_prop") or "").strip()
                    pos = str(i.get("position") or "").strip()
                    typo = str(i.get("typography") or "").strip()
                    lines.append(
                        f"  - `{comp}`（{i.get('kind') or 'caption'}）"
                        + (f"，内容传给 `{prop}`" if prop else "")
                        + (f"，位置 {pos}" if pos else "")
                        + (f"，排版 {typo}" if typo else "") + "。")
    # 姿态是**一条跨帧的事实**，静帧看不出来：只写「侧身行走的剪影」时，写手很容易
    # 交一张站着的人（2026-10-19 真机三镜全是常量 figX）。这里把「逐帧动」说死。
    if str(manifest.get("recurring_pose") or "").strip():
        # 主语是 **brief 定的主体**，不是默认的「人」：真机 2026-10-19 菊花镜，
        # 这句原文「人物/主体必须逐帧动起来」被评审后的重写理解成「必须有人」，
        # 把整片唯一的主体换回了人影。参考片的贯穿姿势是数据，但这一镜画的是谁
        # 由 brief 决定；brief 说不要人，动的就是花、器物或光。
        lines.append("**这一镜的主体必须逐帧动起来**（主体是 brief 定的那一个；"
                     "brief 说画面里不要人时，动的就是花/物件，不是人）：用模板里"
                     "承担主体的组件，或自己按 `useCurrentFrame()` 算运动；"
                     "**不要交一张静止的画面**——静帧看着对，成片里是不动的。"
                     "主体位置也别钉死：模板若有位移用的 props（`x` / `pan` 之类），"
                     "把它算成帧的函数，让主体在镜内真的动起来。"
                     "**默认整镜都在动**：没有明确写「停下」的 brief，就别传 "
                     "`walking={false}`、也别写中途停下的条件。"
                     "需要位移时 `facing` 必须和方向一致（朝右＝x 增大）。")
    body = str(pack.get("body") or "").strip()
    if body:
        lines.append("")
        lines.append("风格包正文：")
        lines.append(body)
    return "\n".join(lines) + "\n"


def template_import_missing(spec: Mapping[str, Any], code: str) -> str:
    """风格包**显式要求**用模板、代码却没 import 它 → 返回可交给模型的重写说明。

    空串＝没要求或已经 import 了。默认没要求：画面由写手自己画（见
    `template_required`），模板只是可选参考。只有 `manifest.use_template: true`
    或 `draw_mode: "template"` 时，这里才做代码级的事实检查——不靠模型自报，
    量的是代码里到底有没有那一条 import。
    """
    if not template_required(spec):
        return ""
    pack = spec.get("style_pack")
    if not isinstance(pack, Mapping):
        return ""
    tpl = pack.get("template") if isinstance(pack.get("template"), Mapping) else None
    if not tpl:
        return ""
    module = str(tpl.get("module") or "").strip()
    if not module:
        return ""
    # 槽位换件的模块也要 import：换了零件的那一镜若照旧只 import 基模板，
    # 画面里仍是默认零件，换件等于没发生。
    bounds = [(str(s.get("component") or ""), str((s.get("bound") or {}).get("module") or ""))
              for s in (tpl.get("slots") or [])
              if isinstance(s, Mapping) and isinstance(s.get("bound"), Mapping)
              and str((s.get("bound") or {}).get("module") or "").strip()]
    comps = [str(c) for c in (tpl.get("components") or []) if str(c).strip()]
    listed = " / ".join(comps)
    # 要的是**真 import 语句**：`from '@dg/…'` / `import '@dg/…'` / `import('@dg/…')`。
    # 注释先剥掉：只查子串会被一条「// TODO: import …」骗过去，然后这镜照样自己重画。
    bare = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", code or "", flags=re.S))

    def _imported(spec_module: str) -> bool:
        return bool(re.search(
            r"(?:\bfrom\s*|\bimport\s*\(?\s*)['\"]" + re.escape(spec_module) + r"['\"]",
            bare))

    if not _imported(module):
        return ("这一镜**没有 import 风格模板** `" + module + "`"
                + (f"（可用组件：{listed}）" if listed else "")
                + "。风格不变量要靠它落地；内容可以按 brief 换，但模板负责的部分"
                "不能整份丢掉（brief 明确不要的零件除外）。重写**完整的一镜代码**："
                "import 模板，用它的组件渲染这一镜不需要换的部分；要换的那一件"
                "照风格不变量自己画。只回代码块。")
    for comp, vmod in bounds:
        if not _imported(vmod):
            return (f"这一镜没有 import 本片的替换件 `{vmod}`（槽位 `{comp}`）。"
                    "本片这个零件已经换成了另一份实现；从基模板 import 同名组件会画回默认零件，"
                    f"换件等于没发生。重写**完整的一镜代码**：`{comp}` 从 `{vmod}` import。"
                    "只回代码块。")
    # import 了但一个组件都没用：`import * as T` 放那儿、画面还是自己重画。
    if comps and not any(c in bare for c in comps):
        return ("这一镜 import 了风格模板 `" + module + "`，但代码里**没有用到它的任何组件**"
                f"（{listed}）。风格包要的不是一条 import：用模板组件渲染这一镜"
                "不需要换的部分；要换的那一件照风格不变量自己画。"
                "重写**完整的一镜代码**，只回代码块。")
    # 主角组件必须真的渲染出来。2026-10-20 真机：镜头 import 了模板、也用了舞台与
    # 光块，唯独把人又手画了一遍——深底 + 深墨，成片里只剩一圈勾边光。风格包在
    # protagonist.component 里点名了主角组件时，这里量的是代码里有没有这个组件。
    prot = spec.get("protagonist")
    want = (str(prot.get("component") or "").strip()
            if isinstance(prot, Mapping) else "")
    if want:
        body = re.sub(r"(?m)^\s*import\b.*$", "", bare)
        if not re.search(r"\b" + re.escape(want) + r"\b", body):
            return (f"这一镜没有渲染主角组件 `{want}`——模板 import 了，人却另画了一遍。"
                    "同一个主角要靠这个组件贯穿全片；衣服/发型/持物用它的 `outfit` "
                    "参数换。重写**完整的一镜代码**，只回代码块。")
    return ""


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
    listing = "\n".join(
        f"{s.get('id') or '?'}：{str(s.get('text') or '').strip()[:60]}" for s in shots)
    # 重复构图：风格包要求全片同一构图时，「相邻镜要变化」这句必须让位——两句话同时
    # 出现，模型会照更近、更熟悉的技能正文办，于是每镜换机位，「刻意重复」就没了。
    pack = spec.get("style_pack") if isinstance(spec.get("style_pack"), Mapping) else {}
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    repeat = bool(manifest.get("repeat_composition")) if isinstance(manifest, Mapping) else False
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
        + _protagonist_block(spec,
                             template_subject=_style_template_usable(spec))
        + _style_pack_block(spec)
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
        + "=== 技能正文 ===\n"
        f"{_contract()}\n"
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
        pack = spec.get("style_pack")
        if isinstance(pack, Mapping):
            manifest = pack.get("manifest")
            if isinstance(manifest, Mapping):
                raw = manifest.get("reference_materials")
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
    from agent_framework import style_authoring as sa

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


def _shot_block(shot: Mapping[str, Any], *, index: int) -> str:
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
        f"{motion_text}"
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
          timeout: int = 60) -> tuple[bool | None, list[str], str]:
    """→ (过没过 / None=未判, 错误行, 未判原因)。"""
    lint = client.get("lint")
    if lint is None:
        return None, [], "渲染容器没有 lint 口"
    try:
        got = lint(code, list(names))
    except Exception as exc:  # noqa: BLE001 —— 外呼失败＝未判，不是通过
        return None, [], f"{type(exc).__name__}: {str(exc)[:160]}"
    if not isinstance(got, Mapping) or "ok" not in got:
        return None, [], "lint 响应里没有 ok 键"
    if got.get("ok"):
        return True, [], ""
    return False, [_fmt_lint(e) for e in (got.get("errors") or [])], ""


def _motion_feedback(account: Mapping[str, Any], *, weak: bool = False) -> str:
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
    parts.append(
        f"判据：≥{want if isinstance(want, int) else 2} 段先后变化，相邻段起点"
        f"≥{gap if gap is not None else 0.4}s。线稿本身整幅平均差只有 0.05~0.3，"
        "要过 1.5 必须做**满宽**的动：满宽色带擦入、或满宽面板自上/下展开/收拢。"
        "做两处，第二处起点比第一处晚 ≥0.4s（最好 1s 以上）；别把块面放进字幕安全区。"
        "**第二处要和第一处一样满**：满宽色带实测能到 10 以上，三分之一宽的小面板只有 1 左右"
        "（差一点就是过不了）——两处都用满宽，只是高度不同（如 6% 与 20%）、进场方向不同。"
        "只让细线移动、只做淡入淡出，读数到不了地板线。")
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
        return False, _motion_feedback(account), account
    if status == "ok":
        # 压线的「过」不算过：草稿档与成片档的像素基数不同，1.6 这种读数会在成片档
        # 掉到 1.5 以下（真机 s03 实测 1.63 → 1.484）。留出余量再收。
        th = account.get("thresholds") if isinstance(account.get("thresholds"), Mapping) else {}
        floor = th.get("diff_min")
        peaks = [e.get("peak_diff") for e in (account.get("events") or [])
                 if isinstance(e, Mapping) and isinstance(e.get("peak_diff"), (int, float))]
        if isinstance(floor, (int, float)) and peaks:
            if min(peaks) < float(floor) * WRITER_MOTION_MARGIN:
                return False, _motion_feedback(account, weak=True), account
        return True, "", account
    return None, (f"闸⑭ 未判：{account.get('reason') or '容器没给出理由'}。"
                  "代码已收下，**这不是通过**。"), account


async def _one_shot(shot: dict[str, Any], *, index: int, system: str,
                    spec: Mapping[str, Any], llm: Any, user_id: str,
                    conversation_id: str, client: Mapping[str, Any],
                    user_request: str = "",
                    references: Sequence[bytes] = (),
                    exemplar: str = "",
                    check_motion: bool = True,
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
    ask = (_request_block(user_request) + _shot_block(shot, index=index)
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
    for attempt in range(1, WRITER_ATTEMPTS + 1):
        try:
            extra: dict[str, Any] = {}
            if WRITER_MODEL:
                extra["model_override"] = WRITER_MODEL
            resp = await _complete_with_identity(
                messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
                max_tokens=WRITER_MAX_TOKENS, timeout=WRITER_TIMEOUT_SEC, **extra)
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
        ok, errors, undetermined = await asyncio.to_thread(
            _lint, code, names, client)
        if ok is None:
            # 未判**不是通过**，但对这一镜来说也没有可行动的报错：照收，让入库前的闸①
            # 再判一次。把它写成「失败」只会让模型重写一份同样没被量过的代码。
            shot["code"] = code
            return (shot, f"{sid}：闸① 未判（{undetermined}）。代码已收下，**这不是通过**。",
                    spent, attempt, motion)
        if ok:
            # 风格包**显式要求**用模板时，「import 它」才是这一镜的硬要求（默认
            # 不要求：画面由写手自己画，模板是可选参考）。要求了的话，模型只读到
            # 文字时最容易把描述自己重画一遍；先用一次重写把它掰回来，预算用完
            # 仍不 import 就照收并记账（堵死比白烧更坏）。
            missed = template_import_missing(spec, code)
            if missed:
                if attempt < WRITER_ATTEMPTS:
                    last_errors = [missed]
                    messages = messages + [
                        {"role": "assistant", "content": f"```tsx\n{code}\n```"},
                        {"role": "user", "content": missed}]
                    continue
                shot["code"] = code
                return (shot, f"{sid}：{missed}\n"
                              "  · 写了多次仍未照做，照收这一稿——"
                              "**这一镜没有按风格包出画 / 连贯性没有守住的保证**。",
                        spent, attempt, motion)
            if not check_motion:
                shot["code"] = code
                return shot, "", spent, attempt, motion
            # 闸① 过了还不够：闸⑭ 判的是像素。这一版草稿渲出来数一遍——数得过就收，
            # 数不过就把**它自己数到的段与间隔**交给模型重写（比只在正文里写「做两处
            # 大块面的动」有用得多：真机 spec5 照着正文写，三镜仍然全红）。
            passed, motion_note, account = await _motion_check(
                shot, spec=spec, code=code, client=client, sid=sid)
            motion = account or motion
            if passed:
                shot["code"] = code
                return shot, "", spent, attempt, motion
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
                                check_motion=do_check)
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
            results = await asyncio.gather(*(run(i, s, exemplar=exemplar)
                                             for i, s in todo))
        else:
            first_i, first_shot = todo[0]
            first = await run(first_i, first_shot)
            exemplar = str(first_shot.get("code") or "")
            rest = await asyncio.gather(*(run(i, s, exemplar=exemplar)
                                          for i, s in todo[1:]))
            results = [first, *rest]
    else:
        results = await asyncio.gather(*(run(i, s) for i, s in todo))
    for shot, note, cost, calls, motion in results:
        sid = str(shot.get("id") or "?")
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
    ask = _request_block(user_request) + _shot_block(shot, index=index)
    ask += ("\n\n=== 上一版静帧的问题（美术评审看的是一张真渲出来的图） ===\n"
            + str(critique or "").strip())
    if old:
        ask += ("\n\n=== 上一版代码 ===\n```tsx\n" + old + "\n```\n"
                "照评审意见重写**完整的一镜代码**（不要只给补丁），只回代码块。")
        # 上一版没 import 模板时，把「这一版必须 import」写在重写要求里——
        # 评审常只说「画面漂了」，不说「你没用模板」；不说破，重写会再漂一次。
        if template_import_missing(spec, old):
            ask += ("\n**这一版必须 import 风格包里的模板模块**（见上面的风格包一节），"
                    "用它的组件渲染主体/光块/铭文/字幕；不要自己重画一遍。")
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
            extra: dict[str, Any] = {}
            if WRITER_MODEL:
                extra["model_override"] = WRITER_MODEL
            resp = await _complete_with_identity(
                messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
                max_tokens=WRITER_MAX_TOKENS, timeout=WRITER_TIMEOUT_SEC, **extra)
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
        ok, errors, undetermined = await asyncio.to_thread(_lint, code, names, client)
        if ok:
            # 评审重写也可能顺手把模板丢了：上一版就 import、这一版自己重画。
            # 与 `_one_shot` 同一把尺，只是预算更小（REWRITE_ATTEMPTS）。
            missed = template_import_missing(spec, code)
            if missed and attempt < REWRITE_ATTEMPTS:
                last_errors = [missed]
                messages = messages + [
                    {"role": "assistant", "content": f"```tsx\n{code}\n```"},
                    {"role": "user", "content": missed}]
                continue
            shot["code"] = code
            return {"ok": True, "note": missed,
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
