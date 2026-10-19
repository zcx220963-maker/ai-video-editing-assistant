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
                     （只对**套了母版**的镜生效：自包含镜不回读，见 :func:`masterless_ids`）
    闸⑥ 字段对账    :func:`check_field_reconcile` 母版回读的自报值 vs 注入值逐项比
    闸⑦ 每镜必画    **不在这一层**——它是整片一层的判据，读的是代码字节里真用上的
                     画法通道，所以在烧像素之前就判完了（`code_spec.validate_code_spec`）
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
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence


from .. import mediaops
from ..mediaops import MediaError
from ..providers import Providers
from . import beats as beat_mod
from . import code_spec as cspec
from . import remotion_client as rc
from . import render as rpipe
from . import judge_channel
from . import review_loop

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
               "motion",
               # 闸㉑（T33）要的那两栏。`motion_anchors` 是**读数**（提醒/未判两个出口），
               # `motion_anchor_frames` 是它算不出来的那个输入：旁白逐词的帧刻度。
               # 后者必须进缓存还原——命中缓存的那一镜不会重跑 TTS，没有它就只能把
               # 字幕锚全记成「未判」，而那是拿一个假未判冒充真判过。
               "motion_anchors", "motion_anchor_frames")
# 2026-10-16 摘了三栏：`asset_motion`（闸⑪ 资产动效三态）、`assets`（闸⑬ 资产账）、
# `reveal`（闸㉗ 未判那一栏）。三条判据的主语都是「上传件/素材上屏」，路拆了，账跟着
# 没地方记。**摘栏会让这之前的缓存 meta 一律被读成「不完整 → 重烧一次」**——这是
# 逐镜缓存设计的既定代价（宁可多烧一次，也不许一份旧 meta 在整片账上被读成
# 「那一闸没人判过」），不是缺陷。

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
            "voice": voice_file, "degrade": degrade,
            "src": str(speech_src) if speech_src is not None else None}


def _extend_narration(narr: dict[str, Any], pad: float, *, fps: int) -> dict[str, Any]:
    """卡拍补长：把这一镜往后补 pad 秒（只补不裁），声轨同步补齐静音。→ 新的 narr。"""
    if pad <= 0:
        return narr
    sec = float(narr["sec"]) + pad
    voice = Path(narr["voice"])
    if narr.get("src"):
        rpipe._pad_audio(Path(narr["src"]), sec, voice)
    else:
        mediaops.silent_wav(voice, duration=sec)
    return {**narr, "sec": sec, "frames": max(2, int(round(sec * fps))), "beat_pad": pad}


def _seam_overlaps(spec: Mapping[str, Any], shots: Sequence[Mapping[str, Any]]) -> list[float]:
    """每处接缝的交叠秒数（与拼接那一趟同一份 transition_plan）。"""
    plan = cspec.transition_plan(spec)
    base = float(plan["duration"]) if str(plan["style"]) not in ("", "none") else 0.0
    over = {str(x.get("after")): (0.0 if str(x.get("style") or "none") == "none"
                                  else float(x.get("duration") or 0.0))
            for x in plan.get("seams") or []}
    return [over.get(str(s.get("id") or ""), base) for s in shots[:-1]]


def _check_frame_account(sid: str, *, planned_frames: int, fps: int,
                         info: dict[str, Any], expected_size: tuple[int, int],
                         size_tolerance: int = 0) -> None:
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
    if abs(w - expected_size[0]) > size_tolerance or abs(h - expected_size[1]) > size_tolerance:
        raise MediaError(
            f"闸③ {sid}：成片尺寸 {w}×{h} 与分镜账 {expected_size[0]}×{expected_size[1]} "
            f"不一致（容差 {size_tolerance} px）——拼接会把不同尺寸的切片一起缩放，"
            "画面会糊，这里先拦下")


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


#: 母版底色常量的 Python 侧镜像（`remotion/src/tokens.ts` 的 `TOKENS.bg`）。
#: 只在一镜**没有母版可回读**、分镜也没申报底色时兜底。改 tokens.ts 就改这里，
#: `backend/tests/test_code_gates.py` 有一条逐字对上的判据。
DEFAULT_BG = "#f6f1e4"


def masterless_ids(shots: Iterable[Mapping[str, Any]]) -> set[str]:
    """这一批镜里**没套母版**的那些镜号：镜头代码里不出现 `<FilmLayout`。

    母版下线那一轮起，镜头代码自包含（裸 `<AbsoluteFill>`）是**合法**写法——于是
    「没读到母版 token」这件事从「这一镜坏了」变成「这一镜本来就不回读」。凡是拿
    回读当依据的判据，都要先过这一道：对自包含镜**沉默**，对套了母版却没回读的镜
    照旧报（那是真的坏了）。
    """
    return {str(s.get("id") or "")
            for s in shots
            if "<FilmLayout" not in str(s.get("code") or "")}


def _bg_of(tokens: Mapping[str, Any] | None,
           shot: Mapping[str, Any] | None = None) -> str:
    """闸④ 的基准色：**母版回读的实际底色**，不是写死的 #fff。

    三级退让，都写在明处：
      ① 母版回读的 `bg`（最可信——那是这一镜真画上去的颜色）；
      ② 这一镜在分镜里申报的 `bg`；
      ③ :data:`DEFAULT_BG`（与 `TOKENS.bg` 同值的常量）。
    自包含镜没有回读，落 ②/③；套了母版却读不到 token 的镜，落到的也是 ②/③——
    后者**不再是硬错**（原先这里直接抛）：这一镜的底色漂不漂，该由闸④ 的像素判据
    说话，而不是由「拿不到基准色」把整镜打成渲不出来。
    """
    bg = str((tokens or {}).get("bg") or "").strip()
    if bg.startswith("#") and len(bg) == 7:
        return bg
    if shot is not None:
        declared = str(shot.get("bg") or "").strip()
        if declared.startswith("#") and len(declared) == 7:
            return declared
    return DEFAULT_BG


def qc_strict(spec: Mapping[str, Any]) -> bool:
    """这条片子是不是严格质检档（缺省严格：没经过产品节点注入的 spec 保持旧语义）。"""
    return str(spec.get("qc") or cspec.QC_STRICT) == cspec.QC_STRICT


def _qc_gate(spec: Mapping[str, Any], warns: list[str], msg: str) -> None:
    """像素闸的出口：严格档照旧打回（抛 MediaError）；预览档记一条提醒、继续出片。

    预览档是主流做法：渲染前人在计划确认卡上看过预览静帧，渲后的机器读数只当体检报告，
    不再触发「整镜退回 → 模型改码 → 重烧」那一圈。
    """
    if qc_strict(spec):
        raise MediaError(msg)
    warns.append(msg)


async def _empty_check(sid: str, clip: Path, *, bg_hex: str, size: tuple[int, int],
                       duration: float, shot_work: Path,
                       band: tuple[int, int, int, int] | None = None,
                       warns: list[str] | None = None) -> dict[str, Any]:
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
    # 三个取样点并发（原来是逐帧 await，每帧一次 ffmpeg，三帧就是三趟白等）。
    # 三个调用之间没有任何共享：取样时刻不同（-ss 不同）、落盘文件各一个
    # （empty15/50/85.raw）、只读不写同一个 clip。gather 保序：下面按位置取
    # ratios / span，顺序错了「最低占比」就取错了帧。
    stats = list(await asyncio.gather(*[
        asyncio.to_thread(_frame_diff_stats, clip, duration * frac, bg_hex=bg_hex,
                          size=size, out=shot_work / f"empty{int(frac * 100)}.raw",
                          band=band)
        for frac in EMPTY_SAMPLES]))
    ratios = [s[0] for s in stats]
    low = min(ratios)
    span = max(s[1] for s in stats)
    where = f"{'带内' if band else '整帧'}"
    def _fail(msg: str) -> None:
        # warns 给了＝预览档：记提醒不打回（见 `_qc_gate`）。
        if warns is None:
            raise MediaError(msg)
        warns.append(msg)

    if low < EMPTY_MIN_RATIO and span < SPAN_MIN:
        _fail(
            f"闸④ {sid}：画面里几乎是空的（{where}三帧非底色像素占比最低 "
            f"{low * 100:.2f}% < {EMPTY_MIN_RATIO * 100:.0f}%，且内容只铺开带短边的 "
            f"{span * 100:.0f}% < {SPAN_MIN * 100:.0f}%）"
            f"——占比 {'、'.join(f'{r * 100:.1f}%' for r in ratios)}。"
            "镜头代码跑起来了但没画出东西（当年卡片链那种「字段写了、这张卡不认」的静默空壳，"
            "在这条链由像素判）。修法：把母题铺满整帧（或你留出来的那块画面，别缩在中间）、"
            "线条加粗到 3px 以上、或给主体块加一层够重的底色（opacity ≥ 0.5）")
    elif span < TIGHT_MIN:
        _fail(
            f"闸④ {sid}：内容挤成一团（{where}非底色像素的外接盒只占带短边 "
            f"{span * 100:.0f}% < {TIGHT_MIN * 100:.0f}%，三帧像素占比最低 "
            f"{low * 100:.2f}%）——画了东西，但那一小块不是画面，是贴纸。"
            f"把主体铺开：占画面短边的 {int(FILL_TARGET * 100)}% 以上，"
            "而不是照手机竖屏的手感在横屏中间画一小簇")
    return {"ratio": low, "span": round(span, 4)}


