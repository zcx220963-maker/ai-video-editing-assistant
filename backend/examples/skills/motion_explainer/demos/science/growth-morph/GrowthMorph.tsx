// growth-morph —— 生长变形（科普向）
//
// 治的是什么：讲"这个东西是怎么一步步长成的"——完全变态、生命周期、
// 恒星演化、材料的相变——现有 128 张卡没有一张讲"形态沿时间变成另一个形态"。
// 模型碰到这类内容只能把四个阶段的图排成一排（幻灯片式），
// 而变形叙事的核心是**上一个形态长成下一个形态**，不是四张图并列。
// 这一族补的是**阶段生长**。
//
// 机制（不是四张图横排）：
//   ① 每个阶段是一组几何构件（卵 = 椭圆 + 叶；幼虫 = 体节串 + 头；
//      蛹 = 梭形壳 + 横纹；蝶 = 身体 + 四片翅膀），构件**逐个**长出
//      （stagger 4f）——"长出来"读得出次序；
//   ② 阶段切换：旧形态淡出（上移 + 缩小），新形态从同一位置生长——
//      过渡段 8f 交叉，不是硬切；
//   ③ 蝶的翅膀展开是 scale + 轻微扇动（sin(帧)）——成虫阶段"活着"；
//   ④ 底部一条生长轴：四个阶段节点连成一条线，当前节点亮起并显示
//      该阶段真实天数——"现在到哪一步"是这条轴在说；
//   ⑤ 收尾 ≥24f 真静止：成虫扇翅尾漂读作"还要变"。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这只生物和它的生长轴；
//   · 天数是**示意**（完全变态模子）：换选题改 STAGES 一张表；
//   · 体节数、翅形都可以改，但"构件逐个长出"这条机制不许改成整组淡入——
//     整组淡入就是四张幻灯片，没有"长"的意思；
//   · 收尾 ≥24f 真静止。
//
// 调校起点：每阶段 44f（构件生长 20f + 驻留 24f）+ 过渡 8f + 收尾 24f，
// 总长 224f（约 7.5s）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing, spring } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, tracking } from '@dg/polish';

export const GROWTH_MORPH_DURATION = 224;

const W = 1920;
const H = 1080;
const CX = 900;
const CY = 500;

// 阶段表：以"完全变态"（卵→幼虫→蛹→成虫）为模子。换选题改这张表，机制不动。
const STAGES = [
  { key: 'egg', label: '卵', days: '4 天' },
  { key: 'larva', label: '幼虫', days: '21 天' },
  { key: 'pupa', label: '蛹', days: '14 天' },
  { key: 'adult', label: '成虫', days: '14 天' },
];

// ── 时刻表 ──────────────────────────────────────────────────────────────────
const GROW = 20;            // 每阶段构件生长时长
const DWELL = 24;           // 每阶段驻留
const XFADE = 8;            // 阶段交叉过渡
const segStart = (i: number): number => i * (GROW + DWELL + XFADE);
const segEnd = (i: number): number => segStart(i) + GROW + DWELL;
const LAST_END = segEnd(STAGES.length - 1);
const HOLD = 24;

// 当前处于哪个阶段、阶段内进度、与下一阶段的过渡进度
const stageOf = (frame: number): { idx: number; p: number; x: number } => {
  for (let i = STAGES.length - 1; i >= 0; i--) {
    if (frame >= segStart(i)) {
      const p = Math.min(1, (frame - segStart(i)) / GROW);
      const x = frame < segEnd(i) ? 0 : Math.min(1, (frame - segEnd(i)) / XFADE);
      return { idx: i, p, x };
    }
  }
  return { idx: 0, p: 0, x: 0 };
};

// 构件逐个长出：第 k 个构件的出现帧
const partAt = (frame: number, k: number): number => {
  const { idx } = stageOf(frame);
  return segStart(idx) + 2 + k * 4;
};

