# -*- coding: utf-8 -*-
"""2026-10-17 真机片之后的那一批修复：每一条都先证红（老行为在这里会挂），再证绿。

背景：7fa1e10 能出片，但有五个毛病——字幕样式没上屏、后半截定格、镜数少画面不连贯、
开头 6 镜被整份替换吃掉没人说、闸⑭ 在预览档说「没有进成片」；再加上花钱看不见、没上限。
这一份按毛病分段，每段证的都是「系统自己做对」，**不靠技能正文提醒**——同一天把这些
规则以「不照做就整份打回」写进正文，模型被吓得不敢交分镜，八条 run 全部卡死在读材料上。

① 字幕：`autofix_caption_layer` 补 `<CaptionLayer/>`（import + 顶层收尾前），幂等，
   `none` 档 / 已摆 / 用 `<CaptionText>` 自排的镜不动，插不准就点名不硬插；
② 时长：`fit_duration` 拉长时每镜封顶原长 × `MAX_STRETCH_RATIO`（老行为一律拉到 15 秒）；
③ 连贯：`layout_family_cap` 按镜数放宽（老行为 20 镜每族最多 2 镜，逼出 10 个族）；
④ 整份替换：`shot_diff_notes` 点名少了哪几镜、断了哪几号；
⑤ 风格：`style` 活着穿过 `validate_code_spec`，并出现在确认卡上；
⑥ 闸⑭ 措辞按档说实话；预览版面读数只留可执行的那几类；
⑦ 花费：`cost_yuan` 分命中/未命中计价、没回缓存数按全价、高峰翻倍；
   `usage_from_raw` 认 DeepSeek 与 OpenAI 两种写法；预算开账/续跑延一份/撞线；
   `RunState` 落盘往返不丢账；
⑧ 缓存：历史折叠有回差——折一次就折到一半，下一条小结果进来不再回头改写历史。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_cost_caption_fix.py``
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import agent as agent_mod                      # noqa: E402
from agent_framework import cost                                     # noqa: E402
from agent_framework.compress import ToolResultGate                  # noqa: E402
from agent_framework.render_gate import motion_summary               # noqa: E402
from agent_framework.run_state import RunState                       # noqa: E402
from storyline_server.motion import code_render as cr                # noqa: E402
from storyline_server.motion import code_spec as cs                  # noqa: E402
from storyline_server.nodes import motion_plan as mp                 # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


# 真机 s10 的形状（缩写过，保留要紧的结构：没有 @dg/film、没有 CaptionLayer、
# 手画一行字幕摆在底部、颗粒与暗角两层盖在最上面）。
REAL_SHAPE = """import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { Grain, Vignette, FONT } from '@dg/polish';

const Shot: React.FC = () => {
  const frame = useCurrentFrame();
  const headIn = interpolate(frame, [0, 18], [0, 1]);
  return (
    <AbsoluteFill style={{ background: '#14121b' }}>
      <div style={{ position: 'absolute', bottom: 96, opacity: headIn }}>名表要几十万</div>
      <Vignette strength={0.45} />
      <Grain opacity={0.05} />
    </AbsoluteFill>
  );
};

