"""按链接取素材：三条取料策略共用一条入库路径（`ingest.ingest_bytes`）。

策略递进，前一条不行才落到下一条：
1. **直链** —— URL 的扩展名已命中媒体白名单（`.mp4/.mp3/.jpg`…），或响应
   `Content-Type` 能反查回扩展名，直接把字节流交给入库。
2. **页面嗅探** —— 抓一次 HTML，找 `og:video` / `<video src>` / `<source src>`，
   命中就转成策略 1。零解析依赖，正则足够（页面结构变了就继续往下走）。
3. **yt-dlp 兜底** —— 站点播放器页面（需要解析清单/分片）交给 yt-dlp，它先下到
   临时工作区，再走同一条入库路径，finally 整目录回收。

护栏（与 `tools/web.py` 同源）：
- `ensure_safe_url` 挡内网/回环/元数据地址，**每一跳重定向都重查一次**——否则一条
  302 就把 SSRF 防护变成摆设；
- 字节上限边收边断（在 `ingest_bytes` 的守卫生成器里），不是先下完再拒；
- 类型白名单复用 `storage.media.kind_of`：入库认识的类型与取料认识的类型必须同一份；
- 需要登录态的站点：cookie 只给**文件路径**（环境变量 `YTDLP_COOKIES_FILE`），
  凭证值不进代码、不进日志、不进 PG；
- YouTube 另有一道硬门槛——签名/`n` 挑战要 JS 运行时：按 `PATH` 探测
  `deno/node/quickjs/bun` 交给 yt-dlp，`YTDLP_JS_RUNTIMES` 可显式指定或钉路径。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .ingest import IngestRejected, ingest_bytes, ingest_local_file, safe_segment
from .storage import Storage
from .storage.media import kind_of
from .tools.web import USER_AGENT, ensure_safe_url

MAX_REDIRECTS = 3
DEFAULT_MAX_FETCH_MB = 512
_CHUNK = 1 << 20
# 嗅探用的小页面读上限：只为拿到视频真实地址，不打算读完整站。
_PAGE_MAX_BYTES = 2_000_000

# Content-Type → 扩展名：直链没有扩展名时（`…/download?id=7`）靠它补出 kind。
_EXT_BY_MIME = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi", "video/webm": ".webm", "video/x-flv": ".flv",
    "video/mp2t": ".ts", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/aac": ".aac", "audio/mp4": ".m4a", "audio/flac": ".flac",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "image/jpeg": ".jpg",
    "image/png": ".png", "image/gif": ".gif", "image/bmp": ".bmp",
    "image/webp": ".webp", "image/heic": ".heic",
}

_OG_VIDEO = re.compile(
    r"<meta[^>]+property=[\"']og:video(?::(?:secure_)?url)?[\"'][^>]*content=[\"']([^\"']+)[\"']",
    re.I)
_OG_VIDEO_ALT = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*property=[\"']og:video(?::(?:secure_)?url)?[\"']",
    re.I)
_VIDEO_SRC = re.compile(r"<(?:video|source)[^>]+src=[\"']([^\"']+)[\"']", re.I)


class FetchRejected(Exception):
    """取料被拒：带 HTTP 语义码，接口层翻译成响应状态，工具层翻译成 Error 字符串。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class FetchPolicy:
    """取料护栏。CLI/配置装配时给出，测试直接构造。"""

    max_mb: int = DEFAULT_MAX_FETCH_MB
    timeout: float = 60.0
    allow_ytdlp: bool = True
    ytdlp: Callable[..., Path] | None = None    # 注入点：离线测试塞假下载器


