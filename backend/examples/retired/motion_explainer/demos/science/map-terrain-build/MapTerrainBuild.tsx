// map-terrain-build —— 地图地形（科普向）
//
// 治的是什么：讲地理/地质/流域时，模型手上只有 ui-entrance 那 21 张产品片卡，
// 于是把地形画成了「带圆角的卡片滑进来」。这一族补的是**地图**。
//
// 机制：
//   ① 等高线自中心向外**逐条立起**（scale 0.82→1 + 上移 26px + 淡入），
//      每条的出现时刻按它的高程排序——所以"从谷底堆到山顶"是空间上成立的。
//   ② 一条河用 stroke-dasharray 把自己画出来（不是 fade in 一整条），
//      画到的位置带一个亮点，读作"水在流"。
//   ③ 三个地名在该高程被堆到之后弹出，带引线指到实际位置。
//   ④ 收尾 28f 真静止 + 缓慢推近（1 → 1.06），给"这是一张能放大的图"一个暗示。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用"功能卡"；画面里只有这张图和它的标注；
//   · 等高线是**程序生成的闭合曲线**（极坐标半径 = 基准 + 高程项 × 起伏函数），
//     不是贴图——所以改起伏函数就换一座山，机制不动；
//   · 河流路径必须落在地形低处（半径大的谷地方向），不能横穿山脊。
import React from 'react';
import { useCurrentFrame, interpolate, Easing, spring } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, softShadow, tracking } from '@dg/polish';

export const MAP_TERRAIN_DURATION = 158;

const W = 1920;
const H = 1080;
const CX = 960;
const CY = 560;

// ── 地形：等高线 ────────────────────────────────────────────────────────────
// 每条等高线是一个闭合曲线。半径 = base + elev × amp，其中起伏由 cos/sin 叠加给出
// （纯函数、禁随机——同一帧渲染两遍必须逐位相同）。
const CONTOURS = [
  { elev: 0, base: 330, amp: 0.10, label: '0 m', tone: 'valley' },
  { elev: 1, base: 285, amp: 0.12, label: '200 m', tone: 'low' },
  { elev: 2, base: 240, amp: 0.14, label: '400 m', tone: 'low' },
  { elev: 3, base: 195, amp: 0.16, label: '600 m', tone: 'mid' },
  { elev: 4, base: 150, amp: 0.18, label: '800 m', tone: 'mid' },
  { elev: 5, base: 105, amp: 0.20, label: '1 000 m', tone: 'high' },
  { elev: 6, base: 62, amp: 0.22, label: '1 180 m', tone: 'peak' },
];

// 起伏函数：两个不同频率的余弦叠加，纯函数无随机
const relief = (theta: number, k: number): number =>
  Math.cos(theta * 2 + k * 0.9) * 0.6 + Math.cos(theta * 5 - k * 1.7) * 0.4;

