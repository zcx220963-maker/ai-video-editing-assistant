"""提示词库：把整段提示文本从 .py 里搬出来，放到磁盘上按需替换。

**为什么需要这一层。** 这个项目对外宣称「LLM 驱动 + 提示词和 skill 动态加载」，
但实测只有工具与技能是真的动态：系统提示词、规划轮整段、5 类纠错 nudge 全部
**硬编码在 5 个 .py 文件里**，改一句文案要动代码、要重新走代码评审与发布。
`ContextBuilder` 其实早就实现了读磁盘的 Bootstrap 机制（``bootstrap_dir``），
但生产装配里那个参数从来没传过——那个口子一直空着。

**这一层的边界（有意为之）。** 搬「整段、可独立成文的自然语言」：系统提示词、
几条纠错说明、规划轮整段（``planning_round.md``，2026-10-02 落盘）。规划轮段里的
条件也一起搬了，但只搬到**块级开关**这一层：模板用空行分块，块首单独一行的
``{?key}`` 表示「这个键没值就整块不出现」。数据仍然由代码算（节点白名单、开关枚举
都是服务端现取的判据），模板只管措辞与出现与否。
不再多加 if/for/嵌套：一旦要那些，就不是「改文案不必改代码」，而是往仓库里塞一门
模板语言、把调试成本从评审换到了渲染。

替换语义（与 ``str.format_map`` 不同）：**只替换我明确提供的键，其余原样保留**。
提示词里天然会写 ``{"plans": [...]}``、``{clip, start, end}`` 这类 JSON 片段，
按 ``format_map`` 会当成占位符直接 KeyError 或吃字符——那是最容易踩的坑。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

# 允许在提示词文件里写的占位符形态：{name} / {name:spec}，name 限定为标识符。
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^{}]*)?\}")

# 块守卫：单独成行的 {?name}——这个键没有值时**整块不出现**。
_BLOCK_GUARD = re.compile(r"^\{\?([A-Za-z_][A-Za-z0-9_]*)\}$")

# 块分隔：一行空白（含只有空格的情况）。
_BLOCK_SEP = re.compile(r"\n[ \t]*\n")

# 仓库自带的提示词目录（随包分发，作为默认来源）
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def render_template(text: str, values: Mapping[str, Any]) -> str:
    """把 ``{key}`` 换成 values 里的值；**没有提供的键原样保留**。

    保留未提供的占位符是有意的：提示词里常常同时含真正的占位符与 JSON 示例
    （``{"plans": [...]}``），把后者当占位符处理会静默吃字符或直接抛 KeyError。
    只替换调用方明确给出的键，语义确定、不会误伤。
    """
    if not values:
        return text

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            return m.group(0)
        return str(values[key])

    return _PLACEHOLDER.sub(_sub, text)


def placeholders_in(text: str) -> set[str]:
    """列出文本需要的键名（自检用：确认模板要的键都提供了）。

    含两种形态：普通占位符 ``{key}`` 与块守卫 ``{?key}``——后者也是一次取值，
    漏传了不会报错，只会让整段文案静默消失，所以更要能被列出来检查。
    """
    keys = {m.group(1) for m in _PLACEHOLDER.finditer(text or "")}
    for line in (text or "").splitlines():
        m = _BLOCK_GUARD.match(line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def _present(value: Any) -> bool:
    """守卫键的「有值」判据：空白串 / None / 空集合 / False 都算没有。

    数字 0 也算没有——契约里守卫键只放文本或布尔，不放计数。
    """
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def render_blocks(text: str, values: Mapping[str, Any]) -> str:
    """按**空行分块**渲染带条件段的提示词。

    两条规则，没有第三条：
      1. 块首单独一行的 ``{?key}`` 是守卫——``values[key]`` 没有值时整块不出现；
         有值时去掉这一行，其余照常渲染。
      2. 块内某行渲染后为空（它本来只有 ``{data}`` 而这次没数据）→ 这一行丢掉，不留空行。

    为什么只要这两条：规划轮那种「几十行按条件拼装」的文案，条件与数据是同一件事
    （有旧卡才提旧卡，有反馈原话才提反馈）。搬进模板只需要**块级开关**，不需要 if/for/
    嵌套——一旦要那些，就不是「改文案不必改代码」而是往仓库里塞一门模板语言了。

    先按空行切块、再替换，因此值里自带的空行不会被重新解释成块边界。
    """
    out: list[str] = []
    for block in _BLOCK_SEP.split(text or ""):
        lines = block.split("\n")
        guard = _BLOCK_GUARD.match(lines[0].strip()) if lines else None
        if guard:
            if not _present(values.get(guard.group(1))):
                continue
            lines = lines[1:]
        kept = [r for r in (render_template(ln, values) for ln in lines) if r.strip()]
        if kept:
            out.append("\n".join(kept))
    return "\n".join(out)


class PromptLibrary:
    """按名字读提示词文件；带内存缓存，支持运行期换目录（热替换）。

    ``dir`` 不存在或某个文件缺失时**落到内联默认值**：提示词是核心资产，
    不能因为一个目录没拷过去就让服务起不来或让模型收到空 system prompt。
    """

    def __init__(self, directory: str | Path | None = None, *,
                 defaults: Mapping[str, str] | None = None,
                 filename: str = "system_prompt.md") -> None:
        self.dir = Path(directory) if directory else None
        self._defaults = dict(defaults or {})
        # 主系统提示词的文件名：与其它片段分开，因为它有自己的默认值来源
        self.main_filename = filename
        self._cache: dict[str, str] = {}
        self._mtime: dict[str, float] = {}

    # ---- 读取 ----

    def _read(self, name: str) -> str | None:
        """读一个提示词文件；文件不存在返回 None（由调用方决定回落）。"""
        if self.dir is None:
            return None
        path = self.dir / name
        try:
            stamp = path.stat().st_mtime
        except OSError:
            self._cache.pop(name, None)
            self._mtime.pop(name, None)
            return None
        if self._cache.get(name) is not None and self._mtime.get(name) == stamp:
            return self._cache[name]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        self._cache[name] = text
        self._mtime[name] = stamp
        return text

    def text(self, name: str, fallback: str = "", **values: Any) -> str:
        """取一段提示词（做占位符替换）；文件缺失就用 ``fallback``。

        ``fallback`` 由调用点传入内联默认值——这样「文件在不在」都不影响行为，
        增量迁移是安全的：先接线，再逐个把默认值搬进文件。
        """
        raw = self._read(name)
        body = raw if raw is not None and raw.strip() else fallback
        return render_template(body, values)

    def blocks(self, name: str, fallback: str = "", **values: Any) -> str:
        """同 ``text``，但按 ``render_blocks`` 的块规则渲染（条件段住在模板里）。"""
        raw = self._read(name)
        body = raw if raw is not None and raw.strip() else fallback
        return render_blocks(body, values)

    def system_prompt(self, fallback: str) -> str:
        return self.text(self.main_filename, fallback)

    # ---- 观测 / 自检 ----

    def loaded_from_disk(self) -> list[str]:
        """当前真正从磁盘读到的提示词文件名（诊断用）。"""
        if self.dir is None or not self.dir.is_dir():
            return []
        return sorted(p.name for p in self.dir.glob("*.md"))

    def describe(self) -> str:
        files = self.loaded_from_disk()
        if not files:
            return "提示词库：未接目录，全部使用内联默认值"
        return f"提示词库：{self.dir}（{len(files)} 份：{', '.join(files)}）"


def build_prompt_library(directory: str | Path | bool | None,
                         defaults: Mapping[str, str] | None = None) -> PromptLibrary:
    """装配入口：显式目录优先；``False`` 表示关闭（全用内联）；``None`` 用仓库自带目录。

    「仓库自带目录存在就用它」这条默认是刻意的：它让「把提示词搬到磁盘」
    这件事**默认生效**，而不是又一个需要记得打开的开关（Bootstrap 机制当年
    就是死在「写了但没人接」上）。
    """
    if directory is False:
        return PromptLibrary(None, defaults=defaults)
    if directory:
        return PromptLibrary(directory, defaults=defaults)
    if DEFAULT_PROMPTS_DIR.is_dir():
        return PromptLibrary(DEFAULT_PROMPTS_DIR, defaults=defaults)
    return PromptLibrary(None, defaults=defaults)
