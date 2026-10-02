# -*- coding: utf-8 -*-
"""B4 真机重启对账：起服务 → 真实一轮对话 → kill -9 → 再起 → 状态全在、未完成任务被续跑。

跑法（需 docker compose up -d 且 .env 已填 PG_DSN / MINIO_* 五项，模型密钥按项目唯一入口配好）：
    PYTHONPATH=. python .smoke/b4_runtime_smoke.py

脚本自己各起一个 run_server 子进程（--storage pg_minio，不接 MCP/Storyline，端口现取），
不动开发者正在用的 :8000。判据全部对着 PG / MinIO 的实际行来钉：
①会话历史 ②长期记忆 ③技能库（正文 + 附件字节）④定时任务行 ⑤心跳行
⑥崩溃后未完成的 run 由新进程 resume 续跑并写出回复。
密钥只按「环境变量是否存在」报告，任何情况下都不打印值。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx

from agent_framework.context import DEFAULT_SYSTEM_PROMPT
from agent_framework.checkpoint import CheckpointManager
from agent_framework.llm_openai import get_default_llm
from agent_framework.memory import MemoryStore
from agent_framework.skill import SkillLoader
from agent_framework.storage import build_storage
from agent_framework.tools.cron import CronScheduler

REPO = Path(__file__).resolve().parents[1]
USER = ""                      # B5 起身份由 /register 现签，见 main 开头
TOKEN = ""                     # 只用于请求头，任何情况下都不打印值
CONV = f"c_b4_{int(time.time())}"
SUF = str(int(time.time()))[-6:]
HEARTBEAT_JOB = "heartbeat"

FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def llm_ready() -> bool:
    """模型密钥由项目唯一入口判定（MY_API_KEY 或 OPENAI_API_KEY）；报错文本不含任何值。"""
    try:
        get_default_llm()
    except RuntimeError as exc:
        print(f"缺少模型密钥：{exc}")
        return False
    print("模型密钥已配置（值未打印）")
    return True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    """一个 run_server 子进程；kill() 等价于掉电式崩溃（不走任何收尾钩子）。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.log = ""
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        # 子进程默认按控制台代码页（cp936）打日志，读侧按 utf-8 解就全是乱码——强制 UTF-8
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "run_server.py", "--storage", "pg_minio",
             "--port", str(self.port), "--no-mcp", "--no-storyline", "--static-dir", ""],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        # 读管道会阻塞，放独立线程里攒日志，别占用事件循环
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.log += line

    async def wait_ready(self, timeout_sec: float = 90.0) -> None:
        async with httpx.AsyncClient(base_url=self.base, timeout=3) as c:
            deadline = time.time() + timeout_sec
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