#: 允许**逐镜不同**的 token 键（R14）。这不是给缺陷开后门：这两件事在 R14 之后
#: 本来就是逐镜可配的观感决定（这一镜满幅、那一镜有安全区；这一镜暗底、那一镜浅底）。
#: 它们进母版的 `used` 表，是为了让**闸④ 拿到这一镜真实的基准色**（`_bg_of` 读它判空）
#: 与让 `_band_of` 拿到真实带子，不是为了参与跨镜一致性。把它们也拿去比，
#: 等于把新能力判死——「这镜满幅、那镜有安全区」会被报成「母版的常量每镜不同」，
#: 而那恰恰是这一版**要**的行为。
#:
#: 白名单是八项：前四项是**申报的**（`bg` 底色、`bleed` 满幅、`captionStyle` 与
#: `highlightStyle` 字幕两档），
#: 后四项 `paper`/`accent`/`hl`/`captionInk` 是**派生值**——母版的字幕条配色改成跟这一镜的
#: 色板走（Python 侧 `code_spec._palette_of` 认出色板后注入 `paper`/`ink`/`accent`，母版再把
#: 它实际排出去的条底、条上写的字色与高亮色回读成 `paper`/`captionInk`/`accent`/`hl`）。
#: 判据只有一条：**这个键在不在这一镜的观感决定里**。在，跨镜比它就是把「这镜牛皮纸、
#: 那镜深海蓝」判成母版漂移。`ink` 不在这七项里——它是母版常量，全片该一致，
#: 正是闸⑤ 要比的那一类。
#:
#: 几何四项（`bodyWidth`/`bodyHeight`/`topSafe`/`gutter`）**仍在比对面里，但不是全片一把
#: 比**：满幅镜的带子等于整帧，跨 `bleed` 比它就是把「这镜满幅、那镜留安全区」判成漂移，
#: 所以按 `bleed` 分桶（见 :data:`GEOMETRY_TOKEN_KEYS` 与 :func:`check_token_drift`)
#: ——「bleed 一样而 bodyWidth 不同」仍然是真漂移，仍然会红。
TOKEN_PER_SHOT_KEYS = frozenset({"bg", "bleed", "captionStyle", "highlightStyle",
                                 "paper", "accent", "hl", "captionInk"})

#: 报**画幅级**几何的四个键：逐镜不同只可能来自 `bleed`，故闸⑤ 按 `bleed` 分桶比它们。
GEOMETRY_TOKEN_KEYS = frozenset({"bodyWidth", "bodyHeight", "topSafe", "gutter"})


def check_token_drift(ledger: list[dict[str, Any]],
                      masterless: Iterable[str] = ()) -> list[str]:
    """闸⑤：跨镜比对母版回读的 token，**容差 0**。→ 错误清单（空 = 通过）。

    不给「差 1 像素算通过」：这些值是母版的常量，真出问题时那 1 像素正是我们要找的
    东西，容差只会把缺陷放过去。

    只比**都读到了**的镜：某镜回读落空不该让整条片子陪葬，但那条落空必须已经在账上
    ——静默跳过才是这个函数最该避免的失败模式。**唯一例外是 `masterless`**
    （:func:`masterless_ids`）：自包含镜（代码里没有 `<FilmLayout`）本来就不回读，
    对它们沉默不是放行，是这条判据对它们**不适用**；套了母版却没回读的镜照旧报。

    `TOKEN_PER_SHOT_KEYS` 里那几个键跳过比对（理由见那份注释）。
    几何四项（:data:`GEOMETRY_TOKEN_KEYS`）**按 `bleed` 分桶**比：满幅镜的带子本来就
    等于整帧，跨 bleed 比它等于把「这镜满幅、那镜留安全区」判成漂移；同桶内不等才红。
    """
    errs: list[str] = []
    self_contained = set(masterless)
    seen: dict[tuple[str, bool], tuple[Any, str]] = {}     # (键, 几何桶) → (值, 首个镜号)
    for rec in ledger:
        toks = rec.get("tokens") or {}
        sid = str(rec.get("id") or "?")
        if not toks:
            if sid not in self_contained:
                errs.append(f"闸⑤ {sid}：没读到母版 token，这一镜没参与跨镜一致性比对")
            continue
        bleed = bool(toks.get("bleed"))
        for name, value in toks.items():
            if name in TOKEN_PER_SHOT_KEYS:
                continue
            key = (name, bleed if name in GEOMETRY_TOKEN_KEYS else False)
            first = seen.get(key)
            if first is None:
                seen[key] = (value, sid)
            elif first[0] != value:
                errs.append(
                    f"闸⑤：{name} 在 {first[1]} 是 {first[0]!r}、在 {sid} 是 {value!r}"
                    f"——母版的常量不该每镜不同（两镜同为 bleed={bleed}）")
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


# 闸⑬（`summarize_assets`）2026-10-16 删：它整份账的主语是 `shot['assets']`——
# 上传件上屏那条腿。腿拆了，这份账没有可记的东西。容器那侧的 `assets_gate` 一并停发。

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
            "scanned": {k: scanned.get(k) for k in ("texts", "leaders", "drawn", "dropped",
                                                    # 闸⑲-a / ⑳(a) 的两个读数（T30）与 ⑳(c) 的
                                                    # 画面指纹（T32）：母版量、这一层只搬，
                                                    # 判在 code_spec。
                                                    "subjects", "subject_share", "text_share",
                                                    "grid_text", "grid_drawn", "colors")},
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


