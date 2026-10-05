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
    lines = flow_lines(steps)
    return (
        "# 角色定义 (Role)\n"
        "你是一位能严格按既定流程完成同类任务的创作助手。以下流程沉淀自一次真实执行。\n\n"
        "# 何时使用 (When)\n"
        f"当用户诉求与下列原始诉求同类时使用：「{user_request or '（未记录）'}」。\n\n"
        "# 执行流程 (Workflow)\n"
        + "\n".join(lines)
        + "\n\n# 约束条件 (Constraints)\n"
        "- 严格按上述步骤顺序执行；上游已成功的产物不要重复执行。\n"
        "- 每一步的参数只列了关键项，缺失参数按用户当次诉求补全。\n"
        "- 执行中遇到与流水不符的现场，如实向用户说明，不要硬套流程。"
    )


_POLISH_SYSTEM = (
    "你是一名资深 Agent 工程师兼流程整理专家。把一次真实执行的工具调用流水，"
    "提炼为一篇可复用的 WORKFLOW SKILL.md（中文）。只输出 markdown 正文，"
    "不要 frontmatter、不要代码围栏。格式：# 角色定义 (Role)、# 何时使用 (When)、"
    "# 执行流程 (Workflow)（逐步，标注工具名与关键参数，可跳过的步骤标注「可跳过」）、"
    "# 约束条件 (Constraints)。执行流水的顺序就是步骤顺序，不要发明流水里没有的工具。"
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
            # 润色失败/输出过短/疑似只回了一句客气话：退回模板，不空手而归
            if len(body) >= 120 and "执行流程" in body:
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
