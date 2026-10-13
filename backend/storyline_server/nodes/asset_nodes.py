"""R3 素材供给节点：search_illustration / select_illustration。

与 core_nodes.SearchMediaNode 的区别：
- search_media 走 materials 表（用户上传/产出的素材），kinds=("video",)
- search_illustration 走 asset_library 表（内置 CC0/PD 素材库），全局共享
- select_illustration 从 search_illustration 结果里选一个，返回 asset_id 供规划轮填进 assets 栏

§5.2 入库配方把各个 CC0/PD 源的切片灌进 asset_library，这两个节点就是检索/选用那批素材的入口。

**源名不在这里写**（R11-T50/P36）：`source` 那一栏的枚举与说明由
``agent_framework.asset_sources`` 那份注册表生成。这里曾经手写着一串 8 个源名，而
命令行只有 5 条腿、活库里只有 2 个源有行——模型照着 schema 点一个必然是空的源，
它只会得出「这库不行」。供给面由注册表与活库生成，不由文案生成。
"""

from __future__ import annotations

from typing import Any

from agent_framework.asset_sources import ASSET_ROLES, declared_sources, source_spec
from agent_framework.identity import storyline_session_id
from agent_framework.library_pick import pick_library_asset
from agent_framework.orchestration import NodeState

from .core_nodes import StoryNode, _obj


class SearchIllustrationNode(StoryNode):
    """检索内置素材库（CC0/PD 图片/图标/质感底）。

    与 search_media 互补：search_media 搜用户上传的视频素材，
    search_illustration 搜内置库的图片/图标/质感底——后者用于 motion 渲染链的 image 通道。
    """
    name = "search_illustration"
    display_name = "检索配图"
    description = "在内置素材库中检索配图/图标/质感底（CC0/PD 源，可用的源由注册表与活库决定）"
    input_schema = _obj("检索关键词与可选过滤", {
        "query": {"type": "string", "description": "检索关键词"},
        "subject": {"type": "string", "description": "主体类型：portrait/animal/object/map/building/icon/texture"},
        "style_family": {"type": "string", "description": "风格族：photo/illustration/icon/3d/texture"},
        # 枚举与描述都从注册表生成（㉔ 第①半判的就是「这一栏里的名字全在注册表里、
        # 且都是今天可声明的」）。
        "source": source_spec(),
        # R11-T53：按「素材在片子里的角色」找件（部件档要的是 part、底图要的是 bg）。
        # 不传＝不按角色过滤（未判的件也进结果）——按角色找件的人不该拿到未判件，
        # 但「随便搜搜」不该因为这一栏空着就搜不到东西。
        "asset_role": {"type": "string", "enum": list(ASSET_ROLES),
                       "description": "素材角色过滤，不传＝不过滤：icon 一枚符号 / part 拼图部件 / "
                                      "scene 一整幅画面 / texture 质感底 / bg 铺满屏的底图。"
                                      "角色由机器初标（Q114），空着的那些件只在不传时才进结果"},
    }, required=["query"])

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        query = str(inputs.get("query") or state.user_request or "").strip()
        subject = str(inputs.get("subject") or "").strip() or None
        style_family = str(inputs.get("style_family") or "").strip() or None
        source = str(inputs.get("source") or "").strip() or None
        role = str(inputs.get("asset_role") or "").strip() or None
        # Q111：把「不可达」和「没素材」分开。回执里必须能读出三态，而且**只有第三态
        # （源名不在可声明集合里）才允许出现源名以外的解释文字**——否则模型会把
        # 「这个源今天没开」误读成「库里没有这类素材」，接着去改写关键词，白跑几轮。
        reason, note = "hit", ""
        if source and source not in declared_sources():
            reason = "source_not_available"
            note = (f"源 {source} 今天不在可声明集合里（可用：{'、'.join(declared_sources())}）——"
                    "**不是库里没有这类素材**，是这条腿还没跑通；"
                    "去掉 source 再检索，或在 schema 给出的源里选一个")
            rows: list[dict[str, Any]] = []
        else:
            rows = await self.storage.assets.search(
                query, subject=subject, style_family=style_family,
                source=source, asset_role=role, limit=20)
            if not rows:
                reason = "no_match"
                # 「库内确无」要有证据：把「这个源一共有多少行」摆出来，
                # 「源是空的」与「关键词没匹配上」在回执上才是两件能分开的事。
                note = (f"库内确无此类目：{await _pool_note(self.storage, source)}。"
                        "换关键词（试着用更具体的物件名），或换 subject/asset_role 再试")
        return {
            "found": [
                {"asset_id": r["id"], "name": r.get("src_id") or r.get("caption") or "",
                 "sha256": r["sha256"], "ext": r["ext"],
                 "subject": r.get("subject") or "", "style_family": r.get("style_family") or "",
                 "source": r.get("source") or "", "license": r.get("license") or "",
                 "asset_role": r.get("asset_role") or "",
                 "scale_safe": r.get("scale_safe"),
                 "width": r.get("width"), "height": r.get("height")}
                for r in rows
            ],
            "source": "asset_library",
            "count": len(rows),
            # Q111 三态：hit 命中 / no_match 库内确无此类目 / source_not_available 该源不在
            # 可声明集合里。第三态才带「换源」那句解释，前两态只给下一步动作。
            "reason": reason,
            "note": note,
        }


