# -*- coding: utf-8 -*-
"""跨文件读技能的打转：重读已给过的窗口不再重发、整 run 总量有软线与硬线。

真机 2026-10-19 run ``8f43a2e43fde``（auto 入口直接进执行轮，35 轮 failed）：模型先把
``motion_explainer`` 的正文按 9 个窗口读完（≈104 KB），再把 6 份附件逐份整取/分页读完
（又 ≈90 KB）——每一份都在自己的窗口上限之内、每一枪都成功，正文/附件/读卡三条家族级
闸一条都没响。随后它把 ``aesthetic-rules.md`` 第 1–190 行按 ``1:60 / 61:60 / 121:70``
**又取了一遍**，第三遍被「同参同结果」闸拦下后连打 6 次，整条 run 按「原地打转」停下。

这条用例钉四件事：

① 同一段落在本 run 已经给过、而且回执还看得见时，不再重发（只回一句指针）；
② 跨文件逐份读——每份都是新文件、每枪都成功——到软线只说一句、到硬线才不执行；
③ 硬线回执点名「下一步该调哪些工具」，名字从注册表读，不写死；
④ ``section=`` 单取不受总量限制（硬线之后唯一该走的那条路）。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_skill_read_budget.py``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from test_tool_failure_guard import (  # noqa: E402
    build_agent, chain_violations)

from agent_framework.messages import Message  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.tool import Tool  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class ReadStub(Tool):
    """``load_skill`` 的替身：每枪都成功、回一段一眼能认出是「正文」的文本。

    记录真执行过的 ``(path, offset, limit, section)``——被闸拦下的枪不该出现在这里。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None, int | None, str]] = []

    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def description(self) -> str:
        return "读技能正文/附件的替身"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object",
                "properties": {"name": {"type": "string"}, "path": {"type": "string"},
                               "offset": {"type": "integer"}, "limit": {"type": "integer"},
                               "section": {"type": "string"}},
                "required": ["name"]}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, name: str, path: str | None = None,
                      offset: int | None = None, limit: int | None = None,
                      section: str | None = None) -> str:
        self.calls.append((path or "SKILL.md", offset, limit, section or ""))
        tag = path or section or "SKILL.md"
        return f"[skill:{name} {tag}@{offset}x{limit}] 这一段正文，第 {len(self.calls)} 枪。"


class ActionStub(Tool):
    """一个非只读工具：软线提醒里应当能读到它的名字（从注册表来，不写死）。"""

    @property
    def name(self) -> str:
        return "next_step_tool"

    @property
    def description(self) -> str:
        return "下一步动作的替身"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _tool_msgs(messages: list[Message]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]


def _notes(messages: list[Message], needle: str) -> list[str]:
    return [str(m.get("content") or "") for m in messages
            if m.get("role") == "system" and needle in str(m.get("content") or "")]


def _read(path: str | None = None, offset: int | None = None,
          limit: int | None = None, section: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"name": "motion_explainer_skill"}
    if path is not None:
        args["path"] = path
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    if section is not None:
        args["section"] = section
    return args


async def case_a_covered_window_is_not_resent() -> None:
    """① 正文：1:120 给过之后再取 1:60，不再重发（run 8f43a2e43fde 的形状）。"""
    print("\n=== ① 正文：整段落在已给过的窗口里，不再重发 ===")
    probe = ReadStub()
    steps = [
        ("tool", "load_skill", _read(limit=120)),
        ("tool", "load_skill", _read(offset=121, limit=70)),
        ("tool", "load_skill", _read(limit=60)),        # 1–60 已被 1–120 覆盖
        ("tool", "load_skill", _read(offset=61, limit=60)),  # 61–120 同样覆盖
        ("answer", "开写"),
    ]
    agent = build_agent(steps, tools=[probe, ActionStub()])
    agent.config.max_iterations = len(steps) + 2
    out = await agent.run(Session(user_id="u", conversation_id="c_read_a"), "做一条片")
    last: list[Message] = agent.llm.calls[-1]

    check(out == "开写", f"① run 正常收尾：{out}")
    check(len(probe.calls) == 2,
          f"① 被覆盖的两枪没有真执行：真打 {len(probe.calls)} 枪")
    receipts = _tool_msgs(last)
    covered = [t for t in receipts if "本 run 已经给过" in t]
    check(len(covered) == 2, f"① 每枪都有回执、不静默丢弃：{len(covered)} 条")
    check(bool(covered) and "1 行起×120 行" in covered[0]
          and "121 行起×70 行" in covered[0],
          f"① 回执把已取过的窗口摆出来：{covered[:1]}")
    check(bool(covered) and "section=键名" in covered[0] and "更宽" in covered[0],
          "① 出路当场可执行：按 section 单取，或用一个更宽的窗口")
    check(chain_violations(last) == [], f"① 链子收口：{chain_violations(last)}")


