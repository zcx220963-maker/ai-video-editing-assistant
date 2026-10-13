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
import json
import re
from dataclasses import dataclass
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
    " "
    # 并批提速：互不依赖的工具必须放在**同一次回复**里，执行器才会并发；
    # 分成两次回复就是串行。真机实测：模型在 load_media 之后单独调 asr、
    # 拿到结果才调 split_shots，于是那条本可并行的链路退化成一前一后，
    # 白等了一整段 ASR（227s）的时间。
    "互不依赖的工具必须放进同一次回复里一起调用，系统才会并行执行它们——"
    "分成两次回复就是串行，白白多等一倍时间。"
    "剪辑链路里有几处天然的并批点：load_media 之后 asr 与 split_shots 可以同轮；"
    "asr 之后 speech_rough_cut 与 understand_clips 可以同轮；"
    "generate_script 之后 generate_voiceover 与 select_BGM 可以同轮。"
    "判断标准只有一个：**这一步需不需要另一步的产物**——不需要就同轮发出去。"
    "有依赖关系的（如 split_shots 与 understand_clips）不要同时调。"
)


@runtime_checkable
class ContextSource(Protocol):
    """向 System Prompt 追加一段上下文的来源（记忆、技能清单等在后续模块实现）。"""

    name: str

    async def render(self, query: str, *, user_id: str | None = None) -> str | None:
        """返回要注入的一段文本；返回 None / 空串表示本轮不注入。

        user_id 为当前会话用户，用于按用户隔离的 ContextSource（如技能清单）。
        """
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
        video = [r for r in rows if str(r.get("kind") or "video") != "audio"]
        audio = [r for r in rows if str(r.get("kind") or "video") == "audio"]
        if video:
            lines.append(
                "剪辑链路第一步请把以上全部 material_id 原样传给 load_media(material_ids=[…])；"
                "不要改写、臆造，也不要把文件名当路径传入。"
                "不同素材用途不同（如采访视频做 ASR、空镜做画面、短视频做风格参考），"
                "全部加载后再按用户意图分配用途。"
            )
        if audio:
            # 曲库歌曲不挂会话、也不是画面：塞进 load_media 只会被判成"纯音频"丢掉，
            # 所以单独给一条出路——出片节点的 bgm 参数直接收 material_id。
            lines.append(
                "其中音频（kind=audio）是配乐/曲目，**不要**当画面素材传给 load_media："
                "零素材出片（render_motion_code）直接把它的 "
                "material_id 传给 bgm 参数即可；"
                "有素材的剪辑链先调 select_BGM(query=歌名) 拿到引用，再交给渲染节点。"
            )
    if rejected:
        lines.append("以下附件无法使用（不存在或无权访问），请向用户说明并忽略："
                     + ", ".join(str(m) for m in rejected))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 上下文面账面（R9-T37）与预算闸（R9-T38）——§5.7 第①条 / 闸㉓ 的合同部分
#
# 为什么要有它：装配面此前只有一张**人工**量出来的字节表（§5.4），而且常驻段
# 完全不受压缩预算管——`compress.py:163-166` 把首条 system 摘出去永不压缩，
# 这条本身是骨架要求的正确行为，它的**推论**才是缺陷：系统提示词堆多少发多少，
# 没有任何东西会拦。账面把「堆了多少」变成产物，预算闸把「堆爆了」变成装配失败。
# --------------------------------------------------------------------------

# 块的三态：无条件常发 / 有条件常发 / 按需。账面只如实登记，判据在别处。
LEDGER_KINDS = ("always", "conditional", "on_demand")

# 账面在留档通道里的类型名（RunState.qa_parts 里的一行，与 prompt_fingerprint 同形）。
# 格式是装配出口定的，所以名字也定在出口这一侧；消费方（agent 侧）引这个常量，
# 别各写一遍字符串——两处各写一遍就是下一个「两份真相」。
LEDGER_ENTRY_TYPE = "context_ledger"

# 结构化装配块（系统提示词 / 运行时事实）的引用方：它们由装配本身消费。
# 写出来是为了让 orphan_sections 那条判据有明确的反面——「常驻但没人读」指的
# 是**没人声明读它**，而不是「没人声明」。
ASSEMBLY_REF = ("assembly",)

