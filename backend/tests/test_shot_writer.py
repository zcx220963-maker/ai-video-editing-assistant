# -*- coding: utf-8 -*-
"""逐镜写手（`motion/shot_writer.py`）的离线守卫：主对话只交 brief，代码单独写。

这一份证的是那条链的**每一道接缝**，不依赖真模型：

① 技能正文按标题/标记截取到（不是空串）——写手的规则来自 SKILL.md 那一份；
② 整片设定逐字稳定（前缀缓存靠它命中），单镜任务里带文案/brief/字段；
③ 回话解析：带语言标记的块、无标记的块、裸代码三种都取得到；
④ lint 失败会把报错喂回去重写，过 lint 的才收；两次仍不过 → unwritten；
⑤ 未判（容器不通）照收，但**不是通过**——回执里如实写着；
⑥ 已有 code 的镜一个字节都不动；
⑦ 模型看到的那一份回执不含逐镜源码（`plan_model_view`）；
⑧ 只交新 brief 的 shots_patch ＝ 重画这一镜（旧代码被丢掉）。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_shot_writer.py``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs                  # noqa: E402
from storyline_server.motion import shot_writer as sw                # noqa: E402
from storyline_server.nodes import motion_plan as mp                 # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1




GOOD = """import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { CaptionLayer } from '@dg/film';
import { Machine } from '@dg/lib/draw/Machine';

const Shot = () => {
  const frame = useCurrentFrame();
  const p = interpolate(frame, [0, 30], [0, 1]);
  return (
    <AbsoluteFill style={{ backgroundColor: '#111' }}>
      <svg viewBox="0 0 1920 1080" width={1920} height={1080}>
        <Machine x={600} y={700} w={420} kind="steam" smoke={p} />
      </svg>
      <CaptionLayer />
    </AbsoluteFill>
  );
};
export default Shot;"""

BAD = "const Shot = () => <div>hi</div>;\nexport default Shot;"


class FakeLLM:
    """按顺序吐预置回话；`usage` 固定，便于断言花费被记下来。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.seen.append(list(messages))
        text = self.replies.pop(0) if self.replies else "```tsx\n" + GOOD + "\n```"
        return SimpleNamespace(content=text, usage={"model": "deepseek-flash",
                                                     "prompt_tokens": 100,
                                                     "completion_tokens": 100})


MOTION_OK = {
    "status": "ok", "samples": 72, "sample_hz": 8,
    "events": [
        {"start_frame": 30, "end_frame": 66, "start_sec": 1.0, "peak_diff": 4.6},
        {"start_frame": 150, "end_frame": 186, "start_sec": 5.0, "peak_diff": 3.8}],
    "thresholds": {"diff_min": 1.5, "min_events": 2, "min_gap_sec": 0.4, "sample_hz": 8},
}

MOTION_WEAK = {
    "status": "ok", "samples": 72, "sample_hz": 8,
    "events": [
        {"start_frame": 8, "end_frame": 19, "start_sec": 0.25, "peak_diff": 2.36},
        {"start_frame": 116, "end_frame": 124, "start_sec": 3.625, "peak_diff": 1.63}],
    "thresholds": {"diff_min": 1.5, "min_events": 2, "min_gap_sec": 0.4, "sample_hz": 8},
}

MOTION_REJECT = {
    "status": "reject", "samples": 72, "sample_hz": 8,
    "reason": "全片只数到 1 个先后变化段（要 ≥2 个）——所有元素挤在同一段时间里一起变",
    "events": [{"start_frame": 0, "end_frame": 9, "start_sec": 0.0, "peak_diff": 0.3}],
    "thresholds": {"diff_min": 1.5, "min_events": 2, "min_gap_sec": 0.4, "sample_hz": 8},
}


def fake_client(verdicts: list[bool | None], motions: list[dict | None] | None = None):
    calls: list[str] = []
    renders: list[str] = []

    def lint(code, declared=None):
        calls.append(code)
        ok = verdicts.pop(0) if verdicts else True
        if ok is None:
            raise RuntimeError("容器不通")
        if ok:
            return {"ok": True, "errors": [], "declared": declared or []}
        return {"ok": False, "errors": [{"rule": "dg/no-import",
                                         "line": 2, "msg": "不许这么写"}]}

    def render(code, cache_key, input_props, declared_fields=None, render=None):
        renders.append(code)
        got = (motions.pop(0) if motions else None) or MOTION_OK
        return {"ok": True, "cache_key": cache_key, "draft": True,
                "render_scale": 0.4, "motion": got}
    return {"lint": lint, "render": render}, calls, renders


