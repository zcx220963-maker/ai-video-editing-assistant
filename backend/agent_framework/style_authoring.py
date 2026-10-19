# -*- coding: utf-8 -*-
"""从参考片沉淀风格模板：抽帧 → 强模型读片 → 写 TSX + manifest → 过闸 → 发布。

为什么要有这一件：模板库（`style_templates`）解决的是「模板存在哪里、怎么推给渲染容器」，
但模板从哪来——此前只有一条路，研发改 `remotion/src/lib/style/*.tsx` 再重建镜像。
要成为通用的视频生成 Agent，用户必须能**传一条参考片**，自己沉淀一套可复用的视觉语法；
后端不能再为每一种风格改代码。

流程（每一步都可单独测）：

1. **抽帧**：`ffprobe` 读时长/尺寸，按时间均匀取 5~7 帧（跳过片头片尾）。
2. **读片**：视觉模型看帧，输出一份结构化分析（manifest + 组件职责 + 风格正文）。
3. **写模板**：文本模型按分析写单文件 TSX，必须导出模板组件与 `TemplateProbe`。
4. **过闸**（全部失败只回清单、不吞）：
   * 模板 lint（`POST /lint-template`：有导出、禁引别的模板、禁随机/时钟/网络）；
   * 导出核对：分析里点名的每个组件都必须真的导出（呼应「正文点到的标识符
     必须在导出里能反查到」）；
   * 真渲三帧（`/render-stills`）——打不出包或画面空都能在这里看见；
   * 确定性：同码 `crf:1, concurrency:1` 渲两遍，必须逐字节相同。
5. **发布**：先在**临时名字**下过闸，成功后才写正式名字——重沉淀一版失败时，
   线上那份 validated 模板一个字节都不该被换掉。
6. **落库**：模板行（源码 + manifest + 组件 + 校验回执）与风格包（`skills(kind=style)`）
   同一次写入；用户在前端就能选到它。

边界：用户只走这条产品流程，不能直接上传 TSX；模板名由服务端按用户前缀分配
（`u-<8 位>-<slug>`），别人的模板既不覆盖也看不见。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import style_templates as st

#: 一条参考片看几帧。**不能只看「构图 / 光位 / 色彩」**：参考片的核心常常是
#: **时间上的语法**——同一个人一直在走、背景与服装逐段换。6 帧只够认出一套静态
#: 外观，看不出「什么在变」；真机 2026-10-19 沉淀出来的模板正是这样：背景会换、
#: 衣服永远穿同一件。12 帧覆盖到更多时代/镜头，才够把不变量与变化轴分开。
FRAME_COUNT = 12
#: 一次最多拿几条同风格参考片。再多也不能让视觉调用无限长：每条至少 4 帧，
#: 4 条已是 16 帧；「差不多风格」通常 2~3 条就能收敛。
MAX_REFERENCE_VIDEOS = 4
#: 多条参考片时总共看几帧（均分，每条至少 4 帧）。多给一点是因为要跨片找共同点，
#: 只在一条里出现的构图不能当成规则。
MULTI_FRAME_TOTAL = 12
#: 片头片尾各跳过 6%：参考片常有黑场、片尾卡。
_EDGE_SKIP = 0.06
#: 预览/闸用的合成时长。3 秒足够让「静止画面」与「有动效」都露出马脚。
PROBE_FPS = 30
PROBE_FRAMES = 90
#: 闸渲两遍的帧数：太短看不出推近/颗粒，太长只是多等。
DETERMINISM_FRAMES = 12
#: 一次沉淀最多重写几版。失败原因逐轮喂回去，不是同一句话重试三遍。
MAX_CODE_ATTEMPTS = 4
MAX_ANALYSIS_ATTEMPTS = 2
#: 读片输出是一份完整 JSON（prose + layers + timeline + style），4000 token 会在
#: JSON 中途截断：真机 2026-10-19 两条参考片都报 `Expecting ',' delimiter`
#: 于 char ~5100~5400，正是 4000 token 的截断点。提到 8000 后一次解析通过。
ANALYSIS_MAX_TOKENS = 8000
MAX_VARIANT_ATTEMPTS = 3
#: 换件输入：用户说的是一句话，不是槽位合约。120 字会把「怎么逐镜变」的后半句
#: 截掉，模型只能猜；这里留给一句完整的需求，解读阶段再压成画法。
MAX_VARIANT_REQUEST_CHARS = 1000
#: 解读后的画法上限。太长会稀释真正要改的那几笔。
MAX_VARIANT_SPEC_CHARS = 400


def _template_model() -> str:
    """写模板/写替换件这一档可以单独指定模型（`MOTION_TEMPLATE_MODEL`）。

    从零写一份满足 props 合约的 TSX，比逐镜写码难得多：弱模型会把必填 props
    写死、把人画成竖条。分析（要看参考帧）与保真评审仍用用户自己的模型；
    这里只覆盖**两次写代码**的调用，不改变用户配的 key/地址。
    """
    return os.getenv("MOTION_TEMPLATE_MODEL", "").strip()
#: 与渲染容器的模板上限同一口径（`server.mjs` 的 TEMPLATE_MAX_BYTES）。
MAX_SOURCE_BYTES = 200 * 1024
#: 名字：小写字母开头，字母数字与 `-`，2~41 位（渲染容器与 lint 同一口径）。
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
_COMPONENT_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,40}$")
_PROBE_EXPORT = "TemplateProbe"

#: 风格包正文写多长。太短读不出语法，太长会把逐镜提示词挤满。
PROSE_MIN_CHARS = 200
PROSE_MAX_CHARS = 4000

#: 允许模板 import 的东西（与 `remotion/lint-rules.mjs` 的 ALLOWED_IMPORTS 同源，
#: 这里只列模板用得上的那一部分；不在表里的 import 会在 lint 那一关被拒）。
TEMPLATE_IMPORTS = (
    "react", "remotion",
    "@remotion/paths", "@remotion/shapes", "@remotion/noise",
    "@remotion/animation-utils",
    "@dg/film", "@dg/tokens", "@dg/fonts",
    "@dg/lib/walk", "@dg/lib/scene", "@dg/lib/brush",
    "@dg/lib/draw/Figure", "@dg/lib/draw/Face", "@dg/lib/draw/Machine",
    "@dg/lib/draw/Building", "@dg/lib/draw/Vehicle", "@dg/lib/draw/Gear",
    "@dg/lib/draw/Chart", "@dg/lib/draw/Arrow", "@dg/lib/draw/Coin",
    "@dg/lib/draw/Book", "@dg/lib/draw/Scale", "@dg/lib/draw/Clock",
    "@dg/lib/draw/Crown", "@dg/themes/palettes.json",
)


class AuthoringError(RuntimeError):
    """沉淀失败。``detail`` 带给人看的原因清单，``attempts`` 记几次。"""

    def __init__(self, message: str, *, detail: Sequence[str] = (),
                 attempts: int = 0) -> None:
        super().__init__(message)
        self.detail = [str(d) for d in detail]
        self.attempts = attempts


# ---------------------------------------------------------------- 视频 → 帧


def probe_video(path: str | Path) -> dict[str, Any]:
    """ffprobe 读时长与画面尺寸。读不到就抛——没有时长就取不出帧。"""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        # Windows 上 ffprobe 的 stderr 可能带非 UTF-8 字节（文件路径/元数据），
        # 走 locale 编码（GBK）会直接抛 UnicodeDecodeError；显式按 UTF-8 容错解码。
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60)
    if proc.returncode != 0:
        raise AuthoringError(f"读不了这条视频（ffprobe 退出 {proc.returncode}）："
                             f"{(proc.stderr or '').strip()[:200]}")
    try:
        data = json.loads(proc.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError, IndexError) as exc:
        raise AuthoringError(f"ffprobe 输出读不懂：{exc}") from exc
    if duration <= 0 or not stream.get("width"):
        raise AuthoringError("这条视频没有可用的时长/尺寸，取不出参考帧。")
    return {"duration": duration, "width": int(stream["width"]),
            "height": int(stream["height"])}


def frame_counts(video_count: int) -> list[int]:
    """几条参考片各看几帧：一条看 12 帧，多条均分且每条至少 4 帧。"""
    n = max(1, int(video_count))
    if n == 1:
        return [FRAME_COUNT]
    per = max(4, MULTI_FRAME_TOTAL // n)
    counts = [per] * n
    rem = MULTI_FRAME_TOTAL - sum(counts)
    for i in range(n):
        if rem <= 0:
            break
        add = min(rem, FRAME_COUNT - counts[i])
        counts[i] += add
        rem -= add
    return counts


def pick_times(duration: float, count: int = FRAME_COUNT) -> list[float]:
    """在 [6%, 94%] 里均匀取 count 个时刻。片头黑场与片尾卡都不该当风格证据。"""
    if duration <= 0:
        return []
    count = max(1, min(int(count), 16))
    lo, hi = duration * _EDGE_SKIP, duration * (1 - _EDGE_SKIP)
    if hi <= lo:
        return [duration / 2]
    step = (hi - lo) / count
    return [round(lo + step * (i + 0.5), 3) for i in range(count)]


#: 场景分数阈值与最短镜长。等距取帧会把 12 帧平均摊在整条片上，遇到快切
#: （真机那条 188s 的片每几秒换一个主体）几乎必然漏掉一半镜头；按切点取，
#: 每一镜至少有一帧作为证据。
_SHOT_THRESHOLD = 0.30
_SHOT_MIN_SEC = 0.8


def detect_shots(video: str | Path, duration: float) -> list[tuple[float, float]]:
    """ffmpeg 场景分数 → ``[(start, end)]``，覆盖整条片（去掉片头片尾）。

    只用来决定**在哪取帧**。检测失败、没有切点、片长读不出来，都退回空列表，
    调用方按等距取帧处理——取帧不能因为这一环失败就整次沉淀失败。
    """
    if duration <= 0:
        return []
    cuts: list[float] = []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-v", "info",
             "-i", str(video), "-an",
             "-filter:v", f"select='gt(scene,{_SHOT_THRESHOLD})',showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300)
        for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)",
                            proc.stderr or ""):
            cuts.append(float(m.group(1)))
    except (OSError, subprocess.SubprocessError):
        return []
    lo, hi = duration * _EDGE_SKIP, duration * (1 - _EDGE_SKIP)
    if hi <= lo:
        return []
    bounds = [lo] + sorted({c for c in cuts if lo < c < hi}) + [hi]
    shots: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if not shots:
            shots.append((a, b))
        elif b - a < _SHOT_MIN_SEC:
            # 闪白/转场常被切成很短的一段；并进上一镜，别为它单独出一帧。
            shots[-1] = (shots[-1][0], b)
        else:
            shots.append((a, b))
    # 一条都没切出来（静态画面、长镜头、检测不可用）→ 当成「没有切点」，
    # 让调用方走等距取帧；整条片算作「一镜」只会取到一两个时刻。
    return shots if len(shots) > 1 else []


def pick_shot_times(duration: float, count: int = FRAME_COUNT,
                    shots: Sequence[tuple[float, float]] = ()
                    ) -> tuple[list[float], list[tuple[float, float]]]:
    """按镜取代表帧：镜数 ≤ 帧数时按镜长分配帧，镜数多时跨片均匀挑镜。

    「每镜一帧」只适用于镜数够多的片子；长镜头片（整条片只有两三镜）里
    那样会只取到四五个时刻。帧数不足 ``count`` 时按镜长加权补，让一镜
    8 分钟的片也能均匀看到中间的变化。
    返回 ``(times, shots)``；``shots`` 为空时退回 ``pick_times``，第二条恒为空。
    """
    count = max(1, min(int(count), 16))
    if not shots:
        return pick_times(duration, count), []
    picked: list[tuple[float, float]] = []
    if len(shots) <= count:
        picked = list(shots)
    else:
        if count == 1:
            picked = [shots[len(shots) // 2]]
        else:
            idx = [round(i * (len(shots) - 1) / (count - 1))
                   for i in range(count)]
            picked = [shots[i] for i in sorted(set(idx))]
    # 每镜至少一帧，剩下的名额给「当前每帧覆盖秒数最大」的那一镜，
    # 等价于按镜长比例分配；同一镜里取等距点，别全挤在切点附近。
    spans = [max(0.0, b - a) for a, b in picked]
    alloc = [1] * len(picked)
    for _ in range(count - len(alloc)):
        i = max(range(len(picked)),
                key=lambda j: spans[j] / max(1, alloc[j]))
        alloc[i] += 1
    times: list[float] = []
    for (a, b), n in zip(picked, alloc):
        for k in range(n):
            times.append(round(a + (b - a) * (k + 0.5) / n, 3))
    return sorted(set(times))[:count], list(shots)


def extract_frames(video: str | Path, out_dir: Path,
                   times: Sequence[float]) -> list[Path]:
    """按时刻各取一帧（缩到 1280 宽、JPEG）。至少 3 帧才继续。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for i, at in enumerate(times):
        dst = out_dir / f"ref-{i:02d}.jpg"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, float(at)):.3f}",
             "-i", str(video), "-frames:v", "1",
             "-vf", "scale=1280:-2", "-q:v", "3", "-y", str(dst)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 1000:
            frames.append(dst)
    if len(frames) < 3:
        raise AuthoringError(f"参考帧取不出来（只要到 {len(frames)} 帧，至少 3 帧）")
    return frames


# ---------------------------------------------------------------- 读片


ANALYSIS_SYSTEM = """你是视觉设计分析师。用户给出一条或几条参考视频的关键帧与镜头切点，
你要把它们的**视觉语法**写成一份能照着复现的拍摄说明书。

只输出一个 JSON 对象，不要代码围栏、不要解释。

**不要套任何现成的内容分类。** 下面的 JSON 只是信封：层有哪些、每层叫什么、
记哪些字段、字段名、时间线怎么分段、风格参数怎么命名，全部由你按这一条片现场决定。
两条不同的片子应该得到两套不同的结构。片里是什么就写什么——人、猫、鸟、鱼、车、
器物、图表、光块、文字……都可以；没有「主体必须是人」或「背景必须是景」这种前提。

信封：
{
  "display": "<这条片的中文名，≤12 字>",
  "description": "<一句话，≤60 字>",
  "prose": "<600~1200 字：构图、光、色、质感、运动、文字、禁止项；读完后另一个人能画出同一套画面>",
  "components": [
    {"name": "<PascalCase 组件名>", "role": "<这一件负责什么>"}
  ],
  "layers": [
    {"id": "<可选>",
     "name": "<这一层在这一条片里叫什么>",
     "component": "<PascalCase 组件名>",
     "kind": "<你定的层类，可空>",
     "default": "<不传参数时这一层画成什么样：形状、比例、位置、颜色、材质>",
     "fields": {"<你定的字段名>": "<这一条片里的取值>"},
     "fixed": ["<跨镜不变的画法>"],
     "varies": ["<逐镜可换的 prop 名>"],
     "evidence": "<t=…s>"}
  ],
  "timeline": [
    {"t": 0.0, "duration": 0.0,
     "camera": "<这一段怎么运镜>",
     "transition": "<到下一段怎么接>",
     "elements": [
       {"name": "<layers 里的层名>",
        "component": "<这一层对应的组件名，可空>",
        "fields": {"<你定的字段名>": "<这一段里这一层的取值/动作>"}}
     ]}
  ],
  "style": {"<你定的风格参数名>": "<取值>"},
  "responsible": "<模板负责哪些部分>"
}

规矩：
* 上面所有尖括号都是占位符：换成这条片里真实看到的名字与取值。示例没有预设任何
  主体、层名或字段名——不要照抄成「人」「背景」「主体」。层数也可以不同：一条片
  可能 3 层，另一条 12 层；一条片可能根本没有「背景」这一层。
* **`layers` 是画面拆解，不是摘要**：参考帧里看得见的每一层都拆出来。每层必须有
  `component` 与 `default`——`default` 是零参数时的完整画法，不是一句形容词。
  `fields` 写**能画出来的东西**：外形、比例、位置（0~1 画面比例）、颜色（#rrggbb）、
  朝向、动作、出现/消失方式。不要只写「很好看」「有质感」。
* **`timeline` 是复刻的核心**：按 `t`（秒）列出关键时刻，写清这一段里有哪几层、
  各自在做什么、镜头怎么动、怎么切到下一段。`t` 以图片前标的秒数与镜头切点为准，
  不要编时间。关键帧少时写 6~12 段即可；切点很多时可以合并成叙事节拍。
* `layers[].component` 必须出现在 `components` 清单里；名字用 PascalCase，
  后面会作为导出名。
* `style` 是**你自己命名的风格参数**：配色、质感、笔触、光位、字体、运动曲线、
  节奏、禁止项……键名随你；后端不解释，会原样交给写模板的人。
* 给了多条参考片时，提炼**共同语法**；只在一条里出现的构图/物件/文字位置不要写成
  全片规则。
* `prose` 写画面，不写剧情、不写流程。
* 如果你观察到跨镜持续的层（一直在运动的主体、贯穿全片的光、固定的文字带），在
  那一层的 `fields` 或 `style` 里写明「整片连续」与它的时钟/节奏——这决定逐镜
  切片能不能接上。
* 如果你还看出别的规律（节拍、声音、镜头语法、配色曲线……），可以自己加顶层块，
  后端会原样保存；但不要为了凑字段而写空话。
"""


def _image_part(path: Path) -> dict[str, Any]:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _frame_times(paths: Sequence[Path],
                 times: Sequence[float]) -> list[float | None]:
    """把 ``ref-07.jpg`` 映射回 ``times[7]``。

    取帧失败会跳过那个时刻，返回的帧列表**不能按下标 zip**：文件名里的序号
    才是它与 ``times`` 的对应关系。对不上的帧给 ``None``（不标时间）。
    """
    out: list[float | None] = []
    for path in paths:
        m = re.search(r"ref-(\d+)", path.stem)
        idx = int(m.group(1)) if m else -1
        out.append(float(times[idx]) if 0 <= idx < len(times) else None)
    return out


def _shot_hint(shots: Sequence[tuple[float, float]]) -> str:
    """镜头清单：切点比帧本身更能说明「一段有多长、在哪里换」。"""
    if not shots:
        return ""
    spans = "；".join(f"{a:.1f}–{b:.1f}s" for a, b in shots[:24])
    more = f" 等 {len(shots)} 镜" if len(shots) > 24 else ""
    return (f"\n  检测到 {len(shots)} 个镜头（{spans}{more}）。"
            "timeline 的分段优先按这些镜来写；帧是每镜的代表帧。")


