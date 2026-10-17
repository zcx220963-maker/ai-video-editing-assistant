# -*- coding: utf-8 -*-
"""R11 ㉖ 回看环（T55/T56）：红/未判的镜 → 看帧 → 改代码 → **只重烧该镜** → 回闸。

**它治的是哪一格。** 闸①–㉕ 判「这一镜画出来的东西对不对」；判完红了之后，原来只有一条
出路：把整条片子打回给人看。回看环补的是**红之后的那半条腿**——把那镜的帧与几何读数发回
模型，让它自己改代码，改完只重烧那一镜再回闸。它不是质检（质检已经在闸里），它是**修复**。

**它不发明任何一条新判据。** 目标镜的名单直接调既有那几条判据（⑲-a / ⑳(c) / ㉕ / ㉑）
在**同一份**逐镜版面回读上跑一遍，红了或未判的镜就是目标（Q115：判据重合处**只读**它们的
输出，不实现第二份）。也就是说：这一条不可能比既有闸更严或更松，它的账与它们的账**同源**。

**两个红源，一条修复的路。** 逐镜身份（⑲-b「画面上那个东西是不是文案说的那个」，见
``identity_targets``，**单独一个开关**，2026-10-17）是第二个红源：它判的是几何闸问不出的
那一格（「必需像」）。它算出来的红镜与几何闸的红镜合到同一份 ``targets``，走同一个「看帧 →
改代码 → 只重烧该镜 → 回闸」，**不为它另开一条修复的路**——那正是 Q115 点名的清单漂移。

**它也不发明第二条提交路。** 模型改出来的代码走 `code_patch.plan_code_patch` → 与
`patch_motion_code` 用的**同一个** `edits[{pointer:"/shots/i/code"}]` 形状落盘。于是
「回看改的镜」与「人改的镜」在库里长成同一个样子；`shot_cache_key` 的输入含那一镜的
`code`，代码变了键就变，键变了就是一次**新版本**——「只重烧该镜」因此在账上是可数的。

**㉖ 的三出口，以及为什么「缺席」不是出口之一。**
``reviewed`` / ``not_run`` / ``undetermined`` 是**被回看判过红的镜**的三种归宿：
真重烧了（缓存键变了且第二次是现烧的）／该回看而没回看（这趟没开、或开了但没换来新切片）
／回看这件事**做不了**（裁决通道或帧通道缺一半）。**过了所有闸的镜不进 ㉖ 的射程，它在
回执里那一栏压根不存在**——缺席、``not_run``、``undetermined`` 是三件事，混成一格就又回到
「没量过写成量过」那条老路（P5/D-未判不绿）。

**数据来自产物，不来自模型的话。** 每个目标镜的证据是两栏从**账**里读的数：改动前/后的
`shot_cache_key`、两次切片的字节数。模型的自我报告在回执里最多是一栏备注，**不许**参与判定
——㉖ 的红法正是照这条设计的（「声称回看过但账上没有第二次切片」→ 必须 `not_run`）。

**回看是真钱，所以它有一个整片开关，默认关。** 每镜一次带图像输入的多模态调用是这条链上
最贵的一类调用；花钱的能力在本仓一律有开关（BGM / 质感 / 转场都是先例），回看不例外
（``REVIEW_KNOB``）。开关关着而又有红镜时，那一栏是 ``not_run``——**不是**缺席、**不是**
通过，回执上明写「这趟没开回看」。开关打开而裁决/帧通道缺一半时是 ``undetermined``
**并阻断出片**（Q113 原文：绝不静默放行，也不许降级成「就当没这步」）。

**依赖如实写：帧通道不在时，本模块只能走到 ``undetermined``。**
帧通道（把 `frame.png` 变成一次可裁决的输入）由 ``judge_channel`` 交付；本模块**明写它的
调用形状并探测它**，但不自己起一个 judge 客户端——那正是主合同反复点的清单漂移（Q90/Q91）。
2026-10-17 起那两半都落地了：``judge_frame``（改代码）与 ``judge_identity``（⑲-b 的身份
裁决）在同一个模块里，探测各问各的名字（``FRAME_CHANNEL_ENTRY`` / ``IDENTITY_ENTRY``）。
"""

from __future__ import annotations

import copy
import importlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from . import code_patch as cp
from . import code_spec as cspec

#: 闸号。帧通道那一侧的事件形状按这个号分发（T55：只加一个 `gate:"26"` 的事件，
#: 不新建客户端）。
GATE = "26"

#: 每镜最多几轮（Q112：只开红/未判的镜，**每镜 ≤2 轮**）。用尽仍红 → 按主合同打回，
#: 不许「看过了就算过」——那会把这条环变成一张免责单。
MAX_ROUNDS = 2

#: 整片开关的名字。进 `spec` 并且**活着穿过校验**（`code_spec.SWITCH_KNOBS`，2026-10-17：
#: 在这之前 `validate_code_spec` 会把这一栏静默删掉，于是「谁写的都不生效」）；开它的写法
#: 是写进 ``args["spec"]``，与自由色 `highlight_color` 同一条路。**计划卡上没有它的位置**：
#: 卡面那排 radio 回答「片子长什么样」，这一档回答「愿不愿意按镜付钱」。
#: 为什么**默认关**：这一档每镜一次带帧的多模态调用，是这条链上最贵
#: 的调用；它救的是「已经红了」的镜，而「红镜本来就出不了片」这一点没变——开关关着只是少一条
#: 自动修复的路，不会让问题片子溜出去。反过来默认开会**替所有人花掉这笔钱**，而本仓的规矩是
#: 「花钱的能力必须能被人关掉」（BGM/质感/转场的先例），不是「必须默认省钱」。
REVIEW_KNOB = "review"

#: 五栏几何读数（T56 点名的五个，`grid_*` 与 `colors` 各算一栏）。为什么要**逐栏**报而不是
#: 把整个 `scanned` 丢过去：这一趟要把「母版量到了什么」与「模型觉得画得怎样」分开——
#: 整包丢过去，模型会开始对读数本身发表意见，而它该做的是改代码。
FIVE_COLUMNS = ("subjects", "subject_share", "text_share", "grid_text", "grid_drawn",
                "colors")

#: 帧通道的模块与入口（T31 的交付件）。名字写在这里而不是散在调用点：探测与调用必须问
#: 同一个名字，否则会出现「探测说在、调用说没有」这种最难查的形状。
FRAME_CHANNEL_MODULE = "storyline_server.motion.judge_channel"
FRAME_CHANNEL_ENTRY = "judge_frame"

