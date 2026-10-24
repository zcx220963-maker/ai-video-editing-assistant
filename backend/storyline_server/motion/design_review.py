"""出片前的**视觉评审**：拿这一镜的预览静帧问一个看得见图的模型，按五条打分。

为什么要有它：

- 一次调用同时回答两件事：「这一帧作为一张画**够不够好**」（笔触与材质、构图、光影、
  版式、PPT 感），以及「这一镜的视觉语言**像不像整片那句风格**」（杂志风/像素赛博朋克…）。
  上一代通道只有「每镜必画」（闸⑦）与「主体申报」（闸⑯）这类**存在性**判据，
  它们对「一个巨大的色块 + 两行字」也会判绿；好不好看、像不像，机器量不出来。
- 2026-10-18 把原先独立的 `selfcheck.style_review` 并进来：同一条静帧、同一个模型，
  分两次问只是把每镜的调用数和等待时间翻倍；分开问还有一个更贵的后果——风格不符
  的镜没有重写入口，只能把清单交回主对话，于是整条片子又走一轮 `plan_motion`。

三态：``pass`` / ``fail`` / ``unjudged``。**未判不许折算成通过**——调用炸了、回话不是
那一行 JSON、图读不出来，都记 ``unjudged``，回执里如实写。

钱：每镜一张小图 + 几百字。一条 20 镜的片子约 0.1~0.3 元。花费随回执带回
（``cost_yuan``），主循环记进这条 run 的预算。

纪律：这一层**只报不拦**，红镜带着 ``fix`` 去重写（`shot_writer.rewrite_shot_code`），
重写完再收。绝不因为评审红就把整份分镜打回。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import style_sections

RUBRIC = ("craft", "composition", "light", "typography", "not_ppt")

_RUBRIC_TEXT = """五条，每条 1~5 分（3 = 及格）：
1 craft        材质与体积：实体是**实心体块**（色块/填充/像素块），有受光面/背光面两档明度
               与落地影；线条有粗细变化（像素风则是像素块层次）。只有一圈描边/线框、
               内部与背景同明度、看不出材质 → 最多 2 分
2 composition  构图：主体占画面 40%~70%，有留白与视觉重心，不缩在角落
3 light        光影：有明确光源方向，亮面/暗面/落地影成一套
4 typography   版式与字体：字号有阶梯（≥2.2 倍），对齐与行宽像排版，不像居中堆字
5 not_ppt      不像 PPT：没有卡片墙/表格框/项目符号/一堆圆角色块/所有东西居中堆着；
               画面里看得见文案点到的东西（物件/人物/动作）。不止一件时，**每一件都要在**，
               而且同处一个场景（有远近/遮挡/朝向），不是一排并排的小图标，也不是只有版式和字。
               只有外轮廓、认不出标志物与部件的「空心图形」不算画出来了"""

_REVIEW_SYSTEM = """你是这条科普片的**美术指导**。你会看到一镜画面的静帧，以及这一镜的文案与整片美术方向。
你已经看过很多「代码画的镜头」：常见病是等宽描线的小图标、一块大色块加几行字、所有东西居中。

判读纪律：
· 画面里那几行大字是这一镜自己的字幕，它是构图的一部分；评版式时一起看，但不要只因为它大就扣分。
· 这一镜该是一幅画，不是一页排版：先找文案对应的那件具体东西有没有被画出来、占没占住画面。
  整帧看下来只有字和色块、找不到这一镜说的是什么东西 → 判 pass=false，fix 写「要画什么」。
· 如果下面给了**实体清单**，就逐个在画面里找：少一件、或只是把几件并排摆成图标行、
  彼此没有远近/遮挡/朝向关系 → 同样判 pass=false，fix 点名缺的是哪一件。
· 找到那一件之后再看**它是不是真的画出来了**：只有一圈描边/线框、里面和背景一个明度、
  没有标志物与部件（钱包没有拉链齿与钞票、骨架没有肋骨与骨盆、显示器没有屏幕内容）——
  这是「空心图形」，不是画。判 pass=false，fix 写清这件东西缺的标志物与部件、该用哪一档
  明暗填实。
