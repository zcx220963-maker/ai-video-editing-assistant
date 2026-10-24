# -*- coding: utf-8 -*-
"""调参台的预览一步里**能离线验的那一半**：整片那几栏的取值、校验与拒收。

为什么只测这些：真正渲三帧要起 Remotion 容器，那是真机验证的事；而「台子上能把
一个不存在的色板放过」这类错误在**提交前**就看得见，代价只是三个假字典。

钉四件事：
① 控件清单只发命中表声明的四栏，当前值取**这一版分镜**的（不是控件的缺省值）；
② 候选只在控件声明里有的时候才发——没发时不编一个下拉；
③ 值不在候选里就拒，拒的理由要说清能选哪些；
④ 改在副本上：原 spec 一个字节不动。

纯函数，不连 PG、不起容器。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework import motion_preview as mp  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def ck(name: str, ok: bool) -> None:
    CHECKS.append((name, bool(ok)))


CONTROLS = {
    "palette": {
        "type": "string", "enum": ["none", "vintage-kraft", "deep-ocean"],
        "default": "none", "value_labels": {"none": "默认", "deep-ocean": "深海蓝"},
    },
    "caption_style": {"type": "string", "enum": ["outlined", "on_picture"],
                      "default": "outlined"},
    "highlight_style": {"type": "string", "enum": ["plain", "color"], "default": "plain"},
    "texture": {"type": "string", "enum": ["none", "film", "tv"], "default": "none"},
    "fps": {"type": "integer", "enum": [15, 30], "default": 30},
}

SPEC = {
    "fps": 15, "palette": "vintage-kraft", "caption_style": "outlined",
    "highlight_style": "plain",
    "shots": [
        {"id": "s01", "text": "第一镜", "code": "x",
         "fields": [{"name": "r", "type": "number", "value": 10, "min": 1, "max": 20}]},
        {"id": "s02", "text": "第二镜", "code": "y", "fields": []},
    ],
}


def main() -> int:
    # ① 只发四栏；当前值取分镜的；没写的栏回落到控件缺省
    rows = mp.film_controls(SPEC, CONTROLS)
    ck("发四栏", [r["key"] for r in rows]
       == list(mp.CONSOLE_FILM_KNOBS))
    by = {r["key"]: r for r in rows}
    ck("配色取这一版的值", by["palette"]["value"] == "vintage-kraft")
    ck("质感没写就回 none", by["texture"]["value"] == "none")
    ck("带候选", by["palette"]["options"][0] == "none")
    ck("带给值看的名字", by["palette"]["labels"].get("deep-ocean") == "深海蓝")
    ck("fps 不是台上的栏", "fps" not in by)

    # ② 没有控件声明时不编候选
    bare = {r["key"]: r for r in mp.film_controls(SPEC, None)}
    ck("无声明仍回当前值", bare["palette"]["value"] == "vintage-kraft")
    ck("无声明不发候选", not bare["palette"].get("options"))

    # ③ 合法值生效；不在候选里、不在白名单里都拒
    spec2, applied, rejected = mp.apply_film_overrides(
        SPEC, {"palette": "deep-ocean", "texture": "film"}, CONTROLS)
    ck("合法值生效", applied == {"palette": "deep-ocean", "texture": "film"})
    ck("改在副本上", spec2["palette"] == "deep-ocean")
    ck("原 spec 不动", SPEC["palette"] == "vintage-kraft" and "texture" not in SPEC)
    ck("没有拒收", not rejected)

    _, _, bad = mp.apply_film_overrides(
        SPEC, {"palette": "sepia", "fps": 24}, CONTROLS)
    names = {r["name"] for r in bad}
    ck("不在候选里的拒", "palette" in names)
    ck("台上没有的栏也拒", "fps" in names)
    palette_why = next(r["why"] for r in bad if r["name"] == "palette")
    ck("拒的理由列出候选", "deep-ocean" in palette_why)

    # ④ 没有控件声明时放行（真正的判据在落补丁那一趟）
    _, ok2, bad2 = mp.apply_film_overrides(SPEC, {"palette": "sepia"}, None)
    ck("无声明时放行交给补丁口", ok2 == {"palette": "sepia"} and not bad2)

    # ⑤ 逐镜字段：声明的放、越界的拒、没声明的拒
    idx, shot = mp.find_shot(SPEC, "s01")
    ck("找到镜号", idx == 0 and shot["id"] == "s01")
    shot2, ok3, bad3 = mp.apply_overrides(shot, {"r": 12, "nope": 1})
    ck("声明字段放行", ok3 == {"r": 12})
    ck("没声明的字段拒", any(r["name"] == "nope" for r in bad3))
    _, _, bad4 = mp.apply_overrides(shot, {"r": 99})
    ck("越界拒", any(r["name"] == "r" for r in bad4))
    ck("改在副本上（逐镜）", shot["fields"][0]["value"] == 10)

    # ⑥ 没旋钮的镜：清单原样交出来（界面据此说「这一镜没有旋钮」）
    rows2 = mp.shot_list(SPEC)
    ck("逐镜清单两镜", [r["id"] for r in rows2] == ["s01", "s02"])
    ck("第二镜没有旋钮", rows2[1]["fields"] == [])
    ck("第一镜的旋钮带范围", rows2[0]["fields"][0].get("max") == 20)

    try:
        mp.find_shot(SPEC, "s99")
    except mp.PreviewError as exc:
        ck("找不到镜时点名现有镜号", "s01" in str(exc))
    else:
        ck("找不到镜时点名现有镜号", False)

    failed = [n for n, ok in CHECKS if not ok]
    for name, ok in CHECKS:
        print(("PASS " if ok else "FAIL ") + name)
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
