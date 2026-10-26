# -*- coding: utf-8 -*-
"""简洁视频线的 HTTP 口 + 作业表 + `/studio` 单页。

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

from storyline_server.simple import film, kernel, project, takes
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
            "seconds": kernel.clamp_seconds(body.get("seconds")),
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
        # 没指定画法就先让模型判（和 `make_film` 同一条判据），这样前端不用逼用户选。
        style = (want if want in (film.MODE_STORY, film.MODE_EXPLAINER)
                 else await film.pick_style(prompt, seconds=sec, user_id=user_id,
                                            conversation_id=conv))
        try:
            if style == film.MODE_STORY:
                got, calls = await film.plan_story(
                    prompt, seconds=sec, aspect=asp, user_id=user_id,
                    conversation_id=conv)
            else:
                got, calls = await film.plan(
                    prompt, seconds=sec, aspect=asp, user_id=user_id,
                    conversation_id=conv)
        except KernelError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"分镜失败：{type(exc).__name__}: {exc}") from exc
        return {"ok": True, "style": style, "calls": calls,
                "shots": len(got.get("scenes") or []), "plan": got}

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

    @app.get("/studio", response_class=HTMLResponse)
    async def studio() -> str:
        """单页。不需要登录也能打开——没登录时提交会 401，页面自己提示去设置。"""
        return STUDIO_HTML


#: 页面内联在 Python 里：没有构建步骤、没有额外静态目录、改完重启即生效。
STUDIO_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>一句话出片</title>
<style>
:root{--bg:#0a0c12;--card:#12161f;--line:#232a36;--ink:#e8eef7;--dim:#8b97a8;
      --acc:#22d3ee;--hot:#ff6b2c;--bad:#f87171}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.6 "Noto Sans CJK SC","Noto Sans SC",system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:22px;margin:0 0 6px;font-weight:600}
.sub{color:var(--dim);font-size:13px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
textarea{width:100%;min-height:88px;resize:vertical;background:#0e131b;color:var(--ink);
         border:1px solid var(--line);border-radius:8px;padding:12px;font:inherit}
textarea:focus{outline:none;border-color:var(--acc)}
.row{display:flex;gap:12px;margin-top:12px;flex-wrap:wrap;align-items:center}
label{color:var(--dim);font-size:13px}
select{background:#0e131b;color:var(--ink);border:1px solid var(--line);
       border-radius:8px;padding:7px 10px;font:inherit}
button{background:var(--acc);color:#04131a;border:0;border-radius:8px;padding:10px 22px;
       font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.45;cursor:default}
.status{margin-top:14px;font-size:13px;color:var(--dim);min-height:20px}
.status b{color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}
.take{background:#0e131b;border:2px solid var(--line);border-radius:10px;padding:8px;
      cursor:pointer;transition:border-color .15s}
.take:hover{border-color:#3a4557}
.take.sel{border-color:var(--hot)}
.take .frames{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px}
.take img{width:100%;border-radius:4px;display:block;background:#000}
.take .no{font-size:12px;color:var(--dim);margin-bottom:6px}
video{width:100%;border-radius:10px;margin-top:16px;background:#000}
.err{color:var(--bad);font-size:13px;margin-top:12px;white-space:pre-wrap}
.hint{color:var(--dim);font-size:12px;margin-top:10px}
</style></head><body><div class="wrap">
<h1>一句话出片</h1>
<div class="sub">写一句你想看到的画面。成片会自己分镜、逐镜画、配旁白与配乐，一次渲完。</div>

<div class="card">
  <textarea id="p" placeholder="例：人类第一次收到来自半人马座的外星信号。1975 年一台射电望远镜捕捉到一段重复的脉冲，四十年后人们才明白那是一次告别。"></textarea>
  <div class="row">
    <label>模式
      <select id="m"><option value="auto" selected>成片 · 自动（推荐）</option>
      <option value="story">成片 · 叙事片</option>
      <option value="film">成片 · 动效科普</option>
      <option value="shot">单镜（快出预览）</option></select>
    </label>
    <label>时长
      <select id="s"><option value="4">4 秒</option><option value="6">6 秒</option>
      <option value="8">8 秒</option><option value="12">12 秒</option>
      <option value="20">20 秒</option><option value="30" selected>30 秒</option>
      <option value="60">60 秒</option><option value="90">90 秒</option>
      <option value="120">120 秒</option><option value="180">180 秒</option>
      <option value="240">240 秒</option><option value="270">270 秒</option>
      <option value="300">300 秒（上限）</option></select>
    </label>
    <label>画幅
      <select id="a"><option value="portrait" selected>竖屏 9:16</option>
      <option value="landscape">横屏 16:9</option><option value="square">方图 1:1</option></select>
    </label>
    <label>配乐
      <select id="b"><option value="">不配乐</option></select>
    </label>
    <label>候选
      <select id="t"><option value="1">1 条（直接出片）</option>
      <option value="2">2 条</option><option value="3" selected>3 条</option></select>
    </label>
    <button id="plan">先看分镜</button>
    <button id="go">直接出片</button>
  </div>
  <div class="status" id="st"></div>
  <div class="err" id="er"></div>
</div>

<div id="planbox" class="card" style="display:none">
  <div class="hint" id="plannote"></div>
  <div id="planshots"></div>
  <div class="row">
    <button id="cont">分镜没问题，出片</button>
    <span class="hint">上面每一句都能直接改；改完再点。确认后才开始烧渲染。</span>
  </div>
</div>

<div id="picks" class="grid"></div>
<div class="row" id="pickrow" style="display:none">
  <button id="rend">渲这条</button>
  <span class="hint">点一张选中，再渲。选中那一条才会烧成片。</span>
</div>
<video id="v" controls playsinline style="display:none"></video>
<div class="hint" id="meta"></div>

<script>
var $ = function (id) { return document.getElementById(id); };
var jobId = "", sel = -1, timer = null;

function setStatus(t) { $("st").innerHTML = t ? "<b>" + t + "</b>" : ""; }
function setErr(t) { $("er").textContent = t || ""; }

var planObj = null;

(function loadBgm() {
  fetch("/simple/bgm").then(function (r) { return r.json(); }).then(function (d) {
    var sel = $("b");
    (d.songs || []).forEach(function (s) {
      var o = document.createElement("option");
      o.value = s.name;
      o.textContent = s.name + (s.duration ? "（" + Math.round(s.duration) + " 秒）" : "");
      sel.appendChild(o);
    });
    if (!(d.songs || []).length) {
      var o = document.createElement("option");
      o.value = ""; o.textContent = "曲库是空的（先在音乐库导入）";
      sel.appendChild(o);
    }
  }).catch(function () {});
})();

$("plan").onclick = async function () {
  var prompt = $("p").value.trim();
  if (!prompt) { setErr("先写一句你想看到的画面"); return; }
  setErr(""); $("planbox").style.display = "none"; planObj = null;
  $("plan").disabled = true; setStatus("出分镜中…（只花 1 次调用，约半分钟）");
  try {
    var r = await fetch("/simple/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: prompt, seconds: +$("s").value,
                             aspect: $("a").value, mode: $("m").value })
    });
    var d = await r.json();
    if (!r.ok) { setErr(d.detail || ("HTTP " + r.status)); setStatus(""); return; }
    planObj = d.plan;
    $("plannote").textContent = (d.plan.title ? "《" + d.plan.title + "》　" : "") +
      d.shots + " 镜　" + d.calls + " 次调用" +
      (d.plan.motif ? "　贯穿物件：" + d.plan.motif : "");
    $("planshots").innerHTML = (d.plan.scenes || []).map(function (s, i) {
      var tag = (s.caption && s.caption.mode === "drawn") ? "（自画字幕）" : "";
      var tbl = s.table ? "（对照表：" + (s.table.title || s.table.cols.join("/")) + "）" : "";
      return '<div style="display:flex;gap:10px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07)">' +
        '<span style="color:#8a8a8a;min-width:26px">' + (i + 1) + '</span>' +
        '<input data-i="' + i + '" value="' + String(s.text || "").replace(/"/g, "&quot;") +
        '" style="flex:1;background:transparent;border:0;border-bottom:1px dashed rgba(255,255,255,.15);color:#f2f5f8;font:inherit;padding:2px 0">' +
        '<span style="color:#b8860b;font-size:12px;white-space:nowrap">' + tag + tbl + '</span></div>';
    }).join("");
    $("planbox").style.display = "block";
    setStatus("");
  } catch (e) { setErr("" + e); setStatus(""); }
  finally { $("plan").disabled = false; }
};

$("cont").onclick = async function () {
  if (!planObj) { setErr("先点「先看分镜」"); return; }
  document.querySelectorAll("#planshots input").forEach(function (inp) {
    var i = +inp.dataset.i;
    if (planObj.scenes[i]) planObj.scenes[i].text = inp.value.trim() || planObj.scenes[i].text;
  });
  $("planbox").style.display = "none";
  await submit({ prompt: $("p").value.trim(), seconds: +$("s").value,
                 aspect: $("a").value, mode: $("m").value, takes: 1,
                 bgm: $("b").value, scenes: planObj });
};

async function submit(payload) {
  setErr(""); $("v").style.display = "none"; $("meta").textContent = "";
  $("picks").innerHTML = ""; $("pickrow").style.display = "none"; sel = -1;
  $("go").disabled = true; $("plan").disabled = true; setStatus("提交中…");
  try {
    var r = await fetch("/simple/video", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (r.status === 401) {
      setStatus(""); setErr("没有登录。先在「设置」里登录并填好模型 key。");
      $("go").disabled = false; return;
    }
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    var j = await r.json();
    jobId = j.job_id; poll();
  } catch (e) {
    setStatus(""); setErr("提交失败：" + e.message);
    $("go").disabled = false; $("plan").disabled = false;
  }
};

$("go").onclick = function () {
  var prompt = $("p").value.trim();
  if (!prompt) { setErr("先写一句你想看到的画面"); return; }
  submit({ prompt: prompt, seconds: +$("s").value, aspect: $("a").value,
           mode: $("m").value, takes: +$("t").value, bgm: $("b").value });
};

function poll() {
  clearTimeout(timer);
  timer = setTimeout(async function () {
    try {
      var r = await fetch("/simple/job/" + jobId);
      if (!r.ok) throw new Error((await r.json()).detail || r.status);
      var j = await r.json();
      paint(j);
      if (["drafting", "rendering", "queued"].indexOf(j.status) >= 0) poll();
    } catch (e) { setErr("轮询失败：" + e.message); $("go").disabled = false; }
  }, 1500);
}

function paint(j) {
  var pr = j.progress || {};
  var label = { queued: "排队中…", drafting: (j.mode === "film" ? "写分镜与镜头…" : "写代码中…"),
                drafted: "挑一条", rendering: "渲染中…（多镜会更久）",
                done: "好了", failed: "失败" }[j.status] || j.status;
  var extra = "";
  if (pr.total) extra = " " + (pr.stage === "plan" ? "分镜" : "镜头 " + pr.done + "/" + pr.total);
  if (j.mode === "film" && j.shots && j.status !== "drafting") extra = " " + j.shots + " 镜";
  setStatus(label + extra + (j.elapsed_sec ? " · " + j.elapsed_sec + "s" : ""));
  if (j.mode === "film") { $("t").parentNode.style.display = "none"; }
  else { $("t").parentNode.style.display = ""; }

  if (j.status === "failed") {
    setErr([j.error].concat(j.lint_errors || []).filter(Boolean).join("\n"));
    $("go").disabled = false;
  }
  if (j.takes && j.takes.length && !$("picks").children.length) {
    j.takes.forEach(function (t) {
      var d = document.createElement("div");
      d.className = "take"; d.dataset.i = t.index;
      var frames = (t.stills || []).map(function (s) { return '<img src="' + s + '">'; }).join("")
        || '<div style="color:#8b97a8;font-size:12px;padding:8px">' + (t.error || "没有静帧") + "</div>";
      d.innerHTML = '<div class="no">候选 ' + (t.index + 1) + '</div><div class="frames">' + frames + "</div>";
      if (!t.has_code) { d.style.opacity = 0.45; d.style.cursor = "default"; }
      else d.onclick = function () {
        sel = t.index;
        Array.prototype.forEach.call($("picks").children, function (x) { x.classList.remove("sel"); });
        d.classList.add("sel"); $("pickrow").style.display = "flex";
      };
      $("picks").appendChild(d);
    });
  }
  if (j.status === "drafted") { $("pickrow").style.display = "flex"; $("go").disabled = false; }
  if (j.status === "done" && j.mp4) {
    $("v").src = j.mp4; $("v").style.display = "block";
    $("pickrow").style.display = "none"; $("go").disabled = false;
    var k = j.receipt || {}, ms = j.ms || {};
    $("meta").textContent = [
      k.seconds ? k.seconds + "s" : "",
      (k.size && k.size[0]) ? k.size.join("×") : "",
      j.calls ? "模型调用 " + j.calls + " 次" : "",
      ms.total_ms ? "总耗时 " + (ms.total_ms / 1000).toFixed(1) + "s" : ""
    ].filter(Boolean).join(" · ");
  }
}

$("rend").onclick = async function () {
  if (sel < 0) { setErr("先点一张候选"); return; }
  setErr(""); $("rend").disabled = true;
  try {
    await fetch("/simple/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, take: sel })
    });
    poll();
  } catch (e) { setErr("提交渲染失败：" + e.message); }
  setTimeout(function () { $("rend").disabled = false; }, 2000);
};
</script></div></body></html>
"""
