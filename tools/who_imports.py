# -*- coding: utf-8 -*-
"""用 AST 查「某个模块还有谁在用」——三种 import 形式全覆盖。

为什么不用 grep：grep 只能匹配你想到的写法。这一轮就因为漏了包内相对 import
（`from . import judge_channel`）而误删了 `judge_channel.py`，靠 `test_import_all`
才捞回来。AST 读的是语法树，`from . import X` / `from ..motion import X` /
`from storyline_server.motion import X` / `import storyline_server.motion.X`
以及 `motion.X.attr` 属性访问，全都能落到同一个答案上。

跑法（宿主机或容器都行）：
    python tools/who_imports.py motion.shot_writer motion.art_director ...
    python tools/who_imports.py --dead motion.simple_motion   # 只报「零引用」
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
PKG_ROOT = "storyline_server"


def module_of(path: Path) -> str:
    """文件路径 → 点分模块名（相对 backend/）。"""
    rel = path.relative_to(BACKEND).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve(module: str, node: ast.ImportFrom) -> list[str]:
    """`from X import a, b` → 被 import 的完整名列表（含 X 本身）。"""
    base = node.module or ""
    if node.level:
        # 相对 import：`from . import x`（level=1）从当前包往上退 level-1 层
        pkg = module.split(".")
        if module.endswith(".__init__"):
            pkg.pop()
        # 模块名本身可能就是一个模块而非包：退 level-1 层再取包
        pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
        base = ".".join(pkg[: len(pkg) - 1] + ([base] if base else []))
    out = [base] if base else []
    out += [f"{base}.{a.name}" if base else a.name for a in node.names]
    return out


def scan() -> dict[str, list[str]]:
    """{被 import 的模块名: [引用它的模块…]}"""
    users: dict[str, list[str]] = {}
    for p in BACKEND.rglob("*.py"):
        if any(part in {"__pycache__", "node_modules"} for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        me = module_of(p)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for name in resolve(me, node):
                    users.setdefault(name, []).append(me)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    users.setdefault(a.name, []).append(me)
    return users


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dead_only = "--dead" in sys.argv
    users = scan()
    bad = 0
    for target in args:
        full = f"{PKG_ROOT}.{target}" if not target.startswith(PKG_ROOT) else target
        hits = sorted(set(users.get(full, [])))
        # 同名子模块（`from . import x` 解析出来的短名）也算
        short = full.split(".")[-1]
        extra = sorted({u for k, v in users.items()
                        if k.endswith("." + short) or k == short
                        for u in v if u not in hits})
        if dead_only:
            status = "DEAD" if not hits and not extra else "LIVE"
            if status == "DEAD":
                bad += 1
            print(f"{status}  {target}  ← {hits or '（零引用）'}")
            continue
        print(f"\n{target}")
        if not hits:
            print("  （零引用）")
        for u in hits:
            print(f"  ← {u}")
    return 0 if bad == 0 or not dead_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