# §5.4 表末「预算」列的**可执行副本**（字节）——**这一列是闸㉓ 的真断值**，
# 口径同附录 B（P95 × 1.15），只是 P95 还没跑：§8.1 十二支夹具的矩阵要等真机
# 首轮，所以今天用的是**实测现值 × 1.15**（对固定形状的块）或**结构上限的字节
# 折算**（对内容由用户/白名单决定的块）。十二支夹具跑完后按真 P95 替换，并
# **同批**写回 §5.4 表——两处不同步由 tests/test_context_hygiene.py 的对账断言判红。
#
# 为什么不能照抄 §5.4 的「目标」列当断值：那几个数是写文档时按现值上浮拍的，
# 而实测下来有三块**今天就已经超**（`<skills>` 清单 3,156 B vs 1,536 B、
# 技能正文 31,186 B vs 16,384 B、`<memory>` 上限折算 13,312 B vs 2,048 B）——
# 拿它们当断值，等于给每一条真机 run 判死刑。目标归目标（见 SECTION_BUDGET_DEBT，
# 每个超目标块都挂着欠哪个任务）；断值归断值（下面这张表，今天就得能过）。
#
# 没列进表的块 = 不限量（`<runtime>`、`<attachments>`、按需 demo 语料）：
# 「没预算」是明确的口径，不是漏填。
SECTION_BUDGET_BYTES: dict[str, int] = {
    # 固定形状的块：实测现值 × 1.15，拦的是「改文案时悄悄堆上去」
    "system_prompt": 3257,        # 实测 2,832（prompts/system_prompt.md）
    "skills": 3630,               # 实测 3,156（6 份清单连内层 <skills> 标签）
    # **T42 换口径**：旧值 35,864 = 31,186（那时整段注入）× 1.15。今天注入的是
    # `Skill.assemble` 拼出来的那一段，最坏一档＝默认集 **24,223 B** + 可选块全给上
    # （T42 时八块 = 35,658 B；**T62 起十一块 = 40,796 B**，两个数都是实测，
    # 含块与块之间的 `\n\n` 连接符，见下面那一段）
    # （全给上在真机上不会发生——`patch-motion` 与 `image-channel` 由不同节点触发、
    # 开关没动时 `film-knobs` 也不发、`whiteboard-imagegen` 只在计划里有 `imagegen`
    # 时才发——但断值要按**能拼出来的最大那一份**算）。
    # 重算仍按同一口径（实测现值 × 1.15）：40,796 × 1.15 ≈ 46,915——但**没有重算**，
    # 断值仍是 T42 那一档按当时最大装配定下的 41,007（35,658 × 1.15 ≈ 41,007）。
    # **这不是放水**：旧断值在 T42 时只剩 500 B 余量，再写一节说明就会把真机 run
    # 判死（§9-Q106 ③）。
    #
    # **T57 之后这个断值刻意没跟着重算**：新加的两块（`asset-parts` 2,260 B +
    # `scale-safe` 1,622 B，通道条件＝motion **且** image）把最大装配推到 36,914 B，
    # 按同一口径该写 42,451——但那条路是「每加一节就抬一次天花板」，抬两次这一行
    # 就不再是一把尺。所以断值保持 41,007：下一次要往常发块里加内容的人，第一个该看
    # 的就是余量这个数——先回答「砍哪一节」，而不是改这一行（Q55 的规矩）。
    # （旧注这里写的 39,540 / 45,471 / 余量 1,467 是**手算装配**得出的：按今天同一把
    # 尺用 `Skill.assemble` 回算是 36,914 / 42,451 / 余量 4,093。手算每块相加会比真装配
    # 大 1~2 KB，**字节数一律以 `Skill.assemble` 的实测为准**，别在注释里做加法。）
    #
    # **T62 起是十一块、40,796 B，余量 211 B**（实测）。这一节（`whiteboard-imagegen`）
    # 最后定在 1,256 B **不是**因为写短了更好读，是因为上面那条规矩：T57 之后剩下的
    # 余量只有 4,093 B（真装配口径），一节写超了就等于**逼着这一行改数**。它今天仍然
    # 装得下，代价是这一节只剩「四条硬规矩 + 三句话 + 一句今天没判据」。
    # 第十二节再来时，211 B 是明摆着的墙——先砍，别抬（Q55）。
    # **真机上到不了 40,796**：它要求写代码腿、供给腿、出图腿、动过的整片开关、
    # 旁白/配乐、局部改六件事**同时**成立。最常见的代码链（`plan_motion` +
    # `render_motion_code`、有旁白、没动整片开关）实发 **28,698 B** = 默认集 24,223 +
    # `motion-anchors` 1,460 + `subject-naming` 1,263 + `text-only-quota` 819 +
    # `sound-design` 933 + 4 个 `\n\n` 连接符。**旧注写的 32,881 拼不出来**——同一把尺
    # 下没有哪种装配能得到这个数（T57 那笔 39,540 是同一个来源的笔误）。
    "skill_instructions": 41007,
    # 内容由上限决定的块：断值 = 结构上限的字节折算，拦的是「有人把上限删了/放开了」。
    # T39 之后记忆改「按条召回 + 不截字」，注入量由自己的上限卡死（memory.py：
    # total_bytes=1400 + per_category_bytes=700），最坏情况（全 CJK、两段标题、
    # 未注入回执带 ISO 时间戳、USAGE_NOTE ≤120 B）≈ 1,800 B。
    # 断值取 §5.4 的目标 2048：结构上界（1,800）与目标之间那点余量留给标题/回执措辞，
    # **严于目标就等于把目标当闸**（§9-Q100 ①），而这里要拦的是「上限被放开」。
    "memory": 2048,
    # **T40 就地修正**：旧值 8,192 B 是「金样 4,834 B × 2」，而那份金样用的是**桩门**
    # （3 个节点、每节点一个开关）——真白名单是 28 个节点（2026-10-13 在 `creation-app`
    # 里接真 Storyline 实测），全命中时这一段 13,022 B，连最常见的视频链
    # （prior/待续跑命中 plan_motion + render_motion_code）都是 8,982 B：
    # 旧值把**正常轮次**判死（`ContextBudgetError` → 请求不发出）。
    # 重算仍按同一口径（实测现值 × 1.15），只是「现值」换成真机的，且取**内容上限**——
    # T40 之后这一段是内容有界的块（`knobs` 只发命中集，命中集 ⊆ 白名单，无命中整块不发），
    # 内容上限是唯一不会把合法轮次判死的数：13,022 × 1.15 ≈ 14,975 → 14,976。
    # 段内的用户可控文本（feedback_quote / pending_asked 是用户原话，neutralize 只防注入
    # 不截长）仍在这条线之内。**这不是「为跑通而抬预算」**：取证与 Q55 第②种出路的
    # 论证见 §9-Q104；§8.1 拿到真 P95 后再按 P95 × 1.15 收紧（同批改本节与指南）。
    "planning_round": 14976,
    # 工具面（T41 随裁剪一起上线，§5.4.1 那一行）。断值取 §5.4 的目标 40,960 B——
    # 这一栏的目标与断值**同数**是有意的，理由与别块相反：别块的目标是「R9 该瘦到哪」
    # （今天做不到，所以断值另算），而这一栏的目标就是「执行轮工具面 ≤40 KB」这条
    # 验收口径本身，它今天必须过（真机实测：按计划裁后 14,561 B，裁剪前 54,140 B）。
    #
    # **只在按计划裁过的那一轮上判**（见 ``tools_box`` 的注释）：未裁的轮次发的是全量
    # 能力面，53 KB 上下是它的正常尺寸——拿这条线去判它们等于让闲聊轮全死，
    # 那正是 §9-Q100 ③ 当初不敢设这个数的原因。未裁那一档的账与取舍登记在 §9-Q105。
    "tools": 40960,
}