const contourPath = (c: (typeof CONTOURS)[number]): string => {
  const N = 96;
  const pts: string[] = [];
  for (let i = 0; i <= N; i++) {
    const th = (i / N) * Math.PI * 2;
    const r = c.base + c.amp * c.base * relief(th, c.elev);
    const x = CX + Math.cos(th) * r;
    const y = CY + Math.sin(th) * r * 0.92; // 略微压扁，读作俯视投影
    pts.push(`${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return pts.join(' ') + ' Z';
};

// ── 河流：一条自山脊流向谷地的折线（落在地形低处）────────────────────────────
// 方向角固定，半径从山顶外缘一路走到谷地——这是"水往下走"的几何表达。
const RIVER_ANGLE = Math.PI * 0.62;
const riverPoints = (): Array<[number, number]> => {
  const out: Array<[number, number]> = [];
  const steps = 46;
  for (let i = 0; i <= steps; i++) {
    const t = i / steps; // 0 = 源头（高处），1 = 谷地（低处）
    const r = 118 + t * 250;
    // 加一点侧向蜿蜒（纯函数），让河不是一条直线
    const wob = Math.sin(t * Math.PI * 3.1) * 26 * (0.35 + t);
    const th = RIVER_ANGLE + wob / r;
    out.push([CX + Math.cos(th) * r, CY + Math.sin(th) * r * 0.92]);
  }
  return out;
};
const RIVER = riverPoints();
const riverD = RIVER.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');

// 河流总长度（用于 dasharray 动画）
const riverLen = (() => {
  let acc = 0;
  for (let i = 1; i < RIVER.length; i++) {
    acc += Math.hypot(RIVER[i][0] - RIVER[i - 1][0], RIVER[i][1] - RIVER[i - 1][1]);
  }
  return acc;
})();

// ── 时刻表 ──────────────────────────────────────────────────────────────────
const RISE_START = 10;          // 第一条等高线起立
const RISE_STEP = 11;           // 相邻两条的间隔
const RIVER_START = 10 + CONTOURS.length * RISE_STEP + 6;   // 河在等高线堆完之后画
const RIVER_DUR = 46;
const LABEL_START = RIVER_START + RIVER_DUR + 8;
const HOLD = 28;

const riseOf = (frame: number, i: number): number => {
  const s = RISE_START + i * RISE_STEP;
  return interpolate(frame, [s, s + 20], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
};

const TONE: Record<string, { stroke: string; fill: string }> = {
  valley: { stroke: 'rgba(91,132,168,0.55)', fill: 'rgba(91,132,168,0.06)' },
  low: { stroke: 'rgba(91,132,168,0.42)', fill: 'rgba(91,132,168,0.04)' },
  mid: { stroke: 'rgba(120,120,110,0.42)', fill: 'rgba(150,145,120,0.05)' },
  high: { stroke: 'rgba(150,130,95,0.5)', fill: 'rgba(180,155,110,0.07)' },
  peak: { stroke: 'rgba(170,140,95,0.72)', fill: 'rgba(200,175,125,0.12)' },
};

const PLACES = [
  { name: '分水岭', at: 0.06, dx: -190, dy: -150 },
  { name: '河谷', at: 0.62, dx: 230, dy: 96 },
  { name: '冲积平原', at: 0.99, dx: -60, dy: 210 },
];

const PlaceTag: React.FC<{ i: number; frame: number }> = ({ i, frame }) => {
  const P = PLACES[i];
  const s = LABEL_START + i * 13;
  const pop = spring({
    frame: frame - s,
    fps: 30,
    config: { damping: 12, stiffness: 170, mass: 0.85 },
    durationInFrames: 24,
  });
  if (frame < s) return null;
  const idx = Math.round(P.at * (RIVER.length - 1));
  const [px, py] = RIVER[idx];
  const x = px + P.dx;
  const y = py + P.dy;
  const bend = (px + x) / 2;
  return (
    <g opacity={Math.min(1, pop * 1.6)}>
      <circle cx={px} cy={py} r={7 * pop} fill={G.accent} />
      <path
        d={`M${px.toFixed(1)},${py.toFixed(1)} L${bend.toFixed(1)},${py.toFixed(1)} L${bend.toFixed(1)},${y.toFixed(1)} L${(x - 22 * pop).toFixed(1)},${y.toFixed(1)}`}
        fill="none"
        stroke={G.hairlineStrong}
        strokeWidth={1.5}
        strokeLinecap="round"
      />
      <rect
        x={x}
        y={y - 24}
        width={148 * pop}
        height={48}
        rx={10}
        fill="#ffffff"
        stroke={G.hairline}
        style={{ filter: 'drop-shadow(0 2px 6px rgba(20,22,28,0.10))' }}
      />
      <text
        x={x + 74 * pop}
        y={y + 7}
        textAnchor="middle"
        fontFamily={FONT.sans}
        fontSize={26}
        fontWeight={650}
        letterSpacing={tracking(26)}
        fill={G.ink1}
      >
        {P.name}
      </text>
    </g>
  );
};

export const MapTerrainBuild: React.FC = () => {
  const frame = useCurrentFrame();

  // 河：dasharray 把自己画出来；画到的位置一个亮点跟着走
  const riverP = interpolate(frame, [RIVER_START, RIVER_START + RIVER_DUR], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.quad),
  });
  const headIdx = Math.min(RIVER.length - 1, Math.round(riverP * (RIVER.length - 1)));
  const [hx, hy] = RIVER[headIdx];

  // 收尾推近
  const zoom = interpolate(frame, [RIVER_START + RIVER_DUR, MAP_TERRAIN_DURATION - HOLD], [1, 1.06], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.quad),
  });
  const headIn = interpolate(frame, [0, 16], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.5, y: 0.26 }} accent={G.accent} vignette={0.13} grain={0} />

      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        <defs>
          <radialGradient id="mt-glow" cx="0.5" cy="0.52" r="0.5">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.85" />
            <stop offset="70%" stopColor="#ffffff" stopOpacity="0.25" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </radialGradient>
        </defs>

        {/* 图面底板 */}
        <rect x={0} y={0} width={W} height={H} fill="url(#mt-glow)" />

        <g transform={`translate(${CX} ${CY}) scale(${zoom}) translate(${-CX} ${-CY})`}>
          {/* 等高线：自中心向外逐条立起。画顺序 = elev 升序 = 空间上的"谷底→山顶" */}
          {CONTOURS.map((c, i) => {
            const p = riseOf(frame, i);
            if (p <= 0) return null;
            const s = 0.82 + 0.18 * p;
            const lift = (1 - p) * 26;
            const t = TONE[c.tone];
            return (
              <g key={c.elev} opacity={p} transform={`translate(0 ${lift.toFixed(2)}) scale(${s.toFixed(4)}) translate(${CX} ${CY}) scale(${(1 / s).toFixed(4)}) translate(${-CX} ${-CY})`}>
                <path d={contourPath(c)} fill={t.fill} stroke={t.stroke} strokeWidth={1.6} />
              </g>
            );
          })}

          {/* 河流：dasharray 自画 */}
          <path
            d={riverD}
            fill="none"
            stroke="rgba(64,120,170,0.9)"
            strokeWidth={6}
            strokeLinecap="round"
            strokeDasharray={`${(riverLen * riverP).toFixed(2)} ${riverLen.toFixed(2)}`}
          />
          {/* 水头亮点：跟着画到的位置走 */}
          {riverP > 0 && riverP < 1 && (
            <>
              <circle cx={hx} cy={hy} r={16} fill="rgba(64,120,170,0.16)" />
              <circle cx={hx} cy={hy} r={7} fill="#3f7cb8" />
            </>
          )}

          {/* 地名 */}
          {PLACES.map((_, i) => (
            <PlaceTag key={PLACES[i].name} i={i} frame={frame} />
          ))}
        </g>

        {/* 高程标尺：右侧一根带刻度的尺，刻度随等高线立起逐个点亮 */}
        <g>
          <line x1={1770} y1={250} x2={1770} y2={830} stroke={G.hairlineStrong} strokeWidth={1.5} />
          {CONTOURS.map((c, i) => {
            const p = riseOf(frame, i);
            if (p <= 0) return null;
            const y = 830 - (c.elev / (CONTOURS.length - 1)) * 580;
            return (
              <g key={`tk-${c.elev}`} opacity={p}>
                <line x1={1758} y1={y} x2={1782} y2={y} stroke={G.ink2} strokeWidth={1.5} />
                <text
                  x={1748}
                  y={y + 9}
                  textAnchor="end"
                  fontFamily={FONT.mono}
                  fontSize={22}
                  fill={G.ink2}
                >
                  {c.label}
                </text>
              </g>
            );
          })}
        </g>
      </svg>

      {/* 固定页眉：只有一句 */}
      <div
        style={{
          position: 'absolute',
          left: 110,
          top: 88,
          opacity: headIn,
          transform: `translateY(${((1 - headIn) * 10).toFixed(2)}px)`,
        }}
      >
        <div
          style={{
            fontSize: 28,
            fontWeight: 650,
            letterSpacing: tracking(28, true),
            textTransform: 'uppercase',
            color: G.accent,
          }}
        >
          Watershed
        </div>
        <div style={{ marginTop: 8, fontSize: 62, fontWeight: 720, letterSpacing: tracking(62), color: G.ink1 }}>
          一座山怎么长出来
        </div>
      </div>

      {/* 右下：流域面积读数（这一帧算出来的） */}
      <div
        style={{
          position: 'absolute',
          right: 110,
          bottom: 84,
          textAlign: 'right',
          fontFamily: FONT.mono,
          fontSize: 24,
          color: G.ink3,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        <div style={{ fontSize: 20, letterSpacing: tracking(20, true), textTransform: 'uppercase' }}>
          Relief
        </div>
        <div style={{ marginTop: 4, fontSize: 44, color: G.ink1, fontWeight: 600 }}>
          {CONTOURS.filter((_, i) => riseOf(frame, i) > 0.5).length} / {CONTOURS.length}
        </div>
      </div>

      <Grain opacity={0.04} />
    </div>
  );
};
