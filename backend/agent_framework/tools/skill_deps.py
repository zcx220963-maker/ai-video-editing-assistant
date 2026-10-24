"""技能依赖预检：扫技能包里的 import，和「容器已装 + 工作区已装」对账，给出确切的安装命令。

## 为什么要有它（2026-10-23）

`run` 工具现在能跑技能脚本了，但用户从 GitHub 拖进来的技能**从不声明依赖**：
`huashu-art-motion` 的脚本里写着 `import cv2`，而镜像里没有 cv2。原来的结局是
`ModuleNotFoundError: No module named 'cv2'`，模型接着开始猜——`pip install cv2`
（这个包不存在，真名是 opencv-python）、`python3 -m pip install opencv`、
换着写法重试，一轮轮空烧。

这里做的事很朴素：**在跑之前就把「缺什么、跑哪条命令」摆出来**。命令是算出来的，
不是让模型猜的。

## 判定口径

| 来源 | 怎么算「已装」 |
|---|---|
| Python | `importlib.util.find_spec(name)` 命中，或 `<工作区>/pylibs/<name>` 存在 |
| Node | `<工作区>/nodelibs/node_modules/<name>` 存在 |

Node 那一格只能退化成「看目录」：静态阶段没法跑 `require.resolve`。代价是**可能多报**
——多报的后果只是让 agent 多跑一条幂等安装，比漏报（撞 `Cannot find module` 再瞎猜）好。

## 三个排除项

1. **标准库**（`sys.stdlib_module_names`）——`import json` 不该让人去装 json。
2. **技能自己的同级脚本**——技能里 `import 动效部件` 指的是包内那个 `动效部件.py`，
   不是 PyPI 上的包。这条不排除的话，几乎每个技能都会被报一堆假缺失。
3. **平台锁定包**（`mlx` / `bpy`）——Linux 上根本装不了或装了也跑不动，进 `unfixable`，
   **不给命令**。给一条注定失败的命令，等于把「瞎试」从运行时搬到安装时。

## 工作区布局

```
<工作区>/pylibs/            pip install --target 落点
<工作区>/nodelibs/node_modules/   npm install --prefix 落点
```

两者都在命名卷上（compose 的 `app-runtime:/app/.runtime`），**重建容器不丢**；
又都不在系统 site-packages 里，**不污染镜像**。`run` 工具把它们注入子进程的
`PYTHONPATH` / `NODE_PATH`，所以技能脚本一行都不用改。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

#: 装包落点（相对工作区根）。`run` 的 `_check_install` 只认这两个目录。
PYLIBS_DIR = "pylibs"
NODELIBS_DIR = "nodelibs"
#: npm 的包实际落在 `<prefix>/node_modules/` 下——`NODE_PATH` 必须指到这一层，
#: 指到 `<prefix>` 是找不到的（2026-10-23 写这一版时踩到的第一个坑）。
NODELIBS_MODULES = "node_modules"

#: 参与扫描的源码扩展名。`.js` 不收：这几个外部技能里的 `.js` 基本都是浏览器端脚本，
#: 满屏 `document` / CDN 裸名，扫进来的假缺失比真缺失多。
_SCAN_EXTS = (".py", ".mjs", ".cjs")

#: 单个文件最多读这么多字节再扫——技能包里偶尔夹着几 MB 的生成代码，全读没必要。
_MAX_SOURCE_BYTES = 512 * 1024

#: 平台锁定：装了也没用。**必须给理由**，不然用户只会看到「缺依赖」却装不上。
PLATFORM_LOCKED: dict[str, str] = {
    "mlx": "Apple Silicon 专用（Linux 没有 wheel），除非技能自己换掉语音方案",
    "mlx_audio": "Apple Silicon 专用（mlx 生态），同上",
    "bpy": "Blender 运行时（约 300 MB，且常与系统库冲突），3D 部分在容器里跑不了",
    "bmesh": "Blender 内置模块（只能跟着 bpy 一起有），同上",
    "torch_mps": "Apple Silicon 专用",
}

#: import 名 → pip 包名。**只有不一致的才要登记**：`import cv2` 对应的包叫
#: opencv-python，直接 `pip install cv2` 会 404——这是模型最容易猜错的一个。
PY_PACKAGE_ALIASES: dict[str, str] = {
    "cv2": "opencv-python-headless",
    "PIL": "Pillow",
    "skimage": "scikit-image",
    "fontTools": "fonttools",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "pkg_resources": "setuptools",
}

#: 镜像里已经装好的（`requirements.txt` 那几行）。写死一份是为了**报告稳定**：
#: `find_spec` 在容器里当然也能命中，但预检脚本有时会在宿主机上跑（单测、CI），
#: 宿主机没装 numpy 就会把「容器其实有」的包报成缺失，用户照着装一遍纯属浪费。
#:
#: 2026-10-23 实测校正：`requests` **不在**镜像里（`docker exec … import requests`
#: 返回 False），原来凭印象写进来，正好把 huashu 真正缺的那一个盖掉——
#: 预检的第一要务是别漏报，宁可让 `find_spec` 去判。
_PREINSTALLED = frozenset({
    "numpy", "PIL", "playwright", "huggingface_hub",
})

#: Node 内置模块。`node:fs` 与裸 `fs` 两种写法都要放行。
#: `test` 是 `node:test`（内置测试运行器）——**剥掉 `node:` 前缀后就是个普通单词**，
#: 漏了它就会把 `import test from 'node:test'` 报成「缺 npm 包 test」。
#: 2026-10-23 在 video-shotcraft 上实测抓到的。
_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "sqlite", "stream", "string_decoder", "sys", "test",
    "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
})

_PY_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b", re.M)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)", re.M)
_NODE_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|\bimport\s+)['"]([^'"\n]+)['"]""")


