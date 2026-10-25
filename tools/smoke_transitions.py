"""转场冒烟：拼一条 3 镜 9 秒的片子，**不调模型**，只验 TransitionSeries 真能渲。

    docker cp tools/smoke_transitions.py creation-app:/app/tools_smoke_transitions.py
    docker exec creation-app python /app/tools_smoke_transitions.py

为什么单列这一条：`lint` 只查语法与白名单，**查不出**「presentation 参数传错」
「TransitionSeries 的 Sequence 没包 AbsoluteFill」这类运行时问题——
它们要等到渲染才炸，而那时候一条 60 秒片子已经烧了 28 次调用。
"""
import asyncio
import subprocess
import sys

sys.path.insert(0, "/app")

from storyline_server.motion import remotion_client as rc
from storyline_server.simple import film, kernel

OUT = "/app/.runtime/simple/trans-test.mp4"
SECONDS = 9.0


def scene(hue: str, title: str, kind: str) -> str:
    """三种不同构图的场景，各自只用设计系统的组件。"""
    if kind == "chart":
        body = '<BarChart values={[0.35, 0.62, 0.9]} labels={["甲", "乙", "丙"]} t={t} hue={HUE} y={0.55} />'
    elif kind == "orbit":
        body = '<OrbitDiagram count={6} t={t} hue={HUE} y={0.42} r={0.24} />'
    else:
        body = '<GlowOrb t={t} hue={HUE} y={0.40} r={0.20} />'
    return f'''const Scene = ({{ dur }}: {{ dur: number }}) => {{
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  const HUE = '{hue}';
  return (
    <AbsoluteFill style={{{{ backgroundColor: '#0a0c12' }}}}>
      <Backdrop hue={{HUE}} t={{t}} />
      <Ribbon hue={{HUE}} t={{t}} y={{0.66}} thick={{0.22}} angle={{-14}} />
      <Particles count={{70}} t={{t}} hue={{HUE}} seed={{5}} />
      {body}
      <HudFrame t={{t}} hue="#EAFBFF" />
      <Caption text="{title}" t={{t}} start={{0.06}} size={{0.075}} y={{0.86}} />
    </AbsoluteFill>
  );
}};'''


async def main() -> int:
    await kernel.ensure_storage()

    bodies = [scene("#FF6B2C", "第一种切法", "orb"),
              scene("#22D3EE", "第二种切法", "chart"),
              scene("#FFC93C", "第三种切法", "orbit")]
    secs = [3.0, 3.0, 3.0]
    kinds = ["slide", "zoom"]

    code, tl = film.assemble(bodies, secs, kinds)
    print("时间表", tl, flush=True)

    lint = rc.lint(code)
    print("lint:", "OK" if lint.get("ok") else "FAIL")
    for e in (lint.get("errors") or [])[:5]:
        print(f"  · 第 {e.get('line')} 行 [{e.get('rule')}] {str(e.get('msg'))[:110]}")
    if not lint.get("ok"):
        open("/tmp/trans.tsx", "w", encoding="utf-8").write(code)
        print("（模块已存 /tmp/trans.tsx）")
        return 1

    path, res = await kernel.render(code, seconds=SECONDS, out_path=OUT)
    print("\n成片", path, path.stat().st_size // 1024, "KB")
    print("回执", kernel.describe_receipt(res))

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=nb_frames,width,height,r_frame_rate",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True)
    print(probe.stdout.strip() or probe.stderr.strip())

    # 转场中点抽三帧：跨过切点看有没有"两镜同时在场"的那一帧
    picks = [tl["cuts"][0] - 2, tl["cuts"][0] + 2, tl["cuts"][1] + 2]
    for i, f in enumerate(picks):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                        "-vf", f"select=eq(n\\,{f}),scale=320:-1",
                        "-frames:v", "1", f"/tmp/tr{i}.jpg"], check=False)
    print("抽帧:", [f"/tmp/tr{i}.jpg" for i in range(len(picks))])
    return 0


raise SystemExit(asyncio.run(main()))
