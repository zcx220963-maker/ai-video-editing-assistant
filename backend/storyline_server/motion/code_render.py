# -*- coding: utf-8 -*-
"""代码镜这条链的执行层：分镜 spec → creation-remotion 逐镜渲染 → 八道闸 → 母带。

与 `render.py`（本文件里叫 `rpipe`）的分工：那一层放**与像素来源无关**的共用件——
配音补长、词时间轴、按镜缓存的收发协议、质感滤镜串（`apply_texture`，本链按镜各跑一趟）、
BGM 闪避。本文件只管代码镜这一段：
把一镜交给 creation-remotion，过八道闸里属于这一层的那七道（闸① 在容器侧、本层只间接受力；
闸⑦ 判在契约层），再拼母带。
共用的那几件不复制第二份，
「有哪几档、每档是哪条滤镜串」只许有一个出处（`render.TEXTURE_FILTERS`），
两处各写一遍迟早不一样。

八道闸各在这一层的哪一步：

    闸① lint        容器里跑，本层只经 `/render-shot` 间接强制（服务端自己再 lint）
    闸② 帧预算      :func:`narration_for` 按 TTS 实测算 frames，超 MAX_FRAMES_TOTAL 抛错
    闸③ 帧账对账    :func:`_check_frame_account` 本地 ffprobe vs 计划帧数
    闸④ 画面非空    :func:`_empty_check` 三帧在母版画面带内数非底色像素 **且** 量内容
                     外接盒铺开了多少（两半合起来判，只看像素占比会判死细线条画风）
    闸⑤ 跨镜一致性  :func:`check_token_drift` 母版回读 token 逐键比，**容差 0**
    闸⑥ 字段对账    :func:`check_field_reconcile` 母版回读的自报值 vs 注入值逐项比
    闸⑦ 风格配额    **不在这一层**——它是整片一层的判据，读的是代码字节里的 import，
                     所以在烧像素之前就判完了（`code_spec.validate_code_spec`）
    闸⑧ 版面回读    :func:`summarize_layout` 母版在探针帧扫画面带里的叠字/被裁/悬空引线，
                     **只记账不拦截**（八道里唯一不设红的那道，理由写在那段的 docstring 里）
    闸⑭ 动效密度    :func:`summarize_motion` 容器按 8 Hz 抽帧数「先后变化段」，
                     :func:`motion_error` 把「静帧 / 一次跳变 / 挨太近」翻成退镜
                     （**判在烧像素之后**：自报的 `card.motion` 不是证据，像素才是）

三条不能忘的口径：

1. **切片自带音轨**。Remotion 渲出来的是哑片，本层把配音混进 `clip.mp4` 之后才进缓存
   与拼接——所以缓存命中时**连 TTS 都不重发**，也所以 `voice/rate/narration` 必须进
   缓存键（见 `code_spec.FILM_KNOBS`）。
2. **时长归声音，不归模型**。模型写的 `min_duration_sec` 只是下限；闸② 注入的
   `durationInFrames` 一律由实测语音时长 + 前后呼吸算出。
3. **闸失败是失败，不是降级**。帧数对不上、画面是空的、两镜字号不一致，一律抛错带镜号
   回清单——静默放过就等于把缺陷当产物交出去（当年卡片链 #102 那次「ok=True 的空画面」
   就是这条要接管的东西）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .. import mediaops
from ..mediaops import MediaError
from ..providers import Providers
from . import code_spec as cspec
from . import remotion_client as rc
from . import render as rpipe

Progress = Callable[[dict[str, Any]], "Awaitable[None] | None"]

#: 一镜内两次进度心跳的间隔（秒）。渲染服务是**一次请求跑完一整镜**，中间没有帧级事件；
#: 不heartbeat的话，一镜跑到停滞看门狗的窗口里就会被误判成死任务（那一次像素其实还在烧）。
HEARTBEAT_SEC = 20.0

#: 闸④：像素与母版底色的差要超过这个级差才算「这里有个东西」。留的是 x264 在平色背景上
#: 的编码噪声余量——实测纸色 #f6f1e4 经一趟 yuv420p 往返后各通道漂 2~4，24 是六倍开外。
EMPTY_DELTA = 24
#: 闸④：三帧里最低的「非底色占比」低于这个数就退回。2% 是 spec §3 的起步值，
#: P4 用三幕夹具量出实际分布后再定终值——定高了会把真·极简版式判死，定低了等于没闸。
EMPTY_MIN_RATIO = 0.02
#: 闸④取样的三个位置。**不是字面的「前/中/末」**：第 0 帧正卡在母版的入场动画起点
#: （字幕 opacity=0、主体还没进来），拿它判空会稳定误报。0.15 已经过完入场。
EMPTY_SAMPLES = (0.15, 0.5, 0.85)

# ---- 闸④ 的第二半：内容在带子里**铺开**了多少（外接盒短边 / 带短边，取三帧最满那一帧） ----
#
# 这三个数是 2026-10-11 拿六支真渲夹具量的（roughjs / three.js / KaTeX × 横屏竖屏，
# 同一套像素口径，只是再从二值帧里算非底色像素的外接盒）。分布：
#
#     three.js   74% ~ 100%      实心形体，本来就铺满
#     roughjs    52% ~ 83%       斜纹与轮廓线，像素占比低到 0.99% 而构图是满的
#     KaTeX      68% ~ 95%       字形细，同上
#     当年判漏的那批横屏镜  ≈9% ~ 36%   母题挤在中间一小块
#
# 所以线画在 45%：它在**真画了东西的细线条画风**（最低 52%）与**挤成一团的构图**
# （最高 36%）之间留出一截余量。为什么必须看铺开而不是只看像素占比——上面那行
# roughjs 就是答案：一条 1% 的斜纹带子按旧口径要判「空壳」，可它画得满满的，
# 拦下来的只会是把线条画细这一件好事。
#: 像素占比不够时的**豁免线**：内容横跨带短边达到它，就不算空壳（低像素的细线条画风靠这条活着）
SPAN_MIN = 0.45
#: **无条件**下限：像素占比够也拦得住——一大块颜色挤在中间 20% 是「小气」，不是「满」
TIGHT_MIN = 0.30
#: 技能正文「把母题按 useBody() 铺满（短边的 60%+）」那句的机器形态：低于它只记账提醒
FILL_TARGET = 0.60

#: 跟着切片一起缓存的本镜账字段。命中时照单还原，缺任一项当未命中重渲（纪律：
#: 少一个键让下游 `rec["..."]` KeyError，等于把一次缓存格式升级变成一次崩溃）。
#: `field_checks` 是闸⑥ 的逐字段结论（值对没对、盒量没量到）——命中时若不还原，
#: 缓存镜的账就会比现渲镜少一栏，而前端〈字段〉页与「无空间命中」列表全靠它。
#: `fill_span` 同理：它是闸④ 的第二半（内容在带子里铺开了多少），不还原的话
#: 「这次有几镜没铺满」那道提醒会在复用切片的那几镜上静默消失。
#: `layout` 同理（闸⑧ 的版面回读）：不还原的话，缓存镜在整片账上就成了「没报问题」，
#: 而它其实是「这一次压根没扫」——两者在回执上必须能分开。缓存键含母版哈希，母版探针
#: 一改所有旧缓存自然失效，所以这一栏加进去不会多烧一镜。
#: `motion` 是闸⑭ 的动效采样读数（段数、间隔、阈值）。**这一栏尤其不能丢**：丢了的镜
#: 在整片账上就成了「动效没判过」却没有任何痕迹，而这一闸判的是「有动效但看不出动」——
#: 正是那种只有账能说话、人眼在成片里看不出来的缺陷。
META_FIELDS = ("sec", "speech_sec", "planned_frames", "frames", "words", "degraded",
               "measured_sec", "width", "height", "fps", "empty_ratio", "fill_span",
               "tokens", "field_checks", "render_timings", "cache_key", "layout",
               "asset_motion", "assets", "motion")

#: 母版回读字段盒的探针帧尾偏移（`durationInFrames - 本值`）。**必须与
#: `remotion/src/tokens.ts` 的 `FIELD_PROBE_TAIL` 同值，两处一起改**：闸⑥ 比的是那一刻
#: DOM 上的值，代表帧也取那一刻，两张东西说的才是同一帧画面——差一帧就成了「盒在这里、
#: 帧在那里」，而字段级命中表整个建立在两者对齐上。
PROBE_TAIL = 2


async def _emit(progress: Progress | None, ev: dict[str, Any]) -> None:
    """报一次进度。节点层传进来的进度函数是协程，拿到返回值必须 await 到底：
    把它 close 掉等于这次续期整条丢掉，而看门狗按「无进展时长」判死。"""
    if progress is None:
        return
    r = progress(ev)
    if asyncio.iscoroutine(r):
        await r


async def _await_heartbeat(progress: Progress | None, *, sid: str, index: int,
                           total: int, awaitable: Any) -> Any:
    """等一个长任务（一镜的整段渲染），期间每 HEARTBEAT_SEC 报一次进度。"""
    task = asyncio.ensure_future(awaitable)
    elapsed = 0.0
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=HEARTBEAT_SEC)
        except asyncio.TimeoutError:
            elapsed += HEARTBEAT_SEC
            await _emit(progress, {"stage": "rendering", "shot": sid, "rendering": sid,
                                   "done": index, "total": total,
                                   "elapsed_sec": round(elapsed)})


def _to_frame_words(timed: list[dict[str, Any]], fps: int) -> list[dict[str, Any]]:
    """词时间轴（ms）→ 母版要的 `{text, startFrame}`。

    时刻在这里换算而不是在浏览器里换算：闸② 已经把 `durationInFrames` 钉成整数，
    词帧也必须落在同一个刻度上，否则「最后一个字亮起来的那一帧」会掉在片子外面。
    """
    out = []
    for w in timed:
        token = str(w.get("token") or "")
        if not token:
            continue
        out.append({"text": token,
                    "startFrame": max(0, int(round(float(w.get("start_ms") or 0.0)
                                                   * fps / 1000.0)))})
    return out


async def narration_for(shot: Mapping[str, Any], spec: Mapping[str, Any],
                        providers: Providers, shot_work: Path) -> dict[str, Any]:
    """一镜的旁白与帧预算（闸② 的算式）。→ {timed, speech, sec, frames, voice, degrade}。

    算式的本体在 `render.py` 那一层（`_speech_of` / `align_words` / `BREATH_*` /
    `DEFAULT_SHOT_SEC`），这里只调用不复写——分镜阶段给用户的估算与这道帧预算必须是
    同一把尺，两处各写一遍迟早一个算呼吸余量一个不算。
    """
    text = str(shot.get("text") or "")
    fps = int(spec["fps"])
    shot_work.mkdir(parents=True, exist_ok=True)
    voice_file = shot_work / "voice.wav"
    mp3 = shot_work / "voice.mp3"
    timed: list[dict[str, Any]] = []
    speech = 0.0
    degrade: str | None = None
    speech_src: Path | None = None
    if spec.get("narration") and text:
        words, speech, degrade = await rpipe._speech_of(
            providers, text, mp3, voice=str(spec.get("voice") or ""),
            rate=str(spec.get("rate") or "+0%"))
        if degrade is None:
            timed = rpipe.align_words(text, words)
            speech_src = mp3
    if timed:
        sec = max(float(shot.get("min_duration_sec") or 0.0),
                  speech + rpipe.BREATH_LEAD_SEC + rpipe.BREATH_TAIL_SEC)
    else:
        sec = max(float(shot.get("min_duration_sec") or 0.0),
                  rpipe.DEFAULT_SHOT_SEC + len(text) * 0.16)
    if speech_src is None:
        mediaops.silent_wav(voice_file, duration=sec)
    else:
        rpipe._pad_audio(speech_src, sec, voice_file)
    frames = max(2, int(round(sec * fps)))
    return {"timed": timed, "speech": speech, "sec": sec, "frames": frames,
            "voice": voice_file, "degrade": degrade}


def _check_frame_account(sid: str, *, planned_frames: int, fps: int,
                         info: dict[str, Any], expected_size: tuple[int, int]) -> None:
    """闸③：本地 ffprobe 的读数 vs 分镜账。**不静默截帧、不静默补帧**。

    对账在 Python 侧而不是只信容器回读的 `probed`：回读发生在**另一台机器的另一份文件**
    上，字节还要过一遍 HTTP。传输出问题时的现场是「容器说 90 帧、本机这文件只有 87 帧」，
    只有本机这一次探测是这次片子的真相。
    """
    got = int(info.get("frames") or 0)
    if got <= 0:      # mp4 的 nb_frames 常常压根不写（缺字段，不是 0）：按时长折算
        got = int(round(float(info.get("duration") or 0.0) * fps))
    if abs(got - planned_frames) > 1:
        raise MediaError(
            f"闸③ {sid}：计划 {planned_frames} 帧（{planned_frames / fps:.2f}s@{fps}fps），"
            f"切片实测 {got} 帧，差 {abs(got - planned_frames)} 帧 > 1——不截不补，"
            "这一镜退回。先查是不是 durationInFrames 被镜头代码自己改了（母版不该有这权力）")
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if (w, h) != tuple(expected_size):
        raise MediaError(
            f"闸③ {sid}：成片尺寸 {w}×{h} 与分镜账 {expected_size[0]}×{expected_size[1]} "
            "不一致——拼接会把不同尺寸的切片一起缩放，画面会糊，这里先拦下")


def _frame_diff_stats(clip: Path, at_sec: float, *, bg_hex: str,
                      size: tuple[int, int], out: Path,
                      band: tuple[int, int, int, int] | None = None
                      ) -> tuple[float, float]:
    """一帧相对母版底色的 → (非底色像素占比, 内容外接盒短边 / 取样区短边)。

    全程 ffmpeg，不引图像库依赖。思路：把该帧与一块纯底色逐通道求差 → 转灰度 →
    按 EMPTY_DELTA 二值化 → 整帧落成 gray rawvideo（`w*h` 个字节，每个非零字节就是
    一个非底色像素）→ Python 数非零字节，并在**同一份字节**上扫出行列外接盒。

    两半在同一次读取里算完是刻意的：多一趟 ffmpeg 就是每镜多几秒，而它们读的是同一帧
    的同一份二值字节——分两次算还会取到两个不同的时刻（`-ss` 落在两帧之间时不保证一致）。
    占比判「有没有画东西」，外接盒判「画的东西铺开了没有」，缺一个都会判错细线条画风
    （见上面 SPAN_MIN 那一段的实测分布）。

    **缩到 1×1 这条路走过、废了**：`scale=1:1:flags=area` 在 1920→1 这种极端比例下会把
    细碎的高亮像素抹成 0（真机 s02 的 roughjs 斜纹圆，逐字节数出来是 4.26%，1×1 那条
    路报 0.00%，判了空画面）。占比是小数而不是均值，就必须逐像素判、逐像素数。

    底色写法必须是 `0xrrggbb`：ffmpeg 的 color 源对 `#f6f1e4` 解析出的是 (241,241,241)
    这块灰，不是母版底色——差得不多，但基准色错了，判的就不是「和这张片的底色比」。

    `band=(x, y, w, h)` 把取样范围裁到母版的**画面带**（镜头代码唯一能画的地方）。
    裁之前分母是整帧，于是**母版自己排的字幕框就够把 2% 这条线迈过去**：横屏那块纸色
    字幕条约 1000×240 像素，占 1920×1080 的 11%，而镜头一行代码都没画也照样绿。判空
    判的是「这一镜画了东西没有」，那就只能在那条带子里数。填充度同理：分母是带子的
    短边，不是整帧短边——横屏的带子只有 732 高，照整帧算会把「铺满带子」读成 68%。
    """
    w, h = int(size[0]), int(size[1])
    cx, cy, cw, ch = 0, 0, w, h
    if band:
        cx, cy, cw, ch = (int(v) for v in band)
    mediaops.ffmpeg("-ss", f"{max(0.0, at_sec):.3f}", "-i", str(clip),
                    "-f", "lavfi", "-i", f"color=c=0x{bg_hex.lstrip('#')}:s={cw}x{ch}:r=1",
                    "-filter_complex",
                    f"[0:v]format=rgb24,crop={cw}:{ch}:{cx}:{cy},setsar=1[v];"
                    f"[1:v]format=rgb24,setsar=1[b];"
                    "[v][b]blend=all_mode=difference,format=gray,"
                    f"geq=lum='255*gt(lum(X,Y),{EMPTY_DELTA})'[out]",
                    "-map", "[out]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt",
                    "gray", "-y", str(out), timeout=120)
    data = out.read_bytes()
    if len(data) != cw * ch:
        raise MediaError(
            f"闸④ 取样失败：期望 {cw * ch} 字节（{cw}×{ch} 灰度帧），得到 {len(data)}")
    nz = len(data) - data.count(0)
    if not nz:
        return 0.0, 0.0
    rows = [i for i in range(ch) if any(data[i * cw:(i + 1) * cw])]
    # 整列切片而不是隔几行取样：取样会把「只在被跳过的那几行里出现」的列漏掉，
    # 外接盒算窄就等于把铺满判成小气。any() 在字节序列上第一个非零处就停，代价可比。
    cols = [j for j in range(cw) if any(data[j::cw])]
    bw = max(cols) - min(cols) + 1
    bh = rows[-1] - rows[0] + 1
    return nz / (cw * ch), min(bw / cw, bh / ch)


def _band_of(tokens: Mapping[str, Any] | None,
             size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """从母版回读的 token 里取出画面带的盒（`useBody()` 报给镜头代码的同一个数）。

    只信母版报的，不在 Python 侧另算一套几何——那正是这条链要避免的漂移。
    母版没报（旧版 `film-layout@1`）时返回 None，退回整帧口径：判空仍然在跑，
    只是分母偏大，不会把有画面的镜误杀。
    """
    t = tokens or {}
    try:
        bw, bh = int(t["bodyWidth"]), int(t["bodyHeight"])
        x, y = int(t["gutter"]), int(t["topSafe"])
    except (KeyError, TypeError, ValueError):
        return None
    if bw <= 0 or bh <= 0 or x < 0 or y < 0 or x + bw > size[0] or y + bh > size[1]:
        return None
    return x, y, bw, bh


def _bg_of(tokens: Mapping[str, Any] | None) -> str:
    """闸④ 的基准色：**母版回读的实际底色**，不是写死的 #fff。

    读不到就抛：底色是母版 token，读不到说明这一镜连母版都没跑起来，那正是闸④
    该拦的东西之一；拿一个猜的颜色去判空，判的是猜色而不是画面。
    """
    bg = str((tokens or {}).get("bg") or "").strip()
    if not bg.startswith("#") or len(bg) != 7:
        raise MediaError(
            f"闸④ 拿不到母版底色（token 回读里的 bg={bg!r}）——没有基准色就不判空")
    return bg


async def _empty_check(sid: str, clip: Path, *, bg_hex: str, size: tuple[int, int],
                       duration: float, shot_work: Path,
                       band: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    """闸④：三帧取样，判「这一镜到底画了东西没有、画的东西铺开了没有」。

    → ``{ratio, span, note}``。两个读数各自取的方向不一样，是判过的：

    * **占比取最低那一帧**（沿用旧语义）：动画中途空过一帧就是空过，最满那一帧遮不住；
    * **填充度取最满那一帧**：入场动画从中心长出来是这条链的正常写法，15% 那一帧本来就
      小。要判的是「这一镜的构图有没有把带子用起来」，那该看它落定之后的样子。

    两条打回规则（都带镜号，都在这条链只花了几秒的时候判掉，不留到母带）：

    ① 空壳：最低占比 < ``EMPTY_MIN_RATIO`` **且** 最满填充 < ``SPAN_MIN``。
      为什么两半要合起来判：只看像素占比会把**细线条画风**判死——实测手绘斜纹整镜
      像素占比 0.99%、公式字形 1.54%，而它们的构图是铺满带子的（填充 52%~74%）。
      这种画面该过，因为它正是这条链相对旧卡片链的全部价值。
    ② 小气：最满填充 < ``TIGHT_MIN``，**占比够也拦**。一大块实心颜色挤在中间 20%
      像素占比能到 4%，但它不是画面，是贴纸（当年那批横屏镜就是这么漏过去的）。

    低于 ``FILL_TARGET`` **不打回**：60% 是技能正文给的目标值，把它做成硬闸等于把
    「留白多的版式」这一种风格判死，而这条链没有权利替审美定风格。那一档由报告层
    拿 `fill_span` 现算成提醒（见 `motion_code_nodes._result`），两处不各存一份。
    """
    stats = []
    for frac in EMPTY_SAMPLES:
        png = shot_work / f"empty{int(frac * 100)}.raw"
        stats.append(await asyncio.to_thread(
            _frame_diff_stats, clip, duration * frac, bg_hex=bg_hex, size=size,
            out=png, band=band))
    ratios = [s[0] for s in stats]
    low = min(ratios)
    span = max(s[1] for s in stats)
    where = f"{'带内' if band else '整帧'}"
    if low < EMPTY_MIN_RATIO and span < SPAN_MIN:
        raise MediaError(
            f"闸④ {sid}：画面里几乎是空的（{where}三帧非底色像素占比最低 "
            f"{low * 100:.2f}% < {EMPTY_MIN_RATIO * 100:.0f}%，且内容只铺开带短边的 "
            f"{span * 100:.0f}% < {SPAN_MIN * 100:.0f}%）"
            f"——占比 {'、'.join(f'{r * 100:.1f}%' for r in ratios)}。"
            "镜头代码跑起来了但没画出东西（当年卡片链那种「字段写了、这张卡不认」的静默空壳，"
            "在这条链由像素判）。修法：把母题按 useBody() 铺满、线条加粗到 3px 以上、"
            "或给主体块加一层够重的底色（opacity ≥ 0.5）")
    if span < TIGHT_MIN:
        raise MediaError(
            f"闸④ {sid}：内容挤成一团（{where}非底色像素的外接盒只占带短边 "
            f"{span * 100:.0f}% < {TIGHT_MIN * 100:.0f}%，三帧像素占比最低 "
            f"{low * 100:.2f}%）——画了东西，但那一小块不是画面，是贴纸。"
            f"把主体按 useBody() 报回来的那条带子排：短边的 {int(FILL_TARGET * 100)}% 以上，"
            "而不是照手机竖屏的手感在横屏中间画一小簇")
    return {"ratio": low, "span": round(span, 4)}


def check_token_drift(ledger: list[dict[str, Any]]) -> list[str]:
    """闸⑤：跨镜比对母版回读的 token，**容差 0**。→ 错误清单（空 = 通过）。

    不给「差 1 像素算通过」：这些值是母版的常量，真出问题时那 1 像素正是我们要找的
    东西，容差只会把缺陷放过去。

    只比**都读到了**的镜：某镜回读落空（服务端已经在 warnings 里报了）不该让整条片子
    陪葬，但那条落空必须已经在账上——静默跳过才是这个函数最该避免的失败模式。
    """
    errs: list[str] = []
    seen: dict[str, tuple[Any, str]] = {}     # token 名 → (值, 首个镜号)
    for rec in ledger:
        toks = rec.get("tokens") or {}
        sid = str(rec.get("id") or "?")
        if not toks:
            errs.append(f"闸⑤ {sid}：没读到母版 token，这一镜没参与跨镜一致性比对")
            continue
        for name, value in toks.items():
            first = seen.get(name)
            if first is None:
                seen[name] = (value, sid)
            elif first[0] != value:
                errs.append(
                    f"闸⑤：{name} 在 {first[1]} 是 {first[0]!r}、在 {sid} 是 {value!r}"
                    "——母版的常量不该每镜不同")
    return errs


def _same_field_value(ftype: str, expected: Any, reported: Any) -> bool:
    """闸⑥ 的比对口径：`number` 容差 **0**、`color` 归一写法差异、其余严格相等。

    敢给 0 容差是因为注入值与自报值之间**没有任何换算**——`<F>` 的 `value` 由闸①
    第三条规则钉死必须写 `fields.<name>` 本身。于是不等只剩一种原因：画面上用的
    不是我们注入的那份值。归一化两边都走 `cspec.normalize_field_value`（同一份代码），
    否则 `#F6F1E4` 与 `#f6f1e4` 这种写法差异会判出一片假红。
    """
    if ftype == "number":
        try:
            return float(expected) == float(reported)
        except (TypeError, ValueError):
            return False          # 自报的压根不是数（NaN 尺寸那一类塌法的现场）
    if ftype == "color":
        e = cspec.normalize_field_value("color", expected)
        r = cspec.normalize_field_value("color", reported)
        return isinstance(e, str) and isinstance(r, str) and e == r
    if ftype == "bool":
        # 不用 `==`：Python 里 `True == 1`，而 1 是这一镜可能被误报进来的另一种东西
        return isinstance(reported, bool) and expected is reported
    return expected == reported


def check_field_reconcile(sid: str, *, shot: Mapping[str, Any],
                          fields_used: Mapping[str, Any] | None,
                          ) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """闸⑥：母版回读的自报值与注入值逐项对 → (错误清单, 逐字段结论)。

    结论四种，**只有 `mismatch` 判红**：

    - `ok`            值对上，且量到了盒
    - `no_geometry`   值对上，但这一处 `<F>` 量不出盒（包在自定义组件里、0 尺寸元素）
    - `unreported`    这一镜压根没回读到这个字段（一处 `<F>` 都没标，或整行落空）
    - `mismatch`      自报值 ≠ 注入值 —— 「表单改了、画面没跟着改」

    缺盒不判红是 spec 定死的：盒是给「点元素反查字段」用的，不是给改值用的；把缺盒判成红
    会逼模型给每个字段套一层壳，那是为了过闸而画，不是画面需要。**但必须照实记账**——
    前端据此把这些字段落进「无空间命中」列表，不许留一片空白。
    """
    expected = cspec.field_values(shot)
    types = cspec.field_types(shot)
    marks = list((fields_used or {}).get("marks") or [])
    errs: list[str] = []
    checks: dict[str, dict[str, Any]] = {}
    for name, want in expected.items():
        mine = [m for m in marks if str(m.get("name") or "") == name]
        boxed = next((m.get("box") for m in mine if m.get("box")), None)
        if not mine:
            checks[name] = {"status": "unreported", "expected": want,
                            "reported": None, "box": None}
            continue
        bad = [m for m in mine if not _same_field_value(types.get(name, ""), want,
                                                        m.get("value"))]
        if bad:
            got = bad[0].get("value")
            more = f"（这一镜 {len(bad)}/{len(mine)} 处标注对不上）" if len(bad) > 1 else ""
            errs.append(
                f"闸⑥ {sid}：字段 {name} 注入的是 {want!r}、画面上自报的是 {got!r}{more}"
                "——表单改了、画面没跟着改。`<F name=…>` 的 value 必须就是这个字段自己的"
                "那份值，不能过完 interpolate/clamp 之后再标回来（那标的是结果，不是旋钮）")
            checks[name] = {"status": "mismatch", "expected": want, "reported": got,
                            "box": boxed, "marks": len(mine)}
            continue
        checks[name] = {"status": "ok" if boxed else "no_geometry", "expected": want,
                        "reported": mine[0].get("value"), "box": boxed,
                        "marks": len(mine)}
    return errs, checks


def _thresh(value: Any) -> dict[str, Any] | None:
    """判据阈值只认「真的是表」这一种；其余一律 None，让措辞那边明写「未知」。

    宁可回执上出现「判据 未知」，也不许拿一个 Python 侧的默认数字冒充母版的判据——
    那样一来容器换了阈值，账面上却还在按旧值解释读数。
    """
    return dict(value) if isinstance(value, Mapping) else None


def summarize_assets(shot_id: str, gate: Any) -> dict[str, Any]:
    """闸⑬：把容器回传的资产判据收成一镜一份稳定的账。

    三态与闸⑪/闸⑧ 同规，且**必须**在账上分开：

    - `ok`            每一件素材都在盘上、sha16 对得上、且判据全过。
    - `undetermined` 这一镜的投递账看不见（没镜号 / 读不到 manifest），或某一件的格式
                      解不出主色（渐进式 JPEG、隔行 PNG…）。**不许**折成 ok——那正是
                      「未判当通过」这条纪律要防的事。
    - `rejected`     严格说走不到这里：那一档在 `/render-shot` 口就 400 了，本函数
                      只在补跑/旧网络返回这类边缘情况下见到它，见到就如实记下。

    `items` 逐件留 `{name, verdict, sha16, width, height, colors}`：回执要能回答
    「是哪一件、错在哪」，而不只是「这一镜的图有问题」。 """
    if gate is None:
        return {"shot": shot_id, "status": "undetermined", "items": [],
                "reason": "容器没回传资产判据（这一镜的闸⑬ 等于没跑过）"}
    if not isinstance(gate, Mapping):
        return {"shot": shot_id, "status": "undetermined", "items": [],
                "reason": f"资产判据形状不认：{type(gate).__name__}"}
    status = str(gate.get("status") or "")
    if status not in ("ok", "undetermined", "rejected"):
        return {"shot": shot_id, "status": "undetermined", "items": [],
                "reason": f"资产判据的状态不认识：{status!r}"}
    items = gate.get("items")
    # 逐件账在、但不是一列：这份账读不成条目，那「几件、各判成什么」就无从谈起。
    # 此时整份状态再乐观也不许记 ok——`ok` 的意思是「逐件都验过」，而逐件根本读不出来。
    if "items" in gate and not isinstance(items, (list, tuple)):
        return {"shot": shot_id, "status": "undetermined", "items": [],
                "reason": f"逐件账不是一列（items 是 {type(items).__name__}）——"
                          "这一镜的素材没人验过，整份状态再乐观也不算数"}
    kept: list[dict[str, Any]] = []
    for it in (items if isinstance(items, (list, tuple)) else []):
        if not isinstance(it, Mapping):
            continue
        colors = it.get("colors")
        kept.append({
            "name": str(it.get("name") or ""),
            "verdict": str(it.get("verdict") or ""),
            "sha16": str(it.get("actual_sha16") or ""),
            "width": it.get("width"), "height": it.get("height"),
            "format": it.get("format"),
            # 只留判据用到的几个数，不留整份直方图：账要能被人读。
            "colors": ({"distinct": colors.get("distinct"),
                        "top_share": colors.get("top_share")}
                       if isinstance(colors, Mapping) else None),
            "reason": str(it.get("reason") or "")[:300],
        })
    return {"shot": shot_id, "status": status, "items": kept,
            "reason": str(gate.get("reason") or "")[:600] or None}


def summarize_layout(shot_id: str, layout_used: Any) -> dict[str, Any]:
    """闸⑧：把容器回传的版面回读收成一镜一份稳定的账。**只记账，永不拦截**。

    为什么这一路不设红：判据全是「几何猜的」——两处文字盒压在一起、引线一端落在空白，
    在真正的画面上都可能是合法设计（刻意叠字、指向画面外的暗示）。误报的代价是模型与
    用户开始无视整条通道，比漏报一条引线贵得多（见 tokens.ts `LAYOUT_PROBE` 那段）。
    所以这里唯一的产出是读数，红不红由技能正文与模型去看，不由这里判。

    `status` 三档必须分开，因为「没有 findings」有两种完全不同的意思：

    - `missing`   这一镜压根没收到版面行——母版那侧的探针没跑起来（多半是镜像里的母版
                  比这条 Python 链旧）。下游**不许**把它读成「版面干净」。
    - `unscanned` 收到了行但 `scanned=null`：画面带 ref 没挂载，这一帧没扫（`reason` 带着）。
    - `scanned`   真扫了，`findings` 是判据命中的清单（空表＝扫过且干净）。
    """
    if layout_used is None:
        return {"shot": shot_id, "status": "missing", "findings": [], "scanned": None,
                "thresholds": None, "reason": "容器没回传版面行（这一镜的版面等于没扫过）"}
    if not isinstance(layout_used, Mapping):
        return {"shot": shot_id, "status": "missing", "findings": [], "scanned": None,
                "thresholds": None, "reason": f"版面行形状不认：{type(layout_used).__name__}"}
    if isinstance(layout_used.get("findings"), (list, tuple)):
        findings = [f for f in layout_used["findings"] if isinstance(f, Mapping)]
    else:
        # 杂色（数字、字符串、对象）一律按「没读出命中」收：这条链不设红，
        # 拿一条无法解释的形状去猜 finding 只会把「记账」变成「拦截」。
        findings = []
    scanned = layout_used.get("scanned")
    if not isinstance(scanned, Mapping):
        return {"shot": shot_id, "status": "unscanned", "findings": findings,
                "scanned": None,
                "thresholds": _thresh(layout_used.get("thresholds")),
                "reason": str(layout_used.get("reason") or "母版没扫这一帧")}
    return {"shot": shot_id, "status": "scanned", "findings": findings,
            "scanned": {k: scanned.get(k) for k in ("texts", "leaders", "drawn", "dropped")},
            "band": scanned.get("band") if isinstance(scanned.get("band"), Mapping) else None,
            "thresholds": _thresh(layout_used.get("thresholds")),
            "frame": layout_used.get("frame"), "reason": None}


_SIDE_CN = {"left": "左", "right": "右", "top": "上", "bottom": "下"}


def _int_or_none(value: Any) -> int | None:
    """账里的帧号只认真数；`True`/字符串/None 一律拿不到（`bool` 是 `int` 的子类，
    不排掉的话 `True` 会被读成第 1 帧——一条假读数比没有读数更难发现）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def summarize_motion(shot_id: str, motion: Any) -> dict[str, Any]:
    """闸⑭：把容器回传的动效采样收成一镜一份稳定的账。

    三态与闸⑬/闸⑪ 同规：

    - `ok`            像素上数得出 ≥2 段先后变化，且相邻段起点 ≥0.4 s。
    - `rejected`      数不出来——静帧（所有元素第 0 帧就已到位）、一次跳变（所有元素挤在
                      同一段时间里一起变）、或挨得太近。**这一档会退镜**，翻成 `MediaError`
                      的那一步在 `motion_error`。
    - `undetermined`  抽帧失败（ffmpeg 拿不到）或容器太旧（压根没回传这一行）。**不许**折成
                      ok：「这一镜的动效没人判过」与「判过没问题」是两件事。

    容器那边的词是 `reject`，账上按本族的词记 `rejected`（与闸⑬ 同规），免得下游要同时
    认两个拼法。`thresholds` **原样搬运**、不在 Python 侧另写一份判据数字：两处各写一遍
    就会出现「容器改了阈值、回执还按旧值解释读数」（与闸⑦ 的 quota_want 同一条理由）。

    原始差值曲线**不进账**（8 Hz × 20 s 就是一两百个数）：账要能被人读，而红的时候那句
    `reason` 已经把段数、间隔、帧号一并报出来了。这里只留 `events`（逐段起止帧与峰值）
    与 `min_gap_frames`——回执要能回答「它到底数到几段、最紧的一对隔多少」。
    """
    if motion is None:
        return {"shot": shot_id, "status": "undetermined", "events": [], "samples": 0,
                "min_gap_frames": None, "thresholds": None,
                "reason": "容器没回传动效采样行（这一镜的闸⑭ 等于没跑过）"}
    if not isinstance(motion, Mapping):
        return {"shot": shot_id, "status": "undetermined", "events": [], "samples": 0,
                "min_gap_frames": None, "thresholds": None,
                "reason": f"动效采样行形状不认：{type(motion).__name__}"}
    raw_status = str(motion.get("status") or "")
    # 容器写 reject、账上写 rejected：两个拼法都认，其余一律按未判收（不认识的
    # 状态最乐观的读法就是「没人判过」，把它读成 ok 正是这一族最坏的写法）。
    status = {"ok": "ok", "reject": "rejected", "rejected": "rejected",
              "undetermined": "undetermined"}.get(raw_status, "undetermined")
    events: list[dict[str, Any]] = []
    for e in (motion.get("events") if isinstance(motion.get("events"), (list, tuple)) else []):
        if not isinstance(e, Mapping):
            continue
        start = _int_or_none(e.get("start_frame"))
        if start is None:
            continue
        events.append({"start_frame": start, "end_frame": _int_or_none(e.get("end_frame")),
                       "start_sec": e.get("start_sec"), "peak_diff": e.get("peak_diff")})
    gaps = [b["start_frame"] - a["start_frame"]
            for a, b in zip(events, events[1:]) if isinstance(a["start_frame"], int)]
    samples = _int_or_none(motion.get("samples")) or 0
    out = {"shot": shot_id, "status": status, "events": events, "samples": samples,
           "sample_hz": motion.get("sample_hz"), "min_gap_frames": min(gaps) if gaps else None,
           "thresholds": _thresh(motion.get("thresholds")),
           "reason": (str(motion.get("reason"))[:600] or None)
                     if motion.get("reason") else None}
    if status == "undetermined" and not out["reason"]:
        out["reason"] = f"容器报了个不认识的状态：{raw_status!r}"
    # 两份证据打架：整份状态写 ok，逐段账却数不出阈值要求的那几段。**以细的那份为准**，
    # 降成未判而不是跟着 ok 走——`ok` 的含义是「判过且过」，而这里恰恰是判据自己
    # 说不圆。阈值取容器回传的那份（本函数不持有第二个拷贝），拿不到就不做这项核对。
    want = (out["thresholds"] or {}).get("min_events")
    if status == "ok" and isinstance(want, int) and not isinstance(want, bool) \
            and len(events) < want:
        out["status"] = "undetermined"
        out["reason"] = (f"容器判 ok，但账上只有 {len(events)} 段（它自己报的判据要 "
                         f"≥{want} 段）——两份证据打架时以细的那份为准，这一镜不算判过")
    return out


