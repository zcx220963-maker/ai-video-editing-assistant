"""结构化状态的数据访问层：一套接口，两个引擎。

  - PgDatastore     ：SQLAlchemy 2.0 async + asyncpg，线上用。
  - MemoryDatastore ：进程内替身，复刻主键/非空/唯一/默认值/序列/原子领取语义，
                      供离线单测与本地最小闭环（定位同 InMemoryMessageQueue）。

分层：基类 `Datastore` 负责表元数据、列名白名单校验、非空与默认值填充、jsonb 编解码、
条件归一；`_do_*` 原语由两个引擎各自实现——PG 拼参数化 SQL，内存引擎在 Python 里比较。
同一份校验在两个引擎上一致生效，离线测试能提前抓出 PG 会拒绝的行。

调用方永远不接触 SQL：表名/列名来自 schema.sql 解析出的白名单并统一过 safe_ident()，
值一律走绑定参数，没有注入面。
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .ddl import (NOW, Column, TableMeta, load_schema, read_migrations_sql,
                  safe_ident, split_statements)


class StorageUnavailable(RuntimeError):
    """存储层不可达（PG 连不上 / MinIO 读不到）。写路径不降级，直接抛。"""


class IntegrityConflict(ValueError):
    """主键或唯一约束冲突。两个引擎抛同一类型，调用方无需区分后端。"""


# --------------------------------------------------------------------------
# 条件 DSL
# --------------------------------------------------------------------------

_OPS = {"eq", "ne", "lt", "le", "gt", "ge", "in", "is_null", "not_null"}
_LET = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
        "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b}
_SQL_OP = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


@dataclass(frozen=True)
class Cond:
    column: str
    op: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"未知条件算子 {self.op!r}，可选：{sorted(_OPS)}")


def eq(v: Any) -> Cond: return Cond("", "eq", v)
def ne(v: Any) -> Cond: return Cond("", "ne", v)
def lt(v: Any) -> Cond: return Cond("", "lt", v)
def le(v: Any) -> Cond: return Cond("", "le", v)
def gt(v: Any) -> Cond: return Cond("", "gt", v)
def ge(v: Any) -> Cond: return Cond("", "ge", v)
def in_(values: Iterable[Any]) -> Cond: return Cond("", "in", list(values))
def is_null() -> Cond: return Cond("", "is_null", None)
def not_null() -> Cond: return Cond("", "not_null", None)


Where = Mapping[str, Any] | Sequence[Cond] | None
OrderBy = Sequence[str] | None


def normalize_where(where: Where) -> list[Cond]:
    """`{"col": v}` 与 `[eq(Cond)...]` 两种写法归一成一串带列名的 Cond。"""
    out: list[Cond] = []
    if not where:
        return out
    pairs: list[tuple[str | None, Any]]
    pairs = list(where.items()) if isinstance(where, Mapping) else [(None, c) for c in where]
    for col, cond in pairs:
        if isinstance(cond, Cond):
            out.append(cond if cond.column else Cond(col or "", cond.op, cond.value))
        elif isinstance(cond, (list, tuple)):
            raise ValueError(f"列 {col} 的条件给了裸列表：多值请用 in_([...])")
        else:
            out.append(Cond(col or "", "eq", cond))
    for c in out:
        if not c.column:
            raise ValueError("条件缺少列名")
    return out


def match(row: Mapping[str, Any], conds: Sequence[Cond]) -> bool:
    """内存引擎侧的条件求值（语义与 PG 一致：NULL 参与比较一律不命中）。"""
    for c in conds:
        v = row.get(c.column)
        if c.op == "is_null":
            ok = v is None
        elif c.op == "not_null":
            ok = v is not None
        elif v is None:
            ok = False
        elif c.op == "in":
            ok = any(v == x for x in c.value)
        else:
            try:
                ok = bool(_LET[c.op](v, c.value))
            except TypeError:
                ok = False
        if not ok:
            return False
    return True


# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------


class Datastore(ABC):
    """表级数据访问接口。子类实现 `_do_*` 原语，公开方法在此组装。"""

    def __init__(self) -> None:
        self.tables, self.sequences, self.ddl = load_schema()

    # ---- 生命周期 ----

    @abstractmethod
    async def ping(self) -> None:
        """一次最小往返；连不上抛 StorageUnavailable。装配阶段调用，不带病服务。"""

    @abstractmethod
    async def ensure_schema(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ---- 引擎原语 ----

    @abstractmethod
    async def _do_insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def _do_select(self, table: str, conds: list[Cond], order_by: OrderBy,
                         limit: int | None) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def _do_update(self, table: str, conds: list[Cond],
                         values: dict[str, Any]) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def _do_delete(self, table: str, conds: list[Cond]) -> int: ...

    @abstractmethod
    async def _do_claim(self, table: str, conds: list[Cond], set_values: dict[str, Any],
                        order_by: OrderBy, limit: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def _do_nextval(self, sequence: str) -> int: ...

    # ---- 元数据与校验 ----

    def meta(self, table: str) -> TableMeta:
        m = self.tables.get(table)
        if m is None:
            raise ValueError(f"未定义的表：{table!r}")
        return m

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def _check_columns(self, m: TableMeta, values: Mapping[str, Any]) -> None:
        m.check_columns(values)

    def _validate(self, m: TableMeta, values: Mapping[str, Any], *, partial: bool) -> None:
        self._check_columns(m, values)
        for name, v in values.items():
            col = m.column(name)
            assert col is not None
            if v is None and not col.nullable and not col.auto:
                raise IntegrityConflict(f"{m.name}.{name} 非空，不能写 NULL")
            if col.is_json and v is not None and not isinstance(v, (dict, list, str)):
                raise TypeError(f"{m.name}.{name} 是 jsonb 列，需传 dict/list/JSON 字符串")
            if col.enum and v is not None and v not in col.enum:
                raise IntegrityConflict(
                    f"{m.name}.{name} 只接受 {list(col.enum)}，实得 {v!r}")
        if not partial:
            for col in m.columns:
                if col.nullable or col.has_default or col.auto:
                    continue
                if col.name not in values:
                    raise IntegrityConflict(f"{m.name}.{col.name} 非空且无默认值，插入必须给值")

    def _fill_defaults(self, m: TableMeta, row: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in row.items() if not (v is None and k in m.auto_columns)}
        for col in m.columns:
            if col.name not in out and col.has_default:
                out[col.name] = self.now() if col.default == NOW else col.default
        for name in m.pk:                       # 主键非空（列级或表级都适用）
            if out.get(name) is None and name not in m.auto_columns:
                raise IntegrityConflict(f"{m.name}.{name} 是主键，不能为空")
        return out

    def _conds(self, m: TableMeta, where: Where) -> list[Cond]:
        conds = normalize_where(where)
        for c in conds:
            m.check_columns([c.column])
        return conds

    # ---- 公开接口 ----

    async def insert(self, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        m = self.meta(table)
        self._validate(m, row, partial=False)
        return await self._do_insert(table, self._fill_defaults(m, row))

    async def get_by_pk(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        m = self.meta(table)
        where = {k: key.get(k) for k in m.pk}
        if any(v is None for v in where.values()):
            raise ValueError(f"{table} 需完整主键 {m.pk}，实得 {sorted(key)}")
        rows = await self._do_select(table, self._conds(m, where), None, 1)
        return rows[0] if rows else None

    async def select(self, table: str, *, where: Where = None, order_by: OrderBy = None,
                     limit: int | None = None) -> list[dict[str, Any]]:
        m = self.meta(table)
        return await self._do_select(table, self._conds(m, where), order_by, limit)

    async def update(self, table: str, values: Mapping[str, Any], *,
                     where: Where = None) -> list[dict[str, Any]]:
        m = self.meta(table)
        self._validate(m, values, partial=True)
        for k, v in list(values.items()):
            col = m.column(k)
            if col is not None and col.has_default and v is None:
                values = dict(values)
                values[k] = self.now() if col.default == NOW else col.default
                break
        return await self._do_update(table, self._conds(m, where), dict(values))

    async def delete(self, table: str, *, where: Where = None) -> int:
        m = self.meta(table)
        return await self._do_delete(table, self._conds(m, where))

    async def count(self, table: str, *, where: Where = None) -> int:
        return len(await self.select(table, where=where))

    async def max(self, table: str, column: str, *, where: Where = None) -> Any:
        m = self.meta(table)
        m.check_columns([column])
        vals = [r[column] for r in await self.select(table, where=where)
                if r.get(column) is not None]
        return max(vals) if vals else None

    async def max_value(self, table: str, column: str, *, where: Where = None) -> Any:
        """取某列最大值——**真正的聚合**，不是「全表拉回来在 Python 里 max」。

        为什么单独开一个：``max()`` 的上面的实现是 ``select`` 全表 + Python ``max``，
        于是「给某会话分配下一个 seq」变成 O(该会话全部历史行)，而它每追加一条消息
        就调用一次 → 整条会话 O(n²)。会话越长每轮越慢，真机表现为「聊久了明显变卡」。
        这里下推到后端：PG 走 ``SELECT MAX(col)``，内存引擎在自己的行上取 max。
        语义与 ``max()`` 完全一致（忽略 NULL，空表回 None），只是不再搬全表。
        """
        m = self.meta(table)
        m.check_columns([column])
        return await self._do_max(m, self._conds(m, where), column)

    @abstractmethod
    async def _do_max(self, meta: Any, conds: list[Cond], column: str) -> Any: ...

    async def distinct_values(self, table: str, column: str, *,
                              where: Where = None) -> list[Any]:
        """某列的去重取值——同样是下推聚合，不把整表（含 jsonb 正文）搬回 Python。

        为什么开这一个：按会话回收要列「库里出现过哪些 session_id」，用 ``select``
        全表等于每次开机把所有 artifacts 的 payload 拖一遍；会话数量是个位数，
        去重取值才是它本来的大小。语义：忽略 NULL，返回值有序（结果稳定才好写断言）。
        """
        m = self.meta(table)
        m.check_columns([column])
        return await self._do_distinct(m, self._conds(m, where), column)

    @abstractmethod
    async def _do_distinct(self, meta: Any, conds: list[Cond],
                           column: str) -> list[Any]: ...

    async def next_id(self, sequence: str) -> int:
        """取下一个序列值：序列名一律照 schema.sql（tasks.id ← task_seq）。"""
        if sequence not in self.sequences:
            raise ValueError(f"schema.sql 里没有序列 {sequence!r}")
        return await self._do_nextval(sequence)

    async def upsert(self, table: str, row: Mapping[str, Any], *,
                     update_values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """撞主键则改、否则插。FileStore 落盘、记忆与技能正文覆盖都走这里。"""
        m = self.meta(table)
        merged = self._fill_defaults(m, row)
        self._validate(m, merged, partial=False)
        upd = dict(update_values) if update_values is not None else {
            k: v for k, v in merged.items() if k not in m.pk and k not in m.auto_columns}
        self._validate(m, upd, partial=True)
        return await self._do_upsert(table, merged, upd)

    async def _do_upsert(self, table: str, row: dict[str, Any],
                         upd: dict[str, Any]) -> dict[str, Any]:
        """默认实现：先按主键查、再改或插。PG 覆写成单条 ON CONFLICT。"""
        m = self.meta(table)
        key = {k: row[k] for k in m.pk}
        got = await self.get_by_pk(table, key)
        if got is None:
            return await self._do_insert(table, row)
        if not upd:
            return got
        rows = await self._do_update(table, self._conds(m, key), upd)
        return rows[0] if rows else got

    async def claim(self, table: str, set_values: Mapping[str, Any], *, where: Where = None,
                    order_by: OrderBy = None, limit: int = 1) -> list[dict[str, Any]]:
        """原子领取若干行并改状态：PG 用 FOR UPDATE SKIP LOCKED，内存引擎锁内比较-改写。"""
        m = self.meta(table)
        self._validate(m, set_values, partial=True)
        return await self._do_claim(table, self._conds(m, where), dict(set_values),
                                    order_by, limit)


# --------------------------------------------------------------------------
# 内存引擎
# --------------------------------------------------------------------------


class MemoryDatastore(Datastore):
    """进程内替身：不连库，但约束、序列、领取语义与 PG 一致。"""

    def __init__(self) -> None:
        super().__init__()
        self._rows: dict[str, list[dict[str, Any]]] = {t: [] for t in self.tables}
        self._counters: dict[str, int] = {t: 0 for t in self.tables}
        self._seqs: dict[str, int] = {s: 0 for s in self.sequences}
        self._lock = asyncio.Lock()
        self._uniq: dict[tuple[str, tuple[str, ...]], set[tuple]] = {}
        # 等值过滤下的 MAX 索引：(表, 列) -> (维度标签, {键: 当前最大})
        self._max_idx: dict[tuple[str, str], tuple[str, dict[tuple, Any]]] = {}
        self.closed = False
        self.calls = 0                      # 契约测试用：确认调用真的落到本引擎

    async def ping(self) -> None:
        if self.closed:
            raise StorageUnavailable("MemoryDatastore 已 close()")
        self.calls += 1

    async def ensure_schema(self) -> None:
        await self.ping()

    async def close(self) -> None:
        self.closed = True

    async def _do_nextval(self, sequence: str) -> int:
        async with self._lock:
            self._seqs[sequence] = self._seqs.get(sequence, 0) + 1
            return self._seqs[sequence]

    async def _do_max(self, meta: Any, conds: list[Cond], column: str) -> Any:
        """取最大值。等值过滤时走**增量维护**的索引，否则退化成一次线性扫描。

        为什么值得单独维护：``max_value`` 在这里被用来分配「会话内下一个 seq」，
        每追加一条消息调一次 → 纯线性扫描就是整条会话 O(n²)。
        PG 侧这个查询走索引是 O(log n)，所以坑只在内存引擎上
        （离线测试、单机演示、memory 后端全吃它）。

        只对「全部是等值条件」的场景建索引：那是唯一在用的热路径，
        而且键能确定地算出来。其它条件（区间、in、null）仍走扫描——正确性不打折。
        """
        await self.ping()
        eq = self._eq_key(conds)
        if eq is not None:
            label, key = eq
            slot = self._max_idx.get((meta.name, column))
            if slot is None or slot[0] != label:
                # 标签不一致说明过滤维度变了：重建这张索引
                slot = (label, {})
                self._max_idx[(meta.name, column)] = slot
            table_idx = slot[1]
            if key not in table_idx:
                vals = [r.get(column) for r in self._rows[meta.name]
                        if match(r, conds) and r.get(column) is not None]
                table_idx[key] = max(vals) if vals else None
            return table_idx[key]
        vals = [r.get(column) for r in self._rows[meta.name]
                if match(r, conds) and r.get(column) is not None]
        return max(vals) if vals else None

    async def _do_distinct(self, meta: Any, conds: list[Cond],
                           column: str) -> list[Any]:
        await self.ping()
        return sorted({r.get(column) for r in self._rows[meta.name]
                       if match(r, conds) and r.get(column) is not None})

    @staticmethod
    def _eq_key(conds: list[Cond]) -> tuple[str, tuple] | None:
        """conds 全是等值条件时，给出 (维度标签, 键值元组)；否则 None。"""
        if not conds:
            return ("", ())
        parts: list[tuple[str, Any]] = []
        for c in conds:
            if c.op != "eq":
                return None
            parts.append((c.column, c.value))
        parts.sort()
        label = ",".join(n for n, _ in parts)
        return (label, tuple(v for _, v in parts))

    def _bump_max(self, table: str, row: Mapping[str, Any]) -> None:
        """行变化后把相关索引的对应槽更新掉（只增不减，所以取 max 即可）。

        删除场景保守处理：删行时把该维度整张索引丢掉，下次查询重建——
        删消息不是热路径，不值得为它维护减量逻辑。
        """
        for (tbl, column), (label, table_idx) in list(self._max_idx.items()):
            if tbl != table or column not in row:
                continue
            val = row.get(column)
            if val is None:
                continue
            # 该行的每个等值维度组合都可能被影响；按行里出现的列枚举代价高，
            # 这里直接按「同一标签」的所有键检查：若键是行的子集就更新。
            dims = label.split(",") if label else []
            for key in list(table_idx):
                if len(key) != len(dims):
                    continue
                if all(row.get(col) == v for col, v in zip(dims, key)):
                    cur = table_idx.get(key)
                    if cur is None or val > cur:
                        table_idx[key] = val

    def _check_unique(self, m: TableMeta, row: Mapping[str, Any], skip: Mapping | None = None) -> None:
        """唯一约束校验。

        原先对**每个**约束都线性扫全表，于是「插第 n 行」是 O(n)、整表是 O(n²)：
        实测 messages 表写 600 条要 1.0 秒（100 条只要 13ms）。PG 引擎靠唯一索引
        是 O(log n)，所以这个坑只落在**内存引擎**上——而离线测试、单机演示、
        以及所有以 memory 后端跑的路径都吃它。

        这里给每个约束维护一个键集合，命中判定退化成一次哈希查找。
        集合由 ``_index_row`` / ``_unindex_row`` 在增删改时**增量维护**，
        不靠「重建」——重建会漏掉「改值但行数不变」的更新（唯一键恰好会那么变）。
        """
        skip = skip or {}
        for grp in (m.pk, *m.unique):
            key = {c: row.get(c) for c in grp}
            if any(v is None for v in key.values()) or all(skip.get(c) == key[c] for c in grp):
                continue
            if tuple(key[c] for c in grp) in self._uniq.setdefault((m.name, grp), set()):
                raise IntegrityConflict(f"{m.name} 唯一约束 {grp} 冲突：{key}")

    def _index_row(self, table: str, row: Mapping[str, Any]) -> None:
        """把一行的唯一键登记进索引（插入后 / 更新后调用）。"""
        m = self.meta(table)
        for grp in (m.pk, *m.unique):
            key = tuple(row.get(c) for c in grp)
            if any(v is None for v in key):
                continue
            self._uniq.setdefault((table, grp), set()).add(key)

    def _unindex_row(self, table: str, row: Mapping[str, Any]) -> None:
        """把一行的唯一键从索引里摘掉（删除前 / 更新前调用）。"""
        m = self.meta(table)
        for grp in (m.pk, *m.unique):
            key = tuple(row.get(c) for c in grp)
            if any(v is None for v in key):
                continue
            got = self._uniq.get((table, grp))
            if got is not None:
                got.discard(key)

    async def _do_insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        m = self.meta(table)
        async with self._lock:
            row = dict(values)
            for name in m.auto_columns:
                if row.get(name) is None:
                    self._counters[table] += 1
                    row[name] = self._counters[table]
            self._check_unique(m, row)
            self._rows[table].append(row)
            self._index_row(table, row)
            self._bump_max(table, row)
        return dict(row)

    async def _do_select(self, table: str, conds: list[Cond], order_by: OrderBy,
                         limit: int | None) -> list[dict[str, Any]]:
        await self.ping()
        rows = [dict(r) for r in self._rows[table] if match(r, conds)]
        for term in reversed(order_by or []):
            desc = term.startswith("-")
            col = term[1:] if desc else term
            self.meta(table).check_columns([col])
            rows.sort(key=lambda r: _sortkey(r.get(col)), reverse=desc)
        return rows[:limit] if limit is not None else rows

    async def _do_update(self, table: str, conds: list[Cond],
                         values: dict[str, Any]) -> list[dict[str, Any]]:
        m = self.meta(table)
        out: list[dict[str, Any]] = []
        async with self._lock:
            for r in self._rows[table]:
                if not match(r, conds):
                    continue
                self._check_unique(m, {**r, **values}, skip=r)
                # 唯一键可能被改（如 seq/主键），旧键要先摘掉再登记新键
                self._unindex_row(table, r)
                r.update(values)
                self._index_row(table, r)
                self._bump_max(table, r)
                out.append(dict(r))
        return out

    async def _do_delete(self, table: str, conds: list[Cond]) -> int:
        async with self._lock:
            doomed = [r for r in self._rows[table] if match(r, conds)]
            for r in doomed:
                self._unindex_row(table, r)
            if doomed:
                # 删行让 max 索引的某些槽可能变高（不能只增），整张丢掉下次重建。
                # 删消息不是热路径，保守重建换取实现简单与判定正确。
                for key in [k for k in self._max_idx if k[0] == table]:
                    self._max_idx.pop(key, None)
            keep = [r for r in self._rows[table] if not match(r, conds)]
            n = len(self._rows[table]) - len(keep)
            self._rows[table] = keep
        return n

    async def _do_claim(self, table: str, conds: list[Cond], set_values: dict[str, Any],
                        order_by: OrderBy, limit: int) -> list[dict[str, Any]]:
        async with self._lock:
            picked = [r for r in self._rows[table] if match(r, conds)][:limit]
            for r in picked:
                # claim 也会改值（owner/lease），唯一键若被涉及同样要重登记
                self._unindex_row(table, r)
                r.update(set_values)
                self._index_row(table, r)
            return [dict(r) for r in picked]


def _sortkey(v: Any) -> tuple:
    """None 与混类型列（可空 duration）都能排：按「是否空 → 类型名 → 值」三级。"""
    if v is None:
        return (0, "", 0)
    if isinstance(v, datetime):
        return (1, v.isoformat(), 0)
    if isinstance(v, bool):
        return (1, "", int(v))
    if isinstance(v, (int, float)):
        return (1, "", v)
    return (1, str(v), 0)


# --------------------------------------------------------------------------
# PostgreSQL 引擎
# --------------------------------------------------------------------------


class PgDatastore(Datastore):
    """真线上引擎。

    SQLAlchemy 与 asyncpg 都在 __init__ 内延迟 import——未安装时 import 本模块不报错，
    只有真的构造 pg_minio 后端才需要它们（同 KafkaMessageQueue 对 aiokafka 的处理）。
    """

    def __init__(self, dsn: str, *, pool_size: int = 5, pool_max_overflow: int = 10,
                 pool_pre_ping: bool = True, pool_recycle_sec: int = 1800,
                 echo: bool = False) -> None:
        super().__init__()
        if not dsn.startswith("postgresql"):
            dsn = "postgresql+asyncpg://" + dsn.split("://", 1)[-1]
        elif "+asyncpg" not in dsn:
            dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        from sqlalchemy.ext.asyncio import create_async_engine  # 延迟导入

        self.dsn = dsn
        # 三条池参数各防一件事（原先只有 pool_size，另两条是审计里那条「池固定 5」）：
        # · max_overflow —— 长任务（渲染代查、批量转写）与 HTTP handler 共用这一池，
        #   池满时溢出几条比让请求卡在队列上等下一轮超时好；
        # · pre_ping —— 容器重启或 PG 侧掐掉空闲连接后，池里那条是死的，取出来直接
        #   一次 StorageUnavailable；pre_ping 先 ping 一下，坏连接就地丢弃重开；
        # · recycle —— 比 ping 更省事的一刀：连接活够久就换新，别等它被中间层掐了才发现。
        self._engine = create_async_engine(
            dsn, pool_size=pool_size, max_overflow=pool_max_overflow,
            pool_pre_ping=pool_pre_ping, pool_recycle=pool_recycle_sec, echo=echo)
        self._Text = __import__("sqlalchemy").text

    async def _run(self, sql: str, params: Mapping[str, Any] | None = None,
                   *, many: bool = False) -> list[dict[str, Any]]:
        from sqlalchemy.exc import DBAPIError, IntegrityError

        try:
            async with self._engine.begin() as conn:
                if many:
                    await conn.execute(self._Text(sql), [dict(p) for p in params or []])
                    return []
                res = await conn.execute(self._Text(sql), dict(params or {}))
                if res.returns_rows and not res.closed:
                    return [dict(r) for r in res.mappings().all()]
                return []
        except IntegrityError as e:
            raise IntegrityConflict(str(e.orig or e)) from e
        except DBAPIError as e:
            raise StorageUnavailable(f"PG 执行失败：{e.orig or e}") from e
        except (OSError, TimeoutError) as e:
            # 端口不通/连接被拒：asyncpg 抛的是裸 OSError 家族，不经 DBAPIError
            raise StorageUnavailable(f"PG 连接不可用：{type(e).__name__}: {e}") from e

    async def _scalar(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        from sqlalchemy.exc import DBAPIError

        try:
            async with self._engine.connect() as conn:
                return (await conn.execute(self._Text(sql), dict(params or {}))).scalar()
        except DBAPIError as e:
            raise StorageUnavailable(f"PG 执行失败：{e.orig or e}") from e
        except (OSError, TimeoutError) as e:
            raise StorageUnavailable(f"PG 连接不可用：{type(e).__name__}: {e}") from e

    async def ping(self) -> None:
        if await self._scalar("SELECT 1") != 1:
            raise StorageUnavailable("PG SELECT 1 未返回 1")

    async def ensure_schema(self) -> None:
        """建表/序列 → 补列与约束放宽（migrations.sql）→ 建索引。

        三份共用 `split_statements`：切句与剥整行注释只有一套规则，每张表前的 `-- 3.x`
        说明行不会再把 CREATE TABLE 整段吃掉。所有语句可反复跑，结果一致。

        索引为什么单独留到最后：已建过的库里 checkpoints 表早就存在，CREATE TABLE
        IF NOT EXISTS 直接跳过它，于是 schema.sql 尾部那些引用新列的 CREATE INDEX 会在
        ALTER ADD COLUMN 之前撞「column does not exist」。索引只依赖表与列，挪到最后
        对全新库没有影响，却让「老库升级」这一条路也能一次跑通。
        """
        ddl = split_statements(self.ddl)
        idx: list[str] = []
        rest = []
        for body in ddl:
            up = body.upper()
            (idx if up.startswith("CREATE INDEX") or up.startswith("CREATE UNIQUE INDEX")
             else rest).append(body)
        for group in (rest, split_statements(read_migrations_sql()), idx):
            for body in group:
                for attempt in (0, 1):
                    try:
                        await self._run(body)
                        break
                    except StorageUnavailable as e:
                        text = str(e)
                        if "already exists" in text:
                            # 多副本同时冷启动：migrations.sql 里那些 DROP IF EXISTS + ADD
                            # 成对语句会在 ADD 上撞车，而「已存在」正是这句要达到的状态。
                            break
                        if attempt == 0:
                            await asyncio.sleep(0.2)   # 并发 DDL 的锁等待/死锁：重试一次
                            continue
                        raise StorageUnavailable(
                            f"{text} ｜ 失败语句起始：{body[:60]!r}") from None

    async def close(self) -> None:
        await self._engine.dispose()

    async def _do_nextval(self, sequence: str) -> int:
        return int(await self._scalar(f"SELECT nextval('{safe_ident(sequence)}')"))

    async def _do_max(self, meta: Any, conds: list[Cond], column: str) -> Any:
        """下推到 SQL 的 MAX：不再把整表（含 jsonb 正文）搬回 Python 再 max。"""
        w, params = self._where(meta, conds)
        sql = (f"SELECT MAX({safe_ident(column)}) AS v FROM {safe_ident(meta.name)}{w}")
        return await self._scalar(sql, params)

    async def _do_distinct(self, meta: Any, conds: list[Cond],
                           column: str) -> list[Any]:
        """下推到 SQL 的 DISTINCT：同上，只回那一列的去重值。"""
        w, params = self._where(meta, conds)
        col = safe_ident(column)
        clause = (f"{w} AND {col} IS NOT NULL" if w
                  else f" WHERE {col} IS NOT NULL")
        sql = (f"SELECT DISTINCT {col} AS v FROM {safe_ident(meta.name)}{clause} "
               f"ORDER BY 1")
        return [r["v"] for r in await self._run(sql, params)]

    # ---- SQL 拼装：标识符白名单 + 值全绑定；jsonb 显式 CAST ----

    def _ph(self, m: TableMeta, col: Column, name: str, v: Any) -> tuple[str, Any]:
        if col.is_json and isinstance(v, (dict, list)):
            return f"CAST(:{name} AS jsonb)", json.dumps(v, ensure_ascii=False)
        return f":{name}", v

    def _insert_sql(self, m: TableMeta, values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        cols, ph, params = [], [], {}
        for i, col in enumerate([c for c in m.columns if c.name in values]):
            if col.auto and values[col.name] is None:
                continue                     # 交给 PG 的 serial 默认值
            p, v = self._ph(m, col, f"p{i}", values[col.name])
            cols.append(safe_ident(col.name))
            ph.append(p)
            params[f"p{i}"] = v
        sql = (f"INSERT INTO {safe_ident(m.name)} ({', '.join(cols)}) "
               f"VALUES ({', '.join(ph)}) RETURNING *")
        return sql, params

    def _where(self, m: TableMeta, conds: list[Cond]) -> tuple[str, dict[str, Any]]:
        if not conds:
            return "", {}
        frags, params = [], {}
        for i, c in enumerate(conds):
            col = m.column(c.column)
            assert col is not None
            name = safe_ident(c.column)
            if c.op == "is_null":
                frags.append(f"{name} IS NULL")
            elif c.op == "not_null":
                frags.append(f"{name} IS NOT NULL")
            elif c.op == "in":
                if not c.value:
                    frags.append("1 = 0")
                    continue
                keys = []
                for j, v in enumerate(c.value):
                    key = f"w{i}_{j}"
                    p, pv = self._ph(m, col, key, v)
                    keys.append(p)
                    params[key] = pv
                frags.append(f"{name} IN ({', '.join(keys)})")
            else:
                p, pv = self._ph(m, col, f"w{i}", c.value)
                frags.append(f"{name} {_SQL_OP[c.op]} {p}")
                params[f"w{i}"] = pv
        return " WHERE " + " AND ".join(frags), params

    def _order(self, m: TableMeta, order_by: OrderBy) -> str:
        if not order_by:
            return ""
        terms = []
        for t in order_by:
            desc = t.startswith("-")
            col = t[1:] if desc else t
            m.check_columns([col])
            terms.append(f"{safe_ident(col)} {'DESC' if desc else 'ASC'} NULLS LAST")
        return " ORDER BY " + ", ".join(terms)

    async def _do_insert(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        m = self.meta(table)
        sql, params = self._insert_sql(m, values)
        rows = await self._run(sql, params)
        if not rows:
            raise IntegrityConflict(f"{table} 插入未返回行")
        return _decoded(m, rows[0])

    async def _do_select(self, table: str, conds: list[Cond], order_by: OrderBy,
                         limit: int | None) -> list[dict[str, Any]]:
        m = self.meta(table)
        w, params = self._where(m, conds)
        sql = f"SELECT * FROM {safe_ident(table)}{w}{self._order(m, order_by)}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_decoded(m, r) for r in await self._run(sql, params)]

    async def _do_update(self, table: str, conds: list[Cond],
                         values: dict[str, Any]) -> list[dict[str, Any]]:
        m = self.meta(table)
        sets, sp = [], {}
        for i, col in enumerate([c for c in m.columns if c.name in values]):
            p, v = self._ph(m, col, f"u{i}", values[col.name])
            sets.append(f"{safe_ident(col.name)} = {p}")
            sp[f"u{i}"] = v
        w, params = self._where(m, conds)
        sql = (f"UPDATE {safe_ident(table)} SET {', '.join(sets)}{w} RETURNING *"
               if sets else f"SELECT * FROM {safe_ident(table)}{w}")
        return [_decoded(m, r) for r in await self._run(sql, {**sp, **params})]

    async def _do_delete(self, table: str, conds: list[Cond]) -> int:
        m = self.meta(table)
        w, params = self._where(m, conds)
        return len(await self._run(f"DELETE FROM {safe_ident(table)}{w} RETURNING 1", params))

    def _pk_or(self, m: TableMeta, rows: list[Mapping[str, Any]],
               tag: str) -> tuple[str, dict[str, Any]]:
        """若干行的主键 → "(a=:t0_0 AND b=:t0_1) OR (a=:t1_0 AND ...)"。"""
        frags, params = [], {}
        for i, row in enumerate(rows):
            inner = []
            for j, c in enumerate(m.pk):
                key = f"{tag}{i}_{j}"
                p, v = self._ph(m, m.column(c), key, row[c])
                inner.append(f"{safe_ident(c)} = {p}")
                params[key] = v
            frags.append("(" + " AND ".join(inner) + ")")
        return " OR ".join(frags), params

    async def _do_upsert(self, table: str, row: dict[str, Any],
                         upd: dict[str, Any]) -> dict[str, Any]:
        m = self.meta(table)
        sql, params = self._insert_sql(m, row)
        sql = sql.replace(" RETURNING *", "")
        conflict = ", ".join(safe_ident(k) for k in m.pk)
        if upd:
            cols = [safe_ident(c.name) for c in m.columns if c.name in upd]
            sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            sql += f" ON CONFLICT ({conflict}) DO UPDATE SET {sets}"
        else:
            sql += f" ON CONFLICT ({conflict}) DO NOTHING"
        sql += " RETURNING *"
        rows = await self._run(sql, params)
        if rows:
            return _decoded(m, rows[0])
        got = await self._do_select(table, [Cond(c, "eq", row[c]) for c in m.pk], None, 1)
        if not got:
            raise IntegrityConflict(f"{table} upsert 未取回行")
        return got

    async def _do_claim(self, table: str, conds: list[Cond], set_values: dict[str, Any],
                        order_by: OrderBy, limit: int) -> list[dict[str, Any]]:
        """同一事务内 SELECT ... FOR UPDATE SKIP LOCKED 再 UPDATE，两实例不会领到同一行。"""
        m = self.meta(table)
        w, params = self._where(m, conds)
        sql = (f"SELECT * FROM {safe_ident(table)}{w}{self._order(m, order_by)}"
               f" LIMIT {int(limit)} FOR UPDATE SKIP LOCKED")
        sets, sp = [], {}
        for i, col in enumerate([c for c in m.columns if c.name in set_values]):
            p, v = self._ph(m, col, f"u{i}", set_values[col.name])
            sets.append(f"{safe_ident(col.name)} = {p}")
            sp[f"u{i}"] = v
        async with self._engine.begin() as conn:
            picked = [dict(r) for r in (await conn.execute(self._Text(sql), params)).mappings().all()]
            if not picked:
                return []
            where, wp = self._pk_or(m, picked, "c")
            upd = (f"UPDATE {safe_ident(table)} SET {', '.join(sets)} "
                   f"WHERE {where} RETURNING *")
            rows = [dict(r) for r in
                    (await conn.execute(self._Text(upd), {**wp, **sp})).mappings().all()]
        return [_decoded(m, r) for r in rows]


def _decoded(m: TableMeta, row: Mapping[str, Any]) -> dict[str, Any]:
    """jsonb 列在驱动返回字符串时还原成 dict/list，两个引擎给仓储层的形状一致。"""
    out = dict(row)
    for name in m.json_columns:
        v = out.get(name)
        if isinstance(v, (str, bytes)):
            try:
                out[name] = json.loads(v)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return out
