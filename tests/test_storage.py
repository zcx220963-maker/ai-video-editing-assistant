"""上线存储层离线验证：不联网、不起容器，全部跑在内存替身 + 静态检查上。

运行：  python tests/test_storage.py

覆盖 spec §10「离线」四条：build_storage 后端分派与未知后端报错、重依赖延迟 import、
schema.sql 静态检查、MemoryObjectStore 与内存 repos 的契约行为（含 SKIP LOCKED 语义
在替身侧的等价断言）。同一套仓储契约在真 PG+MinIO 上由 test_storage_contract.py 再跑一遍。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import agent_framework.storage as st_mod
from agent_framework.storage import (
    Cond,
    IntegrityConflict,
    MemoryDatastore,
    MemoryObjectStore,
    StorageUnavailable,
    Workspace,
    build_storage,
)
from agent_framework.storage.ddl import load_schema, read_migrations_sql, split_statements
from agent_framework.storage.object_store import ContentCache

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def raises(fn, exc, label: str) -> None:
    try:
        fn()
    except exc:
        check(True, label)
    except Exception as e:            # noqa: BLE001
        check(False, f"{label}（实得 {type(e).__name__}: {e}）")
    else:
        check(False, f"{label}（未抛异常）")


async def araises(coro_fn, exc, label: str) -> None:
    """断言协程抛特定异常（不能用 asyncio.run：本测试整体跑在一个事件循环里）。"""
    try:
        await coro_fn()
    except exc:
        check(True, label)
    except Exception as e:            # noqa: BLE001
        check(False, f"{label}（实得 {type(e).__name__}: {e}）")
    else:
        check(False, f"{label}（未抛异常）")


async def run_aiter(ait) -> bytes:
    return b"".join([c async for c in ait])


# --------------------------------------------------------------------------
# 1. 后端分派 + 密钥只从环境读
# --------------------------------------------------------------------------


async def test_factory(tmp: Path) -> None:
    s = build_storage("memory", cache_root=tmp / "c", workspace_root=tmp / "w")
    check(isinstance(s, st_mod.Storage) and s.backend == "memory", "build memory → Storage")
    check(isinstance(s.db, MemoryDatastore), "memory → MemoryDatastore")
    check(isinstance(s.objects, MemoryObjectStore), "memory → MemoryObjectStore")
    check(isinstance(s.workspace, Workspace) and s.workspace.objects is s.objects,
          "Workspace 与 objects 同实例")
    check(all(getattr(s, a) is not None for a in
              ("users", "conversations", "messages", "materials", "render_jobs",
               "checkpoints", "inbox", "tasks", "subagents", "jobs", "memories",
               "skills")), "12 个表级仓储全部装配")
    check(callable(s.artifacts) and type(s.artifacts("s", "a")).__name__ == "ArtifactsRepo",
          "artifacts 按 (会话,产物) 现取")
    raises(lambda: build_storage("sqlite"), ValueError, "未知 backend 抛 ValueError")

    saved = {k: os.environ.pop(k, None) for k in st_mod.ENV_KEYS}
    real_loader = st_mod._load_env_file
    st_mod._load_env_file = lambda *a, **k: None   # 本用例要考察「环境变量缺失」，不能被 .env 补上
    try:
        raises(lambda: build_storage("pg_minio"), StorageUnavailable,
               "缺环境变量时 pg_minio 拒绝启动（不静默退回本地）")
        os.environ["PG_DSN"] = "postgresql://u:p@127.0.0.1:1/none"
        for k in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            os.environ[k] = "x"
        try:
            pg = build_storage("pg_minio")
        except Exception:                 # 未装 SDK 或引擎不可用：构造阶段就失败
            check(True, "引擎不可用（未装 SDK / 连不上）时构造即抛，不静默降级")
        else:
            await araises(pg.start, StorageUnavailable, "PG/MinIO 不可达时 start() 抛错")
    finally:
        st_mod._load_env_file = real_loader
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


async def test_lazy_imports() -> None:
    """minio / asyncpg 未安装也必须能 import 本包（照 mq.py 的 KafkaMessageQueue 范式）。

    子进程里挡住这两个包，而不是看主进程的 sys.modules：SDK 装好之后主进程里
    它们迟早会出现，那种断言只反映用例顺序，不反映导入图。
    """
    probe = (
        "import builtins, sys, tempfile\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in ('minio', 'asyncpg'):\n"
        "        raise ImportError('blocked by test: ' + name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "import agent_framework.storage as s\n"
        "root = tempfile.mkdtemp()\n"
        "st = s.build_storage('memory', cache_root=root + '/c', workspace_root=root + '/w')\n"
        "assert st.backend == 'memory' and st.objects.bucket\n"
    )
    # 子进程的 sys.path[0] 是它的工作目录：必须是项目根，否则 import agent_framework
    # 先因路径不成立而失败，测不到「挡住 SDK 也照样能用」这件事本身。
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    check(r.returncode == 0,
          "屏蔽 minio/asyncpg 的子进程仍能 import 本包并构造 memory 后端"
          + ("" if r.returncode == 0 else f"（{r.stderr.strip()[-160:]}）"))
    import importlib

    m = importlib.import_module("agent_framework.storage.object_store")
    check(hasattr(m, "MinioObjectStore"), "MinioObjectStore 类可见（构造时才 import SDK）")
    raises(lambda: m.MinioObjectStore("", "", "", "b"), ValueError,
           "空 endpoint/密钥直接拒绝构造")


# --------------------------------------------------------------------------
# 2. schema.sql 静态检查
# --------------------------------------------------------------------------

# 全局共享表：按设计就没有 owner 列（技能对所有用户一致、心跳/定时属部署级、
# 边表随父表受约），显式列出以免静态检查假阳性。users 是归属表本身，其主键 id
# 就是其它表的 owner 列。
GLOBAL_TABLES = {"users", "skills", "skill_files", "scheduled_jobs", "task_edges",
                 "checkpoint_entries"}   # 一致点链随父 run 归属，本行不重复挂会话列
SCOPE_COLS = ("user_id", "owner_user_id", "scope", "session_id", "conv_id")
PATH_COLS = {"path", "file_path", "local_path", "abs_path", "filepath", "dir", "root"}


async def test_schema_static() -> None:
    tables, sequences, sql = load_schema()
    check(len(tables) == 20, f"20 张表（实得 {len(tables)}）")
    check(sequences == frozenset({"task_seq"}), "序列只声明 task_seq")
    check(set(tables) == {
        "users", "conversations", "messages", "materials", "upload_sessions",
        "artifacts", "render_jobs",
        "checkpoints", "checkpoint_entries", "inbox_messages", "tasks", "task_edges",
        "subagents", "scheduled_jobs", "memories", "skills", "skill_files", "timelines",
        "app_secrets", "token_usage"}, "表名与设计一一对应")

    n_create = sql.count("CREATE TABLE")
    check(n_create == sql.count("CREATE TABLE IF NOT EXISTS"), "建表语句全幂等")
    check(sql.count("CREATE SEQUENCE") == sql.count("CREATE SEQUENCE IF NOT EXISTS")
          == 1, "序列语句幂等")
    check(sql.count("CREATE INDEX") == sql.count("CREATE INDEX IF NOT EXISTS"),
          "索引语句全幂等")
    check("DROP " not in sql.upper(), "DDL 不含 DROP（可反复执行）")

    # 放宽已存在对象的约束只能放 migrations.sql（DROP 不允许进 schema.sql）
    mig = read_migrations_sql()
    mig_stmts = split_statements(mig)
    check(all(s.upper().startswith(("ALTER TABLE ", "CREATE INDEX ", "DO ")) for s in mig_stmts),
          f"migrations 只含放宽/补列/一次性变换（实得 {mig_stmts[:1]}）")
    check(sum(s.upper().startswith("DO ") for s in mig_stmts) == 1,
          "有状态的一次性变换用 DO 块表达：条件不成立时整块跳过，仍可无条件重跑")
    check(mig.upper().count("DROP CONSTRAINT IF EXISTS")
          == mig.upper().count("ADD CONSTRAINT") > 0,
          "migrations 的 ADD CONSTRAINT 都配了 DROP IF EXISTS（可反复执行）")

    # schema.sql 的 checkpoints.status 与 migrations.sql 里放宽它的那份必须给出**同一组**
    # 取值。漂移的后果很重、且只在特定库上复现：migrations 里那份若比 schema 窄，
    # 只要库里已有一条 awaiting_approval 的挂起 run，加约束就会因「已有行违反」失败在
    # 启动流程里——表现是「服务起不来」。真机踩过一次（checkpoints_status_check）。
    #
    # 只比对 checkpoints 那一处：其它表也有叫 status 的列，取值集合各不相同。
    def _checkpoints_status(text: str) -> set:
        found: set = set()
        for m in re.finditer(
                r"checkpoints_status_check(.*?);", text, re.I | re.S):
            blk = m.group(1)
            for v in re.findall(r"status\s+IN\s*\(([^)]*)\)", blk, re.I):
                found |= {x.strip().strip("'") for x in v.split(",") if x.strip()}
        # schema.sql 里该约束是匿名随列写的，直接抓 checkpoints 建表段
        seg = re.search(r"CREATE TABLE IF NOT EXISTS checkpoints\s*\((.*?)\n\);",
                        text, re.I | re.S)
        if seg:
            for v in re.findall(r"status\s+IN\s*\(([^)]*)\)", seg.group(1), re.I):
                found |= {x.strip().strip("'") for x in v.split(",") if x.strip()}
        return found

    in_schema, in_mig = _checkpoints_status(sql), _checkpoints_status(mig)
    check(in_schema == in_mig and "awaiting_approval" in in_schema,
          f"checkpoints.status 取值两边一致且含挂起态"
          f"（schema={sorted(in_schema)}，migrations={sorted(in_mig)}）")
    check(not [s for s in split_statements(sql) if s.upper().startswith("ALTER")],
          "schema.sql 仍不含 ALTER：放宽一律在 migrations.sql")

    scoped = [t for t, m in tables.items()
              if t not in GLOBAL_TABLES and not (set(m.names) & set(SCOPE_COLS))]
    check(not scoped, f"每张业务表都带归属/作用域列（违例：{scoped}）")
    leaked = {t: sorted(set(m.names) & PATH_COLS) for t, m in tables.items()}
    check(not any(leaked.values()), f"不存在文件路径列（违例：{leaked}）")
    check("object_key" in tables["materials"].names and "sha256" in tables["materials"].names,
          "素材以 object_key + sha256 定位，不落地路径")
    check(tables["artifacts"].pk == ("session_id", "node", "artifact_id"),
          "artifacts 复合主键 = 会话/节点/产物")
    check(tables["messages"].column("qa").is_json and tables["materials"].column("kind").type == "text",
          "jsonb 与标量类型解析正确")
    check(tables["messages"].column("id").auto, "messages.id 为 bigserial 自增")

    enums = {"messages.role": ("user", "assistant"),
             "materials.kind": ("video", "audio", "image"),
             "materials.origin": ("upload", "library", "bgm", "url"),
             "render_jobs.status": ("queued", "running", "done", "failed"),
             "checkpoints.status": ("running", "completed", "failed", "superseded", "awaiting_approval"),
             "checkpoint_entries.kind": ("delta", "full"),
             "tasks.status": ("pending", "claimed", "completed"),
             "subagents.status": ("idle", "working", "shutdown"),
             "memories.category": ("user", "tool")}
    bad = {k: tables[k.split(".")[0]].column(k.split(".")[1]).enum for k in enums
           if tables[k.split(".")[0]].column(k.split(".")[1]).enum != enums[k]}
    check(not bad, f"CHECK (col IN ...) 全部解析成列枚举（违例：{bad}）")
    check(tables["messages"].column("role").allows("user")
          and not tables["messages"].column("role").allows("system"),
          "枚举判定可用于两引擎写入前的同一道校验")


# --------------------------------------------------------------------------
# 3. 对象存储契约（内存替身）
# --------------------------------------------------------------------------


async def test_object_store(tmp: Path) -> None:
    objs = MemoryObjectStore(tmp / "cache", bucket="creation-assets")
    await objs.ensure_ready()

    async def chunks(payload: bytes):
        for i in range(0, len(payload), 4):
            yield payload[i:i + 4]

    info = await objs.put("users/u1/demo.mp4", chunks(b"0123456789"),
                          content_type="video/mp4")
    check(info.bytes == 10 and len(info.sha256) == 64 and info.content_type == "video/mp4",
          "put 返回 ObjectInfo（bytes/sha256/content_type）")
    head = await objs.head("users/u1/demo.mp4")
    check(head is not None and head.sha256 == info.sha256, "head 命中同一 sha256")
    check(await objs.head("users/u1/missing.mp4") is None, "head 不存在返回 None 而非抛错")
    check(await run_aiter(objs.get_stream("users/u1/demo.mp4")) == b"0123456789",
          "get_stream 全量字节一致")
    check(await run_aiter(objs.get_stream("users/u1/demo.mp4", start=6)) == b"6789",
          "get_stream 支持 start（视频拖动）")
    check(await run_aiter(objs.get_stream("users/u1/demo.mp4", start=2, end=4)) == b"234",
          "get_stream 支持 end 闭区间语义")
    url = await objs.presign_get("users/u1/demo.mp4", ttl_sec=60)
    check(url.startswith("memory://creation-assets/users/u1/demo.mp4"), "presign 含桶与 key")

    dst = tmp / "ws"
    p1 = await objs.localize("users/u1/demo.mp4", dst)
    check(p1.exists() and p1.read_bytes() == b"0123456789", "localize 落真实文件（供 ffmpeg）")
    p2 = await objs.localize("users/u1/demo.mp4", tmp / "ws2")
    cached_files = [p for p in objs.cache.dir.rglob("*") if p.is_file()]
    check(p2.read_bytes() == b"0123456789" and len(cached_files) == 1,
          "两次 localize 共用一份内容寻址缓存（不重复占盘）")
    await objs.delete("users/u1/demo.mp4")
    check(await objs.head("users/u1/demo.mp4") is None, "delete 之后 head 为空")
    await araises(lambda: run_aiter(objs.get_stream("users/u1/demo.mp4")),
                  StorageUnavailable, "读不存在的 key 抛 StorageUnavailable")

    cache = ContentCache(tmp / "cache2", max_bytes=1024)
    src = tmp / "big.bin"
    src.write_bytes(b"x" * 8)
    await cache.store(src, "a" * 64)
    src2 = tmp / "big2.bin"
    src2.write_bytes(b"y" * 8)
    await cache.store(src2, "b" * 64)
    check(cache.path_for("a" * 64).exists() and cache.path_for("b" * 64).exists(),
          "store 按 sha256 内容寻址落盘")
    out = await cache.fetch_to("b" * 64, tmp / "out", "f.bin")
    check(out is not None and out.read_bytes() == b"y" * 8, "fetch_to 命中缓存")
    check(await cache.fetch_to("c" * 64, tmp / "out", "g.bin") is None,
          "fetch_to 未命中返回 None（调用方回源）")
    cache.max_bytes = 10
    cache.touch(cache.path_for("b" * 64))       # b 最新，a 该被逐出
    dropped = cache.evict()
    check(dropped == 1 and not cache.path_for("a" * 64).exists()
          and cache.path_for("b" * 64).exists(), "超预算按 mtime 逐出最旧一份")


# --------------------------------------------------------------------------
# 4. 仓储契约：归属、序列、领取、清理
# --------------------------------------------------------------------------


async def test_repos(s: st_mod.Storage) -> None:
    uid, tok = await s.users.register("dev")
    check(uid.startswith("u-") and len(tok) >= 32, "register 发随机 token")
    check(await s.users.verify(tok) == uid, "verify 由 token 反查 user_id")
    check(await s.users.verify(tok + "x") is None, "错 token 返回 None")
    check(await s.users.get(uid) is not None, "库里只存哈希不存明文")
    row = await s.db.get_by_pk("users", {"id": uid})
    check(tok not in str(row), "行内容不含明文 token")

    await s.conversations.ensure(uid, "c1", "第一条")
    await s.conversations.ensure(uid, "c1", "改名")
    check(len(await s.conversations.list_for(uid)) == 1, "ensure 幂等（不重复插行）")
    await araises(lambda: s.conversations.ensure("u-other", "c1"),
                  IntegrityConflict, "他人会话 id 复用被拒")

    m1 = await s.messages.append(uid, "c1", "user", "你好", attachments=["mat-a"])
    m2 = await s.messages.append(uid, "c1", "assistant", "在的")
    check((m1["seq"], m2["seq"]) == (1, 2), "seq 会话内单调")
    check(m1["attachments"] == ["mat-a"], "jsonb 附件原样往返")
    hist = await s.messages.history(uid, "c1")
    check([h["role"] for h in hist] == ["user", "assistant"], "history 按 seq 升序")
    check(await s.messages.history("u-other", "c1") == [], "无归属者读不到别人的历史")
    await araises(lambda: s.messages.append("u-other", "c1", "user", "投毒"),
                  IntegrityConflict, "越权写别人的会话直接抛")
    await s.messages.set_qa(m2["id"], {"question": "你好", "answer": [{"type": "answer"}]})
    got = await s.db.get_by_pk("messages", {"id": m2["id"]})
    check(got["qa"]["question"] == "你好", "qa 结构可回写（取代 .jsonl 追加）")

    mat = await s.materials.register(uid, "c1", "users/u1/c1/mat-x.mp4", "海边日落.mp4",
                                     "video", bytes_=1234, sha256="ab" * 32,
                                     duration_sec=8.5, width=1920, height=1080,
                                     has_audio=True)
    check(mat["id"].startswith("mat-"), "素材主键是 material_id 而非路径")
    ok, bad = await s.materials.resolve([mat["id"], "mat- nope"], user_id=uid)
    check(len(ok) == 1 and bad == ["mat- nope"], "resolve 分离命中与越权 id")
    other = await s.materials.register("u-2", None, "users/u2/lib/other.mp4", "别人的.mp4",
                                       "video", bytes_=1, sha256="cd" * 32)
    ok2, bad2 = await s.materials.resolve([mat["id"], other["id"]], user_id=uid)
    check([r["id"] for r in ok2] == [mat["id"]] and bad2 == [other["id"]],
          "跨用户素材解析不到（取代裸路径可读一切）")
    check([r["id"] for r in await s.materials.search(uid, "海边")] == [mat["id"]],
          "2-gram 中文检索命中（原 rglob 扫盘的替身）")
    check(await s.materials.search("u-2", "海边") == [], "检索只见自己可见的")
    await araises(lambda: s.materials.register(
        uid, None, "users/u1/c1/mat-x.mp4", "重复.mp4", "video", bytes_=1, sha256="z"),
        IntegrityConflict, "object_key 唯一约束生效")
    await araises(lambda: s.materials.register(
        uid, None, "k2", "坏类型.mp4", "pdf", bytes_=1, sha256="z"),
        IntegrityConflict, "kind 受 CHECK 约束")

    art = s.artifacts("sess-1", "art-1")
    await art.put("parse_media", {"materials": [mat["id"]]})
    await art.put("plan_timeline_pro", {"clips": 3})
    check(await art.get("parse_media") == {"materials": [mat["id"]]}, "产物 payload 往返")
    check(await art.executed() == ["parse_media", "plan_timeline_pro"], "executed 按节点名有序")
    check((await art.meta("parse_media"))["artifact_id"] == "art-1", "meta 供拦截器校验归属")
    other_art = s.artifacts("sess-1", "art-2")
    check(await other_art.has("parse_media") is False, "产物按 artifact_id 隔离")
    await art.put("parse_media", {"materials": []})
    check(await art.get("parse_media") == {"materials": []}, "同名节点覆盖写（upsert）")

    job = await s.render_jobs.open("sess-1", "art-1")
    await s.render_jobs.progress("sess-1", "art-1", "render_video", 40)
    mid = await s.render_jobs.get("sess-1", "art-1")
    check((mid["stage"], mid["percent"]) == ("render_video", 40), "进度跨实例可见（取代探针文件）")
    await s.render_jobs.succeed("sess-1", "art-1", "renders/sess-1/art-1.mp4", 8.5)
    check(await s.render_jobs.ready("sess-1", "art-1"), "ready 需 done + 有成片 key")
    again = await s.render_jobs.open("sess-1", "art-1")
    check(again["id"] == job["id"] and again["status"] == "running"
          and await s.db.count("render_jobs") == 1, "同产物复跑复位原行，不插第二条")
    await s.render_jobs.progress("sess-1", "art-1", "render_video", 10)
    check(await s.render_jobs.reap_hanging(-1) == 1, "悬挂 running 超时置 failed")
    await s.render_jobs.fail("sess-1", "art-1", "素材缺失")
    check((await s.render_jobs.get("sess-1", "art-1"))["error"] == "素材缺失", "失败原因入库")

    ck = await s.checkpoints.save({"run_id": "r1", "session_id": "sess-1", "iteration": 1})
    await s.checkpoints.append_entry(
        "r1", kind="full", payload=[{"role": "user", "content": "hi"}],
        iteration=1, parent_seq=None)
    check("messages" not in await s.checkpoints.load("r1"),
          "指针行只答「到哪了」，正文不在这一行")
    check((await s.checkpoints.load_entries("r1"))[0]["payload"][0]["content"] == "hi",
          "一致点正文原样往返")
    check([c["run_id"] for c in await s.checkpoints.list_unfinished()] == ["r1"],
          "未完成 run 可枚举（崩溃恢复入口）")
    await s.checkpoints.save({**ck, "status": "completed"})
    check(await s.checkpoints.list_unfinished() == [], "完成后不再出现在恢复列表")

    await s.inbox.send(uid, "c1", "planner", {"text": "结果"}, sender="editor")
    await s.inbox.send(uid, "c1", "editor", {"text": "收到"})
    check(await s.inbox.agents(uid, "c1") == ["editor", "planner"], "agents 枚举有信的节点")
    unread = await s.inbox.read(uid, "c1", "planner")
    check(len(unread) == 1 and unread[0]["content"]["text"] == "结果", "read 返回未读")
    check(await s.inbox.peek(uid, "c1", "planner") == [], "read 之后 peek 为空（可审计，非删文件）")
    check(await s.inbox.purge_consumed(uid, "c1") == 1, "purge 只清已读")

    t1 = await s.tasks.create("sp", "拆解")
    t2 = await s.tasks.create("sp", "出片", blocked_by=[t1["id"]])
    check((t1["id"], t2["id"]) == (1, 2), "id 取自 task_seq")
    ready = [t["id"] for t in await s.tasks.ready("sp")]
    check(ready == [1], "依赖未完成的 2 号不在 ready 列表")
    check((await s.tasks.get(2))["blockedBy"] == [1] and (await s.tasks.get(1))["blocks"] == [2],
          "双向边同源于边表（取代读-改-写多文件）")
    await araises(lambda: s.tasks.create("sp", "悬空", blocked_by=[999]),
                  IntegrityConflict, "引用不存在的前置任务被拒（不留悬空边）")
    claimed = await s.tasks.claim("sp", "w1")
    check(claimed["id"] == 1 and claimed["owner"] == "w1"
          and await s.tasks.claim("sp", "w2") is None, "同一任务不会被两个 worker 领走")
    check((await s.tasks.get(2))["status"] == "pending", "被阻塞的下游不会被误领")
    await s.tasks.complete("sp", 1)
    check([t["id"] for t in await s.tasks.ready("sp")] == [2], "上游完成后下游进入 ready")

    await s.subagents.register("sp", "planner", "提示词")
    await s.subagents.set_status("sp", "planner", "working", lease_sec=-5)
    check(await s.subagents.reset_expired("sp") == 1, "租约到期的 working 归位 idle")
    check((await s.subagents.list("sp"))[0]["status"] == "idle", "重启后不再谎报忙")

    await s.jobs.upsert({"name": "heartbeat", "schedule": {"kind": "every"},
                         "state": {"next_run_at_ms": 100, "last_run_at_ms": None}})
    await s.jobs.upsert({"name": "daily", "schedule": {"kind": "at"},
                         "state": {"next_run_at_ms": 500, "last_run_at_ms": None}})
    due = await s.jobs.due(200)
    check([j["name"] for j in due] == ["heartbeat"], "due 只看到点的启用 job")
    won = await s.jobs.claim_run(due[0]["id"], due[0]["state"],
                                 new_state={"next_run_at_ms": 400, "last_run_at_ms": 200})
    check(won is not None and won["state"]["last_run_at_ms"] == 200,
          "比较-交换领取成功（state 整份比对）")
    loser = await s.jobs.claim_run(due[0]["id"], due[0]["state"],
                                   new_state={"next_run_at_ms": 400, "last_run_at_ms": 200})
    check(loser is None, "state 已被改动 → 第二个副本领不到（多实例不重复触发）")
    check(await s.jobs.due(200) == [], "领取时就把 next_run 推进，不再到期")
    await s.jobs.set_state(due[0]["id"], {"next_run_at_ms": 10, "last_run_at_ms": 200},
                           enabled=False)
    check(await s.jobs.due(200) == [], "禁用后不参与到期（enabled 只表达用户意图）")
    check(await s.jobs.drop_id(due[0]["id"]) == 1, "drop_id 删行")

    await s.memories.write(uid, "user", "偏好：短镜头")
    await s.memories.append(uid, "user", "追加：爱用空镜")
    check("短镜头" in (await s.memories.read(uid, "user")), "记忆读写按 (user,category)")
    check(set((await s.memories.entries(uid))) == {"user"}, "entries 只列自己的分类")
    await araises(lambda: s.memories.write(uid, "other", "x"), IntegrityConflict,
                  "category 受 CHECK 约束")

    await s.skills.upsert("storyline", "# 正文", description="剪辑",
                          frontmatter={"name": "storyline"},
                          files=[{"relpath": "scripts/a.py", "object_key": "skills/storyline/scripts/a.py"}])
    got = await s.skills.get("storyline")
    check(got["body"] == "# 正文" and got["files"][0]["relpath"] == "scripts/a.py",
          "技能正文入库、附件走对象存储 key")
    await s.skills.upsert("storyline", "# 新正文")
    check((await s.skills.get("storyline"))["files"] == [], "重存以最新附件清单为准")
    check([r["name"] for r in await s.skills.list()] == ["storyline"], "list 可枚举")
    check(await s.skills.drop("storyline") == 1 and await s.skills.get("storyline") is None,
          "drop 连带清关联表")

    await araises(lambda: s.db.insert("users", {"id": "u-x", "no_such_col": 1}),
                  ValueError, "未知列名直接拒绝（列名白名单）")
    await araises(lambda: s.db.insert("users", {"id": "u-x"}),
                  IntegrityConflict, "非空无默认列缺值拒绝")
    check((await s.db.select("users", where=[Cond("id", "eq", uid)]))[0]["id"] == uid,
          "Cond 列表写法可用")
    check(await s.db.update("users", {"token_hash": "z" * 64}, where={"id": "u-none"}) == [],
          "不存在的条件更新返回空列表")


async def test_workspace(tmp: Path) -> None:
    s = build_storage("memory", cache_root=tmp / "wc", workspace_root=tmp / "ws")
    await s.start()
    key = "users/u1/c1/mat-1.mp4"

    async def chunks():
        yield b"fake-video-bytes"

    await s.objects.put(key, chunks(), content_type="video/mp4")
    d = s.workspace.dir_for("sess", "art")
    src = {"id": "mat-1", "object_key": key, "filename": "海边日落.mp4"}
    local = await s.workspace.localize_material(src, d)
    check(local.parent == d and local.read_bytes() == b"fake-video-bytes",
          "localize 到会话工作区，节点拿到真路径")
    out = d / "final.mp4"
    out.write_bytes(b"mp4mp4")
    info = await s.workspace.publish(out, "renders/sess/art.mp4")
    check(info.bytes == 6 and (await s.objects.head("renders/sess/art.mp4")) is not None,
          "publish 把成片流回对象存储")
    n = s.workspace.cleanup("sess", "art")
    check(n == 2 and not d.exists(), f"cleanup 清掉中间产物（{n} 个文件）")
    check(s.workspace.dir_for("sess2", "") != s.workspace.dir_for("sess2", "a"),
          "dir_for 按产物分目录")
    weird = s.workspace.dir_for("../escape", "a/b")
    check(weird.resolve().is_relative_to(s.workspace.root.resolve()),
          "危险 id 被消毒，逃不出工作区根")


async def test_ingest_normalize(tmp: Path) -> None:
    """E：非常规编码入库自动归一化转 H.264；H.264 原件不转、探测失败不阻断。"""
    from agent_framework.ingest import ingest_bytes
    from agent_framework.storage.media import probe_sync

    s = build_storage("memory", cache_root=tmp / "nz_rc", workspace_root=tmp / "nz_rw")
    await s.start()
    await s.users.provision("u-nz")
    await s.conversations.ensure("u-nz", "c-nz")

    def make(src: Path, codec_args: list[str]) -> None:
        subprocess.run(["ffmpeg", "-hide_banner", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=1",
                        *codec_args, str(src)], check=True, capture_output=True)

    raw = tmp / "nz_raw"
    raw.mkdir(parents=True, exist_ok=True)
    mpeg4 = raw / "clip_mpeg4.mp4"
    make(mpeg4, ["-c:v", "mpeg4", "-q:v", "5"])
    check(probe_sync(mpeg4)["codec"] == "mpeg4", "构造出非常规编码测试片（mpeg4）")

    async def chunks(p: Path):
        yield p.read_bytes()

    out = await ingest_bytes(s, chunks(mpeg4), filename="clip_mpeg4.mp4",
                             user_id="u-nz", conversation_id="c-nz", origin="upload")
    check(out.get("normalized") is True and out.get("normalized_from") == "mpeg4",
          f"非常规编码入库自动归一化（{out.get('normalized_from')} → h264）")
    check(out["object_key"].endswith("-n.mp4") and out.get("original_object_key"),
          f"materials 指向归一化件、原件键可追溯（{out['object_key']}）")
    localized = await s.objects.localize(out["object_key"], tmp / "nz_dl")
    meta = probe_sync(localized)
    check(meta["codec"] == "h264" and meta["pix_fmt"] == "yuv420p",
          f"归一化产物为 H.264/yuv420p（{meta['codec']}/{meta['pix_fmt']}）")
    check(await s.objects.head(out["original_object_key"]) is not None,
          "原件字节仍在桶里")

    h264 = raw / "clip_h264.mp4"
    make(h264, ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"])
    out2 = await ingest_bytes(s, chunks(h264), filename="clip_h264.mp4",
                              user_id="u-nz", conversation_id="c-nz", origin="upload")
    check(not out2.get("normalized"), "H.264/yuv420p 原件入库不触发转码")

    async def fake_chunks():
        yield b"FAKE-VIDEO-BYTES"

    out3 = await ingest_bytes(s, fake_chunks(), filename="a.mp4",
                              user_id="u-nz", conversation_id="c-nz", origin="upload")
    check(not out3.get("normalized") and not out3.get("normalize_warning"),
          "探测失败的假字节按原样入库（不转码不告警）")
    await s.close()


async def main() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="storage_test_"))
    for title, fn, *args in [
        ("① build_storage 后端分派", test_factory, tmp),
        ("② 重依赖延迟 import", test_lazy_imports),
        ("③ schema.sql 静态检查", test_schema_static),
        ("④ ObjectStore 契约", test_object_store, tmp),
        ("⑤ Workspace 进出与回收", test_workspace, tmp),
        ("⑥ 入库归一化（E）", test_ingest_normalize, tmp),
    ]:
        print(f"\n[{title}]")
        await fn(*args)
    print("\n[⑦ 仓储契约（内存引擎）]")
    s = build_storage("memory", cache_root=tmp / "rc", workspace_root=tmp / "rw")
    await s.start()
    await test_repos(s)
    await s.close()

    print(f"\n{_checks - _fails}/{_checks} 通过")
    os.makedirs(".runtime", exist_ok=True)
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
