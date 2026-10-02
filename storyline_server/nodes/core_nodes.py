"""真实剪辑节点（音视频团队侧实现，方案 A：ffmpeg + MoviePy + faster-whisper + edge-tts + VL）。

与主 Agent 侧 mock 的关系：节点名 / description / required_nodes（DAG 边）/ payload 契约
键与 ``agent_framework/video_editing.py`` 完全一致——主 Agent 端零改动，只是把
「假数据」换成「真处理」。所有阻塞调用（ffmpeg / whisper / MoviePy / 网络 IO）
一律经 ``asyncio.to_thread`` 离开事件循环；外部能力失败走**降级不失败**路径，
保证离线也能整链出片。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from agent_framework.orchestration import (
    BaseNode,
    NodeRegistry,
    NodeState,
    _safe,
)
from agent_framework.storage import Storage, StorageUnavailable, to_ref

from .. import mediaops
from ..mediaops import MediaError
from ..providers import ProviderError, Providers
from ..settings import Settings

# ── MoviePy 浮点边界兜底 ──────────────────────────────────────────
# subclipped(end_time) 在 end_time==clip.duration 时因浮点误差可能判越界。
# monkey-patch Clip.subclipped，把 end_time 钳到 duration - 1e-3，根治所有调用点。
_moviepy_patched = False
def _patch_moviepy_subclip():
    global _moviepy_patched
    if _moviepy_patched:
        return
    try:
        from moviepy.Clip import Clip as _Clip
        _orig = _Clip.subclipped
        def _safe(self, start=0, end=None):
            _dur = getattr(self, "duration", None) or 0
            if end is not None and _dur > 0 and float(end) >= _dur - 1e-6:
                end = _dur - 1e-3
                if float(end) <= float(start):
                    end = float(start) + min(1e-3, max(0.0, _dur - float(start)))
            return _orig(self, start, end)
        _Clip.subclipped = _safe
        _moviepy_patched = True
    except Exception:
        pass

TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "data" / "templates.json"

TRANSITION_STYLES = ["fade", "fadeblack", "dissolve", "wipeleft", "slideup", "circleopen"]
TEXT_STYLES = ["subtitle_clean", "subtitle_bold", "花字_pop", "花字_neon"]


def _obj(desc: str, props: dict[str, Any] | None = None,
         required: list[str] | None = None) -> dict[str, Any]:
    """构造节点入参的 JSON Schema。

    ``required`` 必须与 ``process`` 里真正会 raise 的分支同源：本函数原先恒为 ``[]``，
    于是 21 个节点工具在 LLM 眼里「什么参数都不用传」，而服务端对
    ``load_media.material_ids`` / ``filter_clips.keep_clips`` / ``group_clips.custom_groups``
    / ``script_template_rec.template_id`` / ``transition_rec.custom_transitions``
    / ``text_rec.custom_styles`` 是硬报错的。模型从 schema 看不出必填 → 空参调用 →
    报错 → 多烧一轮重试。本地校验器（``tool.py``）也只查 required，同样拦不住。
    """
    return {"type": "object", "description": desc, "properties": props or {},
            "required": list(required or [])}


def load_templates() -> list[dict[str, Any]]:
    return json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))


def _template_ids() -> list[str]:
    """脚本模板的合法 id 列表——给 JSON Schema 当 enum 源。

    读不到就退成空列表（schema 里等同「无枚举」），绝不因为一个 data 文件缺失
    就让整个节点包 import 失败：那会把整个剪辑服务带下去。
    """
    try:
        return [str(t["id"]) for t in load_templates() if isinstance(t, dict) and t.get("id")]
    except Exception:  # noqa: BLE001 - 缺文件/坏 JSON 只影响枚举提示
        return []


def write_concat_list(path: Path, parts: list[Path]) -> Path:
    """写 ffmpeg concat 的列表文件——路径必须转义，否则整条兜底渲染路径必失败。

    concat 解复用器把 ``file '...'`` 里的反斜杠当**转义字符**：Windows 路径
    ``C:\\Users\\...\\000.mp4`` 会被吞成 ``C:Users...000.mp4``，ffmpeg 报

        Impossible to open 'C:Users...000.mp4'

    真机现场（本项目所在路径 "C:\\Users\\xu'zhi'cheng\\Desktop\\智能创作助手"）：
    ``render_mode=ffmpeg`` 与分段渲染这两条兜底路径 **100% 失败**，而 MoviePy 主路径
    一失败就会提示「改用简化渲染/分段渲染」——等于把用户引到两条死路上。
    报错文本又是天书，排查方向会被带偏到编解码上。

    规则：统一正斜杠（ffmpeg 在 Windows 上照样认），再把单引号按 concat 语法的
    ``'\\''`` 转义。这样纯 ASCII 路径、含空格路径、含单引号路径都能用。
    """
    lines = []
    for p in parts:
        s = str(Path(p).absolute()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{s}'")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class StoryNode(BaseNode):
    """真实节点基类：注入 Settings/Providers/Storage，提供会话工作目录。"""

    def __init__(self, settings: Settings, providers: Providers,
                 storage: Storage, **kw: Any) -> None:
        self.settings = settings
        self.providers = providers
        self.storage = storage
        super().__init__(**kw)

    def _work(self, state: NodeState, *dirs: str) -> Path:
        """本次产物的临时工作目录（只传目录段并全部 mkdir；文件名在调用处拼接）。

        落点是 storage.workspace 根下的 ``{session}/{artifact}``，整目录可随时丢弃：
        字节真相在对象存储里，这里只是渲染期的中转（spec §3.6）。
        """
        return self.storage.workspace.dir_for(state.session_id, state.artifact_id, *dirs)

    async def _local(self, state: NodeState, value: Any, *dirs: str) -> Path:
        """上游产物里的文件引用 → 本机可读文件（ ffmpeg/MoviePy 只吃本地路径）。

        引用是 `obj:对象键`，字节按需在运行时取回，所以持久化产物里没有工作区绝对路径：
        换实例、进程重启、工作区被回收都不影响旧产物重新出片。
        """
        return await self.storage.workspace.localize_ref(
            value, self._work(state, *dirs))

    async def _keep(self, state: NodeState, path: Path, stage: str) -> str:
        """节点自产文件（配音/转场/占位 BGM）→ 对象存储，返回写进 payload 的引用。"""
        return await self.storage.workspace.publish_derived(
            path, session_id=state.session_id, artifact_id=state.artifact_id,
            stage=stage)

    async def _capped(self, aw: Any, timeout: float | None = None) -> Any:
        """外部能力调用统一限时：卡死的第三方必须超时降级，不能冻住整条剪辑链。

        不传 timeout 用 caps.provider_timeout_sec；ASR 等按时长伸缩的调用点显式给值。
        """
        return await asyncio.wait_for(
            aw, timeout=timeout or self.providers.caps.provider_timeout_sec)


# --------------------------------------------------------------------------
# 输入阶段
# --------------------------------------------------------------------------

class SearchMediaNode(StoryNode):
    name = "search_media"
    display_name = "检索素材"
    description = "在素材库中检索素材（用户未上传时使用）"
    input_schema = _obj("检索关键词", {"query": {"type": "string"}})

    async def process(self, state, inputs):
        query = str(inputs.get("query") or state.user_request or "").strip()
        rows = await self.storage.materials.search(
            state.user_id, query, conv_id=state.conversation_id or None,
            kinds=("video",), limit=20)
        return {"found": [{"material_id": r["id"], "filename": r["filename"],
                           "kind": r["kind"], "duration": r["duration_sec"]}
                          for r in rows],
                "source": "materials"}


class LoadMediaNode(StoryNode):
    name = "load_media"
    display_name = "素材入库"
    description = "加载素材，得到时长等基础信息与初始 clips（固定步骤）"
    input_schema = _obj("素材 material_id 列表（来自消息附件或 search_media 结果）",
                        {"material_ids": {"type": "array", "items": {"type": "string"}}},
                        required=["material_ids"])

    async def process(self, state, inputs):
        ids = [str(x) for x in (inputs.get("material_ids")
                                or state.flags.get("material_ids") or [])]
        if not ids:
            src = inputs.get("search_media") or {}
            ids = [str(m["material_id"]) for m in src.get("found", [])
                   if isinstance(m, dict) and m.get("material_id")]
        if not ids:
            raise ValueError("load_media：没有 material_ids（请先 search_media 或随消息附件传入）")
        # LLM 可能从 MinIO 对象路径（mat-xxx-n.mp4）提取带 -n 后缀的 ID，
        # 实际素材 ID 不带 -n（mat-xxx）。统一去掉 -n 后缀避免查不到。
        ids = [m[:-2] if m.endswith("-n") else m for m in ids]
        got, denied = await self.storage.materials.resolve(
            ids[:20], user_id=state.user_id, conv_id=state.conversation_id or None)
        media: list[dict[str, Any]] = []
        clips: list[dict[str, Any]] = []
        unreadable: list[str] = []
        audio_only: list[str] = []
        for row in got:
            ref = to_ref(row["object_key"])
            try:
                local = await self._local(state, ref, "material")
                info = await asyncio.to_thread(mediaops.probe, local)
            except (MediaError, OSError, TimeoutError, StorageUnavailable):
                unreadable.append(row["id"])       # 字节坏了：跳过这条，不毁整链
                continue
            if info["width"] == 0 or info["height"] == 0:
                audio_only.append(row["id"])        # 纯音频（如 BGM mp3）：不进画面轨
                continue
            info["id"] = f"m{len(media)}"
            info["material_id"] = row["id"]
            info["object_key"] = row["object_key"]
            info["filename"] = row["filename"]
            # path 是引用而不是本机路径：payload 会被写进 artifacts 表并随分叉复制，
            # 绝对路径在那时候已经指向别处（或什么都不指向）。
            info["path"] = ref
            media.append(info)
            clips.append({
                "id": info["id"], "material_id": row["id"],
                "object_key": row["object_key"], "filename": row["filename"],
                "path": info["path"],
                "start": 0.0, "end": info["duration"],
                "duration": info["duration"],
                "width": info["width"], "height": info["height"],
                "fps": info["fps"], "has_audio": info["has_audio"],
            })
        if not media:
            raise ValueError(
                f"load_media：{len(ids)} 个 id 没有一个能读出可用视频"
                f"（越权/不存在 {len(denied)} 个，读不出 {len(unreadable)} 个，"
                f"纯音频 {len(audio_only)} 个）")
        out: dict[str, Any] = {"media": media, "clips": clips}
        if denied:
            out["skipped_unauthorized"] = denied   # spec §5 步骤 10：越权只跳过并回报
        if unreadable:
            out["skipped_unreadable"] = unreadable
        if audio_only:
            out["skipped_audio_only"] = audio_only
        return out


# --------------------------------------------------------------------------
# 素材处理层
# --------------------------------------------------------------------------

class SplitShotsNode(StoryNode):
    name = "split_shots"
    display_name = "镜头切分"
    description = "按镜头把素材切成片段"
    required_nodes = ["load_media"]

    async def process(self, state, inputs):
        caps = self.settings.caps
        clips = inputs["load_media"]["clips"]

        async def _split_one(clip):
            src = await self._local(state, clip["path"], "material")
            ranges = await asyncio.to_thread(
                mediaops.scene_ranges, src, caps.scene_threshold,
                caps.min_shot_sec, caps.max_shot_sec, clip["end"] - clip["start"])
            return [{"id": f"{clip['id']}_s{j}", "path": clip["path"],
                     "start": round(clip["start"] + s, 3),
                     "end": round(clip["start"] + e, 3),
                     "duration": round(e - s, 3),
                     "width": clip["width"], "height": clip["height"],
                     "fps": clip["fps"], "has_audio": clip["has_audio"]}
                    for j, (s, e) in enumerate(ranges)]

        results = await asyncio.gather(*(_split_one(c) for c in clips))
        shots: list[dict[str, Any]] = []
        for part in results:
            shots.extend(part)
        return {"clips": shots, "shot_count": len(shots)}


class AsrNode(StoryNode):
    name = "asr"
    display_name = "语音转写"
    description = "语音识别：把素材声轨转写为带时间戳的文本段"
    required_nodes = ["load_media"]

    async def process(self, state, inputs):
        warnings: list[str] = []
        pcaps = self.providers.caps

        async def _process_one(clip):
            if not clip.get("has_audio"):
                return [], []
            wav = self._work(state, "asr") / f"{clip['id']}.wav"
            try:
                src = await self._local(state, clip["path"], "material")
                await asyncio.to_thread(mediaops.extract_audio, src, wav)
            except (MediaError, StorageUnavailable) as e:
                return [], [f"{clip['id']}: 抽音失败 {e}"]
            try:
                audio_dur = float((await asyncio.to_thread(mediaops.probe, wav))["duration"])
            except MediaError:
                audio_dur = 0.0
            # ASR 单次调用的上限必须**夹在 MCP 传输超时之内**，否则服务端还在算、
            # 客户端已经超时放弃：那不只是白跑，还会让上层把它当失败处理。
            # 原先 max(provider_timeout, 60 + 1.5×音频秒) 是无上界的：音频 400s → 660s、
            # 600s → 960s、900s → 1410s，而 [local_mcp_server].timeout = 600s。
            # 于是「素材音频超过约 6 分 40 秒」必然客户端超时（历史上还会被 MCP 客户端
            # 当断线重发一次，整段再跑一遍）。留出 30s 余量给传输与序列化。
            _tool_timeout = float(getattr(self.settings.server, "timeout", 600) or 600)
            asr_ceiling = max(60.0, _tool_timeout - 30.0)
            asr_timeout = min(
                asr_ceiling,
                max(pcaps.provider_timeout_sec,
                    pcaps.asr_timeout_base_sec + audio_dur * pcaps.asr_timeout_per_audio_sec))
            if (pcaps.asr_timeout_base_sec
                    + audio_dur * pcaps.asr_timeout_per_audio_sec) > asr_ceiling:
                warnings.append(
                    f"{clip['id']}: 音频 {audio_dur:.0f}s，ASR 预算已夹到 "
                    f"{asr_timeout:.0f}s（传输上限 {_tool_timeout:.0f}s）；超长音频建议先分段。")
            try:
                segs = await self._capped(
                    asyncio.to_thread(self.providers.transcribe, wav),
                    timeout=asr_timeout)
            except (ProviderError, TimeoutError) as e:
                return [], [f"{clip['id']}: ASR 降级 {type(e).__name__}: {str(e)[:120]}"]
            clip_segs = [{"clip": clip["id"], "text": s["text"],
                          "start": round(clip["start"] + s["start"], 3),
                          "end": round(clip["start"] + s["end"], 3)} for s in segs]
            return clip_segs, []

        clips = inputs["load_media"]["clips"]
        results = await asyncio.gather(*(_process_one(c) for c in clips))
        segments: list[dict[str, Any]] = []
        for segs, warns in results:
            segments.extend(segs)
            warnings.extend(warns)
        return {"asr_segments": segments, "warnings": warnings}


class SpeechRoughCutNode(StoryNode):
    name = "speech_rough_cut"
    display_name = "口播粗剪"
    description = (
        "语音粗剪：依据 ASR 文本保留口播内容段。"
        "推荐做法：先读 asr 结果，由 LLM 分析完整转写文本选出最有价值的段落，"
        "通过 keep_segments 参数传入（start/end/clip）。"
    )
    required_nodes = ["asr"]
    input_schema = _obj("语音粗剪", {
        "target_duration_sec": {
            "type": "number",
            "minimum": 1, "maximum": 600, "unit": "秒",
            "description": "目标成片时长（秒，1~600）；不传则保留全部",
        },
        "highlight": {
            "type": "boolean",
            "description": "true=高光精选模式（需传 keep_segments 指定保留段落）",
        },
        "clip": {
            "type": "string",
            "description": "只从指定 clip（素材编号，如 m2）的 ASR 段中提取；"
                           "不传则对所有素材的 ASR 段统一处理",
        },
        "keep_segments": {
            "type": "array",
            "description": "LLM 选定的段落列表，每项 {start, end, clip, text?}",
            "items": {"type": "object"},
        },
    })

    async def process(self, state, inputs):
        segs = inputs["asr"]["asr_segments"]
        clip_filter = inputs.get("clip")
        if clip_filter:
            segs = [s for s in segs if s.get("clip") == clip_filter]
        # 保留有口播覆盖的区间（相邻间隔 < 0.5s 合并：仅消除 ASR 断句毛刺，
        # 不把自然停顿聚成超长段——高光精选需要碎段作为候选，打分层负责偏好整段话题）
        rough: list[dict[str, Any]] = []
        for s in segs:
            if rough and s["clip"] == rough[-1]["clip"] and s["start"] - rough[-1]["end"] < 0.5:
                rough[-1]["end"] = s["end"]
                rough[-1]["text"] += " " + s["text"]
            else:
                rough.append(dict(s))

        # LLM 选段优先：Agent 读 ASR 转写后自行挑段落，直接用
        keep = inputs.get("keep_segments")
        if keep:
            resolved: list[dict[str, Any]] = []
            for ks in keep:
                seg = dict(ks)
                if "text" not in seg or not seg.get("text"):
                    for r in rough:
                        if r.get("clip") == seg.get("clip") and abs(
                            float(r["start"]) - float(seg["start"])) < 1.0:
                            seg["text"] = r["text"]
                            break
                resolved.append(seg)
            return {"rough_clips": resolved, "kept_sec": round(sum(
                float(r["end"]) - float(r["start"]) for r in resolved), 2),
                "mode": "llm_selected"}

        # 高光精选：需要 LLM 传 keep_segments，不再用权力词打分替用户决策
        if _wants_highlight(state, inputs) and rough:
            raise ValueError(
                "speech_rough_cut 高光精选模式需要你传入 keep_segments 参数。"
                "请通读 ASR 全文后选出最有价值的段落，或向用户提问想保留哪些内容。"
            )
        return {"rough_clips": rough, "kept_sec": round(sum(
            r["end"] - r["start"] for r in rough), 2)}


def _parse_caption_list(raw: str, n: int) -> list[str] | None:
    """把 VL 的批量回答解析成 n 条描述。

    正常形态是 JSON 字符串数组（与帧顺序一一对应）；解析失败或条数不足时，
    把整段文本用作本批每一帧的描述——比降级占位信息量大，也兼容非 JSON 回答。
    """
    try:
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                items = [str(x).strip() for x in arr]
                if len(items) >= n and all(items[:n]):
                    return items[:n]
    except (json.JSONDecodeError, ValueError):
        pass
    text = (raw or "").strip()
    return [text] * n if text else None


_STYLE_TAG_RE = re.compile(r"【风格[：:](.+?)】")


def _extract_style_hints(captions: dict[str, str]) -> str:
    """从各镜 caption 里提取【风格：…】标注，去重合并成一条风格摘要供 LLM 参考。"""
    seen: set[str] = set()
    parts: list[str] = []
    for cap in captions.values():
        for m in _STYLE_TAG_RE.finditer(cap or ""):
            hint = m.group(1).strip()
            if hint and hint not in seen:
                seen.add(hint)
                parts.append(hint)
    return "；".join(parts)


class UnderstandClipsNode(StoryNode):
    name = "understand_clips"
    display_name = "画面理解"
    description = "为每个片段生成内容描述 captions"
    required_nodes = ["split_shots"]

    async def process(self, state, inputs):
        caps = self.settings.caps
        shots = inputs["split_shots"]["clips"]
        warnings: list[str] = []
        # 整轮只解析一次 key（前端配置 → 环境变量 → 回落位）：逐镜再查库等于白加 N 次往返
        vkey, vsrc = await self.providers.resolve_key(state.user_id)

        # 1) 本地抽帧（每镜中点一帧）；并行抽取加速
        frames_dir = self._work(state, "frames")

        async def _extract_one(shot):
            frame = frames_dir / f"{_safe(shot['id'])}.jpg"
            mid = (shot["start"] + shot["end"]) / 2
            try:
                src = await self._local(state, shot["path"], "material")
                await asyncio.to_thread(
                    mediaops.extract_frame, src, mid, frame, caps.proxy_max_side)
                return shot["id"], frame
            except (MediaError, OSError, StorageUnavailable):
                return shot["id"], None

        frame_of = dict(await asyncio.gather(*(_extract_one(s) for s in shots)))

        # 2) 组批发 VL：每批 N 帧一次请求，批数封顶——否则 177 段=177 次带图往返，
        #    600s MCP 超时内跑不完。多批并行发（信号量限并发避免 API 限流）
        batch_n = max(1, int(caps.vl_frames_per_batch))
        max_batches = max(1, int(caps.vl_max_batches))
        batches = [shots[i:i + batch_n] for i in range(0, len(shots), batch_n)]
        if len(batches) > max_batches:
            warnings.append(
                f"镜头数 {len(shots)} 超过批数上限 {max_batches}×{batch_n} 帧，"
                f"后 {len(batches) - max_batches} 批走降级占位")
            batches = batches[:max_batches]

        _vl_prompt = (
            "只输出一个 JSON 字符串数组：第 i 项用一两句中文描述第 i 张画面的"
            "内容、主体与氛围，供短视频文案参考。"
            "如果能看到编辑风格特征（字幕样式/位置、转场效果、色调滤镜、画面节奏），"
            "在描述末尾用【风格：…】标注，"
            "例如「夜景城市航拍，暖色调【风格：快剪约2s一镜，底部白字短句字幕，闪白转场】」。"
            "数组长度必须等于图片数，不要输出解释或代码块标记。")
        # 并发度来自配置（原先硬编码 4）：实测把它提到 8 总耗时近半，
        # 提到 16 快 3.5 倍，而单请求耗时不变——瓶颈在客户端这个数，不在模型侧。
        _vl_sem = asyncio.Semaphore(max(1, int(getattr(caps, "vl_concurrency", 8))))

        async def _vl_batch(batch):
            usable = [(s, frame_of[s["id"]]) for s in batch if frame_of.get(s["id"])]
            if not usable:
                return [], list(s["id"] for s in batch), True
            prompt = f"下面给你 {len(usable)} 张按顺序排列的视频帧。{_vl_prompt}"
            async with _vl_sem:
                try:
                    raw = await asyncio.to_thread(
                        self.providers.vision, [f for _, f in usable], prompt,
                        api_key=vkey, key_source=vsrc)
                    texts = _parse_caption_list(raw, len(usable))
                except (ProviderError, OSError):
                    texts = None
            if texts is None:
                return [], [s["id"] for s, _ in usable], True
            return [(s["id"], t) for (s, _), t in zip(usable, texts)], [], False

        vl_results = await asyncio.gather(*(_vl_batch(b) for b in batches))
        captions: dict[str, str] = {}
        fallback_ids: set[str] = set()
        vl_requests = sum(1 for _, _, failed in vl_results if not failed)
        for pairs, fb_ids, _ in vl_results:
            for sid, t in pairs:
                captions[sid] = t
            fallback_ids.update(fb_ids)

        # 3) 组装：结构字段与逐镜版完全一致，只是 caption 来源变成批量回答
        out: list[dict[str, Any]] = []
        fallback_n = 0
        for shot in shots:
            cap = captions.get(shot["id"])
            if not cap:
                fallback_n += 1
                cap = f"片段{fallback_n}（{shot['duration']:.1f}s，视觉理解降级占位）"
            out.append({"clip": shot["id"], "path": shot["path"],
                        "start": shot["start"], "end": shot["end"],
                        "duration": shot["duration"],
                        "width": shot.get("width", 0), "height": shot.get("height", 0),
                        "fps": shot.get("fps", 0.0),
                        "caption": cap})
        result: dict[str, Any] = {"clip_captions": out, "vl_requests": vl_requests}
        style_hint = _extract_style_hints(captions)
        if style_hint:
            result["style_hint"] = style_hint
        if warnings:
            result["warnings"] = warnings
        return result


class FilterClipsNode(StoryNode):
    name = "filter_clips"
    display_name = "片段筛选"
    description = "按用户要求筛选片段；传 keep_clips（clip ID 列表）直接用 LLM 决策，不传则兜底关键词匹配"
    required_nodes = ["understand_clips"]
    require_explicit_call = True
    input_schema = _obj("片段筛选", {
        "keep_clips": {
            "type": "array", "items": {"type": "string"},
            "description": "LLM 选定的 clip ID 列表（如 [\"m0_s0\",\"m0_s2\"]）；"
                           "传了直接按列表过滤，跳过关键词匹配",
        },
    }, required=["keep_clips"])

    async def process(self, state, inputs):
        caps = inputs["understand_clips"]["clip_captions"]
        keep_clips = inputs.get("keep_clips")
        if not keep_clips:
            raise ValueError(
                "filter_clips 需要你传入 keep_clips 参数（clip ID 列表）。"
                "请先读 understand_clips 的画面描述，按用户要求决定保留哪些片段，"
                "或向用户提问要保留/排除哪些内容。"
            )
        keep_set = set(keep_clips)
        kept = [c for c in caps if c.get("clip") in keep_set]
        return {"clips": kept, "dropped": len(caps) - len(kept),
                "source": "llm_keep_clips"}


class GroupClipsNode(StoryNode):
    name = "group_clips"
    display_name = "片段分组"
    description = "对片段排序分组，组织叙事逻辑；必须传 custom_groups（LLM 决策）"
    required_nodes = ["filter_clips"]
    require_explicit_call = True
    input_schema = _obj("片段分组", {
        "custom_groups": {
            "type": "array",
            "description": "LLM 定义的分组方案，每组含 group_id/clips(clip ID 列表)/summary；"
                           "传了直接用，跳过按素材来源分组",
        },
    }, required=["custom_groups"])

    async def process(self, state, inputs):
        clips = inputs["filter_clips"]["clips"]
        custom = inputs.get("custom_groups")
        if not custom:
            raise ValueError(
                "group_clips 需要你传入 custom_groups 参数（分组方案）。"
                "请按叙事逻辑对片段分组，或向用户提问希望怎么组织内容顺序。"
            )
        clip_map = {c.get("clip"): c for c in clips}
        groups = []
        for g in custom:
            g_clips = [clip_map[cid] for cid in g.get("clips", []) if cid in clip_map]
            if g_clips:
                groups.append({
                    "group_id": g.get("group_id", f"group_{len(groups)+1:04d}"),
                    "clips": g_clips,
                    "duration": round(sum(c["duration"] for c in g_clips), 3),
                    "summary": g.get("summary", ""),
                })
        if not groups:
            raise ValueError("custom_groups 未能匹配到任何片段，请检查 clip ID 是否正确。")
        return {"groups": groups, "source": "llm_custom_groups"}


# --------------------------------------------------------------------------
# 逻辑与脚本层
# --------------------------------------------------------------------------

class ScriptTemplateRecNode(StoryNode):
    name = "script_template_rec"
    display_name = "脚本模板推荐"
    description = "脚本推荐：基于素材理解结果推荐旁白结构模板；传 template_id 直接用 LLM 决策"
    required_nodes = ["understand_clips"]
    require_explicit_call = True
    input_schema = _obj("脚本模板推荐", {
        "template_id": {
            "type": "string",
            # 枚举源必须是**真实存在**的那几个 id：计划门的参数校验要求每个参数
            # 「值能反查」（枚举 / 开关 / 带界数值 / 曲库标签），只给描述不给 enum
            # 会被判「没有可反查的枚举源」而打回——真机实测两次出卡都卡在这里：
            # 模型填的 tpl_vlog_3act 本身是对的，但服务端无从核验，只能拒绝。
            # 模板是固定集合（data/templates.json），直接列进 schema 即可。
            "enum": _template_ids(),
            "description": "LLM 选定的模板 ID，取值见 enum；传了直接用，跳过关键词匹配",
        },
    }, required=["template_id"])

    async def process(self, state, inputs):
        templates = load_templates()
        custom_tpl = inputs.get("template_id")
        if not custom_tpl:
            raise ValueError(
                "script_template_rec 需要你传入 template_id 参数。"
                "请根据内容选择合适的脚本结构模板，或向用户提问希望什么叙事风格。"
            )
        tpl = next((t for t in templates if t["id"] == custom_tpl), None)
        if not tpl:
            raise ValueError(f"template_id- {custom_tpl} 不存在，可选：{[t['id'] for t in templates]}")
        return {"templates": [tpl], "source": "llm_template_id"}


_SCRIPT_PROMPT = """你是短视频文案作者。根据素材分组与脚本模板写旁白文案。
总要求：{style}；整体主题：{request}
模板结构：{structure}
各分组素材画面（按叙事顺序）：
{groups}
只输出 JSON（不要解释、不要代码块标记）：
{{"title": "12字内标题", "groups": [{{"group_id": "…", "raw_text": "该组旁白文案，60-120字口语化"}}]}}"""


def normalize_custom_script(raw_custom: Any) -> str:
    """把模型给的 ``custom_script`` 归一成「每行一句」的干净文本。

    四种输入都要认（模型实际给过这四种）：
      · ``None`` / 空           → 空串（调用方据空串回落到 LLM 生成）
      · 字符串                  → 原样（按空行分段的手写文本）
      · 字符串数组              → 换行拼接
      · **对象数组**            → 取每项的 text/raw_text/sentence

    最后一种曾经出过真机事故：模型传
    ``[{"group": "group_0001", "text": "小白兔…"}]``，而当时用 ``str(x)`` 硬转，
    于是每项变成 Python 字面量串 ``"{'group': 'group_0001', 'text': '小白兔…'}"``
    （单引号 + 花括号），脏字符一路进字幕，用户看到一屏 ``{'group': ...}``，
    渲染反复失败、模型报「文案生成节点坏了」。

    另外做一层兜底：整串文本里若仍残留这种字面量，把 ``text`` 的值抠出来
    （模型也可能把它当**一个字符串**传进来）。

    做成模块级函数而不是写在 ``process`` 里：这样用例可以直接 import 它。
    埋在方法里就只能靠 exec 抠源码来测——那正是「测试与生产各写一份」的来源。
    """
    raw = raw_custom
    if isinstance(raw, Mapping):
        return ""          # 已是最终结构对象，由调用方另走一条路
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for x in raw:
            if isinstance(x, Mapping):
                txt = x.get("text") or x.get("raw_text") or x.get("sentence") or ""
                if isinstance(txt, (list, tuple)):
                    txt = " ".join(str(v) for v in txt)
                txt = str(txt).strip()
                if txt:
                    parts.append(txt)
            else:
                s = str(x).strip()
                if s:
                    parts.append(s)
        text = "\n".join(parts).strip()
    else:
        text = str(raw or "").strip()

    # 兜底：清掉残留的 Python 字面量外壳
    if text and "{" in text and "'" in text and ("'text'" in text or "'group'" in text):
        salvaged: list[str] = []
        for line in text.splitlines():
            found = re.findall(r"['\"]text['\"]\s*:\s*['\"]([^'\"]+)['\"]", line)
            if found:
                salvaged.extend(found)
            elif line.strip() and not line.strip().startswith("{"):
                salvaged.append(line.strip())
        if salvaged:
            text = "\n".join(salvaged).strip()
    return text


class GenerateScriptNode(StoryNode):
    name = "generate_script"
    display_name = "文案生成"
    description = "根据分组与推荐脚本模板生成视频文案 group_scripts"
    required_nodes = ["group_clips", "script_template_rec"]
    input_schema = _obj("文案生成", {"custom_script": {
        "anyOf": [{"type": "string"},
                  {"type": "array", "items": {"type": "string"}},
                  {"type": "object", "additionalProperties": True}],
        "description": "用户指定的成品文案：字符串=按空行分段的手写文本；"
                       "字符串数组=每段一句（会按换行合并后再分段）；"
                       "对象=最终结构 {title, group_scripts:[{group_id, raw_text}]}。不传则由 LLM 生成"}})

    async def process(self, state, inputs):
        groups = inputs["group_clips"]["groups"]
        tpl = inputs["script_template_rec"]["templates"][0]
        raw_custom = inputs.get("custom_script")
        custom_obj: dict[str, Any] | None = None   # LLM 常直接给最终结构对象：{"title", "group_scripts":[…]}
        if isinstance(raw_custom, dict):
            custom_obj = raw_custom
            custom = ""
        else:
            # 归一逻辑抽成模块级 normalize_custom_script（见它的 docstring）：
            # 这样用例能直接 import 测，不必用 exec 抠源码。
            custom = normalize_custom_script(raw_custom)
        parsed: dict[str, Any] | None = None
        if custom_obj is not None:
            parsed = custom_obj
        elif not custom:
            brief = "\n".join(
                f"- {g['group_id']}: {g['summary']}" for g in groups)
            prompt = _SCRIPT_PROMPT.format(
                style=tpl["style"], request=state.user_request or "自由发挥",
                structure=json.dumps(tpl["structure"], ensure_ascii=False),
                groups=brief)
            try:
                raw = await self._capped(self.providers.llm([
                    {"role": "system", "content": "你只输出合法 JSON。"},
                    {"role": "user", "content": prompt}]))
                m = re.search(r"\{.*\}", raw, re.S)
                parsed = json.loads(m.group(0)) if m else None
            except (ProviderError, TimeoutError, json.JSONDecodeError):
                parsed = None
        sentences_map = {
            "tpl_knowledge_talk": 3, "tpl_product_review": 3, "tpl_vlog_3act": 4, "tpl_free": 2,
        }
        scripts = []
        custom_entries = (custom_obj or {}).get("group_scripts") or []
        for i, g in enumerate(groups):
            text = ""
            if custom_entries and i < len(custom_entries):
                entry = custom_entries[i]
                text = (str(entry.get("raw_text") or entry.get("text") or "").strip()
                        if isinstance(entry, dict) else str(entry).strip())
            elif custom:
                parts = [p.strip() for p in re.split(r"\n{2,}|==+", custom) if p.strip()]
                text = parts[i] if i < len(parts) else (parts[-1] if parts else g["summary"])
            elif parsed and i < len(parsed.get("groups", [])):
                text = str(parsed["groups"][i].get("raw_text", "")).strip()
            if not text:
                text = g["summary"].replace("；", "。") + "。这就是这段视频的看点。"
                src = "fallback"
            else:
                src = "custom" if (custom or custom_entries) else "llm"
            sentences = [s.strip() for s in re.split(r"[。！？!?\n]", text) if s.strip()]
            scripts.append({
                "group_id": g["group_id"], "raw_text": text,
                "sentences": sentences, "template": tpl["id"], "source": src,
            })
        title = (parsed or {}).get("title") or (state.user_request[:12] or "未命名")
        return {"group_scripts": scripts, "title": title}


_KEEP_ORIGINAL_AUDIO_PROP = {
    "keep_original_audio": {
        "type": "boolean",
        "description": "true=保留口播原声做混剪（配音环节跳过，字幕用 ASR 原文，空镜画面铺在原声下）",
    },
}

# 时间线规划 + 渲染的公共入参：原声开关 + 高光精选参数
_TIMELINE_PROPS = {
    **_KEEP_ORIGINAL_AUDIO_PROP,
    "target_duration_sec": {
        "type": "number",
        "minimum": 1, "maximum": 600, "unit": "秒",
        "description": "目标成片时长（秒，1~600）；高光精选模式下截到此长度，避免长演讲爆渲染",
    },
    "highlight": {
        "type": "boolean",
        "description": "true=高光精选模式（需 LLM 传 keep_segments 指定保留段落）",
    },
    "speaker_ratio": {
        "type": "number", "minimum": 0, "maximum": 1,
        "description": "原声混剪模式下口播源出镜画面占比（0~1）；如 0.2=出镜20%+空镜80%；"
                       "不传默认 0.2；仅 keep_original_audio=true 时生效",
    },
}


def _wants_original_audio(state, inputs) -> bool:
    flag = inputs.get("keep_original_audio")
    if flag is None:
        flag = state.flags.get("keep_original_audio") if getattr(state, "flags", None) else None
    return bool(flag) if flag is not None else False


def _wants_highlight(state, inputs) -> bool:
    flag = inputs.get("highlight")
    if flag is None:
        flag = state.flags.get("highlight") if getattr(state, "flags", None) else None
    return bool(flag) if flag is not None else False


def _resolve_target_duration(state, inputs, default: float | None) -> float | None:
    """目标时长优先级：显式入参 > state.flags > 默认值。"""
    val = inputs.get("target_duration_sec")
    if val is None:
        val = state.flags.get("target_duration_sec") if getattr(state, "flags", None) else None
    if val is not None:
        return max(3.0, float(val))
    return default


def _resolve_speaker_ratio(inputs) -> float:
    """出镜占比优先级：显式入参 > 默认 0.2。"""
    val = inputs.get("speaker_ratio")
    if val is not None:
        return max(0.0, min(1.0, float(val)))
    return 0.2



async def _original_timeline_or_none(node, state, inputs):
    """原声保真混剪：口播段串联为音轨，空镜画面铺底，字幕取 ASR 原文；不满足条件回 None。"""
    if not _wants_original_audio(state, inputs):
        return None
    groups = inputs["group_clips"]["groups"]
    bgm = inputs["select_BGM"]["bgm"]
    rough = (inputs.get("speech_rough_cut") or {}).get("rough_clips") or []
    if not rough:
        # 素材没有可转写的口播，但用户明确要了原声：换成配音就是违背承诺，
        # 音轨就取每段画面自己的声音。
        return _build_original_from_clips(groups, bgm, node.settings.caps.bgm_volume)
    # 安全网：即使 rough_cut 未精选，也按 target_duration_sec 截断避免爆渲染
    target = _resolve_target_duration(state, inputs, None)
    if target:
        total = sum(float(r["end"]) - float(r["start"]) for r in rough)
        if total > target * 1.2:
            truncated: list[dict[str, Any]] = []
            acc = 0.0
            for r in rough:
                dur = float(r["end"]) - float(r["start"])
                if acc + dur > target * 1.1:
                    break
                truncated.append(r)
                acc += dur
            if truncated:
                rough = truncated
    loaded = inputs.get("load_media") or await state.store.get("load_media") or {}
    media_list = loaded.get("media") or []
    media_by_id = {m.get("id"): m for m in media_list}
    return _build_original_timeline(groups, rough, media_by_id, bgm,
                                    node.settings.caps.bgm_volume,
                                    speaker_ratio=_resolve_speaker_ratio(inputs))


def _build_original_timeline(clips_groups, rough, media_by_id, bgm_path, bgm_volume,
                             *, speaker_ratio=0.2):
    """时间线总长由口播原声驱动；画面按 speaker_ratio 混合出镜与空镜。"""
    audio_events: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    audio_paths: set[str] = set()
    t = 0.0
    for r in rough:
        m = media_by_id.get(r["clip"])
        if m is None:
            continue
        dur = max(0.2, float(r["end"]) - float(r["start"]))
        audio_events.append({"path": m["path"], "start": round(t, 3), "end": round(t + dur, 3),
                             "src_start": r["start"], "src_end": r["end"], "kind": "original"})
        subtitles.append({"text": r.get("text", ""), "start": round(t, 3),
                          "end": round(t + dur - 0.05, 3), "style": "subtitle_clean"})
        t += dur
        audio_paths.add(m["path"])
    total = round(t, 3)
    if total <= 0:
        return None
    # 画面轨：按 speaker_ratio 混合出镜与空镜
    broll = [c for g in clips_groups for c in g["clips"] if c["path"] not in audio_paths]
    if not broll:
        broll = [c for g in clips_groups for c in g["clips"]]
    speaker_clips: list[dict[str, Any]] = []
    for r in rough:
        m = media_by_id.get(r["clip"])
        if m is None:
            continue
        speaker_clips.append({
            "path": m["path"],
            "duration": max(0.2, float(r["end"]) - float(r["start"])),
            "start": float(r["start"]),
        })
    events: list[dict[str, Any]] = []
    t = 0.0
    bi, si = 0, 0
    speaker_dur = 0.0
    while t < total - 0.05 and (broll or speaker_clips):
        use_speaker = bool(speaker_clips) and speaker_dur < speaker_ratio * (t + 0.5)
        if use_speaker:
            c = speaker_clips[si % len(speaker_clips)]
            si += 1
        elif broll:
            c = broll[bi % len(broll)]
            bi += 1
        else:
            break
        seg_end = min(total, t + max(0.5, float(c["duration"])))
        src_s = float(c.get("start", 0.0))
        events.append({"path": c["path"], "start": round(t, 3), "end": round(seg_end, 3),
                       "src_start": round(src_s, 3), "src_end": round(src_s + seg_end - t, 3)})
        if use_speaker:
            speaker_dur += seg_end - t
        t = seg_end
    ref = broll[0] if broll else (speaker_clips[0] if speaker_clips else {})
    return {
        "width": ref.get("width") or 1280, "height": ref.get("height") or 720,
        "fps": ref.get("fps") or 25.0, "duration": total,
        "events": events, "audio_events": audio_events, "subtitles": subtitles,
        "bgm": {"path": bgm_path, "volume": bgm_volume} if bgm_path else None,
        "transition_styles": [], "mode": "original_audio",
    }


def _build_original_from_clips(clips_groups, bgm_path, bgm_volume):
    """没有口播段但显式要原声：音轨取每段画面自己的声音窗口，画面与音频同段对齐。

    字幕留空——没有 ASR 原文就没有「原声」的字幕，拿画面描述凑一条是假动作。
    """
    events: list[dict[str, Any]] = []
    audio_events: list[dict[str, Any]] = []
    t = 0.0
    ref: dict[str, Any] = {}
    for g in clips_groups:
        for c in g["clips"]:
            dur = max(0.2, float(c["duration"]))
            src_start = round(float(c.get("start", 0.0)), 3)
            src_end = round(src_start + dur, 3)
            if not ref:
                ref = c
            events.append({"path": c["path"], "start": round(t, 3), "end": round(t + dur, 3),
                           "src_start": src_start, "src_end": src_end})
            audio_events.append({"path": c["path"], "start": round(t, 3), "end": round(t + dur, 3),
                                 "src_start": src_start, "src_end": src_end, "kind": "original"})
            t += dur
    total = round(t, 3)
    if total <= 0:
        return None
    return {
        "width": ref.get("width") or 1280, "height": ref.get("height") or 720,
        "fps": ref.get("fps") or 25.0, "duration": total,
        "events": events, "audio_events": audio_events, "subtitles": [],
        "bgm": {"path": bgm_path, "volume": bgm_volume} if bgm_path else None,
        "transition_styles": [], "mode": "original_audio",
    }


class GenerateVoiceoverNode(StoryNode):
    name = "generate_voiceover"
    display_name = "口播配音"
    description = "根据文案生成配音（原声混剪模式下自动跳过）"
    required_nodes = ["generate_script"]
    input_schema = _obj("配音生成", _KEEP_ORIGINAL_AUDIO_PROP)

    async def process(self, state, inputs):
        # 原声模式：音轨由素材原声承担，不合成 TTS（有无口播段都一样）
        if _wants_original_audio(state, inputs):
            return {"voiceover": [], "mode": "original_audio"}
        out = []
        for s in inputs["generate_script"]["group_scripts"]:
            dst = self._work(state, "voiceover") / f"{_safe(s['group_id'])}.mp3"
            try:
                await self._capped(self.providers.tts(s["raw_text"], dst))
                src = "edge-tts"
            except (ProviderError, TimeoutError):
                dst = self._work(state, "voiceover") / f"{_safe(s['group_id'])}-fb.wav"
                await asyncio.to_thread(mediaops.silent_wav, dst,
                                        max(2.0, len(s["raw_text"]) * 0.22))
                src = "silent_fallback"
            try:
                info = await asyncio.to_thread(mediaops.probe, dst)
                dur = float(info["duration"])
            except MediaError:
                dur = max(2.0, len(s["raw_text"]) * 0.22)
            # 自产的字节只有进了对象存储才谈得上「产物」：工作区随时会被回收
            ref = await self._keep(state, dst, "voiceover")
            out.append({"group_id": s["group_id"], "path": ref,
                        "duration": round(dur, 3), "source": src})
        return {"voiceover": out}


class GenerateAITransitionNode(StoryNode):
    name = "generate_ai_transition"
    display_name = "AI转场生成"
    description = "AI 转场生成：在相邻分组之间生成转场效果片段"
    required_nodes = ["group_clips"]

    async def process(self, state, inputs):
        caps = self.settings.caps
        groups = inputs["group_clips"]["groups"]
        transitions = []
        style = random.choice(TRANSITION_STYLES)
        for a, b in zip(groups, groups[1:]):
            tail, head = a["clips"][-1], b["clips"][0]
            dst = self._work(state, "ai_transitions") / \
                f"{_safe(a['group_id'])}_{_safe(b['group_id'])}.mp4"
            try:
                tail_src = await self._local(state, tail["path"], "material")
                head_src = await self._local(state, head["path"], "material")
                await asyncio.to_thread(
                    mediaops.xfade_clip, tail_src, head_src,
                    max(tail["start"], tail["end"] - caps.transition_sec),
                    head["start"], dst, style, caps.transition_sec,
                    (head["width"] or 1280, head["height"] or 720))
                transitions.append({"path": await self._keep(state, dst, "ai_transitions"),
                                    "style": style,
                                    "between": [a["group_id"], b["group_id"]],
                                    "duration": caps.transition_sec})
            except Exception:
                continue  # 转场缺失不阻断主链路
        return {"ai_transitions": transitions}


class TransitionRecNode(StoryNode):
    name = "transition_rec"
    display_name = "转场推荐"
    description = "转场推荐；必须传 custom_transitions（LLM 决策）"
    required_nodes = ["generate_script"]
    require_explicit_call = True
    input_schema = _obj("转场推荐", {
        "custom_transitions": {
            "type": "array",
            "description": "LLM 选定的转场样式列表，每项含 group_id/style；传了直接用",
        },
    }, required=["custom_transitions"])

    async def process(self, state, inputs):
        scripts = inputs["generate_script"]["group_scripts"]
        custom = inputs.get("custom_transitions")
        if not custom:
            raise ValueError(
                "transition_rec 需要你传入 custom_transitions 参数。"
                "请根据每组内容氛围选择转场样式，或向用户提问希望什么转场风格。"
            )
        return {"transitions": custom, "source": "llm_custom_transitions"}


class TextRecNode(StoryNode):
    name = "text_rec"
    display_name = "花字推荐"
    description = "文本推荐；必须传 custom_styles（LLM 决策）"
    required_nodes = ["generate_script"]
    require_explicit_call = True
    input_schema = _obj("花字推荐", {
        "custom_styles": {
            "type": "array",
            "description": "LLM 选定的字幕样式列表，每项含 group_id/style；传了直接用",
        },
    }, required=["custom_styles"])

    async def process(self, state, inputs):
        scripts = inputs["generate_script"]["group_scripts"]
        custom = inputs.get("custom_styles")
        if not custom:
            raise ValueError(
                "text_rec 需要你传入 custom_styles 参数。"
                "请根据每组内容选择字幕样式，或向用户提问希望什么字幕风格。"
            )
        return {"text_effects": custom, "source": "llm_custom_styles"}


class SelectBGMNode(StoryNode):
    name = "select_BGM"
    display_name = "背景音乐选择"
    description = "选择合适的背景音乐（可显式传 query 指定歌名，如 query='邓紫棋 Someday I'll Fly'）"
    required_nodes = ["generate_script"]
    input_schema = _obj("选配乐", {"query": {"type": "string", "description": "歌曲名/关键词，优先于 user_request"}})

    async def process(self, state, inputs):
        conv = state.conversation_id or None
        # 显式 query 优先于整段 user_request——LLM 传歌名不再被整句话稀释
        req = str(inputs.get("query") or "").strip() or (state.user_request or "")
        pool = await self.storage.materials.search(
            state.user_id, req, conv_id=conv, kinds=("audio",), origin="bgm", limit=5)
        if not pool:      # 没建曲库：用户上传的音频里挑一条最贴题的当 BGM
            pool = await self.storage.materials.search(
                state.user_id, req, conv_id=conv, kinds=("audio",), limit=5)
        if pool:
            row = pool[0]
            # 素材本来就在对象存储里：这里没必要先下载一份本机副本给渲染层再传回去
            return {"bgm": to_ref(row["object_key"]), "material_id": row["id"],
                    "source": "materials", "filename": row["filename"]}
        # 无曲库：生成轻量占位音轨而非失败（渲染层音量已压低）
        dst = self._work(state, "bgm") / "placeholder_bgm.wav"
        await asyncio.to_thread(mediaops.tone_wav, dst, 12.0, 196)
        return {"bgm": await self._keep(state, dst, "bgm"),
                "source": "generated_placeholder"}


# --------------------------------------------------------------------------
# 时间轴规划层
# --------------------------------------------------------------------------

def _speech_paths_from_inputs(inputs) -> set[str]:
    """从 speech_rough_cut + load_media 提取有 ASR 内容的素材路径（用于画面轨排除）。"""
    rough = (inputs.get("speech_rough_cut") or {}).get("rough_clips") or []
    if not rough:
        return set()
    loaded = inputs.get("load_media") or {}
    media_by_id = {m.get("id"): m for m in (loaded.get("media") or [])}
    return {m["path"] for r in rough
            if (m := media_by_id.get(r["clip"])) is not None}


def _build_timeline(clips_groups, scripts, voiceover, bgm_path, bgm_volume,
                    *, transitions=None, text_effects=None, transition_files=None,
                    exclude_paths=None):
    """把分组片段/文案/配音/BGM 排成多轨时间线（render 侧唯一依赖的结构）。

    exclude_paths：口播源素材路径集合——画面轨中这些片段用空镜替换，
    保持组结构与配音/字幕对齐不变（TTS 模式下也不露演讲人脸）。
    """
    vo_by_group = {v["group_id"]: v for v in voiceover}
    sc_by_group = {s["group_id"]: s for s in scripts}
    tr_style = {t["group_id"]: t["style"] for t in (transitions or [])}
    tx_style = {t["group_id"]: t["style"] for t in (text_effects or [])}
    exclude_set = set(exclude_paths or [])
    # 空镜池：用于替换画面轨中的口播源片段
    pool = [c for g in clips_groups for c in g["clips"] if c["path"] not in exclude_set]
    pool_i = 0

    events: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    audio_events: list[dict[str, Any]] = []
    t = 0.0
    ref = next((c for g in clips_groups for c in g["clips"]
                if not exclude_set or c["path"] not in exclude_set), None) \
        or next((c for g in clips_groups for c in g["clips"]), None)
    for gi, g in enumerate(clips_groups):
        g_start = t
        for c in g["clips"]:
            # 画面轨：口播源片段用空镜替换，保持时长与组结构不变
            vis = c
            if exclude_set and c["path"] in exclude_set and pool:
                vis = pool[pool_i % len(pool)]
                pool_i += 1
            vis_dur = min(c["duration"], vis["duration"])
            events.append({"path": vis["path"], "start": round(t, 3),
                           "end": round(t + c["duration"], 3),
                           "src_start": vis["start"],
                           "src_end": round(vis["start"] + vis_dur, 3)})
            t += c["duration"]
        vo = vo_by_group.get(g["group_id"])
        sc = sc_by_group.get(g["group_id"])
        if vo:
            # 音轨事件必须自带 duration：渲染侧（ffmpeg 兜底路径）按
            # src_end → end → src_start+duration 推这一段该取多长，缺了就退成 0、
            # 抽出一段静音（整条兜底路径出无声片）。写清楚，别让下游去猜。
            audio_events.append({"path": vo["path"], "start": round(g_start, 3),
                                 "duration": float(vo.get("duration") or 0.0),
                                 "group_id": g["group_id"]})
            # 字幕：按句子均摊到该组（视频与配音取较长者）
            span = max(t - g_start, vo["duration"])
            sents = (sc or {}).get("sentences") or []
            if sents:
                per = span / len(sents)
                for si, sent in enumerate(sents):
                    subtitles.append({"text": sent,
                                      "start": round(g_start + si * per, 3),
                                      "end": round(g_start + (si + 1) * per - 0.05, 3),
                                      "style": tx_style.get(g["group_id"], "subtitle_clean")})
            t = max(t, g_start + vo["duration"])
            # 配音比画面长时，必须把画面**铺满**到配音结束为止。
            # 原先只把时间指针 t 推到配音末尾、不给画面补事件，于是画面轨比音轨短：
            # 成片尾段画面定格在最后一帧、音轨被 -shortest 截断（实测 6.0s 配音配
            # 1.5s 画面 → 成片 format 6.0s，但音轨只有 1.499s）。
            # 真实场景几乎必触发：60~120 字旁白 ≈15-30s，而 LLM 常只分到两三个短镜头。
            # 用空镜池继续铺（池取自本组之外的同批素材；没有池就循环本组画面），
            # 保持既有事件形状（path/src_start/src_end），渲染侧无需改。
            gap = round(t - g_start - sum(c["duration"] for c in g["clips"]), 3)
            fillers = [c for c in pool if c["path"] not in exclude_set] or \
                      [c for c in g["clips"]]
            # 从素材**可用的源区间**里循环取（不能顺着往后读：源片只有 3s 时，
            # 读到 3s 之后 ffmpeg 会切出空片段，整条画面轨就塌了）。
            pool_cursor = 0.0
            guard = 0
            while gap > 0.04 and fillers and guard < 256:
                guard += 1
                f = fillers[(gi + guard) % len(fillers)]
                avail = max(0.04, float(f["duration"]))
                take = min(avail, gap)
                # 在本素材内部循环：从 pool_cursor 起取 take，越界就回到起点重算。
                src_off = pool_cursor
                if src_off + take > avail:
                    src_off = 0.0
                src_start = float(f.get("start", 0.0)) + src_off
                events.append({"path": f["path"], "start": round(t - gap, 3),
                               "end": round(t - gap + take, 3),
                               "src_start": round(src_start, 3),
                               "src_end": round(src_start + take, 3),
                               "kind": "vo_fill"})
                pool_cursor = src_off + take
                if pool_cursor >= avail - 0.001:
                    pool_cursor = 0.0
                gap = round(gap - take, 3)
        # AI 转场片段插在组间
        if transition_files and gi < len(transition_files):
            tf = transition_files[gi]
            events.append({"path": tf["path"], "start": round(t, 3),
                           "end": round(t + tf["duration"], 3),
                           "src_start": 0.0, "src_end": tf["duration"],
                           "kind": "transition"})
            t += tf["duration"]
    return {
        "width": (ref or {}).get("width") or 1280,
        "height": (ref or {}).get("height") or 720,
        "fps": (ref or {}).get("fps") or 25.0,
        "duration": round(t, 3),
        "events": events,
        "audio_events": audio_events,
        "subtitles": subtitles,
        "bgm": {"path": bgm_path, "volume": bgm_volume} if bgm_path else None,
        "transition_styles": [tr_style.get(g["group_id"]) for g in clips_groups],
    }


def _trim_timeline(tl: dict[str, Any], target: float | None
                   ) -> tuple[dict[str, Any], str | None]:
    """把时间线硬裁到目标秒数：计划卡承诺的秒数要落在画面上，不是只落在选段偏好里。

    必须裁事件本体而不是只改 duration——ffmpeg 兜底路径不看 duration，成片长度就是各
    事件 src 窗口之和（``_render_via_ffmpeg``）。没给目标或本就不超则原样返回、说明为 None。
    """
    if not target or float(tl.get("duration") or 0) <= target:
        return tl, None
    limit = float(target)

    def _clip(item: dict[str, Any]) -> dict[str, Any] | None:
        start = float(item["start"])
        if start >= limit - 0.02:
            return None
        out = dict(item)
        if "end" in item:
            end = min(float(item["end"]), limit)
            out["end"] = round(end, 3)
        else:
            end = limit
        if item.get("src_start") is not None and item.get("src_end") is not None:
            src_start = float(item["src_start"])
            out["src_end"] = round(min(float(item["src_end"]),
                                       src_start + max(0.05, end - start)), 3)
        return out

    events = [e for e in (_clip(x) for x in tl.get("events", [])) if e]
    audio = [a for a in (_clip(x) for x in tl.get("audio_events", [])) if a]
    subs = [s for s in (_clip(x) for x in tl.get("subtitles", []))
            if s and s["end"] > s["start"]]
    total = round(max([float(e["end"]) for e in events]) if events else limit, 3)
    note = f"已按目标时长裁到 {total}s（原 {tl.get('duration')}s）"
    return {**tl, "duration": total, "events": events,
            "audio_events": audio, "subtitles": subs}, note


def _plan_output(state, inputs, tl: dict[str, Any], key: str) -> dict[str, Any]:
    """三个 plan 节点共同的收尾：目标时长落地 + 裁过就在输出里说明。"""
    tl, note = _trim_timeline(tl, _resolve_target_duration(state, inputs, None))
    out = {key: tl}
    if note:
        out["notes"] = [note]
    return out


class PlanTimelineNode(StoryNode):
    name = "plan_timeline"
    display_name = "时间线编排"
    description = "基础时间线规划：把片段/文案/配音/BGM 组织成时间线（固定步骤；支持 keep_original_audio 原声混剪）"
    required_nodes = ["speech_rough_cut", "group_clips", "generate_script",
                      "generate_voiceover", "select_BGM"]
    input_schema = _obj("时间线规划", _TIMELINE_PROPS)

    async def process(self, state, inputs):
        tl = await _original_timeline_or_none(self, state, inputs)
        if tl is None:
            tl = _build_timeline(
                inputs["group_clips"]["groups"],
                inputs["generate_script"]["group_scripts"],
                inputs["generate_voiceover"]["voiceover"],
                inputs["select_BGM"]["bgm"], self.settings.caps.bgm_volume,
                exclude_paths=_speech_paths_from_inputs(inputs))
        return _plan_output(state, inputs, tl, "timeline")


class PlanTimelineProNode(StoryNode):
    name = "plan_timeline_pro"
    display_name = "时间线编排·专业版"
    description = "规划时间轴-专业版：在基础时间线上并入推荐的转场与花字/字幕样式（同样支持原声混剪）"
    required_nodes = ["speech_rough_cut", "group_clips", "generate_script",
                      "generate_voiceover", "select_BGM", "transition_rec", "text_rec"]
    input_schema = _obj("时间线规划（专业版）", _TIMELINE_PROPS)

    async def process(self, state, inputs):
        tl = await _original_timeline_or_none(self, state, inputs)
        if tl is None:
            tl = _build_timeline(
                inputs["group_clips"]["groups"],
                inputs["generate_script"]["group_scripts"],
                inputs["generate_voiceover"]["voiceover"],
                inputs["select_BGM"]["bgm"], self.settings.caps.bgm_volume,
                transitions=inputs["transition_rec"]["transitions"],
                text_effects=inputs["text_rec"]["text_effects"],
                exclude_paths=_speech_paths_from_inputs(inputs))
        return _plan_output(state, inputs, tl, "timeline_pro")


class PlanTimelineAITransitionNode(StoryNode):
    name = "plan_timeline_ai_transition"
    display_name = "时间线编排·AI转场"
    description = "含 AI 转场的时间线规划：把 AI 生成的转场并入时间线（与 plan_timeline 平行的另一条创作路径；同样支持原声混剪）"
    required_nodes = ["speech_rough_cut", "group_clips", "generate_script",
                      "generate_voiceover", "select_BGM", "generate_ai_transition"]
    input_schema = _obj("时间线规划（AI 转场）", _TIMELINE_PROPS)

    async def process(self, state, inputs):
        tl = await _original_timeline_or_none(self, state, inputs)
        if tl is None:
            tl = _build_timeline(
                inputs["group_clips"]["groups"],
                inputs["generate_script"]["group_scripts"],
                inputs["generate_voiceover"]["voiceover"],
                inputs["select_BGM"]["bgm"], self.settings.caps.bgm_volume,
                transition_files=inputs["generate_ai_transition"]["ai_transitions"],
                exclude_paths=_speech_paths_from_inputs(inputs))
        return _plan_output(state, inputs, tl, "timeline_ai")


# --------------------------------------------------------------------------
# 最终输出
# --------------------------------------------------------------------------

async def _localize_timeline(tl: dict[str, Any], workspace, dst_dir: Path) -> dict[str, Any]:
    """时间线里的文件引用 → 本机可读路径的**渲染期副本**。

    只有这一刻需要本机路径：持久化的时间线一律存 `obj:` 引用，所以同一份产物在别的
    实例、进程重启之后、或工作区被回收之后仍能重新出片。副本只交给渲染函数，
    从不写回 artifacts。
    """
    resolved: dict[str, str] = {}

    async def _p(value: Any) -> str:
        ref = str(value)
        if ref not in resolved:
            resolved[ref] = str(await workspace.localize_ref(ref, dst_dir))
        return resolved[ref]

    out = {**tl,
           "events": [{**ev, "path": await _p(ev["path"])} for ev in tl.get("events", [])],
           "audio_events": [{**ae, "path": await _p(ae["path"])}
                            for ae in tl.get("audio_events", [])]}
    if (tl.get("bgm") or {}).get("path"):
        out["bgm"] = {**tl["bgm"], "path": await _p(tl["bgm"]["path"])}
    return out


_REF_LOOKALIKE = re.compile(r"(obj:)?([A-Za-z0-9_\-./]*mat-[a-z0-9]+[A-Za-z0-9_\-./]*)")


def _object_keys_in(tl: Any) -> list[str]:
    """把时间线里所有**指向对象存储的引用**抠出来（裸键与 ``obj:`` 前缀两种都认）。"""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            s = node
            if s.startswith("obj:"):
                out.append(s[4:])
            elif "/" in s and ("mat-" in s or s.startswith(("renders/", "derived/"))):
                out.append(s)

    walk(tl)
    return out


async def validate_timeline_objects(timeline: Any, storage: Any) -> None:
    """渲染前校验时间线里每个对象键**真的存在**，不存在就**立刻**报错。

    为什么要这一道（真机事故）：模型可以自己手写 ``timeline`` JSON（原声混剪时它常这么
    做，好把出镜画面钉到对应句子的原镜头时间码上）。而 ``render_video`` 原先对手写的
    timeline **逐字使用、不校验**——于是键写错要等到渲染器深处才炸：

        对象不存在：users/u-d9a478c9a6d7/convs/d2ef8d12…/mat-0fe987.mp3

    BGM 其实来自**音乐库**（另一个 owner / 另一个会话），真键是
    ``users/u-bgm-library/convs/c-bgm-library/mat-0fe987.mp3``；模型照抄了另外两条
    同会话素材的样式，把 BGM 也套上了当前会话前缀。渲染跑了十分钟才失败，
    而且报错只说"对象不存在"，不说"你要的其实是哪个键"。

    所以这里：一次 HEAD 就能拦下，并把**正确的键**一并告诉模型（按素材 id 反查
    materials 表），让它一次改对而不是猜。
    """
    if not timeline or storage is None:
        return
    keys = _object_keys_in(timeline)
    if not keys:
        return
    objects = getattr(storage, "objects", None)
    if objects is None or not hasattr(objects, "head"):
        return
    bad: list[tuple[str, str]] = []
    for key in dict.fromkeys(keys):          # 去重，保持顺序
        try:
            if await objects.head(key) is not None:
                continue
        except Exception:  # noqa: BLE001 - 探测失败不当作"不存在"，交给渲染阶段
            continue
        # 反查正确的键：键里通常带 mat-xxxxxx 这个素材 id
        hint = ""
        m = re.search(r"(mat-[a-z0-9]+)", key)
        if m:
            try:
                row = await storage.materials.get(m.group(1))
            except Exception:  # noqa: BLE001
                row = None
            real = str((row or {}).get("object_key") or "")
            if real and real != key:
                hint = f"（素材 {m.group(1)} 的真实对象键是：{real}）"
        bad.append((key, hint))
    if bad:
        lines = [f"  · {k}{h}" for k, h in bad]
        raise ValueError(
            "时间线里有对象键**不存在于对象存储**，渲染会失败，先改掉再提交：\n"
            + "\n".join(lines)
            + "\n请用上面的真实键（素材入库/曲库返回的就是它），不要按当前会话前缀拼。")


class RenderVideoNode(StoryNode):
    name = "render_video"
    display_name = "成片渲染"
    description = ("根据时间线渲染成片（固定终点）。可直接传 timeline 参数跳过规划工具，自行控制画面/字幕/转场。"
                   "渲染常在分钟级：本工具先内联等一小段时间，短片直接回成片；"
                   "返回 status=queued/running 时说明仍在跑，须继续调用 render_status"
                   "（同 artifact_id）直到 done/failed 再收尾，不要提前结束")
    required_nodes = ["plan_timeline"]
    # media_url 是一条一小时后就读不通的 presigned 直链：只回给调用方，不落进共享库
    # （读侧一律按 video 对象键现签——见 Storage.render_view 与 media_replay）
    ephemeral = ("media_url",)
    input_schema = _obj("渲染成片", {
        **_TIMELINE_PROPS,
        "timeline": {
            "type": "object",
            "description": "自定义时间线 JSON（含 events/audio_events/subtitles/bgm 等）；"
                           "传入后直接用这份时间线渲染，跳过 plan_timeline* 自动生成。"
                           "用于精确控制画面穿插、字幕、转场等自动规划工具搞不定的需求",
        },
        "wait_sec": {
            "type": "number",
            "description": "本次调用内联等待渲染完成的秒数（缺省用服务端 [capabilities]."
                           "render_grace_sec；上限 render_wait_max_sec）。传 0 表示只提交不等。",
        },
        "render_mode": {
            "type": "string",
            "description": "渲染路径：auto（默认，先 MoviePy 失败再问用户）/ ffmpeg（直接用 ffmpeg 兜底渲染）/"
                           "low_res（降分辨率重试 MoviePy）。auto 模式下 MoviePy 失败会返回可选方案让用户选，"
                           "不会静默兜底。",
        },
    })

    async def process(self, state, inputs):
        store = state.store
        # 自定义时间线优先：agent 手写的时间线直接用，绕过规划工具
        tl = inputs.get("timeline")
        if not tl:
            # 终止路径三选一：优先专业版/AI 转场版，回退基础版
            for key, field_name in (("plan_timeline_ai_transition", "timeline_ai"),
                                    ("plan_timeline_pro", "timeline_pro"),
                                    ("plan_timeline", "timeline")):
                payload = await store.get(key) or {}
                tl = payload.get(field_name)
                if tl:
                    break
        if not tl:
            raise ValueError("render_video：没有 timeline 参数，Store 中也没有任何时间线产物")
        title = (await store.get("generate_script") or {}).get("title", "未命名")

        # 空产物名的归一必须与 artifacts 表、workspace 目录一致（都是 '_default'）：
        # 同一次运行在 render_jobs 与 artifacts 里要能用同一个 artifact_id 对上。
        artifact = state.artifact_id or "_default"
        sess = state.session_id
        jobs = self.storage.render_jobs
        # open 回带**本次尝试的令牌**：后面所有写（进度/成功/失败）都带上它，
        # 令牌一旦被换掉（判死、重开），本次的迟到写就自动作废。
        row = await jobs.open(sess, artifact)
        attempt = str((row or {}).get("attempt") or "")
        # **开启任务之后**再校验对象键是否真的存在。
        #
        # 为什么放在 open 之后而不是之前：渲染失败必须留下一条 failed 的渲染任务
        # （界面与 render_status 靠它报告"这一步没成"）。若在 open 之前抛，任务压根没
        # 建立，失败就"消失"了——tests/test_render_pipeline 正是钉这条保证。
        #
        # 为什么还是要校验：模型可以自己手写 timeline（原声混剪时常这么做），
        # 而键写错原先要等渲染器深处才炸——真机实测跑了十分钟才失败，报错只说
        # 「对象不存在：…/mat-0fe987.mp3」，不说正确的是哪个键。
        # 现在一次 HEAD 就拦下，并把**真实的对象键**告诉它（见 validate_timeline_objects）。
        try:
            await validate_timeline_objects(tl, self.storage)
        except ValueError as exc:
            await jobs.fail(sess, artifact, str(exc), attempt=attempt)
            raise
        # 成片也落在会话工作区：MoviePy 的 temp_audio 与兜底分片因此同在工作区内（spec §6
        # 的三处媒体面污染），被引用的字节也取在这一层（render/src），失败一并回收。
        # 成功路径保留该目录——时间线里的配音/转场仍能按引用重取，但真要说得上
        # 「可复用」的是对象存储里的字节，不是这个目录。
        dst = self._work(state, "render") / f"{_safe(artifact)}.mp4"
        probe = _JobProgress(jobs, asyncio.get_running_loop(), sess, artifact, attempt)
        render_mode = (inputs.get("render_mode") or
                       (state.flags.get("render_mode") if getattr(state, "flags", None) else None) or
                       "auto")
        done = False
        try:
            tl = await _localize_timeline(tl, self.storage.workspace,
                                          self._work(state, "render", "src"))
            moviepy_error = None
            if render_mode == "ffmpeg":
                probe("ffmpeg_fallback", 60)
                await asyncio.to_thread(_render_via_ffmpeg, tl, dst, probe)
            elif render_mode == "low_res":
                tl_low = {**tl, "width": (tl.get("width") or 1280) // 2,
                          "height": (tl.get("height") or 720) // 2}
                await asyncio.to_thread(_render_with_moviepy, tl_low, dst, self.settings, probe)
            elif render_mode == "segmented":
                await asyncio.to_thread(_render_segmented, tl, dst, self.settings, probe)
            else:
                try:
                    await asyncio.to_thread(_render_with_moviepy, tl, dst, self.settings, probe)
                except Exception as e:
                    moviepy_error = e
            if moviepy_error is not None:
                # 失败也把**回退方案**写进 render_jobs.result：渲染走的是「提交 + 轮询」，
                # 模型的视图是从这一行拼出来的，只写 error 的话选项就到不了模型
                # （真机实测：节点返回值里有完整 options，模型只看到一句错误，
                #  "失败就给可选方案"这条设计在真实路径上永远走不到）。
                fallback = {
                    "__fallback_options__": True,
                    "error": f"MoviePy 渲染失败：{str(moviepy_error)[:200]}",
                    "options": [
                        {"key": "ffmpeg", "label": "简化渲染（ffmpeg 直出）",
                         "description": "用 ffmpeg 直接切片拼接 + 混音，不做转场/字幕，保证出片"},
                        {"key": "low_res", "label": "降分辨率重试",
                         "description": "把时间线缩到一半分辨率再用 MoviePy 渲染，内存占用减半"},
                        {"key": "segmented", "label": "分段渲染",
                         "description": "把时间线按 60 秒切段分别渲染再拼接，每段内存小"},
                    ],
                    "hint": "请在计划卡中选择一个方案，系统会按你的选择重新渲染。",
                }
                await jobs.fail(sess, artifact, f"MoviePy 失败: {str(moviepy_error)[:300]}",
                                attempt or None, result=fallback)
                return fallback
            info = await asyncio.to_thread(mediaops.probe, dst)
            object_key = f"renders/{_safe(sess)}/{_safe(artifact)}.mp4"
            await self.storage.workspace.publish(
                dst, object_key, content_type="video/mp4")
            await probe.drain()          # 迟到进度不能盖掉 done/100
            # 终态产物整份入库（不含会过期的 media_url）：提交 + 轮询的轮询端据此在
            # 任意实例、进程重启之后重建出与阻塞渲染同形状的结果。
            out = {"video": object_key, "duration": info["duration"],
                   "width": info["width"], "height": info["height"], "title": title}
            await jobs.succeed(sess, artifact, object_key, float(info["duration"]),
                               result=out, attempt=attempt or None)
            done = True
        except Exception as e:
            await probe.drain()
            await jobs.fail(sess, artifact, str(e)[:500], attempt or None)
            raise
        finally:
            if not done:
                # 只收回自己的 render/（本节点这次的渲染期副本）。material 等目录里是
                # localize_ref 取回的中转字节：删掉不影响正确性（引用在库里，用时现取），
                # 只是同会话重跑要重新下一遍源片，所以失败不顺手清它。
                self.storage.workspace.cleanup(sess, state.artifact_id, "render")
        return {**out,
                "media_url": await self.storage.objects.presign_get(object_key)}


class _JobProgress:
    """渲染进度探针：MoviePy 在工作线程里回调，跨线程投回事件循环写 render_jobs。

    ``attempt`` 是本次渲染的尝试令牌：写进度时带上它，令牌不符的写会被 repo 丢弃。
    渲染线程杀不掉，被看门狗判死之后它还会继续回调一段时间——不围栏的话，这些
    迟到的进度会把「已经是新一次尝试」或「已是 failed」的行改回 running。
    """

    def __init__(self, jobs: Any, loop: asyncio.AbstractEventLoop,
                 session_id: str, artifact_id: str, attempt: str = "") -> None:
        self.jobs, self.loop = jobs, loop
        self.session_id, self.artifact_id = session_id, artifact_id
        self.attempt = attempt
        self._pending: list = []

    def __call__(self, stage: str, percent: int) -> None:
        self._pending.append(asyncio.run_coroutine_threadsafe(
            self.jobs.progress(self.session_id, self.artifact_id, stage, percent,
                               self.attempt or None),
            self.loop))

    async def drain(self) -> None:
        for fut in self._pending:
            try:
                await asyncio.wrap_future(fut)
            except Exception:
                pass          # 进度是观测面，写不动不该毁掉成片
        self._pending.clear()


def _encode_logger(notify, base: int = 70, span: int = 28):
    """MoviePy 的逐帧进度 → render_jobs 的编码百分比（70→98），控制台一个字符都不打印。

    2.1.2 的 ``write_videofile`` 没有 ``progress_callback``，而 ``default_bar_logger``
    对既不是 ``"bar"`` 也不是 ``None`` 的入参原样返回——于是给一个 proglog 子类就是
    唯一能拿到「编到第几帧」的口子。这一步不是装饰：编码是整条链路里最长的一段，
    百分比钉在 70 不动和真卡死在观测面上长得一模一样，看门狗也就无从判停滞。
    """
    import proglog

    class _FrameBars(proglog.ProgressBarLogger):
        def __init__(self) -> None:
            super().__init__(min_time_interval=0.5)   # 帧号最多每半秒看一次
            self._last = -1

        def log(self, message) -> None:
            pass    # 基类把每条消息存进 self.logs：两千帧的渲染不该攒一份日志正文

        def bars_callback(self, bar, attr, value, old_value=None) -> None:
            if bar != "frame_index" or attr != "index":
                return
            total = (self.bars.get(bar) or {}).get("total") or 0
            if total <= 0:
                return
            percent = base + min(span, int(span * float(value) / total))
            if percent != self._last:
                self._last = percent
                notify("encoding", percent)

    return _FrameBars()


def _subtitle_layers(tl: dict[str, Any], size: tuple[int, int],
                     settings: Settings) -> list:
    """时间线字幕 → 与画面层并列的 TextClip 列表（一条字幕一层，绝不嵌套合成）。

    逐条嵌套 CompositeVideoClip 是这里最贵的一种写法：每个套上去的合成都要对整幅
    画面重跑一遍 PIL，五段字幕＝逐帧五遍——真机 87 秒的 1080p 片子就是这么跑到半小时的。
    """
    from moviepy import TextClip

    w, h = size
    font = str(_find_font(settings.caps.subtitle_font, settings.caps.font_dirs))
    layers = []
    for sub in tl.get("subtitles", []):
        try:
            text = sub.get("text", "")
            if not text:
                continue
            tc = TextClip(font=font, text=text[:40], font_size=max(20, h // 22),
                          method="caption", size=(int(w * 0.9), None),
                          text_align="center",
                          stroke_color="black",
                          stroke_width=1 if sub.get("style") != "subtitle_bold" else 2,
                          color="white")
            layers.append(tc.with_start(sub["start"]).with_duration(
                max(0.2, sub["end"] - sub["start"])).with_position(("center", "bottom")))
        except Exception:
            continue  # MoviePy v2 TextClip 参数差异：丢字幕不丢成片
    return layers


def _render_with_moviepy(tl: dict[str, Any], dst: Path, settings: Settings,
                         notify) -> None:
    """MoviePy 2.x 渲染：多轨合成 + 字幕 + 配音 + BGM（阻塞，to_thread 内跑）。"""
    from moviepy import (AudioFileClip, CompositeAudioClip, CompositeVideoClip,
                         VideoFileClip)
    from moviepy.video.fx import FadeIn, FadeOut

    _patch_moviepy_subclip()

    w, h, fps = tl["width"] or 1280, tl["height"] or 720, max(1.0, tl["fps"] or 25.0)
    duration = tl["duration"] or 0.1
    notify("video_track", 10)
    # 能关掉 reader 的只有刚构造出来的那个文件 clip：subclipped / resized / with_effects
    # 返回的都是共享同一 reader 的浅拷贝，而 CompositeAudioClip.close() 是空操作。
    handles: list = []
    layers = []
    styles = tl.get("transition_styles") or []
    for i, ev in enumerate(tl["events"]):
        # audio=False：音轨统一由 audio_events 提供，别顺手为源视频再开一个无人关闭的
        # 音频 reader（with_audio(None) 丢掉的正是它，Windows 上锁的就是它）
        vc = VideoFileClip(ev["path"], audio=False)
        handles.append(vc)
        vc = vc.subclipped(float(ev["src_start"]), float(ev["src_end"]))
        vc = vc.resized((w, h)).with_start(ev["start"])
        style = None
        if ev.get("kind") == "transition":
            style = "fade"
        elif i < len(styles):
            style = styles[i]
        if style in ("fade", "fadeblack", "dissolve"):
            vc = vc.with_effects([FadeIn(0.25), FadeOut(0.25)])
        layers.append(vc.with_position("center"))
    composed = CompositeVideoClip(layers, size=(w, h))
    duration = min(float(duration), composed.duration)
    notify("subtitles", 40)
    sub_layers = _subtitle_layers(tl, (w, h), settings)
    # 字幕与画面层**拍平在同一层**合成，而不是套一层 CompositeVideoClip。
    #
    # 为什么：MoviePy 2.x 的 compose_on 在背景带 alpha 时，每层每帧都要新建整幅
    # RGBA 画布再做 paste + alpha_composite（moviepy/video/VideoClip.py:781-793），
    # 而且内层合成的 mask 每帧重算（compositing/CompositeVideoClip.py:130-139）。
    # 套两层就等于把整幅画面的合成做两遍——渲染时间基本与「层数×画布面积」成正比，
    # 而真机实测渲染 660s 占整跑 69%，是本链路最大的单点。
    #
    # 拍平不改变结果：字幕层本来就带 with_start/with_position，它作为兄弟层与
    # 「先合成画面、再把它和字幕合成」在数学上等价。已逐帧验证：
    # .runtime/audit/verify_p1_flatten.py 对真实素材 75/75 帧像素 sha1 完全一致，
    # 耗时 40.7 → 24.4 ms/帧（1.67×）。
    canvas = CompositeVideoClip([*layers, *sub_layers], size=(w, h)) if sub_layers else composed
    canvas = canvas.subclipped(0, duration)
    notify("audio", 55)
    audio_items = []
    for ae in tl.get("audio_events", []):
        try:
            ac = AudioFileClip(ae["path"])
            handles.append(ac)
            # 原声混剪：音轨来自源视频的口播区间（src_start/src_end）
            if ae.get("src_start") is not None and ae.get("src_end") is not None:
                ac = ac.subclipped(float(ae["src_start"]), float(ae["src_end"]))
            ac = ac.with_start(ae["start"])
            audio_items.append(ac)
        except Exception:
            continue
    bgm = tl.get("bgm")
    if bgm and Path(bgm["path"]).exists():
        try:
            from moviepy.audio.fx import AudioLoop, MultiplyVolume
            # AudioLoop 内部 concatenate 出 CompositeAudioClip，而它的 close() 是空操作：
            # 必须留住下面这个原始 clip，否则整条 BGM 的 wav 句柄没人能关。
            looped = AudioFileClip(bgm["path"])
            handles.append(looped)
            bc = looped.with_effects([AudioLoop(duration=duration)])
            bc = bc.with_effects([MultiplyVolume(bgm.get("volume", 0.2))])
            audio_items.append(bc)
        except Exception:
            pass
    if audio_items:
        mixed = CompositeAudioClip(audio_items)
        # 配音整段挂在分组起点，裁短画面后音轨可能比画面长：不夹住，mux 就把尾巴续上去
        if float(mixed.duration or 0) > duration:
            mixed = mixed.subclipped(0, duration)
        canvas = canvas.with_audio(mixed)
    notify("encoding", 70)
    try:
        canvas.write_videofile(str(dst), fps=fps, codec="libx264", audio_codec="aac",
                               preset="ultrafast", threads=8,
                               logger=_encode_logger(notify),
                               temp_audiofile=str(dst.with_name("temp_audio.m4a")))
    finally:
        # 每一个文件 clip 都握着一个 ffmpeg 子进程和打开的文件：漏关一个，Windows 上
        # 就把整份工作区目录锁到进程结束——失败清理和 sweep_stale 都删不掉它，
        # 「工作区随时可弃」成了空话。关不掉也只可能是成片已经没了的事。
        for c in handles:
            try:
                c.close()
            except Exception:
                pass



def _render_segmented(tl: dict[str, Any], dst: Path, settings: Settings,
                      notify) -> None:
    """分段渲染：按 60 秒切时间线，每段用 MoviePy 渲染再拼接。

    ``notify`` 每段都要报一次：不报的话整条路径在看门狗眼里「从未有过进度」，
    长片必然被判死（真机现场就是这一条把正在跑的渲染打死、再开第二份）。
    """
    say = notify or (lambda *_: None)
    total = float(tl.get("duration", 0))
    seg_dur = 60.0
    n_segs = max(1, int(total // seg_dur) + (1 if total % seg_dur > 0 else 0))
    tmp = _attempt_dir(dst, "seg")
    parts = []
    for i in range(n_segs):
        s, e = i * seg_dur, min(total, (i + 1) * seg_dur)
        if e - s < 0.5:
            continue
        seg_tl = _slice_timeline(tl, s, e)
        if not seg_tl.get("events"):
            continue
        part = tmp / f"{i:03d}.mp4"
        try:
            _render_with_moviepy(seg_tl, part, settings, notify)
        except Exception:
            _render_via_ffmpeg(seg_tl, part, notify)
        # 分段路径的进度按「已完成段数」推进（60 → 95），末段拼接再补一次。
        say("segments", 60 + int(35 * (i + 1) / max(1, n_segs)))
        if part.exists() and part.stat().st_size > 0:
            parts.append(part)
    if not parts:
        raise MediaError("分段渲染：所有段都失败")
    list_file = tmp / "_list.txt"
    write_concat_list(list_file, parts)
    mediaops.ffmpeg("-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-c", "copy", str(dst))


def _slice_timeline(tl: dict[str, Any], start: float, end: float) -> dict[str, Any]:
    """从时间线中截取 [start, end) 区间的 events/audio_events/subtitles。"""
    def _clip_events(events, key="events"):
        out = []
        for ev in events:
            s, e = float(ev.get("start", 0)), float(ev.get("end", 0))
            if e <= start or s >= end:
                continue
            ns, ne = max(s, start), min(e, end)
            offset = ns - s
            out.append({**ev, "start": round(ns - start, 3), "end": round(ne - start, 3),
                        "src_start": round(float(ev.get("src_start", 0)) + offset, 3),
                        "src_end": round(float(ev.get("src_end", 0)) + offset, 3)})
        return out
    return {
        "width": tl.get("width", 1280), "height": tl.get("height", 720),
        "fps": tl.get("fps", 25.0), "duration": round(end - start, 3),
        "events": _clip_events(tl.get("events", [])),
        "audio_events": _clip_events(tl.get("audio_events", [])),
        "subtitles": _clip_events(tl.get("subtitles", [])),
        "bgm": tl.get("bgm"), "transition_styles": tl.get("transition_styles", []),
        "mode": tl.get("mode", ""),
    }


def _attempt_dir(dst: Path, tag: str) -> Path:
    """给兜底渲染的中转目录一个**每次尝试都不同**的名字。

    原先用的是 ``_fb_{stem}`` / ``_seg_{stem}`` 这种固定名。一旦同一产物被重渲
    （看门狗判死后的重试、或两次提交撞在一起），两份渲染会写进同一个目录：
    现场就留下过 69 个分段时间戳分两波、互相覆盖的痕迹，成片可能是两次尝试的混合体。
    加上 pid 与时间戳后，两次尝试各写各的，最后成片仍是同一个 dst（后者覆盖前者，
    符合「同产物重渲覆盖同一个成片键」的既有语义）。
    """
    import os
    import time as _time

    stamp = f"{os.getpid()}-{int(_time.time() * 1000) % 1000000}"
    d = Path(dst).with_suffix("").parent / f"_{tag}_{Path(dst).stem}_{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_via_ffmpeg(tl: dict[str, Any], dst: Path, notify=None) -> None:
    """兜底渲染：画面切片拼接（无原声）+ audio_events/BGM 混音。

    MoviePy 失败时的保底路径。与 ``_render_with_moviepy`` 对齐：
    画面轨不带原声（``-an``），音轨统一由 ``audio_events`` + BGM 提供。

    ``notify(stage, percent)`` 必须**全程**被调用，不能只在开头报一次：渲染停滞看门狗
    按「多久没进度」判死，一次都不报的表等于「立刻停滞」——真机上这就表现为
    「正常在跑的渲染被判失败，然后重试又开第二份」。每个切片报一次，进度就有节奏了。
    """
    say = notify or (lambda *_: None)
    tmp = _attempt_dir(dst, "fb")

    # 1. 画面：切片（无音频）→ concat
    parts = []
    n_ev = max(1, len(tl["events"]))
    for i, ev in enumerate(tl["events"]):
        part = tmp / f"{i:03d}.mp4"
        dur = max(0.04, float(ev["src_end"]) - float(ev["src_start"]))
        mediaops.ffmpeg("-ss", f"{float(ev['src_start']):.3f}", "-i", str(ev["path"]),
                        "-t", f"{dur:.3f}", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        str(part))
        parts.append(part)
        say("video_track", 60 + int(20 * (i + 1) / n_ev))
    list_file = tmp / "_vlist.txt"
    write_concat_list(list_file, parts)
    video_only = tmp / "_video.mp4"
    mediaops.ffmpeg("-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-c", "copy", str(video_only))
    say("audio", 82)

    # 2. 抽取 audio_events 各段音频
    audio_segs: list[tuple[Path, float]] = []
    for j, ae in enumerate(tl.get("audio_events", [])):
        seg = tmp / f"_a{j}.wav"
        ss = float(ae.get("src_start", 0.0))
        # 取「结束点」必须按 src_end → end → src_start+duration 的次序回退。
        # ``_build_timeline`` 写出的 audio_events **只有 path/start/group_id**，
        # 没有 src_end 也没有 end——原先只回退到 ``end``（默认 0.0），于是
        # 「配音 6.0s」被算成 ``max(0.04, 0 - 0)`` = 0.04s，抽出来的是一段 0.04s 的
        # 静音。整条 ffmpeg 兜底路径因此**始终出无声片**，而且不报错、看不出问题。
        if "src_end" in ae:
            se = float(ae["src_end"])
        elif "end" in ae:
            se = float(ae["end"])
        else:
            se = ss + float(ae.get("duration", 0.0) or 0.0)
        dur = max(0.04, se - ss)
        try:
            mediaops.ffmpeg("-ss", f"{ss:.3f}", "-i", str(ae["path"]),
                            "-t", f"{dur:.3f}", "-vn", "-ac", "2", "-ar", "48000",
                            "-c:a", "pcm_s16le", str(seg))
            audio_segs.append((seg, float(ae.get("start", 0.0))))
        except Exception:
            continue
    say("audio", 88)

    # 3. BGM（循环到总时长 + 调音量）
    bgm = tl.get("bgm")
    bgm_wav: Path | None = None
    if bgm and Path(bgm["path"]).exists():
        bgm_wav = tmp / "_bgm.wav"
        vol = float(bgm.get("volume", 0.2))
        total = float(tl.get("duration", 0)) or 9999
        try:
            mediaops.ffmpeg("-stream_loop", "-1", "-i", str(bgm["path"]),
                            "-t", f"{total:.3f}",
                            "-af", f"volume={vol}",
                            "-ac", "2", "-ar", "48000",
                            "-c:a", "pcm_s16le", str(bgm_wav))
        except Exception:
            bgm_wav = None

    # 4. 混音 + mux
    if not audio_segs and not bgm_wav:
        mediaops.ffmpeg("-i", str(video_only),
                        "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=48000",
                        "-c:v", "copy", "-c:a", "aac", "-shortest", str(dst))
        say("encoding", 96)
        return

    inputs = ["-i", str(video_only)]
    filter_parts: list[str] = []
    idx = 0
    for seg_path, start_t in audio_segs:
        inputs += ["-i", str(seg_path)]
        idx += 1
        delay_ms = max(0, int(start_t * 1000))
        filter_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[a{idx}]")
    if bgm_wav:
        inputs += ["-i", str(bgm_wav)]
        idx += 1
        filter_parts.append(f"[{idx}:a]adelay=0|0[a{idx}]")
    mix_labels = "".join(f"[a{i}]" for i in range(1, idx + 1))
    filter_complex = ";".join(filter_parts) + (
        f";{mix_labels}amix=inputs={idx}:duration=longest:normalize=0[aout]")
    mediaops.ffmpeg(*inputs,
                    "-filter_complex", filter_complex,
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac",
                    "-shortest", str(dst))
    say("encoding", 96)


def _find_font(preferred: str, font_dirs: list[str]) -> Path:
    names = (preferred, "msyh", "msyhbd", "simhei", "NotoSansCJK-Regular", "arial")
    dirs = [Path(d) for d in font_dirs]
    for d in dirs:
        for name in names:
            for ext in (".ttf", ".ttc", ".otf"):
                f = d / f"{name}{ext}"
                if f.exists():
                    return f
    for d in dirs:            # 目录里没有偏好字体：退到该目录下任意 ttf/ttc
        if d.is_dir():
            for ext in (".ttf", ".ttc", ".otf"):
                got = next(iter(sorted(d.glob(f"*{ext}"))), None)
                if got:
                    return got
    return Path("arial")


REAL_NODE_CLASSES = [
    # 输入阶段 → 素材处理层 → 逻辑与脚本层 → 时间轴规划层 → 最终输出
    SearchMediaNode, LoadMediaNode,
    SplitShotsNode, UnderstandClipsNode, FilterClipsNode, GroupClipsNode,
    AsrNode, SpeechRoughCutNode,
    ScriptTemplateRecNode, GenerateScriptNode, GenerateAITransitionNode,
    TransitionRecNode, TextRecNode, GenerateVoiceoverNode, SelectBGMNode,
    PlanTimelineNode, PlanTimelineProNode, PlanTimelineAITransitionNode,
    RenderVideoNode,
]


def build_real_registry(settings: Settings, providers: Providers, storage: Storage,
                        allowed: list[str] | None = None) -> NodeRegistry:
    """实例化真实节点；allowed 为 TOML available_nodes 白名单。"""
    reg = NodeRegistry()
    name_to_cls = {cls.name: cls for cls in REAL_NODE_CLASSES}
    for nm in (allowed or list(name_to_cls)):
        cls = name_to_cls.get(nm)
        if cls is not None:
            cls(settings, providers, storage, registry=reg)
    return reg
