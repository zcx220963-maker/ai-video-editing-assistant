# -*- coding: utf-8 -*-
"""`run_migrate.py` 的专用离线测试：本地磁盘遗留 → PG/MinIO 那一层的幂等与形状。

按 D3 的规矩跑在内存替身上（`build_storage("memory")`），不碰容器；真机那半边由
`.smoke/b5_migrate_smoke.py` 在 PG + MinIO 上跑同一套遗留样本并连跑两遍对账。

覆盖：
  1. 连跑两遍**逐表快照完全一致**（第二遍各类 migrated=0），对象键也不增加。
  2. 会话历史：一行 .jsonl = user+assistant 成对；qa.parts 原样保留；写坏的尾行只跳过；
     迁移后新出现的遗留文件只补新行（seq 接得上）；话里的 mat-* 只认库里真有的。
  3. checkpoints：completed 迁入、running 拒迁（否则下次启动自动恢复会重放陈旧话）。
  4. scheduled_jobs：heartbeat/cron 两份文件同表落位、id 保留、同名/缺 name 跳过。
  5. memories：分类白名单外的文件拒迁；库里已有内容不被覆盖。
  6. artifacts：(session, artifact, node) 命中即跳。
  7. materials：字节进桶、可整读回原文件；同 sha256 第二次跑不再入库；非素材扩展名跳过。
  8. render_jobs：out/*.mp4 → 桶 + done/100 + video_object_key。
  9. 凭证：默认写不可登录占位（verify 反查不到）；--emit-credentials 才发真 token 且只写
     文件不打印；第二遍不重发。已迁完的库（只剩占位行）光给 --emit-credentials 什么都不发，
     要 --reissue-unclaimed 才把占位轮换成可登录；文件里记过的 id 不再轮换。
 10. dry-run：scanned>0 而全表零写入，凭证文件不生成。
 11. 不合规范的遗留 id 目录不迁入；临时工作区与本地内容缓存不进桶。

运行：  python tests/test_migrate.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import build_storage
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from run_migrate import Migrator

TABLES = ("users", "conversations", "messages", "materials", "artifacts",
          "render_jobs", "checkpoints", "scheduled_jobs", "memories")

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# --------------------------------------------------------------------------
# 遗留磁盘样本
# --------------------------------------------------------------------------

def write_legacy_tree(root: Path, *, ns: str = "", root_memory: bool = True) -> str:
    """造一棵「B1–B4 之前」的磁盘目录：八类遗留数据各有样本，含该被跳过的坏样本。

    可重复调用（内容固定），所以「迁移后又有新遗留文件冒出来」这类用例能直接重跑。
    ns 是给真机冒烟用的命名空间：共享开发库里要能安全清理，不撞已有 job 名。
    """
    uid = f"{ns}u-old"
    one, two = f"{ns}c-one", f"{ns}c-two"
    runtime = root / ".runtime"
    story = root / ".storyline"

    conv = runtime / "sessions" / uid
    conv.mkdir(parents=True, exist_ok=True)
    lines = [
        {"question": "第一条话",
         "answer": [{"type": "think", "content": "想一下"},
                    {"type": "tool call", "name": "web_search", "arguments": {"q": "x"}},
                    {"type": "answer", "content": "第一轮答复"}]},
        {"question": "第二条话",
         "answer": [{"type": "answer", "content": "第二轮答复\n带换行"}]},
    ]
    (conv / f"{one}.jsonl").write_text(
        "\n".join(json.dumps(l, ensure_ascii=False) for l in lines)
        + "\n{\"question\": 写坏的尾行", encoding="utf-8")
    (conv / f"{two}.jsonl").write_text(
        json.dumps({"question": "另一个会话", "answer": "字符串式答复"}, ensure_ascii=False),
        encoding="utf-8")
    (runtime / "sessions" / f"{ns}u bad id").mkdir(parents=True, exist_ok=True)

    ck = runtime / "checkpoints"
    ck.mkdir(parents=True, exist_ok=True)
    (ck / f"{ns}run-a.json").write_text(json.dumps({
        "version": 1, "run_id": f"{ns}run-a", "session_id": f"{uid}:{one}",
        "message": "第一条话", "iteration": 2,
        "messages": [{"role": "user", "content": "第一条话"}],
        "status": "completed", "created_at_ms": 1_700_000_000_000,
        "updated_at_ms": 1_700_000_000_500}), encoding="utf-8")
    (ck / f"{ns}run-b.json").write_text(json.dumps({
        "run_id": f"{ns}run-b", "session_id": f"{uid}:{one}", "message": "没跑完的",
        "iteration": 1, "messages": [], "status": "running",
        "created_at_ms": 1, "updated_at_ms": 2}), encoding="utf-8")

    (runtime / "heartbeat.json").write_text(json.dumps({"version": 1, "jobs": [{
        "id": f"{ns}hb-1", "name": f"{ns}heartbeat", "task": "heartbeat", "enabled": True,
        "delete_after_run": False, "schedule": {"kind": "every", "every_ms": 30000},
        "state": {"next_run_at_ms": 1_700_000_030_000}}]}), encoding="utf-8")
    (runtime / "cron_jobs.json").write_text(json.dumps({"version": 1, "jobs": [
        {"id": f"{ns}cron-1", "name": f"{ns}早间简报", "task": "生成今天的简报",
         "enabled": True, "delete_after_run": True,
         "schedule": {"kind": "at", "at_ms": 1_700_000_000_000},
         "state": {"next_run_at_ms": 1_700_000_000_000}},
        {"id": f"{ns}cron-2", "name": f"{ns}heartbeat", "task": "重名占用 UNIQUE(name)",
         "schedule": {"kind": "every", "every_ms": 60000}, "state": {}},
        {"id": f"{ns}cron-3", "task": "没有 name", "schedule": {}, "state": {}},
    ]}), encoding="utf-8")

    mem = runtime / "memory" / uid
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "User.md").write_text("用户偏好：短镜头、少字幕。", encoding="utf-8")
    (mem / "scratch.md").write_text("不在白名单里的分类", encoding="utf-8")
    if root_memory:
        (runtime / "memory" / "Tool.md").write_text("工具记忆落在 default 名下",
                                                    encoding="utf-8")

    art = story / "cache" / "sessions" / f"{uid}_{one}" / "load_media"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_default.json").write_text(json.dumps({
        "node": "load_media", "artifact_id": "_default", "session_id": f"{uid}:{one}",
        "payload": {"media": [{"path": ".storyline\\uploads\\a.mp4"}],
                    "clips": ["m0"]}}), encoding="utf-8")

    up = story / "uploads" / uid / one
    up.mkdir(parents=True, exist_ok=True)
    (up / "海边日落.mp4").write_bytes(b"FAKE-VIDEO-BYTES-0123456789")
    (up / "读一下.txt").write_text("不是素材", encoding="utf-8")

    out = story / "out" / f"{uid}_{one}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "final.mp4").write_bytes(b"RENDERED-BYTES-0123456789abcdef")
    return uid


def _read_all(storage, key: str) -> bytes:
    async def _go() -> bytes:
        return b"".join([c async for c in storage.objects.get_stream(key)])
    return asyncio.run(_go())


async def _snapshot(storage) -> dict[str, list[str]]:
    """逐表内容指纹：两遍之间任何新增/改动都会露出来。"""
    out: dict[str, list[str]] = {}
    for t in TABLES:
        rows = await storage.db.select(t)
        out[t] = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return out


class Env:
    """一个用例一套临时磁盘 + 一个开着的内存存储（迁移器跑完不 close，可连跑多遍）。"""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        write_legacy_tree(self.root)
        self.storage = build_storage("memory", cache_root=self.root / "cache",
                                     workspace_root=self.root / "ws")

    def migrator(self, **kw) -> Migrator:
        return Migrator(self.storage, runtime_dir=self.root / ".runtime",
                        storyline_dir=self.root / ".storyline", **kw)

    def run(self, credentials: Path | None = None, **kw) -> dict[str, object]:
        """跑一遍，返回 kind → Report。"""
        return {r.kind: r for r in asyncio.run(
            self.migrator(credentials_path=credentials, **kw).run())}

    def snap(self) -> dict[str, list[str]]:
        return asyncio.run(_snapshot(self.storage))

    def objkeys(self) -> list[str]:
        return sorted(self.storage.objects.data)

    def history(self, user: str, conv: str) -> list[dict]:
        return asyncio.run(self.storage.messages.history(user, conv))


def case_double_run() -> None:
    print("\n[1] 连跑两遍：快照逐表一致，第二遍零迁入")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        rep1 = env.run()
        counts = {k: (r.scanned, r.migrated) for k, r in rep1.items()}
        check(all(r.migrated > 0 for r in rep1.values()),
              f"第一遍八类都有迁入：{ {k: v[1] for k, v in counts.items()} }")
        snap1, keys1 = env.snap(), env.objkeys()

        rep2 = env.run()
        snap2, keys2 = env.snap(), env.objkeys()
        check(snap1 == snap2, f"两遍快照逐表一致：{ {t: len(snap1[t]) for t in TABLES} }")
        check(sum(r.migrated for r in rep2.values()) == 0,
              f"第二遍零迁入：{ {k: r.migrated for k, r in rep2.items()} }")
        check(all(r.skipped for r in rep2.values() if r.scanned),
              "第二遍每条扫到的都给出跳过理由：" + str(
                  [r.skipped[0] for r in rep2.values() if r.scanned][:4]))
        check(keys1 == keys2, f"对象键不增长（{len(keys1)} 个）")


def case_messages() -> None:
    print("\n[2] 会话历史成对迁入，坏尾行只跳过")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        rep = env.run()
        hist = env.history("u-old", "c-one")
        check([h["role"] for h in hist] == ["user", "assistant", "user", "assistant"],
              f"两行 .jsonl → 四条消息：{[h['role'] for h in hist]}")
        check([h["seq"] for h in hist] == [1, 2, 3, 4],
              f"seq 在会话内连续：{[h['seq'] for h in hist]}")
        check([h["content"] for h in hist]
              == ["第一条话", "第一轮答复", "第二条话", "第二轮答复\n带换行"],
              f"正文按 QA 结构落位：{[h['content'] for h in hist]}")
        parts = hist[1]["qa"]["parts"]
        check([p["type"] for p in parts] == ["think", "tool call", "answer"]
              and parts[1]["name"] == "web_search",
              f"assistant 的 qa.parts 原样保留：{[p.get('type') for p in parts]}")
        check(hist[0]["qa"]["migrated_from"] == "u-old/c-one.jsonl#0"
              and hist[2]["qa"]["migrated_from"] == "u-old/c-one.jsonl#1",
              f"每行带幂等标记：{[h['qa']['migrated_from'] for h in hist]}")
        check(rep["messages"].scanned == 3 and rep["messages"].migrated == 6,
              f"写坏的尾行在解析阶段丢弃，不入库也不计："
              f"scanned={rep['messages'].scanned} migrated={rep['messages'].migrated}")

        h2 = env.history("u-old", "c-two")
        check(len(h2) == 2 and h2[1]["content"] == "字符串式答复",
              f"answer 是裸字符串也能落（旧格式宽容）：{[h['content'] for h in h2]}")
        convs = {c["id"]: c["title"] for c in asyncio.run(
            env.storage.conversations.list_for("u-old"))}
        check(set(convs) >= {"c-one", "c-two"} and set(convs.values()) == {"新对话"},
              f"会话行随历史建出来（旧磁盘没有标题）：{convs}")


def case_new_files_after_run() -> None:
    print("\n[2b] 迁移后新出现的遗留文件只补新行；mat-* 只认库里真有的")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        env.run()

        async def _seed() -> str:
            return (await env.storage.materials.register(
                "u-old", None, "users/u-old/known.mp4", "known.mp4", "video",
                bytes_=4, sha256="abcd"))["id"]
        mid = asyncio.run(_seed())

        (env.root / ".runtime" / "sessions" / "u-old" / "c-three.jsonl").write_text(
            json.dumps({"question": f"用附件 {mid} 和 mat-ghost 各剪一版",
                        "answer": [{"type": "answer", "content": "好的"}]},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        rep = env.run()
        check(rep["messages"].migrated == 2,
              f"第二遍只补新会话的两行：{rep['messages'].migrated}")
        h3 = env.history("u-old", "c-three")
        check(h3[0]["attachments"] == [mid],
              f"悬空 id 不进 attachments（mat-ghost 被丢掉）：{h3[0]['attachments']}")
        check(rep["users"].migrated == 0
              and any("已存在" in s for s in rep["users"].skipped),
              f"新增会话不重复补身份：{rep['users'].skipped[:2]}")


def case_checkpoints_and_jobs() -> None:
    print("\n[3] checkpoints：completed 迁入、running 拒迁")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        rep = env.run()
        ck = asyncio.run(env.storage.checkpoints.load("run-a"))
        check(ck is not None and ck["status"] == "completed" and ck["iteration"] == 2,
              f"completed run 迁入：{ {k: ck.get(k) for k in ('run_id', 'session_id', 'status', 'iteration')} }")
        check(asyncio.run(env.storage.checkpoints.load("run-b")) is None,
              "running run 没进库")
        check(any("重放" in s for s in rep["checkpoints"].skipped),
              f"拒迁理由点明危害：{rep['checkpoints'].skipped}")
        check(asyncio.run(env.storage.checkpoints.list_unfinished()) == [],
              "启动对账不会捞到陈旧 run")

    print("\n[4] scheduled_jobs：两份文件同表、id 保留、坏 job 跳过")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        rep = env.run()
        jobs = {j["name"]: j for j in asyncio.run(env.storage.jobs.list())}
        check(set(jobs) == {"heartbeat", "早间简报"}, f"落位的 job：{sorted(jobs)}")
        check(jobs["heartbeat"]["id"] == "hb-1"
              and jobs["heartbeat"]["schedule"]["every_ms"] == 30000,
              f"heartbeat 的 id 与 schedule 原样保留：{jobs['heartbeat']['id']}")
        check(jobs["早间简报"]["delete_after_run"] is True, "cron 的 delete_after_run 落住")
        check(any("同名" in s for s in rep["scheduled_jobs"].skipped),
              f"重名 job 按 UNIQUE(name) 跳过：{rep['scheduled_jobs'].skipped}")
        check(any("缺 name" in s for s in rep["scheduled_jobs"].skipped),
              "缺 name 的 job 跳过（表上 name 非空唯一）")
        check(env.run()["scheduled_jobs"].migrated == 0, "第二遍 job 零迁入")


def case_memories_and_artifacts() -> None:
    print("\n[5] memories 白名单 + artifacts 三元组")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        rep = env.run()

        async def _mem():
            return (await env.storage.memories.read("u-old", "user"),
                    await env.storage.memories.read("default", "tool"))
        user_mem, tool_mem = asyncio.run(_mem())
        check(user_mem == "用户偏好：短镜头、少字幕。", f"user 分类内容：{user_mem!r}")
        check(tool_mem == "工具记忆落在 default 名下",
              f"根目录下的记忆归 default：{tool_mem!r}")
        check(any("白名单" in s for s in rep["memories"].skipped),
              f"scratch.md 被拒：{rep['memories'].skipped}")

        async def _art():
            repo = env.storage.artifacts("u-old:c-one", "_default")
            return await repo.get("load_media"), await repo.executed()
        payload, executed = asyncio.run(_art())
        check(executed == ["load_media"], f"节点名进 artifacts 表：{executed}")
        check("uploads" in str(payload),
              f"payload 原样带入（仍含遗留本地路径）：{str(payload)[:70]}")
        rep2 = env.run()
        check(rep2["memories"].migrated == 0 and rep2["artifacts"].migrated == 0,
              "第二遍记忆与产物零迁入")
        check(any("不覆盖" in s for s in rep2["memories"].skipped),
              f"库里已有内容优先：{rep2['memories'].skipped}")


def case_media() -> None:
    print("\n[6] 素材与成片：字节进桶、行进表、坏样本跳过")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        rep = env.run()
        mats = asyncio.run(env.storage.materials.list_visible("u-old", "c-one"))
        check([m["filename"] for m in mats] == ["海边日落.mp4"],
              f"上传目录里的素材按本人可见：{[m['filename'] for m in mats]}")
        row = mats[0]
        check(row["kind"] == "video" and row["origin"] == "upload"
              and row["object_key"].startswith("users/u-old/convs/c-one/"),
              f"kind/origin/对象键：{ {k: row[k] for k in ('kind', 'origin', 'object_key')} }")
        src = env.root / ".storyline" / "uploads" / "u-old" / "c-one" / "海边日落.mp4"
        check(_read_all(env.storage, row["object_key"]) == src.read_bytes(),
              "桶里字节与原文件一致")
        check(any("入库失败" in s for s in rep["materials"].skipped),
              f"txt 不是素材，跳过并说明：{rep['materials'].skipped}")

        job = asyncio.run(env.storage.render_jobs.get("u-old_c-one", "final"))
        check(job and job["status"] == "done" and job["percent"] == 100
              and job["video_object_key"].startswith("renders/u-old_c-one/"),
              f"out/ 成片 → render_jobs done/100：{ {k: job.get(k) for k in ('status', 'percent', 'video_object_key')} }")
        out = env.root / ".storyline" / "out" / "u-old_c-one" / "final.mp4"
        check(_read_all(env.storage, job["video_object_key"]) == out.read_bytes(),
              "成片字节进桶")

        rep2 = env.run()
        check(rep2["materials"].migrated == 0 and rep2["render_jobs"].migrated == 0,
              "第二遍素材与成片零迁入（sha256 去重 + 会话/产物去重）")
        check(any("同内容素材" in s for s in rep2["materials"].skipped),
              f"跳过理由给出已存在的 id：{rep2['materials'].skipped}")


def case_credentials() -> None:
    print("\n[7] 遗留身份的凭证：默认占位不可登录，--emit-credentials 才发真 token")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        env.run()

        async def _probe() -> tuple[dict, list[dict]]:
            rows = await env.storage.db.select("users")
            return (next(r for r in rows if r["id"] == "u-old"), rows)
        placeholder, rows = asyncio.run(_probe())
        check([r["id"] for r in rows] == ["u-old", "default"],
              f"只补合法遗留 id（坏 id 目录不迁入；根目录记忆归 default 也补一行）："
              f"{[r['id'] for r in rows]}")
        check(asyncio.run(env.storage.users.verify("anything")) is None
              and asyncio.run(env.storage.users.verify(placeholder["token_hash"])) is None,
              "占位行反查不到任何身份（身份存在 ≠ 可登录）")
        check(len(placeholder["token_hash"]) == 64
              and all(c in "0123456789abcdef" for c in placeholder["token_hash"]),
              "库里只有 sha256 形态的哈希")

    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        cred = env.root / "out" / "legacy-credentials.txt"
        env.run(credentials=cred)
        lines = cred.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 1, f"凭证文件一行一个遗留身份：{[l.split(chr(9))[0] for l in lines]}")
        uid, _, token = lines[0].partition("\t")
        check(uid == "u-old" and len(token) == 43,
              f"每行是 user_id<TAB>32 字节 urlsafe token（值不打印）：{uid} / {len(token)} 字符")
        check(asyncio.run(env.storage.users.verify(token)) == "u-old",
              "发出去的 token 真能反查回遗留 id（历史接得回来）")

        rep2 = env.run(credentials=cred)
        check(rep2["users"].migrated == 0 and len(cred.read_text().splitlines()) == 1,
              f"第二遍不重发凭证：{rep2['users'].skipped}")
        check(asyncio.run(env.storage.users.verify(token)) == "u-old",
              "旧 token 仍认（重跑没换掉哈希）")

    # 已迁完的库补凭证：占位行早就在了，光给 --emit-credentials 什么都不发——
    # 这一条正是本机第一次真跑时踩的空转，必须 --reissue-unclaimed 才轮换成可登录。
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        env.run()                                   # 第一遍：占位哈希
        cred = env.root / "out" / "legacy-credentials.txt"
        rep_plain = env.run(credentials=cred)
        check(rep_plain["users"].migrated == 0 and not cred.exists(),
              f"只给 --emit-credentials：已存在的占位行不轮换、文件也不生成"
              f"（{rep_plain['users'].skipped}）")
        before_hash = asyncio.run(env.storage.users.get("u-old"))["token_hash"]

        rep_re = env.run(credentials=cred, reissue_unclaimed=True)
        lines = cred.read_text(encoding="utf-8").splitlines()
        uid, _, tok2 = lines[0].partition("\t")
        check(rep_re["users"].migrated == 1 and len(lines) == 1 and uid == "u-old",
              f"--reissue-unclaimed 把占位轮换成真凭证：{uid}（{len(tok2)} 字符，值不打印）")
        check(asyncio.run(env.storage.users.verify(tok2)) == "u-old",
              "轮换后的 token 真能登进那个遗留身份（历史接得回来）")
        after = asyncio.run(env.storage.users.get("u-old"))
        check(after["token_hash"] != before_hash,
              "轮换改的就是那一行的哈希（不是新插了一行）")

        rep_re2 = env.run(credentials=cred, reissue_unclaimed=True)
        check(rep_re2["users"].migrated == 0
              and cred.read_text(encoding="utf-8").splitlines() == lines,
              f"文件里记过的 id 不再轮换（否则本人手上那把当场失效）：{rep_re2['users'].skipped}")
        check(asyncio.run(env.storage.users.verify(tok2)) == "u-old",
              "重跑后发出去的那把 token 仍然有效")


def case_dry_run() -> None:
    print("\n[8] dry-run：只统计不写")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        cred = env.root / "creds.txt"
        rep = env.run(dry_run=True, credentials=cred)
        snap = env.snap()
        check(all(v == [] for v in snap.values()),
              f"九张表全空：{ {k: len(v) for k, v in snap.items()} }")
        check(all(r.scanned > 0 for r in rep.values()),
              f"各类都扫到了样本：{ {k: r.scanned for k, r in rep.items()} }")
        check(all(r.migrated == 0 for r in rep.values()), "迁入计数全 0")
        check(all(any("dry-run" in s for s in r.skipped) for r in rep.values()),
              "dry-run 的跳过理由写得很明白")
        check(not cred.exists(), "凭证文件没生成")
        check(env.objkeys() == [], f"桶里没落任何对象：{env.objkeys()}")


def case_no_side_effects() -> None:
    print("\n[9] 不合规范的 id 与临时目录：都不动")
    with tempfile.TemporaryDirectory() as td:
        env = Env(td)
        (env.root / ".runtime" / "workspace" / "u-old_c_one" / "frames").mkdir(parents=True)
        (env.root / ".runtime" / "workspace" / "u-old_c_one" / "frames" / "f1.jpg"
         ).write_bytes(b"X")
        (env.root / ".storyline" / "cache" / "objects" / "ab").mkdir(parents=True,
                                                                      exist_ok=True)
        (env.root / ".storyline" / "cache" / "objects" / "ab" / "abc").write_bytes(b"Y")
        rep = env.run()
        keys = env.objkeys()
        check(not any("frames" in k or k.endswith("/abc") for k in keys),
              f"抽帧与本地内容缓存没进桶：{keys}")
        check(any("命名规范" in s for s in rep["users"].skipped),
              f"带空格的遗留 id 目录被拒：{rep['users'].skipped}")
        check(env.history("u bad id", "c-one") == [], "被拒的 id 名下什么都没有")


def main() -> None:
    for fn in (case_double_run, case_messages, case_new_files_after_run,
               case_checkpoints_and_jobs, case_memories_and_artifacts, case_media,
               case_credentials, case_dry_run, case_no_side_effects):
        fn()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
