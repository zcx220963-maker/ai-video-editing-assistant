"""自然语言 → 风格包（manifest + 正文）。**只产数据，不产代码。**

为什么要有这一层：风格包本来就能由用户手写一份（技能管理、风格工作室），但要求人
先会写 manifest JSON——那是「配置」，不是「说需求」。用户原话是「为什么我的 agent
不能写出按用户需求的风格」。这里就是那一步：一句话进来，一份能直接选用的风格包
出去（显示名、正文、manifest），落库仍走普通的技能行。

三条边界：

* **不写模板代码。** 模板是另一条链（参考片沉淀 / 换件）负责的可执行层，要过编译
  与探针；这里只决定「挂哪份现成模板，或干脆不挂」。没有合适模板时 `draw_mode` 写
  `self`，写手按 manifest 与正文自己画——这正是「换个新风格不必先有一份模板」。
* **字幕是硬约束，不是风格选项。** 白字黑边、整条同色、中英双行同号，是用户反复
  确认过的口径；所以生成器不提供别的字幕形态，`caption_style` / `highlight_style`
  在归一化时被钉死。
* **失败只报一句人话。** 模型回的不是 JSON、或没有正文，抛 :class:`DraftError`，
  端点转成 4xx；不落半份风格包。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping, Sequence

#: 正文上限：与风格包注入侧同一个量级（那边 12000）。超了截断，不拒绝。
MAX_BODY_CHARS = 12000
#: 描述上限：再长就不是「一句话说需求」了，而且会把上下文挤掉。
MAX_DESCRIPTION_CHARS = 2000

_HEX = re.compile(r"^#[0-9a-f]{6}$")
_CAPTION_SIZE = (0.2, 1.5)
_CAPTION_BOTTOM = (0.005, 0.3)
_CAPTION_CAP = (0.02, 0.08)
_ORDERS = ("en-zh", "zh-en")
_TEXTURES = ("none", "film", "tv", "glow", "bleach")
#: 生成器**不开放**的字幕形态：用户要的是白字黑边、整条同色。
_CAPTION_STYLE = "outlined"
_HIGHLIGHT_STYLE = "plain"


class DraftError(ValueError):
    """这一份生成不出可用的风格包（模型回话不合法 / 缺正文）。"""


SYSTEM = """你在为一支「纯代码绘制的横屏科普片」写一份**风格包**。
风格包是一套整片统一的视觉契约（数据），不是分镜，也不是代码。逐镜的写手会照它
画每一镜，整片因此看起来是同一套语言。

只输出一个 JSON 对象，不要解释，不要 Markdown 围栏：
{
  "display": "6-12 个汉字的名字",
  "description": "一两句话说明这套视觉适合什么题材（80 字以内）",
  "body": "600-1200 字正文",
  "manifest": { ... }
}

manifest 里可以用这些键（用得上的才写，没把握的不写）：
- "template": 从下面给出的模板清单里选一个最接近的；都不接近就写 ""。
- "use_template"/"draw_mode": 选了模板写 true/"template"；没选写 false/"self"。
- "bleed": true 或 false（画面是否铺满整帧）。
- "palette": 颜色对象，值一律小写 #rrggbb。常用键：page（整片底色）、surface、
  field、text、muted、accent、border、light、rim。按这套视觉需要增删。
- "texture": 整片质感档，只能是 none / film / tv / glow / bleach。
- "caption": 只能含 look、position、width、size、order、bottom、cap；
  size 是字号系数（0.2–1.5），order 只能是 "en-zh"，bottom 是满幅时字块离画面底
  的比例（0.005–0.3），cap 是字号硬上限（0.02–0.08，占画面高）。
- "composition": 自由键值，写这一套的构图与相机（如主体占画面比例、地平线、运镜）。
- "recurring_pose"/"recurring_prop": 一句话，整片反复出现的姿势或道具（可选）。
- "motion": 自由键值，写运镜、颗粒、暗角、雾等整片运动纪律。
- "facets": {"visual": {"invariants": [{"name": "...", "value": "..."}]}}，
  3–6 条「跨镜不能变」的视觉不变量（底色、光位、材质、构图、笔法…）。
- "avoid": 8–12 条画面上不要出现的东西。
- "writer_note": 一句话补充，写给逐镜写手（可选）。

硬约束：
1. 所有画面都必须能用代码画出来：几何、材质、光、文字、粒子。不得依赖照片、
   贴图、AI 生图或任何外部素材。
2. 字幕永远白字黑边、整条同色、中英双行同号。你只决定字幕的位置与大小，
   不要写「关键词换色」「逐词点亮」，也不要提议字幕条或第二条字幕。
