"""配乐卡拍的离线验证：合成点击轨上测速度与拍点精度，再验切点补长的算术。

真 ffmpeg 合成音轨（不联网）；判据是**量出来的数**：速度误差 < 1 BPM、拍点误差 < 1 帧（30fps）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storyline_server import mediaops                       # noqa: E402
from storyline_server.motion import beats as bt             # noqa: E402

WORK = Path(__file__).resolve().parents[1] / ".tmp" / "beats_work"
_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


def _click(path: Path, period: float, first: float, sec: float = 20.0) -> None:
    # 每拍一下 30ms 的 1kHz 短促音，底下垫一层很轻的持续音（模拟伴奏）
    phase = (period - first) % period
    expr = (f"sin(2*PI*1000*t)*lt(mod(t+{phase:.4f},{period:.4f}),0.03)"
            "+0.02*sin(2*PI*220*t)")
    mediaops.ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={sec}", str(path))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    print("\n〔检拍〕两种速度、两种相位：速度误差 < 1 BPM，拍点误差 < 1 帧")
    for bpm, first in ((120.0, 0.37), (90.0, 0.12)):
        period = 60.0 / bpm
        f = WORK / f"click{int(bpm)}.wav"
        _click(f, period, first)
        got = bt.detect_beats(f, work=WORK / "w")
        check(got["bpm"] is not None and abs(got["bpm"] - bpm) < 1.0,
              f"{bpm:g} BPM 测成 {got['bpm']}")
        errs = [abs((b - first) - round((b - first) / period) * period) for b in got["beats"]]
        check(bool(errs) and max(errs) < 1 / 30,
              f"{bpm:g} BPM 拍点最大误差 {max(errs) if errs else None}s < 一帧")

    print("\n〔检不出就不卡〕纯持续音没有拍")
    flat = WORK / "flat.wav"
    mediaops.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=10", str(flat))
    got = bt.detect_beats(flat, work=WORK / "w")
    check(got["beats"] == [], f"持续音不硬猜拍点（{got['reason']}）")

    print("\n〔补长〕切点＝镜尾 − 交叠/2，只往后补、不超过上限、末镜不补")
    beats = [i * 0.5 for i in range(40)]
    pads = bt.snap_pads([2.3, 3.1, 1.8, 2.0], [0.4, 0.4, 0.4], beats)
    check(pads[-1] == 0.0, "末镜不补（后面没有切点）")
    check(all(0.0 <= p <= bt.MAX_PAD_SEC for p in pads), f"补长都在 [0, 上限] 内：{pads}")
    t, cuts = 0.0, []
    for sec, pad, d in zip([2.3, 3.1, 1.8], pads, [0.4, 0.4, 0.4]):
        cuts.append(t + sec + pad - d / 2)
        t += sec + pad - d
    check(all(abs(c / 0.5 - round(c / 0.5)) < 1e-6 for c in cuts),
          f"补完之后每个切点都落在拍上：{[round(c, 3) for c in cuts]}")
    check(bt.snap_pads([1.0, 1.0], [0.0], []) == [0.0, 0.0], "没有拍点＝一镜都不补")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