def motion_error(shot_id: str, motion: Mapping[str, Any] | None) -> str | None:
    """账 → 「这一镜该退」的一句人话；通过或未判返回 `None`。

    未判在这里**不退出**（那时判据压根没跑成），但它照样不是通过——`describe_motion`
    会把它单独列一行，报告层按未判计。这一闸要拦的是「有动效但看不出动」，
    不是「容器里的 ffmpeg 不见了」：把后者也判成片的罪，等于让工具故障冒充内容缺陷。
    """
    if not isinstance(motion, Mapping) or str(motion.get("status")) != "rejected":
        return None
    th = motion.get("thresholds") if isinstance(motion.get("thresholds"), Mapping) else {}
    want, gap_sec = th.get("min_events"), th.get("min_gap_sec")
    evs = [e for e in (motion.get("events") or [])
           if isinstance(e, Mapping) and isinstance(e.get("start_frame"), int)]
    # 「数到 1 段」这种拒法（静帧除外）最要紧的是**它把那一段量在了哪几帧上**：
    # 只报段数，模型与人都没法判断是「三件齐发」还是「隔得太近」，而这两者的改法完全不同。
    span = ""
    if evs:
        end = evs[-1].get("end_frame")
        span = f"，跨帧 {evs[0]['start_frame']}–{end if isinstance(end, int) else '?'}"
    tight = motion.get("min_gap_frames")
    tight_txt = f"（最紧的一对的起点隔 {tight} 帧）" if isinstance(tight, int) else ""
    return (f"闸⑭ 动效事件密度未通过，{shot_id} 退回（切片没进缓存、母带没开始）：\n"
            f"  · {motion.get('reason') or '容器没给出理由'}\n"
            f"  · 判据：每镜 ≥{want if isinstance(want, int) else '?'} 段先后变化、"
            f"相邻段的起点 ≥{gap_sec if gap_sec is not None else '?'} s；"
            f"这一镜数到 {len(evs)} 段{span}{tight_txt}")


