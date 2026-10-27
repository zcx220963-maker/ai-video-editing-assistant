# -*- coding: utf-8 -*-
"""口播/素材链「选区改」的合同：命中表 + 指针补丁 + 只重烧受影响窗口（纯函数那一份）。

为什么值得单独钉：这条链最贵的失败模式是**改错了地方还不报错**——指针落错一个字段、
拿着旧表改了新版时间线，像素已经烧完才知道。这类错误只有把判据钉成用例才拦得住，
真机跑一遍看不见（它给的是「看起来正常」的片子）。

覆盖面与 ``timeline_edit`` 模块开头的两条红线一一对应：
① 指针必须逐字取自表、末端必须是白名单里的**已有**字段、段 id 与指针不符整批拒收；
② 一次改动不许悄悄改掉总长与音画对应——所以只接受「不改长度」的编辑，
   改画面段的源区间时**成对平移**、把输出长度按原样还回来。

另有一条只有缓存才有的坑：缓存键**多算**输入只是白烧，**少算**输入是拿旧片冒充新片。
所以这里逐层验一遍「改了它，这一窗的键必须变；没改它，键必须不变」。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

if __name__ != "__main__":
    # 本文件没有 main()，整份脚本在模块层直接执行。pytest 收集阶段会把 tests/test_*.py
    # 当模块导入一次（见 tests/conftest.py 的说明），不挡的话收集期就会跑完检查并 sys.exit，
    # 泼成 INTERNALERROR。所以被当模块导入时整份跳过：检查只走 __main__（直跑或 conftest 子进程）。
    import pytest
    pytest.skip("整份执行的独立脚本，仅 __main__ 下跑检查", allow_module_level=True)

from open_storyline import timeline_edit as te            # noqa: E402
from open_storyline.nodes.core_nodes import (             # noqa: E402
    _fade_index_map, assign_segment_ids,
)

FAILS = 0
CHECKS = 0


def check(cond: bool, label: str) -> None:
    global FAILS, CHECKS
    CHECKS += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILS += 1


def fixture() -> dict:
    """一条 12 秒的原声时间线：3 个画面窗、2 条字幕、1 条盖层、1 条配音、1 段配乐。

    刻意让第 2 条字幕**跨**第 2/3 窗的边界（5.5~10.0 跨过 9.0），这样「改一句字幕要
    重烧两侧窗口」这条判据能被用例抓住，而不是只在纸上成立。
    """
    tl = {
        "width": 640, "height": 360, "fps": 25.0, "duration": 12.0,
        "mode": "original_audio",
        "events": [
            {"path": "users/u/convs/c/mat-A.mp4", "start": 0.0, "end": 4.0,
             "src_start": 10.0, "src_end": 14.0},
            {"path": "users/u/convs/c/mat-A.mp4", "start": 4.0, "end": 9.0,
             "src_start": 20.0, "src_end": 25.0},
            {"path": "users/u/convs/c/mat-B.mp4", "start": 9.0, "end": 12.0,
             "src_start": 0.0, "src_end": 3.0},
        ],
        "audio_events": [
            {"path": "renders/s/vo-1.mp3", "start": 0.0, "duration": 9.0,
             "group_id": "g1"},
        ],
        "subtitles": [
            {"text": "第一句", "start": 0.0, "end": 4.0, "style": "subtitle_clean"},
            {"text": "第二句，这一条横跨两个窗口", "start": 5.5, "end": 10.0,
             "style": "subtitle_clean"},
        ],
        "overlay_events": [
            {"path": "users/u/convs/c/mat-C.png", "start": 5.0, "end": 7.0,
             "fit": "contain", "src_start": None, "src_end": None},
        ],
        "bgm": {"path": "users/u/convs/c/mat-music.mp3", "volume": 0.2,
                "offset_sec": 1.0},
        "transition_styles": [],
    }
    assign_segment_ids(tl)
    return tl


def keys_by_window(tl: dict) -> dict[str, str]:
    return {sid: key for sid, _s, _e, _i, key in te.windows(tl)}


def inputs_by_window(tl: dict) -> dict[str, dict]:
    return {sid: i for sid, _s, _e, i, _k in te.windows(tl)}


# ---------------------------------------------------------------------------
# ① 窗口：切在画面段的边界上，跨界的层不另开窗口
# ---------------------------------------------------------------------------

tl = fixture()
bounds = te.window_boundaries(tl)
check(bounds == [(0.0, 4.0), (4.0, 9.0), (9.0, 12.0)],
      f"窗口只按画面段边界切（实得 {bounds}）")
check(len(te.windows(tl)) == 3, "字幕/盖层/配音跨界不额外开窗（否则未改的段也会被切碎）")
check(te.window_id_at(tl, 4.0) == str(tl["events"][1]["id"]),
      "窗口名 = 起点上那一格画面段的 id（表、日志、缓存能按同一名字对上）")

# 结尾没有画面段覆盖时补一段，别把尾巴丢了
tail = copy.deepcopy(tl)
tail["duration"] = 14.0
check(te.window_boundaries(tail)[-1] == (12.0, 14.0),
      "总长比最后一段画面长 → 尾巴补成一窗（否则那两秒既不进表也不进缓存）")

# ---------------------------------------------------------------------------
# ② 缓存键：改了的那一窗必须变，没改的必须不变
# ---------------------------------------------------------------------------

base_keys = keys_by_window(tl)
check(len(set(base_keys.values())) == 3, "三窗三把键（键里吃的是这一窗自己的输入）")

a = copy.deepcopy(tl)
a["subtitles"][0]["text"] = "第一句改过了"
assign_segment_ids(a)
ka = keys_by_window(a)
check(ka[te.window_id_at(tl, 0.0)] != base_keys[te.window_id_at(tl, 0.0)],
      "改第一窗里的字幕 → 第一窗的键变")
check(ka[te.window_id_at(tl, 9.0)] == base_keys[te.window_id_at(tl, 9.0)],
      "同一处改动没碰第三窗 → 第三窗的键不动（可以整窗复用字节）")

b = copy.deepcopy(tl)
b["subtitles"][1]["text"] = "第二句改过了"     # 5.5~10.0 横跨 4~9 与 9~12 两窗
assign_segment_ids(b)
kb = keys_by_window(b)
check(kb[te.window_id_at(tl, 4.0)] != base_keys[te.window_id_at(tl, 4.0)]
      and kb[te.window_id_at(tl, 9.0)] != base_keys[te.window_id_at(tl, 9.0)],
      "跨边界的字幕改动 → 它压着的**两**窗都换键（只换一窗就是接缝另一侧算了旧字）")
check(kb[te.window_id_at(tl, 0.0)] == base_keys[te.window_id_at(tl, 0.0)],
      "（对照）第一窗不含这条字幕，键不动")

c = copy.deepcopy(tl)
c["events"][2]["src_start"] = 1.5
c["events"][2]["src_end"] = 4.5
assign_segment_ids(c)
wins_c = te.windows(c)
check(wins_c[2][4] != list(base_keys.values())[2],
      "改第三窗的画面取区间 → 那一窗换键")
check([w[4] for w in wins_c[:2]] == list(base_keys.values())[:2],
      "（对照）改源区间只牵连它自己那一窗，前两窗的键一字不差")
check(wins_c[2][0] != tl["events"][2]["id"],
      "取区间变了 → 那一段的内容 id 随之换（指针与旧缓存都认不到它，正是该有的行为）")

d = copy.deepcopy(tl)
d["bgm"]["volume"] = 0.35
kd = keys_by_window(d)
check(all(kd[w] != base_keys[w] for w in base_keys),
      "换配乐音量 → 每一窗都换键（配乐混在每一窗自己的声音里，漏算就是拿旧配乐冒充）")

e = copy.deepcopy(tl)
e["overlay_events"][0]["fit"] = "cover"
ke = keys_by_window(e)
check(ke[te.window_id_at(tl, 4.0)] != base_keys[te.window_id_at(tl, 4.0)],
      "改盖层的 fit → 它所在窗口换键（fit 影响这一窗的像素）")

f = copy.deepcopy(tl)
f["duration"] = 13.0          # 只在总长里，没有任何层吃它
check(keys_by_window(f)[te.window_id_at(tl, 0.0)] == base_keys[te.window_id_at(tl, 0.0)],
      "没层可吃的字段变化不牵连已有窗口（键认输入，不认文档里多写的数）")

# 窗口内的相对坐标：同一段内容挪到片子的别处，窗口输入应当跟着平移而不是变味
g = copy.deepcopy(tl)
g["overlay_events"][0]["start"] = 6.0
g["overlay_events"][0]["end"] = 8.0
gi_old = inputs_by_window(tl)[te.window_id_at(tl, 4.0)]
gi_new = inputs_by_window(g)[te.window_id_at(g, 4.0)]
old_at = {str(x["id"]): (x["at"], x["to"]) for x in gi_old["overlays"]}
new_at = {str(x["id"]): (x["at"], x["to"]) for x in gi_new["overlays"]}
check(old_at != new_at, "盖层挪了位置 → 窗口输入里的相对时刻跟着变（键随之变）")

# 源时刻按窗口起点平移：跨窗的层不能把「源片第 5 秒」当成「本窗第 5 秒」用
h = te.window_inputs(tl, 5.0, 9.0)
layer = next(x for x in h["subs"] if str(x["id"]) == str(tl["subtitles"][1]["id"]))
check(layer["at"] == 0.5 and layer["src_start"] is None,
      f"字幕在窗内的相对起点 = 5.5-5.0（实得 at={layer['at']}）")
win_tail = te.window_inputs(tl, 9.0, 12.0)
sub_tail = next(x for x in win_tail["subs"] if str(x["id"]) == str(tl["subtitles"][1]["id"]))
check(abs(sub_tail["at"] - 0.0) < 1e-9 and sub_tail["to"] == 1.0,
      f"跨进第三窗的字幕只留它在这一窗的那一截（实得 {sub_tail['at']}~{sub_tail['to']}）")

# ---------------------------------------------------------------------------
# ③ 命中表：自足、形状与图形科普片那条一致、字幕带标成「估的」
# ---------------------------------------------------------------------------

frames = {str(tl["events"][0]["id"]): "renders/s/a/frames/f0.png"}
table = te.build_table(tl, frames=frames, cache_keys=base_keys,
                       fade_styles={}, errors=[])
check(table["schema"] == "segment-hitmap/1", "表带 schema 版本")
check(table["hit_mode"] == "track",
      "hit_mode=track：这条链没有空间命中层，前端据此关掉框选")
check(table["doc"] == tl and table["doc_fingerprint"] == te.fingerprint(tl),
      "表里嵌着**这一版**时间线与它的指纹（补丁那一头据此拒旧表）")
check([t["key"] for t in table["tracks"]] == ["events", "audio_events",
                                             "subtitles", "overlay_events", te.BGM_TRACK],
      "轨道顺序与标签单源（前端一份面板服务两条链，键名不能各写一套；配乐伪轨排在最后）")
check(sum(t["rows"] for t in table["tracks"]) == len(table["timeline"]) == 8,
      f"每段一行（events3+audio1+subs2+overlay1+bgm1=8，实得 {len(table['timeline'])}）")
check(set(table["shots"]) == {str(x["id"]) for k in te.TRACK_LABELS
                              for x in tl[k]} | {te.BGM_TRACK},
      "shots 按段 id 建索引，四轨的段与配乐都在（前端点配乐 lane 也要能落到指针）")
check(all(e["confidence"] == "exact"
          for shot in table["shots"].values() for e in shot["entries"]),
      "指针恒为精确（框与字段都由数据直接给出，没有量错像素的可能）")
sub_entries = [e for e in table["shots"][str(tl["subtitles"][0]["id"])]["entries"]]
check(all(e["box_precision"] == "estimated" for e in sub_entries),
      "字幕带的高度是排版估算 → 单独标在 box_precision 上，不冒充量过的数字")
ev_entries = table["shots"][str(tl["events"][0]["id"])]["entries"]
check(all(e["box_precision"] == "derived" for e in ev_entries)
      and all(e["box"] == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0} for e in ev_entries),
      "画面段占满舞台：框由数据给出（derived）而不是声称量过")
check({e["field"].rsplit("/", 1)[1] for e in ev_entries} == set(te.EDITABLE["events"]),
      "一段的条目 = 白名单里的那几个字段，一格一条（前端不再自己拆表单）")
check(all(e["field"] == f"/events/0/{e['field'].rsplit('/', 1)[1]}"
          for e in ev_entries),
      "条目里的 field 就是可直接提交的指针原文（调用方不必自己拼字段名）")
check(sum(1 for r in table["timeline"] if r["frame"]) == 1
      and sum(1 for r in table["timeline"] if r["cache_key"]) == 3,
      "代表帧只挂在传了 frames 的那一段，其余行留空串（前端据此显示「这一格没有帧」）")
row0 = next(r for r in table["timeline"] if r["id"] == str(tl["events"][0]["id"]))
check(row0["frame"] == frames[row0["id"]] and row0["cache_key"] == base_keys[row0["id"]],
      "行里带回指的代表帧与缓存键（缩略图与「这窗有没有字节」都不必再问后端）")

box = te.subtitle_box(tl, tl["subtitles"][1])
check(0 <= box["x"] and box["x"] + box["w"] <= 1.0001
      and 0 <= box["y"] and box["y"] + box["h"] <= 1.0001,
      f"字幕估算框归一化且不出舞台（{box}）")

# ---------------------------------------------------------------------------
# ④ 指针与白名单：改错地方还不报错，是这条链最贵的失败模式
# ---------------------------------------------------------------------------

p_sub = f"/subtitles/0/text"
check(te.read_pointer(tl, p_sub) == "第一句", "read_pointer 取的就是表里那一格")
try:
    te.read_pointer(tl, "/subtitles/9/text")
    check(False, "指针越界必须抛")
except KeyError:
    check(True, "指针越界必须抛（不静默回 None）")

ok = te.apply_patch(tl, p_sub, "改成这样")
check(ok["subtitles"][0]["text"] == "改成这样", "改字幕文本：新对象里值生效")
check(tl["subtitles"][0]["text"] == "第一句", "补丁**不改入参**（时间线是已入库的产物）")

for bad, why in [
    ("/events/0/start", "画面段的输出区间是排出来的结果，不是旋钮"),
    ("/events/0/end", "同上：拧它等于把后面所有段连声轨一起挪走"),
    ("/audio_events/0/start", "口播声音是整条时间线的时钟基准"),
    ("/subtitles/0/enable", "末端不是白名单字段"),
    ("/events/0/duration", "白名单里没有的字段"),
    ("/timeline/0/path", "轨道名不在四轨里"),
    ("events/0/text", "少了开头的斜杠"),
    ("/subtitles/text", "少了下标"),
    ("/subtitles/0", "指针没落到字段上"),
]:
    try:
        te.apply_patch(tl, bad, 1.0)
        check(False, f"拒收 {bad}（{why}）")
    except ValueError:
        check(True, f"拒收 {bad}：{why}")

try:
    te.apply_patch(tl, "/subtitles/0/style", "   ")
    check(False, "空字符串必须拒（放过就等于「改了个看不见的值」）")
except ValueError:
    check(True, "字符串字段传空白 → 拒收")
try:
    te.apply_patch(tl, "/subtitles/0/start", "不是数字")
    check(False, "数字字段传非数字必须拒")
except ValueError:
    check(True, "数字字段传非数字 → 拒收")
try:
    te.apply_patch(tl, "/subtitles/0/start", True)
    check(False, "bool 当数字要拒（True 会写进时间线并静默改成 1 秒）")
except ValueError:
    check(True, "bool 不当数字用 → 拒收")
try:
    te.apply_patch(tl, "/subtitles/5/text", "x")
    check(False, "越界指针必须拒")
except ValueError:
    check(True, "越界指针 → 拒收并说明这条时间线只有几段")

# 新增字段冒充已有字段：蒙一个不存在的键会渲染出一个什么都不吃的输入
try:
    te.apply_patch(tl, "/events/0/kind", "vo_fill")
    check(True, "kind 允许（它本来就可能在段里缺省，白名单里点名了它）")
except ValueError as exc:
    check(False, f"kind 应在白名单内：{exc}")
try:
    te.apply_patch({"events": [{"path": "a", "start": 0.0, "end": 1.0}],
                    "duration": 1.0}, "/events/0/src_end", 2.0)
    check(False, "末端不是已有字段 → 必须拒（别蒙一个不存在的键）")
except ValueError:
    check(True, "末端不是已有字段 → 拒收（新增字段请整段改写）")

# 段号与指针不符 = 拿着旧表改新片，整批拒收
try:
    te.apply_patch(tl, "/subtitles/0/text", "x",
                   expect_id=str(tl["subtitles"][1]["id"]))
    check(False, "指针与段号不符必须整条拒")
except ValueError as exc:
    check("重新出片" in str(exc), f"指针与段号不符 → 拒收并给出路（{str(exc)[:60]}…）")
te.apply_patch(tl, "/subtitles/0/text", "x", expect_id=str(tl["subtitles"][0]["id"]))
check(True, "段号对得上就放行")

# 改源区间：成对平移、输出长度原样还回来
moved = te.apply_patch(tl, "/events/1/src_start", 22.0)
ev = moved["events"][1]
check(abs((ev["src_end"] - ev["src_start"]) - (ev["end"] - ev["start"])) < 0.01,
      f"改 src_start → src_end 跟着平移，取的区间长度不变"
      f"（实得 {ev['src_start']}~{ev['src_end']}）")
check(ev["start"] == 4.0 and ev["end"] == 9.0,
      "画面段在成片里占多长**没被动**（改了它就要重排后面每一段）")
moved2 = te.apply_patch(tl, "/events/1/src_end", 26.0)
check(abs(moved2["events"][1]["src_start"] - 21.0) < 0.01,
      f"改 src_end → src_start 反向平移（实得 {moved2['events'][1]['src_start']}）")

# ---------------------------------------------------------------------------
# ⑤ 一批补丁：空批拒、报告口径、以及「应当重烧哪几窗」
# ---------------------------------------------------------------------------

try:
    te.plan_patch(tl, edits=[])
    check(False, "空补丁必须拒")
except ValueError as exc:
    check("不必分叉" in str(exc), f"空补丁 → 拒（放过就会多一版没人改过的片子）")
try:
    te.plan_patch(tl, edits=["不是对象"])
    check(False, "补丁条目非对象必须拒")
except ValueError:
    check(True, "补丁条目非对象 → 拒")

doc2, report = te.plan_patch(tl, edits=[
    {"pointer": "/subtitles/0/text", "value": "第一句改过了",
     "segment_id": str(tl["subtitles"][0]["id"])},
    {"pointer": f"/events/2/src_start", "value": 1.5,
     "segment_id": str(tl["events"][2]["id"])},
])
check(report["edits"] == 2, "报告记下这次一共几格")
check(sorted(report["changed_ids"]) == sorted([str(tl["subtitles"][0]["id"]),
                                               str(tl["events"][2]["id"])]),
      f"changed_ids 就是被点名的那两段（实得 {report['changed_ids']}）")
check(report["reordered"] is False and report["removed_ids"] == [],
      "本工具不改顺序也不删段（改了就不是选区改）")
check(report["changed"]["subtitles"] and report["changed"]["events"],
      "changed 按轨分组，读的人能一眼看出动了哪几轨")

assign_segment_ids(doc2)          # 与真节点同：改完按内容重打段 id
expected = te.expected_rebuild_ids(tl, doc2, fade=_fade_index_map(tl),
                                   new_fade=_fade_index_map(doc2))
check(set(expected) == {str(tl["events"][0]["id"]), str(doc2["events"][2]["id"])},
      f"应当重烧 = 第一窗（字幕在那儿）+ 第三窗（改了取区间，id 也随之换）"
      f"（实得 {expected}）")
check(str(tl["events"][1]["id"]) not in expected,
      "第二窗没被点名、输入也没变 → 不在重烧名单里")

# 跨窗改动要在「应当重烧」里也覆盖两侧
doc3, _ = te.plan_patch(tl, edits=[
    {"pointer": "/subtitles/1/text", "value": "横跨的这条改了",
     "segment_id": str(tl["subtitles"][1]["id"])}])
exp3 = te.expected_rebuild_ids(tl, doc3, fade={}, new_fade={})
check({str(tl["events"][1]["id"]), str(tl["events"][2]["id"])} <= set(exp3),
      f"改一条跨窗字幕 → 两侧窗口都在重烧名单（实得 {exp3}）")

# 转场参与缓存：改转场样式等于改这一窗的像素
tl_fade = copy.deepcopy(tl)
tl_fade["transition_styles"] = [{"group_id": tl["events"][1].get("group_id") or "g1",
                                 "style": "fade"}]
fade_map = {1: "fade"}
k_with = te.windows(tl, fade=fade_map)
k_without = te.windows(tl, fade=None)
same_id = str(tl["events"][1]["id"])
check(dict((s, k) for s, _a, _b, _c, k in k_with)[same_id]
      != dict((s, k) for s, _a, _b, _c, k in k_without)[same_id],
      "同一窗、只有转场不同 → 缓存键不同（漏算转场会拿无转场的旧切片冒充）")

# 指纹：表与版本必须成对
check(te.fingerprint(tl) == te.fingerprint(fixture()),
      "同一份时间线两次构造指纹一致（表知道自己量的是哪一版）")
drift = copy.deepcopy(tl)
drift["subtitles"][0]["text"] = "只有字幕变了一字"
check(te.fingerprint(drift) != te.fingerprint(tl),
      "内容一变指纹就变（旧表据此被拒，不会改到别的段上）")

# ---------------------------------------------------------------------------
# ⑦ 配乐伪轨：/bgm/… 没有下标，段号恒 "bgm"
# ---------------------------------------------------------------------------

bg = te.apply_patch(tl, "/bgm/volume", 0.6)
check(bg["bgm"]["volume"] == 0.6 and tl["bgm"]["volume"] == 0.2,
      "改配乐音量落到新对象上、不动入参（时间线是已入库的产物）")
check(bg["bgm"].get("manual") is None,
      "配乐是整片一条，不属于「某一段被手改」——不打段级 manual")
try:
    te.apply_patch(tl, "/bgm/volume", 0.6, expect_id="au-xyz")
    check(False, "配乐指针带错段号必须拒（拿旧表改新片的老路）")
except ValueError as exc:
    check("段号对不上" in str(exc), f"配乐指针也吃 expect_id：{str(exc)[:70]}")
no_bgm = {k: v for k, v in tl.items() if k != "bgm"}
try:
    te.apply_patch(no_bgm, "/bgm/volume", 0.6)
    check(False, "没有配乐却改 /bgm/… 必须拒")
except ValueError as exc:
    check("没有配乐" in str(exc) and "select_BGM" in str(exc),
          f"拒收要说清出路（加曲子归计划卡管）：{str(exc)[:80]}")
try:
    te.apply_patch(tl, "/bgm/pitch", 1.2)
    check(False, "配乐白名单外的字段必须拒")
except ValueError as exc:
    check("不在可改白名单" in str(exc), f"配乐字段白名单生效：{str(exc)[:60]}")

table = te.build_table(tl)
bg_rows = [r for r in table["timeline"] if r["track"] == "bgm"]
check(len(bg_rows) == 1 and bg_rows[0]["id"] == "bgm"
      and bg_rows[0]["start_sec"] == 1.0,
      f"命中表里有配乐这一行，起点吃 offset_sec（实得 {bg_rows}）")
check("bgm" in [t["key"] for t in table["tracks"]],
      "tracks 清单带配乐轨——前端据此画第四条 lane，不是自己数出来的")
fields = sorted(e["field"] for e in table["shots"]["bgm"]["entries"])
check(fields == ["/bgm/offset_sec", "/bgm/path", "/bgm/volume"],
      f"配乐三个旋钮各一条可点条目（实得 {fields}）")
check(table["editable_fields"]["bgm"] == {"offset_sec": "number", "path": "string",
                                         "volume": "number"}
      and table["editable_fields"]["subtitles"]["position"] == "enum"
      and table["editable_fields"]["subtitles"]["color"] == "color"
      and table["controls"]["volume_max"] == te._VOLUME_MAX,
      "字段连**类型**与取值范围一起发表：缺省的 volume 原值是 None，前端只能按类型画数字框"
      "（按原值猜就会把音量写成字符串，入口拒收等于「这一格改不动」）；"
      "范围也同源——表单不许在前端重写第二份数字")

# 未设置的栏要把**渲染真正吃进去的那个默认值**一起发出去：真机栽过——滑杆把没写的音量画成
# 最小值 0、色块把没写的字幕色画成黑，而片子里是 1.0× 白字，读数本身就是假事实。
by_field = {e["field"]: e for s in table["shots"].values() for e in s["entries"]}
check(by_field["/subtitles/0/color"]["value"] is None
      and by_field["/subtitles/0/color"]["default"] == "white"
      and by_field["/subtitles/0/position"]["default"] == "bottom",
      "字幕没写颜色/位置 → 表发 white、bottom（与 core_nodes 的 or 缺省同源）")
check(by_field["/subtitles/0/font_size"]["default"]
      == te.subtitle_font_size(tl, tl["subtitles"][0]),
      f"没写字号 → 发排版公式算出的那一个（实得 {by_field['/subtitles/0/font_size']['default']}）")
check(by_field["/audio_events/0/volume"]["default"] == 1.0,
      f"某段声音没写音量 → 发 1.0×（实得 {by_field['/audio_events/0/volume']['default']}）")
check(by_field["/bgm/volume"]["value"] == 0.2 and by_field["/bgm/volume"]["default"] == 0.2,
      "配乐写了音量就用它自己的值，default 只是那一栏缺省时的兜底")

bg_quiet = te.apply_patch(tl, "/bgm/volume", 0.05)
check(set(keys_by_window(bg_quiet).items()) != set(base_keys.items()),
      "改配乐音量 → 缓存输入真的变了（配乐混在每一窗自己的声音里，漏算就是拿旧配乐冒充）")

# ---------------------------------------------------------------------------
# ⑧ 逐段音量与源区间裁切：改了哪一段、动了多少字节，都要有账
# ---------------------------------------------------------------------------

crop = te.apply_patch(tl, "/audio_events/0/src_start", 2.0)
au0 = crop["audio_events"][0]
check(au0["src_start"] == 2.0 and au0["src_end"] == 11.0,
      f"声音段裁源区间也成对生效、长度按原样还回来（实得 {au0['src_start']}~{au0['src_end']}）")
check(au0["start"] == 0.0 and au0["duration"] == 9.0,
      "裁切不动输出区间与时长：口播的时钟基准因此没被一次选区改挪走")
check(au0.get("manual") == te.MANUAL_MARK,
      "被亲手拧过的声音段要打标——不打标，校准会把它钉回去、闸会替它解释")

# 把手拖过头：编辑器只发**一格**指针。
# (a) 两端都写过（crop 这份是 2.0~11.0）：把 src_start 拖到另一端上/越过它 → 新长度
#     已经是 0 甚至负数。放过它就落出一段零长度的源段，渲到那儿 MoviePy 合成音频时抛
#     「zero-size array to reduction operation minimum which has no identity」——崩在渲染
#     深处，而补丁已经落库，用户看到的是「改坏了但不知道哪一步」。
pinched = te.apply_patch(crop, "/audio_events/0/src_start", 11.0)
check(pinched["audio_events"][0]["src_start"] == 11.0
      and pinched["audio_events"][0]["src_end"] == 20.0,
      f"拖到另一端（零长度）也必须按原长度平移回去，不能留 11.0~11.0 "
      f"（实得 {pinched['audio_events'][0]['src_start']}~{pinched['audio_events'][0]['src_end']}）")
over = te.apply_patch(crop, "/audio_events/0/src_start", 12.0)
check(round(over["audio_events"][0]["src_end"]
            - over["audio_events"][0]["src_start"], 3) == 9.0,
      f"拖过界（负长度）同样是原长度平移（实得 {over['audio_events'][0]['src_start']}"
      f"~{over['audio_events'][0]['src_end']}）")
# (b) 另一端从没写过：补出来的值本身可能是负秒（这段长 9 秒，只给 src_end=1.0 → -8.0）。
left = te.apply_patch(tl, "/audio_events/0/src_end", 1.0)
check(left["audio_events"][0]["src_start"] == 0.0
      and left["audio_events"][0]["src_end"] == 9.0,
      f"往左顶到负起点要整段顶回 0：源时间不能是负数，也不能因此改长度"
      f"（实得 {left['audio_events'][0]['src_start']}~{left['audio_events'][0]['src_end']}）")
check(left["audio_events"][0]["duration"] == 9.0,
      "顶回负起点同样不动输出时长")
try:
    te.apply_patch(tl, "/audio_events/0/volume", 2.5)
    check(False, "音量越界必须拒（倍率不是百分比）")
except ValueError as exc:
    check("超出可取范围" in str(exc), f"音量范围在入口就说清：{str(exc)[:60]}")
quiet = te.apply_patch(tl, "/audio_events/0/volume", 0.8)
check(quiet["audio_events"][0]["volume"] == 0.8
      and keys_by_window(quiet) != base_keys,
      "逐段音量真的进了缓存输入（没算进去就是拿旧响度的切片冒充新的）")
check("duration" in te.window_inputs(quiet, 0.0, 4.0)["audio"][0]
      and te.window_inputs(quiet, 0.0, 4.0)["audio"][0]["volume"] == 0.8,
      "窗口输入里读得到这一段自己的音量")

_, rep = te.plan_patch(tl, edits=[
    {"pointer": "/audio_events/0/volume", "value": 0.5},
    {"pointer": "/subtitles/0/text", "value": "第一句（重录）"}])
check(rep["manual_ids"] == [str(tl["audio_events"][0]["id"])],
      f"改动账分得清「手改的声音/取景」与「只改字」（实得 {rep['manual_ids']}）")

# ---------------------------------------------------------------------------
# ⑨ 字幕样式三件套：入口白名单 + 算框跟着旋钮走
# ---------------------------------------------------------------------------

sty = te.apply_patch(tl, "/subtitles/0/color", "#f0b7bd")
check(sty["subtitles"][0]["color"] == "#f0b7bd", "十六进制颜色收")
sty2 = te.apply_patch(sty, "/subtitles/0/color", "ivory")
check(sty2["subtitles"][0]["color"] == "ivory", "颜色名也收（Pillow 认它才不丢整条字幕链）")
for bad in ("red;background:url(x)", "#gghhii", "rgb(1,2,3)", ""):
    try:
        te.apply_patch(sty, "/subtitles/0/color", bad)
        check(False, f"颜色 {bad!r} 不该收")
    except ValueError:
        check(True, f"颜色 {bad!r} 拒收（会原样进 Pillow，认不出就整批丢字幕）")
try:
    te.apply_patch(sty, "/subtitles/0/font_size", 500)
    check(False, "字号越界必须拒")
except ValueError as exc:
    check("font_size" in str(exc), f"字号范围说清在入口：{str(exc)[:60]}")
try:
    te.apply_patch(sty, "/subtitles/0/position", "middle")
    check(False, "位置只能取档位之一")
except ValueError as exc:
    check("bottom/center/top" in str(exc), f"位置档位写进报错：{str(exc)[:70]}")

check(te.read_pointer(tl, "/subtitles/0/color") is None,
      "没写过的样式格读回 None（表单要能显示空白让人填），而不是报错")
try:
    te.read_pointer(tl, "/subtitles/0/author")
    check(False, "白名单外的键不该能读")
except KeyError:
    check(True, "非 CREATABLE 的缺失键仍然报错（蒙一个不存在的键从来不是出路）")

low = te.apply_patch(sty, "/subtitles/0/font_size", 200)
low["subtitles"][0]["position"] = "top"
box_top = te.subtitle_box(low, low["subtitles"][0])
box_plain = te.subtitle_box(tl, tl["subtitles"][0])
check(box_top["y"] < 0.2 and box_top["h"] > box_plain["h"],
      f"算框跟着字号与位置走（top/200px → {box_top}；默认 → {box_plain}）")
check(te.subtitle_font_size(low, low["subtitles"][0]) == 200.0
      and te.subtitle_font_size({"height": 360}, {}) == 20.0,
      "字号单源：给了按给的、没给退默认公式（渲出来的与表里算的必须同一个数）")

# ---------------------------------------------------------------------------
# ⑩ 手改段：同步闸与渲染前校准都要让路，并且这笔账要露出来（B6）
# ---------------------------------------------------------------------------

from open_storyline.nodes.core_nodes import (             # noqa: E402
    av_sync_check, evidence_ledger, manual_sync_notes,
    render_plan, resync_original_audio_timeline)


def gate_fixture() -> dict:
    """原声混剪：两幅画面都取自带口播的那份素材，第二段故意取错源时刻（差 4 秒）。"""
    return {
        "mode": "original_audio", "width": 640, "height": 360, "fps": 25.0,
        "duration": 8.0,
        "events": [
            {"path": "mat-A.mp4", "start": 0.0, "end": 4.0,
             "src_start": 10.0, "src_end": 14.0},
            {"path": "mat-A.mp4", "start": 4.0, "end": 8.0,
             "src_start": 10.0, "src_end": 14.0, "manual": te.MANUAL_MARK},
        ],
        "audio_events": [
            {"path": "mat-A.mp4", "start": 0.0, "end": 8.0, "src_start": 10.0,
             "src_end": 18.0},
        ],
    }


g = gate_fixture()
worst_g, bad_g = av_sync_check(g)
check(len(bad_g) == 0,
      f"第 2 段标了手改 → 闸不判它；第 1 段本来就对齐 → 一份零违规的账（实得 {bad_g}）")
g2 = gate_fixture()
del g2["events"][1]["manual"]
g2["events"][1]["src_start"] = 20.0
worst2, bad2 = av_sync_check(g2)
check(len(bad2) == 1 and "第 2 段画面" in bad2[0],
      f"同样这一格没标手改就必须判（闸不是橡皮图章）：{bad2}")
notes = manual_sync_notes(g)
check(len(notes) == 1 and "按用户手改" in notes[0] and "只能人眼验" in notes[0],
      f"免判的那一段要如实记一条，而不是从账上消失（实得 {notes}）")
check(render_plan(g)["manual"] == notes, "出片计划带这份手改账")
led = evidence_ledger(render_plan(g), worst_drift=worst_g, resynced=0, bgm=False)
exempt = [e for e in led if "按用户手改" in str(e.get("claim"))]
check(len(exempt) == 1 and exempt[0]["status"] != "verified",
      f"证据账里这条必须是 UNVERIFIED（verified={exempt[0].get('status') if exempt else '没有这一条'}）")

bare = gate_fixture()
del bare["events"][1]["manual"]
moved = resync_original_audio_timeline(bare)
check(moved == 1 and bare["events"][1]["src_start"] == 14.0,
      f"没标手改时校准照旧把错位那段钉回声音（实得 {moved} / "
      f"{bare['events'][1]['src_start']}）")
kept = gate_fixture()
kept["events"][0]["src_start"] = 30.0     # 没标手改、故意挪错的一段
kept["events"][1]["src_start"] = 99.0     # 标了手改、也挪错的一段
n = resync_original_audio_timeline(kept)
check(n == 1 and kept["events"][1]["src_start"] == 99.0,
      "校准跳过手改段：用户刚拧好的取景被钉回声音的位置，改动就静默消失了")
check(kept["events"][0]["src_start"] == 10.0,
      "没标手改的那一段照旧按声音钉回去——豁免只给标了的那一段")

print(f"\n{CHECKS} 项断言，{FAILS} 项失败")
sys.exit(1 if FAILS else 0)
