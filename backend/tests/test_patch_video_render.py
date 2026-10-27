# -*- coding: utf-8 -*-
"""口播/素材链的「选区改」端到端：真烧一遍，再只重烧受影响的那几个窗口。

纯函数那一半在 ``test_timeline_patch.py``（指针、白名单、缓存键、应当重烧哪几窗）；
这一份管的是**接上真渲染之后还成立吗**：命中表真的从成片量出来了、补丁真的只吃了
那一窗的字节、旧版真的还在原位。这三件事各有各的「离线全绿、真机出错」的方式：

* 命中表靠 ``tl_canonical``（本地化**之前**那份，带对象键），拿错那份就是一条指针都对不上；
* 「只重烧受影响窗口」靠缓存键，而缓存键最容易少算输入——少算一个就是拿旧片冒充新片；
* fork 换作用域靠 render_jobs 与对象键的落位，沿用旧号就是覆盖旧版（退路没了）。

素材是 lavfi 现造的假片子（不联网、不要密钥），6 秒 3 窗，正好够把上面三条各钉一次。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import types as _types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import Interceptor, NodeState  # noqa: E402
from agent_framework.storage import build_storage, to_ref          # noqa: E402
from open_storyline import mediaops                                  # noqa: E402
from open_storyline import timeline_edit as te                   # noqa: E402

# 出片链的现场搭建（假素材、内存替身存储、假 providers）与管线专测共用一套，
# 这里只加「渲染完之后还能不能选着改」这一段。
from test_render_pipeline import (  # noqa: E402
    CONV, USER, _open_store, fake_providers, make_source, seed, write_config,
)

FAILS = 0
CHECKS = 0


def check(cond: bool, label: str) -> None:
    global FAILS, CHECKS
    CHECKS += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILS += 1


def timeline_of(path_ref: str) -> dict:
    """一条 6 秒的原声时间线：3 个画面窗、2 条字幕（第 2 条**跨**第 2/3 窗边界）。

    跨边界的那一条是故意的——「改一句字幕要重烧它压着的两窗」这条判据只有跨界才测得出来，
    不跨界时「只烧一窗」和「正确的实现」长得一模一样。
    """
    return {
        "width": 640, "height": 360, "fps": 25.0, "duration": 6.0,
        "mode": "original_audio",
        "events": [
            {"path": path_ref, "start": 0.0, "end": 2.0, "src_start": 0.0,
             "src_end": 2.0, "kind": "original"},
            {"path": path_ref, "start": 2.0, "end": 4.0, "src_start": 2.0,
             "src_end": 4.0, "kind": "original"},
            {"path": path_ref, "start": 4.0, "end": 6.0, "src_start": 4.0,
             "src_end": 6.0, "kind": "original"},
        ],
        "audio_events": [
            {"path": path_ref, "start": 0.0, "end": 2.0, "src_start": 0.0,
             "src_end": 2.0, "kind": "original"},
            {"path": path_ref, "start": 2.0, "end": 4.0, "src_start": 2.0,
             "src_end": 4.0, "kind": "original"},
            {"path": path_ref, "start": 4.0, "end": 6.0, "src_start": 4.0,
             "src_end": 6.0, "kind": "original"},
        ],
        "subtitles": [
            {"text": "第一句只占第一窗", "start": 0.2, "end": 1.8,
             "style": "subtitle_clean"},
            {"text": "第二句横跨第二窗和第三窗", "start": 2.5, "end": 5.0,
             "style": "subtitle_clean"},
        ],
        "transition_styles": [],
    }


async def read_json(storage, key: str) -> dict:
    raw = b"".join([c async for c in storage.objects.get_stream(key)])
    return json.loads(raw.decode("utf-8"))


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="patch_video_"))
    settings = write_config(tmp)
    raw = tmp / "raw"
    raw.mkdir()
    # 6 秒、三段各 2 秒、自带一条正弦音轨——正好当「原声口播」用
    make_source(raw / "src.mp4", [
        "testsrc2=size=640x360:rate=25:duration=2",
        "smptebars=size=640x360:rate=25:duration=2",
        "color=c=blue:size=640x360:rate=25:duration=2",
    ], 440)

    storage = build_storage("memory",
                            cache_root=settings.storage.cache_root,
                            workspace_root=settings.storage.workspace_root)
    mat = await seed(storage, raw / "src.mp4")
    obj_key = f"users/{USER}/convs/{CONV}/{mat}.mp4"
    reg = fake_registry(settings, storage)
    itp = Interceptor(reg)

    sess = "pv:main"
    state = NodeState(session_id=sess, artifact_id="v1", user_request="口播选区改验证",
                      store=await _open_store(storage, sess, "v1"),
                      user_id=USER, conversation_id=CONV)
    # 手写时间线（原声口播的常见形态）：不跑创意链那几步，直接把这一版交给出片节点
    tl = timeline_of(to_ref(obj_key))
    await state.store.put("plan_timeline", {"timeline": tl})

    packed = await itp.invoke("render_video", state, render_mode="segments")
    out = packed["output"]

    # ---------- ① 出片这一版：命中表、代表帧、逐窗账都真存在 ----------
    check(out["video"] == f"renders/pv_main/v1.mp4", f"成片落在产物作用域（{out['video']}）")
    hit_key = out.get("hitmap") or ""
    check(bool(hit_key) and await storage.objects.head(hit_key) is not None,
          "出片带回命中表对象键，且表里有字节")
    table = await read_json(storage, hit_key)
    check(table["schema"] == "segment-hitmap/1" and table["hit_mode"] == "track",
          "表的版本与命中形态：track（这条链没有空间命中层）")
    doc = table["doc"]
    check(te.fingerprint(doc) == table["doc_fingerprint"],
          "表里嵌的时间线与指纹成对（补丁那一头据此拒旧表）")
    check(all(e["path"] == obj_key or str(e["path"]).endswith(".mp4")
              and "/" in str(e["path"]) for e in doc["events"]),
          "表里的 path 是**对象键**而不是本机路径（缓存键与指针都得跨机器认得出）")
    wins = te.windows(doc)
    check(len(wins) == 3, f"六秒三窗（实得 {len(wins)}）")
    check(out["segment_cache"]["windows"] == 3
          and len(out["segment_cache"]["rebuilt"]) == 3
          and out["segment_cache"]["reused"] == [],
          "首渲没有可复用的字节：三窗全烧")
    seg_frames = out.get("segment_frames") or {}
    head_ok = [await storage.objects.head(k) is not None
               for k in seg_frames.values()]
    check(len(seg_frames) == 3 and all(head_ok),
          f"逐窗代表帧已入库并真能 HEAD 到（{len(seg_frames)} 张，{head_ok}）")
    check(set(seg_frames) == {sid for sid, _s, _e, _i, _k in wins},
          "代表帧按窗口 id 索引（前端的行与缓存键说的是同一批窗口）")

    # ---------- ② 改第一窗里的一句字幕：只重烧那一窗 ----------
    sub0 = doc["subtitles"][0]
    win0, win1, win2 = [sid for sid, _s, _e, _i, _k in wins]
    p2 = await reg.get("patch_video")(
        NodeState(session_id=sess, artifact_id="", user_request="改第一句字幕",
                  store=await _open_store(storage, sess, ""),
                  user_id=USER, conversation_id=CONV),
        base_artifact_id="v1",
        edits=[{"pointer": "/subtitles/0/text", "value": "第一句改过了",
                "segment_id": str(sub0["id"])}],
        wait_sec=0)
    o2 = p2["output"]
    patch2 = o2["patch"]
    check(o2["video"] != out["video"], f"局部改产出**新**对象键（{o2['video']}）")
    check(patch2["base_artifact_id"] == "v1" and p2["artifact_id"] not in ("", "v1"),
          f"新版本有自己的产物号（{p2['artifact_id']}）")
    check(await storage.objects.head(out["video"]) is not None,
          "旧版成片仍在原位（改坏了直接播旧版，不必重渲）")
    check(await storage.objects.head(o2["video"]) is not None, "新版成片有字节")
    check(patch2["changed_ids"] == [str(sub0["id"])],
          f"改动账就是被点名的那一条字幕（实得 {patch2['changed_ids']}）")
    check(patch2["expected_rebuild"] == [win0],
          f"按输入比对应当只重烧第一窗（实得 {patch2['expected_rebuild']}）")
    rebuilt2 = o2["segment_cache"]["rebuilt"]
    reused2 = o2["segment_cache"]["reused"]
    check(set(rebuilt2) == {win0} and set(reused2) == {win1, win2},
          f"这次真烧的只有第一窗（rebuilt={rebuilt2} reused={reused2}）")
    check(abs(float(o2["duration"]) - float(out["duration"])) < 0.15,
          f"总长没被一次选区改掉（{out['duration']} → {o2['duration']}）")
    ev2 = o2["evidence"][:3]
    check(len(ev2) == 3 and all(e["status"] == "verified" for e in ev2),
          "局部改的三条账都验过（读的是哪一版、只重烧受影响窗、旧版没被覆盖）")
    check(any("只重烧受影响窗口" in e["claim"] for e in ev2),
          "账里写着「点名几段 / 应当重烧几窗 / 真烧几窗」的原话")
    check(set(patch2["before_frames"]) == {win0},
          f"改前对比帧按**要重烧的那一窗**给（前端摆「改前/改后」用的就是它）："
          f"{sorted(patch2['before_frames'])}")

    # 新版自己的表：指针与段号必须换成这一版的（改了字的那条字幕 id 也变了）
    table2 = await read_json(storage, o2["hitmap"])
    check(table2["doc_fingerprint"] != table["doc_fingerprint"],
          "新版本的指纹不同（旧表从此认不到新片）")
    check(table2["doc"]["subtitles"][0]["text"] == "第一句改过了",
          "新表里嵌的就是改过的那一版")
    check(str(table2["doc"]["subtitles"][0]["id"]) != str(sub0["id"]),
          "改了字的字幕拿到新段 id（指针不冒充还指着老内容）")

    # 会话当前时间线也同步成这一版：模型下一次整片重渲不能把用户的改动丢掉
    cur = await storage.artifacts(sess, "").get("plan_timeline")
    check((cur or {}).get("timeline", {}).get("subtitles", [{}])[0].get("text")
          == "第一句改过了",
          "改动写回会话当前 plan_timeline（换配乐整片重渲时不会退回旧字幕）")

    # 像素级对账：账上说「第一窗重烧了」，那就得在**新成片的画面里**看得见改动。
    # 只看台账会漏掉一类错法——把改动过的那一窗从上一版成片里切出来拼进新片：
    # 台账照样报 rebuilt，用户看到的却还是改之前的字幕。1.0s 正落在改过的那条字幕里。
    old_bytes = b"".join([c async for c in storage.objects.get_stream(out["video"])])
    new_bytes = b"".join([c async for c in storage.objects.get_stream(o2["video"])])
    (tmp / "v1.mp4").write_bytes(old_bytes)
    (tmp / "p1.mp4").write_bytes(new_bytes)
    f_old = mediaops.extract_frame(tmp / "v1.mp4", 1.0, tmp / "old@1.0.png")
    f_new = mediaops.extract_frame(tmp / "p1.mp4", 1.0, tmp / "new@1.0.png")
    check(f_new.read_bytes() != f_old.read_bytes(),
          "改过的那一窗在**新成片**里像素确实变了（1.0s 一帧与旧版不同，不是拿旧片切出来冒充）")

    # ---------- ③ 第二次改：跨窗的字幕要重烧两窗，第一窗这次从缓存拿 ----------
    sub1 = table2["doc"]["subtitles"][1]
    p3 = await reg.get("patch_video")(
        NodeState(session_id=sess, artifact_id="", user_request="改横跨的那句",
                  store=await _open_store(storage, sess, ""),
                  user_id=USER, conversation_id=CONV),
        base_artifact_id=p2["artifact_id"],
        edits=[{"pointer": "/subtitles/1/text", "value": "第二句改过了",
                "segment_id": str(sub1["id"])}],
        wait_sec=0)
    o3 = p3["output"]
    win_ids = {sid for sid, _s, _e, _i, _k in te.windows(table2["doc"])}
    exp3 = set(o3["patch"]["expected_rebuild"])
    check(exp3 <= win_ids and len(exp3) == 2,
          f"跨窗字幕改动 → 应当重烧它压着的两窗（实得 {sorted(exp3)}）")
    check(set(o3["segment_cache"]["rebuilt"]) == exp3,
          "真烧的窗与应当烧的窗一字不差")
    check(set(o3["patch"]["before_frames"]) == exp3,
          f"跨窗改动的改前对比帧也是按**要重烧的那两窗**给"
          f"（{sorted(o3['patch']['before_frames'])}）")
    check(len(o3["segment_cache"]["reused"]) == 1,
          f"第三窗直接复用上一版字节（reused={o3['segment_cache']['reused']}）")
    check(await storage.objects.head(o2["video"]) is not None,
          "上一版（第一次局部改的那版）也没被覆盖：两版都能播回去")

    # ---------- ④ 拒收：错指针 / 旧表 / 空补丁，都在烧像素之前 ----------
    for edits, why in [
        ([{"pointer": "/events/0/start", "value": 1.0,
           "segment_id": str(table["doc"]["events"][0]["id"])}],
         "改画面段的输出区间"),
        ([{"pointer": "/subtitles/0/text", "value": "x",
           "segment_id": str(table["doc"]["subtitles"][1]["id"])}],
         "指针与段号对不上（拿旧表改新片）"),
        ([], "空补丁"),
    ]:
        try:
            await reg.get("patch_video")(
                NodeState(session_id=sess, artifact_id="", user_request="应当被拒",
                          store=await _open_store(storage, sess, ""),
                          user_id=USER, conversation_id=CONV),
                base_artifact_id="v1", edits=edits, wait_sec=0)
            check(False, f"拒收{why}")
        except ValueError as exc:
            check(True, f"拒收{why}：{str(exc)[:52]}…")

    stale = NodeState(session_id=sess, artifact_id="", user_request="没有表的版本",
                      store=await _open_store(storage, sess, ""),
                      user_id=USER, conversation_id=CONV)
    try:
        await reg.get("patch_video")(stale, base_artifact_id="nope123",
                                    edits=[{"pointer": "/subtitles/0/text",
                                            "value": "x"}], wait_sec=0)
        check(False, "base 那一版没有命中表时必须拒")
    except ValueError as exc:
        check("没有命中表" in str(exc), f"没有表就拒猜指针（{str(exc)[:48]}…）")

    # 指纹闸门：把表里的 doc 与指纹改成对不上，落笔前就该被拦
    bad_key = "renders/pv_main/v_bad/hitmap.json"
    tampered = dict(table)
    tampered["doc"] = {**table["doc"], "subtitles": [
        {**table["doc"]["subtitles"][0], "text": "表里的内容被换过了"},
        *table["doc"]["subtitles"][1:]]}

    async def bad_row():
        yield json.dumps(tampered, ensure_ascii=False).encode("utf-8")

    await storage.objects.put(bad_key, bad_row(), content_type="application/json")
    try:
        await reg.get("patch_video")(
            NodeState(session_id=sess, artifact_id="", user_request="表与片子不是同一版",
                      store=await _open_store(storage, sess, ""),
                      user_id=USER, conversation_id=CONV),
            base_artifact_id="v_bad",
            edits=[{"pointer": "/subtitles/0/text", "value": "x"}], wait_sec=0)
        check(False, "指纹与表里那份 doc 对不上时必须拒")
    except ValueError as exc:
        check("另一版时间线" in str(exc),
              f"旧表/错表被指纹闸拦下（{str(exc)[:52]}…）")

    # ---------- ⑤ 换版号：传回来的版号恰好等于 base 时也必须换 ----------
    # 前面几段都用 artifact_id="" 走「换新号」那条路。这条最容易漏的输入是
    # 「接着改这一版」：前端把基线换成 A 之后，state.artifact_id 就是 A 自己的号，
    # 沿用即覆盖 A——而 A 那一版字节是这次改动唯一的退路。两条链在这里必须同规则。
    from open_storyline.nodes.core_nodes import PatchVideoNode

    v1_row_before = await storage.render_jobs.get(sess, "v1")
    st5 = NodeState(session_id=sess, artifact_id="v1", user_request="接着改 v1",
                    store=await _open_store(storage, sess, "v1"),
                    user_id=USER, conversation_id=CONV)
    new_id = await PatchVideoNode._fork(
        _types.SimpleNamespace(storage=storage), st5,
        sess=sess, base="v1", doc=table["doc"])
    check(new_id not in ("", "_default", "v1") and st5.artifact_id == new_id,
          f"base 与当前版号相同时另起一号（实得 {new_id!r}）")
    check(await storage.objects.head("renders/pv_main/v1.mp4") is not None,
          "v1 的成片字节还在原位（改坏了播回 v1，不必重渲）")
    v1_row_after = await storage.render_jobs.get(sess, "v1")
    check((v1_row_after or {}).get("video_object_key")
          == (v1_row_before or {}).get("video_object_key"),
          "v1 的渲染任务行没被新版的落位覆盖（换号不只是换个文件名）")

    # ---------- ⑥ 窗口名漂移：改画面段取源区间会让该窗改名，仍须重烧、且取得到改前帧 ----------
    # 真机踩过的坑（清单 A）：改第一窗的声音/取源区间后，这一段**内容**变了、段 id 被
    # assign_segment_ids 按内容重算，窗口名从 base 的 ev-X 漂成 patched 的 ev-Y。若按改前
    # 那张表的名字点名（expected），漂移的窗不在名单里，被当成「没改」从旧片 base_cut 切进来
    # ——用户改动静默丢失，旧字节还被钉进切片缓存、此后每次命中都错。
    # 这里改 events/0/src_start（MANUAL_FIELDS 之一：apply_patch 给它打上 manual，渲染前校准
    # 因此跳过它、用户改的取源区间保住），断言五件事：① 窗口真改名了（场景确实复现漂移）；
    # ② (b) 判据不因改名假红（rebuilt ⊆ planned，planned 取渲染器自己的 planned_rebuild）；
    # ③ 漂移窗是真渲染出来的、不是从旧片切；④ (d) 改前帧按新名当键仍取到 base 旧名字节、
    # 对象键真读得到；⑤ 像素对账——新成片那一帧与旧版不同。
    base_wins = [sid for sid, _s, _e, _i, _k in te.windows(table["doc"])]
    ev0 = table["doc"]["events"][0]
    p6 = await reg.get("patch_video")(
        NodeState(session_id=sess, artifact_id="", user_request="改第一窗取源区间",
                  store=await _open_store(storage, sess, ""),
                  user_id=USER, conversation_id=CONV),
        base_artifact_id="v1",
        edits=[{"pointer": "/events/0/src_start", "value": 1.0,
                "segment_id": str(ev0["id"])}],
        wait_sec=0)
    o6 = p6["output"]; pc6 = o6["patch"]; sc6 = o6.get("segment_cache") or {}
    new_doc6 = (await read_json(storage, o6["hitmap"]))["doc"]
    new_wins6 = [sid for sid, _s, _e, _i, _k in te.windows(new_doc6)]
    check(new_wins6[0] != base_wins[0],
          f"改取源区间让第一窗真的改了名（{base_wins[0]} → {new_wins6[0]}）——正是要兜的漂移")
    rebuilt6 = set(sc6.get("rebuilt") or [])
    planned6 = set(sc6.get("planned_rebuild") or [])
    check(rebuilt6 <= planned6,
          f"(b) 改名后判据不假红：rebuilt ⊆ planned（rebuilt={sorted(rebuilt6)} "
          f"planned={sorted(planned6)}）")
    check(new_wins6[0] in rebuilt6 and new_wins6[0] in planned6,
          f"(b) 漂移后的那一窗既在名单里又被重烧（rebuilt={sorted(rebuilt6)}）")
    led6 = {str(r.get("id")): r.get("from") for r in sc6.get("ledger") or []}
    check(led6.get(new_wins6[0]) == "rendered",
          f"(b) 漂移窗是渲染出来的、不是从旧片切进来冒充（from={led6.get(new_wins6[0])}）")
    bf6 = dict(pc6.get("before_frames") or {})
    check(set(bf6) == {new_wins6[0]},
          f"(d) 改前帧按**漂移后的新名**当键给（键={sorted(bf6)}）")
    got6 = {re.search(r"frames/([^/]+)\.png$", v).group(1)
            for v in bf6.values() if v}
    check(got6 == {base_wins[0]},
          f"(d) 新名映射回 base 旧名取字节（取到 {sorted(got6)}，应为 {base_wins[0]}）")
    for _k6, _v6 in bf6.items():
        check(await storage.objects.head(_v6) is not None,
              f"(d) 改前帧对象键真读得到（{_v6}）")
    o1_bytes = b"".join([c async for c in storage.objects.get_stream(out["video"])])
    o6_bytes = b"".join([c async for c in storage.objects.get_stream(o6["video"])])
    (tmp / "v1.mp4").write_bytes(o1_bytes)
    (tmp / "p6.mp4").write_bytes(o6_bytes)
    f_o1 = mediaops.extract_frame(tmp / "v1.mp4", 1.0, tmp / "o1@1.0.png")
    f_o6 = mediaops.extract_frame(tmp / "p6.mp4", 1.0, tmp / "o6@1.0.png")
    check(f_o6.read_bytes() != f_o1.read_bytes(),
          "漂移的第一窗在**新成片**里像素确实变了（1.0s 一帧与旧版不同，不是拿旧片切出来冒充）")

    # ---------- ⑦ 名字漂移的**真**来源：拧声音段的取景，校准把画面段搬走 ----------
    # ⑥ 那条兜不住这次的 bug：它改的是画面段自己，而那一段带「按用户手改」标、渲染前
    # 的校准不碰它，于是 patched（expected 据以算名的那一份）与渲染器用的是**同一套名**
    # ——修复前的代码也能过 ⑥。真机踩过的是另一条路：用户拧的是**声音**段的取源区间
    # （也标手改、同步闸让路），而校准为了让嘴型对上，把压在它底下的**画面段**搬到新
    # 声音位置（见 resync_original_audio_timeline）→ 那些画面段换了内容 → 换了名字。
    # 名字换了有两种落法：换成一个全新名，或者与另一窗**撞成同一段素材的同一取源区间**
    # （id 由 path+源区间算，撞名了 assign_segment_ids 就去重成 `ev-xxx#2`）。真机那一次
    # 是前者、这条用例跑出的是后者——两者都是 expected（校准前那张表点的名）里没有的名字。
    # 只要渲染器用的名字不在 expected 里，点名就落空、那一窗被当成「没改」从旧片
    # base_cut 切进来：用户的改动静默丢失，那份旧字节还被钉进切片缓存、此后每次命中都错。
    # 所以这条用例锁的是**planned − expected 非空、且那几窗必须真重烧**——把 core_nodes
    # 里 base_doc 按秒区间补点名那一段注掉，⑦ 必须变红（⑥ 仍绿）。
    au0 = table["doc"]["audio_events"][0]
    # 只发**一格**指针——编辑器拖一个把手就只改那一个数，成对是 apply_patch 的活：
    # 另一端按原输出长度平移回去（取景 0→2 变 2→4，输出仍 2 秒）。以前这里留过
    # 2→2 的零长度源段，MoviePy 合成那条声音时直接炸；根因已在 timeline_edit 修掉，
    # 所以用例必须发 UI 真实发的那种形状，否则它替旧缺陷兜着。
    p7 = await reg.get("patch_video")(
        NodeState(session_id=sess, artifact_id="", user_request="改第一段原声取景",
                  store=await _open_store(storage, sess, ""),
                  user_id=USER, conversation_id=CONV),
        base_artifact_id="v1",
        edits=[{"pointer": "/audio_events/0/src_start", "value": 2.0,
                "segment_id": str(au0["id"])}],
        wait_sec=0)
    o7 = p7["output"]; pc7 = o7["patch"]; sc7 = o7.get("segment_cache") or {}
    doc7 = (await read_json(storage, o7["hitmap"]))["doc"]
    wins7 = [sid for sid, _s, _e, _i, _k in te.windows(doc7)]
    exp7 = set(pc7.get("expected_rebuild") or [])
    rebuilt7 = set(sc7.get("rebuilt") or [])
    planned7 = set(sc7.get("planned_rebuild") or [])
    led7 = {str(r.get("id")): r.get("from") for r in sc7.get("ledger") or []}
    check(set(wins7) != set(base_wins),
          f"⑦ 校准真的搬走了画面段（{base_wins} → {wins7}）——漂移复现")
    lost7 = planned7 - exp7
    check(bool(lost7),
          f"⑦ 漏点名复现：渲染器认得、expected 却点不到的窗（planned={sorted(planned7)} "
          f"expected={sorted(exp7)}）——正是补点名要吃下的那几窗")
    check(lost7 <= rebuilt7,
          f"⑦ base_doc 按秒区间把这几窗追回点名并重烧（漏网={sorted(lost7)} "
          f"rebuilt={sorted(rebuilt7)}）")
    check(all(led7.get(sid) == "rendered" for sid in lost7),
          f"⑦ 漏网窗是重烧出来的、不是从旧片切进来冒充"
          f"（{ {sid: led7.get(sid) for sid in sorted(lost7)} }）")
    check(rebuilt7 <= planned7,
          f"⑦ 判据不假红：真烧的窗都在应当重烧的名单里（rebuilt={sorted(rebuilt7)}）")
    bf7 = dict(pc7.get("before_frames") or {})
    bs7 = dict(pc7.get("before_frame_shots") or {})
    check(lost7 <= set(bf7) and all(bs7.get(sid) in base_wins for sid in lost7),
          f"⑦ 改前帧按新名给键、字节取自 base 旧名（{bs7}）")
    check(await storage.objects.head(bf7[wins7[0]]) is not None,
          f"⑦ 那一张改前帧真读得到（{bf7.get(wins7[0])}）")
    o7_bytes = b"".join([c async for c in storage.objects.get_stream(o7["video"])])
    (tmp / "p7.mp4").write_bytes(o7_bytes)
    f_o7 = mediaops.extract_frame(tmp / "p7.mp4", 1.0, tmp / "o7@1.0.png")
    check(f_o7.read_bytes() != mediaops.extract_frame(
        tmp / "v1.mp4", 1.0, tmp / "o1r@1.0.png").read_bytes(),
        "⑦ 第一窗在新成片里像素确实变了（不是拿旧片那一秒冒充）")

    print(f"\n{CHECKS} 项断言，{FAILS} 项失败")
    sys.exit(1 if FAILS else 0)


def fake_registry(settings, storage):
    from open_storyline.nodes.core_nodes import build_real_registry
    return build_real_registry(settings, fake_providers(), storage)


if __name__ == "__main__":
    asyncio.run(main())
