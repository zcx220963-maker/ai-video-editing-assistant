# -*- coding: utf-8 -*-
"""Storyline 真实节点离线测试：ffmpeg 合成素材 + fake providers，全 DAG 真渲染出片。

不联网：faster-whisper / VL / edge-tts / 文案 LLM 全部注入 fake（或注入必然失败
的 provider 验证降级路径）。存储层注入 memory 替身（D3）：素材一律走「字节进
ObjectStore + 一行 materials」，节点只认 material_id，不再有扫盘路径。成片同样
只认对象键（renders/{会话}/{产物}.mp4）+ presigned 直链，节点产物落 artifacts
表、渲染进度落 render_jobs 表。断言真实 mp4 可从对象存储取回、payload 契约键、
场景切分、ASR→粗剪链路、三种时间线结构与渲染优先级选择、越权跳过、BGM 查曲库、
外部能力「报错」与「卡死不返回」两种故障形态下的降级，以及**落进 artifacts 表的
每行 payload 都只带 `obj:` 文件引用、不含工作区绝对路径**。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import re
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, Interceptor, NodeState
from agent_framework.storage import build_storage, new_id, ref_key, to_ref
from agent_framework.storage.media import kind_of, mime_of, probe as probe_meta
from storyline_server import mediaops
from storyline_server.nodes.core_nodes import (
    _parse_caption_list,

    build_real_registry,
    load_templates,
)
from storyline_server.providers import (
    ProviderError,
    _filter_asr_hallucinations,
    build_providers,
)
from storyline_server.settings import Settings

FAILS = 0

USER = "u_test"
CONV = "c_test"


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def _leaked_paths(rows, ws_root: Path) -> list[str]:
    """产物行里仍在写本机绝对路径的段（节点名: 段）——改写后应当一条都没有。

    判据不只看工作区根：任何 `C:/…` 形态都是同一类病——把「这台机器的哪个位置」
    写进了跨机器、跨重启存在的库。presigned 直链里的 `://` 不算（那是对象存储的址）。
    """
    root = str(Path(ws_root).resolve()).replace("\\", "/").lower()
    bad: list[str] = []
    for r in rows:
        for seg in re.findall(r'"([^"]*)"', json.dumps(r["payload"], ensure_ascii=False)):
            t = seg.replace("\\", "/").lower()
            if root in t or re.match(r"^[a-z]:/", t):
                bad.append(f"{r['node']}: {seg}")
    return bad


async def refs_have_bytes(storage, refs) -> bool:
    """一组引用是否都能在对象存储里 head 出非零字节（渲染层据此认账）。"""
    heads = await asyncio.gather(*(storage.objects.head(ref_key(r)) for r in refs))
    return bool(refs) and all(h is not None and h.bytes > 0 for h in heads)


async def prep_creative(itp, state, material_ids, *, need_pro=False,
                        highlight_keep=None, highlight_off=False):
    """模拟 LLM 显式调用创意决策节点（filter_clips/group_clips/script_template_rec 等）。

    端到端测试里 Interceptor 不会自动调 require_explicit_call=True 的节点，
    所以得像 LLM 一样先调这些节点传参数，再调 render_video。
    """
    state.flags["material_ids"] = material_ids
    if highlight_off:
        state.flags["highlight"] = False
    await itp.invoke("understand_clips", state)
    caps = (await state.store.get("understand_clips"))["clip_captions"]
    all_clips = [c["clip"] for c in caps]
    await itp.invoke("filter_clips", state, keep_clips=all_clips)
    if need_pro and len(all_clips) >= 2:
        mid = len(all_clips) // 2
        groups = [
            {"group_id": "g1", "clips": all_clips[:mid], "summary": "前半段"},
            {"group_id": "g2", "clips": all_clips[mid:], "summary": "后半段"},
        ]
    else:
        groups = [{"group_id": "g1", "clips": all_clips, "summary": "全部片段"}]
    await itp.invoke("group_clips", state, custom_groups=groups)
    await itp.invoke("script_template_rec", state, template_id="tpl_vlog_3act")
    if need_pro:
        gps = (await state.store.get("group_clips"))["groups"]
        await itp.invoke("transition_rec", state, custom_transitions=[
            {"group_id": g["group_id"], "style": "fade"} for g in gps])
        await itp.invoke("text_rec", state, custom_styles=[
            {"group_id": g["group_id"], "style": "normal"} for g in gps])
    if highlight_keep is not None:
        await itp.invoke("speech_rough_cut", state, keep_segments=highlight_keep)


def make_source(dst: Path, parts: list[str], audio_freq: int) -> None:
    """lavfi 合成多场景测试片：不同画面源 concat，含明确场景切换 + 正弦音轨。"""
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


def write_config(tmp: Path, suffix: str = "") -> Settings:
    """TOML 只留存储后端与工作区目录：[media] 四目录已退役（spec §8），素材库在 materials 表。"""
    cfg = tmp / f"config{suffix}.toml"
    cfg.write_text(
        f"""
