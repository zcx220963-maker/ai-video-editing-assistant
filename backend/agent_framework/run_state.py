"""一次 run 里「挂起之后仍然要成立」的状态——显式字段，随 checkpoint 落盘。

以前这些事实散在 ``ctx.extras`` 的约定键上（``approved_plan`` / ``tool_calls_seen`` /
``plan_candidates`` / ``qa_parts`` …）。``extras`` 是纯内存字典、不进指针行，于是任何一次
挂起→续跑（渲染确认门、ask_user 弹窗、兜底方案选择）都会把它们清零。真机表现：

* 执行轮在渲染门前停过一次，续跑那半截再也没有对账帧——``approved_plan`` 没了，
  对账钩子按「普通轮」处理，``plan reconciliation`` 从此不再发出；
* 计划步骤在挂起前就真跑过了，续跑后 ``calls_attempted`` 是空的，于是那句
  「本轮一次步骤都没调用」的硬保证对着用户打假——和已经修掉的 ``asked_this_run``
  同一个病灶（见 README §3.16 第 ② 条）；
* 规划轮弹窗提问后续跑，``plan_candidates`` 清零，卡明明在界面上却被告知「没有出卡」；
* 挂起那半截攒下的 ``qa_parts``（思考、工具调用气泡、计划卡）整个丢掉，
  历史里那条 assistant 行只剩续跑之后的半截。

所以这里把它收敛成一个有字段清单、有序列化、随 ``cp.plan["state"]`` 落盘的对象。
仍然留在 ``extras`` 的是**本轮调用私有**的注入（``run_id`` / ``checkpoint`` /
``checkpoint_manager`` / ``handover`` / ``confirm_plan_requested`` / 两处去重计数）：
它们要么每次 ``_drive`` 由调用方重新给，要么在同一轮里就被消费掉，落盘反而失真。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# 指针行里的字段名：cp.plan 已经是「本 run 的计划侧产物」这一列（candidates/audit 住在
# 这里），state 作为它的兄弟键，不额外加列，也不需要迁移。
STATE_FIELD = "state"


@dataclass
class RunState:
    """本 run 跨挂起仍然成立的事实。字段即清单——新增状态只能加在这里。"""

    # 执行轮：用户在计划卡上确认过的那一份承诺（普通轮/规划轮恒为 None）
    approved_plan: dict[str, Any] | None = None
    # 真发生过的调用名，含失败的那几次——「执行轮一次步骤都没调用」判据用它算。
    # 那道硬保证不该因为「这一轮是从挂起点续上来的」而打假。
    calls_attempted: list[str] = field(default_factory=list)
    # 其中**真的跑到了注册表**的那部分（UnknownToolError 的调用从未发生，不算跑过）：
    # 计划对账的输入。
    calls_executed: list[str] = field(default_factory=list)
    # 规划轮：submit_plan 通过四重校验的候选、伴随警告、卡是否已回投过。
    # 候选与警告**不在 state 里重复存一份**——它们已由 submit_plan 落在
    # ``cp.plan["candidates"]`` / ``cp.plan["warnings"]``（确认接口与 /plans/{id} 重放的
    # 数据源），restore() 从那里读回来；这里只持久化「卡推过没有」这一位。
    plan_candidates: list[dict[str, Any]] = field(default_factory=list)
    plan_warnings: list[str] = field(default_factory=list)
    plan_card_pushed: bool = False
    # 渲染确认门：这条 run 里**哪些出片通道**已经问过那道门题（节点名清单）。
    #
    # 为什么必须持久化（放内存不够）：真机实测同一道题被问了几十遍——
    # ``decision_is_confirm`` 只认 ``confirm_render`` 一个 key，用户在弹窗里答了
    # 「保内容完整 / 音乐时长内」这类选项，门判定"你没确认" → 模型重渲 → 再拦 →
    # 再问同一道题，无限循环。放内存的版本还会被两件事打穿：进程重启（内存清零）、
    # 续跑换 run 身份。落进 state 就跟着指针行走，重启与续跑都记得「这道题问过了」。
    #
    # 为什么按**通道**而不是整条 run 一个布尔：用户确认了剪素材那条路
    # （``render_video``），不等于批准了零素材那条路（``render_motion_video``）——
    # 那是另一笔不可逆的算力，一位布尔会把第二道门直接放行。
    render_gate_asked: list[str] = field(default_factory=list)
    # 对账：上一次推给前端的偏差（据此去重），以及终答时算出的完整结论
    audit_pushed: dict[str, Any] | None = None
    plan_audit: dict[str, Any] | None = None
    # 本轮 think / tool call 片段（QA .jsonl 与 assistant 行的 qa 字段用）。
    # 挂起那半截也要进最后那条 assistant 行，否则历史里只剩续跑之后的片段。
    qa_parts: list[dict[str, Any]] = field(default_factory=list)

    # ---- 序列化 ----

    def to_json(self) -> dict[str, Any]:
        return {
            "approved_plan": self.approved_plan,
            "calls_attempted": list(self.calls_attempted),
            "calls_executed": list(self.calls_executed),
            "plan_card_pushed": bool(self.plan_card_pushed),
            "render_gate_asked": list(self.render_gate_asked),
            "audit_pushed": (dict(self.audit_pushed)
                             if isinstance(self.audit_pushed, Mapping) else None),
            "plan_audit": (dict(self.plan_audit)
                           if isinstance(self.plan_audit, Mapping) else None),
            "qa_parts": [dict(p) for p in self.qa_parts],
        }

    @classmethod
    def from_json(cls, raw: Any) -> "RunState":
        d = raw if isinstance(raw, Mapping) else {}
        plan = d.get("approved_plan")
        return cls(
            approved_plan=dict(plan) if isinstance(plan, Mapping) else None,
            calls_attempted=[str(x) for x in (d.get("calls_attempted") or [])],
            calls_executed=[str(x) for x in (d.get("calls_executed") or [])],
            plan_card_pushed=bool(d.get("plan_card_pushed")),
            render_gate_asked=[str(x) for x in (d.get("render_gate_asked") or [])
                               if isinstance(x, str)],
            audit_pushed=(dict(d["audit_pushed"])
                          if isinstance(d.get("audit_pushed"), Mapping) else None),
            plan_audit=(dict(d["plan_audit"])
                        if isinstance(d.get("plan_audit"), Mapping) else None),
            qa_parts=[dict(p) for p in (d.get("qa_parts") or []) if isinstance(p, Mapping)],
        )

    # ---- 与指针行的往来 ----

    def persist(self, cp: Any) -> None:
        """把当前状态写进 ``cp.plan["state"]``：下一次指针行落盘自然带上。"""
        if cp is None:
            return
        plan = getattr(cp, "plan", None)
        if not isinstance(plan, dict):
            return
        plan[STATE_FIELD] = self.to_json()

    @classmethod
    def restore(cls, cp: Any) -> "RunState":
        """从指针行取回状态；没有（老 run / 普通轮）就是全新的。"""
        if cp is None:
            return cls()
        plan = getattr(cp, "plan", None) or {}
        state = cls.from_json(plan.get(STATE_FIELD))
        # 候选与警告另有归处（submit_plan 落的这两列，确认接口按它们取计划本体）：
        # 挂起→续跑之后规划轮的「有没有出卡」判据要看得见它们，否则会对界面上
        # 确实在着的卡打一句「没有出卡」。
        if not state.plan_candidates:
            state.plan_candidates = [dict(p) for p in (plan.get("candidates") or [])
                                     if isinstance(p, Mapping)]
            state.plan_warnings = [str(x) for x in (plan.get("warnings") or [])]
        return state

    @staticmethod
    def retain_residue(cp: Any, state: "RunState") -> None:
        """收尾时指针行只留「这份 run 认过的承诺」。

        完成的 run 不会被恢复，调用清单与 transcript 不必再存（transcript 已经在
        assistant 行的 ``qa.parts`` 里，对账结论另有 ``cp.plan["audit"]``）。

        要留的只有两类：
          · ``approved_plan`` —— 「编译后的那一份计划」在库里唯一的落点。
            从这条 run 的某个一致点分叉时，子 run 得继承同一个承诺，而不是退回
            「没有批准计划」——那道执行轮的硬保证和对账都会因此失效。
          · ``render_gate_asked`` —— **「渲染确认门已经问过哪些通道」这份清单必须活过收尾**。
            真机事故（用户原话「为什么老是问我这个问题，我回答无数遍了」）：
            run 撞迭代上限 → 收尾把这份清单清掉 → 用户点「继续」→ 门认为"没问过"
            → 又把同一道题问一遍，如此往复。它记的是"这条 run 已经问过用户"，
            与 approved_plan 同属「已认过的事实」，不是可丢的中间过程。
        """
        plan = getattr(cp, "plan", None) if cp is not None else None
        if not isinstance(plan, dict):
            return
        keep: dict[str, Any] = {}
        if isinstance(state.approved_plan, Mapping):
            keep["approved_plan"] = dict(state.approved_plan)
        if state.render_gate_asked:
            keep["render_gate_asked"] = list(state.render_gate_asked)
        if keep:
            plan[STATE_FIELD] = keep
        else:
            plan.pop(STATE_FIELD, None)

    # ---- 累积 ----

    def note_attempted(self, tool_name: str) -> None:
        self.calls_attempted.append(tool_name)

    def note_executed(self, tool_name: str) -> None:
        self.calls_executed.append(tool_name)

    def note_render_gate_asked(self, *nodes: str) -> None:
        """记住「这几道出片门的题已经问过了」——同一通道同一条 run 只问一次。"""
        for n in nodes:
            if n and n not in self.render_gate_asked:
                self.render_gate_asked.append(n)


def tool_names_in(messages: Any) -> list[str]:
    """消息列表里已经发生过的工具调用名（按 ``role == "tool"`` 的回执算）。

    分叉出的子 run 用它重建调用清单：分叉点之前的步骤确实跑过（回执还在上下文里），
    之后的已被作废——照父 run 的清单继续对账会把「这次要重做的那几步」当成已履行。
    """
    out: list[str] = []
    for m in messages or []:
        get = m.get if isinstance(m, Mapping) else (
            lambda k, _m=m: getattr(_m, k, None))
        if get("role") != "tool":
            continue
        name = get("name")
        if name:
            out.append(str(name))
    return out
