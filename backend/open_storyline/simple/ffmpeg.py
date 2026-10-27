# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 的两层薄封装。

为什么单独一个文件：`voice.py` 与 `mix.py` 各抄了一份一模一样的 `_ffmpeg`
（连报错文案都一样）。两份副本的下场是可预见的——改了其中一份的报错分级，
另一份不会跟着变，然后"同一个错在两个地方长得不一样"。

只有两件事：
    run(args)       跑一条 ffmpeg，失败抛 `KernelError`
    duration(path)  读媒体时长（读不出来返回 0.0，**不抛**）

`duration()` 不抛是刻意的：它在 `synthesize()` 里只用来做"产物是不是空壳"的判定，
读不出来时上层自己会报"TTS 产物读不出时长"，不需要这里再抛一次。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .kernel import KernelError


def run(args: list[str], *, stage: str = "voice") -> None:
    """跑一条 ffmpeg 命令。失败时把 stderr 前 200 字带进异常——那是唯一有用的信息。"""
    exe = shutil.which("ffmpeg") or "ffmpeg"
    proc = subprocess.run([exe, "-y", "-v", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise KernelError(stage, f"ffmpeg 失败：{(proc.stderr or '').strip()[:200]}")


def duration(path: Path) -> float:
    """媒体时长（秒）。读不出来返回 0.0。"""
    exe = shutil.which("ffprobe") or "ffprobe"
    proc = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0