async def case_b_attachment_covered_window() -> None:
    """② 附件：aesthetic-rules 的 1:60 / 61:60 已被 1:120 覆盖，不再重发。"""
    print("\n=== ② 附件：同一段换个起始行再取，不再重发 ===")
    probe = ReadStub()
    steps = [
        ("tool", "load_skill", _read(path="aesthetic-rules.md", limit=120)),
        ("tool", "load_skill", _read(path="aesthetic-rules.md", offset=121, limit=70)),
        ("tool", "load_skill", _read(path="aesthetic-rules.md", limit=60)),
        ("tool", "load_skill", _read(path="aesthetic-rules.md", offset=61, limit=60)),
        ("answer", "开写"),
    ]
    agent = build_agent(steps, tools=[probe, ActionStub()])
    agent.config.max_iterations = len(steps) + 2
    out = await agent.run(Session(user_id="u", conversation_id="c_read_b"), "做一条片")
    last: list[Message] = agent.llm.calls[-1]

    check(out == "开写", f"② run 正常收尾：{out}")
    check(len(probe.calls) == 2,
          f"② 附件被覆盖的两枪没有真执行：真打 {len(probe.calls)} 枪")
    covered = [t for t in _tool_msgs(last) if "本 run 已经给过" in t]
    check(len(covered) == 2, f"② 两枪各有一条回执：{len(covered)} 条")
    check(bool(covered) and "aesthetic-rules.md" in covered[0],
          f"② 回执点名是哪份附件：{covered[:1]}")
    check(chain_violations(last) == [], f"② 链子收口：{chain_violations(last)}")


async def case_c_cross_file_soft_and_hard_lines() -> None:
    """③ 跨文件逐份读：软线只说一句（点名下一步工具），硬线起不再执行。"""
    from agent_framework.agent import (_MAX_SKILL_READS,
                                       _MAX_SKILL_READS_HARD)

    print("\n=== ③ 跨文件：软线一句提醒、硬线不再执行 ===")
    probe = ReadStub()
    total = _MAX_SKILL_READS_HARD + 3
    steps = [("tool", "load_skill", _read(path=f"ref-{i:02d}.md"))
             for i in range(1, total + 1)] + [("answer", "开写")]
    agent = build_agent(steps, tools=[probe, ActionStub()])
    agent.config.max_iterations = total + 3
    out = await agent.run(Session(user_id="u", conversation_id="c_read_c"), "做一条片")
    last: list[Message] = agent.llm.calls[-1]

    check(out == "开写", f"③ run 正常收尾：{out}")
    check(len(probe.calls) == _MAX_SKILL_READS_HARD,
          f"③ 真打到硬线为止：{len(probe.calls)} 枪"
          f"（软线 {_MAX_SKILL_READS} / 硬线 {_MAX_SKILL_READS_HARD}）")
    notes = _notes(last, "本 run 已经用 load_skill")
    check(len(notes) == 1,
          f"③ 越过软线只说一句（第 {_MAX_SKILL_READS + 1} 枪那一刻）：{len(notes)} 条")
    check(bool(notes) and f"取了 {_MAX_SKILL_READS + 1} 次" in notes[0],
          f"③ 那句话说的是越过那一刻的真数：{notes[:1]}")
    check(bool(notes) and "`next_step_tool`" in notes[0],
          "③ 提醒点名下一步该调的**真实**工具（从注册表读，不写死）")
    check(bool(notes) and "停读" in notes[0],
          "③ 提醒不是「别读了」，是「停读、去动作」")
    refused = [t for t in _tool_msgs(last) if "到上限了" in t]
    check(len(refused) == total - _MAX_SKILL_READS_HARD,
          f"③ 硬线之后每一枪都有回执：{len(refused)} 条")
    check(bool(refused) and "section=键名" in refused[0],
          f"③ 硬线回执给出唯一该走的路（section 单取）：{refused[:1]}")
    check(chain_violations(last) == [], f"③ 链子收口：{chain_violations(last)}")


