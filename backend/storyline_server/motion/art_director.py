"""逐镜写码前的**美术导演**：一次纯文本调用，把风格拆成可执行做法，并把 brief 补成具体画面。

为什么放在写码前（2026-10-18）：

- 评审是在**渲完之后**才看见「这一帧只有字和色块」的；同样的判断，在文本阶段就能提前做。
  真机那条像素赛博朋克的片子里，28 镜有 14 镜不像那个风格——不是代码画不出来，而是
  brief 只写了「展示社会比较」这类抽象话，写手没有落点，只能退回版式。
- 这层只改 `brief` 与 `style_grammar`，不碰文案、不写代码；一次调用覆盖整片，
  不是每镜一次。失败不拦：原 brief 照用，写手与后面的视觉评审照常工作。
- 它解决的是「第一次为什么不做好」里**可预先知道**的那一半：画什么、在哪、光从哪来、
  字排哪、风格落到哪几个具体做法。真正只有渲出来才看得见的（构图失衡、材质糊、
  动效不够）仍交给静帧评审与重写。

开关：``MOTION_ART_DIRECTOR=0`` 关掉（关掉时不动 spec、不花钱）。
模型：``MOTION_DIRECTOR_MODEL`` 可单独指定；缺省跟随用户默认模型。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Sequence

DIRECTOR_ENABLED = os.environ.get(
    "MOTION_ART_DIRECTOR", "1") not in ("0", "false", "no")
DIRECTOR_MODEL = os.environ.get("MOTION_DIRECTOR_MODEL", "").strip()
DIRECTOR_TIMEOUT_SEC = 120.0
#: 一条 28 镜的片子，逐镜 200 字的落点约 5~6k token；给 8192 免得回话被截在半句。
DIRECTOR_MAX_TOKENS = 8192
#: 改写后的 brief 上限。写手的单镜任务块本来就会截，这里先截住，避免一条 brief
#: 把整份导演回话撑爆。
BRIEF_MAX_CHARS = 320

_SYSTEM = """你是这条科普片的美术导演。你只改写每一镜的「画面 brief」，不写代码、不改文案，
也不替用户改选题或事实。你的唯一目标：让镜头画师**第一次落笔**就画出一幅具体的画，
而不是一页排版。

先做两件事：
1. 把整片风格拆成 3~5 个**可执行做法**：网格/笔触尺寸、色数、边缘处理、光源方向、
   材质/纹理、体积与明暗（受光面/背光面/落地影怎么分）、运动方式。写成一句，≤200 字。
2. 逐镜做两件事：
   a. **圈实体**：从这一镜文案里圈出能被画出来的实体——人、物、地点、动作的结果
      （谁对谁做了什么）。一镜通常 2~4 个，文案只点到一件时就只写一件，**别硬凑**。
      每个实体写成名词短语（「鲣鸟」「巨型斑点蛋」「三枚沙色蛋」「海边草丛」），
      不写「理念」「趋势」这类画不出形状的词。
   b. **补画面落点**：几件实体各在画面哪一侧、谁前谁后、谁被谁挡、各占多大；
      光从哪来；字幕排在哪——**按带子写**（底带 / 顶带 / 与某项咬合），并写明
      实体都落在带外的那块画面区里，别让主体伸进字幕带。**每件实体再补一句形状**：外轮廓是什么、
      「少了它就不像」的标志物是什么、2 个部件是什么（钱包＝鼓起的软包 + 拉链齿 +
      探出的钞票边角）。如果原 brief 已经具体，保留它，只补缺的部分。

纪律：
· 不改文案、不加新事实、不换选题；用户明确要求优先于你的偏好。
· 只画 brief 与文案里**已经有**的东西：不要凭空加人物、品牌、地名、数据或事件。
  brief 没提人，就不要加人；brief 说了「只画物件」，就只画物件。
· 一镜里的几件实体要**同处一个场景**：有远近、有遮挡、有朝向，像一幅插图；
  不是并排摆一排小图标，也不是各占一个圆角卡片。
