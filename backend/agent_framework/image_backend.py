"""R6-T24 出图后端的**唯一一条出网腿**：构造请求、发出去、把**回来的字节**交出去。

为什么要有这个模块，而不是让「自检口」与「链」各写一遍：这仓里已经有两次同形的教训
（L2 的「没有真出图就报 ok:true」、闸⑬ 的「引用了素材、画面上是纯色」）——**自检说自己
好、链上却是另一条路**，是最难查的一类假绿。所以 `/settings/image/test` 与 `imagegen`
节点调的是本模块的同一个函数；自检过的就是链上跑的那条请求，一字不差。

本模块管四件事，每件都对应一个具体的坏法：

1. **可配 model / size**。原先请求体里写死 `"model": "doubao-seedream-5-0-pro"`
   （`server.py:2342`）——换一家的后果是把火山的模型名原样发给别家，第一次调用就 400/404，
   而错误信息里看不出「哪里写死了」。这里把 model 与 size 收成入参，缺省按 provider 补。
2. **真取字节**。原先自检只判 `bool(data["data"][0]["url"])`——**有 URL 就算成功**，
   那张 URL 可能指向 404、指向一张 1×1 占位图、或者根本拉不动。这里必须把字节拉回来，
   并回报字节数与 sha256 前 16 位；「有响应」与「有图」是两件事。
3. **认得出这是不是一张图**。按魔数嗅探（PNG/JPEG/WebP），不是图就报错——省得把一段
   HTML 错误页当成素材投进素材库，等到渲染时才在画面上出现一块空白。
4. **绝不带出密钥**。异常文案只写 provider / 主机名 / HTTP 码，`Authorization` 头
   连片段都不进日志（T24 的口径：密钥只验存在，绝不打印）。

**出网是同步 urllib**（与 `server.py` 原来的写法一致）——这仓没有 aiohttp 依赖，为一条
出网腿引一个异步 HTTP 栈不划算；调用方用 `asyncio.to_thread` 包一层即可。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

#: provider → 默认 base_url。与 `server.py` 原先内联的那张表**同一份**（现在只有这里一份）。
PROVIDER_BASE_URLS: dict[str, str] = {
    "siliconflow": "https://api.siliconflow.cn/v1",
    "minimax": "https://api.minimax.chat/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "aliyun": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "volc": "https://ark.cn-beijing.volces.com/api/v3",
}

#: provider → 默认模型名。空表项 = **这个 provider 没有可用的默认**，必须显式配
#: `image_model`，否则宁可不发请求也不发一个注定错的模型名（发出去只会拿到一条
#: 各家措辞不同的 400，而真正的原因是「我们这边没配」）。
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "siliconflow": "Kwai-Kolors/Kolors",
    "zhipu": "cogview-3-flash",
    "aliyun": "wanx2.1-t2i-turbo",
    "volc": "doubao-seedream-5-0-pro",     # 原来的写死值挪到这里，成为**火山一家的**默认
    "minimax": "",
}

#: 默认尺寸。出图 API 的 size 是各家自定的字符串（`1024x1024` 是通用写法），
#: 所以它只做「有默认值」这一件事，不做校验——各家尺寸表不一样，本地字典只会更早过期。
DEFAULT_SIZE = "1024x1024"

REQUEST_TIMEOUT_SEC = 60.0
DOWNLOAD_TIMEOUT_SEC = 60.0
#: 单张图的字节上限。出图产物正常几百 KB；给到 32 MB 是防「后端回了一整段 HTML」或
#: 「回了一张 8000×8000 的原图」把内存吃干——超过就是异常，如实报，不截断入库。
MAX_IMAGE_BYTES = 32 * 1024 * 1024

#: 魔数 → 扩展名。只认这三种：出图后端实际会回的位图就是它们。GIF 是**故意不收**的
#: （§3.1：动图的帧由浏览器解码，⑬ 的位图身份判不了），回动图就当「不是一张静图」报错。
MAGIC_EXT: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),        # RIFF????WEBP，下面再确认后四位
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ImageBackendError(RuntimeError):
    """出图腿上的任何一种失败。文案面向排障的人，**不带密钥**。"""


@dataclass
class ImageResult:
    """一次出图的产物与账。``data`` 是**真字节**，不是 URL。"""

    data: bytes
    ext: str
    provider: str
    model: str
    size: str
    source: str = ""
    latency_ms: int = 0
    origin_url_host: str = ""
    width: int | None = None
    height: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def sha16(self) -> str:
        return self.sha256[:16]

    @property
    def bytes(self) -> int:
        return len(self.data)


def default_model(provider: str, configured: str = "") -> str:
    """显式配的压过 provider 默认；两边都没有就回空串，由调用方拒绝发请求。"""
    got = (configured or "").strip()
    if got:
        return got
    return PROVIDER_DEFAULT_MODELS.get((provider or "").strip().lower(), "")


def api_base(provider: str, base: str = "") -> str:
    """自建实例地址（`image_base_url`）压过 provider 默认；两边都没有回空串。"""
    got = (base or "").strip()
    if got:
        return got.rstrip("/")
    return PROVIDER_BASE_URLS.get((provider or "").strip().lower(), "").rstrip("/")


def build_request(*, provider: str, base: str, model: str, size: str,
                  prompt: str, count: int = 1) -> tuple[str, dict[str, Any]]:
    """→ (url, body)。**纯函数**：不发网，所以「body 里到底写了什么」可以直接断言。

    这是 T24 那条「去掉硬编码」的可判落点——红样是「把 model 传成 A，body 里出现了 B」。
    body 走 OpenAI 兼容的 `/images/generations` 形状：`{model, prompt, n, size}`。

    空提示词在这里就挡下（而不是留给各家后端去报）：各家对空 prompt 的措辞都不同，
    而真正的原因在我们这边——判在本地，错误文案才指得准。
    """
    if not (prompt or "").strip():
        raise ImageBackendError(
            "出图提示词为空——不许拿空串去打后端（各家的报错措辞都不同，"
            "而真正的原因在我们这边）")
    root = api_base(provider, base)
    if not root:
        raise ImageBackendError(
            f"未知出图后端 {provider!r}：没有默认 base_url，也没有自建实例地址——"
            f"可选 {', '.join(sorted(PROVIDER_BASE_URLS))}，或填「实例地址」")
    url = f"{root}/images/generations"
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": max(1, int(count)),
        "size": size or DEFAULT_SIZE,
        # 显式要 b64：有的后端默认回 url，而 url 会过期（签名地址），
        # 也可能指向一个要求鉴权的桶。拿到字节才算数，所以两边都支持、优先 b64。
        "response_format": "b64_json",
    }
    return url, body


def sniff_ext(data: bytes) -> str:
    """按魔数认格式；不是认识的位图就回空串。"""
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 的 IHDR 里读宽高；不是 PNG 或读不出就回 None。

    为什么值得解析这 24 个字节：**1×1 占位图**是这条链历史上真出现过的一种「成功」——
    HTTP 200、Content-Type: image/png、字节数非零，画面上却什么都没有。宽高是最便宜的
    分辨办法，且它是白名单内的字段（大端无符号 32 位，偏移 16/20），不需要图像库。
    """
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def host_of(url: str) -> str:
    """只留主机名——报错与账面上要能说出「打给了谁」，但不带路径里的签名参数。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    return parts.netloc or ""


def parse_response(data: Any) -> tuple[bytes, str, str]:
    """→ (bytes, ext, origin_url)。优先 b64，其次把 url 拉回来。

    两者都没有 = 后端有响应但没回图——**这一条必须抛**，不能回「成功但空」：
    前半句「后端有响应」是真的，后半句「所以出图成功」不是。
    """
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ImageBackendError(
            f"后端有响应但没回图（响应里没有 data[0]）：{json.dumps(data, ensure_ascii=False)[:200]}")
    item = items[0]
    b64 = item.get("b64_json") or item.get("b64") or ""
    if isinstance(b64, str) and b64.strip():
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            raise ImageBackendError(f"b64_json 解不开（{type(exc).__name__}）") from exc
        return raw, sniff_ext(raw), ""
    url = str(item.get("url") or "").strip()
    if not url:
        raise ImageBackendError(
            f"后端有响应但没回图（data[0] 既无 b64_json 也无 url）："
            f"{json.dumps(item, ensure_ascii=False)[:200]}")
    return b"", "", url


def fetch_bytes(url: str, *, timeout: float = DOWNLOAD_TIMEOUT_SEC) -> bytes:
    """把后端给的 URL 拉成字节。签名地址过期/桶要鉴权都在这里红，而不是投一张空图。"""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ImageBackendError(
            f"取图地址拉不动：HTTP {exc.code}（{host_of(url)}）——"
            "出图后端回的 URL 可能已经过期，或那张图放在需要鉴权的桶里") from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageBackendError(
            f"取图地址拉不动：{type(exc).__name__}: {str(exc)[:160]}（{host_of(url)}）") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageBackendError(
            f"取回的图超过 {MAX_IMAGE_BYTES // 1048576} MB 上限，不当素材处理")
    return raw


def generate(*, provider: str, key: str, base: str, prompt: str,
             model: str = "", size: str = "", count: int = 1,
             timeout: float = REQUEST_TIMEOUT_SEC) -> ImageResult:
    """发一条出图请求，回**真字节**与账。同步；调用方自行 `to_thread`。

    密钥只在这里出现一次（拼 `Authorization` 头），不进返回值、不进异常、不进日志。
    """
    provider = (provider or "").strip().lower()
    prompt = (prompt or "").strip()
    model_name = default_model(provider, model)
    if not model_name:
        raise ImageBackendError(
            f"出图后端 {provider!r} 没有默认模型，且页面上没填「模型名」——"
            f"请在设置里填一个（例如 {', '.join(sorted(PROVIDER_BASE_URLS))} 各家自己的模型 id）")
    use_size = (size or "").strip() or DEFAULT_SIZE
    url, body = build_request(provider=provider, base=base, model=model_name,
                              size=use_size, prompt=prompt, count=count)

    started = time.monotonic()
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_IMAGE_BYTES)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        raise ImageBackendError(
            f"出图请求被拒：HTTP {exc.code}（{host_of(url)}，model={model_name!r}）"
            f"{('：' + detail) if detail else ''}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageBackendError(
            f"出图请求发不出去：{type(exc).__name__}: {str(exc)[:160]}（{host_of(url)}）") from exc

    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise ImageBackendError(
            f"出图响应不是 JSON：{type(exc).__name__}（{host_of(url)}）") from exc

    data, ext, origin_url = parse_response(parsed)
    if not data:
        data = fetch_bytes(origin_url)
        ext = sniff_ext(data)
    if not ext:
        raise ImageBackendError(
            f"拿回来的字节不是认识的位图（{len(data)} 字节，开头 {data[:12]!r}）——"
            "多半是一段 HTML 错误页或 JSON，不能当素材入库")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageBackendError(f"出图产物超过 {MAX_IMAGE_BYTES // 1048576} MB 上限，不当素材处理")
    m = png_size(data)
    notes: list[str] = []
    if m is not None:
        if m[0] <= 1 or m[1] <= 1:
            notes.append(f"后端回的是一张 {m[0]}×{m[1]} 的占位图，画面上会是空白")
    return ImageResult(data=data, ext=ext, provider=provider, model=model_name,
                       size=use_size, latency_ms=int((time.monotonic() - started) * 1000),
                       origin_url_host=host_of(origin_url),
                       width=(m[0] if m else None), height=(m[1] if m else None),
                       notes=notes)
