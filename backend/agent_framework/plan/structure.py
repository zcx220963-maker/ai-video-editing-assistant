"""整卡层面的结构检查：拓扑与显式依赖 / 跳过规则 / 重复步骤 / 重复开关 / 文案卫生。

与 ``validate`` 那份的分工：那边管「一步一步归一化并判它合不合规」，
这一份拿**已经成形的一整张卡**（或原始步骤的拓扑视图）查跨步骤的事实——
顺序与依赖是否自相矛盾、同一个节点/同一组开关是否出现两次、文案里是否引用了词表
根本不存在的名字。两类判据都只问 ``PlanVocabulary`` 要事实，自己不含任何节点名。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .support import PlanIssues, clean
from .vocab import PlanVocabulary

# 词表外别名的形状：snake_case 标识符（至少一个下划线）。
# 展示字段里出现**英文原名**不打回——块 A 的出口替换器会换轨成中文；
# 只有引用了词表里根本没有的名字才属于正确性问题（臆造），替换器无能为力。
_ALIAS = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


class StructureChecks:
    """跨步骤的结构判据：只观测出 issues，不改调用、也不短路别的检查。"""

    def __init__(self, vocab: PlanVocabulary) -> None:
        self.vocab = vocab

    # ---- ② 拓扑相容 / 跳过规则 / 文本卫生 ----

    def topology(self, tag: str, steps: list[dict[str, Any]],
                 issues: PlanIssues) -> None:
        """步骤序与 dag_contract.requires 拓扑相容：前置排在后面就是错。

        前置**没上卡**是允许的（拦截器会补齐），界面上画成虚边——
        「乱序容忍但逼依赖显式化」里被禁的是顺序颠倒，不是省略。
        """
        seq_of = {s["node"]: s["seq"] for s in steps}
        for step in steps:
            for dep in step["requires"]:
                if dep in seq_of and seq_of[dep] > step["seq"]:
                    issues.error(f"计划 {tag} 第 {step['seq']} 步（{step['node']}）"
                                 f"排在它的前置「{dep}」之前，与 DAG 契约冲突。")

    # 2026-10-16 删掉了 `explicit_deps`（「标了 require_explicit_call 的前置必须
    # 写进 steps，否则打回」）。它守的是执行期那个报错，而那个报错本身撤了
    # ——架构总纲里只有「拦截器递归补齐」，没有「某些节点必须显式调用否则报错」
    # 这一条，它是全仓唯一一处超出文档的硬约束（见 `orchestration.Interceptor`
    # 的 `_ensure_deps` / `_annotate`）。判据撤了，这条校验就没有守的对象：
    # 留着它只会继续把「没把某几步写进卡」当成错误，而它已经只是一种写法。
    # 那几步仍然**值得**写进卡（写进去才由你定参数，不写就按默认值补）——
    # 这句话归规划轮提示段（`prompt._explicit_nodes`）与工具说明（`mcp._desc_tail`），
    # 它们给的是建议，不再是一条会把卡打回的规则。

    def skip_rules(self, tag: str, steps: list[dict[str, Any]],
                   issues: PlanIssues) -> None:
        """skippable 只在没有下游依赖踩着时成立；否则降级为不可跳并给角标原因。"""
        present = {s["node"] for s in steps}
        for step in steps:
            if not step["skippable"]:
                continue
            blocked = (self.vocab.contract.downstream(step["node"]) - {step["node"]}) & present
            if blocked:
                step["skippable"] = False
                step["skip_reason"] = f"下游「{'、'.join(sorted(blocked))}」依赖它的产物"
                issues.warn(f"计划 {tag} 第 {step['seq']} 步（{step['node']}）本可跳过，"
                            f"但{step['skip_reason']}，已置为不可跳。")

    def duplicates(self, tag: str, steps: list[dict[str, Any]],
                   issues: PlanIssues) -> None:
        seen: set[str] = set()
        for step in steps:
            if step["node"] in seen:
                issues.error(f"计划 {tag} 里节点「{step['node']}」出现了多次。")
            seen.add(step["node"])

    def dup_param_options(self, tag: str, steps: list[dict[str, Any]],
                          issues: PlanIssues) -> None:
        """跨步骤查重：同一参数 + 同一组选项在多个步骤各出现一次 = 重复弹窗。

        语速/音色/画幅等整片属性只在第一步设一次，后续步骤自动继承；
        LLM 对两步都列了相同的 param_options 时在这里硬拦。
        """
        seen: dict[tuple[str, frozenset[str]], int] = {}
        for step in steps:
            for opt in step.get("param_options") or []:
                vals = frozenset(v.get("value") for v in opt.get("options") or [])
                sig = (opt.get("key") or "", vals)
                if sig in seen:
                    issues.error(
                        f"计划 {tag} 第 {step['seq']} 步的参数「{opt['key']}」"
                        f"与第 {seen[sig]} 步的选项完全相同——"
                        f"整片属性只在第一次出现的步骤设一次，后续步骤继承即可，"
                        f"不要重复列 param_options。")
                else:
                    seen[sig] = step["seq"]

    def hygiene(self, tag: str, texts: Sequence[Any], issues: PlanIssues) -> None:
        """只拦一种文本错误：词表里没有的别名/臆造工具名（英文原名由出口替换器处理）。

        词表要含真实参数键：``material_id`` 这类名字是入参不是节点，模型在 why/expectation
        里提它是正常表达，拦下来只会逼它换个说法重试一轮。

        同理要含**节点产出字段名**（``asr_segments``/``clip_captions``/``groups``…）：
        模型在为什么/预期里引用上游产物字段是最自然的写法，真机实测它连写三次
        「引用不存在的名字「asr_segments」」，卡连着两轮出不来。产出字段不是臆造的工具名，
        该放行。
        """
        vocab = (self.vocab.vocabulary() | self.vocab.param_keys() | self.vocab.enum_values()
                 | self.vocab.output_keys())
        bad: list[str] = []
        for text in texts:
            for token in _ALIAS.findall(clean(text or "")):
                if token in vocab or self.vocab.resolve_tool(token) is not None:
                    continue
                if token not in bad:        # 同一个名字在 why/label/expectation 里各写一遍
                    bad.append(token)       # 只算一处：判据是「这个名字不在词表里」
        # 按**整份计划**合批上报，一条判据一条意见：同一个臆造名字写进了三步的 why 与
        # expectation 就是 6 条一模一样的话，全用来挤打回清单的窗口，而模型删掉那个
        # 名字一次就全好了。名字一个不少地列出来，它才知道要改几处。
        if not bad:
            return
        if len(bad) == 1:
            issues.error(f"计划 {tag} 的文案引用了不存在的名字「{bad[0]}」"
                         f"（词表里没有，界面换不成中文）。")
            return
        names = "".join(f"「{t}」" for t in bad)
        issues.error(f"计划 {tag} 的文案引用了 {len(bad)} 个不存在的名字{names}"
                     f"（词表里没有，界面换不成中文）。")
