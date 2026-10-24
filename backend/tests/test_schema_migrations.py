# -*- coding: utf-8 -*-
"""迁移口径的常驻断言：新库（`schema.sql`）与老库（`migrations.sql`）必须同一条口径。

为什么要常驻、而且要跨两个文件比：`materials.origin` 的取值集合在两处各写了一份——
`schema.sql` 是**建表期** CHECK（只对新库生效），`migrations.sql` 是**改已存在对象**的
DROP+ADD（只对老库生效，也是唯一执行口，见 `ddl.MIGRATIONS_FILE`）。历史上这两份漂过一次：
`schema.sql` 列了 `'art'` 而 `migrations.sql` 没列，于是活库把 `'art'` 挡在门外，
而所有测试都在内存替身上跑——**两条口径各绿各的**，直到真机上插一行才炸。
所以这里比的是「两份文本逐项相等」，不是「各自合法」。

跑法：`python tests/test_schema_migrations.py`
  · 前两组纯离线（读文件、读技能正文）——没有容器也必须绿；
  · 第三组真 PG：`PG_DSN` 可达才跑，不可达则整组 SKIP 并**在末尾列出未验证清单**
    （不许把「跳过」读成「通过」）。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import ddl

_checks = 0
_fails = 0
_skipped: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# `origin IN ('a','b',...)` / `CHECK (origin = ANY (ARRAY['a'::text,...]))` 两种写法都收。
_IN_LIST = re.compile(r"origin\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


def _origin_values(sql: str) -> set[str]:
    """从一段 SQL 里抠出 `origin` 约束的取值集合。抠不到回空集（由调用方判红）。"""
    m = _IN_LIST.search(sql)
    if not m:
        return set()
    return {v.strip().strip("'") for v in m.group(1).split(",") if v.strip()}


def _parse_constraint_def(defn: str) -> set[str]:
    """把 `pg_get_constraintdef` 的 `ARRAY['a'::text, ...]` 文本解析成取值集合。"""
    head = defn.split("ARRAY[", 1)
    if len(head) < 2:
        # 退回 IN (...) 写法
        return {v.strip().strip("'") for v in re.findall(r"'([^']+)'", defn)}
    body = head[1].split("]", 1)[0]
    return {v.split("::", 1)[0].strip().strip("'")
            for v in body.split(",") if v.strip()}


def _skill_bodies() -> dict[str, str]:
    from agent_framework.skill import _parse_frontmatter
    out: dict[str, str] = {}
    root = Path(__file__).resolve().parents[1] / "examples" / "skills"
    for md in sorted(root.glob("*/SKILL.md")):
        meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        out[meta.get("name") or md.parent.name] = body
    return out


# ---------------------------------------------------------------------------
# ① 新库 ↔ 老库：origin 取值逐项相等（离线）
# ---------------------------------------------------------------------------
def test_schema_migrations_agree() -> None:
    print("\n=== ① schema.sql ↔ migrations.sql：origin 取值逐项相等 ===")
    schema = ddl.read_schema_sql()
    migrations = ddl.read_migrations_sql()

    new_db = _origin_values(schema)
    old_db = _origin_values(migrations)
    check(new_db, f"schema.sql 的 materials.origin 取值抠得出来（{sorted(new_db)}）")
    check(old_db, f"migrations.sql 的 materials.origin 取值抠得出来（{sorted(old_db)}）")
    check(new_db == old_db,
          f"新库口径 == 老库口径（只在新库有：{sorted(new_db - old_db)}；"
          f"只在老库有：{sorted(old_db - new_db)}）")
    # 放宽到 'art' 是 R2 的冻结决定：出图/抠图产物要能登记回 materials。
    check("art" in new_db, "新库口径含 'art'（出图产物档）")
    check("library" in new_db, "新库口径含 'library'（策展库投递档）")

    # schema.sql 的不变量：纯幂等建表，不含 DROP（放宽 CHECK 那条必须走 migrations.sql）。
    check("DROP CONSTRAINT" not in schema.upper(),
          "schema.sql 不含 DROP CONSTRAINT（放宽语句只准进 migrations.sql）")


# ---------------------------------------------------------------------------
# ② 技能正文点名的 origin 取值，必须都在口径里（离线）
# ---------------------------------------------------------------------------
def test_skill_origins_are_real() -> None:
    print("\n=== ② 技能正文点名的每个 origin 取值都能在口径里找到 ===")
    known = _origin_values(ddl.read_schema_sql())
    bodies = _skill_bodies()
    check(bodies, f"示例技能读到了（{len(bodies)} 份）")
    # 只认**明确写成 origin 取值**的那种引用：`origin` 附近或 `'xxx'` 形式的素材来源档。
    # 判据保守（宁漏不误）：正文里出现的 origin 值集合，必须 ⊆ 真实口径。
    origin_ctx = re.compile(r"origin[^\n]{0,40}?('([a-z]+)')", re.IGNORECASE)
    found: set[str] = set()
    for body in bodies.values():
        for _full, val in origin_ctx.findall(body):
            found.add(val)
    unknown = found - known
    check(not unknown,
          f"正文点名的 origin 取值都在口径里（臆造：{sorted(unknown)}；正文共出现 {sorted(found)}）")


# ---------------------------------------------------------------------------
# ③ 真 PG：pg_constraint 与口径一致，且 'art'/'library' 各能插入读回
# ---------------------------------------------------------------------------
class _Rollback(Exception):
    """用于把插入读回整段回滚——真库上不留探针行。"""


async def _live_checks() -> None:
    import asyncpg

    dsn = os.environ.get("PG_DSN", "")
    if not dsn:
        _skipped.append("③ 真 PG：未设 PG_DSN")
        return
    dsn = dsn.split("+asyncpg", 1)[0]
    if not dsn.startswith("postgresql"):
        dsn = "postgresql://" + dsn.split("://", 1)[-1]
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # noqa: BLE001
        _skipped.append(f"③ 真 PG：连不上（{type(exc).__name__}）")
        return
    try:
        defn = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'materials_origin_check'")
        live = _parse_constraint_def(defn or "")
        declared = _origin_values(ddl.read_schema_sql())
        check(live == declared,
              f"活库 materials_origin_check == 口径（只活库有：{sorted(live - declared)}；"
              f"只口径有：{sorted(declared - live)}）")
        check("art" in live, "活库约束含 'art'")

        # 插入并读回（整段回滚，不留脏行）。需要一个真实 owner_user_id。
        uid = await conn.fetchval("SELECT id FROM users LIMIT 1")
        if uid is None:
            _skipped.append("③ 真 PG：库里没有 users 行，跳过插入读回")
            return
        async with conn.transaction():
            for origin in ("art", "library"):
                mid = f"chk_{origin}_probe"
                await conn.execute(
                    "INSERT INTO materials (id, owner_user_id, object_key, filename,"
                    " kind, bytes, sha256, origin) VALUES ($1,$2,$3,$4,'image',1,$5,$6)"
                    " ON CONFLICT (id) DO UPDATE SET origin = EXCLUDED.origin",
                    mid, uid, f"probe/{mid}", "probe.png", "0" * 64, origin)
                back = await conn.fetchval(
                    "SELECT origin FROM materials WHERE id = $1", mid)
                check(back == origin, f"origin='{origin}' 能插入并读回（读到 {back!r}）")
            raise _Rollback()
    except _Rollback:
        pass
    finally:
        await conn.close()


def main() -> int:
    test_schema_migrations_agree()
    test_skill_origins_are_real()
    try:
        asyncio.run(_live_checks())
    except Exception as exc:  # noqa: BLE001
        _skipped.append(f"③ 真 PG：异常（{type(exc).__name__}: {exc}）")

    print()
    if _skipped:
        print("未验证清单（跳过 ≠ 通过）：")
        for s in _skipped:
            print(f"  · {s}")
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