def _num(value: Any) -> str:
    """读数里的数字 → 两位小数；杂色写成「拿不到读数」。

    这条链的告警是给模型看的，一句没法核对的话比一句粗糙的话更糟，而**为了排版好看
    在这里抛**更糟——那一刻这一镜的像素已经烧完了。
    """
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "拿不到读数"


def describe_assets(shot_id: str, assets: Mapping[str, Any] | None) -> list[str]:
    """一镜的资产判据 → 逐条人话（整片账与回执都用这一份措辞，别处不另抄）。

    `ok` 不吭声——那是「判过且过」，与版面那条的「扫过且干净」同理：正常的一镜
    不该在回执里占一行。而 `undetermined` **必须**出一条，且要说明是哪几件、为什么
    没判：那一档的意思是「这件素材没人验过」，写成沉默就等于把它读成通过。
    同理，整份状态写 `ok` 而逐件账里有点不是 pass 的（两份证据打架）也要出声，
    以逐件为准——这条比版面那侧严一档，因为资产判据是机器判的、不存在「几何猜错了」
    的余地：一份自相矛盾的账只可能是账坏了。
    """
    if not isinstance(assets, Mapping):
        # 账整个缺失（容器没回传 / 回传了杂色）：这不是「pass」，是「没人验过」。
        what = "容器没回传资产判据" if assets is None else f"资产账形状不认：{type(assets).__name__}"
        return [f"闸⑬ {shot_id}：资产账缺失（{what}）——这一镜的素材没人验过，"
                "不许当成「图上没问题」"]
    a = assets
    st = str(a.get("status") or "undetermined")
    items = a.get("items") if isinstance(a.get("items"), (list, tuple)) else []
    bad = [i for i in items if isinstance(i, Mapping) and i.get("verdict") != "pass"]
    if st == "ok" and not bad:
        return []
    who = "；".join(f"{i.get('name') or '?'}（{i.get('reason') or i.get('verdict') or '?'}）"
                    for i in bad) or str(a.get("reason") or "没给出理由")
    if st == "rejected":
        return [f"闸⑬ {shot_id}：文案里用了图，但图上不了屏——{who}"]
    if st == "ok":
        # 整份状态写 ok、逐件账里却有点不是 pass 的：两份证据打架。**以更细的那份为准**
        # 并出声——按整份状态沉默，就等于把一件没验过的素材读成通过，而这一栏存在的
        # 全部理由就是不让这件事发生。
        return [f"闸⑬ {shot_id}：整份状态写 ok，逐件账里却有 {len(bad)} 件不是 pass（{who}）——"
                "两份证据打架时以逐件为准，这一镜的图不算验过"]
    return [f"闸⑬ {shot_id}：{len(bad) or '有'} 件素材没判（{who}）——"
            "「没判」不等于「没问题」，这一镜的画面主体等于没人验过"]