· 整片要的风格如果写在下面，这一镜的版式、配色、笔触、视觉语汇要落在它上面；明显不是那个
  风格（要像素赛博朋克却做成干净的杂志版式、要手绘却全是等宽矢量线）→ 同样判 pass=false，
  fix 写清「往哪个方向靠」。
· 只看画面，不评文案写得好不好、不评事实对不对。
· 拿不准就判 pass=true。你是在找**明显**该重画的镜，不是挑刺。
· 1 分只给「这一条基本没做」，不要因为有一点瑕疵就给 1。

""" + _RUBRIC_TEXT + """

只回一行 JSON，不要别的字：
{"pass": true 或 false, "scores": {"craft": 1-5, "composition": 1-5, "light": 1-5,
 "typography": 1-5, "not_ppt": 1-5}, "why": "≤50 字，说你看到了什么", "fix": "≤90 字，pass=false 时怎么改；pass=true 留空"}"""

#: 同时看几镜（纯网络请求，与渲染并发无关）。2026-10-18 从 4 提到 8：
#: 一条 28 镜的片子，这一层是纯等待——并发翻倍，评审墙钟减半。
REVIEW_PARALLEL = max(1, int(os.environ.get("MOTION_REVIEW_PARALLEL", "8") or 8))


def _parse_review(text: str) -> dict[str, Any] | None:
    """从回话里取出那一行 JSON；形状不对就 ``None``（= 未判）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(got, Mapping) or not isinstance(got.get("pass"), bool):
        return None
    raw_scores = got.get("scores")
    if not isinstance(raw_scores, Mapping):
        return None
    scores: dict[str, int] = {}
    for key in RUBRIC:
        val = raw_scores.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 5:
            return None
        scores[key] = val
    return {
        "pass": bool(got["pass"]),
        "scores": scores,
        "why": str(got.get("why") or "").strip()[:80],
        "fix": str(got.get("fix") or "").strip()[:140],
    }


