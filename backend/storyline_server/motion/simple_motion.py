# -*- coding: utf-8 -*-
"""**一句话出一条片子**——把「Claude + Remotion」那种体验单独做出来的一条链。

为什么要有这个文件（2026-10-24）
--------------------------------
`plan_motion` 那条链解决的是「3 分钟、12 镜、中文排版、事实核查、每次都要成」。
它为此付出了 6.9 万行、25 道闸、四道保险的代价——那些代价是**任务**要的，不是
「出片」这件事本身要的。用户真正想要的「像网上那样简单」，在任务缩到
**一个镜头、一句描述、一段 5~15 秒**之后，其实只需要四步：

    一句提示 → 一次模型调用写一段 TSX → lint → 渲一镜 → 一个 mp4

这个文件就是那四步，**故意不做**下面这些：

- 没有需求文档、没有分镜 spec、没有导演、没有配方卡；
- 没有闸④⑤⑥⑧⑭⑰⑲⑳㉑㉕㉜、没有静帧评审、没有回写环；
- 没有 DB、没有 session、没有 artifact 表。

留下的只有两道**躲不掉**的硬闸，因为它们是物理约束而不是审美判断：

1. **lint**——代码跑不起来就没有帧。`/render-shot` 服务端本来就会自己再 lint 一遍，
   这里先 lint 一次只是为了**能拿到错误去改**，而不是等到渲染失败。
2. **durationInFrames**——Remotion 的合成必须有确定时长，服务端看到 0 直接 400。

其余一切（好不好看、寡不寡淡、动效够不够）**不做机器判定**，只把渲染服务回的
`warnings` 原样交出去给人看。这是刻意的：那条链的教训是代理指标越多、误判越多、
时间越花在让指标不打架上。这里把审美判断还给人和模型。

关于**方差**（实测三次，2026-10-24）
------------------------------------
同一句提示连跑三次，三次的代码结构完全不同，观感明显有高低（鲸鱼穿城市那次，
第一版最好，第二版主体提前出画、第三版窗户铺成了像素网格）。这就是网上那些 demo
不告诉你的那一半：**它们是挑出来的**。所以这条链的用法是「重跑 2~3 次挑一版」，
不是「一次就要对」。想稳住就写细一点提示（「主体整段留在画面内」「窗户别铺满」这类
具体的约束比形容词管用）。

同样，渲染服务回的 `闸⑭ 动效事件密度：只数到 1 个变化段` **不一定是错的**：一段
连续的横移（主体匀速穿过画面）在它的判据里就是「一段」。三次实测里这条一直在响，
而画面是好的——这正是「代理指标代替不了审美」的现场证据。这里只把它当提示。

用法
----
命令行（在 `backend/` 下，或容器里）：

    python -m storyline_server.motion.simple_motion "一只鲸鱼穿过夜晚的城市" \\
        --seconds 8 --aspect portrait --out ../../outputs/whale.mp4

    # 只渲、不调模型（改一版代码时最常用，几秒出片）
    python -m storyline_server.motion.simple_motion --code-file my_shot.tsx --seconds 8

代码里：

    from storyline_server.motion.simple_motion import make_video
    got = await make_video("一只鲸鱼穿过夜晚的城市", seconds=8,
                           user_id=uid, conversation_id=cid)
    got["mp4"]   # 落盘路径
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import remotion_client as rc

#: 一镜的缺省时长。8 秒是「够看清一件事、又不至于让一次生成承担叙事」的长度。
DEFAULT_SECONDS = 8.0
DEFAULT_ASPECT = "portrait"
DEFAULT_FPS = 30
#: 时长下限/上限。低于 2 秒看不清，高于 60 秒就不该走这条链（该走分镜那条）。
MIN_SECONDS, MAX_SECONDS = 2.0, 60.0
ASPECTS = ("portrait", "landscape", "square")

#: 写码一次、修 lint 最多几次。与 `shot_writer` 的两次同口径——第三次通常是在
#: 同一处打转，继续烧钱不如把错误交出去。
FIX_ROUNDS = 2
CODE_MAX_TOKENS = 8000
CODE_TIMEOUT_SEC = 180.0

def _project_root() -> Path:
    """项目根。宿主机上是 `<repo>/backend/storyline_server/motion/…`（往上三层），
    容器里是 `/app/storyline_server/motion/…`（往上两层）——两处都要落得对，
    否则缺省产物会写到 `/outputs` 这种莫名其妙的地方。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend").is_dir() or (parent / "outputs").is_dir():
            return parent
    return here.parents[2]


ROOT = _project_root()


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


def _ask(prompt: str, seconds: float, aspect: str, fps: int) -> str:
    """用户消息。**只给这件事本身**——不给分镜、不给风格包、不给闸的判据。"""
    return (
        f"描述：{prompt.strip()}\n\n"
        f"这一段 {seconds:g} 秒（{int(round(seconds * fps))} 帧，{fps}fps，"
        f"{aspect}）。所有时间点都按这 {seconds:g} 秒排，铺满整段。"
    )


