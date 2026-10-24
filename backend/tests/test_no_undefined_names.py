# -*- coding: utf-8 -*-
"""静态体检：模块里不能有「用了但没定义」的名字。

为什么加（2026-10-24 真机）：删风格包那一段时，`_PREFLIGHT_CACHE` /
`_PREFLIGHT_TTL_SEC` 这两行正好在被删区间的末尾，被连带删掉了。
`py_compile` 只查语法，查不出这个；于是**启动正常、跑到 plan_motion 才炸**
`name '_PREFLIGHT_CACHE' is not defined`——用户看到的是「分镜工具报错」，
不是「代码有 bug」。

这个测试补的就是那一层：对关键模块做 AST 扫描，收集顶层定义（含 import、
函数参数、except as、global），再找所有 Name 引用，报出交集之外的。

**局限**：只查模块级名字，不查属性（`obj.missing`）与跨模块导入是否真存在。
但历史炸点全是这一类，先把它焊住。
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    "storyline_server/nodes/motion_plan.py",
    "storyline_server/motion/shot_writer.py",
    "storyline_server/motion/recipe_index.py",
    "storyline_server/motion/code_spec.py",
    "storyline_server/motion/art_director.py",
    "storyline_server/motion/design_review.py",
    "storyline_server/motion/selfcheck.py",
    "storyline_server/motion/code_render.py",
    "storyline_server/server.py",
    # 2026-10-24：`_env_kb` 用了 `os.environ` 但没 import os，app 起不来。
    # 这一层同样要焊住——`py_compile` 只查语法。
    "agent_framework/agent.py",
    "agent_framework/server.py",
]

_BUILTINS = set(dir(builtins))


def _collect(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Global):
            defined.update(node.names)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return defined | _BUILTINS, used


def main() -> None:
    fails = 0
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            print(f"  - {rel} 不存在，跳过")
            continue
        have, used = _collect(p)
        missing = sorted(n for n in used - have if not n.startswith("__"))
        if missing:
            fails += 1
            print(f"  ✗ {rel} 用了未定义的名字：{missing}")
        else:
            print(f"  ✓ {rel}")
    print(f"\n{'全过' if not fails else str(fails) + ' 个文件有问题'}（{len(TARGETS)} 个模块）")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
