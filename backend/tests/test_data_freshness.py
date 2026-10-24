# -*- coding: utf-8 -*-
"""数据时效的离线用例：只报不拦、别误伤历史叙述、回执上真有这一栏。

真机事故（2026-10-18）：一条 2026 年出的片子里写着「2024 年，中国短视频用户人均每天刷
156 分钟」——数字对得上当年那期报告，但已经不是最新一期。出处闸只问「这一页在本会话里
真打开过吗」，不问「有没有更新一版」；模型凭记忆写下的旧数因此一路过闸、上了屏。

判据住在 ``motion_plan.freshness_warnings``，这里钉四件事：

1. **只认「近几年的年份 + 带单位的数量」**。讲历史的片子（1949 年、5.4 亿）与「2024 年
   发布了某物」这种事件叙述都不该被它打扰——它是提醒，不是内容审查。
2. **填了出处也照样提醒**。出处闸证的是「查过」，不是「查的是最新一期」——两件事不能
   互相顶替，这正是本次事故的漏洞。
3. **有年份没出处**单独提醒一次，理由不同、措辞也不同。
4. **回执里真有这一栏**（不拦入库、不打断出片，模型读回执自己决定换不换）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs                    # noqa: E402
from storyline_server.nodes import motion_plan as mp                  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _spec_with(text: str, *, source: dict | None = None) -> dict:
    """一镜的最小合法 spec（够 ``plan_payload`` 跑完全部整片判据）。"""
    shot = {
        "id": "s01",
        "text": text,
        "highlight": [],
        "brief": "一只旧钟摆在画面中间，字幕排在底带。",
        "code": ('<AbsoluteFill style={{background: "#0b1620"}}>'
                 '<svg viewBox="0 0 1920 1080"><circle cx="960" cy="480" r="120" '
                 'fill="none" stroke="#c9b98d" strokeWidth="8"/></svg>'
                 "</AbsoluteFill>"),
        "min_duration_sec": 5.0,
        "subject": "旧钟",
        "subject_channel": "vector",
    }
    if source is not None:
        shot["source"] = source
    raw = {"title": "夹具片", "aspect": "landscape", "fps": 30, "narration": False,
           "caption_style": "on_picture", "texture": "none", "shots": [shot]}
    spec, errs = cs.validate_code_spec(raw)
    assert errs == [], f"夹具本身没过契约：{errs}"
    return spec


def main() -> int:
    now = datetime(2026, 10, 18)

    print("\n=== 一、真机那一句：2024 年的统计值，2026 年还在用 ===")
    real = [{"id": "s12",
             "text": "2024年，中国短视频用户人均每天刷156分钟，约2.6小时。",
             "source": {"标题": "《中国网络视听发展研究报告（2025）》",
                        "链接": "https://example.org/report"}}]
    warns = mp.freshness_warnings(real, now=now)
    check(len(warns) == 1 and warns[0].startswith("s12"),
          f"点名镜号、只出一条：{warns}")
    check("2024" in warns[0] and "2026" in warns[0],
          f"把数据期与当前年份并排摆出来：{warns[0][:60]}…")
    check("最新一期" in warns[0] and "数据期" in warns[0],
          f"说的是「查最新一期 / 写明数据期」，不是「打回重写」：{warns[0][-40:]}")

    print("\n=== 二、填了出处也照样提醒（出处证查过，不证是最新一期）===")
    no_src = [{**real[0], "source": {}}]
    warns2 = mp.freshness_warnings(no_src, now=now)
    check(len(warns2) == 1 and "2024" in warns2[0],
          f"没填出处时仍是同一条时效提醒（不是改用「没出处」那一句）：{warns2}")
    check("没填 source" not in warns2[0],
          "旧数据期优先——先把「换最新一期」说清楚，别让人以为填个出处就完事")

    print("\n=== 三、不误伤：去年的、历史的、事件叙述的 ===")
    last_year = [{"id": "s01", "text": "2025年，人均每天刷156分钟。",
                  "source": {"标题": "某报告", "链接": "https://example.org/r"}}]
    check(mp.freshness_warnings(last_year, now=now) == [],
          "落后一年以内不提醒——年度报告的统计期本来就滞后一年")
    history = [{"id": "s02", "text": "1949年，全国人口约5.4亿。"}]
    check(mp.freshness_warnings(history, now=now) == [],
          "历史年份 + 数量不提醒（讲历史不是「旧数据」）")
    event = [{"id": "s03", "text": "2024年，OpenAI 发布了 GPT-4o。"}]
    check(mp.freshness_warnings(event, now=now) == [],
          "没有带单位的数量＝事件叙述，不提醒")
    plain_year = [{"id": "s04", "text": "2024年很关键。"}]
    check(mp.freshness_warnings(plain_year, now=now) == [],
          "只有年份没有统计值，不提醒")

    print("\n=== 四、边界：同一年份在不同「现在」下判定不同 ===")
    at_2025 = mp.freshness_warnings(real, now=datetime(2025, 3, 1))
    check(at_2025 == [],
          f"2025 年看 2024 年的数只落后一年，不提醒（实得 {at_2025}）")
    at_2027 = mp.freshness_warnings(real, now=datetime(2027, 1, 1))
    check(len(at_2027) == 1 and "2024" in at_2027[0],
          "2027 年看同一个数仍提醒")

    print("\n=== 五、近年的数字没填出处：另一种提醒 ===")
    fresh_no_src = [{"id": "s05", "text": "2025年，人均每天刷156分钟。"}]
    warns5 = mp.freshness_warnings(fresh_no_src, now=now)
    check(len(warns5) == 1 and "没填 source" in warns5[0],
          f"数据期不老、但没有出处时，提醒补出处（实得 {warns5}）")
    fresh_src = [{**fresh_no_src[0],
                  "source": {"标题": "某报告", "链接": "https://example.org/r"}}]
    check(mp.freshness_warnings(fresh_src, now=now) == [],
          "数据期不老、出处也填了，就不再提醒")

    print("\n=== 六、回执里真有这一栏（只报不拦、照常入库）===")
    spec = _spec_with("2024年，中国短视频用户人均每天刷156分钟。",
                      source={"标题": "某报告", "链接": "https://example.org/r"})
    payload = mp.plan_payload(spec, target=5)
    check("freshness_warnings" in payload,
          "plan_motion 回执带 freshness_warnings")
    check(isinstance(payload["freshness_warnings"], list)
          and payload["freshness_warnings"]
          and payload["freshness_warnings"][0].startswith("s01"),
          f"这一栏真按镜号点名（实得 {payload['freshness_warnings']}）")
    check(payload.get("target_warnings") is not None and payload["shot_count"] == 1,
          "分镜照常入库、照常出账——提醒不进任何打回清单")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
