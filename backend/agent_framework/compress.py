"""上下文压缩：在窗口预算内分级裁剪历史。

对应设计文档「上下文压缩」的三级递进策略（仅当估算 token 超过预算时逐级触发）：
  第一级  折叠 Tool 的信息：把较旧的大段工具结果压成预览，只保留最近若干条完整结果。
  第二级  丢弃太过久远的消息：仍超预算时，从最早的历史消息开始淘汰。
  第三级  对过往消息做全量摘要：把被淘汰的消息压成一条摘要，保留要点。

被折叠/丢弃/摘要后始终保留：首条 system、最后一条当前 user 输入。

本模块产出的是 ContextBuilder 需要的 compressor 接缝：
    async def __call__(messages, query) -> messages
默认摘要器不依赖 LLM（确定性）；生产可用 make_llm_summarizer(llm) 换成模型摘要。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Iterable

from .llm import LLMClient
from .messages import Message
from .summary_store import SummaryStore, summary_key

# token 计数器：Message -> int
TokenCounter = Callable[[Message], int]
# 摘要器：list[Message] -> 摘要文本（可异步）
Summarizer = Callable[[list[Message]], "Awaitable[str] | str"]

FOLD_MARKER = "已折叠工具结果"


def default_token_counter(message: Message) -> int:
    """无 tiktoken 依赖的估算：**中文按字数算**，西文按 ~4 字符/词元。

    原先一律 ``len(text) // 4``——对中文偏低 4~6 倍。真机后果是整个压缩闸门形同虚设：
    1360 个中文字被算成 344 token（实际 1~1.5 token/字），101 条消息 151844 字符被算成
    38305，而真实占用已过十万。于是「超预算就压缩」永不触发，直到模型自己报
    上下文超限——表现就是「聊着聊着/剪着剪着莫名其妙断了」。

    判据：CJK（含中日韩标点、全角字符）一个字≈1 token；其余按 4 字符≈1 token。
    比装 tiktoken 更省事，且对中英混排的实际偏差在可接受范围内。
    """
    text = str(message.get("content") or "")
    if message.get("tool_calls"):
        text += json.dumps(message["tool_calls"], ensure_ascii=False)
    return estimate_tokens(text) + 4


def estimate_tokens(text: str) -> int:
    """中英混排的 token 估算：CJK 逐字计 1，其余每 4 字符计 1。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        # CJK 统一表意文字 / 扩展A / 兼容表意 / 中日韩符号与标点 / 全角
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
                or 0xF900 <= code <= 0xFAFF or 0x3000 <= code <= 0x303F
                or 0xFF00 <= code <= 0xFFEF or 0x3040 <= code <= 0x30FF
                or 0xAC00 <= code <= 0xD7AF):
            cjk += 1
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk



async def default_summarizer(dropped: list[Message]) -> str:
    """无 LLM 的确定性摘要：概述被压缩的消息，保留最早的用户诉求片段。"""
    n = len(dropped)
    first_user = next(
        (str(m.get("content") or "") for m in dropped if m.get("role") == "user"), ""
    )
    return f"（历史摘要）先前 {n} 条对话已压缩省略。早期诉求：{first_user[:80]}"


def make_llm_summarizer(llm: LLMClient, max_chars: int = 500) -> Summarizer:
    """用 LLM 对淘汰消息做全量摘要（生产用）。"""

    async def summarize(dropped: list[Message]) -> str:
        transcript = "\n".join(
            f"{m.get('role')}: {str(m.get('content') or '')[:max_chars]}" for m in dropped
        )
        resp = await llm.complete(
            [
                {
                    "role": "system",
                    "content": "把以下对话历史压缩成一段要点摘要，保留关键事实与决定：",
                },
                {"role": "user", "content": transcript},
            ]
        )
        return resp.content or await default_summarizer(dropped)

    return summarize


# ---------------------------------------------------------------------------
# 执行循环里的工具结果闸门（活列表）
#
# 与上面那个 ContextCompressor 的分工：压缩器跑在 ``ContextBuilder.build()`` 里，
# 也就是**一轮 run 开始前**、一条工具结果都还没产生的时候。它管的是「会话历史」
# （user/assistant 行）。而一次 run 内部的 ReAct 循环每轮都会把**全部**已发生的
# 工具结果重新发一遍，这一段此前**完全没有闸门**——真机 run a3d8ff08098a：
# 40 轮下来首发提示词 258K token、整条 run 累计 5.7M token，其中大头就是
# ``load_skill`` 的整篇正文与 ``fetch_url`` 的整页网页被逐轮重发。
# 下面这层补的就是这一段。
# ---------------------------------------------------------------------------

