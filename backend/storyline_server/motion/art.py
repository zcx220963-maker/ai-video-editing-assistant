# -*- coding: utf-8 -*-
"""自定义画面通道：模型自己写的 SVG 先过这里，再进模板。

这条通道存在的理由：参考片里有两类画面是版式组件盖不住的——一类是**为这一条片子
画的示意图**（原子钟的能级跃迁、海底电缆的剖面），一类是**连续运动**。硬编码组件
每加一个都要人先画一遍，所以这里把画图的笔交给模型，服务端只当**闸**，不当作者。

三条不可让的口径，都在 ``clean_svg`` 里落地：

1. **只放行绘图，不放行执行**。元素与属性都用白名单：新增的 SVG 能力（脚本、
   外链、可执行滤镜）默认进不来，而不是我们把已知的坏东西删干净。
2. **一帧 = 一个时刻的纯函数**。SMIL（``<animate>`` / ``<animateTransform>`` /
   ``<set>``）按墙钟推进，而 headless 逐帧截图一次只推得起 1~2 个 rAF，动画会冻在
   起始帧——所以整类拒绝。要动就写 ``data-anim``，由模板的 ``apply(t)`` 按时刻算。
3. **画不下就说画不下**。``viewBox`` 必填：它是横竖两种画幅共用一套坐标的唯一办法
   （见 templates.py 的画面带），没有它就只能按像素摆，横屏必然错位。

净化是**幂等**的：校验时过一次（``spec.validate_spec``），编译页面时再过一次
（``templates.compile_art``）。两条渲染入口都汇到 ``validate_spec``，但出片页只认
经过这两道的字节，谁将来多开一条路径也不会漏。
"""

from __future__ import annotations

import html
import re
from typing import Any

#: 单镜 SVG 的容量。逐帧截图按启动计费，几千元素的一张图会把每帧的排版时间抬高；
#: 这两个上限是「一屏画得下的示意图」的经验值，不是审美判断。
MAX_SVG_CHARS = 20000
MAX_ELEMENTS = 400
MAX_ATTR_CHARS = 4000

#: 允许出现的元素。白名单：往后 additions 只会让画面更受限，不会更危险。
ALLOWED_ELEMENTS = frozenset({
    "svg", "g", "defs", "title", "desc", "symbol", "use",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textPath",
    "linearGradient", "radialGradient", "stop", "marker",
    "clipPath", "mask", "filter", "feGaussianBlur", "feOffset",
    "feBlend", "feColorMatrix", "feMerge", "feMergeNode", "feFlood", "feComposite",
})

#: 见到就退回的元素（不静默删）：它们要么能执行，要么把动画挂在墙钟上。
#: 静默删会让模型看到一张「少了点什么」的画面却不知道少了什么。
FORBIDDEN_ELEMENTS = {
    "script": "script：画面里不许有脚本，动效用 data-anim 声明",
    "foreignobject": "foreignObject：只放 SVG 绘图元素，别嵌 HTML",
    "iframe": "iframe：不引外部文档",
    "object": "object：不引外部对象",
    "embed": "embed：不引外部插件",
    "image": "image：不引外部位图——这类片子没有素材",
    "animate": "animate 是 SMIL 动画，按墙钟走：headless 逐帧截图推不动它，画面会冻在"
               "起始帧。改成 data-anim 声明，由时刻 t 算",
    "animatetransform": "animateTransform 同样是 SMIL，在这条链路上会冻住",
    "animatemotion": "animateMotion 同样是 SMIL，在这条链路上会冻住",
    "animatecolor": "animateColor 同样是 SMIL，在这条链路上会冻住",
    "set": "set 也是 SMIL：用 data-anim 声明状态",
    "style": "style 块：样式写在 fill/stroke/font-size 这类属性上，别用 CSS 动画",
}

