#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键体检：把「这个项目还能不能跑」压成一条命令。

为什么要有它（2026-10-24）：147 个测试文件散在 `backend/tests/`，其中一半是
**脚本式**的（模块顶层就跑断言、失败 `SystemExit(1)`），另一半是 pytest 函数式；
还有一批会连网络。直接 `pytest tests` 会被两类东西拖死：
① 脚本式的那个在收集期就 `SystemExit`，pytest 直接 `INTERNALERROR`，整个套件跑不起来；
② 有网络依赖的用例会挂住（实测 5 分钟零输出）。
于是「跑一遍测试」变成一件要靠人记性的事——那它就不是体检，是运气。

做法：**一个文件一个子进程**，各自带超时。挂住的算「超时」而不是把整轮拖死，
脚本式的照旧按退出码判。这样任何单文件的坏法都只影响它自己那一行。

用法：
    python selfcheck.py                 # 全部（约 5~10 分钟）
    python selfcheck.py --only skill    # 只跑文件名含 skill 的
    python selfcheck.py --timeout 60    # 单文件超时（秒，缺省 180）
    python selfcheck.py --fail-fast     # 第一条红就停

退出码：0 全绿；1 有红/超时。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "backend" / "tests"


def discover(pattern: str) -> list[Path]:
    files = sorted(p for p in TESTS.glob("test_*.py") if p.is_file())
    if pattern:
        files = [p for p in files if pattern.lower() in p.name.lower()]
    return files


def run_one(path: Path, timeout: float) -> tuple[str, float, str]:
    """→ (状态, 用时秒, 一句话说明)。状态取 ok / fail / timeout。

    **输出走临时文件，不走管道**（2026-10-24 踩的）：`subprocess.run(capture_output=True)`
    用两根管道接子进程，而 pytest 起来的**孙进程**（有几个测试自己再 spawn 一个 python）
    会继承这两根管道。超时那一刻 `run()` 杀掉的是 pytest，孙进程还活着、管道没 EOF，
    于是 `communicate()` 一直等——**超时形同虚设**，整轮就卡在那里。
    改成把 stdout/stderr 直接写文件：没有管道可等，超时就是超时。
    """
    t0 = time.time()
    env = dict(os.environ)
    # 测试要能 import `storyline_server.*` / `agent_framework.*`：根是 backend/
    env["PYTHONPATH"] = str(ROOT / "backend") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "pytest", str(path), "-q", "--tb=no",
           "-p", "no:cacheprovider"]
    out_path = ROOT / ".tmp" / "selfcheck" / (path.stem + ".log")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT / "backend"), env=env,
                                  stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout", time.time() - t0, f"超过 {timeout:.0f}s 没结束（多为网络依赖）"
    secs = time.time() - t0
    if proc.returncode == 0:
        return "ok", secs, ""
    tail = [ln for ln in out_path.read_text("utf-8", "replace").splitlines() if ln.strip()]
    msg = tail[-1][:120] if tail else f"退出码 {proc.returncode}"
    return "fail", secs, msg


def default_timeout(path: Path) -> float:
    """单文件超时按**它的行数**给，不再一刀切。

    为什么（2026-10-24 实测）：默认 45s 的时候，`test_code_gates` 单跑要 59.6s、
    `test_code_spec` 约 40s——它们**全是过的**，却因为「这一轮跑得慢」被判成红。
    于是一份体检报告把「慢」和「坏」混成一格，读的人先去修一个不存在的 bug。

    按行数给的依据：这些是脚本式测试，耗时大致随断言条数走。给到每千行 6 秒、
    再兜底 90 秒，`test_code_spec`（1442 行）约 99 秒、`test_code_gates`（944 行）
    约 96 秒，都在线内；真正会挂住的（联网那几个）仍然远在线上，照旧算超时。
    """
    try:
        lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 90.0
    return max(90.0, min(300.0, lines / 1000 * 6.0 + 90.0))


def main() -> int:
    ap = argparse.ArgumentParser(description="一键体检（逐文件跑，带超时）")
    ap.add_argument("--only", default="", help="只跑文件名含这个词的")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="单文件超时秒数；缺省按文件行数算（见 default_timeout）")
    ap.add_argument("--fail-fast", action="store_true", help="第一条红就停")
    args = ap.parse_args()

    if not TESTS.is_dir():
        print(f"没有测试目录：{TESTS}")
        return 1
    files = discover(args.only)
    if not files:
        print(f"没有匹配的测试文件（--only={args.only!r}）")
        return 1

    print(f"体检 {len(files)} 个文件"
          f"（超时：{'固定 ' + str(args.timeout) + 's' if args.timeout else '按文件行数，90~300s'}）\n")
    bad: list[tuple[Path, str, str]] = []
    t_all = time.time()
    for i, p in enumerate(files, 1):
        limit = args.timeout or default_timeout(p)
        state, secs, msg = run_one(p, limit)
        mark = {"ok": "ok ", "fail": "XX ", "timeout": "TT "}[state]
        print(f"{mark}{p.name:<44} {secs:6.1f}s  {msg}")
        if state != "ok":
            bad.append((p, state, msg))
            if args.fail_fast:
                print("\n--fail-fast：停在这一条")
                break

    total = time.time() - t_all
    print(f"\n{'=' * 68}")
    if not bad:
        print(f"全绿：{len(files)} 个文件，{total:.1f}s")
        return 0
    print(f"红 {len(bad)} / {len(files)}（总 {total:.1f}s）")
    for p, state, msg in bad:
        print(f"  [{state}] {p.name}  {msg}")
    print("\n单看某一条：python selfcheck.py --only " + bad[0][0].stem.replace("test_", ""))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
