# -*- coding: utf-8 -*-
"""dry-run 的两半：出片计划的账 + 渲染闸对它的放行 + 证据分级那本账。

为什么单独一条用例（真机诉求原话「这个工具也不是动态的啊感觉」）：改一版布局要
下载字节、要编码、要等几十秒，才知道排错了。``render_video(dry_run=true)`` 把
「先看账」变成一次不烧像素的调用，于是这一张账本身必须可信——而可信的两件事是：

1. **账算得对**（``render_plan`` 的段数/秒数/空档/盖层落点，逐条对着时间线算）；
2. **它不算一次渲染**（``should_gate_render`` 必须放行它，否则用户问「先看看安排」
   却被弹一道「确认渲染吗」，等于把同一道题问两遍；而真渲那一次必须照拦）。

全程纯函数，不连 PG、不碰 ffmpeg。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework.render_gate import should_gate_render  # noqa: E402
from storyline_server.nodes.core_nodes import (  # noqa: E402
    EVIDENCE_LEVELS, SYNC_TOLERANCE_SEC, _flag_on, _overlay_window_issues,
    evidence_ledger, render_plan,
)

_fails = 0
_checks = 0


def ver(entry: dict) -> bool:
    """账本里「验过」的读法只有一处：status 是那个词表，别在断言里再拼一遍。"""
    return entry["status"] == "verified"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class Call:
    """最简工具调用：只有 name / arguments 两个属性是判据要看的。"""

    def __init__(self, name: str, **arguments) -> None:
        self.name = name
        self.arguments = arguments


def obj_key() -> str:
    # 渲染侧真正吃的是对象键（localize 之后才是本机路径）；计划里只该留下文件名
    return "obj:users/u_pipe/convs/c_pipe/green_node.mp4"


def timeline():
    return {
        "width": 1280, "height": 720, "fps": 25.0, "duration": 10.0,
        "mode": "original_audio",
        # 画面三段：0~4、4~7、7~10
        "events": [
            {"path": obj_key(), "start": 0.0, "end": 4.0},
            {"path": obj_key(), "start": 4.0, "end": 7.0},
            {"path": "obj:users/u/convs/c/broll.mp4", "start": 7.0, "end": 10.0,
             "kind": "transition"},
        ],
        # 声音两段：0~3、5~8 —— 3~5 是空档，8 之后没声音（末尾定格）
        "audio_events": [
            {"path": obj_key(), "start": 0.0, "end": 3.0,
             "src_start": 20.0, "src_end": 23.0},
            {"path": obj_key(), "start": 5.0, "end": 8.0,
             "src_start": 40.0, "src_end": 43.0},
        ],
        "subtitles": [{"text": "第一句"}, {"text": "第二句"},
                      {"text": "第三句"}, {"text": "第四句"}],
        "overlay_events": [
            {"path": obj_key(), "segments": ["asr-1"],
             "start": 5.0, "end": 8.0, "src_start": 0.0, "src_end": 3.0,
             "fit": "contain"},
        ],
        "bgm": {"path": "obj:users/u/convs/c/song.mp3", "volume": 0.18},
    }


def deep_strings(value, out=None):
    out = [] if out is None else out
    if isinstance(value, dict):
        for v in value.values():
            deep_strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            deep_strings(v, out)
    elif isinstance(value, str):
        out.append(value)
    return out


def main() -> int:
    print("\n=== ① render_plan：账要对着时间线算 ===")
    plan = render_plan(timeline())
    check(plan["duration"] == 10.0 and plan["resolution"] == "1280x720",
          f"总长与分辨率（{plan['duration']}s / {plan['resolution']}）")
    check(plan["picture"]["segments"] == 3
          and abs(plan["picture"]["seconds"] - 10.0) < 1e-6,
          f"画面轨 {plan['picture']['segments']} 段共 {plan['picture']['seconds']}s")
    check(plan["voice"]["segments"] == 2
          and abs(plan["voice"]["seconds"] - 6.0) < 1e-6,
          f"人声轨 {plan['voice']['segments']} 段共 {plan['voice']['seconds']}s")
    check(plan["voice"]["gaps"] == [[3.0, 5.0]],
          f"人声空档算出来了（{plan['voice']['gaps']}）——听得到静音的那几秒")
    check(plan["voice"]["reaches"] == 8.0,
          f"人声只铺到 {plan['voice']['reaches']}s（成片 10s，末尾两秒没声音）")
    check(plan["subtitles"]["segments"] == 4 and plan["subtitles"]["sample"] ==
          ["第一句", "第二句", "第三句"],
          "字幕条数 + 只取前 3 条文案（侧栏不被几十条淹没）")
    check(plan["transitions"] == 1, f"转场事件计数（{plan['transitions']}）")

    check(len(plan["overlays"]) == 1, "覆盖层列进计划了")
    ov = plan["overlays"][0]
    check(ov["at"] == [5.0, 8.0] and ov["anchors"] == ["asr-1"],
          f"第 1 层：锚 asr-1 → 盖在成片 {ov['at'][0]}~{ov['at'][1]}s")
    check(ov["src_window"] == [0.0, 3.0] and ov["fit"] == "contain",
          f"取盖层素材自己的 {ov['src_window']} 秒，fit={ov['fit']}")
    check(ov["audio"] is False, "计划如实写着覆盖层静音（它自带音轨就会和口播抢声床）")
    check(plan["bgm"] == {"volume": 0.18, "asset": "song.mp3"},
          f"配乐只露音量与文件名（{plan['bgm']}）")

    leaked = [s for s in deep_strings(plan)
              if s.startswith("obj:") or "\\" in s or (len(s) > 1 and s[1:3] == ":\\")]
    check(not leaked, f"计划里不出现对象键与本机路径（漏出 {leaked}）")
    check(all(o["source"] == Path(obj_key()).name for o in plan["overlays"]),
          "盖层素材以文件名亮相（green_node.mp4），不带目录")

    print("\n=== ② _overlay_window_issues：不碰字节就能判出的问题 ===")
    check(_overlay_window_issues(timeline()) == [], "排得下的时间线不报问题")
    no_path = {"duration": 10.0, "overlay_events": [{"start": 1.0, "end": 2.0}]}
    check("没有 path" in _overlay_window_issues(no_path)[0],
          "没写 path 的层被点名（要盖哪段素材？）")
    no_window = {"duration": 10.0, "overlay_events": [{"path": obj_key()}]}
    issues = _overlay_window_issues(no_window)
    check(len(issues) == 1 and "start/end" in issues[0],
          "既没锚 segments 也没写秒数的层被点名")
    anchored_only = {"duration": 10.0,
                     "overlay_events": [{"path": obj_key(), "segments": ["asr-3"]}]}
    issues = _overlay_window_issues(anchored_only)
    check(len(issues) == 1 and "锚了 segments" in issues[0],
          "只锚了 id、还没解析成秒 → 说的是「解析没跑到」，不冤枉模型漏写")
    over = {"duration": 10.0,
            "overlay_events": [{"path": obj_key(), "start": 8.0, "end": 12.5}]}
    issues = _overlay_window_issues(over)
    check(len(issues) == 1 and "超出成片总长" in issues[0],
          f"盖超出片总长被点名（{issues[0] if issues else ''}）")
    check(len(_overlay_window_issues({"duration": 10.0,
                                      "overlay_events": [{}, {"path": obj_key(),
                                                             "start": 0.0,
                                                             "end": 1.0}]})) == 1,
          "逐层编号从 1 起，只报有问题的那一层")

    print("\n=== ③ should_gate_render：dry_run 不拦，真渲照拦 ===")
    real = Call("render_video", timeline={})
    dry = Call("render_video", dry_run=True, timeline={})
    check(should_gate_render([real], enabled=True) is True,
          "真渲那一次必须拦（不确认绝不渲染）")
    check(should_gate_render([dry], enabled=True) is False,
          "dry_run 不产出成片，拦它等于把「要不要渲」问两遍")
    check(should_gate_render([Call("render_video", dry_run="true")], enabled=True) is False,
          '模型把开关写成字符串 "true" 也算 dry_run')
    check(should_gate_render([Call("render_video", dry_run="false")], enabled=True) is True,
          '字符串 "false" 不许被 bool("false") 读成真——那就绕过了确认门')
    check(should_gate_render([dry, real], enabled=True) is True,
          "同一批里既有 dry 又有真渲 → 仍要拦（放行只看是不是**整批**都不出片）")
    check(should_gate_render([Call("plan_timeline")], enabled=True) is False,
          "别的工具不触发渲染门")
    check(should_gate_render([real], enabled=False) is False,
          "门关着时一律放行（开关在配置里，不在这里硬编）")
    check(should_gate_render([], enabled=True) is False,
          "没有调用时不拦")

    print("\n=== ④ _flag_on：布尔入参的稳妥读法 ===")
    check(all(_flag_on(v) is False for v in
              (False, None, "", "false", "FALSE", "0", "no", "off", "否", "不", "假", [])),
          "「关」的十种写法都读成假（含模型爱写的字符串 false/0/否）")
    check(all(_flag_on(v) is True for v in (True, "true", "True", "1", "yes", "on", [0])),
          "「开」的写法都读成真")

    print("\n=== ⑤ 证据分级账本：没做的那一步不许出现在 verified 里 ===")
    led_dry = evidence_ledger(plan, worst_drift=0.0, resynced=0, bgm=True)
    levels = {e["level"] for e in led_dry}
    check(levels <= set(EVIDENCE_LEVELS),
          f"级别只用词表里的这几种（实际出现 {sorted(levels)}）")
    check(all(e["status"] in ("verified", "UNVERIFIED") for e in led_dry)
          and all(e["claim"] and e["label"] for e in led_dry),
          "每条都有主张、级别名与终态（界面按这三样画，缺一个就是空壳）")
    check(all(not ver(e) for e in led_dry if e["level"] in ("byte", "frame")),
          "只按计划、没下字节也没抽帧时：byte/frame 那几条全是 UNVERIFIED")
    check(all(e["proof"] for e in led_dry if not ver(e)),
          "没验的每条都写了「为什么没验」，不是一句空白的未验")
    check(all(e["status"] == "UNVERIFIED" for e in led_dry
              if e["level"] == "listening"),
          "听感那条永远未验——机器没有听觉")
    human_only = [e for e in led_dry
                  if e["level"] in ("listening", "eyeball")]
    check(len(human_only) == 3
          and all(not ver(e) for e in human_only),
          f"机器压根验不了的 {len(human_only)} 条都在人这一级（人耳/人眼）且永远未验")
    check(not any(e["level"] == "frame" and not ver(e)
                  and ("字幕" in e["claim"] or "对题" in e["claim"])
                  for e in led_dry),
          "「抽帧看过」不替抽帧判不出的主张背书（卡片上会出现 ? 抽帧看过 的自相矛盾）")

    frames_ok = {"at": [0.45, 1.5, 2.55], "luma": [88.2, 90.1, 87.4],
                 "black_indices": [], "distinct": True, "min_luma": 87.4}
    probed = {"duration": 3.0, "width": 640, "height": 360, "has_audio": True}
    led_real = evidence_ledger(plan, worst_drift=0.0, resynced=2, probed=probed,
                               frames=frames_ok)
    frame_claims = [e for e in led_real if e["level"] == "frame" and ver(e)]
    check(len(frame_claims) == 2,
          f"真渲 + 抽帧到位时，像素级证据有两条（{[e['claim'][:12] for e in frame_claims]}）")
    check(any("3.0s" in e["claim"] and ver(e) for e in led_real
              if e["level"] == "byte"),
          "成片的真实时长进了 byte 级证据（ffprobe 量的，不是时间线写的）")
    check(any("钉回 2 段" in e["claim"] for e in led_real),
          "校准自动改回几段也记进证据（否则「同步」是静默修出来的，事后无从核对）")

    led_black = evidence_ledger(plan, worst_drift=0.0, resynced=0, probed=probed,
                                frames={**frames_ok, "black_indices": [0, 1, 2],
                                        "min_luma": 1.2})
    check(not any(ver(e) for e in led_black
                  if e["level"] == "frame" and "黑屏" in e["claim"]),
          "抽帧发现接近全黑时，「不是黑屏」这条落回 UNVERIFIED 并带帧号")
    led_frozen = evidence_ledger(plan, worst_drift=0.0, resynced=0, probed=probed,
                                 frames={**frames_ok, "distinct": False})
    check(not any(ver(e) for e in led_frozen if "定格" in e["claim"]),
          "三帧一模一样时「画面在动」不许自称验过（指纹相同=像定格）")
    led_off = evidence_ledger(plan, worst_drift=1.4, resynced=0, probed=probed,
                              frames=frames_ok)
    check(not any(ver(e) for e in led_off if "音画同步" in e["claim"]),
          f"偏差 {SYNC_TOLERANCE_SEC}s 闸值以上时，同步那条是 UNVERIFIED（1.4s）")
    led_nosnd = evidence_ledger(plan, worst_drift=0.0, resynced=0,
                                probed={**probed, "has_audio": False}, frames=frames_ok)
    check(not any(ver(e) for e in led_nosnd if "声音轨" in e["claim"]),
          "排了声音段却导出无音轨：这条自己认领，不靠人反馈")
    check(all(ver(e) or e["proof"] for e in led_real),
          "整账自洽：要么有证据，要么有「为什么没证据」")

    print(f"\n{'ALL PASSED' if _fails == 0 else f'{_fails} FAILED'} "
          f"({_checks} checks)")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
