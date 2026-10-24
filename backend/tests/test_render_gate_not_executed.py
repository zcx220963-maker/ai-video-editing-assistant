# -*- coding: utf-8 -*-
"""渲染确认门：用户选「要改」时，挂起的 render_video 绝不能冒充已执行。

真机事故（用户日志原文）：
    seq=18 [assistant] calls=['render_video id=call_00_5y9…']   ← 模型要渲染
    seq=19 [tool] tool_call_id=call_00_5y9…
           和音乐长度一致或者比音乐短一点,保证句子不被从中间截断      ← 用户的原话！
    seq=20 [assistant] 渲染已提交，正在跑。                        ← 模型被骗了
    seq=20 [tool] {"render": {"status": "none"},
                   "hint": "这个作用域还没有 render_video 任务"}
    seq=22 [tool] 和音乐长度一致或者比音乐短一点…                   ← 又一次
    模型：「render_video 连续两次返回的都是那句用户补充要求」

根因：`_settle_pending` 的非确认分支把 `note`（用户自由文本）直接当成 ``render_video``
的**工具结果**写回，模型看到「工具返回了这句话」就以为受理了。
实际服务端从未创建渲染任务 → 整条链路断在最后一步。

这条用例钉住：那种 tool 结果必须是**失败**形态（is_tool_error 为真），
且文本要说明「未执行 / 没有产物」，不能让模型误以为成功。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.checkpoint import CheckpointManager  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolError, ToolRegistry, is_tool_error  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class RenderTool(Tool):
    """最简 render_video：只被记录，不真渲染。"""

    def __init__(self) -> None:
        self.calls = 0
        self.args: list[dict] = []      # 每次真执行的入参，供「改了参数重交」那条断言

    @property
    def name(self) -> str:
        return "render_video"

    @property
    def description(self) -> str:
        return "成片渲染"

    @property
    def parameters(self) -> dict:
        # `html` 有默认值只是为了 ⑥ 能传「改了参数的第二次提交」；不标 required，
        # 前面几段用例照旧空参调用。
        return {"type": "object", "properties": {"html": {"type": "string"}},
                "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kw):
        self.calls += 1
        self.args.append(dict(kw))
        return '{"node": "render_video", "render": {"status": "running"}}'


async def main() -> int:
    storage = build_storage("memory")
    await storage.start()
    try:
        tool = RenderTool()
        reg = ToolRegistry()
        reg.register(tool)          # type: ignore[arg-type]
        mgr = CheckpointManager(storage)
        # 模型先要渲染（会被确认门拦下），用户再给自由文本要求
        llm = ScriptedLLM([("tool", "render_video", {}), ("answer", "收到")])
        runner = AgentOnceRun(
            llm, reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6), checkpoint=mgr, storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_render_note")
        await runner.run(sess, "渲染成片", run_id="run-rn")

        cp = await mgr.load("run-rn")
        check(cp is not None and cp.status == "awaiting_approval",
              f"渲染门把 render_video 拦下并挂起（status={getattr(cp, 'status', '?')}）")
        check(tool.calls == 0, f"挂起时渲染**没有**被执行（实际 {tool.calls} 次）")

        # 用户选「要改」并写自由文本（真机那句原话）
        note = "和音乐长度一致或者比音乐短一点,保证句子不被从中间截断"
        await runner.approve(cp, sess, decision="adjust_plan", note=note)
        check(tool.calls == 0, f"给要求之后渲染**仍然没执行**（实际 {tool.calls} 次）")

        cp2 = await mgr.load("run-rn")
        msgs = list(getattr(cp2, "messages", None) or [])
        results = [m for m in msgs
                   if isinstance(m, dict) and m.get("role") == "tool"
                   and m.get("name") == "render_video"]
        check(bool(results), f"补了 render_video 的 tool 结果（{len(results)} 条）")
        if results:
            body = str(results[-1].get("content") or "")
            print(f"    实际写回的文本：{body[:120]}")
            # 关键：必须是「失败/未执行」形态，不能冒充成功回执
            check("Error" in body or "未执行" in body,
                  "写回的是失败形态（不会让模型以为成功）")
            check("没有" in body and ("产物" in body or "运行" in body),
                  "明确说明「没有运行 / 没有产物」")
            check(note in body, "带上了用户的要求原文（模型据此重做）")
            # 反向：旧行为是纯用户文本、不带任何失败标记
            check(not body.strip().startswith("和音乐"),
                  "不再是「纯用户文本」那种冒充回执的写法")

        print()
        # 关键回归：**同一道门题只能问一次**。
        #
        # 真机事故：`decision_is_confirm` 只认 `confirm_render`，用户选「保内容完整」
        # 不是它 → 门认为"没确认" → 模型重渲 → 再拦 → 再问同一道题，问了几十遍。
        # 用户的原则：「答了就是答了」。所以第二次渲染必须**放行**（不再拦、不再问）。
        print("=== ② 同一道门题不能问第二次 ===")
        # 模拟模型「没改参数就重渲」（真机就是这样）
        llm2 = ScriptedLLM([("tool", "render_video", {}),
                            ("tool", "render_video", {}),
                            ("answer", "完成")])
        storage2 = build_storage("memory")
        await storage2.start()
        try:
            tool2 = RenderTool()
            reg2 = ToolRegistry()
            reg2.register(tool2)      # type: ignore[arg-type]
            mgr2 = CheckpointManager(storage2)
            runner2 = AgentOnceRun(
                llm2, reg2, ContextBuilder("BASE"), hooks=CompositeHook([]),
                config=AgentConfig(max_iterations=6), checkpoint=mgr2,
                storage=storage2,
            )
            sess2 = Session(user_id="u", conversation_id="c_gate_once")
            await runner2.run(sess2, "渲染成片", run_id="run-go")
            cp_a = await mgr2.load("run-go")
            check(cp_a is not None and cp_a.status == "awaiting_approval",
                  "第一次渲染被拦下（该问就问）")
            check(tool2.calls == 0, f"第一次没渲染（实际 {tool2.calls}）")

            # 用户选了「保完整」（非 confirm_render）
            await runner2.approve(cp_a, sess2, decision="keep_full_sentence",
                                 note="按音乐时长内,不要截断句子即可")
            # 关键断言：第二次渲染**不该再被拦**
            cp_b = await mgr2.load("run-go")
            status_b = getattr(cp_b, "status", "?")
            check(tool2.calls >= 1,
                  f"第二次渲染**放行了**（渲染真的执行了 {tool2.calls} 次）")
            check(status_b != "awaiting_approval",
                  f"没有再次停在等待确认上（status={status_b}）——"
                  f"这就是「问几十遍」的反面")
        finally:
            await storage2.close()

        print()
        print("=== ③ 这个标记必须**落盘**（重启/换实例后仍记得）===")
        # 上一版把标记放内存，真机被两件事打穿：进程重启、续跑换代——
        # 于是同一道题又问了两遍。这里直接验它进了可恢复状态。
        storage3 = build_storage("memory")
        await storage3.start()
        try:
            tool3 = RenderTool()
            reg3 = ToolRegistry()
            reg3.register(tool3)      # type: ignore[arg-type]
            mgr3 = CheckpointManager(storage3)
            llm3 = ScriptedLLM([("tool", "render_video", {}), ("answer", "先停")])
            runner3 = AgentOnceRun(
                llm3, reg3, ContextBuilder("BASE"), hooks=CompositeHook([]),
                config=AgentConfig(max_iterations=4), checkpoint=mgr3,
                storage=storage3,
            )
            sess3 = Session(user_id="u", conversation_id="c_persist")
            await runner3.run(sess3, "渲染", run_id="run-persist")
            cp3 = await mgr3.load("run-persist")
            # RunState 落在 cp.plan["state"]（见 RunState.persist），不是 cp.state
            def _flag(cp) -> object:
                plan = getattr(cp, "plan", None) or {}
                return (plan.get("state") or {}).get("render_gate_asked")

            got = _flag(cp3)
            check(got == ["render_video"],
                  f"挂起时问过哪个通道已落进 cp.plan['state']（实际 {got!r}）")
            # 关键：从盘上重新读一遍（模拟进程重启后恢复），标记仍在
            cp3b = await mgr3.load("run-persist")
            got2 = _flag(cp3b)
            check(got2 == ["render_video"],
                  f"重新载入后仍然记得（实际 {got2!r}）——重启不会再问一遍")

            # 换一个全新的运行器实例（模拟新进程），用同一份 checkpoint 续跑
            tool4 = RenderTool()
            reg4 = ToolRegistry()
            reg4.register(tool4)      # type: ignore[arg-type]
            runner4 = AgentOnceRun(
                ScriptedLLM([("tool", "render_video", {}), ("answer", "完成")]),
                reg4, ContextBuilder("BASE"), hooks=CompositeHook([]),
                config=AgentConfig(max_iterations=4), checkpoint=mgr3,
                storage=storage3,
            )
            await runner4.approve(cp3b, sess3, decision="keep_full_sentence",
                                  note="音乐时长内")
            cp4 = await mgr3.load("run-persist")
            check(tool4.calls >= 1,
                  f"**新实例**续跑后渲染放行了（执行 {tool4.calls} 次）——"
                  f"换进程也不会再问一遍")
            check(getattr(cp4, "status", "?") != "awaiting_approval",
                  f"没有再次停在等待确认（status={getattr(cp4, 'status', '?')}）")
        finally:
            await storage3.close()

        print()
        print("=== ④ 收尾（retain_residue）不能把这一位清掉 ===")
        # 真机事故（用户原话「为什么老是问我这个问题，我回答无数遍了」）：
        # run 撞迭代上限 → 收尾时 retain_residue 只保留 approved_plan、把这位置清掉
        # → 用户点「继续」→ 门认为"没问过" → 又把同一道题问一遍，如此往复。
        from agent_framework.run_state import RunState  # noqa: PLC0415

        st = RunState()
        st.note_render_gate_asked("render_video")
        cp_fake = type("CP", (), {"plan": {"state": {"render_gate_asked": ["render_video"],
                                                    "calls_attempted": ["x"]}}})()
        RunState.retain_residue(cp_fake, st)
        kept = (cp_fake.plan.get("state") or {}).get("render_gate_asked")
        check(kept == ["render_video"],
              f"收尾后问过的通道清单仍在（实际 {kept!r}）——「继续」不会再问一遍")
        check("calls_attempted" not in (cp_fake.plan.get("state") or {}),
              "中间过程（调用清单）仍然被精简掉（收尾该瘦身的部分没变胖）")

        # 「直接出片」同理必须活过收尾：它记的是用户这一轮已经决定不打断，
        # 续跑那半截没人再带这个参数，收尾清掉就等于凭空把确认卡塞回去。
        st_gs = RunState(go_straight=True)
        cp_gs = type("CP", (), {"plan": {"state": {}}})()
        RunState.retain_residue(cp_gs, st_gs)
        check((cp_gs.plan.get("state") or {}).get("go_straight") is True,
              "收尾后「直接出片」仍在——续跑不会再弹确认卡")

        # 没问过时不该凭空写进去
        st2 = RunState()
        cp_fake2 = type("CP", (), {"plan": {"state": {"render_gate_asked": []}}})()
        RunState.retain_residue(cp_fake2, st2)
        check((cp_fake2.plan.get("state") or {}).get("render_gate_asked") is None,
              "没问过时不留这一位（state 该空就空）")

        print()
        print("=== ⑤ 答「要改」不等于答准：模型重做之后**必须重新过门** ===")
        # 真机事故（2026-10-16，run 9272c7f59501，20 镜那条）：
        # 用户在确认卡上答的是「要改」+ 新要求（年份不对、风格不统一），不是批准。
        # 可这一位是**发问那一刻**就记的，于是这个通道被记成"问过了"，
        # `ungated_render_nodes` 从此跳过它 —— 模型改完之后第 109 轮再出片，
        # **一道门都没过，直接烧像素**。用户只能问「怎么这轮没有先预览确认」。
        #
        # 更要命的是 `decision_text` 自己写着「系统会再弹一次计划确认卡给用户过目」——
        # 那句话在修复前是**假的**。这条用例就是钉住这个承诺。
        storage5 = build_storage("memory")
        await storage5.start()
        try:
            tool5 = RenderTool()
            reg5 = ToolRegistry()
            reg5.register(tool5)      # type: ignore[arg-type]
            mgr5 = CheckpointManager(storage5)
            # 脚本：要渲染 →（被拦，用户答"要改"）→ 模型重做后再要渲染 → 收尾
            llm5 = ScriptedLLM([("tool", "render_video", {}),
                                ("tool", "render_video", {}),
                                ("answer", "完成")])
            runner5 = AgentOnceRun(
                llm5, reg5, ContextBuilder("BASE"), hooks=CompositeHook([]),
                config=AgentConfig(max_iterations=6), checkpoint=mgr5,
                storage=storage5,
            )
            sess5 = Session(user_id="u", conversation_id="c_regate")
            await runner5.run(sess5, "渲染成片", run_id="run-regate")
            cp5a = await mgr5.load("run-regate")
            check(cp5a is not None and cp5a.status == "awaiting_approval",
                  "第一次出片被拦下（该问就问）")

            got5 = (cp5a.plan.get("state") or {}).get("render_gate_asked")
            check(got5 == ["render_video"],
                  f"发问时记下通道（实际 {got5!r}）——这条性质不许动："
                  f"它撑着「渲染失败后重交不再弹第二张卡」")

            # 用户答「要改」并写自由文本（真机那句原话的形态）。
            #
            # decision 传的就是**用户打的那句话本身**——前端 qSubmit 在用户自己打字时
            # 把原文当 decision 发（`decideApproval(c.approval, first.answer, …)`），
            # 所以它必然不在卡片的选项 key 里。这里必须照真形传，不能图省事传 ADJUST_OPTION。
            await runner5.approve(
                cp5a, sess5,
                decision="薄肌理论是26年的啊,而且画面风格不够统一,我要偏杂志风",
                note="薄肌理论是26年的啊,而且画面风格不够统一,我要偏杂志风")
            check(tool5.calls == 0, f"答「要改」时渲染**没有**执行（实际 {tool5.calls}）")

            # ★ 核心断言：这一答不是批准 → 这道门作废，模型第二次出片必须**再被拦**
            cp5b = await mgr5.load("run-regate")
            status5b = getattr(cp5b, "status", "?")
            check(status5b == "awaiting_approval",
                  f"模型改完再出片**重新过了门**（status={status5b}）——"
                  f"这就是「答要改 ≠ 答准」")
            check(tool5.calls == 0,
                  f"重新过门时渲染仍然没被执行（实际 {tool5.calls}）——"
                  f"修复前这里会直接烧像素")
            got5b = (cp5b.plan.get("state") or {}).get("render_gate_asked")
            check(got5b == ["render_video"],
                  f"重新过门后又记上了（实际 {got5b!r}）")

            # 反向：换成**批准**，同一通道不该被问第二次（老性质不许被这次修复打穿）
            #
            # 只在**确实还挂着**的时候才批准：回归打穿时上面那条断言会失败、
            # 这条 run 已经跑完，再 approve 一个没挂起的 checkpoint 会直接抛异常，
            # 整个用例变成一段 traceback 而不是几条 ✗ ——那样看不出坏了哪一条。
            if status5b == "awaiting_approval":
                await runner5.approve(cp5b, sess5, decision="confirm_render", note="")
                cp5c = await mgr5.load("run-regate")
                check(tool5.calls >= 1,
                      f"批准之后渲染真的执行了（{tool5.calls} 次）")
                check(getattr(cp5c, "status", "?") != "awaiting_approval",
                      f"批准之后不再停在等待确认（status={getattr(cp5c, 'status', '?')}）")
            else:
                check(False, "这条 run 没在等确认，批准那半截跑不到——"
                             "说明上面「重新过门」已经打穿")
        finally:
            await storage5.close()

        print()
        print("=== ⑥ 出了废片要能改：同参数重交拦下，改了参数必须放行 ===")
        # 真机事故（2026-10-16，run b7df827ebed4「薄肌理论」）：第一次 render_web **成功了**
        # ——但是个 20 秒全黑的废片。模型看出来了，改好 HTML 重交，被回
        # 「本次编排已经确认并提交渲染，无需再次提交」；再改名重交，还是同一句。
        # `_last_render_failed` 那道放行口救不了它：上一次**返回的是成功**。
        # 于是模型改好了代码却一个字节都送不进去，整条 run 只能停在那儿向用户解释。
        #
        # 判据：认的是 **(这条 run, 这个通道, 这一次提交的参数)**。
        storage6 = build_storage("memory")
        await storage6.start()
        try:
            tool6 = RenderTool()
            reg6 = ToolRegistry()
            reg6.register(tool6)      # type: ignore[arg-type]
            mgr6 = CheckpointManager(storage6)
            # 脚本：提 A（用户批准，真跑）→ 再提 A（逐字相同，该压）→ 提 B（改了，该跑）
            llm6 = ScriptedLLM([
                ("tool", "render_video", {"html": "<html>A</html>"}),
                ("tool", "render_video", {"html": "<html>A</html>"}),
                ("tool", "render_video", {"html": "<html>B 改好的</html>"}),
                ("answer", "改完重出"),
            ])
            runner6 = AgentOnceRun(
                llm6, reg6, ContextBuilder("BASE"), hooks=CompositeHook([]),
                config=AgentConfig(max_iterations=6), checkpoint=mgr6,
                storage=storage6,
            )
            sess6 = Session(user_id="u", conversation_id="c_resubmit")
            await runner6.run(sess6, "出片", run_id="run-resubmit")
            cp6 = await mgr6.load("run-resubmit")
            check(getattr(cp6, "status", "?") == "awaiting_approval",
                  "第一次出片被拦下（确认门照旧）")
            await runner6.approve(cp6, sess6, decision="confirm_render", note="")
            # 脚本里提了三次：A、A（逐字相同）、B。跑完应当是 A 一次 + B 一次 = 2，
            # **不是 3**——中间那次逐字相同的重交必须被压住（白烧像素的那道守卫）。
            a_runs = [a for a in tool6.args if a.get("html") == "<html>A</html>"]
            check(tool6.calls == 2 and len(a_runs) == 1,
                  f"逐字相同的重交被压住（A 只跑了 {len(a_runs)} 次，总执行 "
                  f"{tool6.calls} 次）——「误判一次白烧几分钟」那道守卫没被打穿")

            cp6b = await mgr6.load("run-resubmit")
            msgs6 = list(getattr(cp6b, "messages", None) or [])
            suppressed = [m for m in msgs6
                          if isinstance(m, dict) and m.get("role") == "tool"
                          and "无需再次提交" in str(m.get("content") or "")]
            check(bool(suppressed),
                  "逐字相同的重交确实被回「无需再次提交」（老性质没被打穿）")

            # ★ 核心：改了参数的第三次**必须真执行**——「用户批准的是这条路出片，
            #   不是只准出一次」。修复前这里也回「无需再次提交」，模型再无出路。
            check(tool6.calls >= 2,
                  f"★ 改了参数重交**真的执行了**（实际共 {tool6.calls} 次）")
            check(any("B 改好的" in str(a.get("html") or "") for a in tool6.args),
                  f"★ 执行的是**改好的那一版**（实际入参：{[a.get('html') for a in tool6.args]}）")
            check(getattr(await mgr6.load("run-resubmit"), "status", "?") != "awaiting_approval",
                  "改好的那一版没有又停下来问一遍（用户已经批准过这条路）")
        finally:
            await storage6.close()

        print()
        print("全部通过" if not _fails else f"有 {_fails} 项未通过")
        print(f"用例 {_checks} 条")
        return 0 if not _fails else 1
    finally:
        await storage.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