#: ⑲-b 的词族栏名（T31 往 schema 里加的镜级栏）。**今天它不在 schema 里**
#: （见主合同 §9-Q106④ 与 T36 的落地记录），所以这一栏只能来自镜级申报；缺了就是缺了，
#: 不允许在这里编一份词族替模型填。
#:
#: 2026-10-17：⑲-b **落地了，但没有用它**。它落的是**盲描述**那条路
#: （``judge_channel.judge_identity``：先只问「你看见了什么」，再拿申报的 ``subject``
#: 去比）——那条路一个词族也不需要，而「别写 `subject_terms`」这句话在技能正文与
#: 回执措辞里都还挂着。所以这一栏**保持不在 schema 里**，`ask_frame` 仍照它取镜级申报。
SUBJECT_TERMS_FIELD = "subject_terms"

#: ⑲-b 的整片开关（2026-10-17）。读法与 :data:`REVIEW_KNOB` 完全一致（``spec`` 里那一栏，
#: 同样在 `code_spec.SWITCH_KNOBS` 里活着穿过校验），**默认关**，
#: 而且是**两层开关里的里面那层**：整片开关 ``review`` 没开时这一条压根不跑
#: （回看环本身没开，算出来的红镜无从处置，那些钱等于白花）。
#:
#: 为什么单独立一个而不是并进 ``review``：并进去会让「打开回看」这件事偷偷把开销从
#: 「每红镜一次」抬到「每镜一次」——一条 20 镜片子里红镜通常三五条。**改一个已有开关的
#: 价钱，比多一个开关危险得多**：用户按旧印象按下那一枚，账却是新的。
#: 每镜一次带图调用的能力在本仓一律有自己的开关（BGM / 质感 / 静帧评审都是先例）。
IDENTITY_KNOB = "identity"

#: ⑲-b 的入口名（与 ``judge_channel.IDENTITY_ENTRY`` 逐字相等，``test_motion_judge_channel.py``
#: 按此断言）。名字写在这里而不是散在调用点：探测与调用必须问同一个名字。
IDENTITY_ENTRY = "judge_identity"


class ReviewBlocked(Exception):
    """回看环**做不了**或做坏了 → 阻断出片（Q113 / T56-②）。

    带**可读回执**（``.receipt``）：判据不可用时要能一眼看出「是裁决通道没配、还是帧通道
    没落地、还是别的镜被带重烧了」。笼统一句「回看失败」等于把一次故障变成一句谜语。
    """

    def __init__(self, message: str, receipt: dict[str, Any] | None = None):
        super().__init__(message)
        self.receipt = receipt or {}


@dataclass(frozen=True)
class Target:
    """一个要回看的镜：镜号 + 每一条判据的原文（不重写措辞，直接用既有闸的输出）。"""

    sid: str
    reasons: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"shot": self.sid, "gates": list(self.gates),
                "reasons": list(self.reasons)}


@dataclass
class Round:
    """一轮回看的账：动了哪几镜、重烧了哪几镜、牵连了哪几镜。"""

    index: int
    asked: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    reburned: list[str] = field(default_factory=list)
    collateral: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 能力探测：两半都在才能回看
# ---------------------------------------------------------------------------

def frame_channel() -> tuple[Any, str]:
    """帧通道（T31）。→ (模块, 一句为什么不可用)；模块为 None 时那句话就是**原因**。

    探的是**两个**事实：模块在不在、入口在不在。只探一个会出现「模块在但入口改名了」这种
    探测说可用、调用当场炸的形状。
    """
    try:
        mod = importlib.import_module(FRAME_CHANNEL_MODULE)
    except Exception as exc:      # noqa: BLE001 —— 缺模块与导入时炸都算「不在」
        return None, (f"T31 的帧通道没落地：`{FRAME_CHANNEL_MODULE}` 导不进来"
                      f"（{type(exc).__name__}: {str(exc)[:120]}）——没有人把 "
                      f"`frame.png` 变成一次可裁决的输入")
    if not hasattr(mod, FRAME_CHANNEL_ENTRY):
        return None, (f"`{FRAME_CHANNEL_MODULE}` 在，但没有入口 `{FRAME_CHANNEL_ENTRY}`"
                      "——探测与调用问的是同一个名字，这里没过")
    return mod, ""


async def capability() -> dict[str, Any]:
    """回看环的能力探测：**裁决通道**（判断模型三件套）+ **帧通道**（T31）两半。

    裁决通道直接复用既有的 `agent_framework.judge`（`secrets.py:22-24` 那套身份/键位、
    页面配置压过 env、热读）——本模块**不**另起一个客户端（Q90/Q91/Q113）。
    `get_judge()` 返回 None 的含义是「既没配三件套，也没有能出网的主模型密钥」，
    那时**任何**第二意见都做不了，回看环只能记未判。
    """
    out: dict[str, Any] = {"gate": GATE, "available": False,
                           "judge": False, "judge_why": "",
                           "frame": False, "frame_why": "",
                           "subject_terms_in_schema": False, "why": ""}
    # ① 裁决通道
    try:
        from agent_framework import judge as judge_mod
        who = await judge_mod.get_judge()
        out["judge"] = who is not None
        if who is None:
            out["judge_why"] = ("判断模型没配：设置页的 judge 三件套（base_url/model/key）"
                               "是空的，也没有能出网的主模型密钥可回落（`secrets.py` "
                               "那三个键位）")
    except Exception as exc:      # noqa: BLE001
        out["judge_why"] = f"判断模型探测失败（{type(exc).__name__}: {str(exc)[:120]}）"
    # ② 帧通道
    mod, why = frame_channel()
    out["frame"] = mod is not None
    out["frame_why"] = why
    # ③ 词族那一栏今天在不在 schema 里。这一项**不参与**可用性判定（它是 ⑲-b 的输入，
    # 不是回看环的通道），但必须如实报——T57 的正文点它名字时靠这一栏决定用哪种写法。
    try:
        props = ((cspec.input_schema() or {}).get("properties") or {})
        shots = ((props.get("shots") or {}).get("items") or {}).get("properties") or {}
        out["subject_terms_in_schema"] = SUBJECT_TERMS_FIELD in shots
    except Exception:             # noqa: BLE001
        out["subject_terms_in_schema"] = False
    out["available"] = bool(out["judge"] and out["frame"])
    if not out["available"]:
        out["why"] = "；".join(x for x in (out["judge_why"], out["frame_why"]) if x)
    return out


