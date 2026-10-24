"""SpawnTool / SubAgent 验证（不联网）：主派生子、子结果回传、子不可再派生。

运行：  python tests/test_subagent.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import Agent, AgentConfig
from agent_framework.hooks import CompositeHook
from agent_framework.llm import ScriptedLLM
from agent_framework.session import SessionManager
from agent_framework.subagent import SubAgentRunner, SpawnTool
from agent_framework.tool import ToolRegistry
from agent_framework.tools.file import register_file_tools, session_workspace_root

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 子 Agent 工具装配器：只给文件工具，不含 spawn
        def child_files(reg: ToolRegistry) -> None:
            register_file_tools(reg, session_workspace_root(tmp))

        # ---- 单元 1：子注册表结构上不含 SpawnTool ----
        runner = SubAgentRunner(ScriptedLLM([("answer", "done")]), [child_files])
        child_reg = runner._build_child_registry()
        check("spawn_subagent" not in child_reg, "子 Agent 注册表无 spawn_subagent（禁止递归派生）")
        check({"write_file", "read_file", "edit_file", "grep"} <= set(child_reg.tool_names), "子 Agent 具备文件工具")

        # SpawnTool 并发标记
        spawn = SpawnTool(runner)
        check(not spawn.concurrency_safe, "spawn_subagent 不可并发（子任务有副作用）")

        # ---- 单元 2：端到端主→子→回传 ----
        # 子 LLM：先调用 write_file 写文件，再给出结论
        child_llm = ScriptedLLM(
            steps=[
                ("tool", "write_file", {"path": "note.txt", "content": "sub result"}),
                ("answer", "已写入 note.txt，子任务完成。"),
            ]
        )
        child_runner = SubAgentRunner(child_llm, [child_files])
        spawn_tool = SpawnTool(child_runner)

        # 主 LLM：先派生子任务，拿到结论后整合成最终答复
        main_llm = ScriptedLLM(
            steps=[
                ("tool", "spawn_subagent", {"task": "把结果写入 note.txt", "context": "目标目录是工作区根"}),
                ("answer", "主 Agent 收到子结论并完成汇总。"),
            ]
        )
        main_reg = ToolRegistry()
        # 真实装配：主/子共用一个进程级工具实例，根目录由当前身份在调用时现取
        register_file_tools(main_reg, session_workspace_root(tmp))
        main_reg.register(spawn_tool)

        agent = Agent(
            llm=main_llm,
            registry=main_reg,
            session_manager=SessionManager(),
            hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6),
        )
        answer = await agent.handle("u", "c", "拆分一个子任务去写文件")

        check("主 Agent 收到子结论" in answer, f"主最终答复正常: {answer}")
        # 子继承主的身份作用域（use_identity_or_inherit）→ 写进同一会话沙箱，而非进程工作目录
        check(Path(tmp, "u_c", "_files", "note.txt").exists(),
              "子 Agent 的 write_file 落在继承来的会话沙箱")
        # 子结论应以 tool 结果回喂进主 LLM 的第 2 轮请求
        round2 = main_llm.calls[1]
        check(
            any(
                m.get("role") == "tool" and "子 Agent 结论" in str(m.get("content", ""))
                for m in round2
            ),
            "子结论以 tool 结果回喂主 Agent",
        )

        # ---- 单元 3：P5（2026-10-17 真机片）上限 10、描述说事实、撞限留摘要 ----
        from agent_framework.agent import _limit_summary
        check(runner.config.max_iterations == 10,
              f"子 Agent 上限 10 轮（实得 {runner.config.max_iterations}）——"
              "6 轮连「读 20 个文件汇总」都做不完，那类活会被派下来的前提是先能做完")
        desc = spawn.description
        check("不能执行任何命令" in desc and "没有 shell" in desc,
              "描述如实写「没有 shell」——真机上模型据此派过「执行 build_spec.py」这类必败的活")
        check("≤5 个文件" in desc and "10 轮" in desc,
              "描述里任务规模（≤5 个文件）与轮数上限（10 轮）都是事实")
        check("子 Agent 不能再派生孙 Agent" not in desc or "不会派生" in desc,
              "描述不再用「不能再派生孙 Agent」这种边角事实占位（能力边界写在前面）")
        check(_limit_summary(["read_file", "read_file", "read_file", "read_file", "grep"])
              == "过程摘要：已调用：read_file ×4、grep ×1；已写：无",
              f"摘要格式与回执约定同形（实得 {_limit_summary(['read_file'] * 4 + ['grep'])}）")
        check("write_file ×1" in _limit_summary(["write_file"]),
              "写过文件的摘要点得出「已写」")

        # 端到端：子 LLM 永远只发工具调用（脚本耗尽后重复最后一步）→ 撞上限 → 回执里
        # 必须同时有「撞限」这句事实与过程摘要，而不是只剩一句「已达最大迭代次数」。
        limit_llm = ScriptedLLM(steps=[("tool", "read_file", {"path": "nope.txt"})])
        limit_runner = SubAgentRunner(limit_llm, [child_files], max_iterations=2)
        got = await limit_runner.run("把文件读出来")
        check("已达最大迭代次数 2" in got,
              f"撞限的说明带实际上限数（实得 {got[:60]!r}）")
        check("过程摘要：已调用：read_file" in got and "已写：无" in got,
              f"撞限回执保留过程摘要（实得 {got[-80:]!r}）")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
