# -*- coding: utf-8 -*-
"""闸⑮（版式族配额）与闸⑯（主体具名）——R5-T15。

这两道闸判的都是**整片层**的事，而且都在 `plan_motion` 就硬拦（不烧像素，改十遍只花几秒）：

  ⑮  卡名种类 ≥ ceil(镜数/4)、同一**族**（卡库类别目录名）≤ 2 镜
      —— 「一条片子不许同一个版式反复摆」那条要求的机器形态。
  ⑯  每镜 `subject` 具名（2~24 字、不在抽象词拒绝名单里）、`subject_channel` 与**代码**
      对账（有图形主体的镜必填且要有证据，纯文字镜不许填）
      —— 「说得出画的是什么」+ 「说的是真的」。通道那一半刻意不来自模型自报，
      判据仍是 `shot_all_channels` 从代码字节里读——⑦ 早就立过这条：自报会漂。

**D-红先**：每条判据都先拿一份**该被拒的样本**证红（并打印它被拒的原话），再拿该放的样本证绿。
⑮ 的两条判据各证一次红不是啰嗦——「八张**不同**的卡全挤在同一个族里」正是「只看卡名种类」会
放过的那份样本，第 2 条判据不单独证红就等着哪天被人当成多余的那半删掉。

本文件**不判**的两件事（跳过 ≠ 通过，逐条列在这里）：
- 「画出来的那个东西像不像 `subject` 说的那个」：机器判不了，归 ⑲-b（盲描述）。
- 真机渲染：这里全是离线直调（两个 checker / `validate_code_spec` / `plan_motion.plan_payload`），
  一帧都不烧；像素那一侧的证据在 test_code_gates 与真机取证的账上。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 两个布局都要认：宿主是 <仓库根>/backend/tests/…，镜像里是 /app/tests/…
_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[1], _HERE.parents[2] / "backend"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def skill_text() -> str:
    """技能正文（模型读的那一份）。模型读正文、机器判常量，两边漂一次 =
    「闸拦了人却不知道为什么」，所以下面每一条常量都在正文里逐字钉一遍。"""
    rel = Path("examples") / "skills" / "motion_explainer" / "SKILL.md"
    for cand in (_HERE.parents[1] / rel, Path("/app") / rel):
        if cand.is_file():
            return cand.read_text("utf-8")
    raise FileNotFoundError(f"技能正文没找到（试过 /app 与 backend 两处）：{rel}")


# 每条通道一段真代码。闸⑦/⑯ 认的都是**代码字节**，所以夹具必须是真代码而不是自报字段。
# P1（2026-10-17）注入契约：每支都加注入头（`getInputProps` 出现 + 用 `text` +
# 摆 `<CaptionLayer/>`），夹具先过契约红的是判据而不是夹具。加完之后骨架越过
# `_SHAPE_MIN_TOKENS`(40) 的豁免线，闸⑫ 会开始比相似度——本文件对它没有空账断言
# （见 `test_code_gates._shot` 那段同族说明）。
CODE = {
    "rough": ('import { roughCanvas } from "roughjs";\n'
              'export const Shot = () => (<FilmLayout>{roughCanvas()}'
              '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
    "vector": ('export const Shot = () => (<FilmLayout><svg />'
               '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
    "three": ('import { Canvas } from "@react-three/fiber";\n'
              'export const Shot = () => (<FilmLayout><Canvas />'
              '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
    "katex": ('import katex from "katex";\n'
              'export const Shot = () => (<FilmLayout>{katex.renderToString("x")}'
              '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
    # 第五条通道（2026-10-18）：真拿了 2D 上下文才算。只写一个空 <canvas> 标签不算
    # （判据见 code_spec._CANVAS_RE）。
    "canvas": ('import { useEffect, useRef } from "react";\n'
               'export const Shot = () => { const ref = useRef(null);\n'
               '  useEffect(() => { const ctx = ref.current.getContext("2d");\n'
               '    ctx.fillRect(0, 0, 10, 10); });\n'
               '  return (<FilmLayout><canvas ref={ref} />{getInputProps().text}'
               '<CaptionLayer /></FilmLayout>); };'),
    "text": ('export const Shot = () => (<FilmLayout>字'
             '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
}

#: 卡库里的真卡（族 = 目录名）。夹具用不存在的卡会在 `_norm_cite` 那一道先红，
#: 那样红的是夹具不是判据——所以 §0 先把「这些卡真的在卡库里」钉住。
FAMILY_CARDS = {
    "opening": ("brand-ink-open", "crane-rise-reveal"),
    "data": ("chart-live-moves", "timeline-travel"),
    "effects": ("assemble-then-type-flyin", "aurora-bloom-bg-flip"),
    "typography": ("blur-slide", "paper-title-card"),
    "camera": ("basic-3d-scene", "crash-zoom-punch"),
    "ui-entrance": ("avatar-bracket-carousel", "card-stack"),
    "interaction": ("autolayout-gap-dial", "canvas-materialize-moves"),
    "rhythm": ("beat-cut-moves", "panel-grid-moves"),
}
#: 卡库里 typography 一族的**另外**几张真卡：第 2 条判据的反例要用「同族、卡名各不相同」。
TYPO_EXTRA = ("brace-expand", "countdown-arc-scatter", "document-typewriter-reveal",
              "flying-words", "glitch-cycle", "gradient-word-sweep")

_DEMO = "ui-entrance/list-stack-press/ListStackPress.tsx"

#: 「这一格不填」的哨兵：`declare=None` 是**整键不出现**（schema 里 subject_channel 是可选的），
#: `declare=""` 是「填了个空串」。两种样本都要试——模型少写一个键与写一个空串在 JSON 上是
#: 两件事，判据必须都拦得住（前者靠 `check_subject_named`，后者靠 schema 的 enum 那一道）。
_AUTO = object()


def card(family: str, k: int = 0) -> str:
    return f"{family}/{FAMILY_CARDS[family][k]}"


def _shot(i: int, *, ch: str = "vector", card_id: str | None = None,
          subject: str = "活字盘", declare: object = _AUTO, **over) -> dict:
    """一镜夹具。`declare` 三态：`_AUTO` 按通道自动填（纯文字镜不填）、`None` 整键不写、
    字符串则原样写进去（用来造「自报与代码不符」「不在白名单」那两种样本）。"""
    s: dict = {"id": f"s{i:02d}", "code": CODE[ch], "text": "胶泥刻字排成一行。",
               "min_duration_sec": 4.0, "subject": subject,
               "card": card_id if card_id is not None else card("typography", i % 2),
               "derived_from": [_DEMO]}
    if declare is _AUTO:
        if ch != "text":
            s["subject_channel"] = ch
    elif declare is not None:
        s["subject_channel"] = declare
    s.update(over)
    return s


def _film(shots: list[dict], **over) -> dict:
    raw = {"title": "夹具片", "aspect": "portrait", "fps": 30, "narration": False,
           "caption_style": "on_picture", "texture": "none", "shots": shots}
    raw.update(over)
    return raw


def _mixed6(**over) -> list[dict]:
    """6 镜：三条通道各两镜、六个族各一镜——⑮ 与 ⑦ 在最低档都放行的那一份。"""
    chans = ["rough", "vector", "katex", "rough", "vector", "katex"]
    fams = ["opening", "data", "effects", "typography", "camera", "ui-entrance"]
    # 闸⑲ 的两条硬判据（色板种数 ≥ceil(n/4)、满幅镜数 ≥ceil(n/5)）也要在这支夹具上成立，
    # 否则「过闸的六镜片」这件事在这里断掉。四套色板轮着绑（4 ≥ 2），s02/s05 满幅并给底色。
    pals = ("warm-cream", "deep-ocean", "obsidian-violet", "solar-pop")
    rows = []
    for i, (c, f) in enumerate(zip(chans, fams)):
        code = f'const palette = palettes["{pals[i % len(pals)]}"];\n' + CODE[c]
        row = _shot(i + 1, ch=c, card_id=card(f), code=code)
        if i in (1, 4):          # 满幅与底色成对写：闸⑲ 一条读 bleed、一条读 bg
            row["bleed"] = True
            row["bg"] = "#0b1620"
        rows.append(row)
    return rows


def _only(errors: list[str], needle: str) -> bool:
    """清单里**有且只有一条**、且那条是 needle 这一道出的——用来证明「红的是它，不是别人」。

    单独 `any(...)` 会被「另有别的错也带同样字样」蒙过去；这里要的是这一个样本只犯这一个错。
    """
    return len(errors) == 1 and needle in errors[0]


def main() -> int:
    print("§0 夹具自检：卡是真卡、通道归类与夹具代码一致（不先钉住，后面红的是夹具而不是判据）")
    known = cs._known_card_ids()
    for fam, names in FAMILY_CARDS.items():
        for name in names:
            check(f"{fam}/{name}" in known, f"夹具用的卡在卡库里：{fam}/{name}")
    for name in TYPO_EXTRA:
        check(f"typography/{name}" in known, f"反例用的同族卡也在卡库里：typography/{name}")
    for ch, code in CODE.items():
        check(cs.shot_style_channel(code) == ch, f"夹具代码的主通道就是 {ch}")
    check(sorted(cs.shot_all_channels(CODE["vector"])) == ["vector"],
          "vector 夹具不夹带别的通道（否则 subject_channel 能凑对）")
    check(len(FAMILY_CARDS) >= 5 and all(len(v) >= 2 for v in FAMILY_CARDS.values()),
          "族表够铺开：⑮ 的绿色样本要 ≥ ceil(6/4)=2 个族、每族还得分到两张卡")

    # ------------------------------------------------------------------
    print("\n① 闸⑮ 第 1 条（卡名种类 ≥ ceil(镜数/4)）——先证红再证绿")
    eight_same = [_shot(i + 1, ch="vector", card_id="typography/paper-title-card")
                  for i in range(8)]
    errs, _ = cs.check_layout_family(eight_same)
    check(any("闸⑮" in e and "只用了 1 张不同的卡" in e for e in errs),
          f"8 镜全摆同一张卡 → 打回（{errs[0][:80] if errs else '没红'}）")
    check(any("typography/paper-title-card×8" in e for e in errs),
          "打回理由把这张卡摆了几镜摊平了——不摊平，读者不知道重复在哪")
    check(any("这一档至少要 2 张" in e for e in errs),
          "打回理由报的是**这一档**的门槛（ceil(8/4)=2），不是常量表本身")
    check(-(-8 // 4) == 2 and any(f"这一档至少要 {-(-8 // 4)} 张" in e for e in errs),
          "门槛是现算的 ceil(镜数/4)，与 8 镜这一档对得上")

    two_kinds_ok = _mixed6()
    check(cs.check_layout_family(two_kinds_ok) == ([], []),
          "6 镜六族 → 绿（镜数没到配额档但 ⑮ 的起步档就是 6）")

    # ------------------------------------------------------------------
    print("\n② 闸⑮ 第 2 条（同一族 ≤2 镜）——**八张不同的卡挤在同一族**就是对第 1 条的反例")
    same_family = [_shot(i + 1, ch="vector", card_id=f"typography/{n}")
                   for i, n in enumerate(TYPO_EXTRA)]
    errs, _ = cs.check_layout_family(same_family)
    check(any("只用了 1 张" not in e and "typography 族占了 6 镜" in e for e in errs),
          f"6 镜六张不同的卡、全在 typography 族 → 打回（{errs[0][:80] if errs else '没红'}）")
    check(len(errs) == 1,
          "这一份样本只犯族这一条——证明「族 ≤2」不是「卡名种类」的马甲，它自己会红")
    check(any("s01、s02、s03、s04、s05、s06" in e for e in errs),
          "族超限那一条把超限的镜逐个点名")
    check(any("当前族分布" in e for e in errs),
          "打回理由带上当前整片族分布——模型才知道往哪个族挪")

    ok8 = [_shot(i + 1, ch="vector", card_id=card(fam, i % 2))
           for i, fam in enumerate(["opening", "opening", "data", "data",
                                    "effects", "effects", "camera", "camera"])]
    check(cs.check_layout_family(ok8) == ([], []),
          "8 镜四族各两镜 → 绿（族的门槛是「不超过 2」，不是「必须各一」）")

    print("\n③ 闸⑮ 的分母与未判档：三镜片谈「族配额」连分母都不成立")
    three_same = [_shot(i + 1, ch="vector", card_id="typography/paper-title-card")
                  for i in range(3)]
    check(cs.check_layout_family(three_same) == ([], []),
          f"3 镜同一张卡也放行：镜数不到 {cs.MIN_SHOTS_FOR_QUOTA} 不判"
          "（放行 ≠ 通过，回执那半在 test_code_gates 里按未判报）")
    check(cs.MIN_SHOTS_FOR_QUOTA == 6 and cs.LAYOUT_FAMILY_MAX_SHOTS == 2,
          "两枚常数与正文/回执同源（改了这里就得改正文，见 §6）")
    no_card = [_shot(i + 1, ch="vector", card_id="", subject="活字盘") for i in range(6)]
    check(cs.check_layout_family(no_card) == ([], []),
          "一镜都没选卡时不在这里喊第二遍（「没选卡」是 card 那一道的错，报两遍会把清单淹掉）")

    # ------------------------------------------------------------------
    print("\n④ 闸⑯ `subject` 一栏——2026-10-18 起只提醒、不打回")
    r, w = cs.check_subject_named([_shot(1, subject="")])
    check(r == [] and _only(w, "闸⑯ 主体具名：s01：subject 没填"),
          f"subject 空串 → 只提醒（{w[:1]}）")
    naked = _shot(1, subject="")
    naked.pop("subject")            # 整键不写：老产物/局部改都可能少这一格
    r, w = cs.check_subject_named([naked])
    check(r == [] and _only(w, "subject 没填"),
          "整键不写同样只提醒——⑯ 不再因为「没填」把一镜打回")
    r, w = cs.check_subject_named([_shot(1, subject="理念")])
    check(r == [] and _only(w, "是抽象词"),
          f"subject=理念 → 只提醒（{w[:1]}）")
    check("活字盘" in w[0],
          "提醒里仍给一个**器物**的改写示范——抽象词画不出形状这件事由视觉评审兜底")
    r, w = cs.check_subject_named([_shot(1, subject="字")])
    check(r == [] and _only(w, "长度 1 字"),
          f"1 字是指代不是主体名 → 只提醒（{w[:1]}）")
    r, w = cs.check_subject_named([_shot(1, subject="一张 1900 年" + "的" * 20 + "地图")])
    check(r == [] and _only(w, "太长是整句话"),
          f"超 {cs.SUBJECT_MAX_CHARS} 字是整句话 → 只提醒（{w[:1]}）")
    for word in sorted(cs.ABSTRACT_SUBJECTS)[:5]:
        check(cs.check_subject_named([_shot(1, subject=word)])[1] != [],
              f"拒绝名单里随机抽一个也提醒得到：{word}")
    check(len(cs.ABSTRACT_SUBJECTS) >= 30,
          f"拒绝名单仍是「一份表」而不是几条特例（{len(cs.ABSTRACT_SUBJECTS)} 条）")
    _rtext, _wtext = cs.check_subject_named([_shot(1, subject="活字盘", ch="text")])
    check(_rtext == [] and _only(_wtext, "一条画法通道都没用上"),
          f"纯文字镜**只提醒不打回**（2026-10-23 晚：闸⑦ 撤了，这条安全网不再拦人；"
          f"「画没画」交给视觉评审按画面判）：打回={_rtext[:1]} 提醒={_wtext[:1]}")

    # ------------------------------------------------------------------
    print("\n⑤ 闸⑯ `subject_channel` 一栏——申报了才验证据，没申报只提醒")
    r, w = cs.check_subject_named([_shot(1, ch="vector", declare=None)])
    check(r == [] and _only(w, "没填 subject_channel"),
          f"画了图形主体却没申报通道 → 只提醒（{w[:1]}）")
    check("vector" in w[0],
          "提醒里报出**代码里认得**的通道，想补的人知道该填哪条")
    r, w = cs.check_subject_named([_shot(1, ch="vector", declare="")])
    check(r == [] and _only(w, "没填 subject_channel"),
          "填了个空串也算没填（空串与缺键在判据上是同一件事）")
    r, _ = cs.check_subject_named([_shot(1, ch="vector", declare="rough")])
    check(_only(r, "自报的通道必须有证据"),
          f"申报 rough 而代码里是 vector → **仍然打回**：自报无证据（{r[:1]}）")
    check("vector" in r[0],
          "打回理由把「代码里真用的是哪条」写出来，模型才知道该改成什么")
    r, _ = cs.check_subject_named([_shot(1, ch="rough", declare="three")])
    check(_only(r, "自报的通道必须有证据"),
          "反向再来一次（rough 代码申报 three）——不是只对某一条通道生效")
    r, _ = cs.check_subject_named([_shot(1, ch="text", declare="vector")])
    check(_only(r, "自报的通道必须有证据") and "vector" in r[0],
          f"没画画的镜**不许**申报通道（自报无证据的另一向）：{r[:1]}")
    check("一条画法通道都没用上" in r[0],
          "理由说清了「代码里一条画法通道都没用上」——"
          "不点这句，读者分不出是「画错了」还是「根本没画」")
    r, w = cs.check_subject_named([_shot(1, ch="vector", declare="svg")])
    check(r == [] and _only(w, "不在白名单"),
          f"不在白名单里的申报值 → 只提醒（按未申报处理，{w[:1]}）")

    print("\n⑥ 闸⑯ 的绿色样本：五条画法通道各自填对（`text` 那一档不可能是绿的）")
    for ch in ("rough", "vector", "katex", "canvas"):
        check(cs.check_subject_named([_shot(1, ch=ch)]) == ([], []),
              f"{ch} 镜按代码申报 → 绿")
    check(cs.check_subject_named([_shot(1, ch="three", declare="three")]) == ([], []),
          "three 镜按代码申报 → 绿（五条画法通道一条不漏）")
    check(cs.check_subject_named([_shot(1, ch="vector", declare="VECTOR")]) == ([], []),
          "申报值大小写归一（`VECTOR` 与 `vector` 是同一个通道，不该为此打回）")
    check(cs.shot_used_channels(CODE["canvas"]) == {"canvas"}
          and cs.shot_drawn(CODE["canvas"]),
          "canvas 镜被认成画法通道（闸⑦ 不会把它判成「只有文字与色块」）")
    check(cs.shot_used_channels('<canvas width={w} />') == set()
          and not cs.shot_drawn('<canvas width={w} />'),
          "空 <canvas> 标签不算通道——没有 getContext 就没有画")
    many = [_shot(1, ch="vector", subject=""), _shot(2, ch="text", declare="katex")]
    r, w = cs.check_subject_named(many)
    check(len(r) == 1 and "s02" in r[0] and len(w) == 1,
          f"两镜：s01 只提醒、s02 打回——三态不折叠（r={r}；w={w}）")

    # ------------------------------------------------------------------
    print("\n⑦ 两道闸真的挂在整份契约上（不是只活在 checker 里）：三个调用点共用 validate_code_spec")
    spec_ok, verrs = cs.validate_code_spec(_film(_mixed6()))
    check(verrs == [], f"六镜混合通道片过整份契约（实际 {verrs}）")
    check(all("subject_channel" in s for s in spec_ok["shots"]) and
          all(s["subject"] for s in spec_ok["shots"]),
          "两栏在归一后就落在 spec 的每一镜上（下游节点读的是它）")

    bad_subject = _mixed6()
    bad_subject[2]["subject"] = ""
    # ⑯ 在 2026-10-23 白天撤出整片判据、**当晚又加回**——加回的只是「账实不符」那一半
    # （申报了哪条通道就必须真用上），`subject` 没填/抽象词/长度越界仍落**提醒**。
    # 依据：允许它不判的实测后果是 12 镜里 8 镜申报 three/katex/rough 而代码全 vector。
    _sub_spec, verrs = cs.validate_code_spec(_film(bad_subject))
    _sub_gate = cs.whole_film_gate_results(_sub_spec["shots"])
    check(any("闸⑯" in w for w in _sub_gate[1])
          and not any("闸⑯" in e for e in _sub_gate[0]),
          f"闸⑯ 在整片判据里（2026-10-23 晚加回），但 `subject` 没填这一半"
          f"**只提醒不打回**（提醒 {_sub_gate[1][:1]} / 打回 {_sub_gate[0][:1]}）")
    check(callable(cs.check_subject_named),
          "但函数还在：回执与 style_facts 仍读它的读数（不拦 ≠ 不明说）")
    check(not any("闸" in e for e in verrs),
          f"契约入口不再装判据（2026-10-16 并档）：读数不随 validate 的 errors 抛出来"
          f"（{verrs[:1]}）")

    bad_family = _mixed6()
    bad_family[0]["card"] = bad_family[1]["card"] = bad_family[2]["card"] = \
        "typography/blur-slide"
    _fam_spec, verrs = cs.validate_code_spec(_film(bad_family))
    _fam_warns = cs.whole_film_gate_results(_fam_spec["shots"])[1]
    check(not any("闸⑮" in e for e in verrs)
          and (not cs.DEMOS_ENABLED or any("闸⑮" in w for w in _fam_warns)),
          f"⑮ 契约入口不打回；DEMOS_ENABLED={cs.DEMOS_ENABLED} 时它不进整片判据"
          f"（{verrs[:1]}）")

    # ⑮⑯ 是「整片层」的闸：模型真机上改的是**一镜**，闸却按整片算——这两个数就是
    # 「改一镜要顺带改几镜」的代价，写出来免得读的人以为它是逐镜闸。
    check(cs.check_layout_family.__doc__ and "同一**族**" in cs.check_layout_family.__doc__,
          "⑮ 的判据口径写在函数 docstring 里（下一把手改判据前先读那一段）")
    check("卡库目录名" in cs.check_layout_family.__doc__,
          "「族」的定义写明了取卡库类别目录名，不是模型自报的自由字符串（§9-Q84）")

    # ------------------------------------------------------------------
    print("\n⑧ 单源钉：计划卡/回执的账、schema、正文、文案词表——四处各读同一份常量")
    rows = cs.style_facts(_mixed6())
    check(rows["card_families"] == {"s01": "opening", "s02": "data", "s03": "effects",
                                    "s04": "typography", "s05": "camera", "s06": "ui-entrance"},
          f"style_facts 逐镜记族，账与卡面同一份：{rows['card_families']}")
    check(rows["subject_channels"]["s01"] == {"declared": "rough", "detected": ["rough"]},
          "style_facts 把申报的通道与代码里认得的通道**并排**摆着（不符的镜在账上自己现形）")
    check(rows["subject_channels"]["s03"]["detected"] == ["katex"]
          and rows["subject_channels"]["s03"]["declared"] == "katex",
          "两条通道的镜也一样（katex）")
    check(cs.style_facts([_shot(1, ch="text")])["card_families"]["s01"] == "typography"
          and cs.style_facts([_shot(1, ch="vector", card_id="")])["card_families"]["s01"] == "",
          "没选卡的镜记空串——「没选卡」与「选了某族」在账上是两件事")

    from storyline_server.nodes import motion_plan as mp        # 只在这里 import：
    from agent_framework.plan import support as plan_support    # 离线直调不带起别的链
    spec6, errs6 = cs.validate_code_spec(_film(_mixed6()))
    assert errs6 == [], f"夹具本身没过契约：{errs6}"
    payload = mp.plan_payload(spec6)
    row0 = payload["plan_table"][0]
    for key in ("subject", "subject_channel", "family"):
        check(key in row0 and key in payload["plan_table"][-1],
              f"计划卡逐镜账上有这一栏：{key}（卡面上直接看得见「这一镜画的是什么」「哪一族」）")
    # DEMOS_ENABLED=False 时 `card` 那一栏在归一时被当没写（见 `code_spec` 里那个开关的
    # 注释），所以族是空串；通道仍按代码字节认出来。两件事分别钉，避免把「开关关着」
    # 误读成「账算错了」。
    check((row0["family"] == "" if not cs.DEMOS_ENABLED else row0["family"] == "opening")
          and row0["subject_channel"] == "rough",
          f"卡面上的值与 style_facts 同一份：{row0['family']!r}/{row0['subject_channel']}")
    check(payload["style_warnings"] == [] or isinstance(payload["style_warnings"], list),
          "提醒清单仍是清单（⑯ 自 2026-10-18 起也会出提醒；⑮ 仍只出打回）")
    # 计划卡那两栏现在由**一个**函数产出（T28 顺手收的：六道整片闸的清单曾经在
    # 三处各抄一遍，抄漏一处就是「某个面看不见这道闸」）。所以这一条从「源码里有没有
    # 把这两道加进去」改成钉那个**唯一的清单**——⑮⑯ 在里头，且卡面走的是它。
    src_plan = (_HERE.parents[1] / "storyline_server" / "nodes" / "motion_plan.py").read_text("utf-8")
    check("cspec.whole_film_gate_results(" in src_plan,
          "计划卡的两栏走那一个唯一的三态出口（提醒/未判分家，见 §6-T28）")
    _gate_names = {g.__name__ for g in cs.WHOLE_FILM_GATES}
    # ⑮（版式族）只在用 demo 时才有意义：`DEMOS_ENABLED = False` 时它整条不进
    # 整片判据（见 `code_spec._DEMO_GATES` 的注释）。本文件不再假设它一定在清单里，
    # 但要钉住「在不在」与那个开关同向——清单与开关各说各话才是真的坏。
    check(("check_layout_family" in _gate_names) == bool(cs.DEMOS_ENABLED),
          f"⑮ 在不在清单里与 DEMOS_ENABLED={cs.DEMOS_ENABLED} 同向"
          f"（当前清单：{sorted(_gate_names)}）")
    # 2026-10-23 白天撤了 ⑯⑩；**当晚 ⑯ 加回**——它判的不是审美，是「说了有没有做到」
    # （申报 three 而代码里没有 three 是账实不符，没有「人眼替机器判」的余地）。
    # ⑩ 字色对比度仍是撤出的：读不清是审美，交给视觉评审。
    check("check_subject_named" in _gate_names
          and "check_ink_contrast" not in _gate_names
          and callable(cs.check_subject_named) and callable(cs.check_ink_contrast),
          f"2026-10-23 晚：⑯ 加回整片判据（只拦「申报通道无证据」），⑩ 仍撤出"
          f"（当前清单 {sorted(_gate_names)}），两个函数都还在")
    for name in ("subject", "subject_channel", "family"):
        check(name in plan_support.OUTPUT_FIELD_NAMES,
              f"文案词表收了 {name}——技能正文教了模型写它，不收就是假告警")

    schema = cs.spec_schema()
    shot_schema = schema["properties"]["shots"]["items"]
    # subject 从 2026-10-16 起**不再进 required**（§4：卡库/demo/主体是供给不是义务）。
    # 判据一个字没动——⑯ 照旧按声明判，缺这一格的镜在建议档点名（见上面那条）。
    # 这里钉的是"两边不许打架"：schema 不再逼它，判据也不再因为"没填"而当它是错的默认值。
    # 2026-10-18：`brief` 与 `code` 二选一（写了 code 就不需要 brief，反之亦然），
    # 所以 schema 的 required 只剩 `text`——两者都缺时由 `_norm_shot` 带镜号报。
    check("subject" not in shot_schema["required"]
          and set(shot_schema["required"]) == {"text"},
          f"schema 只硬逼 text（实得 {shot_schema['required']}）——"
          "这一镜成立的必要条件就这一样，其余都是供给")
    check(shot_schema["properties"]["subject"]["maxLength"] == cs.SUBJECT_MAX_CHARS,
          "schema 的长度上限与常量同源（schema 拦一次、判据拦一次，两次数同一个值）")
    check(shot_schema["properties"]["subject_channel"]["enum"] == list(cs.SUBJECT_CHANNELS),
          f"schema 的枚举与常量同源：{shot_schema['properties']['subject_channel']['enum']}")
    check("subject_channel" not in shot_schema["required"],
          "subject_channel **不**进 required：必填性按「这一镜有没有图形主体」分向判，"
          "一刀切成必填会逼纯文字镜自报无证据")
    check(cs.style_facts([_shot(1, ch="text")])["channels"]["s01"] == "text",
          "纯文字镜在 style 那栏仍是 text（⑯ 没有把它的通道改写成别的东西）")

    skill = skill_text()
    # ⑮（版式族配额）只在用 demo 时成立；`DEMOS_ENABLED = False` 的今天，正文里
    # 不该再教模型去凑「每族最多几镜」——留着它等于教一条没有闸在判的规矩。
    check(cs.DEMOS_ENABLED or "`ceil(镜数/4)`" not in skill,
          "正文没有在 DEMOS_ENABLED=False 时教 ⑮ 的族配额")
    check("主体具名（闸⑯）" in skill, "正文有 ⑯ 的教程段")
    check("视觉手艺" in skill and "自由" in skill,
          "正文有「视觉手艺」与「画法自由」两段——这是本轮质量改造的落点")
    check(f"{cs.SUBJECT_MIN_CHARS}~{cs.SUBJECT_MAX_CHARS} 字" in skill,
          "正文写着 subject 的长度区间")
    for ch in cs.SUBJECT_CHANNELS:
        check(f"`{ch}`" in skill, f"正文的通道表里有 `{ch}`（⑯ 的通道枚举与正文同源）")
    check("五条画法通道" in skill or "5 条画法通道" in skill,
          "正文写着通道条数，与 schema 的 enum 同一个说法")
    _chan = re.search(r"\n# 通道表（闸⑦）\n(.*?)(?=\n# )", skill, re.S)
    check(_chan is not None and "`image`" not in _chan.group(1),
          "通道表里没有 `image` 那一行——它不是画法通道，留着就会被当成能交差的一档")
    for word in ("理念", "趋势", "意义"):
        check(word in skill and word in cs.ABSTRACT_SUBJECTS,
              f"正文点名的抽象词与拒绝名单同源：{word}")
    check("不要填" in skill and "自报无证据" in skill,
          "正文写清了「一条画法通道都没用上的镜不许填这一栏」以及理由")
    check("写着 `rough` 而代码里一个 `roughjs` 都没有" in skill,
          "正文把「自报与代码不符」这条打回理由的样本写出来了")

    print("\n未验证清单（跳过 ≠ 通过）：")
    print("  · `subject` 画得像不像：机器判不了，归 ⑲-b 盲描述（本文件不判）")
    print("  · 真机渲染：这里是离线直调，像素证据在 test_code_gates 与真机取证账上")
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
