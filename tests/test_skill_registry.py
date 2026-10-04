"""技能注册通道（skill registry）：文件夹导入 / zip 上传 / 清单与词表联动。

对应「像 coding agent 一样靠文件夹和页面扩展能力」的三条通道之一（Skill 层）：
  ① 文件夹导入：examples/skills/ 下丢目录 → sync_from_dir 幂等入库（含新增的 ui_aesthetics）；
  ② zip 上传：import_zip 三道防线（大小/条目数/zip-slip）+ 根上 SKILL.md 自动归一；
  ③ 幂等：同名重导整体覆盖、附件清单以最新一次为准，不产生重复行。

运行：  python tests/test_skill_registry.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import io
import sys
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.skill import SkillLoader
from agent_framework.storage import build_storage

_checks = 0
_fails = 0
ROOT = Path(__file__).resolve().parent.parent


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def part1_folder_import() -> None:
    print("\n[1] 文件夹导入：examples/skills 幂等入库，含新增的 ui_aesthetics")
    loader = SkillLoader(build_storage("memory"))
    imported = await loader.sync_from_dir(ROOT / "examples" / "skills")
    check("ui_aesthetics" in imported, f"新技能包被导入：{sorted(imported)}")

    again = await loader.sync_from_dir(ROOT / "examples" / "skills")
    check(sorted(again) == sorted(imported), "重扫幂等：同名整体覆盖，不报错不重复")

    skills = {s.name: s for s in await loader.discover()}
    ui = skills.get("ui_aesthetics")
    check(ui is not None and ui.display == "界面美化",
          f"frontmatter display 进清单（{ui.display if ui else '缺'}）")
    check(bool(ui.description) and "CAPABILITY" in ui.description,
          "description 完整（触发关键）")
    check(not ui.always and ui.available, "非常驻、依赖满足 → 只进清单不带正文")


async def part2_zip_import() -> None:
    print("\n[2] zip 上传：根上 SKILL.md 归一 + 附件入库")
    loader = SkillLoader(build_storage("memory"))
    skill_md = (ROOT / "examples" / "skills" / "ui_aesthetics" / "SKILL.md").read_bytes()
    data = _make_zip({
        "SKILL.md": skill_md,                       # 根上直接 SKILL.md 的形状
        "references/palette.md": "调色板:主色#2563eb".encode("utf-8"),
    })
    imported = await loader.import_zip("my-skill.zip", data)
    check(imported == ["ui_aesthetics"], f"根形状归一后导入：{imported}")

    sk = await loader.get("ui_aesthetics")
    check(sk is not None and "references/palette.md" in [f["relpath"] for f in sk.files],
          "附件登记进 skill_files")
    body = await loader.read_attachment(sk, "references/palette.md")
    check(b"2563eb" in body, "附件字节可从对象存储读回")


async def part3_zip_guards() -> None:
    print("\n[3] 上传三道防线：类型 / 大小 / zip-slip")
    loader = SkillLoader(build_storage("memory"))

    for label, fn, data in [
        ("非 zip 后缀", "skill.txt", b"hello"),
        ("空内容", "skill.zip", b""),
        ("超大包", "big.zip", b"\x00" * (21 * 1024 * 1024)),
    ]:
        try:
            await loader.import_zip(fn, data)
            check(False, f"{label}（竟未拒收）")
        except ValueError as e:
            check(True, f"{label} 拒收（{e}）")

    slip = io.BytesIO()
    with zipfile.ZipFile(slip, "w") as zf:
        zf.writestr("../evil.md", "越界")
    try:
        await loader.import_zip("evil.zip", slip.getvalue())
        check(False, "zip-slip（竟未拒收）")
    except ValueError as e:
        check("越界" in str(e), f"zip-slip 拒收（{e}）")

    empty = _make_zip({"readme.txt": b"no skill here"})
    check(await loader.import_zip("empty.zip", empty) == [],
          "无 SKILL.md 的包安静跳过，不报错不入库")


async def main() -> None:
    await part1_folder_import()
    await part2_zip_import()
    await part3_zip_guards()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
