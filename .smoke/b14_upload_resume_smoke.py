# -*- coding: utf-8 -*-
"""B14 真机冒烟：大文件分片上传——服务掉电重启后，进度还在桶里。

跑法（需 docker compose up -d，.env 五项已填）：
    PYTHONPATH=. python -u .smoke/b14_upload_resume_smoke.py

为什么必须真机再跑一遍：离线用例（tests/test_upload_resume.py，88 项）把切法、拒单、
幂等、归属都钉死了，但那里的「桶」是内存字典、「服务重启」只是换了一个 Storage 句柄、
「素材」只是一堆等长字节。下面这几件事只有在真 MinIO + 真 PG + 真进程上才同时成立：

① 分片字节真的落在桶里（不是本机临时目录），而且 ``list_prefix`` 从 MinIO 一次列举就带回
   x-amz-meta-sha256，与本机切片指纹逐片一致——「已有哪几片」全靠这一次列举，不必逐片 HEAD；
② 传到第 2 片把服务进程 **kill -9**（掉电式崩溃，任何收尾钩子都不走），再起一个新进程：
   新事件循环、新 asyncpg 池、**新工作区与新缓存目录**（等价于换机器）。同身份同会话重发
   init 接回的是同一条 upload_id，且进度里那两片已在——实例内存里本来就没有状态；
③ 补齐后 complete 拼出的素材与本机源文件逐字节一致，且 ffprobe 在新实例 localize 出来的
   文件上读得出时长与音视频流：那是一节剪得动的料，不是一堆恰好等长的字节；
③b complete 的 ``parts_sha256`` 在真桶上核：少报条数回 400 且分片一片不少，把其中一片的摘要
   算错回 422 且**只有那一片**从桶里消失、其余完好，照 missing_parts 重传那片再交正确清单出片，
   比对用的基准是真 MinIO 报回的 x-amz-meta-sha256，不是本机内存里那份；
④ 真库跑遍账本每一条 SQL：open/find_live/touch/finalize/drop/expired（离线只走过内存替身）；
⑤ 收尾：complete 后该会话在桶里的 uploads/ 前缀为空、账本行标 completed；abort 删净分片；
   另一身份的 token 问同一条 upload_id 得 404。

自己起自己的端口、自己的工作区，不动开发者常驻的 :8000/:8001；token 只按存在/长度报告，
绝不打印值；跑完把自己造的行与对象删干净并复核零残留。末尾那次过期清扫按 updated_at 选行、
不分归属，所以放在最后执行——本表只由分片上传特性写入。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".smoke"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

from agent_framework import uploads  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.storage.media import probe as probe_meta  # noqa: E402
from b6_fork_smoke import Service, bearer, free_port  # noqa: E402

STAMP = int(time.time())
CONV = f"c_b14_{STAMP}"
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(label)
    return bool(cond)


def argv(port: int, ws: Path, cache: Path) -> list[str]:
    """一个临时主服务：只做 HTTP + 存储，不接 MCP/Storyline，也不抢常驻实例的活。"""
    return ["run_server.py", "--storage", "pg_minio", "--port", str(port),
            "--no-mcp", "--no-storyline", "--no-resume", "--static-dir", "",
            "--workspace-root", str(ws), "--cache-root", str(cache),
            "--max-upload-mb", "512"]


def synth_source(dst: Path) -> bytes:
    """本机 ffmpeg 造一段真会动的素材（1280x720/30fps/6s + 440Hz 音轨，约 7MB）。

    必须是真容器：分片拼装只要错一个字节，moov atom 就废了，ffprobe 当场读不出来——
    用随机字节做这件事等于没做。全关键帧（-g 1）而不是 -b:v 目标码率：x264 对简单画面
    远打不满目标码率，第一版因此只造出 0.82MB（一片就走完了），续传场景展不开。
    """
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "1", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(dst)],
        check=True, timeout=300)
    return dst.read_bytes()


def slice_of(blob: bytes, ps: int, i: int) -> bytes:
    return blob[i * ps:(i + 1) * ps]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def put_part(c: httpx.AsyncClient, tok: str, upload_id: str, i: int,
                   data: bytes) -> dict[str, Any]:
    r = await c.put("/upload/part",
                    params={"upload_id": upload_id, "part": i, "sha256": sha(data)},
                    content=data, headers=bearer(tok))
    if r.status_code != 200:
        check(False, f"PUT /upload/part 第 {i} 片（{len(data)} 字节）200："
                     f"实得 {r.status_code} {r.text[:160]}")
        raise RuntimeError(f"part {i} → {r.status_code}")
    return r.json()


async def status(c: httpx.AsyncClient, tok: str, upload_id: str) -> dict[str, Any]:
    r = await c.get("/upload/status", params={"upload_id": upload_id}, headers=bearer(tok))
    return {"http": r.status_code, "v": r.json() if r.status_code == 200 else {}}


async def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("SKIP  本机 PATH 上没有 ffmpeg，造不出真素材", flush=True)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="b14_"))
    blob = synth_source(tmp / "航拍原片.mp4")
    total, whole = len(blob), sha(blob)
    ps = uploads.pick_part_size(total)
    n_parts = -(-total // ps)
    print(f"\n=== B14 分片续传真机冒烟：源片 {total / 2**20:.2f}MB "
          f"按 {ps / 2**20:.0f}MB 切 {n_parts} 片 ===", flush=True)
    check(n_parts >= 3, f"切出 {n_parts} 片：够「传两片—崩—接回其余」")

    st = build_storage("pg_minio")
    await st.start()
    await st.provision_internal()

    port1, port2 = free_port(), free_port()
    base1, base2 = f"http://127.0.0.1:{port1}", f"http://127.0.0.1:{port2}"
    svc1 = Service(argv(port1, tmp / "ws1", tmp / "cache1"), f"main1-{port1}")
    svc2 = Service(argv(port2, tmp / "ws2", tmp / "cache2"), f"main2-{port2}")
    uid = tok = uid2 = tok2 = sid = ""
    material_id = obj_key = ""
    aborted: list[str] = []

    try:
        svc1.start()
        if not await svc1.wait_ready(base1):
            check(False, "第一个临时主服务起来并 /health 通过（后面的检查作废）")
            return 1

        async with httpx.AsyncClient(base_url=base1, timeout=180) as c:
            a = (await c.post("/register", json={"device_name": "b14-uploader"})).json()
            b = (await c.post("/register", json={"device_name": "b14-other"})).json()
            uid, tok, uid2, tok2 = a["user_id"], a["token"], b["user_id"], b["token"]
            check(bool(tok) and bool(tok2), f"两个真身份注册（token 只记长度 "
                  f"{len(tok)}/{len(tok2)}，值不打印）")

            r = await c.post("/upload/init", params={
                "conversation_id": CONV, "filename": "航拍原片.mp4",
                "size": total, "sha256": whole}, headers=bearer(tok))
            check(r.status_code == 200, f"POST /upload/init 200（{r.status_code} {r.text[:160]}）")
            v = r.json()
            sid = v["upload_id"]
            check(v["part_size"] == ps and v["part_count"] == n_parts,
                  f"服务端回权威切法：{v['part_size'] / 2**20:.0f}MB × {v['part_count']} 片")
            check(v["missing_parts"] == list(range(n_parts)) and v["received_bytes"] == 0,
                  "起手全缺：missing_parts 就是客户端该传的片号清单")

            print("\n--- ① 真桶：分片字节与逐片指纹 ---", flush=True)
            prefix = uploads.session_prefix(uid, CONV, sid)
            head_before = await st.objects.list_prefix(prefix)
            check(head_before == [], "刚 init 时桶里这片前缀下什么都没有（进度只由 PUT 造成）")
            for i in (0, 1):
                await put_part(c, tok, sid, i, slice_of(blob, ps, i))
            got = await st.objects.list_prefix(prefix)
            check([g.key.rsplit("/", 1)[-1] for g in got] == ["part_00000", "part_00001"],
                  f"MinIO 列举按键升序回两片（实得 {[g.key.rsplit('/', 1)[-1] for g in got]}）")
            check([g.bytes for g in got] == [len(slice_of(blob, ps, i)) for i in (0, 1)],
                  f"每条字节数与写入一致（实得 {[g.bytes for g in got]}）")
            check([g.sha256 for g in got] == [sha(slice_of(blob, ps, i)) for i in (0, 1)],
                  "一次列举就带回 x-amz-meta-sha256 且与本机切片逐片相符"
                  "（续传不必逐片 HEAD）")

            s1 = await status(c, tok, sid)
            check(s1["http"] == 200 and s1["v"]["received_bytes"] == 2 * ps
                  and s1["v"]["missing_parts"] == list(range(2, n_parts)),
                  f"服务口径的进度：{s1['v']['received_bytes'] / 2**20:.0f}MB 已收，"
                  f"还缺 {s1['v']['missing_parts']}")

        print("\n--- ② kill -9：掉电式崩溃后重起新进程 ---", flush=True)
        svc1.kill()
        await asyncio.sleep(1.0)
        svc2.start()
        if not await svc2.wait_ready(base2):
            check(False, "第二个临时主服务起来（换端口/新事件循环/新连接池/新工作区）")
            return 1

        async with httpx.AsyncClient(base_url=base2, timeout=180) as c:
            r = await c.post("/upload/init", params={
                "conversation_id": CONV, "filename": "航拍原片.mp4",
                "size": total, "sha256": whole}, headers=bearer(tok))
            check(r.status_code == 200, f"新进程上重发 init 200（{r.status_code} {r.text[:160]}）")
            v = r.json()
            check(v["upload_id"] == sid and v["resumed"] is True,
                  f"接回的是崩溃前那条 upload_id（{sid}）")
            check([p["index"] for p in v["parts"]] == [0, 1]
                  and v["missing_parts"] == list(range(2, n_parts)),
                  f"崩溃前那两片在新进程眼里已经在（{[p['index'] for p in v['parts']]}），"
                  f"重传点落在第 {v['missing_parts'][0] if v['missing_parts'] else '—'} 片")
            check(v["received_bytes"] == 2 * ps and v["percent"] > 0,
                  f"进度百分比续上了：{v['percent']}%")

            print("\n--- ③ 补齐 → complete → 真素材 ---", flush=True)
            for i in range(2, n_parts):
                await put_part(c, tok, sid, i, slice_of(blob, ps, i))

            # 逐片内容比对在真 MinIO 上跑一遍：报回来的摘要必须是桶里真存下的字节算出的
            digests = [sha(slice_of(blob, ps, i)) for i in range(n_parts)]
            r = await c.post("/upload/complete", params={"upload_id": sid},
                             headers=bearer(tok), json={"parts_sha256": digests[:2]})
            check(r.status_code == 400,
                  f"只报 2 条摘要（实有 {n_parts} 片）→ 400，少报等于漏检（{r.text[:120]}）")
            check(len([g for g in await st.objects.list_prefix(prefix)]) == n_parts,
                  "被 400 拒掉的这次没动桶：分片一片不少")
            lie = list(digests)
            lie[1] = sha(b"a different take with the same part length")
            r = await c.post("/upload/complete", params={"upload_id": sid},
                             headers=bearer(tok), json={"parts_sha256": lie})
            check(r.status_code == 422 and "已作废" in r.text,
                  f"第 1 片的本地摘要与桶里字节对不上 → 422（{r.status_code} {r.text[:160]}）")
            left = sorted(g.key.rsplit("/", 1)[-1]
                          for g in await st.objects.list_prefix(prefix))
            check(f"part_{1:05d}" not in left and len(left) == n_parts - 1,
                  f"真桶里只作废对不上的那一片（剩 {left}）")
            s2 = await status(c, tok, sid)
            check(s2["v"].get("missing_parts") == [1],
                  f"缺口由桶的列举如实还回来：{s2['v'].get('missing_parts')}")
            await put_part(c, tok, sid, 1, slice_of(blob, ps, 1))
            r = await c.post("/upload/complete", params={"upload_id": sid}, headers=bearer(tok),
                             json={"parts_sha256": digests})
            check(r.status_code == 200, f"POST /upload/complete 200（{r.status_code} {r.text[:200]}）")
            m = r.json()
            check(m.get("parts_verified") == n_parts,
                  f"{m.get('parts_verified')} 片与本地摘要逐片对上（真 MinIO 报回的指纹）")
            material_id, obj_key = m.get("material_id", ""), m.get("object_key", "")
            check(m.get("parts_cleaned") == n_parts and not m.get("parts_cleanup_pending"),
                  f"拼装完立刻收回 {m.get('parts_cleaned')} 片分片")
            check(m.get("bytes") == total and m.get("kind") == "video",
                  f"素材 {total / 2**20:.2f}MB、kind=video（与 /upload 同一条入库出口）")
            dur, has_audio = m.get("duration"), m.get("has_audio")
            check(bool(dur) and float(dur) > 4 and has_audio is True,
                  f"ingest 在拼出来的字节上真探到内容：时长 {dur}s、有音轨 {has_audio}")

            info = await st.objects.head(obj_key)
            check(info is not None and info.bytes == total,
                  f"成品在 MinIO：{obj_key}（{info.bytes if info else 0} 字节）")
            back = await st.objects.localize(obj_key, tmp / "pull")
            check(sha(Path(back).read_bytes()) == whole,
                  "从桶里取回的字节与本机源文件 sha256 全等（跨进程拼装无一字节错位）")
            try:
                p = await probe_meta(back)
                ok, note = (float(p.get("duration_sec") or 0) > 4 and p.get("has_audio") is True,
                            f"{p.get('duration_sec')}s {p.get('width')}x{p.get('height')} "
                            f"codec={p.get('codec')} 音轨={p.get('has_audio')}")
            except Exception as e:  # noqa: BLE001
                ok, note = False, f"ffprobe 失败：{e}"
            check(ok, f"新工作区里 ffprobe 读得出这节料（剪得动的料）：{note}")
            check(await st.objects.list_prefix(prefix) == [],
                  "complete 后该会话的 uploads/ 前缀在真桶里已空")
            row = await st.db.get_by_pk("upload_sessions", {"id": sid})
            check(row is not None and row["status"] == "completed"
                  and row["material_id"] == material_id,
                  f"账本行标 completed 且回填 material_id（实得 {row and row['status']}）")
            db_mat = await st.db.get_by_pk("materials", {"id": material_id})
            check(db_mat is not None and db_mat["conv_id"] == CONV
                  and db_mat["owner_user_id"] == uid and db_mat.get("origin") == "upload",
                  "materials 行归对人对会话、origin=upload")

            r = await status(c, tok2, sid)
            check(r["http"] == 404, f"别人的 token 问这条 upload_id → 404（实得 {r['http']}）")

            print("\n--- ④ 真库把账本每条 SQL 走一遍 ---", flush=True)
            live = await st.upload_sessions.find_live(uid, CONV, "航拍原片.mp4", total, ps, whole)
            check(live is None, "find_live 只认 status=uploading：已 completed 的那条不复用")
            r = await c.post("/upload/init", params={
                "conversation_id": CONV, "filename": "另一半.mp4",
                "size": total, "sha256": whole}, headers=bearer(tok))
            sid_other = r.json()["upload_id"]
            check(sid_other != sid, "换了文件名就是新开一条会话（不复用已完成的那条）")
            await put_part(c, tok, sid_other, 0, slice_of(blob, ps, 0))
            got = await st.objects.list_prefix(uploads.session_prefix(uid, CONV, sid_other))
            check(len(got) == 1, "touch/open 之后桶里确实只有那一片（touch 不动字节）")
            r = await c.post("/upload/abort", params={"upload_id": sid_other}, headers=bearer(tok))
            check(r.status_code == 200 and r.json().get("aborted") is True
                  and r.json().get("parts_cleaned") == 1, f"abort 删净分片（{r.text[:120]}）")
            check(await st.objects.list_prefix(uploads.session_prefix(uid, CONV, sid_other)) == [],
                  "abort 后该前缀在真桶里为空")
            check(await st.db.get_by_pk("upload_sessions", {"id": sid_other}) is None,
                  "abort 连账本行一起删（drop 走过真 PG）")

            # 放弃的会话：靠过期清扫回收。max_age_sec=-1 让「刚写的行」也算过期，
            # 因此放在所有断言之后执行——这一句会把整表按 updated_at 扫一遍。
            r = await c.post("/upload/init", params={
                "conversation_id": CONV, "filename": "半途而废.mp4",
                "size": total}, headers=bearer(tok))
            sid_lazy = r.json()["upload_id"]
            aborted.append(sid_lazy)
            await put_part(c, tok, sid_lazy, 0, slice_of(blob, ps, 0))
            swept = await uploads.sweep_upload_sessions(st, max_age_sec=-1)
            check(swept["swept"] >= 1 and swept["parts_cleaned"] >= 1,
                  f"过期清扫（真 PG 的 expired 查询）回收 {swept['swept']} 条会话、"
                  f"{swept['parts_cleaned']} 片字节")
            check(await st.db.get_by_pk("upload_sessions", {"id": sid_lazy}) is None
                  and await st.objects.list_prefix(
                      uploads.session_prefix(uid, CONV, sid_lazy)) == [],
                  "半途而废的那条：行与桶里的片都没了")
    finally:
        print("\n--- ⑤ 收回自建数据 ---", flush=True)
        for svc in (svc1, svc2):
            svc.kill()
        residue = await _cleanup(st, uid, uid2, CONV, material_id, obj_key, sid, aborted)
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n自建数据残留复核：{residue}", flush=True)
        await st.close()

    print(f"\n{'全部通过' if not FAILS else f'{len(FAILS)} 项失败'}", flush=True)
    for f in FAILS:
        print(f"  FAIL {f}", flush=True)
    return 1 if FAILS else 0


async def _cleanup(st, uid: str, uid2: str, conv: str, material_id: str, obj_key: str,
                   sid: str, extra_sessions: list[str]) -> str:
    """删净自建的素材行/账本行/身份，并复核桶里与库里都不留痕。"""
    ids = [i for i in [sid, *extra_sessions] if i]
    for sid_ in ids:
        try:
            for info in await st.objects.list_prefix(uploads.session_prefix(uid, conv, sid_)):
                await st.objects.delete(info.key)
        except Exception as e:  # noqa: BLE001
            print(f"  [清理] 分片 {sid_}：{e}", flush=True)
    try:
        if obj_key:
            await st.objects.delete(obj_key)
        for row in await st.db.select("upload_sessions", where={"owner_user_id": uid}):
            await st.db.delete("upload_sessions", where={"id": row["id"]})
        for row in await st.db.select("upload_sessions", where={"owner_user_id": uid2}):
            await st.db.delete("upload_sessions", where={"id": row["id"]})
        if material_id:
            await st.materials.drop(uid, material_id)
        for one in (uid, uid2):
            if one:
                await st.conversations.drop(one, conv)
                await st.db.delete("users", where={"id": one})
    except Exception as e:  # noqa: BLE001
        print(f"  [清理] 行/身份：{e}", flush=True)
    left_sessions = await st.db.count("upload_sessions", where={"owner_user_id": uid})
    left_objs = [i.key for i in await st.objects.list_prefix(f"uploads/{uid}/")]
    left_mat = bool(material_id) and await st.db.get_by_pk("materials", {"id": material_id})
    left_user = bool(uid) and await st.db.get_by_pk("users", {"id": uid})
    return (f"账本 {left_sessions} / 分片对象 {left_objs} / "
            f"素材 {'在' if left_mat else '无'} / 身份 {'在' if left_user else '无'}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