def _ask(*, text: str, subject: str, art: Mapping[str, Any],
         prop: str = "", style: str = "", grammar: str = "",
         entities: Sequence[str] = (),
         brief: str = "",
         caps: Sequence[str] = (),
         skill_contract: Mapping[str, Any] | None = None) -> str:
    lines = [f"这一镜的文案：「{text}」"]
    if subject:
        lines.append(f"这一镜要画的东西：「{subject}」")
    # brief 里有**这一镜明确不要的东西**（真机 2026-10-19：「全片无人物」）。
    # 评审不看见它，就会按模板的默认画法要求「人物去哪了」，红镜意见再把
    # 人影塞回重写里——判红的人反而成了违约的人。
    if str(brief or "").strip():
        lines.append(f"这一镜的画面要求（brief，含明确不要的东西）：「"
                     f"{str(brief).strip()[:800]}」")
        lines.append("brief 里说**不要**的东西出现在画面里 → pass=false，fix 点名"
                     "「多了什么、删掉它」；brief 没要的东西也别因为它属于模板就留着。")
        # 2026-10-22 真机：10 镜里 s01/s02/s10 的 brief 都写了「侧躺的人形 /
        # 一排头肩 / 他坐起来」，代码里却一个人都没有——那几镜恰好不是「走」，
        # 模板只给了 `SilhouetteWalk`，写手就把他整件拿掉了。评审是最后一道
        # 能看见这件事的眼睛：brief 点名的人不见，不能只判「画面挺好」。
        lines.append("brief 点名的人/主体（「人形」「同一个人」「他」「她」）在画面里"
                     "找不到 → pass=false，fix 写「这一镜少了他，把人补回来」；"
                     "姿势不是走路也一样在，不能因为模板只画走路就把他删掉。")
    names = [str(e).strip() for e in entities if str(e).strip()]
    if names:
        lines.append("这一镜文案里的实体（逐个找，少了哪件、或只是并排摆成图标行都不算）："
                     f"「{'、'.join(names)}」")
    if style:
        lines.append(f"整片要的风格：「{style}」")
    if grammar:
        lines.append(f"风格落地（这几条具体做法有没有做到）：「{grammar}」")
    if prop:
        lines.append(f"整片贯穿装置（只在适合的镜出现，不是每镜必有）：「{prop}」")
    if art:
        bits = "；".join(f"{k}={v}" for k, v in art.items()
                         if str(v or "").strip())
        if bits:
            lines.append(f"整片美术方向：{bits}")
    pack = skill_contract if isinstance(skill_contract, Mapping) else {}
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    if pack:
        # 风格包是用户选定的整片契约：五条评分照旧（**不加第六条键**，
        # `_parse_review` 恰好要五个），但判读口径按它收紧。
        lines.append("")
        lines.append(f"用户选定的风格包：「{pack.get('display') or pack.get('name')}」"
                     f"（模板 {manifest.get('template') or '无'}）")
        for key, label in (("composition", "构图"), ("recurring_pose", "贯穿姿势"),
                           ("recurring_prop", "贯穿道具"), ("caption", "字幕"),
                           ("inscription", "铭文"), ("avoid", "不要出现"),
                           # 用户在前端写给评审这一层的话（2026-10-21）：属于这份
                           # 风格的数据；空着整行不出现。
                           ("review_note", "用户补充：写给评审")):
            value = manifest.get(key)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if str(value or "").strip():
                lines.append(f"  {label}：{str(value)[:1200]}")
        lines.append("判读口径（覆盖上面同名的默认说法）：")
        if manifest.get("repeat_composition"):
            lines.append("· composition 判「和全片是否同一构图」，**不是**判「有没有变化」。"
                         "主体位置、光块位置、地平线、字幕位置与风格包一致就是对的。")
        # 模板是默认画法，不是内容上限：逐镜按 brief 换内容（对象、外形、场景、
        # 道具）是允许的；扣分只扣「风格不变量被换掉」和「同一零件画了两份」。
        lines.append("· craft 判「有没有守住风格不变量」：配色、构图、光位、笔法、"
                     "运动节奏、字号与版式必须和风格包一致；内容（对象、外形、场景、"
                     "服饰、道具、文字）按这一镜 brief 换是对的，**不因为内容不同扣分**。"
                     "另画了第二个人/第二块光/第二条字幕，或把风格换成了另一套 → "
                     "pass=false，fix 点名。")
        lines.append("· typography 判「字幕是否贴画面、是否只有一条」；"
                     "擅自加了一条横幅或第二份字幕 → pass=false。")
        lines.append("· 风格包要求重复构图时，「和上一镜太像」本身**不是**扣分理由。")
        facets = manifest.get("facets")
        if isinstance(facets, Mapping):
            text = facets.get("text") if isinstance(facets.get("text"), Mapping) else {}
            items = [i for i in (text.get("items") or []) if isinstance(i, Mapping)]
            if items:
                lines.append("· typography 还要判：这一镜的字是否用的都是风格包的文字组件（"
                             + " / ".join(f"`{i.get('component')}`" for i in items[:4])
                             + "），位置/字号是否一致；另起一条或换了字体 → pass=false。")
            motion = facets.get("motion") if isinstance(facets.get("motion"), Mapping) else {}
            film = [t for t in (motion.get("tracks") or [])
                    if isinstance(t, Mapping) and t.get("scope") == "film"]
            if film:
                lines.append("· craft 还要判：主体是否仍在**整片连续层**里（同一套比例、"
                             "运动轴线与节奏，没有每镜重新摆姿势、重新起步）。"
                             "外形按时代/场景换是内容，不扣分；比例、位置与运动断了才扣。")
            changed = []
            for fname in ("visual", "motion", "text"):
                facet = facets.get(fname) if isinstance(facets.get(fname), Mapping) else {}
                for v in facet.get("variations") or []:
                    if isinstance(v, Mapping) and v.get("prop"):
                        changed.append(f"{v.get('name') or v.get('prop')}"
                                       f"（`{v.get('prop')}`）")
            if changed:
                lines.append("· craft 看这些会变的维度是否真的取了本镜该有的那一档："
                             + "；".join(changed[:8]) + "。")
        body = str(pack.get("body") or "").strip()
        if body:
            # 渐进式风格包：评审只看「整片都成立的那一段 + 这一镜点名的能力」。
            # 整份正文给它，它会拿这一镜本来没打算用的能力去扣分。
            if str(manifest.get("progressive") or "").strip().lower() in ("1", "true", "yes", "on"):
                shown = [style_sections.default_text(body)]
                for name in caps:
                    text = style_sections.section_text(body, str(name))
                    if text:
                        shown.append(f"### {str(name)}\n{text}")
                body = "\n\n".join(t for t in shown if t.strip())
            if body:
                lines.append("风格包正文（判读依据）：")
                lines.append(body)
    lines.append("下面是这一镜的一张静帧。按五条打分，"
                 "并判断这一镜要不要重画、像不像整片要的风格。")
    return "\n".join(lines)


