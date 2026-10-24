# -*- coding: utf-8 -*-
"""折叠 load_skill 时必须**整段留下索引**，不能只留开头 200 字符。

真机事故（2026-10-16，run b7df827ebed4，「薄肌理论」20 镜那条）：

    seq [tool] load_skill
    [已折叠工具结果｜load_skill｜这条是更早一轮的调用结果，原文 60036 字节，
     此处只留开头。需要原文就重新调用一次该工具（用更窄的参数）。]
    [skill:motion_explainer_skill location=skills/motion_explainer_skill/SKILL.md]
    附件（315 个，用 load_skill(name, path="相对路径") 读取）：
      - aesthetic-rules.md（27656 字节）
      - final-review.md（7096 字节）
      - music-bea…                       ← 就切在这里

模型手里只剩 3 个附件名（共 315 个）、一个 section 键名都没有，可回执要它
「用更窄的参数重取」。于是它照着猜：

    load_skill(path="references/film-api.md")   → 无此附件（这个技能没有 references/）
    load_skill(path="references/palettes.json") → 无此附件
    load_skill(section="film-api")              → 没有这个 section
    load_skill(section="palettes")              → 没有这个 section

四次全错 → 熔断提示 → 转去 grep 会话工作区找 CaptionText（工具说明写着找不到）→
得出**假结论**「技能附件读不到，这是环境限制」，写进了给用户的答复。
而真答案就在被切掉的那几行里：`demos/_fixtures/Fixtures.tsx`（35 KB，CaptionText 在那儿）。

这条用例钉住三件事：
  ① 折叠后附件名与分段键名**都还在**（恢复路径的钥匙没被拿走）；
  ② 正文**真的被丢掉了**（折叠仍然省钱，不是把整条留着）；
  ③ 回执那半句是 load_skill 自己的路，不再是「用更窄的参数」。

运行：  python tests/test_skill_fold_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework.compress import (  # noqa: E402
    DEFAULT_FOLD_AT_BYTES, DEFAULT_FOLD_PREVIEW, DEFAULT_KEEP_RECENT_RESULTS,
    INDEX_FOLD_TOOLS, SKILL_INDEX_END, ToolResultGate,
)
from agent_framework.skill import LoadSkillTool, Skill  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


#: 正文里那句一眼能认出来的话。折叠之后**不该**还在——它就是被省掉的那几十 KB。
BODY_SENTINEL = "这一段是正文，折叠之后应当整段消失。"

#: 真机上被切掉的那两份关键附件。名字必须活过折叠。
KEY_ATTACHMENT = "demos/_fixtures/Fixtures.tsx"


def _body() -> str:
    """一份长到会触发出章节目录、且带真分段标记的正文。"""
    parts = ["# 动效说明\n", "这是一份很长的技能正文。\n"]
    for key in ("film-knobs", "patch-motion", "subject-naming", "motion-anchors"):
        parts.append(f"\n<!-- section: {key} -->\n## {key}\n")
        parts.append((BODY_SENTINEL + "\n") * 6)
        parts.append(("填充行，让正文足够长以触发出章节目录。\n") * 90)
    return "".join(parts)


def _skill() -> Skill:
    files = [{"relpath": "aesthetic-rules.md", "bytes": 27656},
             {"relpath": "final-review.md", "bytes": 7096},
             {"relpath": "music-beat-sync.md", "bytes": 13509}]
    # 故意排在清单**后段**：200 字符的通用预览必然够不着它们。
    files += [{"relpath": f"demos/_fixtures/F{i}.tsx", "bytes": 1000 + i}
              for i in range(1, 6)]
    files.append({"relpath": KEY_ATTACHMENT, "bytes": 35872})
    files.append({"relpath": "demos/MANIFEST.json", "bytes": 4096})
    # 真机那份技能里 `shots/` 有 138 件，被折叠成一行**指针**
    # （「完整相对路径见 shots/INDEX.md」）——那一行是整批附件的唯一入口。
    files += [{"relpath": f"shots/{i:03d}.md", "bytes": 900 + i}
              for i in range(20)]
    files.append({"relpath": "shots/INDEX.md", "bytes": 8192})
    return Skill(name="motion_explainer_skill", description="", always=False,
                 location="skills/motion_explainer_skill/SKILL.md",
                 body=_body(), files=files)


class _Loader:
    async def get(self, name: str):            # noqa: ANN201
        return _skill() if name == "motion_explainer_skill" else None


def _gate() -> ToolResultGate:
    # 门槛压低：这条用例验的是**折叠取哪一段**，不是门槛本身。
    return ToolResultGate(fold_at_bytes=2000, keep_recent=1)


async def main() -> int:
    tool = LoadSkillTool(_Loader())            # type: ignore[arg-type]
    raw = await tool.execute(name="motion_explainer_skill")

    print("=== ⓪ 生产者确实在索引后面放了分隔行 ===")
    check(SKILL_INDEX_END in raw, "整份输出里有索引分隔行")
    idx = raw.find(SKILL_INDEX_END)
    check(idx > 0 and KEY_ATTACHMENT in raw[:idx],
          f"分隔行之前就有附件清单（含 {KEY_ATTACHMENT}）")
    check(BODY_SENTINEL in raw[idx:], "分隔行之后才是正文（分隔行位置没放反）")

    print()
    print("=== ① 折叠之后：索引整段留下 ===")
    msgs = [
        {"role": "tool", "name": "load_skill", "content": raw},
        # 后面垫几坨大结果，把总量顶过门槛、逼出折叠
        {"role": "tool", "name": "fetch_url", "content": "y" * 4000},
        {"role": "tool", "name": "fetch_url", "content": "z" * 4000},
    ]
    stats = _gate().shrink(msgs)
    folded = str(msgs[0]["content"])

    check(stats["folded"] >= 1, f"确实折了（{stats['folded']} 条）")
    check("已折叠工具结果" in folded, "第一条被折成回执形态")
    check(KEY_ATTACHMENT in folded,
          f"附件名活了下来（{KEY_ATTACHMENT}）——这就是真机上被切掉的那个名字")
    check("aesthetic-rules.md" in folded, "清单开头那几个也还在")
    # 大批附件被折成一行指针——**那一行必须活下来**，真机那份技能的 138 张 shot 卡
    # 只剩这一个入口。
    check("完整相对路径见 shots/INDEX.md" in folded,
          "被折叠的大目录**指针**活了下来（去 INDEX.md 拿完整路径）")
    for key in ("film-knobs", "patch-motion", "subject-naming", "motion-anchors"):
        check(key in folded, f"分段键名 {key} 活了下来（模型靠它写 section=）")
    check("章节目录" in folded,
          "带行号的章节目录活了下来（真机上模型靠它 offset= 直取「8 套主题色板」那节）")

    print()
    print("=== ② 正文真的被丢掉了（折叠仍然省钱）===")
    check(BODY_SENTINEL not in folded, "正文那句已经不在了")
    kept = len(folded.encode("utf-8"))
    check(kept < len(raw.encode("utf-8")) // 2,
          f"留下来的远小于原文（{kept} B vs {len(raw.encode('utf-8'))} B）")

    print()
    print("=== ③ 回执那半句是 load_skill 自己的路 ===")
    check("用更窄的参数" not in folded,
          "不再是那句把模型引去猜参数的「用更窄的参数」")
    check("path=" in folded and "section=" in folded and "offset=" in folded,
          "回执点名说清怎么取（path= / section= / offset=）")
    check("索引整份留在下面" in folded,
          "回执明确告诉模型：索引没被切掉，就在下面")
    check("read_file / grep" in folded,
          "顺带堵死那条走不通的路（技能文件不在会话工作区里）")

    print()
    print("=== ④ 没带索引的结果退回通用行为（不误伤）===")
    # 附件读取 / 按行分页的结果没有分隔行——不能因为找不到记号就整条留着。
    plain = {"role": "tool", "name": "load_skill", "content": "手" * 3000}
    msgs2 = [plain,
             {"role": "tool", "name": "fetch_url", "content": "y" * 4000},
             {"role": "tool", "name": "fetch_url", "content": "z" * 4000}]
    _gate().shrink(msgs2)
    got2 = str(msgs2[0]["content"])
    check("已折叠工具结果" in got2, "照样被折")
    check(len(got2) < 1000,
          f"退回「只留开头」的老行为（{len(got2)} 字符，不是整条留着）")

    print()
    print("=== ⑤ 通用工具没被带偏 ===")
    other = {"role": "tool", "name": "fetch_url", "content": "网" * 3000}
    msgs3 = [other,
             {"role": "tool", "name": "fetch_url", "content": "y" * 4000},
             {"role": "tool", "name": "fetch_url", "content": "z" * 4000}]
    _gate().shrink(msgs3)
    got3 = str(msgs3[0]["content"])
    preview3 = got3.split("]\n", 1)[-1].rstrip("…")
    check(preview3 == "网" * DEFAULT_FOLD_PREVIEW,
          f"其它工具仍只留开头 {DEFAULT_FOLD_PREVIEW} 字符")
    check("index" not in got3.lower(), "没被塞进技能那套回执")
    check(INDEX_FOLD_TOOLS == frozenset({"load_skill"}),
          f"要整段留索引的只有 load_skill（实际 {set(INDEX_FOLD_TOOLS)}）")
    check(DEFAULT_FOLD_AT_BYTES > 100_000,
          f"生产门槛没被这条用例里的 2000 带偏（实际 {DEFAULT_FOLD_AT_BYTES}）")
    check(0 < DEFAULT_KEEP_RECENT_RESULTS < 10,
          f"最近结果条数自洽（{DEFAULT_KEEP_RECENT_RESULTS}）")

    print()
    print("=== ⑥ 折到回差下沿就收手（不只卡在门槛线上）===")
    # fold_to_ratio：触发后一口气折到 fold_at_bytes 的 50%，否则每来一条新结果
    # 都要再折一次、整段前缀缓存每轮重算。
    g6 = ToolResultGate(fold_at_bytes=48_000, keep_recent=3)
    msgs6 = [{"role": "tool", "name": "fetch_url", "content": f"r{i:02d}" + "x" * 1996}
             for i in range(30)]
    stats6 = g6.shrink(msgs6)
    mask6 = ["已折叠工具结果" in str(m.get("content")) for m in msgs6]
    check(mask6[0], "超线时仍然从最老的开始折")
    check(sum(mask6) >= 4, f"没有停在条数下限上（共折 {sum(mask6)} 条）")
    check(not any(mask6[-3:]), "最近 3 条一条没折（keep_recent）")
    check(stats6["total_bytes"] <= 48_000 * 0.5 + 1,
          f"折到回差下沿就收手（{stats6['total_bytes']} ≤ 24000）")

    print()
    print("=== ⑦ 折叠顺序：研究结果先折，技能结果最后 ===")
    # run 639a：40 条 load_skill 结果被折成存根后，模型换起始行把同一份重扫三遍。
    # 先该折的是「看过、消化过」的网页，不是它正拿来写计划与代码的正文/附件——
    # 技能结果的恢复路径最贵（窗口重取会撞同参同结果闸）。
    # 第三项（最新一条）受 keep_recent 保护，永远不折：用它的体积做「总量」，让
    # 可折的那两条先被上面两条用例分别覆盖。
    def _three(skill: int, web: int) -> list[dict]:
        return [{"role": "tool", "name": "load_skill", "content": "s" * skill},
                {"role": "tool", "name": "fetch_url", "content": "w" * web},
                {"role": "tool", "name": "fetch_url", "content": "n" * 1000}]

    # 折掉一份大网页就够回到回差下沿：技能正文一条不动。
    msgs7 = _three(5_000, 20_000)
    ToolResultGate(fold_at_bytes=15_000, keep_recent=1).shrink(msgs7)
    check("已折叠工具结果" not in str(msgs7[0]["content"]),
          "折一份网页就够时，load_skill 一条不动")
    check("已折叠工具结果" in str(msgs7[1]["content"]),
          "先折的是更早的网页（研究结果是已消化的原料）")

    # 网页折完仍超线：这时才轮到技能结果（顺序保守，不是不折）。
    msgs8 = _three(60_000, 10_000)
    ToolResultGate(fold_at_bytes=70_000, keep_recent=1).shrink(msgs8)
    check("已折叠工具结果" in str(msgs8[1]["content"]),
          "网页先被折（即使它比技能正文小）")
    check("已折叠工具结果" in str(msgs8[0]["content"]),
          "网页折完仍超线：这时才轮到技能结果（顺序保守，不是不折）")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
