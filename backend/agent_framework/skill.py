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
from typing import Any, AsyncIterator, Callable, Iterable

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

    async def read_attachment(self, skill: Skill, relpath: str) -> bytes:
        """按 skill_files 登记的 object_key 取附件字节——只认表里在案的 relpath。"""
        entry = next((f for f in skill.files if f["relpath"] == relpath), None)
        if entry is None:
            raise KeyError(f"技能 {skill.name} 无附件 {relpath!r}")
        data = b""
        async for chunk in self._objects.get_stream(entry["object_key"]):
            data += chunk
        return data

    async def attachment_url(self, skill: Skill, relpath: str, ttl_sec: int = 3600) -> str:
        """非文本附件给一个临时读链接，而不是把字节塞回上下文。"""
        entry = next(f for f in skill.files if f["relpath"] == relpath)
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


class LoadSkillTool(Tool):
    """按需加载：命中技能后拉取其完整正文，或按相对路径拉某个附件。"""

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
            "按名称加载技能的完整说明。缺省返回 SKILL.md 正文（并列出附件清单）；"
            "传 path（如 references/x.md、scripts/y.py）返回该附件内容。"
            "附件来自对象存储，只认技能登记过的相对路径，不接受任意文件路径。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名称（来自 <skills> 清单）"},
                "path": {"type": "string",
                         "description": "可选：技能目录内附件的相对路径；缺省取正文"},
            },
            "required": ["name"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, name: str, path: str | None = None) -> str:
        skill = await self._loader.get(name)
        if skill is None:
            return f"Error: 未找到技能 {name}"
        if not skill.available:
            return f"Error: 技能 {name} 不可用（{skill.unavailable_reason}）"
        rel = (path or "").strip().lstrip("/")
        if rel:
            return await self._read_file(skill, rel)
        if not skill.body:
            return f"技能 {name} 无正文内容。location={skill.location}"
        head = f"[skill:{name} location={skill.location}]"
        if skill.files:
            listing = "\n".join(f"  - {f['relpath']}（{f.get('bytes', 0)} 字节）"
                                for f in skill.files)
            head += "\n附件（用 load_skill(name, path=\"相对路径\") 读取）：\n" + listing
        return f"{head}\n{skill.body}"

    async def _read_file(self, skill: Skill, rel: str) -> str:
        if rel == "SKILL.md":
            return f"[skill:{skill.name} location={skill.location}]\n{skill.body}"
        if not any(f["relpath"] == rel for f in skill.files):
            known = ", ".join(f["relpath"] for f in skill.files) or "（该技能无附件）"
            return f"Error: 技能 {skill.name} 无附件 {rel}；在案的附件：{known}"
        try:
            data = await self._loader.read_attachment(skill, rel)
        except StorageUnavailable as exc:
            return f"Error: 读取附件 {rel} 失败：{exc}"
        try:
            return f"[skill:{skill.name} file={rel}]\n{data.decode('utf-8')}"
        except UnicodeDecodeError:
            url = await self._loader.attachment_url(skill, rel)
            return (f"[skill:{skill.name} file={rel}] 非文本附件（{len(data)} 字节），"
                    f"可直接访问的临时链接：{url}")


def register_skill_tools(registry, loader: SkillLoader) -> None:
    registry.register(LoadSkillTool(loader))