· 若整片给了**贯穿主体**（下方有「整片贯穿主体」那一条），每一镜的 brief 都要把这个
  **同一个东西**放进画面：写清它在画面哪一侧、占多大、朝哪边走、这一镜是什么材质/状态，
  别漏掉；它可能是人，也可能是一头鹿、一辆车、一块发光的矩形。文案没提到它也一样——
  这一条优先于「brief 没提人就不要加人」。**同一身份不等于同一外形**：时代/场景变了，
  就在 brief 里写清这一镜的外形（轮廓、服饰、配件、持物），写手按 brief 画，别让模板
  里参考片那一刻的固定外形把每一镜都锁死。
· 每件实体都要写成**有体积的东西**：实心、有受光面与背光面、有落地影；
  不要写成「一个方框 + 一个标签」「一圈描边线框」——那不是画面。
· 构图纪律看下方输入：没有风格包时相邻镜要有变化；有风格包且要求重复构图时，
  每一镜都守同一构图、同一套风格不变量，只换内容（时代背景、对象外形、服饰、
  道具、文字）与光色。
· 有风格包时，brief 末尾只补**这一镜真的会变、模板又需要的**那几项内容（哪几项
  看风格包里的层/时间线与它声明的 prop；没有就只写画面）。常见的是「这一镜那块
  光/亮面是什么」「主体这一下在做什么」「画面里要出现的那行字」。不要为了凑格式
  加固定三句，也不要写参考片里的原文内容。
另外，如果整片**还没有美术方向**（用户没给 `art`），按风格补一份；已经给了就别改：
`look` 是把画风拍成一句话；`brush` 取 graphite/marker/watercolor/ink，`light` 取
top-left/left/soft/…，`texture` 取 paper/grain/none，`type` 取
serif-display/sans-editorial/…，`motion` 取 spring/linear/…；`palette_note` 写色温
弧线（如「火光橙 → 油灯琥珀 → 霓虹粉 → 屏幕冷蓝」）。枚举拿不准就省略那一栏。

只回一行 JSON，不要别的字：
{"style_grammar": "…", "art": {"look": "…", "brush": "…", "light": "…", "texture": "…", "type": "…", "motion": "…", "palette_note": "…"}, "shots": [{"id": "s01", "entities": ["…", "…"], "brief": "…"}]}"""


def _parse(text: str) -> dict[str, Any] | None:
    """从回话里取那一行 JSON；形状不对就 ``None``。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(got, Mapping):
        return None
    return dict(got)


