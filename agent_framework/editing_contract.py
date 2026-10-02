"""剪辑 DAG 的**机器可读契约**：从剪辑服务端取回「节点 → 前置依赖 / reducer」。

为什么要有这一层：节点定义只活在 Storyline 服务端（它是 DAG 与拦截器的唯一权威），
主服务这边只有 MCP 工具名和一段给人看的描述。以前靠正则从描述里抠「（需先完成：a, b）」
来猜依赖——那是把契约写成散文再解析回来，改文案就断。现在服务端直接给结构化契约：

* ``Tool.reads`` / ``Tool.writes``（Agent 循环的并发分批依据，见 ``tool.plan_batches``）
* ``downstream(node)``：分叉重跑时要把哪些产物一起作废（见 ``checkpoint.fork``）

契约取不到（server 没这个工具 / 离线）时退化成空表：所有节点未声明读写集 →
``plan_batches`` 按「无法判定即不同批」保守串行，正确性不依赖契约存在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


def store_key(node: str) -> str:
    """节点产物在状态总线上的键：与 ``orchestration.BaseNode.write_keys`` 同形状。"""
    return f"store:{node}"


@dataclass(frozen=True)
class NodeContract:
    name: str
    requires: tuple[str, ...] = ()
    reducer: str = "last_write_wins"
    # 节点自有入参的真实 schema（properties 那一层：type/description/enum/minimum/
    # maximum/unit）。MCP 的工具声明只带得出类型，枚举与上下界留在剪辑服务端，
    # 计划卡要拿它们当「可反查的枚举源」，所以随契约一并取回。
    params: Mapping[str, Any] = field(default_factory=dict)
    # 该节点**不会**被主服务自动补齐（需 LLM 显式调用并传创意决策参数）。
    # 计划门据此在出卡阶段就挡下「漏了这个前置」的计划——否则用户确认完，
    # 执行到下游那一步才抛错，整轮白跑。
    explicit: bool = False

    @property
    def writes(self) -> frozenset[str]:
        return frozenset({store_key(self.name)})

    @property
    def reads(self) -> frozenset[str]:
        return frozenset(store_key(d) for d in self.requires)


@dataclass
class EditingContract:
    nodes: dict[str, NodeContract] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def get(self, name: str) -> NodeContract | None:
        return self.nodes.get(name)

    @property
    def names(self) -> list[str]:
        return list(self.nodes)

    def required_map(self) -> dict[str, list[str]]:
        """{节点名: [前置依赖]}——任务板按 DAG 拆解、依赖校验都吃这个形状。"""
        return {n: list(c.requires) for n, c in self.nodes.items()}

    def downstream(self, name: str) -> set[str]:
        """本节点之后（含自身）会被它影响的节点集合——分叉重跑要一并作废的产物。"""
        if name not in self.nodes:
            return {name}
        dependents: dict[str, set[str]] = {}
        for n in self.nodes.values():
            for dep in n.requires:
                dependents.setdefault(dep, set()).add(n.name)
        out: set[str] = {name}
        stack = [name]
        while stack:
            cur = stack.pop()
            for nxt in dependents.get(cur, ()):
                if nxt not in out:
                    out.add(nxt)
                    stack.append(nxt)
        return out


def _unwrap(payload: Any) -> Any:
    """MCP tools/call 的原始结果（content 块列表）→ 其中的文本；其他形状原样返回。"""
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        texts = [str(b.get("text", "")) for b in payload["content"]
                 if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return "\n".join(t for t in texts if t)
    return payload


def parse_contract(payload: Any) -> EditingContract:
    """``dag_contract`` 的返回（MCP 结果 / JSON 文本 / 已解析 dict）→ EditingContract。"""
    payload = _unwrap(payload)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return EditingContract()
    if not isinstance(payload, dict):
        return EditingContract()
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return EditingContract()
    out: dict[str, NodeContract] = {}
    for name, spec in nodes.items():
        if isinstance(spec, dict):
            params = spec.get("params")
            out[str(name)] = NodeContract(
                name=str(name),
                requires=tuple(str(x) for x in (spec.get("requires") or [])),
                reducer=str(spec.get("reducer") or "last_write_wins"),
                params=params if isinstance(params, dict) else {},
                explicit=bool(spec.get("explicit", False)),
            )
        elif isinstance(spec, (list, tuple)):
            out[str(name)] = NodeContract(name=str(name),
                                          requires=tuple(str(x) for x in spec))
    return EditingContract(nodes=out)


async def load_contract(client: Any, *, timeout: float = 30.0,
                        tool_name: str = "dag_contract") -> EditingContract:
    """向剪辑服务端要一次契约；拿不到就回空契约（调用方按「未声明」保守处理）。"""
    try:
        raw = await client.call_tool(tool_name, {}, timeout)
    except Exception:  # noqa: BLE001 - 契约缺失是降级，不是故障
        return EditingContract()
    return parse_contract(raw)


class ContractSlot:
    """装配期发出去、启动后才填上的契约引用。

    主服务先建好 Team 与 ``rerun_from`` 等部件，Storyline 才在 ``on_startup`` 里连上；
    传引用而非值，这些部件在真正被调用时才读到契约，不必为顺序互相等。
    """

    def __init__(self, contract: EditingContract | None = None) -> None:
        self.contract = contract or EditingContract()

    def fill(self, contract: EditingContract) -> None:
        self.contract = contract

    def __bool__(self) -> bool:
        return bool(self.contract)

    @property
    def names(self) -> list[str]:
        return self.contract.names

    def required_map(self) -> dict[str, list[str]]:
        return self.contract.required_map()

    def downstream(self, name: str) -> set[str]:
        return self.contract.downstream(name)
