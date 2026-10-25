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
    python selfcheck.py --fast          # 只跑「快」的那批（提交前用，见 FAST_MAX_SEC）
    python selfcheck.py --probe         # 5 个像素探针（容器不在记 UNVERIFIED，不算红）

退出码：0 全绿；1 有红/超时。**UNVERIFIED 不算红**——「没量过」与「量过且通过」是两件事，
把前者记成红会让体检在没起容器时永远失败，记成绿则是这个项目吃过三个月的那个亏。
它单独一档、退出码 0、报告标黄，由人决定要不要起容器重跑。

2026-10-24（T0.2/T0.3）加了 `--fast` 与 `--probe`：
· `--fast` 按**上一轮的实测耗时**挑文件，不按行数猜；没有耗时就按行数兜底。
  提交前跑它，超 90 秒只警告不拦（`.git/hooks/pre-commit`，见 `tools/hooks/`）。
· `--probe` 跑 `remotion/tests/` 那 5 个像素探针（glow / bloom / texture / env / DOF）。
  它们只能在 `creation-remotion` 容器里跑（`docker exec ... node tests/xxx-probe.mjs`），
  容器没起就 5 条 UNVERIFIED。结果落 `.tmp/selfcheck/probes.json`。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "backend" / "tests"
OUT_DIR = ROOT / ".tmp" / "selfcheck"
TIMINGS_PATH = OUT_DIR / "timings.json"
PROBES_PATH = OUT_DIR / "probes.json"

#: `--fast` 的入选线（秒）。**为什么不写 3**：实测 `test_motif_share` 要 7 秒，
#: 而它只是「读源码字符串 + 几个比值」，是最便宜的一类用例——按 3 秒切会把它排除在外，
#: 于是「把 FILL_TARGET 改成 0.9」这种改动提交时拦不住（T0.2 的验收正是这一条）。
#: 10 秒这条线是按实测分布定的：纯离线用例几乎全在 10 秒内，联网/起容器的那批远在线上。
FAST_MAX_SEC = float(os.environ.get("SELFCHECK_FAST_MAX_SEC", "10") or 10)
#: 提交前那一步的时间上限：超了只警告不拦（不能因为体检慢就把人堵在门外）。
HOOK_BUDGET_SEC = float(os.environ.get("SELFCHECK_HOOK_BUDGET", "90") or 90)

#: 五个像素探针：名字 → (脚本, 一句话题目)。**判据住在容器里**，这里只负责跑与记账。
#: 容器不在 = 5 条 UNVERIFIED，不是 5 条红。
PROBES: tuple[tuple[str, str, str], ...] = (
    ("glow", "effects-probe.mjs", "真 glow：@remotion/effects 在软 GL 上出不出光"),
    ("bloom", "bloom-probe.mjs", "bloom：EffectComposer 提亮的是「一圈柔光」还是「整幅糊了」"),
    ("texture", "texture-probe.mjs", "贴图：useLoader + staticFile 在软 GL 上真能贴（高频能量翻倍）"),
    ("env", "env-probe.mjs", "环境贴图：金属件反射里有天空/窗光，不是黑球"),
    ("dof", "dof-probe.mjs", "景深：postprocessing 的 DepthOfField 在软 GL 上真跑得动"),
)
REMOTION_CONTAINER = os.environ.get("SELFCHECK_REMOTION_CONTAINER", "creation-remotion")
REMOTION_BASE = os.environ.get("SELFCHECK_REMOTION_BASE", "http://127.0.0.1:8010")


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


def _load_timings() -> dict[str, float]:
    """上一轮每个文件的实测耗时（秒）。没有就返回空表——**不许拿它当结论**。"""
    try:
        got = json.loads(TIMINGS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in got.items()
            if isinstance(v, (int, float))} if isinstance(got, dict) else {}


