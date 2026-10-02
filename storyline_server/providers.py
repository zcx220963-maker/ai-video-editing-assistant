"""外部能力 Provider：视觉理解 / ASR / 文案 LLM / TTS。

设计原则（项目记忆约束③）：edge-tts 与视觉理解是网络服务，本地 faster-whisper 是
重资源——四类能力统一收口在这里，节点侧只看到同步/异步函数与 ``ProviderError``；
每个调用点都有降级路径，保证离线也能整链出片。
key 的来源按「前端『设置』（PG app_secrets）→ 环境变量名（Settings 里只存变量名）→
主 LLM 回落位」解析，本模块与配置文件里都不出现密钥值。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_framework.llm_openai import THINKING_OFF

from .settings import Capabilities

USER_AGENT = "storyline-mcp/1.0"

# faster-whisper 对静音/音乐段的经典幻觉句（中文场景实测形态）：不丢弃会带歪粗剪
_ASR_HALLUCINATION_RE = re.compile(
    r"^(谢谢观看|谢谢收看|谢谢大家|字幕由|字幕制作|字幕|请不吝点赞|订阅|关注|"
    r"三连|下期再见|by\s?e|subtitle|amara\.org)", re.I)


def _asr_text_is_garbage(text: str) -> bool:
    """乱码段判定：有效字符（中日韩/字母/数字）占比过低，多为解码噪声或幻觉。"""
    useful = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff"
                 or ch.isascii() and ch.isalnum())
    return bool(text) and useful / len(text) < 0.4


def _filter_asr_hallucinations(
        segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤会污染时间轴的幻觉段：黑名单句 / 乱码段 / 连续重复 ≥4 次的复读段。"""
    out: list[dict[str, Any]] = []
    last_text: str | None = None
    repeat = 0
    for s in segments:
        t = (s.get("text") or "").strip()
        if not t or _ASR_HALLUCINATION_RE.match(t) or _asr_text_is_garbage(t):
            continue
        if t == last_text:
            repeat += 1
            if repeat >= 3:   # 同一句连续第 4 次起视为幻觉复读，保留前 3 次
                continue
        else:
            last_text, repeat = t, 0
        out.append({**s, "text": t})
    return out


class ProviderError(RuntimeError):
    """外部能力不可用（无网络 / 无 key / 模型缺失），调用方应走降级路径。"""


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: int = 120) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class Providers:
    """四类能力的可注入容器。测试里替换成 fake 即可全离线跑通 DAG。"""

    caps: Capabilities = field(default_factory=Capabilities)

    # ---- 视觉理解（阻塞，节点里 to_thread）----
    def vision(self, images: list[Path], prompt: str, *,
               api_key: str | None = None, key_source: str = "") -> str:
        """多图一次请求的描述。``api_key`` 由调用方解析后传入（前端配置优先）。

        不传则退回进程内的同步解析链，直调脚本与离线测试仍然跑得通。
        """
        frames: list[str] = []
        for img in images:
            b = Path(img).read_bytes()
            frames.append("data:image/jpeg;base64," + base64.b64encode(b).decode("ascii"))
        if api_key is None:
            key, source = self._resolve_vl_key()
        else:
            key, source = api_key.strip(), (key_source or "调用方传入")
        if not key:
            raise ProviderError(f"未配置模型密钥（来源 {source}）：视觉理解走降级路径")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in frames]
        payload: dict[str, Any] = {
            "model": self.caps.vl_model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self.caps.vl_max_tokens,
            "temperature": 0.3,
        }
        if not self.caps.vl_thinking:
            payload.update(THINKING_OFF)
        try:
            data = _post_json(
                f"{self.caps.vl_base}/chat/completions",
                payload,
                {"Authorization": f"Bearer {key}"}, timeout=90)
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            if not text:
                # 思考型模型会把预算花在 reasoning_content 上，content 空是真实故障形态
                spent = len((msg.get("reasoning_content") or "").strip())
                why = ("思考开着" if self.caps.vl_thinking else
                       f"思考已关（thinking=disabled）却仍回了 {spent} 字符思考内容")
                raise ProviderError(
                    f"VL 返回空描述（{self.caps.vl_model}，{why}，"
                    f"预算 {self.caps.vl_max_tokens} tokens）：视觉理解走降级路径")
            return text
        except ProviderError:
            raise
        except Exception as e:  # 网络/响应结构异常统一归为 ProviderError
            raise ProviderError(f"VL 调用失败（key 来源 {source}）: {e}") from e

    def _fallback_key(self) -> tuple[str, str]:
        """进程内可见的 key：`caps.vl_key_env` → 主 LLM 回落位。取不到返回空串，不抛。"""
        key = os.environ.get(self.caps.vl_key_env, "").strip()
        if key:
            return key, self.caps.vl_key_env
        try:
            from agent_framework.llm_openai import get_default_llm

            key = (get_default_llm().api_key or "").strip()
        except Exception:  # noqa: BLE001 - 主 LLM 通道没配好时这里只是回落位，交给上层判定
            return "", ""
        return (key, "主 LLM 通道") if key else ("", "")

    async def resolve_key(self, user_id: str = "") -> tuple[str, str]:
        """节点在 to_thread **之前** await 这个：前端「设置」→ 环境变量 → 主 LLM 回落位。

        app_secrets 只有异步句柄能读，而 vision() 是阻塞函数（跑在 to_thread 里），
        所以解析放在异步侧、把结果显式传进去——不为读一行库再引入同步 PG 驱动。
        """
        from agent_framework.secrets import resolve_api_key

        fallback, _ = self._fallback_key()
        return await resolve_api_key(user_id, fallback=fallback)

    def _resolve_vl_key(self) -> tuple[str, str]:
        """同步链的 key 解析（无 PG 层）：拿不到就抛，调用方走降级路径。

        一个 key 用到底——DeepSeek 的 `deepseek-flash` 本身就是多模态的，没必要
        再让使用者去第二个服务商注册。错误文本里只出现**来源名**，绝不出现值。
        """
        key, source = self._fallback_key()
        if key:
            return key, source
        raise ProviderError(
            f"未设置 {self.caps.vl_key_env}，主 LLM 也没有可用 key，前端「设置」未配置："
            "视觉理解走降级路径")

    # ---- ASR（阻塞，首次加载模型较慢）----
    def transcribe(self, wav: Path) -> list[dict[str, Any]]:
        """→ [{"start","end","text"}…]；无音频/无模型时抛 ProviderError。"""
        try:
            from faster_whisper import WhisperModel  # noqa: F401  只验依赖可用性
        except ImportError as e:
            raise ProviderError(f"faster-whisper 未安装: {e}") from e
        caps = self.caps
        attempts = _asr_attempts(caps)
        last: Exception | None = None
        for device in attempts:
            try:
                model = _get_whisper(caps, device)
                segments, _info = model.transcribe(
                    str(wav), language=None, vad_filter=True,
                    vad_parameters={"min_silence_duration_ms":
                                    int(caps.max_pause_sec * 1000)})
                out = []
                for s in segments:
                    text = (s.text or "").strip()
                    if text:
                        out.append({"start": round(float(s.start), 3),
                                    "end": round(float(s.end), 3), "text": text})
                out = _filter_asr_hallucinations(out)
                _ASR_DEVICE[0] = device      # 记住可用设备，后续调用不再重复踩坑
                return out
            except Exception as e:
                # CUDA 缺库（cublas64_12.dll）要到真正转写时才暴露，构造期看不出来
                last = e
        raise ProviderError(
            f"ASR 失败（依次试过 {attempts}）: {last}") from last

    # ---- 文案 LLM（异步，复用主框架 DeepSeek 通道）----
    async def llm(self, messages: list[dict[str, str]]) -> str:
        try:
            from agent_framework.llm_openai import get_default_llm
            resp = await get_default_llm().complete(messages=messages, tools=None)
            return (resp.content or "").strip()
        except Exception as e:
            raise ProviderError(f"文案 LLM 失败: {e}") from e

    # ---- TTS（异步，edge-tts 免费服务）----
    async def tts(self, text: str, dst: Path) -> Path:
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            import edge_tts
            await edge_tts.Communicate(text, self.caps.tts_voice,
                                       rate=self.caps.tts_rate).save(str(dst))
            if not dst.exists() or dst.stat().st_size < 512:
                raise RuntimeError("edge-tts 产物为空")
            return dst
        except Exception as e:
            raise ProviderError(f"TTS 失败: {e}") from e