def _ask(spec: Mapping[str, Any], todo: Sequence[Mapping[str, Any]],
         user_request: str = "") -> str:
    style = str(spec.get("style") or "").strip() or "（没写，按选题自己定）"
    art = spec.get("art") if isinstance(spec.get("art"), Mapping) else {}
    art_bits = "；".join(f"{k}={v}" for k, v in art.items() if str(v or "").strip())
    prop = str(spec.get("recurring_prop") or "").strip()
    protagonist = spec.get("protagonist")
    lines = []
    # 用户原话放最前面：导演改写 brief 时，最容易丢的就是「用户点名要/不要的东西」。
    req = " ".join(str(user_request or "").split())
    if req:
        lines += ["=== 用户原话（最高事实）===",
                  req[:4000] + ("…" if len(req) > 4000 else ""),
                  "用户点名要的、点名不要的、说的年代与语气，都以此为准；"
                  "改写 brief 时不许把它替换成默认画法。", ""]
    lines += [
        f"整片风格：「{style}」",
        f"画幅：{spec.get('aspect') or 'landscape'}",
    ]
    shell = str(spec.get("shell") or "").strip()
    if shell:
        # 片壳是整片契约：外框与底部字幕归壳，brief 只描述画面带里那一镜的主体。
        lines.append(
            f"整片片壳：{shell}——暖纸底、顶部色条、左上圆钟、右上期号与底部字幕由片壳"
            "统一画，全片同一位置与字号。每一镜的 brief 只写画面带里的主体、场景与动作，"
            "不要给字幕的位置或字号，也不要写底色、色条、钟或期号。")
    if art_bits:
        lines.append(f"整片美术方向：{art_bits}")
    if prop:
        lines.append(f"整片贯穿装置：「{prop}」")
    if isinstance(protagonist, Mapping):
        look = str(protagonist.get("look") or "").strip()
        if look:
            # `kind` 是自由文本，**不猜成「人」**：走路的猫、飞行的鸟都走同一条路。
            kind = str(protagonist.get("kind") or "").strip()
            render = str(protagonist.get("render") or "silhouette").strip()
            wardrobe = str(protagonist.get("wardrobe") or "").strip()
            extra = f"；外观/材质随时代：{wardrobe}" if wardrobe else ""
            subject = f"同一个{kind}" if kind else "同一个主体"
            lines.append(
                f"整片贯穿主体（**{subject}，每镜都要在**；画法 {render}）："
                f"「{look}」" + extra)
            if wardrobe:
                # 2026-10-20：用户要「同一个人换衣服」，不是每镜重新发明一个人。
                # brief 负责写清这一镜**穿什么、戴什么、手里拿什么**；骨架、身高、
                # 步态仍由整片那一组连续性数字守住。
                lines.append(
                    "每一镜的 brief 都要写清这一镜的衣着、头饰与持物（照上面的演进线"
                    "和用户原话，按时代换），但**不要另换一副身体**：身高、骨架、"
                    "朝向与步态整片不变，换的只是外面这一层。")
    pack = spec.get("style_pack") if isinstance(spec.get("style_pack"), Mapping) else {}
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    if pack:
        # 风格包是**用户选定的整片契约**：它排在逐镜输入之前，导演先看到它再排画面。
        lines.append("")
        lines.append(f"风格包：「{pack.get('display') or pack.get('name')}」"
                     f"（{pack.get('name')}，模板 {manifest.get('template') or '无'}）")
        for key, label in (("composition", "构图与相机"),
                           ("recurring_pose", "贯穿姿势"),
                           ("recurring_prop", "贯穿道具"),
                           ("caption", "字幕"),
                           ("inscription", "铭文"),
                           ("avoid", "不要出现"),
                           # 用户在前端写给导演这一层的话（2026-10-21）：属于这份风格
                           # 的数据，不是后端的风格分支；空着整行不出现。
                           ("art_note", "用户补充：写给分镜导演")):
            value = manifest.get(key)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if str(value or "").strip():
                lines.append(f"  {label}：{str(value)[:1200]}")
        # 沉淀出来的三个角度（visual / motion / text）：导演排 brief 时就要把它们
        # 拆进每一镜，不能让写手到写代码时才发现「这一镜该穿什么、写什么字」。
        facets = manifest.get("facets")
        if isinstance(facets, Mapping):
            motion = facets.get("motion") if isinstance(facets.get("motion"), Mapping) else {}
            film = [t for t in (motion.get("tracks") or [])
                    if isinstance(t, Mapping) and t.get("scope") == "film"]
            if film:
                lines.append("  动作纪律：主体属于**整片连续层**（"
                             + " / ".join(str(t.get("component") or t.get("name") or "")
                                          for t in film[:4])
                             + "）。每一镜的 brief 要写清这一镜里主体在做什么动作，"
                               "但**不要把它写成每镜重新起步**——步态/位移由全片时钟连起来。")
            text = facets.get("text") if isinstance(facets.get("text"), Mapping) else {}
            items = [i for i in (text.get("items") or []) if isinstance(i, Mapping)]
            if shell:
                # 字幕归片壳，brief 不再给它的位置/字号；画面里的铭文与招牌字照旧。
                items = [i for i in items
                         if str(i.get("kind") or "caption").strip().lower()
                         not in ("caption", "subtitle", "字幕")]
            if items:
                lines.append("  文字纪律：每一镜 brief 要给这些文字的位置、字号与内容——"
                             + "；".join(
                                 f"{i.get('kind') or 'caption'}（位置 "
                                 f"{i.get('position') or '照风格不变量'}）"
                                 for i in items[:4])
                             + ("（铭文写年代与地点；底部字幕由片壳统一画，brief 不用管）。"
                                if shell else
                                "（铭文写年代与地点，字幕用本镜文案；用模板组件或"
                                "自己画都行，但**字幕只有一条**）。"))
            changed: list[str] = []
            for fname in ("visual", "motion", "text"):
                facet = facets.get(fname)
                if not isinstance(facet, Mapping):
                    continue
                for v in facet.get("variations") or []:
                    if isinstance(v, Mapping) and v.get("prop"):
                        vals = " / ".join(str(x) for x in (v.get("values") or [])[:4])
                        changed.append(f"{v.get('name') or v.get('prop')}"
                                       + (f"（{vals}）" if vals else "")
                                       + f" → `{v.get('prop')}`")
            if changed:
                lines.append("  每一镜 brief 要写明这些会变的维度取哪一档："
                             + "；".join(changed[:8]) + "。")
        # 新沉淀没有 facets：层与时间线是这份风格包唯一的编排来源。导演在写 brief
        # 之前就要看到「参考片有哪几层、什么先出现」，否则又退回「一页排版」。
        # 层名/字段名由读片模型定，这里只按结构渲染，不解释键名。
        layers = manifest.get("layers")
        if isinstance(layers, (list, tuple)) and layers:
            lines.append("  画面分层（每一层在这一镜里都要有落点；缺省画法只是参考）：")
            for layer in layers[:12]:
                if not isinstance(layer, Mapping):
                    continue
                name = str(layer.get("name") or layer.get("component") or "").strip()
                if not name:
                    continue
                default = str(layer.get("default") or "").strip()
                fields = layer.get("fields")
                detail = ""
                if isinstance(fields, Mapping) and fields:
                    detail = "；".join(f"{k}={v}" for k, v in list(fields.items())[:6])
                lines.append(f"  - {name}" + (f"：{default[:160]}" if default else "")
                             + (f"（取值：{detail[:200]}）" if detail else ""))
        timeline = manifest.get("timeline")
        if isinstance(timeline, (list, tuple)) and timeline:
            lines.append("  参考片时间线（复刻顺序与节奏，内容按本镜文案换）：")
            for i, seg in enumerate(timeline[:8], 1):
                if not isinstance(seg, Mapping):
                    continue
                t = seg.get("t")
                at = f" t={float(t):.1f}s" if isinstance(t, (int, float)) else ""
                camera = str(seg.get("camera") or "").strip()
                lines.append(f"  - 段 {i}{at}" + (f"：{camera[:120]}" if camera else ""))
        body = str(pack.get("body") or "").strip()
        if body:
            lines.append("风格包正文：")
            lines.append(body)
        lines.append("")
    if isinstance(manifest, Mapping) and manifest.get("repeat_composition"):
        lines.append("构图纪律：本片刻意重复同一构图——主体位置、贯穿光块位置、地平线、"
                     "字幕位置每镜都一样；只换时代背景、服装、道具内容与光色。"
                     "不要为了变化而换机位、换主体位置或换字幕位置。")
    else:
        lines.append("构图纪律：不要把每一镜写成同一种构图；相邻镜要有变化。")
    lines.append("逐镜输入：")
    for s in todo:
        sid = str(s.get("id") or "")
        text = str(s.get("text") or "").strip()
        brief = str(s.get("brief") or "").strip() or "（没写）"
        subject = str(s.get("subject") or "").strip()
        entities = [str(e).strip() for e in (s.get("entities") or []) if str(e).strip()]
        lines.append(f"- {sid} 文案：{text}")
        lines.append(f"  现有 brief：{brief}")
        if subject:
            lines.append(f"  已申报主体：{subject}")
        if entities:
            lines.append(f"  已申报实体：{'、'.join(entities)}")
    # 有贯穿主体就写这一条，不必等风格包：2026-10-20 真机片没选风格包，导演把
    # 「站着看戏」原样写进 brief，40 镜里好几镜主体站死，用户说「人在走动的怎么
    # 不连贯」。步态是整片契约的一部分，必须落到每一镜的动作描述里。
    _has_lead = (isinstance(protagonist, Mapping)
                 and bool(str(protagonist.get("look") or "").strip()))
    if str(manifest.get("recurring_pose") or "").strip() or _has_lead:
        lines.append("每一镜的 brief 都要写清**主体怎么动**（从哪走到哪、边走边做什么、"
                     "在哪停半步再走），不能只写「主体在画面里」或「站在某处看着」——"
                     "成片里主体必须一直在动，停也只停半步。")
    lines.append("给每一镜圈实体、补画面落点；把整片风格拆成可执行做法。只回那一行 JSON。")
    return "\n".join(lines)


