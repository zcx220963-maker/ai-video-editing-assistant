"""内部组件目录：从 `remotion/src/lib` 的**源码**现读，不手写。

为什么要有它：写手手里只有技能正文里那几张手写的表——`@dg/lib/draw/*` 有一张完整的
props 表，笔刷、场景层、走路件只有一句话的简介，`<WalkingFigure>` 的 18 个字段
（`render` / `outfit` / `rimSide` …）根本没有成文清单。于是写手只能猜：猜错 prop 名
不会报错——组件落回缺省值，画面不对、闸① 不红、渲染也不报，只有人看片时才发现
「这个人物怎么是平涂的」。用户 2026-10-22 的原话：「导入别人的 skill，只保留一个
自动入口，就能调用我们内部所有的组件库」——组件库对写手开放，先得让它**看得见**。

这份目录是源码的投影：导出名、`*Props` 的字段名与类型、`export type X = 'a' | 'b'`
的联合取值全部现读。手写一份清单必然漂，而漂的那一侧正是写手照着写的那一侧
（2026-10-19 真机 s07 把 `x={W*0.32}` 传进了收画面比例的模板参数，人物被乘了第二遍）。

三条边界：
- **只给签名，不给教程**。写法示例在技能正文与模板 usage 里；这里多写一句，
  每镜都要多付一次 token。
- **限量**：整段有字节上限（`CATALOG_MAX_BYTES`），超了按模块顺序截，并明说截了
  多少——静默截断会让写手以为「库里就这些」。
- **读不到就整段不发**（源码不在镜像里 / 路径不对）：少一段目录只是少点参考，
  不该让写镜失败。
"""

from __future__ import annotations

import re
from functools import lru_cache
from . import code_spec as cspec

#: 缓存代号：源码重新读一次就 +1（`_build` 的参数只用来让 lru_cache 换槽）。
_GENERATION = [0]

#: 整段上限（字节）。取 5 KB 上下：写手每镜都背着它，而它只在「真要 import 某件」时
#: 有用；再大就开始和 brief、模板段抢注意力。
CATALOG_MAX_BYTES = 5_200

#: 一个组件最多列几个字段。走路件有 18 个、笔刷 9 个，全列没问题；这一档防的是
#: 以后出现接收 `...rest` 或大对象的那种组件把预算吃光。
_FIELD_MAX = 20

#: 单个类型的长度上限（`blend?: 'normal' | 'multiply' | …` 这类联合最长的有 90 字符）。
_TYPE_MAX = 60

#: 目录覆盖的模块（按这个顺序进目录，也是截断时的优先级）：
#: 笔刷 → 场景层 → 走路件 → 面板件 → 简笔画零件。
#:
#: **风格模板不在这一份里**：它们只在「本片选了这套模板」时才有意义，而那时完整的
#: props 由 `style_templates.component_usage()` 按模板记录单独注入——那份比这里准
#: （带着参考片沉淀下来的缺省值，以及「位置是画面比例 0–1」的单位边界）。没选模板
#: 的片子里列一堆模板组件名，只会让写手去 import 一套它不该用的画法。
_MODULES: tuple[tuple[str, str], ...] = (
    ("@dg/lib/brush", "brush/index.tsx"),
    ("@dg/lib/scene", "scene.tsx"),
    ("@dg/lib/walk", "walk.tsx"),
    ("@dg/lib/Caption", "Caption.tsx"),
    ("@dg/lib/ClipCard", "ClipCard.tsx"),
    ("@dg/lib/DigitRoll", "DigitRoll.tsx"),
    ("@dg/lib/FlashCut", "FlashCut.tsx"),
    ("@dg/lib/PageCam", "PageCam.tsx"),
    ("@dg/lib/VerticalTicker", "VerticalTicker.tsx"),
    ("@dg/lib/FlatPanel", "FlatPanel.tsx"),
    ("@dg/lib/draw/Figure", "draw/Figure.tsx"),
    ("@dg/lib/draw/Face", "draw/Face.tsx"),
    ("@dg/lib/draw/Machine", "draw/Machine.tsx"),
    ("@dg/lib/draw/Building", "draw/Building.tsx"),
    ("@dg/lib/draw/Vehicle", "draw/Vehicle.tsx"),
    ("@dg/lib/draw/Gear", "draw/Gear.tsx"),
    ("@dg/lib/draw/Chart", "draw/Chart.tsx"),
    ("@dg/lib/draw/Arrow", "draw/Arrow.tsx"),
    ("@dg/lib/draw/Coin", "draw/Coin.tsx"),
    ("@dg/lib/draw/Book", "draw/Book.tsx"),
    ("@dg/lib/draw/Scale", "draw/Scale.tsx"),
    ("@dg/lib/draw/Clock", "draw/Clock.tsx"),
    ("@dg/lib/draw/Crown", "draw/Crown.tsx"),
)