async def _pool_note(storage: Any, source: str | None) -> str:
    """`no_match` 那一态的**证据栏**：这个源（或整库）一共有多少行。

    为什么非要有这一句：「库里 0 行」与「关键词没匹配上」在回执里长得一样，
    而它们的下一步动作完全相反——前者要去跑腿（或换源），后者只要换关键词。
    读不到行数时如实说「没读到」，不把读不到说成 0。
    """
    scope = f"源 {source}" if source else "整库"
    try:
        n = await storage.assets.count(source=source)
    except Exception:      # noqa: BLE001
        return f"{scope}的行数没读到（库不可达）——这一步是未判，不是「没有素材」"
    if n <= 0:
        return f"{scope}里 0 行（这条腿还没入过库，换关键词没用）"
    return f"{scope}共 {n} 行，但没匹配上这个关键词"


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
        # R11-T54：**部件档**（icon/part）要过「最细笔画实测 ≥2 px」这一关。只拦
        # `False`（量出来不够细）；`None` 是**未判**（栅格件、纯填充路径量不出最细笔画），
        # 未判**不拦**——它只是没被这一格证明，拦它等于把「没量」当「不合格」；
        # 但它会带着 `scale_note` 一起回给模型，让「我知道这件没量过」进上下文。
        role = str(row.get("asset_role") or "")
        scale_note = ""
        if role in ("icon", "part"):
            facts = row.get("scale_facts") or {}
            if not isinstance(facts, dict):
                facts = {}
            verdict = row.get("scale_safe")
            if verdict is False:
                raise ValueError(
                    f"select_illustration：asset_id {asset_id}（角色 {role}）缩到片子里的最小尺寸时"
                    f"最细笔画不够（{facts.get('reason') or '实测未过'}）——部件档的最小占地是画布短边的 "
                    "0.18，这一档上它会糊成一道灰雾。换一件，或把它当 scene/bg 用（那一档按整幅画面放，"
                    "不走这个 0.18 的缩放）")
            scale_note = str(facts.get("reason") or "")
            if verdict is None:
                scale_note = f"最细笔画**未判**：{scale_note or '量不出来（栅格件或纯填充路径）'}"
        out = {
            "asset_id": row["id"], "name": row.get("src_id") or row.get("caption") or "",
            "sha256": row["sha256"], "ext": row["ext"],
            "subject": row.get("subject") or "", "style_family": row.get("style_family") or "",
            "source": row.get("source") or "", "license": row.get("license") or "",
            "asset_role": role,
            "scale_safe": row.get("scale_safe"),
            "scale_note": scale_note,
            "object_key": row.get("object_key") or "",
            "width": row.get("width"), "height": row.get("height"),
        }
        # T45：选用的同时把字节投进本会话的 materials（origin='library'），让
        # 「检索 → 选用 → 上屏」闭环——只回一个 asset_id 的话，下游那条「只认
        # material_id」的路仍然拿不到字节。投递失败不改判这次选用：回执里带
        # `material_note` 说明原因（`pick_library_asset` 的形制就是不抛异常）。
        if state.user_id and state.conversation_id:
            mid, note = await pick_library_asset(
                self.storage, storyline_session_id(state.user_id, state.conversation_id),
                row["id"], conversation_id=state.conversation_id)
            out["material_id"] = mid
            out["material_note"] = note
        else:
            out["material_id"] = ""
            out["material_note"] = "没有会话身份（离线直调），这条库素材没投进素材库"
        return out


ASSET_NODE_CLASSES = [SearchIllustrationNode, SelectIllustrationNode]