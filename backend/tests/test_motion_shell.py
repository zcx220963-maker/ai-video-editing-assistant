# -*- coding: utf-8 -*-
"""片壳（`shell`）：外框与底部字幕由壳统一画，镜头代码只画主体。

要证的事：

① 契约只收登记过的壳名；`shell_label` 截断、壳不进计划卡旋钮；
② 选了壳就换缓存键（壳画的是像素），壳名或期号变了都得重烧；
③ 系统不再往镜头代码里补 `<CaptionLayer/>`——壳模式下字幕归壳，补一条就是两条；
④ 注入给母版的 props 带 `shell` / `shellLabel`，且 `captionFromCode` 恒为 false；
⑤ 美术导演的提示词在壳模式下不再让 brief 给字幕排位置。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_motion_shell.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import art_director as ad   # noqa: E402
from storyline_server.motion import code_render as cr   # noqa: E402
from storyline_server.motion import code_spec as cs     # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


CODE = ('import {getInputProps} from "remotion"; '
        'export const Shot = () => { const {text} = getInputProps(); '
        'return (<AbsoluteFill><svg /></AbsoluteFill>); };')


def _shot(**over) -> dict:
    base = {"id": "s01", "code": CODE,
            "text": "中子星一茶匙物质重约十亿吨", "highlight": ["中子星"],
            "min_duration_sec": 6.0, "subject": "中子星",
            "subject_channel": "vector"}
    base.update(over)
    return base


def _spec(**over) -> dict:
    base = {"title": "片壳测试", "aspect": "landscape", "fps": 30,
            "narration": True, "caption_style": "on_picture", "texture": "none",
            "shots": [_shot()]}
    base.update(over)
    return base


def main() -> int:
    print("① 契约：只收登记过的壳名，期号截断，壳不进卡面")
    spec, errors = cs.validate_code_spec(_spec(shell="paper-flat",
                                               shell_label="第 001 期 · 测试"))
    check(errors == [], f"paper-flat 是合法壳（实际 {errors}）")
    check(spec.get("shell") == "paper-flat", "壳名原样留下")
    check(spec.get("shell_label") == "第 001 期 · 测试", "期号小字原样留下")
    long_label, _ = cs.validate_code_spec(_spec(
        shell="paper-flat", shell_label="期" * 200))
    check(len(long_label.get("shell_label") or "") == cs.SHELL_LABEL_MAX_CHARS,
          f"超长期号只截到 {cs.SHELL_LABEL_MAX_CHARS} 字，不为一句话打回整份分镜")

    _, bad = cs.validate_code_spec(_spec(shell="wood-frame"))
    check(bool(bad) and "shell" in " ".join(bad),
          f"没登记的壳名报错（entry 里没有这个组件，渲不出来）：{bad}")

    no_label, _ = cs.validate_code_spec(_spec(shell="paper-flat"))
    check("shell_label" not in no_label, "没写期号就不出现")
    plain, _ = cs.validate_code_spec(_spec())
    check("shell" not in plain, "没选壳的老片子逐字节不变")

    props = cs.spec_schema()["properties"]
    check(props["shell"]["enum"] == list(cs.SHELLS)
          and props["shell_label"]["maxLength"] == cs.SHELL_LABEL_MAX_CHARS,
          "schema 的枚举与上限从常量拼（两处各抄一遍必然漂）")
    import json as _json
    check("shell" not in _json.dumps(cs.knob_props(), ensure_ascii=False),
          "壳不进计划卡旋钮（自由值不上卡）")
    check("shell" not in cs.FILM_KNOBS and "shell_label" not in cs.FILM_KNOBS,
          "壳不在整片键清单里（它按自己的两栏单独进键）")

    print()
    print("② 缓存键：选了壳 / 换壳 / 换期号都换键")
    def key(**over) -> str:
        film = {k: spec.get(k) for k in cs.FILM_KNOBS}
        film.update({k: v for k, v in over.items() if k in ("shell", "shell_label")})
        return cs.shot_cache_key(spec["shots"][0], film=film,
                                 master_hash="a" * 16, lockfile_hash="b" * 16)

    k_shell = key(shell="paper-flat", shell_label="第 001 期 · 测试")
    check(k_shell != key(), "没壳 → 选壳：换键（纸底/色条/钟/字幕都是像素）")
    check(k_shell != key(shell="paper-flat", shell_label="第 002 期 · 测试"),
          "同一壳换期号 → 换键（右上角那行字变了）")
    check(key(shell="paper-flat") != key(shell="paper-flat", shell_label="第 001 期"),
          "期号从无到有 → 换键")

    print()
    print("③ 壳模式下系统不再补 <CaptionLayer/>")
    # 顶层收尾必须独占一行：`autofix_caption_layer` 靠 `^\\s*</AbsoluteFill>` 定位。
    template = ('import {getInputProps} from "remotion";\n'
                'export const Shot = () => { const {text} = getInputProps();\n'
                '  return (\n'
                '    <AbsoluteFill>\n'
                '      <svg />\n'
                '    </AbsoluteFill>\n'
                '  );\n'
                '};')
    with_shell = _spec(shell="paper-flat", shots=[_shot(code=template)])
    out, notes = cs.autofix_caption_layer(with_shell)
    check(out["shots"][0]["code"] == template and notes == [],
          "壳模式下不补字幕件、一个字节不改（补了就是与壳叠成两条）")
    without_shell = _spec(shots=[_shot(code=template)])
    out2, notes2 = cs.autofix_caption_layer(without_shell)
    check("CaptionLayer" in out2["shots"][0]["code"] and notes2,
          "没壳时行为不变：系统仍替它补上字幕")

    print()
    print("④ 注入 props：带壳名与期号，字幕恒归壳")
    shot = spec["shots"][0]
    p = cr.shot_input_props(shot, spec, frames=90, frame_words=[])
    check(p.get("shell") == "paper-flat", f"shell 进了 props：{p.get('shell')}")
    check(p.get("shellLabel") == "第 001 期 · 测试", "壳的期号小字也进了 props")
    check(p.get("captionFromCode") is False,
          "captionFromCode 恒为 false——镜头代码写了字（招牌/图表）也不让母版让位")
    p2 = cr.shot_input_props(plain["shots"][0], plain, frames=90, frame_words=[])
    check("shell" not in p2 and "shellLabel" not in p2,
          "没选壳时 props 不多这两个键（老片子逐字节不变）")

    print()
    print("⑤ 导演提示词：壳模式下 brief 不再给字幕排位置")
    shell_spec = _spec(shell="paper-flat", shell_label="第 001 期 · 测试")
    ask = ad._ask(shell_spec, [{"id": "s01", "text": "一句话"}],
                  user_request="做一条讲火的片子")
    check("整片片壳：paper-flat" in ask, "导演先看到壳这一层")
    check("不要给字幕的位置或字号" in ask, "brief 不再排字幕")
    ask_plain = ad._ask(_spec(), [{"id": "s01", "text": "一句话"}])
    check("片壳" not in ask_plain, "没选壳时不多这一段（老片子提示词逐字不变）")

    print()
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