def describe_layout(shot_id: str, layout: Mapping[str, Any] | None) -> list[str]:
    """一镜的版面读数 → 逐条人话（整片账与回执都用这一份措辞，别处不另抄）。

    `status != scanned` 时**必须**出一条：那不等于「没问题」，等于「这一镜的版面没验过」，
    而回执上把没验过写成干净是这条链最不该犯的那种错。
    """
    lay = layout if isinstance(layout, Mapping) else {}
    st = str(lay.get("status") or "missing")
    if st == "missing":
        return [f"闸⑧ {shot_id}：版面回读落空（{lay.get('reason') or '没收到版面行'}）——"
                "这一镜的版面等于没扫过，不许当成「扫过且干净」"]
    if st == "unscanned":
        return [f"闸⑧ {shot_id}：这一帧没扫版面（{lay.get('reason') or '母版没扫'}）"]
    th = lay.get("thresholds") if isinstance(lay.get("thresholds"), Mapping) else {}
    out: list[str] = []
    fs = lay.get("findings") if isinstance(lay.get("findings"), (list, tuple)) else []
    for f in fs:
        if not isinstance(f, Mapping):
            continue
        ts = f.get("texts") if isinstance(f.get("texts"), (list, tuple)) else []
        who = "、".join(f"「{str(t)[:16]}」" for t in ts)
        kind = str(f.get("kind") or "")
        if kind == "overlap":
            out.append(f"闸⑧ {shot_id}：{who} 压在一起（重叠 "
                       f"{_num(f.get('ratio'))}，判据 "
                       f"{th.get('overlap_ratio', '未知')}）")
        elif kind == "clipped":
            out.append(f"闸⑧ {shot_id}：{who} 被画面带"
                       f"{_SIDE_CN.get(str(f.get('side')), f.get('side'))}边缘切掉约 "
                       f"{f.get('px')}px（判据 {th.get('clip_px', '未知')}px）")
        elif kind == "dangling":
            out.append(f"闸⑧ {shot_id}：{who} 的引线另一端落在空白处"
                       f"（带内坐标 {f.get('at')}）")
        else:
            out.append(f"闸⑧ {shot_id}：{kind} {who}")
    sc = lay.get("scanned") if isinstance(lay.get("scanned"), Mapping) else {}
    dropped = int(sc.get("dropped") or 0) if isinstance(sc.get("dropped"), (int, float)) else 0
    if dropped > 0:
        out.append(f"闸⑧ {shot_id}：单帧 finding 超上限，丢了 {dropped} 条"
                   f"（上限 {th.get('max_findings', '未知')}）")
    return out


