# -*- coding: utf-8 -*-
"""统筹层（showrunner）：LLM 当导演，代码只当「手和眼」。

设计见仓库根的 `设计-LLM当导演-2026-10-26.md`。三条核心：

1. **镜数与镜长由 LLM 给**，不由公式算。`sec` 是内容的一部分，不是除法。
2. **判据只剩物理的**（没崩 / sec ≥ 旁白 / 不是全白全黑）；审美交给评审。
   阈值是「机器替人做审美判断」的产物，这里不再有阈值。
3. **代码只做四件事**：渲、抽帧、把帧给模型看、记账。

为什么单独一个文件：旧的两条路（explainer / story）还在 `film.py` 里，
新路径先隔离出来，跑过一轮再删旧的。`film.make_film` 现在只调这里。

命名：`director.py` 是「两套分镜提示词」，这里是**统筹**（谁在什么时候做什么）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from . import kernel, qa
from .kernel import KernelError

#: 镜数上下限。上限 40 是**护栏**（防它写 40 镜），不是审美判据。
MIN_SHOTS, MAX_SHOTS = 3, 40
#: 单镜时长区间。1.0 秒是「闪帧」的下限，15 秒是「静帧」的上限。
MIN_SHOT_SEC, MAX_SHOT_SEC = 1.0, 15.0
#: 总时长与目标的允许偏差。超了让导演重给一次，**不替它改值**。
DURATION_TOLERANCE = 0.15
#: 评审回路最多几轮。3 轮是成本上限（16 镜 × 3 = 48 次带图调用）。
JUDGE_ROUNDS = 1
#: 每轮最多重画几镜。再多说明分镜本身有问题，该重做分镜。
MAX_FIX_PER_ROUND = 8
#: 单镜「写-渲-看」最多几轮（2026-10-26）。反馈必须在**每一步之内**，
#: 不能一口气盲写 28 镜再事后返修——那样错 20 个，4 轮修不完只能整片打回。
SHOT_TRIES = 3
#: 导演调用最多试几次（格式/约束不合格就带着问题重问）。
PLAN_TRIES = 3
#: 镜长要比旁白长出的留白（秒）。与 `voice.GAP_SEC` 同源，这里只用于校验。
VOICE_GAP = 0.35
#: 曝光闭环：写手提示写的就是 40/255，真机实测整片 p50 = 27.5、98.4% 的帧低于它，
#: 而 `physical()` 只判 <3 全黑 → 一直合法。渲完按读数补 gain，不靠模型"记得画亮"。
EXPOSURE_TARGET, EXPOSURE_MAX_GAIN = 40.0, 2.6      # 封顶 2.6：再高会把噪点一起提上来


PLAN_SYSTEM = """你是这部短片的导演。只做一件事：把用户那句话变成一份**分镜表**。

输出**纯 JSON**，不要解释、不要 markdown 代码块：

{
  "title": "片名（≤12 字）",
  "tone": "整片的画法基调。**必须写清四件**：① 底色（给色值）② 主色两个（给色值）③ 构图习惯（满/空、对称/偏心、地平线高低）④ 笔触（矢量平涂/剪纸/手绘/3D/照片拼贴）。这四件决定十几镜像不像同一部片子；含糊一句，每镜就会各画各的",
  "motif": "贯穿全片的那一件东西（≤8 字，没有就空串）",
  "caption": {"size": 0.045, "at": "lower", "color": "#F2F5F8", "scrim": 0},
  "shots": [
    {"sec": 3.2, "text": "这一镜的字幕（≤20 字）", "plan": "画什么、怎么动、用什么技术",
     "transition": "cut"}
  ]
}

## 先想审美，再写分镜

`caption` 是**全片一套的字幕规格**——字幕由写手逐镜画进画面，只有这一份能让 23 镜的字一样：
`size` 字号占短边 0.03~0.06、`at` 位置 bottom/lower/center/upper/left、`color` 字色、`scrim` 底衬 0~0.4。

`tone` 是**你对这个题材的审美判断**，不是填空题：先想三个方向（各一句"长什么样 + 为什么
适合这题材"），挑最贴的那一个写进 `tone`。**避开偷懒答案**——暗底加一个光点、深蓝科技感、
极简白底黑字、"电影感"三个字本身，那是模型的默认口味，不是这个题材的选择。
**用户原话里点了风格就必须照它**（"剪纸""高饱和""像苹果发布会""手绘笔记"），不许改成你偏好的。

## 镜数与镜长由你定

**这是你最重要的判断，不要套公式。**

- 总时长约 [[SECONDS]] 秒，但**不是均分**：一句话该停 2 秒就 2 秒，该停 9 秒就 9 秒。
- 单镜 [[MINSEC]]~[[MAXSEC]] 秒，[[LO]]~[[HI]] 镜都算对。讲得下就少几镜，讲不下就多几镜。
- **节奏参考**（经验，不是硬指标）：快切科普是 14+ 镜 / 60 秒、每镜 3~5 秒、画面很密；
  叙事长镜是 4~12 秒一镜、单镜自己撑得住。用户要"深刻克制"就别快切，要"燃"就快切。

## `plan` 要写到能被别人画出来

写清**画什么**（具体物件，不是"科技感"）、**怎么动**、**用什么技术**。技术按内容挑，不按习惯挑：
· `svg` 手绘矢量（默认：抽象概念/图表/剪影/星空/光晕）· `three` 3D（有体积有反射，贵 3~5 倍）
· `components` 设计系统（逐词字幕/科技卡/数据卡）· `html` 真排版（表格/长文/网页感）

