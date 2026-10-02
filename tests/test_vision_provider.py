"""视觉理解 Provider 的 key 来源与预算离线验证：**一个 key 用到底**。

运行：  python tests/test_vision_provider.py

离线纪律：`providers._post_json` 换成假网关（不起真连接、不碰真密钥），
主 LLM 通道用替身。真机 VL 冒烟另跑，本文件必须无网可过。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import os
import sys
import tempfile
import types
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import storyline_server.providers as P  # noqa: E402
from storyline_server.providers import ProviderError  # noqa: E402
from storyline_server.settings import Capabilities, Settings  # noqa: E402

ENV = "OPENAI_API_KEY"
ENV_KEY = "sk-from-env-ENVVALUE123"
LLM_KEY = "sk-from-mainllm-LLMVALUE456"

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _fails += 1
        print(f"  FAIL {label}")


class FakeGateway:
    """假 /chat/completions：记录请求，返回预设响应。"""

    def __init__(self, reply: dict | None = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, dict, int]] = []
        self.reply = reply or {"choices": [{"message": {"content": "海浪拍打礁石的空镜"}}]}
        self.exc = exc

    def __call__(self, url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
        self.calls.append((url, payload, headers, timeout))
        if self.exc:
            raise self.exc
        return self.reply


def caps(**over) -> Capabilities:
    base = dict(vl_base="https://vl.example/v1", vl_model="vl-test", vl_key_env=ENV,
                vl_max_tokens=1024)
    base.update(over)
    return Capabilities(**base)


def frames(tmp: Path, n: int) -> list[Path]:
    out = []
    for i in range(n):
        f = tmp / f"frame{i}.jpg"
        f.write_bytes(bytes([0xFF, 0xD8, i, 0xD9]))
        out.append(f)
    return out


def run(fn) -> None:
    """每个用例都在干净的环境变量与主 LLM 替身下跑。"""
    saved_env = os.environ.get(ENV)
    import agent_framework.llm_openai as L
    real_get_default = L.get_default_llm
    try:
        os.environ.pop(ENV, None)
        fn()
    finally:
        os.environ.pop(ENV, None)
        if saved_env is not None:
            os.environ[ENV] = saved_env
        L.get_default_llm = real_get_default


def stub_llm(key: str | None, exc: Exception | None = None) -> None:
    import agent_framework.llm_openai as L

    def _fake(*a, **kw):
        if exc:
            raise exc
        return types.SimpleNamespace(api_key=key)

    L.get_default_llm = _fake


# --------------------------------------------------------------------------

def case_env_key_wins(tmp: Path) -> None:
    os.environ[ENV] = ENV_KEY
    gw = FakeGateway()
    P._post_json = gw
    prov = P.Providers(caps=caps())
    text = prov.vision(frames(tmp, 1), "描述这帧")
    check(text == "海浪拍打礁石的空镜", "env 有 key 时正常返回描述")
    url, payload, headers, _t = gw.calls[0]
    check(headers["Authorization"] == f"Bearer {ENV_KEY}", "env 的 key 优先于主 LLM 通道")
    check(url == "https://vl.example/v1/chat/completions", "打到 vl_base")
    check(payload["model"] == "vl-test", "model 取 vl_model")
    check(payload["max_tokens"] == 1024, "max_tokens 取 caps.vl_max_tokens（不再写死 256）")


def case_falls_back_to_main_llm(tmp: Path) -> None:
    stub_llm(LLM_KEY)
    gw = FakeGateway()
    P._post_json = gw
    prov = P.Providers(caps=caps())
    prov.vision(frames(tmp, 1), "描述这帧")
    _u, _p, headers, _t = gw.calls[0]
    check(headers["Authorization"] == f"Bearer {LLM_KEY}",
          "env 未设时回退复用主 LLM 的同一个 key（一个 key 用到底）")


def case_no_key_anywhere(tmp: Path) -> None:
    stub_llm("")
    prov = P.Providers(caps=caps())
    try:
        prov.vision(frames(tmp, 1), "描述这帧")
        check(False, "两处都没 key 应抛 ProviderError")
    except ProviderError as e:
        check(ENV in str(e), "报错点名该设哪个环境变量")
        check("OPENAI" in str(e) or "主 LLM" in str(e), "报错说明也查了主 LLM 通道")


def case_llm_channel_broken(tmp: Path) -> None:
    """主 LLM 通道现在只是回落位：读它炸了也不能把原始异常抛给剪辑链。"""
    stub_llm(None, exc=RuntimeError("未提供 API Key"))
    prov = P.Providers(caps=caps())
    try:
        prov.vision(frames(tmp, 1), "描述这帧")
        check(False, "回落位不可用应转成 ProviderError 走降级")
    except ProviderError as e:
        check("前端" in str(e) and ENV in str(e), "报错交代查过哪几层来源")
        check(LLM_KEY not in str(e) and ENV_KEY not in str(e), "错误文本不带任何密钥值")


def case_empty_content_explains_budget(tmp: Path) -> None:
    os.environ[ENV] = ENV_KEY
    P._post_json = FakeGateway(reply={"choices": [{"message": {
        "content": "", "reasoning_content": "x" * 300}}]})
    prov = P.Providers(caps=caps(vl_model="deepseek-flash", vl_max_tokens=256))
    try:
        prov.vision(frames(tmp, 1), "描述这帧")
        check(False, "content 为空应抛 ProviderError 而不是塞占位")
    except ProviderError as e:
        check("思考内容" in str(e) and "300" in str(e), "空返回时说明预算被思考内容吃掉")
        check("256" in str(e), "报错带上当前预算")


def case_multi_image_payload(tmp: Path) -> None:
    os.environ[ENV] = ENV_KEY
    gw = FakeGateway()
    P._post_json = gw
    prov = P.Providers(caps=caps())
    prov.vision(frames(tmp, 3), "按顺序描述这三帧")
    _u, payload, _h, _t = gw.calls[0]
    content = payload["messages"][0]["content"]
    check(content[0]["type"] == "text", "文本提示在前")
    check([c["type"] for c in content[1:]].count("image_url") == 3, "三帧全部带上（支持批量）")
    check(all(c["image_url"]["url"].startswith("data:image/jpeg;base64,")
              for c in content[1:]), "帧以 data URL 传")


def case_network_error_hides_key(tmp: Path) -> None:
    os.environ[ENV] = ENV_KEY
    P._post_json = FakeGateway(exc=OSError("connection reset"))
    prov = P.Providers(caps=caps())
    try:
        prov.vision(frames(tmp, 1), "描述这帧")
        check(False, "网络异常应转成 ProviderError")
    except ProviderError as e:
        check("connection reset" in str(e), "保留底层错误文本")
        check(ENV_KEY not in str(e), "错误文本不含密钥值")
        check("key 来源" in str(e), "点明这次用的是哪个 key 来源")


def case_caller_supplied_key(tmp: Path) -> None:
    """节点在 to_thread 前 await 解析好再传进来——传了的 key 必须说了算。"""
    os.environ[ENV] = ENV_KEY
    gw = FakeGateway()
    P._post_json = gw
    prov = P.Providers(caps=caps())
    pg_key = "sk-from-pg-appsecrets-PG789"
    prov.vision(frames(tmp, 1), "描述这帧", api_key=pg_key, key_source="前端配置")
    _u, _p, headers, _t = gw.calls[0]
    check(headers["Authorization"] == f"Bearer {pg_key}",
          "调用方传入的 key 覆盖环境变量（前端配置优先）")
    try:
        prov.vision(frames(tmp, 1), "描述这帧", api_key="", key_source="未配置")
        check(False, "传空 key 不该偷偷回落到环境变量")
    except ProviderError as e:
        check("来源 未配置" in str(e), "空 key 直接点名未配置并带上来源名")
        check(pg_key not in str(e) and ENV_KEY not in str(e), "诊断文本不带密钥值")


def case_vl_thinking_off(tmp: Path) -> None:
    """逐镜一次请求的热路径默认关思考：177 段就是 177 次带图往返，省一半时间。"""
    os.environ[ENV] = ENV_KEY
    gw = FakeGateway()
    P._post_json = gw
    prov = P.Providers(caps=caps())
    prov.vision(frames(tmp, 1), "描述这帧")
    _u, payload, _h, _t = gw.calls[0]
    check(payload.get("thinking") == {"type": "disabled"},
          f"默认带 thinking=disabled（实得 {payload.get('thinking')}）")
    check(payload["max_tokens"] == 1024, "关思考后预算照旧")

    gw2 = FakeGateway()
    P._post_json = gw2
    P.Providers(caps=caps(vl_thinking=True)).vision(frames(tmp, 1), "描述这帧")
    check("thinking" not in gw2.calls[0][1], "vl_thinking=true 时这个参数整个不发")


def case_single_key_defaults() -> None:
    c = Capabilities()
    check(c.vl_key_env == ENV, "默认 VL 与主 LLM 共用 OPENAI_API_KEY")
    check(c.vl_model == "deepseek-flash" and c.vl_base == "https://api.deepseek.com",
          "默认 VL 走 DeepSeek 同源端点")
    check(c.vl_max_tokens >= 1024, "默认预算够思考型模型出 content")
    check(c.vl_thinking is False, "默认关思考（逐镜热路径提速）")
    s = Settings.load(Path("examples/storyline/config.toml"))
    check(s.caps.vl_key_env == ENV and s.caps.vl_model == "deepseek-flash",
          "仓库里的 config.toml 与默认口径一致")
    check(s.caps.vl_max_tokens == 1024, "config.toml 显式写了预算")
    check(s.caps.vl_thinking is False, "config.toml 显式写了 vl_thinking=false")
    # key 不再硬编码，而是运行时从环境变量/前端配置解析——这里只验解析链路通
    from agent_framework.llm_openai import get_default_llm, OpenAICompatClient
    import os as _os
    _saved_key = _os.environ.get("OPENAI_API_KEY")
    _os.environ["OPENAI_API_KEY"] = "sk-test-key-for-resolution-check-1234567890"
    try:
        _llm = OpenAICompatClient()
        check(len(_llm.api_key or "") > 20, "环境变量 OPENAI_API_KEY 能被解析到 LLM 客户端")
    finally:
        if _saved_key is not None:
            _os.environ["OPENAI_API_KEY"] = _saved_key
        else:
            _os.environ.pop("OPENAI_API_KEY", None)


def main() -> None:
    old = P._post_json
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for fn in (case_env_key_wins, case_falls_back_to_main_llm, case_no_key_anywhere,
                       case_llm_channel_broken, case_empty_content_explains_budget,
                       case_multi_image_payload, case_network_error_hides_key,
                       case_caller_supplied_key, case_vl_thinking_off):
                run(lambda: fn(tmp))
            run(case_single_key_defaults)
    finally:
        P._post_json = old
    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
