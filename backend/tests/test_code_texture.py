# -*- coding: utf-8 -*-
"""逐镜质感通道（§5.3 / B6）的离线用例：档名解析、缓存键口径、补丁口指针，以及那条
「换质感 0 帧 Remotion 重烧、命中缓存的镜照样被加上质感」的保证。

这份文件判的是**四件事各自有没有唯一出处**：

① 声明期：非法档名带镜号打回，合法档名归一成小写；空串与 ``"none"`` 是**两种形状**
   （前者「这一镜不表态」，后者「这一镜点名不要」），合并成一个就等于丢掉一种用户选择。
② 解析：真正跑哪一档只许从 :func:`code_spec.shot_texture` 出。渲染层与记账层各写一遍
   「镜头有就用、没有问整片」的话，就会出现账面 film、画面原片——这条通道的存在理由
   正是要消灭那个。
③ 缓存键：``texture`` **故意不在** ``SHOT_KNOBS``/``FILM_KNOBS`` 里，所以换档 0 镜重烧。
   这条按字节断言，而且带一条对照（改文案必须换键）——没有对照，「键没变」可能只是
   夹具压根没进键。
④ 执行层：滤镜跑在**取到切片之后、拼接之前**，按镜各一趟；缓存里存的是加质感**之前**
   的像素。所以「命中缓存」不等于「跳过滤镜」，而「这一镜选 none」也不等于「整片不过滤」。

最后两组是补丁口与前端控件那一半：``/shots/i/texture`` 认得、非法值**打回不钳值**、
   指针与镜号对不上要拒，档位清单与标签从命中表的 ``controls.texture`` 单源读出
   （前端抄一份，迟早与闸门对不上）。

为什么这里必须真跑 ffmpeg：这条通道的保证是**像素级的**——「0 帧重烧」说的是 Remotion
那边一次都不提交，而画面确实变了。只断言账面上的档名，就验不出「账上加了、画面没加」
这一族错；那族错恰恰是挪动滤镜位置最可能带来的。

便宜档同 ``test_code_gates.py``：240×320@10fps、``narration=False``；假服务与假缓存
直接 import 那一份，两处各造一套的话，「两个夹具对同一条通道给出不同结论」这种红
就分不清是通道的问题还是夹具的问题。

跑法：容器内 ``PYTHONPATH=/app python tests/test_code_texture.py``。
"""

from __future__ import annotations

import asyncio
import copy
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.mediaops import MediaError                   # noqa: E402
from storyline_server.motion import code_patch as cp               # noqa: E402
from storyline_server.motion import code_render as cr              # noqa: E402
from storyline_server.motion import code_spec as cs                # noqa: E402
from storyline_server.motion import render as rpipe                # noqa: E402
from test_code_gates import (FakeCache, FakeService, W, H, _shot)   # noqa: E402

_fails = 0
_checks = 0
WORK = Path("/tmp") / "code_texture_work"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _fspec(shots: list[dict], *, texture: str = "none") -> dict:
    """整片默认档可指定的夹具（``_spec`` 那份把 texture 写死成 none，改不了）。"""
    raw = {"title": "质感夹具", "aspect": "portrait", "fps": 10, "narration": False,
           "caption_style": "on_picture", "texture": texture, "shots": shots}
    spec, errors = cs.validate_code_spec(cs.overlay_knobs(dict(raw), {}))
    assert errors == [], f"夹具本身没过契约：{errors}"
    spec = copy.deepcopy(spec)
    spec["width"], spec["height"] = W, H
    return spec


def _tshot(i: int = 1, **over) -> dict:
    """**code/text 按镜号区分**：缓存键里没有镜号（同输入同像素本就该共用一份切片），
    两镜交同一段代码会让「命中缓存的那几镜也被加质感」退化成「只有一镜」。"""
    # 闸⑦「每镜必画」逐镜判：一镜只有文字与色块连 `_fspec` 都过不去，所以夹具里
    # 必须真画出一件东西。用一支裸 `<svg/>`（落 `vector` 那条通道，与基座夹具默认
    # 申报的 `subject_channel` 对齐）——P1（2026-10-17）注入契约另加注入头
    # （读 `getInputProps`、用 `text`、摆 `<CaptionLayer/>`）；加完之后骨架越过
    # `_SHAPE_MIN_TOKENS`(40)，闸⑫ 会开始比相似度——本文件没有「整片闸必须空账」
    # 的断言（见 `test_code_gates._shot` 那段同族说明）。
    base = {"code": f"export const Shot = () => (<FilmLayout>主体{i:02d}<svg />"
                    "{getInputProps().text}<CaptionLayer /></FilmLayout>);",
            "text": f"第 {i} 镜要说的那一句", "min_duration_sec": 2.0}
    base.update(over)
    return _shot(i, **base)


