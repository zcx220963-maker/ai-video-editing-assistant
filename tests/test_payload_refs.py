# -*- coding: utf-8 -*-
"""持久化产物里的文件引用（`obj:`）契约——payload 不得写工作区绝对路径。

病根：节点把 ffmpeg 看得见的那个本机路径顺手写进 payload，而 payload 会进
``artifacts`` 表、被 fork 整份复制到新作用域、还会作为工具结果写进
``checkpoint_entries``。工作区按设计是**可丢弃**的（启动 ``sweep_stale``、渲染失败
``cleanup``、换一台机器、换一份产物作用域），于是那些路径迟早指向空气。

这里钉四条：
① 引用原语：`to_ref` / `ref_key`，非引用（遗留绝对路径）如实判为 None；
② ``Workspace.localize_ref``：同一引用在同一目录只落一次（并发索取也只回源一次）、
   不同对象键同名不互相盖、遗留绝对路径本机还在就照用、字节没了就如实报错并给出路；
③ ``publish_derived``：自产文件进对象存储的键面（``derived/{会话}/{产物}/{环节}/{名}``）、
   引用回传、同名重发覆盖同一条不留孤儿；
④ **整链搬迁证明**：真跑一遍剪辑链出片 → 把本机的工作区与内容缓存目录**全删** →
   从 artifacts 表重建 Store 再渲染一次，只靠 payload 里的引用就把片重新剪出来。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, Interceptor, NodeState
from agent_framework.storage import (
    StorageUnavailable,
    build_storage,
    new_id,
    ref_key,
    to_ref,
)
from agent_framework.storage.media import kind_of, mime_of, probe as probe_meta
from agent_framework.storage.object_store import REF_PREFIX, scoped_dir
from storyline_server import mediaops
from storyline_server.nodes.core_nodes import _localize_timeline, build_real_registry
from storyline_server.providers import build_providers
from storyline_server.settings import Settings

FAILS = 0
USER = "u_ref"
CONV = "c_ref"


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def write_config(tmp: Path) -> Settings:
    cfg = tmp / f"config_{tmp.name[:6]}.toml"
    cfg.write_text(
        f"""
[local_mcp_server]
server_name = "storyline"
port = 8096

[storage]
backend = "memory"
workspace_root = "{(tmp / 'workspace').as_posix()}"
cache_root = "{(tmp / 'object_cache').as_posix()}"

