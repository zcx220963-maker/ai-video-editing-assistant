# -*- coding: utf-8 -*-
"""R7-T22：转场落在**拼接层**、进出场落在**母版**——两侧各自的那条保证怎么被证住。

这个通道一次加进来四件事，它们**不在同一层**，所以这份文件按层分段，而不是按开关分：

① 声明期（``validate_code_spec``）：转场档位、叠多久、入场与出场各是什么动作。
   坏输入先证红（档名、档位、动作三样都要带字段名打回），合法输入再证绿；
   旧片子没写这四栏时是 ``none``——「不写」与「写了 none」在像素上是同一件事，
   但在**账面**上要归一成同一个值，否则回执会写出「不转场、叠 0.4 秒」这种自相矛盾。
② 转场时长的对账（``check_transition``）：只在渲染趟、拿到**实测秒数**之后判。
   一镜多长是 TTS 说了算的，分镜期手里只有 ``min_duration_sec`` 这个下限——
   拿它判会把「真实时长本来够」的片子误杀，所以那条判据故意不在分镜期。
③ 「转场真的生效了」（``check_transition_effect``）：成片该比逐镜之和短 ``(n−1)×d`` 秒，
   这条是**机器判据**（画面上「少了 4 秒」看不出来）。容差按帧给：（接缝数+2）/fps。
④ 两侧在缓存键上的**相反**口径，这是这份文件里最要紧的一节：
   ``entrance``/``exit`` 是**母版**画进切片的，不进键就会拿旧切片冒充新片；
   ``transition`` 跑在**拼接**那一趟，逐镜切片一个字节都不动，进了键就是
   「整片把淡出换成擦除要重烧 20 镜」。两条都按字节断言，且各带一条**对照**
   （改 text 必须换键）——没有对照，「键没变」可能只是夹具压根没进键。

后两节是执行层与单源：⑤ 真跑两次整链（有转场 / 硬接）量成片时长，外加
「同一批镜跑第二次全命中缓存、成片照样带转场」——这就是 T22 的第一条断言
（逐镜独立渲染仍能命中缓存；**在镜头层**引入 ``<TransitionSeries>`` 即破，所以
⑦ 另有一条结构断言：源码与依赖里都不许出现它）。⑥ 窗口算术与闸④ 的采样点关系：
母版那层动作不许碰 0.15/0.5/0.85，这条按算术逐帧扫出来，不靠注释里的那句话。

为什么必须真跑 ffmpeg：转场这条保证的**下限**是像素级的——「ffmpeg 有没有按转场接」
只有成片时长量得出来；只断言账面档名，验不出「账上写了转场、拼接层还是硬接」。

便宜档同 ``test_code_gates.py``：240×320@10fps、``narration=False``；假服务与假缓存
直接 import 那一份（两处各造一套的话，「两个夹具对同一条通道给出不同结论」这种红
就分不清是通道的问题还是夹具的问题）。

跑法：容器内 ``PYTHONPATH=/app python tests/test_code_transition.py``。
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server import mediaops                             # noqa: E402
from storyline_server.mediaops import MediaError                   # noqa: E402
from storyline_server.motion import code_render as cr              # noqa: E402
from storyline_server.motion import code_spec as cs                # noqa: E402
from test_code_gates import FakeCache, FakeService, W, H, _shot    # noqa: E402

_fails = 0
_checks = 0
WORK = Path("/tmp") / "code_transition_work"
ROOT = Path(__file__).resolve().parents[1]
# 两种落地形状都要认：仓库里 backend/ 与 remotion/ 是兄弟目录，容器里 backend 就是
# 工作目录根（`/app`，remotion 在它底下）。写死一种，另一处这条断言就会去读一个
# 不存在的文件——而 read_text 抛的是异常，不是「这条判据没跑，记未判」。
REMOTION_SRC = (ROOT.parent if (ROOT.parent / "remotion").is_dir() else ROOT) / "remotion" / "src"
FILM_TSX = REMOTION_SRC / "FilmLayout.tsx"
SKILL = ROOT / "examples" / "skills" / "motion_explainer" / "SKILL.md"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _fspec(shots: list[dict], **knobs) -> dict:
    """整片开关可指定的夹具（``test_code_gates._spec`` 把这几栏写死成默认，改不了）。"""
    raw = {"title": "转场夹具", "aspect": "portrait", "fps": 10, "narration": False,
           "caption_style": "on_picture", "texture": "none", "shots": shots}
    raw.update(knobs)
    spec, errors = cs.validate_code_spec(cs.overlay_knobs(dict(raw), {}))
    assert errors == [], f"夹具本身没过契约：{errors}"
    spec = copy.deepcopy(spec)
    spec["width"], spec["height"] = W, H
    return spec


def _code_lines(path: Path) -> list[str]:
    """去掉注释行之后的源码（唯一用途见⑦：判「有没有用树内转场」）。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(("*", "//", "/*", "#")):
            continue
        out.append(line)
    return out


