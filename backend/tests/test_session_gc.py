"""成片入库 + 按会话级联回收的离线验证。

覆盖三件事（都是这次改造的硬约束，缺一条就是嘴上说了没做了）：
1. **成片入库**：渲染终态把那一版字节登记进 ``materials``（``origin='render'``），
   同一对象键重烧只更新一行；入库失败只回一句说明，不新增素材行、不硬造归属。
2. **级联回收**：删对话连带清掉该会话名下的产物 / 渲染任务 / run 与一致点 / token 流水 /
   协作状态，以及桶里的会话前缀字节；**素材库登记过的字节一个都不许动**。
3. **开机对账**：库里残留的孤儿会话（含 ``{user}:{conv}`` 与 ``u:{user}:c:{conv}`` 两种
   拼法）与「只有字节、没有行」的崩溃残留都能被扫到并回收，在册对话不受影响。
4. **`assets/` 不在回收网里**（T14）：删会话与开机对账都不许碰内置素材库命名空间下的
   一个字节，包括**没人指向**的那些（引用计数为 0 ≠ 可删，那是给人判读的）。

离线纪律：只用 ``build_storage("memory")``，不连 PG、不连 MinIO。
``materials.origin`` 的 CHECK 放开到含 'render' 只在 PG 侧才真的验得到——本文件验不了它，
真机那一步（README §6 记录）才算这条数。

运行：  python tests/test_session_gc.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import session_gc
from agent_framework.identity import (parse_session, session_forms,
                                      storyline_session_id)
from agent_framework.render_library import register_render
from agent_framework.storage import Cond, build_storage

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


async def put(st, key: str, body: bytes = b"0123456789") -> None:
    """往对象存储塞一个对象（MemoryObjectStore 要的是异步块流）。"""
    async def gen():
        yield body
    await st.objects.put(key, gen(), content_type="video/mp4")


async def keys(st, prefix: str) -> list[str]:
    return [i.key for i in await st.objects.list_prefix(prefix)]


def sess(user: str, conv: str) -> tuple[str, str]:
    """(剪辑侧作用域键, Agent 侧作用域键)——同一个会话的两副面孔，回收必须两副都清。"""
    return storyline_session_id(user, conv), f"{user}:{conv}"


def scope_of(sid: str) -> str:
    from agent_framework.orchestration import _safe
    return _safe(sid)


async def new_storage(tmp: Path, name: str):
    st = build_storage("memory", cache_root=tmp / name / "cache",
                       workspace_root=tmp / name / "ws")
    await st.start()
    return st


# --------------------------------------------------------------------------
# ① 作用域拼法
# --------------------------------------------------------------------------

async def case_forms() -> None:
    s1, p1 = sess("u-1", "c-1")
    check(session_forms(s1) == sorted([p1, s1]),
          "剪辑侧键能展开成两种拼法")
    check(session_forms(p1) == session_forms(s1),
          "Agent 侧键展开结果与剪辑侧键一致（删一侧等于删两侧）")
    check(parse_session(s1) == ("u-1", "c-1") and parse_session(p1) == ("u-1", "c-1"),
          "两种拼法都能拆回 (用户, 对话)")
    check(parse_session("sess-9") == ("", ""),
          "离线会话键拆不出归属，回空而不是硬猜")
    check(session_forms("sess-9") == ["sess-9"],
          "拆不出归属时原样回一条，不编一个作用域")


# --------------------------------------------------------------------------
# ② 成片入库
# --------------------------------------------------------------------------

async def case_register(tmp: Path) -> None:
    st = await new_storage(tmp, "register")
    try:
        await st.users.provision("u-1")
        await st.conversations.ensure("u-1", "c-1", "中子星")
        s1, _ = sess("u-1", "c-1")
        key = f"renders/{scope_of(s1)}/a1.mp4"
        await put(st, key, b"film-bytes")

        mid, note = await register_render(st, s1, key, title="中子星：一茶匙",
                                          duration_sec=42.5)
        row = (await st.db.select("materials", where={"id": mid})) [0] if mid else {}
        check(mid != "" and note == "", "渲染终态登记成功并回 material_id")
        check(row.get("origin") == "render", "素材行标成 render（与上传素材同表不同源）")
        check(row.get("filename") == "中子星：一茶匙.mp4", "展示名用这次的标题")
        check(row.get("object_key") == key and row.get("bytes") == len(b"film-bytes"),
              "字节数取自对象本身，不是渲染方自报")
        check(bool(row.get("sha256")), "指纹随头信息回来并落库")
        check(row.get("conv_id") == "c-1" and row.get("owner_user_id") == "u-1",
              "归属同时记用户与会话——对话删了仍归用户")

        mid2, note2 = await register_render(st, s1, key, title="改名再来一版",
                                            duration_sec=44.0)
        rows = await st.db.select("materials", where={"object_key": key})
        check(mid2 == mid and len(rows) == 1, "同一对象键重烧只更新那一行，不插第二条")
        check(rows[0]["filename"] == "改名再来一版.mp4"
              and rows[0]["duration_sec"] == 44.0, "重烧把展示名与时长刷新")

        bad_key = f"renders/{scope_of(s1)}/missing.mp4"
        mid3, note3 = await register_render(st, s1, bad_key, title="不存在的片子")
        check(mid3 == "" and note3 != "",
              "对象不在桶里：不登记、回一句说明（而不是静默）")
        check(await st.db.count("materials") == 1, "说明那条没留下半行脏账")

        mid4, note4 = await register_render(st, "sess-offline", key, title="离线直调")
        check(mid4 == "" and "拆不出用户" in note4,
              "离线会话键不硬造 owner，如实回说明")

        mid5, _note5 = await register_render(st, s1, key, title='恶意/名称:*?"<>|换行\n')
        row5 = (await st.db.select("materials", where={"id": mid5}))[0]
        check(mid5 == mid, "净化标题后仍落在同一行（没插第二条）")
        check(not any(ch in row5["filename"] for ch in '/\\:*?"<>|\n'),
              f"展示名里的路径分隔符被换掉：{row5['filename']!r}")
        check(len(row5["filename"]) <= 84, "展示名截断在合理长度内（80 + .mp4）")
    finally:
        await st.close()


# --------------------------------------------------------------------------
# ③ 级联回收（含保护名单与在途拒绝）
# --------------------------------------------------------------------------

async def _seed(tmp: Path, name: str):
    """造两个会话的完整现场：各自的行、各自的字节，外加一份素材与一份成片。"""
    st = await new_storage(tmp, name)
    await st.users.provision("u-1")
    await st.conversations.ensure("u-1", "c-1", "第一册")
    await st.conversations.ensure("u-1", "c-2", "第二册")
    s1, p1 = sess("u-1", "c-1")
    s2, p2 = sess("u-1", "c-2")
    k1, k2 = scope_of(s1), scope_of(s2)

    await st.db.insert("artifacts", {"session_id": s1, "node": "render_video",
                                     "artifact_id": "a1", "payload": {}})
    await st.db.insert("render_jobs", {"id": "rj-1", "session_id": s1,
                                       "artifact_id": "a1", "status": "done"})
    await st.db.insert("checkpoints", {"run_id": "run-1", "session_id": p1,
                                       "created_at_ms": 1, "updated_at_ms": 1})
    await st.db.insert("checkpoint_entries", {"run_id": "run-1", "seq": 1,
                                              "created_at_ms": 1, "payload": {}})
    await st.db.insert("token_usage", {"user_id": "u-1", "session_id": p1,
                                       "run_id": "run-1"})
    await st.db.insert("tasks", {"id": 1, "scope": p1, "name": "切镜"})
    await st.db.insert("tasks", {"id": 2, "scope": p1, "name": "出片"})
    await st.db.insert("task_edges", {"task_id": 2, "depends_on": 1})
    await st.db.insert("subagents", {"scope": p1, "name": "planner"})
    # 第二册：同一用户的另一个会话，一条都不能被第一次回收波及
    await st.db.insert("render_jobs", {"id": "rj-2", "session_id": s2,
                                       "artifact_id": "a2", "status": "done"})
    await st.db.insert("checkpoints", {"run_id": "run-2", "session_id": p2,
                                       "created_at_ms": 1, "updated_at_ms": 1})

    for key in (f"renders/{k1}/a1.mp4", f"renders/{k1}/a1/hitmap.json",
                f"renders/{k1}/frames/1.png", f"derived/{k1}/a1/asr.json",
                f"motion-shots/{k1}/h1/clip.mp4", f"render-windows/{k1}/w1/clip.mp4",
                "uploads/u-1/convs/c-1/up-1/part_00000",
                "users/u-1/convs/c-1/mat-1.mp4",
                f"renders/{k2}/a2.mp4",
                # 「字节落了桶、行却没落」的崩溃残留：作用域不属于任何对话
                "renders/other/a.mp4"):
        await put(st, key)
    return st, (s1, p1, s2, p2, k1, k2)


async def case_purge(tmp: Path) -> None:
    st, (s1, p1, s2, p2, k1, k2) = await _seed(tmp, "purge")
    try:
        film_key = f"renders/{k1}/a1.mp4"
        await register_render(st, s1, film_key, title="第一册成片", duration_sec=30.0)

        out = await session_gc.purge_conversation(st, "u-1", "c-1")
        check(not out.get("skipped"), "无在途渲染时正常回收")
        for table in session_gc.SESSION_TABLES:
            left = await st.db.count(table, where=[Cond("session_id", "in", [s1, p1])])
            check(left == 0, f"{table} 两种拼法的行都清空")
        check(await st.db.count("checkpoint_entries") == 0, "一致点随所属 run 一起清")
        check(await st.db.count("task_edges") == 0, "任务依赖边不留悬空")
        check(await st.db.count("tasks") == 0 and await st.db.count("subagents") == 0,
              "协作状态（任务板 + 子 Agent）随会话清空")

        check(await st.db.count("render_jobs", where={"session_id": s2}) == 1
              and await st.db.count("checkpoints", where={"session_id": p2}) == 1,
              "同一用户的另一个会话一行都没被波及")
        check(await keys(st, f"renders/{k2}/") == [f"renders/{k2}/a2.mp4"],
              "另一个会话的字节没被误删")

        check(await st.objects.head(film_key) is not None,
              "已入库的成片字节仍在（回收不许动用户已有的东西）")
        check(out["kept"] >= 1, f"账上记了护住的字节数（kept={out['kept']}）")
        for gone in (f"renders/{k1}/a1/hitmap.json", f"renders/{k1}/frames/1.png",
                     f"derived/{k1}/a1/asr.json", f"motion-shots/{k1}/h1/clip.mp4",
                     f"render-windows/{k1}/w1/clip.mp4",
                     "uploads/u-1/convs/c-1/up-1/part_00000"):
            check(await st.objects.head(gone) is None, f"会话工作区字节已回收：{gone}")
        check(await st.objects.head("users/u-1/convs/c-1/mat-1.mp4") is not None,
              "用户上传素材的字节前缀根本不在回收范围内")
        check(await st.db.count("materials") == 1
              and (await st.db.select("materials"))[0]["conv_id"] == "c-1",
              "素材行不由回收负责（对话行还在时归属也还在）")

        other = await session_gc.purge_session(st, "sess-1")
        check(await st.objects.head("renders/other/a.mp4") is not None,
              "按前缀回收不会把 sess-1 误配到 sess-12 这类近邻作用域")
        check(other["objects"] == 0, "查无字节时账上是 0，不是失败")
    finally:
        await st.close()


async def case_busy(tmp: Path) -> None:
    st, (s1, _p1, _s2, _p2, k1, _k2) = await _seed(tmp, "busy")
    try:
        await st.db.update("render_jobs", {"status": "running"},
                           where={"id": "rj-1"})
        out = await session_gc.purge_conversation(st, "u-1", "c-1")
        check(bool(out.get("skipped")), "渲染在途时拒绝回收并说明原因")
        check(await st.db.count("render_jobs", where={"session_id": s1}) == 1,
              "拒绝时一行都没删")
        check(await keys(st, f"renders/{k1}/") != [], "拒绝时一个字节都没删")
        # 收口之后（终态行）下一次回收就成立——开机对账等的就是这一刻
        await st.db.update("render_jobs", {"status": "done"}, where={"id": "rj-1"})
        out2 = await session_gc.purge_conversation(st, "u-1", "c-1")
        check(not out2.get("skipped") and await st.db.count("render_jobs") == 1,
              "渲染落终态后回收补上（另一会话的行仍在）")
        check(await keys(st, f"renders/{k1}/") == [], "补收把这版工作区字节也带走了")
    finally:
        await st.close()


# --------------------------------------------------------------------------
# ④ 开机对账
# --------------------------------------------------------------------------

async def case_sweep(tmp: Path) -> None:
    st, (s1, p1, s2, p2, k1, k2) = await _seed(tmp, "sweep")
    try:
        check(await session_gc.orphan_sessions(st) == [], "在册对话不留孤儿")
        check(await session_gc.orphan_scopes(st) == ["other"],
              "桶里唯一不属于任何对话的作用域被抓到")

        await register_render(st, s1, f"renders/{k1}/a1.mp4", title="第一册成片",
                              duration_sec=30.0)
        await st.conversations.drop("u-1", "c-1")       # 只删对话行 = 崩溃现场
        orphans = await session_gc.orphan_sessions(st)
        check(sorted(orphans) == sorted([s1, p1]),
              "删掉对话行后，两种拼法的残留都被列为孤儿")

        ledger = await session_gc.sweep_orphans(st)
        scopes = {e.get("session_id") or e.get("scope") for e in ledger}
        kept_total = sum(int(e.get("kept", 0)) for e in ledger)
        check(await st.db.count("render_jobs", where={"session_id": s1}) == 0
              and await st.db.count("checkpoints", where={"session_id": p1}) == 0,
              "对账把孤儿会话的行清干净")
        check(await st.objects.head(f"renders/{k1}/a1.mp4") is not None,
              "对账同样不碰素材库登记过的字节")
        sess_entries = [e for e in ledger if e.get("session_ids")]
        check(len(sess_entries) == 1 and kept_total == 1,
              f"同一会话的两副面孔只回收一次、护住的字节只计一次"
              f"（会话条目 {len(sess_entries)} 条 / kept={kept_total}）")
        check(await st.objects.head("renders/other/a.mp4") is None,
              "只有字节、没有行的崩溃残留也被扫掉")
        check("other" in scopes, "纯字节作用域的账单独成一条（不是被会话回收顺带删的）")
        check(await st.db.count("render_jobs", where={"session_id": s2}) == 1,
              "在册会话的行与字节照旧")
        check(await session_gc.sweep_orphans(st) == [],
              "第二次扫不到东西（幂等，不留反复回收的尾巴）")
    finally:
        await st.close()


# --------------------------------------------------------------------------
# ⑤ assets/ 命名空间不在回收网里（T14）
# --------------------------------------------------------------------------

async def case_asset_namespace(tmp: Path) -> None:
    """删会话 / 开机对账都**不许碰 `assets/` 下的一个字节**。

    判据（T14）：「删会话后 assets/ 对象仍在」——防的是将来有人顺手把 `assets/`
    加进 `OBJECT_PREFIXES`。光断言常量里没有 `assets/` 不够：真正会伤人的是**行为**，
    所以这里把两种删法都跑一遍，再断言库素材一个不少。

    为什么要连**没人指向的**那条也断言：`assets/` 下的对象本来就允许「引用计数为 0」
    （入库半途崩了、或行被删了），巡检脚本 `scripts/asset_gc_report.py` 只把它列给
    人看。回收若按「没人指向 = 可删」来判，就会把这类待人工判读的对象直接抹掉。
    """
    st = await new_storage(tmp, "assets-ns")
    s1, p1 = sess("u-1", "c-1")
    k1 = scope_of(s1)
    try:
        await st.users.provision("u-1")
        await st.conversations.ensure("u-1", "c-1", "第一册")

        # 库素材两条：一条有 asset_library 行（在册），一条没有（引用计数为 0）。
        # 名字故意跨两个「目录」——回收是按前缀扫的，`assets/iconify/` 这种二级段
        # 正是 orphan_scopes 会拆成作用域去比对的那一格。
        await st.assets.add(sha256="a" * 64, bytes_=128, mime="image/svg+xml", ext="svg",
                            object_key="assets/iconify/mdi/whale.svg",
                            source="iconify", license="Apache-2.0")
        for key in ("assets/iconify/mdi/whale.svg", "assets/_stale/nobody.png"):
            await put(st, key)
        # 会话侧的字节混在一起，用来证明这一轮回收**真的在删东西**
        for key in (f"renders/{k1}/a1.mp4", f"derived/{k1}/a1/asr.json"):
            await put(st, key)

        # 静态那一半：常量里不许出现 assets（含 `assets`、`assets/iconify/` 这类变体），
        # 以及**任何会话作用域**拼出来的回收前缀都不落在 assets 命名空间。
        prefixes = tuple(session_gc.OBJECT_PREFIXES)
        check(not any(p.startswith("assets") for p in prefixes),
              f"OBJECT_PREFIXES 里没有任何 assets* 前缀（实得 {prefixes}）")
        scoped = session_gc._scope_prefixes(scope_of(s1))
        check(not any(p.startswith("assets") for p in scoped),
              f"会话作用域拼出来的回收前缀不落在 assets/（实得 {scoped}）")

        out = await session_gc.purge_conversation(st, "u-1", "c-1")
        check(out["objects"] == 2,
              f"这一轮确实删掉了会话工作区的 {out['objects']} 个字节（回收真的在工作）")
        check(await st.objects.head(f"renders/{k1}/a1.mp4") is None
              and await st.objects.head(f"derived/{k1}/a1/asr.json") is None,
              "会话工作区字节已回收")
        check(await st.objects.head("assets/iconify/mdi/whale.svg") is not None,
              "删会话后 assets/ 下**在册**的库素材仍在")
        check(await st.objects.head("assets/_stale/nobody.png") is not None,
              "删会话后 assets/ 下**没人指向**的库素材也仍在"
              "（引用计数为 0 不等于可删，那是给人判读的）")

        # 开机对账那一侧：先让会话成孤儿，再扫。`assets/_stale/` 若被当成作用域，
        # 这一步就会把它整目录删掉——所以这条断言是这块的真正红线。
        await st.conversations.drop("u-1", "c-1")
        check(await session_gc.orphan_scopes(st) == [],
              "assets/ 下的对象不会被拆成「孤儿作用域」（实得 "
              f"{await session_gc.orphan_scopes(st)}）")
        await session_gc.sweep_orphans(st)
        check(await st.objects.head("assets/iconify/mdi/whale.svg") is not None
              and await st.objects.head("assets/_stale/nobody.png") is not None,
              "开机对账后 assets/ 下两条都还在")
        check(await st.db.count("asset_library") == 1,
              "素材库的行也不由回收负责")
    finally:
        await st.close()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="session_gc_"))
    print("\n[作用域拼法]")
    await case_forms()
    print("\n[成片入库]")
    await case_register(tmp)
    print("\n[级联回收]")
    await case_purge(tmp)
    print("\n[渲染在途]")
    await case_busy(tmp)
    print("\n[开机对账]")
    await case_sweep(tmp)
    print("\n[assets/ 不在回收网里]")
    await case_asset_namespace(tmp)

    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
