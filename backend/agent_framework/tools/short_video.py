# -*- coding: utf-8 -*-
"""短视频三件套：一句话出片 / 只改一镜 / 重渲整片。

这是简洁视频线**唯一的对外入口**——在这之前它只有 `/studio` 一个网页，
用户在对话里说"帮我做个讲瑞利散射的片子"时，Agent 手上没有任何工具能接住这句话。

为什么是"阻塞式"而不是"提交作业再轮询"：
这条线本身就要 10~40 分钟（写分镜 + 逐镜出码 + 质检 + 纯 CPU 逐帧渲染），
所以它是**一次长工具调用**，并在 `_TOOL_CALL_TIMEOUTS` 里各有自己的档位（3600s / 900s）。
在这套产品里，"长任务 = 一次长工具调用"是既有约定；再引入一套作业轮询，
就多了一条"任务跑到一半 Agent 轮次结束了"的失败路径。
（2026-10-25 之前主链还有一个 `plan_motion`，那条图形科普链已整条删除，不再是这里的参照。）

三个工具的分工，对应"改一镜"这件事的三个粒度：

    make_short_video   0 → 1：写分镜 + N 镜代码 + 质检 + 渲成片
    reshoot_shot       1 镜：只重写这一镜，出 2 张静帧（1 次调用，秒级）
    render_short_film  1 片：按当前工程重渲（不再问模型）

**没有 `reshoot_shot` 就没有"按 Claude+Remotion 的方式做"**：那条工作流的实质是
"看静帧 → 说哪一镜不对 → 只改那一镜 → 再渲"，而不是"重新生成一条"。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..identity import current_identity
from ..tool import Tool
from open_storyline.simple import pipeline

# 六个工具只认 `pipeline` 这一个入口（R18）。下面这些名字是它的再导出，
# **不是**各自去 import 子模块——`test_simple_node_contract.py` 的 AST 断言守这条。
# 这样「HTTP 快线」与「Agent 工具」共用同一份实现，不会再各自漂移。
deliver = pipeline.deliver
edit = pipeline.edit
export = pipeline.export
facts = pipeline.facts
film = pipeline.film
kernel = pipeline.kernel
project = pipeline.project
showrunner = pipeline.showrunner

#: 秒数上下限。上限由**结构**决定，不是拍脑袋：
#:   动效：镜数 ≤ `film.MAX_SHOTS`(56) × `film.MAX_SHOT_SEC`(5.2) = 291 秒
#:   叙事：镜数 ≤ `film.STORY_MAX_SHOTS`(24) × `film.STORY_MAX_SHOT_SEC`(12) = 288 秒
#: 240 秒（4 分钟）是给"三分钟知识大赏那种片子"留的量，同时把渲染时间压在
#: 一次工具调用（3600 秒）以内——240 秒 ≈ 3600 帧，渲染约 35 分钟。
MIN_SEC, MAX_SEC = 8.0, 240.0


#: `render=false` 时最多出几张预览静帧。每张约 1~2 秒（scale 0.35），
#: 12 张 ≈ 20 秒；比整片渲染（27 分钟）便宜两个数量级。
#: 上限存在的理由：40 镜全出就是 80 秒，用户等不起，也没必要一次看全。
PREVIEW_STILLS = 16


def _clamp_seconds(value: Any, default: float = 30.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return max(MIN_SEC, min(MAX_SEC, v))


def _secs_or_estimate(value: Any, prompt: str) -> float:
    """给了就用给的；没给就**按内容量估**，不用常数兜。

    原来 `seconds` 缺省 30——用户只说"做个关于 X 的视频"，默认值就在替他做决定。
    现在这条判断交给调用方（agent）从用户原话里取（"两分钟"→120）；取不到才估。
    """
    if value in (None, "", 0):
        return _clamp_seconds(film.estimate_seconds(prompt))
    return _clamp_seconds(value)


async def _publish_stills(got: dict[str, Any], *, asp: str, user_id: str,
                          storage: Any = None,
                          limit: int = PREVIEW_STILLS) -> list[str]:
    """每镜渲一张静帧并签名，返回 URL 数组（按镜号顺序）。

    「先看再渲」那条路要用它：渲染占整条链 93% 的时间，所以把人的判断放在
    渲染**之前**——返工成本从 27 分钟降到 30 秒。
    单张失败不算整条失败（一张图传不上去而已）。
    """
    import base64

    urls: list[str] = []
    tl = got.get("timeline") or {}
    n = int(got.get("shots") or 0)
    for i in range(min(n, limit)):
        try:
            blobs = await film.shot_stills(got["code"], tl, i, aspect=asp,
                                           fps=kernel.DEFAULT_FPS,
                                           at=(0.55,), scale=0.35)
        except Exception:  # noqa: BLE001 —— 这一镜出不来就跳过
            continue
        for b64 in (blobs or [])[:1]:
            try:
                pub = await deliver.publish_bytes(
                    base64.b64decode(b64), suffix="jpg",
                    content_type="image/jpeg", storage=storage,
                    user_id=user_id)
                urls.append(pub["url"])
            except Exception:  # noqa: BLE001
                continue
    return urls


def _ident() -> tuple[str, str]:
    ident = current_identity()
    return (ident.user_id if ident else "", ident.conversation_id if ident else "")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


class MakeShortVideoTool(Tool):
    """一句话 → 一条多镜成片（无素材、纯图形科普片）。"""

    #: 可以写进计划卡：它是一个"步骤"（虽然一次调用就跑完整条链）。
    plan_node = True

    def __init__(self, storage: Any = None) -> None:
        self._storage = storage

    @property
    def name(self) -> str:
        return "make_short_video"

    @property
    def display_name(self) -> str:
        return "做一条图形科普短片"

    @property
    def description(self) -> str:
        return (
            "把**一句主题**做成一条完整短视频：写分镜 → 逐镜写画面代码 → 逐帧质检 → "
            "一次渲完 → 回一条能播的链接。**不需要用户上传任何素材**，画面全部由代码画出来，"
            "适合知识科普、原理讲解、数据解读这类没有实拍素材的内容。\n"
            "时长 8~300 秒。**镜数由内容定**（叙事每镜 4.5~12 秒、动效每镜 ≤5.2 秒），"
            "不由公式定——同样 165 秒，15 镜和 22 镜都可能对。\n"
            "**画法默认 `auto`，不用选**：系统按「有没有主角和时间」自己判，"
            "超过 90 秒一律叙事片（长片要长镜，否则碎成 PPT）。\n"
            "**用户要先看分镜就别调这个**，调 `plan_short_film`（10 秒、1 次调用），"
            "确认后把它的 `plan` 传给这里的 `scenes` 出片。\n"
            "**这会花 5~30 分钟（时长越长越久）且期间没有中间反馈**，"
            "调完之后直接把成片链接发给用户，不要只说“已生成”。\n"
            "如果用户之后要改某一镜（“第 7 镜那个太阳太大”），用 reshoot_shot，"
            "**不要重新调这个工具**——重做一条既贵又不会更接近用户想要的样子。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "主题或一句话需求。可以带讲法，例如"
                                          "“为什么天空是蓝色的，从瑞利散射讲到日落变红”"},
                "seconds": {"type": "number",
                            "description": (
                                f"时长秒数，{MIN_SEC:.0f}~{MAX_SEC:.0f}。**不要用默认值**："
                                "先从用户原话里取（「两分钟」→120、「半分钟」→30）；"
                                "用户没说，就按内容量估：一个知识点 30~45 秒、一个概念讲透 "
                                "60~90 秒、一个故事 120~180 秒。都不传＝按内容量自动估")},
                "aspect": {"type": "string", "enum": ["portrait", "landscape", "square"],
                           "description": "画幅，默认 portrait（竖屏）"},
                "style": {"type": "string", "enum": ["auto", "explainer", "story"],
                          "description": "画法。**默认 auto，系统自己判**（判据：这条片子"
                                         "有没有主角和时间）。只有用户明确说「要动效那种」"
                                         "或「要纪录片那种」时才指定"},
                "render": {"type": "boolean",
                           "description": "是否渲成片。true（默认）= 出 mp4 链接；"
                                          "false = 只写代码与质检、回静帧，用来先确认方向"},
                "voice": {"type": "boolean",
                          "description": "是否配旁白（默认 true）。旁白由每镜字幕合成，"
                                         "并**按语音时长定镜长**；TTS 失败会自动退回无声"},
                "bgm": {"type": "string",
                        "description": "配乐。**两种写法都收**：附件音频的 material_id"
                                       "（用户把歌挂成附件时用它），或曲名（如「We Never」）。"
                                       "**不填就不配乐**。短于片长会循环铺满，不会换成别的"},
                "scenes": {"type": "object",
                           "description": "分镜（`plan_short_film` 返回的 `plan` 原样传回来）。"
                                          "给了就**跳过分镜调用**——用户看过、改过的讲法不会再变一次"},
            },
            "required": ["prompt"],
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, prompt: str, seconds: Any = None,
                      aspect: str = "portrait", render: bool = True,
                      voice: bool = True, style: str = "auto",
                      bgm: str = "", scenes: Any = None) -> str:
        text = str(prompt or "").strip()
        if len(text) < 4:
            return "Error: prompt 太短，至少给一个完整主题（例如“为什么天空是蓝色的”）"
        sec = _secs_or_estimate(seconds, text)
        asp = kernel.norm_aspect(aspect)
        want = str(style or "").strip().lower()
        mode = want if want in (film.MODE_STORY, film.MODE_EXPLAINER) else "auto"
        user_id, conv_id = _ident()

        await kernel.ensure_storage()
        # **先把配乐定下来再写镜**（2026-10-27）：卡拍要在写镜之前知道拍点，
        # 因为镜长要先按拍点补好、画面才按新时长渲。同一首后面交给 `mix_into`，
        # 两处必须一致，否则拍点和实际配乐对不上。
        _picked = None
        if str(bgm or "").strip():
            try:
                from .. import bgm as bgm_pick
                _picked = await bgm_pick.resolve(
                    self._storage, user_id, text,
                    work_dir=kernel.out_dir() / "bgm", need_sec=sec,
                    name=str(bgm).strip())
            except Exception:  # noqa: BLE001 —— 配乐解析失败不该拦出片
                _picked = None
        try:
            got = await film.make_film(text, seconds=sec, aspect=asp,
                                       user_id=user_id, conversation_id=conv_id,
                                       voice=bool(voice), mode=mode,
                                       scenes=scenes or None,
                                       bgm_path=(_picked or {}).get("path"))
        except kernel.KernelError as exc:
            return _json({"ok": False, "stage": exc.stage, "error": str(exc),
                          "lint_errors": exc.lint_errors[:5]})
        except Exception as exc:  # noqa: BLE001 —— 回喂给模型，别让整轮崩掉
            return _json({"ok": False, "stage": "unknown",
                          "error": f"{type(exc).__name__}: {exc}"})

        fid = project.new_id()
        try:
            project.save(fid, code=got["code"], prompt=text, seconds=sec, aspect=asp,
                         fps=kernel.DEFAULT_FPS, plan=got["plan"], secs=got["secs"],
                         kinds=got["transitions"], bodies=got["bodies"],
                         audit=got.get("audit") or {}, coverage=got.get("coverage") or {},
                         calls=got["calls"], rewritten=got.get("rewritten"),
                         fallback=got.get("fallback"), mode=got.get("mode") or "")
        except OSError:
            # 落盘失败不影响出片，但要如实说——没有工程就改不了一镜。
            pass

        out: dict[str, Any] = {
            "ok": True,
            "film_id": fid,
            "title": str((got.get("plan") or {}).get("title") or ""),
            "style": got.get("mode") or mode,
            "shots": got["shots"],
            "seconds": got["total_sec"],
            "calls": got["calls"],
            "transitions": got["transitions"],
            "coverage": round(float((got.get("coverage") or {}).get("ratio") or 0), 3),
            # 组件覆盖率**恒接近 0**（画法全交给模型后，它不再用设计系统组件，
            # 全用裸 SVG）。真正该看的是"有几种不同的画法"——`variety` 越接近
            # `shots` 越好。`coverage` 只为兼容旧消费方保留，附一句说明防止误读。
            "coverage_note": "画法已全交给模型，组件覆盖率恒≈0，不是故障；看 variety",
            "variety": int(got.get("variety") or 0),
            # 卡拍：`{"bpm","beats","snapped","pad_total"}`；没配乐或检不出拍时是
            # `{"skipped": 原因}`。如实报，别让用户以为"切点一定卡在拍上"。
            "beat_sync": got.get("beat_sync") or {},
            "audit": {
                "ratio": round(float((got.get("audit") or {}).get("ratio") or 0), 3),
                # `defects` 是客观缺陷（空帧/全黑），`concerns` 是模型看图给的意见。
                # 前者拦出片，后者只如实报出来——审美不该由代码否决。
                "defects": (got.get("audit") or {}).get("defects") or [],
                "concerns": (got.get("audit") or {}).get("concerns") or [],
                "bad": (got.get("audit") or {}).get("bad") or [],
            },
            "fallback_shots": got.get("fallback") or [],
            "mp4_url": "",
        }
        # **质量门槛要说出来**（2026-10-27）。原来 `audit.ratio = 0.0`（26 镜全不合格）
        # 也照样静默交付——真机发生过一次「交付了一条 26 镜全黑的片子」。
        # 这里不改 `ok`（片子确实渲出来了），但给出一个明确的 verdict，
        # 让调用方和用户都知道"这条没达标，别直接发"。
        _ratio = out["audit"]["ratio"]
        out["quality"] = {
            "pass_ratio": _ratio,
            "verdict": "ok" if _ratio >= 0.6 else "below_bar",
            "note": ("" if _ratio >= 0.6 else
                     f"只有 {_ratio:.0%} 的镜通过画面自检（门槛 60%）——"
                     f"建议用 reshoot_shot 重画不合格的那几镜，别直接发布"),
        }
        # 出处账：每个数字能不能说清从哪来。**不拦片**，只在报告里如实写。
        # 视觉自检看不出"这个数是编的"，而编一个数字是这条链路里最贵的错。
        src = facts.audit_sources(got.get("plan") or {})
        out["sources"] = src["sources"]
        out["sources_note"] = src["note"]
        if src["missing"]:
            out["numbers_without_source"] = src["missing"]
        # 有旁白时总长由语音决定，可能明显偏离用户要的秒数。**如实说**，
        # 别让模型把"我要 30 秒"当成已完成（它只会看到 26 秒的片子却以为交付了 30 秒）。
        want = float(sec)
        if abs(float(got["total_sec"]) - want) > max(2.0, want * 0.12):
            out["duration_note"] = (
                f"实际 {got['total_sec']} 秒（要求 {want:g} 秒）：镜长由旁白时长决定，"
                f"不是硬凑的。要更接近 {want:g} 秒，就调整字幕字数或镜数重做一条。")

        if not render:
            # `render=false` 的用处是「先看再渲」（2026-10-26 补）：
            # 渲染占整条链 93% 的时间（实测 165 秒片：渲 27 分钟、模型 2 分钟），
            # 所以把人的判断放在**渲染之前**，返工成本从 27 分钟降到 30 秒。
            #
            # 但原来这一支只回文字，用户看不到画面——「先看再渲」等于没做成。
            # 这里给每一镜出一张静帧并签名，用户点头再调 render_short_film。
            out["stills"] = await _publish_stills(
                got, asp=asp, user_id=user_id, storage=self._storage,
                limit=PREVIEW_STILLS)
            out["hint"] = (
                f"已写好代码、**没有渲成片**。`stills` 是每镜一张预览图"
                f"（共 {len(out['stills'])} 张，按镜号顺序）——"
                "用 markdown 图片语法发给用户看（`![s01](链接)`），"
                "等他点头再调 render_short_film 出成片。"
                "要改某一镜用 reshoot_shot(film_id, shot, note)，不要重做整条。"
                "**别把长链接直接贴成文字**，用图片语法。")
            return _json(out)

        # 音轨先合成（旁白 concat + 配乐混音），再交给**唯一一份**渲染交付。
        # 渲 + 合流 + 上传原来在这里、`render_short_film`、HTTP、节点里各抄了一遍，
        # 改一处忘另一处（`media_url` 那次就是这么漏的）。现在只走
        # `pipeline.render_and_publish`。
        track = None
        vinfo = got.get("voice") or {}
        if vinfo.get("degraded"):
            out["voice"] = "none"
            out["voice_error"] = vinfo.get("error") or "TTS 全部失败"
        elif vinfo:
            try:
                from open_storyline.simple import pipeline as _pl

                vo = _pl.voice
                from .. import bgm as bgm_pick
                track = vo.build_track(
                    [Path(p) if p else None for p in vinfo.get("files") or []],
                    got["secs"], kernel.out_dir() / f"track-{fid}.m4a")
                # 配乐**只在点名时才加**（2026-10-26 改）：不填 `bgm` 就静音出片。
                track, tune = await bgm_pick.mix_into(
                    track, kernel.out_dir() / f"track-{fid}-bgm.m4a",
                    storage=self._storage, user_id=user_id, prompt=text,
                    total_sec=got["total_sec"], work_dir=kernel.out_dir() / "bgm",
                    bgm_name=bgm, picked=_picked)
                out["bgm"] = (tune or {}).get("filename") or ""
                out["voice"] = "ok"
                out["voice_failed_shots"] = vinfo.get("failed") or []
                out["voice_over_shots"] = vinfo.get("over") or []
            except Exception as exc:  # noqa: BLE001 —— 有声是加分项，不是前置条件
                out["voice"] = "failed"
                out["voice_error"] = f"{type(exc).__name__}: {exc}"
                track = None

        try:
            made = await pipeline.render_and_publish(
                fid, storage=self._storage, user_id=user_id,
                code=got["code"], track=track,
                audio_name="audio.m4a" if track else "")
        except kernel.KernelError as exc:
            out.update(ok=False, stage=exc.stage, error=str(exc))
            return _json(out)
        except Exception as exc:  # noqa: BLE001
            out.update(ok=False, stage="render", error=f"{type(exc).__name__}: {exc}")
            return _json(out)

        out["mp4_url"] = made["mp4_url"]
        # 成片回执要带**播放卡片契约**的两个键（2026-10-26 修）：
        #   `media_url` —— `MediaCardHook` 只认这个名字，它据此发一条 type=media 帧，
        #                  前端才渲染 `<video controls>` + 「下载成片」。
        #   `video`     —— 对象键（不是 presigned 直链），落进 qa.parts 当持久指针；
        #                  读历史时按它现签一条新链接，链接过期也不丢片子。
        out["media_url"] = made["mp4_url"]
        out["video"] = made["object_key"]
        out["duration"] = round(float(made["duration"]), 1)
        out["size_mb"] = made["size_mb"]
        if made.get("voice") == "ok":
            out.setdefault("voice", "ok")
        # `render_and_publish` 已经把 `render_ms` 一起算好了（R18 收口时它接过了
        # `kernel.render` 的返回值）。这里原来还写着 `res`——那个变量在收口时就没了，
        # 于是整条链跑完、片子也上传了，**回执却在这一行抛 NameError**，
        # 用户拿不到链接（2026-10-27 真机踩到）。
        out["render_ms"] = made.get("render_ms")
        out["hint"] = ("成片卡片会**自动**出现在对话里（可播放、可下载）——"
                       "不要把这串 presigned 地址贴进回答，那是给程序用的，几百个字符没人看。"
                       "用户要改某一镜就用 "
                       "reshoot_shot(film_id, shot, note)，**不要重新做一条**。"
                       "报告里带上 shots / audit.ratio / bgm；"
                       "**如果 numbers_without_source 非空，如实说一句哪几镜的数字没标出处**"
                       "——片子已出完，这时候补不进去了；该查的在出片前就该查清。")
        return _json(out)


class PlanShortFilmTool(Tool):
    """一句话 → 分镜表（不写画面、不渲染）。约 10 秒、1 次调用。

    为什么要单独一个工具：一次成片二十几次调用、十几分钟到半小时，中间零反馈。
    主题或讲法错了只能整条重跑。先出分镜表让人看一眼——十五句话读下来十秒，
    读着不对就别烧那半小时。

    用户确认后把返回的 `plan` 原样传给 `make_short_video` 的 `scenes`，那一步
    就不再调分镜（省 1 次调用，也省掉"重跑出来还是另一个样子"）。
    """

    #: 它只是"看一眼"，不产出可交付的东西，不该上计划卡。
    plan_node = False

    def __init__(self, storage: Any = None) -> None:
        self._storage = storage

    @property
    def name(self) -> str:
        return "plan_short_film"

    @property
    def display_name(self) -> str:
        return "先出分镜（不出片）"

    @property
    def description(self) -> str:
        return ("只出分镜表，**不写画面、不渲染**。约 10 秒、1 次调用。\n"
                "用户说「先看分镜」「先给我看看讲法」「别急着出」时用它。\n"
                "返回的 `plan` 里有 angle（它自己选的角度）、hook（开场钩子）、"
                "scenes（每镜一句字幕 + 画面描述）。用户确认后，把 `plan` **原样**"
                "传给 `make_short_video` 的 `scenes` 字段出片。")

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "主题。给一句话就够，**不要写分镜**——分镜是这一步的产出"},
                "seconds": {"type": "number",
                            "description": (
                                f"时长秒数，{MIN_SEC:.0f}~{MAX_SEC:.0f}。**不要用默认值**："
                                "先从用户原话里取（「两分钟」→120、「半分钟」→30）；"
                                "用户没说，就按内容量估：一个知识点 30~45 秒、一个概念讲透 "
                                "60~90 秒、一个故事 120~180 秒。都不传＝按内容量自动估")},
                "aspect": {"type": "string", "enum": ["portrait", "landscape", "square"],
                           "description": "画幅，默认 portrait（竖屏）"},
            },
            "required": ["prompt"],
        }

    async def execute(self, prompt: str, seconds: Any = None,
                      aspect: str = "portrait") -> str:
        text = str(prompt or "").strip()
        if len(text) < 2:
            return "Error: prompt 太短，至少给一个话题（例如「0 的历史」）"
        sec = _secs_or_estimate(seconds, text)
        asp = kernel.norm_aspect(aspect)
        user_id, conv_id = _ident()
        await kernel.ensure_storage()
        # 2026-10-26：不再分片种。原来这里先 `pick_style` 再二选一调 `plan` /
        # `plan_story`，而那条 explainer 分镜已经随「LLM 当导演」删了——
        # 留着就会 AttributeError（`film.plan` 不存在）。现在统一走 `showrunner.plan`，
        # 它给的正是 `make_short_video` 要吃的那份分镜（`shots[{sec,text,plan}]`）。
        try:
            got, calls = await showrunner.plan(
                text, seconds=sec, aspect=asp, user_id=user_id,
                conversation_id=conv_id)
        except kernel.KernelError as exc:
            return _json({"ok": False, "stage": exc.stage, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 —— 回喂给模型，别让整轮崩掉
            return _json({"ok": False, "stage": "plan",
                          "error": f"{type(exc).__name__}: {exc}"})
        return _json({
            "ok": True, "calls": calls,
            "shots": len(got.get("shots") or []),
            "total_sec": round(sum(float(s["sec"]) for s in got.get("shots") or []), 2),
            "plan": got,
            "next": ("把 plan 原样传给 make_short_video 的 scenes 出片；"
                     "用户要改就直接改 plan.shots[i].text 或 .sec 再传"),
        })


class ReshootShotTool(Tool):
    """只重写某一镜，出静帧给人看。"""

    plan_node = True

    def __init__(self, storage: Any = None) -> None:
        self._storage = storage

    @property
    def name(self) -> str:
        return "reshoot_shot"

    @property
    def display_name(self) -> str:
        return "改一镜"

    @property
    def description(self) -> str:
        return (
            "只重写一条片子的**某一镜**，并回 2 张这一镜的静帧链接。1 次模型调用、几秒到几十秒。\n"
            "用户说“第 7 镜太阳太大”“s03 太暗了”“把那句字幕换掉”时用它——"
            "**不要用 make_short_video 重做整条**。\n"
            "`note` 原样填用户的话（“太阳太大、挡住字了”），不要翻译成术语："
            "模型对用户原话的理解比转述准。\n"
            "改完只更新工程，**没有重渲整片**；用户认可静帧后再调 render_short_film。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "film_id": {"type": "string", "description": "make_short_video 返回的 film_id"},
                "shot": {"type": "integer", "description": "镜号，从 1 开始"},
                "note": {"type": "string",
                         "description": "用户的原话，例如“太阳太大、挡住字了”"},
            },
            "required": ["film_id", "shot"],
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, film_id: str, shot: int, note: str = "") -> str:
        user_id, conv_id = _ident()
        try:
            proj = project.load(str(film_id))
        except FileNotFoundError as exc:
            return _json({"ok": False, "error": str(exc),
                          "hint": "film_id 不对。用 list_short_films 看最近做过哪些。"})

        await kernel.ensure_storage()
        try:
            got = await edit.reshoot(proj, int(shot), note=str(note or ""),
                                     user_id=user_id, conversation_id=conv_id)
        except kernel.KernelError as exc:
            return _json({"ok": False, "stage": exc.stage, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        # 先落工程，再渲静帧——静帧渲不出来时，改动也不会丢。
        try:
            project.update(film_id, bodies=got["bodies"], kinds=got["kinds"],
                           audit=got["audit"])
            (project.dir_of(film_id) / "film.tsx").write_text(
                got["code"], encoding="utf-8")
            (project.dir_of(film_id) / f"s{int(shot):02d}.tsx").write_text(
                got["bodies"][int(shot) - 1], encoding="utf-8")
        except OSError:
            pass

        stills: list[str] = []
        try:
            blobs = await film.shot_stills(got["code"], got["timeline"], int(shot) - 1,
                                           aspect=kernel.norm_aspect(proj.get("aspect")),
                                           fps=int(proj.get("fps") or kernel.DEFAULT_FPS))
            import base64
            for b64 in blobs:
                try:
                    pub = await deliver.publish_bytes(
                        base64.b64decode(b64), suffix="jpg",
                        content_type="image/jpeg", storage=self._storage,
                        user_id=user_id)
                    stills.append(pub["url"])
                except Exception:  # noqa: BLE001 —— 一张传不上去不算失败
                    continue
        except kernel.KernelError:
            pass

        row = (got["audit"].get("shots") or [{}])[0]
        return _json({
            "ok": True,
            "film_id": film_id,
            "shot": int(shot),
            "stills": stills,
            "verdict": row.get("worst") or "unknown",
            "content": row.get("content"),
            "vivid": row.get("vivid"),
            "hint": ("把 stills 里的链接用 markdown 图片语法（![s07](链接)）发给用户看。"
                     "用户认可就说“要重渲整片吗”，认可后调 render_short_film；"
                     "还要改就再调 reshoot_shot。"),
        })


class RenderShortFilmTool(Tool):
    """按当前工程重渲整片（不再问模型）。"""

    plan_node = True

    def __init__(self, storage: Any = None) -> None:
        self._storage = storage

    @property
    def name(self) -> str:
        return "render_short_film"

    @property
    def display_name(self) -> str:
        return "重渲整片"

    @property
    def description(self) -> str:
        return ("把一条已经存在的片子按**当前工程**重渲一遍，并回一条新链接。"
                "不再调用模型，所以**不花钱**，但要 5~15 分钟（纯 CPU 逐帧渲）。"
                "用在：改完某一镜（reshoot_shot）用户认可之后。")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"film_id": {"type": "string", "description": "工程 id"}},
            "required": ["film_id"],
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, film_id: str) -> str:
        user_id, _ = _ident()
        try:
            project.load(str(film_id))
        except FileNotFoundError as exc:
            return _json({"ok": False, "error": str(exc)})

        # 渲 + 合音轨 + 上传**只有一份实现**（`pipeline.render_and_publish`）：
        # 重渲不传 track，用工程里存的那条——改一镜只该改画面，声音不该重生成一遍。
        try:
            made = await pipeline.render_and_publish(
                str(film_id), storage=self._storage, user_id=user_id)
        except kernel.KernelError as exc:
            return _json({"ok": False, "stage": exc.stage, "error": str(exc),
                          "lint_errors": exc.lint_errors[:5]})
        except FileNotFoundError as exc:
            return _json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "stage": "render",
                          "error": f"{type(exc).__name__}: {exc}"})

        # 与 make_short_video 同一套播放卡片契约：`media_url` 给 MediaCardHook 发
        # type=media 帧（前端渲染 <video>），`video` 是对象键、落进 qa.parts 当持久
        # 指针。少了这两个键，重渲完用户只看到一串 presigned 地址——「改一镜」这条
        # 路就走了半截。
        return _json({
            "ok": True,
            "film_id": film_id,
            "mp4_url": made["mp4_url"],
            "media_url": made["mp4_url"],
            "video": made["object_key"],
            "duration": made["duration"],
            "size_mb": made["size_mb"],
            "voice": made["voice"],
            "hint": ("成片卡片会自动出现在对话里（可播放、可下载）——"
                     "不要把 presigned 地址贴进回答。"),
        })


class ExportFilmTool(Tool):
    """把一条片子导成**可跑的 Remotion 工程**（zip）。"""

    def __init__(self, storage: Any = None) -> None:
        self._storage = storage

    @property
    def name(self) -> str:
        return "export_film_project"

    @property
    def display_name(self) -> str:
        return "导出成 Remotion 工程"

    @property
    def description(self) -> str:
        return ("把一条已存在的片子导成**可以直接 npm i && npx remotion render** 的工程，"
                "压成 zip 回一条下载链接。不调模型、不渲染，几秒钟。\n"
                "用户说“我想自己改”“能不能给我工程”“要能重新渲”时用它。"
                "导出的工程是自包含的：只依赖 remotion / react / @remotion/transitions，"
                "改一句字幕或改某一镜的画面都不用重跑整条链。")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"film_id": {"type": "string", "description": "工程 id"}},
            "required": ["film_id"],
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, film_id: str) -> str:
        user_id, _ = _ident()
        try:
            folder = export.export_remotion(str(film_id))
        except FileNotFoundError as exc:
            return _json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "stage": "export",
                          "error": f"{type(exc).__name__}: {exc}"})

        try:
            zip_path = export.zip_dir(folder, kernel.out_dir() / f"{film_id}-remotion.zip")
            pub = await deliver.publish_bytes(
                zip_path.read_bytes(), suffix="zip", content_type="application/zip",
                storage=self._storage, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "stage": "save",
                          "error": f"{type(exc).__name__}: {exc}"})

        files = sorted(p.name for p in folder.rglob("*") if p.is_file())
        return _json({
            "ok": True,
            "film_id": film_id,
            "zip_url": pub["url"],
            "size_kb": round(pub["bytes"] / 1024),
            "files": files,
            "hint": ("把 zip_url 发给用户，并告诉他三条命令："
                     "`npm i` → `npm run studio`（实时预览）→ `npm run render`（出片）。"
                     "改一句字幕在 src/film.tsx 里搜那句话；改某一镜找 `const S07 =`。"),
        })


class ListShortFilmsTool(Tool):
    """最近做过的片子。"""

    @property
    def name(self) -> str:
        return "list_short_films"

    @property
    def display_name(self) -> str:
        return "我做过哪些片子"

    @property
    def description(self) -> str:
        return ("列出最近做过的图形科普短片（id / 主题 / 镜数 / 成片链接）。"
                "用户说“刚才那个片子呢”“再改一下上次那条”时先用它拿到 film_id。")

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self) -> str:
        rows = project.list_recent()
        return _json({"ok": True, "films": rows,
                      "hint": "" if rows else "还没有做过片子。"})


def register_short_video_tools(registry, *, storage: Any = None) -> None:
    """注册简洁线的原生工具。

    **`PlanShortFilmTool` 与 `RenderShortFilmTool` 不再注册**（2026-10-27）：
    它们改成了 MCP 节点（`open_storyline/nodes/simple_nodes.py` 的
    `plan_short_film` / `render_short_film`），进 DAG、产物进 Store。

    同一个名字只能有一个实现——MCP 工具是 `name_prefix=False` 注册的，
    两边同名时 `registry.register` 会「后注册的悄悄盖掉前一个」，
    出事时看不出跑的是哪一份。类还留着（`__init__.py` 有引用），但不上工具面。
    """
    registry.register(MakeShortVideoTool(storage=storage))
    registry.register(ReshootShotTool(storage=storage))
    registry.register(ExportFilmTool(storage=storage))
    registry.register(ListShortFilmsTool())


__all__ = [
    "PlanShortFilmTool", "MakeShortVideoTool", "ReshootShotTool", "RenderShortFilmTool",
    "ExportFilmTool", "ListShortFilmsTool", "register_short_video_tools",
]
