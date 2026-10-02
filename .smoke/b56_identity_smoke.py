# -*- coding: utf-8 -*-
"""B5-6 真机冒烟：身份来路收敛在**真 PG 的外键**上成立。

跑法（需 docker compose up -d 且 .env 已填五项）：
    PYTHONPATH=. python .smoke/b56_identity_smoke.py

离线替身没有外键，所以「写入路径不再自建用户行」这条判据只有在真库上才钉得住：
  1. 真 run_server 子进程启动日志出现「内部身份已登记」（default / cron 走的是真库）；
  2. 未登记的 user_id 在 conversations / materials / memories 三张外键表上都被拒，
     并且被拒之后 users 里仍然没有它的行（不会「顺手补一行」把身份养出来）；
  3. provision 出来的内部身份可被外键引用，但其占位哈希当凭证用不了（verify 反查不到）；
  4. /register 签发的身份照旧能建会话、写历史；
  5. 收尾：本轮造的 id 全删，users/conversations/materials/memories 残留 0 行。

token 与密钥只报长度或是否存在，任何情况下都不打印值。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx

from agent_framework.llm_openai import get_default_llm
from agent_framework.storage import INTERNAL_IDENTITIES, IntegrityConflict, build_storage

REPO = Path(__file__).resolve().parents[1]
FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    """一个真 run_server 子进程（--storage pg_minio，不接 MCP/Storyline/前端）。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.log = ""
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "run_server.py", "--storage", "pg_minio",
             "--port", str(self.port), "--no-mcp", "--no-storyline", "--static-dir", ""],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.log += line

    async def wait_ready(self, timeout_sec: float = 90.0) -> None:
        deadline = time.time() + timeout_sec
        async with httpx.AsyncClient(base_url=self.base, timeout=3) as c:
            while time.time() < deadline:
                try:
                    if (await c.get("/health")).status_code == 200:
                        return
                except Exception:  # noqa: BLE001 - 还没起来，继续等
                    pass
                if self.proc.poll() is not None:
                    raise RuntimeError(f"服务子进程提前退出：\n{self.log[-2000:]}")
                await asyncio.sleep(0.5)
        raise RuntimeError(f"等待 /health 超时：\n{self.log[-2000:]}")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)


async def refused(coro_factory, label: str) -> None:
    """期望外键拒绝：抛 IntegrityConflict 才算成立。"""
    try:
        await coro_factory()
    except IntegrityConflict as exc:
        check(True, f"{label} 被真 PG 外键拒绝（{str(exc)[:90]}）")
    except Exception as exc:  # noqa: BLE001 - 别的错因要如实报出来
        check(False, f"{label} 抛了意外的错：{type(exc).__name__}: {exc}")
    else:
        check(False, f"{label} 竟然写成功了（users 行被悄悄补出来了？）")


async def main() -> None:
    storage = build_storage("pg_minio")
    try:
        get_default_llm()
    except RuntimeError as exc:
        print(f"缺少模型密钥（/chat/sync 那一条要真模型）：{exc}")
        sys.exit(2)
    print("模型密钥已配置（值未打印）")
    await storage.start()
    print(f"存储层已连通：backend={storage.backend}")
    ns = f"b56{int(time.time()) % 1000000}"
    stranger, internal, uid_reg = f"{ns}-stranger", f"{ns}-internal", ""
    ids = [stranger, internal]
    svc = Service()
    try:
        # ---------- 1. 真服务启动：内部身份是启动期一次登记的 ----------
        svc.start()
        await svc.wait_ready()
        check("内部身份已登记" in svc.log,
              "run_server 启动日志出现「内部身份已登记」")
        for uid in INTERNAL_IDENTITIES:
            row = await storage.users.get(uid)
            check(row is not None and len(row["token_hash"]) == 64,
                  f"真库里内部身份 {uid} 有行、只存 64 位哈希")
            check(await storage.users.verify(row["token_hash"]) is None,
                  f"内部身份 {uid} 的占位哈希当凭证用不了（verify 反查不到）")
        async with httpx.AsyncClient(base_url=svc.base, timeout=60) as c:
            j = await c.post("/register", json={"device_name": "冒烟"})
            uid_reg, tok = j.json()["user_id"], j.json()["token"]
            ids.append(uid_reg)
            check(uid_reg.startswith("u-") and len(tok) == 43,
                  f"/register 仍走签发路径（token 长度 {len(tok)}，值未打印）")

        # ---------- 2. 未登记身份在三张外键表上都写不进去，也不会被补出来 ----------
        await refused(lambda: storage.conversations.ensure(stranger, f"{ns}-conv"),
                      "conversations（会话历史写入路径）")
        await refused(lambda: storage.materials.register(
            stranger, None, f"users/{stranger}/lib/{ns}.mp4", "冒烟.mp4", "video",
            bytes_=4, sha256="0" * 64), "materials（素材入库路径）")
        await refused(lambda: storage.memories.write(stranger, "user", "冒烟记忆"),
                      "memories（长期记忆写入路径）")
        check(await storage.users.get(stranger) is None,
              "三次被拒之后 users 里仍没有该 id（写入路径不自建身份）")

        # ---------- 3. 内部登记过的 id 立刻可被外键引用 ----------
        await storage.users.provision(internal)
        await storage.users.provision(internal)
        check(await storage.db.count("users", where={"id": internal}) == 1,
              "provision 幂等：真库里同 id 只有一行")
        conv = f"{ns}-conv"
        row = await storage.conversations.ensure(internal, conv)
        check(row["user_id"] == internal, "provision 过的身份能建会话（外键满足）")
        m = await storage.messages.append(internal, conv, "user", "冒烟一轮")
        check(m["seq"] == 1, "该身份下历史照常写得进")

        # ---------- 4. /register 出来的身份走同一条路（鉴权路径没被改坏）----------
        reg_conv = f"{ns}-reg"
        reg_row = await storage.conversations.ensure(uid_reg, reg_conv)
        check(reg_row["user_id"] == uid_reg, "/register 身份可直接建会话")
        async with httpx.AsyncClient(base_url=svc.base, timeout=60) as c:
            r = await c.post("/chat/sync", json={"conversation_id": reg_conv,
                                                 "message": "只回两个字：在的"},
                             headers={"Authorization": f"Bearer {tok}"})
            check(r.status_code == 200 and bool(r.json().get("answer", "").strip()),
                  f"真服务一轮对话仍写进同一会话（HTTP {r.status_code}）")
    finally:
        svc.kill()
        for uid in ids:
            await storage.db.delete("users", where={"id": uid})     # CASCADE 带走会话/历史
        await storage.db.delete("memories", where={"user_id": stranger})
        left: dict[str, int] = {}
        for table, col in (("users", "id"), ("conversations", "user_id"),
                           ("materials", "owner_user_id"), ("memories", "user_id")):
            rows = []
            for uid in ids + [stranger]:
                rows += await storage.db.select(table, where={col: uid})
            left[table] = len(rows)
        check(all(v == 0 for v in left.values()), f"本轮造的 id 全部收回：{left}")
        await storage.close()
    print("\n全部通过：B5-6 的身份来路收敛在真 PG 上成立" if FAILS == 0 else f"\n失败 {FAILS} 项")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
