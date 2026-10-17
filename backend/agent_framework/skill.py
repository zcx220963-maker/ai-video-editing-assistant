"""SKILL 技能系统：技能库在 PG（skills / skill_files），附件字节在 MinIO，渐进式加载。

对应设计文档「SKILL 技能系统」+ spec §3.13、§4：
- 一个技能 = skills 表一行（name / description / frontmatter / body）
  + skill_files 若干附件行，附件字节存对象存储键 skills/{name}/{relpath}。
- 渐进式加载：先由 SkillManifestContextSource 注入【轻量清单】(<skills> XML，只有
  name/description/location)；LLM 命中后用 load_skill 拉【完整正文】，附件再按需
  load_skill(name, path=相对路径) 取，避免一次塞满上下文。
- frontmatter always=true 且可用的技能，正文常驻注入，无需再 load。
- 依赖不满足（如缺 CLI: curl）→ available=false，不加载。
- 本地目录只是【导入源】(sync_from_dir)：扫盘 → 正文进表、附件进对象存储。
  读路径不碰磁盘，多实例从同一张表读到一致清单。
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Sequence

from .compress import SKILL_INDEX_END
from .storage import StorageUnavailable
from .tool import Tool

# 与 hooks.py 同一根 logger：技能生命周期里的「尽力而为」失败只留痕，不打断动作
logger = logging.getLogger("agent_framework")

# requirement token（如 "CLI: curl" / "ENV: KEY"）-> (是否满足, 说明)
RequirementChecker = Callable[[str], "tuple[bool, str]"]


def default_requirement_checker(token: str) -> tuple[bool, str]:
    t = token.strip()
    if ":" in t:
        prefix, _, arg = t.partition(":")
        prefix, arg = prefix.strip().upper(), arg.strip()
    else:
        prefix, arg = "CLI", t
    if prefix in ("CLI", "CMD", "COMMAND"):
        return shutil.which(arg) is not None, f"CLI: {arg}"
    if prefix == "ENV":
        return bool(os.getenv(arg)), f"ENV: {arg}"
    return True, t  # 未知前缀按满足处理，避免误杀


@dataclass
class Skill:
    name: str
    description: str
    always: bool
    location: str                 # 附件前缀（对象存储键面）；SKILL.md 是正文的别名
    body: str                     # skills.body（frontmatter 之后的正文）
    requires: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)   # skill_files 行
    available: bool = True
    unavailable_reason: str = ""
    display: str = ""             # frontmatter display：面向用户的中文名
    declared_sections: list[str] = field(default_factory=list)  # frontmatter sections：

    # ---- 分段键（T42，§5.7 第②条）----

    @property
    def section_names(self) -> list[str]:
        """正文里**真出现**的分段名（按出现顺序；无标记就是空表）。

        与 ``declared_sections`` 是两件事：declared 是 frontmatter 里那份声明，
        这里是正文里真切的块。两者不一致说明有人加/删了块没改声明（或反过来）——
        对账靠 `tests/test_skill.py`，这里只如实报。
        """
        return list(split_sections(self.body)[1])

    def assemble(self, sections: Iterable[str] = ()) -> str:
        """按需拼正文：**标记之外的正文 + 这次点名要的那几块**（T42）。

        没有分段标记的技能（今天绝大多数）原样返回 ``body``——这一层是**纯增益**，
        不改变任何既有技能的内容。
        """
        return assemble_sections(self.body, sections)

    def manifest_lines(self) -> str:
        attr = "true" if self.available else "false"
        lines = [
            f'  <skill available="{attr}">',
            f"    <name>{self.name}</name>",
        ]
        if self.display:
            lines.append(f"    <display>{self.display}</display>")
        lines += [
            f"    <description>{self.description}</description>",
            f"    <location>{self.location}</location>",
        ]
        if not self.available and self.requires:
            lines.append(f"    <requires>{'; '.join(self.requires)}</requires>")
        lines.append("  </skill>")
        return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """极简 frontmatter 解析：--- 包裹的 key: value 行 + 其后正文。"""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text.strip()
    lines = text.splitlines()
    # 第一段 '---' 之后到下一个 '---'
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
        i += 1
    body = "\n".join(lines[i + 1 :]).strip() if i < len(lines) else ""
    return meta, body


def _parse_requires(raw: str) -> list[str]:
    """解析 requires：支持 'CLI: curl'、'CLI: curl, ENV: KEY'、'[...]'。"""
    raw = raw.strip().strip("[]")
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _parse_sections(raw: Any) -> list[str]:
    """解析 frontmatter 的 ``sections:``：``image-channel, sound-design`` 这种一行清单。

    为什么沿用 ``_parse_requires`` 那种「一行逗号分隔」而不是 YAML 列表：frontmatter
    解析器是**极简的** ``key: value``（多行列表它读不了，读不了就会静默丢掉后半段）。
    要写多行列表就得先把解析器换成 YAML 库，而那会把「改一行 frontmatter」的代价从
    评审换到「依赖 + 换行缩进的坑」上——为六个名字不值这个价。
    """
    raw = str(raw or "").strip().strip("[]")
    return [tok.strip().strip("\"'") for tok in raw.split(",") if tok.strip()]


# --------------------------------------------------------------------------
# 分段键（T42，§5.7 第②条）：正文里单独成行的 `<!-- section: <name> -->` 起一块，
# 块到下一个标记、`<!-- /section -->` 或文末为止。**标记之外的正文 = 默认注入集**。
#
# 为什么默认集是「标记之外」而不是「标了 default 的那些」：加一节说明是**增**动作，
# 而「默认发什么」是**既有行为**。按这个方向定，新写的一节天生不进默认集（T36 那三节
# 就是这么加的），既有技能一个字都不用标、行为逐字不变（没有标记 → 整份正文照发）
# ——反过来定的话，每加一节都要回头去数「默认集里原来有哪些」，那才是漂移的入口。
#
# 为什么有收尾标记 `<!-- /section -->`：没有它，块只能一路排到文末，于是「中间抽掉一段、
# 后面接着回默认集」这件事写不出来（`motion_explainer` 要抽的三段就散在正文中段：
# 整片开关、资产与图像通道、闸⑪）。收尾标记之后的行重新算默认集，**不写它就等于
# 「一直到下一个标记」**——两种写法都存在，是刻意的：末尾的块不必再多写一个收尾标记。
#
# 为什么标记用注释形而不是 `# 标题`：标题是**给人看的排版**，注释是**给机器看的身份**。
# 拿 `# 一镜只有七栏` 这种标题当分段键，等于把「改标题措辞」变成「改注入行为」——
# 技能正文的措辞本来就是最常改的东西。
_SECTION_MARK = re.compile(r"^<!--\s*section:\s*([A-Za-z0-9_.\-]+)\s*-->\s*$")
_SECTION_END = re.compile(r"^<!--\s*/section\s*-->\s*$")


def _section_pieces(body: str) -> list[tuple[str | None, str]]:
    """正文 → ``[(段名或 None, 文本)]``（按出现顺序；``None`` = 标记之外的默认集）。"""
    pieces: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in (body or "").splitlines():
        m = _SECTION_MARK.match(line.strip())
        if m:
            pieces.append((m.group(1), []))
            continue
        if _SECTION_END.match(line.strip()):
            pieces.append((None, []))
            continue
        pieces[-1][1].append(line)
    return [(name, "\n".join(lines).strip()) for name, lines in pieces]


def split_sections(body: str) -> tuple[str, list[str]]:
    """正文 → ``(默认集文本, 分段名清单)``。没有标记时分段名清单为空。"""
    base: list[str] = []
    names: list[str] = []
    for name, text in _section_pieces(body):
        if name is None:
            base.append(text)
        else:
            names.append(name)
    return "\n\n".join(t for t in base if t), names


def assemble_sections(body: str, sections: Iterable[str] = ()) -> str:
    """默认集 + 这次点名要的那几块（保正文原序）；**没有分段标记时原样返回**。

    要一个本技能没有的段名时**静默跳过**，不报错：段名是「通道/特性键」（计划里用了
    图像通道 → 要 ``image-channel``），同一个键在不同技能里写不写那一段是各技能的
    自由——十二份技能各自只认自己有的那几块，调用方不必先问清单。
    """
    want = {str(s).strip() for s in (sections or ()) if str(s).strip()}
    pieces = _section_pieces(body)
    if not any(name for name, _ in pieces):
        return body
    out = [text for name, text in pieces
           if text and (name is None or name in want)]
    return "\n\n".join(out)


def section_text(body: str, name: str) -> str | None:
    """**单取一块**：那个分段键对应的正文，没有这一块时回 ``None``。

    与 ``assemble_sections`` 的分工：那一个是「默认集 + 点名的块」——它服务的场景是
    **整份注入**（模型还没开口，服务端先替它把该带的带上）；这一条服务的场景是
    **模型自己点名**（``load_skill(name, section=…)``），回的就该只是那一块：
    想省字节才点名，再顺手把默认集塞回去就等于没省。
    """
    key = str(name or "").strip()
    if not key:
        return None
    for piece, text in _section_pieces(body):
        if piece == key:
            return text
    return None


def section_catalog(body: str) -> list[tuple[str, str, int]]:
    """分段目录：``[(键名, 小节标题, 字节数)]``（按正文原序）；没标记就回空表。

    为什么服务端要主动给这份目录（而不是等模型去问）：分段键是**服务端与技能之间的
    约定**（``<!-- section: film-knobs -->``），模型在白名单、schema、附件清单里都
    查不到它。不给目录，模型只知道「有一份技能」，不知道里面还有几块可以单取——
    那就退化成整份加载，分块等于没分。
    """
    out: list[tuple[str, str, int]] = []
    for name, text in _section_pieces(body):
        if not name:
            continue
        title = next((ln.strip().lstrip("#").strip()
                      for ln in text.splitlines() if ln.startswith("#")), "")
        out.append((name, title[:40], len(text.encode("utf-8"))))
    return out


def section_directory_line(body: str, *, planned: Iterable[str] = (),
                           included: bool = True) -> str:
    """一行式的分段目录（含「本轮计划用得上哪几块」），没标记就回空串。

    ``planned`` 是 ``plan.skills.sections_for_plan`` 的读法（本轮计划命中的那几块）。
    它从「强制注入」降成这里的**一句提示**：计划读了哪几步就该用到哪些块，这件事
    服务端知道、模型不知道，所以必须说出来；但**要不要取、取哪几块由模型自己定**
    （§2.2：技能的视角还给模型）。取法就写在同一行里。

    ``included``：这一行跟着的那份正文里**有没有**这几块。两条路两种真话——
    ``preload_skills`` 给的是默认集（不含），整份加载给的是全文（含），
    说反了模型会拿"上面那份里就有"去上面找。
    """
    rows = section_catalog(body)
    if not rows:
        return ""
    want = {str(s).strip() for s in (planned or ()) if str(s).strip()}
    parts = [f"{name}（{title or '—'}，{size} B）" + ("**本轮计划用得上**" if name in want
                                                   else "")
             for name, title, size in rows]
    where = ("这一份正文里也有它们" if included
             else "**上面这份正文里没有它们**，要就照下面取")
    return (f"\n\n（这份技能按小节分了几块：{'；'.join(parts)}。{where}——"
            f'要哪一块单独取：load_skill(name=..., section=键名)；'
            f"为其中一块翻整份（40 KB 上下、会被压缩闸从中间切）不划算。）")


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


class SkillLoader:
    """读路径：skills/skill_files 两张表 + 对象存储附件；本地目录仅作导入源。"""

    def __init__(self, storage: Any, *,
                 checker: RequirementChecker = default_requirement_checker) -> None:
        self._skills = storage.skills
        self._objects = storage.objects
        self._checker = checker

    @staticmethod
    def object_key(name: str, relpath: str) -> str:
        return f"skills/{name}/{relpath}"

    def _to_skill(self, row: dict[str, Any]) -> Skill:
        fm = row.get("frontmatter") or {}
        requires = _parse_requires(str(fm.get("requires", "")))
        available, reason = self._check(requires)
        return Skill(
            name=row["name"],
            description=row.get("description") or str(fm.get("description", "")),
            always=_is_true(fm.get("always", "")),
            location=self.object_key(row["name"], "SKILL.md"),
            body=row.get("body") or "",
            requires=requires,
            files=list(row.get("files") or []),
            available=available,
            unavailable_reason=reason,
            display=str(fm.get("display", "") or ""),
            # frontmatter 的 `sections:` 是**声明**（一行逗号分隔的名字），正文里那几个
            # `<!-- section: … -->` 块是**事实**。两份都要有：声明让人一眼看得出这个技能
            # 分了哪几块，事实保证块真的存在——对账（声明 ⊆ 事实、事实 ⊆ 声明）由
            # `tests/test_skill.py` 判红，不许只写一头。
            declared_sections=_parse_sections(fm.get("sections", "")),
        )

    async def discover(self, user_id: str | None = None) -> list[Skill]:
        return [self._to_skill(r) for r in await self._skills.list(user_id)]

    async def get(self, name: str) -> Skill | None:
        row = await self._skills.get(name)
        return self._to_skill(row) if row else None

    def _find_file(self, skill: Skill, relpath: str) -> dict | None:
        """技能附件的寻址：正文里的裸 relpath 也能命中登记在 `demos/` 下的那份。

        `shots/INDEX.md` 的 demo 栏写的是 `rhythm/panel-grid-moves/X.tsx`，而
        `skill_files` 登记的是 `demos/rhythm/panel-grid-moves/X.tsx`——照抄索引就
        取不到附件（真机 run 97c1289eba35：三镜连试两次被拒后停手）。
        """
        files = skill.files
        rel = str(relpath or "").strip().lstrip("./").replace("\\", "/")
        bare = rel[len("demos/"):] if rel.startswith("demos/") else rel
        for cand in (rel, f"demos/{bare}", bare):
            entry = next((f for f in files if f["relpath"] == cand), None)
            if entry is not None:
                return entry
        low = {f["relpath"].lower(): f for f in files}
        for cand in (rel.lower(), f"demos/{bare}".lower()):
            if cand in low:
                return low[cand]
        # 文件名拼错了目录、或拿卡名拼了文件名：按文件名唯一命中，再按「这张卡的目录里
        # 只有一份 .tsx」唯一命中。都不唯一就不猜（猜错比报错更难查）。
        base = bare.rsplit("/", 1)[-1].lower()
        by_base = [f for f in files if f["relpath"].lower().rsplit("/", 1)[-1] == base]
        if len(by_base) == 1:
            return by_base[0]
        folder = bare.rsplit("/", 1)[0].lower() if "/" in bare else ""
        if folder:
            in_dir = [f for f in files
                      if f["relpath"].lower().startswith(f"demos/{folder}/")
                      and f["relpath"].lower().endswith(".tsx")]
            if len(in_dir) == 1:
                return in_dir[0]
        return None

    async def read_attachment(self, skill: Skill, relpath: str) -> bytes:
        """按 skill_files 登记的 object_key 取附件字节——只认表里在案的 relpath。"""
        entry = self._find_file(skill, relpath)
        if entry is None:
            raise KeyError(f"技能 {skill.name} 无附件 {relpath!r}")
        data = b""
        async for chunk in self._objects.get_stream(entry["object_key"]):
            data += chunk
        return data

    async def attachment_url(self, skill: Skill, relpath: str, ttl_sec: int = 3600) -> str:
        """非文本附件给一个临时读链接，而不是把字节塞回上下文。"""
        entry = self._find_file(skill, relpath)
        if entry is None:
            raise KeyError(f"技能 {skill.name} 无附件 {relpath!r}")
        return await self._objects.presign_get(entry["object_key"], ttl_sec=ttl_sec)

    async def sync_from_dir(self, skills_dir: str | Path,
                            *, owner_user_id: str | None = None) -> list[str]:
        """导入源 → 库里一份：正文 upsert 进 skills，SKILL.md 以外的文件进对象存储。

        幂等：同名技能整体覆盖（附件清单以最新一次导入为准）。
        owner_user_id 为 None 表示系统内置（共享），填了表示用户个人添加（私有）。
        """
        root = Path(skills_dir)
        if not root.is_dir():
            return []
        imported: list[str] = []
        for sub in sorted(root.iterdir()):
            md = sub / "SKILL.md"
            if not sub.is_dir() or not md.exists():
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            name = meta.get("name") or sub.name
            previous = await self._skills.get(name)
            files: list[dict[str, Any]] = []
            for p in sorted(sub.rglob("*")):
                if not p.is_file() or p == md:
                    continue
                rel = p.relative_to(sub).as_posix()
                key = self.object_key(name, rel)
                data = p.read_bytes()
                await self._objects.put(key, _one_chunk(data),
                                        content_type=mimetypes.guess_type(rel)[0] or "",
                                        size=len(data))
                files.append({"relpath": rel, "object_key": key, "bytes": len(data)})
            await self._skills.upsert(name, body,
                                      description=meta.get("description", ""),
                                      frontmatter=meta, files=files,
                                      owner_user_id=owner_user_id)
            # 附件生命周期随 skill_files（spec §4）：新清单没沿用的旧对象要清掉
            keep = {f["relpath"] for f in files}
            for stale in (previous or {}).get("files") or []:
                if stale["relpath"] not in keep:
                    await self._objects.delete(stale["object_key"])
            imported.append(name)
        return imported

    async def import_zip(self, filename: str, data: bytes,
                         *, max_bytes: int = 20 * 1024 * 1024,
                         owner_user_id: str | None = None) -> list[str]:
        """上传技能包（zip）→ 解到临时目录 → 走同一份 sync_from_dir（幂等覆盖）。

        包形状两种都收：根上直接 SKILL.md，或若干顶层目录各含 SKILL.md。
        安装面做三道防线：大小上限、条目数上限、zip-slip（压缩包内路径越界）拒收。
        返回导入的技能名清单。
        owner_user_id 填了归属本人（私有），None 表示系统内置（共享）。
        """
        if not (filename or "").lower().endswith(".zip"):
            raise ValueError("技能包必须是 .zip 文件")
        if not data:
            raise ValueError("技能包内容为空")
        if len(data) > max_bytes:
            raise ValueError(f"技能包过大（{len(data)} 字节 > 上限 {max_bytes}）")
        with tempfile.TemporaryDirectory(prefix="skill_upload_") as td:
            root = Path(td)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                members = zf.namelist()
                if len(members) > 500:
                    raise ValueError(f"压缩包条目过多（{len(members)} > 500）")
                root_resolved = str(root.resolve())
                for m in members:
                    target = (root / m).resolve()
                    if not str(target).startswith(root_resolved):
                        raise ValueError(f"压缩包路径越界，拒收：{m}")
                zf.extractall(root)
            if (root / "SKILL.md").is_file():
                # 根上直接 SKILL.md 的形状：包进一个子目录，统一交给 sync_from_dir
                sub = root / "_packaged"
                sub.mkdir()
                for p in list(root.iterdir()):
                    if p != sub:
                        shutil.move(str(p), str(sub / p.name))
            return await self.sync_from_dir(root, owner_user_id=owner_user_id)

    async def drop(self, name: str) -> int:
        """删技能：删行的同时清掉它的附件对象，否则对象成为永不被引用的孤儿字节。

        原来只调 storage.skills.drop()——行没了、skill_files 关联表清了，但附件字节
        还留在 MinIO（README §6）。对象删除按 delete_material 的「删不掉不阻断」口径
        走 best-effort：单个对象失败只告警，既不把技能行留下（行没了就不会再被引用），
        也不把异常抛出 drop 之外；行删除才是这次操作的确定性结果。
        """
        row = await self._skills.get(name)
        for f in (row or {}).get("files") or []:
            key = f.get("object_key")
            if not key:
                continue
            try:
                await self._objects.delete(key)
            except Exception as exc:  # noqa: BLE001 - 对象删不掉不阻断删行
                logger.warning("技能 %s 的附件对象删除失败（%s）：%s", name, key, exc)
        return await self._skills.drop(name)

    def _check(self, requires: list[str]) -> tuple[bool, str]:
        for token in requires:
            ok, desc = self._checker(token)
            if not ok:
                return False, f"缺少依赖 {desc}"
        return True, ""


class SkillManifestContextSource:
    """ContextSource：注入轻量技能清单；always 技能附完整正文。"""

    name = "skills"
    # 账面（§5.7 第①条）自报：清单每轮无条件发（无技能才返 None），
    # 读它的是 load_skill——清单上的名字就是能被按需拉取的技能。
    ledger_kind = "always"
    ledger_referenced_by = ("load_skill",)

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    async def render(self, query: str, *, user_id: str | None = None) -> str | None:  # noqa: ARG002 (query 预留给未来匹配)
        skills = await self._loader.discover(user_id)
        if not skills:
            return None
        manifest = "<skills>\n" + "\n".join(s.manifest_lines() for s in skills) + "\n</skills>"
        sections = [manifest]

        # 常驻：always 且可用 → 直接附正文
        for s in skills:
            if s.always and s.available and s.body:
                sections.append(
                    f'<skill_instructions name="{s.name}">\n{s.body}\n</skill_instructions>'
                )
        return "\n\n".join(sections)


#: 正文超过这个体量就在末尾附一句「共几行、怎么续读」。取值在闸的截断线
#: （`compress.DEFAULT_RESULT_MAX_BYTES`，24 KB）**之下**：等回执说「中间被省掉」
#: 才告诉它怎么取，模型已经先撞上那面墙、已经开始了那串找不回来的尝试。
_PAGING_NOTE_BYTES = 8_000

#: 章节目录最多列几行标题。真机那份技能 44 个小节全列约 1.2 KB——它进的是闸的**尾巴**
#: 那 28%（约 6.7 KB），列得下；但这个数不该随技能变大而无限长，超出的按行号往后取即可。
_OUTLINE_MAX_ROWS = 48

#: 「无附件 xxx」那条错误里最多列几个在案附件。上百个全列是十几 KB 的一团路径，
#: 而模型此刻只想知道「我写的名字错在哪」——完整清单在整份加载时已经给过。
_MISSING_LIST_MAX = 12

#: 一个目录下附件数达到这么多，清单里就折叠成「目录 / 件数 / 索引在哪」一行。
_BULK_MIN = 8

#: 折叠那一行要指出的索引附件名：这几批同族文件各自带一份完整清单。
_INDEX_NAMES = ("INDEX.md", "MANIFEST.json", "README.md")

#: 附件清单最多列几行。真机那份技能 290 个附件逐个列是 17.6 KB——比闸的头窗口还大，
#: 会把同一份回复里排在后面的正文与章节目录整个挤出窗口。折叠之后是十几行。
_MANIFEST_MAX_EXACT = 20


class LoadSkillTool(Tool):
    """按需加载：命中技能后拉取其完整正文，或按相对路径拉某个附件，都可按行分页。

    **为什么非得分页不可**（真机实测，14 镜「薄肌理论」那条 run）：这份技能的
    ``SKILL.md`` 有 42 KB，进链前会被 `compress.ToolResultGate` 按字节切掉中间一段
    （那条回执只说「中间被省掉，请用更窄的参数重取」）。而技能正文**只有这一条取值
    的路**：它不在会话工作区里（模型去 `read_file skills/…/SKILL.md` 撞「文件不存在」，
    又去 检索 那个目录撞「检索起点不存在」），也没有「换关键词」这一说。于是模型只能
    抱着那句「中间被省掉」把同一份技能反复加载（每次切点还一样），十几次调用全烧在
    找回那段上——**回执给了它一条走不通的路**。

    所以分页做在工具这一侧，按**行**切：offset 是 1 起始的行号，页脚直接写出「共 N 行、
    下一句该怎么写」。上面那道字节闸不用撤——它拦别的大结果仍然对，只是从这里出去的
    正文不再是「切在哪一行看运气」。

    **分页之后紧接着暴露的第二处**（同一条 run 修完重跑）：模型翻得动，就一路翻下去
    ——674 行顺读了 11 页，又回头重取其中 9 页，**四十几轮工具调用一个计划都没交出来**。
    它缺的不是「怎么续读」，是**「这一节在第几行」**与**「哪几行已经看过了」**：它明确
    知道自己要闸⑦ 通道表 / ㉑ 动效锚定 / ⑯ 主体具名 / 数据镜 / 节点参数，却只能按 120 行一段
    顺读，读完凭印象回头猜（offset 240 / 359 / 478 / 597 全是猜的，真值在 335 / 631 /
    481 / 596）。所以：

    - 整份给出时附一份**带行号的章节目录**（``_outline``）——那一份文档里唯一查不到的
      事实，就在标题里摆着；
    - 页脚写全三件事（``_page``）：这一页从哪一节开始、接着读的 offset 是几、**到底了
      没有**——有「这是最后一页」这个确定信号，回头重取就没有理由。
    """

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def display_name(self) -> str:
        return "加载技能"

    @property
    def description(self) -> str:
        return (
            "按名称加载技能的完整说明。缺省返回 SKILL.md **整份**正文（其中「另分了几块」"
            "的块也在里面，末尾附分段目录与附件清单）；只想要其中一块就传 section"
            "（分段键，比读整份省得多）；"
            "传 path（如 references/x.md、scripts/y.py）返回该附件内容；"
            "正文与附件都可以用 offset（1 起始的行号）与 limit（行数）按行分页续读，"
            "回执末尾会写明共有多少行。长正文在**整份给出时**还附一份带行号的章节目录，"
            "要哪一节直接按那个行号取那一节——**别从头顺读**。"
            "附件来自对象存储，只认技能登记过的相对路径，不接受任意文件路径"
            "——技能文件不在会话工作区里，用 read_file / grep 找不到它们。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名称（来自 <skills> 清单）"},
                "section": {"type": "string",
                            "description": "可选：只取正文里的一个分段块（键名见整份加载时"
                                           "给的分段目录），比读整份省得多"},
                "path": {"type": "string",
                         "description": "可选：技能目录内附件的相对路径；缺省取正文"},
                "offset": {"type": "integer",
                           "description": "可选：起始行号（1 起始）；与 limit 一起分页续读"},
                "limit": {"type": "integer", "description": "可选：读取行数；缺省整份"},
            },
            "required": ["name"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, name: str, path: str | None = None,
                      offset: int | None = None, limit: int | None = None,
                      section: str | None = None) -> str:
        skill = await self._loader.get(name)
        if skill is None:
            return f"Error: 未找到技能 {name}"
        if not skill.available:
            return f"Error: 技能 {name} 不可用（{skill.unavailable_reason}）"
        rel = (path or "").strip().lstrip("/")
        if rel:
            return await self._read_file(skill, rel, offset=offset, limit=limit)
        if not skill.body:
            return f"技能 {name} 无正文内容。location={skill.location}"
        if str(section or "").strip():
            return self._read_section(skill, str(section).strip())
        head = f"[skill:{name} location={skill.location}]"
        if skill.files:
            head += (f"\n附件（{len(skill.files)} 个，用 "
                     f'load_skill(name, path="相对路径") 读取）：\n'
                     + "\n".join(self._attachment_listing(skill.files)))
        body, err = self._page(skill.body, offset, limit)
        if err:
            return err
        if body is None:
            return (f"{head}{self._outline(skill.body)}"
                    f"\n{SKILL_INDEX_END}"
                    f"\n{skill.body}{self._paging_note(name, skill.body)}"
                    f"{section_directory_line(skill.body, included=True)}")
        # 分页那头**不重复整份附件清单**（几百字节到 1 KB 的目录，跟着每一页发一遍
        # 就是按页数翻倍）与**整份章节目录**（它在第一次加载时给过）。名字与位置留着
        # ——模型得能对上「这是刚才那份技能的正文切片」。
        return (f"[skill:{name} location={skill.location}]"
                f"{self._outline(skill.body, only_from=offset)}"
                f"\n{SKILL_INDEX_END}\n{body}")

    @staticmethod
    def _read_section(skill: "Skill", section: str) -> str:
        """只取某一块正文（``load_skill(name, section=…)``）。

        为什么单开这一条而不是让 offset/limit 顶着用：分段键**不是行号**，模型无从知道
        `film-knobs` 在第几行（真机那条 run 就是这么按 120 行顺读、读完再猜着回取的）。
        知道键名与知道行号是两件事，工具得能接住前一种。

        键名写错时不猜、不模糊匹配：把**在案的键名**列出来（键名是服务端与技能的约定，
        模型没有别的途径查得到），它照着改一个词就行——与附件路径那条错误同一形状。
        """
        text = section_text(skill.body, section)
        if text is None:
            names = [f"`{name}`" for name, _title, _size in section_catalog(skill.body)]
            if not names:
                return (f"Error: 技能 {skill.name} 的正文没有分段标记，"
                        f"没有 section 可点名——按整份或按 offset/limit 取。")
            return (f"Error: 技能 {skill.name} 没有 section「{section}」。"
                    f"在案的键名：{'、'.join(names[:_MISSING_LIST_MAX])}"
                    + ("…" if len(names) > _MISSING_LIST_MAX else "")
                    + "（键名见整份加载时给的分段目录）。")
        return (f"[skill:{skill.name} location={skill.location} section={section}]"
                f"\n{text}\n\n（这只是其中一块，共 {len(text.encode('utf-8'))} 字节；"
                f"要别的块照分段目录换 section 键名，不要整份重读。）")

    @staticmethod
    def _page(text: str, offset: int | None, limit: int | None) -> tuple[str | None, str]:
        """按行切片 → ``(正文, 错误)``。没给 offset/limit 就是 ``(None, "")``，
        调用方照原样给整份：**只在真的在分页时改变输出形状**，不传参数的老路径一字不动。

        越界不夹取而是回一句人话错误（与 `read_file` 同一形状）：夹取会让模型以为
        「后面没有了」，而它其实只是把行号写错了。

        页脚三件事：**这一页在哪一节**、**接着读的 offset 是几**、**到底了没有**。
        真机实测（14 镜「薄肌理论」那条 run）：这三件都没有时，模型读了 674 行里的
        11 页、又回头重取其中 9 页——它没有一个能确认「哪几行已经看过了」的办法。
        """
        if not offset and not limit:
            return None, ""
        lines = text.splitlines()
        start = max(1, int(offset or 1))
        if start > len(lines):
            return None, f"Error: offset {start} 超出末尾（全文共 {len(lines)} 行）"
        end = len(lines) if not limit else min(len(lines), start + max(1, int(limit)) - 1)
        chunk = "\n".join(lines[start - 1:end])
        where = LoadSkillTool._heading_at(lines, start)
        where = f"——从「{where}」这一节开始" if where else ""
        rest = ("**到底了**" if end >= len(lines) else f"接着读用 offset={end + 1}")
        return f"{chunk}\n\n（第 {start}–{end} 行，共 {len(lines)} 行{where}；{rest}）", ""

    @staticmethod
    def _attachment_listing(files: Sequence[Mapping[str, Any]]) -> list[str]:
        """附件清单：**小目录逐个列，大目录折叠成一行**。

        为什么非要折叠（真机实测，14 镜「薄肌理论」那条 run）：这份技能有 **290 个附件**，
        逐个列出来是 **17.6 KB**——比单条工具结果 24 KB 的闸的头窗口（约 17.3 KB）还大。
        后果不是「清单难看」，是**它把同一份回复里排在它后面的东西整个挤出窗口**：章节目录
        （1.2 KB）刚好落在切点之外，模型一个字都没看到。清单本身也在中途被切断——真机那次
        模型就是拿这半截清单去猜路径的。

        折叠的判据只用**目录 + 件数**，不认技能内容：一个目录下件数不多就照原样列（模型
        要按 path 取值，名字得是真的）；件数多说明那是一批同族文件，给「目录 / 件数 /
        哪一份是它的索引」就够——真机这两批各有一份现成的索引附件（``shots/INDEX.md``
        与 ``demos/MANIFEST.json``），完整相对路径本来就在里面。
        """
        rows = [str(f.get("relpath") or "") for f in files]
        size = {str(f.get("relpath") or ""): f.get("bytes", 0) for f in files}
        groups: dict[str, list[str]] = {}
        for rel in rows:
            groups.setdefault(rel.split("/", 1)[0] if "/" in rel else "", []).append(rel)
        out: list[str] = []
        # 根目录上的先列（pipeline.md / sound-design.md 这类是模型真会点名的）
        for name in sorted(groups, key=lambda k: (k != "", k)):
            members = sorted(groups[name])
            if not name or len(members) < _BULK_MIN:
                out.extend(f"  - {rel}（{size.get(rel, 0)} 字节）" for rel in members)
                continue
            index = next((rel for rel in members
                          if rel.rsplit("/", 1)[-1] in _INDEX_NAMES), "")
            if index:
                out.append(f"  - {name}/：{len(members)} 个文件，完整相对路径见 {index}")
            else:
                out.append(f"  - {name}/：{len(members)} 个文件，如 "
                           + "、".join(members[:3]) + " …")
        if len(out) > _MANIFEST_MAX_EXACT:
            rest = len(out) - _MANIFEST_MAX_EXACT
            out = out[:_MANIFEST_MAX_EXACT] + [f"  …（另有 {rest} 项未列出）"]
        return out

    @staticmethod
    def _heading_at(lines: list[str], lineno: int) -> str:
        """第 ``lineno`` 行往上最近的那个标题（没有就给空串）。"""
        for i in range(min(lineno, len(lines)), 0, -1):
            line = lines[i - 1]
            if line.startswith("#"):
                return line.lstrip("#").strip()[:40]
        return ""

    @staticmethod
    def _outline(text: str, *, only_from: int | None = None) -> str:
        """长正文的**章节目录**：标题 + 行号。短正文不给（几十行加张目录纯属噪音）。

        为什么非给不可（真机实测，14 镜「薄肌理论」那条 run）：这份技能 674 行、44 个小节，
        模型明确知道自己要哪几节（闸⑦ 通道表 / ㉑ 动效锚定 / ⑯ 主体具名 / 数据镜 / 节点参数），
        **却无从知道它们在文件里的哪一行**——于是它按 120 行一段顺读，读完再回头猜着取
        （offset 240 / 359 / 478 / 597 全是猜的，真值在 335 / 631 / 481 / 596），四十几轮
        工具调用一个计划都没交出来。**"要哪一节在哪一行"是这一份文档唯一查不到的事实**，
        而它本来就在标题里摆着。

        ``only_from``：分页调用只在**第一页**附目录——它在整份加载时已经给过一次，每页
        再发一遍就是按页数翻倍。
        """
        if len(text.encode("utf-8")) <= _PAGING_NOTE_BYTES:
            return ""
        if only_from not in (None, 1):
            return ""
        rows = [(i, ln) for i, ln in enumerate(text.splitlines(), start=1)
                if ln.startswith("#")]
        if not rows:
            return ""
        shown = rows[:_OUTLINE_MAX_ROWS]
        body = "\n".join(f"  {i:>4}  {ln.strip()[:52]}" for i, ln in shown)
        more = (f"\n  …还有 {len(rows) - len(shown)} 节（按行号往后取即可）"
                if len(rows) > len(shown) else "")
        return ("\n章节目录（左边是行号：要哪一节就 load_skill(name, offset=那个行号) "
                f"直取那一节，**不要从头顺读**）：\n{body}{more}")

    @staticmethod
    def _paging_note(name: str, text: str) -> str:
        """整份给出时，太长的正文附一句「共几行、怎么续读」。

        阈值取在闸的截断线（24 KB）**之下**：等回执说「中间被省掉」才告诉它怎么取，
        模型已经先撞上那面墙了。行数写出来，它才知道该从第几行接着要。
        """
        if len(text.encode("utf-8")) <= _PAGING_NOTE_BYTES:
            return ""
        total = len(text.splitlines())
        return (f"\n\n（这份正文共 {total} 行；太长读不完时用 "
                f'load_skill(name="{name}", offset=行号, limit=行数) 按行续读，'
                f"不要追问原文里没有的东西。）")

    async def _read_file(self, skill: Skill, rel: str,
                         *, offset: int | None = None, limit: int | None = None) -> str:
        if rel == "SKILL.md":
            body, err = self._page(skill.body, offset, limit)
            if err:
                return err
            if body is not None:
                return (f"[skill:{skill.name} location={skill.location}]"
                        f"{self._outline(skill.body, only_from=offset)}"
                        f"\n{SKILL_INDEX_END}\n{body}")
            return (f"[skill:{skill.name} location={skill.location}]"
                    f"{self._outline(skill.body)}\n{SKILL_INDEX_END}\n{skill.body}"
                    + self._paging_note(skill.name, skill.body))
        found = self._loader._find_file(skill, rel) if hasattr(self._loader, "_find_file")             else next((f for f in skill.files if f["relpath"] == rel), None)
        if found is not None:
            rel = found["relpath"]          # 按在案的真名读，回执里写的也是真名
        if found is None:
            # 附件清单**只报一小截**：真机实测这份技能有两百多个附件，全倒出来是十几 KB
            # 的一团路径（还多半会被闸从中间切掉），而模型此刻只想知道「我写的那个名字
            # 错在哪」。完整清单本来就在整份加载的那一段里给过。
            names = [f["relpath"] for f in skill.files]
            head = "、".join(names[:_MISSING_LIST_MAX]) or "（该技能无附件）"
            more = f"…（共 {len(names)} 个）" if len(names) > _MISSING_LIST_MAX else ""
            return (f"Error: 技能 {skill.name} 无附件 {rel}；在案的附件有 {head}{more}。"
                    f"完整清单见整份加载该技能时给的那一节。")
        try:
            data = await self._loader.read_attachment(skill, rel)
        except StorageUnavailable as exc:
            return f"Error: 读取附件 {rel} 失败：{exc}"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            url = await self._loader.attachment_url(skill, rel)
            return (f"[skill:{skill.name} file={rel}] 非文本附件（{len(data)} 字节），"
                    f"可直接访问的临时链接：{url}")
        body, err = self._page(text, offset, limit)
        if err:
            return err
        if body is not None:
            return f"[skill:{skill.name} file={rel}]\n{body}"
        # 附件这一路**不加**页脚：取附件是「把这份字节原样交给模型」，正文一字不改是
        # 它唯一的契约（`test_code_forage` ⑥ 拿最大那份 demo 逐字节验的就是这条）。
        # 缺的那半句由闸那边的回执补上——它已经知道这是 load_skill 的结果。
        return f"[skill:{skill.name} file={rel}]\n{text}"


def register_skill_tools(registry, loader: SkillLoader) -> None:
    registry.register(LoadSkillTool(loader))
