# -*- coding: utf-8 -*-
"""跑一条 60 秒叙事片，验证「内容厚 / 笔触薄」这版提示词：画法散不散、版式有没有真画。

判据不是"好不好看"（那个主观），是三条可量的：
  ① 画法多样性（每镜 SVG 元素构成去重后剩几种）
  ② 每镜是否真的画了版式文字（右上出处 / 左下注脚 / 对照表）
  ③ 质检合格率与亮度饱和度

跑法（容器里，后台）：
    nohup python /app/tools/run_dunhuang.py > /tmp/dh.log 2>&1 &
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app").is_dir() else str(Path(__file__).resolve().parents[1]))

BASE = "http://localhost:8000"

PROMPT = """主题：敦煌藏经洞里那 5 万卷文书，被谁封起来的、又为什么散到了十个国家——一个至今没有定论的悬案。

讲法（五段，按顺序推进，不要跳）：

1. 发现。1900 年 6 月 22 日，道士王圆箓清理莫高窟第 16 窟甬道的积沙，北壁裂开一道缝，里面是一间三米见方的密室，塞满写本。他往里看了一眼——不是金银，是纸。
   要落的点：这个人是逃荒到甘肃的湖北人，当了道士，管着这片没人管的洞窟。他不是学者。

2. 尺度。这些卷子从公元 405 年一直写到 1002 年，近六百年不断代：汉文、藏文、回鹘文、粟特文、于阗文、梵文，甚至一份希伯来文祷文。一个洞，装下了整条丝绸之路的七种语言。
   要落的点：把「七种文字」排成一行并列展示，让观众看见这不是一种文明的档案，是很多种。

3. 悬案。这些卷子本该被读，却被从外面砌死了，墙面重新抹平、画上壁画。为什么封？学界吵了一百多年：避西夏兵祸？还是废弃经卷的「瘗埋」？没有定论。
   要落的点：这里要停一下——不是所有问题都有答案，这一段的语气是「至今没人知道」。

4. 散失。1907 年斯坦因用四十块马蹄银换走二十四箱；1908 年伯希和挑走最精的六千卷——他懂汉学，知道哪卷最值钱。1910 年清廷下令运京，途中又散失一批。今天它们散在英国、法国、俄国、日本、美国等十余国，中国国家图书馆只剩一万六千件。
   要落的点：这一段的语气是克制，不要煽情。「他懂汉学，知道哪卷最值钱」这一句比任何形容词都狠。

5. 反转与收束。散落反而救下了一部分——留在原地的，在之后的战乱与流散里损失得更多。那个封洞的人，大概只是想把这些东西藏起来，不是想毁掉。他做到了九百年，只是没能做到更久。
   要落的点：最后一句要慢，留白。

信息层要求（这些必须成块出现，不要混在字幕里）：
· 第 2 段：七种文字并列一行；
· 第 4 段：一张「散落各国」对照表（地名 + 件数），用虚线连回敦煌；
· 全片：右上角小字出处，左下角注脚。像纪录片的信息层，贴边、字号小。

字幕纪律：这条片子里有几镜的画面本来就有适合承载文字的东西（洞窟墙壁、经卷、书页、纸）。
这些镜可以让字幕成为画面的一部分（`caption.mode = "drawn"`，你自己把这句话画进去）；
其余镜用统一字幕层。全片 `drawn` 不要超过 1/3 的镜。

画法只守三条铁律（其余每镜你自己定，不要每镜都长一样）：
① 全片只有一个主光源，光来自画面里存在的那个东西（油灯、月亮、洞口的日光）；
② 主体用剪影，边缘有轮廓光；整体暗调克制，颜色只用来点光；
③ 全片是同一个世界：同一套夜空/沙土质感，不要一镜写实一镜扁平。