async def case_d_section_always_allowed() -> None:
    """④ 到硬线之后，section= 单取照常执行（这条闸不能把恢复路径堵死）。"""
    from agent_framework.agent import _MAX_SKILL_READS_HARD

    print("\n=== ④ 硬线之后 section= 仍可单取 ===")
    probe = ReadStub()
    total = _MAX_SKILL_READS_HARD + 1
    steps = [("tool", "load_skill", _read(path=f"ref-{i:02d}.md"))
             for i in range(1, total + 1)]
    steps += [("tool", "load_skill", _read(section="film-knobs")),
              ("answer", "开写")]
    agent = build_agent(steps, tools=[probe, ActionStub()])
    agent.config.max_iterations = len(steps) + 2
    out = await agent.run(Session(user_id="u", conversation_id="c_read_d"), "做一条片")

    check(out == "开写", f"④ run 正常收尾：{out}")
    sections = [c for c in probe.calls if c[3] == "film-knobs"]
    check(len(sections) == 1,
          f"④ 硬线之后 section 单取真的执行了：{sections}")
    receipts = _tool_msgs(agent.llm.calls[-1])
    check(any("section=键名" in t for t in receipts),
          "④ 前面被硬线拦下的那一枪有回执（说明此刻确实越了线）")


class IndexOnlyStub(Tool):
    """与 `LoadSkillTool` 同形：大正文的**整份**加载只回索引，按页取才给正文。

    真机 run ``a1f66c02f3c9`` 的死法：第一步 `load_skill(name)` 只拿到目录，却被
    记成「1 行起整份已给过」；之后每次按行取正文都被判成重复，模型连打 6 枪同一
    调用，整条 run 按原地打转停下。这条替身把这个形状固定下来。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, int | None, int | None, str]] = []

    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def description(self) -> str:
        return "大正文整份只给索引的替身"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object",
                "properties": {"name": {"type": "string"}, "path": {"type": "string"},
                               "offset": {"type": "integer"}, "limit": {"type": "integer"},
                               "section": {"type": "string"}},
                "required": ["name"]}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, name: str, path: str | None = None,
                      offset: int | None = None, limit: int | None = None,
                      section: str | None = None) -> str:
        from agent_framework.skill import INDEX_ONLY_NOTE

        self.calls.append((path, offset, limit, section or ""))
        if section:
            return f"[skill:{name} section={section}] 这一块的正文。"
        if offset is None and limit is None:
            return ("[skill:motion_explainer_skill location=skills/x/SKILL.md]\n"
                    "章节目录：\n  1  总则\n  99  每镜必画\n"
                    f"[SKILL_INDEX_END]\n（这份正文共 1422 行——**{INDEX_ONLY_NOTE}**。"
                    "按分段目录用 section，或 offset/limit 取正文。）")
        return f"[skill:{name}] 第 {offset} 行起 × {limit} 行的正文。"


async def case_e_index_only_whole_read_does_not_cover_body() -> None:
    """⑤ 整份加载只回索引时，不能记成「正文已经给过」（run a1f66c02f3c9）。"""
    print("\n=== ⑤ 只给了目录 ≠ 给了正文 ===")
    probe = IndexOnlyStub()
    steps = [
        ("tool", "load_skill", _read()),                     # 只拿到索引
        ("tool", "load_skill", _read(offset=1, limit=1422)),  # 这次真给正文
        ("tool", "load_skill", _read(limit=98)),              # 已被正文覆盖 → 不重发
        ("answer", "开写"),
    ]
    agent = build_agent(steps, tools=[probe, ActionStub()])
    agent.config.max_iterations = len(steps) + 2
    out = await agent.run(Session(user_id="u", conversation_id="c_read_e"), "做一条片")
    last: list[Message] = agent.llm.calls[-1]

    check(out == "开写", f"⑤ run 正常收尾：{out}")
    check(len(probe.calls) == 2,
          f"⑤ 整份索引不挡正文：真执行 {len(probe.calls)} 枪（应当是索引 + 1:1422）")
    check(bool(probe.calls) and probe.calls[1][:3] == (None, 1, 1422),
          f"⑤ 第二枪是真取正文的那一枪：{probe.calls[1:2]}")
    covered = [t for t in _tool_msgs(last) if "本 run 已经给过" in t]
    check(len(covered) == 1,
          f"⑤ 只有「真给过之后」的重叠才被拦：{len(covered)} 条")
    check(chain_violations(last) == [], f"⑤ 链子收口：{chain_violations(last)}")


async def main() -> int:
    await case_a_covered_window_is_not_resent()
    await case_b_attachment_covered_window()
    await case_c_cross_file_soft_and_hard_lines()
    await case_d_section_always_allowed()
    await case_e_index_only_whole_read_does_not_cover_body()
    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
