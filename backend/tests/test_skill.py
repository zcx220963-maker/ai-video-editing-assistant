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
    LoadSkillTool,
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
            files={"references/long.md":
                   # 每行垫 100 个「填」字：240 行 ≈ 75 KB。分页/聚合断言与大小无关，
                   # 但留一份真正的大附件，能同时测到「大附件」这一路。
                   ("\n".join(f"第 {i} 行。" + "填" * 100 for i in range(1, 241))
                    + "\n").encode()},
        )
        _write_skill(
            root, "editing",
            "name: editing\ndescription: 通用剪辑流程",
            "步骤：load_media → split_shots → ... → render_video。\n\n"
            "<!-- section: bgm-picking -->\n# 配乐怎么选\n配乐只从曲库选。\n<!-- /section -->\n\n"
            "<!-- section: patch-video -->\n# 出片之后怎么改\n改就调 patch_video。\n"
            "<!-- /section -->",
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

            # 风格包不进清单：它不是模型该自己发现的过程技能（用户在前端选、由
            # plan_motion 按名注入），而每沉一套就永久多占一份常驻预算——真机
            # 2026-10-19 沉完一套风格后清单顶穿闸㉓，用户一句话都发不出去。
            await storage.skills.upsert(
                "style_demo", "同一构图、同一光位。", description="参考片沉淀",
                frontmatter={"name": "style_demo", "kind": "style",
                             "style_manifest": {"template": "style_demo"}})
            rendered2 = await src.render("帮我剪个视频")
            check("style_demo" not in rendered2, "风格包不进 <skills> 常驻清单")
            check("<name>github</name>" in rendered2, "滤掉风格包后其余技能照常列出")
            check(len(await loader.discover()) == 4, "discover 仍能读到风格包（只是不列清单）")
            await storage.skills.drop("style_demo")   # 后面的「库里仍是三个技能」按原样对账

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

            # ---- section：只取正文里的一块（§2.2：切入点交还给模型）----
            # 分段键是服务端与技能之间的约定（`<!-- section: film-knobs -->`），模型在
            # 白名单 / schema / 附件清单里都查不到它——所以整份加载时必须给目录，
            # 单取时必须只回那一块（顺手把默认集塞回去就等于没分块）。
            from agent_framework.skill import section_catalog, section_text
            check([n for n, _t, _s in section_catalog(editing.body)] ==
                  ["bgm-picking", "patch-video"],
                  "分段目录按正文原序给出键名")
            check(section_text(editing.body, "bgm-picking").startswith("# 配乐怎么选")
                  and "render_video" not in section_text(editing.body, "bgm-picking"),
                  "section_text 单取一块只回那一块")
            check(section_text(editing.body, "nope") is None,
                  "没有这一块就回 None（不猜、不模糊匹配）")

            r = await reg.execute("load_skill", {"name": "editing", "section": "bgm-picking"})
            check("配乐只从曲库选" in r and "render_video" not in r and "改就调 patch_video" not in r,
                  "load_skill(section=…)：只回那一块，默认集与别的块一个字不捎")
            check("section=bgm-picking" in r and "字节" in r,
                  f"回执写明取的是哪一块、共多少字节：{r[:60]}")
            r = await reg.execute("load_skill", {"name": "editing"})
            check("render_video" in r, "整份就是整份：分好的块也在里面")
            check("bgm-picking（配乐怎么选，" in r and "patch-video（出片之后怎么改，" in r
                  and "section=键名" in r,
                  "整份加载末尾附分段目录（键名 + 小节标题 + 字节 + 取法）")
            r = await reg.execute("load_skill", {"name": "editing", "section": "nope"})
            check(is_tool_error(r) and "bgm-picking" in str(r) and "patch-video" in str(r),
                  f"键名写错：把在案的键名列出来，不猜：{str(r)[:70]}")
            r = await reg.execute("load_skill", {"name": "github", "section": "bgm-picking"})
            check(is_tool_error(r) and "没有分段标记" in str(r),
                  "没有分段标记的技能点名 section：如实说没有可点的块")

            # ---- 小窗口聚合（真机 2026-10-17「薄肌理论」那条 run 的病根）----
            # 病根：模型对着 293 行的 demo 连打 80 枪 `offset=N, limit=1`——每枪都成功、
            # 每枪只回一行，参数级判据一次都不响，一路磨到迭代上限。修法：**工具是唯一
            # 知道这一份多大的地方**，小窗口不放行、替它放大成大段并如实写明；页脚与
            # 说明/提示词里都改教「一次取一大段 / 整份」，不再教「按行续读」。
            from agent_framework.skill import _PAGE_MIN_LINES, _PAGE_STEP_LINES
            check(_PAGE_MIN_LINES < _PAGE_STEP_LINES and _PAGE_MIN_LINES >= 1,
                  f"阈值本身自洽（{_PAGE_MIN_LINES} → {_PAGE_STEP_LINES}）")
            r = await reg.execute("load_skill", {"name": "github", "path": "references/long.md",
                                                 "offset": 2, "limit": 1})
            check("第 2 行。" in r and "第 101 行。" in r and "第 102 行。" not in r,
                  "limit=1 的窗口被放大到 100 行（一次一行读不完一份长文）")
            check("你要的窗口太小（limit=1）" in r
                  and f"已按 {_PAGE_STEP_LINES} 行给你" in r,
                  "放大这件事在页脚**如实写明**——不是静默改行为")
            check("offset=102" in r and "一次取一大段" in r and "整份就不带 offset/limit" in r,
                  "页脚给下一枪的正确形状：大段取 / 整份，而不是一行行续")
            r = await reg.execute("load_skill", {"name": "github", "path": "references/long.md",
                                                 "offset": 2, "limit": 60})
            check("第 2 行。" in r and "第 61 行。" in r and "第 62 行。" not in r,
                  "正常窗口（limit=60）一字不多给——聚合只接住小窗口")
            r = await reg.execute("load_skill", {"name": "github", "path": "references/long.md",
                                                 "offset": 102, "limit": 150})
            check("第 102 行。" in r and "第 240 行。" in r and "**到底了**" in r,
                  "整段取到末尾：页脚照旧给「到底了」的确定信号")
            note = LoadSkillTool._paging_note("s", "填充行\n" * 3000)
            check("别一行一行取" in note and f"≈{_PAGE_STEP_LINES} 行" in note,
                  "整份附的续读话术教「大段取」，不再教「按行续读」")
            desc = reg.get("load_skill").description
            check("别一行一行取" in desc and "整份就不带 offset/limit" in desc,
                  "工具说明里也把正确的续读形状写死了（模型先读的是它）")

            # ---- 小附件的合法窗口真分页、越界才整份兜底 ----
            # 2026-10-17 run 639a8d0c0c8f 曾把小附件一律整份回（折叠后按窗口重扫，
            # 同一份内容几十个别名）。代价在 run 9ff287ee91ab 暴露：模型对 7~24 KB
            # 的附件连换 offset，每次仍从第 1 行开始，同参同结果闸一响就再也拿不到
            # 内容，最后连打 6 轮同样的调用把 run 拖停。现在：窗口合法就真按行给；
            # 只有「越界」这一种错才整份兜底（别让一句错误把唯一的内容挡在外面）。
            from agent_framework.skill import _WHOLE_ATTACHMENT_BYTES
            check(_WHOLE_ATTACHMENT_BYTES > 0, "小附件越界兜底阈值存在")
            r = await reg.execute("load_skill", {"name": "editing",
                                                 "path": "references/steps.md",
                                                 "offset": 2, "limit": 1})
            check("第一步：加载素材" in r and "已整份给你" in r,
                  "小附件 offset 越界：整份兜底，不只是回一句错误")
            check("分页只留给更大的文件" in r,
                  "兜底如实写明（不是静默忽略参数）")
            r = await reg.execute("load_skill", {"name": "editing",
                                                 "path": "references/steps.md"})
            check("第一步：加载素材" in r and "已整份给你" not in r,
                  "不带参数取小附件：一字不多（没加没必要的回执）")
            r = await reg.execute("load_skill", {"name": "github",
                                                 "path": "references/long.md",
                                                 "offset": 2, "limit": 1})
            check("已整份给你" not in r and "你要的窗口太小（limit=1）" in r,
                  "大附件有合法窗口：照旧分页 + 小窗口聚合")

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

            # ---- 大正文只给索引（真机 run 9ff287ee91ab）----
            # 病根：1422 行、60 KB 的正文一次整份回给模型，占掉半份上下文预算；
            # 模型随后按 offset 顺读 32 个窗口才撞上窗口上限，72 轮没交计划。
            # 分段键与行号本来就在标题里——先给目录，正文按节取。
            gen = Path(tmp, "skills3")
            sentinel = "这句只应出现在 section 单取里。"
            _write_skill(
                gen, "huge",
                "name: huge\ndescription: 大正文技能",
                ("<!-- section: deep -->\n# 深处\n" + sentinel +
                 "\n<!-- /section -->\n\n" + "填" * 25_000 + "\n"),
                files={"notes.md": ("第 1 行\n第 2 行\n第 3 行\n第 4 行\n第 5 行\n").encode()},
            )
            await loader.sync_from_dir(gen)
            r = await reg.execute("load_skill", {"name": "huge"})
            check(sentinel not in r, "正文超过 24 KB：整份加载不再把正文整个倒出来")
            check("只给了上面的目录" in r and "section=键名" in r,
                  f"回执说清这次只给目录、正文按节取：{r[-120:]}")
            check("deep（深处，" in r, "分段目录随索引一起给（不等模型去猜键名）")
            r = await reg.execute("load_skill", {"name": "huge", "section": "deep"})
            check(sentinel in r, "按 section 能真拿到那一节（索引不是死路）")
            r = await reg.execute("load_skill", {"name": "huge", "path": "notes.md",
                                                 "offset": 3, "limit": 60})
            check("第 3 行" in r and "第 2 行" not in r and "第 5 行" in r,
                  f"小附件带合法 offset：真从第 3 行给（不再忽略参数回整份）：{r[:60]}")
            check("到底了" in r, "小附件分页也有「到底了」的确定信号")

            # 注入到 ContextBuilder
            b = ContextBuilder("BASE", context_sources=[src], runtime_context=None)
            msgs = await b.build(Session(user_id="u", conversation_id="c"), "剪视频")
            check("<skills>" in msgs[0]["content"], "System 含 <skills> 清单")

            # ---- T43/Q70：<skills> 只许包一层，正文与清单同级 ----
            # 这两支的红法就是改前的形态：出口按 source.name 再包一次，于是成品里
            # `<skills>` 出现 2 次，且 `<skill_instructions>` 被吞成 `<skills>` 的子节点。
            # 判据只看首字符（自带标签的段不再包），所以这里也照**文本**断言，不解析结构。
            content = msgs[0]["content"]
            check(content.count("<skills>") == 1 and content.count("</skills>") == 1,
                  f"<skills> 开/闭标签各恰好 1 次（实得 {content.count('<skills>')} / "
                  f"{content.count('</skills>')}）")
            check("</skills>\n\n<skill_instructions" in content,
                  "<skill_instructions> 与 <skills> 同级（排在 </skills> 之后，不是它的子节点）")
            check(content.index("</skills>") < content.index("<skill_instructions"),
                  "清单在前、常驻正文在后（顺序没被包装规则改掉）")
        finally:
            await storage.close()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