3. body 写这套视觉的语法（底色、光位、材质、构图、运镜、字、避免项），
   不要写分镜内容，不要写流程纪律（例如「不照做就打回」这类话一句都不要）。
4. 用中文写名字与正文；不要出现「我」「我们」。
5. 数值必须落在上面给的区间内；颜色一律小写 #rrggbb；不要输出区间以外的键值。
"""


REVISE_SYSTEM = """你在修改一份**已经存在**的风格包。用户会用日常语言说想改哪里，
你要把这句话落成对 manifest 与正文的修改。

只输出一个 JSON 对象，不要解释，不要 Markdown 围栏：
{
  "manifest": { ...改完的**完整** manifest... },
  "body": "改完的完整正文",
  "note": "一句话说改了什么（给人看）",
  "needs_template": false
}

规则：
1. 只改用户提到的那一点，其余逐字保留——完整返回，不要删掉没提到的键。
2. 字幕形态不能改（白字黑边、整条同色、中英双行同号）；位置、大小、次序可以改。
3. 颜色一律小写 #rrggbb；caption.size 0.2–1.5，caption.bottom 0.005–0.3，
   caption.cap 0.02–0.08；texture 只能是 none/film/tv/glow/bleach。
4. template 只能在给定的模板清单里选，或留空。
5. 如果用户要改的是**画面组件本身**（换掉某个部件、要一套现在没有的组件），
   不要自己写代码：manifest 与 body 原样返回，needs_template 写 true，
   在 note 里说「这一步属于模板那一层」。
6. 正文只在用户的要求涉及视觉语法时才改；否则原样返回。
7. 不要写流程纪律（例如「不照做就打回」），不要出现「我」「我们」。
"""


def revise_messages(instruction: str, manifest: Mapping[str, Any], body: str,
                    templates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    usable = [t for t in templates if str(t.get("name") or "").strip()][:20]
    tpl_lines = ["可挂的模板：无。template 写空串。"]
    if usable:
        tpl_lines = ["可挂的模板："]
        for t in usable:
            tpl_lines.append(f"- {t.get('name')}｜{t.get('display') or t.get('name')}")
    user = "\n".join([
        f"用户这次要改的：{instruction.strip()}",
        "",
        "现在的 manifest：",
        json.dumps(manifest, ensure_ascii=False, indent=1)[:8000],
        "",
        "现在的正文：",
        str(body or "")[:MAX_BODY_CHARS],
        "",
        *tpl_lines,
    ])
    return [{"role": "system", "content": REVISE_SYSTEM},
            {"role": "user", "content": user}]


def parse_revision(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise DraftError("这次没改出来，换句话再说一次试试")
    try:
        got = json.loads(text[start:end + 1])
    except (TypeError, ValueError) as exc:
        raise DraftError("模型回的不是合法 JSON，换句话再说一次试试") from exc
    if not isinstance(got, Mapping) or not isinstance(got.get("manifest"), Mapping):
        raise DraftError("模型没有给出改完的风格包，换句话再说一次试试")
    return {
        "manifest": dict(got["manifest"]),
        "body": str(got.get("body") or ""),
        "note": _str(got.get("note"), 200),
        "needs_template": bool(got.get("needs_template")),
    }


async def revise(llm: Any, *, instruction: str, manifest: Mapping[str, Any],
                 body: str, templates: Sequence[Mapping[str, Any]],
                 identity_user: str = "",
                 conversation_id: str = "") -> dict[str, Any]:
    """一句话改风格包：返回改完的 manifest/正文与一句说明，**不落库**。

    改的是数据；要换画面组件时只回一个 ``needs_template``，让界面把人引到模板那
    一层，而不是让模型现场写 TSX。
    """
    text = str(instruction or "").strip()
    if not text:
        raise DraftError("先说一句想改哪里")
    try:
        from .identity import use_identity
        messages = revise_messages(text, manifest, body, templates)
        if identity_user:
            with use_identity(identity_user, conversation_id):
                resp = await llm.complete(messages, max_tokens=2400, timeout=120.0)
        else:
            resp = await llm.complete(messages, max_tokens=2400, timeout=120.0)
        raw = str(getattr(resp, "content") or "")
    except DraftError:
        raise
    except Exception as exc:  # noqa: BLE001 - 与生成同口径：只说一句人话
        raise DraftError(f"这次没能改出来：{exc}") from exc
    got = parse_revision(raw)
    names = [str(t.get("name") or "") for t in templates]
    got["manifest"] = normalize_manifest(got["manifest"], names)
    got["body"] = (got["body"] or str(body or ""))[:MAX_BODY_CHARS]
    return got


def build_messages(description: str,
                   templates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """一次生成请求。模板清单只给名字与一句话用途，不给源码。"""
    lines = [f"用户想要的视觉：{description.strip()}", ""]
    usable = [t for t in templates if str(t.get("name") or "").strip()][:20]
    if usable:
        lines.append("可挂的模板（选了就把名字写进 manifest.template）：")
        for t in usable:
            desc = str(t.get("description") or "").strip().replace("\n", " ")[:160]
            lines.append(f"- {t.get('name')}｜{t.get('display') or t.get('name')}"
                         + (f"：{desc}" if desc else ""))
    else:
        lines.append("可挂的模板：无。manifest.template 写空串，draw_mode 写 self。")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(lines)}]


def parse_draft(raw: str) -> dict[str, Any]:
    """模型回话 → 草稿。围栏、前后闲话都容忍；解析不出就抛 DraftError。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise DraftError("模型没有给出可用的风格包，再说一次想要的感觉试试")
    try:
        got = json.loads(text[start:end + 1])
    except (TypeError, ValueError) as exc:
        raise DraftError("模型给的风格包不是合法 JSON，再说一次想要的感觉试试") from exc
    if not isinstance(got, Mapping):
        raise DraftError("模型给的风格包不是一份对象，再说一次想要的感觉试试")
    body = str(got.get("body") or "").strip()
    manifest = got.get("manifest")
    if not body:
        raise DraftError("模型给了一份没有正文的风格包，再说一次想要的感觉试试")
    if not isinstance(manifest, Mapping):
        raise DraftError("模型给的风格包缺少 manifest，再说一次想要的感觉试试")
    return {
        "display": str(got.get("display") or "").strip(),
        "description": str(got.get("description") or "").strip(),
        "body": body[:MAX_BODY_CHARS],
        "manifest": dict(manifest),
    }


