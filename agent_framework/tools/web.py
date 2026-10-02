"""原生网络工具：Fetch（抓取网页转文本）与 Search（联网检索）。

设计取舍：
- 零第三方依赖：用标准库 urllib，并放进 asyncio.to_thread 执行以免阻塞事件循环。
- 可测试 / 可换后端：SearchTool 的 provider、FetchTool 的 transport 均可注入，
  验证脚本不打真实网络即可跑通。默认 provider 为 DuckDuckGo HTML 抓取（尽力而为，
  易受对方页面变动影响），生产建议替换为带 key 的正规搜索 API。
- 安全：仅允许 http/https；默认拒绝回环 / 私有网段，降低 SSRF 风险；限制响应大小。

并发标记：两者都是只读无副作用 → concurrency_safe，可进 gather 批次。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from ..tool import Tool

USER_AGENT = "Mozilla/5.0 (compatible; AgentFramework/0.1)"


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


def default_transport(url: str, max_bytes: int, timeout: float) -> _Fetched:
    """同步抓取（供 to_thread 调用）：读至多 max_bytes 字节。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme 已校验)
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        return _Fetched(resp.status, ctype, data[:max_bytes], truncated)


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

    async def execute(self, url: str, max_bytes: int = 200_000) -> str:
        err = ensure_safe_url(url)
        if err:
            return err
        try:
            fetched = await asyncio.to_thread(self._transport, url, max_bytes, self.timeout)
        except Exception as e:  # noqa: BLE001 - 网络异常回喂给 LLM
            return f"Error: 抓取失败 {url}: {e}"

        ctype = fetched.content_type.lower()
        if "html" in ctype or fetched.body.lstrip()[:15].lower().startswith(b"<!doctype html"):
            text = html_to_text(fetched.body.decode("utf-8", errors="replace"))
        else:
            text = fetched.body.decode("utf-8", errors="replace").strip()
        if not text:
            return f"(空内容) status={fetched.status} url={url}"
        if fetched.truncated:
            text += f"\n...（内容超过 {max_bytes} 字节，已截断）"
        return f"[{fetched.status}] {url}\n{text}"


# --------------------------------------------------------------------------
# SearchTool
# --------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

    def format(self) -> str:
        line = f"- {self.title}"
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
        results = _parse_lite(_grab(f"https://lite.duckduckgo.com/lite/?{q}"), max_results)
        if results:
            return results
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


class SearchTool(Tool):
    """联网检索，返回标题 / 链接 / 摘要列表。"""

    def __init__(self, provider: SearchProvider | None = None, timeout: float = 20.0) -> None:
        self._provider = provider or duckduckgo_provider
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def display_name(self) -> str:
        return "联网检索"

    @property
    def description(self) -> str:
        return "搜索互联网信息，返回若干条标题、链接与摘要。可据结果再用 fetch_url 深入抓取。"

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

    async def execute(self, query: str, max_results: int = 5) -> str:
        try:
            out = await asyncio.to_thread(self._provider, query, max_results)
        except Exception as e:  # noqa: BLE001
            return f"Error: 搜索失败: {e}"
        if isinstance(out, tuple):  # (None, error)
            return out[1]
        results: list[SearchResult] = out
        if not results:
            return f"未找到结果：query={query!r}"
        return "\n".join(r.format() for r in results)


def register_web_tools(registry, **kwargs) -> None:
    """注册 Fetch + Search 工具。可传 provider / timeout 等覆盖默认后端。"""
    registry.register(FetchTool(timeout=kwargs.get("timeout", 20.0), transport=kwargs.get("transport")))
    registry.register(SearchTool(provider=kwargs.get("provider"), timeout=kwargs.get("timeout", 20.0)))