async def review_stills(*, shots: Sequence[Mapping[str, Any]],
                        stills: Mapping[str, Any], art: Mapping[str, Any] | None = None,
                        prop: str = "", style: str = "", grammar: str = "",
                        skill_contract: Mapping[str, Any] | None = None,
                        references: Sequence[bytes] = (),
                        user_id: str = "", conversation_id: str = "",
                        llm: Any = None) -> dict[str, Any]:
    """逐镜看预览静帧 → ``{checked, passed, failed, unjudged, scores, cost_yuan}``。

    ``stills`` 是 ``{镜号: 本地图片路径}``（`code_render.preview_stills` 的产物）。
    ``style`` 是整片那句风格；与五条一起问，风格明显不符同样判红并给 ``fix``。
    整个函数不抛：这是一份增益，不是闸。
    """
    from agent_framework.cost import cost_yuan

    from .selfcheck import _complete_with_identity

    out: dict[str, Any] = {"checked": 0, "passed": [], "failed": [], "unjudged": [],
                           "scores": {}, "cost_yuan": 0.0}
    rows = {str(s.get("id")): s for s in shots if isinstance(s, Mapping)}
    gate = asyncio.Semaphore(REVIEW_PARALLEL)

    async def one(sid: str, path: Any) -> None:
        try:
            b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            out["unjudged"].append(sid)
            return
        shot = rows.get(sid) or {}
        messages = [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": _ask(
                    text=str(shot.get("text") or ""),
                    subject=str(shot.get("subject") or ""),
                    art=art or {}, prop=prop, style=style,
                    grammar=grammar, skill_contract=skill_contract,
                    brief=str(shot.get("brief") or ""),
                    caps=[str(c) for c in (shot.get("caps") or [])],
                    entities=[str(e) for e in (shot.get("entities") or [])])},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]
        async with gate:
            try:
                resp = await _complete_with_identity(
                    messages, llm=llm, user_id=user_id,
                    conversation_id=conversation_id)
            except Exception:  # noqa: BLE001 —— 一镜看不成不带塌整份
                out["unjudged"].append(sid)
                return
        usage = getattr(resp, "usage", None)
        if usage:
            out["cost_yuan"] += cost_yuan(usage, model=str(usage.get("model") or ""))
        verdict = _parse_review(str(getattr(resp, "content", "") or ""))
        if verdict is None:
            out["unjudged"].append(sid)
            return
        out["checked"] += 1
        out["scores"][sid] = verdict["scores"]
        if verdict["pass"]:
            out["passed"].append(sid)
        else:
            out["failed"].append({
                "shot": sid,
                "why": verdict["why"],
                "fix": verdict["fix"],
                "scores": verdict["scores"],
            })

    items = sorted(stills.items())
    rate = _review_rate()
    if rate <= 0:
        items = []
    elif rate < 1.0 and items:
        # 隔 round(1/rate) 镜取一镜：0.34 → 每 3 镜看 1 镜。排序后取，确定性。
        step = max(1, round(1.0 / rate))
        items = items[::step]
    await asyncio.gather(*(one(str(sid), p) for sid, p in items))
    for key in ("passed", "unjudged"):
        out[key].sort()
    out["failed"].sort(key=lambda x: x["shot"])
    out["cost_yuan"] = round(out["cost_yuan"], 4)
    return out