#: 单条工具结果超过这个体积就截断（字节）。
#: 这个数按「一份 demo 源码要能整份进来」定：真机 run 97c1289eba35（2026-10-15）
#: 闸⑫ 骨架相似度 9 镜未过（0.14~0.54 / 阈值 0.61），根因就是原先的 24,000 把每份
#: demo 的尾部 50~110 行切掉了——而尾部正是主组件与 export，骨架比对最吃重的部分。
DEFAULT_RESULT_MAX_BYTES = 60_000

#: 活列表里全部工具结果的合计体积超过这个数，就开始折叠更早的那些（字节）。
#: 原先的 96_000 比的是「单条的上限」：把单条改到 60_000 之后，两份完整 demo 就能把活列表撑过 96_000，
#: 第二轮写码要用的那一批被折成 200 字符预览——模型又变回凭记忆手搓，闸⑫ 的相似度回到 0.14~0.54。
#:
#: 160_000 → 400_000（2026-10-18 真机 run 66c0b56541ca）：模型写分镜要同时记着技能正文
#: （≈86 KB）与卡库索引（≈58 KB），≈144 KB 贴着 160 KB 那条线；42 份网页一进来就越线，
#: 折叠切掉一块正在用的材料，它只好重读——那条 run 把 1200 行正文按 120 行一窗从头读了
#: 三遍、索引读了 8 遍，104 轮一张分镜都没交。门槛定低并不省钱：缓存命中时多带 100 KB
#: 上下文每轮只多约 0.002 元（DeepSeek 命中价 0.02 元 / 百万），真正贵的是「折了再重读」。
#: 再配上折叠顺序（研究结果先折、技能材料最后折，见 `ToolResultGate.shrink`），
#: 正在用的那份设计材料就不会被几十份看过的网页挤出去。
#:
#: 400_000 → 1_000_000（同日，按价目表与实测算的）：这条链实测 1 token ≈ 2.7 字节，
#: 1 MB 工具结果 ≈ 37 万 token；模型窗口 1M token。折叠只数**工具结果**，模型自己写的
#: 代码（写文件 / 改文件 / 交分镜，真机一条约 67 万字节）另占窗口，最坏合计约 60 万 token，
#: 仍有余量。钱的账：缓存命中 0.02 元 / 百万，30 万 token 的一轮约 0.006 元；而每折一次
#: 就改写一次历史，那一轮按未命中计价（1 元 / 百万，贵 50 倍）。所以折得越少越省——
#: 这条线只当「别撑爆窗口」的应急闸用，正常一条片子触发不到它。
DEFAULT_FOLD_AT_BYTES = 1_000_000

#: 看过就消化了的「原料」类工具：越线时**先折**它们。技能正文与卡库（`load_skill`）是模型
#: 正拿着写代码的设计材料、而且重取最贵，排在**最后**折。
RESEARCH_TOOLS = frozenset({"web_search", "fetch_url", "fetch_media"})

#: 一旦触发折叠，就一口气折到这个比例（相对 ``fold_at_bytes``）才停——**回差**。
#:
#: 为什么非要有回差（2026-10-17，钱烧在哪）：原先的停止条件是「刚好压回门槛以下」，
#: 于是越线之后**每来一条新结果就要回头再折一条更早的**——每一轮都在改写历史中段。
#: 模型服务的提示词缓存是按**前缀**命中的：中间改一个字，从那里往后整段都按「未命中」
#: 计价，而未命中是命中的 50 倍（DeepSeek：1 元 vs 0.02 元 / 百万 token）。真机 run
#: 32affa23c933 改写了 53 次历史，估算九成的钱花在这上面。
#: 折到一半意味着下一次要再涨 ``fold_at × 0.5`` 才会再动历史：改写从「每轮一次」
#: 降成「偶尔一次」，代价是那一次多折掉几条——那几条本来也是更早、更不在用的。
DEFAULT_FOLD_TO_RATIO = 0.5

#: 最近这么多条工具结果永不折叠——模型当前正在用的那一批必须完整。
DEFAULT_KEEP_RECENT_RESULTS = 3

