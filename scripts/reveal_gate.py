"""R12-T61 巡检：闸㉗ 的比色算术、两支反例、以及「假绿夹为什么没有入口」。

**这条闸为什么必须存在。** ㉗ 是这族风格里唯一**只吃像素、不吃申报**的判据，而它自己
最容易被写成一把假的尺：把纸底色写成一个常量、或者把「过」定义成一个恒真的条件。
一旦那样，回执上会写着「揭示时序已核」，画面上却是「一张静图配字幕」从头到尾——
这条链上没有任何别的闸会响（满幅插画下 ⑲-a 的主体占比天然 ≈1、㉕ 的徽章四条也不触发）。
所以这份巡检判的是**判据本身**，四件事：

① **算术**：ΔE 的三个量纲锚点（同色 0 / 黑-白 ≈100 / 纸-白 与 纸-墨 分居阈值两侧），
   以及 `sample_points` 的两条规矩（越界点丢掉、矩形里按网格取点）；
② **基准的出处**：纸底色只能从**原图四角内缩**量出来——中心画着东西也不许影响它；
   四角彼此不一致时返回 `None`（未判），不是硬编一个值继续往下算；
③ **两支反例**：提前露出、到点没显示，都必须**打回**；正例必须过；
④ **假绿夹**：基准一旦**不是真正的纸底**（纯白也好、内容色也好），「该显示的块里
   必须不全是纸底」这一半就**恒真**——纸上什么都没画也过。所以这条判据守的是基准的
   **出处**（只能从原图四角量出来），不是基准的值；第 ⑤ 组用源码文本证「没有第二个
   入口」。这也是 `rgb_to_lab` 那三个量纲锚点要印出来的理由：换阈值、换风格时，
   得一眼看得出「噪声在哪儿、内容在哪儿」。

**不连库、不烧渲染**：合成图由本地 ffmpeg 现画，帧字节由纯 Python 拼出，
判据走的是生产代码里**同一个** `reveal_gate.judge`（没有第二份对着干的逻辑）。

用法：
    python scripts/reveal_gate.py            # 逐条打 ok/XX
    python scripts/reveal_gate.py --json     # 末尾附机器可读的失败清单

退出码：0 全绿 / 1 有不合预期。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".smoke" / "reveal_gate"


def _add_paths() -> None:
    """把 `storyline_server` 放上 import 路径（宿主与容器两种布局）。"""
    for cand in (ROOT / "backend", Path("/app")):
        if (cand / "storyline_server").is_dir():
            sys.path.insert(0, str(cand))
            return


_add_paths()

from storyline_server import mediaops                                   # noqa: E402
from storyline_server.motion import reveal_gate as rg                   # noqa: E402

FAILURES: list[str] = []

#: 合成图用的两个色：与 `assets/*` 里那族风格的纸底同族（暖白纸）与深灰墨。
#: **只是夹具的用色**，与生产里那句「基准不许写死常量」不冲突——生产里没有任何一处
#: 写着这两个数（第 ④ 组会用源码文本把这件事也证一遍）。
PAPER_RGB = (245, 235, 215)
INK_RGB = (58, 58, 58)


def say(ok: bool, msg: str) -> None:
    if not ok:
        FAILURES.append(msg)
    print(f"{'  ok ' if ok else '  XX '} {msg}")


def _frame(size: tuple[int, int], *, blocks: list[tuple[int, int, int, int,
                                                       tuple[int, int, int]]]) -> bytes:
    """拼一帧 RGB24：先铺纸底，再按 `blocks` 画矩形（x, y, w, h, rgb）。"""
    w, h = size
    buf = bytearray(bytes(PAPER_RGB) * (w * h))
    for x, y, bw, bh, rgb in blocks:
        for yy in range(max(0, y), min(h, y + bh)):
            for xx in range(max(0, x), min(w, x + bw)):
                o = (yy * w + xx) * 3
                buf[o:o + 3] = bytes(rgb)
    return bytes(buf)


def _artwork(dst: Path, *, corner_rgbs: list[tuple[int, int, int]] | None) -> Path:
    """画一张 256×256 的合成插画：四角可选同色/四色，中心一块深灰。

    中心那块是**故意的**：它就在四角取样区之外，而基准若被内容带着走（例如改成整图
    平均），量出来的值会明显偏向灰——第 ② 组就靠这个差把「基准取错了地方」抓住。
    """
    w = 256
    chain = [f"color=c=0x{PAPER_RGB[0]:02X}{PAPER_RGB[1]:02X}{PAPER_RGB[2]:02X}:s={w}x{w}"]
    if corner_rgbs:
        # 四块 40×40 压在四角：`CORNER_INSET 0.06 ~ CORNER_PATCH 0.06` 在 256 上是
        # [15.4, 30.7] 这一小片，40 足够盖住它，也够小、不碰中心。
        spots = [(0, 0), (w - 40, 0), (0, w - 40), (w - 40, w - 40)]
        for (x, y), rgb in zip(spots, corner_rgbs):
            chain.append(f"drawbox=x={x}:y={y}:w=40:h=40:"
                         f"color=0x{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}:t=fill")
    chain.append(f"drawbox=x=96:y=96:w=64:h=64:"
                 f"color=0x{INK_RGB[0]:02X}{INK_RGB[1]:02X}{INK_RGB[2]:02X}:t=fill")
    mediaops.ffmpeg("-f", "lavfi", "-i", chain[0], "-vf", ",".join(chain[1:]),
                    "-frames:v", "1", "-y", str(dst), timeout=120)
    return dst


def _paper_like(rgb: tuple[float, float, float]) -> bool:
    return all(abs(float(a) - float(b)) <= 6.0 for a, b in zip(rgb, PAPER_RGB))


# ---------------------------------------------------------------------------
# ① 算术
# ---------------------------------------------------------------------------

def case_arithmetic() -> None:
    d_bb = rg.delta_e((0, 0, 0), (255, 255, 255))
    say(95 <= d_bb <= 105, f"ΔE(黑,白)={d_bb:.1f}，落在 CIE76 的满量程附近（L 从 0 到 100）")
    say(rg.delta_e((120, 130, 140), (120, 130, 140)) < 1e-9, "同一个颜色的 ΔE 是 0（判据的零点是对的）")

    d_paper_white = rg.delta_e(PAPER_RGB, (255, 255, 255))
    d_paper_ink = rg.delta_e(PAPER_RGB, INK_RGB)
    # 两侧锚点：**下界是压缩噪声、上界是内容**。纸底与纯白差 12.8 > 阈值——这一条当初
    # 被我写反过（以为「纸-白」在阈值内），夹具第一次跑就把它抓了出来。它的意义是：
    # 一个「不是真纸底」的基准（例如写死的纯白）不会让判据更宽松地放行，而是把**所有**
    # 纸底都读成内容——错得响亮，见第 ④ 组。
    say(d_paper_ink > 2 * rg.PAPER_DELTA_E_MAX,
        f"纸底与**墨色**差 ΔE {d_paper_ink:.1f} > 2×阈值（余量够大：阈值不踩在内容上）")
    say(d_paper_white > rg.PAPER_DELTA_E_MAX,
        f"纸底与**纯白**差 ΔE {d_paper_white:.1f} > 阈值 {rg.PAPER_DELTA_E_MAX:g}"
        "——所以「写死一个纯白基准」不是宽松，是把纸底判成内容（第 ④ 组）")
    say(rg.PAPER_DELTA_E_MAX >= 4,
        f"阈值 {rg.PAPER_DELTA_E_MAX:g} ≥ 4：平坦区被 H.264 抖出的 ΔE 1~3（⑭/㉑ 抽帧"
        "那几趟的经验值）必须落在阈值**之内**，否则每一镜都会因为压缩噪声被判红")

    size = (64, 64)
    ink_block = (32, 32, 16, 16, INK_RGB)
    data = _frame(size, blocks=[ink_block])
    over_ink = rg.sample_points(data, size, [[32.0, 32.0, 16.0, 16.0]])
    say(len(over_ink) == rg.GRID_N * rg.GRID_N,
        f"矩形里按 {rg.GRID_N}×{rg.GRID_N} 取点（取到 {len(over_ink)} 个）")
    say(all(rg.delta_e(p, INK_RGB) < 3 for p in over_ink),
        "落在墨块上的那 9 个点读到的都是墨色（网格取点在矩形内、没跑出去）")
    on_paper = rg.sample_points(data, size, [[0.0, 0.0, 16.0, 16.0]])
    say(all(rg.delta_e(p, PAPER_RGB) < 3 for p in on_paper),
        "落在纸底的矩形读到的都是纸色")
    outside = rg.sample_points(data, size, [[60.0, 60.0, 16.0, 16.0]])
    say(outside == [],
        "**越界的矩形一个点都不采**（不夹到画面边上——夹边会把画面外的申报读成"
        "边缘的颜色，而边缘常是底/描边，等于判据自己把自己判绿）")


# ---------------------------------------------------------------------------
# ② 基准的出处
# ---------------------------------------------------------------------------

def case_paper_baseline() -> None:
    same = _artwork(WORK / "art_same_corners.png", corner_rgbs=None)
    paper, why = rg.paper_from_image(same)
    say(paper is not None and _paper_like(paper),
        f"四角同色的原图 → 量出纸底 {tuple(round(v) for v in paper) if paper else None}"
        f"（{why or 'ok'}）")
    if paper is not None:
        d_ink = rg.delta_e(paper, INK_RGB)
        say(d_ink > 2 * rg.PAPER_DELTA_E_MAX,
            f"同一张图的中心画着一大块墨，而量出来的基准与墨差 ΔE {d_ink:.1f}——"
            "**基准没把内容算进去**（四角内缩取样的意义就在这里）")
        d_mid = abs(sum(paper) / 3 - sum(PAPER_RGB) / 3)
        say(d_mid < 6, f"量出来的基准与合成时的纸色平均差 {d_mid:.1f}（H.264/PNG 都不该偏）")

    diff = _artwork(WORK / "art_diff_corners.png",
                    corner_rgbs=[(255, 255, 255), (200, 30, 30), (30, 160, 60), (40, 60, 200)])
    paper2, why2 = rg.paper_from_image(diff)
    say(paper2 is None and "不一致" in why2,
        f"四角画着四种颜色的原图 → **退回不判**（{why2[:60]}）——"
        "一份自己就不一致的基准拿去比色，会得到一条「每镜都红」或「每镜都绿」的判据")

    missing = WORK / "art_missing.png"
    paper3, why3 = rg.paper_from_image(missing)
    say(paper3 is None and bool(why3),
        "图读不到（不存在的路径）→ 退回不判，理由带回来（**未判不是通过**）")


# ---------------------------------------------------------------------------
# ③ 两支反例 + 正例
# ---------------------------------------------------------------------------

def case_counterexamples() -> None:
    size = (64, 64)
    data = _frame(size, blocks=[(32, 32, 16, 16, INK_RGB)])
    over_ink = rg.sample_points(data, size, [[32.0, 32.0, 16.0, 16.0]])
    on_paper = rg.sample_points(data, size, [[0.0, 0.0, 16.0, 16.0]])
    paper = (float(PAPER_RGB[0]), float(PAPER_RGB[1]), float(PAPER_RGB[2]))

    why1 = rg.judge("paper", over_ink, paper)
    say(bool(why1) and "还没开始" in why1,
        f"反例一（**提前露出**）：还没开始的块里采到了墨 → 打回（{why1[:48]}…）")
    why2 = rg.judge("drawn", on_paper, paper)
    say(bool(why2) and "到点没显示" in why2,
        f"反例二（**到点没显示**）：该画完的块里全是纸底 → 打回（{why2[:48]}…）")
    say(rg.judge("paper", on_paper, paper) == "" and rg.judge("drawn", over_ink, paper) == "",
        "正例两向都过：没开始的块全是纸底、该显示的块里真有内容")

    say(rg.judge("paper", [], paper) == "",
        "**空掩码**在「还没开始」那一向天然成立（没有像素是它的，谈不上提前露出）")
    why3 = rg.judge("drawn", [], paper)
    say(bool(why3) and "允许掩码是空的" in why3,
        "**空掩码**在「该画完」那一向是硬矛盾（到点却被后续块 + protected 全扣空）→ 打回")


# ---------------------------------------------------------------------------
# ④ 假绿夹
# ---------------------------------------------------------------------------

def case_fake_green() -> None:
    size = (64, 64)
    data = _frame(size, blocks=[])
    on_paper = rg.sample_points(data, size, [[0.0, 0.0, 16.0, 16.0]])

    white = (255.0, 255.0, 255.0)
    ink = (float(INK_RGB[0]), float(INK_RGB[1]), float(INK_RGB[2]))

    # A. 基准换成纯白：「该显示却全是纸底」这一半**恒真**（纸上什么都没有也过），
    #    而「还没开始」那一半反过来把所有纸底判成内容。**假绿藏在后半句里**：
    #    只要基准不是真正的纸底，`want="drawn"` 那一半就再也没有信息量。
    say(rg.judge("drawn", on_paper, white) == "",
        "假绿夹 A（基准换成**纯白**）：该显示的块里全是纸底，却判过——"
        "「该显示」那一半被架空（这正是假绿，而它只在基准不是真纸底时发生）")
    say(bool(rg.judge("paper", on_paper, white)),
        "同一个纯白基准下，「还没开始」那一半把**每一块纸底**都判成内容——"
        "同一个错基准的两个方向：一边恒真、一边恒假")

    # B. 基准换成内容色（这里用墨）：同样把「该显示」那一半架空（纸上没东西也过），
    #    并且把「还没开始」那一半判红。结论与 A 相同，只是更极端。
    say(rg.judge("drawn", on_paper, ink) == "",
        "假绿夹 B（基准换成**内容色**）：该显示的块里全是纸底，同样判过——"
        "架空与基准偏向哪边无关，只与「它是不是纸底」有关")
    why_ink = rg.judge("paper", on_paper, ink)
    say(bool(why_ink) and "还没开始" in why_ink,
        "同一个内容色基准下，「还没开始」那一半立刻判「提前露出」→ 假红（有人看得见）")

    # C. 所以这一组要盯住的不是基准的**值**，而是它的**出处**：
    #    生产代码里纸底色只能从 `paper_from_image` 量出来（下一组用源码文本证
    #    「没有第二个入口」）。这里只留一句量出来的边界，供日后重定阈值时对照。
    say(0 < rg.delta_e(PAPER_RGB, white) < rg.delta_e(PAPER_RGB, ink),
        f"边界记一笔：ΔE(纸,白)={rg.delta_e(PAPER_RGB, white):.1f} < "
        f"ΔE(纸,墨)={rg.delta_e(PAPER_RGB, ink):.1f}——离纸底越近的错基准，"
        "假绿那一半越隐蔽（「该显示」那一半仍然恒真，只是错得没那么响）")


def case_no_hardcoded_baseline() -> None:
    """基准不许有第二个入口：源码文本层面的正向证据。"""
    src = (ROOT / "backend/storyline_server/motion/reveal_gate.py").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    trips = re.findall(r"\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)", body)
    say(not trips,
        f"模块里**没有任何三元组颜色字面量**（扫到 {trips or '无'}）——"
        "纸底色不可能是写死的常量")
    say(body.count("PAPER_DELTA_E_MAX =") == 1 and body.count("CORNER_AGREE_DELTA_E_MAX =") == 1,
        "两个 ΔE 阈值各只有**一处**定义（判据与巡检不会各拿一个数）")
    say("_avg(corners)" in body and "def paper_from_image" in body,
        "纸底色在代码里只有一条来路：`paper_from_image` 的四角取样 → `_avg(corners)`")
    say("paper is None" in body and "undetermined.append" in body,
        "取不到基准那一档走的是 `None` → 未判分支（**未判不是通过**）")


# ---------------------------------------------------------------------------
# ⑤ 三个出口（真调一次生产实现）
# ---------------------------------------------------------------------------

def case_three_outlets() -> None:
    """`check_reveal` 的三支前置都在触到渲染/存储**之前**返回，所以这里能真调它。"""
    quiet = {"id": "s01", "code": ""}
    errs, undet = asyncio.run(rg.check_reveal(
        "s01", quiet, gate={}, band=None, clip=WORK / "none.mp4", size=(64, 64),
        fps=25, frames=50, storage=None, work=WORK))
    say(errs == [] and undet == [],
        "**没申报 `reveal`** → (打回=[], 未判=[])：这一档是「不适用」，连未判都算不上")

    shot = {"id": "s01", "reveal": {"canvas": {"w": 64, "h": 64},
                                    "scene_duration_ms": 2000,
                                    "regions": [{"id": "r1", "sequence": 1,
                                                 "region": {"x": 0, "y": 0, "w": 16, "h": 16},
                                                 "start_ms": 0, "duration_ms": 400}]},
            "assets": [{"sha256": "0" * 64, "name": "a.png"}]}
    errs2, undet2 = asyncio.run(rg.check_reveal(
        "s01", shot, gate={}, band=None, clip=WORK / "none.mp4", size=(64, 64),
        fps=25, frames=50, storage=None, work=WORK))
    say(errs2 == [] and len(undet2) == 1 and "没回读" in undet2[0],
        f"申报了但**母版没回读** `DG_REVEAL:` → 未判一条、打回零条（{undet2[0][:40]}…）")

    errs3, undet3 = asyncio.run(rg.check_reveal(
        "s01", {**shot, "assets": []}, gate={}, band=None, clip=WORK / "none.mp4",
        size=(64, 64), fps=25, frames=50, storage=None, work=WORK))
    say(errs3 == [] and len(undet3) == 1 and "没回读" in undet3[0],
        "顺序：母版回读那一关**排在取纸底之前**（一次回读都没有时，连素材字节都不去取）")

    say(len(undet2) == 1 and errs2 == [],
        "**未判 ≠ 通过**：这三支的 `errors` 都是空的，但 `undetermined` 里有话——"
        "回执层照实摊开（`motion_code_nodes` 的 `reveal_unjudged` 那栏）")


def main() -> int:
    # 判词里带「㉗」这类字符：Windows 宿主控制台默认 GBK，直接 print 会 UnicodeEncodeError
    # ——夹具自己炸掉会把「有一支反例没跑」伪装成「这一支跑过了」。容器里本来就是 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:       # noqa: BLE001 —— 老解释器没有 reconfigure 就算了
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="末尾附机器可读的失败清单")
    args = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)

    for fn in (case_arithmetic, case_paper_baseline, case_counterexamples,
               case_fake_green, case_no_hardcoded_baseline, case_three_outlets):
        print(f"\n== {fn.__name__} ==")
        try:
            fn()
        except Exception as e:      # noqa: BLE001 —— 夹具自己炸了也要算红，不许静默
            say(False, f"{fn.__name__} 抛了 {type(e).__name__}: {str(e)[:160]}")

    print()
    if FAILURES:
        print(f"XX 闸㉗ 巡检：{len(FAILURES)} 条不合预期")
        for f in FAILURES:
            print(f"   · {f}")
    else:
        print("ok 闸㉗ 巡检：全绿")
    if args.json:
        print(json.dumps({"ok": not FAILURES, "failures": FAILURES}, ensure_ascii=False))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
