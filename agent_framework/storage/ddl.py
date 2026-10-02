"""把 schema.sql 解析成表元数据——单一事实源。

内存引擎、仓储层、以及「DDL 与代码是否漂移」的测试都从这里取列名/类型/默认值/主键，
不再另写一份表结构。只支持本项目 DDL 用到的语法子集（CREATE TABLE / SEQUENCE / INDEX，
列定义为「名字 类型 [NOT NULL] [DEFAULT x] [UNIQUE] [CHECK (...)]」），遇到读不懂的行
直接抛错——宁可启动即失败，也不要静默把某一列漏掉。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_FILE = "schema.sql"
MIGRATIONS_FILE = "migrations.sql"

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", re.S)
_SEQUENCE = re.compile(r"CREATE SEQUENCE IF NOT EXISTS (\w+)")
_REFERENCES = re.compile(r"REFERENCES (\w+)\s*\(")
_TABLELINE = re.compile(r"^(PRIMARY KEY|UNIQUE|FOREIGN KEY|CHECK|EXCLUDE|CONSTRAINT)\b")
_COL = re.compile(r"^(\w+)\s+(.+)$", re.S)
_MULTIWORD_TYPES = ("double precision", "timestamp with time zone", "character varying")
_CONSTRAINT_RE = re.compile(r"\s+CHECK\s*\(", re.I)
_DEFAULT_RE = re.compile(r"\s+DEFAULT\s+(.+?)(?=\s+(?:NOT NULL|NULL|UNIQUE|PRIMARY KEY)\b|$)", re.I | re.S)

# now() 的哨兵值：由引擎在写入时填当前时间
NOW = "__now__"

_SKIP_LINE_STARTS = ("--",)


class SchemaSyntaxError(ValueError):
    """DDL 里出现了本解析器读不懂的写法。"""


def safe_ident(name: str) -> str:
    """校验标识符可安全拼进 SQL。

    表名/列名全部来自本模块解析出的白名单，但所有调用点仍统一过这一道——拼 SQL 的
    入口只留一个校验口，避免以后新增调用点绕过。
    """
    if not _IDENT.match(name):
        raise ValueError(f"非法 SQL 标识符：{name!r}")
    return name


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    default: Any = None           # Python 值；NOW 表示写入时取当前时间
    has_default: bool = False
    auto: bool = False            # serial / bigserial
    enum: tuple[str, ...] | None = None   # CHECK (col IN (...)) 的取值集合

    @property
    def is_json(self) -> bool:
        return self.type in ("jsonb", "json")

    @property
    def is_time(self) -> bool:
        return self.type.startswith("timestamp") or self.type in ("date", "time")

    def allows(self, value: Any) -> bool:
        return self.enum is None or value is None or value in self.enum


@dataclass(frozen=True)
class TableMeta:
    name: str
    columns: tuple[Column, ...]
    pk: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    @property
    def auto_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.auto)

    @property
    def json_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_json)

    def check_columns(self, cols: Any) -> None:
        """白名单校验：非本表列名一律拒绝（拼 SQL 前的最后一道闸）。"""
        keys = cols.keys() if isinstance(cols, dict) else cols
        for k in keys:
            if k not in self.names:
                raise ValueError(f"表 {self.name} 没有列 {k!r}（列名须存在于 schema.sql）")


def _literal(raw: str) -> Any:
    """SQL 默认值字面量 → Python 值。读不懂的抛错。"""
    s = raw.strip().rstrip(",").strip()
    low = s.lower()
    if low.startswith("now(") or low == "current_timestamp":
        return NOW
    if low in ("null",):
        return None
    if low in ("true", "false"):
        return low == "true"
    if s.startswith("'") and s.endswith("'"):
        inner = s[1:-1].replace("''", "'")
        if inner in ("{}", "[]"):
            return json.loads(inner)
        return inner
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        raise SchemaSyntaxError(f"无法解析的 DEFAULT 字面量：{raw!r}") from None


def _col_type(rest: str) -> tuple[str, str]:
    low = rest.lower()
    for t in _MULTIWORD_TYPES:
        if low.startswith(t):
            return t, rest[len(t):].strip()
    m = re.match(r"^(\w+)(?:\s*\([\d,\s]+\))?", rest)
    if not m:
        raise SchemaSyntaxError(f"无法解析列类型：{rest!r}")
    return m.group(1).lower(), rest[m.end():].strip()


_CHECK_IN = re.compile(r"^(\w+)\s+IN\s*\((.*)\)$", re.S)


def _iter_check_clauses(rest: str) -> list[str]:
    """抓出每个 CHECK(...) 括号内的完整内容——必须做括号配平，
    否则 IN ('a','b') 的内层右括号会把非贪婪匹配提前截断。"""
    out: list[str] = []
    low = rest.upper()
    i = 0
    while True:
        j = low.find("CHECK", i)
        if j < 0:
            return out
        k = rest.find("(", j)
        if k < 0:
            return out
        depth, p = 0, k
        while p < len(rest):
            if rest[p] == "(":
                depth += 1
            elif rest[p] == ")":
                depth -= 1
                if depth == 0:
                    break
            p += 1
        if depth != 0:
            raise SchemaSyntaxError(f"CHECK 括号未闭合：{rest[k:k + 40]!r}")
        out.append(rest[k + 1:p])
        i = p + 1


def _parse_checks(rest: str, name: str) -> tuple[str, ...] | None:
    """从列定义里抽出 CHECK (col IN ('a','b')) 的取值集合；其它 CHECK 形态忽略。"""
    for clause in _iter_check_clauses(rest):
        m = _CHECK_IN.match(re.sub(r"\s+", " ", clause).strip())
        if not m:
            continue
        if m.group(1) != name:
            raise SchemaSyntaxError(f"列 {name} 上的 CHECK 指的是 {m.group(1)}：{clause!r}")
        vals = [x.strip() for x in m.group(2).split(",")]
        if not vals or any(not v.startswith("'") or not v.endswith("'") for v in vals):
            raise SchemaSyntaxError(f"CHECK IN 只支持字符串字面量列表：{clause!r}")
        return tuple(v[1:-1].replace("''", "'") for v in vals)
    return None


def _parse_column(line: str) -> Column:
    line = re.sub(r"\s+", " ", line).strip().rstrip(",")
    m = _COL.match(line)
    if not m:
        raise SchemaSyntaxError(f"无法解析的列定义行：{line!r}")
    name, tail = m.group(1), m.group(2)
    ctype, rest = _col_type(tail)
    enum = _parse_checks(rest, name)
    # 类型后的 CHECK(...) 整段剥掉，避免把 CHECK 里的字面量误读成 DEFAULT
    rest = _CONSTRAINT_RE.split(rest, maxsplit=1)[0]
    auto = ctype in ("serial", "bigserial")
    nullable = not re.search(r"\bNOT NULL\b", rest, re.I)
    has_def = bool(_DEFAULT_RE.search(rest))
    default = _literal(_DEFAULT_RE.search(rest).group(1)) if has_def else None
    if auto:
        nullable = False
    if enum is not None and default is not None and default not in enum:
        raise SchemaSyntaxError(f"列 {name} 的默认值 {default!r} 不在 CHECK 允许集 {enum}")
    return Column(name=name, type=ctype, nullable=nullable, default=default,
                  has_default=has_def, auto=auto, enum=enum)


def _parse_body(body: str) -> tuple[tuple[Column, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]:
    # 去掉注释行后按「顶层逗号」切片段——CHECK (a, b) 内部的逗号在括号里，不会被切开
    lines = [l for l in body.splitlines() if l.strip() and not l.strip().startswith(_SKIP_LINE_STARTS)]
    joined = " ".join(l.strip() for l in lines)
    parts, buf, depth = [], "", 0
    for ch in joined:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())

    cols: list[Column] = []
    pk: tuple[str, ...] = ()
    unique: list[tuple[str, ...]] = []
    for p in parts:
        if not p:
            continue
        if p.lower().startswith("constraint "):
            p = p[len("constraint "):].split(" ", 1)[1]
        if _TABLELINE.match(p):
            head = re.sub(r"\s+", " ", p.split("(", 1)[0]).strip().lower()
            inside = re.search(r"\(([^)]*)\)", p)
            if not inside:
                raise SchemaSyntaxError(f"表级约束缺少列清单：{p!r}")
            grp = tuple(x.strip() for x in inside.group(1).split(","))
            if head == "primary key":
                pk = grp
            elif head == "unique":
                unique.append(grp)
            continue
        col = _parse_column(p)
        if re.search(r"\bPRIMARY KEY\b", p, re.I):
            if pk:
                raise SchemaSyntaxError(f"多处主键声明：{p!r}")
            pk = (col.name,)
            col = replace(col, nullable=False)
        cols.append(col)
        if re.search(r"\bUNIQUE\b", p, re.I):
            unique.append((col.name,))
        continue
    if not cols:
        raise SchemaSyntaxError("表没有任何列定义")
    if not pk:
        raise SchemaSyntaxError(f"表未声明主键：列里既无 PRIMARY KEY 也无表级主键")
    return tuple(cols), pk, tuple(unique)


def parse_schema(sql: str) -> dict[str, TableMeta]:
    """schema.sql 文本 → {表名: TableMeta}。"""
    tables: dict[str, TableMeta] = {}
    for m in _TABLE.finditer(sql):
        name = m.group(1)
        cols, pk, uniq = _parse_body(m.group(2))
        meta = TableMeta(name=name, columns=cols, pk=pk, unique=uniq)
        known = set(meta.names)
        for grp in (pk, *uniq):
            for col in grp:
                if col not in known:
                    raise SchemaSyntaxError(f"{name}: 约束列 {col!r} 不在列清单里")
        if name in tables:
            raise SchemaSyntaxError(f"表重复定义：{name}")
        tables[name] = meta
    for ref in _REFERENCES.findall(sql):
        if ref not in tables:
            raise SchemaSyntaxError(f"外键指向未定义的表：{ref}")
    return tables


def _schema_path() -> Path:
    return Path(__file__).with_name(SCHEMA_FILE)


def read_schema_sql() -> str:
    return _schema_path().read_text(encoding="utf-8")


def read_migrations_sql() -> str:
    """已建库的约束放宽（真引擎在 schema.sql 之后执行）。文件缺失时返回空串。

    单独一份是因为 schema.sql 有条守卫不变量「不含 DROP，纯幂等建表」，而放宽 CHECK
    必须 DROP + ADD。B5 的 run_migrate.py 接管迁移后，本文件并入其中。
    """
    p = _schema_path().with_name(MIGRATIONS_FILE)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


@lru_cache(maxsize=1)
def load_schema() -> tuple[dict[str, TableMeta], frozenset[str], str]:
    """(表元数据, 序列名集合, DDL 原文) —— 全项目唯一读取 schema.sql 的入口。"""
    sql = read_schema_sql()
    return parse_schema(sql), frozenset(parse_sequences(sql)), sql


def parse_sequences(sql: str) -> set[str]:
    return set(_SEQUENCE.findall(sql))


def split_statements(sql: str) -> list[str]:
    """DDL 文本 → 可逐句执行的语句。

    先剥整行注释再判空：每张表前都有 `-- 3.x` 说明行，按片段开头过滤会把 CREATE TABLE
    整段跳掉，只留下紧随的 CREATE INDEX 报 relation 不存在。

    认 ``$$`` 美元引用：migrations.sql 里的 ``DO $$ ... $$`` 块内部每条语句都以分号结尾，
    按分号切开会把一个块炸成十几条残缺语句。块内不再嵌套 ``$$``（要嵌套得换 ``$tag$``）。
    """
    parts, buf, in_dollar = [], "", False
    i = 0
    while i < len(sql):
        if sql.startswith("$$", i):
            in_dollar = not in_dollar
            buf += "$$"
            i += 2
            continue
        ch = sql[i]
        buf += ch
        i += 1
        if ch == ";" and not in_dollar:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)

    out: list[str] = []
    for raw in parts:
        body = "\n".join(l for l in raw.splitlines()
                         if l.strip() and not l.strip().startswith("--")).strip()
        if body:
            out.append(body)
    return out
