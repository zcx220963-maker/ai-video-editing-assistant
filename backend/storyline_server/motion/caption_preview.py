"""字幕样张：把「字幕样式 × 高亮样式」这一对渲成一张静帧，让人**在渲染前**看见它长什么样。

「文字库」里挑一对样式，等整片渲完才发现「这档字幕压在画面正中」——那是最贵的一种发现
（几分钟渲染 + 一次重来）。这里用与成片**同一条链**渲一张小图：同一份样片底稿
（`code_spec.sample_shot_code()`）、同一份注入的 props（`shot_input_props`），
只把时长与像素规模调小。所以样张上的字幕不是「照着成片的样子画出来的第二份实现」，
它就是成片那一条——样式、高亮、配色都是**同一件元件**（`@dg/film` 的 `CaptionLayer`）
当场排的。

三件事值得单独说：

* **一次一对**。2026-10-16 面板重做之后，字幕不再是一排「档」而是一张
  **字幕样式 × 高亮样式**的网（7 × 5）。不预渲整张网：用户点/看的是**哪一对**就渲哪一对
  （`preview_job`），渲过的那一对按内容寻址留在对象存储里，再看到就走缓存。
  一次一图也意味着样片代码只打包一遍——同一对里换色板走的就是同一个键前缀。
* **色板是入参，不是猜出来的**。成片那侧只能从「代码里的绑定」或「申报的底色」去认
  一镜用哪套色板（`code_spec.film_style_of`），而这里色板是用户点的那一档，直接给。
  所以样张里的 `film_style` 是显式构造的，不走那条反查。
* **样张按 (母版, 底稿, 色板, 字幕样式, 高亮样式, 强调色) 缓存**，键是对象存储里的内容地址。
  前两项凑成一张图的「决定因素」：`master_hash` 来自渲染容器（它算的是 `tokens.ts` 与
  零件文件的哈希），底稿那一项是 `sample_shot_code()` 那份代码自己的哈希。**两样都要**：
  零件换了而键不动，就会把旧零件渲出来的样张当新的发出去；底稿换了而键不动，就是
  这一头——改过底稿（挪那层柔光、换示范文案）之后，点开的还是**上一次那张旧图**。
  这两件事都只在第二次之后才看得出来，是最难查的那一类。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

from . import code_spec as cspec
from . import code_render as crender

#: 样张那句示范文案。选它的理由：够长（能断成两行，露出行距与盒高）、带标点
#: （字幕不会缩成一坨）、有两个可以点亮的词（高亮样式要看得出差别）。
SAMPLE_TEXT = "字幕跟着这一镜的色板走，念到哪个词就点亮哪个词"
SAMPLE_HIGHLIGHT = ("色板", "点亮")

#: 样张的画布与时长。竖屏 1080×1920 是最常见的画幅；3 秒（90 帧 @30fps）既够字幕
#: 入场动画落定，又够逐词点亮走到一半——**取哪一帧**就取这个「一半」：
#: 太早所有样式看起来都是「字还没出来」，太晚逐词点亮就与整句亮着没有区别。
PREVIEW_FPS = 30
PREVIEW_FRAMES = 90
PREVIEW_AT = 0.62
PREVIEW_SCALE = 0.4

#: 样片镜号。渲染容器拿它决定 `public/assets/<sid>/` 指到哪——样张一件素材都没有，
#: 这个号只是让那一步有个确定的值。
SAMPLE_SHOT_ID = "caption-preview"

#: 取色的槽位：`film_style` 的键 ← 色板的槽位。
_STYLE_SLOTS = (("bg", "page"), ("paper", "surface"), ("ink", "text"),
                ("accent", "accent"))

_HEX_RE = re.compile(r"#[0-9a-f]{6}")

#: 一对样式在对象键里的拼法。两个取值都是 `[a-z_]`，所以只消一个不会出现在取值里的
#: 分隔符——用 `+` 而不是 `-`：`on_picture` 自己带下划线，`-` 也不会歧义，但
#: 「字幕+高亮」读起来就是那一对，键名自解释。
_PAIR_SEP = "+"

#: 强调色接在同一段里的第二个分隔符（`on_picture+color@ff8a3d`）。为什么**不另开一段
#: 目录**：换了颜色＝另一张图，而这件事是**所有按这个键缓存的人**都要看见的——回执键、
#: 对象键、前端那张 `{色板: {键: 图}}` 表读的都是同一个字符串，多一段就多一处要对齐。
#: `#` 不带进来：对象键里有它不难看，但颜色那一栏在 URL 里本就走 `%23`，
#: 键里留纯十六进制反而好读好对。
_PAIR_COLOR_SEP = "@"


def pair_key(caption_style: str, highlight_style: str, color: str = "") -> str:
    """(字幕样式, 高亮样式[, 强调色]) → 一个既当回执键、又当对象键末段的字符串。

    强调色是用户在「文字库」钉的那一枚颜色（`code_spec.COLOR_KNOBS`），空串＝没钉。
    它进这一个键而不是别的任何地方，理由见 :data:`_PAIR_COLOR_SEP`。
    """
    base = f"{caption_style}{_PAIR_SEP}{highlight_style}"
    color = cspec.norm_highlight_color(color)
    return f"{base}{_PAIR_COLOR_SEP}{color[1:]}" if color else base


def sample_words(text: str = SAMPLE_TEXT, *, frames: int = PREVIEW_FRAMES,
                 chunk: int = 2) -> list[dict[str, Any]]:
    """样张的**词时间轴**：每两个字一段，均匀铺在一段时长里。

    没有它，`charStartFrames` 返回 null，「逐词点亮」就退化成「整句一直亮着」，
    于是几张样张长得一样、看不出区别来。它同时驱动 karaoke 的逐字透明度。
    段按 `text` 的顺序切（字幕元件是按顺序往后找词的），所以标点落在段首也不会对不上。
    """
    words = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    step = max(1, int(frames * 0.9 / max(1, len(words))))
    return [{"text": w, "startFrame": i * step} for i, w in enumerate(words)]


def film_style(palette_key: str) -> dict[str, str]:
    """样张的 `film_style`：从色板取「底色 / 条底 / 字色 / 强调色」四色。

    `palette_key` 空串＝「默认」那一档，返回空表——字幕元件据此走它自己的缺省
    （底色用 `TOKENS.bg`，字幕配色按亮度二档取）。认不出的色板名同样返回空表：**不猜**，
    宁可按默认渲一张，也不拿一套不相干的配色冒充。
    """
    if not palette_key:
        return {}
    pal = (cspec.palettes() or {}).get(palette_key)
    if not isinstance(pal, dict):
        return {}
    out: dict[str, str] = {}
    for key, slot in _STYLE_SLOTS:
        color = str(pal.get(slot) or "").strip().lower()
        if _HEX_RE.fullmatch(color):
            out[key] = color
    return out


def sample_fields(palette_key: str) -> list[dict[str, Any]]:
    """底稿代码能读到的两色（`fields.page` / `fields.surface`）。

    底稿上除了字幕什么都没有（见 `remotion/src/preview/caption-sample.tsx` 纪律 1），
    它用得着的就这两色：底色与那一层柔光。与 :func:`film_style` 同源同值——样片代码
    不绑色板名（一张样张要能在任意色板下渲），颜色只能从注入里来。缺的那一栏给空串，
    样片代码那边落回 `TOKENS`。

    为什么只声明两栏：闸① 的字段对账两头都判（读了没声明 / 声明了没读），多声明的
    那两色会直接判红。字幕那几色不经这条路——它们走 `film_style`（见 :func:`film_style`）。
    """
    style = film_style(palette_key)
    return [{"name": "page", "type": "color", "value": style.get("bg", "")},
            {"name": "surface", "type": "color", "value": style.get("paper", "")}]


def _spec(caption_style: str, highlight_style: str) -> dict[str, Any]:
    """合成一份最小整片参数：样张只要画幅、时长与字幕那两维。"""
    return {"fps": PREVIEW_FPS, "aspect": "portrait",
            "width": 1080, "height": 1920, "narration": False,
            "caption_style": _pick(caption_style, cspec.CAPTION_STYLES, "outlined"),
            "highlight_style": _pick(highlight_style, cspec.HIGHLIGHT_STYLES, "color"),
            # 整片进出场关掉：那是一层整帧位移，样张要站在原地给人量位置。
            "entrance": "none", "exit": "none"}


def _pick(value: str, allowed: tuple[str, ...], fallback: str) -> str:
    """认不出的一律落缺省——样张不是校验口，模型那份 spec 才由 `_norm_shot` 判。"""
    got = (value or "").strip().lower()
    return got if got in allowed else fallback


def preview_job(caption_style: str = "", highlight_style: str = "",
                palette_key: str = "", highlight_color: str = "") -> dict[str, Any] | None:
    """这一对样式的一组渲染载荷（`input_props` + 取哪一帧）。

    样片代码读不到（镜像里没带那份文件）时返回 `None`——调用方据此回一句「预览暂时
    渲不出来」，而不是发一次注定失败的请求。
    """
    code = cspec.sample_shot_code()
    if not code.strip():
        return None
    style = _spec(caption_style, highlight_style)
    color = cspec.norm_highlight_color(highlight_color)
    shot = {"id": SAMPLE_SHOT_ID, "code": code, "text": SAMPLE_TEXT,
            "highlight": list(SAMPLE_HIGHLIGHT),
            "fields": sample_fields(palette_key),
            "caption_style": style["caption_style"],
            "highlight_style": style["highlight_style"]}
    props = crender.shot_input_props(shot, style, frames=PREVIEW_FRAMES,
                                     frame_words=sample_words())
    sticky = film_style(palette_key)
    if sticky:
        # 显式覆盖 `shot_input_props` 那份：那一份是给**出片**用的（只有代码与申报的
        # 底色可认），而这里色板是用户点出来的，比反查更可靠。
        props["film_style"] = sticky
    if color:
        # 同理：用户钉的强调色在这里是**入参**，不经过任何反查。它进的是与成片同一个键
        # （`shot_input_props` 里那一栏，母版的 `injectedAccent()` 读它），所以样张上
        # 那几个高亮词的颜色就是成片里那几个词的颜色。
        props["highlightColor"] = color
    return {"key": pair_key(style["caption_style"], style["highlight_style"], color),
            "input_props": props,
            "frame": int(round(PREVIEW_FRAMES * PREVIEW_AT))}


def _hash12(text: str) -> str:
    """一段文本 → 12 位十六进制。键里放哈希而不是内容：内容会长到不能当路径。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def object_key(pair: str, *, palette_key: str, master_hash: str,
               sample_hash: str) -> str:
    """样张在对象存储里的地址：**内容寻址**（母版 · 底稿 · 色板 · 那一对样式）。

    前两段是一张样图的两个「决定因素」，缺一不可：零件换了（`master_hash` 变）落到新键上，
    不会把旧零件渲出来的样张当新的发出去；底稿换了（`sample_hash` 变）同样落到新键上，
    不会在挪过版面之后还发上一次那张旧图。两段用 `.` 连着读作「母版.底稿」，与色板那一段
    用 `/` 分开——它们是同一张图的父子关系，不是并列的三层目录。

    键里不带会话/产物作用域——样张不属于任何一次出片，按作用域放反而会在成片被清掉时
    一起消失（与试听音频同一条）。
    """
    master = re.sub(r"[^0-9a-zA-Z]", "", master_hash or "")[:12] or "nomaster"
    sample = re.sub(r"[^0-9a-zA-Z]", "", sample_hash or "")[:12] or "nosample"
    pal = palette_key or "default"
    return f"captionpreview/{master}.{sample}/{pal}/{pair}.jpg"