export default Shot;
"""


def case_caption() -> None:
    print("① 字幕：要上字幕的镜系统补 <CaptionLayer/>")
    spec = {"aspect": "landscape", "caption_style": "outlined",
            "shots": [{"id": "s01", "code": REAL_SHAPE, "text": "一句"}]}
    out, notes = cs.autofix_caption_layer(spec)
    code = out["shots"][0]["code"]
    check(cs.caption_by_code(code), "补上了 <CaptionLayer />（老行为：14/14 镜一镜都没有）")
    check("import { CaptionLayer } from '@dg/film';" in code, "补了 import 行")
    check(code.index("<CaptionLayer />") > code.index("<Grain"),
          "插在颗粒/暗角之后：字幕在最上层，不被颗粒盖住")
    check(code.index("<CaptionLayer />") < code.rindex("</AbsoluteFill>"),
          "插在顶层 </AbsoluteFill> 之内")
    check(notes and "outlined" in notes[0] and "300px" in notes[0],
          f"说明点名了档位与字幕带高度：{notes[:1]}")
    again, n2 = cs.autofix_caption_layer(out)
    check(again["shots"][0]["code"] == code and not n2, "幂等：再跑一遍不重复补、不出说明")

    merged = REAL_SHAPE.replace("import { Grain",
                                "import { resolveFilmStyle } from '@dg/film';\nimport { Grain")
    out2, _ = cs.autofix_caption_layer({**spec, "shots": [{"id": "s01", "code": merged}]})
    c2 = out2["shots"][0]["code"]
    check("import { resolveFilmStyle, CaptionLayer } from '@dg/film'" in c2
          and c2.count("@dg/film") == 1,
          "已有 @dg/film 的 import 就并进那一行，不另起一行")

    for label, sp in (("整片 none", {**spec, "caption_style": "none"}),
                      ("镜级 none", {**spec, "shots": [{"id": "s01", "code": REAL_SHAPE,
                                                        "caption_style": "none"}]})):
        o, n = cs.autofix_caption_layer(sp)
        check(not cs.caption_by_code(o["shots"][0]["code"]) and not n,
              f"{label}：不补（那一档的意思就是不上字幕）")
    selfset = REAL_SHAPE.replace("<Grain opacity={0.05} />",
                                 "<Grain opacity={0.05} />\n      <CaptionText />")
    o, n = cs.autofix_caption_layer({**spec, "shots": [{"id": "s01", "code": selfset}]})
    check(not cs.caption_by_code(o["shots"][0]["code"]) and not n,
          "用 <CaptionText> 自己排字的镜不动（那也读注入，是正路）")
    # 2026-10-18：字幕改为「由这一镜的代码排进构图」之后，判「自己画了字幕」必须同时认
    # `<CaptionText/>`——只认整条 `<CaptionLayer/>` 会让每一镜多出一条母版底部字幕。
    check(cs.caption_any_by_code(selfset) and not cs.caption_by_code(selfset),
          "「自己排了字幕」认 <CaptionText/>：母版据此让位，不叠第二条")
    check(cs.caption_any_by_code("<CaptionLayer />")
          and not cs.caption_any_by_code("const s = '<CaptionTextish>';"),
          "两件字幕元件都算；同名前缀的字符串不算（判据是标签名，不是子串）")
    props_cap = cr.shot_input_props(
        {"id": "s01", "code": selfset, "text": "一句"},
        {"fps": 30, "aspect": "landscape", "width": 1920, "height": 1080},
        frames=150, frame_words=[])
    check(props_cap["captionFromCode"] is True,
          "注入 captionFromCode=True：自排字幕的镜，母版不再画底部那条")
    check("名表要几十万" not in code and notes and len(notes) == 2 and "名表要几十万" in notes[1],
          "它自己画在字幕带里（bottom: 96）的那行字删掉了、回执里点了名（否则叠成两行字幕）")
    nested = REAL_SHAPE.replace(
        "opacity: headIn }}>名表要几十万",
        "opacity: interpolate(frame, [0, 9], [0, 1], { extrapolateRight: 'clamp' }) }}>名表要几十万")
    o, _ = cs.autofix_caption_layer({**spec, "shots": [{"id": "s01", "code": nested}]})
    check("名表要几十万" not in o["shots"][0]["code"],
          "style 里套一层花括号（真机 s20 的写法）也认得")
    for label, swap in (("里面是 svg 图形", "<svg><circle r={4} /></svg>"),
                        ("里面是简笔画零件", "<Figure pose=\"stand\" />"),
                        ("离底边够远（bottom: 420）", None)):
        src = (REAL_SHAPE.replace("名表要几十万", swap) if swap else
               REAL_SHAPE.replace("bottom: 96", "bottom: 420"))
        o, _ = cs.autofix_caption_layer({**spec, "shots": [{"id": "s01", "code": src}]})
        kept = swap if swap else "名表要几十万"
        check(kept in o["shots"][0]["code"], f"{label}：不删（宁可漏删不许误删）")
    nofill = "export default () => <div>无顶层</div>;"
    o, n = cs.autofix_caption_layer({**spec, "shots": [{"id": "s01", "code": nofill}]})
    check(o["shots"][0]["code"] == nofill and n and "找不到顶层" in n[0],
          "找不到顶层 </AbsoluteFill>：不硬插，说明里点名让模型自己摆")


# 真机 s01 的形状（2026-10-19 那版）：按旧技能正文用 <CaptionFrame> 分带，又在它外面
# 套了一个定位盒子摆 <CaptionText/>。同一句字错开几像素，成片上就是「字幕重影」。
DOUBLE_CAPTION = """import React from 'react';
import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { CaptionFrame, CaptionText } from '@dg/film';

const Shot: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: '#14121b' }}>
      <CaptionFrame band={220}>
        <svg><circle cx={960} cy={400} r={120} fill="#ffb15c" /></svg>
      </CaptionFrame>
      <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0.035 * 100 }}>
        <CaptionText fitWidth={0.88} />
      </div>
    </AbsoluteFill>
  );
};