def _near_peaks(diffs: Any, thresholds: Any, sample_hz: Any,
                events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """「差一点」的那几处：亚门槛、但已经明显在动的峰值。→ `[{start_sec, peak_diff}]`

    门槛取容器回传的那一份（`diff_min`），不在这里另写一个数；亚门槛的下沿用
    `motionsample.mjs` 里同一条回落线（地板线的 0.4 倍）——低于它的那些读数连
    「在动」都算不上，报给写手只会多一条噪声。

    只留**局部峰**、并且跳过已经进了 `events` 的那段时间：同一个动效不该既算「过线」，
    又在这里再报一遍。最多三处（时间顺序），够写手定位，又不会把回执撑长。
    """
    if not isinstance(diffs, Sequence) or isinstance(diffs, (str, bytes)) or len(diffs) < 3:
        return []
    th = thresholds if isinstance(thresholds, Mapping) else {}
    floor = th.get("diff_min")
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        floor = 1.5
    hz = sample_hz if isinstance(sample_hz, (int, float)) and sample_hz > 0 else 8.0
    inside = []
    for e in events:
        a, b = e.get("start_sec"), e.get("end_sec")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            inside.append((float(a), float(b)))

    def _in_event(t: float) -> bool:
        return any(a - 0.06 <= t <= b + 0.06 for a, b in inside)

    vals = [float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
            for v in diffs]
    picks: list[dict[str, Any]] = []
    for i, v in enumerate(vals):
        if not (floor * 0.4 <= v < floor):
            continue
        prev = vals[i - 1] if i else 0.0
        nxt = vals[i + 1] if i + 1 < len(vals) else 0.0
        if v < prev or v < nxt:          # 只取局部峰（一段上升沿里报一次就够）
            continue
        at = round((i + 0.5) / hz, 2)
        if _in_event(at):
            continue
        if picks and at - picks[-1]["start_sec"] < 0.25:   # 挨得太近的并成一个
            if v > picks[-1]["peak_diff"]:
                picks[-1] = {"start_sec": at, "peak_diff": round(v, 2)}
            continue
        picks.append({"start_sec": at, "peak_diff": round(v, 2)})
    if len(picks) > 3:
        keep = sorted(picks, key=lambda p: -p["peak_diff"])[:3]
        picks = sorted(keep, key=lambda p: p["start_sec"])
    return picks


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
    就会出现「容器改了阈值、回执还按旧值解释读数」（与校验层那几处阈值同一个理由）。

    原始差值曲线**不进账**（8 Hz × 20 s 就是一两百个数）：账要能被人读，而红的时候那句
    `reason` 已经把段数、间隔、帧号一并报出来了。这里只留 `events`（逐段起止帧与峰值）
    与 `min_gap_frames`——回执要能回答「它到底数到几段、最紧的一对隔多少」。

    **但「差一点」的那几处要留**（`near`）：2026-10-18 真机抓到的曲线是
    `[14.05, 10.24, 1.40, 0.50, … 0.19, 1.15, 1.23, 0.87, …]`——第二处动效（4.4 s 的
    大面板）其实**画了**，峰值 1.23，只差地板线 1.5 一点点，却被「数到 1 段」这句
    话吞掉。写手看到「只有一段」只会再排一遍时间轴，而它真正该做的是把那一处做大。
    所以这里留最响的几处**亚门槛**峰值（起止秒 + 峰值），给重写要求当抓手。
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
    out["near"] = _near_peaks(motion.get("diffs"), out["thresholds"],
                              out["sample_hz"], events)
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


def motion_error(shot_id: str, motion: Mapping[str, Any] | None, *,
                 strict: bool = True) -> str | None:
    """账 → 「这一镜该退」的一句人话；通过或未判返回 `None`。

    未判在这里**不退出**（那时判据压根没跑成），但它照样不是通过——`describe_motion`
    会把它单独列一行，报告层按未判计。这一闸要拦的是「有动效但看不出动」，
    不是「容器里的 ffmpeg 不见了」：把后者也判成片的罪，等于让工具故障冒充内容缺陷。

    `strict` 只管**措辞**（真正的打回在 `_qc_gate`）：预览档这一镜仍在成片里，
    文案却写「退回」，用户会以为片里没它（2026-10-17 真机 13 镜就是这么被误读的）。
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
    head = (f"闸⑭ 动效事件密度未通过，{shot_id} 退回（切片没进缓存、母带没开始）："
            if strict else
            f"闸⑭ 动效事件密度未通过，{shot_id} 这一镜**仍在成片里**"
            f"（预览档只提醒；要它退镜请切 strict）：")
    return (head + "\n"
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


# 闸⑬ 的措辞（`describe_assets`）同上：账没了，说账的话跟着走。

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
        elif kind == "overlap_subject":
            # 「字压图」——这是用户从真机上指出来的那一类（画出来的东西被字幕盖住，
            # 而且从前的判据只比文字盒与文字盒，这一路**一次都没响过**）。
            # 措辞里必须带上「这块字被压掉多少」，因为它是唯一可执行的量：模型据此
            # 知道是把字挪出去还是把图缩小，而「压在一起了」这句话本身没法改成代码。
            box = f.get("box") if isinstance(f.get("box"), Mapping) else {}
            where = ""
            if all(k in box for k in ("x", "y", "w", "h")):
                where = (f"（这块字在带内 x={box['x']} y={box['y']}"
                         f" w={box['w']} h={box['h']}）")
            out.append(f"闸⑧ {shot_id}：{who} 压在画出来的图形上（这块字被盖住 "
                       f"{_num(f.get('ratio'))}，判据 "
                       f"{th.get('subject_overlap_ratio', '未知')}）{where}"
                       "——把这块字移开，或给它一条自己的衬底，别让它压在图上")
        else:
            out.append(f"闸⑧ {shot_id}：{kind} {who}")
    sc = lay.get("scanned") if isinstance(lay.get("scanned"), Mapping) else {}
    dropped = int(sc.get("dropped") or 0) if isinstance(sc.get("dropped"), (int, float)) else 0
    if dropped > 0:
        out.append(f"闸⑧ {shot_id}：单帧 finding 超上限，丢了 {dropped} 条"
                   f"（上限 {th.get('max_findings', '未知')}）")
    return out


def describe_motion(shot_id: str, motion: Mapping[str, Any] | None, *,
                    strict: bool = True) -> list[str]:
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
                f"判据 ≥{want} 段、相邻起点 ≥{gap} s）。"
                + ("这一镜已经带镜号退回，没有进成片" if strict else
                   # 预览档（产品缺省）不退镜，只记提醒——原先这里一律写「没有进成片」，
                   # 而那 13 镜其实全在成片里（2026-10-17 真机片）。按档说实话。
                   "预览档只提醒：这一镜**仍在成片里**，后半截看起来会停住；"
                   "要它退镜请切 strict")]
    return [f"闸⑭ {shot_id}：动效没人判过（{m.get('reason') or '容器没回传采样行'}）——"
            "「没判」不等于「看得出动」，这一镜的动效读数在整片账上算未判"]


def shot_input_props(shot: Mapping[str, Any], spec: Mapping[str, Any], *,
                     frames: int, frame_words: list[dict[str, Any]],
                     film_start_sec: float = 0.0) -> dict[str, Any]:
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
    props: dict[str, Any] = {
        # `shotId` 2026-10-16 摘：它原来只为一件事存在——渲染侧据此把 `publicDir`
        # 指到 `public/assets/<sid>/`，让那一镜的上传件能按镜号找到。上传件不上屏
        # 那条腿拆掉之后，母版里没有一处读它（全仓只剩这一行写着它），
        # 那一层 publicDir 切换也一并没了。
        "fps": int(spec["fps"]),
        "durationInFrames": int(frames),
        # 这一镜在整片时间轴上的起点（秒）。逐镜切片各自从 0 帧渲染，但参考片里
        # 「一直在走的人」是**整片连续的一层**：镜头代码用
        # `filmStartSec + frame/fps` 取绝对时间，模板的 film 作用域轨道据此算步态，
        # 切点处才不会换脚/重新起步。必须由**真实时间轴**注入（配音时长决定），
        # 不能让写手在代码里写死估算值——那样每镜都会偏一点。
        "filmStartSec": round(float(film_start_sec), 3),
        "aspect": str(spec["aspect"]),
        "width": int(spec["width"]),
        "height": int(spec["height"]),
        "text": str(shot.get("text") or ""),
        "highlight": [str(h) for h in (shot.get("highlight") or [])],
        "words": frame_words,
        # 层E2（R19）/ 字幕两维（2026-10-16）：**这一镜**的档——先看镜级覆盖，
        # 没有才回落整片那一档。画字那侧读的就是这两个键，它不必知道「逐镜」这件事：
        # 「整片一档」与「逐镜覆盖」在它看来是同一件事，区别只在注入时谁赢。
        # 缺省是「贴画面 + 关键词换色」——用户否掉了纸条，缺省就得是那个不像 PPT 的组合。
        "captionStyle": str(shot.get("caption_style")
                            or spec.get("caption_style") or "on_picture"),
        "highlightStyle": str(shot.get("highlight_style")
                              or spec.get("highlight_style") or "color"),
        # 这一镜的字幕**由代码自己摆**（代码里有 `<CaptionLayer/>` 或构图的
        # `<CaptionText/>`）——母版那条让位，否则同屏两条字幕。判据是**代码的静态事实**
        # （`cspec.caption_any_by_code`：2026-10-18 起自排字幕是缺省写法，只认整条
        # `<CaptionLayer/>` 会让每一镜多出一条母版底部字幕）：运行期母版看不到这一镜的
        # 代码，而「有没有第二条」此时此地就定了。
        # 一栏布尔，不按「申报了才发」那一套省——母版那侧读的是真值（`=== true`），
        # 缺席与 false 同一件事，少一个「有没有这一栏」的分支。
        "captionFromCode": cspec.caption_any_by_code(
            str(shot.get("code") or ""),
            template_slots=cspec.style_template_slots(spec)),
        "fields": cspec.field_values(shot),
        # 母版进出场（T22）：档名 + 窗口帧数。`none` 或者窗口不足 2 帧时是 0/0，
        # 母版据此完全不套那层动画（于是「不选进出场」的片子逐帧与从前一致）。
        "entrance": str(spec.get("entrance") or "none"),
        "exit": str(spec.get("exit") or "none"),
        "entranceFrames": window,
        "exitFrames": window,
    }
    # 闸㉗ 的两项注入（`reveal` / `revealProbeFrames`）2026-10-16 删：那一族申报说的是
    # 「素材的哪个区域、在第几帧揭开」，没有素材就没有可揭的东西。`reveal` 也不再进
    # 缓存键（`shot_cache_key`）——于是 props 里少两个键，母版那一段整个不跑。
    # R14 风格档（底色 / 满幅 / 字幕条配色）。**只在 `film_style_of` 真给出东西时才发**
    # ——理由同「只在真申报时才发」：给没申报的镜塞一个 `film_style: {bleed: false, bg: ""}`
    # 会让 input_props 白涨字节（这一份 props 逐镜要过 HTTP），而母版读不到就当缺省值
    # （`FILM_STYLE_DEFAULT`），于是不含这一档的老片子读数一个字节都不变。
    # 认得出这一镜的色板时这一份还会带派生三色（`paper`/`ink`/`accent`），
    # 触发条件与 `bg` 不同，见 `code_spec.film_style_of`。
    film_style = cspec.film_style_of(shot, film=spec)
    if film_style:
        props["film_style"] = film_style
    # 整片美术方向（2026-10-18）。**只在真写了时才发**——理由与 `film_style` 同一条：
    # 没写 `art` 的片子 props 逐字节不变，缓存命中的老镜一个也不会因此重烧。
    # 读它的是两处：`resolveFilmStyle` 把归一后的缺省合进风格档，镜头代码写
    # `film.art.brush` 就能与整片对齐；以及写手提示词里那一段（`shot_writer._film_block`）。
    art = spec.get("art")
    if isinstance(art, Mapping) and art:
        props["artDirection"] = dict(art)
    # 用户钉的高亮词颜色（2026-10-16，「文字库」的「强调色」）。**只在真钉了的时候才发**
    # ——空串的意思就是「没钉，跟这一镜的色板走」，母版读不到这一栏就照老路
    # 取 `captionLook()` 的 accent。上面那两栏字幕键进的是字典字面量，因为它们
    # 每一镜都有值，而这一栏有「没有」这个状态。
    #
    # 它**不进 `TOKEN_PER_SHOT_KEYS`**（名单在下面 486 行附近）：那批是「允许逐镜不同的
    # 母版 token」，这一栏是整片一个值、没有逐镜覆盖，跨镜比对里本来就漂不动。
    # 但它是**整片开关**，所以进了 `FILM_KNOBS`（换了颜色＝换了像素，必须重烧）。
    highlight_color = cspec.norm_highlight_color(spec.get("highlight_color"))
    if highlight_color:
        props["highlightColor"] = highlight_color
    return props


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
    art = spec.get("art")
    # 整片写了 `art` 时，即使这一镜没选质感档也要过这一趟：那一层是**整片统一**
    # 的颗粒/暗角/色温（`render.art_grade`），跳过它就成了「同一版片子里几镜是
    # 统一过的、几镜没统一」，而这件事在成片里没人看得出来，只能靠这里不跳。
    if kind == "none" and not rpipe.art_grade(art):
        return clip, kind, "", frame
    out_clip = dst_dir / "clip_tex.mp4"
    chain = await asyncio.to_thread(rpipe.apply_texture, clip, out_clip, kind, art=art)
    if not chain:
        raise MediaError(
            f"{sid}：质感选的是 {kind}，却没拿到滤镜串——交付给拼接的还是原片。"
            "请核对 render.TEXTURE_FILTERS（质感档）与 render.art_grade（美术方向）"
            "里有没有这一档（宁可这一镜退回，也不出一版「账上加了、画面没加」的片子）")
    out_frame = dst_dir / "frame_tex.png"
    await _tail_frame(out_clip, out_frame, fps=int(spec["fps"]))
    return out_clip, kind, chain, out_frame


# 闸㉘ 的第二半（`_check_reveal_canvas`）2026-10-16 删：它拿 `reveal.canvas` 去对**素材的
# 真实像素尺寸**——没有素材就没有这张对账表。㉘ 的 schema 自洽那一半（`check_reveal_regions`）
# 也在 `code_spec.py` 同时下架。

# `_stage_shot_assets` 2026-10-16 删：它把 `shot["assets"]` 的字节按镜投到渲染容器的
# `public/assets/<镜号>/` 下（`POST /stage-assets`，两端各算一次 sha16 对账），并留一份
# `_manifest.json` 给闸⑬ 的渲染侧读。供图那三条路拆了以后 `assets` 恒为空，这一段是
# 一条永远走不进去的路——连同 `remotion_client` 的 `stage_asset` 口一起摘。

async def render_code_shot(shot: Mapping[str, Any], *, spec: Mapping[str, Any],
                           providers: Providers, work: Path, index: int,
                           frames_used: int, identities: Mapping[str, str],
                           client: dict[str, Any],
                           storage: Any = None,
                           cache: "rpipe.ShotCache | None" = None,
                           progress: Progress | None = None,
                           draft: Mapping[str, Any] | None = None,
                           narration: "asyncio.Future[dict[str, Any]] | None" = None,
                           budget: "FrameBudget | None" = None,
                           film_start_sec: float = 0.0
                           ) -> dict[str, Any]:
    """一镜（代码）→ 带声切片 + 记账。八道闸里属于这一镜的那几道都在这条路上。

    判红的是 ②（帧预算，进来之前）③（帧账）④（画面判两半）⑥（字段对账）；① 判在容器侧、
    这一层只把它的错误原文搬回来，⑤ 比的是**跨镜**所以落在拼接前，⑧ 只记账不设红。

    `identities` 是 /healthz 现报的 `master_hash` / `lockfile_hash`，缓存键要它们。

    `draft` 是**草稿档**（`{scale, image_format, crf, concurrency}`）：给「改一镜 → 数一遍闸」
    这个验收循环用的低像素档。它只换像素档，**不换判据**，也不改写任何一道闸的读数口径
    （⑭ 读 64 px 宽的灰度差，⑥⑧⑲-a/⑳/㉕ 读比例盒），所以草稿档绿了不等于成片档免检——
    成片那一次照烧。三条硬性区别：不查缓存、不写缓存、不补质感滤镜；产物键与服务端成片档
    不同名（`<key>.draft.mp4`），就是为了不让缩过的片子占住成片缓存键去冒充成片。
    """
    fps = int(spec["fps"])
    size = (int(spec["width"]), int(spec["height"]))
    sid = str(shot.get("id") or f"s{index + 1:02d}")
    shot_work = work / f"shot{index:02d}"
    shot_work.mkdir(parents=True, exist_ok=True)

    key = cspec.shot_cache_key(dict(shot), film=spec,
                               master_hash=str(identities["master_hash"]),
                               lockfile_hash=str(identities["lockfile_hash"]),
                               film_start_sec=film_start_sec)
    # 草稿档：换像素档不换判据，也**换一个键**。缩过的片子若占住成片缓存键，下一次
    # 命中缓存就是在拿 0.4 倍的片冒充成片——服务端落盘名同理带 `.draft` 后缀。
    draft_scale = 1.0
    render_payload: dict[str, Any] | None = None
    render_key = key
    if draft is not None:
        draft_scale = float(draft.get("scale") or 1.0)
        render_payload = {"draft": True,
                          **{k: v for k, v in draft.items()
                             if k in ("scale", "image_format", "jpeg_quality", "crf",
                                      "concurrency")}}
        render_key = hashlib.sha256(f"draft|{draft_scale}|{key}".encode()).hexdigest()[:40]
    if cache is not None and draft is None:
        got = await cache.fetch(key, shot_work)
        if got:
            try:
                meta = json.loads(got["meta.json"].read_text(encoding="utf-8"))
                hits = json.loads(got["hitmap.json"].read_text(encoding="utf-8"))
            except (KeyError, OSError, ValueError):
                meta = None            # 读不成不是事故：这一镜照实重渲一遍
            # 预览档烧出来、带着提醒进缓存的切片，严格档不认（它在严格档下本该被退回）。
            if meta and meta.get("qc_warnings") and qc_strict(spec):
                meta = None
            if meta and all(k in meta for k in META_FIELDS):
                rec = {k: v for k, v in meta.items() if k in META_FIELDS}
                cached_qc: list[str] = list(meta.get("qc_warnings") or [])
                # 命中缓存也要过闸⑭：判据的阈值（段数、间隔）**不在缓存键里**，
                # 所以缓存里的那一镜只证明「按当时的判据它是绿的」。阈值一改，
                # 复用切片的那几镜就会带着旧判据的读数直接进母带——这里补一刀，
                # 拒因与现渲的那一路同源（同一个 `motion_error`）。
                cached_reject = motion_error(sid, rec.get("motion"),
                                             strict=qc_strict(spec))
                if cached_reject and cached_reject not in cached_qc:
                    _qc_gate(spec, cached_qc,
                             f"{cached_reject}（这一镜来自缓存，按当前判据重判不过）")
                # 闸㉑ 同理，而且理由更硬：**申报（`shot["motion"]`）压根不进缓存键**
                # ——它不改变任何像素，改它不该重烧。但判据读的正是申报，于是命中缓存的
                # 那一镜必须拿**当前的**申报重判一次，否则回执上会挂着一份按旧申报算出来
                # 的读数，而那是账上写一套、画面对着一套里最坏的一种。重判要的两个输入
                # 都在缓存里：⑭ 的像素段（`rec["motion"]`）与旁白词表（`motion_anchor_frames`）。
                cached_anchor_errs, cached_anchor_warns, cached_anchor_undet = \
                    cspec.check_motion_windows(
                        shot, events=(rec.get("motion") or {}).get("events") or [],
                        word_frames=rec.get("motion_anchor_frames") or [], fps=fps,
                        beats=None)
                if cached_anchor_errs:
                    _qc_gate(spec, cached_qc, "\n".join(
                        f"{e}（这一镜来自缓存，按当前申报重判不过）"
                        for e in cached_anchor_errs))
                rec["motion_anchors"] = {"warnings": cached_anchor_warns,
                                         "undetermined": cached_anchor_undet}
                # 命中缓存**不等于**这一镜可以跳过滤镜：缓存里的那一镜是没加质感的像素，
                # 质感在缓存之后按镜补。跳过它就成了「整片加了质感、命中缓存的那几镜是
                # 原片」，而同一版片子里两种像素并存是没人看得见的错（闸⑤ 只管数值一致）。
                clip, kind, chain, frame = await _shot_texture(
                    got["clip.mp4"], shot_work, shot=shot, spec=spec, sid=sid,
                    frame=got["frame.png"])
                rec.update({"id": sid, "clip": clip, "hits": hits,
                            "cached": True, "frame": frame, "chain": "remotion",
                            "texture": kind, "texture_filter": chain,
                            "qc_warnings": cached_qc,
                            "beat_pad": float(shot.get("_beat_pad") or 0.0)})
                return rec

    # 旁白可能已由 `_render_shots` 提前并行合成好（TTS 预取）；没有就当场合成。
    narr = (await narration if narration is not None
            else await narration_for(shot, spec, providers, shot_work))
    sec, frames = float(narr["sec"]), int(narr["frames"])
    frame_budget = rpipe.max_frames_total(
        fps, spec.get("target_duration_sec"))
    if budget is not None:
        # 并发烧镜时帧预算按「预约」记：哪一镜先到先占，总数仍是同一道闸②。
        budget.reserve(sid, frames)
    elif frames_used + frames > frame_budget:
        raise MediaError(
            f"闸② 帧预算用尽：到 {sid} 需要 {frames_used + frames} 帧，上限 "
            f"{frame_budget}。降 fps（{fps}→10）或缩短片子再来——"
            "不静默砍镜，也不静默截帧")

    # 词表算一次、两处用：注入给母版的那份 props，与闸㉑ 判「字幕锚的首次可读帧」用的
    # 那份。各算一遍就会出现「母版按 A 表点亮、判据按 B 表对窗口」这种只在真机上
    # 看得见的分歧（与 `shot_input_props` 里 fields 那条同规）。
    frame_words = _to_frame_words(narr["timed"], fps)
    props = shot_input_props(shot, spec, frames=frames, frame_words=frame_words,
                             film_start_sec=film_start_sec)
    resp = await _await_heartbeat(
        progress, sid=sid, index=index, total=len(spec.get("shots") or []),
        awaitable=asyncio.to_thread(
            client["render"], str(shot.get("code") or ""), render_key, props,
            None, render_payload))
    await _emit(progress, {"stage": "rendered", "shot": sid, "done": index + 1,
                           "total": len(spec.get("shots") or []),
                           "cache_key": key, "frames": frames})

    raw = shot_work / "silent.mp4"
    art = str(resp.get("artifact") or f"/artifact/{key}")
    got_bytes = await asyncio.to_thread(client["fetch"], art, raw)
    info = await asyncio.to_thread(mediaops.probe, raw)
    # 期望尺寸按档折算：草稿档的产物是 `成片尺寸 × scale`，编码器要求偶数边，
    # 取整方式由容器那侧决定，所以草稿档给 2 px 容差——这不是放水，是**换档以后
    # 尺寸本来就该变小**；成片档 (size_tolerance=0) 仍然逐像素严格。
    exp_size = (round(size[0] * draft_scale / 2) * 2,
                round(size[1] * draft_scale / 2) * 2)
    _check_frame_account(sid, planned_frames=frames, fps=fps, info=info,
                         expected_size=exp_size, size_tolerance=2 if draft else 0)
    # 闸⑥ 排在闸④ 之前：它只是比对这一次响应里已经带回来的数据，一次 ffmpeg 都不开。
    # 值对不上时没必要再花几秒去判这张片的空不空——反正这一镜一定要退回。
    qc_warns: list[str] = []
    field_errs, field_checks = check_field_reconcile(
        sid, shot=shot, fields_used=resp.get("fields_used"))
    if field_errs:
        _qc_gate(spec, qc_warns,
            f"闸⑥ 字段级对账未通过，{sid} 退回（切片没进缓存、母带没开始）：\n"
            + "\n".join(f"  · {e}" for e in field_errs[:10]))
    # 闸⑭ 排在闸④ 之前，与闸⑥ 同一条理由：读数就在这次响应里，一次 ffmpeg 都不开。
    # **必须在切片进缓存之前判**——退回去的一镜不该在缓存里留一份「动效不合格」的切片，
    # 否则下一次同一个 cache_key 命中缓存、从缓存那一路进来（那一路只认读数不重判内容）。
    motion = summarize_motion(sid, resp.get("motion"))
    motion_err = motion_error(sid, motion, strict=qc_strict(spec))
    if motion_err:
        _qc_gate(spec, qc_warns, motion_err)
    # 闸㉑ 的后半段（T33）紧挨着闸⑭：它读的就是 ⑭ 刚数出来的那几段（触发帧），
    # 与这份词表。**同样排在切片进缓存之前**——理由与 ⑭ 一字不差：退回去的一镜
    # 不许在缓存里留一份「锚不上任何数据」的切片。
    anchor_errors, anchor_warns, anchor_undet = cspec.check_motion_windows(
        shot, events=motion.get("events") or [],
        word_frames=[int(w["startFrame"]) for w in frame_words],
        fps=fps, beats=None)
    if anchor_errors:
        _qc_gate(spec, qc_warns, "\n".join(anchor_errors))
    tokens = resp.get("tokens") or None
    # 取样几何折算：母版回读的带子是合成器像素（1080×1920 空间），而草稿档的切片是
    # 成片尺寸 × scale，直接拿它去 crop 会越界（ffmpeg 报 Parsed_crop_1 配不起来）。
    # 闸④/㉗ 判的都是**比例**，分子分母一起缩，结论不随档位移动；代价是像素基数变小、
    # 草稿档的 jpeg 噪声让非底色占比略偏高——所以草稿档只用来快速数动效，成片档仍严格重判。
    gate_size = (int(info["width"]), int(info["height"]))
    gs = gate_size[0] / size[0] if size[0] else 1.0
    band_full = _band_of(tokens, size)
    gate_band = None if band_full is None else (
        int(band_full[0] * gs), int(band_full[1] * gs),
        max(2, int(band_full[2] * gs)), max(2, int(band_full[3] * gs)))
    fill = await _empty_check(sid, raw, bg_hex=_bg_of(tokens, shot),
                              size=gate_size, duration=float(info["duration"]),
                              shot_work=shot_work, band=gate_band,
                              warns=None if qc_strict(spec) else qc_warns)

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
        # 闸⑭ 动效事件密度：上面那一刀只拦 rejected；能走到这里的是 ok 或 undetermined，
        # 后者的意思是「这一镜的动效没人判过」，必须原样留档（未判不许折成通过）。
        "motion": motion,
        # 闸㉑ 的读数（T33）：只留**两个出口**（提醒/未判）+ 判它用的那份词表。
        # 打回那一档在渲染口就抛了、走不到这里，所以账上要能看出的是「拿什么判的、
        # 哪几条没判完」。词表进缓存是必须的：命中缓存的那一镜不重跑 TTS，
        # 少了它字幕锚只能记成未判（见 META_FIELDS 那条注释）。
        "motion_anchors": {"warnings": anchor_warns, "undetermined": anchor_undet},
        "motion_anchor_frames": [int(w["startFrame"]) for w in frame_words],
        "render_timings": resp.get("timings") or {}, "cache_key": key,
        "chain": "remotion",
        "lint_warnings": [str(w)[:200] for w in (resp.get("warnings") or [])][:8],
        "bytes": int(got_bytes),
        "hits": _shot_table(sid, index, size, shot=shot, checks=field_checks),
        # 预览档下没打回、只记下来的像素闸读数（严格档这里恒为空：红了早就抛了）。
        "qc_warnings": qc_warns,
        "beat_pad": float(shot.get("_beat_pad") or 0.0),
    }
    if draft is not None:
        # 草稿档不进缓存：缓存的字节身份是「这一 cache_key 的这一份成片像素」，
        # 缩过的片子进去就等于让下一次命中拿草稿冒充成片。质感同理跳过——那是
        # 缓存之后的一整代重编码，验收循环不需要它，成片那一次会补上。
        rec["draft"] = {"scale": draft_scale,
                        "render": render_payload or {},
                        "cache_key": key}
        return rec

    if cache is not None and narr["degrade"] is None:
        meta = {k: rec[k] for k in META_FIELDS}
        if qc_warns:
            # 不进 META_FIELDS（那张表缺一栏就整份缓存作废）；单独带着，严格档据此不认这份缓存。
            meta["qc_warnings"] = qc_warns
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


#: 预览静帧：取这一镜的哪个位置（入场动画已落定、内容基本铺开），以及同时在飞的镜数
#: （容器那侧打包串行、渲染 2 路，3 路在飞正好让一镜打包时另两镜在渲）。
PREVIEW_AT = 0.7
PREVIEW_PARALLEL = 3
#: 预览静帧的分辨率系数。**2026-10-18 从 0.4 提到 1.0**：0.4 那档是「成本约整镜的
#: 百分之一」那个年代的余数，而今天这组图是**确认卡上用户唯一看得到的东西**——真机
#: 上 1920×1080 的片子渲成 768×432、JPEG 80，用户看完说「整体画面质量很低」，判的其实
#: 是这张缩水图。多付的是每帧几十到一两百毫秒（打包 1~3 秒才是固定成本），换的是
#: 「看到的就是会渲出来的那一帧」。成片那一趟不走这里（`render.scale` 缺省 1）。
PREVIEW_SCALE = 1.0


def _preview_frames(shot: Mapping[str, Any], spec: Mapping[str, Any]) -> int:
    """预览用的估算帧数：没有真实语音时长，按字数估（与整片估算同一把尺的中值）。"""
    text = str(shot.get("text") or "")
    if spec.get("narration") and text:
        sec = 0.6 + len(text) * 0.25 + rpipe.BREATH_LEAD_SEC + rpipe.BREATH_TAIL_SEC
    else:
        sec = rpipe.DEFAULT_SHOT_SEC + len(text) * 0.16
    sec = max(float(shot.get("min_duration_sec") or 0.0), sec)
    return max(2, int(round(sec * int(spec["fps"]))))


async def preview_stills(spec: Mapping[str, Any], *, client: dict[str, Any],
                         work: Path, storage: Any = None
                         ) -> tuple[dict[str, Path], list[str], dict[str, dict[str, Any]]]:
    """每镜渲一张预览静帧（小 JPEG）→ ({镜号: 文件}, 没出成的说明, {镜号: 版面账})。

    这是渲染前计划确认卡上的那几张图：人看一眼就知道画面对不对，成本约是整镜渲染的
    百分之一。任何一镜失败只记一句说明、不拦——预览是增益，硬闸在渲染那一趟。

    版面账（`summarize_layout` 那一份形状）是这一趟顺带回来的：字压字、字压图、出画，
    在**还没烧像素**的时候就能点名——那是修一处版面最便宜的时刻（改一镜代码、几秒钟），
    烧完再发现就是重烧那一镜。客户端没有带读数的那个口（离线用例的假服务）时版面账为空。
    """
    out: dict[str, Path] = {}
    notes: list[str] = []
    layouts: dict[str, dict[str, Any]] = {}
    stills_fn = client.get("stills_layout")
    gate = asyncio.Semaphore(PREVIEW_PARALLEL)
    work.mkdir(parents=True, exist_ok=True)
    # 预览帧也按同一把「估算时长」的尺给全片时钟：模板的 film 轨道（一直在走的人）
    # 在确认卡上就该是正确的一步，而不是每镜都从 0 帧抬起脚。
    planned_starts: list[float] = []
    acc = 0.0
    shots_for_clock = list(spec.get("shots") or [])
    overlaps = _seam_overlaps(spec, shots_for_clock)
    for i, s in enumerate(shots_for_clock):
        planned_starts.append(round(acc, 3))
        acc += _preview_frames(s, spec) / float(spec["fps"])
        if i < len(overlaps):
            acc -= overlaps[i]

    async def one(i: int, shot: Mapping[str, Any]) -> None:
        sid = str(shot.get("id") or f"s{i + 1:02d}")
        code = str(shot.get("code") or "")
        if not code.strip():
            return
        frames = _preview_frames(shot, spec)
        props = shot_input_props(shot, spec, frames=frames, frame_words=[],
                                 film_start_sec=planned_starts[i])
        async with gate:
            try:
                if stills_fn is not None:
                    got = await asyncio.to_thread(
                        stills_fn, code, props, [int(frames * PREVIEW_AT)],
                        scale=PREVIEW_SCALE)
                    stills = list((got or {}).get("stills") or [])
                    raw_layout = (got or {}).get("layout")
                    if raw_layout is not None:
                        layouts[sid] = summarize_layout(sid, raw_layout)
                else:
                    stills = await asyncio.to_thread(
                        client["stills"], code, props, [int(frames * PREVIEW_AT)],
                        scale=PREVIEW_SCALE)
            except Exception as exc:  # noqa: BLE001 - 预览缺一张不拦
                notes.append(f"{sid}：预览静帧没出成（{type(exc).__name__}: {str(exc)[:120]}）")
                return
        b64 = str((stills[0] if stills else {}).get("jpeg_b64") or "")
        if not b64:
            notes.append(f"{sid}：预览静帧是空的")
            return
        dst = work / f"preview_{sid}.jpg"
        dst.write_bytes(base64.b64decode(b64))
        out[sid] = dst

    await asyncio.gather(*(one(i, s) for i, s in enumerate(spec.get("shots") or [])))
    return out, notes, layouts


#: 预览版面读数里**值得模型当场改**的那几类（字压字、字压图、出画）。
#: 引线落空（`dangling`）不在此列：它在静帧上常是「引线还在画、终点还没到」的中间态，
#: 拿一帧去要求改，误报多于真病——整镜渲染那一趟的读数照旧会报它。
PREVIEW_LAYOUT_KINDS = frozenset({"overlap", "overlap_subject", "clipped"})


def _layout_trustworthy(lay: Mapping[str, Any]) -> bool:
    """这一份版面读数的坐标可信吗：有盒子落在画面之外**一整个画面那么远**就不可信。

    2026-10-17 实测：`/render-stills` 那一路量出来的盒子整体偏了约 -1,000,000 px，
    而且宽度塌了（四个字被量成 40 px 宽、232 px 高的竖排）——量的那一刻画面不在正常的
    排版状态里。同一镜走 `/render-shot` 量是对的。这种读数会给每一镜报十几条
    「出画 / 字压字」的假警报，模型照着去改本来没问题的代码，比没有读数更坏。
    所以整镜作废、一条不报；等探针在静帧那一路修好，这一关自然放行。
    """
    # `summarize_layout` 把 band 提到了顶层（不在 `scanned` 里）。
    band = lay.get("band") if isinstance(lay.get("band"), Mapping) else None
    w = float((band or {}).get("w") or 0)
    h = float((band or {}).get("h") or 0)
    if w <= 0 or h <= 0:
        return False
    for f in lay.get("findings") or []:
        box = f.get("box") if isinstance(f, Mapping) else None
        if not isinstance(box, Mapping):
            continue
        try:
            x, y = float(box.get("x", 0)), float(box.get("y", 0))
        except (TypeError, ValueError):
            return False
        if not (-w <= x <= 2 * w and -h <= y <= 2 * h):
            return False
    return True


def preview_layout_findings(layouts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """预览版面账 → 只留可执行的那几类，逐条人话（措辞与 `describe_layout` 同一份）。

    坐标不可信的那几镜（见 `_layout_trustworthy`）整镜跳过——一条假警报都不发。
    """
    out: list[str] = []
    for sid in sorted(layouts):
        lay = layouts[sid]
        if str(lay.get("status")) != "scanned" or not _layout_trustworthy(lay):
            continue
        kept = [f for f in (lay.get("findings") or [])
                if isinstance(f, Mapping) and f.get("kind") in PREVIEW_LAYOUT_KINDS]
        if not kept:
            continue
        out.extend(describe_layout(sid, {**lay, "findings": kept}))
    return out


class FrameBudget:
    """闸② 的并发版：多镜同时烧时共用一本帧数账。超额的那一镜当场抛（与串行版同一句话）。"""

    def __init__(self, *, used: int, limit: int) -> None:
        self.used = int(used)
        self.limit = int(limit)

    def reserve(self, sid: str, frames: int) -> None:
        if self.used + frames > self.limit:
            raise MediaError(
                f"闸② 帧预算用尽：到 {sid} 需要 {self.used + frames} 帧，上限 "
                f"{self.limit}。降 fps（→10）或缩短片子再来——"
                "不静默砍镜，也不静默截帧")
        self.used += frames


#: 同时在烧的镜数。渲染容器那侧打包串行、逐帧渲染按 DG_RENDER_SLOTS 路并发，
#: 这一侧并发提交才吃得到那几路（串行提交时容器的多路永远只用上一路）。
RENDER_PARALLEL = max(1, int(os.environ.get("MOTION_RENDER_PARALLEL", "2") or 2))


#: TTS 预取的并发路数：配音是网络请求，开太多会被 TTS 服务限流，4 路足够跑在渲染前面。
TTS_PREFETCH = 4


async def _render_shots(shots: list[dict[str, Any]], *, spec: Mapping[str, Any],
                        providers: Providers, work: Path, client: dict[str, Any],
                        identities: Mapping[str, str], storage: Any = None,
                        cache: "rpipe.ShotCache | None" = None,
                        progress: Progress | None = None,
                        frames_used: int = 0,
                        beats: Sequence[float] | None = None
                        ) -> tuple[list[Path], list[dict[str, Any]],
                                   dict[str, dict[str, Path]], dict[str, Any], int]:
    """**逐镜那一趟**：烧每一镜 → (clips, ledger, files, hitmaps, 帧预算计数)。

    单独抽出来，是因为它现在有**两个**调用方：整片那一趟（`render_code_motion`）与
    ㉖ 回看环的**只重烧改到的镜**那一趟。两处各写一遍循环，迟早一处带上 `rec["hits"]`、
    另一处忘了，于是命中表在回看之后整片消失——而命中表正是前端「选着改」的入口。
    「只重烧」这件事不需要额外机制：同一个 `cache` 传进来，没改代码的镜自然命中缓存，
    改过的那镜键变了、自然重烧（见 `shot_cache_key`：键的输入含这一镜的 `code`）。
    """
    clips: list[Path] = []
    ledger: list[dict[str, Any]] = []
    files: dict[str, dict[str, Path]] = {}
    hitmaps: dict[str, Any] = {}
    # TTS 预取：渲染容器一次只烧一镜（串行是为了不 OOM），但配音与它无关——
    # 开头就把所有镜的旁白并行合成（限 TTS_PREFETCH 路并发），烧到哪一镜时它的配音
    # 多半已经好了，不再「合成一句 → 烧一镜 → 再合成下一句」地干等。
    # 命中缓存的镜用不上它（切片自带音轨），白合成一次的代价是一次 TTS 网络请求。
    gate = asyncio.Semaphore(TTS_PREFETCH)

    async def _narr(i: int, shot: Mapping[str, Any]) -> dict[str, Any]:
        async with gate:
            return await narration_for(shot, spec, providers, work / f"shot{i:02d}")

    prefetch = ([asyncio.ensure_future(_narr(i, shot)) for i, shot in enumerate(shots)]
                if spec.get("narration") or beats else [None] * len(shots))
    # 卡拍：先等齐每镜的真实时长，再按拍点算每镜该往后补多少（只补不裁，最多半拍左右），
    # 补长进缓存键——同一段代码、补长不同就是不同的切片（帧数变了）。
    shots = list(shots)
    if beats:
        narrs = list(await asyncio.gather(*prefetch))
        pads = beat_mod.snap_pads([float(n["sec"]) for n in narrs],
                                  _seam_overlaps(spec, shots), beats)
        fps_i = int(spec["fps"])
        for i, pad in enumerate(pads):
            if pad > 0:
                narrs[i] = await asyncio.to_thread(_extend_narration, narrs[i], pad, fps=fps_i)
                shots[i] = {**shots[i], "_beat_pad": pad}
        loop = asyncio.get_running_loop()
        prefetch = []
        for n in narrs:
            fut = loop.create_future()
            fut.set_result(n)
            prefetch.append(fut)
    # 全片时钟：参考片里「一直在走的人」是整片连续的一层，模板的 film 作用域轨道
    # 按 `filmStartSec + frame/fps` 算步态。逐镜并发渲染时，每一镜只知道自己，
    # 不知道前面几镜真实有多长——所以先把所有旁白时长收齐、算出每镜起点，再开始烧。
    # 配音本来就在开头全部并行预取，这里只是把「开始烧第一镜」等到时长齐，**不增加
    # 一次 TTS 请求**；换来的是切点处相位/位移真的接得上，而不是每镜都偏一点。
    film_starts: list[float] = []
    # 拼接层是 xfade：后一镜提前 overlaps[i] 秒进场，真实起点要减掉累计交叠，
    # 否则全片时钟会越走越偏（硬切时 overlaps 全 0，与从前一致）。
    overlaps = _seam_overlaps(spec, shots)
    clock_narrs = ([await t for t in prefetch]
                   if prefetch and all(t is not None for t in prefetch) else None)
    if clock_narrs is not None:
        acc = 0.0
        for i, n in enumerate(clock_narrs):
            film_starts.append(round(acc, 3))
            acc += float((n or {}).get("sec") or 0.0)
            if i < len(overlaps):
                acc -= overlaps[i]
    else:
        fps_i = float(spec["fps"])
        acc = 0.0
        for i, shot in enumerate(shots):
            film_starts.append(round(acc, 3))
            acc += _preview_frames(shot, spec) / fps_i
            if i < len(overlaps):
                acc -= overlaps[i]
    # 多镜并发烧（RENDER_PARALLEL 路）。结果按镜号顺序装配；某一镜红了，还没开始的镜
    # 不再开（与串行版「红在第 k 镜、后面不渲」同语义），已在烧的照常烧完进缓存。
    budget = FrameBudget(used=frames_used, limit=rpipe.max_frames_total(
        int(spec["fps"]), spec.get("target_duration_sec")))
    slots = asyncio.Semaphore(RENDER_PARALLEL)
    stop = asyncio.Event()
    done_n = 0

    async def _one(i: int, shot: Mapping[str, Any]) -> dict[str, Any] | None:
        nonlocal done_n
        async with slots:
            if stop.is_set():
                return None
            try:
                rec = await render_code_shot(shot, spec=spec, providers=providers, work=work,
                                             index=i, frames_used=frames_used,
                                             identities=identities, client=client,
                                             storage=storage,
                                             cache=cache, progress=progress,
                                             narration=prefetch[i], budget=budget,
                                             film_start_sec=film_starts[i])
            except BaseException:
                stop.set()
                raise
        done_n += 1
        await _emit(progress, {"stage": "shot", "shot": rec["id"], "done": done_n,
                               "total": len(shots), "frames_total": budget.used})
        return rec

    try:
        results = await asyncio.gather(*(_one(i, s) for i, s in enumerate(shots)),
                                       return_exceptions=True)
        for got in results:
            if isinstance(got, BaseException):
                raise got
        for rec in results:
            if rec is None:
                continue
            hitmaps[rec["id"]] = rec.pop("hits", None) or {}
            files[rec["id"]] = {"clip": rec["clip"], "frame": rec["frame"]}
            clips.append(rec["clip"])
            ledger.append({k: v for k, v in rec.items() if k not in ("clip", "frame")})
        frames_used = budget.used
    finally:
        # 某一镜红了提前抛出时，没轮到的预取不能留成孤儿任务（也别吞掉它们的异常告警）。
        for t in prefetch:
            if t is not None and not t.done():
                t.cancel()
        await asyncio.gather(*(t for t in prefetch if t is not None), return_exceptions=True)
    return clips, ledger, files, hitmaps, frames_used


#: 静帧审美评审的整片开关（层 D2）。读法与 ``review_loop.REVIEW_KNOB`` 完全一致
#: （``spec`` 里那一栏，且在 `code_spec.SWITCH_KNOBS` 里活着穿过校验），**默认关**：
#: 这一档是**逐镜一次多模态调用**，是这条链上最贵的
#: 一类调用。它把闸④ 的射程从「带子里有没有东西」抬到「画得好不好」，关着只是少一条
#: 自动质检的路，不会让问题片子溜出去；反过来默认开会替所有人花掉这笔钱。
#: 花钱的能力必须能被人关掉（BGM / 质感 / 转场 / 回看都是先例）。
STILL_REVIEW_KNOB = "still_review"


async def _still_review(spec: dict[str, Any], ledger: Sequence[Mapping[str, Any]],
                        files: Mapping[str, Any], *,
                        scanned: Mapping[str, Any] | None = None
                        ) -> dict[str, Any]:
    """逐镜静帧审美评审（层 D2）→ 一份三出口回执。**它不拦交付。**

    闸④ 是**机器判据**（带内非底色占比 + 填充度），它天生判不了「构图失衡 / 元素重叠 /
    廉价光效 / 文字读不清」——那些是审美，像素统计说不出话。这一趟把每一镜的关键帧递给
    判断模型（复用 ``agent_framework.judge``，**不另起客户端**，Q90/Q91），按技能正文的
    Q 组判据问一句「违反吗」，把结论摊在回执上。

    三出口（**不许合并**）：``verdict=True`` 判红（画面站不住，一条**提醒**不是打回）、
    ``verdict=False`` 判绿、``verdict=None`` **未判**（没配置 / 调用失败 / 低置信）。
    「未判」与「判绿」在回执上必须是两句话——混成一格就又回到「没量过写成量过」。

    开关关着时 ``enabled=False``、``shots`` 为空，并**明写**「这趟没开」：
    「没开」既不是「缺席」（缺席＝压根不需要），更不是「通过」。
    """
    enabled = bool(spec.get(STILL_REVIEW_KNOB))
    cap = await judge_channel.capability()
    out: dict[str, Any] = {
        "gate": "04-b", "knob": STILL_REVIEW_KNOB, "enabled": enabled,
        "judge": dict(cap), "criteria": list(judge_channel.criteria_ids()),
        "shots": {}, "notes": [],
        "counts": {"pass": 0, "fail": 0, "undetermined": 0, "not_run": 0},
    }
    if not enabled:
        out["note"] = (f"静帧审美评审这趟没开（spec 里没有 `{STILL_REVIEW_KNOB}`）——"
                       "这一栏对每一镜都**没有结论**，不许读成「画面没问题」")
        out["counts"]["not_run"] = len(ledger)
        return out
    if not cap.get("judge"):
        # 没配置＝完全不启用（`judge.py` 的纪律）。这一栏如实记「做不了」，
        # **不**把它折算成通过，也不抛——它是提醒层，不是交付门。
        why = str(cap.get("why") or "")
        out["note"] = f"评审做不了：{why}"
        for i, r in enumerate(ledger):
            out["shots"][str(r.get("id") or f"s{i + 1:02d}")] = {
                "verdict": None, "confidence": 0.0, "why": why}
            out["counts"]["undetermined"] += 1
        return out
    by_id = {str(s.get("id") or ""): s for s in (spec.get("shots") or [])}
    scan = scanned if isinstance(scanned, Mapping) else {}
    for i, r in enumerate(ledger):
        sid = str(r.get("id") or f"s{i + 1:02d}")
        slot = files.get(sid)
        frame = slot.get("frame") if isinstance(slot, Mapping) else None
        if frame is None:
            # 没帧就不评：让模型闭着眼改代码是这条链上最坏的一种「判过了」。
            out["shots"][sid] = {
                "verdict": None, "confidence": 0.0,
                "why": "这一镜的 frame.png 没找到——没帧就不评（不许拿读数代替画面）"}
            out["counts"]["undetermined"] += 1
            continue
        got = await judge_channel.review_still(
            shot=by_id.get(sid) or {"id": sid}, image=frame,
            columns=review_loop.five_columns(scan.get(sid)))
        out["shots"][sid] = {"verdict": got.get("verdict"),
                             "confidence": float(got.get("confidence") or 0.0),
                             "reason": str(got.get("reason") or ""),
                             "why": str(got.get("why") or "")}
        v = got.get("verdict")
        out["counts"]["fail" if v is True else "pass" if v is False
                      else "undetermined"] += 1
    red = [sid for sid, s in out["shots"].items() if s["verdict"] is True]
    if red:
        out["notes"].append(
            "闸④-b（静帧审美）判红：" + "、".join(red)
            + "——这是**提醒**不是打回：像素上「有没有东西」由闸④ 管，"
              "「好不好看」由这一条如实报出来给人看")
    return out


async def render_code_motion(spec: dict[str, Any], *, providers: Providers, work: Path,
                             client: dict[str, Any] | None = None,
                             storage: Any = None,
                             bgm: Path | None = None,
                             progress: Progress | None = None,
                             cache: "rpipe.ShotCache | None" = None,
                             review: bool | None = None,
                             identity: bool | None = None,
                             user_id: str = "", conv_id: str = "") -> dict[str, Any]:
    """整条链：代码分镜 spec → 成片。质感在**每一镜的切片**上过一趟滤镜
    （`_shot_texture`，拼接之前），母带那一趟只剩拼接与 BGM 闪避——都调 `render.py`
    的同一批函数，不在这里复制第二份。

    ``review`` 是 ㉖ 回看环那个整片开关（R11）：``None`` = 看 ``spec["review"]``，
    显式 True/False 压过它。**默认关**：这一档每镜一次带帧的多模态调用，是最贵的调用，
    而它救的是「已经红了」的镜——开关关着只是少一条自动修复的路，不会让问题片子溜出去；
    关着的时候回执里那一栏对红镜是 `not_run`，**不许折算成通过**（Q112/Q113）。

    ``identity`` 是 ⑲-b（画面上那个东西是不是文案说的那个）那个**里面那层**开关
    （2026-10-17）：读法与 ``review`` 逐字同规（``None`` = 看 ``spec["identity"]``），
    而且**只有 ``review`` 也开着才跑**——回看环没开时它算出来的红镜无从处置，那笔钱
    等于白花。**默认关**，理由同 ``review``：它也是每镜一次带图调用。回执里
    ``review_loop.identity`` 那一栏永远写明这趟开没开（「没开」不是「像」）。

    ``user_id`` / ``conv_id`` 是闸㉛（指名权）的会话域身份。**空 = 不判那道闸**
    （读不到身份 = 未判，不是放行也不是打回——R13 的偏差口径：attachments 到不了
    本进程，判据落在会话域；没有身份就没有会话域）。调用方（出片节点）从 NodeState
    带进来；离线直调不传，那道闸按未判走，账上如实记。
    """
    client = client or rc.endpoints()
    work.mkdir(parents=True, exist_ok=True)
    shots = list(spec.get("shots") or [])
    if not shots:
        raise MediaError("spec.shots 为空，没有可渲染的镜头")
    # ㉖ 的开关：参数压过 spec 里那一栏；两处都没写就是**关**（默认关的理由见 docstring）。
    review_enabled = bool(spec.get(review_loop.REVIEW_KNOB)) if review is None else bool(review)
    # ⑲-b 的开关：读法逐字同 ㉖，但它是**里面那一层**——回看环没开时它一律不跑
    # （算出来的红镜没人能处置，那笔钱白花）。真正的「开没开」由回看环在
    # `receipt["identity"]["enabled"]` 里如实写，这里只把外层的取值传下去。
    identity_enabled = (bool(spec.get(review_loop.IDENTITY_KNOB))
                        if identity is None else bool(identity))

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

    # ---- 烧像素**之前**的两道闸（R13-T73 / R13-闸㉛）：一帧都不烧 ----
    #
    # T73：回看环开了而那条腿做不了（裁决通道没配 / 帧通道没落地）→ 当场打回并
    # 点名，而不是渲到一半抛 ReviewBlocked。后者是旧形状：片子烧完（或烧到一半）
    # 才发现回看跑不了，整条 run 白烧——而这件事在**第一帧之前**就查得出来。
    # 合同原文（R13-Q163）：本卷只做失败前置，不补 judge_channel。
    if review_enabled:
        cap = await review_loop.capability()
        if not cap.get("available"):
            raise MediaError(
                "回看环这趟开不了（spec 里 review=true）："
                + str(cap.get("why") or "（探测没给出原因）")
                + "——T73 的规矩：判据不可用时在**烧像素之前**打回并点名，"
                "而不是渲染到一半抛 ReviewBlocked（那样整条片子白烧）。"
                "把 review 关掉（缺省就是关，关着只是少一条自动修复的路，"
                "不会让问题片子溜出去），或把缺的那半条腿配齐再开。")

    # 配乐卡拍：有配乐且没关 beat_sync 时，先检一遍拍点（几百毫秒的 numpy），
    # 逐镜往后补到最近的拍上再烧。检不出稳定的拍就不卡（beats 为空）。
    beat_info: dict[str, Any] = {"beats": [], "bpm": None, "reason": "没有配乐"}
    if bgm is not None and Path(bgm).exists() and spec.get("beat_sync", True):
        beat_info = await asyncio.to_thread(beat_mod.detect_beats, bgm, work=work / "beats")
    beats = beat_info["beats"] or None

    clips, ledger, files, hitmaps, frames_used = await _render_shots(
        shots, spec=spec, providers=providers, work=work, client=client,
        identities=identities, storage=storage, cache=cache, progress=progress,
        beats=beats)

    film_qc: list[str] = []
    drift = check_token_drift(ledger, masterless_ids(shots))
    if drift:
        _qc_gate(spec, film_qc, "闸⑤ 跨镜一致性未通过，母带还没开始（一帧不拼接）：\n"
                 + "\n".join(f"  · {e}" for e in drift[:10]))

    # ---- 闸㉖ 回看环（R11-T56）：红/未判的镜先走一趟回看，改完**只重烧那一镜** ----
    # 位置是这里，不是更后面：回看改的是某一镜的**代码**，改完那一镜的切片、
    # `planned_frames`、版面读数全都是新的——下游（⑦ 转场配不配得上、⑰ 声画对账、
    # ⑲-a/⑳(c)/㉕ 的判点、⑬ 的拼接）读的都是**这一份**账。放在拼接之前、放在
    # 这些判据之前，它们判的就一定是回看之后的那一版；放在后面就变成「判旧的、
    # 拼新的」，那是最坏的一种账（两个版本各占一半）。
    review_receipt: dict[str, Any] | None = None
    # 回看环要 fps（按帧换算锚点）；原先 fps 到拼接那段才赋值，review=true 时必抛
    # UnboundLocalError——回看环一开就崩。
    fps = int(spec["fps"])
    if review_enabled is not False:
        # 回看重烧用的那一份分镜：账采纳的是它渲出来的，闸⑤ 的两桶（套母版 / 自包含）
        # 也就得按它分——拿回看**之前**的那份分镜分桶，回看把某镜改成自包含之后，
        # 那一镜会被当成「套了母版却没回读」重新红一次。
        reburn_shots: list[dict[str, Any]] = list(shots)

        async def _reburn(new_spec: dict[str, Any]) -> dict[str, Any]:
            """再烧一次**逐镜**那一趟（不走拼接/混音）：只有代码变了的镜是缓存未命中。"""
            reburn_shots[:] = list(new_spec.get("shots") or [])
            c2, l2, f2, h2, n2 = await _render_shots(
                list(new_spec.get("shots") or []), spec=new_spec, providers=providers,
                work=work, client=client, identities=identities, storage=storage,
                cache=cache, progress=progress, beats=beats)
            return {"shots": l2, "shot_files": f2, "clips": c2, "hitmaps": h2,
                    "frames_used": n2}

        try:
            rev = await review_loop.run(
                spec=spec, first={"shots": ledger, "shot_files": files},
                render_fn=_reburn, cache=cache, work=work,
                enabled=bool(review_enabled), identity=identity_enabled, fps=fps)
        except review_loop.ReviewBlocked as exc:
            # Q113：判据不可用要**阻断出片 + 出可读回执**，不是放行。
            raise MediaError(str(exc)) from exc
        review_receipt = rev["receipt"]
        if rev["render"] is not ledger:
            # 回看真的重烧了：**整份采纳**新的逐镜账（clips/ledger/files/hitmaps/
            # 帧预算计数五样一起换）。只换一部分会出现「画面用新的、账用旧的」——
            # 而那正是回执最会骗人的形状。
            got = rev["render"]
            clips = list(got.get("clips") or [])
            ledger = list(got.get("shots") or [])
            files = dict(got.get("shot_files") or {})
            hitmaps = dict(got.get("hitmaps") or {})
            frames_used = int(got.get("frames_used") or frames_used)
            drift = check_token_drift(ledger, masterless_ids(reburn_shots))
            if drift:
                raise MediaError("闸⑤ 跨镜一致性未通过（回看改过代码之后重判）：\n"
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
    # 层E1（R19）：把「只列覆盖」的 seams 摊成**逐接缝**的 (档, 时长) 一份，后面四处
    # 全用这一份——时长对账 / 声画账 / 拼接 / 生效判据。四处各摊一遍就会出现
    # 「判据按 A 算、ffmpeg 按 B 接」那种只有抽帧才看得见的分歧（`shot_texture`
    # 那条「记账与渲染不许各写一遍」的同族教训）。
    seam_eff: list[tuple[str, float]] = []
    for st, d in cspec.seams_to_per_index(plan["seams"], shot_secs):
        style_i = st or str(plan["style"])
        span_i = float(d) if st else float(plan["duration"])
        if style_i in ("", "none") or span_i <= 0:
            style_i, span_i = "none", 0.0
        seam_eff.append((style_i, span_i))
    seam_secs = [d for _st, d in seam_eff]
    seam_plan = [{"style": st, "duration": d} for st, d in seam_eff]
    misfit = cspec.check_transition(shot_secs, style=plan["style"],
                                    duration=plan["duration"],
                                    seams=plan["seams"])[0]
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
    planned_film_sec = (sum(sec for _, sec in shot_secs) - sum(seam_secs))
    voice_tails = [(str(r["id"]),
                    max(0.0, float(r.get("sec") or 0.0)
                        - (float(r.get("speech_sec") or 0.0) + rpipe.BREATH_LEAD_SEC)))
                   for r in ledger]
    active_gaps = sum(1 for d in seam_secs if d > 0)
    av_errors, av_warns = cspec.check_av_accounting(
        narration=bool(spec.get("narration")), film_sec=planned_film_sec,
        clip_db=clip_db, clips_with_audio=clips_with_audio, bgm_sec=bgm_sec,
        gaps=active_gaps, overlap_sec=(max(seam_secs) if seam_secs else 0.0),
        overlap_secs=seam_secs,
        voice_tails=voice_tails, fps=fps)
    if av_errors:
        _qc_gate(spec, film_qc, "闸⑰ 声画对账未通过，母带还没开始（一帧都没拼）：\n"
                         + "\n".join(f"  · {e}" for e in av_errors))

    # ---- 闸⑲-a 画面主体在场（R8-T30）：也是判在拼接之前 ----
    # 读数来自每一镜烧完之后母版的版面回读（闸⑧ 那条通道顺路带回的两个数），所以这一道
    # 只能在**产物上**判；但判的地方仍然放在拼接之前——「这一镜画面上是卡片+文字」是
    # 逐镜的事，拼完再判等于让整片白拼一次。
    subject_scanned = {str(r["id"]): r.get("layout") for r in ledger}
    sub_errors, sub_warns, sub_undet = cspec.check_subject_present(
        spec["shots"], scanned=subject_scanned)
    if sub_errors:
        _qc_gate(spec, film_qc, "闸⑲-a 画面主体在场未通过，母带还没开始（一帧都没拼）：\n"
                         + "\n".join(f"  · {e}" for e in sub_errors))
    # 回执上的读数：逐镜三个原始值（母版量的），取证要的是数不是「过了」。
    subject_readings: dict[str, dict[str, Any]] = {}
    for i, s in enumerate(spec["shots"]):
        row = subject_scanned.get(str(s.get("id") or f"s{i + 1:02d}")) or {}
        sc = row.get("scanned") if isinstance(row.get("scanned"), Mapping) else {}
        subject_readings[str(s.get("id") or f"s{i + 1:02d}")] = {
            "subjects": (sc or {}).get("subjects"),
            "subject_share": (sc or {}).get("subject_share"),
            "text_share": (sc or {}).get("text_share"),
            "status": row.get("status"),
        }

    # ---- 闸⑳(c) 画面同构（R8-T32）：同样判在拼接之前 ----
    # 判的是**画面**：两镜代码可以写得不一样（闸⑨ 跨镜骨架同构因此放过它，而且那条还要
    # ≥3 镜），屏幕上却仍是同一张卡。读数就挂在上面那份版面回读里，所以这一道的调用点
    # 紧挨着 ⑲-a——两条闸读同一趟回读、判的却是两件事（铺开多大一片 / 是不是同一张）。
    deg_errors, deg_warns, deg_undet = cspec.check_degenerate_forms(
        spec["shots"], scanned=subject_scanned)
    if deg_errors:
        _qc_gate(spec, film_qc, "闸⑳(c) 画面同构未通过，母带还没开始（一帧都没拼）：\n"
                         + "\n".join(f"  · {e}" for e in deg_errors))
    degenerate_readings: dict[str, dict[str, Any]] = {}
    for i, s in enumerate(spec["shots"]):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        row = subject_scanned.get(sid) or {}
        sc = row.get("scanned") if isinstance(row.get("scanned"), Mapping) else {}
        degenerate_readings[sid] = {
            "grid_text": (sc or {}).get("grid_text"),
            "grid_drawn": (sc or {}).get("grid_drawn"),
            "colors": (sc or {}).get("colors"),
            "status": row.get("status"),
        }

    # ---- 闸㉕ 徽章构图（R11）：判的还是同一趟 DOM 回读，还是拼接之前 ----
    # 与 ⑲-a/⑳(c) 并排：三条闸读的是**同一份**逐镜版面回读（⑲-a 读「主体太小」、
    # ⑳(c) 读「两镜像同一张卡」、㉕ 读「主体太大且居中近方形」），所以调用点紧挨着，
    # 且都在 `mediaops.concat` **之前**——徽章是逐镜的事，拼完再判等于让整片白拼一次。
    # 读数里那栏逐件盒（`subject_boxes`）是 ㉕ 新加的，共用 `subjectBoxes()`，
    # 阈值全留在 Python 这一侧（母版只报原始读数）。
    badge_errors, badge_warns, badge_undet = cspec.check_badge_composition(
        spec["shots"], scanned=subject_scanned)
    if badge_errors:
        _qc_gate(spec, film_qc, "闸㉕ 徽章构图未通过，母带还没开始（一帧都没拼）：\n"
                         + "\n".join(f"  · {e}" for e in badge_errors))
    badge_readings: dict[str, dict[str, Any]] = {}
    for i, s in enumerate(spec["shots"]):
        sid = str(s.get("id") or f"s{i + 1:02d}")
        row = subject_scanned.get(sid) or {}
        sc = row.get("scanned") if isinstance(row.get("scanned"), Mapping) else {}
        badge_readings[sid] = {
            "subjects": (sc or {}).get("subjects"),
            "subject_share": (sc or {}).get("subject_share"),
            "band": (sc or {}).get("band"),
            "subject_boxes": (sc or {}).get("subject_boxes"),
            "status": row.get("status"),
        }

    await asyncio.to_thread(mediaops.concat, clips, master, fps=fps,
                            transition=seam_plan)
    total = float(mediaops.probe(master)["duration"])
    not_taken = cspec.check_transition_effect([sec for _, sec in shot_secs], total,
                                              style=plan["style"],
                                              duration=plan["duration"], fps=fps,
                                              seams=seam_secs)
    if not_taken:
        _qc_gate(spec, film_qc, "\n".join(f"  · {e}" for e in not_taken))

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

    # ---- 闸④ 的第三半：静帧审美评审（层 D2）----
    # 放在**母带已经出好之后**：这一趟是逐镜一次多模态调用，是这条链上最贵的一类；
    # 它只判「画面好不好看」，判错了也不该让已经烧好的像素白费。闸④ 的两半
    # （非底色占比 / 填充度）是机器判据、红了出不了片；这一半是**提醒**，永远不打回。
    still_receipt = await _still_review(spec, ledger, files, scanned=subject_scanned)

    # 预览档的体检报告：逐镜 + 整片没打回、只记下来的那几条（严格档恒为空）。
    qc_report = [*film_qc, *(w for r in ledger for w in (r.get("qc_warnings") or []))]
    return {"video": out, "duration": info["duration"], "width": info["width"],
            "height": info["height"], "fps": info["fps"], "shots": ledger,
            "qc": str(spec.get("qc") or cspec.QC_STRICT),
            "qc_warnings": qc_report,
            # 卡拍的账：检到的速度、拍数、实际补过的镜（补长秒数）；检不出时 reason 说为什么。
            "beat_sync": {"bpm": beat_info.get("bpm"), "beats": len(beat_info.get("beats") or []),
                          "reason": beat_info.get("reason") or "",
                          "padded": {str(r["id"]): r.get("beat_pad")
                                     for r in ledger if r.get("beat_pad")}},
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
            # 层E1（R19）：**真落了转场的那几处**接缝数（硬接的那几处不算），
            # 以及逐接缝的实际档与时长——整片一档时它就是同一个值重复 n−1 次。
            "transition_gaps": active_gaps,
            "transition_seams": seam_plan,
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
            # 闸⑲-a 的读数（T30）：逐镜两个原始占比 + 三个出口。取证要的是**原始读数**，
            # 不是「过了」。未判那一档（母版没回读到）在这里是 status != scanned，
            # 与「量到了、很小」不是同一句话。
            "subject_presence": {
                "min_bbox": cspec.SUBJECT_MIN_BBOX,
                "text_dominant": cspec.TEXT_DOMINANT_SHARE,
                "shots": subject_readings,
                "warnings": list(sub_warns),
                "undetermined": list(sub_undet),
            },
            # 闸⑳(c) 的读数（T32）：逐镜的画面指纹原文。取证要的是**指纹**本身——
            # 「这两镜的格子一样」要能被人拿着两个数自己核，而不是只看到一句「不重复」。
            "degenerate_forms": {
                "shots": degenerate_readings,
                "warnings": list(deg_warns),
                "undetermined": list(deg_undet),
            },
            # 闸㉕ 的读数（R11）：逐镜的件数 / 占比 / 带尺寸 / **逐件盒**。取证要的是盒
            # 本身——「居中近方形」要能被人拿着 4 个数自己核，而不是只看到一句「是徽章」。
            "badge_composition": {
                "min_share": cspec.BADGE_MIN_SHARE,
                "center_tol": cspec.BADGE_CENTER_TOL,
                "aspect_tol": cspec.BADGE_ASPECT_TOL,
                "shots": badge_readings,
                "warnings": list(badge_warns),
                "undetermined": list(badge_undet),
            },
            # 闸㉖ 的回执（R11-T56）：**三出口 + 缺席**。这一栏不打回画面，它保证的是
            # 「回看」这件事不在交付语里被说成做过——数据全部来自账（缓存键 + 切片字节），
            # 不来自模型的话。没开回看时红镜记 `not_run`（不是缺席、更不是通过）。
            "review_loop": review_receipt,
            # 闸④-b 的回执（层 D2）：逐镜的**静帧审美**三出口。它是提醒层，不打回——
            # 「没开」明写成 not_run、「没配置」明写成 undetermined，两者都不许读成通过。
            "still_review": still_receipt,
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
