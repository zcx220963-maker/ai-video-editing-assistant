// exploded-assembly —— 拆解合体（科普向）
//
// 治的是什么：讲"一个东西由哪些部分组成、各部分在哪"——原子、细胞、发动机、
// 心脏——现有 128 张卡里 camera/space-camera-moves 有一条 exploded-view，
// 但它是产品片的 UI 构件沿 Z 轴炸开（卡片式、二维平面、为炫技），
// 而科普母题的拆解要的是**同心结构沿半径撑开 + 每一件叫得出名字**。
// 这一族补的是**有名字的拆解**。
//
// 机制（不是 opacity 定时器）：
//   ① 先合体呈现一颗完整的原子（核 + 三条轨道 + 三个电子），静止可读；
//   ② 炸开：各构件沿半径方向撑开（轨道半径 ×1.45、核 ×1.18），
//      每一个构件的引线标签在它到位时画入——"这件叫这个"才指着实物；
//   ③ 电子**一直在转**（角度是帧的函数，炸开不暂停）：
//      拆解中的静止是"停下来看"，不是"画面卡住"；
//   ④ 悬停读数（半径/质量/电荷，这一帧算出来的），然后合回，
//      合回后末 30f 真静止。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这颗原子和它的标签；
//   · 数值是**示意**（玻尔模型，氢原子量级）：换选题改 PARTS 一张表；
//   · 卡面自己的已知坑要写清"这是玻尔模型、不是现代电子云图"——
//     科普片拿过时模型当现状讲，是比没有图更坏的错；
//   · 轨道一律完整圆（椭圆读作"轨道是压扁的"，量子轨道没有这个意思）；
//   · 收尾 ≥30f 真静止：合回之后尾漂读作"又散架了"。
//
// 调校起点：合体 24f + 炸开 34f + 悬停 62f + 合回 30f + 静止 30f，总长 180f（6s）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, tracking } from '@dg/polish';

export const EXPLODED_ASSEMBLY_DURATION = 180;

const W = 1920;
const H = 1080;
const CX = 900;
const CY = 540;

// 构件：以氢/氦原子为模子（玻尔模型示意）。换选题改这张表，机制不动。
// r = 合体态轨道半径，er = 电子半径，ang0 = 初始角，spd = 每帧角速度，
// label = 构件名，read = 悬停读数，dir = 标签引线方向（角度，弧度）。
const PARTS = [
  {
    key: 'nucleus', r: 0, er: 0, ang0: 0, spd: 0,
    label: '原子核', read: '质子 + 中子', dir: Math.PI * 1.15,
  },
  {
    key: 'o1', r: 120, er: 13, ang0: 0.4, spd: 0.085,
    label: '电子', read: '负电，极轻', dir: Math.PI * 0.25,
  },
  {
    key: 'o2', r: 210, er: 13, ang0: 2.6, spd: 0.05,
    label: '第二轨道', read: 'n = 2', dir: Math.PI * 0.85,
  },
  {
    key: 'o3', r: 300, er: 13, ang0: 4.4, spd: 0.032,
    label: '第三轨道', read: 'n = 3', dir: Math.PI * 1.62,
  },
];

// ── 时刻表 ──────────────────────────────────────────────────────────────────
const SOLID_END = 24;      // 合体呈现结束
const EXPLODE_END = 58;    // 炸开结束
const HOVER_END = 120;     // 悬停结束
const REASSEMBLE_END = 150; // 合回结束
const HOLD = 30;

// 炸开进度：0 = 合体，1 = 完全炸开（悬停段恒 1，合回段回 0）
const explodeP = (frame: number): number => {
  if (frame < SOLID_END) return 0;
  if (frame < EXPLODE_END) {
    return interpolate(frame, [SOLID_END, EXPLODE_END], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
      easing: Easing.inOut(Easing.cubic),
    });
  }
  if (frame < HOVER_END) return 1;
  if (frame < REASSEMBLE_END) {
    return interpolate(frame, [HOVER_END, REASSEMBLE_END], [1, 0], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
      easing: Easing.inOut(Easing.cubic),
    });
  }
  return 0;
};

const Nucleus: React.FC<{ frame: number; p: number }> = ({ frame, p }) => {
  // 核：7 个小球聚成团（示意质子中子），随炸开放大
  const s = 1 + 0.18 * p;
  const bumps = [
    [0, 0, 26], [30, 6, 24], [-32, -4, 24], [4, -30, 22], [-6, 30, 22], [34, -26, 20], [-30, 26, 20],
  ];
  return (
    <g transform={`translate(${CX} ${CY}) scale(${s.toFixed(4)})`}>
      {bumps.map(([bx, by, br], i) => (
        <circle key={i} cx={bx} cy={by} r={br} fill={i % 2 === 0 ? '#c96f4a' : '#e0a458'} stroke="rgba(23,24,28,0.18)" strokeWidth={1.2} />
      ))}
      <circle cx={0} cy={0} r={46} fill="none" stroke="rgba(23,24,28,0.22)" strokeWidth={1.5} />
    </g>
  );
};