#: 折叠后保留的预览字符数。
DEFAULT_FOLD_PREVIEW = 200

TRUNCATE_MARKER = "结果已截断"

#: 截断回执里那半句「怎么把剩下的拿回来」，**按工具分**。
#: 为什么不能一句话通用：工具不同，路就不同。原先那半句列的是「读文件带 offset/limit、
#: 检索限定路径、分页取列表」，对 `load_skill` 一条都不成立——技能正文不在会话工作区
#: 里（`read_file` 打不开 `skills/…`，检索也没有那个起点），也没有「换关键词重取」这
#: 一说。真机实测（14 镜「薄肌理论」）：模型读到「middle was truncated」之后照着这句
#: 话去 read_file 撞「文件不存在」、去检索撞「检索起点不存在」，然后只能把同一份技能
#: 反复加载（每次切点还一样），十几次调用全烧在找回那段上。**回执给一条走不通的路，
#: 比不给更坏**——模型会一条条试。默认那句是给没列在这里的通用工具用的，一字不改。
_RECOVERY_HINT = {
    # 这句话亲手教出过「一行一行翻」：真机看到「按行续读」就写 limit=1，
    # 一枪一行磨穿迭代预算（2026-10-17 demo 80 枪、2026-10-18 pipeline.md 200 轮）。
    # 续读要教的是**整份 / 大页**，不是按行。
    "load_skill": ('再用 load_skill 把同一份 name（附件加 path）**一次取回**——'
                   '不带 offset/limit 就是整份，带分页就把 limit 给到 100 行以上；'
                   '技能文件不在会话工作区里，read_file / grep 找不到它们；'
                   '不要一行一行地翻'),
    "read_file": ("再用 read_file 换一个覆盖范围（offset 定位、limit 给到 100 行以上），"
                  "不要一行一行地翻"),
    "grep": "用 grep 换更窄的 pattern、或限定 path/glob 再检索一次",
    "fetch_url": "换一条更具体的链接（这一份太长，抓取工具没有「只取某一节」的参数），"
                 "或换一个源再抓",
    "web_search": "换更具体的关键词、或换一个源站再检索一次",
}
_DEFAULT_RECOVERY_HINT = ("用更窄的参数重取一次（读文件带 offset/limit、检索限定路径、"
                          "分页取列表）")

#: 这些工具的结果里带着「模型必须原样引回去」的 id / 候选清单，永不折叠。
#: 截断那头仍然只切中间（头尾都留），所以就算被折叠，id 也还在预览的开头。
NEVER_FOLD_TOOLS = frozenset({"submit_plan", "confirm_plan", "ask_user"})

#: ``load_skill`` 整份输出里「索引到此为止」的分隔行。折叠时**切在它前面**，于是留下来
#: 的恰好是附件清单 + 分段目录，正文（几十 KB）整段丢掉。
#:
#: 它是生产者（``skill.LoadSkillTool``）与消费者（本模块 ``shrink``）之间唯一的约定，
#: 所以两边引的是同一个常量，不各写一份。
SKILL_INDEX_END = "── 以上是索引（附件清单 + 分段目录），以下是正文 ──"

#: 折叠后要**整段保留索引**的工具，而不是只留开头 ``fold_preview`` 个字符。
#:
#: 通用折叠对绝大多数工具够用：参数是已知的，重调一次就回来了。对 ``load_skill``
#: 不成立——它的开头是「位置行 + 附件清单 + 章节目录」，**那就是这份结果的索引**，
#: 而「换个更窄的参数重取」这条路**唯一需要的东西正是它**。切掉索引，等于把恢复路径
#: 和走这条路要用的钥匙一起拿走。
#:
#: 真机代价（2026-10-16，run b7df827ebed4，「薄肌理论」20 镜那条）：模型拿到的折叠回执
#: 说「需要原文就重新调用一次该工具（用更窄的参数）」，可它手里只有 200 字符——够看见
#: 3 个附件名（共 315 个）、一个 section 键名都没有。于是它照着「更窄的参数」去猜：
#: `references/film-api.md`（这个技能根本没有 references/ 目录）、`references/palettes.json`、
#: `section=film-api`、`section=palettes`——四次全错，熔断提示随即出现。它转而用 `grep`
#: 在会话工作区找 `CaptionText`（工具说明白纸黑字写着技能文件不在工作区里，找不到），
#: 最后得出**假结论**：「技能附件读不到，这是环境限制，不是我的检索方式问题」——
#: 把整条 20 镜片子的阻塞归因到一个并不存在的问题上，还写进了给用户的答复。
#:
#: 真答案就在被切掉的那几行里：`demos/_fixtures/Fixtures.tsx`（35 KB，`CaptionText` 在那里）。
INDEX_FOLD_TOOLS = frozenset({"load_skill"})


