"""图形科普片出片节点（remotion 通道，本仓库图形科普片的**唯一一条链**）：
分镜 spec → creation-remotion 逐镜渲 → 母带。

与 `motion_nodes.py` 那两条中性基类（``MotionRenderBase`` / ``MotionPatchBase``）的关系是
**继承**，不是另写一条：局部改/出片的全部价值都在于「走的是同一条流水线」——同一套产物
形状（``renders/{会话}/{产物}/motion.mp4`` + ``frames/`` + ``hitmap.json``）、同一份按镜
切片缓存（同一个对象前缀，所以 ``session_gc`` 的级联回收覆盖得到这条链）、同一个出片门与
同一套证据分级。另起一个类必然出现「这一版少了某道校验」，而那要到抽帧时才看得见，
那时像素已经烧完。

这个文件负责的是**只有这一条链才有**的四件事：
* **契约**：``code_spec``（一镜只剩 code/text/highlight/source/min_duration_sec + fields）
  ——画面在代码里，版式与字幕带在母版里，所以没有「选一个内置版式」那一栏；
* **出画面的执行层**：``code_render.render_code_motion``，一镜一次 HTTP 到渲染容器，
  闸①②③④⑤⑥ 在那一层落地（闸⑦ 不在这层——它是整片一层的判据，在 ``code_spec``
  的契约校验里就红了，一轮像素都不烧）；
* **命中表**：整镜一条之外，**每个声明的字段再一条**（盒由母版那一趟浏览器回读量出来，
  pointer 指向 ``/shots/i/fields/{name}/value``）——可改字段不是固定清单，
  数据来源是这一镜自己交出来的 ``fields``；
* **证据**：闸③ 帧数、闸④ 两半（非底色占比 **与** 带内填充度）、闸⑤ token 逐键等、
  闸⑥ 注入值与画面自报值逐字段等、闸⑦ 整片画法通道分布，机器量得到的都上账，
  并把「审美一致」这条如实标成闸⑤ 管不到的 UNVERIFIED。

基类继承来的那几件（``_bgm_ref`` 的归属判定、``_spotcheck`` 的像素级证据、``_publish_frames``
的产物作用域代表帧、``_work``/``_local`` 的字节中转）一行都没重写——它们与「哪条链出画面」
无关。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from agent_framework.orchestration import NodeState, _safe
from agent_framework.render_library import register_render

from ..motion import code_patch as mcode
from ..motion import code_render
from ..motion import code_spec as cspec
from ..motion import hitmap as mhit
from ..motion import remotion_client as rc
from .core_nodes import (SYNC_TOLERANCE_SEC, _JobProgress, _ev, _obj,
                         _resolve_target_duration)
from .motion_nodes import (BLOCK_LONG_FACTOR, BLOCK_SHORT_FACTOR,
                           BREATH_FLOOR_SEC, MotionPatchBase, MotionRenderBase,
                           _ObjectShotCache, read_spec_input)
from .motion_plan import plan_payload


class RenderCodeMotionVideoNode(MotionRenderBase):
    name = "render_motion_code"
    display_name = "图形科普片出片（代码镜）"
    description = (
        "没有素材也能出片：画面由模型**逐镜交 React/TSX 代码**、由渲染容器逐帧渲出来——"
        "图示、手绘质感（rough.js 斜纹）、立体几何（three.js）、公式（katex）都是真代码，"
        "没有任何内置版式可退。镜头长度仍由真实 TTS 时长决定，字幕仍逐词点亮，"
        "BGM 仍在人声起时压低。适用：需要「看得见的动画/图/3D/公式」的科普片。"
        "spec 由 plan_motion 产出（每镜的代码也在那一步过分镜闸），"
        "也可手写（每镜必须有 code；inline 那一份照样就地过出处闸，冒充查证过的出处"
        "会在烧像素之前带镜号打回）。渲染是分钟级到十分钟级："
        "返回 status=queued/running 时须继续调 render_status（同 artifact_id）直到 done/failed")

    #: 渲染容器的四个口（``{health, lint, render, fetch}``）。缺省在**用时**才取
    #: （``rc.endpoints()`` 读的是环境变量里的地址），做成类属性是为了注入：局部改要在
    #: 落盘前复跑闸①，而离线用例得能塞一段假服务进去——monkeypatch 模块函数会把别的
    #: 用例一起带跑（``code_render`` 那份 client 参数是同一条理由）。
    remotion_client: dict[str, Any] | None = None

    def _client(self) -> dict[str, Any]:
        return self.remotion_client or rc.endpoints()

    input_schema = _obj("代码镜图形科普片出片", {
        "spec": {
            **cspec.spec_schema(),
            "description": "分镜 spec（plan_motion 的产物；缺省从 Store 的 "
                           "plan_motion 取，也可按下面的结构手写——手写那份没过分镜闸，"
                           "这里会就地补走一次**出处闸**）。每一镜都必须交一段真实代码——"
                           "这条链没有默认版式可退。"
                           "**别写每镜时长**：时钟归声音，min_duration_sec 只当下限。"
                           "aspect/fps/narration/voice/rate/subtitle_mode/texture 也能走下面的"
                           "顶层开关，顶层的优先（用户在计划卡上勾的那一份）。"
                           "texture 是**整片默认档**，每一镜能用他自己的 texture 覆盖；"
                           "滤镜跑在切片之后的那一趟上、不在缓存键里，所以换档"
                           "**不重烧任何镜头**",
        },
        **cspec.knob_props(),
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
        raw = await read_spec_input(state, inputs)
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                "render_motion_code：没有 spec 参数，Store 里也没有 plan_motion 产物——"
                "先调 plan_motion 出代码分镜，或把 spec 直接传进来")
        # 顶层开关并进来再过一次校验——与 plan_motion 入库前调的是同一个 validate_code_spec：
        # 越界的值不许走到烧像素。
        spec, errors = cspec.validate_code_spec(cspec.overlay_knobs(dict(raw), inputs))
        # 出处闸在代码链上的第二个调用点：闸的本体在 plan_motion（入库前），这里只兜住
        # 「没经过 plan_motion、直接塞 inline 代码分镜」那一路——判据仍是同一份 source_gate。
        await self._gate_inline(state, inputs, spec, errors)

        sess, artifact = state.session_id, state.artifact_id or "_default"
        jobs = self.storage.render_jobs
        # 先开任务行再校验：失败必须留下一行 failed，否则「这次出片没成」在界面与
        # render_status 里压根不存在，错误就消失了。
        row = await jobs.open(sess, artifact)
        attempt = str((row or {}).get("attempt") or "")
        try:
            out = await self._render(
                state, spec, errors,
                target=_resolve_target_duration(state, inputs, None),
                bgm_ref=str(inputs.get("bgm") or ""),
                sess=sess, artifact=artifact, jobs=jobs, attempt=attempt)
        except Exception as exc:
            lint_errs = getattr(exc, 'lint_errors', None) or []
            detail = str(exc)[:500]
            if lint_errs:
                detail += "：\n" + "\n".join(
                    f"  · {e.get('rule', '?')} L{e.get('line', '?')}: {e.get('msg', '')}"
                    for e in lint_errs[:8])
            await jobs.fail(sess, artifact, detail, attempt or None)
            raise
        return {**out,
                "media_url": await self.storage.objects.presign_get(out["video"])}

    async def _render(self, state: NodeState, spec: dict[str, Any],
                      errors: list[str], *, target: float | None, bgm_ref: str,
                      sess: str, artifact: str, jobs: Any,
                      attempt: str, gate_note: str = "") -> dict[str, Any]:
        if errors:
            raise ValueError(
                f"{gate_note}代码分镜校验未通过，一帧都没开始渲（照清单改完再来）：\n"
                + "\n".join(f"  · {e}" for e in errors[:12])
                + (f"\n  · …另有 {len(errors) - 12} 条同类错误" if len(errors) > 12 else ""))

        low, high = cspec.estimate_sec(spec)
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
                f"偏离超过闸值——一镜一次整段渲染烧不起这一次：{suggest}。改完重调本工具")

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
        done = {"pct": -1}

        async def on_event(ev: dict[str, Any]) -> None:
            pct = self._percent(ev, total)
            if pct != done["pct"]:        # 心跳每 20 秒一次，同值不写库
                done["pct"] = pct
                probe(str(ev.get("stage") or "motion"), pct)

        try:
            spec_for_render = dict(spec)
            if target is not None:
                spec_for_render["target_duration_sec"] = target
            out = await code_render.render_code_motion(
                spec_for_render,
                providers=self.providers, work=work, bgm=bgm_path,
                client=self._client(),
                progress=on_event, cache=_ObjectShotCache(self.storage.workspace, sess))
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
            # 逐镜切片与中转字节是纯垃圾（一镜一份 silent.mp4/clip.mp4），成片已进
            # 对象存储——成败都收回。缓存里那份由 _ObjectShotCache 另外发布，不受影响。
            self.storage.workspace.cleanup(sess, state.artifact_id, "motion")

        result = self._result(spec, out, target=target, low_high=(low, high),
                              warnings=cspec.check_target(spec, target),
                              bgm_ref=ref, object_key=object_key,
                              frames=frames, frame_reason=frame_reason,
                              hitmap_ref=hits_key, shot_frames=shot_frames,
                              hitmap_errors=list(out.get("hitmap_errors") or []))
        # 成片入素材库（与有素材那条剪辑链走同一个 register_render）：入库失败只补一句说明——
        # 像素已经烧出来了，记账失败不该把一次成功渲染改判成失败。
        mat_id, mat_note = await register_render(
            self.storage, sess, object_key,
            title=str(spec.get("title") or "未命名"),
            duration_sec=float(out["duration"]))
        if mat_id:
            result["material_id"] = mat_id
        if mat_note:
            result.setdefault("notes", []).append(mat_note)
        await jobs.succeed(sess, artifact, object_key, float(out["duration"]),
                           result=result, attempt=attempt or None)
        return result

    @staticmethod
    def _percent(ev: dict[str, Any], total: int) -> int:
        """事件 → 百分比（0~4 校准 / 4~92 逐镜 / 93 拼接 / 96 混音）。

        这条链**没有帧级事件**（一镜一次 HTTP，中间浏览器不外露进度），所以 4~92 那一段
        按镜跳而不是连续爬。为此渲染层每 20 秒发一次 ``rendering`` 心跳——不是给用户看的
        （数值不变），是给停滞看门狗续命的：一镜跑到几十秒以上时，没有心跳的那一行会被
        误判成死任务，而像素其实还在烧。
        """
        stage = str(ev.get("stage") or "motion")
        if stage in ("rendering", "rendered", "shot"):
            return 4 + int(88 * int(ev.get("done") or 0) / total)
        if stage == "assemble":
            return 93
        if stage == "mux":
            return 96
        return 2

    async def _publish_hitmap(self, state: NodeState, spec: dict[str, Any],
                              out: dict[str, Any], *, sess: str, artifact: str,
                              frames: dict[str, str] | None = None) -> str:
        """命中表落对象存储，返回对象键（落不成回空串，不牵连这次出片）。

        每镜一份表：整镜一条 + **这一镜声明的每个字段一条**（盒由闸⑥ 那一趟浏览器回读
        顺手量好，量不到就写 ``box: null``，字段仍进表——「点不中但要能改」是合法形态，
        前端把这些落进「无空间命中」列表，不许留一片空白）。

        表本身仍然必须**自足**——带上这一版分镜、它的指纹、逐镜秒数与切片缓存键，否则
        「选区改」那一头只能回退去猜会话里的 ``plan_motion``，而一个会话可能出过好几版。
        为什么还带 chain：这一栏现在是**判旧产物**的钥匙——声明式那一路下线之前出的片子，
        表里写的不是 remotion，局部改与前端认出它才能给出「重新出一版」，而不是把补丁投进
        一条已经没有渲染器的流水线。
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
                             # 代表帧取的是末段（动画落定、字幕停在整句）
                             "frame_at_sec": round(sec, 3),
                             "cached": bool(rec.get("cached")),
                             "cache_key": rec.get("cache_key") or "",
                             "chain": rec.get("chain") or "remotion",
                             # 这一镜**实际生效**的质感档。放在逐镜账上而不是命中表条目里：
                             # 表是缓存对象（存的是加质感之前写好的那一份），账是这一次
                             # 渲染现算的——放在表里，命中缓存的那几镜会报出上一个版本的档。
                             "texture": str(rec.get("texture") or "none"),
                             "frame": (frames or {}).get(sid) or ""})
            start += sec
        payload = {
            "schema": "motion-code-hitmap/1",
            "chain": "remotion",
            "spec": spec,
            "spec_fingerprint": mhit.fingerprint(spec),
            "hit_mode": "space",
            # 白名单五条（镜粒度四条 + 字段级一条）。两类复闸口径不同，写在表上让前端与
            # 模型都看得见：改 `code` 必须**重过闸①**才允许落盘（绕开 lint 等于闸① 不存在），
            # 改 `/shots/i/fields/{name}/value` 与 `/shots/i/texture` **不重过闸①**
            # （代码字节没动，lint 结论不可能变），但必须落在声明的范围里——字段看该镜声明的
            # min/max，质感看 `code_spec.TEXTURES`；越界一律打回，不钳值。
            "editable_pointers": list(cspec.EDITABLE_POINTERS),
            "controls": cspec.knob_props(),
            "identities": {"master_hash": str(out.get("master_hash") or ""),
                           "lockfile_hash": str(out.get("lockfile_hash") or ""),
                           "gl": str(out.get("gl") or "")},
            "width": int(out["width"]), "height": int(out["height"]),
            "fps": float(out["fps"]), "duration": float(out["duration"]),
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
                "成片照常，但这次不能按镜选着改")
            return ""

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
        cached = [r for r in ledger if r.get("cached")]
        burned = [r for r in ledger if not r.get("cached")]
        refs = dict(shot_frames or {})
        pixel_ok = bool(frames) and not frames.get("black_indices")
        ratios = [float(r["empty_ratio"]) for r in ledger if r.get("empty_ratio") is not None]
        # 闸④ 的第二半（带内填充度）与第一半一样：只把**读数**摊进报告，
        # 「铺得不够开」那句在这里按 FILL_TARGET 现算——缓存镜从 meta 还原出的也是同一份
        # 读数，两类账行的形状因此完全一致（提醒文案若存在 meta 里，复用切片的那几镜
        # 会带着当初那句过时的提醒）。
        spans = {str(r["id"]): float(r["fill_span"]) for r in ledger
                 if r.get("fill_span") is not None}
        tight = sorted(sid for sid, v in spans.items() if v < code_render.FILL_TARGET)
        tokened = [r for r in ledger if r.get("tokens")]
        # 整片层三道闸（⑦ 配额 / ⑨ 跨镜同构 / ⑩ 文字对比度）都判在契约层
        # （validate_code_spec，烧像素之前），到这里的这一份只报账：打回的那一路根本
        # 走不完这次渲染，所以三道都取 [1]（提醒）而不是 [0]（错误）。三道的提醒合成
        # 一份，和计划卡上看到的是同一串。
        facts = cspec.style_facts(spec["shots"])
        style_warns = (cspec.check_style_quota(spec["shots"])[1]
                       + cspec.check_shot_structure(spec["shots"])[1]
                       + cspec.check_ink_contrast(spec["shots"])[1])
        ch_counts: dict[str, int] = {}
        for ch in facts["channels"].values():
            ch_counts[ch] = ch_counts.get(ch, 0) + 1
        # 闸⑦ 的档位从 CHANNEL_QUOTA 现算，报告层不另写一份数字（两处各写一遍就会出现
        # 「契约改了配额、回执还在报旧数」）；镜数不到最低档时 quota_want=0，意思是
        # 「这一档不判」，账上照实这么说，不许显示成「已通过」。
        quota_want = (max(k for limit, k in cspec.CHANNEL_QUOTA
                          if len(spec["shots"]) >= limit)
                      if len(spec["shots"]) >= cspec.MIN_SHOTS_FOR_QUOTA else 0)
        # 闸⑥ 的三份汇总：摊平成 (镜号, 字段名, 结论)，好让「几个字段」与「几个镜的字段」
        # 这两种问法都能从同一份账上答出来（缓存镜的 field_checks 从 meta 还原，同一形状）
        checked = [(str(r.get("id")), name, check) for r in ledger
                   for name, check in (r.get("field_checks") or {}).items()]
        matched = [x for x in checked if x[2].get("status") in ("ok", "no_geometry")]
        boxed = [x for x in checked if x[2].get("box")]
        # 闸⑧ 的整片账：措辞由 code_render.describe_layout 单源产出（含「没扫过」那条），
        # 本层只统计与摊平。`layout_missing` 里的镜号必须点名——把「这一镜没扫」写成
        # 「这一镜干净」是这条链最不该犯的那种错，所以它单独占一个数而不是混在 0 告警里。
        layout_warns = [w for r in ledger
                        for w in code_render.describe_layout(str(r.get("id")),
                                                             r.get("layout"))]
        layout_scanned = [r for r in ledger
                          if (r.get("layout") or {}).get("status") == "scanned"]
        layout_missing = sorted(str(r.get("id")) for r in ledger
                                if (r.get("layout") or {}).get("status") != "scanned")
        per_shot_ms = [float((r.get("render_timings") or {}).get("per_frame_ms") or 0.0)
                       for r in burned]

        evidence = [
            _ev(f"成片字节量到的：{got[0]}x{got[1]}@{got[2]:g}fps、{out['duration']:.1f}s、"
                f"{len(ledger)} 镜 {int(out['published_frames'])} 帧"
                f"（本次真渲 {len(burned)} 镜共 {out['frames_total']} 帧，"
                f"其余 {len(cached)} 镜直接复用切片；每镜画面按语音时长收口）",
                "machine", verified=got == want,
                proof="probe 的就是要发布的那个文件，与 spec 要求一致" if got == want
                else f"与 spec 要求 {want[0]}x{want[1]}@{want[2]:g} 不符"),
            _ev(f"闸③ 帧账对账：{len(ledger)}/{len(ledger)} 镜的切片帧数与闸② 注入的"
                f"计划帧数差 ≤1（{sum(int(r.get('planned_frames') or 0) for r in ledger)} 帧）",
                "machine", verified=bool(ledger),
                proof="对账读的是本机这文件（不是容器回读）——差一帧以上已经带镜号退回，"
                      "不静默截帧也不静默补帧" if ledger else "这份分镜没有镜头"),
            _ev(f"闸④ 画面非空（两半）：{len(ledger)} 镜里最低的非底色像素占比 "
                f"{(min(ratios) * 100 if ratios else 0):.1f}%（闸值 "
                f"{code_render.EMPTY_MIN_RATIO * 100:.0f}%），最紧那一镜的内容填充 "
                f"{(min(spans.values()) * 100 if spans else 0):.0f}% 带短边"
                f"（空壳闸值 {code_render.SPAN_MIN * 100:.0f}%、小气闸值 "
                f"{code_render.TIGHT_MIN * 100:.0f}%，目标 "
                f"{int(code_render.FILL_TARGET * 100)}% 只提醒）；取样点 "
                f"{'/'.join(f'{int(f * 100)}%' for f in code_render.EMPTY_SAMPLES)}"
                + (f"；复用切片的 {len(cached)} 镜判的是当初烧它的那一次"
                   if cached else ""),
                "machine", verified=bool(ratios) and bool(spans),
                proof="每帧裁到母版回读的画面带（字幕带在外，不参与判空），与母版实际底色"
                      "逐通道求差、按 24 级差二值化后逐字节数非零像素——基准色来自 token "
                      "回读，不是猜的 #fff。两半的分工：**空壳**要占比与小气度同时低才打回"
                      "（线条画/细网格本来就只有 1~3% 像素，只看占比会把合法风格判成空画面），"
                      "**小气**（内容只挤在带子中间一小块）占比够也拦。到这一行说明每一镜"
                      "两半都过了——打回的那一路带镜号退回，不会出现在这份账里"
                if spans else "没有一次真渲（整片命中缓存），这一条没有本次数值"),
            _ev(f"闸⑤ 跨镜一致性：{len(tokened)}/{len(ledger)} 镜读回母版 token，"
                f"{len(tokened[0]['tokens']) if tokened else 0} 个键逐镜比对**容差 0** 全等",
                "machine", verified=bool(tokened) and len(tokened) == len(ledger),
                proof="字号/字幕带/边距/安全区是母版常量，两镜取值不等就带镜号退回；"
                      "这条保证的是**数值一致**"
                if len(tokened) == len(ledger) else
                f"缺回读：{sorted({str(r['id']) for r in ledger} - {str(r['id']) for r in tokened})}"),
            _ev(f"闸⑥ 字段级对账：{len(checked)} 个声明字段里 {len(matched)} 个"
                f"**画面上自报的值与注入的值逐字相等**"
                f"（其中 {len(boxed)} 个还量到了像素盒，可供点元素反查字段）",
                "machine",
                verified=bool(checked) and len(matched) == len(checked),
                proof="期望值取的就是缓存键里那一份扁表，自报值来自母版那一趟浏览器回读，"
                      "两者同一刻度；只有 mismatch 判红，"
                      "没自报（unreported）与量不到盒（no_geometry）照实记在这一行"
                if checked else
                "这份分镜没交出可改的旋钮（每镜 fields 全空），闸⑥ 没有可对账的东西"),
            _ev(f"闸⑦ 风格配额：{len(spec['shots'])} 镜用了 {len(facts['variety'])} 种画法通道（"
                + "、".join(f"{ch}×{k}" for ch, k in sorted(ch_counts.items())) + "）"
                + (f"，这一档要求 ≥{quota_want} 种" if quota_want else "，镜数没到最低档 "
                   f"{cspec.MIN_SHOTS_FOR_QUOTA}，这一档不判配额")
                + (f"；纯文字/纯色块镜 {len(facts['text_only'])} 个"
                   f"（{'、'.join(facts['text_only'])}）" if facts["text_only"] else ""),
                "machine",
                verified=bool(quota_want) and len(facts["variety"]) >= quota_want,
                proof="通道按**代码字节里的 import** 归类（roughjs / three+@react-three/fiber / "
                      "katex / @remotion 的 paths·shapes 或裸 SVG 标记 / 只剩文字色块），"
                      "判在 validate_code_spec 里、烧像素之前；这里报的是同一份账。"
                      "quota_want=0 时这一条是**未判**不是通过——短片子照样能全是文字"
                if ch_counts else "这份分镜没有镜头"),
            _ev(f"闸⑧ 版面回读：{len(layout_scanned)}/{len(ledger)} 镜在探针帧真扫过画面带，"
                f"命中 {len(layout_warns)} 条告警（叠字 / 被画面带切 / 引线悬空三类，"
                f"逐条点名在 layout_warnings 里）"
                + (f"；没扫的镜：{'、'.join(layout_missing)}" if layout_missing else ""),
                "machine", verified=bool(ledger) and len(layout_scanned) == len(ledger),
                proof="**只记账不拦截**：判据是母版在探针帧（与闸⑥ 同一帧、同一条通道）量回来的 "
                      "画面带内 DOM 几何，阈值随读数一起回传（overlap_ratio/clip_px/"
                      "anchor_ratio/leader_min_px，全在 tokens.ts 的 LAYOUT_PROBE）。"
                      "这一路故意不设红——刻意叠字、引线指向画面外都可能是合法设计，"
                      "误报的代价是整条通道"
                      "没人再看。扫过的镜 findings 为空才是「干净」；没扫的镜单独点名，"
                      "不混进 0 告警里"
                if ledger else "这份分镜没有镜头"),
            _ev(f"闸⑨ 跨镜同构：{len(spec['shots'])} 镜里"
                + (f"有 {len(facts['same_shape_groups'])} 组代码结构几乎一样（"
                   + "；".join("、".join(g) for g in facts["same_shape_groups"]) + "）"
                   if facts["same_shape_groups"] else
                   "没有一组（≥3 镜）抽掉文字与色相后剩同一副骨架"),
                "machine",
                verified=not facts["same_shape_groups"],
                proof="判据是把 import、注释、字符串字面量与十六进制色号全抹掉之后的"
                      "token 多重集相似度（≥"
                      f"{int(cspec.STRUCT_SAME * 100)}% 算同构）。**打回**判在 "
                      "validate_code_spec（烧像素之前），这里报的是同一份账；"
                      "换色相、换文案骗不过这条——今天拿仓库里 152 对真实 demo 校准过："
                      "确实不同的两镜最高 0.736，只换文字与配色的那一对是 1.000"),
            _ev(f"闸⑩ 文字对比度：{len(facts['contrast'])} 镜量出了文字与底的组合，"
                + (f"最低 {min(c['ratio'] for c in facts['contrast']):.2f}:1"
                   if facts["contrast"] else "本片没有可量的字面颜色")
                + (f"；认不出底色的镜：{facts['contrast_unjudged']} 镜"
                   if facts["contrast_unjudged"] else ""),
                "machine",
                verified=bool(facts["contrast"])
                and all(c["ratio"] >= cspec.CONTRAST_MIN for c in facts["contrast"]),
                proof="按 WCAG 2.1 的相对亮度算比值，低于 "
                      f"{cspec.CONTRAST_MIN:.0f}:1 **打回**、低于 "
                      f"{cspec.CONTRAST_READABLE:.1f}:1 只提醒。取的是「这镜所有底色里"
                      "最好读的那一个」，所以深卡白字压在浅页面上不算违规。R3F 的 "
                      "material/light 颜色不进这一判（那不是文字）；**只从 palette 变量"
                      "取色而没有字面颜色时判不了**，那一条照实记在 "
                      "style_contrast_unjudged，不许当成已通过"),
            _ev("逐镜提醒（不打回）：" + "；".join(list(style_warns)
                + ([f"这几镜的内容没铺满画面带（短边的 "
                    f"{int(code_render.FILL_TARGET * 100)}% 是目标）："
                    + "、".join(f"{sid} {spans[sid] * 100:.0f}%" for sid in tight)]
                   if tight else []))
                if (style_warns or tight) else
                "逐镜提醒（不打回）：没有——文案的具体项与画面带铺满度都在目标线上",
                "machine", verified=True,
                proof="这一份合流了闸⑦/⑨/⑩ 三道的**提醒**（各自的打回早就在 "
                      "validate_code_spec 拦住了，走不到这里），加上铺满度清单。其中"
                      "这两条**故意不做硬闸**：文案的具体项只看得见数字/引号术语/拉丁缩写，"
                      "地名人名器物名词法认不出，误伤的代价是一整轮重规划；铺满度低于 "
                      f"{code_render.FILL_TARGET:.0%} 但高于小气闸 "
                      f"{code_render.TIGHT_MIN:.0%} 的，是「构图小气」这种审美问题，"
                      "机器拦不得（闸④ 拦的是空壳与挤成一坨）。所以它们落成清单，"
                      "由人看完再决定改不改"),
            _ev(f"时钟归声音：{len(held)}/{len(voiced)} 个带声镜头的长度 ≥ 自己语音时长 + "
                f"{BREATH_FLOOR_SEC}s，没有一镜在话说到一半时切走",
                "machine", verified=bool(voiced) and len(held) == len(voiced),
                proof="每镜秒数由 TTS 实测回填，再折算成 durationInFrames 注入母版"
                if voiced else "这份分镜没有带旁白的镜头（narration=false）"),
            _ev(f"字幕逐词点亮：{words} 个词按 fps 折成帧号交给母版字幕带",
                "machine", verified=bool(voiced),
                proof="词时间戳来自 TTS 返回，落在与 durationInFrames 同一刻度上"
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
        texture = str(out.get("texture") or "none")
        tchain = str(out.get("texture_filter") or "")
        tex_by_shot = {str(k): str(v or "none")
                       for k, v in (out.get("texture_by_shot") or {}).items()}
        textured = {sid: kind for sid, kind in tex_by_shot.items() if kind != "none"}
        if not textured:
            evidence.append(_ev(
                "质感：全片没加（逐镜都是 none，切片原样进拼接）", "machine",
                verified=True,
                proof="整片默认看顶层 texture，每一镜可用自己的 texture 覆盖；"
                      "单镜写 \"none\" 是**显式不给这一镜加**（与留空「跟整片默认」不是一回事）。"
                      "一档都不加时那一趟滤镜整个跳过"))
        else:
            by_kind: dict[str, list[str]] = {}
            for sid, kind in sorted(textured.items()):
                by_kind.setdefault(kind, []).append(sid)
            evidence.append(_ev(
                f"逐镜质感：{len(textured)}/{len(ledger)} 镜加了档——"
                + "、".join(f"{kind} 档 {len(ids)} 镜（{'、'.join(ids)}）"
                            for kind, ids in sorted(by_kind.items()))
                + (f"；实际消费的滤镜链 {tchain[:70]}…" if tchain else "，但滤镜链是空的"),
                "machine", verified=bool(tchain),
                proof="质感跑在**每一镜的切片之后、拼接之前**（按镜各跑一遍），且**故意不进**"
                      "缓存键：缓存存的是加质感之前的像素，所以换任何一镜的档都 0 镜重烧，"
                      "命中切片的镜照样被加上质感"
                if tchain else
                f"加了档却没拿到滤镜串——成片是原片，请核对 "
                f"render.TEXTURE_FILTERS 里有没有这一档"))
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
                                "取样点撞进了同一张静止版式——代码镜静止停着也合法，"
                                "要确认动感请看中间几秒"))
        else:
            evidence.append(_ev("抽帧看有没有黑屏", "frame", verified=False,
                                proof=frame_reason))
        evidence += [
            _ev(f"渲染身份：母版 {out['master_hash']}／依赖 {out['lockfile_hash']}／"
                f"WebGL 档 {out['gl'] or '未报'}",
                "machine", verified=True,
                proof="两段哈希由 /healthz 现报、并进每一镜的缓存键：改一次母版 token 或"
                      "换一版依赖＝全片重烧（像素确实变了，这是正确行为不是缺陷）"),
            _ev("逐镜账每行都带 chain=remotion（这一版画面出自代码链）",
                "machine", verified=all(r.get("chain") == "remotion" for r in ledger),
                proof="这一栏现在是判旧产物的钥匙：声明式那一路出的片子写的是别的值，"
                      "而那条流水线已经下线，局部改认出它才能要求重新出一版"),
            _ev(f"性能：本次真渲 {len(burned)} 镜，每帧毫秒最高 "
                f"{out.get('per_frame_ms_max') if out.get('per_frame_ms_max') is not None else '未报'}"
                + ("（软件 WebGL：3D 镜慢在这里）" if float(
                    max(per_shot_ms) if per_shot_ms else 0) > 0 else ""),
                "machine", verified=out.get("per_frame_ms_max") is not None,
                proof="计时来自容器回读的 timings，不是本地掐表"
                if out.get("per_frame_ms_max") is not None
                else "整片命中切片缓存，本次没有真渲，所以没有每帧毫秒"),
        ]
        tables = out.get("hitmap") or {}
        entries = [e for tab in tables.values() for e in (tab.get("entries") or [])]
        field_entries = [e for e in entries if e.get("kind") == "field"]
        evidence.append(_ev(
            f"按镜/按字段选着改的命中表：{len(tables)} 镜 {len(entries)} 条可点条目"
            f"（整镜 {len(entries) - len(field_entries)} 条 + 字段 {len(field_entries)} 条，"
            f"其中 {sum(1 for e in field_entries if e.get('box'))} 条量到了像素盒）。"
            f"指针白名单 {len(cspec.EDITABLE_POINTERS)} 条：{'、'.join(cspec.EDITABLE_POINTERS)}",
            "machine", verified=bool(hitmap_ref),
            proof="盒是母版那一趟浏览器回读顺手量的（与闸⑥ 同一帧）；量不到的字段"
                  "**照样进表**（box=null、field_status 记着为什么），前端把它们落进"
                  "「无空间命中」列表——点不中但要能改是合法形态，不许留空白。"
                  "表里带这一版 spec、它的指纹、逐镜秒数与切片缓存键，局部改那一头靠指纹"
                  "判这张表是不是当前这版量出来的，对不上就拒收"
            if hitmap_ref else "；".join(list(hitmap_errors or [])[:3])
            or "命中表没有落进对象存储"))
        evidence.append(_ev(
            f"按镜切片缓存：{len(cached)}/{len(ledger)} 镜直接复用了已有切片"
            f"（省掉 {len(cached)} 次整段渲染）",
            "machine", verified=True,
            proof="缓存键 =「代码字节 + 该镜文案/点亮词/下限秒 + **字段值扁表**（name→value）"
                  "+ 整片开关 + 母版哈希 + 依赖锁哈希」；改一个字或改一个字段值即未命中，"
                  "只重烧那一镜。字段的 label/min/max/step 不进键（改控件边界不动像素），"
                  "换 BGM 与换质感都不动切片"
            if cached else
            "首次出片，每镜都真渲了一遍；同一会话里再渲相同镜头就会命中"))
        evidence.append(_ev(
            f"每镜代表帧已留存 {len(refs)}/{len(ledger)}（时间线缩略图与改前改后对比取它）",
            "machine", verified=len(refs) == len(ledger) and bool(ledger),
            proof="取切片末段那一帧（动画落定、字幕停在整句）"
                  if len(refs) == len(ledger) else
                  f"缺：{sorted({str(r['id']) for r in ledger} - set(refs))}"))
        evidence += [
            _ev("听感：语速舒不舒服、BGM 压得够不够、有没有爆音", "listening",
                verified=False, proof="机器没有听觉——音量曲线只能证明压了，不能证明好听"),
            _ev("画面好不好看：代码写出来的构图、留白、动效节奏", "eyeball",
                verified=False,
                proof="闸④ 只证明画面上有东西、闸⑤ 只证明数值一致，两者都读不出审美——"
                      "这条链的版式统一在母版一层，构图与动效节奏由每镜代码自写，"
                      "没有模板替它兜底"),
            _ev("内容对不对题、史实与数字准不准", "eyeball", verified=False,
                proof="spec.shots[].source 记了出处，但机器不判真伪"),
            _ev("代码里那一镜该不该用 3D/公式：选错表达手段不报错，只是慢或难看",
                "eyeball", verified=False,
                proof="闸① 管得住危险 API，管不住「用四棱锥讲一个比喻」"),
        ]
        return {
            "video": object_key,
            "duration": out["duration"],
            "width": out["width"],
            "height": out["height"],
            "fps": out["fps"],
            "title": spec.get("title") or "未命名",
            "chain": "remotion",
            "mix_mode": out["mix_mode"],
            "texture": texture,
            "frames_total": out["frames_total"],
            "motion_ledger": ledger,
            "hitmap": hitmap_ref,
            "cached_shots": len(cached),
            "shot_frames": refs,
            "hitmap_errors": list(hitmap_errors or []),
            "degraded": degraded,
            "empty_ratios": out.get("empty_ratios") or {},
            "fill_spans": out.get("fill_spans") or {},
            # 整片层三道闸判在契约层，但**账要出在两处**：plan_motion 那份是「改之前就该
            # 看得见」，这一份是「已经渲出来了，画面用的到底是哪几条通道、有没有同构、
            # 文字压在什么底色上」。两份读的是同一个 style_facts，所以不会出现计划卡说
            # 两种画法、回执说三种。
            "style_channels": facts["channels"],
            "style_text_only": facts["text_only"],
            "style_same_shape": facts["same_shape_groups"],
            "style_contrast": facts["contrast"],
            "style_contrast_unjudged": facts["contrast_unjudged"],
            "style_warnings": list(style_warns),
            "fill_warnings": [f"{sid}：内容只铺开画面带短边的 {spans[sid] * 100:.0f}%"
                              f"（目标 {int(code_render.FILL_TARGET * 100)}%）"
                              for sid in tight],
            # 闸⑧：告警逐条带镜号（describe_layout 单源措辞）；`layout_unscanned` 单独一栏，
            # 因为「这一镜没扫」与「这一镜扫了且干净」在回执上必须是两句话。
            "layout_warnings": layout_warns,
            "layout_unscanned": layout_missing,
            "master_hash": out["master_hash"],
            "lockfile_hash": out["lockfile_hash"],
            "gl": out["gl"],
            "per_frame_ms_max": out.get("per_frame_ms_max"),
            "target_duration_sec": target,
            "estimated_sec": list(low_high),
            "target_warnings": warnings,
            "evidence": evidence,
            "evidence_rule": "只有 status=verified 的条目能对用户声称验过；"
                             "UNVERIFIED 的那几条要如实说没验过，别替它们背书",
        }