#: 允许的属性名（``data-*`` 另有规则，见 ``_ANIM_ATTRS``）。
ALLOWED_ATTRS = frozenset({
    "id", "class", "style", "transform", "transform-origin", "opacity", "visibility",
    "display", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-dasharray", "stroke-dashoffset", "vector-effect", "paint-order",
    "d", "points", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "width", "height", "dx", "dy", "rotate", "offset",
    "viewbox", "preserveaspectratio", "refx", "refy", "markerwidth", "markerheight",
    "markerunits", "orient", "clip-path", "clip-rule", "mask", "filter",
    "font-family", "font-size", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "letter-spacing", "word-spacing", "text-decoration",
    "stop-color", "stop-opacity", "gradientunits", "gradienttransform",
    "spreadmethod", "stddeviation", "in", "in2", "result", "operator", "mode",
    "k1", "k2", "k3", "k4", "type", "values", "href", "startoffset", "textlength",
    "lengthadjust",
})

#: 声明式动画的取值。每种都在 templates._JS 的 apply(t) 里有一段真实实现。
ANIM_KINDS = (
    "fade", "rise", "slide-x", "draw", "grow-x", "grow-y", "pop", "count",
    "wipe", "orbit", "pulse", "pan",
)

ANIM_LABELS = {
    "fade": "淡入", "rise": "上浮入场", "slide-x": "侧向滑入", "draw": "描出一条线",
    "grow-x": "横向长出来", "grow-y": "纵向长出来", "pop": "弹出", "count": "数字滚动",
    "wipe": "擦除显现", "orbit": "绕圈", "pulse": "明暗呼吸", "pan": "横移漂移",
}

#: 这几种**永不停止**：相位由 (t − at)/period 算，最后一帧也和第一帧不同。
#: 有它们在场，``settle_ms`` 只能算到镜头结束——逐帧截图的成本从「几帧」变成「整镜」。
#: 这是连续运动的价，技能文档里要写明。
CONTINUOUS_ANIMS = frozenset({"orbit", "pulse", "pan"})

#: 动画属性与各自区间。越界一律退回，不夹紧——夹紧会悄悄改掉模型的编排意图。
_ANIM_ATTRS: dict[str, tuple[float, float]] = {
    "data-at": (0.0, 180000.0),
    "data-dur": (1.0, 20000.0),
    "data-dist": (-4000.0, 4000.0),
    "data-period": (200.0, 60000.0),
    "data-count-to": (-1e12, 1e12),
    "data-dp": (0.0, 6.0),
}
#: count 的数量级后缀（万/亿/万亿）。它是文字不是数值，所以不进上面那张区间表。
NUM_UNIT_ATTR = "data-num-unit"
MAX_NUM_UNIT_CHARS = 8
ANIM_KIND_ATTR = "data-anim"
DEFAULT_ANIM_AT_MS = 0.0
DEFAULT_ANIM_DUR_MS = 600.0
DEFAULT_PERIOD_MS = 2400.0

