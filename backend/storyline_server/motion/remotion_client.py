# -*- coding: utf-8 -*-
"""creation-remotion 容器的 HTTP 客户端——图形科普片「代码镜」这条链的唯一外呼口。

契约真源是 `remotion/server.mjs`，这五个口之外没有别的接口：

    GET  /healthz       → {ok, node, remotion, chromium, gl, master_hash, lockfile_hash}
    POST /lint          → {ok, errors:[{rule,line,column,msg}], declared}   恒 200
    POST /render-shot   → {ok, cache_key, artifact, bytes, composition,
                            probed, timings, tokens, fields_used, layout_used,
                            motion, warnings}

    GET  /artifact/{k}  → video/mp4 字节

三条写代码时必须记住的事：

1. **都是阻塞调用**。真实节点里一律 `asyncio.to_thread(...)` 包出去，别在事件循环里
   直接调——一镜渲染是几十秒到几分钟量级。
2. **/render-shot 在服务端是串行的**（一次只放一镜进浏览器，避开软件 WebGL 的内存
   尖峰），所以「响应慢」里有一部分是排队。超时按排队 + 渲染两段预算给，默认 900 秒。
3. **cache_key 由调用方按字节算好传进来**，服务端拒绝缺省（缺省＝缓存永不命中、产物
   无界堆积）。同理 `durationInFrames` 必须由 Python 侧按 TTS 实测秒数注入，服务端
   看到 0 直接 400——这是闸②的落地处，客户端不替调用方兜底。
4. **键里的 `master_hash` / `lockfile_hash` 取 /healthz 现报的值**，不在 Python 侧重算
   文件哈希。两处各算一遍迟早不一致（换行符、编码、算哪几个文件），而键漂移的表现是
   **静默命中旧母版渲出的切片**——改了什么全片换像素、账上却看不出来。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENV_URL = "REMOTION_URL"
DEFAULT_URL = "http://remotion:8010"
USER_AGENT = "creation-remotion-client/1"


class RemotionError(RuntimeError):
    """外呼失败。`status` 是服务端的 HTTP 码，`lint_errors` 只在闸①拒绝时有值。"""

    def __init__(self, msg: str, *, status: int | None = None,
                 lint_errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(msg)
        self.status = status
        self.lint_errors = lint_errors or []


def base_url(base: str | None = None) -> str:
    return (base or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")


def _request(method: str, path: str, *, base: str | None, payload: dict | None,
             timeout: int) -> tuple[int, dict[str, Any]]:
    url = f"{base_url(base)}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {"error": raw.decode("utf-8", "replace")[:400]}
        msg = str(parsed.get("error") or f"{method} {path} 失败（HTTP {e.code}）")
        raise RemotionError(msg, status=e.code, lint_errors=parsed.get("errors") or []) from e
    except urllib.error.URLError as e:
        raise RemotionError(f"creation-remotion 不可达（{url}）：{e.reason}", status=None) from e
    try:
        return resp.status, json.loads(body.decode("utf-8"))
    except Exception as e:
        raise RemotionError(f"{method} {path} 返回的不是 JSON：{e}") from e


def health(*, base: str | None = None, timeout: int = 10) -> dict[str, Any]:
    """服务存活 + 本次实测的 gl 档（性能表要的那一列）。"""
    _, j = _request("GET", "/healthz", base=base, payload=None, timeout=timeout)
    return j


def lint(code: str, *, declared_fields: list[str] | None = None,
         base: str | None = None, timeout: int = 60) -> dict[str, Any]:
    """闸①。恒 200，返回 {ok, errors, declared}；不 ok 由调用方决定打回还是放弃。

    `declared_fields` 是这一镜声明的字段名。字段对账那三条规则拿它比，而名单不在
    代码字节里——不传就等于让这三条只按「注入的 fields 表」判，与渲染口那条退回路径
    同口径。调用方（执行层）有归一化后的分镜，应当显式传，别靠服务端猜。
    """
    payload: dict[str, Any] = {"code": code}
    if declared_fields is not None:
        payload["declared_fields"] = list(declared_fields)
    _, j = _request("POST", "/lint", base=base, payload=payload, timeout=timeout)
    return j


def render_shot(*, code: str, cache_key: str, input_props: dict[str, Any],
                declared_fields: list[str] | None = None,
                render: dict[str, Any] | None = None,
                base: str | None = None, timeout: int = 900) -> dict[str, Any]:
    """渲一镜。服务端会自己再 lint 一遍，返回的 `errors` 就是拒绝理由。

    抛错时 `lint_errors` 非空 = 闸①拒绝（重交代码可修），为空 = 渲染/超预算类失败。
    `render` 是**像素档**：`{draft, scale, image_format, jpeg_quality, crf, concurrency}`。
    不传＝服务端默认（成片档），传了也只是换档，**不换判据**——闸⑭ 读 64 px 宽的灰度差、
    闸⑥⑧⑲-a 读比例盒，都与输出分辨率无关。所以 `draft=True` 那一档是给验收循环用的：
    一镜改十几遍时先按草稿档数闸，判据绿了再按成片档烧一次。
    草稿档**不许进切片缓存**（服务端把产物落在 `<key>.draft.mp4`，与成片档不同名，
    就是为了不让一份缩了的片子占住成片键去冒充成片）。
    """
    payload: dict[str, Any] = {"code": code, "cache_key": cache_key,
                               "input_props": input_props}
    if declared_fields is not None:
        payload["declared_fields"] = list(declared_fields)
    if render is not None:
        payload["render"] = dict(render)
    status, j = _request("POST", "/render-shot", base=base, payload=payload,
                         timeout=timeout)
    if status != 200 or not j.get("ok"):
        raise RemotionError(f"渲染失败：{j.get('error') or j}", status=status,
                            lint_errors=j.get("errors") or [])
    return j


def fetch_artifact(artifact: str, dest: Path, *, base: str | None = None,
                   timeout: int = 300) -> int:
    """把 `/artifact/{key}` 抓到本地。返回写字节数，与 content-length 对不上就抛。

    对账按字节做是刻意的：切片缓存命中判据是「同一 cache_key 同一份像素」，
    一次被截断的下载如果静默落盘，后续所有帧比对都在比一份坏文件。
    """
    url = f"{base_url(base)}{artifact}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            declared = resp.headers.get("content-length")
            data = resp.read()
    except urllib.error.URLError as e:
        raise RemotionError(f"产物下载失败（{url}）：{e.reason}") from e
    if declared is not None and int(declared) != len(data):
        raise RemotionError(f"产物字节数对不上：声明 {declared}，实收 {len(data)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


def render_stills(*, code: str, input_props: dict[str, Any], frames: list[int],
                  scale: float = 0.4, base: str | None = None,
                  timeout: int = 120, with_layout: bool = False) -> Any:
    """预览静帧：渲这一镜的几帧小 JPEG → ``[{frame, jpeg_b64}]``。

    成本约是整镜渲染的百分之一（打包 1~3 秒 + 每帧几百毫秒），用在渲染前的计划确认卡上
    给人看——这是「抽几帧看」的主流验证方式，替代「整镜烧完再由机器判」。

    ``with_layout=True`` 时改回 ``{"stills": […], "layout": {…} | None}``：服务端这一趟
    本来就带回一行版面读数（字压字、字压图、出画——见 `server.mjs` 的 `renderStills`），
    原先这里只取图、把读数丢了，于是「烧像素之前就能改」的那一次机会白白放过。
    缺省仍回列表，别的调用方不受影响。
    """
    payload = {"code": code, "input_props": input_props, "frames": list(frames),
               "scale": scale}
    status, j = _request("POST", "/render-stills", base=base, payload=payload,
                         timeout=timeout)
    if status != 200 or not j.get("ok"):
        raise RemotionError(f"预览静帧失败：{j.get('error') or j}", status=status)
    stills = list(j.get("stills") or [])
    if with_layout:
        layout = j.get("layout")
        return {"stills": stills, "layout": layout if isinstance(layout, dict) else None}
    return stills


def render_stills_multi(*, code: str, variants: list[dict[str, Any]],
                        scale: float = 0.4, base: str | None = None,
                        timeout: int = 300) -> list[dict[str, Any]]:
    """**一段代码、多组 props** 各渲一张静帧 → ``[{variant, frame, jpeg_b64}]``。

    与 :func:`render_stills` 是同一个口：差别只在这个走它的 `variants` 分段口，
    于是渲染容器**只打包一次**。为什么值得单独一条：一屏字幕样张是六档样式共用同一份
    样片代码、只换注入的 props——逐档各发一次就要各打包一遍（每遍 1~3 秒），
    面板上点一下要干等十几秒。

    `variant` 是**序号回执**：调用方按它对号入座，不靠数组次序猜（少一张图时，
    按次序对就会把「第 5 档的图」发成「第 6 档的图」，且看不出来）。
    """
    payload = {"code": code, "variants": list(variants), "scale": scale}
    status, j = _request("POST", "/render-stills", base=base, payload=payload,
                         timeout=timeout)
    if status != 200 or not j.get("ok"):
        raise RemotionError(f"样张渲染失败：{j.get('error') or j}", status=status)
    return list(j.get("stills") or [])


def endpoints(*, base: str | None = None, health_timeout: int = 10,
              lint_timeout: int = 60, render_timeout: int = 900,
              fetch_timeout: int = 300) -> dict[str, Any]:
    """几个口绑成一组普通可调用对象：``{health, lint, render, fetch, stills,
    stills_multi}``。

    执行层拿这一组而不是直接 import 本模块的函数，为的是**依赖注入不用 monkeypatch**：
    离线用例塞一个假服务就是一段 dict，而改模块函数会把别的用例一起带跑。
    位置参数口径固定：``render(code, cache_key, input_props)``、``fetch(artifact, dest)``。
    """
    return {
        "health": lambda: health(base=base, timeout=health_timeout),
        "lint": lambda code, declared_fields=None: lint(
            code, declared_fields=declared_fields, base=base, timeout=lint_timeout),
        "render": lambda code, cache_key, input_props, declared_fields=None, render=None: render_shot(
            code=code, cache_key=cache_key, input_props=input_props,
            declared_fields=declared_fields, render=render, base=base,
            timeout=render_timeout),
        "fetch": lambda artifact, dest: fetch_artifact(artifact, dest, base=base,
                                                       timeout=fetch_timeout),
        "stills": lambda code, input_props, frames, scale=0.4: render_stills(
            code=code, input_props=input_props, frames=frames, scale=scale, base=base),
        "stills_layout": lambda code, input_props, frames, scale=0.4: render_stills(
            code=code, input_props=input_props, frames=frames, scale=scale, base=base,
            with_layout=True),
        "stills_multi": lambda code, variants, scale=0.4: render_stills_multi(
            code=code, variants=variants, scale=scale, base=base),
    }
