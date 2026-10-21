"""图形科普片出片节点的**共用流水线**：读哪一版分镜 → 校验 → 复闸 → 分叉 → 开任务行 → 渲 → 那份账。

这个文件里没有一个类会被注册成工具。注册的是 `motion_code_nodes.py` 的
``render_motion_code`` / ``patch_motion_code``，它们从这里继承与「哪条链出画面」无关的那半段：
产物形状（``renders/{会话}/{产物}/motion.mp4`` + ``frames/`` + ``hitmap.json``）、按镜切片缓存
的对象实现（同一个对象前缀，所以 ``session_gc`` 的级联回收覆盖得到）、进度上账与看门狗、
证据分级、局部改的指纹核验与分叉。

链专属的那半段（契约、闸①~⑥、命中表条目、代码链的证据）全在子类里。两条出片路曾经共用
这个文件（声明式那一路在 2026-10-10 整条删掉），留下的钩子属性因此只有一个赋值点——仍然
留在类属性上而不是写死进流程：**流程一旦开始出现「是代码链就跳过这一步」，两条链就会各自
漏掉不同的闸**，而那要到抽帧时才看得见，那时像素已经烧完。现在这个位置管住的是下一族链。

节点层负责的四件事（渲染层一概不知道）：
* **入参把关**：spec 先过校验器，枚举越界、高亮词不在文案里这类错必须在**烧像素之前**
  回清单——一次渲染是分钟级到十分钟级，跑完再报错不叫把关，叫事故；
* **进度上账**：逐镜/逐帧事件折成 render_jobs 的百分比（同值去重，146 帧不该写 146 行）。
  这一步不只是给界面看：看门狗按「无进展时长」判死，不报进度会被误杀；
* **产物落库**：成片经 workspace.publish 进对象存储，payload 只留对象键；``media_url``
  是会过期的 presigned 直链，标 ephemeral 只回调用方——与 render_video 同一条契约；
* **证据分级**：机器量得到的（字节、像素、每镜有没有放完自己的旁白）标 verified，
  听感与「版式好不好看、史实对不对」永远标 UNVERIFIED——不许替验不了的条目背书。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from agent_framework.orchestration import ArtifactStore, NodeState, _safe
from agent_framework.storage import to_ref

from ..motion import hitmap as mhit
from ..motion import render as motion
from .core_nodes import (StoryNode, _ev, _frame_spotcheck,
                         _resolve_target_duration, new_patch_scope_id)
from .motion_plan import settle_sources

#: 目标时长的硬闸倍数：估算区间整体偏离到这个份上就别开工——一次渲染是分钟级，
#: 「先渲二十分钟再告诉你比目标长一倍」不是把关。
BLOCK_LONG_FACTOR = 1.6
BLOCK_SHORT_FACTOR = 0.5

#: 每镜至少比自己的声音多留这么多秒（渲染层的呼吸余量是 0.10+0.35，留 0.05 给取整）
BREATH_FLOOR_SEC = 0.40


async def read_spec_input(state: NodeState, inputs: dict[str, Any]) -> Any:
    """``spec`` 的三种来路：入参 → Store 里的 ``plan_motion`` → repo 里那一份（跨轮回）。

    抽成一个函数而不是每个出片节点各写一遍：这三层回落是真机一件件补出来的（同一条
    run 里 Store 还没落、跨轮续跑时只有 repo 有），少一层就变成「明明分过镜却说没分过」。
    出片与局部改读的是同一份 ``plan_motion``，取数逻辑必须同源。
    拿不到时回 ``None``——由调用方决定报哪句错（错误文案要带自己的节点名）。
    """
    raw = inputs.get("spec")
    if isinstance(raw, Mapping) and raw:
        return raw
    raw = (await state.store.get("plan_motion") or {}).get("motion_spec")
    if isinstance(raw, Mapping) and raw:
        return raw
    repo = getattr(state.store, "repo", None)
    if repo is not None:
        fresh = await repo.get("plan_motion")
        if isinstance(fresh, Mapping):
            return fresh.get("motion_spec")
    return None


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


class MotionRenderBase(StoryNode):
    """出片类节点的共用件：inline 出处闸、BGM 归属、抽帧证据、代表帧发布。

    这一族自己**不渲像素**：``_render`` / ``_result`` / ``_publish_hitmap`` / ``_percent``
    由具体链的子类给出（见 ``motion_code_nodes.RenderCodeMotionVideoNode``）。
    """

    # media_url 是一条一小时后就读不通的 presigned 直链：只回调用方，不落进共享库
    ephemeral = ("media_url",)
    required_nodes: list[str] = ["plan_motion"]

    async def _gate_inline(self, state: NodeState, inputs: dict[str, Any],
                           spec: dict[str, Any], errors: list[str]) -> None:
        """渲染口拿到的是**没过过分镜闸的那一份**时，就地补走一次出处对账。

        为什么需要：``required_nodes=["plan_motion"]`` 保证的是「这个会话分过镜」，
        不是「本次渲的就是那份分镜」。``read_spec_input`` 的第一层回落是**入参里的
        inline spec**，它盖在 Store 那份上面——于是「分镜时查过出处，渲时换成一套没查过的
        数字」这条路本来一直畅通（真机查出来时两条图形链都通）。这不是往渲染节点里再写
        一遍判据：判据只有 ``motion_plan.settle_sources`` 这一份，这里多出来的只是一个调用点。

        2026-10-19 起这里也**只降级、不打回**：对不上账的出处被标成「未核实」后照常
        渲染（理由与代价见 ``motion_plan._mark_source_unverified``）——出片口拦一次，
        烧的是一整轮已经写好的镜头与十分钟等待；账面上那一句「未核实」同样不会被观众
        误读成查证过。

        分镜本身已经不合法时不走这一步：那种 spec 交给对账只会把「校验没通过」掩盖成
        「出处有问题」，而调用方需要先看到前者。
        """
        inline = inputs.get("spec")
        if errors or not (isinstance(inline, Mapping) and inline):
            return
        notes, _states = await settle_sources(state.session_id, spec.get("shots") or [])
        for note in notes:
            state.summary.notes.append(note)

    async def _bgm_ref(self, state: NodeState, value: str) -> str:
        """bgm 入参 → 能取到字节的引用。三种写法都收：``obj:`` 引用、素材 material_id、曲名。

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
            # 用户说的常常是曲名（「背景音乐用 We Never」），模型就原样填进来——
            # 按文件名在本人可见的音频里找一首最像的（与 search_media 同一个检索口）。
            # 真机 run 79dcdc0ec0a8 传的就是 bgm="We Never"，曲库里那首叫
            # 「DJariium, DJAQ - We Never (高燃鼓點版).mp3」。
            hits = await self.storage.materials.search(
                state.user_id, value, conv_id=state.conversation_id or None,
                kinds=("audio",), limit=3)
            if hits:
                return to_ref(hits[0]["object_key"])
            raise ValueError(f"没有这条配乐素材：{value}（按 material_id 与曲名都没找到）")
        return to_ref(got[0]["object_key"])

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


