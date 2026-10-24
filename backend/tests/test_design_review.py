# -*- coding: utf-8 -*-
"""视觉评审（`motion/design_review.py`）与带批评的重写（`shot_writer.rewrite_shot_code`）。

这一层是 M2 的「画 → 看 → 改」闭环。要证的事：

① 评审回话的形状：只有五行分数齐全、`pass` 是布尔的那一行 JSON 才算判过；
② 三态：通过 / 红（带 why 与 fix）/ 未判（回话乱、图读不出、调用炸）——**未判不算通过**；
③ 开关：spec 那一栏优先，其次环境变量，缺省开；
④ 重写预算：红镜最多改 `limit` 镜，超出的点名进 skipped；一镜炸了不带塌整份；
⑤ 单镜重写：过 lint 才覆盖旧代码；lint 未判或两稿都不过 → 保留上一版能跑的代码；
⑥ 每次调用都记账。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_design_review.py``
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs                  # noqa: E402
from storyline_server.motion import design_review as dr             # noqa: E402
from storyline_server.motion import shot_writer as sw                # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def verdict(passed: bool, *, why: str = "", fix: str = "") -> str:
    scores = {"craft": 4, "composition": 3, "light": 4, "typography": 3, "not_ppt": 5}
    if not passed:
        scores = {"craft": 2, "composition": 2, "light": 2, "typography": 2, "not_ppt": 2}
    body = "{" + ", ".join(f'"{k}": {v}' for k, v in scores.items()) + "}"
    return ('{"pass": %s, "scores": %s, "why": "%s", "fix": "%s"}'
            % ("true" if passed else "false", body, why, fix))


GOOD = """import React from 'react';
import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { CaptionText } from '@dg/film';

const Shot = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ backgroundColor: '#111' }}>
      <svg viewBox="0 0 1920 1080" width={1920} height={1080}>
        <circle cx={960 + frame} cy={540} r={200} fill="none" stroke="#fff" />
      </svg>
      <CaptionText />
    </AbsoluteFill>
  );
};
export default Shot;"""

OLD = "const Shot = () => <div>上一版</div>;\nexport default Shot;"


class VisionLLM:
    """按「这一镜的文案」分发预置回话（评审是并发调的，不能按调用顺序发）。"""

    def __init__(self, replies: dict[str, str], *, boom: str = "") -> None:
        self.replies = replies
        self.boom = boom
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.seen.append(list(messages))
        ask = messages[1]["content"][0]["text"]
        if self.boom and self.boom in ask:
            raise RuntimeError("模型网关 502")
        for key, reply in self.replies.items():
            if key in ask:
                return SimpleNamespace(content=reply, usage={
                    "model": "deepseek-flash", "prompt_tokens": 1200,
                    "completion_tokens": 120})
        return SimpleNamespace(content="看着还行", usage={
            "model": "deepseek-flash", "prompt_tokens": 1200, "completion_tokens": 20})


class SeqLLM:
    """按顺序吐回话（重写是串行重试，顺序是确定的）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.seen.append(list(messages))
        text = self.replies.pop(0) if self.replies else "```tsx\n" + GOOD + "\n```"
        return SimpleNamespace(content=text, usage={
            "model": "deepseek-flash", "prompt_tokens": 400, "completion_tokens": 800})


def fake_client(verdicts: list[bool | None]):
    calls: list[str] = []

    def lint(code, declared=None):
        calls.append(code)
        ok = verdicts.pop(0) if verdicts else True
        if ok is None:
            raise RuntimeError("容器不通")
        if ok:
            return {"ok": True, "errors": [], "declared": declared or []}
        return {"ok": False, "errors": [{"rule": "dg/no-import",
                                         "line": 2, "msg": "不许这么写"}]}
    return {"lint": lint}, calls


def spec_with(*shots):
    return cs.validate_code_spec({
        "aspect": "landscape", "fps": 30, "narration": False,
        "palette": "deep-ocean", "style": "杂志编辑风：左对齐网格、大留白、单一强调色",
        "shots": list(shots)})[0]


