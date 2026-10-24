# -*- coding: utf-8 -*-
"""规划轮 system 段的**逐字**金样：文案搬进 prompts/planning_round.md 后不许改一个字。

钉的是这次迁移的唯一风险面：把几十行按条件拼装的文案换成「模板 + 块守卫」，
渲染结果哪怕差一个空格，模型的行为就可能漂。所以这里不比「意思差不多」，
比的是**字符串相等**。

金样在拆之前从旧代码逐字捕获（``tests/data/planning_round_golden.json``，
9 条用例覆盖：最小 / 待确认卡开关 / 换一版旧卡 / 反馈原话 / 待续跑 / 同会话历史 /
全开 / 无节点无开关 / 有节点无开关）。这份用例跑两遍同一批输入：

① 没接库（``prompt_library=None``）——这条路径**曾经**是「回落到内联模板」，
   2026-10-14 内联副本删掉后它变成「退回仓库自带 prompts/ 读」。留着这一组是因为
   ``PlanGate`` 的 ``prompt_library`` 是可选的，离线替身/旧装配就是不传；
   渲染必须仍然成立，不许因为「库没接」而拼不出提示或拼出空段。
② 仓库自带 prompts/（默认生效的那条路径）——生产实际读的文案。

**原 ③「两份模板互相逐字相同」的漂移守卫已撤**（同一次改动）：它守的是
「文件一份、内联一份，两边都会漂」，而现在只剩盘上一份，那条比对退化成了
「拿文件跟它自己比」——必然绿、等于没守。防漂移改由 ``manifest.json`` 与
``validate_manifest`` 从另一头守（清单说有就得有、加了文件要记账、单份不得超上限）。

另加一条接线守卫：``run_server.py`` 构造 ``PlanGate`` 时必须真的传 ``prompt_library``。
「写了但没人接」是本项目死过一次的失败模式（Bootstrap 的 bootstrap_dir 一直没传），
金样全绿也可能意味着磁盘那份从来没被读过。

**T40 追加**（2026-10-13）：``knobs`` 段从「白名单非空就每轮整份发」改成条件注入，
于是金样里 9 条有 8 条的渲染结果**本该**变（§5.7 第③条），逐字重烤过一次并逐条
核过差异只有 knobs 那一块；另加两支用例把两个方向都钉住，并新开两节：

⑦ ``knobs`` 段的条件判据（命中才发 / 无命中整块不发 / 只发命中的那几个节点 /
   判据只认**计划上下文**，已产出节点与自由闲聊不算）；
⑧ 八个块守卫**双向**各来一遍（此前金样只盖住「该开时开了」，「该关没关」没人测）。

运行：  python tests/test_planning_prompt_golden.py
"""

from __future__ import annotations

from pathlib import Path as _Path
import sys
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import ast
import asyncio
import json
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.plan.prompt import (  # noqa: E402
    PLANNING_PROMPT_FILE, planning_section, planning_section_blocks,
)
from agent_framework.prompts import (  # noqa: E402
    DEFAULT_PROMPTS_DIR, build_prompt_library, placeholders_in,
)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "tests" / "data" / PLANNING_PROMPT_FILE.replace(
    ".md", "_golden.json")).read_text(encoding="utf-8"))

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class _StubGate:
    """只给 planning_section 用到的五张口：白名单、开关事实（整批 / 逐节点）、提示词库、显式上卡节点。

    ``explicit_call_nodes`` 按白名单取交集（真实契约里这个集合本就 ⊆ 白名单）：
    白名单里没有那几张卡时它就是空集，``{?must_include}`` 整段不出现——守卫的
    「有条件才说话」这一支也一并被金样盖住。
    """

    def __init__(self, whitelist, knob_facts, library=None) -> None:
        self._wl, self._facts, self.prompt_library = whitelist, knob_facts, library

    def whitelist(self) -> list:
        return list(self._wl)

    def explicit_call_nodes(self) -> set:
        return set(self._wl) & {"filter_clips", "group_clips"}

    async def knob_facts(self) -> list:
        return json.loads(json.dumps(self._facts))  # 防被渲染过程就地改

    async def knob_facts_for(self, node: str) -> dict:
        """单个节点的开关事实（T40 起规划段按命中节点逐个取）。

        ``keys`` 与 ``knobs`` 同源：真装配里由 ``vocab.knob_facts_for`` 一次算出，
        替身这里照同一形状补上，免得「替身有的口真装配没有」这种假绿。
        """
        for fact in await self.knob_facts():
            if fact["node"] == node:
                fact["keys"] = [k["key"] for k in fact["knobs"]]
                return fact
        return {"node": node, "knobs": [], "keys": []}


