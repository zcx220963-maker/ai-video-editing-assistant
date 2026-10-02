"""run 层的模型工具：``rerun_from``——从某个节点处回退并换新产物作用域重跑。

对应「换 BGM 重渲染、上游产物全复用」这条真实诉求：剪辑流程走到第 8 步发现配乐不对，
要重做的只有 select_BGM 及其下游，切镜/ASR/画面理解那些昂贵的上游结果必须留下。

机制（三件事同时发生，缺一即错位）：
1. ``CheckpointManager.fork`` 取该节点结果**之前**的一致点，开一条子 run；
2. 子 run 换一份 ``artifact_id``，并把父 run 的产物集复制过来、只作废重跑点及其下游
   （作废靠 ``EditingContract.downstream``，所以剪辑 DAG 必须先接入）；
3. 本循环把当前执行交给子 run：上下文回退、身份里的产物作用域换掉、父 run 置 superseded。

之后模型再调 select_BGM / render_video，MCP 包装层带着新 artifact_id 出门，服务端拦截器
在新作用域里看到的正是「上游齐全、重跑点空缺」的状态。

``run_id`` 让回溯跨出本轮：用户说「回到上次那条再换一首」时，分叉点是**那条 run** 的一致点，
不是当前这条。两条入口（本轮 / 跨轮）共用同一套 fork 原语，差别只在归属校验——
跨轮只认同一会话里的 run，别人的或已清的 run 一律回绝。
"""

from __future__ import annotations

import json
from typing import Any

from ..editing_contract import ContractSlot
from ..hooks import _current_hook_ctx
from ..tool import Tool, ToolError, ToolRegistry