def build_analysis_messages(frames: Sequence[Path], *, note: str = "",
                            prior_error: str = "", name_hint: str = "",
                            groups: Sequence[Sequence[Path]] | None = None,
                            times: Sequence[Sequence[float | None]] | None = None,
                            shot_groups: Sequence[Sequence[tuple[float, float]]]
                            | None = None) -> list[dict[str, Any]]:
    """帧 → 视觉分析请求。

    ``groups`` 给出时按「第 N 条参考片」分组说明；单条参考片走 ``frames``
    老路径。分组顺序就是图片发送顺序，两处必须同源。

    ``times`` 给每组帧在参考片里的秒数（与 ``groups`` 同形）。每张图前插一行
    ``t=…s``——**没有时间戳，模型只能写「什么在变」，写不出「第几秒出现、
    第几秒切、动多久」的时间线**；而复刻参考片真正要复用的是编排。
    """
    labels: list[float | None] = []
    if groups and len(groups) > 1:
        total = sum(len(g) for g in groups)
        text = (f"下面是 {len(groups)} 条风格相近的参考视频的关键帧，"
                f"共 {total} 帧；按参考片分组、组内按时间顺序，每张图前标了"
                "它在参考片里的秒数。"
                "请提炼它们共同的视觉语法，不要把只在某一条里出现的构图或物件"
                "写成全片规则。")
        ordered: list[Path] = []
        for i, group in enumerate(groups, 1):
            text += f"\n第 {i} 条参考片（{len(group)} 帧）："
            if shot_groups and i - 1 < len(shot_groups):
                text += _shot_hint(shot_groups[i - 1])
            ordered.extend(group)
            if times and i - 1 < len(times) and len(times[i - 1]) >= len(group):
                labels.extend(None if t is None else float(t)
                              for t in times[i - 1][:len(group)])
            else:
                labels.extend([None] * len(group))
    else:
        text = "下面是同一条参考视频的关键帧（按时间顺序），每张图前标了它在片中的秒数。"
        if shot_groups and shot_groups[0]:
            text += _shot_hint(shot_groups[0])
        ordered = list(frames)
        if times and len(times[0]) >= len(ordered):
            labels = [None if t is None else float(t) for t in times[0][:len(ordered)]]
        else:
            labels = [None] * len(ordered)
    if name_hint.strip():
        text += (f"\n用户给这套风格起的名字：{name_hint.strip()[:24]}"
                 "（display 优先用它；它超过 12 字时再压缩）")
    if note.strip():
        text += f"\n用户补充：{note.strip()[:500]}"
    if prior_error.strip():
        text += ("\n\n上一版分析没被接受，原因：\n" + prior_error.strip()[:1500]
                 + "\n请按同样格式重出一份完整的 JSON。"
                 "如果原因是输出被截断，请压缩 prose 与 evidence 的长度，"
                 "优先保证 JSON 完整。")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for i, path in enumerate(ordered):
        at = labels[i] if i < len(labels) else None
        if at is not None:
            content.append({"type": "text", "text": f"t={at:.2f}s"})
        content.append(_image_part(path))
    return [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {"role": "user", "content": content},
    ]


def _strip_fence(text: str) -> str:
    raw = str(text or "").strip()
    m = re.search(r"```(?:json|tsx|ts|jsx|js)?\s*\n(.*?)```", raw, re.S)
    return (m.group(1) if m else raw).strip()


#: `motion_contract` 的枚举。写错不静默留着一个模型看不懂的值：归一成缺省档。
_MOTION_SUBJECT = ("continuous", "stop_and_go", "static")
_MOTION_PHASE = ("global", "per_shot")
_MOTION_CUT = ("hard_cut", "dissolve", "match_cut")
_MOTION_CADENCE = ("per_shot", "continuous", "at_cut")


def _norm_invariants(value: Mapping[str, Any]) -> list[dict[str, str]]:
    invariants: list[dict[str, str]] = []
    for item in (value.get("invariants") or [])[:12]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:40]
        val = str(item.get("value") or "").strip()[:120]
        prop = str(item.get("prop") or "").strip()
        evidence = str(item.get("evidence") or "").strip()[:120]
        if not (name or val or prop):
            continue
        invariants.append({"name": name, "value": val, "prop": prop,
                           "evidence": evidence})
    return invariants


def _norm_variations(value: Mapping[str, Any], *, where: str) -> list[dict[str, Any]]:
    """变化轴。**没有 `prop` 就拒收**：说了会变却不说由谁驱动，等于没沉淀。"""
    variations: list[dict[str, Any]] = []
    for item in (value.get("variations") or [])[:12]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:40]
        prop = str(item.get("prop") or "").strip()
        if not prop:
            raise AuthoringError(
                f"{where}.variations 每一条都要有 `prop`——"
                "分析说这件东西会变，就必须点明模板组件用哪个 prop 驱动它"
                f"（这一条是「{name or '未命名'}」）")
        if not re.match(r"^[A-Za-z_]\w*$", prop):
            raise AuthoringError(f"variation 的 prop 名不合法：{prop}")
        values = [str(v).strip()[:60] for v in (item.get("values") or [])
                  if str(v).strip()][:8]
        cadence = str(item.get("cadence") or "").strip()
        variations.append({
            "name": name, "prop": prop, "values": values,
            "cadence": cadence if cadence in _MOTION_CADENCE else "per_shot",
            "evidence": str(item.get("evidence") or "").strip()[:120]})
    return variations


def norm_motion_contract(value: Any) -> dict[str, Any]:
    """参考片的「动」契约 → 可执行的形状（不变量 / 变化轴 / 连续性）。

    为什么单列一份：静态 manifest 只回答「长什么样」，回答不了「什么在变」。
    参考片的核心常常是**时间上的语法**——同一个人一直在走、背景和衣服逐段换。
    不把变化轴沉淀成 `prop`，模板只能把其中一档写死；镜头代码再想换也没有口子。

    宽松归一是刻意的：老分析没有这一节 → `{}`，行为与从前一致；枚举写错 →
    取缺省；但**一条 variation 没有 `prop` 就拒收**——说了会变却不说由谁驱动，
    等于没沉淀，喂回分析模型重出一份比静默丢掉更有用。
    """
    if not isinstance(value, Mapping):
        return {}
    invariants = _norm_invariants(value)
    variations = _norm_variations(value, where="motion_contract")
    continuity: dict[str, str] = {}
    raw_cont = value.get("continuity")
    if isinstance(raw_cont, Mapping):
        subject = str(raw_cont.get("subject_motion") or "").strip()
        phase = str(raw_cont.get("phase") or "").strip()
        cut = str(raw_cont.get("cut") or "").strip()
        if subject in _MOTION_SUBJECT:
            continuity["subject_motion"] = subject
        if phase in _MOTION_PHASE:
            continuity["phase"] = phase
        if cut in _MOTION_CUT:
            continuity["cut"] = cut
    out: dict[str, Any] = {}
    if invariants:
        out["invariants"] = invariants
    if variations:
        out["variations"] = variations
    if continuity:
        out["continuity"] = continuity
    return out


#: 沉淀的三个角度。参考片不是一张静帧：它有**怎么动**、**怎么好看**、**字怎么摆**。
#: 只沉淀静态 manifest 时，模板作者会把「动」和「字」都省掉，逐镜写手只能自己发挥——
#: 成片于是变成「背景换了、人停下/换脚/朝错方向、字幕每镜一个样」。
_FACET_NAMES = ("visual", "motion", "text")


def norm_facets(value: Any) -> dict[str, Any]:
    """`style_manifest.facets` → 三个角度的可执行契约（visual / motion / text）。

    形状故意扁：每个 facet 自己带 `components` / `invariants` / `variations`，
    motion 另带 `tracks`（film/shot 作用域），text 另带 `items`（组件 + 内容 prop）。
    这样沉淀期的闸和逐镜写手都只需要遍历同一份数据，不必为某套风格写分支。
    """
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for name in _FACET_NAMES:
        raw = value.get(name)
        if not isinstance(raw, Mapping):
            continue
        facet: dict[str, Any] = {}
        comps: list[str] = []
        for c in (raw.get("components") or [])[:12]:
            cname = str(c.get("name") if isinstance(c, Mapping) else c or "").strip()
            if cname and _COMPONENT_RE.match(cname) and cname not in comps:
                comps.append(cname)
        if comps:
            facet["components"] = comps
        invariants = _norm_invariants(raw)
        variations = _norm_variations(raw, where=f"facets.{name}")
        if invariants:
            facet["invariants"] = invariants
        if variations:
            facet["variations"] = variations
        if name == "motion":
            tracks: list[dict[str, Any]] = []
            for item in (raw.get("tracks") or [])[:8]:
                if not isinstance(item, Mapping):
                    continue
                comp = str(item.get("component") or "").strip()
                label = str(item.get("name") or comp).strip()[:40]
                if not (comp or label):
                    continue
                scope = str(item.get("scope") or "").strip()
                if scope not in ("film", "shot"):
                    scope = "shot"
                clock = str(item.get("clock") or "").strip()
                if scope == "film" and not clock:
                    clock = "filmTimeSec"
                track: dict[str, Any] = {
                    "name": label, "component": comp, "scope": scope,
                    "continuous": bool(item.get("continuous", scope == "film")),
                    "clock": clock,
                }
                props = item.get("props")
                if isinstance(props, Mapping):
                    track["props"] = {str(k)[:40]: str(v)[:40]
                                      for k, v in props.items() if str(k).strip()}
                tracks.append(track)
            if tracks:
                facet["tracks"] = tracks
            for key, allowed in (("subject_motion", _MOTION_SUBJECT),
                                 ("phase", _MOTION_PHASE), ("cut", _MOTION_CUT)):
                got = str(raw.get(key) or "").strip()
                if got in allowed:
                    facet[key] = got
            # 也认 `continuity` 包一层的写法（与 motion_contract 同形）。
            cont = raw.get("continuity")
            if isinstance(cont, Mapping):
                for key, allowed in (("subject_motion", _MOTION_SUBJECT),
                                     ("phase", _MOTION_PHASE), ("cut", _MOTION_CUT)):
                    got = str(cont.get(key) or "").strip()
                    if got in allowed and key not in facet:
                        facet[key] = got
            camera = str(raw.get("camera") or "").strip()[:120]
            if camera:
                facet["camera"] = camera
            transitions = [str(t).strip()[:40] for t in (raw.get("transitions") or [])
                           if str(t).strip()][:6]
            if transitions:
                facet["transitions"] = transitions
        if name == "text":
            items: list[dict[str, str]] = []
            for item in (raw.get("items") or [])[:8]:
                if not isinstance(item, Mapping):
                    continue
                kind = str(item.get("kind") or "").strip()[:20] or "caption"
                comp = str(item.get("component") or "").strip()
                prop = str(item.get("content_prop") or "").strip()
                if not (comp or prop):
                    continue
                if prop and not re.match(r"^[A-Za-z_]\w*$", prop):
                    raise AuthoringError(f"facets.text.items 的 content_prop 不合法：{prop}")
                items.append({
                    "kind": kind, "component": comp, "content_prop": prop,
                    "position": str(item.get("position") or "").strip()[:40],
                    "typography": str(item.get("typography") or "").strip()[:120],
                })
            if items:
                facet["items"] = items
        out[name] = facet
    return out


def _contract_from_facets(facets: Mapping[str, Any]) -> dict[str, Any]:
    """motion facet → 老的 `motion_contract` 形状，供还没认识 facets 的旧路径读。"""
    motion = facets.get("motion") if isinstance(facets, Mapping) else None
    if not isinstance(motion, Mapping):
        return {}
    out: dict[str, Any] = {}
    if motion.get("invariants"):
        out["invariants"] = motion["invariants"]
    if motion.get("variations"):
        out["variations"] = motion["variations"]
    cont = {k: motion[k] for k in ("subject_motion", "phase", "cut")
            if motion.get(k)}
    if cont:
        out["continuity"] = cont
    return out


#: `figure` 里每一档的允许区间。模型会写出离谱比例（身高 3.0、头半径 0.9），
#: 夹到画面里站得住的范围，比整份分析打回重出便宜；夹宽度见下方 `_num`。
_FIGURE_RANGES = {
    "head": {"r": (0.015, 0.14), "cx": (-0.25, 0.25), "cy": (0.65, 1.10)},
    "torso": {"shoulder": (0.04, 0.30), "waist": (0.02, 0.25),
              "top": (0.40, 0.95), "bottom": (0.30, 0.85)},
    "limb": {"arm": (0.015, 0.12), "leg": (0.02, 0.16)},
    "garment": {"hem": (0.0, 0.85), "halfWidth": (0.03, 0.35)},
}


def _norm_figure(value: Any) -> dict[str, Any]:
    """参考片里主体的画法。缺这一节就返回空 dict——老分析照旧成立。"""
    if not isinstance(value, Mapping) or value.get("present") is False:
        return {}

    def _num(raw: Any, lo: float, hi: float) -> float | None:
        if raw is None or isinstance(raw, bool):
            return None
        try:
            n = float(raw)
        except (TypeError, ValueError):
            return None
        if n != n:  # NaN
            return None
        return round(max(lo, min(hi, n)), 4)

    out: dict[str, Any] = {}
    head = value.get("head")
    if isinstance(head, Mapping):
        h: dict[str, Any] = {}
        shape = str(head.get("shape") or "").strip()[:24]
        if shape:
            h["shape"] = shape
        for key, (lo, hi) in _FIGURE_RANGES["head"].items():
            n = _num(head.get(key), lo, hi)
            if n is not None:
                h[key] = n
        if h:
            out["head"] = h
    torso = value.get("torso")
    if isinstance(torso, Mapping):
        t = {key: _num(torso.get(key), lo, hi)
             for key, (lo, hi) in _FIGURE_RANGES["torso"].items()}
        t = {k: v for k, v in t.items() if v is not None}
        if t:
            out["torso"] = t
    limb = value.get("limb")
    if isinstance(limb, Mapping):
        li = {key: _num(limb.get(key), lo, hi)
              for key, (lo, hi) in _FIGURE_RANGES["limb"].items()}
        li = {k: v for k, v in li.items() if v is not None}
        if li:
            out["limb"] = li
    garment = value.get("garment")
    if isinstance(garment, Mapping):
        kind = str(garment.get("kind") or "").strip()[:24]
        g: dict[str, Any] = {"kind": kind} if kind else {}
        for key, (lo, hi) in _FIGURE_RANGES["garment"].items():
            n = _num(garment.get(key), lo, hi)
            if n is not None:
                g[key] = n
        if isinstance(garment.get("slit"), bool):
            g["slit"] = garment["slit"]
        if g:
            out["garment"] = g
    for key in ("headwear", "gait", "evidence"):
        text = str(value.get(key) or "").strip()[:160]
        if text:
            out[key] = text
    carried = [str(c).strip()[:80] for c in (value.get("carried") or [])
               if str(c).strip()][:4]
    if carried:
        out["carried"] = carried
    # 只剩一句 evidence 不算沉淀出画法：至少要有头/躯干/四肢/衣服之一。
    if not any(k in out for k in ("head", "torso", "limb", "garment")):
        return {}
    return out


def _norm_free_fields(value: Any, *, limit: int = 16) -> dict[str, Any]:
    """LLM 自己定的字段表：只保形状与长度，**不改名字、不套枚举**。

    参考片不同，字段就该不同（这条片子可能只有「实体」「背景」，另一条会有
    「光源」「视差」「字」）。后端只负责把它原样传给写模板/写镜的人；在这里
    写死字段名，等于又把「所有视频长什么样」写死了一遍。
    """
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(value.items())[:limit]:
        key = str(k).strip()[:40]
        if not key:
            continue
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[key] = v
        elif isinstance(v, str):
            out[key] = v[:300]
        else:
            try:
                out[key] = json.dumps(v, ensure_ascii=False)[:500]
            except (TypeError, ValueError):
                out[key] = str(v)[:300]
    return out


def _norm_free_value(value: Any, *, depth: int = 0) -> Any:
    """LLM 自己定形状的风格参数：只截长度与层数，**不改键名、不删未知键**。

    这是整套沉淀里最不能「聪明」的一处。参考片不同，style 的键就该不同：
    一条片可能有 `配色/纸纹/笔触/运镜`，另一条可能有 `色温/颗粒/字幕带/节奏`。
    后端一旦在这里按白名单过滤，等于又把「所有视频长什么样」写死了一遍。
    """
    if depth > 6:
        return None
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:48]:
            key = str(k).strip()[:60]
            if not key:
                continue
            got = _norm_free_value(v, depth=depth + 1)
            if got not in (None, "", [], {}):
                out[key] = got
        return out
    if isinstance(value, (list, tuple)):
        items = [_norm_free_value(v, depth=depth + 1) for v in list(value)[:32]]
        return [v for v in items if v not in (None, "", [], {})]
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    return text[:600] if text else None


def _norm_layers(value: Any) -> list[dict[str, Any]]:
    """参考帧的画面拆解。每一层都必须落到一个模板组件，并写清缺省画法。

    这是「沉淀完整」的清单：逐镜写手只该覆盖这里标了 `varies` 的 prop，清单外的
    东西不该由他现场发明。缺 `component` / `default` 直接打回分析，不静默补空——
    一个没写缺省画法的层，等于模板不传参数时没有它。

    **层名、数量、字段由 LLM 在读片时自己定**（`fields` 原样保存，后端不解释）。
    `kind` 只作粗分类提示；它不在固定枚举里时保留原词，不强行归到 `other`。
    """
    if not isinstance(value, list) or not value:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(value[:12]):
        if not isinstance(item, Mapping):
            continue
        comp = str(item.get("component") or "").strip()
        if not comp or not _COMPONENT_RE.match(comp):
            raise AuthoringError(
                f"layers 第 {i + 1} 层没有合法的 component（PascalCase 组件名）——"
                "画面里每一层都要落到模板里的一个组件，不能只写一句描述")
        default = str(item.get("default") or "").strip()
        if not default:
            raise AuthoringError(
                f"layers 第 {i + 1} 层（{comp}）没有 default——"
                "模板不传参数时这一层画成什么样，必须写清（参考片里的默认画法）")
        lid = str(item.get("id") or "").strip()
        if not re.match(r"^[a-z][a-z0-9_]*$", lid):
            lid = f"layer_{i + 1}"
        while lid in seen:
            lid += "_x"
        seen.add(lid)
        kind = str(item.get("kind") or "").strip().lower()[:24]
        varies = [str(v).strip() for v in (item.get("varies") or [])
                  if re.match(r"^[A-Za-z_]\w*$", str(v).strip())][:8]
        fixed = [str(v).strip()[:60] for v in (item.get("fixed") or [])
                 if str(v).strip()][:8]
        geometry: dict[str, Any] = {}
        raw_geo = item.get("geometry")
        if isinstance(raw_geo, Mapping):
            for k, v in list(raw_geo.items())[:8]:
                if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                    continue
                geometry[str(k)[:24]] = round(v, 4) if isinstance(v, float) else v
        out.append({
            "id": lid,
            "kind": kind or "other",
            "name": str(item.get("name") or "").strip()[:60],
            "component": comp,
            "default": default[:240],
            "fields": _norm_free_fields(item.get("fields")),
            "fixed": fixed,
            "varies": varies,
            "geometry": geometry,
            "evidence": str(item.get("evidence") or "").strip()[:120],
        })
    return out


