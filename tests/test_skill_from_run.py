# -*- coding: utf-8 -*-
"""执行流水沉淀为技能（skill_flows）的离线验证。

运行：  python tests/test_skill_from_run.py   # 全离线：内存替身 + 手工构造的执行消息

钉住四件事：
① extract_steps：assistant.tool_calls 与 tool 结果按 call_id 配对、按序输出、
   arguments JSON 容错（坏 JSON 落进 _ 而不是炸）；
② draft_body 无 LLM：退回确定性模板——含「执行流程」、每步带工具名、原始诉求在列；
③ pick_skill_name：占用名自动加后缀，不覆盖既有技能；
④ 端到端：真实 checkpoint 链（begin→complete）→ load → extract 步数正确。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.checkpoint import CheckpointManager
from agent_framework.skill_flows import (
    draft_body, extract_steps, first_user_request, pick_skill_name,
)
from agent_framework.storage import build_storage

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def sample_messages() -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "把采访和空镜剪成一条 30 秒的精华"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "load_media",
                          "arguments": "{\"material_ids\": [\"m1\"]}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "bad_json", "arguments": "{oops"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "load_media",
         "content": "已载入 1 个素材：采访.mp4（30.0s）"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c3", "type": "function",
             "function": {"name": "render_video",
                          "arguments": "{\"artifact_id\": \"art-1\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "c3", "name": "render_video",
         "content": "渲染完成，时长 30.0s"},
    ]


async def part1_extract() -> None:
    print("\n[1] extract_steps：配对、保序、坏 JSON 容错")
    steps = extract_steps(sample_messages())
    check([s["tool"] for s in steps] == ["load_media", "bad_json", "render_video"],
          f"三步按序提取：{[s['tool'] for s in steps]}")
    check(steps[0]["args"] == {"material_ids": ["m1"]},
          f"arguments JSON 解析成参数：{steps[0]['args']}")
    check(steps[1]["args"] == {}, "坏 JSON 兜成空参数而不炸")
    check("已载入" in steps[0]["result"] and "30.0s" in steps[2]["result"],
          f"结果按 call_id 配对到各自步骤（{steps[0]['result'][:14]}…）")
    check(first_user_request(sample_messages()).startswith("把采访"),
          "原始诉求取自第一条 user")


async def part2_fallback_draft() -> None:
    print("\n[2] 无 LLM：确定性模板兜底")
    steps = extract_steps(sample_messages())
    body, polished = await draft_body(steps, "把采访和空镜剪成 30 秒精华", llm=None)
    check(polished is False, "未走 LLM 润色")
    check("执行流程" in body and "角色定义" in body, "模板结构完整（角色/流程/约束）")
    check("load_media" in body and "render_video" in body, "每步带工具名")
    check("把采访和空镜剪成 30 秒精华" in body, "原始诉求在「何时使用」里")


async def part3_name_pick() -> None:
    print("\n[3] 命名：占用自动加后缀，不覆盖既有技能")
    existing = {"run_flow_ab12cd"}
    check(pick_skill_name("run_flow_ab12cd", existing) == "run_flow_ab12cd-2",
          "占用 → -2")
    check(pick_skill_name("我的 流程!", set()) == "我的_流程",
          "非法字符清洗净化")
    used_up = {"run_flow_ab12cd", *(f"run_flow_ab12cd-{n}" for n in range(2, 10))}
    late = pick_skill_name("run_flow_ab12cd", used_up)
    check(late.startswith("run_flow_ab12cd-") and late not in used_up,
          f"后缀用尽 → 时间后缀且不冲突（{late}）")


async def part4_checkpoint_roundtrip() -> None:
    print("\n[4] 端到端：真实 checkpoint 链 → load → 提取")
    storage = build_storage("memory")
    cm = CheckpointManager(storage)
    cp = await cm.begin("u1:c1", "剪一条 30 秒精华", sample_messages(),
                    run_id="run_skilltest")
    await cm.complete(cp, messages=sample_messages())
    back = await cm.load(cp.run_id)
    check(back is not None, "load 重建成功")
    steps = extract_steps(back.messages)
    check(len(steps) == 3, f"从链上提出 3 步（实际 {len(steps)}）")
    check(steps[0]["tool"] == "load_media", "步骤顺序与执行一致")


async def main() -> None:
    await part1_extract()
    await part2_fallback_draft()
    await part3_name_pick()
    await part4_checkpoint_roundtrip()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
