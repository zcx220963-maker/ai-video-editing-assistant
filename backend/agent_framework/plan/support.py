"""计划门的公共件：上限常量、纯文本工具、校验结论容器、入参形状归一。

这一层谁都用得到，所以它不依赖任何其他计划门模块（除标准库与 typing）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

PLAN_MAX_CANDIDATES = 3        # 一次规划最多几张卡
PLAN_STEPS_MAX = 24            # 单计划步数上限（防卡面失控）
CUSTOM_MAX_ITEMS = 5           # 自定义诉求条数
CUSTOM_MAX_CHARS = 200         # 单条自定义诉求长度

# 节点**产出**的字段名：模型在 why/expectation 里引用上游产物字段是正常表达，
# 不是「臆造工具名」。真机实测它连写三次「引用了不存在的名字「asr_segments」」，
# 卡连着两轮出不来。这些名字逐条对着 open_storyline 各节点的返回键抄；
# 只用于放行文案，不参与执行判定（写多了也不会放宽别的东西）。
OUTPUT_FIELD_NAMES = frozenset({
    "media", "clips", "asr_segments", "clip_captions", "rough_clips", "groups",
    "templates", "templates_info", "group_scripts", "voiceover", "bgm",
    "timeline", "events", "audio_events", "overlay_events", "subtitles", "warnings",
    "ids", "corrections", "unchanged_suspects", "corrected",
    "shot_count", "structure", "raw_text", "group_id", "start", "end", "duration",
    # 渲染产物的账本与 dry_run 回执（技能正文按字段解释给模型看，不是可调用名）：
    "evidence", "evidence_rule", "render_plan", "blocking", "not_checked",
    "will_render", "timeline_digest", "src_start", "src_end",
    # 零素材图形科普片那一路：plan_motion 的分镜账 + 出片节点的帧数回执
    # （技能正文里「读 estimated_sec 与 target_warnings 对账」就是这些键）：
    # motion_ledger 是逐镜账；field_checks 与 planned_frames 是每一镜上的闸⑥逐字段结论
    # 与帧预算；changed_shots 是局部改账里「改了哪几镜」那一栏。都不是入参键，
    # schema 扫不出来，不收就是假告警。
    "motion_spec", "estimated_sec", "target_warnings", "target_duration_sec",
    "plan_table", "char_count", "frames_total",
    "motion_ledger", "field_checks", "planned_frames", "changed_shots",
    # 出处对账与链身份：source_gate 是分镜卡上每一镜的凭据状态（verified /
    # declared_unverified / no_source / ledger_absent），chain 标这一版画面出自哪条流水线
    # （局部改拿它判旧产物），code_chars 是每一镜的代码字节数。三者都是产物里的键、不是入参，
    # 技能正文要教模型「如实报每镜的凭据是哪一种」，不收就是假告警。
    "source_gate", "chain", "code_chars",
    # ↑ 那一栏的**取值本身**也要收（与 ``motion_plan.GATE_STATES`` 同源）：技能正文教模型
    # 「照这四种如实复述每镜的凭据」，而判定值不在任何入参 enum 上（它只活在产物里），
    # 不收就会被当成臆造工具名——教模型换个含糊说法反而更糟，「已核实」与「没查过」
    # 在答复里必须是可以区分的两件事。
    "verified", "declared_unverified", "no_source", "ledger_absent",
    # 局部改那一路（patch_motion_code）：min_duration_sec 是归一后的镜头字段名
    # （duration_sec 归一就成了它），before_frames 是补丁账里的改前代表帧。
    # 两者都不是入参键，schema 扫不出来，不收就是假告警——技能正文教模型「拖镜头长度
    # 改的是 min_duration_sec」，被拦下来模型只能换个说法，读者反而看不到真字段名。
    "min_duration_sec", "before_frames",
    # 闸⑦（每镜必画）与闸④ 的第二半（带内填充度）的产物键：技能正文教模型
    # 「照 style_channels 说出这版用了哪几条画法通道」「fill_warnings 那几镜要重排」，
    # 这些值只出现在回执与分镜账里，schema 扫不出来，不收就是假告警。
    "style_channels", "style_text_only", "style_warnings",
    "fill_spans", "fill_warnings",
    # 数据时效（2026-10-18）的产物键：freshness_warnings 是分镜卡上「哪几镜的数字标的是
    # 两年前的年份（或没填出处）」那栏人话提醒。它只活在 plan_motion 回执里，schema 扫不出来；
    # 技能正文要教模型「先照这一栏换最新一期再交版」，不收就是假告警。
    "freshness_warnings",
    # 闸⑧（版面回读，记账不拦截）的产物键：layout_warnings 是逐镜带镜号的人话告警
    # （叠字/被画面带切/引线悬空），layout_unscanned 点名的不是「干净」而是「这一镜没扫」。
    # 技能正文教模型「有告警就照条目点名、有没扫的镜就如实说没验过」，这两个键只活在回执里，
    # schema 扫不出来，不收就是假告警。
    "layout_warnings", "layout_unscanned",
    # 闸⑭（动效事件密度）的产物键，与上两栏同形同理由：motion_warnings 是逐镜带镜号的人话
    # （「像素上数不出先后动」是哪一镜、数到几段、最紧一对隔多少帧），motion_unjudged 点名
    # 的不是「动效没问题」而是「这一镜的动效没人判过」（抽帧拿不到 / 容器比判据旧）。
    # 技能正文教模型「被判据拒的镜要说清是哪一镜、差在哪，未判的镜要如实说没判过」，
    # 这两个键只活在回执里，schema 扫不出来，不收就是假告警。
    "motion_warnings", "motion_unjudged",
    # 闸㉑（动效锚定，R8-T33）的产物键，**必须与上面那两栏分开收**：⑭ 判的是「画面上
    # 有没有先后变化」，㉑ 判的是「这些变化为哪条数据服务」——一镜可能 ⑭ 过了而 ㉑ 未判
    # （动效有先后、锚在 `fields.` 上而窗口反推不出来）。技能正文要教模型「锚到本镜声明过的
    # 字段 / 旁白第几个词 / BGM 第几个节拍，每一条动效都得说清为谁动」，所以 `motion` /
    # `anchors` / `kind` 这三个**分镜 schema 的嵌套键**也在这里收下（与 `subject_channel`
    # 同一形状：`vocab.props_for` 只扫顶层，嵌套键扫不出来，不收就是假告警）。
    "motion_anchor_warnings", "motion_anchor_unjudged",
    "motion", "anchors", "kind",
    # 闸⑮（版式族配额）与闸⑯（主体具名）的产物键：技能正文要教模型「每镜写清 subject
    # 与 subject_channel」，卡面的逐镜账里这两栏与 style（代码里认得的通道）并排摆着。
    # `subject`/`subject_channel` **在 plan_motion 的 schema 里**（不是臆造名），但
    # 计划文案的词表只扫工具 schema 的**顶层**键（`vocab.props_for`），分镜里那层扫不到——
    # 技能正文教了模型就会写，不收就是假告警（与 `min_duration_sec` 同一形状）。
    # `family` 是闸⑮ 的产物键（族名），只活在卡面账里，schema 也扫不出来。
    "subject", "subject_channel", "family",
    # 取材轮（T20）的产物键：forage_plan 是逐镜「这一镜要哪几份 demo」，forage_demos 是
    # 那几份的**全文**（逐件 relpath/bytes/sha16/text），forage_bytes 是本轮真给出去的
    # 字节数，forage_oversize 是超线没给全文的那几件。四个键都只活在取材轮的回执里，
    # schema 扫不出来——技能正文要教模型「按 forage_oversize 点名的那几件去 load_skill
    # 逐件取，别凭记忆写码」，不收就是假告警：模型照实报出这个名字反而被拦。
    "forage_plan", "forage_demos", "forage_bytes", "forage_oversize",
    # 口播 / 素材片那一路（patch_video）：segment_cache 是产物里的逐窗账
    # （windows/rebuilt/reused/ledger 那一整块的顶层键名），segment_id 是补丁条目里
    # 与指针交叉核对的段号。两者都不是入参键也不是工具名，不收就是假告警——
    # 技能正文教模型「照 segment_cache 那份账说话」，被拦下来读者反而看不到真字段名。
    "segment_cache", "segment_id",
    # 一次性出片（make_short_video / SimpleFilmNode）的回执键：mp4_url 是成片直链，
    # sources_note 是出处对账那句话，numbers_without_source 是「哪几镜的数字没标出处」。
    # 三者只活在返回体里（`tools/short_video.py:259-267`、
    # `open_storyline/simple/pipeline.py:OUTPUT_FIELDS`），技能正文按它们教模型
    # 「把链接原样贴出来、没出处的数字如实点名」，schema 扫不出来，不收就是假告警。
    "mp4_url", "sources_note", "numbers_without_source",
})

MCP_PREFIX = "storyline_"

def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def enum_of(spec: Any) -> set[str]:
    """一份参数声明里所有可能的取值（enum / const / oneOf 分支 + 嵌套子字段里声明的取值）。

    用于词表：计划一旦真把带 enum 的节点写进 steps，模型就会在 why/expectation 里
    顺口提它的取值（如 ``script_template_rec`` 的 ``tpl_vlog_3act``）。不收就会判成
    「引用了不存在的名字」——真机实测：模型按提示补上了节点，紧接着因为文案里写了
    模板 id 被判臆造，卡连着两轮出不来。

    为什么要往 ``properties`` / ``items`` 里递归：像分镜这种「一个参数装整棵结构」的
    节点，取值清单挂在子字段上（每镜的版式组件名在 ``shots[].card`` 的 enum 里）。
    只看顶层就收不到它，而模型在计划文案里写「版式选 dict_entry」是完全正常的表达。

    只在声明里明确列出的值才算，不做任何猜测。
    """
    out: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or not isinstance(node, Mapping):
            return
        for key in ("enum", "examples"):
            raw = node.get(key)
            if isinstance(raw, (list, tuple)):
                out.update(str(v) for v in raw if isinstance(v, (str, int, float, bool)))
        if "const" in node:
            out.add(str(node["const"]))
        if node.get("type") == "boolean":
            out.update({"true", "false"})
        for branch in node.get("oneOf") or node.get("anyOf") or ():
            walk(branch, depth + 1)
        props = node.get("properties")
        if isinstance(props, Mapping):
            for sub in props.values():
                walk(sub, depth + 1)
        for key in ("items", "additionalProperties"):
            sub = node.get(key)
            if isinstance(sub, Mapping):
                walk(sub, depth + 1)
            elif isinstance(sub, (list, tuple)):
                for item in sub:
                    walk(item, depth + 1)

    walk(spec)
    return out


def as_card_value(value: Any) -> str:
    """卡面上的枚举值统一成字符串：布尔写成 true/false，数字去掉多余 .0。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = clean(value)
    if re.fullmatch(r"-?\d+\.0", text):
        return text[:-2]
    return text