# §5.4 的「目标」列：R9 要把这几块瘦到哪。**与上面那张断值表是两件事**——
# 断值拦「今天会不会爆」，目标说「最终该多小」。账面上两种超线分开记，因为
# 「今天没超」不等于「达标了」（§5.4 末列的「目标」数就是这一列）。
SECTION_TARGET_BYTES: dict[str, int] = {
    "system_prompt": 3072,
    "skills": 1536,
    "memory": 2048,
    "planning_round": 5120,
    "knobs": 1228,
    "skill_instructions": 16384,
    "tools": 40960,
}

# 今天**已经超目标**的那几块，逐块登记欠哪个任务（键必须同时在 SECTION_TARGET_BYTES 里）。
# 为什么要有这张表：没有它，三块已知超标的东西会被 ×1.15 的断值洗成绿色，
# R9 剩下的活也就没人记得了——账面要能区分「合规」与「还没还完的账」。
#
# 三个数一行：目标字节、实测现值（字节，方向）、欠哪个任务。
# 现值口径在 T40 纠了一次：原先的 `knobs`（1,829）与 `planning_round`（4,834「金样」）
# 都是照**桩门**（3 个节点）量的，真白名单是 28 个节点 —— 桩门量出来的数会把
# 「真机上正常的一轮」写成超标或达标的反面。现值一律取**真机内容上限**（全命中），
# 与断值同源（§9-Q104）。
SECTION_BUDGET_DEBT: dict[str, tuple[int, int, str]] = {
    "skills": (1536, 3156,
               "描述瘦身（**没有任务号**）：T43 只去重内层 <skills> 标签，省不到 40 B"),
    "knobs": (1228, 9870,
              "T40 只改了**什么时候发**（非命中轮 0 B / 视频链 6,121 B / 全开 9,870 B），"
              "没瘦身——命中集的开关文案本身还超目标，瘦身**没有任务号**"),
    "planning_round": (5120, 13022,
                       "T40 已条件化（冷启动 2,275 B、口播链 4,384 B 进目标了），"
                       "剩下的 6～9.9 KB 全是 `knobs`——与上一行同欠、**没有任务号**"),
    "skill_instructions": (16384, 28698,
                           "T42 只还了一半：分段取块已落地（`plan/skills.py"
                           " sections_for_plan` + `skill.py assemble_sections`，"
                           "`motion_explainer` 默认集 31,762 → 24,296 B），但 **16,384 B 的目标"
                           "今天没到**——最常见的代码链一轮实发 ≈28.8 KB（默认集 + T36 三节 +"
                           " 声音块），差的那些是「写代码必须随身的合同」（选卡方法/适配 demo/"
                           " 可用 import/代码模板/七栏/十一道闸/通道表/⑮⑯/字体白名单/TOKENS 的"
                           " 合计 21 KB）。**再切就是砍合同不是砍脂肪**：每一块都有闸在真读它，"
                           "砍掉只会把闸前拦截换成事后打回。**没有任务号**，要还它得先回答"
                           "「哪一块不给了」。现值取**最常见代码链一轮的实发量**（离线装配实测），"
                           "不是全块全给的那一档（T62 起十一块全给上 40,796）"),
}