class RerunFromTool(Tool):
    """让模型自己回到某个节点之前重跑（不改历史，只开分支）。"""

    def __init__(self, contract_slot: ContractSlot) -> None:
        self._slot = contract_slot

    @property
    def name(self) -> str:
        return "rerun_from"

    @property
    def display_name(self) -> str:
        return "从某步重跑"

    @property
    def description(self) -> str:
        return (
            "回到某个剪辑节点执行**之前**的状态并在那里重跑：该节点及其下游的产物作废，"
            "上游产物（切镜、ASR、画面理解等）原样复用，且重跑写在一份新的产物集里，"
            "原先那一版不会被改花。用户说「换个 BGM 重新出片」「这段文案重做」时用。"
            "默认回溯本轮这条执行；用户说「回到上次那一版」时给 run_id（本会话内的历史执行，"
            "可只给前缀）。"
            f"可分叉的节点：{', '.join(self._slot.names) or '（剪辑 DAG 未接入）'}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "node": {"type": "string",
                         "description": "重跑起点节点名，如 select_BGM / plan_timeline"},
                "reason": {"type": "string", "description": "为什么重跑（写进本次分支，便于回看）"},
                "run_id": {
                    "type": "string",
                    "description": ("从**哪一次执行**回溯（限当前会话，可只给前 8 位）。"
                                    "不给就是本轮这条 run；给了就是跨轮回溯——"
                                    "「回到上次那一版换个 BGM」才需要它。"),
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "重跑那一步及其之后要**照这句做**的新诉求（会作为子执行最新的一条用户指令）。"
                        "回退之后你看到的上下文仍停在原话，所以必须把改动写清楚，"
                        "例如「背景音乐改用《X》，query 填 X 的完整歌名，然后规划时间线并出片」。"),
                },
            },
            "required": ["node"],
        }

    # 会开新 run、改产物作用域：不参与并发批次。
    @property
    def read_only(self) -> bool:
        return False

    async def _resolve_run(self, mgr, cp, run_id: str) -> str:
        """目标 run：默认本轮；给了 run_id 就必须在**同一会话**里且前缀唯一命中。"""
        if not run_id.strip():
            return cp.run_id
        runs = await mgr.list_for_session(cp.session_id)
        hits = [r for r in runs if str(r.get("run_id", "")).startswith(run_id.strip())]
        if not hits:
            raise ToolError("rerun_from",
                            f"本会话没有以「{run_id}」开头的执行（跨会话与不存在同样回绝）。"
                            f"本会话最近的执行：{[str(r.get('run_id', ''))[:8] for r in runs[:5]]}")
        if len(hits) > 1:
            raise ToolError("rerun_from",
                            f"「{run_id}」在本会话里命中 {len(hits)} 条执行，请给更长的前缀："
                            f"{[str(r.get('run_id', ''))[:12] for r in hits[:6]]}")
        row = hits[0]
        if str(row.get("session_id") or "") != cp.session_id:
            raise ToolError("rerun_from", "那条执行不属于当前会话")
        return str(row["run_id"])

    async def _candidates(self, mgr, cp, node: str) -> list[dict[str, Any]]:
        """本会话里确实跑过这个节点的历史执行（错误消息里给模型当路标用）。"""
        out: list[dict[str, Any]] = []
        for r in await mgr.list_for_session(cp.session_id):
            rid = str(r.get("run_id") or "")
            if not rid:
                continue
            at = next((p["seq"] for p in await mgr.history(rid)
                       if node in (p.get("tools") or [])), None)
            if at is not None:
                out.append({"run_id": rid, "seq_with_node": at,
                            "status": r.get("status"), "message": r.get("message"),
                            "artifact_id": (r.get("scope") or {}).get("artifact_id") or ""})
        return out[:8]

    async def execute(self, node: str, reason: str = "", instruction: str = "",
                      run_id: str = "") -> str:
        ctx = _current_hook_ctx.get()
        if ctx is None:
            raise ToolError("rerun_from", "只能在一次 Agent 执行内调用")
        mgr = ctx.extras.get("checkpoint_manager")
        cp = ctx.extras.get("checkpoint")
        if mgr is None or cp is None:
            raise ToolError("rerun_from",
                            "本次执行未开启 checkpoint 快照，无从回退（服务以 --no-checkpoint 启动）")
        if not self._slot:
            raise ToolError("rerun_from",
                            "剪辑 DAG 尚未接入（Storyline 未连通），不知道哪些节点受它影响")
        if node not in self._slot.names:
            raise ToolError("rerun_from",
                            f"未知节点「{node}」。可分叉的节点：{self._slot.names}")
        target = await self._resolve_run(mgr, cp, run_id)
        cross = target != cp.run_id
        try:
            at_seq = await mgr.seq_before_tool(target, node)
        except KeyError as e:
            detail = str(e.args[0] if e.args else e)
            if run_id.strip():        # 指定了历史执行还找不到，就别再猜
                raise ToolError("rerun_from", detail) from e
            cands = await self._candidates(mgr, cp, node)
            hint = (f"本会话里有这些历史执行跑过「{node}」，把它们的 run_id 填进来即可跨轮回溯："
                    f"{cands}" if cands else
                    f"本会话没有任何一次执行跑过「{node}」")
            raise ToolError("rerun_from", f"{detail}{('；' + hint) if cands or not cross else ''}") from e
        dropped = sorted(self._slot.downstream(node))
        child = await mgr.fork(target, at_seq, invalidate=dropped, message=instruction)
        if cross:
            # fork 就地让位的是「被回溯的那条老 run」，本轮原来这条不在它的谱系里，
            # 没人收它就会永远停在 running：崩溃恢复与「继续」都把它当在途分支重播一遍
            # （而它已经被本轮弃用了）。同一轮回溯不需要这句——让位的正是它自己。
            await mgr.supersede(cp.run_id)
        ctx.extras["handover"] = child
        return json.dumps({
            "rerun_from": node,
            "at_seq": at_seq,
            "forked_from": target,
            "cross_run": cross,
            "run_id": child.run_id,
            "artifact_id": child.artifact_id,
            "invalidated": dropped,
            "reason": reason,
            "instruction": child.message,
            "note": ("本轮执行已接到新 run 与新产物作用域；上一条用户指令就是这次重跑要照办的"
                     f"新要求，请直接从 {node} 开始做，勿复用回退点之后的旧结果。"
                     "这一次分叉之后不要再分第二次——重做的那一步会写进新作用域。"
                     + ("（这次是从本会话更早的一次执行回溯的。）" if cross else "")),
        }, ensure_ascii=False)


def register_run_tools(registry: ToolRegistry, contract_slot: ContractSlot) -> None:
    """把 run 层工具（目前只有 rerun_from）注册进已有 registry。"""
    registry.register(RerunFromTool(contract_slot))
