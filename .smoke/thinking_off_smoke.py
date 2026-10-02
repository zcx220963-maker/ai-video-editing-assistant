"""真机冒烟：关掉模型思考之后，两条通道是不是真的更快、且答案仍然可用。

三截：
  A. 本地回显服务器 + **真 openai SDK**：证明 `thinking` 这个非标准参数经
     extra_body 真的并进了出网 JSON body（SDK 的 create() 没有 **kwargs，
     顶层传会 TypeError——离线单测的假客户端是验不出这层差别的）。
  B. 真 DeepSeek 文本：同一条 prompt，生产 client 开/关思考各跑 3 次，比中位耗时。
  C. 真 DeepSeek 视觉：走生产 providers.vision（understand_clips 的调用形状）。

运行：  python -u .smoke/thinking_off_smoke.py
全程不打印任何密钥值（只看长度/后四位）。
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.llm_openai import OpenAICompatClient  # noqa: E402
from storyline_server.providers import build_providers  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

FRAME = Path(".smoke/_vl_frames/mat-c14d61_t60.jpg")
TEXT_PROMPT = "用一句话说明 3 和 5 哪个大，只给结论不要推导。"
VL_PROMPT = "用一句话客观描述这帧画面里有什么，包括人物/景物/字幕文字。"

_checks = 0
_fails = 0


def check(cond: bool, label: str, extra: str = "") -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' → ' + extra) if extra else ''}")
    if not cond:
        _fails += 1


# ---------------------------------------------------------------- A：真 SDK 出网形状
class _Echo(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        _Echo.received.append(json.loads(self.rfile.read(n).decode("utf-8")))
        body = json.dumps({
            "id": "cmpl-echo", "object": "chat.completion", "created": 1,
            "model": "echo", "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": "回声"}}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


async def case_real_sdk_body() -> None:
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        for thinking, want in ((False, True), (True, False)):
            _Echo.received.clear()
            llm = OpenAICompatClient(model="echo-model", api_key="sk-smoke-echo-key",
                                     base_url=base, thinking=thinking)
            resp = await llm.complete([{"role": "user", "content": "在吗"}])
            got = _Echo.received[0] if _Echo.received else {}
            check(resp.content == "回声", f"thinking={thinking} 时真 SDK 请求正常返回")
            has = got.get("thinking") == {"type": "disabled"}
            check(has is want,
                  f"出网 body 里 thinking=disabled 的存在性符合预期（thinking={thinking}）",
                  f"实得 {got.get('thinking')}")
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ B：真 DeepSeek 文本
async def one(llm: OpenAICompatClient) -> tuple[int, str]:
    t0 = time.perf_counter()
    resp = await llm.complete([{"role": "user", "content": TEXT_PROMPT}])
    return int((time.perf_counter() - t0) * 1000), (resp.content or "").strip()


async def case_real_text() -> None:
    off = OpenAICompatClient(thinking=False)
    on = OpenAICompatClient(thinking=True)
    check(bool(off.api_key), "主 LLM 通道拿得到 key（只验有无）",
          f"长度 {len(off.api_key)} 末四位 …{off.api_key[-4:]}")
    # 交替采样，避免冷启动把差异解释成别的东西
    samples: dict[str, list[int]] = {"off": [], "on": []}
    ans: dict[str, str] = {}
    for _ in range(3):
        for which, llm in (("off", off), ("on", on)):
            ms, text = await one(llm)
            samples[which].append(ms)
            ans[which] = text
    mo, mn = statistics.median(samples["off"]), statistics.median(samples["on"])
    print(f"  关思考 中位 {mo:.0f}ms  各次 {samples['off']}  答复「{ans['off'][:40]}」")
    print(f"  开思考 中位 {mn:.0f}ms  各次 {samples['on']}  答复「{ans['on'][:40]}」")
    check(bool(ans["off"]) and bool(ans["on"]), "两种模式都出 content")
    check(mo < mn, f"关思考确实更快（{mo:.0f} < {mn:.0f}ms）")
    check(mo * 1.2 < mn, "快出的不是一点半点（中位差 >20%）",
          f"{mn / max(mo, 1):.1f}×")


# ----------------------------------------------------------------- C：真 DeepSeek 视觉
def case_real_vision() -> None:
    if not FRAME.is_file():
        print("  跳过 C：没有 .smoke/_vl_frames 下的测试帧")
        return
    base = Settings.load(Path("examples/storyline/config.toml")).caps
    on_caps = Settings.load(Path("examples/storyline/config.toml")).caps
    on_caps.vl_thinking = True
    provs = {"off": build_providers(base), "on": build_providers(on_caps)}

    def timed(which: str) -> tuple[int, str]:
        t0 = time.perf_counter()
        text = provs[which].vision([FRAME], VL_PROMPT)
        return int((time.perf_counter() - t0) * 1000), text

    # 交替采样：单次对比会被「谁先跑谁摊上冷启动」带偏（第一版就被 1966 vs 1467 打过脸）
    samples: dict[str, list[int]] = {"off": [], "on": []}
    desc = ""
    for _ in range(3):
        for which in ("off", "on"):
            ms, text = timed(which)
            samples[which].append(ms)
            if which == "off":
                desc = text
    mo, mn = statistics.median(samples["off"]), statistics.median(samples["on"])
    print(f"  关思考 中位 {mo:.0f}ms 各次 {samples['off']}")
    print(f"  开思考 中位 {mn:.0f}ms 各次 {samples['on']}")
    print(f"  描述「{desc[:70]}」")
    check(len(desc.strip()) > 10 and "降级" not in desc, "关思考后仍拿到真描述")
    check(any(k in desc for k in ("人", "讲", "字幕", "台", "麦", "舞", "背景")),
          "描述里确实有画面要素", desc[:40])
    check(mo < mn, f"VL 中位更快（{mo:.0f} < {mn:.0f}ms）")


async def main() -> None:
    await case_real_sdk_body()
    await case_real_text()
    case_real_vision()
    print(f"\n{_checks - _fails}/{_checks} 通过")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
