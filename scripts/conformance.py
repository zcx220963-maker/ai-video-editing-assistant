# -*- coding: utf-8 -*-
"""对账脚本：把《重构-严格对齐框架设计》里的自述与代码/文档本身对一遍。

为什么要有它：这份文档的错，几乎全是**单点都对、交叉全错**——
§6 的「先证红」列了 3 条、§8 矩阵有 5 条；附录 B 漏了 R5 要改的
`tools/__init__.py`；附录 C 的取证命令用非贪婪正则截白名单，被注释里的
`[storyline]` 提前收尾，于是"复现"出 29 而不是 30。人眼查不出来，机器一跑就出。

用法：
    python scripts/conformance.py            # 人读清单
    python scripts/conformance.py --strict   # 有任何不一致就退出码 1

它**不跑业务测试**（那是 backend/tests/*.py 的事），只做文本与计数对账。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "重构-严格对齐框架设计-2026-10-27.md"
BACKEND = ROOT / "backend"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def check_r_ids(doc: str) -> list[str]:
    """R1–R20 必须连续无空号。"""
    ids = sorted({int(m) for m in re.findall(r"^### R(\d+)", doc, re.M)})
    want = list(range(1, 21))
    if ids != want:
        missing = sorted(set(want) - set(ids))
        extra = sorted(set(ids) - set(want))
        return [f"R 编号不连续：缺 {missing} 多 {extra}"]
    return []


def check_d_ids(doc: str) -> list[str]:
    """D1–D17 必须连续（决策台账）。"""
    ids = sorted({int(m) for m in re.findall(r"^\| D(\d+) \|", doc, re.M)})
    want = list(range(1, 18))
    if ids != want:
        missing = sorted(set(want) - set(ids))
        return [f"D 编号不连续：缺 {missing}"]
    return []


def check_red_lists_subset(doc: str) -> list[str]:
    """§6 每个 R 的「先证红」条目 ⊆ §8 矩阵里同前缀的编号。

    这条正是 E-7 那类错的机器版：改了矩阵忘了列表（或反过来）。
    """
    problems: list[str] = []
    # 只比测试文件名：§6 里出现的 `test_xxx.py` 必须能在 §8 该 R 的行里找到。
    # 不能把 §6 的其余反引号标识符（max_iterations / on_progress 这类实现细节）
    # 一起比——§8 是判据表，不是实现清单，否则每次都会假报。
    sections = re.split(r"^### R(\d+)", doc, flags=re.M)
    for i in range(1, len(sections), 2):
        rid = sections[i]
        body = sections[i + 1]
        m = re.search(r"\*\*先证红\*\*：(.+?)(?=\n- \*\*|\Z)", body, re.S)
        if not m:
            continue
        files = set(re.findall(r"(test_[a-z0-9_]+\.py)", m.group(1)))
        if not files:
            continue
        matrix_rows = "\n".join(re.findall(rf"^\| R{rid}-[a-z] \|.*$", doc, re.M))
        missing = {f for f in files if f not in matrix_rows}
        if missing:
            problems.append(f"R{rid}: §6 提到但 §8 没有：{sorted(missing)}")
    return problems


def check_appendix_b_paths(doc: str) -> list[str]:
    """附录 B 的路径必须真实存在（新增的除外，标注了「新增」的跳过）。"""
    problems: list[str] = []
    m = re.search(r"## 附录 B：文件改动清单(.*?)\n## ", doc, re.S)
    if not m:
        return ["找不到附录 B"]
    block = m.group(1)
    for path in re.findall(r"`(backend/[^`]+?\.(?:py|toml|md))`", block):
        if not (ROOT / path).exists():
            problems.append(f"附录 B 列了不存在的文件：{path}")
    return problems


def check_counts(doc: str) -> list[str]:
    """§0.4 的 40 条计数必须自洽，且与 §3 表里的状态逐行相等。"""
    problems: list[str] = []
    m = re.search(r"## 3\. 设计章节逐项对照（40 条）(.*?)\n## ", doc, re.S)
    if not m:
        return ["找不到 §3 对照表"]
    table = m.group(1)
    rows = re.findall(r"^\| ([ABCODE]\d+) \|[^|]*\|[^|]*\| (对齐|等价|半|缺|示例) \|",
                      table, re.M)
    tally: dict[str, int] = {}
    for _, status in rows:
        tally[status] = tally.get(status, 0) + 1
    head = re.search(r"### 0\.4 对照总览（40 条）(.*?)\n### ", doc, re.S)
    if not head:
        return ["找不到 §0.4"]
    declared = {}
    for label, key in (("对齐", "对齐"), ("等价", "等价"), ("半成品", "半"),
                       ("缺口", "缺"), ("示例", "示例")):
        mm = re.search(rf"{label} (\d+) 条", head.group(1))
        declared[key] = int(mm.group(1)) if mm else -1
    for key, value in declared.items():
        got = tally.get(key, 0)
        if value != got:
            problems.append(f"§0.4 写 {key}={value}，§3 表实际 {got}")
    if sum(declared.values()) != 40:
        problems.append(f"§0.4 五类合计 {sum(declared.values())} ≠ 40")
    if len(rows) != 40:
        problems.append(f"§3 表行数 {len(rows)} ≠ 40")
    return problems


def check_whitelist(doc: str | None) -> list[str]:
    """白名单计数必须与两份 TOML 实测一致（用 tomllib，不用正则）。

    doc=None 表示文档不在工作树（新克隆仓库常见）：只做 TOML 结构检查，
    跳过「文档写的数字是否等于实测」这一段。
    """
    import tomllib

    problems: list[str] = []
    sizes = {}
    for name in ("config.toml", "config.docker.toml"):
        p = BACKEND / "examples" / "storyline" / name
        if not p.exists():
            problems.append(f"缺配置：{name}")
            continue
        data = tomllib.loads(_read(p))
        nodes = data.get("local_mcp_server", {}).get("available_nodes", [])
        sizes[name] = len(nodes)
        if "dag_contract" not in nodes:
            problems.append(f"{name} 的白名单缺 dag_contract")
        if not data.get("local_mcp_server", {}).get("available_node_pkgs"):
            problems.append(f"{name} 缺 available_node_pkgs（R16）")
    if len(set(sizes.values())) > 1:
        problems.append(f"两份白名单项数不一致：{sizes}")
    elif sizes and doc is not None:
        n = next(iter(sizes.values()))
        if f"| 产品白名单项（现状） | {n} |" not in doc:
            problems.append(f"§11.3 的产品白名单项数不是实测的 {n}")
    return problems


def check_pytest_claim(doc: str) -> list[str]:
    """文档不能把 pytest 当验收命令——容器里没装，测试都是独立 check() 脚本。

    只拦命令式写法（`python -m pytest` / `pytest backend/...`）；正文里解释
    「容器没装 pytest」是合法说明，不报警。
    """
    if re.search(r"python\s+-m\s+pytest|(^|\s)pytest\s+backend/", doc, re.M):
        return ["文档仍在用 pytest 当验收口径（容器没装 pytest，测试是独立脚本）"]
    return []


DOC_CHECKS = (
    ("R 编号连续", check_r_ids),
    ("D 编号连续", check_d_ids),
    ("§6 先证红 ⊆ §8 矩阵", check_red_lists_subset),
    ("附录 B 路径存在", check_appendix_b_paths),
    ("§0.4 / §3 计数一致", check_counts),
    ("无 pytest 口径", check_pytest_claim),
)

CODE_CHECKS = (
    ("白名单与 TOML", check_whitelist),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有不一致就退出码 1")
    ap.add_argument("--doc", help="对账用的重构文档路径（默认仓库 docs/ 下那一份）")
    args = ap.parse_args()
    doc_path = Path(args.doc) if args.doc else DOC
    doc: str | None = None
    checks: list[tuple[str, object]] = []
    if doc_path.exists():
        doc = _read(doc_path)
        checks.extend(DOC_CHECKS)
    else:
        print(f"[SKIP] 找不到文档：{doc_path}（只跑代码侧检查）")
    checks.extend(CODE_CHECKS)
    bad = 0
    for label, fn in checks:
        problems = fn(doc)  # type: ignore[operator]
        if problems:
            bad += len(problems)
            print(f"[FAIL] {label}")
            for p in problems:
                print(f"       - {p}")
        else:
            print(f"[ OK ] {label}")
    print(f"\n共 {len(checks)} 项检查，{bad} 处不一致。")
    return 1 if (bad and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
