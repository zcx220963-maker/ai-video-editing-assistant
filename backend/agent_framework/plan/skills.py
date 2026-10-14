"""技能预注入：执行轮起步把各步 skills_hint 声明的技能正文直接拼进上下文。

不赌模型自己点 ``load_skill``——``always=true`` 的技能已由常驻上下文给过，这里去重。

T42 起这里多了一半职责：**这一轮该发哪几块正文**（``sections_for_plan``）。技能正文从
「整段全量」改成「默认集 + 按需块」（分段标记与装配住在 ``skill.py``），而「按需」的判据
住在这里——它与 ``knobs.plan_node_hits`` 是同一件事的两个面（都是「本轮计划用得到什么」），
所以共用 ``knobs.bare_node`` 一个节点名口径，不新写第二套。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .knobs import bare_node
from .support import clean

__all__ = ["preload_skills", "sections_for_plan", "changed_film_knobs",
           "wants_sound_design", "WHITEBOARD_SECTIONS", "SUPPLY_SECTIONS"]

#: 写代码的节点（本轮的正文块几乎全由它们决定）。与 ``vocab._IMPLICIT_SKILLS`` 同一份
#: 名单口径：那三个节点各挂一份 ``motion_explainer_skill``，所以这里就是「谁在写代码」。
MOTION_NODES = ("plan_motion", "render_motion_code", "patch_motion_code")

#: 「计划里要走图像/部件通道」的节点。判据是**节点在不在计划里**，不是「正文里提没提
#: 图」：供给腿真被排进计划，才轮得到 `image-channel` 那一节说「从哪取、取不到怎么办」。
#: 三个名字取自 ``asset_nodes`` 与 ``imagegen_nodes``（真实注册表里的节点名）。
IMAGE_NODES = ("search_illustration", "select_illustration", "imagegen")

#: 整片开关：卡面能勾、镜头代码一行都不用写的那些（§5.7 第②条的「字幕/BGM 两块按需」）。
#: ``entrance``/``exit``/``subtitle_mode`` 在缓存键里（见 ``code_spec`` 那条注释），
#: ``transition``/``transition_sec`` 其实**不在**——它们跑在拼接那一趟，换档不重烧任何一镜。
FILM_KNOB_KEYS = ("texture", "subtitle_mode", "transition", "transition_sec",
                  "entrance", "exit")

#: 只要计划里有写代码的节点就随身的块（T36 那三节：主体怎么具名、锚怎么写、纯文字镜配额）。
#: **必须常随**：它们讲的是「代码怎么写才过得了闸」，不是「某条通道才用得上」。
MOTION_SECTIONS = ("motion-anchors", "subject-naming", "text-only-quota")

#: 图像/部件通道的两块。成对发：讲「从哪取」与「取到之后动效闸怎么判」是同一件事的两半。
IMAGE_SECTIONS = ("image-channel", "asset-motion")

#: R11-T57 的两块：**部件怎么拼成一个场景**（闸㉕ 的活法）与**`scale_safe` 怎么用**。
#: 通道条件是 ``motion`` **且** ``image`` 两条腿同时在计划里，缺一不发——
#: 少了 ``image`` 这一腿，正文里那些 `search_illustration`/`asset_role` 今天根本调不到
#: （那是「教一条今天走不通的路」）；少了 ``motion`` 这一腿，本轮没有人写镜头代码，
#: 「一件放大铺满画面 = 一枚徽章」这句话没有读者。两块一起发，是因为第①条把「取不到件」
#: 之后的下一步指给了第②条（换一件 / 改当 scene 用），拆开发出去只会各说一半。
SUPPLY_SECTIONS = ("asset-parts", "scale-safe")

#: R12-T62 的那一块：**出图腿的视觉规范**（手绘纸底那一族：纸底 `#F5EBD7`、深灰线、
#: 点缀色 ≤3、画面内零文字）。通道条件是「**这一轮真会产出一张 raster 插画**」，
#: 而它的机器可见形态就是计划里有 `imagegen`（出图节点）——那条规矩要写进的就是
#: 这个节点唯一的入参 `prompt`。
#:
#: **为什么条件不是「有供给腿」**（`IMAGE_NODES` 那三条）：`search_illustration` /
#: `select_illustration` 取的是库里现成的件，它们的观感不由我们定，把「统一出图视觉
#: 规范」发给那一轮等于教模型一条它执行不了的路（§5.7 第②条那条「通道条件」的反面）。
#: 也**必须**同时有写代码的节点：没有镜头代码就没有 `reveal` 申报与 `staticFile` 引用，
#: 这一节的读者是「正在给某个镜头配图并把标注写出来的那个模型」，缺了它这一节没有读者。
WHITEBOARD_SECTIONS = ("whiteboard-imagegen",)


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


def sections_for_plan(compiled: Any, card: Any = None, *,
                      imagegen_visible: bool = True) -> tuple[str, ...]:
    """本片计划 → 这次点名要的正文块（升序；空表＝只要默认集）。

    ``compiled`` 是执行轮的编译结果（``{"steps": [{node, params, …}]}``），``card`` 是那张
    计划卡（可选，只为读 ``param_options[].default``）。判据六条，逐条都能反证：

    1. 计划里有写代码的节点（``MOTION_NODES``）→ 发 ``MOTION_SECTIONS`` 三块；
    2. 计划**动过**整片开关 → 追加 ``film-knobs``（见 ``changed_film_knobs``）；
    3. 计划里有供给腿节点（``IMAGE_NODES``）→ 发 ``IMAGE_SECTIONS`` 两块；
    4. 本片有旁白或配乐 → 追加 ``sound-design``（见 ``wants_sound_design``）；
    5. 写代码**且**有供给腿 → 追加 ``SUPPLY_SECTIONS`` 两块（T57；条件是两条腿**同时**
       在计划里，理由见 ``SUPPLY_SECTIONS`` 上面那段）；
    6. 写代码**且**计划里有 `imagegen` **且这一轮它真的调得到** → 追加
       ``WHITEBOARD_SECTIONS`` 一块（T62/T68；条件比第 5 条窄一档，理由见
       ``WHITEBOARD_SECTIONS`` 上面那段）。

    ``imagegen_visible=False``（R13-T68）＝ **这台机器没配出图后端**。此时把 ``imagegen``
    从节点集里整条摘掉：第 3/5/6 三条判据都当它不在计划里。为什么不是「照发、让节点
    自己降级」：那一节的正文讲的是「出图腿的视觉规范」，读者是「正在给某个镜头配图」
    的那个模型——而它这一轮调不到出图（``GenerateImageNode`` 会回一条降级回执）。
    发出去等于教一条今天走不通的路（§5.7 第②条「通道条件」的反面），而且烧到第 9、10 镜
    才停下来问用户，正是清单第②项那张四选项卡的根因。

    计划里**一个 motion 节点都没有**时返回空表：那一轮要么是常规剪辑（另一份技能），
    要么只是问一句——此时 ``preload_skills`` 的 ``names`` 本来也不含这份技能，
    ``sound-design`` / ``image-channel`` 那些块**字节数为 0**（§5.7 第②条的机器判据①）。
    """
    steps = _steps(compiled)
    nodes = _nodes(steps)
    if not imagegen_visible:
        nodes = nodes - {"imagegen"}
    writes_code = bool(nodes.intersection(MOTION_NODES))
    has_supply = bool(nodes.intersection(IMAGE_NODES))
    out: list[str] = []
    if writes_code:
        out.extend(MOTION_SECTIONS)
        if changed_film_knobs(compiled, card):
            out.append("film-knobs")
        if wants_sound_design(compiled):
            out.append("sound-design")
    if "patch_motion_code" in nodes:
        out.append("patch-motion")
    if has_supply:
        out.extend(IMAGE_SECTIONS)
    if writes_code and has_supply:
        out.extend(SUPPLY_SECTIONS)
    if writes_code and "imagegen" in nodes:
        out.extend(WHITEBOARD_SECTIONS)
    return tuple(sorted(set(out)))


async def preload_skills(loader: Any, names: Sequence[str], *,
                         resident: Iterable[str] = (),
                         sections: Iterable[str] = ()) -> list[str]:
    """执行轮起步预注入：各步声明的技能正文直接拼进上下文（不赌模型点 load_skill）。

    ``always=true`` 的技能已由 ``SkillManifestContextSource`` 常驻注入，这里不重复给。

    ``sections``（T42）：本轮计划点名要的分段块（来自 ``sections_for_plan``）。按技能
    **各取各的**——同一个段名在这份技能里没有就是没有（``Skill.assemble`` 静默跳过），
    调用方不必先问清单，这里也不为「一个都没命中」报错。没有分段标记的技能（今天绝大
    多数）行为逐字不变：整份正文照发。
    """
    if loader is None or not names:
        return []
    want = [str(s).strip() for s in (sections or ()) if str(s).strip()]
    skip = set(resident)
    out: list[str] = []
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
        body = assemble(want) if callable(assemble) else skill.body
        if not body:
            continue
        out.append(f'<skill_instructions name="{skill.name}">\n{body}\n'
                   f'</skill_instructions>')
    return out