async def concretize(spec: dict[str, Any], *, user_id: str = "",
                     conversation_id: str = "", llm: Any = None,
                     user_request: str = "") -> dict[str, Any]:
    """补 `spec["style_grammar"]` 与无 code 镜的 `brief`（原地改）→ 一份账。

    返回 ``{enabled, changed, style_grammar, notes, cost_yuan, calls}``。
    不抛：这一步是增益，不是闸——拿不到回话就照原 brief 继续写。
    """
    from agent_framework.cost import cost_yuan

    from .selfcheck import _complete_with_identity

    out: dict[str, Any] = {
        "enabled": DIRECTOR_ENABLED, "changed": [], "style_grammar": "",
        "notes": [], "cost_yuan": 0.0, "calls": 0}
    if not DIRECTOR_ENABLED:
        return out
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    todo = [s for s in shots if not str(s.get("code") or "").strip()]
    if not todo:
        return out

    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _ask(spec, todo, user_request)}]
    kwargs: dict[str, Any] = {
        "max_tokens": DIRECTOR_MAX_TOKENS, "timeout": DIRECTOR_TIMEOUT_SEC}
    if DIRECTOR_MODEL:
        kwargs["model_override"] = DIRECTOR_MODEL
    try:
        resp = await _complete_with_identity(
            messages, llm=llm, user_id=user_id,
            conversation_id=conversation_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 导演调用炸了不改 brief
        out["notes"].append(
            f"美术导演未判：调用失败（{type(exc).__name__}: {str(exc)[:120]}），"
            "这一版按原 brief 写。")
        return out
    out["calls"] = 1
    usage = getattr(resp, "usage", None)
    if usage:
        out["cost_yuan"] = round(
            float(cost_yuan(usage, model=str(usage.get("model") or ""))), 4)
    got = _parse(str(getattr(resp, "content", "") or ""))
    if got is None:
        out["notes"].append("美术导演未判：回话不是那一行 JSON，这一版按原 brief 写。")
        return out

    grammar = str(got.get("style_grammar") or "").strip()
    if grammar:
        spec["style_grammar"] = grammar[:220]
        out["style_grammar"] = spec["style_grammar"]

    from .code_spec import ENTITY_MAX_CHARS, MAX_ENTITIES_PER_SHOT, norm_art

    # 整片美术方向：主对话漏写时由导演补一份（2026-10-19 真机：21 镜只写了风格那句话，
    # `art` 是空的，写手拿不到笔触/光位/色温弧线，21 镜全用同一个冷色 accent）。
    # 只补不覆盖：用户或主对话已经写了 `art` 时，导演的意见不生效。
    art_raw = got.get("art")
    if isinstance(art_raw, Mapping) and not spec.get("art"):
        art, _art_errors = norm_art(dict(art_raw))
        if art:
            spec["art"] = art
            out["art"] = art
            out["notes"].append(
                "美术导演：整片没写 `art`，按风格补了整片美术方向"
                f"（{', '.join(f'{k}={v}' for k, v in art.items())}）。")

    rows = {str(s.get("id")): s for s in todo}
    for item in got.get("shots") or []:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("id") or "").strip()
        if sid not in rows:
            continue
        touched = False
        raw_entities = item.get("entities")
        if isinstance(raw_entities, str):
            raw_entities = re.split(r"[、,，;；/]", raw_entities)
        if isinstance(raw_entities, (list, tuple)):
            names: list[str] = []
            for ent in raw_entities:
                name = str(ent or "").strip()
                if name and name not in names:
                    names.append(name[:ENTITY_MAX_CHARS])
                if len(names) >= MAX_ENTITIES_PER_SHOT:
                    break
            if names:
                rows[sid]["entities"] = names
                touched = True
        brief = str(item.get("brief") or "").strip()
        if brief:
            rows[sid]["brief"] = brief[:BRIEF_MAX_CHARS]
            touched = True
        if touched:
            out["changed"].append(sid)
    out["changed"].sort()
    if out["changed"]:
        out["notes"].append(
            f"美术导演：把整片风格拆成可执行做法，并给 {len(out['changed'])} 镜"
            f"圈了实体、补了画面落点"
            f"（{'、'.join(out['changed'][:8])}"
            + ("…" if len(out["changed"]) > 8 else "") + "）。")
    return out