const partPop = (frame: number, k: number): number => {
  const at = partAt(frame, k);
  if (frame < at) return 0;
  return spring({
    frame: frame - at,
    fps: 30,
    config: { damping: 13, stiffness: 240, mass: 0.7 },
    durationInFrames: 14,
  });
};

// 阶段淡出：过渡段 0→1（新阶段生长时旧阶段撤）
const stageOut = (frame: number): number => {
  const { idx, x } = stageOf(frame);
  if (x <= 0) return 0;
  return interpolate(x, [0, 1], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.in(Easing.cubic),
  });
};

// ── 四个阶段的构件 ──────────────────────────────────────────────────────────

const Egg: React.FC<{ frame: number }> = ({ frame }) => {
  const pop = partPop(frame, 0);
  const leaf = partPop(frame, 1);
  return (
    <g opacity={Math.min(pop, leaf)}>
      {/* 叶子 */}
      <path
        d={`M${CX - 150},${CY + 120} Q${CX},${CY + 170} ${CX + 150},${CY + 120} Q${CX},${CY + 60} ${CX - 150},${CY + 120} Z`}
        fill="#9dbb8a"
        stroke="rgba(23,24,28,0.16)"
        strokeWidth={1.5}
        transform={`translate(0 0) scale(${(0.9 + 0.1 * leaf).toFixed(3)})`}
        style={{ transformOrigin: `${CX}px ${CY + 120}px` }}
      />
      {/* 卵：椭圆，淡青 */}
      <ellipse
        cx={CX}
        cy={CY - 10}
        rx={46 * (0.6 + 0.4 * pop)}
        ry={60 * (0.6 + 0.4 * pop)}
        fill="#cfe3dd"
        stroke="rgba(23,24,28,0.2)"
        strokeWidth={1.5}
      />
      <ellipse cx={CX - 12} cy={CY - 26} rx={12} ry={18} fill="#ffffff" opacity={0.5 * pop} />
    </g>
  );
};

const Larva: React.FC<{ frame: number }> = ({ frame }) => {
  // 体节：7 个圆沿一条弧排列（身体轴线），头是大圆
  const segs = 7;
  const bodyPath = (t: number): [number, number] => [
    CX - 240 + 480 * t,
    CY + 60 - Math.sin(t * Math.PI) * 70,
  ];
  return (
    <g>
      {/* 体节逐个长出：从尾到头 */}
      {Array.from({ length: segs }, (_, k) => {
        const i = segs - 1 - k;                 // 从最后一节开始长
        const pop = partPop(frame, i);
        if (pop <= 0) return null;
        const [x, y] = bodyPath((i + 0.5) / segs);
        const r = 40 - Math.abs(i - 3) * 3;
        return (
          <circle
            key={`seg-${i}`}
            cx={x}
            cy={y}
            r={r * (0.5 + 0.5 * pop)}
            fill={i === 0 ? '#7fa06a' : '#a8c48f'}
            stroke="rgba(23,24,28,0.18)"
            strokeWidth={1.4}
          />
        );
      })}
      {/* 头 + 触角：体节之后 */}
      {(() => {
        const pop = partPop(frame, segs);
        if (pop <= 0) return null;
        const [hx, hy] = bodyPath(0);
        return (
          <g opacity={pop}>
            <circle cx={hx - 40} cy={hy - 20} r={44 * (0.5 + 0.5 * pop)} fill="#5d8a4f" stroke="rgba(23,24,28,0.2)" strokeWidth={1.4} />
            <circle cx={hx - 56} cy={hy - 34} r={5} fill={G.ink1} />
            <path d={`M${hx - 60},${hy - 52} q-14,-22 4,-34`} fill="none" stroke={G.ink2} strokeWidth={3} strokeLinecap="round" />
            <path d={`M${hx - 40},${hy - 56} q6,-24 22,-30`} fill="none" stroke={G.ink2} strokeWidth={3} strokeLinecap="round" />
          </g>
        );
      })()}
    </g>
  );
};

