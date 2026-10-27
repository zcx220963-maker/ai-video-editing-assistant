# -*- coding: utf-8 -*-
"""shots/INDEX.md 不许退化（2026-10-14 真机那次 failed 的回归）。

**这份索引不是文档，是选卡的唯一判据。** SKILL.md:56/65 对模型说得很硬：
「先读 `shots/INDEX.md`（一次加载技能调用）……扫完就能选卡，**不要逐张调
「加载技能」读卡全文**」「不要读超过 30 张卡」。模型因此**不打开卡**就选——
索引里那一句话必须真的在。索引一空，那条禁令就变成「凭卡名猜」，模型唯一的活路
只剩逐张打开：124 张卡按 2 张/轮读，撞上 `run_server.py:282` 的 `max_iterations=40`
当场 failed。真机 run `a3d8ff08098a` 就是这么炸的——第 13–40 轮**全是 `load_skill`
读卡**，烧掉 3,412,468 token、一张计划卡都没出。

坏的是生成器 `scripts/build_index.py`（它按一份早已不存在的 markdown 格式解析，
而真卡是 YAML frontmatter），三处同时坏：描述 124/124 全空、能量抓成正文里的帧号
数组 `[236,236,236]`、demo 字节 124/124 为 0。

**为什么一直没人发现**：全仓零处断言读过 INDEX.md。判据
（`code_spec._known_card_ids`）自己扫 `shots/` 目录，索引坏了它照样绿——
「模型拿不到选卡信息」这件事没有任何闸在读。这份文件就是那道闸。

三条断言，对应三处坏法，各自都能单独把旧索引判红：
  ① 覆盖：索引里的卡 id 集合 == 判据认的卡库（**双向**——少一张与多一张都红）。
     不拿生成器自己的输出对账，而是去问 `_known_card_ids()`，两处独立算出来的
     东西相等才有意义；否则「生成器和它的测试一起错」会一直绿。
  ② 不退化：每行有非空的一句话描述、demo 字节 > 0。
  ③ 形状：能量是短标签而不是被误抓的数字串；头部那句「不要逐张打开卡」在，
     因为它是与索引配套的——索引坏了，这句话就从省力变成陷阱。

跑法：容器内 `python -I backend/tests/test_shot_index.py`（本文件只读工作树，
不起服务；描述为空时 `scripts/build_index.py` 自己也以退出码 1 报红，那是第一道闸）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ✓/✗ 在 GBK 控制台直接 print 会 UnicodeEncodeError（pytest 捕获才没暴露）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.motion import code_spec as cs  # noqa: E402

FAIL: list[str] = []

SKILL = Path(__file__).resolve().parents[1] / "examples/retired/motion_explainer"
INDEX = SKILL / "shots/INDEX.md"
SHOTS = SKILL / "shots"

#: 一整行卡片：`- \`类别/卡名\` [能量] — 描述 … | 时长 … | 适用 … | demo \`目录\` N B`
LINE_RE = re.compile(
    r"^- `(?P<cid>[^`]+)`\s+\[(?P<energy>[^\[\]]+)\]\s+—\s+(?P<desc>\S.*?)"
    r"(?:\s+\|\s+时长\s+(?P<dur>[^|]+?))?"
    r"(?:\s+\|\s+适用\s+(?P<fit>[^|]+?))?"
    r"\s+\|\s+demo\s+`(?P<demo>[^`]+)`\s+(?P<bytes>[\d,]+)\s+B\s*$"
)

#: 描述缺失时生成器写下的占位符——出现即退化。
MISSING = "（缺一句话描述）"


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAIL.append(label)


def _card_lines() -> list[str]:
    return [ln for ln in INDEX.read_text("utf-8").splitlines() if ln.startswith("- `")]


def case_index_not_degenerate() -> None:
    """② 不退化：描述与 demo 字节。这一组在旧索引上 124/124 全红。"""
    print("\n=== ② 索引不退化：每行都得有「这句话」和 demo 字节 ===")
    lines = _card_lines()
    check(bool(lines), f"索引里有卡片行（{len(lines)} 行）")

    unparsed = [ln for ln in lines if not LINE_RE.match(ln)]
    rows = [m.groupdict() for m in map(LINE_RE.match, lines) if m]
    # **非要非空不可**：下面两条是逐行看的，若一行都解析不出来它们会**空过**
    # （旧索引改的是整份形状，于是「每行都合形状」在 0 行上打过一次 ✓）。
    # 逐行断言必须先钉住「确实有行可看」，否则下次换个形状退化照样绿。
    check(rows and not unparsed,
          f"每行都合 `- \\`卡\\` [能量] — 描述 | … | demo \\`目录\\` N B` 的形状"
          f"（解析出 {len(rows)}/{len(lines)} 行）"
          + (f"；首行：{unparsed[0][:110]}" if unparsed else ""))

    empty = [r["cid"] for r in rows if MISSING in r["desc"] or not r["desc"].strip()]
    check(rows and not empty,
          f"每张卡都有一句话描述（缺 {len(empty)} 张）"
          + (f"：{', '.join(empty[:5])}" if empty else "")
          + "——这一句就是模型选卡的唯一依据；它一空，"
            "「不要逐张打开卡」这条禁令就变成「凭卡名猜」，模型只能逐张读卡撞迭代上限")

    zero = [r["cid"] for r in rows if int(r["bytes"].replace(",", "")) <= 0]
    check(rows and not zero,
          f"每张卡都算得出 demo 字节（0 B 的有 {len(zero)} 张）"
          + (f"：{', '.join(zero[:5])}" if zero else "")
          + "——0 B 意味着 `derived_from` 那一栏模型无从下手")


def case_index_shape() -> None:
    """③ 形状：能量别抓错东西；配套那句禁令得在。"""
    print("\n=== ③ 形状：能量是短标签、配套禁令在 ===")
    lines = _card_lines()
    rows = [m.groupdict() for m in map(LINE_RE.match, lines) if m]

    # 旧版用 `re.search(r'\[(.+?)\]', text)` 抓全文第一个方括号，抓到的是正文里的
    # 帧号数组（`[236,236,236]`）。特征是**以数字开头**——能量是中文/`n/a`。
    numeric = [r["cid"] for r in rows if re.match(r"\s*\d", r["energy"])]
    check(rows and not numeric,
          f"能量列不是被误抓的数字串（可疑 {len(numeric)} 行）"
          + (f"：{numeric[:5]}" if numeric else "")
          + "——`[236,236,236]` 这种就是正文帧号数组，不是能量")

    head = INDEX.read_text("utf-8")
    check("不要" in head and "读超过 30 张卡" in head,
          "头部保留了 SKILL.md 那句「不要再逐张打开卡全文 / 不要读超过 30 张卡」"
          "——它与索引是配套的：索引够用这句话才成立，索引空掉它反成陷阱")


def case_index_covers_library() -> None:
    """① 覆盖：索引必须与判据认的卡库**双向**相等。

    为什么不拿生成器的输出去对账自己：那只能证明「它前后一致」，证明不了
    「它对」。`code_spec._known_card_ids()` 是模型填 `card` 时真正被校验的那一份
    （它自己扫 `shots/` 目录），两边独立算出来相等，索引才算真的够用。
    """
    print("\n=== ① 覆盖：索引里的卡 id == 判据认的卡库 ===")
    library = set(cs._known_card_ids())
    check(bool(library), f"卡库非空（{len(library)} 张）")

    indexed = {m.group("cid") for m in map(LINE_RE.match, _card_lines()) if m}
    check(indexed and len(indexed) == len(_card_lines()),
          f"索引里没有重复卡名（{len(_card_lines())} 行 / {len(indexed)} 个 id）")

    missing = sorted(library - indexed)     # 卡在库里、索引没列 → 模型选不到它
    extra = sorted(indexed - library)       # 索引列了、库里没有 → 模型抄了就被判据拒
    check(not missing, f"库里的卡索引都列了（漏 {len(missing)} 张：{missing[:5]}）")
    check(not extra,
          f"索引里的卡判据都认（多 {len(extra)} 张：{extra[:5]}）"
          + "——模型是照抄行首那个 id 填 `card` 的，索引多列一张就是一次必拒")

    # 行首那个 id 与卡文件名同源：真卡 124 张 frontmatter 的 `name` 都等于文件名，
    # 若将来某张卡改名而不改文件名，索引会列出一个判据不认的 id。
    on_disk = {f"{d.name}/{c.stem}"
               for d in SHOTS.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))
               for c in d.glob("*.md")}
    check(on_disk == library,
          f"卡库与盘上文件一致（盘 {len(on_disk)} 张 / 库 {len(library)} 张；"
          f"差 {sorted(on_disk ^ library)[:5]}）")


def main() -> int:
    case_index_not_degenerate()
    case_index_shape()
    case_index_covers_library()
    print()
    if FAIL:
        print(f"红了 {len(FAIL)} 条：")
        for s in FAIL:
            print(f"  · {s}")
        return 1
    print("全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
