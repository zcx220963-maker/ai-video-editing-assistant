"""把两组抽帧拼成左右对比图（左=参考，右=我们）。**按时间等分对齐**。

    docker cp tools/make_compare.py creation-app:/app/tools_make_compare.py
    docker exec creation-app python /app/tools_make_compare.py \
        /tmp/ref-frames2 /tmp/story-frames /tmp/compare/story.jpg

为什么参数化目录：先前把两条片子的帧都写进 `/tmp/ref-frames`，后跑的那条
把前一条覆盖了，结果对比图左右两栏都是自己（踩过一次）。
"""
import subprocess
import sys
from pathlib import Path

PAIRS = 4          # 拼几行
W, H = 480, 270    # 每格尺寸（两条都是 16:9）


def run(args: list[str]) -> None:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        print(" ".join(args)[:100], "→", (p.stderr or "")[:160])


def main() -> int:
    left = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ref-frames2")
    right = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/story-frames")
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "/tmp/compare/compare.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)

    cells: list[Path] = []
    for side in (left, right):
        frames = sorted(side.glob("f*.jpg"))
        if not frames:
            print(f"XX {side} 里没有帧")
            return 1
        step = max(1, len(frames) // PAIRS)
        picked = frames[::step][:PAIRS]
        while len(picked) < PAIRS:
            picked.append(picked[-1])
        for i, f in enumerate(picked):
            dst = out.parent / f"{side.name}-{i}.jpg"
            run(["ffmpeg", "-y", "-v", "error", "-i", str(f), "-vf",
                 f"scale={W}:{H},pad={W + 8}:{H + 8}:4:4:color=black", str(dst)])
            cells.append(dst)

    args = ["ffmpeg", "-y", "-v", "error"]
    for i in range(PAIRS):
        args += ["-i", str(cells[i]), "-i", str(cells[PAIRS + i])]
    fc, rows = [], []
    for i in range(PAIRS):
        fc.append(f"[{i * 2}][{i * 2 + 1}]hstack[r{i}]")
        rows.append(f"[r{i}]")
    fc.append("".join(rows) + f"vstack=inputs={PAIRS}[out]")
    args += ["-filter_complex", ";".join(fc), "-map", "[out]", str(out)]
    run(args)
    print(out, out.exists(), out.stat().st_size if out.exists() else 0)
    return 0 if out.exists() else 1


raise SystemExit(main())
