# -*- coding: utf-8 -*-
"""T31 帧通道（``storyline_server.motion.judge_channel``）的离线用例。

**这份文件判的是一件「本来悬空」的事。** 闸㉖ 回看环（``review_loop.py``）从落地那天起
就把两个名字写在常量里——``FRAME_CHANNEL_MODULE = "storyline_server.motion.judge_channel"``
与 ``FRAME_CHANNEL_ENTRY = "judge_frame"``——然后**探它**。探不到时的行为是 Q113 那条
（``undetermined`` 并阻断出片），所以这个模块缺席的表现不是「少一个功能」，是
**每一片有红镜的片子都出不来**。这一份把那个洞堵上，并把它堵成什么样钉死。

分六节：

① **入口形状**：``review_loop.frame_channel()`` 今天拿得到模块与入口。两个名字**单源**
   ——探测问的名字与模块自己的 ``__name__`` / 入口名逐字相等。只探模块不探入口，
   会出现「模块在、入口改名了」这种探测说可用、调用当场炸的形状（review_loop 自己
   的注释点了这条，这里按它断言）。
② **``judge_frame`` 的调用形状与三出口**：它拿到的是**画面本身** + 五栏几何读数 +
   既有闸判红的原文 + 那一镜的 spec（含现有代码）。三出口是
   「真改出了新代码」/「看了，不用改」/「通道炸了」——**后两种都不是通过**，
   它们与「没跑」在回执上是三句话。这里逐条证。
③ **``self_test`` 的盲测**：两支夹具（单个放大部件 / 两部件场景）必须被**分得开**。
   分不开 = 判据不可用 = ``ok`` 必须是 ``None``（未判），**不许**拿一个假的自证把
   回看环放行（review_loop 的 ``self_test`` 注释原文）。
④ **静帧审美评审（层 D2）**：把闸④ 从「带子里有没有东西」抬到「画得好不好」的那一半。
   判据面**从技能正文读**（``aesthetic-rules.md`` 的 Q 组 / ``final-review.md`` 的 Q 组），
   不在这里抄第二份——抄一份的表现是「正文改了、评审还在用旧条目」。
   三出口同样分明：判红 / 判绿 / 未判；**低置信与没配置都归未判**（宁可漏纠不误伤，
   与 ``judge.py`` 的 ``_MIN_CONFIDENCE`` 同一条纪律）。
⑤ **纪律（结构断言）**：① 不另起 judge 客户端——模块里必须**复用**
   ``agent_framework.judge``，且源码里不许出现自己发 HTTP 去裁决的字样（Q90/Q91）；
   ② 任何失败 → 未判，绝不抛出去把交付带塌。
⑥ **接线**：``review_loop.capability()`` 的 ``frame`` 那一半从此为真——这一条是
   「洞堵上了」的直接证据，不是对上面几条的重复。
⑦ **接线（层 D2）**：闸④-b 静帧审美评审真的挂在出片链上、开关关着时账怎么写。
⑧ **⑲-b 身份判据（2026-10-17）**：``judge_identity`` 判的是「画面上那个东西是不是文案
   说的那个」——⑯ 逐字写着自己判不了这一格，闸㉝ 只判「有没有落到库里一个具名件上」。
   这一节钉的是三件事：**问法是盲的**（盲描述那一趟的输入里没有申报的 ``subject``，
   否则拿回来的是一句附和）、**认不出就是不像**（``by="blind"``，且不再花第二次调用）、
   **未判不进红镜名单**（拿它当红镜＝让模型在没有理由的情况下改代码）。

为什么这里**不真跑一次多模态调用**：这一份判的是**通道的形状与三出口**，而真实调用要
出网、要密钥、要花钱——把它塞进离线用例，得到的会是「有密钥才绿」的用例。真链路的
端到端取证另做（同 ``test_code_texture`` 的真跑 ffmpeg 那一节：那一条判的是像素，
这一条判的是接线）。

跑法：容器内 ``PYTHONPATH=/app python tests/test_motion_judge_channel.py``。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_render as cr            # noqa: E402
from storyline_server.motion import judge_channel as jc          # noqa: E402
from storyline_server.motion import review_loop as rl            # noqa: E402

_fails = 0
_checks = 0
ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "examples" / "retired" / "motion_explainer"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


# ---------------------------------------------------------------------------
# 替身：一个假 LLM（生成那一半）与一个假 judge 模块（裁决那一半）
# ---------------------------------------------------------------------------

class FakeLLM:
    """只实现 ``complete(messages)`` —— ``judge_channel`` 对 LLM 的全部要求就这一条。

    记下每次调用收到的 messages，好让「画面到底有没有递过去」这件事可断言：
    一个只把文字递过去、把 ``image`` 悄悄丢掉的实现，在只断言返回值的用例下是全绿的。
    """

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else ""
        return types.SimpleNamespace(content=text, tool_calls=[], usage=None)


def _fake_judge(*, verdict=None, confidence=0.0, calls: list | None = None,
                configured: bool = True, reason: str = ""):
    """假 ``agent_framework.judge`` 模块。

    ``verdict=None`` 表示「裁决跑不通」（等价于真模块任何失败都回 None 的那条纪律）。
    """
    mod = types.ModuleType("fake_judge")

    async def get_judge():
        return mod if configured else None

    async def judge(question, *, text="", facts=None, timeout=6.0, user_id=""):
        if calls is not None:
            calls.append({"question": question, "text": text, "facts": dict(facts or {})})
        if verdict is None:
            return None
        return {"verdict": bool(verdict), "confidence": float(confidence),
                "reason": reason}

    mod.get_judge = get_judge
    mod.judge = judge
    return mod


def _png_bytes() -> bytes:
    """一张最小的合法 PNG（1×1），用来当「帧」——这一节不判像素，判的是它有没有被递过去。"""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
        "IQAAAABJRU5ErkJggg==")


def _shot(**over) -> dict:
    base = {"id": "s03", "text": "这一镜讲的是水分子怎么抱团",
            "subject": "水分子团簇", "subject_channel": "vector",
            "card": "effects/riso-print-hits", "derived_from": ["<original>"],
            "code": "export default function Shot(){return null;}"}
    base.update(over)
    return base


#: 读帧那一段的假回复。**读数里不许有结论**（不许有 good/score）——结论归 judge()，
#: 两段各干各的；这条夹具本身就是那个形状的示范。
_READING = ('{"subjects": 1, "text_readable": true, "overlap": false, '
            '"cheap_effect": false, "subject_present": true, "note": "一个居中的团簇"}')

#: ⑲-b 的三条盲描述假回复：看明白了 / 认不出是什么 / 看明白了但是别的东西。
#: 三条都是**观察员的话**，里面一个结论也没有——「像不像」是裁决那一段的事。
_BLIND_OK = ('{"thing": "一台带烟囱的机器", "kind": "器物", "others": [], '
             '"readable": true}')
_BLIND_UNREADABLE = ('{"thing": "一堆看不清的线", "kind": "说不清", "others": [], '
                     '"readable": false}')
_BLIND_MISS = ('{"thing": "一列方块", "kind": "符号", "others": ["一片底色"], '
               '"readable": true}')


class _MemCache:
    """只实现 ``fetch(key, dst)`` 的假切片缓存（``review_loop.cached_frame`` 的全部要求）。

    ``dst`` 收下了就丢掉：这一份判的是「帧有没有被取到、有没有被递过去」，
    不是「文件落没落到工作区」——那一条归切片缓存自己的用例。
    """

    def __init__(self, frames: dict):
        self.frames = dict(frames)

    async def fetch(self, key, dst):
        got = self.frames.get(str(key))
        return None if got is None else {"frame.png": got}


def _columns() -> dict:
    return {"subjects": 1, "subject_share": 0.31, "text_share": 0.12,
            "grid_text": 0, "grid_drawn": 0, "colors": 3}


async def main() -> int:
    real_llm = jc._llm
    real_judge = jc._judge_module
    try:
        # =====================================================================
        print("\n〔①〕帧通道入口：review_loop 探的那个名字，今天真的在")
        mod, why = rl.frame_channel()
        check(mod is not None,
              f"`{rl.FRAME_CHANNEL_MODULE}` 导得进来且带 `{rl.FRAME_CHANNEL_ENTRY}`"
              f"——探不到时闸㉖ 只能 undetermined 并阻断出片（Q113）"
              + (f"；实得：{why}" if mod is None else ""))
        check(mod is not None and mod.__name__ == rl.FRAME_CHANNEL_MODULE,
              "模块名单源：探测问的名字就是模块自己的名字（两处各写一遍就会漂）")
        check(getattr(mod, rl.FRAME_CHANNEL_ENTRY, None) is not None,
              f"入口 `{rl.FRAME_CHANNEL_ENTRY}` 在（review_loop 的探测分两步："
              "模块在不在、入口在不在——只探一步会出现「探测说可用、调用当场炸」）")

        # =====================================================================
        print("\n〔②〕judge_frame：调用形状与三出口")
        fake = FakeLLM(
            "```tsx\nexport default function Shot(){return <FilmLayout>水分子抱团</FilmLayout>;}\n```",
            "NO_CHANGE",
            "",
        )
        jc._llm = lambda: fake
        got = await jc.judge_frame(
            gate=rl.GATE, shot="s03", image=_png_bytes(), columns=_columns(),
            reasons=["闸⑲-a：主体占比 8% 低于下限，画面里认不出申报的主体"],
            shot_spec=_shot(), round=1,
            terms_field=rl.SUBJECT_TERMS_FIELD, subject_terms=None)
        check(isinstance(got, dict) and "ok" in got and "code" in got and "reason" in got,
              f"返回的是三键一条（ok/code/reason），实得 {sorted(got) if isinstance(got, dict) else type(got).__name__}")
        code = got.get("code") or ""
        check(code.startswith("export default") and "```" not in code,
              "出口一「真改出了新代码」：围栏剥掉了，交出来的是能直接落盘的 TSX"
              f"——实得前 40 字：{code[:40]!r}")
        blob = fake.calls[0]
        imgs = [p for m in blob for p in (m.get("content") if isinstance(m.get("content"), list) else [])
                if isinstance(p, dict) and p.get("type") == "image_url"]
        check(len(imgs) == 1,
              "画面**真的**递过去了（这条环存在的全部理由：几何读数说不清画面上是一枚"
              "徽章还是一张脸）——只递文字的实现会在这一条上现形")
        check(any("闸⑲-a" in str(m.get("content")) for m in blob),
              "既有闸判红的**原文**也递过去了：模型据此知道该改什么，而不是自己猜")

        got2 = await jc.judge_frame(gate=rl.GATE, shot="s03", image=_png_bytes(),
                                    columns=_columns(), reasons=["闸㉕：部件拼场景"],
                                    shot_spec=_shot(), round=2)
        check(got2["ok"] is True and got2["code"] is None,
              "出口二「看了，不用改」：ok=True 且 code 为空——"
              "回看环据此记 not_run（账上没有第二次切片），**不是**通过")
        check(bool(got2.get("reason")),
              "这个出口必须带一句为什么（空话会被读成「模型没说话」）")

        got3 = await jc.judge_frame(gate=rl.GATE, shot="s03", image=_png_bytes(),
                                    columns=None, reasons=["闸㉑：动效窗口"],
                                    shot_spec=_shot(), round=2)
        check(got3["ok"] is False and got3["code"] is None,
              "出口三「模型没吐东西」：ok=False 且 code 为空（空回复不许被读成「不用改」）")

        # 通道炸了不许带塌整条片子
        class BoomLLM:
            async def complete(self, messages, tools=None):
                raise RuntimeError("上游 502")

        jc._llm = lambda: BoomLLM()
        got4 = await jc.judge_frame(gate=rl.GATE, shot="s03", image=_png_bytes(),
                                    columns=_columns(), reasons=["闸㉕"],
                                    shot_spec=_shot(), round=1)
        check(got4["ok"] is False and got4["code"] is None and "502" in (got4.get("why") or ""),
              "一次调用炸了**不抛**：如实回 ok=False + 原因（review_loop 的 ask_frame 也兜一层，"
              "但兜两层的代价只是一行，带塌整片子的代价是重烧）")

        # 传进去的帧是路径 / 字符串 / 字节，三种都得认。夹具必须**落一份真文件**：
        # 拿一个不存在的路径去断言，红的是夹具（读不到字节 → 空 data URL），
        # 而不是「路径这条路走不通」——那种红会把人引到错的地方去。
        probe = Path("/tmp/judge_channel_probe.png")
        probe.write_bytes(_png_bytes())
        for kind, val in (("路径", probe), ("字符串", str(probe))):
            fake2 = FakeLLM("NO_CHANGE")
            jc._llm = lambda: fake2
            await jc.judge_frame(gate=rl.GATE, shot="s03", image=val, columns=None,
                                 reasons=["x"], shot_spec=_shot(), round=1)
            blob2 = fake2.calls[0]
            url = next((p.get("image_url", {}).get("url", "") for m in blob2
                        for p in (m.get("content") if isinstance(m.get("content"), list) else [])
                        if isinstance(p, dict) and p.get("type") == "image_url"), "")
            check(url.startswith("data:image/"),
                  f"{kind}形式的帧也被包成 data URL 递过去（缓存那一路回的是路径，"
                  "硬要求 bytes 会让它在生产里静默取不到帧）")

        # =====================================================================
        print("\n〔③〕self_test：两支夹具必须被分得开，分不开就是未判")
        fake3 = FakeLLM("1", "2")
        jc._llm = lambda: fake3
        st = await jc.self_test(gate=rl.GATE, terms_field=rl.SUBJECT_TERMS_FIELD)
        check(isinstance(st, dict) and st.get("ok") is True,
              f"单部件 / 两部件两支被分得开 → ok=True，实得 {st.get('ok')}（{st.get('why') or ''}）")
        fake4 = FakeLLM("2", "2")
        jc._llm = lambda: fake4
        st2 = await jc.self_test(gate=rl.GATE, terms_field=rl.SUBJECT_TERMS_FIELD)
        check(st2.get("ok") is False,
              "分不开 → ok=False（**不是** None：分不开是「量过、判据不灵」，"
              "与「压根没做」在回执上是两句话）")

        class BoomLLM2:
            async def complete(self, messages, tools=None):
                raise RuntimeError("没网")

        jc._llm = lambda: BoomLLM2()
        st3 = await jc.self_test(gate=rl.GATE, terms_field=rl.SUBJECT_TERMS_FIELD)
        check(st3.get("ok") is None,
              "通道炸了 → ok=None（未判）——不许拿一个假的自证把回看环放行")

        # =====================================================================
        print("\n〔④〕静帧审美评审（层 D2）：判据面来自正文，三出口分明")
        ids = jc.criteria_ids()
        check(isinstance(ids, (tuple, list)) and len(ids) >= 8 and "Q1" in ids,
              f"判据编号从正文抽出来（{len(ids)} 条：{list(ids)[:4]}…）")
        body = (SKILL / "aesthetic-rules.md").read_text(encoding="utf-8")
        missing = [i for i in ids if f"### {i}." not in body]
        check(not missing,
              f"每一条编号在 `aesthetic-rules.md` 里都真有其人（缺的：{missing}）"
              "——在这里抄第二份判据面的表现是「正文改了、评审还在用旧条目」")
        dig = jc.criteria_digest()
        check("Q1" in dig and len(dig) > 200,
              f"判据摘要非空且带编号（{len(dig)} 字）")

        # 两段：先读帧（眼），再裁决（尺）。`judge()` 是文本契约，看不见像素——
        # 少了读帧那一段，「审美评审」就变成「拿一栏几何读数问好看吗」，那是编话不是看画。
        calls: list = []
        jc._llm = lambda: FakeLLM(_READING)
        jc._judge_module = lambda: _fake_judge(verdict=True, confidence=0.93, calls=calls)
        bad = await jc.review_still(shot=_shot(), image=_png_bytes(), columns=_columns())
        check(bad["verdict"] is True and bad["confidence"] == 0.93,
              "出口一「画得不好」：verdict=True（Noul 的语义是『与事实矛盾』，"
              "问句按这条写——红＝违反审美准则）")
        check(isinstance(bad.get("reading"), dict)
              and bad["reading"].get("subjects") == 1,
              "回执里带着**画面读数**——判红时人能看到模型当时看见了什么，"
              "而不是只看到一句结论")
        check("good" not in (bad.get("reading") or {})
              and "score" not in (bad.get("reading") or {}),
              "读数里没有结论字段：结论归 judge()，两段混在一起就会出现"
              "「观察员顺手把裁决也做了」而判据面其实没被用上")
        check(bool(calls) and "审美" in calls[0]["question"] and "Q1" in calls[0]["question"],
              "问句里点着判据编号问（只说『好看吗』得到的是废话）")
        check(calls[0]["facts"].get("subject_share") == 0.31
              and calls[0]["facts"].get("shot") == "s03",
              "五栏几何读数与镜号作为**客观事实**一并递过去（模型据此对照画面，"
              "而不是凭印象发言）")

        jc._judge_module = lambda: _fake_judge(verdict=None)
        und = await jc.review_still(shot=_shot(), image=_png_bytes(), columns=_columns())
        check(und["verdict"] is None and und.get("why"),
              "出口二「未判」：裁决跑不通 → verdict=None，**不许**折算成通过")

        jc._judge_module = lambda: _fake_judge(configured=False)
        und2 = await jc.review_still(shot=_shot(), image=_png_bytes(), columns=_columns())
        check(und2["verdict"] is None and "没配" in (und2.get("why") or ""),
              "出口三「没配置」：与「跑了但失败」分开报（judge.py 的纪律：未配置＝完全不启用）")

        jc._judge_module = lambda: _fake_judge(verdict=False, confidence=0.9)
        good = await jc.review_still(shot=_shot(), image=_png_bytes(), columns=_columns())
        check(good["verdict"] is False,
              "判绿：verdict=False（这一条不拦交付，它只是把闸④ 的射程抬高一档）")

        # 低置信不许判红：judge.py 已经在里面拦了（回 None），这里证的是**外层也不自作主张**
        jc._judge_module = lambda: _fake_judge(verdict=True, confidence=0.51)
        low = await jc.review_still(shot=_shot(), image=_png_bytes(), columns=_columns())
        check(low["verdict"] is True and low["confidence"] == 0.51,
              "置信度原样带回来（判不判红是调用方的规矩——这里不替 judge.py 重写一遍阈值）")

        # =====================================================================
        print("\n〔⑤〕纪律：不另起 judge 客户端；任何失败都不抛")
        src = Path(jc.__file__).read_text(encoding="utf-8")
        check("from agent_framework import judge" in src
              or "agent_framework.judge" in src,
              "复用既有的 `agent_framework.judge`（Q90/Q91：另起一个裁决客户端＝清单漂移）")
        banned = [w for w in ("urllib.request", "httpx", "aiohttp", "requests.post")
                  if w in src]
        check(not banned,
              f"源码里没有自己发 HTTP 去裁决的字样（{banned}）——"
              "裁决的键位/优先级链只在 `secrets.py` 那一处")
        check(await _never_raises(),
              "本模块的每一条出口都不抛（含 ⑲-b 那两条）：返回的是账，不是异常")

        # =====================================================================
        print("\n〔⑥〕接线：review_loop.capability() 的 frame 那一半从此为真")
        jc._judge_module = lambda: _fake_judge(configured=False)
        cap = await rl.capability()
        check(cap["frame"] is True and cap["frame_why"] == "",
              f"帧通道那一半不再报「没落地」（实得 frame={cap['frame']}、"
              f"why={cap['frame_why'][:60]!r}）")
        check(cap["available"] is False and cap["judge"] is False,
              "另一半（裁决）如实报没配——两半都真了才 available，"
              "少一半时 ㉖ 仍然 undetermined 并阻断（Q113 没被这条改动松掉）")

        # =====================================================================
        print("\n〔⑦〕接线：闸④-b 静帧审美评审真的挂在出片链上（默认关）")
        # 「能力没有调用方 = 等于没这个能力」（R19 收尾时在 code_patch 那条上吃过的教训）。
        # 这一节判的就是那个调用方在不在、开关关着时账怎么写。
        src_cr = Path(cr.__file__).read_text(encoding="utf-8")
        check('"still_review": still_receipt' in src_cr
              and "_still_review(" in src_cr,
              "出片回执里那一栏真的接上了（`render_code_motion` 有调用点、报告有那一栏）")

        ledger = [{"id": "s01"}, {"id": "s02"}]
        shots = [_shot(id="s01"), _shot(id="s02")]
        frames = {"s01": {"frame": probe}, "s02": {"frame": probe}}
        r_off = await cr._still_review({"shots": shots}, ledger, frames, scanned={})
        check(r_off["enabled"] is False and not r_off["shots"]
              and r_off["counts"]["not_run"] == 2,
              f"默认关：enabled=False、逐镜没有结论、记 not_run（实得 {r_off['counts']}）"
              "——「没开」既不是缺席（缺席＝压根不需要），更不是通过")
        check("没开" in (r_off.get("note") or ""),
              "关着的时候回执**明写**「这趟没开」（空着会被读成「没发现问题」）")

        spec_on = {"shots": shots, "still_review": True}
        jc._judge_module = lambda: _fake_judge(configured=False)
        r_nc = await cr._still_review(spec_on, ledger, frames, scanned={})
        check(r_nc["counts"]["undetermined"] == 2 and "做不了" in (r_nc.get("note") or ""),
              "开了但裁决通道没配：逐镜 undetermined + 明写做不了"
              "（不折算成通过、也不抛——它是提醒层不是交付门）")

        jc._llm = lambda: FakeLLM(_READING)
        jc._judge_module = lambda: _fake_judge(verdict=True, confidence=0.9)
        r_on = await cr._still_review(spec_on, ledger, frames, scanned={})
        check(r_on["counts"]["fail"] == 2 and r_on["shots"]["s01"]["verdict"] is True,
              f"开了且判红：逐镜 verdict=True、counts.fail=2（实得 {r_on['counts']}）")
        check(bool(r_on["notes"]) and "提醒" in r_on["notes"][0],
              "判红是**提醒**不是打回（像素上「有没有东西」仍由闸④ 管着出不出得去）")
        check(bool(r_on["criteria"]) and "Q1" in r_on["criteria"],
              "回执里带着判据编号——判红时人回头能查到是哪一条")

        jc._judge_module = lambda: _fake_judge(verdict=False, confidence=0.9)
        r_green = await cr._still_review(spec_on, ledger, frames, scanned={})
        check(r_green["counts"]["pass"] == 2 and not r_green["notes"],
              "判绿：counts.pass=2、没有提醒")

        # 没帧不许硬判：拿读数代替画面是这条链上最坏的一种「判过了」
        jc._judge_module = lambda: _fake_judge(verdict=True, confidence=0.9)
        r_noframe = await cr._still_review(spec_on, ledger, {}, scanned={})
        check(r_noframe["counts"]["undetermined"] == 2
              and all(s["verdict"] is None for s in r_noframe["shots"].values()),
              "帧没找到 → 逐镜未判（不许拿几何读数代替画面去判「好不好看」）")

        # =====================================================================
        print("\n〔⑧〕⑲-b 身份判据：画面上那个东西，是文案说的那个吗")
        # 这一节判的是「『必需像』这句话落在哪一条能自己红的判据上」。静态那半（闸㉝）
        # 只判「有没有落到库里一个具名件上」——一件 `<Figure/>` 摆在角落、主体仍是一团
        # 点阵，它不响。⑯ 的 docstring 逐字写着自己判不了这一格。所以这一节要证的是：
        # ⑲-b 真的在问身份，而且**问法是盲的**。
        check(callable(getattr(jc, jc.IDENTITY_ENTRY, None))
              and rl.IDENTITY_ENTRY == jc.IDENTITY_ENTRY,
              f"入口 `{jc.IDENTITY_ENTRY}` 在，且 review_loop 探的名字与它逐字相等"
              "（名字漂了就是「探测说在、调用说没有」）")
        check(rl.IDENTITY_KNOB != rl.REVIEW_KNOB and rl.IDENTITY_KNOB != "review",
              f"⑲-b 的开关是**单独一个**（`{rl.IDENTITY_KNOB}` ≠ `{rl.REVIEW_KNOB}`）："
              "并进 `review` 会让「打开回看」偷偷把开销从「每红镜一次」抬到「每镜一次」"
              "——改一个已有开关的价钱，比多一个开关危险得多")

        # —— 关着：一条调用都不许发 -------------------------------------------------
        jc._llm = lambda: FakeLLM(_BLIND_OK)
        quiet = FakeLLM(_BLIND_OK)
        jc._llm = lambda: quiet
        r_off2 = await rl.identity_targets(shots, enabled=False)
        check(not quiet.calls and r_off2["enabled"] is False
              and r_off2["counts"]["not_run"] == 2 and not r_off2["targets"],
              f"开关关着：一条调用都不发、逐镜记 not_run、红镜名单为空（实得 "
              f"{r_off2['counts']}，调用 {len(quiet.calls)} 次）")
        check("没开" in r_off2["why"] and "画得像" in r_off2["why"],
              "关着时**明写**这趟没开，并说清「这一栏没有结论、不许读成画得像」"
              "——「没开」不是缺席（缺席＝压根不需要），更不是通过")

        # —— 开着：盲描述里不许出现申报的 subject ------------------------------------
        jc._llm = lambda: FakeLLM(_BLIND_OK)
        jc._judge_module = lambda: _fake_judge(verdict=False, confidence=0.9)
        blind_calls = FakeLLM(_BLIND_OK)
        jc._llm = lambda: blind_calls
        got_like = await jc.judge_identity(shot=_shot(subject="水分子团簇"),
                                           image=_png_bytes())
        blob = " ".join(str(m.get("content")) for m in blind_calls.calls[0])
        check("水分子团簇" not in blob and "水分子" not in blob,
              "**盲**：盲描述那一趟的输入里没有申报的 `subject`（把答案递过去，拿回来的"
              "是一句附和——那条路上画成一团乱麻也有一半概率被夸「抓住了神韵」）")
        check(got_like["verdict"] is False and got_like["by"] == "judge",
              f"观察员说清了、裁决说「是它」→ verdict=False（实得 {got_like['verdict']}）")

        # —— 认不出是什么：直接判不像，且**不再花第二次调用** ------------------------
        jc._llm = lambda: FakeLLM(_BLIND_UNREADABLE)
        judge_calls: list = []
        jc._judge_module = lambda: _fake_judge(verdict=False, confidence=0.9,
                                               calls=judge_calls)
        got_unread = await jc.judge_identity(shot=_shot(subject="蒸汽机"),
                                             image=_png_bytes())
        check(got_unread["verdict"] is True and got_unread["by"] == "blind",
              f"认不出是什么 → 直接判**不像**，`by='blind'`（实得 {got_unread['by']!r}）"
              "——用户那句话是「必需像」：一个看画的人说不出画的是什么，它就不像")
        check(not judge_calls,
              "这一条**不再花第二次调用**（认不出这件事观察员自己就说了）")
        check("蒸汽机" in got_unread["reason"]
              and "一堆看不清的线" in got_unread["reason"],
              "判红那句把两边的原话都带上（他看见的 + 你申报的），"
              "模型照它改画面而不是照它改 `subject`")

        # —— 红镜真的合进 targets，走同一条修复的路 ---------------------------------
        jc._llm = lambda: FakeLLM(_BLIND_MISS)
        jc._judge_module = lambda: _fake_judge(verdict=True, confidence=0.9,
                                              reason="他看见的是一列方块，不是蒸汽机")
        r_on2 = await rl.identity_targets(shots, enabled=True,
                                          cache=_MemCache({"k1": _png_bytes(),
                                                           "k2": _png_bytes()}),
                                          keys={"s01": "k1", "s02": "k2"})
        t = r_on2["targets"][0] if r_on2["targets"] else None
        check(t is not None and t.sid == "s01" and t.gates == ("19b",),
              f"判不像的镜进红镜名单（实得 {[x.sid for x in r_on2['targets']]}）")
        check(t is not None and "⑲-b" in t.reasons[0] and "一列方块" in t.reasons[0],
              "红镜的理由是**判官的原话**（不重写措辞——回看环拿它当改代码的输入）")
        check(r_on2["counts"]["unlike"] == 2,
              f"判不像那两镜都算进 unlike（实得 {r_on2['counts']}）")

        # —— 未判**不进**红镜名单：拿它当红镜＝让模型在没有理由的情况下改代码 --------
        jc._judge_module = lambda: _fake_judge(configured=False)
        jc._llm = lambda: FakeLLM(_BLIND_OK)
        r_un = await rl.identity_targets(shots, enabled=True,
                                         cache=_MemCache({"k1": _png_bytes(),
                                                          "k2": _png_bytes()}),
                                         keys={"s01": "k1", "s02": "k2"})
        check(not r_un["targets"] and r_un["counts"]["undetermined"] == 2,
              f"裁决通道没配 → 逐镜**未判**、红镜名单为空（实得 {r_un['counts']}）"
              "——几何闸的未判进名单是因为「没量到版面」值得看一眼，⑲-b 的未判是"
              "「这一趟没问出结论」，拿它当红镜是回看环最贵的一种浪费")

        # —— 没帧 / 没 subject：都是未判，不许折算成「像」 --------------------------
        jc._judge_module = lambda: _fake_judge(verdict=True, confidence=0.9)
        jc._llm = lambda: FakeLLM(_BLIND_OK)
        class _NoCache:
            async def fetch(self, key, dst):
                return None
        r_nof = await rl.identity_targets(shots, enabled=True, cache=_NoCache(),
                                          keys={"s01": "k1", "s02": "k2"})
        check(r_nof["counts"]["undetermined"] == 2 and not r_nof["targets"]
              and all("没取到" in (v.get("why") or "") for v in r_nof["shots"].values()),
              "帧没取到 → 逐镜未判（**不**回落到「只看代码就说像」）")
        r_nosubj = await rl.identity_targets([_shot(id="s01", subject="")], enabled=True,
                                             cache=_MemCache({"k1": _png_bytes()}),
                                             keys={"s01": "k1"})
        check(r_nosubj["counts"]["undetermined"] == 1 and not r_nosubj["targets"],
              "这一镜没申报 `subject` → 未判（⑲-b 无从比；闸⑯ 会拦这一格）")

        # —— 接线：回看环真的把它算进红源，且那一步在「没有红镜」那条早退**之前** ----
        src_rl = Path(rl.__file__).read_text(encoding="utf-8")
        check("ident: dict[str, Any] = await identity_targets(" in src_rl
              and '"identity": ident,' in src_rl,
              "⑲-b 的红镜合进同一份 `targets`、那一栏进回执（不另开一条修复的路，Q115）")
        check(src_rl.index("await identity_targets(") < src_rl.index('"absent_reason"'),
              "它在「本轮没有红镜」那条早退**之前**跑：一镜的几何闸全绿、画的那个东西"
              "却根本不是文案说的那个，正是这一条要抓的形状")
    finally:
        jc._llm = real_llm
        jc._judge_module = real_judge

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


async def _never_raises() -> bool:
    """把四条出口在**最坏的输入**下各走一遍：只要有一条抛出来，这里就是 False。"""
    class Boom:
        async def complete(self, messages, tools=None):
            raise RuntimeError("x")

    jc._llm = lambda: Boom()
    ok = True
    for kw in ({"image": None, "shot_spec": None, "columns": None, "reasons": None},
               {"image": b"", "shot_spec": {}, "columns": {}, "reasons": []}):
        try:
            await jc.judge_frame(gate=rl.GATE, shot="s01", round=1, **kw)
        except Exception:
            ok = False
    jc._judge_module = lambda: _fake_judge(verdict=None)
    try:
        await jc.review_still(shot=None, image=None, columns=None)
    except Exception:
        ok = False
    # ⑲-b 两条也要在最坏输入下走一遍。`readable` 那一条尤其：它**不走**裁决通道，
    # 是这一族里唯一一处「判红不需要第二个模型」的出口，写歪了就是一条自己会抛的判据。
    for kw in ({"shot": None, "image": None},
               {"shot": {"subject": "蒸汽机"}, "image": b""},
               {"shot": {"subject": ""}, "image": _png_bytes()}):
        try:
            await jc.judge_identity(**kw)
        except Exception:
            ok = False
    try:
        await rl.identity_targets([_shot()], enabled=True, cache="不是缓存", keys=None)
    except Exception:
        ok = False
    return ok


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