def _save_timings(timings: dict[str, float]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TIMINGS_PATH.write_text(json.dumps(timings, ensure_ascii=False, indent=1,
                                      sort_keys=True), "utf-8")


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def changed_stems() -> set[str]:
    """这次改动涉及的模块名（暂存区 + 工作树）。→ `{"code_render", "motion_plan", …}`

    用途只有一个：**`--fast` 要把「和这次改动有关的用例」排在最前面**。
    光按耗时挑会漏掉关键的那一条——`test_motif_share` 要 15 秒，按耗时排不进快集，
    而它恰好是钉 `FILL_TARGET` 的那一个：把 0.60 改成 0.9，提交就拦不住了（T0.2 的验收）。
    """
    out = _git("diff", "--cached", "--name-only") + _git("diff", "--name-only")
    stems: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if p.suffix in (".py", ".mjs", ".ts", ".tsx"):
            stems.add(p.stem)
    return stems


def related_files(files: list[Path], stems: set[str]) -> list[Path]:
    """测试源码里提到这次改动的模块 → 这些用例与本次改动相关。"""
    if not stems:
        return []
    got: list[Path] = []
    for p in files:
        try:
            body = p.read_text("utf-8", errors="replace")
        except OSError:
            continue
        if any(s and s in body for s in stems):
            got.append(p)
    return got


def fast_files(files: list[Path]) -> list[Path]:
    """`--fast` 选哪一批：**相关用例优先，其余按上一轮实测耗时挑**。

    为什么按耗时而不按「离线/联网」这种标签：标签要靠人维护，一年后一定有一半是错的；
    耗时是跑出来的事实。没有耗时的文件（新加的用例）按行数估——短文件先跑。
    """
    timings = _load_timings()
    cheap: list[Path] = []
    for p in files:
        secs = timings.get(p.name)
        if secs is not None:
            if secs <= FAST_MAX_SEC:
                cheap.append(p)
            continue
        try:
            lines = len(p.read_text("utf-8", errors="replace").splitlines())
        except OSError:
            continue
        if lines <= 300:
            cheap.append(p)
    # 相关用例排前面（hook 的 90 秒预算用尽时，被砍掉的是「便宜的无关用例」）
    rel = related_files(files, changed_stems())
    seen = set(rel)
    return rel + [p for p in cheap if p not in seen]


def _docker(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """跑一条 docker 命令；docker 不在或超时都返回 (非零, 说明)。"""
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return 127, "本机没有 docker"
    except subprocess.TimeoutExpired:
        return 124, f"docker {' '.join(args)} 超过 {timeout:.0f}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def container_running(name: str) -> tuple[bool, str]:
    code, out = _docker(["inspect", "-f", "{{.State.Running}}", name], timeout=15)
    if code != 0:
        return False, f"容器 {name} 查不到（{out.strip()[:80]}）"
    return out.strip().lower() == "true", "" if out.strip().lower() == "true" else f"容器 {name} 没在跑"


def run_probe(name: str, script: str, timeout: float) -> dict:
    """跑一个探针（**在容器里**跑，因为它要 POST 给容器内的 :8010）。"""
    t0 = time.time()
    cmd = ["exec", "-e", f"DG_BASE={REMOTION_BASE}", REMOTION_CONTAINER,
           "sh", "-c", f"cd /srv && node tests/{script}"]
    code, out = _docker(cmd, timeout=timeout)
    secs = round(time.time() - t0, 1)
    if code == 0:
        return {"name": name, "script": script, "status": "ok", "secs": secs,
                "detail": (out.strip().splitlines() or [""])[-1][:200]}
    if code == 124:
        return {"name": name, "script": script, "status": "fail", "secs": secs,
                "detail": out.strip()[:200] or "超时"}
    tail = [ln for ln in out.strip().splitlines() if ln.strip()]
    return {"name": name, "script": script, "status": "fail", "secs": secs,
            "detail": " | ".join(tail[-3:])[:300] or f"退出码 {code}"}


def probes_main(timeout: float) -> int:
    """`--probe`：五个像素探针。**容器不在 → 5 条 UNVERIFIED、退出码 0、报告标黄。**"""
    up, why = container_running(REMOTION_CONTAINER)
    rows: list[dict] = []
    if not up:
        for name, script, about in PROBES:
            rows.append({"name": name, "script": script, "status": "unverified",
                         "secs": 0.0, "detail": f"{why}——这一档**没有量过**，不是通过"})
    else:
        print(f"探针跑在 {REMOTION_CONTAINER} 里（{REMOTION_BASE}），单条超时 {timeout:.0f}s\n")
        for name, script, about in PROBES:
            row = run_probe(name, script, timeout)
            rows.append(row)
            mark = {"ok": "ok ", "fail": "XX ", "unverified": "UN "}[row["status"]]
            print(f"{mark}{name:<10} {row['secs']:6.1f}s  {about}")
            if row["status"] == "fail":
                print(f"   └ {row['detail']}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROBES_PATH.write_text(json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "container": REMOTION_CONTAINER,
         "running": up, "results": rows}, ensure_ascii=False, indent=1), "utf-8")

    n_ok = sum(1 for r in rows if r["status"] == "ok")
    n_un = sum(1 for r in rows if r["status"] == "unverified")
    n_bad = sum(1 for r in rows if r["status"] == "fail")
    print(f"\n{'=' * 68}")
    print(f"探针：ok {n_ok} / UNVERIFIED {n_un} / 红 {n_bad}　（账：{PROBES_PATH.relative_to(ROOT)}）")
    if n_un:
        print("UNVERIFIED 不算红，但**也不等于通过**：这五档今天没人量过。"
              f"起容器后重跑：docker compose --profile app up -d remotion")
    if n_bad:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="一键体检（逐文件跑，带超时）")
    ap.add_argument("--only", default="", help="只跑文件名含这个词的")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="单文件超时秒数；缺省按文件行数算（见 default_timeout）")
    ap.add_argument("--fail-fast", action="store_true", help="第一条红就停")
    ap.add_argument("--fast", action="store_true",
                    help=f"只跑上一轮实测 ≤{FAST_MAX_SEC:.0f}s 的那批（提交前用）")
    ap.add_argument("--probe", action="store_true",
                    help="跑 5 个像素探针（容器不在记 UNVERIFIED，不算红）")
    ap.add_argument("--probe-timeout", type=float, default=300.0, help="单条探针超时（秒）")
    args = ap.parse_args()

    if args.probe:
        return probes_main(args.probe_timeout)

    if not TESTS.is_dir():
        print(f"没有测试目录：{TESTS}")
        return 1
    files = discover(args.only)
    if not files:
        print(f"没有匹配的测试文件（--only={args.only!r}）")
        return 1
    all_count = len(files)
    timings = _load_timings()
    if args.fast:
        files = fast_files(files)
        if not files:
            print("没有可跑的快用例（先跑一次全量，让它记下耗时）")
            return 1

    print(f"体检 {len(files)} 个文件"
          + (f"（--fast：从 {all_count} 个里挑「与本次改动相关」+ 上一轮实测 "
             f"≤{FAST_MAX_SEC:.0f}s 的，预算 {HOOK_BUDGET_SEC:.0f}s）"
             if args.fast else "")
          + f"（超时：{'固定 ' + str(args.timeout) + 's' if args.timeout else '按文件行数，90~300s'}）\n")
    bad: list[tuple[Path, str, str]] = []
    t_all = time.time()
    for i, p in enumerate(files, 1):
        limit = args.timeout or default_timeout(p)
        state, secs, msg = run_one(p, limit)
        timings[p.name] = round(secs, 2)
        mark = {"ok": "ok ", "fail": "XX ", "timeout": "TT "}[state]
        print(f"{mark}{p.name:<44} {secs:6.1f}s  {msg}")
        if state != "ok":
            bad.append((p, state, msg))
            if args.fail_fast:
                print("\n--fail-fast：停在这一条")
                break
        if args.fast and time.time() - t_all > HOOK_BUDGET_SEC and i < len(files):
            print(f"\n--fast 预算 {HOOK_BUDGET_SEC:.0f}s 已用尽，剩下的 {len(files) - i} 个不跑了"
                  "（提交前那一步只做快速拦阻；全量用 python selfcheck.py）")
            break

    _save_timings(timings)
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