const Orbit: React.FC<{ i: number; frame: number; p: number }> = ({ i, frame, p }) => {
  const P = PARTS[i];
  const r = P.r * (1 + 0.45 * p);
  const ang = P.ang0 + frame * P.spd;
  const ex = CX + Math.cos(ang) * r;
  const ey = CY + Math.sin(ang) * r;
  return (
    <g>
      {/* 轨道圆：合体时细虚线，炸开后实线 */}
      <circle
        cx={CX}
        cy={CY}
        r={r}
        fill="none"
        stroke={G.hairlineStrong}
        strokeWidth={2}
        strokeDasharray={p > 0.02 ? 'none' : '4 8'}
        opacity={0.55 + 0.4 * p}
      />
      {/* 电子：一直在转 */}
      <circle cx={ex} cy={ey} r={P.er} fill={G.accent} />
      <circle cx={ex} cy={ey} r={P.er + 7} fill="none" stroke={G.accent} strokeWidth={2} opacity={0.45} />
    </g>
  );
};

const PartLabel: React.FC<{ i: number; frame: number; p: number }> = ({ i, frame, p }) => {
  const P = PARTS[i];
  // 标签在炸开进度 0.55 之后画入，合回时先撤
  const at = EXPLODE_END - 16;
  const inP = interpolate(frame, [at, at + 14], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const outP = frame >= HOVER_END
    ? interpolate(frame, [HOVER_END, HOVER_END + 12], [1, 0], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
      })
    : 1;
  const op = Math.min(inP, outP) * p;
  if (op <= 0.02) return null;
  // 引线起点：构件位置（核在中心，电子在轨道上）
  const ang = P.ang0 + frame * P.spd;
  const r = P.r * (1 + 0.45 * p);
  const fromX = P.key === 'nucleus' ? CX : CX + Math.cos(ang) * r;
  const fromY = P.key === 'nucleus' ? CY : CY + Math.sin(ang) * r;
  // 标签锚点：构件沿 dir 方向推 330px（炸开后才出得来）
  const lx = (P.key === 'nucleus' ? CX : fromX) + Math.cos(P.dir) * (P.key === 'nucleus' ? 300 : 250);
  const ly = (P.key === 'nucleus' ? CY : fromY) + Math.sin(P.dir) * (P.key === 'nucleus' ? 190 : 170);
  const midX = fromX + (lx - fromX) * 0.45;
  const midY = fromY + (ly - fromY) * 0.45;
  return (
    <g opacity={op}>
      <path
        d={`M${fromX.toFixed(1)},${fromY.toFixed(1)} L${midX.toFixed(1)},${midY.toFixed(1)} L${lx.toFixed(1)},${ly.toFixed(1)}`}
        fill="none"
        stroke={G.hairlineStrong}
        strokeWidth={2}
      />
      <circle cx={lx} cy={ly} r={6} fill={G.ink1} />
      <text x={lx + (Math.cos(P.dir) >= 0 ? 16 : -16)} y={ly - 6} textAnchor={Math.cos(P.dir) >= 0 ? 'start' : 'end'} fontFamily={FONT.sans} fontSize={34} fontWeight={700} fill={G.ink1} letterSpacing={tracking(34)}>
        {P.label}
      </text>
      <text x={lx + (Math.cos(P.dir) >= 0 ? 16 : -16)} y={ly + 28} textAnchor={Math.cos(P.dir) >= 0 ? 'start' : 'end'} fontFamily={FONT.mono} fontSize={24} fill={G.ink3} style={{ fontVariantNumeric: 'tabular-nums' }}>
        {P.read}
      </text>
    </g>
  );
};

export const ExplodedAssembly: React.FC = () => {
  const frame = useCurrentFrame();
  const p = explodeP(frame);
  const headIn = interpolate(frame, [0, 18], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  // 阶段牌：合体 → 拆解 → 悬停 → 合回（顶栏状态读数，这一帧算出来的）
  const phase = frame < SOLID_END ? '整体' : frame < EXPLODE_END ? '拆解中' : frame < HOVER_END ? '各就各位' : frame < REASSEMBLE_END ? '合回' : '完整';

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.46, y: 0.3 }} accent={G.accent} vignette={0.13} grain={0} />

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
          Take it apart
        </div>
        <div style={{ marginTop: 8, fontSize: 62, fontWeight: 720, letterSpacing: tracking(62), color: G.ink1 }}>
          拆开这一颗
        </div>
      </div>

      {/* 顶栏右侧：阶段读数（这一帧算出来的，不是写死的字符串） */}
      <div style={{ position: 'absolute', right: 110, top: 84, textAlign: 'right', opacity: headIn }}>
        <div style={{ fontSize: 22, letterSpacing: tracking(22, true), textTransform: 'uppercase', color: G.ink3 }}>
          Phase
        </div>
        <div style={{ marginTop: 4, fontSize: 44, fontWeight: 700, color: G.ink1, fontFamily: FONT.sans }}>
          {phase}
        </div>
        <div style={{ marginTop: 2, fontFamily: FONT.mono, fontSize: 24, color: G.accent, fontVariantNumeric: 'tabular-nums' }}>
          {Math.round(p * 100)}% open
        </div>
      </div>

      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        {/* 轨道与电子（核在最底） */}
        {PARTS.slice(1).map((_, i) => (
          <Orbit key={PARTS[i + 1].key} i={i + 1} frame={frame} p={p} />
        ))}
        <Nucleus frame={frame} p={p} />
        {/* 标签（炸开时画入） */}
        {PARTS.map((_, i) => (
          <PartLabel key={`lb-${PARTS[i].key}`} i={i} frame={frame} p={p} />
        ))}
      </svg>

      <Grain opacity={0.04} />
    </div>
  );
};
