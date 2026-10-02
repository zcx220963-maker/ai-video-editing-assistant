"""上下文构建（可扩展总线）。

对应设计文档「Context 构建」，把喂给 LLM 的当前上下文拆成清晰的装配位：

    System Prompt  ← 基础提示 + Bootstrap 文件 + Runtime Context + ContextSource(记忆/技能…) + 本轮附件事实
    History QA     ← Session 历史
    User Prompt    ← 当前输入
        ↓  组装为消息列表
    Runtime/压缩：可选 compressor 对消息列表做分级压缩（折叠/丢弃/摘要由后续模块实现）
        ↓
    Current Context → LLM Call

工具 schema 由 Agent 经 `tools=` 参数随调用一起下发（见 AgentOnceRun），不混进消息体。
ContextSource / compressor 均为可插拔接缝：记忆系统、SKILL 系统、上下文压缩各自实现并注册。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import (TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol,
                    Sequence, runtime_checkable)

from .messages import Message, user
from .session import Session

if TYPE_CHECKING:  # 只在类型检查时导入，避免 context ↔ prompts 的循环
    from .prompts import PromptLibrary

PROMPT_VERSION = "2026-10-01.v2"


def prompt_fingerprint(text: str, *, version: str = PROMPT_VERSION) -> str:
    """prompt 指纹 = 版本号 + 内容哈希前 8 位。改了 prompt 文本，指纹自动变。"""
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{version}:{h}"


DEFAULT_SYSTEM_PROMPT = (
    "你是「智能创作助手」。无论任何情况，你必须始终用简体中文思考和回复。"
    "你在调用工具前对步骤的任何说明文字，也必须是简体中文，绝不允许输出英文句子——"
    "即使工具返回的是英文内容也不允许改变语言。"
    "工具失败处理：同一工具连续失败 2 次后，必须停止重试，向用户说明失败原因并询问如何处理"
    "（换素材、换参数、跳过这步或换方案），不要盲目改参数反复重试同一工具。"
    " "
    # 项目的核心原则（与 prompts/system_prompt.md 一致，漂移由 tests/test_prompt_library.py 守卫）：
    # 模型可以提方案，但不能替用户偷偷定事。它与「提问必须走弹窗」是一体两面——
    # 前者管「不许悄悄决定」，后者管「要问就问成弹窗」。
    "最重要的一条原则：你可以给用户提方案，但**不能替用户偷偷定事**。"
    "凡是会影响结果的选择（时长、出镜比例、保留哪段、用哪个模板、要不要配音、音量、风格…），"
    "只有两种合法做法：① 把它作为可见的选项交给用户（计划卡上的开关，或 ask_user 的选项卡），"
    "让用户自己挑；② 你自己判断不了就先问清楚再动手。"
    "**绝对不许**自己挑一个值直接用上却不让用户知道，也不许用「60~120 秒可选」"
    "这类模糊说法把决定权含混过去。你已经选好的默认值也要显示出来，让用户有机会改。"
    " "
    # 提问方式：这条与 prompts/system_prompt.md 必须一致（文件在就用文件，缺了回落到这里）。
    # 为什么必须写死：技能正文只说了「向用户提问」、没说怎么问，模型于是把问题写进正文——
    # 用户只能自己打字，而且经常答非所问。
    "需要用户拿主意时，一律调用 ask_user 工具，不要把问题写在回复正文里。"
    "界面上 ask_user 会弹出选项卡供用户点选；只在正文里写问题的话，"
    "用户只能自己打字，而且经常答非所问。"
    "这条没有例外——包括「请你发素材/贴链接」这类请求，也要用 ask_user 给出选项"
    "（例如「我现在上传」「我把链接贴给你」「用你已有的某条素材」），"
    "而不是在正文里要用户打字回复。"
    " "
    "什么时候必须问：多种做法各有取舍而你不确定用户想要哪种、"
    "某个目标会带来用户可能不接受的代价（例如按时长要求必须截断一句话、必须牺牲某个素材）、"
    "工具明确要求你提供创意决策参数而你又判断不了、缺少必要材料。"
    "问的时候给 2~6 个有真实差异的选项，把你建议的那个标 recommended，并允许用户自己写一条。"
    "能自己判断的不要问，也不要拿它确认「是否继续」这类空话。"
    "问完就停下等用户选，不要在同一条回复里替用户把选择也定了。"
)


@runtime_checkable
class ContextSource(Protocol):
    """向 System Prompt 追加一段上下文的来源（记忆、技能清单等在后续模块实现）。"""

    name: str

    async def render(self, query: str) -> str | None:
        """返回要注入的一段文本；返回 None / 空串表示本轮不注入。"""
        ...


# runtime_context: () -> str（返回空串表示不注入）
RuntimeContext = Callable[[], str]
# compressor: async (messages, query) -> messages（未提供则不压缩）
ContextCompressor = Callable[[list[Message], str], Awaitable[list[Message]]]


def default_runtime_context() -> str:
    return f"当前时间：{_dt.datetime.now().isoformat(timespec='seconds')}"


def _fact_line(row: Mapping[str, Any]) -> str:
    """一行素材事实：id | 文件名 | 类型 | 时长 | 分辨率 | 音轨。缺列就跳过该段。"""
    bits = [str(row.get("id") or row.get("material_id") or "?"),
            str(row.get("filename") or "(未命名)"),
            str(row.get("kind") or "?")]
    dur = row.get("duration_sec")
    if dur:
        bits.append(f"{float(dur):.1f}s")
    w, h = row.get("width"), row.get("height")
    if w and h:
        bits.append(f"{w}x{h}")
    if row.get("kind") in (None, "video"):
        bits.append("含音轨" if row.get("has_audio") else "无音轨")
    return " | ".join(bits)


def render_attachments_section(rows: Sequence[Mapping[str, Any]],
                               rejected: Sequence[str] = ()) -> str | None:
    """本轮附件 → 结构化事实段。空附件返回 None（不注入空段，省 token）。"""
    if not rows and not rejected:
        return None
    lines: list[str] = []
    if rows:
        lines.append("本条消息附带以下素材（material_id 已通过归属校验，可直接使用）：")
        lines.extend(f"- {_fact_line(r)}" for r in rows)
        lines.append(
            "剪辑链路第一步请把以上全部 material_id 原样传给 load_media(material_ids=[…])；"
            "不要改写、臆造，也不要把文件名当路径传入。"
            "不同素材用途不同（如采访视频做 ASR、空镜做画面、短视频做风格参考），"
            "全部加载后再按用户意图分配用途。"
        )
    if rejected:
        lines.append("以下附件无法使用（不存在或无权访问），请向用户说明并忽略："
                     + ", ".join(str(m) for m in rejected))
    return "\n".join(lines)


class ContextBuilder:
    def __init__(
        self,
        system_prompt: str | None = None,
        *,
        bootstrap_dir: str | Path | None = None,
        bootstrap_files: Sequence[str | Path] | None = None,
        context_sources: Sequence[ContextSource] | None = None,
        runtime_context: RuntimeContext | None = default_runtime_context,
        compressor: ContextCompressor | None = None,
        prompt_library: "PromptLibrary | None" = None,
    ) -> None:
        # 提示词优先取磁盘（prompts/system_prompt.md），取不到才用内联默认值。
        # ``system_prompt=None`` 是默认入口，表示「按提示词库解析」；
        # 显式传字符串仍然生效（测试与调用方覆盖用），语义向后兼容。
        self.prompt_library = prompt_library
        if system_prompt is None:
            system_prompt = (prompt_library.system_prompt(DEFAULT_SYSTEM_PROMPT)
                             if prompt_library is not None else DEFAULT_SYSTEM_PROMPT)
        self.system_prompt = system_prompt
        self.bootstrap_dir = Path(bootstrap_dir) if bootstrap_dir else None
        self.bootstrap_files = list(bootstrap_files or [])
        self.context_sources: list[ContextSource] = list(context_sources or [])
        self.runtime_context = runtime_context
        self.compressor = compressor

    def reload_prompts(self) -> str:
        """从磁盘重读系统提示词（热替换入口）；返回新的提示词指纹。

        指纹会变，qa.parts 里记的 prompt_fingerprint 因此能把「换过文案之后的结果」
        与旧结果区分开——这正是 eval 归因需要的。
        """
        if self.prompt_library is None:
            return self.fingerprint()
        self.system_prompt = self.prompt_library.system_prompt(DEFAULT_SYSTEM_PROMPT)
        return self.fingerprint()

    def fingerprint(self) -> str:
        """当前 system_prompt 的指纹（版本号 + 内容哈希）。eval 回归据此归因。"""
        return prompt_fingerprint(self.system_prompt)

    # ---- Bootstrap 文件（角色 / 约定等静态文件）----

    def _bootstrap_sections(self) -> list[str]:
        paths: list[Path] = []
        if self.bootstrap_dir and self.bootstrap_dir.is_dir():
            paths.extend(sorted(self.bootstrap_dir.glob("*.md")))
        for p in self.bootstrap_files:
            paths.append(Path(p))

        sections: list[str] = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                sections.append(f"<{path.name}>\n{text}\n</{path.name}>")
        return sections

    def add_source(self, source: ContextSource) -> None:
        self.context_sources.append(source)

    # ---- 组装 ----

    async def build(
        self,
        session: Session,
        current_input: str,
        *,
        attachments: Sequence[Mapping[str, Any]] = (),
        rejected_attachments: Sequence[str] = (),
        extra_sections: Sequence[str] = (),
    ) -> list[Message]:
        """装配本轮上下文。

        ``extra_sections`` 是**本轮专属**的整段文本（计划门的批准计划 / 用户诉求 /
        预注入技能正文）：它们由服务端确定性代码产出、只对这一次执行有意义，
        不该做成常驻 ContextSource 让每轮都重跑一遍检索。
        """
        parts: list[str] = [self.system_prompt]

        parts.extend(self._bootstrap_sections())

        if self.runtime_context:
            rt = self.runtime_context()
            if rt:
                parts.append(f"<runtime>\n{rt}\n</runtime>")

        for source in self.context_sources:
            section = await source.render(current_input)
            if section:
                parts.append(f"<{source.name}>\n{section.strip()}\n</{source.name}>")

        att = render_attachments_section(attachments, rejected_attachments)
        if att:
            parts.append(f"<attachments>\n{att}\n</attachments>")

        parts.extend(s for s in extra_sections if s and s.strip())

        system_content = "\n\n".join(p for p in parts if p)

        messages: list[Message] = [
            {"role": "system", "content": system_content},
            *session.messages,  # History QA
            user(current_input),
        ]

        if self.compressor:
            messages = await self.compressor(messages, current_input)
        return messages