数字出处（写进注脚）：五万余件与 405—1002 年断代据《敦煌遗书总目索引》；二十四箱与四十块马蹄银据斯坦因《西域考古记》；国家图书馆一万六千件据中国国家图书馆藏敦煌文献目录。哪个数字你没把握，就删掉改成定性描述，不要编。"""


def call(path: str, tok: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"authorization": f"Bearer {tok}", "content-type": "application/json"},
        method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def sh(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def probe(path: Path) -> dict:
    out: dict = {}
    d = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path)])
    out["duration"] = round(float(d), 2) if d else 0.0
    out["audio"] = sh(["ffprobe", "-v", "error", "-select_streams", "a",
                       "-show_entries", "stream=codec_name",
                       "-of", "default=nw=1:nk=1", str(path)]) or "(none)"
    vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    for line in vol.splitlines():
        if "mean_volume:" in line:
            out["mean_db"] = float(line.split("mean_volume:")[1].split("dB")[0])
    sig = sh(["ffprobe", "-v", "error", "-f", "lavfi", "-i",
              f"movie={path},signalstats", "-show_entries",
              "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.SATAVG",
              "-of", "csv=p=0"])
    ys, ss = [], []
    for row in sig.splitlines():
        p = row.split(",")
        if len(p) >= 2:
            try:
                ys.append(float(p[0]))
                ss.append(float(p[1]))
            except ValueError:
                continue
    if ys:
        ys.sort(); ss.sort()
        out["yavg_p50"] = round(ys[len(ys) // 2], 1)
        out["satavg_p50"] = round(ss[len(ss) // 2], 1)
    return out


async def main() -> int:
    uid = "u-6d962435c873"
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

    from agent_framework.auth import Authenticator
    from agent_framework.storage import build_storage

    st = build_storage("pg_minio")
    await st.start()
    tok = await Authenticator(st).issue_jwt(uid)

    sub = call("/simple/video", tok, {
        "prompt": PROMPT, "seconds": seconds, "aspect": "landscape",
        "mode": "story", "voice": True})
    jid = sub["job_id"]
    print(f"作业 {jid}　{seconds:g}s　mode={sub.get('mode')} style={sub.get('style')}", flush=True)

    t0 = time.time()
    last, view = "", {}
    while time.time() - t0 < 3600:
        await asyncio.sleep(15)
        try:
            view = call(f"/simple/job/{jid}", tok)
        except Exception as exc:  # noqa: BLE001
            print(f"  轮询失败：{exc}", flush=True)
            continue
        p = view.get("progress") or {}
        line = f"[{int(time.time() - t0):4d}s] {view.get('status')}/{view.get('stage')}"
        if p:
            line += f" {p.get('stage')} {p.get('done')}/{p.get('total')}"
        if line != last:
            print(line, flush=True)
            last = line
        if view.get("status") in ("done", "failed"):
            break

    print("-" * 60, flush=True)
    print(json.dumps({k: view.get(k) for k in
                      ("status", "error", "shots", "calls", "voice", "bgm",
                       "sources_note", "style")}, ensure_ascii=False, indent=1), flush=True)
    cov = view.get("coverage") or {}
    print(f"coverage {cov.get('ratio')}　per_shot {cov.get('per_shot')}", flush=True)
    print(f"sources {json.dumps(view.get('sources') or [], ensure_ascii=False)[:300]}", flush=True)

    dst = Path(f"/app/.runtime/simple/{jid}-av.mp4")
    if view.get("status") != "done" or not dst.exists():
        print(f"XX 没有成片：{dst}", flush=True)
        await st.close()
        return 1
    print(f"成片 {dst}（{dst.stat().st_size / 1048576:.1f} MB）", flush=True)
    print(json.dumps(probe(dst), ensure_ascii=False), flush=True)

    # 抽 6 帧，等比覆盖全片
    fr = Path("/tmp/dh_frames")
    fr.mkdir(exist_ok=True)
    for f in fr.glob("*.jpg"):
        f.unlink()
    dur = probe(dst)["duration"] or seconds
    for i in range(6):
        t = dur * (i + 0.5) / 6
        sh(["ffmpeg", "-v", "error", "-ss", f"{t:.1f}", "-i", str(dst),
            "-frames:v", "1", "-vf", "scale=960:-1", str(fr / f"f{i + 1}.jpg")])
    print(f"抽帧 {len(list(fr.glob('*.jpg')))} 张 → {fr}", flush=True)
    await st.close()
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
