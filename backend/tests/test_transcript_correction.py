# -*- coding: utf-8 -*-
"""ASR 修字闸 correct_transcript 的离线验证（借来的第四条：转写只有人能校对，但改动必须受闸）。

运行：  python tests/test_transcript_correction.py   # 全离线：内存替身 + 手工播种的 asr 产物，不起容器/不联网

为什么要有这道闸：模型读完转写就往下走，字幕与选段用的都是 whisper 的原文。同音字、
专有名词错了没人纠——而「让模型顺手改一下」这条路一旦放开时间戳，就会变成第 N 次
「音画不同步」：文本改对了，句子的位置被挪走了，覆盖层的锚点也就锚错了。

钉住六件事：
① 只改字：修正后的 asr_segments 与原文**同长、同序、id 与 start/end 逐字不动**，
   只有被点名的那条 text 变了（外加 corrected 标记）；
② 锚点必须真存在、不重复、文本非空、改动体量在 ±30%（且至少 3 个字/词）以内——
   四种越界各自当场 raise，报错里列出真实存在的 id，且失败不留产物；
③ 修字表（corrections）逐条留 before/after/落在哪一秒/改了几个字，是「谁改的」的记录；
④ 没被改的可疑段（整句只有语气词、同一个字连三遍、识别失败的占位说法）单列一张待办，
   这是形状启发式，不是判错别字；
⑤ 下游认这一份：speech_rough_cut 探到 Store 里有 correct_transcript 就用修过的文本，
   粗剪区间与 ids 一字不改；覆盖层锚点解析在修字前后得到同一个输出窗口（id 没动 ⇒ 盖层不挪位）；
⑥ mock 夹具（agent_framework/video_editing.py）的产物键与真节点契约一致，离线用例验得到同一条路。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, NodeState, NodeRegistry
from agent_framework.storage import build_storage
from agent_framework.video_editing import ALL_NODE_CLASSES
from agent_framework.video_editing import CorrectTranscriptNode as MockCorrect
from storyline_server.nodes.core_nodes import (
    CorrectTranscriptNode, SpeechRoughCutNode, _suspect, _unit_count,
    resolve_overlay_anchors,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# 一份「whisper 会这么错」的转写：一句正常、一句叠字、一句占位、一句纯语气词、一句收尾。
SEED = [
    {"id": "asr-0", "clip": "m0", "text": "今天我们聊聊音画同步的标题", "start": 0.2, "end": 3.1},
    {"id": "asr-1", "clip": "m0", "text": "这个谢谢谢谢的片段是识别打结", "start": 3.4, "end": 6.0},
    {"id": "asr-2", "clip": "m0", "text": "（听不清）", "start": 6.2, "end": 7.0},
    {"id": "asr-3", "clip": "m1", "text": "嗯嗯啊", "start": 7.1, "end": 7.6},
    {"id": "asr-4", "clip": "m1", "text": "最后总结一句：绑句不绑秒", "start": 8.0, "end": 11.2},
]


def seed_payload() -> dict:
    return {"asr_segments": [dict(s) for s in SEED], "warnings": []}


def real_registry(storage) -> NodeRegistry:
    """真节点：correct_transcript 与 speech_rough_cut 都只读 Store，不碰 ffmpeg，
    故 settings/providers 传 None 也能跑——这道闸验的是数据契约，不是媒体处理。"""
    reg = NodeRegistry()
    CorrectTranscriptNode(None, None, storage, registry=reg)
    SpeechRoughCutNode(None, None, storage, registry=reg)
    return reg


async def open_state(storage, sid: str, aid: str) -> NodeState:
    store = await ArtifactStore.open(storage.artifacts(sid, aid), sid, aid)
    await store.put("asr", seed_payload())
    await store.persist("asr")
    return NodeState(session_id=sid, artifact_id=aid, user_request="把转写里的错字改掉",
                     store=store)


# ---- ①②③④ 节点级 ----

async def case_node_contract(storage) -> None:
    reg = real_registry(storage)
    node = reg.get("correct_transcript")

    state = await open_state(storage, "t:ok", "a1")
    packed = await node(state, corrections=[
        {"id": "asr-0", "text": "今天我们聊聊音画同步的问题"},
    ])
    out = packed["output"]
    segs = out["asr_segments"]
    check([s["id"] for s in segs] == [s["id"] for s in SEED],
          f"段 id 与顺序一字未动：{[s['id'] for s in segs]}")
    check([(s["start"], s["end"], s["clip"]) for s in segs]
          == [(s["start"], s["end"], s["clip"]) for s in SEED],
          "start/end/clip 逐字保留——改字不许挪位置，锚点与区间才有得靠")
    check(segs[0]["text"] == "今天我们聊聊音画同步的问题" and segs[0].get("corrected") is True,
          f"被点名的那条换了文本：{segs[0]}")
    check(all("corrected" not in s for s in segs[1:]), "没被改的那几条原样不动")
    led = out["corrections"]
    check(len(led) == 1 and led[0]["before"] == SEED[0]["text"]
          and led[0]["after"] == "今天我们聊聊音画同步的问题"
          and led[0]["at"] == [0.2, 3.1] and led[0]["changed_units"] == 0,
          f"修字表逐条记下改前改后与落点：{led}")
    check(out["corrected"] == 1 and out["note"],
          f"计数与说明随产物一起落库：{out['corrected']}／{out['note']}")
    stored = await state.store.get("correct_transcript")
    check(stored == out, "产物确实进了 Store（下游探的就是这一份）")

    # 可疑段清单：没被改的、形状像出问题的
    state2 = await open_state(storage, "t:sus", "a1")
    out2 = (await node(state2, corrections=[{"id": "asr-4", "text": "最后总结一句：绑句不绑时间"}]))["output"]
    suspects = {s["id"]: s["why"] for s in out2["unchanged_suspects"]}
    check(set(suspects) == {"asr-1", "asr-2", "asr-3"},
          f"可疑段正好是那三条没被改的（asr-1 叠字 / asr-2 占位 / asr-3 语气词）：{sorted(suspects)}")
    check("三遍" in suspects["asr-1"] and "打结" in suspects["asr-1"],
          f"叠字段给了形状理由：{suspects['asr-1']}")
    check("识别失败" in suspects["asr-2"], f"占位段给了占位理由：{suspects['asr-2']}")
    check("语气词" in suspects["asr-3"], f"纯语气词段给了语气词理由：{suspects['asr-3']}")
    check(all({"id", "clip", "at", "text", "why"} <= set(s) for s in out2["unchanged_suspects"]),
          "可疑段带 id/落点/原文，模型照着就能定位到要修的那句")

    # ---- ② 四种越界 ----
    bad_cases = [
        ("unknown_id", "锚点不存在", [{"id": "asr-77", "text": "随便改改"}], "修字锚点认不出来"),
        ("dup_id", "同一 id 改两遍",
         [{"id": "asr-0", "text": "今天我们聊聊音画同步的问题一"},
          {"id": "asr-0", "text": "今天我们聊聊音画同步的问题二"}], "被改了两遍"),
        ("empty_text", "空文本", [{"id": "asr-0", "text": "   "}], "text 是空的"),
        ("too_big", "整段换内容",
         [{"id": "asr-0", "text": "这句话被整段换成了完全不同的另一段话，用来试探闸的边界"}],
         "改动过大"),
        ("no_param", "没传 corrections", None, "需要你传入 corrections"),
    ]
    for code, label, corrections, needle in bad_cases:
        st = await open_state(storage, f"t:bad_{code}", "a1")
        try:
            if corrections is None:
                await node(st)
            else:
                await node(st, corrections=corrections)
            check(False, f"{label}：必须打回")
        except ValueError as e:
            check(needle in str(e), f"{label} 当场报错（{str(e).splitlines()[0][:60]}…）")
        check(not await st.store.has("correct_transcript"),
              f"{label} 失败后 Store 里没有半成品产物（下游不会读到半个修字结果）")
        check(await st.store.get("asr") == seed_payload(),
              f"{label} 不改动原始 asr 产物")
    st = await open_state(storage, "t:badid", "a1")
    try:
        await node(st, corrections=[{"id": "asr-77", "text": "改"}])
    except ValueError as e:
        check("asr-0" in str(e) and "asr-4" in str(e),
              "报错直接列出真实存在的 id，模型不用猜")


# ---- ⑤ 下游：粗剪与覆盖层锚点 ----

async def case_downstream(storage) -> None:
    reg = real_registry(storage)
    node, rough = reg.get("correct_transcript"), reg.get("speech_rough_cut")

    state = await open_state(storage, "t:down", "a1")
    # 没修字时：粗剪用原文
    before = (await rough(state))["output"]
    check(any(r["text"].startswith("今天我们聊聊音画同步的标题") for r in before["rough_clips"]),
          "没调用修字闸时粗剪照用转写原文（闸是可选的，不是必经）")

    await node(state, corrections=[{"id": "asr-0", "text": "今天我们聊聊音画同步的问题"}])
    after = (await rough(state))["output"]
    texts = [r["text"] for r in after["rough_clips"]]
    check(any("问题" in t for t in texts) and not any("标题" in t for t in texts),
          f"修过字之后粗剪采用修过的文本：{texts}")
    check([r.get("ids") for r in after["rough_clips"]] == [r.get("ids") for r in before["rough_clips"]],
          "粗剪的区间与 ids 一字未动（改字不该改剪切点）")

    # 覆盖层锚点：修字前后各算一次（各用一份全新时间线，解析结果会写回入参），
    # 两条输出窗口必须完全相同——id 没动，盖层就不挪位
    def fresh_tl() -> dict:
        return {"audio_events": [{"path": "obj:src.mp4", "start": 0.0, "end": 4.0,
                                  "src_start": 8.0, "src_end": 12.0, "kind": "original"}],
                "events": [], "overlay_events": [{"segments": ["asr-4"], "path": "obj:ov.mp4"}]}

    media_paths = {"m0": "obj:src.mp4", "m1": "obj:src.mp4"}
    tl_a, tl_b = fresh_tl(), fresh_tl()
    plain = resolve_overlay_anchors(tl_a, seed_payload()["asr_segments"], media_paths=media_paths)
    fixed = resolve_overlay_anchors(tl_b, (await state.store.get("correct_transcript"))["asr_segments"],
                                    media_paths=media_paths)
    check(plain == 1 and abs(tl_a["overlay_events"][0]["start"] - 0.0) < 0.01
          and abs(tl_a["overlay_events"][0]["end"] - 3.2) < 0.01,
          f"锚点按 id 现算输出窗口：{tl_a['overlay_events'][0]['start']}"
          f"~{tl_a['overlay_events'][0]['end']}s")
    check(tl_a["overlay_events"] == tl_b["overlay_events"] and fixed == plain,
          "修字后同一 id 得到同一窗口（时间戳不动 ⇒ 盖层不挪位）")


# ---- ⑥ 夹具与真节点的产物契约一致 ----

async def case_mock_parity(storage) -> None:
    reg = NodeRegistry()
    MockCorrect(registry=reg)
    state = NodeState(session_id="t:mock", artifact_id="a1", store=await ArtifactStore.open(
        storage.artifacts("t:mock", "a1"), "t:mock", "a1"))
    await state.store.put("asr", {"asr_segments": [{"id": "asr-0", "clip": "m0",
                                                    "text": "asr:m0", "start": 0.0, "end": 1.0}]})
    out = (await reg.get("correct_transcript")(
        state, corrections=[{"id": "asr-0", "text": "asr:m0 修过"}]))["output"]
    real_out = (await real_registry(storage).get("correct_transcript")(
        await open_state(storage, "t:parity", "a1"),
        corrections=[{"id": "asr-0", "text": "今天我们聊聊音画同步的问题"}]))["output"]
    check(set(out) == set(real_out),
          f"mock 夹具的产物键与真节点一致：{sorted(out)}")
    check(out["asr_segments"][0]["text"] == "asr:m0 修过"
          and out["asr_segments"][0]["start"] == 0.0 and out["asr_segments"][0]["id"] == "asr-0",
          "mock 同样只改文本、不动 id 与时间")
    check(any(n.name == "correct_transcript" for n in ALL_NODE_CLASSES),
          "mock 节点进了 ALL_NODE_CLASSES（离线注册表与真注册表同名同 DAG）")


# ---- 纯函数：字数口径与可疑形状 ----

def case_helpers() -> None:
    check(_unit_count("今天我们聊聊音画同步") == 10, "中文按字计")
    check(_unit_count("hello world 你好") == 4, "中英混排：拉丁按词、汉字按字（2+2）")
    check(_unit_count("谢谢 谢谢！") == 4, "标点与空白不计入体量")
    check(_suspect("嗯嗯啊") == "整段只有语气词", "纯语气词段 → 可疑（整段只有语气词）")
    check("三遍" in _suspect("谢谢谢谢谢谢"), "同一个字连三遍以上 → 可疑")
    check("识别失败" in _suspect("（听不清）"), "识别失败的占位说法 → 可疑")
    check(not _suspect("最后总结一句：绑句不绑秒"), "正常收尾句无可疑理由")
    check(not _suspect("这是一句正常的完整口播内容"), "正常句子不该被标成可疑")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        print("\n=== 纯函数：字数口径与可疑形状 ===")
        case_helpers()
        print("\n=== ①②③④ 节点契约：只改字，越界打回 ===")
        asyncio.run(case_node_contract(storage))
        print("\n=== ⑤ 下游：粗剪采用修过的文本，锚点不挪位 ===")
        asyncio.run(case_downstream(storage))
        print("\n=== ⑥ mock 夹具与真节点同契约 ===")
        asyncio.run(case_mock_parity(storage))
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
