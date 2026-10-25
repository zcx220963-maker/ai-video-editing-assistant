# -*- coding: utf-8 -*-
"""内核：三个纯函数——写码 / 静帧 / 渲染。没有状态、没有 DB、没有 spec。

拆成三个而不是一个大函数，是为了 `takes`（出 N 条候选、只渲挑中的那一条）：
候选阶段只需要「写码 + 静帧」，成片阶段只需要「渲」。合在一起就没法只做前一半。

    write_code(prompt, ...) -> str          一段 TSX，已过 lint
    stills(code, ...)       -> list[str]    N 张 base64 JPEG，给人挑
    render(code, ...)       -> Path         一个 mp4 文件

失败一律抛 :class:`KernelError`，带 `stage`（write / fix / lint / render / save）——
前端要能说清「卡在哪一步」，不能只回一句「失败了」。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ..motion import remotion_client as rc

DEFAULT_SECONDS = 6.0
#: 单条上限。2026-10-25 从 60 提到 300。
#:
#: 为什么是 300 而不是 180：**有旁白时总长由文案决定**，你说 180 秒、
#: 45 句旁白说下来可能是 181.5 秒——差 0.8% 就把整条片子拒掉是错的
#: （实测就这么拒过一次）。所以这里的 300 只是一个"物理兜底"，
#: 真正的结构上限在 `film`：`MAX_SHOTS(56) × MAX_SHOT_SEC(5.2) = 291 秒`，
#: 超了由 `split_seconds` 给出带解释的报错。
MIN_SECONDS, MAX_SECONDS = 2.0, 300.0
DEFAULT_ASPECT = "portrait"
DEFAULT_FPS = 30
ASPECTS = ("portrait", "landscape", "square")

#: 写码一次、修 lint 最多几次。第三次通常是在同一处打转，继续烧钱不如把错误交出去。
FIX_ROUNDS = 2
CODE_MAX_TOKENS = 8000
CODE_TIMEOUT_SEC = 180.0

#: 静帧取样点（整段比例）与缩放。0.35 倍够看清构图，成本约整镜的 1/100。
STILL_AT = (0.15, 0.50, 0.85)
STILL_SCALE = 0.35


class KernelError(RuntimeError):
    """哪一步失败的、为什么。`stage` 是给用户看的那个词。"""

    def __init__(self, stage: str, msg: str,
                 lint_errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(msg)
        self.stage = stage
        self.lint_errors = list(lint_errors or [])

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "error": str(self),
                "lint_errors": self.lint_errors[:8]}


def out_dir() -> Path:
    """成片落盘目录。容器里是 `/app/.runtime/simple`（有卷、可写）。"""
    d = Path(os.environ.get("SIMPLE_OUT_DIR") or "/app/.runtime/simple")
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001 —— 容器外跑 CLI 时退回临时目录
        p = Path(tempfile.gettempdir()) / "simple"
        p.mkdir(parents=True, exist_ok=True)
        return p


#: 系统提示词——1.2 KB，与主链每镜 46,700 字符是同一个任务的两种写法。
#: 它只讲三件事：怎么写得能跑、怎么写得好看、有哪些零件可调。
_SYSTEM = """你是做动态图形的。写**一个** Remotion 组件，把用户那句描述变成一段画面。

硬性要求（不满足就渲不出来）：
1. 单文件、自包含：`export default Shot;`，顶层 `<AbsoluteFill>` 铺满整帧。
2. **必须** `import { useT, lerp, seg, E } from '@dg/motion'` 并真的用上其中一个
   （`useT()` 给你 0~1 的整段进度，`seg(t, a, b, E.standard)` 切段，`lerp(t, x, y)` 插值）。
3. 时间只从 `useT()` / `useCurrentFrame()` 拿；时长/帧率/画幅从 `useVideoConfig()` 拿，
   **别写死**。