async def self_test() -> dict[str, Any]:
    """盲测自证（T55 红法 / Q113）：两支夹具必须被**分得开**，分不开＝判据不可用。

    两支的构造与 T55 的验收同源：① 单个放大图标（今天 ㉕ 的红样形状）② 两部件场景
    （㉕ 放行的形状）。**判据在帧通道那一侧**——本模块只把两支递过去、读它的裁决，
    不在这一侧实现第二份（Q115，与 `subject_terms` 同一条：词族的真相在 ⑲-b）。
    通道不在 → 直接未判，不许拿一个假的自证把回看环放行。

    返回 ``{"ok": bool|None, …}``；``ok=None`` 是**未判**（「分不开」与「没做」两件事）。
    """
    cap = await capability()
    if not cap["available"]:
        return {"ok": None, "why": cap["why"], "detail": None}
    mod, _ = frame_channel()
    fn = getattr(mod, "self_test", None)
    if fn is None:
        return {"ok": None,
                "why": (f"帧通道 `{FRAME_CHANNEL_MODULE}` 没有自证入口 `self_test`——"
                        "「能不能区分『单个放大图标』与『两部件场景』」这句话今天没人量过"),
                "detail": None}
    try:
        got = await fn(gate=GATE, terms_field=SUBJECT_TERMS_FIELD)
    except Exception as exc:      # noqa: BLE001
        return {"ok": None, "why": f"自证调用失败（{type(exc).__name__}: {str(exc)[:160]}）",
                "detail": None}
    if not isinstance(got, Mapping):
        return {"ok": None, "why": f"自证返回的不是一条裁决（{got!r}）", "detail": None}
    ok = got.get("ok")
    return {"ok": None if ok is None else bool(ok),
            "why": str(got.get("reason") or ""), "detail": dict(got)}


# ---------------------------------------------------------------------------
# 目标镜：把既有四条判据的「红/未判」逐镜翻出来
# ---------------------------------------------------------------------------

