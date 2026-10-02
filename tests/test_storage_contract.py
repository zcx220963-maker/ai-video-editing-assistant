"""存储层双引擎契约测试：同一套用例分别跑内存替身与真 PG+MinIO。

运行：  python tests/test_storage_contract.py

内存侧必跑（离线）；pg_minio 侧在容器不可达 / SDK 未装时整组 SKIP 并在末尾打印
未验证清单——不允许「跳过」被读成「通过」。用例一律自带随机后缀 id，可在同一个
真库上反复跑而不与历史数据相撞。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import secrets
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.skill import SkillLoader
from agent_framework.storage import (
    IntegrityConflict,
    StorageUnavailable,
    build_storage,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"    {'✓' if cond else '✗'} {label}")


def suf() -> str:
    return secrets.token_hex(4)


async def _chunks(data: bytes):
    for i in range(0, len(data), 7):
        yield data[i:i + 7]


# --------------------------------------------------------------------------
# 用例：每个都是 (名字, 协程)，两引擎各跑一遍，断言必须完全一致
# --------------------------------------------------------------------------


async def case_identity(s, tmp: Path) -> None:
    uid, tok = await s.users.register("契约")
    check(await s.users.verify(tok) == uid, "token 反查 user_id")
    check(await s.users.verify("wrong") is None, "错 token 拒")
    row = await s.db.get_by_pk("users", {"id": uid})
    check(tok not in str(row), "库内只有 token 哈希")
    await s.users.register("契约")
    check(await s.users.verify(tok) == uid, "重复注册不影响旧 token")
    # B5-6：客户端自带 user_id 时代的 users.ensure 已删除，只剩启动期的内部登记
    check(not hasattr(s.users, "ensure"), "users.ensure 过渡位已删除")
    pid = "internal-" + suf()
    await s.users.provision(pid)
    await s.users.provision(pid)
    prow = await s.db.get_by_pk("users", {"id": pid})
    check(await s.db.count("users", where={"id": pid}) == 1, "provision 幂等（同 id 只一行）")
    check(prow is not None and len(prow["token_hash"]) == 64, "内部登记补的是 64 位哈希")
    check(await s.users.verify(prow["token_hash"]) is None,
          "内部身份可被外键引用但不等于可登录（占位哈希反查不到）")


async def case_conversation_isolation(s, tmp: Path) -> None:
    ua, _ = await s.users.register("a")
    ub, _ = await s.users.register("b")
    cid = "conv-" + suf()
    await s.conversations.ensure(ua, cid, "A 的会话")
    try:
        await s.conversations.ensure(ub, cid, "抢占")
        check(False, "他人会话 id 抢占应被拒")
    except IntegrityConflict:
        check(True, "他人会话 id 抢占被拒")
    m = await s.messages.append(ua, cid, "user", "只有 A 能写")
    check((await s.messages.history(ub, cid)) == [], "B 读不到 A 的历史")
    check((await s.messages.history(ua, cid))[0]["id"] == m["id"], "A 读得到自己的")


async def case_message_seq_and_json(s, tmp: Path) -> None:
    uid, _ = await s.users.register("u")
    cid = "conv-" + suf()
    await s.conversations.ensure(uid, cid)
    seqs = [(await s.messages.append(uid, cid, "user", f"第{i}条",
                                     attachments=[f"mat-{i}"])).get("seq")
            for i in range(3)]
    check(seqs == [1, 2, 3], f"seq 单调递增（实得 {seqs}）")
    rows = await s.messages.history(uid, cid)
    check([r["content"] for r in rows] == ["第0条", "第1条", "第2条"], "按 seq 升序返回")
    check(rows[1]["attachments"] == ["mat-1"], "jsonb 数组原样往返")
    await s.messages.set_qa(rows[2]["id"], {"question": "第2条",
                                            "answer": [{"type": "think", "text": "想"}]})
    got = await s.db.get_by_pk("messages", {"id": rows[2]["id"]})
    check(got["qa"]["answer"][0]["type"] == "think", "嵌套 qa 结构往返")


async def case_material_ownership(s, tmp: Path) -> None:
    uid, _ = await s.users.register("owner")
    other, _ = await s.users.register("other")
    key = f"users/{uid}/c1/mat-{suf()}.mp4"
    mat = await s.materials.register(uid, None, key, "海边日落 4K.mp4", "video",
                                     bytes_=1024, sha256="a" * 64, duration_sec=12.5,
                                     width=3840, height=2160, has_audio=True)
    check(mat["duration_sec"] == 12.5 and mat["width"] == 3840 and mat["has_audio"] is True,
          "ffprobe 元数据往返（double/int/bool）")
    byurl = await s.materials.register(
        uid, None, f"users/{uid}/c1/mat-{suf()}.mp4", "按链接取来的.mp4", "video",
        bytes_=2048, sha256="e" * 64, origin="url")
    check(byurl["origin"] == "url",
          "origin='url'（链接取料）过 CHECK —— 真引擎上即 migrations.sql 已生效")
    ok, bad = await s.materials.resolve([mat["id"]], user_id=other)
    check(ok == [] and bad == [mat["id"]], "越权 id 解析不到")
    ok, _ = await s.materials.resolve([mat["id"]], user_id=uid)
    check([r["id"] for r in ok] == [mat["id"]], "owner 解析得到")
    # 会话内共享的前提是「会话属于调用者」：光报对 conv_id 不构成权限
    cid2 = "conv-" + suf()
    await s.conversations.ensure(uid, cid2)
    shared = await s.materials.register(uid, cid2,
                                        f"users/{uid}/convs/{cid2}/mat-{suf()}.mp4",
                                        "共享素材.mp4", "video", bytes_=10, sha256="d" * 64)
    ok, _ = await s.materials.resolve([shared["id"]], user_id=uid, conv_id=cid2)
    check([r["id"] for r in ok] == [shared["id"]], "本人会话内的素材可解析")
    ok, bad = await s.materials.resolve([shared["id"]], user_id=other, conv_id=cid2)
    check(ok == [] and bad == [shared["id"]], "报上别人的 conv_id 也解析不到（会话归属校验）")
    try:
        await s.materials.register(other, None, key, "撞 key", "video",
                                   bytes_=1, sha256="b" * 64)
        check(False, "object_key 唯一约束应生效")
    except IntegrityConflict:
        check(True, "object_key 唯一约束生效")
    try:
        await s.materials.register(uid, None, f"k-{suf()}", "坏类型", "pdf",
                                   bytes_=1, sha256="c" * 64)
        check(False, "kind 枚举约束应生效")
    except IntegrityConflict:
        check(True, "kind 枚举约束生效")
    hit = await s.materials.search(uid, "海边日落")
    check([r["id"] for r in hit] == [mat["id"]], "中文 2-gram 检索命中")


async def case_artifact_scope(s, tmp: Path) -> None:
    sess = "sess-" + suf()
    a1, a2 = s.artifacts(sess, "art-1"), s.artifacts(sess, "art-2")
    await a1.put("parse_media", {"materials": 1})
    check(await a1.get("parse_media") == {"materials": 1}, "产物 payload 往返")
    check(await a2.get("parse_media", "缺省") == "缺省", "不同 artifact_id 互不可见")
    await a1.put("parse_media", {"materials": 2})
    check(await a1.get("parse_media") == {"materials": 2}, "同名节点覆盖写")
    check((await a1.meta("parse_media"))["node"] == "parse_media", "meta 带节点归属")
    other_sess = "sess-" + suf()
    check(await s.artifacts(other_sess, "art-1").get("parse_media", None) is None,
          "不同会话之间产物隔离")


async def case_render_job_lifecycle(s, tmp: Path) -> None:
    sess, art = "sess-" + suf(), "art-1"
    job = await s.render_jobs.open(sess, art)
    check(job["status"] == "running" and job["percent"] == 0, "open → running")
    await s.render_jobs.progress(sess, art, "render_video", 55)
    mid = await s.render_jobs.get(sess, art)
    check((mid["stage"], mid["percent"]) == ("render_video", 55), "进度写读一致")
    await s.render_jobs.succeed(sess, art, f"renders/{sess}/{art}.mp4", 9.5)
    check(await s.render_jobs.ready(sess, art), "done + 有 key → ready")
    again = await s.render_jobs.open(sess, art)
    check(again["id"] == job["id"], "同 (会话,产物) 复跑复用原行（UNIQUE 不炸）")
    check(await s.render_jobs.reap_hanging(-1) >= 1, "悬挂 running 被回收")
    check((await s.render_jobs.get(sess, art))["status"] == "failed", "回收后状态为 failed")


async def case_render_job_enqueue(s, tmp: Path) -> None:
    """「提交 + 轮询」的存储语义：queued 是真实状态，终态产物随 result 落库。"""
    sess, art = "sess-" + suf(), "art-enq"
    first = await s.render_jobs.enqueue(sess, art)
    check(isinstance(first, dict) and first["status"] == "queued",
          "enqueue 首提交 → queued（回单行 dict，不是 list）")
    await s.render_jobs.progress(sess, art, "encoding", 30)
    held = await s.render_jobs.enqueue(sess, art)
    check(held["id"] == first["id"] and held["percent"] == 30,
          "非终态重复 enqueue 原样返回，不复位正在跑的那次")
    await s.render_jobs.succeed(sess, art, f"renders/{sess}/{art}.mp4", 7.5,
                                {"video": f"renders/{sess}/{art}.mp4", "duration": 7.5,
                                 "width": 640, "height": 360, "title": "契约"})
    done = await s.render_jobs.get(sess, art)
    check((done["result"] or {}).get("width") == 640
          and float(done["duration_sec"]) == 7.5,
          f"succeed 把终态产物整份存下：{sorted((done['result'] or {}))}")
    reset = await s.render_jobs.enqueue(sess, art)
    check(isinstance(reset, dict) and reset["status"] == "queued"
          and not reset["result"] and not reset["video_object_key"],
          "终态再提交 → 复位成 queued 且清掉上一版产物（回读的也是复位后的行）")
    q_sess, q_art = "sess-" + suf(), "art-queued-only"
    await s.render_jobs.enqueue(q_sess, q_art)
    key = f"renders/{sess}/{art}.mp4"
    await s.objects.put(key, _chunks(b"view-bytes"), content_type="video/mp4")
    await s.render_jobs.succeed(sess, art, key, 7.5,
                                {"video": key, "duration": 7.5, "title": "视图"})
    view = await s.render_view(sess, art)
    check(view["status"] == "done" and view["title"] == "视图"
          and "://" in str(view["media_url"]),
          "render_view 在 done 时平铺 result 并现签 media_url")
    check(await s.render_view(sess, "noSuchArtifact") is None,
          "没提交过的作用域 render_view 回 None")
    await s.objects.delete(key)
    check(await s.render_jobs.reap_hanging(-1) >= 1, "queued 悬挂也回收")
    check((await s.render_jobs.get(q_sess, q_art))["status"] == "failed",
          "被收回的 queued 落 failed，轮询端不会空等")


async def case_claim_exclusive(s, tmp: Path) -> None:
    """SKIP LOCKED 语义的等价断言：同一行只能被领到一次。"""
    scope = "scope-" + suf()
    t1 = await s.tasks.create(scope, "上游")
    t2 = await s.tasks.create(scope, "下游", blocked_by=[t1["id"]])
    check([t["id"] for t in await s.tasks.ready(scope)] == [t1["id"]], "下游被阻塞不入 ready")
    first = await s.tasks.claim(scope, "w1")
    check(first is not None and first["id"] == t1["id"], "w1 领到上游")
    check(await s.tasks.claim(scope, "w2") is None, "w2 无未领任务可领")
    check((await s.tasks.get(t2["id"]))["status"] == "pending", "被阻塞的下游不被误领")
    await s.tasks.complete(scope, t1["id"])
    check([t["id"] for t in await s.tasks.ready(scope)] == [t2["id"]], "上游完成 → 下游 ready")
    check((await s.tasks.get(t1["id"]))["blocks"] == [t2["id"]], "反向边由边表推导")

    await s.jobs.upsert({"name": "job-" + suf(), "schedule": {"kind": "at", "at_ms": 10},
                         "state": {"next_run_at_ms": 10, "last_run_at_ms": None}})
    due = await s.jobs.due(20)
    check(len(due) == 1, "到点的 job 进入 due")
    won = await s.jobs.claim_run(due[0]["id"], due[0]["state"],
                                 new_state={"next_run_at_ms": 999, "last_run_at_ms": 20})
    check(won is not None, "state 比较-交换领取成功")
    check(await s.jobs.due(20) == [], "领取即推进 next_run，不再到期")
    check(await s.jobs.claim_run(due[0]["id"], due[0]["state"],
                                 new_state={"next_run_at_ms": 999, "last_run_at_ms": 20}) is None,
          "旧 state 再领一次领不到（jsonb 相等比较两引擎一致）")
    row = await s.jobs.get_by_name(due[0]["name"])
    check(row["state"] == {"next_run_at_ms": 999, "last_run_at_ms": 20} and row["enabled"],
          "state 整份覆盖，enabled 不受领取影响")
    check(await s.jobs.drop_id(row["id"]) == 1, "用例自建的 job 行用完即删（不留给线上服务解析）")


async def case_inbox_and_lease(s, tmp: Path) -> None:
    uid, _ = await s.users.register("u")
    cid = "conv-" + suf()
    await s.conversations.ensure(uid, cid)
    await s.inbox.send(uid, cid, "planner", {"text": "一"}, type_="result", sender="p1")
    await s.inbox.send(uid, cid, "planner", {"text": "二"})
    unread = await s.inbox.read(uid, cid, "planner")
    check([u["content"]["text"] for u in unread] == ["一", "二"], "read 按投递顺序返回未读")
    check(unread[0]["type"] == "result" and unread[0]["sender"] == "p1", "type/sender 往返")
    check(await s.inbox.peek(uid, cid, "planner") == [], "已读不再出现在 peek")
    check(await s.inbox.purge_consumed(uid, cid) == 2, "purge 清掉已读")

    scope = "scope-" + suf()
    await s.subagents.register(scope, "planner", "提示词")
    await s.subagents.set_status(scope, "planner", "working", lease_sec=-1)
    check(await s.subagents.reset_expired(scope) == 1, "过期租约被重置")
    check((await s.subagents.list(scope))[0]["status"] == "idle", "重置后状态为 idle")
    await s.subagents.set_status(scope, "planner", "working", lease_sec=600)
    check(await s.subagents.reset_expired(scope) == 0, "租约未到期不动")


async def case_checkpoint_and_memory(s, tmp: Path) -> None:
    run = "run-" + suf()
    await s.checkpoints.save({"run_id": run, "session_id": "sess-" + suf(),
                              "message": "把视频剪短", "iteration": 3})
    await s.checkpoints.append_entry(
        run, kind="full", payload=[{"role": "user", "content": "把视频剪短"}],
        iteration=3, parent_seq=None)
    got = await s.checkpoints.load(run)
    check(got["iteration"] == 3
          and (await s.checkpoints.load_entries(run))[0]["payload"][0]["role"] == "user",
          "checkpoint 往返：进度在指针行、正文在增量链")
    check(run in [c["run_id"] for c in await s.checkpoints.list_unfinished()], "未完成可枚举")
    await s.checkpoints.save({"run_id": run, "session_id": got["session_id"],
                              "status": "completed"})
    check(run not in [c["run_id"] for c in await s.checkpoints.list_unfinished()],
          "完成后从恢复列表消失")
    first = got
    row2 = await s.db.get_by_pk("checkpoints", {"run_id": run})
    check(row2["iteration"] == 0, "save 是全量快照：未传字段按默认覆盖")
    check(row2["created_at_ms"] == first["created_at_ms"], "upsert 保留首次创建时间")

    # ---- 分叉/执行清单面用到的每条查询都在真引擎上过一遍 ----
    for i in (1, 2, 3):                       # 再叠三条 delta，凑出一条能截断的链
        await s.checkpoints.append_entry(
            run, kind="delta", payload=[{"role": "tool", "name": f"t{i}", "content": "{}"}],
            iteration=i, parent_seq=i - 1)
    chain = await s.checkpoints.load_entries(run, upto_seq=2)
    check([e["seq"] for e in chain] == [0, 1, 2]
          and [e["parent_seq"] for e in chain[1:]] == [0, 1],
          f"load_entries 按 seq 升序且能在界处截断（时间旅行）：{[e['seq'] for e in chain]}")
    check((await s.checkpoints.load_entries(run))[-1]["kind"] == "delta",
          "同一条链上 full 与 delta 共存")

    sess = "sess-" + suf()
    parent = "run-" + suf()
    child = "run-" + suf()
    await s.checkpoints.save({"run_id": parent, "session_id": sess, "message": "出一条片子",
                              "status": "completed",
                              "scope": {"storyline_session": sess, "artifact_id": "art-a"}})
    await s.checkpoints.save({"run_id": child, "session_id": sess, "message": "换 BGM",
                              "head_seq": 1, "forked_from": parent, "forked_at_seq": 0,
                              "scope": {"storyline_session": sess, "artifact_id": "art-b"}})
    back = await s.checkpoints.load(child)
    check(back["scope"]["artifact_id"] == "art-b" and back["forked_from"] == parent
          and back["forked_at_seq"] == 0,
          "jsonb scope 与分叉来源原样往返")
    check({r["run_id"] for r in await s.checkpoints.list_by_session(sess)} == {parent, child},
          "list_by_session 列出本会话全部 run（含已完成的与分叉出来的）")
    check([r["run_id"] for r in await s.checkpoints.list_forks(parent)] == [child],
          "list_forks 走 forked_from 那条索引")
    check([r["run_id"] for r in await s.checkpoints.list_unfinished_by_session(sess)] == [child],
          "「继续」只挑 running 的那条，已完成的父 run 不算在途")
    await s.checkpoints.append_entry(child, kind="full", payload=[{"role": "user", "content": "x"}],
                                     iteration=0, parent_seq=None)
    n = await s.checkpoints.drop(child)
    check(n == 2 and await s.checkpoints.load(child) is None
          and await s.checkpoints.load_entries(child) == [],
          f"drop 连带删掉整条一致点链，不留孤儿行（实删 {n} 行）")

    uid, _ = await s.users.register("u")
    await s.memories.write(uid, "user", "偏好短镜头")
    await s.memories.append(uid, "user", "爱用空镜")
    check((await s.memories.read(uid, "user")) == "偏好短镜头\n爱用空镜", "记忆追加")
    check((await s.memories.read(uid, "tool")) == "", "未写过的分类读空串")

    # 并发追加：首建行 + 后续比较-交换，一条都不许丢（spec 缺陷清单「记忆 append 丢更新」）。
    # 12 路而不是 5 路：旧实现用 claim 做 CAS，而 PG 的 claim 是 SKIP LOCKED——
    # 「行正被别人锁住」也被算成一次失败，并发一高就把重试预算全白烧掉（真机复现：
    # 5 路里约 2/3 次直接抛 IntegrityConflict，12 路必炸）。
    results = await asyncio.gather(*(s.memories.append(uid, "tool", f"第{i}条")
                                     for i in range(12)), return_exceptions=True)
    errs = [r for r in results if isinstance(r, BaseException)]
    check(not errs, f"12 路并发追加没有一路抛错（抛了 {len(errs)} 路：{errs[:1]}）")
    tool_txt = await s.memories.read(uid, "tool")
    check(all(f"第{i}条" in tool_txt for i in range(12)), "12 路并发追加一条不丢")
    check(len(tool_txt.splitlines()) == 12, "并发追加没有重复也没有半条")

    check(await s.memories.drop_category(uid, "user") == 1
          and set(await s.memories.entries(uid)) == {"tool"}, "按分类删除")


async def case_skills_and_object_store(s, tmp: Path) -> None:
    name = "skill-" + suf()
    await s.skills.upsert(name, "# 正文", description="剪辑技能",
                          frontmatter={"name": name, "disabled": False},
                          files=[{"relpath": "scripts/a.py",
                                  "object_key": f"skills/{name}/scripts/a.py"}])
    got = await s.skills.get(name)
    check(got["description"] == "剪辑技能" and got["frontmatter"]["disabled"] is False,
          "技能正文与 frontmatter 往返")
    check([f["relpath"] for f in got["files"]] == ["scripts/a.py"], "附件清单在案")
    await s.skills.upsert(name, "# 新正文")
    check((await s.skills.get(name))["files"] == [], "重存后附件清单以最新为准")
    check(await s.skills.drop(name) == 1 and await s.skills.get(name) is None, "技能可删")

    # 技能库读路径（SkillLoader）：本地目录只当导入源，正文读表、附件读对象存储
    sk_name = "contract-skill-" + suf()
    src = tmp / "skills-in" / sk_name
    (src / "references").mkdir(parents=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {sk_name}\ndescription: 契约用例技能\nalways: true\n"
        "requires: ENV: PATH\n---\n\n正文：先读附件。", encoding="utf-8")
    (src / "references" / "note.md").write_text("附件正文", encoding="utf-8")
    loader = SkillLoader(s)
    check(await loader.sync_from_dir(src.parent) == [sk_name], "导入源目录 → 一个技能入库")
    sk = await loader.get(sk_name)
    check(sk.always and sk.available and "先读附件" in sk.body,
          "正文/always/requires 都从 skills 行读出（不扫盘）")
    check([f["relpath"] for f in sk.files] == ["references/note.md"], "附件行进 skill_files")
    check((await loader.read_attachment(sk, "references/note.md")).decode() == "附件正文",
          "附件字节经对象存储取回")
    check(sk_name in (await loader.attachment_url(sk, "references/note.md")),
          "非文本附件用的临时链接也按 object_key 签")
    check(sk_name in [x.name for x in await loader.discover()], "discover 从表里读到该技能")
    await s.skills.drop(sk_name)
    check(await loader.get(sk_name) is None, "删行后读路径立即看不到该技能")

    key = f"users/u1/conv/{'m' + suf()}.mp4"
    payload = bytes(range(64)) * 3
    info = await s.objects.put(key, _chunks(payload), content_type="video/mp4")
    check(info.bytes == len(payload), "put 返回字节数")
    head = await s.objects.head(key)
    check(head is not None and head.bytes == len(payload), "head 命中")
    check(head.sha256 == info.sha256 and len(head.sha256) == 64,
          "head.sha256 与 put 一致（内容指纹，不用 etag）")
    got_bytes = await _collect(s.objects.get_stream(key))
    check(got_bytes == payload, "get_stream 全量字节一致")
    tail = await _collect(s.objects.get_stream(key, start=10, end=20))
    check(tail == payload[10:21], "区间读闭区间语义")
    url = await s.objects.presign_get(key, ttl_sec=120)
    check(isinstance(url, str) and key in url, "presign 返回含 key 的 URL")
    local = await s.workspace.localize(key, tmp / "loc")
    check(local.read_bytes() == payload, "localize 落盘字节一致（ffprobe 可用）")
    again = await s.workspace.localize(key, tmp / "loc2")
    check(again.read_bytes() == payload, "二次 localize 仍可取（内容寻址缓存）")
    await s.objects.delete(key)
    check(await s.objects.head(key) is None, "delete 之后 head 为空")
    try:
        await _collect(s.objects.get_stream(key))
        check(False, "读已删对象应抛 StorageUnavailable")
    except StorageUnavailable:
        check(True, "读已删对象抛 StorageUnavailable")


async def case_object_list_prefix(s, tmp: Path) -> None:
    """按前缀问出「桶里已有哪几片」——分片续传的立足点，两个引擎语义必须一致。"""
    owner = f"uploads/u-lp-{suf()}/convs/c/"
    want = {f"{owner}part_{i:05d}": bytes([i]) * (1024 + i) for i in range(3)}
    want[f"{owner}part_00003"] = "末片可以短于其他片".encode("utf-8")
    for k in sorted(want):
        await s.objects.put(k, _chunks(want[k]), content_type="application/octet-stream")
    noise = f"uploads/u-lp-noise-{suf()}/x"
    await s.objects.put(noise, _chunks("不在该前缀下".encode("utf-8")))

    got = await s.objects.list_prefix(owner)
    check([i.key for i in got] == sorted(want),
          f"只回该前缀下的键且按键升序（实得 {[i.key.rsplit('/', 1)[-1] for i in got]}）")
    check({i.key: i.bytes for i in got} == {k: len(v) for k, v in want.items()},
          "每条的字节数与写入一致（续传据字节数判断哪些片可跳过）")
    head_sha = (await s.objects.head(owner + "part_00000")).sha256
    listed = next(i.sha256 for i in got if i.key == owner + "part_00000")
    check(len(listed) in (0, 64) and listed in ("", head_sha),
          f"列举带回的指纹要么完整且与 head 一致、要么为空（实得 {listed[:12] or '空'}）")
    check(await s.objects.list_prefix(f"nope-{suf()}/") == [], "无命中的前缀回空列表")
    for k in want:
        await s.objects.delete(k)
    check(await s.objects.list_prefix(owner) == [], "删净后该前缀不再有条目")
    await s.objects.delete(noise)


async def _collect(ait) -> bytes:
    return b"".join([c async for c in ait])


CASES = [
    ("身份与 token", case_identity),
    ("会话归属隔离", case_conversation_isolation),
    ("消息 seq 与 jsonb", case_message_seq_and_json),
    ("素材归属与检索", case_material_ownership),
    ("产物作用域", case_artifact_scope),
    ("渲染任务生命周期", case_render_job_lifecycle),
    ("渲染任务提交与轮询", case_render_job_enqueue),
    ("原子领取互斥", case_claim_exclusive),
    ("收件箱与租约", case_inbox_and_lease),
    ("checkpoint 与记忆", case_checkpoint_and_memory),
    ("技能与对象存储", case_skills_and_object_store),
    ("对象前缀列举", case_object_list_prefix),
]


async def run_suite(title: str, s) -> None:
    tmp = Path(tempfile.mkdtemp(prefix=f"contract_{title}_"))
    print(f"\n[{title}]")
    for name, fn in CASES:
        print(f"  · {name}")
        await fn(s, tmp)


async def make_pg():
    try:
        s = build_storage("pg_minio")
    except Exception as e:            # noqa: BLE001  未装 SDK / 缺环境变量
        return None, f"构造失败：{type(e).__name__}: {e}"
    try:
        await s.start()
    except Exception as e:            # noqa: BLE001  容器不可达
        return None, f"连接失败：{type(e).__name__}: {e}"
    return s, ""


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="contract_mem_"))
    mem = build_storage("memory", cache_root=tmp / "cache", workspace_root=tmp / "ws")
    await mem.start()
    await run_suite("memory 替身", mem)
    await mem.close()

    pg, why = await make_pg()
    skipped = [name for name, _ in CASES]
    if pg is not None:
        await run_suite("pg_minio 真引擎", pg)
        await pg.close()
        skipped = []
    else:
        print(f"\n[pg_minio 真引擎] SKIP —— {why}")
        print("  （docker compose up -d 且装好 asyncpg/minio 后重跑本文件即可验证）")

    print("\n未验证清单：" + ("无——两引擎都跑过了" if not skipped else f"{len(skipped)} 组仅在内存替身上验证"))
    for name in skipped:
        print(f"  - {name}")
    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