def norm_timeline(value: Any) -> list[dict[str, Any]]:
    """参考片时间线：**分段、层的名字、每层的字段都由读片模型自己定**。

    这里只做三件事：按时间排序、给缺省 `t`/`duration`、截断过长文本。
    不要求固定的字段名，也不要求每段都有同样的层——不同参考片本来就不一样。
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for seg in value[:32]:
        if not isinstance(seg, Mapping):
            continue
        try:
            t = float(seg.get("t", seg.get("start", 0)) or 0.0)
        except (TypeError, ValueError):
            t = 0.0
        try:
            dur = float(seg.get("duration") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        elements: list[dict[str, Any]] = []
        raw = seg.get("elements")
        if not isinstance(raw, list):
            raw = seg.get("layers") if isinstance(seg.get("layers"), list) else []
        for el in raw[:16]:
            if not isinstance(el, Mapping):
                continue
            name = str(el.get("name") or el.get("component") or "").strip()[:60]
            if not name:
                continue
            elements.append({
                "name": name,
                "component": str(el.get("component") or "").strip()[:40],
                "fields": _norm_free_fields(
                    el.get("fields") if isinstance(el.get("fields"), Mapping)
                    else el.get("state")),
            })
        if not elements:
            continue
        out.append({
            "t": round(max(0.0, t), 3),
            "duration": round(max(0.0, dur), 3),
            "camera": str(seg.get("camera") or "").strip()[:160],
            "transition": str(seg.get("transition") or "").strip()[:40],
            "elements": elements,
        })
    out.sort(key=lambda s: s["t"])
    return out


def parse_style_analysis(text: str, *,
                         require_layers: bool = False) -> dict[str, Any]:
    """模型输出 → 规范化分析。坏形状抛 ``AuthoringError``（原因会喂回下一轮）。

    ``require_layers`` 只给新沉淀用：没有画面拆解的分析照样能解析（老沉淀不被打死），
    但新沉淀必须交清单，否则「模板默认完整」无从核对。
    """
    try:
        obj = json.loads(_strip_fence(text))
    except ValueError as exc:
        raise AuthoringError(f"分析不是合法 JSON：{exc}") from exc
    if not isinstance(obj, Mapping):
        raise AuthoringError("分析必须是一个 JSON 对象")

    display = str(obj.get("display") or "").strip()[:24]
    description = str(obj.get("description") or "").strip()[:120]
    prose = str(obj.get("prose") or "").strip()
    responsible = str(obj.get("responsible") or "").strip()[:120]
    if not display:
        raise AuthoringError("分析缺 display（中文名）")
    if len(prose) < PROSE_MIN_CHARS:
        raise AuthoringError(
            f"风格正文太短（{len(prose)} 字，至少要 {PROSE_MIN_CHARS} 字）："
            "它要能让另一个人画出同一套画面。")
    prose = prose[:PROSE_MAX_CHARS]

    comps: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in obj.get("components") or []:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            role = str(item.get("role") or "").strip()[:60]
        else:
            name, role = str(item or "").strip(), ""
        if not name or name == _PROBE_EXPORT or name in seen:
            continue
        if not _COMPONENT_RE.match(name):
            raise AuthoringError(f"组件名不合法：{name}（要 PascalCase，字母数字）")
        seen.add(name)
        comps.append({"name": name, "role": role})
    # 风格参数：键名与形状由 LLM 定。新分析写 `style`；旧沉淀写 `style_manifest`，
    # 两者都按自由对象收，**不按白名单过滤**。没有这一个块也能继续——正文、layers
    # 与参考帧已经足够让写模板的人动笔。
    raw_style = obj.get("style")
    if not isinstance(raw_style, Mapping):
        raw_style = obj.get("style_manifest")
    if not isinstance(raw_style, Mapping):
        raw_style = {}
    # 旧沉淀的固定形状先摘出来交给各自的规范化器：通用截断会把 facets 里
    # 深一层的 variations/tracks/items 削空，而这些正是沉淀闸要读的。
    _LEGACY_KEYS = ("figure", "facets", "motion_contract", "layers", "timeline")
    legacy_raw = {k: raw_style.get(k) for k in _LEGACY_KEYS}
    clean = _norm_free_value({k: v for k, v in raw_style.items()
                              if k not in legacy_raw})
    if not isinstance(clean, dict):
        clean = {}

    # 旧沉淀的形状兼容读取：有就规范化后保留，没有就跳过。新分析不再要求它们。
    figure = _norm_figure(legacy_raw["figure"])
    if figure:
        clean["figure"] = figure
    facets = norm_facets(legacy_raw["facets"])
    if facets:
        for fname in _FACET_NAMES:
            for cname in (facets.get(fname) or {}).get("components", []):
                if cname in seen or cname == _PROBE_EXPORT:
                    continue
                seen.add(cname)
                comps.append({"name": cname, "role": f"{fname} 部件"})
        clean["facets"] = facets
    raw_contract = legacy_raw["motion_contract"]
    contract = _contract_from_facets(facets) or norm_motion_contract(raw_contract)
    if contract:
        clean["motion_contract"] = contract

    # 画面拆解：每一层都要有组件与缺省画法。层里点到、但没进顶层清单的组件也并进来，
    # 否则「这一层没被模板覆盖」要等逐镜写手自己补画才发现。
    layers = _norm_layers(obj.get("layers") or legacy_raw["layers"])
    if require_layers and not layers:
        raise AuthoringError(
            "分析缺 layers：把参考帧里看得见的每一层都拆出来，每层给 component 与 "
            "default——没有这份清单，模板漏画哪一层都没人发现")
    if layers:
        for layer in layers:
            cname = layer["component"]
            if cname in seen or cname == _PROBE_EXPORT:
                continue
            seen.add(cname)
            comps.append({"name": cname,
                          "role": (layer["name"] or layer["kind"])[:30] + " 图层"})

    # 参考片时间线：读片模型自己分段、自己给每层起名字与字段。存进 manifest，
    # 写模板与逐镜写手都按这份**数据**复刻编排；后端不解释字段，也不要求固定键。
    timeline = norm_timeline(obj.get("timeline") or legacy_raw["timeline"])
    if timeline:
        clean["timeline"] = timeline

    # 顶层自由块：LLM 自己加的观察（可能是 `beats`、`sound`、`color_script`……）。
    # 后端不认识它们，但必须原样带下去——删掉就是把模型多看出来的一层扔了。
    known_top = {"display", "description", "prose", "responsible",
                 "components", "layers", "timeline", "style", "style_manifest"}
    blocks: dict[str, Any] = {}
    for key, value in obj.items():
        if key in known_top:
            continue
        got = _norm_free_value(value)
        if got not in (None, "", [], {}):
            blocks[str(key).strip()[:40]] = got

    if not comps:
        raise AuthoringError(
            "分析没有任何可导出的组件：每层至少给一个 component（PascalCase）")
    if len(comps) > 16:
        raise AuthoringError(
            f"组件清单最多 16 个，实际 {len(comps)} 个")

    result = {"display": display, "description": description, "prose": prose,
              "responsible": responsible, "components": comps, "manifest": clean}
    if layers:
        result["layers"] = layers
    if timeline:
        # manifest 里也留了一份（模板作者与逐镜写手都从 manifest 读），
        # 顶层再放一份，给 payload/publish 用。
        result["timeline"] = timeline
    if blocks:
        result["blocks"] = blocks
    return result


# ---------------------------------------------------------------- 写模板


CODE_SYSTEM = """你在为一个 React + Remotion 的视频项目写一个**风格模板文件**（单文件 TSX）。
这个文件会被渲染容器按模块名 import，供后续每一镜复用同一套视觉。

硬约束：
* 只输出 TSX 源码本身，不要代码围栏、不要解释。
* 只能 import 这些模块：{imports}。
* 只画矢量/代码图形：不要用图片、外部 URL、字体文件、素材文件。
* 禁：Math.random / Date.now / new Date / performance / setTimeout / setInterval /
  requestAnimationFrame / eval / new Function / fetch / XMLHttpRequest / localStorage。
  所有变化都必须是 useCurrentFrame() 或 props 的确定性函数；同一份代码渲两遍必须逐像素相同。
* 不要 import 任何 `@dg/lib/style/...`（模板之间不能互相依赖）。
* 画面里的坐标用成片设备像素；画面尺寸从 `useVideoConfig()` 取。但**组件对外的 props**
  里，凡是表示位置/尺寸的（x、y、groundY、height/heightRatio、width、size、pan 之类）
  一律用**画面比例 0~1**，在组件内部再乘 width/height。逐镜写代码的人拿到的是比例，
  传像素会把主体推出画外；缺省值也必须写成比例，与分析里的位置/尺寸字段同口径。
* **跨镜会变的东西必须做成 props，不能写死在组件里**。但**字段名与取值由分析里的
  `layers[].fields` / `timeline` 决定，不是固定枚举**：分析给了哪些变化轴，就暴露
  哪些 prop；分析没给的主体/服饰/道具内容，不要自己发明一套 `wardrobe` 档位。
  如果确实保留了一个参考主体组件，它只提供**一种默认画法**；逐镜写手会按 brief
  自己画内容，或传它声明过的 prop。不要要求逐镜从固定字面量里选。
* **模板负责风格骨架与运动机制，内容归逐镜代码**：舞台分层、相机/视差、光池、
  质感、字幕版式这些跨镜不变的东西做成组件；主体、服饰、道具、背景里属于这一镜
  内容的部分，不要把「全世界的事物」枚举进模板。分析里的 `fields` 是这一条参考片
  的观察，不是内容清单。
* 分析里有 `timeline` 时，按它的段落与 `fields` 实现运动：`t` 是参考片秒数，
  `camera`/`transition` 做成组件参数或导出常量；各层的动作做成接收 `clock` 的
  确定性函数。复刻的是「什么先出现、怎么动、何时切」，不是把内容写死。
  **不要把参考片的总时长写死进组件**：timeline 用来定运动语法与段落结构，
  逐镜写手会把它缩放到本镜的时长。
* 如果你确实给某个内容型 prop 做了多档画法，每一档都要改到轮廓本身（不能只换颜色
  或标签），并给未知取值留一个明确的兜底；但优先做成逐镜可自己画的机制，而不是
  把所有档位写进模板。
* **每一层怎么画，由它的 `default` 与 `fields` 决定，不要套现成模板。**
  分析里的层名、字段名、取值都是这一条片的观察：可能是一块表盘、一只鸟、一条鱼、
  一张图表、一束光、一行字。按 `default` 里的形状/比例/位置/颜色画；`fields` 给了
  更多细节就用上。**没有「默认身材」「默认背景」这种兜底——参考帧里是什么就画什么。**
  某一层如果确实是直立人形，参考帧会附上，轮廓/比例以帧为准；但不要因此把别的层
  也套成人形。
* 跨镜会变的字段（`layers[].varies` 或 `fields` 里写明「逐镜/随…变」的）要变成
  该层组件的**可选 prop**（`?:` + 缺省值），逐镜写手按自己的内容传。`fields` 里
  其余键是参考片这一条的观察，不必逐条变成 prop，但如果它是画法的一部分，
  就把它落到默认值或常量里。
* **模板默认必须完整：零参数就要渲出参考片的整帧。** 用户提示词可能只有一句
  「做一个关于咖啡历史的视频」，逐镜写手也可能什么都不传：
  - `layers` 里每一层都要由它对应的组件**默认画出来**（用该层的 `default`）；
  - 所有 props 都是**可选覆盖**（`?:` + 缺省值），不要设必填的内容参数；
    字幕/铭文的 `text` 也给缺省值（参考片里那一行），逐镜再覆盖；
  - 变化轴（`varies`）做成 prop，但**不传就用参考片的缺省档**；
  - `{probe}` 必须不接收任何 props 就能渲出整帧——预览帧与保真评审只看它。
  不要默认「逐镜写手会自己补画主体/光块/背景」：他补出来的就是通用件，
  整片会变回「背景在换、主体永远是同一个」。
* **新分析没有 `facets`，别去找它**：这一版沉淀的主体是 `layers` + `timeline` +
  `style`，按上面两条把它们落成组件。旧沉淀如果带 `facets`，就按它实现：
  - `facets.visual`：配色/构图/质感/光位。`variations` 每档给一个 prop，
    `invariants` 写成固定值或固定比例。
  - `facets.motion`：按 `tracks` 实现。`scope: "film"` 的轨道是**整片连续的一层**
    ——组件必须接收 `clock`（缺省 `filmTimeSec`，单位秒）并用它算运动，**不要**用
    `useCurrentFrame()` 从 0 重算；`scope: "shot"` 的轨道从本镜 0 帧算起。
  - `facets.text`：`items` 里每个 `component` 都要导出，并有 `content_prop` 指定的
    内容 prop——示例文字不能写死，逐镜把字幕/铭文传进来。
  - facet 里所有 `prop` 都必须在某个导出组件的 props 类型里存在并真的被使用。
* **不要给 `<div>` 写 `background` / `backgroundColor`**，包括半透明遮罩与局部色块。
  lint 会逐行拦，拦一次就整份重写。写法对照：
  - ✗ `<div style={{{{position: 'absolute', inset: 0, backgroundColor: '#000'}}}} />`
  - ✗ `<div style={{{{background: PALETTE.ground}}}} />`
  - ✓ `<svg width={{width}} height={{height}}><rect width={{width}} height={{height}}
    fill={{PALETTE.ground}} /></svg>`
  - ✓ `<AbsoluteFill style={{{{backgroundImage: `linear-gradient(180deg, ${{a}} 0%,
    ${{b}} 100%)`}}}} />`（渐变可以，纯色不行）
  所有纯色填充都走 SVG `<rect>`/`<circle>`/`<path>` 的 `fill`。颜色取分析里
  你自己写的颜色字段（可能是 `palette`，也可能叫别的名字）或参考帧里看到的色；
  分析没给颜色才用 `@dg/themes/palettes.json` 兜底。

必须导出：
1. `{probe}`：无 props 的自包含预览组件。它必须把 `layers` 里的**每一层**都画上，
   渲出参考片的完整默认画面（填满整帧，用 useCurrentFrame() 让静止/动效都成立）。
   预览帧、保真评审与过闸都只看它——少画一层，这里就露出来。
2. 下面每一个组件名都要作为具名导出存在：
{components}

可用零件（**签名按这里写，不要猜**；下面没列全的 prop 可省略）：
* `@dg/lib/walk`：`WalkingFigure`（x, footY, height, facing, phase, strideSec,
  walking, wardrobe, render, fill, rim, shadow）。**只在参考片里确实有一层是
  直立行走进出画面的人形时才可用**；主体是猫、鸟、鱼、车、器物、光块时不要拿它
  兜底——按那一层的 `default` / `fields` 自己画。即便用了它，也只当基础骨架，
  外形按参考帧调整。
* `@dg/lib/scene`：
  - `Scene`（far / mid / near / horizon / haze / bg）
  - `Horizon`（y, color, thickness, opacity, seed）
  - `Particles`（count, seed, color, size, opacity, rise, **area**）。
    `area` 是**四元组** `[x0, y0, x1, y1]`，不是对象：
    ✓ `area={{[0, 0, width, groundY * height]}}`
    ✗ `area={{{{x: 0, y: 0, w: width, h: gy}}}}`（运行时会抛
    `object is not iterable`，整条沉淀直接 500）
* `@dg/lib/brush`：
  - `Grain` / `PaperTexture`（opacity, scale **像素**，缺省 180；scale=1 几乎看不见）
  - `Vignette`（strength, inner **像素**, color；inner 是暗角开始半径，1080p 约 620，
    不要传 0.5 这类 0~1 比例——那会把整帧压暗）
  - `LightPool`（**x, y 必填**，radius, color, opacity, blend）
  - `SoftShadow`（**x, y 必填**，rx, ry, color, opacity）
  - `BrushStroke`、`Wash`（d 必填，color, opacity, seed, bleed）
* `@dg/film`：`CaptionText`（text, fitWidth, color, fontSize）、
  `resolveFilmStyle(getInputProps())`
* `@dg/tokens`：`TOKENS`（纸/墨等基础色）
* `remotion`：`AbsoluteFill`、`interpolate`、`spring`、`useCurrentFrame`、`useVideoConfig`

注意组件分层：`Particles` / `WalkingFigure` 自带 `<svg>`，`LightPool` / `Vignette` /
`Grain` 铺满整帧，这几种直接放在 `<AbsoluteFill>` 下即可；`Wash` / `SoftShadow` 画的是
SVG 子元素，**必须放在你自己的 `<svg>` 里**（推荐再包一层 `<g transform=…>` 做画布变换），
不要直接挂在 `<div>` 下。

* **公共导出组件必须自包含**：返回 `<AbsoluteFill>`（里面放你的 `<svg>`）或直接返回
  `<svg>`；**不要返回裸 `<g>`**。裸 `<g>` 挂在 `<AbsoluteFill>`（一个 div）下不会画出来，
  静态闸会拦。只有不导出的私有辅助组件可以返回 `<g>`，并且必须被公共组件的 `<svg>` 包住。
* **JSX 属性字符串不解析转义**：`text="第一行\\n第二行"` 会原样显示反斜杠 + n。
  多行文案写成 `text={{'第一行\\n第二行'}}`；组件内部按 `String(text).split(/\\n|\\\\n/)`
  拆行，兼容逐镜代码用属性字符串传进来的字面 `\\n`。

