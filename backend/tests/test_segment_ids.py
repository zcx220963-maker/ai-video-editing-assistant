# -*- coding: utf-8 -*-
"""段 id：命中表、切片缓存键、补丁指针共用的地基——必须跟内容走、不跟位置走。

为什么单独钉一条（选区改的前置条件）：原先 events 里只有位置没有身份。前端拖一次
顺序、或者改一句字幕，任何按下标寻址的东西（上一版命中表的行、缓存里那段字节、
补丁指针）都会集体错位，结果是「改一处、重烧全片」。这条用例把三件事钉死：
① 改顺序 → id 不变；② 改内容 → 只有那一段的 id 变；③ 渲染期本地化换 path
   不算内容变化 → id 不能漂。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.nodes.core_nodes import (  # noqa: E402
    _localize_timeline, assign_segment_ids, timeline_digest,
)

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def tl_fixture():
    return {
        "width": 1280, "height": 720, "fps": 25.0, "duration": 12.0, "mode": "original",
        "events": [
            {"path": "users/u/convs/c/mat-A.mp4", "start": 0.0, "end": 4.0,
             "src_start": 10.0, "src_end": 14.0},
            {"path": "users/u/convs/c/mat-A.mp4", "start": 4.0, "end": 9.0,
             "src_start": 20.0, "src_end": 25.0},
            {"path": "users/u/convs/c/mat-B.mp4", "start": 9.0, "end": 12.0,
             "src_start": 0.0, "src_end": 3.0, "kind": "vo_fill"},
        ],
        "audio_events": [
            {"path": "renders/s/vo-1.mp3", "start": 0.0, "duration": 9.0,
             "group_id": "g1"},
        ],
        "subtitles": [
            {"text": "第一句", "start": 0.0, "end": 4.0, "style": "subtitle_clean"},
            {"text": "第二句", "start": 4.0, "end": 9.0, "style": "subtitle_clean"},
        ],
        "overlay_events": [
            {"path": "users/u/convs/c/mat-C.png", "segments": ["asr-2"],
             "start": 5.0, "end": 7.0},
        ],
        "bgm": {"path": "users/u-bgm/mat-1.mp3", "volume": 0.2},
    }


def ids_of(tl, key):
    return [item.get("id") for item in tl[key]]


# 1. 每条都拿到 id，且四类都有
tl = assign_demo = tl_fixture()
n = assign_segment_ids(tl)
check(n == 7, f"四类列表共 7 段全部打标（返回 {n}）")
check(all(ids_of(tl, k) and all(x for x in ids_of(tl, k))
          for k in ("events", "audio_events", "subtitles", "overlay_events")),
      "events/audio_events/subtitles/overlay_events 都有非空 id")
check(len(set(ids_of(tl, "events"))) == 3, "同一路径不同源区间 → 三个不同 id")

# 2. 改顺序：id 跟着内容走
before = {e["src_start"]: e["id"] for e in tl["events"]}
shuffled = tl_fixture()
shuffled["events"] = [shuffled["events"][2], shuffled["events"][0],
                      shuffled["events"][1]]
assign_segment_ids(shuffled)
after = {e["src_start"]: e["id"] for e in shuffled["events"]}
check(before == after, "拖拽重排后每段的 id 完全不变（id 不认位置）")

# 3. 改内容：只有那一段的 id 变
edited = copy.deepcopy(tl)
edited["events"][1]["src_end"] = 27.5
assign_segment_ids(edited)
check(ids_of(edited, "events")[1] != ids_of(tl, "events")[1],
      "改 src_end → 那一段 id 变化（缓存键随之失效，该段重烧）")
check(ids_of(edited, "events")[0] == ids_of(tl, "events")[0]
      and ids_of(edited, "events")[2] == ids_of(tl, "events")[2],
      "同一份时间线里未改动的段 id 不动（不牵连别的段）")

# 4. 改字幕文本：只有那一条字幕换 id
sub = copy.deepcopy(tl)
sub["subtitles"][0]["text"] = "第一句（改过）"
assign_segment_ids(sub)
check(sub["subtitles"][0]["id"] != tl["subtitles"][0]["id"], "改字幕文本 → 该条 id 变")
check(sub["subtitles"][1]["id"] == tl["subtitles"][1]["id"], "另一条字幕 id 不变")
check(ids_of(sub, "events") == ids_of(tl, "events"), "改字幕不动画面段的 id")

# 5. 秒数写法归一：12 / 12.0 / "12.000" 算同一段
a, b = tl_fixture(), tl_fixture()
a["events"][0]["src_start"] = 10.0
b["events"][0]["src_start"] = "10.000"
a["events"][0]["src_end"] = 14
assign_segment_ids(a)
assign_segment_ids(b)
check(a["events"][0]["id"] == b["events"][0]["id"],
      "同一秒数写成 10.0 / \"10.000\" / 14 都算同一段（不因 JSON 写法换 id）")

# 6. 内容完全相同的两段（空镜循环铺满会造出来）→ id 必须互不相同
dup = tl_fixture()
dup["events"].append({"path": "users/u/convs/c/mat-B.mp4", "start": 12.0, "end": 15.0,
                      "src_start": 0.0, "src_end": 3.0, "kind": "vo_fill"})
assign_segment_ids(dup)
check(len(set(ids_of(dup, "events"))) == 4,
      "两段内容全同 → 加后缀区分，id 仍唯一")

# 7. 本地化不能改 id：真实渲染里 id 算在 localize 之前
import asyncio  # noqa: E402


class _Ws:
    async def localize_ref(self, ref, dst_dir):
        return str(Path(dst_dir) / Path(ref).name)


loc = asyncio.run(_localize_timeline(tl, _Ws(), Path("/tmp/render-src")))
check(ids_of(loc, "events") == ids_of(tl, "events"),
      "_localize_timeline 把对象键换成本机路径后，段 id 原样带过去")
check(all(e["path"] != t["path"] for e, t in zip(loc["events"], tl["events"])),
      "（对照）本地化确实换了 path：证明 id 认的不是本机路径")

# 8. 幂等 + 空输入不炸
ids_first = copy.deepcopy(ids_of(tl, "events"))
assign_segment_ids(tl)
check(ids_of(tl, "events") == ids_first, "重复调用不改 id（幂等：id 只认内容）")
check(assign_segment_ids({}) == 0 and assign_segment_ids(None) == 0
      and assign_segment_ids({"events": "不是列表"}) == 0,
      "空/畸形时间线返回 0 段，不抛")

# 9. digest 里带上 id 后仍能算，且同一份内容指纹稳定
d1 = timeline_digest(copy.deepcopy(tl), max_drift=0.0, resynced=0)["sha16"]
d2 = timeline_digest(copy.deepcopy(tl), max_drift=0.0, resynced=0)["sha16"]
check(d1 == d2, "打标后的 timeline_digest 稳定（dry-run 与真渲可对照）")

print(f"\n{_checks} 项断言，{_fails} 项失败")
# T19：模块级 `sys.exit` 让本文件**无法被任何收集器 import**（pytest 收集到这里就
# 直接结束进程）。加一层 `__main__` 守卫后，直接跑 rc 口径不变，而将来要用别的
# 收集器时也不会被这一句掀桌。（本轮不以此阻塞，只是把这条路留着别堵死。）
if __name__ == "__main__":
    sys.exit(1 if _fails else 0)