def truncate_result(text: str, limit: int, *, tool: str = "") -> str:
    """结果文本超过 ``limit`` 字节时截成「头 + 回执 + 尾」；没超就原样返回。

    为什么头尾都留、只切中间：头是「这个工具成功了、给了什么」，尾常带「共 N 条 /
    失败清单 / 下一步」，中间那段（长列表、大段正文）才是重复度最高的部分。

    为什么留回执而不是静默截断：模型看到半截 JSON 会当成完整结果去解析，然后基于
    残缺数据下判断——那是**无声**的错误。回执里写原文多大、怎么重取，它就有路可走。

    ``tool`` 用来挑那半句「怎么重取」：路是按工具分的，给错一条模型就会照着试
    （见 ``_RECOVERY_HINT`` 的说明）。不传就还是原来那句通用的。
    """
    if limit <= 0:
        return text
    raw = text.encode("utf-8")
    n = len(raw)
    if n <= limit:
        return text
    # 回执本身要占几十字节，从留出的余量里扣，免得「截完仍然超限」。
    head_budget = int(limit * 0.72)
    tail_budget = max(0, limit - head_budget - 300)
    # 按字节切、按字符解码并丢掉半个字符：切在 UTF-8 中间会造出乱码，
    # 模型读到的就是乱码。
    head = raw[:head_budget].decode("utf-8", "ignore")
    tail = raw[-tail_budget:].decode("utf-8", "ignore") if tail_budget else ""
    receipt = (
        f"\n\n[{TRUNCATE_MARKER}｜原文 {n} 字节（约 {estimate_tokens(text)} token），"
        f"此处只给了开头与结尾，中间被省掉。"
        f"需要原文就{_RECOVERY_HINT.get(tool, _DEFAULT_RECOVERY_HINT)}，"
        f"不要凭这段推测缺失的内容。]\n"
    )
    return head + receipt + tail


