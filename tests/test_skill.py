"""SKILL 系统验证（不联网）：导入 → 表 + 对象存储、清单/渐进加载/always/依赖不可用。

技能库已从「扫本地目录」换成「读 PG skills 表 + MinIO 附件」（spec §3.13、§4）：
本地目录只是导入源，读路径不碰磁盘——所以这里导入完成后把源目录改名，
清单与 load_skill 必须照常工作。

运行：  python tests/test_skill.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.context import ContextBuilder
from agent_framework.session import Session
from agent_framework.skill import (
    SkillLoader,
    SkillManifestContextSource,
    register_skill_tools,
)
from agent_framework.storage import build_storage
from agent_framework.tool import is_tool_error
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _write_skill(root: Path, name: str, frontmatter: str, body: str,
                 files: dict[str, bytes] | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    for rel, data in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "skills")
        _write_skill(
            root, "github",
            'name: github\ndescription: "Interact with GitHub using gh CLI"\nalways: true',
            "用 gh CLI 操作 GitHub：先 gh auth status，再按需 gh pr create。",
        )
        _write_skill(
            root, "editing",
            "name: editing\ndescription: 通用剪辑流程",
            "步骤：load_media → split_shots → ... → render_video。",
            files={"references/steps.md": "第一步：加载素材\n".encode(),
                   "scripts/run.py": b"print('render')\n",
                   "assets/logo.png": bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0xFF])},
        )
        _write_skill(
            root, "weather",
            "name: weather\ndescription: Get current weather\nrequires: CLI: definitely_missing_cli_xyz",
            "需要 curl 抓取天气。",
        )

        storage = build_storage("memory")
        await storage.start()
        try:
            loader = SkillLoader(storage)
            check(await loader.discover() == [], "读路径只看表：未导入时清单为空")

            names = sorted(await loader.sync_from_dir(root))
            check(names == ["editing", "github", "weather"], "导入源目录后三个技能入库")

            skills = {s.name: s for s in await loader.discover()}
            check(set(skills) == {"github", "editing", "weather"}, "表里读到三个技能")
            check(skills["github"].always and skills["github"].available, "github always 且可用")
            check(skills["editing"].available and not skills["editing"].always,
                  "editing 可用非常驻")
            check(not skills["weather"].available, "weather 因缺依赖 available=false")
            check("缺少依赖" in skills["weather"].unavailable_reason, "不可用原因记录")

            # 落点：正文在 skills 行，附件在 skill_files 行 + 对象存储
            rows = await storage.db.select("skills")
            check(all(r["body"] and r["frontmatter"] for r in rows),
                  "正文与 frontmatter 都在 skills 表里")
            frows = await storage.db.select("skill_files", where={"skill": "editing"})
            check([f["relpath"] for f in frows] == ["assets/logo.png", "references/steps.md",
                                                    "scripts/run.py"],
                  "附件按 relpath 登记（SKILL.md 不占附件位）")
            check(all(f["object_key"] == f"skills/editing/{f['relpath']}" and f["bytes"] > 0
                      for f in frows), "object_key 走 skills/{name}/{relpath} 布局")
            editing = skills["editing"]
            check((await loader.read_attachment(editing, "references/steps.md")).decode()
                  .startswith("第一步"), "附件字节真在对象存储里")

            # 源目录改名后仍能读：证明读路径不碰磁盘
            moved = root.with_name("skills-moved")
            root.rename(moved)
            check(len(await loader.discover()) == 3, "导入源消失后清单照常（读的是表）")

            # 清单渲染
            src = SkillManifestContextSource(loader)
            rendered = await src.render("帮我剪个视频")
            check("<skills>" in rendered and '<skill available="true">' in rendered,
                  "清单含可用技能")
            check('<skill available="false">' in rendered, "清单标注不可用技能")
            check("<requires>CLI: definitely_missing_cli_xyz</requires>" in rendered,
                  "不可用技能给出 requires")
            check("<location>skills/github/SKILL.md</location>" in rendered,
                  "清单 location 指向技能键面前缀")

            # always 常驻附正文；非 always 正文【不】进清单
            check("<skill_instructions" in rendered and "gh auth status" in rendered,
                  "always 技能正文常驻注入")
            check("render_video" not in rendered, "非 always 技能正文不进清单（渐进加载）")

            # load_skill 工具：正文 + 附件清单 + 按需取附件
            reg = ToolRegistry()
            register_skill_tools(reg, loader)
            check(reg.get("load_skill").concurrency_safe, "load_skill 可并发 (read_only)")

            r = await reg.execute("load_skill", {"name": "editing"})
            check("render_video" in r and "location=skills/editing/SKILL.md" in r,
                  f"按需拉取 editing 正文: {r[:28]}")
            check("references/steps.md" in r and "assets/logo.png" in r,
                  "正文返回时附附件清单（下一步可按 path 取）")
            r = await reg.execute("load_skill", {"name": "editing",
                                                 "path": "references/steps.md"})
            check("第一步：加载素材" in r and "file=references/steps.md" in r,
                  "path 取回文本附件内容")
            r = await reg.execute("load_skill", {"name": "editing", "path": "SKILL.md"})
            check("render_video" in r, "path=SKILL.md 等价于取正文")
            r = await reg.execute("load_skill", {"name": "editing", "path": "assets/logo.png"})
            check("非文本附件" in r and "skills/editing/assets/logo.png" in r,
                  "二进制附件给临时链接而非字节")
            r = await reg.execute("load_skill", {"name": "editing",
                                                 "path": "../../etc/passwd"})
            check(is_tool_error(r) and "无附件" in str(r), "未登记的相对路径被拒（含穿越写法）")
            r = await reg.execute("load_skill", {"name": "weather"})
            check(is_tool_error(r) and "不可用" in str(r), "load 不可用技能被拒")
            r = await reg.execute("load_skill", {"name": "nope"})
            check(is_tool_error(r), "load 未知技能报错")

            # 重导入：同名整体覆盖，附件清单以最新为准
            root2 = Path(tmp, "skills2")
            _write_skill(root2, "editing", "name: editing\ndescription: 通用剪辑流程 v2",
                         "步骤 v2：只有 load_media。")
            await loader.sync_from_dir(root2)
            again = await loader.get("editing")
            check(again.description.endswith("v2") and "只有 load_media" in again.body,
                  "重导入覆盖正文与描述")
            check(again.files == [], "重导入后附件清单以最新一次为准")
            check(await storage.objects.head("skills/editing/references/steps.md") is None,
                  "没被沿用的旧附件对象一起删掉（不留孤儿字节）")
            check(len(await storage.db.select("skills")) == 3, "重导入不叠加行")
            check(sorted(s.name for s in await loader.discover()) ==
                  ["editing", "github", "weather"], "库里仍是三个技能")

            # 注入到 ContextBuilder
            b = ContextBuilder("BASE", context_sources=[src], runtime_context=None)
            msgs = await b.build(Session(user_id="u", conversation_id="c"), "剪视频")
            check("<skills>" in msgs[0]["content"], "System 含 <skills> 清单")
        finally:
            await storage.close()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
