# -*- coding: utf-8 -*-
"""叙事片（`mode="story"`）怎么画一镜。

从 `film.py` 拆出来的（2026-10-25）：那边已经 1,363 行、装着两套完全不同的片子，
而旧链就是 7 千行一个文件。这里的边界很清楚——

    film.py    编排：分镜 → 并发写镜 → 拼装 → 质检环 → 分派
    story.py   画法：一镜的提示词、写手、分镜
    scene.py   包装：运镜/文字层/字幕排版 + 兜底三套 + 画法指纹

**本文件不在模块级 import film**（只有 `write_scene_story_code` 里延迟 import
`_looks_complete`），所以 film 可以放心地在模块级 import 这里，没有循环。

包装那一半（`wrap_story_scene` / `safe_story_scene` / `_caption_fit` /
`_scene_fingerprint` / `_ensure_motif`）在 `scene.py`——它们不调模型，是确定性代码，
跟"写手怎么被提示"没有关系。`story.py` 只在这里**重新导出** `_ensure_motif`，
因为写手写完要用它补锚点声明。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..motion import remotion_client as rc
from . import caption, fixes, kernel, system
from .kernel import KernelError
from .scene import _ensure_motif  # noqa: F401  —— 写手内部用，也对外保持旧名字可用


#: 叙事片的**画面写手**。注意它被要求"不要 AbsoluteFill、不要运镜"——
#: 外层由 `wrap_story_scene` 统一套 `Camera` 与文字层。
#:
#: **这一版故意不给"可选零件清单"**（2026-10-25 改）。原因有实测：
#: 上一版列了 11 件电影感零件，15 镜跑出来只有 **7 种组件集合**、**0 处自由 SVG**——
#: 给了清单它就在清单里选，清单本身就是天花板。而参考片那种片子的画面
#: （外星文字、四人控制台、断裂舰体）**从来不是从零件库里挑的**，是现场画出来的。
#:
#: 现在的做法与 Remotion 官方 Agent Skills 一致：给**画法纪律**，不给零件目录。
#: 画面全部由模型用 SVG 画（`<path>` / `<polygon>` / 渐变 / 遮罩），
#: 外层只提供三样它不该管的东西：运镜、文字层、以及一个"画不出来时"的兜底。
_STORY_SCENE_SYSTEM = """你在画一个叙事短片的一镜。画面、运镜、字幕、章节标记——**全由你决定**。

输出格式（一字不差）：
```tsx
const MOTIF_AT = { x: 0.5, y: 0.5, r: 0.05 };   // 这一镜里那件贯穿的东西在哪；没有就写 null
const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <>
      {/* 画面：全部由你用 SVG 画 */}
    </>
  );
};
```
**第一行 `const MOTIF_AT` 必须写**（归一化坐标，`r` 是半径占宽度的比例）；这一镜没有那件
东西就写 `const MOTIF_AT = null;`。它只声明、不渲染：外层切镜时用它把"那件东西"从上一镜
的位置接到下一镜的位置——**不写这一行，切镜那一处就会断。**
**外层只做兜底**：你自己写 `<AbsoluteFill>`（含运镜/字幕），就不套外面那一层。

**接着上一镜画**：光从同一方向来、配色同一套——但要换机位/换尺度；构图不许重复。

**画面全部由你画**：`<svg viewBox={`0 0 ${width} ${height}`}>` + `<defs>`（线性/径向渐变、
遮罩）+ `<path>` / `<polygon>` / `<rect>` / `<circle>` / `<ellipse>` / `<line>`。
`width`/`height` 从 `useVideoConfig()` 拿。**没有"可选零件清单"——要画什么就画什么。**

画面结构**由你定**：要空间感就分层（背景/中景/前景）；要平面、要特写、要一整块纯色也行。
唯一硬要求：**这一镜得有能被指着说的东西，而且它要看得见**——面积 ≥ 画面 15%、与背景差 ≥40 级。
**"留空"是背景留空，不是主体消失**：一块大暗部加一个小点，在手机上是纯黑。
· 变化要有先后：先建立，再动。挤在同一瞬间变完就是一次跳变，不是动效。

光与色：**基调照 `tone` 画**（底色、主色、构图习惯、笔触都在那里，别自己另起一套）。
只有两条硬底线，因为它们决定"看不看得见"：整帧平均亮度 ≥ 38/255；主体与背景差 ≥ 40 级。

