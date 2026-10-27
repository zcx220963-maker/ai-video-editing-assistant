"""原生网络工具：Fetch（抓取网页转文本）与 Search（联网检索）。

设计取舍：
- 零第三方依赖：用标准库 urllib，并放进 asyncio.to_thread 执行以免阻塞事件循环。
- 可测试 / 可换后端：SearchTool 的 provider、FetchTool 的 transport 均可注入，
  验证脚本不打真实网络即可跑通。默认检索后端是**一条链**：前端「设置」配过搜索 API
  （brave / tavily / serpapi / 自建 searxng）就只走它，没配则走免 key 回落链
  （维基搜索 API + Bing RSS + DuckDuckGo，按查询文字路由）——见 ``auto_provider``。
- 安全：仅允许 http/https；默认拒绝回环 / 私有网段，降低 SSRF 风险；限制响应大小。

并发标记：两者都是只读无副作用 → concurrency_safe，可进 gather 批次。
"""

from __future__ import annotations

import asyncio
import codecs
import gzip
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from ..tool import Tool, ToolError

USER_AGENT = "Mozilla/5.0 (compatible; AgentFramework/0.1)"

#: DDG 判定脚本流量时回的页面文案（2026-10-07 真机撞上：「Select all squares containing a duck」）
_BOT_CHALLENGE = "bots use DuckDuckGo too"

#: 各家后端的「你不是人」文案与状态码。分类只用来把**「后端把流量挡在门外」**和
#: **「网上没有这条查询」**分开回——混成一桶，模型会把能核实的事实判成查不到（真机
#: 事故：整条片子的数字因此全没了）。
_BLOCK_MARKS = (_BOT_CHALLENGE, "select all squares", "安全验证", "captcha",
                "are you a robot", "unusual traffic", "access denied", "rate limit")
_BLOCK_STATUSES = (401, 403, 405, 429, 503)


def _blocked(status: int, body: str) -> bool:
    """这一页是不是「反爬壳页 / 被限流」而不是「没有内容」。"""
    if status in _BLOCK_STATUSES:
        return True
    low = body[:4000].lower()
    return any(mark in low for mark in _BLOCK_MARKS)


# --------------------------------------------------------------------------
# 网络与 URL 安全
# --------------------------------------------------------------------------

def ensure_safe_url(url: str) -> str | None:
    """校验 URL 协议与主机是否允许访问；不安全则返回错误信息，否则 None。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return f"Error: 无法解析 URL {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"Error: 仅支持 http/https，拒绝 {parsed.scheme or '(无协议)'}"
    host = parsed.hostname
    if not host:
        return f"Error: URL 缺少主机名 {url!r}"
    if _is_blocked_host(host):
        return f"Error: 拒绝访问私有/回环地址 {host}"
    return None


def _is_blocked_host(host: str) -> bool:
    if host.lower() in ("localhost", "metadata.google.internal"):
        return True
    # 解析域名到 IP 再判断，避免 DNS rebinding 之外的常见内网目标。
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # 解析失败交给后续请求自然报错
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return True
    return False


@dataclass
class _Fetched:
    status: int
    content_type: str
    body: bytes
    truncated: bool


def _ascii_url(url: str) -> str:
    """把 URL 里非 ASCII 的片段百分号编码——含空格也一起编。

    urllib 只吃 ASCII：模型贴的中文维基链接（``…/wiki/中子星``）直接送进 Request
    会撞 "'ascii' codec can't encode characters"，报成「抓取失败」而不是抓到页面。
    safe 里保留 ``%``，已经编码过的 URL 不会被二次编码成 ``%25``。
    """
    return urllib.parse.quote(url, safe=":/?&=%#[]@!$'()*+,;~-_.")


def _decode_body(raw: bytes, encoding: str, limit: int) -> tuple[bytes, bool]:
    """按 Content-Encoding 还原字节 → (正文, 是否因预算被截)。

    **urllib 不解压**（它只负责传输，不像 requests 那样替你解码）。B 站这类 CDN 无视
    ``Accept-Encoding: identity`` 照发 gzip，于是抓回来的是压缩流本身：里面必然有 0x00
    字节，`decode(errors="replace")` 把它们原样留成 U+0000，工具文本再进 jsonb 列时
    ``json.dumps`` 转成 ``\\u0000``，PG 直接报「unsupported Unicode escape sequence」——
    界面上一格乱码 + 一句 PG 失败，其实是这一处漏解压的下游症状。
    """
    enc = (encoding or "").split(";")[0].strip().lower()
    if not raw or enc not in ("gzip", "x-gzip", "deflate", "br"):
        return raw, False
    if enc == "br":
        try:
            import brotli  # noqa: PLC0415 - 可选依赖，缺了宁可报错也不喂乱码
        except ImportError as e:
            raise RuntimeError("响应是 brotli(br) 压缩，本镜像没有 brotli 解码器") from e
        d = brotli.Decompressor()
        out = d.process(raw)
        return out[:limit], len(out) > limit
    # gzip 是 16+MAX_WBITS 的 zlib 流；裸 deflate 有些服务端不给 zlib 套壳，两种都试。
    variants = ((16 + zlib.MAX_WBITS),) if enc in ("gzip", "x-gzip") \
        else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    for wbits in variants:
        dec = zlib.decompressobj(wbits)
        try:
            out = dec.decompress(raw, limit) + dec.flush()
        except zlib.error:
            continue
        # 截断的流（读取时按预算切过）不报错，只是 eof 为假——照实标出来。
        return out, not dec.eof
    raise RuntimeError(f"响应声明 Content-Encoding={enc}，但两种 deflate 壳都解不开")


def _read_budget(fp, max_bytes: int, encoding: str) -> _Fetched:
    """从响应（或错误页）读至多 max_bytes 字节，读完就解压。"""
    raw = fp.read(max_bytes + 1)
    over = len(raw) > max_bytes
    body, dec_trunc = _decode_body(raw[:max_bytes], encoding, max_bytes)
    return _Fetched(getattr(fp, "status", None) or 0, "", body, over or dec_trunc)


def default_transport(url: str, max_bytes: int, timeout: float, *,
                      headers: dict[str, str] | None = None,
                      data: bytes | None = None) -> _Fetched:
    """同步抓取（供 to_thread 调用）：读至多 max_bytes 字节（按**解压后**的正文计）。

    ``headers``/``data`` 是给带 key 的检索 API 用的（它们要 ``X-Subscription-Token``、
    ``Authorization``、POST JSON）——位置参数仍是 (url, max_bytes, timeout)，
    注入用的假 transport 不必认识这两个新参数。
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate",
            **(headers or {})}
    req = urllib.request.Request(_ascii_url(url), headers=hdrs, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme 已校验)
            f = _read_budget(resp, max_bytes, resp.headers.get("Content-Encoding", ""))
            f.content_type = resp.headers.get("Content-Type", "")
            f.status = resp.status
            return f
    except urllib.error.HTTPError as e:
        # 403/429 这类错误页也是内容：取回字节，让调用方用 _blocked 把
        # 「被反爬挡住」和「网上没有这条」分开回——混成一桶，模型就把能核实的事实
        # 判成查不到。HTTPError 本身就是个类文件对象。
        f = _read_budget(e, max_bytes, e.headers.get("Content-Encoding", ""))
        f.content_type = e.headers.get("Content-Type", "")
        f.status = e.code
        return f


