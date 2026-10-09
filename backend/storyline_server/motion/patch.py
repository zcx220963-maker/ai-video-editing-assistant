# -*- coding: utf-8 -*-
"""局部改的补丁装配：把一次「选区改」的四种写法拼成一版新分镜，并在烧像素之前把错说完。

为什么单有这一层（而不是让出片节点连着 ``hitmap.apply_patch`` 直接写）：

* **次序必须有人钉住**。指针里的镜号是**改之前那一版**的下标，所以「先落笔、再重排」
  才是对的；反过来就把「第 2 镜的数字」写进了被换到第 2 位的另一镜上。四种入参
  （指针改值 / 整镜改写 / 删镜 / 重排）谁先谁后，是这条链路最容易悄悄写错的地方。
* **每一类都要带人话的错误**。改错了地方却不报错，是「选着改」最贵的失败模式：
  用户看见的画面是「该变的没变、不该变的变了」，而像素已经烧完，回不去。
* **不许交空补丁**。值与原来一样、指针来自改版前的命中表、前端把旧表往回提交——
  这几种都会出一版一模一样的片子并白烧一次分钟级渲染，所以在这里就拒。

本模块不加新合同，判据全在既有闸门上：落笔走 ``hitmap.apply_patch``（只认
``/shots/<i>/…``、不许凭空造字段），落完之后照样过 ``spec.validate_spec``
（枚举越界、高亮词不在文案里、版式空壳、这张卡不读的死键，全部带镜号退回）。
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from . import hitmap as m_hit
from . import spec as m_spec

#: 整镜改写允许的字段 = ``validate_spec`` 归一之后的镜头键。
#: 不含 ``id``：镜号是命中表、按镜缓存与改前改后对比帧共同的锚点，换掉它等于让这三样
#: 各自去找一个新归属——那不叫改一镜，叫偷偷换成另一镜。
#: 也不含 ``duration_sec``：归一结果里它已经叫 ``min_duration_sec``（有旁白时时钟归声音，
#: 这一栏只剩「下限」的意思），时间线上拖镜头长度改的就是它。
#: ``theme``/``overlay`` 在这里是**整份替换**的入口（样式表单一次给好几个旋钮，
#: 图层列表一次给一整叠）；单改一格走指针（``/shots/0/theme/bg``）就够了，
#: 两栏的每个键都由 ``spec._norm_theme``/``_norm_overlay`` 补齐过，指针永远落在已有字段上。
EDITABLE_SHOT_FIELDS = ("card", "text", "highlight", "label", "panel", "stamp",
                        "visual", "source", "min_duration_sec", "theme", "overlay")


def shot_id(shot: Any, index: int = -1) -> str:
    """镜头的身份（与 ``validate_spec`` 同一套缺省写法：缺 id 就按顺序补 sNN）。"""
    return str((shot or {}).get("id") or f"s{index + 1:02d}")


def shot_ids(shots: Sequence[Any]) -> list[str]:
    return [shot_id(s, i) for i, s in enumerate(shots)]


def _err(items: list[str], msg: str) -> None:
    items.append(msg)


def _fail(errors: list[str]) -> None:
    raise ValueError("\n".join(f"  · {e}" for e in errors[:12])
                     + (f"\n  · …另有 {len(errors) - 12} 条同类问题"
                        if len(errors) > 12 else ""))


def _ensure_style_defaults(shots: list[Any]) -> None:
    """就地给缺 ``theme``/``overlay`` 的镜头补上默认值（升级前那版片子没有这两栏）。

    补的不是凭空想的值，而是**那一版画出来就是这个样子**：样式栏的默认值与模板 CSS
    里那些 ``var(--paper,#f6f1e4)`` 的兜底同值，叠加层默认为空。不补齐的话，指针的
    末端落在一个当时还不存在的字段上，``apply_patch`` 只能拒收——用户对着旧片子
    点「换个背景色」，收到的却是「指针末端不是已有字段」。
    """
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        if "theme" not in shot:
            shot["theme"] = copy.deepcopy(m_spec.DEFAULT_THEME)
        if "overlay" not in shot:
            shot["overlay"] = []


def plan_patch(base: dict[str, Any], *,
               edits: Sequence[Mapping[str, Any]] = (),
               shot_sets: Sequence[Mapping[str, Any]] = (),
               remove_shots: Sequence[str] = (),
               reorder: Sequence[str] = ()) -> tuple[dict[str, Any], dict[str, Any]]:
    """→ (改后的原始 spec, 改动账)。任何一条落不下去就 ``ValueError`` 一次说完，不留半成品。

    返回的 spec **尚未再归一**——调用方还要过 ``validate_spec``。这一层只管「改在对的
    地方」，取值合不合法归分镜闸门管；两层的错误清单都带镜号，但「照清单改完再来」
    这句只该有一个出处，否则用户会收到两份口径不一的退回清单。
    """
    if not isinstance(base, Mapping) or not base.get("shots"):
        raise ValueError("局部改：那一版成片没有可改的分镜（motion_spec 读不到，或里面没有镜头）")

    errors: list[str] = []
    doc = copy.deepcopy(dict(base))
    # 两边都要补：只补 doc 的话「补齐 theme/overlay 这一栏」本身就成了每镜都有的
    # 一处差异，改一镜的样式会连带整片重烧（改动账比的是字典相等，不是像素）。
    base_shots = copy.deepcopy(list(base["shots"]))
    _ensure_style_defaults(doc["shots"])
    _ensure_style_defaults(base_shots)
    base_ids = shot_ids(base["shots"])

    # ── ① 指针改值（命中表给的就是这一种）：按 base 的下标落笔 ──────────────
    applied = 0
    for n, item in enumerate(edits or (), 1):
        if not isinstance(item, Mapping):
            _err(errors, f"改动 {n}：不是对象（每条写成 {{pointer, value}}，"
                         "pointer 取自命中表条目的 field）")
            continue
        pointer = str(item.get("pointer") or "")
        if not pointer.startswith("/shots/"):
            _err(errors, f"改动 {n}：pointer={pointer!r} 不在镜头子树上——"
                         "命中表只量 /shots/<镜号>/… 这一棵，越界的指针无处落笔")
            continue
        if "value" not in item:
            _err(errors, f"改动 {n}：没有 value 键（这一格要改成什么；写成 null 也算给了）")
            continue
        segs = m_hit._segs(pointer)          # 与 apply_patch 同一套 ~1/~0 转义口径
        if segs[-1] == "id":
            _err(errors, f"改动 {n}：改的是镜号（{pointer}）。镜号是命中表、按镜缓存与"
                         "对比帧共同的锚点，换了它这三样就都指到别处；"
                         "要改观众看到的说法请改 text")
            continue
        want = str(item.get("shot") or "")
        if want and segs[1].isdigit():
            i = int(segs[1])
            if i < len(base_ids) and base_ids[i] != want:
                _err(errors, f"改动 {n}：条目指认 {want}，但指针 {pointer} 落在第 {i + 1} 镜"
                             f"（{base_ids[i]}）上——命中表与这一版分镜对不上，"
                             "请按当前版本的表重取指针")
                continue
        try:
            doc = m_hit.apply_patch(doc, pointer, item["value"])
        except (ValueError, IndexError, KeyError) as exc:
            _err(errors, f"改动 {n} 落不下去：{exc}")
            continue
        applied += 1

    # ── ② 整镜改写：允许一次写好几栏，也允许新增这张卡认识的键 ──────────────
    # 为什么要有这一条而不让指针包打天下：apply_patch 拒绝写到「不存在的字段」上
    # （凭空的字段名十有八九是猜的，写进去画面上什么都不会多），可属性表单确实要给
    # 一张原本没有 panel/没有 stamp 的卡补上这一栏——那必须有个合法的入口。
    sets_applied = 0
    for n, item in enumerate(shot_sets or (), 1):
        if not isinstance(item, Mapping):
            _err(errors, f"改写 {n}：不是对象（每条写成 {{shot, set}}）")
            continue
        sid = str(item.get("shot") or "")
        current = shot_ids(doc["shots"])
        if sid not in current:
            _err(errors, f"改写 {n}：{sid} 不在这一版分镜里（现有：{'、'.join(current)}）")
            continue
        raw_set = item.get("set")
        if not isinstance(raw_set, Mapping) or not raw_set:
            _err(errors, f"改写 {n}：set 必须是非空对象（这一镜要改成什么样的各栏值）")
            continue
        if "id" in raw_set:
            _err(errors, f"改写 {n}：set 里不许带 id（镜号是命中表与缓存的锚点）")
            continue
        unknown = [str(k) for k in raw_set if str(k) not in EDITABLE_SHOT_FIELDS]
        if unknown:
            _err(errors, f"改写 {n}：{'、'.join(unknown)} 不是这一镜有的栏"
                         f"（可改：{'、'.join(EDITABLE_SHOT_FIELDS)}）")
            continue
        i = current.index(sid)
        doc["shots"][i] = {**doc["shots"][i], **copy.deepcopy(dict(raw_set))}
        sets_applied += 1

    if errors:
        _fail(errors)

    # ── ③ 删镜（按镜号删，不按下标：前端手里的是镜号）──────────────────────
    removed: list[str] = []
    for sid in dict.fromkeys(str(x) for x in (remove_shots or ())):
        if sid not in shot_ids(doc["shots"]):
            _err(errors, f"要删的 {sid} 不在这一版里（现有：{'、'.join(shot_ids(doc['shots']))}）")
            continue
        removed.append(sid)
    if removed and len(removed) >= len(doc["shots"]):
        _err(errors, f"这一版一共 {len(doc['shots'])} 镜，全删了就没有片子了——"
                     "要重做整片请走 plan_motion → render_motion_video，不叫局部改")
        removed = []
    if removed:
        doc["shots"] = [s for s in doc["shots"] if shot_id(s) not in removed]

    # ── ④ 重排：必须交**整套**镜号（少一个就是「顺便删了」，那是另一件事）───
    reordered = False
    moved = [str(x) for x in (reorder or ())]
    if moved:
        left = shot_ids(doc["shots"])
        dup = [x for x in dict.fromkeys(moved) if moved.count(x) > 1]
        extra = [x for x in moved if x not in left]
        missing = [x for x in left if x not in moved]
        if dup or extra or missing:
            _err(errors, "重排清单不成一套："
                         + (f"重复 {'、'.join(dup)}；" if dup else "")
                         + (f"这一版里没有 {'、'.join(extra)}；" if extra else "")
                         + (f"漏了 {'、'.join(missing)}（要删镜请写进 remove_shots，"
                            f"重排只做顺序、不改集合）" if missing else ""))
        else:
            by_id = {shot_id(s, i): s for i, s in enumerate(doc["shots"])}
            doc["shots"] = [by_id[x] for x in moved]
            reordered = moved != left

    if errors:
        _fail(errors)

    # 改动账按「镜内容有没有变」算，而不是按调用方给了几条补丁算：值写回原样、
    # 或者两条补丁改同一格，都只该重烧一次像素——账必须与将要烧的东西一致。
    before = {shot_id(s, i): s for i, s in enumerate(base_shots)}
    changed = [sid for sid, s in
               zip(shot_ids(doc["shots"]), doc["shots"])
               if sid not in before or s != before[sid]]
    if not changed and not removed and not reordered:
        raise ValueError(
            "这一版补丁没有改动任何一格（提交的值与原来一样，或指针指错了栏）——"
            "所以不烧这次渲染：出一版一模一样的片子，界面上看着像「改完了」，"
            "其实还是旧的。请核对命中表给的 pointer 与该格当前值")

    return doc, {"changed_shots": changed, "removed_shots": removed,
                 "reordered": reordered, "edits_applied": applied,
                 "shot_sets_applied": sets_applied}