def describe_motion(shot_id: str, motion: Mapping[str, Any] | None) -> list[str]:
    """一镜的动效读数 → 逐条人话（整片账与回执都用这一份措辞，别处不另抄）。

    `ok` 不吭声（判过且过的一镜不该在回执里占一行）。`rejected` 与 `undetermined`
    **各出一条**，而且说法必须不同：前者的意思是「这一镜的像素上确实看不出先后动」，
    后者是「这一镜压根没判过」。把后者写成沉默或写成前者的语气，都是拿未判当通过。
    """
    m = motion if isinstance(motion, Mapping) else {}
    st = str(m.get("status") or "undetermined")
    if st == "ok":
        return []
    th = m.get("thresholds") if isinstance(m.get("thresholds"), Mapping) else {}
    want = th.get("min_events", "未知")
    gap = th.get("min_gap_sec", "未知")
    got = len(m.get("events") or []) if isinstance(m.get("events"), (list, tuple)) else "?"
    tight = m.get("min_gap_frames")
    if st == "rejected":
        return [f"闸⑭ {shot_id}：像素上数不出「先后动」——{m.get('reason') or '容器没给出理由'}"
                f"（数到 {got} 段、最紧一对隔 {tight if tight is not None else '—'} 帧，"
                f"判据 ≥{want} 段、相邻起点 ≥{gap} s）。这一镜已经带镜号退回，没有进成片"]
    return [f"闸⑭ {shot_id}：动效没人判过（{m.get('reason') or '容器没回传采样行'}）——"
            "「没判」不等于「看得出动」，这一镜的动效读数在整片账上算未判"]


def shot_input_props(shot: Mapping[str, Any], spec: Mapping[str, Any], *,
                     frames: int, frame_words: list[dict[str, Any]]) -> dict[str, Any]:
    """`/render-shot` 的 `input_props`：母版要的排班全在这里，镜头代码一样也拿不到。

    `durationInFrames` 是闸② 的注入点（服务端见到 0 直接 400）；`words` 用帧刻度，
    与同一份 fps 换算而来，字幕点亮与语音才对得上。

    `fields` 走 `cspec.field_values` 那一份扁表：与缓存键里的、闸⑥ 拿来当期望值的**是
    同一段代码算出的同一批字节**。两处各算一遍的话，「注入的是 41、账上期望的是 41.0」
    这种差异会让闸⑥ 稳定判红，而画面其实一张都没变。

    R7-T22：入场/出场按**帧数**注入（`entranceFrames`/`exitFrames`），窗口的算术在
    Python 侧（`cspec.shot_motion_window`）——母版只负责按这两个数插值。为什么要这么切：
    窗口上限（这一镜的 10%）与闸④ 的采样点（0.15/0.5/0.85）是同一条算术关系，
    在 TS 里再算一遍就有了第二个出处，将来谁把上限放宽一点，闸④ 会先在真机上开始误报
    空壳，而没有人会想到去看母版那行代码。
    """
    window = cspec.shot_motion_window(frames)
    return {
        # 镜号进 props 只为一件事：渲染侧据此把 `publicDir` 指到
        # `public/assets/<sid>/`（§4-5）。它**不进缓存键**——键是按代码字节 + 旋钮 +
        # 整片开关 + assets 的 sha/name 对算的（`shot_cache_key`），与镜号无关；
        # 两镜交出同一段代码本就该共用一份切片。所以它也不能反过来影响画面：
        # 母版读不到它就当没有。
        "shotId": str(shot.get("id") or ""),
        "fps": int(spec["fps"]),
        "durationInFrames": int(frames),
        "aspect": str(spec["aspect"]),
        "width": int(spec["width"]),
        "height": int(spec["height"]),
        "text": str(shot.get("text") or ""),
        "highlight": [str(h) for h in (shot.get("highlight") or [])],
        "words": frame_words,
        "subtitleMode": str(spec.get("subtitle_mode") or "full"),
        "fields": cspec.field_values(shot),
        # 母版进出场（T22）：档名 + 窗口帧数。`none` 或者窗口不足 2 帧时是 0/0，
        # 母版据此完全不套那层动画（于是「不选进出场」的片子逐帧与从前一致）。
        "entrance": str(spec.get("entrance") or "none"),
        "exit": str(spec.get("exit") or "none"),
        "entranceFrames": window,
        "exitFrames": window,
    }


async def _tail_frame(clip: Path, dst: Path, *, fps: int) -> None:
    """取切片**末段**那一帧（动画落定、字幕停在整句），P3 的对比帧与缩略图都读它。

    `-sseof -0.1` 走过、废了：带声切片的视频流常比容器时长短一截（`-shortest` 按音频
    切，最后一帧的时刻落在目标点之前），这种 seek 失败 ffmpeg **退 0 且不写文件**——
    于是 frame.png 静默缺失，而调用方的 `cache.store` 要读它：FileNotFoundError 被
    except 吞成账上一句 cache_stored，结果是**整条链的切片缓存永不命中**，「局部改只
    重烧受影响镜」在这条链悄悄退化成每镜重烧。所以：按切片自己的帧数取流内的一帧，
    取不到就当场抛，绝不留静默缺失。

    这一帧也是母版回读字段盒的那一帧（`durationInFrames - PROBE_TAIL`，两边同一个
    常量），命中表里的盒与这张图说的是同一时刻，前端把盒叠在代表帧上才不错位。
    """
    clip_info = await asyncio.to_thread(mediaops.probe, clip)
    cfps = float(clip_info.get("fps") or fps) or float(fps)
    vframes = int(clip_info.get("frames") or 0) or int(round(
        float(clip_info.get("duration") or 0.0) * cfps))
    at = max(0.0, (vframes - PROBE_TAIL) / cfps) if vframes > PROBE_TAIL else 0.0
    await asyncio.to_thread(
        lambda: mediaops.ffmpeg("-ss", f"{at:.3f}", "-i", str(clip), "-frames:v", "1",
                                str(dst), timeout=120))
    if not dst.exists() or dst.stat().st_size == 0:
        raise MediaError(
            f"{clip.name}：代表帧没取出来（-ss {at:.3f}，切片视频 {vframes} 帧 "
            f"@{cfps}fps）——缩略图与切片缓存都读它；这一处静默缺失会把整条链的缓存"
            "拖成永不命中，所以宁可这一镜退回")


