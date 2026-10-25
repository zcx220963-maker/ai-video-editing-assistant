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
from . import kernel, system
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
_STORY_SCENE_SYSTEM = """你在画一个叙事短片的一镜。只写场景本身，不写字幕、不加运镜。

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
**第一行 `const MOTIF_AT` 必须写**（归一化坐标，`r` 是半径占宽度的比例）。
它只声明、不渲染：外层在切镜时用它把"那件东西"从上一镜的位置接到下一镜的位置，
观众的眼睛才跟得住。**不写这一行，切镜那一处就会断。**
**不要 AbsoluteFill 外层**（外面会套运镜），用 Fragment。

**画面全部由你画**：`<svg viewBox={`0 0 ${width} ${height}`}>` + `<defs>`（线性/径向渐变、
遮罩）+ `<path>` / `<polygon>` / `<rect>` / `<circle>` / `<ellipse>` / `<line>`。
`width`/`height` 从 `useVideoConfig()` 拿。**没有"可选零件清单"——要画什么就画什么。**

三层结构（缺一层就会像一页排版）：
1. **背景**：天空/空间。用 **2~3 段不同色的线性渐变**（三段同色 = 这一镜失败）
   + 一处径向光晕（日/月/信号/火）。
2. **中景**：**一个能被指着说的具体东西**——射电望远镜、人影、城市废墟、行星、
   舰队、控制台、断裂的舰体……占画面 **25%~55%**，用 `<path>` 画轮廓，
   填近黑，边缘给 1~3px 的冷色描边当轮廓光。
3. **前景**（可选）：地面/水面/栏杆/尘埃，压住画面下沿，让空间有纵深。

光与色（决定像电影还是像 PPT）：
· **全片只有一个主光**，亮部面积 <15%。
· **整帧平均亮度 ≥ 38/255**（参考片实测 p50 = 41.6；低于 30 就像"欠曝"）；
  主体与背景的亮度差 **≥ 40 级**（差不够就看不见）。
· 底色 `#05070c`~`#141a24`；高饱和色**只用来点光**（火/信号/星）。
· **不要高饱和**：大块面用灰蓝/褐黑，SATAVG 目标 5~12。
· 两拍：0~60% 建立画面，60~100% 一个缓慢变化（光变强、粒子移动、轮廓微移）。

**不要写**：`ChapterMark` / `TimeMark` / `RichCaption`（字幕与标记由外层统一加）、
`Camera`（外层已经套了运镜）。你自己再画一套会出现两个章节号。
不引用外部图片/字体。时间只从 `t` 拿，不写死秒数。

**如果这一镜里画了那件贯穿的东西，必须在组件体最上面声明它的位置**（只声明，不渲染）：

```tsx
const MOTIF_AT = { x: 0.62, y: 0.45, r: 0.05 };   // 归一化坐标，r 是半径占宽度的比例
```

这一行是给**外层**看的：切镜时外层会用它把"那件东西"从上一镜的位置**接到**下一镜的位置，
于是观众的眼睛跟得住。不写这一行，这一镜就没有接力——那一处会显得断。
（这一镜里没有那件东西时，写成 `const MOTIF_AT = null;`）"""


