# -*- coding: utf-8 -*-
"""内部组件目录：写手能看见哪些件、每个件收什么参数（从源码现读，不手写）。

用户 2026-10-22 的原话：「导入别人的 skill，只保留一个自动入口，就能调用我们内部
所有的组件库」。技能正文里那几张手写的表只盖住了 `@dg/lib/draw/*`；笔刷、场景层、
走路件、面板件**没有成文的 props 清单**，写手只能猜，而猜错 prop 名是静默的
（组件落回缺省值，画面不对、闸不红、渲染不报）。

这一支钉的是「目录真的是源码的投影」：
① 关键签名在（含 `*Props` 里的字段、`export type` 联合的取值、`outfit` 这类对象别名展开）；
② 整段有字节上限，超了要明说截了多少（静默截断会让写手以为「库里就这些」）；
③ 源码读不到时整段不发（返回空串），写镜照常进行；
④ 整片设定里真的有这一段，且带模板时写明「模板负责的别用通用件重画」。

运行：  python tests/test_component_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs  # noqa: E402
from storyline_server.motion import components, shot_writer as sw  # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _has(text: str, *needles: str) -> bool:
    return all(n in text for n in needles)


def spec_with(shot: dict, **over) -> dict:
    body = {"aspect": "landscape", "fps": 30, "narration": False,
            "palette": "deep-ocean", "style": "克制的杂志编辑风", "shots": [shot]}
    body.update(over)
    return cs.validate_code_spec(body)[0]


def main() -> None:
    cat = components.component_catalog()
    if not cat:
        # 本机没有 remotion 源码树（只拷了后端）时这一支无从判——如实记「未验证」，
        # 不装成通过：目录是**源码的投影**，没有源码就没有可对账的东西。
        print("· 跳过：本机找不到 remotion/src/lib（容器里由 Dockerfile COPY 进来）")
        print("未验证（源码树不在）")
        return

    print(f"① 关键签名从源码读出来了（{len(cat.encode())} B）")
    check("=== 内部组件库" in cat, "有抬头，写手知道这一段是什么")
    check("成片设备像素" in cat, "写明单位（与模板的「画面比例 0–1」区分开）")
    check(_has(cat, "`<BrushStroke/>`", "points: Array<[number, number]>",
               "brush?: pencil|marker|watercolor"),
          "BrushStroke 的字段与笔性取值都在")
    check(_has(cat, "`<WalkingFigure/>`", "render?: silhouette|flat|lit|line",
               "footY?: number"),
          "WalkingFigure 的字段在（此前没有成文清单）")
    check("outfit?: {build,garment," in cat,
          "outfit 这类对象别名被展开成字段名（不然等于没说）")
    check(_has(cat, "`<Machine/>`", "kind?: steam|press|arm|turbine"),
          "简笔画零件的枚举展开成真实取值（`MachineKind` → steam|press|…）")
    check(_has(cat, "`<Scene/>`", "far?: React.ReactNode", "horizon?: number"),
          "场景层的具名插槽在（前/中/后三层）")
    check("blend?: normal|multiply|overlay|soft-light|screen" in cat,
          "字面量联合去掉引号，与别名那侧同一种写法")

    print("\n② 字节上限：超了按模块顺序截，并且明说截了")
    check(len(cat.encode()) <= components.CATALOG_MAX_BYTES + 400,
          f"整段 {len(cat.encode())} B ≤ 上限 {components.CATALOG_MAX_BYTES} B + 抬头")
    old = components.CATALOG_MAX_BYTES
    try:
        components.CATALOG_MAX_BYTES = 600
        components.clear_cache()
        small = components.component_catalog()
    finally:
        components.CATALOG_MAX_BYTES = old
        components.clear_cache()
    check("没有列出" in small, "截断时明说还有模块没列")
    check(len(small.encode()) < len(cat.encode()), "上限小了，目录真的变短")

    print("\n③ 源码读不到：整段不发，不抛")
    real = cs._master_file
    try:
        cs._master_file = lambda rel: None       # noqa: ARG005 - 模拟「源码不在镜像里」
        components.clear_cache()
        check(components.component_catalog() == "", "返回空串（写镜照常进行）")
        film = sw._film_block(spec_with({"id": "s01", "text": "句子", "brief": "一句话",
                                         "min_duration_sec": 6}))
        check("=== 内部组件库" not in film, "整片设定里也不会出现一段空目录")
    finally:
        cs._master_file = real
        components.clear_cache()

    print("\n④ 整片设定里真的有这一段；模板通道 2026-10-24 已整条删除")
    spec = spec_with({"id": "s01", "text": "蒸汽机让英国跑了起来",
                      "brief": "左侧蒸汽机冒烟，右侧工人扛煤", "min_duration_sec": 8})
    film = sw._film_block(spec)
    check("=== 内部组件库" in film and "`<BrushStroke/>`" in film,
          "写手的整片设定里带着签名目录")
    check(film.index("=== 内部组件库") < film.index("=== 技能正文 ==="),
          "目录排在技能正文之前（正文里的示例是最后读到的）")
    check("本片必用模板" not in film,
          "没有「必用模板」那句话——模板通道已删，这句话连同它的判据一起不在了")
    # 2026-10-24：这三条原来测的是**模板通道**（`style_pack` + `draw_mode: template`
    # → 「本片必用模板…通用件只补模板没覆盖的部分」）。同一天风格包整条通道被删掉
    # （`_STYLE_TEMPLATES` / `TEMPLATES_ENABLED` / `template_required` 全删，
    # `spec["style_pack"]` → `spec["skill_contract"]`），于是「必用」那句再也不出现。
    # 删掉的功能不该留一条永远绿的断言，换成钉住**今天这个世界**的两件事：
    #   ① 老字段 `style_pack` 不再被接受（写了就是契约错，不是静默忽略）；
    #   ② 新字段 `skill_contract` 是唯一入口，`kind=style` 的技能照样拒绝。
    _legacy = {**spec, "style_pack": {"name": "style_silhouette", "display": "剪影年轮",
                                     "manifest": {"template": "silhouette-history"},
                                     "template": {"name": "silhouette-history"}}}
    check(sw._film_block(_legacy) == film,
          "老字段 style_pack 今天被**静默忽略**（契约不拒未知键）：写了与不写，"
          "整片设定逐字节相同——模板通道删干净了的证据")

    print(f"\n通过 {_checks - _fails}/{_checks}" +
          ("" if _fails == 0 else f"，失败 {_fails}"))
    raise SystemExit(1 if _fails else 0)


if __name__ == "__main__":
    main()
