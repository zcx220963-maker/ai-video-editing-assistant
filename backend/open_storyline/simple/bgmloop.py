# -*- coding: utf-8 -*-
"""把一首曲子做成**接缝无缝**的循环源（长度 = 原长 − xf）。

从 `mix.py` 拆出来的（2026-10-28）：那是「挑歌 + 铺满 + 闪避」的编排文件，
而这一段是纯 ffmpeg 滤波图，判据、误差、参数各成一摊，混在一处已经越过单文件上限。

**为什么必须做**（2026-10-27）：原来直接用 `-stream_loop -1` 硬接。实测 90 秒的曲
铺 175 秒，t=90s 处 RMS 从 -15.5 dB 掉到 -26.7 dB（掉 11 dB），听感是"音乐断了一下"。
**为什么不串多个 `acrossfade`**：实测串三段只出 86 秒——接在 `acrossfade` 后面会吞掉
第三段。做法是"把尾巴淡进开头"：`[尾巴][开头]acrossfade` 得 xf 秒过渡块，再接原曲
中段 → 总长 `原长 − xf`；它末尾正好接得回开头，铺多少遍都不会断。
"""

from __future__ import annotations

from pathlib import Path

from .ffmpeg import duration, run

#: 交叉淡化宽度的上下限（秒），按原长比例取中间值。
XFADE_MAX, XFADE_MIN, XFADE_RATIO = 2.0, 0.4, 0.03


def seamless_loop(bgm: Path, work: Path) -> Path:
    """返回接缝无缝的循环源文件；曲子太短（不值得做）时原样返回。"""
    dur = duration(bgm)
    xf = min(XFADE_MAX, max(XFADE_MIN, dur * XFADE_RATIO)) if dur > 0 else 0.0
    if dur <= xf * 3:
        return bgm                      # 太短，不值得做（也避免除零/越界）
    out = work / "bgm-loop.m4a"
    run(["-i", str(bgm), "-filter_complex",
         f"[0:a]atrim=0:{xf:.3f},asetpts=PTS-STARTPTS[head];"
         f"[0:a]atrim={dur - xf:.3f}:{dur:.3f},asetpts=PTS-STARTPTS[tail];"
         f"[0:a]atrim={xf:.3f}:{dur - xf:.3f},asetpts=PTS-STARTPTS[mid];"
         f"[tail][head]acrossfade=d={xf:.3f}:c1=tri:c2=tri[blend];"
         f"[blend][mid]concat=n=2:v=0:a=1[a]",
         "-map", "[a]", "-c:a", "aac", "-b:a", "192k", str(out)])
    return out
