# -*- coding: utf-8 -*-
"""三个自检工具：把闸的判据交到模型手里，让它在交活**之前**自己查。

**它填的是哪一个洞。** 闸原来的位置是「模型交完 → 服务端判 → 不合格打回 → 模型再来
一轮」。模型从头到尾**看不到判据**，只能撞上去才知道，而一次打回的代价是整轮重来。
这三个工具把同一批判据**原样**包成工具：想查就查，查完再交——**判据一个字节没改，
换的只是「谁先看到」**。

**为什么是工具不是闸。** 产品口径是「它编译不了代码就包装成为工具」。判据本身是对的，
错的是「只有服务端跑得到」。所以这里**只包装、不新增判据**：

    check_shot_code ← remotion_client.lint()              闸①（白名单 / 字段对账 / 纯函数）
    check_film      ← code_spec.whole_film_gate_results()  整片九道闸 + style_facts
    preview_shot    ← remotion_client.render_stills() + 一次带图子调用

回话与闸的回话**同源**，不在这里抄第二份。抄第二份的表现是「工具说绿、闸说红」，
而那种漂在离线用例里看不出来。

**三出口，一条不许合并的纪律。** 每个出口都不是「过/不过」两态：渲不出来、服务不可达
时，答案是「**未判**」——既不是通过，也不是失败。这跟 `judge_channel` 里那条
「未判 ≠ 通过」是同一条（P5）。把未判写成绿，就是把「没量过」记成「量过」。

**为什么住在 storyline 侧而不是 agent_framework。** 它们包的全是**这个进程里**的函数
（`code_spec` / `remotion_client`），而 `code_spec` 活在 editor 容器——`MOTION_QC` 也在
那个容器的环境里。放进 `agent_framework` 就等于在 app 容器里算第二遍闸：同一份判据、
两个进程、两套配置，是这仓库反复点名的「清单漂移」最标准的形状。所以它们与
`dag_contract` 同路：由剪辑服务端声明、经 MCP 进主服务注册表。

**三个都不带 `contract`**（它们不是 DAG 节点）——这正是「两轮都可见、永不被裁」的来源，
见 `agent_framework/plan/round.py` 的 `_kept`：`contract is None` 的工具一律保留。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import code_render as crender
from . import code_spec as cspec
from . import remotion_client as rc

__all__ = ["check_shot_code", "check_film", "preview_shot",
           "PREVIEW_AT", "DEFAULT_PREVIEW_SEC"]

#: 按镜长的哪几个比例取帧。挑三个点而不是一帧：只看第 0 帧常常正好落在入场动画的
#: 空白处，那是**取景取错了**，不是画面空。0.15 / 0.5 / 0.85 与闸④ 的采样点是**同一组
#: 比例**——同一件事在两处用两套比例，迟早一处判空壳、另一处判正常。
PREVIEW_AT: tuple[float, ...] = (0.15, 0.5, 0.85)

#: 不指定时长时按多少秒算这一镜。模型写代码的当口还没定死时长，给一个够长的缺省：
#: 只要不短于它自己的入场动画，取到的帧就能代表画面。
DEFAULT_PREVIEW_SEC = 3.0


def _fmt_lint_error(e: Any) -> str:
    """`{rule,line,column,msg}` → 一行可读的话。逐字保留 rule 与 msg——模型要按它改。"""
    if not isinstance(e, Mapping):
        return str(e)
    rule = str(e.get("rule") or "")
    msg = str(e.get("msg") or e.get("message") or "")
    line, col = e.get("line"), e.get("column")
    where = f"第{line}行" if line else ""
    if col:
        where = f"{where}第{col}列" if where else f"第{col}列"
    head = f"[{rule}] " if rule else ""
    return f"{head}{where} {msg}".strip()


def check_shot_code(code: str, *, declared_fields: Sequence[str] | None = None,
                    client: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """闸① 自检：这一段 TSX 过不过白名单 / 字段对账 / 逐帧纯函数那几条。

    ``declared_fields`` 是这一镜声明的字段名。**显式传**，别让服务端猜——字段对账那三条
    规则拿它比，而名单不在代码字节里（与 `remotion_client.lint` 同口径）。
    """
    src = str(code or "")
    if not src.strip():
        return {"verdict": "red",
                "errors": ["代码是空的——这一镜画面上不会有任何东西（闸⑦「每镜必画」）"],
                "declared": [], "note": ""}
    lint = (client or rc.endpoints()).get("lint")
    fields = list(declared_fields) if declared_fields is not None else None
    try:
        got = lint(src, fields)
    except rc.RemotionError as exc:
        # 服务不可达**不许**当成绿：那是「没量过」。与 `_prelint` 修静默通过同一条理由。
        return {"verdict": "undetermined", "errors": [], "declared": [],
                "note": f"未判：渲染服务不可达（{exc}）。**这不是通过**——这段代码没有"
                        f"经过闸①。可以照交，但闸会在渲染那一步再判一次。"}
    errors = [_fmt_lint_error(e) for e in (got.get("errors") or [])]
    ok = bool(got.get("ok"))
    return {"verdict": "green" if ok else "red",
            "errors": errors,
            "declared": [str(x) for x in (got.get("declared") or [])],
            "note": "" if ok else "逐条改掉再交。这些就是闸① 的规则，一个字节没改。"}


def _jsonable(obj: Any) -> Any:
    """把返回值里**进不了 JSON** 的东西掰直——工具边界上的那一层。

    为什么在这一层做，而不是去改 `code_spec.style_facts`：`style_facts` 是内部 API，
    `all_channels[sid]` 用集合表达「这一镜用上了哪几条通道」是它的自然形状，回执与
    计划卡读的也是集合语义。而**工具的返回值要过 MCP 的 JSON 序列化**——一个 `set`
    就足以让整次调用炸在那一层，判据本身却一点毛病没有。

    真机代价（2026-10-16，run 9272c7f59501，20 镜那条）：`check_film` **每次**调用都回
    `Error: Object of type set is not JSON serializable`，模型连试四次（seq 41/50/53/59）
    才确认它没用，退回逐镜 `check_shot_code`——**「闸变工具」这条线刚上线就有一半是空的**。
    而离线用例全绿：它们只比 errors/warnings/undetermined 三个字符串列表，
    从来没有把这个返回值真的序列化过一次。所以这里只做「掰直」，不碰判据。

    集合 → **排序后的列表**（不是原样 list()）：顺序稳定，两次调用、两个进程之间可比。
    """
    if isinstance(obj, Mapping):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def check_film(shots: Sequence[Mapping[str, Any]], *,
               with_facts: bool = True) -> dict[str, Any]:
    """整片自检：整片判据的三个出口 + 风格账。

    **与服务端判的是同一份**（`code_spec.whole_film_gate_results`，判据一个字节不改），
    而且从 2026-10-16 起服务端也**只报不拦**了——所以这里的读数就是入库/出片时会写在
    计划卡与回执上的那一串，不是"另一档更严的估算"。没有 `strict` 参数：并档之后
    整片判据不分档（见 `code_spec.SOFT_GATES` 上面那段）。

    返回值**必须过 `_jsonable`**：它要经 MCP 交出去，而风格账里有集合。
    """
    rows = [dict(s) for s in (shots or ()) if isinstance(s, Mapping)]
    errors, warns, undetermined = cspec.whole_film_gate_results(rows)
    verdict = "red" if errors else ("undetermined" if undetermined else "green")
    out: dict[str, Any] = {"verdict": verdict, "shots": len(rows),
                           "errors": list(errors), "warnings": list(warns),
                           "undetermined": list(undetermined)}
    if with_facts:
        try:
            out["facts"] = cspec.style_facts(rows)
        except Exception as exc:      # noqa: BLE001 —— 账算不出来不该带塌这次自检
            out["facts_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return _jsonable(out)


# ---------------------------------------------------------------------------
# preview_shot：渲几帧，让模型自己看一眼
# ---------------------------------------------------------------------------

#: 审读员。**它不改代码**——回话原样交给写这一镜的那个模型，由它自己决定改不改。
#: 这条分工是刻意的：让审读员顺手改代码，就等于在系统里又长出一个隐形的写手。
_PREVIEW_SYSTEM = """你是这个视频项目里的**画面审读员**。你收到的是一镜渲染出来的几张静帧。
你的任务**只有一个**：如实说出画面上有什么。你不改代码、不给方案、不提建议——你的回话
会原样交给写这一镜代码的那个模型，供它自己判断。

