"""文字库偏好 → 出片参数的钉法（`agent_framework/motion_prefs.py`）。

这一节守的是**「挑过就真的算数」**这句话的机制面：写口收了值、库里也存住了，但钉不
进这一轮的出片参数，用户看到的一切都照旧——而那是最难发现的一种坏（界面全对、片子
不变）。所以这里钉四件事：

1. 只收**能钉的那几维**、值必须是非空字符串，脏数据静默忽略而不是丢给下游报错；
2. 覆盖而不是补缺（用户挑过的维度以用户为准，哪怕模型也给了值）；
3. 只对出片通道的两个**整片开关**节点生效，逐镜覆盖不碰；
4. 钉住的每一维**都能在确认卡上看见**——`PINNABLE_KEYS ⊆ MOTION_KNOBS`
   与 `MOTION_NODE ∈ MOTION_KNOB_NODES` 这两条是本文件里最贵的两行：它们掉一个，
   卡上写的就是模型挑的、实际渲的是用户挑的，而卡照样弹得出来。

运行：python tests/test_motion_prefs.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
from dataclasses import dataclass, field
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import motion_prefs as mp
from agent_framework.render_gate import MOTION_KNOBS, MOTION_NODE
from storyline_server.motion import code_spec as cs

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


@dataclass
class FakeCall:
    """够用的假 tool call：只需要 name 与 arguments 两个属性。"""
    name: str
    arguments: Any = field(default_factory=dict)


#: 用户挑过的那几维。2026-10-16 起字幕是**两维**（字幕样式 × 高亮样式），不再是
#: 「形态 + 位置 + 动画」三维——这里跟着换成两维，另一维（模型给的那份）用不同值，
#: 才看得出「覆盖」这条真的在判。
PICKS = {"caption_style": "paper", "highlight_style": "ink",
         "highlight_color": "#ff8a3d",
         "voice": "zh-CN-YunjianNeural", "rate": "+10%"}


def main() -> int:
    print("① 只钉能钉的那几维，脏数据静默忽略")
    # 真机上库里可能留着历史版本写进去的键（比如某一版前端存过 texture）。
    # 静默忽略它，而不是把一个不认识的开关塞进节点参数、让渲染在几分钟后失败。
    dirty = {**PICKS, "texture": "film", "aspect": "landscape"}
    check(mp.pinned_knobs(dirty) == PICKS,
          "整片开关里只有声音与字那几维被收下")
    check(mp.pinned_knobs({}) == {} and mp.pinned_knobs(None) == {}
          and mp.pinned_knobs([]) == {},
          "没挑过 / 不是映射（库里读到字符串、列表）一律回空表，不抛错")
    check(mp.pinned_knobs({"voice": "   "}) == {} and mp.pinned_knobs({"voice": None}) == {},
          "空串与 None 都按「取消这一维」处理（与 set_prefs 的语义同一条）")
    check(mp.pinned_knobs({"voice": "  zh-CN-YunjianNeural  "})
          == {"voice": "zh-CN-YunjianNeural"},
          "值两边的空白摘掉（多一个空格就是另一个音色名，下游认不出来）")

    print("\n② 覆盖，不是补缺")
    calls = [FakeCall("render_motion_code",
                      {"spec": {"shots": []}, "caption_style": "on_picture",
                       "highlight_style": "color", "aspect": "landscape"})]
    pinned = mp.pin_tool_calls(calls, PICKS)
    args = calls[0].arguments
    check(args["caption_style"] == "paper" and args["highlight_style"] == "ink",
          "模型这一轮给的字幕两维被用户挑的那一对盖掉（覆盖，不是补缺）")
    check(args["aspect"] == "landscape"
          and args["spec"] == {"shots": [], "highlight_color": "#ff8a3d"},
          "没挑过的维度一个字节都没动；spec 级那一维（强调色）落进 spec——"
          "卡面上没有它的位置，那儿是它唯一的去处")
    check(args["voice"] == "zh-CN-YunjianNeural" and args["rate"] == "+10%",
          "音色与语速同样盖上")
    check(sorted(set(pinned)) == sorted(PICKS), f"回执把真钉下去的键报出来（给记账用）：{sorted(set(pinned))}")
    check(mp.pin_tool_calls([FakeCall("render_motion_code", {"caption_style": "on_picture"})], {})
          == [],
          "一维都没挑过 → 一个键都不写，回空账")

    print("\n③ 只对出片通道的那两个整片开关节点生效")
    others = [FakeCall(n, {"caption_style": "on_picture"}) for n in
              ("plan_timeline", "render_video", "patch_motion_code", "read_node_history")]
    check(mp.pin_tool_calls(others, PICKS) == []
          and all(c.arguments == {"caption_style": "on_picture"} for c in others),
          "别的节点（含局部改那条路）原样不动")
    check(MOTION_NODE in mp.MOTION_KNOB_NODES,
          f"确认门拦的那个节点（{MOTION_NODE}）在钉的名单里——"
          "漏了它，卡上写的是模型挑的、实际渲的是用户挑的")
    check(mp.MOTION_KNOB_NODES == {"plan_motion", "render_motion_code"},
          f"钉的名单就是这两个整片开关节点：{sorted(mp.MOTION_KNOB_NODES)}")

    print("\n④ 坏形状：参数不是映射就跳过，不硬塞")
    bad = FakeCall("render_motion_code", "caption_style=paper")
    check(mp.pin_tool_calls([bad], PICKS) == [] and bad.arguments == "caption_style=paper",
          "模型把 args 写成字符串时原样留着——下游本来就会报「参数不是对象」，"
          "在这儿塞个字典只会把那句话变成更难认的异常")
    bad_spec = FakeCall("render_motion_code", {"spec": "shots: []"})
    check(mp.pin_tool_calls([bad_spec], PICKS) and "highlight_color" not in bad_spec.arguments
          and bad_spec.arguments["spec"] == "shots: []"
          and bad_spec.arguments["caption_style"] == "paper",
          "args 是映射、但 spec 不是：只跳过 spec 级那一维，其余几维照钉"
          "（没道理为一栏坏形状把另一栏也丢掉）")

    print("\n⑤ 逐镜覆盖不受影响")
    spec = {"shots": [{"id": "s3", "caption_style": "none",
                       "highlight_style": "plain"}]}
    calls = [FakeCall("plan_motion", {"spec": spec, "caption_style": "on_picture"})]
    mp.pin_tool_calls(calls, PICKS)
    check(calls[0].arguments["caption_style"] == "paper"
          and calls[0].arguments["spec"]["shots"][0]["caption_style"] == "none"
          and calls[0].arguments["spec"]["shots"][0]["highlight_style"] == "plain",
          "钉的是整片默认，某一镜在分镜里单独写的那两维仍然赢")

    print("\n⑥ 钉住的每一维都要有去处：卡面（顶层入参）或 spec")
    missing = [k for k in mp.PINNABLE_KEYS
               if k not in MOTION_KNOBS and k not in mp.SPEC_LEVEL_KEYS]
    check(not missing,
          "PINNABLE_KEYS 每一维要么在确认卡的 MOTION_KNOBS 里，要么在 SPEC_LEVEL_KEYS 里"
          f"（两边都不在＝钉了但到不了任何地方）：缺 {missing}")
    # 自由色**不能**上卡面：卡面的每一项都要能反查到枚举源（`plan/vocab.py::enum_source`），
    # 自由色四样都不是，写进参数的后果不是「那一栏渲不出来」，而是**整份计划被打回**。
    check(not (set(mp.SPEC_LEVEL_KEYS) & set(MOTION_KNOBS)),
          f"spec 级那几维不在卡面名单里：{sorted(set(mp.SPEC_LEVEL_KEYS) & set(MOTION_KNOBS))}")
    check(set(mp.PINNABLE_KEYS) == set(mp.pinned_knobs(PICKS)),
          "两份名单同源：PINNABLE_KEYS 就是「挑得动的整片开关」全表")

    print("\n⑦ 契约读不出时不许收值（校验的入口）")
    live = {"dims": {"caption_style": {"enum": list(cs.CAPTION_STYLES)},
                     "highlight_style": {"enum": list(cs.HIGHLIGHT_STYLES)},
                     "voice": {"enum": ["zh-CN-YunjianNeural"]}}}
    names, rules = mp.pref_keys_from_contract(live)
    check(names == ["caption_style", "highlight_style", "voice"],
          f"只认契约里声明过的维度，且按 PINNABLE_KEYS 的次序：{names}")
    check(mp.pref_keys_from_contract({}) == ([], {})
          and mp.pref_keys_from_contract(None) == ([], {}),
          "契约拿不到 → 空，写入口据此拒收（而不是收下一个没法校验的值）")
    got, errs = mp.check_patch({"caption_style": "on_picture"}, names=names, rules=rules)
    check(got == {"caption_style": "on_picture"} and not errs, "合法值收下")
    got, errs = mp.check_patch({"caption_style": "torn_highlight"}, names=names, rules=rules)
    check(not got and len(errs) == 1 and "不在可选值里" in errs[0],
          f"枚举外的取值拒收并说清可选值：{errs}")
    got, errs = mp.check_patch({"texture": "film"}, names=names, rules=rules)
    check(not got and len(errs) == 1 and "不是这里能挑的" in errs[0],
          f"面板管不着的维度拒收（收下了用户永远改不掉）：{errs}")
    got, errs = mp.check_patch({"highlight_style": None}, names=names, rules=rules)
    check(got == {"highlight_style": None} and not errs,
          "None 是「恢复默认」那一档的写法，不是非法值")
    got, errs = mp.check_patch("caption_style=paper", names=names, rules=rules)
    check(not got and errs == ["prefs 必须是对象"], f"整体不是对象时给一句人话：{errs}")

    print("\n⑧ 自由色那一维：收颜色、拒别的，「空」与 None 同义")
    colors = {"dims": {"highlight_color": {"kind": "color", "enum": []}}}
    cnames, crules = mp.pref_keys_from_contract(colors)
    check(cnames == ["highlight_color"] and crules["highlight_color"]["kind"] == "color",
          f"形状从契约现取，不在这里按键名硬编码：{crules}")
    got, errs = mp.check_patch({"highlight_color": "#FF8A3D"}, names=cnames, rules=crules)
    check(got == {"highlight_color": "#ff8a3d"} and not errs,
          f"大小写归一成小写六位（写成两种大小写＝两个值，缓存键会跟着抖）：{got}")
    got, errs = mp.check_patch({"highlight_color": "#F80"}, names=cnames, rules=crules)
    check(got == {"highlight_color": "#ff8800"} and not errs, f"三位缩写也收：{got}")
    for bad in ("红色", "red", "rgb(255,0,0)", "#ff8a3", 12, True):
        got, errs = mp.check_patch({"highlight_color": bad}, names=cnames, rules=crules)
        check(not got and len(errs) == 1 and "不是一条颜色" in errs[0],
              f"{bad!r} 拒收并说清要什么（收下了只会让字幕静默变回缺省色）：{errs}")
    got, errs = mp.check_patch({"highlight_color": None}, names=cnames, rules=crules)
    check(got == {"highlight_color": None} and not errs, "None = 取消这一维（跟色板走）")
    got, errs = mp.check_patch({"highlight_color": "  "}, names=cnames, rules=crules)
    check(got == {"highlight_color": None} and not errs,
          "空串与 None 同义：面板上「跟随色板」那一枚送的就是它")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
