"""按帧号抽几张图（避免 PowerShell → docker → sh 三层转义把变量吃掉）。

    docker cp tools/grab_frames.py creation-app:/app/tools_grab_frames.py
    docker exec creation-app python /app/tools_grab_frames.py <mp4> <帧号,逗号分隔> <输出前缀>
"""
import subprocess
import sys
from pathlib import Path

src = sys.argv[1] if len(sys.argv) > 1 else "/app/.runtime/simple/story-code.mp4"
frames = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "40,400,1100,2100").split(",")]
prefix = sys.argv[3] if len(sys.argv) > 3 else "/tmp/g"

for f in frames:
    dst = f"{prefix}-{f}.jpg"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf",
                        f"select=eq(n\\,{f}),scale=420:-1", "-frames:v", "1", dst],
                       capture_output=True, text=True)
    print(("ok  " if Path(dst).exists() else "XX  ") + dst
          + ("" if Path(dst).exists() else " " + (r.stderr or "")[:120]))