判读纪律：
· 只说你**看得见**的。看不清就说看不清，不要按常识补全（这一镜画什么不由你猜）。
· **空帧必须点名**：只说「这一帧是空的」就够，比任何委婉说法都有用。
· 逐帧分开说，别把几帧并成一句「整体来看」。
"""


def _still_url(still: Mapping[str, Any]) -> str:
    """`{frame, jpeg_b64}` → `data:` URL。渲染容器回的就是 base64，这里不再编码一次。"""
    b64 = str(still.get("jpeg_b64") or still.get("b64") or "")
    if not b64:
        return ""
    mime = str(still.get("mime") or "image/jpeg")
    return f"data:{mime};base64,{b64}"


def _preview_ask(*, frames: Sequence[int], question: str) -> str:
    lines = [f"下面是这一镜渲出来的 {len(frames)} 张静帧"
             f"（帧号 {', '.join(str(f) for f in frames)}）。",
             "逐帧说清楚：画面上**实际有什么**（元素、文字、构图、大致位置）。",
             "如果某一帧**基本是空的**（只有底色、没有可辨认的内容），直接说出来——"
             "「这一帧是空的」是这条自检最重要的答案，别用「比较简洁」这类话带过去。"]
    if question:
        lines.append(f"调用方另外想问：{question}")
    lines.append("只描述你看到的。不要改代码、不要贴代码。")
    return "\n".join(lines)


def _default_llm() -> Any:
    """生成那一半走全项目共享的主 LLM（与 `judge_channel._llm` 同一条）。"""
    from agent_framework.llm_openai import get_default_llm
    return get_default_llm()


def _preview_frames(total: int, at: Sequence[float] | None = None) -> list[int]:
    """镜长 → 取哪几帧（去重、升序、钳在片子范围内）。"""
    total = max(2, int(total))
    return sorted({min(total - 1, max(0, int(round(total * f))))
                   for f in (at or PREVIEW_AT)})


async def _complete_with_identity(messages: list[dict[str, Any]], *, llm: Any,
                                  user_id: str, conversation_id: str,
                                  **kwargs: Any) -> Any:
    """调一次模型，把**调用方注入的身份**绑在这一次调用上。

    为什么要显式绑：本模块跑在剪辑服务端——**另一个进程**，Agent 那条身份 contextvar
    不过 HTTP。不绑，`secrets.resolve_api_key` 的 `current_user_id()` 就是空串，
    查不到这位用户的页面配置，键永远解析不出来（真机表现是「未配置模型密钥」，
    而键其实配着）。节点的做法是同一个道理：`server.py` 从注入的 `user_id` 显式取。

    ``**kwargs`` 原样交给 ``complete``（``max_tokens`` / ``timeout``，见
    ``shot_writer`` 的写代码调用）。缺省不传时行为与从前完全一致。
    """
    from agent_framework.identity import use_identity
    client = llm or _default_llm()
    if not user_id:
        return await client.complete(messages, **kwargs)
    with use_identity(user_id, conversation_id):
        return await client.complete(messages, **kwargs)


async def preview_shot(*, code: str, shot: Mapping[str, Any] | None = None,
                       spec: Mapping[str, Any] | None = None,
                       duration_sec: float = DEFAULT_PREVIEW_SEC,
                       at: Sequence[float] | None = None, question: str = "",
                       user_id: str = "", conversation_id: str = "",
                       client: Mapping[str, Any] | None = None,
                       llm: Any = None) -> dict[str, Any]:
    """渲这一镜的几帧静帧，让模型看一眼，把「看到了什么」交回来。

    **为什么必须由工具内部发一次带图子调用。** 工具返回值经 `messages.tool_result()`
    会被 `str()` 掉——把图片内容块直接交出去，模型收到的是一串 base64 而不是画面。
    所以画面**在这里**被消费掉，交出去的是看过之后的话。这个形状仿
    `judge_channel.judge_frame`（同一件事：让判断模型看得见画面），只是那边要的是
    代码提案，这边要的是**如实描述**。

    **注入参数一个都不在这里拼。** `durationInFrames` / `fields` / `captionFromCode`
    全走 `code_render.shot_input_props`——那是 `/render-shot` 与 `/render-stills` 共用的
    同一个出口。在这里拼第二份的表现是「预览到的这一张与成片里的那一帧不是同一张」；
    而少注一个 `durationInFrames`，渲染容器直接 400（那是闸② 的注入点）。

    ``user_id`` / ``conversation_id`` 由主服务的 MCP 包装层注入（模型自报的不作数）：
    这一次子调用要按**那位用户**的键走，理由见 `_complete_with_identity`。
    """
    src = str(code or "")
    if not src.strip():
        return {"ok": False, "frames": [], "review": "",
                "note": "没有代码可渲——先把它写出来再来看。"}
    row = dict(shot) if isinstance(shot, Mapping) else {}
    row["code"] = src
    raw = dict(spec) if isinstance(spec, Mapping) else {}
    # 只预览**这一镜**：`spec` 在这里只借它的几何缺省（aspect / fps / width / height 由
    # `validate_code_spec` 补全，那是这一组缺省的唯一出处），不借它的 shots。
    raw["shots"] = [row]
    norm, _errors = cspec.validate_code_spec(raw)
    row = (norm.get("shots") or [row])[0]
    fps = int(norm.get("fps") or 30)
    total = max(2, int(round(float(duration_sec) * fps)))
    picked = _preview_frames(total, at)
    try:
        props = crender.shot_input_props(row, norm, frames=total, frame_words=[])
    except Exception as exc:      # noqa: BLE001 —— 组不出注入参数＝这一趟未判
        return {"ok": False, "frames": picked, "review": "",
                "note": f"未判：这一镜的注入参数没组出来（{type(exc).__name__}: "
                        f"{str(exc)[:160]}）。**这不是通过**。"}
    stills = (client or rc.endpoints()).get("stills")
    try:
        got = stills(src, props, picked)
    except rc.RemotionError as exc:
        return {"ok": False, "frames": picked, "review": "",
                "note": f"未判：这一镜没能渲出静帧（{exc}）。**这不是通过**——可以照交，"
                        f"但「画面上有没有东西」要等真正渲染那一步才判。"}

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": _preview_ask(frames=picked, question=question)}]
    for still in got:
        if isinstance(still, Mapping):
            url = _still_url(still)
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) == 1:
        return {"ok": False, "frames": picked, "review": "",
                "note": "渲染服务这一趟没回任何一帧——记「未判」，不是「没问题」。"}

    messages = [{"role": "system", "content": _PREVIEW_SYSTEM},
                {"role": "user", "content": parts}]
    try:
        resp = await _complete_with_identity(messages, llm=llm, user_id=user_id,
                                             conversation_id=conversation_id)
    except Exception as exc:      # noqa: BLE001 —— 一次调用炸了不该带塌整条链（同 judge_channel）
        return {"ok": False, "frames": picked, "review": "",
                "note": f"未判：看帧的调用炸了（{type(exc).__name__}: {str(exc)[:200]}）。"}
    text = str(getattr(resp, "content", "") or "").strip()
    if not text:
        # 空回复**不许**读成「画面没问题」：那是模型没说话，不是它看过之后说没事。
        return {"ok": False, "frames": picked, "review": "",
                "note": "模型这一趟没吐任何东西——记「未判」，不是「没问题」。"}
    return {"ok": True, "frames": picked, "review": text, "note": "",
            # 真机 2026-10-19 run `3dd70754ba89`：三镜预览都成功之后，模型把三份
            # 一字不差的代码又重写了两遍，6 轮零变化后被无进展守卫停下——它以为
            # `shots/*.tsx` 写完就算交付。预览只是自查；成片只读 `plan_motion` 里的
            # brief/code，不读工作区里的草稿文件。这句话每次预览都带回去。
            "next": "预览只是自查，不是交付。这一镜满意后把它交进 `plan_motion`"
                    "（交 `brief`；想自己写就带 `code`），全部镜齐了再调 "
                    "`render_motion_code`。写进工作区的 `shots/*.tsx` 只是草稿，"
                    "出片不会读它。"}


# ---------------------------------------------------------------------------
# 风格对照（2026-10-18）：出片前，用带图模型逐镜看一眼「这一镜像不像用户要的那种风格」
# ---------------------------------------------------------------------------
#
# 为什么要有它：风格由用户的话决定（`spec["style"]` 那一句，模型自己从用户要求里写出来），
# 但每一镜做成什么样仍是模型自己挑的卡、自己写的代码——真机 run（「高级偏杂志风」）里
# s05 是像样的杂志版式（眉题 + 衬线斜体大标题 + 细栏线），s02 却是一张 PPT 式对比表格、
# s01 是霓虹灯大字。机器量不出「杂志感」，所以这里不设判据、不拦：拿预览静帧去问一个
# 看得见图的模型，不像的镜带着改法写进回执，模型出片前可以用 shots_patch 只改那几镜。
#
# 花钱：每镜一张小图 + 几百字，一条 20 镜的片子约 0.05~0.2 元。花费随回执带回
# （`cost_yuan`），主循环把它记进这条 run 的预算（见 `agent._charge_nodes`）。

_STYLE_SYSTEM = """你是这条视频的**美术指导**。你会看到一镜画面的静帧，以及这条片子要的整体风格（一句话）。
你只判断一件事：这一镜的**版式与视觉语言**符不符合那一句风格。

