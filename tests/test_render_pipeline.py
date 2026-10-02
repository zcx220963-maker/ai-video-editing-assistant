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
    _render_via_ffmpeg,
    _render_with_moviepy,
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


def fake_providers():
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
                           transcribe=lambda wav: [], llm=llm, tts=tts)


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
