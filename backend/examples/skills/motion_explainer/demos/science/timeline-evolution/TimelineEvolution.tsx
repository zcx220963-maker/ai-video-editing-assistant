// timeline-evolution —— 演化时间轴（真实比例，科普向）
//
// 治的是什么：讲"这件事是怎么一步步变成这样的"——生命演化、恒星一生、
// 元素发现史、气候变迁——现有 128 张卡里 data/timeline-travel 是产品片的
// changelog 轴（版本等距、卡片弹立），而科普母题的轴必须**真实比例**：
// 生命前 40 亿年没有硬骨、没有眼睛，早期节点在轴上就该挤在一起——
// 把 46 亿年等距排开，等于告诉观众"每段时间一样长"，那是假话。
// 这一族补的是**比例诚实**的时间轴。
//
// 机制（不是等距排节点）：
//   ① 轴上每个节点的 x = (年份 − 起点) / (终点 − 起点) × 轴长——位置就是年代；
//   ② 视口沿轴行进：段与段的滚动帧数按年代距离分配（大间隔多滚、小间隔少滚），
//     所以"从地球形成滚到最早生命"用的帧数远多于"从祖先滚到现在"；
//   ③ 顶部年份读数随帧真实跳动（tabular-nums）——"轴在走"的唯一凭据；
//   ④ 节点滚到视口中心时卡片沿引线弹出（交替上下），末节点急停推近；
//   ⑤ 刻度（每 500 Ma）随轴走，只有落在视口内的才画——观众看见的是
//     "一条很长的轴在窗外过"，不是"六个点排排站"。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这条轴和它的节点；
//   · 年份是真实读数（示意模子：生命演化 46 亿年），换选题改 NODES 一张表，
//     轴的比例逻辑不动；
//   · 不许对数轴：对数压缩早期等于把"40 亿年无大事"画成"很短"，失真；
//   · 收尾 ≥30f 真静止：轴尾漂读作"还没到今天"。
//
// 调校起点：入场 20f + 5 段滚动（每段 20f + 节点 hold 24f）+ 末节点推近
// 与收尾 36f，总长 276f（约 9.2s）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, tracking } from '@dg/polish';

export const TIMELINE_EVOLUTION_DURATION = 276;

const W = 1920;
const H = 1080;
const AXIS_Y = 560;
const CX = W / 2;               // 视口中心：节点到达这里时弹出

// 节点：年份单位 Ma（百万年前，0 = 现在）。**数值是示意**（生命演化史模子）：
// 换选题改这一张表；轴的比例逻辑（年份 → x）不动。
const START = -4600;            // 轴起点（最早年份）
const END = 0;                  // 轴终点（现在）
const NODES = [
  { year: -4600, label: '地球形成', sub: '熔岩球，没有海', up: false },
  { year: -3800, label: '最早的生命', sub: '单细胞，无核', up: true },
  { year: -540, label: '寒武纪大爆发', sub: '眼睛、壳、骨骼一起出现', up: false },
  { year: -230, label: '恐龙时代', sub: '爬行类统治 1.6 亿年', up: true },
  { year: -66, label: '恐龙灭绝', sub: '小行星，哺乳类上位', up: false },
  { year: 0, label: '现在', sub: '智人，46 亿年之后的读者', up: true },
];
const N = NODES.length;

// 年份 → 轴上的世界 x（真实比例）
const yearToX = (year: number): number => ((year - START) / (END - START)) * (W * 3.2);

// ── 时刻表：分段滚动，段帧数 ∝ 该段的年代距离 ──────────────────────────────
const ENTER = 20;               // 轴入场
const SEG_FRAMES = NODES.slice(1).map((_, i) => {
  const dist = Math.abs(NODES[i + 1].year - NODES[i].year);
  // 每段 18–34f：大间隔滚得久，小间隔滚得快（但不低于 18f 免得读不清）
  return Math.round(18 + 16 * Math.min(1, dist / 4000));
});
const HOLD = 24;                // 节点弹出后的驻留
const segStart = (i: number): number =>
  ENTER + SEG_FRAMES.slice(0, i).reduce((a, b) => a + b, 0) + i * HOLD;
const nodeAtFrame = (i: number): number => segStart(i) + HOLD;   // 节点到达视口中心
const LAST_AT = nodeAtFrame(N - 1);
const ZOOM = 26;                // 末节点推近
const HOLD_END = 30;            // 收尾静止

// 第 frame 帧对应的当前年份（分段线性插值，速度 ∝ 年代距离）
const yearAt = (frame: number): number => {
  if (frame < ENTER) return START;
  let acc = ENTER;
  for (let i = 0; i < N - 1; i++) {
    const segLen = SEG_FRAMES[i];
    if (frame < acc + segLen) {
      const p = (frame - acc) / segLen;
      return NODES[i].year + (NODES[i + 1].year - NODES[i].year) * p;
    }
    acc += segLen + HOLD;
    if (frame < acc) return NODES[i + 1].year;   // hold 段：年份停在节点上
  }
  return END;
};

// 轴的世界位移：让"当前年份"落在视口中心
const axisShift = (frame: number): number => CX - yearToX(yearAt(frame));

// 节点弹出进度：该节点到达中心后 0→1
const popOf = (i: number, frame: number): number => {
  const at = nodeAtFrame(i);
  if (frame < at - 2) return 0;
  return interpolate(frame, [at, at + 16], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.back(1.6)),
  });
};

const TICKS: number[] = [];
for (let y = -4500; y <= 0; y += 500) TICKS.push(y);