_AUTO_NAME_RE = re.compile(r"<\s*([A-Za-z_][\w.\-]*)([^>]*)>")
_NAME_ATTR_RE = re.compile(r"""name\s*=\s*["']([^"']+)["']""")


class ContextBudgetError(RuntimeError):
    """㉓ 预算闸：某个常发块的字节顶穿了 §5.4 的断值（`context_ledger` 里那个），请求**不发出**。

    刻意抛错而不是就地截断：截断是静默丢内容——`memory.py:116-121` 的「保尾部」
    正是这个形状（看着还在，其实丢了头部）。这条链的规矩是「少带可以，但要说出来」，
    说出来之后由人决定是压内容还是抬预算（抬预算要走 Q55，不许临场改）。
    """


def section_budget(name: str) -> int | None:
    """块名 → 预算字节；没进表 = 不限量（返回 None）。

    带 ``:`` 的动态块名（``skill_instructions:motion_explainer``）按前缀取：
    预算是按**层**定的，不是按每份附件定。
    """
    if name in SECTION_BUDGET_BYTES:
        return SECTION_BUDGET_BYTES[name]
    return SECTION_BUDGET_BYTES.get(name.split(":", 1)[0])


def section_target(name: str) -> int | None:
    """块名 → §5.4 的**目标**字节（不是断值）；没进表 = 本文档没给它定目标。"""
    if name in SECTION_TARGET_BYTES:
        return SECTION_TARGET_BYTES[name]
    return SECTION_TARGET_BYTES.get(name.split(":", 1)[0])


