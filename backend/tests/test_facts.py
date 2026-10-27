# -*- coding: utf-8 -*-
"""出处账：片子里的数字，能不能说清它从哪来。

为什么要单独测：这是**唯一一条像素判据碰不到的质量线**。质检能看出"这一镜太暗"，
但 `47% 的青少年……` 里的 47 是不是真的，任何画面分析都测不出来。
所以判据只能建在文本上，也就必须被测试钉住。

四条：
① `numbers_in` 认数字、不认中文数字（"四十年"是叙述，不需要出处）
② `normalize` 把模型给的各种烂形状收成同一种
③ `audit_sources` 只在**出处的 claim 里也出现了这个数字**时算覆盖
④ 缺出处**不拦片**，只写进 note / missing

运行：  python tests/test_facts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_storyline.simple import facts  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _FAILS += 1
        print(f"  XX   {label}")


def main() -> int:
    print("\n① numbers_in：认阿拉伯数字，不认中文数字")
    check(facts.numbers_in("47% 的青少年每天刷 3 小时") == ["47", "3"],
          f"抽出两个数字（{facts.numbers_in('47% 的青少年每天刷 3 小时')}）")
    check(facts.numbers_in("地球时间 1975 年") == ["1975"], "年份也算数字（它是个事实断言）")
    check(facts.numbers_in("四十年后人们才明白") == [],
          "中文数字不算（「四十年后」是叙述，不是统计）")
    check(facts.numbers_in("12.5 亿人") == ["12.5"], "小数不会被拆成 12 和 5")
    check(facts.numbers_in("") == [] and facts.numbers_in(None) == [], "空输入不炸")
    check(facts.numbers_in("12 和 12") == ["12"], "同一镜里重复的数字只报一次")

    print("\n② normalize：模型给的各种烂形状都收成一种")
    got = facts.normalize([
        {"claim": "47% 青少年", "source": "CNNIC 报告", "url": "https://x"},
        {"fact": "3 小时", "from": "QuestMobile"},          # 字段名不对
        "一句话出处",                                        # 直接给字符串
        None, 0, {"nothing": 1},                            # 垃圾
    ])
    check(len(got) == 3, f"四条有效、三条垃圾（实得 {len(got)}）")
    check(got[1] == {"claim": "3 小时", "source": "QuestMobile", "url": ""},
          f"`fact`/`from` 别名也认（{got[1]}）")
    check(got[2]["claim"] == "一句话出处" and got[2]["source"] == "",
          "纯字符串收成 claim-only")
    check(all(set(x) == {"claim", "source", "url"} for x in got), "形状统一")
    check(facts.normalize("不是列表") == [] and facts.normalize(None) == [],
          "非列表输入返回空，不抛")
    check(len(facts.normalize([{"claim": f"c{i}"} for i in range(50)]))
          == facts.MAX_SOURCES, f"最多留 {facts.MAX_SOURCES} 条（再多就是在凑数）")

    print("\n③ audit_sources：覆盖判据是「出处的 claim 里也出现了这个数字」")
    plan = {
        "scenes": [
            {"text": "47% 的青少年每天刷 3 小时"},
            {"text": "四十年后人们才明白"},
            {"text": "1975 年，一台射电望远镜捕捉到一段脉冲"},
        ],
        "sources": [{"claim": "47% 青少年日均 3 小时", "source": "CNNIC", "url": ""}],
    }
    got = facts.audit_sources(plan)
    check(got["numbers"] == ["47", "3", "1975"],
          f"全片数字（{got['numbers']}）")
    check([m["shot"] for m in got["missing"]] == [3],
          f"第 3 镜的 1975 没有出处被点名（{got['missing']}）")
    check("第 3 镜" in got["note"], f"note 说清是哪一镜（{got['note']}）")
    check("别当成已核实" in got["note"], "note 明确要求如实说，不许假装核实过")

    print("\n④ 不拦片：缺出处只是账，不是失败")
    no_src = {"scenes": [{"text": "增长了 300%"}], "sources": []}
    got = facts.audit_sources(no_src)
    check(got["missing"] and got["missing"][0]["shot"] == 1, "一条出处都没有 → 全点名")
    check(isinstance(got["note"], str) and got["note"], "有话说（不会静默）")

    clean = {"scenes": [{"text": "那天，屏幕上跳出一段信号"}], "sources": []}
    got = facts.audit_sources(clean)
    check(got["missing"] == [] and "不需要出处" in got["note"],
          f"全片没数字就不啰嗦（{got['note']}）")

    full = {"scenes": [{"text": "47% 的人"}],
            "sources": [{"claim": "47% 的人", "source": "X", "url": ""}]}
    got = facts.audit_sources(full)
    check(got["missing"] == [] and "全部覆盖" in got["note"], f"全覆盖（{got['note']}）")

    print("\n⑤ 畸形输入不炸（它会拿到模型刚生成的东西）")
    for bad in (None, {}, {"scenes": None}, {"scenes": [None, 1, "x"]},
                {"scenes": [], "sources": "不是列表"}):
        try:
            facts.audit_sources(bad)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"       {bad} → {type(exc).__name__}: {exc}")
        check(ok, f"audit_sources({bad!r}) 不抛")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
