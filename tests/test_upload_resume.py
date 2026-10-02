"""大文件分片续传（离线，内存存储替身 + FastAPI TestClient，不联网、本地磁盘不经手）。

钉住的事实：
① 切法由服务端定并回给客户端，片长按账本推算校验（短收、超长都当场拒，且不留脏对象）；
② **续传状态以桶为准**：已有哪几片问 list_prefix，所以 init 重发能接回同一条会话，
   换一个 Storage 实例（= 换副本）也看得见同一份进度；
③ 收齐后按序拼流走 ingest_bytes ——与 /upload 同一条入库出口，素材形状完全一致；
④ 内容校验两道：逐片 sha256（不符退 422 并重传该片）、complete 带按序逐片摘要
   （与桶里真存下的字节逐片比，对不上只作废那几片）；init 声明整文件 sha256 是
   脚本/工具侧更强的第三种核法（拼装时整条指纹再核一遍）；
⑤ 归属与鉴权：token 反查身份，别人的 upload_id 一律 404（不区分不存在与无权）；
⑥ 收尾：complete 后分片即删、重复 complete 幂等、abort 删净字节、过期清扫兜底回收。

运行：  python tests/test_upload_resume.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import hashlib
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework import uploads
from agent_framework.agent import Agent, AgentConfig
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.llm import ScriptedLLM
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import Storage, build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def run(coro):
    return asyncio.run(coro)


def storage_in(td: str) -> Storage:
    return build_storage("memory", cache_root=Path(td) / "cache",
                         workspace_root=Path(td) / "ws")


def make_agent(storage: Storage) -> Agent:
    return Agent(
        llm=ScriptedLLM(steps=[("answer", "x")]),
        registry=ToolRegistry(),
        session_manager=SessionManager(storage),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=3),
        storage=storage,
    )


def register(client: TestClient) -> tuple[str, dict[str, str]]:
    j = client.post("/register", json={}).json()
    return j["user_id"], {"Authorization": f"Bearer {j['token']}"}


# 3.5MB 的确定性字节：够跨 4 片（MIN_PART_BYTES=1MB），又不拖慢离线套件
TOTAL = 3670016
BLOB = (bytes(range(256)) * (TOTAL // 256))[:TOTAL]
SHA = hashlib.sha256(BLOB).hexdigest()


def slice_part(i: int, ps: int) -> bytes:
    return BLOB[i * ps:(i + 1) * ps]


def init(client: TestClient, hdr: dict, *, conv: str = "c_up", name: str = "无人机航拍.mp4",
         size: int = TOTAL, **kw):
    return client.post("/upload/init",
                       params={"conversation_id": conv, "filename": name, "size": size, **kw},
                       headers=hdr)


def put(client: TestClient, hdr: dict, upload_id: str, i: int, blob: bytes, sha: str = ""):
    q = {"upload_id": upload_id, "part": i}
    if sha:
        q["sha256"] = sha
    return client.put("/upload/part", params=q, content=blob, headers=hdr)


def keys_under(storage: Storage, prefix: str) -> list[str]:
    return run(_keys_under(storage, prefix))


async def _keys_under(storage: Storage, prefix: str) -> list[str]:
    return [i.key for i in await storage.objects.list_prefix(prefix)]


def main() -> None:
    # ---- ① 纯函数：切法推算与键布局 ----
    print("① 切法由服务端定：片长、片数、末片余数")
    check(uploads.pick_part_size(500_000) == uploads.MIN_PART_BYTES,
          "小文件一片走完（默认片长 1MB 已大于文件本身）")
    check(uploads.pick_part_size(300 * 1024 * 1024) == 16 * uploads.MIN_PART_BYTES,
          "大文件按 16MB 起切")
    check(uploads.pick_part_size(TOTAL, 64) == uploads.MIN_PART_BYTES,
          "客户端要 64B 一片被夹到下限：请求数不能失控")
    check(uploads.pick_part_size(20 * uploads.MIN_PART_BYTES, 10 ** 9) == uploads.MAX_PART_BYTES,
          "客户端要 1GB 一片被夹到上限")
    check(uploads.pick_part_size(900 * 1024 ** 3) == uploads.MAX_PART_BYTES,
          "近 1TB 直接抬到最大片长")
    plan = {"part_count": 4, "part_size": 1000, "total_bytes": 3500}
    check([uploads.expected_part_bytes(plan, i) for i in range(4)] == [1000, 1000, 1000, 500],
          "除末片外等长，末片是余数（可推算，无需逐片记账）")
    got = uploads.session_prefix("u-1", "c-abc", "up-abc")
    check(got == "uploads/u-1/convs/c-abc/up-abc/",
          f"分片前缀按「谁的哪会话的哪次上传」分格（实得 {got}）")
    sneaky = uploads.session_prefix("u-1", "../../w/in", "up-abc")
    check(".." not in sneaky and sneaky.startswith("uploads/u-1/convs/")
          and sneaky.count("/") == 5, f"会话 id 里的目录穿越剥掉了（实得 {sneaky}）")
    check(uploads.part_key("u-1", "c", "up-abc", 7) == "uploads/u-1/convs/c/up-abc/part_00007",
          "片号定宽补零：字典序 == 数值序，列举回来就能按序拼")
    check(issubclass(uploads.UploadRejected, uploads.IngestRejected),
          "UploadRejected 是 IngestRejected 的子类：端点层一个 except 就够")

    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        app = create_app(make_agent(storage), InMemoryMessageQueue(), storage=storage,
                         max_upload_mb=8, upload_sweep_sec=0)      # 后台清扫单独一节再验
        with TestClient(app) as c:
            uid, hdr = register(c)
            other, hdr_other = register(c)

            # ---- ② init ----
            print("② POST /upload/init 定下切法并回权威 part_size/part_count")
            r = init(c, hdr)
            check(r.status_code == 200, f"init 200（{r.status_code} {r.text[:160]}）")
            v = r.json()
            sid, ps, last = v["upload_id"], v["part_size"], v["part_count"] - 1
            check(sid.startswith("up-") and v["part_count"] == -(-TOTAL // ps) and last == 3,
                  f"回 upload_id 与 part_count={v['part_count']}（按 {ps // 1024 ** 2}MB 切）")
            check(v["missing_parts"] == list(range(v["part_count"])) and v["received_bytes"] == 0,
                  "起手全缺：missing_parts 就是该传的片号清单")
            check(v["filename"] == "无人机航拍.mp4" and v["size"] == TOTAL
                  and v["status"] == "uploading", "文件名/总长/状态回给客户端（续传要靠它复现同一签名）")
            check(app.state.upload_sweep is None, "upload_sweep_sec<=0 时不起后台清扫")

            # ---- ③ 乱序传片 + 中途问进度 ----
            print("③ PUT /upload/part 逐片直写对象存储，片长当场核")
            ok_last = put(c, hdr, sid, last, slice_part(last, ps))
            check(ok_last.status_code == 200
                  and ok_last.json()["bytes"] == len(slice_part(last, ps)),
                  f"先传末片（{len(slice_part(last, ps))} 字节）：短收的末片合法")
            p0 = slice_part(0, ps)
            ok0 = put(c, hdr, sid, 0, p0, hashlib.sha256(p0).hexdigest())
            check(ok0.status_code == 200 and ok0.json()["sha256"] == hashlib.sha256(p0).hexdigest(),
                  "带该片 sha256 时逐片核对通过")
            mid = c.get(f"/upload/status?upload_id={sid}", headers=hdr).json()
            check([p["index"] for p in mid["parts"]] == [0, last]
                  and mid["missing_parts"] == list(range(1, last)),
                  f"status 以桶的列举为准（已有 {len(mid['parts'])} 片、还差 {len(mid['missing_parts'])} 片）")
            check(0 < mid["percent"] < 100, f"进度按已收字节算出 {mid['percent']}%")

            # ---- ④ 拒：越界片号 / 短收 / 超长 / 坏指纹 / 越权 ----
            print("④ 坏请求当场拒，且桶里不留脏对象")
            check(put(c, hdr, sid, 99, b"x").status_code == 400, "片号越界回 400")
            check(put(c, hdr, sid, 1, b"too short").status_code == 400,
                  "短收回 400（绝不挂着一条永远拼不齐的账）")
            check(put(c, hdr, sid, 1, b"Z" * (ps + 10)).status_code == 413,
                  "超过账本推算的片长回 413")
            bad = put(c, hdr, sid, 1, slice_part(1, ps), "0" * 64)
            check(bad.status_code == 422, f"该片 sha256 不符回 422（{bad.json().get('detail')}）")
            left = sorted(k.rsplit("part_", 1)[1]
                          for k in keys_under(storage, uploads.session_prefix(uid, "c_up", sid)))
            check(left == ["00000", f"{last:05d}"],
                  f"被拒的三次都没在桶里留下半片（实有 {left}）")
            check(put(c, hdr_other, sid, 1, slice_part(1, ps)).status_code == 404,
                  "别人的 upload_id 回 404（不区分不存在与无权）")
            check(c.get(f"/upload/status?upload_id={sid}").status_code == 401,
                  "无凭证的续传问询回 401")
            check(put(c, hdr, "up-nope", 0, b"x").status_code == 404, "不存在的 upload_id 回 404")

            # ---- ⑤ 断网重连 / 换副本 ----
            print("⑤ 刷新页面或断网重连：同参数再 init 接回同一条账")
            again = init(c, hdr).json()
            check(again["resumed"] is True and again["upload_id"] == sid,
                  "复用同一 upload_id（不另起一份账、不把旧分片丢成孤儿）")
            check([p["index"] for p in again["parts"]] == [0, last],
                  "已有分片照常报回来：客户端只需补缺的")
            twin = Storage(storage.backend, storage.db, storage.objects, storage.workspace)
            tv = run(uploads.upload_status(twin, user_id=uid, upload_id=sid))
            check([p["index"] for p in tv["parts"]] == [0, last] and tv["upload_id"] == sid,
                  "换实例（同一桶同一库）问同一份进度：结果一致，续传不依赖原进程")
            try:
                run(uploads.upload_status(twin, user_id=other, upload_id=sid))
                check(False, "别人问这条会话应当被拒")
            except uploads.UploadRejected as e:
                check(e.status == 404, f"服务层同样按归属拒外人问询（{e.status}）")

            # ---- ⑥ 缺片被拒 → 补齐 → 拼装入库 ----
            print("⑥ 收齐 → 按序拼流 → 与 /upload 同一条 ingest_bytes 入库")
            inc = c.post(f"/upload/complete?upload_id={sid}", headers=hdr)
            check(inc.status_code == 409 and "还差" in inc.json()["detail"],
                  f"缺片时 complete 回 409 并说清差几片（{inc.json().get('detail')}）")
            check(run(storage.upload_sessions.get(sid))["status"] == "uploading",
                  "被拒的 complete 不动账本：补齐后可以再 complete")
            for i in range(1, last):
                check(put(c, hdr, sid, i, slice_part(i, ps)).status_code == 200, f"补齐第 {i} 片")
            done = c.post(f"/upload/complete?upload_id={sid}", headers=hdr)
            check(done.status_code == 200, f"complete 200（{done.status_code} {done.text[:200]}）")
            m = done.json()
            check(m["object_key"] == f"users/{uid}/convs/c_up/{m['material_id']}.mp4",
                  "素材对象键布局与 /upload 完全一致（分片只是搬运方式，不是第二种入库语义）")
            check(m["parts_cleaned"] == v["part_count"] and m["parts_cleanup_pending"] is False,
                  f"入库后 {m['parts_cleaned']} 片分片立即删除")
            check(keys_under(storage, "uploads/") == [], "桶里 uploads/ 前缀已空")
            check(m["url"] and m["kind"] == "video" and m["bytes"] == TOTAL,
                  "回 material_id + presigned URL + 总字节：与直传响应同形状")

            async def _assembled_is_the_source() -> None:
                got = b"".join([b async for b in storage.objects.get_stream(m["object_key"])])
                check(got == BLOB, f"拼装结果与源文件逐字节相同（{len(got)} 字节，乱序上传不影响顺序）")
                row = await storage.materials.get(m["material_id"])
                check(row is not None and row["sha256"] == SHA and row["origin"] == "upload"
                      and row["owner_user_id"] == uid,
                      "materials 行的 sha256 由整文件算出：入库侧也没有第二条路径")
                check(row is not None and "path" not in row, "数据里没有任何本地路径字段")
            run(_assembled_is_the_source())

            # ---- ⑦ 幂等与终态 ----
            print("⑦ 重复 complete 幂等；已完成不再收片")
            twice = c.post(f"/upload/complete?upload_id={sid}", headers=hdr)
            check(twice.status_code == 200 and twice.json()["material_id"] == m["material_id"]
                  and twice.json().get("already_completed") is True,
                  "响应丢了的客户端重发 complete，拿回同一条素材而不是一个错误")
            check(put(c, hdr, sid, 0, slice_part(0, ps)).status_code == 409,
                  "已入库的会话不再收片（要重传请重新 init）")
            st = c.get(f"/upload/status?upload_id={sid}", headers=hdr).json()
            check(st["status"] == "completed" and st["percent"] == 100 and st["missing_parts"] == [],
                  "完成态的 status 不谎报「全缺」：percent 100、missing 为空")
            lib = c.get("/materials?conversation_id=c_up", headers=hdr).json()["materials"]
            check([x["material_id"] for x in lib] == [m["material_id"]],
                  "分片上传的素材出现在素材列表里（与直传无差别）")

            # ---- ⑧ 整文件指纹 ----
            print("⑧ init 给整文件 sha256：逐片合法也要整体核得上")
            good = init(c, hdr, conv="c_sha", name="a.mp4", size=TOTAL, sha256=SHA).json()
            check(good["sha256"] == SHA, "声明的整文件指纹回显")
            for i in range(good["part_count"]):
                put(c, hdr, good["upload_id"], i, slice_part(i, good["part_size"]))
            oksha = c.post("/upload/complete?upload_id=" + good["upload_id"], headers=hdr)
            check(oksha.status_code == 200, f"指纹相符 → 入库 200（{oksha.status_code} {oksha.text[:120]}）")
            lie = init(c, hdr, conv="c_lie", name="a.mp4", size=TOTAL, sha256="f" * 64).json()
            for i in range(lie["part_count"]):
                put(c, hdr, lie["upload_id"], i, slice_part(i, lie["part_size"]))
            nope = c.post("/upload/complete?upload_id=" + lie["upload_id"], headers=hdr)
            check(nope.status_code == 422 and "sha256" in nope.json()["detail"],
                  f"整文件指纹不符回 422（{nope.json().get('detail')}）")
            check(run(storage.db.select("materials", where={"conv_id": "c_lie"})) == [],
                  "指纹不符的绝不登记成素材：宁可退回重传")
            check(keys_under(storage, uploads.session_prefix(uid, "c_lie", lie["upload_id"]))
                  != [], "被拒的这次分片仍在桶里：账本未动，修好后补齐即可，不必从头再来")

            # ---- ⑧b complete 带 parts_sha256：逐片内容比对（浏览器路径上的第二道校验）----
            print("⑧b POST /upload/complete 带 parts_sha256：与桶里真存下的字节逐片比")
            ch = init(c, hdr, conv="c_chain", name="e.mp4", size=TOTAL).json()
            cps, cn = ch["part_size"], ch["part_count"]
            digests = [hashlib.sha256(slice_part(i, cps)).hexdigest() for i in range(cn)]
            for i in range(cn):
                # 第 1 片传「同名同尺寸的另一份文件」留下的字节：长度完全合规，
                # 逐片 PUT 时又没带指纹——这正是 init 会复用同一条账带来的真实风险。
                blob = b"\x00" * len(slice_part(i, cps)) if i == 1 else slice_part(i, cps)
                check(put(c, hdr, ch["upload_id"], i, blob).status_code == 200,
                      f"第 {i} 片收下（片长与总长都核得上）")
            r = c.post("/upload/complete?upload_id=" + ch["upload_id"], headers=hdr,
                       json={"parts_sha256": [d.upper() for d in digests]})
            check(r.status_code == 422 and "已作废" in r.json()["detail"]
                  and "1" in r.json()["detail"],
                  f"第 1 片与本地摘要对不上：回 422 并点名（{r.json().get('detail')}）")
            left = sorted(k.rsplit("part_", 1)[1]
                          for k in keys_under(storage, uploads.session_prefix(uid, "c_chain",
                                                                              ch["upload_id"])))
            check(left == [f"{i:05d}" for i in range(cn) if i != 1],
                  f"只作废对不上的那一片，其余不必重传（桶里剩 {left}）")
            check(run(storage.db.select("materials", where={"conv_id": "c_chain"})) == [],
                  "比对失败的不登记成素材：宁可退回补传")
            check(run(storage.upload_sessions.get(ch["upload_id"]))["status"] == "uploading",
                  "账本仍是 uploading：这次失败不该把已传的 3 片一起作废")
            back = c.get("/upload/status?upload_id=" + ch["upload_id"], headers=hdr).json()
            check(back["missing_parts"] == [1],
                  f"缺口如实还回来，客户端照着补就能续（missing_parts={back['missing_parts']}）")
            check(put(c, hdr, ch["upload_id"], 1, slice_part(1, cps)).status_code == 200,
                  "按 missing_parts 补回第 1 片")
            fixed = c.post("/upload/complete?upload_id=" + ch["upload_id"], headers=hdr,
                           json={"parts_sha256": digests})
            check(fixed.status_code == 200
                  and fixed.json()["parts_verified"] == cn,
                  f"整列比对通过：parts_verified={fixed.json().get('parts_verified')}/{cn}"
                  f"（大写十六进制也认）")
            check(run(storage.materials.get(fixed.json()["material_id"]))["sha256"] == SHA,
                  "逐片比对过的素材，整文件指纹也与源一致")

            bd = init(c, hdr, conv="c_body", name="f.mp4", size=TOTAL).json()
            for i in range(bd["part_count"]):
                put(c, hdr, bd["upload_id"], i, slice_part(i, bd["part_size"]))
            curl = "/upload/complete?upload_id=" + bd["upload_id"]
            check(c.post(curl, headers=hdr, json={"parts_sha256": digests[:2]}).status_code == 400,
                  "条数不等于 part_count 回 400（少报等于漏检）")
            check(c.post(curl, headers=hdr,
                         json={"parts_sha256": ["z" * 64] * bd["part_count"]}).status_code == 400,
                  "有项不是 64 位十六进制回 400")
            check(c.post(curl, headers=hdr, json={"parts_sha256": "nope"}).status_code == 400,
                  "parts_sha256 给了字符串回 400")
            check(c.post(curl, headers=hdr, json=[1, 2]).status_code == 400,
                  "请求体不是对象回 400")
            check(c.post(curl, headers=hdr, content=b"{oops").status_code == 400,
                  "坏 JSON 回 400，而不是静默当成「没声明」")
            check(run(storage.upload_sessions.get(bd["upload_id"]))["status"] == "uploading"
                  and len(keys_under(storage, uploads.session_prefix(
                      uid, "c_body", bd["upload_id"]))) == bd["part_count"],
                  "被拒的这几次一片都没丢：400 只改响应，不动桶与账本")
            plain = c.post(curl, headers=hdr, content=b"")
            check(plain.status_code == 200 and plain.json()["parts_verified"] == 0,
                  "空请求体 = 不做这道校验：照常入库，但 parts_verified 如实回 0")

            # ---- ⑨ init 侧的闸 ----
            print("⑨ init 的边界：类型、总长、上限、指纹格式、会话归属")
            check(init(c, hdr, name="笔记.txt").status_code == 415, "白名单外类型回 415")
            check(init(c, hdr, size=0).status_code == 400, "size=0 回 400")
            check(init(c, hdr, size=9 * 1024 * 1024).status_code == 413,
                  "超过 --max-upload-mb 上限回 413")
            check(init(c, hdr, sha256="abc").status_code == 400,
                  "指纹不是 64 位十六进制回 400")
            dirty = init(c, hdr, conv="c_x", name="../../evil:shell.mp4")
            check(dirty.status_code == 200 and dirty.json()["filename"] == "evil_shell.mp4",
                  f"文件名净化：穿越与非法字符剥除（实得 {dirty.json().get('filename')}）")
            run(storage.conversations.ensure(other, "c_victim"))
            check(init(c, hdr, conv="c_victim").status_code == 409,
                  "往别人的会话发起上传回 409（归属在收第一片之前就钉死）")

            # ---- ⑩ abort ----
            print("⑩ POST /upload/abort：取消即清字节")
            ab = init(c, hdr, conv="c_abort", name="b.mp4").json()
            put(c, hdr, ab["upload_id"], 0, slice_part(0, ab["part_size"]))
            put(c, hdr, ab["upload_id"], 1, slice_part(1, ab["part_size"]))
            res = c.post("/upload/abort?upload_id=" + ab["upload_id"], headers=hdr)
            check(res.status_code == 200 and res.json()["parts_cleaned"] == 2,
                  f"abort 删掉已传的 {res.json().get('parts_cleaned')} 片")
            check(run(storage.upload_sessions.get(ab["upload_id"])) is None,
                  "账本行一并作废：不留一条永远传不完的计划")
            check(keys_under(storage, uploads.session_prefix(uid, "c_abort", ab["upload_id"])) == [],
                  "取消后 uploads/ 下不再留有该会话的字节")
            check(c.get("/upload/status?upload_id=" + ab["upload_id"], headers=hdr).status_code == 404,
                  "abort 之后再问进度回 404")

            # ---- ⑪ 过期清扫 ----
            print("⑪ sweep_upload_sessions 回收没动静的会话（含成品写好后没删净的分片）")
            stale = init(c, hdr, conv="c_stale", name="c.mp4").json()
            put(c, hdr, stale["upload_id"], 0, slice_part(0, stale["part_size"]))
            check(run(storage.upload_sessions.expired(-1)) != [],
                  "expired(-1) 把刚建的账当成过期（测试拿它当时间快进）")
            swept = run(uploads.sweep_upload_sessions(storage, max_age_sec=-1))
            check(swept["swept"] >= 1 and swept["parts_cleaned"] >= 1,
                  f"清扫报告：会话 {swept['swept']} 条、分片 {swept['parts_cleaned']} 片")
            check(keys_under(storage, f"uploads/{uid}/convs/c_stale/") == [], "分片字节从桶里消失")
            check(run(storage.upload_sessions.get(stale["upload_id"])) is None, "账本行一并删掉")

    # ---- ⑫ 后台清扫任务 ----
    print("⑫ create_app 的后台清扫：启动扫一次 + 按间隔再扫")
    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        app = create_app(make_agent(storage), InMemoryMessageQueue(), storage=storage,
                         upload_ttl_sec=-1, upload_sweep_sec=0.05)
        with TestClient(app) as c:
            uid, hdr = register(c)
            v = init(c, hdr, conv="c_bg", name="d.mp4", size=512_000).json()
            put(c, hdr, v["upload_id"], 0, b"Q" * 512_000)
            deadline = time.time() + 3
            while time.time() < deadline and run(storage.upload_sessions.get(v["upload_id"])):
                time.sleep(0.05)
            check(isinstance(app.state.upload_sweep, dict)
                  and app.state.upload_sweep.get("swept", 0) >= 1,
                  f"app.state.upload_sweep = {app.state.upload_sweep}")
            check(run(storage.upload_sessions.get(v["upload_id"])) is None,
                  "ttl 到点的会话在后台被收走，不需要重启实例")
            check(keys_under(storage, "uploads/") == [], "后台清扫同样把分片字节删净")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
