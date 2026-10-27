"""配乐节拍检测 + 镜头切点卡拍（不引新依赖：ffmpeg 解码 + numpy）。

为什么自己写而不引 librosa：这条链只要两样东西——整首曲子的拍点时刻、以及「把切点往后
挪到最近的拍上」——librosa 连带 numba/llvmlite 一大串原生依赖，换来的这两样用一百行
numpy 就够。判据是经典的「谱通量起音包络 + 自相关估速度 + 相位搜索」：

1. ffmpeg 解成 11025 Hz 单声道 16-bit PCM；
2. 每 512 点一帧（≈46 ms）做 rfft，取对数幅度谱的**正向**帧差之和＝起音强度包络；
3. 包络去均值后做自相关，在 60~180 BPM 的滞后区间里取峰（按 120 BPM 附近的对数正态先验加权，
   免得把八分音符当成拍、或把两小节当成拍）；
4. 拍周期定下后，在一个周期内搜相位：哪个起点落在包络上的总强度最大；
5. 每个拍点再在 ±10% 周期内就近吸附到包络局部峰，扛住轻微的速度漂移。

包络太弱（纯人声、白噪、静音）就返回空表——**卡拍是增益，检不出就不卡**，绝不硬猜。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .. import mediaops

SR = 11025
HOP = 256
WIN = 1024
BPM_MIN, BPM_MAX = 60.0, 180.0
#: 包络帧号 → 秒的校正：谱通量第 i 格是第 i 与 i+1 帧之差，起音落在窗内时那一格才跳，
#: 用 120/90 BPM 合成点击轨标定（见 tests/test_beats.py）。
ONSET_LAG_SEC = 0.090
#: 起音尖峰度下限（通量 p99 / 中位数）。
PEAKY_MIN = 3.0
#: 卡拍时一镜最多补多长（秒）。只往后补、不裁：补太多观众会觉得这一镜拖。
MAX_PAD_SEC = 0.6


def detect_beats(path: str | Path, *, work: Path, max_sec: float = 600.0
                 ) -> dict[str, Any]:
    """→ ``{"beats": [秒…], "bpm": float | None, "reason": str}``；检不出时 beats 为空。"""
    try:
        import numpy as np
    except Exception:  # noqa: BLE001 - 没有 numpy 就没有卡拍
        return {"beats": [], "bpm": None, "reason": "缺 numpy"}
    work.mkdir(parents=True, exist_ok=True)
    raw = work / "bgm_mono.pcm"
    try:
        mediaops.ffmpeg("-i", str(path), "-t", f"{max_sec:.0f}", "-ac", "1",
                        "-ar", str(SR), "-f", "s16le", str(raw), timeout=120)
        pcm = np.fromfile(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception as exc:  # noqa: BLE001
        return {"beats": [], "bpm": None, "reason": f"解码失败：{type(exc).__name__}"}
    if pcm.size < SR * 4:
        return {"beats": [], "bpm": None, "reason": "曲子太短（< 4 秒）"}

    n = 1 + (pcm.size - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    frames = pcm[idx] * np.hanning(WIN)[None, :]
    mag = np.log1p(np.abs(np.fft.rfft(frames, axis=1)) * 10.0)
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    if flux.size < 64 or float(flux.max()) <= 1e-6:
        return {"beats": [], "bpm": None, "reason": "起音包络太弱"}
    # 尖峰度：有打击感的音乐起音处的通量远高于平常（点击轨 ~3000×、流行乐 ~5~30×），
    # 持续音/纯人声铺底只有 ~1×——那种曲子归一化之后噪声也会长得像拍，必须先拦掉。
    peaky = float(np.percentile(flux, 99)) / max(1e-9, float(np.median(flux)))
    if peaky < PEAKY_MIN:
        return {"beats": [], "bpm": None, "reason": f"没有明显的起音（尖峰度 {peaky:.1f}）"}
    # 去掉慢变的响度趋势（≈0.5 秒滑窗），只留起音尖峰
    k = max(3, int(0.5 * SR / HOP))
    env = flux - np.convolve(flux, np.ones(k) / k, mode="same")
    env = np.maximum(env, 0.0)
    env /= float(env.max()) or 1.0

    fps = SR / HOP
    lag_min = int(round(fps * 60.0 / BPM_MAX))
    lag_max = int(round(fps * 60.0 / BPM_MIN))
    centered = env - env.mean()
    ac = np.correlate(centered, centered, mode="full")[centered.size - 1:]
    if ac[0] <= 0 or lag_max >= ac.size:
        return {"beats": [], "bpm": None, "reason": "自相关不足"}
    lags = np.arange(lag_min, lag_max + 1)
    bpms = 60.0 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    score = ac[lags] / ac[0] * prior
    best = int(lags[int(np.argmax(score))])
    # 节拍感太弱（自相关峰比噪声高不了多少）＝没有稳定的拍，不卡
    if float(ac[best] / ac[0]) < 0.08:
        return {"beats": [], "bpm": None, "reason": "没有稳定的拍"}
    # 抛物线插值求小数滞后：整数滞后在 120 BPM 附近会差出 2%，一首歌拍点就漂掉半拍
    period = float(best)
    if 1 <= best < ac.size - 1:
        y0, y1, y2 = float(ac[best - 1]), float(ac[best]), float(ac[best + 1])
        den = y0 - 2 * y1 + y2
        if den < 0:
            period = best + 0.5 * (y0 - y2) / den

    # 相位：在一个周期内搜起点，按小数周期铺网格取包络总强度最大的那一个
    def grid(o: float) -> np.ndarray:
        return np.round(np.arange(o, env.size - 1, period)).astype(int)

    offsets = np.arange(0.0, period, 0.5)
    offset = float(offsets[int(np.argmax([env[grid(o)].sum() for o in offsets]))])
    reach = max(1, int(period * 0.1))
    beats: list[float] = []
    for c in grid(offset):
        lo, hi = max(0, c - reach), min(env.size, c + reach + 1)
        peak = lo + int(np.argmax(env[lo:hi]))
        beats.append(round(float(peak / fps + ONSET_LAG_SEC), 3))
    return {"beats": [b for b in beats if b >= 0.0],
            "bpm": round(60.0 * fps / period, 1), "reason": ""}


def snap_pads(shot_secs: Sequence[float], seam_secs: Sequence[float],
              beats: Sequence[float], *, max_pad: float = MAX_PAD_SEC) -> list[float]:
    """每镜该往后补多少秒，才能让它的切点落在拍上 → 与 ``shot_secs`` 等长的补长表。

    切点取接缝**中点**：xfade 两镜交叠 d 秒，观众感到的「切」在交叠的正中间，
    所以对齐的是 ``镜尾 − d/2``；硬切时 d=0，就是镜尾本身。最后一镜不补（后面没有切点）。
    只往后补（不裁旁白），补长超过 ``max_pad`` 或一个拍周期的就放弃这一镜（不硬卡）。
    """
    if not beats or not shot_secs:
        return [0.0] * len(shot_secs)
    period = (beats[-1] - beats[0]) / max(1, len(beats) - 1)
    limit = min(max_pad, period * 0.95) if period > 0 else max_pad
    pads: list[float] = []
    t = 0.0
    bi = 0
    for i, sec in enumerate(shot_secs):
        d = float(seam_secs[i]) if i < len(seam_secs) else 0.0
        if i == len(shot_secs) - 1:
            pads.append(0.0)
            break
        cut = t + float(sec) - d / 2.0
        while bi < len(beats) and beats[bi] < cut - 0.02:
            bi += 1
        pad = 0.0
        if bi < len(beats):
            gap = beats[bi] - cut
            if 0.0 <= gap <= limit:
                pad = round(gap, 3)
        pads.append(pad)
        t += float(sec) + pad - d
    return pads
