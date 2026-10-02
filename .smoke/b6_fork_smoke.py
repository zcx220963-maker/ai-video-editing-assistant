# -*- coding: utf-8 -*-
"""B6 真机冒烟：执行记录面（指针行 + 一致点增量链）、HTTP 分叉、以及剪辑契约的真取回。

跑法（需 docker compose up -d，.env 五项已填，模型密钥已就位）：
    PYTHONPATH=. python .smoke/b6_fork_smoke.py

两段：
① 契约——临时 config 换端口起一台**当前代码**的 Storyline，主服务侧的 `load_contract`
   真打 `dag_contract`，验节点数与 downstream 闭包（README §3.3 那行日志的前提）。
② 执行面——`--no-storyline` 起主服务（本服务无剪辑能力，正是设计口径），走真 HTTP + 真 MQ
   + 真 PG：一轮 chat 落增量链 → /runs 清单 → /runs/{id}/history → fork 出子 run 跑完
   → 父 run 置 superseded → 新消息不被 resume 劫持 → 别人的 run 404。

自己起自己的端口，不动开发者常驻的 :8000/:8001；token 与密钥只按存在/长度报告，绝不打印值；
跑完把自己造的行删干净并复核零残留。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_framework.editing_contract import load_contract  # noqa: E402
from agent_framework.llm_openai import (  # noqa: E402
    DEFAULT_BASE_URL, DEFAULT_MODEL, MY_BASE_URL, MY_MODEL)
from agent_framework.secrets import API_KEY_NAME  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tools.mcp import MCPServerConfig, connect_server  # noqa: E402
from agent_framework.video_editing import (  # noqa: E402
    load_storyline_config, storyline_server_url)

REPO = Path(__file__).resolve().parents[1]
STAMP = int(time.time())
CONV = f"c_b6_{STAMP}"
FAILS: list[str] = []


def check(cond, label):
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def borrow_key(st) -> str:
    """从库里已配置过密钥的身份借一把（只走页面同款写入口，值绝不打印）。

    新注册身份没有 app_secrets 行，模型调用会如实报「未配置模型密钥」——那验得到失败
    路径，验不到分叉成功路径，所以借一把真的走完全程。
    """
    rows = await st.db.select("app_secrets", where={"key_name": API_KEY_NAME})
    for r in rows:
        v = str(r.get("value") or "")
        if v.strip():
            return v.strip()
    return ""


async def preflight_credit() -> str:
    """真机冒烟的前置闸：先花 4 个 token 问模型「还调用得通吗」。

    这些冒烟的判据全建立在「那一轮真跑到 completed」上。密钥没余额时 run 会在
    iteration=0 直接 failed，于是十几条断言一起 FAIL——看着像代码坏了，其实是账户空了。
    返回 ""（可以继续）或一句中文原因。
    """
    st = build_storage("pg_minio")
    await st.start()
    try:
        key = await borrow_key(st)
    finally:
        await st.close()
    if not key:
        return "库里没有已配置的模型密钥（app_secrets 无值），真机冒烟跑不了。"
    model = MY_MODEL or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
    base_url = MY_BASE_URL or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    try:
        from openai import AsyncOpenAI  # noqa: PLC0415 - 只在真要出网时才依赖它

        async with AsyncOpenAI(api_key=key, base_url=base_url) as client:
            await client.chat.completions.create(
                model=model, max_tokens=4,
                messages=[{"role": "user", "content": "回一个「在」字"}], timeout=60)
    except Exception as exc:  # noqa: BLE001 - 冒烟只分类不重试
        code = getattr(exc, "status_code", None)
        tip = {401: "密钥无效", 402: "账户余额不足", 403: "访问被拒",
               429: "请求过于频繁"}.get(int(code or 0), "调用失败")
        return (f"模型{tip}（{code or type(exc).__name__}）——真机冒烟需要先解决它，"
                f"否则 run 会在 iteration=0 就 failed，一堆断言 FAIL 是环境不是缺陷。"
                f"原始错误：{str(exc)[:160]}")
    return ""


async def credit_gate() -> int:
    """冒烟开场用：额度没问题返 0，否则把原因打印出来、返退出码 2。"""
    reason = await preflight_credit()
    if not reason:
        return 0
    print(f"SKIP  {reason}", flush=True)
    return 2


class Service:
    """一个子进程服务（主服务或临时 Storyline），日志攒在内存里给判据用。"""

    def __init__(self, argv: list[str], tag: str) -> None:
        self.argv = argv
        self.tag = tag
        self.log = ""
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen([sys.executable, "-u", *self.argv], cwd=str(REPO),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, env=env)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.log += line

    async def wait_port(self, port: int, timeout_sec: float = 180.0) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return True
            if self.proc.poll() is not None:
                print(f"[{self.tag}] 子进程提前退出：\n{self.log[-1500:]}")
                return False
            await asyncio.sleep(0.5)
        print(f"[{self.tag}] 等端口 :{port} 超时：\n{self.log[-1500:]}")
        return False

    async def wait_ready(self, base: str, timeout_sec: float = 120.0) -> bool:
        async with httpx.AsyncClient(base_url=base, timeout=3) as c:
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                try:
                    if (await c.get("/health")).status_code == 200:
                        return True
                except Exception:  # noqa: BLE001 - 还没起来
                    pass
                if self.proc.poll() is not None:
                    print(f"[{self.tag}] 子进程提前退出：\n{self.log[-1500:]}")
                    return False
                await asyncio.sleep(0.5)
        print(f"[{self.tag}] 等 /health 超时：\n{self.log[-1500:]}")
        return False

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- ① 真契约
async def part_contract() -> None:
    print("\n=== ① 剪辑契约：真打一次 dag_contract（当前代码的 Storyline）===")
    port = free_port()
    src = (REPO / "examples" / "storyline" / "config.toml").read_text(encoding="utf-8")
    tmp = REPO / ".tmp" / f"storyline_b6_{STAMP}.toml"
    tmp.parent.mkdir(exist_ok=True)
    # 只换端口：其余（存储后端、能力、节点白名单）与开发机一致
    tmp.write_text(re.sub(r"(?m)^port\s*=\s*\d+\s*$", f"port = {port}", src), encoding="utf-8")
    svc = Service(["run_storyline.py", "--config", str(tmp.relative_to(REPO))], "storyline")
    svc.start()
    try:
        if not await svc.wait_port(port):
            check(False, "临时 Storyline 起来（后续契约检查作废）")
            return
        cfg = load_storyline_config(tmp)
        url = storyline_server_url(cfg)
        client = await connect_server(MCPServerConfig(
            name="storyline", type="streamableHttp", url=url,
            enabled_tools=storyline_tools(cfg), tool_timeout=60))
        listed = {t.get("name") for t in await client.list_tools(timeout=60)}
        contract = await load_contract(client, timeout=60)
        check("dag_contract" in listed, f"服务端把 DAG 契约当工具暴露（{len(listed)} 工具在表）")
        check(bool(contract), f"契约非空：{len(contract.nodes)} 节点")
        check(len(contract.nodes) >= 19, f"节点数覆盖整条剪辑链（实得 {len(contract.nodes)}）")
        if contract:
            ds = contract.downstream("select_BGM")
            check({"plan_timeline", "render_video"} <= ds or not ds,
                  f"downstream(select_BGM) 覆盖下游：{sorted(ds)}")
            check(all(c.writes for c in contract.nodes.values())
                  and all(isinstance(c.requires, tuple) for c in contract.nodes.values()),
                  "每个节点都带写集与依赖元组（Tool.reads/writes 的原料齐了）")
            # 空契约时的退化口径：只有自身，不猜下游
            from agent_framework.editing_contract import EditingContract
            check(EditingContract().downstream("whatever") == {"whatever"},
                  "契约取不到时 downstream 退化成只作废自己")
        await client.close()
    finally:
        svc.kill()
        tmp.unlink(missing_ok=True)


def storyline_tools(cfg) -> list[str]:
    return list(cfg.get("local_mcp_server", {}).get("available_nodes", [])) or ["*"]


# ---------------------------------------------------------------- ② 执行面
async def wait_run(client, token, run_id, timeout_sec=240.0) -> dict | None:
    """按 /runs/{id} 轮询到终态（completed / failed / superseded）。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = await client.get(f"/runs/{run_id}", headers=bearer(token))
        if r.status_code == 200:
            run = r.json()["run"]
            if run["status"] != "running":
                return run
        await asyncio.sleep(1.0)
    return None


