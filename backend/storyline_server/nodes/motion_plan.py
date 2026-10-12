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

import json
from typing import Any, Mapping

from agent_framework.orchestration import NodeState
from agent_framework.retrieval import cite_fields, claims_verified, ledger, matches_ledger

from ..motion import code_spec as cspec
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
            "description": "用户要的目标秒数。只出提醒（估算偏离过大时建议删镜/加镜），"
                           "本节点不会因此拒绝",
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
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                "plan_motion：spec 必须是对象且非空——每镜至少交一句文案和一段真实代码")
        # 顶层开关先并进来再校验：卡上勾了 landscape 就得渲成横屏，模型在 spec 里写的
        # portrait 让位；并早并晚都要过校验器，越界照样回清单。
        spec, errors = cspec.validate_code_spec(cspec.overlay_knobs(dict(raw), inputs))
        if errors:
            # 带病入库比报错更贵：render_motion_code 读的就是这一份，
            # 于是错误会在**烧完像素之后**才现形。
            raise ValueError(
                "代码分镜校验未通过，没有入库（照清单改完再调本工具）：\n"
                + "\n".join(f"  · {e}" for e in errors[:12])
                + (f"\n  · …另有 {len(errors) - 12} 条同类错误" if len(errors) > 12 else ""))
        target = _resolve_target_duration(state, inputs, None)
        spec["target_duration_sec"] = target
        cited, gate = await source_gate(state.session_id, spec["shots"])
        if cited:
            # 校验过的分镜仍可能带着「像查过的一样」的猜测出处入库；一旦出了片，
            # 那些字面就在成片与逐镜账里替模型背了书。拦在入库前。
            raise ValueError(
                "出处闸未通过，分镜没有入库（本会话真打开过的页面才算出处）：\n"
                + "\n".join(f"  · {e}" for e in cited[:12])
                + (f"\n  · …另有 {len(cited) - 12} 条同类错误" if len(cited) > 12 else ""))
        return plan_payload(spec, target=target, gate=gate)


def plan_payload(spec: dict[str, Any], *, target: float | None = None,
                 next_hint: str = "",
                 gate: dict[str, str] | None = None) -> dict[str, Any]:
    """``plan_motion`` 产物的形状（归一后的 spec + 那份要给人看的账）——**单源**。

    为什么抽出来：``patch_motion_code`` 把一版局部改写回 ``plan_motion`` 时，必须写成与
    这里**一模一样**的形状（出片节点与确认卡读的都是这几栏）。两处各抄一遍，
    迟早出现「局部改之后卡面上 shot_count 还是旧数字」这种谎。

    ``gate`` 是出处闸顺手算出的每镜判定。闸只拦「冒充查证过」那一档，剩下三种
    （真对上了账、作者明写没核实、压根没填出处）**都会入库**——逐镜账要是把它们
    混成一个空白格，观众就没法知道画面右上角那行字的凭据是哪一种了。
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
    # 错误那三份都在 validate_code_spec 里拦过了；这里取的是**拦完之后给人看的提醒**，
    # 三道闸各出一份，合流成一栏，用户在同一张卡上看得见「哪几镜其实是同一张卡」。
    quota_warns = cspec.check_style_quota(shots)[1] \
        + cspec.check_shot_structure(shots)[1] + cspec.check_ink_contrast(shots)[1]

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
