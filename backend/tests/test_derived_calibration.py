# -*- coding: utf-8 -*-
"""闸⑫ 的阈值 `DERIVED_MIN_SIM=0.61` 必须**标定得出来**（Q7），这一份盯着这件事。

Q7 的原话是：「已由 `scripts/calibrate_derived_sim.py` 标定（非占位）。**若标定脚本
无法在容器内复现该数，闸⑫ 视为未完成**」。所以判据不是「脚本跑得动」，而是三条：

1. **复现得住**。跑一遍标定，按脚本自己写的规则选出来的阈值，必须正好等于
   `code_spec.DERIVED_MIN_SIM`。改语料、改 `canonical_shape` 的正则、改
   `_SHAPE_MIN_TOKENS` 却没同批改阈值，这里就红——这正是「写死的数变成化石」那一刻。
2. **规则真的是规则**。0.61 必须是「满足正样本召回 ≥95% 且负样本通过 ≤2% 的**最小**
   两位小数格点」，所以它的**前一个**格点 0.60 必须**超线**。只断「0.61 没超线」是不够的
   ——那样把它改成 0.80 也照样绿，而 0.80 会开始误伤真派生的镜。
3. **账要指得出方向**。负样本用的是 demo↔demo（语料里没有「真生成过的镜头代码」），
   脚本必须把这条偏差**写在正文里并指向 §9**——它偏保守（真手搓的代码与任何 demo 的
   相似度只会更低），但那是一条推断，不是实测，不该只活在某次会话的记忆里。

外加两条工程钉：**确定性**（同一语料两次跑出的账逐位相同——种子钉在脚本里，否则
「复现不出来」将无从判断是语料变了还是采样变了），以及**导入无副作用**（`import` 这个
模块不许顺手跑一遍 12403 对的两两比对）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.motion import code_spec as cs          # noqa: E402

_fails = 0
_checks = 0

SCRIPT = _BACKEND / "scripts" / "calibrate_derived_sim.py"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _load_script():
    """按路径把标定脚本当模块装进来（`scripts/` 不是包，没法 import 名字）。"""
    spec = importlib.util.spec_from_file_location("calibrate_derived_sim", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                              # type: ignore[union-attr]
    return mod


def main() -> int:
    cal = _load_script()
    rep = cal.calibrate()

    print("\n[① 阈值复现得住：规则选出来的就是这个数]")
    check(rep["agree"] is True,
          f"标定选的阈值 {rep['chosen_threshold']} == code_spec {cs.DERIVED_MIN_SIM}")
    check(rep["code_spec_derived_min_sim"] == cs.DERIVED_MIN_SIM,
          "账上记的就是 code_spec 里那个值（不是脚本自己抄的一份）")
    check(rep["corpus"]["comparable"] >= 100,
          f"语料够厚才谈得上标定（可比 demo {rep['corpus']['comparable']} 份）")

    print("\n[② 规则真的是规则：0.61 是最小格点]")
    grid = rep["pass_rate_at_grid"]
    line = rep["rule"]["max_negative_pass"]
    check(grid["0.60"] > line,
          f"0.60 超线（{grid['0.60']:.3%} > {line:.0%}）——所以 0.61 才是最小值，"
          "不是随便挑的")
    check(grid["0.61"] <= line, f"0.61 在线上（{grid['0.61']:.3%} ≤ {line:.0%}）")
    check(rep["positive"]["recall_at_threshold"] >= rep["rule"]["min_recall"],
          f"正样本召回 {rep['positive']['recall_at_threshold']:.1%} "
          f"≥ {rep['rule']['min_recall']:.0%}")
    # 规则本身的取值也要钉：那两条线是 U5 行写下的契约（「正样本召回 ≥95% 且负样本
    # 通过 ≤2%」），不是脚本内部可以随手放宽的旋钮。为什么非得钉——**召回那条今天
    # 是非紧的**（正样本全是 1.000，阈值只要 ≤1.0 就满足），所以把 MIN_RECALL 改成 0
    # 不会让任何一个数变红，`agree` 照样是 True。也就是说：不钉这一条，就有一条
    # 「悄悄放宽了标定口径」的路是敞开的，而它不会在任何别的地方留下痕迹。
    check(cal.MIN_RECALL == 0.95 and cal.MAX_NEG_PASS == 0.02,
          f"两条线的取值就是 U5 写的那两条（实得 {cal.MIN_RECALL} / {cal.MAX_NEG_PASS}）")
    check(rep["negative"]["median"] < cs.DERIVED_MIN_SIM < rep["negative"]["max"],
          f"阈值落在负样本分布里（中位 {rep['negative']['median']:.3f} < "
          f"{cs.DERIVED_MIN_SIM} < max {rep['negative']['max']:.3f}）——它是个分位点，"
          "不是「比所有负样本都高」的那种线，这也说明伪造声明仍有约 "
          f"{rep['negative']['pass_at_threshold']:.2%} 的漏网率（登记在案）")

    print("\n[③ 正样本：闸⑫ 对文案/色相/import 的免疫真的成立]")
    check(rep["positive"]["n"] == rep["corpus"]["comparable"],
          f"每一份可比 demo 都做了扰动（{rep['positive']['n']} 对）")
    check(rep["positive"]["min"] == 1.0 and not rep["positive"]["broken"],
          f"全部 1.000（最低 {rep['positive']['min']}）——"
          "换文案/色号/import 掉下 1.0 就意味着闸⑫ 会误伤真派生的镜")

    print("\n[④ 阈值不是写死的：把它挪开，标定自己会说不同意]")
    old = cs.DERIVED_MIN_SIM
    try:
        cs.DERIVED_MIN_SIM = 0.55                             # 格点上确实存在的一个值
        cal.calibrate.cache_clear()
        moved = cal.calibrate()
    finally:
        cs.DERIVED_MIN_SIM = old
        cal.calibrate.cache_clear()
    check(moved["agree"] is False,
          "把 code_spec 的阈值改成 0.55 → 标定报不一致（说明它真在比，不是复述）")
    check(moved["chosen_threshold"] == rep["chosen_threshold"],
          f"而且它自己选出来的还是 {moved['chosen_threshold']}（结论来自语料，不来自常量）")
    # 反过来：把**规则**收紧，选出来的数就必须跟着动。只改常量那一侧（上面两条）挡不住
    # 「`chosen = 0.61` 写死、`agree` 只是拿它对一下常量」这种假标定——那种脚本在语料
    # 变化时会安静地继续报「一致」。规则一紧，写死的那份立刻露馅。
    old_pass = cal.MAX_NEG_PASS
    try:
        cal.MAX_NEG_PASS = 0.0
        cal.calibrate.cache_clear()
        strict = cal.calibrate()
    finally:
        cal.MAX_NEG_PASS = old_pass
        cal.calibrate.cache_clear()
    check(strict["chosen_threshold"] is not None
          and strict["chosen_threshold"] > rep["chosen_threshold"],
          f"把负样本线收到 0% → 选出的阈值上移到 {strict['chosen_threshold']}"
          "（结论真的来自这张分布表）")
    check(cal.SEED == 20261012,
          f"扰动种子钉着（{cal.SEED}）——code_spec 里那段实测数（p95/p98/max）是"
          "种子相关的，改种子要同批改那段话，否则注释里的数就不再是复现得出来的")

    print("\n[⑤ 确定性：同一语料两次跑出的账必须逐位相同]")
    again = cal.calibrate()
    check(json.dumps(again, sort_keys=True) == json.dumps(rep, sort_keys=True),
          "两次结果逐位一致（种子钉在脚本里：否则‘复现不出来’分不清是语料变了还是采样变了）")
    cal.calibrate.cache_clear()
    fresh = cal.calibrate()
    check(fresh["negative"]["median"] == rep["negative"]["median"],
          "清掉缓存重算仍是同一个数")

    print("\n[⑥ 偏差登记在案：负样本是 demo↔demo，且指向 §9]")
    doc = cal.__doc__ or ""
    check("demo↔demo" in doc.replace(" ", ""),
          "脚本正文写明负样本是哪一对（不是含糊的「样本集」）")
    check("§9" in doc, "并指向 §9：这条偏差偏保守是**推断**，要留痕不靠记忆")
    check("偏保守" in doc, "把方向说清：真手搓的代码只会更不像任何 demo，漏网率只会更低")

    print("\n[⑦ 导入无副作用]")
    # 用缓存大小判，不用耗时判：导入时若顺手跑了一遍标定，`lru_cache` 的 currsize
    # 就是 1（耗时判法在慢机器上会左右摇摆，而这件事与快慢无关）。
    p = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, 'scripts');"
                        " import calibrate_derived_sim as m;"
                        " print(m.calibrate.cache_info().currsize)"],
                       cwd=str(_BACKEND), capture_output=True, text=True, timeout=120)
    check(p.stdout.strip() == "0",
          f"import 只定义函数、不跑那 12403 对比对（缓存条目 {p.stdout.strip()!r}）")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