def _number(value: Any, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and lo <= num <= hi else None


def _str(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _string_list(value: Any, limit: int, each: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _str(item, each)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _mapping_of_scalars(value: Any, *, limit: int = 16) -> dict[str, Any]:
    """自由键值的对象：只留数字 / 字符串 / 两数数组，键名原样（它是数据，不是代码）。"""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = _str(key, 40)
        if not name:
            continue
        if isinstance(item, bool):
            out[name] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            if math.isfinite(float(item)):
                out[name] = item
        elif isinstance(item, str):
            text = item.strip()[:200]
            if text:
                out[name] = text
        elif (isinstance(item, (list, tuple)) and len(item) == 2
              and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                      for x in item)):
            out[name] = list(item)
        if len(out) >= limit:
            break
    return out


def normalize_manifest(raw: Mapping[str, Any],
                       template_names: Sequence[str]) -> dict[str, Any]:
    """把模型写的那份 manifest 收成可落库的形状。

    只做「区间与枚举」这一类结构校验；键名本身**不按某套风格白名单过滤**——风格
    参数由模型现场起名（见 `_free_style_lines`），过滤会把新词全丢掉。未知键留，
    但限制条数与长度，避免一段随笔把每镜提示词挤掉。
    """
    src = dict(raw or {})
    names = {str(n) for n in template_names if str(n).strip()}
    out: dict[str, Any] = {"version": 1}

    tpl = _str(src.get("template"), 80)
    if tpl in names:
        out["template"] = tpl
        out["use_template"] = True
        out["draw_mode"] = "template"
    else:
        # 没有合适模板就不挂：写手按正文与不变量自己画。留一个空 template 会让
        # `_attach_style_pack` 去模板库查一个不存在的名字，回执里多一条「暂不可用」。
        out["draw_mode"] = "self"
        out["use_template"] = False

    if isinstance(src.get("bleed"), bool):
        out["bleed"] = src["bleed"]
    if isinstance(src.get("palette"), Mapping):
        pal = {_str(k, 24).lower(): str(v).strip().lower()
               for k, v in src["palette"].items()
               if isinstance(v, str) and _HEX.match(str(v).strip().lower())}
        pal = {k: v for k, v in pal.items() if k}
        if pal:
            out["palette"] = pal
    texture = _str(src.get("texture"), 16).lower()
    if texture in _TEXTURES:
        out["texture"] = texture
    # 字幕：形态是硬约束，只收位置/大小/次序这些数值。
    out["caption_style"] = _CAPTION_STYLE
    out["highlight_style"] = _HIGHLIGHT_STYLE
    cap_src = src.get("caption") if isinstance(src.get("caption"), Mapping) else {}
    caption: dict[str, Any] = {}
    look = _str(cap_src.get("look"), 20)
    if look:
        caption["look"] = look
    position = _str(cap_src.get("position"), 20)
    if position:
        caption["position"] = position
    width = _number(cap_src.get("width"), 0.2, 0.95)
    if width is not None:
        caption["width"] = round(width, 3)
    size = _number(cap_src.get("size"), *_CAPTION_SIZE)
    if size is not None:
        caption["size"] = round(size, 3)
    order = _str(cap_src.get("order"), 8).lower()
    caption["order"] = order if order in _ORDERS else "en-zh"
    bottom = _number(cap_src.get("bottom"), *_CAPTION_BOTTOM)
    if bottom is not None:
        caption["bottom"] = round(bottom, 4)
    cap = _number(cap_src.get("cap"), *_CAPTION_CAP)
    if cap is not None:
        caption["cap"] = round(cap, 4)
    out["caption"] = caption

    composition = _mapping_of_scalars(src.get("composition"))
    if composition:
        out["composition"] = composition
    motion = _mapping_of_scalars(src.get("motion"))
    if motion:
        out["motion"] = motion

    pose = _str(src.get("recurring_pose"), 200)
    if pose:
        out["recurring_pose"] = pose
    prop = _str(src.get("recurring_prop"), 200)
    if prop:
        out["recurring_prop"] = prop
    note = _str(src.get("writer_note"), 1200)
    if note:
        out["writer_note"] = note

    facets_src = src.get("facets") if isinstance(src.get("facets"), Mapping) else {}
    visual = facets_src.get("visual") if isinstance(facets_src.get("visual"), Mapping) else {}
    invariants: list[dict[str, str]] = []
    for item in (visual.get("invariants") or [])[:8]:
        if not isinstance(item, Mapping):
            continue
        name = _str(item.get("name"), 40)
        value = _str(item.get("value"), 200)
        if name or value:
            invariants.append({"name": name, "value": value})
    if invariants:
        out["facets"] = {"visual": {"invariants": invariants}}

    avoid = _string_list(src.get("avoid"), 16, 80)
    if avoid:
        out["avoid"] = avoid
    narration_avoid = _string_list(src.get("narration_avoid"), 16, 40)
    if narration_avoid:
        out["narration_avoid"] = narration_avoid

    # 未知键原样保留（风格参数是模型现场起名的），但限条数与单条长度。
    known = set(out) | {"version", "template", "use_template", "draw_mode",
                        "palette_name", "shell", "shell_label",
                        "layers", "timeline", "motion_contract", "slots",
                        "blocks", "variants", "facets", "caption_style",
                        "highlight_style"}
    extra = 0
    for key, value in src.items():
        name = _str(key, 40)
        if not name or name in known or name in out:
            continue
        if extra >= 12:
            break
        if isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                continue
            if len(text) <= 600:
                out[name] = value
                extra += 1
        elif isinstance(value, (str, int, float, bool)):
            text = _str(value, 600)
            if text:
                out[name] = value
                extra += 1
    return out


def draft_from_text(raw: str, template_names: Sequence[str]) -> dict[str, Any]:
    """parse + normalize 的合成入口（测试与端点共用同一条路）。"""
    draft = parse_draft(raw)
    draft["manifest"] = normalize_manifest(draft["manifest"], template_names)
    draft["display"] = (draft["display"] or "未命名风格")[:40]
    draft["description"] = (draft["description"] or "")[:120]
    return draft


async def generate(llm: Any, *, description: str,
                   templates: Sequence[Mapping[str, Any]],
                   identity_user: str = "",
                   conversation_id: str = "") -> dict[str, Any]:
    """调一次模型，返回可直接落库的草稿。模型报错也转成 DraftError。"""
    desc = str(description or "").strip()[:MAX_DESCRIPTION_CHARS]
    if not desc:
        raise DraftError("先说一句想要什么视觉")
    try:
        from .identity import use_identity
        messages = build_messages(desc, templates)
        if identity_user:
            with use_identity(identity_user, conversation_id):
                resp = await llm.complete(messages, max_tokens=2400, timeout=120.0)
        else:
            resp = await llm.complete(messages, max_tokens=2400, timeout=120.0)
        raw = str(getattr(resp, "content") or "")
    except DraftError:
        raise
    except Exception as exc:  # noqa: BLE001 - 模型/密钥侧的任何毛病都只说一句人话
        raise DraftError(f"这次没能写出风格包：{exc}") from exc
    names = [str(t.get("name") or "") for t in templates]
    return draft_from_text(raw, names)