const Pupa: React.FC<{ frame: number }> = ({ frame }) => {
  const pop = partPop(frame, 0);
  const vein = partPop(frame, 1);
  return (
    <g opacity={Math.min(pop, vein)}>
      {/* 枝：一根斜线 */}
      <line x1={CX - 190} y1={CY - 190} x2={CX + 190} y2={CY - 230} stroke="#8a6f4d" strokeWidth={7} strokeLinecap="round" />
      {/* 蛹壳：梭形（闭合 path） */}
      <path
        d={`M${CX},${CY - 250} C${CX + 90},${CY - 150} ${CX + 80},${CY + 130} ${CX},${CY + 220} C${CX - 80},${CY + 130} ${CX - 90},${CY - 150} ${CX},${CY - 250} Z`}
        fill="#d9c9a8"
        stroke="rgba(23,24,28,0.22)"
        strokeWidth={2}
        transform={`translate(0 0) scale(${(0.7 + 0.3 * pop).toFixed(3)})`}
        style={{ transformOrigin: `${CX}px ${CY - 20}px` }}
      />
      {/* 横纹：三条 */}
      {[0.2, 0.45, 0.7].map((t, k) => {
        const vp = partPop(frame, 2 + k);
        if (vp <= 0) return null;
        const y = CY - 250 + (CY + 220 - (CY - 250)) * t;
        const halfW = 78 * Math.sin(Math.PI * t * 0.9 + 0.15);
        return (
          <path
            key={`v-${k}`}
            d={`M${CX - halfW},${y} Q${CX},${y + 14} ${CX + halfW},${y}`}
            fill="none"
            stroke="rgba(23,24,28,0.3)"
            strokeWidth={3}
            opacity={vp}
          />
        );
      })}
    </g>
  );
};

// 翅膀 path（右翅，左翅镜像）。前翅大、后翅小。
const WING_TOP = `M0,-10 C60,-70 190,-120 250,-150 C300,-175 320,-140 280,-90 C230,-30 120,10 30,20 Z`;
const WING_BOT = `M0,10 C50,60 150,110 210,130 C260,145 270,175 220,180 C150,185 60,140 20,80 Z`;

const Adult: React.FC<{ frame: number }> = ({ frame }) => {
  const body = partPop(frame, 0);
  const wTop = partPop(frame, 1);
  const wBot = partPop(frame, 2);
  const ant = partPop(frame, 3);
  if (body <= 0) return null;
  // 翅膀扇动：sin(帧)，慢周期
  const flap = 1 + 0.05 * Math.sin(frame * 0.16);
  const wing = (side: number, top: boolean): React.ReactNode => {
    const p = top ? wTop : wBot;
    if (p <= 0) return null;
    return (
      <g transform={`translate(${CX} ${CY}) scale(${side * p * flap} ${p})`}>
        <path d={top ? WING_TOP : WING_BOT} fill={top ? '#d98cb3' : '#b87ba0'} stroke="rgba(23,24,28,0.25)" strokeWidth={2} />
        <path d={top ? WING_TOP : WING_BOT} fill="none" stroke="rgba(255,255,255,0.35)" strokeWidth={1.5} />
      </g>
    );
  };
  return (
    <g>
      {/* 翅膀在身体之下（先画） */}
      {wing(1, true)}
      {wing(-1, true)}
      {wing(1, false)}
      {wing(-1, false)}
      {/* 身体 */}
      <ellipse cx={CX} cy={CY} rx={22 * (0.5 + 0.5 * body)} ry={95 * (0.5 + 0.5 * body)} fill={G.ink2} />
      <circle cx={CX} cy={CY - 100} r={26 * (0.5 + 0.5 * body)} fill={G.ink2} />
      {/* 触角 */}
      {ant > 0 && (
        <g opacity={ant}>
          <path d={`M${CX - 8},${CY - 118} q-16,-34 6,-52`} fill="none" stroke={G.ink2} strokeWidth={3.5} strokeLinecap="round" />
          <path d={`M${CX + 8},${CY - 118} q16,-34 -6,-52`} fill="none" stroke={G.ink2} strokeWidth={3.5} strokeLinecap="round" />
          <circle cx={CX - 4} cy={CY - 170} r={5} fill={G.ink2} />
          <circle cx={CX + 4} cy={CY - 170} r={5} fill={G.ink2} />
        </g>
      )}
    </g>
  );
};

