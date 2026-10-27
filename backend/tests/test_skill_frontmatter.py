# -*- coding: utf-8 -*-
"""R13：frontmatter YAML 子集（行内列表/引号/多行）+ 未知 requires 前缀 fail-closed。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.skill import (  # noqa: E402
    LoadSkillTool,
    SkillLoader,
    _parse_frontmatter,
    default_requirement_checker,
)
from agent_framework.storage import build_storage  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


_SKILL_MD = """\
---
name: demo_skill
description: 演示技能
sections: knobs, writer-only
readers: writer-only=writer
---
默认正文第一段。

<!-- section: knobs -->
## 旋钮
knobs-body-content
<!-- /section -->

<!-- section: writer-only -->
## 写手
writer-body-content
"""


async def load_section() -> None:
    """R13-c：导入真文件 → load_skill(section=…) 只回那一块，不夹默认集。"""
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "skills"
        (root / "demo_skill").mkdir(parents=True)
        (root / "demo_skill" / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")

        storage = build_storage("memory")
        await storage.start()
        loader = SkillLoader(storage)
        imported = await loader.sync_from_dir(root)
        check(imported == ["demo_skill"], f"技能导入成功（{imported}）")
        tool = LoadSkillTool(loader)

        whole = await tool.execute("demo_skill")
        check("默认正文第一段" in whole, "整份加载含默认集")
        check("knobs" in whole and "writer-only" in whole,
              "整份加载附分段目录（模型才知道键名）")

        part = await tool.execute("demo_skill", section="knobs")
        check("knobs-body-content" in part, "按 section 取到目标块")
        check("默认正文第一段" not in part, "单取不带默认集（省下的就是省下的）")
        check("writer-body-content" not in part, "不夹别的块")
        check("[skill:demo_skill" in part and "section=knobs" in part, "回执标明来源与段名")

        writer = await tool.execute("demo_skill", section="writer-only")
        check(writer.startswith("Error") and "写手侧" in writer,
              "readers: writer 的段在规划侧被硬拦")

        missing = await tool.execute("demo_skill", section="nope")
        check(missing.startswith("Error") and "knobs" in missing,
              "键名写错时列出在案键名（不猜、不模糊匹配）")


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

    # ---- R13-c：真导入一份带分段标记的技能，按 section 单取 ----
    asyncio.run(load_section())

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
