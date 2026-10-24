"""镜头配方卡与知识文档：把**库里所有 skill 的附件**接进写手 prompt。

## 为什么要有它（2026-10-23）

`demos/` 下躺着 165 个动效件、`shots/` 下躺着 136 篇配方卡（每篇 2.5~5 KB，写清
「这个镜头怎么拍、参数取多少、有哪些坑」），但 `SKILL.md` 第 11 行是
`exclude: demos/, shots/` —— **全被排除在技能附件之外**（2026-10-18 的决定，理由
正当：一条 run 光 demo 就读 53 份、还编出不存在的名字卡死）。

结果是写手只看到正文的**原则**（要成组、用 spring、要隔开），没有一条**手法**。
实测后果：s01 / s09 被闸⑭ 数出 **0 个变化段**——整镜是一张静帧。

而用户从 GitHub 导入的 7 个 skill（`video-shotcraft` 1011 文件 / 6.5 MB 等）**写手一个
都看不到**：`shot_writer._contract()` 只读 `_SKILL_DIR/SKILL.md` 一个文件。

## 数据源：库里所有 skill 的附件（不是某个目录）

`SkillLoader.discover()` → `read_attachment()`。这样**导入一个新 skill 就自动生效**，
不需要为它改代码。

附件分两类进来：

| 类型 | 判据 | 进哪 |
|---|---|---|
| **配方卡** | `.md` + frontmatter 有 `name` 且有 `一句话` 或 `适用` | 主目录（带摘要，参与分配） |
| **知识文档** | `.md` + 路径含 `knowledge/` `references/` `styles/`，且不是 README/CHANGELOG | 兜底目录（标题 + 前 120 字） |
| 其余 | README / CHANGELOG / 脚本 / 素材 | 不进 |

**为什么要第二类**：实测 `dashi-animationstyle/knowledge/动效和画面变化.md`（11,593 B）
是全库最有价值的一份动效方法论（「每个镜头写清四件事：进场 → 主动作 → 次要动作 →
出场或转场」，带盲测判例），但它**没有 frontmatter**——只按配方卡判据会漏掉它。

## 为什么匹配必须靠模型

第一版试过纯规则（目录加分 + 中文二元组重叠），实测**全错**：
「真空腔与电子云」→「列表堆叠」、「跳动的心脏」→「极光背景翻转」、
「一万个火柴人」→「冲击反馈」。

根因不是参数没调好：**配方卡写的是「手法」（示波流线、点阵重组、轴爆表），
镜的 `subject` 写的是「题材」（心脏、电子云、火柴人）**。两套词汇天然不重叠，
只有语义理解能把「心脏要有体积」连到「3D 场景 + 材质 + 接触阴影」那一篇。

所以：`catalog()` 出目录，`assign()` 用**一次模型调用**做分配，`block()` 只负责取段落。
**分配失败就不塞**——配错一篇比不给更糟，写手会照错的手法画。

## 上限（写在这里，免得以后重新推）

catalog 按条数封顶（`MAX_CATALOG_ITEMS`）。现在库里 261 篇配方卡，离上限还有余量；
到 800 篇以上，关键词/目录粗筛会开始丢好配方，那时要换成 embedding 检索。
但**模型 context 不是瓶颈**（128K tokens ≈ 40 万中文字），真瓶颈是注意力稀释。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import code_spec as cspec

#: 单篇塞进 prompt 的字符上限。最长的一篇 6 KB，截到 3.6 KB 仍保留「动效核心 + 参数表」。
MAX_RECIPE_CHARS = 3600
#: 知识文档只给「标题 + 前 N 字」——它们没有 frontmatter，全文太长且结构不一。
KNOWLEDGE_HEAD_CHARS = 600
#: catalog 条数上限。**这是硬边界**：超过就截断并在末尾注明还有多少篇没列出。
#: 现在全库 249（外部配方卡）+ 136（内置）+ 126（知识）≈ 511 条，接近上限；
#: 到 800 篇以上该换 embedding 检索了（见模块 docstring 末段）。
MAX_CATALOG_ITEMS = 520
#: 知识文档进目录的条数上限（它们只有标题，没有摘要，多了会淹没配方卡）。
MAX_KNOWLEDGE_ITEMS = 60
#: catalog 里每条「一句话」截到多少字。
CATALOG_LINE_CHARS = 70
#: 每份 skill 收多少条附件进目录（2026-10-24 起**对目录结构零假设**，只按配额）。
#: 实际配额是**动态的**：`(MAX_CATALOG_ITEMS - 内置条数) // skill 份数`，
#: 再用这个值封顶——份数少时每份能看见更多文件，份数多时自动摊薄，
#: 不会因为导入了几份大 skill 就把目录撑爆。
MAX_FILES_PER_SKILL = 28
#: 每份至少给几条（低于这个数就没意义了，目录会只剩大 skill）。
MIN_FILES_PER_SKILL = 6
#: 分配阶段允许模型「先要文件看内容」的轮数（子 agent 式按需读）。
MAX_READ_ROUNDS = 2
#: 单个文件读给模型看的字符上限。
#: 2026-10-24 从 2500 提到 4000：一个 `.tsx` 示例常在 5000 字上下，
#: 2500 只能看到一半，写手还得再读一次——而多读一轮的代价比多给 1500 字大。
READ_CHARS = 4000
#: 小于这个字数的附件不进（多是 re-export 壳子）。
_MIN_FILE_CHARS = 300
#: 代码类附件进 prompt 的字符上限。
_CODE_CHARS = 2600
#: 算「文本」的扩展名——**不判断它在哪个目录**，只看能不能读成文本。
#: 这是兼容性的关键：`dashi` 的画法在 `.html`、`huashu` 在自定义目录，
#: 按目录名或按 `.tsx` 白名单筛，每导入一份新 skill 就要回来补一次规则。
_TEXT_EXTS = (".md", ".mdx", ".txt", ".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs",
              ".html", ".htm", ".css", ".py", ".sh", ".json", ".yaml", ".yml",
              ".glsl", ".frag", ".vert", ".svg")
#: 排序优先级：越小越先收。md（说明书/方法论）> 组件代码 > 普通脚本 > 其他文本。
_EXT_PRIO = {".md": 0, ".mdx": 0, ".tsx": 1, ".jsx": 1, ".js": 2, ".mjs": 2,
             ".ts": 2, ".cjs": 2, ".html": 3, ".htm": 3}
#: 代码类扩展名 → 代码块语言标记。空串＝当文档处理（只取开头）。
_EXT_LANG = {".tsx": "tsx", ".jsx": "jsx", ".ts": "ts", ".js": "js",
             ".mjs": "js", ".cjs": "js", ".html": "html", ".htm": "html",
             ".css": "css", ".py": "python", ".sh": "bash", ".json": "json",
             ".glsl": "glsl", ".frag": "glsl", ".vert": "glsl"}
#: 从附件里抽「第一句可读描述」：注释 / 标题 / 第一行非代码文本。
_HINT_RE = re.compile(r"^\s*(?://|/\*+|\*|#)\s*(\S.{0,100})$", re.M)


def _stem_of(rel: str) -> str:
    return rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _first_hint(text: str, limit: int = CATALOG_LINE_CHARS) -> str:
    """附件的一句话描述。注释优先，其次标题，最后第一行非代码文本。"""
    for m in _HINT_RE.finditer(text[:800]):
        cand = m.group(1).strip()
        if len(cand) >= 4 and not cand.startswith(
                ("eslint", "@ts-", "noqa", "!", "type ", "source ")):
            return cand[:limit]
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if not s or s.startswith(("!", "<", "import ", "export ", "{", "}",
                                  "const ", "function ", "def ", "class ")):
            continue
        if len(s) >= 4:
            return s[:limit]
    return ""


#: 只取这几段（配方卡正文）。
_KEEP_SECTIONS = ("意图", "动效核心", "三式选型", "节奏", "参数表", "怎么做", "手法")
#: 知识类附件所在的目录前缀。
_KNOWLEDGE_DIRS = ("knowledge/", "references/", "styles/")
#: 这些前缀/文件名一律不进：仓库文档与展示素材说明，没有画面价值。
_SKIP_NAMES = re.compile(r"^(readme|changelog|contributing|license|notice)", re.I)
_SKIP_DIRS = ("assets/",)


@dataclass(frozen=True)
class Recipe:
    name: str
    one_liner: str
    fit: str
    skill: str
    relpath: str
    body: str
    kind: str = "recipe"          # recipe | doc | code
    lang: str = ""                # kind=code 时的代码块语言（tsx/js/html/…）


@dataclass
class _Cache:
    key: Any = None
    recipes: tuple[Recipe, ...] = ()
    knowledge: tuple[Recipe, ...] = ()
    by_name: dict[str, Recipe] = field(default_factory=dict)
    #: 每份 skill 一行「名字｜描述」——Agent Skills 的 Level 1（metadata 常驻）。
    #: 有了它，模型才能**自己发现**有哪份技能可用，而不是靠用户原话里恰好提到名字。
    skill_lines: tuple[str, ...] = ()


_CACHE = _Cache()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """极简 frontmatter：`key: value` 逐行读，不做 YAML。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            meta[k] = v
    return meta, body.lstrip("\n")