async def render_one(*, client: dict[str, Any], storage: Any,
                     caption_style: str = "", highlight_style: str = "",
                     palette_key: str = "", highlight_color: str = "") -> dict[str, str]:
    """渲（或从缓存取）这一对样式的样张 → `{pair: 对象键}`（渲不出来时是空表）。

    缓存判据是**对象在不在**（`objects.head`），与试听那条同规：库里有就不再渲第二次。
    渲染服务没起来不抛给调用方之外的地方——回空表由上层说一句「暂时渲不出来」，
    样张不是出片链路的一环。
    """
    job = preview_job(caption_style, highlight_style, palette_key, highlight_color)
    if job is None:
        return {}
    # 渲染服务连不上／没起来：回空表。样张不是出片链路的一环，这里抛出去只会让
    # 「看一眼样式」变成一条报错——而渲染服务本来就允许缺席（compose 里那两个
    # 服务之间没有依赖）。
    try:
        health = await asyncio.to_thread(client["health"])
    except Exception:  # noqa: BLE001 - 连不上就是没有样张
        return {}
    if not health.get("ok"):
        return {}
    # 底稿只读一次：键里那段哈希（下面）与真正发去渲的那份代码必须是**同一份文本**，
    # 各读一次就可能是两次读到不同的文件（改样张的那一刻正是这样），键与图就对不上了。
    code = cspec.sample_shot_code()
    key = object_key(job["key"], palette_key=palette_key,
                     master_hash=str(health.get("master_hash") or ""),
                     sample_hash=_hash12(code))
    if await storage.objects.head(key) is not None:
        return {job["key"]: key}

    stills = await asyncio.to_thread(
        client["stills"], code, job["input_props"],
        [int(job["frame"])], PREVIEW_SCALE)
    b64 = ""
    for still in stills or []:
        b64 = str((still or {}).get("jpeg_b64") or "")
        if b64:
            break
    if not b64:
        # 这一轮没渲出来：**不回键**。上层据此如实说「这一对暂时没有样张」，而不是给
        # 一个指向不存在对象的地址——前端会拿到破图，而破图与「这档就是这么素」在
        # 界面上长得一样。
        return {}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sample.jpg"
        path.write_bytes(base64.b64decode(b64))
        await storage.workspace.publish(path, key, content_type="image/jpeg")
    return {job["key"]: key}
