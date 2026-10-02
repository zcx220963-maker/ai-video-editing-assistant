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

import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

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
        )

    async def discover(self) -> list[Skill]:
        return [self._to_skill(r) for r in await self._skills.list()]

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

    async def sync_from_dir(self, skills_dir: str | Path) -> list[str]:
        """导入源 → 库里一份：正文 upsert 进 skills，SKILL.md 以外的文件进对象存储。

        幂等：同名技能整体覆盖（附件清单以最新一次导入为准）。
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
                                      frontmatter=meta, files=files)
            # 附件生命周期随 skill_files（spec §4）：新清单没沿用的旧对象要清掉
            keep = {f["relpath"] for f in files}
            for stale in (previous or {}).get("files") or []:
                if stale["relpath"] not in keep:
                    await self._objects.delete(stale["object_key"])
            imported.append(name)
        return imported

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

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    async def render(self, query: str) -> str | None:  # noqa: ARG002 (query 预留给未来匹配)
        skills = await self._loader.discover()
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
