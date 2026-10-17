"""会话级的「文字库」偏好 → 出片通道的参数。

用户在左侧「文字库」里挑过的那几维（字幕形态/位置/入场动画、音色、语速）钉进**这一条
对话**的出片参数；**没挑过的维度照旧归模型**。开一个新对话就回到「交给模型自己定」。

## 为什么要有这一步，明明提示词里也能说

提示词里说一句「用户选了撕纸高亮」，模型**大概**会照做——但那是一句话的印象，不是一条
判据；而它做不做，用户看不出来。这里钉的是确定的事：这一维的值就是用户挑的那一个。
钉完还有第二个好处：确认卡读的是同一份参数（``build_render_ask`` 从 ``resp.tool_calls``
取），所以**卡上写的就是真会渲的**。

## 为什么钉在响应归一这一步，而不是 ``before_tool_call`` 钩子

钩子跑在**执行**阶段（``agent._execute_tool_calls`` 里），而渲染确认门在它**之前**读
``resp.tool_calls`` 的参数（``render_gate.build_render_ask``）。用钩子会出现「卡上写着
模型挑的 A，实际渲的是用户钉的 B」——而这一处的全部意义就是让两者一致。所以钉在
响应回来之后、门之前（``agent._tool_round`` 的开头），卡、执行、回执三处看同一份值。

## 只钉**整片默认档**，不碰逐镜覆盖

这两件事在 spec 里是两层：顶层开关是整片默认，``spec.shots[i].caption_style`` 是逐镜
覆盖（层E2 开出来的能力）。这里只写顶层——用户挑的是「这条片子用哪种字幕」，某一镜要
单独换个样式仍然是分镜自己的事。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

#: 可以被钉住的开关名。**与剪辑服务端 ``code_spec.LIBRARY_DIMS`` 同一批**：
#: 那一份是「文字库面板管得着哪几维」的权威定义，这一份是它在主服务里的使用点。
#: 名单短、且是「声音与字」那几维——画幅/帧率/质感那些仍归模型与计划卡。
PINNABLE_KEYS: tuple[str, ...] = ("caption_style", "highlight_style", "highlight_color",
                                  "voice", "rate")

#: 自由色那一维的形状（``#abc`` / ``#aabbcc``）。与剪辑服务端 ``code_spec._HEX_RE``
#: 同一口径，但**在这里自带一份**：主服务不装配剪辑服务端的实现（同 ``render_gate``
#: 那几张标签表），而这里认错的后果是收下一条渲不出来的颜色。漂了由
#: ``tests/test_motion_prefs.py`` 拦下。
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _expand_color(color: str) -> str:
    """``#ABC`` → ``#aabbcc``。归一必须做：同一个颜色写成两种大小写会一路走到镜头
    代码里当两件事（缓存键、偏好比对都按字节），而那是一次全片重烧。"""
    body = color[1:]
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    return "#" + body.lower()

#: 会被钉的节点。``plan_motion`` 也要钉：分镜卡上写的就是这一份，不钉的话
#: 「计划卡写着纸条底，执行时变成用户挑的贴画面」——同一件事两条口径。
#: （``none`` 那一档尤其明显：整片不上字幕，模型在写分镜那一刻就得知道。）
MOTION_KNOB_NODES = frozenset({"plan_motion", "render_motion_code"})

#: 上面那几维里**不是节点顶层入参**的那几个：值写进 ``spec`` 而不是顶层。
#:
#: 为什么它们不同：顶层入参是要摊在计划卡上的（`plan/vocab.py` 从
#: ``input_schema.properties`` 反查枚举源），而自由色没有枚举可列——它一旦出现在卡面
#: 参数里，**整份计划会被打回**（「没有可反查的枚举源」）。所以这一维绕开卡面，
#: 直接落到 spec 上：`validate_code_spec` 归一它、`shot_input_props` 注入它、
#: `shot_cache_key` 也读那一份，三处都在 spec 上。
#: 与剪辑服务端 `code_spec.COLOR_KNOBS` 是同一批（那边多说一句「面板渲取色控件」）。
SPEC_LEVEL_KEYS: tuple[str, ...] = ("highlight_color",)


def pinned_knobs(prefs: Any) -> dict[str, Any]:
    """会话偏好 → 「可以钉的那几维」。→ ``{开关名: 值}``，一维都没挑过就是空表。

    只收 :data:`PINNABLE_KEYS` 里的键、且值必须是非空字符串：存进库里的东西本应由
    ``PUT /convs/{id}/prefs`` 校验过，但**别把下游的正确性押在上游校验上**——
    库里有一条历史脏数据（比如某一版前端写进去过别的键），这里静默忽略它，
    而不是把一个不认识的开关塞进节点参数、让渲染在几分钟后失败。
    值为空串按「取消这一维」处理，与 ``ConversationsRepo.set_prefs`` 的 ``None`` 同义。
    """
    if not isinstance(prefs, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in PINNABLE_KEYS:
        value = prefs.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def pin_tool_calls(tool_calls: Sequence[Any], prefs: Any) -> list[str]:
    """把用户挑过的开关盖进这条 run 的出片工具调用上。→ 真钉下去的键（给记账用）。

    **覆盖，不是补缺**：用户挑过的维度一律以用户的为准，哪怕模型也给了值。
    「模型这次挑得挺好」不构成不覆盖的理由——用户明确表过态，让他再赌一次模型听不听
    是没有意义的。

    只对 :data:`MOTION_KNOB_NODES` 里的节点生效。参数不是映射（模型把 args 写成了
    字符串等坏形状）就跳过这一条：那种情况下游本来就会报错，在这儿硬塞一个字典进去
    只会把「参数根本不是对象」变成一句更难认的异常。
    """
    knob = pinned_knobs(prefs)
    if not knob:
        return []
    pinned: list[str] = []
    for tc in tool_calls:
        if getattr(tc, "name", None) not in MOTION_KNOB_NODES:
            continue
        args = getattr(tc, "arguments", None)
        if not isinstance(args, dict):
            continue
        pin_into_args(args, knob)
        pinned.extend(knob)
    return pinned


def pin_into_args(args: dict[str, Any], knob: Mapping[str, Any]) -> None:
    """就地盖值。拆出来是为了让用例能直接打这一个函数，不必造一整个 tool call。

    :data:`SPEC_LEVEL_KEYS` 里的那几维写进 ``args["spec"]`` 而不是顶层：顶层入参的位置
    是计划卡，而它们上不了卡（见那条注释）。``spec`` 不是映射（模型把它写成了字符串等
    坏形状）时跳过这一条——那种情况下游本来就会报错，在这儿硬塞一个字典进去只会把
    「参数根本不是对象」变成一句更难认的异常。
    """
    for key, value in knob.items():
        if key in SPEC_LEVEL_KEYS:
            spec = args.get("spec")
            if isinstance(spec, dict):
                spec[key] = value
            continue
        args[key] = value


def summarise(prefs: Any, *, labels: Mapping[str, Mapping[str, str]] | None = None
              ) -> list[str]:
    """钉住的那几维翻成人话，给面板顶栏与日志用。→ 如 ``["字幕 撕纸高亮·逐词点亮"]``。

    标签表由调用方从契约取（``agent._motion_value_labels``）——主服务不抄一份取值对照表，
    取不到就退回原始取值（宁可让日志里露一次机器名，也不要在这里长出第二份词表）。
    """
    labels = labels or {}
    out: list[str] = []
    for key, value in pinned_knobs(prefs).items():
        got = (labels.get(key) or {}).get(value) or value
        out.append(f"{key} {got}")
    return out


def pref_keys_from_contract(library: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """从契约里的 ``text_library`` 读出「哪几维能挑、各自按什么规则校验」。

    → ``(维度名, {维度: {"kind": …, "enum": […]}})``。契约没取到（剪辑服务端还没连上）
    时回空，写入口据此**拒绝**这次写入而不是收下一个没法校验的值——收了它，出片时才会
    在下游报错，而那时用户已经以为设置成功了。

    规则从契约的 ``dims`` 现取、**不在这里按维度名硬编码**：自由色那一维（``kind`` 是
    ``"color"``）该收 ``#rrggbb`` 而不是枚举，这件事的出处是剪辑服务端那份契约，
    这里只是它的读者。
    """
    dims = (library or {}).get("dims") if isinstance(library, Mapping) else None
    if not isinstance(dims, Mapping):
        return [], {}
    names: list[str] = []
    rules: dict[str, dict[str, Any]] = {}
    for k in PINNABLE_KEYS:
        dim = dims.get(k)
        if not isinstance(dim, Mapping):
            continue
        names.append(k)
        rules[k] = {"kind": str(dim.get("kind") or "enum"),
                    "enum": [str(v) for v in (dim.get("enum") or [])]}
    return names, rules


def check_patch(patch: Any, *, names: Iterable[str],
                rules: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """校验一次写入。→ ``(收下的键值, 拒绝的理由)``。

    四条判据（都是「收下了但根本不起作用」那类错，所以一律拒绝而不是忽略）：
      · 键必须在可挑的那几维里（写进来一个 ``aspect``，面板上没有它，用户永远改不掉）；
      · 枚举档的值必须在那一维的枚举里（写 ``torn`` 这个母版不认的形态，出片时才打回）；
      · 自由色那一维（``kind == "color"``）的值是一条颜色，归一成小写六位——
        它最终落在 CSS 的 ``color`` 上，收下一条认不出的值不会报错，只会让字幕
        静默变回缺省色，而那正是一处「设置看着像生效了」的假象；
      · 值可以是 ``None`` = **取消这一维**（回到「交给模型定」），这也是「恢复默认」的写法；
        自由色那一维的空串与它同义（面板上「跟随色板」送的就是空）。
    """
    got: dict[str, Any] = {}
    errs: list[str] = []
    known = set(names)
    if not isinstance(patch, Mapping):
        return {}, ["prefs 必须是对象"]
    for key, value in patch.items():
        key = str(key)
        if key not in known:
            errs.append(f"{key} 不是这里能挑的（可挑：{'、'.join(sorted(known)) or '无'}）")
            continue
        if value is None:
            got[key] = None
            continue
        rule = rules.get(key) or {}
        if str(rule.get("kind") or "enum") == "color":
            text = value.strip() if isinstance(value, str) else ""
            if isinstance(value, str) and not text:
                # 空串与 None 同义：都是「不钉这一维，跟色板走」。
                got[key] = None
            elif _COLOR_RE.match(text):
                got[key] = _expand_color(text)
            else:
                errs.append(f"{key}={value!r} 不是一条颜色（#rrggbb）；要跟色板走就送 null")
            continue
        if not isinstance(value, str) or value not in (rule.get("enum") or ()):
            errs.append(f"{key}={value!r} 不在可选值里：{'、'.join(rule.get('enum') or ())}")
            continue
        got[key] = value
    return got, errs
