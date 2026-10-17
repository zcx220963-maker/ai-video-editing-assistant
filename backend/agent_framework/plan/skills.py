"""技能预注入：执行轮起步把各步 skills_hint 声明的技能正文直接拼进上下文。

不赌模型自己点 ``load_skill``——``always=true`` 的技能已由常驻上下文给过，这里去重。

T42 起这里多了一半职责：**这一轮建议模型看哪几块正文**（``sections_for_plan``）。技能
正文分「默认集」与「标记出来的几块」（分段标记与装配住在 ``skill.py``），而「哪几块值得
看」的判据住在这里——它与 ``knobs.plan_node_hits`` 是同一件事的两个面（都是「本轮计划
用得到什么」），所以共用 ``knobs.bare_node`` 一个节点名口径，不新写第二套。

2026-10-16：这份判断从「替模型注入」降成正文末尾的一行**分段目录**（§2.2）——技能读哪
一块是模型的事，服务端只把它看不见的那份清单摆出来。``preload_skills`` 里那句注释记着
这一条的账（原先六块正文无条件随每一轮发下去）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..skill import section_directory_line
from .knobs import bare_node
from .support import clean

__all__ = ["preload_skills", "sections_for_plan", "changed_film_knobs",
           "wants_sound_design"]

#: 写代码的节点（本轮的正文块几乎全由它们决定）。与 ``vocab._IMPLICIT_SKILLS`` 同一份
#: 名单口径：那三个节点各挂一份 ``motion_explainer_skill``，所以这里就是「谁在写代码」。
MOTION_NODES = ("plan_motion", "render_motion_code", "patch_motion_code")

#: 整片开关：卡面能勾、镜头代码一行都不用写的那些（§5.7 第②条的「字幕/BGM 两块按需」）。
#: ``entrance``/``exit``/``caption_style``/``highlight_style``/``palette`` 在缓存键里
#: （见 ``code_spec`` 那条注释），``transition``/``transition_sec`` 其实**不在**——
#: 它们跑在拼接那一趟，换档不重烧任何一镜。
#:
#: ``palette``（2026-10-17）在这里是因为它**改了影响的是每一镜的底色**，
#: 而这一份名单的用途正是「值变了要不要把技能正文捞出来重讲一遍」——
#: 用户在卡上把整片换成深海蓝，模型写代码时就得知道这件事，否则它照旧按米白排。
FILM_KNOB_KEYS = ("palette", "texture", "caption_style", "highlight_style",
                  "transition", "transition_sec", "entrance", "exit")

#: 只要计划里有写代码的节点就随身的块（T36 那三节：主体怎么具名、锚怎么写、每镜必画，
#: 2026-10-17 加 `draw-parts`：每一镜要画的那件东西怎么拆成零件库里的件）。
#: **必须常随**：它们讲的是「代码怎么写才过得了闸」，不是「某条通道才用得上」。
#: 第三节 2026-10-16 从 `text-only-quota`（纯文字镜配额）改名成 `draw-floor`（每镜必画）——
#: 配额那一档没有了，「几镜可以纯文字」这句话本身已不成立，名字留着就是骗读者。
#: `draw-parts`（2026-10-17）是闸㉝ 的对读那一半：㉝ 在契约层硬拦「一件具名件都没用上」，
#: 这一节教的是「怎么把文案里的那个东西拆成库里的件」——闸只判有没有用，像不像全在这一节。
MOTION_SECTIONS = ("motion-anchors", "subject-naming", "draw-floor", "draw-parts")

# 2026-10-16 拆掉的三组块（原本在这里，正文里那几节也一并删了）：
#   `IMAGE_SECTIONS`（`image-channel` / `asset-motion`）、`SUPPLY_SECTIONS`
#   （`asset-parts` / `scale-safe`）、`WHITEBOARD_SECTIONS`（`whiteboard-imagegen`）
# ——它们全都在讲「素材从哪取、取到的件怎么用」，而取材这条路已从产品里拿掉：
# 图形片的画面只有一路，模型逐镜写代码画（闸⑦「每镜必画」）。留下这几块等于
# 教模型一条今天调不到的路（§5.7 第②条「通道条件」的反面）。


def _flag(value: Any) -> bool:
    """开关值 → 真假（``True`` / ``"true"`` / ``"1"`` / ``"yes"`` 都算真）。"""
    if isinstance(value, bool):
        return value
    return clean(value).lower() in ("true", "1", "yes", "y")


def _steps(plan: Any) -> list[Mapping[str, Any]]:
    """计划 → 步骤清单（``{"steps": [...]}`` 或步骤序列两种形状都收）。"""
    rows = plan.get("steps") if isinstance(plan, Mapping) else plan
    return [s for s in (rows or ()) if isinstance(s, Mapping)]


def _nodes(steps: Sequence[Mapping[str, Any]]) -> set[str]:
    return {bare_node(s.get("node")) for s in steps if clean(s.get("node"))}


def _card_defaults(card: Any) -> dict[tuple[str, str], str]:
    """卡面默认值 → ``{(节点名, 参数名): 默认值}``。

    默认值按**节点**索引而不是按步骤：同一节点的入参形状只有一份（枚举与默认值都来自
    那份 schema），两处冗余的步骤（比如两个 ``render_motion_code``）默认值必然相同。
    """
    out: dict[tuple[str, str], str] = {}
    for step in _steps(card):
        node = bare_node(step.get("node"))
        for opt in step.get("param_options") or ():
            if isinstance(opt, Mapping):
                out[(node, clean(opt.get("key")))] = clean(opt.get("default"))
    return out


def changed_film_knobs(compiled: Any, card: Any = None) -> list[str]:
    """本轮计划**动过**的整片开关（升序，没动过就是空表）。

    「动过」＝ 编译后的值 ≠ 卡面默认值。这一条判要拿卡面（``param_options[].default``），
    是因为编译结果**看不出用户勾没勾**——``params`` 里放的是终值，用户没动过时它就是默认值。

    **拿不到卡面时（续跑路径只有编译结果）一律按「动过」算**：宁可多发那 2.7 KB 的
    「这几件事分别在哪儿、代价差多少」，也不能在用户真勾了 ``entrance`` 的那一轮把这段
    吞掉——吞掉的后果是模型照着默认值在每镜代码里手写一段进出场（这正是本节正文第一句
    要拦的那件事）。方向只有一个，且写在注释里，不是「有时候发有时候不发」。
    """
    defaults = _card_defaults(card)
    hits: set[str] = set()
    for step in _steps(compiled):
        node = bare_node(step.get("node"))
        params = step.get("params")
        if not isinstance(params, Mapping):
            continue
        for key in FILM_KNOB_KEYS:
            if key not in params:
                continue
            default = defaults.get((node, key))
            if default is None or clean(params.get(key)) != default:
                hits.add(key)
    return sorted(hits)


def wants_sound_design(compiled: Any) -> bool:
    """这一轮要不要发「声音：旁白与配乐」那一块。

    判据是本片的两个开关**终值**（编译结果里就有，不必回卡面）：`narration` 为真、
    或 `bgm` 非空 → 要。一个开关都读不到（计划里的 motion 步骤没有 params）时**按「要」算**
    ——与 ``changed_film_knobs`` 同一条方向：宁可多发那 933 B 的配乐纪律，
    也不能在真配了乐的那一轮把「配乐只从曲库选」这句话吞掉。
    """
    seen = False
    for step in _steps(compiled):
        if bare_node(step.get("node")) not in MOTION_NODES:
            continue
        params = step.get("params")
        if not isinstance(params, Mapping):
            continue
        seen = True
        if _flag(params.get("narration")) or clean(params.get("bgm")):
            return True
    return not seen


def sections_for_plan(compiled: Any, card: Any = None) -> tuple[str, ...]:
    """本片计划 → **值得模型考虑**的正文块（升序；空表＝没有建议，只有默认集）。

    2026-10-16 改口径（§2.2）：这份输出曾经是「这次替模型注入哪几块」，现在是
    「分段目录里给哪几块加一句『本轮计划用得上』」——**读不读由模型自己点**
    （``preload_skills`` 已不再把它们塞进上下文，见那一段的注释）。判据本身没变，
    逐条都能反证：

    1. 计划里有写代码的节点（``MOTION_NODES``）→ ``MOTION_SECTIONS`` 三块；
    2. 计划**动过**整片开关 → 追加 ``film-knobs``（见 ``changed_film_knobs``）；
    3. 计划里有 `patch_motion_code` → 追加 ``patch-motion``；
    4. 本片有旁白或配乐 → 追加 ``sound-design``（见 ``wants_sound_design``）。

    曾经还有一条「计划里有供给腿（`search_illustration`/`select_illustration`/`imagegen`）
    → 发图像与部件那几块」：那三个节点连同那几块正文 2026-10-16 一并删了，判据随之消失
    （见上面那段注释）。参数 ``imagegen_visible`` 也随之删掉——它的全部作用就是把
    `imagegen` 从节点集里摘掉，而 `imagegen` 已经不在了。

    计划里**一个 motion 节点都没有**时返回空表：那一轮要么是常规剪辑（另一份技能），
    要么只是问一句——此时 ``preload_skills`` 的 ``names`` 本来也不含这份技能，
    ``sound-design`` 那一块**字节数为 0**（§5.7 第②条的机器判据①）。

    ``compiled`` 是执行轮的编译结果（``{"steps": [{node, params, …}]}``），``card`` 是那张
    计划卡（可选，只为读 ``param_options[].default``）。
    """
    steps = _steps(compiled)
    nodes = _nodes(steps)
    writes_code = bool(nodes.intersection(MOTION_NODES))
    out: list[str] = []
    if writes_code:
        out.extend(MOTION_SECTIONS)
        if changed_film_knobs(compiled, card):
            out.append("film-knobs")
        if wants_sound_design(compiled):
            out.append("sound-design")
    if "patch_motion_code" in nodes:
        out.append("patch-motion")
    return tuple(sorted(set(out)))


async def preload_skills(loader: Any, names: Sequence[str], *,
                         resident: Iterable[str] = (),
                         sections: Iterable[str] = ()) -> list[str]:
    """执行轮起步预注入：各步声明的技能正文直接拼进上下文（不赌模型点 load_skill）。

    ``always=true`` 的技能已由 ``SkillManifestContextSource`` 常驻注入，这里不重复给。

    ``sections``（T42，2026-10-16 改口径）：本轮计划命中、**值得模型考虑**的分段块
    （来自 ``sections_for_plan``）。它**不再**把这些块塞进上下文——那正是 §2.2 要还
    回去的那件事：技能的哪一块该读，模型自己点。这里只把这份判断写成正文末尾的一行
    **分段目录**（含「本轮计划用得上哪几块」），取法是 ``load_skill(name, section=…)``。

    改成这一条的理由有两条，第二条是硬理由：
      · 计划读了哪几步→该用到哪些块，服务端比模型清楚（模型看不到那份块的清单），
        所以这句提示得说；但「说」不等于「替它读」。
      · 真机实测那份技能 42 KB，其中六块正文合计约 12 KB 无条件随每一轮发下去——
        计划里只用了 `sound-design` 的那一轮，另外五块也照样进上下文。

    没有分段标记的技能（今天绝大多数）行为逐字不变：整份正文照发、一个字不加。
    """
    if loader is None or not names:
        return []
    want = [str(s).strip() for s in (sections or ()) if str(s).strip()]
    skip = set(resident)
    out: list[str] = []
    carried: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = clean(name)
        if not key or key in seen or key in skip:
            continue
        seen.add(key)
        try:
            skill = await loader.get(key)
        except Exception:  # noqa: BLE001 - 预注入是增益，读不到就照常跑
            continue
        if skill is None or not getattr(skill, "available", True) or not skill.body:
            continue
        assemble = getattr(skill, "assemble", None)
        body = assemble(()) if callable(assemble) else skill.body
        if not body:
            continue
        directory = section_directory_line(skill.body, planned=want, included=False)
        out.append(f'<skill_instructions name="{skill.name}">\n{body}{directory}\n'
                   f'</skill_instructions>')
        carried.append(skill.name)
    if carried:
        out.append(
            "这一轮随身的技能：" + "、".join(sorted(set(carried)))
            + "。其中**有的是这一步的节点默认带的，不是你点名的**（服务端按节点挂了一份"
              "默认技能表 `_IMPLICIT_SKILLS`，为的是零素材那条链上模型拿不到任何素材事实）。"
              "技能读什么仍由你定：`<skills>` 清单里是**全部候选**，换一份用 "
              "`load_skill(name)`；随身这份里想只看某一块，用 "
              "`load_skill(name, section=键名)`（键名见上面那行分段目录）。")
    return out