#: 一版分镜最多重写几镜。**缺省不封顶**（红几镜改几镜）：
#: 从前缺省 8，14 镜红时剩下 6 镜只能把清单交回主对话，主对话再走一整轮
#: `plan_motion`（重新写码、重渲全片预览、重评全片）——那一轮比多改 6 镜贵一个量级。
#: 要封顶就设 `MOTION_MAX_VISUAL_REWRITES=8`；设 0 等于不自动重写。
_raw_rewrite_cap = os.environ.get("MOTION_MAX_VISUAL_REWRITES", "").strip().lower()
MAX_REWRITES: int | None = (
    None if _raw_rewrite_cap in ("", "all", "-1")
    else max(0, int(_raw_rewrite_cap)))

#: 同时重写几镜。重写是「模型写代码 + lint」的纯网络等待，彼此不共享状态；
#: 从前这里是一圈串行 `await`，8 镜就是 8 个模型的完整往返叠在一起。
REWRITE_PARALLEL = max(1, int(os.environ.get("MOTION_REWRITE_PARALLEL", "4") or 4))


def review_enabled(spec: Mapping[str, Any]) -> bool:
    """这一趟开不开视觉评审：spec 那一栏优先，其次环境变量，**缺省开**（按 RATE 抽样）。

    2026-10-20 曾从「缺省开」改成「缺省关」：那时评审是在替一个结构问题兜底——外框/
    字幕/主体各画各的，只能出图后找一台看图模型去挑「字幕压住主体」。片壳上线后那
    类问题在结构上不成立，缺省路径不该再多一趟「按镜计费、还要重写重渲」的调用。

    2026-10-22 又改回缺省开，因为默认形态变了：不再给写手挂预先写死的模板，画面全靠
    它按契约（技能正文 + manifest）现写——**没人看一眼，就没人知道它写成什么样**。
    看片闭环现在是这条路的第三根柱子，不再是兜底。代价由两件事压住：
    · `MOTION_VISUAL_REVIEW_RATE` 抽样（容器里缺省 0.34，每三镜看一镜）；
    · `MOTION_VISUAL_ROUNDS` 封顶（缺省 2 轮）+ 同一句意见改过一次就停手
      （见 `same_fix`），不会在同一处来回烧。
    不想付就设 `MOTION_VISUAL_REVIEW=0`，或在 spec 里写 `visual_review: false`。
    """
    raw = spec.get("visual_review") if isinstance(spec, Mapping) else None
    if isinstance(raw, bool):
        return raw
    env = os.environ.get("MOTION_VISUAL_REVIEW", "1").strip().lower()
    return env not in ("0", "false", "no", "off", "")


#: 一版分镜最多「看 → 改 → 重渲 → 再看」几轮。缺省 2：
#: 第一轮按评审改，第二轮确认改对了没有；第三轮起多半是在同一处来回，
#: 花的钱与墙钟都真，画面不动。
REVIEW_ROUNDS = max(1, min(4, int(os.environ.get("MOTION_VISUAL_ROUNDS", "2") or 2)))


def review_rounds(spec: Mapping[str, Any]) -> int:
    """这一份分镜走几轮：spec 那一栏优先，其次环境变量（1~4）。"""
    raw = spec.get("visual_review_rounds") if isinstance(spec, Mapping) else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(1, min(4, raw))
    return REVIEW_ROUNDS


def same_fix(before: Any, after: Any) -> bool:
    """两轮的红是不是同一句意见（忽略空白，只看前 60 字）。

    上一轮已经照着这句改过、重渲之后评审还是这句 → 再改一次只是同一个来回：
    写手没理解、或这条意见本身在要求一件代码画不出的东西。停在原地、把这件事
    如实写进回执，比自动再烧一轮好。
    """
    def _norm(text: Any) -> str:
        return re.sub(r"\s+", "", str(text or ""))[:60]

    left, right = _norm(before), _norm(after)
    return bool(left) and left == right



