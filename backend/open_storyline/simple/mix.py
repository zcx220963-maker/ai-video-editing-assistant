# -*- coding: utf-8 -*-
"""配乐：挑一首 + 铺满整长 + 闪避到旁白之下。

从 `voice.py` 拆出来的（2026-10-25）。边界是"谁在响"：voice 管旁白（TTS → 定镜长），
mix 管配乐（哪首歌 + 怎么压到旁白之下）。

**"加个背景音乐"其实是四件事，缺一件就出问题**：
1. `amix` 必须 `normalize=0`——缺省会把每个输入除以路数，旁白凭空小 6 dB。
2. **侧链闪避**：旁白响时音乐让路。固定音量只有"盖住人声"和"小到听不见"两种结果。
3. **首尾淡入淡出**：整首剪的歌铺到短片上就是硬切，切点有一声"咔"。
4. **循环接缝无缝化**（见 `_seamless_loop`）：硬接会在接缝处断一下。

`pick_local()` 只读本地目录（`SIMPLE_BGM_DIR`），是**离线兜底**；线上走 `agent_framework/bgm.py`
读 `materials` 表。这一层不许 import storage（边界见 boundary 测试）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .ffmpeg import duration, run
from .kernel import KernelError

#: BGM 音量（相对旁白）。有侧链闪避之后可以给到 0.20；0.12 是"没有闪避时"的保守值。
BGM_GAIN = 0.20
BGM_FADE_IN = 1.5        # 淡入（秒）。0 会让第一帧"啪"一下有音乐。
BGM_FADE_OUT = 2.0       # 淡出（秒）。会被夹到总长的一半以内——4 秒的片子不能淡 2 秒。
BGM_ENV = "SIMPLE_BGM_DIR"   # 本地兜底目录。没有就不加配乐。


def pick_local(seed: str) -> Path | None:
    """按 seed 从 `SIMPLE_BGM_DIR` 里确定性地挑一首。没有就返回 None。

    确定性（按 seed 取模）而不是随机：同一条片子重渲时配乐不该变，否则"重渲看看"就是抽奖。
    """
    root = Path(os.environ.get(BGM_ENV) or "")
    if not root.is_dir():
        return None
    files = sorted(p for p in root.iterdir()
                   if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac"))
    if not files:
        return None
    idx = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(files)
    return files[idx]


#: 交叉淡化宽度的上下限（秒）。接缝无缝化就靠它——见 `_seamless_loop`。
XFADE_MAX, XFADE_MIN, XFADE_RATIO = 2.0, 0.4, 0.03


def _seamless_loop(bgm: Path, work: Path) -> Path:
    """把一首曲子做成**接缝无缝**的循环源（长度 = 原长 − xf）。

    **为什么必须做**（2026-10-27）：原来直接用 `-stream_loop -1` 硬接。实测 90 秒的曲
    铺 175 秒，t=90s 处 RMS 从 -15.5 dB 掉到 -26.7 dB（掉 11 dB），听感是"音乐断了一下"。
    **为什么不串多个 `acrossfade`**：实测串三段只出 86 秒——接在 `acrossfade` 后面会吞掉
    第三段。做法是"把尾巴淡进开头"：`[尾巴][开头]acrossfade` 得 xf 秒过渡块，再接原曲
    中段 → 总长 `原长 − xf`；它末尾正好接得回开头，铺多少遍都不会断。
    """
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


def add_bgm(track: Path, dst: Path, *, total_sec: float, bgm: Path,
            gain: float = BGM_GAIN) -> Path:
    """把 BGM 循环铺到整长，**闪避**到旁白之下，再与旁白混成一条。

    无缝化 → 铺满 → 首尾淡入淡出 → 侧链压缩 → 混音。`[0:a]asplit=2` 是因为旁白要
    **用两次**：一次当键去压音乐，一次进混音。
    """
    total = max(1.0, float(total_sec))
    fade_out = min(BGM_FADE_OUT, total * 0.5)
    out_at = max(0.0, total - fade_out)
    loop = _seamless_loop(bgm, dst.parent)
    run([
        "-i", str(track), "-stream_loop", "-1", "-i", str(loop),
        "-filter_complex",
        f"[1:a]volume={gain},atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={min(BGM_FADE_IN, total * 0.5):.3f},"
        f"afade=t=out:st={out_at:.3f}:d={fade_out:.3f}[bg];"
        f"[0:a]asplit=2[vo][key];"
        f"[bg][key]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[duck];"
        f"[vo][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
        "-map", "[a]", "-c:a", "aac", "-b:a", "160k", str(dst)])
    if not dst.exists() or dst.stat().st_size < 1024:
        raise KernelError("voice", "配乐混音失败（产物为空）")
    return dst
