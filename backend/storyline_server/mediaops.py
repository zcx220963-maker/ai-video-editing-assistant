"""ffmpeg / ffprobe 原子操作层（阻塞函数）。

所有函数都是同步阻塞的——真实节点里必须用 ``asyncio.to_thread`` 包裹调用，
避免卡住事件循环（约定见项目记忆与 BaseNode.process 的 await 语义）。
只依赖标准库 + 系统 PATH 上的 ffmpeg 完整构建。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus"}
# 前端 accept 一直允许选图，后端此前没有图片白名单（存储层 spec §4 要补的一处）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"}


class MediaError(RuntimeError):
    pass


def run(args: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise MediaError(f"命令失败 rc={proc.returncode}: {' '.join(args[:6])}…\n{tail}")
    return proc


def ffmpeg(*args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-hide_banner", "-y", *args], timeout=timeout)


def probe(path: str | Path) -> dict[str, Any]:
    """ffprobe → {width,height,duration,fps,frames,has_audio}（load_media 的契约字段）。

    不回传 `path`：这是对**本机某份文件**的一次观测，把它的绝对路径顺手带进节点产物，
    就是持久化 payload 里工作区路径泄漏的源头（引用只由 storage 层的 obj: 引用落库）。
    """
    p = Path(path)
    if not p.exists():
        raise MediaError(f"媒体文件不存在: {p}")
    proc = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ], timeout=60)
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0.0)
    fps = 0.0
    if v:
        rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        try:
            fps = float(Fraction(rate))
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        if fps <= 0 or fps > 1000:  # ffprobe 对部分容器返回 1000/1001 之外的垃圾值
            fps = 25.0
    return {
        "width": int(v.get("width", 0)) if v else 0,
        "height": int(v.get("height", 0)) if v else 0,
        "duration": round(duration, 3),
        "fps": round(fps, 3),
        # 视频流自己的帧数：逐帧截图那条路拿它对账「计划帧数 vs 成片真有多少帧」
        "frames": int(v.get("nb_frames") or 0) if v else 0,
        "has_audio": a is not None,
    }


def cut(src: str | Path, start: float, end: float, dst: str | Path) -> Path:
    """按时间区间切出片段（重编码保证帧精确，veryfast 缩短耗时）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.04, float(end) - float(start))
    ffmpeg("-ss", f"{float(start):.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", str(dst))
    return dst


def extract_audio(src: str | Path, dst: str | Path) -> Path:
    """抽单声道 16kHz wav，供 faster-whisper 使用。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst))
    return dst


def extract_frame(src: str | Path, at_sec: float, dst: str | Path, max_side: int = 480) -> Path:
    """在 at_sec 处抽一帧并缩放为代理图（送 VL 模型，控制体积）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-ss", f"{max(0.0, float(at_sec)):.3f}", "-i", str(src),
           "-frames:v", "1",
           "-vf", f"scale={max_side}:-2", "-q:v", "3", str(dst))
    return dst


_PTS = re.compile(r"pts_time:([0-9]+\.?[0-9]*)")