export default Shot;
"""


def case_duplicate_caption() -> None:
    print("①-b 字幕重影：<CaptionFrame> 自己画了字幕，又摆一份 <CaptionText/> → 只留一条")
    spec = {"aspect": "landscape", "caption_style": "on_picture",
            "shots": [{"id": "s01", "code": DOUBLE_CAPTION, "text": "一句"}]}
    out, notes = cs.autofix_duplicate_caption(spec)
    code = out["shots"][0]["code"]
    check(code.count("<CaptionText") == 0,
          "分带外那份 <CaptionText/> 连同它的定位盒子一起删掉了（老行为：两条字叠成重影）")
    check("<CaptionFrame" in code and "caption={false}" not in code,
          "能删就不动 <CaptionFrame>：分带照旧管画面区")
    check(notes and "重影" in notes[0] and "s01" in notes[0],
          f"回执点名是哪一镜、为什么：{notes[:1]}")
    again, n2 = cs.autofix_duplicate_caption(out)
    check(again["shots"][0]["code"] == code and not n2, "幂等：再跑一遍不重复删、不出说明")

    # 定位盒子里还有图形（不能整段删）→ 给 <CaptionFrame> 补 caption={false}，
    # 字只留模型自己摆的那一份。
    framed = DOUBLE_CAPTION.replace(
        "<div style={{ position: 'absolute', left: 0, right: 0, bottom: 0.035 * 100 }}>\n"
        "        <CaptionText fitWidth={0.88} />\n"
        "      </div>",
        "<div style={{ position: 'absolute', left: 0, right: 0, bottom: 0.035 * 100 }}>\n"
        "        <svg><rect width={40} height={40} /></svg>\n"
        "        <CaptionText fitWidth={0.88} />\n"
        "      </div>")
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s01", "code": framed}]})
    check("caption={false}" in o["shots"][0]["code"] and n and "caption={false}" in n[0],
          "删不掉时给 <CaptionFrame> 补 caption={false}（不硬删图形）")
    o2, n2 = cs.autofix_duplicate_caption(o)
    check(o2["shots"][0]["code"] == o["shots"][0]["code"] and not n2,
          "补过 caption 的不再补第二个（幂等）")

    # 带 text= 的 <CaptionText> 是图注/招牌，不是重复字幕。
    label = DOUBLE_CAPTION.replace("<CaptionText fitWidth={0.88} />",
                                   "<CaptionText text=\"图注：1960s\" />")
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s01", "code": label}]})
    check(o["shots"][0]["code"] == label and not n,
          "带 text= 的 <CaptionText> 是另一行字：不删")

    # 只有 <CaptionFrame>、没有第二份字幕：不动。
    only_frame = DOUBLE_CAPTION.split("      <div style=")[0] + \
        "    </AbsoluteFill>\n  );\n};\n\nexport default Shot;\n"
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s01", "code": only_frame}]})
    check(o["shots"][0]["code"] == only_frame and not n, "只有 <CaptionFrame>：不动")

    # 模型已经明说「我自己排字」（caption={false}）：不再塞第二个 caption，也不删它摆的那条。
    explicit = DOUBLE_CAPTION.replace("<CaptionFrame band={220}>",
                                      "<CaptionFrame band={220} caption={false}>")
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s01", "code": explicit}]})
    check(o["shots"][0]["code"] == explicit and not n,
          "已显式写 caption={false} 的：不覆盖（那是它的选择）")

    # 没有 code 的镜原样穿过。
    o, _ = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s01", "text": "还没写"}]})
    check(o["shots"][0] == {"id": "s01", "text": "还没写"}, "没有 code 的镜原样穿过")


TEMPLATE_CAPTION = """import React from 'react';
import { AbsoluteFill } from 'remotion';
import { CaptionText } from '@dg/film';
import { StyleCaption } from '@dg/lib/style/generated/u-1-style';

const Shot: React.FC = () => (
  <AbsoluteFill style={{ background: '#14121b' }}>
    <StyleCaption text="领一块面包，坐进看台，看一整天" position="top-left" />
    <div style={{ position: 'absolute', left: 0.06 * 1920, top: 0.10 * 1080 }}>
      <CaptionText fitWidth={0.62 * 1920} emphasis="color" />
    </div>
  </AbsoluteFill>
);

export default Shot;
"""


def case_template_caption_duplicate() -> None:
    print("①-c 模板字幕 + <CaptionText/>：两件都画同一句 → 只留模板那条")
    style_pack = {"template": {"slots": [
        {"component": "StyleCaption", "text_kind": "caption", "content_prop": "text"},
    ]}}
    spec = {"aspect": "landscape", "caption_style": "on_picture",
            "style_pack": style_pack,
            "shots": [{"id": "s03", "code": TEMPLATE_CAPTION,
                       "text": "领一块面包，坐进看台，看一整天"}]}
    out, notes = cs.autofix_duplicate_caption(spec)
    code = out["shots"][0]["code"]
    check("<StyleCaption" in code, "模板的主字幕组件留着（它是风格契约）")
    check("<CaptionText" not in code, "重复的 <CaptionText/> 删掉了（老行为：左上角两行字重叠）")
    check(notes and "模板的主字幕" in notes[0] and "s03" in notes[0],
          f"回执点名是哪一镜、为什么：{notes[:1]}")
    again, n2 = cs.autofix_duplicate_caption(out)
    check(again["shots"][0]["code"] == code and not n2, "幂等：再跑一遍不重复删")
    # 带 text= 的那份是图注，不是重复字幕：不删。
    label = TEMPLATE_CAPTION.replace(
        '<CaptionText fitWidth={0.62 * 1920} emphasis="color" />',
        '<CaptionText text="图注：公元 1 世纪" />')
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s03", "code": label}]})
    check(o["shots"][0]["code"] == label and not n,
          "带 text= 的 <CaptionText> 是另一行字：不删")
    # 没有 style_pack（自由画）时不动：<CaptionText/> 就是唯一一条。
    o, n = cs.autofix_duplicate_caption(
        {**spec, "style_pack": {}, "shots": [{"id": "s03", "code": TEMPLATE_CAPTION}]})
    check(o["shots"][0]["code"] == TEMPLATE_CAPTION and not n,
          "没有模板字幕槽位时：<CaptionText/> 不删")

    # 2026-10-23 真机：白名单把槽位的 `text_kind` 丢了，于是上面两条判据在**校验之后**
    # 全部失灵——10 镜里 8 镜被补了第二条字幕，母版那条也没让位。这一条守的是白名单。
    packed, packed_err = cs.validate_code_spec({
        "aspect": "landscape", "caption_style": "outlined",
        "style_pack": {"name": "style_x", "template": {
            "module": "@dg/lib/style/generated/u-1-style",
            "slots": [{"component": "StyleCaption", "text_kind": "caption",
                       "content_prop": "text"}]}},
        "shots": [{"id": "s03", "text": "一句", "code": TEMPLATE_CAPTION}],
    })
    check(packed_err == [], f"带模板槽位的 spec 合规（实际 {packed_err}）")
    kept = [s.get("text_kind") for s in cs.style_template_slots(packed)]

    # ①-c′ 模板字幕 + `<CaptionLayer/>`。2026-10-22 真机：槽位缺 `text_kind`，
    # 兜底补进来的整条字幕与模板字幕同屏，10 镜里 6 镜两行字。
    layered = """import React from 'react';
