"""模型通道自检：用「当前真正解析到的那把 key」各发一次文本与带图请求。

为什么必须带图：这个项目「一个 key 用到底」的前提是同一个服务商的同一个模型既写文案
又做视觉理解（deepseek-flash 实测支持 image_url）。只测文本会漏掉「key 有效但模型不
吃图」这一类真实故障——而它正是视觉理解整链静默降级、文案全靠占位的起点。

安全边界：返回值里只有掩码、耗时、状态与模型回复摘要，任何分支都不带出密钥值。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any

from . import secrets
from .identity import use_identity
from .llm_openai import DEFAULT_THINKING, THINKING_OFF

PROBE_TIMEOUT_SEC = 30.0
# 48×32 的渐变测试图（约 0.7 KB）：只为验证 image_url 这条通道收不收、答不答。
TEST_IMAGE_DATA_URL = (
    "data:image/jpeg;base64,"
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcv"
    "Jik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAAgADADASIAAhEBAxEB/8QAHwAAAQUB"
    "AQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJx"
    "FDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlq"
    "c3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi"
    "4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQD"
    "BAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2"
    "Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmq"
    "srO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDgFs/a"
    "pVs/atlbP2qZbP2r9CxeJPMwuLMZbP2qZbP2rYWz9qmWz9q+UxeJPpcLizHWz9qmWz9q2Fs/apls/avlMXiT"
    "6XC4sVbP2qZbP2rYWz9qmWz9q+0xeJPwrC4sx1s/apVs/atlbP2qZbP2r5TF4k+lwuLMZbP2qZbP2rYWz9qm"
    "Wz9q+UxeJPpcLiz/2Q=="
)
_TEXT_PROMPT = "只回复四个字，不要任何标点或解释：连接正常"
_VISION_PROMPT = "用一句中文说明这张图里有什么。只输出这句话。"


def _brief(raw: bytes | str, limit: int = 180) -> str:
    """错误摘要：压掉换行并截断，避免把整页 HTML 灌进前端。"""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return " ".join(text.split())[:limit]


def _post_chat(base_url: str, key: str, payload: dict[str, Any]) -> tuple[bool, str, int]:
    """阻塞的一次 chat/completions（调用方用 to_thread 包住）。→ (是否可用, 摘要, 毫秒)"""
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "creation-assistant/1.0",
                 "Authorization": f"Bearer {key}"}, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {_brief(e.read())}", int((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001 - 自检就是把异常变成可读文本给人看
        return False, f"{type(e).__name__}: {_brief(str(e))}", int((time.monotonic() - t0) * 1000)
    ms = int((time.monotonic() - t0) * 1000)
    msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        spent = len((msg.get("reasoning_content") or "").strip())
        return False, f"content 为空（思考内容占用了 {spent} 字符）", ms
    return True, text[:80], ms


async def probe_text(user_id: str, llm: Any) -> dict[str, Any]:
    """走生产同款通道：绑好身份后调用运行时 LLM client，验的是热读之后的真实链路。"""
    t0 = time.monotonic()
    try:
        with use_identity(user_id, "selfcheck"):
            resp = await asyncio.wait_for(
                llm.complete(messages=[{"role": "user", "content": _TEXT_PROMPT}], tools=None),
                timeout=PROBE_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {_brief(str(e))}",
                "ms": int((time.monotonic() - t0) * 1000)}
    reply = (getattr(resp, "content", "") or "").strip()
    return {"ok": bool(reply), "detail": reply[:80] or "content 为空",
            "ms": int((time.monotonic() - t0) * 1000)}


async def probe_vision(user_id: str, *, base_url: str, model: str,
                       thinking: bool = DEFAULT_THINKING) -> dict[str, Any]:
    """带图一次请求：视觉理解用的就是同一把 key，这一步过了剪辑链才不会静默降级。"""
    key, source = await secrets.resolve_api_key(user_id)
    if not key:
        return {"ok": False, "detail": f"未取到可用密钥（来源 {source}）", "ms": 0}
    payload = {"model": model, "max_tokens": 256, "temperature": 0.3,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": _VISION_PROMPT},
                   {"type": "image_url", "image_url": {"url": TEST_IMAGE_DATA_URL}}]}]}
    # 跟运行时同一条思考开关：probe 的意义是「生产会怎么走这里就怎么走」
    if not thinking:
        payload.update(THINKING_OFF)
    ok, detail, ms = await asyncio.to_thread(_post_chat, base_url, key, payload)
    return {"ok": ok, "detail": detail, "ms": ms}


async def self_check(storage: Any, user_id: str, llm: Any) -> dict[str, Any]:
    """一次完整自检：先确认 key 从哪来，再分别验文本与视觉，全绿才算可用。"""
    key, source = await secrets.resolve_api_key(user_id, fallback=getattr(llm, "api_key", ""))
    base_url = getattr(llm, "base_url", "") or ""
    model = getattr(llm, "model", "") or ""
    out: dict[str, Any] = {"source": source, "masked": secrets.mask(key), "model": model,
                           "base_url": base_url, "backend": getattr(storage, "backend", ""),
                           "thinking": bool(getattr(llm, "thinking", DEFAULT_THINKING))}
    if not key:
        out.update({"ok": False, "text": None, "vision": None,
                    "detail": "未配置密钥：在「设置」里填入 API Key 后再试"})
        return out
    out["text"] = await probe_text(user_id, llm)
    out["vision"] = await probe_vision(user_id, base_url=base_url, model=model,
                                       thinking=getattr(llm, "thinking", DEFAULT_THINKING))
    out["ok"] = bool(out["text"]["ok"] and out["vision"]["ok"])
    out["detail"] = ("两路通道均可用" if out["ok"]
                     else "有一路不通：文本走文案/对话，视觉走 understand_clips，"
                          "任一路失败对应功能都会降级")
    return out
