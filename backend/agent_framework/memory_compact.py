"""记忆压缩：``memories`` 表里那块只会变长的正文，按「截 / 筛 / 去重 / 限量」收口。

与 ``compress.py`` 的分工（两件事，别混）：
- ``ContextCompressor`` / ``ToolResultGate`` 管**一次请求里**的消息列表——「这次发出去
  多少」；产物用完即弃，下一轮重算。
- 这一层管**存储里**的记忆正文——「长期留下来多少」；产物是库里的真数据。

为什么存储侧也必须有压缩（这不是锦上添花）：注入侧每类只给 ``per_category_bytes``
（700 B，见 memory.py），而写入侧**没有任何长度上限**——``append`` 一次一行、只增不减。
memory.py 的 docstring 里记着实测：append 200 次 = 30489 B。也就是绝大部分记忆模型
**永远读不到**，却每次都要参与打分、排序、抢那 700 B，还要整套跟着进压缩账。
更坏的是**同一条事实写十遍**（模型每轮「顺手记一下」的典型结果）会把真正切题的那条
挤出 ``top_k``——那是准确率问题，不只是容量问题。

**触发**（这次新增的那一半）：不在读侧、不做定时扫描，而是**写在写侧**——
``MemoryStore.append()`` 落盘后看一眼这一类的条数/字节，过线才整理一次。写侧是唯一
能保证「增长发生的那一刻就被收口」的位置：读侧改内容会让「读」变成有副作用的动作
（T39 已明令读侧只读），定时扫描在「一个进程外面还可能有别的进程」的部署里根本不是
同一个进程的记忆。

**四道收口，全部可核对、可复述**（不做「悄悄丢几条」这种事）：

1. **截**：单条超过 ``max_entry_bytes`` 就截断并留标记，**不是删**。一行 1.2 KB 不是
   记忆，是一段被粘进来的文档；但内容本身是模型写的，删它等于替它做判断，截断只是
   把它压回「一行」的体量。
2. **筛**（低质）：空行、太短（< ``min_entry_chars``）、纯占位（「无」「暂无」「N/A」）、
   问句（以 ？ 结尾——那是模型自己的疑问，不是事实）、只有标点或数字。
   **安全阀**：筛到一条不剩时**留下最新那一条**——清空一类的记忆是不可逆的，而它可能
   只是被规则误判；宁可留一条糟的，也不许把一整类记忆抹掉。
3. **去重**：归一化（去项目符号、压空白、去首尾标点、ASCII 转小写）之后——
   骨架完全相同 → 留**最新**的那条（append 是时序的，后写的措辞就是当前措辞）；
   一条的骨架被另一条整段包含 → 留**长**的那条（信息更全），一样长留最新的。
   「换了说法」的同义改写**刻意不做**（判据不够用，见 ``_subsumed`` 那段）。
4. **限量**：仍超 ``max_entries`` / ``max_bytes`` 时从**最早**的开始丢。丢最早有依据：
   读侧本来就把条目序号当写入先后用（``MemoryContextSource.render`` 的 ``scored`` 里
   ``idx`` 就是「写入先后」），而注入侧永远优先保最新的状态行。

**为什么整理结果不写进正文**（不做「已压缩」那种标记行）：那一行会被读侧当成**一条
记忆**参与打分、还会被注入给模型——等于用一条自制的元信息去挤真实的记忆。判决只说给
写入方（工具回执 / 接口响应 / 日志），库里只留真数据。被删的行也**不静默**：
``CompactReport`` 逐条带着原文前几十字，回执里能原样念出来。

幂等：同一份正文连跑两次结果相同（四道里没有一条依赖「这是第几次跑」）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 截断标记。它必须短——它自己也算在这一行的字节里。
ENTRY_TRUNCATE_MARKER = "…（本条过长，已截断）"

# 低质判据的理由标签。出现的地方是回执与日志，读它们的是人（和模型），所以要能看懂。
JUNK_EMPTY = "空行"
JUNK_SHORT = "太短"
JUNK_PLACEHOLDER = "占位行"
JUNK_QUESTION = "问句（不是事实）"
JUNK_NOISE = "只有标点或数字"
DUP_EXACT = "重复"
DUP_SUBSUMED = "被更全的一条包含，留更全的那条"
OVER_LIMIT = "超上限，丢最早"


# ---------------------------------------------------------------------------
# 词法：CJK 连续段的二元组 + ASCII 词（小写、≥2 字）
#
# 这份词法的**唯一实现**在本模块（原先长在 ``memory.MemoryContextSource.terms`` 里）。
# 判重与召回必须同口径：召回判「这条跟这轮话题同词」、判重判「这两条在说同一件事」，
# 各写一份就会出现「召回想合的合不上、判重想留的留不住」这种两套词法的经典毛病。
# ---------------------------------------------------------------------------

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


def ascii_words(text: str) -> set[str]:
    """文本里的 ASCII 词（小写，≥2 字）。判据里 ASCII 词按**整词**匹配、CJK 按子串，
    所以打分那侧要能拿到「这条里有哪些整词」，不能只拿到 ``terms`` 那个混合集合。"""
    return {m.group(0).lower() for m in _ASCII_WORD_RE.finditer(str(text or ""))}


def terms(text: str) -> set[str]:
    """文本 → 判据项：ASCII 词（小写，≥2 字）+ CJK 连续段的二元组。

    用二元组而不是分词：没有词典、没有依赖，中文里「文案」这种词本来就会跨词边界
    （「写文案」「文案风格」），二元组对这类匹配反而更稳。单字 CJK 段（如「跑」）
    本身就是一个词，直接收。
    """
    text = str(text or "")
    out = ascii_words(text)
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


# ---------------------------------------------------------------------------
# 单条归一化与判据
# ---------------------------------------------------------------------------

# 列表标记前缀：`- xx`、`* xx`、`1) xx`、`1、xx`、`（1）xx`、`1. xx`。
#
# 两条**刻意不剥**的东西，都是被真机上会出现的正文逼出来的：
# - `第 N 条/款/步` 不剥：它看着像序号，实际是内容——「计划卡：跳过第 2 步「生成配音」」
#   与「…第 3 步…」是两条不同的事实，剥掉「第 N 步」会让它们归一化成同一条而被去重
#   （第一版就是这么写的，20 条各不相同的编号记忆被合成一条）。
# - ASCII 点号后面**必须跟空白**才算列表标记：否则「1.5 倍速」会被剥成「5 倍速」。
_BULLET_RE = re.compile(
    r"^\s*(?:[-*•·—–]+|（\s*\d+\s*）|\(\d+\)|\d+\s*[、,，)）:：]|\d+\.\s)[\s、.．:：]*")
_WS_RE = re.compile(r"\s+")
# 首尾标点里**不收 ? ？**：问句是低质判据之一，把它当普通标点剥掉就再也判不出来
# （第一版就是这么错的——「要不要加字幕？」归一化后与「要不要加字幕」同骨架，
# 判据那头看到的是一个已经没有问号的句子）。
_EDGE_PUNCT = "。，,.;；:：!！、~～\"'“”‘’()（）[]【】{}<>《》 \t"
_HAS_PAYLOAD_RE = re.compile(r"[0-9A-Za-z一-鿿]")

# 纯占位行：模型「这轮没什么好记的」时最常写的那几句。按归一化后的**全等**匹配，
# 不做包含匹配——「没有偏好」是占位，但「用户没有偏好竖屏」是一句真话。
_PLACEHOLDER_KEYS = frozenset({
    "无", "暂无", "没有", "未知", "不清楚", "略", "空", "同上",
    "无记忆", "暂无记忆", "没有记忆", "无偏好", "暂无偏好", "没有偏好",
    "无内容", "暂无内容", "没有内容", "待补充", "待确认", "未提及",
    "none", "null", "nil", "n/a", "na", "nothing", "empty", "tbd", "todo", "nan",
})


def normalize(line: str) -> str:
    """条目 → 判重用的骨架：去项目符号、压空白、去首尾标点、ASCII 转小写。

    只用于**比较**，不用于存储：正文里那行原样留着（模型回读时看到的还是自己写的样子）。
    """
    text = _WS_RE.sub(" ", str(line or "")).strip()
    prev = None
    while prev != text:            # 前缀可能叠着（「1. - xx」），剥到不动为止
        prev = text
        text = _BULLET_RE.sub("", text).strip()
    text = text.strip(_EDGE_PUNCT).strip()
    return text.lower()


def _clip_entry(text: str, limit: int) -> str:
    """单条按字节截断（保住 UTF-8 边界）。"""
    raw = text.encode("utf-8")
    if limit <= 0 or len(raw) <= limit:
        return text
    budget = max(1, limit - len(ENTRY_TRUNCATE_MARKER.encode("utf-8")))
    return raw[:budget].decode("utf-8", "ignore") + ENTRY_TRUNCATE_MARKER


def junk_reason(key: str, *, min_chars: int) -> str:
    """低质判据：返回理由标签；不是低质就返回空串。

    入参是**归一化后的骨架**（``normalize`` 的产物），所以「- 无。」与「无」在这里
    是同一件事。判据全部是「这条本身就不成立」，不涉及任何主观取舍——
    一旦要判断「这句话有没有价值」，这条规则就不该留在这一层（那要模型来判）。
    """
    if not key:
        return JUNK_EMPTY
    if key in _PLACEHOLDER_KEYS:
        return JUNK_PLACEHOLDER
    if not _HAS_PAYLOAD_RE.search(key):
        return JUNK_NOISE
    if len(key) < min_chars:
        return JUNK_SHORT
    if key.endswith(("？", "?")):
        return JUNK_QUESTION
    return ""


@dataclass
class _Item:
    key: str
    text: str
    order: int          # 写入先后（append 是时序的，越大越新）


def _subsumed(a: _Item, b: _Item, *, min_subsumed_chars: int) -> bool:
    """一条被另一条**整段包含**吗（短的 ≥ ``min_subsumed_chars``）。

    **为什么只做包含、不做「换了说法」的相似度**：这条线本来想用 CJK 二元组词集的
    Jaccard 去抓同义改写，实测抓不住——「用户喜欢极简风格」与「用户偏好极简的风格」
    的二元组 Jaccard 只有 0.25；要让它命中，线得压到 0.4 上下，可那个位置上
    「字幕用大字，颜色黄色」与「字幕用大字，颜色白色」这类**只差一个真事实**的句子
    同样会被合并（它们只差一个字，任何字符级相似度都分不开「换了说法」和「换了个值」）。
    这不是参数没调好，是判据本身不够用：**要治同义改写得上模型判决**，属于另一件事。
    所以这里只留包含关系——它按构造就是精确的：短的那条每个字都在长的那条里，
    合并它不丢任何信息。抓不住的近重复留给下一轮或留给人，不靠压线硬凑。
    """
    short, long = (a, b) if len(a.key) <= len(b.key) else (b, a)
    return bool(short.key) and short.key != long.key \
        and len(short.key) >= min_subsumed_chars and short.key in long.key


def _richer(a: _Item, b: _Item) -> _Item:
    """留哪条：骨架更长的（信息更全）；一样长留更新的（措辞是当前的）。"""
    if len(a.key) != len(b.key):
        return a if len(a.key) > len(b.key) else b
    return a if a.order > b.order else b


@dataclass
class CompactReport:
    """一次整理的判决：改了什么、丢了什么、丢的原文是什么。"""

    category: str = ""
    before_lines: int = 0
    before_bytes: int = 0
    after_lines: int = 0
    after_bytes: int = 0
    #: (原因标签, 原文摘要)：判决可复述的凭据。摘要给的是**原文**，不是骨架——
    #: 看回执的人要能认出「刚才被丢掉的那条就是我写的那句」。
    dropped: list[tuple[str, str]] = field(default_factory=list)
    #: 「保 X ← 并 Y」：近重复合并的去向。
    merged: list[str] = field(default_factory=list)
    #: 被截断的行（摘要）。截断不是删，所以与 dropped 分开记。
    truncated: list[str] = field(default_factory=list)
    #: 筛到一条不剩、靠安全阀留下最新一条。
    kept_for_safety: bool = False
    #: 没做任何事的原因（未过线 / 并发争用 / 空）。有它就不该有别的判决。
    skipped: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.merged or self.truncated) \
            or self.after_bytes != self.before_bytes

    def summary(self) -> str:
        """一行够念的判决。空串 = 什么都没发生。"""
        if self.skipped:
            return self.skipped
        if not self.changed:
            return ""
        parts: list[str] = []
        if self.merged:
            parts.append(f"合并近重复 {len(self.merged)}")
        junk = [r for r, _ in self.dropped if r != OVER_LIMIT]
        if junk:
            parts.append(f"清掉低质/重复 {len(junk)}")
        over = sum(1 for r, _ in self.dropped if r == OVER_LIMIT)
        if over:
            parts.append(f"超上限丢最早 {over}")
        if self.truncated:
            parts.append(f"截断超长 {len(self.truncated)}")
        if self.kept_for_safety:
            parts.append("全部判低质，保住了最新一条")
        return (f"{'、'.join(parts)}：{self.before_lines} 条 / {self.before_bytes} B → "
                f"{self.after_lines} 条 / {self.after_bytes} B")

    def detail(self, limit: int = 6) -> str:
        """判决的明细（回执/日志用）：丢了哪些、拿什么原文丢的。"""
        rows: list[str] = []
        for reason, brief in self.dropped[:limit]:
            rows.append(f"丢({reason}) {brief}")
        for pair in self.merged[:limit]:
            rows.append(f"并 {pair}")
        for brief in self.truncated[:limit]:
            rows.append(f"截 {brief}")
        head = "；".join(rows)
        total = len(self.dropped) + len(self.merged) + len(self.truncated)
        if total > limit:
            head += f"；…另 {total - limit} 处"
        return head


def _brief(text: str, n: int = 40) -> str:
    flat = _WS_RE.sub(" ", str(text or "")).strip()
    return flat if len(flat) <= n else flat[:n - 1] + "…"


# ---------------------------------------------------------------------------
# 主流程（纯函数：不碰存储，好核对也好离线复算）
# ---------------------------------------------------------------------------

def compact_content(content: str, *, category: str = "",
                    rules: "MemoryCompactor | None" = None) -> tuple[str, CompactReport]:
    """正文 → (整理后的正文, 判决)。**纯函数**：同样的输入永远给同样的输出。"""
    r = rules or MemoryCompactor()
    rep = CompactReport(category=category)
    raw_lines = [ln.strip() for ln in str(content or "").splitlines()]
    raw_lines = [ln for ln in raw_lines if ln]
    rep.before_lines = len(raw_lines)
    rep.before_bytes = len(str(content or "").encode("utf-8"))
    if not raw_lines:
        rep.skipped = "这一类本来就是空的"
        return "", rep

    # ① 截 + ② 筛。判决先攒在本地：安全阀要用到「谁被判过」。
    items: list[_Item] = []
    junk: list[tuple[str, str, str]] = []      # (原因, 摘要, 原文)
    for ln in raw_lines:
        text = _clip_entry(ln, r.max_entry_bytes)
        if text != ln:
            rep.truncated.append(_brief(ln))
        key = normalize(text)
        reason = junk_reason(key, min_chars=r.min_entry_chars)
        if reason:
            junk.append((reason, _brief(text), text))
            continue
        items.append(_Item(key=key, text=text, order=len(items)))

    # 安全阀：整类都被判低质时留最新那条（见模块 docstring）
    if not items:
        last = _clip_entry(raw_lines[-1], r.max_entry_bytes)
        items = [_Item(key=normalize(last), text=last, order=0)]
        rep.kept_for_safety = True
        junk = [row for row in junk if row[2] != last]
    rep.dropped.extend((reason, brief) for reason, brief, _ in junk)

    # ③ 去重：骨架全同 → 位置留第一个（时序不乱），措辞留最新的（当前措辞）
    slot: dict[str, int] = {}
    deduped: list[_Item] = []
    for it in items:
        at = slot.get(it.key)
        if at is None:
            slot[it.key] = len(deduped)
            deduped.append(it)
            continue
        rep.dropped.append((DUP_EXACT, _brief(deduped[at].text)))
        deduped[at] = _Item(key=it.key, text=it.text, order=deduped[at].order)

    # ③′ 包含合并：反复扫到不动为止（条数上限 40 上下，平方级足够）
    merged = True
    while merged and len(deduped) > 1:
        merged = False
        for i in range(len(deduped)):
            for j in range(i + 1, len(deduped)):
                a, b = deduped[i], deduped[j]
                if not _subsumed(a, b, min_subsumed_chars=r.min_subsumed_chars):
                    continue
                keep = _richer(a, b)
                drop = b if keep is a else a
                rep.merged.append(f"{_brief(keep.text)} ← {_brief(drop.text)}")
                deduped.remove(drop)
                merged = True
                break
            if merged:
                break

    # ④ 限量：从最早（order 最小 = 列表头）开始丢
    def total_bytes(rows: list[_Item]) -> int:
        return len("\n".join(x.text for x in rows).encode("utf-8"))

    while len(deduped) > 1 and (len(deduped) > r.max_entries
                                or total_bytes(deduped) > r.max_bytes):
        gone = deduped.pop(0)
        rep.dropped.append((OVER_LIMIT, _brief(gone.text)))

    new_content = "\n".join(x.text for x in deduped)
    rep.after_lines = len(deduped)
    rep.after_bytes = len(new_content.encode("utf-8"))
    if not rep.changed:
        rep.skipped = "查过一遍，没有可整理的"
    return new_content, rep


class MemoryCompactor:
    """阈值 + 判决入口：什么时候动手、动到什么程度。

    默认值怎么定的（都是对着注入侧那两个数算出来的，不是拍的）：

    - 注入侧每类 700 B、合计 1400 B（``MemoryContextSource``）。存储到 6000 B 时，
      已经是注入预算的 8.5 倍——也就是说**九成的记忆模型根本读不到**，那正是该收口的
      时候；再晚就是「越写越读不着」。
    - 条数同理：注入侧一轮最多 6 条（``top_k``），24 条 = 4 轮不同话题才可能被读到一次。
    - ``max_entries=40`` / ``max_bytes=12000`` 是硬顶：前者对齐 memory.py 里
      「实测 append 200 次 = 30489 B」那条教训的量级——40 条 / 12 KB 之后，
      新写的必然把老的挤出去，这件事该**发生**（记忆本来就该遗忘），但要发生得
      有判决、有记录，而不是悄悄消失在注入预算里。
    """

    def __init__(
        self,
        *,
        compact_at_bytes: int = 6000,
        compact_at_entries: int = 24,
        max_entries: int = 40,
        max_bytes: int = 12000,
        min_entry_chars: int = 4,
        min_subsumed_chars: int = 4,
        max_entry_bytes: int = 1200,
        retries: int = 8,
    ) -> None:
        self.compact_at_bytes = int(compact_at_bytes)
        self.compact_at_entries = int(compact_at_entries)
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        # 4 而不是 6：「讨厌冗长」这种正经的短偏好也是 4 个字，卡在 6 会把真偏好当噪声。
        # 低于 4 个字的一行（「好的」「ok」）在事实层面确实什么都没说。
        self.min_entry_chars = int(min_entry_chars)
        # 4 与 min_entry_chars 同值不是巧合：能活到合并这一步的条目本来就 ≥ 4 字，所以
        # 这个参数今天是**不生效的下界**（留着是为了「包含合并要有下界」这条判断可见）。
        # 真正的下界是 min_entry_chars——「文案」这种两个字的老条目早被判成「太短」清了。
        self.min_subsumed_chars = int(min_subsumed_chars)
        self.max_entry_bytes = int(max_entry_bytes)
        # CAS 重试预算：与 ``MemoriesRepo.append`` 同源——CAS 失败必然意味着别人真的
        # 改了内容，所以每一轮都在往前走，轮数约等于同 (user, category) 上的并发写者数。
        self.retries = int(retries)

    # ---- 触发 ----

    def over_limit(self, content: str) -> str:
        """过线了吗：过线返回原因（原样进回执，要能读），没过返回空串。"""
        text = str(content or "")
        n_lines = len([ln for ln in text.splitlines() if ln.strip()])
        n_bytes = len(text.encode("utf-8"))
        if n_lines > self.compact_at_entries:
            return f"{n_lines} 条 > 触发线 {self.compact_at_entries} 条"
        if n_bytes > self.compact_at_bytes:
            return f"{n_bytes} B > 触发线 {self.compact_at_bytes} B"
        return ""

    def compact(self, content: str, *, category: str = "",
                force: bool = False) -> tuple[str, CompactReport]:
        """过线才动（``force=True`` 时不看线，供「整理记忆」按钮与工具显式调用）。"""
        text = str(content or "")
        rep = CompactReport(category=category)
        if not force:
            why = self.over_limit(text)
            if not why:
                n_lines = len([ln for ln in text.splitlines() if ln.strip()])
                rep.before_lines = rep.after_lines = n_lines
                rep.before_bytes = rep.after_bytes = len(text.encode("utf-8"))
                rep.skipped = (f"未过线（{n_lines} 条 / {len(text.encode('utf-8'))} B，"
                               f"触发线 {self.compact_at_entries} 条 / "
                               f"{self.compact_at_bytes} B）")
                return text, rep
        return compact_content(text, category=category, rules=self)

    # ---- 写侧的单条判据（append 用）----

    def duplicate_of(self, content: str, text: str) -> str | None:
        """要追加的这条已经在了吗：在就返回**已存在的那条原文**，不在返回 None。

        判据用**归一化全等**，不做包含/近重复——追加是模型主动说「再加一条」，
        把「说得更全的新说法」拦下来会让它以为写进去了（实际上没有）。近重复交给
        整理那一步合，那里有判决可看。
        """
        key = normalize(text)
        if not key:
            return None
        for ln in str(content or "").splitlines():
            if ln.strip() and normalize(ln) == key:
                return ln.strip()
        return None

    def junk_of(self, text: str) -> str:
        """要追加的这条本身是低质吗（append 用，返回理由标签或空串）。"""
        return junk_reason(normalize(text), min_chars=self.min_entry_chars)


def compactor_of(obj: Any) -> MemoryCompactor:
    """取一个对象身上的压缩器（没有就用默认的）。

    给「同一台机器上不同装配点造出来的 MemoryStore 共用一份阈值」用：面板要把触发线
    显示出来，而它手上只有 store，没有 compactor。
    """
    got = getattr(obj, "compactor", None)
    return got if isinstance(got, MemoryCompactor) else MemoryCompactor()