判读纪律：
· 画面最底下那一两行大字是字幕，由系统统一排，**不算**这一镜的版式，别拿它扣分。
· 只看版式、字体层级、配色、线条、留白、构图这些，不评文案写得好不好。
· 像 PPT 的就是不符合：卡片墙、表格框、项目符号、一堆圆角色块、所有东西居中堆着。
· 与风格冲突的语汇也是不符合：比如要「杂志风」却用霓虹灯管字、科幻发光、游戏 UI。
· 拿不准就判符合。你是在帮它找**明显**不对的镜，不是挑刺。

只回一行 JSON，不要别的字：
{"fits": true 或 false, "why": "≤40 字，说你看到了什么", "fix": "≤60 字，不符合时怎么改；符合时留空"}"""

#: 同时看几镜（受供应商并发与渲染无关，纯网络请求）。
STYLE_REVIEW_PARALLEL = 4


def _parse_verdict(text: str) -> dict[str, Any] | None:
    import json
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(got, Mapping) or not isinstance(got.get("fits"), bool):
        return None
    return dict(got)


async def style_review(*, style: str, shots: Sequence[Mapping[str, Any]],
                       stills: Mapping[str, Any], user_id: str = "",
                       conversation_id: str = "", llm: Any = None) -> dict[str, Any]:
    """逐镜看预览静帧对照整片风格 → ``{misfits, unjudged, checked, cost_yuan}``。

    ``stills`` 是 ``{镜号: 图片文件路径}``（`code_render.preview_stills` 的产物）。
    任何一镜看不成（没图、调用炸了、回话不是那一行 JSON）都记 ``unjudged``——
    **未判不是符合**，回执里如实写。整个函数不抛：这是一份增益，不是闸。
    """
    import asyncio
    import base64
    from pathlib import Path

    from agent_framework.cost import cost_yuan

    out: dict[str, Any] = {"misfits": [], "unjudged": [], "checked": 0, "cost_yuan": 0.0}
    if not str(style or "").strip():
        return out
    gate = asyncio.Semaphore(STYLE_REVIEW_PARALLEL)
    texts = {str(s.get("id")): str(s.get("text") or "") for s in shots if isinstance(s, Mapping)}

    async def one(sid: str, path: Any) -> None:
        try:
            b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            out["unjudged"].append(sid)
            return
        ask = (f"整片要的风格：「{style}」\n这一镜的文案：「{texts.get(sid, '')}」\n"
               "下面是这一镜的一张静帧。它的版式符不符合那一句风格？")
        messages = [{"role": "system", "content": _STYLE_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": ask},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]
        async with gate:
            try:
                resp = await _complete_with_identity(messages, llm=llm, user_id=user_id,
                                                     conversation_id=conversation_id)
            except Exception:  # noqa: BLE001 —— 一镜看不成不带塌整份
                out["unjudged"].append(sid)
                return
        usage = getattr(resp, "usage", None)
        if usage:
            out["cost_yuan"] += cost_yuan(usage, model=str(usage.get("model") or ""))
        verdict = _parse_verdict(str(getattr(resp, "content", "") or ""))
        if verdict is None:
            out["unjudged"].append(sid)
            return
        out["checked"] += 1
        if not verdict["fits"]:
            why = str(verdict.get("why") or "").strip()[:60]
            fix = str(verdict.get("fix") or "").strip()[:90]
            out["misfits"].append(f"{sid}：{why}" + (f"——改法：{fix}" if fix else ""))

    await asyncio.gather(*(one(str(sid), p) for sid, p in sorted(stills.items())))
    out["misfits"].sort()
    out["unjudged"].sort()
    out["cost_yuan"] = round(out["cost_yuan"], 4)
    return out
