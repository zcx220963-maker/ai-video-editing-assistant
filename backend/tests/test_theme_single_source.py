# -*- coding: utf-8 -*-
"""主题唯一来源（R4-T9 + R4-T8-1）：色板与三档预设只有一处住址。

判据不是「palettes.json 里有 8 个色板」——那只证明了今天有 8 个。真正要钉住的是
**「加第 9 个时两侧同时看得见」**：只有两侧都不自己留一份名单，单一来源才成立。
所以每一条「绿」都配一条把它变红的办法，写在用例里：

  ① 数得对：`len(palettes()) == 8`，且就是那八个名字，颜色键齐全且是 hex。
     —— 红法：往 palettes.json 塞第 9 个色板（数变 9）或删掉一个（数变 7）。
  ② `preset` 不是色板：同一文件里住着三档预设，`palettes()` 必须把它剔掉。
     —— 红法：把 `palettes()` 里 `k != "preset"` 那半句删掉，①立刻红（数成 9）。
        这条是给①上的保险：预设段是 dict，按形状猜就会混进去。
  ③ Python 侧不留名单：换一个 REMOTION_ROOT，文件里多一个色板，`palettes()` 就得
     多还一个。它读的是文件，不是常量。
     —— 红法：把 `palettes()` 改成返回写死的八键字典，这条立刻红。
  ④ Node 侧不留名单：渲染侧靠 `import palettes from '@dg/themes/palettes.json'`
     （tsconfig 的 `@dg/themes/*` → `src/themes/*`，闸① 白名单放行）拿色板，
     而不是把八个名字抄进 lint-rules / FilmLayout / server。名字一旦被抄进去，
     加第 9 个时那一侧就瞎了。
     —— 红法：在 lint-rules.mjs 里写一行 `const PALETTES = ['vintage-kraft', ...]`。
  ⑤ 三档表与 preset 逐项一致（T8-1 的验收）：SKILL.md 那张表是模型看的，
     palettes.json 的 preset 是机器读的。两份对上才算「语义真的并进去了」，
     而不是「文件删了、表还指着空气」。

跑法：`python tests/test_theme_single_source.py`（容器内 `PYTHONPATH=/app`）。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import json
import os
import re
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs      # noqa: E402

_checks = 0
_fails = 0
_unverified: list[str] = []

COLOR_KEYS = {"page", "surface", "field", "text", "muted", "accent", "border"}
EIGHT = {"vintage-kraft", "deep-ocean", "obsidian-violet", "modern-light",
         "midnight", "solar-pop", "coral-burst", "color-play"}
PRESETS = {"ink-archive", "tech-data", "night-concept"}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _remotion_dir() -> Path | None:
    env = os.environ.get("REMOTION_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    b = _backend_dir()
    for cand in (b / "remotion", b.parent / "remotion"):
        if cand.is_dir():
            return cand
    return None


def _raw() -> dict:
    p = _remotion_dir()
    if p is None:
        return {}
    f = p / "src" / "themes" / "palettes.json"
    if not f.is_file():
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ① 数得对
# ---------------------------------------------------------------------------
def test_eight_palettes() -> None:
    print("\n=== ① len(palettes()) == 8，且就是那八个 ===")
    got = cs.palettes()
    check(len(got) == 8, f"色板数 = 8（实际 {len(got)}：{sorted(got)}）")
    check(set(got) == EIGHT, f"名字集合与冻结的八个一致（差集：{set(got) ^ EIGHT}）")
    shapes = {k: sorted(v) for k, v in got.items() if set(v) != COLOR_KEYS}
    check(not shapes, f"每个色板都有那七个颜色键（形状不同的：{shapes}）")
    bad = [f"{k}.{kk}={vv!r}" for k, v in got.items() for kk, vv in v.items()
           if not (isinstance(vv, str) and _HEX.match(vv))]
    check(not bad, f"每个值都是 6 位 hex（越界：{bad[:3]}）")


# ---------------------------------------------------------------------------
# ② preset 不是色板
# ---------------------------------------------------------------------------
def test_preset_not_counted() -> None:
    print("\n=== ② preset 段住在同一文件里，但不是色板 ===")
    raw = _raw()
    if not raw:
        _unverified.append("② palettes.json 读不到——preset 剔除未验证")
        return
    check("preset" in raw, "raw 文件里有 preset 段（T8-1 并把三档预设并进来）")
    check(set(raw["preset"]) == PRESETS,
          f"preset 里有且只有那三档（实际 {sorted(raw.get('preset', {}))}）")
    check("preset" not in cs.palettes(),
          "palettes() 把 preset 剔掉了——不剔的话闸⑩ 会把 palettes['preset'].text "
          "当成合法颜色写法，八色板的自检也会数成九个")
    # 这一条才是②的真实形状：preset 的**值**也全是 dict，按形状过滤必然漏。
    check(all(isinstance(v, dict) for v in raw.get("preset", {}).values()),
          "preset 的三个值都是 dict（所以只能按名字剔，不能按 isinstance 剔）")


# ---------------------------------------------------------------------------
# ③ Python 侧不留名单
# ---------------------------------------------------------------------------
def test_python_reads_file() -> None:
    print("\n=== ③ Python 侧读文件，不读写死的常量 ===")
    remo = _remotion_dir()
    base = _raw()
    if not base:
        _unverified.append("③ palettes.json 读不到——「换一份文件就多一个色板」未验证")
        return
    ninth = dict(base)
    ninth["probe-ninth"] = {k: "#123456" for k in COLOR_KEYS}
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "src" / "themes"
        d.mkdir(parents=True)
        (d / "palettes.json").write_text(json.dumps(ninth, ensure_ascii=False),
                                         encoding="utf-8")
        old = os.environ.get("REMOTION_ROOT")
        os.environ["REMOTION_ROOT"] = td
        cs.palettes.cache_clear()
        try:
            got = cs.palettes()
        finally:
            cs.palettes.cache_clear()
            if old is None:
                os.environ.pop("REMOTION_ROOT", None)
            else:
                os.environ["REMOTION_ROOT"] = old
        check("probe-ninth" in got and len(got) == 9,
              f"文件里多一个色板，palettes() 就多还一个（拿到 {len(got)} 个）——"
              f"它读的是文件那八个键，不是写死的常量")
    check(len(cs.palettes()) == 8, "回到真目录后又是 8 个（没被探针污染）")


# ---------------------------------------------------------------------------
# ④ Node 侧不留名单
# ---------------------------------------------------------------------------
def _node_theme_files(remo: Path) -> list[Path]:
    names = ["lint-rules.mjs", "lint.mjs", "server.mjs",
             "src/FilmLayout.tsx", "src/tokens.ts"]
    return [remo / n for n in names if (remo / n).is_file()]


def test_node_reads_file() -> None:
    print("\n=== ④ Node 侧也不留名单（靠 import，而不是抄一遍） ===")
    remo = _remotion_dir()
    if remo is None:
        _unverified.append("④ remotion/ 不可达——Node 侧单一来源未验证")
        return
    rules = remo / "lint-rules.mjs"
    ts = remo / "tsconfig.json"
    if not rules.is_file() or not ts.is_file():
        _unverified.append("④ lint-rules.mjs / tsconfig.json 不可达（app 镜像只有子集）")
        return

    text = rules.read_text(encoding="utf-8")
    check("'@dg/themes/palettes.json'" in text,
          "闸① 白名单放行 `@dg/themes/palettes.json`（渲染侧靠 import 拿色板）")
    tstext = ts.read_text(encoding="utf-8")
    check('"@dg/themes/*"' in tstext and "src/themes/*" in tstext,
          "tsconfig 把 `@dg/themes/*` 指到 `src/themes/*`（import 解析得到）")

    # 真判据：八个名字一个都不许在渲染侧被抄成字面量。
    # （`midnight` 在 src/demos 下有的是 LOOKS.midnight —— 那是 demo 自己的夹具视觉系统，
    #   另一套命名空间，与本判据无关；这里只看主题链路的四个文件。）
    leaked: list[str] = []
    for f in _node_theme_files(remo):
        src = f.read_text(encoding="utf-8")
        for name in EIGHT:
            if f'"{name}"' in src or f"'{name}'" in src or f'`{name}`' in src:
                leaked.append(f"{f.relative_to(remo)}:{name}")
    check(not leaked,
          f"渲染侧的 lint/server/FilmLayout/tokens 里没有写死色板名（抄进去的：{leaked}）"
          "—— 抄了的话，加第 9 个色板时那一侧就瞎了")


# ---------------------------------------------------------------------------
# ⑤ 三档表 ↔ preset 逐项一致（T8-1 的验收）
# ---------------------------------------------------------------------------
_ROW = re.compile(r"^\|\s*`([a-z-]+)`\s*\|(.+)\|\s*$")


def test_skill_table_matches_preset() -> None:
    print("\n=== ⑤ SKILL.md 三档表与 palettes.json 的 preset 逐项一致（T8-1） ===")
    raw = _raw()
    remo = _remotion_dir()
    if not raw or remo is None:
        _unverified.append("⑤ palettes.json / remotion 不可达——三档表比对未验证")
        return
    skill = (_backend_dir() / "examples" / "retired" / "motion_explainer" / "SKILL.md")
    if not skill.is_file():
        skill = remo.parent / "backend" / "examples" / "retired" / "motion_explainer" / "SKILL.md"
    if not skill.is_file():
        _unverified.append("⑤ SKILL.md 不可达——三档表比对未验证")
        return

    preset = raw.get("preset", {})
    rows: dict[str, list[str]] = {}
    in_table = False
    for line in skill.read_text(encoding="utf-8").splitlines():
        if line.startswith("| 档 |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            m = _ROW.match(line)
            if m:
                rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]

    check(set(rows) == PRESETS, f"表里三档与 preset 同名（表里：{sorted(rows)}）")
    for name in sorted(PRESETS & set(rows) & set(preset)):
        cols = rows[name]
        # 列序：色板 | texture | 字幕（样式 + 高亮） | 主体来源 | 适用选题
        got_pal = re.findall(r"`([a-z-]+)`", cols[0])
        got_tex = set(re.findall(r"`([a-z_]+)`", cols[1]))
        got_sub = re.findall(r"`([a-z_]+)`", cols[2])
        want = preset[name]
        check(got_pal == want["palettes"],
              f"{name} 色板一致：表={got_pal} preset={want['palettes']}")
        want_tex = {want["texture"]} | ({want["texture_alt"]} if want["texture_alt"] else set())
        check(got_tex == want_tex,
              f"{name} texture 一致：表={sorted(got_tex)} preset={sorted(want_tex)}")
        # 字幕那一格是两个值（字幕样式 + 高亮样式），**次序**也判：写反了表上读起来
        # 就是另一件事（「纸条底 + 撕纸高亮」与「撕纸底 + 纸条高亮」，后者根本不存在）。
        want_sub = [want["caption_style"], want["highlight_style"]]
        check(got_sub == want_sub,
              f"{name} 字幕两维一致：表={got_sub} preset={want_sub}")
    check("styles.json" not in skill.read_text(encoding="utf-8"),
          "SKILL.md 里不再指向已删的 styles.json（该指 palettes.json 的 preset 段）")


def main() -> int:
    cs.palettes.cache_clear()
    test_eight_palettes()
    test_preset_not_counted()
    test_python_reads_file()
    test_node_reads_file()
    test_skill_table_matches_preset()
    print()
    if _unverified:
        print("未验证清单（跳过 ≠ 通过）：")
        for s in _unverified:
            print(f"  · {s}")
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