def _pick_sections(body: str) -> str:
    """按 `## 标题` 切，只留 `_KEEP_SECTIONS` 里那几段。"""
    parts = re.split(r"(?m)^##\s+", body)
    kept: list[str] = []
    for chunk in parts[1:]:
        title = chunk.split("\n", 1)[0].strip()
        if any(k in title for k in _KEEP_SECTIONS):
            kept.append("## " + chunk.rstrip())
    text = "\n\n".join(kept).strip()
    if not text:
        text = body.strip()[:2000]
    if len(text) > MAX_RECIPE_CHARS:
        text = text[:MAX_RECIPE_CHARS].rstrip() + "\n…（这一篇更长，这里截断了）"
    return text


def _title_of(body: str) -> str:
    """知识文档没有 frontmatter，用第一个 `# 标题` 当名字。"""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:40]
    return ""


def _load_builtin() -> list[Recipe]:
    """扫内置 `_SKILL_DIR/shots/**/*.md`。

    **为什么内置目录要单独扫**：`motion_explainer` 的 136 篇配方卡被 SKILL.md 的
    `exclude: demos/, shots/` 排除了，所以它们**不在 `skill_files` 表里**——只扫库里
    附件会漏掉这 136 篇（它们与当前管线最贴，中文、按本产品的闸写）。
    """
    root = cspec._SKILL_DIR / "shots"
    out: list[Recipe] = []
    try:
        paths = sorted(root.rglob("*.md"))
    except OSError:
        return out
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _parse_frontmatter(text)
        name = str(meta.get("name") or "").strip()
        if not name or not (meta.get("一句话") or meta.get("适用")):
            continue
        out.append(Recipe(
            name=name,
            one_liner=str(meta.get("一句话") or "").strip(),
            fit=str(meta.get("适用") or "").strip(),
            skill="motion_explainer",
            relpath="shots/" + p.relative_to(root).as_posix(),
            body=body))
    return out