def _strip_tags(markup: str) -> str:
    """去掉标签与实体，只留一行能读的文本（各家摘要字段都混着 <b>/<span>）。"""
    text = re.sub(r"<[^>]+>", "", markup or "")
    return re.sub(r"\s+", " ", urllib.parse.unquote(text)).strip()


class _BackendBlocked(RuntimeError):
    """后端把这条流量判成机器（人机验证 / 限流 / 拒绝脚本 UA）。"""


class _BackendBadShape(RuntimeError):
    """后端回了东西，但不是约定的结构——页面改版或中间层劫持。"""


def _fetch_json(transport: Callable[..., _Fetched] | None, url: str, timeout: float, *,
                headers: dict[str, str] | None = None,
                data: bytes | None = None) -> tuple[Any, int]:
    """取一份 JSON，返回 (解析后的对象, HTTP 状态)。解析失败按内容异常抛出。"""
    tp = transport or default_transport
    fetched = tp(url, 2_000_000, timeout, headers=headers, data=data) \
        if headers or data else tp(url, 2_000_000, timeout)
    body = fetched.body.decode("utf-8", errors="replace")
    if _blocked(fetched.status, body):
        raise _BackendBlocked(fetched.status)
    try:
        return json.loads(body), fetched.status
    except ValueError as e:
        raise _BackendBadShape(f"返回不是 JSON（{e}）") from e


# --------------------------------------------------------------------------
# HTML → 纯文本
# --------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


#: C0 控制字符与 DEL（\t \n \r 除外）。0x00 是漏解压的压缩流里最常见的一个。
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _scrub_control(text: str) -> str:
    """外部字节解码出的文本里剔掉控制字符——PG 的 text/jsonb 不收 U+0000，
    ``json.dumps`` 会把任何漏网的 0x00 转成 ``\\u0000``，整条消息就写不进去。
    抓取是外部输入入口，这一刀必须在这里落，不能让每个下游各自防。"""
    return _CONTROL.sub("", text)


#: 页面声明字符集的两种写法：HTTP 头的 ``; charset=`` 与 HTML 的 ``<meta charset>`` /
#: ``<meta http-equiv="Content-Type" content="text/html; charset=gbk">``。
_CHARSET = re.compile(r"charset[ \t]*=[ \t]*[\"']?([A-Za-z][A-Za-z0-9_\-]*)", re.I)

#: 中文页最常见的三种声明都是 GBK 家族，gb18030 是它们的超集，统一升一档少一类失败。
_CODEC_ALIAS = {"gb2312": "gbk", "gbk": "gb18030", "windows-936": "gb18030",
                "gb-2312": "gbk", "x-gbk": "gbk", "utf8": "utf-8"}


def _decode_text(raw: bytes, content_type: str = "") -> str:
    """按**页面自己声明的**字符集解码，而不是逢字节就按 UTF-8 硬解。

    压缩壳（Content-Encoding）治的是「解不开」，这是同类的另一半「解开了是乱码」：
    中文站还有一批 GBK/GB18030 页（真机 ``js.99.com.cn`` 那条），按 UTF-8 解出来全是
    U+FFFD，模型据此判「这页没内容」或把引文抄成乱码——而引文要过出处闸，抄错等于造假。
    声明只信两处：HTTP 头、HTML 里的 ``<meta>``；都没有才按 UTF-8 兜底。
    任何一档解不动就退到下一档，**绝不因为编码问题让抓取报错**。
    """
    names: list[str] = []
    for found in (_CHARSET.search(content_type or ""),
                  _CHARSET.search(raw[:4096].decode("ascii", "ignore"))):
        if found:
            names.append(found.group(1))
    names.append("utf-8")
    for raw_name in names:
        codec = _CODEC_ALIAS.get(raw_name.lower(), raw_name.lower())
        try:
            codecs.lookup(codec)
            return _scrub_control(raw.decode(codec))
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return _scrub_control(raw.decode("utf-8", errors="replace"))


_META_KEYS = ("description", "keywords", "og:title", "og:description", "og:site_name",
              "article:title", "article:summary", "twitter:title", "twitter:description")


