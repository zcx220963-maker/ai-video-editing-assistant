# -*- coding: utf-8 -*-
"""视频 → 静帧：给写手与评审当**看图**的输入。

2026-10-24 从 `style_authoring.py` 里拆出来的：那几个函数是通用的视频抽帧工具，
与「风格包沉淀」没有关系。风格包通道整条删除之后，只有这一节还需要留下。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

#: 一次取几帧（与沉淀那边同一口径）。
FRAME_COUNT = 12
#: 一条片子最多用几条参考视频。
MAX_REFERENCE_VIDEOS = 4
#: 多条参考视频合计取几帧。
MULTI_FRAME_TOTAL = 12
#: 片头片尾各跳过的比例。
_EDGE_SKIP = 0.06
#: 探片用的采样帧率与帧数。
PROBE_FPS = 30
PROBE_FRAMES = 90

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


