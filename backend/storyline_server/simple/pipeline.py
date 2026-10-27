"""简洁视频线的**唯一实现入口**（R18）。

在这之前，同一套 `simple` 能力有**两个调用面、零 Node**：
`POST /simple/video` 直接调 `film.make_film`，`tools/short_video.py` 的六个工具
各自 `from storyline_server.simple import ...` 再各自调。两边都会漂移。

现在所有调用方（HTTP、六个 Agent 工具、`simple_film` Node）只认这一个模块：
它把 `film / edit / export / project / deliver / facts / kernel / showrunner`
收进来再统一出口，业务体仍是原来那份（不重写已验证的出片链），
但**入口只剩一个**。

与设计 §21「流程原子化」的差距（simple 内部仍是单节点黑盒）登记在
文档 §13 X1/X2，不在这里假装已经拆开。
"""

from __future__ import annotations

from typing import Any, Callable

from . import deliver, edit, export, facts, film, kernel, project, showrunner, takes, voice

__all__ = [
    "deliver", "edit", "export", "facts", "film", "kernel", "project",
    "showrunner", "takes", "voice",
    "make_film", "shot_stills", "estimate_seconds", "save_project",
    "publish", "publish_bytes", "run", "OUTPUT_FIELDS",
]

make_film = film.make_film
shot_stills = film.shot_stills
estimate_seconds = film.estimate_seconds
publish = deliver.publish
publish_bytes = deliver.publish_bytes

#: 出片结果的对外契约（R18-b：HTTP 与 Agent 工具必须返回同一组字段）。
OUTPUT_FIELDS = ("mp4_url", "object_key", "title", "sources", "receipt")


def save_project(film_id: str, **fields: Any) -> None:
    """落一份工程（与网页 `/studio` 共用同一份落盘）。失败不致命。"""
    try:
        project.save(film_id, **fields)
    except OSError:
        pass


async def run(
    prompt: str,
    *,
    seconds: float,
    aspect: str,
    user_id: str = "",
    conversation_id: str = "",
    mode: str = "auto",
    voice_on: bool = True,
    scenes: Any = None,
    on_stage: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """一句话出片的第一段：分镜 → N 镜代码 → 质检（渲染由调用方接着做）。

    返回 `film.make_film` 的原始结果，外加 `title` / `sources` 两个契约字段，
    这样 HTTP 与工具不需要各自再解析一遍 plan。
    """
    got = await make_film(
        prompt, seconds=seconds, aspect=aspect,
        user_id=user_id, conversation_id=conversation_id or "",
        on_stage=on_stage, voice=voice_on, mode=mode or "auto",
        scenes=scenes or None,
    )
    got.setdefault("title", (got.get("plan") or {}).get("title") or "")
    audit = facts.audit_sources(got.get("plan") or {})
    got.setdefault("sources", audit.get("sources") or [])
    got.setdefault("sources_note", audit.get("note") or "")
    return got


def receipt(mp4_url: str, object_key: str = "", **extra: Any) -> dict[str, Any]:
    """统一回执：五个契约字段 + 需要的附加信息。"""
    out = {"mp4_url": mp4_url, "object_key": object_key, **extra}
    missing = [k for k in OUTPUT_FIELDS if k not in out]
    if missing:
        out.setdefault("receipt", {})["missing"] = missing
    return out