async def wait_for(fn, timeout_sec: float) -> bool:
    """轮询到 fn() 为真为止；超时返回 False。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if await fn():
            return True
        await asyncio.sleep(1.0)
    return False


async def wait_log(svc: "Service", needles: str | tuple[str, ...],
                   timeout_sec: float = 15.0) -> bool:
    """/health 通了不等于那行日志已被读管道线程攒进 svc.log —— 按候选文本等一会儿。"""
    want = (needles,) if isinstance(needles, str) else tuple(needles)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if any(n in svc.log for n in want):
            return True
        await asyncio.sleep(0.2)
    return any(n in svc.log for n in want)


async def resumed_ok(storage, run_id: str, user: str, conv: str) -> bool:
    """恢复完成的判据：快照收尾为 completed，且该会话多出一条 assistant 回复。"""
    row = await storage.checkpoints.load(run_id)
    finished = row is not None and row["status"] == "completed"
    replies = [h for h in await storage.messages.history(user, conv)
               if h["role"] == "assistant"]
    return finished and len(replies) >= 2


async def main() -> None:
    # 先构造：pg_minio 会把 .env 里的配置落进环境变量（只读值、不打印），
    # 早于这一步查密钥会误判 .env 里的 OPENAI_API_KEY 不存在。
    storage = build_storage("pg_minio")
    if not llm_ready():
        sys.exit(2)
    await storage.start()
    print(f"存储层已连通：backend={storage.backend}")

    svc_a, svc_b = Service(), Service()
    cron_id = ""
    cp = None
    smoke_skill = f"smoke-skill-{SUF}"
    skill_objects: list[str] = []
    import_root: Path | None = None
    try:
        # ============ 进程 A：真实一轮对话，状态全部落 PG ============
        svc_a.start()
        await svc_a.wait_ready()
        check(await wait_log(svc_a, "已连通并完成建表校验"), "进程 A 启动日志：存储层校验通过")
        check(await wait_log(svc_a, ("技能库已按", "仅使用库里已有的技能", "技能库导入失败")),
              "进程 A 启动日志：技能库从导入源刷进 PG")

        async with httpx.AsyncClient(base_url=svc_a.base, timeout=180) as c:
            no_cred = await c.post("/chat/sync", json={
                "conversation_id": CONV, "message": "不该被处理"})
            check(no_cred.status_code == 401,
                  f"真服务上没凭证就进不去：/chat/sync → {no_cred.status_code}")
            global USER, TOKEN
            reg = (await c.post("/register", json={"device_name": "b4-smoke"})).json()
            USER, TOKEN = reg["user_id"], reg["token"]
            print(f"身份现签：user_id={USER}（token 长度 {len(TOKEN)}，值不打印）", flush=True)
            r = await c.post("/chat/sync", json={
                "conversation_id": CONV,
                "message": "只回一句「冒烟 A 完成」，不要调用任何工具。"},
                headers={"Authorization": f"Bearer {TOKEN}"})
            answer_a = (r.json() or {}).get("answer", "")
            check(r.status_code == 200 and bool(answer_a.strip()),
                  f"/chat/sync 真实模型应答：{answer_a[:40]}")

        roles = [h["role"] for h in await storage.messages.history(USER, CONV)]
        check(roles == ["user", "assistant"], f"会话历史两行进了 PG：{roles}")

        # ---- 播种三类「上一个进程留下的状态」，各走自己的真实写入口 ----
        mem_text = f"冒烟标记 {SUF}：回答要保持一句话以内"
        await MemoryStore(storage, user_id=USER).write("user", mem_text)
        check(await storage.memories.read(USER, "user") == mem_text, "长期记忆写进 memories 表")

        job = await CronScheduler(storage).create_job(
            task="冒烟：到点报时一次", name=f"smoke-cron-{SUF}",
            kind="every", every_seconds=3600, delete_after_run=False)
        cron_id = job.id
        row = await storage.jobs.get(cron_id)
        check(row and row["enabled"] and row["state"].get("next_run_at_ms"),
              f"定时任务行进了 scheduled_jobs：{cron_id}")

        resume_msg = "只回一句「崩溃后恢复成功」，不要调用任何工具。"
        cp = await CheckpointManager(storage).begin(
            f"{USER}:{CONV}", resume_msg,
            [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
             {"role": "user", "content": resume_msg}], None)
        unfinished = [x["run_id"] for x in await storage.checkpoints.list_unfinished()]
        check(cp.run_id in unfinished, f"未完成 run 挂进 checkpoints 表：{cp.run_id}")

        loader = SkillLoader(storage)
        skills = await loader.discover()
        check(len(skills) >= 2, f"技能正文在 skills 表：{[s.name for s in skills]}")
        # 附件端到端：导入源里现成的技能都是纯正文，所以自建一个带嵌套附件的技能，
        # 走服务启动同款入口 sync_from_dir 入库，再从 MinIO 原样取回字节。
        import_root = Path(tempfile.mkdtemp(prefix="b4_skill_"))
        sub = import_root / smoke_skill
        (sub / "ref").mkdir(parents=True)
        (sub / "SKILL.md").write_text(
            f"---\nname: {smoke_skill}\ndescription: 冒烟用带附件技能\n---\n只回一句确认。\n",
            encoding="utf-8")
        blob = f"冒烟附件正文 {SUF}".encode("utf-8")
        (sub / "ref" / "note.txt").write_bytes(blob)
        check(await loader.sync_from_dir(import_root) == [smoke_skill],
              "带附件技能经 sync_from_dir 入库")
        sm = await loader.get(smoke_skill)
        rel = "ref/note.txt"
        skill_objects = [f["object_key"] for f in (sm.files if sm else [])]
        check(skill_objects == [f"skills/{smoke_skill}/{rel}"],
              f"附件清单登记 object_key：{skill_objects}")
        got = await loader.read_attachment(sm, rel)
        check(got == blob, "技能附件字节从 MinIO 原样取回")
        after = await loader.discover()
        check(len(after) == len(skills) + 1, f"新技能立刻出现在清单里：{len(after)} 个")

        # ============ 掉电式崩溃 + 重启对账 ============
        svc_a.kill()
        check(svc_a.proc.poll() is not None, "进程 A 已 kill（未走任何收尾钩子）")

        svc_b.start()
        await svc_b.wait_ready()
        check(await wait_log(svc_b, f"恢复未完成执行 run={cp.run_id}"),
              "进程 B 启动日志：认领到未完成 run 并投递恢复")
        check(await wait_for(lambda: resumed_ok(storage, cp.run_id, USER, CONV), 240),
              "重启后未完成任务续跑完成：快照收尾 + assistant 回复入库")

        still = await storage.jobs.get(cron_id)
        check(still and still["enabled"]
              and still["state"].get("next_run_at_ms") == row["state"]["next_run_at_ms"],
              "定时任务跨重启原样在位（未被补跑风暴改写）")
        check(await storage.jobs.get_by_name(HEARTBEAT_JOB) is not None,
              "心跳行在 scheduled_jobs 里（与定时任务同表）")
        check(await storage.memories.read(USER, "user") == mem_text, "长期记忆跨重启可读")
        reloaded = SkillLoader(storage)
        check(len(await reloaded.discover()) == len(after),
              f"技能清单跨重启数量一致（库是唯一源，不依赖本地目录）：{len(after)} 个")
        sm2 = await reloaded.get(smoke_skill)
        check(sm2 is not None and await reloaded.read_attachment(sm2, rel) == blob,
              "技能附件跨重启仍从 MinIO 取回同样字节")
        roles2 = [h["role"] for h in await storage.messages.history(USER, CONV)]
        check(roles2 == ["user", "assistant", "assistant"], f"历史按 seq 累积：{roles2}")

        async with httpx.AsyncClient(base_url=svc_b.base, timeout=180) as c:
            r = await c.post("/chat/sync", json={
                "conversation_id": CONV,
                "message": "我让你记住的那条冒烟标记是什么？直接复述内容。"},
                headers={"Authorization": f"Bearer {TOKEN}"})   # 旧 token 在新进程里仍认
            echoed = (r.json() or {}).get("answer", "")
            check(SUF in echoed, f"新进程带上了 PG 里的记忆：{echoed[:60]}")
    finally:
        for svc in (svc_a, svc_b):
            try:
                svc.kill()
            except Exception as exc:  # noqa: BLE001 - 收尾阶段进程已不在无所谓
                print(f"（收尾：{exc}）")
        # 冒烟自留地全部收回：定时任务 / 记忆 / 快照 / 会话历史 / 技能正文与附件
        if cron_id:
            await CronScheduler(storage).delete_job(cron_id)
        await storage.memories.drop_category(USER, "user")
        if cp is not None:
            await storage.checkpoints.drop(cp.run_id)
        await storage.db.delete("messages", where={"conv_id": CONV})
        await storage.conversations.drop(USER, CONV)
        for key in skill_objects:
            await storage.objects.delete(key)
        await storage.skills.drop(smoke_skill)
        if import_root is not None:
            shutil.rmtree(import_root, ignore_errors=True)
        await storage.close()

    print("\n全部通过：B4 运行时状态确已迁入 PG + MinIO" if FAILS == 0 else f"\n失败 {FAILS} 项")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