def _meta_text(markup: str) -> str:
    """JS 壳页的兜底：正文全靠脚本渲染时，能读的只有 <title> 与 <meta>。

    YouTube -watch 页就是这一类——服务端 HTML 除了 script 几乎没别的节点，
    `_TextExtractor` 把 script/title 全跳过之后返回空串，工具回「(空内容)」，
    模型据此判「这页没有内容」。取 meta 是为了把「壳页」和「空页」分开。
    """
    parts: list[str] = []
    t = re.search(r"<title[^>]*>(.*?)</title>", markup, re.S | re.I)
    if t:
        parts.append("title: " + _strip_tags(t.group(1)))
    for m in re.finditer(r"<meta\b[^>]*>", markup, re.I):
        tag = m.group(0)
        key = re.search(r'(?:name|property)\s*=\s*"([^"]+)"', tag, re.I)
        val = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I)
        if key and val and key.group(1).lower() in _META_KEYS:
            text = _strip_tags(val.group(1))
            if text:
                parts.append(f"{key.group(1)}: {text}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# FetchTool
# --------------------------------------------------------------------------

class FetchTool(Tool):
    """抓取网页并转成纯文本。"""

    def __init__(
        self,
        timeout: float = 20.0,
        transport: Callable[[str, int, float], _Fetched] | None = None,
    ) -> None:
        self.timeout = timeout
        self._transport = transport or default_transport

    @property
    def name(self) -> str:
        return "fetch_url"

    @property
    def display_name(self) -> str:
        return "抓取网页"

    @property
    def description(self) -> str:
        return "抓取给定 http/https URL 的内容并转成纯文本返回。用于阅读网页 / 接口原始响应。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的完整 URL"},
                "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 200000"},
            },
            "required": ["url"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def _deepseek_fetch(self, url: str, max_bytes: int) -> tuple[str, str]:
        """先试 DeepSeek 官方 Fetch → (回执, 说明)。回执为空 = 没走成，调用方照旧直接抓。

        只在「没注入 transport」且模型密钥是 DeepSeek 的时候走（见 `deepseek_auto_key`）。
        官方抓不到（403 安全策略 / 404 无正文 / 限流重试用完）就退回直接抓——那条路免费，
        而且有时正好抓得到；402 余额不足也退回，但把这件事写进说明，让模型与用户看得见。
        """
        if self._transport is not default_transport:
            return "", ""
        key = await deepseek_auto_key()
        if not key:
            return "", ""
        try:
            page = await asyncio.to_thread(_deepseek_call, "/v1/tools/fetch", {"url": url},
                                           key, self.timeout)
        except DeepSeekToolError as e:
            return "", f"（DeepSeek 抓取没成：HTTP {e.status}，{e}；已改为直接抓取）\n"
        except Exception as e:  # noqa: BLE001 - 网络异常也退回直接抓
            return "", f"（DeepSeek 抓取没成：{type(e).__name__}；已改为直接抓取）\n"
        content = _scrub_control(str(page.get("content") or "").strip())
        if not content:
            return "", "（DeepSeek 抓取没拿到正文；已改为直接抓取）\n"
        title = str(page.get("title") or "").strip()
        raw = content.encode("utf-8")
        tail = ""
        if len(raw) > max_bytes:
            content = raw[:max_bytes].decode("utf-8", errors="ignore")
            tail = f"\n...（内容超过 {max_bytes} 字节，已截断）"
        from ..retrieval import note_hit
        await note_hit(url, title, backend="fetch_url")
        head = f"# {title}\n" if title and not content.lstrip().startswith("#") else ""
        return (f"[200·DeepSeek Fetch] {url}\n{head}{content}{tail}\n"
                + COST_LINE.format(DEEPSEEK_CALL_YUAN)), ""

    async def execute(self, url: str, max_bytes: int = 200_000) -> str:
        err = ensure_safe_url(url)
        if err:
            return err
        via_api, note = await self._deepseek_fetch(url, max_bytes)
        if via_api:
            return via_api
        return note + await self._direct_fetch(url, max_bytes)

    async def _direct_fetch(self, url: str, max_bytes: int) -> str:
        """直接用 HTTP 抓目标站（免费；官方 Fetch 没走成、或没有 DeepSeek 密钥时的那一路）。"""
        try:
            fetched = await asyncio.to_thread(self._transport, url, max_bytes, self.timeout)
        except Exception as e:  # noqa: BLE001 - 网络异常回喂给 LLM
            return f"Error: 抓取失败 {url}: {e}"

        # 404/410 是**结论**，不是「有内容没读到」：把错误页的导航正文回喂给模型，
        # 它会当成一次没读全，对着同一个死链接一枪接一枪。真机 run f5031cbe1163
        #（2026-10-17）不存在的维基条目被同一 URL 抓了 150+ 次，一路打到熔断。
        # 按 ``counts_as_failure=False`` 抛：这条错误本身是可执行的结论（换源 / 判死），
        # 不该计入「工具坏了」的连续失败账。
        if fetched.status in (404, 410):
            raise ToolError(self.name,
                            f"地址不存在（HTTP {fetched.status}）：{url}——这个地址没有内容，"
                            f"同一个 URL 重试永远不会成功；换一个来源，或改用检索找现存页面。",
                            counts_as_failure=False)

        ctype = fetched.content_type.lower()
        is_html = "html" in ctype or fetched.body.lstrip()[:15].lower().startswith(b"<!doctype html")
        markup = _decode_text(fetched.body, fetched.content_type)
        text = _scrub_control(html_to_text(markup) if is_html else markup.strip())
        shell = ""
        if is_html and not text:
            # 一个字都没抽到 ≠ 页面是空的：JS 渲染的壳页（YouTube -watch、各类 SPA）服务端
            # HTML 里只有 script。按字符数当阈值会误伤本来正文就短的小页面，所以只判「全空」。
            meta = _scrub_control(_meta_text(markup))
            if meta:
                text = meta
                shell = "\n（正文抽不到字，以上取自 <title>/<meta>——这页多半靠 JS 渲染）"
            else:
                shell = "(这页是 JS 渲染的壳，服务端 HTML 里连 <title>/<meta> 都没给可读正文)"
        # 「这一页把我们的流量挡在门外」和「网上没有这条内容」是两句话：后者才是事实
        # 结论，前者只该让人换源。先判它，否则 403 的错误页会掉进下面的「空内容」分支。
        # 文案判据只在短页面上生效——长正文里出现 "captcha" 是内容本身，不是壳。
        if _blocked(fetched.status, text if len(text) < 600 else ""):
            return (f"Error: 这一页被挡在门外（HTTP {fetched.status}，反爬/限流），"
                    f"不代表网上没有这条内容——换一个源，或用已知页面直取。 url={url}")
        if not text:
            if shell:
                return (f"{shell} status={fetched.status} url={url} —— 需要浏览器渲染才能读到"
                        f"内容，**不代表这页没有内容**")
            return f"(空内容) status={fetched.status} url={url}"
        if fetched.truncated:
            text += f"\n...（内容超过 {max_bytes} 字节，已截断）"
        # 抓到正文才算「这一页真打开过」：出处闸认的是这一笔，不是模型的口头声明。
        if 200 <= fetched.status < 400:
            from ..retrieval import note_hit
            await note_hit(url, "", backend="fetch_url")
        return f"[{fetched.status}] {url}\n{text}" + shell


# --------------------------------------------------------------------------
# SearchTool
# --------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    # 哪一路后端出的这一条（免 key 链是多路的，用户与模型都得看得见结果从哪来）
    backend: str = ""
    # 与查询词的重合判定。默认 True：只有免 key 回落链会显式标 False——
    # 它按字面匹配，「一条都不重合」是能被机器证明的事实；带 key 的搜索引擎有排序，
    # 同义改写的相关页会死在这种判据上，所以那条路不做这个判断。
    relevant: bool = True

    def format(self) -> str:
        line = f"- {self.title}"
        if self.backend:
            line += f"  [{self.backend}]"
        if self.url:
            line += f"\n  {self.url}"
        if self.snippet:
            line += f"\n  {self.snippet}"
        return line


# provider(query, max_results) -> list[SearchResult]；出错可返回 (None, error)
SearchProvider = Callable[..., "list[SearchResult] | tuple[None, str]"]


def _extract_ddg_url(href: str) -> str:
    """从 DuckDuckGo 跳转链接里解出真实 URL。

    跳转形如 ``//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F&rut=abc``——
    真实地址在 ``uddg`` 参数里，必须按查询串解析；直接 ``split("uddg=")[-1]`` 会把
    ``&rut=…`` 一起带进结果 URL。
    """
    if "uddg=" not in href:
        return href
    try:
        qs = urllib.parse.urlparse(href).query or href.split("?", 1)[-1]
        vals = urllib.parse.parse_qs(qs, keep_blank_values=True).get("uddg")
        if vals and vals[0]:
            return vals[0]
    except ValueError:
        pass
    return urllib.parse.unquote(href.split("uddg=", 1)[-1].split("&", 1)[0])


def _parse_lite(html: str, max_results: int) -> list[SearchResult]:
    """解析 lite.duckduckgo.com/lite/ 的表格页：结果链接 + result-snippet 摘要。"""
    links = re.findall(
        r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', html, re.S)
    snippets = re.findall(
        r'class="result-snippet"[^>]*>(?P<snippet>.*?)</td>', html, re.S)
    results: list[SearchResult] = []
    si = 0
    for href, title in links:
        if "uddg=" not in href:   # lite 页内的导航/翻页都是 DDG 自家链接
            continue
        real = _extract_ddg_url(href)
        title_t = re.sub(r"<[^>]+>", "", title).strip()
        if not real or not title_t:
            continue
        snippet = ""
        if si < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[si]).strip()
            si += 1
        results.append(SearchResult(title=title_t, url=real, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


def duckduckgo_provider(
    query: str, max_results: int, timeout: float = 20.0,
    transport: Callable[[str, int, float], _Fetched] | None = None,
) -> list[SearchResult] | tuple[None, str]:
    """尽力而为的 DuckDuckGo 检索后端（无 key）。

    先打 lite 端点（2026-09 实测 html.duckduckgo.com 对脚本 UA 回 202 反爬壳页、
    0 条结果，lite 端 200 且带真结果）；lite 结构变更或 0 结果时回落 html 端点。
    页面结构变动时仍可能失效——生产建议替换为带 key 的正规搜索 API。
    """
    tp = transport or default_transport
    q = urllib.parse.urlencode({"q": query})

    def _grab(url: str) -> str:
        fetched = tp(url, 1_000_000, timeout)
        return fetched.body.decode("utf-8", errors="replace")

    try:
        lite = _grab(f"https://lite.duckduckgo.com/lite/?{q}")
        results = _parse_lite(lite, max_results)
        if results:
            return results
        if _BOT_CHALLENGE in lite:
            # 「后端要人机验证」和「网上没有这个数」必须分开回：前者过一会儿能查到，
            # 后者是事实问题。混在一起，模型就把能核实数字判成查不到、整条片子的数字全没了。
            return None, ("Error: 搜索后端要求人机验证（DDG 把这条流量判成 bot），"
                          "不代表网上查不到——稍后重试，或用 fetch_url 打开已知页面")
        html = _grab(f"https://html.duckduckgo.com/html/?{q}")
    except Exception as e:  # noqa: BLE001
        return None, f"Error: 搜索后端不可用: {e}"

    results: list[SearchResult] = []
    for m in re.finditer(
        r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        html,
        re.DOTALL,
    ):
        href = _extract_ddg_url(urllib.parse.unquote(m.group("href")))
        title = re.sub(r"<[^>]+>", "", m.group("title")).strip()
        snippet = re.sub(r"<[^>]+>", "", m.group("snippet")).strip()
        results.append(SearchResult(title=title, url=href, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------------------
# 带 key 的检索后端：前端「设置」配哪一路就只走哪一路
# --------------------------------------------------------------------------

#: 四路的共同点：结果是 JSON、按 key 或实例地址鉴权、不看这台机器的 IP 像不像爬虫。
KEYED_SEARCH_PROVIDERS = ("deepseek", "brave", "tavily", "serpapi", "searxng")


# --------------------------------------------------------------------------
# DeepSeek 官方 Search / Fetch（2026-10-18 接入）
#
# 与 DeepSeek 模型 API 同一个 Bearer 鉴权，所以**默认沿用模型密钥**：模型地址是
# api.deepseek.com、又没在设置页另配搜索后端时，联网检索与抓取都走它（见
# `deepseek_auto_key`）。为什么要换：原先的免 key 回落链（维基 + Bing RSS + DuckDuckGo）
# 中文长尾召回差，直接抓取又常被反爬挡（真机 run 里知乎 403、抓不到正文）；官方 Fetch
# 直接回 Markdown 正文，Search 自带 600~800 字的长摘要（给模型读的那一档）。
#
# 价目：Search / Fetch 各 **5 元 / 千次成功请求**（失败、无结果、超时不计费）。每次成功
# 的回执末尾带一行 `node_cost_yuan=0.005`，主循环据此记进这条 run 的预算（见
# `agent._charge_nodes`）。限速：账号级各 5 QPS，Fetch 另有按目标域名的限速。
# --------------------------------------------------------------------------

DEEPSEEK_TOOLS_BASE = "https://api.deepseek.com"
DEEPSEEK_CALL_YUAN = 0.005
#: 官方说明：408 / 429 / 5xx 等一下再重试；402 余额不足；其余 4xx 改了请求才重试。
_DEEPSEEK_RETRY = frozenset({408, 429, 500, 502, 503, 504})
_DEEPSEEK_RETRIES = 2
#: 计费行的写法（主循环 `_charge_nodes` 认它）。
COST_LINE = "[计费 node_cost_yuan={:.3f}]"
#: 「没另配后端、沿用模型密钥走 DeepSeek」那一路的后端名（回执首行与回落判断都用它）。
DEEPSEEK_AUTO_LABEL = "DeepSeek 官方检索（沿用模型密钥）"


class DeepSeekToolError(RuntimeError):
    """DeepSeek Search / Fetch 回了非 200（重试用完之后）。``status`` 留给调用方分流。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)


def _deepseek_call(path: str, body: dict[str, Any], key: str, timeout: float,
                   transport: Callable[..., _Fetched] | None = None) -> dict[str, Any]:
    tp = transport or default_transport
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json"}
    for attempt in range(_DEEPSEEK_RETRIES + 1):
        fetched = tp(DEEPSEEK_TOOLS_BASE + path, 8_000_000, timeout, headers=headers, data=data)
        text = fetched.body.decode("utf-8", errors="replace")
        if fetched.status == 200:
            try:
                got = json.loads(text)
            except ValueError as e:
                raise DeepSeekToolError(200, f"返回不是 JSON（{e}）") from e
            if not isinstance(got, dict):
                raise DeepSeekToolError(200, "返回的 JSON 不是对象")
            return got
        if fetched.status in _DEEPSEEK_RETRY and attempt < _DEEPSEEK_RETRIES:
            time.sleep(1.0 * (attempt + 1))
            continue
        try:
            msg = str((json.loads(text).get("error") or {}).get("message") or "")
        except (ValueError, AttributeError):
            msg = ""
        if fetched.status == 402:
            msg = "DeepSeek 账户余额不足（402），请先充值"
        elif fetched.status == 401:
            msg = "DeepSeek API Key 无效或缺失（401）"
        raise DeepSeekToolError(fetched.status, msg or f"HTTP {fetched.status}")
    raise DeepSeekToolError(0, "重试用完")


def deepseek_provider(query: str, max_results: int, timeout: float = 20.0,
                      transport: Callable[..., _Fetched] | None = None, *,
                      key: str = "", base_url: str = "") -> list[SearchResult]:
    """DeepSeek 官方 Search：长摘要（summary）给模型读，带发布日期。"""
    q = (query or "").strip()[:128]
    payload = _deepseek_call("/v1/tools/search",
                             {"query": q, "count": max(1, min(int(max_results or 5), 50)),
                              "need_content": False}, key, timeout, transport)
    out: list[SearchResult] = []
    for r in (payload.get("results") or [])[:max_results]:
        if not isinstance(r, dict):
            continue
        date = str(r.get("date") or "")[:10]
        text = str(r.get("summary") or r.get("snippet") or "").strip()
        out.append(SearchResult(
            title=_strip_tags(str(r.get("title") or "")), url=str(r.get("url") or ""),
            snippet=(f"（{date}）" if date else "") + text, backend="DeepSeek"))
    return out


async def deepseek_auto_key() -> str:
    """没另配搜索后端时，模型密钥能不能直接拿来调 DeepSeek Search / Fetch → 那把 key（不能就空串）。

    条件只有一个：这位用户的主模型地址是 api.deepseek.com（同一个账号、同一种鉴权）。
    别家的模型 key 不许往 DeepSeek 发——那等于把 key 交到别人家门口。
    环境变量 ``WEB_TOOLS_DEEPSEEK=0`` 关掉这一路（回到免 key 回落链）。
    """
    if os.environ.get("WEB_TOOLS_DEEPSEEK", "1").strip().lower() in ("0", "false", "no"):
        return ""
    from ..llm_openai import get_default_llm
    from ..secrets import resolve_api_key, resolve_model_base
    llm = get_default_llm()
    _model, base = await resolve_model_base(fallback_model=llm.model,
                                            fallback_base=llm.base_url)
    if "api.deepseek.com" not in (base or "").lower():
        return ""
    key, _source = await resolve_api_key(fallback=getattr(llm, "api_key", "") or "")
    return key


def brave_provider(query: str, max_results: int, timeout: float = 20.0,
                   transport: Callable[..., _Fetched] | None = None, *,
                   key: str = "") -> list[SearchResult]:
    payload, _ = _fetch_json(
        transport,
        "https://api.search.brave.com/res/v1/web/search?"
        + urllib.parse.urlencode({"q": query, "count": max(1, min(max_results, 20)),
                                  "safesearch": "moderate"}),
        timeout, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    rows = ((payload.get("web") or {}).get("results") or [])[:max_results]
    return [SearchResult(title=_strip_tags(r.get("title")), url=str(r.get("url") or ""),
                         snippet=_strip_tags(r.get("description"))) for r in rows]


def tavily_provider(query: str, max_results: int, timeout: float = 20.0,
                    transport: Callable[..., _Fetched] | None = None, *,
                    key: str = "") -> list[SearchResult]:
    body = json.dumps({"query": query, "max_results": max(1, min(max_results, 20)),
                       "include_answer": False}).encode()
    payload, _ = _fetch_json(transport, "https://api.tavily.com/search", timeout,
                             headers={"Accept": "application/json",
                                      "Content-Type": "application/json",
                                      "Authorization": f"Bearer {key}"}, data=body)
    rows = (payload.get("results") or [])[:max_results]
    return [SearchResult(title=_strip_tags(r.get("title")), url=str(r.get("url") or ""),
                         snippet=_strip_tags(r.get("content"))) for r in rows]


def serpapi_provider(query: str, max_results: int, timeout: float = 20.0,
                     transport: Callable[..., _Fetched] | None = None, *,
                     key: str = "") -> list[SearchResult]:
    # 中文查询要显式要中文界面，否则 SerpAPI 按美国英文结果排，标题全是拼音站。
    params = {"engine": "google", "q": query, "num": max(1, min(max_results, 20)),
              "api_key": key}
    params.update({"hl": "zh-cn", "gl": "cn"} if _has_cjk(query) else {"hl": "en"})
    payload, _ = _fetch_json(
        transport, "https://serpapi.com/search.json?" + urllib.parse.urlencode(params),
        timeout, headers={"Accept": "application/json"})
    rows = (payload.get("organic_results") or [])[:max_results]
    return [SearchResult(title=_strip_tags(r.get("title")), url=str(r.get("link") or ""),
                         snippet=_strip_tags(str(r.get("snippet") or ""))) for r in rows]


def searxng_provider(query: str, max_results: int, timeout: float = 20.0,
                     transport: Callable[..., _Fetched] | None = None, *,
                     base_url: str = "", key: str = "") -> list[SearchResult]:
    """自建 SearXNG 实例：无 key，聚合多家引擎，中文长尾与新近内容都查得到。

    ``format=json`` 需要在实例的 settings.yml 打开 ``json_format``（默认不开）；
    没开时回 403，由 _BackendBlocked 报出来。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise _BackendBadShape("searxng 需要自建实例地址（设置页那一栏）")
    headers = {"Accept": "application/json"}
    if key:
        headers["X-Proxy-Secret"] = key
    payload, _ = _fetch_json(
        transport, base + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "pageno": 1}), timeout, headers=headers)
    rows = (payload.get("results") or [])[:max_results]
    return [SearchResult(title=_strip_tags(r.get("title")), url=str(r.get("url") or ""),
                         snippet=_strip_tags(r.get("content") or r.get("snippet")))
            for r in rows]


# provider 名 → (实现, 要不要 key, 要不要自建地址)
_KEYED: dict[str, tuple[Callable[..., list[SearchResult]], bool, bool]] = {
    "deepseek": (deepseek_provider, True, False),
    "brave": (brave_provider, True, False),
    "tavily": (tavily_provider, True, False),
    "serpapi": (serpapi_provider, True, False),
    "searxng": (searxng_provider, False, True),
}


def keyed_provider(name: str, key: str, base_url: str):
    """按配置装配带 key 的后端，返回 (callable, 错误信息)——两者只有一个非空。"""
    spec = _KEYED.get((name or "").strip().lower())
    if spec is None:
        return None, (f"Error: 搜索后端 {name!r} 不认识（可用："
                      f"{', '.join(KEYED_SEARCH_PROVIDERS)}；留空 = 免 key 回落链）")
    fn, need_key, need_base = spec
    if need_key and not key:
        return None, f"Error: 搜索后端 {name} 需要 api_key，但这一层读不到 key"
    if need_base and not base_url:
        return None, f"Error: 搜索后端 {name} 需要自建实例地址（base_url）"
    return (lambda q, n, timeout=20.0: fn(q, n, timeout, key=key, base_url=base_url)), ""


# --------------------------------------------------------------------------
# 免 key 回落链：谁在这台机器上真回结果，谁排前面
# --------------------------------------------------------------------------

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u3130-\u318f]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


_LATIN_WORD = re.compile(r"[a-z0-9]{2,}")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def _query_terms(query: str) -> tuple[set[str], set[str]]:
    """查询拆成两组：强词（拉丁词 + 中文 2 字组）与弱字（单个汉字/假名）。

    这里没有分词器可用，bigram 是不依赖词表的唯一取法：「薄肌训练原理」→
    {薄肌, 肌训, 训练, 练原, 原理}。强词对一个都碰不上时再放宽到单个字——
    放宽是为了不错杀**同义改写**（查「薄肌」回「肌肉」：bigram 零重合但共享一个「肌」），
    而闸真正要杀的是「连一个字都不相干」（查「薄肌训练原理」回「2021年香港」）。
    两组词能碰的部位不一样：强词标题与摘要都算，弱字组只算标题（为什么这么切见
    `_mark_relevance`）。
    """
    text = (query or "").lower()
    strong = set(_LATIN_WORD.findall(text))
    weak: set[str] = set()
    for run in _CJK_RUN.findall(text):
        weak.update(run)
        strong.update(run[i:i + 2] for i in range(len(run) - 1))
    return strong, weak


def _mark_relevance(query: str, rows: list[SearchResult]) -> list[SearchResult]:
    """给每条结果打上 `relevant`：与查询词够不上重合的标 False。

    **两组词比对的部位不一样**：强词（拉丁词 / 中文二字组）在标题或摘要里命中都算，
    弱字组（单个汉字/假名）只许命中标题。摘要的位置这么切不是收紧偏好，是真机量出来的:
    2026-10-11 查「薄肌 训练 原理」，免 key 链交回《医学词汇表》《火影忍者角色列表》
    《FAIRY TAIL角色列表》，它们与查询的唯一字面重合是**摘要里的一个单字**（「骨骼肌」的
    「肌」）。摘要是上百字的正文片段，任何中文查询都能在里面捞到一个共用字——按摘要算单字
    重合，等于这条闸对中文长尾查询不判。标题是页面自己声明的主题，所以「薄肌」对上「肌肉」
    还是走标题里的「肌」，同义改写救得回来。

    判据到字面重合为止——它是能被机器证明的事实，不是相关性排序。排序要懂语义，这条链没有
    那个能力；所以这条闸治的是「把无关页当证据」，治不了「相关但不够权威」。
    """
    strong, weak = _query_terms(query)
    if not (strong or weak):        # 查询里一个可比对的词都没有：不判，全留
        for row in rows:
            row.relevant = True
        return rows
    for row in rows:
        title = (row.title or "").lower()
        hay = f"{row.title} {row.snippet}".lower()
        row.relevant = any(t in hay for t in strong) or any(c in title for c in weak)
    return rows


def wikipedia_provider(query: str, max_results: int, timeout: float = 20.0,
                       transport: Callable[..., _Fetched] | None = None, *,
                       lang: str = "zh") -> list[SearchResult]:
    """维基百科搜索 API（匿名可用、稳定、回 JSON）。

    真机探点（2026-10-08）：DDG 两个端点、mojeek、brave 网页版、startpage、ecosia
    从这台机器全部 403/429/202 反爬壳，只有这里和 Bing RSS 回真结果。但它的覆盖面
    是百科——新近数字、长尾中文网页不在里面，所以**只当链上的一路，不当唯一**。
    """
    payload, _ = _fetch_json(
        transport,
        f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "query", "list": "search", "format": "json",
             "srlimit": max(1, min(max_results, 20)), "srsearch": query}),
        timeout, headers={"Accept": "application/json"})
    rows = ((payload.get("query") or {}).get("search") or [])[:max_results]
    label = "维基百科" if lang == "zh" else "Wikipedia"
    out: list[SearchResult] = []
    for r in rows:
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        out.append(SearchResult(
            title=f"{title} · {label}",
            url=f"https://{lang}.wikipedia.org/wiki/"
                + urllib.parse.quote(title.replace(" ", "_")),
            snippet=_strip_tags(str(r.get("snippet") or ""))))
    return out


