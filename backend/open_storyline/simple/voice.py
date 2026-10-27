# -*- coding: utf-8 -*-
"""旁白：每镜字幕 → TTS → **按语音时长定镜长** → 铺成一条音轨 → 与画面合流。

为什么音轨不是"加个背景音乐"：无声的片子是"动态信息图"，有声的才是"短片"。
而对这条线来说，语音还解决了另一个更硬的问题——**镜长原来是我均分的**
（60 秒 15 镜各 4 秒），于是每镜的字幕长度与它的停留时间无关：5 个字的镜和 14 个字的镜
一样长。按语音时长定镜长之后，"这一镜停多久"由"这句话要说多久"决定，
节奏自然就对了。

四步，每步都能单独测：

    synthesize()   一镜一条 mp3（edge-tts，免费、要联网）
    fit_secs()     语音秒数 → 镜长（下限、留白、上限）
    fill_to()      总长不够目标 → 把留白摊回每镜（叙事片用）
    build_track()  按镜长把每段铺到时间轴（pad / trim 到整镜），再 concat
    mux()          画面 mp4 + 音轨 → 成片（`-c:v copy`，不重编码画面）

**TTS 失败不抛给用户**：`narrate()` 出问题时退回"无声 + 均分镜长"，
由上层在报告里写清"这条没旁白"。一条没有旁白的片子仍然是片子；
因为 TTS 挂了整条失败，才是把可用性绑在一个外部服务上。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .ffmpeg import duration, run
from .kernel import KernelError

#: 音色。云希是男声、语速偏快，科普解说里最常用的一档。
DEFAULT_VOICE = "zh-CN-YunxiNeural"
#: 语速。+8% 比默认快一档——短视频的旁白普遍比朗读快。
DEFAULT_RATE = "+8%"
#: 每镜语音说完之后留的空白（秒）。0 会让片子像连珠炮。
GAP_SEC = 0.45
#: 一镜最短（秒）。再短观众来不及看清画面。
MIN_SHOT_SEC = 2.2
#: 每镜最长：与 `film.MAX_SHOT_SEC` 同一口径（5.2 秒是"不像 PPT"的硬指标）。
MAX_SHOT_SEC = 5.2


async def synthesize(text: str, dst: Path, *, voice: str = DEFAULT_VOICE,
                     rate: str = DEFAULT_RATE) -> float:
    """一句话 → 一个 mp3。返回时长（秒）。"""
    text = str(text or "").strip()
    if not text:
        return 0.0
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        import edge_tts
    except Exception as exc:  # noqa: BLE001
        raise KernelError("voice", f"edge-tts 不可用：{exc}") from exc

    com = edge_tts.Communicate(text, voice, rate=rate)
    try:
        with dst.open("wb") as fh:
            async for chunk in com.stream():
                if chunk.get("type") == "audio":
                    fh.write(chunk["data"])
    except Exception as exc:  # noqa: BLE001 —— 在线服务，网络抖动很常见
        raise KernelError("voice", f"TTS 调用失败：{type(exc).__name__}: {exc}") from exc

    if not dst.exists() or dst.stat().st_size < 512:
        raise KernelError("voice", "TTS 产物为空（服务返回了但没有音频）")
    sec = duration(dst)
    if sec <= 0:
        raise KernelError("voice", "TTS 产物读不出时长")
    return sec


def fit_secs(voice_secs: list[float], *, gap: float = GAP_SEC,
             lo: float = MIN_SHOT_SEC, hi: float = MAX_SHOT_SEC) -> list[float]:
    """语音秒数 → 镜长。

    规则：`max(lo, voice + gap)`；超过 `hi` 时先压留白（最多压到 0.1），
    还超就**夹到 hi** 并在末尾抛一行提示——**不静默改**。
    夹到 hi 的后果是那句话会被切掉尾巴，所以 `narrate()` 会把这种镜报出来。
    """
    out: list[float] = []
    for sec in voice_secs:
        want = max(lo, float(sec) + gap)
        if want > hi:
            want = max(lo, min(hi, float(sec) + 0.1))
        out.append(round(min(hi, want), 2))
    return out


def over_long(voice_secs: list[float], *, hi: float = MAX_SHOT_SEC) -> list[int]:
    """哪些镜的语音本身就超过镜长上限（1 起）。它们会被切尾，必须报给用户。"""
    return [i + 1 for i, s in enumerate(voice_secs) if float(s) >= hi - 0.1]


def fill_to(secs: list[float], target: float, *, hi: float,
            floor_ratio: float = 0.92) -> list[float]:
    """总长不够目标时，把**留白**摊回每镜（每镜夹在 `hi` 以内）。

    为什么需要这一步：镜数按"目标秒数 ÷ 每镜秒数"定，但每镜**实际**停多久由那句
    话说多久定。实测要 100 秒、11 镜、每句约 5.8 秒 → 出来 **69.3 秒**，只有目标的
    69%。用户要的是"3 分钟的片子"，不是"11 个镜"。

    拉长的是每镜说完之后的空白（`build_track` 本来就按镜长 pad），所以**音画同步不受
    影响**，只是节奏更慢——叙事片本来就该慢。三轮把缺口摊平，摊不动（全部到 `hi`）就停。

    只用于叙事片。动效科普的快节奏不该靠拉长留白来凑时长——那会让它变慢，
    而它该做的是加镜数（那是分镜阶段的事，不是这里）。
    """
    out = [round(float(s), 2) for s in secs]
    if not out or target <= 0 or sum(out) >= float(target) * floor_ratio:
        return out
    for _ in range(3):
        gap = float(target) - sum(out)
        if gap <= 0.02:
            break
        room = [max(0.0, float(hi) - s) for s in out]
        cap = sum(room)
        if cap <= 0.02:
            break
        add = min(gap, cap)
        out = [round(s + add * r / cap, 2) for s, r in zip(out, room)]
    return out


#: 单镜 TTS 重试（2026-10-26）：edge-tts 偶发返回 0 字节，一次失败就放弃会让那镜「有字幕没声音」。
VOICE_RETRIES, VOICE_RETRY_DELAY = 3, 0.8


async def narrate(texts: list[str], out_dir: Path, *, voice: str = DEFAULT_VOICE,
                  rate: str = DEFAULT_RATE) -> dict[str, Any]:
    """每镜一条 mp3。**并发 3 路**——TTS 是网络往返，串行 15 镜要 20 秒。

    返回 ``{files, secs, failed}``。单镜失败不抛：那几镜退回"按字数估时长"，
    其余照常有声。整条都失败时上层会退回无声。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)
    files: list[Path | None] = [None] * len(texts)
    secs: list[float] = [0.0] * len(texts)
    failed: list[int] = []

    async def one(i: int) -> None:
        async with sem:
            dst = out_dir / f"v{i + 1:02d}.mp3"
            for attempt in range(VOICE_RETRIES):
                try:
                    files[i], secs[i] = dst, await synthesize(texts[i], dst, voice=voice, rate=rate)
                    return
                except KernelError:
                    files[i] = None
                    dst.unlink(missing_ok=True)
                    if attempt + 1 < VOICE_RETRIES:
                        await asyncio.sleep(VOICE_RETRY_DELAY * (attempt + 1))
            failed.append(i + 1)

    await asyncio.gather(*[one(i) for i in range(len(texts))])

    # 失败的那几镜按字数估：中文约 0.22 秒/字（实测 12 字 ≈ 2.5 秒 @+8%）。
    for i in failed:
        secs[i] = round(max(MIN_SHOT_SEC, len(str(texts[i])) * 0.22 + GAP_SEC), 2)
    return {"files": files, "secs": secs, "failed": failed}


