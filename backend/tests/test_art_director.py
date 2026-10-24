# -*- coding: utf-8 -*-
"""写码前的美术导演（`motion/art_director.py`）。

要证的事：

① 一次调用把整片风格拆成可执行做法，并只改**还没有 code** 的镜的 brief；
② 已有 code 的镜一个字节都不动（重画要走 shots_patch，不在写码前偷偷改）；
③ 回话不是那一行 JSON、调用炸了 → 不改 brief，写一句「未判」，不拦；
④ 开关关掉时一次模型调用都不发；
⑤ 花费记在账上，提示词里带着风格、文案与原 brief。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_art_director.py``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import art_director as ad   # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class FakeLLM:
    def __init__(self, content: str, *, boom: bool = False) -> None:
        self.content = content
        self.boom = boom
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.seen.append(list(messages))
        if self.boom:
            raise RuntimeError("模型网关 502")
        return SimpleNamespace(content=self.content, usage={
            "model": "deepseek-flash", "prompt_tokens": 900,
            "completion_tokens": 300})


def spec_with(*shots):
    return {"aspect": "landscape", "fps": 30, "narration": False,
            "style": "像素赛博朋克：16 位像素网格、深蓝黑夜、霓虹紫红强调",
            "shots": list(shots)}


def main() -> int:
    print("① 补风格落地与画面落点，只碰没有 code 的镜")
    spec = spec_with(
        {"id": "s01", "text": "薄肌理论在社交平台上引爆", "brief": "展示社会比较"},
        {"id": "s02", "text": "它把身体变成一种身份符号", "brief": ""},
        {"id": "s03", "text": "已有代码的镜", "brief": "别动我", "code": "const Shot = () => null;\nexport default Shot;"},
    )
    reply = ('说明\n{"style_grammar": "像素网格 8px；色数 ≤6；硬边；左上单一光源",'
             '"shots": ['
             '{"id": "s01", "entities": ["肌肉特写", "霓虹广告屏", "仰头的人群"], '
             '"brief": "霓虹像素城市夜景，左侧巨幅广告屏上滚动肌肉特写，'
             '右下角一小群人仰头看；光从屏幕打在脸上；字幕压在底部留白带"},'
             '{"id": "s02", "entities": ["身份卡", "人剪影", "像素人群"], '
             '"brief": "一块像素化的身份卡悬在画面中央，卡片上是一个人剪影，'
             '背后的像素人群逐渐模糊；侧光；字幕放在卡片下方"},'
             '{"id": "s03", "entities": ["不该加"], "brief": "不该被改"}]}\n结束')
    llm = FakeLLM(reply)
    got = asyncio.run(ad.concretize(spec, llm=llm))
    check(got["calls"] == 1, "整片只发一次调用")
    check(spec.get("style_grammar") == "像素网格 8px；色数 ≤6；硬边；左上单一光源",
          "风格被拆成可执行做法")
    check("广告屏" in spec["shots"][0]["brief"], "s01 的 brief 补成了具体画面")
    check("身份卡" in spec["shots"][1]["brief"], "s02 的空 brief 也被补上")
    check(spec["shots"][0]["entities"] == ["肌肉特写", "霓虹广告屏", "仰头的人群"],
          f"s01 的实体清单落进 spec：{spec['shots'][0].get('entities')}")
    check("entities" not in spec["shots"][2], "已有 code 的 s03 连实体栏都没加")
    check(spec["shots"][2]["brief"] == "别动我", "已有 code 的 s03 一个字节没动")
    check(got["changed"] == ["s01", "s02"], f"账上只记改过的镜：{got['changed']}")
    check(got["cost_yuan"] > 0, f"花费记下来了：¥{got['cost_yuan']}")
    check("美术导演" in " ".join(got["notes"]), "回执里说明补了几镜")
    ask = llm.seen[0][1]["content"]
    check("像素赛博朋克" in ask and "展示社会比较" in ask and "薄肌理论" in ask,
          "提示词带着整片风格、原 brief 与文案")
    system = llm.seen[0][0]["content"]
    check("圈实体" in ask and "同处一个场景" in system,
          "提示词要求圈实体并组成一个场景")
    check(ad.BRIEF_MAX_CHARS >= len(spec["shots"][0]["brief"]), "brief 不超过导演上限")

    print()
    print("② 回话乱 / 调用炸：不改 brief，也不拦")
    spec = spec_with({"id": "s01", "text": "一句话", "brief": "原 brief"})
    llm = FakeLLM("我觉得可以画得好一点")
    got = asyncio.run(ad.concretize(spec, llm=llm))
    check(spec["shots"][0]["brief"] == "原 brief", "乱回不改原 brief")
    check("未判" in " ".join(got["notes"]), "回执写清未判")

    spec = spec_with({"id": "s01", "text": "一句话", "brief": "原 brief"})
    llm = FakeLLM("", boom=True)
    got = asyncio.run(ad.concretize(spec, llm=llm))
    check(spec["shots"][0]["brief"] == "原 brief", "调用炸了也不改原 brief")
    check("未判" in " ".join(got["notes"]), "炸了同样写未判")

    print()
    print("③ 开关关掉 / 没有要写的镜：一次调用都不发")
    spec = spec_with({"id": "s01", "text": "一句话", "brief": "原 brief"})
    llm = FakeLLM("{}")
    origin = ad.DIRECTOR_ENABLED
    ad.DIRECTOR_ENABLED = False
    try:
        got = asyncio.run(ad.concretize(spec, llm=llm))
    finally:
        ad.DIRECTOR_ENABLED = origin
    check(llm.calls == 0 and got["enabled"] is False, "关掉时零调用")

    spec = spec_with({"id": "s01", "text": "一句话", "brief": "已有",
                      "code": "const Shot = () => null;\nexport default Shot;"})
    llm = FakeLLM("{}")
    got = asyncio.run(ad.concretize(spec, llm=llm))
    check(llm.calls == 0 and not got["changed"], "整片都有 code 时不发调用")

    print()
    print("④ 实体清单：字符串写法也收、去重、封顶 5 个")
    spec = spec_with({"id": "s01", "text": "鲣鸟在海边孵蛋", "brief": "原 brief"})
    reply = ('{"style_grammar": "扁平矢量；暖色；柔影", "shots": [{"id": "s01", '
             '"entities": "鲣鸟、巨型蛋、鲣鸟、三枚蛋、草丛、落日、海面", '
             '"brief": "鲣鸟站在巨型蛋旁，三枚蛋在沙坑里"}]}')
    got = asyncio.run(ad.concretize(spec, llm=FakeLLM(reply)))
    check(spec["shots"][0]["entities"] == ["鲣鸟", "巨型蛋", "三枚蛋", "草丛", "落日"],
          f"逗号串拆开、去重、只留前 5 个：{spec['shots'][0].get('entities')}")
    check(spec["shots"][0]["brief"].startswith("鲣鸟站在"), "brief 照常落进 spec")
    check(got["changed"] == ["s01"], "只改有实体的那一镜")

    print()
    print("⑤ 整片漏写 art 时导演补一份；已经写了就不覆盖")
    spec = spec_with({"id": "s01", "text": "火光里的岩壁", "brief": "原 brief"})
    reply = ('{"style_grammar": "单色剪影；暖光", "art": {'
             '"look": "单色剪影加暖光池", "brush": "graphite", "light": "left",'
             '"texture": "paper", "type": "serif-display", "motion": "spring",'
             '"palette_note": "火光橙 → 屏幕冷蓝", "bogus": "x"},'
             '"shots": [{"id": "s01", "brief": "岩壁前一堆火"}]}')
    got = asyncio.run(ad.concretize(spec, llm=FakeLLM(reply)))
    check(spec.get("art", {}).get("brush") == "graphite"
          and spec["art"].get("palette_note") == "火光橙 → 屏幕冷蓝",
          f"漏写的 art 被补上（{spec.get('art')}）")
    check("bogus" not in spec.get("art", {}),
          "枚举外/不认识的栏不落进 spec——归一那一份说了算")
    check("美术方向" in " ".join(got["notes"]), "回执说明补了美术方向")

    spec = spec_with({"id": "s01", "text": "一句话", "brief": "原 brief"})
    spec["art"] = {"brush": "marker"}
    reply = ('{"art": {"brush": "watercolor", "look": "不该生效"},'
             '"shots": [{"id": "s01", "brief": "新 brief"}]}')
    asyncio.run(ad.concretize(spec, llm=FakeLLM(reply)))
    check(spec["art"] == {"brush": "marker"}, "已经写了 art 时导演不覆盖")

    print()
    print("⑥ 整片贯穿主体：每一镜的 brief 都要把同一个东西放进去")
    spec = spec_with({"id": "s01", "text": "他在洞口点起火", "brief": "岩壁与火堆"})
    spec["protagonist"] = {"look": "瘦高圆头的剪影", "kind": "人", "render": "lit",
                           "wardrobe": "兽皮斗篷 → 连帽衫"}
    llm = FakeLLM('{"shots": [{"id": "s01", "brief": "同一个人站在火堆前"}]}')
    got = asyncio.run(ad.concretize(spec, llm=llm))
    ask = llm.seen[0][1]["content"]
    system = llm.seen[0][0]["content"]
    check("整片贯穿主体" in ask and "瘦高圆头的剪影" in ask,
          "提示词带着整片贯穿主体（导演不会把它当成「凭空加人」删掉）")
    check("同一个" in ask and "画法 lit" in ask, "主体类型与画法方向也进了提示词")
    check("兽皮斗篷 → 连帽衫" in ask, "材质/衣服演进线也进了提示词")
    check("每一镜的 brief 都要把这个" in system
          and "**同一个东西**" in system
          and "优先于「brief 没提人就不要加人」" in system,
          "系统提示写清：有贯穿主体时，文案没提到它也要画")
    check("也可能是一头鹿、一辆车、一块发光的矩形" in system,
          "系统提示不把贯穿主体默认成人")

    print()
    print("⑦ 风格包有贯穿姿势：每镜 brief 必须写「主体怎么动」")
    spec = spec_with({"id": "s01", "text": "火光照亮第一张脸", "brief": "洞穴"})
    spec["skill_contract"] = {"name": "style_demo", "display": "人影风格",
                          "manifest": {"template": "demo", "repeat_composition": True,
                                       "recurring_pose": "侧身行走的剪影步态"}}
    llm = FakeLLM('{"shots": [{"id": "s01", "brief": "同一个人从左侧走进火光"}]}')
    asyncio.run(ad.concretize(spec, llm=llm))
    ask = llm.seen[0][1]["content"]
    spec["skill_contract"]["manifest"]["art_note"] = "每镜 brief 要写清年代与一件旧物"
    spec["skill_contract"]["manifest"]["review_note"] = "评审专用口径"
    llm = FakeLLM('{"shots": [{"id": "s01", "brief": "同一个人从左侧走进火光"}]}')
    asyncio.run(ad.concretize(spec, llm=llm))
    ask_note = llm.seen[0][1]["content"]
    check("评审专用口径" not in ask_note, "评审那层的补充要求不进导演提示词")
    spec["skill_contract"]["manifest"].pop("art_note")
    spec["skill_contract"]["manifest"].pop("review_note")

    # 每一镜都成立的能力（manifest.caps_always）由系统补上：不依赖导演每镜
    # 记不记得点名；名字仍取风格包自己的段名，不认识的照样丢掉。
    spec = spec_with({"id": "s01", "text": "一句话", "brief": "原 brief"})
    spec["skill_contract"] = {
        "name": "style_demo", "display": "演示",
        "body": ("<!-- section: materials -->\n# 材质\n正文\n"
                 "<!-- section: camera-3d -->\n# 三维\n正文\n"),
        "manifest": {"progressive": "true", "caps_always": ["materials", "没有这段"]},
    }
    llm = FakeLLM('{"shots": [{"id": "s01", "brief": "b", '
                  '"caps": ["camera-3d", "不存在的名字"]}]}')
    asyncio.run(ad.concretize(spec, llm=llm))
    check(spec["shots"][0].get("caps") == ["materials", "camera-3d"],
          f"caps_always 补上、导演点名的去重并保留：{spec['shots'][0].get('caps')}")

    spec.pop("skill_contract")
    llm = FakeLLM('{"shots": [{"id": "s01", "brief": "x"}]}')
    asyncio.run(ad.concretize(spec, llm=llm))
    check("主体怎么动" not in llm.seen[0][1]["content"],
          "没有风格包时不多这一段（老片子提示词逐字不变）")

    print()
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
