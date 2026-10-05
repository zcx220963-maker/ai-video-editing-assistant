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