def _fix_ask(code: str, errors: list[dict[str, Any]]) -> str:
    """把 lint 的错误原样喂回去。给行号与规则名——它照着改，不必猜。"""
    rows = []
    for e in errors[:20]:
        line = e.get("line")
        rule = e.get("rule") or ""
        msg = e.get("msg") or e.get("message") or ""
        rows.append(f"- 第 {line} 行 [{rule}] {msg}")
    return (
        "这段代码过不了闸①（渲染前的静态检查）。错误：\n"
        + "\n".join(rows)
        + "\n\n只改这些错误，别重写画面。回完整的 ```tsx 代码块。\n\n"
        + code
    )


def _cache_key(code: str, *, aspect: str, fps: int, frames: int) -> str:
    """产物身份：同一段代码 + 同一组注入参数 = 同一份像素。"""
    blob = f"{aspect}|{fps}|{frames}|{code}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


async def _complete(messages: list[dict[str, Any]], *, llm: Any, user_id: str,
                    conversation_id: str) -> str:
    from .selfcheck import _complete_with_identity

    resp = await _complete_with_identity(
        messages, llm=llm, user_id=user_id, conversation_id=conversation_id,
        max_tokens=CODE_MAX_TOKENS, timeout=CODE_TIMEOUT_SEC)
    return str(getattr(resp, "content", "") or "")


#: 进程内只绑一次。命令行跑这条链时**没有** `_boot()` 替我们绑存储，
#: 而用户的模型密钥存在 PG 的 app_secrets 里——不绑，`resolve_api_key` 就只能看到
#: 环境变量，报出来的却是「未配置模型密钥」（真机上那句话会把人带到错误的排查方向）。
_STORAGE_BOUND = False


