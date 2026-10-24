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
    clean_steps, draft_body, extract_steps, first_user_request, merge_runs,
    pick_skill_name,
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
    print("\n[2] 无 LLM：泛化模板——流程讲意图,具体值降级为示例附录")
    steps = extract_steps(sample_messages())
    body, polished = await draft_body(steps, "把采访和空镜剪成 30 秒精华", llm=None)
    check(polished is False, "未走 LLM 润色")
    check("执行流程" in body and "角色定义" in body, "模板结构完整")
    check("load_media" in body and "render_video" in body, "每步带工具名")
    check("把采访和空镜剪成 30 秒精华" in body, "原始诉求在「何时使用」里")
    # 泛化:流程段讲意图,不焊死会话特定值
    flow_part = body.split("本次执行参数示例")[0]
    check("material_ids = 用户素材的 material_ids" in flow_part,
          "易变参数改写为推导说明")
    check("obj:users/" not in flow_part and "mat-0fe987" not in flow_part,
          "流程段不再焊着对象键/素材 ID")
    check("载入用户上传/检索到的素材" in flow_part, "每步带意图说明")
    check("本次执行参数示例" in body and "m1" in body,
          "具体值收进示例附录供参考")


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


def _step(tool, args=None, result="ok"):  # noqa: ANN001
    return {"tool": tool, "args": args or {}, "result": result}


async def part5_clean_and_merge() -> None:
    print("\n[5] 净化与合并：试错链收敛成干净主流程")
    raw = [
        _step("submit_plan", {"plans": []}),                       # 控制面:剔
        _step("ask_user", {}, "请你选择"),                          # 问询:剔
        _step("load_media", {"material_ids": ["m1"]}),
        _step("render_video", {"artifact_id": "a1"}, "Error: 编码失败"),   # 失败:剔
        _step("render_video", {"artifact_id": "a1"}, "Error: 编码失败"),   # 重试失败:剔
        _step("render_video", {"artifact_id": "a1"}),              # 重试成功:留
        _step("fetch_media", {"url": "u1"}, "ok1"),
        _step("fetch_media", {"url": "u2"}, "ok2"),                # 同名不同参:都留
    ]
    cleaned, dropped = clean_steps(raw)
    check([s["tool"] for s in cleaned] ==
          ["load_media", "render_video", "fetch_media", "fetch_media"],
          f"控制面/失败剔除、纯重试合并、不同参保留：{[s['tool'] for s in cleaned]}")
    check(dropped == 4, f"剔除计数（2 控制面 + 2 失败重试）={dropped}")
    check(cleaned[1]["args"] == {"artifact_id": "a1"},
          "重试合并保留的是成功那次的参数")

    # 会话级:run2 修正了 BGM 并重渲(换了产物作用域)
    run1 = [_step("load_media"), _step("select_BGM", {"query": "轻快"}),
            _step("render_video", {"artifact_id": "art-1"})]
    run2 = [_step("select_BGM", {"query": "钢琴版"}),
            _step("render_video", {"artifact_id": "art-2"})]
    merged, replaced = merge_runs([run1, run2])
    check([s["tool"] for s in merged] == ["load_media", "select_BGM", "render_video"],
          f"跨 run 同工具只留最后一次,位置在首次出现处：{[s['tool'] for s in merged]}")
    check(merged[1]["args"] == {"query": "钢琴版"}
          and merged[2]["args"] == {"artifact_id": "art-2"},
          "保留的是修正后的参数(新 BGM/新产物作用域)")
    check(replaced == 2, f"被替换计数={replaced}")


async def part6_generalization() -> None:
    print("\n[6] 泛化守卫:LLM 产出焊死具体值时退回模板")
    steps = extract_steps(sample_messages())

    class FakeLLM:
        async def complete(self, messages, tools=None):  # noqa: ANN001, ARG002
            class R:
                content = ("# 角色定义\n先载入素材 mat-0fe987 和 obj:users/u-x/c-y/mat-1.mp3\n"
                           "# 执行流程\n1. 调用 load_media 载入上面那两个素材\n" * 3)
            return R()

    body, polished = await draft_body(steps, "剪一条精华", llm=FakeLLM())
    check(polished is False, "焊死素材 ID 的 LLM 产出被判不合格")
    check("obj:users/" not in body.split("本次执行参数示例")[0],
          "退回的模板正文流程段干净")

    class GoodLLM:
        async def complete(self, messages, tools=None):  # noqa: ANN001, ARG002
            class R:
                content = ("# 角色定义 (Role)\n你是剪辑助手。\n" * 2
                           + "# 何时使用 (When)\n同类再创作诉求。\n"
                           + "# 执行流程 (Workflow)\n"
                           + "1. **load_media** — 载入用户素材(material_ids=用户素材列表)\n"
                           + "2. **render_video** — 按诉求渲染\n"
                           + "# 参数如何随诉求变化\n全部来自当次诉求。\n"
                           + "# 本次执行参数示例（仅参考）\n- load_media: m1\n"
                           + "# 约束条件 (Constraints)\n按顺序执行。\n")
            return R()

    body2, polished2 = await draft_body(steps, "剪一条精华", llm=GoodLLM())
    check(polished2 is True, "泛化合格的 LLM 产出被采用")


async def main() -> None:
    await part1_extract()
    await part2_fallback_draft()
    await part3_name_pick()
    await part4_checkpoint_roundtrip()
    await part5_clean_and_merge()
    await part6_generalization()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