[capabilities]
scene_threshold = 0.25
""", encoding="utf-8")
    return Settings.load(cfg)


def make_storage(settings: Settings):
    return build_storage(
        settings.storage.backend,
        cache_root=settings.storage.cache_root,
        workspace_root=settings.storage.workspace_root,
        cache_max_gb=settings.storage.workspace_max_gb)


def make_source(dst: Path) -> None:
    """两个场景 + 正弦音轨：够切出两个镜头、够渲染出一条短片。"""
    mediaops.ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(dst))


async def seed(storage, src: Path) -> tuple[str, str]:
    """一次上传的形状：字节进对象存储 + 一行 materials；返回 (对象键, material_id)。"""
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
    return key, mid


def fake_providers():
    async def llm(messages):
        return json.dumps({"title": "引用搬迁验证片",
                           "groups": [{"group_id": "group_0001",
                                       "raw_text": "两个场景的引用测试。"}]},
                          ensure_ascii=False)

    async def tts(text, dst):
        mediaops.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "1.2", str(dst))
        return Path(dst)

    return build_providers(Settings().caps,
                           vision=lambda images, prompt, **kw: "测试画面：条纹与色块",
                           transcribe=lambda wav: [], llm=llm, tts=tts)


def leaked_paths(rows, ws_root: Path) -> list[str]:
    """产物行里仍写着本机绝对路径的「节点名: 段」——改写后应当为空。

    判据不止工作区根：任何 `C:/…` 形态都是同一类病——把「这台机器的哪个位置」写进了
    跨机器、跨重启存在的库。presigned 直链里的 `://` 不算（那是对象存储的址）。
    """
    root = str(Path(ws_root).resolve()).replace("\\", "/").lower()
    bad: list[str] = []
    for r in rows:
        for seg in re.findall(r'"([^"]*)"', json.dumps(r["payload"], ensure_ascii=False)):
            t = seg.replace("\\", "/").lower()
            if root in t or re.match(r"^[a-z]:/", t):
                bad.append(f"{r['node']}: {seg}")
    return bad


# --------------------------------------------------------------------------

def case_ref_primitives() -> None:
    print("\n[① 引用原语]")
    check(REF_PREFIX == "obj:", "引用前缀是 obj:（一眼和盘符路径区分开）")
    check(to_ref("users/a/b.mp4") == "obj:users/a/b.mp4", "to_ref 只加前缀，不改对象键")
    check(ref_key("obj:users/a/b.mp4") == "users/a/b.mp4", "ref_key 取回对象键")
    check(ref_key("C:/tmp/a.mp4") is None and ref_key("") is None
          and ref_key(None) is None, "非引用（本机绝对路径 / 空值）如实判为 None")


async def case_localize_ref(tag: str) -> None:
    print("\n[② localize_ref：一次落盘、同名不互盖、遗留路径与失效引用]")
    tmp = Path(tempfile.mkdtemp(prefix=f"refs_{tag}_"))
    st = make_storage(write_config(tmp))
    ws = st.workspace
    blob_a, blob_b = "素材A的字节".encode() * 10, "素材B的字节".encode() * 20

    async def put(key: str, blob: bytes) -> None:
        async def chunks():
            yield blob
        await st.objects.put(key, chunks(), content_type="video/mp4")

    await put("users/u1/convs/c1/matA.mp4", blob_a)
    await put("users/u2/convs/c2/matA.mp4", blob_b)
    dst = scoped_dir(ws.root, "s:ref", "art1", "material")

    p1 = await ws.localize_ref(to_ref("users/u1/convs/c1/matA.mp4"), dst)
    p2 = await ws.localize_ref(to_ref("users/u2/convs/c2/matA.mp4"), dst)
    check(p1 != p2 and p1.read_bytes() == blob_a and p2.read_bytes() == blob_b,
          "同名不同键各占各的 slot，不会互相盖住")
    check(p1.parent.is_relative_to(dst) and p2.parent.is_relative_to(dst),
          "两份都落在调用方给的中转目录里（工作区内，随作用域回收）")

    before = len(st.objects.calls)
    again = await ws.localize_ref(to_ref("users/u1/convs/c1/matA.mp4"), dst)
    check(again == p1 and len(st.objects.calls) == before,
          "同一引用同一目录命中已有文件就直返：不回源、也不再拷一份")

    got = await asyncio.gather(*(
        ws.localize_ref(to_ref("users/u1/convs/c1/matA.mp4"),
                        scoped_dir(ws.root, "s:ref", f"art{99 + i}", "material"))
        for i in range(8)))
    check(len({p.name for p in got}) == 1 and got[0].read_bytes() == blob_a,
          f"同一份字节的 8 个并发索取各得一个完整文件（{got[0].name}）")

    legacy = tmp / "legacy.mp4"
    legacy.write_bytes("旧写入方式留下的本机路径".encode())
    check(await ws.localize_ref(str(legacy), dst) == legacy,
          "遗留 payload 的绝对路径：本机文件还在就照旧可用")
    try:
        await ws.localize_ref(str(tmp / "已经不在了.mp4"), dst)
        check(False, "遗留绝对路径指向空气时必须抛错")
    except StorageUnavailable as e:
        check("重跑" in str(e), f"字节不在本机就如实说「重跑产出它的节点」：{str(e)[:46]}…")
    try:
        await ws.localize_ref(to_ref("users/u1/convs/c1/被删掉的.mp4"), dst)
        check(False, "引用指向不存在的对象时必须抛错")
    except StorageUnavailable as e:
        check("被删掉的.mp4" in str(e), "引用回源失败时把对象键原样报出来")
    await st.close()
    shutil.rmtree(tmp, ignore_errors=True)


async def case_publish_derived() -> None:
    print("\n[③ publish_derived：自产字节的键面与引用]")
    tmp = Path(tempfile.mkdtemp(prefix="refs_derived_"))
    st = make_storage(write_config(tmp))
    ws = st.workspace
    f = ws.dir_for("s:derived", "artD", "voiceover") / "group_0001.mp3"
    f.write_bytes(b"first-take")
    ref = await ws.publish_derived(f, session_id="s:derived", artifact_id="artD",
                                   stage="voiceover")
    check(ref == to_ref("derived/s_derived/artD/voiceover/group_0001.mp3"),
          f"键面 = derived/{{会话}}/{{产物}}/{{环节}}/{{文件名}}：{ref}")
    head = await st.objects.head(ref_key(ref))
    check(head is not None and head.bytes == 10 and head.content_type == "audio/mpeg",
          "入库字节数与 content_type 都在（按扩展名给出）")

    f.write_bytes(b"second-take-is-longer")
    ref2 = await ws.publish_derived(f, session_id="s:derived", artifact_id="artD",
                                    stage="voiceover")
    check(ref2 == ref and (await st.objects.head(ref_key(ref))).bytes == 21
          and len([k for k in st.objects.data if k.startswith("derived/")]) == 1,
          "同产物同名重发覆盖同一条键，不留孤儿对象")

    other = await ws.publish_derived(f, session_id="s:derived", artifact_id="artE",
                                     stage="voiceover")
    check(other != ref, "换一份产物作用域就是另一个键：两个版本的配音互不覆盖")
    await st.close()
    shutil.rmtree(tmp, ignore_errors=True)


async def drive_creative_decisions(itp, state, mat: str) -> None:
    """把两个「必须显式调用」的创意节点跑掉，让 render_video 的依赖补齐能走到终点。

    契约（``require_explicit_call``）要求创意决策由调用方传入，服务端不替它兜底：
      · ``group_clips`` 要 ``custom_groups``（组里放真实 clip id）；
      · ``script_template_rec`` 要 ``template_id``（取值见 data/templates.json）。

    ``filter_clips`` 同样标了显式调用，但它排在 group_clips 之前，这里一并给上
    ``keep_clips``——不然 group_clips 的依赖补齐又会撞同一道墙。
    """
    await itp.invoke("split_shots", state, material_ids=[mat])
    await itp.invoke("understand_clips", state)
    caps = (await state.store.get("understand_clips")) or {}
    clip_ids = [str(c.get("clip_id") or c.get("id"))
                for c in (caps.get("clip_captions") or [])
                if (c.get("clip_id") or c.get("id"))]
    keep = clip_ids[:2] or ["m0_s0"]
    await itp.invoke("filter_clips", state, keep_clips=keep)
    await itp.invoke("group_clips", state, custom_groups=[
        {"group_id": "g0", "clips": keep, "summary": "引用搬迁用"},
    ])
    await itp.invoke("script_template_rec", state, template_id="tpl_free")


async def case_chain_moves() -> None:
    print("\n[④ 整链搬迁：删光本机目录后，只靠库里的引用重新出片]")
    tmp = Path(tempfile.mkdtemp(prefix="refs_chain_"))
    settings = write_config(tmp)
    st = make_storage(settings)
    raw = tmp / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    make_source(raw / "src.mp4")
    key, mat = await seed(st, raw / "src.mp4")

    itp = Interceptor(build_real_registry(settings, fake_providers(), st))
    sid, art = "s:move", "artM"
    store = await ArtifactStore.open(st.artifacts(sid, art), sid, art)
    state = NodeState(session_id=sid, artifact_id=art, user_request="剪一条引用测试片",
                      store=store, user_id=USER, conversation_id=CONV)
    # group_clips / script_template_rec 标了 require_explicit_call：拦截器**不会**
    # 自动补齐它们（创意决策必须由调用方传入）。所以这里先把这两个显式驱动一遍，
    # 再交给 render_video 走依赖补齐。本用例考的是「引用能不能扛住搬迁」，
    # 之前靠自动补齐正好绕过了这条契约，契约收紧后那条路就断了。
    await drive_creative_decisions(itp, state, mat)
    first = await itp.invoke("render_video", state, material_ids=[mat])
    dur1 = float(first["output"]["duration"])
    check(dur1 > 1.0, f"第一次渲染出片（{dur1}s）")

    rows = await st.artifacts(sid, art).rows()
    leak = leaked_paths(rows, st.workspace.root)
    check(len(rows) >= 10 and not leak,
          f"{len(rows)} 行产物里没有任何本机绝对路径（泄漏：{leak}）")

    tl = (await st.artifacts(sid, art).snapshot())["plan_timeline"]["timeline"]
    check(all(ref_key(e["path"]) for e in tl["events"])
          and all(ref_key(a["path"]) for a in tl["audio_events"])
          and ref_key(tl["bgm"]["path"]),
          "时间线三条轨（画面 / 音轨 / BGM）落库的都是引用")
    check(tl["events"][0]["path"] == to_ref(key),
          "画面轨的引用就是素材的对象键（没有第二套命名）")
    derived = sorted(k.split("/")[-1] for k in st.objects.data if k.startswith("derived/"))
    check(any(n.endswith(".mp3") for n in derived) and any(n.endswith(".wav") for n in derived),
          f"配音与占位 BGM 的字节进了对象存储：{derived}")

    # 会失效的东西不当持久引用：直链一小时后就读不通，存进共享库等于埋一条注定死掉的引用
    rv = (await st.artifacts(sid, art).snapshot())["render_video"]
    check("video" in rv and "media_url" not in rv,
          f"落库的 render_video 只有对象键、没有 media_url（键面 {sorted(rv)}）")
    check(first["output"].get("media_url") and "renders/s_move/artM.mp4" in first["output"]["media_url"],
          "media_url 照常回给调用方（剔的是入库那份，返回值不受影响）")

    # 关键一步：本机的中转全删——等价于 sweep_stale 收工 + 换一台机器 + 进程重启
    for doomed in (st.workspace.root, Path(settings.storage.cache_root)):
        shutil.rmtree(doomed, ignore_errors=True)
    residue = [p for base in (st.workspace.root, Path(settings.storage.cache_root))
               if base.exists() for p in base.rglob("*") if p.is_file()]
    check(not residue,
          "工作区与内容缓存目录已删空（本机不留一份中转字节）："
          f"残留 {[str(p.relative_to(tmp)) for p in residue[:5]]}")

    reopened = await ArtifactStore.open(st.artifacts(sid, art), sid, art)
    check(await reopened.get("plan_timeline") is not None
          and (await reopened.get("split_shots"))["clips"],
          "从 artifacts 表重建的 Store 读得到上游产物（进程重启的形状）")

    traced_before = len(itp.order_trace)
    state2 = NodeState(session_id=sid, artifact_id=art, user_request="换个说法再出一版",
                       store=reopened, user_id=USER, conversation_id=CONV)
    second = await itp.invoke("render_video", state2)
    dur2 = float(second["output"]["duration"])
    check(abs(dur2 - dur1) < 0.35,
          f"只靠库里的引用就重新出片：{dur1}s → {dur2}s")
    check(len(itp.order_trace) - traced_before == 1,
          f"搬迁只跑了终点这一个节点，上游按引用复用（本轮 {itp.order_trace[traced_before:]}）")
    check(second["output"]["video"] == "renders/s_move/artM.mp4",
          "同产物重渲覆盖同一个成片键")

    tl_after = (await st.artifacts(sid, art).get("plan_timeline"))["timeline"]
    check(all(ref_key(e["path"]) for e in tl_after["events"]),
          "渲染之后库里的时间线依旧全是引用（本机路径没被写回去）")
    local = await _localize_timeline(tl_after, st.workspace,
                                     st.workspace.dir_for(sid, art, "render", "src"))
    check(all(Path(e["path"]).is_file() for e in local["events"])
          and all(ref_key(e["path"]) for e in tl_after["events"]),
          "渲染期副本指向真实文件，而库里的时间线原样不动")
    await st.close()
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    async def _run() -> None:
        case_ref_primitives()
        await case_localize_ref("localize")
        await case_publish_derived()
        await case_chain_moves()

    asyncio.run(_run())
    print()
    print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
