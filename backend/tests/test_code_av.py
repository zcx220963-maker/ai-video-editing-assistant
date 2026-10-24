# -*- coding: utf-8 -*-
"""闸⑰ 音画数据对账（R7-T23）：把 P20 那三条「没人查的事」判成数与字节。

P20 的原话是「声画只有形容词」——BGM 短于成片、`narration` 关着却有旁白轨，
两件事都没有一层会说话。这一道**判在拼接之前**（红了连一帧都不拼），判三件事：

  ① BGM 素材 ≥ 成片 × 0.98（`render._mux_bgm` 用的是 `-stream_loop -1`：短了不报错，
     是从头反复——所以「短」这件事今天在成片上只表现为「音乐怎么又来了」）；
  ② 旁白开着就得真有声、关着就得真静（**逐镜切片**的电平，不是主观印象）；
  ③ 接缝的字尾账：转场把两侧各 d 秒的尾巴交叠淡出，而语音就住在尾巴前面——
     d 不得大于两侧语音之后的余量（T22 的 `acrossfade` 那笔账归这一条）。

三条都是「量出来的数对不上」才红。量不到的（没有音轨 / volumedetect 没吐数）
按 D-未判不绿 **记提醒、不当通过**。

真 ffmpeg 那一节为什么必须留着：`audio_levels` 读的是**这条链同一个二进制**
解出来的电平，而「静音垫片」与「真说话」在**时长上一模一样**——只比时长的判据
在它们身上完全等价，这就是 ② 存在的理由，也是这一节要钉住的事实。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server import mediaops                              # noqa: E402
from storyline_server.mediaops import MediaError                   # noqa: E402
from storyline_server.motion import code_render as cr               # noqa: E402
from storyline_server.motion import code_spec as cs                 # noqa: E402
from test_code_gates import FakeService, _shot, _spec              # noqa: E402

_fails = 0
_checks = 0
WORK = Path("/tmp") / "code_av_work"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _av(**over):
    """一条「全绿」的账，用例只改要判的那一格。"""
    base = dict(narration=True, film_sec=10.0, clip_db=-20.0, clips_with_audio=2,
                bgm_sec=12.0, gaps=1, overlap_sec=0.4,
                voice_tails=[("s01", 0.5), ("s02", 0.5)], fps=30)
    base.update(over)
    return cs.check_av_accounting(**base)


def main() -> int:
    async def run() -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

        # =============================================================
        print("\n〔①〕BGM 够不够长：0.98 是一条线，不是一句感觉")
        errs, _ = _av(bgm_sec=9.8)
        check(errs == [], f"正好 0.98 倍放行（10s 的片子配 9.8s 的曲子）：{errs}")
        errs, warns = _av(bgm_sec=9.79)
        check(errs == [] and any("BGM 偏短" in w for w in warns),
              f"短了只提醒、不打回（用户指定的配乐不该被否掉）：{errs} {warns[:1]}")
        errs, warns = _av(bgm_sec=3.0)
        check(any("循环约 3.3 遍" in w for w in warns),
              f"3 秒的曲子配 10 秒的片子：把「会被循环几遍」算给看的人：{warns[:1]}")
        errs, warns = _av(bgm_sec=None)
        check(errs == [] and warns == [],
              f"没选 BGM → 这一档**不存在**（不是通过、也不是未判）：{errs} {warns}")

        # =============================================================
        print("\n〔②〕旁白在不在：两个方向都要判，缺一半就有半边永远没人看")
        errs, _ = _av(narration=False, clip_db=-91.0, clips_with_audio=2)
        check(errs == [], f"关着且真静（静音垫片 −91dB）→ 绿：{errs}")
        errs, _ = _av(narration=False, clip_db=-20.0, clips_with_audio=2)
        check(any("旁白轨没关干净" in e for e in errs),
              f"关着却响着 −20dB → 红（旧轨残留/静音垫片被换成真音）：{errs[:1]}")
        errs, warns = _av(narration=False, clip_db=None, clips_with_audio=0)
        check(errs == [] and warns == [],
              f"关着、也没有音轨 → 绿（0 镜带音轨是**确定的事实**，不是未判）：{warns}")
        errs, warns = _av(narration=False, clip_db=None, clips_with_audio=1)
        check(errs == [] and warns and "不判" in warns[0],
              f"带着音轨却量不出电平 → 未判（记提醒，不当通过）：{warns}")
        errs, _ = _av(narration=True, clips_with_audio=0, clip_db=None)
        check(any("没有一镜带音轨" in e for e in errs),
              f"开着旁白却一条音轨都没有 → 红：{errs[:1]}")
        errs, _ = _av(narration=True, clip_db=-91.0)
        check(any("静音垫片，不是旁白" in e for e in errs),
              f"开着旁白但最响只有 −91dB → 红（「加了静音轨」不等于「有配音」）：{errs[:1]}")
        errs, warns = _av(narration=True, clip_db=None, clips_with_audio=2)
        check(errs == [] and warns and "不判" in warns[0],
              f"开着、有音轨、量不到电平 → 未判：{warns}")
        errs, _ = _av(narration=True, clip_db=-26.0)
        check(errs == [], f"开着且真有声（−26dB）→ 绿：{errs}")

        # =============================================================
        print("\n〔③〕接缝的字尾账：转场那个交叠窗口吃不吃得下这条片子自己的字尾")
        errs, _ = _av(overlap_sec=0.4, voice_tails=[("s01", 0.5), ("s02", 0.9)])
        check(errs == [], f"d=0.4、两侧余量 0.5/0.9 → 绿：{errs}")
        errs, _ = _av(overlap_sec=0.9, voice_tails=[("s01", 0.5), ("s02", 0.9)])
        check(any("接缝 1（s01→s02）" in e and "会切到字" in e for e in errs),
              f"d=0.9 吃进 0.5s 的余量 → 红，且**点名是哪个接缝、两侧各剩多少**：{errs[:1]}")
        errs, _ = _av(overlap_sec=0.5 + cs.AV_TAIL_SLACK_SEC - 0.001,
                      voice_tails=[("s01", 0.5), ("s02", 0.5)])
        check(errs == [], f"差一帧以内（容差就是 1 帧）→ 放行，别让浮点挡好片：{errs}")
        errs, _ = _av(overlap_sec=0.5 + cs.AV_TAIL_SLACK_SEC + 0.01,
                      voice_tails=[("s01", 0.5), ("s02", 0.5)])
        check(any("会切到字" in e for e in errs), f"越过一帧 → 红：{errs[:1]}")
        errs, _ = _av(gaps=0, overlap_sec=0.0, voice_tails=[("s01", 0.05)])
        check(errs == [], f"单镜片：没有接缝，这一档无从判也不该红：{errs}")
        errs, _ = _av(gaps=2, overlap_sec=0.9,
                      voice_tails=[("s01", 0.5), ("s02", 2.0), ("s03", 0.5)])
        check(len([e for e in errs if "会切到字" in e]) == 2,
              f"两个接缝各判各的（s02 很长也不豁免两头）：{len(errs)} 条")

        # =============================================================
        print("\n〔真 ffmpeg〕这条判据读的是字节：静音与真话在**时长上完全一样**")
        sil = mediaops.silent_wav(WORK / "sil.wav", duration=2.0)
        tone = mediaops.tone_wav(WORK / "tone.wav", duration=2.0, freq=220)
        ds, dt = mediaops.probe(sil)["duration"], mediaops.probe(tone)["duration"]
        check(abs(ds - dt) <= 0.05,
              f"两支音轨时长几乎相等（{ds}s vs {dt}s）——只比时长的判据在这里等价，"
              "这就是闸⑰ 要量电平的理由")
        lv_s, lv_t = mediaops.audio_levels(sil), mediaops.audio_levels(tone)
        check(lv_s["max_db"] <= cs.AV_SILENT_DB < lv_t["max_db"],
              f"电平能把两者分开：静音 {lv_s['max_db']:.1f}dB ≤ 静音线 "
              f"{cs.AV_SILENT_DB:.0f}dB < 正弦 {lv_t['max_db']:.1f}dB")
        check(mediaops.probe(sil)["has_audio"] and mediaops.probe(tone)["has_audio"],
              "两支都带音轨（所以 ② 的分岔读的是 has_audio，不是文件在不在）")
        # 量不到就抛，绝不返回 0 冒充安静——那会让这条判据恰好在它该响的时候闭嘴。
        broke = WORK / "noaudio.mp4"
        mediaops.ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.2",
                        "-c:v", "libx264", "-y", str(broke))
        try:
            got = mediaops.audio_levels(broke)
            check(False, f"没有音频流时必须抛，实得 {got}——返回 0 就是把「没量到」"
                         "写成「很安静」，这条判据会恰好在它该响的时候闭嘴")
        except MediaError as exc:
            # ffmpeg 在「要求 -af 而文件没有音频流」时自己就退非零，这一条读的是它原样抛上来
            # 的那句话（不吞、不兜底成 0）——措辞归 ffmpeg，我们只钉「抛了」这件事。
            check("rc=" in str(exc) or "volumedetect" in str(exc),
                  f"没有音频流时当场抛、不兜底：{str(exc)[:80]}")

        # =============================================================
        print("\n〔整链〕闸⑰ 判在拼接之前：红了连母带都不出现")
        # 绿的那一趟：narration=False、无 BGM（夹具的切片没有音轨——那是确定的事实）。
        svc = FakeService(mode="ok")
        spec = _spec([_shot(1), _shot(2, text="换个说法")])
        out = await cr.render_code_motion(spec, providers=None, work=WORK / "ok",
                                          client=svc)
        av = out["av_accounting"]
        # ★ 这里量到的事实值得记住：旁白关着时这条链**照样**给每一镜垫一条**静音**轨
        #   （`narration_for` → `silent_wav`），所以 `clips_with_audio=2`、电平是 −91dB。
        #   「带音轨」与「有旁白」因此是两件事——只判音轨在不在的写法在这里会误报，
        #   这就是 ② 判**电平**而不是判流存在性的理由（真机同一个数）。
        check(av["narration"] is False and av["clips_with_audio"] == 2
              and av["clip_max_db"] is not None and av["clip_max_db"] <= cs.AV_SILENT_DB,
              f"回执把这一档的读数摊开：2 镜带**静音垫片**、最响 {av['clip_max_db']}dB "
              f"≤ 静音线——「带音轨」不等于「有旁白」，读的是电平：{av['clip_max_db']}")
        check(av["bgm_sec"] is None and abs(av["film_sec_planned"]
                                            - sum(r["sec"] for r in out["shots"])) < 1e-6,
              f"成片长度按账算（无转场时＝逐镜之和）：{av['film_sec_planned']}")
        check(all(t >= 0 for t in av["voice_tails"].values()),
              f"逐镜语音尾余量也摊在回执上（③ 读的就是它）：{av['voice_tails']}")
        check((WORK / "ok" / "final.mp4").exists(), "绿的那一趟照样出片")

        # 〔报告层〕判在执行层、报在回执上——这一节判的是**回执那行**有没有如实摊开。
        # 报告层错一次的形状是「账明明量到了，回执却写成另一回事」或者「回执照抄了一份
        # 自己算的数」；两种都不在渲染链上红（`test_code_gates` 的〔报告层〕是同一个道理）。
        from storyline_server.nodes.motion_code_nodes import RenderCodeMotionVideoNode
        from storyline_server.settings import Settings
        from storyline_server.providers import Providers

        node = RenderCodeMotionVideoNode(Settings(), Providers(), None)  # type: ignore[arg-type]
        rep = node._result(spec, out, target=None, low_high=(0.0, 0.0),
                           warnings=[], bgm_ref="", object_key="k/offline.mp4",
                           frames=None, frame_reason="离线用例不抽帧")
        line = [e for e in rep["evidence"] if "闸⑰ 音画数据对账" in e["claim"]]
        check(bool(line), "回执里有「闸⑰ 音画数据对账」这一行")
        ev = line[0] if line else {"claim": "", "status": "", "proof": ""}
        # 读数必须与渲染层带回来的是**同一个数**（回执不重算——重算就会出现两把尺）。
        check("没有 BGM" in ev["claim"]
              and "旁白**关着**" in ev["claim"]
              and f"{av['clip_max_db']} dB" in ev["claim"],
              f"这一行把 BGM 档与旁白档（含最响电平）摊开，且数与回执同源：{ev['claim']}")
        check(ev["status"] == "verified",
              f"无 BGM、旁白关着且真静 → 这一行按 verified 交出（status={ev['status']}）")
        check("静音垫片也算带音轨" in ev["proof"] or "带音轨" in ev["claim"],
              "「带音轨 ≠ 有旁白」这句判据的分岔写在给看的人的地方，不是只活在注释里")

        # 红的那一趟：1 秒 BGM 配这条片子。判据的输入之一（bgm_sec）来自**真文件**。
        svc = FakeService(mode="ok")
        bgm = mediaops.tone_wav(WORK / "short_bgm.wav", duration=1.0, freq=110)
        raised = None
        try:
            await cr.render_code_motion(spec, providers=None, work=WORK / "red",
                                        client=svc, bgm=bgm)
        except MediaError as exc:
            raised = exc
        check(raised is None or "BGM" not in str(raised),
              f"1 秒 BGM 不再让闸⑰ 打回整片（循环混音，交付时提醒）：{str(raised)[:110]}")

        # =============================================================
        print("\n〔单源〕常量与量法只有一处")
        src = Path(cr.__file__).read_text(encoding="utf-8")
        check(src.count("AV_SILENT_DB") <= 1 and src.count("AV_BGM_MIN_SHARE") <= 1,
              "渲染层只**读**契约那两个常量，不另抄一份数（抄了就会出现两把尺）")
        check("stream_loop" in Path(cr.rpipe.__file__).read_text(encoding="utf-8"),
              "「短 BGM 会被循环」是混音那趟自己写的选项——① 判的正是它带来的那件事")
        check("volumedetect" in Path(mediaops.__file__).read_text(encoding="utf-8"),
              "电平用这条链同一个 ffmpeg 量，不引第二个解码器")
        assert "闸⑰" in Path(cs.__file__).read_text(encoding="utf-8")
        print("\n闸⑰ 的措辞与读数只此一处（`check_av_accounting`）")

    asyncio.run(run())
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
