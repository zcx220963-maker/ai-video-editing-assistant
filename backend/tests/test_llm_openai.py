"""OpenAI 兼容适配器 + 真实 Search→Fetch ReAct 链路单测（不联网）。

用假的 openai 风格异步客户端按脚本返回响应，驱动 AgentOnceRun 依次：
  LLM→调用 web_search → LLM→调用 fetch_url → LLM→给出最终答复。
验证点：
  - 发给 LLM 的 assistant.tool_calls.arguments 是 JSON 字符串（OpenAI 规范）。
  - 适配器把 SDK 的 tool_calls 正确解析回 ToolCall(arguments=dict)。
  - 工具结果以 role=tool 消息回填给下一轮。
  - 思考模式默认关掉，且开关（构造参数 / 环境变量）两条路都能把它打开。
  - 工具参数串的四种坏法（多吐收尾符 / max_tokens 截断 / 双重编码 / 中间提前闭合）
    各自怎么被保守修复，修不好时工具层说的是「整串不是合法 JSON」而不是「缺必填」。

运行：  python tests/test_llm_openai.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import inspect
import json
import os
import sys
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentOnceRun, AgentConfig
from agent_framework.context import ContextBuilder
from agent_framework.llm_openai import OpenAICompatClient
from agent_framework.session import Session
from agent_framework.tool import Tool, ToolRegistry
from agent_framework.tools.web import FetchTool, SearchTool, SearchResult, _Fetched

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeCompletions:
    """模拟 openai 的 chat.completions.create，按脚本返回并记录每轮入参。"""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[list[dict]] = []
        self.raw: list[dict] = []  # 完整 kwargs：验证思考开关这类「参数有没有发出去」的断言要用

    async def create(self, **kwargs):
        self.calls.append(kwargs.get("messages", []))
        self.raw.append(kwargs)
        if kwargs.get("stream"):
            return _FakeStream([_delta_event("流式片段")])
        step = self._scripted.pop(0)
        if step["kind"] == "tool":
            msg = SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        id=f"call_{len(self.calls)}",
                        type="function",
                        function=SimpleNamespace(
                            name=step["name"],
                            arguments=json.dumps(step["args"], ensure_ascii=False),
                        ),
                    )
                ],
            )
        else:
            msg = SimpleNamespace(content=step["content"], tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


class _FakeStream:
    """假 async stream：complete_stream 用 `async for event in stream` 消费。"""

    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


def _delta_event(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


def fake_provider(query, max_results):
    return [SearchResult(title="DeepSeek 官网", url="https://api.deepseek.com/home", snippet="介绍页")]


def fake_transport(url, max_bytes, timeout):
    body = b"<html><body><h1>DeepSeek</h1><p>Flash model docs.</p></body></html>"
    return _Fetched(200, "text/html", body, False)


async def main() -> None:
    scripted = [
        {"kind": "tool", "name": "web_search", "args": {"query": "deepseek flash"}},
        {"kind": "tool", "name": "fetch_url", "args": {"url": "https://api.deepseek.com/home"}},
        {"kind": "answer", "content": "已找到：DeepSeek Flash 文档在 api.deepseek.com。"},
    ]

    fc = FakeCompletions(scripted)
    client = SimpleNamespace(chat=SimpleNamespace(completions=fc))
    llm = OpenAICompatClient(model="deepseek-chat", client=client)

    reg = ToolRegistry()
    reg.register(SearchTool(provider=fake_provider))
    reg.register(FetchTool(transport=fake_transport))

    agent = AgentOnceRun(
        llm=llm,
        registry=reg,
        context_builder=ContextBuilder(),
        config=AgentConfig(max_iterations=5),
    )
    session = Session(user_id="u", conversation_id="c")
    result = await agent.run(session, "帮我查一下 DeepSeek flash 模型的文档在哪")

    check("api.deepseek.com" in result, f"最终答复来自链路: {result}")
    check(len(fc.calls) == 3, f"共 3 轮 LLM 调用: {len(fc.calls)}")

    # 第 2 轮请求里应含 assistant(tool_calls)，且 arguments 为 JSON 字符串
    second = fc.calls[1]
    asst = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
    check(bool(asst), "第2轮含 assistant(tool_calls) 消息")
    if asst:
        arg = asst[0]["tool_calls"][0]["function"]["arguments"]
        check(
            isinstance(arg, str) and json.loads(arg) == {"query": "deepseek flash"},
            "arguments 为 JSON 字符串且可回解",
        )
    check(any(m.get("role") == "tool" for m in second), "第2轮含 tool 结果消息")

    # 第 3 轮请求里应含 fetch_url 抓取结果
    third = fc.calls[2]
    check(
        any("Flash model docs" in str(m.get("content")) for m in third if m.get("role") == "tool"),
        "第3轮含 fetch_url 抓取结果",
    )

    await case_thinking_switch()
    case_tool_args_salvage()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


async def case_thinking_switch() -> None:
    """思考模式开关：默认关（快约 2 倍），且开关必须真能打开。

    断言卡在 extra_body 而不是顶层 kwargs——openai SDK 的 create() 没有 **kwargs，
    顶层写 `thinking=` 会 TypeError，只有 extra_body 会并进 JSON body。
    """
    saved_env = os.environ.pop("OPENAI_THINKING", None)

    def client_with(**kw):
        fc = FakeCompletions([{"kind": "answer", "content": "好"}])
        c = OpenAICompatClient(model="deepseek-chat",
                               client=SimpleNamespace(chat=SimpleNamespace(completions=fc)),
                               **kw)
        return c, fc

    # ① 默认：complete() 与 complete_stream() 两条路都带上关思考的参数
    c, fc = client_with()
    check(c.thinking is False, "默认不开思考")
    await c.complete([{"role": "user", "content": "hi"}])
    check(fc.raw[0].get("extra_body") == {"thinking": {"type": "disabled"}},
          f"complete() 经 extra_body 发出 thinking=disabled（实得 {fc.raw[0].get('extra_body')}）")

    async for _ in c.complete_stream([{"role": "user", "content": "hi"}]):
        pass
    check(fc.raw[1].get("extra_body") == {"thinking": {"type": "disabled"}},
          "complete_stream() 同样带 thinking=disabled")

    # ② 显式打开：参数整个消失，回到服务端默认行为
    c2, fc2 = client_with(thinking=True)
    await c2.complete([{"role": "user", "content": "hi"}])
    check("extra_body" not in fc2.raw[0], "thinking=True 时不发这个参数")

    # ③ 环境变量：不改一行代码就能按部署切
    try:
        os.environ["OPENAI_THINKING"] = "on"
        c3, fc3 = client_with()
        await c3.complete([{"role": "user", "content": "hi"}])
        check("extra_body" not in fc3.raw[0], "OPENAI_THINKING=on 打开思考")

        os.environ["OPENAI_THINKING"] = "off"
        c4, fc4 = client_with()
        check(fc4.raw == [], "构造期不发请求")
        await c4.complete([{"role": "user", "content": "hi"}])
        check(fc4.raw[0].get("extra_body") == {"thinking": {"type": "disabled"}},
              "OPENAI_THINKING=off 关闭思考")
    finally:
        if saved_env is None:
            os.environ.pop("OPENAI_THINKING", None)
        else:
            os.environ["OPENAI_THINKING"] = saved_env

    # ④ 防漂移：发出去的 kwargs 必须都是真 SDK 收得下的参数名
    try:
        from openai.resources.chat.completions import AsyncCompletions

        allowed = set(inspect.signature(AsyncCompletions.create).parameters)
    except Exception:  # noqa: BLE001 - 没装 SDK 时这条跳过而不是假失败
        allowed = set()
    if allowed:
        extra = [k for k in fc.raw[0] if k not in allowed]
        check(not extra, f"kwargs 全部是 SDK 认识的参数名（多出 {extra}）")

    # ⑤ 重试策略：4xx 客户端错误立即失败（重试无意义），5xx 才重试
    from unittest.mock import AsyncMock, patch

    class _HttpError(Exception):
        def __init__(self, code: int, msg: str) -> None:
            super().__init__(msg)
            self.status_code = code

    class _ErrCompletions:
        def __init__(self, err: Exception) -> None:
            self.err = err
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            raise self.err

    def err_client(code: int, msg: str):
        ec = _ErrCompletions(_HttpError(code, msg))
        return OpenAICompatClient(
            api_key="k",
            client=SimpleNamespace(chat=SimpleNamespace(completions=ec)),
        ), ec

    c402, ec402 = err_client(402, "Insufficient Balance")
    try:
        await c402.complete([{"role": "user", "content": "hi"}])
        check(False, "402 应立即抛错")
    except RuntimeError as e:
        check("余额不足" in str(e), "402 报「余额不足」而非「超时」")
    check(ec402.calls == 1, f"402 不重试（只调 1 次，实得 {ec402.calls}）")

    c401, ec401 = err_client(401, "invalid api key")
    try:
        await c401.complete([{"role": "user", "content": "hi"}])
        check(False, "401 应立即抛错")
    except RuntimeError as e:
        check("密钥无效" in str(e), "401 报「密钥无效」")
    check(ec401.calls == 1, "401 不重试")

    c500, ec500 = err_client(500, "server boom")
    with patch("agent_framework.llm_openai.asyncio.sleep", new=AsyncMock()):
        try:
            await c500.complete([{"role": "user", "content": "hi"}])
            check(False, "500 最终应抛错")
        except RuntimeError:
            pass
    from agent_framework.llm_openai import _MAX_ATTEMPTS
    # `_MAX_ATTEMPTS=3` = 首发 1 次 + 重试 2 次；旧断言写死 2，把「重试次数」当成了总次数。
    check(ec500.calls == _MAX_ATTEMPTS,
          f"500 重试 {_MAX_ATTEMPTS - 1} 次（共 {_MAX_ATTEMPTS} 次，实得 {ec500.calls}）")


class _NoRawDecode:
    """把 ``json.JSONDecoder().raw_decode`` 那一路拔掉，等价于「第四路没写」。

    先证红用的替身：其余行为全部转给真的 json 模块，只让 raw_decode 抛解析错，
    好让调用方走回 ``{"_raw": …}`` 那条老路。
    """

    JSONDecodeError = json.JSONDecodeError

    @staticmethod
    def loads(text):
        return json.loads(text)

    class _Decoder:
        def raw_decode(self, text, idx=0):
            raise json.JSONDecodeError("no such path", text, 0)

    JSONDecoder = _Decoder


class _PlanTool(Tool):
    """submit_plan 的形状：唯一的必填是 plans，用来触发 prepare_call 的缺参分支。"""

    @property
    def name(self) -> str:
        return "submit_plan"

    @property
    def description(self) -> str:
        return "交计划卡"

    @property
    def parameters(self) -> dict:
        return {"type": "object",
                "properties": {"plans": {"type": "array"}},
                "required": ["plans"]}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs) -> str:
        return "ok"


def case_tool_args_salvage() -> None:
    """工具参数的第四路（中间提前闭合）+ ``_raw`` 的如实报错文案。

    为什么单独钉：这条链跨两个文件——``llm_openai._parse_tool_args`` 负责把串
    救回 dict，``tool.prepare_call`` 负责在救不回来时说真话。
    两边当时都坏：解析器三路全落空 → 整包进 ``_raw``；工具层看到 ``_raw`` 里没有
    ``plans`` → 报「missing required params ['plans']」。那句报错指错了方向，
    模型照着它换键名重试，而每次参数字节都不同，同参熔断接不到，
    规划轮 40 迭代烧光（真机 run ``c364585634f2``，2026-10-15）。
    """
    print("\n=== 工具参数第四路：最外层在中间就闭合了，后面还拖着一段 ===")
    from agent_framework.llm_openai import _parse_tool_args

    fixture = (_Path(__file__).resolve().parent / "data" /
               "real_overclose_submit_plan.txt")
    text = fixture.read_text(encoding="utf-8")
    check(len(text) == 5079, f"夹具是真机那份原串的长度：{len(text)}")

    # ① 先证红：拔掉第四路，这份串整包落进 _raw（老修复三路确实接不到它）
    import agent_framework.llm_openai as mod
    saved_json = mod.json
    mod.json = _NoRawDecode
    try:
        blind = _parse_tool_args(text)
    finally:
        mod.json = saved_json
    check(list(blind) == ["_raw"],
          f"① 拔掉第四路：三种老修复全落空，整包进 _raw：{list(blind)}")

    # ② 补上第四路：拿到 plans，且第一个方案是完整可用的
    doc = _parse_tool_args(text)
    check("plans" in doc and "_raw" not in doc,
          f"② 第四路认出头部文档：keys={list(doc)}")
    plans = doc.get("plans") or []
    check(len(plans) == 2,
          f"② 头部文档里带两个 plans 元素（第二个是站错层的第 4 步，见 ③）：{len(plans)}")
    check(plans and plans[0].get("plan_id") == "p1" and len(plans[0].get("steps") or []) == 3,
          f"② p1 完整（3 步）：plan_id={plans[0].get('plan_id') if plans else None}")

    # ③ 如实承认第四路的边界：尾部不是被补全的，是被丢掉的，而且丢的不是同一回事。
    #    真机这份串里，模型把 p1 那个对象提前闭合了（`}]}`），于是**p1 的第 4 步**
    #    落在了 plans 层——它被救回来了，只是站错了位置；被丢掉的 2229 字符是 p2 整体。
    #    这条断言防的是「以后有人把第四路吹成完整恢复」：救回来的仍然是一份结构错位
    #    的计划，只是错法从「整包读不懂」变成「方案 2 缺 plan_id」——后者可改，前者只会让人换键名重试。
    stub = plans[1] if len(plans) > 1 else {}
    check(not stub.get("plan_id") and stub.get("seq") == 4
          and stub.get("node") == "select_BGM",
          f"③ 第二个元素是 p1 的第 4 步（站错了层），不是空壳：{list(stub)}")

    # ④ 工具层的报错必须指对方向
    reg = ToolRegistry()
    reg.register(_PlanTool())
    _, _, err = reg.prepare_call("submit_plan", {"_raw": text})
    check(bool(err) and "不是合法 JSON 对象" in err and "missing required" not in err,
          f"④ 整串坏 → 报「不是合法 JSON 对象」而不是「缺 plans」：{(err or '')[:48]}")
    check(bool(err) and str(len(text)) in err,
          f"④ 报错里带上原文字数（模型据此缩体积）：{(err or '')[:64]}")

    # ④b 同一个 doc 直接进 prepare_call：必填这一关过了。
    #      第四路换来的不是「卡能出」，是「调用能走到参数与结构校验层」——
    #      结构错位（plans[1] 缺 plan_id）会被校验器点名打回，那是可改的方向。
    _, casted, err_ok = reg.prepare_call("submit_plan", doc)
    check(not err_ok and len(casted.get("plans") or []) == 2,
          f"④b 救回的 dict 过了必填关（不再整包报废）：err={err_ok}")

    # ⑤ 负例：真的缺必填仍然报缺必填，不许被这条文案盖掉
    _, _, err2 = reg.prepare_call("submit_plan", {"goal": "只写了目标"})
    check(bool(err2) and "missing required params ['plans']" in err2,
          f"⑤ 真缺参仍按缺参报：{(err2 or '')[:48]}")

    # ⑥ 合成最小形状：{ } 数量是平的、错不在末尾，三路老修复都接不到
    mid = '{"plans": [{"plan_id": "p1"}]},{"plan_id": "p2"}'
    check(list(_parse_tool_args(mid)) == ["plans"],
          f"⑥ 合成串认出头部文档：{list(_parse_tool_args(mid))}")
    tail = '{"plans": [{"plan_id": "p1"'
    check("plans" in _parse_tool_args(tail),
          "⑥ 另一族：max_tokens 截断仍走括号栈补全（第四路没抢它的事）")
    check(_parse_tool_args("这不是 JSON") == {"_raw": "这不是 JSON"},
          "⑥ 彻底不是 JSON 的串仍然如实进 _raw（不硬凑）")


if __name__ == "__main__":
    asyncio.run(main())