class MotionPatchBase(MotionRenderBase):
    """局部改（选区改后端）的共用编排：按指针改几格，只重烧受影响的那几镜。

    为什么复用出片节点而不是另写一条渲染路：局部改的全部价值就在于**它走的是同一条
    流水线**——同一套闸门（分镜校验、目标时长、音画同步、抽帧证据）、同一种产物形状、
    同一份按镜切片缓存。另写一条必然出现「局部改出来的这一版少了某道校验」
    这种漂移，而漂移要到抽帧时才看得见，那时像素已经烧完。

    真正的差异只有三处，全在这一层：
    * **读哪一版分镜**：不读会话的 ``plan_motion``，读那一版成片**自己带的编辑包**
      （命中表 + 表所量的 spec + 指纹）——一个会话里出过好几版时，只有产物作用域
      那份说得出屏幕上这一块对应分镜的哪一格；
    * **落在哪个产物号**：分叉成一个新作用域，旧版的成片/表/代表帧一个字节都不动
      （所以「改坏了回去」不需要重渲，直接播旧版）；
    * **多出来的那份账**：改了哪几镜、真烧了几镜、旧版还在不在原位。

    下面五样是**链专属数据源**，由具体链的子类赋值（见
    ``motion_code_nodes.PatchCodeMotionVideoNode``）：装配器、校验器、产物形状装配函数、
    链身份，以及三处要点名工具名的文案。做成类属性而不是在 ``process`` 里 ``if 链名: …``
    的理由在文件头。
    """

    payload_builder: Any
    patch_validator: Any
    patch_planner: Any
    #: base 那一版编辑包必须声明的链。
    patch_chain: str
    #: 退回文案里那句「请重新出片」要点名的工具名。
    patch_render_node: str
    #: 证据文案里那句「缓存键在哪把函数里算」的出处。它不参与任何判定，只是必须说真话。
    patch_cache_key_fn: str
    #: 分叉之后写给模型的下一次指引。各链点名自己的 patch 节点，共用一份文案
    #: 就是教它调错一条链的工具——那会撞上「链身份不符」，看起来像 bug。
    patch_next_hint: str

    # 没有 DAG 上游：要改的东西全在 base_artifact_id 指向的那一版里，
    # 而 required_nodes 一旦写了出片节点，拦截器补齐依赖时会**重跑一次整片出片**。
    required_nodes: list[str] = []
    require_explicit_call = True
    #: 告诉 MCP 入口：本节点会在体里分叉出一个新产物作用域，渲染任务行必须开在**那个新号**上，
    #: 而不是调用方传来的号（模型通常不传，于是落到 ``_default``＝base 那一版）。
    #: 不预发就是「任务行开在 base、终态写在新版」：base 的出片账被抹回 queued，
    #: 一次成功的局部改在轮询端等不到终态、被看门狗按停滞判死。
    forks_artifact_scope = True

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        sess = state.session_id
        base = str(inputs.get("base_artifact_id") or "").strip()
        if not base:
            # 这条文案要回答的是**模型下一步该调哪个工具**，不只是说明它错在哪。
            # 2026-10-20 真机 run 3920dfd725e7：用户说「人都一个样」，模型在还没出过片的
            # 时候先调了局部改，被这句挡住后没有回头改分镜，转去 read_node_history 换键名
            # 试，两轮把预算烧完——用户看到的是「卡死」，实际是一次错误的补救方向。
            # 所以把「还没出过片」那条路原样写进来。
            raise ValueError(
                "局部改：必须指定 base_artifact_id——要改的是哪一版成片"
                "（出片返回体顶层的 artifact_id）。如果还没出过片，就没有可改的基准："
                "要改分镜就用 plan_motion 传 shots_patch，只交要改的那几镜的 brief，"
                "然后调 render_motion_code 出片")

        bundle = await self._bundle(sess, base)
        spec_base, whence, fp = await self._base_spec(sess, base, bundle)
        patched, report = self.patch_planner(
            spec_base,
            edits=inputs.get("edits") or (),
            shot_sets=inputs.get("shot_sets") or (),
            remove_shots=inputs.get("remove_shots") or (),
            reorder=inputs.get("reorder") or ())
        # 质感在这一层覆盖（顶层是**整片默认档**，逐镜的 shots[].texture 覆盖它），与 bgm 的
        # 「传了才改口」同一条口径：它不进按镜缓存键，滤镜跑在取到切片之后的那一趟上，
        # 覆盖它不会让任何一镜重烧，所以「换一档看看」也算局部改。
        want_texture = str(inputs.get("texture") or "").strip()
        if want_texture:
            patched["texture"] = want_texture
        # 调参台那几栏整片观感（2026-10-21）：配色 / 字幕样式 / 强调方式 / 质感。
        # 前三栏在 `FILM_KNOBS` 里——换它们等于换了每一镜的像素，会重烧整片；
        # 质感不在（跑在切片之后），换它一镜都不重烧。代价不同，但入口是同一个。
        # 值不在这里判：认不出的色板/档名由 `patch_validator`（`validate_code_spec`）
        # 带镜号退回，那一份才是出片与局部改共用的唯一判据。这里放行一个拼错的
        # 值是**安全**的——它会在校验那一步被退回，而不是静默落成「没选」。
        film = inputs.get("film")
        if isinstance(film, Mapping):
            for key in ("palette", "caption_style", "highlight_style", "texture"):
                value = film.get(key)
                if value is not None and str(value).strip():
                    patched[key] = str(value).strip()
        # 配乐音量与人声压低开关同 texture 一样在烧完像素之后的那一趟上（配乐在母带合流、
        # 质感在每一镜切片的滤镜上），都不进按镜缓存键，改它们一镜都不重烧，所以也算局部改。
        bgm_conf = dict(patched.get("bgm") or {})
        if "bgm_volume" in inputs and inputs["bgm_volume"] is not None:
            bgm_conf["volume"] = inputs["bgm_volume"]
        if "bgm_duck" in inputs and inputs["bgm_duck"] is not None:
            bgm_conf["duck"] = inputs["bgm_duck"]
        if bgm_conf:
            patched["bgm"] = bgm_conf
        # 补丁后的分镜照样过出片那道闸门：局部改不是「免检通道」，
        # 改完照样可能把高亮词改到文案外面、把字段值改到声明区间外面。
        spec, errors = self.patch_validator(patched)
        # 落盘之前的链专属闸门。代码镜链在这里**只为代码字节变了的镜**复跑闸①：
        # 必须跑在校验之后（字段名单要取归一后的那份）、分叉之前（验不过的字节一寸不落）。
        await self._pre_gate(state, base=spec_base, spec=spec, report=report,
                             errors=errors)
        # 出处对账只管**本次改到的**镜：职责是拦住「新写上去的猜测冒充查证过的出处」，
        # 而不是替整片重新审计一遍旧账（旧版里那些出处当初已经过一次这道账）。
        # 2026-10-19 起对不上账的出处就地降级成「未核实」，提醒随补丁回执带回去，
        # 不再把这次局部改退回去重交（理由与代价见 `motion_plan._mark_source_unverified`）。
        cited_ids = [sid for sid in report["changed_shots"]
                     if any(shot.get("id") == sid and shot.get("source")
                            for shot in spec["shots"])]
        source_notes, gate = await self._sources(state, spec, cited_ids)

        target = _resolve_target_duration(
            state, inputs, await self._base_target(sess, base))
        # 这一趟在分叉之前花掉的钱（写手按新 brief 重写镜头代码）随补丁回执带回去。
        extra_cost = ({"node_cost_yuan": report["node_cost_yuan"]}
                      if report.get("node_cost_yuan") else None)
        if source_notes:
            extra_cost = dict(extra_cost or {})
            extra_cost["source_notes"] = source_notes
        artifact = await self._fork(state, sess=sess, base=base, spec=spec,
                                    target=target, gate=gate, extra=extra_cost)

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

    # ---- 落盘之前的链专属闸门（子类在这里复跑闸①）----

    async def _pre_gate(self, state: NodeState, *, base: dict[str, Any],
                        spec: dict[str, Any], report: dict[str, Any],
                        errors: list[str]) -> None:
        """为什么要留这个位置而不是让子类重写 ``process``：局部改的编排顺序（读哪一版 →
        装配 → 校验 → 复跑链专属闸 → 出处闸 → 分叉 → 开任务行 → 渲）必须**逐字同序**，
        子类重写 process 换来的是一条少了某一步的链路，而那一步的缺失只在像素里看得见。

        ``errors`` 是校验已经攒下的退回清单：分镜本身还不合法时子类**不该**拿它去外呼
        （一份连字段名单都对不上的代码交给闸①，只会把「改错了什么」掩盖成「lint 报错」）。
        """
        return None

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
                    f"指针只能靠猜——请重新出片（{self.patch_render_node} 会顺带重新量表）")
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
        # 链身份：这一栏写在表上，是为了让「拿着别的链的表下补丁」在一句话里说清楚，
        # 而不是让用户得到一份谁都看不懂的退回清单（各链的指针形状不同）。
        got_chain = str(bundle.get("chain") or "")
        if got_chain != self.patch_chain:
            raise ValueError(
                f"局部改：{base} 这一版的编辑包写的是「{got_chain or '没有链身份'}」，"
                f"本工具只改「{self.patch_chain}」那一路，且只认 {self.patch_render_node} "
                "出的片。链身份缺失的那一批是老产物（出片时还没写这一栏），"
                "它们对应的渲染流水线已经下线——请重新 plan_motion → "
                f"{self.patch_render_node} 出一版新片子")
        return bundle

    async def _base_spec(self, sess: str, base: str,
                         bundle: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        """→ (那一版渲的分镜, 从哪儿读到的, 指纹)。

        首选编辑包自带的 spec（与表同源，天然是「这一版片子渲的那一份」）；
        包是**不带 spec** 的老片子（那一栏是后来才加的）时才退到会话产物，而
        **退的那一路必须验指纹**：``plan_motion`` 会被后来的分镜覆盖，拿它冒充旧版就是
        拿新分镜去配旧命中表，指针落下去改的就是别的字。
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
                    f"会话里也没有 plan_motion 产物。请重新 plan_motion → "
                    f"{self.patch_render_node}")
        got_fp = mhit.fingerprint(spec)
        if not fp or fp != got_fp:
            raise ValueError(
                f"局部改：{base} 的命中表量的是另一版分镜（表指纹 {fp or '没有'}、"
                f"读到的分镜指纹 {got_fp}）——中间分镜被改过，指针会把新分镜的字认错。"
                f"重新出片（{self.patch_render_node}，会按当前分镜重新量表）是唯一出路")
        return spec, whence, fp

    async def _sources(self, state: NodeState, spec: dict[str, Any],
                       ids: list[str]) -> tuple[list[str], dict[str, str]]:
        """→ (提醒清单, 每镜判定)。

        对账只对**本次改到**的镜走（判据本体在分镜那一层，旧版里那些出处当初已经过一次），
        判定却整片都标：这一趟之后要把逐镜行改写回 ``plan_motion``，漏标的镜在账上会
        显示成「没出处」——那是把「查过」写丢，另一种假。

        2026-10-19 起只降级、不打回：第一项是提醒（进补丁回执），不再累进 `errors`。
        """
        return await settle_sources(state.session_id, spec["shots"],
                                    audit_ids=set(str(i) for i in ids))

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
                    spec: dict[str, Any], target: float | None,
                    gate: dict[str, str] | None = None,
                    extra: Mapping[str, Any] | None = None) -> str:
        """换一份产物作用域，并把改动同步成「会话当前分镜」；→ 新产物号。

        为什么必须换作用域：渲染任务行、成片字节、命中表、代表帧全都按 artifact_id 落位，
        沿用 base 的号等于把旧版覆盖掉——那样「改坏了回去」只能重渲一次，
        而局部改本来就是为了不重渲。

        为什么同时把改动写回 ``_default`` 作用域的 plan_motion：那一份是**模型**下一次
        调出片节点时读的。不写，就会出现「用户已经局部改过三处，
        而模型换配乐重渲时把三处改动全丢了」——那是把用户的编辑当草稿。
        旧版片子自己不受影响：它的分镜存在自己那份编辑包里。
        """
        artifact = state.artifact_id or ""
        if artifact in ("", "_default") or artifact == base:
            # 等于 base 的那一种必须换号：沿用就是把旧版覆盖掉，而旧版留着才能
            # 「改坏了直接播回去」——那一版片子是这次局部改唯一的退路。
            artifact = new_patch_scope_id()
        payload = self.payload_builder(
            spec, target=target, next_hint=self.patch_next_hint, gate=gate)
        if extra:
            payload.update(dict(extra))
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
                proof="缓存键 = 镜头内容 + 全局开关的哈希（见 "
                      f"{self.patch_cache_key_fn}），"
                      "没被改动的镜内容一字不差，于是整段跳过"
                if set(rebuilt) <= expected else
                f"没改动的镜里有 {sorted(set(rebuilt) - expected)} 被重烧了——"
                f"缓存键漏算了某个影响画面的输入，该修 {self.patch_cache_key_fn}",
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