class ToolResultGate:
    """一次 run 内部对工具结果的收口：单条截断 + 按累计体积折叠更早的结果。

    **只改内容、不删消息**，所以 ``assistant(tool_calls)`` 与它那批 tool 回执的配对
    永远不动——那是最容易被 LLM API 以 400 打回来的地方（``_repair_orphans`` 处理的
    正是这类破坏的后果）。

    省下的不只是钱：工具结果占的窗口越小，同一批事实越不会被挤到「被丢弃的历史」
    那一段里去，所以这一层同时也是**准确率**的事。
    """

    def __init__(
        self,
        *,
        result_max_bytes: int = DEFAULT_RESULT_MAX_BYTES,
        fold_at_bytes: int = DEFAULT_FOLD_AT_BYTES,
        keep_recent: int = DEFAULT_KEEP_RECENT_RESULTS,
        fold_preview: int = DEFAULT_FOLD_PREVIEW,
        fold_to_ratio: float = DEFAULT_FOLD_TO_RATIO,
    ) -> None:
        self.result_max_bytes = int(result_max_bytes)
        self.fold_at_bytes = int(fold_at_bytes)
        # 回差的下沿（见 `DEFAULT_FOLD_TO_RATIO`）。比例夹在 (0, 1]：1 = 退回「刚好压回门槛」
        # 的老行为，只给测试与对照用。
        self.fold_to_bytes = int(self.fold_at_bytes * min(1.0, max(0.05, float(fold_to_ratio))))
        self.keep_recent = int(keep_recent)
        self.fold_preview = int(fold_preview)

    def text(self, name: str, content: Any) -> str:
        """工具结果 → 进链的那段文本（单条超限即截断）。

        调用点用它取代 ``messages.tool_result`` 里的 ``str(content)``：结果对象本身
        不动（``_follow_inflight_renders`` 还要按原对象取 job 状态），只有喂给模型的
        那份文本被收口。
        """
        raw = content if isinstance(content, str) else str(content)
        return truncate_result(raw, self.result_max_bytes, tool=name)

    def shrink(self, messages: list[Message]) -> dict[str, int]:
        """就地把更早的工具结果折叠成预览；返回这一趟的统计（无动作则全 0）。

        触发条件是**累计**体积（``fold_at_bytes``）而不是条数：两条 200KB 的网页和
        两百条 200 字节的回执，前者才是要治的。合计在预算内时一条都不动——平时
        模型看到的仍是原样结果，只有真撑大时才瘦身。

        触发之后折到 ``fold_to_bytes`` 才停（回差，见 `DEFAULT_FOLD_TO_RATIO`）：
        每改写一次历史，后面整段都要按缓存未命中重算，所以要少动、一次动够。
        """
        stats = {"folded": 0, "freed_bytes": 0, "total_bytes": 0}
        idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(idx) <= self.keep_recent:
            return stats

        def size(i: int) -> int:
            return len(str(messages[i].get("content") or "").encode("utf-8"))

        total = sum(size(i) for i in idx)
        stats["total_bytes"] = total
        if total <= self.fold_at_bytes:
            return stats

        # 折叠顺序：研究结果 → 其它工具 → 技能材料（各档内部仍按从旧到新）。
        head = idx[: len(idx) - self.keep_recent]
        order = ([i for i in head if messages[i].get("name") in RESEARCH_TOOLS]
                 + [i for i in head if messages[i].get("name") not in RESEARCH_TOOLS
                    and messages[i].get("name") not in INDEX_FOLD_TOOLS]
                 + [i for i in head if messages[i].get("name") in INDEX_FOLD_TOOLS])
        for i in order:
            if total <= self.fold_to_bytes:
                break
            m = messages[i]
            content = str(m.get("content") or "")
            if FOLD_MARKER in content:  # 已折叠过：不重复加回执（否则越折越长）
                continue
            if m.get("name") in NEVER_FOLD_TOOLS:
                continue
            before = size(i)
            # 小回执不折：折了省不了几个字节，却会磨掉「这条调用出过什么错」这类
            # 短而关键的事实（错误原因、id、用户那句新要求）——折叠只对真正大的结果
            # 才有意义，那才是要治的东西。
            if before <= self.fold_preview + 200:
                continue
            preview, kept_index = self._fold_preview(m.get("name", ""), content)
            # 回执那半句「怎么把原文拿回来」**按工具分**——表就在上面，与
            # ``truncate_result`` 用的是同一张。原先这里硬写着「用更窄的参数」，
            # 对 ``load_skill`` 恰好是条走不通的路（索引已经被切掉了，见
            # ``INDEX_FOLD_TOOLS``）：模型照着它去猜参数，四次全错。
            if kept_index:
                how = ("**索引整份留在下面**，需要哪一份就照它上面的名字取：附件用 path=，"
                       "分段用 section=，或按章节目录里的行号用 offset= 直取那一节。"
                       "技能文件不在会话工作区里，read_file / grep 找不到它们")
            else:
                how = ("需要原文就"
                       + _RECOVERY_HINT.get(str(m.get("name") or ""),
                                             _DEFAULT_RECOVERY_HINT))
            folded = (
                f"[{FOLD_MARKER}｜{m.get('name', '')}｜这条是更早一轮的调用结果，"
                f"原文 {before} 字节，正文已省掉。{how}。]\n{preview}…"
            )
            messages[i] = {**m, "content": folded}
            delta = before - size(i)
            total -= delta
            stats["folded"] += 1
            stats["freed_bytes"] += delta
        stats["total_bytes"] = total
        return stats

    def _fold_preview(self, name: str, content: str) -> tuple[str, bool]:
        """折叠后保留的那一段 → ``(预览, 是否保留了整份索引)``。

        默认是开头 ``fold_preview`` 个字符。列入 ``INDEX_FOLD_TOOLS`` 的工具改留
        **整份索引**（切在 ``SKILL_INDEX_END`` 前面）——理由见那个常量的注释。

        索引分隔行不在（老结果、或者这条本来就是按 ``path``/``offset`` 取回来的
        分页结果）时**退回通用行为**：不能因为找不到记号就整条原样留着，那等于白折。
        """
        if name in INDEX_FOLD_TOOLS:
            cut = content.find(SKILL_INDEX_END)
            if cut > 0:
                return content[:cut], True
        return content[: self.fold_preview], False


