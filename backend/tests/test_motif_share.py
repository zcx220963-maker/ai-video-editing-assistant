# -*- coding: utf-8 -*-
"""T18b：母题占比回归——「横屏正文带那档，母题只占 9%」不许再静默过关。

**这一条治的病**（合同 §0.5-P11、案例在 §8.3）：2026-10-11 那批横屏镜里，母题被挤在
画面中间一小块，`fill_span` 实测落在 **≈9% ~ 36%**；当年那条链只判「有没有画东西」，
于是十二镜全绿出片，人眼一看就是「小气」。根因不在画法，在**镜头代码根本不知道带子
多大**——它自己猜 `viewBox="0 0 1080 620"` 并十二镜照抄，而横屏那条带子是 1800×732
（`remotion/src/FilmLayout.tsx` 的 `useBody()` 就是为这件事开的那个口子）。

**这一条钉什么**：把记录在案的那条分布**当锚定负样本**，逐点喂给闸④ 的第二半，证明
它在**横屏**带子上判得动；并顺手钉住「分母只能是带子、不能是整帧」在横屏这一侧的具体
后果（整帧会把铺满读成 68%）。**不新造任何阈值**——用的就是 `code_render` 现值
（`SPAN_MIN` / `TIGHT_MIN` / `FILL_TARGET`），合同 T18b 的绿那一栏写的就是这句。

**为什么单独一份文件而不是塞进 `test_code_gates.py`**：那份文件里闸④ 的两半已经判过了，
但**只在竖屏 1080×1920 上判**（见它 :450 那句「横屏同理」——那句话到今天为止只是一个
断言，没有一支夹具）。横屏是**最容易漏的那一档**（带子矮到 732，分母换一下读数就翻倍），
所以它值得自己的一节、自己的锚定样本。

**已知边界（如实记账，见 §9-Q88）**：记录在案的那批上限 36% 落在 `TIGHT_MIN=0.30`
**以上**、`SPAN_MIN=0.45` 以下——所以「一大块实心块占带短边 36%」这种构图今天**照样过闸**
（占比够、小气闸够不着）；只有它是**细线条**（占比低于 2%）时才被空壳闸那半拦住。
本文件把这两种情形各自的读数都打出来钉住，**不替它补一条新线**：补线是 T32 的 ⑳(b)
（拿锚定几何整段判）该干的事，T18b 的职责是「不复现 + 边界写在明面上」。

判据只用 `_frame_diff_stats` 与 `_empty_check` 两个**已有**入口读真实字节，不另起一套
像素算术——另起一套就会出现「用例绿、闸红」这种最坏的错配。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server import mediaops                            # noqa: E402
from storyline_server.mediaops import MediaError                 # noqa: E402
from storyline_server.motion import code_render as cr            # noqa: E402
from storyline_server.motion import code_spec as cs              # noqa: E402

_fails = 0
_checks = 0
_unverified: list[str] = []

BG = "#f6f1e4"                       # 与母版 tokens.bg 同一个值（同 test_code_gates）
BG0X = "0x" + BG[1:]                 # ffmpeg 的 color 源只认 0x 写法
FPS = 10
WORK = Path("/tmp") / "motif_share_work"

#: 横屏真尺寸与真带子。1920×1080 → 带子 `1920-2×60` × `1080-48-300` = **1800×732**。
#: 分母就是它——`code_render.py:249` 那句「横屏的带子只有 732 高，照整帧算会把『铺满
#: 带子』读成 68%」里的两个数都从这组常量来，本文件把它逐字量出来（§0 / §2）。
SIZE = cs.ASPECTS["landscape"]
BAND = (60, 48, 1800, 732)
#: 母版回读的那份 token（`_band_of` 只认这四个键，Python 侧不另算几何）。
LAND_TOKENS = {"bg": BG, "fontSize": 78, "bodyWidth": 1800, "bodyHeight": 732,
               "gutter": 60, "topSafe": 48}

#: 记录在案的那条分布：2026-10-11 判漏的那批横屏镜 `fill_span` 的实测区间。
#: 用例不假装复现到小数点后——它钉的是「这一整段今天各自被哪条规则接住」。
RECORDED_LOW, RECORDED_HIGH = 0.09, 0.36


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _even(v: float) -> int:
    """yuv420p 的 drawbox 在奇数边长上会取整到邻格，先把边长自己取成偶数——
    否则「我要 k=0.09」实际画出来是另一个数，而用例仍然报它想要的数。"""
    n = int(round(v))
    return n - (n % 2)


def _synth(dest: Path, *, size: tuple[int, int], box: tuple[int, int, int, int],
           seconds: float = 3.0) -> None:
    """造一支「底色 + 一块红」的片子。box 是绝对像素坐标（与 `crop` 同一套）。"""
    w, h = size
    x, y, bw, bh = box
    mediaops.ffmpeg("-v", "error", "-f", "lavfi", "-i",
                    f"color=c={BG0X}:s={w}x{h}:r={FPS}",
                    "-vf", f"drawbox=x={x}:y={y}:w={bw}:h={bh}:c=0xd62828:t=fill",
                    "-frames:v", str(int(seconds * FPS)), "-pix_fmt", "yuv420p",
                    str(dest), timeout=120)


def _cluster(k: float, *, band: tuple[int, int, int, int] = BAND) -> tuple[int, int, int, int]:
    """「母题被挤」的几何：一块**居中**的小簇，其外接盒的宽与高各是带子的 k 倍。

    取 `k` 而不是取绝对像素，是因为被记录的那条分布就是按比值说的（「只占 9%」）。
    宽高同比 → `span = min(k·bw/bw, k·bh/bh) = k`，于是「我喂进去的 k」与「闸读出来的
    span」是同一个数——这一步故意留着可比，好让读数对不上时当场看得出来。
    """
    x, y, bw, bh = band
    cw, ch = _even(k * bw), _even(k * bh)
    return (x + (bw - cw) // 2, y + (bh - ch) // 2, cw, ch)


def _read(clip: Path, tag: str, *, band=BAND, size=SIZE) -> tuple[float, float]:
    return cr._frame_diff_stats(clip, 0.0, bg_hex=BG, size=size,
                                out=WORK / f"{tag}.raw", band=band)


def _land_spec() -> dict:
    """一支**横屏**单镜片，过真契约（闸的读数是 spec 里的 width/height 算出来的）。"""
    raw = {"title": "横屏母题占比夹具", "aspect": "landscape", "fps": FPS, "narration": False,
           "caption_style": "on_picture", "texture": "none",
           "shots": [{"id": "s01",
                      "code": "export const Shot = () => (<FilmLayout>x</FilmLayout>);",
                      "text": "母题铺满带子", "min_duration_sec": 3.0,
                      "card": "typography/paper-title-card", "subject": "斜纹圆",
                      "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}]}
    spec, errors = cs.validate_code_spec(cs.overlay_knobs(dict(raw), {}))
    assert errors == [], f"夹具本身没过契约：{errors}"
    return spec


async def _gate(clip: Path, tag: str, *, band=BAND) -> tuple[dict | None, str]:
    """把一支片子过一遍**真的** `_empty_check`。→ (读数, 退回原因)。

    不自己复写「span < TIGHT_MIN 就红」这句判断——那样这份用例判的是它自己写的规则，
    而闸改了口径它也照样绿。
    """
    (WORK / tag).mkdir(parents=True, exist_ok=True)
    info = mediaops.probe(clip)
    try:
        out = await cr._empty_check("s01", clip, bg_hex=BG, size=SIZE,
                                    duration=float(info["duration"]),
                                    shot_work=WORK / tag, band=band)
        return out, ""
    except MediaError as exc:
        return None, str(exc)


def section_geometry() -> None:
    print("\n§0 几何与常量先对齐（后面每个数都从这组来，对不上就不必往下判）")
    check(SIZE == (1920, 1080), f"横屏尺寸取自契约表本身：{SIZE}")
    check(cr._band_of(LAND_TOKENS, SIZE) == BAND,
          f"带子 = 母版回读的那四个数：{cr._band_of(LAND_TOKENS, SIZE)}")
    check(cr.TIGHT_MIN < cr.SPAN_MIN < cr.FILL_TARGET,
          f"三条线的次序：TIGHT_MIN={cr.TIGHT_MIN} < SPAN_MIN={cr.SPAN_MIN} "
          f"< FILL_TARGET={cr.FILL_TARGET}（空壳的豁免线比小气线宽，目标最高）")
    check(RECORDED_LOW < cr.TIGHT_MIN,
          f"记录在案的下限 {RECORDED_LOW:.0%} 落在小气线 {cr.TIGHT_MIN:.0%} 以下——"
          "「母题只占 9%」那档今天有硬闸接住")
    check(RECORDED_HIGH < cr.SPAN_MIN,
          f"记录在案的上限 {RECORDED_HIGH:.0%} 也落在空壳豁免线 {cr.SPAN_MIN:.0%} 以下——"
          "这一整段都算「挤成一团」；但注意它**高于**小气线（已知边界，见 §3 与 §9-Q88）")
    check(cr._band_of({"bg": BG}, SIZE) is None,
          "母版没回读几何时退回整帧口径（判空照跑，不误杀）——横屏同理")


def section_crushed() -> None:
    print("\n§1 红：记录在案的那两档，真实字节喂进闸④ 必须被接住（红先）")
    work = WORK / "crushed"

    # ① 细线条的 9%：占比低（< 2%）**且**铺开不够 → 空壳那一半接住。
    k = RECORDED_LOW
    thin = work / "thin09.mp4"
    thin.parent.mkdir(parents=True, exist_ok=True)
    # 细线条画法：一簇同心方框线（像素少、外接盒就是那一簇），不是实心块。
    x, y, cw, ch = _cluster(k)
    mediaops.ffmpeg("-v", "error", "-f", "lavfi", "-i",
                    f"color=c={BG0X}:s={SIZE[0]}x{SIZE[1]}:r={FPS}",
                    "-vf", f"drawbox=x={x}:y={y}:w={cw}:h={ch}:c=0xd62828:t=2",
                    "-frames:v", str(3 * FPS), "-pix_fmt", "yuv420p", str(thin),
                    timeout=120)
    ratio, span = _read(thin, "thin09")
    check(abs(span - k) < 0.01, f"喂 k={k:.0%} → 闸读出的填充 {span:.1%}（同一几何）")
    check(ratio < cr.EMPTY_MIN_RATIO,
          f"细线条那簇的像素占比 {ratio:.2%} < {cr.EMPTY_MIN_RATIO:.0%}——"
          "单看占比它就该被打回（旧口径正是这么判死细线条的）")
    out, why = asyncio.run(_gate(thin, "thin09"))
    check(out is None and "闸④" in why,
          f"整支过 `_empty_check` → 被闸④ 接住：{why[:120]}")

    # ② 实心块的 25%：占比够（远高于 2% 线）**但**铺不开 → 小气那一半接住。
    #    这一支才是「母题被挤」的原话：画了一块，可那一小块不是画面，是贴纸。
    k = 0.25
    blob = work / "blob25.mp4"
    _synth(blob, size=SIZE, box=_cluster(k))
    ratio, span = _read(blob, "blob25")
    check(abs(span - k) < 0.01, f"喂 k={k:.0%} → 填充 {span:.1%}")
    check(ratio > cr.EMPTY_MIN_RATIO,
          f"这一支的占比 {ratio:.1%} 远高于 {cr.EMPTY_MIN_RATIO:.0%} 线——"
          "空壳那半够不着它，必须是**小气**那半出手")
    out, why = asyncio.run(_gate(blob, "blob25"))
    check(out is None and "挤成一团" in why,
          f"占比够也拦得住，退回原因点名「挤成一团」：{why[:150]}")
    check(f"{cr.TIGHT_MIN * 100:.0f}%" in why and f"{span * 100:.0f}%" in why,
          "退回原因带着**闸值**与**实测值**两个数，不是只说「小」")


def section_band_denominator() -> None:
    print("\n§2 绿：铺满带子的那支过闸；而同一份字节按整帧量会读成 68%（分母是带子这事有代价）")
    work = WORK / "full"
    work.mkdir(parents=True, exist_ok=True)
    full = work / "bandfill.mp4"
    _synth(full, size=SIZE, box=BAND)              # 正好填满带子
    _r_band, span_band = _read(full, "bandfill")
    _r_full, span_full = _read(full, "bandfill_full", band=None)
    check(span_band > 0.99,
          f"按**带子**量：铺满 {span_band:.0%}（这是镜头代码用 useBody() 排出来的样子）")
    check(abs(span_full - 732 / 1080) < 0.01,
          f"按**整帧**量：同一份字节只有 {span_full:.0%}——"
          "`code_render` 注释里那个「横屏会读成 68%」的数，在这里被逐字量出来")
    check(span_band - span_full > 0.30,
          f"两个分母差 {span_band - span_full:.0%}：横屏的带子矮（732/1080），"
          "分母选错的后果比竖屏大得多——这就是「横屏同理」那句话该有夹具的原因")
    out, why = asyncio.run(_gate(full, "bandfill"))
    check(out is not None and not why,
          f"整支过 `_empty_check` → 不抛（读数 {out}）")
    check(out and out["span"] > 0.99,
          "返回值里的 span 是同一趟算的，不是另算一遍")

    # 反向的一支：把**同一份字节**裁到那块红自己的盒子上，读数必须变成铺满——
    # 证的是「读数跟着分母走」，而不是「这个夹具碰巧过线」。
    _r_own, span_own = _read(full, "bandfill_own", band=BAND)
    check(span_own > 0.99, f"同一份字节裁到它自己那条带子上：{span_own:.0%}")


def section_threshold_bands() -> None:
    print("\n§3 三条线的分档：过闸 / 只提醒 / 已知边界（记录在案的 30%~36% 这一段）")
    work = WORK / "bands"
    work.mkdir(parents=True, exist_ok=True)

    # (a) 60%：达到 FILL_TARGET，过闸且不该进「铺得不够开」的提醒。
    k = cr.FILL_TARGET
    a = work / "k60.mp4"
    _synth(a, size=SIZE, box=_cluster(k))
    _r, span = _read(a, "k60")
    check(span >= cr.FILL_TARGET, f"k={k:.0%} → {span:.0%} ≥ FILL_TARGET，过闸不打回")
    out, why = asyncio.run(_gate(a, "k60"))
    check(out is not None and not why, f"`_empty_check` 不抛（读数 {out}）")

    # (b) 45%：**低于 FILL_TARGET 但高于 TIGHT_MIN** —— 这正是「只提醒不打回」那一档。
    #     把「留白多的版式」判死是这条链没有权利做的事（见 `_empty_check` 的 docstring）。
    k = 0.50
    b = work / "k50.mp4"
    _synth(b, size=SIZE, box=_cluster(k))
    _r, span = _read(b, "k50")
    check(cr.TIGHT_MIN < span < cr.FILL_TARGET,
          f"k={k:.0%} → {span:.0%} 落在 {cr.TIGHT_MIN:.0%}~{cr.FILL_TARGET:.0%} 之间")
    out, why = asyncio.run(_gate(b, "k50"))
    check(out is not None and not why,
          "这一档**不打回**：FILL_TARGET 是技能正文给的目标值，做成硬闸等于把"
          "「留白多」这一种风格判死——提醒由报告层按同一根线现算")

    # (c) 已知边界：记录在案的上限 36%，实心块今天照样过闸。这一条**钉的是现状**，
    #     不是认可——所以标签里必须写明它是边界，且 §9-Q88 记着为什么不在 T18b 补线。
    k = RECORDED_HIGH
    c = work / "k36.mp4"
    _synth(c, size=SIZE, box=_cluster(k))
    ratio, span = _read(c, "k36")
    check(abs(span - k) < 0.01, f"复现记录在案的上限：k={k:.0%} → 填充 {span:.1%}")
    check(span > cr.TIGHT_MIN and ratio > cr.EMPTY_MIN_RATIO,
          f"它的占比 {ratio:.1%} 过线、铺开 {span:.0%} 也过小气线——两条硬闸都够不着它")
    out, why = asyncio.run(_gate(c, "k36"))
    check(out is not None and not why,
          f"**已知边界**（§9-Q88）：实心块占带短边 {span:.0%} 今天过闸。"
          "本文件不替它补新线——补线是 T32 的 ⑳(b) 拿锚定几何整段判的活")
    check(RECORDED_HIGH >= cr.TIGHT_MIN,
          f"边界来自两个现值的相对位置（{RECORDED_HIGH:.0%} ≥ TIGHT_MIN "
          f"{cr.TIGHT_MIN:.0%}），不是谁调出来的")


def section_single_source() -> None:
    print("\n§4 单源钉：T18b 没有新造任何阈值，也没另起一套像素算术")
    import ast
    import inspect
    src_cr = inspect.getsource(cr)
    check("SPAN_MIN = 0.45" in src_cr and "TIGHT_MIN = 0.30" in src_cr
          and "FILL_TARGET = 0.60" in src_cr,
          "三个阈值只在 `code_render.py` 定义这一次（用例引用的就是这三个常量）")

    # 「本文件没有重定义某某」这类自检有个自指陷阱：把名字写成字符串字面量去找，
    # 那句话本身就在被找的源码里，于是**永远**找得到、**永远**判红。用 AST 看
    # **真的赋值语句**，字面量就绕不进来（真有人写了 `SPAN_MIN = 0.9`，这里会红）。
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    assigned = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    for name in ("SPAN_MIN", "TIGHT_MIN", "FILL_TARGET"):
        check(name not in assigned,
              f"本文件没有自己赋值 `{name}`——阈值只有 `code_render.py` 那一份，"
              "「沿用现值」不是指抄一份过来")

    # 同理：针**拼出来**而不是写字面量，否则针自己在源码里，这条自检恒假。
    needles = ["read_" + "bytes", "min(b" + "w", "any(" + "data"]
    mine = inspect.getsource(sys.modules[__name__])
    hit = [n for n in needles if n in mine]
    check(not hit,
          f"本文件里找不到外接盒/掩码算术的写法 {needles}（找到的是 {hit}）——"
          "读数只走 `_frame_diff_stats` / `_empty_check` 两个已有入口；"
          "在用例里复写一套就会出现「用例绿、闸红」这种最坏的错配")
    check("_frame_diff_stats" in mine and "_empty_check" in mine,
          "两个入口确实被用到了（上一条不是「压根没读」的绿）")

    skill = (Path(__file__).resolve().parents[1] / "examples" / "retired"
             / "motion_explainer" / "SKILL.md")
    if skill.is_file():
        body = skill.read_text(encoding="utf-8")
        check("FILL_TARGET=0.60" in body and "TIGHT_MIN=0.30" in body,
              "技能正文那张闸表里印的是同一批数（模型看得到的与代码用的是同一组）")
    else:
        _unverified.append(f"§4 技能正文不可达（{skill}）——「正文与代码同源」未验证")
        print(f"  ⚠ 未判：{skill} 不在")


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"T18b 母题占比回归：横屏 {SIZE[0]}×{SIZE[1]}，带子 {BAND[2]}×{BAND[3]}，fps {FPS}")
    section_geometry()
    section_crushed()
    section_band_denominator()
    section_threshold_bands()
    section_single_source()
    shutil.rmtree(WORK, ignore_errors=True)

    print("\n未验证清单（跳过 ≠ 通过）：")
    if not _unverified:
        print("  · （无）")
    for item in _unverified:
        print(f"  · {item}")
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
