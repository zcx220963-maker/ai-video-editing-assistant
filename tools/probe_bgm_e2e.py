# -*- coding: utf-8 -*-
"""真机验证：曲库配乐**真的进到音轨里**了没有。

为什么不用"看代码对不对"来判断：这条线曾经整段是死的——`voice.pick_bgm()` 只读
`SIMPLE_BGM_DIR`，容器里没那个目录，于是"配了曲库"在成片里等于没配，而所有测试全绿。
所以这里用**电平**当判据：

    一条纯静音旁白轨（anullsrc）→ 混配乐 → 量 mean_volume
    静音是 −91 dB；混进音乐之后必然跳到 −30 dB 上下。
    数字不动 = 音乐没进去。

跑法（容器里）：
    python /app/tools/probe_bgm_e2e.py <user_id> [主题]
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app").is_dir() else str(Path(__file__).resolve().parents[1]))

from agent_framework import bgm  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402


def run(args: list[str]) -> str:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    p = subprocess.run([exe, "-y", "-v", "info", *args], capture_output=True, text=True)
    return (p.stderr or "") + (p.stdout or "")


def mean_volume(path: Path) -> float | None:
    """整条音轨的平均电平（dB）。静音是 −91 左右。"""
    out = run(["-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    for line in out.splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                return None
    return None


async def main() -> int:
    uid = sys.argv[1] if len(sys.argv) > 1 else "u-6d962435c873"
    theme = sys.argv[2] if len(sys.argv) > 2 else "三体 宇宙 信号 孤寂"
    total = 12.0

    st = build_storage("pg_minio")
    await st.start()
    try:
        work = Path(tempfile.mkdtemp(prefix="bgmprobe-"))
        print(f"用户 {uid}　主题「{theme}」　片长 {total:g}s")
        print("-" * 60)

        got = await bgm.pick(st, uid, theme, work_dir=work, need_sec=total)
        if got is None:
            print("XX 曲库里取不到任何一首 —— 配乐不会进片")
            return 1
        print(f"选中：{got['filename']}")
        print(f"      来源 {got['origin']}　时长 {got['duration']:g}s　"
              f"本地 {got['path']}（{got['path'].stat().st_size // 1024} KB）")

        # 一条纯静音旁白轨，长度 = 片长（与 voice.build_track 出来的东西同构）
        silent = work / "silent.m4a"
        run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{total:g}", "-c:a", "aac", "-b:a", "160k", str(silent)])
        before = mean_volume(silent)
        print(f"\n混音前（纯静音旁白）：mean_volume = {before} dB")

        out = work / "mixed.m4a"
        track, tune = await bgm.mix_into(
            silent, out, storage=st, user_id=uid, prompt=theme,
            total_sec=total, work_dir=work)
        after = mean_volume(track)
        print(f"混音后：mean_volume = {after} dB　（{track}）")
        print(f"配乐说明：{(tune or {}).get('filename') or '（没加）'}")

        dur = subprocess.run(
            [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(track)],
            capture_output=True, text=True).stdout.strip()
        print(f"混音后时长：{dur} s（应等于 {total:g}）")
        print("-" * 60)
        if after is None or before is None:
            print("XX 量不出电平")
            return 1
        if after - before < 20:
            print(f"XX 电平只涨了 {after - before:.1f} dB —— 配乐没真的混进去")
            return 1
        print(f"OK  电平涨了 {after - before:.1f} dB —— 配乐确实进了音轨")
        return 0
    finally:
        await st.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
