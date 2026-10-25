# -*- coding: utf-8 -*-
"""出处账：片子里的每个数字，都要能说清它从哪来。

## 为什么这件事必须有一条代码在管

视觉自检看不出"这个数是编的"。质检能看出"这一镜太暗"、"这镜只有一块颜色"，
但 `47% 的青少年……` 里的 47 是不是真的，任何像素判据都测不出来。

而编一个数字是这条链路里**最贵的错**：片子看起来完全正常，用户拿去讲，
讲的是一个不存在的统计。旧链有一整套出处闸，简洁线在重写时把它丢了。

## 这里不做闸，只做账

判据只到"这个数字有没有出处"，不到"这个出处对不对"——后者要联网核验，
属于另一个量级的事。所以：

    audit_sources(plan) -> {numbers, sources, missing}

`missing` 非空时**不拦片**，只在报告里如实写出来。理由：一条片子因为
"某个数字没标出处"就整条失败，是把可用性绑在一个软指标上；而
"报告里写着 3 处数字没出处"是用户能立刻判断的东西。

## 什么算"有出处"

最小可信判据：**出处那条 `claim` 里也出现了这个数字**。
不是模糊匹配语义（那会给出"看起来对了"的假安心），就是数字本身出现过。
宁可报"没覆盖"，也不要报"覆盖了"却是错的。
"""

from __future__ import annotations

import re
from typing import Any

#: 数字（含小数与百分号）。中文数字（"四十年"）不算——它太容易与量词混淆，
#: 而且"四十年后"这种叙述不需要出处，需要出处的是 `47%` / `12 亿` / `1975`。
NUM_RE = re.compile(r"\d+(?:\.\d+)?")

#: 出处条目最多留几条。再多说明它在凑数，报告里也读不完。
MAX_SOURCES = 12

#: 两套分镜提示词**共用**的这一段。放在这里而不是各写一份：
#: 文案纪律一旦有两份副本，改了其中一份另一份不会跟着变——然后两种片子的
#: 数字标准就不一样了，而没人会去比对两段中文。
SOURCES_RULE = """
**如果字幕里出现具体数字**（人数/年份/比例/价格/距离/时长），必须同时给 `sources`：
一个数组，每项 `{"claim": "这个数字说了什么（≤20 字）", "source": "出处名称（机构/论文/书名/媒体）", "url": "链接，没有就空串"}`。
**宁可把数字删掉，也不要给一个查不到出处的数字**——编出来的数字是这条片子最贵的错，
而且没有任何画面判据能发现它。全片没有数字就给空数组 `[]`。"""


def numbers_in(text: Any) -> list[str]:
    """这段文字里出现的数字（按出现顺序，去重）。"""
    seen: list[str] = []
    for n in NUM_RE.findall(str(text or "")):
        if n not in seen:
            seen.append(n)
    return seen


def normalize(raw: Any) -> list[dict[str, str]]:
    """把模型给的 `sources` 收拾成 `[{claim, source, url}]`。

    模型会写成字符串数组、或字段名不对、或混进 null。这里一律收成同一种形状，
    坏条目直接丢——留着它会让报告里出现半截东西。
    """
    out: list[dict[str, str]] = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_SOURCES]:
        if isinstance(item, str) and item.strip():
            out.append({"claim": item.strip()[:60], "source": "", "url": ""})
            continue
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or item.get("fact") or "").strip()
        src = str(item.get("source") or item.get("from") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        if claim or src:
            out.append({"claim": claim[:60], "source": src[:60], "url": url[:300]})
    return out


def audit_sources(plan: Any) -> dict[str, Any]:
    """盘一遍这条片子的数字与出处。

    返回 ``{numbers, sources, missing, note}``：
      numbers  全片字幕里出现过的数字（去重）
      sources  模型给的出处
      missing  带数字但**没有出处覆盖**的镜（1 起），每项带 `shot/numbers/text`
      note     给人看的一句话（报告里直接用）
    """
    scenes = [s for s in ((plan or {}).get("scenes") or []) if isinstance(s, dict)]
    sources = normalize((plan or {}).get("sources"))
    cited = {n for s in sources for n in numbers_in(s.get("claim"))}

    numbers: list[str] = []
    missing: list[dict[str, Any]] = []
    for i, sc in enumerate(scenes):
        nums = numbers_in(sc.get("text"))
        if not nums:
            continue
        for n in nums:
            if n not in numbers:
                numbers.append(n)
        gaps = [n for n in nums if n not in cited]
        if gaps:
            missing.append({"shot": i + 1, "numbers": gaps,
                            "text": str(sc.get("text") or "")[:40]})

    if not numbers:
        note = "全片没有数字，不需要出处"
    elif not missing:
        note = f"{len(numbers)} 处数字，{len(sources)} 条出处，全部覆盖"
    else:
        note = (f"{len(numbers)} 处数字，{len(sources)} 条出处；"
                f"{len(missing)} 镜的数字没有出处（第 "
                + "、".join(str(m["shot"]) for m in missing[:6])
                + " 镜）——报告里要如实说，别当成已核实")
    return {"numbers": numbers, "sources": sources, "missing": missing, "note": note}
