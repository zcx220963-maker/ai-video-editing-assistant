# -*- coding: utf-8 -*-
"""B13 真机冒烟：持久化产物只带对象引用（`obj:`），换一份工作区也能凭引用重出片。

离线那条（``tests/test_payload_refs.py`` ④）验的是同一件事的形状；这里把它压到**真共享
库**上：真 PG（artifacts/render_jobs/materials）+ 真 MinIO（对象字节、head、presigned）
+ 真 ffmpeg/MoviePy。外部文案/TTS 用注入的 fake——本冒烟钉的是存储层，不是模型。

三段：
① 真上传一份合成素材（字节进 MinIO + 一行 materials），整链跑到出片：库里的每一行
   产物扫过不能出现本机绝对路径，时间线里每条引用都能在 MinIO head 到；
② 换一份**空工作区 + 空内容缓存**的存储实例（等价于换机器 / 进程重启后清盘）从同一张
   artifacts 表重建 Store，只重跑 render_video 就把片重新剪出来（时长一致、上游不重跑）；
③ 第一台机器的工作区必须能当场删净——渲染里漏关一个 ffmpeg 句柄，Windows 就会把它
   锁到进程结束，「工作区随时可弃」当场破产。

自己起临时工作区/缓存目录与临时身份，不动开发者常驻的 :8000/:8001，也不碰它们的工作区；
凭证只从环境变量读，任何 key/token 的值都不打印；跑完把自建行与自建对象全收回并复核零残留。

跑法（需 docker compose up -d，.env 五项已填）：
    PYTHONPATH=. python -u .smoke/b13_payload_refs_smoke.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, Interceptor, NodeState  # noqa: E402
from agent_framework.storage import build_storage, new_id, ref_key  # noqa: E402
from agent_framework.storage.media import kind_of, mime_of, probe as probe_meta  # noqa: E402
from storyline_server import mediaops  # noqa: E402
from storyline_server.nodes.core_nodes import build_real_registry  # noqa: E402
from storyline_server.providers import build_providers  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

STAMP = int(time.time())
USER = f"u_b13_{STAMP}"
CONV = f"c_b13_{STAMP}"
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(label)
    return bool(cond)


def make_source(dst: Path) -> None:
    """三段画面 + 正弦音轨的合成片：够切镜、够出片，不依赖任何外部素材。"""
    mediaops.ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=25:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=480x270:rate=25:duration=2",
        "-f", "lavfi", "-i", "color=c=navy:size=480x270:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst))


def fake_providers():
    async def llm(messages):
        return json.dumps({
            "title": "引用契约真机冒烟",
            "groups": [{"group_id": "group_0001",
                        "raw_text": "开场一句，中段一句，收尾一句。"}],
        }, ensure_ascii=False)

    async def tts(text, dst):
        mediaops.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "1.5", str(dst))
        return Path(dst)

    return build_providers(
        Settings().caps,
        vision=lambda images, prompt, **kw: "三段测试画面：条纹、彩条、深蓝",
        transcribe=lambda wav: [{"start": 0.4, "end": 2.0, "text": "开场一句话"},
                                {"start": 3.0, "end": 5.2, "text": "中段一句话收尾"}],
        llm=llm, tts=tts)


async def seed_material(st, src: Path) -> tuple[str, str]:
    """一次真实上传的形状：字节直写 MinIO + materials 登记，返回 (对象键, material_id)。"""
    mid = new_id("mat", 6)
    key = f"users/{USER}/convs/{CONV}/{mid}{src.suffix}"
    blob = src.read_bytes()

    async def chunks():
        yield blob

    await st.objects.put(key, chunks(), content_type=mime_of(src.name))
    meta = await probe_meta(src)
    await st.users.provision(USER)
    await st.conversations.ensure(USER, CONV)
    await st.materials.register(
        USER, CONV, key, src.name, kind_of(src.name) or "video",
        bytes_=len(blob), sha256=hashlib.sha256(blob).hexdigest(),
        mime=mime_of(src.name),
        duration_sec=meta["duration_sec"], width=meta["width"], height=meta["height"],
        has_audio=meta["has_audio"], origin="upload", material_id=mid)
    return key, mid


def walk_strings(v: Any):
    if isinstance(v, dict):
        for x in v.values():
            yield from walk_strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from walk_strings(x)
    elif isinstance(v, str):
        yield v


def local_paths(strings) -> list[str]:
    """payload 里出现过的本机绝对路径（Windows 盘符 / POSIX 绝对目录）。"""
    return [s for s in strings
            if re.match(r"^[a-zA-Z]:[\\/]", s) or re.match(r"^/(?:[^/]+/)+[^/]+$", s)]


async def state_of(st, sid: str, art: str, request: str) -> NodeState:
    store = await ArtifactStore.open(st.artifacts(sid, art), sid, art)
    return NodeState(session_id=sid, artifact_id=art, user_request=request,
                     store=store, user_id=USER, conversation_id=CONV)


async def main() -> int:
    sid, art = f"u:{USER}:c:{CONV}", "b13"
    tmp = Path(tempfile.mkdtemp(prefix="b13_refs_"))
    ws1, cache1 = tmp / "ws1", tmp / "cache1"
    ws2, cache2 = tmp / "ws2", tmp / "cache2"
    st1 = build_storage("pg_minio", workspace_root=ws1, cache_root=cache1)
    await st1.start()
    await st1.provision_internal()
    mat, mat_key = "", ""
    st2 = None
    try:
        raw = tmp / "src.mp4"
        make_source(raw)
        mat_key, mat = await seed_material(st1, raw)
        check((await st1.objects.head(mat_key)) is not None,
              f"素材字节真进了 MinIO（{mat_key}）")

        # ---------- ① 整链出片：库里的产物只带引用 ----------
        itp1 = Interceptor(build_real_registry(Settings(), fake_providers(), st1))
        out1 = await itp1.invoke("render_video", await state_of(st1, sid, art, "剪一条引用冒烟片"),
                                 material_ids=[mat])
        key1, dur1 = out1["output"]["video"], float(out1["output"]["duration"])
        check((await st1.objects.head(key1)) is not None and dur1 > 1.0,
              f"整链出片：{key1}（{dur1}s）")

        rows = await st1.artifacts(sid, art).rows()
        leak = local_paths(s for r in rows for s in walk_strings(r["payload"]))
        check(len(rows) >= 10 and not leak,
              f"真库里 {len(rows)} 行产物没有一处本机绝对路径（泄漏：{leak[:3]}）")

        # 会过期的一样不当持久引用：直链只回调用方，库里留对象键
        rv = next((r["payload"] for r in rows if r["node"] == "render_video"), {})
        check("video" in rv and "media_url" not in rv,
              f"真库 render_video 行键面 {sorted(rv)}：有对象键、没有会过期的直链")
        check(str(out1["output"].get("media_url", "")).startswith("http"),
              "presigned 直链仍回给调用方（剔的是入库那份）")

        tl = (await st1.artifacts(sid, art).snapshot())["plan_timeline"]["timeline"]
        refs = ([e["path"] for e in tl["events"]]
                + [a["path"] for a in tl.get("audio_events", [])]
                + [x for x in [(tl.get("bgm") or {}).get("path")] if x])
        heads = await asyncio.gather(*[st1.objects.head(ref_key(r) or "") for r in refs])
        check(refs and all(ref_key(r) for r in refs)
              and all(h is not None and h.bytes > 0 for h in heads),
              f"时间线 {len(refs)} 条引用逐条能在 MinIO head 到字节")

        # ---------- ③ 第一份工作区当场删净（句柄没漏关） ----------
        shutil.rmtree(ws1, ignore_errors=True)
        shutil.rmtree(cache1, ignore_errors=True)
        left = ([p for p in ws1.rglob("*") if p.is_file()] if ws1.exists() else [])
        left += [p for p in cache1.rglob("*") if p.is_file()] if cache1.exists() else []
        check(not left, "第一份工作区/内容缓存当场删净"
                        f"（漏关的 ffmpeg 句柄会把它锁到进程结束）：{[str(p) for p in left[:2]]}")
        await st1.close()

        # ---------- ② 换一份空工作区，从同一张表按引用重出片 ----------
        st2 = build_storage("pg_minio", workspace_root=ws2, cache_root=cache2)
        await st2.start()
        itp2 = Interceptor(build_real_registry(Settings(), fake_providers(), st2))
        traced = len(itp2.order_trace)
        out2 = await itp2.invoke("render_video",
                                 await state_of(st2, sid, art, "空工作区重出片"),
                                 material_ids=[mat])
        check(itp2.order_trace[traced:] == ["render_video"],
              f"空工作区只重跑终点（{itp2.order_trace[traced:]}），上游产物按引用复用")
        dur2 = float(out2["output"]["duration"])
        check(out2["output"]["video"] == key1 and abs(dur2 - dur1) < 0.35,
              f"凭引用重出片成功：{dur1}s → {dur2}s，成片键不变")
        job = await st2.render_jobs.get(sid, art)
        check(job["status"] == "done" and job["video_object_key"] == key1,
              "render_jobs 终态由第二个实例写回同一行")
        rows2 = await st2.artifacts(sid, art).rows()
        leak2 = local_paths(s for r in rows2 for s in walk_strings(r["payload"]))
        check(not leak2, f"重出片之后库里依旧干净（泄漏：{leak2[:3]}）")
        return 1 if FAILS else 0
    finally:
        residue = await _cleanup(st2 or st1, sid, art, mat, mat_key)
        for s in (st1, st2):
            if s is not None:
                try:
                    await s.close()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
        print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
        print(f"自建数据残留复核：{residue}", flush=True)
    return 1


async def _cleanup(st, sid: str, art: str, mat: str, mat_key: str) -> str:
    """收回自建的行与对象并复核零残留；对象键从库里的引用反推，不留孤儿。"""
    keys: set[str] = set()
    try:
        for r in await st.artifacts(sid, art).rows():
            keys |= {ref_key(s) for s in walk_strings(r["payload"]) if ref_key(s)}
        for j in await st.db.select("render_jobs", where={"session_id": sid}):
            if j.get("video_object_key"):
                keys.add(j["video_object_key"])
        await st.db.delete("artifacts", where={"session_id": sid})
        await st.db.delete("render_jobs", where={"session_id": sid})
    except Exception as e:  # noqa: BLE001
        print(f"  [清理] 产物/渲染任务：{e}", flush=True)
    if mat_key:
        keys.add(mat_key)
    for k in sorted(keys):
        try:
            await st.objects.delete(k)
        except Exception:  # noqa: BLE001
            pass
    try:
        if mat:
            await st.materials.drop(USER, mat)
        await st.conversations.drop(USER, CONV)
        await st.db.delete("users", where={"id": USER})
    except Exception as e:  # noqa: BLE001
        print(f"  [清理] 身份/素材行：{e}", flush=True)
    orphan = [k for k in sorted(keys) if await st.objects.head(k) is not None]
    return (f"产物 {await st.db.count('artifacts', where={'session_id': sid})} / "
            f"渲染任务 {await st.db.count('render_jobs', where={'session_id': sid})} / "
            f"素材 {'在' if mat and await st.db.get_by_pk('materials', {'id': mat}) else '无'} / "
            f"身份 {'在' if await st.db.get_by_pk('users', {'id': USER}) else '无'} / "
            f"未删净对象 {orphan}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
