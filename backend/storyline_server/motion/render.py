# -*- coding: utf-8 -*-
"""零素材图形科普片的出片执行层：配音 → 定时 → 逐帧截图 → 合成 → 混音。

与 render_video 的分工：那条路是「有素材的时间线剪辑」，这条路是「没有素材的排版
出片」。两条共用 mediaops 的 ffmpeg 封装与同一条音画同步闸值，但**不共用时间线**——
这里的时钟由声音决定（见下），塞进 plan_timeline 只会让两边都变复杂。

三件在真机上量出来、写代码时必须记住的事：

1. **镜头时长由配音实测决定**，不由模型给。`tts_timed` 回词级时间轴，每镜时长 =
   语音时长 + 前后呼吸；模型的 `duration_sec` 只当最低驻留用。反过来（先定秒数再
   塞话）必然出现「话没说完画面已切走」。
2. **headless Chrome 的 `--virtual-time-budget` 推不动 rAF**（实测一次截图只发出
   1~2 个回调），所以页面按 `?t=毫秒` 出静态帧，一帧一次浏览器启动；也因此
   **动画停止后的帧直接复用末帧**，不白花启动成本。
3. **``--window-size`` 在截图模式与 dump-dom 模式下含义不同**（实测：同一条
   ``--window-size=1920,1080``，``--dump-dom`` 报视口 1898×982，``--screenshot`` 却把
   100vw×100vh 铺满 1920×1080）。像素出自截图那一路，所以 ``viewport_inset`` 用标尺图
   + cropdetect 量它（本机与容器 Chromium 实测都是 0×0 边框）。按 dump-dom 那组数字放大
   窗口，页面会按放大后的尺寸排版、再整体按回目标尺寸，成片被竖向压掉 9%。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import mediaops
from ..mediaops import MediaError
from ..providers import ProviderError, Providers
from . import hitmap as _hits
from . import templates as T

Progress = Callable[[dict[str, Any]], "Awaitable[None] | None"]

BREATH_LEAD_SEC = 0.10      # 每镜开口前的呼吸
BREATH_TAIL_SEC = 0.35      # 每镜收尾后的驻留
DEFAULT_SHOT_SEC = 2.6      # 无旁白且模型没给时长时的兜底驻留
MAX_FRAMES_TOTAL = 4000     # 逐帧截图按启动计费，超了要用户降 fps/缩片子，不静默砍
FRAME_TIMEOUT_SEC = 75.0    # 单次截图上限（含新旧 headless 两次尝试）

_VIEWPORT_PROBE = ('<!doctype html><html><head><meta charset="utf-8"><style>'
                   '*{margin:0}html,body{width:100%;height:100%;background:#000;'
                   'overflow:hidden}#r{width:100vw;height:100vh;background:#fff}'
                   '</style></head><body><div id="r"></div></body></html>')


def find_chrome() -> str | None:
    """浏览器定位与网页出片通道共用一套判据（Chrome/Edge 的 headless 能力等价）。"""
    from ..nodes.web_nodes import _find_chrome      # 延迟引：nodes 包比本模块重
    return _find_chrome()


def _file_uri(page: Path) -> str:
    """页面路径 → headless 能吃的 ``file://`` URI。

    必须先绝对化：配置里的 ``workspace_root`` 是相对路径（``.storyline/workspace``），
    而 ``Path.as_uri`` 对相对路径直接抛 ValueError——真机第一次跑就挂在这里。
    """
    return page.resolve().as_uri()


def viewport_inset(chrome: str, width: int, height: int, work: Path) -> tuple[int, int]:
    """→ (dx, dy) = 「窗口尺寸 − 视口尺寸」。每个渲染任务量一次，别处不猜。

    量的必须是**出像素那条路**。早先这里跑 ``--dump-dom`` 读 ``innerWidth``，看着更直接，
    实测却和 ``--screenshot`` 不是一回事：同一条 ``--window-size=1920,1080``，dump-dom
    报视口 1898×982，而截图那张 PNG 里 100vw×100vh 的白块铺满了 1920×1080。按前者把
    窗口放大到 1942×1178 去截图，页面就按 1942×1178 排版（``--u`` 跟着变大、画面带四周
    留出纸边），再整体按回 1920×1080 时竖向被压掉 9%——版式扁了，命中表的框也对不上。

    所以改成截一张标尺图（纯黑底 + 一块正好 100vw×100vh 的白），用 cropdetect 读白块的
    实际像素尺寸：这就是这一版浏览器在截图模式下真正给版式用的视口。
    """
    page = work / "_viewport.html"
    page.write_text(_VIEWPORT_PROBE, encoding="utf-8")
    png = work / "_viewport.png"
    png.unlink(missing_ok=True)      # 别让上一轮的标尺图冒充这次的测量结果
    from ..nodes.web_nodes import chrome_container_flags
    for headless in ("--headless=new", "--headless"):
        try:
            mediaops.run([chrome, headless, "--disable-gpu", "--hide-scrollbars",
                          "--no-first-run", *chrome_container_flags(),
                          f"--window-size={width},{height}", "--virtual-time-budget=1200",
                          f"--screenshot={png}", _file_uri(page)], timeout=90)
        except MediaError:
            continue
        if not png.exists():
            continue
        try:
            det = mediaops.ffmpeg("-i", str(png), "-vf",
                                  "cropdetect=limit=2:round=1:reset=0", "-frames:v", "1",
                                  "-f", "null", "-", timeout=60)
        except MediaError:
            continue
        rows = re.findall(r"crop:(\d+):(\d+):(\d+):(\d+)", det.stderr or "")
        if not rows:
            continue
        w, h, x, y = (int(v) for v in rows[-1])
        if w <= 0 or h <= 0:
            continue
        # 白块从哪开始也要还回去：视口若被浏览器挪了位置，光算尺寸仍会错位
        return (max(0, width - w - x), max(0, height - h - y))
    return (0, 0)


def capture_frame(chrome: str, target: str, png: Path, win_w: int, win_h: int) -> None:
    """截一帧：时刻已经写在 target 的 ?t= 里，这里只负责把这一帧落成 PNG。"""
    from ..nodes.web_nodes import chrome_container_flags
    png.parent.mkdir(parents=True, exist_ok=True)
    base = ["--disable-gpu", "--hide-scrollbars", "--no-first-run",
            *chrome_container_flags(),
            f"--window-size={win_w},{win_h}", f"--screenshot={png}",
            "--virtual-time-budget=1200"]
    last: Exception | None = None
    for headless in ("--headless=new", "--headless"):
        try:
            mediaops.run([chrome, headless, *base, target], timeout=FRAME_TIMEOUT_SEC)
        except MediaError as e:
            last = e
            continue
        if png.exists() and png.stat().st_size > 1024:
            return
        last = last or MediaError(f"{png.name} 没落盘")
    raise MediaError(f"headless 截图失败：{last}")


def assemble(work: Path, frames: int, fps: int, out: Path, *,
             scale_to: tuple[int, int] | None = None) -> None:
    """帧序列 → mp4。scale_to 用来把「放大窗口截出来的图」按回目标尺寸。"""
    vf = "format=yuv420p" if scale_to is None else (
        f"scale={scale_to[0]}:{scale_to[1]}:flags=bicubic,format=yuv420p")
    mediaops.ffmpeg("-framerate", str(fps), "-start_number", "0",
                    "-i", str(work / "f%04d.png"), "-frames:v", str(frames),
                    "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
                    timeout=max(180, frames * 3))


# ---------------------------------------------------------------------------
# 单镜：配音 → 时长 → 页面 → 帧 → 带声片段
# ---------------------------------------------------------------------------

def _pad_audio(src: Path, sec: float, dst: Path) -> Path:
    """把一段配音补到**正好** sec 秒（44.1k 立体声，好和别的镜头直接 concat）。"""
    mediaops.ffmpeg("-i", str(src), "-af", f"apad=whole_dur={sec:.3f}",
                    "-ar", "44100", "-ac", "2", "-t", f"{sec:.3f}", str(dst),
                    timeout=180)
    return dst


async def _speech_of(providers: Providers, text: str, dst: Path, *, voice: str,
                     rate: str) -> tuple[list[dict[str, Any]], float, str | None]:
    """→ (词时间轴, 语音时长, 降级说明)。TTS 不可用时回静音，片子照出、账上记一笔。"""
    try:
        path, words = await providers.tts_timed(text, dst, voice=voice, rate=rate)
        return words, float(mediaops.probe(path)["duration"]), None
    except (ProviderError, MediaError, OSError) as e:
        mediaops.silent_wav(dst, duration=max(0.5, len(text) * 0.22))
        return [], 0.0, f"TTS 不可用，本镜走静音：{e}"


# ---------------------------------------------------------------------------
# 按镜缓存：一镜的像素只烧一次
# ---------------------------------------------------------------------------

#: 缓存格式版本。命中表的条目形状、帧的复用口径变了就 +1，让旧键整体失效——
#: 留着旧对象不会报错，只会让「选着改」读到一份对不上版的表。
CACHE_VERSION = "1"

#: 影响这一镜**像素与声音**的顶层开关。BGM 不在其中（它在母带那一层混，
#: 换配乐不该让 20 镜全部重烧）；title/style 名之外的版式参数也不在（版式参数
#: 全在 shot 自己身上）。
_CACHE_KNOBS = ("aspect", "width", "height", "fps", "style", "narration",
                "voice", "rate", "subtitle_mode")


def shot_cache_key(shot: dict[str, Any], spec: dict[str, Any]) -> str:
    """→ 这一镜的缓存键（16 位十六进制）。

    判据是「重渲同一份输入必然得到同一张图」：镜头内容 + 决定排版与配音的顶层开关。
    少算一个键就是拿旧片冒充新片——所以 ``voice``/``rate`` 这类只在有旁白时才起作用的
    开关也一律计入，不玩「这次没旁白就可以不算」的条件哈希。
    """
    payload = {"v": CACHE_VERSION, "shot": shot,
               "knobs": {k: spec.get(k) for k in _CACHE_KNOBS}}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class ShotCache:
    """``render_shot`` 用的最小缓存接口（节点侧用对象存储实现，测试用字典实现）。

    两个方法都按 ``key`` 收发**一组同名文件**：``clip.mp4``（带声切片，拼接直接吃它）、
    ``frame.png``（settle 那一刻的代表帧，编辑器画缩略图与对比帧）、``meta.json``
    （本镜账：秒数/帧数/词数/降级说明）、``hitmap.json``（这一镜的命中表）。
    命中表跟着走是因为它和像素同源：换了缓存就必须换表，否则「选着改」会按旧框改错字段。

    只缓存**没有降级**的镜头：TTS 抖动过一次就把静音片钉进缓存，等于把一次故障变成
    这条片子的永久事实。
    """

    FILES = ("clip.mp4", "frame.png", "meta.json", "hitmap.json")

    async def fetch(self, key: str, dst_dir: Path) -> dict[str, Path] | None:
        """命中 → 落到 ``dst_dir`` 的同名字典；未命中 → None。"""
        raise NotImplementedError

    async def store(self, key: str, src: dict[str, Path]) -> None:
        """把 ``src`` 里的文件按本缓存的文件名约定存起来（失败只记一笔，不影响出片）。"""
        raise NotImplementedError


#: 跟着切片一起缓存的「本镜账」字段：命中时不必再 probe、再数帧。
#: ``degraded`` 也在里面——只有没降级的镜才进缓存，所以它恒为 null，
#: 但少了这一栏，命中镜的记录就让下游 ``r["degraded"]`` 直接 KeyError（真机撞过）。
_META_FIELDS = ("sec", "speech_sec", "frames", "captured_frames", "words",
                "degraded", "measured_sec", "width", "height", "fps", "settle_ms")


def _ledger_from_meta(meta: dict[str, Any], clip: Path, sid: str,
                      hits: dict[str, Any]) -> dict[str, Any]:
    """缓存命中：本镜账从 ``meta.json`` 还原，不再 probe 也不再数帧。"""
    rec = {k: v for k, v in meta.items() if k in _META_FIELDS}
    rec.update({"id": sid, "clip": clip, "hits": hits, "cached": True})
    return rec


async def render_shot(shot: dict[str, Any], *, spec: dict[str, Any], providers: Providers,
                      work: Path, chrome: str, inset: tuple[int, int],
                      index: int, frames_used: int,
                      progress: Progress | None = None,
                      cache: "ShotCache | None" = None) -> dict[str, Any]:
    """一镜 → (带声 mp4, 记账)。帧数超预算时抛错，不静默截断。

    ``cache`` 给了就走「按镜复用」：命中即整段跳过（连 TTS 都不重发），未命中则渲完
    把切片、代表帧、命中表与本镜账一起存进去。见 :class:`ShotCache`。
    """
    fps = int(spec["fps"])
    width, height = int(spec["width"]), int(spec["height"])
    text = str(shot.get("text") or "")
    sid = str(shot.get("id") or f"s{index + 1:02d}")
    shot_work = work / f"shot{index:02d}"
    shot_work.mkdir(parents=True, exist_ok=True)

    key = shot_cache_key(shot, spec) if cache is not None else ""
    if cache is not None:
        got = await cache.fetch(key, shot_work)
        if got:
            try:
                meta = json.loads(got["meta.json"].read_text(encoding="utf-8"))
                hits = json.loads(got["hitmap.json"].read_text(encoding="utf-8"))
            except (KeyError, OSError, ValueError):
                meta = None      # 缓存读不成不是事故：这一镜照实重渲一遍
            else:
                # 字段不齐全 = 老格式（比如缺 degraded 那一栏）：照原样还原会让下游
                # 少一个键就崩，宁可多烧这一镜一次，重渲后 store 用新格式覆盖同一个键。
                if (meta and hits.get("entries") is not None
                        and all(k in meta for k in _META_FIELDS)):
                    rec = _ledger_from_meta(meta, got["clip.mp4"], sid, hits)
                    rec.update({"cache_key": key, "frame": got["frame.png"]})
                    return rec

    timed: list[dict[str, Any]] = []
    speech = 0.0
    degrade: str | None = None
    voice_file = shot_work / "voice.wav"
    mp3 = shot_work / "voice.mp3"
    speech_src: Path | None = None
    if spec.get("narration") and text:
        try:
            _, words = await providers.tts_timed(
                text, mp3, voice=str(spec.get("voice") or ""),
                rate=str(spec.get("rate") or "+0%"))
            speech = float(mediaops.probe(mp3)["duration"])
            speech_src = mp3
            timed = T.align_words(text, words)
        except (ProviderError, MediaError, OSError) as e:
            degrade = f"TTS 不可用，本镜走静音：{e}"
    if timed:
        sec = max(float(shot.get("min_duration_sec") or 0.0),
                  speech + BREATH_LEAD_SEC + BREATH_TAIL_SEC)
    else:
        # 无旁白 / TTS 降级：按阅读速度驻留，仍尊重模型给的时长下限
        sec = max(float(shot.get("min_duration_sec") or 0.0),
                  DEFAULT_SHOT_SEC + len(text) * 0.16)
    if speech_src is None:
        mediaops.silent_wav(voice_file, duration=sec)
    else:
        _pad_audio(speech_src, sec, voice_file)
    frames = max(2, int(round(sec * fps)))
    if frames_used + frames > MAX_FRAMES_TOTAL:
        raise MediaError(
            f"帧数预算用尽：本片到 {sid} 需要 {frames_used + frames} 帧，上限 "
            f"{MAX_FRAMES_TOTAL}。降 fps（{fps}→10）或缩短片子再来")

    page = shot_work / "page.html"
    page.write_text(T.build_shot_page(shot, spec=spec, timed=timed, duration_sec=sec,
                                      width=width, height=height, fps=fps),
                    encoding="utf-8")
    uri = _file_uri(page)
    settle = T.settle_ms(shot, timed, sec)
    # 命中表：多花一次浏览器启动，换「画面上这一块 = spec 里哪个字段」这张对账单。
    # 它跑在同一份页面上（只是多个 ?probe=1），所以量到的框就是出片会看到的框。
    # 探针失败不推翻这次渲染——片子是真的存在，只是这一镜暂时选不动。
    hits = _hits.shot_table(shot, index, await asyncio.to_thread(
        _hits.probe_shot, chrome, page, width + inset[0], height + inset[1]))
    live = min(frames, max(1, int(round(settle * fps / 1000)) + 1))
    last = shot_work / f"f{live - 1:04d}.png"
    for i in range(live):
        await asyncio.to_thread(capture_frame, chrome, f"{uri}?t={int(i * 1000 / fps)}",
                                shot_work / f"f{i:04d}.png",
                                width + inset[0], height + inset[1])
        if progress is not None:
            r = progress({"stage": "capture", "shot": sid, "frame": i + 1,
                          "shot_frames": frames})
            if asyncio.iscoroutine(r):
                await r
    for i in range(live, frames):           # 动画已停：末帧直接复用，省启动
        shutil.copyfile(last, shot_work / f"f{i:04d}.png")

    await asyncio.to_thread(assemble, shot_work, frames, fps,
                            shot_work / "video.mp4",
                            scale_to=(width, height) if any(inset) else None)
    clip = shot_work / "clip.mp4"
    mediaops.ffmpeg("-i", str(shot_work / "video.mp4"), "-i", str(voice_file),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest",
                    str(clip), timeout=300)
    info = mediaops.probe(clip)
    rec: dict[str, Any] = {
        "id": sid, "clip": clip, "sec": sec, "speech_sec": round(speech, 3),
        "frames": frames, "captured_frames": live,
        "words": len(timed), "degraded": degrade,
        "measured_sec": info["duration"], "width": info["width"],
        "height": info["height"], "fps": info["fps"], "hits": hits,
        "settle_ms": int(round(settle)), "cache_key": key,
        "frame": shot_work / "frame.png"}
    # 代表帧 = 入场动画落定那一帧。编辑器画缩略图、局部改前后对比都取它，
    # 因为它和命中表量的是同一时刻（探针也跑在 settle 上）。
    shutil.copyfile(last, rec["frame"])
    if cache is not None and degrade is None:
        # 降级片不进缓存：一次 TTS 抖动不该被钉成这条片子的永久事实
        meta = {k: rec[k] for k in _META_FIELDS}
        (shot_work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                             encoding="utf-8")
        (shot_work / "hitmap.json").write_text(json.dumps(hits, ensure_ascii=False),
                                               encoding="utf-8")
        try:
            await cache.store(key, {"clip.mp4": clip, "frame.png": rec["frame"],
                                    "meta.json": shot_work / "meta.json",
                                    "hitmap.json": shot_work / "hitmap.json"})
        except Exception as exc:            # noqa: BLE001 - 存不成只影响下次要不要重烧
            rec["cache_stored"] = f"没存进缓存：{type(exc).__name__}: {str(exc)[:120]}"
    return rec


async def _mux_bgm(master: Path, bgm: Path | None, *, volume: float, duck: bool,
                   total_sec: float, out: Path) -> str:
    """旁白 + BGM 合流。返回实际走的合成方式，写进记账。"""
    if bgm is None or not bgm.exists():
        mediaops.ffmpeg("-i", str(master), "-c", "copy", str(out), timeout=300)
        return "voice_only"
    if duck:
        # 输入顺序不能反：sidechaincompress 压的是**第一个**输入，第二个只是触发信号。
        # 反了会变成「拿 BGM 压旁白」——旁白不动、BGM 却在混音里丢了（实测静音段 -91 dB）。
        graph = (f"[1:a]volume={volume:.3f}[bg];"
                 f"[bg][0:a]sidechaincompress=threshold=0.03:ratio=8:"
                 f"attack=20:release=400:makeup=1[dk];"
                 f"[dk][0:a]amix=inputs=2:duration=first:dropout_transition=0:"
                 f"normalize=0[aout]")
        mode = "bgm_ducked"
    else:
        graph = (f"[1:a]volume={volume:.3f}[bg];"
                 f"[bg][0:a]amix=inputs=2:duration=first:dropout_transition=0:"
                 f"normalize=0[aout]")
        mode = "bgm_mixed"
    mediaops.ffmpeg("-i", str(master),
                    "-stream_loop", "-1", "-t", f"{total_sec:.3f}", "-i", str(bgm),
                    "-filter_complex", graph,
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
                    "-t", f"{total_sec:.3f}", str(out), timeout=600)
    return mode


async def render_motion(spec: dict[str, Any], *, providers: Providers, work: Path,
                        bgm: Path | None = None,
                        progress: Progress | None = None,
                        cache: ShotCache | None = None) -> dict[str, Any]:
    """整条链路：分镜 spec → 成片。返回记账（每镜时长/帧数/降级），不落对象存储。"""
    work.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    if chrome is None:
        raise MediaError("找不到 headless 浏览器（chrome/edge）："
                         "设 STORYLINE_CHROME 指到可执行文件后重试")
    width, height, fps = int(spec["width"]), int(spec["height"]), int(spec["fps"])
    inset = await asyncio.to_thread(viewport_inset, chrome, width, height, work)
    shots = list(spec.get("shots") or [])
    if not shots:
        raise MediaError("spec.shots 为空，没有可渲染的镜头")

    clips: list[Path] = []
    ledger: list[dict[str, Any]] = []
    files: dict[str, dict[str, Path]] = {}
    # 命中表单独一本账：它几十 KB 且模型不需要读（是给人点选用的），混进逐镜账就会
    # 随工具结果进上下文，白花 token。
    hitmaps: dict[str, Any] = {}
    frames_used = 0
    for i, shot in enumerate(shots):
        rec = await render_shot(shot, spec=spec, providers=providers, work=work,
                                chrome=chrome, inset=inset, index=i,
                                frames_used=frames_used, progress=progress,
                                cache=cache)
        hitmaps[rec["id"]] = rec.pop("hits", None) or {}
        files[rec["id"]] = {"clip": rec["clip"], "frame": rec["frame"]}
        # 帧预算只管「这次真的启动浏览器截了多少帧」：缓存镜一帧没截，
        # 把它算进去会让复用旧镜的局部改依然撞上限，等于把省下来的力气没收。
        if not rec.get("cached"):
            frames_used += int(rec["frames"])
        clips.append(rec["clip"])
        ledger.append({k: v for k, v in rec.items() if k not in ("clip", "frame")})
        if progress is not None:
            r = progress({"stage": "shot", "shot": rec["id"], "done": i + 1,
                          "total": len(shots), "frames_total": frames_used})
            if asyncio.iscoroutine(r):
                await r

    master = work / "master.mp4"
    if progress is not None:
        r = progress({"stage": "assemble"})
        if asyncio.iscoroutine(r):
            await r
    await asyncio.to_thread(mediaops.concat, clips, master, fps=fps)
    total = float(mediaops.probe(master)["duration"])

    bgm_conf = spec.get("bgm") or {}
    out = work / "final.mp4"
    if progress is not None:
        r = progress({"stage": "mux"})
        if asyncio.iscoroutine(r):
            await r
    mix_mode = await _mux_bgm(master, bgm, volume=float(bgm_conf.get("volume", 0.18)),
                              duck=bool(bgm_conf.get("duck", True)),
                              total_sec=total, out=out)
    info = mediaops.probe(out)
    drift = abs(info["duration"] - total)
    probe_failed = [f"{sid}：{(tab.get('error') or '')[:160]}"
                    for sid, tab in hitmaps.items() if tab.get("error")]
    return {"video": out, "duration": info["duration"], "width": info["width"],
            "height": info["height"], "fps": info["fps"], "shots": ledger,
            "shot_files": files,
            "cached_shots": sum(1 for r in ledger if r.get("cached")),
            "hitmap": hitmaps, "hitmap_errors": probe_failed,
            "frames_total": frames_used,
            # 计划帧数与成片真有的帧数是两回事（每镜画面按语音收口），分开报
            "published_frames": int(info.get("frames") or 0), "inset": list(inset),
            "chrome": chrome, "mix_mode": mix_mode, "bgm": str(bgm or ""),
            "av_drift_sec": round(drift, 3),
            "degraded": [f"{r['id']}：{r['degraded']}" for r in ledger if r["degraded"]]}
