"""执行流水 → 技能（经验沉淀）：把一个 run 的工具调用流水提炼成 WORKFLOW SKILL。

数据源是 checkpoint 链上的工作消息（assistant.tool_calls 与 tool 结果按 call_id 配对）。
两档质量，功能永远可用：
* 确定性提取是骨架——步骤序列、关键参数、结果预览，纯代码零依赖；
* 有模型密钥时让 LLM 把骨架润成与 examples/skills 同格式的成文（角色/何时使用/
  执行流程/约束），失败或输出过短就退回模板——不因润色失败而拒绝沉淀。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .messages import Message

_PREVIEW_LEN = 160
_MAX_STEPS = 40


def _preview(content: Any) -> str:
    """工具结果的预览：压空白、截断——流水里只需要"这一步做成了什么"的信号。"""
    text = content if isinstance(content, str) else str(content)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_PREVIEW_LEN]


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def extract_steps(messages: list[Message] | None) -> list[dict[str, Any]]:
    """两遍扫描：先收全部工具结果（按 tool_call_id），再按序取 assistant 的
    tool_calls 与各自结果配对——结果永远出现在调用之后，单遍扫描会全部落空。"""
    by_call: dict[str, str] = {}
    for m in messages or []:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            by_call[m["tool_call_id"]] = _preview(m.get("content"))

    steps: list[dict[str, Any]] = []
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {"_": _short(args)}
            steps.append({
                "tool": str(fn.get("name") or ""),
                "args": args,
                "result": by_call.get(str(tc.get("id") or ""), ""),
            })
    return steps


def first_user_request(messages: list[Message] | None) -> str:
    for m in messages or []:
        if m.get("role") == "user":
            return _short(m.get("content"), 300)
    return ""


def _args_line(args: dict[str, Any]) -> str:
    if not args:
        return ""
    parts = [f"{k}={_short(v, 60)}" for k, v in list(args.items())[:6]]
    return "关键参数: " + "；".join(parts)


# 这些不是创作步骤：规划/问询/查询/技能加载/协作面调用，进技能只会教模型绕路
NON_FLOW_TOOLS = frozenset({
    "submit_plan", "confirm_plan", "ask_user",
    "render_status", "read_node_history", "load_skill",
    "read_memory", "update_memory",
    "send_message", "read_inbox", "spawn_subagent", "start_subagent",
    "team_status", "plan_editing_team", "list_tasks", "claim_task",
    "complete_task", "create_cron_job", "list_cron_jobs", "delete_cron_job",
})


def _is_failed(result: str) -> bool:
    """ToolError 的字符串形状以 Error 开头（tool.py），失败尝试不进流程。"""
    return result.startswith("Error")


def _fingerprint(step: dict[str, Any]) -> str:
    """动作指纹：工具 + 参数（排除 artifact_id——分叉换作用域不改变动作本身）。"""
    args = {k: v for k, v in (step.get("args") or {}).items() if k != "artifact_id"}
    return step["tool"] + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True)


def clean_steps(steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """净化单条 run 的流水，返回 (净步骤, 剔除数)：

    1) 剔除非创作步骤（规划/问询/查询/协作面）；
    2) 剔除失败尝试——改了三次才成的试错不进技能，成功做法才进；
    3) 紧邻同名且参数相同的连发合并为一次（纯重试）；参数不同的连发是合法多次使用，保留。
    """
    flow = [st for st in steps
            if st["tool"] not in NON_FLOW_TOOLS and not _is_failed(st["result"])]
    out: list[dict[str, Any]] = []
    dropped = len(steps) - len(flow)
    i = 0
    while i < len(flow):
        j = i
        while j + 1 < len(flow) and flow[j + 1]["tool"] == flow[i]["tool"]:
            j += 1
        streak = flow[i:j + 1]
        if len(streak) == 1 or len({_fingerprint(x) for x in streak}) > 1:
            out.extend(streak)
        else:
            out.append(streak[-1])          # 纯重试：只留最后一次
            dropped += len(streak) - 1
        i = j + 1
    return out, dropped


def merge_runs(runs_steps: list[list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    """会话级合并：跨 run 同名工具只保留最后一次——后一次代表用户修正后的做法；
    位置留在首次出现处，主流程顺序不乱。返回 (合并步骤, 被替换数)。"""
    out: list[dict[str, Any]] = []
    pos: dict[str, int] = {}
    replaced = 0
    for steps in runs_steps:
        for st in steps:
            t = st["tool"]
            if t in pos:
                out[pos[t]] = st
                replaced += 1
            else:
                pos[t] = len(out)
                out.append(st)
    return out, replaced


# 每个节点在流程里的"意图"——技能讲的是这个,不是某次执行的参数回放
NODE_INTENT = {
    "search_media": "用户没有上传素材时,按关键词在素材库检索",
    "load_media": "载入用户上传/检索到的素材(material_ids 来自会话附件或检索结果,不凭空编造)",
    "split_shots": "按镜头切分素材",
    "asr": "转写素材人声,得到带时间戳的语句",
    "correct_transcript": "修正常见转写错字(只改文本,不动时间轴)",
    "speech_rough_cut": "按语义选段做口播粗剪(keep_segments 保留完整语义段)",
    "understand_clips": "为每个镜头生成画面描述",
    "filter_clips": "按用户要求筛出符合的片段",
    "group_clips": "把片段排序分组,组织叙事结构",
    "script_template_rec": "推荐文案结构模板",
    "generate_script": "生成分组文案(custom_script 可传入用户风格要求)",
    "generate_voiceover": "按文案生成配音(用原声则跳过)",
    "select_BGM": "按用户指定的曲目/风格选配乐(query 用曲目名或风格词)",
    "generate_ai_transition": "在分组间生成转场片段",
    "transition_rec": "推荐转场方式",
    "text_rec": "推荐花字/字幕样式",
    "plan_timeline": "把片段/文案/声音/BGM 编排成时间线(比例/时长等来自用户诉求)",
    "plan_timeline_pro": "时间线编排(专业版,含转场与花字)",
    "plan_timeline_ai_transition": "时间线编排(含 AI 转场)",
    "render_video": "按时间线渲染出片",
    "render_web": "把网页渲染成视频",
    "plan_motion": "零素材出片的第一站：把每镜文案/版式/高亮词写成分镜 spec 并校验入库"
                   "（不烧像素，改文案只重调这一步）",
    "render_motion_video": "把分镜 spec 排成版式画面逐帧截屏出片（镜头长度归真实语音时长，"
                           "话没说完画面不切走）",
    "patch_motion_video": "对已出片的那一版下补丁：按命中表指针改某几镜的字段/删镜/调序，"
                          "只重烧受影响的镜，旧版本不动（要改全局版式或重做整片走 plan_motion → "
                          "render_motion_video）",
}

# 参数键 → 技能里的"推导说明"(这些值属于当次诉求,不该焊死在流程里)
VOLATILE_PARAM_HINTS = {
    "material_ids": "用户素材的 material_ids(见会话附件)",
    "artifact_id": "当前产物作用域",
    "target_duration_sec": "用户要求的目标时长",
    "speaker_ratio": "用户要求的出镜占比",
    "query": "用户指定的曲目或风格",
    "keep_segments": "按语义选定的段落(以当次 ASR 结果为准)",
    "keep_clips": "按用户要求筛出的片段(以当次 understand_clips 结果为准)",
    "custom_groups": "按叙事逻辑设计的分组(以当次片段为准)",
    "spec": "本轮的分镜 spec(文案按当次查到的资料重写,不复用历史 spec)",
    "bgm": "配乐引用：消息附件/曲库歌曲的 material_id，或 select_BGM 返回的 obj: 引用"
           "（曲库条目不挂会话，material_id 比 obj: 更稳）",
}


def _generalize_args(args: dict[str, Any]) -> list[str]:
    """把一次执行的参数改写成"这条该填什么"的说明:
    易变键给推导提示;其余值原样列出但剥掉对象键/长 JSON——它们是示例,不是流程。"""
    lines: list[str] = []
    for k, v in list(args.items())[:6]:
        if k in VOLATILE_PARAM_HINTS:
            lines.append(f"{k} = {VOLATILE_PARAM_HINTS[k]}")
            continue
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        text = text.replace("\n", " ")
        if "obj:users/" in text or len(text) > 90:
            text = text[:60] + "…(本次执行示例值,按当次诉求替换)"
        lines.append(f"{k} = {text}")
    return lines


def flow_lines(steps: list[dict[str, Any]]) -> list[str]:
    """给 LLM 看的流水原文（也是模板步骤的原料）。"""
    lines = []
    for i, st in enumerate(steps[:_MAX_STEPS], 1):
        line = f"{i}. 调用 \"{st['tool']}\""
        al = _args_line(st["args"])
        if al:
            line += f"（{al}）"
        if st["result"]:
            line += f" → {st['result']}"
        lines.append(line)
    if len(steps) > _MAX_STEPS:
        lines.append(f"…（共 {len(steps)} 步，仅列前 {_MAX_STEPS} 步）")
    return lines


def _fallback_body(steps: list[dict[str, Any]], user_request: str) -> str:
    """无 LLM 的泛化模板:主流程只讲意图与参数推导;本次具体值收进示例附录。"""
    lines = []
    appendix: list[str] = []
    for i, st in enumerate(steps[:_MAX_STEPS], 1):
        tool = st["tool"]
        intent = NODE_INTENT.get(tool, f"调用 {tool} 完成对应处理")
        lines.append(f"{i}. **{tool}** — {intent}")
        ga = _generalize_args(st["args"])
        for g in ga:
            lines.append(f"   - {g}")
        example = _args_line(st["args"])
        if example:
            appendix.append(f"- {tool}: {example}")

    appendix_text = ("\n".join(appendix) if appendix else "（无）")
    return (
        "# 角色定义 (Role)\n"
        "你是一位精通本系统剪辑工具链的创作助手。本技能沉淀自一次真实执行，"
        "给出的是同类任务的**推荐流程与参数推导方式**——具体素材、时长、比例"
        "永远以用户当次诉求为准。\n\n"
        "# 何时使用 (When)\n"
        f"当用户诉求与下述原始诉求同类（同类型视频再创作）时：「{user_request or '（未记录）'}」。\n\n"
        "# 执行流程 (Workflow)\n"
        + "\n".join(lines)
        + "\n\n# 参数如何随诉求变化\n"
        "- 素材、时长、占比、曲目/风格全部来自用户当次诉求与当次素材的分析结果；\n"
        "- 标注「按当次…」的参数禁止直接复用示例值；\n"
        "- 依赖关系由工具声明（required_nodes），上游未执行会被拦截器自动补齐。\n\n"
        "# 本次执行参数示例（仅参考，按当次诉求替换）\n"
        + appendix_text
        + "\n\n# 约束条件 (Constraints)\n"
        "- 严格按流程顺序执行；上游已成功的产物不要重复执行。\n"
        "- 执行中遇到与流程不符的现场，如实向用户说明，不要硬套流程。"
    )


_POLISH_SYSTEM = (
    "你是一名资深 Agent 工程师兼流程整理专家。把一次真实执行的工具调用流水，"
    "提炼为一篇**可复用**的 WORKFLOW SKILL.md（中文）。只输出 markdown 正文，"
    "不要 frontmatter、不要代码围栏。\n"
    "泛化是第一原则：素材 ID、片段 ID、ASR 段 id、具体时长/比例/曲目等"
    "会话特定值，在流程正文里一律改写成「按用户当次诉求确定」的推导说明"
    "（如 material_ids=用户素材列表；比例/时长=用户要求；BGM=用户指定曲目）；"
    "工具结果里的 JSON 一律不要引用。流程正文讲**意图与顺序**"
    "（每步先说什么目的，再列需要用户诉求决定的参数），不要发明流水里没有的工具，"
    "步骤顺序即流水顺序。\n"
    "结构：# 角色定义 (Role)、# 何时使用 (When)、# 执行流程 (Workflow)"
    "（逐步，可跳过的步骤标注「可跳过」）、# 参数如何随诉求变化、"
    "# 本次执行参数示例（仅参考，放流水里的具体值）、# 约束条件 (Constraints)。"
)


async def draft_body(steps: list[dict[str, Any]], user_request: str, *,
                     llm: Any = None) -> tuple[str, bool]:
    """生成 SKILL.md 正文。返回 (正文, 是否经过 LLM 润色)。"""
    flow_text = "\n".join(flow_lines(steps))
    if llm is not None:
        try:
            resp = await llm.complete([
                {"role": "system", "content": _POLISH_SYSTEM},
                {"role": "user",
                 "content": f"原始诉求：{user_request or '（未记录）'}\n\n执行流水：\n{flow_text}"},
            ])
            body = (getattr(resp, "content", "") or "").strip()
            # 润色失败/输出过短/没做到泛化（流程段还焊着素材对象键）/疑似敷衍：
            # 退回泛化模板，不空手而归
            flow_part = body.split("本次执行参数示例")[0]
            if (len(body) >= 120 and "执行流程" in body
                    and "obj:users/" not in flow_part):
                return body, True
        except Exception:
            pass
    return _fallback_body(steps, user_request), False


def sanitize_skill_name(base: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", (base or "").strip())
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:64] or "run_flow"


def pick_skill_name(base: str, existing: set[str]) -> str:
    """取一个未占用的技能名：base 空闲直接用，否则 -2…-9、再往后加时间后缀。"""
    name = sanitize_skill_name(base)
    if name not in existing:
        return name
    for n in range(2, 10):
        cand = f"{name}-{n}"
        if cand not in existing:
            return cand
    import time
    return f"{name}-{int(time.time()) % 100000}"
