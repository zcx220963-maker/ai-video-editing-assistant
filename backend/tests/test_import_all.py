"""跨模块导入体检：把每个 storyline 模块都 import 一遍。

为什么加（2026-10-24）：删风格包那批函数时，`motion_code_nodes` 里还留着
`from .motion_plan import refresh_style_template`——`py_compile` 查不出，
`test_no_undefined_names` 也只查模块内的名字。于是**分镜能出、渲染一点就炸**：
`cannot import name 'refresh_style_template'`。这个测试补的就是跨模块那一层。

**2026-10-24 修**：这份原先写成脚本（模块顶层就 `raise SystemExit`），
pytest 收集到它会直接 `INTERNALERROR`，把**整个测试套件**拖成跑不起来——
一个体检文件把体检本身废掉。现在改成普通测试函数：路径按 `__file__` 推，
不再硬编码 `/app`，宿主机与容器里都能跑。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # backend/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PKG = ROOT / "storyline_server"


def _modules() -> list[str]:
    out: list[str] = []
    for p in sorted(PKG.rglob("*.py")):
        # `.dg_*.py` 是开发期小脚本、`__pycache__` 是字节码，都不是模块
        if any(part == "__pycache__" or part.startswith(".") for part in p.parts):
            continue
        rel = p.relative_to(ROOT).with_suffix("")
        parts = [x for x in rel.parts if x and not x.startswith(".")]
        if not parts:
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(parts)
        if name and name not in out:
            out.append(name)
    return out


def test_all_modules_import() -> None:
    """每个模块都要 import 得动——跨模块的悬空引用只有这样才查得出来。"""
    modules = _modules()
    assert modules, f"没扫到任何模块（PKG={PKG}）"
    fails: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:                     # noqa: BLE001
            fails.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
    assert not fails, f"{len(fails)}/{len(modules)} 个模块导入失败：\n" + "\n".join(fails)