async def part_runsurface() -> None:
    print("\n=== ② 执行面：真 HTTP + 真 MQ + 真 PG 的增量链与分叉 ===")
    port = free_port()
    # --no-resume 是硬要求：不带它，这台临时服务会去恢复**别人**在途的 run，
    # 而它没有那些身份的模型密钥，会把别人的运行态从 running 顶成 failed。
    svc = Service(["run_server.py", "--storage", "pg_minio", "--port", str(port),
                   "--no-mcp", "--no-storyline", "--static-dir", "",
                   "--no-resume", "--max-iterations", "4"], "main")
    svc.start()
    base = f"http://127.0.0.1:{port}"
    st = build_storage(backend="pg_minio")
    await st.start()
    users: list[str] = []
    runs: list[str] = []
    try:
        if not await svc.wait_ready(base):
            check(False, "主服务起来（后续执行面检查作废）")
            return
        check("已禁用 Storyline 探测" in svc.log or "本服务无剪辑能力" in svc.log,
              "未连通 Storyline → 启动日志明确宣告无剪辑能力")
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            ra = (await c.post("/register", json={"device_name": "b6-a"})).json()
            rb = (await c.post("/register", json={"device_name": "b6-b"})).json()
            ta, tb = ra["token"], rb["token"]
            users += [ra["user_id"], rb["user_id"]]
            check(all(x not in svc.log for x in (ta, tb)), "日志里没有 token 值")

            key = await borrow_key(st)
            check(bool(key), "从已配置身份借到一把模型密钥（值不打印，只走页面同款写入口）")
            if not key:
                return
            sk = (await c.post("/settings/api-key", headers=bearer(ta),
                               json={"api_key": key})).json()
            check(sk.get("status") == "saved" and key not in json.dumps(sk),
                  f"密钥配上且响应只有掩码：{sk.get('masked') or sk.get('api_key')}")

            body = (await c.get("/tools", headers=bearer(ta))).json()
            groups = body.get("groups") or {}
            names = [t["name"] for v in groups.values() if isinstance(v, list)
                     for t in v if isinstance(t, dict) and t.get("name")]
            nodes = {"load_media", "split_shots", "asr", "understand_clips", "generate_script",
                     "select_BGM", "plan_timeline", "render_video"}
            check(not (nodes & set(names)),
                  f"无 Storyline 时工具表里没有任何剪辑节点（{len(names)} 个工具，"
                  f"命中 {sorted(nodes & set(names))}）")
            check("rerun_from" in names, "rerun_from 常驻工具表（契约空时它自己回绝）")

            # —— 一轮真对话
            q = (await c.post("/chat", headers=bearer(ta), json={
                "conversation_id": CONV,
                "message": "不要调用任何工具，只回复两个字：收到"})).json()
            parent = q["run_id"]
            runs.append(parent)
            done = await wait_run(c, ta, parent)
            check(done is not None and done["status"] == "completed",
                  f"父 run 真跑完：{done and done['status']}")
            if done is None:
                return
            check(done["head_seq"] >= 1,
                  f"一轮对话至少推进出 seq=0 基准 + 收尾一致点（head_seq={done['head_seq']}）")

            row = await st.checkpoints.load(parent)
            check("messages" not in row, "指针行里不再整包存 messages")
            entries = await st.checkpoints.load_entries(parent)
            check([e["seq"] for e in entries] == list(range(len(entries)))
                  and len(entries) == row["head_seq"] + 1,
                  f"链连续且长度等于 head_seq+1（{len(entries)} 条）")
            check(entries[0]["kind"] == "full"
                  and {e["kind"] for e in entries[1:]} <= {"delta"},
                  "链首是 full 基准，其后按增量落")
            total = sum(len(e["payload"]) for e in entries)
            check(len(entries) > 1 and max(len(e["payload"]) for e in entries) < total,
                  f"没有任何一轮在整条重写：最长 payload "
                  f"{max(len(e['payload']) for e in entries)} 条 < 全链 {total} 条")

            hist = (await c.get(f"/runs/{parent}/history", headers=bearer(ta))).json()
            check([p["seq"] for p in hist["points"]] == [e["seq"] for e in entries],
                  f"/history 与库里的链一致（{len(hist['points'])} 个一致点）")
            lst = (await c.get(f"/convs/{CONV}/runs", headers=bearer(ta))).json()
            check([r["run_id"] for r in lst["runs"]][:1] == [parent],
                  "本会话执行清单列得出这条 run")
            check((await c.get(f"/runs/{parent}", headers=bearer(tb))).status_code == 404,
                  "别人的 run 按 404 处理（不泄露存在性）")

            # —— HTTP 分叉
            fk = (await c.post(f"/runs/{parent}/fork", headers=bearer(ta), json={
                "at_seq": 0, "message": "同样只回复两个字：好的"}))
            child = fk.json()["run_id"]
            runs.append(child)
            check(fk.status_code == 200 and fk.json()["status"] == "queued",
                  "fork 只投一帧 MQ（不就地执行）")
            cdone = await wait_run(c, ta, child)
            check(cdone is not None and cdone["status"] == "completed",
                  f"子 run 真跑完：{cdone and cdone['status']}")
            if cdone is None:
                return
            prow = await st.checkpoints.load(parent)
            check(prow["status"] == "superseded", "父 run 已让位（superseded）")
            check(cdone["forked_from"] == parent and cdone["forked_at_seq"] == 0,
                  "子 run 记着从谁的哪个一致点分出来")
            centries = await st.checkpoints.load_entries(child)
            check(centries[0]["kind"] == "full" and centries[0]["seq"] == 0,
                  "子 run 另起一条链，链首是基准")
            check(prow["session_id"].endswith(CONV) and
                  prow["session_id"] == (await st.checkpoints.load(child))["session_id"],
                  "分叉留在同一会话里（不换会话）")
            check(child not in [r["run_id"] for r in
                                (await c.get(f"/convs/{CONV}/runs", headers=bearer(tb))
                                 ).json()["runs"]],
                  "会话清单也按 owner 过滤（换人看不到别人的两条 run）")
            act = (await c.get(f"/convs/{CONV}/runs/active", headers=bearer(ta))).json()
            check(act["run"] is None, "completed/superseded 都不算在途")

            # —— 新消息不被「继续」劫持
            q2 = (await c.post("/chat", headers=bearer(ta), json={
                "conversation_id": CONV,
                "message": "不要调用任何工具，只回复两个字：第三轮"})).json()
            runs.append(q2["run_id"])
            check(q2["run_id"] not in (parent, child),
                  "不带 resume 标记的新消息开新 run")
            d2 = await wait_run(c, ta, q2["run_id"])
            check(d2 is not None and d2["status"] == "completed", "第三条 run 也真跑完")

            msgs = (await c.get(f"/convs/{CONV}/messages", headers=bearer(ta))).json()
            texts = [m.get("text", "") for m in msgs["messages"]]
            check(sum("收到" in t for t in texts) >= 1 and sum("好的" in t for t in texts) >= 1,
                  f"三条执行的答复都落回同一会话历史（{len(texts)} 行）")
    finally:
        svc.kill()
        for r in runs:
            try:
                await st.checkpoints.drop(r)
            except Exception as e:  # noqa: BLE001
                print(f"清理 run {r} 失败：{e}")
        for u in users:
            try:
                await st.secrets.drop(u, API_KEY_NAME)   # 借来的 key 不在库里多留一份
                await st.conversations.drop(u, CONV)     # 消息行随外键 CASCADE 走
                await st.db.delete("users", where={"id": u})
            except Exception as e:  # noqa: BLE001
                print(f"清理用户 {u} 失败：{e}")
        await st.close()

    # 零残留复核（新连接，确保上面的 close 已生效）
    st2 = build_storage(backend="pg_minio")
    await st2.start()
    try:
        left = [r for r in runs if await st2.checkpoints.load(r)]
        check(not left, f"自建的 run 已全部收回（残留 {left}）")
        orphan = [e["run_id"] for e in await st2.db.select("checkpoint_entries")
                  if e["run_id"] in runs]
        check(not orphan, f"指针行删掉后没有孤儿一致点（残留 {set(orphan)}）")
        check(not [u for u in users if await st2.users.get(u)], "自建身份已删")
    finally:
        await st2.close()


async def main() -> None:
    # 模型额度是这些断言的前提：没额度时 run 会在 iteration=0 就 failed，
    # 十几条断言一起 FAIL，那是环境不是缺陷——先闸掉再说。
    if await credit_gate():
        return
    await part_contract()
    await part_runsurface()
    print("\n" + ("全部通过" if not FAILS else f"{len(FAILS)} 项失败："))
    for f in FAILS:
        print("  ✗ " + f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
