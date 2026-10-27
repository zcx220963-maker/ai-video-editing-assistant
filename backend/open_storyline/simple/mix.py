# -*- coding: utf-8 -*-
"""配乐：挑一首 + 铺满整长 + 闪避到旁白之下。

从 `voice.py` 拆出来的（2026-10-25）：那边 232 行、已经顶到 250 的硬顶，
再加"闪避 + 淡入淡出"就必然超。拆的边界是"谁在响"：

    voice.py   旁白：TTS → 按语音定镜长 → 铺时间轴 → 与画面合流
    mix.py     配乐：哪首歌 + 怎么压到旁白之下

## "加个背景音乐"其实是三件事

只做 `volume` + `amix` 出来的东西是这样的：旁白一开口音乐就糊住人声，把音乐调小
又变成"全程有一层听不清的嗡"。所以这里三件事缺一不可：

1. **`amix` 必须 `normalize=0`**。缺省 `normalize=1` 会把每个输入除以路数，
   旁白凭空小 6 dB——成片听起来"声音小"，根因常在这里，不在 `loudnorm`。
2. **侧链闪避**（`sidechaincompress`）：旁白响的时候音乐自动让路，说完再抬回来。
   固定音量做不到这件事：它只有"盖住人声"和"小到听不见"两种结果。
3. **首尾淡入淡出**。曲库里的歌是按"整首"剪的，铺到 12 秒的片子上就是在中间硬切，
   切点会有一声"咔"。

## 曲目从哪来

`pick_local()` 只读本地目录（`SIMPLE_BGM_DIR`），是**离线兜底**：
开发机上没曲库、对象存储也没起时，扔几个 mp3 进目录就能出片。
线上走 `agent_framework/bgm.py`（读 `materials` 表里 `origin="bgm"` 的曲库），
取不到再退到这里。这一层不能 import storage——`tests/test_simple_boundary.py`
钉着 `simple/` 不许碰 `storage.repositories`，那条边界挡的是"顺手复用分镜链"的第一步。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .ffmpeg import run
from .kernel import KernelError

#: BGM 音量（相对旁白）。有侧链闪避之后可以给到 0.20：
#: 0.12 是"没有闪避时"的保守值——那时音乐一大就盖人声，所以只能压得很小，
#: 听感上像"音乐没进来"。有闪避之后，人声在的时候它自动让路，不需要这么小。
BGM_GAIN = 0.20
#: 淡入（秒）。0 会让第一帧"啪"一下有音乐。
BGM_FADE_IN = 1.5
#: 淡出（秒）。会被夹到总长的一半以内——4 秒的片子不能淡 2 秒。
BGM_FADE_OUT = 2.0
#: 本地兜底目录。没有就不加配乐。
BGM_ENV = "SIMPLE_BGM_DIR"


def pick_local(seed: str) -> Path | None:
    """按 seed 从 `SIMPLE_BGM_DIR` 里确定性地挑一首。没有就返回 None。

    确定性（按 seed 取模）而不是随机：同一条片子重渲时配乐不该变，
    否则"重渲一遍看看"就变成了在抽奖。
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


def add_bgm(track: Path, dst: Path, *, total_sec: float, bgm: Path,
            gain: float = BGM_GAIN) -> Path:
    """把 BGM 循环铺到整长，**闪避**到旁白之下，再与旁白混成一条。

    滤镜图三步：铺满（`atrim`）→ 淡入淡出 → 用旁白当键做侧链压缩 → 混音。
    `[0:a]asplit=2` 是因为旁白要**用两次**：一次当键去压音乐，一次进混音。
    """
    total = max(1.0, float(total_sec))
    fade_out = min(BGM_FADE_OUT, total * 0.5)
    out_at = max(0.0, total - fade_out)
    run([
        "-i", str(track), "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex",
        f"[1:a]volume={gain},atrim=0:{total:.3f},"
        f"afade=t=in:st=0:d={min(BGM_FADE_IN, total * 0.5):.3f},"
        f"afade=t=out:st={out_at:.3f}:d={fade_out:.3f}[bg];"
        f"[0:a]asplit=2[vo][key];"
        f"[bg][key]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[duck];"
        f"[vo][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
        "-map", "[a]", "-c:a", "aac", "-b:a", "160k", str(dst)])
    if not dst.exists() or dst.stat().st_size < 1024:
        raise KernelError("voice", "配乐混音失败（产物为空）")
    return dst