def _pad_to(src: Path, dst: Path, seconds: float) -> None:
    """把一段音频**补齐或截断到恰好 seconds**（48k 双声道，便于后面 concat）。"""
    run(["-i", str(src), "-af",
         f"apad=pad_dur=30,atrim=0:{seconds:.3f},asetpts=N/SR/TB",
         "-ar", "48000", "-ac", "2", str(dst)])


def build_track(files: list[Path | None], secs: list[float], dst: Path,
                *, work: Path | None = None) -> Path:
    """按镜长把每段语音铺到时间轴，拼成一条音轨。

    为什么"每段先 pad/trim 到整镜再 concat"而不是"按 offset 混音"：
    前者每一段的长度**就是**镜长，所以音画同步是结构性的——只要 `secs` 与画面
    用的是同一份，就不会出现"越往后越不同步"。混音要算偏移，错一处全片歪。
    """
    work = work or dst.parent
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    for i, sec in enumerate(secs):
        src = files[i] if i < len(files) else None
        piece = work / f"p{i + 1:02d}.wav"
        if src is None:
            run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-t", f"{max(0.1, sec):.3f}", str(piece)])
        else:
            _pad_to(src, piece, max(0.1, sec))
        parts.append(piece)

    listing = work / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    run(["-f", "concat", "-safe", "0", "-i", str(listing),
         "-c:a", "aac", "-b:a", "160k", str(dst)])
    if not dst.exists() or dst.stat().st_size < 1024:
        raise KernelError("voice", "音轨拼接失败（产物为空）")
    return dst


def pick_bgm(seed: str) -> Path | None:
    """**已搬到 `mix.py`**（`mix.pick_local`）。留着这个名字只是因为旧调用点还在用。

    配乐那一整套（挑曲、铺满、闪避、淡入淡出）在 `mix.py`——那边跟旁白没有关系，
    放在同一个文件里只会让两边都改不动。
    """
    from .mix import pick_local
    return pick_local(seed)


#: 成片响度目标。参考片实测 mean −15.9 dB / max −1.4 dB，而我们原来是 −28.6 dB
#: ——差 13 dB，在手机上就是"要开大音量才听得清"。
#: −16 LUFS / 真峰 −1.5 dB 是短视频平台的通行档（抖音约 −14，YouTube −14，播客 −16）。
LOUD_I, LOUD_TP, LOUD_LRA = -16.0, -1.5, 11.0


def mux(video: Path, audio: Path, dst: Path) -> Path:
    """画面 + 音轨 → 成片。**画面不重编码**（`-c:v copy`），所以只花几秒。

    音频这一趟顺手做**响度标准化**（`loudnorm`）：参考片 mean −15.9 dB，
    我们原来 −28.6 dB——同样一条片子，别人听起来"有制作"，我们听起来"声音小"。
    """
    run(["-i", str(video), "-i", str(audio),
         "-c:v", "copy",
         "-af", f"loudnorm=I={LOUD_I}:TP={LOUD_TP}:LRA={LOUD_LRA}",
         "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-movflags", "+faststart", str(dst)], stage="save")
    if not dst.exists() or dst.stat().st_size < 1024:
        raise KernelError("save", "音画合流失败（产物为空）")
    return dst
