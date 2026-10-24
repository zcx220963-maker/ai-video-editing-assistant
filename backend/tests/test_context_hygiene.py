"""上下文面卫生：账面（T37）与预算闸㉓（T38）——§5.7 第①条。

运行：  PYTHONPATH=/app python tests/test_context_hygiene.py     （容器内）
        PYTHONPATH=.   python tests/test_context_hygiene.py     （宿主）

这一支测的不是「上下文变小了」，而是**装配面有没有账、超线会不会红**：
① 账面真的存在于代码里（T37 的「先红」断言——实现前 `context_ledger` 在整个仓里
   零命中，所以这支在实现前必红）；② 账面每一行的字节与真发出去的那段逐字对得上；
③ 超长 `USAGE_NOTE` 让请求**发不出去**（不是截断），删回预算内即绿。
④ 断值表与目标表是两件事：断值今天必须过得去（否则每一条真机 run 当场死），
   目标可以还没达标，但**每个没达标的块都要挂着欠哪个任务**。
⑤ 逐块红样：系统提示词 / `<skills>` 清单 / 技能正文各顶过断值一次，**三次都红**，
   且每次只红那一块——只红一块才说明预算表被逐块读了（⑩）。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.context import (  # noqa: E402
    ASSEMBLY_REF, ContextBudgetError, ContextBuilder, LEDGER_ENTRY_TYPE, LEDGER_KINDS,
    Section, SECTION_BUDGET_BYTES, SECTION_BUDGET_DEBT, SECTION_TARGET_BYTES,
    section_budget, section_bytes, section_name, section_target, tools_box,
)
from agent_framework.session import Session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class _Src:
    """最小 ContextSource：只实现协议要求的 name + render。"""

    def __init__(self, name, text, **kw):
        self.name = name
        self._text = text
        for k, v in kw.items():
            setattr(self, k, v)

    async def render(self, query, *, user_id=None):
        return self._text


def _sess():
    return Session(user_id="u1", conversation_id="c1")


def _build(builder, text="你好"):
    return asyncio.run(builder.build(_sess(), text))


def _mk(*, system_prompt="系统提示词", runtime=lambda: "当前时间：2026-10-13T00:00:00",
        sources=(), **kw):
    return ContextBuilder(system_prompt=system_prompt, runtime_context=runtime,
                          context_sources=list(sources), **kw)


# --------------------------------------------------------------------------


def case_ledger_exists() -> None:
    """① T37 的「先红」断言：账面真的落进了代码，不是文档里的一个词。

    实现前 `context_ledger` 在四个目录零命中（§5.4 的原话），所以这支那时必红。
    """
    print("\n① 账面排在代码里（`context_ledger` 四目录零命中 → 先红）")
    hits = [p for d in ("agent_framework", "storyline_server", "tests")
            for p in (ROOT / d).rglob("*.py") if "context_ledger" in p.read_text(encoding="utf-8")]
    check((ROOT / "agent_framework" / "context.py") in hits,
          f"装配出口产 `context_ledger`（命中 {len(hits)} 个文件）")
    check(LEDGER_ENTRY_TYPE == "context_ledger", "留档通道里的类型名就是 `context_ledger`")
    check("context_ledger" in (ROOT / "tests" / "test_context_hygiene.py").read_text(encoding="utf-8"),
          "这支测试自己也在把账当判据用（不是在断言一个常量）")
    agent_src = (ROOT / "agent_framework" / "agent.py").read_text(encoding="utf-8")
    check('"type": LEDGER_ENTRY_TYPE' in agent_src,
          "留档那一行引的是这个常量（两处各写一遍字符串就是下一个「两份真相」）")
    src = (ROOT / "agent_framework" / "context.py").read_text(encoding="utf-8")
    for key in ('"sections"', '"tools"', '"over_budget"', '"orphan_sections"'):
        check(key in src, f"§5.7 冻结的键 {key} 在出口处")

    ledger = _mk().last_ledger
    _build(_mk())
    ledger = _mk()
    _build(ledger)
    got = set(ledger.last_ledger)
    check({"sections", "tools", "over_budget", "orphan_sections"} <= got,
          f"账面键齐（多出来的也照实报：{sorted(got - {'sections', 'tools', 'over_budget', 'orphan_sections'})}）")
    check("over_target" in got, "多一栏 `over_target` 把「超目标」与「超断值」分开记")


def case_sections_shape() -> None:
    """② 逐行的字节与真发出去的那段逐字对得上（账面的全部意义）。"""
    print("\n② 账面逐行：字节、kind、条件键、引用方")
    b = _mk(sources=[_Src("memory", "  用户偏好：文艺风格  ",
                          ledger_kind="always", ledger_referenced_by=("update_memory",))])
    msgs = _build(b)
    rows = b.last_ledger["sections"]
    names = [r["name"] for r in rows]
    check(names == ["system_prompt", "runtime", "memory"], f"分块与顺序：{names}")
    for r in rows:
        check(set(r) == {"name", "bytes", "kind", "condition_key", "referenced_by"},
              f"{r['name']} 的五个字段与 §5.7 格式一致")
        check(r["kind"] in LEDGER_KINDS, f"{r['name']}.kind 在三态里（{r['kind']}）")
    check(all(r["referenced_by"] for r in rows), "三个结构化块都声明了引用方（不会误判成孤儿）")
    check(b.last_ledger["orphan_sections"] == [], "没有「常驻却没人声明读它」的块")

    # 逐字对账：把发布出去的 system_content 按 \n\n 切开，每段字节必须与账里那一行相等
    parts = msgs[0]["content"].split("\n\n")
    check(len(parts) == len(rows), f"发出去的段数与账里的行数相等（{len(parts)} vs {len(rows)}）")
    for part, row in zip(parts, rows):
        check(section_bytes(part) == row["bytes"],
              f"{row['name']} 的字节逐字对上（{section_bytes(part)}）")

    # 空段不进账（记一条 0 字节的段只会让人怀疑它到底发了没有）。
    # 「不注入」在协议里的表达是 render 返回 None/空串（context.py 的 ContextSource 契约），
    # 不是返回空白——空白会被包成 <skill> 空段发出去，那是既有行为，这里照旧。
    b2 = _mk(sources=[_Src("skill", None)])
    _build(b2)
    check([r["name"] for r in b2.last_ledger["sections"]] == ["system_prompt", "runtime"],
          "render 返 None 的 context_source 不进账面")

    # 条件段
    b3 = _mk()
    _build(b3)
    check("attachments" not in [r["name"] for r in b3.last_ledger["sections"]],
          "没附件时 <attachments> 不出现")


def case_kinds_and_names() -> None:
    """③ 三态与自动命名：裸串按首个标签取名，认不出就照实说 unlabeled。"""
    print("\n③ 三态 / 自动命名 / kind 校验")
    check(section_name('<planning_round>\nx\n</planning_round>') == "planning_round",
          "裸标签 → 块名")
    check(section_name('<skill_instructions name="motion_explainer">\nb\n</skill_instructions>')
          == "skill_instructions:motion_explainer", "带 name= 的标签 → 块名:名字")
    check(section_name("没有标签的裸文本") == "unlabeled", "不猜：认不出来就叫 unlabeled")
    try:
        Section(name="x", text="y", kind="常发")
        check(False, "kind 必须在三态里（这里该抛）")
    except ValueError as exc:
        check("always" in str(exc), "kind 越界当场抛而不是记账面上")

    b = _mk()
    _build(b, "#")  # extra_sections 走裸串那一路
    b = _mk()
    asyncio.run(b.build(_sess(), "q", extra_sections=[
        '<approved_plan>\n照单执行\n</approved_plan>',
        Section(name="planning_round", text="<planning_round>\nx\n</planning_round>",
                kind="conditional", condition_key="planning_round",
                referenced_by=("submit_plan",),
                sub_blocks=(("knobs", "开关清单一行"),)),
    ]))
    rows = {r["name"]: r for r in b.last_ledger["sections"]}
    check("approved_plan" in rows, "裸串自动命名进账")
    check(rows["approved_plan"]["kind"] == "on_demand",
          "裸串默认按「按需」记（产出方知道更多就该说更多）")
    check(rows["planning_round"]["condition_key"] == "planning_round",
          "带身份的 Section 连条件键一起进账")
    check("knobs" in rows, "子块单独成行（`knobs` 在 §5.4 末列单列一行）")
    check(rows["knobs"]["bytes"] < rows["planning_round"]["bytes"],
          "子块字节含在父段里（逐块看，不是加起来看总账）")


def case_tools_box() -> None:
    """④ tools 那栏：装不出来的数记 None（未判），不许伪造成 0。"""
    print("\n④ tools 栏（装配出口填不了，由 agent 侧补）")
    b = _mk()
    _build(b)
    t = b.last_ledger["tools"]
    check(t == {"sent": None, "total_registered": None, "filter": None, "largest": []},
          f"没装工具表时三格为 None/空：{t}")
    defs = [{"type": "function", "function": {"name": n, "description": "x" * k}}
            for n, k in (("a", 10), ("b", 500), ("c", 100), ("d", 20))]
    box = tools_box(defs, total_registered=53, filter_name="planning_registry")
    check(box["sent"] == 4 and box["total_registered"] == 53,
          f"sent=4 < total=53（差 = 按轮裁剪省下的量）")
    check(box["largest"][0]["name"] == "b" and len(box["largest"]) == 3,
          f"largest 只留最大的三份：{[r['name'] for r in box['largest']]}")
    check(box["largest"][0]["bytes"] == section_bytes(
        '{"type": "function", "function": {"name": "b", "description": "%s"}}' % ("x" * 500)),
        "largest 的字节口径与 sections 一致（同一把尺）")


def case_over_budget_raises() -> None:
    """⑤ 顶过断值 → 请求发不出去；删回预算内即绿（双向，D-红先）。"""
    print("\n⑤ 超长 USAGE_NOTE：请求发不出去（不是截断）")
    from agent_framework.memory import MemoryContextSource

    class _Store:
        def __init__(self, text):
            self._t = text
        async def entries(self, user_id):
            return {"user": self._t}

    src = MemoryContextSource(_Store("用户偏好：文艺风格"))
    b = _mk(sources=[src])
    _build(b)
    row = next(r for r in b.last_ledger["sections"] if r["name"] == "memory")
    check(row["bytes"] <= SECTION_BUDGET_BYTES["memory"],
          f"正常记忆在断值内（{row['bytes']} ≤ {SECTION_BUDGET_BYTES['memory']}）")

    # 注入一段超长 USAGE_NOTE（§5.7 第①条指定的红法）
    src.USAGE_NOTE = "用法：" + "这段教程每次常发，长了就是堆上下文。" * 400
    try:
        _build(_mk(sources=[src]))
        check(False, "超长 USAGE_NOTE 该让请求发不出去")
    except ContextBudgetError as exc:
        msg = str(exc)
        check("memory" in msg and "> 预算" in msg,
              "报红说清哪块超、超了多少（不是只说「超预算了」）")
        check("不许截断" in msg, "报红写明不许用截断顶过去")

    # 删回预算内即绿
    src.USAGE_NOTE = "用法：短了。"
    _build(_mk(sources=[src]))
    check(True, "删回预算内即绿（同一支测试双向跑）")

    # 断值表逐块真被读：换一块顶（系统提示词）
    try:
        _build(_mk(system_prompt="字" * 1200))
        check(False, "系统提示词顶过断值也该红")
    except ContextBudgetError as exc:
        check("system_prompt" in str(exc), "换一块顶 → 那一块报红（表是被逐块读的）")


def case_per_block_red() -> None:
    """⑩ ㉓ 逐块红样：系统提示词 / `<skills>` 清单 / 技能正文**各顶过断值一次**。

    判据由**断值表**驱动——样本大小照着 `section_budget(name)` 造，不写字面字节数；
    红的必须是**那一块且只有那一块**（`over_budget == [那一块]`）。三次里有任意一次
    没红，就说明预算表没被逐块读：那是这条闸最危险的失效形态（闸看着在，其实是装饰）。

    三块都走**真产出方**，不用手搓字符串冒充：
    - `system_prompt`：装配自己发的那一段（`ContextBuilder.system_prompt`）；
    - `skills`：`SkillManifestContextSource`（`<skills>` 清单的真出口）；
    - `skill_instructions:<名>`：`plan/skills.py: preload_skills` 的原样返回
      （执行轮预注入技能正文走的正是这条），它自动命名成 `skill_instructions:big`，
      断值按前缀 `skill_instructions` 取——顺带钉住前缀那条规则真的接上了。
    """
    print("\n⑩ 逐块红样：三块各顶一次，三次都红（只红一块才是被逐块读）")
    from agent_framework.plan.skills import preload_skills
    from agent_framework.skill import Skill, SkillManifestContextSource

    def _pad(n_bytes: int) -> str:
        """按 UTF-8 字节造一段填充：CJK 3 B/字，末尾用 ASCII 补足余数。"""
        n_bytes = max(n_bytes, 3)
        s = "字" * (n_bytes // 3)
        return s + "x" * (n_bytes - len(s.encode("utf-8")))

    class _ManifestLoader:
        """只实现 discover 的替身：技能**名**就是要顶的那一块。

        2026-10-22 起清单里的 description 只放一句话摘要（`Skill.catalog_summary`，
        单条有上限），拿它当红样顶不到预算——那段长度是结构上限封住的，**这是有意的**
        （见 §5.4.1 那一行）。所以红样改顶**没有上限的那一栏**：技能名。它仍走真产出方
        `SkillManifestContextSource`，判的仍是「这一块被逐块读了吗」。
        """

        def __init__(self, name_bytes: int) -> None:
            self._n = name_bytes

        async def discover(self, user_id=None):  # noqa: ARG002
            return [Skill(name=_pad(self._n), description="x", always=False,
                          location="skills/big", body="")]

    class _BodyLoader:
        """只实现 get 的替身：正文就是技能正文那一块。"""

        def __init__(self, body_bytes: int) -> None:
            self._n = body_bytes

        async def get(self, name):
            return Skill(name=name, description="x", always=False,
                         location=f"skills/{name}", body=_pad(self._n))

    def _plan_system_prompt(n):
        return _mk(system_prompt=_pad(n)), []

    def _plan_skills(n):
        return _mk(sources=[SkillManifestContextSource(_ManifestLoader(n))]), []

    def _plan_instructions(n):
        extra = asyncio.run(preload_skills(_BodyLoader(n), ["big"]))
        return _mk(), extra

    plans = [
        ("系统提示词", "system_prompt", _plan_system_prompt),
        ("<skills> 清单", "skills", _plan_skills),
        ("技能正文", "skill_instructions:big", _plan_instructions),
    ]
    for label, block, plan in plans:
        budget = section_budget(block)
        check(budget is not None, f"{label}：{block} 在断值表里（{budget} B）")
        # 红样比断值多 3 B——「等于」不算超（`bytes > budget` 才是），差一点不红
        # 正是这条闸该有的行为：它拦的是越线，不是踩线。
        builder, extra = plan(budget + 3)
        try:
            asyncio.run(builder.build(_sess(), "你好", extra_sections=extra))
            check(False, f"{label}：顶上断值该红（样本是按 {budget + 3} B 造的）")
        except ContextBudgetError as exc:
            over = builder.last_ledger["over_budget"]
            check(over == [block],
                  f"{label}：只有 {block} 上榜，别的块没被牵连：{over}")
            check(f"{block}：" in str(exc) or f"- {block}:" in str(exc),
                  f"{label}：报红点名的是这一块")
            check(f"预算 {budget} B" in str(exc),
                  f"{label}：报的是表里那个数（{budget} B），不是另写一个")
            check("不许截断" in str(exc), f"{label}：报红写明不许用截断顶过去")
        # 双向：删回预算内即绿（只测「会红」会漏掉「什么都在红」）
        builder2, extra2 = plan(max(budget // 4, 12))
        asyncio.run(builder2.build(_sess(), "你好", extra_sections=extra2))
        row = next(r for r in builder2.last_ledger["sections"]
                   if r["name"].startswith(block.split(":")[0]))
        check(row["bytes"] <= budget,
              f"{label}：缩回 {row['bytes']} B ≤ {budget} B 即绿（同一支双向跑）")


def case_targets_and_debt() -> None:
    """⑥ 断值 vs 目标：断值今天过得去，没达标的块必须挂着欠账。"""
    print("\n⑥ 断值表 / 目标表 / 欠账表")
    check(set(SECTION_BUDGET_DEBT) <= set(SECTION_TARGET_BYTES),
          "每个欠账块都在目标表里（不许挂一个没有目标的账）")
    for name, (target, ref, why) in sorted(SECTION_BUDGET_DEBT.items()):
        check(SECTION_TARGET_BYTES[name] == target,
              f"{name}：欠账表与目标表的数一致（{target}）")
        check(ref > target, f"{name}：它**确实**还超着目标（现值 {ref} > 目标 {target}）")
        check(len(why) > 8, f"{name}：写了欠哪个任务（{why.split('：')[0]}）")
        check(name in SECTION_BUDGET_BYTES or name == "knobs",
              f"{name}：要么有断值，要么在注释里说明今天为什么不设断值")
    for name, budget in sorted(SECTION_BUDGET_BYTES.items()):
        target = section_target(name)
        check(target is None or budget >= target,
              f"{name}：断值不低于目标（{budget} ≥ {target}）——否则每一条真机 run 当场死")
    # 目标不许「比断值还松」：那样它就不是目标了
    for name, target in sorted(SECTION_TARGET_BYTES.items()):
        budget = SECTION_BUDGET_BYTES.get(name)
        check(budget is None or budget >= target or name in SECTION_BUDGET_DEBT,
              f"{name}：目标严于断值又没挂欠账 → 该有一条（{target} vs {budget}）")


def case_orphans() -> None:
    """⑦ orphan_sections：常驻、却没人声明读它的块要露出来。"""
    print("\n⑦ 孤儿段：always 且 referenced_by 为空")
    b = _mk(sources=[_Src("mystery", "每轮都发，但没人说读它")])
    _build(b)
    check(b.last_ledger["orphan_sections"] == ["mystery"],
          f"没人声明消费者的常驻段上榜：{b.last_ledger['orphan_sections']}")
    check(b.last_ledger["over_budget"] == [], "孤儿段只登记，不拦（它不是预算问题）")
    b2 = _mk(sources=[_Src("memory", "x", ledger_kind="always",
                           ledger_referenced_by=("update_memory",))])
    _build(b2)
    check(b2.last_ledger["orphan_sections"] == [], "声明了消费者就不上榜")
    check(ASSEMBLY_REF == ("assembly",),
          "结构化装配块自报 assembly（有明确反面，判据才不是装饰）")
    b3 = _mk(runtime=None, sources=[_Src("mystery", "只有它一个")])
    _build(b3)
    check(b3.last_ledger["orphan_sections"] == ["mystery"],
          "runtime 关掉也不影响判据（它本来就被点名）")


def case_planning_blocks() -> None:
    """⑧ 规划轮段走带子块的出口：整段文本与纯文本出口逐字相同。"""
    print("\n⑧ planning_section_blocks 与 planning_section 同源")
    from agent_framework.plan.prompt import planning_section, planning_section_blocks

    class _Gate:
        prompt_library = None
        def whitelist(self):
            return ["group_clips"]
        def explicit_call_nodes(self):
            return {"group_clips"}
        async def knob_facts(self):
            return [await self.knob_facts_for("group_clips")]
        async def knob_facts_for(self, node):
            knobs = ([{"key": "max_sec", "kind": "number", "values": "1~60",
                       "default": "12", "note": ""}] if node == "group_clips" else [])
            return {"node": node, "knobs": knobs,
                    "keys": [k["key"] for k in knobs]}
    gate = _Gate()
    # T40 起这一段是**条件**注入（只发本轮计划命中的节点），所以这里给一份计划上下文
    # （待续跑的未履行节点就是 group_clips）；不给的那一支在 ⑩ 的 `knobs` 那一条里。
    ctx = {"pending": {"plan_id": "p-1", "label": "旧卡", "run_id": "r-1",
                       "unfulfilled": ["group_clips"], "asked": "要不要切短一点"}}
    plain = asyncio.run(planning_section(gate, **ctx))
    text, blocks = asyncio.run(planning_section_blocks(gate, **ctx))
    check(text == plain, "两个出口的整段文本逐字相同（子块不是另算一遍）")
    _noknob_text, noknob_blocks = asyncio.run(planning_section_blocks(gate))
    check([n for n, _t in noknob_blocks if n == "knobs"] == [],
          "没有计划上下文：`knobs` 子块整个不上报（T40，账面因此如实 0 字节）")
    names = [n for n, _t in blocks]
    check("knobs" in names, f"子块带上了开关清单：{names}")
    check(all(isinstance(t, str) and t for _n, t in blocks), "子块文本非空才上报")
    b = _mk()
    asyncio.run(b.build(_sess(), "q", extra_sections=[Section(
        name="planning_round", text=text, kind="conditional",
        condition_key="planning_round", referenced_by=("submit_plan",),
        sub_blocks=blocks)]))
    rows = {r["name"]: r for r in b.last_ledger["sections"]}
    check(rows["planning_round"]["bytes"] <= SECTION_BUDGET_BYTES["planning_round"],
          f"规划段在断值内（{rows['planning_round']['bytes']} ≤ "
          f"{SECTION_BUDGET_BYTES['planning_round']}）")
    check(rows["knobs"]["bytes"] > 0, "knobs 子块按真字节进账")


def case_doc_sync() -> None:
    """⑨ §5.4.1 那张表与常量表逐行对账（同批改两处，单改即红）。

    **指南不在镜像里**（Dockerfile 只 COPY `backend/`），所以这一条在容器内是
    **未判**并显式打一行——不是绿。落地前跑对账的那条腿是仓（宿主）：那里的
    `REMOVIDE_DEVELOPMENT_GUIDE.md` 与 `backend/tests/` 是同一份工作树的文件。
    """
    print("\n⑨ 文档 §5.4.1 ↔ `SECTION_BUDGET_BYTES` / `SECTION_TARGET_BYTES`")
    guide_path = ROOT.parent / "REMOVIDE_DEVELOPMENT_GUIDE.md"
    if not guide_path.is_file():
        print(f"  · 未判：{guide_path} 不在（镜像里只有 backend/）——"
              f"这条对账在仓里跑，容器内不判")
        return
    guide = guide_path.read_text(encoding="utf-8")
    if "### 5.4.1" not in guide:
        check(False, "指南里有 §5.4.1 断值表（找不到就没法对账）")
        return
    body = guide.split("### 5.4.1", 1)[1].split("\n### ", 1)[0]
    rows = [ln for ln in body.splitlines()
            if ln.startswith("| `") and "---" not in ln]
    check(len(rows) >= 5, f"断值表有 {len(rows)} 行")

    def _nums(cell: str) -> list[int]:
        return [int(s.replace(",", "")) for s in re.findall(r"[\d,]+(?=\s*B)", cell)]

    seen: set[str] = set()
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        names = re.findall(r"`([^`]+)`", cells[0])
        seen.update(names)
        if "不设断值" in cells[1]:
            for n in names:
                check(n not in SECTION_BUDGET_BYTES,
                      f"{n}：文档说今天不设断值，代码里也不该有（表是被逐块读的）")
            continue
        budget = _nums(cells[1])
        target = _nums(cells[3])
        check(len(budget) == 1 and len(target) == 1,
              f"{names[0]}：这一行的断值与目标各只有一个数（{budget} / {target}）")
        for n in names:
            check(SECTION_BUDGET_BYTES.get(n) == budget[0],
                  f"{n}：断值与文档一致（代码 {SECTION_BUDGET_BYTES.get(n)} = 文档 {budget[0]}）")
            check(SECTION_TARGET_BYTES.get(n) == target[0],
                  f"{n}：目标与文档一致（代码 {SECTION_TARGET_BYTES.get(n)} = 文档 {target[0]}）")
    for n in sorted(set(SECTION_BUDGET_BYTES) | set(SECTION_TARGET_BYTES)):
        check(n in seen, f"{n}：常量表里的块在文档 §5.4.1 里有交代（不许只在代码里）")


def main() -> int:
    print("上下文面卫生：账面（T37）+ 预算闸㉓（T38）")
    case_ledger_exists()
    case_sections_shape()
    case_kinds_and_names()
    case_tools_box()
    case_over_budget_raises()
    case_targets_and_debt()
    case_orphans()
    case_planning_blocks()
    case_doc_sync()
    case_per_block_red()
    print(f"\n{'全部通过' if not _fails else '失败'}：{_checks - _fails}/{_checks}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