`transition` 只写 `cut` 或 `fade`，**默认 `cut`**（硬切是节奏的一部分）；只有段落交界、时间跳跃、情绪收束才用 `fade`，全片不超过 1/3 的切点。

## 字幕

`text` 是观众读到的那一句，也是旁白的原文。**≤20 字**，一句话一件事。
不要写成"下面我们来看"这种空话。

## 出处

字幕里出现具体数字（人数/年份/比例/时长）时，另给 `sources`：
`[{"claim": "这个数字说了什么（≤20 字）", "source": "出处名称", "url": "链接或空串"}]`。
**查不到出处的数字，删掉，不要编。**

画幅 [[ASPECT]]。"""


SHOT_CONTEXT = """## 整片的画法基调（每一镜都要遵守，否则十几镜不像同一部片子）

{tone}

**tone 说的是气质，不是曝光。** 写"近黑底"是指底色深，**不是整片欠曝**——
整帧平均亮度仍要 **≥38/255**，主体与背景亮度差 **≥40 级**。
（真机踩过：tone 写"克制不鲜艳"，15 镜全做成 17/255，观众看不出画的是什么。）

## 前两镜画过什么（**接着它们画同一个世界；但不要重复构图**）

{prev}

## 可用画法

· `svg` 手绘矢量（默认）——抽象概念、图表、剪影、星空、光晕
· `three` 3D——有体积/有光/有反射的东西；比 svg 贵 3~5 倍，非它不可时用
· `components` 设计系统组件——逐词字幕、科技卡、数据卡
· `html` 真排版——表格、长文、网页感"""


#: 整片画面合格线（2026-10-26）。不合格的镜占比超过这个就不许出片。
#: 26 镜里 26 镜不合格（合格率 0.0）照样渲完交付过，所以要有一道真闸。
QUALITY_PASS_RATIO = 0.6


def parse_json(text: str) -> dict[str, Any] | None:
    """从模型回复里挖出第一个 JSON 对象（它常包 ```json 围栏）。"""
    raw = str(text or "")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    try:
        got = json.loads(raw)
        return got if isinstance(got, dict) else None
    except (TypeError, ValueError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        got = json.loads(raw[start:end + 1])
        return got if isinstance(got, dict) else None
    except (TypeError, ValueError):
        return None


def plan_problems(obj: dict[str, Any] | None, seconds: float) -> list[str]:
    """校验导演给的 plan。返回问题列表（空 = 合格）。名字里没有 `check_`（边界测试禁止）。
    **只做物理校验，不替它改值**：越界就让它重给——钳过的镜长会让"节奏由内容定"失效。
    """
    if not isinstance(obj, dict):
        return ["没有回合法 JSON"]
    # `tone` 是整片唯一的画法载体。空的就等于 23 个写手各画各的，必散。
    if len(str(obj.get("tone") or "").strip()) < 12:
        return ["`tone` 太短——整片唯一的画法基调，要写清底色/主色/构图/笔触，否则每镜各画各的"]
    shots = obj.get("shots")
    if not isinstance(shots, list) or not shots:
        return ["`shots` 不是非空数组"]
    if not (MIN_SHOTS <= len(shots) <= MAX_SHOTS):
        return [f"镜数 {len(shots)} 不在 {MIN_SHOTS}~{MAX_SHOTS} 之间"]
    bad: list[str] = []
    total = 0.0
    for i, s in enumerate(shots):
        if not isinstance(s, dict):
            bad.append(f"第 {i + 1} 镜不是对象")
            continue
        try:
            sec = float(s.get("sec"))
        except (TypeError, ValueError):
            bad.append(f"第 {i + 1} 镜 sec 不是数字")
            continue
        if not (MIN_SHOT_SEC <= sec <= MAX_SHOT_SEC):
            bad.append(f"第 {i + 1} 镜 sec={sec:g} 不在 {MIN_SHOT_SEC:g}~{MAX_SHOT_SEC:g}")
        total += sec
        if not str(s.get("text") or "").strip():
            bad.append(f"第 {i + 1} 镜没有 text")
    if seconds > 0 and total > 0:
        off = abs(total - seconds) / seconds
        if off > DURATION_TOLERANCE:
            bad.append(f"总时长 {total:g} 秒与目标 {seconds:g} 秒差 {off:.0%}"
                       f"（允许 {DURATION_TOLERANCE:.0%}）")
    return bad[:6]


def normalize(obj: dict[str, Any]) -> dict[str, Any]:
    """导演的 plan → 内部形状。`plan` 映射成写手认的 `plate`。

    **白名单式转换**：没在这里留一手的字段会被丢掉。`caption` / `transition` 都为此专门保留。
    """
    shots: list[dict[str, Any]] = []
    cap0 = obj.get("caption") if isinstance(obj.get("caption"), dict) else {}
    for s in obj.get("shots") or []:
        if not isinstance(s, dict):
            continue
        shots.append({
            "sec": round(float(s.get("sec") or 3.0), 2),
            "text": str(s.get("text") or "").strip()[:40],
            "plate": str(s.get("plan") or s.get("plate") or "").strip(),
            "tech": str(s.get("tech") or "").strip(),
            # 转场要带出来：白名单式转换不留就会丢，全片退回硬编码循环（2026-10-26 真机）。
            "transition": str(s.get("transition") or "").strip().lower(),
            "caption": s.get("caption") if isinstance(s.get("caption"), dict) else cap0,
        })
    return {
        "title": str(obj.get("title") or "").strip()[:24],
        "tone": str(obj.get("tone") or "").strip()[:200],
        "motif": str(obj.get("motif") or "").strip()[:20],
        "sources": obj.get("sources") if isinstance(obj.get("sources"), list) else [],
        "shots": shots,
        # 三次都没命中时长、最后按比例缩过 → 如实带出去（`make` 会写进 duration_note）
        **({"scaled_from": obj["scaled_from"]} if obj.get("scaled_from") else {}),
    }


def _total_sec(obj: dict[str, Any]) -> float:
    """这份 plan 的总时长（镜长之和）。"""
    return round(sum(float(x.get("sec") or 0) for x in (obj.get("shots") or [])
                     if isinstance(x, dict)), 2)


def structural_ok(obj: dict[str, Any] | None, seconds: float) -> bool:
    """结构合格吗：镜数、单镜时长、text 都对，只有总长可能不对。
    总长是唯一"模型难一次命中、但代码能按比例救回来"的偏差；镜数错那种救不了。
    """
    if not isinstance(obj, dict):
        return False
    if len(str(obj.get("tone") or "").strip()) < 12:
        return False   # tone 不合格不能按"只差时长"救——救回来的是没有画法基调的一稿
    shots = obj.get("shots")
    if not isinstance(shots, list) or not (MIN_SHOTS <= len(shots) <= MAX_SHOTS):
        return False
    for x in shots:
        if not isinstance(x, dict) or not str(x.get("text") or "").strip():
            return False
        try:
            sec = float(x.get("sec"))
        except (TypeError, ValueError):
            return False
        if not (MIN_SHOT_SEC <= sec <= MAX_SHOT_SEC):
            return False
    return True


def scale_to(obj: dict[str, Any], seconds: float) -> dict[str, Any]:
    """按比例缩到目标时长，保留相对节奏（哪镜长哪镜短不变）；零头摊给最长那镜。"""
    shots = [x for x in (obj.get("shots") or []) if isinstance(x, dict)]
    total = sum(float(x.get("sec") or 0) for x in shots)
    if not shots or total <= 0:
        return obj
    k = seconds / total
    for x in shots:
        x["sec"] = round(max(MIN_SHOT_SEC, min(MAX_SHOT_SEC, float(x["sec"]) * k)), 2)
    drift = round(seconds - sum(x["sec"] for x in shots), 2)
    if drift:
        # 优先摊到"还有余量"的那一镜（最长的那镜）
        target = max(shots, key=lambda x: x["sec"])
        target["sec"] = round(max(MIN_SHOT_SEC, min(MAX_SHOT_SEC,
                                                   target["sec"] + drift)), 2)
    obj["scaled_from"] = round(total, 1)
    return obj


async def plan(prompt: str, *, seconds: float, aspect: str, llm: Any = None,
               user_id: str = "", conversation_id: str = "") -> tuple[dict[str, Any], int]:
    """导演调用：一句话 → {title, tone, motif, shots[{sec, text, plate}]}。"""
    lo = max(MIN_SHOTS, int(seconds / MAX_SHOT_SEC))
    hi = min(MAX_SHOTS, max(lo + 1, int(seconds / MIN_SHOT_SEC)))
    # 用户消息里不能留 `[[ASPECT]]`：占位符只替换 system（真机踩过，原样喂给了模型）。
    base = (f"主题：{str(prompt).strip()}\n\n"
            f"目标时长 {seconds:g} 秒——**所有镜的 sec 加起来要接近这个数**，"
            f"画幅 {aspect}。镜数与每镜时长由你定（{lo}~{hi} 镜都算对），不要均分。")
    ask = base
    # 不用 str.format：这段 prompt 里有 JSON 示例，花括号会被当占位符
    # （实测 KeyError: '\n  "title"'）。用 [[...]] 手工替换。
    system = (PLAN_SYSTEM
              .replace("[[SECONDS]]", f"{seconds:g}")
              .replace("[[MINSEC]]", f"{MIN_SHOT_SEC:g}")
              .replace("[[MAXSEC]]", f"{MAX_SHOT_SEC:g}")
              .replace("[[LO]]", str(lo))
              .replace("[[HI]]", str(hi))
              .replace("[[ASPECT]]", aspect))
    last: list[str] = ["没调用成功"]
    best: dict[str, Any] | None = None      # 结构合格、只是总长偏了的那一份
    for _ in range(PLAN_TRIES):
        try:
            raw = await kernel._complete(
                [{"role": "system", "content": system},
                 {"role": "user", "content": ask}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
        except Exception as exc:  # noqa: BLE001
            raise KernelError("plan", f"导演调用失败（{type(exc).__name__}）：{exc}") from exc
        obj = parse_json(raw)
        last = plan_problems(obj, seconds)
        if not last:
            got = normalize(obj or {})
            if not got["shots"]:
                last = ["normalize 之后没有镜头"]
                continue
            return got, 1
        # 只差总长 → 给**精确差值与动作**。只说"差 17%"，模型不知道往哪改（真机连挂三次）。
        if structural_ok(obj, seconds):
            best = obj
            total = _total_sec(obj)
            delta = round(total - seconds, 1)
            verb = "多" if delta > 0 else "少"
            how = (f"把 3~4 镜各缩短 2~4 秒，或者去掉 2~3 镜"
                   if delta > 0 else "把几镜各加长 2~3 秒，或者补 1~2 镜")
            ask = (f"主题：{str(prompt).strip()}\n\n"
                   f"上一版总长 **{total:g} 秒**，比目标 {seconds:g} 秒{verb} "
                   f"{abs(delta):g} 秒。\n"
                   f"**只改时长，讲法一个字都别动**：{how}，"
                   f"让总数落在 {seconds * 0.95:.0f}~{seconds * 1.05:.0f} 秒。"
                   f"其余照旧，只回 JSON。")
        else:
            ask = (f"主题：{str(prompt).strip()}\n\n"
                   "上一版不合格：\n" + "\n".join(f"· {x}" for x in last)
                   + f"\n\n目标时长 {seconds:g} 秒，画幅 {aspect}。按上面的问题改，只回 JSON。")
    # 三次都没命中：能救就救——结构合格只是总长偏了，按比例缩到目标并记
    # `scaled_from`（降级显式）。整条失败比"时长偏 17%"更糟。
    if best is not None:
        got = normalize(scale_to(best, seconds))
        if got["shots"]:
            return got, PLAN_TRIES
    raise KernelError("plan", "导演给了 3 次都不合格：" + "；".join(last))


def prev_summary(shots: list[dict[str, Any]], index: int) -> str:
    """前两镜画过什么——**这是「不要重复」的替代品**，比事后判据自然。"""
    lo = max(0, index - 2)
    rows = [f"· 第 {j + 1} 镜：{str(shots[j].get('plate') or '')[:60]}"
            for j in range(lo, index)]
    return "\n".join(rows) if rows else "（这是开头两镜，没有前作）"


async def write_shot(shot: dict[str, Any], *, index: int, total: int,
                     tone: str, shots: list[dict[str, Any]], aspect: str,
                     llm: Any = None, user_id: str = "", conversation_id: str = "",
                     extra: str = "", motif: str = "") -> str:
    """写一镜的画面代码。复用 `story.write_scene_story_code` 的格式检查与 lint。"""
    from .story import write_scene_story_code      # 延迟 import：避免循环

    ctx = SHOT_CONTEXT.format(tone=tone or "（未指定，按内容自己定）",
                              prev=prev_summary(shots, index))
    note = ctx + (f"\n\n## 上一版的问题（必须修掉）\n{extra}" if extra else "")
    return await write_scene_story_code(
        shot, index=index, total=total, seconds=float(shot.get("sec") or 3.0),
        aspect=aspect, llm=llm, user_id=user_id, conversation_id=conversation_id,
        extra=note, motif=motif)


async def judge(evidence: list[dict[str, Any]], shots: list[dict[str, Any]],
                tone: str, *, llm: Any = None, user_id: str = "",
                conversation_id: str = "", concurrency: int = 3) -> dict[int, str]:
    """让模型看每一镜的帧 + 读数，回「要不要改、改什么」。

    **读数是证据，不是判据**：模型看到 content=0.06 加一张几乎空的图，
    自己会说"这镜太空"。代码不替它下结论。
    """
    sem = asyncio.Semaphore(concurrency)
    out: dict[int, str] = {}

    async def one(ev: dict[str, Any]) -> None:
        i = int(ev["index"]) - 1
        if i < 0 or i >= len(shots):
            return
        async with sem:
            try:
                hint = await judge_shot(
                    ev.get("jpeg") or "", readings=ev.get("readings") or {},
                    shot=shots[i], tone=tone, prev=prev_summary(shots, i),
                    llm=llm, user_id=user_id, conversation_id=conversation_id)
            except Exception:  # noqa: BLE001 —— 看不了就不看，别拖垮整条
                return
            if hint:
                out[int(ev["index"])] = hint

    await asyncio.gather(*[one(ev) for ev in evidence])
    return out


def physical(ev: dict[str, Any]) -> str:
    """物理判据：ok / black / white / unknown。只判"没渲出来"——偏暗是审美，交给评审。"""
    r = ev.get("readings") or {}
    if not r:
        return "unknown"
    lum = float(r.get("mean_lum") or 0.0)
    if lum <= 3.0:
        return "black"
    if lum >= 250.0:
        return "white"
    return "ok"


def adopt(scenes: Any) -> dict[str, Any]:
    """外部给的分镜（人改过的 / 复用上一次的）→ 内部形状。

    **两种形状都收**：list（只要 shots）与 dict（`/simple/plan` 的整份产物）。
    收 dict 是为了「先看分镜、改完再继续」那条路——`tone` / `motif` / `sources`
    不能在这一步丢掉（丢了画法基调与出处账就没了）。
    """
    if isinstance(scenes, dict):
        got = normalize(scenes)
        if got["shots"]:
            return got
        scenes = scenes.get("shots") or scenes.get("scenes") or []
    rows = [s for s in (scenes if isinstance(scenes, list) else []) if isinstance(s, dict)]
    return normalize({"shots": rows})


async def make(prompt: str, *, seconds: float = 60.0,
               aspect: str = kernel.DEFAULT_ASPECT, fps: int = kernel.DEFAULT_FPS,
               llm: Any = None, user_id: str = "", conversation_id: str = "",
               on_stage: Any = None, voice: bool = True,
               scenes: Any = None) -> dict[str, Any]:
    """一句话 → 一条成片。返回的字典与旧 `film.make_film` 同形（`short_video` 直接吃）。

    四步：导演 → 旁白校验 → 逐镜写 → 评审回路。代码只做「渲、抽帧、给模型看、记账」。
    """
    # 延迟 import：film 在模块级 import 本文件，这里不能反过来。
    from . import system, voice as vo
    from .film import (assemble, parse_motif_at, safe_story_scene,
                       transitions_for_story, wrap_story_scene, _scene_fingerprint)

    def tick(stage: str, done: int, total: int) -> None:
        if on_stage:
            try:
                on_stage(stage, done, total)
            except Exception:  # noqa: BLE001
                pass

    # ── ① 导演：镜数与镜长由 LLM 给 ────────────────────────────────────────
    tick("plan", 0, 0)
    if scenes:
        plan_obj, calls = adopt(scenes), 0
    else:
        plan_obj, calls = await plan(prompt, seconds=seconds, aspect=aspect,
                                     llm=llm, user_id=user_id,
                                     conversation_id=conversation_id)
    shots = plan_obj["shots"]
    n = len(shots)
    secs = [float(s["sec"]) for s in shots]
    tone = str(plan_obj.get("tone") or "")
    # 兜底底色从 `tone` 里取第一个色值（`tone` 现在必须写底色）。取不到才用 #05070c。
    _m = re.search(r"#[0-9a-fA-F]{6}", tone)
    film_bg = _m.group(0) if _m else ""
    motif = str(plan_obj.get("motif") or "")

    # ── ② 旁白：**只校验，不改值** ─────────────────────────────────────────
    #
    # 镜长是导演给的。这里唯一要做的是「旁白念不完就把这一镜拉长」——
    # 音画同步是物理约束，不是审美判断，所以必须在代码里。
    narr: dict[str, Any] | None = None
    if voice:
        tick("voice", 0, n)
        try:
            narr = await vo.narrate([str(s.get("text") or "") for s in shots],
                                    kernel.out_dir() / "voice")
        except Exception as exc:  # noqa: BLE001 —— TTS 挂了不该整条失败
            narr = {"files": [None] * n, "secs": [0.0] * n,
                    "failed": list(range(1, n + 1)),
                    "error": f"{type(exc).__name__}: {exc}"}
        if narr.get("files") and any(f is not None for f in narr["files"]):
            need = [float(v or 0.0) + VOICE_GAP for v in (narr.get("secs") or [])]
            if len(need) == n:
                secs = [round(max(a, b), 2) for a, b in zip(secs, need)]
            total_voice = round(sum(secs), 2)
            if total_voice > kernel.MAX_SECONDS:
                raise KernelError(
                    "voice", f"旁白念完需要 {total_voice:g} 秒，超过物理上限 "
                             f"{kernel.MAX_SECONDS:g} 秒——减少镜数，或让每句字幕更短。")
        else:
            narr["degraded"] = True

    # ── ③ 逐镜写（并发） ───────────────────────────────────────────────────
    tick("scenes", 0, n)
    sem = asyncio.Semaphore(4)
    done = 0
    scene_errors: list[str] = []

    async def one(i: int) -> tuple[str, str]:
        nonlocal done
        async with sem:
            kw = dict(total=n, tone=tone, shots=shots, aspect=aspect, llm=llm,
                      user_id=user_id, conversation_id=conversation_id, motif=motif)
            try:
                body, hint = "", ""
                # 写一镜 → 立刻渲这一镜 → 看读数 → 不对就带着读数重写。
                # 反馈在每一步之内，不攒到最后批量返修：那样 28 镜里错 20 个，
                # 4 轮返修修不完，只能整片打回（2026-10-26 连续三次都是这个死法）。
                for attempt in range(SHOT_TRIES):
                    try:
                        body = await write_shot(shots[i], index=i, **kw, extra=hint)
                    except KernelError:
                        hint = ("上一版画不出来。**简化这一镜**：只画 plate 里最核心的那一个"
                                "东西，其余全去掉，不要细节、不要第二层。")
                        continue
                    cand = wrap_story_scene(body, shots[i], index=i, bg=film_bg)
                    code_i, _ = assemble([cand], [secs[i]], ["cut"], fps=fps,
                                         motifs=[parse_motif_at(cand)])
                    ev = await shot_evidence(code_i, [secs[i]], aspect=aspect,
                                             fps=fps, only=1)
                    rd = (ev[0].get("readings") if ev else {}) or {}
                    if not qa.is_blank(rd):
                        return cand, body          # 这一镜成了，定稿
                    warns = (ev[0].get("warnings") if ev else []) or []
                    hint = qa.readout_fix(rd, warnings=warns) or \
                        "这一帧渲出来几乎空白——主体没画出来，重画这一镜"
                    if warns:
                        hint += "\n\n渲染时浏览器控制台报错（按 file:line 定位）：\n" \
                                + "\n".join(f"- {w}" for w in warns[:8])
                return wrap_story_scene(body, shots[i], index=i, bg=film_bg), body
            finally:
                done += 1
                tick("scenes", done, n)

    results = await asyncio.gather(*[one(i) for i in range(n)],
                                   return_exceptions=True)
    bodies: list[str] = []
    # `raws` = 模型自己写的那半（不含外层注入的 Camera / 字幕）。
    # 覆盖率必须量这个：量 `bodies` 的话，外层给每镜都塞了 Camera+RichCaption，
    # 于是 ratio 恒等于 1.0——那个 100% 不度量任何东西（真机 23 镜全是它）。
    raws: list[str] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            # 兜底会盖住失败原因，所以错误原文必须收起来（报告里要能看见）。
            scene_errors.append(f"s{i + 1:02d} {type(r).__name__}: {str(r)[:160]}")
            bodies.append(safe_story_scene(shots[i], index=i, bg=film_bg))
            raws.append("")
        else:
            bodies.append(r[0])
            raws.append(r[1])
    calls += n

    kinds = transitions_for_story(shots)
    plan_obj["transitions"] = kinds

    def rebuild() -> tuple[str, dict[str, Any]]:
        """拼装一次。**每次都重新读 motif 锚点**——重写过的镜位置会变。"""
        return assemble(bodies, secs, kinds, fps=fps,
                        motifs=[parse_motif_at(b) for b in bodies])

    code, tl = rebuild()

    # ── ④ 评审回路：看帧 → 说哪不行 → 只重画那几镜（≤3 轮） ────────────────
    rewritten: list[int] = []
    evidence: list[dict[str, Any]] = []
    notes: dict[int, str] = {}
    #: 每镜被重画过几次。`sorted()[:MAX_FIX_PER_ROUND]` 每轮都取镜号最小的 8 个，
    #: 前 8 镜一直不合格就永远轮不到第 9 镜之后（2026-10-26：23 镜里只有 2~13 被修过）。
    tries: dict[int, int] = {}
    for _round in range(JUDGE_ROUNDS):
        tick("audit", 0, n)
        evidence = await shot_evidence(code, secs, aspect=aspect, fps=fps)
        was_blank = {int(e["index"]): qa.is_blank(e.get("readings") or {})
                     for e in evidence}
        # 物理判据先过一遍：没渲出来（全黑/全白）的镜不用问模型，直接重画。
        blind = [int(e["index"]) for e in evidence if physical(e) != "ok"]
        notes = await judge(evidence, shots, tone, llm=llm, user_id=user_id,
                            conversation_id=conversation_id)
        # 读数翻成诊断再交给写手：评审的 hint 限 40 字，传不出对比度差多少。
        # 这一步不改画法，只把「缺多少」说清楚——怎么改仍由模型自己定。
        for e in evidence:
            _r = e.get("readings") or {}
            _fix = qa.readout_fix(_r)
            if _fix:
                _no = int(e["index"])
                notes[_no] = (notes[_no] + "；" + _fix) if notes.get(_no) else _fix
        for no in blind:
            notes.setdefault(no, "这一帧几乎是全黑或全白——画面没画出来，重画")
        if not notes:
            break
        fixed = 0
        for shot_no, hint in sorted(notes.items(),
                                   key=lambda kv: (tries.get(kv[0], 0), kv[0])
                                   )[:MAX_FIX_PER_ROUND]:
            i = shot_no - 1
            if not (0 <= i < n):
                continue
            try:
                body = await write_shot(shots[i], index=i, total=n, tone=tone,
                                        shots=shots, aspect=aspect, llm=llm,
                                        user_id=user_id,
                                        conversation_id=conversation_id,
                                        extra=hint, motif=motif)
                cand = wrap_story_scene(body, shots[i], index=i, bg=film_bg)
                old = bodies[i]
                bodies[i] = cand
                code, tl = rebuild()
                # 重写不许把画面写坏：只渲这一镜，比原来更空就退回原版。
                # 2026-10-26 真机：31 次重写把 23 镜写成 content=0.0 的纯背景，
                # 而没被重写的 4 镜（含 s27 那张脑网格）全正常。
                probe = await shot_evidence(code, secs, aspect=aspect, fps=fps,
                                            only=shot_no)
                if (probe and not was_blank.get(shot_no, True)
                        and qa.is_blank(probe[0].get("readings") or {})):
                    bodies[i] = old
                    code, tl = rebuild()
                    continue
                rewritten.append(shot_no)
                tries[shot_no] = tries.get(shot_no, 0) + 1
                fixed += 1
            except KernelError:
                pass
        if not fixed:
            break
        calls += fixed
        code, tl = rebuild()

    # ── ④.5 曝光闭环：量出来的亮度 → 每镜一个 gain（不靠模型记得画亮，也不全片盖白）
    gains: dict[int, float] = {}
    for e in evidence:
        lum = float((e.get("readings") or {}).get("mean_lum") or 0)
        if lum > 0:
            gains[int(e["index"]) - 1] = round(
                min(EXPOSURE_MAX_GAIN, max(1.0, EXPOSURE_TARGET / lum)), 2)
    if any(g > 1.05 for g in gains.values()):
        bodies = [wrap_story_scene(raws[i], shots[i], index=i, gain=gains.get(i, 1.0), bg=film_bg)
                  if raws[i] else bodies[i] for i in range(n)]
        code, tl = rebuild()

    # ── 记账（形状与旧链一致，`short_video` 与前端都直接吃） ────────────────
    evidence = await shot_evidence(code, secs, aspect=aspect, fps=fps)
    notes = await judge(evidence, shots, tone, llm=llm, user_id=user_id,
                        conversation_id=conversation_id)
    for _e in evidence:
        _r = _e.get("readings") or {}
        _fx = qa.readout_fix(_r)
        if _fx:
            _n0 = int(_e["index"])
            notes[_n0] = (notes[_n0] + "；" + _fx) if notes.get(_n0) else _fx
    bad = sorted(set(notes) | {int(e["index"]) for e in evidence if physical(e) != "ok"})
    # 行形状与旧 `qa.audit` 对齐（`content` / `vivid` 摊在顶层）：
    # `reshoot_shot` 与前端都直接读 row["content"]，改成嵌套会让它们静默拿到 None。
    rows: list[dict[str, Any]] = []
    for e in evidence:
        r = e.get("readings") or {}
        rows.append({
            "index": int(e["index"]),
            "content": r.get("content"), "vivid": r.get("vivid"),
            "mean_lum": r.get("mean_lum"), "edge": r.get("edge"),
            "readings": r,
            "verdicts": [physical(e)], "worst": physical(e),
        })
    _blank = {int(e["index"]) for e in evidence if qa.is_blank(e.get("readings") or {})}
    defects = sorted(_blank | {int(e["index"]) for e in evidence if physical(e) != "ok"})
    concerns, bad = sorted(set(notes) - set(defects)), defects
    _pass = round((n - len(bad)) / max(1, n), 3)
    audit = {
        # `ok` 现在表示「**画面合格**」，不再是「抽帧成功」。
        # 2026-10-26 那次：26 镜全不合格、ratio 0.0，但 ok=True（它只表示抽到了帧），
        # 消费方读成「画面没问题」，还解释成「质检服务没连上」。语义必须分开。
        "ok": bool(evidence) and not defects,
        "evidence": bool(evidence),
        "defects": defects,
        "concerns": concerns,
        "pass_ratio": _pass,
        "ratio": _pass,
        "bad": bad,
        "shots": rows,
        "frames": sum(int(e.get("frames") or 0) for e in evidence),
        "errors": [] if evidence else ["一帧都没渲出来"],
    }
    voice_info: dict[str, Any] | None = None
    if narr is not None:
        vsecs = [float(s) for s in (narr.get("secs") or [])]
        voice_info = {
            "files": [str(p) if p else "" for p in (narr.get("files") or [])],
            "secs": [round(s, 2) for s in vsecs],
            "failed": narr.get("failed") or [],
            "degraded": bool(narr.get("degraded")),
            "error": narr.get("error") or "",
            "over": [],
        }

    return {
        "code": code,
        # `scenes` 是给 `facts.audit_sources` 用的（它读 plan["scenes"] 的 text）：
        # 出处账按镜盘数字，键名沿用旧的，免得两处各改一遍。
        "plan": {**plan_obj, "scenes": shots},
        "secs": secs, "calls": calls, "shots": n, "mode": "auto",
        "coverage": system.coverage(raws), "audit": audit,
        "rewritten": rewritten, "patched": [], "fallback": [],
        "picture": "code", "scene_errors": scene_errors,
        "variety": len({_scene_fingerprint(b) for b in bodies}),
        "timeline": tl, "transitions": kinds,
        "bodies": bodies, "scenes": shots,
        "elements": {i + 1: system.element_count(b) for i, b in enumerate(bodies)},
        "voice": voice_info,
        "total_sec": round(sum(secs), 2),
        # 导演三次都没命中时长、最后按比例缩过 → **如实写出来**，
        # 别让用户以为"导演一次就给对了"。降级要显式。
        **({"duration_note": (
            f"导演给的总长是 {plan_obj['scaled_from']:g} 秒，已按比例缩到 "
            f"{round(sum(secs), 2):g} 秒（哪镜长哪镜短的相对节奏保留）")}
           if plan_obj.get("scaled_from") else {}),
    }


async def shot_evidence(code: str, secs: list[float], *, aspect: str,
                   fps: int = kernel.DEFAULT_FPS, base: str | None = None,
                   scale: float = 0.30,
                   only: int | None = None) -> list[dict[str, Any]]:
    """渲帧 + 量读数，**不下结论**——新路径专用。

    与 `qa.audit()` 的区别：那边把读数折成 verdict，这里原样交出去由模型判断。
    阈值是「机器替人做审美判断」的产物，而两套阈值方向相反、合并必然误伤。

    返回每镜一条：
    ``{index, readings: {content,vivid,mean_lum,edge}, jpeg: <base64>, frames: n}``
    `jpeg` 取 0.5 那一帧（镜中段）——0.15 那一点常落在进场动画里，看不出这一镜。
    """
    frames, owners = qa.sample_frames(secs, fps=fps)
    if only is not None:                      # 只渲指定镜（重写后复检用）
        keep = [(f, o) for f, o in zip(frames, owners) if o == only - 1]
        frames, owners = [f for f, _ in keep], [o for _, o in keep]
    total = sum(secs)
    got: dict[int, dict[str, float]] = {}
    jpegs: dict[int, str] = {}
    warns_by_shot: dict[int, list[str]] = {}

    for i in range(0, len(frames), qa.BATCH):
        chunk = frames[i:i + qa.BATCH]
        try:
            pairs = await kernel.stills_pairs(code, chunk, seconds=total,
                                              aspect=aspect, fps=fps,
                                              scale=scale, base=base)
        except KernelError:
            continue
        f2o = {f: o for f, o in zip(chunk, owners[i:i + qa.BATCH])}
        for p in pairs:
            fr = p["frame"]
            got[fr] = qa.measure(p["jpeg_b64"])
            jpegs[fr] = p["jpeg_b64"]
            ow = f2o.get(fr)
            if ow is not None and p.get("warnings"):
                warns_by_shot.setdefault(ow, []).extend(p["warnings"])

    # 按镜聚合：读数取三个采样点里**最好的那个**（一帧画满就算这镜画到了）。
    by_shot: dict[int, list[int]] = {}
    for frame, owner in zip(frames, owners):
        if frame in got:
            by_shot.setdefault(owner, []).append(frame)

    out: list[dict[str, Any]] = []
    for i in range(len(secs)):
        fs = by_shot.get(i) or []
        if not fs:
            out.append({"index": i + 1, "readings": {}, "jpeg": "", "frames": 0,
                        "warnings": []})
            continue
        rows = [got[f] for f in fs]
        readings = {
            "content": round(max(r.get("content", 0) for r in rows), 3),
            "vivid": round(max(r.get("vivid", 0) for r in rows), 3),
            "mean_lum": round(max(r.get("mean_lum", 0) for r in rows), 1),
            "edge": round(max(r.get("edge", 0) for r in rows), 2),
        }
        mid = fs[len(fs) // 2]
        out.append({"index": i + 1, "readings": readings,
                    "jpeg": jpegs.get(mid) or jpegs.get(fs[0]) or "",
                    "frames": len(fs),
                    "warnings": list(dict.fromkeys(warns_by_shot.get(i, [])))})
    return out


JUDGE_SYSTEM = """你看的是这部短片里的一帧，和它旁边的几个读数。判断这一镜能不能交付。

**读数是参考，不是判据。** 暗调片 content 天然低，但**低到什么程度就不叫暗调、叫欠曝**，
下面这几个区间是实测量出来的，用它校准你的眼睛：

· `mean_lum`（整帧平均亮度）**38~55 是正常**。**低于 30 就是欠曝**——
  主体和背景糊在一起，观众看不出画的是什么。成片实测过 17，那种帧看着就是一片黑。
· `content`（与背景亮度差 >20 的像素占比）**0.15 以上算有主体**。
  **低于 0.08 说明主体和背景几乎同色**，等于没画。
· `vivid` 低于 0.01 对暗调片是正常的，不用管。

按顺序判，命中任一条就回「改：<具体改法>」：

1. **有没有一件能被指着说的东西**？（望远镜/人影/城市/行星/控制台/表格/数据）
   只有渐变和色块、或者只有一行字 → 要改。
2. **画面是不是空的**？`content` < 0.08 = 主体和背景几乎同色 = 没画东西 → 要改。tone 说"留空/极简"只放宽"满"的要求，**不豁免"主体看得见"**——留空是留负空间，不是黑屏。
3. **和前面几镜是不是同一个母题**（又是城市+落日、又是同一个圆）→ 要改：
   换地点、换尺度、换时间、换视角。
4. **有没有兑现这一镜的 plan**？plan 说要画 A，画面上没有 A → 要改。
5. **字幕在不在、读得清吗**？缺字/被截断/白字压亮部 → 要改（字幕由写手自己画，外层不补）。

**改法里必须带可执行的目标值**，否则写手不知道"亮多少"：

· 太暗 → `改：天空改成三段渐变(top #0a1020/mid #2a2340/low #7a4630)、
  加一处径向光晕、主体占 30%，整帧亮度提到 38 以上`
· 太空 → `改：主体放大到占画面 35%，加一条地面线压住下沿，再补两处前景细节`
· 不这么写就等于没说——「亮一点」「再多点东西」写手没法执行。

要改就回：`改：<一句具体改法，不超过 40 字，带目标值>`
不用改就回：`过`
不要解释、不要客套、不要分点。"""


async def judge_shot(jpeg_b64: str, *, readings: dict[str, Any], shot: dict[str, Any],
                     tone: str, prev: str, llm: Any = None,
                     user_id: str = "", conversation_id: str = "") -> str:
    """判一镜。返回 ``"改：…"`` 或 ``""``（通过）。

    与 `review.review_frame` 的区别：那边只判「是不是重复了母题」，这边判**全部**——
    有没有主体、空不空、像不像排版、有没有兑现 plan、字幕读不读得清。
    这是整条链里**唯一一处审美判断**，代码不再自己下结论。
    """
    if not jpeg_b64:
        return ""
    plan_txt = str(shot.get("plate") or "").strip() or "（这一镜没有写 plan）"
    text = str(shot.get("text") or "").strip()
    r = readings or {}
    read_txt = (f"content={r.get('content', '?')} vivid={r.get('vivid', '?')} "
                f"mean_lum={r.get('mean_lum', '?')}/255 edge={r.get('edge', '?')}")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": [
            {"type": "text",
             "text": (f"整片基调：{tone or '（未指定）'}\n"
                      f"前面几镜画的是：\n{prev}\n"
                      f"这一镜的 plan：{plan_txt}\n"
                      f"这一镜的字幕：{text}\n"
                      f"读数：{read_txt}\n\n这一帧：")},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
        ]},
    ]
    try:
        raw = await kernel._complete(messages, llm=llm, user_id=user_id,
                                     conversation_id=conversation_id)
    except Exception:  # noqa: BLE001 —— 看不了就不看，别把整条拖垮
        return ""
    txt = str(raw or "").strip().replace("\n", " ")
    if not txt or txt.startswith("过"):
        return ""
    return txt[:120]