def _bytes(p: Path) -> bytes:
    return p.read_bytes() if p and p.exists() else b""


def _plan(base: dict, **kw):
    """装配器 → ((改后 spec, 改动账) 或 None, 错误原话)。错误要按原话断，一抛就停在第一条。"""
    try:
        return cp.plan_code_patch(base, **kw), ""
    except ValueError as exc:
        return None, str(exc)


async def _run(spec: dict, *, work: Path, client, cache=None):
    try:
        return await cr.render_code_motion(spec, providers=None, work=work,
                                           client=client, cache=cache), None
    except Exception as exc:                            # noqa: BLE001
        return None, exc


def main() -> int:
    async def run() -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

        # ================================================================
        print("\n〔声明期·红〕非法档名带镜号打回")
        spec, errors = cs.validate_code_spec(cs.overlay_knobs(dict(
            {"title": "夹具", "aspect": "portrait", "fps": 10, "narration": False,
             "caption_style": "on_picture", "texture": "none",
             "shots": [_tshot(1, texture="retro8mm"), _tshot(2)]}), {}))
        check(bool(errors) and "s01" in " ".join(errors) and "retro8mm" in " ".join(errors),
              f"这一镜的档名没滤镜链 → 带镜号退回：{errors[:1]}")
        check("film" in " ".join(errors),
              "退回原话把可用档位一起给出（不然用户只能再来一轮猜）")

        print("\n〔声明期·红〕顶层整片默认档同样拒非法值")
        _, errors = cs.validate_code_spec(cs.overlay_knobs(dict(
            {"title": "夹具", "aspect": "portrait", "fps": 10, "narration": False,
             "caption_style": "on_picture", "texture": "vintage", "shots": [_tshot(1)]}), {}))
        check(bool(errors) and "texture" in " ".join(errors),
              f"顶层写错也是红，不静默当 none：{errors[:1]}")

        print("\n〔声明期·绿〕写法归一 + 三种形状各自保住")
        ok = _fspec([_tshot(1, texture="  FILM "), _tshot(2, texture=""),
                     _tshot(3, texture="none")])
        check([s["texture"] for s in ok["shots"]] == ["film", "", "none"],
              f"大写带空格归一成 film；空串与 none **各自保住**没被合并："
              f"{[s['texture'] for s in ok['shots']]}")

        print("\n〔解析〕shot_texture 只认这一份规则")
        film = {"texture": "film"}
        check(cs.shot_texture({"texture": "tv"}, film) == "tv", "点名覆盖整片默认")
        check(cs.shot_texture({"texture": ""}, film) == "film", "空串＝表态交给整片")
        check(cs.shot_texture({"texture": "none"}, film) == "none",
              "显式 none 在整片加档的片子里＝这一镜原片直出（与空串不同）")
        check(cs.shot_texture({}, {"texture": "retro8mm"}) == "none",
              "没归一的旧产物里整片档名不认识 → 退回 none，不猜一档")

        # ================================================================
        print("\n〔缓存键·字节〕换质感必须同键")
        film = _fspec([_tshot(1)], texture="none")
        shot0 = film["shots"][0]
        ident = {"master_hash": "master0000000000", "lockfile_hash": "lockfile000000"}
        k_none = cs.shot_cache_key(shot0, film=film, **ident)
        k_film = cs.shot_cache_key({**shot0}, film=_fspec([_tshot(1)], texture="film"),
                                    **ident)
        check(k_none == k_film,
              "整片从 none 换 film → 切片缓存键逐字节相同（换档 0 镜重烧的**键侧**根据）")
        own = cs.shot_cache_key({**shot0, "texture": "tv"}, film=film, **ident)
        check(own == k_none, "只换这一镜的档 → 同键（逐镜覆盖也不进键）")
        control = cs.shot_cache_key({**shot0, "text": "换了个说法"}, film=film, **ident)
        check(control != k_none,
              "对照：改文案必须换键——没这条，上面的「同键」可能只是夹具没进键")
        check("texture" not in cs.SHOT_KNOBS and "texture" not in cs.FILM_KNOBS,
              "texture 两块旋钮名单都不收（上面那两条才成立）")

        # ================================================================
        print("\n〔渲染〕整片加档：逐镜账、滤镜串、交付的切片与代表帧")
        svc = FakeService(mode="ok")
        base = _fspec([_tshot(1), _tshot(2)], texture="none")
        out1, err = await _run(base, work=WORK / "burn", client=svc)
        check(err is None, f"基线（都不加档）一路到底：{err}")
        check(len(svc.calls) == 2, f"基线烧了 2 镜（提交次数 {len(svc.calls)}）")

        out2, err = await _run(_fspec([_tshot(1), _tshot(2)], texture="film"),
                               work=WORK / "burn_film", client=svc)
        check(err is None, f"整片换 film 一路到底：{err}")
        check(out2["texture"] == "film", "顶层这栏现在是**整片默认档**")
        check(out2["texture_by_shot"] == {"s01": "film", "s02": "film"},
              f"逐镜点名实际跑的档：{out2['texture_by_shot']}")
        for sid, f in out2["shot_files"].items():
            check(f["clip"].name == "clip_tex.mp4" and f["frame"].name == "frame_tex.png",
                  f"{sid} 交付的是加过质感的那一份切片与代表帧：{f['clip'].name}")
        check(all(str(r.get("texture_filter")) for r in out2["shots"]),
              "每一镜账上都带着**实际消费的滤镜串**（空串就是「选了档却没跑」）")
        check(out1["shot_files"]["s01"]["clip"].read_bytes()
              != out2["shot_files"]["s01"]["clip"].read_bytes(),
              "同一镜换了档，交付切片的字节真的不同（不是只改了账面）")

        # ================================================================
        print("\n〔保证〕换质感 0 帧重烧，而命中缓存的镜照样被加上质感")
        cache = FakeCache()
        svc = FakeService(mode="ok")
        plain, err = await _run(_fspec([_tshot(1), _tshot(2)], texture="none"),
                                work=WORK / "c1", client=svc, cache=cache)
        check(err is None and len(svc.calls) == 2,
              f"第一趟正常烧 2 镜（{len(svc.calls)} 次提交）")
        before = len(svc.calls)
        rep, err = await _run(_fspec([_tshot(1), _tshot(2)], texture="tv"),
                              work=WORK / "c2", client=svc, cache=cache)
        check(err is None, f"整片换 tv 一路到底：{err}")
        check(len(svc.calls) == before,
              f"Remotion 一次都没再提交（{before} → {len(svc.calls)}）＝0 帧重烧")
        check(rep["cached_shots"] == 2,
              f"两镜都命中缓存：cached_shots={rep['cached_shots']}")
        check(rep["texture_by_shot"] == {"s01": "tv", "s02": "tv"},
              f"命中缓存 ≠ 跳过滤镜：{rep['texture_by_shot']}")
        raw = WORK / "c2" / "shot00" / "cached_clip.mp4"
        check(_bytes(rep["shot_files"]["s01"]["clip"]) != _bytes(raw),
            "加档后的交付切片 ≠ 缓存里取出的那份原片（**像素真变了**，不是只记了档名）")
        check(raw.exists(), "缓存里存的那一份就是加质感之前的切片（键与内容同一口径）")

        # ================================================================
        print("\n〔逐镜覆盖〕跟随 / 点名另一档 / 显式不要，三镜同时在一版片子里")
        svc = FakeService(mode="ok")
        mixed = _fspec([_tshot(1), _tshot(2, texture="glow"), _tshot(3, texture="none")],
                       texture="film")
        out3, err = await _run(mixed, work=WORK / "mix", client=svc)
        check(err is None, f"混档一路到底：{err}")
        check(out3["texture_by_shot"] == {"s01": "film", "s02": "glow", "s03": "none"},
              f"三种形状各得其所：{out3['texture_by_shot']}")
        rec3 = {r["id"]: r for r in out3["shots"]}
        check(rec3["s01"]["texture_filter"] and rec3["s02"]["texture_filter"]
              and rec3["s01"]["texture_filter"] != rec3["s02"]["texture_filter"],
              "两镜各跑自己那一档的滤镜串（母带一趟只能选一档，做不到这件事）")
        check(not rec3["s03"]["texture_filter"]
              and out3["shot_files"]["s03"]["clip"].name != "clip_tex.mp4",
              "显式 none 的那一镜原样进拼接，没被整片默认档拖着加一层")
        check(out3["texture_filter"] == rec3["s01"]["texture_filter"],
              f"顶层报的是**实际消费过**的那条串：{out3['texture_filter'][:40]}…")

        # ================================================================
        print("\n〔红〕选了档却没拿到滤镜串：宁可这一镜退回，不出「账上加了、画面没加」的片")
        saved = dict(rpipe.TEXTURE_FILTERS)
        rpipe.TEXTURE_FILTERS.pop("film", None)
        try:
            out, err = await _run(_fspec([_tshot(1)], texture="film"),
                                  work=WORK / "nochain", client=FakeService(mode="ok"))
        finally:
            rpipe.TEXTURE_FILTERS.clear()
            rpipe.TEXTURE_FILTERS.update(saved)
        check(isinstance(err, MediaError) and "滤镜串" in str(err),
              f"契约有这一档、执行层没这条链 → 当场抛：{str(err)[:70]}")
        check(not (WORK / "nochain" / "final.mp4").exists(),
              "抛在拼接之前：母带没开始，不会出一版原片冒充加了质感的片")
        check(rpipe.TEXTURE_FILTERS.get("film") == saved.get("film"),
              "夹具把滤镜表还原了（下一条用例拿的是真表）")

        # ================================================================
        print("\n〔补丁口〕/shots/i/texture 认、落盘、且改完仍过真契约")
        base = _fspec([_tshot(1), _tshot(2)], texture="film")
        pair, err = _plan(base, edits=[{"pointer": "/shots/1/texture",
                                        "value": "TV", "shot": "s02"}])
        got = pair[0] if pair else None
        check(err == "", f"指针被接受：{err[:80]}")
        check(got and got["shots"][1]["texture"] == "tv",
              f"落的是归一后的档名：{got and got['shots'][1].get('texture')}")
        recheck, errors = cs.validate_code_spec(dict(got))
        check(errors == [] and recheck["shots"][1]["texture"] == "tv",
              f"改完再过一遍真契约，值没被归一转丢：{errors[:1]}")

        # 「同值重交」会被装配器拒（那条规则是故意的），所以这一条要从**已经点过档**的镜起步
        declared = _fspec([_tshot(1, texture="glow"), _tshot(2)], texture="film")
        pair, err = _plan(declared, edits=[{"pointer": "/shots/0/texture",
                                            "value": "", "shot": "s01"}])
        got = pair[0] if pair else None
        check(err == "" and got and got["shots"][0]["texture"] == "",
              f"空串是合法值（＝这一镜收回表态、跟随整片）：{err[:60]}")

        _, err = _plan(base, edits=[{"pointer": "/shots/0/texture",
                                     "value": "retro8mm", "shot": "s01"}])
        check("retro8mm" in err and "没有对应的滤镜链" in err and "不钳值" in err,
              f"非法档打回、**不钳值**（钳了就是账面一档、画面另一档）：{err[:70]}")
        pair, _ = _plan(base, edits=[{"pointer": "/shots/0/texture",
                                      "value": "retro8mm", "shot": "s01"},
                                     {"pointer": "/shots/1/texture",
                                      "value": "tv", "shot": "s02"}])
        check(pair is None, "一批里有一条非法 → 整批不落盘，不出「半改半没改」的版本")

        _, err = _plan(base, edits=[{"pointer": "/shots/0/texture",
                                     "value": "tv", "shot": "s02"}])
        check("对不上" in err and "/shots/0/texture" in err,
              f"指针与镜号对不上要拒（质感这条不例外）：{err[:70]}")

        # ================================================================
        print("\n〔补丁口·层E 新栏〕字幕档与转场也得有入口——新能力没有入口＝没有这个能力")
        norm = _fspec([_tshot(1), _tshot(2)])
        # 「这一镜**长什么样**」那一组：文案 / 代码 / 时长 / 质感 / 构图 / 字幕形态 / 接缝。
        look = {"code", "text", "highlight", "fields", "min_duration_sec", "texture",
                "source", "bleed", "bg", "caption_style", "highlight_style",
                "transition",
                # 2026-10-18：只改 `brief` 不给 `code` ＝ 重画这一镜（写手按新
                # brief 重写）。白名单跟着加，测试的期望值也要跟上。
                "brief"}
        check(set(cp.EDITABLE_SHOT_FIELDS) == look,
              f"白名单恰好是这一组（多/少的是 {sorted(set(cp.EDITABLE_SHOT_FIELDS) ^ look)}）")
        # 反面：那几栏**申报**故意不在里面——它们各自有一道闸在读，改申报而不改代码
        # ＝让闸读到假的（把 `card` 从 A 换成 B 而代码还是 A 的形状，闸⑮ 会以为
        # 这一族又多了一镜）。改它们只有一条正路：连 `code` 一起重交。
        # 2026-10-16：原来是七栏，`assets` 与 `reveal` 随供图那三条路一起出库了，
        # 这一份清单跟着收成五栏（它是以「今天镜头里有哪几栏」为准的现读账）。
        # 现读账：归一之后的镜头键减去白名单。`caption_note` / `style_note` 随
        # 「这一镜刻意不出字幕 / 刻意偏离 style_facts」那条通道的收口退出，`entities`
        # 随「本镜实体」申报进来——以今天实际归一出来的键为准，多一栏少一栏都在这里红。
        declared = sorted(set(norm["shots"][0]) - look - {"id"})
        check(declared == ["card", "derived_from", "entities", "motion",
                           "subject", "subject_channel"],
              f"归一之后的镜头键减去白名单＝这六栏申报（漏了谁就是它悄悄进了白名单）："
              f"{declared}")
        check(not (set(declared) & set(cp.EDITABLE_SHOT_FIELDS)),
              "两边不相交：白名单只有一份，没有第二处漏进来的口子")
        pair, err = _plan(norm, shot_sets=[
            {"shot": "s01", "set": {"caption_style": "paper", "highlight_style": "ink",
                                    "transition": "wipeleft"}}])
        got = pair[0] if pair else None
        check(err == "" and got and got["shots"][0]["caption_style"] == "paper"
              and got["shots"][0]["highlight_style"] == "ink"
              and got["shots"][0]["transition"] == "wipeleft",
              f"整镜改写真落得下这三栏：{err[:60]}")
        recheck, errors = cs.validate_code_spec(dict(got))
        check(errors == [] and recheck["shots"][0]["caption_style"] == "paper"
              and recheck["shots"][0]["highlight_style"] == "ink"
              and recheck["shots"][0]["transition"] == "wipeleft",
              f"改完仍过真契约，三栏都没被归一转丢：{errors[:1]}")
        # 「改得进去」还只是账面事实——这一栏得真进到拼接计划里才算数
        seams = cs.transition_plan(recheck)["seams"]
        check([(s["after"], s["style"]) for s in seams] == [("s01", "wipeleft")],
              f"`transition` 落在**这一镜之后**那处接缝上：{seams}")
        pair, _ = _plan(norm, shot_sets=[
            {"shot": "s01", "set": {"transition": "wipeleft"}},
            {"shot": "s02", "set": {"transition": "circleopen"}}])
        seams2 = cs.transition_plan(cs.validate_code_spec(dict(pair[0]))[0])["seams"]
        check([s["after"] for s in seams2] == ["s01"],
              f"对照：**末镜**写这一栏不产生接缝（`transition_plan` 只遍历 `shots[:-1]`，"
              f"末镜之后没有接缝可转）：{[s['after'] for s in seams2]}")
        _, errors = cs.validate_code_spec({**norm, "shots": [
            {**norm["shots"][0], "caption_style": "bottom"}, norm["shots"][1]]})
        check(any("caption_style" in e and "bottom" in e for e in errors),
              f"坏档名在落笔之后被点名打回（补丁口只管「改在对的地方」）：{errors[:1]}")

        # ================================================================
        print("\n〔单源〕档位清单与标签从契约出，前端与计划卡不各抄一份")
        check(list(cs.TEXTURES) == ["none", "film", "tv", "glow", "bleach"],
              f"契约那侧的档位：{cs.TEXTURES}")
        check(set(rpipe.TEXTURE_FILTERS) == set(cs.TEXTURES) - {"none"}
              and all(rpipe.TEXTURE_FILTERS.values()),
              "除 none 外每一档都有非空滤镜串（少一条就是上面那个红）")
        check("none" not in rpipe.TEXTURE_FILTERS,
              "none 没有滤镜串——它就是「原样进拼接」，不是某条链的别名")
        # 2026-10-20 可见度地板：暗到看不清才提，正常亮度一个字节不动。
        _lift_dark = rpipe.luma_lift_chain(20)
        check(_lift_dark is not None and _lift_dark[0].startswith("[0:v]eq=gamma="),
              f"平均亮度 20 的切片拿到提亮链：{(_lift_dark or ('', ''))[1]}")
        check(rpipe.luma_lift_chain(120) is None,
              "平均亮度 120 已经够亮，不补任何滤镜（不把正常片子洗白）")
        check(rpipe.luma_lift_chain(20, floor=0) is None,
              "MOTION_LUMA_FLOOR=0 时这条地板关闭")
        props = cs.knob_props()["texture"]
        check(props.get("enum") == list(cs.TEXTURES) and props.get("value_labels"),
              f"控件的 enum + 中文标签由契约带回（前端界面读 controls.texture）："
              f"{list(props.get('value_labels') or {})}")
        check(cs.EDITABLE_POINTERS.count("/shots/{i}/texture") == 1
              and len(cs.EDITABLE_POINTERS) == 5,
              f"指针白名单五条且含质感这条：{cs.EDITABLE_POINTERS}")

    asyncio.run(run())
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