# --------------------------------------------------------------------------
# 打开连接：手工跟随重定向，每一跳都重做 SSRF 校验
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """交出重定向控制权给 `_open`，好让每一跳都过一遍 SSRF 校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):   # type: ignore[no-untyped-def]
        return None


def _open(url: str, timeout: float):
    """同步打开到最终响应（供 to_thread 调用），返回 (响应, 最终 URL)。

    `urllib` 默认会静默跟到任意目标，内网校验就会被一条 302 绕过，所以这里自己跟、
    自己限跳数、每一跳重新校验。
    """
    opener = urllib.request.build_opener(_NoRedirect())
    opener.addheaders = [("User-Agent", USER_AGENT)]
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        err = ensure_safe_url(current)
        if err:
            raise FetchRejected(400, err.removeprefix("Error: "))
        try:
            return opener.open(current, timeout=timeout), current   # noqa: S310
        except urllib.error.HTTPError as e:                          # noqa: PERF203
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location", "")
                if not loc:
                    raise FetchRejected(502, f"重定向缺少 Location：{url}") from None
                current = urllib.parse.urljoin(current, loc)
                continue
            raise FetchRejected(502, f"源站返回 {e.code}：{url}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise FetchRejected(502, f"取料连接失败：{type(e).__name__}: {e}") from None
    raise FetchRejected(502, f"重定向超过 {MAX_REDIRECTS} 跳：{url}")


def _chunk_stream(resp: Any, max_bytes: int) -> AsyncIterator[bytes]:
    """同步 HTTP 响应 → 异步块流：读线程化，累计越界就地断。"""

    async def gen() -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        read = 0
        while True:
            b = await loop.run_in_executor(None, resp.read, _CHUNK)
            if not b:
                break
            read += len(b)
            if read > max_bytes:
                raise FetchRejected(
                    413, f"源站字节数超过上限 {max_bytes // (1024 * 1024)}MB")
            yield b

    return gen()


def _filename_from(url: str, resp: Any, final_url: str) -> str:
    """取文件名优先级：Content-Disposition > URL 路径 > 最终 URL 路径。"""
    cd = resp.headers.get("Content-Disposition", "") if resp is not None else ""
    m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^;\"']+)", cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip())
        if name:
            return name
    for u in (url, final_url):
        tail = Path(urllib.parse.urlparse(u).path).name
        if tail and kind_of(tail):
            return tail
    return ""


def _has_media_ext(url: str) -> bool:
    return bool(kind_of(Path(urllib.parse.urlparse(url).path).name))


def normalize_url(url: str) -> str:
    """把「裸主机」链接补成 https://：用户常直接贴 `www.x.com/a/b`，
    它 urlparse 出来的 scheme 是空的，被 `ensure_safe_url` 拒成「(无协议)」——
    对贴链接的人来说那是句看不懂的报错，而不是他没写协议。
    """
    u = url.strip()
    if u and not urllib.parse.urlparse(u).scheme:
        host = u.split("/", 1)[0]
        if re.fullmatch(r"[^:/\s?#]+\.[A-Za-z][A-Za-z0-9]*(:\d+)?", host):
            return "https://" + u
    return u


# --------------------------------------------------------------------------
# 策略 1：直链
# --------------------------------------------------------------------------

async def _fetch_direct(
    storage: Storage,
    url: str,
    *,
    user_id: str,
    conversation_id: str,
    max_bytes: int,
    timeout: float,
) -> dict[str, Any]:
    resp, loc = await asyncio.to_thread(_open, url, timeout)
    try:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        name = _filename_from(url, resp, loc)
        if not kind_of(name):
            ext = _EXT_BY_MIME.get(ctype, "")
            name = f"{(name or 'media')}{ext}" if ext else name
        if not kind_of(name):
            raise FetchRejected(
                415, f"链接不是可入库的媒体类型（Content-Type={ctype or '未知'}）")
        clen = resp.headers.get("Content-Length", "")
        if clen.isdigit() and int(clen) > max_bytes:
            raise FetchRejected(
                413, f"源站声明大小 {int(clen)} 字节，超过上限 {max_bytes}")
        try:
            return await ingest_bytes(
                storage, _chunk_stream(resp, max_bytes), filename=name,
                user_id=user_id, conversation_id=conversation_id,
                origin="url", max_bytes=max_bytes)
        except IngestRejected as e:
            raise FetchRejected(e.status, e.message) from None
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001 - 关闭失败不掩盖真正的结果
            pass


# --------------------------------------------------------------------------
# 策略 2：页面嗅探
# --------------------------------------------------------------------------

async def _sniff_video_url(url: str, *, timeout: float) -> str | None:
    """抓一次 HTML，找出页内视频地址（相对 URL 补成绝对）；找不到返回 None。

    嗅探失败不是错误——它只是"这条策略没命中"，调用方会继续往下走。
    """
    try:
        resp, loc = await asyncio.to_thread(_open, url, timeout)
    except FetchRejected:
        return None
    try:
        body = await asyncio.wait_for(
            asyncio.to_thread(resp.read, _PAGE_MAX_BYTES), timeout)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    html = (body or b"").decode("utf-8", errors="replace")
    for pat in (_OG_VIDEO, _OG_VIDEO_ALT, _VIDEO_SRC):
        m = pat.search(html)
        if m and m.group(1).strip():
            return urllib.parse.urljoin(loc, m.group(1).strip())
    return None


# --------------------------------------------------------------------------
# 策略 3：yt-dlp 兜底
# --------------------------------------------------------------------------

# yt-dlp 自己的优先级（高→低），也是它认的全部运行时名。YouTube 的签名/n 挑战必须由
# JS 运行时解：缺了它，兜底策略对 YouTube 只会回一句 "Requires JavaScript"。
JS_RUNTIME_PRIORITY = ("deno", "node", "quickjs", "bun")


def js_runtimes_from_env() -> dict[str, dict[str, str]]:
    """交给 yt-dlp 的 `js_runtimes` 参数：`{运行时名: {"path": 可执行文件}}`。

    `YTDLP_JS_RUNTIMES="deno,node:C:/nvm/node.exe"` —— 只列名字就交给 PATH 找，
    带冒号钉死路径（Windows 的盘符在第一个冒号后面，不会被误当分隔符）。
    **不设这个变量时按优先级探测 PATH**，把找得到的都启用：deno 缺席的机器
    （本机就是）也能用现成的 Node ≥22，而不是整条兜底策略哑掉。
    一个都没有就返回空 dict —— 那是真的没运行时，让 yt-dlp 报它自己的错。
    """
    raw = os.environ.get("YTDLP_JS_RUNTIMES", "").strip()
    if raw:
        wanted: list[tuple[str, str | None]] = []
        for item in (x.strip() for x in re.split(r"[;,]", raw)):
            if not item:
                continue
            name, _, path = item.partition(":")
            name = name.strip().lower()
            if name not in JS_RUNTIME_PRIORITY:
                raise FetchRejected(
                    500, f"YTDLP_JS_RUNTIMES 里有 yt-dlp 不认的运行时名：{name}。"
                         f"支持 {'、'.join(JS_RUNTIME_PRIORITY)}")
            wanted.append((name, path.strip() or None))
        out: dict[str, dict[str, str]] = {}
        for name, path in wanted:
            if path:
                # 钉死的路径当场验在不在（yt-dlp 接受二进制本身或其所在目录）：
                # 写错一个字母静默传下去，换来的是一句看不懂的解析失败
                if not Path(path).exists():
                    raise FetchRejected(
                        500, f"YTDLP_JS_RUNTIMES 里 {name} 的路径不存在：{path}")
                out[name] = {"path": path}
            elif (exe := shutil.which(name)):
                out[name] = {"path": str(exe)}
        if not out:
            raise FetchRejected(
                500, f"YTDLP_JS_RUNTIMES 指定的运行时在本机都找不到：{raw}")
        return out
    return {name: {"path": exe}
            for name in JS_RUNTIME_PRIORITY if (exe := shutil.which(name))}


def default_ytdlp_runner(url: str, dst_dir: Path, max_bytes: int) -> Path:
    """用 yt-dlp 把链接下到 dst_dir，返回落地文件路径（同步，供 to_thread 调用）。

    依赖**延迟 import**：没装 yt-dlp 的环境里本模块照常可用，只是少了兜底策略。
    cookie 只接文件路径（环境变量 `YTDLP_COOKIES_FILE`），凭证值不进代码/日志/PG。
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise FetchRejected(502, f"未安装 yt-dlp，无法解析该页面：{e}") from None

    dst_dir.mkdir(parents=True, exist_ok=True)
    runtimes = js_runtimes_from_env()
    opts: dict[str, Any] = {
        "outtmpl": str(dst_dir / "%(title).80B.%(ext)s"),
        # 只避 Windows 保留名与非法字符，不做 ASCII 化：`restrictfilenames` 会把中文标题
        # 压成 "_"，附件卡片上就成了「_.mp4」。入库前还有 safe_segment 再净化一次。
        "windowsfilenames": True,
        "quiet": True, "noprogress": True,
        "socket_timeout": 30,
        "max_filesize": max_bytes,
        "format": "bv*+ba/b",
    }
    if runtimes:
        opts["js_runtimes"] = runtimes
    cookie_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except FetchRejected:
        raise
    except Exception as e:  # noqa: BLE001 - 只为把「缺 JS 运行时」单独说清楚，其余原样上抛
        if re.search(r"javascript|\bjs\b", str(e), re.I) and not runtimes:
            raise FetchRejected(
                502, f"yt-dlp 解析失败：{str(e)[:200]}。该站点要 JS 运行时解签名"
                     "（本机 PATH 上没找到 deno/node/quickjs/bun）："
                     "装一个，或用 YTDLP_JS_RUNTIMES=node:C:\\path\\node.exe 指到已有的") from None
        raise
    files = [p for p in sorted(dst_dir.iterdir()) if p.is_file()]
    if not files:
        raise FetchRejected(502, "yt-dlp 未产出文件（站点策略变化或需要登录）")
    return max(files, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

async def fetch_media(
    storage: Storage,
    url: str,
    *,
    user_id: str,
    conversation_id: str,
    policy: FetchPolicy | None = None,
) -> dict[str, Any]:
    """按链接取一条素材 → materials 行 + 对象存储字节 + presigned URL。

    返回值与 /upload 完全同构（含 material_id 与可播 URL），所以前端附件卡片、
    `load_media(material_ids=[…])` 都不需要知道这条素材是爬来的。
    """
    pol = policy or FetchPolicy()
    url = normalize_url(url)
    err = ensure_safe_url(url)
    if err:
        raise FetchRejected(400, err.removeprefix("Error: "))
    max_bytes = pol.max_mb * 1024 * 1024

    # 候选直取目标：扩展名自证 → 就它一条；否则先嗅页面，嗅不到（或嗅到的不是自己）
    # 也把原 URL 留作候选——响应头里的 Content-Type 仍能认出血统直链。
    if _has_media_ext(url):
        targets = [url]
    else:
        sniffed = await _sniff_video_url(url, timeout=pol.timeout)
        targets = [url] if not sniffed or sniffed == url else [sniffed, url]
    if not pol.allow_ytdlp:
        targets = targets[:1]     # 没有兜底可退，多试一条只会拖慢报错

    for target in targets:
        try:
            return await _fetch_direct(storage, target, user_id=user_id,
                                       conversation_id=conversation_id,
                                       max_bytes=max_bytes, timeout=pol.timeout)
        except FetchRejected as e:
            # 只有「类型不对」值得换策略；超限、内网、源站故障就地抛出。
            if e.status != 415:
                raise
            last = e
    if not pol.allow_ytdlp:
        raise last

    sess = f"url:{safe_segment(user_id, 'u')}"
    art = safe_segment(conversation_id, "c")
    work = storage.workspace.dir_for(sess, art)
    try:
        runner = pol.ytdlp or default_ytdlp_runner
        try:
            path = await asyncio.to_thread(runner, url, work, max_bytes)
        except FetchRejected:
            raise
        except Exception as e:  # noqa: BLE001 - yt-dlp 的失败类型五花八门，统一成 502
            raise FetchRejected(502,
                                f"yt-dlp 解析失败：{type(e).__name__}: {str(e)[:200]}。"
                                "站点解析器会随对方改版失效（先试 pip install -U yt-dlp）；"
                                "需要登录态的站点把浏览器 cookie 文件路径写进 YTDLP_COOKIES_FILE"
                                ) from None
        try:
            return await ingest_local_file(
                storage, path, user_id=user_id, conversation_id=conversation_id,
                origin="url", max_bytes=max_bytes)
        except IngestRejected as e:
            raise FetchRejected(e.status, e.message) from None
    finally:
        storage.workspace.cleanup(sess, art)      # 下载产物一律不留本地（spec §6）


__all__ = ["DEFAULT_MAX_FETCH_MB", "FetchPolicy", "FetchRejected", "JS_RUNTIME_PRIORITY",
           "MAX_REDIRECTS", "default_ytdlp_runner", "fetch_media",
           "js_runtimes_from_env", "normalize_url"]