# WhisperModel 加载昂贵（GPU 显存 + 权重），进程级缓存；_ASR_DEVICE 记住探明的可用设备。
_WHISPER_CACHE: dict[tuple[str, str, str], Any] = {}
_ASR_DEVICE: list[str | None] = [None]


def _asr_attempts(caps: Capabilities) -> list[str]:
    """auto 的尝试顺序：上次成功的设备优先，其次 cuda→cpu；显式配置就只用那一台。"""
    if caps.asr_device != "auto":
        return [caps.asr_device]
    order = ["cuda", "cpu"]
    if _ASR_DEVICE[0] in order:
        order.remove(_ASR_DEVICE[0])
        order.insert(0, _ASR_DEVICE[0])
    return order


def _get_whisper(caps: Capabilities, device: str) -> Any:
    from faster_whisper import WhisperModel
    compute = caps.asr_compute
    ckey = (caps.asr_model, device, compute)
    if ckey in _WHISPER_CACHE:
        return _WHISPER_CACHE[ckey]
    if device == "cuda":
        model = WhisperModel(caps.asr_model, device="cuda",
                             compute_type=compute if compute != "auto" else "float16")
    elif device == "cpu" and caps.asr_device == "auto":
        model = WhisperModel(caps.asr_model, device="cpu",
                             compute_type="int8")  # auto 的回落档：CPU + int8
    else:
        model = WhisperModel(caps.asr_model, device=device,
                             compute_type=None if compute == "auto" else compute)
    _WHISPER_CACHE[ckey] = model
    return model


def build_providers(caps: Capabilities, *,
                    vision: Callable | None = None,
                    transcribe: Callable | None = None,
                    llm: Callable | None = None,
                    tts: Callable | None = None) -> Providers:
    """装配 Providers；传入 callable 可覆盖任一能力（测试注入 fake 用）。"""
    prov = Providers(caps=caps)
    if vision is not None:
        prov.vision = vision  # type: ignore[method-assign]
    if transcribe is not None:
        prov.transcribe = transcribe  # type: ignore[method-assign]
    if llm is not None:
        prov.llm = llm  # type: ignore[method-assign]
    if tts is not None:
        prov.tts = tts  # type: ignore[method-assign]
    return prov
