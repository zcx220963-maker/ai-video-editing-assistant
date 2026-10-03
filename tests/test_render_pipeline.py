# -*- coding: utf-8 -*-
"""B3 渲染管线专测：对象存储 ↔ 会话工作区 ↔ render_jobs 的边界（spec §5/§6/§9）。

不联网、不起容器（D3：内存替身 + 真 ffmpeg/MoviePy）。钉住七件事：
① 一句话出片后，成片字节只在 ``renders/{会话}/{产物}.mp4``，回放链接只在 presigned
   直链，产物判定只在 ``render_jobs.status='done'``；
② presigned 直链指向的对象可整读、可 Range 读（视频拖动）；
③ 媒体面污染修复：MoviePy 的 ``temp_audio.m4a`` 与兜底分片 ``_fb_*`` 只落会话
   工作区，工作区之外不再出现任何媒体文件（原 ``out_dir`` 概念已退役）；持久化的
   时间线里只有 ``obj:`` 引用，本机路径只在渲染期那份副本上出现；
④ 渲染失败：``render_jobs`` 置 failed 带原因、工作区当场回收、artifacts 里不留
   render_video 产物（引用取不到字节 = 产物失效，同样不降级，绝不产出引用死链的
   "成功"）；
⑤ 工作区 TTL：``sweep_stale`` 只删过期 artifact 目录；
⑥ 内容缓存 LRU：容量上限按最后使用时间逐出，同一内容重复 localize 不占两份；
⑦ 编码期进度：percent 必须从 70 一路走到 98（真机「卡在 70%」= 慢，不是死锁，
   没有回报通路就看门狗也无从判停滞），且字幕与画面层并列而不是逐条嵌套合成。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, Interceptor, NodeState
from agent_framework.storage import build_storage, new_id, ref_key, to_ref
from agent_framework.storage.media import kind_of, mime_of, probe as probe_meta
from storyline_server import mediaops
from storyline_server.nodes.core_nodes import (
    _localize_timeline,
    _overlay_layers,
    _render_via_ffmpeg,
    _render_with_moviepy,
    _slice_timeline,
    _subtitle_layers,
    build_real_registry,
)
from storyline_server.providers import build_providers
from storyline_server.settings import Settings

FAILS = 0

USER = "u_pipe"
CONV = "c_pipe"


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def make_source(dst: Path, parts: list[str], audio_freq: int) -> None:
    inputs, labels, total = [], "", 0.0
    for i, graph in enumerate(parts):
        dur = next(float(t.split("=")[1]) for t in graph.split(":") if t.startswith("duration"))
        total += dur
        inputs += ["-f", "lavfi", "-i", graph]
        labels += f"[{i}:v]"
    mediaops.ffmpeg(*inputs, "-f", "lavfi", "-i", f"sine=frequency={audio_freq}:duration={total}",
                    "-filter_complex", f"{labels}concat=n={len(parts)}:v=1:a=0[v]",
                    "-map", "[v]", "-map", f"{len(parts)}:a",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(dst))


def write_config(tmp: Path) -> Settings:
    """[media] 四目录已退役：TOML 只剩服务端口 + 存储后端（工作区/内容缓存两个可丢弃目录）。"""
    cfg = tmp / "config.toml"
    cfg.write_text(
        f"""
[local_mcp_server]
server_name = "storyline"
port = 8097