async def _shot_texture(clip: Path, dst_dir: Path, *, shot: Mapping[str, Any],
                        spec: Mapping[str, Any], sid: str,
                        frame: Path) -> tuple[Path, str, str, Path]:
    """质感滤镜跑在**这一镜的切片上**、拼接之前。

    → (交给拼接的切片, 这一镜的档名, 实际消费的滤镜串, 代表帧)。

    为什么在缓存之后：切片缓存存的一直是**没加质感**的像素（``SHOT_KNOBS``/
    ``FILM_KNOBS`` 都不收 texture），所以「整片从 film 换 tv」与「只换这一镜」都烧不到
    任何 Remotion 帧——代价只是这一镜再过一趟 ffmpeg，而它本来在母带那趟也要再过一次。
    档名一律走 :func:`cspec.shot_texture`：记账与渲染各写一遍「镜头有就用、没有问整片」
    的话，就会出现账面写 film、画上是原片。
    """
    kind = cspec.shot_texture(shot, spec)
    if kind == "none":
        return clip, kind, "", frame
    out_clip = dst_dir / "clip_tex.mp4"
    chain = await asyncio.to_thread(rpipe.apply_texture, clip, out_clip, kind)
    if not chain:
        raise MediaError(
            f"{sid}：质感选的是 {kind}，却没拿到滤镜串——交付给拼接的还是原片。"
            "请核对 render.TEXTURE_FILTERS 里有没有这一档（宁可这一镜退回，"
            "也不出一版「账上加了、画面没加」的片子）")
    out_frame = dst_dir / "frame_tex.png"
    await _tail_frame(out_clip, out_frame, fps=int(spec["fps"]))
    return out_clip, kind, chain, out_frame


async def _stage_shot_assets(shot: Mapping[str, Any], *, sid: str,
                             storage: Any, client: dict[str, Any]) -> list[dict[str, Any]]:
    """渲染前把本镜 assets 的字节投到渲染容器 `public/assets/<sid>/` 下，并留一份 manifest。

    链路（§4-5「投递方向唯一」）：shot.assets[].sha256 → asset_library 表查 object_key →
    MinIO 取字节 → **本机先算一遍 sha16** → `POST /stage-assets`（服务端再按字节算一次，
    对不上直接 400）→ 落 `public/assets/<sid>/<name>` → 投完写
    `public/assets/<sid>/_manifest.json`。

    **为什么是 `<sid>/` 而不是平铺在 `public/` 下**：两镜各要一张 `chart.png`、内容不同，
    平铺时后投的那张会把先投的覆盖掉（或恰好相反——服务端的幂等短路看见文件在就不写，
    于是**第二镜静默拿到第一镜的字节**）。按镜号分目录之后，同名不同内容的素材互不相干；
    渲染侧也据此把这一镜的 `publicDir` 指到 `public/assets/<sid>`，`staticFile("<name>")`
    才落得进这一镜自己的那份字节。

    **三处 sha16 必须一致**（申报 / 落盘 / 闸⑬ 回读），所以这里在投之前先按库里的字节
    自己算一遍：库行被换过、或对象被同名覆盖过，都在这里红，而不是等画面上出现一张
    跟声明对不上的图。

    没声明 assets 的镜头直接过。asset_library 里查不到的 sha256 抛错——
    模型填了一个素材库里没有的 sha256，渲出来就是空白，不如在这里就红。
    """
    assets = shot.get("assets") or []
    if not assets:
        return []
    stage = client.get("stage_asset")
    if stage is None:
        raise MediaError(f"{sid}：声明了 {len(assets)} 个素材但渲染客户端没有 stage_asset 口")
    items: list[dict[str, Any]] = []
    for a in assets:
        sha256 = str(a.get("sha256") or "").strip().lower()
        name = str(a.get("name") or "").strip()
        if not sha256 or not name:
            continue          # _norm_shot 已经报过错了，这里不重复报
        row = await storage.assets.get_by_sha(sha256)
        if row is None:
            raise MediaError(
                f"{sid}：素材 sha256={sha256[:16]}… 在 asset_library 里查不到——"
                "模型填了一个素材库里没有的素材，请先入库或去掉这条 asset 声明")
        object_key = str(row.get("object_key") or "")
        if not object_key:
            raise MediaError(f"{sid}：素材 {name} 的 asset_library 行没有 object_key")
        chunks = storage.objects.get_stream(object_key)
        buf = bytearray()
        async for chunk in chunks:
            buf.extend(chunk)
        raw_bytes = bytes(buf)
        sha16 = sha256[:16]
        real16 = hashlib.sha256(raw_bytes).hexdigest()[:16]
        if real16 != sha16:
            raise MediaError(
                f"{sid}：素材 {name} 的库内字节与登记的 sha256 对不上（登记 {sha16}…，"
                f"实算 {real16}…）——库行被换过或对象被同名覆盖过，先重新入库再来。"
                "带着这份字节去渲染，画面上出现的跟账上写的会是两张图")
        await asyncio.to_thread(stage, f"assets/{sid}/{name}", sha16,
                                base64.b64encode(raw_bytes).decode("ascii"))
        items.append({"name": name, "sha16": sha16, "bytes": len(raw_bytes),
                      "ext": str(a.get("ext") or ""),
                      # 这张素材自己是动图还是静图。三档（animated/static/undetermined）
                      # 由闸⑪ 那一路回读填写，这里拿不到就如实写 undetermined——
                      # 编一个 static 会让闸⑪ 的「动图当静图用」判据永远绿。
                      "motion": "undetermined"})
    # manifest 也走同一个口投，不复用别的写入通道：它是「这一镜**真投成功了什么**」的
    # 记录，而 lint 期要拿它判「声明了但没投递」。跟素材一起走同一条腿，两边的失败方式
    # 才一致（投递腿断了，manifest 也不会凭空出现在容器里）。
    blob = json.dumps({"sid": sid, "items": items},
                      ensure_ascii=False, sort_keys=True).encode("utf-8")
    await asyncio.to_thread(stage, f"assets/{sid}/_manifest.json",
                            hashlib.sha256(blob).hexdigest()[:16],
                            base64.b64encode(blob).decode("ascii"))
    return items


