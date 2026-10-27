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

import shutil
from pathlib import Path
from typing import Any, Callable

from . import deliver, edit, export, facts, film, kernel, project, showrunner, takes, voice

__all__ = [
    "deliver", "edit", "export", "facts", "film", "kernel", "project",
    "showrunner", "takes", "voice",
    "make_film", "shot_stills", "estimate_seconds", "save_project",
    "publish", "publish_bytes", "run", "render_and_publish",
    "render_code_and_publish", "OUTPUT_FIELDS",
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


async def render_code_and_publish(code: str, *, seconds: float, aspect: str,
                                  storage: Any, user_id: str = "",
                                  track: Any = None, key: str = "film") -> dict[str, Any]:
    """一段代码 → 渲 → 合音轨 → 上传。**最底层那一份，全仓唯一。**

    按工程的出片与 HTTP 的候选渲染都落到这里；它之下 `kernel.render` /
    `voice.mux` / `deliver.publish` 各只出现一次。
    """
    rendered, res = await kernel.render(code, seconds=seconds, aspect=aspect)
    final, voice_state = rendered, "none"
    if track is not None and Path(track).exists():
        final = voice.mux(rendered, Path(track), kernel.out_dir() / f"{key}-av.mp4")
        voice_state = "ok"
    pub = await deliver.publish(final, storage=storage, user_id=user_id)
    return {"mp4_url": pub["url"], "object_key": pub["object_key"],
            "duration": round(float(seconds), 2), "voice": voice_state,
            "size_mb": round(pub["bytes"] / 1048576, 1),
            "render_ms": (kernel.describe_receipt(res).get("ms") or {}).get("render_ms")}


async def render_and_publish(film_id: str, *, storage: Any, user_id: str = "",
                             code: str | None = None,
                             track: Any = None,
                             audio_name: str = "") -> dict[str, Any]:
    """按**工程**渲：读工程 → 渲 → 合音轨 → 上传 → 回写工程。

    这段原来被抄了三遍（HTTP、原生 `make_short_video` / `render_short_film`、
    节点 `render_short_film`），改一处忘另一处——`media_url` 那次就是这么漏的。

    · 首次出片：传 `track` + `audio_name`，本函数存进工程、合流、上传。
    · 重渲：什么都不传，用工程里的 `audio`（画面重做、音轨照用）。
    """
    proj = project.load(film_id)
    src = code
    if src is None:
        path_tsx = project.dir_of(film_id) / "film.tsx"
        if not path_tsx.exists():
            raise kernel.KernelError("render", f"工程 {film_id} 里没有 film.tsx，无法渲染")
        src = path_tsx.read_text(encoding="utf-8")

    # 渲多长按**这条片子实际的每镜秒数**算，不是按用户当初要的 seconds：
    # 有旁白时镜长由语音决定，seconds 只是目标。用错这个数会截尾或补黑。
    secs = [float(s) for s in proj.get("secs") or []]
    total = round(sum(secs), 2) if secs else float(proj.get("seconds") or 30)
    aspect = kernel.norm_aspect(proj.get("aspect"))

    # 音轨：给了新的就存进工程（下次重渲照用），没给用工程里那份。
    use = track
    if use is None:
        saved = str(proj.get("audio") or "")
        if saved and (project.dir_of(film_id) / saved).exists():
            use = project.dir_of(film_id) / saved
    if use is not None and Path(use).exists() and audio_name:
        shutil.copyfile(use, project.dir_of(film_id) / audio_name)
        project.update(film_id, audio=audio_name)

    made = await render_code_and_publish(
        src, seconds=total, aspect=aspect, storage=storage, user_id=user_id,
        track=use, key=film_id)
    project.update(film_id, mp4_url=made["mp4_url"], object_key=made["object_key"])
    return {**made, "film_id": film_id, "shots": len(secs)}
