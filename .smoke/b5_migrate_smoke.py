# -*- coding: utf-8 -*-
"""B5 迁移真机冒烟：同一套遗留样本在**真 PG + 真 MinIO** 上连跑两遍，逐表对账。

跑法（需 docker compose up -d + .env 已填密钥）：
    PYTHONPATH=. python .smoke/b5_migrate_smoke.py

为什么离线全绿还要这一份：内存替身不执行外键、不执行 UNIQUE(name)/UNIQUE(session,artifact)，
也不校验 sha256 之后的真实反查；B1/B2 就是在真机才挖出三个假绿（见项目记忆）。
这份冒烟用带时间戳命名空间的样本 id，跑完把九张表与桶里的字节清干净，不给开发库留残留。
token 值只从凭证文件读回来用一次，不落标准输出。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

from agent_framework.storage import build_storage
from run_migrate import Migrator
from test_migrate import write_legacy_tree

FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def digest(rows) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="b5_mig_smoke_"))
    ns = f"mig{int(time.time()) % 100000}z"
    uid = write_legacy_tree(tmp, ns=ns, root_memory=False)
    one, two = f"{ns}c-one", f"{ns}c-two"
    sess, out_sess = f"{uid}:{one}", f"{uid}_{one}"
    cred = tmp / "credentials.txt"
    print(f"命名空间 {ns}｜遗留身份 {uid}｜样本目录 {tmp}", flush=True)

    storage = build_storage("pg_minio", cache_root=tmp / "cache",
                            workspace_root=tmp / "ws")

    def migrator(**kw):
        return Migrator(storage, runtime_dir=tmp / ".runtime",
                        storyline_dir=tmp / ".storyline", **kw)

    async def scope_rows() -> dict[str, list[str]]:
        t = storage.db
        return {
            "users": digest(await t.select("users", where={"id": uid})),
            "conversations": digest(await t.select("conversations", where={"id": one})
                                    + await t.select("conversations", where={"id": two})),
            "messages": digest(await t.select("messages", where={"conv_id": one})
                               + await t.select("messages", where={"conv_id": two})),
            "materials": digest(await t.select("materials", where={"owner_user_id": uid})),
            "artifacts": digest(await t.select("artifacts", where={"session_id": sess})),
            "render_jobs": digest(await t.select("render_jobs", where={"session_id": out_sess})),
            "checkpoints": digest(await t.select("checkpoints", where={"run_id": f"{ns}run-a"})),
            "scheduled_jobs": digest(
                await t.select("scheduled_jobs", where={"name": f"{ns}heartbeat"})
                + await t.select("scheduled_jobs", where={"name": f"{ns}早间简报"})),
            "memories": digest(await t.select("memories", where={"user_id": uid})),
        }

    try:
        rep1 = {r.kind: r for r in await migrator(credentials_path=cred).run()}
        check(all(r.migrated > 0 for r in rep1.values()),
              f"第一遍八类都有迁入：{ {k: r.migrated for k, r in rep1.items()} }")
        snap1 = await scope_rows()
        check(all(snap1[k] for k in snap1), f"九张表都拿到本命名空间的行：{ {k: len(v) for k, v in snap1.items()} }")

        rep2 = {r.kind: r for r in await migrator(credentials_path=cred).run()}
        snap2 = await scope_rows()
        check(sum(r.migrated for r in rep2.values()) == 0,
              f"第二遍零迁入：{ {k: r.migrated for k, r in rep2.items()} }")
        check(snap1 == snap2, "真 PG 上两遍逐表快照一致（幂等）")
        rep3 = {r.kind: r for r in await migrator(dry_run=True).run()}
        check(await scope_rows() == snap1 and all(r.migrated == 0 for r in rep3.values()),
              "第三遍 dry-run：零写入，快照仍是那一份")

        # ---------- 内容形状 ----------
        hist = await storage.messages.history(uid, one)
        check([h["role"] for h in hist] == ["user", "assistant", "user", "assistant"],
              f"历史成对迁入：{[h['role'] for h in hist]}")
        check([h["seq"] for h in hist] == [1, 2, 3, 4],
              f"seq 从 1 递增：{[h['seq'] for h in hist]}")
        parts = hist[1]["qa"]["parts"]
        check([p["type"] for p in parts] == ["think", "tool call", "answer"],
              f"qa.parts 过 jsonb 原样回来：{[p.get('type') for p in parts]}")
        check(hist[0]["qa"]["migrated_from"] == f"{uid}/{one}.jsonl#0",
              f"幂等标记落在行里：{hist[0]['qa']['migrated_from']}")

        rows = await storage.db.select("users", where={"id": uid})
        line = cred.read_text(encoding="utf-8").splitlines()
        token = next((l.split("\t")[1] for l in line if l.startswith(uid + "\t")), "")
        check(len(line) == 1 and token, f"凭证文件一行：{[l.split(chr(9))[0] for l in line]}")
        check(len(rows[0]["token_hash"]) == 64 and rows[0]["token_hash"] != token,
              "库里只有 sha256，明文不落库")
        check(await storage.users.verify(token) == uid,
              "真库里反查：迁移发的 token 认得回遗留身份（换浏览器接得住历史）")
        check(await storage.users.verify("no-such-token") is None, "错 token 反查不到")

        mats = await storage.materials.list_visible(uid, one)
        if not mats:
            raise SystemExit("FAIL  迁移未落 materials 行，冒烟终止")
        mat_key = mats[0]["object_key"]
        check(mat_key.startswith(f"users/{uid}/convs/{one}/") and mats[0]["kind"] == "video",
              f"素材行在 materials：{ {k: mats[0][k] for k in ('filename', 'kind', 'object_key')} }")
        src = (tmp / ".storyline" / "uploads" / uid / one / "海边日落.mp4").read_bytes()
        url = await storage.objects.presign_get(mat_key)
        check(url.startswith("http"), f"回放是 http presigned 直链：{url[:80]}…")
        async with httpx.AsyncClient(timeout=60) as c:
            got = await c.get(url)
            rng = await c.get(url, headers={"range": "bytes=0-3"})
        check(got.status_code == 200 and got.content == src,
              f"MinIO 取回字节与原文件一致（{got.status_code} / {len(got.content)} 字节）")
        check(rng.status_code == 206 and len(rng.content) == 4,
              f"区间读 206 可用：{rng.status_code} {rng.headers.get('content-range')}")

        job = await storage.render_jobs.get(out_sess, "final")
        if not job:
            raise SystemExit("FAIL  迁移未落 render_jobs 行，冒烟终止")
        render_key = job["video_object_key"]
        check(job["status"] == "done" and job["percent"] == 100
              and render_key == f"renders/{out_sess}/final.mp4",
              f"成片进 render_jobs：{ {k: job.get(k) for k in ('status', 'percent', 'video_object_key')} }")
        async with httpx.AsyncClient(timeout=60) as c:
            v = await c.get(await storage.objects.presign_get(render_key))
        check(v.content == (tmp / ".storyline" / "out" / out_sess / "final.mp4").read_bytes(),
              f"成片字节在桶里可整读（{len(v.content)} 字节）")

        art = await storage.artifacts(sess, "_default").get("load_media")
        check("uploads" in str(art), f"节点产物进 artifacts：{str(art)[:70]}")

        jobs = {j["name"]: j for j in await storage.jobs.list()
                if j["name"].startswith(ns)}
        check(set(jobs) == {f"{ns}heartbeat", f"{ns}早间简报"},
              f"两份文件同表落位：{sorted(jobs)}")
        check(jobs[f"{ns}heartbeat"]["id"] == f"{ns}hb-1"
              and jobs[f"{ns}heartbeat"]["schedule"]["every_ms"] == 30000,
              "heartbeat 的 id 与 schedule 原样保留")
        check(await storage.checkpoints.load(f"{ns}run-a") is not None
              and await storage.checkpoints.load(f"{ns}run-b") is None,
              "completed run 进库、running run 拒迁")
        stale = [r for r in await storage.checkpoints.list_unfinished()
                 if str(r["run_id"]).startswith(ns)]
        check(stale == [], f"启动对账捞不到陈旧 run：{[r['run_id'] for r in stale]}")
        check((await storage.memories.read(uid, "user")).startswith("用户偏好"),
              "长期记忆进 memories")

        # ---------- 清理：本命名空间一行不留 ----------
        for key in [mat_key, render_key]:
            await storage.objects.delete(key)
        t = storage.db
        for table, col, vals in (
                ("messages", "conv_id", [one, two]),
                ("conversations", "id", [one, two]),
                ("materials", "owner_user_id", [uid]),
                ("memories", "user_id", [uid]),
                ("artifacts", "session_id", [sess]),
                ("render_jobs", "session_id", [out_sess]),
                ("checkpoints", "run_id", [f"{ns}run-a", f"{ns}run-b"]),
                ("scheduled_jobs", "name", [f"{ns}heartbeat", f"{ns}早间简报"])):
            for v_ in vals:
                await t.delete(table, where={col: v_})
        await t.delete("users", where={"id": uid})
        residue = await scope_rows()
        check(all(not v for v in residue.values()),
              f"残留 0 行：{ {k: len(v) for k, v in residue.items()} }")
        for key in [mats[0]["object_key"], job["video_object_key"]]:
            check(await storage.objects.head(key) is None, f"桶里对象已删：{key}")
    finally:
        await storage.close()

    print()
    print("FAILED" if FAILS else "SMOKE PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
