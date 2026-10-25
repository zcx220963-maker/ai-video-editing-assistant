"""BGM 曲库链路（离线，桩掉外站，不发子进程）。

钉住的事实（这条链曾整条断掉两次，而且都是**静默**的）：

① 在线搜歌/取链/下载**不能 shell out 到 curl**：镜像是 python:3.12-slim，里面没有 curl。
   原先 `_gd_music_get` 调 subprocess 失败 → 每个源的异常被逐源 `continue` 吞掉 →
   `/bgm/search` 恒返 200 + 空列表，前端看起来像「没这首歌」而不是「服务坏了」。
   外站 403 的真实原因是 User-Agent 黑名单，不是 SSL，所以 urllib 带浏览器 UA 就够。
② `/bgm/import` 传 `conversation_id=None`（曲库不挂会话）：ingest_bytes 原先无条件
   `conversations.ensure(user_id, None)` → 主键为空抛 ValueError → 500，一首歌都进不来。
③ 曲库的可见性只认 owner：`list_visible(origin="bgm")` 本人查得到、别人查不到；
   删除同时清桶里的字节与 materials 行。

运行：  python tests/test_bgm_library.py
"""

from __future__ import annotations

import asyncio
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
import tempfile
import urllib.error
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework import server as srv
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


def make_agent(storage: Storage) -> Agent:
    return Agent(
        llm=ScriptedLLM(steps=[("answer", "x")]), registry=ToolRegistry(), session_manager=SessionManager(storage),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=1), storage=storage,
    )


