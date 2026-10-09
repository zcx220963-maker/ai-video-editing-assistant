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
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from ..tool import Tool

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


def default_transport(url: str, max_bytes: int, timeout: float, *,
                      headers: dict[str, str] | None = None,
                      data: bytes | None = None) -> _Fetched:
    """同步抓取（供 to_thread 调用）：读至多 max_bytes 字节。

    ``headers``/``data`` 是给带 key 的检索 API 用的（它们要 ``X-Subscription-Token``、
    ``Authorization``、POST JSON）——位置参数仍是 (url, max_bytes, timeout)，
    注入用的假 transport 不必认识这两个新参数。
    """
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(_ascii_url(url), headers=hdrs, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme 已校验)
        ctype = resp.headers.get("Content-Type", "")
        data_out = resp.read(max_bytes + 1)
        truncated = len(data_out) > max_bytes
        return _Fetched(resp.status, ctype, data_out[:max_bytes], truncated)


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
        # 抓到正文才算「这一页真打开过」：出处闸认的是这一笔，不是模型的口头声明。
        if 200 <= fetched.status < 400:
            from ..retrieval import note_hit
            await note_hit(url, "", backend="fetch_url")
        return f"[{fetched.status}] {url}\n{text}"


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
KEYED_SEARCH_PROVIDERS = ("brave", "tavily", "serpapi", "searxng")


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
            for row in got:
                row.backend = row.backend or label
            return got
        notes.append(f"{label}：0 条")

    detail = "；".join(notes)
    hint = ("想稳定查中文长尾与新近数据，请在前端「设置」配搜索后端"
            "（brave / tavily / serpapi / 自建 SearXNG）")
    if any("人机验证" in n or "限流" in n for n in notes):
        return None, (f"Error: 免 key 检索链被后端挡住（{detail}）。这是**这台机器的出口被判成爬虫**，"
                      f"不代表网上查不到——可稍后重试、用 fetch_url 打开已知页面，或{hint}。")
    return None, (f"检索链各路都没有命中（{detail}）。免 key 后端的覆盖面只有百科与通用网页，"
                  f"新近数字与长尾内容本来就不在其中——**查不到不等于事实不存在**，"
                  f"换关键词再试、用 fetch_url 打开已知页面，或{hint}；"
                  f"没查证到的内容不要写成查证过的出处。")


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
                "或在设置页配置搜索后端（brave / tavily / serpapi / 自建 SearXNG）。"
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
        try:
            out = await asyncio.to_thread(runner, query, max_results)
        except Exception as e:  # noqa: BLE001
            return f"Error: 搜索失败: {e}"
        if isinstance(out, tuple):  # (None, error)
            return out[1]
        results: list[SearchResult] = out
        if not results:
            return f"未找到结果：query={query!r}（后端：{label}）"
        stage = results[0].backend or label
        from ..retrieval import note_hits
        await note_hits([{"url": r.url, "title": r.title} for r in results],
                        backend=stage)
        return f"[检索后端：{stage}]\n" + "\n".join(r.format() for r in results)


def register_web_tools(registry, **kwargs) -> None:
    """注册 Fetch + Search 工具。可传 provider / timeout 等覆盖默认后端。"""
    registry.register(FetchTool(timeout=kwargs.get("timeout", 20.0), transport=kwargs.get("transport")))
    registry.register(SearchTool(provider=kwargs.get("provider"),
                                 timeout=kwargs.get("timeout", 20.0),
                                 transport=kwargs.get("transport")))
