"""计划门的装配层：把事实层、校验层、帧编译拼成对外的 ``PlanGate``。

调用方（``AgentOnceRun`` 的规划轮与执行轮、``run_server`` 的装配、确认接口）只认这一个对象。
包里的每一层各司其职，谁也不反向依赖装配层：

* ``vocab.PlanVocabulary`` —— 节点与参数的事实（白名单、词表、枚举源、规划轮注册表）
* ``validate.PlanValidator`` —— ①~④ 四重校验（node / 拓扑与依赖 / skills_hint / param_options）
* ``compile.PlanCompiler`` —— ⑤ 确认帧编译（选中版本 + 参数终值 + 跳过 + 自定义诉求）
* ``tools`` —— 模型侧的 ``submit_plan`` / ``confirm_plan``
* ``prompt`` —— 执行轮的两段分离注入与同会话历史段；规划轮 system 段的**措辞**在
  ``prompts/planning_round.md``，这里只备它要插的数据
* ``reconcile`` —— 事后对账与两个钩子
* ``wording`` —— 假称判据的措辞正则
* ``skills`` —— 技能预注入
* ``resume`` —— 「确认过但没跑完」的那条执行轮的查账入口
* ``support`` —— 上限常量与纯文本工具（谁都用得到，故不依赖包内任何其他模块）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

from ..catalog import ToolCatalog
from ..editing_contract import ContractSlot, EditingContract
from ..tool import Tool, ToolRegistry
from .compile import PlanCompiler
from .support import PlanIssues
from .validate import PlanValidator
from .vocab import PlanVocabulary

if TYPE_CHECKING:
    from ..prompts import PromptLibrary


class PlanGate:
    """计划的服务端四重校验 + execute 帧校验（纯代码，无 LLM）。

    本身不持任何判据，只做装配与转发。依赖全部**现取**（注册表与契约要到启动后才填齐，
    构造时只握引用）：``contract`` 给依赖边与下游集，``registry`` 给节点参数枚举源，
    ``skills`` 给技能可用性，``extra_options`` 给不在 schema 里的真实枚举
    （生产里是 BGM 曲库的实际标签），``catalog`` 给词表以判「臆造别名」，
    ``prompt_library`` 给规划轮文案的磁盘版本（``prompt.planning_section`` 是它唯一的消费者；
    没接就用内联回落，提示词目录不在也不该让规划轮拼不出提示）。
    """

    def __init__(self, *,
                 registry: ToolRegistry,
                 contract: ContractSlot | EditingContract | None = None,
                 skills: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
                 extra_options: Callable[[str, str], Awaitable[Sequence[str]]] | None = None,
                 catalog: ToolCatalog | None = None,
                 prompt_library: "PromptLibrary | None" = None) -> None:
        self.vocab = PlanVocabulary(registry=registry, contract=contract,
                                    extra_options=extra_options, catalog=catalog)
        self.validator = PlanValidator(self.vocab, skills=skills)
        self.compiler = PlanCompiler()
        # 只有一个消费者：``prompt.planning_section`` 从这里取规划轮文案的磁盘版本。
        # 没接（None）就用内联回落——提示词目录不在也不该让规划轮拼不出提示。
        self.prompt_library = prompt_library

    # ---- 事实层：转发 ----

    @property
    def catalog(self) -> ToolCatalog:
        return self.vocab.catalog

    @property
    def contract(self) -> EditingContract:
        return self.vocab.contract

    def resolve_tool(self, node: str) -> Tool | None:
        return self.vocab.resolve_tool(node)

    def whitelist(self) -> set[str]:
        return self.vocab.whitelist()

    def vocabulary(self) -> set[str]:
        return self.vocab.vocabulary()

    def param_keys(self) -> set[str]:
        return self.vocab.param_keys()

    def enum_values(self) -> set[str]:
        return self.vocab.enum_values()

    def output_keys(self) -> set[str]:
        return self.vocab.output_keys()

    def explicit_call_nodes(self) -> set[str]:
        """必须**显式写进 steps** 的节点（执行期不自动补齐的那几个）。

        规划轮提示段用它把这件事说在前面——原先只有校验器知道，模型要交一次卡、
        吃一次打回才知道。
        """
        return self.vocab.explicit_call_nodes()

    def planning_registry(self, *, submit_tool: Any, confirm_tool: Any) -> ToolRegistry:
        return self.vocab.planning_registry(submit_tool=submit_tool,
                                            confirm_tool=confirm_tool)

    async def knob_facts(self) -> list[dict[str, Any]]:
        return await self.vocab.knob_facts()

    # ---- 校验与编译：转发 ----

    async def validate(self, payload: Any) -> tuple[list[dict[str, Any]], PlanIssues]:
        return await self.validator.validate(payload)

    def validate_execute(self, plan: Mapping[str, Any],
                         frame: Mapping[str, Any]
                         ) -> tuple[dict[str, Any], PlanIssues]:
        return self.compiler.validate_execute(plan, frame)
