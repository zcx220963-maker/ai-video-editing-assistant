# -*- coding: utf-8 -*-
"""T31 帧通道 + 层 D2 静帧审美评审：让「判断模型」看得见画面。

**它填的是哪一个洞。** 闸㉖ 回看环（``review_loop.py``）从落地那天起就把
``FRAME_CHANNEL_MODULE = "storyline_server.motion.judge_channel"`` 与
``FRAME_CHANNEL_ENTRY = "judge_frame"`` 写死在常量里，然后**探它**。探不到时的行为是
Q113 那条——``undetermined`` **并阻断出片**。所以这个模块缺席的表现不是「少一个功能」，
是**每一片有红镜的片子都出不来**。本模块把那个洞堵上。

**三件事，一条通道。**

1. **帧通道（T31）**：``judge_frame`` 把一镜的 ``frame.png`` + 母版量到的五栏几何读数 +
   既有闸判它红/未判的**原文** + 那一镜的 spec（含现有代码）一起递过去，拿回一份**代码
   提案**。它是回看环「红之后的那半条腿」——几何读数说不清「画面上是一枚徽章还是一张
   脸」，只有画面说得清，所以画面必须进输入。
2. **静帧审美评审（层 D2）**：``review_still`` 把闸④ 从「带子里有没有东西」抬到
   「画得好不好」。闸④ 是**机器判据**（像素非空），它天生判不了构图失衡、元素重叠、
   廉价光效；这一半补的就是那句「好看吗」。
3. **⑲-b 身份判据**：``judge_identity`` 判**画面上那个东西是不是文案说的那个**
   （``blind_read`` 先盲描述，再拿申报的 ``subject`` 去比）。⑯ 明写着自己判不动这一格，
   闸㉝ 只判「有没有落到一个具名件上」——「必需像」这句话落在判据上的唯一形状是看画面。

**裁决那一半复用既有客户端，不另起一个。** 静帧评审走 ``agent_framework.judge`` 的
``judge()``（``secrets.py`` 那套身份/键位、页面配置压过 env、热读）——Q90/Q91 反复点的
就是「在别处再长一个裁决客户端」这种清单漂移。本模块**只**做两件它该做的事：
把画面变成模型看得懂的输入、把判据面从技能正文读出来。键位、优先级、置信度阈值
一条都不在这里重写。

**三出口，一条不许合并的纪律。** ``judge_frame`` 的出口是
「真改出了新代码」/「看了，不用改」/「通道炸了」；``review_still`` 的出口是
「判红」/「判绿」/「未判」。后两者在回执上必须是**两句话**——「没配置」与「跑了但失败」
混成一格，就又回到「没量过写成量过」那条老路（P5 / D-未判不绿）。

**任何失败都不抛。** 与 ``judge.py`` 逐字同规：未配置＝完全不启用、任何失败＝None、
绝不阻塞交付。这一条在这里尤其要紧——回看环已经在它那一侧按 Q113 决定「要不要阻断」，
本模块再抛一次，只会把一次可读的故障变成一句谜语。

**判据面从正文读，不在这里抄第二份。** ``criteria_ids()`` / ``criteria_digest()`` 读
``examples/skills/motion_explainer/aesthetic-rules.md`` 的 Q 组。抄一份的表现是
「正文改了条目、评审还在用旧的那几条」——而那正是这份代码最该避免的形状。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 与 ``review_loop.GATE`` 同一个号。回看环那一侧按 ``gate:"26"`` 分发事件（T55：
#: 只加一个事件，不新建客户端）；这里再写一遍常量是**故意的**——两处若漂，探测与调用
#: 问的就不是同一件事，而那种漂在测试里看得见、在生产里看不见。
GATE = "26"

#: 本模块自己的名字与入口。``review_loop`` 的常量与这里**必须**逐字相等，
#: ``test_motion_judge_channel.py`` ① 按此断言（探测问的名字 = 模块自己的名字）。
MODULE_NAME = "storyline_server.motion.judge_channel"
ENTRY_NAME = "judge_frame"

#: 判据正文（相对技能目录）。``aesthetic-rules.md`` 给 Q 组编号与规则句，
#: ``final-review.md`` 给成片终检的 Q 组口径——两份都在，是为了让评审看得见
#: 「判据从哪来」，而不是让模型凭印象说好看。
_CRITERIA_DOC = "aesthetic-rules.md"
_FINAL_DOC = "final-review.md"
#: 抽哪一组。Q = 质感·运镜·构图——闸④ 要抬高的正是这一组（R 组是节奏、S 组是声音，
#: 它们各自有别的闸在管，混进来会让这一条评审变成一个说不清射程的大杂烩）。
_CRITERIA_GROUP = "Q"

#: 单次评审塞进问句的判据条数上限。全塞进去会挤掉画面本身的注意力，而且这一条是
#: 「逐镜」调用——每镜多 2k 字，20 镜就是一次可观的开销。取前 N 条（正文里编号在前
#: 的是更基础的构图/质感条目）。
_CRITERIA_LIMIT = 6


# ---------------------------------------------------------------------------
# 注入点（测试用；生产走真通道）
# ---------------------------------------------------------------------------
# 写成模块级**函数**而不是常量，是为了让替身能整体换掉：用例里 `jc._llm = lambda: fake`
# 一句就能把生成那一半换成假的，不必去 patch openai SDK 或全局单例。
# 两个都**同步**——真通道也是同步取模块/取单例（重活在被调方，不在这里）。

def _llm() -> Any:
    """生成那一半：全项目共享的主 LLM（``llm_openai.get_default_llm``）。

    为什么生成走主模型而不是判断模型：``judge()`` 的契约是**是/否 + 概率**，
    它吐不出 TSX。回看环要的是**代码提案**，那是生成任务。
    """
    from agent_framework.llm_openai import get_default_llm
    return get_default_llm()


def _judge_module() -> Any:
    """裁决那一半：既有的 ``agent_framework.judge``。**不另起客户端**（Q90/Q91）。"""
    from agent_framework import judge as judge_mod
    return judge_mod


# ---------------------------------------------------------------------------
# 判据面：从技能正文读
# ---------------------------------------------------------------------------

def _skill_dir() -> Path:
    """技能目录。仓库里是 ``backend/examples/retired/motion_explainer``，
    容器里 backend 就是 ``/app``（``code_spec._SKILL_DIR`` 同一套落位判断）。

    2026-10-25 从 ``skills/`` 移到 ``retired/``：那条链退役后它不该再出现在
    模型可选的技能列表里（否则模型会照着正文调已摘白名单的节点）。判据面照旧读它。
    """
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[3] / "backend"):
        cand = base / "examples" / "retired" / "motion_explainer"
        if cand.is_dir():
            return cand
    return here.parents[2] / "examples" / "retired" / "motion_explainer"


def _read_doc(name: str) -> str:
    p = _skill_dir() / name
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def criteria_ids() -> tuple[str, ...]:
    """Q 组的判据编号（``Q1``…）。**从正文抽**，不写死一张表。

    抽不到就回空元组——调用方据此**不说**「按这几条判」（说错编号比不说更坏，
    与 ``code_spec.skill_name()`` 那条注释同一个理由）。
    """
    body = _read_doc(_CRITERIA_DOC)
    if not body:
        return ()
    return tuple(re.findall(rf"^###\s+({_CRITERIA_GROUP}\d+)\.", body, re.M))


def criteria_digest(limit: int = _CRITERIA_LIMIT) -> str:
    """把 Q 组的「规则」句摘成一段可塞进问句的判据面（带编号）。

    只取**规则那一行**：判例与自检问题对「这一帧好不好看」没有增量，塞进去只是挤占
    画面的注意力。编号一定带上——评审说「违反 Q4」时，人回头能查到是哪一条。
    """
    body = _read_doc(_CRITERIA_DOC)
    if not body:
        return ""
    out: list[str] = []
    for cid in criteria_ids()[:max(1, int(limit))]:
        m = re.search(rf"^###\s+{cid}\.\s*(.+?)\n(.*?)(?=^###\s|\Z)",
                      body, re.S | re.M)
        if not m:
            continue
        title = m.group(1).strip()
        rule = next((ln.strip() for ln in m.group(2).splitlines()
                     if ln.strip().startswith("规则：")), "")
        out.append(f"{cid} {title}｜{rule[len('规则：'):].strip() if rule else ''}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 帧 → 模型的输入
# ---------------------------------------------------------------------------

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".bmp": "image/bmp"}

_FENCE = re.compile(r"```(?:tsx|ts|jsx|js|typescript)?\s*\n?(.*?)```", re.S)
_NO_CHANGE = re.compile(r"(?i)^no[\s_-]?change[.。!！]?$")


def _data_url(image: Any) -> str:
    """帧 → ``data:`` URL。认 **bytes / 路径 / 字符串路径** 三种。

    三种都得认，是因为上游给的是哪一种不由本模块决定：缓存那一路回的是**路径**
    （``review_loop.cached_frame`` 取的是 ``cache.fetch`` 落在工作区里的那份），
    而探针那一路给的是内存里的**字节**。只认一种的表现是「生产里静默取不到帧」——
    那种红不会出现在任何一条离线用例里。
    """
    if image is None:
        return ""
    if isinstance(image, (bytes, bytearray, memoryview)):
        return "data:image/png;base64," + base64.b64encode(bytes(image)).decode("ascii")
    p = Path(str(image))
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    mime = _MIME.get(p.suffix.lower(), "image/png")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _unwrap(text: str) -> str:
    """剥 markdown 围栏。模型十有八九会包一层 ```tsx——不剥就等于把围栏一起落盘。"""
    t = (text or "").strip()
    m = _FENCE.search(t)
    return (m.group(1) if m else t).strip()


# ---------------------------------------------------------------------------
# ① 帧通道（T31）：judge_frame
# ---------------------------------------------------------------------------

_SYSTEM = """你是这个视频项目里的**镜头代码医生**。你收到的是一镜**已经渲出来的画面**、
从画面像素量到的几何读数（只有套了母版的镜才有读数，自包含的镜没有——**不要**把「没有
读数」当成画面是空的），以及既有闸判它红/未判的原文。你的唯一任务是：**改这一镜的
代码**，让画面同时满足「闸不红了」与「画面本身站得住」。

硬约束（违反其中任何一条，你的提案会被打回，等于这一轮白花）：
· 交出来的必须是**完整可落盘的一份 TSX**，`export default` 一个组件；
· 组件**自包含**：顶层是 `<AbsoluteFill>`，底色/构图/字号/字幕位都由这一镜自己定；
  母版 `<FilmLayout>` 已下线，不要 import 它（`@dg/film` 现在只是字幕与小组件的零件包）；
· 只改这一镜；不要动别的镜的代码，不要新增依赖，不要 import 项目里没有的模块；
· 逐帧纯函数：不许 `Date.now()`、`Math.random()`、网络请求；
· 代码 ≤ 40000 字节。

回话格式（只回这个，不要解释）：
· 需要改 → 只回一份 TSX（可以包 ```tsx 围栏）；
· 画面其实没问题、不该改 → 只回 `NO_CHANGE`。
"""


def _user_text(*, sid: str, reasons: Sequence[str], columns: Mapping[str, Any] | None,
               spec: Mapping[str, Any], round_index: int,
               terms_field: str | None, subject_terms: Any) -> str:
    """把「该改什么」摊成一段话。读数与闸原文**分开列**——混在一段里，模型会开始
    对读数本身发表意见，而它该做的是改代码（回看环的注释点了这条）。"""
    col = dict(columns or {})
    lines = [f"镜号：{sid}（第 {round_index} 轮回看）",
             f"这一镜申报的文案：{spec.get('text') or '（无）'}",
             f"这一镜申报的主体：{spec.get('subject') or '（无）'}"
             f"（通道：{spec.get('subject_channel') or '（无）'}）",
             f"这一镜用的卡：{spec.get('card') or '（无）'}"]
    if terms_field and subject_terms:
        lines.append(f"这一镜申报的词族（{terms_field}）：{subject_terms}")
    if col:
        lines.append("从画面量到的几何读数（套母版的镜才有）：" + "、".join(
            f"{k}={v}" for k, v in col.items()))
    else:
        lines.append("这一趟没量到几何读数（自包含的镜本来就没有，**不要**"
                     "据此以为画面是空的）")
    if reasons:
        lines.append("既有闸判它红/未判的原文（逐条照抄，别改写）：")
        lines += [f"  {i}. {r}" for i, r in enumerate(reasons, 1)]
    lines.append(f"这一镜现在的代码：\n```tsx\n{spec.get('code') or ''}\n```")
    lines.append("按上面的格式回话。")
    return "\n".join(lines)


async def judge_frame(*, gate: str = GATE, shot: str = "", image: Any = None,
                      columns: Mapping[str, Any] | None = None,
                      reasons: Sequence[str] | None = None,
                      shot_spec: Mapping[str, Any] | None = None,
                      round: int = 1, terms_field: str | None = None,
                      subject_terms: Any = None) -> dict[str, Any]:
    """一次回看调用 → ``{"ok": bool, "code": str|None, "reason": str, "why": str}``。

    **调用形状是契约，写在这里一处**（T55）。递过去的三样东西各有各的用处：
    ``image`` 是画面本身、``columns`` 是母版量到的五栏原始读数、``reasons`` 是既有闸
    判红/未判的原文。词族按**镜级申报**取（``subject_terms`` 今天不在 schema 里，
    缺就是缺，不在这里编一份替模型填）。

    返回的 ``code`` 是**提案**不是判决：它要过 ``plan_code_patch`` → 再归一 → 再渲染 →
    再回闸，四道都过了才算数。``ok=False`` 或 ``code`` 为空＝这一轮没换来东西，
    该镜回落 ``not_run``（**不是** reviewed：账上没有第二次切片）。
    """
    spec = dict(shot_spec or {}) if isinstance(shot_spec, Mapping) else {}
    rs = [str(x) for x in (reasons or []) if str(x).strip()]
    url = _data_url(image)
    parts: list[dict[str, Any]] = [{
        "type": "text",
        "text": _user_text(sid=str(shot or spec.get("id") or "?"), reasons=rs,
                           columns=columns, spec=spec, round_index=int(round or 1),
                           terms_field=terms_field, subject_terms=subject_terms)}]
    if url:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": parts}]
    try:
        resp = await _llm().complete(messages)
    except Exception as exc:      # noqa: BLE001 —— 一次调用炸了不该带塌整条片子
        return {"ok": False, "code": None, "reason": "", "raw": None,
                "why": f"回看调用失败（{type(exc).__name__}: {str(exc)[:200]}）"}
    content = str(getattr(resp, "content", "") or "")
    body = _unwrap(content)
    if not body:
        # 空回复**不许**被读成「不用改」：那是模型没说话，不是它看过之后说没问题。
        return {"ok": False, "code": None, "reason": "", "raw": content,
                "why": "模型这一轮没吐任何东西（既没有 TSX，也没有 NO_CHANGE）"}
    if _NO_CHANGE.match(body):
        return {"ok": True, "code": None, "raw": content,
                "reason": "模型看过帧之后认为这一镜不用改（NO_CHANGE）——"
                          "回看环据此记 not_run：账上没有第二次切片，就不算回看过",
                "why": ""}
    return {"ok": True, "code": body, "raw": content,
            "reason": f"模型给出了一份代码提案（{len(body)} 字节）", "why": ""}


# ---------------------------------------------------------------------------
# ② 自证：两支夹具必须被分得开
# ---------------------------------------------------------------------------

_PROBE_SINGLE = ("drawbox=x=210:y=390:w=120:h=120:color=0x1f5fc4:t=fill")
_PROBE_TWO = ("drawbox=x=90:y=390:w=120:h=120:color=0x1f5fc4:t=fill,"
              "drawbox=x=330:y=390:w=120:h=120:color=0xc0392b:t=fill")
_PROBE_BG = "color=c=0xf6f1e4:s=540x960"


def _probe_frames(dst: Path) -> tuple[Path, Path] | None:
    """造两张探针图：**单个放大部件** 与 **两部件场景**。

    为什么要真造图而不是塞两段描述：这一条判的是**帧通道能不能看**——把描述当输入，
    验的就不是画面了。用 ffmpeg 现场画（本链路的硬依赖，渲染管线离了它跑不动），
    省得往仓库里塞两张二进制资产。
    """
    from .. import mediaops
    a, b = dst / "probe-single.png", dst / "probe-two-part.png"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        mediaops.ffmpeg("-f", "lavfi", "-i", _PROBE_BG, "-vf", _PROBE_SINGLE,
                        "-frames:v", "1", str(a), timeout=60)
        mediaops.ffmpeg("-f", "lavfi", "-i", _PROBE_BG, "-vf", _PROBE_TWO,
                        "-frames:v", "1", str(b), timeout=60)
    except Exception:             # noqa: BLE001 —— 造不出图就是「没做」，如实回 None
        return None
    return (a, b) if a.is_file() and b.is_file() else None


_COUNT_SYSTEM = ("你在做一次**盲测**：只看画面，数出**彼此分离、可辨认的部件**有几个。"
                 "背景上的装饰与噪点不算部件。只回一个阿拉伯数字，不要别的字。")


async def _count_parts(image: Any) -> int | None:
    """问模型「画面里有几个彼此分离的可辨认部件」。失败/答非数字 → None（未判）。"""
    url = _data_url(image)
    if not url:
        return None
    messages = [{"role": "system", "content": _COUNT_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "画面里有几个彼此分离、可辨认的部件？只回数字。"},
                    {"type": "image_url", "image_url": {"url": url}}]}]
    try:
        resp = await _llm().complete(messages)
    except Exception:             # noqa: BLE001
        return None
    m = re.search(r"\d+", str(getattr(resp, "content", "") or ""))
    return int(m.group()) if m else None


async def self_test(*, gate: str = GATE, terms_field: str | None = None,
                    work: Path | None = None) -> dict[str, Any]:
    """盲测自证（T55 红法 / Q113）：两支夹具必须被**分得开**，分不开＝判据不可用。

    两支的构造与 T55 的验收同源：① **单个放大部件**（㉕ 的红样形状）② **两部件场景**
    （㉕ 放行的形状）。判据在**帧通道这一侧**——本模块只把两支递过去、读它的读数，
    不在回看环那一侧实现第二份（Q115）。

    返回 ``{"ok": bool|None, …}``：
    · ``True``  = 两支分得开（1 / 2）；
    · ``False`` = **量过，但判据不灵**（两支答成同一个数）；
    · ``None``  = **未判**（造不出图 / 调用失败）——「分不开」与「没做」是两句话，
      混成一格就是拿一个假的自证把回看环放行。
    """
    import tempfile
    dst = Path(work) if work is not None else Path(tempfile.mkdtemp(prefix="judge-probe-"))
    frames = _probe_frames(dst)
    if frames is None:
        return {"ok": None, "detail": None,
                "why": "自证用的两张探针图造不出来（ffmpeg 不在或调用失败）——"
                       "这一趟没人量过「帧通道分不分得开单部件与两部件」"}
    one, two = frames
    got_one = await _count_parts(one)
    got_two = await _count_parts(two)
    if got_one is None or got_two is None:
        return {"ok": None, "detail": {"single": got_one, "two_part": got_two},
                "why": "自证调用失败（帧通道这一趟没读出数）——记未判，不许拿它放行"}
    ok = (got_one == 1 and got_two == 2)
    return {"ok": ok,
            "detail": {"single": got_one, "two_part": got_two},
            "why": ("单部件读成 %s、两部件读成 %s——%s" % (
                got_one, got_two,
                "两支分得开" if ok else
                "两支分不开：这条判据今天不灵，㉖ 只能记未判（不许当通过）"))}


# ---------------------------------------------------------------------------
# ③ 静帧审美评审（层 D2）：闸④ 的「画得好不好」
# ---------------------------------------------------------------------------

#: 读帧用的问句。**为什么非要先读一遍帧**：``judge()`` 的输入是文本
#: （``state = {reply, facts}``），它看不见像素。而「画得好不好」这件事离开画面就问不出来
#: ——拿一栏几何读数去问审美，得到的只会是模型对着数字编的一句漂亮话。
#: 所以这一半的链路是**两段**：帧通道把画面读成**结构化读数**（有眼），
#: 判断模型拿读数 + 判据裁决（有尺）。两段各自复用既有零件，本模块不新增长在别处的能力。
_READ_SYSTEM = (
    "你是这一镜画面的**观察员**。只看画面，如实回答，不要评价、不要建议。"
    "只回一个 JSON 对象，不要别的字：\n"
    '{"subjects": <画面里彼此分离、可辨认的主体件数，整数>,'
    '"text_readable": <画面里的文字在手机小窗尺寸下读得清吗，true/false>,'
    '"overlap": <有没有元素互相压住、挤在一起，true/false>,'
    '"cheap_effect": <有没有廉价感的光效（群发泛光/闪烁/过曝），true/false>,'
    '"subject_present": <有没有一个明确的主体，true/false>,'
    '"note": "<一句话描述你看到什么，不超过 40 字>"}'
)

_QUESTION = (
    "这一镜的画面是否**违反**下列审美准则？只判画面本身的构图/质感/文字可读性/"
    "元素落位，不判史实与文案。判据如下：\n{criteri}\n"
    "违反（画面站不住、有廉价感、读不清）＝ true；画面干净、站得住 ＝ false。"
)


async def read_frame(image: Any) -> dict[str, Any] | None:
    """帧 → **结构化读数**（``subjects`` / ``text_readable`` / ``overlap`` /
    ``cheap_effect`` / ``subject_present`` / ``note``）。失败 → ``None``（未判）。

    它是「眼」：把像素转成判断模型读得懂的事实。读数里**不许**出现结论（不许有
    ``good`` / ``score`` 这类字段）——结论归 ``judge()``，两段各干各的，
    混在一起就会出现「观察员顺手把裁决也做了」而判据面其实没被用上。
    """
    url = _data_url(image)
    if not url:
        return None
    messages = [{"role": "system", "content": _READ_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "这一镜的画面里有什么？按格式回 JSON。"},
                    {"type": "image_url", "image_url": {"url": url}}]}]
    try:
        resp = await _llm().complete(messages)
    except Exception:             # noqa: BLE001 —— 读不到帧就是未判，不阻塞交付
        return None
    content = str(getattr(resp, "content", "") or "")
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group())
    except Exception:             # noqa: BLE001
        return None
    if not isinstance(got, Mapping):
        return None
    keep = ("subjects", "text_readable", "overlap", "cheap_effect",
            "subject_present", "note")
    return {k: got.get(k) for k in keep if k in got} or None


async def _judge_ready(mod: Any) -> tuple[bool, str]:
    """裁决通道在不在。→ (可用, 一句为什么不可用)。

    ``get_judge()`` 返回 None 的含义是「既没配 judge 三件套，也没有能出网的主模型密钥」
    ——那时**任何**第二意见都做不了。替身模块若没有这个入口，不拦，交给 ``judge()``
    自己回 None（把「没配置」与「跑了但失败」在能分的时候分开，分不了的时候不硬猜）。
    """
    fn = getattr(mod, "get_judge", None)
    if fn is None:
        return True, ""
    try:
        who = await fn()
    except Exception as exc:      # noqa: BLE001
        return False, f"判断模型探测失败（{type(exc).__name__}: {str(exc)[:120]}）"
    if who is None:
        return False, ("判断模型没配：设置页的 judge 三件套（base_url/model/key）是空的，"
                       "也没有能出网的主模型密钥可回落（`secrets.py` 那三个键位）")
    return True, ""


async def review_still(*, shot: Mapping[str, Any] | None = None, image: Any = None,
                       columns: Mapping[str, Any] | None = None,
                       criteria: Sequence[str] | None = None,
                       user_id: str = "") -> dict[str, Any]:
    """一镜的静帧审美评审 → ``{"verdict": bool|None, "confidence": float,
    "reason": str, "criteria": [...], "reading": dict|None, "why": str}``。

    **两段**，各用各的零件：``read_frame`` 把画面读成结构化读数（**眼**），
    ``agent_framework.judge.judge`` 拿读数 + 判据裁决（**尺**）。合成一段会让
    「判据面没被用上」这件事在回执上消失——而判据面正是这一条与「模型随口说好看」的区别。

    ``verdict=True`` ＝ **违反**审美准则（画得不好）；``False`` ＝ 站得住；
    ``None`` ＝ **未判**（没帧 / 没配置 / 调用失败 / 低置信）。Noul 的语义是「与事实矛盾」，
    所以问句按「是否违反」写——这样 ``verdict=True`` 与 ``actionable()`` 的含义
    跟 ``judge.py`` 其余三处守卫**逐字一致**，不必在这里再翻译一次。

    **它不拦交付。** 未配置＝完全不启用、任何失败＝未判（``judge.py`` 的纪律）。
    调用方（报告层）拿它当一条**提醒**用：判红说明「这一镜画面站不住，值得看一眼」，
    而不是「这一片出不去」——闸④ 那条机器判据仍然管着「出不出得去」。
    """
    crit = [str(x) for x in (criteria or criteria_ids())]
    blank: dict[str, Any] = {"verdict": None, "confidence": 0.0, "reason": "",
                             "criteria": crit, "reading": None}
    mod = _judge_module()
    ready, why = await _judge_ready(mod)
    if not ready:
        return {**blank, "why": why}
    # 第一段：先有眼。没帧 / 读帧失败 → 直接未判——**不许**退化成「只看读数就判审美」。
    reading = await read_frame(image)
    if reading is None:
        return {**blank,
                "why": "这一镜的画面没读到（没帧，或读帧那一次调用失败）——记未判，"
                       "不许拿几何读数代替画面去判「好不好看」"}
    spec = dict(shot or {}) if isinstance(shot, Mapping) else {}
    facts: dict[str, Any] = {"shot": spec.get("id"), "card": spec.get("card"),
                             "subject": spec.get("subject"), "reading": reading}
    for k, v in dict(columns or {}).items():
        facts[k] = v
    text = "；".join(x for x in (
        f"文案：{spec.get('text')}" if spec.get("text") else "",
        f"主体：{spec.get('subject')}" if spec.get("subject") else "",
        f"用的卡：{spec.get('card')}" if spec.get("card") else "",
        "画面读数：" + json.dumps(reading, ensure_ascii=False)) if x)
    question = _QUESTION.format(criteri=criteria_digest() or "（判据正文没读到）")
    try:
        v = await mod.judge(question, text=text, facts=facts, user_id=user_id)
    except Exception as exc:      # noqa: BLE001 —— 任何失败＝未判，绝不阻塞交付
        return {**blank, "reading": reading,
                "why": f"静帧评审调用失败（{type(exc).__name__}: {str(exc)[:160]}）——记未判"}
    if not v:
        return {**blank, "reading": reading,
                "why": "裁决没给出可用结论（没配置 / 调用失败 / 置信度不够）——"
                       "记未判，**不许**折算成「画面没问题」"}
    return {"verdict": bool(v.get("verdict")),
            "confidence": float(v.get("confidence") or 0.0),
            "reason": str(v.get("reason") or ""), "criteria": crit,
            "reading": reading, "why": ""}


# ---------------------------------------------------------------------------
# ④ ⑲-b 身份判据（盲描述）：画面上那个东西，是文案说的那个吗
# ---------------------------------------------------------------------------
# 这一条填的是**⑯ 明写着自己判不动的那一格**：闸⑯ 只问「这一镜有没有申报一个具名的
# 主体、那个通道在代码里有没有证据」，它**判不了画出来的那个东西像不像**申报的主体。
# 静态那半（闸㉝）能判「有没有落到库里一个具名件上」，也判不了「像不像」。
# 「必需像」这句话落在判据上的唯一形状就是**看画面**——所以在这一层。

#: 本模块的第二个入口名。``review_loop`` 按这个名字探它，与
#: :data:`ENTRY_NAME` 同一条纪律：探测与调用必须问同一个名字。
IDENTITY_ENTRY = "judge_identity"

#: 盲描述的问句。**「盲」是这一条的全部**：不告诉它 ``subject`` 是什么，
#: 它就无从把申报的东西复述回来。先问「你看见了什么」，再拿申报的去比。
#: 反过来（先把 subject 递过去、问「像不像」）拿回来的是一句**附和**——
#: 那条路上，画成一团乱麻也有一半概率被夸「抓住了神韵」。
_BLIND_SYSTEM = (
    "你是这一镜画面的**观察员**。只看画面，如实说出你看见了什么。"
    "**不要猜这张图想画什么**，不要评价，不要给建议。只回一个 JSON 对象，不要别的字：\n"
    '{"thing": "<画面里最显眼的那件东西是什么，用日常说法，不超过 20 字>",'
    '"kind": "<它属于哪一类：人 / 器物 / 建筑 / 车辆 / 图表 / 符号 / 文字块 / 说不清>",'
    '"others": ["<画面上另外还有哪些可辨认的东西；没有就空数组>"],'
    '"readable": <能不能一眼看出这件事物是什么，true/false>}'
)

#: 身份问句。极性跟着 ``judge()`` 的语义走（``verdict=True`` ＝ 与事实矛盾／违反），
#: 与 ``review_still`` 的「是否违反」逐字同规：**不像 ＝ true**。
_IDENTITY_QUESTION = (
    "下面那段描述出自**另一个人**：他只看画面，不知道这一镜的文案。\n"
    "这一镜的文案点到的主体是「{subject}」。请判断：他看见的那件东西**不是**这个主体吗？\n"
    "判法：类别与标志物对得上就算**是那个主体**（简笔画求达意不求写实——"
    "「蒸汽机」画成锅炉加烟囱加飞轮，是对得上的；「一个人」画成一根线挑着一个头，也对得上）；"
    "明显是别的东西（说的是机器、画出来让人看不出是什么；说的是人、画的是一列方块）"
    "就是**不是**。\n"
    "只判身份，不判画得好不好（那是另一条判据）。不是＝true；是＝false。"
)


async def blind_read(image: Any) -> dict[str, Any] | None:
    """帧 → **盲描述**（``thing`` / ``kind`` / ``others`` / ``readable``）。失败 → ``None``。

    它是 ⑲-b 的「眼」，与 ``read_frame`` 是**两问**而不是一问：``read_frame`` 问的是
    「有没有一个明确的主体」（一个布尔），这一问问的是「那个东西是什么」。
    合并成一问的后果是模型会顺手把「像不像」也答了，而那时判据面根本没被用上——
    与 ``review_still`` 那条「两段各干各的」同一条理由。

    输入里**没有** ``subject``：这一层看不见答案，也就无从附和。
    """
    url = _data_url(image)
    if not url:
        return None
    messages = [{"role": "system", "content": _BLIND_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": "画面上有什么？按格式回 JSON。"},
                    {"type": "image_url", "image_url": {"url": url}}]}]
    try:
        resp = await _llm().complete(messages)
    except Exception:             # noqa: BLE001 —— 读不到帧就是未判，不阻塞交付
        return None
    content = str(getattr(resp, "content", "") or "")
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group())
    except Exception:             # noqa: BLE001
        return None
    if not isinstance(got, Mapping):
        return None
    keep = ("thing", "kind", "others", "readable")
    out = {k: got.get(k) for k in keep if k in got}
    return out or None


async def judge_identity(*, shot: Mapping[str, Any] | None = None, image: Any = None,
                         user_id: str = "") -> dict[str, Any]:
    """一镜的 ⑲-b 裁决：**画面上那个东西是文案说的那个吗**。

    → ``{"verdict": bool|None, "confidence": float, "reason": str, "subject": str,
    "blind": dict|None, "by": str, "why": str}``

    ``verdict=True`` ＝ **不像**（画面上那个东西不是申报的 ``subject`` 说的那个）；
    ``False`` ＝ 像；``None`` ＝ **未判**（没帧 / 这一镜没申报 subject / 读不到 /
    调用失败 / 低置信）。极性跟着 ``judge.py`` 的「与事实矛盾」走，与 ``review_still``
    逐字同规，调用方不必在这里再翻译一次。

    **三条出路各有各的话，不许合并**（P5 / D-未判不绿）：

    * ``readable=false``（观察员说这件东西认不出是什么）→ **直接判不像**，不再花第二次
      调用。理由就是用户那句话：「必需像」——一个看画的人说不出画的是什么，它就不像。
      这一条**不走**裁决通道，所以 ``by="blind"`` 与 ``by="judge"`` 必须分得开：
      「两个模型比出来的」与「一个模型说认不出」是两件事，回执上不能长成同一句话。
    * 观察员看清楚了、但裁决通道没配／调用失败 → 未判。**不许**把「看不出来」
      折算成「像」，也不许把「没配」折算成「不像」。
    """
    spec = dict(shot or {}) if isinstance(shot, Mapping) else {}
    subject = str(spec.get("subject") or "").strip()
    blank: dict[str, Any] = {"verdict": None, "confidence": 0.0, "reason": "",
                             "subject": subject, "blind": None, "by": "", "why": ""}
    if not subject:
        return {**blank, "why": ("这一镜没有申报 `subject`（闸⑯ 会拦这一格）——"
                                 "⑲-b 无从比「像不像」，记未判")}
    # 先问裁决通道在不在**再读帧**：反过来的话，没配判断模型时这一条会替每一镜白花一次
    # 带图调用（`judge_ready` 是免费的），而「没配置＝完全不启用」是这一层的纪律。
    mod = _judge_module()
    ready, why = await _judge_ready(mod)
    if not ready:
        return {**blank, "why": why}
    blind = await blind_read(image)
    if blind is None:
        return {**blank,
                "why": ("这一镜的画面没读到（没帧，或盲描述那一次调用失败）——"
                        "记未判，不许拿「没量到」当「像」")}
    # 认不出是什么 ＝ 不像。这一条不必再问一次裁决通道：观察员自己就说了认不出。
    if blind.get("readable") is False:
        thing = str(blind.get("thing") or "").strip() or "说不清是什么"
        return {"verdict": True, "confidence": 1.0, "subject": subject, "blind": blind,
                "by": "blind", "why": "",
                "reason": (f"只看画面的人说不出画的是什么（他说的是「{thing}」）——"
                           f"文案点到的主体是「{subject}」，这一镜的画面认不出它")}
    text = "；".join(x for x in (
        f"文案：{spec.get('text')}" if spec.get("text") else "",
        f"申报的主体：{subject}",
        "另一个人只看画面写下的描述：" + json.dumps(blind, ensure_ascii=False)) if x)
    question = _IDENTITY_QUESTION.format(subject=subject)
    try:
        v = await mod.judge(question, text=text,
                           facts={"shot": spec.get("id"), "subject": subject,
                                  "blind": blind}, user_id=user_id)
    except Exception as exc:      # noqa: BLE001 —— 任何失败＝未判，绝不阻塞交付
        return {**blank, "blind": blind,
                "why": f"⑲-b 裁决调用失败（{type(exc).__name__}: {str(exc)[:160]}）——记未判"}
    if not v:
        return {**blank, "blind": blind,
                "why": "裁决没给出可用结论（没配置 / 调用失败 / 置信度不够）——"
                       "记未判，**不许**折算成「像」"}
    return {"verdict": bool(v.get("verdict")), "confidence": float(v.get("confidence") or 0.0),
            "reason": str(v.get("reason") or ""), "subject": subject, "blind": blind,
            "by": "judge", "why": ""}


# ---------------------------------------------------------------------------
# 能力：如实报两半
# ---------------------------------------------------------------------------

async def capability() -> dict[str, Any]:
    """本模块的几半各自在不在。**只读、只描述**，不拦任何东西。

    ``frame`` 恒为真（模块能 import 就说明入口在，``review_loop`` 那一侧也按同一事实探）。
    ``identity`` 报的是 ⑲-b 那个入口在不在——与 ``frame`` 同一条理由：**探测与调用必须问
    同一个名字**，名字漂了就会出现「探测说在、调用说没有」。
    ``judge`` 报的是裁决通道——与 ``review_loop.capability()`` 同一句为什么，
    因为它们是同一件事的两次读法，两处措辞漂了就会出现「回看环说没配、评审说能跑」。
    """
    out: dict[str, Any] = {"gate": GATE, "frame": True, "judge": False, "why": "",
                           "identity_why": ""}
    out["identity"] = callable(globals().get(IDENTITY_ENTRY))
    if not out["identity"]:
        out["identity_why"] = f"本模块没有 ⑲-b 入口 `{IDENTITY_ENTRY}`"
    ready, why = await _judge_ready(_judge_module())
    out["judge"] = ready
    out["why"] = why
    return out