def _shots(**first) -> list[dict]:
    """两镜夹具（每调一次都是新字典——整片开关那一批 spec 要各拿一份互不牵连）。

    第二镜的文案**必须与第一镜不同**：镜号故意不进缓存键，两镜交出同一段代码本就该
    共用一份切片——所以文案也一样时它们会共用一个键，⑥ 那两条「几镜命中」的断言
    就永远数不到 2（第一镜现渲、第二镜命中，看上去像缓存出了问题）。
    """
    return [_shot(1, **first), _shot(2, text="同一片斜纹换个说法")]


def _key(spec: dict, shot_i: int = 0) -> str:
    """第 i 镜的缓存键。film 那一栏**就是** ``FILM_KNOBS`` 那八项——
    与真链（``render_code_shot``）取法一致，别的字段压根不进这条通道。"""
    film = {k: spec.get(k) for k in cs.FILM_KNOBS}
    return cs.shot_cache_key(spec["shots"][shot_i], film=film,
                             master_hash="a" * 16, lockfile_hash="b" * 16)


async def _run(spec: dict, *, name: str, cache=None, client=None):
    work = WORK / name
    if work.exists():
        shutil.rmtree(work)
    return await cr.render_code_motion(spec, providers=None, work=work,
                                       client=client or FakeService(), cache=cache)


