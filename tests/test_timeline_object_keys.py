# -*- coding: utf-8 -*-
"""渲染前对象键校验：写错的键要**立刻**报错并给出正确的键。

真机事故：模型手写 timeline 时把 BGM 的键按「当前会话」前缀拼了——
    users/u-d9a478c9a6d7/convs/d2ef8d12…/mat-0fe987.mp3    ← 渲染器报「对象不存在」
而 BGM 来自**音乐库**（另一个 owner/会话），真键是
    users/u-bgm-library/convs/c-bgm-library/mat-0fe987.mp3
原先 render_video 对手写 timeline 逐字使用、不校验，于是跑了十分钟才失败，
报错还只说"不存在"，不说正确键是哪个。

这条用例钉住：
  ① 存在性判断按真实 head 走（不存在的键必须被拦下）；
  ② 报错里带上**正确的键**（按 mat id 反查 materials）；
  ③ 全部存在时放行（不误伤）；
  ④ 认得出裸键与 obj: 前缀两种写法。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from storyline_server.nodes.core_nodes import (  # noqa: E402
    _object_keys_in, validate_timeline_objects,
)

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class FakeObjects:
    def __init__(self, present: set[str]) -> None:
        self.present = present

    async def head(self, key: str):
        return object() if key in self.present else None


class FakeMaterials:
    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows

    async def get(self, mid: str):
        k = self.rows.get(mid)
        return {"object_key": k} if k else None


class FakeStorage:
    def __init__(self, present: set[str], mats: dict[str, str]) -> None:
        self.objects = FakeObjects(present)
        self.materials = FakeMaterials(mats)


GOOD = "users/u-d9a478c9a6d7/convs/d2ef8d12-2116-4de4-b567-5e4ad8d8f128/mat-21665d-n.mp4"
BGM_REAL = "users/u-bgm-library/convs/c-bgm-library/mat-0fe987.mp3"
BGM_BAD = "users/u-d9a478c9a6d7/convs/d2ef8d12-2116-4de4-b567-5e4ad8d8f128/mat-0fe987.mp3"


async def main() -> int:
    print("=== ① 抠引用：裸键与 obj: 前缀都要认出来 ===")
    tl = {"events": [{"path": GOOD}, {"path": "obj:" + BGM_REAL}],
          "audio_events": [{"path": BGM_REAL, "kind": "bgm"}],
          "title": "不是键的普通字符串", "duration": 234.0}
    keys = _object_keys_in(tl)
    check(GOOD in keys, "认得出裸对象键")
    check(BGM_REAL in keys, "认得出 obj: 前缀的对象键（去掉了前缀）")
    check("不是键的普通字符串" not in keys, "不把普通文本当键")

    print("\n=== ② 写错的键：要拦下，并给出正确键 ===")
    storage = FakeStorage(present={GOOD, BGM_REAL},
                          mats={"mat-0fe987": BGM_REAL})
    bad_tl = {"events": [{"path": GOOD}], "audio_events": [{"path": BGM_BAD}]}
    try:
        await validate_timeline_objects(bad_tl, storage)
        check(False, "写错的键**没有**被拦下（应当报错）")
    except ValueError as e:
        msg = str(e)
        print(f"    报错：{msg[:160]}")
        check(BGM_BAD in msg, "点名了那个不存在的键")
        check(BGM_REAL in msg, "给出了**正确的键**（模型能一次改对）")

    print("\n=== ③ 全部存在：放行（不误伤）===")
    ok_tl = {"events": [{"path": GOOD}, {"path": "obj:" + BGM_REAL}],
             "audio_events": [{"path": BGM_REAL}]}
    try:
        await validate_timeline_objects(ok_tl, storage)
        check(True, "键都存在时放行")
    except ValueError as e:
        check(False, f"误伤了：{e}")

    print("\n=== ④ 空/无键/无存储：不炸 ===")
    for tl2, label in ((None, "None"), ({}, "空 dict"),
                       ({"events": []}, "空 events"),
                       ({"title": "没有键"}, "没有键")):
        try:
            await validate_timeline_objects(tl2, storage)
            check(True, f"{label} 放行")
        except Exception as e:  # noqa: BLE001
            check(False, f"{label} 不该炸：{e}")
    try:
        await validate_timeline_objects(ok_tl, None)
        check(True, "storage 为 None 时放行（不炸）")
    except Exception as e:  # noqa: BLE001
        check(False, f"storage=None 不该炸：{e}")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