[local_mcp_server]
server_name = "storyline"
port = 8099

[storage]
backend = "memory"
workspace_root = "{(tmp / f'workspace{suffix}').as_posix()}"
cache_root = "{(tmp / f'object_cache{suffix}').as_posix()}"

[capabilities]
asr_model = "tiny"
scene_threshold = 0.25
""",
        encoding="utf-8")
    return Settings.load(cfg)


def make_storage(settings: Settings):
    return build_storage(
        settings.storage.backend,
        cache_root=settings.storage.cache_root,
        workspace_root=settings.storage.workspace_root,
        cache_max_gb=settings.storage.workspace_max_gb)


async def seed(storage, src: Path, *, owner: str = USER, conv: str = CONV,
               origin: str = "library", filename: str | None = None) -> str:
    """等价于一次 /upload：字节直写对象存储 + materials 登记，返回 material_id。"""
    mid = new_id("mat", 6)
    key = f"users/{owner}/convs/{conv}/{mid}{src.suffix}"
    blob = src.read_bytes()

    async def chunks():
        yield blob

    info = await storage.objects.put(key, chunks(), content_type=mime_of(src.name))
    meta = await probe_meta(src)
    await storage.users.provision(owner)
    await storage.conversations.ensure(owner, conv)
    await storage.materials.register(
        owner, conv, key, filename or src.name, kind_of(src.name) or "video",
        bytes_=info.bytes, sha256=info.sha256, mime=mime_of(src.name),
        duration_sec=meta["duration_sec"] or None, width=meta["width"] or None,
        height=meta["height"] or None, has_audio=meta["has_audio"],
        origin=origin, material_id=mid)
    return mid


def good_providers(tmp: Path):
    async def llm(messages):
        return json.dumps({
            "title": "三场景测试片",
            "groups": [{"group_id": "group_0001",
                        "raw_text": "大家好，今天看三个场景。中间的条纹最抢眼。最后安静收尾。"}],
        }, ensure_ascii=False)

    async def tts(text, dst):
        mediaops.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "2.0", str(dst))
        return Path(dst)

    return build_providers(
        Settings().caps,
        vision=lambda images, prompt, **kw: "一个彩色测试画面，出现条纹与纯色块切换",
        transcribe=lambda wav: [
            {"start": 0.5, "end": 2.2, "text": "大家好，欢迎观看"},
            {"start": 3.0, "end": 5.4, "text": "今天展示三个场景切换"},
        ],
        llm=llm, tts=tts)


def dead_providers():
    async def bad_llm(messages):
        raise ProviderError("offline")

    async def bad_tts(text, dst):
        raise ProviderError("offline")

    return build_providers(
        Settings().caps,
        vision=lambda images, prompt, **kw: (_ for _ in ()).throw(ProviderError("offline")),
        transcribe=lambda wav: (_ for _ in ()).throw(ProviderError("offline")),
        llm=bad_llm, tts=bad_tts)


def hung_providers(timeout_sec: float = 0.3, hang_sec: float = 3.0):
    """外部能力不报错但永不返回（真实故障：faster-whisper 解码挂死、模型下载卡住）。

    节点必须靠 ``[capabilities].provider_timeout_sec`` 自己断开，否则整条剪辑链无限等待。
    """
    caps = replace(Settings().caps, provider_timeout_sec=timeout_sec,
                   asr_timeout_base_sec=0.0, asr_timeout_per_audio_sec=0.0)

    def hang_asr(wav):
        time.sleep(hang_sec)
        return []

    async def hang_llm(messages):
        await asyncio.sleep(hang_sec)
        return ""

    async def hang_tts(text, dst):
        await asyncio.sleep(hang_sec)
        return Path(dst)

    return build_providers(caps, vision=lambda images, prompt, **kw: "一个彩色测试画面",
                           transcribe=hang_asr, llm=hang_llm, tts=hang_tts)


def make_silent(dst: Path, parts: list[str]) -> None:
    """无音轨的纯画面测试片（模拟空镜素材）。"""
    inputs, labels = [], ""
    for i, graph in enumerate(parts):
        inputs += ["-f", "lavfi", "-i", graph]
        labels += f"[{i}:v]"
    mediaops.ffmpeg(*inputs, "-filter_complex",
                    f"{labels}concat=n={len(parts)}:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", str(dst))


def speech_providers():
    """transcribe 按 wav 文件名区分素材：只有 speech(m0) 返回口播段。"""
    async def llm(messages):
        return json.dumps({"title": "励志演讲混剪", "groups": []}, ensure_ascii=False)

    def transcribe(wav):
        if Path(wav).stem == "m0":
            return [{"start": 0.5, "end": 2.2, "text": "你们一定要相信"},
                    {"start": 3.0, "end": 5.4, "text": "坚持到底就会看到光"}]
        return []

    return build_providers(Settings().caps,
                           vision=lambda images, prompt, **kw: "彩色测试画面",
                           transcribe=transcribe, llm=llm)


async def fresh_state(storage, sid: str, artifact: str, request: str, *,
                      user_id: str = USER, conv_id: str = CONV) -> NodeState:
    """等价于服务端一次请求：Store 从 artifacts 表重建，节点只认 material_id。"""
    store = await ArtifactStore.open(storage.artifacts(sid, artifact), sid, artifact)
    return NodeState(session_id=sid, artifact_id=artifact, user_request=request,
                     store=store, user_id=user_id, conversation_id=conv_id)


def case_unit_helpers() -> None:
    """B/D 的纯函数单测：VL 批量回答解析 + ASR 幻觉段过滤。"""
    print("\n[单元：_parse_caption_list（B）与 _filter_asr_hallucinations（D）]")
    check(_parse_caption_list('["甲画面", "乙画面"]', 2) == ["甲画面", "乙画面"],
          "JSON 数组按序解析")
    check(_parse_caption_list('["甲画面", "乙画面"]', 3)
          == ['["甲画面", "乙画面"]'] * 3,
          "JSON 条数不足 → 同样走整段复用兜底（不降占位）")
    check(_parse_caption_list("一段普通描述", 4) == ["一段普通描述"] * 4,
          "非 JSON 回答整段复用到每帧（不降占位）")
    check(_parse_caption_list("   ", 2) is None, "空回答 → None（降级占位）")

    segs = [
        {"start": 0.0, "end": 1.0, "text": "正常的一句话"},
        {"start": 1.0, "end": 2.0, "text": "谢谢观看"},                 # 黑名单幻觉
        {"start": 2.0, "end": 3.0, "text": "@@@ #### <<<< >>>"},        # 乱码
        {"start": 3.0, "end": 4.0, "text": "字幕由 Amara.org 提供 resonable"},
        {"start": 4.0, "end": 5.0, "text": "重复段"},
        {"start": 5.0, "end": 6.0, "text": "重复段"},
        {"start": 6.0, "end": 7.0, "text": "重复段"},
        {"start": 7.0, "end": 8.0, "text": "重复段"},                   # 复读第 4 次
    ]
    got = _filter_asr_hallucinations(segs)
    texts = [g["text"] for g in got]
    check("谢谢观看" not in texts and not any("@@@" in t for t in texts),
          "黑名单句与乱码段被丢弃")
    check(not any("字幕由" in t for t in texts), "字幕占位句被丢弃")
    check(texts.count("重复段") == 3, f"连续复读只保留前 3 次（{texts.count('重复段')}）")
    check(texts[0] == "正常的一句话", "正常段原样保留")



async def main() -> None:
    case_unit_helpers()
    tmp = Path(tempfile.mkdtemp(prefix="storyline_test_"))
    settings = write_config(tmp)
    raw = tmp / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    make_source(raw / "src_a.mp4", [
        "testsrc2=size=640x360:rate=25:duration=2",
        "smptebars=size=640x360:rate=25:duration=2",
        "color=c=blue:size=640x360:rate=25:duration=2",
    ], 440)
    make_source(raw / "src_b.mp4", [
        "color=c=green:size=640x360:rate=25:duration=2",
        "color=c=red:size=640x360:rate=25:duration=2",
    ], 300)

    storage = make_storage(settings)
    mat_a = await seed(storage, raw / "src_a.mp4")
    mat_b = await seed(storage, raw / "src_b.mp4")
    # 他人素材：混进 material_ids 也只能被跳过，不能进片
    mat_foreign = await seed(storage, raw / "src_b.mp4",
                             owner="u_other", conv="c_other", filename="别人的.mp4")
    check(len(storage.objects.data) == 3 and mat_a != mat_b,
          "素材进对象存储 + materials 表（已无本地素材库目录）")

    templates = load_templates()
    check(len(templates) == 4 and templates[0]["id"] == "tpl_vlog_3act",
          "脚本模板库加载（4 套起承转合骨架）")

    # ---------- 运行 1：主链路 search→…→render（fake 能力 + 真 ffmpeg/MoviePy） ----------
    settings0 = settings
    reg = build_real_registry(settings0, good_providers(tmp), storage)
    itp = Interceptor(reg)

    s_search = await fresh_state(storage, "t:search", "a0", "找一段条纹测试素材")
    res = await itp.invoke("search_media", s_search, query="src")
    found = res["output"]["found"]
    check(res["output"]["source"] == "materials"
          and {m["material_id"] for m in found} == {mat_a, mat_b}
          and all("path" not in m for m in found),
          "search_media 查 materials 表命中 2 条，只回 material_id")

    state = await fresh_state(storage, "t:main", "art1", "把素材剪成一条vlog")
    await prep_creative(itp, state, [mat_a, mat_b, mat_foreign])
    packed = await itp.invoke("render_video", state,
                              material_ids=[mat_a, mat_b, mat_foreign])
    payload = packed["output"]
    object_key = payload["video"]
    check(object_key == "renders/t_main/art1.mp4", "成片只认对象键 renders/{会话}/{产物}.mp4")
    dl = tmp / "downloaded"
    video = await storage.objects.localize(object_key, dl)
    check(video.exists() and video.stat().st_size > 10_000,
          "render_video 出片：字节在对象存储、可完整取回")
    check("://" in payload["media_url"], "media_url 是 presigned 直链，供前端播放卡片")
    check(payload["duration"] >= 4.0, f"成片时长合理（{payload['duration']}s）")
    check(payload["title"] == "三场景测试片", "LLM 标题进入成片契约")
    head = await storage.objects.head(object_key)
    check(head is not None and head.bytes == video.stat().st_size,
          "对象元数据齐备（head 可读、字节数一致）")

    for nm in ("load_media", "split_shots", "understand_clips", "filter_clips",
               "group_clips", "script_template_rec", "generate_script",
               "generate_voiceover", "select_BGM", "plan_timeline", "render_video"):
        check(nm in itp.order_trace, f"拦截器递归补齐并执行 {nm}")

    lm = await state.store.get("load_media")
    check({m["material_id"] for m in lm["media"]} == {mat_a, mat_b},
          "load_media 按 material_ids 从对象取回素材（拦截器经 state.flags 传入参）")
    check(lm["skipped_unauthorized"] == [mat_foreign],
          "越权 material_id 只跳过并回报，不毁整链")
    check({m["object_key"] for m in lm["media"]} == {
              f"users/{USER}/convs/{CONV}/{mat_a}.mp4",
              f"users/{USER}/convs/{CONV}/{mat_b}.mp4"}
          and {m["filename"] for m in lm["media"]} == {"src_a.mp4", "src_b.mp4"},
          "media 带 object_key/filename，对象键末段就是 material_id")

    shots = (await state.store.get("split_shots"))["clips"]
    check(len(shots) >= 3, f"场景检测切出多镜头（{len(shots)} shots）")
    check(all(s["end"] - s["start"] >= settings0.caps.min_shot_sec - 1e-6 for s in shots),
          "镜头不短于 min_shot_sec")
    caps0 = (await state.store.get("understand_clips"))["clip_captions"]
    check(len(caps0) == len(shots) and all(c["caption"] for c in caps0),
          "每个镜头都有 caption（VL fake 注入）")
    uc0 = await state.store.get("understand_clips")
    check(uc0.get("vl_requests", 0) * settings0.caps.vl_frames_per_batch >= len(shots)
          and uc0["vl_requests"] < len(shots),
          f"VL 批量请求：{len(shots)} 镜头只发 {uc0['vl_requests']} 次（B）")
    fc0 = await state.store.get("filter_clips")
    check(fc0.get("source") == "llm_keep_clips" and fc0["clips"],
          f"filter_clips 用 LLM 传的 keep_clips 过滤（F）: source={fc0.get('source')}")
    tl = (await state.store.get("plan_timeline"))["timeline"]
    check(tl["width"] == 640 and tl["height"] == 360, "时间线画布取素材真实分辨率")
    check(len(tl["events"]) >= 3 and tl["subtitles"], "时间线含多事件轨 + 字幕")
    check(tl["bgm"] and ref_key(tl["bgm"]["path"])
          and await refs_have_bytes(storage, [tl["bgm"]["path"]]),
          "占位 BGM 以对象引用进时间线，字节在对象存储里")
    vo = (await state.store.get("generate_voiceover"))["voiceover"]
    check(await refs_have_bytes(storage, [v["path"] for v in vo]),
          "配音全部进对象存储，payload 里只有引用")
    rows = await storage.artifacts("t:main", "art1").rows()
    check(rows and not _leaked_paths(rows, storage.workspace.root),
          f"整链持久化 payload 里没有工作区绝对路径（{len(rows)} 行产物）")

    row = await storage.db.get_by_pk("artifacts", {"session_id": "t:main",
                                                   "node": "generate_script",
                                                   "artifact_id": "art1"})
    check(row is not None and row["payload"] and row["artifact_id"] == "art1",
          "节点产物落 artifacts 表（session/node/artifact 三元主键）")
    reopened = await ArtifactStore.open(storage.artifacts("t:main", "art1"), "t:main", "art1")
    check(await reopened.get("generate_script") == await state.store.get("generate_script"),
          "新建 Store 从 artifacts 表读回同一份产物（跨请求/重启衔接）")
    job = await storage.render_jobs.get("t:main", "art1")
    check(job["status"] == "done" and job["stage"] == "done" and job["percent"] == 100
          and job["video_object_key"] == object_key,
          "渲染进度写 render_jobs 表 done/100（取代本地探针文件）")

    # ---------- ASR → 语音粗剪支线 ----------
    await itp.invoke("speech_rough_cut", state)
    asr = await state.store.get("asr")
    rough = (await state.store.get("speech_rough_cut"))["rough_clips"]
    check(asr["asr_segments"], "ASR 转写段产出（fake whisper）")
    check(len(rough) == 4 and all(len({r["clip"] for r in rough}) == 2 for _ in [0])
          and rough[0]["text"].startswith("大家好"),
          "粗剪保留口播区间（两段间隔>0.5s 不合并）")

    # ---------- 专业版 / AI 转场版时间线（渲染优先级：ai > pro > base） ----------
    await prep_creative(itp, state, [mat_a, mat_b, mat_foreign], need_pro=True)
    await itp.invoke("plan_timeline_pro", state)
    await itp.invoke("generate_ai_transition", state)
    await itp.invoke("plan_timeline_ai_transition", state)
    tlp = (await state.store.get("plan_timeline_pro"))["timeline_pro"]
    for key, fld in (("plan_timeline", "timeline"), ("plan_timeline_pro", "timeline_pro"),
                     ("plan_timeline_ai_transition", "timeline_ai")):
        got = await state.store.get(key)
        check(isinstance(got, dict) and got[fld], f"{key} 产出 {fld}")
    check(any(t for t in tlp["transition_styles"] if t), "专业版并入转场推荐样式")
    ait = (await state.store.get("generate_ai_transition"))["ai_transitions"]
    n_groups = len((await state.store.get("group_clips"))["groups"])
    check(len(ait) == max(0, n_groups - 1) and n_groups >= 2
          and await refs_have_bytes(storage, [a["path"] for a in ait]),
          f"AI 转场片段真实渲染并入库（{n_groups} 组 → {len(ait)} 段 xfade）")
    tlai = (await state.store.get("plan_timeline_ai_transition"))["timeline_ai"]
    check(any(e.get("kind") == "transition" for e in tlai["events"]),
          "AI 转场片段并入时间线事件轨")

    packed2 = await itp.invoke("render_video", state)
    check(packed2["output"]["duration"] >= packed["output"]["duration"],
          "二次渲染走 ai 时间线（转场加长）且成功出片")
    check(packed2["output"]["video"] == object_key,
          "同产物重渲覆盖同一对象键，不产生第二条成片")
    check((await storage.db.count("render_jobs")) == 1,
          "同 (会话, 产物) 复跑复位原行，不插第二条渲染任务")

    # ---------- 契约负例：没有 material_ids 就报错，不再回落扫盘 ----------
    try:
        await itp.invoke("load_media", await fresh_state(storage, "t:noids", "an", "随便剪剪"))
        check(False, "无 material_ids 时 load_media 直接报错")
    except ValueError as e:
        check("material_ids" in str(e), "无 material_ids 时 load_media 直接报错（不扫盘）")

    # ---------- 运行 2：外部能力全部不可用 → 降级不失败，仍然出片 ----------
    reg2 = build_real_registry(settings0, dead_providers(), storage)
    itp2 = Interceptor(reg2)
    state2 = await fresh_state(storage, "t:degraded", "art2", "离线降级也要出片")
    await prep_creative(itp2, state2, [mat_a])
    packed3 = await itp2.invoke("render_video", state2, material_ids=[mat_a])
    v2 = await storage.objects.localize(packed3["output"]["video"], dl)
    caps2 = (await state2.store.get("understand_clips"))["clip_captions"]
    vo2 = (await state2.store.get("generate_voiceover"))["voiceover"]
    sc2 = (await state2.store.get("generate_script"))["group_scripts"]
    check(v2.exists() and v2.stat().st_size > 10_000, "降级链路仍产出真实 mp4")
    check(all("降级占位" in c["caption"] for c in caps2), "VL 不可用 → caption 占位降级")
    check(all(v["source"] == "silent_fallback" for v in vo2), "TTS 不可用 → 静音配音降级")
    check(all(s["source"] == "fallback" for s in sc2), "LLM 不可用 → 文案兜底降级")

    # ---------- 运行 2b：外部能力「不报错但永不返回」→ 超时切断，链路照样出片 ----------
    reg2b = build_real_registry(settings0, hung_providers(), storage)
    itp2b = Interceptor(reg2b)
    state2b = await fresh_state(storage, "t:hung", "art2b", "第三方卡死也不能冻住链")
    await prep_creative(itp2b, state2b, [mat_a])
    t0 = time.monotonic()
    packed2b = await itp2b.invoke("render_video", state2b, material_ids=[mat_a])
    elapsed = time.monotonic() - t0
    v2b = await storage.objects.localize(packed2b["output"]["video"], dl)
    warn2b = (await state2b.store.get("asr"))["warnings"]
    check(elapsed < 30, f"卡死的 ASR/LLM/TTS 被 provider_timeout_sec 切断（整链 {elapsed:.1f}s）")
    check(any("TimeoutError" in w for w in warn2b),
          f"ASR 卡死记超时降级 warning，不再无限等（{warn2b[:1]}）")
    check(v2b.exists() and v2b.stat().st_size > 10_000, "卡死降级链路仍产出真实 mp4")

    # ---------- 运行 3：口播原声混剪（演讲视频 + 空镜，保真原声、跳 TTS） ----------
    settings3 = write_config(tmp, suffix="3")
    storage3 = make_storage(settings3)
    raw3 = tmp / "raw3"
    raw3.mkdir(parents=True, exist_ok=True)
    make_source(raw3 / "0_speech.mp4", [
        "testsrc2=size=640x360:rate=25:duration=3",
        "smptebars=size=640x360:rate=25:duration=3",
    ], 500)
    make_silent(raw3 / "1_broll.mp4", [
        "color=c=green:size=640x360:rate=25:duration=2",
        "color=c=red:size=640x360:rate=25:duration=2",
    ])
    mat_speech = await seed(storage3, raw3 / "0_speech.mp4")
    mat_broll = await seed(storage3, raw3 / "1_broll.mp4")
    music = tmp / "music3"
    music.mkdir(parents=True, exist_ok=True)
    mediaops.tone_wav(music / "轻快_日常.wav", 6.0, 330)
    mediaops.tone_wav(music / "励志_慢燃.wav", 6.0, 220)
    await seed(storage3, music / "轻快_日常.wav", origin="bgm")
    mat_bgm_epic = await seed(storage3, music / "励志_慢燃.wav", origin="bgm")

    reg3 = build_real_registry(settings3, speech_providers(), storage3)
    itp3 = Interceptor(reg3)

    state3 = await fresh_state(storage3, "t:orig", "art3",
                               "保留这段演讲的原声，配空镜画面剪一条励志视频")
    await prep_creative(itp3, state3, [mat_speech, mat_broll], highlight_off=True)
    packed4 = await itp3.invoke("render_video", state3,
                                material_ids=[mat_speech, mat_broll],
                                keep_original_audio=True)
    tl3 = (await state3.store.get("plan_timeline"))["timeline"]
    rough3 = (await state3.store.get("speech_rough_cut"))["rough_clips"]
    check("speech_rough_cut" in itp3.order_trace and tl3.get("mode") == "original_audio",
          "显式 keep_original_audio=True 启用原声混剪时间线")
    check(len(tl3["audio_events"]) == len(rough3) == 2
          and all(a["src_start"] is not None and a["src_end"] > a["src_start"]
                  and a["path"].endswith(f"{mat_speech}.mp4") for a in tl3["audio_events"]),
          "音轨=口播原声区间（src_start/src_end + path 是素材的对象引用）")
    check([s["text"] for s in tl3["subtitles"]] == [r["text"] for r in rough3],
          "字幕逐字取 ASR 原文")
    check(all(e["path"].endswith(f"{mat_broll}.mp4") or e["path"].endswith(f"{mat_speech}.mp4")
               for e in tl3["events"]),
           "画面轨用空镜和口播源画面混合（原声混剪模式含出镜画面）")
    vo3 = await state3.store.get("generate_voiceover")
    check(vo3["voiceover"] == [] and vo3.get("mode") == "original_audio",
          "原声模式下配音环节自动跳过")
    check(abs(tl3["duration"] - 4.1) < 0.05, f"时间线总长=口播净时长（{tl3['duration']}s）")
    bgm3 = await state3.store.get("select_BGM")
    bgm_row = await storage3.materials.get(bgm3["material_id"])
    check(bgm3["source"] == "materials" and bgm3["material_id"] == mat_bgm_epic
          and bgm_row["filename"] == "励志_慢燃.wav"
          and bgm3["bgm"] == to_ref(bgm_row["object_key"]),
          "select_BGM 查曲库命中且按请求关键词「励志」排序，返回素材的对象引用")
    check(tl3["bgm"] and tl3["bgm"]["path"] == bgm3["bgm"],
          "曲库 BGM 并入原声混剪时间线")
    out3 = await storage3.objects.localize(packed4["output"]["video"], tmp / "dl3")
    info3 = mediaops.probe(out3)
    check(out3.exists() and info3["has_audio"], "原声混剪成片含音轨")
    check(abs(info3["duration"] - 4.1) < 0.6,
          f"成片时长≈口播净时长（实测 {info3['duration']}s）")

    # 显式参数路径：普通请求 + keep_original_audio=True 同样启用
    state4 = await fresh_state(storage3, "t:orig2", "art4", "把这两段素材剪到一起")
    await prep_creative(itp3, state4, [mat_speech, mat_broll])
    packed5 = await itp3.invoke("render_video", state4,
                                material_ids=[mat_speech, mat_broll],
                                keep_original_audio=True)
    tl4 = (await state4.store.get("plan_timeline"))["timeline"]
    check(tl4.get("mode") == "original_audio" and tl4["audio_events"],
          "显式 keep_original_audio=True 参数启用原声混剪")
    out4 = await storage3.objects.localize(packed5["output"]["video"], tmp / "dl4")
    check(abs(mediaops.probe(out4)["duration"] - 4.1) < 0.6,
          "显式参数路径同样出片含原声")

    # ---------- 运行 3c：素材没有口播段（无语音短片）——承诺必须照样落地 ----------
    print("\n[无口播素材：显式原声 + 目标时长]")
    settings5 = write_config(tmp, suffix="5")
    storage5 = make_storage(settings5)
    raw5 = tmp / "raw5"
    raw5.mkdir(parents=True, exist_ok=True)
    make_source(raw5 / "0_nospeech.mp4", [
        "testsrc2=size=640x360:rate=25:duration=4",
        "smptebars=size=640x360:rate=25:duration=4",
    ], 440)
    mat_nospeech = await seed(storage5, raw5 / "0_nospeech.mp4")

    async def plain_llm(messages):
        return json.dumps({"title": "短片精华", "groups": []}, ensure_ascii=False)

    no_speech_provs = build_providers(
        settings5.caps, vision=lambda images, prompt, **kw: "彩色测试画面",
        transcribe=lambda wav: [], llm=plain_llm)
    reg5 = build_real_registry(settings5, no_speech_provs, storage5)
    itp5 = Interceptor(reg5)

    state5 = await fresh_state(storage5, "t:nospeech", "art5", "把这条片段剪成一条精华")
    await prep_creative(itp5, state5, [mat_nospeech])
    packed5c = await itp5.invoke("render_video", state5, material_ids=[mat_nospeech],
                                 keep_original_audio=True, target_duration_sec=5.0)
    meta5 = await state5.store.get("plan_timeline")
    tl5 = meta5["timeline"]
    check((await state5.store.get("speech_rough_cut"))["rough_clips"] == [],
          "本例前提：ASR 确实没有口播段")
    check(tl5.get("mode") == "original_audio",
          "显式 keep_original_audio=True：没有口播段也不回落成配音路径")
    check(tl5["audio_events"] and all(a["kind"] == "original"
                                      and a["src_end"] > a["src_start"]
                                      and a["path"].endswith(f"{mat_nospeech}.mp4")
                                      for a in tl5["audio_events"]),
          "音轨=每段画面自己的原声窗口（不是 voiceover/*）")
    check((await state5.store.get("generate_voiceover"))["voiceover"] == [],
          "原声模式下不合成 TTS，无口播段也跳过")
    check(tl5["subtitles"] == [], "没有 ASR 原文就不凑字幕")
    check(tl5["duration"] <= 5.05, f"承诺的 5 秒落地：时间线 {tl5['duration']}s")
    check(max((e["end"] for e in tl5["events"]), default=0) <= 5.05,
          "画面事件同样裁到 5 秒（ffmpeg 兜底路径不看 duration，只看事件）")
    check(all(e["src_end"] - e["src_start"] <= e["end"] - e["start"] + 0.02
              for e in tl5["events"]),
          "src 窗口不越过事件自身的时长")
    check(any("裁到" in n for n in meta5.get("notes", [])),
          f"输出里说清了裁剪动作：{meta5.get('notes', [])}")
    out5 = await storage5.objects.localize(packed5c["output"]["video"], tmp / "dl5")
    info5 = mediaops.probe(out5)
    check(out5.exists() and info5["has_audio"] and info5["duration"] <= 5.6,
          f"成片真含原声且不超目标（实测 {info5['duration']}s）")

    # 未要求原声：走配音路径，目标时长同样要裁得住
    state6 = await fresh_state(storage5, "t:nospeech2", "art6", "给这段画面配一段旁白")
    await prep_creative(itp5, state6, [mat_nospeech])
    packed6 = await itp5.invoke("render_video", state6, material_ids=[mat_nospeech],
                                target_duration_sec=5.0)
    tl6 = (await state6.store.get("plan_timeline"))["timeline"]
    check(tl6.get("mode") != "original_audio" and tl6["audio_events"],
          "未要求原声时仍走配音路径（回落没有被误改）")
    check(tl6["duration"] <= 5.05, f"配音路径同样受目标时长约束：{tl6['duration']}s")
    check(all(e["end"] <= 5.05 for e in tl6["events"])
          and all(s["end"] <= 5.05 for s in tl6["subtitles"]),
          "画面事件与字幕都裁进 5 秒窗口")
    out6 = await storage5.objects.localize(packed6["output"]["video"], tmp / "dl6")
    info6 = mediaops.probe(out6)
    check(info6["duration"] <= 5.6, f"配音路径成片实测 {info6['duration']}s 不超目标")

    # ---------- 高光精选：speech_rough_cut 按语义打分挑金句段，plan_timeline 截到目标时长 ----------
    print("\n[高光精选：长演讲 → 短语录视频]")

    def highlight_transcribe(wav):
        if Path(wav).stem == "m0":
            return [
                {"start": 0.1, "end": 1.4, "text": "你们一定要坚持梦想，努力改变人生！"},
                {"start": 1.9, "end": 2.1, "text": "嗯"},
                {"start": 2.6, "end": 4.0, "text": "勇敢面对未来，相信自己可以做到。"},
                {"start": 4.5, "end": 5.9, "text": "不要放弃希望，光芒就在前方。"},
            ]
        return []

    async def highlight_llm(messages):
        return json.dumps({"title": "励志高光语录", "groups": []}, ensure_ascii=False)

    highlight_provs = build_providers(settings3.caps,
                                      vision=lambda images, prompt, **kw: "彩色画面",
                                      transcribe=highlight_transcribe, llm=highlight_llm)
    reg_hl = build_real_registry(settings3, highlight_provs, storage3)
    itp_hl = Interceptor(reg_hl)

    state_hl = await fresh_state(storage3, "t:hl", "art_hl",
                                "把演讲高光语录提取出来作为人声，做成励志短片")
    hl_keep = [
        {"start": 0.1, "end": 1.4, "clip": "m0", "text": "你们一定要坚持梦想，努力改变人生！"},
        {"start": 2.6, "end": 4.0, "clip": "m0", "text": "勇敢面对未来，相信自己可以做到。"},
    ]
    await prep_creative(itp_hl, state_hl, [mat_speech, mat_broll],
                        highlight_keep=hl_keep)
    packed_hl = await itp_hl.invoke("render_video", state_hl,
                                   material_ids=[mat_speech, mat_broll],
                                   target_duration_sec=3.0,
                                   keep_original_audio=True)
    rough_hl = (await state_hl.store.get("speech_rough_cut"))["rough_clips"]
    rough_meta = await state_hl.store.get("speech_rough_cut")
    tl_hl = (await state_hl.store.get("plan_timeline"))["timeline"]
    full_seg_count = 4  # highlight_transcribe 返回 4 段
    check(rough_meta.get("mode") == "llm_selected",
          "LLM 显式传 keep_segments 选段（高光模式）")
    check(len(rough_hl) < full_seg_count,
          f"高光精选从 {full_seg_count} 段精选到 {len(rough_hl)} 段")
    kept_sec = sum(r["end"] - r["start"] for r in rough_hl)
    check(kept_sec <= 3.0 * 1.16,
          f"精选总时长 {kept_sec:.1f}s 不超目标 3.0s 的 115%")
    check("嗯" not in " ".join(r["text"] for r in rough_hl),
          "低分短段'嗯'被高光精选丢弃")
    check("勇敢" in " ".join(r["text"] for r in rough_hl),
          "高分权力词段'勇敢面对未来'被保留")
    check(tl_hl.get("mode") == "original_audio",
          "高光+原声混剪：时间线仍为 original_audio 模式")
    check(tl_hl["duration"] <= 3.05,
          f"时间线总长被 target_duration_sec 硬裁到 3 秒（{tl_hl['duration']:.1f}s）")
    out_hl = await storage3.objects.localize(packed_hl["output"]["video"], tmp / "dl_hl")
    check(out_hl.exists() and mediaops.probe(out_hl)["has_audio"],
          "高光精选成片成功渲染含音轨")
    hl_dur = mediaops.probe(out_hl)["duration"]
    check(hl_dur <= 3.0 * 1.3,
          f"成片时长 {hl_dur:.1f}s 远短于全演讲 4.3s（高光精选生效）")

    print()
    print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
