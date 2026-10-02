"""WebTool（Fetch/Search）验证脚本 —— 注入假后端，不产生真实网络请求。

运行：  python tests/test_web_tools.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.tool import ToolRegistry, is_tool_error
from agent_framework.tools.web import (
    _Fetched,
    FetchTool,
    SearchResult,
    SearchTool,
    _extract_ddg_url,
    duckduckgo_provider,
    ensure_safe_url,
    html_to_text,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# 假 transport：不联网，直接返回预置字节
def fake_transport(url: str, max_bytes: int, timeout: float) -> _Fetched:
    html = b"<html><head><style>x{}</style></head><body><h1>Title</h1><p>Hello world</p></body></html>"
    return _Fetched(200, "text/html; charset=utf-8", html[:max_bytes], False)


def fake_big_transport(url: str, max_bytes: int, timeout: float) -> _Fetched:
    body = b"a" * (max_bytes + 1)
    return _Fetched(200, "text/plain", body[:max_bytes], True)


def fake_provider(query: str, max_results: int):
    items = [
        SearchResult(title=f"R{i}", url=f"https://ex{i}.com", snippet=f"snippet {i}")
        for i in range(3)
    ]
    return items[:max_results]


def failing_provider(query: str, max_results: int):
    return None, "Error: 后端挂了"


_LITE_HTML = """
<html><body><table>
<tr><td>1.&nbsp;</td><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc123">Result A</a></td></tr>
<tr><td class="result-snippet">snippet A</td></tr>
<tr><td>2.&nbsp;</td><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb&amp;rut=def456">Result B</a></td></tr>
<tr><td class="result-snippet">snippet B</td></tr>
</table></body></html>
"""

_HTML_FALLBACK = """
<html><body>
<div class="result">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fc&amp;rut=xyz">Result C</a>
<a class="result__snippet" href="#">snippet C</a>
</div>
</body></html>
"""


def ddg_transport(calls: list[str]):
    def transport(url: str, max_bytes: int, timeout: float) -> _Fetched:
        calls.append(url)
        body = _LITE_HTML if "lite.duckduckgo.com" in url else _HTML_FALLBACK
        return _Fetched(200, "text/html", body.encode("utf-8")[:max_bytes], False)
    return transport


def empty_lite_transport(calls: list[str]):
    def transport(url: str, max_bytes: int, timeout: float) -> _Fetched:
        calls.append(url)
        body = b"<html><body>No results.</body></html>" \
            if "lite.duckduckgo.com" in url else _HTML_FALLBACK.encode("utf-8")
        return _Fetched(200, "text/html", body[:max_bytes], False)
    return transport


def case_duckduckgo_provider() -> None:
    print("\n[Search 默认后端：lite 优先 + html 兜底 + uddg 解析]")
    check(_extract_ddg_url(
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=abc")
        == "https://example.com/a", "uddg 解析切掉 &rut= 尾巴")
    calls: list[str] = []
    out = duckduckgo_provider("q", 5, transport=ddg_transport(calls))
    assert not isinstance(out, tuple)
    check(calls[0].startswith("https://lite.duckduckgo.com/lite/"),
          f"首选 lite 端点（{calls[0]}）")
    check(len(out) == 2 and out[0].url == "https://example.com/a"
          and "&rut=" not in out[0].url and "uddg=" not in out[0].url,
          "lite 结果解析干净（无跳转参数残留）")
    check(out[0].snippet == "snippet A", "lite 摘要解析")

    calls2: list[str] = []
    out2 = duckduckgo_provider("q", 5, transport=empty_lite_transport(calls2))
    assert not isinstance(out2, tuple)
    check(any("lite.duckduckgo.com" in u for u in calls2)
          and any("html.duckduckgo.com" in u for u in calls2),
          "lite 0 结果回落 html 端点")
    check(len(out2) == 1 and out2[0].url == "https://example.com/c",
          "html 端点兜底解析出结果")


async def main() -> None:
    reg = ToolRegistry()
    reg.register(FetchTool(transport=fake_transport))
    reg.register(SearchTool(provider=fake_provider))
    print("已注册:", reg.tool_names)

    check(reg.get("fetch_url").concurrency_safe, "fetch_url 可并发 (read_only)")
    check(reg.get("web_search").concurrency_safe, "web_search 可并发 (read_only)")

    # URL 安全
    check(ensure_safe_url("file:///etc/passwd").startswith("Error"), "拒绝 file:// 协议")
    check(ensure_safe_url("http://127.0.0.1/secret").startswith("Error"), "拒绝回环地址")
    check(ensure_safe_url("http://localhost/").startswith("Error"), "拒绝 localhost")
    check(ensure_safe_url("not-a-url").startswith("Error"), "拒绝无协议 URL")
    check(ensure_safe_url("https://8.8.8.8/dns") is None, "允许公网 IP")

    # html_to_text
    txt = html_to_text("<div><script>var x=1;</script><p>Hi</p><p>There</p></div>")
    check("Hi" in txt and "There" in txt and "var x" not in txt, "HTML 抽取纯文本并丢弃 script")

    # Fetch 正常
    r = await reg.execute("fetch_url", {"url": "https://8.8.8.8/page"})
    check(r.startswith("[200]") and "Title" in r and "Hello world" in r, f"fetch 返回状态+正文: {r[:24]!r}")
    # Fetch 被 SSRF 拦截
    r = await reg.execute("fetch_url", {"url": "http://127.0.0.1/"})
    check(is_tool_error(r), "fetch 拦截内网地址")
    # Fetch 截断
    reg2 = ToolRegistry()
    reg2.register(FetchTool(transport=fake_big_transport))
    r = await reg2.execute("fetch_url", {"url": "https://8.8.8.8/big", "max_bytes": 100})
    check("已截断" in r, "fetch 超长内容截断提示")

    # Search 正常
    r = await reg.execute("web_search", {"query": "hello", "max_results": 2})
    check("R0" in r and "R1" in r and "R2" not in r, f"search 返回并限制条数:\n{r}")
    # Search 后端报错
    reg3 = ToolRegistry()
    reg3.register(SearchTool(provider=failing_provider))
    r = await reg3.execute("web_search", {"query": "x"})
    check(is_tool_error(r), f"search 后端错误回喂: {str(r)[:20]}")

    # Search 默认 provider（C：lite 优先 / html 兜底 / uddg 解析）
    case_duckduckgo_provider()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
