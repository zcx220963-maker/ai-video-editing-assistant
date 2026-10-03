"""复盘进记忆：把用户在计划卡上动过的地方与成片退回，落成下一轮能读到的偏好。

**为什么在服务端算，而不是让模型自己在终答里「顺手记住」。** 卡面上的差异是两份
现成数据（``submit_plan`` 存下的候选卡 + 浏览器回传的确认帧）一比就出来的结构化事实；
指望模型总结，实测会把「跳过了加 BGM 这步」写成「用户不喜欢音乐」这种没有锚点的断言，
而下一次规划读到它就直接少排一步。这里的每一行都带「第几步、哪个节点、哪个参数、
从什么改成什么」，可核对也可去重。

**三条信号，各自对应一个用户动作：**
- 确认帧差异（``plan_card_lines``）：跳过哪步、把哪个枚举参数改成非默认值、另写了什么自由文本。
- 「换一版」的原话（``revise_line``）：用户否掉这版卡时给出的理由。
- 成片退回（``rework_line``）：本会话此前已经真渲染出过成片，本轮计划里再次出现
  ``render_video``——这是「上一版没被接受」的机器可判口径，不是猜语气。

**为什么不写「用户喜欢/不喜欢 X」的结论。** 一次勾选只是一个数据点，写成结论会让下轮
把偶然当偏好（跳过一次 BGM ≠ 永远不要 BGM）。落的都是动作本身，判断留给下一轮。

安全：分类只能用 ``memory.CATEGORIES`` 白名单里的 ``user``/``tool``（PG 侧还有 CHECK）；
归属仍由 ``MemoryStore`` 从执行身份解析，本模块不经手 user_id。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Sequence

from .catalog import get_catalog
from .memory import CATEGORIES, MemoryStore
from .plan.support import as_card_value, clean, neutralize, num
from .render_gate import RENDER_NODE

logger = logging.getLogger(__name__)

# 单条记忆的行长度上限：注入侧本来就有 2000/4000 字的总量闸，超限截的是**更早**的行，
# 所以行本身必须短——一行占掉几百字，等于把整段老记忆挤出去。
LINE_MAX_CHARS = 160
# 一轮最多写几行：一次确认帧通常 0~3 条，给到 8 是防「把整张卡抄进记忆」。
MAX_LINES_PER_ROUND = 8

_SKIP_PREFIX = "计划卡：跳过"
_PARAM_PREFIX = "计划卡：改动"
_CUSTOM_PREFIX = "计划卡手写诉求"
_REVISE_PREFIX = "换一版计划的理由"
_REWORK_PREFIX = "成片退回重做"


def _label(node: Any) -> str:
    """节点中文名（查得到就用，查不到就原样给回机器名——不编名字）。"""
    return get_catalog().label(str(node or "")) or "(无名步骤)"


def _seq_of(value: Any) -> int | None:
    got = num(value)
    return None if got is None else int(got)


def _clip(text: str) -> str:
    """把**整行**压到上限：先压空白，再截字并留省略号。

    上限管的是整行（含「计划卡：跳过第 N 步」这段前缀），不是只管用户写的那截：
    注入侧按 2000/4000 字截的是**更早**的行，一行太长就等于把老记忆挤出去。
    """
    text = " ".join(str(text or "").split())
    if len(text) <= LINE_MAX_CHARS:
        return text
    return text[:LINE_MAX_CHARS - 1] + "…"


def plan_card_lines(plan: Mapping[str, Any], frame: Mapping[str, Any]
                    ) -> list[tuple[str, str]]:
    """候选卡 vs 确认帧的差异 → ``(category, 记忆行)``。

    只记**动过**的地方：参数值等于卡面默认值就不算偏好；卡上不存在的步骤/参数忽略
    （那些在 ``validate_execute`` 里已经打回，不该在这里再判一遍）。
    """
    steps: dict[int, Mapping[str, Any]] = {}
    for raw in plan.get("steps") or []:
        if not isinstance(raw, Mapping):
            continue
        seq = _seq_of(raw.get("seq"))
        if seq is not None:
            steps[seq] = raw
    if not steps:
        return []

    lines: list[tuple[str, str]] = []
    skipped: set[int] = set()
    for item in frame.get("skips") or []:
        seq = _seq_of(item.get("step_seq") if isinstance(item, Mapping) else item)
        step = steps.get(seq) if seq is not None else None
        if step is None or seq in skipped:
            continue
        skipped.add(seq)
        lines.append(("user", f"{_SKIP_PREFIX}第 {seq} 步「{_label(step.get('node'))}」"))

    for item in frame.get("param_finals") or []:
        if not isinstance(item, Mapping):
            continue
        seq = _seq_of(item.get("step_seq"))
        key = clean(item.get("key"))
        if seq is None or not key or seq in skipped:
            continue          # 整步被跳了，步内参数怎么选都不再是偏好
        step = steps.get(seq)
        if step is None:
            continue
        opt = next((o for o in (step.get("param_options") or [])
                    if isinstance(o, Mapping) and clean(o.get("key")) == key), None)
        if opt is None:
            continue
        value = as_card_value(item.get("value"))
        default = as_card_value(opt.get("default"))
        if value == default:
            continue          # 没动 = 没有可记的偏好
        lines.append(("user",
                      f"{_PARAM_PREFIX}第 {seq} 步「{_label(step.get('node'))}」的"
                      f"「{get_catalog().param_label(key)}」＝{value}（卡面默认 {default}）"))

    for item in frame.get("overrides") or []:
        if not isinstance(item, Mapping):
            continue
        text = _clip(neutralize(clean(item.get("value"))))
        if not text:
            continue
        seq = _seq_of(item.get("step_seq"))
        step = steps.get(seq) if seq is not None else None
        if step is None:
            lines.append(("user", f"{_CUSTOM_PREFIX}（整体）：{text}"))
        else:
            key = clean(item.get("key")) or "_general"
            where = f"第 {seq} 步「{_label(step.get('node'))}」"
            if key != "_general":
                where += f"的「{get_catalog().param_label(key)}」"
            lines.append(("user", f"{_CUSTOM_PREFIX}（{where}）：{text}"))
    return [(category, _clip(line)) for category, line in lines]


def revise_line(feedback: str) -> tuple[str, str] | None:
    """「换一版」时用户写的理由：这是一句直接的偏好，原话落进去（已中和标签）。"""
    text = _clip(neutralize(clean(feedback)))
    if not text:
        return None
    return ("user", _clip(f"{_REVISE_PREFIX}：{text}"))


def rework_line(prior_nodes: Iterable[str], planned_nodes: Iterable[str],
                reason: str = "") -> tuple[str, str] | None:
    """成片退回：本会话此前已真渲染出过成片，本轮计划又排了 ``render_video``。

    口径是「产物在不在」，不是「用户语气好不好」——dry-run 的回执不写产物表，
    所以「先看看会渲成什么样」不会被误记成退回。
    """
    had = RENDER_NODE in set(prior_nodes or ())
    again = RENDER_NODE in set(planned_nodes or ())
    if not (had and again):
        return None
    line = (f"{_REWORK_PREFIX}：本会话此前已出过成片，本轮计划再次渲染"
            f"「{_label(RENDER_NODE)}」——上一版未被接受")
    text = _clip(neutralize(clean(reason)))
    if text:
        line += f"。当轮用户原话：{text}"
    return ("tool", _clip(line))


class PlanRetrospective:
    """把复盘行写进 memories 表：按分类去重，一轮限量，写失败只告警。"""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def record(self, entries: Sequence[tuple[str, str] | None]
                     ) -> list[str]:
        """返回真正写进去的行（供调用方与用例核对）。

        去重靠「整行子串」：同一次确认被重放（崩溃续跑、前端重复点击）时行内容完全
        一致，于是第二次不写——否则一次跳过会在记忆里堆成「每次都跳过」。
        """
        written: list[str] = []
        current: dict[str, str] = {}
        for entry in entries:
            if entry is None:
                continue
            category, line = entry
            line = clean(line)
            if not line or len(written) >= MAX_LINES_PER_ROUND:
                continue
            if category not in CATEGORIES:
                logger.warning("复盘记忆分类不在白名单，已忽略：%r", category)
                continue
            if category not in current:
                current[category] = await self._store.read(category)
            if line in current[category]:
                continue
            updated = await self._store.append(category, line)
            current[category] = updated if isinstance(updated, str) else (
                current[category] + "\n" + line)
            written.append(line)
        return written
