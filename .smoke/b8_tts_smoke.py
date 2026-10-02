# -*- coding: utf-8 -*-
"""edge-tts 真机验证：真网络合成 + 真 ffprobe 量时长 + 真节点不再落 silent_fallback。

离线套件里 edge-tts 一直是注入替身或「必然失败」的降级路径，所以这条链从没被证明过。
本冒烟只做三件事，全部打真网络：
  ① provider 层：真 voice 合成出可播放 mp3（字节 > 512、ffprobe 时长 > 0.3s）；
  ② 节点层：真 build_providers 跑 generate_voiceover，每条都标 source="edge-tts"，
     并且 payload 真进了 Store（下游 plan_timeline 读得到）；
  ③ 反向对照：换一个必然不存在的 voice，节点必须落 silent_fallback 且**不抛穿**——
     没有这条对照，②的 "edge-tts" 标签说明不了什么（可能只是没判降级）。
不需要容器：TTS 不碰 PG/MinIO，Store 用测试替身（真引擎侧由 b6/b7 覆盖）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import ArtifactStore, NodeState
from agent_framework.storage import build_storage
from storyline_server import mediaops
from storyline_server.nodes.core_nodes import build_real_registry
from storyline_server.providers import ProviderError, build_providers
from storyline_server.settings import Settings

FAILS = 0
USER, CONV = "u_tts_smoke", "c_tts_smoke"
LINE = "各位同学，今天这段视频我们讲三件事：怎么找素材、怎么留住原声、怎么配上合适的背景音乐。"


def check(cond: bool, label: str) -> bool:
    global FAILS
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILS += 1
    return cond


def voiceover(packed: dict) -> list[dict]:
    """节点返回的是打包视图 {node, artifact_id, output}——取包里的配音列表。"""
    return ((packed or {}).get("output") or {}).get("voiceover") or []


async def case_provider(tmp: Path) -> None:
    print("\n[① provider 层：真 edge-tts 出可播音频]")
    prov = build_providers(Settings().caps)
    dst = tmp / "provider.mp3"
    try:
        out = await prov.tts(LINE, dst)
    except ProviderError as e:
        check(False, f"edge-tts 真合成没报错（实际：{e}）")
        return
    size = Path(out).stat().st_size if Path(out).exists() else 0
    check(size > 512, f"真合成出非空字节：{size} B（不是占位空文件）")
    try:
        info = await asyncio.to_thread(mediaops.probe, Path(out))
        dur = float(info["duration"])
    except Exception as e:  # noqa: BLE001 - 判据本身
        check(False, f"ffprobe 读得动这个 mp3（实际：{e}）")
        return
    check(dur > 0.3, f"ffprobe 量出真实时长 {dur:.2f}s（静默占位不会有）")
    check(Path(out).suffix == ".mp3", "产物是 mp3（edge-tts 的输出格式，不是降级用的 wav）")
    check(float(info.get("duration") or 0) < 60.0, "时长在合理区间（没被截断也没无限延长）")


async def _voiceover_state(storage, artifact: str) -> NodeState:
    store = await ArtifactStore.open(storage.artifacts(f"{USER}:{CONV}", artifact),
                                     f"{USER}:{CONV}", artifact)
    state = NodeState(session_id=f"{USER}:{CONV}", artifact_id=artifact,
                      user_request="给这段素材配音", store=store,
                      user_id=USER, conversation_id=CONV)
    await store.put("generate_script", {"group_scripts": [
        {"group_id": "g1", "raw_text": LINE},
        {"group_id": "g2", "raw_text": "第二段口播，稍微短一点，用来验证多条都能合成。"},
    ], "source": "llm"})
    return state


async def case_node(tmp: Path) -> None:
    print("\n[② 节点层：generate_voiceover 走真 edge-tts]")
    storage = build_storage("memory", workspace_root=tmp / "ws")
    await storage.start()
    settings = Settings()
    reg = build_real_registry(settings, build_providers(settings.caps), storage)
    node = reg.get("generate_voiceover")
    check(node is not None, "注册表里有 generate_voiceover")
    if node is None:
        return
    state = await _voiceover_state(storage, "a_tts_real")
    out = await node(state)
    rows = voiceover(out)
    check(len(rows) == 2, f"两段口播都产出音频（实得 {len(rows)}）")
    srcs = [r.get("source") for r in rows]
    check(srcs == ["edge-tts", "edge-tts"],
          f"每条都标 edge-tts、没有一条落降级（实得 {srcs}）")
    durs = [float(r.get("duration") or 0) for r in rows]
    check(bool(durs) and all(d > 0.3 for d in durs), f"节点自报时长与真音频相符：{durs}")
    check(bool(rows) and all(
        Path(r["path"]).exists() and Path(r["path"]).stat().st_size > 512 for r in rows),
        "音频文件真落在工作区且非空")
    stored = await state.store.get("generate_voiceover")
    got = [x["source"] for x in (stored or {}).get("voiceover") or []]
    check(got == srcs and bool(got),
          f"payload 已进 Store（下游 plan_timeline 读得到同一份，实得 {got}）")
    await storage.close()


async def case_degrade_contrast(tmp: Path) -> None:
    print("\n[③ 反向对照：真失败时必须落 silent_fallback 而不是抛穿]")
    storage = build_storage("memory", workspace_root=tmp / "ws_fb")
    await storage.start()
    settings = Settings()
    dead_caps = replace(settings.caps, tts_voice="zh-CN-ThisVoiceDoesNotExist-Audio")
    reg = build_real_registry(settings, build_providers(dead_caps), storage)
    node = reg.get("generate_voiceover")
    state = await _voiceover_state(storage, "a_tts_fallback")
    try:
        out = await node(state)
    except Exception as e:  # noqa: BLE001
        check(False, f"TTS 真失败不抛穿整条链（实际抛了 {type(e).__name__}: {e}）")
        await storage.close()
        return
    rows = voiceover(out)
    srcs = [r.get("source") for r in rows]
    check(bool(rows) and all(s == "silent_fallback" for s in srcs),
          f"坏 voice 下如实标 silent_fallback（实得 {srcs}）——"
          "所以 ② 的 edge-tts 标签是被判过而不是默认值")
    await storage.close()


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tts_smoke_") as raw:
        tmp = Path(raw)
        await case_provider(tmp)
        await case_node(tmp)
        await case_degrade_contrast(tmp)
    print("\n" + ("全部通过" if FAILS == 0 else f"{FAILS} 项未通过"))
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