class ContextCompressor:
    def __init__(
        self,
        max_tokens: int,
        *,
        keep_recent_tool_results: int = 2,
        fold_preview: int = 60,
        token_counter: TokenCounter | None = None,
        summarizer: Summarizer | None = None,
        summaries: SummaryStore | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.keep_recent_tool_results = keep_recent_tool_results
        self.fold_preview = fold_preview
        self.count = token_counter or default_token_counter
        self.summarizer = summarizer or default_summarizer
        # 摘要的持久化口（R10-T47）。传 None = 今天的行为：每轮重算、不落库。
        # 它**不影响压缩结果**，只影响「同一段前缀第二次要不要再花一次模型调用」，
        # 所以没有存储的调用方（单测、离线脚本）什么都不用改。
        self.summaries = summaries
        # 当前所在的会话作用域，由装配方在调用前 bind 进来。空串 = 未知，
        # 于是不查缓存也不落库——宁可多花一次模型调用，也不要把 A 会话的摘要
        # 喂给 B 会话（那是串话，比多花钱坏得多）。
        self._scope = ""

    # ---- 作用域绑定（Q64：不改压缩器签名，靠一个鸭子类型的钩子）----

    def bind_scope(self, scope: str, **_: object) -> None:
        """告诉压缩器「这一轮在哪个会话里」。

        为什么不把 scope 加进 ``__call__`` 的形参：``ContextCompressor`` 协议的签名
        （``(messages, query)``）是冻结的，装配侧、Agent 侧与测试都按它写；多一个形参
        就是三处跟着改。装配方在调用前 duck-typing 地看一眼有没有这个钩子，
        有就绑、没有就算了（自定义压缩器不必实现它）。
        """
        self._scope = str(scope or "")

    # ---- 估算 ----

    def estimate(self, messages: list[Message]) -> int:
        return sum(self.count(m) for m in messages)

    # ---- 第一级：折叠工具结果 ----

    def _fold_tool_info(self, body: list[Message]) -> list[Message]:
        tool_idx = [i for i, m in enumerate(body) if m.get("role") == "tool"]
        keep = self.keep_recent_tool_results
        if len(tool_idx) <= keep:
            return body
        fold_set = set(tool_idx[: len(tool_idx) - keep])
        out: list[Message] = []
        for i, m in enumerate(body):
            content = str(m.get("content") or "")
            if i in fold_set and FOLD_MARKER not in content:
                preview = content[: self.fold_preview]
                out.append(
                    {
                        **m,
                        "content": f"[{FOLD_MARKER} {m.get('name','')}：{preview}…共 {len(content)} 字符]",
                    }
                )
            else:
                out.append(m)
        return out

    # ---- 第二级：从最早的 body 消息开始淘汰，保留能塞进预算的近期消息 ----

    def _split_to_fit(
        self, system: list[Message], body: list[Message], reserve: int
    ) -> tuple[list[Message], list[Message]]:
        """返回 (dropped, kept)：从后往前尽量多保留近期消息；至少保留最后一条。"""
        running = self.estimate(system) + reserve
        split = 0  # body[split:] 保留，body[:split] 淘汰
        for i in range(len(body) - 1, -1, -1):
            cost = self.count(body[i])
            # 最后一条（当前输入）无条件保留，不触发切分
            if i < len(body) - 1 and running + cost > self.max_tokens:
                split = i + 1
                break
            running += cost
        return body[:split], body[split:]

    # ---- 组装 ----

    async def __call__(self, messages: list[Message], query: str = "") -> list[Message]:
        if not messages or self.estimate(messages) <= self.max_tokens:
            return messages  # 预算内不压缩

        if messages[0].get("role") == "system":
            system, body = [messages[0]], list(messages[1:])
        else:
            system, body = [], list(messages)

        # 第一级：折叠工具信息
        body = self._fold_tool_info(body)
        if self.estimate(system + body) <= self.max_tokens:
            return system + body

        # 预留一条摘要消息的空间，再做第二/三级
        reserve = 32
        dropped, kept = self._split_to_fit(system, body, reserve)

        if dropped:
            summary = await self._summarize(dropped)
            summary_msg: Message = {
                "role": "system",
                "content": f"<history_summary>\n{summary}\n</history_summary>",
            }
            return _repair_orphans(system + [summary_msg] + kept)
        return _repair_orphans(system + kept)

    async def _summarize(self, dropped: list[Message]) -> str:
        """被淘汰那一段 → 摘要文本。**同一段前缀只总结一次**（R10-T47/Q64）。

        判据落在被淘汰那一段的**内容指纹**上，不是「第几轮」：压缩器拿不到
        ``head_seq``（见 ``__call__`` 的签名），而且「同一段历史」本来就是它唯一
        该关心的东西——前缀多淘汰了一条，指纹就变、重算一次也是对的（更长的一段
        该有覆盖它的新摘要）。

        命中时**一次模型调用都不发**，这正是这条通道存在的理由：每轮重算一次摘要，
        省下的是真钱。
        """
        key = summary_key(dropped)
        if self.summaries is not None and self._scope:
            cached = await self.summaries.get(self._scope, key)
            if cached:
                return cached
        text = self.summarizer(dropped)
        try:
            summary = await text if _is_awaitable(text) else str(text or "")
        except Exception:
            # 摘要是尽力而为的降级通道，不是硬依赖：LLM 超时/额度失败只该让这段
            # 历史摘要变粗，不该把整条 run 炸掉——炸在这里时用户消息都还没落库，
            # 界面上看到的就是「发了没反应」。
            summary = await default_summarizer(dropped)
        if self.summaries is not None and self._scope and summary.strip():
            await self.summaries.put(self._scope, key, summary, covered=len(dropped))
        return summary


def _repair_orphans(messages: list[Message],
                    keep_ids: Iterable[str] = ()) -> list[Message]:
    """清理压缩后可能出现的孤儿 tool_calls 与孤儿 tool 消息，并重排错位消息。

    压缩器淘汰旧消息时可能只淘汰了 tool 结果但保留了发起调用的 assistant，
    或反之——这会让 LLM API 报 ``tool_calls must be followed by tool messages``。
    双向修复：
      1. assistant 带 tool_calls 但无对应 tool 消息 → 移除该 tool_call
         （全部孤儿则删 ``tool_calls`` 键；若 content 也为空则整条丢弃）
      2. tool 消息但前面无对应 assistant tool_calls → 移除该 tool 消息
      3. assistant(tool_calls) 与 tool 结果之间夹了非 tool 消息（如 system）
         → 把非 tool 消息移到 tool 结果之后，恢复 API 要求的连续性

    ``keep_ids`` 是**必须保留**的 tool_call id（HITL 挂起时那批待批调用）。
    没有这个参数就会踩一个很隐蔽的坑：审批挂起点上，待批的 tool_calls **本来就没有**
    tool 结果（结果要等用户批准后才回填），于是被判成孤儿全部剥掉——批准续跑时
    assistant 已无 tool_calls，而 ``_settle_pending`` 又补了一条 tool 回执，
    变成「孤儿 tool 消息」，LLM API 直接 400，整条 run 失败。
    """
    keep = {str(i) for i in keep_ids if i}
    tool_msg_ids = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    valid_tool_ids = tool_msg_ids | keep
    assistant_call_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                assistant_call_ids.add(tc.get("id"))

    # ---- Pass 1：清理孤儿 ----
    cleaned: list[Message] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in valid_tool_ids]
            if kept:
                cleaned.append({**m, "tool_calls": kept})
            else:
                stripped = {k: v for k, v in m.items() if k != "tool_calls"}
                if stripped.get("content"):
                    cleaned.append(stripped)
        elif role == "tool":
            if m.get("tool_call_id") in (assistant_call_ids | keep):
                cleaned.append(m)
        else:
            cleaned.append(m)

    # ---- Pass 2：重排——assistant(tool_calls) 后必须紧跟 tool 消息 ----
    out: list[Message] = []
    i = 0
    while i < len(cleaned):
        m = cleaned[i]
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            needed = {tc.get("id") for tc in m["tool_calls"]}
            collected: list[Message] = []
            deferred: list[Message] = []
            j = i + 1
            while j < len(cleaned) and needed:
                nxt = cleaned[j]
                if nxt.get("role") == "tool" and nxt.get("tool_call_id") in needed:
                    needed.discard(nxt["tool_call_id"])
                    collected.append(nxt)
                else:
                    deferred.append(nxt)
                j += 1
            out.extend(collected)
            out.extend(deferred)
            i = j
        else:
            i += 1
    return out


def _is_awaitable(x: Any) -> bool:
    import asyncio

    return asyncio.iscoroutine(x) or asyncio.isfuture(x)