_EXPORT_RE = re.compile(r"^export const (\w+): React\.FC<(\w+Props)>", re.M)
_ALIAS_RE = re.compile(r"^export type (\w+)\s*=\s*([^;{]+);", re.M)
#: `export interface XProps {`（没有 `=`）与 `export type XProps = {` 两种都收；
#: 基件走 `extends Inked`，那些字段不在本文件里，只能靠 `Inked` 自己那一行说明。
_BLOCK_RE = re.compile(
    r"export (?:interface|type) %s(?:\s+extends\s+[\w, ]+)?\s*(?:=\s*)?\{")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _alias_values(text: str) -> dict[str, str]:
    """文件里的类型别名 → 一行能用的话。

    两种都收：
    - 字面量联合：`export type MachineKind = 'steam' | 'press'` → `steam|press`
      ——写手最需要的就是取值集（写别的会静默落回缺省款）；
    - 对象：`export interface WalkOutfit { … }` 或 `type X = { … }` → `{build,garment,…}`
      ——`<WalkingFigure outfit={…}>` 的 18 个枚举字段都住在这里面，不展开它，
      目录里就只有一句 `outfit?: WalkOutfit`，等于没说。
    """
    out: dict[str, str] = {}
    for name, body in _ALIAS_RE.findall(text):
        body = _norm(body)
        if "{" in body or "=>" in body or "import(" in body:
            continue
        parts = [p.strip().strip("'\"") for p in body.split("|")]
        if not all(re.fullmatch(r"[\w.\-一-鿿]+", p) for p in parts if p):
            continue
        if len(parts) > 12:
            continue
        out[name] = "|".join(parts)
    for name in re.findall(r"^export (?:interface|type) (\w+)\s*(?:=\s*)?\{", text, re.M):
        body = _props_body(text, name)
        if not body:
            continue
        names = [m.group(1) for m in
                 (re.match(r"^\s*([A-Za-z_]\w*)(\?)?\s*:", ln)
                  for ln in body.splitlines()) if m]
        if names:
            out.setdefault(name, "{" + ",".join(names[:8]) + "}")
    return out


def _props_body(text: str, name: str) -> str:
    """`export interface XProps {...}` 的花括号内文（允许 `extends`，也允许类型别名）。"""
    m = re.compile(_BLOCK_RE.pattern % re.escape(name)).search(text)
    if not m:
        return ""
    depth = 1
    i = m.end()
    start = i
    while i < len(text) and depth:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[start:i - 1] if depth == 0 else ""


_ONE_LINE_FIELD_RE = re.compile(r"^([A-Za-z_]\w*)(\?)?\s*:\s*(.+)$")

_LITERAL_UNION_RE = re.compile(
    r"^(?:'[^']*'|\"[^\"]*\")(?:\s*\|\s*(?:'[^']*'|\"[^\"]*\"))+$")


def _compact_union(typ: str) -> str:
    """`'normal' | 'multiply' | 'overlay'` → `normal|multiply|overlay`。

    字面量联合里的引号只占字节，取值本身才是写手要抄的东西；与别名那一侧
    （`kind?: steam|press|arm|turbine`）保持同一种写法，免得同一段里两种排版。
    """
    if _LITERAL_UNION_RE.match(typ):
        return "|".join(p.strip().strip("'\"") for p in typ.split("|"))
    return typ


def _fields(body: str, aliases: dict[str, str]) -> list[str]:
    """字段 → `name?: type`。多行类型（对象、函数）拼回一行再截。"""
    out: list[str] = []
    buf = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("/*") \
                or line.startswith("*") or line.startswith("/**"):
            continue
        buf = (buf + " " + line).strip() if buf else line
        if not buf.endswith(";") and not buf.endswith(","):
            continue
        entry = buf.rstrip(";,").strip()
        buf = ""
        m = _ONE_LINE_FIELD_RE.match(entry)
        if not m:
            continue
        name, opt, typ = m.group(1), m.group(2), _norm(m.group(3))
        typ = _compact_union(_norm(aliases.get(typ, typ)))
        # 行内对象/函数类型不展开（`{ x: number; y: number }`、`(f: number) => void`），
        # 只报「对象」：这不是省事，是那两种形状在签名里没有可照抄的取值。
        # 别名展开出来的 `{build,garment,…}` 没有冒号，不受这一条影响。
        if "=>" in typ or (typ.startswith("{") and ":" in typ):
            typ = "对象"
        if len(typ) > _TYPE_MAX:
            typ = typ[:_TYPE_MAX - 1] + "…"
        out.append(f"{name}{'?' if opt else ''}: {typ}")
        if len(out) >= _FIELD_MAX:
            break
    return out


def _module_line(alias: str, rel: str) -> str:
    path = cspec._master_file("src/lib/" + rel)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    aliases = _alias_values(text)
    comps: list[str] = []
    for comp, props in _EXPORT_RE.findall(text):
        fields = _fields(_props_body(text, props), aliases)
        # 一个字段都没有的组件（props 是行内对象 / 没声明）只报名字——
        # 报一行空签名比不报更坏：写手会以为它不收参数。
        sig = (f"`<{comp}/>` " + " · ".join(fields)) if fields else f"`<{comp}/>`"
        comps.append(sig)
    if not comps:
        return ""
    return f"`{alias}`：" + "；".join(comps)


@lru_cache(maxsize=4)
def _build(generation: int) -> str:
    """``generation`` 只是让缓存可失效（见 `clear_cache`），不参与渲染。"""
    del generation
    lines: list[str] = []
    used = 0
    skipped = 0
    for alias, rel in _MODULES:
        line = _module_line(alias, rel)
        if not line:
            continue
        size = len(line.encode("utf-8")) + 1
        if lines and used + size > CATALOG_MAX_BYTES:
            skipped += 1
            continue
        lines.append(line)
        used += size
    if not lines:
        return ""
    text = ("=== 内部组件库（从源码读出的签名；用得上就 import，用不上自己画）===\n"
            "字段带 `?` 是可省。这些件的位置/尺寸都是**成片设备像素**（不是画面比例）。"
            "只列签名，用法见技能正文的示例与模板 usage。\n"
            + "\n".join(lines))
    if skipped:
        text += f"\n（另有 {skipped} 个模块没有列出：这一段有长度上限。）"
    return text


def component_catalog() -> str:
    """写手用的组件签名目录；源码读不到就返回空串（少一段参考，不影响写镜）。"""
    return _build(_GENERATION[0])


def clear_cache() -> None:
    """源码换了一份就重读（测试与热替换用）。"""
    _GENERATION[0] += 1
    _build.cache_clear()
