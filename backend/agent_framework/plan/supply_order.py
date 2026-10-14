# -*- coding: utf-8 -*-
"""R13 §4 供给优先级：四档表的**唯一真源**，外加闸㉙（取材次序）的判据。

**为什么要有这个文件。** 供给优先级此前只有三处散文 + 一条字面串测试
（R13 §0.10/§0.11）：P19/T25/SKILL.md 各写各的措辞，``test_imagegen.py`` 里那个
``_priority_is_stated`` 只查两个串——改了次序不改这两串，测试仍绿而散文还在教旧次序。
合同（R13 §4/Q161）定的修法是：**这张表是唯一真源，三处散文面与它逐字双向比对**
（表里有而散文没有 → 红；散文里有而表里没有 → 也红）。

表与合同 R13 §4 逐列同形：档 / 动作 / 联网 / 花钱 / 谁触发 / 今天状态。

**次序（用户 2026-10-14 拍板，覆盖旧的「先检索、未命中再出图」）**：

    ① 用户指名的上传件（只有用户触发）→ ② 库检索命中（模型）→
    ③ 代码自己画（模型）→ ④ 出图 API（只有用户明确点名）

三条硬话（R13 §4）：

1. ④ 是最后一路，且必须先有 ② 的一次 ``no_match`` 留痕才允许被调用（闸㉙，
   见 :func:`check_supply_order`）。
2. ①④ 都归「用户触发」，模型不许自动走（闸㉛，见
   ``agent_framework.designated_asset``）。
3. 降级**允许且必须可见**，静默出片仍然是失败（T25/Q9/Q27 那半句一个字不改）。

**旧口径在这里是「已废弃」，不是「另一种说法」**：``LEGACY_PHRASES`` 里那几串在
任何散文面出现即红——留着旧措辞不删，就出现「测试绿而散文还在教旧次序」那种假绿。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: 四档的圈码（档号 → 字符）。渲染与比对都从这张表取，不手写「①②③④」字面量。
TIER_MARKS = ("①", "②", "③", "④")

#: R13 §4 那张四档表。``status`` 一栏记**今天**的状态（不是合同成稿时的状态）：
#: T65 索引腿已上线（上传即入 asset_library，``enabled=False`` 不进检索面）。
SUPPLY_ORDER: tuple[dict[str, Any], ...] = (
    {
        "tier": 1,
        "label": "用户指名的上传件",
        "action": "attachments 里的 material_id → 索引行 → shots[].assets",
        "online": False,
        "costs": False,
        "who": "只有用户",
        "status": "已上线（T65 索引腿：上传即入库，enabled=False，只按 sha 直取）",
    },
    {
        "tier": 2,
        "label": "库检索命中",
        "action": "search_illustration → select_illustration",
        "online": False,
        "costs": False,
        "who": "模型",
        "status": "存在（内置 CC0/PD 库，可按 asset_role 过滤）",
    },
    {
        "tier": 3,
        "label": "代码自己画",
        "action": "rough / three / katex / vector 四条画法通道",
        "online": False,
        "costs": False,
        "who": "模型",
        "status": "存在且有闸（⑦/⑯/⑳/㉕）",
    },
    {
        "tier": 4,
        "label": "出图 API",
        "action": "imagegen（配好 provider 与密钥才出得了图）",
        "online": True,
        "costs": True,
        "who": "只有用户明确点名",
        "status": "存在；没配后端时降级为检索（降级必须可见，不许静默）",
    },
)

#: 每一档在散文面里**必须逐字出现**的锚点短语。判据的「表 → 散文」方向用它：
#: 漏掉任何一档，就是散文在教一条不完整的次序。
TIER_ANCHORS: tuple[str, ...] = tuple(t["label"] for t in SUPPLY_ORDER)

#: 旧口径整串（P19/T25/Q27 那版把出图当第②档的直接后继，把 ③ 排除在外）。
#: 在散文面里出现即红——「散文 → 表」方向的判据。
LEGACY_PHRASES: tuple[str, ...] = (
    "先检索素材库、未命中再出图",
)

#: 降级那半句（T25/Q9/Q27 冻结，R13 §4 硬话 3：一个字不改地挂到新次序上）。
#: 它必须出现在每一处散文面——新次序不许把「降级可见」这条老承诺弄丢。
DEGRADE_PHRASE = "静默出片仍然是失败"


def supply_order_line(tier: Mapping[str, Any]) -> str:
    """一档 → 一行散文（圈码 + 标签 + 谁触发 + 动作）。三处散文面逐字嵌入这一行。"""
    mark = TIER_MARKS[int(tier["tier"]) - 1]
    return f"{mark} {tier['label']}（{tier['who']}触发）：{tier['action']}"


def render_supply_order() -> str:
    """整表 → 五行文本（四档 + 降级那句）。**这就是「表」在散文里的样子**——
    SKILL.md 供给节、imagegen 工具描述、imagegen_nodes 模块头三处都逐字嵌入它，
    测试拿它做比对。

    降级那行（T25/Q9/Q27 冻结）跟着表一起渲染，而不是让三处散文面各自再写一遍：
    各写一遍就是三份措辞，而 Q90/Q91 那条病正是「两处清单各算各的」。
    """
    lines = [supply_order_line(t) for t in SUPPLY_ORDER]
    lines.append(
        "降级允许且必须可见：没配出图后端时明说降级（回执写明「图像通道降级为素材库检索，"
        f"本片 N 镜无图」），{DEGRADE_PHRASE}——降级本身不是失败，把没有的图写成有才是")
    return "\n".join(lines)


def supply_order_problems(text: str) -> list[str]:
    """R13-T67 的双向比对判据：散文面 ↔ 四档表。空列表 = 同源。

    两个方向，各自对应一种漂移（Q90/Q91 那三兄弟踩过三次的那个病）：

    * **表 → 散文**：四行渲染文本必须逐字出现。少一行 = 散文在教一条不完整的
      次序（例如漏掉 ①，模型就学不到「只有用户指名才能用用户上传的图」）。
    * **散文 → 表**：旧口径整串不许出现。留着它 = 测试绿而散文还在教旧次序
      （R13 §0.11 原话）。
    * 降级那句（T25/Q9/Q27）必须还在——新次序不许把老承诺弄丢。
    """
    problems: list[str] = []
    body = str(text or "")
    for tier in SUPPLY_ORDER:
        line = supply_order_line(tier)
        if line not in body:
            problems.append(
                f"表里有而散文没有：第{tier['tier']}档「{tier['label']}」那一行"
                f"没逐字出现（应为：{line}）")
    for old in LEGACY_PHRASES:
        if old in body:
            problems.append(
                f"散文里有而表里没有：旧口径「{old}」还在——"
                f"它把出图当第②档的直接后继（R13 §4 的次序是 ①②③④，出图是最后一路）")
    if DEGRADE_PHRASE not in body:
        problems.append(
            f"降级那半句丢了：R13 §4 硬话 3 要求「降级允许且必须可见」原样保留"
            f"（「{DEGRADE_PHRASE}」这一句必须出现）")
    return problems


# ---------------------------------------------------------------------------
# 闸㉙ 取材次序（R13 §5）：第④档必须先有第②档的一次 no_match 留痕
# ---------------------------------------------------------------------------

#: 闸㉙ 打回时给出的出路。写成整句而不是指路短语：模型下一轮只有照着这句走，
#: 才不会又发明第三条路（R13 §5 末条「不许把它做成提示词里再强调一遍」——
#: 判据在这里，出路也在这里，两件事都落在执行层）。
_SUPPLY_ORDER_REJECT = (
    "闸㉙ 取材次序：这一趟 imagegen 出图之前，本 run 里没有一次 search_illustration "
    "返回 no_match 的留痕——出图是第④档（最后一路、花钱、联网），必须先有第②档的"
    "一次未命中才允许被调用。现在按次序走：search_illustration 打本镜的具体名词"
    "（主体名 + 场景，不是主题词）；库内确无再回来出图；库里有命中的就用库里的"
    "（免费、不联网、可溯源）；这一镜要的不是写实照片时，按第③档用 rough / three / "
    "katex / vector 四条画法通道自己画——代码画出来的东西能动、零成本零等待、可溯源"
)


def check_supply_order(*, traced_no_match: bool, used_imagegen: bool
                       ) -> tuple[str, str]:
    """闸㉙ → (打回理由, 未判理由)。两者都空 = 过。**纯函数，一次 I/O 都不做。**

    判的是两个布尔（接线方负责取数，判据只判）：

    * ``used_imagegen``：这一趟真的要去出图（有后端、prompt 非空）。
    * ``traced_no_match``：本 run 的产物总线里有过一次 ``search_illustration``
      且它的 ``reason == "no_match"``（Q111 三态里那一态，``asset_nodes`` 已经在产）。

    三出口照 §3 的规矩：

    * **过**：有 no_match 留痕 → 第④档的调用条件成立；
    * **打回**：出了图而之前没有 no_match 留痕（「只有 imagegen、无检索」的序列
      今天全绿——那就是本闸存在的理由）；
    * **未判**：这一 run 没出图 → 这道闸本轮无事可判（不是通过，回执上如实写
      「本片没走图像腿」）。

    为什么判据拆出来单独一个函数：出图要走真后端（烧钱），而判据本身不需要。
    拆出来之后离线夹具能对着同一份实现跑（D-红先两支反例就是调它），
    「什么算过」在这份代码里只有一个出处。
    """
    if not used_imagegen:
        return "", ("本片没走图像腿（这一趟没有 imagegen 调用）——"
                    "闸㉙ 本轮无事可判（未判不是通过）")
    if traced_no_match:
        return "", ""
    return _SUPPLY_ORDER_REJECT, ""


def supply_order_undetermined_note() -> str:
    """没出图时写进回执的那句（未判出口的措辞，与 :func:`check_supply_order`
    同源——回执与判据不许各写一遍）。"""
    return check_supply_order(traced_no_match=False, used_imagegen=False)[1]


async def traced_no_match_of(store: Any) -> bool:
    """从产物总线读「本 run 有过一次 no_match 检索」的留痕（闸㉙ 的取数半边）。

    读的是 ``search_illustration`` 节点产物的 ``reason`` 栏——它执行完自动进
    总线（``orchestration.Store``），所以这一句就是全部接线：不需要新机制，
    也不需要模型自己申报「我检索过了」（自报的留痕会被模型记错，而总线里的
    是机器记的）。

    读不到（节点没跑过、产物形状不对）→ ``False``：那不是「检索过了」，
    闸按「没有留痕」判——它要的正是「先有未命中」这个**事实**，不是
    「模型说它检索过了」这句话。

    **为什么是 async**：``Store.get`` 是 async（产物总线可能落在 ``artifacts``
    表上，不只是进程内存）。第一版写成同步函数，``store.get(...)`` 返回的是一个
    从未被 await 的 coroutine——它恰好不是 ``Mapping``，于是「读不到留痕」那条
    分支每次都命中，判据恒 False 而测试还全绿（判据对 True 的期望值与恒 False
    的返回值正好在「没有留痕」那几个用例上一致）。**这类错不在断言里响，在
    RuntimeWarning 里响**——所以调用方一律 ``await``，测试里也一律 ``await``。
    """
    if store is None:
        return False
    try:
        row = await store.get("search_illustration")
    except Exception:                      # noqa: BLE001 —— 读不到留痕 = 没有留痕
        return False
    if not isinstance(row, Mapping):
        return False
    return str(row.get("reason") or "") == "no_match"


__all__ = [
    "SUPPLY_ORDER", "TIER_MARKS", "TIER_ANCHORS", "LEGACY_PHRASES", "DEGRADE_PHRASE",
    "supply_order_line", "render_supply_order", "supply_order_problems",
    "check_supply_order", "supply_order_undetermined_note", "traced_no_match_of",
]


def _self_check() -> Sequence[str]:
    """自证：渲染文本自身必须满足双向判据（否则三处散文面永远做不到同源）。"""
    return supply_order_problems(render_supply_order())
