# -*- coding: utf-8 -*-
"""续跑：写镜定稿即落盘，重来时复用导演与已写好的镜，不调模型。

    python tests/test_resume.py

不调模型、不渲染——假 LLM + 假读数，只验"该不该复用"这件事。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_storyline.simple import kernel, showrunner  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _FAILS += 1
        print(f"  XX   {label}")


PROMPT = "续跑测试：讲一下潮汐锁定"
SECONDS, ASPECT, FPS = 24.0, "landscape", 30

PLAN = {
    "title": "潮汐锁定",
    "tone": "底色 #0a0c14，主色 #E8F0F5 与 #4FD1E0，构图偏心留白，笔触矢量平涂剪影",
    "caption": {"size": 0.045, "at": "lower", "color": "#F2F5F8", "scrim": 0},
    "fx": ["grain"],
    "shots": [{"sec": 4.0, "text": f"第{i + 1}句", "plan": "画个东西",
               "transition": "cut"} for i in range(6)],
}

_CALLS = {"plan": 0, "shot": 0}


async def _plan(prompt, **kw):
    _CALLS["plan"] += 1
    return PLAN, 1


async def _write(shot, **kw):
    _CALLS["shot"] += 1
    return ('const Scene = () => (<AbsoluteFill style={{backgroundColor: "#0a0c14"}}>'
            '<svg viewBox="0 0 100 100"><circle cx={50} cy={50} r={20} fill="#4FD1E0"/>'
            '<rect x={10} y={10} width={8} height={8} fill="#E8F0F5"/></svg>'
            '</AbsoluteFill>);')


async def _evidence(code, secs, **kw):
    # 一帧"不空"的读数：亮度够、内容占比够
    return [{"index": 1, "frames": 1, "jpeg": "",
             "readings": {"mean_lum": 42.0, "content": 0.30, "vivid": 0.2},
             "warnings": []} for _ in secs]


def _seed(rdir: Path, n_done: int, tone: str) -> None:
    """造一份"写到第 n_done 镜就断了"的缓存。"""
    showrunner._resume_dir(rdir.name)
    (rdir / "plan.json").write_text(
        json.dumps({"at": time.time(), "plan": PLAN}, ensure_ascii=False),
        encoding="utf-8")
    for i in range(n_done):
        sig = f"{PLAN['shots'][i]['sec']:g}|{PLAN['shots'][i]['text']}"
        showrunner._persist_shot(
            rdir, i, sig, tone,
            'const Scene = () => <AbsoluteFill><svg viewBox="0 0 10 10">'
            '<circle r={3} /></svg></AbsoluteFill>;', f"RAW{i}")


async def main() -> int:
    showrunner.plan = _plan
    showrunner.write_shot = _write
    showrunner.shot_evidence = _evidence

    key = showrunner._resume_key(PROMPT, SECONDS, ASPECT, FPS)
    rdir = kernel.out_dir() / ".resume" / key
    shutil.rmtree(rdir, ignore_errors=True)

    kw = dict(seconds=SECONDS, aspect=ASPECT, fps=FPS, voice=False)

    print("\n① 第一次跑：全部要调模型，成功后缓存清掉")
    got1 = await showrunner.make(PROMPT, **kw)
    check(_CALLS["plan"] == 1, f"导演调了 1 次（{_CALLS['plan']}）")
    check(_CALLS["shot"] == 6, f"写镜调了 6 次（{_CALLS['shot']}）")
    check(got1["reused_plan"] is False and got1["reused_shots"] == 0,
          f"reused 全 0（{got1['reused_plan']}/{got1['reused_shots']}）")
    check(not rdir.exists(), "成功后缓存被清掉（下次是另一条片子）")

    print("\n② 写到第 3 镜断掉 → 重来只补没定稿的")
    _CALLS["plan"] = _CALLS["shot"] = 0
    _seed(rdir, 3, PLAN["tone"])
    got2 = await showrunner.make(PROMPT, **kw)
    check(_CALLS["plan"] == 0, f"导演没调（复用分镜，{_CALLS['plan']}）")
    check(_CALLS["shot"] == 3, f"只写了没定稿的 3 镜（{_CALLS['shot']}）")
    check(got2["reused_shots"] == 3, f"reused_shots=3（{got2['reused_shots']}）")
    check(got2["reused_plan"] is True, "reused_plan=True")
    check(not rdir.exists(), "续跑成功后缓存也清掉")

    print("\n③ 画法换了（tone 变了）→ 一镜都不复用")
    _CALLS["plan"] = _CALLS["shot"] = 0
    _seed(rdir, 3, "另一套基调")
    got3 = await showrunner.make(PROMPT, **kw)
    check(got3["reused_shots"] == 0, f"一镜都没复用（{got3['reused_shots']}）")
    check(_CALLS["shot"] == 6, f"6 镜全重写（{_CALLS['shot']}）")

    print("\n④ 缓存过期（2 小时外）→ 导演重调")
    _CALLS["plan"] = _CALLS["shot"] = 0
    showrunner._resume_dir(key)
    (rdir / "plan.json").write_text(
        json.dumps({"at": 0.0, "plan": PLAN}, ensure_ascii=False), encoding="utf-8")
    await showrunner.make(PROMPT, **kw)
    check(_CALLS["plan"] == 1, f"导演重调（{_CALLS['plan']}）")
    check(_CALLS["shot"] == 6, f"6 镜全重写（{_CALLS['shot']}）")

    shutil.rmtree(rdir, ignore_errors=True)
    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
