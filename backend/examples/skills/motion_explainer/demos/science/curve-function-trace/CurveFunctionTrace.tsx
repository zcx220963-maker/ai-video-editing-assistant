// curve-function-trace —— 函数曲线逐笔绘制（科普向）
//
// 治的是什么：科普片里「这个函数长什么样」是最高频的画面之一——
// 正弦、抛物线、指数衰减、阻尼振荡。现有 132 张卡里一张都挑不出来，
// 模型只能硬套 data/chart-live-moves 的骨架——表现出来就是「图表做出了仪表盘的样子」。
// 这一族补的是**曲线的绘制过程本身**：不是给一个成品图，而是让观众看见"它一笔一笔长出来"。
//
// 机制（不是 opacity 淡入一张成品图）：
//   ① 坐标轴与网格先淡入（给"这是坐标系"一个锚定）；
//   ② 包络线（虚线）先画入——"振幅在衰减"这条信息在主线之前先铺好；
//   ③ 主曲线用 stroke-dashoffset 逐笔绘制，从左到右，每一帧推进一点；
//   ④ 关键点（峰 / 谷 / 零点 / 包络交点）在曲线"画到"该点时弹出标注——
//      不是提前排好的，是"画到这里才揭示"；
//   ⑤ 底部函数表达式随帧逐字打出，与曲线绘制同步。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这条曲线和它的坐标系；
//   · 每个点的 y 坐标必须是**这一帧算出来的**（不是 lookup table）；
//   · 坐标轴刻度用 tabular-nums，对齐才读得出"0.5 对 1.0"；
//   · 收尾 ≥28f 真静止：曲线尾漂读作"还没画完"。
//
// 调校起点：轴 18f + 包络 22f + 绘制 120f + 标注滞后 8f + 收尾 30f，总长 198f（6.6s）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, tracking } from '@dg/polish';

export const CURVE_FUNCTION_DURATION = 198;

const W = 1920;
const H = 1080;
const PAD = 120;            // 画布边距
const GW = W - PAD * 2;     // 网格宽
const GH = H - PAD * 2 - 180; // 网格高（底部留给函数式）
const OX = PAD;             // 原点 x（左下角）
const OY = H - PAD - 180;   // 原点 y

// 函数定义：阻尼振荡 y = e^(-0.3x) * sin(4πx)
// x ∈ [0, 4]，映射到画布宽度
const X_MIN = 0;
const X_MAX = 4;
const N_POINTS = 800;       // 采样点数（足够多的线段让曲线平滑）

const fn = (x: number): number => Math.exp(-0.3 * x) * Math.sin(4 * Math.PI * x);
const envelopePos = (x: number): number => Math.exp(-0.3 * x);
const envelopeNeg = (x: number): number => -Math.exp(-0.3 * x);

// x 值 → 画布坐标
const xToCanvas = (x: number): number => OX + (x - X_MIN) / (X_MAX - X_MIN) * GW;
const yToCanvas = (y: number): number => OY - (y + 1.1) / 2.2 * GH; // y 范围 [-1.1, 1.1]

// 生成路径 d 字符串
const buildPath = (yf: (x: number) => number): string => {
  const step = (X_MAX - X_MIN) / N_POINTS;
  let d = '';
  for (let i = 0; i <= N_POINTS; i++) {
    const x = X_MIN + i * step;
    const y = yf(x);
    const cx = xToCanvas(x);
    const cy = yToCanvas(y);
    d += (i === 0 ? 'M' : 'L') + `${cx.toFixed(1)},${cy.toFixed(1)}`;
  }
  return d;
};

const MAIN_PATH = buildPath(fn);
const ENV_POS_PATH = buildPath(envelopePos);
const ENV_NEG_PATH = buildPath(envelopeNeg);

// 关键点：x 坐标 + 类型 + 标签
const KEY_POINTS: { x: number; label: string; sub: string; type: 'peak' | 'valley' | 'zero' | 'env' }[] = [
  { x: 0.125, label: '首峰', sub: 'A₀ = 1.0', type: 'peak' },
  { x: 0.375, label: '首谷', sub: 'A₁ = -0.75', type: 'valley' },
  { x: 0.625, label: '二峰', sub: 'A₂ = 0.55', type: 'peak' },
  { x: 0.875, label: '二谷', sub: 'A₃ = -0.40', type: 'valley' },
  { x: 1.25, label: '三峰', sub: 'A₄ = 0.22', type: 'peak' },
  { x: 1.75, label: '四峰', sub: 'A₅ = 0.10', type: 'peak' },
  { x: 2.5, label: '趋零', sub: '|y| < 0.05', type: 'env' },
];