**这一镜的语言由你定**：`Camera`（推/拉/横移）、`ChapterMark` / `TimeMark` / `RichCaption`
都可以用。**用了就自己写 `<AbsoluteFill>`**（写它＝外层不再插手）；不写外层兜底加一层缓推 + 一句字幕。
不引用外部图片/字体。时间只从 `t` 拿，不写死秒数。"""


async def write_scene_story_code(scene: dict[str, Any], *, index: int, total: int,
                                 seconds: float, aspect: str, llm: Any = None,
                                 user_id: str = "", conversation_id: str = "",
                                 extra: str = "", motif: str = "") -> str:
    """让模型用**电影感零件**画这一镜的场景（1 次调用）。

    `extra` 是质检打回时的具体指令；`motif` 是**贯穿全片的那一件东西**——
    它在每一镜里以不同大小/位置出现，十几张独立的图才变成"一部片子"。
    参考片里那个"蓝色小点"就是这么用的（第 2 镜一个点、第 6 镜是舰队、末镜是地球）。
    """
    cap = caption.plan(scene)
    text = str(scene.get("text") or "").strip()
    ask = (f"这是第 {index + 1}/{total} 镜，时长 {seconds:g} 秒。\n"
           f"**这一镜要画的画面**：{str(scene.get('plate') or scene.get('brief') or '').strip()}\n"
           # 字幕归谁画由分镜的 `caption.mode` 定（见 caption.py）。drawn 时下面那段
           # "不要画字幕"要当场反过来说——否则写手照旧不画，外层也不加，这一镜就没字。
           + (caption.drawn_brief(scene) if cap["mode"] == "drawn" else
              f"（这一镜的字幕是「{text}」——"
              f"**不要把它画进画面**，字幕由外层统一加。）\n")
           + (f"**全片贯穿的一件东西**：{motif}。这一镜里它也要出现，"
              f"大小/位置/明暗和别的镜不同（很小的一处也算；它是让十几镜连成一部片子的那根线）。\n"
              if motif and index >= 2 else "")
           + "\n**输出的第一行必须是这一句**（位置用归一化坐标 0~1，r 是半径占宽度比例）：\n"
             "`const MOTIF_AT = { x: <0~1>, y: <0~1>, r: <0.02~0.3> };`\n"
             "（这一镜里没有那件东西就写 `const MOTIF_AT = null;`）\n"
             "**这三个数按镜号换，不要照抄**——否则接力变成原地发光，等于没做：\n"
             f"  · 第 {index + 1} 镜：x 取 "
             f"{'0.18~0.38（偏左）' if index % 2 == 0 else '0.62~0.82（偏右）'}；"
             f"y 取 {['0.28~0.42', '0.48~0.60', '0.66~0.80'][index % 3]}；"
             f"r 取 {['0.03~0.05', '0.06~0.09', '0.10~0.14'][index % 3]}。\n"
           # `extra` 是整片的语法（tone + 前两镜 + 可用画法），不是"上一版的问题"。
           # 原来这里标成「上一版的问题（必须修掉）」——tone 被当成 bug 清单发下去了。
           + (f"\n{extra}\n" if extra else "")
           + f"画幅 {aspect}。只写那两行（`MOTIF_AT` 与 `const Scene`），不写 import/export。")
    last = ""
    retry = ""      # 上一版被挡下的原因，拼进下一次；盲重试＝同一句话采样三次（2026-10-26 改）
    for _ in range(3):
        try:
            raw = await kernel._complete(
                [{"role": "system", "content": _STORY_SCENE_SYSTEM},
                 {"role": "user", "content": ask + retry}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
        except Exception as exc:  # noqa: BLE001
            raise KernelError("scene", f"第 {index + 1} 镜写场景失败：{exc}") from exc
        body = kernel.extract_code(raw) or str(raw or "").strip()
        if "const Scene" not in body:
            last = "没按格式回 `const Scene`"
            retry = "\n\n**上一版没有 `const Scene`**：只要 `MOTIF_AT` 与 `Scene` 两行。"
            continue
        # 从靠前的那个开始留：一刀切在 `const Scene` 会切掉写在最上面的
        # `const MOTIF_AT`，于是锚点读不到、接力不接，而 lint 与渲染都正常。
        _marks = [p for p in (body.find("const MOTIF_AT"), body.find("const Scene")) if p >= 0]
        body = body[min(_marks):].strip()
        # 延迟 import：拆文件时漏搬 `SCENE_HEADER` 会让每镜在这里 NameError，
        # 然后被兜底静默盖住（整片全兜底，日志只显示"质检 1.0"）。
        from .film import SCENE_HEADER, _looks_complete
        if not _looks_complete(body):
            last = "组件体不完整"
            retry = "\n\n**上一版组件体不完整**（标签没闭合），重写并自己数一遍闭合。"
            continue
        # 确定性修补必须在 lint 之前：字体族 / div 纯色块这两类必然被挡，不该烧模型调用。
        body = fixes.before_lint(_ensure_motif(body))
        # **每镜写完立刻 lint**：括号配平查不出"标签没闭合"，而那种错会让整片
        # 在 `parse-error` 处失败——那时已烧掉十几次调用，还得人肉数第几镜。
        probe = (SCENE_HEADER + "\n" + system.SYSTEM_TSX + "\n" + body
                 + "\n\nexport default Scene;\n")
        try:
            got_lint = await asyncio.to_thread(rc.lint, probe)
        except rc.RemotionError:
            got_lint = {"ok": True}          # 服务不可达时不要误杀
        if not got_lint.get("ok"):
            errs = got_lint.get("errors") or []
            last = "lint：" + "；".join(
                f"[{e.get('rule')}] {str(e.get('msg'))[:60]}" for e in errs[:2])
            rows = "\n".join(f"- 第 {e.get('line')} 行 [{e.get('rule')}] "
                             f"{str(e.get('msg'))[:120]}" for e in errs[:6])
            retry = (f"\n\n**上一版被 lint 挡下，这几条必须改掉**：\n{rows}\n"
                     "（字体只写 `TOKENS.fontStack`；纯色块用 SVG rect。）")
            continue
        # **字幕必须真的画进去**（2026-10-26）：字幕交给模型画之后，真机 26 镜里 17 镜
        # 根本没写那行字——旁白念了、画面上没有，观众感觉"音画不同步"。用代码判字在不在，
        # 比让评审看图可靠（评审看图只能"觉得像有字"）。
        if cap["mode"] == "drawn" and text and \
                sum(1 for c in set(text) if c in body) < len(set(text)) * 0.9:
            last = "字幕没画进画面"
            retry = (f"\n\n**上一版没把字幕画进画面**：画面里必须出现「{text}」这一整句，"
                     "用 `<text>` 或 div 写出来。")
            continue
        return body
    raise KernelError("scene", f"第 {index + 1} 镜写了 3 次仍不成形：{last}")


async def plan_story(prompt: str, *, seconds: float,
                     aspect: str = kernel.DEFAULT_ASPECT, llm: Any = None,
                     user_id: str = "", conversation_id: str = ""
                     ) -> tuple[dict[str, Any], int]:
    """叙事片的分镜（1 次调用）。

    依赖的四个名字（提示词、镜数、镜长上下限）仍在 `film.py`——那里是"两种模式共用
    的骨架"。延迟 import 而不是搬到这边：搬了 film 又要反过来 import，
    循环就是这么来的。
    """
    from .film import (_STORY_SYSTEM, STORY_MAX_SHOTS,  # noqa: PLC0415
                       STORY_MAX_SHOT_SEC, STORY_MIN_SHOT_SEC)

    # **镜数由内容定**（2026-10-26 改）：以前这里写死"请拆成 n 个镜头"，
    # 于是同样的秒数永远出同样的镜数——那是公式，不是分镜。现在只给范围：
    # 每镜 4.5~12 秒，所以 lo~hi 都算对。讲得下就少几镜（长镜更沉）。
    lo = max(3, int(seconds / STORY_MAX_SHOT_SEC))
    hi = min(STORY_MAX_SHOTS, int(seconds / STORY_MIN_SHOT_SEC))
    ask = (f"主题：{prompt.strip()}\n\n"
           f"总时长 {seconds:g} 秒。**镜数由内容定**：每镜 "
           f"{STORY_MIN_SHOT_SEC:g}~{STORY_MAX_SHOT_SEC:g} 秒，所以 {lo}~{hi} 镜都算对。"
           f"讲得下就少几镜，讲不下就多几镜，别为了凑数把一句话拆成两句。"
           f"画幅 {aspect}。")
    try:
        raw = await kernel._complete(
            [{"role": "system", "content": _STORY_SYSTEM},
             {"role": "user", "content": ask}],
            llm=llm, user_id=user_id, conversation_id=conversation_id)
    except Exception as exc:  # noqa: BLE001
        raise KernelError("plan", f"分镜调用失败（{type(exc).__name__}）：{exc}") from exc

    from .film import _parse_json      # 延迟 import：避免模块级循环
    got = _parse_json(raw)
    if not got or not isinstance(got.get("scenes"), list) or not got["scenes"]:
        raise KernelError("plan", "分镜没回合法 JSON")
    scenes = [s for s in got["scenes"] if isinstance(s, dict)][:STORY_MAX_SHOTS]
    if not scenes:
        raise KernelError("plan", "分镜里没有可用镜头")
    # `motif` = 贯穿全片的那一件东西。模型没给就留空（不影响出片），
    # 给了就注入每一镜的写手提示——这是"十几张图变成一部片子"的那根线。
    # `sources` = 数字的出处账（见 `facts.py`）。存进 plan 就跟着工程落盘。
    # `angle` / `hook` = 模型自己挑的讲法角度与开场那句钩子（用户只给话题时才有）。
    # 留着是为了让用户能在分镜表上看见"它选了哪个角度"，而不是黑箱。
    from .facts import normalize    # 延迟 import：story 不在模块级碰 film 以外的东西
    return {"angle": str(got.get("angle") or "").strip()[:24],
            "hook": str(got.get("hook") or "").strip()[:80],
            "title": str(got.get("title") or "").strip(),
            "motif": str(got.get("motif") or "").strip()[:20],
            "sources": normalize(got.get("sources")),
            "scenes": scenes}, 1