写法要求：组件通过 props 暴露可调项（位置/尺寸/颜色/文案），缺省值取分析里的 manifest；
不要在模块顶层读帧号，不要在渲染期做 I/O。
"""


def _sample_frames(frames: Sequence[Path], limit: int = 4) -> list[Path]:
    """从参考帧里等距抽几张给写模板的人看。全发会把预算烧在重复画面上。"""
    items = list(frames)
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)]


def _required_props(analysis: Mapping[str, Any]) -> list[str]:
    """从分析里算出模板**必须存在**的 props，写进第一版请求。

    沉淀闸（`check_motion_contract` / `check_facets`）本来就会查这些，但只在
    写完一版之后才查：弱模型第一版常把 `wardrobe` / `phase` / `text` 写死，
    白烧一轮写码。把同一份数据提前摊开，第一版就有机会全带上。
    """
    manifest = analysis.get("manifest") if isinstance(analysis, Mapping) else None
    manifest = manifest if isinstance(manifest, Mapping) else {}
    contract = manifest.get("motion_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    facets = manifest.get("facets")
    facets = facets if isinstance(facets, Mapping) else {}

    assigned: dict[str, list[str]] = {}

    def add(comp: Any, prop: Any) -> None:
        name = str(prop or "").strip()
        if not name:
            return
        who = str(comp or "").strip() or "（任一导出组件）"
        bucket = assigned.setdefault(who, [])
        if name not in bucket:
            bucket.append(name)

    for layer in analysis.get("layers") or []:
        if isinstance(layer, Mapping):
            for prop in layer.get("varies") or []:
                add(layer.get("component"), prop)
    for item in contract.get("variations") or []:
        if isinstance(item, Mapping):
            add("", item.get("prop"))
    for fname in _FACET_NAMES:
        facet = facets.get(fname)
        if not isinstance(facet, Mapping):
            continue
        for item in facet.get("variations") or []:
            if isinstance(item, Mapping):
                add("", item.get("prop"))
    text = facets.get("text")
    if isinstance(text, Mapping):
        for item in text.get("items") or []:
            if isinstance(item, Mapping):
                add(item.get("component"), item.get("content_prop"))
    motion = facets.get("motion")
    if isinstance(motion, Mapping):
        for track in motion.get("tracks") or []:
            if not isinstance(track, Mapping):
                continue
            if track.get("scope") == "film" and track.get("continuous"):
                add(track.get("component"), track.get("clock") or "filmTimeSec")
    continuity = contract.get("continuity")
    if isinstance(continuity, Mapping) \
            and continuity.get("subject_motion") == "continuous":
        add("", "phase")

    return [f"- `{who}`：" + "、".join(f"`{p}`" for p in props)
            for who, props in assigned.items()]


def build_code_messages(analysis: Mapping[str, Any], *, module: str,
                        prior_errors: Sequence[str] = (),
                        frames: Sequence[Path] = ()) -> list[dict[str, Any]]:
    comps = "\n".join(f"- `{c['name']}`：{c['role'] or '（无说明）'}"
                      for c in analysis.get("components") or [])
    system = CODE_SYSTEM.format(
        imports=", ".join(f"`{m}`" for m in TEMPLATE_IMPORTS),
        probe=_PROBE_EXPORT, components=comps)
    required = _required_props(analysis)
    user = (
        f"模板名：`{module}`\n"
        f"显示名：{analysis.get('display')}\n"
        f"一句话：{analysis.get('description')}\n"
        f"模板负责：{analysis.get('responsible')}\n\n"
        "视觉规范（manifest，逐条落到代码里）：\n"
        + json.dumps(analysis.get("manifest") or {}, ensure_ascii=False, indent=2)
        + ("\n\n画面拆解（每一层都要有默认实现；不传参数就画成 default 描述的样子）：\n"
           + json.dumps(analysis.get("layers") or [], ensure_ascii=False, indent=2)
           if analysis.get("layers") else "")
        + ("\n\n时间线（什么先出现、怎么动、何时切；`t` 是参考片秒数。"
           "把它做成接收整片时钟的确定性函数，不要把内容写死）：\n"
           + json.dumps(analysis.get("timeline") or [], ensure_ascii=False, indent=2)
           if analysis.get("timeline") else "")
        + ("\n\n读片时另外记下的观察块（键名是读片模型自己定的；"
           "对画法有用的就落进代码）：\n"
           + json.dumps(analysis.get("blocks") or {}, ensure_ascii=False, indent=2)
           if analysis.get("blocks") else "")
        + (
            "\n\n必须存在的 props（参考片里会变的维度都要有出口；"
            "缺一个，模板就把那一档写死了）：\n"
            + "\n".join(required)
            if required else "")
        + "\n\n风格正文：\n" + str(analysis.get("prose") or "")
    )
    if prior_errors:
        user += ("\n\n上一版没通过，逐条处理以下问题后重写整份文件：\n"
                 + "\n".join(f"- {e}" for e in list(prior_errors)[:12]))
    picked = _sample_frames(frames)
    if picked:
        user += ("\n\n下面附上参考帧（等距抽样）。各层的轮廓、外形、比例与运动"
                 "以这些帧为准；分析里的 `layers` / `timeline` / 颜色是从它们看出来的，"
                 "两者冲突时以帧为准。帧里是什么就画什么——不一定是人。")
        content: Any = [{"type": "text", "text": user},
                        *(_image_part(p) for p in picked)]
    else:
        content = user
    return [{"role": "system", "content": system},
            {"role": "user", "content": content}]


FIDELITY_SYSTEM = """你是风格复刻验收员。用户给你参考片的若干关键帧，以及一份风格模板
**不传任何参数**渲出的预览帧。你要逐层核对模板是否复刻了参考片的视觉语法。

只输出一个 JSON 对象，不要代码围栏、不要解释：
{"ok": true/false, "missing": ["缺了什么"], "mismatched": ["哪一层怎么不对"], "notes": "一句话"}

判据：
* 参考帧里看得见的每一层，渲染帧里都要有：主体、光源、远景、地面/近景、质感、文字、
  携带物。少一层就是 missing，写清是哪一层。
* 结构、比例、位置要对：按分析里的 `layers` 逐层核对轮廓、比例（占画面多少）、
  位置、颜色、光位与文字排版。差得明显就是 mismatched，写清「参考是什么、
  渲染成了什么」。分析里没有的层，渲染帧里多出来也算 mismatched。
* **不要把参考片的内容当成要求**：具体文字、年代、地点、时代背景可以不同；但它们的
  位置、字号、颜色与排版要对。
* 刻意重复的构图不是缺点；参考片里每镜同一构图时，渲染帧也应当同一构图。
* 每条 ≤60 字，最多 8 条。`ok` 只在 missing 与 mismatched 都为空时为 true。
"""


def _b64_part(jpeg_b64: str) -> dict[str, Any]:
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}}


def build_fidelity_messages(analysis: Mapping[str, Any],
                            frames: Sequence[Path],
                            stills: Sequence[Mapping[str, Any]]
                            ) -> list[dict[str, Any]]:
    """参考帧 + 零参数渲染帧 → 保真评审请求。只有看图才能发现「人不对」。"""
    layers = analysis.get("layers") or []
    text = ("下面是参考片的关键帧，以及模板**不传任何参数**渲出的预览帧。\\n"
            "先逐层核对画面拆解，再给结论。\\n\\n画面拆解：\\n"
            + json.dumps(layers, ensure_ascii=False, indent=2))
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for i, path in enumerate(_sample_frames(frames, 4), 1):
        content.append({"type": "text", "text": f"参考帧 {i}"})
        content.append(_image_part(path))
    for i, still in enumerate(list(stills)[:3], 1):
        b64 = str(still.get("jpeg_b64") or "")
        if b64:
            content.append({"type": "text", "text": f"模板渲染帧 {i}"})
            content.append(_b64_part(b64))
    return [{"role": "system", "content": FIDELITY_SYSTEM},
            {"role": "user", "content": content}]


def parse_fidelity(text: str) -> tuple[bool, list[str]]:
    """评审输出 → (是否通过, 差异清单)。坏形状抛 ``AuthoringError`` 由调用方跳过。"""
    try:
        obj = json.loads(_strip_fence(text))
    except ValueError as exc:
        raise AuthoringError(f"保真评审不是合法 JSON：{exc}") from exc
    if not isinstance(obj, Mapping):
        raise AuthoringError("保真评审必须是一个 JSON 对象")
    diffs: list[str] = []
    for key in ("missing", "mismatched"):
        for item in obj.get(key) or []:
            text_item = str(item).strip()[:120]
            if text_item:
                diffs.append(text_item)
    ok = bool(obj.get("ok")) and not diffs
    if not ok and not diffs:
        diffs = ["模板渲染帧与参考帧存在结构性差异（评审未逐条列出）"]
    return ok, diffs[:8]


# ---------------------------------------------------------------- 槽位替换件
#
# 沉淀出的模板是一组导出组件；**每个组件都是一个槽位**。外壳（构图、相机、时间轴、
# 字幕/铭文版式、配色规则）不动，把其中一个组件换成另一份满足同一 props 合约的
# 实现，就是「整体还是那套模板，只换一个零件」。替换件自己也是过闸后的模板行，
# 只是登记在基模板的 `manifest.variants` 下，供运行时按槽位绑定。


VARIANT_SYSTEM = """你是风格模板的「替换件」作者。模板的外壳（构图、相机、时间轴、
字幕/铭文版式、配色规则）已经存在，你只写**一个组件**去替换其中一个零件；
整体仍然是原来那套模板，不是另起一套视觉。

只输出一份完整 TSX 源码，不要解释；可以用一个 ```tsx 围栏。
硬要求：
* 导出名保持模板给的那个组件名，不要改名。
* props 名称必须覆盖槽位合约里的全部字段，并且**全部可选**（`p?:` 且给缺省值）
  ——模板可能一个参数都不传，零参数就要画出这个零件。
* 用户要的分档比合约里现有的取值更多时（更多时代/场景/形态），可以在对应 prop 的
  联合类型里**增加取值**：原有的取值一个都不能删、不能改名，新取值要和原有取值
  属于同一类东西（比如同一套场景枚举）。每个新取值都要画成一眼能区分的样子。
  类型只增不减，模板照旧能传原来的值。
* **内容型取值必须改到轮廓本身**：换一个取值就要换外形（头形、衣摆、持物、
  整体轮廓），不能只换颜色、只换配饰，或只把标签改掉。参考片里的具体物件只
  属于参考片对应的那一档，不要做成所有取值共用的固定部件。
* **侧脸的人，帽子和兜帽不要包住整颗头**：闭合的圆顶、或横贯整个脑袋的长檐，
  在纯黑剪影里读起来是「戴头盔的士兵」，不是连帽衫/棒球帽。兜帽只盖后脑与
  头顶后半，前额与脸这一侧留出头形；帽檐只在前额探出一小截。袍/铠这类头饰
  可以盖住头，但轮廓要能看出是软的兜帽，不是硬的盔。
* 位置/尺寸参数沿用模板的单位：**画面比例 0–1**，不是像素；颜色一律 `#rrggbb`。
* 保持参考模板的画法与配色（剪影/平涂/线条、墨色与强调色、光的处理），
  只换对象本身，不换整片风格。
* 只用白名单里的 import；**不要 import 基模板模块或原组件**，替换件必须自足；
  不要外部图片/URL/字体；不要 `Math.random` / `Date.now`。
* 不要画第二个人、第二块光、字幕或铭文——那些是别的槽位的事。
"""


#: 用户只会说人话（「第一镜为什么是个戴头盔的战士」），不会说槽位合约
#: （「wardrobe=cloak 画成兽皮斗篷」）。没有这一步，换件输入框等于要求用户
#: 先学会模板的 props——正常用户弄不好也看不懂。这里只做解读，不做闸：
#: 解读失败就按原话写，换件照常进行。
VARIANT_INTENT_SYSTEM = """你是风格模板的「换件需求解读器」。用户只给一句自然语言，
你要判断他想换模板里的哪个零件、这个零件在不同镜头里应该怎么变，然后给出一份
可以直接照着画的要求。只输出一个 JSON 对象，不要解释，不要代码。

字段：
{
  "component": "必须原样取槽位清单里的一个组件名",
  "label": "≤16 个汉字，给用户看的短名",
  "mode": "fixed" 或 "by_props",
  "spec": "≤400 字的画法要求",
  "props": ["依赖的 prop 名"],
  "note": "一句话说明你的判断"
}

规则：
* 用户的话指向哪个零件就选哪个；说不清时选画面主体。提示里已选的零件只作参考，
  用户的话明显指向另一个零件时以用户的话为准。
* 如果用户要求「每一镜 / 不同时代 / 随内容变化」，就是 by_props：必须从该组件的
  props 合约里挑一个能表达变化的 prop，并按它的**合法取值**逐档写画法
  （每个取值都要画成一眼能区分的样子）。不要发明 prop 名。
* 现有取值不够表达用户要的所有档位时（比如只有「山峦/城市/战场」，用户还要
  室内、街机厅、手机屏幕），用同一个 prop，在 spec 里**新增取值**并逐个写清
  画法；替换件会把新取值加进联合类型。原有取值不要删改。
* 如果合约里完全没有能表达这种变化的 prop，就用 fixed，并在 note 里说明
  「当前参数无法逐镜分档」——不要硬编一个不存在的参数。
* 只写这个零件画的内容与必要画法：轮廓、比例、姿态、持物、明暗、材质。
  不要写代码、不要建议改外壳、不要画第二个主体/第二块光/字幕。
* 沿用基模板自己的画法与配色（以槽位清单里写的默认画法为准），沿用画面比例单位。
* 用户的原话里可能有抱怨或疑问（「为什么是戴头盔的战士」），把它翻译成
  要避免什么、要画成什么，不要复述情绪。
"""


#: 「换件」面板的候选选项：用户不知道怎么描述时，点一句就能填进输入框。
#: 选项跟着模板自己的槽位清单生成，不预设某一种风格或某一种零件；点选后
#: 仍会经过 `VARIANT_INTENT_SYSTEM` 那一步压成画法，所以选项写得口语一点没关系。
VARIANT_OPTIONS_SYSTEM = """你是风格模板的「换件选项建议器」。用户点中了模板里的一个零件，
你要给 4~6 条可以直接点选的换件选项。只输出一个 JSON 对象，不要解释，不要代码。
{"component": "用户没点零件时你选的那个组件名；点了就原样返回",
 "options": [{"text": "≤40 个汉字，点一下就填进输入框的话",
              "mode": "replace 或 restyle 或 by_shot",
              "follow": "by_shot 时的变化轴 prop，可空"}],
 "axes": [{"prop": "该零件 props 里真实存在的名字", "label": "≤6 个汉字的人话名"}]}
规则：
* 选项要针对这个零件**现在的画法**，具体、可执行；不要写「换成别的东西」这种空话。
* 2~3 条 by_shot：说明跟着什么逐镜变（镜头内容 / 时代 / 场景 / 情绪），
  text 里不要出现参数名。
* 1 条 replace：给出这个零件合理的替代类别（不预设唯一答案，让用户自己补）。
* 1~2 条 restyle：针对它的轮廓 / 比例 / 材质 / 颜色给具体改法。
* mode=by_shot 时 follow 只能取该零件 props 合约里真实存在的名字；没有合适的就留空。
* 不要提代码、import、组件名；text 是给普通用户看的一句话。
* 这是建议不是规则：不要写「必须」「不照做就打回」这类话。
"""


#: 一句话常常同时要求改好几样（「人物轮廓和背景都按时代变，从古代到现代」）。
#: 只让用户一次点一个零件，等于把一句完整的诉求拆成几次操作，普通用户做不来。
#: 这里把原话拆成 1~4 个零件上的改动；每一条仍按单件的形状交给 `author_variant`。
EDIT_PLAN_SYSTEM = """你是风格模板的「改片需求拆解器」。用户用一句自然语言描述想要的效果，
常常同时涉及画面里的好几样东西。把这句话拆成 1~4 个零件上的改动。
只输出一个 JSON 对象，不要解释，不要代码。
{"edits": [{
  "component": "必须原样取槽位清单里的一个组件名",
  "label": "≤16 个汉字，给用户看的短名",
  "mode": "fixed 或 by_props",
  "spec": "≤400 字的画法要求，只写这个零件怎么画",
  "props": ["依赖的 prop 名"],
  "note": "一句话说明你的判断"
}]}
规则：
* 用户只提到一样东西，就只出一条。提到人物/背景/道具/字幕等多样，就每样一条。
* 不要为凑数把用户没提到的零件也拆进来；用户点中的零件要尽量都用上。
* 每个零件分两件事：**画法**（怎么画：剪影/平涂/渐变/字体，模板固定）和
  **内容**（画的是什么：山/平地、士兵/古人、哪句话）。默认只改内容；
  用户明确说「改画法/轮廓/风格」时才连画法一起改。
* 要求「每一镜 / 按时代 / 随内容变化」→ by_props，并从该零件的 props 合约里挑
  能表达变化的 prop。现有取值不够表达用户要的时代/场景时，仍然用这个 prop，
  在 spec 里列出**新增取值**和每档画法（替换件会把新取值加进联合类型）。
* 不需要逐镜变 → fixed。
* 只写这个零件画的内容与必要画法：轮廓、比例、姿态、持物、明暗、材质。
  不要写代码、不要建议改外壳、不要画第二个主体/第二块光/字幕。