[storage]
backend = "memory"
workspace_root = "{(tmp / 'workspace').as_posix()}"
cache_root = "{(tmp / 'object_cache').as_posix()}"
""", encoding="utf-8")
    return Settings.load(cfg)


async def seed(storage, src: Path) -> str:
    mid = new_id("mat", 6)
    key = f"users/{USER}/convs/{CONV}/{mid}{src.suffix}"
    blob = src.read_bytes()

    async def chunks():
        yield blob

    info = await storage.objects.put(key, chunks(), content_type=mime_of(src.name))
    meta = await probe_meta(src)
    await storage.users.provision(USER)
    await storage.conversations.ensure(USER, CONV)
    await storage.materials.register(
        USER, CONV, key, src.name, kind_of(src.name) or "video",
        bytes_=info.bytes, sha256=info.sha256, mime=mime_of(src.name),
        duration_sec=meta["duration_sec"] or None, width=meta["width"] or None,
        height=meta["height"] or None, has_audio=meta["has_audio"],
        origin="library", material_id=mid)
    return mid


def fake_providers(transcribe=None):
    async def llm(messages):
        return json.dumps({
            "title": "渲染管线验证片",
            "groups": [{"group_id": "group_0001", "raw_text": "三个场景的测试解说。"}],
        }, ensure_ascii=False)

    async def tts(text, dst):
        mediaops.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "1.5", str(dst))
        return Path(dst)

    return build_providers(Settings().caps,
                           vision=lambda images, prompt, **kw: "测试画面：条纹与色块",
                           transcribe=transcribe or (lambda wav: []),
                           llm=llm, tts=tts)


def inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


async def prep_creative(itp, state, material_ids):
    """模拟 LLM 显式调用创意决策节点。"""
    state.flags["material_ids"] = material_ids
    await itp.invoke("understand_clips", state)
    caps = (await state.store.get("understand_clips"))["clip_captions"]
    all_clips = [c["clip"] for c in caps]
    await itp.invoke("filter_clips", state, keep_clips=all_clips)
    await itp.invoke("group_clips", state, custom_groups=[{
        "group_id": "g1", "clips": all_clips, "summary": "全部片段",
    }])
    await itp.invoke("script_template_rec", state, template_id="tpl_vlog_3act")


MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".jpg"}


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="render_pipe_"))
    settings = write_config(tmp)
    check(not hasattr(settings, "media"), "MediaSettings 已退役：Settings 里没有 media 属性")
    try:
        bad = tmp / "bad.toml"
        bad.write_text('[media]\nout_dir = "x"\n', encoding="utf-8")
        Settings.load(bad)
        check(False, "残留 [media] 段应直接报错（D1 不留兼容位）")
    except ValueError as e:
        check("[media]" in str(e), "残留 [media] 段应直接报错（D1 不留兼容位）")

    raw = tmp / "raw"
    raw.mkdir(parents=True)
    make_source(raw / "src.mp4", [
        "testsrc2=size=640x360:rate=25:duration=2",
        "smptebars=size=640x360:rate=25:duration=2",
        "color=c=blue:size=640x360:rate=25:duration=2",
    ], 440)

    storage = build_storage("memory",
                            cache_root=settings.storage.cache_root,
                            workspace_root=settings.storage.workspace_root)
    mat = await seed(storage, raw / "src.mp4")
    reg = build_real_registry(settings, fake_providers(), storage)
    itp = Interceptor(reg)

    # ---------- ① 一句话出片：三处真相各自的形状 ----------
    store = await _open_store(storage, "p:main", "art1")
    state = NodeState(session_id="p:main", artifact_id="art1", user_request="剪一条测试片",
                      store=store, user_id=USER, conversation_id=CONV)
    await prep_creative(itp, state, [mat])
    packed = await itp.invoke("render_video", state, material_ids=[mat])
    out = packed["output"]
    key = out["video"]
    check(key == "renders/p_main/art1.mp4", "成片返回对象键，不含任何本地路径")
    head = await storage.objects.head(key)
    check(head is not None and head.bytes > 10_000 and len(head.sha256) == 64,
          "对象存储里有成片字节（head 给出大小与 sha256）")
    url = out["media_url"]
    check(url.startswith("memory://") and key in url and "ttl=" in url,
          "media_url 是 presigned 直链（离线替身 memory://，线上为 http(s)://）")
    job = await storage.render_jobs.get("p:main", "art1")
    check(job["status"] == "done" and job["percent"] == 100 and job["stage"] == "done"
          and job["video_object_key"] == key,
          "产物判定：render_jobs.status=done + video_object_key")
    check(abs(float(job["duration_sec"]) - float(out["duration"])) < 0.05,
          "render_jobs 里存了成片时长（供列表页直接显示）")
    check(await storage.render_jobs.ready("p:main", "art1"), "ready 需 done + 有成片 key")
    # 成片指纹：把「实际被渲染的那一版时间线」记进产物（render_jobs.result 与返回体同源）。
    # 用户三次报「音画不同步」时我们手里只有 duration/title，只能猜；现在能自证。
    digest = out.get("timeline_digest") or {}
    tl_ok = (await state.store.get("plan_timeline"))["timeline"]
    check(len(str(digest.get("sha16") or "")) == 16
          and digest.get("video_segments") == len(tl_ok["events"])
          and digest.get("audio_segments") == len(tl_ok["audio_events"])
          and float(digest.get("max_av_drift_sec", 99)) < 0.25,
          f"成片里存了指纹（sha16 + 段数 + 最大偏差）：{digest}")
    check((await storage.render_jobs.get("p:main", "art1"))["result"]["timeline_digest"]
          == digest, "指纹随 render_jobs.result 入库（轮询口/重启后都拿得到）")
    exec_nodes = await storage.artifacts("p:main", "art1").executed()
    check({"load_media", "plan_timeline", "render_video"} <= set(exec_nodes)
          and len(exec_nodes) >= 10, f"整链产物在 artifacts 表（{len(exec_nodes)} 行）")

    # ---------- ② 直链对象可整读、可 Range 读（前端视频拖动） ----------
    whole = b"".join([c async for c in storage.objects.get_stream(key)])
    check(len(whole) == head.bytes, "整读对象：字节数与 head 一致")
    rng = b"".join([c async for c in storage.objects.get_stream(key, start=0, end=1023)])
    check(len(rng) == 1024 and whole.startswith(rng), "Range 读前 1 KiB 可用（拖动进度条）")
    fetched = await storage.objects.localize(key, tmp / "client")
    info = mediaops.probe(fetched)
    check(info["duration"] > 4.0 and info["width"] == 640,
          f"取回的成片 ffprobe 可读（{info['duration']}s / {info['width']}x{info['height']}）")

    # ---------- ③ 媒体面污染：工作区之外不留任何媒体文件 ----------
    ws = storage.workspace.root
    tl = (await state.store.get("plan_timeline"))["timeline"]
    fb_dir = storage.workspace.dir_for("p:main", "art_fb", "render")
    tl_local = await _localize_timeline(tl, storage.workspace, fb_dir / "src")
    check(all(ref_key(e["path"]) for e in tl["events"]),
          "持久化的时间线里每条事件都只有对象引用")
    check(all(Path(e["path"]).is_file() for e in tl_local["events"]),
          "渲染期副本才指向本机文件（用完即随工作区丢弃）")
    await asyncio.to_thread(_render_via_ffmpeg, tl_local, fb_dir / "art_fb.mp4")
    fb_dst = fb_dir / "art_fb.mp4"
    check(fb_dst.exists() and fb_dst.stat().st_size > 5_000, "兜底渲染路径可用（切片 + concat）")
    fb_parts_dir = next(p for p in ws.rglob("*") if p.is_dir() and p.name.startswith("_fb_"))
    check(inside(fb_parts_dir, ws), f"兜底分片 {fb_parts_dir.name} 落在会话工作区内，不暴露成媒体")

    strays = [p for p in tmp.rglob("*")
              if p.is_file() and p.suffix.lower() in MEDIA_EXTS
              and not inside(p, ws) and not inside(p, raw)
              and not inside(p, tmp / "client")]
    check(not strays, f"工作区/素材原件/下载点之外没有媒体文件泄漏（strays={ [p.name for p in strays] }）")
    check(not (tmp / "out").exists(), "旧 out_dir 目录概念已不存在")

    # ---------- ④ 渲染失败：状态入库 + 工作区回收 + 不留假成功 ----------
    fail_state = NodeState(session_id="p:fail", artifact_id="af", user_request="坏素材也要诚实失败",
                           store=await _open_store(storage, "p:fail", "af"),
                           user_id=USER, conversation_id=CONV)
    broken = json.loads(json.dumps(tl))
    for ev in broken["events"]:
        ev["path"] = to_ref(f"users/{USER}/convs/{CONV}/definitely-missing.mp4")
    await fail_state.store.put("plan_timeline", {"timeline": broken})
    # 同会话重跑要复用的中转字节（localize_ref 取回来的源片）：失败渲染无权连带删掉
    mat_dir = storage.workspace.dir_for("p:fail", "af", "material")
    (mat_dir / "keep-me.mp4").write_bytes(b"bytes localized back from MinIO for a rerun")
    try:
        await reg.get("render_video")(fail_state)
        check(False, "素材文件缺失时 render_video 必须抛错")
    except Exception as e:
        check(True, f"素材缺失时 render_video 外抛（{type(e).__name__}）")
        failed = await storage.render_jobs.get("p:fail", "af")
        check(failed["status"] == "failed" and failed["error"],
              f"失败写 render_jobs.status=failed + error（{str(failed['error'])[:60]}…）")
        check(failed["video_object_key"] is None, "失败任务不记 video_object_key（ready 不成立）")
        check(not await storage.render_jobs.ready("p:fail", "af"), "失败产物不满足 ready")
        check(not (ws / "p_fail" / "af" / "render").exists(),
              "失败路径当场回收自己写入的 render/ 目录")
        check((mat_dir / "keep-me.mp4").exists(),
              "失败只收 render/ 自己那份副本，不动 material/ 的中转字节（清了只是逼重跑重新下载）")
        check(not await fail_state.store.has("render_video"),
              "artifacts 表里没有失败产物的 render_video 行（不产引用死链的成功）")

    # ---------- ④b 音画同步闸：校准认不出的错位必须拦，不静默出片 ----------
    # ④ 那份是「字节取不到」；这份是「字节都在、嘴型对不上」：同源素材的画面被排在
    # 没有它自己声音的窗口里——resync 认不出（它只掰得动「有声音但对歪」的段）。
    # 没有这道闸，片子照出，回给用户的还是那句「严格音画同步」（真机报了三次）。
    av_state = NodeState(session_id="p:av", artifact_id="af_av",
                         user_request="口播画面排在静音窗口里",
                         store=await _open_store(storage, "p:av", "af_av"),
                         user_id=USER, conversation_id=CONV)
    tl = json.loads(json.dumps(tl))
    dur = float(tl["duration"])
    # 手写一份原声时间线：画面沿用它自己的素材，声音却只排前半段 →
    # 后半段的画面是「同源素材在播，却没有它自己的声音」，嘴在动而音已停。
    mis = {**tl, "mode": "original_audio",
           "audio_events": [{"path": tl["events"][0]["path"], "start": 0.0,
                             "end": round(dur / 2, 3), "src_start": 0.0,
                             "src_end": round(dur / 2, 3), "kind": "original"}]}
    check(any(e["path"] == tl["events"][0]["path"] and e["start"] >= dur / 2
              for e in mis["events"]),
          f"用例前提成立：有同源画面落在声音之外（{[(e['start'], e['path'].split('/')[-1]) for e in mis['events']]}）")
    await av_state.store.put("plan_timeline", {"timeline": mis})
    try:
        await reg.get("render_video")(av_state)
        check(False, "出镜画面落在没有它声音的窗口时，渲染闸必须打回")
    except ValueError as e:
        gate = await storage.render_jobs.get("p:av", "af_av")
        check("音画同步闸" in str(e) and "没有它自己的声音在播" in str(e),
              f"闸回的是可执行的逐条原因（{str(e)[:60]}…）")
        check(gate["status"] == "failed" and gate["video_object_key"] is None,
              "闸在 jobs.open 之后：失败留下 failed 行（界面/render_status 据此报告）")
        check(not await av_state.store.has("render_video"),
              "被闸拦下的渲染不写产物行（不出「有了」的假象）")

    # 同一产物重跑：状态复位为 running 后再次成功，仍只有一行
    retry = NodeState(session_id="p:fail", artifact_id="af", user_request="换成好素材重渲",
                      store=await _open_store(storage, "p:fail", "af"),
                      user_id=USER, conversation_id=CONV)
    await retry.store.put("plan_timeline", {"timeline": tl})
    packed2 = await reg.get("render_video")(retry)
    check(packed2["output"]["video"] == "renders/p_fail/af.mp4", "重跑产出新对象键（同产物名覆盖）")
    rows = await storage.db.select("render_jobs", where={"session_id": "p:fail"})
    check(len(rows) == 1 and rows[0]["status"] == "done",
          "复跑复位原行：render_jobs 仍只有一行且状态已回到 done")

    # 未给 artifact_id 的一次运行：两张表必须落在同一个归一后的作用域上。
    # 必须走拦截器——只有它把节点产物 persist 进 artifacts 表，直接调节点只会写内存镜像。
    anon = NodeState(session_id="p:anon", artifact_id="", user_request="没给产物名的那句话",
                     store=await _open_store(storage, "p:anon", ""),
                     user_id=USER, conversation_id=CONV)
    await prep_creative(itp, anon, [mat])
    packed3 = await itp.invoke("render_video", anon, material_ids=[mat])
    check(packed3["output"]["video"] == "renders/p_anon/_default.mp4",
          f"空产物名归一为 _default（spec §4 键面照写），不自造 final："
          f"{packed3['output']['video']}")
    ajob = await storage.render_jobs.get("p:anon", "_default")
    arows = await storage.db.select("artifacts", where={"session_id": "p:anon"})
    adict = {r["node"]: r["artifact_id"] for r in arows}
    check(ajob["status"] == "done" and {r["artifact_id"] for r in arows} == {"_default"},
          "同一次运行的 render_jobs 与 artifacts 用同一个 artifact_id（跨表可对账）"
          f"：render_jobs artifact_id={ajob.get('artifact_id')!r} status={ajob.get('status')!r}"
          f" artifacts={adict}")

    # ---------- ⑤ 工作区 TTL：只收过期 artifact 目录 ----------
    stale = ws / "p_old" / "art_old" / "render"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "x.mp4").write_bytes(b"stale")
    old = time.time() - settings.storage.workspace_ttl_sec - 60
    os.utime(ws / "p_old" / "art_old", (old, old))
    removed = storage.workspace.sweep_stale(settings.storage.workspace_ttl_sec)
    check(removed >= 1 and not (ws / "p_old").exists(), f"sweep_stale 收回过期目录（{removed} 个文件）")
    check((ws / "p_main" / "art1").exists(), "未过期的工作区目录不受影响")

    # ---------- ⑥ 内容缓存 LRU：容量上限逐出 + 内容寻址不重复占位 ----------
    tiny_cache = tmp / "tiny_cache"
    size1 = (raw / "src.mp4").stat().st_size
    other = tmp / "other.mp4"
    make_source(other, ["color=c=green:size=320x240:rate=25:duration=1"], 200)
    size2 = len(other.read_bytes())
    lru = build_storage("memory", cache_root=tiny_cache, workspace_root=tmp / "tiny_ws",
                        cache_max_gb=(size1 + 1) / 1024 ** 3)
    k1 = await _put_file(lru, "users/u_pipe/convs/c_pipe/k1.mp4", raw / "src.mp4")
    k2 = await _put_file(lru, "users/u_pipe/convs/c_pipe/k2.mp4", other)

    def cached_count():
        d = tiny_cache / "objects"
        return len([p for p in d.rglob("*") if p.is_file()]) if d.is_dir() else 0

    a = await lru.objects.localize(k1, tmp / "d1")
    check(a.exists() and cached_count() == 1, f"localize 落真实文件并写缓存（{size1}B）")
    await lru.objects.localize(k1, tmp / "d2")
    check(cached_count() == 1, "同一内容再取不回源、不占两份（sha256 内容寻址）")
    await lru.objects.localize(k2, tmp / "d3")
    left = sorted(p.name for p in (tiny_cache / "objects").rglob("*") if p.is_file())
    check(left == [(await lru.objects.head(k2)).sha256],
          f"超出容量按最后使用时间逐出：只剩新对象 {size2}B，最久未用的 {size1}B 已被挤掉")

    # ---------- ⑦ 编码期进度 + 字幕扁平化 ----------
    # 真机那次「卡在 70%」不是死锁而是慢：编码阶段一条进度都不回报，观测面上
    # 「慢」和「死」长得一模一样，看门狗也就无从判停滞。这两条钉住回报通路本身。
    trace: list[tuple[str, int]] = []
    enc_dst = fb_dir / "progress.mp4"
    await asyncio.to_thread(_render_with_moviepy, tl_local, enc_dst, settings,
                            lambda s, p: trace.append((s, p)))
    enc = [p for (s, p) in trace if s == "encoding"]
    check(enc_dst.exists() and enc_dst.stat().st_size > 5_000,
          "直接调渲染体也出片（进度回报不改变成片语义）")
    check(len(set(enc)) >= 2 and max(enc) > 70,
          f"编码阶段百分比在推进，不是钉死一个数：{sorted(set(enc))}")
    check(all(b >= a for a, b in zip(enc, enc[1:])), "编码百分比只增不减")
    check(enc[0] == 70 and max(enc) <= 98,
          f"编码段占 70→98，100 留给 succeed 那一步（首 {enc[0]}，尾 {max(enc)}）")
    check([s for (s, _) in trace] == ["video_track", "subtitles", "audio",
                                      *["encoding"] * len(enc)],
          f"阶段顺序不变：{[s for (s, _) in trace]}")
    subs = [s for s in tl_local.get("subtitles", []) if s.get("text")]
    layers = _subtitle_layers(tl_local, (tl_local["width"], tl_local["height"]), settings)
    check(len(layers) == len(subs),
          f"字幕一条一层、与画面层并列，不再逐条嵌套合成（{len(layers)}/{len(subs)}）")

    print("\n=== ⑧ 覆盖层：两条渲染路径都要真的画出像素 ===")
    # 只验「时间线里有这一层」不够——覆盖层最容易悄悄不生效（层没进合成、
    # enable 窗口算错、fit 分支抛异常被吞掉，片子照样出）。这里拿一块纯绿素材
    # 盖在中间一秒上，抽帧看那一眼看到的是不是绿，两条路径各验一次。
    ov_src = raw / "green_overlay.mp4"
    make_source(ov_src, ["color=c=green:size=640x360:rate=25:duration=2"], 900)
    tl_dur = float(tl_local["duration"])
    mid = round(tl_dur / 2, 3)
    ov_tl = {**tl_local, "overlay_events": [
        {"path": str(ov_src), "start": mid, "end": round(mid + 1.0, 3),
         "src_start": 0.0, "src_end": 1.0, "fit": "cover"}]}

    def _rgb_at(video: Path, at: float) -> tuple[int, int, int]:
        png = video.with_name(f"{video.stem}_g{at:.2f}.png")
        mediaops.ffmpeg("-v", "error", "-ss", f"{at:.3f}", "-i", str(video),
                        "-frames:v", "1", str(png))
        from PIL import Image  # noqa: PLC0415
        px = list(Image.open(png).convert("RGB").resize((16, 16)).getdata())
        png.unlink(missing_ok=True)
        return tuple(sum(c[i] for c in px) // len(px) for i in range(3))

    def _is_green(rgb) -> bool:
        return rgb[1] > 120 and rgb[1] > rgb[0] + 40 and rgb[1] > rgb[2] + 40

    built = _overlay_layers(ov_tl, (int(ov_tl["width"]), int(ov_tl["height"])), [])
    check(len(built) == 1, f"覆盖层进得了合成层列表，没被兜底 except 吞掉（{len(built)} 层）")

    for label, runner in (("moviepy", lambda src, dst: _render_with_moviepy(
                               src, dst, settings, lambda *a: None)),
                          ("ffmpeg", lambda src, dst: _render_via_ffmpeg(src, dst))):
        with_ov = fb_dir / f"overlay_{label}.mp4"
        await asyncio.to_thread(runner, ov_tl, with_ov)
        check(with_ov.exists() and with_ov.stat().st_size > 5_000,
              f"{label} 路径带覆盖层也能出片")
        rgb_in = _rgb_at(with_ov, mid + 0.5)
        rgb_out = _rgb_at(with_ov, max(0.1, mid - 0.8))
        check(_is_green(rgb_in), f"{label}：盖层窗口内是那块绿（RGB{rgb_in}）")
        check(not _is_green(rgb_out),
              f"{label}：窗口外仍是主轨（RGB{rgb_out}）——覆盖层不改别处的画面")
        check(mediaops.probe(with_ov)["has_audio"],
              f"{label}：盖层不带声音，音轨仍在（声音没被覆盖层抢走）")

    trace_ov: list[tuple[str, int]] = []
    await asyncio.to_thread(_render_via_ffmpeg, ov_tl, fb_dir / "ov_trace.mp4",
                            lambda s, p: trace_ov.append((s, p)))
    percents = [p for _, p in trace_ov]
    check(all(b >= a for a, b in zip(percents, percents[1:])),
          f"兜底路径带覆盖层时进度仍只增不减：{trace_ov}")
    sliced = _slice_timeline({**tl_local, "overlay_events": ov_tl["overlay_events"]},
                             mid, mid + 1.0)
    check(len(sliced.get("overlay_events") or []) == 1
          and abs(sliced["overlay_events"][0]["start"]) < 0.01,
          f"分段渲染前切片带上覆盖层：{sliced['overlay_events'][0]}")

    print("\n=== ⑨ 节点级入口：模型只写 asr 段 id，秒数由 render_video 算 ===")
    # ⑧ 验的是渲染体（本机路径、手写的秒）。这一节验的是模型走的那条路：
    # 它只会被要求写 segments: ["asr-1"]，秒数由 resolve_overlay_anchors 按成片算。
    # 锚错 id 必须当场报错并留 failed 行——否则就是「用户说盖了、其实没盖」。
    node_src = raw / "flat_speaker.mp4"
    make_source(node_src, ["color=c=blue:size=640x360:rate=25:duration=3"], 440)
    mat_node = await seed(storage, node_src)
    # 两句之间留 0.7 秒空隙：小于 0.5 秒 speech_rough_cut 会把它们并成一段，
    # 那时「锚第二句」等于盖满全片，窗口外那条断言就失去意义了。
    speech = [{"text": "第一句话在这里", "start": 0.2, "end": 1.2},
              {"text": "第二句话在这里", "start": 1.9, "end": 2.7}]
    reg_ov = build_real_registry(settings, fake_providers(
        transcribe=lambda wav: list(speech)), storage)
    itp_ov = Interceptor(reg_ov)
    ov_key = f"users/{USER}/convs/{CONV}/green_node.mp4"
    await _put_file(storage, ov_key, ov_src)
    ov_state = NodeState(session_id="p:ov", artifact_id="af_ov",
                         user_request="讲到第二句时换成空镜，口播声音不动",
                         store=await _open_store(storage, "p:ov", "af_ov"),
                         user_id=USER, conversation_id=CONV)
    await prep_creative(itp_ov, ov_state, [mat_node])
    ov_params = {"keep_original_audio": True, "material_ids": [mat_node]}

    try:
        await itp_ov.invoke("render_video", ov_state,
                            overlay_events=[{"segments": ["asr-99"], "path": to_ref(ov_key)}],
                            **ov_params)
        check(False, "锚点 id 不存在时 render_video 必须打回")
    except ValueError as e:
        check("锚点认不出来" in str(e) and "asr-0" in str(e),
              f"报错直接列出真实存在的 id：{str(e).replace(chr(10), ' / ')[:120]}")
        bad_job = await storage.render_jobs.get("p:ov", "af_ov")
        check(bad_job["status"] == "failed" and "锚点" in str(bad_job["error"]),
              f"坏锚点留下 failed 行，界面据此报告（{bad_job['status']}）")

    ids = [s["id"] for s in (await ov_state.store.get("asr"))["asr_segments"]]
    check(ids == ["asr-0", "asr-1"],
          f"asr 段自带稳定 id，覆盖层就是锚它（{ids}）")
    packed_ov = await itp_ov.invoke(
        "render_video", ov_state,
        overlay_events=[{"segments": [ids[1]], "path": to_ref(ov_key)}], **ov_params)
    out_ov = packed_ov["output"]
    tl_ov = (await ov_state.store.get("plan_timeline"))["timeline"]
    a1 = tl_ov["audio_events"][1]
    check((out_ov.get("timeline_digest") or {}).get("overlay_segments") == 1,
          f"指纹记下了这一层：{out_ov.get('timeline_digest')}")
    ov_local = await storage.objects.localize(out_ov["video"], tmp / "ov_client")
    inside_rgb = _rgb_at(ov_local, (float(a1["start"]) + float(a1["end"])) / 2)
    outside_rgb = _rgb_at(ov_local, 0.4)
    check(_is_green(inside_rgb),
          f"锚到第二句话，那句话正播时画面是绿的（RGB{inside_rgb}）")
    check(not _is_green(outside_rgb),
          f"第一句话仍归主轨（RGB{outside_rgb}）——秒数按成片算，不是全片盖满")
    check(mediaops.probe(ov_local)["has_audio"],
          "口播声音没被覆盖层抢走（原声仍在）")
    done_job = await storage.render_jobs.get("p:ov", "af_ov")
    check(done_job["status"] == "done", "同一次运行里失败→改对→成功，行状态跟着复位")

    print("\n=== ⑩ dry_run：先回出片计划，不烧像素 ===")
    # 「改了编排 → 渲 → 十分钟 → 被闸拦下 / 或者根本不是我要的那版」这条循环太贵。
    # dry_run 把渲染前的每一道校验原样跑一遍（同一批判定函数），只回账。
    dry_state = NodeState(session_id="p:dry", artifact_id="af_dry",
                          user_request="先告诉我这版会渲成什么样",
                          store=await _open_store(storage, "p:dry", "af_dry"),
                          user_id=USER, conversation_id=CONV)
    await prep_creative(itp, dry_state, [mat])
    packed_dry = await itp.invoke("render_video", dry_state, material_ids=[mat],
                                  dry_run=True)
    dry = packed_dry["output"]
    check(dry.get("dry_run") is True and dry.get("will_render") is True,
          f"好编排：dry_run 说渲得出来（blocking={dry['blocking']}）")
    check("video" not in dry and "media_url" not in dry,
          "回的是账，不是成片引用")
    plan = dry["plan"]
    tl_dry = (await dry_state.store.get("plan_timeline"))["timeline"]
    check(plan["picture"]["segments"] == len(tl_dry["events"])
          and plan["voice"]["segments"] == len(tl_dry["audio_events"])
          and abs(plan["duration"] - float(tl_dry["duration"])) < 0.01,
          f"账对得上这份时间线：{plan['duration']}s / 画面 {plan['picture']['segments']} 段"
          f" / 声音 {plan['voice']['segments']} 段")
    check(dry["timeline_digest"]["video_segments"] == plan["picture"]["segments"]
          and dry["timeline_digest"]["audio_segments"] == plan["voice"]["segments"]
          and dry["timeline_digest"]["overlay_segments"] == len(plan["overlays"]),
          "指纹与账同源（同一份时间线的两种说法，不会各算各的）")
    check(await storage.render_jobs.get("p:dry", "af_dry") is None,
          "dry_run 不开渲染任务行（留下一行 running 会被看门狗判停滞、被界面当成正在出片）")
    check(not await dry_state.store.has("render_video"),
          "dry_run 不写 artifacts：它不是一次渲染产物，写了就是「这步已有产出」的假象")
    check(not (ws / "p_dry" / "af_dry" / "render").exists(),
          "dry_run 不建 render/ 目录：既不取字节也不写字节")
    check(len(dry["not_checked"]) >= 3,
          f"dry_run 如实交代自己看不到的（{len(dry['not_checked'])} 条）")

    # 会被闸拦下的那份：dry_run 现在就把它报出来，而不是等十分钟
    mis2 = json.loads(json.dumps(tl))
    dur2 = float(mis2["duration"])
    mis2["mode"] = "original_audio"
    mis2["audio_events"] = [{"path": mis2["events"][0]["path"], "start": 0.0,
                             "end": round(dur2 / 2, 3), "src_start": 0.0,
                             "src_end": round(dur2 / 2, 3), "kind": "original"}]
    bad_dry = (await itp.invoke("render_video", dry_state, dry_run=True,
                                timeline=mis2))["output"]
    check(bad_dry["will_render"] is False and bad_dry["blocking"],
          f"坏编排：dry_run 当场说渲不出来（{str(bad_dry['blocking'][0])[:50]}…）")
    check(await storage.render_jobs.get("p:dry", "af_dry") is None,
          "被 dry_run 判死的编排同样不写渲染任务行")
    try:
        await itp.invoke("render_video", dry_state, dry_run=True,
                         overlay_events=[{"segments": ["asr-777"],
                                          "path": to_ref(ov_key)}])
        check(False, "dry_run 里锚错 id 同样要当场报错")
    except ValueError as e:
        check("锚点认不出来" in str(e), f"dry_run 也跑锚点解析（{str(e).splitlines()[0]}）")
        check(await storage.render_jobs.get("p:dry", "af_dry") is None,
              "dry_run 的失败不留下 failed 行（压根没开过任务）")
    # 关掉 dry_run：同一份编排真的出片，指纹与 dry_run 说的一致
    real_after_dry = (await itp.invoke("render_video", dry_state,
                                       material_ids=[mat]))["output"]
    check(real_after_dry["timeline_digest"]["sha16"] == dry["timeline_digest"]["sha16"],
          "按 dry_run 的账去掉开关再渲，成片指纹一致（不会「看到一版、渲出另一版」）")

    print("\n=== ⑪ 证据分级：账本在真链路上的落点 ===")
    # 「渲染完成」这四个字底下混着三种东西：算出来的、量过字节/像素的、机器验不了的。
    # 这里钉的是「没做的那一步不许自称做过」——dry_run 没下字节，它那本账里
    # byte/frame 就必须是 UNVERIFIED；真渲之后同一类条目要升上来，而听感那条永远不升。
    dry_led = dry["evidence"]
    check(dry_led and all(e["status"] == "UNVERIFIED" for e in dry_led
                          if e["level"] in ("byte", "frame")),
          f"dry_run 的账：byte/frame {sum(1 for e in dry_led if e['level'] in ('byte', 'frame'))} "
          "条全是 UNVERIFIED（它压根没下载也没编码）")
    check(any(e["level"] == "machine" and e["status"] == "verified" for e in dry_led),
          "同一本账里 machine 级是验过的——分级不是「全都算未验」的挡箭牌")
    check(dry.get("evidence_rule"), "账本随附引用规则：只有 verified 的能对用户声称验过")

    ov_led = out_ov["evidence"]
    by_level: dict[str, list[dict]] = {}
    for e in ov_led:
        by_level.setdefault(e["level"], []).append(e)
    check(all(e["status"] == "verified" for e in by_level.get("byte", [])),
          f"真渲之后 byte 级条目升为 verified（{len(by_level.get('byte', []))} 条，ffprobe 量的）")
    check(any(e["status"] == "verified" for e in by_level.get("frame", [])),
          f"抽帧真跑了：frame 级有 verified 条目（{[e['claim'][:14] for e in by_level.get('frame', [])]}）")
    check(all(e["status"] == "UNVERIFIED" for e in by_level.get("listening", [])),
          "听感那条在成片产物里仍是 UNVERIFIED（机器没有听觉）")
    check(any("对不对题" in e["claim"] and e["status"] == "UNVERIFIED" for e in ov_led),
          "「盖上去的是不是你要那张」如实标未验——只有用户这一眼知道")
    job_ov = await storage.render_jobs.get("p:ov", "af_ov")
    check((job_ov.get("result") or {}).get("evidence"),
          "整本账进了 render_jobs.result：轮询端与历史重放拼得出的同一份")
    leftover = list((ws / "p_ov" / "af_ov" / "render").rglob("*.gray"))
    check(not leftover, f"抽帧的灰度裸流读完即删（工作区残留 {len(leftover)} 个）")

    print()
    print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


async def _open_store(storage, sid: str, artifact: str):
    return await ArtifactStore.open(storage.artifacts(sid, artifact), sid, artifact)


async def _put_file(storage, key: str, src: Path) -> str:
    blob = src.read_bytes()

    async def chunks():
        yield blob

    await storage.objects.put(key, chunks(), content_type=mime_of(src.name))
    return key


if __name__ == "__main__":
    asyncio.run(main())