def scene_ranges(src: str | Path, threshold: float = 0.3,
                 min_sec: float = 0.6, max_sec: float = 12.0,
                 total_duration: float | None = None) -> list[tuple[float, float]]:
    """ffmpeg 场景检测 → [(start,end)…] 镜头区间列表。

    用 ``select='gt(scene,T)',showinfo`` 的 stderr pts_time 序列作为切点，
    再按 min/max 镜头长度合并与拆分。
    """
    info = probe(src)
    dur = total_duration if total_duration is not None else info["duration"]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(src),
         "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
        creationflags=_NO_WINDOW)
    cuts = sorted({float(m) for m in _PTS.findall(proc.stderr or "") if 0.05 < float(m) < dur - 0.05})
    bounds = [0.0, *cuts, dur]
    ranges: list[tuple[float, float]] = []
    for s, e in zip(bounds, bounds[1:]):
        if e - s < 1e-3:
            continue
        ranges.append((round(s, 3), round(e, 3)))
    # 合并过短片段
    merged: list[list[float]] = []
    for s, e in ranges:
        if merged and e - s < min_sec:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    if merged and merged[-1][1] - merged[-1][0] < min_sec and len(merged) > 1:
        merged[-2][1] = merged[-1][1]
        merged.pop()
    # 拆分过长片段
    out: list[tuple[float, float]] = []
    for s, e in merged:
        n = max(1, int((e - s) // max_sec) + 1)
        step = (e - s) / n
        for i in range(n):
            out.append((round(s + i * step, 3), round(min(s + (i + 1) * step, e), 3)))
    return out


def concat(clips: list[str | Path], dst: str | Path, *,
           fps: float | None = None) -> Path:
    """按顺序拼接片段（concat filter 统一重编码，避免参数不一致导致的流错误）。

    ``fps`` 锁输出帧率：逐帧截图那条路（motion）的片段是「每段各自的秒数 × 同一帧率」，
    不锁帧率时 concat 会把段间时长差摊进时间戳，成片平均帧率掉到 9.7~9.84——
    画面看不出，但「成片字节量到的 fps」与 spec 对不上，机器证据只能标 UNVERIFIED。
    """
    if not clips:
        raise MediaError("concat 需要至少一个片段")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    for c in clips:
        args += ["-i", str(c)]
    n = len(clips)
    ins = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    args += ["-filter_complex", f"{ins}concat=n={n}:v=1:a=1[v][a]",
             "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac"]
    if fps:
        args += ["-r", str(fps), "-vsync", "cfr"]
    args += [str(dst)]
    ffmpeg(*args)
    return dst


def silent_wav(dst: str | Path, duration: float = 5.0) -> Path:
    """生成静音 wav —— TTS 不可用时的配音降级，保证时间线可渲染。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono",
           "-t", f"{float(duration):.3f}", str(dst))
    return dst


def tone_wav(dst: str | Path, duration: float = 5.0, freq: int = 220) -> Path:
    """生成正弦音 wav —— 无 BGM 曲库时的降级占位轨。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc=0.1*sin({freq}*2*PI*t):s=24000",
           "-t", f"{float(duration):.3f}", str(dst))
    return dst


_XFADE = {
    "fade": "fade", "fadeblack": "fadeblack", "dissolve": "dissolve",
    "wipeleft": "wipeleft", "wiperight": "wiperight", "slideup": "slideup",
    "circleopen": "circleopen", "radial": "radial", "smoothleft": "smoothleft",
    "hardcut": "fade",  # 硬切以 0 时长近似：给一个极短淡入避免滤镜报错
}


def patch_moviepy_chapters() -> bool:
    """修 MoviePy 2.x 解析带 Chapters 的视频时的 IndexError。

    病根（moviepy/video/io/ffmpeg_reader.py）::

        input_chapters = []                     # line 414：只建了空表
        ...
        if self._current_chapter:               # line 498
            input_chapters[input_number].append(self._current_chapter)   # line 499

    章节在 ffmpeg 输出里排在流信息之前，所以碰到第一个 ``Stream #`` 行走「退出章节」
    分支时，``input_chapters`` 还是空表——``input_chapters[0]`` 直接 IndexError::

        IndexError: list index out of range
        → OSError: Error passing `ffmpeg -i` command output: ...

    同一个文件里的章节处理器（line 556）是有边界检查的，只有这处漏了，是上游的疏漏。

    影响面**很大**且不像路径问题那样容易被识破：只要源素材带章节（HandBrake 转码、
    蓝光/DVD 抓取、B 站等站点下载、带章节的录屏——都很常见），``VideoFileClip`` 就
    打不开，成片永远出不来。真机现场：render_video 卡在 ``video_track`` 10%，
    报错文本被视图截断，看上去像「ffmpeg 读源文件中断」，排查方向会被带偏到路径/编码上。

    这里按项目既有手法（同 ``core_nodes._patch_moviepy_subclip``）修掉它。

    实现：把 ``FFmpegInfosParser.parse`` 的源码取出来，在 ``compile`` 阶段做一次
    精确文本替换补上那段边界检查，再用同一个类的 ``__dict__`` 当 globals 求值，
    于是 ``self`` 与 ``self.result`` 等既有状态一律照旧，行为与上游只差这一处。
    避免了两条更难走的路：包装 ``parse`` 够不到局部变量 ``input_chapters``；
    靠 trace 回调注局部变量则依赖 CPython 版本的具体约定，太脆。

    打不上（版本结构变了 / 取不到源码）返回 False，不影响其它功能。
    """
    try:
        from moviepy.video.io import ffmpeg_reader as _fr
    except Exception:  # noqa: BLE001 - 没装 moviepy 时媒体层本来也用不到
        return False

    parser = getattr(_fr, "FFmpegInfosParser", None)
    if parser is None:
        return False
    if getattr(parser, "_dsh_chapter_guard", False):
        return True                    # 已经打过，幂等

    # 目标文本：上游「退出章节」分支缺的那段检查（同函数 229 行的章节分支本来就有）。
    buggy = (
        "                # exit chapter\n"
        "                if self._current_chapter:\n"
        "                    input_chapters[input_number].append(self._current_chapter)\n"
    )
    fixed = (
        "                # exit chapter\n"
        "                if self._current_chapter:\n"
        "                    # 上游漏了这段：章节排在流信息之前，走到这里时\n"
        "                    # input_chapters 还是空表，直接下标就 IndexError。\n"
        "                    while len(input_chapters) < input_number + 1:\n"
        "                        input_chapters.append([])\n"
        "                    input_chapters[input_number].append(self._current_chapter)\n"
    )

    try:
        import inspect

        source = inspect.getsource(parser)
    except (OSError, TypeError):
        return False
    if source.count(buggy) != 1:
        return False                   # 上游改了写法：不猜，保持原样

    patched = source.replace(buggy, fixed)
    # globals 必须有模块级的 re / os / sp 这些名字（方法体里直接在用），
    # 再叠上类的 __dict__ 让 self 与其余方法保持同一身份。
    import sys as _sys

    module = _sys.modules.get(parser.__module__)
    namespace: dict = dict(getattr(module, "__dict__", {}))
    namespace.update(vars(parser))
    namespace.setdefault("__builtins__", __builtins__)
    try:
        exec(compile(patched, "<moviepy-ffmpeg_reader-patched>", "exec"), namespace)  # noqa: S102
    except Exception:  # noqa: BLE001 - 求值失败就退回上游实现
        return False

    new_parser = namespace.get("FFmpegInfosParser")
    if new_parser is None:
        return False

    parser.parse = new_parser.parse
    parser._dsh_chapter_guard = True
    return True


# 导入即修：这是媒体层的既有缺陷，不该等某个调用点想起来才打。
# 失败只影响「带章节素材能不能读」，不影响其它功能，所以静默按未被修补处理。
patch_moviepy_chapters()




def xfade_clip(tail_src: str | Path, head_src: str | Path, start_a: float,
               start_b: float, dst: str | Path, style: str = "fade",
               duration: float = 0.4, size: tuple[int, int] | None = None) -> Path:
    """从 A 的 start_a、B 的 start_b 各取一小段，生成转场效果片段（AI/模板转场共用）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    w, h = size or (1280, 720)
    d = max(0.1, float(duration))
    proc = ffmpeg(
        "-ss", f"{start_a:.3f}", "-i", str(tail_src), "-t", f"{d:.3f}",
        "-ss", f"{start_b:.3f}", "-i", str(head_src), "-t", f"{d:.3f}",
        "-filter_complex",
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[a];"
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[b];"
        f"[a][b]xfade=transition={_XFADE.get(style, 'fade')}:duration={d}:offset=0[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(dst))
    if not dst.exists() or dst.stat().st_size == 0:
        raise MediaError(f"xfade 产物为空: {proc.stderr[-400:]}")
    return dst