* 沿用基模板自己的画法与配色（以槽位清单里写的默认画法为准），沿用画面比例单位。
* 原话里可能有抱怨或疑问，把它翻译成要避免什么、要画成什么，不要复述情绪。
"""


def _short_label(text: str) -> str:
    """一句话 → 列表里能看的短名。不截断原需求，只用来做展示名。"""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    return cleaned[:24] or "替换件"


def _content_line(c: Mapping[str, Any]) -> str:
    """一条内容清单 → 一句给模型看的话。

    `values` 是**能原样传给组件的字面量**（枚举）；`reference` 只是参考片里
    见过的语义标签（中文「军装」、色名「火橙」）。两者不能混成一句，否则
    写手会把「军装」当 `wardrobe` 的值传进去，组件落到缺省分支、画错也不报错。
    """
    prop = str(c.get("prop") or "")
    values = [str(x) for x in (c.get("values") or []) if str(x).strip()]
    ref = [str(x) for x in (c.get("reference") or []) if str(x).strip()]
    if values:
        line = f"`{prop}` 可传 {' / '.join(values)}"
        if ref:
            line += f"（参考片里对应：{' / '.join(ref)}）"
        return line
    if ref:
        return (f"`{prop}` 没有固定取值，参考片用过 {' / '.join(ref)}"
                "（按这一镜的需要传合法值）")
    return f"`{prop}`"


def _slot_brief(slots: Sequence[Mapping[str, Any]]) -> str:
    """槽位清单 → 解读器看的几行。

    每个零件都分成两件事，解读器必须分清：
    * **画法（固定）**：这个零件怎么画（剪影/平涂/渐变/字体），模板已经定死；
    * **内容（可改）**：它画的是山还是平地、士兵还是古人、哪句话——这才是
      用户要换的东西，通过 props 逐镜给值，或写成替换件。
    """
    lines: list[str] = []
    for slot in slots:
        varies = "、".join(f"`{p}`" for p in (slot.get("varies") or []))
        content = "；".join(
            _content_line(c)
            for c in (slot.get("content") or []) if isinstance(c, Mapping))
        lines.append(
            f"- `{slot.get('component') or ''}`"
            f"（{slot.get('label') or ''}，{slot.get('kind') or 'other'}）\n"
            f"  画法（固定）：{slot.get('default') or '见组件本身'}\n"
            f"  内容可改（逐镜给值的轴）：{varies or '（未声明）'}\n"
            f"  现在能画（内容取值）：{content or '（未声明；按画法默认）'}\n"
            # 绑定了替换件时，真正在画的是替换件：它新增的取值必须出现在合约里，
            # 否则解读器会以为模板还是只有基模板那几档。
            f"  props 合约：{slot.get('contract_live') or slot.get('contract_text') or '（无）'}")
    return "\n".join(lines)


#: 界面上让用户选的三种改法 → 一句给解读器的说明。用户不需要知道 props。
VARIANT_MODES = {
    "replace": "换成别的东西（固定一种形态）",
    "restyle": "改画法与轮廓（固定一种形态）",
    "by_shot": "跟着每一镜变（按镜头传入的参数逐档变化）",
}


def parse_variant_intent(
        text: str, slots: Sequence[Mapping[str, Any]], request: str, *,
        hint_component: str = "", hint_mode: str = "", follow: str = "",
        fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """解读器输出 → 白名单形状。坏形状只回退，不抛——解读是增强不是闸。

    组件名、prop 名都按槽位清单核一遍：模型写了一个模板里不存在的零件，
    写手会 import 到一个不存在的导出，换件就在打包期才炸。
    """
    names = [str(s.get("component") or "") for s in slots if s.get("component")]
    base = dict(fallback or {})
    base.setdefault("component", hint_component if hint_component in names else "")
    base.setdefault("label", _short_label(request))
    base.setdefault("mode", "fixed")
    base.setdefault("spec", str(request or "").strip()[:MAX_VARIANT_SPEC_CHARS])
    base.setdefault("props", [])
    base.setdefault("note", "")
    try:
        obj = json.loads(_strip_fence(text))
    except ValueError:
        return base
    if not isinstance(obj, Mapping):
        return base
    out = dict(base)
    comp = str(obj.get("component") or "").strip()
    if comp in names:
        out["component"] = comp
    spec = str(obj.get("spec") or "").strip()
    if spec:
        out["spec"] = spec[:MAX_VARIANT_SPEC_CHARS]
    label = str(obj.get("label") or "").strip()
    if label:
        out["label"] = label[:24]
    mode = "by_props" if str(obj.get("mode") or "") == "by_props" else "fixed"
    # 只认这个槽位真实存在的 props：解读器编出来的参数写手接不到，分档就落空了。
    slot = next((s for s in slots
                 if str(s.get("component") or "") == out["component"]), None)
    allowed = {str(p) for p in (slot.get("contract") or [])} if slot else set()
    props = [str(p).strip() for p in (obj.get("props") or [])
             if str(p).strip() in allowed]
    out["props"] = props[:6]
    # 界面上的三种改法是硬约束：
    # * 「换成别的 / 改画法」＝只要一种形态，不要再自作主张分档；
    # * 「跟着每一镜变」＝必须落到一个真实存在、能表达变化的轴（优先模板声明的
    #   `varies`，再退到合约里的其它 prop）。落不到就明说，不编参数。
    if hint_mode in ("replace", "restyle"):
        mode, out["props"] = "fixed", []
    elif hint_mode == "by_shot":
        axes = [str(p) for p in ((slot or {}).get("varies") or [])
                if str(p) in allowed]
        if str(follow or "").strip() in allowed:
            picked = [str(follow).strip()]
        elif out["props"]:
            picked = out["props"]
        else:
            picked = axes[:2] or sorted(allowed)[:2]
        out["props"] = picked[:6]
        mode = "by_props" if picked else "fixed"
    elif str(follow or "").strip() in allowed:
        out["props"] = [str(follow).strip()]
    if mode == "by_props" and not out["props"]:
        mode = "fixed"
        out["note"] = ((str(out.get("note") or "") + "；").lstrip("；")
                       + "解读结果没有落到可用的参数上，按固定画法")
    out["mode"] = mode
    note = str(obj.get("note") or out.get("note") or "").strip()
    if note:
        out["note"] = note[:200]
    return out


async def interpret_variant_request(
        llm: Any, *, base_row: Mapping[str, Any], slots: Sequence[Mapping[str, Any]],
        request: str, hint_component: str = "", mode: str = "", follow: str = "",
        identity_user: str = "", conversation_id: str = "") -> dict[str, Any]:
    """自然语言需求 → 「换哪个零件、按什么分档、画成什么样」。

    模型侧失败（超时/坏 JSON/不可达）只回退到原话与提示里选的零件，不拦换件：
    「堵死比白烧更坏」，一次解读失败不该让用户的一分钟需求直接作废。
    """
    names = [str(s.get("component") or "") for s in slots if s.get("component")]
    hint = str(hint_component or "").strip()
    mode = str(mode or "").strip()
    follow = str(follow or "").strip()
    fallback = {
        "component": hint if hint in names else "",
        "label": _short_label(request),
        "mode": "fixed",
        "spec": str(request or "").strip()[:MAX_VARIANT_SPEC_CHARS],
        "props": [],
        "note": "",
    }
    # 解读器失败时也要守住界面上选的改法：用户点了「跟着每一镜变」，
    # 不能因为一次超时就退成整片一种轮廓。落不到真实参数就保持固定，并在 note 里说清。
    if mode == "by_shot":
        slot = next((s for s in slots
                     if str(s.get("component") or "") == fallback["component"]), None)
        allowed = {str(p) for p in ((slot or {}).get("contract") or [])}
        axes = [str(p) for p in ((slot or {}).get("varies") or [])
                if str(p) in allowed]
        picked = ([str(follow).strip()] if str(follow).strip() in allowed
                  else axes[:1])
        fallback["props"] = picked
        if picked:
            fallback["mode"] = "by_props"
        else:
            fallback["note"] = "这个零件没有可用的变化参数，按固定画法"
    user = (f"模板：《{base_row.get('display') or base_row.get('name')}》"
            f"（{base_row.get('name')}）\n槽位清单：\n{_slot_brief(slots)}\n\n"
            f"用户原话：{request}\n"
            + (f"提示里已选的零件：`{hint}`（只作参考）\n" if hint else "")
            + (f"用户选择的改法：{VARIANT_MODES[mode]}\n" if mode in VARIANT_MODES else "")
            + (f"用户指定的变化轴：`{follow}`（只作参考）\n" if follow else ""))
    try:
        resp = await _complete(
            llm, [{"role": "system", "content": VARIANT_INTENT_SYSTEM},
                  {"role": "user", "content": user}],
            identity_user=identity_user, conversation_id=conversation_id,
            max_tokens=1200, timeout=90.0)
        raw = str(getattr(resp, "content") or "")
    except Exception:  # noqa: BLE001 - 解读失败按原话换件
        return fallback
    return parse_variant_intent(raw, slots, request, hint_component=hint,
                                hint_mode=mode, follow=follow,
                                fallback=fallback)


def parse_edit_plan(
        text: str, slots: Sequence[Mapping[str, Any]], *,
        request: str = "", hints: Sequence[str] = (),
        hint_mode: str = "", follow: str = "") -> list[dict[str, Any]]:
    """拆解器输出 → 1~4 条单件改动（形状与 `parse_variant_intent` 一致）。

    用户点中的零件是硬边界：拆解器写了别的零件也不采纳。模型坏 JSON 或一条都
    对不上时，按用户点中的每个零件各回退一条——点了就是想改，不因为一次解读
    失败让整句需求作废。
    """
    names = [str(s.get("component") or "") for s in slots if s.get("component")]
    wanted = []
    for h in hints:
        h = str(h or "").strip()
        if h in names and h not in wanted:
            wanted.append(h)
    edits: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        obj = json.loads(_strip_fence(text))
    except ValueError:
        obj = None
    items = obj.get("edits") if isinstance(obj, Mapping) else None
    if isinstance(items, list):
        for raw in items[:4]:
            if not isinstance(raw, Mapping):
                continue
            comp = str(raw.get("component") or "").strip()
            if comp not in names or comp in seen:
                continue
            if wanted and comp not in wanted:
                continue
            spec = str(raw.get("spec") or "").strip() or request
            intent = parse_variant_intent(
                json.dumps(raw, ensure_ascii=False), slots, spec,
                hint_component=comp, hint_mode=hint_mode,
                follow=follow if len(wanted) <= 1 else "")
            if str(intent.get("component") or "") != comp:
                continue
            label = str(raw.get("label") or "").strip()[:24]
            if label:
                intent["label"] = label
            seen.add(comp)
            edits.append(intent)
    def hinted(comp: str) -> dict[str, Any]:
        intent = parse_variant_intent(
            "", slots, request, hint_component=comp,
            hint_mode=hint_mode, follow=follow)
        # 与单件解读同一条回退纪律：用户点了「跟着每一镜变」，一次解读失败
        # 不能把整句需求退成整片一种；落不到真实参数才保持固定并说明。
        if hint_mode == "by_shot":
            slot = next((s for s in slots
                         if str(s.get("component") or "") == comp), None)
            allowed = {str(p) for p in ((slot or {}).get("contract") or [])}
            axes = [str(p) for p in ((slot or {}).get("varies") or [])
                    if str(p) in allowed]
            picked = ([str(follow).strip()] if str(follow).strip() in allowed
                      else axes[:1])
            intent["props"] = picked
            if picked:
                intent["mode"] = "by_props"
            else:
                intent["note"] = "这个零件没有可用的变化参数，按固定画法"
        return intent

    # 点中的零件是用户已经做出的选择：拆解器漏掉的那几个也补一条，
    # 不因为模型少写一条就静默少改。没点零件时仍以拆解结果为准。
    for comp in wanted[:4]:
        if comp not in seen:
            edits.append(hinted(comp))
    return edits


async def interpret_edit_plan(
        llm: Any, *, base_row: Mapping[str, Any],
        slots: Sequence[Mapping[str, Any]], request: str,
        components: Sequence[str] = (), mode: str = "", follow: str = "",
        identity_user: str = "", conversation_id: str = "") -> list[dict[str, Any]]:
    """一句自然语言 → 最多 4 个零件各自怎么改。

    与单件解读同一套纪律：模型侧失败只回退，不拦；点中的零件是硬边界。
    """
    names = [str(s.get("component") or "") for s in slots if s.get("component")]
    hints = [str(c or "").strip() for c in components
             if str(c or "").strip() in names]
    user = (f"模板：《{base_row.get('display') or base_row.get('name')}》"
            f"（{base_row.get('name')}）\n槽位清单：\n{_slot_brief(slots)}\n\n"
            f"用户原话：{request}\n")
    if hints:
        user += ("用户点中的零件：" + "、".join(f"`{c}`" for c in hints)
                 + "（只拆这几个，每个都要有一条）\n")
    if mode in VARIANT_MODES and mode:
        user += f"用户选择的改法（对所有零件生效）：{VARIANT_MODES[mode]}\n"
    if follow:
        user += f"用户指定的变化轴：`{follow}`（只作参考）\n"
    try:
        resp = await _complete(
            llm, [{"role": "system", "content": EDIT_PLAN_SYSTEM},
                  {"role": "user", "content": user}],
            identity_user=identity_user, conversation_id=conversation_id,
            max_tokens=1600, timeout=90.0)
        raw = str(getattr(resp, "content") or "")
    except Exception:  # noqa: BLE001 - 解读失败按用户点中的零件回退
        raw = ""
    return parse_edit_plan(raw, slots, request=request, hints=hints,
                           hint_mode=mode, follow=follow)


FIT_STORYBOARD_SYSTEM = """你是「分镜落地检查器」。这份分镜将由这套模板逐镜画出来。
模板的每个零件有固定画法；哪些内容现在能画，见槽位清单的「现在能画」与 props 合约。
只判断一件事：分镜里**明确写到**的画面内容或画法，模板现在能不能画。

规则：
* 能画（内容在该 prop 的可传取值里，或本来就是这个零件负责的画法）→ 不出条目。
* 明确写了但模板画不了（例如分镜写洞穴、客厅、地铁，背景取值只有山/城市/战场）
  → 出一条，用 by_props，在 spec 里列出**要新增的取值**和每档画法。
