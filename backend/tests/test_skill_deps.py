"""技能依赖预检（`tools/skill_deps.py`）验证脚本（无需第三方依赖）。

运行：  python tests/test_skill_deps.py

覆盖开发文档《技能运行时通用化》§5 里点名的那几条：
标准库排除、同级脚本排除、import 名 → pip 名、平台锁定进 `unfixable`、
已装的（工作区 / 预装）不报、node 侧的内置与 scoped 包、以及「零依赖技能不提示安装」。

判定口径说明：`_py_installed` 会看**运行这份脚本的解释器**里有没有那个包，
所以依赖「本机没装某包」的断言不能用 numpy/scipy 这种机器上大概率有的名字。
这里把 `_py_installed` 换成受控替身（需要精确控制「缺什么」），
再单独用真实实现验一次「工作区里装了就算有」。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.tools import skill_deps as sd

_checks = 0
_fails = 0

_REAL_PY_INSTALLED = sd._py_installed


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        _fails += 1
        print(f"  ✗ {label}")


def pretend_missing(*present: str) -> None:
    """把「已装判定」换成受控替身：`present` 里的算装了，其余全算缺。"""
    have = set(present)

    def fake(name: str, root: Path | None) -> bool:
        return name in have

    sd._py_installed = fake


def restore() -> None:
    sd._py_installed = _REAL_PY_INSTALLED


class FakeSkill:
    """够 `deps_report` 用的最小技能：只要 `name` 与 `files`。"""

    def __init__(self, name: str, files: list[str]) -> None:
        self.name = name
        self.files = [{"relpath": r} for r in files]


async def main() -> None:
    print("=== 1. 标准库不报 ===")
    r = sd.report_from_sources({
        "a.py": "import json\nimport os.path\nfrom pathlib import Path\nfrom typing import Any\n",
    })
    check(r["missing_python"] == [], f"json/os/pathlib/typing 都不算缺: {r['missing_python']}")
    check(r["commands"] == [] and r["unfixable"] == [], "没有依赖 → 不生成任何安装命令")
    check(sd.format_report(r) == "", "没有依赖 → format_report 返回空串（回执里不出现这一段）")

    print("\n=== 2. 技能自己的同级脚本不报 ===")
    r = sd.report_from_sources({
        "main.py": "import 动效部件\nimport helper\nfrom pipeline import render\n",
        "动效部件.py": "X = 1\n",
        "helper.py": "Y = 2\n",
        # `from pipeline import render` 指的是包内这个目录，不是 PyPI 上的包
        "pipeline/render.py": "Z = 3\n",
    })
    check(r["missing_python"] == [], f"包内同级模块/目录不算第三方包: {r['missing_python']}")

    print("\n=== 3. import 名 ≠ pip 名 ===")
    pretend_missing()
    r = sd.report_from_sources({"pipeline/build.py": "import cv2\nimport scipy\n"})
    check(r["missing_python"] == ["cv2", "scipy"], f"两个都缺: {r['missing_python']}")
    check(r["renamed"] == {"cv2": "opencv-python-headless"},
          f"cv2 单列 pip 名（否则用户去 PyPI 搜 cv2 会扑空）: {r['renamed']}")
    cmd = r["commands"][0] if r["commands"] else ""
    check("opencv-python-headless" in cmd and " scipy" in cmd,
          f"命令里用 pip 名: {cmd}")
    check("cv2" not in cmd.split("--target")[-1], f"命令里不再出现 cv2: {cmd}")
    check("--target <工作区>/pylibs" in cmd, f"没有工作区时给占位符: {cmd}")

    print("\n=== 4. 平台锁定 → unfixable，且不给命令 ===")
    pretend_missing()
    r = sd.report_from_sources({"voice.py": "import mlx_audio\nimport soundfile\n"})
    check(any("mlx_audio" in u for u in r["unfixable"]), f"mlx_audio 进 unfixable: {r['unfixable']}")
    check(any("Apple Silicon" in u for u in r["unfixable"]), "unfixable 带理由，不是一句「缺依赖」")
    joined = " ".join(r["commands"])
    check("mlx" not in joined, f"不生成注定失败的安装命令: {joined}")
    check("soundfile" in joined, "同一份报告里可装的那部分照旧给命令")
    text = sd.format_report(r)
    check("跑不了" in text and "mlx_audio" in text, f"回执里明说跑不了: {text.splitlines()[-1][:40]}")

    print("\n=== 5. 已经装过的（工作区 / 镜像预装）不报 ===")
    restore()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / sd.PYLIBS_DIR / "cv2").mkdir(parents=True)
        check(sd._py_installed("cv2", root), "pylibs 里有目录 → 算已装")
        check(sd._py_installed("numpy", root), "numpy 在镜像预装名单里 → 算已装")
        check(not sd._py_installed("zzz_not_a_real_module", root), "真的没有 → 算缺")
        r = sd.report_from_sources({"a.py": "import cv2\nimport numpy\n"}, root)
        check(r["missing_python"] == [], f"已装的不进报告: {r['missing_python']}")

    print("\n=== 6. node 侧：内置、相对路径、scoped 包 ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        r = sd.report_from_sources({
            "render.mjs": (
                "import fs from 'node:fs';\n"
                "import path from 'path';\n"
                "import { helper } from './helper.mjs';\n"
                "import * as THREE from 'three';\n"
                "import { renderMedia } from '@remotion/renderer';\n"
                "const puppeteer = require('puppeteer-core');\n"
            ),
            "helper.mjs": "export const helper = 1;\n",
        }, root)
        check(r["missing_node"] == ["@remotion/renderer", "puppeteer-core", "three"],
              f"只报真·第三方包: {r['missing_node']}")
        # `node:test` 剥掉前缀就是 "test" —— 漏了它会把内置测试运行器报成 npm 包
        r3 = sd.report_from_sources({
            "x.mjs": "import test from 'node:test';\nimport assert from 'node:assert';\n",
        }, root)
        check(r3["missing_node"] == [], f"`node:test` 是内置的，不报: {r3['missing_node']}")
        cmd = next((c for c in r["commands"] if c.startswith("npm ")), "")
        check("--prefix " + str(root / sd.NODELIBS_DIR) in cmd, f"落点在工作区 nodelibs: {cmd}")
        # 装过之后不再报
        (root / sd.NODELIBS_DIR / "node_modules" / "three").mkdir(parents=True)
        r2 = sd.report_from_sources({"render.mjs": "import * as THREE from 'three';"}, root)
        check(r2["missing_node"] == [], f"nodelibs/node_modules 里有 → 不再报: {r2['missing_node']}")

    print("\n=== 7. Python 2 老脚本不能让整包预检失灵 ===")
    pretend_missing()
    r = sd.report_from_sources({"old.py": "print 'hello'\nimport scipy\n"})
    check(r["missing_python"] == ["scipy"], f"ast 解析失败退回正则仍能扫出依赖: {r['missing_python']}")

    print("\n=== 8. 相对导入不算依赖 ===")
    r = sd.report_from_sources({"pkg/mod.py": "from . import sibling\nfrom ..base import x\n"})
    check(r["missing_python"] == [], f"from . import … 不报: {r['missing_python']}")

    print("\n=== 9. deps_report：优先读已解包的磁盘目录 ===")
    pretend_missing()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        base = root / "skills" / "demo"
        (base / "pipeline").mkdir(parents=True)
        (base / "pipeline" / "build.py").write_text("import soundfile\n", encoding="utf-8")
        (base / "render.mjs").write_text("import 'three';\n", encoding="utf-8")
        (base / "notes.md").write_text("import cv2\n", encoding="utf-8")
        skill = FakeSkill("demo", ["pipeline/build.py", "render.mjs", "notes.md"])

        async def _boom(rel: str) -> bytes:      # 有磁盘副本时不该再走对象存储
            raise AssertionError(f"不该读对象存储：{rel}")

        report = await sd.deps_report(skill, root, base=base, reader=_boom)
        check(report["scanned"] == 2, f"只扫 .py/.mjs（.md 不算源码）: scanned={report['scanned']}")
        check(report["missing_python"] == ["soundfile"], f"Python 侧: {report['missing_python']}")
        check(report["missing_node"] == ["three"], f"Node 侧: {report['missing_node']}")
        check(report["skill"] == "demo", "报告带上技能名（回执标题要用）")

    print("\n=== 9b. 项目型技能自己装的 node_modules 也算「已装」 ===")
    restore()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        base = root / "skills" / "shotcraft"
        (base / "template" / "node_modules" / "remotion").mkdir(parents=True)
        (base / "template" / "src").mkdir(parents=True)
        (base / "template" / "src" / "index.tsx").write_text("import {x} from 'remotion';\n",
                                                             encoding="utf-8")
        skill = FakeSkill("shotcraft", ["template/src/index.tsx"])
        report = await sd.deps_report(skill, root, base=base)
        check(report["missing_node"] == [],
              f"template/node_modules 里装了 → 不再报缺（否则装完还催着装）: {report['missing_node']}")

    print("\n=== 9c. 模板字符串拼出来的动态导入不算包名 ===")
    restore()
    r = sd.report_from_sources({"scripts/gen.mjs": (
        "const d = await import(`@demos/${name}.mjs`);\n"
        "const e = await import(`./x/${name}`);\n"
    )})
    check(r["missing_node"] == [], f"`@demos/${{name}}` 这种不报: {r['missing_node']}")

    print("\n=== 10. deps_report：没有磁盘副本时走 reader（上传时那条路）===")
    pretend_missing()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill = FakeSkill("demo", ["a.py", "b.mjs"])
        blobs = {"a.py": b"import pedalboard\n", "b.mjs": b"import 'three';\n"}

        async def _reader(rel: str) -> bytes:
            return blobs[rel]

        report = await sd.deps_report(skill, root, reader=_reader)
        check(report["missing_python"] == ["pedalboard"], f"reader 路径: {report['missing_python']}")
        check(report["missing_node"] == ["three"], f"reader 路径 node: {report['missing_node']}")

    print("\n=== 11. 零依赖技能不提示安装（dashi-animationstyle 那种）===")
    restore()
    with tempfile.TemporaryDirectory() as tmp:
        r = sd.report_from_sources({
            "anim.mjs": "import fs from 'node:fs';\nimport { a } from './util.mjs';\n",
            "util.mjs": "export const a = 1;\n",
        }, Path(tmp))
        check(r["commands"] == [] and r["unfixable"] == [], "一个命令都不生成")
        check(not sd.has_work(r), "has_work=False → 回执里不会出现依赖预检段")

    print("\n=== 12. libs_env：注入但不覆盖 ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = sd.libs_env(root, {"PYTHONPATH": "/opt/base"})
        parts = env["PYTHONPATH"].split(sd.os.pathsep)
        check(parts[0] == str(root / "pylibs"), f"pylibs 前置: {env['PYTHONPATH']}")
        check(parts[-1] == "/opt/base", "原有 PYTHONPATH 保留在后面")
        node = env["NODE_PATH"].split(sd.os.pathsep)
        check(str(root / "nodelibs" / "node_modules") in node, f"NODE_PATH 指向 node_modules: {env['NODE_PATH']}")

    restore()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