def section_bytes(text: str) -> int:
    """块的字节数。口径与 §5.7 一致：UTF-8 编码后的字节（不是字符数）。

    为什么不用 `compress.py` 的 token 估算当账面：账面要能被逐字核对
    （人对着 §5.4 表复核），token 是估算量、会随计数器改口径而漂。
    两者分工：字节进账，token 进压缩预算（`estimate_tokens`，那条链不在这里）。
    """
    return len(text.encode("utf-8"))


def section_name(text: str) -> str:
    """裸段文本 → 块名：取首个标签，带 ``name="X"`` 就接上。

    ``<planning_round>`` → ``planning_round``；
    ``<skill_instructions name="motion_explainer">`` → ``skill_instructions:motion_explainer``。
    认不出标签时回 ``"unlabeled"``——**不猜**：账面里出现 unlabeled 就是有人
    在往装配里塞不带身份的裸串，值得看见。
    """
    m = _AUTO_NAME_RE.match(text.lstrip())
    if not m:
        return "unlabeled"
    tag = m.group(1)
    attr = _NAME_ATTR_RE.search(m.group(2) or "")
    return f"{tag}:{attr.group(1)}" if attr else tag


@dataclass(frozen=True)
class Section:
    """一个**带身份**的装配块。

    裸字符串仍然能用（`build(extra_sections=[…])` 会自动取名），但那样账面里
    只有名字、没有条件键与引用方：`kind` 一律 `on_demand`、`referenced_by` 空。
    产出方知道得更多就该说更多——`knobs` 的字节、条件键、谁读它，都只有产出方知道。

    ``sub_blocks``：这一段里**另有独立预算**的子块（现在只有规划轮段里的
    `knobs` / `session_history_rows`——它们的字节已含在 ``text`` 里，
    所以求和会重复计；账面因此是「逐块看」，不是「加起来看总账」）。
    """

    name: str
    text: str
    kind: str = "on_demand"
    condition_key: str | None = None
    referenced_by: tuple[str, ...] = ()
    sub_blocks: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in LEDGER_KINDS:
            raise ValueError(f"kind 必须是 {LEDGER_KINDS} 之一，收到 {self.kind!r}")


def tools_box(definitions: Sequence[Mapping[str, Any]], *,
              total_registered: int | None = None,
              filter_name: str | None = None) -> dict[str, Any]:
    """本轮真发出去的工具 schema → 账面上的 ``tools`` 栏。

    ``sent`` 是**这一轮实际下发**的份数，``total_registered`` 是注册表里全部可见份数：
    两者的差就是「按轮裁剪」省下的量（T41 的判据读它）。``largest`` 取字节最大的三份，
    够定位「谁在顶预算」——列全了反而没人看。

    T41 补两栏（都是为了判据能被**逐字复核**，不是为了让账面好看）：
    ``bytes`` 是这一轮工具面的总字节（``tools`` 断值判的就是它），
    每份最大的工具另带 ``desc_bytes``——§5.7 第⑥条的「描述 ≤600 B」是**描述**的线，
    而整份 schema 的字节由入参形状决定（分镜那种节点天然近 2 KB，砍它就等于砍掉参数）。
    两个数分开记，判据才不会含混成「工具该多小」。
    """
    rows = []
    for d in definitions:
        fn = d.get("function") or {}
        rows.append({"name": str(fn.get("name") or "?"),
                     "bytes": section_bytes(json.dumps(d, ensure_ascii=False)),
                     "desc_bytes": section_bytes(str(fn.get("description") or ""))})
    rows.sort(key=lambda r: (-r["bytes"], r["name"]))
    total = sum(r["bytes"] for r in rows)
    # 断值只在**按计划裁过的那一轮**上判（T41）。理由：未裁的那些轮次（闲聊 / 定时任务）
    # 发的是全量能力面，53 KB 上下是它的正常尺寸，拿 40 KB 去判它们等于让闲聊轮全死
    # （§9-Q100 ③ 那条顾虑的原样）。裁剪过的一轮则相反——它的字节数完全由我们控制，
    # 超线就说明裁剪逻辑坏了（或者有人把白名单放大了一个量级），那时必须响。
    budget = section_budget("tools") if filter_name else None
    return {
        "sent": len(rows),
        "total_registered": total_registered,
        "filter": filter_name,
        "bytes": total,
        "budget": budget,
        "over_budget": bool(budget is not None and total > budget),
        "largest": rows[:3],
    }


