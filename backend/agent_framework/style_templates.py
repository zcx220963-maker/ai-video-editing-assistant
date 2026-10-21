# -*- coding: utf-8 -*-
"""风格模板库：种子、可见性、推送到渲染容器。

为什么要有这一层：此前模板是**烤进镜像**的 TSX（`remotion/src/lib/style/*.tsx`），
换一套视觉必须改代码、重建镜像。要让它成为「用户传参考片就能沉淀」的通用能力，
模板必须变成**库里的数据**：源码进 PG，发布时推给渲染容器，镜头代码按名字 import。

两条边界：

* **可执行与非可执行分开**：模板（TSX）在 `style_templates`，风格包（数值+正文）
  在 `skills(kind=style)`。用户只能通过产品流程**沉淀**模板，不能直接上传 TSX 绕过闸。
* **PG 是唯一真相，容器只是缓存**：渲染容器重建后目录会空，靠 `sync_validated()`
  从 PG 重推；推不动时只告警，不让一次网络抖动把整条出片链拦死（「堵死比白烧更坏」）。

系统模板（`owner_user_id IS NULL`）可以没有源码——它们随镜像发布，种子只登记元数据。
用户模板必须有源码，且只会落在 `generated/<name>`，与内置模块互不覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

ENV_URL = "REMOTION_URL"
DEFAULT_URL = "http://remotion:8010"
SEED_FILE = Path(__file__).resolve().parents[1] / "examples" / "templates.json"
USER_MODULE_PREFIX = "@dg/lib/style/generated/"


def seed_templates() -> list[dict[str, Any]]:
    """系统模板种子。文件缺失/写坏时返回空表——模板库空着仍能自由画。

    种子里可以写 `"probe_file": "probes/<name>.tsx"`：预览样例的 TSX 放在单独文件里，
    不必把几十行代码塞进 JSON 的一个字符串。读不到就当这一份没有 probe（预览那只
    接口会如实报「这套模板没有预览样例」），不让种子读取失败拖垮整张表。
    """
    try:
        raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for r in raw:
        if not (isinstance(r, Mapping) and r.get("name")):
            continue
        item = dict(r)
        rel = str(item.get("probe_file") or "").strip()
        if rel and not str(item.get("probe") or "").strip():
            try:
                path = (SEED_FILE.parent / rel).resolve()
                if path.is_relative_to(SEED_FILE.parent):
                    item["probe"] = path.read_text(encoding="utf-8")
            except OSError:
                pass
        out.append(item)
    return out


async def seed_system_templates(storage: Any) -> int:
    """把种子写进库（只补缺，不覆盖）。返回**新增**条数。"""
    added = 0
    for item in seed_templates():
        before = await storage.style_templates.get(str(item["name"]))
        await storage.style_templates.ensure(
            str(item["name"]),
            display=str(item.get("display") or ""),
            description=str(item.get("description") or ""),
            module=str(item.get("module") or ""),
            components=list(item.get("components") or []),
            source=str(item.get("source") or ""),
            manifest=dict(item.get("manifest") or {}),
            probe=str(item.get("probe") or ""),
            status=str(item.get("status") or "validated"),
            owner_user_id=None,
            validation=dict(item.get("validation") or {}),
        )
        if before is None:
            added += 1
        elif str(item.get("probe") or "").strip() and not str(before.get("probe") or "").strip():
            # 老库里已经有一行、只是当初没带预览样例：把样例补上。只补空的，
            # 绝不覆盖——用户沉淀的模板自己带 probe，种子没有资格改它。
            setter = getattr(storage.style_templates, "set_probe", None)
            if setter is not None:
                await setter(str(item["name"]), str(item["probe"]))
    return added


def visible(row: Mapping[str, Any], user_id: str) -> bool:
    owner = row.get("owner_user_id")
    return owner is None or owner == user_id


def source_hash(row: Mapping[str, Any]) -> str:
    """模板源码指纹。系统模板没有源码：用模块名 + 版本，随镜像变化。"""
    source = str(row.get("source") or "")
    if source:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"baked:{row.get('module') or ''}:{row.get('version') or 1}"


# 两种写法都认：`type XProps = { … };` 与 `interface XProps { … }`。
_PROPS_TYPE_RE = re.compile(
    r"(?:type\s+(\w+?)Props\s*=\s*\{|interface\s+(\w+?)Props\s*\{)(.*?)\}\s*;?", re.S)
# 字段之间用 `;` 或换行分隔：生成出来的 props 类型常常写在一行里
# （`{ wardrobe?: string; phase?: number }`），只按行锚定会漏掉后半截——
# 沉淀期就会误判「模板没有 wardrobe」，把一份好模板打回。
_PROP_FIELD_RE = re.compile(r"\s*([A-Za-z_]\w*)(\??)\s*:\s*([^;\n]+)")


def component_props(source: str) -> dict[str, str]:
    """公开别名：沉淀期要校验「参考片里会变的那几件，组件有没有对应 prop」。

    私有实现留在下面（`component_usage` 同一个来源）；对外只暴露这一个名字，
    免得写手/沉淀两侧各抄一份正则，漂了也不知道。
    """
    return _component_props(source)


def _component_props(source: str) -> dict[str, str]:
    """从模板源码里抽出每个组件的 props（名字 + 类型），给写手一份可照抄的签名。

    为什么要有它：模板只报「可用组件：WalkerFigure」时，模型得**猜**这个组件收什么
    参数。猜不中就不 import，改自己手画——真机 2026-10-19 的 3 镜正是这样漂掉的：
    模板里明明有走路循环，成片里人站着不动。给签名，import 才从「冒险」变成「照做」。
    """
    out: dict[str, str] = {}
    for m in _PROPS_TYPE_RE.finditer(source or ""):
        name = m.group(1) or m.group(2)
        # 联合类型常常写成多行（替换件增加场景取值时尤其常见）：
        # `sceneType?: 'a' | 'b'\n | 'c' | 'd'`。按行锚定的字段正则会把它
        # 截成第一行，写手只看得见前两个取值——「新增取值」等于没加。
        # 只把**不像新字段/结尾**的换行并成空格，字段之间的换行仍保留。
        body = re.sub(r"\n\s*(?![A-Za-z_]\w*\??\s*:|\})", " ", m.group(3))
        fields = []
        for f in _PROP_FIELD_RE.finditer(body):
            field, optional = f.group(1), f.group(2)
            typ = re.sub(r"\s+", " ", f.group(3)).strip()[:200]
            fields.append(f"{field}{optional}: {typ}")
        if fields:
            out[name] = "；".join(fields)
    return out


def _enum_values(contract_text: str) -> dict[str, list[str]]:
    """从 props 合约里抽出「字符串字面量联合类型」的取值。

    为什么只认字面量联合类型：`sceneType?: 'mountains' | 'city'` 里那两个词
    是**能真传进组件的值**；而沉淀时 variations 记下的「山峦 / 城市 / 战场」
    只是参考片里的语义标签，直接当 prop 值传给组件会落到缺省分支——看起来
    没报错，画面却永远是山。清单要把「能传的值」和「看一眼认识的语义」分开。

    自由类型（`accent?: string`、数字、元组）不进这里：颜色必须传 `#rrggbb`，
    「火橙」这种词只能当语义参考，不能当值。
    """
    out: dict[str, list[str]] = {}
    for field in re.split(r"；", contract_text or ""):
        m = re.match(r"\s*([A-Za-z_]\w*)\??\s*:\s*(.+?)\s*$", field)
        if not m:
            continue
        typ = m.group(2)
        values = re.findall(r"'([^']*)'", typ)
        rest = re.sub(r"'[^']*'", "", typ).replace("|", "").strip()
        if rest or len(values) < 2:
            continue
        out[m.group(1)] = [v for v in values if v]
    return out


#: 组件 props 的单位契约，跟在签名后面一起进提示词。
#:
#: 逐镜写代码那条链的通用规则是「坐标单位就是设备像素」，模板组件的约定恰好相反：
#: props 收画面比例，内部自己乘 width/height（见 `style_authoring.CODE_SYSTEM`）。
#: 不说死这条边界，模型会按默认规则传像素——真机 2026-10-19 的三个镜头正是这样：
#: `x={W*0.32}`、`groundY={H*0.82}`，人物与舞台被乘了第二遍、整块推出画外，
#: 预览三帧「一个人都没有」。
PROP_UNITS_NOTE = (
    "单位：以上位置/尺寸参数（x、y、groundY、heightRatio、widthRatio、pan、size 等）"
    "是**画面比例 0–1**，不是像素；颜色一律 `#rrggbb`。"
    "你自己的 `<svg>`/代码仍按设备像素画。")


def component_usage(row: Mapping[str, Any], *, max_chars: int = 1800) -> str:
    """组件清单 → 「名字（职责）：props」几行 + 单位契约。职责来自 components[].role。

    单位契约**固定留在末尾**：截断只截签名清单，不能把那句「是比例不是像素」
    先截掉——真机上缺的正是这一句。
    """
    props = _component_props(str(row.get("source") or ""))
    lines: list[str] = []
    for c in (row.get("components") or []):
        if isinstance(c, Mapping):
            name, role = str(c.get("name") or "").strip(), str(c.get("role") or "").strip()
        else:
            name, role = str(c or "").strip(), ""
        if not name:
            continue
        line = f"- `{name}`" + (f"（{role}）" if role else "")
        if props.get(name):
            line += f"：{props[name]}"
        lines.append(line)
    if not lines:
        return ""
    note = PROP_UNITS_NOTE
    room = max_chars - len(note) - 1
    body = "\n".join(lines)[:max(0, room)]
    return f"{body}\n{note}"


def prop_names(contract: str) -> list[str]:
    """`x?: number；groundY?: number` → `['x', 'groundY']`。

    `component_props()` 给人看的是签名文本；槽位合约要的是**名字集合**——
    替换组件必须原样接收同一组 props，名字对不上就接不住模板传进来的值。
    """
    out: list[str] = []
    for part in re.split(r"[；;\n]", contract or ""):
        m = re.match(r"\s*([A-Za-z_]\w*)", part)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _facet_kind(manifest: Mapping[str, Any], comp: str) -> str:
    """没有 `layers` 的老模板：从 facets 反推这个组件是哪类槽位。

    只读已沉淀的数据，不对某套风格写分支：文字项点名的组件 → text；
    film 作用域的连续轨道 → subject；shot 作用域轨道 → background；其余 other。
    """
    defaults = manifest.get("defaults") if isinstance(manifest.get("defaults"), Mapping) else {}
    facets = defaults.get("facets") if isinstance(defaults.get("facets"), Mapping) else {}
    text = facets.get("text") if isinstance(facets.get("text"), Mapping) else {}
    for item in text.get("items") or []:
        if isinstance(item, Mapping) and str(item.get("component") or "") == comp:
            return "text"
    motion = facets.get("motion") if isinstance(facets.get("motion"), Mapping) else {}
    for track in motion.get("tracks") or []:
        if not isinstance(track, Mapping) or str(track.get("component") or "") != comp:
            continue
        if track.get("scope") == "film" and track.get("continuous"):
            return "subject"
        return "background"
    return "other"


def template_slots(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """模板的**槽位清单**：每个导出组件就是一个可替换槽位。

    为什么以组件为单位：写手逐镜写代码时本来就是按组件 import 的；把它换成
    另一份满足同一 props 合约的实现，就是「整体还是那套模板，只换一个零件」。
    槽位数据只描述「这个组件收什么 props、有哪些替换件」，不写某套风格的组件名。

    优先读沉淀时写下的 `manifest.defaults.slots`（含 layers 的标签/几何）；
    老模板没有这一节时，从 `components` + facets 现场推一份，保证存量模板也能换件。
    替换件统一记在 `manifest.variants`，重新沉淀模板时不会被 defaults 覆盖。
    """
    manifest = row.get("manifest") if isinstance(row.get("manifest"), Mapping) else {}
    defaults = manifest.get("defaults") if isinstance(manifest.get("defaults"), Mapping) else {}
    props = _component_props(str(row.get("source") or ""))
    roles: dict[str, str] = {}
    components: list[str] = []
    for c in (row.get("components") or []):
        if isinstance(c, Mapping):
            name, role = str(c.get("name") or "").strip(), str(c.get("role") or "").strip()
        else:
            name, role = str(c or "").strip(), ""
        if not name:
            continue
        components.append(name)
        if role:
            roles[name] = role

    layers = [l for l in (defaults.get("layers") or []) if isinstance(l, Mapping)]
    by_comp: dict[str, list[Mapping[str, Any]]] = {}
    for layer in layers:
        comp = str(layer.get("component") or "").strip()
        if comp:
            by_comp.setdefault(comp, []).append(layer)

    def layer_label(comp: str) -> str:
        """一个组件管好几层时，把层名聚合起来（「渐变天空、远山、地面」）。

        沉淀时 `build_slots` 已按组件聚合；老模板存的是第一层的名字
        （「渐变天空」），这里用同一份 `layers` 现场补正，用户才知道
        「换背景」换的是整块背景，不是只换天空。
        """
        names: list[str] = []
        for layer in by_comp.get(comp) or []:
            name = str(layer.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
        return "、".join(names[:4])

    def layer_default(comp: str) -> str:
        bits: list[str] = []
        for layer in by_comp.get(comp) or []:
            name = str(layer.get("name") or "").strip()
            default = str(layer.get("default") or "").strip()
            if name or default:
                bits.append(f"{name}：{default}" if name and default
                            else (name or default))
        return "；".join(bits)[:240]

    def build(comp: str, *, label: str = "", kind: str = "",
              default: str = "", varies: Any = ()) -> dict[str, Any]:
        contract_text = props.get(comp, "")
        return {
            "component": comp,
            "label": label or roles.get(comp) or comp,
            "kind": kind or "other",
            "default": default,
            "varies": [str(v) for v in (varies or []) if str(v).strip()],
            "contract": prop_names(contract_text),
            "contract_text": contract_text,
            "variants": [],
        }

    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    stored = defaults.get("slots")
    if isinstance(stored, list) and stored:
        for item in stored:
            if not isinstance(item, Mapping):
                continue
            comp = str(item.get("component") or "").strip()
            if not comp or comp in seen:
                continue
            seen.add(comp)
            slots.append(build(
                comp,
                label=layer_label(comp) or str(item.get("label") or ""),
                kind=str(item.get("kind") or "") or _facet_kind(manifest, comp),
                default=layer_default(comp) or str(item.get("default") or ""),
                varies=item.get("varies") or (),
            ))
    else:
        # 老模板：组件清单 + facets 推槽位；layers 有匹配就用 layers 的标签/几何。
        for comp in [*by_comp, *components]:
            if not comp or comp in seen:
                continue
            seen.add(comp)
            first = (by_comp.get(comp) or [{}])[0]
            slots.append(build(
                comp,
                label=layer_label(comp) or str(first.get("name") or ""),
                kind=str(first.get("kind") or "") or _facet_kind(manifest, comp),
                default=layer_default(comp) or str(first.get("default") or ""),
                varies=first.get("varies") or (),
            ))

    # 文字槽位的内容契约。`facets.text.items` 是沉淀时写下的：哪个组件是**主字幕**
    # （`kind: caption`）、哪个是日期铭文，以及内容由哪个 prop 传入（`content_prop`）。
    # 只读沉淀数据——不同模板的文字组件名各不相同，这里不写任何组件名。
    #
    # 为什么必须带出来：模板组件的 prop 都是可选的，不传就沿用**参考片那一镜**的
    # 缺省值。真机 2026-10-19 的菊花镜正是这样：写手渲染了 `<StyleCaption>` 却没传
    # `text`，预览里显示的是参考片的台词「到山西，下雪了。新靴，还是没舍得穿。」——
    # 内容被模板缺省值顶掉了。下游（提示词、字幕归属判定）都要知道这块字是内容。
    text_by_comp: dict[str, Mapping[str, Any]] = {}
    for holder in (defaults.get("facets"), manifest.get("facets")):
        if not isinstance(holder, Mapping):
            continue
        text_facet = holder.get("text")
        if not isinstance(text_facet, Mapping):
            continue
        for item in text_facet.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            comp = str(item.get("component") or "").strip()
            if comp and comp not in text_by_comp:
                text_by_comp[comp] = item
    for slot in slots:
        item = text_by_comp.get(str(slot.get("component") or ""))
        if not item:
            continue
        text_kind = str(item.get("kind") or "").strip()
        if text_kind:
            slot["text_kind"] = text_kind
        content_prop = str(item.get("content_prop") or "").strip()
        if content_prop:
            slot["content_prop"] = content_prop
        position = str(item.get("position") or "").strip()
        if position:
            slot["position"] = position

    # 参考片里出现过的内容取值（场景、服饰、光色……）：告诉用户「这个零件现在
    # 能画哪几档」，也告诉拆解器「山/平地」该落到哪个 prop。数据只从已沉淀的
    # variations 读，不对某套风格写死。
    values_by_prop: dict[str, list[str]] = {}

    def _collect(items: Any) -> None:
        for v in (items or []):
            if not isinstance(v, Mapping):
                continue
            prop = str(v.get("prop") or "").strip()
            if not prop:
                continue
            bucket = values_by_prop.setdefault(prop, [])
            for x in (v.get("values") or []):
                x = str(x).strip()
                if x and x not in bucket:
                    bucket.append(x)

    _collect((manifest.get("motion_contract") or {}).get("variations"))
    _collect((defaults.get("motion_contract") or {}).get("variations"))
    for holder in (manifest.get("facets"), defaults.get("facets")):
        if not isinstance(holder, Mapping):
            continue
        for section in ("visual", "motion"):
            part = holder.get(section)
            if isinstance(part, Mapping):
                _collect(part.get("variations"))
    for slot in slots:
        enums = _enum_values(str(slot.get("contract_text") or ""))
        axes = list(slot.get("varies") or [])
        order = axes + [p for p in (slot.get("contract") or []) if p not in axes]
        entries: list[dict[str, Any]] = []
        for p in order:
            declared = list(values_by_prop.get(p) or [])
            enum = list(enums.get(p) or [])
            if enum:
                entries.append({
                    "prop": p,
                    "values": enum[:8],
                    # 参考片里的中文标签只作语义提示（「军装」≈ cap），
                    # 不冒充可传的字面量。
                    "reference": [x for x in declared if x not in enum][:6],
                })
            elif declared:
                # 自由类型（颜色/数字/文字）：没有可传的枚举，记下参考片
                # 用过的语义，让写手按它自己选合法值。
                entries.append({"prop": p, "values": [], "reference": declared[:6]})
        slot["content"] = entries[:8]

    variants = manifest.get("variants")
    if isinstance(variants, list):
        for v in variants:
            if not isinstance(v, Mapping):
                continue
            comp = str(v.get("component") or "").strip()
            target = next((s for s in slots if s["component"] == comp), None)
            if target is None:
                continue
            record = {
                "name": str(v.get("name") or ""),
                "module": str(v.get("module") or ""),
                "component": comp,
                "label": str(v.get("label") or ""),
                "contract": [str(p) for p in (v.get("contract") or []) if str(p).strip()],
                # 替换件自己的 props 文本：它可以给联合类型加取值（更多时代/场景）。
                # 写手要靠这份文本知道「这一镜能传哪些新值」，否则只会沿用基模板那一档。
                "contract_text": str(v.get("contract_text") or ""),
                "hash": str(v.get("hash") or ""),
            }
            target["variants"].append(record)
            # 刚换上的那一件自动生效：换件按钮的意图就是「用上它」，不要求用户
            # 再记住替换件名、手写 `style_overrides`。同一槽位只认最新的默认件；
            # 显式写 `style_overrides`（或写 `default`）仍可覆盖/回退。
            if v.get("default") is True and "bound" not in target:
                target["bound"] = dict(record)
    # 绑定件才是这一片真正画的组件：它给联合类型加的取值，要算进「现在能画」。
    # 不合并的话，自动落地检查第二遍仍会看到旧的取值表，同一件东西会被反复补件。
    for slot in slots:
        bound = slot.get("bound")
        if not isinstance(bound, Mapping):
            continue
        live = _enum_values(str(bound.get("contract_text") or ""))
        if not live:
            continue
        entries = slot.get("content") if isinstance(slot.get("content"), list) else []
        by_prop = {str(c.get("prop") or ""): c for c in entries
                   if isinstance(c, dict)}
        for prop, values in live.items():
            entry = by_prop.get(prop)
            if entry is None:
                entry = {"prop": prop, "values": [], "reference": []}
                entries.append(entry)
                by_prop[prop] = entry
            have = [str(x) for x in (entry.get("values") or [])]
            for v in values:
                if v not in have:
                    have.append(v)
            entry["values"] = have[:12]
        slot["content"] = entries[:10]
        slot["contract_live"] = str(bound.get("contract_text") or "")
    return slots


def as_pack_template(row: Mapping[str, Any]) -> dict[str, Any]:
    """模板行 → 注入到 `style_pack["template"]` 的形状（写手只认这一份）。"""
    # 组件清单会原样进写手提示词：坏形状（dict 里没 name、数字、None）必须在这里
    # 丢掉，不能 `str()` 一下变成 "None" 混进可用清单——模型会照着 import 一个不存在
    # 的导出，整镜打包失败。
    components = [c.get("name") if isinstance(c, Mapping) else c
                  for c in (row.get("components") or [])]
    manifest = row.get("manifest") if isinstance(row.get("manifest"), Mapping) else {}
    return {
        "name": str(row.get("name") or ""),
        "display": str(row.get("display") or row.get("name") or ""),
        "module": str(row.get("module") or ""),
        "components": [str(c) for c in components if isinstance(c, str) and c.strip()],
        # props 签名：模型不用猜参数，import 才是可执行的指令而不是一句愿望。
        "usage": component_usage(row),
        "responsible": str(manifest.get("responsible") or "").strip(),
        "hash": source_hash(row),
        # 槽位：每个组件都能换一份同合约的实现；不传就用默认组件。
        "slots": template_slots(row),
    }


# ---------------------------------------------------------------- remotion 侧


def _base(base: str | None = None) -> str:
    return (base or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")


def _http(method: str, path: str, payload: Mapping[str, Any] | None = None,
          *, base: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{_base(base)}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "creation-template-store/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 渲染容器把「为什么 400」写在响应体里（lint 明细、哪个参数不合法）。
        # 只抛 `HTTP Error 400` 会把一个可修的原因变成「环境坏了」。
        try:
            body = exc.read().decode("utf-8", "replace")[:1200]
        except Exception:  # noqa: BLE001 - 读不到响应体就退回状态行
            body = ""
        raise RuntimeError(
            f"{method} {path} → HTTP {exc.code}：{body or exc.reason}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"{method} {path} 返回的不是 JSON：{exc}") from exc


def remote_templates(*, base: str | None = None) -> dict[str, str]:
    """渲染容器里已有哪些生成模板 → {name: hash}。不可达时抛，由调用方降级。"""
    got = _http("GET", "/templates", base=base)
    out = got.get("templates")
    return {str(k): str(v) for k, v in (out or {}).items()} if isinstance(out, Mapping) else {}


def push_template(row: Mapping[str, Any], *, base: str | None = None) -> str:
    """把一条**用户模板**推给渲染容器；返回落地的 hash。系统模板（无源码）直接跳过。"""
    name = str(row.get("name") or "").strip()
    source = str(row.get("source") or "")
    if not name:
        raise ValueError("模板缺 name")
    if not source:
        return source_hash(row)
    got = _http("POST", "/templates", {"name": name, "source": source}, base=base)
    if not got.get("ok"):
        raise RuntimeError(str(got.get("error") or f"推送模板 {name} 失败"))
    return str(got.get("hash") or source_hash(row))


def drop_remote(name: str, *, base: str | None = None) -> None:
    try:
        _http("DELETE", f"/templates/{name}", base=base)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, OSError):
        pass


async def sync_validated(storage: Any, *, base: str | None = None) -> dict[str, Any]:
    """把库里所有 validated 且有源码的模板推到渲染容器。

    返回 ``{"pushed": n, "kept": n, "failed": [names], "skipped": reason}``——启动期
    只记一行日志，不抛：渲染容器可能还在起，晚一点 `ensure_remote()` 会补推。
    """
    try:
        remote = remote_templates(base=base)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, OSError) as exc:
        return {"pushed": 0, "kept": 0, "failed": [], "skipped": str(exc)[:200]}
    pushed, kept, failed = 0, 0, []
    for row in await storage.style_templates.list(status="validated"):
        name = str(row.get("name") or "")
        source = str(row.get("source") or "")
        if not source:
            continue
        want = source_hash(row)
        if remote.get(name) == want:
            kept += 1
            continue
        try:
            push_template(row, base=base)
            pushed += 1
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, OSError):
            failed.append(name)
    return {"pushed": pushed, "kept": kept, "failed": failed, "skipped": ""}


_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_CACHE_TTL = 30.0


async def ensure_remote(storage: Any, name: str, *, base: str | None = None) -> tuple[bool, str]:
    """确保某一个模板在渲染容器里是最新的。返回 (可用, 说明)。

    带 30 秒进程内缓存：一镜写一次代码，不能每次都打一轮网络；但也不能永久缓存
    ——渲染容器重启后目录会空，缓存太久的代价是「模板明明在库里，镜头却 import 失败」。
    """
    row = await storage.style_templates.get(name)
    if row is None:
        return False, "模板不在库里"
    if str(row.get("status") or "") != "validated":
        return False, f"模板状态是 {row.get('status')}，还没过闸"
    source = str(row.get("source") or "")
    if not source:
        # 系统内置：镜像里已经烤好，不归这里管。
        return True, ""
    now = time.monotonic()
    cached = _CACHE.get(name)
    if cached is None or now - cached[0] > _CACHE_TTL:
        try:
            _CACHE[name] = (now, remote_templates(base=base))
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, OSError) as exc:
            return False, f"渲染容器不可达：{str(exc)[:160]}"
    want = source_hash(row)
    if _CACHE[name][1].get(name) == want:
        return True, ""
    try:
        push_template(row, base=base)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, OSError) as exc:
        return False, f"模板推送失败：{str(exc)[:160]}"
    _CACHE[name] = (now, {**_CACHE.get(name, (now, {}))[1], name: want})
    return True, ""
