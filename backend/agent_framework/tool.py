"""工具抽象父类 Tool 与注册中心 ToolRegistry。

对应设计文档「工具注册和执行」章节：子工具继承 Tool 重写 name/description/
parameters/execute；Registry 负责注册、导出 schema、按名执行，并对参数做校验。

并发契约（供 Agent 循环拆执行批次）：
  read_only / exclusive / concurrency_safe  —— 粗粒度门禁；
  reads / writes                            —— 细粒度**状态键**集合，两个调用冲突的
    定义是「写集相交」或「一方的写集撞上另一方的读集」。原来靠「同批里出现了被依赖的
    节点就整体串行」的启发式，说不清两个工具同时写同一份产物会怎样；现在写哪个键是
    显式声明的，同键并发就有确定语义（见 orchestration.Store 的 reducer）。
失败契约：Registry.execute 失败时返回 ``ToolError`` 实例（其字符串形式就是回喂给模型
的文本），调用方按类型判定，不再嗅探 ``startswith("Error")``。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, Iterable

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}

# 工具执行失败时追加给 LLM 的提示，引导其换思路重试。
_HINT = "\n\n[Analyze the error above and try a different approach.]"


class ToolError(Exception):
    """工具执行失败。带工具名与原始详情，字符串形式即回喂给模型的文本。

    ``counts_as_failure`` 把两种「失败」分开：

    * ``True``（默认）——真跑坏了（超时、后端报错）。计入连续失败守卫，连着两次就
      让模型停下来别再原地重试。
    * ``False``——**这条错误本身就是可执行的反馈**：模型读着清单改输入再交一次，
      那就是正路，不是原地重试。典型是 ``submit_plan`` 的校验打回。

    混在一起算，守卫会把「改一个字再交一次」判成「工具坏了」，逼模型停下来把锅
    抛给用户——真机实测：规划轮就因此弹出一张「当前这轮没有可用的剪辑执行工具，
    你希望怎么处理？」的选项卡，用户对着它无从下手。
    """

    def __init__(self, tool: str, detail: str, *,
                 counts_as_failure: bool = True) -> None:
        self.tool = tool
        self.detail = detail
        self.counts_as_failure = counts_as_failure
        super().__init__(detail)

    def __str__(self) -> str:
        detail = self.detail if self.detail.startswith("Error") else f"Error: {self.detail}"
        return f"{detail}{_HINT}"


class UnknownToolError(ToolError):
    """注册表里没有这个工具——调用从未发生。

    规划轮里模型试调剪辑节点必然走到这条路：它既不是某个剪辑步骤跑坏了，更不是那一步
    真跑过。进度卡与计划对账都按这个类型把它摘出去，否则一条规划轮会把整会话顶成
    「✂ 剪辑受阻 · 有一步失败」，还把没执行的步骤记成已履行。
    """


def is_tool_error(result: Any) -> bool:
    """一次工具返回值是不是失败——唯一的判定口。"""
    return isinstance(result, ToolError)


def _keys(values: Iterable[str] | None) -> frozenset[str]:
    return frozenset(v for v in (values or ()) if v)


class Tool(ABC):
    """所有工具的抽象父类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，用于 function call。"""

    @property
    def display_name(self) -> str:
        """面向用户的中文名；空 = 未声明（界面上只能退回机器名）。

        机器名是**键**（Store key、``artifacts.node``、DAG 契约字段），不能改也无需改；
        这一条只是给人看的那一版，出口处由 ``catalog.ToolCatalog`` 统一替换。
        """
        return ""

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述，供 LLM 判断何时调用。"""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """工具入参的 JSON Schema。"""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """执行工具，返回字符串或内容块列表。"""

    # ---- 并发能力判定（默认保守：不可并发）----

    @property
    def read_only(self) -> bool:
        """是否只读、无副作用。"""
        return False

    @property
    def exclusive(self) -> bool:
        """即便开启并发也需独占执行。"""
        return False

    # ---- 状态键读写集（并发调度的确定依据）----

    @property
    def reads(self) -> frozenset[str]:
        """本工具会读的状态键；空集 = 未声明（按「证明不了独立就不同批」处理）。"""
        return frozenset()

    @property
    def writes(self) -> frozenset[str]:
        """本工具会写的状态键。未声明的写工具一律独占一批（见 ``plan_batches``）。"""
        return frozenset()

    @property
    def concurrency_safe(self) -> bool:
        """是否可与其他工具进同一批 gather。

        两条来路：只读无副作用；或**声明了状态键读写集**——那就不必再退到「写工具一律
        串行」这道粗门禁，``conflicts()`` 会按键精确拆开真正冲突的两个调用。两条都不满足
        （会写又没声明键）的工具独占一批。
        """
        if self.exclusive:
            return False
        return self.read_only or bool(self.writes or self.reads)

    # ---- HITL：执行前是否需要人工审批 ----

    @property
    def requires_approval(self) -> bool:
        """True = 执行前挂起等人工审批（HITL 断点）。

        默认 False：不擅自改变既有流程。需要审批的工具由装配方显式打开——
        工具自声明，或 run_server 的 ``--approve-tools`` 按名覆盖。
        """
        return False

    # ---- schema 导出 ----

    def to_schema(self) -> dict[str, Any]:
        """导出 OpenAI function-calling 格式的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        """把 JSON Schema 的联合类型（如 ["string","null"]）解析为首个非 null 类型。"""
        if isinstance(t, list):
            for item in t:
                if item != "null":
                    return item
            return None
        return t


class ToolRegistry:
    """工具注册中心：动态注册、导出 schema、按名执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # HITL：按名覆盖的审批集合（--approve-tools）；工具自声明 requires_approval 也认。
        self._approve: set[str] = set()
        # 「本轮为什么少了某些工具」的一句话说明（例如规划轮按设计挡掉了剪辑节点）。
        # 只影响报错文案，见 _unknown_tool_error。
        self.unknown_tool_hint: str = ""

    def set_approval(self, names: Iterable[str]) -> None:
        """装配期指定「执行前需人工审批」的工具名集合。"""
        self._approve = {n for n in names if n}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def needs_approval(self, name: str) -> bool:
        """该工具执行前是否要先挂起等人工审批（工具自声明 或 --approve-tools 覆盖）。"""
        tool = self._tools.get(name)
        return bool(tool is not None and (tool.requires_approval or name in self._approve))

    def get_definitions(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    def displays(self) -> dict[str, str]:
        """{机器名: 中文名}，只收声明了的（未声明的不进表，出口处退回机器名）。"""
        return {t.name: t.display_name for t in self._tools.values() if t.display_name}

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def all_tools(self) -> list[Tool]:
        """已注册的全部工具（只读遍历用；别拿它改注册表）。"""
        return list(self._tools.values())

    def _unknown_tool_error(self, name: str) -> str:
        """调了一个不存在的工具时的报错；能给出「它为什么不在」就说清楚。

        为什么值得单独一条：规划轮会**刻意**把剪辑执行节点挡在门外（那是设计，
        不是故障）。原先只回一句 ``tool '<中文名>' not found``，模型看不出这是
        「本轮按设计不给你」，于是转而去问用户「工具不可用，你希望怎么处理？」——
        真机实测它就卡在这里问了一轮。这里把「本轮按设计不提供」和「这个名字根本
        不存在」分开说，模型就知道该走 submit_plan 而不是来问用户。
        """
        hint = self.unknown_tool_hint
        if hint:
            return (f"Error: 本轮不提供「{name}」。{hint}")
        return f"Error: tool '{name}' not found"

    def prepare_call(
        self, name: str, params: dict[str, Any]
    ) -> tuple[Tool | None, dict[str, Any], str | None]:
        """解析并校验一次调用。返回 (tool, casted_params, error)。"""
        tool = self._tools.get(name)
        if tool is None:
            return None, params, self._unknown_tool_error(name)

        schema = tool.parameters or {}
        props: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])

        missing = [k for k in required if k not in params]
        if missing:
            return tool, params, f"Error: missing required params {missing}"

        casted: dict[str, Any] = {}
        for key, value in params.items():
            spec = props.get(key)
            if spec is None:
                casted[key] = value
                continue
            expected = Tool._resolve_type(spec.get("type"))
            py_type = _TYPE_MAP.get(expected or "")
            if py_type and not isinstance(value, py_type):
                try:
                    casted[key] = py_type(value)
                except (TypeError, ValueError):
                    return (
                        tool,
                        params,
                        f"Error: param '{key}' expected {expected}, got {value!r}",
                    )
            else:
                casted[key] = value
        return tool, casted, None

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """执行一次工具调用。失败一律返回 ``ToolError``：它是失败信号的唯一类型。"""
        tool, params, error = self.prepare_call(name, params)
        if error:
            # 「工具不存在」与「参数不对」都算失败，但只有前者意味着调用从未发生。
            return (UnknownToolError(name, error + _HINT) if tool is None
                    else ToolError(name, error + _HINT))
        assert tool is not None  # guarded by prepare_call()
        try:
            result = tool.execute(**params)
            if inspect.isawaitable(result):
                result = await result
        except ToolError as e:          # 工具自己判定的失败：原样回喂，不再套一层
            return e
        except Exception as e:  # noqa: BLE001 - 工具异常需回喂给 LLM
            return ToolError(name, f"Error executing {name}: {e}")
        if result is None:
            return ToolError(name, f"Error executing {name}: 工具返回空值")
        if isinstance(result, str) and result.startswith("Error"):
            return ToolError(name, result + _HINT)
        return result

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# --------------------------------------------------------------------------
# 并发调度：按状态键冲突把一批调用切成「组内并发、组间按序」的批次
# --------------------------------------------------------------------------


