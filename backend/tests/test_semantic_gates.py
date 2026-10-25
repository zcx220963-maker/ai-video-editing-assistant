# -*- coding: utf-8 -*-
"""R8 的三道语义闸：⑲-a 主体在场（T30）／⑳ 退化形态锚定（T32）／㉑ 动效锚定（T33）。

这三道是本文档**唯一一批「以前被写成永远没有机器判据」**的东西（§2.2 的 ⑲–㉑）。
它们的共同形状是：**把形容词换成一个能读的数**——
「画面有没有主体」→「主体件并集外接盒占画面带多少」；
「是不是那张丑卡」→「是不是已被人眼驳回过的三种形态之一」；
「动效有没有意义」→「每个事件锚在哪个字段上、触发帧在不在那个字段的首读窗口里」。

这一支只放**离线判得动**的那些格子（判据本体 + 出口三态 + 单源 + 结构闸）。
要真渲一镜才量得出来的那两栏（⑲-a 的两个占比、⑳(c) 的粗网格指纹与主色）在
`remotion/tests/layout-probe.mjs` 的真机夹具与 §8.1 的矩阵里；**没量到一律记未判**，
不在本文件里假装量到了。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs                 # noqa: E402
from storyline_server.motion import code_render as cr               # noqa: E402

_fails = 0
_checks = 0
_HERE = Path(__file__).resolve()


def _repo_root() -> Path:
    """这份用例读的几份**不在后端树里**的东西（`remotion/src/FilmLayout.tsx`、
    `remotion/src/lib/LayoutProbe.tsx` 与 `remotion/tests/layout-probe.mjs`）在两处布局下
    位置不同：镜像里 `remotion/` 与后端同根（`/app/remotion`），宿主机工作树上它与
    `backend/` 并列。**两种都能跑**是刻意的——版式差异不该把整份用例崩成
    FileNotFoundError（那会看起来像「断言失败」）。
    两处都没有时返回镜像那一版的口径，让真正的断言照常报红。
    """
    for cand in (_HERE.parents[1], _HERE.parents[2]):
        if (cand / "remotion" / "src" / "FilmLayout.tsx").is_file():
            return cand
    return _HERE.parents[1]


ROOT = _repo_root()


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _shot(i: int = 1, **over) -> dict:
    base = {"id": f"s{i:02d}", "sec": 2.0, "text": "一句话",
            "code": "export const Shot = () => (<FilmLayout>x</FilmLayout>);",
            "card": "typography/blur-slide", "subject": "一只杯子"}
    base.update(over)
    return base


def _scanned(**over) -> dict:
    """母版回读的一行（闸⑧ 的形状）。用例只改要判的那一格。"""
    row = {"status": "scanned",
           "scanned": {"texts": 3, "leaders": 0, "drawn": 5, "dropped": 0,
                       "subjects": 2, "subject_share": 0.42, "text_share": 0.20,
                       # ⑳(c) 的三栏（T32）：粗网格位掩码 + 主色前四。
                       "grid_text": 0b100000_000010_000000_000000_000000_000000,
                       "grid_drawn": 0b000000_111100_111100_111100_000000_000000,
                       "colors": ["#1b1f3a", "#f4c95d"]}}
    row["scanned"].update(over)
    return row


#: 两镜「画面上是同一张卡」的那组指纹——用例里反复用（同一张卡换个写法坐标会差几像素，
#: 所以判据用的是粗网格而不是精确坐标）。
_CARD = {"grid_text": 0b001100_001100_000000_000000_000000_000000,
         "grid_drawn": 0b000000_000000_111111_111111_000000_000000,
         "colors": ["#2b2b3c", "#e8e8f0"]}


def main() -> int:
    # =================================================================
    print("\n〔⑲-a 判据本体〕主体铺不开**且**文字占大片，才是「卡片+文字」")
    errs, _, _ = cs.check_subject_present([_shot()],
                                          scanned={"s01": _scanned(subject_share=0.42)})
    check(errs == [], f"主体占到 42% → 绿：{errs}")

    # 红样（P10 那支）：代码里画了东西（图元数 3），画面上却只是一张卡配一大片字。
    errs, warns, undet = cs.check_subject_present(
        [_shot()], scanned={"s01": _scanned(subject_share=0.08, text_share=0.72,
                                            subjects=3)})
    check(len(errs) == 1 and "卡片+文字" in errs[0],
          f"主体 8% + 文字 72% → 打回，且说得出「卡片+文字」这四个字：{errs[:1]}")
    check("8%" in errs[0] and "72%" in errs[0],
          "打回的话里两边的**读数**都在（模型能直接看出该改哪一边）")
    check(warns == [] and undet == [], "打回那一档不再往提醒/未判里各抄一份")

    # 单踩一条：只提醒不打回——满屏大字配小图标的封面镜、以及一行字的过渡镜
    # 各自只踩一条，联合判的意义就在这里。
    errs, warns, undet = cs.check_subject_present(
        [_shot()], scanned={"s01": _scanned(subject_share=0.05, text_share=0.30)})
    check(errs == [] and len(warns) == 1 and undet == [],
          f"主体小但文字也不多 → 只提醒（构图小气归闸④ 与正文目标值）：{warns[:1]}")
    errs, _, _ = cs.check_subject_present(
        [_shot()], scanned={"s01": _scanned(subject_share=0.30, text_share=0.80)})
    check(errs == [], "主体够大、字也多 → 绿（单条判会把这种镜误伤）")

    # 边界两侧：线是线，不许被浮点抹平。
    errs, _, _ = cs.check_subject_present([_shot()],
                                          scanned={"s01": _scanned(subject_share=0.18)})
    check(errs == [], "正好 0.18 → 放行")
    errs, _, _ = cs.check_subject_present(
        [_shot()], scanned={"s01": _scanned(subject_share=0.179, text_share=0.72)})
    check(len(errs) == 1, f"0.179 且文字主导 → 红（差一点也是差）：{errs[:1]}")
    errs, warns, _ = cs.check_subject_present(
        [_shot()], scanned={"s01": _scanned(subject_share=0.05, text_share=0.45)})
    check(errs == [] and len(warns) == 1,
          "文字正好 0.45 → 不算「主导」（判据是 > 0.45，不是 ≥）")

    # =================================================================
    print("\n〔⑲-a 空壳那一格〕代码写了、画面上没有——⑦⑯ 与 ⑲-b 之间的缝")
    errs, _, _ = cs.check_subject_present(
        [_shot(subject_channel="rough")],
        scanned={"s01": _scanned(subjects=0, subject_share=0.0, text_share=0.30)})
    check(len(errs) == 1 and "空壳" in errs[0],
          f"申报了 rough 而屏幕上图元数=0 → 打回「主体是空壳」：{errs[:1]}")
    errs, warns, _ = cs.check_subject_present(
        [_shot()],  # 纯文字镜：不申报通道，图元数 0 是**正常**的
        scanned={"s01": _scanned(subjects=0, subject_share=0.0, text_share=0.30)})
    check(errs == [] and len(warns) == 1,
          f"没申报通道的纯文字镜图元数=0 不打回（⑦ 给的那 1/3 额度）：{errs}")

    # =================================================================
    print("\n〔⑲-a 出口三态〕量不到 ≠ 量到了很小（D-未判不绿）")
    errs, warns, undet = cs.check_subject_present([_shot()], scanned=None)
    check(errs == [] and warns == [] and len(undet) == 1 and "未判" in undet[0],
          f"整片没有回读 → 落**未判**（既不红也不提醒）：{undet}")
    errs, warns, undet = cs.check_subject_present(
        [_shot()], scanned={"s01": {"status": "unscanned", "scanned": None}})
    check(errs == [] and len(undet) == 1, "这一镜没扫成 → 未判（「没扫」不读成「干净」）")
    errs, warns, undet = cs.check_subject_present(
        [_shot()], scanned={"s01": {"status": "scanned", "scanned": {"subjects": 2}}})
    check(len(undet) == 1 and warns == [],
          f"镜像里的母版比这条链旧（没带两个占比）→ 未判，不是「很小」：{undet}")
    # 真量到 0 与没量到必须分开：0 走打回，None 走未判。
    errs, _, undet = cs.check_subject_present(
        [_shot(subject_channel="rough")],
        scanned={"s01": _scanned(subjects=0, subject_share=0.0, text_share=0.1)})
    check(len(errs) == 1 and undet == [],
          "**量到了** 0 个图元 → 打回（0 是一个读数，None 才是「没量到」）")

    print("\n〔⑲-a 读数形状〕bool 是 int 的子类：拿 True 当 1.0 就是「没量到写成量到了」")
    check(cs._as_float(0.3) == 0.3 and cs._as_float(0) == 0.0,
          "真数照收（0 也是真数）")
    check(cs._as_float(True) is None and cs._as_float("0.3") is None
          and cs._as_float(None) is None,
          "True / 字符串 / None 一律算没量到——不顺手转成 1.0 / 0.3 / 0")

    # =================================================================
    print("\n〔闸⑧ 字压图〕这一格 2026-10-16 之前全仓没有一处代码在问")
    _th8 = {"subject_overlap_ratio": 0.3, "subject_max_share": 0.85}

    def _lay(findings=(), thresholds=_th8, **over):
        """闸⑧ 的一行回读：在 `_scanned` 之上加 `findings` / `thresholds`。
        `thresholds` 单独一个形参——它属于**行**而不是 `scanned` 那一栏。"""
        row = _scanned(**over)
        row["findings"] = [dict(f) for f in findings]
        row["thresholds"] = None if thresholds is None else dict(thresholds)
        return row

    _PRESS = {"kind": "overlap_subject", "texts": ["工业革命之后的富态"],
              "box": {"x": 120, "y": 400, "w": 300, "h": 90},
              "ratio": 0.62, "share": 0.31}

    errs, warns, undet = cs.check_text_over_subject([_shot()], scanned={"s01": _lay()})
    check(errs == [] and warns == [] and undet == [],
          f"没有命中 → 一声不响（绿：{errs}{warns}{undet}）")

    errs, warns, undet = cs.check_text_over_subject(
        [_shot()], scanned={"s01": _lay(findings=[_PRESS])})
    check(len(errs) == 1 and "压在画出来的图形上" in errs[0],
          f"一条 overlap_subject → 打回：{errs[:1]}")
    check("0.62" in errs[0] and "0.3" in errs[0] and "工业革命之后的富态" in errs[0],
          "打回的话里点名是哪块字、盖住多少、判据是哪条——"
          "「压住了」这句话本身没法改成代码，改法必须跟着读数一起给")
    check(warns == [] and undet == [],
          "打回那一档不再往提醒/未判里各抄一份（同 ⑲-a 的规矩）")

    # **这一条是本判据的边界**：⑧ 的另外三类命中是「几何猜的」，真画面上可能是
    # 合法设计（刻意叠字、指向画面外的暗示），只有字压图这一种没有「刻意的」读法。
    errs, _, _ = cs.check_text_over_subject(
        [_shot()], scanned={"s01": _lay(findings=[
            {"kind": "overlap", "texts": ["甲", "乙"], "ratio": 0.9},
            {"kind": "clipped", "texts": ["丙"], "side": "right", "px": 124},
            {"kind": "dangling", "texts": ["丁"], "at": {"x": 1, "y": 2}}])})
    check(errs == [],
          f"叠字/被裁/悬空三类**不**在这一格判红（它们由回执去说，不拖回看环）：{errs}")

    errs, _, undet = cs.check_text_over_subject([_shot()], scanned=None)
    check(errs == [] and len(undet) == 1 and "未判不是通过" in undet[0],
          f"整片没有回读 → 未判（既不红也不绿）：{undet}")
    errs, _, undet = cs.check_text_over_subject(
        [_shot()], scanned={"s01": {"status": "missing", "scanned": None}})
    check(errs == [] and len(undet) == 1,
          "这一镜没回读到（母版/测量壳都没报）→ 未判，不读成「干净」")

    # 「验过、没事」与「没验过」分得开：纯图元镜 `texts=0` 是**结构上不可能**犯这个错，
    # 不是「没量到」。判成未判会让每一镜没有文字的片子都进回看环重烧。
    errs, _, undet = cs.check_text_over_subject(
        [_shot()], scanned={"s01": _lay(texts=0, subjects=3)})
    check(errs == [] and undet == [],
          "扫过了但这一镜一处文字都没有 → **绿**，不是未判（它结构上不可能压住字）")

    errs, _, _ = cs.check_text_over_subject(
        [_shot()], scanned={"s01": _lay(findings=[_PRESS], thresholds={})})
    check(len(errs) == 1 and "未知" in errs[0],
          f"读数没带判据时明写「未知」，不填一个默认数字冒充：{errs[:1]}")

    print("\n〔闸⑧ 字压图 单源〕判据数值只有 TS 那一份，Python 侧一个都不许抄")
    t8_src = inspect.getsource(cs.check_text_over_subject)
    check("0.3" not in t8_src and "0.85" not in t8_src and "LAYOUT_PROBE" not in t8_src,
          "判据只从读数里的 `thresholds` 取，源码里没有第二个数值来源")
    check("subject_overlap_ratio" in t8_src and "subject_max_share" not in t8_src,
          "只引判红的那一条阈值——`subject_max_share` 是母版那边的**剔除**规则，"
          "Python 侧连它的名字都不该出现（出现就是准备在本地再剔一遍）")

    # =================================================================
    print("\n〔闸㉝ 具名件〕「画的是文案点到的那件东西」的第一格：代码里得真有一件具名的")
    # 这一节判的是 2026-10-17 那条新判据。它接在闸⑦ 后面把**地板抬高一格**：
    # ⑦ 只问「画了没有」（一个 `<path>` 就算数），㉝ 问「画的那件东西有没有名字」。
    # 红样取自真机：那一镜申报了「富态轮廓的点阵图」，代码是 180 个点摆成一个椭圆——
    # ⑦ 放它过去，因为椭圆也是「画出来的东西」。
    _S09_DOTS = ('export const Shot = () => {\n'
                 '  const pts = Array.from({length: 180}, (_, i) => {\n'
                 '    const a = (i / 180) * Math.PI * 2;\n'
                 '    return [960 + Math.cos(a) * 320, 540 + Math.sin(a) * 180];\n'
                 '  });\n'
                 '  return (\n'
                 '    <AbsoluteFill style={{backgroundColor: "#f6f1e4"}}>\n'
                 '      <svg viewBox="0 0 1920 1080" width={1920} height={1080}>\n'
                 '        {pts.map(([x, y], i) => (\n'
                 '          <circle key={i} cx={x} cy={y} r={6} fill="#23211c" />\n'
                 '        ))}\n'
                 '      </svg>\n'
                 '    </AbsoluteFill>\n'
                 '  );\n'
                 '};\n'
                 'export default Shot;\n')
    # ㉝（每镜至少用一件简笔画零件）与 `drawn_parts` **2026-10-24 一起删除**：
    # 它只判「import 名出现过」，拦不住「画得不像」，却逼所有镜依赖一套固定模板；
    # 用户 2026-10-18 就要求「不要再给一堆模板」，零件库降级为可选参考件。
    check(not hasattr(cs, "check_named_parts") and not hasattr(cs, "drawn_parts"),
          "㉝ 与它的识别函数一起走了——零件库现在是可选参考件，不是必用件")

    # 单源：那 13 个件名只在 `_DRAW_PARTS` 写一遍，同时喂 import 白名单与「rough 通道」。
    # 不这么办的话，「用库里的人偶画画」会被闸⑦ 判成「只画了文字与色块」——一件真画了
    # 东西的镜反而被打回，那是最坏的一种错（判据把人往反方向赶）。
    _USE = ('import { Machine } from "@dg/lib/draw/Machine";\n'
            + _S09_DOTS.replace('<svg ', '<Machine x={960} y={880} w={520} kind="steam" />\n      <svg ', 1))
    check(all(p in cs.IMPORT_WHITELIST for p in cs._DRAW_PARTS)
          and set(cs._DRAW_PARTS).issubset(set(cs.PICTORIAL_CHANNELS["rough"])),
          "13 个件既在白名单里、又记在 `rough` 通道上（一处常量两个读者）")
    check("rough" in cs.shot_used_channels(_USE),
          f"用库里的件画画 = 真用上了 `rough` 通道（实得 {cs.shot_used_channels(_USE)}）"
          "——不然闸⑦ 会把一件真画了东西的镜判成「只画了文字与色块」，"
          "那是最坏的一种错（判据把人往反方向赶）")

    # =================================================================
    print("\n〔⑳(c) 判据本体〕两镜代码可以写得不同，画面上却是同一张卡")
    errs, warns, undet = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": _scanned(**_CARD),
                 "s02": _scanned(subject_share=0.55, text_share=0.11)})
    check(errs == [] and undet == [],
          f"两镜指纹不同 → 绿（主体占比不同只是顺带，判的是指纹）：{errs}")

    # 红样：**代码不同、画面同一张**——这正是闸⑨ 看不见的那一格（它比代码骨架，
    # 而且还要 ≥3 镜才定罪），所以红样里的两镜 code 必须真的不一样。
    card_shots = [
        _shot(1, code="export const Shot = () => (<FilmLayout><circle r={300}/></FilmLayout>);"),
        _shot(2, code="export const Shot = () => (<FilmLayout><rect w={200}/></FilmLayout>);"),
        _shot(3, code="export const Shot = () => (<FilmLayout><path d='M0 0h9'/></FilmLayout>);"),
    ]
    errs, warns, undet = cs.check_degenerate_forms(
        card_shots, scanned={"s01": _scanned(**_CARD),
                             "s02": _scanned(**_CARD),
                             "s03": _scanned(subject_share=0.61, text_share=0.09)})
    check(len(errs) == 1 and "同一张卡" in errs[0],
          f"三镜里 s01/s02 画面同构 → 恰好一条打回：{errs}")
    check("s01" in errs[0] and "s02" in errs[0] and "s03" not in errs[0],
          "打回的话点名**是哪一对**（不是含混地说「本片有重复」）")
    check("0x" in errs[0] and "2b2b3c" in errs[0],
          f"话里带着**两个读数**（网格指纹 + 主色）——取证要的是数不是「像」：{errs[0][:60]}…")
    check(warns == [] and undet == [],
          "打回那一档不再往提醒/未判里各抄一份（三态不重号）")

    # **并列断言**：同一条片子上，闸⑨（跨镜同构）**已于 2026-10-24 删除**。
    # ⑨ 比的是代码骨架、要 ≥3 镜才定罪；它判「像不像 PPT」，属于审美，
    # 交给静帧视觉评审。⑳(c) 比的是**画面**、两镜即算，那一格不受影响。
    check(not hasattr(cs, "check_shot_structure"),
          "闸⑨ 已删——判「跨镜同构」交给静帧视觉评审，不再用代码骨架定罪")

    # 几何撞上、配色不同 → 放行：同版式不同配色是**合法**的系列镜头。
    errs, warns, undet = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": _scanned(**_CARD),
                 "s02": _scanned(**{**_CARD, "colors": ["#0a3d62", "#ffffff"]})})
    check(errs == [] and undet == [],
          f"指纹撞上而配色不同 → 放行（「同版式不同配色」不该被这条拦住）：{errs}")
    # 配色按**集合**比，不按频次序比：频次相近的两个色在两条片子上会互换名次，
    # 按位比就会把同一张卡判成不同（那正是「读数的排法」冒充「判据」）。
    errs, _, _ = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": _scanned(**{**_CARD, "colors": ["#2b2b3c", "#e8e8f0"]}),
                 "s02": _scanned(**{**_CARD, "colors": ["#e8e8f0", "#2b2b3c"]})})
    check(len(errs) == 1, f"同一组颜色换个次序 → 照样定罪（比的是集合）：{errs[:1]}")

    # 只撞一半不算：文字压得一样但图元铺得不一样。
    errs, _, _ = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": _scanned(**_CARD),
                 "s02": _scanned(**{**_CARD,
                                    "grid_drawn": 0b111111_000000_000000_000000_000000_000000})})
    check(errs == [], f"只有文字网格一样 → 绿（两条腿都要撞上）：{errs}")

    # 0 是「这一镜没有这一类东西」：两个 0 相等不构成候选（否则纯文字镜会互相定罪）。
    errs, _, _ = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": _scanned(grid_text=0, grid_drawn=0, colors=[]),
                 "s02": _scanned(grid_text=0, grid_drawn=0, colors=[])})
    check(errs == [], f"两镜都没文字也没图元（读数都是 0）→ 绿，不拿 0 当指纹：{errs}")

    print("\n〔⑳(c) 出口三态〕几何撞上而主色认不出 → 这一对**没判完**（D-未判不绿）")
    errs, warns, undet = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": _scanned(**{**_CARD, "colors": []}),
                 "s02": _scanned(**_CARD)})
    check(errs == [] and len(undet) == 1 and "没认出来" in undet[0],
          f"一边认不出主色 → 未判（既不当「相同」也不当「不同」）：{undet}")
    check("s01" in undet[0], "未判那条点名是哪一镜的主色没认出来")

    errs, warns, undet = cs.check_degenerate_forms([_shot(1), _shot(2)], scanned=None)
    check(errs == [] and warns == [] and len(undet) == 2,
          f"整片没有回读 → 两镜各一条未判（既不红也不放行）：{len(undet)} 条")
    errs, _, undet = cs.check_degenerate_forms(
        [_shot(1), _shot(2)],
        scanned={"s01": {"status": "unscanned", "scanned": None},
                 "s02": _scanned(**_CARD)})
    check(len(undet) == 1 and "闸⑧" in undet[0],
          f"这一镜没扫成 → 未判，且说得出是哪一趟落空：{undet}")
    errs, _, undet = cs.check_degenerate_forms(
        [_shot(1)], scanned={"s01": _scanned(grid_text=None, grid_drawn=None)})
    check(len(undet) == 1 and "旧" in undet[0],
          f"镜像里的母版比这条链旧（没带粗网格）→ 未判，不是「不重复」：{undet}")

    print("\n〔⑳(c) 读数形状〕bool 是 int 的子类：True 掩码毫无意义，只能算没量到")
    check(cs._as_int(0) == 0 and cs._as_int(37) == 37, "真整数照收（0 也是真数）")
    check(cs._as_int(True) is None and cs._as_int(None) is None
          and cs._as_int("37") is None and cs._as_int(3.0) is None,
          "True / None / 字符串 / 浮点一律算没量到——不顺手转成 1")

    # =================================================================
    # ㉑ 动效锚定（R8-T33）。这一道要的是**两个红样**（出处：§6-T33 的验收口径、
    # §8.3 第 7 支反例）：①`anchors: []` 的事件 → 闸前红；②触发帧落在所锚数据
    # 「首次可读」窗口外 >k 帧 → 闸后红。红样先写、先按红印在屏幕上，再写绿样。
    print("\n〔㉑ 闸前〕「有动效却不说为谁动」在烧像素之前就拒（一次请求都不该发出去）")

    def _mo(*anchors: str, kind: str = "标题浮入") -> dict:
        return {"kind": kind, "anchors": list(anchors)}

    no_anchor = cs.check_motion_anchors([_shot(motion=[_mo()])])
    check(len(no_anchor) == 1 and "没有锚" in no_anchor[0],
          f"`anchors: []` 即红——空数组说的正是「这条动效不为任何数据服务」：{no_anchor}")
    no_kind = cs.check_motion_anchors([_shot(motion=[_mo("subtitle:1", kind=" ")])])
    check(len(no_kind) == 1 and "没写 kind" in no_kind[0],
          f"说不出是什么动效就没法说它服务谁：{no_kind}")
    not_list = cs.check_motion_anchors([_shot(motion={"kind": "浮入"})])
    check(len(not_list) == 1 and "不是清单" in not_list[0],
          f"motion 自己不是清单也打回（错在形状上时要能说清错在哪）：{not_list}")
    bad_item = cs.check_motion_anchors([_shot(motion=["浮入"])])
    check(len(bad_item) == 1 and "不是对象" in bad_item[0],
          f"清单里混进一条非对象：{bad_item}")

    print("\n〔㉑ 闸前〕锚的写法是白名单：认不出的写法一律打回")
    for raw, why in [("field.title", "少了个 s"), ("subtitle:0", "从 0 数起"),
                     ("subtitle: 1", "中间有空格"), ("subtitle:1.0", "小数写法"),
                     ("beat:01", "前导零"), ("title", "没写前缀"), ("", "空串")]:
        got = cs.check_motion_anchors([_shot(motion=[_mo(raw)])])
        check(len(got) == 1 and "认不出" in got[0],
              f"{raw!r}（{why}）→ 红：一个写错的锚在回执上看着和真锚一模一样")
    check(cs.parse_motion_anchor("fields.title") == ("fields", "title")
          and cs.parse_motion_anchor("subtitle:12") == ("subtitle", "12")
          and cs.parse_motion_anchor("beat:3") == ("beat", "3"),
          "三种写法的正常形态各解析一次（对照上面那七支）")

    print("\n〔㉑ 闸前〕锚到本镜**没声明过**的字段，与没锚同罪（⑥ 的白名单）")
    miss_f = cs.check_motion_anchors([_shot(motion=[_mo("fields.title")])])
    check(len(miss_f) == 1 and "没声明过这个字段" in miss_f[0]
          and "一个字段都没声明" in miss_f[0],
          f"没声明字段时连清单都报得出来：{miss_f}")
    hit_f = cs.check_motion_anchors([_shot(fields=[{"name": "title"}],
                                           motion=[_mo("fields.title")])])
    check(hit_f == [], f"声明过的字段照过：{hit_f}")
    wrong_f = cs.check_motion_anchors([_shot(fields=[{"name": "body"}],
                                             motion=[_mo("fields.title")])])
    check(len(wrong_f) == 1 and "声明的是：body" in wrong_f[0],
          f"锚到别的字段上时把真清单报出来（不然模型无从改）：{wrong_f}")
    check(cs.check_motion_anchors([_shot()]) == [],
          "一条都没申报 → 闸前一句话不说（「没有动效」与「有动效不说为谁动」是两句话，"
          "后者归闸后按像素数出来的段判）")

    print("\n〔㉑ 闸前〕锚挂了几个就按几个算：上限堵掉「把整句话的词全锚上」")
    many = cs.check_motion_anchors([_shot(motion=[_mo("subtitle:1", "subtitle:2",
                                                      "subtitle:3", "subtitle:4")])])
    check(len(many) == 1 and f"上限 {cs.MOTION_MAX_ANCHORS}" in many[0],
          f"4 个锚 → 红（判据是「每一个锚都要在窗口内」，挂满了等于没锚）：{many}")
    check(cs.check_motion_anchors([_shot(motion=[_mo("subtitle:1", "subtitle:2",
                                                     "subtitle:3")])]) == [],
          "刚好三个照过（上限不是「只允许一个」）")

    print("\n〔㉑ 闸后〕红样：触发帧离所锚数据「首次可读」那一帧太远")
    words = [0, 12, 24, 36]                       # 本镜旁白四个词的帧刻度
    fps = 30
    tol = max(1, fps // cs.MOTION_ANCHOR_TOLERANCE_DIV)
    check((tol, cs.MOTION_ANCHOR_TOLERANCE_DIV) == (10, 3),
          f"容差 k = fps // {cs.MOTION_ANCHOR_TOLERANCE_DIV}（@30fps = {tol} 帧）"
          f"——按时间走而不是写死帧数")
    near = cs.check_motion_windows(_shot(motion=[_mo("subtitle:2")]),
                                   events=[{"start_frame": 18, "end_frame": 24}],
                                   word_frames=words, fps=fps)
    check(near == ([], [], []),
          f"第 2 个词的首帧是 {words[1]}、事件触发在第 18 帧，差 6 ≤ {tol} → 过了：{near}")
    far = cs.check_motion_windows(_shot(motion=[_mo("subtitle:2")]),
                                  events=[{"start_frame": 30, "end_frame": 36}],
                                  word_frames=words, fps=fps)
    check(len(far[0]) == 1 and "差 +18 帧" in far[0][0] and f"容差 ±{tol}" in far[0][0],
          f"差 18 > {tol} → 红，并把差几帧、容差多少都印出来：{far[0]}")
    every = cs.check_motion_windows(_shot(motion=[_mo("subtitle:1", "subtitle:4")]),
                                    events=[{"start_frame": 0, "end_frame": 6}],
                                    word_frames=words, fps=fps)
    check(len(every[0]) == 1 and "subtitle:4" in every[0][0],
          f"**每一个**锚都要在窗口内：一个在（subtitle:1 差 0）、一个不在"
          f"（subtitle:4 差 36）→ 照样红：{every[0]}")

    print("\n〔㉑ 闸后〕红样：像素上数得出先后变化、却**一条都没申报**（§8.3 第 7 支）")
    silent = cs.check_motion_windows(_shot(),
                                     events=[{"start_frame": 5}, {"start_frame": 20}],
                                     word_frames=words, fps=fps)
    check(len(silent[0]) == 1 and "一条动效都没申报" in silent[0][0]
          and "2 段" in silent[0][0],
          f"这正是 ⑭ 只数事件、不查锚而静默放过去的那一支：{silent[0]}")
    one_seg = cs.check_motion_windows(_shot(), events=[{"start_frame": 5}],
                                      word_frames=words, fps=fps)
    check(one_seg[0] == [] and len(one_seg[2]) == 1 and "没申报动效" in one_seg[2][0],
          f"只数到 1 段（⑭ 那一档本来就过不去）→ 未判，不拿它凑一条红：{one_seg[2]}")

    print("\n〔㉑ 闸后〕三出口分清：算不出窗口的落**未判**，不许当通过，也不许当红")
    blind = {
        "fields.title（本镜声明过）": cs.check_motion_windows(
            _shot(fields=[{"name": "title"}], motion=[_mo("fields.title")]),
            events=[{"start_frame": 5}], word_frames=words, fps=fps),
        "beat:2（这条链没有节拍表）": cs.check_motion_windows(
            _shot(motion=[_mo("beat:2")]),
            events=[{"start_frame": 5}], word_frames=words, fps=fps),
        "subtitle:9（旁白只有 4 个词）": cs.check_motion_windows(
            _shot(motion=[_mo("subtitle:9")]),
            events=[{"start_frame": 5}], word_frames=words, fps=fps),
    }
    for label, (e, _w, u) in blind.items():
        check(e == [] and len(u) == 1 and "没判过" in u[0],
              f"{label} → 未判（不是通过、也不是红）：{u}")
    check("反推" in blind["fields.title（本镜声明过）"][2][0],
          "`fields.` 落未判的理由要写出来：字段从第 0 帧就注进去了，"
          "「画面上首次可读」这一层反推不出来（反推它就是在做 ⑲-b 要模型做的那件事）")
    check("没有节拍表" in blind["beat:2（这条链没有节拍表）"][2][0],
          "`beat:` 落未判的理由是这条链压根没有节拍检测（不是「节拍对不上」）")
    beat_ok = cs.check_motion_windows(_shot(motion=[_mo("beat:2")]),
                                      events=[{"start_frame": 15, "end_frame": 20}],
                                      word_frames=words, fps=fps, beats=[0, 15, 30])
    check(beat_ok == ([], [], []),
          f"把节拍表喂进来时这条路是通的（今天没人喂——所以它必然落未判）：{beat_ok}")

    print("\n〔㉑ 闸后〕条数对不上 → 未判（对不齐就说不清哪条对哪段），这是一条已知绕法")
    mism = cs.check_motion_windows(_shot(motion=[_mo("subtitle:2")]),
                                   events=[{"start_frame": 18}, {"start_frame": 40}],
                                   word_frames=words, fps=fps)
    check(mism[0] == [] and len(mism[2]) == 1 and "两边对不上" in mism[2][0]
          and "没判过" in mism[2][0],
          f"申报 1 条、像素 2 段 → 未判（段数被像素切成两段完全可能，不判红）：{mism[2]}")
    check("Q98" in inspect.getsource(cs.check_motion_windows),
          "这条绕法在判据的 docstring 里点名记在 §9-Q98——不许在交付语里被抹掉")

    print("\n〔㉑ 闸后〕提醒栏：重复的锚（判据的份量得按**不同数据**算）")
    dup = cs.check_motion_windows(_shot(motion=[_mo("subtitle:2", "subtitle:2")]),
                                  events=[{"start_frame": 18}], word_frames=words, fps=fps)
    check(dup[0] == [] and len(dup[1]) == 1 and "重复项" in dup[1][0],
          f"`['subtitle:2','subtitle:2']` 看着像两个锚都在窗口里、其实只锚了一个数据"
          f"——出声但不拦（它没有错，只是比看上去薄）：{dup[1]}")
    partial = cs.check_motion_windows(
        _shot(fields=[{"name": "title"}],
              motion=[_mo("fields.title", "subtitle:1")]),
        events=[{"start_frame": 0, "end_frame": 6}], word_frames=words, fps=fps)
    check(partial[0] == [] and len(partial[2]) == 1,
          f"一个锚在窗内、另一个算不出窗口 → 未判（有一个没判过就是没判过）：{partial}")
    partial_far = cs.check_motion_windows(
        _shot(fields=[{"name": "title"}],
              motion=[_mo("fields.title", "subtitle:1")]),
        events=[{"start_frame": 40}], word_frames=words, fps=fps)
    check(len(partial_far[0]) == 1 and len(partial_far[2]) == 1,
          f"算得出窗口的那个锚越界 → 红（红优先），同时把没算出来的那条记未判："
          f"{partial_far}")

    print("\n〔㉑ 缓存还原〕申报不进缓存键，所以命中缓存必须拿**当前申报**重判")
    check("motion_anchors" in cr.META_FIELDS and "motion_anchor_frames" in cr.META_FIELDS,
          "㉑ 的两个读数在 META_FIELDS 里：命中缓存的镜不重跑 TTS，旁白词表只能从缓存还原"
          "——少了它就等于把字幕锚全记成假未判")
    render_src = (_HERE.parents[1] / "storyline_server" / "motion" / "code_render.py"
                  ).read_text("utf-8")
    check(render_src.count("cspec.check_motion_windows") == 2,
          "㉑ 的判据在渲染层出现**两次**：现渲那一路、以及命中缓存那一路（同一份判据、"
          "同一个函数——申报不进缓存键，改申报不该重烧，但判据读的正是申报，"
          "所以缓存镜必须按当前申报重判一遍）")
    cache_src = render_src.split("cached_anchor_errs")[1].split("rec.update(")[0]
    check('rec.get("motion")' in cache_src and
          'rec.get("motion_anchor_frames")' in cache_src,
          "缓存路径重判的两个输入都从缓存还原：⑭ 的像素段 + 旁白词表（不凭空重数）")
    check('rec["motion_anchors"] = ' in render_src,
          "缓存命中后把重判出来的读数写回这一镜的账（不写回就只剩现渲的那几镜有账）")
    check('"motion_anchors": {"warnings"' in render_src
          and '"motion_anchor_frames": [int(w["startFrame"])' in render_src,
          "现渲那一路也把两个读数写进这一镜的账（两条路存的形状必须一样，"
          "否则命中缓存的那几镜在整片账上就少一栏）")

    # =================================================================
    print("\n〔单源与结构闸〕判据的数字与「谁算主体件」都只有一处")
    src_spec = (_HERE.parents[1] / "storyline_server" / "motion" / "code_spec.py"
                ).read_text("utf-8")
    src_render = (_HERE.parents[1] / "storyline_server" / "motion" / "code_render.py"
                  ).read_text("utf-8")
    check(src_render.count("cspec.SUBJECT_MIN_BBOX") == 1
          and src_render.count("cspec.TEXT_DOMINANT_SHARE") == 1
          and src_render.count("SUBJECT_MIN_BBOX = ") == 0
          and src_render.count("TEXT_DOMINANT_SHARE = ") == 0,
          "渲染层只**读**契约的两个常量（各一次、都带 `cspec.` 前缀），"
          "自己这边一次都不重写（重写就会出现两把尺）")
    check("TEXT_DOMINANT_SHARE" in src_spec and
          src_spec.count("TEXT_DOMINANT_SHARE = ") == 1,
          "两个阈值各声明一次")
    # 这道结构闸钉的是**判据的命门**：把 <div> 算进主体件，「卡片+文字」会绿得非常稳。
    #
    # 2026-10-16 起这一段的**主语换了文件**：探针整体从母版搬进
    # `remotion/src/lib/LayoutProbe.tsx`（母版下线之后自包含镜的版面读数恒为 missing，
    # 见那个文件的头注释）。搬的是位置，不是事实——所以下面每条断言的对象跟着换，
    # 并**新增一条**钉「只有一份」：母版必须 import 那份，不许自己再留一份副本
    # （两份探针＝两把尺，且其中一份永不执行）。
    tsx = (ROOT / "remotion" / "src" / "FilmLayout.tsx").read_text("utf-8")
    probe = (ROOT / "remotion" / "src" / "lib" / "LayoutProbe.tsx").read_text("utf-8")
    check("from './lib/LayoutProbe'" in tsx and "function probeLayout" not in tsx
          and "SUBJECT_TAGS" not in tsx,
          "母版只 **import** 探针，自己不另留一份（两份探针＝两把尺，"
          "且住在母版里的那一份对自包含镜永不执行）")
    check("SUBJECT_TAGS" in probe and "'canvas'" in probe and "'img'" in probe,
          "探针里有一份「画得出图形的那几类元素」的清单")
    check("'div'" not in probe.split("SUBJECT_TAGS")[1][:400],
          "**div 不在里面**——涂了底色的 div 就是那张卡片本身，算进主体这条闸就自废了")
    check("shareOfBoxes" in probe and "subject_share" in probe and "text_share" in probe,
          "探针报的是两个**原始读数**（占比），阈值不写在 TS 那一侧")
    check("subject_share" in src_render and "subjects" in src_render,
          "回读的三件（个数 + 两个占比）在 Python 这一侧被搬进回执")
    check("subjects" in src_render and "subject_share" in src_render,
          "闸⑧ 那条回读通道把三个读数带过来（不新开一条通道）")

    # 与 ④ 的分工：④ 判「画面上有没有东西」，⑲-a 判「那个东西铺开了多大一片」。
    check("EMPTY_MIN_RATIO" not in src_spec and "SPAN_MIN" not in src_spec,
          "⑲-a 不重造闸④ 的两个数——它读的是母版量的主体/文字两个新读数")

    # ⑳(c)：判据本体一处、读数只搬不重算、不重复实现 ⑳(a)(b)。
    check(src_render.count("cspec.check_degenerate_forms") == 1
          and src_render.count("check_degenerate_forms") == 1,
          "渲染层只调一次 ⑳(c) 的判据（拼接之前那一道），自己这边不重算")
    check("DEGEN_GRID" not in src_render and "gridSignature" not in src_render,
          "粗网格与指纹都长在母版那一侧，Python 只读它的产出")
    deg_src = inspect.getsource(cs.check_degenerate_forms)
    check("check_subject_present" in deg_src and "fill_span" in deg_src,
          "⑳(c) 的 docstring 里点名 ⑳(a) 归 check_subject_present、⑳(b) 归闸④ —— "
          "**引用而不重写**（同一判据抄成第二份就是清单漂移）")
    check("gridSignature" in probe and "colorHistogram" in probe and "DEGEN_GRID" in probe,
          "探针里报的是两个**原始读数**（粗网格指纹 + 主色），阈值不写在 TS 那一侧")
    # P2（2026-10-17）之后，读「声明的颜色」只剩 `colorHistogram` 一处。
    # 这一条钉的是：它收的是 DOM 里声明的颜色字段，不是对像素做直方图。
    hist = probe.split("function colorHistogram")[1].split("\n}")[0]
    check("backgroundColor" in hist and "getAttribute('fill')" in hist
          and "getImageData" not in hist and "drawImage" not in hist,
          "主色收的是 DOM 里**声明的**颜色字段（不是对像素做直方图）——"
          "这一道判的是「谁画的」而不是「看起来像不像」，也就不用多跑一趟解码")

    # ㉑：闸前那一半挂在**整片判据那一个出口**上（不烧像素），闸后那一半只长在渲染层；
    # 容差与上限各只有一处，且都不在 TS 那一侧——母版只回「哪些帧动过」，判它够不够近的
    # 是 Python。
    # 2026-10-16 并档：闸前那一半从 `validate_code_spec` 搬进了 `whole_film_gate_results`
    # （判据一律只报不拦，见那个函数的 docstring）。搬的只是**位置**——这一条钉的
    # 「恰好一处」不变，所以它跟着改的是"在哪一个函数里"，不是"有几处"。
    check(src_spec.count("MOTION_MAX_ANCHORS = ") == 1
          and src_spec.count("MOTION_ANCHOR_TOLERANCE_DIV = ") == 1
          and src_spec.count("errors.extend(check_motion_anchors(shots))") == 1
          and "def whole_film_gate_results" in src_spec,
          "㉑ 闸前的四个判据挂在整片判据那一个出口上，上限与容差各声明一次")
    check("MOTION_ANCHOR" not in tsx,
          "容差与上限都不在 TS 那一侧：母版只回「哪几帧动过」，判它够不够近的是 Python")
    nodes_src = (_HERE.parents[1] / "storyline_server" / "nodes" / "motion_code_nodes.py"
                 ).read_text("utf-8")
    check("MOTION_ANCHOR_TOLERANCE_DIV" in nodes_src
          and "cspec.MOTION_ANCHOR_TOLERANCE_DIV" in nodes_src,
          "回执里印出来的容差也引同一个常量（不在这里抄第二个 k）")
    check(src_render.count("fps=fps") >= 2,
          "渲染层两次调用都**把 fps 递给判据**、不自己折成帧数（k 的定义只在一处）")
    shot_sc = cs.spec_schema()["properties"]["shots"]["items"]
    check("motion" in shot_sc["properties"] and "motion" not in shot_sc["required"],
          "schema 里 `motion` 存在但**不进 required**：缺了它 = 「这一镜压根没有动效」"
          "（归闸⑭ 按像素判），空锚 = 「有动效却不说为谁动」（归 ㉑ 红）——两句话")
    check(shot_sc["properties"]["motion"]["items"]["required"] == ["kind", "anchors"],
          "每条动效的 kind 与 anchors 都是必填（schema 层就挡住形状错）")
    check(shot_sc["properties"]["motion"]["items"]["properties"]["anchors"]["minItems"] == 1
          and shot_sc["properties"]["motion"]["items"]["properties"]["anchors"]["maxItems"]
          == cs.MOTION_MAX_ANCHORS,
          "anchors 的上下限与 Python 判据同源（minItems 1 / maxItems 引用同一个常量）")
    check("MOTION_ANCHOR_FORMS" in inspect.getsource(cs.check_motion_anchors),
          "闸前的白名单引用同一个常量，不在判据里另写一份「三种写法」")
    check("motion_anchor_unjudged" in nodes_src and "motion_anchor_warnings" in nodes_src
          and "motion_unjudged" in nodes_src,
          "回执上 ㉑ 的两个出口**另起两栏**，不并进 ⑭ 那两栏：一镜可能「动效密度过了」"
          "而「锚定未判」，并栏就是把前者当后者")
    # 「申报了锚的镜」必须读 spec 里那一栏：账里的 `r["motion"]` 是 ⑭ 从像素上数出来的
    # 读数（每一镜都有一份），拿它当申报，这一行就会把「这一镜有动效」说成「这一镜申报了锚」。
    check('for s in spec["shots"] if s.get("motion")' in nodes_src,
          "回执里「几镜申报了锚」数的是 spec 的申报栏，不是 ⑭ 的读数栏")

    print("\n未验证清单（跳过 ≠ 通过）：")
    print("  · 真像素那一格：⑲-a 的几何读数与 ⑳(c) 的画面指纹都要靠真渲一镜才量得出来，"
          "本文件里的回读行是构造的（真机取证在 layout-probe.mjs 与 §8.1 矩阵）")
    print("  · ㉑ 的两个红样本文件都造出来了（闸前空锚、闸后越窗），但**真机那一半**还欠着："
          "① 触发帧来自闸⑭ 的真像素采样、词刻度来自真 TTS，两者在真片上的差分布没人量过"
          "（k=fps//3 的标定脚本 §6 附录 B 要求、尚不存在，记在 §9-Q98）；"
          "② `fields.<key>` 与 `beat:<n>` 两条锚今天必然落**未判**——`beat:` 这条链没有节拍表，"
          "`fields.` 的「画面上首次可读」这一层反推不出来。**所以「锚到必然存在字段上」这条"
          "绕法今天只被闸前那一半挡着**（字段必须真在本镜 fields 里），窗口那一半挡不住")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