def _review_rate() -> float:
    """显式开启评审时看几成镜（0~1，缺省 1.0=全看）。

    用户要的是「出片之后按 1/3 抽检」：把这一栏设成 0.34，就每三镜看一镜，
    抽样按镜号排序隔几镜取一镜——确定性，同一份分镜每次抽到的是同一批。
    """
    try:
        rate = float(os.environ.get("MOTION_VISUAL_REVIEW_RATE", "1") or 1)
    except (TypeError, ValueError):
        rate = 1.0
    return min(1.0, max(0.0, rate))


async def rewrite_failed(*, spec: dict[str, Any], failed: Sequence[Mapping[str, Any]],
                         user_id: str = "", conversation_id: str = "",
                         llm: Any = None, client: Mapping[str, Any] | None = None,
                         limit: int | None = None,
                         stills: Mapping[str, Any] | None = None,
                         user_request: str = "",
                         prefix: str = "上一版静帧的问题") -> dict[str, Any]:
    """红镜逐镜重写（``shot_writer.rewrite_shot_code``），原地改 ``spec["shots"]``。

    → ``{rewritten: {镜号: {...}}, skipped: [镜号], cost_yuan, calls}``。
    ``skipped`` 是超出预算（``MAX_REWRITES``）或重写没写成的镜——回执要能回答
    「哪几镜没改、为什么」。

    ``prefix`` 是交给写手那一句的抬头。视觉评审用的是「上一版静帧的问题」；预览阶段
    发现代码编译不过时没有静帧可看，抬头换成「这一镜的代码编译不过」——不然写手会
    去找一张不存在的图。

    ``stills`` 是 ``{镜号: 本地静帧路径}``：**把上一版那张图一并交给写手**。
    只给文字批评时，写手是在改自己想象中的画面；给了图，它才看得见「空心方框」「线框
    骨架」到底长什么样。真机 2026-10-18：34 镜按文字意见重写，重评仍有 26 镜红。
    """
    from . import shot_writer

    rows = {str(s.get("id")): s for s in spec.get("shots") or [] if isinstance(s, Mapping)}
    out: dict[str, Any] = {"rewritten": {}, "skipped": [], "cost_yuan": 0.0, "calls": 0}
    todo = []
    for item in failed:
        sid = str(item.get("shot") or "")
        if sid and sid in rows:
            todo.append(item)
    budget = ((len(todo) if MAX_REWRITES is None else MAX_REWRITES)
              if limit is None else max(0, int(limit)))
    for item in todo[budget:]:
        out["skipped"].append(str(item.get("shot")))
    gate = asyncio.Semaphore(REWRITE_PARALLEL)

    async def one(item: Mapping[str, Any]) -> None:
        sid = str(item.get("shot"))
        critique = (f"{prefix}：{item.get('why') or '评审没给理由'}"
                    f"\n改法：{item.get('fix') or '按五条里最低的那几条改'}")
        async with gate:
            try:
                got = await shot_writer.rewrite_shot_code(
                    rows[sid], critique=critique, spec=spec, user_id=user_id,
                    conversation_id=conversation_id, llm=llm, client=client,
                    still=(stills or {}).get(sid), user_request=user_request)
            except Exception as exc:  # noqa: BLE001 —— 一镜重写失败不带塌整份
                out["skipped"].append(sid)
                out["rewritten"][sid] = {
                    "ok": False,
                    "note": f"重写调用炸了（{type(exc).__name__}: {str(exc)[:120]}）"}
                return
        out["cost_yuan"] += float(got.get("cost_yuan") or 0.0)
        out["calls"] += int(got.get("calls") or 0)
        out["rewritten"][sid] = {
            "ok": bool(got.get("ok")), "note": str(got.get("note") or "")[:240]}
        if not got.get("ok"):
            out["skipped"].append(sid)

    await asyncio.gather(*(one(item) for item in todo[:budget]))
    out["cost_yuan"] = round(out["cost_yuan"], 4)
    out["skipped"].sort()
    return out