def _diff(a: str, b: str) -> str:
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return (f"\n      断点 @{at}\n      金样: …{a[max(0, at - 40):at + 40]!r}"
            f"\n      实得: …{b[max(0, at - 40):at + 40]!r}")


def _render(case, library):
    gate = _StubGate(case["whitelist"], case["knob_facts"], library)
    return asyncio.run(planning_section(gate, **case["kwargs"]))


def case_inline_fallback() -> None:
    print("\n① 没接提示词库（prompt_library=None）也逐字等于金样（退回仓库目录读）")
    for case in GOLDEN["cases"]:
        got = _render(case, None)
        check(got == case["expect"],
              f"{case['name']}（{len(got)} 字符）" + ("" if got == case["expect"]
                                                    else _diff(case["expect"], got)))


def case_on_disk() -> None:
    print(f"② 仓库自带 prompts/{PLANNING_PROMPT_FILE} 逐字等于同一批金样")
    lib = build_prompt_library(None)
    check(PLANNING_PROMPT_FILE in lib.loaded_from_disk(),
          f"文件真的在磁盘上（读到的清单：{lib.loaded_from_disk()}）")
    for case in GOLDEN["cases"]:
        got = _render(case, lib)
        check(got == case["expect"],
              f"{case['name']}（{len(got)} 字符）" + ("" if got == case["expect"]
                                                    else _diff(case["expect"], got)))


def case_no_leftover_placeholder() -> None:
    print("③ 渲染结果里不留守卫行与未替换的数据占位符")
    everything = next(c for c in GOLDEN["cases"] if c["name"] == "everything")
    data_keys = ("nodes", "knobs", "must_include", "must_include_notes", "prior_cards",
                 "feedback_quote",
                 "session_history_rows", "pending_plan_id", "pending_label",
                 "pending_run_id", "pending_unfulfilled", "pending_asked",
                 "capability_notes")
    for label, library in (("未接库", None), ("磁盘", build_prompt_library(None))):
        got = _render(everything, library)
        check("{?" not in got, f"{label}版：块守卫行不会漏进上下文")
        left = [f"{{{k}}}" for k in data_keys if f"{{{k}}}" in got]
        check(not left, f"{label}版：数据键全被替换，无残留 {left}")


def case_guard_key_contract() -> None:
    print("④ 模板要的键 == 代码给的键（少一个键就整段静默消失）")
    wanted = {"has_pending_plan", "prior_cards", "feedback_quote", "has_pending",
              "session_history_rows", "nodes", "must_include", "must_include_notes",
              "knobs",
              "pending_plan_id", "pending_label", "pending_run_id",
              "pending_unfulfilled", "pending_asked",
              # R13：计划期探活那一段（`capability.py`）。它与其他守卫不同——
              # 值不由本模块算，而由 `agent.plan()` 现查活库后递进来；
              # 递空串（离线装配 / 启动期自检）时整块不出现，与加这一段之前逐字相同。
              "capability_notes"}
    file_keys = placeholders_in(
        (DEFAULT_PROMPTS_DIR / PLANNING_PROMPT_FILE).read_text(encoding="utf-8"))
    check(file_keys == wanted, f"磁盘模板的键名/守卫名固定：{sorted(file_keys)}")

    given = {"nodes", "must_include", "must_include_notes", "knobs",
             "has_pending_plan", "prior_cards",
             "feedback_quote", "session_history_rows", "has_pending", "pending_plan_id",
             "pending_label", "pending_run_id", "pending_unfulfilled", "pending_asked",
             "capability_notes"}
    check(wanted <= given, "planning_section 的 values 覆盖了模板要的每个键")