def spec_with(*shots):
    return cs.validate_code_spec({
        "aspect": "landscape", "fps": 30, "narration": False,
        "palette": "deep-ocean", "style": "杂志编辑风：左对齐网格、大留白、单一强调色",
        "shots": list(shots)})[0]


def main() -> int:
    print("① 技能正文截取：写手拿到的是 SKILL.md 的那一份")
    contract = sw._contract()
    check(len(contract) > 10000, f"截到 {len(contract)} 字节")
    check("# 这一镜画什么：你交 `brief`，代码由系统逐镜写" in contract,
          "含镜头内容主段")
    check("交付入口只有一个：`plan_motion`" in contract
          and "成片不读这些文件" in contract.replace("\r\n", "\n"),
          "写明交付入口是 plan_motion、工作区里的草稿不算交付")
    check("零件库" in contract, "含零件库")
    check("字不许压在图上" in contract, "含字幕安全区")
    check("自由" in contract, "画法自由（零件库不再是必选）")
    check("视觉手艺" in contract, "含视觉手艺一节")
    check("时间与缓动" in contract, "官方 Remotion 规则已拼进契约")

    print("② 整片设定稳定、单镜任务带齐字段")
    spec = spec_with({"id": "s01", "text": "蒸汽机让英国跑了起来", "brief": "左侧蒸汽机冒烟，右侧工人扛煤",
                      "entities": ["蒸汽机", "煤袋", "工人"],
                      "highlight": ["蒸汽机"], "min_duration_sec": 8})
    film = sw._film_block(spec)
    check(film == sw._film_block(spec), "同一份 spec 两次逐字相同（前缀缓存）")
    check("deep-ocean" in film and "杂志编辑风" in film, "整片色板与风格写进去了")
    check("300" in film, "字幕安全区按横屏 300px 写进去了")
    # 2026-10-19 真机 13 镜：模板的 StyleCaption 和框架的 <CaptionText/> 同时渲染，
    # 左上角两行字叠成重影。模板槽位声明 text_kind=caption 时必须说清「只留一条」。
    ask = sw._shot_block(spec["shots"][0], index=0)
    check("蒸汽机让英国跑了起来" in ask and "左侧蒸汽机冒烟" in ask, "单镜任务带文案与 brief")
    check("brief 是这一镜的最高事实" in ask and "不要用 0 / 空串 / 透明" in ask,
          "brief 明确不要的零件优先于模板，且不许用空值藏起来")
    check("useVideoConfig" in ask, "提醒不要写死帧数")
    check("圈实体" in ask, "单镜任务先要求从文案里圈出实体")
    check("蒸汽机、煤袋、工人" in ask and "每一个都要画出来" in ask,
          "实体清单逐件进单镜任务，并要求都画进同一个场景")
    check("同处一个场景" in ask, "写清多实体不是各画各的，要有空间关系")
    check("实体：" in ask and "外轮廓" in ask and "光：" in ask,
          "单镜任务要求先写一条注释，把每件实体拆到外轮廓/标志物/部件")
    check("交之前按这五条自检" in ask and "40%~70%" in ask,
          "交稿前自检与美术评审同一套五条（第一次就按评审标准写）")
    check("同处一个场景" in sw._SYSTEM_HEAD and "别硬凑" in sw._SYSTEM_HEAD,
          "系统提示把「一镜多实体、组成一个场景」定成默认画法，同时不许硬凑")
    check("至少两拍" in ask, "动效要求至少两拍，不是一帧定格再淡出")
    check("贯穿装置" not in film, "没写 recurring_prop 时整片设定不出现这一段（老片子字节不变）")
    check("风格落地" not in film, "没写 style_grammar 时不多一个字节（前缀缓存不变）")
    spec["style_grammar"] = "像素网格 8px；色数 ≤6；硬边；左上单一光源"
    film_grammar = sw._film_block(spec)
    check("风格落地" in film_grammar and "像素网格 8px" in film_grammar,
          "风格的可执行拆解逐字进整片设定（美术导演补的那几条真的到达写手）")
    del spec["style_grammar"]
    spec["recurring_prop"] = "一枚钉在墙上的读数表"
    film_prop = sw._film_block(spec)
    check("整片贯穿装置" in film_prop and "一枚钉在墙上的读数表" in film_prop,
          "贯穿装置逐字进整片设定（同一段进每一镜，前缀缓存照命中）")
    check("UI 贴片" in film_prop, "写清它是画里世界的一件道具，不是 UI 贴片")
    del spec["recurring_prop"]

    check("整片贯穿主体（**同一个东西**" not in film,
          "没写 protagonist 时整片设定不出现这一段（技能正文里那一句 import 注释不算）")
    spec["protagonist"] = {
        "look": "瘦高、圆头、宽肩，全片只画剪影", "kind": "人", "render": "lit",
        "wardrobe": "兽皮斗篷 → 连帽衫",
        "height": 0.48, "foot_y": 0.86, "facing": "right",
        "stride_sec": 1.0, "light": "火光橙 → 手机屏冷蓝"}
    film_pro = sw._film_block(spec)
    check("整片贯穿主体" in film_pro and "瘦高、圆头、宽肩" in film_pro,
          "贯穿主体逐字进整片设定（同一段进每一镜，前缀缓存照命中）")
    check("WalkingFigure" not in sw._protagonist_block(spec),
          "主体段落不点名任何组件：画法留给写手（2026-10-20 用户：为什么非要限制模板）")
    check("受光/背光两档以上" in film_pro,
          "画法方向 lit 原样写进提示词（要真实的样子时不是默认剪影）")
    check("朝画面右" in film_pro and "x 随时间增大" in film_pro,
          "朝向就是行进方向（面朝右＝x 增大，别倒着走）")
    check("默认整镜都在动" in film_pro,
          "默认一直在动；中途停下只留给明确写了「停下」的 brief")
    check("停的那一镜也别把它画小" in film_pro, "停一拍时怎么画也写了，别把主体停丢")
    check("兽皮斗篷 → 连帽衫" in film_pro, "材质/衣服演进线原样进设定")

    # 非人主体：同样不给任何共享骨架，身份与连续性照旧。
    spec["protagonist"] = {
        "look": "一块竖立的发光矩形，边缘一圈暖光", "kind": "发光的矩形",
        "render": "lit", "height": 0.34, "foot_y": 0.72, "facing": "right"}
    film_obj = sw._protagonist_block(spec)
    check("发光的矩形" in film_obj and "0.34" in film_obj and "0.72" in film_obj,
          "非人主体的身份与连续性比例仍写清")
    check("WalkingFigure" not in film_obj,
          "非人主体不给任何共享件的调用样板")
    del spec["protagonist"]

    print("②-d 用户原话：每一镜任务块的最前面都原样带一遍")
    req = "一只橘猫蹲在便利店门口躲雨，湿漉漉的，不要卡通脸"
    rb = sw._request_block(f"  {req}\n")
    check(rb.startswith("=== 用户原话") and req in rb, "原话逐字保留、压成一行")
    check(sw._request_block("") == "", "用户没说话时一个字节都不多")
    rb_long = sw._request_block("猫" * 5000)
    check("已截断" in rb_long and len(rb_long) < 4200,
          "超长原话截断但保留开头（开头通常才是这条片子要什么）")
    import inspect as _inspect
    for fn in (sw.write_shot_code, sw.rewrite_shot_code):
        check("user_request" in _inspect.signature(fn).parameters,
              f"{fn.__name__} 收 user_request（身份层注入到节点，再逐镜传下来）")
    req_client, _, _ = fake_client([True])
    req_spec = spec_with({"id": "s01", "text": "猫在雨里", "brief": "一只橘猫蹲在门口"})
    req_llm = FakeLLM(["```tsx\n" + GOOD + "\n```"])
    asyncio.run(sw.write_shot_code(req_spec, llm=req_llm, client=req_client,
                                   parallel=1, user_request=req))
    sent = str(req_llm.seen[0][-1]["content"])
    check(sent.startswith("=== 用户原话") and req in sent and "brief" in sent,
          "写手这一镜的提问以用户原话打头，分镜 brief 跟在后面")


    # 按需块（2026-10-23）：清单**只有一份**——schema enum / 校验过滤 / 写手两张表
    # 三处同源。漂了的表现是「卡上写了 chart、写手却没拿到那一块」，离线看不出来。
    check(set(sw._RULES_OPTIONAL) | set(sw._SKILL_OPTIONAL) == set(cs.SKILL_SECTION_NAMES),
          f"写手两张表的键与 SKILL_SECTION_NAMES 逐值相等"
          f"（{sorted(set(sw._RULES_OPTIONAL) | set(sw._SKILL_OPTIONAL))}）")
    check(set(cs.spec_schema()["properties"]["skill_sections"]["items"]["enum"])
          == set(cs.SKILL_SECTION_NAMES),
          "schema 的 enum 与 SKILL_SECTION_NAMES 逐值相等")
    _base_c = sw._contract(True)
    _chart_c = sw._contract(True, ("chart",))
    check(len(_chart_c) > len(_base_c) and "图表与路径" in _chart_c
          and "图表与路径" not in _base_c,
          "按需块默认不给、点名才给（chart 那一节）")
    check("4.5 转场" not in _base_c,
          "转场写法默认不给：写手不管接缝，转场在拼接层")

    # 批 3（2026-10-23）：全片画面表 + 相邻镜。上面那份 `spec` 只有一镜，
    # 相邻关系要用一条三镜的片才验得出来。
    _raw3 = {"title": "三镜", "aspect": "landscape", "fps": 30, "narration": False,
             "subtitle_mode": "full", "texture": "none",
             "shots": [{"id": f"s{i:02d}", "text": f"第{i}句文案",
                        "brief": f"第{i}镜的画面：俯视桌面，一只旧钱包居中，暖橙侧光",
                        "min_duration_sec": 3.0,
                        "card": "typography/paper-title-card",
                        "subject": "某物", "derived_from": ["<original>"]}
                       for i in (1, 2, 3)]}
    _spec3, _e3 = cs.validate_code_spec(cs.overlay_knobs(dict(_raw3), {}))
    check(_e3 == [], f"三镜夹具过契约（{_e3[:1]}）")
    check("画面：俯视桌面" in sw._film_block(_spec3),
          "全片画面表带每镜的 brief（不只是 60 字文案）——写手唯一知道别人画了什么的地方")
    _s0 = sw._shot_block(_spec3["shots"][0], index=0, spec=_spec3)
    _s1 = sw._shot_block(_spec3["shots"][1], index=1, spec=_spec3)
    _s2 = sw._shot_block(_spec3["shots"][2], index=2, spec=_spec3)
    # 注意：`_shot_block` 的通用正文里本来就有「下一镜的位置与相位要接得上」这句，
    # 所以判相邻镜必须用**带括号的整句标记**，不能只搜「下一镜」三个字。
    _UP, _DOWN = "上一镜（已经画过", "下一镜（这一镜要在视觉上接得住它）"
    check(_DOWN in _s0 and _UP not in _s0,
          "首镜只有下一镜（它没有上一镜，给一句假的会变成假约束）")
    check(_UP in _s1 and _DOWN in _s1, "中间镜同时看得到上一镜与下一镜")
    check(_UP in _s2 and _DOWN not in _s2, "末镜只有上一镜")
    check(_UP not in sw._shot_block(_spec3["shots"][0], index=0),
          "不传 spec 时一个字节都不多（老调用点逐字不变）")

    # 批 4（2026-10-23）：逐镜按需块只进那一镜的 user 消息（system 不能变，否则
    # 前缀缓存整片作废）。
    _spec3["shots"][1]["sections"] = ["chart"]
    check("图表与路径" in sw._shot_block(_spec3["shots"][1], index=1, spec=_spec3)
          and "图表与路径" not in sw._shot_block(_spec3["shots"][0], index=0, spec=_spec3),
          "逐镜点名的按需块只进那一镜（别的镜不付这 2.4 KB）")
    check("图表与路径" not in sw._film_block(_spec3),
          "逐镜的块不进 system 提示——system 一镜一变，前缀缓存就整片作废")
    _spec3["shots"][1]["sections"] = []

    # 共享账（2026-10-23）：写手自报的画法 → 全片账 → 后面每一镜都读得到。
    _code1 = ("import React from 'react';\n"
              "// 实体：旧钱包=翻毛皮+拉链齿+钞票角（受光/背光/影）；桌面=木纹｜光：左前暖橙｜字：左下\n"
              "export const Shot = () => null;\n")
    _look = sw._look_from_code(_code1)
    check(_look.startswith("实体：") and "光：左前暖橙" in _look,
          "画法账的原料是写手自报的那条注释（零额外调用）")
    check(sw._look_from_code("export const Shot = () => null;") == "",
          "没写注释的镜不产生假账（宁缺勿猜）")
    _led_spec = {}
    check(sw._ledger_block(_led_spec) == "", "账没立起来时一个字节都不多")
    sw._seed_ledger(_led_spec, _code1, {"id": "s01"})
    sw._seed_ledger(_led_spec, "// 实体：另一件东西｜光：顶光｜字：右\n", {"id": "s02"})
    _led = _led_spec["film_ledger"]
    check(_led["look"] == _look, "第一镜立的规矩不被后来的镜覆盖")
    check(sorted(_led["shots"]) == ["s01", "s02"], "每写完一镜回填一行")
    _blk = sw._ledger_block(_led_spec)
    check("全片画法账" in _blk and "已经画完的镜" in _blk,
          f"账渲染成两段给写手看（{len(_blk.encode('utf-8'))} 字节）")
    # 回程：评审的结论写回账，去重且有上限。
    sw.record_fix(_led_spec, ["s03"], "配色从暖橙跳到冷蓝", "s03 换回暖橙")
    sw.record_fix(_led_spec, ["s03"], "配色从暖橙跳到冷蓝", "s03 换回暖橙")
    check(len(_led["fixes"]) == 1, "同一条结论不重复记")
    for i in range(12):
        sw.record_fix(_led_spec, ["s05"], f"问题{i}", f"改法{i}")
    check(len(_led["fixes"]) == 8, f"账有上限（{len(_led['fixes'])} 条），不会越滚越长")
    check("全片复核挑出的问题" in sw._ledger_block(_led_spec),
          "评审的结论进账——重写任意一镜都看得到")
    _s_with_led = sw._shot_block({"id": "s03", "text": "三", "brief": "三"},
                                 index=2, spec={"shots": [{"id": f"s{i}"} for i in (1, 2, 3)],
                                                "film_ledger": _led})
    check("全片画法账" in _s_with_led, "单镜块里真的带上了账")
    # 复核本身：镜数不够就直接跳过，不花一分钱。
    check(asyncio.run(sw.review_film({"shots": [{"id": "s01"}]}, llm=None))["issues"] == [],
          f"少于 {sw.FILM_REVIEW_MIN_SHOTS} 镜不复核（不花冤枉钱）")


    print("③ 回话解析：三种形状都取得到")
    check(sw.extract_code("说明\n```tsx\n" + GOOD + "\n```\n结束") == GOOD, "带 tsx 标记")
    check(sw.extract_code("```\n" + GOOD + "\n```") == GOOD, "无语言标记")
    check(sw.extract_code("前言\n" + GOOD).startswith("import React"), "裸代码从 import 起")

    print("④ lint 报错喂回去重写")
    bad_code = "```tsx\n" + BAD + "\n```"
    spec = spec_with({"id": "s01", "text": "一句话文案", "brief": "一台蒸汽机"})
    client, calls, _ = fake_client([False, True])
    llm = FakeLLM([bad_code, "```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(spec["shots"][0]["code"] == GOOD, "第二次过 lint 的代码被收下")
    check(len(calls) == 2, "lint 被判了两次")
    check(any("dg/no-import" in str(m) for m in llm.seen[-1]),
          "第二次调用带上了 lint 的原始报错")
    check(out["written"] == {"s01": len(GOOD)}, f"账上写着 s01 写了 {len(GOOD)} 字节")
    check(out["cost_yuan"] > 0, f"花费记下来了：¥{out['cost_yuan']}")

    print("⑤ 写了三次仍不过 → 点名，不是静默通过")
    spec = spec_with({"id": "s02", "text": "一句话文案", "brief": "一台机器"})
    client, _, _ = fake_client([False, False, False])
    llm = FakeLLM([bad_code, bad_code, bad_code])
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(out["unwritten"] == ["s02"], "s02 记进 unwritten")
    check(not spec["shots"][0]["code"], "没写成的代码不留在产物里")
    check("dg/no-import" in out["notes"][0], "回执带着 lint 原文")

    print("⑥ 容器不通＝未判，照收但明说不是通过")
    spec = spec_with({"id": "s03", "text": "一句话文案", "brief": "一台机器"})
    client, _, _ = fake_client([None])
    llm = FakeLLM(["```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(spec["shots"][0]["code"] == GOOD, "代码收下了")
    check(out["unwritten"] == [], "不算失败")
    check("未判" in out["notes"][0] and "不是通过" in out["notes"][0],
          "回执如实写未判")

    print("⑦ 已经带了 code 的镜一个字节都不动")
    spec = spec_with({"id": "s01", "text": "一句话文案", "code": GOOD},
                     {"id": "s02", "text": "第二句", "brief": "一个齿轮"})
    before = spec["shots"][0]["code"]
    llm = FakeLLM(["```tsx\n" + GOOD + "\n```"])
    client, calls, _ = fake_client([True])
    asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=2))
    check(spec["shots"][0]["code"] == before, "s01 的代码没被重写")
    check(len(calls) == 1, "只给 s02 判了 lint")

    print("⑧ 回给模型的那一份不含逐镜源码")
    payload = {"motion_spec": spec, "shot_count": 2, "plan_table": []}
    view = mp.plan_model_view(payload)
    check(all("code" not in s for s in view["motion_spec"]["shots"]),
          "瘦身版没有 code")
    check(all(s.get("code_chars") for s in view["motion_spec"]["shots"]),
          "每镜只报 code_chars")
    check("code_note" in view and "shots_patch" in view["code_note"], "告诉模型怎么改")
    check("code" in spec["shots"][0], "库里那一份仍有完整代码")

    print("⑨ 只交 brief 的补丁＝重画这一镜")
    base = {"shots": [{"id": "s01", "text": "old", "brief": "旧画面", "code": GOOD}],
            "aspect": "landscape"}
    merged = mp.merge_shots_patch(base, [{"id": "s01", "brief": "新画面：一台锅炉"}])
    check("code" not in merged["shots"][0], "旧代码被丢掉（否则画面不会变）")
    check(merged["shots"][0]["brief"] == "新画面：一台锅炉", "新 brief 进去了")
    merged2 = mp.merge_shots_patch(base, [{"id": "s01", "text": "只改文案"}])
    check(merged2["shots"][0]["code"] == GOOD, "没给 brief 的文案补丁保留原代码")

    print("⑨-b spec 与 shots_patch 同时出现 → 两个都并，不许丢")
    # 真机 run 8c5e161ddb20：模型把「要改的 6 镜」放 spec、「新加的 16 镜」放 shots_patch，
    # 旧写法只认 spec，16 镜被静默丢掉，32 镜的片子按 6 镜算时长（66s vs 220s）打回。
    base16 = {"style": "旧风格", "target_duration_sec": 220,
              "shots": [{"id": f"s{i:02d}", "text": f"第{i}句", "code": GOOD}
                        for i in range(1, 17)]}
    patch16 = [{"id": f"s{i:02d}", "text": f"第{i}句", "brief": f"第{i}镜"}
               for i in range(17, 33)]
    spec6 = {"style": "新风格", "target_duration_sec": 220,
             "shots": [{"id": "s01", "text": "改过的第一句", "brief": "重画第一镜"},
                       {"id": "s02", "text": "改过的第二句", "brief": "重画第二镜"}]}
    got = mp.merge_submission(base16, spec6, patch16)
    check(len(got["shots"]) == 32, f"16 底稿 + 16 补丁 + 6 改写 = {len(got['shots'])} 镜")
    check(got["shots"][0]["text"] == "改过的第一句", "spec 里的改写赢过底稿")
    check("code" not in got["shots"][0], "改写的那镜旧代码被丢掉（否则画面不变）")
    check(got["shots"][16]["id"] == "s17", "新镜按 id 追加在末尾")
    check(got["style"] == "新风格", "spec 的顶层字段照常生效")

    print("⑨-c 整份重交：brief 逐字相同的镜复用上一版代码，不重写不重渲")
    # 真机 run 8c5e161ddb20：一次 600 s 超时让模型把同一份 32 镜整份重交了三遍，
    # 每一遍都从零写一遍代码（写手调用 + 草稿真渲 ×32）。brief 没变的镜画面不可能变。
    coded = {"shots": [{"id": "s01", "text": "第一句", "brief": "一台蒸汽机", "code": GOOD},
                       {"id": "s02", "text": "第二句", "brief": "一条传送带", "code": GOOD + "\n"}]}
    again = {"aspect": "landscape", "shots": [
        {"id": "s01", "text": "改过的第一句", "brief": "  一台蒸汽机 "},   # 只改文案、空白不同
        {"id": "s02", "text": "第二句", "brief": "一条新的传送带"},        # brief 变了
        {"id": "s03", "text": "第三句", "brief": "一座工厂"},              # 上一版没有
        {"id": "s04", "text": "第四句", "brief": "一座工厂", "code": BAD},  # 模型自己交了 code
    ]}
    got, reused = mp.reuse_coded(coded, again)
    check(reused == ["s01"], f"只复用了 brief 没变的那一镜：{reused}")
    check(got["shots"][0]["code"] == GOOD, "s01 的代码逐字节复用（文案改了不影响画面）")
    check("code" not in got["shots"][1], "brief 变了 → 不复用，交给写手重画")
    check("code" not in got["shots"][2], "上一版没有这一镜 → 没得复用")
    check(got["shots"][3]["code"] == BAD, "模型自己交了 code 的镜不被覆盖")
    check(again["shots"][0].get("code") is None, "入参不被就地改动")
    _g, _r = mp.reuse_coded(None, again)
    check(_r == [], "本会话没有上一版 → 一镜都不复用")

    print("⑨-d 换了风格包 → 整份不复用（brief 没变也不能沿用上一版的画面）")
    # brief 只描述「这一镜画什么」，画法由风格包定。用户在计划卡上把「自动」换成
    # 「剪影年轮」，brief 一个字没动——若照常复用，13 镜会全是上一版自由画的代码，
    # 用户看到的「换风格」等于没换，而且不报错。
    pack_a = {"name": "style_a", "manifest": {"template": "silhouette-history"}}
    pack_b = {"name": "style_b", "manifest": {"template": "silhouette-history"}}
    packed_base = {"skill_contract": pack_a, "shots": [
        {"id": "s01", "text": "第一句", "brief": "一台蒸汽机", "code": GOOD}]}
    same_pack = {"skill_contract": dict(pack_a), "shots": [
        {"id": "s01", "text": "第一句", "brief": "  一台蒸汽机 "}]}
    got, reused = mp.reuse_coded(packed_base, same_pack)
    check(reused == ["s01"], f"同一个风格包 → 照常复用（{reused}）")
    check(got["shots"][0]["code"] == GOOD, "复用的是那一份代码")
    got, reused = mp.reuse_coded(packed_base, {**same_pack, "skill_contract": pack_b})
    got, reused = mp.reuse_coded(packed_base, {**same_pack, "skill_contract": dict(
        pack_a, manifest={"template": "other-template"})})
    got, reused = mp.reuse_coded({"shots": packed_base["shots"]}, same_pack)
    got, reused = mp.reuse_coded(packed_base, {"shots": same_pack["shots"]})

    print("⑩ 契约：brief 可省 code，两者都缺才打回")
    spec, errs = cs.validate_code_spec({"shots": [
        {"id": "s01", "text": "一句话", "brief": "一台蒸汽机"}]})
    check(not errs and spec["shots"][0]["brief"] == "一台蒸汽机", "只有 brief 能过契约")
    _spec, errs = cs.validate_code_spec({"shots": [{"id": "s01", "text": "一句话"}]})
    check(any("没有 brief 也没有 code" in e for e in errs),
          "两者都缺时带镜号说清怎么改")

    print("⑪ 草稿真渲的闸⑭ 不过 → 拿实测段数重写")
    spec = spec_with({"id": "s04", "text": "一句话文案", "brief": "一条传送带"})
    llm = FakeLLM(["```tsx\n" + GOOD + "\n```", "```tsx\n" + GOOD + "\n```"])
    client, _, renders = fake_client([True, True], [MOTION_REJECT, MOTION_OK])
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(spec["shots"][0]["code"] == GOOD, "第二稿过闸⑭ 后被收下")
    check(len(renders) == 2, "草稿渲了两次（红一次、绿一次）")
    fed = str(llm.seen[-1])
    check("闸⑭" in fed and "1 个先后变化段" in fed and "0.4" in fed,
          "把实测段数与判据间隔喂回写手")
    check(out["unwritten"] == [], "s04 不算失败")

    print("⑫ 闸⑭ 重写一次仍红 → 照收但明说不是通过（堵死比白烧更坏）")
    spec = spec_with({"id": "s05", "text": "一句话文案", "brief": "一台机器"})
    client, _, renders = fake_client([True, True], [MOTION_REJECT] * 2)
    llm = FakeLLM(["```tsx\n" + GOOD + "\n```"] * 2)
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(len(renders) == sw.WRITER_MOTION_RETRIES + 1,
          f"草稿真渲了 {len(renders)} 次（首稿 + {sw.WRITER_MOTION_RETRIES} 次重写）")
    check(spec["shots"][0]["code"] == GOOD, "最后一稿照收（整份分镜不为一镜动效被拦死）")
    check(out["unwritten"] == [], "不算没写成")
    check("闸⑭" in out["notes"][0] and "不是通过" in out["notes"][0],
          "回执点名闸⑭ 且明说不是通过")
    check(out["motion"]["s05"]["status"] == "rejected", "读数按镜留账")
    check(out["motion"]["s05"]["events"][0]["start_sec"] == 0.0, "账上带着实测起点")

    print("⑬ 闸⑭ 未判＝照收但明说不是通过")
    spec = spec_with({"id": "s06", "text": "一句话文案", "brief": "一台机器"})
    client, _, _ = fake_client([True], [{"status": "undetermined", "samples": 0,
                                        "reason": "ffmpeg 抽帧失败"}])
    llm = FakeLLM(["```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(spec["shots"][0]["code"] == GOOD, "代码收下了")
    check(out["unwritten"] == [], "不算失败")
    check("未判" in out["notes"][0] and "不是通过" in out["notes"][0],
          "回执如实写未判")

    print("⑭ 草稿档压线的「过」不算过 → 按读数重写（成片档会掉到线下）")
    spec = spec_with({"id": "s07", "text": "一句话文案", "brief": "一架天平"})
    client, _, renders = fake_client([True, True], [MOTION_WEAK, MOTION_OK])
    llm = FakeLLM(["```tsx\n" + GOOD + "\n```"] * 2)
    out = asyncio.run(sw.write_shot_code(spec, llm=llm, client=client, parallel=1))
    check(len(renders) == 2, "压线那稿没有被直接收下")
    fed = str(llm.seen[-1])
    check("压线" in fed and "1.63" in fed and str(sw.WRITER_MOTION_MARGIN) in fed,
          "把最淡那一段的峰值与余量写进重写要求")
    check(spec["shots"][0]["code"] == GOOD, "加强后的第二稿被收下")

    print("⑮ 「差一点」的那处要抄给写手（不然它只会重排时间轴）")
    acct = dict(MOTION_REJECT)
    acct["near"] = [{"start_sec": 4.56, "peak_diff": 1.23}]
    txt = sw._motion_feedback(acct)
    check("差一点" in txt and "4.56" in txt and "1.23" in txt,
          f"亚门槛峰值连同起点一起喂回去：{txt.splitlines()[-1][:80]}…")
    check("第二处要和第一处一样满" in txt, "改法点名「两处一样满」，不是「再排一遍时间轴」")

    print("⑯ 跨镜连续：按渲染侧注入的全片时钟算步态/位移，不写死起点")
    spec = spec_with(
        {"id": "s01", "text": "第一句", "min_duration_sec": 6.0},
        {"id": "s02", "text": "第二句", "min_duration_sec": 7.0},
        {"id": "s03", "text": "第三句"})
    shot = spec["shots"][1]
    text = sw._shot_block(shot, index=1)
    check("getInputProps()" in text and "filmStartSec" in text,
          "起点由渲染侧注入（真实时长由配音决定，写死在代码里每镜都会偏）")
    check("filmStartSec + frame/fps" in text,
          "写清全片时钟的算法")
    check("不要写死起点秒数" in text, "明确禁止在代码里写死估算的起点")
    check("不要每镜从 0 起" in text, "写清这是跨镜连续量的时钟")
    check("这一镜在整片时间轴上的起点：" not in text,
          "不把估算的 min_duration 当真相写进提示词（配音时长会变）")

    print("⑰ 分镜申报的动效要进写手任务块（不然它只能猜，猜完被草稿闸打回）")
    spec = spec_with({"id": "s01", "text": "一句话文案",
                      "brief": "一只旧木箱，盖子半开",
                      "motion": [{"kind": "箱子从画面左侧滑入", "anchors": ["subtitle:1"]},
                                 {"kind": "盖板自上翻开", "anchors": ["subtitle:3"]}]})
    text = sw._shot_block(spec["shots"][0], index=0)
    check("申报的动效" in text, "任务块里有一段「这一镜申报的动效」")
    check("箱子从画面左侧滑入" in text and "盖板自上翻开" in text,
          "两条动效按顺序逐条写进任务块")
    check("subtitle:1" in text and "subtitle:3" in text,
          "锚（它服务哪句话/哪个字段）一并给出，写手知道每一拍挂在哪儿")
    check("每一条都要真的在像素上发生" in text, "明说这些不是参考而是必须发生的")
    check("0.4 秒" in text and "80%" in text,
          "把草稿闸的判据（间隔与收住时刻）写在同一段里，写第一稿就能过")
    bare = sw._shot_block(spec_with({"id": "s02", "text": "没有申报动效"})["shots"][0],
                          index=1)
    check("申报的动效" not in bare, "没申报动效的镜不多出这一段（老片子字节不变）")

    print()
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
