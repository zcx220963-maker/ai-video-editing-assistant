"""向用户提问的工具：把「需要用户拿主意」的时刻从打字问答变成选项卡弹窗。

**为什么要有这个工具。** 真机踩到的两个问题都指向同一件事：

1. 模型遇到该问用户的地方，只能把问题写进普通答复里，用户**必须打字**回答。
   而用户真正想要的是「列几个选项，点一下」——`docs/` 里那张弹窗截图就是这个意思。
2. 更糟的是模型有时**不问就自己决定**。真机实例：用户说「90 秒」，模型发现
   按 90 秒会把最后一句金句截断，却选择直接截断、只在事后「告知偏差」。
   用户的诉求是：这种时候应该停下来问，而不是替用户决定。

所以这里提供一个模型可主动调用的提问工具。它**不返回业务结果**，而是让本轮挂起、
把一道结构化的问题发到前端；用户选完之后从断点续跑，把所选 key 作为工具结果喂回模型
（见 ``agent.AgentOnceRun.approve`` 与 ``_APPROVAL_DECISION_TEXT`` 的同一条路）。

**与审批断点共用什么。** 挂起/续跑/落盘全部复用既有的 HITL 机制
（``checkpoint.await_approval`` + ``action.op="approve"``），只是帧里多带一个
``ask`` 结构。好处是「刷新后还能重开」这类修复一次性覆盖两条路径。
"""

from __future__ import annotations

import json
from typing import Any

from .tool import Tool, ToolError

# 一道问题最多几个选项：够用即可，过多会让弹窗变成菜单。
MAX_OPTIONS = 6
# 每个选项的说明与标题长度上限（前端会截断显示，这里先卡住，避免超长文案）。
MAX_TITLE_CHARS = 200
MAX_LABEL_CHARS = 60
MAX_DESC_CHARS = 240


def normalize_question(payload: Any) -> dict[str, Any]:
    """把模型给的参数整理成前端直接可渲染的 ``ask`` 结构。

    容许模型写得比较随意（options 里给字符串数组也行），但输出形状固定：
    ``{title, options: [{key, label, description, recommended}], allow_custom,
    custom_hint, multi}``。整理失败抛 ``ToolError``，让模型按提示重写——
    静默兜一个空问题会让前端弹出一张没有选项的卡，比报错更糟。
    """
    if not isinstance(payload, dict):
        raise ToolError("ask_user", "参数必须是一个对象")
    title = str(payload.get("title") or payload.get("question") or "").strip()
    if not title:
        raise ToolError("ask_user", "必须给 title（你要问用户的那句话）")
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS]

    raw_options = payload.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        raise ToolError("ask_user", "必须给 options（不少于 1 个可选项）")
    if len(raw_options) > MAX_OPTIONS:
        raise ToolError("ask_user", f"options 最多 {MAX_OPTIONS} 个")

    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_options, start=1):
        if isinstance(item, str):
            key, label, desc, rec = item.strip(), item.strip(), "", False
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("title") or "").strip()
            key = str(item.get("key") or label).strip()
            desc = str(item.get("description") or item.get("detail") or "").strip()
            rec = bool(item.get("recommended"))
        else:
            raise ToolError("ask_user", f"第 {i} 个选项既不是字符串也不是对象")
        if not label:
            raise ToolError("ask_user", f"第 {i} 个选项没有 label")
        if not key:
            key = f"opt{i}"
        if key in seen:
            # 撞 key 会让「用户选了哪个」无从区分，直接打回让模型改
            raise ToolError("ask_user", f"选项 key 「{key}」重复，请换一个")
        seen.add(key)
        options.append({
            "key": key[:MAX_LABEL_CHARS],
            "label": label[:MAX_LABEL_CHARS],
            "description": desc[:MAX_DESC_CHARS],
            "recommended": rec,
        })

    return {
        "title": title,
        "options": options,
        "allow_custom": bool(payload.get("allow_custom", True)),
        "custom_hint": str(payload.get("custom_hint") or "其他（自己写一句）")[:MAX_LABEL_CHARS],
        "multi": bool(payload.get("multi", False)),
    }


class AskUserTool(Tool):
    """让模型把「需要用户拿主意」的时刻变成一次弹窗提问。

    用法约定（写进 description，模型据此调用）：
      · 只在**确实需要用户决定**时用：时长/取舍/风格/是否保留某段/是否继续渲染；
      · 不要用它问「要不要继续」这类空话，也不要拿它代替正常答复；
      · 建议标一个 ``recommended: true``，前端会显示「推荐」徽标。
    """

    def __init__(self) -> None:
        # 提问本身不产生副作用，也不改任何状态（挂起由循环负责）
        self.last_question: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def display_name(self) -> str:
        return "向用户提问"

    @property
    def description(self) -> str:
        return (
            "向用户提一个多选题，界面上会弹出选项卡片，用户点选后再继续。"
            "**什么时候该用**：需要用户拿主意而你自己定不了时——例如"
            "「目标时长会让最后一句被截断，是保完整句子把时长放宽，还是就按原时长截断」、"
            "「要在保留某人出镜和画面干净之间取舍」、「不确定风格时让用户挑」。"
            "**什么时候不该用**：能自己判断的不要问；不要用它确认「是否继续」；"
            "不要拿它代替正常答复。"
            "**怎么写**：title 是要问的那句话；options 给 2~6 个有真实差异的选项，"
            "把你建议的那个标 recommended=true；确有必要时用户还能自己写一条。"
            "调用后本轮会停在这里等用户选择，不要在同一条消息里继续往下编造结果。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "要问用户的那句话（一句话说清在问什么）"},
                "options": {
                    "type": "array",
                    "description": "2~6 个可选项，按推荐程度排",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string",
                                    "description": "选项标识（英文/短词，回喂给你时用它）"},
                            "label": {"type": "string", "description": "选项标题（短）"},
                            "description": {"type": "string",
                                            "description": "一句话说明这个选项的后果"},
                            "recommended": {"type": "boolean",
                                            "description": "是否是你推荐的选项"},
                        },
                        "required": ["label"],
                    },
                },
                "allow_custom": {"type": "boolean",
                                 "description": "是否允许用户自己写一条（默认 true）"},
                "custom_hint": {"type": "string",
                                "description": "自定义项的叫法，默认「其他（自己写一句）」"},
            },
            "required": ["title", "options"],
        }

    @property
    def read_only(self) -> bool:
        # 不写任何数据，但**不能**与别的工具并批：它的语义是「停下来问」，
        # 和别的写操作并发会让人分不清用户到底在批准哪一步。
        return True

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        """整理问题并记下；真正的挂起由 AgentOnceRun 在工具轮之后处理。

        返回一段**给模型自己看**的说明（不是给用户看的）：它应当停下来等，
        而不是接着把结果编出来。
        """
        question = normalize_question(kwargs)
        self.last_question = question
        return json.dumps({
            "asked": True,
            "title": question["title"],
            "options": [o["key"] for o in question["options"]],
            "note": "问题已发给用户，本轮到此为止。不要继续编造用户还没做的选择，"
                    "也不要声称已经开始执行。",
        }, ensure_ascii=False)
