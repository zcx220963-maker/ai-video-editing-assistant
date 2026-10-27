# -*- coding: utf-8 -*-
"""R13：frontmatter YAML 子集（行内列表/引号/多行）+ 未知 requires 前缀 fail-closed。"""

from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.skill import (  # noqa: E402
    _parse_frontmatter, default_requirement_checker)

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def main() -> None:
    # ---- 基本 key: value ----
    meta, body = _parse_frontmatter("---\nname: demo\ndisplay: 演示\n---\n正文")
    check(meta["name"] == "demo" and meta["display"] == "演示", "普通 key: value")
    check(body == "正文", "正文切得对")

    # ---- 行内列表 → 拍平成逗号串（下游按 str 读）----
    meta, _ = _parse_frontmatter("---\ntags: [a, b, c]\n---\n")
    check(meta["tags"] == "a, b, c", f"行内列表拍平（实际 {meta['tags']!r}）")

    # ---- 引号 ----
    meta, _ = _parse_frontmatter('---\ndisplay: "带: 冒号的名字"\n---\n')
    check(meta["display"] == "带: 冒号的名字", f"引号里的冒号不被切开（{meta['display']!r}）")

    # ---- 布尔 ----
    meta, _ = _parse_frontmatter("---\nalways: true\n---\n")
    check(meta["always"] == "true", "布尔拍平成 true")

    # ---- 没有 frontmatter ----
    meta, body = _parse_frontmatter("# 只有正文")
    check(meta == {} and body == "# 只有正文", "没有 frontmatter 时原样返回")

    # ---- 坏 YAML 回退而不炸 ----
    meta, body = _parse_frontmatter("---\nname: [未闭合\n---\n正文")
    check(isinstance(meta, dict), "坏 YAML 回退成 dict（不抛）")

    # ---- requires：未知前缀 fail-closed（R13）----
    ok, why = default_requirement_checker("CLI: sh")
    check(ok is True, "已知 CLI 前缀按真实探测")
    ok, why = default_requirement_checker("ENV: WB_NO_SUCH_ENV_XYZ")
    check(ok is False, "缺环境变量 → 不可用")
    ok, why = default_requirement_checker("MCP: some-server")
    check(ok is False and "未知依赖类型" in why,
          f"未知前缀按不可用处理（实际 {ok}, {why}）")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
