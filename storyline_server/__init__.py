"""Storyline MCP Server 包：真实视频剪辑节点服务（方案 A：ffmpeg + MoviePy + faster-whisper + edge-tts + VL）。

- settings: TOML → 强类型配置
- mediaops: ffmpeg/ffprobe 原子操作（阻塞）
- providers: 视觉理解 / ASR / 文案 LLM / TTS 外部能力（可注入 fake 全离线测试）
- nodes:    与主 Agent mock 契约逐字对齐的 19 个真实节点
- server:   FastMCP 装配 + 服务端 Interceptor（复用 agent_framework.orchestration）
"""

from .settings import Capabilities, ServerSettings, Settings, StorageSettings
from .providers import ProviderError, Providers, build_providers
from .server import DEFAULT_SESSION, SESSION_HEADER, StorylineServer, make_server

__all__ = [
    "Settings", "ServerSettings", "StorageSettings", "Capabilities",
    "Providers", "ProviderError", "build_providers",
    "StorylineServer", "make_server", "SESSION_HEADER", "DEFAULT_SESSION",
]
