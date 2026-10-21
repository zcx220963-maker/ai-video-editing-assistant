# -*- coding: utf-8 -*-
"""画面风格工具：用户在对话里的一句话（＋可选的参考视频/图片）直接变成风格包。

为什么放在对话里而不是一个面板：用户的原话是「用户不会按你这个风格工作室改，
根本看不懂」。风格包的四个层面（风格 / 版式 / 模板 / 提示词）对使用的人是透明的；
他只需要说「换成水墨风」「底子再冷一点」「照着这段视频做」。这只工具接住那句话，
做好数据层（`skills(kind=style)`），并立刻让这一轮的分镜按它走。

边界：
* 只写数据，不写 TSX；参考素材只用来读视觉，不上屏。
* 系统风格只读：要改就克隆一份用户自己的。
* 任何失败都回一句人能看懂的话（`ToolError(counts_as_failure=False)`），
  不把「模型返回了 502」这种原始异常甩给用户。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Sequence

from ..identity import current_identity, rebind_style_skill
from ..tool import Tool, ToolError
from .. import style_pack_gen as gen
from .. import style_reference as ref

#: 事件类型：前端收到后把「当前风格」换成这一份（后续消息随 /chat 带回服务端）。
STYLE_EVENT = "style"

#: 让「自动 / 默认 / 不用风格」这类话落到「取消」上，而不是去匹配一个风格名。
_AUTO_WORDS = ("自动", "默认", "不用", "取消", "随你", "你定")


def _manifest_of(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """技能行 → manifest。历史行里可能是 dict，也可能是一行 JSON 字符串。"""
    fm = (row or {}).get("frontmatter")
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except ValueError:
            fm = {}
    raw = (fm or {}).get("style_manifest") if isinstance(fm, Mapping) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _display_of(row: Mapping[str, Any] | None) -> str:
    fm = (row or {}).get("frontmatter")
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except ValueError:
            fm = {}
    return str((fm or {}).get("display") or (row or {}).get("name") or "")


def _is_style(row: Mapping[str, Any] | None) -> bool:
    fm = (row or {}).get("frontmatter")
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except ValueError:
            fm = {}
    return str((fm or {}).get("kind") or "") == "style"


def _pick(styles: Sequence[Mapping[str, Any]], words: str) -> Mapping[str, Any] | None:
    """按用户的话找一份风格：先整名/显示名精确，再包含。找不到回 None。"""
    q = words.strip().lower()
    if not q:
        return None
    for row in styles:
        if str(row.get("name") or "").lower() == q:
            return row
        if _display_of(row).strip().lower() == q:
            return row
    hits = [row for row in styles
            if q in _display_of(row).strip().lower()
            or q in str(row.get("name") or "").lower()
            or (_display_of(row).strip().lower()
                and _display_of(row).strip().lower() in q)]
    if not hits:
        return None
    # 多个命中时取名字最短的那个：用户说「水墨」时，命中《水墨长卷》比
    # 命中《水墨长卷·冷灰版》更接近本意。
    return sorted(hits, key=lambda r: len(_display_of(r) or str(r.get("name") or "")))[0]


class StyleLookTool(Tool):
    """对话里的画面风格入口。"""

    def __init__(self, *, storage: Any, llm: Any, mq: Any = None) -> None:
        self._storage = storage
        self._llm = llm
        self._mq = mq

    @property
    def name(self) -> str:
        return "style_look"

    @property
    def display_name(self) -> str:
        return "画面风格"

    @property
    def description(self) -> str:
        return (
            "把用户说的画面风格、或他给的参考视频/图片/一段文字，直接变成这个对话"
            "正在用的风格。用户说「换成水墨风」「底子再冷一点」「字幕往下一点」"
            "「照着这段视频做」时调用；**不要**建议用户去任何面板、也不要让他改参数。\n"
            "action：\n"
            "- make：做一套新风格并立即使用（用户要一套现在没有的视觉时用；可以只给"
            "words，也可以同时给 material_ids 作参考）。\n"
            "- adjust：在当前风格上只改 words 说的那一处。\n"
            "- choose：从已有风格里选一套，words 写风格名；words 写「自动/默认」则取消。\n"
            "- list：列出当前可用的风格。\n"
            "参考素材只用来理解视觉，不会出现在成片里；每一帧仍由代码绘制。"
            "出分镜之前先把风格定好；做完直接接着往下做，不要向用户复述参数。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["make", "adjust", "choose", "list"],
                    "description": "要做的动作；默认 make。",
                },
                "words": {
                    "type": "string",
                    "description": "用户原话（或你提炼的一句）；choose 时写风格名。",
                },
                "material_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "这条消息里的参考视频/图片的 material_id，可空。",
                },
            },
            "required": ["action"],
        }

    @property
    def exclusive(self) -> bool:
        # 它改的是「这一轮用哪套风格」：必须排在分镜之前单独执行，不能和别的工具同批
        # 并发——同批的话分镜可能先跑，拿到的是旧风格。
        return True

    # ---- 内部 ----

    async def _styles(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self._storage.skills.list(user_id)
        return [row for row in rows if _is_style(row)]

    async def _templates(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self._storage.style_templates.list(user_id, status="validated")
        return [{"name": str(r.get("name") or ""),
                 "display": str(r.get("display") or r.get("name") or ""),
                 "description": str(r.get("description") or "")}
                for r in rows if str(r.get("name") or "").strip()]

    async def _fresh_name(self) -> str:
        for _ in range(6):
            name = f"style_{uuid.uuid4().hex[:8]}"
            if await self._storage.skills.get(name) is None:
                return name
        return f"style_{uuid.uuid4().hex[:12]}"

    async def _save(self, *, name: str, display: str, description: str,
                    body: str, manifest: Mapping[str, Any], user_id: str) -> None:
        await self._storage.skills.upsert(
            name, body, description=description,
            frontmatter={"name": name, "display": display, "kind": "style",
                         "description": description,
                         "style_manifest": dict(manifest)},
            owner_user_id=user_id)

    async def _apply(self, name: str, display: str) -> None:
        ident = current_identity()
        if ident is None:
            return
        # 工具在 gather 的子任务里跑，contextvar 的改动出不了这个任务；按会话记一份，
        # MCP 包装层下一轮注入 style_skill 时读它（见 style_override）。
        from ..style_override import set_style_override
        set_style_override(ident.session_id, name)
        rebind_style_skill(name)
        if self._mq is not None:
            from ..connection_manager import OUTBOUND_TOPIC
            await self._mq.publish(OUTBOUND_TOPIC, ident.session_id, {
                "type": STYLE_EVENT,
                "session_id": ident.session_id,
                "style_skill": name,
                "display": display,
            })

    async def _make(self, *, words: str, material_ids: Sequence[str],
                    user_id: str, conversation_id: str) -> str:
        templates = await self._templates(user_id)
        name = await self._fresh_name()
        try:
            got = await ref.make_style(
                self._storage, self._llm, words=words,
                material_ids=material_ids, templates=templates,
                user_id=user_id, conversation_id=conversation_id, name=name)
        except gen.DraftError as exc:
            raise ToolError(self.name, str(exc), counts_as_failure=False) from exc
        except ref.ReferenceError as exc:
            raise ToolError(self.name, str(exc), counts_as_failure=False) from exc
        except Exception as exc:  # noqa: BLE001 - 工具侧失败也要是一句人话
            raise ToolError(self.name, f"这次没做出风格：{str(exc)[:200]}",
                            counts_as_failure=False) from exc
        await self._apply(got["name"], got["display"])
        note = ""
        if got.get("notes"):
            note = "；参考里有读不了的：" + "；".join(got["notes"][:2])
        source = (f"按 {got.get('frames')} 张参考画面" if got.get("frames")
                  else "按你说的")
        why = f"（{got['description'][:60]}）" if got.get("description") else ""
        return (f"已{source}做好《{got['display']}》，并用于这个对话{why}。"
                f"接下来的分镜和成片按它画{note}。")

    async def _adjust(self, *, words: str, user_id: str,
                      conversation_id: str) -> str:
        ident = current_identity()
        cur = str(getattr(ident, "style_skill", "") or "")
        if not cur:
            return await self._make(words=words, material_ids=[], user_id=user_id,
                                    conversation_id=conversation_id)
        row = await self._storage.skills.get(cur)
        if row is None or not _is_style(row):
            return await self._make(words=words, material_ids=[], user_id=user_id,
                                    conversation_id=conversation_id)
        owner = row.get("owner_user_id")
        if owner not in (None, user_id):
            return await self._make(words=words, material_ids=[], user_id=user_id,
                                    conversation_id=conversation_id)
        manifest = _manifest_of(row)
        display = _display_of(row) or cur
        body = str(row.get("body") or "")
        templates = await self._templates(user_id)
        try:
            rev = await gen.revise(
                self._llm, instruction=words, manifest=manifest, body=body,
                templates=templates, identity_user=user_id,
                conversation_id=conversation_id)
        except gen.DraftError as exc:
            raise ToolError(self.name, str(exc), counts_as_failure=False) from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolError(self.name, f"这次没改出来：{str(exc)[:200]}",
                            counts_as_failure=False) from exc

        # 系统风格只读：改动落在用户自己的一份上，内建那份一个字节都不动。
        target = cur
        if owner is None:
            target = await self._fresh_name()
        fm = {"name": target, "display": display, "kind": "style",
              "description": str(row.get("description") or ""),
              "style_manifest": dict(rev["manifest"])}
        if target == cur:
            # 留一步可退回：改坏了还能读回上一版。只留一步，不建版本表。
            fm["style_manifest_prev"] = manifest
            fm["body_prev"] = body
        await self._storage.skills.upsert(
            target, rev["body"], description=str(row.get("description") or ""),
            frontmatter=fm, owner_user_id=user_id)
        await self._apply(target, display)
        note = str(rev.get("note") or "").strip()
        tail = ""
        if rev.get("needs_template"):
            tail = ("；要换的是画面里的部件本身——下次把一条参考视频或几张参考图"
                    "一起发来，就照着重做。")
        return (f"已改好《{display}》" + (f"：{note}" if note else "")
                + tail + "。这一版接着按它做。")

    async def _choose(self, *, words: str, user_id: str) -> str:
        if not words.strip():
            return await self._list(user_id)
        if any(w in words for w in _AUTO_WORDS):
            await self._apply("", "")
            return "已回到「按选题自己定」：之后不再挂固定风格。"
        styles = await self._styles(user_id)
        hit = _pick(styles, words)
        if hit is None:
            names = "、".join(_display_of(s) for s in styles[:8]) or "无"
            raise ToolError(
                self.name,
                f"没有找到「{words.strip()[:30]}」这套风格。现在可用：{names}。"
                "要一套新的就用 make 直接做，不要让用户去建。",
                counts_as_failure=False)
        name = str(hit.get("name") or "")
        await self._apply(name, _display_of(hit))
        return f"已换成《{_display_of(hit) or name}》，这一版按它画。"

    async def _list(self, user_id: str) -> str:
        styles = await self._styles(user_id)
        ident = current_identity()
        cur = str(getattr(ident, "style_skill", "") or "")
        if not styles:
            return "现在没有可选风格；用户想要什么就直接用 make 做一套。"
        lines = []
        for row in styles[:20]:
            name = str(row.get("name") or "")
            lines.append(f"- {_display_of(row) or name}"
                         + ("（系统内置）" if row.get("owner_user_id") is None else ""))
        now = f"当前：{cur}" if cur else "当前：按选题自己定"
        return now + "。可用：\n" + "\n".join(lines)

    async def execute(self, action: str = "make", words: str = "",
                      material_ids: Sequence[str] | None = None) -> Any:
        ident = current_identity()
        if ident is None or not ident.user_id:
            raise ToolError(self.name, "没有当前用户，无法改画面风格",
                            counts_as_failure=False)
        act = str(action or "make").strip().lower()
        text = str(words or "").strip()
        ids = [str(m) for m in (material_ids or []) if str(m).strip()]
        if act == "list":
            return await self._list(ident.user_id)
        if act == "choose":
            return await self._choose(words=text, user_id=ident.user_id)
        if act == "adjust":
            if not text and not ids:
                raise ToolError(self.name, "先说一句想改哪里",
                                counts_as_failure=False)
            if ids:
                # adjust 带参考素材 = 「照着重做这一版」。不静默丢掉附件：走 make 的
                # 读画面通路，把用户真正想换的那套视觉做出来。
                return await self._make(
                    words=text or "照着这些参考画面重做一套",
                    material_ids=ids, user_id=ident.user_id,
                    conversation_id=ident.conversation_id)
            return await self._adjust(words=text, user_id=ident.user_id,
                                      conversation_id=ident.conversation_id)
        # make（默认）
        if not text and not ids:
            raise ToolError(self.name, "先说一句想要什么视觉，或给一条参考视频/图片",
                            counts_as_failure=False)
        return await self._make(words=text, material_ids=ids,
                                user_id=ident.user_id,
                                conversation_id=ident.conversation_id)


def register_style_tools(registry: Any, *, storage: Any, llm: Any,
                         mq: Any = None) -> None:
    registry.register(StyleLookTool(storage=storage, llm=llm, mq=mq))
