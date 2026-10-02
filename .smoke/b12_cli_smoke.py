# -*- coding: utf-8 -*-
"""`scripts/runs_cli.py` 的真机冒烟：不开浏览器、不进前端，只用公开 HTTP/WS 端点走一遍时间旅行。

跑法（需 docker compose up -d、.env 五项已填、库里任一身份配过模型密钥）：
    PYTHONPATH=. python -u .smoke/b12_cli_smoke.py

为什么必须真机跑：CLI 的全部意义在于「另一条客户端路也走得通」，离线用例里 TestClient 用的是
同一份进程内对象，验不出「命令行参数解析对了没」「token 只从文件读不打印」「WS 帧真能等到
answer」「fork 投出去之后子 run 真在共享库里落出来」。这些都只有起一个真服务、真起子进程 CLI
才成立。

打的是临时实例（`--no-resume` + 随机端口），不动开发者常驻的 :8000/:8001；
token 与密钥只按存在/长度报告，绝不打印值；跑完把自己造的身份删干净并复核零残留。

判据分四段：
  ① register → 凭证落文件（屏幕只有 user_id），chat --wait --frames 当场看到 delta/answer
  ② runs / history：链上每点带 `tools`，`--before-node` 按服务端同一条规则算分叉点
  ③ fork --at-seq：子 run 走完并落终态，父 run 就地置 superseded（`runs` 清单里看得境）
  ④ 两处如实回绝：链首没有可复用点 / 分叉点不猜——CLI 必须把服务端那句拒绝原样传出来
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".smoke"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

from agent_framework.secrets import API_KEY_NAME  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from b6_fork_smoke import Service, bearer, borrow_key, free_port  # noqa: E402

STAMP = int(time.time())
CONV = f"c_b12_{STAMP}"
CLI = str(REPO / "scripts" / "runs_cli.py")
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(str(label))
    return bool(cond)


def cli(args: list[str], base: str, cred: Path, user: str,
        timeout: float = 900.0) -> subprocess.CompletedProcess[str]:
    """一次 CLI 调用。token 走 --token-file（不进命令行、不进进程列表）。"""
    argv = [sys.executable, "-u", CLI, "--base-url", base,
            "--token-file", str(cred), "--user", user, *args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "AGENT_TOKEN": ""}
    return subprocess.run(argv, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def cli_raw(args: list[str], base: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """不带凭证的一次调用（register 段用：那时还没有 token 可带）。"""
    argv = [sys.executable, "-u", CLI, "--base-url", base, *args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(argv, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def out(p: subprocess.CompletedProcess[str]) -> str:
    return (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")


def jloads(text: str) -> Any:
    """取输出里第一个能解析的 JSON 块——人类可读行可能夹在前面。"""
    start = text.find("[")
    if start < 0:
        start = text.find("{")
    return json.loads(text[start:]) if start >= 0 else None


async def main() -> int:
    st = build_storage("pg_minio")
    await st.start()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    svc = Service(["run_server.py", "--storage", "pg_minio", "--port", str(port),
                   "--no-mcp", "--no-storyline", "--static-dir", "", "--no-resume",
                   "--max-iterations", "3"], f"cli-{port}")
    cred = REPO / ".tmp" / f"b12_cli_{STAMP}.credentials.txt"
    user = ""
    token = ""
    try:
        svc.start()
        if not check(await svc.wait_ready(base), f"临时实例 :{port} 起来并 /health 通过"):
            print(svc.log[-1500:])
            return 1

        # ---- ① register：token 只落文件 ------------------------------------
        r = cli_raw(["register", "--token-file", str(cred)], base)
        check(r.returncode == 0 and "token 追加进" in r.stdout,
              f"① register 成功并把 token 写进文件：{r.stdout.strip()[:80]}")
        lines = [l.split("\t") for l in cred.read_text(encoding="utf-8").splitlines() if l.strip()]
        user, token = lines[0][0], lines[0][1].strip()
        check(f"\t{token}" not in r.stdout + r.stderr and token not in r.stdout + r.stderr,
              f"① 屏幕上没有 token（只报了 {len(token)} 字符的长度）")
        check(r.returncode == 0 and user.startswith("u-"),
              f"① 新身份 {user}")

        key = await borrow_key(st)
        if not check(bool(key), f"① 借到一把模型密钥（值不打印，只记长度 {len(key)}）"):
            return 1
        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            check((await c.post("/settings/api-key", headers=bearer(token),
                                 json={"api_key": key})).status_code == 200,
                  "① 页面同款写入口给新身份配上密钥")

        # ---- ② chat --wait --frames：当场看回投 ----------------------------
        r = cli(["chat", "--conversation", CONV, "--message",
                 f"不要调用任何工具，只回一句中文：CLI 时间旅行 {STAMP}",
                 "--wait", "--frames"], base, cred, user)
        o = out(r)
        check(r.returncode == 0 and "[ws] connected" in o, f"② CLI 先连上 WS 再投话：\n{o[:160]}")
        check("[ws] delta" not in o and "CLI 时间旅行" in o,
              "② delta 是逐字打印（不整行加标记），正文在输出里")
        check("[ws] answer" in o, "② 等到 answer 帧才收（不是投完就松手）")
        check("completed" in o, "② --wait 真的追到终态 completed")
        run_id = next((ln.split("run_id=")[1].split()[0] for ln in o.splitlines()
                       if "run_id=" in ln), "")
        check(bool(run_id), f"② 拿到 run_id {run_id}")
        check(token not in o, "② 这一整段输出里没有 token")

        # ---- ③ runs / history：清单与链 -----------------------------------
        r = cli(["--json", "runs", "--conversation", CONV], base, cred, user)
        runs = jloads(out(r)) or []
        check(len(runs) == 1 and runs[0]["run_id"] == run_id,
              f"③ runs 列出本轮那一条执行：{[x.get('run_id') for x in runs]}")
        r = cli(["--json", "history", "--run", run_id], base, cred, user)
        pts = jloads(out(r)) or []
        check(len(pts) >= 2 and pts[0]["seq"] == 0,
              f"③ history 给出一致点链（{len(pts)} 个点）：{[p['seq'] for p in pts]}")
        check(all("tools" in p and "kind" in p for p in pts),
              "③ 每个点都带 tools 与 kind——选分叉点的人不必蒙")

        # ---- ④ fork：回到链首开一条新执行 ---------------------------------
        r = cli(["fork", "--run", run_id, "--at-seq", "0", "--message",
                 f"不要调用任何工具，只回一句中文：CLI 分叉第二版 {STAMP}", "--wait"],
                base, cred, user)
        o2 = out(r)
        child = next((ln.split("子 run ")[1].split()[0] for ln in o2.splitlines()
                      if "子 run " in ln), "")
        check(r.returncode == 0 and bool(child), f"④ fork 投出去并回子 run id：{child or o2[:200]}")
        check("completed" in o2, "④ --wait 追到子 run 终态")
        check("CLI 分叉第二版" in o2, "④ 分叉带的新诉求进了子 run 的话里")
        r = cli(["--json", "runs", "--conversation", CONV], base, cred, user)
        runs = jloads(out(r)) or []
        by_id = {x["run_id"]: x for x in runs}
        check(len(runs) == 2, f"④ 清单变成两条：{[x['run_id'] for x in runs]}")
        check(by_id.get(child, {}).get("forked_from") == run_id
              and by_id.get(child, {}).get("forked_at_seq") == 0,
              f"④ 子 run 记着父与分叉点：{by_id.get(child, {}).get('forked_from')}"
              f"@{by_id.get(child, {}).get('forked_at_seq')}")
        check(by_id.get(run_id, {}).get("status") == "superseded",
              f"④ 父 run 就地置 superseded（不是还挂在 completed）：{by_id.get(run_id, {}).get('status')}")

        # ---- ⑤ resume：同一条 run 回到一致点续跑 ---------------------------
        r = cli(["resume", "--run", child, "--at-seq", "0", "--wait"], base, cred, user)
        o3 = out(r)
        check(r.returncode == 0 and child in o3 and "completed" in o3,
              f"⑤ resume 回链首续跑，同一条 run 再到终态：{o3[:120]}")

        # ---- ⑥ 两处如实回绝 ------------------------------------------------
        r = cli(["fork", "--run", child], base, cred, user, timeout=60)
        check(r.returncode != 0 and "分叉点不能靠猜" in out(r),
              f"⑥ 不给分叉点就回绝：{out(r).strip()[:80]}")
        r = cli(["history", "--run", child, "--before-node", "no_such_node"], base, cred, user)
        check(r.returncode != 0 and "这条链上没有" in out(r),
              f"⑥ 链上没有那个节点就回绝并列出可分叉的名字：{out(r).strip()[:90]}")
        r = cli(["fork", "--run", child, "--before-node", "no_such_node"], base, cred, user)
        check(r.returncode != 0 and "这条链上没有" in out(r),
              "⑥ fork 的 --before-node 走同一条拒绝（不是自认一套规则）")
    finally:
        svc.kill()
        if cred.exists():
            cred.unlink()        # 凭证文件是本脚本自造的临时件，用完删掉
        if user:
            try:
                await st.conversations.drop(user, CONV)
                await st.secrets.drop(user, API_KEY_NAME)
                await st.db.delete("users", where={"id": user})
            except Exception as exc:  # noqa: BLE001
                print(f"[清理] {user} 删除失败：{exc}", flush=True)
        left_user = await st.db.get_by_pk("users", {"id": user}) if user else None
        left_convs = await st.db.count("conversations", where={"user_id": user}) if user else 0
        await st.close()
        print(f"自建数据残留复核：身份 {'在' if left_user else '0'} / 会话 {left_convs} 条"
              f" / 凭证文件 {'仍在' if cred.exists() else '已删'}", flush=True)
    print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
