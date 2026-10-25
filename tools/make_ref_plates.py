"""把参考片在**每个镜头的中点**抽成插画（`s01.jpg`…），供 `plates_dir` 用。

    docker cp tools/make_ref_plates.py creation-app:/app/tools_make_ref_plates.py
    docker exec creation-app python /app/tools_make_ref_plates.py /tmp/ref.mp4 /tmp/plates-ref

为什么这么做：验证"插画叙事"这条通道时，**唯一想排除的变量就是"图从哪来"**。
用参考片自己的帧当插画，等于把出图这一环固定住，剩下的（长镜、缓推、文字层、
压暗、转场、合流）就都能单独验——验过了，缺的就只有出图本身。
真机上这些帧换成出图 API 的结果，其余一行代码不用改。

中点取法：参考片实测切点在 [15.63, 20.67, 25.7, 61.53, 71.6, 89.23, 99.3, 101.8]，
其中 89~91 秒那几个 0.04~0.6 秒的是转场伪切，合并掉，剩下 9 个真镜。
"""
import subprocess
import sys
from pathlib import Path

#: 每个真镜的中点（秒）。改片源时按 `tools/analyze_reference.py` 的切点重算。
MIDS = (7.8, 18.2, 23.2, 43.6, 66.6, 80.4, 95.2, 100.5, 118.4)


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ref.mp4"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/plates-ref")
    out.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(MIDS, start=1):
        dst = out / f"s{i:02d}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", src,
             "-frames:v", "1", "-vf", "scale=1280:720:force_original_aspect_ratio=increase,"
                                       "crop=1280:720", "-q:v", "3", str(dst)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"XX {t}s: {(r.stderr or '')[:160]}")
            continue
        print(f"ok s{i:02d}.jpg @{t}s {dst.stat().st_size // 1024}KB")
    got = len(list(out.glob("s*.jpg")))
    print(f"\n{got} 张 → {out}")
    return 0 if got == len(MIDS) else 1


raise SystemExit(main())
