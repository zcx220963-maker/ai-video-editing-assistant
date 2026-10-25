# -*- coding: utf-8 -*-
"""评审意见与机器判据对账（T0.5）：**评审说看不见、机器量到 30%，就不许重写**。

为什么要有这个文件（2026-10-24）：

`design_review.review_stills` 是一台看图模型说「这一帧行不行」。它说的每一条红，
今天都直接进 `rewrite_failed`——一次写手调用、一次重渲、一次重评。而其中有些红是
**机器已经量过的**：闸⑲-a 手上有主体件占比、闸⑩ 手上有对比度比值。两边打架时
从前没人管，**评审说了算**。代价是双向的：误判 → 白烧一次写手，还可能把对的画面改坏。

这个文件钉五件事：

1. **冲突时记未判、不重写**（不是「按评审改」，也不是「按机器改」——是「没有定论」）；
2. **没有读数 ≠ 机器不同意**：取不到读数时照旧重写，但要记 `unread`，
   回执不许把「没对过账」写成「对过账、没问题」；
3. **四条规则各判一次**，且**只认评审真实说过的用词**（「扁平横条」「同明度」
   「看不见主体」——第一版按「太空」写，命中 0）；
4. **不相关的意见原样放行**：对账只处理它认识的那四类，别的一句话都不许改；
5. **接线顺序**：对账必须排在「升申报 / 补构图 / 喂教训 / 重写」之前——
   一条被拦下的意见如果先改了 brief，副作用已经落下了。

跑法：`python tests/test_review_reconcile.py`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import review_reconcile as rr   # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _failed(sid: str, why: str, fix: str = "重画") -> dict:
    return {"shot": sid, "why": why, "fix": fix,
            "scores": {"craft": 2, "composition": 2, "light": 3,
                       "typography": 3, "not_ppt": 2}}


def _scanned(sid: str, *, share: float, subjects: int, text: float = 0.2) -> dict:
    return {sid: {"shot": sid, "status": "scanned",
                  "scanned": {"subjects": subjects, "subject_share": share,
                              "text_share": text},
                  "findings": [], "thresholds": {}, "reason": None}}


def _contrast(sid: str, ratio: float) -> list[dict]:
    return [{"id": sid, "ratio": ratio, "inks": ["#111111"], "surfaces": ["#ffffff"]}]


SHOTS = [{"id": "s05", "text": "这一镜讲的是钟摆", "code": "<svg><path d='M0 0'/></svg>"}]


def main() -> int:
    print("== ① 评审说看不见，而闸⑲-a 量到主体在 ==")
    got = rr.reconcile([_failed("s05", "看不见主体，画面一片空白")],
                       shots=SHOTS, scanned=_scanned("s05", share=0.30, subjects=3))
    check(len(got["unverified"]) == 1, f"记进 unverified（实得 {got['unverified']}）")
    check(got["kept"] == [], "**不进重写清单**（这是这条的全部意义）")
    check(got["notes"] and "冲突" in got["notes"][0],
          f"回执写明「评审与机器读数冲突」（实得 {got['notes'][:1]}）")
    check("30%" in (got["notes"][0] if got["notes"] else ""),
          "回执带机器读数（不是只说一句「有冲突」）")

    print("\n== ② 同一句话，但机器量到主体确实很小 → 照旧重写 ==")
    got = rr.reconcile([_failed("s05", "看不见主体，画面一片空白")],
                       shots=SHOTS, scanned=_scanned("s05", share=0.05, subjects=1))
    check(len(got["kept"]) == 1 and got["unverified"] == [],
          "主体只占 5% → 评审说对了，进重写清单")
    check(got["unread"] == [], "有读数就不算 unread（这是「对过账且一致」）")

    print("\n== ③ 没有读数 → 照旧重写，但记 unread ==")
    got = rr.reconcile([_failed("s05", "看不见主体")], shots=SHOTS, scanned={})
    check(len(got["kept"]) == 1, "没读数时照旧重写（不拿猜的对账）")
    check(len(got["unread"]) == 1 and got["unread"][0]["rule"] == "subject_missing",
          f"记一条 unread（实得 {got['unread']}）")
    # 版面行在、但这一镜没扫：同样是「没量过」，不许当成「量过且干净」
    got = rr.reconcile([_failed("s05", "看不见主体")], shots=SHOTS,
                       scanned={"s05": {"status": "missing", "scanned": None}})
    check(len(got["unread"]) == 1, "status=missing 的版面行也算没量过")

    print("\n== ④ 同明度 / 糊在一起 → 对比度够就降级 ==")
    got = rr.reconcile([_failed("s05", "内部与背景同明度，糊在一起")],
                       shots=SHOTS, contrast=_contrast("s05", 4.8))
    check(len(got["downgraded"]) == 1 and got["kept"] == [],
          "对比度 4.8:1（线 3:1）→ 判评审误判、降级")
    got = rr.reconcile([_failed("s05", "内部与背景同明度")],
                       shots=SHOTS, contrast=_contrast("s05", 2.0))
    check(len(got["kept"]) == 1, "对比度 2.0:1 → 评审说对了，照旧重写")
    got = rr.reconcile([_failed("s05", "一团模糊的灰蓝圆")],
                       shots=SHOTS, contrast=_contrast("s05", 5.0))
    check(len(got["downgraded"]) == 1, "「模糊」这类真实用词也命中 low_contrast")

    print("\n== ⑤ 只有一块 / 静止 → 机器读数说话 ==")
    got = rr.reconcile([_failed("s05", "并排的扁平横条，像表格行")],
                       shots=SHOTS, fills={"s05": 0.72})
    check(len(got["downgraded"]) == 1, "铺满 72%（线 60%）→ 「只有一块」不成立")
    got = rr.reconcile([_failed("s05", "只有一块色块")], shots=SHOTS, fills={"s05": 0.30})
    check(len(got["kept"]) == 1, "铺满 30% → 评审说对了")
    got = rr.reconcile([_failed("s05", "只有一块色块")], shots=SHOTS)
    check(len(got["unread"]) == 1, "没给铺满度 → unread，不假装对过账")

    got = rr.reconcile([_failed("s05", "全程一动不动")], shots=SHOTS,
                       motion={"s05": {"events": [{"start_sec": 1.0, "peak_diff": 8.4}]}})
    check(len(got["downgraded"]) == 1, "帧差峰值 8.4（地板线 1.5）→ 「静止」不成立")
    got = rr.reconcile([_failed("s05", "没有先后")], shots=SHOTS,
                       motion={"s05": {"events": []}})
    check(len(got["unread"]) == 1, "动效账里没有峰值 → unread（不是「没动」）")

    print("\n== ⑥ 不认识的意见原样放行 ==")
    item = _failed("s05", "配色偏冷，与整片色温弧线不符", "把主色往暖橙挪")
    got = rr.reconcile([item], shots=SHOTS, scanned=_scanned("s05", share=0.5, subjects=4))
    check(len(got["kept"]) == 1 and got["kept"][0]["fix"] == item["fix"],
          "不在这四类里 → 原样进重写（一个字都不改）")
    check(got["unverified"] == [] and got["downgraded"] == [],
          "也不许顺手记成冲突")

    print("\n== ⑦ 汇总口径 ==")
    got = rr.reconcile(
        [_failed("s01", "看不见主体"), _failed("s02", "同明度"), _failed("s03", "只有一块"),
         _failed("s04", "配色偏冷")],
        shots=[{"id": f"s0{i}"} for i in (1, 2, 3, 4)],
        scanned=_scanned("s01", share=0.4, subjects=2),
        contrast=_contrast("s02", 6.0), fills={"s03": 0.9})
    note = rr.summary_note(got)
    check("1 条评审意见与机器读数冲突" in note, f"汇总说清冲突条数（实得 {note!r}）")
    check("2 条经机器读数判定为评审误判" in note, "汇总说清误判条数")
    check(len(got["kept"]) == 1 and got["kept"][0]["shot"] == "s04", "只有 s04 要重写")

    print("\n== ⑧ 接线顺序：对账排在所有「按意见改」的动作之前 ==")
    src = (Path(__file__).resolve().parents[1]
           / "storyline_server" / "nodes" / "motion_plan.py").read_text("utf-8")
    order = [src.find(x) for x in
             ("review_reconcile.reconcile(", "_bump_channel_by_review(spec, fresh)",
              "_bump_density_by_review(spec, fresh)", "design_review.rewrite_failed(")]
    check(all(i >= 0 for i in order), f"四处都在（{order}）")
    check(order == sorted(order),
          f"顺序正确：对账 → 升申报 → 补构图 → 重写（实得 {order}）")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