def main() -> int:
    async def run() -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

        # =============================================================
        print("\n〔①〕声明期：坏输入先证红，合法输入再证绿")
        base_shots = [_shot(1), _shot(2)]

        spec_ok = _fspec(base_shots, transition="wipeleft", transition_sec="0.6",
                         entrance="rise", exit="zoom")
        check(spec_ok["transition"] == "wipeleft"
              and spec_ok["transition_sec"] == "0.6",
              f"合法档位原样落账（{spec_ok['transition']} / {spec_ok['transition_sec']}）")
        check(spec_ok["entrance"] == "rise" and spec_ok["exit"] == "zoom",
              f"入场/出场两栏分开存：{spec_ok['entrance']} / {spec_ok['exit']}——"
              "「进来」与「出去」是互为反向的两件事，共用一栏就写不出「自下浮入、向上浮出」")

        raw_bad = {"title": "坏档", "aspect": "portrait", "fps": 10, "narration": False,
                   "caption_style": "on_picture", "texture": "none",
                   "transition": "kenburns", "transition_sec": "1.5",
                   "entrance": "spiral", "exit": "wobble", "shots": base_shots}
        spec_bad, errs = cs.validate_code_spec(cs.overlay_knobs(dict(raw_bad), {}))
        blob = " / ".join(errs)
        check(len(errs) >= 4, f"四个坏值各报一条（实得 {len(errs)} 条）：{blob[:120]}")
        check("kenburns" in blob and "transition" in blob,
              "转场档名不认识要带档名与字段名打回（模型才知道自己写错了哪一格）")
        check("1.5" in blob and "transition_sec" in blob,
              "叠多久按**档位**收：1.5 不在 0.2/0.4/0.6/0.9 里就是错，不当它是「更长的转场」放过去")
        check("spiral" in blob and "exit" in blob and "wobble" in blob,
              "入场与出场各自校验，报错点名是哪一栏")
        check(spec_bad["transition"] == "none" and spec_bad["transition_sec"] == "0"
              and spec_bad["entrance"] == "none" and spec_bad["exit"] == "none",
              f"坏值归一成「不动作」而不是留着原串（{spec_bad['transition']} / "
              f"{spec_bad['transition_sec']} / {spec_bad['entrance']} / {spec_bad['exit']}）")

        spec_default = _fspec(base_shots)
        check(spec_default["transition"] == "none"
              and spec_default["transition_sec"] == "0"
              and spec_default["entrance"] == "none" and spec_default["exit"] == "none",
              "旧片子（没写这四栏）落账是 none/0——不选转场时账面不许留着 0.4 秒")
        check(cs.transition_plan(spec_default) == {"style": "none", "duration": 0.0, "seams": []},
              "transition_plan 是「不转场」的唯一出口：硬接那条路的 duration 是 0.0"
              "（seams 为空——没有逐接缝覆盖）")

        spec_num = _fspec(base_shots, transition="fade", transition_sec=0.4)
        check(spec_num["transition_sec"] == "0.4",
              f"时长写成数字也收（归一成档位字符串 {spec_num['transition_sec']!r}）——"
              "模型从 JSON Schema 抄的数字类型不该被自己打回")
        # 2026-10-19 真机：0.3 这种落在两档之间的数字会让整份分镜退回重交。
        # 差 0.1 秒不是「渲不出来」的错——吸到最近一档，照常入库。
        for given, want in ((0.3, "0.4"), (0.5, "0.4"), (0.8, "0.9"), (0.1, "0.2")):
            snapped = _fspec(base_shots, transition="fade", transition_sec=given)
            check(snapped["transition_sec"] == want,
                  f"{given} 秒吸到最近一档 {want}（实得 {snapped['transition_sec']!r}）："
                  "数字差一档不该让整份分镜重交")
        # MCP 工具入口按 `input_schema` 生成 Python 签名（`server.py::_signature_for`）：
        # type 写成联合时必须逐项映射，否则 pydantic 按单一 string 校验，模型传 0.4
        # 这一个数字就在工具入口被拒——还没走到 validate_code_spec 的归一。
        from storyline_server.server import StorylineServer
        class _Node:                                  # noqa: N801 - 只要 input_schema
            input_schema = {"type": "object",
                            "properties": {"transition_sec": {"type": ["string", "number"]}}}
        _anno = StorylineServer._signature_for(_Node()).parameters["transition_sec"].annotation
        check(str in getattr(_anno, "__args__", ()) and float in _anno.__args__,
              f"联合类型逐项映射进 MCP 签名（{_anno}）：数字 0.4 不该在工具入口被拒")
        props = cs.knob_props()
        for key in ("transition", "transition_sec", "entrance", "exit"):
            labels = (props[key].get("value_labels") or {})
            enum = props[key].get("enum") or []
            check(bool(enum) and all(v in labels and labels[v].strip() for v in enum),
                  f"{key} 是枚举且每个取值都有中文标签（{len(enum)} 档）——"
                  "卡面直接显示这些字，缺一个就露 wipeleft 这种机器名")
        check(all(isinstance(v, str) for v in props["transition_sec"]["enum"]),
              f"transition_sec 的取值是**字符串档位** {props['transition_sec']['enum']}："
              "整片开关的 schema 只收 enum/boolean（卡面每一项都要能列出候选与中文标签），"
              "写成自由数字就没有候选可列，0.37 秒这种值也只能在渲染期才发现配不上短镜")

        # =============================================================
        print("\n〔②〕转场时长对账：只在拿到实测秒数之后判，且判据会真的响")
        ok, _ = cs.check_transition([("s01", 6.3), ("s02", 4.1)], style="fade",
                                    duration=0.4)
        check(ok == [], f"0.4s 配 6.3s/4.1s 的镜：放行（半镜线是 {0.5 * 4.1:.2f}s）")
        bad, _ = cs.check_transition([("s01", 1.6), ("s02", 6.3)], style="fade",
                                     duration=0.9)
        check(len(bad) == 1 and "s01" in bad[0] and "0.80" in bad[0],
              f"0.9s 叠在 1.6s 的镜上（半镜 0.80s）：打回，且报得出是哪一处、限值多少"
              f"——{bad[0][:90] if bad else '没报'}")
        check("0.2, 0.4, 0.6" in (bad[0] if bad else ""),
              "打回时顺手给出「降到哪几档还来得及」——只说错了不改，模型只能瞎试")
        edge, _ = cs.check_transition([("s01", 1.8), ("s02", 6.3)], style="fade",
                                      duration=0.9)
        check(len(edge) == 1,
              "边界取**严格小于**：0.9s 对半镜 0.90s 打回（等于半镜时 offset+duration "
              "正好顶到段尾，浮点误差会把一次好渲染挡在门外）")
        blank, _ = cs.check_transition([("s01", 0.0), ("s02", 6.3)], style="fade",
                                       duration=0.4)
        check(len(blank) == 1 and "s01" in blank[0] and "不许当通过" in blank[0],
              "拿不到实测时长就**不判**：报「没法判」而不是放行——未判从不等于通过")
        none_ok, _ = cs.check_transition([("s01", 0.3)], style="none", duration=0.0)
        check(none_ok == [], "没选转场就不判（判据不该对硬接的片子说话）")
        many, _ = cs.check_transition([("s01", 1.0), ("s02", 1.1), ("s03", 1.2)],
                                      style="fade", duration=0.9)
        check(len(many) == 1 and "另有 1 处" in many[0],
              f"多处超限报第一处 + 总处数（{many[0][-40:] if many else ''}）——"
              "逐处报会把打回清单淹掉，而它们本来是同一条错")

        # =============================================================
        print("\n〔②-2〕逐接缝转场（层E1）：seams 覆盖与硬接混搭")
        # 整片 fade 0.4s，第二镜之后覆盖为 wipeleft，第一镜之后覆盖为 none（硬接）
        spec_seams = _fspec(
            [_shot(1, transition="none"), _shot(2, transition="wipeleft"), _shot(3)],
            transition="fade", transition_sec="0.4")
        plan = cs.transition_plan(spec_seams)
        check(plan["style"] == "fade" and plan["duration"] == 0.4,
              f"整片档仍然是 fade/0.4（{plan['style']}/{plan['duration']}）")
        check(len(plan["seams"]) == 2,
              f"seams 有 2 条覆盖（s01→none, s02→wipeleft），实得 {len(plan['seams'])}")
        s01_seam = next((s for s in plan["seams"] if s.get("after") == "s01"), None)
        s02_seam = next((s for s in plan["seams"] if s.get("after") == "s02"), None)
        check(s01_seam and s01_seam["style"] == "none",
              f"s01 之后的接缝是 none（硬接覆盖整片 fade）：{s01_seam}")
        check(s02_seam and s02_seam["style"] == "wipeleft" and s02_seam["duration"] == 0.4,
              f"s02 之后的接缝是 wipeleft/0.4（沿用整片时长）：{s02_seam}")
        # check_transition 逐接缝判：none 的那一条不判
        ok_seams, _ = cs.check_transition(
            [("s01", 6.0), ("s02", 5.0), ("s03", 4.0)],
            style="fade", duration=0.4, seams=plan["seams"])
        check(ok_seams == [],
              f"逐接缝判：none 不判、fade 0.4 配 6/5/4s 都放行：{ok_seams}")
        # 整片 none，但某一条接缝覆盖为 fade（duration 从默认取）
        spec_mix = _fspec(
            [_shot(1, transition="fade"), _shot(2)],
            transition="none")
        plan_mix = cs.transition_plan(spec_mix)
        check(plan_mix["style"] == "none" and plan_mix["duration"] == 0.0,
              f"整片不转场（{plan_mix['style']}/{plan_mix['duration']}）")
        check(len(plan_mix["seams"]) == 1 and plan_mix["seams"][0]["style"] == "fade"
              and plan_mix["seams"][0]["duration"] == float(cs.TRANSITION_SEC_DEFAULT),
              f"s01 覆盖为 fade，duration 从默认档取（{cs.TRANSITION_SEC_DEFAULT}）："
              f"{plan_mix['seams']}")
        # check_transition_effect 逐接缝
        eff_secs = [4.0, 4.0]
        eff_seams = [0.4]  # 一条接缝，0.4s
        check(cs.check_transition_effect(eff_secs, 8.0 - 0.4, style="fade",
                                         duration=0.4, fps=10, seams=eff_seams) == [],
              "逐接缝生效判：4+4 叠 0.4 → 7.6s，判生效")
        check(len(cs.check_transition_effect(eff_secs, 8.0, style="fade",
                                             duration=0.4, fps=10, seams=eff_seams)) == 1,
              "逐接缝生效判：4+4 硬接成 8.0s → 红（该短 0.4s）")
        # seams 全 0 = 全部硬接，不判
        check(cs.check_transition_effect(eff_secs, 8.0, style="fade",
                                         duration=0.4, fps=10, seams=[0.0]) == [],
              "seams=[0.0] = 这一条硬接，不判（active=0）")
        # seams_to_per_index
        spi = cs.seams_to_per_index(
            [{"after": "s02", "style": "fade", "duration": 0.4}],
            [("s01", 4.0), ("s02", 5.0), ("s03", 6.0)])
        check(spi == [("", 0.0), ("fade", 0.4)],
              f"seams_to_per_index：第 0 条没覆盖（'',0），第 1 条覆盖了：{spi}")

        # =============================================================
        print("\n〔③〕「转场真的生效了」：成片该短多少就短了多少")
        secs = [4.0, 4.0, 4.0]
        taken = cs.check_transition_effect([4.0, 4.0, 4.0], 12.0 - 0.8, style="fade",
                                           duration=0.4, fps=10)
        check(taken == [], "三段各 4s、成片 11.2s（短了 2×0.4）：判生效")
        not_taken = cs.check_transition_effect(secs, 12.0, style="fade",
                                               duration=0.4, fps=10)
        check(len(not_taken) == 1 and "本该比逐镜之和短 0.80s" in not_taken[0],
              f"拼接层退回硬接（一点没叠）：红，且报得出「该短多少、实际短多少」——"
              f"{not_taken[0][:80]}")
        tol = (3 - 1 + 2) / 10
        # `round(…, 3)` 不是装饰：12.0-0.8-0.4 在二进制里是 10.799999999999999，
        # 差出来就是 0.4000000000000011 > 容差——这条断言会为浮点噪声而红。
        check(cs.check_transition_effect(secs, round(12.0 - 0.8 - tol, 3), style="fade",
                                         duration=0.4, fps=10) == [],
              f"容差 (接缝+2)/fps = {tol:.2f}s 之内算生效（每次合并有约 1 帧取整差）")
        check(len(cs.check_transition_effect(secs, 12.0 - 0.8 + tol + 0.01,
                                             style="fade", duration=0.4, fps=10)) == 1,
              "越过容差就红——容差是给取整差的，不是给「少叠了一点」的")
        check(cs.check_transition_effect(secs, 12.0, style="none", duration=0.0,
                                         fps=10) == [],
              "没选转场时这条判据不说话（硬接片的成片时长本来就等于逐镜之和）")

        # =============================================================
        print("\n〔④〕缓存键：两侧口径**相反**，各带一条对照")
        spec_a = _fspec(_shots(), transition="fade", transition_sec="0.4")
        k0 = _key(spec_a)
        spec_b = _fspec(_shots(), transition="dissolve", transition_sec="0.9")
        check(_key(spec_b) == k0,
              "整片把「淡叠 0.4 秒」换成「溶解 0.9 秒」→ 逐镜的键一个字不变："
              "转场跑在拼接那一趟，切片一个字节不动（进键的代价是重烧 20 镜的 Remotion 帧）")
        check(_key(_fspec(_shots(text="斜纹填满的一片方"), transition="fade",
                          transition_sec="0.4")) != k0,
              "对照：改文案**必须**换键——不然上面那条「键没变」可能只是夹具全都没进键")
        check(_key(_fspec(_shots(), entrance="rise")) != k0
              and _key(_fspec(_shots(), exit="fade")) != k0,
              "换进出场 → **换键**：那一层动画是母版逐帧画进切片的，不进键就会拿旧切片冒充新片"
              "（与字幕那两维同族：都是母版画的像素）")
        # `palette`（2026-10-17）也在这份名单里：整片底色是母版/起点那几笔**画进切片**
        # 的颜色，换一套就是另一个像素——与字幕那两维同族。
        # `art`（2026-10-18）同理：写手按它重写每一镜的代码，切片末段那趟统一调色
        # 也读它（`render.art_grade`）——换美术方向就是换像素。
        check(set(cs.FILM_KNOBS) == {"fps", "aspect", "narration", "voice", "rate",
                                     "caption_style", "highlight_style", "highlight_color",
                                     "palette", "entrance", "exit", "art", "protagonist"},
              f"FILM_KNOBS 就是这七项 + 字幕两维 + 强调色 + 整片色板 + 美术方向 + 主角"
              f"（都是母版/镜头画进切片的像素）：{cs.FILM_KNOBS}")
        check(not (set(cs.SWITCH_KNOBS) & set(cs.FILM_KNOBS)),
              "花钱的那三个开关**不在**这份名单里：它们不改像素，改的是「烧完之后再看一遍」"
              "——进了这个名字就会让「开一次回看」变成「全片重烧」")
        check("transition" not in cs.FILM_KNOBS and "transition_sec" not in cs.FILM_KNOBS,
              "转场这两栏不在 FILM_KNOBS 里（上面那条「同键」才成立）")
        check("transition" not in cs.SHOT_KNOBS,
              f"也不在 SHOT_KNOBS 里（{cs.SHOT_KNOBS}）——转场不是某一镜的属性")

        # =============================================================
        print("\n〔⑤〕窗口算术：母版那层动作不许碰闸④ 的采样点")
        check(cs.shot_motion_window(100) == 10 and cs.shot_motion_window(200) == 15,
              f"窗口 = min(这一镜的 10%, 15 帧)（100 帧 → {cs.shot_motion_window(100)}、"
              f"200 帧 → {cs.shot_motion_window(200)}）")
        check(cs.shot_motion_window(10) == 0 and cs.shot_motion_window(4) == 0,
              "短到 2 帧以下就当没有：1 帧的淡入不是淡入，是硬切上多一层闪")
        worst = 0.0
        worst_f = 0
        for frames in range(2, 601):
            w = cs.shot_motion_window(frames)
            if w <= 0:
                continue
            if w / (0.15 * frames) > worst:
                worst, worst_f = w / (0.15 * frames), frames
        check(worst < 1.0,
              f"逐帧扫 2~600 帧：动作窗口最坏占闸④ 第一个采样点的 {worst:.2f}"
              f"（{worst_f} 帧那一档）——窗口收在 0.15 之前，母版做动作不会让闸④ 开始误报空壳")
        # 红样（纯算术，不改模块常量）：上限只要抬到 20%，就会顶进 0.15 那一格。
        bad_share = 0.20
        hit = [f for f in range(16, 601)
               if min(round(f * bad_share), cs.SHOT_MOTION_MAX_FRAMES) >= 0.15 * f]
        check(bool(hit),
              f"把上限抬到 {bad_share:.0%} 就会在 {hit[:3]} 这些帧数上碰到 0.15 ——"
              "这条关系是真的，不是注释里的一句好听的话")
        check(cs.SHOT_MOTION_MAX_SHARE == 0.10 and cs.SHOT_MOTION_MAX_FRAMES == 15,
              f"常量停在验过的这一对：{cs.SHOT_MOTION_MAX_SHARE} / "
              f"{cs.SHOT_MOTION_MAX_FRAMES}")

        # =============================================================
        print("\n〔⑥〕真跑 ffmpeg：有转场 / 硬接 / 重跑命中缓存")
        clips_spec = _fspec(_shots(), transition="fade", transition_sec="0.4")
        res = await _run(clips_spec, name="fade")
        ledger_sec = [float(r["sec"]) for r in res["shots"]]
        want_short = 0.4          # 一处接缝 × 0.4s
        gap = sum(ledger_sec) - float(res["duration"])
        check(res["transition"] == "fade" and res["transition_gaps"] == 1,
              f"账面：转场 {res['transition']}、接缝 {res['transition_gaps']} 处")
        check(abs(gap - want_short) <= (1 + 2) / 10,
              f"成片比逐镜之和短 {gap:.2f}s（该短 {want_short:.2f}s，逐镜 "
              f"{[round(s, 2) for s in ledger_sec]}，成片 {res['duration']:.2f}s）——"
              "画面上「少了 0.4 秒」是看不出来的，这条机器判据才是「转场真生效」的证据")
        check(abs(res["overlap_sec"] - want_short) <= (1 + 2) / 10,
              f"回执自己带着这笔账：overlap_sec={res['overlap_sec']}")

        hard = await _run(_fspec(_shots()), name="none")
        hard_gap = sum(float(r["sec"]) for r in hard["shots"]) - float(hard["duration"])
        check(abs(hard_gap) <= 1 / 10,
              f"对照：不选转场时成片时长就等于逐镜之和（差 {hard_gap:+.2f}s）——"
              "两条路的区别只有接缝")
        not_taken = cs.check_transition_effect([float(r["sec"]) for r in hard["shots"]],
                                               float(hard["duration"]), style="fade",
                                               duration=0.4, fps=10)
        check(len(not_taken) == 1,
              "红样：把**硬接那一次**的实测时长喂给生效判据（假装 spec 选了 fade）→ 红。"
              "这就是「账上写了转场、拼接层没按它接」的现场")

        cache = FakeCache()
        again = await _run(clips_spec, name="fade2", cache=cache)
        warmed = await _run(clips_spec, name="fade3", cache=cache)
        check(warmed["cached_shots"] == 2,
              f"同一批镜跑第二次：{warmed['cached_shots']} 镜全命中缓存"
              "（逐镜独立渲染仍命中——转场没落进镜头层，所以缓存键没被搅动）")
        check(abs(float(warmed["duration"]) - float(again["duration"])) <= 1 / 10,
              f"命中缓存的成片照样带着转场（{warmed['duration']:.2f}s vs 现渲的 "
              f"{again['duration']:.2f}s）——转场是每次拼接都重新落一遍的")
        switch = await _run(_fspec(_shots(), transition="fade",
                                   transition_sec="0.4", entrance="rise"),
                            name="rise", cache=cache)
        check(switch["cached_shots"] == 0,
              "只把进出场从 none 换成 rise：0 镜命中缓存（四④ 那条「进键」在真链上的样子）")

        # =============================================================
        print("\n〔⑥-2〕逐接缝覆盖**真跑一次**：硬接与叠化混在同一条拼接链上")
        # 为什么这一条非跑 ffmpeg 不可：这一支在拼接层走的是 `concat` 而不是 `xfade`——
        # 「一条链上两种接法交替」的合法性只有 ffmpeg 说得清。层E1 落地时正是这一支真红过
        # **两次**，两次都是纯算术那一节（〔②-2〕）看不到的：
        #   ① `concat` 要的是「每段先视频再音频」四个输入口，只喂两个视频口会在链接期报
        #      「pad 1 (audio) 接到了视频输出」；
        #   ② `concat` 吐出来的流时基是 1/1000000，而没经过 concat 的那一路还停在输入文件的
        #      1/10 —— `xfade` 要求两侧时基一致，于是报「timebase do not match」。
        # 纯叠化（〔⑥〕）与纯硬接（hard）两条老路都碰不到这两条，所以它们是层E1 独有的红。
        # 三镜的代码必须**各不一样**：⑨ 跨镜同构从 3 镜起判，三支同构的代码会在 `_fspec`
        # 里就被打回，那时红的是夹具而不是判据。
        seam_shots = [
            # 2026-10-16 闸⑦「每镜必画」上线后，这批镜的代码里各补一件真画的东西：
        # 原来几支都是纯文字，连 `_fspec` 那一道都过不去。三件**各不相同**
        # （圆 / 方 / 线）——⑨ 跨镜同构从 3 镜起判，三支同构的代码会在夹具那一层
        # 就被打回，那时红的是夹具而不是判据。P1（2026-10-17）注入契约另加了
        # 注入头（读 `getInputProps`、用 `text`、摆 `<CaptionLayer/>`）；加完之后
        # 骨架越过 `_SHAPE_MIN_TOKENS`(40)，⑫ 会开始比相似度——本文件对它没有
        # 空账断言（见 `test_code_gates._shot` 那段同族说明）。
            _shot(1, transition="none", card="opening/brand-ink-open",
                  code='export const Shot = () => (<FilmLayout>甲<svg><circle /></svg>'
                       '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
            _shot(2, transition="wipeleft", text="第二镜换个说法",
                  card="data/before-after-slider-scrub",
                  code='export const Shot = () => (<FilmLayout><div>乙</div><svg><rect /></svg>'
                       '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
            _shot(3, text="第三镜再换个说法", card="effects/assemble-then-type-flyin",
                  code='export const Shot = () => (<FilmLayout>丙<svg><line /></svg>'
                       '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
        ]
        seam_spec = _fspec(seam_shots, transition="fade", transition_sec="0.4")
        seams_res = await _run(seam_spec, name="seams")
        seam_sec = [float(r["sec"]) for r in seams_res["shots"]]
        seam_gap = sum(seam_sec) - float(seams_res["duration"])
        check(seams_res["transition"] == "fade" and seams_res["transition_gaps"] == 1,
              f"账面：整片 fade 0.4，但只有 1 处接缝真叠（另一处被覆盖成硬接）——"
              f"实得 {seams_res['transition_gaps']} 处")
        check(abs(seam_gap - 0.4) <= (2 + 2) / 10,
              f"混搭成片只短 0.40s（逐镜 {[round(s, 2) for s in seam_sec]}、"
              f"成片 {seams_res['duration']:.2f}s、实短 {seam_gap:.2f}s）——"
              "硬接那一处一秒都没叠。层E1 之前整片只有一个档，这一支根本表达不出来")
        check([s["style"] for s in seams_res["transition_seams"]] == ["none", "wipeleft"],
              f"回执逐接缝报得出各是哪一档（s01 之后 none、s02 之后 wipeleft）："
              f"{seams_res['transition_seams']}")
        # 反向的对照：把两处都覆盖成硬接 → 成片时长回到逐镜之和，且一处都不叠。
        # 没有这一条，「上面那条短了 0.4s」可能只是某个常量的功劳。
        both_hard = await _run(
            # 同上：两镜各带一件真画的东西（闸⑦「每镜必画」逐镜判），注入头与上面同批。
            _fspec([_shot(1, transition="none", card="opening/brand-ink-open",
                          code='export const Shot = () => (<FilmLayout>甲<svg><circle /></svg>'
                               '{getInputProps().text}<CaptionLayer /></FilmLayout>);'),
                    _shot(2, transition="none", text="第二镜换个说法",
                          card="data/before-after-slider-scrub",
                          code='export const Shot = () => (<FilmLayout><div>乙</div>'
                               '<svg><rect /></svg>{getInputProps().text}'
                               '<CaptionLayer /></FilmLayout>);')],
                   transition="fade", transition_sec="0.4"),
            name="seams_hard")
        both_gap = (sum(float(r["sec"]) for r in both_hard["shots"])
                    - float(both_hard["duration"]))
        check(both_hard["transition_gaps"] == 0 and abs(both_gap) <= 2 / 10,
              f"对照：两处都覆盖成 none → 成片时长等于逐镜之和（差 {both_gap:+.2f}s、"
              f"叠了 {both_hard['transition_gaps']} 处）——「整片 fade、逐接缝全硬接」"
              "这条路走得通，且它走的还是纯 concat 那条老链")

        try:
            mediaops.concat([str(again["shot_files"]["s01"]["clip"]),
                             str(again["shot_files"]["s02"]["clip"])],
                            WORK / "too-long.mp4", fps=10,
                            transition={"style": "fade", "duration": 9.0})
            check(False, "转场时长不短于最短的一镜时必须拒（xfade 会吃掉整段）")
        except MediaError as exc:
            check("不短于最短的一镜" in str(exc),
                  f"兜底那道：9s 的转场对 3.88s 的镜 → MediaError「{str(exc)[:40]}…」"
                  "（判据在渲染趟，这一条防的是绕过判据的调用者）")

        # =============================================================
        print("\n〔⑦〕单源与结构：档名两处一致，且转场不许落回镜头层")
        check(set(mediaops._XFADE) >= set(cs.TRANSITION_STYLES) - {"none"},
              f"契约里每一档都在 ffmpeg 那张表里接得上（缺的会静默退成 fade）"
              f"：{sorted(set(cs.TRANSITION_STYLES) - {'none'} - set(mediaops._XFADE))}")
        tsx = FILM_TSX.read_text(encoding="utf-8")
        line = next((ln for ln in tsx.splitlines()
                     if "ShotMotionStyle =" in ln), "")
        rhs = line.split("=", 1)[-1].strip().rstrip(";").strip() if line else ""
        ts_styles = tuple(part.strip().strip("'\"") for part in rhs.split("|")) if rhs else ()
        check(ts_styles == cs.SHOT_MOTION_STYLES,
              f"母版那五个档名与契约逐值相等（TS {ts_styles} / Python "
              f"{cs.SHOT_MOTION_STYLES}）——跨不了语言，只能靠这条闸")
        check("entranceFrames" in tsx and "exitFrames" in tsx,
              "母版按**注入的帧数**插值（窗口算术只有 Python 一份，见 ⑤）")
        # 这条是拿真机证出来的：模板里的 `<FilmLayout text=…>` 并不转交这四条，于是母版
        # 若只认 props，整片进出场在生产上**一帧都不演**——而「没演」与「没选」在成片、
        # 在闸④/⑤/⑥ 的读数上都一模一样（真渲取证：去掉 `getInputProps` 回退，探针那一段
        # 照样全绿，等于判据在空转）。所以母版自己从渲染入参里取，显式 props 只做优先。
        check("getInputProps" in tsx and "resolveShotMotion" in tsx,
              "母版自己从渲染入参里取进出场档——镜头代码漏转交一行，整片不该静默地不演")
        check("entrance = 'none'" not in tsx.split("export const FilmLayout")[1][:400],
              "这四条在组件签名里**不许给默认值**：「没转交」与「转交了 none」要分得开，"
              "给了默认值就分不开了（回退会永远拿不到机会）")
        # 只看**代码行**：母版里有一整段注释解释「为什么不引 @remotion/transitions」，
        # 整篇扫文本会把那段说明当成违规——判据不该罚一份把它写清楚的注释。
        code = _code_lines(FILM_TSX)
        pkg = json.loads((REMOTION_SRC.parent / "package.json").read_text(encoding="utf-8"))
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        check(not any("TransitionSeries" in ln or "@remotion/transitions" in ln
                      for ln in code),
              "母版里没有树内转场：`<TransitionSeries>` 要求相邻片段在同一棵 React 树里，"
              "与我们逐镜独立渲、按 shot_cache_key 缓存这件事**直接冲突**")
        check(not any(k.startswith("@remotion/transitions") for k in deps),
              "依赖表里也没有它（装上了就迟早有人用；这条防的是「顺手 import 一下」）")
        stray = [p.name for p in REMOTION_SRC.rglob("*.ts*")
                 if any("TransitionSeries" in ln for ln in _code_lines(p))]
        check(not stray, f"整棵 remotion/src 里都没有树内转场（扫到的：{stray}）")
        props = cr.shot_input_props(_shot(1), _fspec(_shots()), frames=100,
                                    frame_words=[])
        check(props["entrance"] == "none" and props["entranceFrames"] == 10,
              f"注入母版的是「档名 + 帧数」两个数（{props['entrance']} / "
              f"{props['entranceFrames']}）")
        props_in = cr.shot_input_props(_shot(1), _fspec(_shots(), transition="fade",
                                                        entrance="rise"),
                                       frames=100, frame_words=[])
        check(props_in["entrance"] == "rise"
              and "transition" not in props_in and "transition_sec" not in props_in,
              "母版拿不到转场：那两栏**不进** input_props——镜头层没有接缝可接，"
              "给了它就等于告诉母版「你有转场可做」，而真正的接缝在拼接层")

        skill = SKILL.read_text(encoding="utf-8")
        check(all(k in skill for k in ("transition", "transition_sec", "entrance", "exit")),
              "技能正文讲了这四个开关——教程不写，模型只能从枚举里猜哪个档是什么意思")
        check(all(f"`{s}`" in skill for s in cs.SHOT_MOTION_STYLES[1:]),
              f"五个动作档在正文里都点了名：{cs.SHOT_MOTION_STYLES[1:]}")
        check(all(v in skill for v in cs.TRANSITION_SEC_CHOICES),
              f"四个时长档位在正文里都点了名：{list(cs.TRANSITION_SEC_CHOICES)}——"
              "正文少写一档，模型就会写 0.3 秒，然后在渲染趟被打回")
        check("不重烧" in skill,
              "正文说清了「换转场不重烧任何一镜」：这是这条通道对用户可见的那一半"
              "（不然没人知道便宜的档位与贵的档位在成本上没差别）")

    asyncio.run(run())
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