def check_tools_budget(box: Mapping[str, Any]) -> None:
    """``tools`` 的断值（§5.4.1 那一行；T41 起真生效）：超线即抛，**请求不发出**。

    与逐块那条（``ContextBuilder._over_budget_message``）同一个形状与同一个出路：
    压回去，或按 Q55 走流程抬预算（两处同批改）。不在这里就地截工具：
    静默少给一个节点，模型只会撞上去才知道，那是「看着还在、其实没了」的另一种写法。
    """
    if not box.get("over_budget"):
        return
    top = "、".join(f"{r.get('name')}({r.get('bytes')} B)"
                    for r in box.get("largest") or [])
    raise ContextBudgetError(
        "上下文预算闸㉓：本轮工具面超出 §5.4 的断值，本次请求不发出（不许截工具顶过去）：\n"
        f"- tools（filter={box.get('filter')}）：{box.get('bytes')} B > 预算 "
        f"{box.get('budget')} B，下发 {box.get('sent')}/"
        f"{box.get('total_registered')} 份 schema；最大的三份：{top}。\n"
        "出路只有两条：把工具面压回预算内（T41 的按计划裁剪就是这件事），"
        "或按 Q55 走流程调预算（两处同批改：context.py 的 SECTION_BUDGET_BYTES 与 §5.4 表）。")


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
        # 最近一次 ``build`` 产出的账面（§5.7 第①条格式）。装配方读它留档、
        # agent 侧往 ``tools`` 栏补工具面。没跑过装配时为 None——**None 不是空账**，
        # 谁读到 None 就该当「未判」，不许当成「没有超预算」。
        self.last_ledger: dict[str, Any] | None = None

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
        extra_sections: Sequence[str | Section] = (),
        recall_terms: str = "",
    ) -> list[Message]:
        """装配本轮上下文。

        ``extra_sections`` 是**本轮专属**的整段文本（计划门的批准计划 / 用户诉求 /
        预注入技能正文）：它们由服务端确定性代码产出、只对这一次执行有意义，
        不该做成常驻 ContextSource 让每轮都重跑一遍检索。
        元素可以是裸串（自动命名）或 :class:`Section`（带条件键与引用方的好账）。

        ``recall_terms``（R9-T39）是本次**装配方才知道**的额外召回词——执行轮的计划
        步骤名就属于这一类：它们在 ``extra_sections`` 里，而 ``ContextSource.render``
        拿到的只有用户消息。把它拼进 query 交给产出方（现只有 ``memory`` 用它做关键词
        召回），比让每个 source 自己去翻计划诚实：装配方知道自己这轮在干嘛。

        返回前产出一本账（``self.last_ledger``，格式见 §5.7 第①条）；
        ``over_budget`` 非空即抛 :class:`ContextBudgetError`——**请求不发出**。
        """
        query = f"{current_input}\n{recall_terms}".strip() if recall_terms else current_input
        sections: list[Section] = []

        def _add(sec: Section) -> None:
            # 空段不进账面也不进提示：账要能逐行对上真发出去的东西，
            # 记一条 0 字节的段只会让人怀疑「它到底发了没有」。
            if sec.text and sec.text.strip():
                sections.append(sec)

        _add(Section(name="system_prompt", text=self.system_prompt,
                     kind="always", condition_key=None, referenced_by=ASSEMBLY_REF))
        for text in self._bootstrap_sections():
            _add(Section(name=section_name(text), text=text, kind="always",
                         condition_key=None, referenced_by=ASSEMBLY_REF))

        if self.runtime_context:
            rt = self.runtime_context()
            if rt:
                _add(Section(name="runtime", text=f"<runtime>\n{rt}\n</runtime>",
                             kind="always", condition_key=None,
                             referenced_by=ASSEMBLY_REF))

        for source in self.context_sources:
            body = await source.render(query, user_id=session.user_id)
            if body:
                # 三个可选属性由 ContextSource 自己声明（协议仍是 `name` + `render`）：
                # 装配侧不认识「记忆」或「技能」是什么，只认它自报的身份与消费者。
                _add(Section(
                    name=str(source.name),
                    text=f"<{source.name}>\n{body.strip()}\n</{source.name}>",
                    kind=str(getattr(source, "ledger_kind", "always")),
                    condition_key=getattr(source, "ledger_condition_key", None),
                    referenced_by=tuple(getattr(source, "ledger_referenced_by", ()) or ()),
                ))

        att = render_attachments_section(attachments, rejected_attachments)
        if att:
            _add(Section(name="attachments", text=f"<attachments>\n{att}\n</attachments>",
                         kind="conditional", condition_key="attachments",
                         referenced_by=("load_media",)))

        for item in extra_sections:
            if isinstance(item, Section):
                _add(item)
            elif item:
                _add(Section(name=section_name(item), text=item))

        system_content = "\n\n".join(s.text for s in sections)

        ledger = self._ledger(sections)
        self.last_ledger = ledger
        if ledger["over_budget"]:
            raise ContextBudgetError(self._over_budget_message(ledger))

        messages: list[Message] = [
            {"role": "system", "content": system_content},
            *session.messages,  # History QA
            user(current_input),
        ]

        if self.compressor:
            messages = await self.compressor(messages, current_input)
        return messages

    # ---- 账面 ----

    def _ledger(self, sections: Sequence[Section]) -> dict[str, Any]:
        """§5.7 第①条的账面格式。四个键的字段名与含义**冻结**，别在别处记账。"""
        rows: list[dict[str, Any]] = []
        for sec in sections:
            rows.append(self._ledger_row(sec.name, sec.text, sec.kind,
                                         sec.condition_key, sec.referenced_by))
            for sub_name, sub_text in sec.sub_blocks:
                if sub_text and sub_text.strip():
                    # 子块的 kind/条件键跟着父段：它有没有出现，取决于父段的守卫，
                    # 而那个守卫名只有产出方知道（现在由产出方写进父段的 condition_key）。
                    rows.append(self._ledger_row(sub_name, sub_text, sec.kind,
                                                 sec.condition_key, sec.referenced_by))
        over = sorted({r["name"] for r in rows
                       if (b := section_budget(r["name"])) is not None and r["bytes"] > b})
        # 「超目标」与「超断值」分开记，因为它们是两件事：前者是 R9 还在还的账
        # （每次都出现在账面上，直到那个任务落地），后者才是**这次请求不发出**。
        over_target = sorted({r["name"] for r in rows
                              if (t := section_target(r["name"])) is not None
                              and r["bytes"] > t})
        orphan = [r["name"] for r in rows if r["kind"] == "always" and not r["referenced_by"]]
        return {
            "sections": rows,
            # 工具面要等 agent 侧拿到 registry 才填得出来（那里才知道注册了多少）；
            # 这里先给一格**未判**的空位，而不是伪造 0。
            "tools": {"sent": None, "total_registered": None, "filter": None,
                      "largest": []},
            "over_budget": over,
            "over_target": over_target,
            "orphan_sections": orphan,
        }

    @staticmethod
    def _ledger_row(name: str, text: str, kind: str, condition_key: str | None,
                    referenced_by: tuple[str, ...]) -> dict[str, Any]:
        return {"name": name, "bytes": section_bytes(text), "kind": kind,
                "condition_key": condition_key, "referenced_by": list(referenced_by)}

    @staticmethod
    def _over_budget_message(ledger: Mapping[str, Any]) -> str:
        """报红要说清三件事：哪块超、超了多少、预算从哪来。只说「超预算了」等于没说。"""
        lines = ["上下文预算闸㉓：以下装配块超出 §5.4 的断值，本次请求不发出（不许截断顶过去）："]
        by_name = {r["name"]: r for r in ledger.get("sections") or []}
        for name in ledger.get("over_budget") or []:
            row = by_name.get(name) or {}
            budget = section_budget(name)
            lines.append(f"- {name}：{row.get('bytes')} B > 预算 {budget} B"
                         f"（kind={row.get('kind')}，条件键={row.get('condition_key')}）")
        lines.append("出路只有两条：把内容压回预算内，或按 Q55 走流程调预算"
                     "（两处同批改：context.py 的 SECTION_BUDGET_BYTES 与 §5.4 表）。"
                     "注意这不是「目标」列——目标见 SECTION_BUDGET_DEBT，那是 R9 要还的账，"
                     "不在这里硬拦。")
        return "\n".join(lines)
