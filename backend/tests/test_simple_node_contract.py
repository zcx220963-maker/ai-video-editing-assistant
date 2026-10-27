# -*- coding: utf-8 -*-
"""R18 的判据：simple 线只有一个实现、一个 Node、六个工具名不变。

    docker exec creation-app python tests/test_simple_node_contract.py

纯静态 + 注册表断言，不跑模型、不渲染。
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_fail = 0
_pass = 0

SIX = ("make_short_video", "plan_short_film", "reshoot_shot",
       "render_short_film", "export_film", "list_short_films")


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


def main() -> None:
    # ---- 1. pipeline 是唯一入口，且导出契约字段 ----
    pipeline = BACKEND / "storyline_server" / "simple" / "pipeline.py"
    check(pipeline.exists(), "存在 simple/pipeline.py")
    text = pipeline.read_text(encoding="utf-8")
    for name in ("def run(", "def receipt(", "OUTPUT_FIELDS"):
        check(name in text, f"pipeline 提供 {name}")

    # ---- 2. short_video 不再直连 simple 子模块（AST 断言，防字符串绕过）----
    tree = ast.parse(src("agent_framework/tools/short_video.py"))
    direct = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("storyline_server.simple"):
            # 只允许 `from storyline_server.simple import pipeline`
            if node.module == "storyline_server.simple":
                names = {a.name for a in node.names}
                if names - {"pipeline"}:
                    direct.append(sorted(names - {"pipeline"}))
            else:
                direct.append(node.module)
    check(not direct, f"short_video 只 import pipeline（越界的：{direct}）")

    # ---- 3. 六个工具名仍在 ----
    sv = src("agent_framework/tools/short_video.py")
    for name in SIX:
        check(name in sv, f"工具名 {name} 仍在")

    # ---- 4. 三个原子节点存在，依赖链正确 ----
    #
    # 2026-10-27：原来这里只有一个黑盒 `simple_film`（内部调 pipeline.run 干完全程），
    # 设计 §21「流程原子化」没做到（差距登记在 §13 X1）。现在拆成
    # plan → write → render 三个节点，依赖用 required_nodes 声明。
    from storyline_server.nodes.simple_nodes import (
        PlanShortFilmNode, RenderShortFilmNode, WriteShortShotsNode)

    check(PlanShortFilmNode.name == "plan_short_film", "分镜节点名 plan_short_film")
    check(list(PlanShortFilmNode.required_nodes) == [], "分镜是起点：无 required_nodes")
    check(WriteShortShotsNode.name == "write_short_shots", "写镜节点名 write_short_shots")
    check(list(WriteShortShotsNode.required_nodes) == ["plan_short_film"],
          f"写镜依赖分镜（{WriteShortShotsNode.required_nodes}）")
    check(RenderShortFilmNode.name == "render_short_film", "渲染节点名 render_short_film")
    check(list(RenderShortFilmNode.required_nodes) == ["write_short_shots"],
          f"渲染依赖写镜（{RenderShortFilmNode.required_nodes}）")
    check("mp4_url" in tuple(RenderShortFilmNode.output_keys),
          "渲染声明 output_keys（预置产物可跳过）")
    check("mp4_url" in tuple(RenderShortFilmNode.ephemeral),
          "mp4_url 是 ephemeral：会过期的直链不进共享库")

    # ---- 4b. 渲染是分钟级，必须登记进 LONG_RUNNING_NODES ----
    from storyline_server.server import LONG_RUNNING_NODES

    check("render_short_film" in LONG_RUNNING_NODES,
          "render_short_film 在 LONG_RUNNING_NODES（走提交+轮询，不让一次往返等一部片子）")

    # ---- 5. 两份 TOML 都暴露了三个节点，且逐项相同 ----
    sets = {}
    for name in ("config.toml", "config.docker.toml"):
        cfg = tomllib.loads(src(f"examples/storyline/{name}"))
        nodes = cfg["local_mcp_server"]["available_nodes"]
        sets[name] = set(nodes)
        for n in ("plan_short_film", "write_short_shots", "render_short_film"):
            check(n in nodes, f"{name} 白名单含 {n}")
        check("simple_film" not in nodes, f"{name} 不再有黑盒 simple_film")
    check(sets["config.toml"] == sets["config.docker.toml"], "两份白名单逐项相同")

    # ---- 5b. 同名只能有一个实现：原生工具不再注册这两个名字 ----
    check("registry.register(PlanShortFilmTool" not in sv,
          "plan_short_film 只由节点提供（原生工具不再注册）")
    check("registry.register(RenderShortFilmTool" not in sv,
          "render_short_film 只由节点提供（原生工具不再注册）")

    # ---- 6. HTTP 快线也走 pipeline ----
    api = src("agent_framework/simple_api.py")
    check("from storyline_server.simple import pipeline" in api, "simple_api 从 pipeline 取能力")
    check("from storyline_server.simple import film" not in api, "simple_api 不再直连 film 子模块")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