# --------------------------------------------------------------------- 扫描

def _py_imports(text: str) -> set[str]:
    """一份 .py 里 import 了哪些**顶层**模块名。

    先走 `ast`（准确，能区分 `if TYPE_CHECKING:` 之外的注释/字符串）；解析失败
    再退回正则——技能包里确实有 Python 2 的脚本，`ast.parse` 直接 SyntaxError，
    不能因为一份老脚本就让整包预检失灵。
    """
    names: set[str] = set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for m in _PY_FROM_RE.finditer(text):
            names.add(m.group(1).split(".")[0])
        for m in _PY_IMPORT_RE.finditer(text):
            for part in m.group(1).split(","):
                head = part.strip().split(" ")[0].split(".")[0]
                if head:
                    names.add(head)
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # `from . import x`：包内相对导入，不装东西
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _node_imports(text: str) -> set[str]:
    """一份 .mjs/.cjs 里 import / require 了哪些包（已剥掉 `node:` 前缀）。"""
    out: set[str] = set()
    for m in _NODE_IMPORT_RE.finditer(text):
        spec = m.group(1).strip()
        if spec.startswith("node:"):
            spec = spec[5:]
        # 模板字符串拼出来的动态导入（`import(\`@demos/${x}\`)`）不是包名，
        # 报出来只会让人去装一个不存在的包。
        if "${" in spec or "`" in spec:
            continue
        out.add(spec)
    return out


def _is_relative(spec: str) -> bool:
    return spec.startswith((".", "/")) or "://" in spec


def _node_pkg(spec: str) -> str:
    """`@scope/pkg/sub` → `@scope/pkg`；`three/examples/x` → `three`。"""
    parts = spec.split("/")
    if spec.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


# --------------------------------------------------------------------- 判定

def _py_installed(name: str, root: Path | None) -> bool:
    if name in _PREINSTALLED:
        return True
    if root is not None and (root / PYLIBS_DIR / name).exists():
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _node_installed(name: str, root: Path | None,
                    bases: tuple[Path | None, ...] = ()) -> bool:
    """node 包算不算「已装」。

    除了统一的 `<工作区>/nodelibs/node_modules/`，还要看**技能自己的目录**：
    项目型技能（自带 `package.json`，如 video-shotcraft 的 `template/`）必须把
    `node_modules` 装在自己那一层，装到 `nodelibs` 里 bundler 不认。只看 nodelibs
    的话，装成功了报告还在说「缺」，等于把用户往重复安装上推。
    """
    if root is not None and (root / NODELIBS_DIR / NODELIBS_MODULES / name).exists():
        return True
    for base in bases:
        if base is None:
            continue
        try:
            if (base / NODELIBS_MODULES / name).exists():
                return True
            # 深度 2：`template/node_modules/x`、`a/b/node_modules/x`
            for depth in ("*/", "*/*/"):
                if next(base.glob(f"{depth}{NODELIBS_MODULES}/{name}"), None) is not None:
                    return True
        except OSError:
            continue
    return False


def _libs_path(root: Path | None) -> tuple[str, str]:
    """命令里用的落点。没有工作区（上传时预检）就给占位符，别编一个假路径。"""
    if root is None:
        return "<工作区>/pylibs", "<工作区>/nodelibs"
    return str(root / PYLIBS_DIR), str(root / NODELIBS_DIR)