class FakeResp:
    """urlopen 的替身：只关心 read() 与请求头。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_fake_urlopen(payload: bytes) -> dict:
    """把 urlopen 换成假实现，记下发出的请求（含 headers）。"""
    seen: dict = {}
    real = srv.urllib.request.urlopen

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        return FakeResp(payload)

    srv.urllib.request.urlopen = fake
    seen["real"] = real
    return seen


def _boom(params, timeout=15.0):
    raise RuntimeError("音乐源不可达")


def main() -> None:
    # ---- ① 音乐源不再依赖 curl ----
    print("① 在线音乐源：纯 urllib，镜像里没 curl 也能跑")
    src = Path(srv.__file__).read_text(encoding="utf-8")
    music_block = src[src.index("GD_MUSIC_API"): src.index("async def _attachment_views")]
    check("subprocess" not in music_block and "curl" not in music_block,
          f"音乐 API 那一段里没有 subprocess/curl（片段长 {len(music_block)}）")
    check("import subprocess" not in src, "server 模块不再 import subprocess")

    payload = b'[{"id":"1","name":"\xe4\xbb\xa5\xe6\xa2\xa6\xe4\xb8\xba\xe9\xa9\xac",' \
              b'"artist":["\xe6\x9f\xb3\xe6\xa2\xb3"],"album":"a","source":"netease"}]'
    seen = install_fake_urlopen(payload)
    got = srv._gd_music_get({"types": "search", "source": "netease", "name": "以梦为马"})
    check(isinstance(got, list) and got[0]["name"] == "以梦为马",
          f"搜索返回解析好的曲目列表（{type(got).__name__}）")
    check(str(seen.get("ua", "")).startswith("Mozilla/"),
          f"带浏览器 User-Agent（实得 {seen.get('ua')!r}）——该站按 UA 黑名单挡默认客户端")
    dl_seen = install_fake_urlopen(b"ID3\x04fake-audio")
    body = srv._gd_download("https://cdn.example/x.mp3")
    check(body == b"ID3\x04fake-audio", "下载走同一条 urllib 出口，返回原始字节")
    check(dl_seen.get("ua", "") == seen.get("ua", ""), "下载请求也带同一个浏览器 UA")
    srv.urllib.request.urlopen = seen["real"]

    # ---- ② 导入链路：桩掉外站，真走 ingest + PG/桶（内存替身） ----
    print("② /bgm/import：曲库素材不挂会话，仍能落库并出现在 /bgm")
    mp3 = b"ID3" + b"\x00" * 4096
    real_music_get, real_download = srv._gd_music_get, srv._gd_download
    calls: dict = {"sources": []}

    def fake_music_get(params, timeout=15.0):
        if params.get("types") == "url":
            return {"url": "https://cdn.example/x.mp3"}
        calls["sources"].append(params.get("source"))
        return [{"id": "1", "name": "以梦为马", "artist": ["柳彬"], "album": "a",
                 "pic_id": "", "url_id": "1", "lyric_id": "1", "source": params["source"]}]

    srv._gd_music_get = fake_music_get
    srv._gd_download = lambda url, timeout=60.0: mp3

    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                               workspace_root=Path(td) / "ws")
        app = create_app(make_agent(storage), InMemoryMessageQueue(), storage=storage,
                         upload_sweep_sec=0)
        with TestClient(app) as c:
            tok = c.post("/register", json={}).json()["token"]
            hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
            other = c.post("/register", json={}).json()["token"]
            hdr_other = {"Authorization": f"Bearer {other}"}

            r = c.get("/bgm/search", params={"q": "以梦为马"}, headers=hdr)
            sj = r.json()
            check(r.status_code == 200 and sj["total"] == 3,
                  f"/bgm/search 聚合三源（{r.status_code}, total={sj.get('total')}）")
            check(calls["sources"] == ["joox", "netease", "bilibili"],
                  f"按 joox→netease→bilibili 依次问（实得 {calls['sources']}）")
            check(sj["tracks"][0].get("track_id") == "1"
                  and sj["tracks"][0].get("artist") == "柳彬",
                  f"字段归一成前端形状 track_id/artist（实得 {sj['tracks'][0]}）")

            imp = c.post("/bgm/import", headers=hdr,
                         json={"track_id": "1", "source": "netease",
                               "name": "以梦为马", "artist": "柳彬"})
            check(imp.status_code == 200,
                  f"/bgm/import 200（实得 {imp.status_code} {imp.text[:160]}）")
            mid = imp.json().get("material_id", "")
            check(imp.json().get("filename") == "柳彬 - 以梦为马.mp3",
                  f"文件名按「艺人 - 歌名」拼（{imp.json().get('filename')}）")

            row = run(storage.materials.get(mid))
            check(row is not None and row["conv_id"] is None,
                  "materials 行不挂会话（conv_id 为 NULL）")
            check(row["origin"] == "bgm" and row["kind"] == "audio",
                  f"origin/kind 落对（{row['origin']}/{row['kind']}）")
            uid = row["owner_user_id"]
            check(not run(storage.conversations.list_for(uid)),
                  "没有为曲库凭空造会话行")

            lst = c.get("/bgm", headers=hdr).json()
            check(lst["total"] == 1 and lst["bgm"][0]["material_id"] == mid,
                  f"GET /bgm 列出这首歌（total={lst['total']}）")
            check(bool(lst["bgm"][0]["url"]), "列表带回可直接播放的签名直链")
            check(c.get("/bgm", headers=hdr_other).json()["total"] == 0,
                  "别人的曲库看不到这首歌")

            # ---- ②-b 音频只进音乐库，不进素材库（素材库只答「哪条视频/图能复用」） ----
            print("②-b 素材库不收音频：上传的 mp3 也归音乐库")

            async def _register(name: str, kind: str) -> str:
                key = f"materials/{uid}/{name}"
                async def _chunks():
                    yield b"\x00" * 16
                await storage.objects.put(key, _chunks(),
                                          content_type="application/octet-stream")
                row2 = await storage.materials.register(
                    uid, None, key, name, kind, bytes_=16, sha256="0" * 64)
                return row2["id"]

            up_audio = run(_register("本地配乐.mp3", "audio"))
            clip = run(_register("采访.mp4", "video"))

            mats = c.get("/materials", headers=hdr).json()["materials"]
            check([m["material_id"] for m in mats] == [clip],
                  f"GET /materials 只有视频，音频不出现（实得 {[m.get('filename') for m in mats]}）")
            songs = c.get("/bgm", headers=hdr).json()
            ids = [b["material_id"] for b in songs["bgm"]]
            check(songs["total"] == 2 and up_audio in ids and mid in ids,
                  f"GET /bgm 同时列出曲库导入与本地上传（{songs['total']} 首）")
            origins = {b["material_id"]: b.get("origin") for b in songs["bgm"]}
            check(origins.get(up_audio) == "upload" and origins.get(mid) == "bgm",
                  f"带 origin 让前端分清来源（{origins}）")

            dele = c.delete(f"/bgm/{mid}", headers=hdr)
            check(dele.status_code == 200 and dele.json()["deleted"] == 1,
                  f"DELETE /bgm/{{id}} 删掉行（{dele.json()}）")
            check(run(storage.objects.head(row["object_key"])) is None,
                  "删除把桶里的字节也清走，不留孤儿对象")

            # ---- ③ 三源全挂时必须报错，不能伪装成「没这首歌」 ----
            print("③ 音乐源调不通 ≠ 搜不到歌")
            srv._gd_music_get = _boom
            down = c.get("/bgm/search", params={"q": "以梦为马"}, headers=hdr)
            check(down.status_code == 502 and "音乐源均无响应" in down.text
                  and "音乐源不可达" in down.text,
                  f"三源全挂 → 502 带原因（{down.status_code} {down.text[:130]}）")

            # 挂一个源、其余照常：结果照给，但原因要带状态码（403 与 503 处置完全不同）
            def _joox_403(params, timeout=15.0):
                if params.get("source") == "joox":
                    raise urllib.error.HTTPError("https://x/api.php", 403, "Forbidden", {}, None)
                return fake_music_get(params, timeout)

            srv._gd_music_get = _joox_403
            part = c.get("/bgm/search", params={"q": "以梦为马"}, headers=hdr)
            pj = part.json()
            check(part.status_code == 200 and pj["total"] == 2
                  and "joox: HTTP 403" in pj["hint"],
                  f"单源 403：结果照出，hint 里带状态码（{pj.get('hint')}）")
            srv._gd_music_get = fake_music_get
        run(storage.close())

    # ---- ④ 直链主机名：浏览器要解析得到 ----
    print("④ presigned 直链按浏览器侧主机名签（容器内的服务名浏览器看不见）")
    from agent_framework.storage.object_store import MinioObjectStore

    def host_of(url: str) -> str:
        return url.split("//", 1)[1].split("/", 1)[0]

    # region 显式给死：不给的话 SDK 会为了查桶区域真去连 endpoint，
    # 而本机测试环境里 "minio" 这个主机名解析不到（容器内才解析得到）。
    inner = MinioObjectStore("minio:9000", "ak", "sk", "creation-assets",
                             region="us-east-1")
    u1 = run(inner.presign_get("users/u/convs/c/mat-x.mp3"))
    check(host_of(u1) == "minio:9000", f"不配 public_endpoint 时行为不变（{host_of(u1)}）")
    outer = MinioObjectStore("minio:9000", "ak", "sk", "creation-assets",
                             public_endpoint="127.0.0.1:9000")
    u2 = run(outer.presign_get("users/u/convs/c/mat-x.mp3"))
    check(host_of(u2) == "127.0.0.1:9000",
          f"配了 public_endpoint → 直链主机换成浏览器解析得到的（{host_of(u2)}）")
    check("X-Amz-Signature=" in u2 and "X-Amz-Signature=" in u1,
          "两条都是签好的直链（签名是本地计算，不要求那个地址容器可达）")

    # ---- ⑤ 附件说明：曲库歌曲不许被当成画面素材塞进 load_media ----
    print("⑤ 附件事实行按素材类型给用途说明")
    from agent_framework.context import render_attachments_section

    song = {"id": "mat-a1b2c3", "filename": "柳彬 - 以梦为马.mp3", "kind": "audio"}
    clip = {"id": "mat-d4e5f6", "filename": "采访.mp4", "kind": "video"}
    only_song = render_attachments_section([song], []) or ""
    check("load_media(material_ids" not in only_song and "bgm 参数" in only_song,
          f"只挂了一首歌时不再要求「第一步全传给 load_media」（实得片段："
          f"{only_song[-90:]!r}）")
    both = render_attachments_section([clip, song], []) or ""
    check("load_media(material_ids" in both and "**不要**当画面素材" in both,
          "视频 + 歌曲同时在场：两条说明并存，各走各的出口")

    # 2026-10-25：原第 ⑥ 节「出片节点的 bgm 收附件/曲库的 material_id」删了。
    # 它测的是 `motion_code_nodes.RenderCodeMotionVideoNode._bgm_ref`，而那个节点
    # 连同整个图形科普链一起退役了。曲库本身的判据在 ①~⑤ 节，一节没少。
    srv._gd_music_get, srv._gd_download = real_music_get, real_download

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
