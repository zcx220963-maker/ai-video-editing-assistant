"""Storyline MCP Server 的配置加载：TOML → 强类型 Settings。

对应设计文档 [local_mcp_server] 配置模板：server/host/port/path/available_nodes
等仍在该段；本模块追加 [capabilities]（ASR/VL/TTS/剪辑参数）与 [storage]（后端、
渲染工作区根、内容缓存根与容量/过期线）两段，全部用标准库 tomllib 解析。
原 [media] 的本地目录职责已随存储层迁移退役（spec §8）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .render_jobs import DEFAULT_MAX_CONCURRENT


@dataclass
class ServerSettings:
    server_name: str = "storyline"
    url_scheme: str = "http"
    connect_host: str = "127.0.0.1"
    host: str = "0.0.0.0"
    port: int = 8001
    path: str = "/mcp"
    json_response: bool = False
    stateless_http: bool = False
    timeout: int = 1800  # 渲染类工具超时（秒）——1080p 长视频编码需 >600s
    available_nodes: list[str] = field(default_factory=list)
    available_node_pkgs: list[str] = field(default_factory=list)


@dataclass
class Capabilities:
    # 视觉理解：任何 OpenAI 兼容的**多模态**端点。默认与主 LLM 同源（deepseek-flash
    # 实测能吃 image_url）；vl_key_env 未设置时回退复用主 LLM 的 key —— 一个 key 用到底。
    vl_base: str = "https://api.deepseek.com"
    vl_model: str = "deepseek-flash"
    vl_key_env: str = "OPENAI_API_KEY"
    # 思考型模型的 content 会被 reasoning_content 抢预算，256 会稳定返回空描述
    vl_max_tokens: int = 1024
    # 思考模式默认关：understand_clips 是逐镜一次请求（177 段=177 次带图往返），
    # 实测关掉后中位 1515ms→770ms、输出 197→42 token，描述质量无退化。
    vl_thinking: bool = False
    # understand_clips 批量抽帧：每批 N 帧合成一次 VL 请求 + 批数上限。
    # 177 段逐镜一次=177 次带图往返，600s MCP 超时跑不完；8 帧/批 → 23 次请求。
    vl_frames_per_batch: int = 8
    vl_max_batches: int = 24
    # ASR（faster-whisper 本地 GPU）
    asr_model: str = "small"
    asr_device: str = "auto"   # auto→cuda，失败回落 cpu
    asr_compute: str = "auto"  # float16 / int8 / auto
    # ASR 超时按音频时长伸缩：timeout = base + 音频秒数 × per_audio，与音频无关的
    # 固定 provider_timeout_sec 会把长口播砍断（180s 固定 vs 16 分钟音频）。
    asr_timeout_base_sec: float = 60.0
    asr_timeout_per_audio_sec: float = 1.5
    # 外部能力单次调用上限（文案 LLM/TTS）：超时报错走节点降级路径
    provider_timeout_sec: float = 180.0
    # TTS（edge-tts 免费）
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"
    # 剪辑参数
    scene_threshold: float = 0.3
    min_shot_sec: float = 0.6
    max_shot_sec: float = 12.0
    max_pause_sec: float = 1.0   # 粗剪时超过该时长的静音视为可切
    bgm_volume: float = 0.2
    subtitle_font: str = "Microsoft YaHei"
    # 字幕字体搜索目录（按序尝试）：跨平台部署时由 [capabilities].font_dirs 覆盖
    font_dirs: list[str] = field(default_factory=lambda: [
        "C:/Windows/Fonts", "/usr/share/fonts", "/System/Library/Fonts",
        "/Library/Fonts"])
    transition_sec: float = 0.4
    proxy_max_side: int = 480    # 送 VL 的抽图长边上限
    # 高光精选：speech_rough_cut 在高光模式下按语义打分挑 top-N 金句段，
    # 截到 highlight_default_sec。用户说"高光/语录/金句/励志"等自动启用。
    highlight_default_sec: float = 120.0
    # 渲染改成提交 + 轮询后，render_video 先内联等这么多秒：短片一次调用就拿到成片，
    # 长片则立刻回任务句柄，由调用方轮询 render_status（不再拿一次调用等一部片子）。
    render_grace_sec: float = 20.0
    # 调用方（前端「直接渲染」、curl）可显式要求更长内联等待，上限受这一项约束，
    # 且必须明显小于 [local_mcp_server].timeout —— 否则只是把原来的阻塞换了个数字。
    render_wait_max_sec: float = 300.0
    # 同时在跑的渲染数：ffmpeg/MoviePy 是 CPU 密集型，开太多互相拖慢
    render_max_concurrent: int = DEFAULT_MAX_CONCURRENT
    # 渲染连续多少秒没往 render_jobs 写过任何东西就判停滞收口（0 = 关掉看门狗）。
    # 判据是「无进展」而不是「总耗时」：编码阶段每 1% 就写一次行，所以真在跑的片子
    # 无论多慢都不会碰到这条线；崩溃遗留则由启动对账管，这条只管进程活着的那一半。
    render_stall_sec: float = 600.0


@dataclass
class StorageSettings:
    """上线存储层开关（spec 2026-09-20）。

    只放非敏感项：PG 的 DSN 与 MinIO 的 endpoint/密钥一律走环境变量
    （PG_DSN / MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET），
    不进 TOML、不落盘、不打印。
    """

    backend: str = "pg_minio"          # 生产默认；测试注入用 memory
    workspace_root: Path = Path(".storyline/workspace")   # 渲染临时工作区（本地，可丢弃）
    # cache_root 是内容缓存的**根**：对象存储层（ContentCache）在它下面再开 objects/{sha}
    # 子目录，所以这里不带 objects 段——早先写成 .storyline/cache/objects 会落成
    # objects/objects/ 双层前缀（README §6）。缓存在设计上可丢弃、可重算，改回单层后
    # 遗留的嵌套条目直接命中不到、按 miss 回源重下即可，故不写迁移（取舍见 object_store）。
    cache_root: Path = Path(".storyline/cache")           # sha256 内容寻址缓存的根
    workspace_max_gb: float = 20.0     # 缓存容量上限，超出按 mtime 逐出
    workspace_ttl_sec: float = 21600.0  # 工作区目录过期线，启动时对账扫掉（spec §6）


@dataclass
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    caps: Capabilities = field(default_factory=Capabilities)
    storage: StorageSettings = field(default_factory=StorageSettings)
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        p = Path(path) if path else None
        raw: dict[str, Any] = {}
        if p and p.exists():
            with open(p, "rb") as f:
                raw = tomllib.load(f)
        if "media" in raw:
            raise ValueError(
                "[media] 段已退役（spec §8）：成片字节在 MinIO renders/，"
                "渲染中间产物在 [storage].workspace_root 下的临时工作区")
        s = raw.get("local_mcp_server", {})
        c = raw.get("capabilities", {})
        server = ServerSettings(
            server_name=s.get("server_name", "storyline"),
            url_scheme=s.get("url_scheme", "http"),
            connect_host=s.get("connect_host", "127.0.0.1"),
            host=s.get("host", "0.0.0.0"),
            port=int(s.get("port", 8001)),
            path=s.get("path", "/mcp"),
            json_response=bool(s.get("json_response", False)),
            stateless_http=bool(s.get("stateless_http", False)),
            timeout=int(s.get("timeout", 600)),
            available_nodes=list(s.get("available_nodes", [])),
            available_node_pkgs=list(s.get("available_node_pkgs", [])),
        )
        caps = Capabilities(
            vl_base=c.get("vl_base", Capabilities.vl_base),
            vl_model=c.get("vl_model", Capabilities.vl_model),
            vl_key_env=c.get("vl_key_env", Capabilities.vl_key_env),
            vl_max_tokens=int(c.get("vl_max_tokens", Capabilities.vl_max_tokens)),
            vl_thinking=bool(c.get("vl_thinking", Capabilities.vl_thinking)),
            vl_frames_per_batch=int(c.get("vl_frames_per_batch",
                                          Capabilities.vl_frames_per_batch)),
            vl_max_batches=int(c.get("vl_max_batches", Capabilities.vl_max_batches)),
            asr_model=c.get("asr_model", Capabilities.asr_model),
            asr_device=c.get("asr_device", Capabilities.asr_device),
            asr_compute=c.get("asr_compute", Capabilities.asr_compute),
            asr_timeout_base_sec=float(c.get("asr_timeout_base_sec",
                                             Capabilities.asr_timeout_base_sec)),
            asr_timeout_per_audio_sec=float(
                c.get("asr_timeout_per_audio_sec",
                      Capabilities.asr_timeout_per_audio_sec)),
            provider_timeout_sec=float(c.get("provider_timeout_sec",
                                             Capabilities.provider_timeout_sec)),
            tts_voice=c.get("tts_voice", Capabilities.tts_voice),
            tts_rate=c.get("tts_rate", Capabilities.tts_rate),
            scene_threshold=float(c.get("scene_threshold", Capabilities.scene_threshold)),
            min_shot_sec=float(c.get("min_shot_sec", Capabilities.min_shot_sec)),
            max_shot_sec=float(c.get("max_shot_sec", Capabilities.max_shot_sec)),
            max_pause_sec=float(c.get("max_pause_sec", Capabilities.max_pause_sec)),
            bgm_volume=float(c.get("bgm_volume", Capabilities.bgm_volume)),
            subtitle_font=c.get("subtitle_font", Capabilities.subtitle_font),
            font_dirs=[str(x) for x in c.get("font_dirs", Capabilities().font_dirs)],
            transition_sec=float(c.get("transition_sec", Capabilities.transition_sec)),
            proxy_max_side=int(c.get("proxy_max_side", Capabilities.proxy_max_side)),
            highlight_default_sec=float(c.get("highlight_default_sec",
                                               Capabilities.highlight_default_sec)),
            render_grace_sec=float(c.get("render_grace_sec",
                                         Capabilities.render_grace_sec)),
            render_wait_max_sec=float(c.get("render_wait_max_sec",
                                            Capabilities.render_wait_max_sec)),
            render_max_concurrent=int(c.get("render_max_concurrent",
                                            Capabilities.render_max_concurrent)),
            render_stall_sec=float(c.get("render_stall_sec",
                                         Capabilities.render_stall_sec)),
        )
        st = raw.get("storage", {})
        backend = st.get("backend", "pg_minio")
        if backend not in ("pg_minio", "memory"):
            raise ValueError(
                f"[storage].backend={backend!r} 未知，仅支持 'pg_minio' / 'memory'")
        storage = StorageSettings(
            backend=backend,
            workspace_root=Path(st.get("workspace_root", ".storyline/workspace")),
            cache_root=Path(st.get("cache_root", ".storyline/cache")),
            workspace_max_gb=float(st.get("workspace_max_gb", 20.0)),
            workspace_ttl_sec=float(st.get("workspace_ttl_sec", 21600.0)),
        )
        return cls(server=server, caps=caps, storage=storage, config_path=p)