# --------------------------------------------------------------------- 报告

def report_from_sources(sources: Mapping[str, str], root: Path | None = None, *,
                        base: Path | None = None,
                        project_dirs: Iterable[str] = ()) -> dict[str, Any]:
    """纯函数：给定「relpath → 源码文本」，算出依赖报告。

    `root` 为 `None` 时命令里用 `<工作区>/…` 占位（上传那一刻还没有会话工作区）。
    `base` 是技能解包后的目录，用来认「项目型技能自己装的 node_modules」。
    `project_dirs` 是技能里带 `package.json` 的子目录——node 包要装进它们各自的目录，
    装到统一的 `nodelibs` 里 bundler 找不到，报告里要提醒这一条。
    """
    py_names: set[str] = set()
    node_names: set[str] = set()
    py_local: set[str] = set()
    node_local: set[str] = set()
    for rel, text in (sources or {}).items():
        low = str(rel).lower()
        if not isinstance(text, str):
            continue
        if low.endswith(".py"):
            py_names |= _py_imports(text)
            # 包内的东西一律不算第三方：`import 动效部件` 指的是同目录那个 `.py`，
            # `from pipeline import render` 指的是同级的 `pipeline/` 目录。
            # 不认这两条的话，几乎每个技能都会被报一堆假缺失（实测 huashu 会报
            # `pipeline`、`lib` 这种纯目录名）。
            parts = Path(rel).parts
            py_local.add(Path(rel).stem)
            py_local.update(parts[:-1])
        elif low.endswith((".mjs", ".cjs")):
            node_names |= _node_imports(text)
            stem = Path(rel).stem
            node_local |= {stem, Path(rel).name}

    missing_py = sorted(
        n for n in py_names
        if n not in sys.stdlib_module_names and n != "__future__"
        and n not in py_local and not _py_installed(n, root)
    )
    missing_node = sorted(
        n for n in {_node_pkg(s) for s in node_names if not _is_relative(s)}
        if n and n not in _NODE_BUILTINS and n not in node_local
        and not _node_installed(n, root, (base,))
    )

    # 平台锁定的不进安装命令：一条注定失败的命令只会把「瞎试」从运行时搬到安装时。
    unfixable: list[str] = []
    todo_py: list[str] = []
    for name in missing_py:
        why = PLATFORM_LOCKED.get(name)
        if why:
            unfixable.append(f"{name}（{why}）")
        else:
            todo_py.append(name)
    todo_node: list[str] = []
    for name in missing_node:
        why = PLATFORM_LOCKED.get(name)
        if why:
            unfixable.append(f"{name}（{why}）")
        else:
            todo_node.append(name)

    py_dir, node_dir = _libs_path(root)
    commands: list[str] = []
    if todo_py:
        pkgs = [PY_PACKAGE_ALIASES.get(n, n) for n in todo_py]
        commands.append(f"python3 -m pip install --target {py_dir} " + " ".join(pkgs))
    if todo_node:
        commands.append(f"npm install --prefix {node_dir} " + " ".join(todo_node))

    # import 名与 pip 名不一致的那几个，单列出来——否则用户看到 `cv2` 去搜 PyPI 会扑空。
    # 名字相同的不要列：`soundfile（pip 名：soundfile）` 这种只是噪音。
    renamed = {n: PY_PACKAGE_ALIASES[n] for n in todo_py
               if PY_PACKAGE_ALIASES.get(n) and PY_PACKAGE_ALIASES[n] != n}
    return {
        "missing_python": missing_py,
        "missing_node": missing_node,
        "commands": commands,
        "unfixable": unfixable,
        "renamed": renamed,
        "project_dirs": [str(d) for d in (project_dirs or ())],
    }


async def _collect_sources(skill: Any, base: Path | None,
                           reader: Callable[[str], Awaitable[bytes]] | None) -> dict[str, str]:
    """把技能里要扫的源码读成文本：优先读**已解包的磁盘目录**，其次走 reader。"""
    rels: list[str] = []
    for f in (getattr(skill, "files", None) or []):
        rel = str((f or {}).get("relpath") or "").replace("\\", "/")
        if rel.lower().endswith(_SCAN_EXTS):
            rels.append(rel)
    sources: dict[str, str] = {}
    for rel in rels:
        text: str | None = None
        if base is not None:
            path = base / rel
            try:
                if path.is_file() and path.stat().st_size <= _MAX_SOURCE_BYTES:
                    text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = None
        if text is None and reader is not None:
            try:
                raw = await reader(rel)
            except Exception:            # noqa: BLE001 - 单个附件读不到就跳过
                continue
            if raw and len(raw) <= _MAX_SOURCE_BYTES:
                text = raw.decode("utf-8", "replace")
        if text is not None:
            sources[rel] = text
    return sources