def conflicts(a: Tool, b: Tool) -> bool:
    """两个工具能不能同批跑。

    冲突的确定定义：写集相交（谁最后写没有定义），或一方的写集撞上另一方的读集
    （后者会读到半程结果）。未声明读写集的工具不参与冲突判定——它们仍受
    ``concurrency_safe`` 这道粗门禁约束。
    """
    if not (a.writes or a.reads) or not (b.writes or b.reads):
        return False
    return bool((a.writes & b.writes) or (a.writes & b.reads)
                or (b.writes & a.reads))


def plan_batches(calls: list[Any], resolve: Any) -> list[list[Any]]:
    """ToolCall 列表 → 执行批次（组内可 gather，组间必须按序）。

    贪心且保持 LLM 给出的原始顺序：每个调用尽量塞进已有的靠前的批次，塞不进就开新批次。
    ``resolve(tool_name) -> Tool | None`` 由调用方提供（注册表查询）。
    """
    batches: list[list[Any]] = []
    # None 哨兵 = 这一批已封死（里面有不可并发的调用），后续调用不得再加入。
    tools_in: list[list[Tool] | None] = []
    for call in calls:
        tool = resolve(getattr(call, "name", call))
        if tool is None or not tool.concurrency_safe:
            batches.append([call])           # 不可并发者独占一批
            tools_in.append(None)
            continue
        target = next(
            (i for i, seated in enumerate(tools_in)
             if seated is not None
             and not any(conflicts(tool, other) for other in seated)),
            None)
        if target is None:
            batches.append([call])
            tools_in.append([tool])
        else:
            batches[target].append(call)
            tools_in[target].append(tool)
    return batches


class EchoTool(Tool):
    """示例工具：原样回显输入，用于验证注册与执行链路。"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显传入的文本，用于连通性测试"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要回显的文本"}
            },
            "required": ["text"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, text: str) -> str:
        return f"echo: {text}"
