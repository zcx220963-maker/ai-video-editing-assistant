# -*- coding: utf-8 -*-
"""评审意见与机器判据对账（T0.5）：**评审说的，机器能不能证实**。

为什么要有这一层（2026-10-24）：

`design_review.review_stills` 是「一台看图模型说这一帧行不行」。它说的每一条红，
今天都直接进 `rewrite_failed` —— 一次写手调用、一次重渲、一次重评。而有些红是
**机器已经量过的**：评审说「看不见主体」，闸⑲-a 手上有主体件的占比；评审说
「糊在一起」，闸⑩ 手上有对比度比值。两边打架时今天没人管，**评审说了算**。

代价是双向的：评审误判 → 白烧一次写手，还可能把对的画面改坏；评审漏判 → 没人补。
这一层只处理前一半，而且**只在机器有读数时**才说话。

四条对账规则（照评审真实用词设计，不是照我们想象的用词）：

| 评审意见关键词 | 机器判据 | 冲突处理 |
|---|---|---|
| 看不见 / 缺失 / 空白 / 找不到 | 闸⑲-a `subject_share` ≥ 0.18 或 `subjects` > 0 | 记 **UNVERIFIED**，不重写 |
| 同明度 / 糊在一起 / 分不清 | 闸⑩ 最好的一档对比度 > 3:1 | 降级为 **warn** |
| 只有一块 / 一整块 | 闸④ `fill_span` > 60% | 降级为 **warn**（评审误判） |
| 没有先后 / 静止 / 一动不动 | 闸⑭ 帧差峰值 ≥ 地板线 | 降级为 **warn**（评审误判） |

**「没有读数」不等于「机器不同意」**。取不到读数（预览没回读版面、这一镜没抽动效）
时，这一条**原样保留**进重写清单，同时记一笔 `unread`——把「没量过」写成「量过且
没问题」，正是这个项目花三个月才发现的病（见 `code_spec.audit_gate_exits` 那段）。

纪律：**这一层不判「谁对」**。冲突时记未判、不重写，理由写进回执让人看见；
机器读数与评审都成立的情形（比如主体在但确实太小）本来就该重写，那种不叫冲突。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .code_spec import CONTRAST_MIN, SUBJECT_MIN_BBOX

#: 规则名 → 命中它的评审用词。**用词照真实评审回话抄的**（2026-10-24 那 12 条意见）：
#: 「只是一团模糊的灰蓝圆」「并排的扁平横条，像表格行」「内部与背景同明度」。
#: 第一版按「太空 / 只有主体」写，用真实意见测命中 0——判据要照**对方实际怎么说**设计。
RULES: dict[str, tuple[str, ...]] = {
    "subject_missing": (
        "看不见", "看不到", "缺失", "空白", "找不到", "没有画出来", "没有主体",
        "一片黑", "全黑", "空无一物", "什么都没有",
    ),
    "low_contrast": (
        "同明度", "糊在一起", "糊成一片", "分不清", "融在一起", "看不清",
        "没有对比", "亮度接近", "模糊",
    ),
    "single_block": (
        "只有一块", "一整块", "一块色块", "只有一个色块", "扁平横条", "像表格行",
        "只有一条", "一条横条",
    ),
    "static": (
        "没有先后", "静止", "一动不动", "看不出动", "没有动", "全程不动",
        "没有变化", "静态",
    ),
}

#: 闸④ 的「铺满」线：铺满 > 这一比例时，「只有一块」这条意见不成立。
FILL_FULL = 0.60
#: 闸⑭ 的帧差地板线（与 `motionsample.mjs` 同一档；读数由渲染侧回传，这里只用来判峰值）。
MOTION_PEAK_FLOOR = 1.5


def _hit(text: str, words: Sequence[str]) -> str:
    """命中哪个词（返回第一个命中的，便于回执说清「按哪句话判的」）。"""
    for w in words:
        if w and w in text:
            return w
    return ""


def _f(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _subject_reading(scanned: Mapping[str, Any], sid: str) -> dict[str, Any] | None:
    """闸⑲-a 的读数：`{subject_share, subjects, text_share}`；没扫过返回 None。"""
    row = scanned.get(sid) if isinstance(scanned, Mapping) else None
    if not isinstance(row, Mapping) or str(row.get("status")) != "scanned":
        return None
    got = row.get("scanned") if isinstance(row.get("scanned"), Mapping) else {}
    share = _f(got.get("subject_share"))
    if share is None:
        return None
    count = got.get("subjects")
    return {"subject_share": share,
            "subjects": count if isinstance(count, int) else None,
            "text_share": _f(got.get("text_share"))}


def _contrast_reading(contrast: Sequence[Mapping[str, Any]], sid: str) -> float | None:
    """闸⑩ 的读数：这一镜「在所有底色上最好的一档」比值；没判过返回 None。"""
    for row in contrast or []:
        if isinstance(row, Mapping) and str(row.get("id")) == sid:
            return _f(row.get("ratio"))
    return None


def reconcile(failed: Sequence[Mapping[str, Any]], *, shots: Sequence[Mapping[str, Any]],
              scanned: Mapping[str, Any] | None = None,
              contrast: Sequence[Mapping[str, Any]] | None = None,
              fills: Mapping[str, Any] | None = None,
              motion: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """逐条红镜对账 → ``{kept, unverified, downgraded, unread, notes}``。

    * ``kept``        仍要重写的（原样交回 `rewrite_failed`）
    * ``unverified``  评审与机器读数冲突、机器说有 → 记未判，**不重写**
    * ``downgraded``  机器读数说明评审误判 → 降级为提醒，**不重写**
    * ``unread``      这一条对应的机器判据没量过 → 照旧重写，但回执要写明「没对过账」
    * ``notes``       给回执的人话（每条冲突一句）

    取不到的读数就传 None：这一层**宁可不对账，也不拿假读数对账**。
    """
    out: dict[str, Any] = {"kept": [], "unverified": [], "downgraded": [],
                           "unread": [], "notes": []}
    for item in failed:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("shot") or "")
        text = f"{item.get('why') or ''} {item.get('fix') or ''}"
        rule = ""
        for name, words in RULES.items():
            if _hit(text, words):
                rule = name
                break
        if not rule:
            out["kept"].append(dict(item))
            continue

        verdict: dict[str, Any] | None = None
        if rule == "subject_missing":
            got = _subject_reading(scanned or {}, sid)
            if got is None:
                out["unread"].append({"shot": sid, "rule": rule,
                                      "reason": "这一镜的版面没回读到（闸⑲-a 没量过）"})
            elif (got["subjects"] or 0) > 0 and got["subject_share"] >= SUBJECT_MIN_BBOX:
                verdict = {
                    "kind": "unverified",
                    "machine": (f"闸⑲-a：主体件 {got['subjects']} 个，占画面带 "
                                f"{got['subject_share'] * 100:.0f}%（线 "
                                f"{SUBJECT_MIN_BBOX * 100:.0f}%）"),
                    "note": (f"{sid}：评审说「{_hit(text, RULES[rule])}」，而闸⑲-a 量到主体件占 "
                             f"{got['subject_share'] * 100:.0f}%——**评审与机器读数冲突**，"
                             "记未判、不重写（改了可能把对的画面改坏）"),
                }
        elif rule == "low_contrast":
            ratio = _contrast_reading(contrast or [], sid)
            if ratio is None:
                out["unread"].append({"shot": sid, "rule": rule,
                                      "reason": "这一镜的字色/底色没认出来（闸⑩ 没量过）"})
            elif ratio > CONTRAST_MIN:
                verdict = {
                    "kind": "downgraded",
                    "machine": f"闸⑩：最好的一档 {ratio:.1f}:1（下限 {CONTRAST_MIN:.0f}:1）",
                    "note": (f"{sid}：评审说「{_hit(text, RULES[rule])}」，而闸⑩ 量到对比度 "
                             f"{ratio:.1f}:1（线 {CONTRAST_MIN:.0f}:1）——评审误判，降级为提醒"),
                }
        elif rule == "single_block":
            fill = _f((fills or {}).get(sid))
            if fill is None:
                out["unread"].append({"shot": sid, "rule": rule,
                                      "reason": "这一镜的铺满度没量到（闸④ 只在烧像素时判）"})
            elif fill > FILL_FULL:
                verdict = {
                    "kind": "downgraded",
                    "machine": f"闸④：铺满 {fill * 100:.0f}%（线 {FILL_FULL * 100:.0f}%）",
                    "note": (f"{sid}：评审说「{_hit(text, RULES[rule])}」，而闸④ 量到画面铺满 "
                             f"{fill * 100:.0f}%——评审误判，降级为提醒"),
                }
        elif rule == "static":
            row = (motion or {}).get(sid) if isinstance(motion, Mapping) else None
            peak = None
            if isinstance(row, Mapping):
                for ev in (row.get("events") or []):
                    if isinstance(ev, Mapping):
                        got = _f(ev.get("peak_diff"))
                        if got is not None:
                            peak = got if peak is None else max(peak, got)
            if peak is None:
                out["unread"].append({"shot": sid, "rule": rule,
                                      "reason": "这一镜的动效没抽（闸⑭ 只在渲染时判）"})
            elif peak >= MOTION_PEAK_FLOOR:
                verdict = {
                    "kind": "downgraded",
                    "machine": f"闸⑭：帧差峰值 {peak:.2f}（地板线 {MOTION_PEAK_FLOOR}）",
                    "note": (f"{sid}：评审说「{_hit(text, RULES[rule])}」，而闸⑭ 量到帧差峰值 "
                             f"{peak:.2f}——评审误判，降级为提醒"),
                }

        if verdict is None:
            out["kept"].append(dict(item))
            continue
        record = {"shot": sid, "why": str(item.get("why") or ""),
                  "rule": rule, "machine": verdict["machine"]}
        out[verdict["kind"]].append(record)
        out["notes"].append(verdict["note"])
    return out


def summary_note(result: Mapping[str, Any]) -> str:
    """一句话交代这一轮对账（回执/计划卡用）；没冲突返回空串。"""
    unv = len(result.get("unverified") or [])
    down = len(result.get("downgraded") or [])
    unread = len(result.get("unread") or [])
    bits = []
    if unv:
        bits.append(f"{unv} 条评审意见与机器读数冲突（记未判、未重写）")
    if down:
        bits.append(f"{down} 条经机器读数判定为评审误判（降级为提醒）")
    if unread:
        bits.append(f"{unread} 条没有机器读数可对账（照旧重写）")
    return "；".join(bits)


def clean(text: str) -> str:
    """压空白（回执里那几行不要带换行）。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()
