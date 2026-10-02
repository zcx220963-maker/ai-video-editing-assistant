# -*- coding: utf-8 -*-
"""真机冒烟：密钥从前端「设置」写进真 PG 的 app_secrets，两个进程都不重启就生效。

跑法（需 docker compose up -d 且 .env 已填五项）：
    PYTHONPATH=. python -u .smoke/settings_key_smoke.py

这一遍要钉住的六件事，替身测试都钉不住（离线库没有真外键、也没有真出网）：
  1. schema 自动应用后真库里确实有 app_secrets 这张表；
  2. /settings/api-key 保存的值**明文落在 PG 里**（两个服务都要拿它真发请求），
     而 HTTP 响应里只有掩码，任何返回体都不含明文；
  3. 服务子进程在写入**之前**就已经启动，仍能读到新 key —— 热读靠 PG 回源，不靠重启；
  4. /settings/test 两路（文本 + 带图）打真 DeepSeek 都通，且来源报「前端配置」；
  5. 把进程内的回落位与环境变量全部清空后，新构造的 client 只凭 PG 里的值也能出网拿到
     模型回复（证明 PG 这一层不是装饰）；
  6. 清除即删行；删掉用户行后 app_secrets 被真外键 CASCADE 带走，不留残留。

密钥值全程只做长度/末 4 位与等值比较，任何分支都不打印。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

import agent_framework.llm_openai as L  # noqa: E402
from agent_framework.identity import use_identity  # noqa: E402
from agent_framework.llm_openai import OpenAICompatClient  # noqa: E402
from agent_framework.secrets import API_KEY_NAME, SOURCE_PG, resolve_api_key  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402

ENV = "OPENAI_API_KEY"
FAILS = 0


def check(cond: bool, label: str, extra: str = "") -> None:
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}{(' → ' + extra) if extra else ''}")
    if not cond:
        FAILS += 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    """一个真 run_server 子进程：它在密钥写入之前启动，所以能验「不重启也热读」。"""

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
                except Exception:  # noqa: BLE001 - 还没起来
                    pass
                if self.proc.poll() is not None:
                    raise RuntimeError(f"服务子进程提前退出：\n{self.log[-2000:]}")
                await asyncio.sleep(0.5)
        raise RuntimeError(f"等待 /health 超时：\n{self.log[-2000:]}")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)


def tail(text: str, n: int = 160) -> str:
    return " ".join(text.split())[:n]


async def main() -> None:
    saved_env = os.environ.get(ENV, "")
    # 要往 PG 里写的值：优先取进程内回落位，其次取当前环境变量（都只报长度，不打印）
    key = (L.MY_API_KEY or saved_env or "").strip()
    if not key:
        print("没有可用密钥（回落位与环境变量都空）——先把 key 粘进 llm_openai.MY_API_KEY "
              "或设 OPENAI_API_KEY 再跑这一遍")
        sys.exit(2)
    print(f"待写入 PG 的密钥：长度 {len(key)}、末 4 位 {key[-4:]}（值不打印）")

    storage = build_storage("pg_minio")
    await storage.start()
    print(f"存储层已连通：backend={storage.backend}")
    svc = Service()
    uid = ""
    try:
        # ---------- 1. 真库里有这张表 ----------
        rows = await storage.db.select("app_secrets")
        check(True, f"app_secrets 表在真库里可读（现有 {len(rows)} 行）")

        # ---------- 2. 服务先启动，之后才有人配 key ----------
        svc.start()
        await svc.wait_ready()
        async with httpx.AsyncClient(base_url=svc.base, timeout=90) as c:
            j = (await c.post("/register", json={"device_name": "settings 冒烟"})).json()
            uid, token = j["user_id"], j["token"]
            hdr = {"Authorization": f"Bearer {token}"}
            check(uid.startswith("u-") and len(token) == 43,
                  f"/register 拿到身份（token 长度 {len(token)}，值不打印）")

            r0 = await c.get("/settings", headers=hdr)
            check(r0.status_code == 200 and r0.json()["api_key"]["configured"] is False,
                  "新身份初始为未配置")
            check(key not in r0.text, "未配置时的返回体里没有任何密钥值")

            bad = await c.post("/settings/api-key", headers=hdr, json={"api_key": "sk a b"})
            check(bad.status_code == 400, "含空格的值在写库前被拒", f"HTTP {bad.status_code}")

            r = await c.post("/settings/api-key", headers=hdr, json={"api_key": f"  {key}  "})
            body = r.json()
            check(r.status_code == 200 and body["status"] == "saved", "保存成功")
            check(body["api_key"]["source"] == SOURCE_PG,
                  f"生效来源立刻变成「{body['api_key']['source']}」")
            check(body["api_key"]["masked"].endswith(key[-4:]) and key not in r.text,
                  f"回显只有掩码 {body['api_key']['masked']}（正文不含明文）")
            check(body["api_key"]["effective"] is True, "端点自报「这一层真的在生效」")

            # ---------- 3. 明文只该出现在 PG 这一处 ----------
            stored = await storage.secrets.get(uid, API_KEY_NAME)
            check(stored == key.strip(), "PG 里存的是去空白后的原值（等值比较，不打印）")

            # ---------- 4. 自检两路都打真服务 ----------
            t0 = time.time()
            tr = await c.post("/settings/test", headers=hdr)
            tj = tr.json()
            print(f"     自检用时 {time.time() - t0:.1f}s · "
                  f"文本 {tj['text']['ok']}({tj['text']['ms']}ms) · "
                  f"视觉 {tj['vision']['ok']}({tj['vision']['ms']}ms) · "
                  f"模型 {tj['model']} @ {tj['base_url']}")
            check(tr.status_code == 200 and tj["ok"] is True, "测试连接整体通过")
            check(tj["source"] == SOURCE_PG,
                  "自检报明用的是前端配置（服务进程启动时还没有这条 key）")
            check(tj["text"]["ok"] and bool(tj["text"]["detail"]),
                  "文本路通", tail(tj["text"]["detail"], 60))
            check(tj["vision"]["ok"] and len(tj["vision"]["detail"]) > 4,
                  "带图路通（剪辑链的视觉理解用的就是这一条）",
                  tail(tj["vision"]["detail"], 60))
            check(key not in tr.text, "自检返回体不含明文密钥")

            # ---------- 5. 只剩 PG 这一层时真的能出网 ----------
            os.environ.pop(ENV, None)
            L.MY_API_KEY = ""
            try:
                got, src = await resolve_api_key(uid)
                check(got == key.strip() and src == SOURCE_PG,
                      "清空环境变量与回落位后仍解析到 PG 里的 key")
                client = OpenAICompatClient(api_key="", base_url=tj["base_url"])
                with use_identity(uid, "smoke-only-pg"):
                    resp = await asyncio.wait_for(
                        client.complete(messages=[{"role": "user",
                                                   "content": "只回两个字：在的"}], tools=None),
                        timeout=60)
                check(bool((resp.content or "").strip()),
                      "只凭 PG 里的 key 完成了一次真对话", tail(resp.content, 40))
            finally:
                L.MY_API_KEY = key
                if saved_env:
                    os.environ[ENV] = saved_env

            # ---------- 6. 清除 + 外键 CASCADE ----------
            cl = await c.post("/settings/api-key", headers=hdr, json={"api_key": ""})
            check(cl.json()["status"] == "cleared"
                  and cl.json()["api_key"]["configured"] is False, "传空串即清除")
            check(await storage.secrets.get(uid, API_KEY_NAME) == "", "清除后该行已删除")
            await storage.secrets.put(uid, API_KEY_NAME, key.strip())
        await storage.db.delete("users", where={"id": uid})
        uid = ""       # 已收回，finally 里不再重复删
        rows_after = [r for r in await storage.db.select("app_secrets")
                      if r["value"] == key.strip()]
        check(not rows_after,
              "删掉用户行后 app_secrets 被真外键 CASCADE 带走（无残留明文）")
    finally:
        svc.kill()
        if uid:
            await storage.db.delete("users", where={"id": uid})
        if saved_env:
            os.environ[ENV] = saved_env
        await storage.close()

    print("\n全部通过：前端自配置的密钥在真 PG + 真服务上成立" if FAILS == 0
          else f"\n失败 {FAILS} 项")
    sys.exit(1 if FAILS else 0)


def j2_base(test_json: dict) -> str:
    """自检返回的 base_url 就是本次真正打过去的地址，复用它，不再自己猜。"""
    return test_json["base_url"]


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
    except Exception as e:  # noqa: BLE001 - 冒烟脚本崩了要看见栈
        import traceback
        traceback.print_exc()
        print(f"\n用例异常：{type(e).__name__}: {e}")
        sys.exit(1)
