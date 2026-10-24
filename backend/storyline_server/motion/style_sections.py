"""风格包正文的分段读取：索引 / 单取一块 / 默认集。

为什么单独一模块：同一份正文有两处读者——**美术导演**（排 brief 时要知道这套风格
有哪几件能力可选）与**逐镜写手**（只加载这一镜点名的那几件）。两个模块各自实现一份
正则会漂（标记格式一改，一处认一处不认，表现是「能力明明写了却没被加载」），
所以解析只有这一份。

标记与 `agent_framework.skill` 的 `<!-- section: … -->` **同形**：那边是技能加载面，
这边是画面注入面，隔着一个进程各自的代码库，不共享模块，但格式必须一致——用户拖进来的
技能包与系统风格包用的是同一套写法。

    <!-- section: volumetric-light -->
    （这一件能力的用法）
    <!-- /section -->

标记之外的正文是**默认集**：整片都成立的风格说明，任何一镜都带。
"""
from __future__ import annotations

import re

_MARK = re.compile(r"^<!--\s*section:\s*([A-Za-z0-9_.\-]+)\s*-->\s*$")
_END = re.compile(r"^<!--\s*/section\s*-->\s*$")


def pieces(body: str) -> list[tuple[str | None, str]]:
    """``[(段名或 None, 文本)]``；None = 标记之外的默认集（按原序，可能有多段）。"""
    out: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in (body or "").splitlines():
        m = _MARK.match(line.strip())
        if m:
            out.append((m.group(1), []))
            continue
        if _END.match(line.strip()):
            out.append((None, []))
            continue
        out[-1][1].append(line)
    return [(name, "\n".join(lines).strip()) for name, lines in out]


def default_text(body: str) -> str:
    """默认集（标记之外的全部正文）。"""
    return "\n\n".join(t for name, t in pieces(body) if name is None and t)


def has_sections(body: str) -> bool:
    return any(name for name, _ in pieces(body))


def section_text(body: str, name: str) -> str:
    """单取一块；没有这一块回空串（不抛——段名会随用户编辑变）。"""
    key = str(name or "").strip()
    if not key:
        return ""
    for piece, text in pieces(body):
        if piece == key:
            return text
    return ""


def index(body: str) -> list[tuple[str, str, int]]:
    """``[(能力名, 第一句, 字节数)]``——给模型看的索引，不是正文。

    索引必须由服务端主动给：段名是**服务端与技能之间的约定**，模型在 schema 与
    白名单里都查不到它。不给索引，模型只知道「有一份长正文」，还是会整份读。
    """
    out: list[tuple[str, str, int]] = []
    for name, text in pieces(body):
        if not name or not text:
            continue
        first = next((ln.strip().lstrip("#-* ").strip()
                      for ln in text.splitlines() if ln.strip()), "")
        out.append((name, first[:60], len(text.encode("utf-8"))))
    return out
