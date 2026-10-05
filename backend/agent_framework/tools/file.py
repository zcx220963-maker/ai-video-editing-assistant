"""原生文件工具：Write / Read / Edit / Grep。

安全约束（spec §8 第八项修正）：根目录在**每次调用时**现取，而不是进程启动时钉死。
钉死成 os.getcwd() 等于把整个仓库交给 LLM——它能改代码、能读 .env；这里取的是
本会话在渲染工作区下的独立沙箱（`{workspace_root}/{user:conv}/_files`），
解析后必须仍位于该沙箱内，绝对路径与 ``..`` 一律拒。

并发标记：Read / Grep 只读无副作用（read_only=True → concurrency_safe），
Write / Edit 会改磁盘（read_only=False → 需独占串行执行）。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Callable

from ..identity import current_identity_or
from ..storage.object_store import scoped_dir
from ..tool import Tool

# 工具实例是进程级单例、会话是每次一个，所以根目录只能是「怎么取」而不是「取到什么」。
RootProvider = Callable[[], Path]

# 文件工具在会话工作区里占的那一层，与渲染产物的 artifact 目录平级。
FILE_SANDBOX = "_files"


def session_workspace_root(workspace_root: str | Path,
                           *, sandbox: str = FILE_SANDBOX) -> RootProvider:
    """构造「本会话文件沙箱」的取法：按当前身份定位，取不到身份退回缺省作用域。"""

    def _provider() -> Path:
        return scoped_dir(workspace_root, current_identity_or().session_id, sandbox)

    return _provider


# 递归检索时跳过这些目录，避免命中依赖 / 版本库 / 缓存。
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}


class FileToolBase(Tool):
    """文件工具基类：按 provider 现取 workspace 根目录并做安全路径解析。"""

    def __init__(self, root_provider: RootProvider) -> None:
        self._root_provider = root_provider

    def root(self) -> Path:
        return Path(self._root_provider()).resolve()

    @staticmethod
    def _resolve(path: str, root: Path) -> Path:
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"路径越出工作区根目录: {path}")
        return candidate

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()


class WriteTool(FileToolBase):
    """写入 / 覆盖文件（自动创建父目录）。"""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def display_name(self) -> str:
        return "写文件"

    @property
    def description(self) -> str:
        return "将给定内容写入文件；文件不存在则创建，存在则整体覆盖。会自动创建父目录。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对本会话工作区的文件路径"},
                "content": {"type": "string", "description": "要写入的完整文本内容"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> str:
        target = self._resolve(path, self.root())
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        action = "覆盖" if existed else "创建"
        return f"已{action}文件 {path}（{len(content)} 字符）"


class ReadTool(FileToolBase):
    """按行读取文件，支持 offset/limit 分页。"""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def display_name(self) -> str:
        return "读文件"

    @property
    def description(self) -> str:
        return "读取文件内容并带行号返回；可用 offset（1 起始的行号）和 limit（行数）分页。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对本会话工作区的文件路径"},
                "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                "limit": {"type": "integer", "description": "读取行数，默认 2000"},
            },
            "required": ["path"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        target = self._resolve(path, self.root())
        if not target.is_file():
            return f"Error: 文件不存在 {path}"
        lines = self._read_lines(target)
        start = max(1, offset)
        end = start + max(1, limit) - 1
        chunk = lines[start - 1 : end]
        if not chunk and lines:
            return f"Error: offset {offset} 超出文件末尾（共 {len(lines)} 行）"
        numbered = "\n".join(f"{i:6d}\t{ln}" for i, ln in enumerate(chunk, start=start))
        shown_end = min(end, len(lines))
        more = "" if shown_end >= len(lines) else f"\n... 共 {len(lines)} 行，已显示至 {shown_end}"
        return f"{path}\n{numbered}{more}"


class EditTool(FileToolBase):
    """把文件中唯一的 old_string 精确替换为 new_string。"""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def display_name(self) -> str:
        return "编辑文件"

    @property
    def description(self) -> str:
        return (
            "在文件中将 old_string 精确替换为 new_string。默认要求 old_string 唯一匹配，"
            "匹配多次会报错（除非设置 replace_all）；不匹配则报错。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对本会话工作区的文件路径"},
                "old_string": {"type": "string", "description": "被替换的原始文本（需精确匹配）"},
                "new_string": {"type": "string", "description": "替换后的文本"},
                "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false"},
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        target = self._resolve(path, self.root())
        if not target.is_file():
            return f"Error: 文件不存在 {path}"
        text = target.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return f"Error: 未找到要替换的文本片段 {path}"
        if count > 1 and not replace_all:
            return f"Error: old_string 匹配 {count} 次，不唯一；请扩大上下文或设置 replace_all"

        if replace_all:
            updated, n = text.replace(old_string, new_string), count
        else:
            updated, n = text.replace(old_string, new_string, 1), 1
        target.write_text(updated, encoding="utf-8")
        return f"已替换 {n} 处（{path}）"


class GrepTool(FileToolBase):
    """在本会话工作区内按正则检索文件内容，返回 file:line: 文本。"""

    @property
    def name(self) -> str:
        return "grep"

    @property
    def display_name(self) -> str:
        return "检索文件"

    @property
    def description(self) -> str:
        return (
            "在本会话工作区内用正则表达式检索文件内容。可选 glob 过滤文件名（如 *.py），"
            "case_insensitive 忽略大小写，max_results 限制返回条数。返回 file:line: 行内容。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "检索起点（文件或目录），相对本会话工作区，默认其根目录"},
                "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
                "case_insensitive": {"type": "boolean", "description": "忽略大小写，默认 false"},
                "max_results": {"type": "integer", "description": "最多返回多少条匹配，默认 100"},
            },
            "required": ["pattern"],
        }

    @property
    def read_only(self) -> bool:
        return True

    def _iter_files(self, base: Path, root: Path):
        if base.is_file():
            yield base
            return
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
                continue
            yield p

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        case_insensitive: bool = False,
        max_results: int = 100,
    ) -> str:
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: 无效的正则表达式 {pattern!r}: {e}"

        root = self.root()
        base = self._resolve(path, root)
        if not base.exists():
            return f"Error: 检索起点不存在 {path}"

        matches: list[str] = []
        for f in self._iter_files(base, root):
            rel = f.relative_to(root).as_posix()
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                if f.stat().st_size > 2_000_000:  # 跳过超大文件
                    continue
                for lineno, line in enumerate(self._read_lines(f), start=1):
                    if regex.search(line):
                        matches.append(f"{rel}:{lineno}: {line.strip()}")
                        if len(matches) >= max_results:
                            return "\n".join(matches) + f"\n...（已达 max_results={max_results} 上限）"
            except (OSError, UnicodeDecodeError):
                continue

        if not matches:
            return f"未找到匹配：pattern={pattern!r} path={path}"
        return "\n".join(matches)


def register_file_tools(registry, root_provider: RootProvider) -> None:
    """把四个文件工具一次性注册到 ToolRegistry（根目录按会话在调用时现取）。"""
    for cls in (WriteTool, ReadTool, EditTool, GrepTool):
        registry.register(cls(root_provider))
