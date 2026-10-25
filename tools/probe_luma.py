# -*- coding: utf-8 -*-
"""量一条成片的亮度/饱和度中位数（signalstats）。awk 没 asort，用 python 算。"""
import statistics
import subprocess
import sys

src = sys.argv[1]
out = subprocess.run(
    ["ffprobe", "-v", "error", "-f", "lavfi", "-i", f"movie={src},signalstats",
     "-show_entries", "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.SATAVG",
     "-of", "csv=p=0"], capture_output=True, text=True).stdout
ys, ss = [], []
for row in out.strip().splitlines():
    p = row.split(",")
    if len(p) >= 2:
        try:
            ys.append(float(p[0]))
            ss.append(float(p[1]))
        except ValueError:
            continue
print(f"frames={len(ys)}  YAVG p50={statistics.median(ys):.1f}  "
      f"min={min(ys):.1f}  max={max(ys):.1f}  SATAVG p50={statistics.median(ss):.1f}")
