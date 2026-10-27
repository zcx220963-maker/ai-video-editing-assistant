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

    # ---- 4. Node 存在且无依赖 ----
    from storyline_server.nodes.simple_nodes import SimpleFilmNode

    check(SimpleFilmNode.name == "simple_film", "Node 名是 simple_film")
    check(list(SimpleFilmNode.required_nodes) == [], "simple_film 无 required_nodes")
    check("mp4_url" in tuple(SimpleFilmNode.output_keys), "声明了 output_keys（预置产物可跳过）")

    # ---- 5. 两份 TOML 都暴露了 simple_film，且逐项相同 ----
    sets = {}
    for name in ("config.toml", "config.docker.toml"):
        cfg = tomllib.loads(src(f"examples/storyline/{name}"))
        nodes = cfg["local_mcp_server"]["available_nodes"]
        sets[name] = set(nodes)
        check("simple_film" in nodes, f"{name} 白名单含 simple_film")
    check(sets["config.toml"] == sets["config.docker.toml"], "两份白名单逐项相同")

    # ---- 6. HTTP 快线也走 pipeline ----
    api = src("agent_framework/simple_api.py")
    check("from storyline_server.simple import pipeline" in api, "simple_api 从 pipeline 取能力")
    check("from storyline_server.simple import film" not in api, "simple_api 不再直连 film 子模块")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
