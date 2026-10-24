"""技能**清单**的形状：一句话摘要、结构上限、放不下的转 find_skill（2026-10-22）。

为什么单开一支：`<skills>` 是每轮无条件发的常驻块，它的长度直接决定闸㉓ 会不会
把用户的请求挡在门外。真机账（2026-10-22）：用户从外部装了 7 份技能，它们的
description 本身就是整段提示词，清单从 3,156 B 涨到 9,408 B，顶穿预算，**一句话都
发不出去**。抬预算只是把墙往后挪；这一支钉住的是**新形状**：

- 清单里每条只放一句话（`summary:` 或 description 第一句，单条 ≤160 B）；
- 整段 description 不再丢——`load_skill` 的抬头会把它补回来；
- 整块有结构上限，超出的技能不进清单，但 `find_skill` 搜得到、`load_skill` 取得到；
- 顺序是确定的（always 在前，再按名字），否则每轮清单都在动，前缀缓存整段作废。

运行：  python tests/test_skill_catalog.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.skill import (
    _CATALOG_MAX_BYTES,
    _CATALOG_SUMMARY_BYTES,
    FindSkillTool,
    LoadSkillTool,
    Skill,
    SkillLoader,
    SkillManifestContextSource,
    register_skill_tools,
)
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


LONG_DESC = (
    "把一段口播做成某种画风的动画视频。"
    "细则一：分镜要先出表，七栏齐全，每镜一个主体一个动作，不许用卡片代替画面。"
    "细则二：写码前先选模板，模板里的常量一个都不许改，只换 props。"
    "细则三：渲染前先出 15%/50%/85% 三张静帧，人看过再烧整片。"
)


def case_summary() -> None:
    print("\n① 一句话摘要：summary: 优先 → description 第一句 → 按字节截断带「…」")
    skill = Skill(name="a", description=LONG_DESC, always=False, location="skills/a/SKILL.md",
                  body="")
    got = skill.catalog_summary()
    check(got == "把一段口播做成某种画风的动画视频。",
          f"没写 summary：取第一句（实得「{got}」）")
    check(len(got.encode()) < len(LONG_DESC.encode()), "清单那一行比整段短")

    explicit = Skill(name="a", description=LONG_DESC, always=False,
                     location="skills/a/SKILL.md", body="", summary="一句话说清干什么")
    check(explicit.catalog_summary() == "一句话说清干什么",
          "写了 summary：以它为准（整段细则不进清单）")

    # 缩写里的点不是句末：真机上切出过「library (e.g.」这种半截
    en = Skill(name="a", description=(
        "Produce a short film in one of the styles of the library (e.g. noir). "
        "More detail follows."), always=False, location="l", body="")
    check(en.catalog_summary().endswith("(e.g. noir)."), f"缩写不截句：{en.catalog_summary()}")

    # 第一句本身就超长：按 UTF-8 字节截，不能切出半个汉字
    fat = Skill(name="a", description="字" * 500, always=False, location="skills/a",
                body="")
    cut = fat.catalog_summary()
    check(len(cut.encode()) <= _CATALOG_SUMMARY_BYTES,
          f"超长句按字节截到上限内（{len(cut.encode())} ≤ {_CATALOG_SUMMARY_BYTES}）")
    check(cut.endswith("…"), "截断处带省略号（模型才知道后面还有）")
    check("�" not in cut, "没有切出半个字符")


def case_manifest_and_load() -> None:
    print("\n② 清单只放摘要；整段 description 随 load_skill 补回来")

    class _Loader:
        def __init__(self, skill: Skill) -> None:
            self._s = skill

        async def discover(self, user_id=None):  # noqa: ARG002
            return [self._s]

        async def get(self, name):
            return self._s if name == self._s.name else None

        async def read_attachment(self, *a, **kw):  # noqa: ARG002
            raise AssertionError("这支不该读附件")

    skill = Skill(name="talk", description=LONG_DESC, always=False,
                  location="skills/talk/SKILL.md", body="正文第一行\n")
    rendered = asyncio.run(SkillManifestContextSource(_Loader(skill)).render(""))
    check("把一段口播做成某种画风的动画视频。" in rendered, "清单一句话在")
    check("细则一" not in rendered, "整段细则**不**进常驻清单")
    out = asyncio.run(LoadSkillTool(_Loader(skill)).execute("talk"))
    check("细则一" in out, "整段 description 在 load_skill 的抬头里（没丢）")


def case_cap_and_find() -> None:
    print("\n③ 结构上限：超出的不进清单，但 find_skill 搜得到、load_skill 取得到")

    async def run() -> None:
        storage = build_storage("memory")
        await storage.start()
        try:
            # 每份技能的清单块约 300 B（名字 + 160 B 摘要 + location），上限放得下
            # 十几份；这里一口气装 40 份「无关」技能，把清单顶过上限。
            for i in range(40):
                await storage.skills.upsert(
                    f"filler-{i:02d}", "正文",
                    description=f"第 {i} 份填充技能，用来把清单顶过结构上限。",
                    frontmatter={"name": f"filler-{i:02d}"})
            # 一份**被藏起来**的真技能：名字排在填充技能后面，注定进不了清单。
            await storage.skills.upsert(
                "zz-tail", "正文", description="没列出来也要能搜到：篝火与剪影的年代转场。",
                frontmatter={"name": "zz-tail"})
            await storage.skills.upsert(
                "style_demo", "风格正文", description="风格包不该出现",
                frontmatter={"name": "style_demo", "kind": "style"})
            loader = SkillLoader(storage)
            rendered = await SkillManifestContextSource(loader).render("")
            size = len(rendered.encode())
            check(size <= _CATALOG_MAX_BYTES + 400,
                  f"清单不超过结构上限 + 提示行（{size} ≤ {_CATALOG_MAX_BYTES} + 400）")
            check("skills_not_listed" in rendered, "超出上限时明确写出「还有 N 份没列」")
            check("find_skill" in rendered, "提示行指向 find_skill（不是死路）")
            check("<name>zz-tail</name>" not in rendered, "排到上限之外的那份确实没列")
            check("style_demo" not in rendered, "风格包仍不进清单")

            reg = ToolRegistry()
            register_skill_tools(reg, loader)
            find = reg.get("find_skill")
            check(find is not None, "find_skill 与 load_skill 一起注册")
            check(find.concurrency_safe, "find_skill 只读，可并发")
            hit = await find.execute("篝火 剪影", limit=3)
            check("zz-tail" in hit, f"清单里没有的技能，全靠 find_skill 找回来：{hit[:60]}")
            got = await load_skill_by_name(loader, "zz-tail")
            check("正文" in got, "找到之后 load_skill 照常取正文")

            empty = await find.execute("，， 。。", limit=3)
            check(empty.startswith("Error:"), "没有可用检索词时给一条人话错误")
            miss = await find.execute("量子纠缠 常温超导", limit=3)
            check("没有和" in miss and "filler-00" in miss,
                  "搜不到时把在案的名字列出来（不猜、不愣住）")
            limited = await find.execute("填充", limit=99)
            check(limited.count("\n- ") <= 20, "limit 夹在 20 以内")
        finally:
            await storage.close()

    asyncio.run(run())


async def load_skill_by_name(loader, name: str) -> str:
    return await LoadSkillTool(loader).execute(name)


def case_style_discoverable() -> None:
    print("\n③′ 风格包不从清单进来，但 find_skill 搜得到（自动入口的唯一入口）")

    async def run() -> None:
        storage = build_storage("memory")
        await storage.start()
        try:
            await storage.skills.upsert(
                "style_silhouette_history", "剪影穿过层叠时代背景，一块发光矩形贯穿全片。",
                description="剪影年轮：侧向行走的剪影 + 分层背景 + 发光矩形。",
                frontmatter={"name": "style_silhouette_history", "kind": "style",
                             "display": "剪影年轮"})
            await storage.skills.upsert(
                "my_import", "导入画面契约的正文。",
                description="我从外面装的一套动画说明书。",
                frontmatter={"name": "my_import", "kind": "workflow", "display": "我的导入技能"},
                owner_user_id="u1")
            loader = SkillLoader(storage)
            rendered = await SkillManifestContextSource(loader).render("", user_id="u1")
            check("style_silhouette_history" not in rendered,
                  "风格包不进常驻清单（每沉淀一套就永久多占预算，见该类的注释）")

            find = FindSkillTool(loader)
            hit = await find.execute("剪影 年轮", limit=5)
            check("style_silhouette_history" in hit,
                  f"按关键词搜得到风格包：{hit[:80]}")
            check("风格包" in hit, "命中的那条标着「风格包」——模型据此知道该填 style_skill")
            check("不用取正文" in hit,
                  "只对命中契约的那一轮提示「不必取正文」（正文几万字，规划轮用不上）")

            imported = await find.execute("导入 动画", limit=5)
            check("my_import" in imported and "导入技能" in imported,
                  f"用户导入的技能同样标出来：{imported[:80]}")
        finally:
            await storage.close()

    asyncio.run(run())


def case_order() -> None:
    print("\n④ 顺序确定：always 在前，其余按名字（前缀缓存不作废）")

    async def run() -> None:
        storage = build_storage("memory")
        await storage.start()
        try:
            for name, always in (("m-zeta", False), ("c-alpha", False), ("n-always", True)):
                await storage.skills.upsert(
                    name, "正文", description=f"{name} 的说明",
                    frontmatter={"name": name, "always": "true" if always else ""})
            loader = SkillLoader(storage)
            one = await SkillManifestContextSource(loader).render("")
            two = await SkillManifestContextSource(loader).render("")
            check(one == two, "同一份数据两次装配逐字相同（顺序不随查询漂）")
            names = [ln.strip().removeprefix("<name>").removesuffix("</name>")
                     for ln in one.splitlines() if "<name>" in ln]
            check(names[:2] == ["n-always", "c-alpha"], f"always 在前、其余按名字：{names}")
        finally:
            await storage.close()

    asyncio.run(run())


def main() -> None:
    print("技能清单的形状：摘要 / 结构上限 / find_skill")
    case_summary()
    case_manifest_and_load()
    case_cap_and_find()
    case_style_discoverable()
    case_order()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
