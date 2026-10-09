"""剪辑节点的**离线夹具**：mock 版 Node + 把 Node 包成 Agent 工具的那层包装。

生产装配里剪辑节点**只**来自 Storyline MCP（DAG、依赖补齐、产物存储都在那台服务上），
本模块不再被任何服务接入；留下它是因为这套 mock 图带着与远端同构的契约
（required_nodes / require_prior_kind / reads / writes / reducer），能在不联网、不起服务的
前提下验证编排层：

- 每个 Node 包成一个 Tool（对应文档 wrapper/register 把 Node 注册成 MCP Tool）；LLM 选中
  某工具后，真正执行前经过 Interceptor —— 未满足的 required_nodes 会被递归补齐，从而约束
  模型「按剪辑流程行动」，且**不强制走到 render_video**（终止点由模型/用户决定）。
- 拓扑本身由 ``validate_dag``/``topo_order`` 把住；``node_required_map`` 把这张图转成
  依赖映射，供离线用例顶替生产路径上的 ``dag_contract``。
- Storyline 的 MCP 配置写在 TOML 里（见文档 [local_mcp_server]）：本模块用标准库 tomllib
  读取，给出 available_nodes 白名单与服务地址；真实接入时把它交给 mcp 模块建 StreamableHttp
  传输即可，这里保留为纯配置解析、不发起网络。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .orchestration import (
    BaseNode,
    Interceptor,
    NodeRegistry,
    NodeState,
    _safe,
    topological_order,
)
from .tool import Tool, ToolRegistry


# --------------------------------------------------------------------------
# 剪辑节点（mock：仅体现 DAG 契约，真实逻辑属音视频团队）
# --------------------------------------------------------------------------

def _obj(desc: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "object", "description": desc, "properties": props or {}, "required": []}


class SearchMediaNode(BaseNode):
    name = "search_media"
    description = "在素材库中检索素材（用户未上传时使用）"
    input_schema = _obj("检索关键词", {"query": {"type": "string"}})

    async def process(self, state, inputs):
        return {"found": [{"material_id": f"mat-mock{i}", "filename": f"web_clip_{i}.mp4",
                           "kind": "video", "duration": 6.0} for i in range(2)],
                "source": "materials"}


class LoadMediaNode(BaseNode):
    name = "load_media"
    description = "加载素材，得到时长等基础信息与初始 clips（固定步骤）"
    input_schema = _obj("素材 material_id 列表（来自消息附件或 search_media 结果）",
                        {"material_ids": {"type": "array", "items": {"type": "string"}}})

    async def process(self, state, inputs):
        ids = [str(x) for x in (inputs.get("material_ids") or [])] or [
            m["material_id"] for m in (inputs.get("search_media") or {}).get("found", [])
            if isinstance(m, dict) and m.get("material_id")]
        # mock 恒定给三条片段，够下游编排；真实节点按素材时长切
        return {"material_ids": ids,
                "media": [{"id": f"m{i}", "material_id": ids[i] if i < len(ids) else ""}
                          for i in range(3)],
                "clips": [{"id": f"c{i}",
                           "material_id": ids[i] if i < len(ids) else ""} for i in range(3)]}


class SplitShotsNode(BaseNode):
    name = "split_shots"
    description = "按镜头把素材切成片段"
    required_nodes = ["load_media"]

    async def process(self, state, inputs):
        clips = inputs["load_media"]["clips"]
        return {"shots": [f"{c['id']}_s0" for c in clips]}


class AsrNode(BaseNode):
    name = "asr"
    description = "语音识别：把素材声轨转写为带时间戳的文本段"
    required_nodes = ["load_media"]

    async def process(self, state, inputs):
        clips = inputs["load_media"]["clips"]
        # 段 id 与真节点同形（asr-N）：修字闸与覆盖层锚点都按 id 寻址，夹具不给 id 就验不到那条路。
        return {"asr_segments": [{"id": f"asr-{n}", "clip": c["id"], "text": f"asr:{c['id']}"}
                                 for n, c in enumerate(clips)]}


class CorrectTranscriptNode(BaseNode):
    name = "correct_transcript"
    description = "ASR 修字闸：只改文本，时间戳与段 id 不动（产物键与真节点契约一致）"
    required_nodes = ["asr"]
    require_explicit_call = True

    async def process(self, state, inputs):
        # 与真节点同形状（storyline_server/nodes/core_nodes.py CorrectTranscriptNode）：
        # 只换 text，id/clip/时间逐字保留；mock 不复制「可疑段」那套启发式，交空清单并写明。
        segs = inputs["asr"]["asr_segments"]
        corrections = inputs.get("corrections")
        if not corrections:
            raise ValueError("correct_transcript 需要你传入 corrections（[{id, text}]）")
        by_id = {str(s.get("id", "")): s for s in segs}
        fixed: dict[str, str] = {}
        ledger: list[dict] = []
        for c in corrections:
            sid = str(c.get("id", "")).strip()
            if sid not in by_id:
                raise ValueError(f"修字锚点认不出来：{sid!r}")
            if sid in fixed:
                raise ValueError(f"同一个 id 被改了两遍：{sid}")
            text = str(c.get("text", "")).strip()
            if not text:
                raise ValueError(f"{sid} 的 text 是空的")
            fixed[sid] = text
            ledger.append({"id": sid, "clip": by_id[sid].get("clip"),
                           "at": [by_id[sid].get("start"), by_id[sid].get("end")],
                           "before": by_id[sid].get("text", ""), "after": text,
                           "changed_units": 0})
        patched = []
        for s in segs:
            seg = dict(s)
            if str(seg.get("id", "")) in fixed:
                seg["text"] = fixed[str(seg.get("id", ""))]
                seg["corrected"] = True
            patched.append(seg)
        return {"asr_segments": patched, "corrections": ledger, "corrected": len(ledger),
                "unchanged_suspects": [], "note": "mock：不跑可疑段启发式，只验契约形状"}


class SpeechRoughCutNode(BaseNode):
    name = "speech_rough_cut"
    description = "语音粗剪：依据 ASR 文本保留口播内容段，先出一版粗剪骨架"
    required_nodes = ["asr"]

    async def process(self, state, inputs):
        # 与真节点一致：修过字的那一份优先（correct_transcript 不会被自动补齐，靠探 Store）。
        source = inputs["asr"]
        if await state.store.has("correct_transcript"):
            source = await state.store.get("correct_transcript") or source
        segs = source["asr_segments"]
        return {"rough_clips": [s["clip"] for s in segs if s["text"]]}


class UnderstandClipsNode(BaseNode):
    name = "understand_clips"
    description = "为每个片段生成内容描述 captions"
    required_nodes = ["split_shots"]

    async def process(self, state, inputs):
        shots = inputs["split_shots"]["shots"]
        return {"clip_captions": [f"cap:{s}" for s in shots]}


class FilterClipsNode(BaseNode):
    name = "filter_clips"
    description = "按用户要求筛选片段"
    required_nodes = ["understand_clips"]

    async def process(self, state, inputs):
        caps = inputs["understand_clips"]["clip_captions"]
        return {"clips": caps[: max(1, len(caps) - 1)]}  # mock：丢掉最后一条


class GroupClipsNode(BaseNode):
    name = "group_clips"
    description = "对片段排序分组，组织叙事逻辑"
    required_nodes = ["filter_clips"]

    async def process(self, state, inputs):
        clips = inputs["filter_clips"]["clips"]
        return {"groups": [{"group_id": "group_0001", "raw": clips}]}


class GenerateScriptNode(BaseNode):
    name = "generate_script"
    description = "根据分组与推荐脚本模板生成视频文案 group_scripts"
    required_nodes = ["group_clips", "script_template_rec"]

    async def process(self, state, inputs):
        groups = inputs["group_clips"]["groups"]
        template = inputs["script_template_rec"]["templates"][0]["id"]
        return {
            "group_scripts": [
                {"group_id": g["group_id"], "raw_text": f"文案@{g['group_id']}", "template": template}
                for g in groups
            ],
            "title": state.user_request[:12] or "未命名",
        }


class GenerateVoiceoverNode(BaseNode):
    name = "generate_voiceover"
    description = "根据文案生成配音"
    required_nodes = ["generate_script"]

    async def process(self, state, inputs):
        scripts = inputs["generate_script"]["group_scripts"]
        return {"voiceover": [f"tts:{s['group_id']}" for s in scripts]}


class ScriptTemplateRecNode(BaseNode):
    name = "script_template_rec"
    description = "脚本推荐：基于素材理解结果推荐旁白结构模板（起承转合骨架）"
    required_nodes = ["understand_clips"]

    async def process(self, state, inputs):
        caps = inputs["understand_clips"]["clip_captions"]
        return {"templates": [{"id": "tpl_vlog_3act", "matched_captions": len(caps)}]}


class GenerateAITransitionNode(BaseNode):
    name = "generate_ai_transition"
    description = "AI 转场生成：在相邻分组之间生成转场效果片段"
    required_nodes = ["group_clips"]

    async def process(self, state, inputs):
        groups = inputs["group_clips"]["groups"]
        return {"ai_transitions": [f"aitrans_{i}" for i in range(max(0, len(groups) - 1))]}


class TransitionRecNode(BaseNode):
    name = "transition_rec"
    description = "转场推荐：按文案从转场库推荐转场类型（淡入/硬切/甩镜…）"
    required_nodes = ["generate_script"]

    async def process(self, state, inputs):
        scripts = inputs["generate_script"]["group_scripts"]
        return {"transitions": [{"group_id": s["group_id"], "style": "fade"} for s in scripts]}


class TextRecNode(BaseNode):
    name = "text_rec"
    description = "文本推荐：按文案推荐花字/字幕样式"
    required_nodes = ["generate_script"]

    async def process(self, state, inputs):
        scripts = inputs["generate_script"]["group_scripts"]
        return {"text_effects": [{"group_id": s["group_id"], "style": "subtitle_clean"} for s in scripts]}


class SelectBGMNode(BaseNode):
    name = "select_BGM"
    description = "选择合适的背景音乐"
    required_nodes = ["generate_script"]

    async def process(self, state, inputs):
        return {"bgm": "bgm_upbeat_01"}


class PlanTimelineNode(BaseNode):
    name = "plan_timeline"
    description = "基础时间线规划：把片段/文案/配音/BGM 组织成时间线（固定步骤；支持 keep_original_audio 原声混剪）"
    required_nodes = ["speech_rough_cut", "group_clips", "generate_script",
                      "generate_voiceover", "select_BGM"]

    async def process(self, state, inputs):
        return {
            "timeline": {
                "groups": len(inputs["group_clips"]["groups"]),
                "voiceover": len(inputs["generate_voiceover"]["voiceover"]),
                "bgm": inputs["select_BGM"]["bgm"],
            }
        }


class PlanTimelineProNode(BaseNode):
    name = "plan_timeline_pro"
    description = "规划时间轴-专业版：在基础时间线上并入推荐的转场与花字/字幕样式"
    required_nodes = ["speech_rough_cut", "group_clips", "generate_script",
                      "generate_voiceover", "select_BGM", "transition_rec", "text_rec"]

    async def process(self, state, inputs):
        return {
            "timeline_pro": {
                "groups": len(inputs["group_clips"]["groups"]),
                "bgm": inputs["select_BGM"]["bgm"],
                "transitions": inputs["transition_rec"]["transitions"],
                "text_effects": inputs["text_rec"]["text_effects"],
            }
        }


class RenderVideoNode(BaseNode):
    name = "render_video"
    description = "根据时间线渲染成片（固定终点）"
    required_nodes = ["plan_timeline"]

    async def process(self, state, inputs):
        # 与真节点（storyline_server/nodes/core_nodes.py RenderVideoNode）同形状的产物契约：
        # 键集合必须一致，MediaCardHook 才能在「Storyline 连不上、走本地 mock 兜底」这条路上
        # 也捕获到 media_url 并投出可播卡片。但仍 strictly mock：不落盘、不写 MinIO——
        # video 用与会话产物作用域一致的对象键（渲染真发生时该键才承载字节），
        # media_url 用离线占位链（memory:// 形状，与存储层内存替身 presign 同款）。
        artifact = state.artifact_id or "_default"
        object_key = f"renders/{_safe(state.session_id)}/{_safe(artifact)}.mp4"
        title = (await state.store.get("generate_script") or {}).get("title", "未命名")
        return {
            "video": object_key,
            "media_url": f"memory://creation-assets/{object_key}?ttl=3600",
            "duration": 0.0, "width": 0, "height": 0, "title": title,
        }


class PlanTimelineAITransitionNode(BaseNode):
    name = "plan_timeline_ai_transition"
    description = "含 AI 转场的时间线规划：把 AI 生成的转场并入时间线（与 plan_timeline 平行的另一条创作路径）"
    required_nodes = ["speech_rough_cut", "group_clips", "generate_script",
                      "generate_voiceover", "select_BGM", "generate_ai_transition"]

    async def process(self, state, inputs):
        return {
            "timeline_ai": {
                "groups": len(inputs["group_clips"]["groups"]),
                "bgm": inputs["select_BGM"]["bgm"],
                "transitions": inputs["generate_ai_transition"]["ai_transitions"],
            }
        }


class RenderWebNode(BaseNode):
    name = "render_web"
    description = "把网页(HTML/URL)渲染成视频:headless 逐帧截图 + ffmpeg 合成(独立终点节点)"
    required_nodes = []

    async def process(self, state, inputs):
        # 与真节点（storyline_server/nodes/web_nodes.py RenderWebNode）同形状的产物契约:
        # video 对象键 + duration/width/height/title + media_url。strictly mock:
        # 不截帧、不合成——离线只验「模型选了这条独立终点路径后拿到的是成片卡形状」。
        artifact = state.artifact_id or "_default"
        object_key = f"renders/{_safe(state.session_id)}/{_safe(artifact)}/web.mp4"
        title = str(inputs.get("title") or "网页出片")
        return {
            "video": object_key,
            "media_url": f"memory://creation-assets/{object_key}?ttl=3600",
            "duration": 0.0, "width": 0, "height": 0, "title": title,
            "web_render": {"frames": 0, "fps": 0, "planned_sec": 0.0,
                           "source": "url" if not inputs.get("html") else "html"},
        }


class PlanMotionNode(BaseNode):
    name = "plan_motion"
    display_name = "图形科普片分镜"
    description = ("校验并归一分镜 spec，存成本会话的 motion_spec"
                   "（零素材出片的第一站，不渲染）")
    required_nodes: list[str] = []

    async def process(self, state, inputs):
        # 与真节点（storyline_server/nodes/motion_plan.py）同形状：motion_spec 是
        # render_motion_video 的唯一入参来源。strictly mock——不跑校验器与估算，
        # 离线只验「分镜 → 出片」这两步在编排层是按依赖串起来的。
        spec = inputs.get("spec") or {}
        shots = [s for s in (spec.get("shots") or []) if isinstance(s, dict)]
        return {"motion_spec": spec, "shot_count": len(shots),
                "char_count": sum(len(str(s.get("text") or "")) for s in shots),
                "estimated_sec": [0.0, 0.0], "target_warnings": [],
                "plan_table": [{"id": s.get("id"), "card": s.get("card")} for s in shots]}


class RenderMotionVideoNode(BaseNode):
    name = "render_motion_video"
    display_name = "图形科普片出片"
    description = ("零素材出片：分镜 spec 的文案排成版式画面 + TTS 旁白 + 逐词字幕"
                   "（独立终点节点，不依赖剪辑链）")
    required_nodes: list[str] = []

    async def process(self, state, inputs):
        # 与真节点（storyline_server/nodes/motion_nodes.py）同形状的产物契约：
        # strictly mock——不起 Chrome、不跑 TTS，离线只验「模型选了这条独立终点路径
        # 后拿到的是成片卡形状」，以及白名单/编排层按节点名做事时它不掉队。
        artifact = state.artifact_id or "_default"
        object_key = f"renders/{_safe(state.session_id)}/{_safe(artifact)}/motion.mp4"
        spec = inputs.get("spec") or {}
        shots = spec.get("shots") or []
        return {
            "video": object_key,
            "media_url": f"memory://creation-assets/{object_key}?ttl=3600",
            "duration": 0.0, "width": 0, "height": 0, "fps": 0,
            "title": spec.get("title") or "图形科普片",
            "style": spec.get("style"),
            "mix_mode": "voice_only", "frames_total": 0,
            "motion_ledger": [{"id": s.get("id"), "sec": 0.0, "speech_sec": 0.0,
                               "words": 0, "frames": 0, "captured_frames": 0}
                              for s in shots if isinstance(s, dict)],
            "degraded": [],
        }


class PatchMotionVideoNode(BaseNode):
    name = "patch_motion_video"
    display_name = "图形科普片局部改"
    description = ("改一版已出片的图形科普片：按命中表指针改几格 / 整镜改写 / 删镜 / 重排，"
                   "只重烧受影响的那几镜，产出一个新版本（旧版原样留着）")
    # 与真节点同：没有 DAG 上游——要改的东西全在 base_artifact_id 那一版里
    required_nodes: list[str] = []
    require_explicit_call = True

    async def process(self, state, inputs):
        # 与真节点（storyline_server/nodes/motion_nodes.py PatchMotionVideoNode）同形状：
        # strictly mock——不读编辑包、不验指纹、不烧像素，离线只验「局部改也在白名单里、
        # 且不会被拦截器当成 render_motion_video 的下游自动补齐」。
        artifact = state.artifact_id or "_default"
        object_key = f"renders/{_safe(state.session_id)}/{_safe(artifact)}/motion.mp4"
        edits = inputs.get("edits") or []
        return {
            "video": object_key,
            "media_url": f"memory://creation-assets/{object_key}?ttl=3600",
            "duration": 0.0, "width": 0, "height": 0, "fps": 0,
            "title": "图形科普片", "style": None,
            "mix_mode": "voice_only", "frames_total": 0,
            "motion_ledger": [], "degraded": [],
            "patch": {"base_artifact_id": str(inputs.get("base_artifact_id") or ""),
                      "changed_shots": [str(e.get("shot") or "") for e in edits
                                        if isinstance(e, dict)],
                      "removed_shots": list(inputs.get("remove_shots") or []),
                      "reordered": bool(inputs.get("reorder"))},
        }


ALL_NODE_CLASSES = [
    # 输入阶段 → 素材处理层 → 逻辑与脚本层 → 时间轴规划层 → 最终输出
    SearchMediaNode, LoadMediaNode,
    SplitShotsNode, UnderstandClipsNode, FilterClipsNode, GroupClipsNode,
    AsrNode, CorrectTranscriptNode, SpeechRoughCutNode,
    ScriptTemplateRecNode, GenerateScriptNode, GenerateAITransitionNode,
    TransitionRecNode, TextRecNode, GenerateVoiceoverNode, SelectBGMNode,
    PlanTimelineNode, PlanTimelineProNode, PlanTimelineAITransitionNode,
    RenderVideoNode, RenderWebNode, PlanMotionNode, RenderMotionVideoNode,
    PatchMotionVideoNode,
]


def build_node_registry(allowed: list[str] | None = None) -> NodeRegistry:
    """实例化剪辑节点并注册；allowed 为 None 时全注册，否则按白名单（如 TOML available_nodes）。"""
    reg = NodeRegistry()
    name_to_cls = {cls.name: cls for cls in ALL_NODE_CLASSES}
    for nm in (allowed or list(name_to_cls)):
        cls = name_to_cls.get(nm)
        if cls is not None:
            cls(registry=reg)
    return reg


# --------------------------------------------------------------------------
# Node → Tool 适配：LLM 调用即经拦截器按 DAG 补齐依赖
# --------------------------------------------------------------------------


class NodeTool(Tool):
    """把一个剪辑节点暴露成 Agent 工具；执行委托给 Interceptor 以享受依赖补齐。"""

    def __init__(self, node: BaseNode, interceptor: Interceptor, state: NodeState) -> None:
        self._node = node
        self._interceptor = interceptor
        self._state = state

    @property
    def name(self) -> str:
        return self._node.name

    @property
    def display_name(self) -> str:
        return self._node.display_name

    @property
    def description(self) -> str:
        deps = f"（需先完成：{', '.join(self._node.required_nodes)}）" if self._node.required_nodes else ""
        return f"{self._node.description}{deps}"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._node.input_schema or {"type": "object", "properties": {}}

    # 并发契约：节点写自己那份 Store 键、读上游键，调度按读写集拆批。
    @property
    def read_only(self) -> bool:
        return False

    @property
    def reads(self) -> frozenset[str]:
        return self._node.read_keys

    @property
    def writes(self) -> frozenset[str]:
        return self._node.write_keys

    async def execute(self, **kwargs: Any) -> str:
        result = await self._interceptor.invoke(self._node.name, self._state, **kwargs)
        # 只回喂结构化产出的紧凑摘要，避免把整段媒体塞回上下文。
        return json.dumps({"node": self._node.name,
                           "artifact_id": result.get("artifact_id"),
                           "output": result.get("output")}, ensure_ascii=False)


class ReadNodeHistoryTool(Tool):
    """让 LLM 按 key 读取某个前驱节点写入 Store 的产物（文档 CAPABILITY 技能的 read_node_history）。"""

    def __init__(self, state: NodeState) -> None:
        self._state = state

    @property
    def name(self) -> str:
        return "read_node_history"

    @property
    def display_name(self) -> str:
        return "读取节点产物"

    @property
    def description(self) -> str:
        return "按节点名读取该节点已产出的结果（Store 中的数据总线）。用于在生成文案前获取素材理解/分组等前驱结果。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "要读取的前驱节点名，如 understand_clips"}
            },
            "required": ["key"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, key: str) -> str:
        store = self._state.store
        if not await store.has(key):
            return (
                f"Error: Store 中还没有 {key} 的产物。"
                f"已执行节点：{await store.executed()}。请先调用对应节点或其依赖。"
            )
        return json.dumps({"node": key, "payload": await store.get(key)}, ensure_ascii=False)


def build_agent_registry(
    node_registry: NodeRegistry, state: NodeState
) -> tuple[ToolRegistry, Interceptor]:
    """把节点注册为工具，并挂上 read_node_history，返回 (ToolRegistry, Interceptor)。

    interceptor 暴露 order_trace 供审计。
    """
    registry = ToolRegistry()
    interceptor = register_editing_tools(registry, node_registry, state)
    return registry, interceptor


def register_editing_tools(
    registry: ToolRegistry, node_registry: NodeRegistry, state: NodeState
) -> Interceptor:
    """把剪辑节点 + read_node_history 注册进「已有」registry（供顶层装配复用），返回 Interceptor。"""
    interceptor = Interceptor(node_registry)
    for node in node_registry.all():
        registry.register(NodeTool(node, interceptor, state))
    registry.register(ReadNodeHistoryTool(state))
    return interceptor


def load_storyline_config(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as f:  # tomllib 需要二进制模式
        return tomllib.load(f)


def storyline_available_nodes(cfg: dict[str, Any]) -> list[str]:
    return list(cfg.get("local_mcp_server", {}).get("available_nodes", []))


def storyline_server_url(cfg: dict[str, Any]) -> str:
    s = cfg.get("local_mcp_server", {})
    scheme = s.get("url_scheme", "http")
    host = s.get("connect_host", "127.0.0.1")
    port = s.get("port", 8001)
    path = s.get("path", "/mcp")
    return f"{scheme}://{host}:{port}{path}"


def validate_dag(node_registry: NodeRegistry) -> list[str]:
    """返回拓扑执行顺序；存在环或悬空依赖会抛 ValueError。"""
    return topological_order(node_registry)
