# -*- coding: utf-8 -*-
"""代码镜这条链的补丁装配（spec §6 的白名单四条 + §12.7 的重烧口径）。

装配器只管「改在对的地方」，取值合不合法归分镜闸门管。为什么不并进 `hitmap.apply_patch`
那条通用补丁器：字段指针 `/shots/i/fields/{name}/value` 的定位方式与它不同——`fields`
是一个**数组**，而数组下标在这里没有意义（表单上的顺序不是身份）。`apply_patch` 走数组
只认 `int(seg)`，所以这一条必须在本模块按 **name** 找。
（字段名的字符集由 `code_spec.FIELD_NAME_RE` 锁死成 JS 标识符，`/` 与 `~` 进不来，
所以按名字找永远不会与下标写法撞车。）

两条不能忘的口径：

1. **改字段值不重过闸①**。代码字节没动，lint 的结论不可能变；重跑一次只是白等十秒。
   改了 `code` 才必须重过闸①，而且必须**在落盘之前**——绕过 lint 改代码等于闸① 不存在。
   复跑的本体在下面的 `relint_codes`：它收一个注入进来的 lint 可调用对象，所以这一层
   谈得上「出容器外呼」也能被离线用例拿假服务跑。节点只负责决定何时调它。
2. **越界打回，不钳值**。`min`/`max` 拦在外面并把区间报给用户；钳了就是「表单填 120、
   画上是 80」，而这条通道存在的理由恰恰是消灭这种不一致（闸⑥ 是它的动态那一半）。

值一律先过 `code_spec.normalize_field_value` 再落盘：`#F60` 与 `#ff6600`、`41` 与
`41.0` 是同一个像素，归一之后「提交了个一模一样的值」就能被改动账认出来并拒掉，
而不是白烧一次分钟级渲染（旧链 #143 那次「局部改其实整片重烧」的同族）。
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from . import code_spec as cspec
from . import hitmap as m_hit

#: 整镜改写允许的那几栏 = ``validate_code_spec`` 归一之后的镜头键（不含 ``id``：
#: 镜号是命中表条目、按镜切片缓存与改前改后对比帧共同的锚点，改号会让三者同时错位）。
EDITABLE_SHOT_FIELDS = ("code", "text", "highlight", "fields",
                        "min_duration_sec", "texture", "source")

#: 白名单五条的**人话形状**。指针被拒时把这行交给用户——他能改的就这五样，
#: 猜第六样（比如 `/shots/0/fields/radius/min`）只会得到另一条错误。
POINTER_GRAMMAR = ("/shots/<i>/code", "/shots/<i>/text", "/shots/<i>/source",
                   "/shots/<i>/fields/<name>/value", "/shots/<i>/texture")


def shot_id(shot: Any, index: int = -1) -> str:
    return str((shot or {}).get("id") or f"s{index + 1:02d}")


def shot_ids(shots: Sequence[Any]) -> list[str]:
    return [shot_id(s, i) for i, s in enumerate(shots)]


def _err(items: list[str], msg: str) -> None:
    items.append(msg)


def _fail(errors: list[str]) -> None:
    raise ValueError("\n".join(f"  · {e}" for e in errors[:12])
                     + (f"\n  · …另有 {len(errors) - 12} 条同类问题"
                        if len(errors) > 12 else ""))


def _parse_pointer(pointer: str) -> tuple[int, str, str] | str:
    """指针 → (镜号下标, 形态, 字段名)，形态 ∈ {code, text, source, field, texture}。

    认不出来时返回**一句错误文本**而不是抛：装配器要一次把整批补丁的错误说完，
    逐条抛的话用户改完第一条再提交才见到第二条，一轮一次分钟级渲染等不起。
    """
    segs = m_hit._segs(pointer)
    if len(segs) < 2 or segs[0] != "shots" or not segs[1].isdigit():
        return (f"指针 {pointer!r} 不在镜头子树上。命中表只认这五条："
                + "、".join(POINTER_GRAMMAR))
    i = int(segs[1])
    if len(segs) == 3 and segs[2] in ("code", "text", "source", "texture"):
        return (i, segs[2], "")
    if len(segs) == 5 and segs[2] == "fields" and segs[4] == "value":
        return (i, "field", segs[3])
    return (f"指针 {pointer!r} 不在白名单里。能改的就这五条："
            + "、".join(POINTER_GRAMMAR)
            + "；字段清单里还有 min/max/step/label 那几栏，它们不改像素也不该被改——"
              "控件的边界是契约，不是用户要绕的东西")


def _apply_field(shot: dict[str, Any], name: str, value: Any,
                 pointer: str) -> str | None:
    """按 name 找到一个字段并落值。→ 错误文本，None 表示落下去了。"""
    fields = shot.get("fields") or []
    field = next((f for f in fields if str(f.get("name") or "") == name), None)
    if field is None:
        declared = "、".join(str(f.get("name")) for f in fields) or "（这一镜没交出旋钮）"
        return (f"指针 {pointer} 要改的字段 {name!r} 不在这一镜的清单里（现有：{declared}）"
                "——字段清单以这一版命中表为准；这一镜没画出来的旋钮不能凭空改")
    bad = cspec.check_field_value(field, value)
    if bad:
        return f"指针 {pointer} 被拒：{bad}"
    field["value"] = cspec.normalize_field_value(str(field.get("type")), value)
    return None


def plan_code_patch(base: dict[str, Any], *,
                    edits: Sequence[Mapping[str, Any]] = (),
                    shot_sets: Sequence[Mapping[str, Any]] = (),
                    remove_shots: Sequence[str] = (),
                    reorder: Sequence[str] = ()) -> tuple[dict[str, Any], dict[str, Any]]:
    """→ (改后的原始 spec, 改动账)。任何一条落不下去就 ``ValueError`` 一次说完。

    返回的 spec **尚未再归一**——调用方还要过 ``validate_code_spec``。这一层只管
    「改在对的地方」，取值合不合法归分镜闸门管；两层错误清单都带镜号，但「照清单改完
    再来」这句只该有一个出处，否则用户会收到两份口径不一的退回清单。
    """
    if not isinstance(base, Mapping) or not base.get("shots"):
        raise ValueError("局部改：那一版成片没有可改的分镜（motion_spec 读不到，"
                         "或里面没有镜头）")

    errors: list[str] = []
    doc = copy.deepcopy(dict(base))
    base_shots = copy.deepcopy(list(base["shots"]))
    base_ids = shot_ids(base["shots"])

    # ── ① 指针改值：按 base 的下标落笔（必须先落笔再重排：指针带的是**这一版**的下标，
    # 重排之后再落笔会把值写进别的镜）──────────────────────────────────────────
    applied = 0
    for n, item in enumerate(edits or (), 1):
        if not isinstance(item, Mapping):
            _err(errors, f"改动 {n}：不是对象（每条写成 {{pointer, value}}，"
                         "pointer 取自命中表条目的 pointer）")
            continue
        pointer = str(item.get("pointer") or "")
        if "value" not in item:
            _err(errors, f"改动 {n}：没有 value 键（这一格要改成什么；写成 null 也算给了）")
            continue
        parsed = _parse_pointer(pointer)
        if isinstance(parsed, str):
            _err(errors, f"改动 {n}：{parsed}")
            continue
        i, kind, name = parsed
        if i >= len(doc["shots"]):
            _err(errors, f"改动 {n}：指针指向不存在的第 {i + 1} 镜"
                         f"（这一版只有 {len(doc['shots'])} 镜）：{pointer}")
            continue
        want = str(item.get("shot") or "")
        if want and base_ids[i] != want:
            _err(errors, f"改动 {n}：条目指认 {want}，但指针 {pointer} 落在第 {i + 1} 镜"
                         f"（{base_ids[i]}）上——命中表与这一版分镜对不上，"
                         "请按当前版本的表重取指针")
            continue
        if kind == "field":
            bad = _apply_field(doc["shots"][i], name, item["value"], pointer)
            if bad:
                _err(errors, f"改动 {n}：{bad}")
                continue
        elif kind == "source":
            # source 在归一结果里永远是对象，但用户交来的必须是同样的形状：
            # 写成一个字符串会把「出处」这一栏变成一句没人解析得了的话（出处闸读不到键）。
            if not isinstance(item["value"], Mapping):
                _err(errors, f"改动 {n}：source 要写成对象（如 {{\"标题\": …, \"链接\": …}}），"
                             f"收到 {item['value']!r}")
                continue
            doc["shots"][i] = {**doc["shots"][i], "source": dict(item["value"])}
        elif kind == "texture":
            # 空串是合法值：它的意思是「这一镜不表态，跟随整片默认那一档」，而 "none"
            # 的意思是「这一镜明确不加质感」。这两个形状在界面上是两个选项，不许合并。
            # `TEXTURES` 里没有空串（它列的是「有滤镜链的档名」），所以这里不能裸判成员——
            # 裸判会把界面「跟整片默认」那一格打回，用户点了却说「改不动」。
            tex = str(item["value"] if item["value"] is not None else "").strip().lower()
            if tex and tex not in cspec.TEXTURES:
                _err(errors, f"改动 {n}：texture={tex!r} 没有对应的滤镜链"
                             f"（可用：{'、'.join(cspec.TEXTURES)}，或写空串表示跟整片默认）"
                             "——打回不钳值，钳了就是账面一档、画面另一档")
                continue
            doc["shots"][i] = {**doc["shots"][i], "texture": tex}
        else:
            if kind == "code" and not str(item["value"] or "").strip():
                _err(errors, f"改动 {n}：code 不能改成空的——这条链没有默认版式可退，"
                             "空代码渲出来就是一张空画面")
                continue
            try:
                doc = m_hit.apply_patch(doc, pointer, item["value"])
            except (ValueError, IndexError, KeyError) as exc:
                _err(errors, f"改动 {n} 落不下去：{exc}")
                continue
        applied += 1

    # ── ② 整镜改写：允许一次写好几栏，也允许新增这一镜原本没有的键 ──────────
    # 为什么要有这一条而不让指针包打天下：`/shots/i/fields/…` 只能改**已声明**字段的值，
    # 给一镜补上原本没交的旋钮（或整段换代码）必须有个合法入口。
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
                     "要重做整片请走 plan_motion → render_motion_code，不叫局部改")
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


def changed_codes(base: Mapping[str, Any], doc: Mapping[str, Any]
                  ) -> dict[str, str]:
    """本次改动里**代码字节变了**的那几镜 → {镜号: 新代码}。

    闸① 的复跑只该为这几镜花钱：值改动不动代码字节，lint 结论不可能变（§12.7）。
    这个判断放在这里而不是节点里，是因为它比的是同一份「镜号 → code」，
    与上面改动账用的是同一个锚点——两处各比一遍迟早一个认字节一个认别的。
    """
    old = {shot_id(s, i): str(s.get("code") or "")
           for i, s in enumerate(base.get("shots") or [])}
    out: dict[str, str] = {}
    for i, s in enumerate(doc.get("shots") or []):
        sid = shot_id(s, i)
        code = str(s.get("code") or "")
        if sid in old and old[sid] != code:
            out[sid] = code
        elif sid not in old and code.strip():
            out[sid] = code
    return out


def declared_names(doc: Mapping[str, Any]) -> dict[str, list[str]]:
    """{镜号: 这一镜声明的字段名}——闸① 的字段对账三条规则拿它比。

    名单不在代码字节里，必须由调用方显交（`remotion_client.lint` 那条口径）；
    漏交等于让「声明未读 / 读未声明 / <F> 没引用 fields.x」这三条只看注入表，
    而补丁这一路恰好是**改完代码再看声明**的时刻，正是这三条该拦的场景。
    """
    return {shot_id(s, i): [str(f.get("name") or "")
                            for f in (s.get("fields") or []) if f.get("name")]
            for i, s in enumerate(doc.get("shots") or [])}


async def relint_codes(base: Mapping[str, Any], doc: Mapping[str, Any], *,
                       lint) -> list[str]:
    """代码字节变了的那几镜**重过闸①**。→ 带镜号的错误清单（空＝全过）。

    ``lint`` 是注入进来的 ``async (code, declared_names) -> {ok, errors}``——本模块不
    import 渲染客户端：装配层收函数，节点才去外呼，这样「code 指针复跑、fields 指针
    不复跑」这条口径能在离线用例里跑真代码而不必起容器。

    两种失败都算红、都不许静默放行：lint 回 ok=false，以及**外呼本身炸了**（容器没起、
    超时）。后一种如果放过去，就等于「渲染服务暂时不通」变成了「这次改的代码不用验」。
    """
    errors: list[str] = []
    names = declared_names(doc)
    for sid, code in changed_codes(base, doc).items():
        try:
            got = await lint(code, names.get(sid) or [])
        except Exception as exc:  # noqa: BLE001 - 验不了不等于验过了
            errors.append(f"{sid}：闸① 复跑没跑成（{type(exc).__name__}: "
                          f"{str(exc)[:160]}）——代码没验过就不许落盘，"
                          "旧版片子原样还在")
            continue
        if got.get("ok"):
            continue
        items = got.get("errors") or []
        if not items:
            errors.append(f"{sid}：闸① 拒绝但没给出条目，原始回执 "
                          f"{str(got)[:200]}")
            continue
        for e in items[:6]:
            errors.append(f"{sid}：闸① {e.get('rule') or '未报规则'}"
                          f" 第 {e.get('line', '?')} 行："
                          f"{str(e.get('msg') or '')[:160]}")
    return errors