def bing_rss_provider(query: str, max_results: int, timeout: float = 20.0,
                      transport: Callable[..., _Fetched] | None = None
                      ) -> list[SearchResult]:
    """Bing 的 RSS 出口（``format=rss``）：机器可读、没有反爬壳，真机 200 且结果对题。

    但它**对中文查询只认第一个词**（``mkt=zh-CN``/``setlang``/``ensearch`` 都试过：
    「一茶匙中子星」→ 百度百科「一」）。所以只在纯拉丁文字查询上用它。
    """
    tp = transport or default_transport
    url = ("https://www.bing.com/search?"
           + urllib.parse.urlencode({"format": "rss", "q": query,
                                     "count": max(1, min(max_results, 30))}))
    fetched = tp(url, 2_000_000, timeout)
    body = fetched.body.decode("utf-8", errors="replace")
    if _blocked(fetched.status, body):
        raise _BackendBlocked(fetched.status)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise _BackendBadShape(f"RSS 结构变更（{e}）") from e
    out: list[SearchResult] = []
    for item in root.iter("item"):
        title = _strip_tags(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        out.append(SearchResult(title=title, url=link,
                                snippet=_strip_tags(item.findtext("description") or "")))
        if len(out) >= max_results:
            break
    return out


def auto_provider(query: str, max_results: int, timeout: float = 20.0,
                  transport: Callable[..., _Fetched] | None = None,
                  ) -> list[SearchResult] | tuple[None, str]:
    """免 key 的检索链：按查询文字路由，逐路试到出结果为止。

    路由规则来自真机探点而不是猜测：中文走维基 API（Bing RSS 会把中文查询截成一个
    词头），拉丁文字走 Bing RSS（覆盖面比百科广）；DDG 排最后——它从这台机器两个端点
    都被人机验证挡住，换 UA、改 POST 都一样，那是出口 IP 的信誉问题。

    第三条规则是「什么才算出结果」：某一路回了条目不等于查到了——**至少要有一条与
    查询词字面重合**才算命中，否则接着试下一路。重合判据只认字面（拉丁词 / 中文
    2 字组，兜底到单个汉字），不判语义，所以它拦得住《2021年香港》这种彻底不相干的
    页，拦不住「相关但不够权威」——那是另一条链（人审 + 出处闸）的活。
    """
    stages: list[tuple[str, Callable[[], list[SearchResult] | tuple[None, str]]]] = []
    if _has_cjk(query):
        stages.append(("中文维基百科 API",
                       lambda: wikipedia_provider(query, max_results, timeout,
                                                  transport=transport, lang="zh")))
    else:
        stages.append(("Bing RSS",
                       lambda: bing_rss_provider(query, max_results, timeout, transport)))
        stages.append(("英文维基百科 API",
                       lambda: wikipedia_provider(query, max_results, timeout,
                                                  transport=transport, lang="en")))
    stages.append(("DuckDuckGo",
                   lambda: duckduckgo_provider(query, max_results, timeout, transport)))

    notes: list[str] = []
    unrelated_total = 0
    for label, run in stages:
        try:
            got = run()
        except _BackendBlocked:
            notes.append(f"{label}：被人机验证/限流挡下")
            continue
        except _BackendBadShape as e:
            notes.append(f"{label}：返回结构不对（{e}）")
            continue
        except Exception as e:  # noqa: BLE001 - 一路抖动不该让整条链报死
            notes.append(f"{label}：不可用（{type(e).__name__}）")
            continue
        if isinstance(got, tuple):      # DDG 用 (None, 说明) 表达后端状态
            notes.append(f"{label}：{str(got[1]).removeprefix('Error: ')}")
            continue
        if got:
            # 「后端回了条目」≠「查到了」：真机实测免 key 链对「薄肌训练原理」
            # 回过一篇《2021年香港》，模型把它当出处继续往下写。零重合的条目在这里就丢，
            # 不进结果、也不进检索账（那条账是「出处真实」闸的唯一凭据）。
            _mark_relevance(query, got)
            kept = [row for row in got if row.relevant]
            dropped = len(got) - len(kept)
            unrelated_total += dropped
            if kept:
                for row in kept:
                    row.backend = row.backend or label
                if unrelated_total:
                    # 丢弃数挂在首条的后端标签上：SearchTool 的回执首行就是从这里取的。
                    # 用「链上累计」而不是「本路」——被丢的可能来自更早那一路，
                    # 而下一路自己也有更精确的后端名（DDG 分 lite/html），都不该被覆盖掉。
                    kept[0].backend = (f"{kept[0].backend}"
                                       f"｜链上已丢弃 {unrelated_total} 条零重合结果")
                return kept
            notes.append(f"{label}：回的 {len(got)} 条与查询词**零重合**（这条链按字面匹配，"
                         f"不认同义改写）")
            continue
        notes.append(f"{label}：0 条")

    detail = "；".join(notes)
    hint = ("想稳定查中文长尾与新近数据，请在前端「设置」配搜索后端"
            "（brave / tavily / serpapi / 自建 SearXNG）")
    if unrelated_total:
        # 各路都回了东西、但没一条对得上查询词。这条要说清楚「不是后端坏了」：
        # notes 里已经带上各路的原话（0 条 / 被挡 / 结构不对），这里只补最相关的那句。
        return None, (
            f"Error: 免 key 检索链各路都只回了**与查询词零重合**的页面"
            f"（共丢弃 {unrelated_total} 条，一条都没记进本会话的检索账——"
            f"没记进去的页面不能当出处引用）。{detail}。"
            f"这条闸只认字面重合，不判语义，所以**查不到不等于事实不存在**："
            f"换更具体或更宽泛的关键词再试、用 fetch_url 打开你已知的页面，或{hint}；"
            f"没查证到的内容不要写成查证过的出处。同一句查询原样重打没有用。")
    if any("人机验证" in n or "限流" in n for n in notes):
        return None, (f"Error: 免 key 检索链被后端挡住（{detail}）。这是**这台机器的出口被判成爬虫**，"
                      f"不代表网上查不到——可稍后重试、用 fetch_url 打开已知页面，或{hint}。")
    return None, (f"检索链各路都没有命中（{detail}）。免 key 后端的覆盖面只有百科与通用网页，"
                  f"新近数字与长尾内容本来就不在其中——**查不到不等于事实不存在**，"
                  f"换关键词再试、用 fetch_url 打开已知页面，或{hint}；"
                  f"没查证到的内容不要写成查证过的出处。")


def provider_refusal(out: Any) -> str:
    """检索链用 `(None, 说明)` 表达「跑完了但一条都没留下」，其余形状不算拒绝。

    单源：工具的执行口和设置页的连通自检读同一句话——不然会出现「工具回 Error，
    自检却说后端有响应」这种自相矛盾的回执（零相关闸落地后真就差一步撞上）。
    """
    return str(out[1]) if isinstance(out, tuple) else ""


class SearchTool(Tool):
    """联网检索，返回标题 / 链接 / 摘要列表。

    后端按「注入的 provider → 前端「设置」/环境变量配的搜索 API → 免 key 回落链」
    的顺序解析，**每次调用现读**——用户刚在页面上填了 key 不必重启任何进程。
    """

    def __init__(self, provider: SearchProvider | None = None, timeout: float = 20.0,
                 transport: Callable[..., _Fetched] | None = None) -> None:
        self._provider = provider
        self._transport = transport
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def display_name(self) -> str:
        return "联网检索"

    @property
    def description(self) -> str:
        return ("搜索互联网信息，返回若干条标题、链接与摘要（回执首行标出这次是哪个后端出的）。"
                "可据结果再用 fetch_url 深入抓取。返回 0 条只代表这些后端没有命中，"
                "不代表事实不存在：换关键词、用 fetch_url 打开已知页面，"
                "或在设置页配置搜索后端（deepseek / brave / tavily / serpapi / 自建 SearXNG；"
                "模型用的是 DeepSeek 时默认就走 DeepSeek 官方检索）。"
                "免 key 回落链按字面匹配，与查询词零重合的条目会被丢弃、也不记进检索账。"
                "本工具与 fetch_url 真打开过的页面会记进会话的检索账，"
                "分镜里的出处只有能对上一笔账才算查证过——没查过就标「未核实」。")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "max_results": {"type": "integer", "description": "返回条数上限，默认 5"},
            },
            "required": ["query"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def _runner(self) -> tuple[Callable[..., Any] | None, str, str]:
        """解析这次用哪个后端，返回 (callable, 后端名, 错误信息)。"""
        if self._provider is not None:
            return self._provider, "注入后端", ""
        from ..secrets import resolve_search_config
        name, key, base, source = await resolve_search_config()
        name = (name or "").strip().lower()
        if not (name or key or base):
            # 没另配后端：模型密钥是 DeepSeek 的就直接用官方 Search（见 `deepseek_auto_key`）。
            # 注入了假 transport（离线用例）时不走它——用例不该打真接口。
            ds_key = "" if self._transport is not None else await deepseek_auto_key()
            if ds_key:
                fn, err = keyed_provider("deepseek", ds_key, "")
                return fn, DEEPSEEK_AUTO_LABEL, err
            return ((lambda q, n: auto_provider(q, n, self.timeout, self._transport)),
                    "免 key 回落链", "")
        if not name:
            # 猜一个服务商等于把这把 key 发到别人家门口——宁可退回让人选。
            return None, "", ("Error: 设置了搜索密钥或实例地址但没选后端，"
                              f"无法判断该把 key 交给谁（可用：{', '.join(KEYED_SEARCH_PROVIDERS)}）")
        fn, err = keyed_provider(name, key, base)
        return fn, f"{source}·{name}", err

    async def execute(self, query: str, max_results: int = 5) -> str:
        runner, label, err = await self._runner()
        if err:
            return err
        if runner is None:
            return "Error: 未配置可用的搜索后端"
        note = ""
        try:
            out = await asyncio.to_thread(runner, query, max_results)
        except DeepSeekToolError as e:
            if e.status in (401, 402):
                return f"Error: {e}"
            if label != DEEPSEEK_AUTO_LABEL:
                return f"Error: 搜索失败（DeepSeek HTTP {e.status}）: {e}"
            # 沿用模型密钥那一路出了岔子（限流、超时、服务端错）：退回免 key 回落链，
            # 别让一次检索因为换了后端就整个失败。
            note = f"（DeepSeek 检索这一次没成：HTTP {e.status}，已改用免 key 回落链）\n"
            label = "免 key 回落链"
            try:
                out = await asyncio.to_thread(
                    lambda: auto_provider(query, max_results, self.timeout, self._transport))
            except Exception as e2:  # noqa: BLE001
                return f"Error: 搜索失败: {e2}"
        except Exception as e:  # noqa: BLE001
            return f"Error: 搜索失败: {e}"
        refusal = provider_refusal(out)
        if refusal:
            return note + refusal
        results: list[SearchResult] = out
        if not results:
            return f"{note}未找到结果：query={query!r}（后端：{label}）"
        stage = results[0].backend or label
        from ..retrieval import note_hits
        await note_hits([{"url": r.url, "title": r.title} for r in results],
                        backend=stage)
        # 官方 Search 按「成功且有结果」计费：只在这一档带计费行。
        cost = ("\n" + COST_LINE.format(DEEPSEEK_CALL_YUAN)) if stage == "DeepSeek" else ""
        return (f"{note}[检索后端：{stage}]\n" + "\n".join(r.format() for r in results) + cost)


def register_web_tools(registry, **kwargs) -> None:
    """注册 Fetch + Search 工具。可传 provider / timeout 等覆盖默认后端。"""
    registry.register(FetchTool(timeout=kwargs.get("timeout", 20.0), transport=kwargs.get("transport")))
    registry.register(SearchTool(provider=kwargs.get("provider"),
                                 timeout=kwargs.get("timeout", 20.0),
                                 transport=kwargs.get("transport")))