def case_templates_identical() -> None:
    print("⑤ 规划轮文案只剩盘上一份（内联副本已删，改回字面量会红）")
    import agent_framework.plan.prompt as P

    on_disk = (DEFAULT_PROMPTS_DIR / PLANNING_PROMPT_FILE).read_text(encoding="utf-8")

    def norm(t: str) -> str:
        return "\n".join(ln.rstrip() for ln in t.replace("\r\n", "\n").split("\n")).rstrip("\n")

    # 内联模板必须真的不在了。留着它＝又回到「同一段文案两处各写一份」，
    # 而当初那条「两份互相逐字相同」的守卫就是被这么绕空的：改一处、另一处没人管。
    check(not hasattr(P, "_PLANNING_TEMPLATE"),
          "模块里没有 _PLANNING_TEMPLATE 了（正文只在 prompts/planning_round.md）")
    # 拿两条读法互相对账，仍有意义：库没接与库接了必须读到**同一份**正文。
    from agent_framework.prompts import prompt_text
    check(norm(prompt_text(None, PLANNING_PROMPT_FILE)) == norm(on_disk),
          "prompt_text(库=None) 与磁盘文件逐字相同（退回仓库目录那条路读对了）")


def _plangate_call_kwargs(path: Path) -> set[str]:
    """run_server 里 ``PlanGate(...)`` 的形参名（AST 取，不靠正则认注释里的字样）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "PlanGate":
                found.update(kw.arg for kw in node.keywords if kw.arg)
    return found


def case_wired_in_production() -> None:
    print("⑥ 生产装配真的传了 prompt_library（不是写了模板没人接）")
    kw = _plangate_call_kwargs(ROOT / "run_server.py")
    check("prompt_library" in kw, f"run_server 的 PlanGate(...) 带上 prompt_library：{sorted(kw)}")
    src = (ROOT / "run_server.py").read_text(encoding="utf-8")
    check(src.index("prompt_library = build_prompt_library")
          < src.index("plan_gate = PlanGate("),
          "提示词库在计划门之前构造（传的是实例，不是 None）")


# ---- T40：`knobs` 段的条件判据 + 八个守卫的双向 ----

_KNOB_HEAD = "能上卡的开关（服务端按节点真实 schema 现取"
_CARD_SWITCH = "卡面开关（param_options）只能从上面这份清单里挑"

_AXIS_WL = ["asr", "select_BGM", "render_video"]
_AXIS_FACTS = [
    {"node": "select_BGM", "knobs": [
        {"key": "bgm_source", "kind": "enum", "values": ["库内", "原声"],
         "default": "库内", "note": "曲库真实标签"},
        {"key": "volume", "kind": "number", "values": "0~2", "default": "1.0", "note": ""}]},
    {"node": "render_video", "knobs": [
        {"key": "subtitle", "kind": "bool", "values": "on/off",
         "default": "", "note": "烧字幕"}]},
]


def _render_blocks_of(**kwargs):
    """按同一套白名单/开关事实渲染一次，返回 ``(整段文本, 子块表)``。"""
    gate = _StubGate(_AXIS_WL, _AXIS_FACTS, None)
    return asyncio.run(planning_section_blocks(gate, **kwargs))


def _knob_bytes(blocks) -> int:
    return sum(len(t.encode("utf-8")) for n, t in blocks if n == "knobs")


def case_knobs_conditional() -> None:
    """⑦ `knobs` 段只在本轮计划命中「暴露旋钮的节点」时才发（§5.7 第③条）。

    每条都**双向**跑：该发的方向、不该发的方向各断言一次，否则「永远不发」也能通过。
    第一节（无任何计划上下文 → 0 字节）在 T40 之前**必红**：旧代码只要白名单非空就
    把整份清单发出去。
    """
    print("\n⑦ `knobs` 段的条件注入（该关没关 / 该开没开 / 逐节点裁剪）")

    # ① 该关没关：冷启动那一轮没有任何计划上下文 → 整块不发，账面如实 0 字节
    text, blocks = _render_blocks_of()
    check(_KNOB_HEAD not in text and _CARD_SWITCH not in text,
          "没有任何计划上下文：开关清单与「只能从上面这份清单挑」那句都不出现")
    check(_knob_bytes(blocks) == 0, f"knobs 子块字节 = 0（实测 {_knob_bytes(blocks)}）")

    # ② 该关没关（第二种写法）：命中的是**没有旋钮**的节点 → 同样整块不发
    text2, blocks2 = _render_blocks_of(prior=[{"plan_id": "p-1", "label": "换一版",
                                               "steps": [{"seq": 1, "node": "asr"}]}])
    check(_KNOB_HEAD not in text2 and _knob_bytes(blocks2) == 0,
          "计划上下文只有 asr（没有可上卡的开关）：照样整块不发")

    # ③ 该开没开：命中 select_BGM → 发它，且**只发它**（render_video 的 subtitle 不许搭车）
    text3, blocks3 = _render_blocks_of(prior=[{"plan_id": "p-1", "label": "换一版", "steps": [
        {"seq": 1, "node": "asr"}, {"seq": 2, "node": "select_BGM"}]}])
    check(_KNOB_HEAD in text3 and "bgm_source" in text3 and "volume" in text3,
          "命中 select_BGM：它的开关进了段")
    check("subtitle" not in text3,
          "没命中的节点（render_video）一个开关都不发——不是「发了但没人用」")
    check(_knob_bytes(blocks3) > 0, f"knobs 子块字节 > 0（实测 {_knob_bytes(blocks3)}）")

    # ④ 该开没开（另一条通道）：命中来自**待续跑未履行的那几步**
    text4, _ = _render_blocks_of(pending={"plan_id": "p-9", "label": "旧卡", "run_id": "r-9",
                                          "unfulfilled": ["render_video"], "asked": "要不要字幕"})
    check("subtitle" in text4 and "bgm_source" not in text4,
          "命中来自 pending 的未履行节点：只发 render_video 的开关")

    # ⑤ 该开没开（第三条通道）：命中来自**用户原话**里的节点名
    text5, _ = _render_blocks_of(feedback="render_video 那步别要了，字幕也别烧")
    check("subtitle" in text5, "用户原话里点名的节点也算命中（feedback 那条通道）")

    # ⑥ 判据的边界（如实钉住，别让它悄悄放宽）：**已产出节点**不算命中
    #
    # 为什么这条要单独钉：把「本会话跑过 plan_motion」也算命中，会让这个会话后面
    # 每一轮（哪怕只是一句「这版多长」）都常发整份开关教程——T40 要治的「参数教程常发」
    # 会从另一个入口原样回来。边界理由写在 `_plan_node_hits` 与 §9-Q103。
    gate = _StubGate(_AXIS_WL, _AXIS_FACTS, None)
    _text6, blocks6 = asyncio.run(planning_section_blocks(
        gate, session_history=[{"run_id": "r-1", "status": "done", "iteration": 2,
                                "is_execution": True, "audit_unfulfilled": []}],
        session_artifacts=["select_BGM", "render_video"]))
    check(_knob_bytes(blocks6) == 0,
          "已产出节点不算命中：只有产物记录的那一轮不发开关教程")

    # ⑦ 同一批输入走磁盘模板也必须一模一样（条件由模板的守卫表达，两份不能漂）
    lib = build_prompt_library(None)
    t_disk, b_disk = asyncio.run(planning_section_blocks(
        _StubGate(_AXIS_WL, _AXIS_FACTS, lib),
        prior=[{"plan_id": "p-1", "label": "换一版", "steps": [{"seq": 2, "node": "select_BGM"}]}]))
    check(_KNOB_HEAD in t_disk and "subtitle" not in t_disk,
          "磁盘模板走同一套判据（段落出现与否由 `{?knobs}` 守卫表达）")
    check(_knob_bytes(b_disk) > 0, "磁盘那份的 knobs 子块也进账")


_GUARDS = (
    # (守卫名, 触发它要用的 kwargs, 块里必须认得出来的一句)
    ("has_pending_plan", {"has_pending_plan": True}, "同会话已有一张待确认的计划卡"),
    ("must_include", {}, "建议**写进这张计划的 steps**"),
    ("prior_cards",
     {"prior": [{"plan_id": "p-1", "label": "换一版", "steps": [{"seq": 1, "node": "asr"}]}]},
     "用户对上一版计划点了「换一版」"),
    ("feedback_quote", {"feedback": "字幕太大"}, "用户对上一版计划不满意"),
    ("has_pending",
     {"pending": {"plan_id": "p-9", "label": "旧卡", "run_id": "r-9",
                  "unfulfilled": ["asr"], "asked": "用哪一段"}},
     "<待续跑的执行轮>"),
    ("session_history_rows",
     {"session_history": [{"run_id": "r-1", "status": "done", "iteration": 1,
                           "message": "剪一版"}],
      "session_artifacts": ["asr"]},
     "<同会话历史>"),
    ("knobs",
     {"prior": [{"plan_id": "p-1", "label": "换一版", "steps": [{"seq": 2, "node": "select_BGM"}]}]},
     _KNOB_HEAD),
    # R13 的第 8 个守卫：计划期探活那一段。它与上面七个**不同源**——值不由本模块算，
    # 而是 `agent.plan()` 现查活库后当参数递进来（见 `plan/capability.py`）。
    # 「递了就有、没递就没有」这一支因此必须单独钉：递漏了整块静默消失，
    # 而那正是这次要治的病（模型不知道本机能力，照提示词字面排一条断腿）。
    ("capability_notes",
     {"capability_notes": "- 出图后端：**未配置**（本机没配）——别排 `imagegen`。"},
     "本机能力实测"),
)


def case_guard_two_ways() -> None:
    """⑧ 八个块守卫**双向**各一遍：该开时开了、该关时真的关。

    此前金样只盖住「该开时开了」（全开那支），「该关没关」没人测——而本篇的守卫
    大多是**条件段**，条件段的默认失败形态恰恰是「条件失效后整段常驻」。
    所以每个守卫都跑两遍：**只**给它那一份输入 → 那句话必须在；什么上下文都不给
    → 那句话必须不在。`must_include` 的开关是白名单里有没有那两张必须显式调用的卡
    （替身按白名单取交集，与真契约同形），所以它那两遍用不同的白名单。
    """
    print("\n⑧ 八个块守卫双向（该关时不许漏、该开时不许缺）")

    for name, kwargs, sentinel in _GUARDS:
        wl = ["asr", "select_BGM", "render_video"] + (["group_clips"] if name == "must_include"
                                                      else [])
        on = asyncio.run(planning_section(_StubGate(wl, _AXIS_FACTS, None), **kwargs))
        check(sentinel in on, f"{{?{name}}}：有值时整块出现")
        off = asyncio.run(planning_section(
            _StubGate(["asr", "select_BGM", "render_video"], _AXIS_FACTS, None)))
        check(sentinel not in off, f"{{?{name}}}：没值时整块消失")

    # 全关那一遍要连**空行**都不留：块被丢掉之后不许在段里留下一片空白
    off_all = asyncio.run(planning_section(
        _StubGate(["asr", "select_BGM", "render_video"], _AXIS_FACTS, None)))
    check("\n\n" not in off_all.strip("\n"),
          "八个守卫全关：段里不留空行（块被丢掉就是丢掉，不是留白）")
    check(off_all.startswith("<planning_round>") and off_all.rstrip().endswith("</planning_round>"),
          "八个守卫全关：外层标签原样收尾（没被守卫切坏）")


def main() -> int:
    print("=== 规划轮提示词迁移：金样逐字 / 块守卫 / 漂移 / 接线 ===")
    case_inline_fallback()
    case_on_disk()
    case_no_leftover_placeholder()
    case_guard_key_contract()
    case_templates_identical()
    case_wired_in_production()
    case_knobs_conditional()
    case_guard_two_ways()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
