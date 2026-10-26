# -*- coding: utf-8 -*-
"""多镜编排的**纯逻辑**测试：时间表算术、转场映射、主色提纯、模块结构。

为什么不测"渲出来好不好看"：那要靠抽帧质检与真机冒烟（`tools/smoke_film.py`）。
这个文件只钉那些**算错了不会报错、只会悄悄少半秒**的地方——正是上一版
`<Series>` 换 `TransitionSeries` 时最容易踩的坑。

    python tests/test_simple_film.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storyline_server.simple import director, film, kernel, qa, system  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _FAILS += 1
        print(f"  XX   {label}")


#: 一段最小的、能过 lint 的场景体（4 件组件，含 Ribbon / HudFrame）。
SCENE = '''const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <AbsoluteFill style={{ backgroundColor: '#0a0c12' }}>
      <Backdrop hue="#22D3EE" t={t} />
      <Ribbon hue="#FF6B2C" t={t} y={0.58} />
      <HudFrame t={t} />
      <Caption text="测试字幕" t={t} size={0.08} />
    </AbsoluteFill>
  );
};'''


def main() -> int:
    print("\n① 时间表：总帧数 = Σ序列 − Σ转场，且与 composition 的 durationInFrames 一致")
    tl = film.timeline([2.0, 2.0, 2.0], ["slide", "cut"])
    check(tl["base"] == [60, 60, 60], f"3 镜各 2 秒 → 各 60 帧（{tl['base']}）")
    check(tl["trans"] == [film.TRANS_FRAMES, 0, 0],
          f"只有第一处是转场，第二处是硬切（{tl['trans']}）")
    check(sum(tl["durs"]) - sum(tl["trans"]) == tl["total"],
          f"Σ序列−Σ转场 == 总帧数（{sum(tl['durs'])}−{sum(tl['trans'])} == {tl['total']}）")
    check(tl["total"] == 180, f"6 秒 @30fps = 180 帧（{tl['total']}）")
    check(tl["starts"][-1] + tl["durs"][-1] == tl["total"],
          f"最后一镜正好收在片尾（{tl['starts'][-1]}+{tl['durs'][-1]}）")
    check(tl["cuts"] == [int(60 + film.TRANS_FRAMES / 2), 120],
          f"切点取过渡中点（{tl['cuts']}）——快门闪要闪在观众感知换镜的那一帧"
          "（第一处 60+8/2=64，第二处是硬切所以就是 120）")

    # 四舍五入的零头：8 镜 × 3.75 秒，逐镜 round 与整段 round 差 4 帧。
    tl2 = film.timeline([3.75] * 8, ["slide"] * 7)
    check(tl2["total"] == 900, f"30 秒 = 900 帧（{tl2['total']}）")
    check(sum(tl2["durs"]) - sum(tl2["trans"]) == 900,
          f"8 镜 3.75 秒的零头被最后一镜吃掉（{sum(tl2['durs'])}−{sum(tl2['trans'])}）")
    check(tl2["starts"][-1] + tl2["durs"][-1] == 900,
          f"没有片尾黑帧（{tl2['starts'][-1]}+{tl2['durs'][-1]}）")

    print("\n② 转场映射：模型漏写不会退化成整片硬切")
    got = film.transitions_for([{}, {}, {}])
    check(len(got) == 2, f"3 镜 → 2 处转场（{got}）")
    check(all(k == "cut" or k in film.TRANS_TSX for k in got),
          f"每一处都是可实现的（{got}）")
    check(got == [film.TRANS_CYCLE[0], film.TRANS_CYCLE[1]],
          f"没写就按 TRANS_CYCLE 发（{got}）")
    kept = film.transitions_for([{"transition": "WIPE"}, {"transition": "cut"},
                                 {"transition": "胡说"}, {}])
    check(len(kept) == 3, f"4 镜 → 3 处转场（{kept}）")
    check(kept[0] == "wipe", f"大小写归一（{kept}）")
    check(kept[1] == "cut", f"cut 是合法值，不是「没写」（{kept}）")
    check(kept[2] == film.TRANS_CYCLE[2 % len(film.TRANS_CYCLE)],
          f"不认识的词按循环兜底（{kept}）")
    check(all(k in film.TRANS_TSX or k == "cut" for k in film.TRANS_CYCLE),
          f"循环表里每一档都实现过（{film.TRANS_CYCLE}）")

    print("\n③ 主色提纯：近黑色抬到「看得出颜色」，正常色不动色相")
    dark = film.ensure_vivid("#0a0c12")
    r, g, b = (int(dark[i:i + 2], 16) for i in (1, 3, 5))
    check(max(r, g, b) >= 200, f"#0a0c12 → {dark}（最亮通道 ≥200）")
    check(b >= r, f"色相没翻：仍是蓝调（b={b} r={r}）")
    keep = film.ensure_vivid("#FF6B2C")
    kr, kg, kb = (int(keep[i:i + 2], 16) for i in (1, 3, 5))
    check(kr > kg > kb and kr >= 240, f"#FF6B2C 原样保留橙调（{keep}）")
    check(film.ensure_vivid("不是颜色") == "#FF6B2C", "非法值退回默认橙")
    check(film.ensure_vivid("#f60").startswith("#"), "三位简写能认（#f60）")

    print("\n④ 模块结构：用 TransitionSeries，不是 Series")
    kinds = ["slide", "cut", "zoom"]
    code, tl3 = film.assemble([SCENE, SCENE, SCENE, SCENE], [2.0] * 4, kinds)
    check("<TransitionSeries>" in code and "</TransitionSeries>" in code,
          "整片套在 TransitionSeries 里")
    check(code.count("<TransitionSeries.Sequence") == 4,
          f"4 镜 = 4 个 Sequence（实际 {code.count('<TransitionSeries.Sequence')}）")
    check(code.count("<TransitionSeries.Transition") == 2,
          f"3 处切点里 2 处是转场（实际 {code.count('<TransitionSeries.Transition')}）")
    check("slide({ direction: 'from-right' })" in code, "slide 带方向")
    check("crossZoom()" in code, "zoom 走 crossZoom")
    check("<Series>" not in code, "没有残留的 <Series>（它放不下 Transition）")
    check("linearTiming" in code and f"durationInFrames: {film.TRANS_FRAMES}" in code,
          f"转场时长 {film.TRANS_FRAMES} 帧走 linearTiming")
    check("const S01" in code and "const S04" in code, "四个场景各自命名，不撞作用域")
    check(code.count("export default Shot;") == 1, "只有一个默认导出")
    check(f"<GlobalFX cuts={{[{', '.join(str(c) for c in tl3['cuts'])}]}}" in code,
          f"快门闪的切点与时间表同源（{tl3['cuts']}）")
    check("Drift" in code and "GlobalFX" in code, "全局层（手持微动 + 七层特效）都在")

    print("\n⑤ 设计系统：新增的两件必须同时进实现与清单")
    names = {m["name"] for m in system.MANIFEST}
    check({"Ribbon", "HudFrame"} <= names, f"清单里有 Ribbon / HudFrame（{sorted(names)}）")
    check("const Ribbon" in system.SYSTEM_TSX and "const HudFrame" in system.SYSTEM_TSX,
          "实现里也有这两个组件——名字对不上不会报错，只会静默退回「自己画」")
    # 清单与实现一一对应：清单里每个名字，实现里都得有 `const <Name>`。
    # 少了不会报错，只会让模型调一个不存在的组件（然后那一镜整段画不出来）。
    missing = [m["name"] for m in system.MANIFEST
               if f"const {m['name']}" not in system.SYSTEM_TSX]
    check(missing == [], f"清单里每个名字在实现里都有对应组件（缺：{missing}）")

    print("\n⑥ 真 lint：拼出来的模块能过闸①")
    try:
        from storyline_server.motion import remotion_client as rc
        got_lint = rc.lint(code)
        errs = got_lint.get("errors") or []
        check(bool(got_lint.get("ok")),
              f"lint 通过（{len(errs)} 条错）"
              + (f"：{[(e.get('rule'), e.get('msg')) for e in errs[:3]]}" if errs else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"  ..   渲染服务不可达，跳过 lint：{exc}")

    print("\n⑦ 确定性补色带：重写两轮仍寡淡的镜，靠几何而不是再求模型")
    plain = '''const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <AbsoluteFill style={{ backgroundColor: '#0a0c12' }}>
      <Caption text="只有一行字" t={t} size={0.08} />
    </AbsoluteFill>
  );
};'''
    fixed = film._inject_ribbon(plain, "#FF6B2C")
    check('<Ribbon hue="#FF6B2C"' in fixed, "没色带的镜被插进一条 Ribbon")
    check(fixed.index("<Ribbon") < fixed.index("<Caption"),
          "色带插在主体之前（背景之上、内容之下，不遮画面）")
    check(fixed.count("<AbsoluteFill") == plain.count("<AbsoluteFill"),
          "只加子元素，不改结构")
    check(film._inject_ribbon(fixed, "#22D3EE") == fixed,
          "已经有色带的不重复插（否则每次复核都加一条，越来越厚）")
    check(film._inject_ribbon("没有 AbsoluteFill 的字符串", "#FF6B2C")
          == "没有 AbsoluteFill 的字符串",
          "认不出容器就原样返回，不猜")

    print("\n⑧ 负尺寸兜底：SVG 负值会静默不渲染，必须在拼装时夹住")
    bad = '<rect x={0} y={0} width={width} height={height * (1 - p * 2)} />'
    ok = film._fix_rects(bad)
    check("height={Math.max(0, height * (1 - p * 2))}" in ok,
          f"负值可能出现的 height 被夹住（{ok}）")
    check("width={Math.max(0, width)}" in ok, "同一条 rect 的 width 也夹（不挑属性）")
    already = '<rect width={Math.max(0, w)} height={Math.max(0, h)} />'
    check(film._fix_rects(already) == already, "已经夹过的不重复夹（不会套两层）")
    check("r={Math.max(0, R * p)}" in film._fix_rects("<circle cx={0} cy={0} r={R * p} />"),
          "circle 的半径同样会负")
    check(film._fix_rects('<line x1={-5} y1={0} x2={10} y2={20} />')
          == '<line x1={-5} y1={0} x2={10} y2={20} />',
          "line 的坐标可以是负数，不碰（夹了反而画错）")
    check(film._fix_rects('<div style={{ width: 10 }} />') == '<div style={{ width: 10 }} />',
          "只管 SVG 标签，不动 div 的样式对象")

    print("\n⑨ 兜底场景：结构完整、组件齐全、文案里的引号不会把代码写坏")
    fb = film.safe_scene('他说"蓝"是 470 纳米', "#22D3EE", index=1)
    check(film._looks_complete(fb), "兜底场景本身是完整组件（括号配平、以 }; 收尾）")
    used = system.used_components(fb)
    check(len(used) >= 5, f"用到的组件 ≥5 件（{used}）")
    check({"Backdrop", "Ribbon", "HudFrame", "Caption"} <= set(used),
          "四件关键组件都在（打底 / 颜色 / 前景 / 文案）")
    check('\\"蓝\\"' in fb, "文案里的双引号被转义，不会截断 JSX 属性")
    check("const Scene" in fb, "导出的是约定的 `const Scene` 名字（assemble 靠它改名）")
    a = film.safe_scene("同一句", "#FF6B2C", index=0)
    b = film.safe_scene("同一句", "#FF6B2C", index=1)
    check(a != b, "不同镜号错开色带角度/高度（连着几镜兜底时不至于一模一样）")

    print("\n⑩ 比例参数兜底：会收「比例」的组件必须过 R()/N()")
    tsx = system.SYSTEM_TSX
    check("const R = " in tsx and "const N = " in tsx,
          "设计系统里有 R()（比例）与 N()（计数）两个兜底函数")
    # 每个组件的源码块 = 从 `const Name` 到下一个 `const `。
    blocks: dict[str, str] = {}
    for part in tsx.split("\nconst ")[1:]:
        name = part.split("=")[0].strip().split(":")[0].strip()
        blocks[name] = part
    ratio_users = ["Backdrop", "Caption", "StatCard", "BarChart", "OrbitDiagram",
                   "LineTrace", "GlowOrb", "ScanGrid", "Particles", "Ribbon", "HudFrame"]
    for name in ratio_users:
        body = blocks.get(name, "")
        check(f"{name}" in blocks and ("R(" in body or "N(" in body),
              f"{name} 收了比例参数并且过 R()/N()")

    print("\n⑪ 时长：180 秒能表达，超过结构上限明确报错（不再静默摊到每镜上）")
    check(film.shot_count(60) == 15, f"60 秒 → 15 镜（{film.shot_count(60)}）")
    check(film.shot_count(180) == 45,
          f"180 秒 → 45 镜（按 4 秒理想节奏；{film.shot_count(180)}）")
    check(film.shot_count(8) == film.MIN_SHOTS,
          f"8 秒也是 {film.MIN_SHOTS} 镜（{film.shot_count(8)}）")

    cap = film.MAX_SHOTS * film.MAX_SHOT_SEC
    for total in (60.0, 120.0, 180.0, 208.0, cap - 1):
        n = film.shot_count(total)
        secs = film.split_seconds(total, n)
        over = [s for s in secs if s > film.MAX_SHOT_SEC + 1e-9]
        check(not over and abs(sum(secs) - total) < 0.02,
              f"{total:g} 秒 / {n} 镜：每镜都 ≤{film.MAX_SHOT_SEC}s 且合计对得上"
              f"（最长 {max(secs):.2f}，合计 {sum(secs):.2f}）")

    over_total = cap + 10
    try:
        film.split_seconds(over_total, film.shot_count(over_total))
        check(False, f"{over_total:g} 秒应当报错（配不出每镜 ≤5.2 秒）")
    except kernel.KernelError as exc:
        check("配不出来" in str(exc) and str(int(cap)) in str(exc),
              f"{over_total:g} 秒明确报错，并说出结构上限 {int(cap)} 秒：{str(exc)[:46]}")
    check(film.shot_count(over_total) == film.MAX_SHOTS,
          f"超上限时仍按 {film.MAX_SHOTS} 镜算——报错发生在分配镜长那一步，不是镜数那一步")

    print("\n⑫ 元素数：把「只有一层」变成可数的东西")
    thin = ('const Scene = ({ dur }: { dur: number }) => { const t = 0;\n'
            '  return (<AbsoluteFill><Backdrop t={t} /><Caption text="一行字" t={t} />'
            '</AbsoluteFill>);\n};')
    check(system.element_count(thin) < qa.MIN_ELEMENTS,
          f"两件组件的镜只有 {system.element_count(thin)} 个元素 < {qa.MIN_ELEMENTS}")
    rich = film.safe_scene("测试", "#22D3EE", index=0)
    check(system.element_count(rich) >= qa.MIN_ELEMENTS,
          f"兜底场景有 {system.element_count(rich)} 个元素（≥{qa.MIN_ELEMENTS}）")
    looped = ('const Scene = () => (<AbsoluteFill>{Array.from({length: 40})'
              '.map((_, i) => <circle key={i} cx={i} cy={i} r={2} />)}</AbsoluteFill>);')
    check(system.element_count(looped) >= qa.MIN_ELEMENTS,
          f"用 map 生成的一批元素按批量计（{system.element_count(looped)}），"
          "否则粒子/网格那种镜会被误判成「只有两个元素」")

    print("\n⑬ 质检：像素合格但元素不够 → sparse，且排在最后才救")
    rows = [{"index": 1, "verdicts": ["ok"], "worst": "ok", "content": 0.5, "vivid": 0.3}]
    check(qa.SEVERITY["sparse"] > qa.SEVERITY["flat"] > qa.SEVERITY["empty"],
          "严重程度：空镜/寡淡先救，层次少最后救")
    hint = qa.hint_for([{**rows[0], "worst": "sparse", "elements": 3}], 0)
    check("3 个视觉元素" in hint and "三层" in hint,
          f"sparse 的打回指令说清「几个元素」和「要补成三层」（{hint[:40]}）")

    print("\n⑭ 旁白定镜长：留白、下限、上限、切尾要报出来")
    from storyline_server.simple import voice as vo  # noqa: PLC0415

    check(vo.fit_secs([2.0]) == [2.45], f"2.0 秒语音 + 0.45 留白（{vo.fit_secs([2.0])}）")
    check(vo.fit_secs([1.0]) == [vo.MIN_SHOT_SEC],
          f"太短的语音抬到下限 {vo.MIN_SHOT_SEC}（{vo.fit_secs([1.0])}）")
    check(vo.fit_secs([9.0]) == [vo.MAX_SHOT_SEC],
          f"太长的语音夹到上限 {vo.MAX_SHOT_SEC}（{vo.fit_secs([9.0])}）")
    check(vo.fit_secs([4.9])[0] <= vo.MAX_SHOT_SEC,
          "贴着上限的语音先压留白，不直接切尾")
    check(vo.over_long([2.0, 5.2, 6.0]) == [2, 3],
          f"语音本身就超上限的镜被点名（{vo.over_long([2.0, 5.2, 6.0])}）")
    check(vo.over_long([2.0, 3.0]) == [], "正常长度的镜不报")
    # 12 字 @+8% 实测 2.5 秒：这条口径用来给"TTS 失败的那几镜"估时长。
    est = round(max(vo.MIN_SHOT_SEC, 12 * 0.22 + vo.GAP_SEC), 2)
    check(2.6 <= est <= 3.4, f"12 字估算 {est} 秒，与实测 2.5 秒同量级")

    print("\n⑮ 补留白：要 100 秒不能只出 69 秒")
    # 真机实测：要 100 秒、11 镜、每句 5.8 秒 → 69.3 秒，只有目标的 69%。
    # 用户要的是"3 分钟的片子"，不是"11 个镜"。
    got = vo.fill_to([6.3] * 11, 100.0, hi=12.0)
    check(abs(sum(got) - 100.0) < 0.5, f"11 镜 6.3 秒 → 总长补到 {sum(got):.1f}（目标 100）")
    check(all(s <= 12.0 + 1e-6 for s in got), f"每镜不超过上限（最大 {max(got):.2f}）")
    check(got == [round(s, 2) for s in got], "两位小数（与工程落盘口径一致）")
    check(vo.fill_to([6.3] * 11, 60.0, hi=12.0) == [6.3] * 11,
          "已经够长就不动（不把 60 秒的片子拉成 100 秒）")
    check(vo.fill_to([11.8] * 11, 200.0, hi=12.0)[0] <= 12.0,
          "目标超过结构上限时只补到上限，不硬凑")
    check(vo.fill_to([], 100.0, hi=12.0) == [], "空输入不炸")
    check(vo.fill_to([6.3] * 11, 0, hi=12.0) == [6.3] * 11, "没给目标就不补")
    # 动效模式不补：快节奏靠加镜数，不靠拉长留白（那是另一件事）。
    check(vo.fill_to([3.6] * 25, 100.0, hi=5.2)[0] <= 5.2, "（动效上限更紧，也不会超）")

    print("\n⑯ 分阶段确认：给了分镜就不再调规划，但 motif / sources 不能丢")
    # 为什么这条重要：一次成片二十几次调用、十几分钟。主题或讲法错了只能整条重跑。
    # 「先看分镜、改完再继续」把错的那一步从 24 次调用降到 1 次。
    # 而这条路的坑在**收编**：把 /simple/plan 的产物传回来时，如果只取 scenes，
    # 跨镜接力（motif）与出处账（sources）会在这一步悄悄丢掉。
    got = film._adopt_plan([{"text": "一句"}])
    check(got["scenes"] == [{"text": "一句"}] and got["motif"] == "" and got["sources"] == [],
          f"只给 list → 只有 scenes（{got}）")
    got = film._adopt_plan({"title": "标题", "motif": "蓝点",
                            "sources": [{"claim": "47% 的人", "source": "X"}],
                            "scenes": [{"text": "一句", "caption": {"mode": "drawn"}}]})
    check(got["title"] == "标题" and got["motif"] == "蓝点",
          "dict 形状 → title 与 motif 都留下（接力不断）")
    check(len(got["sources"]) == 1 and got["sources"][0]["source"] == "X",
          "sources 过一遍归一化后留下（出处账不丢）")
    check(got["scenes"][0]["caption"] == {"mode": "drawn"},
          "每镜的 caption 原样带过去（字幕版式不丢）")
    for bad in (None, [], {}, {"scenes": "不是列表"}, {"scenes": [None, 1]}):
        check(film._adopt_plan(bad)["scenes"] == [], f"_adopt_plan({bad!r}) → 空 scenes，不炸")

    print("\n⑰ 画法自动判 + 镜数由内容定（2026-10-26）")
    # 这两条是「用户只给一个话题」的前提：不该让他看到 explainer/story，
    # 也不该让镜数由公式决定（同样 165 秒，15 镜和 22 镜都可能是对的）。
    import inspect
    check(inspect.signature(film.make_film).parameters["mode"].default == "auto",
          "make_film 的 mode 缺省是 auto（不是 explainer）")

    orig = kernel._complete

    def stub(reply: str):
        async def _fake(messages, **kw):
            return reply
        return _fake

    try:
        kernel._complete = stub("explainer")
        check(asyncio.run(film.pick_style("为什么天空是蓝的")) == film.MODE_EXPLAINER,
              "讲原理 → 判成 explainer")
        kernel._complete = stub("story")
        check(asyncio.run(film.pick_style("敦煌藏经洞")) == film.MODE_STORY,
              "有主角和时间 → 判成 story")
        kernel._complete = stub("随便说点什么")
        check(asyncio.run(film.pick_style("x")) == film.MODE_EXPLAINER,
              "回的不是这两个词 → 落到 explainer（默认分支）")

        async def _boom(messages, **kw):
            raise RuntimeError("模型挂了")
        kernel._complete = _boom
        check(asyncio.run(film.pick_style("x")) == film.MODE_STORY,
              "判不出来按 story 走——有主角的片子用叙事画法不算错，反过来不一定")
        # 长片一律叙事：动效片每镜 ≤5.2 秒，165 秒要 40 镜，碎成 PPT。
        # 实测只给「0」+ 165 秒，判成 explainer 出来 40 镜（每镜 4 秒）。
        kernel._complete = stub("explainer")
        check(asyncio.run(film.pick_style("0", seconds=165)) == film.MODE_STORY,
              "超过 90 秒一律叙事片（不调模型，直接判）")
        check(asyncio.run(film.pick_style("为什么天空是蓝的", seconds=60))
              == film.MODE_EXPLAINER, "90 秒以内才交给模型判内容")
    finally:
        kernel._complete = orig

    # 镜数：提示词里不许再出现"秒数 ÷ 9 / ÷ 4"这种公式
    for name, sysmsg, div in (("叙事", director.STORY_SYSTEM, "÷ 9"),
                              ("动效", director.PLAN_SYSTEM, "÷ 4")):
        check("镜数由内容定" in sysmsg, f"{name}提示词说「镜数由内容定」")
        check(div not in sysmsg, f"{name}提示词里没有「{div}」这个公式")
    check("angle" in director.STORY_SYSTEM and "hook" in director.STORY_SYSTEM,
          "叙事提示词要求先想三个角度、挑钩子最硬的那个")
    # 用户已经给了讲法时不许模型自己另选角度（否则他会把用户的大纲推翻）
    check("已经给了讲法就照他的" in director.STORY_SYSTEM,
          "给了讲法就照用户的，不另选角度")
    got = film._adopt_plan({"angle": "被禁的零", "hook": "0 被禁过",
                            "scenes": [{"text": "x"}]})
    check(got["angle"] == "被禁的零" and got["hook"] == "0 被禁过",
          "分镜的 angle/hook 在「先看分镜→再出片」那条路上不丢")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