import { AbsoluteFill } from 'remotion';
import { CaptionLayer } from '@dg/film';
import { StyleCaption } from '@dg/lib/style/generated/u-1-style';

const Shot: React.FC = () => (
  <AbsoluteFill>
    <StyleCaption text="茶凉之前，没有人起身" />
    <CaptionLayer />
  </AbsoluteFill>
);

export default Shot;
"""
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s05", "code": layered}]})
    code2 = o["shots"][0]["code"]
    check("<StyleCaption" in code2 and "<CaptionLayer" not in code2,
          "模板字幕 + <CaptionLayer/>：只留模板那条")
    check(n and "s05" in n[0] and "CaptionLayer" in n[0],
          f"回执点名是哪一镜、删了哪件：{n[:1]}")
    again, n2 = cs.autofix_duplicate_caption(o)
    check(again["shots"][0]["code"] == code2 and not n2, "幂等：再跑一遍不重复删")
    # 带子元素/跨多行的 <CaptionLayer> 不碰：宁可漏删，不许把画面里的东西一起删掉。
    rich = layered.replace(
        "    <CaptionLayer />\n",
        "    <CaptionLayer style={{ bottom: 40 }}>\n      <span>另一行</span>\n    </CaptionLayer>\n")
    o, n = cs.autofix_duplicate_caption({**spec, "shots": [{"id": "s05", "code": rich}]})
    check(o["shots"][0]["code"] == rich and not n,
          "带子元素的 <CaptionLayer> 不删（只认独占一行的自闭合写法）")


def case_duration() -> None:
    print("② 时长：拉长时每镜最多拉到原长的 1.25 倍")
    shots = [{"id": f"s{i:02d}", "text": "短句", "code": "x", "min_duration_sec": 8.0}
             for i in range(1, 15)]
    spec = {"narration": False, "shots": shots}
    out, notes = cs.fit_duration(spec, 210.0)
    secs = [s["min_duration_sec"] for s in out["shots"]]
    check(max(secs) <= 8.0 * cs.MAX_STRETCH_RATIO + 1e-6,
          f"14 镜 × 8s 凑 210s：每镜 ≤ {8 * cs.MAX_STRETCH_RATIO:g}s（实得最大 {max(secs)}；"
          "老行为一律拉到 15s，后半截定格）")
    check(notes and "加镜" in notes[0] and "约 23 镜" in notes[0],
          f"凑不够时明说「要更长就加镜」并给镜数：{notes[:1]}")
    short, _ = cs.fit_duration({"narration": False, "shots": [dict(s, min_duration_sec=20.0)
                                                              for s in shots]}, 210.0)
    check(sum(s["min_duration_sec"] for s in short["shots"]) < 280,
          "压短不受这一条限制（压短只会让动画更紧）")


def case_family() -> None:
    print("③ 连贯：版式族配额**已整道删除**（2026-10-24）")
    # 闸⑮ 与 layout_family_cap 一起删了：它判的是「版式别扎堆」，属于审美，
    # 交给静默视觉评审。这里只守「钩子真的走了」。
    check(not hasattr(cs, "check_layout_family") and not hasattr(cs, "layout_family_cap"),
          "闸⑮ 与它的配额函数一起删了——留着会让人以为它还在判")


def case_replace() -> None:
    print("④ 整份替换：少了哪几镜、断了哪几号要说出来")
    prev = {"shots": [{"id": f"s{i:02d}"} for i in range(1, 7)]}
    now = {"shots": [{"id": f"s{i:02d}"} for i in range(7, 21)]}
    notes = mp.shot_diff_notes(prev, now)
    check(any("整份替换" in n and "6 镜 → 本次 14 镜" in n and "s01" in n and "s06" in n
              for n in notes), f"真机那个形状（s01–s06 → s07–s20）：点名少了 6 镜 {notes[:1]}")
    check(any("镜号不连续" in n and "s01" in n for n in notes), "不从 s01 起：点名断号")
    check(mp.shot_diff_notes(None, {"shots": [{"id": "s01"}, {"id": "s02"}]}) == [],
          "第一次交、镜号连续：一声不响")
    check(mp.shot_diff_notes(now, {"shots": now["shots"] + [{"id": "s21"}]})[0].startswith("镜号"),
          "只增不减：不报「整份替换」（增镜是正常操作）")


def case_style() -> None:
    print("⑤ 风格：一句话活着穿过校验、出现在确认卡上")
    raw = {"aspect": "landscape", "narration": False,
           "style": "杂志编辑风：左对齐网格、眉题+大标题、细栏线",
           "shots": [{"id": "s01", "text": "一句", "code": "x", "min_duration_sec": 3}]}
    spec, _ = cs.validate_code_spec(raw)
    check(spec.get("style") == raw["style"], "style 没被校验器静默删掉")
    spec2, _ = cs.validate_code_spec({**raw, "style": "长" * 400})
    check(len(spec2.get("style") or "") == cs.STYLE_MAX_CHARS, "超长截断、不报错")
    spec3, _ = cs.validate_code_spec({k: v for k, v in raw.items() if k != "style"})
    check("style" not in spec3, "没写就没有这一栏（不塞空串）")
    check("style" in cs.spec_schema()["properties"], "schema 里有这一栏，模型看得见")
    card = motion_summary({"shot_count": 3, "style": raw["style"]})
    check(card.get("风格", "").startswith("杂志编辑风"), f"确认卡上有「风格」一行：{card.get('风格')}")


def case_motion_wording() -> None:
    print("⑥ 闸⑭ 措辞按档；预览版面读数只留可执行的几类")
    motion = {"status": "rejected", "reason": "全片只数到 0 个先后变化段", "events": [],
              "thresholds": {"min_events": 2, "min_gap_sec": 0.4}}
    strict = cr.describe_motion("s07", motion, strict=True)[0]
    preview = cr.describe_motion("s07", motion, strict=False)[0]
    check("没有进成片" in strict, "严格档：照旧说退回、没进成片")
    check("没有进成片" not in preview and "仍在成片里" in preview,
          "预览档：说实话——仍在成片里（老行为一律写「没有进成片」）")
    band = {"w": 1920, "h": 1080}
    # 2026-10-20 起：`overlap_subject`（字压图）与 `dangling`（引线落空）都不再驱动
    # 预览返工——静帧上误报多于真病，整镜那一趟照旧会报。预览只留字压字与出画。
    lay = {"s01": {"status": "scanned", "thresholds": {}, "band": band,
                   "findings": [{"kind": "dangling", "texts": ["引线"]},
                                {"kind": "overlap_subject", "texts": ["标题"], "ratio": 0.5,
                                 "box": {"x": 100, "y": 800, "w": 200, "h": 60}},
                                {"kind": "overlap", "texts": ["标题", "副标题"], "ratio": 0.5}]},
           "s02": {"status": "missing", "findings": []}}
    got = cr.preview_layout_findings(lay)
    check(len(got) == 1 and "s01" in got[0] and "压在一起" in got[0],
          f"只留字压字这一条；引线落空、字压图与没扫到的镜都不进预览提醒：{got}")
    # 真机静帧那一路的读数原样（盒子在 y≈-1e6）：整镜作废，一条假警报都不发
    broken = {"s07": {"status": "scanned", "thresholds": {}, "band": band,
                      "findings": [{"kind": "clipped", "texts": ["白皮肤"], "side": "top",
                                    "px": 999720, "box": {"x": 100, "y": -999799, "w": 40, "h": 232}}]}}
    check(cr.preview_layout_findings(broken) == [],
          "坐标落在画面外一百万像素的读数（静帧那一路的实测）：整镜不报")


def case_cost() -> None:
    print("⑦ 花费：分命中/未命中计价、封顶、续跑延一份")
    night = datetime(2026, 10, 17, 14, 30, tzinfo=timezone.utc)      # 北京 22:30 周六 → 空闲
    peak = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)          # 北京 10:00 周五 → 高峰
    u = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
         "cache_hit_tokens": 900_000, "cache_miss_tokens": 100_000}
    check(abs(cost.cost_yuan(u, model="deepseek-flash", when=night) - 0.118) < 1e-9,
          "空闲时段：90 万命中 + 10 万未命中 = 0.118 元")
    check(abs(cost.cost_yuan(u, model="deepseek-flash", when=peak) - 0.236) < 1e-9,
          "高峰时段翻倍")
    bare = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    check(abs(cost.cost_yuan(bare, model="deepseek-flash", when=night) - 1.0) < 1e-9,
          "供应商没回缓存数：整段按未命中算（宁可算重）")
    check(cost.price_of("deepseek-v4-pro")["miss"] == 4.5, "pro 走 pro 的价")

    ds = SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105,
                         prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20)
    oa = SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105,
                         prompt_tokens_details=SimpleNamespace(cached_tokens=60))
    none = SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105)
    check(cost.usage_from_raw(ds)["cache_hit_tokens"] == 80, "认 DeepSeek 的写法")
    check(cost.usage_from_raw(oa)["cache_miss_tokens"] == 40, "认 OpenAI 的写法")
    check("cache_hit_tokens" not in cost.usage_from_raw(none), "都没有就不写（不编 0）")

    st = RunState()
    agent_mod._open_budget(st, resuming=False)
    check(st.budget_yuan == cost.run_budget_yuan() == 3.0, "开账：每条 3 元")
    agent_mod._charge(st, {"prompt_tokens": 3_100_000, "completion_tokens": 0,
                           "model": "deepseek-flash"})
    check(agent_mod._over_budget(st), "花到 3 元以上就撞线")
    agent_mod._open_budget(st, resuming=True)
    check(not agent_mod._over_budget(st) and st.budget_yuan > 6.0,
          "撞过线之后续跑：再给一份（用户看过账、点了继续）")
    st2 = RunState(spent_yuan=1.0, budget_yuan=3.0)
    agent_mod._open_budget(st2, resuming=True)
    check(st2.budget_yuan == 3.0, "没撞过线的续跑（渲染确认门之后）：不延")
    back = RunState.from_json(st.to_json())
    check(back.spent_yuan == st.spent_yuan and back.budget_yuan == st.budget_yuan,
          "落盘往返不丢账（续跑不归零）")


def case_fold() -> None:
    print("⑧ 缓存：历史折叠有回差，不再每轮回头改写")
    gate = ToolResultGate(fold_at_bytes=100_000, keep_recent=2)
    msgs = [{"role": "tool", "name": "fetch_url", "tool_call_id": f"c{i}",
             "content": "x" * 20_000} for i in range(6)]          # 120 KB，越线
    gate.shrink(msgs)
    total = sum(len(m["content"]) for m in msgs)
    check(total <= gate.fold_to_bytes + 40_000,
          f"一次折到回差下沿附近（{total} 字节，下沿 {gate.fold_to_bytes}）")
    snapshot = [m["content"] for m in msgs]
    msgs.append({"role": "tool", "name": "fetch_url", "tool_call_id": "c6",
                 "content": "y" * 20_000})
    gate.shrink(msgs)
    check([m["content"] for m in msgs[:6]] == snapshot,
          "再来一条 20 KB：前面的历史一个字都没改（老行为：越线后每来一条就回头折一条）")
    # 2026-10-18 真机 run 66c0b56541ca：技能正文先读、42 份网页后到，老顺序（从旧到新）
    # 先折的是正在用的技能正文，模型把 1200 行正文从头读了三遍。
    from agent_framework.compress import FOLD_MARKER

    def _mixed(skill_kb: int, pages: int) -> list:
        return ([{"role": "tool", "name": "load_skill", "tool_call_id": "k0",
                  "content": "[skill:x file=SKILL.md]\n" + "s" * (skill_kb * 1000)}]
                + [{"role": "tool", "name": "fetch_url", "tool_call_id": f"w{i}",
                    "content": "w" * 20_000} for i in range(pages)])
    msgs = _mixed(20, 6)                                    # 140 KB，网页折完就够
    ToolResultGate(fold_at_bytes=100_000, keep_recent=1).shrink(msgs)
    check(FOLD_MARKER not in msgs[0]["content"]
          and all(FOLD_MARKER in m["content"] for m in msgs[1:6]),
          "越线时先折网页、技能正文留着（哪怕它更早）")
    msgs = _mixed(80, 2)                                    # 120 KB，网页折完仍超线
    ToolResultGate(fold_at_bytes=100_000, keep_recent=1).shrink(msgs)
    check(FOLD_MARKER in msgs[0]["content"],
          "网页折完仍放不下，才轮到技能正文（不会因为顺序而永远超线）")
    check(ToolResultGate().fold_at_bytes == 1_000_000,
          "缺省门槛 1 MB（≈37 万 token，按实测 2.7 字节/token；模型窗口 1M，留出写代码的余量）")


def _round(i: int, calls: list[tuple[str, str, str]]) -> list[dict]:
    """一轮：assistant(tool_calls) + 各条 tool 回执。calls = [(工具名, 参数, 结果)]。"""
    tcs = [{"id": f"c{i}_{k}", "type": "function",
            "function": {"name": n, "arguments": a}} for k, (n, a, _) in enumerate(calls)]
    return ([{"role": "assistant", "content": "", "tool_calls": tcs}]
            + [{"role": "tool", "tool_call_id": f"c{i}_{k}", "name": n, "content": r}
               for k, (n, _, r) in enumerate(calls)])


def case_stall() -> None:
    print("⑨ 原地打转：重复的失败连发就停，改着往前走的不停；报错指路")
    bad = ("load_skill", '{"path": "demos/typography/type-rhythm-sync/WordSwapOnBeat.tsx"}',
           "Error: 无附件")
    msgs = [{"role": "system", "content": "s"}]
    for i in range(agent_mod._STALL_ROUNDS):
        msgs += _round(i, [bad])
    st = RunState(budget_yuan=3.0)
    check(agent_mod._stop_reason(st, msgs) == "",
          f"同一个失败调用：第一次不算重复，连发 {agent_mod._STALL_ROUNDS} 轮还差一轮——不停")
    msgs += _round(99, [bad])
    check(agent_mod._stop_reason(st, msgs) == "stalled",
          "再重复一轮就停（真机那条连发了 150 多次）")
    check(st.stall_floor == len(msgs), "停下时记住了位置")
    check(agent_mod._stop_reason(st, msgs) == "",
          "点「继续」之后只看新轮次：末尾那几轮失败不会让它一上来就又停")

    fixing = [{"role": "system", "content": "s"}]
    for i in range(12):
        fixing += _round(i, [("plan_motion", f'{{"try": {i}}}', f"Error: 闸① 第 {i} 处")])
    check(agent_mod._stop_reason(RunState(budget_yuan=3.0), fixing) == "",
          "照着打回清单反复改分镜（每次参数都不同）：12 轮失败也不停——那是在推进")

    mixed = [{"role": "system", "content": "s"}]
    for i in range(10):
        mixed += _round(i, [bad, ("fetch_url", f'{{"url": "u{i}"}}', "正文……")])
    check(agent_mod._stop_reason(RunState(budget_yuan=3.0), mixed) == "",
          "一轮里还有成功的调用：不算打转")

    # 真机 run 7054bfa3f503：同一段代码原样写进 s16、s17……s60，每次都「成功」
    dup = [{"role": "system", "content": "s"}]
    same = json.dumps({"content": "const Shot = () => null;"})
    for i in range(16, 16 + agent_mod._STALL_ROUNDS + 1):
        args = json.dumps({"path": f"shots/s{i}.tsx", "content": "const Shot = () => null;"})
        dup += _round(i, [("write_file", args, f"已创建文件 shots/s{i}.tsx")])
    st = RunState(budget_yuan=3.0)
    check(agent_mod._stop_reason(st, dup) == "stalled"
          and "同一段内容" in agent_mod._last_failed_call(dup),
          "同一段内容反复写进不同文件（每次都成功）：也算打转、停下并点名")
    fresh = [{"role": "system", "content": "s"}]
    for i in range(12):
        args = json.dumps({"path": f"shots/s{i}.tsx", "content": f"const S{i} = () => null;"})
        fresh += _round(i, [("write_file", args, f"已创建文件 shots/s{i}.tsx")])
    check(agent_mod._stop_reason(RunState(budget_yuan=3.0), fresh) == "",
          "每镜写的内容都不同：12 轮写文件也不停（那是在干活）")
    polls = [{"role": "system", "content": "s"}]
    for i in range(12):
        polls += _round(i, [("render_status", '{"artifact_id": "_default"}', "running 40%")])
    check(agent_mod._stop_reason(RunState(budget_yuan=3.0), polls) == "",
          "轮询渲染进度（同参、成功）：不算打转")

    # 真机 run 02cbcfb2440e：同一段光带一轮轮叠进同一个文件，old_string 每轮变长、
    # 参数逐字不同，老判据（按整串参数比）永远认不出重复。
    block = ('  {/* 火光扫过整条岩壁 */}\n'
             '  <path d={`M${width * 0.22},${height * 0.30} Z`} fill={accent} />\n')
    grow = [{"role": "system", "content": "s"}]
    cur = block
    for i in range(agent_mod._STALL_ROUNDS + 1):
        nxt = cur + block
        args = json.dumps({"path": "shots/s01.tsx", "old_string": cur, "new_string": nxt})
        grow += _round(100 + i, [("edit_file", args, "已替换 1 处（shots/s01.tsx）")])
        cur = nxt
    check(agent_mod._stop_reason(RunState(budget_yuan=3.0), grow) == "stalled"
          and "反复叠进同一个文件" in agent_mod._last_failed_call(grow),
          "同一段内容反复叠进同一个文件（参数每轮更长）：也算打转、停下并点名")
    legit = [{"role": "system", "content": "s"}]
    body = ""
    for i in range(12):
        nxt = body + f"  const v{i} = {i};\n"
        args = json.dumps({"path": "shots/s01.tsx", "old_string": body, "new_string": nxt})
        legit += _round(120 + i, [("edit_file", args, "已替换 1 处（shots/s01.tsx）")])
        body = nxt
    check(agent_mod._stop_reason(RunState(budget_yuan=3.0), legit) == "",
          "每次追加的内容都不同：12 轮编辑也不停（那是在推进）")
    del same

    from agent_framework.skill import _nearest_attachments
    names = ["demos/typography/type-rhythm-sync/FontWeightPump.tsx",
             "demos/typography/type-rhythm-sync/KaraokeFillSync.tsx",
             "demos/typography/blur-slide/BlurSlide.tsx",
             "demos/camera/basic-3d-scene/Basic3DScene.tsx"]
    near = _nearest_attachments(names, "demos/typography/type-rhythm-sync/WordSwapOnBeat.tsx")
    check(near == names[:2], f"编出来的文件名：报同目录里真有的那两份 {near}")
    near = _nearest_attachments(names, "typography/no-such-card/X.tsx")
    check(near == ["demos/typography/blur-slide/", "demos/typography/type-rhythm-sync/"],
          f"连卡名都是编的：报同类别下真有的卡（不报 camera 那些不相干的）{near}")


def case_style_review() -> None:
    print("⑩ 风格对照：带图模型逐镜看预览，不像的点名带改法；花费记进账")
    import asyncio
    import tempfile
    from storyline_server.motion import selfcheck

    replies = {
        "s01": '{"fits": false, "why": "霓虹灯管大字", "fix": "换成衬线大标题加细栏线"}',
        "s02": '好的 {"fits": true, "why": "眉题加大标题", "fix": ""}',
        "s03": "我觉得还行",                                    # 不是那一行 JSON → 未判
    }

    class FakeLLM:
        async def complete(self, messages, tools=None):
            ask = messages[1]["content"][0]["text"]
            sid = next(k for k in replies if f"「{k}文案」" in ask)
            img = messages[1]["content"][1]["image_url"]["url"]
            assert img.startswith("data:image/jpeg;base64,")
            return SimpleNamespace(content=replies[sid],
                                   usage={"prompt_tokens": 2000, "completion_tokens": 50,
                                          "model": "deepseek-flash"})

    with tempfile.TemporaryDirectory() as tmp:
        stills = {}
        for sid in replies:
            p = Path(tmp) / f"{sid}.jpg"
            p.write_bytes(b"\xff\xd8fakejpeg")
            stills[sid] = p
        shots = [{"id": sid, "text": f"{sid}文案"} for sid in replies]
        got = asyncio.run(selfcheck.style_review(style="杂志编辑风", shots=shots,
                                                  stills=stills, llm=FakeLLM()))
    check(got["misfits"] == ["s01：霓虹灯管大字——改法：换成衬线大标题加细栏线"],
          f"不像的镜点名并带改法：{got['misfits']}")
    check(got["unjudged"] == ["s03"] and got["checked"] == 2,
          "回话不是那一行 JSON：记未判，不当成符合")
    check(got["cost_yuan"] > 0, f"花费算出来了：¥{got['cost_yuan']}")
    empty = asyncio.run(selfcheck.style_review(style="", shots=shots, stills={}, llm=FakeLLM()))
    check(empty["checked"] == 0 and not empty["misfits"], "没有 style 就不看（不白花钱）")

    st = RunState(budget_yuan=3.0)
    old = [{"role": "tool", "content": '{"node_cost_yuan": 5.0}'}]     # 上一条 run 的旧回执
    agent_mod._open_budget(RunState(), resuming=False, messages=old)
    fresh = RunState()
    agent_mod._open_budget(fresh, resuming=False, messages=old)
    msgs = old + [{"role": "tool", "content": '{"stored": true, "node_cost_yuan": 0.12}'}]
    agent_mod._charge_nodes(fresh, msgs)
    check(abs(fresh.spent_yuan - 0.12) < 1e-9, "只记这条 run 的节点花费，旧对话里那笔不重复记")
    agent_mod._charge_nodes(fresh, msgs)
    check(abs(fresh.spent_yuan - 0.12) < 1e-9, "同一条回执只记一次")
    back = RunState.from_json(fresh.to_json())
    check(back.node_scan == fresh.node_scan, "扫描位置跨挂起保留（续跑不重复记）")
    del st


def case_no_demo() -> None:
    print("⑪ 不用 demo：附件不登记、卡名与派生栏忽略、三道 demo 判据不跑")
    from agent_framework.skill import _excluded_prefixes
    check(_excluded_prefixes({"exclude": "demos/, shots/"}) == ("demos/", "shots/"),
          "frontmatter 的 exclude 读成前缀")
    check(_excluded_prefixes({}) == (), "没写就不排除（别的技能不受影响）")
    check(cs.DEMOS_ENABLED is False, "镜头代码不再从 demo 来（用户 2026-10-18 的决定）")
    names = {g.__name__ for g in cs.WHOLE_FILM_GATES}
    check(not names & {"check_derived_similarity", "check_original_share", "check_layout_family"},
          f"⑫ ⑳ ⑮ 不在整片判据里（剩 {len(names)} 道）")
    errs: list[str] = []
    card, derived = cs._norm_cite({"card": "no/such-card", "derived_from": ["x/Y.tsx"]}, "s01", errs)
    check(card == "" and derived == [] and not errs,
          "模型凭印象写了卡名和 demo 路径：忽略，不打回整份分镜")
    skill = (Path(__file__).resolve().parents[1] / "examples" / "retired" / "motion_explainer"
             / "SKILL.md").read_text(encoding="utf-8")
    check("exclude: demos/, shots/" in skill and "禁止从零写" not in skill,
          "技能正文：排除 demo 目录、不再要求照抄")


def main() -> int:
    for case in (case_caption, case_duplicate_caption, case_template_caption_duplicate,
                 case_duration, case_family,
                 case_replace, case_style,
                 case_motion_wording, case_cost, case_fold, case_stall, case_style_review,
                 case_no_demo):
        case()
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