export const GrowthMorph: React.FC = () => {
  const frame = useCurrentFrame();
  const { idx } = stageOf(frame);
  const headIn = interpolate(frame, [0, 18], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });

  const stages: Record<string, React.FC<{ frame: number }>> = {
    egg: Egg, larva: Larva, pupa: Pupa, adult: Adult,
  };
  const Stage = stages[STAGES[idx].key];

  // 底部生长轴：四节点连线，当前亮
  const axisY = 900;
  const axisX = (i: number): number => 480 + i * 320;

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.47, y: 0.3 }} accent={G.accent} vignette={0.13} grain={0} />

      {/* 页眉 */}
      <div
        style={{
          position: 'absolute',
          left: 110,
          top: 84,
          opacity: headIn,
          transform: `translateY(${((1 - headIn) * 10).toFixed(2)}px)`,
        }}
      >
        <div style={{ fontSize: 28, fontWeight: 650, letterSpacing: tracking(28, true), textTransform: 'uppercase', color: G.accent }}>
          Grow into it
        </div>
        <div style={{ marginTop: 8, fontSize: 62, fontWeight: 720, letterSpacing: tracking(62), color: G.ink1 }}>
          一步一步长成
        </div>
      </div>

      {/* 右上角：当前阶段读数（这一帧算出来的） */}
      <div style={{ position: 'absolute', right: 110, top: 84, textAlign: 'right', opacity: headIn }}>
        <div style={{ fontSize: 22, letterSpacing: tracking(22, true), textTransform: 'uppercase', color: G.ink3 }}>
          Stage {idx + 1} / {STAGES.length}
        </div>
        <div style={{ marginTop: 4, fontSize: 48, fontWeight: 720, color: G.ink1 }}>
          {STAGES[idx].label}
        </div>
        <div style={{ marginTop: 2, fontFamily: FONT.mono, fontSize: 26, color: G.accent, fontVariantNumeric: 'tabular-nums' }}>
          约 {STAGES[idx].days}
        </div>
      </div>

      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        {/* 当前阶段构件 */}
        <Stage frame={frame} />
      </svg>

      {/* 底部生长轴 */}
      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        <line x1={axisX(0)} y1={axisY} x2={axisX(STAGES.length - 1)} y2={axisY} stroke={G.hairlineStrong} strokeWidth={3} />
        {STAGES.map((s, i) => {
          const on = i <= idx;
          return (
            <g key={s.key}>
              <circle cx={axisX(i)} cy={axisY} r={on ? 18 : 12} fill={on ? G.accent : '#ffffff'} stroke={on ? G.accent : G.hairlineStrong} strokeWidth={2.5} />
              <text x={axisX(i)} y={axisY + 52} textAnchor="middle" fontFamily={FONT.sans} fontSize={26} fontWeight={on ? 700 : 500} fill={on ? G.ink1 : G.ink3}>
                {s.label}
              </text>
              <text x={axisX(i)} y={axisY + 84} textAnchor="middle" fontFamily={FONT.mono} fontSize={20} fill={G.ink3} style={{ fontVariantNumeric: 'tabular-nums' }}>
                {s.days}
              </text>
            </g>
          );
        })}
      </svg>

      <Grain opacity={0.04} />
    </div>
  );
};
