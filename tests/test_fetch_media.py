"""按链接取素材（fetch_media）离线验证：三条策略 + 五道护栏，全程假网络、假下载器。

运行：  python tests/test_fetch_media.py

离线纪律：`media_fetch._open` 整体换成假传输（不起真连接），`socket.getaddrinfo`
换成假 DNS（内网判定仍跑真代码），yt-dlp 换成写文件的假下载器。真机链接冒烟另跑，
不在本文件里——本文件必须无网可过。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import email.message
import json
import os
import socket
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import media_fetch
from agent_framework.identity import use_identity
from agent_framework.media_fetch import (
    MAX_REDIRECTS,
    FetchPolicy,
    FetchRejected,
    default_ytdlp_runner,
    fetch_media,
)
from agent_framework.storage import build_storage
from agent_framework.tools.fetch_media import FetchMediaTool

# main() 会把 `_open` 换成假传输；重定向用例要跑真身，先留一份引用。
REAL_OPEN = media_fetch._open

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


# --------------------------------------------------------------------------
# 假网络：media_fetch._open 的替身
# --------------------------------------------------------------------------

class Headers:
    def __init__(self, kv: dict[str, str]) -> None:
        self._kv = {k.lower(): v for k, v in kv.items()}

    def get(self, name: str, default: str = "") -> str:
        return self._kv.get(name.lower(), default)


class FakeResp:
    def __init__(self, body: bytes, kv: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = Headers(kv or {})
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._body)
        out, self._body = self._body[:n], self._body[n:]
        return out

    def close(self) -> None:
        self.closed = True


class FakeNet:
    """url → (body, headers)。未登记的路由当作源站故障，避免测试悄悄走真网。"""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.calls: list[str] = []
        self.resps: list[FakeResp] = []

    def add(self, url: str, body: bytes = b"", **headers: str) -> None:
        self.routes[url] = (body, dict(headers))

    def __call__(self, url: str, timeout: float):
        if url not in self.routes:
            raise FetchRejected(502, f"假网络未登记：{url}")
        self.calls.append(url)
        resp = FakeResp(*self.routes[url])
        self.resps.append(resp)
        return resp, url


DNS = {
    "media.example.com": "93.184.216.34",   # 公网地址
    "site.example.com": "93.184.216.35",
    "other.example.com": "93.184.216.36",
    "inside.example.com": "10.0.0.5",        # 解析到内网 → 必须被拦
}
_real_getaddrinfo = socket.getaddrinfo


def fake_getaddrinfo(host, port, *a, **kw):        # noqa: ANN001
    if host in DNS:
        return [(2, 1, 6, "", (DNS[host], 0))]
    return _real_getaddrinfo(host, port, *a, **kw)


POLICY = FetchPolicy(max_mb=512, timeout=5.0)


class Ctx:
    def __init__(self, s, tmp: Path, net: FakeNet) -> None:
        self.s, self.tmp, self.net = s, tmp, net

    @property
    def ws_root(self) -> Path:
        return Path(self.s.workspace.root)

    async def read_back(self, key: str) -> bytes:
        p = await self.s.objects.localize(key, self.ws_root / "_readback")
        return p.read_bytes()

    async def rows(self, user_id: str) -> int:
        return await self.s.db.count("materials", where={"owner_user_id": user_id})


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

async def case_direct_link(ctx: Ctx) -> None:
    uid, cid = "u-direct", "c-direct"
    url = "https://media.example.com/sea/sunset.mp4"
    body = b"FAKE-MP4-" + bytes(range(64))
    ctx.net.add(url, body, **{"Content-Type": "video/mp4",
                              "Content-Length": str(len(body))})
    res = await fetch_media(ctx.s, url, user_id=uid, conversation_id=cid, policy=POLICY)
    check(res["material_id"].startswith("mat-") and res["id"] == res["material_id"],
          "直链回 material_id")
    check(res["kind"] == "video" and res["origin"] == "url", "kind/origin 正确")
    check(res["filename"] == "sunset.mp4" and res["bytes"] == len(body), "文件名与字节数")
    check(res["object_key"] == f"users/{uid}/convs/{cid}/{res['material_id']}.mp4",
          f"对象键布局：{res['object_key']}")
    check(bool(res["url"]), "回了可播 URL")
    check(await ctx.read_back(res["object_key"]) == body, "对象存储里的字节一致")
    row = await ctx.s.db.get_by_pk("materials", {"id": res["material_id"]})
    check(row is not None and row["origin"] == "url", "materials 行落了 origin=url")
    check(ctx.net.calls == [url], "带扩展名直链不嗅探（一次请求）")
    check(all(r.closed for r in ctx.net.resps), "响应句柄全部关闭")
    check(not (ctx.ws_root / "url" / "_probe").exists(), "探测临时目录用完即删")
    # 与 /upload 完全同构：load_media 不需要知道这条素材是爬来的
    ok, denied = await ctx.s.materials.resolve([res["material_id"]], user_id=uid,
                                               conv_id=cid)
    check(len(ok) == 1 and not denied, "material_id 可被 load_media 解析")
    ok, denied = await ctx.s.materials.resolve([res["material_id"]], user_id="u-other")
    check(ok == [] and denied == [res["material_id"]], "别人解析不到（归属）")


async def case_content_type_no_ext(ctx: Ctx) -> None:
    uid, cid = "u-ctype", "c-ctype"
    url = "https://media.example.com/download?id=7"
    body = b"\x00\x00\x00\x18ftypmp42" + b"z" * 128
    ctx.net.add(url, body, **{"Content-Type": "video/mp4"})
    res = await fetch_media(ctx.s, url, user_id=uid, conversation_id=cid, policy=POLICY)
    check(res["kind"] == "video" and res["filename"] == "media.mp4",
          f"无扩展名直链靠 Content-Type 补出 kind（{res['filename']}）")
    check(res["object_key"].endswith(".mp4"), "对象键带上补出的扩展名")
    check(ctx.net.calls.count(url) == 2, "嗅探未命中后仍直取原 URL（共两次请求）")


async def case_content_disposition(ctx: Ctx) -> None:
    uid, cid = "u-cd", "c-cd"
    url = "https://media.example.com/d/9f3"
    ctx.net.add(url, b"ID3" + b"\x00" * 40, **{
        "Content-Type": "audio/mpeg",
        "Content-Disposition":
            "attachment; filename*=UTF-8''%E6%B5%B7%E8%BE%B9%E6%97%A5%E8%90%BD.mp3"})
    res = await fetch_media(ctx.s, url, user_id=uid, conversation_id=cid, policy=POLICY)
    check(res["filename"] == "海边日落.mp3",
          f"Content-Disposition 优先（{res['filename']}）")
    check(res["kind"] == "audio", "音频 kind")


async def case_sniff_og_video(ctx: Ctx) -> None:
    uid, cid = "u-sniff", "c-sniff"
    page = "https://site.example.com/p/123"
    video = "https://site.example.com/v/clip.mp4"
    ctx.net.add(page, b'<html><head><meta property="og:video" content="/v/clip.mp4">'
                      b"</head></html>", **{"Content-Type": "text/html"})
    ctx.net.add(video, b"MP4DATA", **{"Content-Type": "video/mp4"})
    res = await fetch_media(ctx.s, page, user_id=uid, conversation_id=cid, policy=POLICY)
    check(res["filename"] == "clip.mp4" and res["kind"] == "video",
          "og:video 相对地址补成绝对后直取")
    check(ctx.net.calls == [page, video], "先嗅页面再取视频，各一次")


async def case_sniff_video_tag(ctx: Ctx) -> None:
    uid, cid = "u-sniff2", "c-sniff2"
    page = "https://site.example.com/p/456"
    video = "https://other.example.com/a.webm"
    ctx.net.add(page, f'<video><source src="{video}"></video>'.encode(),
                **{"Content-Type": "text/html;charset=utf-8"})
    ctx.net.add(video, b"WEBM", **{"Content-Type": "video/webm"})
    res = await fetch_media(ctx.s, page, user_id=uid, conversation_id=cid, policy=POLICY)
    check(res["filename"].endswith(".webm") and res["kind"] == "video",
          "<video>/<source> 的 src 也能嗅到")


async def case_ytdlp_fallback(ctx: Ctx) -> None:
    uid, cid = "u-ytdlp", "c-ytdlp"
    page = "https://site.example.com/watch/abc"
    ctx.net.add(page, b"<html><body>\xe6\x92\xad\xe6\x94\xbe\xe5\x99\xa8\xe9\xa1\xb5\xe9\x9d\xa2</body></html>",
                **{"Content-Type": "text/html"})
    seen: dict[str, object] = {}

    def fake_ytdlp(url: str, dst_dir: Path, max_bytes: int) -> Path:
        seen.update(url=url, dst=dst_dir, max_bytes=max_bytes)
        dst_dir.mkdir(parents=True, exist_ok=True)
        f = dst_dir / "标题.80B.mp4"
        f.write_bytes(b"YTDLP-BYTES")
        return f

    pol = FetchPolicy(max_mb=8, timeout=5.0, ytdlp=fake_ytdlp)
    res = await fetch_media(ctx.s, page, user_id=uid, conversation_id=cid, policy=pol)
    check(res["kind"] == "video" and res["origin"] == "url", "yt-dlp 产物走同一条入库路径")
    check(await ctx.read_back(res["object_key"]) == b"YTDLP-BYTES", "字节进了对象存储")
    check(seen["url"] == page and seen["max_bytes"] == 8 * 1024 * 1024,
          "兜底拿到原始链接与字节上限")
    work = Path(seen["dst"])       # type: ignore[arg-type]
    check(ctx.ws_root in work.parents, "下载落在临时工作区内")
    check(not work.exists() and not any(work.parent.rglob("*")),
          "finally 整目录回收（本地不留下载产物）")


async def case_ytdlp_no_output(ctx: Ctx) -> None:
    uid, cid = "u-emptydl", "c-emptydl"
    page = "https://site.example.com/watch/none"
    ctx.net.add(page, b"<html></html>", **{"Content-Type": "text/html"})

    def fake_ytdlp(url: str, dst_dir: Path, max_bytes: int) -> Path:
        dst_dir.mkdir(parents=True, exist_ok=True)
        f = dst_dir / "empty.mp4"
        f.write_bytes(b"")            # 下载「成功」但零字节
        return f

    try:
        await fetch_media(ctx.s, page, user_id=uid, conversation_id=cid,
                          policy=FetchPolicy(timeout=5.0, ytdlp=fake_ytdlp))
        check(False, "yt-dlp 空产出应报错")
    except FetchRejected as e:
        check(e.status == 400 and "空素材" in e.message,
              f"空产出 → 入库拒绝（{e.message}）")
    check(await ctx.rows(uid) == 0, "失败不留 materials 行")


async def case_ytdlp_crash_mapped(ctx: Ctx) -> None:
    """下载器抛裸异常（真 yt-dlp 的 DownloadError 家族）也必须翻译成 502，不能变 500。"""
    uid, cid = "u-crash", "c-crash"
    page = "https://site.example.com/watch/boom"
    ctx.net.add(page, b"<html></html>", **{"Content-Type": "text/html"})

    class DownloadError(Exception):
        pass

    def boom(url: str, dst_dir: Path, max_bytes: int) -> Path:
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / "partial.mp4.part").write_bytes(b"junk")     # 崩在半路的残渣
        raise DownloadError("ERROR: video unavailable")

    try:
        await fetch_media(ctx.s, page, user_id=uid, conversation_id=cid,
                          policy=FetchPolicy(timeout=5.0, ytdlp=boom))
        check(False, "下载器崩溃应报错")
    except FetchRejected as e:
        check(e.status == 502 and "yt-dlp 解析失败" in e.message,
              f"裸异常 → 502（{e.message[:46]}）")
        check("pip install -U yt-dlp" in e.message and "YTDLP_COOKIES_FILE" in e.message,
              "报错带排障线索：解析器会随站点改版失效，登录态站点要 cookie 文件")
    except Exception as e:                                      # noqa: BLE001
        check(False, f"裸异常泄漏到调用方：{type(e).__name__}")
    check(await ctx.rows(uid) == 0, "崩溃未登记 materials")
    check(not (ctx.ws_root / "url_u-crash").exists(), "崩溃后残渣一并回收")


async def case_ytdlp_runner_opts(ctx: Ctx) -> None:
    """真下载器的参数：cookie 只从环境变量取路径，凭证值不落参数、不落日志。"""
    captured: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, opts: dict) -> None:
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):            # noqa: ANN001
            captured["urls"] = urls
            dst = Path(captured["opts"]["outtmpl"]).parent
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "clip.mp4").write_bytes(b"x" * 10)

    real_mod = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
    env_key = "YTDLP_COOKIES_FILE"
    real_env = os.environ.get(env_key)
    dst = ctx.tmp / "ytdlp_opts"
    try:
        os.environ[env_key] = ""
        path = default_ytdlp_runner("https://site.example.com/watch/1", dst, 1234)
        opts = captured["opts"]
        check(path.name == "clip.mp4" and path.is_file(), "假 yt-dlp 产出的文件被选回")
        check(captured["urls"] == ["https://site.example.com/watch/1"], "传的是原始链接")
        check("cookiefile" not in opts, "未配 cookie 文件时不带 cookiefile")
        check(opts.get("max_filesize") == 1234, "字节上限传给了下载器")
        check(opts.get("windowsfilenames") is True and "%(ext)s" in opts.get("outtmpl", ""),
              "本地路径安全 + 扩展名模板")
        check("restrictfilenames" not in opts,
              "不做 ASCII 化（中文标题曾被压成「_.mp4」，看不出取回的是什么）")
        check(Path(str(opts["outtmpl"])).parent == dst, "下载产物落在指定工作区")
        captured["opts"] = {}

        os.environ[env_key] = str(ctx.tmp / "cookies.txt")
        default_ytdlp_runner("https://site.example.com/watch/2", dst, 1234)
        check(captured["opts"].get("cookiefile") == str(ctx.tmp / "cookies.txt"),
              "cookie 只以文件路径形式来自环境变量")

        class NoOutput(FakeYoutubeDL):
            def download(self, urls):                # noqa: ANN001
                captured["urls"] = urls              # 什么都没下下来

        sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=NoOutput)
        try:
            default_ytdlp_runner("https://site.example.com/watch/3",
                                 ctx.tmp / "ytdlp_none", 1234)
            check(False, "下载器空手而归应报错")
        except FetchRejected as e:
            check(e.status == 502 and "未产出文件" in e.message,
                  f"yt-dlp 未产出文件 → 502（{e.message}）")
    finally:
        if real_env is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = real_env
        if real_mod is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = real_mod
        ctx.net.calls.clear()
        for p in (dst / "clip.mp4",):
            p.unlink(missing_ok=True)


async def case_js_runtime(ctx: Ctx) -> None:
    """YouTube 的签名/n 挑战必须由 JS 运行时解：钉探测、传参、缺位时的可操作报错。

    本机连不上 www.youtube.com（TCP 层就超时），所以这一段**不碰网络**：
    钉的是「参数按 yt-dlp 认的形状传出去」+「真 yt-dlp 认这个运行时」。
    """
    import shutil
    from agent_framework.media_fetch import JS_RUNTIME_PRIORITY, js_runtimes_from_env

    env_key = "YTDLP_JS_RUNTIMES"
    real_env = os.environ.get(env_key)
    real_mod = sys.modules.get("yt_dlp")
    captured: dict[str, object] = {"boom": ""}
    dst = ctx.tmp / "js_rt"

    class FakeYoutubeDL:
        def __init__(self, opts: dict) -> None:
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):            # noqa: ANN001
            if captured["boom"]:
                raise RuntimeError(str(captured["boom"]))
            d = Path(captured["opts"]["outtmpl"]).parent
            d.mkdir(parents=True, exist_ok=True)
            (d / "clip.mp4").write_bytes(b"y" * 10)

    def set_env(value: str | None) -> None:
        if value is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = value

    js_err = "ERROR: [youtube] Signature solving requires a JavaScript runtime"
    real_fn = media_fetch.js_runtimes_from_env
    try:
        sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)

        set_env(None)
        det = js_runtimes_from_env()
        expect = [n for n in JS_RUNTIME_PRIORITY if shutil.which(n)]
        check(sorted(det) == sorted(expect) and all(list(v) == ["path"] for v in det.values()),
              f"不设变量就按 PATH 探测：{sorted(det)}（which 命中 {expect}）")

        captured["opts"] = {}
        default_ytdlp_runner("https://www.youtube.com/watch?v=x", dst, 1234)
        if det:
            check(captured["opts"].get("js_runtimes") == det,
                  "探测到的运行时整份传给 yt-dlp")
        else:
            check("js_runtimes" not in captured["opts"],
                  "本机一个运行时都没有时不塞空 dict（让 yt-dlp 报它自己的错）")

        pinned = Path(sys.executable).as_posix()
        set_env(f"node:{pinned}")
        check(js_runtimes_from_env() == {"node": {"path": pinned}},
              "name:path 钉死路径，Windows 盘符后的冒号不被当分隔符")
        try:
            set_env("node:E:/definitely/missing/node.exe")
            js_runtimes_from_env()
            check(False, "钉死的路径不存在时应当场回绝")
        except FetchRejected as e:
            check(e.status == 500 and "不存在" in e.message,
                  f"写错的路径不静默传下去（{e.message[:44]}…）")
        set_env("node , deno;quickjs")
        got = js_runtimes_from_env()
        check("node" in got and set(got) <= set(JS_RUNTIME_PRIORITY),
              f"只列名字交给 PATH（{sorted(got)}）")
        try:
            set_env("cjs")
            js_runtimes_from_env()
            check(False, "yt-dlp 不认的运行时名应回绝")
        except FetchRejected as e:
            check(e.status == 500 and "quickjs" in e.message,
                  f"不认的名字当场回绝并列出支持项（{e.message[:40]}…）")
        try:
            set_env("bun:/definitely/missing/bun")
            default_ytdlp_runner("https://x.example/a", dst, 1234)
            check(False, "指定的路径不可读时应回绝而不是静默降级")
        except FetchRejected as e:
            check(e.status == 500 and "YTDLP_JS_RUNTIMES" in e.message,
                  f"显式指定的运行时不在本机 → 如实回绝（{e.message[:40]}…）")

        # 一台没有运行时的机器 + 站点报 JavaScript：报错必须告诉用户装什么、怎么指
        set_env(None)
        captured["boom"] = js_err
        media_fetch.js_runtimes_from_env = lambda: {}
        try:
            default_ytdlp_runner("https://www.youtube.com/watch?v=y", dst, 1234)
            check(False, "缺运行时且站点报 JavaScript 应回 502")
        except FetchRejected as e:
            check(e.status == 502 and "YTDLP_JS_RUNTIMES" in e.message
                  and "deno" in e.message,
                  f"缺 JS 运行时报可操作的一条（{e.message[:52]}…）")
        except Exception as e:                                         # noqa: BLE001
            check(False, f"缺运行时时报错泄漏成裸异常：{type(e).__name__}")
        finally:
            media_fetch.js_runtimes_from_env = real_fn

        # 运行时明明有、站点仍报 JavaScript（解析器改版之类）：不改写，原样交给上层通用映射
        captured["boom"] = js_err
        try:
            default_ytdlp_runner("https://www.youtube.com/watch?v=z", dst, 1234)
            check(False, "假下载器抛异常应外抛")
        except FetchRejected:
            check(False, "有运行时时不该套「缺 JS 运行时」这条文案")
        except RuntimeError as e:
            check("JavaScript" in str(e), "有运行时时原样上抛，由调用方通用映射处理")
        captured["boom"] = ""

        # 真 yt-dlp（不联网）：认这个参数形状，并自己复核可执行文件到底能不能用
        if real_mod is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = real_mod
        try:
            import yt_dlp
        except ImportError:
            check(False, "离线套件按约定应能拿到 yt_dlp（requirements 里在）")
        else:
            with yt_dlp.YoutubeDL({"quiet": True,
                                   "js_runtimes": {"node": {"path": str(sys.executable)}}}) as ydl:
                rt = ydl._js_runtimes["node"]
                check(rt is not None and rt.info.supported is False,
                      "指到不是 JS 运行时的可执行文件时 yt-dlp 复核得出来："
                      f"{rt.info.version} / supported={rt.info.supported}")
            if det:
                with yt_dlp.YoutubeDL({"quiet": True, "js_runtimes": det}) as ydl:
                    infos = {k: v.info for k, v in ydl._js_runtimes.items()}
                check(all(i is not None and i.supported for i in infos.values()),
                      "本机探测到的运行时真被 yt-dlp 认下且判支持："
                      f"{ {k: i.version for k, i in infos.items()} }")
            else:
                print("  ·  本机 PATH 上没有 deno/node/quickjs/bun，运行时支持项未验（部署时装一个）")
    finally:
        set_env(real_env)
        media_fetch.js_runtimes_from_env = real_fn
        if real_mod is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = real_mod
        for p in (dst / "clip.mp4",):
            p.unlink(missing_ok=True)


async def case_ssrf_blocked(ctx: Ctx) -> None:
    for url in ("http://127.0.0.1:8000/a.mp4", "http://localhost/x.mp4",
                "http://169.254.169.254/latest/meta-data", "http://10.0.0.9/a.mp4",
                "http://inside.example.com/a.mp4", "file:///c:/windows/win.ini"):
        try:
            await fetch_media(ctx.s, url, user_id="u-ssrf", conversation_id="c-1",
                              policy=POLICY)
            check(False, f"应拦下 {url}")
        except FetchRejected as e:
            check(e.status == 400, f"拦下 {url}")
    check(ctx.net.calls == [], "内网/非法协议一次请求都没发出")


async def case_redirect_revalidated(ctx: Ctx) -> None:
    """`_open` 真身：每一跳重定向都重过 SSRF，否则一条 302 就把防护变成摆设。"""

    class ScriptedOpener:
        """按脚本逐跳表演：("redirect", loc) / ("ok", body) / ("status", code)。"""

        addheaders = None

        def __init__(self, script: list[tuple[str, object]]) -> None:
            self.script = script
            self.i = 0

        def open(self, url, timeout=None):          # noqa: ANN001, ARG002
            kind, val = (self.script[self.i] if self.i < len(self.script)
                         else ("status", 503))
            self.i += 1
            if kind == "redirect":
                h = email.message.Message()
                if val:
                    h["Location"] = str(val)
                raise urllib.error.HTTPError(url, 302, "Found", h, None)
            if kind == "ok":
                return FakeResp(bytes(val), {"Content-Type": "video/mp4"})   # type: ignore[arg-type]
            if kind == "neterr":
                raise urllib.error.URLError(str(val))
            raise urllib.error.HTTPError(url, int(val), "Boom",
                                         email.message.Message(), None)

    real = urllib.request.build_opener
    try:
        def use(script):                                       # noqa: ANN001
            urllib.request.build_opener = lambda *a, **kw: ScriptedOpener(script)

        use([("redirect", "http://127.0.0.1/inner.mp4")])
        try:
            REAL_OPEN("https://media.example.com/r/1.mp4", 5.0)
            check(False, "跳转到内网应被拒")
        except FetchRejected as e:
            check(e.status == 400 and "私有/回环" in e.message,
                  f"302 → 内网被拦（{e.message}）")

        use([("redirect", f"https://media.example.com/r/{i}.mp4")
             for i in range(MAX_REDIRECTS + 2)])
        try:
            REAL_OPEN("https://media.example.com/r/0.mp4", 5.0)
            check(False, "跳数过多应被限")
        except FetchRejected as e:
            check(e.status == 502 and "重定向超过" in e.message,
                  f"超过 {MAX_REDIRECTS} 跳 → 502（{e.message}）")

        use([("redirect", "")])
        try:
            REAL_OPEN("https://media.example.com/r/2.mp4", 5.0)
            check(False, "缺 Location 的重定向应报错")
        except FetchRejected as e:
            check(e.status == 502 and "Location" in e.message,
                  f"重定向缺 Location → 502（{e.message}）")

        use([("redirect", "https://media.example.com/r/final.mp4"), ("ok", b"OK")])
        resp, loc = REAL_OPEN("https://media.example.com/r/start.mp4", 5.0)
        check(loc.endswith("/r/final.mp4"), f"正常重定向跟到最终 URL（{loc}）")
        check(resp.read(2) == b"OK", "跟随后能读到字节")

        use([("status", 500)])
        try:
            REAL_OPEN("https://media.example.com/x.mp4", 5.0)
            check(False, "500 应报错")
        except FetchRejected as e:
            check(e.status == 502 and "源站返回 500" in e.message,
                  f"源站 5xx → 502（{e.message}）")

        use([("neterr", "timed out")])
        try:
            REAL_OPEN("https://media.example.com/y.mp4", 5.0)
            check(False, "连接层故障应报错")
        except FetchRejected as e:
            check(e.status == 502 and "取料连接失败" in e.message,
                  f"URLError/超时 → 502（{e.message}）")
    finally:
        urllib.request.build_opener = real


async def case_size_limits(ctx: Ctx) -> None:
    uid, cid = "u-clen", "c-clen"
    url = "https://media.example.com/big.mp4"
    ctx.net.add(url, b"tiny", **{"Content-Type": "video/mp4",
                                 "Content-Length": str(50 * 1024 * 1024)})
    before = len(ctx.s.objects.data)
    try:
        await fetch_media(ctx.s, url, user_id=uid, conversation_id=cid,
                          policy=FetchPolicy(max_mb=10, timeout=5.0))
        check(False, "声明超限应拒绝")
    except FetchRejected as e:
        check(e.status == 413 and "源站声明大小" in e.message,
              f"Content-Length 超限 → 413（{e.message}）")
    check(await ctx.rows(uid) == 0 and len(ctx.s.objects.data) == before,
          "声明超限：未入库也未写字节")

    uid2, cid2 = "u-stream", "c-stream"
    url2 = "https://media.example.com/stream.mp4"
    ctx.net.add(url2, b"x" * (6 * 1024 * 1024), **{"Content-Type": "video/mp4"})
    before = len(ctx.s.objects.data)
    try:
        await fetch_media(ctx.s, url2, user_id=uid2, conversation_id=cid2,
                          policy=FetchPolicy(max_mb=2, timeout=5.0))
        check(False, "流式超限应拒绝")
    except FetchRejected as e:
        check(e.status == 413 and "源站字节数" in e.message,
              f"_chunk_stream 边收边断 → 413（{e.message}）")
    check(ctx.net.resps[-1].closed, "超限后连接立即关闭（不是下完再拒）")
    check(await ctx.rows(uid2) == 0, "流式超限未登记 materials")
    check(len(ctx.s.objects.data) == before, "流式超限未留下对象（不是先落盘再拒）")


async def case_type_rejected(ctx: Ctx) -> None:
    uid, cid = "u-type", "c-type"
    ctx.net.add("https://media.example.com/doc.pdf", b"%PDF-1.7",
                **{"Content-Type": "application/pdf"})
    try:
        await fetch_media(ctx.s, "https://media.example.com/doc.pdf",
                          user_id=uid, conversation_id=cid,
                          policy=FetchPolicy(timeout=5.0, allow_ytdlp=False))
        check(False, "pdf 应被类型白名单拒绝")
    except FetchRejected as e:
        check(e.status == 415, f"白名单外类型 → 415（{e.message}）")

    page = "https://site.example.com/news"
    ctx.net.add(page, b"<html><body>\xe7\xba\xaf\xe6\x96\x87\xe5\xad\x97</body></html>",
                **{"Content-Type": "text/html"})
    try:
        await fetch_media(ctx.s, page, user_id=uid, conversation_id=cid,
                          policy=FetchPolicy(timeout=5.0, allow_ytdlp=False))
        check(False, "网页且禁用兜底应报错")
    except FetchRejected as e:
        check(e.status == 415, f"禁用 yt-dlp 时明确回类型错误（{e.message}）")
    check(await ctx.rows(uid) == 0, "类型被拒未入库")


async def case_empty_and_source_down(ctx: Ctx) -> None:
    uid, cid = "u-empty", "c-empty"
    ctx.net.add("https://media.example.com/empty.mp4", b"",
                **{"Content-Type": "video/mp4"})
    try:
        await fetch_media(ctx.s, "https://media.example.com/empty.mp4",
                          user_id=uid, conversation_id=cid, policy=POLICY)
        check(False, "空字节应拒绝")
    except FetchRejected as e:
        check(e.status == 400 and "空素材" in e.message, f"空素材 → 400（{e.message}）")
    try:
        await fetch_media(ctx.s, "https://media.example.com/404.mp4",
                          user_id=uid, conversation_id=cid, policy=POLICY)
        check(False, "源站故障应拒绝")
    except FetchRejected as e:
        check(e.status == 502, "未登记路由按源站故障处理")
    check(await ctx.rows(uid) == 0, "两次失败都未入库")


async def case_bare_host_normalized(ctx: Ctx) -> None:
    uid, cid = "u-bare", "c-bare"
    url = "https://media.example.com/sea/bare.mp4"
    ctx.net.add(url, b"BARE-BYTES", **{"Content-Type": "video/mp4"})
    res = await fetch_media(ctx.s, "media.example.com/sea/bare.mp4",
                            user_id=uid, conversation_id=cid, policy=POLICY)
    check(res["filename"] == "bare.mp4" and ctx.net.calls == [url],
          f"裸主机链接补出 https:// 后照常取料（实到 {ctx.net.calls}）")
    check(media_fetch.normalize_url("www.bilibili.com/video/BV1bV4y177V9")
          == "https://www.bilibili.com/video/BV1bV4y177V9", "不带协议的贴链接能认出来")
    check(media_fetch.normalize_url("https://x.com/a") == "https://x.com/a",
          "已带协议的原样交给护栏")
    check(media_fetch.normalize_url("  ftp://x/a  ") == "ftp://x/a",
          "非 http 协议不硬掰成 https")
    check(media_fetch.normalize_url("帮我把这段剪一下") == "帮我把这段剪一下",
          "不是链接的整句不去猜")
    check(media_fetch.normalize_url("localhost/x.mp4") == "localhost/x.mp4"
          and media_fetch.normalize_url("127.0.0.1:9000/x") == "127.0.0.1:9000/x",
          "本机名与 IP 字面量不补协议（内网判定仍由 ensure_safe_url 把关）")


async def case_tool(ctx: Ctx) -> None:
    tool = FetchMediaTool()
    r = await tool.execute(url="https://media.example.com/sea/tool.mp4")
    check(r.startswith("Error:") and "未装配" in r, f"未接存储不静默退回本地（{r[:28]}）")

    tool = FetchMediaTool(storage=ctx.s, policy=POLICY)
    r = await tool.execute(url="https://media.example.com/sea/tool.mp4")
    check(r.startswith("Error:") and "user/conversation" in r,
          f"无身份时报归属缺失（{r[:32]}…）")

    ctx.net.add("https://media.example.com/sea/tool.mp4", b"TOOL-BYTES",
                **{"Content-Type": "video/mp4"})
    with use_identity("u-tool", "c-tool") as ident:
        check(ident.user_id == "u-tool", "身份上下文生效")
        r = await tool.execute(url="https://media.example.com/sea/tool.mp4")
    obj = json.loads(r)
    check(obj["kind"] == "video" and obj["material_id"].startswith("mat-"),
          "工具回 JSON 素材描述")
    check("load_media" in obj["hint"], "hint 指向下一步 load_media")
    check(obj["object_key"].startswith("users/u-tool/convs/c-tool/"),
          "归属由执行身份决定，模型插不上手")
    check(await ctx.rows("u-tool") == 1, "工具路径同样入库")

    with use_identity("u-tool", "c-tool"):
        r = await tool.execute(url="http://127.0.0.1/x.mp4")
    check(r.startswith("Error: 取料失败（400）"), f"护栏错误回喂模型（{r[:34]}）")
    check(tool.read_only is False, "写类工具，不标只读")


CASES = [
    ("直链入库", case_direct_link),
    ("Content-Type 反查 kind", case_content_type_no_ext),
    ("Content-Disposition 文件名", case_content_disposition),
    ("og:video 嗅探", case_sniff_og_video),
    ("<video> 标签嗅探", case_sniff_video_tag),
    ("yt-dlp 兜底 + 工作区回收", case_ytdlp_fallback),
    ("yt-dlp 空产出", case_ytdlp_no_output),
    ("yt-dlp 崩溃映射 502", case_ytdlp_crash_mapped),
    ("yt-dlp 参数与 cookie 来源", case_ytdlp_runner_opts),
    ("yt-dlp JS 运行时（YouTube 硬门槛）", case_js_runtime),
    ("SSRF 拦截", case_ssrf_blocked),
    ("重定向逐跳校验", case_redirect_revalidated),
    ("字节上限", case_size_limits),
    ("类型白名单", case_type_rejected),
    ("空素材与源站故障", case_empty_and_source_down),
    ("裸主机链接补协议", case_bare_host_normalized),
    ("工具层封装", case_tool),
]


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fetch_media_"))
    s = build_storage("memory", cache_root=tmp / "cache", workspace_root=tmp / "ws")
    await s.start()

    socket.getaddrinfo = fake_getaddrinfo
    old_open = media_fetch._open
    try:
        for name, fn in CASES:
            print(f"\n[{name}]")
            net = FakeNet()
            media_fetch._open = net
            try:
                await fn(Ctx(s, tmp, net))
            finally:
                media_fetch._open = old_open
    finally:
        socket.getaddrinfo = _real_getaddrinfo
        await s.close()

    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
