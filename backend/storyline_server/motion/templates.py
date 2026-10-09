# -*- coding: utf-8 -*-
"""「档案卡」版式模板：把一镜的分镜数据编译成一页自播放的 HTML。

设计口径（照参考片实测还原）：
* **画面 100% 是排版**：纸质底 + 档案卡 + 角标 + 状态面板 + 手撕纸条字幕 + 红印章，
  没有任何实拍/生成式画面，所以每一帧都可确定性重放；
* **一帧 = 一个时刻的纯函数**：页面把 `?t=毫秒` 直接算成画面状态（解析期同步应用），
  **不靠 rAF 推进**。实测 headless Chrome 在 `--virtual-time-budget` 下只发得出 1~2 个
  rAF 回调就饿死，把动画挂在 rAF 上会让每个镜头都截成起始帧；rAF 循环只在真实浏览器
  里留着做预览；
* **不用随机数**：纸纹用固定 seed 的 feTurbulence，撕边用固定 clip-path。

坐标一律写在**设计空间**里，再用 `--u` 等比缩放到实际画布。设计空间有两套：

* 竖屏与方图共用 1080×1920（档案卡那套），方图只是四周多留纸；
* 横屏另有一套 1920×1080 的版式：状态面板与时间地点标签收到上沿，字幕变成一条
  居中的横排大字，中间留出整条**画面带**（1760×658）。档案系卡片是按竖构图画的，横屏时
  按自身像素落进这条带里（两侧留纸）；图示卡与自定义画面走 SVG viewBox 自适应，
  但**自适应只保证装得下，不保证铺得满**：内置图示的坐标是 1000×560（1.79:1），
  竖屏的带宽正好 1080 所以吃满宽度，横屏的带是 2.67:1，`meet` 缩放就被带高 658 卡住，
  实得 1175×658、两侧各留 292 纸。要在横屏铺满带宽，只能自画宽幅 viewBox
  （``card=custom``，例如 ``viewBox="0 0 1600 600"``）。
"""

from __future__ import annotations

import html
import json
from typing import Any

from . import art as _art
from . import diagrams as _dg

DESIGN_W = 1080
DESIGN_H = 1920
WIDE_W = 1920           # 横屏第二套版式的设计空间
WIDE_H = 1080
ENTER_MS = 420          # 卡片入场时长
TEAR_MS = 260           # 字幕条撕开时长

#: 用 SVG viewBox 自适应画面带的卡型（图示卡 + 自定义画面），其余按竖构图缩放。
FILL_KINDS = tuple(_dg.BUILDERS) + ("custom",)

#: 纸纹的六道固定坐标（竖屏设计空间里的原值）。固定 seed、固定位置——不用随机数。
_SCRATCH_SEEDS = ((120, 260, 60, 40), (820, 420, 70, 30), (260, 1500, 50, 60),
                  (760, 1660, 66, 34), (520, 880, 40, 52), (900, 1180, 58, 26))

SERIF = ('"Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",'
         '"Microsoft YaHei",serif')
MONO = '"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace'


# ---------------------------------------------------------------------------
# 词 → 文案字符区间：TTS 给的是词序列，字幕要按字排，两边必须对齐一次
# ---------------------------------------------------------------------------

