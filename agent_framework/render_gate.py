"""渲染前确认门：编排完成、正要渲染时，先把编排结果给用户看，等确认再渲。

**为什么需要它（用户原话的意思）。** 真机跑完一次才暴露问题：用户看到成片才发现
「最后一句被截断了」。用户的诉求是——渲染前就把编排结果（提取内容、切分、分镜、
画面、声音怎么排的）摆出来，让用户决定「改」还是「直接渲」；改则重做计划再弹一次，
直到满意为止。另有一条硬要求：「**不确认绝不渲染**」。

**为什么是一个独立模块。** 「要不要拦、拦什么、帧长什么样」是策略，
agent 循环只该负责「按策略挂起」。把它抽出来就能单独测，也避免继续往已经很长的
``agent.py`` 里塞判断。

**为什么复用 ask/审批机制。** 渲染确认本质上就是「问用户一道题」，与 ``ask_user``
同一形态：编号选项 + 推荐徽标 + 铅笔自定义。复用同一套挂起/续跑/落盘，
好处是「刷新后重弹」「多题分页」「自定义输入」这些能力一次性对两条路径都生效。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# 触发确认的节点名。做成常量而不是散落的字面量：将来要给别的节点加门只需改这里。
RENDER_NODE = "render_video"

# 渲染确认的选项。key 会作为 decision 回喂给服务端并进模型上下文，
# 所以用自解释的短词而不是 opt1/opt2。
CONFIRM_OPTION = "confirm_render"
ADJUST_OPTION = "adjust_plan"
# 时长冲突专用：保内容完整（放宽时长）/ 就按原时长截断
KEEP_FULL_OPTION = "keep_full_sentence"
TRUNCATE_OPTION = "truncate_as_planned"


def should_gate_render(tool_calls: Any, *, enabled: bool) -> bool:
    """这一批工具调用里有没有「要渲染」，且门是开着的。

    只看名字，不看参数：只要能渲就得先给用户看编排结果。
    """
    if not enabled:
        return False
    for tc in tool_calls or ():
        if getattr(tc, "name", None) == RENDER_NODE:
            return True
    return False


def _fmt(seconds: Any) -> str:
    try:
        return f"{float(seconds):.0f}s"
    except (TypeError, ValueError):
        return "?"


def duration_conflict(preview: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """成片时长会不会把话截断？会的话给出「放宽到多少秒才够」。

    这正是真机那次的问题：用户说 90 秒，模型发现最后一句金句会被截断，
    却选择直接截断、只在事后「告知偏差」。用户要求这种时候停下来问。

    判据取自预览里**已经算好**的那条告警，不在这里重算：
    两处各算一遍一定会漂移，而漂移的表现就是「界面说有冲突、题面说的却是另一个数」。
    """
    if not preview:
        return None
    tl = preview.get("timeline") or {}
    plan_dur = tl.get("duration")
    if not plan_dur:
        return None
    hit = next((str(w) for w in (preview.get("warnings") or [])
                if "截断" in str(w)), None)
    if hit is None:
        return None
    m = re.search(r"到\s*([0-9.]+)\s*s", hit)
    if not m:
        return None
    try:
        need = float(m.group(1))
    except ValueError:
        return None
    if need <= float(plan_dur):
        return None
    # 向上取整到 5 秒，留一点余量（句子结尾通常还有一点静音）
    suggest = int(need // 5 * 5 + 5)
    return {"planned": float(plan_dur), "needed": need, "suggest": suggest,
            "warning": hit}


def preview_summary(preview: Mapping[str, Any] | None) -> dict[str, str]:
    """预览 → 弹窗顶部那排小标签（用户不用开侧边栏也能看个大概）。"""
    if not preview or not preview.get("ready"):
        return {}
    s = preview.get("summary") or {}
    tl = preview.get("timeline") or {}
    out: dict[str, str] = {}
    if tl.get("duration"):
        out["时长"] = _fmt(tl["duration"])
    if s.get("resolution"):
        out["画面"] = str(s["resolution"])
    if s.get("groups"):
        out["段落"] = f"{s['groups']} 段"
    if s.get("clips"):
        out["镜头"] = f"{s['clips']} 个"
    audio = preview.get("audio") or {}
    if audio.get("original_audio"):
        out["声音"] = "保留原声"
    elif audio.get("voiceover_count"):
        out["声音"] = f"配音 {audio['voiceover_count']} 段"
    if audio.get("bgm"):
        out["配乐"] = str((audio["bgm"] or {}).get("filename") or "已选")[:18]
    if tl.get("subtitles"):
        out["字幕"] = f"{tl['subtitles']} 条"
    return out


def build_render_ask(preview: Mapping[str, Any] | None,
                     *, title: str = "") -> dict[str, Any]:
    """把编排预览变成一道确认题。

    普通情况两个选项就够：确认渲 / 我还要改。但**如果发现时长会把话截断**，
    就把它升级成一道专门的题（保完整 / 按原时长截断 / 自己指定秒数）——
    那时用户要决定的不是「渲不渲」，而是「宁可长一点还是宁可短一点」。
    这正是用户举的那个例子：模型不该替用户决定把话截掉。
    """
    warnings = list((preview or {}).get("warnings") or [])
    conflict = duration_conflict(preview)

    if conflict:
        planned, suggest = conflict["planned"], conflict["suggest"]
        head = (f"按现在的编排，成片 {planned:.0f} 秒，但内容到 "
                f"{conflict['needed']:.0f} 秒——最后的话会被截断。怎么办？")
        ask: dict[str, Any] = {
            "title": head,
            "options": [
                {"key": KEEP_FULL_OPTION,
                 "label": f"保内容完整，时长放宽到约 {suggest} 秒",
                 "description": "不截断句子；画面按内容补足，成片会长一点",
                 "recommended": True},
                {"key": TRUNCATE_OPTION,
                 "label": f"就按 {planned:.0f} 秒，末尾截断",
                 "description": "保持原定时长，接受最后一句被截断"},
            ],
            "allow_custom": True,
            "custom_hint": "其他（直接写你要多少秒，例如「100 秒」）",
            "warnings": warnings,
            "conflict": conflict,
        }
    else:
        head = title or "这是即将渲染的编排结果，确认开始渲染吗？"
        if warnings:
            # 有偏差时把偏差顶到问题里：那正是用户最需要据此决定的地方
            head = f"{head}（有 {len(warnings)} 处需要你留意）"
        ask = {
            "title": head,
            "options": [
                {"key": CONFIRM_OPTION, "label": "就这样，开始渲染",
                 "description": "按上面的编排渲染成片",
                 "recommended": not warnings},
                {"key": ADJUST_OPTION, "label": "我还要改",
                 "description": "先不渲染；告诉我要改什么，我重做一版编排再给你确认",
                 "recommended": bool(warnings)},
            ],
            "allow_custom": True,
            "custom_hint": "其他（直接写要改什么，例如「出镜比例调到 30%」）",
        }
        if warnings:
            ask["warnings"] = warnings

    summary = preview_summary(preview)
    if summary:
        ask["preview"] = summary
    return ask


def decision_is_confirm(decision: str) -> bool:
    """用户的心意是不是「原样渲染」。

    认不出来的 decision 一律当「不确认」——渲染是不可逆的贵操作，
    宁可多问一次也不要在没看清选择时直接开渲。
    注意时长冲突那两个选项都**不**算「原样渲」：选保完整要先放宽时长，
    选截断要按原时长重做，两者都得让模型再动一次参数，所以都返回 False
    （接下来做什么由 ``decision_text`` 说清）。
    """
    return str(decision or "").strip() == CONFIRM_OPTION


def decision_text(decision: str, custom: str = "") -> str:
    """把用户的选择变成一句给模型看的话（进 messages，作为这一轮的答复）。"""
    d = str(decision or "").strip()
    if custom.strip():
        return (f"用户对编排结果提出修改：{custom.strip()}\n"
                f"请不要渲染，先按这个要求重做编排（必要时重新出计划卡），"
                f"做完再把新的编排结果给用户确认。")
    if decision_is_confirm(d):
        return "用户已确认编排结果，可以开始渲染。"
    if d == ADJUST_OPTION:
        return ("用户选择「我还要改」，尚未确认渲染。请不要渲染，"
                "先问清楚要改什么（用选项式提问），再重做编排。")
    if d == KEEP_FULL_OPTION:
        return ("用户要求**保内容完整**：不要把话截断。请把成片时长放宽到能容纳全部内容"
                "（约到那条内容的结束时间），重做时间线让画面补足，"
                "然后把新的编排结果再交用户确认，确认后才渲染。")
    if d == TRUNCATE_OPTION:
        return ("用户接受按原定时长截断末尾。请按原时长重做/确认时间线；"
                "注意成片末尾会少一句话——这件事用户已经拍板，渲染前不必再问。")
    return f"用户对编排结果的选择是「{d or '未选择'}」，未确认渲染，不要开始渲染。"
