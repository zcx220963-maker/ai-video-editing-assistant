# -*- coding: utf-8 -*-
"""短视频三件套：一句话出片 / 只改一镜 / 重渲整片。

这是简洁视频线**唯一的对外入口**——在这之前它只有 `/studio` 一个网页，
用户在对话里说"帮我做个讲瑞利散射的片子"时，Agent 手上没有任何工具能接住这句话。

为什么是"阻塞式"而不是"提交作业再轮询"：
主链的 `plan_motion` 本来就是一次 56 分钟的工具调用（`_TOOL_CALL_TIMEOUTS` 给它 2700 秒）。
在这套产品里，"长任务 = 一次长工具调用"是既有约定；再引入一套作业轮询，
就多了一条"任务跑到一半 Agent 轮次结束了"的失败路径。

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
from storyline_server.simple import deliver, edit, export, facts, film, kernel, project

#: 秒数上下限。上限由**结构**决定，不是拍脑袋：
#:   动效：镜数 ≤ `film.MAX_SHOTS`(56) × `film.MAX_SHOT_SEC`(5.2) = 291 秒
#:   叙事：镜数 ≤ `film.STORY_MAX_SHOTS`(24) × `film.STORY_MAX_SHOT_SEC`(12) = 288 秒
#: 240 秒（4 分钟）是给"三分钟知识大赏那种片子"留的量，同时把渲染时间压在
#: 一次工具调用（3600 秒）以内——240 秒 ≈ 3600 帧，渲染约 35 分钟。
MIN_SEC, MAX_SEC = 8.0, 240.0


def _clamp_seconds(value: Any, default: float = 30.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return max(MIN_SEC, min(MAX_SEC, v))


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
                            "description": f"时长秒数，{MIN_SEC:.0f}~{MAX_SEC:.0f}，默认 30"},
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

    async def execute(self, prompt: str, seconds: Any = 30.0,
                      aspect: str = "portrait", render: bool = True,
                      voice: bool = True, style: str = "auto",
                      bgm: str = "", scenes: Any = None) -> str:
        text = str(prompt or "").strip()
        if len(text) < 4:
            return "Error: prompt 太短，至少给一个完整主题（例如“为什么天空是蓝色的”）"
        sec = _clamp_seconds(seconds)
        asp = kernel.norm_aspect(aspect)
        want = str(style or "").strip().lower()
        mode = want if want in (film.MODE_STORY, film.MODE_EXPLAINER) else "auto"
        user_id, conv_id = _ident()

        await kernel.ensure_storage()
        try:
            got = await film.make_film(text, seconds=sec, aspect=asp,
                                       user_id=user_id, conversation_id=conv_id,
                                       voice=bool(voice), mode=mode,
                                       scenes=scenes or None)
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
            "audit": {
                "ratio": round(float((got.get("audit") or {}).get("ratio") or 0), 3),
                "bad": (got.get("audit") or {}).get("bad") or [],
            },
            "fallback_shots": got.get("fallback") or [],
            "mp4_url": "",
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
            out["hint"] = ("已写好代码但**没有渲成片**。要看成片请调 render_short_film"
                           "（传上面的 film_id）；要改某一镜用 reshoot_shot。")
            return _json(out)

        # 渲画面 → 合成音轨 → 合流 → 上传。音轨失败**不整条失败**：无声的片子仍是片子。
        final = path = None
        try:
            path, res = await kernel.render(got["code"], seconds=got["total_sec"],
                                            aspect=asp)
        except kernel.KernelError as exc:
            out.update(ok=False, stage=exc.stage, error=str(exc))
            return _json(out)
        except Exception as exc:  # noqa: BLE001
            out.update(ok=False, stage="render", error=f"{type(exc).__name__}: {exc}")
            return _json(out)

        final = path
        audio_name = ""
        vinfo = got.get("voice") or {}
        if vinfo.get("degraded"):
            out["voice"] = "none"
            out["voice_error"] = vinfo.get("error") or "TTS 全部失败"
        elif vinfo:
            try:
                from storyline_server.simple import voice as vo
                from .. import bgm as bgm_pick
                track = vo.build_track(
                    [Path(p) if p else None for p in vinfo.get("files") or []],
                    got["secs"], kernel.out_dir() / f"track-{fid}.m4a")
                # 配乐**只在点名时才加**（2026-10-26 改）：不填 `bgm` 就静音出片。
                track, tune = await bgm_pick.mix_into(
                    track, kernel.out_dir() / f"track-{fid}-bgm.m4a",
                    storage=self._storage, user_id=user_id, prompt=text,
                    total_sec=got["total_sec"], work_dir=kernel.out_dir() / "bgm",
                    bgm_name=bgm)
                out["bgm"] = (tune or {}).get("filename") or ""
                # 音轨留在工程里：重渲时画面重做、音轨照用（旁白与镜长是一起定的）
                audio_name = "audio.m4a"
                shutil.copyfile(track, project.dir_of(fid) / audio_name)
                final = vo.mux(path, track, kernel.out_dir() / f"{fid}-av.mp4")
                out["voice"] = "ok"
                out["voice_failed_shots"] = vinfo.get("failed") or []
                out["voice_over_shots"] = vinfo.get("over") or []
            except Exception as exc:  # noqa: BLE001 —— 有声是加分项，不是前置条件
                out["voice"] = "failed"
                out["voice_error"] = f"{type(exc).__name__}: {exc}"
                final = path
                audio_name = ""

        try:
            pub = await deliver.publish(final, storage=self._storage, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            out.update(ok=False, stage="save", error=f"{type(exc).__name__}: {exc}")
            return _json(out)

        project.update(fid, mp4_url=pub["url"], object_key=pub["object_key"],
                       audio=audio_name)
        out["mp4_url"] = pub["url"]
        out["size_mb"] = round(pub["bytes"] / 1048576, 1)
        out["render_ms"] = (kernel.describe_receipt(res).get("ms") or {}).get("render_ms")
        out["hint"] = ("把 mp4_url 直接发给用户（能点开就播）。用户要改某一镜就用 "
                       "reshoot_shot(film_id, shot, note)，**不要重新做一条**。"
                       "报告里带上 shots / audit.ratio / bgm；"
                       "**如果 numbers_without_source 非空，必须主动告诉用户哪几镜的"
                       "数字没查出出处**——别让用户以为都核实过了。")
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
                            "description": f"时长秒数，{MIN_SEC:.0f}~{MAX_SEC:.0f}，默认 30"},
                "aspect": {"type": "string", "enum": ["portrait", "landscape", "square"],
                           "description": "画幅，默认 portrait（竖屏）"},
            },
            "required": ["prompt"],
        }

    async def execute(self, prompt: str, seconds: Any = 30.0,
                      aspect: str = "portrait") -> str:
        text = str(prompt or "").strip()
        if len(text) < 2:
            return "Error: prompt 太短，至少给一个话题（例如「0 的历史」）"
        sec = _clamp_seconds(seconds)
        asp = kernel.norm_aspect(aspect)
        user_id, conv_id = _ident()
        await kernel.ensure_storage()
        mode = await film.pick_style(text, seconds=sec, user_id=user_id,
                                     conversation_id=conv_id)
        try:
            if mode == film.MODE_STORY:
                got, calls = await film.plan_story(
                    text, seconds=sec, aspect=asp, user_id=user_id,
                    conversation_id=conv_id)
            else:
                got, calls = await film.plan(
                    text, seconds=sec, aspect=asp, user_id=user_id,
                    conversation_id=conv_id)
        except kernel.KernelError as exc:
            return _json({"ok": False, "stage": exc.stage, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 —— 回喂给模型，别让整轮崩掉
            return _json({"ok": False, "stage": "plan",
                          "error": f"{type(exc).__name__}: {exc}"})
        return _json({
            "ok": True, "style": mode, "calls": calls,
            "shots": len(got.get("scenes") or []),
            "plan": got,
            "next": ("把 plan 原样传给 make_short_video 的 scenes 出片；"
                     "用户要改就直接改 plan.scenes[i].text 再传"),
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
            proj = project.load(str(film_id))
        except FileNotFoundError as exc:
            return _json({"ok": False, "error": str(exc)})

        code = (project.dir_of(film_id) / "film.tsx")
        if not code.exists():
            return _json({"ok": False, "error": "工程里没有 film.tsx，无法重渲"})
        src = code.read_text(encoding="utf-8")

        # 渲多长要按**这条片子实际的每镜秒数**算，不是按用户当初要的 `seconds`：
        # 有旁白时镜长由语音决定，`seconds` 只是目标（实测要 180 秒、出来 123 秒）。
        # 用错这个数，重渲出来会被截尾或补黑。
        secs = [float(s) for s in proj.get("secs") or []]
        total = round(sum(secs), 2) if secs else float(proj.get("seconds") or 30)
        try:
            path, res = await kernel.render(
                src, seconds=total,
                aspect=kernel.norm_aspect(proj.get("aspect")))
        except kernel.KernelError as exc:
            return _json({"ok": False, "stage": exc.stage, "error": str(exc),
                          "lint_errors": exc.lint_errors[:5]})
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "stage": "render",
                          "error": f"{type(exc).__name__}: {exc}"})

        # 音轨照用：改一镜只该改画面，声音不该重生成一遍（旁白与镜长是一起定的）。
        final, voice_state = path, "none"
        audio = str(proj.get("audio") or "")
        if audio:
            track = project.dir_of(film_id) / audio
            if track.exists():
                try:
                    from storyline_server.simple import voice as vo
                    final = vo.mux(path, track, kernel.out_dir() / f"{film_id}-av.mp4")
                    voice_state = "ok"
                except Exception as exc:  # noqa: BLE001
                    voice_state = f"failed: {type(exc).__name__}: {exc}"
                    final = path

        try:
            pub = await deliver.publish(final, storage=self._storage, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "stage": "save",
                          "error": f"{type(exc).__name__}: {exc}"})

        project.update(film_id, mp4_url=pub["url"], object_key=pub["object_key"])
        return _json({
            "ok": True,
            "film_id": film_id,
            "mp4_url": pub["url"],
            "size_mb": round(pub["bytes"] / 1048576, 1),
            "voice": voice_state,
            "render_ms": (kernel.describe_receipt(res).get("ms") or {}).get("render_ms"),
            "hint": "把 mp4_url 直接发给用户。",
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
    registry.register(PlanShortFilmTool(storage=storage))
    registry.register(MakeShortVideoTool(storage=storage))
    registry.register(ReshootShotTool(storage=storage))
    registry.register(RenderShortFilmTool(storage=storage))
    registry.register(ExportFilmTool(storage=storage))
    registry.register(ListShortFilmsTool())


__all__ = [
    "PlanShortFilmTool", "MakeShortVideoTool", "ReshootShotTool", "RenderShortFilmTool",
    "ExportFilmTool", "ListShortFilmsTool", "register_short_video_tools",
]