SVG_NS = "http://www.w3.org/2000/svg"
_ATTR_SPLIT_RE = re.compile(
    r"""\s*([A-Za-z_:][-\w:.]*)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")
_TAG_HEAD_RE = re.compile(r"^(/?)\s*([A-Za-z_][-\w:.]*)")


class SvgError(ValueError):
    """模型写的 SVG 结构不合格。异常文本就是退回给模型的那句话。"""


def _forbidden(tag: str) -> str | None:
    return FORBIDDEN_ELEMENTS.get(tag.lower())


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _tokens(src: str) -> list[tuple[str, str, str]]:
    """源码 → [(kind, body, tag)]，kind ∈ text|comment|open|close|self。

    自己扫描而不用 ``<[^>]*>``：属性值里的 ``>`` 写在引号内是合法的，正则会把一条
    路径劈成两半。
    """
    out: list[tuple[str, str, str]] = []
    i, n = 0, len(src)
    while i < n:
        lt = src.find("<", i)
        if lt < 0:
            out.append(("text", src[i:], ""))
            break
        if lt > i:
            out.append(("text", src[i:lt], ""))
        if src.startswith("<!--", lt):
            end = src.find("-->", lt + 4)
            if end < 0:
                raise SvgError("注释没有闭合（<!-- … -->）")
            out.append(("comment", "", ""))
            i = end + 3
            continue
        if src.startswith("<!", lt) or src.startswith("<?", lt):
            raise SvgError("画面里不接受 <!DOCTYPE>、<!ENTITY>、CDATA 与 <?处理指令>")
        j, quote = lt + 1, ""
        while j < n:
            ch = src[j]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == ">":
                break
            j += 1
        if j >= n:
            raise SvgError(f"标签没有闭合：{src[lt:lt + 60]!r}…")
        body = src[lt + 1:j]
        m = _TAG_HEAD_RE.match(body)
        if not m:
            raise SvgError(f"读不懂的标签：{body[:40]!r}")
        slash, tag = m.group(1), _local(m.group(2))
        rest = body[m.end():]
        if slash:
            kind = "close"
        elif rest.rstrip().endswith("/"):
            kind, rest = "self", rest.rstrip()[:-1]
        else:
            kind = "open"
        out.append((kind, rest, tag))
        i = j + 1
    return out


def _attrs(src: str) -> list[tuple[str, str]]:
    """属性串 → [(name, value)]；无值属性按空串处理。"""
    pairs: list[tuple[str, str]] = []
    src = src.rstrip()
    pos = 0
    while pos < len(src):
        m = _ATTR_SPLIT_RE.match(src, pos)
        if not m or m.end() == pos:
            break
        value = next((g for g in m.group(2, 3, 4) if g is not None), "")
        pairs.append((m.group(1), value))
        pos = m.end()
    return pairs


def _num(value: str, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SvgError(f"{label}={value!r} 不是数字") from None


def _style_ok(value: str) -> bool:
    """style 只许写外观：外链 url()、表达式、@规则一律不要。"""
    text = value.lower()
    if "expression(" in text or "@" in text or "<" in text:
        return False
    return all(url.strip().strip("'\"").startswith("#")
               for url in re.findall(r"url\(([^)]*)\)", text))


def _render_attr(name: str, value: str, errors: list[str], *, root: bool = False) -> str | None:
    """单个属性 → 净化后的写法；None = 丢掉（并在 errors 里留话）。"""
    if name.lower().startswith("on"):
        errors.append(f"丢掉了事件属性 {name}——画面里不许有脚本")
        return None
    if name.lower().startswith("xmlns"):
        return f'{name.lower()}="{html.escape(value, quote=True)}"'
    local = _local(name)
    low = local.lower()
    if low.startswith("data-"):
        if low == ANIM_KIND_ATTR:
            kind = value.strip()
            if kind not in ANIM_KINDS:
                errors.append(f'data-anim="{kind}" 不在动效清单里'
                              f"（可用：{', '.join(ANIM_KINDS)}）")
                return None
            return f'{ANIM_KIND_ATTR}="{kind}"'
        if low in _ANIM_ATTRS:
            lo, hi = _ANIM_ATTRS[low]
            num = _num(value, low)
            if not lo <= num <= hi:
                errors.append(f"{low}={value} 超出 [{lo:g}, {hi:g}]")
                return None
            return f'{low}="{num:g}"'
        if low == NUM_UNIT_ATTR:
            unit = value.strip()
            if len(unit) > MAX_NUM_UNIT_CHARS:
                errors.append(f"{NUM_UNIT_ATTR}={unit!r} 超过 {MAX_NUM_UNIT_CHARS} 字——"
                              f"这是数量级后缀，写 万/亿/万亿 这一类")
                return None
            return f'{NUM_UNIT_ATTR}="{html.escape(unit, quote=True)}"' if unit else None
        errors.append(f"丢掉了未登记的属性 {low}——动画只用 data-anim / -at / -dur / "
                      f"-dist / -period / -count-to / -dp / -num-unit")
        return None
    if low == "href":
        target = value.strip()
        if not target.startswith("#"):
            errors.append(f"href 只能指本图内部的 #id，外链已丢掉：{target[:40]!r}")
            return None
        return f'href="{html.escape(target, quote=True)}"'
    if low not in ALLOWED_ATTRS:
        return None
    if len(value) > MAX_ATTR_CHARS:
        errors.append(f"属性 {low} 写了 {len(value)} 字，超过 {MAX_ATTR_CHARS}，已丢掉")
        return None
    if low == "style" and not _style_ok(value):
        errors.append("style 里出现 url() 外链或表达式，已整条丢掉")
        return None
    if root and low in ("width", "height"):
        return None            # 根 svg 的尺寸归画面带，写死就顶掉了自适应
    return f'{local}="{html.escape(value, quote=True)}"'


def _render_attrs(attrs: list[tuple[str, str]], errors: list[str], *, root: bool = False) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for name, value in attrs:
        item = _render_attr(name, value, errors, root=root)
        if item is None:
            continue
        key = _local(item.split("=", 1)[0]).lower()
        if key in seen:      # href 与 xlink:href 归一之后会撞名，写两遍就是非法 SVG
            continue
        seen.add(key)
        kept.append(item)
    return kept


def clean_svg(raw: Any) -> tuple[str, list[str]]:
    """模型写的 SVG → (可安全内联的 SVG, 退回原因清单)。

    清单非空即**不合格**：调用方（``spec.validate_spec``）带上镜号退给模型，改完重调。
    这里不猜意图、不替谁补画。
    """
    src = str(raw or "").strip()
    if not src:
        return "", ["visual.svg 是空的——自定义画面必须给一整段 <svg>"]
    if len(src) > MAX_SVG_CHARS:
        return "", [f"visual.svg 有 {len(src)} 字 > {MAX_SVG_CHARS}：一镜一张示意图，"
                    f"细节请用多个镜头拆开讲"]
    errors: list[str] = []
    try:
        tokens = _tokens(src)
        clean, errs = _clean_tokens(tokens, errors)
    except SvgError as e:
        return "", [f"visual.svg 读不懂：{e}"]
    # 同一个原因开闭标签各报一次是噪音，不是信息
    return clean, list(dict.fromkeys(errs))


def _clean_tokens(tokens: list[tuple[str, str, str]],
                  errors: list[str]) -> tuple[str, list[str]]:
    pieces: list[str] = []
    stack: list[tuple[str, bool]] = []     # (标签, 是否是被拆壳的未知容器)
    elements = 0
    root_done = False
    for kind, body, tag in tokens:
        if kind == "comment":
            continue
        if kind == "text":
            if not root_done:
                if body.strip():
                    errors.append("<svg> 之外写了正文，已丢掉——整段只能有一个 <svg>")
            elif stack:
                pieces.append(html.escape(html.unescape(body), quote=False))
            elif body.strip():
                errors.append("</svg> 之后还有正文，已丢掉——整段只能有一个 <svg>")
            continue
        blocked = _forbidden(tag)
        if blocked:
            errors.append(f"<{tag}>：{blocked}")
            continue
        if kind == "close":
            if not stack:
                errors.append(f"多了一个 </{tag}>")
                continue
            opened, unwrap = stack.pop()
            if opened != tag:
                errors.append(f"标签没配对：开了 <{opened}> 却关在 </{tag}>")
            elif not unwrap:
                pieces.append(f"</{tag}>")
            continue
        if tag not in ALLOWED_ELEMENTS:
            # 未知元素：拆壳留子。不在白名单就画不出东西，危险的是元素而不是内容。
            if kind == "open":
                stack.append((tag, True))
                errors.append(f"<{tag}> 不是可绘图的元素，已按容器展开（里面的内容照画）")
            continue
        if not root_done:
            if tag != "svg":
                return "", [f"根节点必须是 <svg>，这里是 <{tag}>"]
            attrs = _attrs(body)
            if not any(_local(k).lower() == "viewbox" for k, _ in attrs):
                return "", ["根 <svg> 缺 viewBox：横屏与竖屏共用一套坐标全靠它，"
                            '请补一个包住内容的方框，例如 viewBox="0 0 1000 560"']
            kept = _render_attrs(attrs, errors, root=True)
            if not any(k.lower().startswith("xmlns") for k in kept):
                kept.insert(0, f'xmlns="{SVG_NS}"')
            if not any(k.lower().startswith("preserveaspectratio") for k in kept):
                kept.append('preserveAspectRatio="xMidYMid meet"')
            pieces.append("<svg " + " ".join(kept) + ">")
            root_done = True
            if kind == "open":
                stack.append(("svg", False))
            else:
                pieces.append("</svg>")
            continue
        elements += 1
        if not stack:
            errors.append(f"</svg> 已经闭合，后面又写了 <{tag}>——整段只能有一个 <svg>")
            continue
        if elements > MAX_ELEMENTS:
            return "", [f"visual.svg 铺开到第 {elements} 个元素，超过上限 {MAX_ELEMENTS}："
                        f"重复形状请用 <defs> + <use>，别逐个写"]
        kept = _render_attrs(_attrs(body), errors)
        head = f"<{tag} " + " ".join(kept) if kept else f"<{tag}"
        if kind == "open":
            pieces.append(head + ">")
            stack.append((tag, False))
        else:
            pieces.append(head + "/>")
    if not root_done:
        return "", errors or ["visual.svg 里没有 <svg> 根节点"]
    if stack:
        errors.append(f"标签没有闭合：{', '.join(t for t, u in stack if not u)[:60]}")
    clean = "".join(pieces)
    if not errors and not any(f"<{t}" in clean for t in
                              ("path", "rect", "circle", "text", "line", "polyline",
                               "polygon", "use", "ellipse")):
        errors.append("visual.svg 净化之后没有任何可绘形状——这样一镜渲出来是一张空白纸")
    return clean, errors


def anim_fragments(art_html: str) -> list[dict[str, Any]]:
    """从编译好的画面 HTML 里读出所有 ``data-anim`` 的落点。

    读 HTML 而不是读 spec：内置图示卡的时值是 builder 自己排的（模型不必知道），
    自定义卡的时值是模型写的——两边最后都写进同一份 HTML，这里看的是**真正会被
    渲染的那份账**，免得 Python 以为动了而 JS 其实没动。
    """
    found: list[dict[str, Any]] = []
    for m in re.finditer(r'<([a-zA-Z][\w]*)\b[^>]*?\bdata-anim="([\w-]+)"([^>]*)>', art_html):
        kind = m.group(2)
        if kind not in ANIM_KINDS:
            continue
        blob = m.group(0)

        def _pick(attr: str, default: float) -> float:
            hit = re.search(rf'\bdata-{attr}="(-?[\d.]+)"', blob)
            return float(hit.group(1)) if hit else default
        found.append({"tag": m.group(1), "anim": kind,
                      "at_ms": _pick("at", DEFAULT_ANIM_AT_MS),
                      "dur_ms": _pick("dur", DEFAULT_ANIM_DUR_MS),
                      "period_ms": _pick("period", DEFAULT_PERIOD_MS)})
    return found


def anim_end_ms(art_html: str, duration_ms: float) -> float | None:
    """画面里所有声明式动画都停止变化的时刻（毫秒）；没有动画 → None。

    * 连续动画（``orbit``/``pulse``/``pan``）永不停止 → 返回 ``duration_ms``，
      整镜都当活帧截；否则会截出一个「动到一半冻住」的镜头；
    * 离散动画取 ``max(at + dur)``。
    """
    items = anim_fragments(art_html)
    if not items:
        return None
    if any(i["anim"] in CONTINUOUS_ANIMS for i in items):
        return duration_ms
    return max(min(i["at_ms"] + i["dur_ms"], duration_ms) for i in items)


def anim_table() -> str:
    """动效清单的一页对照表（单源在这里，别在散文里抄第二遍）。

    拼成**一行**是给 JSON Schema 的 description 用的：那里是多行文本框，
    每条一行会把「visual 字段说明」冲散。
    """
    return " / ".join(
        f"{k}＝{ANIM_LABELS[k]}"
        f"{'（不停，整镜都要截）' if k in CONTINUOUS_ANIMS else ''}"
        for k in ANIM_KINDS)