4. 不联网、不引用任何外部图片/字体/音频。画面全部由 SVG / div / canvas 画出来。
5. **纯色块用 SVG 的 `<rect fill>` / `<circle fill>`，不要用 `<div style={{backgroundColor}}>`**。
6. 随机要确定性：用 `random(seed)`，不用 `Math.random`。
7. 只回一个 ```tsx 代码块，不要解释、不要分文件。

画面要求（决定好不好看）：
· 画**那件具体的东西**，不是一页排版。大标题 + 色块 + 几行字不算画面。
· 至少两拍动，而且两拍要**隔开**：第一拍在 0~50% 内做完（主体入画/大色块擦入），
  第二拍在 60%~100% 之间（主运动/收束），中间留一段平稳。所有东西挤在同一瞬间变完，
  看起来是一次跳变，不是动效。
· **主体整段都留在画面内**：起止位置各留 5% 余量，不要走出画外。「穿过」用位移 +
  前后景遮挡表现，不要让主体离场——一段片子里有三成时间没有主体，等于空镜。
· 深底可以，但必须给**高饱和**色块（#FF6B2C / #22D3EE / #FFC93C / #FF3D7F 这一类），
  饱和色块占画面 ≥25%。低饱和的粉彩色渲出来是黑白片。
· 主体要有**不透明**的实体面（受光面 + 背光面 + 落地影），不要整块半透明飘着。
· 有光源方向。字号要有阶梯，别居中堆字。

可用的零件（可选；import 不到就自己画）：
`@dg/motion` 的 `useT / lerp / seg / E`；`@dg/tokens` 的 `TOKENS`；
`@dg/lib/draw/*` 的 Arrow / Book / Building / Chart / Clock / Coin / Crown /
Face / Figure / Gear / Machine / Scale / Vehicle。"""


_FENCE_RE = re.compile(r"```(?:tsx|ts|jsx|javascript|typescript)\s*\n(.*?)```", re.S)
_ANY_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    """从回话里取那段 TSX：先认语言标记明确的块，其次最长的块，最后从首个 import 起。"""
    blocks = [b.strip() for b in _FENCE_RE.findall(text or "") if b.strip()]
    if not blocks:
        blocks = [b.strip() for b in _ANY_FENCE_RE.findall(text or "") if b.strip()]
    if blocks:
        return max(blocks, key=len)
    raw = (text or "").strip()
    at = raw.find("import ")
    return raw[at:].strip() if at >= 0 else ""


def clamp_seconds(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = DEFAULT_SECONDS
    return max(MIN_SECONDS, min(MAX_SECONDS, v))


def norm_aspect(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in ASPECTS else DEFAULT_ASPECT


def frames_of(seconds: float, fps: int) -> int:
    return max(1, int(round(seconds * fps)))


def props_of(seconds: float, aspect: str, fps: int = DEFAULT_FPS) -> dict[str, Any]:
    return {"durationInFrames": frames_of(seconds, fps), "fps": int(fps),
            "aspect": norm_aspect(aspect), "text": ""}


def cache_key(code: str, *, aspect: str, fps: int, frames: int) -> str:
    """产物身份：同一段代码 + 同一组注入参数 = 同一份像素。"""
    blob = f"{aspect}|{fps}|{frames}|{code}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


#: 进程内只绑一次存储：命令行跑这条链时没有 `_boot()` 替我们绑，
#: 而用户密钥存在 PG 的 app_secrets 里——不绑就只会看到环境变量。
_STORAGE_BOUND = False


async def ensure_storage() -> None:
    """把存储层绑上，让模型密钥能按 user_id 热读。已绑过就什么都不做。

    失败不抛：绑不上多半是 PG/MinIO 没起，但那次调用仍可能靠环境变量里的 key 成功；
    真正的错误由 `_complete` 如实报出来。
    """
    global _STORAGE_BOUND
    if _STORAGE_BOUND:
        return
    try:
        from agent_framework import secrets as _secrets
        if _secrets.bound_storage() is not None:
            _STORAGE_BOUND = True
            return
        from agent_framework.storage import build_storage
        st = build_storage("pg_minio")
        await st.start()
        _STORAGE_BOUND = True
    except Exception:  # noqa: BLE001
        pass


async def _complete(messages: list[dict[str, Any]], *, llm: Any, user_id: str,
                    conversation_id: str) -> str:
    from ..motion.selfcheck import _complete_with_identity

    resp = await _complete_with_identity(
        messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
        max_tokens=CODE_MAX_TOKENS, timeout=CODE_TIMEOUT_SEC)
    return str(getattr(resp, "content", "") or "")


def _ask(prompt: str, seconds: float, aspect: str, fps: int) -> str:
    """用户消息。**只给这件事本身**——不给分镜、不给风格包、不给闸的判据。"""
    return (
        f"描述：{prompt.strip()}\n\n"
        f"这一段 {seconds:g} 秒（{frames_of(seconds, fps)} 帧，{fps}fps，"
        f"{aspect}）。所有时间点都按这 {seconds:g} 秒排，铺满整段。"
    )


def _fix_ask(code: str, errors: list[dict[str, Any]]) -> str:
    """把 lint 的错误原样喂回去。给行号与规则名——它照着改，不必猜。"""
    rows = []
    for e in errors[:20]:
        rows.append(f"- 第 {e.get('line')} 行 [{e.get('rule') or ''}] "
                    f"{e.get('msg') or e.get('message') or ''}")
    return ("这段代码过不了闸①（渲染前的静态检查）。错误：\n" + "\n".join(rows)
            + "\n\n只改这些错误，别重写画面。回完整的 ```tsx 代码块。\n\n" + code)


async def write_code(prompt: str, *, seconds: float = DEFAULT_SECONDS,
                     aspect: str = DEFAULT_ASPECT, fps: int = DEFAULT_FPS,
                     llm: Any = None, user_id: str = "",
                     conversation_id: str = "", fix_rounds: int = FIX_ROUNDS,
                     base: str | None = None) -> tuple[str, int]:
    """一次调用写一段 TSX，lint 不过就改。返回 ``(代码, 模型调用次数)``。

    这里是**唯一的循环**：lint → 改 → lint。它不判审美，只判「跑不跑得起来」。
    """
    sec = clamp_seconds(seconds)
    aspect = norm_aspect(aspect)
    await ensure_storage()

    try:
        got = await _complete(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": _ask(prompt, sec, aspect, fps)}],
            llm=llm, user_id=user_id, conversation_id=conversation_id)
    except Exception as exc:  # noqa: BLE001
        raise KernelError("write", f"写码调用失败（{type(exc).__name__}）：{exc}") from exc

    calls = 1
    src = extract_code(got)
    if not src:
        raise KernelError("write", "模型没回代码块")

    errors: list[dict[str, Any]] = []
    for attempt in range(max(0, int(fix_rounds)) + 1):
        try:
            # **必须 to_thread**：remotion_client 是同步 urllib，直接 await 会把事件循环
            # 卡住整个渲染时长——60 秒片子渲 200 秒，期间 HTTP 轮询全部超时（实测踩过）。
            got_lint = await asyncio.to_thread(rc.lint, src, base=base)
        except rc.RemotionError as exc:
            # 服务不可达 ≠ 代码有问题。如实报「没判」，不要伪装成通过。
            raise KernelError("lint", f"渲染服务不可达，lint 未判：{exc}") from exc
        errors = list(got_lint.get("errors") or [])
        if got_lint.get("ok"):
            return src, calls
        if attempt >= fix_rounds:
            raise KernelError("lint", f"lint 改 {fix_rounds} 次仍不过（{len(errors)} 条）",
                              lint_errors=errors)
        try:
            got = await _complete(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _fix_ask(src, errors)}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
            calls += 1
        except Exception as exc:  # noqa: BLE001
            raise KernelError("fix", f"改 lint 的调用失败（{type(exc).__name__}）：{exc}",
                              lint_errors=errors) from exc
        fixed = extract_code(got)
        if not fixed:
            raise KernelError("fix", "改 lint 时模型没回代码块", lint_errors=errors)
        src = fixed

    raise KernelError("lint", "lint 未通过", lint_errors=errors)


async def stills(code: str, *, seconds: float = DEFAULT_SECONDS,
                 aspect: str = DEFAULT_ASPECT, fps: int = DEFAULT_FPS,
                 at: tuple[float, ...] = STILL_AT, scale: float = STILL_SCALE,
                 base: str | None = None) -> list[str]:
    """渲 N 张静帧（base64 JPEG）。成本约整镜的 1/100，用来给人挑。"""
    sec = clamp_seconds(seconds)
    frames = frames_of(sec, fps)
    picks = sorted({max(0, min(frames - 1, int(round(r * (frames - 1))))) for r in at})
    return await stills_frames(code, picks, seconds=sec, aspect=aspect, fps=fps,
                               scale=scale, base=base)


async def stills_pairs(code: str, frames: list[int], *,
                       seconds: float = DEFAULT_SECONDS,
                       aspect: str = DEFAULT_ASPECT, fps: int = DEFAULT_FPS,
                       scale: float = STILL_SCALE,
                       base: str | None = None) -> list[dict[str, Any]]:
    """按**绝对帧号**渲静帧，返回 ``[{frame, jpeg_b64}]``。

    **服务端一次最多渲 4 帧**（`/render-stills` 的上限，实测：要 6 张只回 4 张）。
    所以调用方必须自己分批；这里不替它分批——分批策略（按镜分组）是质检的事。
    """
    sec = clamp_seconds(seconds)
    picks = sorted({int(f) for f in frames})
    if not picks:
        return []
    try:
        got = await asyncio.to_thread(
            rc.render_stills, code=code, input_props=props_of(sec, aspect, fps),
            frames=picks, scale=scale, base=base)
    except rc.RemotionError as exc:
        raise KernelError("stills", f"静帧渲染失败：{exc}") from exc
    return [{"frame": int(item.get("frame", -1)),
             "jpeg_b64": str(item.get("jpeg_b64") or "")}
            for item in got if item.get("jpeg_b64")]


async def stills_frames(code: str, frames: list[int], *,
                        seconds: float = DEFAULT_SECONDS,
                        aspect: str = DEFAULT_ASPECT, fps: int = DEFAULT_FPS,
                        scale: float = STILL_SCALE,
                        base: str | None = None) -> list[str]:
    """只要 base64 的版本（按**绝对帧号**）。"""
    pairs = await stills_pairs(code, frames, seconds=seconds, aspect=aspect,
                               fps=fps, scale=scale, base=base)
    return [p["jpeg_b64"] for p in pairs]


async def render(code: str, *, seconds: float = DEFAULT_SECONDS,
                 aspect: str = DEFAULT_ASPECT, fps: int = DEFAULT_FPS,
                 out_path: str | Path | None = None, draft: bool = False,
                 base: str | None = None) -> tuple[Path, dict[str, Any]]:
    """渲成片并落盘。返回 ``(路径, 渲染回执)``。

    **超时按帧数算，不用固定值**：`rc.render_shot` 缺省 900 秒，而渲染是纯 CPU、
    约 330ms/帧——1800 帧（60 秒）刚好卡在 10 分钟，3690 帧（123 秒）就会超时。
    实测 2026-10-25 那条 123 秒的片子就是这么挂在 `TimeoutError: timed out` 上的，
    而报错发生在**渲染已经开始之后**（钱和时间都花了）。
    """
    sec = clamp_seconds(seconds)
    aspect = norm_aspect(aspect)
    props = props_of(sec, aspect, fps)
    frames = int(props["durationInFrames"])
    key = cache_key(code, aspect=aspect, fps=fps, frames=frames)
    # 0.6 秒/帧是实测 330ms 的两倍余量，再加 300 秒给 bundle 与首帧。
    timeout = max(900, int(frames * 0.6) + 300)
    try:
        res = await asyncio.to_thread(
            rc.render_shot, code=code, cache_key=key, input_props=props,
            render={"draft": True} if draft else None, base=base, timeout=timeout)
    except rc.RemotionError as exc:
        raise KernelError("render", f"渲染失败：{exc}",
                          lint_errors=list(getattr(exc, "lint_errors", []) or [])) from exc

    dest = Path(out_path) if out_path else (out_dir() / f"{key}.mp4")
    try:
        await asyncio.to_thread(rc.fetch_artifact,
                                str(res.get("artifact") or ""), dest, base=base)
    except rc.RemotionError as exc:
        raise KernelError("save", f"产物下载失败：{exc}") from exc
    return dest, res


def describe_receipt(res: dict[str, Any]) -> dict[str, Any]:
    """把渲染回执压成前端要的那几个数。"""
    comp = res.get("composition") or {}
    fps = comp.get("fps") or DEFAULT_FPS
    frames = comp.get("durationInFrames") or 0
    return {
        "seconds": round(frames / max(1, fps), 2),
        "fps": fps,
        "size": [comp.get("width"), comp.get("height")],
        "warnings": [w for w in (res.get("warnings") or [])
                     if "闸⑤" not in w and "闸⑧" not in w][:4],
        "motion": (res.get("motion") or {}).get("status"),
        "ms": res.get("timings") or {},
    }


def now_ms() -> int:
    return int(time.time() * 1000)
