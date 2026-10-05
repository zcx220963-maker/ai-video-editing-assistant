"""计划门（块 B）：规划轮的工具、服务端四重校验、执行帧编译与事后对账。

对应交接文档《剪辑 Agent「Plan-and-Execute 确认门 + 中文显示单源」》§3/§5/§7。
这一层全是**纯代码**，一次 LLM 都不叫（各职责分别住在包里的哪个模块见 ``gate`` 的模块说明）：

* **规划轮物理过滤**：剪辑执行节点根本不在那一轮的注册表里——模型想越权也无工具可调
  （见 ``PlanVocabulary.planning_registry``），比提示词里写「请勿执行」强。
* **枚举是承诺，自定义是诉求**：``param_options`` 的每个值都要能反查到枚举源
  （节点 input_schema 的 enum / 布尔开关 / 带上下界的数值 / 外部注入的曲库真实标签），
  反查不到就不许上卡；自由文本永不进节点参数，只进 ``<user_custom_requests>`` 段。
* **软约束**：批准的计划是强提示，不是硬调度。``reconcile`` 只在事后算偏差
  （计划外步骤 / 声明了没调），不拦截任何一次调用。

中文名不在这层拼：这层的产物里留机器名，出门时由 ``catalog.ToolCatalog`` 换轨
（块 A 的出口替换器对 ``node``/``skills_hint`` 这类键走 ``*_display`` 旁路）。

计划卡「当轮落库」的 contextvar 通道（``record_plan_card`` / ``drain_plan_cards`` /
``reset_plan_cards``）住在 ``plan_replay``，这里只是转出：``MessagesRepo`` 也要用它们，
而存储层不该反向 import 本包。
"""

from __future__ import annotations

from ..plan_replay import (drain_plan_cards, drain_plan_round, record_plan_card,
                           reset_plan_cards)
from .compile import PlanCompiler
from .gate import PlanGate
from .prompt import planning_section, render_injections
from .reconcile import (NON_STEP_TOOLS, PlanCardHook, PlanReconcileHook,
                        reconcile)
from .resume import pending_continuation
from .skills import preload_skills
from .support import (CUSTOM_MAX_CHARS, CUSTOM_MAX_ITEMS, PLAN_MAX_CANDIDATES,
                      PlanIssues, neutralize)
from .tools import ConfirmPlanTool, SubmitPlanTool
from .validate import PlanValidator
from .vocab import PlanVocabulary
from .wording import claims_plan_card, claims_step_executed

__all__ = [
    "PLAN_MAX_CANDIDATES", "CUSTOM_MAX_ITEMS", "CUSTOM_MAX_CHARS",
    "PlanGate", "PlanVocabulary", "PlanValidator", "PlanCompiler",
    "SubmitPlanTool", "ConfirmPlanTool", "PlanIssues", "neutralize",
    "render_injections", "reconcile", "preload_skills", "planning_section",
    "pending_continuation", "claims_plan_card", "claims_step_executed",
    "PlanCardHook", "PlanReconcileHook", "NON_STEP_TOOLS",
    "record_plan_card", "drain_plan_cards", "drain_plan_round", "reset_plan_cards",
]
