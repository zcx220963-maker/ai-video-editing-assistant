# -*- coding: utf-8 -*-
"""闸㉗ 揭示-时序像素一致（R12-T61）：**未开始的区域，画面上必须还是纸底**。

这条闸判的是「申报的时序」与「像素」对不对得上，判它不需要任何模型、也不需要判断：

* 抽帧时刻 `t`；`start_ms > t` 的块，它的**允许掩码**（块 − 后续块 − protected）里的
  采样点必须仍是**纸底色**；
* `start_ms + duration_ms ≤ t` 的块必须**已经显示**（允许掩码内不能全是纸底）。

三出口照 §3 的规矩：**通过 / 打回 / 未判**（没申报这一族、或底色取样失败都记未判，
**不是绿**）。

**为什么它不可替代（本卷最重要的一段）。** 这一族风格的画面主体是一整幅 raster 插画，
而几何族（⑲-a 的 `subject_share`、㉕ 的徽章构图）在满幅插画下会**自动放行**：
`img` 的占比天然 ≈1，而 16:9 的插画长边/短边 1.78 > 1.35 不触发徽章那四条。于是
「一张静图配字幕」在这一族里没有任何一条既有闸会响——㉗ 是唯一一条**只吃像素、
不吃申报**的判据：它把「什么时候该露出什么」从形容词变成了一次比色。

**三条口径，写在最前面：**

1. **比色基准永远是「原图四角内缩取样」**，不写死任何常量（Q118）。所谓「假绿夹」
   （把基准换成纯白就会全绿）在这条实现里**没有入口**：基准只能从插画字节里量出来，
   量不到就记未判——这是结构性保证，不是靠断言守的。
2. **几何只在母版里换算一次**。哪一块落在画面上的哪个矩形，由母版按它这一帧的
   实况盒算出来并回读（`DG_REVEAL:` 行）；这一层**不自己算**（算了就是两把尺）。
   本层只做两件母版做不了的事：把矩形挪到帧坐标系（加画面带原点）、去抽那一帧比色。
3. **无图像库依赖**。与闸④ 同一条路：全程 ffmpeg → `rawvideo` 字节 → 纯 Python 取点。
   `Pillow` 不在这个镜像的依赖里，为了比几个色点把它引进来是给整条链加一条长期依赖。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import mediaops
from . import code_spec as cspec

#: 纸底色与「已显示」的判据：CIE76 的 ΔE。
#:
#: **为什么取 6**：下界是压缩噪声——H.264 在平坦区会把纸底抖出 ΔE 1~3（⑭/㉑ 那几趟
#: 抽帧的经验值）；上界是内容——`scripts/reveal_gate.py` 把这张合成图上量到的两个数
#: 印出来：纸-墨 69.8、纸-纯白 12.8（连最浅的「内容」都在阈值之上）。6 夹在噪声与
#: 内容之间，离两端各留一倍以上。**这个数是判据选型、不是量出来的**，与
#: `LAYOUT_PROBE.overlap_ratio` 同族：改它必须同时改 `scripts/reveal_gate.py` 的
#: 那一组夹具，否则它只是一句好看的话。真机复核（T64）之后它会跟基线一起被重新定值。
PAPER_DELTA_E_MAX = 6.0

#: 四角取样彼此之间允许的最大 ΔE：超过它说明「这份底色基准自己就不一致」
#: （四角画着不同的东西、或图被裁过），此时**整条判据的基准不成立** → 未判。
#: 这一条是 T61 那支「基准自己错了」的反例在实现里的正面形态。
CORNER_AGREE_DELTA_E_MAX = 10.0

#: 四角内缩比例与取样块边长（原图像素的比例）。内缩是为躲开图片自身的描边/圆角，
#: 与对面那条「从原图四角内缩取样」的规矩同一个意思。
CORNER_INSET = 0.06
CORNER_PATCH = 0.06

#: 一块区域里按几乘几的网格取点。3×3 是这个问题的下限之下再留一点余量：
#: 一块区域里只要**有一个**采样点不是纸底，就算「已经显示」；网格太稀会把一块
#: 只有窄笔画的区域读成空（假红），太密则是在同一片平坦色上反复取同一个数。
GRID_N = 3

#: 取点时再往里缩一点（矩形边长的比例）：掩码前沿正好压在边上，压着边取会把
#: 抗锯齿的半透明像素读成一个既不像纸也不像内容的值。
GRID_INSET = 0.18


# ---------------------------------------------------------------------------
# 颜色：sRGB → Lab → ΔE(CIE76)
# ---------------------------------------------------------------------------

def _srgb_to_linear(v: float) -> float:
    v = v / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: Sequence[float]) -> tuple[float, float, float]:
    """sRGB（0~255）→ CIE Lab（D65）。整数实现，不引 numpy。"""
    r, g, b = (_srgb_to_linear(float(c)) for c in rgb[:3])
    # sRGB → XYZ（D65），再做白点归一
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = (r * 0.2126729 + g * 0.7151522 + b * 0.0721750) / 1.00000
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a: Sequence[float], b: Sequence[float]) -> float:
    """两个 sRGB 色的 CIE76 色差。"""
    la, lb = rgb_to_lab(a), rgb_to_lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5


def _avg(px: Sequence[bytes]) -> tuple[float, float, float]:
    n = len(px) or 1
    return (sum(p[0] for p in px) / n, sum(p[1] for p in px) / n, sum(p[2] for p in px) / n)


# ---------------------------------------------------------------------------
# 抽帧与取点（全程 ffmpeg → rawvideo；**不引图像库**）
# ---------------------------------------------------------------------------

def frame_rgb(clip: Path, *, at_sec: float, size: tuple[int, int]) -> bytes:
    """抽一帧 → `w*h*3` 字节的 RGB24。

    与 `code_render._frame_diff_stats` 同一条路（那一处的方法学与踩过的坑都写在那儿）：
    先 `-ss` 再 `-i`（快速定位），只取一帧，落 rawvideo 到内存外的临时文件。
    字节数对不上就抛——半帧的字节流会让后面每一个采样点都取到别的像素上，
    而那是**看起来正常的错读数**，比抛错坏得多。
    """
    w, h = int(size[0]), int(size[1])
    out = clip.parent / f".reveal_frame_{int(at_sec * 1000)}.raw"
    mediaops.ffmpeg("-ss", f"{max(0.0, at_sec):.3f}", "-i", str(clip),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-y", str(out), timeout=120)
    data = out.read_bytes()
    try:
        out.unlink()
    except OSError:
        pass
    if len(data) != w * h * 3:
        raise mediaops.MediaError(
            f"闸㉗ 抽帧失败：期望 {w * h * 3} 字节（{w}×{h} RGB24），得到 {len(data)}")
    return data


def sample_points(data: bytes, size: tuple[int, int],
                  rects: Sequence[Sequence[float]]) -> list[tuple[float, float, float]]:
    """一组矩形 → 每个矩形内 `GRID_N × GRID_N` 个采样点的色（越界的点丢掉）。

    矩形是**帧坐标系**（左上角原点）。「越界的点丢掉」而不是夹到边上：
    夹边会把画面外的采样点算成画面边缘的颜色，而边缘往往是母版的底色或描边——
    一个越界的申报于是会被读成「纸底」，也就是那条判据自己把自己判绿了。
    """
    w, h = int(size[0]), int(size[1])
    out: list[tuple[float, float, float]] = []
    for r in rects:
        x, y, rw, rh = (float(v) for v in r[:4])
        if rw <= 0 or rh <= 0:
            continue
        ix, iy = rw * GRID_INSET, rh * GRID_INSET
        x0, y0 = x + ix, y + iy
        gw, gh = rw - 2 * ix, rh - 2 * iy
        for gi in range(GRID_N):
            for gj in range(GRID_N):
                px = x0 + gw * (gi + 0.5) / GRID_N
                py = y0 + gh * (gj + 0.5) / GRID_N
                cx, cy = int(px), int(py)
                if cx < 0 or cy < 0 or cx >= w or cy >= h:
                    continue
                o = (cy * w + cx) * 3
                out.append((float(data[o]), float(data[o + 1]), float(data[o + 2])))
    return out


# ---------------------------------------------------------------------------
# 纸底色：从**插画原图**四角内缩取样（Q118：不写死常量）
# ---------------------------------------------------------------------------

def paper_from_image(img: Path) -> tuple[tuple[float, float, float] | None, str]:
    """插画原图 → (纸底色, 理由)。**基准只能从这里来。**

    做法：四个角各裁一块内缩后的小patch（各 `CORNER_PATCH` 边长），一次 ffmpeg 横拼成
    一条 → 读成 RGB24 → 每一块取平均。四块彼此差超过 `CORNER_AGREE_DELTA_E_MAX` 就
    退回 `(None, 理由)`：那说明这份基准自己不一致（四角画着不同的东西），而拿一个
    不一致的基准去比色，会得到一条「每一镜都红」或「每一镜都绿」的判据。

    **为什么不是「取整图平均」或「取左上角一个点」**：整图平均会被内容拉向灰；
    单点会被一颗噪点、一条描边定死整条判据。内缩 + 小块平均 + 四角彼此对账，
    是「这条基准可以自己证明自己」的最小组合。
    """
    # 四个角各裁一块 → 各自 `scale=16:16`（**缩放本身就是在那块里取平均**，
    # 比手工算平均更少一处实现）→ 横拼成 64×16 的一张 → 读成 RGB24。
    #
    # **为什么必须用 `filter_complex` 而不是一串 `-vf "crop=…,crop=…"`**：
    # `-vf` 里逗号连接是**流水线**——第二个 crop 的 `iw/ih` 是第一个 crop **输出**的
    # 尺寸，偏移也相对它。那样裁出来的根本不是四个角，是左上角里再裁一小块，
    # 而且**画面看起来仍然是一张正常的底色**（四块全是同一个位置的颜色），
    # ΔE 会互相吻合、判据照常放行。这是这条判据里最容易写错、且错了不自知的一处。
    patch = CORNER_INSET
    far = 1 - CORNER_INSET - CORNER_PATCH
    box = f"crop=iw*{CORNER_PATCH}:ih*{CORNER_PATCH}"
    parts = [
        f"[0:v]{box}:iw*{patch}:ih*{patch},scale=16:16[c1]",
        f"[0:v]{box}:iw*{far}:ih*{patch},scale=16:16[c2]",
        f"[0:v]{box}:iw*{patch}:ih*{far},scale=16:16[c3]",
        f"[0:v]{box}:iw*{far}:ih*{far},scale=16:16[c4]",
        "[c1][c2][c3][c4]hstack=inputs=4[out]",
    ]
    out = img.parent / ".reveal_paper.raw"
    try:
        mediaops.ffmpeg("-i", str(img), "-filter_complex", ";".join(parts),
                        "-map", "[out]", "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-y", str(out), timeout=120)
        data = out.read_bytes()
    except Exception as e:      # noqa: BLE001 —— 量不到基准是「未判」，不是这一镜的错
        return None, f"原图四角取样失败（{type(e).__name__}: {str(e)[:80]}）"
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    # 读回来的是 `hstack` 的 64×16：**四块在同一行的左右**，于是字节序是
    # 「第 0 行：块1 块2 块3 块4，第 1 行：块1 块2 块3 块4……」，**不是**「块1 全读、
    # 再块2 全读」。按 768 字节切会把四块各取到四分之一、再把它们平均——四角于是
    # 永远「彼此一致」，那条对账永远绿。第一次跑这份代码就是栽在这里：
    # 四角画着四种颜色的合成图，量出来的是**四种颜色的平均** (130,125,135)，
    # 而四块一模一样。所以这一段必须按行去交错，且要有这条注释拦着后来的人。
    w16, h16 = 16, 16
    row_bytes = w16 * 4 * 3
    if len(data) != row_bytes * h16:
        return None, (f"原图四角取样的字节数不对（得到 {len(data)}，应为 {row_bytes * h16}）"
                      f"——这份基准不可用")
    tiles: list[list[bytes]] = [[], [], [], []]
    for r in range(h16):
        row = data[r * row_bytes:(r + 1) * row_bytes]
        for t in range(4):
            seg = row[t * w16 * 3:(t + 1) * w16 * 3]
            tiles[t].extend(seg[j:j + 3] for j in range(0, len(seg), 3))
    corners = [_avg(px) for px in tiles]
    worst = 0.0
    worst_pair = ""
    for i in range(len(corners)):
        for j in range(i + 1, len(corners)):
            d = delta_e(corners[i], corners[j])
            if d > worst:
                worst, worst_pair = d, f"{i + 1}-{j + 1}"
    if worst > CORNER_AGREE_DELTA_E_MAX:
        return None, (f"原图四角取样的底色彼此不一致（第 {worst_pair} 角差 ΔE {worst:.1f} "
                      f"> {CORNER_AGREE_DELTA_E_MAX:g}）——这份基准不能用，"
                      f"这一镜的 ㉗ **没判过**（未判不是通过）")
    return _avg(corners), ""


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

def _region_plan(shot: Mapping[str, Any], frames: int, fps: int) -> dict[int, list[dict[str, Any]]]:
    """这一镜要在**哪几帧**、拿**哪些块**去比色（帧号 → 该帧的判据清单）。

    第 `f` 帧上的清单来自两块：
    * **该显示的**：`start_ms + duration_ms ≤ t` 的块（它们的允许掩码里必须有内容）；
    * **不该显示的**：`start_ms > t` 的块（它们的允许掩码里必须全是纸底）。

    两句话在同一个时刻上判完，是 `code_spec.reveal_probe_frames` 那条设计的一半
    （另一半是「只要 n 个时刻就够」）。**这里不重算帧号**，直接读 Python 侧算好、
    注入给母版的那一份（`revealProbeFrames` 由同一份申报算出），两处各算一遍就会
    出现「母版报了 A 帧、这里等 B 帧」的错位。
    """
    raw = shot.get("reveal")
    if not isinstance(raw, Mapping):
        return {}
    regions = [r for r in (raw.get("regions") or []) if isinstance(r, Mapping)]
    out: dict[int, list[dict[str, Any]]] = {}
    for f in cspec.reveal_probe_frames(shot, frames=frames, fps=fps):
        t_ms = f / fps * 1000 if fps else 0.0
        rows: list[dict[str, Any]] = []
        for r in regions:
            try:
                start = int(r.get("start_ms"))
                dur = int(r.get("duration_ms"))
            except (TypeError, ValueError):
                continue
            rid = str(r.get("id") or "?")
            if start > t_ms:
                rows.append({"id": rid, "want": "paper"})
            elif start + dur <= t_ms:
                rows.append({"id": rid, "want": "drawn"})
        out[f] = rows
    return out


async def check_reveal(sid: str, shot: Mapping[str, Any], *, gate: Mapping[str, Any],
                       band: tuple[int, int, int, int] | None, clip: Path,
                       size: tuple[int, int], fps: int, frames: int,
                       storage: Any, work: Path) -> tuple[list[str], list[str]]:
    """闸㉗ → (打回, 未判)。**排在这一镜切片进缓存之前**（红了不烧第二趟）。

    五个前置条件，每一个对应一种「未判」而不是「通过」：

    1. 这一镜没申报 `reveal` → **不适用**（不是未判，是这一格没有它的事）；
    2. 母版没回读 `DG_REVEAL:` 行 → 未判（母版旧、或这一族没跑起来）；
    3. 纸底色取不到（原图字节读不到、四角不一致）→ 未判，**理由原样带上**；
    4. 画面带原点读不到（token 里没有 `gutter/topSafe`）→ 未判：没有它就没法把
       母版报的「带内坐标」挪到帧坐标系，硬猜一个原点是拿错位的像素做判据；
    5. 抽帧失败 → 抛出（这是链路的错，不是这一镜的错，不许静默成未判）。
    """
    plan = _region_plan(shot, frames, fps)
    if not plan:
        return [], []                                   # ① 没申报：㉗ 不适用
    head = f"闸㉗ 揭示时序：{sid}："
    got = gate.get("reveal_used")
    snaps = {int(k): v for k, v in got.items()} if isinstance(got, Mapping) else {}
    if not snaps:
        return [], [f"{head}母版没回读分区揭示行（`DG_REVEAL:` 一条都没有）——"
                    f"这一镜的揭示时序**没判过**（未判不是通过）"]

    paper, why = await _paper_of(shot, storage=storage, work=work)
    if paper is None:
        return [], [f"{head}{why}"]
    if band is None:
        return [], [f"{head}画面带原点没读到（token 里缺 gutter/topSafe）——"
                    f"母版报的是带内坐标，没有原点就挪不到帧坐标系上，这一镜**没判过**"]
    bx, by, _bw, _bh = band

    errors: list[str] = []
    undetermined: list[str] = []
    for f, rows in sorted(plan.items()):
        snap = snaps.get(f)
        if not isinstance(snap, Mapping):
            undetermined.append(
                f"{head}第 {f} 帧母版没回读（plan 里有、账上没有）——这一帧**没判过**")
            continue
        masks = {str(m.get("id")): m for m in (snap.get("regions") or [])
                 if isinstance(m, Mapping)}
        data = await asyncio.to_thread(frame_rgb, clip, at_sec=f / fps, size=size)
        for row in rows:
            rid, want = str(row["id"]), row["want"]
            m = masks.get(rid)
            if m is None:
                undetermined.append(
                    f"{head}第 {f} 帧：母版回读里没有 {rid} 这一块——这一块**没判过**")
                continue
            # 母版报的是**带内**坐标（它自己那一层的原点在带的左上角），
            # 而抽出来的帧是**整幅画面**：这里把带原点加回去，是这条判据里
            # 唯一的坐标换算，且只做一次（母版那边不再有人做第二次）。
            rects = [[bx + float(r[0]), by + float(r[1]), float(r[2]), float(r[3])]
                     for r in (m.get("rects") or []) if len(r) >= 4]
            pts = sample_points(data, size, rects)
            why = judge(want, pts, paper)
            if why:
                errors.append(f"{head}第 {f} 帧（t={f / fps * 1000:.0f} ms）：{rid} {why}")
    return errors, undetermined


def judge(want: str, pts: Sequence[Sequence[float]],
          paper: Sequence[float]) -> str:
    """**一句话判词：空串 = 过。** 这条判据的全部内容都在这个函数里。

    单独拆出来（而不是留在 `check_reveal` 的循环里）有两个理由：一是抽帧、读母版回读、
    取纸底那三件事都需要真机，而**判据本身不需要**——拆出来之后离线夹具能对着同一份
    实现跑（`scripts/reveal_gate.py` 的两支反例就是调它，而不是另写一份对着干的逻辑）；
    二是「什么算过」这件事在这份代码里只有一个出处。

    两句话，两个方向（`want` 由 `_region_plan` 按申报算出来）：

      * `want="paper"`（这一块**还没开始**）：掩码里的采样点必须**全是纸底**，
        有一个不是 → 提前露出来了；
      * `want="drawn"`（这一块**应该已画完**）：掩码里必须**不全是纸底**，
        全是纸底 → 到点没显示。

    **空掩码**是第三种情形，两个方向不对称：`want="paper"` 时它天然成立（没有像素是
    这一块的，谈不上提前露出）；`want="drawn"` 时它是硬矛盾——到了点却整块被
    「后续块 + protected」扣空，画面上没有它的任何像素。
    """
    if not pts:
        return ("" if want == "paper" else
                "这一块应当已画完，而它的允许掩码是空的（被后续块或 protected 全扣掉了）"
                "——画面上没有它的任何像素")
    des = [delta_e(p, paper) for p in pts]
    far = [d for d in des if d > PAPER_DELTA_E_MAX]
    if want == "paper" and far:
        return (f"这一块**还没开始**，而它的区域里已有 {len(far)}/{len(des)} 个采样点不是纸底"
                f"（最大 ΔE {max(des):.1f} > {PAPER_DELTA_E_MAX:g}）——提前露出来了："
                "检察这一块的 start_ms 与它前面那些块的允许掩码")
    if want == "drawn" and not far:
        return (f"这一块应当已经画完，而它区域里 **{len(des)} 个采样点全是纸底**"
                f"（最大 ΔE {max(des):.1f} ≤ {PAPER_DELTA_E_MAX:g}）——到点没显示："
                "检察这一块的 duration_ms 或它是否被后续块扣空了")
    return ""


async def _paper_of(shot: Mapping[str, Any], *, storage: Any, work: Path
                    ) -> tuple[tuple[float, float, float] | None, str]:
    """这一镜的纸底色：取**它声明的那张插画原图**的四角内缩取样。

    取哪一张：`assets[0]`。为什么是第一个而不是「找一张是插画的」：这一链没有
    「哪一件是画面主体」这一栏（`asset_role` 说的是素材在片子里的角色，不是这一镜
    的主图），凭空挑一张就是替模型做决定；而**声明顺序**是模型自己写的、
    回执里看得见的一个决定。多件素材的镜，㉗ 用第一件的底色当基准，并在理由里写清。
    """
    assets = [a for a in (shot.get("assets") or []) if isinstance(a, Mapping)]
    if not assets:
        return None, "这一镜申报了分区揭示却没有声明任何素材——没有原图就取不出纸底基准"
    first = assets[0]
    sha = str(first.get("sha256") or "").strip().lower()
    if not sha or storage is None:
        return None, (f"这一镜的第一件素材没有可用的 sha256"
                      f"（{sha[:16]!r}），纸底基准取不到——这一镜的 ㉗ **没判过**")
    try:
        row = await storage.assets.get_by_sha(sha)
    except Exception as e:      # noqa: BLE001
        return None, (f"素材行读不到（{type(e).__name__}: {str(e)[:60]}），"
                      f"纸底基准取不到——这一镜的 ㉗ **没判过**（未判不是通过）")
    if not row:
        return None, (f"素材 sha256={sha[:16]}… 在 asset_library 里查不到，"
                      f"纸底基准取不到——这一镜的 ㉗ **没判过**（未判不是通过）")
    key = str(row.get("object_key") or "")
    if not key:
        return None, (f"素材 sha256={sha[:16]}… 的行没有 object_key，字节取不到——"
                      f"这一镜的 ㉗ **没判过**（未判不是通过）")
    # 落盘要带扩展名：ffmpeg 是按名字认容器的，`.tmp` 会让它认不出 PNG。
    # 优先用**这一镜自己的申报**（模型写的 ext，闸⑬ 那一路也在读同一个字段），
    # 退回 object_key 的后缀，再退回 PNG——三档都是"说得出出处"的，不猜。
    ext = str(first.get("ext") or "").lstrip(".").lower()
    if not ext:
        ext = (key.rsplit(".", 1)[-1] if "." in key else "png").lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    dst = work / f".paper_{sha[:16]}.{ext}"
    try:
        buf = bytearray()
        async for chunk in storage.objects.get_stream(key):
            buf.extend(chunk)
        await asyncio.to_thread(dst.write_bytes, bytes(buf))
    except Exception as e:      # noqa: BLE001
        return None, (f"素材字节读不到（{type(e).__name__}: {str(e)[:60]}），"
                      f"纸底基准取不到——这一镜的 ㉗ **没判过**（未判不是通过）")
    try:
        return await asyncio.to_thread(paper_from_image, dst)
    finally:
        try:
            dst.unlink()
        except OSError:
            pass