def five_columns(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """一镜的五栏几何读数（母版量的那一块）。 → ``None`` 表示**没量到**。

    没量到必须是 `None` 而不是一包空值：空值到了模型那一侧会被读成「这一镜什么都没有」，
    而事实是「这一趟没读到」——这正是回看环最不该让模型误判的一栏（它会照着假读数改代码）。
    """
    sc = (row or {}).get("scanned") if isinstance(row, Mapping) else None
    if not isinstance(sc, Mapping) or not sc:
        return None
    cols = {k: sc.get(k) for k in FIVE_COLUMNS if k in sc}
    if "band" in sc:
        cols["band"] = sc.get("band")
    return cols or None


def layout_of(result: Mapping[str, Any], sid: str) -> Mapping[str, Any]:
    """一次渲染的结果里，该镜的**版面回读块**（`{status, scanned, reason}`）。"""
    for r in (result.get("shots") or []):
        if isinstance(r, Mapping) and str(r.get("id") or "") == sid:
            lay = r.get("layout")
            return lay if isinstance(lay, Mapping) else {}
    return {}


def scanned_of(result: Mapping[str, Any]) -> dict[str, Any]:
    """一次渲染的结果 → 既有那几条闸要的 `scanned` 形状（`{sid: 版面块}`）。

    与 `code_render` 里 ⑲-a/⑳(c)/㉕ 的调用点取的是**同一份**（`{str(r["id"]): r["layout"]}`）
    ——两处各拼一次就会出现「闸读的与回看读的不是同一趟回读」。
    """
    return {str(r.get("id")): (r.get("layout") if isinstance(r.get("layout"), Mapping)
                               else {}) for r in (result.get("shots") or [])
            if isinstance(r, Mapping)}


def collect_targets(shots: Sequence[Mapping[str, Any]], *,
                    scanned: Mapping[str, Any] | None = None,
                    rows: Mapping[str, Mapping[str, Any]] | None = None,
                    fps: int = 30) -> list[Target]:
    """本轮**红或未判**的镜（去重、保序）。判据全部复用既有四条，不重写。

    逐镜那三条（⑲-a / ㉕ / ㉑）按**单镜**调用：返回的每一条错误天然带着镜号，不必去解析
    既有闸那句中文的措辞（解析措辞就是把措辞变成接口，改一个字就崩）。⑳(c) 是**成对**的，
    所以按两两组合调用同一份 `check_degenerate_forms`——配对红了**两条镜都是目标**：
    这一对里改哪一条都可能把「同一张卡」解开，只让模型看自己那一条，它改完仍会同构。

    **只收「红」与「未判」，不收「提醒」**：提醒是「偏小/偏偏」这类构图建议，回看是为
    「这一镜出不了片」准备的（Q112 的射程）。把提醒也拖进来会让每一镜都进回看，
    额度翻倍——那正是 Q112 否掉的那一档。
    """
    rows_map = scanned if isinstance(scanned, Mapping) else {}
    led = rows if isinstance(rows, Mapping) else {}
    hits: dict[str, dict[str, list[str]]] = {}

    def bump(sid: str, gate: str, msgs: Sequence[str]) -> None:
        slot = hits.setdefault(sid, {})
        for m in msgs:
            slot.setdefault(gate, []).append(str(m))

    for i, s in enumerate(shots):
        sid = cp.shot_id(s, i)
        e, _w, u = cspec.check_subject_present([s], scanned=rows_map)
        bump(sid, "19a", list(e) + list(u))
        e, _w, u = cspec.check_badge_composition([s], scanned=rows_map)
        bump(sid, "25", list(e) + list(u))
        # ⑧ 字压图（2026-10-16 新增的那一类命中）：这一镜的字幕压在画出来的图形上。
        # 归到回看环是因为它的改法是**局部**的（把这块字挪开 / 给它一条衬底），
        # 正是回看环这套「只重烧这一镜」的机器擅长的活；而它读的是同一条版面回读，
        # 与 ⑲-a 共用 `rows_map`，不需要任何新的数据通路。
        e, _w, u = cspec.check_text_over_subject([s], scanned=rows_map)
        bump(sid, "8t", list(e) + list(u))
        # ㉑：判据要的两个输入都在账里（缓存命中也带着它们，见 META_FIELDS 那条注释）。
        row = led.get(sid) or {}
        events = ((row.get("motion") or {}).get("events") or []) \
            if isinstance(row.get("motion"), Mapping) else []
        e, _w, u = cspec.check_motion_windows(
            s, events=events, word_frames=row.get("motion_anchor_frames") or [],
            fps=int(fps), beats=None)
        bump(sid, "21", list(e) + list(u))

    ids = [cp.shot_id(s, i) for i, s in enumerate(shots)]
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            e, _w, _u = cspec.check_degenerate_forms(
                [dict(shots[a]), dict(shots[b])], scanned=rows_map)
            if e:
                bump(ids[a], "20c", e)
                bump(ids[b], "20c", e)

    out: list[Target] = []
    for sid in ids:
        slot = hits.get(sid)
        if not slot:
            continue
        out.append(Target(sid=sid,
                          reasons=tuple(m for g in sorted(slot) for m in slot[g]),
                          gates=tuple(sorted(slot))))
    return out


def versions(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """一次渲染的**逐镜版本账**：缓存键 + 是不是现烧的 + 切片字节数。

    这三栏就是 ㉖ 的证据面：`cache_key` 变了＝这一镜的代码变了（键的输入含那一镜的 `code`）；
    `cached=False`＝它真的重烧了；字节数＝那一次切片的体积。字节取**文件本身**而不是账上
    那栏 `bytes`：那一栏在缓存命中那一路不存在（不在 `META_FIELDS` 里），拿它当证据会出现
    「命中的镜没有字节数」，于是「两次切片」这句话在一半的情况下没有第二个数。
    """
    out: dict[str, dict[str, Any]] = {}
    files = result.get("shot_files") if isinstance(result.get("shot_files"), Mapping) else {}
    for r in (result.get("shots") or []):
        if not isinstance(r, Mapping):
            continue
        sid = str(r.get("id") or "")
        if not sid:
            continue
        slot = files.get(sid) if isinstance(files.get(sid), Mapping) else {}
        clip = slot.get("clip")
        n: int | None = None
        try:
            n = int(clip.stat().st_size) if clip is not None else None
        except OSError:
            n = None
        out[sid] = {"cache_key": str(r.get("cache_key") or ""),
                    "cached": bool(r.get("cached")), "clip_bytes": n}
    return out


# ---------------------------------------------------------------------------
# 回执（㉖）：三出口 + 缺席，全部从账里读
# ---------------------------------------------------------------------------

def receipt(*, enabled: bool, cap: Mapping[str, Any], targets: Sequence[Target],
            outcomes: Mapping[str, Mapping[str, Any]],
            before: Mapping[str, Mapping[str, Any]],
            after: Mapping[str, Mapping[str, Any]],
            all_sids: Sequence[str],
            rounds: Sequence[Round] = (),
            self_check: Mapping[str, Any] | None = None,
            identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """㉖ 的回执：三出口逐个写明理由，**缺席单独列一栏**。

    为什么把「缺席」也显式列出来：三出口那张表容易读成「没有 reviewed 的就是 not_run」，
    而过了所有闸的镜**根本不在射程里**。列出来之后，「这一镜为什么没有那一栏」在回执上
    有唯一答案：它在缺席名单里，还是在三出口里——两句话不能混。

    ``identity`` 是 ⑲-b 那一栏（画面上那个东西是不是文案说的那个）。它**另起一格**而不是
    并进 ``shots``：那一格讲的是「这一镜改没改、重烧没重烧」，⑲-b 讲的是「画得像不像」，
    两件事各有各的证据面（缓存键 vs 盲描述）。混成一格后，「改过了」会被读成「现在像了」。
    """
    ident = dict(identity) if isinstance(identity, Mapping) else None
    tids = {t.sid for t in targets}
    shots: dict[str, Any] = {}
    for t in targets:
        o = outcomes.get(t.sid) or {}
        b, a = before.get(t.sid) or {}, after.get(t.sid) or {}
        shots[t.sid] = {
            "status": str(o.get("status") or "undetermined"),
            "why": str(o.get("why") or ""),
            "gates": list(t.gates),
            "reasons": list(t.reasons),
            "rounds": int(o.get("rounds") or 0),
            "cache_key_before": b.get("cache_key"),
            "cache_key_after": a.get("cache_key"),
            "clip_bytes_before": b.get("clip_bytes"),
            "clip_bytes_after": a.get("clip_bytes"),
            "reburned": bool(o.get("reburned")),
            "still_red": bool(o.get("still_red")),
        }
    collateral = sorted({c for r in rounds for c in r.collateral})
    return {
        "gate": GATE,
        "enabled": bool(enabled),
        "judge": dict(cap),
        "self_check": dict(self_check) if self_check else None,
        # ⑲-b 那一栏。「没开」在它自己的 `why` 里明写，这里不替它翻译。
        "identity": ident,
        "rounds_max": MAX_ROUNDS,
        "rounds": [{"index": r.index, "asked": list(r.asked),
                    "changed": list(r.changed), "reburned": list(r.reburned),
                    "collateral": list(r.collateral), "notes": list(r.notes)}
                   for r in rounds],
        "shots": shots,
        # 缺席：**过了所有闸**、压根不进射程的镜。它跟 `not_run` 是两个不同的世界。
        "absent": [sid for sid in all_sids if sid not in tids],
        # 别的镜被带重烧（T56 红样②）：这是「局部改只重烧受影响镜」那条既有保证的破口，
        # **必须阻断**——回看环不许自己把那条保证弄回去。
        "collateral": collateral,
        # 额度：预算是真钱。`preflight_credit` 落地时这一栏是它的入参（今天那个前置闸
        # 还不存在，如实记在回执里，不假装已经算进预估）。
        "budget": {"rounds_max": MAX_ROUNDS, "targets": len(targets),
                   "calls_max": len(targets) * MAX_ROUNDS},
        "counts": {
            "reviewed": sum(1 for v in shots.values() if v["status"] == "reviewed"),
            "not_run": sum(1 for v in shots.values() if v["status"] == "not_run"),
            "undetermined": sum(1 for v in shots.values() if v["status"] == "undetermined"),
            "absent": max(0, len(all_sids) - len(tids)),
        },
    }


# ---------------------------------------------------------------------------
# 单镜回看：把帧与五栏读数递过去，拿回一份**代码提案**
# ---------------------------------------------------------------------------

async def ask_frame(mod: Any, *, sid: str, frame: Any, columns: Mapping[str, Any] | None,
                    shot: Mapping[str, Any], reasons: Sequence[str], round_index: int
                    ) -> dict[str, Any]:
    """一次回看调用 → ``{"ok":…, "code":…, "why":…, "raw":…}``。

    **调用形状是契约，写在这里一处**（T55：只加一个 `gate:"26"` 的事件）。传过去的三样东西
    各有各的用处：``image`` 是**画面本身**（这一环存在的理由：几何读数说不清「画面上是一枚
    徽章还是一张脸」）、``columns`` 是母版量到的五栏原始读数、``reasons`` 是既有闸判它红/
    未判的原文——模型据此知道该改什么，而不是自己猜。词族那一栏按**镜级申报**取
    （`subject_terms` 今天不在 schema 里，缺就是缺，不许在这里编一份替模型填）。

    返回的 ``code`` 是**提案**，不是判决：它要过 `plan_code_patch` → 再归一 → 再渲染 →
    再回闸，四道都过了才算数。``ok=False`` 或 `code` 为空＝这一轮没换来东西，该镜回落
    ``not_run``（**不是** reviewed：账上没有第二次切片）。
    """
    fn = getattr(mod, FRAME_CHANNEL_ENTRY)
    try:
        got = await fn(gate=GATE, shot=sid, image=frame, columns=dict(columns or {}),
                       reasons=[str(x) for x in reasons], shot_spec=dict(shot),
                       round=round_index,
                       terms_field=SUBJECT_TERMS_FIELD,
                       subject_terms=(shot or {}).get(SUBJECT_TERMS_FIELD))
    except Exception as exc:      # noqa: BLE001 —— 一次调用炸了不该带塌整条片子
        return {"ok": False, "code": None, "raw": None,
                "why": f"回看调用失败（{type(exc).__name__}: {str(exc)[:200]}）"}
    if not isinstance(got, Mapping):
        return {"ok": False, "code": None, "raw": got,
                "why": f"回看返回的不是一条结果（{type(got).__name__}）"}
    code = got.get("code")
    code = code if isinstance(code, str) and code.strip() else None
    return {"ok": bool(got.get("ok", True)), "code": code, "raw": dict(got),
            "why": str(got.get("reason") or "")}


async def cached_frame(cache: Any, key: str, dst: Any) -> Any:
    """从**缓存**里取这一镜的 ``frame.png``（T56：不重渲一次就为了拿帧）。

    没有缓存（`cache=None`）或缓存里没有这一版 → `None`，并把后果如实往上传：拿不到帧的
    回看等于让模型闭着眼改代码，那一轮记跳过，**不许**退化成「没帧就只看读数改」——
    `frame.png` 写进这一环的输入就是因为它不可替代（P38：产了帧就必须有人读）。
    """
    if cache is None or not key:
        return None
    try:
        got = await cache.fetch(key, dst)
    except Exception:             # noqa: BLE001 —— 取不到就是取不到，如实回 None
        return None
    return (got or {}).get("frame.png")


# ---------------------------------------------------------------------------
# ⑲-b：画面上那个东西是文案说的那个吗（逐镜一次带图调用，单独开关）
# ---------------------------------------------------------------------------

async def _ask_identity(mod: Any, *, sid: str, frame: Any,
                        shot: Mapping[str, Any]) -> dict[str, Any]:
    """一次 ⑲-b 调用 → 同 ``judge_identity`` 的形状，另加 ``ok``。

    调用形状是契约（与 ``ask_frame`` 同规）：``shot`` 里有 ``subject`` 与 ``text``，
    那两样就是这一条要的全部输入。**不传词族**——盲描述那条路不需要它
    （见 :data:`SUBJECT_TERMS_FIELD` 那段注释）。
    """
    fn = getattr(mod, IDENTITY_ENTRY)
    try:
        got = await fn(shot=dict(shot), image=frame)
    except Exception as exc:      # noqa: BLE001 —— 一次调用炸了不该带塌整条片子
        return {"ok": False, "verdict": None, "reason": "", "subject": "", "by": "",
                "blind": None, "confidence": 0.0,
                "why": f"⑲-b 调用失败（{type(exc).__name__}: {str(exc)[:200]}）"}
    if not isinstance(got, Mapping):
        return {"ok": False, "verdict": None, "reason": "", "subject": "", "by": "",
                "blind": None, "confidence": 0.0,
                "why": f"⑲-b 返回的不是一条裁决（{type(got).__name__}）"}
    blind = got.get("blind")
    return {"ok": True, "verdict": got.get("verdict"),
            "reason": str(got.get("reason") or ""),
            "subject": str(got.get("subject") or ""),
            "by": str(got.get("by") or ""),
            "blind": dict(blind) if isinstance(blind, Mapping) else None,
            "confidence": float(got.get("confidence") or 0.0),
            "why": str(got.get("why") or "")}


async def identity_targets(shots: Sequence[Mapping[str, Any]], *, enabled: bool = False,
                           cache: Any = None, keys: Mapping[str, Any] | None = None,
                           work: Any = None,
                           ask: Callable[..., Awaitable[dict[str, Any]]] | None = None
                           ) -> dict[str, Any]:
    """⑲-b 逐镜身份裁决 → ``{"knob", "enabled", "why", "shots", "targets", "counts"}``。

    它补的是 ``collect_targets`` **进不去**的那一格：几何闸只看「有没有、多大、压没压住」，
    没有一条判据问得出「画面上那个东西是不是文案说的那个」。所以它是**第二个红源**，
    它的红镜与几何闸的红镜合到同一份 ``targets`` 里、走同一个回看环
    （不许为它另开一条修复的路——那正是 Q115 点名的清单漂移）。

    **开关关着就一声不响**：``enabled=False`` 时一条调用都不发，``why`` 里明写
    「这趟没开」，``counts.not_run`` 记满——「没开」不是「缺席」（缺席＝压根不需要），
    更不是「像」。回执上这三句话必须分得开（P5 / D-未判不绿）。

    **未判不进红镜名单**：几何闸的未判进名单是因为「没量到版面」值得看一眼；⑲-b 的未判
    是「这一趟没问出结论」，拿它当红镜会让模型在**没有任何理由**的情况下改代码，
    那正是回看环最贵的浪费。所以未判只落回执那一栏，如实写着「没量到」。
    """
    out: dict[str, Any] = {"knob": IDENTITY_KNOB, "enabled": bool(enabled), "why": "",
                           "shots": {}, "counts": {"unlike": 0, "like": 0,
                                                   "undetermined": 0, "not_run": 0}}
    targets: list[Target] = []
    if not shots:
        return {**out, "targets": targets}
    if not enabled:
        out["why"] = (f"⑲-b 这趟没开（`{IDENTITY_KNOB}` 没开，或整片开关 `{REVIEW_KNOB}` "
                      "没开）——这一栏对每一镜都**没有结论**，不许读成「画得像」")
        out["counts"]["not_run"] = len(shots)
        return {**out, "targets": targets}
    mod, why = frame_channel()
    if mod is None or not callable(getattr(mod, IDENTITY_ENTRY, None)):
        out["why"] = (why or f"帧通道 `{FRAME_CHANNEL_MODULE}` 没有 ⑲-b 入口 "
                             f"`{IDENTITY_ENTRY}`——「画面上那个东西是不是它」这句话今天"
                             "没人问过")
        for i, s in enumerate(shots):
            out["shots"][cp.shot_id(s, i)] = {"verdict": None, "why": out["why"]}
            out["counts"]["undetermined"] += 1
        return {**out, "targets": targets}
    for i, s in enumerate(shots):
        sid = cp.shot_id(s, i)
        subject = str(s.get("subject") or "").strip()
        if not subject:
            out["shots"][sid] = {
                "verdict": None, "subject": "",
                "why": "这一镜没申报 `subject`——⑲-b 无从比「像不像」（闸⑯ 会拦这一格）"}
            out["counts"]["undetermined"] += 1
            continue
        # 帧从**缓存**里取（与回看环同一处取法）：这一镜这一版的 `clip.mp4` 与
        # `frame.png` 是同一次切片一起存进去的，取帧不会多烧一帧。
        key = str((keys or {}).get(sid) or "")
        dst = (work / "identity" / sid) if work is not None else None
        frame = await cached_frame(cache, key, dst)
        if frame is None:
            out["shots"][sid] = {
                "verdict": None, "subject": subject,
                "why": ("这一镜的 `frame.png` 没取到（缓存里没有这一版，或这趟没挂缓存）"
                        "——身份没量过，记未判（**不**回落到「只看代码就说像」）")}
            out["counts"]["undetermined"] += 1
            continue
        got = await (ask or _ask_identity)(mod, sid=sid, frame=frame, shot=s)
        out["shots"][sid] = {"verdict": got.get("verdict"), "subject": subject,
                             "reason": got.get("reason"), "by": got.get("by"),
                             "blind": got.get("blind"),
                             "confidence": got.get("confidence"), "why": got.get("why")}
        if got.get("verdict") is True:
            out["counts"]["unlike"] += 1
            targets.append(Target(sid=sid, gates=("19b",), reasons=(
                f"闸⑲-b 画得像不像：{sid}：" + (got.get("reason")
                 or f"画面上那个东西不是文案说的「{subject}」"),)))
        elif got.get("verdict") is False:
            out["counts"]["like"] += 1
        else:
            out["counts"]["undetermined"] += 1
    return {**out, "targets": targets}


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

async def run(*, spec: dict[str, Any], first: Mapping[str, Any],
              render_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
              cache: Any = None, work: Any = None, enabled: bool = False,
              identity: bool | None = None,
              fps: int | None = None, apply_patch: bool = True,
              ask: Callable[..., Awaitable[dict[str, Any]]] | None = None
              ) -> dict[str, Any]:
    """跑一趟回看环。→ ``{"receipt":…, "spec":…, "render":…, "block": bool, "error": str,
    "still_red": [...]}``。

    ``first`` 是第一趟渲染的结果（`render_code_motion` 的返回），``render_fn`` 是**再烧一次**
    那趟（调用方传进来的闭包，内部带着同一个 `cache`——没改动的镜命中缓存、只有改到的镜真烧，
    这正是「只重烧该镜」在账上的形状）。``apply_patch=False`` 只判定不改 spec（自检用）。

    四处**不妥协**，每一处对应一条既有保证：
    ① 目标镜名单来自既有四条判据（不许另起一套）；
    ② 改动走 `code_patch` 那条腿（不许直接写 `spec["shots"][i]["code"]`）；
    ③ 只有**别的镜**被重烧时**阻断**（T56-②：「局部改只重烧受影响镜」不许被这条环弄回去）；
    ④ 裁决/帧通道缺一半时**阻断且出可读回执**（Q113），不许静默放行、不许降级成「就当没这步」。

    ``identity`` 是 ⑲-b 那一层开关（2026-10-17）：``None`` = 看 ``spec["identity"]``，
    显式 True/False 压过它；**而且 `enabled` 为假时它一律为假**（外层的 `review` 关着，
    就不存在「红镜能被处置」这件事）。回执 ``["identity"]["enabled"]`` 如实记最终取值。
    """
    fps = int(fps or spec.get("fps") or 30)
    shots_spec = list(spec.get("shots") or [])
    identity_on = bool(spec.get(IDENTITY_KNOB)) if identity is None else bool(identity)
    all_sids = [cp.shot_id(s, i) for i, s in enumerate(shots_spec)]
    rows: dict[str, Mapping[str, Any]] = {}
    for i, s in enumerate(shots_spec):
        sid = all_sids[i]
        got = layout_of(first, sid)
        rows[sid] = {"layout": got, "status": got.get("status")}
    scanned = {sid: rows[sid]["layout"] for sid in all_sids}
    orig_before = versions(first)

    targets = collect_targets(shots_spec, scanned=scanned, rows=rows, fps=fps)
    cap = await capability()
    self_check = (await self_test()) if cap["available"] else {
        "ok": None, "why": cap["why"], "detail": None}
    # ⑲-b：第二个红源。它必须在 `if not targets` 那条早退**之前**跑——一镜的几何闸全绿、
    # 画的那个东西却根本不是文案说的那个，正是这一条要抓的形状。开关是两层的：
    # 整片开关没开时这一条压根不发调用（算出来的红镜也无从处置，那笔钱等于白花）。
    ident: dict[str, Any] = await identity_targets(
        shots_spec, enabled=bool(enabled and identity_on), cache=cache,
        keys={sid: (orig_before.get(sid) or {}).get("cache_key") for sid in all_sids},
        work=work)
    if ident["targets"]:
        merged = {t.sid: t for t in targets}
        for t in ident["targets"]:
            prev = merged.get(t.sid)
            merged[t.sid] = (t if prev is None else
                             Target(sid=t.sid, reasons=prev.reasons + t.reasons,
                                    gates=tuple(sorted(set(prev.gates) | set(t.gates)))))
        targets = [merged[sid] for sid in all_sids if sid in merged]
    outcomes: dict[str, dict[str, Any]] = {}
    rounds: list[Round] = []

    def finish(ret: Mapping[str, Any] | None = None, *, extra: Mapping[str, Any] | None = None,
               block: bool = False, error: str = "",
               spec_out: dict[str, Any] | None = None) -> dict[str, Any]:
        r = receipt(enabled=enabled, cap=cap, targets=targets, outcomes=outcomes,
                    before=orig_before, after=versions(ret or first), all_sids=all_sids,
                    rounds=rounds, self_check=self_check, identity=ident)
        if extra:
            r.update(dict(extra))
        return {"receipt": r, "spec": spec_out if spec_out is not None else spec,
                "render": ret or first, "block": bool(block), "error": error,
                "still_red": [t.sid for t in targets
                              if (outcomes.get(t.sid) or {}).get("still_red")]}

    if not targets:
        # 没有红/未判的镜：这一条**不进射程**，回执里那一栏对每一镜都缺席。
        return finish({"absent_reason": ("本轮没有 ⑲-a/⑳(c)/㉕/㉑ 报红或未判的镜"
                                         + ("，⑲-b 也没判出不像的镜"
                                            if ident["enabled"] else
                                            "（⑲-b 这趟没开，它那一栏没有结论）"))})

    if not enabled:
        # 开关没开：三出口里的 `not_run`——明写「这趟没开回看」。**不是**缺席
        # （缺席是「压根不需要」），更不是通过。
        for t in targets:
            outcomes[t.sid] = {
                "status": "not_run", "rounds": 0, "reburned": False, "still_red": True,
                "why": (f"这一镜本轮报了 {'、'.join(t.gates)}，该走回看；整片开关 "
                        f"`{REVIEW_KNOB}` 这趟没开——**没有回看过**，"
                        f"这一栏不许折算成通过（Q112/Q113）"),
            }
        return finish({"note": f"回看没开（{REVIEW_KNOB}=false）：红/未判的镜记 not_run"})

    if not cap["available"]:
        # Q113 原文：判据不可用 → undetermined **并阻断出片**，绝不静默放行。
        for t in targets:
            outcomes[t.sid] = {
                "status": "undetermined", "rounds": 0, "reburned": False, "still_red": True,
                "why": (f"回看做不了：{cap['why']}（这一镜报了 {'、'.join(t.gates)}）"
                        f"——既不是「不需要回看」，也不是「回看过了」"),
            }
        r = finish({"note": "回看做不了，阻断出片（Q113）"}, block=True, error=cap["why"])
        raise ReviewBlocked(
            "闸㉖ 回看环：判据不可用，这一趟**做不了回看**，按 Q113 阻断出片（不是放行）\n"
            f"  · {cap['why']}\n"
            f"  · 要回看的镜：{'、'.join(t.sid for t in targets)}"
            + (f"\n  · 例如：{targets[0].reasons[0][:160]}" if targets[0].reasons else ""),
            r["receipt"])

    mod, _ = frame_channel()
    current: dict[str, Any] = dict(spec)
    shots_now = copy.deepcopy(shots_spec)
    result: Mapping[str, Any] = first
    tids = [t.sid for t in targets]
    live: dict[str, list[str]] = {t.sid: list(t.reasons) for t in targets}

    for r_i in range(1, MAX_ROUNDS + 1):
        rd = Round(index=r_i)
        if not live:
            break
        proposals: dict[str, str] = {}
        cur = versions(result)
        for sid in list(live):
            i = next((j for j, s in enumerate(shots_now) if cp.shot_id(s, j) == sid), None)
            if i is None:
                live.pop(sid, None)
                continue
            rd.asked.append(sid)
            # 帧从**缓存**里取：这一镜这一版的 `clip.mp4` 与 `frame.png` 是同一次切片
            # 一起存进去的（`cache.store(key, {...})`），所以「取帧」不会多烧一帧。
            key = str((cur.get(sid) or {}).get("cache_key") or "")
            dst = (work / f"review{r_i}" / sid) if work is not None else None
            frame = await cached_frame(cache, key, dst)
            if frame is None:
                rd.notes.append(
                    f"{sid}：这一镜的 `frame.png` 没取到（缓存里没有这一版，或这趟没挂缓存）"
                    f"——拿不到帧的回看不做，本轮跳过（**不**回落到「只看读数就改代码」）")
                live.pop(sid, None)
                continue
            got = await (ask or ask_frame)(
                mod, sid=sid, frame=frame, columns=five_columns(scanned.get(sid)),
                shot=shots_now[i], reasons=live[sid], round_index=r_i)
            if not got.get("code"):
                rd.notes.append(f"{sid}：本轮没改出东西"
                                f"（{got.get('why') or '模型没给出新代码'}）")
                continue
            proposals[sid] = str(got["code"])

        if not proposals:
            rounds.append(rd)
            break

        if apply_patch:
            edits = []
            for sid, code in proposals.items():
                i = next((j for j, s in enumerate(shots_now) if cp.shot_id(s, j) == sid), None)
                if i is None:
                    continue
                edits.append({"pointer": f"/shots/{i}/code", "shot": sid, "value": code})
            try:
                # 与 `patch_motion_code` **同一条腿**：指针形状、越界打回、镜号认领都在
                # `plan_code_patch` 一处判——这里自己写一遍 `shots[i]["code"]=…` 就会出现
                # 「人改的走校验、回看改的不走」这种最难发现的裂隙。
                patched, _acc = cp.plan_code_patch(dict(current), edits=edits)
            except ValueError as exc:
                for sid in proposals:
                    rd.notes.append(f"{sid}：提案落不下去（{str(exc)[:200]}）")
                rounds.append(rd)
                break
            shots_now = copy.deepcopy(list(patched.get("shots") or []))
            rd.changed = sorted(proposals)

        current = {**current, "shots": copy.deepcopy(shots_now)}
        reused = versions(result)
        result = await render_fn(current)
        after = versions(result)
        # 只重烧改到的那几镜：别的镜的缓存键必须一个都没动、而且都得是命中的。
        for sid, v in after.items():
            b = reused.get(sid) or {}
            if sid in rd.changed:
                if v.get("cache_key") != b.get("cache_key") and not v.get("cached"):
                    rd.reburned.append(sid)
                continue
            if v.get("cache_key") != b.get("cache_key") or not v.get("cached"):
                rd.collateral.append(sid)
        # 改完的镜拿新一轮的读数回闸：仍红就留着下一轮。
        new_scan = scanned_of(result)
        fresh = {t.sid: t for t in collect_targets(list(current.get("shots") or []),
                                                   scanned=new_scan,
                                                   rows=rows_for(result), fps=fps)}
        scanned = new_scan
        rows = rows_for(result)
        live = {sid: (list(fresh[sid].reasons) if sid in fresh else []) for sid in tids}
        live = {k: v for k, v in live.items() if v}
        rounds.append(rd)

        if rd.collateral:
            # T56-②：把别的镜也带重烧了 —— 「局部改只重烧受影响镜」这条既有保证被这一环
            # 弄破了。**必须红**，而且在继续烧之前就红（再烧下去只会更多）。
            for sid in tids:
                outcomes[sid] = {
                    "status": "undetermined", "rounds": rd.index,
                    "reburned": sid in rd.reburned, "still_red": True,
                    "why": f"第 {rd.index} 轮重烧牵连了别的镜（{'、'.join(rd.collateral)}）"
                           f"——「局部改只重烧受影响镜」这条保证被破了，回看环的结果不可信",
                }
            r = finish(result, extra={"note": "重烧牵连别的镜，阻断出片（T56-②）"},
                       block=True, error="重烧牵连别的镜", spec_out=current)
            raise ReviewBlocked(
                f"闸㉖ 回看环：第 {rd.index} 轮重烧把**别的镜**也带上了"
                f"（{'、'.join(rd.collateral)}）——局部改只该重烧改到的那几镜，"
                "这条既有保证不许被回看环弄破", r["receipt"])

    # 逐镜定出口：**账上有第二次切片**才算 reviewed。
    # 比的是 `orig_before`（**第一趟**那一份）而不是轮内滚动的那一份：滚动那一份每轮都会被
    # 覆盖成「上一轮之后」的样子，两轮下来首尾变成同一次读数，「缓存键变过没有」这件事在
    # 账上就消失了——而它正是 `reviewed` 与 `not_run` 的唯一分界。
    after = versions(result)
    # ⑲-b 复判：**改过的那几镜要再问一次**。没有这一步，一条 ⑲-b 红镜改完之后，回执上
    # 能说的只有「几何闸过了」——可那句话是关于尺寸与落位的，不是关于「像」的。
    # 拿它当 ⑲-b 的结论，就是一次「没量过写成量过」（D-未判不绿）。只复判**重烧过**的镜：
    # 没重烧的镜画面没变，再问一次只会得到一个一模一样的答案。
    ident_red = [t.sid for t in ident["targets"]]
    if ident_red:
        changed = [sid for sid in ident_red
                   if bool((after.get(sid) or {}).get("cache_key"))
                   and (after.get(sid) or {}).get("cache_key")
                       != (orig_before.get(sid) or {}).get("cache_key")
                   and not (after.get(sid) or {}).get("cached")]
        again = await identity_targets(
            [s for i, s in enumerate(shots_now) if cp.shot_id(s, i) in set(changed)],
            enabled=bool(changed), cache=cache,
            keys={sid: (after.get(sid) or {}).get("cache_key") for sid in all_sids},
            work=work) if changed else {"shots": {}, "counts": None}
        for sid, got in dict(again.get("shots") or {}).items():
            slot = ident["shots"].get(sid)
            if isinstance(slot, dict):
                slot["after"] = {"verdict": got.get("verdict"),
                                 "reason": got.get("reason"), "by": got.get("by"),
                                 "why": got.get("why")}
        for sid in ident_red:
            slot = ident["shots"].get(sid)
            if isinstance(slot, dict) and sid not in changed:
                slot["after"] = None
        ident["recheck"] = {"asked": changed, "counts": again.get("counts"),
                            "not_asked": [sid for sid in ident_red if sid not in changed],
                            "why": ("没被重烧的红镜不复发问：画面没变，再问一次只会得到"
                                    "同一个答案。「改过了」与「现在像了」是两句话，"
                                    "这一栏只在前者成立时才敢说后者。")}
    for sid in tids:
        b, a = orig_before.get(sid) or {}, after.get(sid) or {}
        reburned = bool(a) and a.get("cache_key") != b.get("cache_key") \
            and not a.get("cached")
        # ⑲-b 没复判出「像」的镜**不许被写成过了**：False 才清，True/None 都留在红里
        # （未判不绿——「没问出来」与「问出来是」是两句话）。
        after_v = ((ident["shots"].get(sid) or {}).get("after") or {}).get("verdict")
        still = (sid in live) or (sid in set(ident_red) and after_v is not False)
        why_ident = "" if sid not in set(ident_red) else (
            "；⑲-b 改后复判：" + {True: "仍不像", False: "已像"}.get(after_v, "未判（不许当像）"))
        if reburned:
            outcomes[sid] = {
                "status": "reviewed",
                "rounds": sum(1 for r in rounds if sid in r.reburned),
                "reburned": True, "still_red": still,
                "why": (f"改过代码并**只**重烧了这一镜：缓存键 {str(b.get('cache_key'))[:12]}…"
                        f" → {str(a.get('cache_key'))[:12]}…，切片 "
                        f"{b.get('clip_bytes')} → {a.get('clip_bytes')} 字节"
                        + why_ident
                        + ("；回闸仍红，按主合同打回该镜（不许「看过了就算过」）" if still
                           else "；回闸已过")),
            }
        else:
            outcomes[sid] = {
                "status": "not_run", "rounds": len(rounds), "reburned": False,
                "still_red": True,
                "why": ("回看跑了，但这一镜账上**没有第二次切片**"
                        f"（缓存键仍是 {str(b.get('cache_key'))[:12]}…、"
                        f"或第二次仍是命中缓存）——「回看过」这句话没有产物撑着，记 not_run"
                        + why_ident),
            }
    return finish(result, spec_out=current)


def rows_for(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """一次渲染的结果 → `collect_targets` 要的 `{sid: {"layout":…}}`（与 `scanned_of` 同源）。"""
    scan = scanned_of(result)
    return {sid: {"layout": lay, "status": lay.get("status")} for sid, lay in scan.items()}
