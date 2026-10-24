# -*- coding: utf-8 -*-
"""整屏 `<feTurbulence>` 颗粒 → 静态贴图 `<Grain/>`。

2026-10-20 真机：36 镜每一镜都手写了「一块铺满画幅、挂着噪声滤镜的 `<rect>`」。
噪声参数是固定的（`seed="17"`），一帧都没变过，可 Chromium 在逐帧渲染里**每帧重算
一遍**。实测同一镜同一 4 帧：带整屏噪声 39.2s，换成静态贴图 8.3s；真机渲染日志的
`每帧≈807ms` 与这条账对得上。整片 36 镜的大半时间烧在这层颗粒上。

这里钉三件事：
① 认得的形状换成 `<Grain/>`，且把浓度/种子照抄过去（画面上仍是同样的颗粒）；
② 认不出的形状（动态噪声、不是铺满画幅、名字被占）**一个字节都不动**；
③ 幂等：换过一遍的代码再跑一遍不再动它——渲染口那一步会重跑旧 checkpoint。

纯函数，不连 PG、不起容器。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion.code_spec import (  # noqa: E402
    _grain_fix_code, autofix_film_grain,
)

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


FULL = """
import React from "react";
import { AbsoluteFill } from "remotion";

const Shot = () => {
  const { width, height } = useVideoConfig();
  return (
    <AbsoluteFill>
      <svg width={width} height={height} viewBox="0 0 1920 1080">
        <defs>
          <filter id="grainF" x="0" y="0" width="100%" height="100%">
            <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2"
              seed="17" stitchTiles="stitch" />
          </filter>
        </defs>
        <rect x="0" y="0" width={width} height={height}
          filter="url(#grainF)" opacity={0.5} />
        <circle cx="100" cy="100" r="40" fill="#333" />
      </svg>
    </AbsoluteFill>
  );
};
"""


def main() -> int:
    print("\n=== ① 认得的那一种：换成 <Grain/>，浓度与种子照抄 ===")
    code, n = _grain_fix_code(FULL)
    check(n == 1, f"换掉了一处（{n}）")
    check("feTurbulence" not in code, "原来那个噪声滤镜没了")
    check("filter=\"url(#grainF)\"" not in code, "引用它的 rect 也没了")
    check('<Grain opacity={0.5} seed={17} blend="normal" />' in code,
          "浓度 0.5、种子 17 原样带过去，混合模式是 normal（旧写法就是原样叠上）")
    check('<circle cx="100" cy="100" r="40"' in code, "别的图形一个都没动")
    check(code.count("<Grain") == 1, "只插了一个")
    check('import { Grain } from "@dg/lib/brush";' in code, "补了 import")
    svg_end = code.find("</svg>")
    check(svg_end >= 0 and code.find("<Grain") > svg_end,
          "`<Grain/>` 放在 </svg> 之后（它是 AbsoluteFill，不能塞进 svg）")

    print("\n=== ② 幂等：渲染口会重跑旧 checkpoint，不能再动一次 ===")
    code2, n2 = _grain_fix_code(code)
    check(n2 == 0 and code2 == code, "第二遍一个字节都没变")

    print("\n=== ③ 认不出的形状一个字节都不动 ===")
    dynamic = FULL.replace('<feTurbulence type="fractalNoise"',
                           '<feTurbulence baseFrequency={freq}')
    got, k = _grain_fix_code(dynamic)
    check(k == 0 and got == dynamic, "噪声参数是活的（{freq}）：那是模型在刻意动的纹理，不动")

    small = FULL.replace("width={width} height={height}\n          filter",
                         "width={width * 0.2} height={height * 0.2}\n          filter")
    got, k = _grain_fix_code(small)
    check(k == 0 and got == small, "不是铺满画幅的 rect：不动")

    named = FULL + "\nconst Grain = () => null;\n"
    got, k = _grain_fix_code(named)
    check(k == 0 and got == named, "代码里自己有个 Grain：再加 import 就是重复声明，不动")

    plain = "const Shot = () => <div />;"
    got, k = _grain_fix_code(plain)
    check(k == 0 and got == plain, "压根没有噪声的代码：不动")

    print("\n=== ④ 已经有 brush import 时把名字并进去，不写第二条 ===")
    with_brush = FULL.replace(
        'import { AbsoluteFill } from "remotion";',
        'import { AbsoluteFill } from "remotion";\n'
        'import { Vignette } from "@dg/lib/brush";')
    got, k = _grain_fix_code(with_brush)
    check(k == 1, "照样换掉")
    check(got.count("@dg/lib/brush") == 1, "brush 只有一条 import")
    check("Vignette, Grain" in got, "名字并进原来那条")

    print("\n=== ⑤ 整份 spec：只报真换过的镜，没命中的镜字节不变 ===")
    spec = {"shots": [
        {"id": "s01", "code": FULL},
        {"id": "s02", "code": plain},
        {"id": "s03", "brief": "只有 brief 的镜"},
    ]}
    out, notes = autofix_film_grain(spec)
    check(len(notes) == 1 and "s01" in notes[0], f"回执点名只换了 s01：{notes[:1]}")
    check(out["shots"][1] is spec["shots"][1], "没命中的镜原对象未变")
    check(out["shots"][2] is spec["shots"][2], "没代码的镜原对象未变")
    check(out["shots"][0]["code"] != FULL, "命中的镜代码换了")
    check(spec["shots"][0]["code"] == FULL, "传入的 spec 没被就地改")

    print(f"\n{'ALL PASSED' if not FAILS else f'{FAILS} FAILED'} ({CHECKS} checks)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
