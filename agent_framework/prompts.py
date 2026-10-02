"""提示词库：把整段提示文本从 .py 里搬出来，放到磁盘上按需替换。

**为什么需要这一层。** 这个项目对外宣称「LLM 驱动 + 提示词和 skill 动态加载」，
但实测只有工具与技能是真的动态：系统提示词、规划轮整段、5 类纠错 nudge 全部
**硬编码在 5 个 .py 文件里**，改一句文案要动代码、要重新走代码评审与发布。
`ContextBuilder` 其实早就实现了读磁盘的 Bootstrap 机制（``bootstrap_dir``），
但生产装配里那个参数从来没传过——那个口子一直空着。

**这一层的边界（有意为之）。** 只搬「整段、可独立成文的自然语言」：
系统提示词与几条纠错说明。**不搬**那些与代码结构强耦合的模板：
规划轮段是几十行按条件拼装 + 中间插节点白名单与参数事实，
它的正确性依赖代码同时维护的数据（白名单、开关枚举），
把拼装逻辑一起搬到模板里只会把「改文案」变成「改模板语言 + 调试渲染」。
先让「改一句话」这件事不再需要改代码，是这一步的目的。

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
    """列出文本里出现的占位符名（自检用：确认模板需要的键都提供了）。"""
    return {m.group(1) for m in _PLACEHOLDER.finditer(text or "")}


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