def env_with(**pairs: str | None):
    """临时改环境变量；返回一个 restore 函数。"""
    saved = {k: os.environ.get(k) for k in pairs}
    for k, v in pairs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def main() -> int:
    print("① 评审回话的形状")
    got = dr._parse_review("说明\n" + verdict(True) + "\n结束")
    check(got is not None and got["pass"] is True, "夹在散文里的那一行也取得到")
    check(got is not None and set(got["scores"]) == set(dr.RUBRIC), "五行分数齐全")
    red = dr._parse_review(verdict(False, why="一笔等宽线", fix="换成笔刷"))
    check(red is not None and red["why"] == "一笔等宽线" and red["fix"] == "换成笔刷",
          "why / fix 原样带回来")

    check(dr._parse_review("没有 JSON") is None, "没有 JSON → 未判")
    check(dr._parse_review('{"pass": "yes", "scores": {}}') is None,
          "pass 不是布尔 → 未判")
    bad = verdict(True).replace('"craft": 4', '"craft": 9')
    check(dr._parse_review(bad) is None, "分数越界 → 未判")
    missing = verdict(True).replace(', "not_ppt": 5', "")
    check(dr._parse_review(missing) is None, "少一条分数 → 未判")
    check(dr._parse_review(verdict(True, why="字" * 200))["why"] == "字" * 80,
          "why 截到 80 字")

    print()
    print("② review_stills：通过 / 红 / 未判 三态")
    with tempfile.TemporaryDirectory() as tmp:
        stills = {}
        for sid in ("s01", "s02", "s03"):
            p = Path(tmp) / f"{sid}.jpg"
            p.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg " + sid.encode())
            stills[sid] = str(p)
        stills["s04"] = str(Path(tmp) / "不存在.jpg")
        shots = [
            {"id": "s01", "text": "好的那一镜", "subject": "一个人",
             "entities": ["一个人", "一把椅子"]},
            {"id": "s02", "text": "差的那一镜", "subject": "一面镜子",
             "entities": ["镜子", "两个人"]},
            {"id": "s03", "text": "乱回的那一镜", "subject": ""},
            {"id": "s04", "text": "图读不出的那镜", "subject": ""},
        ]
        llm = VisionLLM({
            "好的那一镜": verdict(True),
            "差的那一镜": verdict(False, why="主体缩在角落", fix="主体放大到画幅六成"),
        })
        out = asyncio.run(dr.review_stills(
            shots=shots, stills=stills, art={"brush": "pencil"},
            prop="一枚钉在墙上的读数表",
            style="像素赛博朋克：16 位像素网格为底",
            grammar="像素网格 8px；色数 ≤6；硬边；左上单一光源", llm=llm))
        check(out["checked"] == 2, f"判过 2 镜：{out['checked']}")
        check(out["passed"] == ["s01"], f"通过的是 s01：{out['passed']}")
        check([f["shot"] for f in out["failed"]] == ["s02"], "红的是 s02")
        check(out["failed"][0]["why"] == "主体缩在角落"
              and "六成" in out["failed"][0]["fix"], "红镜带着 why 与 fix")
        check(out["unjudged"] == ["s03", "s04"],
              f"乱回与图读不出都记未判：{out['unjudged']}")
        check(set(out["scores"]) == {"s01", "s02"}, "只给真判过的镜留分数")
        check(out["cost_yuan"] > 0, f"花费记下来了：¥{out['cost_yuan']}")
        check(llm.calls == 3, f"图读不出那一镜没发请求（只发了 {llm.calls} 次）")
        check("整片美术方向" in llm.seen[0][1]["content"][0]["text"],
              "整片美术方向进了提问")
        check("一枚钉在墙上的读数表" in llm.seen[0][1]["content"][0]["text"],
              "贯穿装置进了提问（评审才知道它在不在）")
        check("像素赛博朋克" in llm.seen[0][1]["content"][0]["text"],
              "整片风格与五条一起问（不再单独跑一趟风格对照）")
        check("像素网格 8px" in llm.seen[0][1]["content"][0]["text"],
              "风格落地也进了提问（评审能按同一套做法判像不像）")
        check("一个人、一把椅子" in llm.seen[0][1]["content"][0]["text"],
              "实体清单进了提问（评审逐件找，少一件就红）")
        check("逐一" in dr._REVIEW_SYSTEM or "逐个" in dr._REVIEW_SYSTEM,
              "评审话术要求逐个实体对账，不是只看有没有画")
        check(llm.seen[0][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"), "静帧以 base64 图块发出去")

    print()
    print("③ 调用炸了 → 未判，不是通过")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s09.jpg"
        p.write_bytes(b"x")
        llm = VisionLLM({"炸掉的那一镜": verdict(True)}, boom="炸掉的那一镜")
        out = asyncio.run(dr.review_stills(
            shots=[{"id": "s09", "text": "炸掉的那一镜"}], stills={"s09": str(p)}, llm=llm))
        check(out["unjudged"] == ["s09"] and not out["passed"],
              "调用抛错 → unjudged，passed 为空")

    print()
    print("④ review_enabled：spec 优先，其次环境变量，缺省开（2026-10-22：看片是默认形态的柱子）")
    restore = env_with(MOTION_VISUAL_REVIEW=None, MOTION_VISUAL_ROUNDS=None)
    try:
        check(dr.review_enabled({}) is True, "都没配 → 开（按 RATE 抽样）")
        check(dr.review_enabled({"visual_review": False}) is False,
              "spec 里显式关 → 关")
        check(dr.review_enabled({"visual_review": True}) is True, "spec 开 → 开")
        os.environ["MOTION_VISUAL_REVIEW"] = "0"
        check(dr.review_enabled({}) is False, "环境变量 0 → 关")
        check(dr.review_enabled({"visual_review": True}) is True, "spec 开赢过环境变量")
        check(dr.review_enabled({"visual_review": "no"}) is False,
              "spec 里不是布尔 → 退回环境变量")
        os.environ["MOTION_VISUAL_REVIEW"] = "false"
        check(dr.review_enabled({}) is False, "环境变量 false → 关")
        os.environ["MOTION_VISUAL_REVIEW"] = "1"
        check(dr.review_enabled({}) is True, "环境变量 1 → 开")

        print("\n④′ 看几轮：缺省 2，spec 可覆盖，夹在 1~4")
        os.environ.pop("MOTION_VISUAL_REVIEW", None)
        check(dr.review_rounds({}) == 2, f"缺省两轮：{dr.review_rounds({})}")
        check(dr.review_rounds({"visual_review_rounds": 1}) == 1, "spec 写 1 就一轮")
        check(dr.review_rounds({"visual_review_rounds": 9}) == 4, "上界夹到 4")
        check(dr.review_rounds({"visual_review_rounds": 0}) == 1, "下界夹到 1")
        check(dr.review_rounds({"visual_review_rounds": "3"}) == dr.REVIEW_ROUNDS,
              "不是整数（字符串）→ 退回缺省，不猜")
        check(dr.review_rounds({"visual_review_rounds": True}) == dr.REVIEW_ROUNDS,
              "布尔不算整数（True 不等于 1 轮）")

        print("\n④″ 同一句意见：改过一次还红就停手")
        check(dr.same_fix("放大主体", " 放大主体 ") is True, "只差空白 → 同一句")
        check(dr.same_fix("放大主体", "换个光源") is False, "换了个说法 → 不是同一句")
        check(dr.same_fix("", "") is False, "两边都空 → 没有「同一句」可言")
        check(dr.same_fix(None, "放大主体") is False, "上一轮没有意见 → 不算同一句")
    finally:
        restore()

    print()
    print("⑤ rewrite_failed：预算、跳过、成本")
    shots = [{"id": f"s{i:02d}", "text": f"第{i}镜", "brief": f"第{i}镜的画面",
              "code": OLD} for i in range(1, 7)]
    spec = {"aspect": "landscape", "fps": 30, "shots": shots}
    rewritten: list[str] = []
    seen_stills: list[tuple[str, object]] = []

    async def fake_rewrite(shot, *, critique, spec, user_id="", conversation_id="",
                           llm=None, client=None, still=None, user_request=""):
        sid = str(shot["id"])
        check(user_request == "一只橘猫蹲在便利店门口",
              f"{sid} 的重写带上了用户原话：{user_request!r}")
        rewritten.append(sid)
        seen_stills.append((sid, still))
        if sid == "s03":
            raise RuntimeError("网关 502")
        if sid == "s04":
            return {"ok": False, "note": "闸① 未判，保留上一版", "cost_yuan": 0.02, "calls": 1}
        shot["code"] = GOOD + f"\n// {sid}"
        check("上一版静帧的问题" in critique or "改法" in critique,
              f"{sid} 的重写带上了评审意见")
        return {"ok": True, "note": "", "cost_yuan": 0.01, "calls": 1}

    origin = sw.rewrite_shot_code
    sw.rewrite_shot_code = fake_rewrite
    try:
        failed = [{"shot": f"s0{i}", "why": "太小", "fix": "放大"} for i in (1, 2, 3, 4, 5)]
        failed.append({"shot": "s99", "why": "不在这一份里", "fix": ""})
        out = asyncio.run(dr.rewrite_failed(spec=spec, failed=failed, limit=3,
                                            llm=object(), client={},
                                            stills={"s01": "prev_s01.jpg"},
                                            user_request="一只橘猫蹲在便利店门口"))
    finally:
        sw.rewrite_shot_code = origin
    check(rewritten == ["s01", "s02", "s03"], f"只改预算内的三镜：{rewritten}")
    check(("s01", "prev_s01.jpg") in seen_stills and ("s02", None) in seen_stills,
          "上一版静帧按镜号交给写手，没有的镜就是 None")
    check(out["skipped"] == ["s03", "s04", "s05"],
          f"炸了的、没写成的、超预算的都点名：{out['skipped']}")
    check(out["rewritten"]["s01"]["ok"] is True and out["rewritten"]["s03"]["ok"] is False,
          "逐镜结果留在账上")
    check("炸了" in out["rewritten"]["s03"]["note"], "炸了的带原因")
    check(out["cost_yuan"] == 0.02, f"花费加起来：¥{out['cost_yuan']}")
    check("s99" not in out["skipped"], "不在这一份分镜里的镜不算跳过")
    check(str(spec["shots"][0]["code"]).endswith("// s01"), "写成的镜原地换掉了代码")
    check(spec["shots"][2]["code"] == OLD, "炸了的那镜保留上一版")

    print()
    print("⑤-b 并行重写、缺省不封顶")
    shots = [{"id": f"p{i:02d}", "text": f"第{i}镜", "brief": "x", "code": OLD}
             for i in range(1, 6)]
    spec = {"aspect": "landscape", "fps": 30, "shots": shots}
    active = 0
    peak = 0

    async def slow_rewrite(shot, *, critique, spec, user_id="", conversation_id="",
                           llm=None, client=None, still=None, user_request=""):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        shot["code"] = GOOD
        return {"ok": True, "note": "", "cost_yuan": 0.0, "calls": 1}

    saved_cap = dr.MAX_REWRITES
    sw.rewrite_shot_code = slow_rewrite
    dr.MAX_REWRITES = None
    try:
        failed = [{"shot": s["id"], "why": "偏抽象", "fix": "画具体物件"} for s in shots]
        out = asyncio.run(dr.rewrite_failed(
            spec=spec, failed=failed, llm=object(), client={}))
    finally:
        sw.rewrite_shot_code = origin
        dr.MAX_REWRITES = saved_cap
    check(peak >= 2, f"重写是并发的（峰值 {peak} 路）")
    check(len(out["rewritten"]) == 5 and not out["skipped"],
          "缺省不封顶：红几镜改几镜")

    print()
    print("⑥ rewrite_shot_code：过 lint 才覆盖，不然保留上一版")
    spec = spec_with({"id": "s11", "text": "一句话", "brief": "一个圆"})
    spec["shots"][0]["code"] = OLD
    client, calls = fake_client([True])
    llm = SeqLLM(["```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.rewrite_shot_code(spec["shots"][0], critique="线条太硬",
                                           spec=spec, llm=llm, client=client))
    check(out["ok"] is True and spec["shots"][0]["code"] == GOOD, "过 lint → 覆盖旧代码")
    check(len(calls) == 1 and out["calls"] == 1, "只判一次 lint、只调一次模型")
    check(out["cost_yuan"] > 0, f"花费记下来了：¥{out['cost_yuan']}")
    ask = llm.seen[0][1]["content"]
    check("线条太硬" in ask and OLD in ask, "评审意见与上一版代码都进了提问")

    spec = spec_with({"id": "s12", "text": "一句话", "brief": "一个圆"})
    spec["shots"][0]["code"] = OLD
    client, calls = fake_client([False, False])
    llm = SeqLLM(["```tsx\n" + GOOD + "\n```", "```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.rewrite_shot_code(spec["shots"][0], critique="太小",
                                           spec=spec, llm=llm, client=client))
    check(out["ok"] is False and spec["shots"][0]["code"] == OLD,
          "两稿都不过 → 旧代码一个字节没动")
    check(len(calls) == sw.REWRITE_ATTEMPTS == 2, f"lint 判了 {len(calls)} 次")
    check(any("闸① lint 没过" in str(m) for m in llm.seen[-1]),
          "第二次调用带上了 lint 的原始报错")
    check("2 次仍没过" in out["note"], "回执明说几次没过")

    spec = spec_with({"id": "s13", "text": "一句话", "brief": "一个圆"})
    spec["shots"][0]["code"] = OLD
    client, _ = fake_client([None])
    llm = SeqLLM(["```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.rewrite_shot_code(spec["shots"][0], critique="太小",
                                           spec=spec, llm=llm, client=client))
    check(out["ok"] is False and spec["shots"][0]["code"] == OLD,
          "lint 未判 → 保留上一版")
    check("未判" in out["note"] and "保留上一版" in out["note"], "回执写清未判")

    spec = spec_with({"id": "s14", "text": "一句话", "brief": "一个圆"})
    spec["shots"][0]["code"] = OLD
    client, calls = fake_client([True])
    llm = SeqLLM(["", "```tsx\n" + GOOD + "\n```"])
    out = asyncio.run(sw.rewrite_shot_code(spec["shots"][0], critique="太小",
                                           spec=spec, llm=llm, client=client))
    check(out["ok"] is True and spec["shots"][0]["code"] == GOOD,
          "第一趟没吐代码 → 要一次，第二趟收下")
    check(len(calls) == 1, "没吐代码的那趟不判 lint")

    print()
    print("⑥-b 带上上一版静帧：写手看见的是真渲出来的那一张")
    spec = spec_with({"id": "s15", "text": "一句话", "brief": "一个圆"})
    spec["shots"][0]["code"] = OLD
    client, _ = fake_client([True])
    llm = SeqLLM(["```tsx\n" + GOOD + "\n```"])
    work = Path(tempfile.mkdtemp())
    prev = work / "prev.jpg"
    prev.write_bytes(b"\xff\xd8\xff\xd9")
    out = asyncio.run(sw.rewrite_shot_code(spec["shots"][0], critique="线条太硬",
                                           spec=spec, llm=llm, client=client, still=prev))
    content = llm.seen[0][1]["content"]
    check(out["ok"] is True, "带图重写照常过 lint")
    check(isinstance(content, list)
          and any(b.get("type") == "image_url" for b in content),
          "上一版静帧作为 image_url 进了这次重写")
    check("线条太硬" in content[0].get("text", ""), "批评还在同一趟里")
    check("上一版渲出来的静帧" in content[0].get("text", ""), "明说这张图就是它自己画的")

    print()
    print("⑦ 风格包：五键照旧，但判读口径按「刻意重复构图」改")
    # 加第六个评分键是最自然的写法，也是最坏的一种：`_parse_review` 恰好要五个，
    # 多一个键 → 整镜变「未判」，13 镜全部悄悄不做评审。
    check(len(dr.RUBRIC) == 5, "RUBRIC 仍是五条")
    pack = {
        "name": "style_demo", "display": "剪影年轮",
        "manifest": {"template": "silhouette-history", "repeat_composition": True,
                     "recurring_pose": "走 → 停 → 前倾",
                     "recurring_prop": "0.70W/0.34H 的发光矩形",
                     "caption": {"look": "overlay"}, "inscription": {"position": "top-right"}},
        "body": "同一个姿势穿过时代背景。",
    }
    ask = dr._ask(text="火光照亮第一张脸", subject="篝火", art={},
                  style="剪影", grammar="",
                  brief="画面正中一株菊花，全片无人物。",
                  skill_contract=pack)
    check("全片无人物" in ask and "brief 里说**不要**的东西出现在画面里" in ask,
          "评审看见 brief 里的排除项：多了人物就判红，而不是要求补人物")
    check("brief 里说**不要**的东西" not in dr._ask(
              text="一句话", subject="一个圆", art={}),
          "没给 brief 时不多这一段（老评审提示逐字不变）")
    check("用户选定的风格包：「剪影年轮」" in ask, "评审提示里点明用户选的那份")
    check("0.70W/0.34H 的发光矩形" in ask and "走 → 停 → 前倾" in ask,
          "贯穿道具与姿势逐条进评审（对不上才算缺点）")
    check("同一个姿势穿过时代背景。" in ask, "风格包正文也进了评审")
    check("和上一镜相同不是缺点" in ask or "一致" in ask,
          "repeat_composition 时构图按「与全片一致」判，不按「要有变化」判")
    check("第六条" in ask or "五项" in ask or "五条" in ask,
          "明说仍是五项评分（防止模型自作主张加一维）")
    pack["manifest"].update({"review_note": "字幕压到主体就判红",
                             "writer_note": "写手专用口径"})
    ask_note = dr._ask(text="一句话", subject="一个圆", art={}, skill_contract=pack)
    check("用户补充：写给评审：字幕压到主体就判红" in ask_note,
          "前端写给评审的补充要求进评审提示")
    check("写手专用口径" not in ask_note,
          "写手那一层的补充要求不进评审（各看各的）")

    judge = dict({"pass": True, "why": "像", "fix": ""},
                 scores={k: 4 for k in dr.RUBRIC})
    parsed = dr._parse_review(__import__("json").dumps(judge))
    check(parsed is not None and parsed["pass"] is True and parsed["scores"]["craft"] == 4,
          "带风格包的正常回话仍是五键解析")
    no_pack = dr._ask(text="一句话", subject="一个圆", art={})
    check("用户选定的风格包" not in no_pack,
          "没选风格包时不多这一段（老评审提示逐字不变）")

    print()
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
