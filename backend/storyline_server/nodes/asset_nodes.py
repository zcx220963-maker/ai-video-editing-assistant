"""R3 素材供给节点：search_illustration / select_illustration。

与 core_nodes.SearchMediaNode 的区别：
- search_media 走 materials 表（用户上传/产出的素材），kinds=("video",)
- search_illustration 走 asset_library 表（内置 CC0/PD 素材库），全局共享
- select_illustration 从 search_illustration 结果里选一个，返回 asset_id 供规划轮填进 assets 栏

§5.2 入库配方把 Wikimedia Commons/Iconify/Poly Haven/ambientCG/Open Peeps/Humaaans/Kenney/Quaternius
的 CC0/PD 切片灌进 asset_library，这两个节点就是检索/选用那批素材的入口。
"""

from __future__ import annotations

from typing import Any

from agent_framework.orchestration import NodeState

from .core_nodes import StoryNode, _obj


class SearchIllustrationNode(StoryNode):
    """检索内置素材库（CC0/PD 图片/图标/质感底）。

    与 search_media 互补：search_media 搜用户上传的视频素材，
    search_illustration 搜内置库的图片/图标/质感底——后者用于 motion 渲染链的 image 通道。
    """
    name = "search_illustration"
    display_name = "检索配图"
    description = "在内置素材库中检索配图/图标/质感底（Wikimedia Commons/Iconify 等 CC0/PD 源）"
    input_schema = _obj("检索关键词与可选过滤", {
        "query": {"type": "string", "description": "检索关键词"},
        "subject": {"type": "string", "description": "主体类型：portrait/animal/object/map/building/icon/texture"},
        "style_family": {"type": "string", "description": "风格族：photo/illustration/icon/3d/texture"},
        "source": {"type": "string", "description": "素材源：wikimedia-commons/iconify/polyhaven/ambientcg/open-peeps/humaaans/kenney/quaternius"},
    })

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        query = str(inputs.get("query") or state.user_request or "").strip()
        subject = str(inputs.get("subject") or "").strip() or None
        style_family = str(inputs.get("style_family") or "").strip() or None
        source = str(inputs.get("source") or "").strip() or None
        rows = await self.storage.assets.search(
            query, subject=subject, style_family=style_family,
            source=source, limit=20)
        return {
            "found": [
                {"asset_id": r["id"], "name": r.get("src_id") or r.get("caption") or "",
                 "sha256": r["sha256"], "ext": r["ext"],
                 "subject": r.get("subject") or "", "style_family": r.get("style_family") or "",
                 "source": r.get("source") or "", "license": r.get("license") or "",
                 "width": r.get("width"), "height": r.get("height")}
                for r in rows
            ],
            "source": "asset_library",
            "count": len(rows),
        }


class SelectIllustrationNode(StoryNode):
    """从 search_illustration 结果里选一个素材，返回 asset_id 供规划轮填进 assets 栏。

    LLM 可以直接从 search_illustration 的 found 列表里挑 asset_id 填进 spec 的 assets 栏，
    不需要经过这个节点——这个节点是给 DAG 链路用的确定性入口。
    """
    name = "select_illustration"
    display_name = "选用配图"
    description = "从检索结果中选用一个配图素材，返回 asset_id 供规划轮填进 assets 栏"
    input_schema = _obj("选用配图", {
        "asset_id": {"type": "string", "description": "素材 ID（来自 search_illustration 的 found[].asset_id）"},
    }, required=["asset_id"])

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(inputs.get("asset_id") or "").strip()
        if not asset_id:
            raise ValueError("select_illustration：没有 asset_id（请先 search_illustration）")
        row = await self.storage.assets.get(asset_id)
        if not row:
            raise ValueError(f"select_illustration：asset_id {asset_id} 不存在或已被禁用")
        if row.get("attribution_required"):
            raise ValueError(f"select_illustration：asset_id {asset_id} 需要署名（attribution_required=true），"
                             "不进检索结果（§5.2）")
        return {
            "asset_id": row["id"], "name": row.get("src_id") or row.get("caption") or "",
            "sha256": row["sha256"], "ext": row["ext"],
            "subject": row.get("subject") or "", "style_family": row.get("style_family") or "",
            "source": row.get("source") or "", "license": row.get("license") or "",
            "object_key": row.get("object_key") or "",
            "width": row.get("width"), "height": row.get("height"),
        }


ASSET_NODE_CLASSES = [SearchIllustrationNode, SelectIllustrationNode]