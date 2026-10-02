"""中文名三源单汇验证（不联网）：display_name 只在一处作者、经一条通道到达主服务。

块 A 的地基：界面上不许出现 split_shots 这类机器名，但机器名又是**键**
（Store key、artifacts.node、DAG 契约字段、fork 的 rerun_nodes），改不得。
所以走双轨——机器名留在结构化字段里，给人看的那一份由三个源各自声明一次：

  1. Storyline 节点：``BaseNode.display_name``（唯一的节点元信息作者），
     经 MCP 标准的 ``title`` 字段到达主服务的 ``MCPTool.display_name``；
  2. 主服务本地工具：``Tool.display_name``（FunctionTool 用关键字参数声明）；
  3. 技能：SKILL.md frontmatter 的 ``display:`` → ``Skill.display``。

这里钉的是「声明齐全 + 通道打通 + 无第二份字面量」，出口替换器本身在
``tests/test_tool_catalog.py``。

运行：  python tests/test_display_name_sources.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.editing_contract import ContractSlot
from agent_framework.llm import ScriptedLLM
from agent_framework.memory import MemoryStore, register_memory_tools
from agent_framework.media_fetch import FetchPolicy
from agent_framework.skill import (
    SkillLoader,
    _parse_frontmatter,
    register_skill_tools,
)
from agent_framework.storage import build_storage
from agent_framework.subagent import make_spawn_tool
from agent_framework.team_tools import FunctionTool, register_team_tools
from agent_framework.tool import ToolRegistry
from agent_framework.tools.cron import CronScheduler, register_cron_tools
from agent_framework.tools.fetch_media import register_fetch_media_tools
from agent_framework.tools.file import register_file_tools, session_workspace_root
from agent_framework.tools.mcp import MCPTool, register_mcp_tools
from agent_framework.tools.runs import register_run_tools
from agent_framework.tools.web import register_web_tools
from agent_framework.orchestration import BaseNode, NodeState
from storyline_server.nodes import core_nodes

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def node_classes() -> list[type]:
    out = []
    for obj in vars(core_nodes).values():
        if (inspect.isclass(obj) and issubclass(obj, BaseNode)
                and obj is not core_nodes.StoryNode
                and getattr(obj, "name", "")):
            out.append(obj)
    return out


# ---- 源 1：Storyline 节点 ------------------------------------------------

def case_nodes() -> None:
    classes = node_classes()
    check(len(classes) == 19, f"扫描到 19 个剪辑节点类（实得 {len(classes)}）")
    missing = [c.__name__ for c in classes if not c.display_name.strip()]
    check(not missing, f"每个节点都声明了中文名（缺失：{missing}）")
    names = [c.name for c in classes]
    displays = [c.display_name for c in classes]
    check(len(set(names)) == len(names), "机器名互不重复")
    check(len(set(displays)) == len(displays), f"中文名互不重复（实得 {len(set(displays))} 种）")
    check(all(d != c.name for c, d in zip(classes, displays)),
          "中文名不等于机器名（不是把英文原样抄一遍）")
    # 实例级覆盖：与 name/description 同一条构造通路
    class _Probe(BaseNode):
        name = "probe_node"

        async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
            return {}

    check(_Probe().display_name == "", "BaseNode 默认无中文名（未声明即退回机器名）")
    check(_Probe(display_name="探针节点").display_name == "探针节点",
          "display_name 可按实例覆盖")
    # 节点工具注册通路：title=node.display_name（源码级钉，避免起真服务）
    src = (_Path(__file__).resolve().parent.parent / "storyline_server" / "server.py"
           ).read_text(encoding="utf-8")
    check("title=node.display_name" in src,
          "Storyline 把节点中文名放进 MCP 的 title 字段")


# ---- 源 2：主服务工具基类 + MCP 通道 -------------------------------------

class _FakeClient:
    server = "srv"

    async def list_tools(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        return self.defs

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> Any:
        return {"content": [{"type": "text", "text": "ok"}]}

    async def initialize(self, timeout: float = 30.0) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        return {}


async def case_tool_base() -> None:
    fn = FunctionTool("demo_tool", "演示", {"type": "object", "properties": {}},
                      lambda: "x", display_name="演示工具")
    check(fn.display_name == "演示工具" and fn.name == "demo_tool",
          "FunctionTool 以关键字参数声明中文名")
    check(FunctionTool("bare", "d", {}, lambda: "x").display_name == "",
          "FunctionTool 未声明时为空串")

    reg = ToolRegistry()
    reg.register(fn)
    reg.register(FunctionTool("other", "d", {}, lambda: "x"))
    check(reg.displays() == {"demo_tool": "演示工具"},
          "ToolRegistry.displays() 只收声明了的（未声明的不进表）")

    client = _FakeClient()
    titled = MCPTool(client, {"name": "split_shots", "title": "镜头切分",
                              "description": "d"}, timeout=1.0)
    untitled = MCPTool(client, {"name": "dag_contract", "description": "d"}, timeout=1.0)
    check(titled.display_name == "镜头切分", "MCPTool 从 tools/list 的 title 读到中文名")
    check(untitled.display_name == "", "远端没给 title 时中文名为空（不编造）")
    check(titled.name == "split_shots", "机器名仍是 title 之前那一个")

    prefixed = MCPTool(client, {"name": "split_shots", "title": "镜头切分"},
                       timeout=1.0, name_prefix="storyline_")
    check(prefixed.name == "storyline_split_shots" and prefixed.display_name == "镜头切分",
          "带前缀的注册名不影响中文名")

    # 注册通路：register_mcp_tools 必须把带 title 的定义整个交给 MCPTool
    client.defs = [{"name": "render_video", "title": "成片渲染",
                    "description": "d", "inputSchema": {"type": "object", "properties": {}}},
                   {"name": "not_whitelisted", "title": "不该出现"}]
    from agent_framework.tools.mcp import MCPServerConfig
    reg2 = ToolRegistry()

    await register_mcp_tools(
        reg2, client,
        MCPServerConfig(name="storyline", type="streamableHttp", url="http://x/mcp",
                        enabled_tools=["render_video"]),
        name_prefix=False)
    check(reg2.get("render_video") is not None
          and reg2.get("render_video").display_name == "成片渲染",
          "白名单注册链路把 title 带到注册表里的工具对象上")
    check(reg2.get("not_whitelisted") is None, "白名单外的工具不注册")


# ---- 源 3：技能 frontmatter ---------------------------------------------

def case_skills() -> None:
    meta, _body = _parse_frontmatter(
        "---\nname: x_skill\ndisplay: 示例技能\ndescription: d\n---\n正文")
    check(meta.get("display") == "示例技能", "frontmatter 解析认 display 键")

    loader_src = inspect.getsource(SkillLoader._to_skill)
    check('fm.get("display"' in loader_src, "技能行的 frontmatter.display 落进 Skill.display")

    root = _Path(__file__).resolve().parent.parent / "examples" / "skills"
    declared: dict[str, str] = {}
    for md in sorted(root.glob("*/SKILL.md")):
        fm, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
        if fm.get("name"):
            declared[fm["name"]] = fm.get("display", "")
    check(len(declared) >= 3, f"扫到 {len(declared)} 个技能")
    bare = [n for n, d in declared.items() if not d]
    check(not bare, f"每个 SKILL.md 都声明了 display（缺失：{bare}）")
    check(all(d != n for n, d in declared.items()), "技能中文名不与技能机器名相同")

    from agent_framework.skill import Skill
    s = Skill(name="x_skill", description="d", always=False, location="skills/x/SKILL.md",
              body="", display="示例技能")
    check("<display>示例技能</display>" in s.manifest_lines(),
          "技能清单把中文名一起注入（模型因此能用中文指名技能）")
    check("<display>" not in Skill(name="y", description="d", always=False,
                                   location="l", body="").manifest_lines(),
          "未声明 display 的技能不注入空标签")


# ---- 汇：装配出来的注册表逐个工具有中文名 --------------------------------

def case_assembled_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        registry = ToolRegistry()
        register_file_tools(registry, session_workspace_root(storage.workspace.root))
        register_web_tools(registry)
        register_fetch_media_tools(registry, storage=storage, policy=FetchPolicy())
        register_run_tools(registry, ContractSlot())
        registry.register(make_spawn_tool(ScriptedLLM([]), []))
        register_skill_tools(registry, SkillLoader(storage))
        register_memory_tools(registry, MemoryStore(storage))
        register_team_tools(registry, llm=ScriptedLLM([]), storage=storage)

        async def _runner(job) -> None:  # 只为构造 scheduler
            return None

        register_cron_tools(registry, CronScheduler(storage, runner=_runner))

        displays = registry.displays()
        unlabelled = [n for n in registry.tool_names if n not in displays]
        check(not unlabelled,
              f"装配后的 {len(registry.tool_names)} 个工具全部声明了中文名"
              f"（缺失：{unlabelled}）")
        check(len(set(displays.values())) == len(displays),
              f"{len(displays)} 个中文名互不重复")
        latin = {n: d for n, d in displays.items()
                 if all(ord(ch) < 0x2100 for ch in d)}
        check(not latin, f"中文名里没有纯英文串（漏网：{latin}）")
        # 关键不变量：schema 里给模型看的名字仍是机器名，双轨不能互相污染
        defs = registry.get_definitions()
        check(all(d["function"]["name"] == d["function"]["name"] for d in defs)
              and {d["function"]["name"] for d in defs} == set(registry.tool_names),
              "导出给模型的 schema 仍用机器名（中文名不进 function-call 协议）")


async def main() -> None:
    print("=== 源 1：Storyline 节点 display_name ===")
    case_nodes()
    print("=== 源 2：Tool.display_name + MCP title 通道 ===")
    await case_tool_base()
    print("=== 源 3：SKILL.md frontmatter display ===")
    case_skills()
    print("=== 单汇：装配后的注册表 ===")
    case_assembled_registry()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
