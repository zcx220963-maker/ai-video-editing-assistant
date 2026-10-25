# -*- coding: utf-8 -*-
"""工程落盘：一条片子的**全部可重算材料**存成一个目录，随时能改一镜再渲。

为什么必须有这一层：没有它，用户说"第 7 镜那个太阳太大了"，
唯一的办法是**整条重跑**——24 次调用、15 分钟，而且重跑出来的第 7 镜还是另一个样子。
这正是"Claude + Remotion"那种工作流和"点一下生成"的分界线：
前者的产物是**工程**（可改、可只重渲一镜），后者是**一个 mp4**（黑箱）。

目录结构（`/app/.runtime/simple/films/<id>/`）：

    film.json     分镜 / 每镜秒数 / 转场 / 每镜代码 / 质检读数 / 成片链接
    film.tsx      拼好的整片模块（渲不出来时这是唯一能查的东西）
    s01.tsx …     每镜单独一份，方便人直接读改

`film.json` 里的 `bodies` 是**每镜的组件体**（不含 import/export），
拼装规则在 `film.assemble()`——所以改完一镜只需要重新拼一次，不用再问模型。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import kernel

#: 一屏能列几条。列表只是给"我刚才做的那个片子呢"用的，不做分页。
LIST_LIMIT = 10


def root() -> Path:
    d = kernel.out_dir() / "films"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_id() -> str:
    """按时间排序的短 id：`20261025-0829-3f2a`。

    为什么不用 uuid：人要在日志和对话里认出"哪一条"，时间前缀比随机串有用得多。
    """
    import uuid
    return time.strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]


def dir_of(film_id: str) -> Path:
    return root() / film_id


def save(film_id: str, *, code: str, prompt: str, seconds: float, aspect: str,
         fps: int, plan: dict[str, Any], secs: list[float], kinds: list[str],
         bodies: list[str], audit: dict[str, Any] | None = None,
         coverage: dict[str, Any] | None = None, calls: int = 0,
         rewritten: list[int] | None = None,
         fallback: list[int] | None = None,
         mp4_url: str = "", object_key: str = "",
         audio: str = "", mode: str = "") -> Path:
    """落盘。返回 film.json 的路径。

    `audio` 是这条片子的音轨（工程目录里的相对文件名）。重渲时**画面重做、音轨照用**
    ——旁白与镜长是一起定下来的，重渲不该让声音变一遍。

    `mode` 必须落盘：改一镜时要按这条片子自己的画法重写（叙事片用动效写手重画，
    出来就是一帧几何色块混在剪影叙事里）。不存它，`reshoot` 只能猜。
    """
    d = dir_of(film_id)
    d.mkdir(parents=True, exist_ok=True)

    (d / "film.tsx").write_text(code, encoding="utf-8")
    for i, body in enumerate(bodies):
        (d / f"s{i + 1:02d}.tsx").write_text(body, encoding="utf-8")

    doc = {
        "id": film_id,
        "created_at": time.time(),
        "prompt": prompt,
        "seconds": round(float(seconds), 2),
        "aspect": aspect,
        "fps": int(fps),
        "mode": mode or "",
        "title": str((plan or {}).get("title") or ""),
        "plan": plan or {},
        "secs": [round(float(s), 2) for s in secs],
        "kinds": list(kinds),
        "bodies": list(bodies),
        "audit": audit or {},
        "coverage": coverage or {},
        "calls": int(calls),
        "rewritten": list(rewritten or []),
        "fallback": list(fallback or []),
        "mp4_url": mp4_url,
        "object_key": object_key,
        "audio": audio,
        "version": 1,
    }
    path = d / "film.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load(film_id: str) -> dict[str, Any]:
    """读工程。不存在就抛 `FileNotFoundError`——调用方负责翻成一句人话。"""
    path = dir_of(film_id) / "film.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到工程 {film_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def update(film_id: str, **fields: Any) -> dict[str, Any]:
    """改几个字段再存回去（改一镜之后用）。"""
    doc = load(film_id)
    doc.update(fields)
    (dir_of(film_id) / "film.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def list_recent(limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """最近的工程（只读 film.json 的头部字段，不读 bodies——那会很慢）。"""
    out: list[dict[str, Any]] = []
    try:
        dirs = sorted(root().iterdir(), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    for d in dirs:
        if len(out) >= limit:
            break
        f = d / "film.json"
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": doc.get("id") or d.name,
            "title": doc.get("title") or "",
            "prompt": doc.get("prompt") or "",
            "seconds": doc.get("seconds"),
            "mode": doc.get("mode") or "",
            "shots": len(doc.get("bodies") or []),
            "calls": doc.get("calls"),
            "mp4_url": doc.get("mp4_url") or "",
            "created_at": doc.get("created_at"),
        })
    return out
