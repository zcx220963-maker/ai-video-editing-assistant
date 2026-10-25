"""量一批抽帧，用来定质检阈值。**这是给阈值做标定用的，不参与生产**。

    python tools/measure_probe.py outputs/simple/y*.jpg

· content —— 与亮度众数差 >20 的像素占比。判"有没有东西"。
· vivid   —— **亮且有色**的像素占比：`max-min ≥ 70` 且 `max ≥ 110`。
             这一条才是"高饱和色块"的口径。用 HSV 的 S=(max-min)/max 会把
             深蓝底(#0a0c12, S=0.44)算成高饱和，于是整片都"很鲜艳"——实测过，没区分力。
· edge    —— 平均梯度（水平+垂直）。判"密不密"。
"""
import sys

import numpy as np
from PIL import Image


def probe(path: str) -> dict[str, float]:
    img = Image.open(path).convert("RGB")
    a = np.asarray(img, dtype=np.float32)
    g = np.asarray(img.convert("L"), dtype=np.float32)

    mx = a.max(axis=2)
    mn = a.min(axis=2)
    chroma = mx - mn

    gx = float(np.abs(np.diff(g, axis=1)).mean())
    gy = float(np.abs(np.diff(g, axis=0)).mean())

    vals, counts = np.unique(g.astype(np.int16), return_counts=True)
    bg = int(vals[int(counts.argmax())])

    return {
        "content": float((np.abs(g - bg) > 20).mean()),
        "vivid": float(((chroma >= 70) & (mx >= 110)).mean()),
        "chroma": float(chroma.mean()),
        "edge": (gx + gy) / 2,
        "lum": float(g.mean()),
    }


if __name__ == "__main__":
    print(f"{'file':<24}{'content':>9}{'vivid':>8}{'chroma':>8}{'edge':>7}{'lum':>6}")
    for p in sys.argv[1:]:
        m = probe(p)
        name = p.replace("\\", "/").rsplit("/", 1)[-1]
        print(f"{name:<24}{m['content']:>9.3f}{m['vivid']:>8.3f}"
              f"{m['chroma']:>8.1f}{m['edge']:>7.2f}{m['lum']:>6.0f}")