async def load(storage: Any, user_id: str | None = None) -> None:
    """扫**内置目录 + 库里所有 skill 的附件**，把配方卡与知识文档装进进程内缓存。

    幂等、可重复调用：缓存键含内置目录 mtime 与每个 skill 的 `(name, 文件数, 总字节)`，
    变了才重扫。**导入新 skill 会自动让键变化**，不需要重启或改代码。

    MinIO 逐个读：431 个 md 约 2 秒。一次 plan 只跑一次。
    """
    from agent_framework.skill import SkillLoader

    try:
        builtin_mtime = (cspec._SKILL_DIR / "shots").stat().st_mtime
    except OSError:
        builtin_mtime = 0
    loader = SkillLoader(storage)
    try:
        skills = await loader.discover(user_id)
    except Exception:      # noqa: BLE001 —— 读不到就当没有，不让它带塌写镜
        skills = []
    key = (builtin_mtime,) + tuple(sorted(
        (s.name, len(s.files or ()), sum(int(f.get("bytes") or 0) for f in (s.files or ())))
        for s in skills))
    if _CACHE.key == key and _CACHE.recipes:
        return

    recipes: list[Recipe] = list(_load_builtin())      # 内置优先（同名时留下它）
    seen: set[str] = {r.name for r in recipes}
    skills = sorted(skills, key=lambda s: s.name)

    # ---- 附件索引：**对 skill 的内部结构零假设**（2026-10-24 重写）----
    #
    # 上一版是"认目录 + 认扩展名"：只收 `knowledge/|references/|styles/` 下的 `.md`，
    # 外加 `.tsx` 示例。结果每导入一份新 skill 就要回来补一次规则——`dashi` 的画法在
    # `.html` 里、`huashu` 在自定义目录里，全都漏掉。
    #
    # 现在照 Agent Skills 规范来（`SKILL.md` 只要求 `name` + `description`，
    # 附件由 agent 自己 `cat`，对目录结构零假设）：**凡是文本文件都进目录**，
    # 只按「信息密度」排序后给每份一个配额，**相关性交给 `assign()` 那一次模型调用判**。
    # 我们不是 agent、没有 bash 循环，所以"按需 cat"这一步换成"先出全量目录、再取被挑中的"。
    per_skill: list[Recipe] = []
    # 动态配额：把「目录上限 − 内置」按 skill 份数平分，再用 MAX_FILES_PER_SKILL 封顶。
    # 这样「导入几份就能看见多少」是自动的，不需要每次回来调常数。
    _room = max(0, MAX_CATALOG_ITEMS - len(recipes))
    quota = max(MIN_FILES_PER_SKILL,
                min(MAX_FILES_PER_SKILL, _room // max(1, len(skills))))
    for sk in skills:
        cands: list[tuple[int, int, str]] = []
        for f in (sk.files or ()):
            rel = str(f.get("relpath") or "")
            low = rel.lower()
            if not low.endswith(_TEXT_EXTS):
                continue                       # 二进制/图片：写手看不了，不进
            if rel.startswith(_SKIP_DIRS) or _SKIP_NAMES.match(rel.rsplit("/", 1)[-1]):
                continue
            ext = "." + low.rsplit(".", 1)[-1]
            cands.append((_EXT_PRIO.get(ext, 4), -int(f.get("bytes") or 0), rel))
        cands.sort()
        # 文档与代码**交替取**：纯按优先级排的话，`.md` 会把配额吃满
        # （`video-shotcraft` 有 1011 个文件，其中大量 md），代码卡一张也剩不下。
        # 交替是通用规则，不看具体是哪份 skill。
        docs = [c for c in cands if _EXT_LANG.get("." + c[2].lower().rsplit(".", 1)[-1], "")]
        codes = [c for c in cands if not _EXT_LANG.get("." + c[2].lower().rsplit(".", 1)[-1], "")]
        ordered: list[tuple[int, int, str]] = []
        while len(ordered) < len(cands):
            for pool in (docs, codes):
                if pool:
                    ordered.append(pool.pop(0))
        cands = ordered

        taken = 0
        for _prio, _neg, rel in cands:
            if taken >= quota:
                break
            try:
                raw = await loader.read_attachment(sk, rel)
            except Exception:      # noqa: BLE001 - 读不到就跳过，不拦分镜
                continue
            text = raw.decode("utf-8", "replace")
            if len(text.strip()) < _MIN_FILE_CHARS:
                continue                       # 太短的多半是壳子 / re-export
            low = rel.lower()
            ext = "." + low.rsplit(".", 1)[-1]

            if ext in (".md", ".mdx"):
                meta, body = _parse_frontmatter(text)
                # 配方卡判据放宽到**规范本身**：`name` + 任一描述字段。
                # 旧版要求 `一句话`/`适用`，于是所有按 Claude 规范写的 skill 全被漏掉。
                one = str(meta.get("一句话") or meta.get("description") or "").strip()
                fit = str(meta.get("适用") or "").strip()
                if meta.get("name") and one:
                    nm = str(meta["name"]).strip()
                    if nm in seen:
                        continue
                    seen.add(nm)
                    per_skill.append(Recipe(name=nm, one_liner=one[:CATALOG_LINE_CHARS],
                                            fit=fit, skill=sk.name, relpath=rel,
                                            body=body, kind="doc", lang="md"))
                    taken += 1
                    continue
                title = _title_of(body) or rel.rsplit("/", 1)[-1]
                nm = f"{sk.name}:{title}"
                if nm in seen:
                    continue
                seen.add(nm)
                per_skill.append(Recipe(name=title, one_liner="", fit="",
                                        skill=sk.name, relpath=rel, body=body,
                                        kind="doc", lang="md"))
                taken += 1
                continue

            lang = _EXT_LANG.get(ext, "")
            nm = f"{sk.name}:{_stem_of(rel)}"
            if nm in seen:
                continue
            seen.add(nm)
            # 脚本类（.py/.sh）**明确标成不能执行**：规范里它们的价值是「跑出来的结果」，
            # 我们不是 agent、没有 bash 循环，跑不了；留着只是让模型知道有这件东西。
            if ext in (".py", ".sh"):
                hint = f"{ext[1:]} 脚本（本环境不能执行，仅供参考）"
            else:
                hint = _first_hint(text) or f"参考文件（{rel}）"
            per_skill.append(Recipe(
                name=nm, one_liner=hint, fit="", skill=sk.name, relpath=rel,
                body=text[:_CODE_CHARS if lang else KNOWLEDGE_HEAD_CHARS],
                kind="code" if lang else "doc", lang=lang))
            taken += 1

    # 按 skill 轮流，避免大 skill（1011 个文件）把目录吃满
    recipes.extend(_interleave(per_skill))

    _CACHE.key = key
    _CACHE.recipes = tuple(recipes)
    # 2026-10-24：知识文档不再是单独一档（统一进 recipes，kind="doc"）。
    # 这个字段留着只为兼容外部读取方，恒为空。
    _CACHE.knowledge = ()
    _CACHE.by_name = {r.name: r for r in recipes}

    # Level 1：每份 skill 的 name + description（规范里这两个字段就是给人/模型判断
    # 「这份技能是干什么的、什么时候该用」的唯一依据）。极短，常驻不吃预算。
    lines: list[str] = []
    for sk in skills:
        fm = getattr(sk, "frontmatter", None) or {}
        desc = str(fm.get("description") or "").strip().replace("\n", " ")
        if not desc:
            body = str(getattr(sk, "body", "") or "")
            desc = next((ln.strip("# ").strip() for ln in body.splitlines()
                         if ln.strip() and not ln.startswith("#")), "")
        if desc:
            lines.append(f"{sk.name}｜{desc[:110]}")
        else:
            lines.append(str(sk.name))
    _CACHE.skill_lines = tuple(lines)


def _interleave(recipes: Sequence["Recipe"]) -> list["Recipe"]:
    """按 skill 轮流取，保证**每一份导入的 skill 都能露头**。

    为什么需要（2026-10-24 实测）：知识文档有 132 篇、目录只列前 60，而它们原先按
    skill 名字母序累积——`huashu-art-motion`(47 篇) + `dashi-animationstyle`(13 篇)
    就把 60 个名额吃满了，`procedural-3d-realism` 那 6 篇（含讲 bloom / 色调映射的
    `references/postprocessing.md`）**全被挤掉**，写手一条光效文档都看不到。
    按 skill 轮转之后，60 个名额平均落到每一份上，谁也不会被另一份挤没。
    """
    buckets: dict[str, list[Recipe]] = {}
    for r in recipes:
        buckets.setdefault(r.skill or "", []).append(r)
    order = list(buckets)
    out: list[Recipe] = []
    depth = 0
    while True:
        added = False
        for k in order:
            bucket = buckets[k]
            if depth < len(bucket):
                out.append(bucket[depth])
                added = True
        if not added:
            break
        depth += 1
    return out


def catalog() -> str:
    """给模型看的目录，**分两档**：

    1. **配方卡**（`名字｜一句话`）—— 一条镜头怎么拍，这是主料，全给；
    2. **知识文档**（`[知识] 标题`）—— 方法论（「一帧先行，再让它动」「每个镜头写清
       四件事」），只有标题、没有摘要：它们太长，而标题本身已经足够让模型判断相关性。

    超过 `MAX_CATALOG_ITEMS` 就截断并在末尾注明还剩多少——**这是明确的上限行为**，
    不是静默丢弃。
    """
    lines: list[str] = []
    # 2026-10-24：不再分「配方卡 / 知识文档」两档——附件索引对目录结构零假设之后，
    # 每条都有「名字 + 一句话」，统一列成一张表。相关性由 `assign()` 那次模型调用判。
    for r in _CACHE.recipes:
        # 第三段是**可点名的路径**：模型光看一句话判断不了时，可以按它要原文。
        lines.append(f"{r.name}｜{r.one_liner[:CATALOG_LINE_CHARS]}"
                     f"｜{r.skill}:{r.relpath}")
    if not lines:
        return ""
    total = len(lines)
    if total > MAX_CATALOG_ITEMS:
        lines = lines[:MAX_CATALOG_ITEMS]
        lines.append(f"…（另有 {total - MAX_CATALOG_ITEMS} 篇未列出：目录到上限了）")
    return "\n".join(lines)


def names() -> tuple[str, ...]:
    return tuple(_CACHE.by_name)


_SYSTEM = """你是这条片的剪辑指导。你只做一件事：给每一镜挑一篇「镜头手法配方卡 / 知识文档」。

判断依据是**这一镜要画什么、怎么动**，不是题材名字像不像：

· 「一束光从地球飞到月球，停在一截」→ 讲「位移 / 轨迹 / 相机推进」的那篇
· 「四行读数依次长出，然后一起疯转停住」→ 讲「错峰入场 / 数字滚动」的那篇
· 「一万个小人做同一个动作」→ 讲「点阵 / 粒子 / 大量小元素」的那篇
· 「心脏收缩两次，要有厚度」→ 讲「主体呼吸 / 材质 / 接触阴影」的那篇
· 「一根刻度线从短长到长，然后数字倒数」→ 讲「描线生长 / 数字滚动」的那篇

**没有合适的就留空。** 配错一篇比不配更糟：写手会照错的手法画，而它没有时间分辨。

只回一个 JSON，不要解释：{"s01": ["名字"], "s02": [], ...}
每镜最多一篇；名字必须逐字来自目录。

**光看目录判断不了时，可以先要文件看内容**（最多两轮）：
只回 {"read": ["技能名:相对路径", ...]}（最多 6 个），我会把这些文件的正文给你，
你读完再给分配结果。路径必须逐字来自下面「文件清单」。
不确定就别读——读文件要花钱，且会占掉你自己的上下文。"""


async def list_files(storage: Any, user_id: str, *, skill: str = "",
                     prefix: str = "", limit: int = 60) -> str:
    """列出某份技能里的文件路径（写手的「翻目录」工具）。

    为什么要它：`readable_lines` 每镜只列 24 条，而 `video-shotcraft` 有 1011 个文件。
    没有列目录的能力，写手就只能读我们挑的那 24 条——那还是「我们筛过的」。
    有了它，写手可以自己说「列出 demos/effects/ 下的文件」，再决定读哪个。
    """
    from agent_framework.skill import SkillLoader

    loader = SkillLoader(storage)
    try:
        skills = await loader.discover(user_id or None)
    except Exception:      # noqa: BLE001
        return ""
    want = str(skill or "").strip()
    pre = str(prefix or "").strip().lstrip("/")
    out: list[str] = []
    for sk in sorted(skills, key=lambda s: s.name):
        if want and sk.name != want:
            continue
        for f in (sk.files or ()):
            rel = str(f.get("relpath") or "")
            if not rel.lower().endswith(_TEXT_EXTS):
                continue
            if pre and not rel.startswith(pre):
                continue
            out.append(f"{sk.name}:{rel}")
            if len(out) >= limit:
                return "\n".join(out) + f"\n…（还有更多，用更具体的 prefix 缩小）"
    return "\n".join(out)


def skill_lines() -> str:
    """Level 1 清单：每份 skill 一行「名字｜描述」。写手据此自己判断要不要用哪份。"""
    return "\n".join(_CACHE.skill_lines)


def readable_lines(skills: Sequence[str], limit: int = 24) -> str:
    """给写手看的「可以点名读的文件」清单（只列已挂载那几份 skill 的）。

    为什么要有这一段：写手有了读文件的工具，还得知道**能读什么**。全库 257 条
    每镜都列一遍太贵，所以只列这一片挂上的那 1~3 份 skill、各取前若干条。
    """
    want = {str(s).strip() for s in (skills or []) if str(s).strip()}
    if not want:
        return ""
    out: list[str] = []
    for r in _CACHE.recipes:
        if r.skill not in want:
            continue
        out.append(f"{r.skill}:{r.relpath}｜{r.one_liner[:48]}")
        if len(out) >= limit:
            break
    return "\n".join(out)


async def read_paths(storage: Any, user_id: str, paths: Sequence[str]) -> str:
    """公开入口：把 `技能名:相对路径` 读成一段可读文本（给写手的读文件工具用）。"""
    return await _read_requested(storage, user_id, paths)


async def _read_requested(storage: Any, user_id: str,
                         paths: Sequence[str]) -> str:
    """把模型点名的 `技能名:相对路径` 读出来，拼成一段可读文本。

    这是「子 agent 按需读 skill」那一步：写手是一次性调用、没有工具循环，
    所以把「读文件」放在分配阶段做——先看目录，判断不了再要文件，读完再分配。
    """
    from agent_framework.skill import SkillLoader

    loader = SkillLoader(storage)
    try:
        skills = {s.name: s for s in await loader.discover(user_id or None)}
    except Exception:      # noqa: BLE001 - 读不到就当没有
        return ""
    chunks: list[str] = []
    for raw in list(paths)[:6]:
        key = str(raw or "").strip()
        if ":" not in key:
            continue
        skname, rel = key.split(":", 1)
        sk = skills.get(skname.strip())
        if sk is None:
            chunks.append(f"### {key}\n（没有这份技能）")
            continue
        try:
            text = (await loader.read_attachment(sk, rel.strip())).decode("utf-8", "replace")
        except Exception:      # noqa: BLE001
            chunks.append(f"### {key}\n（读不到这个文件）")
            continue
        chunks.append(f"### {key}\n```\n{text[:READ_CHARS]}\n```")
    return "\n\n".join(chunks)


async def assign(spec: dict[str, Any], *, storage: Any = None, llm: Any = None,
                 user_id: str = "", conversation_id: str = "") -> dict[str, Any]:
    """一次调用：把配方卡分配到各镜，写进 ``spec["film_ledger"]["recipes"]``。

    → ``{assigned, cost_yuan, calls, note}``。**不抛**：配不上就这一轮不塞，
    写手退回「只有原则没有手法」的老路——比塞错一篇好。
    """
    from agent_framework.cost import cost_yuan

    from .selfcheck import _complete_with_identity

    out: dict[str, Any] = {"assigned": {}, "cost_yuan": 0.0, "calls": 0, "note": ""}
    if storage is not None:
        await load(storage, user_id or None)
    cat = catalog()
    if not cat:
        out["note"] = "库里没有可用的配方卡/知识文档，这一轮不塞"
        return out
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    if not shots:
        return out
    rows = "\n".join(
        f"{s.get('id')}｜主体：{s.get('subject') or '（没写）'}"
        f"｜画法：{s.get('subject_channel') or '（没申报）'}"
        f"｜brief：{str(s.get('brief') or '')[:90]}"
        for s in shots)
    ask = (f"## 目录（名字｜一句话｜可点名读取的路径）\n{cat}"
           f"\n\n## 分镜\n{rows}")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": ask},
    ]
    got: dict[str, list[str]] = {}
    raw: dict[str, Any] = {}
    for round_no in range(MAX_READ_ROUNDS + 1):
        try:
            resp = await _complete_with_identity(
                messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
                max_tokens=1200, timeout=120)
        except Exception as exc:  # noqa: BLE001 —— 分配是增益，不是闸
            out["note"] = f"分配调用失败（{type(exc).__name__}），这一轮不塞配方"
            return out
        out["calls"] += 1
        usage = getattr(resp, "usage", None)
        if usage:
            out["cost_yuan"] = round(
                out["cost_yuan"] + cost_yuan(usage, model=str(usage.get("model") or "")), 4)

        text = str(getattr(resp, "content", "") or "")
        m = re.search(r"\{.*\}", text, re.S)
        raw = {}
        if m:
            try:
                raw = json.loads(m.group(0))
            except ValueError:
                raw = {}
        want = raw.get("read") if isinstance(raw, dict) else None
        if isinstance(want, list) and want and round_no < MAX_READ_ROUNDS:
            # 模型要看文件：读出来接在对话后面，让它下一轮再分配。
            body = await _read_requested(storage, user_id, [str(x) for x in want])
            if not body:
                break
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "## 你要的文件\n\n" + body})
            out["read"] = list(out.get("read") or []) + [str(x) for x in want][:6]
            continue
        break

    if raw:
        valid = set(names())
        for sid, arr in raw.items():
            if not isinstance(arr, list):
                continue
            keep = [str(x).strip() for x in arr if str(x).strip() in valid][:1]
            if keep:
                got[str(sid)] = keep
    if not got:
        out["note"] = "分配没给出任何可用的名字（这一轮不塞配方）"
        return out

    led = spec.get("film_ledger")
    if not isinstance(led, dict):
        led = {}
        spec["film_ledger"] = led
    led["recipes"] = got
    out["assigned"] = got
    return out


