# -*- coding: utf-8 -*-
"""文档-代码对账（T0.6）：**两边都不说，才是不合法**。

为什么要有这个文件（2026-10-24）：

设计文档描述「设计意图」，代码是「实际行为」。已确认漂了三处：

| 机制 | 文档说 | 代码实际 |
|---|---|---|
| 记忆系统 | `User.md` / `Tool.md` 单文件、全文覆盖 | **PG 的 `memories` 表** |
| CronTool | 「用文件系统持久化」 | **`storage.jobs`（PG）** |
| SKILL frontmatter | 只有 `name` / `description` / `always` | 多一层 **`readers:`**（按读者过滤分段） |

新人按文档理解、按代码调试，就会反复踩这三处坑。

**这条不是「让文档追代码」**：文档先行（设计变更）和代码先行（实现优化）都合法，
不合法的只有「两边都不说」。所以这个文件不比对每一句话，它比对**三张清单**：

1. `MIGRATED` —— 旧说法 → 现在的实现，**每条必须写明迁到哪**，而且
   （a）目标文件存在（b）目标文件里真有那段实现（c）迁移留痕还在（d）文档里确实还
   在说那个旧说法（否则这条清单本身过期了）。
   **验收：把 `memory.py` 里「取代 User.md / Tool.md」那行删掉 → 这里红。**
2. `PUBLIC_MECHANISMS` —— 代码里有、且**对外可见**的机制（今天一条：`readers:`），
   必须至少有一篇 live 文档提到它。反方向也查：代码里删了它，文档那句话就成了假话。
3. `INTERNAL` —— 代码里有、但**刻意**不对外说的（内部实现）。标注它的人必须给出理由；
   理由为空 = 不是「内部实现」，是漏了文档。

另外一条自动检查：**live 文档里引用的模块路径必须存在**。审计报告（`audit-*.md`、
`复盘-*`）是**当时的快照**，不对账——它们写的是历史，历史不需要跟着代码变。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def read(rel: str) -> str:
    p = ROOT / rel
    try:
        return p.read_text("utf-8")
    except OSError:
        return ""


#: **今天仍然成立的**设计文档（对账范围）。加一篇在这里 = 承诺它跟着代码走。
LIVE_DOCS: tuple[str, ...] = (
    "docs/ARCHITECTURE.md",
    "docs/能力与链路总览-2026-10-23.md",
    "链路全解-2026-10-24.md",
    "docs/盘点-母版还写死了什么-2026-10-23.md",
    "docs/设计-导入的skill自动接入写手-2026-10-23.md",
)

#: **当时快照**，不对账。每条必须写明为什么——「不想改」不是理由。
POINT_IN_TIME: dict[str, str] = {
    "backend/docs/audit-2026-10-01-剪辑流程根因报告.md":
        "审计报告：记录 2026-10-01 当天的现场与一次性临时脚本，脚本后来已清理",
    "backend/docs/audit-2026-10-01-稳定性优化报告.md":
        "审计报告：记录 2026-10-01 那轮优化的前后读数，不是当前状态的说明",
    "backend/docs/audit-2026-10-01-第三轮优化报告.md":
        "审计报告：记录第三轮优化的现场，随代码演进会自然过期",
    "backend/docs/audit-2026-10-01-缺陷修复记录.md":
        "审计报告：逐条记录当时修掉的缺陷，属于变更史而非现状",
    "backend/docs/项目完善度体检-2026-10-02.md":
        "体检快照：2026-10-02 那天的分数与缺口清单，之后已经补过多轮",
    "backend/docs/开发文档-图像驱动视频能力-2026-10-12.md":
        "未落地设计：图像驱动那条链没有实现，这份文档是提案而不是现状",
    "docs/复盘-《一秒》实跑与改造方案-2026-10-23.md":
        "复盘：描述那一轮实跑的现场与当时的改造方案",
    "docs/诊断-闸⑭判据改不动细线-2026-10-23.md":
        "诊断：一次测量的记录，结论已落进代码注释",
    "docs/诊断-能力显弱的真实断点-2026-10-23.md":
        "诊断：一次测量的记录，结论已落进代码注释",
}

#: 旧说法 → 现在的实现。**每条必须写明迁到哪**，四样东西缺一不可：
#: `to_file`（在哪）、`to_marker`（真在那儿的证据）、`note_file`+`note_marker`（迁移留痕）、
#: `doc`（还在说旧话的那篇文档——没有它就说明这条清单该删了）。
MIGRATED: tuple[dict[str, str], ...] = (
    {
        "old": "User.md",
        "old_alt": "Tool.md",
        "to": "PG 的 memories 表（agent_framework/memory.py）",
        "to_file": "backend/agent_framework/memory.py",
        "to_marker": "memories",
        "note_file": "backend/agent_framework/memory.py",
        "note_marker": "取代 User.md / Tool.md",
        "doc": "backend/docs/项目复习指南.md",
        "why": "记忆从「两个 markdown 单文件全文覆盖」迁到 PG 的 memories 表",
    },
    {
        "old": "整 JSON 文件",
        "old_alt": "文件系统",
        "to": "storage.jobs（PG 的 jobs 表，agent_framework/tools/cron.py）",
        "to_file": "backend/agent_framework/tools/cron.py",
        "to_marker": "storage.jobs",
        "note_file": "backend/agent_framework/tools/cron.py",
        "note_marker": "原来是一个整 JSON 文件",
        "doc": "backend/docs/项目复习指南.md",
        "why": "定时任务从「一个整 JSON 文件」迁到 PG 的 jobs 表（多副本不再互相踩）",
    },
)

#: 代码里有、且**对外可见**的机制：文档必须提，代码删了文档那句就得跟着改。
PUBLIC_MECHANISMS: tuple[dict[str, str], ...] = (
    {
        "name": "readers:",
        "code_file": "backend/agent_framework/skill.py",
        "code_marker": "_parse_readers",
        "doc": "docs/ARCHITECTURE.md",
        "what": "SKILL.md frontmatter 的 `readers:`（按读者过滤分段）",
    },
)

#: 代码里有、刻意不对外说的：**理由必须写**（空理由 = 漏了文档，不是内部实现）。
INTERNAL: dict[str, str] = {
    "_seed_ledger": "逐镜回填 film_ledger 的内部步骤，不是用户能配的机制",
    "_channel_samples_block": "写手 prompt 的内部拼装块",
}

#: 文档里「明确在说删除」的行不参与存在性检查——它们引用的是**已经不在了**的东西。
_DEAD_MARKERS = ("删除", "不在了", "已下线", "已摘除", "退出", "搬走", "旧链", "历史")
#: 文档里的占位符（`test_xxx.py` / `x.py`）不是真路径。
_PLACEHOLDER = re.compile(r"(^|/)(x|xxx|test_xxx|_smoke|a)\.(py|mjs|tsx)$")
_MODULE_REF = re.compile(r"`([A-Za-z0-9_./\-]*(?:motion|agent_framework|nodes)/[A-Za-z0-9_./\-]+\.py)`")


def _file_index() -> dict[str, list[Path]]:
    """全仓文件按 basename 建索引（文档里常只写短路径）。"""
    idx: dict[str, list[Path]] = {}
    for p in ROOT.rglob("*"):
        s = str(p)
        if any(x in s for x in ("node_modules", "__pycache__", ".git\\", ".git/",
                                ".tmp", ".storyline", ".runtime", ".pytest_cache")):
            continue
        if p.is_file():
            idx.setdefault(p.name, []).append(p)
    return idx


def _migrated_problems(reader) -> list[str]:
    """逐条核对 `MIGRATED`，返回问题清单。``reader`` 可注入——**反证要用**。

    抽成函数而不是内联，是为了能拿一个「被篡改的 reader」跑一遍：
    「把 memory.py 里那句迁移留痕删掉，这个检查必须红」这条验收，只有这样才验得动
    （否则要么去改真文件、要么这条验收只是嘴上说说）。
    """
    problems: list[str] = []
    for item in MIGRATED:
        tag = f"{item['old']} → {item['to']}"
        if not (ROOT / item["to_file"]).exists():
            problems.append(f"{tag}：目标文件不存在（{item['to_file']}）")
            continue
        if item["to_marker"] not in reader(item["to_file"]):
            problems.append(f"{tag}：目标文件里找不到实现证据 {item['to_marker']!r}")
        if item["note_marker"] not in reader(item["note_file"]):
            problems.append(
                f"{tag}：**迁移留痕没了**（{item['note_file']} 里没有 "
                f"{item['note_marker']!r}）——「迁到哪了」没人说得清")
        doc = reader(item["doc"])
        if not doc or (item["old"] not in doc and item.get("old_alt", "") not in doc):
            problems.append(f"{tag}：{item['doc']} 已经不提这个旧说法了，这条清单该删")
    return problems


def main() -> int:
    index = _file_index()

    print("== ① 迁移清单：旧说法 → 现在的实现，每条都写清迁到哪 ==")
    problems = _migrated_problems(read)
    check(problems == [], f"三条迁移全部对得上（实得 {problems}）")
    for item in MIGRATED:
        print(f"  · {item['old']} → {item['to']}（{item['why']}）")
    # 反证：抹掉 memory.py 里那句「取代 User.md / Tool.md」，检查必须红。
    def _sabotaged(rel: str) -> str:
        body = read(rel)
        return body.replace("取代 User.md / Tool.md", "")
    got = _migrated_problems(_sabotaged)
    check(any("迁移留痕没了" in p for p in got),
          f"反证：删掉 memory.py 那句 → 报「迁移留痕没了」（实得 {len(got)} 条）")

    print("\n== ② 文档说、代码没有 → 红；代码有、文档没说 → 红 ==")
    for mech in PUBLIC_MECHANISMS:
        body = read(mech["code_file"])
        check(mech["code_marker"] in body,
              f"{mech['what']}：代码里还在（{mech['code_file']} 找 {mech['code_marker']!r}）")
        doc = read(mech["doc"])
        check(mech["name"] in doc,
              f"{mech['what']}：{mech['doc']} 提到了它（公开机制不许只在代码里）")

    print("\n== ③ 标成「内部实现」的必须给理由 ==")
    code_all = "\n".join(read(str(p.relative_to(ROOT)).replace("\\", "/"))
                         for p in (ROOT / "backend").rglob("*.py"))
    for name, why in INTERNAL.items():
        check(bool(why.strip()), f"{name}：标注给了理由")
        check(name in code_all, f"{name}：代码里确实存在（标注不是过期的）")

    print("\n== ④ live 文档引用的模块必须存在 ==")
    missing: list[str] = []
    for rel in LIVE_DOCS:
        text = read(rel)
        if not text:
            check(False, f"{rel}：存在（live 文档清单里有一篇找不到）")
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(d in line for d in _DEAD_MARKERS):
                continue
            for ref in _MODULE_REF.findall(line):
                base = ref.split("/")[-1]
                if _PLACEHOLDER.search(ref):
                    continue
                if base not in index:
                    missing.append(f"{rel}:{i} → {ref}")
    check(not missing, f"live 文档没有失效的模块引用（实得 {len(missing)} 处：{missing[:3]}）")

    print("\n== ⑤ 快照文档都写了「为什么不对账」 ==")
    for rel, why in POINT_IN_TIME.items():
        check((ROOT / rel).exists(), f"{rel}：存在")
        check(len(why.strip()) >= 8, f"{rel}：给了理由（{why[:20]}…）")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