async def ensure_storage() -> None:
    """把存储层绑上，让模型密钥能按 user_id 热读。已经绑过（服务进程内）就什么都不做。

    失败**不抛**：绑不上多半是 PG/MinIO 没起，但那次调用仍可能靠环境变量里的 key 成功。
    真正的错误会在 `_complete` 里如实报出来。
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
    except Exception:  # noqa: BLE001 —— 见 docstring：让后面的调用自己报错
        pass


async def make_video(prompt: str, *, seconds: float = DEFAULT_SECONDS,
                     aspect: str = DEFAULT_ASPECT, fps: int = DEFAULT_FPS,
                     llm: Any = None, user_id: str = "",
                     conversation_id: str = "", fix_rounds: int = FIX_ROUNDS,
                     out_path: str | Path | None = None, draft: bool = False,
                     code: str = "", base: str | None = None,
                     keep_code: bool = True) -> dict[str, Any]:
    """一句话 → 一个 mp4。返回一份**如实**的账，失败也不抛。

    参数：
        prompt    这一镜要画什么。一句话就够；写细一点画面会更准。
        seconds   时长（2~60 秒）。它同时是「这段片子多长」与「动效铺多久」。
        aspect    portrait / landscape / square。
        code      直接给代码就跳过模型（改一版代码重渲时用，几秒出片）。
        fix_rounds lint 不过时最多改几次。

    返回：
        {"ok": True, "mp4": 路径, "code": 源码, "calls": 模型调用次数,
         "lint_errors": [], "warnings": 渲染服务的告警, "ms": {...}}
        失败时 {"ok": False, "stage": "gen|fix|lint|render|save", "error": ...}
    """
    t0 = time.time()
    out: dict[str, Any] = {"ok": False, "calls": 0, "lint_errors": [], "warnings": []}

    sec = max(MIN_SECONDS, min(MAX_SECONDS, float(seconds)))
    if aspect not in ASPECTS:
        aspect = DEFAULT_ASPECT
    frames = max(1, int(round(sec * fps)))
    props = {"durationInFrames": frames, "fps": int(fps), "aspect": aspect, "text": ""}

    # ---- ① 写码（或直接用给的代码）----
    src = (code or "").strip()
    if not src:
        await ensure_storage()
        try:
            got = await _complete(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _ask(prompt, sec, aspect, fps)}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
            out["calls"] += 1
        except Exception as exc:  # noqa: BLE001 —— 调用炸了要如实报，不能装作在写
            out.update(stage="gen", error=f"写码调用失败（{type(exc).__name__}）：{exc}")
            return out
        src = extract_code(got)
        if not src:
            out.update(stage="gen", error="模型没回代码块")
            return out

    # ---- ② lint（不过就让模型改，最多 fix_rounds 次）----
    errors: list[dict[str, Any]] = []
    for attempt in range(max(0, int(fix_rounds)) + 1):
        try:
            got_lint = rc.lint(src, base=base)
        except rc.RemotionError as exc:
            # 服务不可达 ≠ 代码有问题。如实报「没判」，不要伪装成 lint 通过。
            out.update(stage="lint", error=f"渲染服务不可达，lint 未判：{exc}")
            return out
        errors = list(got_lint.get("errors") or [])
        if got_lint.get("ok"):
            break
        if attempt >= fix_rounds:
            out.update(stage="lint", lint_errors=errors,
                       error=f"lint 改 {fix_rounds} 次仍不过（{len(errors)} 条）")
            return out
        try:
            got = await _complete(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _fix_ask(src, errors)}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
            out["calls"] += 1
        except Exception as exc:  # noqa: BLE001
            out.update(stage="fix", lint_errors=errors,
                       error=f"改 lint 的调用失败（{type(exc).__name__}）：{exc}")
            return out
        fixed = extract_code(got)
        if not fixed:
            out.update(stage="fix", lint_errors=errors, error="改 lint 时模型没回代码块")
            return out
        src = fixed

    # ---- ③ 渲一镜 ----
    key = _cache_key(src, aspect=aspect, fps=fps, frames=frames)
    try:
        res = rc.render_shot(code=src, cache_key=key, input_props=props,
                             render={"draft": True} if draft else None, base=base)
    except rc.RemotionError as exc:
        out.update(stage="render", error=f"渲染失败：{exc}",
                   lint_errors=list(getattr(exc, "lint_errors", []) or []))
        return out

    # ---- ④ 取字节落盘 ----
    dest = Path(out_path) if out_path else (
        ROOT / "outputs" / "simple" / f"{key}.mp4")
    try:
        n = rc.fetch_artifact(str(res.get("artifact") or ""), dest, base=base)
    except rc.RemotionError as exc:
        out.update(stage="save", error=f"产物下载失败：{exc}")
        return out

    comp = res.get("composition") or {}
    out.update(
        ok=True, mp4=str(dest), bytes=n, cache_key=key,
        seconds=round(comp.get("durationInFrames", frames) / max(1, comp.get("fps", fps)), 2),
        aspect=aspect, fps=comp.get("fps", fps),
        size=[comp.get("width"), comp.get("height")],
        warnings=list(res.get("warnings") or []),
        motion=(res.get("motion") or {}).get("status"),
        ms={"total": int((time.time() - t0) * 1000),
            **(res.get("timings") or {})},
    )
    if keep_code:
        out["code"] = src
    return out


def describe(got: dict[str, Any]) -> str:
    """把账翻成人话——命令行与工具回执共用这一份措辞。"""
    if not got.get("ok"):
        head = f"没出来（{got.get('stage')}）：{got.get('error')}"
        errs = got.get("lint_errors") or []
        if errs:
            head += "\n" + "\n".join(
                f"  · 第 {e.get('line')} 行 [{e.get('rule')}] "
                f"{e.get('msg') or e.get('message')}" for e in errs[:8])
        return head
    ms = got.get("ms") or {}
    lines = [
        f"成片：{got['mp4']}",
        f"{got.get('seconds')}s · {got.get('aspect')} · "
        f"{(got.get('size') or ['?', '?'])[0]}×{(got.get('size') or ['?', '?'])[1]} · "
        f"{round(got.get('bytes', 0) / 1024)}KB",
        f"耗时 {round(ms.get('total', 0) / 1000, 1)}s"
        f"（打包 {ms.get('bundle_ms', 0)}ms / 渲染 {ms.get('render_ms', 0)}ms），"
        f"模型调用 {got.get('calls')} 次",
    ]
    warns = [w for w in (got.get("warnings") or [])
             if not w.startswith("[browser]") and "闸⑧ 版面" not in w]
    if warns:
        lines.append("渲染服务的提醒：" + "；".join(warns[:3]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="simple_motion", description="一句话出一条片子（单镜，跳过全部分镜流程）")
    ap.add_argument("prompt", nargs="?", default="", help="这一镜要画什么")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    ap.add_argument("--aspect", default=DEFAULT_ASPECT, choices=list(ASPECTS))
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--out", default="", help="mp4 落盘路径")
    ap.add_argument("--code-file", default="", help="直接渲这份 TSX，不调模型")
    ap.add_argument("--draft", action="store_true", help="草稿档（缩分辨率，快）")
    ap.add_argument("--user-id", default="")
    ap.add_argument("--conversation-id", default="")
    ap.add_argument("--json", action="store_true", help="回 JSON 而不是人话")
    a = ap.parse_args(argv)

    code = ""
    if a.code_file:
        code = Path(a.code_file).read_text(encoding="utf-8")
    elif not a.prompt.strip():
        ap.error("要么给一句 prompt，要么 --code-file")

    got = asyncio.run(make_video(
        a.prompt, seconds=a.seconds, aspect=a.aspect, fps=a.fps, code=code,
        user_id=a.user_id, conversation_id=a.conversation_id,
        out_path=a.out or None, draft=a.draft))
    if a.json:
        print(json.dumps({k: v for k, v in got.items() if k != "code"},
                         ensure_ascii=False, indent=2))
    else:
        print(describe(got))
    return 0 if got.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
