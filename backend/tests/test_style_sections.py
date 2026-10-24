"""风格包正文的分段与渐进式加载（不联网）：索引 / 单取 / 逐镜注入 / 三处读者口径。

为什么要有这份：渐进式加载靠一条约定——风格包正文里 `<!-- section: name -->`
划出的块，索引常驻系统提示，正文只随「这一镜点了名」给写手。这条约定跨三个读者
（美术导演选、写手画、评审判），任何一处退化成「整份正文照发」都不会报错、
只会在成本与提示词长度上慢慢变坏，且模型会开始照着正文一步步写死。

跑：  python tests/test_style_sections.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import (art_director, design_review, shot_writer,
                                     style_sections)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _fails += 1
        print(f"  FAIL {label}")


BODY = """风格总述：整片都成立的那一段。

<!-- section: light -->
# 光

主光在画面里有出处。

<!-- /section -->

<!-- section: smoke -->
# 烟

烟要接在一个东西上。

<!-- /section -->
"""

PACK = {"name": "style_demo", "display": "演示风格", "body": BODY,
        "manifest": {"version": 1, "progressive": True}}


def spec_with(progressive: bool = True) -> dict:
    pack = dict(PACK)
    pack["manifest"] = {"version": 1, "progressive": progressive}
    return {"width": 1920, "height": 1080, "fps": 30, "aspect": "16:9",
            "style": "演示风格", "style_pack": pack}


def main() -> None:
    print("① 分段解析")
    names = [n for n, _ in style_sections.pieces(BODY)]
    check(names[:1] == [None], "标记之前的正文是默认集")
    idx = style_sections.index(BODY)
    check([n for n, _, _ in idx] == ["light", "smoke"], f"索引按原序只列有名块（{idx}）")
    check(idx[0][1] == "光", "索引第一句去掉标题符号")
    check(style_sections.default_text(BODY).startswith("风格总述"), "默认集从总述开始")
    check("主光" not in style_sections.default_text(BODY), "默认集不含能力块正文")
    check(style_sections.section_text(BODY, "smoke").startswith("# 烟"), "按名取块")
    check(style_sections.section_text(BODY, "nope") == "", "没有的块回空串、不抛")
    check(style_sections.has_sections(BODY) is True, "has_sections 认得出分段")

    print("② 写手：索引常驻、正文按镜给")
    spec = spec_with()
    film = shot_writer._film_block(spec)
    check("`light`" in film and "`smoke`" in film, "系统提示里有能力索引")
    check("主光在画面里有出处" not in film, "系统提示里**没有**能力块正文")
    check("风格总述" in film, "默认集在系统提示里")
    block = shot_writer._caps_block({"id": "s01", "caps": ["smoke"]}, spec)
    check("烟要接在一个东西上" in block, "点名的那一件，正文进来了")
    check("主光" not in block, "没点名的那一件，正文不进来")
    check("这套风格里没有的能力" in shot_writer._caps_block({"caps": ["nope"]}, spec),
          "认不出的名字给一句宽松提示（不拦）")
    lazy = shot_writer._film_block(spec_with(False))
    check("主光在画面里有出处" in lazy, "非渐进的老风格包仍整份注入（行为不变）")

    print("③ 写手：重写路径与首写同一份单镜块")
    shot = {"id": "s01", "caps": ["smoke"]}
    check(shot_writer._caps_block(shot, spec) in
          shot_writer._shot_block(shot, index=0, spec=spec),
          "_shot_block 带 spec 时含能力正文")

    print("④ 导演：只拿默认集 + 索引")
    todo = [{"id": "s01", "text": "一句话", "brief": "（没写）"}]
    ask = art_director._ask(spec, todo)
    check("风格总述" in ask, "导演看到默认集")
    check("主光在画面里有出处" not in ask, "导演**不**看能力块正文（否则会照正文写死每一步）")
    check("`light`" in ask and "caps" in ask, "导演拿到能力索引并被要求逐镜点名")
    lazy_ask = art_director._ask(spec_with(False), todo)
    check("主光在画面里有出处" in lazy_ask, "非渐进风格包导演仍看整份正文")

    print("⑤ 评审：只拿默认集 + 这一镜点名的能力")
    review = design_review._ask(text="t", subject="s", art={}, style="演示风格",
                                style_pack=PACK, caps=["light"], brief="b")
    check("主光在画面里有出处" in review, "评审看到这一镜点名的能力")
    check("烟要接在一个东西上" not in review, "评审不看没点名的能力")
    lazy_review = design_review._ask(text="t", subject="s", art={}, style="演示风格",
                                     style_pack={**PACK, "manifest": {"progressive": False}},
                                     caps=["light"], brief="b")
    check("烟要接在一个东西上" in lazy_review, "非渐进风格包评审仍整份注入")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
