"""fetch_media 工具：把「按链接取素材」暴露给 Agent（实现复用 `agent_framework.media_fetch`）。

放在 tools 层而不是 `tools/web.py`，是为了避开一条 import 环：`media_fetch` 要用
web 工具的 SSRF 校验与 UA，反过来让 web 模块 import 它就成环了。

身份与上传同权：user_id / conversation_id 取自 `identity` 的 contextvar（由 Agent 在
run 入口写入），模型既看不到也改不了——所以爬来的素材天然归当前用户当前会话。
"""

from __future__ import annotations

import json
from typing import Any

from ..identity import current_identity
from ..media_fetch import FetchPolicy, FetchRejected, fetch_media
from ..storage import Storage
from ..tool import Tool


class FetchMediaTool(Tool):
    """按链接取素材：直链 / 页面嗅探 / yt-dlp 兜底 → 入库 → 回 material_id。"""

    def __init__(self, storage: Storage | None = None,
                 policy: FetchPolicy | None = None) -> None:
        self._storage = storage
        self._policy = policy or FetchPolicy()

    @property
    def name(self) -> str:
        return "fetch_media"

    @property
    def display_name(self) -> str:
        return "按链接取素材"

    @property
    def description(self) -> str:
        return ("按链接取素材（用户不想自己下载视频时用）。传一条 http/https 链接："
                "视频/音频/图片直链、含视频的网页、或站点播放页都可以，"
                "取回后直接入库成为一条素材。返回 material_id，"
                "剪辑第一步把它原样传给 load_media(material_ids=[…])；"
                "不要把返回的文件名当路径用。多条链接就调用多次。")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "要取料的完整链接（http/https）"},
            },
            "required": ["url"],
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, url: str) -> str:
        if self._storage is None:
            return "Error: 存储层未装配，fetch_media 不可用（不会退回本地路径）"
        ident = current_identity()
        if ident is None or not ident.user_id or not ident.conversation_id:
            return "Error: 当前执行身份缺少 user/conversation，无法登记素材归属"
        try:
            res = await fetch_media(
                self._storage, url, user_id=ident.user_id,
                conversation_id=ident.conversation_id, policy=self._policy)
        except FetchRejected as e:
            return f"Error: 取料失败（{e.status}）：{e.message}"
        except Exception as e:  # noqa: BLE001 - 回喂给 LLM，不让整轮崩掉
            return f"Error: 取料异常：{type(e).__name__}: {e}"
        return json.dumps(
            {**res, "hint": "把它原样传给 load_media(material_ids=[…]) 开始剪辑"},
            ensure_ascii=False)


def register_fetch_media_tools(registry, *, storage: Storage | None = None,
                               policy: FetchPolicy | None = None) -> None:
    registry.register(FetchMediaTool(storage=storage, policy=policy))


__all__ = ["FetchMediaTool", "register_fetch_media_tools"]