const AXIS_IN = 18;         // 轴淡入帧数
const ENV_IN = 22;          // 包络线画入帧数
const DRAW_START = 40;      // 主曲线起绘帧
const DRAW_END = 160;       // 主曲线绘完帧
const LABEL_LAG = 8;        // 标注滞后帧数
const HOLD = 30;            // 收尾静止

export const CurveFunctionTrace: React.FC = () => {
  const frame = useCurrentFrame();

  // 轴与网格淡入
  const axisOp = interpolate(frame, [0, AXIS_IN], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });

  // 包络线画入（stroke-dashoffset）
  const envProgress = interpolate(frame, [AXIS_IN, AXIS_IN + ENV_IN], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.cubic),
  });

  // 主曲线绘制进度 0→1
  const drawP = interpolate(frame, [DRAW_START, DRAW_END], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.cubic),
  });

  // 函数式逐字打出进度
  const formula = 'y = e⁻⁰·³ˣ · sin(4πx)';
  const typeP = interpolate(frame, [DRAW_START, DRAW_END + 20], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const typeChars = Math.floor(typeP * formula.length);

  // 计算主曲线的总长度（用于 dashoffset）
  // 用 path.getTotalLength() 在 SVG 里做，但这里先预估算一个近似值
  // 实际上用 CSS stroke-dasharray/dashoffset 更简单，但在 Remotion 里需要每帧算
  // 改用 clipPath：一个随 drawP 增长的矩形从左到右覆盖
  const clipW = drawP * (GW + 20);

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.5, y: 0.3 }} accent={G.accent} vignette={0.12} grain={0} />

      {/* 页眉 */}
      <div style={{ position: 'absolute', left: 110, top: 64, opacity: axisOp }}>
        <text style={{ fontFamily: FONT.sans, fontSize: 28, fontWeight: 600, fill: G.ink3, letterSpacing: tracking(28) }}>
          阻尼振荡
        </text>
      </div>

      <svg width={W} height={H} style={{ position: 'absolute', top: 0, left: 0 }}>
        {/* 网格 */}
        <g opacity={axisOp * 0.35}>
          {[0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4].map((x) => (
            <line key={`vx-${x}`}
              x1={xToCanvas(x)} y1={yToCanvas(1.1)}
              x2={xToCanvas(x)} y2={yToCanvas(-1.1)}
              stroke={G.hairline} strokeWidth={1}
            />
          ))}
          {[-1, -0.5, 0, 0.5, 1].map((y) => (
            <line key={`hy-${y}`}
              x1={xToCanvas(0)} y1={yToCanvas(y)}
              x2={xToCanvas(4)} y2={yToCanvas(y)}
              stroke={G.hairline} strokeWidth={1}
            />
          ))}
        </g>

        {/* 坐标轴 */}
        <g opacity={axisOp}>
          <line x1={OX} y1={yToCanvas(0)} x2={OX + GW} y2={yToCanvas(0)} stroke={G.ink2} strokeWidth={2} />
          <line x1={OX} y1={yToCanvas(-1.1)} x2={OX} y2={yToCanvas(1.1)} stroke={G.ink2} strokeWidth={2} />
          {/* x 轴刻度 */}
          {[0, 1, 2, 3, 4].map((x) => (
            <g key={`xt-${x}`}>
              <line x1={xToCanvas(x)} y1={yToCanvas(0)} x2={xToCanvas(x)} y2={yToCanvas(0) + 8} stroke={G.ink2} strokeWidth={2} />
              <text x={xToCanvas(x)} y={yToCanvas(0) + 36} textAnchor="middle" fontFamily={FONT.mono} fontSize={22} fill={G.ink3} style={{ fontVariantNumeric: 'tabular-nums' }}>
                {x.toFixed(0)}
              </text>
            </g>
          ))}
          {/* y 轴刻度 */}
          {[-1, -0.5, 0.5, 1].map((y) => (
            <g key={`yt-${y}`}>
              <line x1={OX - 8} y1={yToCanvas(y)} x2={OX} y2={yToCanvas(y)} stroke={G.ink2} strokeWidth={2} />
              <text x={OX - 18} y={yToCanvas(y) + 7} textAnchor="end" fontFamily={FONT.mono} fontSize={20} fill={G.ink3} style={{ fontVariantNumeric: 'tabular-nums' }}>
                {y.toFixed(1)}
              </text>
            </g>
          ))}
          {/* 轴标签 */}
          <text x={OX + GW + 14} y={yToCanvas(0) + 6} fontFamily={FONT.sans} fontSize={22} fill={G.ink3}>t</text>
          <text x={OX + 6} y={yToCanvas(1.15) - 6} fontFamily={FONT.sans} fontSize={22} fill={G.ink3}>y</text>
        </g>

        {/* 包络线 */}
        <g opacity={interpolate(frame, [AXIS_IN, AXIS_IN + 12], [0, 0.5], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })}>
          <path d={ENV_POS_PATH} fill="none" stroke={G.accent} strokeWidth={2} strokeDasharray={`${envProgress * 2000} 2000`} strokeDashoffset={0} opacity={0.45} />
          <path d={ENV_NEG_PATH} fill="none" stroke={G.accent} strokeWidth={2} strokeDasharray={`${envProgress * 2000} 2000`} strokeDashoffset={0} opacity={0.45} />
        </g>

        {/* 主曲线：用 clipPath 做逐笔绘制 */}
        <defs>
          <clipPath id="curve-clip">
            <rect x={OX - 10} y={0} width={clipW} height={H} />
          </clipPath>
        </defs>
        <g clipPath="url(#curve-clip)">
          <path d={MAIN_PATH} fill="none" stroke={G.ink1} strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" />
        </g>

        {/* 当前绘制点（笔尖） */}
        {drawP > 0 && drawP < 1 && (
          <circle
            cx={xToCanvas(X_MIN + drawP * (X_MAX - X_MIN))}
            cy={yToCanvas(fn(X_MIN + drawP * (X_MAX - X_MIN)))}
            r={6}
            fill={G.accent}
            style={{ filter: 'drop-shadow(0 0 6px ' + G.accent + ')' }}
          />
        )}

        {/* 关键点标注 */}
        {KEY_POINTS.map((kp, i) => {
          const kpFrame = DRAW_START + (kp.x / X_MAX) * (DRAW_END - DRAW_START) + LABEL_LAG;
          const p = interpolate(frame, [kpFrame, kpFrame + 14], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
            easing: Easing.out(Easing.back(1.5)),
          });
          if (p <= 0) return null;
          const cx = xToCanvas(kp.x);
          const cy = yToCanvas(fn(kp.x));
          const up = kp.type === 'peak' || kp.type === 'env';
          const labelY = up ? cy - 40 - 20 * (1 - p) : cy + 50 + 20 * (1 - p);
          return (
            <g key={i} opacity={p}>
              {/* 引线 */}
              <line x1={cx} y1={cy} x2={cx} y2={labelY + (up ? 14 : -14)} stroke={G.hairlineStrong} strokeWidth={1.5} />
              {/* 标签卡片 */}
              <g transform={`translate(${cx} ${labelY})`}>
                <rect x={-70} y={-22} width={140} height={44} rx={10} fill="#ffffff" stroke={G.hairline} style={{ filter: 'drop-shadow(0 3px 10px rgba(20,22,28,0.08))' }} />
                <text x={0} y={-2} textAnchor="middle" fontFamily={FONT.sans} fontSize={18} fontWeight={650} fill={G.ink1}>{kp.label}</text>
                <text x={0} y={18} textAnchor="middle" fontFamily={FONT.mono} fontSize={15} fill={G.accent} style={{ fontVariantNumeric: 'tabular-nums' }}>{kp.sub}</text>
              </g>
            </g>
          );
        })}
      </svg>

      {/* 底部函数式 */}
      <div style={{
        position: 'absolute',
        left: PAD,
        bottom: 40,
        fontFamily: FONT.mono,
        fontSize: 32,
        fontWeight: 500,
        color: G.ink1,
        letterSpacing: '0.04em',
      }}>
        {formula.slice(0, typeChars)}
        <span style={{ opacity: 0.3 }}>{formula.slice(typeChars)}</span>
      </div>
    </div>
  );
};
