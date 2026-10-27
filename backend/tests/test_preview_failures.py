# -*- coding: utf-8 -*-
"""预览没出成时：哪一半系统自己修，哪一半必须写到用户眼前。

真机（2026-10-20）s05 那格「无预览」的代价不是一张缺图：那段代码编译不过，点继续
会在这一镜整片中停；而卡上只写「无预览」，用户不知道该做什么。所以这里钉三件事：

① **两种病要分开**：代码编译不过（重试/重渲都没用）与一时没抢到机器（重渲就出）；
② **报错要留下能照着改的那一行**：以前截前 120 字，恰好把
   `shot.tsx:65:8: ERROR: The symbol "backX" has already been declared` 截掉了；
③ **改不好时卡上要说人话**：哪几镜没生成、继续会停、想先看该选哪个。

纯函数，不连 PG、不起容器。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.render_gate import (  # noqa: E402
    ADJUST_OPTION, CONFIRM_OPTION, build_render_ask, motion_storyboard,
)
from open_storyline.motion.code_render import (  # noqa: E402
    _preview_fail_note, preview_failure_kind,
)

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


class Boom(Exception):
    pass


def main() -> int:
    print("\n=== ① 报错里留下能照着改的那一行 ===")
    err = Boom(
        "Error: Module build failed (from ./node_modules/babel-loader/lib/index.js):\n"
        "Transform failed with 1 error:\n"
        '/srv/src/user-shots/shot.tsx:65:8: ERROR: The symbol "backX" has already '
        "been declared\n"
        "    at handleError (/srv/node_modules/esbuild/lib/main.js:1:1)\n")
    note = _preview_fail_note("s05", err)
    check("shot.tsx:65:8" in note, f"带行号的一句留下了：{note[:80]}")
    check("backX" in note, "病因（重复声明的名字）留下了")
    check(len(note) <= 320, f"仍然是一句话（{len(note)} 字），不把整段日志倒进卡里")

    print("\n=== ② 编译不过 vs 一时没抢到，两类分开 ===")
    check(preview_failure_kind(note) == "final",
          "编译错判成 final：重试与重渲都是同一句，该让写手重画")
    timeout = TimeoutError("Timeout (30000ms) exceeded rendering the component initially")
    tnote = _preview_fail_note("s02", timeout)
    check(preview_failure_kind(tnote) == "transient",
          "初始挂载超时判成 transient：重画反而会把能跑的代码换掉")
    check("30000ms" in tnote, "超时那句也原样留着（看得出是哪种超时）")
    delay = Boom('delayRender() "Waiting for root component to load" was called')
    check(preview_failure_kind(_preview_fail_note("s16", delay)) == "transient",
          "等根组件加载也归 transient（真机上串行重渲一次就出）")

    print("\n=== ③ 自动重画也救不回来时，卡上要说人话 ===")
    motion = {
        "shots": [{"id": "s01", "text": "第一镜"}, {"id": "s05", "text": "第五镜"}],
        "preview_frames": {"s01": "renders/u/c/_default/preview/s01.jpg"},
        "preview_failures": {"s05": "final", "s02": "transient"},
    }
    board = motion_storyboard(motion)
    check(board[0].get("thumb_key", "").endswith("s01.jpg"), "有图的镜照旧带预览键")
    check("thumb_key" not in board[1], "没图的镜不硬编一个空图地址")

    ask = build_render_ask(None, motion=motion, title="确认开始渲染吗？")
    warns = [str(w) for w in (ask.get("warnings") or [])]
    check(any("s05" in w and "停下" in w for w in warns),
          f"编译不过的那一镜被点名，并说清继续会在那里停：{warns}")
    check(not any("s02" in w for w in warns),
          "一时没抢到的那一镜不进警告：它一般不影响出片，喊了反而吓人")
    check(any("我还要改" in w for w in warns),
          "给了用户自己的一步：选「我还要改」并说这一镜要画成什么样")
    keys = [o.get("key") for o in ask.get("options") or []]
    check(CONFIRM_OPTION in keys and ADJUST_OPTION in keys,
          "照样可以确认——只报不拦（堵死比白烧更坏）")

    healthy = {"shots": [{"id": "s01", "text": "第一镜"}],
               "preview_frames": {"s01": "k"}}
    check(not (build_render_ask(None, motion=healthy, title="确认开始渲染吗？")
               .get("warnings") or []),
          "全部出了图时不多喊一句")

    print(f"\n{'ALL PASSED' if not FAILS else f'{FAILS} FAILED'} ({CHECKS} checks)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
