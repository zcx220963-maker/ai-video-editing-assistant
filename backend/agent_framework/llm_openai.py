"""OpenAI 兼容 LLM 客户端适配器（可直连 DeepSeek 等）。

DeepSeek 提供 OpenAI 兼容接口：base_url=https://api.deepseek.com，
model=deepseek-chat。任何 OpenAI 兼容网关（Moonshot / Qwen 兼容模式 / vLLM /
Ollama 等）改三个参数即可复用本适配器。

设计：内部持有的 ``client`` 只要实现 ``chat.completions.create(...)`` 异步方法即可，
因此可注入假客户端做无网络单测。

密钥解析在**每次请求前**发生（见 `_client_for_request`），优先级：
前端「设置」写入的 app_secrets → 环境变量（按 `OPENAI_API_KEY` → `DEEPSEEK_API_KEY` →
`SILICONFLOW_API_KEY` 的顺序取第一把非空的）→ 下面这个回落常量。
所以页面上改一次 key 就立刻生效，不用重启，也不用两个进程各配一遍。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from . import secrets as runtime_secrets
from .llm import LLMResponse, StreamChunk
from .messages import Message, ToolCall

logger = logging.getLogger(__name__)

# 思考模式：deepseek-flash 是思考型模型，默认会先写一大段 reasoning_content。
# 实测（2026-09-21，真端点）关掉之后：带图请求中位 1515ms→770ms、输出 197→42 token，
# tool_calls 照常发出，文案质量没退化；而 `enable_thinking=false` /
# `reasoning={"effort":"none"}` 这两种写法服务端默默收下但**照样思考**（陷阱）。
# 这是 JSON body 里的参数：裸 HTTP 侧直接并入 payload，openai SDK 侧必须经 extra_body
# 传（SDK 的 create() 没有 **kwargs，写 `thinking=` 会 TypeError）。
THINKING_OFF = {"thinking": {"type": "disabled"}}
DEFAULT_THINKING = False          # ← 默认关：这个项目的时间花在成百次小请求上
THINKING_ENV = "OPENAI_THINKING"  # 设成 on/true/1 就把思考打开（长推理任务）

# DeepSeek 默认值；可被环境变量 / 构造参数覆盖。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 库里还没配 key 时用来通过 SDK 构造期校验的占位串：它永远不会出网，
# 因为真正发请求前一定先解析出可用 key（解析不到就直接抛，不带病出网）。
PLACEHOLDER_API_KEY = "pending-frontend-settings"

# ==================== 回落位（前端「设置」与环境变量都没配时才用到） ====================
# 构造期优先级：显式入参 > 下面三项 > 环境变量(OPENAI_*) > 默认值。留空 = 跳过该项。
# 日常使用请直接在页面「设置」里填：写进 PG 后两个服务同时热读，无需重启。
# ⚠️ 密钥不要硬编码在此：前端「设置」或环境变量 OPENAI_API_KEY 才是配置入口。
MY_API_KEY = ""                    # ← 留空；要回落配置请设环境变量 OPENAI_API_KEY
MY_MODEL = "deepseek-flash"        # ← 例如 "deepseek-chat" 或 "deepseek-v4-flash"；留空用默认
MY_BASE_URL = ""     # ← 换服务商时才填；留空用 https://api.deepseek.com
# DeepSeek-V4.1-Flash 输出上限 384K tokens；26 镜分镜（每镜含 TSX 代码）约需 25K~40K。
DEFAULT_MAX_TOKENS = 131072
# ==============================================================================


def _is_retryable(err: Exception) -> bool:
    """4xx 客户端错误（认证/余额/参数）重试无意义；429 限流与 5xx/网络超时可重试。"""
    code = getattr(err, "status_code", None)
    if code is None:
        return True            # 网络异常 / 超时：可重试
    if code == 429:
        return True            # 限流：退避后可能恢复
    return code >= 500         # 5xx 服务端错误可重试；4xx 一律不重试


def _parse_tool_args(raw: str) -> dict[str, Any]:
    """工具调用参数串 → dict，并对**流式拼接产生的尾部垃圾**做保守修复。

    为什么不能只 ``json.loads`` 了事：流式下参数是逐块拼出来的，至少出过三种坏法：

    1. **多吐收尾括号**：``{"plans": [...]]}}`` —— ``}`` 比 ``{`` 多，剥掉末尾 1~3 字符即可。
    2. **max_tokens 截断**：12 镜分镜的参数串很长，模型输出被 max_tokens 砍断，
       JSON 没闭合（``}`` 比 ``{`` 少）。之前只剥多闭合、不补少闭合，
       于是整份参数变成 ``{"_raw": …}``，工具层看到 ``spec=None`` 直接报错——
       用户连着 4 轮拿不到分镜卡。补全只追加**结构上缺的闭合符**，
       不捏造内容：截断丢的是尾部数据，补上括号后解析出的部分仍可用。
    3. **双重编码**：某些模型把 arguments 整体当一个字符串值再 JSON 编码一次，
       ``json.loads`` 成功但返回 ``str`` 而非 ``dict``。再 loads 一次即可。

    修不好就退回 ``{"_raw": …}``，由工具层如实报错。
    """
    text = raw or ""
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
        if isinstance(doc, dict):
            return doc
        if isinstance(doc, str) and doc.strip():
            try:
                inner = json.loads(doc)
                if isinstance(inner, dict):
                    logger.warning("工具参数双重编码，已解一层")
                    return inner
            except json.JSONDecodeError:
                pass
        return {"_raw": text}
    except json.JSONDecodeError:
        pass
    # 多闭合：剥末尾 1~3 字符
    if text.count("}") > text.count("{") or text.count("]") > text.count("["):
        for cut in (1, 2, 3):
            if cut >= len(text):
                break
            try:
                doc = json.loads(text[:-cut])
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict):
                logger.warning("工具参数流式拼接多了 %d 个收尾字符，已保守修复", cut)
                return doc
    # 少闭合：max_tokens 截断——用括号栈+字符串状态跟踪正确补全
    repaired = _repair_truncated_json(text)
    if repaired is not None:
        try:
            doc = json.loads(repaired)
            if isinstance(doc, dict):
                logger.warning("工具参数被 max_tokens 截断，已按括号栈补全闭合符")
                return doc
        except json.JSONDecodeError:
            pass
    return {"_raw": text}


def _repair_truncated_json(text: str) -> str | None:
    """对被 max_tokens 截断的 JSON 串，按实际括号栈和字符串状态补全闭合符。

    只做结构补全，不捏造内容：截断丢的尾部数据不会无中生有。
    返回补全后的字符串；如果文本看起来不像 JSON 开头则返回 None。
    """
    stripped = text.strip()
    if not stripped.startswith("{") and not stripped.startswith("["):
        return None
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == ("{" if ch == "}" else "["):
                stack.pop()
    closers = "".join("}" if o == "{" else "]" for o in reversed(stack))
    base = text + ('"' if in_string else "")
    rstrip = base.rstrip()
    candidates = [
        rstrip + closers,
        rstrip.rstrip(",").rstrip() + closers,
        rstrip + "null" + closers if rstrip.endswith(":") else None,
    ]
    for c in candidates:
        if not c:
            continue
        try:
            json.loads(c)
            return c
        except json.JSONDecodeError:
            continue
    return rstrip + closers


def _describe_error(err: Exception) -> str:
    """把异常转成对用户可读的中文原因，区分认证失败/余额不足/超时等。"""
    code = getattr(err, "status_code", None)
    msg = str(err)
    if code == 401:
        return f"模型密钥无效（401），请在页面「设置」里更新 API Key。原始错误：{msg}"
    if code == 402:
        return f"模型账户余额不足（402），请充值或更换有余额的 API Key。原始错误：{msg}"
    if code == 403:
        return f"模型访问被拒（403），请检查 API Key 权限。原始错误：{msg}"
    if code == 429:
        return f"模型请求过于频繁（429），请稍后重试。原始错误：{msg}"
    if code == 400:
        return f"模型请求参数错误（400）。原始错误：{msg}"
    if code is not None:
        return f"模型返回错误（{code}）。原始错误：{msg}"
    return f"模型调用失败（可能是网络超时）。原始错误：{msg}"


def _resolve_thinking(explicit: bool | None) -> bool:
    """思考模式开关：显式入参 > 环境变量 OPENAI_THINKING > 模块默认。"""
    if explicit is not None:
        return explicit
    raw = os.getenv(THINKING_ENV, "").strip().lower()
    if raw in ("on", "true", "1", "yes"):
        return True
    if raw in ("off", "false", "0", "no"):
        return False
    return DEFAULT_THINKING


class OpenAICompatClient:
    """把归一化的 messages + tools 转成 Chat Completions 调用。"""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        client: Any | None = None,
        thinking: bool | None = None,
    ) -> None:
        self.model = model or MY_MODEL or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
        self.api_key = api_key or MY_API_KEY or runtime_secrets.env_api_key()[0] or ""
        self.base_url = base_url or MY_BASE_URL or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        self.temperature = temperature
        self.max_tokens = max_tokens or DEFAULT_MAX_TOKENS
        self.thinking = _resolve_thinking(thinking)
        self._injected = client is not None
        self._client = client or self._build_real_client()

    def _build_real_client(self):
        from openai import AsyncOpenAI  # 延迟导入，避免无网络/未安装时导入失败

        # 构造期不拦空 key：用户可能还没在页面上填。真正出网前一定先解析（见下）。
        return AsyncOpenAI(api_key=self.api_key or PLACEHOLDER_API_KEY,
                           base_url=self.base_url)

    async def _resolve_request(self) -> tuple[Any, str]:
        """本次请求真正用的 (client, model)：前端改过 key/模型/地址就换，不改则复用。

        注入的假客户端（离线单测）原样返回——测试验的是调用形态，不是密钥。
        with_options 只在确有差异时才传参（openai SDK 的 None 会当覆盖值用）。
        """
        if self._injected:
            return self._client, self.model
        key, source = await runtime_secrets.resolve_api_key(fallback=self.api_key)
        if not key:
            raise RuntimeError(
                "未配置模型密钥：在页面「设置」里填 API Key（或设环境变量 "
                f"{' / '.join(runtime_secrets.ENV_KEY_NAMES)} 之一，按此优先级）。"
                f"已尝试的来源：{source}。")
        model, base = await runtime_secrets.resolve_model_base(
            fallback_model=self.model, fallback_base=self.base_url)
        if key == self.api_key and model == self.model and base == self.base_url:
            return self._client, self.model
        opts: dict[str, Any] = {}
        if key != self.api_key:
            opts["api_key"] = key
        if base != self.base_url and base:
            opts["base_url"] = base
        client = self._client.with_options(**opts) if opts else self._client
        return client, (model or self.model)

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        client, model = await self._resolve_request()
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp = await client.chat.completions.create(
                    **{**self._kwargs(messages, tools), "model": model}, timeout=60
                )
                return self._parse(resp)
            except Exception as e:
                last_err = e
                if not _is_retryable(e):
                    raise RuntimeError(_describe_error(e)) from e
                if attempt < 1:
                    await asyncio.sleep(2)
        raise RuntimeError(
            f"LLM 调用 2 次重试均失败：{_describe_error(last_err)}") from last_err

    def _kwargs(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if not self.thinking:
            # 走 extra_body：SDK 的 create() 没有 **kwargs，直接把 `thinking=` 传进去会
            # TypeError；extra_body 才是官方给「非标准参数」留的口子（会并进 JSON body）。
            kwargs["extra_body"] = dict(THINKING_OFF)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    async def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ):
        """流式：逐块下发文本 delta；tool_calls 在流里是分片到达的，按 index 聚合到末块。"""
        client, model = await self._resolve_request()
        stream = None
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                stream = await client.chat.completions.create(
                    **{**self._kwargs(messages, tools, stream=True), "model": model},
                    timeout=60
                )
                break
            except Exception as e:
                last_err = e
                if not _is_retryable(e):
                    raise RuntimeError(_describe_error(e)) from e
                if attempt < 1:
                    await asyncio.sleep(2)
        if stream is None:
            raise RuntimeError(
                f"LLM 流式调用 2 次重试均失败：{_describe_error(last_err)}") from last_err
        acc: dict[int, dict[str, str]] = {}  # index -> {id, name, args}
        usage: dict[str, int] | None = None
        async for event in stream:
            raw_usage = getattr(event, "usage", None)
            if raw_usage is not None:
                usage = {
                    "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(raw_usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(raw_usage, "total_tokens", 0) or 0,
                }
            choices = getattr(event, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield StreamChunk(delta=piece)
            for tc in getattr(delta, "tool_calls", None) or []:
                slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    slot["name"] += getattr(fn, "name", "") or ""
                    slot["args"] += getattr(fn, "arguments", "") or ""

        tool_calls: list[ToolCall] = []
        for idx in sorted(acc):
            s = acc[idx]
            tool_calls.append(
                ToolCall(id=s["id"] or f"call_{idx}", name=s["name"],
                         arguments=_parse_tool_args(s["args"]))
            )
        yield StreamChunk(tool_calls=tool_calls, usage=usage)

    @staticmethod
    def _parse(resp: Any) -> LLMResponse:
        choice = resp.choices[0].message
        content = choice.content
        raw_calls = getattr(choice, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for call in raw_calls:
            fn = call.function
            tool_calls.append(ToolCall(id=call.id, name=fn.name,
                                       arguments=_parse_tool_args(fn.arguments)))
        usage = None
        raw_usage = getattr(resp, "usage", None)
        if raw_usage is not None:
            usage = {
                "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(raw_usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(raw_usage, "total_tokens", 0) or 0,
            }
        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)


_default_llm: OpenAICompatClient | None = None


def get_default_llm(**overrides: Any) -> OpenAICompatClient:
    """全项目共享的 LLM 客户端：模型/地址在这里按「构造参数 → 环境变量 → 默认值」解析一次。

    所有入口（联网演示、剪辑链路等）都应从这里取 client，确保共用同一个 API Key。
    缓存的只是连接与默认参数；**key 不在这里定死**——每次请求前按当前身份回源
    （前端「设置」→ 环境变量 → 回落位），所以首次调用之后才配的 key 一样生效。
    """
    global _default_llm
    if _default_llm is None:
        _default_llm = OpenAICompatClient(**overrides)
    return _default_llm
