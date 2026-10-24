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


def case_bot_challenge_is_not_no_results() -> None:
    """「后端要人机验证」≠「网上没有这个数」——混回会让模型把能核实的数字判成查不到。"""
    print("\n[DDG 反爬页要如实报成后端问题]")
    challenge = (b"<html><body>Unfortunately, bots use DuckDuckGo too. "
                 b"Select all squares containing a duck</body></html>")

    def tp(url: str, max_bytes: int, timeout: float) -> _Fetched:
        return _Fetched(200, "text/html", challenge[:max_bytes], False)

    out = duckduckgo_provider("q", 5, transport=tp)
    check(isinstance(out, tuple) and "人机验证" in out[1],
          f"反爬页回的是后端错误（{out if not isinstance(out, tuple) else out[1][:60]}）")


class _FakeResp:
    status = 200
    headers = {"Content-Type": "text/html"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return b"<p>ok</p>"[:n]


def case_zero_overlap_is_not_a_result() -> None:
    """「后端回了条目」≠「查到了」：免 key 链按字面匹配，零重合的条目不许当结果。

    真机形状：查「薄肌训练原理」，中文维基百科回的是《2021年香港》——一次都没失败，
    模型把它当出处继续写，同参重打 20+ 次直到把整条 run 烧到配额上限。
    """
    from agent_framework.tools import web as web_mod

    print("\n[免 key 链的相关性闸：零重合不算出结果]")

    strong, weak = web_mod._query_terms("薄肌 训练 原理")
    check({"薄肌", "训练", "原理"} <= strong, f"① 中文按 2 字组取强词：{sorted(strong)}")
    check("薄" in weak and "理" in weak, f"① 单字进弱字组兜同义：{sorted(weak)}")
    check(web_mod._query_terms("neutron star mass")[0] >= {"neutron", "star", "mass"},
          "① 拉丁按词取强词")

    row = SearchResult(title="2021年香港", url="https://zh.wikipedia.org/wiki/2021年香港",
                       snippet="本年度大事记")
    kept = web_mod._mark_relevance("薄肌 训练 原理", [row])
    check(kept[0].relevant is False, "② 与查询词一个字都不重合的条目标 False")
    muscle = SearchResult(title="肌肉", url="https://x/m", snippet="骨骼肌的收缩机制")
    check(web_mod._mark_relevance("薄肌 训练 原理", [muscle])[0].relevant is True,
          "② 共享单个汉字（肌）算相关——同义改写不许错杀")
    check(web_mod._mark_relevance("  ", [SearchResult("任意", "u", "s")])[0].relevant is True,
          "② 查询里没有可比对的词时闸不判（全留）")

    # ②-b 真机形状（2026-10-11 /settings/search/test 查「薄肌 训练 原理」交回的样例标题：
    # 《医学词汇表》《火影忍者角色列表》《FAIRY TAIL角色列表》）。它们与查询的唯一字面重合
    # 是**摘要里的一个单字**——摘要动辄上百字，任何中文查询都能在里面捞到一个共用字。
    # 所以弱字组只许命中标题（标题是页面自己声明的主题，「薄肌」对上「肌肉」靠的就是它），
    # 强词（拉丁词 / 二字组）在标题或摘要里命中都算。
    snippet_only = SearchResult(title="医学词汇表 · 维基百科",
                                url="https://zh.wikipedia.org/wiki/医学词汇表",
                                snippet="本表收录骨骼肌、心肌、平滑肌等词条")
    check(web_mod._mark_relevance("薄肌 训练 原理", [snippet_only])[0].relevant is False,
          "②-b 只在摘要里撞上一个单字不算相关（弱字组不许碰摘要）")
    title_hit = SearchResult(title="肌肉 · 维基百科", url="https://zh.wikipedia.org/wiki/肌肉",
                             snippet="人体骨骼肌的收缩机制")
    check(web_mod._mark_relevance("薄肌 训练 原理", [title_hit])[0].relevant is True,
          "②-b 单字重合落在标题里算相关——同义改写仍然不许错杀")
    strong_in_snippet = SearchResult(title="抗阻练习手册", url="https://x/h",
                                     snippet="系统讲解肌肉训练的强度安排")
    check(web_mod._mark_relevance("薄肌 训练 原理", [strong_in_snippet])[0].relevant is True,
          "②-b 二字组（训练）落在摘要里照样算相关（强词不限位置）")

    def _auto_with_fakes(query: str, rows_by_label: dict[str, list[dict]],
                         max_results: int = 5):
        """把免 key 链的三路后端换成固定返回，跑一次 auto_provider，跑完必还原。"""
        def fake(label: str):
            def run(q, n, timeout=20.0, *a, **k):
                return [SearchResult(**r) for r in rows_by_label[label]]
            return run
        saved = (web_mod.wikipedia_provider, web_mod.bing_rss_provider,
                 web_mod.duckduckgo_provider)
        web_mod.wikipedia_provider = fake("zh-wiki")
        web_mod.bing_rss_provider = fake("bing")
        web_mod.duckduckgo_provider = fake("ddg")
        try:
            return web_mod.auto_provider(query, max_results)
        finally:
            (web_mod.wikipedia_provider, web_mod.bing_rss_provider,
             web_mod.duckduckgo_provider) = saved

    # ③ 第一路全是无关页 → 不许就此收工，继续往下一路试
    out = _auto_with_fakes("薄肌 训练 原理", {
        "zh-wiki": [{"title": "2021年香港", "url": "https://a/1", "snippet": "大事记"}],
        "bing": [],
        "ddg": [{"title": "肌纤维类型与训练", "url": "https://b/2",
                 "snippet": "慢肌与快肌"}],
    })
    check(isinstance(out, list) and len(out) == 1,
          f"③ 零重合的那路被跳过、下一路的对题结果留下：{out}")
    check(bool(out) and "链上已丢弃 1 条零重合" in out[0].backend,
          f"③ 回执里写明丢弃数（后端标签）：{out[0].backend if out else ''}")

    # ④ 各路全回无关页 → (None, Error)，措辞要说清「不是后端坏了、也没进检索账」
    out2 = _auto_with_fakes("薄肌 训练 原理", {
        "zh-wiki": [{"title": "2021年香港", "url": "https://a/1", "snippet": "大事记"}],
        "bing": [],
        "ddg": [{"title": "撒哈拉沙漠", "url": "https://c/3", "snippet": "沙丘"}],
    })
    check(isinstance(out2, tuple), f"④ 全零重合时不回条目：{type(out2)}")
    msg = out2[1] if isinstance(out2, tuple) else ""
    check(msg.startswith("Error:"),
          f"④ 回执是 Error 开头，失败守卫看得见（原样重打就此封顶）：{msg[:24]}")
    check("零重合" in msg and "检索账" in msg and "查不到不等于事实不存在" in msg,
          f"④ 话里三件事实齐：丢弃原因 / 不能当出处 / 不等于不存在：{msg[:120]}")
    check("原样重打" in msg, f"④ 明说同句重打没有用：{msg[-90:]}")

    # ⑤ 同一路里对题与无关并存：丢掉的绝不混进结果（也就绝不进检索账）
    out3 = _auto_with_fakes("薄肌 训练 原理", {
        "zh-wiki": [{"title": "薄肌是什么", "url": "https://d/4", "snippet": "皮下脂肪"},
                    {"title": "2021年香港", "url": "https://a/1", "snippet": "大事记"}],
        "bing": [], "ddg": [],
    })
    check(isinstance(out3, list) and all("2021年香港" not in r.title for r in out3)
          and len(out3) == 1, f"⑤ 对题的留下、无关的不出现：{[r.title for r in out3]}")
    check(web_mod.wikipedia_provider.__name__ != "run",
          "⑤ 假后端跑完已还原（不污染后面的用例）")


def case_ascii_url_transport() -> None:
    """真机踩到的：``…/wiki/中子星`` 报的是编码错，不是抓不到页。"""
    print("\n[默认 transport：非 ASCII 链接要先编成 URL 安全形式]")
    from agent_framework.tools import web as web_mod

    seen: list[str] = []
    real = web_mod.urllib.request.urlopen

    def spy(req, timeout=None):  # noqa: ANN001
        seen.append(req.full_url)
        return _FakeResp()

    web_mod.urllib.request.urlopen = spy
    try:
        web_mod.default_transport("https://zh.wikipedia.org/wiki/中子星", 1000, 5)
        web_mod.default_transport("https://ex.com/a%20b c", 1000, 5)
    finally:
        web_mod.urllib.request.urlopen = real
    check(len(seen) == 2 and all(u.isascii() for u in seen),
          f"送进 urllib 的 URL 全 ASCII（{seen}）")
    check("%E4%B8%AD%E5%AD%90%E6%98%9F" in seen[0], "中文路径按 UTF-8 百分号编码")
    check("a%20b%20c" in seen[1] and "%2520" not in seen[1],
          "空格被补上编码，已编码的 % 不被二次编码")


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
    case_bot_challenge_is_not_no_results()
    case_zero_overlap_is_not_a_result()
    case_ascii_url_transport()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
