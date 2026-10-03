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

import copy
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

    print("\n=== ⑤ 渲染前校准：模型手写的 timeline 也要被掰回来 ===")
    # 真机事故：模型手写整个 timeline 直接交给 render_video（绕过 builder），
    # 它按「句子的镜头时间码」切画面、按「ASR 音频时间码」排声音，两套轴不重合，
    # 每个出镜段偏移各不相同（实测 −4.07 / +1.90 / −33.97 / +6.41 / −14.01 秒…）。
    # 修 builder 修不了这条路，所以渲染前必须再校准一次。
    from storyline_server.nodes.core_nodes import (  # noqa: PLC0415
        resync_original_audio_timeline,
    )

    hand = {
        "mode": "original_audio",
        "events": [
            # 画面按"镜头时间码" 197.92，声音其实是 201.99 起
            {"path": ONCAM, "start": 16.84, "end": 26.07,
             "src_start": 197.92, "src_end": 207.15},
            {"path": BROLL, "start": 26.07, "end": 27.0,
             "src_start": 0.0, "src_end": 0.93},
            # 偏得最狠的一段：−33.97
            {"path": ONCAM, "start": 40.36, "end": 43.52,
             "src_start": 311.24, "src_end": 314.4},
            # 已对齐的不该被动
            {"path": ONCAM, "start": 0.0, "end": 7.72,
             "src_start": 0.0, "src_end": 7.72},
        ],
        "audio_events": [
            {"path": ONCAM, "start": 0.0, "end": 7.72,
             "src_start": 0.0, "src_end": 7.72},
            {"path": ONCAM, "start": 16.84, "end": 40.36,
             "src_start": 201.99, "src_end": 225.51},
            {"path": ONCAM, "start": 40.36, "end": 63.06,
             "src_start": 311.26, "src_end": 333.96},
        ],
    }
    # resync 会**原地**改 hand，所以先把「未校准的原样」留一份给 ⑥ 的闸门用例。
    hand_misaligned = copy.deepcopy(hand)
    n = resync_original_audio_timeline(hand)
    check(n == 2, f"校准了 2 段错位的（实际 {n}）")
    by_start = {e["start"]: e for e in hand["events"]}
    check(abs(by_start[16.84]["src_start"] - 201.99) < 0.01,
          f"197.92 → {by_start[16.84]['src_start']}（按声音钉回来）")
    check(abs(by_start[40.36]["src_start"] - 311.26) < 0.01,
          f"311.24 → {by_start[40.36]['src_start']}（修掉那 −33.97 秒）")
    check(by_start[26.07]["src_start"] == 0.0,
          "空镜段不动（它不是口播素材，没有嘴型可言）")
    check(by_start[0.0]["src_start"] == 0.0, "本来就对齐的不动")
    # 幂等：再跑一遍不该继续改
    check(resync_original_audio_timeline(hand) == 0, "再校准一次是幂等的（0 改动）")

    print("\n=== ⑥ 渲染前的同步闸：认不出的错位不许静默出片 ===")
    # 校准只**修**它认得出的；闸门负责**拦**它认不出的。缺这道闸，模型换一种写法
    # （真机烧了六次 40 轮预算那句「严格音画同步」）就能带着错位出片还自称同步。
    from storyline_server.nodes.core_nodes import av_sync_check  # noqa: PLC0415

    mis = copy.deepcopy(hand_misaligned)
    worst0, bad0 = av_sync_check(mis)
    check(len(bad0) == 1 and 4.0 < worst0 < 4.1,
          f"修之前就能拦下（{len(bad0)} 条违规，最大偏差 {worst0:.2f}s）")
    check(resync_original_audio_timeline(mis) > 0 and not av_sync_check(mis)[1],
          "校准之后同一份时间线无违规（闸与校准对得上，不会互挑刺）")

    blind = {"mode": "original_audio",
             "events": [{"path": ONCAM, "start": 0.0, "end": 5.0,
                         "src_start": 0.0, "src_end": 5.0}],
             "audio_events": []}
    b_bad = av_sync_check(blind)[1]
    check(bool(b_bad) and "没排任何声音段" in b_bad[0],
          f"原声模式却空着声音段 = 盲排，判违规：{b_bad[:1]}")

    gap_tl = {"mode": "original_audio",
              "events": [
                  # 口播素材的画面排在 8.0~15.0，而它的声音只在 0~7.72 与 16.84 之后
                  {"path": ONCAM, "start": 8.0, "end": 15.0,
                   "src_start": 8.0, "src_end": 15.0},
                  # 空镜排在没有它声音的位置——它没有嘴可对，不该报
                  {"path": BROLL, "start": 15.0, "end": 16.84,
                   "src_start": 3.0, "src_end": 4.84},
              ],
              "audio_events": [
                  {"path": ONCAM, "start": 0.0, "end": 7.72,
                   "src_start": 0.0, "src_end": 7.72},
                  {"path": ONCAM, "start": 16.84, "end": 40.36,
                   "src_start": 201.99, "src_end": 225.51},
              ]}
    g_bad = av_sync_check(gap_tl)[1]
    check(len(g_bad) == 1 and "没有它自己的声音在播" in g_bad[0],
          f"只判有嘴型可言的段（出镜落在声音空档），空镜不误报：{g_bad[:1]}")
    check(resync_original_audio_timeline(copy.deepcopy(gap_tl)) == 0,
          "这类错位校准认不出（正是闸存在的理由）")
    worst_b, bad_b = av_sync_check(tl)
    check(not bad_b and worst_b < 0.01,
          f"builder 正常产物放行（最大偏差 {worst_b:.3f}s）")

    print("\n=== ⑦ 成片指纹：把「实际渲染的那一版」记进产物 ===")
    from storyline_server.nodes.core_nodes import timeline_digest  # noqa: PLC0415

    d1 = timeline_digest(tl, max_drift=worst_b, resynced=0)
    d2 = timeline_digest(tl, max_drift=worst_b, resynced=0)
    d3 = timeline_digest(gap_tl, max_drift=8.0, resynced=2)
    check(d1["sha16"] == d2["sha16"] and len(d1["sha16"]) == 16, "同一份时间线指纹稳定")
    check(d1["sha16"] != d3["sha16"], "不同时间线指纹不同")
    check(d1["mode"] == "original_audio" and d1["video_segments"] == len(tl["events"])
          and d1["audio_segments"] == len(tl["audio_events"]),
          f"段数与模式如实入账：{d1['video_segments']} 画面 / {d1['audio_segments']} 声音")
    check(d3["max_av_drift_sec"] == 8.0 and d3["resynced_segments"] == 2,
          "最大偏差与校准段数入账（以后能核对，不用猜）")

    print("\n=== ⑧ 覆盖层锚点：盖画面认「哪句话」，不认手写的秒 ===")
    from storyline_server.nodes.core_nodes import (  # noqa: PLC0415
        resolve_overlay_anchors,
    )

    anchors = [
        {"id": "asr-0", "clip": "m0", "start": 0.0, "end": 7.72, "text": "第一句"},
        {"id": "asr-1", "clip": "m0", "start": 156.81, "end": 165.93, "text": "第二句"},
        {"id": "asr-2", "clip": "m0", "start": 201.99, "end": 225.51, "text": "第三句"},
    ]
    paths = {"m0": ONCAM}

    ov_tl = copy.deepcopy(tl)
    ov_tl["overlay_events"] = [
        {"segments": ["asr-2"], "path": BROLL},
        {"segments": ["asr-0", "asr-1"], "path": BROLL, "fit": "contain"},
        {"start": 0.0, "end": 2.0, "path": BROLL, "src_start": 1.0, "src_end": 3.0},
    ]
    n_ov = resolve_overlay_anchors(ov_tl, anchors, media_paths=paths)
    ovs = ov_tl["overlay_events"]
    # 声音轴：asr-0→0~7.72、asr-1→7.72~16.84、asr-2→16.84~40.36
    check(n_ov == 2, f"解析了 2 层锚点写法（实际 {n_ov}，第 3 层是显式秒数写法）")
    check(abs(ovs[0]["start"] - 16.84) < 0.01 and abs(ovs[0]["end"] - 40.36) < 0.01,
          f"asr-2 → 输出 {ovs[0]['start']}~{ovs[0]['end']} 秒（由声音轨算出，不是手写）")
    check(abs(ovs[1]["start"] - 0.0) < 0.01 and abs(ovs[1]["end"] - 16.84) < 0.01,
          f"多段锚点取并集 {ovs[1]['start']}~{ovs[1]['end']}")
    check(ovs[0]["src_start"] == 0.0 and abs(ovs[0]["src_end"] - 23.52) < 0.01
          and ovs[0]["fit"] == "cover",
          "缺省：盖层从自己源片 0 秒起取满窗口、铺满裁切")
    check(ovs[2]["src_start"] == 1.0 and ovs[2]["end"] == 2.0,
          "写了 start/end 的层不被锚点改写")

    bad_tl = {"mode": "original_audio", "events": [],
              "audio_events": copy.deepcopy(tl["audio_events"]),
              "overlay_events": [{"segments": ["asr-9"], "path": BROLL}]}
    try:
        resolve_overlay_anchors(bad_tl, anchors, media_paths=paths)
        check(False, "不存在的 id 应当报错")
    except ValueError as e:
        msg = str(e)
        check("asr-9" in msg and "asr-0" in msg and "asr_segments[].id" in msg,
              "报错把不认识的 id 和真实存在的 id 一起说出来")

    cut_tl = {"mode": "original_audio", "events": [],
              "audio_events": [a for a in copy.deepcopy(tl["audio_events"])
                               if abs(a["src_start"] - 156.81) > 0.01],
              "overlay_events": [{"segments": ["asr-1"], "path": BROLL}]}
    try:
        resolve_overlay_anchors(cut_tl, anchors, media_paths=paths)
        check(False, "被剪掉的段落应当报错")
    except ValueError as e:
        check("多半已被剪掉" in str(e), f"锚点落在没有声音的窗口 → 直说：{str(e)[:60]}…")

    # 盖层用的是口播素材本身时，它也有嘴型要对——同一道校准/闸门管着它
    cam_ov = {"mode": "original_audio",
              "events": copy.deepcopy(tl["events"]),
              "audio_events": copy.deepcopy(tl["audio_events"]),
              "overlay_events": [{"segments": ["asr-2"], "path": ONCAM}]}
    resolve_overlay_anchors(cam_ov, anchors, media_paths=paths)
    check(resync_original_audio_timeline(cam_ov) == 1,
          "盖层取口播素材时，校准把它的源时刻也钉到声音上")
    o0 = cam_ov["overlay_events"][0]
    check(abs(o0["src_start"] - 201.99) < 0.01, f"钉到声音的源时刻：{o0['src_start']}")
    check(not av_sync_check(cam_ov)[1], "钉完之后闸门放行")
    o0["src_start"] = 10.0
    o0["src_end"] = 15.0
    off_bad = av_sync_check(cam_ov)[1]
    check(bool(off_bad) and "覆盖层" in off_bad[0],
          f"盖层错位同样被拦下：{off_bad[0][:44]}…")

    d_ov = timeline_digest(cam_ov, max_drift=0.0, resynced=1)
    check(d_ov["overlay_segments"] == 1, "成片指纹记下覆盖层层数")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
