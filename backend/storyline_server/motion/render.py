# -*- coding: utf-8 -*-
"""图形科普片与口播/母带共用的那一段执行层：配音、呼吸、按镜缓存接口、质感、混音。

P4 之前这个模块还负责「零素材排版逐帧截图」；那条链下线后，留下的只有**代码链的
母带那一趟也要用的四件事**。为什么不搬进 `code_render.py`：

* 这几件事与「像素怎么来的」无关——配音补长、词时间轴、缓存的收发协议、质感滤镜串、
  BGM 闪避，两条链当年各自实现过一份，漂过一次「界面上有这档、渲出来是原片」。
* 换一档质感（整片或单镜）/ 换一首 BGM 都**不该重烧任何一镜**：滤镜跑在切片缓存之后的
  那一趟上、BGM 跑在母带合流，所以它们的实现只能有一个出处。

两条不能忘的口径：

1. **镜头时长由配音实测决定**，不由模型给。`tts_timed` 回词级时间轴，每镜时长 =
   语音时长 + 前后呼吸；模型的 `duration_sec` 只当最低驻留用。反过来（先定秒数再
   塞话）必然出现「话没说完画面已切走」。
2. **TTS 不可用是降级，不是失败**：`_speech_of` 回静音并把原因带在账上，片子照出，
   但这一镜**不进缓存**——把一次故障钉成这条片子的永久事实，比静音更糟。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import mediaops
from ..mediaops import MediaError
from ..providers import ProviderError, Providers

BREATH_LEAD_SEC = 0.10      # 每镜开口前的呼吸
BREATH_TAIL_SEC = 0.35      # 每镜收尾后的驻留
DEFAULT_SHOT_SEC = 2.6      # 无旁白且模型没给时长时的兜底驻留

#: 整片帧预算（闸② 用它判）。帧率与文案长度在这里换成「这次要编多少帧」，
#: 超了要用户降 fps / 缩片子，不静默砍——静默砍等于交出一份不是对方要的东西。
MAX_FRAMES_TOTAL = 4000


# ---------------------------------------------------------------------------
# 配音：一条镜的音频与词时间轴
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


def align_words(text: str, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 edge-tts 的词时间轴贴到文案的字符区间上（母版按字排版，逐词点亮要这个）。

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


# ---------------------------------------------------------------------------
# 按镜缓存：一镜的像素只烧一次
# ---------------------------------------------------------------------------

class ShotCache:
    """逐镜切片的最小缓存接口（节点侧用对象存储实现，测试用字典实现）。

    两个方法都按 ``key`` 收发**一组同名文件**：``clip.mp4``（带声切片，拼接直接吃它）、
    ``frame.png``（代表帧，编辑器画缩略图与对比帧）、``meta.json``（本镜账：
    秒数 / 帧数 / 词数 / 降级说明）、``hitmap.json``（这一镜的命中表）。
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


# ---------------------------------------------------------------------------
# 观感滤镜与母带合流（滤镜现在按镜跑；函数留在这里，让「有哪几档」只有一个出处）
# ---------------------------------------------------------------------------

#: 质感档：**每一镜的切片上过一趟**视频滤镜（拼接之前），不是母带上的那一趟。
#: 值是一条 filter_complex 里的视频链，输入 pad 为 ``[0:v]``、输出 label 必须是
#: ``[v]``（``apply_texture`` 按这个口径拼命令）。
#:
#: 为什么挪到拼接之前：母带上那一趟整片只能选一档，「第 3 镜老电视、其余柔光」这种
#: 要求在单层滤镜里做不到。为什么它**仍然不进**按镜切片缓存键：缓存存的是没加质感的
#: 像素，滤镜在缓存之后按镜补——换档（整片或单镜）一帧 Remotion 都不重烧，代价只是
#: 这一镜再过一次 x264，而它本来在拼接那一趟也要再过一次。
#: 代价也说清楚：加了质感的镜会多**一代**编码，所以「none」是缺省档，不是可省的装饰。
#: 取值清单的另一半在 ``code_spec.TEXTURES``，两边各写一遍就会出现「界面上有这档、
#: 渲出来是原片」，由 ``tests/test_code_spec.py`` 按表逐项查。
TEXTURE_FILTERS: dict[str, str] = {
    # 老胶片：细颗粒 + 暗角 + 一点暖偏，纸片版式压色偏所以走 colorbalance 而不是 eq
    "film": ("[0:v]noise=alls=7:allf=t+u,vignette=PI/5,"
             "colorbalance=rm=.05:gm=.01:bm=-.05,"
             "unsharp=3:3:.6:3:3:.2[v]"),
    # 老电视：横向扫描线 + 左右色偏 + 压一点饱和，颗粒比胶片细
    "tv": ("[0:v]drawgrid=w=iw:h=3:t=1:c=black@0.16,"
           "rgbashift=rh=2:bh=-2,eq=saturation=.82:brightness=.02,"
           "noise=alls=4:allf=t[v]"),
    # 柔光：亮部分成一糊再 screen 回原图，高光往外泛。
    # 必须先 format=gbrp：blend 的 screen 是按平面算的，留在 YUV 上它会作用到
    # chroma（中心值 128 被推到约 192）——画面不是变亮而是整片换色相（实测染成品红）。
    "glow": ("[0:v]format=gbrp,split=2[base][soft];[soft]gblur=sigma=14[blur];"
             "[base][blur]blend=all_mode=screen:all_opacity=.38,"
             "format=yuv420p[v]"),
    # 漂白：硬对比、低饱和，冲印过头那种发白的暗部
    "bleach": ("[0:v]eq=contrast=1.22:saturation=.42:brightness=.03,"
               "curves=preset=increase_contrast,unsharp=5:5:.4[v]"),
}


def apply_texture(src: Path, out: Path, kind: str) -> str:
    """一段视频 → 加质感的视频（代码链按**每一镜的切片**各调一次，拼接之前）。
    → 实际消费的滤镜串（空串 = 这趟没做，原样拷贝）。

    返回串而不是布尔：记账要能回答「画面没变是档选错了，还是滤镜根本没跑」。
    """
    chain = TEXTURE_FILTERS.get(str(kind or "").strip().lower())
    if not chain:
        mediaops.ffmpeg("-i", str(src), "-c", "copy", str(out), timeout=300)
        return ""
    mediaops.ffmpeg("-i", str(src), "-filter_complex", chain,
                    "-map", "[v]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "copy",
                    "-movflags", "+faststart", str(out), timeout=900)
    return chain


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
