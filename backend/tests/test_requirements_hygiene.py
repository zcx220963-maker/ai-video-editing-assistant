"""依赖与自持件的卫生闸（R10-T49-③ / Q69）：把「别造轮子」变成一条**可失败的断言**。

Q69 的原话是「靠一支常驻测试，不靠表格」。两条判据：

① **声明了的直接依赖必须真被 import**——「装着不用」在上一轮是靠人工体检才发现的。
   白名单**必须写一行理由**（如「运行时 CLI」「驱动名只出现在 DSN 里，由 SQLAlchemy
   自己 import」），不写理由就等于把闸拆掉。落在 §3.6 里那句「复用底账」因此会红，
   而不是等下一次人工体检才被想起来。
② **§3.6 登记为「本仓自持」的手写件必须各自有金样**（`tests/test_handrolled_goldens.py`）。
   换库换了一半、或者旧手写件没删干净，都会在这里红。
③ 额外一条（T44 的收口）：`mcp` 包必须在**客户端侧**（`agent_framework/**`）被用到——
   这条是 P30 的证据：包声明了、服务端在用，而客户端还在手写 JSON-RPC。
   它在 T44 落地前**必然红**，落地后必须绿。

**为什么 npm 那一侧不按同一把尺判**：`remotion/package.json` 里有一批只能从 CLI 入口
看到的包（`@remotion/renderer` 由 `npx remotion render` 加载、`typescript`/`@types/react`
是构建期件），按「源码里有没有 import」扫会造出一张长白名单，比它想防的问题还吵。
那边真正的闸是 `npm run lint` 与 §3.3 的真渲探针，不在这一支里重复。

运行：  python tests/test_requirements_hygiene.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent          # backend/
TESTS = ROOT / "tests"
REQUIREMENTS = ("requirements.txt", "requirements-storyline.txt")

#: 发行名 → 源码里的 import 名。绝大多数是同名（把 `-` 换成 `_`），这里只列**不同名**的。
IMPORT_ALIASES: dict[str, str] = {
    "sqlalchemy": "sqlalchemy",
    "python-multipart": "multipart",
    "pillow": "PIL",
    "beautifulsoup4": "bs4",
    "pyyaml": "yaml",
    "edge-tts": "edge_tts",
    "faster-whisper": "faster_whisper",
    "yt-dlp": "yt_dlp",
    "python-dotenv": "dotenv",
    "opencv-python": "cv2",
}

#: 允许「声明了但源码里不直接 import」的包，**每条都要有理由**。
WHITELIST: dict[str, str] = {
    "asyncpg": "驱动名只出现在 DSN（postgresql+asyncpg://），由 SQLAlchemy 自己 import；"
               "本仓直接 import 它反而是绑死驱动实现",
    "greenlet": "SQLAlchemy 异步上下文切换的运行时依赖（它 import 它，不是我们）；"
                "声明它是为了把版本钉住，缺了 sqlalchemy 的 async engine 会当场报错",
}

#: §3.6 登记为「本仓自持」的手写件：金样测试文件里必须有对应标记。
#: key = 判据串（在 test_handrolled_goldens.py 里搜得到），value = 人读的名字。
HANDROLLED: dict[str, str] = {
    "part1_jwt_and_password": "JWT HS256 + PBKDF2（auth.py）",
    "part2_yaml_subset": "YAML 子集 + ${ENV} 展开（tools/mcp.py）",
    "part3_frontmatter": "frontmatter 解析（skill.py）",
    "part4_cjk_counting": "CJK token 估算（compress.py）",
}
GOLDEN_FILE = "test_handrolled_goldens.py"

#: 客户端侧：T44 之后 `mcp` 必须在这里出现（P30 的证据）。
CLIENT_SIDE = "agent_framework"

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------

def _declared() -> dict[str, str]:
    """requirements*.txt → {发行名: 声明的那个文件名}。注释行与 `-r` 之类跳过。"""
    out: dict[str, str] = {}
    for name in REQUIREMENTS:
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"([A-Za-z0-9_.\-]+)", line)
            if m:
                out.setdefault(m.group(1), name)
    return out


def _py_sources(*, under: str | None = None) -> str:
    """源码正文（默认整个 backend/，`under` 给定时只看那个子目录）。

    跳过 `tests/`：测试里 import 一个包不能证明生产路径在用它——
    那正是「装了不用」最容易被洗绿的地方。
    """
    base = ROOT / under if under else ROOT
    chunks: list[str] = []
    for p in sorted(base.rglob("*.py")):
        if "tests" in p.parts or "__pycache__" in p.parts:
            continue
        chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _imported(blob: str, module: str) -> bool:
    """源码里有没有 import 这个模块（含 `importlib.import_module("x")` 那种延迟写法）。

    只认**语句级**的 import：这样「注释里提到 aiohttp」不会被当成在用——
    上一轮那张「声明了没人用」的欠账，恰恰就是这么留下来的。
    """
    esc = re.escape(module)
    sub = rf"(?:\.[A-Za-z_][A-Za-z0-9_]*)*"        # 子模块：sqlalchemy.ext.asyncio / mcp.server.fastmcp
    return bool(
        re.search(rf"(?m)^[ \t]*from[ \t]+{esc}{sub}[ \t]+import\b", blob)
        or re.search(rf"(?m)^[ \t]*import[ \t]+{esc}\b", blob)
        or re.search(rf"import_module\([ \t]*[\"']{esc}", blob)
    )


# --------------------------------------------------------------------------

def part1_declared_are_used() -> None:
    print("\n[1] ① requirements*.txt 里声明的每个包都在源码里被 import")
    declared = _declared()
    check(bool(declared), f"读到声明清单（{len(declared)} 个包）")
    blob = _py_sources()
    for pkg in sorted(declared):
        if pkg in WHITELIST:
            check(bool(WHITELIST[pkg].strip()),
                  f"{pkg}：白名单条目**有理由**（{WHITELIST[pkg][:24]}…）")
            continue
        module = IMPORT_ALIASES.get(pkg.lower(), pkg.lower().replace("-", "_"))
        check(_imported(blob, module),
              f"{pkg}（import {module}）在 backend/ 里真被用到"
              f"——零命中说明装了不用（声明在 {declared[pkg]}）")
    stale = sorted(set(WHITELIST) - set(declared))
    check(not stale,
          f"白名单里没有「早就不声明了还挂着」的残留条目：{stale or '无'}")


def part2_handrolled_have_goldens() -> None:
    print("\n[2] ② §3.6 的四处自持件各有一条金样")
    path = TESTS / GOLDEN_FILE
    check(path.exists(), f"金样文件存在（tests/{GOLDEN_FILE}）")
    if not path.exists():
        return
    src = path.read_text(encoding="utf-8")
    for marker, label in HANDROLLED.items():
        check(f"def {marker}(" in src, f"{label} 的金样在场（{marker}）")
    # 「不改行为只锁行为」这句话的落地：金样必须真的在比较，不是光打印
    check(src.count("eq(") >= 20, f"金样里有实质比较（{src.count('eq(')} 处 eq(...) 断言）")


def part3_mcp_used_client_side() -> None:
    print("\n[3] ③ `mcp` 在客户端侧被用到（T44/P30）")
    client = _py_sources(under=CLIENT_SIDE)
    check(_imported(client, "mcp"),
          f"{CLIENT_SIDE}/** 里 import 了 mcp 包（手写 JSON-RPC 已被官方 SDK 薄适配层取代）")


def main() -> None:
    part1_declared_are_used()
    part2_handrolled_have_goldens()
    part3_mcp_used_client_side()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