* 分镜没写清楚的不猜；不要为了「更丰富」而出条目；不要改画法，除非分镜明确要求。
* 同一个零件最多一条，总共最多 4 条；只改这个零件画的内容与必要画法，不改外壳。
* 没有缺口就输出 {"edits": []}。
只输出 JSON：
{"edits": [{
  "component": "必须原样取槽位清单里的一个组件名",
  "label": "≤16 个汉字",
  "mode": "by_props",
  "spec": "≤400 字：新增取值与每档画法",
  "props": ["依赖的 prop 名"],
  "note": "为什么现有取值不够"
}]}
"""


async def fit_storyboard(
        llm: Any, *, base_row: Mapping[str, Any],
        slots: Sequence[Mapping[str, Any]], storyboard: str,
        identity_user: str = "", conversation_id: str = "") -> list[dict[str, Any]]:
    """分镜 → 模板还缺哪些内容档（没有缺口返回空表）。

    与 `interpret_edit_plan` 的区别只在系统提示：这里是**保守的落地检查**——
    分镜没写清的不许猜，能画的不许出条目。模型侧失败一律返回空表，
    **绝不因为一次检查失败拦分镜**（「堵死比白烧更坏」）。
    """
    text = str(storyboard or "").strip()
    if not text:
        return []
    user = (f"模板：《{base_row.get('display') or base_row.get('name')}》"
            f"（{base_row.get('name')}）\n槽位清单：\n{_slot_brief(slots)}\n\n"
            f"这份分镜要画：\n{text[:6000]}\n")
    try:
        resp = await _complete(
            llm, [{"role": "system", "content": FIT_STORYBOARD_SYSTEM},
                  {"role": "user", "content": user}],
            identity_user=identity_user, conversation_id=conversation_id,
            max_tokens=1400, timeout=90.0)
        raw = str(getattr(resp, "content") or "")
    except Exception:  # noqa: BLE001 - 检查失败只代表「没发现缺口」
        return []
    return parse_edit_plan(raw, slots, request=text[:2000])


def fallback_variant_options(slot: Mapping[str, Any]) -> list[dict[str, str]]:
    """模型不可用时的三条方向：比空白输入框好，也比写死某套风格的例子通用。"""
    comp = str(slot.get("component") or "")
    label = str(slot.get("label") or comp or "这个零件")
    axes = [str(p) for p in (slot.get("varies") or []) if str(p)]
    return [
        {"text": f"每一镜跟着镜头内容改变{label}的轮廓与形态",
         "mode": "by_shot", "follow": axes[0] if axes else ""},
        {"text": f"改{label}的画法与轮廓（其它零件不动）",
         "mode": "restyle", "follow": ""},
        {"text": f"把{label}换成", "mode": "replace", "follow": ""},
    ]


def parse_variant_options(
        text: str, slots: Sequence[Mapping[str, Any]], *,
        component: str = "") -> dict[str, Any]:
    """建议器输出 → 白名单形状。落不到真实零件/参数上的一律丢掉，不抛。"""
    names = [str(s.get("component") or "") for s in slots if s.get("component")]
    want = str(component or "").strip()
    slot = next((s for s in slots
                 if str(s.get("component") or "") == want), None)
    obj: Mapping[str, Any] = {}
    try:
        raw = json.loads(_strip_fence(text))
        if isinstance(raw, Mapping):
            obj = raw
    except ValueError:
        pass
    if slot is None:
        # 没点零件：只认模型自己选的那个；选不到就回到「点一个零件」，不瞎猜。
        picked = str(obj.get("component") or "").strip()
        slot = next((s for s in slots
                     if str(s.get("component") or "") == picked), None)
        if slot is None:
            return {"component": "", "label": "", "options": [], "axes": []}
    comp = str(slot.get("component") or "")
    contract = {str(p) for p in (slot.get("contract") or [])}
    varies = [str(p) for p in (slot.get("varies") or []) if str(p) in contract]
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (obj.get("options") or []):
        if not isinstance(item, Mapping):
            continue
        txt = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()[:60]
        if not txt or txt in seen:
            continue
        mode = str(item.get("mode") or "").strip()
        if mode not in VARIANT_MODES:
            mode = "restyle"
        follow = str(item.get("follow") or "").strip()
        if follow not in contract:
            follow = ""
        if mode == "by_shot" and not follow and varies:
            follow = varies[0]
        seen.add(txt)
        options.append({"text": txt, "mode": mode, "follow": follow})
        if len(options) >= 6:
            break
    if not options:
        options = fallback_variant_options(slot)
    axes: list[dict[str, str]] = []
    for item in (obj.get("axes") or []):
        if not isinstance(item, Mapping):
            continue
        prop = str(item.get("prop") or "").strip()
        if prop not in contract:
            continue
        label = str(item.get("label") or "").strip()[:8] or prop
        if all(a["prop"] != prop for a in axes):
            axes.append({"prop": prop, "label": label})
    for prop in varies:
        if all(a["prop"] != prop for a in axes):
            axes.append({"prop": prop, "label": prop})
    return {"component": comp,
            "label": str(slot.get("label") or comp),
            "options": options, "axes": axes[:8]}


async def suggest_variant_options(
        llm: Any, *, slots: Sequence[Mapping[str, Any]],
        base_row: Mapping[str, Any], component: str = "",
        identity_user: str = "", conversation_id: str = "") -> dict[str, Any]:
    """给换件面板生成几条能点选的选项——跟着模板自己的槽位来，不预设风格。

    这是一次小调用（≤900 tokens），失败/超时时对已点中的零件退到通用三条；
    没有点中零件时返回空清单，面板保持「点一个零件」的状态，不替用户瞎选。
    """
    names = [str(s.get("component") or "") for s in slots if s.get("component")]
    picked = str(component or "").strip()
    focus = next((s for s in slots
                  if str(s.get("component") or "") == picked), None)
    if picked and focus is None:
        return {"component": "", "label": "", "options": [], "axes": []}
    user = (f"模板：《{base_row.get('display') or base_row.get('name')}》"
            f"（{base_row.get('name')}）\n"
            f"{str(base_row.get('description') or '')}\n"
            f"槽位清单：\n{_slot_brief([focus] if focus else slots)}\n\n"
            + (f"用户点中的零件：`{picked}`。只给这个零件出选项。\n"
               if focus else "用户还没点零件，你选最值得改的那一个，并在 component 里写它。\n"))
    try:
        resp = await _complete(
            llm, [{"role": "system", "content": VARIANT_OPTIONS_SYSTEM},
                  {"role": "user", "content": user}],
            identity_user=identity_user, conversation_id=conversation_id,
            max_tokens=900, timeout=45.0)
        raw = str(getattr(resp, "content") or "")
    except Exception:  # noqa: BLE001 - 选项只是帮用户开口，失败不该挡住换件
        if focus is None:
            return {"component": "", "label": "", "options": [], "axes": []}
        return {"component": str(focus.get("component") or ""),
                "label": str(focus.get("label") or ""),
                "options": fallback_variant_options(focus), "axes": []}
    return parse_variant_options(raw, slots, component=picked)


def build_variant_messages(*, slot: Mapping[str, Any], base_row: Mapping[str, Any],
                           target: str, frames: Sequence[Path] = (),
                           prior_errors: Sequence[str] = (),
                           intent: Mapping[str, Any] | None = None
                           ) -> list[dict[str, Any]]:
    """基模板的一个槽位 + 目标描述 → 写替换件的请求。"""
    comp = str(slot.get("component") or "")
    contract_text = str(slot.get("contract_text") or "")
    contract = [str(p) for p in (slot.get("contract") or []) if str(p).strip()]
    source = str(base_row.get("source") or "")
    user = (
        f"基模板：{base_row.get('display') or base_row.get('name')}"
        f"（{base_row.get('name')}）\n"
        f"要替换的槽位：`{comp}`"
        + (f"（{slot.get('label')}）" if slot.get("label") else "") + "\n"
        f"这个槽位在参考片里的默认画法：{slot.get('default') or '（见原组件）'}\n"
        f"用户原话（可能是口语、抱怨或反问，不要照抄）：{target}\n"
        f"换成本镜要画的东西：**{target}**\n\n"
        "槽位合约（必须原样接住这些 props；名字不要改、不要删，类型只增不减）：\n"
        f"{contract_text or '（这个组件原本没有 props 类型；按零参数写，所有可调项都给缺省值）'}\n"
        "可调 props 名单："
        + ("、".join(f"`{p}`" for p in contract) if contract else "（无）") + "\n"
        "如果这次要的分档比合约里现有的取值更多（更多时代/场景/形态），就在对应的\n"
        "联合类型里增加取值，并在组件里为每个新取值画出一眼能区分的样子；\n"
        "原有取值必须继续被接受。\n"
    )
    if isinstance(intent, Mapping) and str(intent.get("spec") or "").strip():
        user += (f"\n解读后的画法（以这份为准，原话只用来理解意图）：\n"
                 f"{intent.get('spec')}\n")
        if str(intent.get("mode") or "") == "by_props":
            props = "、".join(f"`{p}`" for p in (intent.get("props") or []))
            user += (f"这个组件在不同镜头会收到不同的 {props} 值。必须按每个取值画出"
                     "**明显不同**的轮廓/形态；几档共用同一副躯干、只换帽子或只换颜色"
                     "都不算分档。\n")
        else:
            user += "这次只画一种形态；不要自行发明参数分档。\n"
    if source:
        user += ("\n下面是基模板源码。只参考它**画这个零件的写法与单位**；"
                 "不要整份抄过来，也不要 import 它：\n```tsx\n"
                 + source[:24000] + "\n```\n")
    if prior_errors:
        user += ("\n上一版没通过，逐条处理以下问题后重写整份文件：\n"
                 + "\n".join(f"- {e}" for e in list(prior_errors)[:10]))
    picked = _sample_frames(frames)
    if picked:
        user += ("\n\n下面附上目标对象/参考片的关键帧：轮廓、比例、姿态以这些帧为准。")
        content: Any = [{"type": "text", "text": user},
                        *(_image_part(p) for p in picked)]
    else:
        content = user
    return [{"role": "system", "content": VARIANT_SYSTEM},
            {"role": "user", "content": content}]


def variant_contract_errors(source: str, component: str,
                            contract: Sequence[str]) -> list[str]:
    """替换件必须接住槽位合约：同样的 props 名，且都可选。"""
    props = st.component_props(source)
    text = props.get(component, "")
    if not text:
        return [f"没有导出组件 `{component}` 的 props 类型——"
                f"要写成 `type {component}Props = {{ … }}` 并在组件上使用它。"]
    have = set(st.prop_names(text))
    errors: list[str] = []
    missing = [p for p in contract if p not in have]
    if missing:
        errors.append("替换件缺少槽位合约里的 props："
                      + "、".join(f"`{p}`" for p in missing)
                      + "。模板会按原样把这些值传进来，缺一个就接不住。")
    required = [p for p in contract
                if p in have and not re.search(rf"(?:^|[；;\s]){re.escape(p)}\?\s*:", text)]
    if required:
        errors.append("这些 props 必须可选（`p?:` 并给缺省值），零参数也要能渲出来："
                      + "、".join(f"`{p}`" for p in required))
    return errors


def variant_probe_code(module: str, component: str) -> str:
    """只渲染替换件的一镜，用来出预览帧。"""
    return (
        "import React from 'react';\n"
        "import { useT } from '@dg/motion';\n"
        f"import {{ {component} }} from '{module}';\n\n"
        "const Shot = () => {\n"
        "  const t = useT();\n"
        "  return (\n"
        "    <div style={{position: 'absolute', inset: 0,\n"
        "                 transform: `scale(${1 + 0 * t})`}}>\n"
        f"      <{component} />\n"
        "    </div>\n"
        "  );\n"
        "};\n"
        "export default Shot;\n")


def variant_name(user_id: str, base_name: str, component: str, target: str) -> str:
    """替换件的库内名字：`u-<用户 8 位>-v-<6 位指纹>`。

    指纹带上基模板、组件和目标描述：同样的目标重复生成落到同一行（版本 +1），
    换一个目标就是另一件。名字短，留出 staging 后缀的余地（2~41 位）。
    """
    uid = re.sub(r"[^a-z0-9]+", "", str(user_id or "").lower())[-8:] or "anon"
    digest = hashlib.sha256(
        f"{base_name}|{component}|{target}".encode("utf-8")).hexdigest()[:6]
    name = f"u-{uid}-v-{digest}"
    if not _NAME_RE.match(name):
        raise AuthoringError(f"分配出的替换件名不合法：{name}")
    return name


async def _collect_variant_frames(videos: Sequence[str | Path]) -> list[Path]:
    """替换件也可以带自己的参考片；没有就按文字写。最多取两条、共 8 帧。"""
    frames: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="variant-frames-") as tmp:
        for i, video in enumerate(list(videos)[:2]):
            meta = await asyncio.to_thread(probe_video, video)
            times = pick_times(float(meta.get("duration") or 0), 4)
            frames += await asyncio.to_thread(
                extract_frames, video, Path(tmp) / f"v{i:02d}", times)
    return frames


async def _variant_base(storage: Any, base_name: str,
                        user_id: str) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    """换件前对基模板的同一套检查：可见、已过闸、属于用户、有槽位。"""
    base = await storage.style_templates.get(base_name)
    if base is None:
        raise AuthoringError(f"模板「{base_name}」不在库里")
    if not st.visible(base, user_id):
        raise AuthoringError(f"模板「{base_name}」不可用")
    if str(base.get("status") or "") != "validated":
        raise AuthoringError(f"模板「{base_name}」还没过闸，先沉淀成功再换件")
    # 登记替换件要写基模板的 manifest；系统模板是共享行，不能混进用户数据。
    if base.get("owner_user_id") is None:
        raise AuthoringError("系统内置模板不能换件（请先另存为自己的模板）")
    slots = st.template_slots(base)
    if not slots:
        raise AuthoringError(f"模板「{base_name}」没有可换的导出组件")
    return base, slots


async def author_variant(
    storage: Any, *, base_name: str, component: str, target: str,
    user_id: str, llm: Any = None, identity_user: str = "",
    videos: Sequence[str | Path] = (), conversation_id: str = "",
    mode: str = "", follow: str = "",
    intent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """给基模板的一个槽位写一件替换实现，过闸后登记为模板变体。

    与整份模板沉淀共用同一套闸：导出核对、合约核对、lint、零参数预览非空。
    不同的是**只生成一个组件**——壳、时间轴、字幕版式都不重写，失败面小得多。
    替换件不动基模板的源码与版本；只往 `manifest.variants` 里追加一条登记。

    ``intent`` 是已经解读好的单件改动（来自 `parse_edit_plan`）：给了就不再
    单独调一次解读器，一句话拆多个零件时每个零件只解读一次。
    """
    if llm is None:
        from .llm_openai import get_default_llm
        llm = get_default_llm()
    base, slots = await _variant_base(storage, base_name, user_id)
    names = [str(s.get("component") or "") for s in slots]
    hint = str(component or "").strip()
    if hint and hint not in names:
        raise AuthoringError(
            f"模板「{base_name}」没有组件「{hint}」；可换的是："
            + "、".join(names[:12]))
    goal = str(target or "").strip()
    if not goal:
        raise AuthoringError(
            "先说明要换成什么（一句自然语言就行，例：第一镜不要戴头盔的战士）")
    if len(goal) > MAX_VARIANT_REQUEST_CHARS:
        goal = goal[:MAX_VARIANT_REQUEST_CHARS]
    # 用户说的是人话，不是槽位合约：先把「换哪个零件、按什么分档」解读出来。
    # 解读失败只回退到原话与已选零件（见 `interpret_variant_request`），不拦换件。
    if intent is not None:
        # 一句话拆多个零件时，`author_edits` 已经解读过；这里不再重复问一次模型。
        intent = dict(intent)
    else:
        intent = await interpret_variant_request(
            llm, base_row=base, slots=slots, request=goal, hint_component=hint,
            mode=mode, follow=follow,
            identity_user=identity_user or user_id,
            conversation_id=conversation_id)
    component = str(intent.get("component") or "")
    if not component:
        raise AuthoringError(
            "没听懂要换哪个零件；在换件面板里点一个零件，或把要求说得更具体一点")
    slot = next(s for s in slots
                if str(s.get("component") or "") == component)
    name = variant_name(user_id, base_name, component, goal)
    module = f"{st.USER_MODULE_PREFIX}{name}"
    staging = f"{name[:36]}-d{hashlib.sha256(name.encode()).hexdigest()[:3]}"
    frames = await _collect_variant_frames(videos) if videos else []

    errors: list[str] = []
    source = ""
    stills: list[dict[str, Any]] = []
    detail: list[str] = []
    attempts = 0
    probe_src = ""
    try:
        for _ in range(MAX_VARIANT_ATTEMPTS):
            attempts += 1
            try:
                code_kwargs: dict[str, Any] = {"max_tokens": 10000,
                                               "timeout": 300.0}
                if _template_model():
                    code_kwargs["model_override"] = _template_model()
                resp = await _complete(
                    llm, build_variant_messages(
                        slot=slot, base_row=base, target=goal,
                        frames=frames, prior_errors=errors, intent=intent),
                    identity_user=identity_user or user_id,
                    conversation_id=conversation_id, **code_kwargs)
                source = _strip_fence(str(getattr(resp, "content", "") or ""))
            except Exception as exc:  # noqa: BLE001 - 模型侧失败也进重试清单
                errors = [f"写替换件时模型调用失败：{str(exc)[:300]}"]
                detail.append(errors[0])
                continue
            if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                errors = [f"替换件源码超过 {MAX_SOURCE_BYTES // 1024}KB"]
                detail.append(errors[0])
                continue
            # 替换件只有这一个组件：不要额外的预览件导出，直接渲组件本身。
            errors = (check_exports(source, [{"name": component}], probe=component)
                      + variant_contract_errors(
                          source, component, [str(p) for p in slot.get("contract") or []]))
            if not errors:
                try:
                    errors = await lint_template(source)
                except Exception as exc:  # noqa: BLE001 - 容器不可达也是可重试原因
                    errors = [f"模板闸不可用：{str(exc)[:200]}"]
            if errors:
                detail.extend(errors)
                continue
            try:
                await push_template(staging, source)
                probe_src = variant_probe_code(
                    f"{st.USER_MODULE_PREFIX}{staging}", component)
                stills = await render_stills(probe_src)
                blank = _blank_reason(stills)
                if blank:
                    errors = [blank]
                    detail.append(blank)
                    continue
                if frames:
                    try:
                        review = await _complete(
                            llm, build_fidelity_messages(
                                {"layers": [{
                                    "id": "slot", "kind": slot.get("kind") or "other",
                                    "name": slot.get("label") or component,
                                    "component": component,
                                    "default": f"按目标描述画成：{goal}",
                                }]}, frames, stills),
                            identity_user=identity_user or user_id,
                            conversation_id=conversation_id,
                            max_tokens=1200, timeout=120.0)
                        ok, diffs = parse_fidelity(
                            str(getattr(review, "content", "") or ""))
                    except AuthoringError as exc:
                        ok, diffs = True, []
                        detail.append(f"替换件保真评审没读出结论，已跳过：{exc}")
                    except Exception as exc:  # noqa: BLE001 - 评审失败不拦
                        ok, diffs = True, []
                        detail.append(f"替换件保真评审调用失败，已跳过：{str(exc)[:200]}")
                    if not ok:
                        errors = ["零参数预览帧与目标参考不符：", *diffs]
                        detail.extend(diffs)
                        continue
                break
            except AuthoringError as exc:
                errors = [str(exc), *exc.detail]
                detail.extend(errors)
            except Exception as exc:  # noqa: BLE001 - 容器/网络错误也进重试
                errors = [f"替换件闸调用失败：{str(exc)[:300]}"]
                detail.extend(errors)
        else:
            await drop_template(staging)
            raise AuthoringError(
                f"替换件没通过闸（已尝试 {MAX_VARIANT_ATTEMPTS} 版）",
                detail=detail, attempts=attempts)

        label = str(intent.get("label") or goal[:24])
        # 替换件可以扩展联合类型的取值（更多时代/场景/形态）。把它自己的 props
        # 文本存下来：运行时写手要能看到「这件能接受哪些新取值」，否则分镜只会
        # 沿用基模板那一档，换件等于白换。
        contract_text = (st.component_props(source).get(component, "")
                         or str(slot.get("contract_text") or ""))
        validation = {"attempts": attempts, "target": goal,
                      "source_bytes": len(source.encode("utf-8")),
                      "stills": len(stills), "screens": len(frames),
                      "intent": dict(intent)}
        values = {
            "display": f"{base.get('display') or base_name} · {label}",
            "description": f"《{base.get('display') or base_name}》槽位 `{component}` 的替换件：{goal}",
            "module": module,
            "components": [{"name": component,
                            "role": f"{slot.get('label') or component}（替换件：{label}）"}],
            "source": source,
            "manifest": {
                "variant_of": {"template": base_name, "component": component,
                               "label": label, "request": goal,
                               "mode": str(intent.get("mode") or "fixed"),
                               "spec": str(intent.get("spec") or "")},
                "contract": [str(p) for p in slot.get("contract") or []],
                "contract_text": contract_text,
                "responsible": f"只负责 `{component}` 这一个槽位；外壳由基模板提供。",
            },
            "probe": probe_src,
            "status": "validated",
            "owner_user_id": user_id,
            "validation": validation,
        }
        existing = await storage.style_templates.get(name)
        if existing is not None and existing.get("owner_user_id") not in (None, user_id):
            raise AuthoringError("同名的替换件属于别人，换一个目标描述")
        row = await storage.style_templates.upsert(name, **values)
        await push_template(name, source)
        # 登记到基模板：运行时 `template_slots()` 从这里把替换件挂回对应槽位。
        # `default=True` 让**刚换上的这一件**在后续分镜里自动生效——用户点「换件」
        # 的意图就是「用上它」，不该再要求他记住替换件名字、手写 style_overrides。
        # 同一槽位只保留最新一件为默认；想回到模板自带的那件，显式指定 `default`。
        base_manifest = dict(base.get("manifest") or {})
        variants: list[dict[str, Any]] = []
        for v in (base_manifest.get("variants") or []):
            if not isinstance(v, Mapping) or v.get("name") == name:
                continue
            item = dict(v)
            if str(item.get("component") or "") == component:
                item["default"] = False
            variants.append(item)
        variants.append({
            "name": name,
            "module": module,
            "component": component,
            "label": label,
            "contract": [str(p) for p in slot.get("contract") or []],
            # 运行时绑定后，写手按这份文本决定这一镜能传哪些新取值。
            "contract_text": contract_text,
            "hash": st.source_hash(row),
            "default": True,
        })
        base_manifest["variants"] = variants[:64]
        await storage.style_templates.set_manifest(base_name, base_manifest)
        return {
            "ok": True,
            "name": name,
            "module": module,
            "component": component,
            "label": label,
            "request": goal,
            "intent": dict(intent),
            "contract": [str(p) for p in slot.get("contract") or []],
            "base_template": base_name,
            "attempts": attempts,
            "stills": stills,
            "validation": validation,
            "template": public_row(row),
        }
    finally:
        await drop_template(staging)


async def author_edits(
    storage: Any, *, base_name: str, target: str, user_id: str,
    components: Sequence[str] = (), llm: Any = None, identity_user: str = "",
    videos: Sequence[str | Path] = (), conversation_id: str = "",
    mode: str = "", follow: str = "",
) -> dict[str, Any]:
    """一句话改画面里的几样东西（人物轮廓 + 背景一起变）。

    普通用户说不清「槽位」「props」，更不会理解一句话只能改一个零件。这里先把
    原话拆成 1~4 个零件上的改动，再逐个走单件替换同一条闸；一个零件失败不影响
    其它零件。全失败才报错，部分成功在回执里列出没换成的那些。
    """
    if llm is None:
        from .llm_openai import get_default_llm
        llm = get_default_llm()
    base, slots = await _variant_base(storage, base_name, user_id)
    names = [str(s.get("component") or "") for s in slots]
    hints: list[str] = []
    for c in components:
        c = str(c or "").strip()
        if not c:
            continue
        if c not in names:
            raise AuthoringError(
                f"模板「{base_name}」没有组件「{c}」；可换的是："
                + "、".join(names[:12]))
        if c not in hints:
            hints.append(c)
    goal = str(target or "").strip()
    if not goal:
        raise AuthoringError("先说想改成什么样（一句话就行）")
    if len(goal) > MAX_VARIANT_REQUEST_CHARS:
        goal = goal[:MAX_VARIANT_REQUEST_CHARS]
    edits = await interpret_edit_plan(
        llm, base_row=base, slots=slots, request=goal, components=hints,
        mode=mode, follow=follow,
        identity_user=identity_user or user_id, conversation_id=conversation_id)
    if not edits:
        raise AuthoringError(
            "没听懂要改画面里的哪一样；可以在换件面板里点一个零件，"
            "或把要求说得更具体一点（比如「人物和背景都跟着时代变」）")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for edit in edits:
        comp = str(edit.get("component") or "")
        try:
            results.append(await author_variant(
                storage, base_name=base_name, component=comp, target=goal,
                user_id=user_id, llm=llm,
                identity_user=identity_user or user_id, videos=videos,
                conversation_id=conversation_id, intent=edit))
        except AuthoringError as exc:
            failures.append({
                "component": comp,
                "label": str(edit.get("label") or comp),
                "error": str(exc),
                "detail": list(exc.detail or []),
            })
        except Exception as exc:  # noqa: BLE001 - 一个零件失败不拖垮其它零件
            failures.append({
                "component": comp,
                "label": str(edit.get("label") or comp),
                "error": f"换这个零件时出错：{str(exc)[:300]}",
                "detail": [],
            })
    if not results:
        detail = [f"{f['label']}：{f['error']}" for f in failures]
        raise AuthoringError("这次要改的零件都没换成", detail=detail)
    return {"ok": True, "request": goal, "base_template": base_name,
            "edits": results, "failures": failures,
            "intent": {"edits": [dict(e) for e in edits]}}


def exported_names(code: str) -> set[str]:
    """源码里的导出名（`export const/function/class` 与 `export {…}` 两种写法）。"""
    names: set[str] = set(re.findall(
        r"export\s+(?:default\s+)?(?:async\s+)?(?:const|let|var|function|class)\s+"
        r"([A-Za-z_$][\w$]*)", code or ""))
    for body in re.findall(r"export\s*\{([^}]*)\}", code or ""):
        for part in body.split(","):
            piece = part.strip()
            if not piece:
                continue
            alias = re.split(r"\s+as\s+", piece)[-1].strip()
            if alias:
                names.add(alias)
    return names


def check_exports(code: str, components: Sequence[Mapping[str, Any]],
                  *, probe: str = _PROBE_EXPORT) -> list[str]:
    """分析点名的组件与预览件必须真导出——否则写手照正文 import 会打包失败。"""
    got = exported_names(code)
    missing = [str(c.get("name") if isinstance(c, Mapping) else c)
               for c in components
               if str(c.get("name") if isinstance(c, Mapping) else c) not in got]
    errors: list[str] = []
    if missing:
        errors.append("缺少导出：" + "、".join(missing))
    if probe not in got:
        errors.append(f"缺少预览件导出：{probe}")
    return errors


#: 公共组件返回裸 `<g>`：它会被逐镜代码挂在 `<div>` 下，SVG 子元素不画。
_BARE_GROUP_RETURN_RE = re.compile(r"(?:return|=>)\s*\(?\s*<g\b")
#: JSX 属性字符串里的 `\n` 不会被解析成换行，会原样显示反斜杠 + n。
_JSX_ESCAPED_NL_RE = re.compile(
    r'\b(?:text|content|caption|inscription|title|label|subtitle)="'
    r'[^"\n]*\\n[^"\n]*"')


def _export_chunks(code: str) -> dict[str, str]:
    """按导出声明起点切块（到下一个导出前），用来判断某个组件返回什么。"""
    starts = list(re.finditer(
        r"export\s+(?:default\s+)?(?:async\s+)?(?:const|let|var|function|class)\s+"
        r"([A-Za-z_$][\w$]*)", code or ""))
    chunks: dict[str, str] = {}
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(code or "")
        chunks[m.group(1)] = (code or "")[m.start():end]
    return chunks


def check_self_contained(code: str, components: Sequence[Mapping[str, Any]],
                         *, probe: str = _PROBE_EXPORT) -> list[str]:
    """公共组件必须自包含：返回 `<AbsoluteFill>` 或 `<svg>`，不能是裸 `<g>`。

    真机 2026-10-19：分析里的主体组件返回 `<g>`，预览件直接放在 `AbsoluteFill`
    （一个 div）下，整层人物画不出来。静态闸看不到画面，这种结构错必须在这里拦；
    否则会以 validated 发布，等到出片才发现没有人。

    只在整块里没有 `<svg` 时才判：组件内部定义返回 `<g>` 的私有辅助函数是合法的，
    只要公共组件的返回里有自己的 `<svg>`。
    """
    names = [str(c.get("name") if isinstance(c, Mapping) else c)
             for c in components]
    chunks = _export_chunks(code)
    errors: list[str] = []
    for name in [*names, probe]:
        chunk = chunks.get(name) or ""
        if not chunk or "<svg" in chunk:
            continue
        if _BARE_GROUP_RETURN_RE.search(chunk):
            errors.append(
                f"`{name}` 直接返回 `<g>`；它会被挂在 div 下，SVG 子元素不会画出来。"
                "公共组件请返回 `<AbsoluteFill>`（内部放 `<svg>`）或直接返回 `<svg>`。")
    return errors


def check_jsx_text_escapes(code: str) -> list[str]:
    """JSX 属性字符串不解析转义：`text="a\\nb"` 会原样显示 `\\n`。

    多行文案要写成 `text={'a\\nb'}`。组件内部同时兼容字面 `\\n`
    （`String(text).split(/\\n|\\\\n/)`），因为逐镜代码也可能用属性字符串传。
    """
    hits = sorted({m.group(0) for m in _JSX_ESCAPED_NL_RE.finditer(code or "")})
    return [
        "JSX 属性字符串里的 `\\n` 不会换行，会原样显示：`" + hit[:60] + "`。"
        "多行文案改用 `text={'第一行\\n第二行'}`，组件内按 `/\\n|\\\\n/` 拆行。"
        for hit in hits
    ]


def _prop_names(signatures: Mapping[str, str]) -> set[str]:
    """`{组件: "a?: number；b: string"}` → {"a", "b"}。"""
    out: set[str] = set()
    for sig in signatures.values():
        for field in str(sig or "").split("；"):
            name = field.split(":", 1)[0].strip().rstrip("?").strip()
            if name:
                out.add(name)
    return out


def check_motion_contract(code: str, analysis: Mapping[str, Any]) -> list[str]:
    """沉淀期硬校验：参考片里**会变**的那几件，模板组件有没有对应的 prop。

    这是「完整复刻」最关键的一道闸，放在**沉淀时**而不是出片后：模板一旦发布，
    镜头代码再怎么写也换不了写死的那一档。真机 2026-10-19 的模板把
    `wardrobe="coat"` 封在组件内部，分析里却明明有「兽皮 → 外套 → 连帽衫」——
    背景在换、人永远穿同一件，就是这笔账。

    只判两件可判的事：`variations[].prop` 必须出现在某个导出组件的 props 里；
    `continuity.subject_motion=continuous` 时必须有 `phase`（或等效时钟）。
    「有没有真的按 prop 画」仍要靠预览帧与评审，不在这里假装量得出。
    """
    manifest = analysis.get("manifest") if isinstance(analysis, Mapping) else None
    contract = (manifest.get("motion_contract")
                if isinstance(manifest, Mapping) else None)
    if not isinstance(contract, Mapping) or not contract:
        return []
    have = _prop_names(st.component_props(code))
    errors: list[str] = []
    missing = [str(v.get("prop")) for v in (contract.get("variations") or [])
               if isinstance(v, Mapping) and v.get("prop")
               and str(v.get("prop")) not in have]
    if missing:
        errors.append(
            "motion_contract 说这些会变，但组件没有对应 props："
            + "、".join(f"`{m}`" for m in missing)
            + "。把这些 prop 加进负责该部分的组件并真的用它——"
            "写死其中一档，参考片里「背景在换、服装也在换」就复刻不出来。")
    cont = contract.get("continuity")
    if (isinstance(cont, Mapping)
            and cont.get("subject_motion") == "continuous"
            and "phase" not in have):
        errors.append(
            "motion_contract.continuity.subject_motion=continuous（整片一直在走/一直在动），"
            "但组件没有 `phase`（或等效的全片时钟 prop）。加一个 0~1 的周期相位，"
            "由调用方按全片时钟传进来——每镜从 0 重算会在切点处换脚。")
    return errors


def check_facets(code: str, analysis: Mapping[str, Any]) -> list[str]:
    """沉淀期按三个角度验收模板：审美 / 动作编排 / 文字组件。

    为什么不能只查 `motion_contract`：参考片的「完整复刻」不止是颜色和构图。它的
    **动作是一层整片连续的轨道**（人一直在走），**字是能被内容驱动的组件**（字幕、
    铭文每镜不同）。这两样不落成导出的组件与 props，逐镜写手就只能各自发挥，
    成片会出现「背景换了、人重新起步」「每镜字幕长得不一样」——不是写手不听话，
    是模板根本没给出口。
    """
    manifest = analysis.get("manifest") if isinstance(analysis, Mapping) else None
    facets = manifest.get("facets") if isinstance(manifest, Mapping) else None
    if not isinstance(facets, Mapping) or not facets:
        return []
    have = _prop_names(st.component_props(code))
    errors: list[str] = []
    missing: list[str] = []
    for fname in _FACET_NAMES:
        facet = facets.get(fname)
        if not isinstance(facet, Mapping):
            continue
        for item in facet.get("variations") or []:
            prop = str(item.get("prop") or "").strip() if isinstance(item, Mapping) else ""
            if prop and prop not in have:
                name = item.get("name") if isinstance(item, Mapping) else ""
                missing.append(f"`{prop}`（{fname}：{name or prop}）")
    if missing:
        errors.append(
            "facets 说这些维度会变，但组件没有对应 props："
            + "、".join(missing)
            + "。把这些 prop 加进负责该部分的组件并真的用它——写死其中一档，"
              "参考片里「背景在换、服装也在换」就复刻不出来。")
    text = facets.get("text")
    if isinstance(text, Mapping):
        for item in text.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            comp = str(item.get("component") or "").strip()
            prop = str(item.get("content_prop") or "").strip()
            if prop and prop not in have:
                errors.append(
                    f"文字部件 `{comp or '未命名'}` 没有内容 prop `{prop}`——"
                    "字幕/铭文要能被逐镜内容驱动，不能把示例文字写死在组件里。")
    motion = facets.get("motion")
    if isinstance(motion, Mapping):
        for track in motion.get("tracks") or []:
            if not isinstance(track, Mapping):
                continue
            if track.get("scope") != "film" or not track.get("continuous"):
                continue
            comp = str(track.get("component") or "未命名").strip()
            clock = str(track.get("clock") or "filmTimeSec").strip()
            if clock not in have and "phase" not in have:
                errors.append(
                    f"`{comp}` 是 film 作用域的连续轨道，但没有全片时钟 prop "
                    f"`{clock}`（或 `phase`）。加一个收「这一镜在整片里的绝对时间」"
                    "的 prop，由镜头代码传 `getInputProps().filmStartSec + frame/fps`——"
                    "每镜从 0 重算会在切点处换脚、停住。")
    return errors


def probe_shot_code(module: str, *, probe: str = _PROBE_EXPORT) -> str:
    """过闸/预览用的一镜：只把模板的预览件挂上去。"""
    return (
        "import React from 'react';\n"
        "import { useT } from '@dg/motion';\n"
        f"import {{ {probe} }} from '{module}';\n\n"
        "const Shot = () => {\n"
        "  const t = useT();\n"
        "  return (\n"
        "    <div style={{position: 'absolute', inset: 0,\n"
        "                 transform: `scale(${1 + 0 * t})`}}>\n"
        f"      <{probe} />\n"
        "    </div>\n"
        "  );\n"
        "};\n"
        "export default Shot;\n")


# ---------------------------------------------------------------- 渲染容器


def _remotion_url(path: str) -> str:
    return f"{st._base()}{path}"


def _fetch_bytes(url: str, timeout: float = 120.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


async def lint_template(source: str) -> list[str]:
    got = await asyncio.to_thread(st._http, "POST", "/lint-template",
                                  {"code": source}, timeout=60.0)
    if got.get("ok"):
        return []
    return [f"lint {e.get('rule')}（{e.get('line')}:{e.get('column')}）：{e.get('msg')}"
            for e in (got.get("errors") or [])] or ["模板 lint 未通过"]


async def push_template(name: str, source: str) -> str:
    got = await asyncio.to_thread(st._http, "POST", "/templates",
                                  {"name": name, "source": source}, timeout=60.0)
    if not got.get("ok"):
        raise AuthoringError(f"模板推给渲染容器失败：{got.get('error')}",
                             detail=[json.dumps(got.get("errors") or [], ensure_ascii=False)[:400]])
    return str(got.get("hash") or "")


async def drop_template(name: str) -> None:
    await asyncio.to_thread(st.drop_remote, name)


async def render_stills(code: str, *, frames: Sequence[float] = (0.15, 0.5, 0.85),
                        scale: float = 0.5) -> list[dict[str, Any]]:
    """渲三张预览帧（JPEG base64）。打不出包/运行时报错都在这里变成可读原因。"""
    idx = [max(0, min(PROBE_FRAMES - 1, round(PROBE_FRAMES * f))) for f in frames]
    got = await asyncio.to_thread(
        st._http, "POST", "/render-stills",
        {"code": code, "scale": scale,
         "input_props": {"fps": PROBE_FPS, "durationInFrames": PROBE_FRAMES,
                         "aspect": "landscape"},
         "frames": idx},
        timeout=300.0)
    if not got.get("ok"):
        raise AuthoringError(f"预览帧渲不出来：{got.get('error')}",
                             detail=[json.dumps(got.get("errors") or [], ensure_ascii=False)[:600]])
    stills = [s for s in (got.get("stills") or []) if s.get("jpeg_b64")]
    if len(stills) < 2:
        raise AuthoringError("预览帧数量不够（少于 2 张）")
    return stills


async def render_clip(code: str, *, key: str) -> bytes:
    got = await asyncio.to_thread(
        st._http, "POST", "/render-shot",
        {"code": code, "cache_key": key, "declared_fields": [],
         "input_props": {"fps": PROBE_FPS, "durationInFrames": DETERMINISM_FRAMES,
                         "aspect": "landscape"},
         "render": {"crf": 1, "concurrency": 1}},
        timeout=300.0)
    if not got.get("ok"):
        raise AuthoringError(f"确定性闸渲不出来：{got.get('error')}",
                             detail=[json.dumps(got.get("errors") or [], ensure_ascii=False)[:600]])
    art = str(got.get("artifact") or "")
    if not art.startswith("/"):
        raise AuthoringError("渲染没有返回产物路径")
    return await asyncio.to_thread(_fetch_bytes, _remotion_url(art), 120.0)


def _blank_reason(stills: Sequence[Mapping[str, Any]]) -> str:
    """预览帧「是不是空的」：JPEG base64 过小＝几乎纯色。

    这不是审美判断，只是拦住「组件编译通过、渲染成功、画面一片黑」这种最常见的
    静默失败。真正的像不像由人看预览帧决定。
    """
    sizes = [len(str(s.get("jpeg_b64") or "")) for s in stills]
    if sizes and max(sizes) < 4000:
        return f"预览帧几乎是空白（最大 {max(sizes)} 字节 base64）"
    return ""


async def determinism_error(code: str, *, tag: str) -> str:
    """同码两渲逐字节比较。不同就返回原因，相同返回空串。"""
    nonce = hashlib.sha256(f"{tag}:{code}".encode("utf-8")).hexdigest()[:12]
    key_a = f"tpl-{nonce}-a"[:64]
    key_b = f"tpl-{nonce}-b"[:64]
    a = await render_clip(code, key=key_a)
    b = await render_clip(code, key=key_b)
    if hashlib.sha256(a).digest() != hashlib.sha256(b).digest():
        return ("同一份模板两次渲染的字节不同：画面里有按墙钟/随机变化的写法，"
                "或用了透明叠加。把变化改成 useCurrentFrame() 的确定性函数，"
                "颜色用实色而不是 opacity。")
    return ""


# ---------------------------------------------------------------- 名字与发布


def template_name(user_id: str, slug: str) -> str:
    """用户模板的全局唯一名：`u-<用户 8 位>-<slug>`。

    模板表的主键是 name（系统模板与用户模板同表），不带用户前缀时两个人都叫
    `my-style` 会互相覆盖。前 8 位足够区分（完整 user_id 是 12 位十六进制）。
    """
    clean = re.sub(r"[^a-z0-9-]+", "-", str(slug or "").strip().lower()).strip("-")
    clean = re.sub(r"-{2,}", "-", clean)[:20].strip("-")
    if not _SLUG_RE.match(clean):
        raise AuthoringError(
            "模板短名不合法：要小写字母开头，字母数字与 `-`，2~20 位（例：firelight-odyssey）")
    uid = re.sub(r"[^a-z0-9]+", "", str(user_id or "").lower())[-8:] or "anon"
    name = f"u-{uid}-{clean}"
    if not _NAME_RE.match(name):
        raise AuthoringError(f"分配出的模板名不合法：{name}")
    return name


def resolve_slug(user_id: str, raw: str, *, hint: str = "") -> str:
    """用户填的名字 → 合法内部短名。

    短名只是模板的主键前缀，不是给用户看的名字（展示名是分析出来的 `display`）。
    所以中文/空格/符号不该打回用户：能提炼出合法 ASCII 就用它，否则按
    「用户 + 原名」生成一个稳定的 `style-xxxxxx`——同一个名字再沉淀一次仍落到
    同一份模板上，而不是每次生成新名字。
    """
    clean = re.sub(r"[^a-z0-9-]+", "-", str(raw or "").strip().lower()).strip("-")
    clean = re.sub(r"-{2,}", "-", clean)[:20].strip("-")
    if _SLUG_RE.match(clean):
        return clean
    seed = f"{user_id}|{raw}|{hint}".encode("utf-8")
    return "style-" + hashlib.sha256(seed).hexdigest()[:6]


def build_slots(analysis: Mapping[str, Any], source: str) -> list[dict[str, Any]]:
    """分析 + 模板源码 → 槽位清单（形状与 `style_templates.template_slots` 一致）。

    每个导出组件是一个槽位：`contract` 从**源码里的 props 类型**读，不靠模型自述；
    标签与几何来自画面拆解（`layers`）。没有落进任何一层的导出组件也列出来，
    只是 kind=other——「所有组件都能换」不能漏掉配角。
    """
    props = st.component_props(source) if source else {}
    roles = {str(c.get("name") or ""): str(c.get("role") or "")
             for c in (analysis.get("components") or [])
             if isinstance(c, Mapping) and c.get("name")}
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(comp: str, *, label: str = "", kind: str = "other",
            default: str = "", varies: Any = ()) -> None:
        if not comp or comp in seen:
            return
        seen.add(comp)
        text = props.get(comp, "")
        slots.append({
            "component": comp,
            "label": label or roles.get(comp) or comp,
            "kind": kind,
            "default": default,
            "varies": [str(v) for v in (varies or []) if str(v).strip()],
            "contract": st.prop_names(text),
            "contract_text": text,
            "variants": [],
        })

    # 一个组件常常负责画面里的好几层（天空、远山、地面都画在同一个背景组件里）。
    # 只拿第一层的名字当标签，用户会以为「换背景」只能换天空——按组件把层名
    # 聚合起来，标签和默认画法才说得清这个零件到底管什么。
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for layer in analysis.get("layers") or []:
        if not isinstance(layer, Mapping):
            continue
        comp = str(layer.get("component") or "").strip()
        if not comp:
            continue
        name = str(layer.get("name") or "").strip()
        kind = str(layer.get("kind") or "other")
        default = str(layer.get("default") or "").strip()
        varies = [str(v) for v in (layer.get("varies") or []) if str(v).strip()]
        got = grouped.get(comp)
        if got is None:
            grouped[comp] = {"parts": [(name, default)], "kind": kind, "varies": varies}
            order.append(comp)
            continue
        if name and all(n != name for n, _ in got["parts"]):
            got["parts"].append((name, default))
        if got["kind"] in ("", "other") and kind not in ("", "other"):
            got["kind"] = kind
        for v in varies:
            if v not in got["varies"]:
                got["varies"].append(v)
    for comp in order:
        got = grouped[comp]
        names = [n for n, _ in got["parts"] if n][:4]
        defaults = [f"{n}：{d}" if n and d else (n or d)
                    for n, d in got["parts"][:4] if n or d]
        add(comp, label="、".join(names),
            kind=str(got["kind"] or "other"),
            default="；".join(defaults)[:240],
            varies=got["varies"])
    for comp in roles:
        add(comp)
    return slots


def style_skill_payload(*, name: str, analysis: Mapping[str, Any],
                        slots: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """模板 → 风格包技能行（用户在前端「风格」下拉里看到的就是它）。"""
    manifest = dict(analysis.get("manifest") or {})
    manifest["template"] = name
    # 画面拆解随风格包一起存：运行时能看出「这一层本来该由模板画」，
    # 也方便前端/回执核对，不再只存在一次性的分析结果里。
    if analysis.get("layers"):
        manifest["layers"] = list(analysis["layers"])
    # 参考片的时间线（什么先出现、怎么动、何时切）也随风格包走：逐镜写手靠它
    # 复刻编排，后端不解释里面的字段。
    if analysis.get("timeline"):
        manifest["timeline"] = list(analysis["timeline"])
    # 读片模型自己加的观察块（键名它定）：后端不认识，但写模板/写镜的人可能用得上，
    # 原样带下去，删掉就等于把多看出来的一层扔了。
    if analysis.get("blocks"):
        manifest["blocks"] = dict(analysis["blocks"])
    # 槽位与替换件也随风格包存一份：规划/执行轮的短提示只读技能行，
    # 不必再为「有哪些可换件」单独查一次模板库。
    if slots:
        manifest["slots"] = [dict(s) for s in slots]
    return {
        "display": analysis.get("display") or name,
        "description": analysis.get("description") or "",
        "body": analysis.get("prose") or "",
        "frontmatter": {
            "name": name,
            "display": analysis.get("display") or name,
            "kind": "style",
            "description": analysis.get("description") or "",
            "style_manifest": manifest,
        },
    }


async def publish(storage: Any, *, name: str, owner_user_id: str,
                  analysis: Mapping[str, Any], source: str,
                  validation: Mapping[str, Any]) -> dict[str, Any]:
    """模板行 + 风格包技能行同一次写入；再推一遍正式名。"""
    defaults = dict(analysis.get("manifest") or {})
    if analysis.get("layers"):
        defaults["layers"] = list(analysis["layers"])
    if analysis.get("timeline"):
        defaults["timeline"] = list(analysis["timeline"])
    if analysis.get("blocks"):
        defaults["blocks"] = dict(analysis["blocks"])
    slots = build_slots(analysis, source)
    if slots:
        defaults["slots"] = slots
    manifest = {"responsible": analysis.get("responsible") or "",
                "defaults": defaults}
    row = await storage.style_templates.get(name)
    if row is not None and row.get("owner_user_id") not in (None, owner_user_id):
        raise AuthoringError("同名模板属于别人，换一个短名")
    if row is not None and row.get("owner_user_id") is None:
        raise AuthoringError("系统内置模板只读，换一个短名")
    # 替换件挂在基模板的 manifest.variants 上，而 defaults 每次重沉淀都会重建：
    # 不在这里接回来，用户沉淀一版新模板就等于把之前做的所有换件悄悄删掉。
    if row is not None:
        prev = row.get("manifest") if isinstance(row.get("manifest"), Mapping) else {}
        variants = prev.get("variants")
        if isinstance(variants, list) and variants:
            manifest["variants"] = list(variants)
    # 预览件必须 import **正式名**：过闸时用的是临时名（staging），发布后会删掉，
    # 把临时名的 probe 存进库，之后每次预览/复核都会 Module not found。
    final_probe = probe_shot_code(f"{st.USER_MODULE_PREFIX}{name}")
    values = {
        "display": analysis.get("display") or name,
        "description": analysis.get("description") or "",
        "module": f"{st.USER_MODULE_PREFIX}{name}",
        "components": list(analysis.get("components") or []),
        "source": source,
        "manifest": manifest,
        "probe": final_probe,
        "status": "validated",
        "owner_user_id": owner_user_id,
        "validation": dict(validation),
    }
    if row is None:
        saved = await storage.style_templates.upsert(name, **values)
    else:
        await storage.style_templates.bump_version(
            name, source=source, probe=final_probe, manifest=manifest,
            validation=dict(validation), status="validated")
        saved = await storage.style_templates.revise(
            name, display=values["display"], description=values["description"],
            module=values["module"], components=values["components"])
    await push_template(name, source)
    skill = style_skill_payload(name=name, analysis=analysis, slots=slots)
    await storage.skills.upsert(
        name, skill["body"], description=skill["description"],
        frontmatter=skill["frontmatter"], owner_user_id=owner_user_id)
    return {"template": saved, "skill": skill}


# ---------------------------------------------------------------- 主流程


async def author_template(
    storage: Any, *,
    video: str | Path | None = None,
    videos: Sequence[str | Path] | None = None,
    user_id: str,
    slug: str,
    note: str = "",
    name_hint: str = "",
    llm: Any = None,
    identity_user: str = "",
    conversation_id: str = "",
) -> dict[str, Any]:
    """一条或几条同风格参考片 → 一套已发布的模板 + 预览帧。

    多条参考片时每个视频各占一组帧、一起交给视觉模型，用来提炼**共同**语法；
    取帧失败的那条会被跳过并记进失败原因，不会因为一条坏片拖垮整次沉淀。

    ``llm`` 缺省取默认客户端；``identity_user`` 是解析 API Key 用的用户
    （app 进程里就是当前登录用户，与 ``user_id`` 相同）。
    """
    if llm is None:
        from .llm_openai import get_default_llm
        llm = get_default_llm()
    paths = [Path(v) for v in (videos if videos is not None
                               else ([] if video is None else [video]))]
    if not paths:
        raise AuthoringError("没有参考片：先上传视频再沉淀")
    if len(paths) > MAX_REFERENCE_VIDEOS:
        raise AuthoringError(
            f"一次最多 {MAX_REFERENCE_VIDEOS} 条参考片（收到 {len(paths)} 条）")
    name = template_name(user_id, slug)
    staging = f"{name[:36]}-d{hashlib.sha256((name + str(user_id)).encode()).hexdigest()[:3]}"
    metas = [await asyncio.to_thread(probe_video, p) for p in paths]
    counts = frame_counts(len(paths))
    attempts = 0
    detail: list[str] = []

    with tempfile.TemporaryDirectory(prefix="style-author-") as tmp:
        groups: list[list[Path]] = []
        used_times: list[list[float | None]] = []
        used_shots: list[list[tuple[float, float]]] = []
        used_metas: list[dict[str, Any]] = []
        for i, (path, meta, count) in enumerate(zip(paths, metas, counts)):
            # 先找切点再取帧：等距取帧会漏掉快切里的整段主体。
            shots = await asyncio.to_thread(
                detect_shots, path, meta["duration"])
            times, shots = pick_shot_times(meta["duration"], count, shots)
            try:
                got_frames = await asyncio.to_thread(
                    extract_frames, path, Path(tmp) / f"v{i:02d}", times)
            except AuthoringError as exc:
                if len(paths) == 1:
                    raise
                detail.append(f"第 {i + 1} 条参考片取帧失败，已跳过：{exc}")
                continue
            groups.append(got_frames)
            # 每张图带上它在参考片里的秒数：模型才能写出「第几秒出现、动多久、
            # 何时切」的时间线，而不是只罗列「有什么」。
            used_times.append(_frame_times(got_frames, times))
            used_shots.append(shots)
            used_metas.append({**meta, "frames": len(got_frames),
                               "shots": len(shots)})
        frames = [p for group in groups for p in group]
        if len(frames) < 3:
            raise AuthoringError("参考帧取不出来（至少 3 帧）", detail=detail,
                                 attempts=attempts)
        analysis: dict[str, Any] | None = None
        analysis_err = ""
        for _ in range(MAX_ANALYSIS_ATTEMPTS):
            attempts += 1
            try:
                resp = await _complete(llm, build_analysis_messages(
                    frames, note=note, prior_error=analysis_err,
                    name_hint=name_hint, groups=groups,
                    times=used_times or None,
                    shot_groups=used_shots or None),
                    identity_user=identity_user or user_id,
                    conversation_id=conversation_id,
                    max_tokens=ANALYSIS_MAX_TOKENS, timeout=240.0)
                analysis = parse_style_analysis(
                    str(getattr(resp, "content", "") or ""), require_layers=True)
                break
            except AuthoringError as exc:
                analysis_err = str(exc)
                detail.append(f"读片：{exc}")
            except Exception as exc:  # noqa: BLE001 - 缺密钥/网络失败也要变成可读原因
                # 不把这类失败留给全局 500：用户看到「读不出视觉规范」加一行原因，
                # 远比一个没有上下文的 Internal Server Error 有用。
                analysis_err = f"读片调用模型失败：{str(exc)[:200]}"
                detail.append(analysis_err)
        if analysis is None:
            raise AuthoringError("读不出这条参考片的视觉规范", detail=detail,
                                 attempts=attempts)

        module = f"{st.USER_MODULE_PREFIX}{staging}"
        probe_src = probe_shot_code(module)
        errors: list[str] = []
        source = ""
        stills: list[dict[str, Any]] = []
        # 保真评审是模型的主观比对（参考帧来自片子不同时刻，构图/文字本来就会变），
        # 只让它按差异清单重写一次；第二次仍不过就**带差异发布**。
        # 堵死比白烧更坏：结构闸/lint/空白/确定性都过了的模板，交给用户看预览帧再决定，
        # 比反复重写四版最后一行不落库更有用。差异写进 validation 与返回值。
        fidelity_rounds = 0
        fidelity_notes: list[str] = []
        for _ in range(MAX_CODE_ATTEMPTS):
            attempts += 1
            try:
                code_kwargs: dict[str, Any] = {"max_tokens": 12000,
                                               "timeout": 300.0}
                if _template_model():
                    code_kwargs["model_override"] = _template_model()
                resp = await _complete(llm, build_code_messages(
                    analysis, module=module, prior_errors=errors, frames=frames),
                    identity_user=identity_user or user_id,
                    conversation_id=conversation_id, **code_kwargs)
                source = _strip_fence(str(getattr(resp, "content", "") or ""))
            except Exception as exc:  # noqa: BLE001 - 模型侧失败也进重试清单
                errors = [f"写模板时模型调用失败：{str(exc)[:300]}"]
                detail.append(errors[0])
                continue
            if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                errors = [f"模板源码超过 {MAX_SOURCE_BYTES // 1024}KB"]
                detail.append(errors[0])
                continue
            errors = (check_exports(source, analysis["components"])
                      + check_self_contained(source, analysis["components"])
                      + check_jsx_text_escapes(source)
                      + check_motion_contract(source, analysis)
                      + check_facets(source, analysis))
            if not errors:
                try:
                    errors = await lint_template(source)
                except Exception as exc:  # noqa: BLE001 - 容器不可达也是可重试原因
                    errors = [f"模板闸不可用：{str(exc)[:200]}"]
            if errors:
                detail.extend(errors)
                continue
            try:
                await push_template(staging, source)
                stills = await render_stills(probe_src)
                blank = _blank_reason(stills)
                if blank:
                    errors = [blank]
                    detail.append(blank)
                    continue
                # 确定性要验的是**模板渲出来的画面**，不是模板源码本身：
                # render-shot 收到的是镜头代码（必须有 default 导出），把模板源码
                # 直接丢过去会以「缺镜头导出」400，看上去却像环境坏了。
                det = await determinism_error(probe_src, tag=staging)
                if det:
                    errors = [det]
                    detail.append(det)
                    continue
                # 保真评审：语法/空白/确定性都过，不代表画得像参考片。拿零参数渲染帧
                # 对参考帧逐层核对；差异回写成下一版的修改清单。评审本身失败不拦——
                # 「网关读不出结论」不该把一条已经渲得出来的沉淀判死（堵死比白烧更坏）。
                if frames and analysis.get("layers"):
                    try:
                        review = await _complete(
                            llm, build_fidelity_messages(analysis, frames, stills),
                            identity_user=identity_user or user_id,
                            conversation_id=conversation_id,
                            max_tokens=1200, timeout=120.0)
                        ok, diffs = parse_fidelity(
                            str(getattr(review, "content", "") or ""))
                    except AuthoringError as exc:
                        ok, diffs = True, []
                        detail.append(f"保真评审没读出结论，已跳过：{exc}")
                    except Exception as exc:  # noqa: BLE001 - 缺密钥/网络失败不拦
                        ok, diffs = True, []
                        detail.append(f"保真评审调用失败，已跳过：{str(exc)[:200]}")
                    if not ok:
                        errors = ["零参数预览帧与参考片不符：", *diffs]
                        detail.extend(diffs)
                        fidelity_notes = list(diffs)
                        if fidelity_rounds >= 1:
                            detail.append(
                                "保真评审仍有差异，按当前模板发布（预览帧可在前端查看）")
                            break
                        fidelity_rounds += 1
                        continue
                break
            except AuthoringError as exc:
                errors = [str(exc), *exc.detail]
                detail.extend(errors)
            except Exception as exc:  # noqa: BLE001 - 容器/网络错误也要进重试清单
                # 原先这里只接 AuthoringError，`_http` 抛的 HTTPError/RuntimeError
                # 会一路冒到接口变成没有原因的 500。闸的失败必须变成可读原因。
                errors = [f"模板闸调用失败：{str(exc)[:300]}"]
                detail.extend(errors)
        else:
            await drop_template(staging)
            raise AuthoringError("模板没通过闸（已尝试 " + str(MAX_CODE_ATTEMPTS) + " 版）",
                                 detail=detail, attempts=attempts)

        validation = {"attempts": attempts, "videos": used_metas,
                      "frames": len(frames), "stills": len(stills),
                      "determinism": "byte-identical",
                      "fidelity": "warn" if fidelity_notes else "ok",
                      "fidelity_notes": fidelity_notes[:8],
                      "source_bytes": len(source.encode("utf-8"))}
        try:
            saved = await publish(
                storage, name=name, owner_user_id=user_id, analysis=analysis,
                source=source, validation=validation)
        finally:
            await drop_template(staging)

    return {
        "ok": True, "name": name, "display": analysis["display"],
        "description": analysis.get("description") or "",
        "module": f"{st.USER_MODULE_PREFIX}{name}",
        "components": list(analysis["components"]),
        "manifest": dict(analysis["manifest"]),
        "prose": analysis["prose"],
        "references": len(used_metas),
        "stills": stills,
        "attempts": attempts,
        "validation": validation,
        "template": saved.get("template") if isinstance(saved, Mapping) else None,
    }


async def _complete(llm: Any, messages: list[dict[str, Any]], *,
                    identity_user: str, conversation_id: str, **kwargs: Any) -> Any:
    """按用户身份解析模型键。contextvar 不过 HTTP，这里显式绑。"""
    from .identity import use_identity
    if not identity_user:
        return await llm.complete(messages, **kwargs)
    with use_identity(identity_user, conversation_id):
        return await llm.complete(messages, **kwargs)


#: 给前端/接口用的可见模板视图（不泄露别人的源码）。
def public_row(row: Mapping[str, Any], *, include_source: bool = False) -> dict[str, Any]:
    out = {
        "name": row.get("name") or "",
        "display": row.get("display") or row.get("name") or "",
        "description": row.get("description") or "",
        "module": row.get("module") or "",
        "components": [str(c.get("name") if isinstance(c, Mapping) else c)
                       for c in (row.get("components") or [])],
        "status": row.get("status") or "",
        "owner_user_id": row.get("owner_user_id"),
        "system": row.get("owner_user_id") is None,
        "version": int(row.get("version") or 1),
        "hash": st.source_hash(row),
        "validation": dict(row.get("validation") or {}),
    }
    # 替换件：前端在列表里标出「谁的哪个槽位」，不必展开源码。
    variant_of = (row.get("manifest") or {}).get("variant_of")
    if isinstance(variant_of, Mapping) and variant_of.get("template"):
        out["variant_of"] = {
            "template": str(variant_of.get("template") or ""),
            "component": str(variant_of.get("component") or ""),
            "label": str(variant_of.get("label") or ""),
        }
    # 槽位清单：前端「换件」面板照这个列可替换零件与已经登记的替换件。
    # 只回展示需要的字段，合约正文留在库里——清单不该因为带源码变重。
    try:
        slots = st.template_slots(row)
    except Exception:  # noqa: BLE001 - 元数据形状异常不该拖垮整份清单
        slots = []
    slots_out = []
    for s in slots:
        if not isinstance(s, Mapping):
            continue
        slots_out.append({
            "component": str(s.get("component") or ""),
            "label": str(s.get("label") or s.get("component") or ""),
            "kind": str(s.get("kind") or "other"),
            "default": str(s.get("default") or ""),
            "varies": [str(v) for v in (s.get("varies") or [])],
            # 参考片里出现过的内容取值：前端显示「现在能画：山、平地」，
            # 用户点零件时不必猜这个零件到底能变出什么。
            "content": [
                {"prop": str(c.get("prop") or ""),
                 "values": [str(x) for x in (c.get("values") or [])],
                 "reference": [str(x) for x in (c.get("reference") or [])]}
                for c in (s.get("content") or []) if isinstance(c, Mapping)
            ],
            "contract": [str(p) for p in (s.get("contract") or [])],
            "variants": [
                {"name": str(v.get("name") or ""),
                 "label": str(v.get("label") or ""),
                 "module": str(v.get("module") or "")}
                for v in (s.get("variants") or []) if isinstance(v, Mapping)
            ],
        })
    if slots_out:
        out["slots"] = slots_out
    if include_source:
        out["source"] = str(row.get("source") or "")
        out["manifest"] = dict(row.get("manifest") or {})
        out["probe"] = str(row.get("probe") or "")
    return out
