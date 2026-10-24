"""抓取链的五条回归：Content-Encoding 漏解压 / 字符集硬解 / 错误页分类 / 控制字符 / JS 壳页。

真机事故（2026-10-11）：B 站页面回「[200] + 一串乱码」，紧接着一条
「PG 执行失败：unsupported Unicode escape sequence」。两条是同一个根因——
urllib **不解压**，CDN 照发的 gzip 字节被当成正文解码，里面必然带 0x00，
`json.dumps` 转成 ``\\u0000`` 写进 jsonb 列时 PG 拒收。

第二类乱码与压缩无关：中文站还在发 GBK/GB18030 页，而旧代码无条件按 UTF-8
解码，汉字解成「浣撹偛」这种看起来像日文的碎片——它不含 0x00，PG 收得下，
于是错误一路无声穿到模型面前。

运行：  python tests/test_fetch_encoding.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import gzip
import json
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.tool import ToolError
from agent_framework.tools.web import (
    _Fetched,
    FetchTool,
    _decode_body,
    _decode_text,
    _meta_text,
    _scrub_control,
    default_transport,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# --------------------------------------------------------------------------
# 本地回环服务：只喂 default_transport（不经 ensure_safe_url，SSRF 闸不在这里测）
# --------------------------------------------------------------------------

_HTML = "<html><body><h1>薄肌理论</h1><p>力量 + 刷脂 + 时间。</p></body></html>"
_SEEN_HEADERS: dict[str, str] = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):    # 静音
        pass

    def _send(self, status: int, ctype: str, body: bytes, enc: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _SEEN_HEADERS["accept-encoding"] = self.headers.get("Accept-Encoding", "")
        if self.path == "/gzip":                      # B 站那种：无视 identity 照发 gzip
            self._send(200, "text/html; charset=utf-8", gzip.compress(_HTML.encode()), "gzip")
        elif self.path == "/deflate":                 # 裸 deflate（无 zlib 套壳）
            c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
            self._send(200, "text/html; charset=utf-8",
                       c.compress(_HTML.encode()) + c.flush(), "deflate")
        elif self.path == "/403":                     # 知乎专栏那种反爬壳
            self._send(403, "text/html; charset=utf-8",
                       "<html><body>安全验证</body></html>".encode())
        elif self.path == "/nul":                     # 漏解压才会出现的 0x00
            self._send(200, "text/plain; charset=utf-8", b"ok\x00\x01\x02 tail")
        elif self.path == "/gbk":                     # js.99.com.cn 那一类：GBK 页
            self._send(200, "text/html; charset=gbk", _HTML.encode("gbk"))
        elif self.path == "/gbk-meta":                # 头里不写 charset，只在 <meta> 里声明
            meta = ("<html><head><meta http-equiv=\"Content-Type\" "
                    "content=\"text/html; charset=gbk\"></head>"
                    "<body><h1>薄肌理论</h1></body></html>")
            self._send(200, "text/html", meta.encode("gbk"))
        else:
            self._send(404, "text/plain", b"nope")


def _serve() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_transport() -> None:
    srv, port = _serve()
    base = f"http://127.0.0.1:{port}"
    try:
        f = default_transport(f"{base}/gzip", 200_000, 10.0)
        check(f.status == 200 and f.body.decode("utf-8", "replace") == _HTML,
              "Content-Encoding: gzip 真解压出正文（不是压缩流本身）")
        check("\x00" not in f.body.decode("utf-8", "replace"), "gzip 路径不残留 0x00")

        f = default_transport(f"{base}/deflate", 200_000, 10.0)
        check(f.body.decode("utf-8", "replace") == _HTML, "裸 deflate（无 zlib 套壳）也解得开")

        f = default_transport(f"{base}/403", 200_000, 10.0)
        check(f.status == 403 and "安全" in f.body.decode("utf-8", "replace"),
              "403 的错误页当正文取回（不再抛 HTTPError 让上层只剩一句抓取失败）")

        f = default_transport(f"{base}/nul", 200_000, 10.0)
        check(b"\x00" in f.body, "非压缩流的 0x00 原样带出（交给工具层剔）")

        check(_SEEN_HEADERS.get("accept-encoding") == "gzip, deflate",
              "请求里显式声明只收 gzip/deflate（不给服务端发 br 的理由）")

        f = default_transport(f"{base}/gzip", 24, 10.0)   # 预算按解压后计
        check(f.truncated and len(f.body) <= 24,
              "max_bytes 是解压后的正文预算：超了标 truncated 且不越界")
    finally:
        srv.shutdown()


def test_charset() -> None:
    """字符集：解压之外的第二类乱码来源（中文站还在发 GBK/GB18030 页）。"""
    srv, port = _serve()
    base = f"http://127.0.0.1:{port}"
    try:
        f = default_transport(f"{base}/gbk", 200_000, 10.0)
        text = _decode_text(f.body, f.content_type)
        check("薄肌理论" in text,
              f"HTTP 头声明 charset=gbk 的页按 GBK 解：{text[:40]}")
        check("\ufffd" not in text, "GBK 页解码后一个替换符都没有")

        f2 = default_transport(f"{base}/gbk-meta", 200_000, 10.0)
        check("薄肌理论" in _decode_text(f2.body, f2.content_type),
              "头里不写 charset、只在 <meta> 里声明的也认")
    finally:
        srv.shutdown()

    gbk_bytes = "薄肌理论".encode("gbk")
    check("薄肌理论" in _decode_text(gbk_bytes, "text/html; charset=gbk"),
          "gbk 统一升一档到超集 gb18030")
    check("薄肌理论" in _decode_text(gbk_bytes, "text/html; charset=windows-936"),
          "windows-936 这种写法认得")
    check("薄肌理论" in _decode_text(gbk_bytes, "text/html; charset=gb2312"),
          "gb2312 也升档（老页里混简体扩展字不会解不开）")
    check(_decode_text("你好".encode("utf-8"), "text/html; charset=utf-8") == "你好",
          "声明 utf-8 时与旧行为逐字相同")
    junk = _decode_text(b"\xff\xfe\x00\x01" + "你好".encode("gbk"),
                        "text/html; charset=x-not-a-codec")
    check(isinstance(junk, str) and "\x00" not in junk,
          "认不出的编码名不报错：退回 UTF-8 兜底并把控制字符剔掉")
    undec = _decode_text(b"\x80\x81\xfe", "text/html; charset=ascii")
    check(isinstance(undec, str), "声明的字符集解不开时退回兜底，不因编码问题让抓取报错")

    t = FetchTool(transport=lambda u, m, tc: _Fetched(200, "text/html; charset=gbk",
                                                      gbk_bytes, False))
    out = asyncio.run(t.execute("https://example.com/gbk"))
    check("薄肌理论" in out and "Error" not in out.split("\n")[0],
          f"FetchTool 出口整链：给模型的是可读汉字：{out[:60]}")


def test_decode_helpers() -> None:
    raw = gzip.compress(b"A" * 5000)
    out, trunc = _decode_body(raw[:40], "gzip", 5000)   # 压缩流被切一刀
    check(trunc and out and out == b"A" * len(out), "切过的 gzip 流不抛异常，按截断报")
    packed = gzip.compress(b"hi")
    out, trunc = _decode_body(packed, "", 1000)
    check(out == packed and not trunc, "没声明 Content-Encoding 时字节原样返回（不做猜探解压）")
    out, trunc = _decode_body(packed, "gzip", 1000)
    check(out == b"hi" and not trunc, "声明 gzip 才解压")
    try:
        _decode_body(b"\x1f\x8b\x00garbage", "gzip", 1000)
        check(False, "解不开的 gzip 要报错，不能静默把乱码当正文")
    except RuntimeError:
        check(True, "解不开的 gzip 报 RuntimeError（宁可不回，也不喂乱码）")


def test_fetch_tool() -> None:
    async def run(tool: FetchTool, url: str) -> str:
        return await tool.execute(url)

    def tool_with(fetched: _Fetched) -> FetchTool:
        return FetchTool(transport=lambda u, m, t: fetched)

    # 1) 控制字符：PG 的 jsonb 不收 U+0000，工具出口必须剔干净
    t = tool_with(_Fetched(200, "text/plain; charset=utf-8",
                           b"hello\x00world\x01\x7f end", False))
    out = asyncio.run(run(t, "https://example.com/nul"))
    check("\x00" not in out and "\x01" not in out and "\x7f" not in out,
          "工具出口剔掉控制字符")
    check("\\u0000" not in json.dumps(out, ensure_ascii=False),
          "回喂文本 json.dumps 之后不含 \\u0000（这条就是 PG 报错的那一句）")

    # 2) 403 分类：说「被挡在门外」，不说「空内容」、也不留「抓取失败」
    t = tool_with(_Fetched(403, "text/html; charset=utf-8",
                           "<html><body>安全验证</body></html>".encode(), False))
    out = asyncio.run(run(t, "https://zhuanlan.zhihu.com/p/1"))
    check("被挡在门外" in out and "不代表网上没有" in out,
          "403 回「反爬挡住 ≠ 查不到」的分类")
    check("空内容" not in out, "403 不再冒充成「(空内容)」")

    # 3) JS 壳页：抽不到正文时退回 <title>/<meta>，并明说是壳
    shell_html = ('<!doctype html><html><head><title>薄肌理论</title>'
                  '<meta property="og:description" content="不要用别人的审美">'
                  '</head><body><script>var ytInitialData = {"a":1};</script>'
                  '</body></html>').encode()
    t = tool_with(_Fetched(200, "text/html; charset=utf-8", shell_html, False))
    out = asyncio.run(run(t, "https://www.youtube.com/watch?v=x"))
    check("JS 渲染" in out or "壳" in out, "壳页明说「正文靠 JS 渲染」")
    check("薄肌理论" in out, "壳页至少把 <title> 里的可读内容交出来")

    # 4) 反向：长正文里出现 "captcha" 是内容，不是壳页 —— 不许误判成被挡
    body = ("<html><body><p>" + "captcha 这个词的英文定义是自动化人机验证测试。" * 40
            + "</p></body></html>").encode()
    t = tool_with(_Fetched(200, "text/html; charset=utf-8", body, False))
    out = asyncio.run(run(t, "https://example.com/article"))
    check("被挡在门外" not in out, "长正文里撞到 captcha 字样不误判成反爬壳")

    # 5) 短正文 ≠ 壳页：真机回归里两条红就是这个阈值——12 个字的合法小页面被 meta 吞了
    small = b"<html><head><style>x{}</style></head><body><h1>Title</h1><p>Hi</p></body></html>"
    t = tool_with(_Fetched(200, "text/html; charset=utf-8", small, False))
    out = asyncio.run(run(t, "https://example.com/tiny"))
    check("Title" in out and "Hi" in out and "JS 渲染" not in out,
          "本来就短的小页面按正文回，不被壳页兜底吞掉")

    # 6) 404/410：地址不存在是句结论，不是「[404] + 页面导航正文」
    #    真机 run f5031cbe1163（2026-10-17）：不存在的维基条目被同一 URL 抓了 150+ 枪，
    #    回的就是「[404] 跳转到内部…主菜单…」——模型当成「有内容没读到」，一路打到熔断。
    async def run_err(tool: FetchTool, url: str) -> ToolError | None:
        try:
            await tool.execute(url)
        except ToolError as e:
            return e
        return None

    wiki_404 = ('<html><body><h1>维基百科目前还没有这个条目</h1>'
                '<p>跳转到内部链接…主菜单…</p></body></html>').encode()
    t = tool_with(_Fetched(404, "text/html; charset=utf-8", wiki_404, False))
    err = asyncio.run(run_err(t, "https://zh.wikipedia.org/wiki/博彼亞"))
    check(err is not None and not err.counts_as_failure,
          "404 按「输入错了」定性（counts_as_failure=False）——熔断留给「工具真坏了」")
    check(err is not None and "地址不存在" in str(err) and "重试" in str(err)
          and "永远不会成功" in str(err),
          "404 明说「同一个 URL 重试永远不会成功」")
    check(err is not None and "跳转到内部" not in str(err) and "[404]" not in str(err),
          "404 页的导航正文不再被当内容回喂")

    t = tool_with(_Fetched(410, "text/plain; charset=utf-8", b"gone", False))
    err = asyncio.run(run_err(t, "https://example.com/gone"))
    check(err is not None and not err.counts_as_failure and "地址不存在" in str(err),
          "410 同 404：地址没了，别重试")

    check(_scrub_control("a\x00b") == "ab" and _scrub_control("\t\n\r") == "\t\n\r",
          "_scrub_control 只剔其余控制字符，制表与换行留着")
    check("title: T" in _meta_text("<head><title>T</title></head>"), "_meta_text 取到 title")


if __name__ == "__main__":
    print("default_transport：Content-Encoding 与错误页")
    test_transport()
    print("字符集声明：GBK/GB18030 页不再按 UTF-8 硬解")
    test_charset()
    print("_decode_body 边界")
    test_decode_helpers()
    print("FetchTool 出口的分类（含 404/403 的区分）")
    test_fetch_tool()
    print(f"{_checks - _fails}/{_checks} 通过")
    sys.exit(1 if _fails else 0)