async def render_code_shot(shot: Mapping[str, Any], *, spec: Mapping[str, Any],
                           providers: Providers, work: Path, index: int,
                           frames_used: int, identities: Mapping[str, str],
                           client: dict[str, Any],
                           storage: Any = None,
                           cache: "rpipe.ShotCache | None" = None,
                           progress: Progress | None = None) -> dict[str, Any]:
    """一镜（代码）→ 带声切片 + 记账。八道闸里属于这一镜的那几道都在这条路上。

    判红的是 ②（帧预算，进来之前）③（帧账）④（画面判两半）⑥（字段对账）；① 判在容器侧、
    这一层只把它的错误原文搬回来，⑤ 比的是**跨镜**所以落在拼接前，⑧ 只记账不设红。

    `identities` 是 /healthz 现报的 `master_hash` / `lockfile_hash`，缓存键要它们。
    """
    fps = int(spec["fps"])
    size = (int(spec["width"]), int(spec["height"]))
    sid = str(shot.get("id") or f"s{index + 1:02d}")
    shot_work = work / f"shot{index:02d}"
    shot_work.mkdir(parents=True, exist_ok=True)

    key = cspec.shot_cache_key(dict(shot), film=spec,
                               master_hash=str(identities["master_hash"]),
                               lockfile_hash=str(identities["lockfile_hash"]))
    if cache is not None:
        got = await cache.fetch(key, shot_work)
        if got:
            try:
                meta = json.loads(got["meta.json"].read_text(encoding="utf-8"))
                hits = json.loads(got["hitmap.json"].read_text(encoding="utf-8"))
            except (KeyError, OSError, ValueError):
                meta = None            # 读不成不是事故：这一镜照实重渲一遍
            if meta and all(k in meta for k in META_FIELDS):
                rec = {k: v for k, v in meta.items() if k in META_FIELDS}
                # 命中缓存也要过闸⑭：判据的阈值（段数、间隔）**不在缓存键里**，
                # 所以缓存里的那一镜只证明「按当时的判据它是绿的」。阈值一改，
                # 复用切片的那几镜就会带着旧判据的读数直接进母带——这里补一刀，
                # 拒因与现渲的那一路同源（同一个 `motion_error`）。
                cached_reject = motion_error(sid, rec.get("motion"))
                if cached_reject:
                    raise MediaError(f"{cached_reject}（这一镜来自缓存，按当前判据重判不过）")
                # 命中缓存**不等于**这一镜可以跳过滤镜：缓存里的那一镜是没加质感的像素，
                # 质感在缓存之后按镜补。跳过它就成了「整片加了质感、命中缓存的那几镜是
                # 原片」，而同一版片子里两种像素并存是没人看得见的错（闸⑤ 只管数值一致）。
                clip, kind, chain, frame = await _shot_texture(
                    got["clip.mp4"], shot_work, shot=shot, spec=spec, sid=sid,
                    frame=got["frame.png"])
                rec.update({"id": sid, "clip": clip, "hits": hits,
                            "cached": True, "frame": frame, "chain": "remotion",
                            "texture": kind, "texture_filter": chain})
                return rec

    narr = await narration_for(shot, spec, providers, shot_work)
    sec, frames = float(narr["sec"]), int(narr["frames"])
    frame_budget = rpipe.max_frames_total(
        fps, spec.get("target_duration_sec"))
    if frames_used + frames > frame_budget:
        raise MediaError(
            f"闸② 帧预算用尽：到 {sid} 需要 {frames_used + frames} 帧，上限 "
            f"{frame_budget}。降 fps（{fps}→10）或缩短片子再来——"
            "不静默砍镜，也不静默截帧")

    props = shot_input_props(shot, spec, frames=frames,
                             frame_words=_to_frame_words(narr["timed"], fps))
    if storage is not None:
        await _stage_shot_assets(shot, sid=sid, storage=storage, client=client)
    resp = await _await_heartbeat(
        progress, sid=sid, index=index, total=len(spec.get("shots") or []),
        awaitable=asyncio.to_thread(
            client["render"], str(shot.get("code") or ""), key, props,
            None, shot.get("assets") or []))
    await _emit(progress, {"stage": "rendered", "shot": sid, "done": index + 1,
                           "total": len(spec.get("shots") or []),
                           "cache_key": key, "frames": frames})

    raw = shot_work / "silent.mp4"
    art = str(resp.get("artifact") or f"/artifact/{key}")
    got_bytes = await asyncio.to_thread(client["fetch"], art, raw)
    info = await asyncio.to_thread(mediaops.probe, raw)
    _check_frame_account(sid, planned_frames=frames, fps=fps, info=info,
                         expected_size=size)
    # 闸⑥ 排在闸④ 之前：它只是比对这一次响应里已经带回来的数据，一次 ffmpeg 都不开。
    # 值对不上时没必要再花几秒去判这张片的空不空——反正这一镜一定要退回。
    field_errs, field_checks = check_field_reconcile(
        sid, shot=shot, fields_used=resp.get("fields_used"))
    if field_errs:
        raise MediaError(
            f"闸⑥ 字段级对账未通过，{sid} 退回（切片没进缓存、母带没开始）：\n"
            + "\n".join(f"  · {e}" for e in field_errs[:10]))
    # 闸⑭ 排在闸④ 之前，与闸⑥ 同一条理由：读数就在这次响应里，一次 ffmpeg 都不开。
    # **必须在切片进缓存之前判**——退回去的一镜不该在缓存里留一份「动效不合格」的切片，
    # 否则下一次同一个 cache_key 命中缓存、从缓存那一路进来（那一路只认读数不重判内容）。
    motion = summarize_motion(sid, resp.get("motion"))
    motion_err = motion_error(sid, motion)
    if motion_err:
        raise MediaError(motion_err)
    tokens = resp.get("tokens") or None
    fill = await _empty_check(sid, raw, bg_hex=_bg_of(tokens),
                              size=size, duration=float(info["duration"]),
                              shot_work=shot_work, band=_band_of(tokens, size))

    clip = shot_work / "clip.mp4"
    # 哑片 + 旁白 → 带声切片（视频流直接 copy：切片已经编过一次，再编一遍只掉画质）
    await asyncio.to_thread(
        lambda: mediaops.ffmpeg("-i", str(raw), "-i", str(narr["voice"]),
                                "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                                "-shortest", str(clip), timeout=300))

    frame = shot_work / "frame.png"
    await _tail_frame(clip, frame, fps=fps)

    rec: dict[str, Any] = {
        "id": sid, "clip": clip, "frame": frame,
        "sec": sec, "speech_sec": round(float(narr["speech"]), 3),
        "planned_frames": frames, "frames": int(info.get("frames") or frames),
        "words": len(narr["timed"]), "degraded": narr["degrade"],
        "measured_sec": info["duration"], "width": info["width"],
        "height": info["height"], "fps": info["fps"],
        "empty_ratio": round(float(fill["ratio"]), 4),
        # 只存读数，不存 `_empty_check` 那句提醒：提醒由报告层按 FILL_TARGET 现算，
        # 否则缓存镜（meta 里没那一栏）与现渲镜的账面形状就不一样了。
        "fill_span": fill["span"],
        "tokens": tokens,
        "field_checks": field_checks,
        # 闸⑧ 的版面读数：与 field_checks 同一次浏览器回读、同一探针帧，也一同进缓存
        # （缺了这一栏，复用切片的那几镜在整片账上会被读成「版面扫过且干净」）。
        "layout": summarize_layout(sid, resp.get("layout_used")),
        # 闸⑪ 资产动效三态：'static'（打回）/ 'animated'（通过）/ 'undetermined'（未判）。
        # 未判不许记成通过——回执必须如实区分。
        "asset_motion": resp.get("asset_motion") or {"status": "undetermined", "reason": "容器没回传资产行"},
        # 闸⑬ 资产存在且非纯色：逐件回执（每件的 sha16 / 尺寸 / 主色档数 + 三态）。
        # 拒的那一档在渲染口就 400 了、走不到这里；能进来的只有 ok / undetermined，
        # 而 undetermined **必须**原样留档——把它折成「没问题」正是这个字段存在的理由。
        "assets": summarize_assets(sid, resp.get("assets_gate")),
        # 闸⑭ 动效事件密度：上面那一刀只拦 rejected；能走到这里的是 ok 或 undetermined，
        # 后者的意思是「这一镜的动效没人判过」，必须原样留档（未判不许折成通过）。
        "motion": motion,
        "render_timings": resp.get("timings") or {}, "cache_key": key,
        "chain": "remotion",
        "lint_warnings": [str(w)[:200] for w in (resp.get("warnings") or [])][:8],
        "bytes": int(got_bytes),
        "hits": _shot_table(sid, index, size, shot=shot, checks=field_checks),
    }
    if cache is not None and narr["degrade"] is None:
        meta = {k: rec[k] for k in META_FIELDS}
        (shot_work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                             encoding="utf-8")
        (shot_work / "hitmap.json").write_text(json.dumps(rec["hits"], ensure_ascii=False),
                                               encoding="utf-8")
        try:
            # 存进缓存的是**加质感之前**的切片与代表帧：texture 不在缓存键里，
            # 靠这一条把「换质感」与「重烧镜头」解耦——存了带质感的像素，换档就会
            # 拿上一档的片子冒充这一档（与「少算一个键就是拿旧片冒充新片」同族）。
            await cache.store(key, {"clip.mp4": clip, "frame.png": frame,
                                    "meta.json": shot_work / "meta.json",
                                    "hitmap.json": shot_work / "hitmap.json"})
        except Exception as exc:        # noqa: BLE001 - 存不成只影响下次要不要重烧
            rec["cache_stored"] = f"没存进缓存：{type(exc).__name__}: {str(exc)[:120]}"

    clip, kind, chain, frame = await _shot_texture(clip, shot_work, shot=shot,
                                                   spec=spec, sid=sid, frame=frame)
    rec.update({"clip": clip, "frame": frame, "texture": kind, "texture_filter": chain})
    return rec


def _shot_table(sid: str, index: int, size: tuple[int, int], *,
                shot: Mapping[str, Any] | None = None,
                checks: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """这一镜的命中表：整镜一条 + **每个声明出来的字段一条**（§12 的字段级 pointer）。

    整镜那条为什么还留着：前端的轨道与框选只认「第 i 镜 = 画面上的哪一块」，这条链整镜
    就是一块的底色——但**必须**是数据而不是代码里的假设，否则编辑器要反过来猜这条链
    有没有命中层。

    字段条目的两半各有各的出处，这是这张表存在的全部意义：
      * `pointer` 来自**分镜契约**（`/shots/i/fields/{name}/value`，改的是数据不是代码）；
      * `box` 来自**渲染那一刻的 DOM**（母版在探针帧量回来的，见 `check_field_reconcile`）。
    量不到盒的字段**照旧出一条**，`box=null` + `field_status` 说明为什么——前端把这些
    落进「无空间命中」列表（口播链 T5 那一档形态），不许因为量不到就不给用户入口。

    盒写成**成片像素**（`coord="output_px"`），不在这里归一化：母版量的是成片那一帧，
    前端叠的也是这一帧，中间每多一次换算就多一处会漂的地方。表里带着 width/height，
    要归一的自己除。

    字段名不做 JSON pointer 转义是**判过的**：`code_spec.FIELD_NAME_RE` 只允许
    `[A-Za-z_$][A-Za-z0-9_$]*`，`/` 与 `~` 进不来，而这两个字符正是转义要处理的全部
    （见 `hitmap._seg`）。这一镜的 name 若来自更早的契约，那份校验已经先拦了。
    """
    entries: list[dict[str, Any]] = [
        {"kind": "shot", "label": "整镜画面", "box": [0, 0, size[0], size[1]],
         "pointer": f"/shots/{index}/code", "fields": ["code", "text", "source"]},
    ]
    for f in ((shot or {}).get("fields") or []):
        name = str(f.get("name") or "")
        c = (checks or {}).get(name) or {}
        box = c.get("box") or None
        entries.append({
            "kind": "field", "name": name, "label": f.get("label"),
            "type": f.get("type"), "unit": f.get("unit") or "",
            # 控件的范围跟着条目走（spec §6：〈字段〉按表吐出去的 fields schema 自动出控件）。
            # bundle 里虽然也嵌了整份 spec，但让前端去那里啃 min/max 等于设了第二个出处——
            # 与 #127「补丁口把字段类型也发出去」是同一条纪律。声明里没给的发 null，
            # 前端据此把滑杆退成纯数字框，而不是自己编一个上界。
            "min": f.get("min"), "max": f.get("max"), "step": f.get("step"),
            "options": list(f.get("options") or []) or None,
            # value 取**注入的那份**（闸⑥ 已确认画面上就是它）；回读值另存一份，
            # 两边都进表，界面对不上时一眼能看出是哪一侧漂了。
            "value": c.get("expected", f.get("value")),
            "reported": c.get("reported"),
            "field_status": c.get("status") or "unreported",
            "box": ([float(box["x"]), float(box["y"]), float(box["w"]), float(box["h"])]
                    if isinstance(box, dict) else None),
            "pointer": f"/shots/{index}/fields/{name}/value",
        })
    return {"schema": "code-shot-table/2", "id": sid, "index": index,
            "width": size[0], "height": size[1], "coord": "output_px",
            "hit_mode": "space",
            "editable_pointers": list(cspec.EDITABLE_POINTERS),
            "entries": entries}


async def render_code_motion(spec: dict[str, Any], *, providers: Providers, work: Path,
                             client: dict[str, Any] | None = None,
                             storage: Any = None,
                             bgm: Path | None = None,
                             progress: Progress | None = None,
                             cache: "rpipe.ShotCache | None" = None) -> dict[str, Any]:
    """整条链：代码分镜 spec → 成片。质感在**每一镜的切片**上过一趟滤镜
    （`_shot_texture`，拼接之前），母带那一趟只剩拼接与 BGM 闪避——都调 `render.py`
    的同一批函数，不在这里复制第二份。"""
    client = client or rc.endpoints()
    work.mkdir(parents=True, exist_ok=True)
    shots = list(spec.get("shots") or [])
    if not shots:
        raise MediaError("spec.shots 为空，没有可渲染的镜头")

    # 身份哈希一次取齐（缓存在 /healthz 里是每次现读，这里整条片子用同一份，
    # 于是「母版中途换了」不会让同一批镜头拿两种键）
    health = await asyncio.to_thread(client["health"])
    if not health.get("ok"):
        raise MediaError(f"creation-remotion 健康检查未通过：{health}")
    identities = {"master_hash": str(health.get("master_hash") or ""),
                  "lockfile_hash": str(health.get("lockfile_hash") or "")}
    missing = [k for k, v in identities.items() if len(v) < 8]
    if missing:
        raise MediaError(
            f"creation-remotion 没报出 {'、'.join(missing)}——缓存键缺这一段就会让"
            "「改了母版却命中旧切片」，整条片子拒绝开工比静默命中便宜")

    clips: list[Path] = []
    ledger: list[dict[str, Any]] = []
    files: dict[str, dict[str, Path]] = {}
    hitmaps: dict[str, Any] = {}
    frames_used = 0
    for i, shot in enumerate(shots):
        rec = await render_code_shot(shot, spec=spec, providers=providers, work=work,
                                     index=i, frames_used=frames_used,
                                     identities=identities, client=client,
                                     storage=storage,
                                     cache=cache, progress=progress)
        hitmaps[rec["id"]] = rec.pop("hits", None) or {}
        files[rec["id"]] = {"clip": rec["clip"], "frame": rec["frame"]}
        if not rec.get("cached"):
            frames_used += int(rec["planned_frames"])
        clips.append(rec["clip"])
        ledger.append({k: v for k, v in rec.items() if k not in ("clip", "frame")})
        await _emit(progress, {"stage": "shot", "shot": rec["id"], "done": i + 1,
                         "total": len(shots), "frames_total": frames_used})

    drift = check_token_drift(ledger)
    if drift:
        raise MediaError("闸⑤ 跨镜一致性未通过，母带还没开始（一帧不拼接）：\n"
                         + "\n".join(f"  · {e}" for e in drift[:10]))

    # ---- 母带：拼接与 BGM 闪避走 `render.py` 的函数；质感**不在这一层**了 ----
    # 质感那一趟挪到了每一镜的切片上（`_shot_texture`，拼接之前），母带这里没有滤镜：
    # 「第 3 镜老电视、其余柔光」这种要求在母带那一趟单次滤镜上是做不到的——一趟只能
    # 选一档。挪动的代价是零：每一帧本来就要在拼接时过一次 x264，现在只是在它之前
    # 多过一次滤镜编码，与原先母带那趟整片滤镜的编码量同一量级。
    master = work / "master.mp4"
    await _emit(progress, {"stage": "assemble"})
    fps = int(spec["fps"])
    # R7-T22：转场落在**拼接层**（`mediaops.concat` → ffmpeg xfade），逐镜切片一个字节
    # 都不动。两条判据都在这一趟、都拿实测秒数：一条判「这个时长配得上这条片子吗」
    # （接缝处不得叠过短镜的一半），一条判「叠上了没有」（成片该短多少就短了多少）。
    # 为什么不在分镜期判前一条：一镜多长是 TTS 说了算的，分镜那会儿手里只有下限。
    plan = cspec.transition_plan(spec)
    shot_secs = [(str(r["id"]), float(r.get("sec") or 0.0)) for r in ledger]
    misfit = cspec.check_transition(shot_secs, style=plan["style"],
                                    duration=plan["duration"])[0]
    if misfit:
        raise MediaError("一帧都没拼，先把转场说清楚：\n"
                         + "\n".join(f"  · {e}" for e in misfit))

    # ---- 闸⑰ 音画数据对账（R7-T23）：判在**拼接之前**，红了连一帧都不用拼 ----
    # 三条量出来的事实：BGM 够不够在整片里只放一遍、旁白到底在不在（逐镜切片的电平）、
    # 以及接缝上的字尾账（转场那个交叠窗口吃不吃得下两侧语音之后的余量）。
    # 成片长度按**账**算（逐镜实测之和 − 接缝数 × d）：那条算术与 T22 的
    # `check_transition_effect` 是同一个数，后者拿成片实测复核它——两处口径必须一致，
    # 不一致时以成片实测为准，那条判据会红。
    clip_db: float | None = None
    clips_with_audio = 0
    for clip in clips:
        try:
            info = await asyncio.to_thread(mediaops.probe, clip)
        except MediaError:
            continue
        if not info.get("has_audio"):
            continue
        clips_with_audio += 1
        try:
            db = float(mediaops.audio_levels(clip)["max_db"])
        except MediaError:
            continue
        clip_db = db if clip_db is None else max(clip_db, db)
    bgm_sec: float | None = None
    if bgm is not None and Path(bgm).exists():
        bgm_sec = float(mediaops.probe(bgm)["duration"])
    planned_film_sec = (sum(sec for _, sec in shot_secs)
                        - max(0, len(clips) - 1) * plan["duration"])
    voice_tails = [(str(r["id"]),
                    max(0.0, float(r.get("sec") or 0.0)
                        - (float(r.get("speech_sec") or 0.0) + rpipe.BREATH_LEAD_SEC)))
                   for r in ledger]
    av_errors, av_warns = cspec.check_av_accounting(
        narration=bool(spec.get("narration")), film_sec=planned_film_sec,
        clip_db=clip_db, clips_with_audio=clips_with_audio, bgm_sec=bgm_sec,
        gaps=max(0, len(clips) - 1), overlap_sec=plan["duration"],
        voice_tails=voice_tails, fps=fps)
    if av_errors:
        raise MediaError("闸⑰ 声画对账未通过，母带还没开始（一帧都没拼）：\n"
                         + "\n".join(f"  · {e}" for e in av_errors))

    await asyncio.to_thread(mediaops.concat, clips, master, fps=fps, transition=plan)
    total = float(mediaops.probe(master)["duration"])
    not_taken = cspec.check_transition_effect([sec for _, sec in shot_secs], total,
                                              style=plan["style"],
                                              duration=plan["duration"], fps=fps)
    if not_taken:
        raise MediaError("\n".join(f"  · {e}" for e in not_taken))

    texture_by_shot = {str(r["id"]): str(r.get("texture") or "none") for r in ledger}
    texture_filter = next((str(r.get("texture_filter") or "") for r in ledger
                           if r.get("texture_filter")), "")

    out = work / "final.mp4"
    await _emit(progress, {"stage": "mux"})
    bgm_conf = spec.get("bgm") or {}
    mix_mode = await rpipe._mux_bgm(
        master, bgm, volume=float(bgm_conf.get("volume", 0.18)),
        duck=bool(bgm_conf.get("duck", True)), total_sec=total, out=out)

    info = mediaops.probe(out)
    timings = [r.get("render_timings") or {} for r in ledger if not r.get("cached")]
    ms = [float(t.get("per_frame_ms") or 0.0) for t in timings if t.get("per_frame_ms")]
    return {"video": out, "duration": info["duration"], "width": info["width"],
            "height": info["height"], "fps": info["fps"], "shots": ledger,
            "shot_files": files,
            "cached_shots": sum(1 for r in ledger if r.get("cached")),
            "hitmap": hitmaps, "hitmap_errors": [],
            "frames_total": frames_used,
            "published_frames": int(info.get("frames") or 0),
            "mix_mode": mix_mode, "bgm": str(bgm or ""),
            # texture 这一栏现在是**整片默认档**（顶层那个开关的原意），每一镜真正跑的
            # 档在 texture_by_shot 里点名——两者可以不同（一镜覆盖、或整片加而这一镜 none）。
            "texture": str(spec.get("texture") or "none"),
            "texture_by_shot": texture_by_shot,
            "texture_filter": texture_filter,
            # 转场的账（T22）：选了什么、接缝几处、**实测**叠掉了多少秒
            # （`overlap_sec` 与 `(接缝数 × 时长)` 对得上就是「转场真生效」的证据，
            # 上面那条判据判的就是这个差；这里留数是为了出片回执能自己算）。
            "transition": plan["style"],
            "transition_sec": plan["duration"],
            "transition_gaps": max(0, len(clips) - 1),
            "overlap_sec": round(sum(sec for _, sec in shot_secs) - total, 3),
            # 进出场同理：档名 + **逐镜**实际窗口帧数（窗口是该镜帧数的 10%、
            # 封顶 15 帧，短镜不足 2 帧时是 0——那时它确实没有动作，
            # 不许把「选了 fade」当成「淡入了」；这也是回执能自己核对的那一栏）
            "entrance": str(spec.get("entrance") or "none"),
            "exit": str(spec.get("exit") or "none"),
            "motion_window_by_shot": {
                r["id"]: cspec.shot_motion_window(int(r.get("planned_frames") or 0))
                for r in ledger},
            "av_drift_sec": round(abs(info["duration"] - total), 3),
            # 闸⑰ 的读数（T23）：这一道判在拼接之前，但账要出在回执上——
            # 「BGM 只放了一遍没有」与「旁白到底在不在」都得能自己算，
            # 而不是只留一句「闸⑰ 通过」。未判那一档（量不到电平）在这里是 `None`，
            # 与「很安静」不是同一个值。
            "av_accounting": {
                "narration": bool(spec.get("narration")),
                "film_sec_planned": round(planned_film_sec, 3),
                "clip_max_db": None if clip_db is None else round(clip_db, 2),
                "clips_with_audio": clips_with_audio,
                "bgm_sec": None if bgm_sec is None else round(bgm_sec, 3),
                "bgm_share": (None if not bgm_sec else
                              round(bgm_sec / planned_film_sec, 3)
                              if planned_film_sec > 0 else None),
                "voice_tails": {sid: round(t, 3) for sid, t in voice_tails},
                "warnings": list(av_warns),
            },
            "degraded": [f"{r['id']}：{r['degraded']}" for r in ledger if r["degraded"]],
            # 闸④ 两半的读数留在返回里：定 EMPTY_MIN_RATIO / SPAN_MIN 终值要的是
            # 这些数字的分布，只留布尔就再不知道那条线离实际有多远
            "empty_ratios": {r["id"]: r.get("empty_ratio") for r in ledger
                             if not r.get("cached")},
            "fill_spans": {r["id"]: r.get("fill_span") for r in ledger
                           if not r.get("cached")},
            "master_hash": identities["master_hash"],
            "lockfile_hash": identities["lockfile_hash"],
            "gl": str(health.get("gl") or ""),
            "per_frame_ms_max": round(max(ms), 1) if ms else None,
            "chain": "remotion"}