# ---------------------------------------------------------------------------
# 局部改（代码镜链）：五条指针 + 只重烧受影响的那几镜
# ---------------------------------------------------------------------------

class PatchCodeMotionVideoNode(MotionPatchBase, RenderCodeMotionVideoNode):
    """代码镜这条链的「选区改」：按指针下补丁，只有受影响的那几镜重烧。

    MRO 是这个类能成立的关键：``process`` / ``_bundle`` / ``_base_spec`` / ``_fork`` /
    ``_ledger`` 全来自 ``MotionPatchBase``（读哪一版、分叉、改动账——这些与哪条链出画面
    无关），``_render`` / ``_result`` / ``_publish_hitmap`` / ``_client`` 全来自
    ``RenderCodeMotionVideoNode``（八道闸、代码链的证据、字段级命中表、版面回读告警）。本类只交出这一条
    链的数据源（校验器、装配器、产物形状、链身份、两处要点名工具名的文案），
    外加一个落盘前的闸① 复跑。

    与出片那一路最实质的一条差别是**闸① 什么时候复跑**：改了 ``code`` 才跑，而且必须
    在分叉落盘**之前**跑；改字段值不跑（代码字节没动，lint 的结论不可能变——那一次外呼
    是白等的好几秒，而这条链的卖点恰恰是「改一个数字只用重烧一镜」）。
    """

    name = "patch_motion_code"
    display_name = "图形科普片局部改（代码镜）"
    description = (
        "改一版**已经出过片**的代码镜图形科普片，不重做分镜也不整片重渲：edits 按命中表"
        "给的指针改某几格（这一镜的代码、文案、出处、质感档，以及**这一镜自己声明出来的"
        "字段值**），"
        "shot_sets 整镜改写，remove_shots 删镜，reorder 重排顺序。只有受影响的那几镜重新烧"
        "像素，其余镜头直接复用已有切片，然后重新拼接、混音，产出一条**新版本**"
        "（新 artifact_id）——旧版原样留着，可以逐镜比较也可以直接回去。"
        "指针白名单五条：``/shots/i/fields/{name}/value``（改一个声明字段的值，按该字段"
        "声明的 type 校验、越界**打回不钳值**，**不重过闸①**）、``/shots/i/code``（改代码，"
        "**必须重过闸①** 才允许落盘）、``/shots/i/texture``（换这一镜的质感档，取值必须在 "
        "film/tv/glow/bleach/none 里，**或写空串表示这一镜收回表态、重新跟随整片默认**——"
        "`none` 是明确不加、`\"\"` 是跟整片走，两件事），"
        "**不重过闸①**、一帧不重烧）、``/shots/i/text``、"
        "``/shots/i/source``。"
        "必须带 base_artifact_id，且它自带的命中表要与所改的那版分镜指纹一致、链身份是 "
        "remotion；不一致就是「拿着旧表改新片」或「拿别的链的表下补丁」，会被拒收。"
        "画幅/帧率/旁白开关/音色/语速/字幕形态不在本工具范围内：改它们等于每一镜都要重烧，"
        "那是重新出片而不是局部改——质感与配乐是例外，两者都在烧完像素之后的那一趟上"
        "（配乐在母带合流，质感在每一镜切片的滤镜上、且不在缓存键里），"
        "换它们一镜都不重烧，所以本工具收 texture 与 bgm。"
        "改完的清单照旧过分镜闸门（代码没交、高亮词不是文案子串、字段名重复或取值越界都会"
        "带镜号退回，且不烧像素）。渲染是分钟级：返回 status=queued/running 时须继续调 "
        "render_status（同 artifact_id）直到 done/failed")
    payload_builder = staticmethod(plan_payload)
    patch_validator = staticmethod(cspec.validate_code_spec)
    patch_planner = staticmethod(mcode.plan_code_patch)
    patch_chain = "remotion"
    patch_render_node = "render_motion_code"
    patch_cache_key_fn = "code_spec.shot_cache_key"
    patch_next_hint = (
        "这一版局部改已入库：继续改请带**本次返回的 artifact_id** 再调 patch_motion_code；"
        "要大改结构（加镜、换叙事）请重走分镜再调 render_motion_code")

    input_schema = _obj("代码镜图形科普片局部改（只重烧受影响的镜）", {
        "base_artifact_id": {
            "type": "string",
            "description": "要改的那一版成片的产物号（render_motion_code 或"
                           " patch_motion_code 返回体顶层的 artifact_id）。"
                           "这一版必须带命中表——没有表就不知道屏幕上这一块对回哪一格",
        },
        "edits": {
            "type": "array",
            "description": "逐格改值。每条 {pointer, value[, shot]}：pointer **必须原样取自"
                           "命中表条目的 pointer 键**（整镜那条给 /shots/i/code 这类，"
                           "字段那条给 /shots/i/fields/{name}/value），不自己拼字段名；"
                           "value 是这一格的新值；shot 建议带上该条目的镜号"
                           "（带了指针与镜号不符就拒收，防止拿旧表改新片）。"
                           "字段值按该字段声明的 type 校验并落在 min/max 内，"
                           "**越界打回不钳值**；要给一镜补上原本没声明的字段请走 shot_sets",
            "items": {"type": "object",
                      "properties": {
                          "pointer": {"type": "string"},
                          "value": {"description": "这一格的新值（number 写数字、"
                                                   "color 写 #RRGGBB、bool 写 true/false、"
                                                   "select 写声明过的候选值之一）"},
                          "shot": {"type": "string"}},
                      "required": ["pointer", "value"]},
        },
        "shot_sets": {
            "type": "array",
            "description": "整镜改写。每条 {shot, set}：set 里给这镜要改的那几栏"
                           f"（可改字段：{', '.join(mcode.EDITABLE_SHOT_FIELDS)}）。"
                           "与 edits 的区别是这里**允许新增**——包括给一镜补上原本没声明的"
                           "字段（fields 整份重写）；镜号 id 不许改——它是命中表、按镜缓存"
                           "与对比帧共同的锚点。改了 code 照样重过闸①，验不过就不落盘",
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
        "bgm_volume": {
            "type": "number", "minimum": 0, "maximum": 0.6,
            "description": "配乐音量（0~0.6）。与 bgm 同层，不进按镜缓存键，"
                           "改它只重走混音一镜都不重烧。不传沿用 base 那一版的值",
        },
        "bgm_duck": {
            "type": "boolean",
            "description": "人声起时压低配乐（true/false）。不传沿用 base 那一版的值",
        },
        "texture": {
            "type": "string", "enum": list(cspec.TEXTURES),
            "description": "换**整片默认**质感档（覆盖这一版 spec 里的 texture）；"
                           "只有个别镜要换请用 edits 的 /shots/i/texture。与配乐同理："
                           "它不进按镜缓存键、跑在切片之后的那一趟滤镜上，所以换档"
                           "**一镜都不重烧**（命中缓存的镜照样被加上新档）；"
                           "不传就沿用 base 那一版的档",
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

    async def _pre_gate(self, state: NodeState, *, base: dict[str, Any],
                        spec: dict[str, Any], report: dict[str, Any],
                        errors: list[str]) -> None:
        """改了代码字节的那几镜**重过闸①**，验不过就不落盘（旧版片子原样还在）。

        三条口径各自的理由：
        * **只比代码字节**（``changed_codes``）：纯改字段值/文案不重跑——代码没动，
          lint 结论不可能变，而那一次外呼要等好几秒，正是这条链要省掉的钱；
        * **分镜本身还不合法时不去外呼**：拿一份连字段名单都对不上的 spec 交给闸①，
          只会把「你改错了哪一格」掩盖成「lint 报错」；
        * **跑在 ``_fork`` 之前**：这一版新代码只要落进产物作用域，界面上就是一条可以
          播的新版本；绕过闸① 让它落盘，等于闸① 只拦第一次出片、不拦改动。
        """
        if errors or not mcode.changed_codes(base, spec):
            return
        client = self._client()
        lint_errors = await mcode.relint_codes(
            base, spec,
            lint=lambda code, names: asyncio.to_thread(client["lint"], code, names))
        if lint_errors:
            raise ValueError(
                "闸① 复跑未通过，这一版补丁没有落盘（旧版片子与它的字节一个不动）：\n"
                + "\n".join(f"  · {e}" for e in lint_errors[:12])
                + (f"\n  · …另有 {len(lint_errors) - 12} 条同类问题"
                   if len(lint_errors) > 12 else "")
                + "\n  （改代码那条指针必须过 lint；只想改画面上的数字请改对应的"
                  "字段值指针，那条不重过闸①）")
