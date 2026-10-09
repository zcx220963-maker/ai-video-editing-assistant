"""零素材图形科普片出片节点（motion 通道）：分镜 spec → 版式页面 → 逐帧截图 → 逐镜合成 → 混音。

分工口径（README §5.2）：本节点是「能力」，Skill 只教模型什么时候选它。
与 render_video 的关系：**另一条独立的终点路径**，而且比它更独立——那条路要先有素材文件，
这条路的输入只有文案与版式（见 motion/ 包），起点即终点。

节点层负责的四件事（渲染层 motion/render.py 一概不知道）：
* **入参把关**：spec 先过 validate_spec，枚举越界、高亮词不在文案里这类错必须在**烧像素
  之前**回清单——一次渲染是分钟级到十分钟级，跑完再报错不叫把关，叫事故；
* **进度上账**：逐帧事件折成 render_jobs 的百分比（同值去重，146 帧不该写 146 行）。
  这一步不只是给界面看：看门狗按「无进展时长」判死，不报进度会被误杀；
* **产物落库**：成片经 workspace.publish 进对象存储，payload 只留对象键；``media_url``
  是会过期的 presigned 直链，标 ephemeral 只回调用方——与 render_video 同一条契约；
* **证据分级**：机器量得到的（字节、像素、每镜有没有放完自己的旁白）标 verified，
  听感与「版式好不好看、史实对不对」永远标 UNVERIFIED——不许替验不了的条目背书。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from agent_framework.orchestration import ArtifactStore, NodeState, _safe
from agent_framework.storage import to_ref

from ..motion import hitmap as mhit
from ..motion import patch as mpatch
from ..motion import render as motion
from ..motion import spec as mspec
from .core_nodes import (SYNC_TOLERANCE_SEC, StoryNode, _JobProgress, _ev,
                         _frame_spotcheck, _obj, _resolve_target_duration)
from .motion_plan import _source_errors, plan_payload

#: 目标时长的硬闸倍数：估算区间整体偏离到这个份上就别开工——逐帧截图按帧计费，
#: 「先渲二十分钟再告诉你比目标长一倍」不是把关。
BLOCK_LONG_FACTOR = 1.6
BLOCK_SHORT_FACTOR = 0.5

#: 每镜至少比自己的声音多留这么多秒（渲染层的呼吸余量是 0.10+0.35，留 0.05 给取整）
BREATH_FLOOR_SEC = 0.40


class _ObjectShotCache(motion.ShotCache):
    """按镜缓存的对象存储实现：``motion-shots/{会话}/{镜头键}/{文件}``。

    为什么键里带会话而不做全局共享：内容哈希相同的两镜确实能复用，但对象键里带会话
    才谈得上「这是谁的画面字节」——全局前缀等于把一条片子的帧交给任何算得出哈希的人。
    而复用真正发生的场景本来就在会话内：同一人同一对话里的重跑、以及局部改只重渲
    受影响的那几镜。

    读失败一律当未命中（重渲一遍即可），写失败由调用方记一笔（下次多烧一次像素）——
    缓存坏了不该让出片失败。
    """

    _TYPES = {"clip.mp4": "video/mp4", "frame.png": "image/png",
              "meta.json": "application/json", "hitmap.json": "application/json"}

    def __init__(self, workspace: Any, session_id: str) -> None:
        self.ws = workspace
        self.sess = session_id

    def obj_key(self, key: str, name: str) -> str:
        return f"motion-shots/{_safe(self.sess)}/{key}/{name}"

    async def fetch(self, key: str, dst_dir: Path) -> dict[str, Path] | None:
        if await self.ws.objects.head(self.obj_key(key, "meta.json")) is None:
            return None
        dst_dir.mkdir(parents=True, exist_ok=True)
        out: dict[str, Path] = {}
        for name in self.FILES:
            dest = dst_dir / name
            try:
                chunks = [c async for c in self.ws.objects.get_stream(
                    self.obj_key(key, name))]
                data = b"".join(chunks)
            except Exception:                    # noqa: BLE001 - 缺一个文件即整体未命中
                return None
            if not data:
                return None
            dest.write_bytes(data)
            out[name] = dest
        return out

    async def store(self, key: str, src: dict[str, Path]) -> None:
        for name in self.FILES:
            p = src.get(name)
            if p is None or not Path(p).exists():
                raise FileNotFoundError(f"缓存缺文件：{name}")
            await self.ws.publish(str(p), self.obj_key(key, name),
                                  content_type=self._TYPES[name])


class RenderMotionVideoNode(StoryNode):
    name = "render_motion_video"
    display_name = "图形科普片出片"
    description = (
        "没有素材也能出片：把分镜 spec 的文案排成版式画面（headless 浏览器逐帧截图），"
        "镜头长度由真实 TTS 时长决定（话没说完画面不会切走），字幕逐词点亮，"
        "人声起时 BGM 自动压低。适用：科普/历史/概念解释类短片，或用户明确说「没有素材，"
        "你自己查资料写文案配画面」。有素材的剪辑仍走 plan_timeline → render_video。"
        "spec 由 plan_motion 产出，也可自行手写。渲染是分钟级到十分钟级：返回 "
        "status=queued/running 时须继续调 render_status（同 artifact_id）直到 done/failed")
    required_nodes: list[str] = ["plan_motion"]
    # media_url 是一条一小时后就读不通的 presigned 直链：只回调用方，不落进共享库
    ephemeral = ("media_url",)
    input_schema = _obj("零素材图形科普片出片", {
        "spec": {
            **mspec.spec_schema(),
            "description": "分镜 spec（plan_motion 的产物；缺省从 Store 的 plan_motion 取，"
                           "也可按下面的结构手写）。**别写每镜 duration_sec**——有旁白时"
                           "时钟归声音，写了只被当作下限参考。"
                           "aspect/fps/narration/voice/rate/subtitle_mode 也能走下面的顶层开关，"
                           "顶层的优先（用户在计划卡上勾的那一份）",
        },
        **mspec.knob_props(),
        "bgm": {
            "type": "string",
            "description": "背景音乐：消息附件/曲库歌曲的 material_id，或 select_bgm 返回的"
                           " obj: 引用（覆盖 spec.bgm.ref）。传了就**必须**取得到字节，"
                           "取不到直接失败——用户点了某条 BGM 却静默出成无声片是骗人。"
                           "不传则按 spec.bgm.ref；两者都没有＝纯人声",
        },
        "target_duration_sec": {
            "type": "number",
            "description": "用户要的目标秒数。渲之前按字数估一遍，偏离太离谱（估算下限超目标 "
                           "1.6 倍、或上限不足目标一半）就拦下不烧像素，回「该删镜还是加镜」的账",
        },
        "wait_sec": {
            "type": "number",
            "description": "本次调用内联等待的秒数（缺省用 [capabilities].render_grace_sec，"
                           "上限 render_wait_max_sec）。传 0 表示只提交不等",
        },
    })

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        raw = inputs.get("spec")
        if not isinstance(raw, Mapping) or not raw:
            raw = (await state.store.get("plan_motion") or {}).get("motion_spec")
        if not isinstance(raw, Mapping) or not raw:
            repo = getattr(state.store, "repo", None)
            if repo is not None:
                fresh = await repo.get("plan_motion")
                if isinstance(fresh, Mapping):
                    raw = fresh.get("motion_spec")
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                "render_motion_video：没有 spec 参数，Store 里也没有 plan_motion 产物——"
                "先调 plan_motion 出分镜，或把 spec 直接传进来")
        # 顶层开关再并一次：用户可能只在出片这一步改了画幅/帧率/音色，而 Store 里那份
        # 是分镜时落的。并完照旧过 validate_spec——越界的值不会走到烧像素那一步。
        spec, errors = mspec.validate_spec(mspec.overlay_knobs(dict(raw), inputs))

        sess, artifact = state.session_id, state.artifact_id or "_default"
        jobs = self.storage.render_jobs
        # 与 render_video 同一条口径：**先开任务行**再校验。失败必须留下一行 failed，
        # 否则「这次出片没成」在界面与 render_status 里压根不存在，错误就消失了。
        row = await jobs.open(sess, artifact)
        attempt = str((row or {}).get("attempt") or "")
        try:
            out = await self._render(
                state, spec, errors,
                target=_resolve_target_duration(state, inputs, None),
                bgm_ref=str(inputs.get("bgm") or ""),
                sess=sess, artifact=artifact, jobs=jobs, attempt=attempt)
        except Exception as exc:
            await jobs.fail(sess, artifact, str(exc)[:500], attempt or None)
            raise
        return {**out,
                "media_url": await self.storage.objects.presign_get(out["video"])}

    async def _bgm_ref(self, state: NodeState, value: str) -> str:
        """bgm 入参 → 能取到字节的引用。两种写法都收：``obj:`` 引用、素材 material_id。

        为什么必须收 material_id：随消息附上的曲库歌曲，模型手里只有 material_id
        （附件事实行不含对象键），而唯一能把歌曲换成 ``obj:`` 引用的 select_BGM 绑在
        有素材那条链上（required_nodes=generate_script），零素材路子里调它会连带补齐
        ASR/片段理解一串上游。归属交给 materials.resolve 判：别人的 id 一律算不存在。
        """
        if value.startswith("obj:"):
            return value
        got, _denied = await self.storage.materials.resolve(
            [value], user_id=state.user_id, conv_id=state.conversation_id or None)
        if not got:
            raise ValueError(f"没有这条配乐素材：{value}")
        return to_ref(got[0]["object_key"])

    async def _render(self, state: NodeState, spec: dict[str, Any],
                      errors: list[str], *, target: float | None, bgm_ref: str,
                      sess: str, artifact: str, jobs: Any,
                      attempt: str, gate_note: str = "") -> dict[str, Any]:
        if errors:
            raise ValueError(
                f"{gate_note}spec 校验未通过，一帧都没开始渲（照清单改完再来）：\n"
                + "\n".join(f"  · {e}" for e in errors[:12])
                + (f"\n  · …另有 {len(errors) - 12} 条同类错误" if len(errors) > 12 else ""))

        low, high = mspec.estimate_sec(spec)
        n = len(spec.get("shots") or [])
        if target and (low > target * BLOCK_LONG_FACTOR or high < target * BLOCK_SHORT_FACTOR):
            too_long = low > target * BLOCK_LONG_FACTOR
            suggest = (f"删到约 {max(1, round(n * target / max(high, 0.1)))} 镜，"
                       f"或把每镜文案缩短"
                       if too_long else
                       f"加到约 {max(n + 1, round(n * target / max(low, 0.1)))} 镜，"
                       f"或放慢语速")
            raise ValueError(
                f"目标 {target:.0f}s，但这份分镜按字数估 {low:.0f}~{high:.0f}s（{n} 镜），"
                f"偏离超过闸值——逐帧截图烧不起这一次：{suggest}。改完重调本工具")

        bgm_conf = spec.get("bgm") or {}
        ref = bgm_ref or str(bgm_conf.get("ref") or "")
        bgm_path: Path | None = None
        if ref:
            try:
                bgm_path = await self._local(state, await self._bgm_ref(state, ref),
                                             "motion", "bgm")
            except Exception as exc:
                raise ValueError(
                    f"BGM 取不到字节：{ref}（{type(exc).__name__}: {str(exc)[:160]}）"
                    "——改传消息附件/曲库的 material_id，或 select_bgm 返回的 obj: 引用；"
                    "或去掉 bgm 出纯人声片") from exc

        work = self._work(state, "motion")
        total = max(1, len(spec.get("shots") or []))
        probe = _JobProgress(jobs, asyncio.get_running_loop(), sess, artifact, attempt)
        done = {"n": 0, "pct": -1}

        async def on_event(ev: dict[str, Any]) -> None:
            stage, pct = self._percent(ev, total, done["n"])
            if stage == "shot":
                done["n"] = int(ev.get("done") or 0)
            if pct != done["pct"]:        # 帧级事件每 0.8 秒来一次，同值不写库
                done["pct"] = pct
                probe(stage, pct)

        try:
            out = await motion.render_motion(
                spec, providers=self.providers, work=work, bgm=bgm_path,
                progress=on_event,
                cache=_ObjectShotCache(self.storage.workspace, sess))
            await probe.drain()           # 迟到进度不能盖掉后面的终态
            frames, frame_reason = await self._spotcheck(state, out)
            object_key = f"renders/{_safe(sess)}/{_safe(artifact)}/motion.mp4"
            await self.storage.workspace.publish(str(out["video"]), object_key,
                                                 content_type="video/mp4")
            shot_frames = await self._publish_frames(out, sess=sess, artifact=artifact)
            hits_key = await self._publish_hitmap(state, spec, out,
                                                  sess=sess, artifact=artifact,
                                                  frames=shot_frames)
        finally:
            await probe.drain()
            # 帧目录是纯垃圾（一次渲染上千张 PNG），成片已进对象存储——成败都收回，
            # 不像 render_video 那样留源片中转字节：这条链路没有源片。
            self.storage.workspace.cleanup(sess, state.artifact_id, "motion")

        result = self._result(spec, out, target=target, low_high=(low, high),
                              warnings=mspec.check_target(spec, target),
                              bgm_ref=ref, object_key=object_key,
                              frames=frames, frame_reason=frame_reason,
                              hitmap_ref=hits_key,
                              shot_frames=shot_frames,
                              hitmap_errors=list(out.get("hitmap_errors") or []))
        await jobs.succeed(sess, artifact, object_key, float(out["duration"]),
                           result=result, attempt=attempt or None)
        return result

    async def _publish_frames(self, out: dict[str, Any], *,
                              sess: str, artifact: str) -> dict[str, str]:
        """每镜的代表帧按产物作用域发布，返回 ``{镜号: 对象键}``。

        为什么不直接给缓存里的 ``motion-shots/...`` 那份：缓存键是内容哈希，随分镜改
        一字就换一条，前端拿它当缩略图地址等于拿一个「下一次就不存在」的 URL；而时间线
        要的是「这条片子的第 3 镜长什么样」，那是产物级的事实，与字节从哪来无关。
        代价是每镜一份 PNG（几十 KB），换来的是这条片子自己说得清它的每一镜。
        """
        refs: dict[str, str] = {}
        for sid, files in (out.get("shot_files") or {}).items():
            frame = files.get("frame")
            if frame is None or not Path(frame).exists():
                out.setdefault("hitmap_errors", []).append(
                    f"{sid}：没有代表帧（缓存与本次渲染都没落出 PNG）")
                continue
            key = f"renders/{_safe(sess)}/{_safe(artifact)}/frames/{_safe(sid)}.png"
            try:
                await self.storage.workspace.publish(str(frame), key,
                                                     content_type="image/png")
                refs[sid] = key
            except Exception as exc:  # noqa: BLE001 - 缩略图缺一张不该牵连整条片子
                out.setdefault("hitmap_errors", []).append(
                    f"{sid}：代表帧没发布（{type(exc).__name__}: {str(exc)[:120]}）")
        return refs

    async def _publish_hitmap(self, state: NodeState, spec: dict[str, Any],
                              out: dict[str, Any], *, sess: str, artifact: str,
                              frames: dict[str, str] | None = None) -> str:
        """命中表落对象存储，返回对象键（落不成回空串，不牵连这次出片）。

        为什么不塞进 ``motion_ledger``：那是给模型看的工具返回。一张命中表有几百条带
        像素盒与计算样式的条目，模型既不需要它、也不该照着它改字段——它是前端画选区、
        以及局部改那一头验指纹的原料，走 HTTP 直读，不经模型的上下文。

        文件必须**自足**：除了框与指针，还带上**这一版分镜本身**、它的指纹与逐镜秒数。
        少了指纹与 spec，局部改就无从判断这张表是不是当前这版分镜量出来的，只能退回去
        拿会话里的 ``plan_motion`` 猜「这一版片子当初渲的是什么」——而那份可能被后来的
        分镜覆盖过；少了秒数，前端得回头拼时间线，而拼出来的偏移与成片差多少没人知道。
        """
        tables = out.get("hitmap") or {}
        if not tables:
            return ""
        timeline, start = [], 0.0
        for rec in out["shots"]:
            sec = float(rec.get("sec") or 0.0)
            sid = rec.get("id")
            timeline.append({"id": sid, "sec": sec,
                             "start_sec": round(start, 3),
                             "speech_sec": rec.get("speech_sec"),
                             "settle_ms": rec.get("settle_ms"),
                             "cached": bool(rec.get("cached")),
                             "cache_key": rec.get("cache_key") or "",
                             "frame": (frames or {}).get(sid) or ""})
            start += sec
        payload = {
            "schema": "motion-hitmap/1",
            "spec": spec,
            "spec_fingerprint": mhit.fingerprint(spec),
            "width": int(out["width"]), "height": int(out["height"]),
            "fps": float(out["fps"]), "duration": float(out["duration"]),
            "settle_unit": "shot_relative_ms",
            "timeline": timeline, "shots": tables,
        }
        path = self._work(state, "motion") / "hitmap.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False),
                            encoding="utf-8")
            key = f"renders/{_safe(sess)}/{_safe(artifact)}/hitmap.json"
            await self.storage.workspace.publish(str(path), key,
                                                 content_type="application/json")
            return key
        except Exception as exc:  # noqa: BLE001 - 命中表是编辑用的附加产物
            out.setdefault("hitmap_errors", []).append(
                f"命中表没落进对象存储（{type(exc).__name__}: {str(exc)[:160]}）："
                "成片照常，但这次不能按区域选着改")
            return ""

    @staticmethod
    def _percent(ev: dict[str, Any], total: int, done_shots: int) -> tuple[str, int]:
        """事件 → (阶段名, 百分比)。0~4 给窗口校准，93/96 给拼接与混音，中间 88% 按镜头走。

        帧级事件只推进「本镜内部」那一小段，所以整片百分比永远单调：
        一镜的最后一帧不会比下一镜的第一帧更高。
        """
        stage = str(ev.get("stage") or "motion")
        if stage == "shot":
            return stage, 4 + int(88 * int(ev.get("done") or 0) / total)
        if stage == "capture":
            frames = max(1, int(ev.get("shot_frames") or 1))
            frac = min(1.0, float(ev.get("frame") or 0) / frames)
            return stage, 4 + int(88 * (done_shots + frac) / total)
        if stage == "assemble":
            return stage, 93
        if stage == "mux":
            return stage, 96
        return stage, 2

    async def _spotcheck(self, state: NodeState,
                         out: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """从**要发布的那个文件**真取几帧：机器能够到的最高一级证据。

        抽帧失败不改判这次渲染（片子确实存在，字节也量到了），只把像素级那两条降回
        UNVERIFIED，并写明为什么降——不许把没做成的那步写成做过了。
        """
        try:
            frames = await asyncio.to_thread(
                _frame_spotcheck, Path(str(out["video"])), float(out["duration"]),
                self._work(state, "motion", "spot"))
            return frames, ""
        except Exception as exc:  # noqa: BLE001 - 证据降级，不改判这次渲染
            return None, (f"抽帧这一步没跑成（{type(exc).__name__}: {str(exc)[:160]}），"
                          "所以「不是黑屏/画面在不在动」没有像素级证据")

    def _result(self, spec: dict[str, Any], out: dict[str, Any], *,
                target: float | None, low_high: tuple[float, float],
                warnings: list[str], bgm_ref: str, object_key: str,
                frames: Mapping[str, Any] | None, frame_reason: str,
                hitmap_ref: str = "",
                shot_frames: Mapping[str, str] | None = None,
                hitmap_errors: list[str] | None = None) -> dict[str, Any]:
        ledger = out["shots"]
        want = (int(spec["width"]), int(spec["height"]), float(spec["fps"]))
        got = (int(out["width"]), int(out["height"]), float(out["fps"]))
        voiced = [r for r in ledger if int(r.get("words") or 0) > 0]
        held = [r for r in voiced
                if float(r["sec"]) >= float(r["speech_sec"]) + BREATH_FLOOR_SEC]
        degraded = list(out.get("degraded") or [])
        drift = float(out.get("av_drift_sec") or 0.0)
        tol = SYNC_TOLERANCE_SEC
        words = sum(int(r.get("words") or 0) for r in ledger)
        copied = sum(int(r["frames"]) - int(r["captured_frames"]) for r in ledger)

        pixel_ok = bool(frames) and not frames.get("black_indices")
        published = int(out.get("published_frames") or 0)
        cached = [r for r in ledger if r.get("cached")]
        refs = dict(shot_frames or {})
        evidence = [
            _ev(f"成片字节量到的：{got[0]}x{got[1]}@{got[2]:g}fps、{out['duration']:.1f}s、"
                f"{len(ledger)} 镜 {published} 帧"
                f"（本次逐帧截图 {out['frames_total']} 帧，其中 "
                f"{copied} 帧是动画停摆后复用末帧省下的；复用切片的不计帧数；"
                f"每镜画面按语音时长收口，所以成片少于计划）",
                "machine", verified=got == want,
                proof="probe 的就是要发布的那个文件，与 spec 要求一致" if got == want
                else f"与 spec 要求 {want[0]}x{want[1]}@{want[2]:g} 不符"),
            _ev(f"时钟归声音：{len(held)}/{len(voiced)} 个带声镜头的长度 ≥ 自己语音时长 + "
                f"{BREATH_FLOOR_SEC}s，没有一镜在话说到一半时切走",
                "machine", verified=bool(voiced) and len(held) == len(voiced),
                proof="每镜秒数由 TTS 实测回填（不是模型写的 duration_sec）"
                if voiced else "这份分镜没有带旁白的镜头（narration=false）"),
            _ev(f"字幕逐词点亮：{words} 个词锚在 TTS 返回的词时间戳上",
                "machine", verified=bool(voiced),
                proof="align_words 按文案顺序把词时间戳贴到字上，页面上是 data-at 属性"
                if voiced else "没有旁白就没有词时间戳，字幕退化成整条显示"),
            _ev(f"混音走的是 {out['mix_mode']}"
                + (f"（BGM={bgm_ref}，人声起时压低）" if "duck" in out["mix_mode"] else ""),
                "machine", verified=True,
                proof="sidechaincompress 压的是 BGM 那一路，触发信号是人声"
                if "duck" in out["mix_mode"] else "本次没有 BGM 输入"),
            _ev(f"音画漂移 {drift:.3f}s（闸值 {tol}s）",
                "machine", verified=drift <= tol,
                proof="拼接与混音都按主轨时长收口，量的是成片与母带之差"),
            _ev("TTS 全部走通，没有静音降级的镜头",
                "machine", verified=not degraded,
                proof="降级会逐镜记在 motion_ledger[].degraded" if not degraded
                else "；".join(degraded[:3])),
        ]
        if frames:
            evidence.append(_ev(
                f"抽帧实测不黑屏：取样点 {frames['at']}s 的亮度 "
                f"{frames['luma']}（最低 {frames['min_luma']}）",
                "frame", verified=pixel_ok,
                proof="ffmpeg 出灰度裸流按字节算均值" if pixel_ok else "有取样点亮度低于 6"))
            evidence.append(_ev("画面在动（取样点之间像素不同）", "frame",
                                verified=bool(frames.get("distinct")),
                                proof="三个取样点的灰度指纹互不相同"
                                if frames.get("distinct") else
                                "取样点撞进了同一张静止版式——这类片子本就常停着读字，"
                                "要确认动感请看中间几秒"))
        else:
            evidence.append(_ev("抽帧看有没有黑屏", "frame", verified=False,
                                proof=frame_reason))
        tables = out.get("hitmap") or {}
        elems = sum(len(tab.get("entries") or []) for tab in tables.values())
        anchored = sum(int(tab.get("resolved") or 0) for tab in tables.values())
        hit_errors = list(hitmap_errors or [])
        evidence.append(_ev(
            f"按区域选着改的命中表：{len(tables)} 镜 {elems} 个可见元素已量到位置，"
            f"其中 {anchored} 个能确定对回分镜字段（其余只给「改这一镜」的入口，"
            f"不猜字段）",
            "machine", verified=bool(hitmap_ref),
            proof="框由页面自己 getBoundingClientRect 量、归一化到画面带；"
                  "字段按文本在 role 圈定的范围内反查，对不上就标 none"
            if hitmap_ref else "；".join(hit_errors[:3]) or "命中表没有落进对象存储"))
        saved = sum(int(r["frames"]) for r in cached)
        evidence.append(_ev(
            f"按镜切片缓存：{len(cached)}/{len(ledger)} 镜直接复用了已有切片"
            f"（省掉 {saved} 次逐帧截图的浏览器启动）",
            "machine", verified=True,
            proof="缓存键 =「这一镜的内容 + 决定排版与配音的顶层开关」的哈希；"
                  "改一个字即未命中，只重烧那一镜。换 BGM 不动切片"
                  if cached else
                  "首次出片，每镜都真烧了一遍；同一会话里再渲相同镜头就会命中"))
        evidence.append(_ev(
            f"每镜代表帧已留存 {len(refs)}/{len(ledger)}（时间线缩略图与改前改后对比取它）",
            "machine", verified=len(refs) == len(ledger) and bool(ledger),
            proof="取入场落定（settle）那一帧，与命中表量的是同一时刻"
                  if len(refs) == len(ledger) else
                  f"缺：{sorted({str(r['id']) for r in ledger} - set(refs))}"))
        evidence += [
            _ev("听感：语速舒不舒服、BGM 压得够不够、有没有爆音", "listening",
                verified=False, proof="机器没有听觉——音量曲线只能证明压了，不能证明好听"),
            _ev("版式对不对味：印章有没有压住字、留白与断行好不好看", "eyeball",
                verified=False, proof="抽帧只比亮度与指纹，读不出排版，需要人这一眼"),
            _ev("内容对不对题、史实与数字准不准", "eyeball", verified=False,
                proof="spec.shots[].source 记了出处，但机器不判真伪"),
        ]
        return {
            "video": object_key,
            "duration": out["duration"],
            "width": out["width"],
            "height": out["height"],
            "fps": out["fps"],
            "title": spec.get("title") or "未命名",
            "style": spec.get("style"),
            "mix_mode": out["mix_mode"],
            "frames_total": out["frames_total"],
            "motion_ledger": ledger,
            "hitmap": hitmap_ref,
            "cached_shots": len(cached),
            "shot_frames": refs,
            "hitmap_errors": hit_errors,
            "degraded": degraded,
            "target_duration_sec": target,
            "estimated_sec": list(low_high),
            "target_warnings": warnings,
            "evidence": evidence,
            "evidence_rule": "只有 status=verified 的条目能对用户声称验过；"
                             "UNVERIFIED 的那几条要如实说没验过，别替它们背书",
        }


class PatchMotionVideoNode(RenderMotionVideoNode):
    """局部改（选区改后端）：按命中表给的指针改几格，只重烧受影响的那几镜。

    为什么复用出片节点而不是另写一条渲染路：局部改的全部价值就在于**它走的是同一条
    流水线**——同一套闸门（validate_spec、目标时长、音画同步、抽帧证据）、同一种产物
    形状、同一份按镜切片缓存。另写一条必然出现「局部改出来的这一版少了某道校验」
    这种漂移，而漂移要到抽帧时才看得见，那时像素已经烧完。

    真正的差异只有三处，全在这一层：
    * **读哪一版分镜**：不读会话的 ``plan_motion``，读那一版成片**自己带的编辑包**
      （命中表 + 表所量的 spec + 指纹）——一个会话里出过好几版时，只有产物作用域
      那份说得出屏幕上这一块对应分镜的哪一格；
    * **落在哪个产物号**：分叉成一个新作用域，旧版的成片/表/代表帧一个字节都不动
      （所以「改坏了回去」不需要重渲，直接播旧版）；
    * **多出来的那份账**：改了哪几镜、真烧了几镜、旧版还在不在原位。
    """

    name = "patch_motion_video"
    display_name = "图形科普片局部改"
    description = (
        "改一版**已经出过片**的图形科普片，不重做分镜也不整片重渲：edits 按命中表给的"
        "指针改某几格（画面上的字、图示里的数值、字幕文案），shot_sets 整镜改写"
        "（给一张卡补上它缺的那一栏），remove_shots 删镜，reorder 重排顺序。"
        "只有受影响的那几镜重新烧像素，其余镜头直接复用已有切片，然后重新拼接、混音，"
        "产出一条**新版本**（新 artifact_id）——旧版原样留着，可以逐镜比较也可以直接回去。"
        "必须带 base_artifact_id，且它自带的命中表指纹要与所改的那版分镜一致；"
        "不一致就是「拿着旧表改新片」，会被拒收，此时唯一出路是重新出片（顺带重新量表）。"
        "画幅/帧率/音色/语速/字幕形态不在本工具范围内：改它们等于每一镜都要重烧，"
        "那是重新出片而不是局部改。改完的清单照旧过分镜闸门（版式空壳、高亮词不是文案子串、"
        "这张卡不读的 visual 死键、枚举越界都会带镜号退回，且不烧像素）。"
        "渲染是分钟级：返回 status=queued/running 时须继续调 render_status"
        "（同 artifact_id）直到 done/failed")
    # 没有 DAG 上游：要改的东西全在 base_artifact_id 指向的那一版里，
    # 而 required_nodes 一旦写了 render_motion_video，拦截器补齐依赖时会**重跑一次整片出片**。
    required_nodes: list[str] = []
    require_explicit_call = True
    ephemeral = ("media_url",)
    input_schema = _obj("图形科普片局部改（只重烧受影响的镜）", {
        "base_artifact_id": {
            "type": "string",
            "description": "要改的那一版成片的产物号（render_motion_video 或"
                           " patch_motion_video 返回体顶层的 artifact_id）。"
                           "这一版必须带命中表——没有表就不知道屏幕上这一块对回哪一格",
        },
        "edits": {
            "type": "array",
            "description": "逐格改值。每条 {pointer, value[, shot]}：pointer **必须原样取自"
                           "命中表条目的 field**（形如 /shots/1/visual/levels/2/value），"
                           "不自己拼字段名；value 是这一格的新值；shot 建议带上该条目的镜号"
                           "（带了指针与镜号不符就拒收，防止拿旧表改新片）。"
                           "只认已有字段：要给一镜补上原本没有的一栏请走 shot_sets",
            "items": {"type": "object",
                      "properties": {
                          "pointer": {"type": "string"},
                          "value": {"description": "这一格的新值（类型按分镜契约：图示的 "
                                                   "value/x/y 必须是数字）"},
                          "shot": {"type": "string"}},
                      "required": ["pointer", "value"]},
        },
        "shot_sets": {
            "type": "array",
            "description": "整镜改写。每条 {shot, set}：set 里给这镜要改的那几栏"
                           f"（可改字段：{', '.join(mpatch.EDITABLE_SHOT_FIELDS)}）。"
                           "与 edits 的区别是这里**允许新增**这张卡认识的键"
                           "（比如给 archive 卡补 visual.line3）；镜号 id 不许改——"
                           "它是命中表、按镜缓存与对比帧共同的锚点",
            "items": {"type": "object",
                      "properties": {"shot": {"type": "string"},
                                     "set": {"type": "object"}},
                      "required": ["shot", "set"]},
        },
        "remove_shots": {
            "type": "array", "items": {"type": "string"},
            "description": "按镜号删镜（不许删光）。删掉的镜的旧代表帧仍在 base 那一版里",
        },
        "reorder": {
            "type": "array", "items": {"type": "string"},
            "description": "重排后的**完整**镜号清单（必须与现有镜号成套，漏一个就当错误）。"
                           "只做顺序、不改内容，所以一镜都不用重烧——拼接而已",
        },
        "bgm": {
            "type": "string",
            "description": "换配乐：material_id 或 obj: 引用（覆盖这一版 spec 里的 bgm.ref）。"
                           "配乐不进按镜缓存键，所以换它只重走混音，一镜都不重烧",
        },
        "target_duration_sec": {
            "type": "number",
            "description": "目标秒数。**缺省沿用 base 那一版当初的承诺值**（从它的渲染任务行读），"
                           "因为「把那个数字改对」不该顺手把时长承诺也换掉；显式传了才改口",
        },
        "wait_sec": {
            "type": "number",
            "description": "本次调用内联等待的秒数（缺省用 [capabilities].render_grace_sec，"
                           "上限 render_wait_max_sec）。传 0 表示只提交不等",
        },
    }, required=["base_artifact_id"])

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        sess = state.session_id
        base = str(inputs.get("base_artifact_id") or "").strip()
        if not base:
            raise ValueError(
                "局部改：必须指定 base_artifact_id——要改的是哪一版成片"
                "（出片返回体顶层的 artifact_id）。不指定就等于凭空造一版，"
                "那条路叫重新出片，不叫局部改")

        bundle = await self._bundle(sess, base)
        spec_base, whence, fp = await self._base_spec(sess, base, bundle)
        patched, report = mpatch.plan_patch(
            spec_base,
            edits=inputs.get("edits") or (),
            shot_sets=inputs.get("shot_sets") or (),
            remove_shots=inputs.get("remove_shots") or (),
            reorder=inputs.get("reorder") or ())
        # 补丁后的分镜照样过出片那道闸门：局部改不是「免检通道」，
        # 改完照样可能把高亮词改到文案外面、把图示值写成中文。
        spec, errors = mspec.validate_spec(patched)
        # 出处闸只管**本次改到的**镜：闸的职责是拦住「新写上去的猜测冒充查证过的出处」，
        # 而不是替整片重新审计一遍旧账（旧版里那些出处当初已经过一次这道闸）。
        cited_ids = [sid for sid in report["changed_shots"]
                     if any(shot.get("id") == sid and shot.get("source")
                            for shot in spec["shots"])]
        errors = errors + await self._sources(state, spec, cited_ids)

        target = _resolve_target_duration(
            state, inputs, await self._base_target(sess, base))
        artifact = await self._fork(state, sess=sess, base=base, spec=spec,
                                    target=target)

        jobs = self.storage.render_jobs
        # 与出片同一条口径：先开任务行再校验，失败必须留下一行 failed，
        # 否则「这次局部改没成」在界面与 render_status 里压根不存在。
        row = await jobs.open(sess, artifact)
        attempt = str((row or {}).get("attempt") or "")
        try:
            out = await self._render(
                state, spec, errors, target=target,
                bgm_ref=str(inputs.get("bgm") or ""),
                sess=sess, artifact=artifact, jobs=jobs, attempt=attempt,
                gate_note="局部改后的分镜")
        except Exception as exc:
            await jobs.fail(sess, artifact, str(exc)[:500], attempt or None)
            raise

        before, base_video, extra = await self._ledger(sess, base, report, out,
                                                       whence=whence, fp=fp,
                                                       cited_ids=cited_ids)
        out["evidence"][0:0] = extra
        out["patch"] = {"base_artifact_id": base, "spec_fingerprint": fp,
                        "spec_source": whence, "before_frames": before,
                        "base_video": base_video, "base_target_duration_sec": target,
                        **report}
        # ``_render`` 落终态时还没有这份补丁账（它要读渲染完的逐镜账）。这里重写一次
        # 同一行，让轮询端与刷新重放拿到的就是本次返回的那份事实——否则用户刷新之后
        # 「改了哪几镜、改前长什么样」在界面与模型眼里都不存在了，局部改看起来就和
        # 重做一版没区别。多写的是一次 UPDATE，不是一帧像素。
        await jobs.succeed(sess, artifact, out["video"], float(out["duration"]),
                           result=out, attempt=attempt or None)
        return {**out,
                "media_url": await self.storage.objects.presign_get(out["video"])}

    # ---- 那一版成片自己带的编辑包 ----

    async def _bundle(self, sess: str, base: str) -> dict[str, Any]:
        """读 base 那一版的命中表包（框 + 它所量的 spec + 指纹）。

        读不到表就拒这次局部改，而不是退化成「按字段名猜」：命中表是屏幕上这一块与
        分镜那一格之间唯一的凭据，没有它时指针就只能由调用方编，而编错的指针会
        改到另一个字段上——**改错了地方还不报错**是这条链路最贵的失败模式。
        """
        key = f"renders/{_safe(sess)}/{_safe(base)}/hitmap.json"
        try:
            if await self.storage.objects.head(key) is None:
                raise ValueError(
                    f"局部改：{base} 这一版没有命中表（{key} 读不到：出片时没量到，"
                    "或产物已过期）。没有表就不知道屏幕上这一块对应分镜的哪一格，"
                    "指针只能靠猜——请重新出片（render_motion_video 会顺带重新量表）")
            raw = b"".join([c async for c in self.storage.objects.get_stream(key)])
            bundle = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读不出来与读不到是同一件事
            raise ValueError(
                f"局部改：{base} 的命中表读不成（{type(exc).__name__}: "
                f"{str(exc)[:160]}）——请重新出片") from exc
        if not isinstance(bundle, dict):
            raise ValueError(f"局部改：{base} 的命中表不是一个对象")
        return bundle

    async def _base_spec(self, sess: str, base: str,
                         bundle: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        """→ (那一版渲的分镜, 从哪儿读到的, 指纹)。

        首选编辑包自带的 spec（与表同源，天然是「这一版片子渲的那一份」）；
        包是这次改动之前出的片（不带 spec）时才退到会话产物，而**退的那一路必须验指纹**：
        ``plan_motion`` 会被后来的分镜覆盖，拿它冒充旧版就是拿新分镜去配旧命中表，
        指针落下去改的就是别的字。
        """
        fp = str(bundle.get("spec_fingerprint") or "")
        embedded = bundle.get("spec")
        if isinstance(embedded, Mapping) and embedded:
            spec, whence = dict(embedded), "这一版成片自带的分镜（编辑包里的 spec）"
        else:
            spec, whence = {}, ""
            for scope in (base, "_default"):
                payload = await self.storage.artifacts(sess, scope).get("plan_motion")
                got = (payload or {}).get("motion_spec") if isinstance(payload, Mapping) else None
                if isinstance(got, Mapping) and got:
                    spec, whence = dict(got), f"会话产物 plan_motion（作用域 {scope}）"
                    break
            if not spec:
                raise ValueError(
                    f"局部改：读不到 {base} 这一版渲的是什么分镜——成片自带的编辑包里没有 spec，"
                    "会话里也没有 plan_motion 产物。请重新 plan_motion → render_motion_video")
        got_fp = mhit.fingerprint(spec)
        if not fp or fp != got_fp:
            raise ValueError(
                f"局部改：{base} 的命中表量的是另一版分镜（表指纹 {fp or '没有'}、"
                f"读到的分镜指纹 {got_fp}）——中间分镜被改过，指针会把新分镜的字认错。"
                "重新出片（render_motion_video，会按当前分镜重新量表）是唯一出路")
        return spec, whence, fp

    async def _sources(self, state: NodeState, spec: dict[str, Any],
                       ids: list[str]) -> list[str]:
        """只对**本次改到**的镜走出处闸（闸的本体在分镜那一层，这里只是复用同一份判据）。"""
        if not ids:
            return []
        return await _source_errors(
            state.session_id,
            [s for s in spec["shots"] if str(s.get("id")) in set(ids)])

    async def _base_target(self, sess: str, base: str) -> float | None:
        row = await self.storage.render_jobs.get(sess, base)
        res = (row or {}).get("result")
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except ValueError:
                return None
        value = res.get("target_duration_sec") if isinstance(res, Mapping) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    # ---- 分叉：新版本落进新作用域，旧版本一个字节都不动 ----

    async def _fork(self, state: NodeState, *, sess: str, base: str,
                    spec: dict[str, Any], target: float | None) -> str:
        """换一份产物作用域，并把改动同步成「会话当前分镜」；→ 新产物号。

        为什么必须换作用域：渲染任务行、成片字节、命中表、代表帧全都按 artifact_id 落位，
        沿用 base 的号等于把旧版覆盖掉——那样「改坏了回去」只能重渲一次，
        而局部改本来就是为了不重渲。

        为什么同时把改动写回 ``_default`` 作用域的 plan_motion：那一份是**模型**下一次
        调 render_motion_video 时读的。不写，就会出现「用户已经局部改过三处，
        而模型换配乐重渲时把三处改动全丢了」——那是把用户的编辑当草稿。
        旧版片子自己不受影响：它的分镜存在自己那份编辑包里。
        """
        artifact = state.artifact_id or ""
        if artifact in ("", "_default"):
            artifact = f"p{uuid.uuid4().hex[:8]}"
        payload = plan_payload(
            spec, target=target,
            next_hint="这一版局部改已入库：继续改请带**本次返回的 artifact_id** 再调 "
                      "patch_motion_video；要大改结构（加镜、换叙事）请重走 plan_motion")
        repo = self.storage.artifacts(sess, artifact)
        try:
            # 上游产物整份带进新作用域（拦截器看到 has(dep) 命中，才谈得上只跑这一步）
            await repo.clone_from(self.storage.artifacts(sess, base if base != artifact
                                                         else "_default"))
        except Exception as exc:  # noqa: BLE001 - 带不上上游不影响本次出片，如实记一笔
            state.summary.notes.append(
                f"局部改：上游产物没带进新作用域 {artifact}"
                f"（{type(exc).__name__}: {str(exc)[:120]}）——本次照常出片，"
                "后续若要接着这一版跑别的节点，可能要重跑那些上游")
        await repo.put("plan_motion", payload)
        # ``artifact`` 在这里永远不是空或 _default（上面刚换过号），所以这一步不会重复写同一行
        await self.storage.artifacts(sess, "").put("plan_motion", payload)
        state.artifact_id = artifact
        state.store = await ArtifactStore.open(repo, sess, artifact)
        return artifact

    # ---- 那份多出来的账 ----

    async def _ledger(self, sess: str, base: str, report: dict[str, Any],
                      out: dict[str, Any], *, whence: str, fp: str,
                      cited_ids: list[str]) -> tuple[dict[str, str], str, list[dict[str, Any]]]:
        """改前的代表帧 + 旧成片是否还在 + 三条局部改专属证据（插在通用证据前面）。"""
        want = report["changed_shots"] + report["removed_shots"]
        before: dict[str, str] = {}
        for sid in want:
            key = f"renders/{_safe(sess)}/{_safe(base)}/frames/{_safe(sid)}.png"
            try:
                if await self.storage.objects.head(key) is not None:
                    before[sid] = key
            except Exception:  # noqa: BLE001 - 对比帧读不到只说明帧没留住，不说明改动没发生
                continue

        base_row = await self.storage.render_jobs.get(sess, base)
        base_video = str((base_row or {}).get("video_object_key") or "")
        still_there = False
        if base_video:
            try:
                still_there = await self.storage.objects.head(base_video) is not None
            except Exception:  # noqa: BLE001
                still_there = False
        if not want:
            before_note = "本次只重排/换配乐，没有一格内容变化，所以没有对比帧"
        else:
            missing = sorted({str(s) for s in want} - set(before))
            before_note = (f"改前的代表帧已取到 {len(before)}/{len(want)}"
                           + (f"，缺 {missing}（base 那一版没留下这些镜的帧）" if missing else ""))

        rebuilt = [str(r["id"]) for r in out["motion_ledger"] if not r.get("cached")]
        expected = set(report["changed_shots"])
        extra = [
            _ev(f"局部改读的是「这一版片子自己的分镜」（{whence}，指纹 {fp}），"
                f"不是会话里可能被后来分镜覆盖过的那一份",
                "machine", verified=True,
                proof="命中表包里的 spec 与 spec_fingerprint 同一份字节写出，"
                      "并重新按指针改动前的 spec 复算过一遍"),
            _ev(f"只重烧受影响镜：改动 {len(report['changed_shots'])} 镜、"
                f"删 {len(report['removed_shots'])} 镜、"
                f"{'重排' if report['reordered'] else '未重排'}，"
                f"这次真烧的是 {len(rebuilt) or '零'} 镜"
                f"（{('、'.join(rebuilt) or '没有')}），"
                f"其余 {out['cached_shots']} 镜直接复用已有切片",
                "machine", verified=set(rebuilt) <= expected,
                proof="缓存键 = 镜头内容 + 全局开关的哈希（见 render.shot_cache_key），"
                      "没被改动的镜内容一字不差，于是整段跳过"
                if set(rebuilt) <= expected else
                f"没改动的镜里有 {sorted(set(rebuilt) - expected)} 被重烧了——"
                "缓存键漏算了某个影响画面的输入，该修 render.shot_cache_key",
            ),
            _ev(f"旧版本没有被覆盖：{base} 的成片"
                + ("仍在原位" if still_there else "字节已不在")
                + f"（{base_video or '没有记录对象键'}）；{before_note}",
                "machine", verified=still_there,
                proof="新版本落在分叉出来的新作用域，写的是另一组对象键"
                if still_there else "旧版成片不在了：回不去旧版，只能重渲"),
        ]
        if cited_ids:
            extra.append(_ev(
                f"出处闸：本次改动里 {len(cited_ids)} 镜写了出处，全部对得上本会话的检索账"
                f"（{'、'.join(cited_ids)}）",
                "machine", verified=True,
                proof="只审计本次改到的镜——闸拦的是「新写上去的猜测冒充查证过的出处」"))
        return before, base_video, extra
