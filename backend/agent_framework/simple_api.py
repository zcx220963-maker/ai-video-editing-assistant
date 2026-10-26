# -*- coding: utf-8 -*-
"""简洁视频线的 HTTP 口 + 作业表。

三个接口，没有第四个：

    POST /simple/video        { prompt, seconds, aspect, takes } -> { job_id }
    GET  /simple/job/{id}     -> 作业视图（含静帧 data URL、成片 presigned URL）
    POST /simple/render       { job_id, take } -> 开始渲那一条
    GET  /studio              单页（无框架、无构建）

**作业表是进程内一个 dict**，不是 DB。重启丢作业、不丢已上传的成片。
这是刻意的：为「重启还能接着渲」建一套 checkpoint，比让用户重点一次贵得多。

注册方式与平台一致：`register(app, storage=..., auth=...)`，由 `server.py` 调一行。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

#: **必须放在模块级**：本文件有 `from __future__ import annotations`，注解是字符串，
#: FastAPI 用 `get_type_hints()` 在**模块 globals** 里找 `Request` / `HTMLResponse`。
#: 放进 `register()` 里当局部导入，它会找不到名字，把 `request` 当成查询参数 → 422。
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from storyline_server.simple import film, kernel, project, showrunner, takes
from storyline_server.simple.kernel import KernelError

#: 同时在跑的作业数上限。渲染容器自己是串行的，这个数只防「连点十次」。
MAX_JOBS = int(os.environ.get("SIMPLE_MAX_JOBS", "3") or 3)
#: 作业在内存里留多久（秒）。过了就清，防止长期运行攒一堆。
JOB_TTL_SEC = float(os.environ.get("SIMPLE_JOB_TTL_SEC", str(2 * 3600)))

_JOBS: dict[str, dict[str, Any]] = {}
_SEM: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(max(1, MAX_JOBS))
    return _SEM


def _prune() -> None:
    now = time.time()
    for jid in [k for k, v in _JOBS.items() if now - v["created_at"] > JOB_TTL_SEC]:
        _JOBS.pop(jid, None)


def _view(job: dict[str, Any]) -> dict[str, Any]:
    """作业 -> 前端视图。静帧转 data URL，成片用 object_key 重签（URL 会过期）。"""
    return {
        "job_id": job["id"],
        "status": job["status"],
        "prompt": job["prompt"],
        "seconds": job["seconds"],
        "aspect": job["aspect"],
        "takes": [
            {
                "index": t["index"],
                "stills": [f"data:image/jpeg;base64,{s}" for s in t.get("stills") or []],
                "error": t.get("error") or "",
                "has_code": bool(t.get("code")),
            }
            for t in job.get("takes") or []
        ],
        "mp4": job.get("mp4_url") or "",
        "mode": job.get("mode") or "shot",
        "style": job.get("style") or "",
        "shots": job.get("shots") or 0,
        "title": job.get("title") or "",
        "progress": job.get("progress") or {},
        "coverage": job.get("coverage") or {},
        "error": job.get("error") or "",
        "stage": job.get("stage") or "",
        "lint_errors": (job.get("lint_errors") or [])[:5],
        "calls": job.get("calls") or 0,
        "voice": job.get("voice") or "",
        "bgm": job.get("bgm") or "",
        "sources": job.get("sources") or [],
        "sources_note": job.get("sources_note") or "",
        "receipt": job.get("receipt") or {},
        "ms": job.get("ms") or {},
        "elapsed_sec": round(time.time() - job["created_at"], 1),
    }


async def _run_film(job: dict[str, Any], *, storage: Any) -> None:
    """成片：分镜 → N 镜代码 → 一个模块 → 一次渲完。"""
    async with _sem():
        job["status"] = "drafting"
        job["stage"] = "plan"

        def on_stage(stage: str, done: int, total: int) -> None:
            job["stage"] = stage
            job["progress"] = {"stage": stage, "done": done, "total": total}

        try:
            got = await film.make_film(
                job["prompt"], seconds=job["seconds"], aspect=job["aspect"],
                user_id=job["user_id"],
                conversation_id=job.get("conversation_id") or "",
                on_stage=on_stage, voice=bool(job.get("voice", True)),
                mode=job.get("style") or "auto",
                scenes=job.get("scenes") or None)
        except KernelError as exc:
            job.update(status="failed", stage=exc.stage, error=str(exc),
                       lint_errors=exc.lint_errors)
            return
        except Exception as exc:  # noqa: BLE001
            job.update(status="failed", stage="plan",
                       error=f"{type(exc).__name__}: {exc}")
            return

        job.update(shots=got["shots"], title=got["plan"].get("title") or "",
                   calls=got["calls"], secs=got["secs"], plan=got["plan"],
                   coverage=got.get("coverage") or {})
        # 出处账：不拦片，只在报告里如实写（见 `simple/facts.py`）。
        from storyline_server.simple import facts as facts_mod
        src = facts_mod.audit_sources(got["plan"])
        job["sources"] = src["sources"]
        job["sources_note"] = src["note"]
        # 落一份**工程**（与对话里那三个工具共用同一份落盘）：网页做的片子，
        # 之后在对话里说"第 7 镜改一下"也能改；反过来也一样。
        try:
            project.save(job["id"], code=got["code"], prompt=job["prompt"],
                         seconds=job["seconds"], aspect=job["aspect"],
                         fps=kernel.DEFAULT_FPS, plan=got["plan"], secs=got["secs"],
                         kinds=got["transitions"], bodies=got["bodies"],
                         audit=got.get("audit") or {},
                         coverage=got.get("coverage") or {}, calls=got["calls"],
                         rewritten=got.get("rewritten"), fallback=got.get("fallback"),
                         mode=got.get("mode") or "")
        except OSError:
            pass
        # 再留一份扁平副本：渲不出来时这是唯一能查的东西（16 次调用换来的）。
        try:
            (kernel.out_dir() / f"film-{job['id']}.tsx").write_text(
                got["code"], encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        # 复用单镜那条渲染路：模块里自带 `<Series>`，composition 仍然是 `shot`。
        _a = got.get("audit") or {}
        if not _a.get("ok"):
            _b = _a.get("bad") or []
            job.update(status="failed", stage="audit", audit=_a,
                       error=(f"{len(_b)}/{got['shots']} 镜是空帧或全黑，已停止渲染。"
                              f"镜号：{_b[:12]}。工程与 tsx 已存，重画这些镜即可。"))
            return
        item = {"index": 0, "code": got["code"]}
        job["status"] = "rendering"
        job["stage"] = "render"
        t0 = time.time()
        try:
            path, res = await kernel.render(
                item["code"], seconds=got["total_sec"], aspect=job["aspect"])
        except KernelError as exc:
            job.update(status="failed", stage=exc.stage, error=str(exc),
                       lint_errors=exc.lint_errors)
            return

        # 音轨：旁白 → 加配乐（曲库）→ 存进工程 → 与画面合流。
        # 失败不整条失败（无声仍是片子）。
        audio_name = ""
        vinfo = got.get("voice") or {}
        if vinfo and not vinfo.get("degraded"):
            try:
                from pathlib import Path
                from storyline_server.simple import voice as vo
                from . import bgm as bgm_pick
                track = vo.build_track(
                    [Path(p) if p else None for p in vinfo.get("files") or []],
                    got["secs"], kernel.out_dir() / f"track-{job['id']}.m4a")
                # 配乐走**曲库**（materials.origin=bgm），取不到退本地目录，再取不到就不加。
                # 这一步以前漏了：`voice.pick_bgm` 只读 SIMPLE_BGM_DIR，而容器里没有那个
                # 目录——于是"配了曲库"在成片里等于没配。
                track, tune = await bgm_pick.mix_into(
                    track, kernel.out_dir() / f"track-{job['id']}-bgm.m4a",
                    storage=storage, user_id=job["user_id"], prompt=job["prompt"],
                    total_sec=got["total_sec"], work_dir=kernel.out_dir() / "bgm",
                    bgm_name=job.get("bgm") or "")
                job["bgm"] = (tune or {}).get("filename") or ""
                audio_name = "audio.m4a"
                import shutil
                shutil.copyfile(track, project.dir_of(job["id"]) / audio_name)
                path = vo.mux(path, track, kernel.out_dir() / f"{job['id']}-av.mp4")
                job["voice"] = "ok"
            except Exception as exc:  # noqa: BLE001
                job["voice"] = f"failed: {type(exc).__name__}: {exc}"
        else:
            job["voice"] = "none"

        job["stage"] = "save"
        try:
            from storyline_server.simple import deliver
            pub = await deliver.publish(path, storage=storage, user_id=job["user_id"])
        except Exception as exc:  # noqa: BLE001
            job.update(status="failed", stage="save",
                       error=f"成片存不进对象存储：{type(exc).__name__}: {exc}；"
                             f"本地文件在 {path}")
            return

        receipt = kernel.describe_receipt(res)
        job.update(status="done", stage="done", mp4_url=pub["url"],
                   object_key=pub["object_key"], receipt=receipt,
                   ms={**receipt.get("ms", {}),
                       "total_ms": int((time.time() - t0) * 1000)})
        try:
            project.update(job["id"], mp4_url=pub["url"], object_key=pub["object_key"],
                           audio=audio_name)
        except (OSError, FileNotFoundError):
            pass


async def _run_draft(job: dict[str, Any], *, storage: Any) -> None:
    """写 N 条候选 + 渲静帧。takes=1 时直接接着渲成片。"""
    async with _sem():
        job["status"] = "drafting"
        job["stage"] = "write"
        try:
            items = await takes.draft(
                job["prompt"], seconds=job["seconds"], aspect=job["aspect"],
                takes=job["takes_n"], user_id=job["user_id"],
                conversation_id=job.get("conversation_id") or "")
        except Exception as exc:  # noqa: BLE001
            job.update(status="failed", stage="write",
                       error=f"{type(exc).__name__}: {exc}")
            return

        job["takes"] = items
        job["calls"] = sum(int(i.get("calls") or 0) for i in items)
        usable = [i for i in items if i.get("code")]
        if not usable:
            first = items[0] if items else {}
            job.update(status="failed", stage=first.get("stage") or "write",
                       error=first.get("error") or "没有可用的代码",
                       lint_errors=first.get("lint_errors") or [])
            return

        if len(usable) == 1:
            # 只有一条能用的，就别让用户点了——直接渲。
            await _render(job, usable[0], storage=storage)
            return

        job["status"] = "drafted"
        job["stage"] = "pick"


async def _render(job: dict[str, Any], item: dict[str, Any], *, storage: Any) -> None:
    job["status"] = "rendering"
    job["stage"] = "render"
    t0 = time.time()
    try:
        path, res = await kernel.render(
            item["code"], seconds=job["seconds"], aspect=job["aspect"])
    except KernelError as exc:
        job.update(status="failed", stage=exc.stage, error=str(exc),
                   lint_errors=exc.lint_errors)
        return
    except Exception as exc:  # noqa: BLE001
        job.update(status="failed", stage="render", error=f"{type(exc).__name__}: {exc}")
        return

    job["stage"] = "save"
    try:
        from storyline_server.simple import deliver
        got = await deliver.publish(path, storage=storage, user_id=job["user_id"])
    except Exception as exc:  # noqa: BLE001
        # 存不进对象存储时，本地文件还在——把路径交出去，至少不丢东西。
        job.update(status="failed", stage="save",
                   error=f"成片存不进对象存储：{type(exc).__name__}: {exc}；"
                         f"本地文件在 {path}")
        return

    receipt = kernel.describe_receipt(res)
    job.update(status="done", stage="done", mp4_url=got["url"],
               object_key=got["object_key"], chosen=int(item["index"]),
               receipt=receipt,
               ms={**receipt.get("ms", {}),
                   "total_ms": int((time.time() - t0) * 1000)})


async def _run_render(job: dict[str, Any], index: int, *, storage: Any) -> None:
    async with _sem():
        try:
            item = takes.pick(job.get("takes") or [], index)
        except KernelError as exc:
            job.update(status="failed", stage=exc.stage, error=str(exc))
            return
        await _render(job, item, storage=storage)


def register(app: Any, *, storage: Any, auth: Any) -> None:
    """挂四个路由。由 `server.py` 调一行——这是本文件唯一碰旧代码的地方。"""

    @app.post("/simple/video")
    async def submit_video(request: Request,
                           user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """提交一次「一句话出片」。立刻返回 job_id，不等渲染。"""
        body = await request.json()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "缺少 prompt：写一句你想看到的画面")
        _prune()
        # `mode` 四个值：`shot` 单镜试片 / `film` 多镜动效科普 / `story` 多镜叙事片 /
        # **`auto`（缺省）让系统自己判画法**——普通用户不该看到那两个词。
        raw = str(body.get("mode") or "").strip().lower()
        style = raw if raw in (film.MODE_STORY, film.MODE_EXPLAINER) else "auto"
        mode = "shot" if raw == "shot" else "film"
        job = {
            "id": uuid.uuid4().hex[:12],
            "created_at": time.time(),
            "user_id": user_id,
            "conversation_id": str(body.get("conversation_id") or ""),
            "prompt": prompt[:2000],
            "mode": mode,
            "style": style,
            # 没给时长就**按内容量估**（原来缺省 6.0 → 夹到 8 秒：用户只说"做个
            # 关于 X 的视频"就得到 8 秒，那是默认值在替他做决定）。
            "seconds": kernel.clamp_seconds(
                body.get("seconds") or film.estimate_seconds(prompt)),
            "aspect": kernel.norm_aspect(body.get("aspect")),
            "takes_n": max(1, min(takes.MAX_TAKES, int(body.get("takes") or 1))),
            "voice": bool(body.get("voice", True)),
            # 配乐**只在点名时才加**（2026-10-26 改）。空 = 不配乐。
            # 点名的那首短了就循环铺满，不换成别的（见 `bgm.mix_into`）。
            "bgm": str(body.get("bgm") or "").strip()[:60],
            # 「先看分镜、改完再继续」那条路：`/simple/plan` 的整份产物原样传回来，
            # 有它就不再调规划（省掉那 1 次调用，也省掉"重跑出来还是另一个样子"）。
            "scenes": body.get("scenes") or None,
            "status": "queued", "stage": "queued", "takes": [],
        }
        _JOBS[job["id"]] = job
        asyncio.create_task((_run_film if mode == "film" else _run_draft)(
            job, storage=storage))
        return {"job_id": job["id"], "status": "queued", "mode": mode,
                "style": style}

    @app.get("/simple/bgm")
    async def bgm_library(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """曲库列表，给前端下拉用。读不到就回空列表——**空列表的意思是"没有可选的"**，
        不是"自动挑"。配乐只在用户点名时才加（见 `bgm.mix_into`）。"""
        rows: list[dict[str, Any]] = []
        try:
            from pathlib import Path
            got = await storage.materials.search(
                user_id, "", conv_id=None, kinds=("audio",), origin="bgm", limit=50)
            for r in got or []:
                rows.append({"name": Path(str(r.get("filename") or "")).stem,
                             "filename": str(r.get("filename") or ""),
                             "duration": round(float(r.get("duration_sec") or 0), 1)})
        except Exception:  # noqa: BLE001 —— 曲库读不到不该让页面打不开
            rows = []
        return {"ok": True, "songs": rows}

    @app.post("/simple/plan")
    async def plan_only(request: Request,
                        user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """只出分镜，不写画面、不渲。**只花 1 次调用**（约 30~60 秒）。

        为什么单独一个端点：一次成片 5~30 分钟、二十几次调用、中间零反馈。
        主题或讲法错了，用户只能整条重跑。先出分镜表让人看一眼——十五句话读完
        只要十秒，读完就知道这条片子对不对。改完把 `plan` 原样传给
        `POST /simple/video` 的 `scenes` 字段，错的那一步就只花 1 次调用。
        """
        body = await request.json()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "缺少 prompt：写一句你想看到的画面")
        sec = kernel.clamp_seconds(body.get("seconds"))
        asp = kernel.norm_aspect(body.get("aspect"))
        conv = str(body.get("conversation_id") or "")
        want = str(body.get("mode") or "").strip().lower()
        await kernel.ensure_storage()
        # 2026-10-26：不再分片种，也不再 `pick_style`——镜数/镜长/画法都由导演定。
        # 原来这里二选一调 `plan` / `plan_story`，而 explainer 那份分镜已随
        # 「LLM 当导演」删掉（`film.plan` 不存在），留着就是 500。
        try:
            got, calls = await showrunner.plan(
                prompt, seconds=sec, aspect=asp, user_id=user_id,
                conversation_id=conv)
        except KernelError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"分镜失败：{type(exc).__name__}: {exc}") from exc
        return {"ok": True, "calls": calls,
                "shots": len(got.get("shots") or []), "plan": got}

    @app.get("/simple/job/{job_id}")
    async def job_view(job_id: str,
                       user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        job = _JOBS.get(job_id)
        if job is None or job["user_id"] != user_id:
            # 传错 id 与无权访问是同一个 404，不区分。
            raise HTTPException(404, "没有这个作业")
        if job.get("object_key") and job["status"] == "done":
            # presigned URL 会过期，每次读都重签一次（比再传一遍便宜得多）。
            try:
                from storyline_server.simple import deliver
                job["mp4_url"] = await deliver.republish(job["object_key"], storage=storage)
            except Exception:  # noqa: BLE001 —— 签不出来就交上一次的链接
                pass
        return _view(job)

    @app.post("/simple/render")
    async def render_take(request: Request,
                          user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        body = await request.json()
        job_id = str(body.get("job_id") or "")
        job = _JOBS.get(job_id)
        if job is None or job["user_id"] != user_id:
            raise HTTPException(404, "没有这个作业")
        if job["status"] in ("rendering", "done"):
            return {"job_id": job_id, "status": job["status"]}
        if job["status"] not in ("drafted", "failed"):
            raise HTTPException(409, f"还没到能渲的时候（当前 {job['status']}）")
        job["status"] = "queued"
        job["error"] = ""
        asyncio.create_task(_run_render(job, int(body.get("take") or 0), storage=storage))
        return {"job_id": job_id, "status": "queued"}


#: 页面内联在 Python 里：没有构建步骤、没有额外静态目录、改完重启即生效。