async def deps_report(skill: Any, root: Path | None, *,
                      base: Path | None = None,
                      reader: Callable[[str], Awaitable[bytes]] | None = None) -> dict[str, Any]:
    """技能依赖预检。

    - `base`：已解包的技能目录（`run` 解包完就是它）——**优先**从这里读，
      免得同一批附件再从对象存储拉一遍。
    - `reader`：`async (relpath) -> bytes`，上传时没有磁盘副本，走这条。
    """
    sources = await _collect_sources(skill, base, reader)
    # 带 package.json 的子目录：node 包得装在项目自己那一层（见 `_node_installed`）
    dirs = sorted({
        str(Path(rel).parent).replace("\\", "/")
        for rel in (str((f or {}).get("relpath") or "").replace("\\", "/")
                    for f in (getattr(skill, "files", None) or []))
        if Path(rel).name == "package.json" and str(Path(rel).parent) not in (".", "")
    })
    report = report_from_sources(sources, root, base=base, project_dirs=dirs)
    report["scanned"] = len(sources)
    report["skill"] = str(getattr(skill, "name", "") or "")
    return report


def has_work(report: Mapping[str, Any]) -> bool:
    return bool(report.get("commands") or report.get("unfixable"))


def format_report(report: Mapping[str, Any], *, indent: str = "") -> str:
    """把报告排成 agent 能直接照抄的几行。没有要说的就返回空串。"""
    if not has_work(report):
        return ""
    lines: list[str] = []
    bad = list(report.get("unfixable") or [])
    locked = {b.split("（")[0] for b in bad}
    missing_py = [n for n in (report.get("missing_python") or []) if n not in locked]
    missing_node = [n for n in (report.get("missing_node") or []) if n not in locked]
    renamed = dict(report.get("renamed") or {})
    if missing_py:
        shown = [f"{n}（pip 名：{renamed[n]}）" if n in renamed else n for n in missing_py]
        lines.append("缺 Python 包：" + "、".join(shown))
    if missing_node:
        lines.append("缺 Node 包：" + "、".join(missing_node))
    cmds = list(report.get("commands") or [])
    if cmds:
        lines.append("装一次就好（装进工作区，重建容器不丢；`run` 已把落点加进 "
                     "PYTHONPATH / NODE_PATH，技能脚本不用改）：")
        lines.extend("  " + c for c in cmds)
    # 项目型技能：上面那条 `--prefix nodelibs` 对它其实不生效（bundler 不看 NODE_PATH），
    # 得指到项目目录。不说这一句，模型会照抄命令、装完仍然 `Cannot find module`。
    proj = list(report.get("project_dirs") or [])
    if missing_node and proj:
        lines.append("注意：这个技能自带项目目录 " + "、".join(f"`{d}/`" for d in proj[:3])
                     + "，它的 node 包要装在项目自己的目录里（`npm install --prefix "
                       "skills/<技能名>/" + proj[0] + "`），装到 nodelibs 里 bundler 找不到。")
    bad = list(report.get("unfixable") or [])
    if bad:
        lines.append("这些在本容器里跑不了，别试安装：" + "；".join(bad))
    return "\n".join(indent + ln for ln in lines)


def iter_scan_rels(files: Iterable[Mapping[str, Any]]) -> list[str]:
    """（给测试/排查用）列出会被扫描的 relpath。"""
    return [str(f.get("relpath") or "") for f in files
            if str(f.get("relpath") or "").lower().endswith(_SCAN_EXTS)]


def libs_env(root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """要注入子进程的两个路径变量（`run._child_env` 用它）。

    `NODE_PATH` 同时给 `<root>/nodelibs` 和 `<root>/nodelibs/node_modules`：
    前者是 `--prefix`，后者才是包真正躺着的地方。两个都给，免得有人把包直接解到
    `nodelibs/` 下时又找不到。
    """
    env = dict(base or {})
    py = str(root / PYLIBS_DIR)
    node_root = str(root / NODELIBS_DIR)
    node_mods = str(root / NODELIBS_DIR / NODELIBS_MODULES)
    old_py = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = py + (os.pathsep + old_py if old_py else "")
    env["NODE_PATH"] = os.pathsep.join([node_mods, node_root])
    return env
