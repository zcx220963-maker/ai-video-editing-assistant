"""量两张抽帧的边缘能量，用来定"有没有实体"的阈值。"""
import sys

import numpy as np
from PIL import Image

for p in sys.argv[1:]:
    a = np.asarray(Image.open(p).convert("L"), dtype=np.float32)
    gx = float(np.abs(np.diff(a, axis=1)).mean())
    gy = float(np.abs(np.diff(a, axis=0)).mean())
    vals, counts = np.unique(a.astype(np.int16), return_counts=True)
    bg = int(vals[int(counts.argmax())])
    content = float((np.abs(a - bg) > 20).mean())
    print(f"{p}: edge={(gx+gy)/2:.2f} content={content:.3f} mean={a.mean():.0f}")