def block(shot: Mapping[str, Any], spec: Mapping[str, Any] | None = None) -> str:
    """这一镜的配方卡/知识段落。没被分配到、或缓存还没热，返回空串。

    缓存由 :func:`assign` 里的 :func:`load` 预热；`shots_patch` 局部重交时若没走
    `assign`，这里返回空——降级成「只有原则」，不影响写镜。
    """
    if not spec:
        return ""
    led = spec.get("film_ledger")
    assigned = led.get("recipes") if isinstance(led, Mapping) else None
    if not isinstance(assigned, Mapping):
        return ""
    sid = str(shot.get("id") or "")
    picked = [str(x) for x in (assigned.get(sid) or []) if str(x).strip()]
    if not picked:
        return ""
    chunks: list[str] = []
    for nm in picked[:2]:
        r = _CACHE.by_name.get(nm)
        if not r:
            continue
        if r.kind == "code":
            # 示例代码整段给（已截过）。它是「长这样」，不是「照这个跑」：
            # 导入 skill 里的代码常引用它自己环境的组件，白名单外的 import 过不了闸①。
            lang = r.lang or "tsx"
            chunks.append(f"【{r.name}】可抄的示例代码（{r.relpath}）\n\n"
                          f"```{lang}\n{r.body.strip()}\n```")
        else:
            head = f"【{r.name}】{r.one_liner}".rstrip()
            if r.fit:
                head += f"\n适用：{r.fit}"
            picked = _pick_sections(r.body)
            # 没有 `##` 小节的文档（Claude 规范里很常见）取开头，别整段丢
            body = picked or re.sub(r"\n{3,}", "\n\n", r.body.strip())[:KNOWLEDGE_HEAD_CHARS]
            chunks.append(head + "\n\n" + body)
    if not chunks:
        return ""
    return (
        "\n=== 这一镜可以照抄的配方卡 ===\n"
        "（从全库的做法文档里挑的，跟这一镜最接近的那一篇）\n\n"
        + "\n\n---\n\n".join(chunks)
        + "\n\n**这是手法参考，不是任务清单**：照它给的节拍、参数、构图去做，"
        "画面内容仍按这一镜的 brief 画。它讲的「三式 / 两式」是同一类镜头的几种拍法，"
        "**挑一种用**，不要把几种全塞进一镜。\n"
    )