def neutralize(text: str) -> str:
    """中和自定义文本里的标签：``<``/``>`` 换成全角，用户写不出闭合标签。

    这是 prompt 注入面，不只是 XSS 面：诉求原样进 system 段，若允许
    ``</user_custom_requests>`` 出现，用户就能自己开一段 system 指令。
    """
    return text.replace("<", "＜").replace(">", "＞")

@dataclass
class PlanIssues:
    """一次校验的结论：errors 非空即打回，warnings 只在卡上挂角标。

    ``errors`` 是原文清单，**只增不改**（``agent.py`` 的确认帧那条路直接
    ``"；".join(errors)`` 用它）。这里另记每条意见**出自哪份候选计划**
    （``sources`` 与 ``errors`` 一一对应），好让 ``groups()`` 把「只有计划标签不同」
    的同一件事合成一条——见它的说明。
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 每条 error 的来源计划标签；"" = 与具体哪一份候选无关（如 plan_id 重复）。
    sources: list[str] = field(default_factory=list)
    #: 当前正在校验的那一份计划的标签，由 ``PlanValidator`` 进入每份计划时设置。
    current: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.sources.append(self.current)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def groups(self) -> list[tuple[str, int]]:
        """errors → 合并去重后的清单 ``(文本, 命中处数)``，按首次出现排序。

        **合并判据**：两句话去掉开头那截「计划 {来源标签}」之后**逐字节相同**。
        那说明撞的是同一判据、同一步骤、同一组取值，只是分别发生在两份候选计划上
        ——分开列一遍不提供任何新信息，却按候选数**成倍**吃清单窗口。

        为什么这不是小事（真机实测，14 镜那份「薄肌理论」）：一次提交 3 份候选，
        校验出 56 条，列出的 12 条**全是第一份候选的**——另外两份的同一批毛病一条
        也没露头（回执只写「另有 42 条」）。模型照着能看见的 12 条改完再交，第二轮
        又是 46 条、列出 11 条，还多撞进「单值开关没有选择余地」那条规则。两次
        打回用完，框架把它推去问用户，整条 run 到此为止。**根子不在「没列全」，
        而在「数不清」**：模型没有任何办法知道自己还差几条、差在哪几份上，只能
        一份一份地试，而候选份数就是那个乘数。

        不合并的两种情形，都是宁可不合也不许合错：来源标签为空（与候选无关的意见）、
        去掉标签后仍有差异（判据/步骤/取值确实不同）。同一判据在同一份计划上重复
        命中（如一个节点出现了三次）会如实标出「共命中 N 处」，不抹成一处。
        """
        rows: list[list[Any]] = []          # [文本, 处数, 命中标签]
        index_of: dict[str, int] = {}
        for i, msg in enumerate(self.errors):
            tag = self.sources[i] if i < len(self.sources) else ""
            head = f"计划 {tag}"
            body = msg[len(head):] if tag and msg.startswith(head) else None
            # 键：带标签的意见按「去掉标签的正文」合并，不带标签的按原文（连同来源）
            key = f"{tag}\x00{msg}" if body is None else f"\x00{body}"
            pos = index_of.get(key)
            if pos is None:
                index_of[key] = len(rows)
                rows.append([msg, 1, [tag] if body is not None else []])
                continue
            row = rows[pos]
            row[1] += 1
            if body is not None and tag not in row[2]:
                row[2].append(tag)
                row[0] = f"计划 {'、'.join(row[2])}{body}"
        return [(row[0] + (f"（这一条判据共命中 {row[1]} 处）" if row[1] > len(row[2]) else ""),
                 int(row[1])) for row in rows]

def topology_view(contract: Any, steps: Any) -> list[dict[str, Any]]:
    """原始步骤 → 够拓扑检查用的最小形状（位置、节点名、契约里现成的 requires）。

    给「已经出现步骤级错误」的那条路用：拓扑与依赖两道检查只读这几个字段，而
    ``requires`` 按节点名问契约就有，不依赖这一步归一化成功。于是「参数不合规」
    与「group_clips 没上卡」能在同一次打回里一起交给模型，不必交两回卡、吃两回打回。

    ``skippable`` 一律 False：跳过规则的判据（下游踩没踩着它）要归一化后的完整
    步骤集才成立，这条路上不跑它。
    """
    out: list[dict[str, Any]] = []
    for index, step in enumerate(steps or (), start=1):
        if not isinstance(step, Mapping):
            continue
        node = clean(step.get("node"))
        if not node:
            continue
        node_contract = contract.get(node) if contract is not None else None
        out.append({"seq": index, "node": node, "skippable": False, "skip_reason": "",
                    "requires": list(node_contract.requires)
                    if node_contract is not None else []})
    return out


def add_implicit_skills(step: dict[str, Any], implicit: Sequence[str],
                        skills: Mapping[str, Any]) -> None:
    """把「这个节点离不开、模型却没写上卡」的技能补进步骤的 skills_hint（不重复、保序）。

    只补清单里查得到且当前可用的：卡上挂一个点开是空的技能，比不补更坏。
    """
    hints = step["skills_hint"]
    for name in implicit:
        skill = skills.get(name)
        if name in hints or skill is None or not getattr(skill, "available", True):
            continue
        hints.append(name)


def coerce_plans(payload: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(payload, Mapping):
        payload = payload.get("plans", payload)
    if isinstance(payload, Mapping):        # {"p1": {...}} 这种写法也接住
        payload = list(payload.values())
    if not isinstance(payload, list):
        return None
    return [p for p in payload if isinstance(p, Mapping)]
