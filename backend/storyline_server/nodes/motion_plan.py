"""图形科普片的**分镜**节点：把模型写的代码分镜校验、归一、落进 Store，并回一份账。

为什么单有一个节点（而不是让模型直接调 render_motion_code）：

* **校验要有落点**：出片只认 Store 里那一份 ``motion_spec``。没有这一步，spec 要么每次
  调用重传一遍（模型容易漏、两处版本会漂），要么偷偷在出片节点里现场校验——而那时候
  已经站在烧像素的门口了。
* **账要在烧像素之前给人看**：每镜几个字、估算多少秒、离目标差多远、这一镜交出几个可改
  的旋钮，这些决定「删镜还是加镜」，属于分镜阶段的事，不该由一次十分钟的渲染来揭晓。
* **改文案不该重新渲一次片**：分镜与出片是两次决定，各自一个工具，才谈得上「只改第 3 镜
  再渲一次」。

口径与 ``motion/code_spec.py`` 一致：**镜头时长不由模型给**——有旁白时时钟归 TTS 实测，
模型写的 ``min_duration_sec`` 只当下限。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from agent_framework.designated_asset import check_shots_designated
from agent_framework.orchestration import NodeState, _safe
from agent_framework.retrieval import cite_fields, claims_verified, ledger, matches_ledger

import os

from ..motion import code_render as crender
from ..motion import code_spec as cspec
from ..motion import remotion_client as rc
from .core_nodes import StoryNode, _obj, _resolve_target_duration

#: 分镜卡上每镜文案最多显示这么多字——界面不是文稿编辑器，全文在 motion_spec 里。
PLAN_TEXT_PREVIEW_CHARS = 24


#: 一镜的出处在这一刻的检索账上算什么。**机器不判真伪**，这里只把「这一笔在不在账上」
#: 如实标出来——观众看到画面上有出处，得知道那三个字是查过的、是模型自陈没查过的、
#: 还是压根没填。
GATE_STATES = ("verified", "declared_unverified", "no_source", "ledger_absent")


def gate_state(source: Any, rows: list[Any] | None) -> str:
    """一镜出处的判定（纯函数：判据与闸本体逐字同源，绝不另写一遍）。

    ``claimed_unmatched``（声称查证过但对不上账）由 :func:`source_gate` 直接打回，
    永远不会出现在入库的分镜里——这里不列它，是为了让「能落进账本的只有这四种」
    成为读代码就能确认的事实。
    """
    if not source:
        return "no_source"
    if not claims_verified(source):
        return "declared_unverified"        # 写了「未核实：…」——不冒充，也就不必打回
    if rows is None:
        return "ledger_absent"              # 没绑账本：拿「没记过」定罪等于一律打回，那是另一种假
    return "verified" if matches_ledger(source, rows) else "claimed_unmatched"


async def source_gate(session_id: str, shots: list[dict[str, Any]], *,
                      audit_ids: set[str] | None = None
                      ) -> tuple[list[str], dict[str, str]]:
    """→ (打回清单, 每镜判定)。闸的本体：分镜入库前、局部改落下那一两镜、渲染口拿到
    没过分镜闸的 inline spec 时，调的都是这一个函数。

    为什么需要它（真机事故 2026-10-08）：检索被人机验证挡在门外之后，模型照样写完了
    分镜，每镜 ``source`` 填的是它自己的记忆。画面与逐镜账如实转录了这些出处，观众读到
    的却是「像查过的一样」的猜测——「机器不判真伪、只如实转录」这条合同被架空了。
    闸不判史实（判不了），只判**这一笔在本会话里发生过吗**。

    ``audit_ids`` 只缩小**打回**的范围（局部改不替整片重审旧账），判定仍逐镜给全：
    两次读账不如一次读完，而标要标全——漏标的镜在账上就成了「没出处」，那是另一种假。

    账本不可用（没绑存储）时整体放行：没地方记命中，就拿「没记过」定罪会退化成
    「所有出处一律打回」，那是另一种假。
    """
    rows = await ledger(session_id)
    states: dict[str, str] = {}
    errors: list[str] = []
    for shot in shots:
        sid = str(shot.get("id") or "")
        source = shot.get("source") or {}
        st = gate_state(source, rows)
        states[sid] = st
        if st != "claimed_unmatched" or (audit_ids is not None and sid not in audit_ids):
            continue
        title, url = cite_fields(source)
        errors.append(
            f"{shot['id']}：出处「{title or url}」写成了查证过的样子，但本会话的检索账里"
            f"没有这一笔（web_search / fetch_url 都没真打开过它"
            + ("，一次检索都没成功过" if not rows else "")
            + "）。要么先去查，要么改成「未核实：<依据一句话>」"
            "——模型记忆不许冒充查证过的出处")
    return errors, states


async def _source_errors(session_id: str, shots: list[dict[str, Any]]) -> list[str]:
    """只要打回清单的那一半（调用点不关心每镜判定时用这个）。"""
    errors, _states = await source_gate(session_id, shots)
    return errors


def _try_rescue_json(text: str) -> Any:
    """尝试 json.loads；失败时用截断修复（括号栈+字符串状态）再试一次。"""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        from agent_framework.llm_openai import _repair_truncated_json
        repaired = _repair_truncated_json(text)
        if repaired:
            return json.loads(repaired)
    except Exception:
        pass
    return None


class PlanMotionNode(StoryNode):
    name = "plan_motion"
    display_name = "图形科普片分镜"
    description = (
        "零素材出片的第一站：把分镜（每镜一句文案 + 高亮词 + 一段**完整 React/TSX 代码**"
        "+ 可选的可改旋钮 fields + 可选出处）校验、归一并存成本会话的 motion_spec，"
        "同时回一份「将要渲成什么样」的账（镜头数、总字数、按字数估的秒数区间、"
        "与目标时长的偏差提醒、每镜交出的字段清单）。"
        "本节点**不渲染、不烧像素**：改文案只需重调本工具。"
        "两轮走法：**取材轮**——只交骨架（所有镜都带 card 与 derived_from、都**不带 code**），"
        "本工具把每一镜被引用 demo 的**全文**回给你（逐件带 relpath/bytes/sha16/text）；"
        "**分镜轮**——所有镜都带上 code 再调一次。先取材再写码是要求不是建议："
        "闸（derived_from）会按代码骨架与所声明 demo 的相似度核对声明，"
        "没读过 demo 就写出来的代码过不了，而读过了就不必手搓。"
        "画面全部来自代码——图示、手绘质感（rough.js）、立体几何（three.js）、公式（katex）"
        "都是真代码；每镜的 code 必须顶层 import 并把画面包进母版 <FilmLayout>，"
        "版式与字幕带由母版统一给，不在代码里自己画一遍。"
        "出处要过一道账：本会话被 web_search / fetch_url 真打开过的页面才算查证过，"
        "凭记忆写的出处要标「未核实：…」，冒充查过的带镜号退回。"
        f"比例：{'、'.join(cspec.ASPECTS)}；帧率白名单：{cspec.FPS_CHOICES}；"
        f"字幕形态：{'、'.join(cspec.SUBTITLE_MODES)}；"
        f"每镜最多 {cspec.MAX_FIELDS_PER_SHOT} 个可改旋钮。"
        "有旁白时不要写每镜时长——镜头长度由真实语音时长决定。"
        f"画幅/帧率/旁白开关/音色/语速/字幕形态/质感档同时提供**顶层入参**（计划卡给用户勾选的位），"
        f"质感可选：{'、'.join(cspec.TEXTURES)}（顶层是整片默认档，每一镜可用 "
        f"shots[].texture 覆盖它；换档只重跑滤镜，不重烧镜头）。"
        "传了会覆盖 spec 里的同名字段。")
    required_nodes: list[str] = []
    require_explicit_call = True
    input_schema = _obj("图形科普片分镜（代码镜）", {
        "spec": {
            **cspec.spec_schema(),
            "description": "代码分镜 spec：每镜 code + text + highlight + 可选 fields + "
                           "可选 source（逐字段含义见结构；没有 card/visual/主题那几栏——"
                           "取材轮交的就是**没有 code 的那一份**：每镜只给 id/text/card/"
                           "derived_from，本工具回被引用 demo 的全文且什么都不入库。"
                           "版式与字幕在母版里，画面在代码里）。"
                           "本节点是**校验闸**：代码没交、文案超出字幕容量、高亮词不是文案子串、"
                           "字段声明越界都会带镜号退回，改完重调即可，不烧像素。"
                           "source 请如实填出处——机器不判真伪，它会如实出现在分镜卡与逐镜账上；"
                           "要把出处排进画面，得在 code 里自己渲染那一栏。"
                           "**出处还要过一道账**：本会话被 web_search / fetch_url 真打开过的页面"
                           "才算查证过，对不上账的会被打回（没查到就写「未核实：…」）。"
                           "aspect/fps/narration/voice/rate/subtitle_mode/texture 也可以走下面的"
                           "顶层开关，顶层的优先（那是用户在计划卡上勾的，不是建议）。"
                           "校验通过后调 render_motion_code 出片。",
        },
        **cspec.knob_props(),
        "target_duration_sec": {
            "type": "number",
            "description": "用户要的目标秒数（取材轮与写码轮都要带）。无旁白时系统按比例把各镜驻留"
                           "对齐到它；偏离大到对不齐时在这里打回并给出镜数建议",
        },
        "shots_patch": {
            "type": "array", "items": {"type": "object"},
            "description": "只交要改的那几镜（按 id 替换上一版分镜里的同号镜，新 id 追加到末尾），"
                           "不传 spec。上一版被打回后改几镜就用它，不必把几十镜代码整份重交",
        },
    })

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        raw = inputs.get("spec")
        # fallback 1：_parse_tool_args 解析失败时返回 {"_raw": 原始串}，
        # 尝试再救一次——原始串可能是整份 {"spec": {...}} 或直接就是 spec 本体。
        if raw is None and isinstance(inputs.get("_raw"), str):
            rescued = _try_rescue_json(inputs["_raw"])
            if isinstance(rescued, dict):
                raw = rescued.get("spec", rescued)
        # fallback 2：LLM 把 spec 内容直接放在顶层（没包 {"spec": ...}），
        # 有 shots 键就当 spec 用。
        if raw is None and "shots" in inputs:
            raw = inputs
        if isinstance(raw, str) and raw.strip():
            raw = _try_rescue_json(raw) or raw
        # 补丁轮：只交改过的那几镜，与本会话上一版分镜（不论那一版是否过了闸）按 id 合并。
        patch = inputs.get("shots_patch")
        if raw is None and isinstance(patch, list) and patch:
            base = _DRAFTS.get(state.session_id)
            if base is None:
                raise ValueError(
                    "plan_motion：shots_patch 找不到上一版分镜（服务重启过或本会话还没交过），"
                    "这一次请交完整的 spec")
            raw = merge_shots_patch(base, patch)
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                "plan_motion：spec 必须是对象且非空——每镜至少交一句文案和一段真实代码")
        # 取材轮排在**一切校验之前**：它根本不是一份分镜（没有 code、没有 aspect），
        # 放进 validate_code_spec 只会得到一屏「code 为空」的清单。判定只看一件事——
        # 这一轮有没有交 code（见 cspec.forage_scan）。
        # `code_from` 先展开：复用别镜代码的那一镜算「带了 code」，不该被判成取材/写码混交。
        raw = cspec.expand_code_refs(raw)
        mode, forage = cspec.forage_scan(raw)
        if mode == "mixed":
            ids = "、".join(f"{r['id']}（{r['card'] or '未声明卡'}）" for r in forage)
            raise ValueError(
                "plan_motion：这次调用有的镜交了 code、有的没交（没交的：" + ids + "）"
                "——服务端分不出「漏填」与「想先取材」，所以这两种意图要各调一次："
                "取材轮**所有镜都不带 code**（带 card 与 derived_from），"
                "分镜轮**所有镜都带 code**。没入库，改完重调。")
        if mode == "forage":
            account, errs = cspec.collect_demos(forage)
            if errs:
                raise ValueError(
                    "取材轮未通过，一份 demo 的字节都没回（照清单改完再调本工具）：\n"
                    + "\n".join(f"  · {e}" for e in errs[:12])
                    + (f"\n  · …另有 {len(errs) - 12} 条同类错误" if len(errs) > 12 else ""))
            return forage_payload(account)
        # 顶层开关先并进来再校验：卡上勾了 landscape 就得渲成横屏，模型在 spec 里写的
        # portrait 让位；并早并晚都要过校验器，越界照样回清单。
        # 质检档：spec 没写就按产品缺省（MOTION_QC，缺省 preview——见 cspec.QC_MODES）。
        # 写码轮的这一版先记成草稿：被下面任何一道闸打回，下一次都可以只用 shots_patch 改那几镜。
        _remember_draft(state.session_id, raw)
        # 读不出的字色由系统就地修好（纯算术，不该为它打回重写整份分镜）。
        raw, contrast_notes = cspec.autofix_ink_contrast(raw)
        spec, errors = cspec.validate_code_spec(
            cspec.overlay_knobs(cspec.with_default_qc(raw), inputs))
        if errors:
            # 带病入库比报错更贵：render_motion_code 读的就是这一份，
            # 于是错误会在**烧完像素之后**才现形。
            raise ValueError(
                "代码分镜校验未通过，没有入库（照清单改完再调本工具；只改了几镜就用"
                " shots_patch 只交那几镜）：\n"
                + "\n".join(f"  · {e}" for e in errors[:12])
                + (f"\n  · …另有 {len(errors) - 12} 条同类错误" if len(errors) > 12 else ""))
        # 目标时长：本次入参 > 会话 flags > spec 里写的那一份（模型常在取材轮带上、
        # 写码轮漏掉——真机 run 79dcdc0ec0a8 就是写码轮 target=None，时长闸被推迟到确认之后）。
        target = _resolve_target_duration(state, inputs, None)
        if target is None and raw.get("target_duration_sec"):
            target = max(3.0, float(raw["target_duration_sec"]))
        # 时长在**入库之前**对齐与把关：无旁白时系统自己把驻留算到目标上；
        # 算不到（或有旁白偏离太多）就在这里打回——而不是等用户点了确认、渲染口才拦。
        spec, duration_notes = cspec.fit_duration(spec, target)
        blocked = cspec.duration_block(spec, target)
        if blocked:
            raise ValueError(blocked + "改完重调本工具（分镜没有入库）")
        spec["target_duration_sec"] = target
        cited, gate = await source_gate(state.session_id, spec["shots"])
        if cited:
            # 校验过的分镜仍可能带着「像查过的一样」的猜测出处入库；一旦出了片，
            # 那些字面就在成片与逐镜账里替模型背了书。拦在入库前。
            raise ValueError(
                "出处闸未通过，分镜没有入库（本会话真打开过的页面才算出处）：\n"
                + "\n".join(f"  · {e}" for e in cited[:12])
                + (f"\n  · …另有 {len(cited) - 12} 条同类错误" if len(cited) > 12 else ""))
        # 闸㉛ 指名权（R13 §5）：用户上传的图**只有用户指名才能摆进镜**——模型凭记忆
        # （或凭 sha 前缀猜）往 assets 里填一个上传件的 sha 时，闸⑬ 与渲染腿一路放行
        # （字节在、索引行在），而用户从没说过「这一镜用我这张图」。
        #
        # **一处如实说明的偏差**：R13 原文要的判据是「本次 run 的 attachments」，
        # 但 attachments 到不了 Storyline 进程（grep 0 命中）。实际判据是会话域双
        # 集合（uploaded_shas × allowed_shas）——防的正是同一件事：凭记忆填的 sha
        # 不会落在会话可见集合里，照样打回。偏差全文写在
        # ``agent_framework.designated_asset`` 模块头。
        designated_errs, designated_unjudged = await check_shots_designated(
            self.storage, spec["shots"],
            user_id=state.user_id or "", conv_id=state.conversation_id or "")
        if designated_errs:
            raise ValueError(
                "指名权闸未通过，分镜没有入库（用户上传的图只有用户指名才能用）：\n"
                + "\n".join(f"  · {e}" for e in designated_errs[:12])
                + (f"\n  · …另有 {len(designated_errs) - 12} 条同类错误"
                   if len(designated_errs) > 12 else ""))
        # 闸① 提前到入库前：原先 lint 只在渲染口里跑，一镜代码写错要等前面几镜都烧完、
        # 渲到它才现形，然后整份重写重提。这里所有镜并行过一遍（不烧像素，每镜几百毫秒）。
        # 渲染容器连不上时不拦——渲染口自己还会再 lint 一遍，那才是硬闸。
        up = await _remotion_up()
        lint_errs = await _prelint(spec["shots"]) if up else []
        if lint_errs:
            raise ValueError(
                "闸① lint 未通过，分镜没有入库（只改报错的那几镜的 code，用 shots_patch "
                "只交那几镜再调本工具）：\n"
                + "\n".join(f"  · {e}" for e in lint_errs[:12])
                + (f"\n  · …另有 {len(lint_errs) - 12} 条同类错误"
                   if len(lint_errs) > 12 else ""))
        payload = plan_payload(spec, target=target, gate=gate,
                               designated_unjudged=designated_unjudged)
        if duration_notes:
            payload["duration_notes"] = duration_notes
        if contrast_notes:
            payload["autofix_notes"] = contrast_notes
        # 预览静帧（主流的「抽几帧看」）：每镜一张小图，渲染前的计划确认卡上给人看。
        # 容器不通、或关了这一档（MOTION_PREVIEW_STILLS=0）就没有图——卡照样弹。
        if up and os.environ.get("MOTION_PREVIEW_STILLS", "1") not in ("0", "false", "no"):
            refs, notes = await self._publish_previews(state, spec)
            if refs:
                payload["preview_frames"] = refs
            if notes:
                payload["preview_notes"] = notes[:12]
        return payload

    async def _publish_previews(self, state: NodeState, spec: dict[str, Any]
                                ) -> tuple[dict[str, str], list[str]]:
        """渲每镜预览静帧并发布到产物作用域 → ({镜号: 对象键}, 说明)。"""
        sess = state.session_id
        artifact = state.artifact_id or "_default"
        files, notes = await crender.preview_stills(
            spec, client=rc.endpoints(), work=self._work(state, "motion", "preview"),
            storage=self.storage)
        refs: dict[str, str] = {}
        for sid, path in files.items():
            key = f"renders/{_safe(sess)}/{_safe(artifact)}/preview/{_safe(sid)}.jpg"
            try:
                await self.storage.workspace.publish(str(path), key,
                                                     content_type="image/jpeg")
                refs[sid] = key
            except Exception as exc:  # noqa: BLE001 - 缺一张预览不拦
                notes.append(f"{sid}：预览图没发布（{type(exc).__name__}）")
        return refs, notes


#: 每个会话最近一版写码轮分镜（补丁轮的底稿）。进程内、有上限：重启后丢了就请模型交整份。
_DRAFTS: dict[str, dict[str, Any]] = {}
_DRAFTS_MAX = 64


def _remember_draft(session_id: str, raw: Mapping[str, Any]) -> None:
    _DRAFTS.pop(session_id, None)
    _DRAFTS[session_id] = dict(raw)
    while len(_DRAFTS) > _DRAFTS_MAX:
        _DRAFTS.pop(next(iter(_DRAFTS)))


def merge_shots_patch(base: Mapping[str, Any], patch: list[Any]) -> dict[str, Any]:
    """按镜号把补丁并进底稿：同号整镜替换，新号追加；顶层开关沿用底稿。"""
    shots = [dict(x) for x in (base.get("shots") or []) if isinstance(x, Mapping)]
    pos = {str(x.get("id") or ""): i for i, x in enumerate(shots)}
    for item in patch:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        sid = str(item["id"])
        if sid in pos:
            shots[pos[sid]] = {**shots[pos[sid]], **dict(item)}
        else:
            pos[sid] = len(shots)
            shots.append(dict(item))
    return {**dict(base), "shots": shots}


async def _remotion_up() -> bool:
    """渲染容器此刻通不通（3 秒探活）。不通时预检与预览都跳过，不拖慢分镜轮。"""
    try:
        got = await asyncio.to_thread(rc.health, timeout=3)
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(got, Mapping) and got.get("ok"))


#: 入库前 lint 的并发路数（/lint 不进渲染串行链，只是起一次 eslint）。
PRELINT_CONCURRENCY = 6
#: 单镜预检超时（秒）：预检是增益不是硬闸，容器慢或不通时不该把分镜轮拖住。
PRELINT_TIMEOUT = 20


async def _prelint(shots: list[dict[str, Any]]) -> list[str]:
    """所有镜的代码并行过闸① → 带镜号的错误清单；渲染容器不可达时返回空（不拦）。"""
    def lint(code: str, names: list[str], assets: list[Any]) -> dict[str, Any]:
        return rc.lint(code, declared_fields=names, assets=assets, timeout=PRELINT_TIMEOUT)
    gate = asyncio.Semaphore(PRELINT_CONCURRENCY)

    async def one(shot: Mapping[str, Any]) -> list[str]:
        code = str(shot.get("code") or "")
        if not code.strip():
            return []
        names = [str(f.get("name") or "") for f in (shot.get("fields") or [])
                 if f.get("name")]
        async with gate:
            try:
                got = await asyncio.to_thread(lint, code, names,
                                              list(shot.get("assets") or []))
            except Exception:  # noqa: BLE001 - 预检外呼失败不拦，渲染口兜底
                return []
        if not isinstance(got, Mapping) or got.get("ok", True):
            return []
        sid = str(shot.get("id") or "?")
        errs = [e for e in (got.get("errors") or [])
                if not (isinstance(e, Mapping) and e.get("rule") == "lint-crash")]
        return [f"{sid}：{e.get('rule') or ''} 第 {e.get('line') or '?'} 行：{e.get('msg') or e}"
                if isinstance(e, Mapping) else f"{sid}：{e}"
                for e in errs]

    results = await asyncio.gather(*(one(s) for s in shots))
    return [e for r in results for e in r]


def forage_payload(account: dict[str, Any]) -> dict[str, Any]:
    """取材轮的产物：被引用 demo 的**全文** + 一份字节账。**不落库、不渲任何东西**。

    ``__no_store__`` 是这条链既有的写法（见 ``render_video`` 的 dry_run）：这一次调用
    确实产出了一样东西（模型上下文里的那几份源码），但它**不是**本会话的 ``motion_spec``
    ——落库就成了「这一步已经有分镜了」的假象，而出片节点读的正是那一格。

    ``next`` 写的是下一步该干什么，不是客套：取材轮最容易出的岔路是模型拿回全文之后
    又调一次取材（换个说法问同样的事），那会白烧一轮上下文。所以这里把「现在去写 code」
    与「超线的那几份用 load_skill 取」两件事都点名。
    """
    oversize = list(account.get("forage_oversize") or [])
    next_hint = ("现在把每一镜的 code 写出来，连同 card 与 derived_from 一起重调本工具"
                 "（这一次所有镜都带 code）。代码要从上面那份全文演化而来——"
                 "闸（derived_from）会按骨架相似度核对你写的代码与声明的 demo，"
                 "相似度低于阈值的声明会被打回。")
    if oversize:
        names = "、".join(str(o.get("relpath")) for o in oversize)
        # 技能名从 SKILL.md 的 frontmatter 读（它不等于目录名），读不到就只说 path，
        # 不编一个名字出来——「照提示去 load_skill 却查无此技能」比不提这件事更坏。
        skill = cspec.motion_skill_name()
        how = (f'load_skill(name="{skill}", path="<relpath>")' if skill
               else 'load_skill（技能名见 <skills> 清单, path="<relpath>"）')
        next_hint += (f"另有 {len(oversize)} 份超出本轮上限没随回执给出（{names}）："
                      "它们是**没给**不是**没内容**，用 "
                      f"{how} 逐件取全文——不要凭记忆写这几镜的代码。")
    return {
        "__no_store__": True,
        "forage": True,
        "stored": False,
        **account,
        "next": next_hint,
    }


def plan_payload(spec: dict[str, Any], *, target: float | None = None,
                 next_hint: str = "",
                 gate: dict[str, str] | None = None,
                 designated_unjudged: list[str] | None = None) -> dict[str, Any]:
    """``plan_motion`` 产物的形状（归一后的 spec + 那份要给人看的账）——**单源**。

    为什么抽出来：``patch_motion_code`` 把一版局部改写回 ``plan_motion`` 时，必须写成与
    这里**一模一样**的形状（出片节点与确认卡读的都是这几栏）。两处各抄一遍，
    迟早出现「局部改之后卡面上 shot_count 还是旧数字」这种谎。

    ``gate`` 是出处闸顺手算出的每镜判定。闸只拦「冒充查证过」那一档，剩下三种
    （真对上了账、作者明写没核实、压根没填出处）**都会入库**——逐镜账要是把它们
    混成一个空白格，观众就没法知道画面右上角那行字的凭据是哪一种了。

    ``designated_unjudged`` 是闸㉛（指名权）的未判档（R13 §5）。与 ``style_unjudged``
    同一形状、同一规矩（T28）：非空即「本片有闸没量过」，卡面按阻断档显示，
    **不许与提醒用同一种措辞与同一种颜色**——把「没判过」读成「小提醒一下」
    就是 D-未判不绿那条病。
    """
    low, high = cspec.estimate_sec(spec)
    shots = spec["shots"]

    def _row(s: dict[str, Any]) -> dict[str, Any]:
        # 出处用闸同一把钥匙读：契约写的是「标题/链接」，只认英文键的读法会让
        # 一条过了闸的中文出处在逐镜账上显示成空白。
        cite_title, cite_url = cite_fields(s["source"])
        text = s["text"]
        return {
            "id": s["id"],
            "text": (text[:PLAN_TEXT_PREVIEW_CHARS] + "…")
                    if len(text) > PLAN_TEXT_PREVIEW_CHARS else text,
            "chars": len(text),
            "highlight": s["highlight"],
            "source": cite_title or cite_url,
            "source_gate": (gate or {}).get(str(s["id"])) or "unchecked",
            "min_sec": s["min_duration_sec"],
            # 代码这一栏只报字节数不报正文：一张卡装不下一段 TSX，而「这一镜有没有交代码」
            # 在闸①~⑥ 那几行账上看的是像素，不是这段字节。
            "code_chars": len(str(s["code"])),
            # 闸⑦ 的两栏机器事实，都**从代码与文案里读出来**而不是模型自报：
            # style 是这一镜真 import 了哪条画法通道，concrete 是文案里有没有
            # 词法看得见的具体项（数字/引号术语/拉丁缩写）。用户在卡上看见
            # 「12 镜里 9 镜 style=text」才会说「补画面」，这一栏的全部价值在这。
            "style": facts["channels"][str(s["id"])],
            "concrete": cspec.has_concrete_item(text),
            # 闸⑯ 的两栏（申报侧）：subject 是模型自报的「这一镜画的是什么」，subject_channel
            # 是它自报的通道。**与上面那栏 style 挨着放是刻意的**——style 是代码里认得的、
            # subject_channel 是自报的，两栏不一致的镜 validate_code_spec 已经拦掉了，
            # 留下的这一份让用户在确认卡上直接看见「这一镜说它画的是什么」。
            "subject": str(s.get("subject") or ""),
            "subject_channel": facts["subject_channels"][str(s["id"])]["declared"],
            # 闸⑮ 的账：这一镜选的卡属于哪个**族**。用户在卡上看见「data×3」才会说
            # 「版式重复了」——族配额那句打回理由指的正是这一栏。
            "family": facts["card_families"][str(s["id"])],
            # 这一镜**实际生效**的质感档（跟随整片时与顶层同值，点名不要时是 none）。
            # 判档只走 `cspec.shot_texture` 那一个出处：这里再写一遍「有就用、没有问整片」，
            # 就会出现卡上写 film、画上是原片。
            "texture": cspec.shot_texture(s, spec),
            # 字段清单进账：确认卡要给用户看清「这一镜交出几个可改的旋钮」，
            # 局部改那一头也要能从同一份产物里读到声明的 name/type/value。
            "fields": [{"name": f["name"], "type": f["type"], "value": f["value"],
                        "label": f.get("label") or "", "unit": f.get("unit") or ""}
                       for f in (s.get("fields") or [])],
        }

    facts = cspec.style_facts(shots)
    # 打回那一份在 validate_code_spec 里拦过了；这里取**拦完之后给人看的**两栏——提醒一栏、
    # 未判一栏（T28）。两栏分家不是措辞讲究：计划卡上「提醒」读作「不改也能出」，
    # 「未判」读作「这一档没人量过」，合成一栏就等于把后者降级成了前者。
    _, quota_warns, quota_undet = cspec.whole_film_gate_results(
        shots, strict=spec.get("qc") == cspec.QC_STRICT)

    return {
        "motion_spec": spec,
        "chain": "remotion",
        "shot_count": len(shots),
        "char_count": sum(len(str(s["text"])) for s in shots),
        "estimated_sec": [low, high],
        "target_duration_sec": target,
        "target_warnings": cspec.check_target(spec, target),
        # 闸⑦⑨⑩ 的整片账：硬判据（配额、纯文字镜占比、跨镜同构、字色对比度）已经在
        # validate_code_spec 里拦过了，下面几栏是**拦完之后给人看的账**——用了哪几种通道、
        # 哪几镜只有文字、哪几镜其实是同一张卡、字色在底色上读到几比一。
        "style_channels": facts["variety"],
        "style_text_only": facts["text_only"],
        "style_same_shape": facts["same_shape_groups"],
        "style_contrast": facts["contrast"],
        "style_contrast_unjudged": facts["contrast_unjudged"],
        "style_warnings": quota_warns,
        # 未判档单列（T28）：这一栏非空即「本片有闸没量过」，卡面按阻断档显示，
        # 不许与上一栏（提醒）用同一种措辞与同一种颜色。卡上看得见 = 用户能在
        # 烧像素之前就叫人补底色/重写代码，而不是等出片回执再发现。
        "style_unjudged": quota_undet,
        # 闸㉛ 指名权的未判档（R13 §5，与 style_unjudged 同一形状同一规矩）：
        # 「本会话上传件名册/素材册读不到」或「本片没用用户上传的图」。
        # 打回那一档在入库前就抛了（见 process 里那一刀），走不到这里。
        "designated_unjudged": list(designated_unjudged or []),
        "aspect": spec["aspect"],
        "resolution": f"{spec['width']}x{spec['height']}",
        "fps": spec["fps"],
        "narration": spec["narration"],
        "voice": spec["voice"] if spec["narration"] else "",
        "rate": spec["rate"] if spec["narration"] else "",
        "subtitle_mode": spec["subtitle_mode"],
        "texture": spec.get("texture") or "none",
        "bgm": spec["bgm"],
        "title": spec["title"],
        "plan_table": [_row(s) for s in shots],
        "next": next_hint or (
            "确认这份分镜（或按用户要求改完）之后调 render_motion_code 出片；"
            "它会先把编排结果交用户确认，不会静默开渲"),
    }