async def write_scene_story_code(scene: dict[str, Any], *, index: int, total: int,
                                 seconds: float, aspect: str, llm: Any = None,
                                 user_id: str = "", conversation_id: str = "",
                                 extra: str = "", motif: str = "") -> str:
    """让模型用**电影感零件**画这一镜的场景（1 次调用）。

    `extra` 是质检打回时的具体指令；`motif` 是**贯穿全片的那一件东西**——
    它在每一镜里以不同大小/位置出现，十几张独立的图才变成"一部片子"。
    参考片里那个"蓝色小点"就是这么用的（第 2 镜一个点、第 6 镜是舰队、末镜是地球）。
    """
    ask = (f"这是第 {index + 1}/{total} 镜，时长 {seconds:g} 秒。\n"
           f"**这一镜要画的画面**：{str(scene.get('plate') or scene.get('brief') or '').strip()}\n"
           f"（这一镜的字幕是「{str(scene.get('text') or '').strip()}」——"
           f"**不要把它画进画面**，字幕由外层统一加。）\n"
           + (f"**全片贯穿的一件东西**：{motif}。这一镜里它也要出现，"
              f"大小/位置/明暗和别的镜不同（很小的一处也算；"
              f"它是让十几镜连成一部片子的那根线）。\n" if motif and index >= 2 else "")
           + "\n**输出的第一行必须是这一句**（位置用归一化坐标 0~1，r 是半径占宽度比例）：\n"
             "`const MOTIF_AT = { x: <0~1>, y: <0~1>, r: <0.02~0.3> };`\n"
             "（这一镜里没有那件东西就写 `const MOTIF_AT = null;`）\n"
             "**这三个数按镜号换，不要照抄**——否则接力变成原地发光，等于没做：\n"
             f"  · 第 {index + 1} 镜：x 取 "
             f"{'0.18~0.38（偏左）' if index % 2 == 0 else '0.62~0.82（偏右）'}；"
             f"y 取 {['0.28~0.42', '0.48~0.60', '0.66~0.80'][index % 3]}；"
             f"r 取 {['0.03~0.05', '0.06~0.09', '0.10~0.14'][index % 3]}。\n"
           + (f"\n**上一版的问题（必须修掉）**：{extra}\n" if extra else "")
           + f"画幅 {aspect}。只写那两行（`MOTIF_AT` 与 `const Scene`），不写 import/export。")
    last = ""
    for _ in range(3):
        try:
            raw = await kernel._complete(
                [{"role": "system", "content": _STORY_SCENE_SYSTEM},
                 {"role": "user", "content": ask}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
        except Exception as exc:  # noqa: BLE001
            raise KernelError("scene", f"第 {index + 1} 镜写场景失败：{exc}") from exc
        body = kernel.extract_code(raw) or str(raw or "").strip()
        if "const Scene" not in body:
            last = "没按格式回 `const Scene`"
            continue
        # 从 `const MOTIF_AT` 或 `const Scene` 里**靠前**的那个开始留。
        # 2026-10-25 踩过：原来一刀切在 `const Scene`，把模型写在最上面的
        # `const MOTIF_AT = {…}` 声明切掉了——于是锚点永远读不到、跨镜接力永远不接，
        # 而 lint 与渲染都正常，表现是"功能像没做一样"。
        _marks = [p for p in (body.find("const MOTIF_AT"), body.find("const Scene")) if p >= 0]
        body = body[min(_marks):].strip()
        # 延迟 import：避免模块级循环。**这两个名字都是从 film 来的**——
        # 2026-10-25 拆文件时只搬了 `_looks_complete`、漏了 `SCENE_HEADER`，
        # 于是每一镜都在这里 NameError，然后被兜底静默盖住（整片 15 镜全成兜底、
        # 日志上只看到"质检 1.0"）。拆文件时**凡是引用了没搬走的全局名，都要在这里出现**。
        from .film import SCENE_HEADER, _looks_complete
        if not _looks_complete(body):
            last = "组件体不完整"
            continue
        body = _ensure_motif(body)      # 用了没声明 → 补一个安全声明（否则渲染炸）
        # **每镜写完立刻 lint**：括号配平查不出"标签没闭合"这类错，而那种错
        # 会让整片在 `parse-error 第 2427 行` 处失败——那时已经烧了 15 次调用，
        # 还得靠人肉去数第几个镜。这里探一遍，坏的就只重写这一镜。
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
    from .film import (_STORY_SYSTEM, STORY_MAX_SHOT_SEC,  # noqa: PLC0415
                       STORY_MIN_SHOT_SEC, shot_count_story)

    n = shot_count_story(seconds)
    ask = (f"主题：{prompt.strip()}\n\n"
           f"总时长 {seconds:g} 秒，请拆成 **{n} 个镜头**，每镜约 "
           f"{seconds / n:.1f} 秒（{STORY_MIN_SHOT_SEC:g}~{STORY_MAX_SHOT_SEC:g} 秒之间）。"
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
    scenes = [s for s in got["scenes"] if isinstance(s, dict)][:n]
    if not scenes:
        raise KernelError("plan", "分镜里没有可用镜头")
    # `motif` = 贯穿全片的那一件东西。模型没给就留空（不影响出片），
    # 给了就注入每一镜的写手提示——这是"十几张图变成一部片子"的那根线。
    # `sources` = 数字的出处账（见 `facts.py`）。存进 plan 就跟着工程落盘。
    from .facts import normalize    # 延迟 import：story 不在模块级碰 film 以外的东西
    return {"title": str(got.get("title") or "").strip(),
            "motif": str(got.get("motif") or "").strip()[:20],
            "sources": normalize(got.get("sources")),
            "scenes": scenes}, 1