def align_words(text: str, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 edge-tts 的词时间轴贴到文案的字符区间上。

    TTS 的分词是带语义的（「于是/欲望/第一次/获得」而非逐字），而字幕按整句排版，
    所以要按**出现顺序**在文案里逐个定位。落不进去的词（TTS 改读或多读）跳过，
    标点与未覆盖的字符并入前一个词——宁可让高亮稍微提前，也不让字幕出现
    「永远亮不起来」的空洞。
    """
    out: list[dict[str, Any]] = []
    pos = 0
    for w in words:
        token = str(w.get("text") or "").strip()
        if not token:
            continue
        idx = text.find(token, pos)
        if idx < 0:
            idx = text.find(token)          # 文案里换个位置出现也算数
        if idx < 0:
            continue
        start = float(w.get("start_ms") or 0.0)
        out.append({"token": token,
                    "char_start": idx,
                    "char_end": idx + len(token),
                    "start_ms": round(start, 1),
                    "end_ms": round(start + float(w.get("duration_ms") or 0.0), 1)})
        pos = idx + len(token)
    # 未覆盖的尾部字符并入最后一个词，保证整句都被时间轴盖住
    if out:
        out[-1]["char_end"] = max(out[-1]["char_end"], len(text))
        prev_end = 0
        for item in out:
            item["char_start"] = max(prev_end, item["char_start"])
            prev_end = item["char_end"]
    return out


def _is_highlight(token: str, terms: list[str]) -> bool:
    for term in terms:
        t = term.strip()
        if t and (t in token or token in t):
            return True
    return False


# ---------------------------------------------------------------------------
# 版式组件（每个 card kind 一段 SVG/HTML，全部在 1080×1920 设计空间里）
# ---------------------------------------------------------------------------

def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _art_plain(v: dict[str, Any]) -> str:
    return '<div class="art"></div>'


def _art_archive(v: dict[str, Any]) -> str:
    """档案卡：红框 + 米色内页 + 挂签（编号/出处）。"""
    inv = _esc(v.get("inv") or "INV. 00000")
    line1 = _esc(v.get("line1") or "")
    line2 = _esc(v.get("line2") or "")
    line3 = _esc(v.get("line3") or "")
    return f'''<div class="art">
  <div class="card archive">
    <div class="frame"></div>
    <div class="inner"></div>
    <div class="tag">
      <div class="tag-hole"></div>
      <div class="inv">{inv}</div>
      <div class="meta">{line1}</div>
      <div class="meta">{line2}</div>
      <div class="meta dim">{line3}</div>
    </div>
  </div></div>'''


def _art_dict_entry(v: dict[str, Any]) -> str:
    word = _esc(v.get("word") or "—")
    note = _esc(v.get("note") or "")
    return f'''<div class="art">
  <div class="card dict">
    <div class="dict-note">{note}</div>
    <div class="dict-word">{word}</div>
    <div class="dict-rule"></div>
  </div></div>'''


_DARK_KINDS = ("theatre", "silhouette")


def _art_stamp(v: dict[str, Any]) -> str:
    """印章。anchor="over" = 作为叠加层盖在别的版式上，落右下角（居中会压住主文字）。"""
    zh = _esc(v.get("zh") or "尚未存在")
    en = _esc(v.get("en") or "NOT YET INVENTED")
    cls = "stamp" + (" over" if v.get("anchor") == "over" else "") \
          + (" on-dark" if v.get("dark") else "")
    return f'''<div class="art">
  <div class="{cls}" data-at="{float(v.get('at_ms') or 0):.0f}">
    <div class="stamp-box"><div class="stamp-zh">{zh}</div>
    <div class="stamp-en">{en}</div></div>
  </div></div>'''


def _art_book(v: dict[str, Any]) -> str:
    lines = int(v.get("lines") or 9)
    rows = "".join(
        f'<rect x="{38 + (i % 2) * 8}" y="{70 + i * 26}" width="{196 - (i % 3) * 22}" height="7" rx="3"/>'
        for i in range(lines))
    return f'''<div class="art">
  <div class="book">
    <svg viewBox="0 0 520 340" class="svg">
      <g class="page">
        <path d="M10 40 L250 20 L250 300 L10 320 Z" fill="#f7f1e3" stroke="#2b2b2b" stroke-width="2"/>
        <path d="M270 20 L510 40 L510 320 L270 300 Z" fill="#f7f1e3" stroke="#2b2b2b" stroke-width="2"/>
        <g fill="#c9c2b2">{rows}</g>
        <g transform="translate(288,70)" fill="#c9c2b2">{rows}</g>
      </g>
    </svg>
  </div></div>'''


def _art_print(v: dict[str, Any]) -> str:
    """版画：上方棕色「版」+ 下方叠放的蓝红条纹印张。"""
    title = _esc(v.get("title") or "版")
    return f'''<div class="art">
  <div class="print">
    <div class="block"><span>{title}</span></div>
    <div class="sheet s1"></div><div class="sheet s2"></div>
    <div class="sheet s3"><i></i><i></i></div>
  </div></div>'''


def _art_theatre(v: dict[str, Any]) -> str:
    seats = "".join(
        f'<path d="M{40 + i * 62} {250 + (i % 2) * 16} q31 -34 62 0" />'
        for i in range(8))
    return f'''<div class="art">
  <div class="theatre">
    <div class="screen"></div>
    <svg viewBox="0 0 560 300" class="svg seats">{seats}</svg>
  </div></div>'''


def _art_webpage(v: dict[str, Any]) -> str:
    """早期网页：地址栏 + 18+ 缩略图网格 + 收入柱状图。"""
    bars = v.get("bars") or [18, 30, 46, 62, 78]
    year = _esc(v.get("year") or "1995")
    cols = "".join(
        f'<div class="cell"><span>18+</span></div>' for _ in range(6))
    chart = "".join(
        f'<i style="height:{max(6, min(100, int(b)))}%"></i>' for b in bars[:6])
    return f'''<div class="art">
  <div class="web">
    <div class="bar">http://www. — {year}</div>
    <div class="web-body">
      <div class="grid">{cols}</div>
      <div class="chart"><div class="chart-t">$ REVENUE</div><div class="cols">{chart}</div></div>
    </div>
  </div></div>'''


def _art_silhouette(v: dict[str, Any]) -> str:
    return '''<div class="art">
  <div class="night">
    <div class="glow"></div>
    <svg viewBox="0 0 420 520" class="svg fig">
      <circle cx="196" cy="120" r="52" fill="#0b0d12"/>
      <path d="M150 176 q46 -18 92 0 l34 210 q-80 26 -160 0 Z" fill="#0b0d12"/>
      <rect x="236" y="252" width="52" height="88" rx="9" fill="#9fc0ff" class="phone"/>
      <path d="M242 262 q20 -8 40 0 l0 66 q-20 8 -40 0 Z" fill="#e8f1ff"/>
    </svg>
  </div></div>'''


def _art_title(v: dict[str, Any]) -> str:
    zh = _esc(v.get("zh") or "")
    en = _esc(v.get("en") or "")
    over = _esc(v.get("over_zh") or "")
    over_en = _esc(v.get("over_en") or "")
    return f'''<div class="art">
  <div class="cover">
    <div class="cover-in">
      <div class="cover-zh">{zh}</div>
      <div class="cover-en">{en}</div>
    </div>
    <div class="stamp cover-stamp" data-at="{float(v.get('at_ms') or 1200):.0f}">
      <div class="stamp-box"><div class="stamp-zh">{over}</div>
      <div class="stamp-en">{over_en}</div></div>
    </div>
  </div></div>'''


def _tag_dg(svg: str) -> str:
    """给自画的根 ``<svg>`` 挂上 ``dg`` 类。

    图示与自画共用同一套设计空间，也就共用同一套字级样式（``.dg .h`` / ``.t`` / ``.s``），
    而它们全挂在 ``.dg`` 下。模型写的根标签没这个类时，图题会退回浏览器默认字号、
    在 1000×560 的坐标里小成一粒——补类比补内联字号便宜，且只碰根标签。
    """
    end = svg.find(">")
    if not svg.startswith("<svg") or end < 0:
        return svg
    head, rest = svg[:end], svg[end:]
    if "class=" in head:
        return head.replace('class="', 'class="dg ', 1) + rest
    if head.endswith("/"):
        head = head[:-1]
        return f'{head} class="dg"/{rest[1:]}'
    return f'{head} class="dg">{rest[1:]}'


def _art_custom(v: dict[str, Any]) -> str:
    """模型自画的示意图。SVG 在这里**再过一次**净化器：

    ``spec.validate_spec`` 入库前已经净化过，但出片页只认经过这两道的字节——
    谁将来多开一条路径（手写 spec、别的调用方）也不会漏掉闸门。净化是幂等的，
    第二次运行既不会改动画面也不会重复报错。
    """
    clean, errors = _art.clean_svg(v.get("svg") or "")
    if errors or not clean:
        # 走到这里说明 spec 那关没拦住；宁渲染一张空白纸，也不内联未经净化的字节。
        return '<div class="empty-art">（这一镜的自定义画面没有可用图形）</div>'
    return _tag_dg(clean)


def _art_diagram(kind: str) -> "Any":
    def build(v: dict[str, Any]) -> str:
        return _dg.diagram_svg(kind, v) or ""
    return build


ART_BUILDERS = {
    "plain": _art_plain,
    "archive": _art_archive,
    "dict_entry": _art_dict_entry,
    "stamp": _art_stamp,
    "book": _art_book,
    "print": _art_print,
    "theatre": _art_theatre,
    "webpage": _art_webpage,
    "silhouette": _art_silhouette,
    "title": _art_title,
    "custom": _art_custom,
    **{kind: _art_diagram(kind) for kind in _dg.BUILDERS},
}

#: 每张卡的 visual **实际读**哪些顶层键（与 ART_BUILDERS 同处维护）。
#: 档案系照 ``_art_*`` 的取值点逐条核对，图示系从 diagrams 接过来——
#: plain/theatre/silhouette 三张纯图形卡不读任何键，写进 visual 的都是死键。
#: ``spec.validate_spec`` 拿它拦「模型以为写了、画面却什么都没有」那一类。
ART_VISUAL_KEYS: dict[str, tuple[str, ...]] = {
    "plain": (),
    "archive": ("inv", "line1", "line2", "line3"),
    "dict_entry": ("word", "note"),
    "stamp": ("zh", "en", "anchor", "dark", "at_ms"),
    "book": ("lines",),
    "print": ("title",),
    "theatre": (),
    "webpage": ("bars", "year"),
    "silhouette": (),
    "title": ("zh", "en", "over_zh", "over_en", "at_ms"),
    "custom": ("svg",),
    **_dg.DIAGRAM_VISUAL_KEYS,
}


def compile_art(shot: dict[str, Any], duration_sec: float) -> tuple[str, str]:
    """一镜的版式 → (画面 HTML, fit)。fit：

    * ``box``＝档案系卡片，按自身像素尺寸落在画面带里（横屏时画面带本身就是容器，两侧留纸）；
    * ``fill``＝SVG viewBox 自适应画面带（图示卡与自定义画面）——装得下，但横屏按带高缩放，
      1000×560 那套坐标铺不满 1760 的带宽。

    这里**只算一次**：``build_shot_page`` 用它排版，``settle_ms`` 用它的 HTML 扫
    ``data-anim`` 的落点。两处各编译一遍就会错开一帧。
    """
    kind = str(shot.get("card") or "plain")
    builder = ART_BUILDERS.get(kind, _art_archive)
    visual = dict(shot.get("visual") or {})
    stamp = shot.get("stamp") or {}
    at = stamp_at_ms(shot, duration_sec)
    if kind in ("stamp", "title"):
        visual["at_ms"] = at
        return builder(visual), "box"
    inner = builder(visual)
    if stamp and at is not None:
        inner += _art_stamp({"zh": stamp.get("zh"), "en": stamp.get("en"),
                             "at_ms": at, "anchor": "over",
                             "dark": kind in _DARK_KINDS})
    return inner, ("fill" if kind in FILL_KINDS else "box")


# ---------------------------------------------------------------------------
# 样式表（一次注入，所有镜头共用）
# ---------------------------------------------------------------------------

_CSS = f'''
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:100%; height:100%; overflow:hidden; background:#d8cdb6; }}
body {{ font-family:{SERIF}; color:#23211c; }}
/* 设计空间 1080x1920 等比缩放；横屏时四周是同一张纸，不做第二套版式 */
.stage {{ position:absolute; left:50%; top:50%; width:{DESIGN_W}px; height:{DESIGN_H}px;
         transform:translate(-50%,-50%) scale(var(--u)); transform-origin:center; }}
.bg {{ position:absolute; inset:0;
      background:
        radial-gradient(120% 80% at 50% 0%, rgba(255,255,255,.30), transparent 60%),
        radial-gradient(140% 90% at 50% 100%, rgba(90,72,44,.22), transparent 62%),
        linear-gradient(160deg,#e9ddc4 0%,#ded0b2 45%,#cdbb97 100%); }}
.bg svg {{ position:absolute; inset:0; width:100%; height:100%; opacity:.5; }}
.scratches {{ position:absolute; inset:0; opacity:.30; }}
.scratches path {{ stroke:#8b7a5c; stroke-width:1.4; fill:none; }}

/* 左上状态面板：整片唯一的"进度条"，靠跳变叙事 */
.panel {{ position:absolute; left:44px; top:112px; display:flex; flex-direction:column; gap:8px; }}
.panel .row {{ background:#f6f1e4; border:1.5px solid #23211c; padding:7px 14px 9px;
              box-shadow:3px 4px 0 rgba(40,32,20,.16); }}
.panel .k {{ font-family:{MONO}; font-size:15px; letter-spacing:.06em; color:#6c6455; }}
.panel .v {{ font-size:31px; font-weight:700; letter-spacing:.02em; }}

/* 右上时间地点标签 */
.label {{ position:absolute; right:44px; top:120px; background:#f6f1e4;
         border:1.5px solid #23211c; padding:9px 18px; font-size:26px; letter-spacing:.05em;
         box-shadow:3px 4px 0 rgba(40,32,20,.16); }}

/* 中央版式区：artWrap 是画面带，里面按卡型二选一 */
.art {{ position:absolute; left:0; top:0; width:100%; height:100%; }}
.svg {{ display:block; }}
.card {{ position:absolute; left:50%; top:44%; transform:translate(-50%,-50%); }}
/* box＝竖构图容器（原样占满画面带，横屏时整体缩放）；fill＝viewBox 自适应 */
.artBox {{ position:absolute; inset:0; }}
.artFill {{ position:absolute; inset:0; }}
.artFill svg {{ width:100%; height:100%; display:block; }}
.empty-art {{ position:absolute; inset:0; display:flex; align-items:center;
             justify-content:center; font-size:30px; color:#8b8371; }}

.card.archive {{ width:640px; height:520px; }}
.card.archive .frame {{ position:absolute; inset:0; background:#a8352c; padding:34px; }}
.card.archive .inner {{ position:absolute; inset:34px; background:#d9b98c; }}
.card.archive .tag {{ position:absolute; left:96px; top:120px; width:430px;
                     background:#f3ecd9; padding:26px 30px 30px 74px;
                     box-shadow:6px 8px 0 rgba(40,30,16,.22); transform:rotate(-1.4deg); }}
.card.archive .tag-hole {{ position:absolute; left:26px; top:34px; width:26px; height:26px;
                          border:3px solid #6d6353; border-radius:50%; }}
.card.archive .inv {{ font-family:{MONO}; font-size:38px; font-weight:700; letter-spacing:.02em; }}
.card.archive .meta {{ font-family:{MONO}; font-size:22px; color:#4a4438; margin-top:10px; }}
.card.archive .meta.dim {{ color:#8b8371; }}

.card.dict {{ width:660px; background:#f7f2e5; padding:34px 40px 44px;
             box-shadow:7px 9px 0 rgba(40,30,16,.20); transform:translate(-50%,-50%) rotate(-.8deg); }}
.dict-note {{ font-family:{MONO}; font-size:22px; color:#7a7263; letter-spacing:.08em; }}
.dict-word {{ font-family:{MONO}; font-size:74px; font-weight:700; margin:14px 0 12px; }}
.dict-rule {{ height:3px; background:#23211c; opacity:.8; }}

.stamp {{ position:absolute; left:50%; top:46%; transform:translate(-50%,-50%) rotate(-9deg);
        width:max-content; }}
.stamp-box {{ border:6px solid #b3271d; color:#b3271d; padding:16px 26px; text-align:center;
             opacity:.86; mix-blend-mode:multiply; }}
.stamp-zh {{ font-size:62px; font-weight:800; letter-spacing:.08em; line-height:1.05; }}
.stamp-en {{ font-family:{MONO}; font-size:22px; letter-spacing:.14em; margin-top:6px; }}
/* 叠加印章：压卡的右下角，绝不居中——居中实测会把词头/主标题糊成一团 */
.stamp.over {{ left:73%; top:50%; }}
.stamp.over .stamp-zh {{ font-size:50px; }}
.stamp.over .stamp-box {{ border-width:5px; padding:12px 20px; }}
.stamp.on-dark .stamp-box {{ mix-blend-mode:normal; color:#d8483c; border-color:#d8483c; }}

.book {{ position:absolute; left:50%; top:46%; transform:translate(-50%,-50%); width:640px; }}
.book .svg {{ width:100%; }}
.book .page path {{ stroke:#23211c; }}

.print {{ position:absolute; left:50%; top:44%; transform:translate(-50%,-50%); width:600px; height:640px; }}
.print .block {{ position:absolute; left:60px; top:0; width:480px; height:250px;
                background:#8a5a34; box-shadow:0 10px 0 rgba(40,30,16,.25); }}
.print .block span {{ position:absolute; right:34px; top:26px; font-size:52px; color:#c99a6b; opacity:.85; }}
.print .sheet {{ position:absolute; width:400px; height:300px; background:#f6f0e0;
                border:2px solid #23211c; }}
.print .s1 {{ left:0; top:250px; transform:rotate(-4deg); }}
.print .s2 {{ right:0; top:272px; transform:rotate(3deg); }}
.print .s3 {{ left:100px; top:300px; display:flex; gap:26px; justify-content:center; padding-top:34px; }}
.print .s3 i {{ width:74px; height:210px; background:#2f4f8f; border-radius:37px 37px 4px 4px; }}
.print .s3 i + i {{ background:#a8352c; }}

.theatre {{ position:absolute; inset:0; background:#07070a; }}
.theatre .screen {{ position:absolute; left:50%; top:20%; transform:translate(-50%,0);
                   width:760px; height:420px; background:#e9e6df;
                   box-shadow:0 0 120px 30px rgba(233,230,223,.20); }}
.theatre .seats {{ position:absolute; left:50%; bottom:16%; transform:translate(-50%,0); width:860px; }}
.theatre .seats path {{ stroke:#3a3a42; stroke-width:5; fill:none; }}

.web {{ position:absolute; left:50%; top:44%; transform:translate(-50%,-50%); width:700px;
       background:#c9c6c0; border:2px solid #23211c; box-shadow:8px 10px 0 rgba(40,30,16,.20); }}
.web .bar {{ background:#2f4f8f; color:#fff; font-family:{MONO}; font-size:24px; padding:12px 18px; }}
.web-body {{ display:flex; gap:18px; padding:20px; background:#e8e6e1; }}
.web .grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; width:250px; }}
.web .cell {{ width:112px; height:112px; border-radius:50%; background:#23211c; color:#fff;
             display:flex; align-items:center; justify-content:center;
             font-family:{MONO}; font-size:26px; }}
.web .chart {{ flex:1; background:#f7f5f1; border:1.5px solid #8d8a84; padding:14px 18px; }}
.web .chart-t {{ font-family:{MONO}; font-size:22px; }}
.web .cols {{ display:flex; align-items:flex-end; gap:14px; height:210px; margin-top:14px; }}
.web .cols i {{ flex:1; background:#2f4f8f; display:block; }}

.night {{ position:absolute; inset:0; background:#06070b; }}
.night .glow {{ position:absolute; left:52%; top:44%; width:640px; height:640px; transform:translate(-50%,-50%);
               background:radial-gradient(circle, rgba(150,185,255,.30), transparent 62%); }}
.night .fig {{ position:absolute; left:50%; top:46%; transform:translate(-50%,-50%); height:820px; }}
.night .phone {{ filter:drop-shadow(0 0 26px rgba(190,215,255,.85)); }}

.cover {{ position:absolute; left:50%; top:42%; transform:translate(-50%,-50%);
         width:560px; height:400px; background:#191713; border:2px solid #000;
         box-shadow:10px 12px 0 rgba(40,30,16,.28); }}
.cover-in {{ position:absolute; inset:26px; border:1.5px solid #8a7a4e;
            display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; }}
.cover-zh {{ font-size:74px; font-weight:800; color:#e7e2d6; letter-spacing:.06em; }}
.cover-en {{ font-family:{MONO}; font-size:22px; color:#b9a876; letter-spacing:.18em; }}
.cover-stamp {{ left:76%; top:80%; }}
.cover-stamp .stamp-zh {{ font-size:44px; }}
.cover-stamp .stamp-box {{ mix-blend-mode:normal; border-width:5px; padding:10px 18px;
                          color:#d8483c; border-color:#d8483c; }}

/* 底部手撕纸条字幕 */
.caption {{ position:absolute; left:50%; bottom:150px; transform:translate(-50%,0);
           width:840px; }}
.strip {{ position:relative; background:#f7f2e2; padding:30px 40px 34px;
         clip-path:polygon(0% 12%, 3% 2%, 9% 9%, 16% 1%, 24% 8%, 33% 2%, 43% 9%, 53% 1%,
                           63% 8%, 73% 2%, 83% 9%, 92% 3%, 100% 11%,
                           100% 88%, 93% 98%, 84% 91%, 74% 99%, 63% 92%, 52% 99%,
                           41% 91%, 31% 98%, 21% 92%, 11% 99%, 3% 92%, 0% 90%);
         box-shadow:0 10px 24px rgba(40,30,16,.20); }}
.strip p {{ font-size:44px; line-height:1.52; letter-spacing:.01em; }}
.strip .w {{ position:relative; z-index:1; opacity:.34; }}
.strip .w.on {{ opacity:1; }}
.strip .hl::after {{ content:""; position:absolute; left:-2px; right:-2px; bottom:2px;
                    height:.44em; background:#f0b7bd; z-index:-1; opacity:0;
                    transition:none; }}
.strip .hl.on::after {{ opacity:1; }}
.tape {{ position:absolute; left:-26px; top:-16px; width:96px; height:40px;
        background:rgba(226,214,180,.85); transform:rotate(-24deg);
        box-shadow:0 2px 6px rgba(0,0,0,.14); }}
'''

#: 图示卡的字号与配色单源在 diagrams.py（组件和它的样式漂了就画歪）
_DIAGRAM_CSS = _dg.DIAGRAM_CSS

#: 横屏第二套版式：**只覆盖定位**——纸质、颜色、印章、撕边全部沿用 _CSS。
#: 设计空间 1920×1080，上沿 172（状态面板两行要占到底）、下沿 250（那条居中大字幕），
#: 中间 1760×658 整条留给画面带。
_CSS_WIDE = f'''
/* 先换设计空间本身：下面每一条都按 1920×1080 排，.stage 不跟着换就等于
   把横屏版式塞进竖屏的盒子里——实测字幕直接掉到可视区外。 */
.stage.wide {{ width:{WIDE_W}px; height:{WIDE_H}px; }}
.stage.wide .panel {{ left:40px; top:44px; gap:6px; }}
.stage.wide .panel .row {{ padding:5px 12px 7px; }}
.stage.wide .panel .k {{ font-size:14px; }}
.stage.wide .panel .v {{ font-size:25px; }}
.stage.wide .label {{ right:40px; top:48px; font-size:21px; padding:6px 14px; }}
.stage.wide #artWrap {{ left:80px; top:172px; right:80px; bottom:250px;
                       width:auto; height:auto; }}
/* 档案系卡按**自身像素尺寸**落进 1760×658 的画面带。
   原来这里是把整张 1080×1920 的竖构图按高度缩成 .35 倍——实测那张 640×520 的封面卡
   在 1920 宽的纸上只剩 224px，等于一枚邮票贴在墙上。竖构图里真正画卡的只有中间一条，
   所以横屏要换的是定位容器，不是比例。 */
.stage.wide .artBox {{ position:absolute; inset:0; }}
.stage.wide .card, .stage.wide .book, .stage.wide .print,
.stage.wide .web, .stage.wide .cover {{ top:50%; }}
.stage.wide .night .fig {{ height:600px; }}
.stage.wide .caption {{ width:1500px; bottom:66px; }}
.stage.wide .strip {{ padding:16px 46px 22px; }}
.stage.wide .strip p {{ font-size:44px; line-height:1.34; text-align:center; }}
.stage.wide .tape {{ left:-22px; top:-14px; width:84px; height:34px; }}
'''

_JS = r'''
const S = window.__SHOT__;
const ENTER = S.enter_ms, TEAR = S.tear_ms;
const clamp = (v,a,b)=>Math.max(a,Math.min(b,v));
const ease = t => 1 - Math.pow(1 - t, 3);
const art = document.getElementById('artWrap');
const cap = document.getElementById('caption');
const panel = document.getElementById('panel');
const label = document.getElementById('label');
const stampEl = document.querySelector('.stamp');
const words = Array.from(document.querySelectorAll('.strip .w'));
const stampAt = stampEl ? Number(stampEl.dataset.at || 0) : 0;

function fit(){
  // 缩放基准是**设计空间**（竖屏 1080x1920 / 横屏 1920x1080），不是成片像素：
  // 两者只在竖屏恰好相等，横屏时像素带宽与设计带宽是两套坐标。
  const u = Math.min(innerWidth/S.dw, innerHeight/S.dh);
  document.documentElement.style.setProperty('--u', u);
}
addEventListener('resize', fit); fit();

// 声明式动效：data-anim 的每种都在这里有对应一段**纯 t 的函数**。
// 解析期把参数一次读完并量好路径长度（getTotalLength 是排版调用，放每帧会拖慢截图），
// 之后不再读 DOM、不再累积状态——同一 t 永远得到同一帧。
const NUM = (el, key, def) => {
  const raw = el.getAttribute('data-' + key);
  if (raw === null || raw === '') return def;
  const v = Number(raw);
  return Number.isFinite(v) ? v : def;
};
const ANIMS = Array.from(document.querySelectorAll('[data-anim]')).map(el => {
  const kind = el.getAttribute('data-anim');
  const a = {el, kind, at: NUM(el, 'at', 0), dur: Math.max(1, NUM(el, 'dur', 600)),
             dist: NUM(el, 'dist', kind === 'wipe' ? 100 : 40),
             to: NUM(el, 'count-to', 0), dp: NUM(el, 'dp', 0),
             unit: el.getAttribute('data-num-unit') || '',
             period: Math.max(200, NUM(el, 'period', 2400))};
  if (kind === 'draw'){
    a.len = (el.getTotalLength ? el.getTotalLength() : 0) || 0;
    el.style.strokeDasharray = a.len;
  }
  if (kind === 'grow-x') el.style.transformOrigin = '0% 50%';
  if (kind === 'grow-y') el.style.transformOrigin = '50% 100%';
  if (kind === 'pop')    el.style.transformOrigin = '50% 50%';
  if (kind !== 'fade' && kind !== 'count' && kind !== 'draw' && kind !== 'wipe')
    el.style.transformBox = 'fill-box';
  el.style.opacity = '0';
  return a;
});

function before(a){                      // 还没轮到：藏好，并摆出「起始姿态」
  const el = a.el;
  el.style.opacity = '0';
  switch (a.kind){
    case 'rise': el.style.transform = `translateY(${a.dist}px)`; break;
    case 'slide-x': el.style.transform = `translateX(${-a.dist}px)`; break;
    case 'pop': el.style.transform = 'scale(.55)'; break;
    case 'grow-x': el.style.transform = 'scaleX(0)'; break;
    case 'grow-y': el.style.transform = 'scaleY(0)'; break;
    case 'draw': el.style.strokeDashoffset = a.len; break;
    case 'wipe': el.style.clipPath = 'inset(0 100% 0 0)'; break;
    case 'count': el.textContent = (0).toFixed(a.dp) + a.unit; break;
  }
}

function paint(a, t){
  const el = a.el, k = a.kind;
  if (k === 'orbit' || k === 'pulse' || k === 'pan'){      // 连续：永远不停，整镜都要截
    const ph = (((t - a.at) / a.period) % 1 + 1) % 1;
    if (k === 'pulse'){ el.style.opacity = (0.35 + 0.6 * (0.5 + 0.5 * Math.sin(ph * 6.2832))).toFixed(3); return; }
    el.style.opacity = t >= a.at ? '1' : '0';
    if (k === 'orbit'){
      const ang = ph * 6.2832;
      el.style.transform = `translate(${(Math.cos(ang) * a.dist).toFixed(2)}px,${(Math.sin(ang) * a.dist).toFixed(2)}px)`;
    } else {
      el.style.transform = `translateX(${(-ph * a.dist).toFixed(2)}px)`;
    }
    return;
  }
  if (t < a.at){ before(a); return; }
  const p = clamp((t - a.at) / a.dur, 0, 1), e = ease(p);
  switch (k){
    case 'fade': el.style.opacity = e.toFixed(3); break;
    case 'rise': el.style.opacity = e.toFixed(3);
      el.style.transform = `translateY(${((1 - e) * a.dist).toFixed(2)}px)`; break;
    case 'slide-x': el.style.opacity = e.toFixed(3);
      el.style.transform = `translateX(${(-(1 - e) * a.dist).toFixed(2)}px)`; break;
    case 'pop': el.style.opacity = Math.min(1, p * 3).toFixed(3);
      el.style.transform = `scale(${(0.55 + 0.45 * e).toFixed(3)})`; break;
    case 'grow-x': el.style.opacity = '1'; el.style.transform = `scaleX(${e.toFixed(3)})`; break;
    case 'grow-y': el.style.opacity = '1'; el.style.transform = `scaleY(${e.toFixed(3)})`; break;
    case 'draw': el.style.opacity = '1';
      el.style.strokeDashoffset = ((1 - e) * a.len).toFixed(2); break;
    case 'wipe': el.style.opacity = '1';
      el.style.clipPath = `inset(0 ${((1 - e) * a.dist).toFixed(2)}% 0 0)`; break;
    case 'count': el.style.opacity = p > 0 ? '1' : '0';
      el.textContent = (a.to * e).toFixed(a.dp) + a.unit; break;
  }
}

// 时间 → 画面：全部状态都由 t 一次算完，不累积、不依赖上一帧。
function apply(t){
  t = Math.max(0, t);
  const a = ease(clamp(t/ENTER,0,1));            // 卡片入场：从上方落下并回正
  art.style.opacity = a.toFixed(3);
  art.style.transform = `translateY(${((1-a)*-64).toFixed(2)}px) rotate(${((1-a)*-2.2).toFixed(2)}deg)`;
  for (const el of [panel,label]){
    if(!el) continue;
    el.style.opacity = a.toFixed(3);
    el.style.transform = `translateX(${((1-a)*-26).toFixed(2)}px)`;
  }
  const c = ease(clamp((t-120)/TEAR,0,1));       // 字幕条撕开
  cap.style.opacity = c.toFixed(3);
  cap.style.transform = `translate(-50%,${((1-c)*36).toFixed(2)}px) scaleY(${(0.86+0.14*c).toFixed(3)})`;
  for (const w of words){                        // 逐词点亮（高亮词同时刷粉底）
    w.classList.toggle('on', t >= Number(w.dataset.at));
  }
  if (stampEl){                                  // 印章落下：从大到小砸下
    const s = ease(clamp((t-stampAt)/220,0,1));
    stampEl.style.opacity = s.toFixed(3);
    stampEl.firstElementChild.style.transform = `scale(${(1.9-0.9*s).toFixed(3)})`;
  }
  for (const an of ANIMS) paint(an, t);          // 版式内声明的动效
}
// 出片路径：?t= 直接钉死这一帧的时刻。headless Chrome 在 --virtual-time-budget 下
// 只发 1~2 个 rAF 回调就饿死，动画循环因此**不能**是出片依赖（实测 frames=1）。
// ?probe=1 是命中表探针（见 _PROBE_JS）：它必须关掉 rAF 循环，否则循环会在探针
// 量完框之后再 apply 一次别的时刻，把入场位移盖回未完成态——量到的框就偏了。
const _q = new URLSearchParams(location.search);
const _t = _q.get('t');
if (_t === null && _q.get('probe') !== '1'){
  (function loop(){ apply(performance.now()); requestAnimationFrame(loop); })();
}
else apply(_t === null ? S.settle_ms : (Number(_t) || 0));
'''

_PROBE_JS = r'''
/* 命中表探针：只在 ?probe=1 时跑，出片路径一行都不执行。
 *
 * 它不改画面，只在文档末尾追加一个隐藏的 <pre>，把「每个可见元素的框 + 它显示的字
 * + 它的计算样式」倒出来，由渲染层用 --dump-dom 回读。框一律归一化到 .stage（0~1），
 * 于是前端叠在播放器上的那一层与成片像素尺寸、与设计空间、与 --u 缩放全都无关。
 *
 * 角色（role）按 CSS 类与「有没有直接文本节点」判，**不靠版式构建器配合**——11 种卡型
 * 加内置图示加模型自画 SVG，只要它们还沿用这套类名，探针就还认得。
 */
(function(){
  var out = [], seq = {};
  function emit(pre, payload){
    var node = document.createElement('pre');
    node.id = pre === 'HITMAP::' ? '__hitmap__' : '__hitmap_err__';
    node.style.display = 'none';
    node.textContent = pre + payload;
    document.body.appendChild(node);
  }
  try {
    var q = new URLSearchParams(location.search);
    if (q.get('probe') !== '1') return;
    /* 时刻必须和代表帧同一口径。这里不能写 Number(q.get('t'))：
       没带 t 时它返回 null，而 Number(null) === 0 —— 于是探针量的是
       入场动画起点（各元素还带着 translateY），框与落墨能差几十像素。*/
    var raw = q.get('t');
    var t = raw === null ? S.settle_ms : Number(raw);
    if (!isFinite(t)) t = S.settle_ms;
    apply(t);
    var sr = document.getElementById('stage').getBoundingClientRect();
    if (!sr.width || !sr.height) { emit('HITMAP_ERR::', 'stage 没有尺寸'); return; }
    function box(el){
      var r = el.getBoundingClientRect();
      return {x: +((r.left - sr.left) / sr.width).toFixed(4),
              y: +((r.top - sr.top) / sr.height).toFixed(4),
              w: +(r.width / sr.width).toFixed(4),
              h: +(r.height / sr.height).toFixed(4)};
    }
    function visible(el){
      var cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      if (Number(cs.opacity) < 0.06) return false;
      var r = el.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    }
    function ownText(el){
      var s = '';
      for (var n = el.firstChild; n; n = n.nextSibling) if (n.nodeType === 3) s += n.nodeValue;
      return s.replace(/\s+/g, ' ').trim();
    }
    function push(role, el, extra){
      if (!el || !visible(el)) return;
      var n = seq[role] || 0;
      var e = {id: (S.id || 'shot') + '.' + role + '.' + n, role: role, seq: n,
               text: (ownText(el) || (el.textContent || '')).replace(/\s+/g, ' ').trim()
                       .slice(0, 160),
               box: box(el)};
      var cs = getComputedStyle(el);
      e.style = {font_size: cs.fontSize, font_weight: cs.fontWeight, color: cs.color,
                 font_family: String(cs.fontFamily).split(',')[0].replace(/["']/g, '')};
      if (extra) for (var p in extra) e[p] = extra[p];
      seq[role] = n + 1;
      out.push(e);
    }
    var rows = document.querySelectorAll('.panel .row');
    for (var i = 0; i < rows.length; i++){
      push('panel-key', rows[i].querySelector('.k'), {row: i});
      push('panel-value', rows[i].querySelector('.v'), {row: i});
    }
    push('label', document.getElementById('label'));
    push('caption', document.querySelector('.caption .strip p'));
    var art = document.getElementById('artWrap');
    if (art){
      var nodes = art.querySelectorAll('*');
      for (var j = 0; j < nodes.length; j++){
        var el = nodes[j], tag = String(el.tagName).toLowerCase();
        var cls = el.getAttribute && (el.getAttribute('class') || '');
        var anim = el.getAttribute && el.getAttribute('data-anim');
        /* 类名按空格分词：写成 \\bstamp\\b 会把 stamp-box、stamp-zh 也算成块，
           一个印章就倒出四条 art-block（父子的 textContent 还回显同一句）。 */
        var isBlock = tag === 'svg'
          || (cls && (' ' + cls + ' ').match(
               / (card|stamp|book|print|web|cover|theatre|night|svg|empty-art) /));
        var hasText = !!ownText(el) || tag === 'text' || tag === 'tspan';
        if (isBlock) push('art-block', el, {cls: cls, tag: tag});
        if (hasText) push('art-text', el, {cls: cls, tag: tag});
        if (anim) push('anim', el, {anim: anim, at: el.getAttribute('data-at') || '0',
                                    dur: el.getAttribute('data-dur') || '',
                                    count_to: el.getAttribute('data-count-to') || '',
                                    unit: el.getAttribute('data-num-unit') || ''});
      }
    }
    emit('HITMAP::', JSON.stringify({shot: S.id || '', settle_ms: S.settle_ms,
                                     t_ms: t, entries: out}));
  } catch (err) {
    emit('HITMAP_ERR::', String(err && err.message || err));
  }
})();
'''


def _caption_html(shot: dict[str, Any], timed: list[dict[str, Any]]) -> str:
    """字幕条：整句按字符切成词 span，逐词点亮，高亮词刷粉底。"""
    text = str(shot.get("text") or "")
    terms = [str(t) for t in (shot.get("highlight") or [])]
    if not timed:
        # 无旁白（纯字幕片）：整句在入场后一次给出，不做逐词点亮
        return (f'<div class="strip"><div class="tape"></div>'
                f'<p><span class="w on" data-at="0">{html.escape(text)}</span></p></div>')
    spans: list[str] = []
    cursor = 0
    for item in timed:
        if item["char_start"] > cursor:
            gap = text[cursor:item["char_start"]]
            spans.append(f'<span class="w" data-at="{item["start_ms"]:.0f}">'
                         f'{html.escape(gap)}</span>')
        token = text[item["char_start"]:item["char_end"]]
        hl = ' hl' if _is_highlight(item["token"], terms) or _is_highlight(token, terms) else ''
        spans.append(f'<span class="w{hl}" data-at="{item["start_ms"]:.0f}">'
                     f'{html.escape(token)}</span>')
        cursor = item["char_end"]
    if cursor < len(text):
        spans.append(f'<span class="w on" data-at="0">{html.escape(text[cursor:])}</span>')
    return ('<div class="strip"><div class="tape"></div>'
            '<p>' + "".join(spans) + '</p></div>')


def _panel_html(panel: dict[str, Any]) -> str:
    if not panel:
        return '<div class="panel" id="panel"></div>'
    rows = []
    for key, val in panel.items():
        rows.append(f'<div class="row"><div class="k">{_esc(key)}</div>'
                    f'<div class="v">{_esc(val)}</div></div>')
    return '<div class="panel" id="panel">' + "".join(rows) + '</div>'


def _label_html(label: dict[str, Any]) -> str:
    if not label:
        return '<div class="label" id="label" style="display:none"></div>'
    parts = [str(v) for v in label.values() if v]
    return f'<div class="label" id="label">{_esc(" · ".join(parts))}</div>'


def stamp_at_ms(shot: dict[str, Any], duration_sec: float) -> float | None:
    """这一镜的印章落下时刻（毫秒）；没有印章 → None。

    三类卡各自有默认落点，且**只在这里算一次**：模板要按它写 `data-at`，
    渲染层要按它决定「哪一帧之后画面不再变化」，两处各算一遍就会错开。
    """
    kind = str(shot.get("card") or "plain")
    dur_ms = duration_sec * 1000
    visual = shot.get("visual") or {}
    if kind == "stamp":                       # 整张卡就是印章
        return _as_float(visual.get("at_ms"), 0.0)
    if kind == "title":                       # 封面卡自带印章，默认落在 45% 处
        return _as_float(visual.get("at_ms"), dur_ms * 0.45)
    stamp = shot.get("stamp") or {}
    if stamp:                                 # 印章作为叠加层盖在版式上
        return _as_float(stamp.get("at_ms"), dur_ms * 0.6)
    return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def settle_ms(shot: dict[str, Any], timed: list[dict[str, Any]],
              duration_sec: float) -> int:
    """所有动画都停止变化的最早时刻：之后的帧像素相同，不必再花一次浏览器启动去截。

    版式里声明的动效（``data-anim``）也要算进来——它由 ``compile_art`` 现编现扫，
    所以内置图示卡的时值和模型自写 SVG 的时值走同一本账。连续动效（orbit/pulse/pan）
    **永不停止**，``anim_end_ms`` 直接给到镜头末尾：这类镜头每帧都是活帧，
    这是连续运动的成本，不是这里的保守估计。
    """
    ends = [ENTER_MS, 120 + TEAR_MS]
    if timed:
        ends.append(max(float(w["end_ms"]) for w in timed))
    at = stamp_at_ms(shot, duration_sec)
    if at is not None:
        ends.append(at + 220)
    dur_ms = duration_sec * 1000
    inner = compile_art(shot, duration_sec)[0]
    anim_end = _art.anim_end_ms(inner, dur_ms)
    if anim_end is not None:
        ends.append(anim_end)
    return round(min(max(ends), dur_ms))


def build_shot_page(shot: dict[str, Any], *, spec: dict[str, Any],
                    timed: list[dict[str, Any]], duration_sec: float,
                    width: int, height: int, fps: int) -> str:
    """一镜 → 一页自播放 HTML（headless Chrome 逐帧截图的输入）。

    画幅决定设计空间：**只有横屏换第二套版式**，竖屏与方图共用那一张 1080×1920 的
    竖构图——方图不是方版式，只是把同一条竖片居中、四周多留纸（参考片的排版本来就
    是为竖构图画的，硬套成方图只会把字幕挤成三行）。
    """
    wide = width > height
    dw, dh = (WIDE_W, WIDE_H) if wide else (DESIGN_W, DESIGN_H)
    inner, fit = compile_art(shot, duration_sec)
    wrap = "artBox" if fit == "box" else "artFill"
    payload = {
        "id": str(shot.get("id") or "shot"), "w": width, "h": height,
        "fps": fps, "dw": dw, "dh": dh,
        "duration_ms": round(duration_sec * 1000),
        "settle_ms": settle_ms(shot, timed, duration_sec),
        "enter_ms": ENTER_MS, "tear_ms": TEAR_MS,
    }
    styles = _CSS + _DIAGRAM_CSS + (_CSS_WIDE if wide else "")
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{_esc(shot.get('id'))}</title>
<style>{styles}</style></head>
<body>
<div class="stage{' wide' if wide else ''}" id="stage">
  <div class="bg">
    <svg class="scratches" viewBox="0 0 {dw} {dh}" preserveAspectRatio="none">{_scratches(dw, dh)}</svg>
  </div>
  {_panel_html(shot.get("panel") or {})}
  {_label_html(shot.get("label") or {})}
  <div class="art" id="artWrap"><div class="{wrap}">{inner}</div></div>
  <div class="caption" id="caption">{_caption_html(shot, timed)}</div>
</div>
<script>window.__SHOT__ = {json.dumps(payload, ensure_ascii=False)};</script>
<script>{_JS}</script>
<script>{_PROBE_JS}</script>
</body></html>'''


def _scratches(dw: int, dh: int) -> str:
    """纸纹按设计空间的比例摆：竖屏逐像素等于原来那六道，横屏不会全挤在左半边。

    纹是装饰不是版式——原样搬过去只会在 1920 宽里糊成一坨，不画又少一层纸质感。
    """
    sx, sy = dw / DESIGN_W, dh / DESIGN_H
    return "".join(
        f'<path d="M{x * sx:.0f} {y * sy:.0f} l{dx * sx:.0f} {dy * sy:.0f} '
        f'l{-dx * sx // 2:.0f} {dy * sy * 2:.0f}" />'
        for x, y, dx, dy in _SCRATCH_SEEDS)
