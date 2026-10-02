# -*- coding: utf-8 -*-
"""音画同步：出镜画面的源时刻必须等于同一墙壁时刻声音正在播的源时刻。

真机事故（用户原话「回到邓紫棋的画面的时候音画不同步啊,不行啊」）：
原实现在画面轨里 `c = speaker_clips[si % len(...)]` 之后直接用它自己的
``start`` 当 ``src_start``。而出镜段的序号 ``si`` **只在用到出镜时才自增**，
与口播段落序号根本不同步，于是画面源时刻一路漂。实测同一墙壁窗 16.84~40.36：

    声音 src = 201.99 ~ 225.51
    画面 src = 197.92 ~ 221.44      ← 早了 4.07 秒，口型对不上

这条用例钉住不变量：**任何墙壁时刻 t 上，若画面取自口播素材，
则画面的 src_start + (t - 画面.start) == 该刻声音的源时刻**。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from storyline_server.nodes.core_nodes import _build_original_timeline  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


ONCAM = "users/u/convs/c/mat-ONCAM.mp4"
BROLL = "users/u/convs/c/mat-BROLL.mp4"


def build():
    """口播三段（源时刻刻意不连续，模拟真机的选段），空镜一段。"""
    rough = [
        {"clip": "m0", "start": 0.0, "end": 7.72, "text": "第一句"},
        {"clip": "m0", "start": 156.81, "end": 165.93, "text": "第二句"},
        {"clip": "m0", "start": 201.99, "end": 225.51, "text": "第三句"},
    ]
    media = {"m0": {"id": "m0", "path": ONCAM, "width": 1920, "height": 1080,
                    "fps": 30.0}}
    groups = [{"group_id": "g1", "clips": [
        {"clip": "b0", "path": BROLL, "start": 0.0, "end": 5.0, "duration": 5.0,
         "width": 1920, "height": 1080, "fps": 30.0}]}]
    return _build_original_timeline(groups, rough, media, None, 0.2, speaker_ratio=0.5)


def main() -> int:
    tl = build()
    check(tl is not None, "时间线构建成功")
    if not tl:
        return 1
    events = tl["events"]
    audios = tl["audio_events"]

    def src_at(wall: float) -> float | None:
        """该墙壁时刻**声音**正在播的源时刻。"""
        for a in audios:
            if a["start"] <= wall < a["end"]:
                return a["src_start"] + (wall - a["start"])
        return None

    print("=== ① 出镜画面与声音逐点对齐 ===")
    oncam = [e for e in events if e["path"] == ONCAM]
    check(bool(oncam), f"画面轨里确实有出镜段（{len(oncam)} 段）")
    worst = 0.0
    for e in oncam:
        # 取段内几个采样点比对
        for k in (0.0, 0.5, 0.9):
            wall = e["start"] + (e["end"] - e["start"]) * k
            want = src_at(wall)
            if want is None:
                continue
            got = e["src_start"] + (wall - e["start"])
            worst = max(worst, abs(got - want))
    check(worst < 0.01,
          f"所有采样点源时刻一致（最大偏差 {worst:.3f}s；修前实测偏 4.07s）")

    print("\n=== ② 具体复现真机那一段 ===")
    # 真机里 16.84~40.36 这截声音播的是原片 201.99 起
    e_here = [e for e in oncam if e["start"] <= 30.0 < e["end"]]
    if e_here:
        e = e_here[0]
        want = src_at(30.0)
        got = e["src_start"] + (30.0 - e["start"])
        print(f"    墙壁 30.0s：画面源={got:.2f}s  声音源={want:.2f}s")
        check(abs(got - want) < 0.01, "该点对齐（修前这里是 4 秒错位）")
    else:
        check(False, "30.0s 处没有出镜段，用例前提不成立")

    print("\n=== ③ 出镜段不跨口播段（跨段会把下一句画面提前放出来）===")
    spans = []
    w = 0.0
    for a in audios:
        spans.append((a["start"], a["end"]))
    bad = []
    for e in oncam:
        inside = any(s0 - 1e-6 <= e["start"] and e["end"] <= s1 + 1e-6
                     for s0, s1 in spans)
        if not inside:
            bad.append((e["start"], e["end"]))
    check(not bad, f"每个出镜段都完整落在某个口播段内（越界 {bad}）")

    print("\n=== ④ 画面轨铺满全长、不重叠 ===")
    t_prev = 0.0
    gaps = []
    for e in events:
        if abs(e["start"] - t_prev) > 0.01:
            gaps.append((t_prev, e["start"]))
        t_prev = e["end"]
    check(not gaps, f"画面事件首尾相接无空洞（{gaps}）")
    check(abs(t_prev - tl["duration"]) < 0.06,
          f"画面轨铺到总时长（{t_prev:.3f} vs {tl['duration']}）")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
