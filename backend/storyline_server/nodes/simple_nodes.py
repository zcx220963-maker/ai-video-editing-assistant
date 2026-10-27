"""简洁视频线的 Node 外壳（R18）。

一条能力，一个 Node：`simple_film`。它的 `required_nodes` 是空的——这不是偷懒，
是这条线**本来就没有上游节点**：一句话进、成片出，中间的分镜/写码/质检/渲染
都在 `simple/pipeline.run()` 里面。

设计 §21 要求「流程原子化」，这一条**没做到**（simple 内部仍是单节点黑盒）。
差距登记在文档 §13 X1：硬拆会让 6 个工具全部变成节点，且会破
`test_simple_boundary.py` 守着的 `simple/` 与 `code_spec` 边界。

调用方（HTTP `/simple/video`、Agent 的 6 个工具）都只认 `pipeline`，
本文件只负责「让它在 MCP 工具面上可见、可被 Agent 按 DAG 规则调用」。
"""

from __future__ import annotations

from typing import Any

from agent_framework.orchestration import BaseNode, NodeState

from ..simple import pipeline


class SimpleFilmNode(BaseNode):
    name = "simple_film"
    display_name = "一句话出片"
    description = (
        "用一句话生成一条完整的短视频（分镜 → 逐镜代码 → 质检 → 渲染 → 链接）。"
        "适合「没有实拍素材、只想要一条讲清楚某件事的片子」。"
        "成片后可继续用 reshoot_shot 改一镜、render_short_film 重渲。"
    )
    required_nodes: list[str] = []
    # 没有任何上游产物可跳过：本节点是这条线的起点。
    output_keys = ("mp4_url",)

    def __init__(self, settings: Any = None, providers: Any = None,
                 storage: Any = None, *, registry: Any = None, **_: Any) -> None:
        # 产品节点的构造签名是 cls(settings, providers, storage, registry=reg)
        # （`build_real_registry` 的工厂就这么调）。这里不继承 StoryNode（会与
        # core_nodes 形成循环导入），只把签名对齐，多余的关键字直接忽略。
        super().__init__()
        self.settings = settings
        self.providers = providers
        self.storage = storage
        self.registry = registry
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "要讲什么（一句话或一段要求）"},
            "seconds": {"type": "number", "description": "目标时长（秒），8~240"},
            "aspect": {"type": "string", "enum": ["9:16", "16:9", "1:1"],
                       "description": "画幅，默认 9:16"},
            "mode": {"type": "string", "enum": ["auto", "explainer", "story"],
                     "description": "片子形态；auto=按内容判断"},
            "voice": {"type": "boolean", "description": "是否配旁白（默认 true）"},
            "render": {"type": "boolean", "description": "false=只出分镜与静帧预览"},
        },
        "required": ["prompt"],
    }

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        got = await pipeline.run(
            inputs["prompt"],
            seconds=float(inputs.get("seconds") or 30.0),
            aspect=str(inputs.get("aspect") or "9:16"),
            user_id=state.user_id if hasattr(state, "user_id") else "",
            mode=str(inputs.get("mode") or "auto"),
            voice_on=bool(inputs.get("voice", True)),
        )
        # 只回契约字段 + 工程 id：整份 TSX 不进产物总线（几十 KB，且会过期）。
        return {
            "title": got.get("title") or "",
            "shots": got.get("shots"),
            "secs": got.get("secs"),
            "calls": got.get("calls"),
            "sources": got.get("sources") or [],
            "film_id": got.get("film_id") or "",
        }