const NodeCard: React.FC<{ i: number; frame: number; shift: number }> = ({ i, frame, shift }) => {
  const nd = NODES[i];
  const p = popOf(i, frame);
  if (p <= 0) return null;
  const x = yearToX(nd.year) + shift;
  const dir = nd.up ? -1 : 1;                 // 上方还是下方
  const cardY = AXIS_Y + dir * (150 + 40 * (1 - p));
  const cardX = x;
  return (
    <g opacity={p}>
      {/* 引线：从轴上圆点到卡片 */}
      <line
        x1={x}
        y1={AXIS_Y}
        x2={cardX}
        y2={cardY + dir * -6}
        stroke={G.hairlineStrong}
        strokeWidth={2}
      />
      {/* 轴上的圆点 */}
      <circle cx={x} cy={AXIS_Y} r={13} fill={G.accent} />
      <circle cx={x} cy={AXIS_Y} r={13 + p * 10} fill="none" stroke={G.accent} strokeWidth={2} opacity={0.5 * (1 - p)} />
      {/* 卡片 */}
      <g transform={`translate(${cardX.toFixed(1)} ${cardY.toFixed(1)})`}>
        <rect
          x={-190}
          y={-64}
          width={380}
          height={128}
          rx={16}
          fill="#ffffff"
          stroke={G.hairline}
          style={{ filter: 'drop-shadow(0 4px 14px rgba(20,22,28,0.10))' }}
        />
        <text x={-160} y={-16} fontFamily={FONT.sans} fontSize={36} fontWeight={720} fill={G.ink1} letterSpacing={tracking(36)}>
          {nd.label}
        </text>
        <text x={-160} y={30} fontFamily={FONT.mono} fontSize={30} fontWeight={650} fill={G.accent} style={{ fontVariantNumeric: 'tabular-nums' }}>
          {nd.year === 0 ? '现在' : `${Math.abs(nd.year)} Ma`}
        </text>
        <text x={-160} y={62} fontFamily={FONT.sans} fontSize={22} fill={G.ink3}>
          {nd.sub}
        </text>
      </g>
    </g>
  );
};

export const TimelineEvolution: React.FC = () => {
  const frame = useCurrentFrame();
  const headIn = interpolate(frame, [0, 18], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const shift = axisShift(frame);
  const year = yearAt(frame);

  // 末节点推近：整轴轻微放大 + 当前年份读数放大
  const zoomP = frame >= LAST_AT
    ? interpolate(frame, [LAST_AT, LAST_AT + ZOOM], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.inOut(Easing.cubic),
      })
    : 0;

  // 轴入场：从左缘推进到位
  const enterP = interpolate(frame, [0, ENTER], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.5, y: 0.3 }} accent={G.accent} vignette={0.14} grain={0} />

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
          4.6 billion years
        </div>
        <div style={{ marginTop: 8, fontSize: 62, fontWeight: 720, letterSpacing: tracking(62), color: G.ink1 }}>
          一条很长的轴
        </div>
      </div>

      {/* 当前年份读数：随帧真实跳动（"轴在走"的凭据） */}
      <div style={{ position: 'absolute', right: 110, top: 84, textAlign: 'right', opacity: headIn }}>
        <div style={{ fontSize: 22, letterSpacing: tracking(22, true), textTransform: 'uppercase', color: G.ink3 }}>
          Now at
        </div>
        <div
          style={{
            marginTop: 4,
            fontFamily: FONT.mono,
            fontSize: 52,
            fontWeight: 700,
            color: G.ink1,
            fontVariantNumeric: 'tabular-nums',
            transform: `scale(${(1 + zoomP * 0.18).toFixed(3)})`,
            transformOrigin: 'right center',
          }}
        >
          {Math.abs(Math.round(year / 10) * 10)} Ma
        </div>
      </div>

      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        <g opacity={enterP} transform={`translate(${shift.toFixed(1)} 0) scale(${(1 + zoomP * 0.06).toFixed(4)} ${(1 + zoomP * 0.06).toFixed(4)}) translate(${(W * zoomP * 0.03).toFixed(1)} 0)`}>
          {/* 主轴 */}
          <line x1={-400} y1={AXIS_Y} x2={yearToX(END) + 400} y2={AXIS_Y} stroke={G.hairlineStrong} strokeWidth={4} strokeLinecap="round" />
          {/* 刻度：每 500 Ma，只画视口内的 */}
          {TICKS.map((y) => {
            const x = yearToX(y);
            if (x + shift < -80 || x + shift > W + 80) return null;
            const major = y % 1000 === 0;
            return (
              <g key={y}>
                <line x1={x} y1={AXIS_Y - (major ? 26 : 14)} x2={x} y2={AXIS_Y + (major ? 26 : 14)} stroke={G.ink3} strokeWidth={major ? 3 : 2} />
                {major && (
                  <text x={x} y={AXIS_Y + 56} textAnchor="middle" fontFamily={FONT.mono} fontSize={22} fill={G.ink3} style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {y === 0 ? '0' : `${Math.abs(y)}`}
                  </text>
                )}
              </g>
            );
          })}
        </g>

        {/* 节点（在轴位移之外各自定位） */}
        {NODES.map((_, i) => (
          <NodeCard key={NODES[i].label} i={i} frame={frame} shift={shift} />
        ))}

        {/* 视口中心参考线：节点到达这里弹出（淡淡一条） */}
        <line x1={CX} y1={AXIS_Y - 300} x2={CX} y2={AXIS_Y + 300} stroke={G.accent} strokeWidth={2} strokeDasharray="3 8" opacity={0.35} />
      </svg>

      <Grain opacity={0.04} />
    </div>
  );
};
