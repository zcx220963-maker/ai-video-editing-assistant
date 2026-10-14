// process-diagram-flow —— 流程示意（循环型，科普向）
//
// 治的是什么：讲"这个过程怎么走一圈"时，模型手上只有产品片的转场卡，
// 于是把流程做成了"几张卡片依次淡入"。这一族补的是**流程**，而且专挑**循环**——
// 水循环、碳循环、食物链、经济循环，科普片里"一圈"比"一串"常见得多。
//
// 机制：
//   ① 节点先全部落位（静止一帧可读），然后**箭头按次序逐段画出来**——
//      画的次序就是流程的次序，所以"先发生什么"是看得见的。
//   ② 一个令牌沿已点亮的路径行进，行到哪里哪一段亮——
//      这是"现在进行到这一步"的唯一凭据。
//   ③ 每段箭头画完时，对应节点从灰底转为实底并弹一下（spring 过冲 6%）。
//   ④ 走完一圈后令牌继续绕（第二圈速度 0.7×），
//      同时中心标题从"一圈"改为"N 圈"——循环类内容必须让观众看见"它一直在转"。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这张流程图和它的标注；
//   · 箭头一律**单向**（循环没有回头路），用 marker 三角而不是线段两端都画头；
//   · 每段箭头的标签是**动作**（"蒸发""凝结"），不是步骤名（"步骤 1"）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing, spring } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, softShadow, tracking } from '@dg/polish';

export const PROCESS_FLOW_DURATION = 176;

const W = 1920;
const H = 1080;
const CX = 860;          // 图心偏左，右侧留给图例
const CY = 548;
const RING_R = 318;      // 节点分布半径

// 节点：绕一圈。angle 用弧度（-π/2 = 正上方，顺时针）。
// 以水循环为模子——换选题改这一张表，机制不动。
const NODES = [
  { key: 'evap', label: '蒸发', angle: -Math.PI / 2, sub: '海面 → 水汽' },
  { key: 'cond', label: '凝结', angle: -Math.PI / 2 + (Math.PI * 2) / 4, sub: '水汽 → 云' },
  { key: 'prec', label: '降水', angle: -Math.PI / 2 + (Math.PI * 2 * 2) / 4, sub: '云 → 地面' },
  { key: 'flow', label: '径流', angle: -Math.PI / 2 + (Math.PI * 2 * 3) / 4, sub: '地面 → 海' },
];
const N = NODES.length;

// 每段箭头：从节点 i 到节点 i+1（绕圈）。用两段贝塞尔拟合出一条"外凸"的弧。
const nodePos = (i: number): [number, number] => [
  CX + Math.cos(NODES[i].angle) * RING_R,
  CY + Math.sin(NODES[i].angle) * RING_R,
];

// 弧线控制点：在两节点连线的外侧（远离圆心）推出 0.42×弦长
const arcPath = (i: number): string => {
  const [x0, y0] = nodePos(i);
  const [x1, y1] = nodePos((i + 1) % N);
  const mx = (x0 + x1) / 2;
  const my = (y0 + y1) / 2;
  // 从中点沿"远离圆心"方向推
  const dx = mx - CX;
  const dy = my - CY;
  const len = Math.hypot(dx, dy) || 1;
  const push = 0.46 * Math.hypot(x1 - x0, y1 - y0);
  const cx = mx + (dx / len) * push;
  const cy = my + (dy / len) * push;
  return `M${x0.toFixed(2)},${y0.toFixed(2)} Q${cx.toFixed(2)},${cy.toFixed(2)} ${x1.toFixed(2)},${y1.toFixed(2)}`;
};

// 二次贝塞尔取点（令牌行进用）
const bez = (i: number, t: number): [number, number] => {
  const [x0, y0] = nodePos(i);
  const [x1, y1] = nodePos((i + 1) % N);
  const mx = (x0 + x1) / 2;
  const my = (y0 + y1) / 2;
  const dx = mx - CX;
  const dy = my - CY;
  const len = Math.hypot(dx, dy) || 1;
  const push = 0.46 * Math.hypot(x1 - x0, y1 - y0);
  const cx = mx + (dx / len) * push;
  const cy = my + (dy / len) * push;
  const u = 1 - t;
  return [
    u * u * x0 + 2 * u * t * cx + t * t * x1,
    u * u * y0 + 2 * u * t * cy + t * t * y1,
  ];
};

// ── 时刻表 ──────────────────────────────────────────────────────────────────
const LAND = 18;              // 节点落位时长
const SEG_DRAW = 26;          // 每段箭头画多久
const SEG_GAP = 10;           // 段间隔
const segStart = (i: number): number => 24 + i * (SEG_DRAW + SEG_GAP);
const FIRST_LAP_END = segStart(N - 1) + SEG_DRAW;   // 第一圈画完
const LAP_DUR = 64;           // 令牌走一圈（第二圈起放慢到 0.7×）

// 令牌在"第 lap 圈、进度 p"的世界坐标
const tokenAt = (lap: number, p: number): [number, number] => {
  const seg = p * N;
  const i = Math.min(N - 1, Math.floor(seg));
  return bez(i, seg - i);
};

const Node: React.FC<{ i: number; frame: number; active: number }> = ({ i, frame, active }) => {
  const nd = NODES[i];
  const [x, y] = nodePos(i);
  const land = interpolate(frame, [0, LAND], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  // 该节点作为"某段终点"被点亮：第 i-1 段画到 70% 时
  const litAt = segStart((i - 1 + N) % N) + SEG_DRAW * 0.7;
  const lit = interpolate(frame, [litAt, litAt + 8], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const pop = spring({
    frame: frame - litAt,
    fps: 30,
    config: { damping: 13, stiffness: 200, mass: 0.8 },
    durationInFrames: 20,
  });
  const scale = (0.94 + 0.06 * pop) * (0.9 + 0.1 * land);
  return (
    <g transform={`translate(${x.toFixed(2)} ${y.toFixed(2)}) scale(${scale.toFixed(4)})`}>
      {/* 外圈：点亮时出现 */}
      <circle
        r={96}
        fill="none"
        stroke={G.accent}
        strokeWidth={2}
        opacity={lit * 0.5}
      />
      {/* 主体 */}
      <circle
        r={78}
        fill={lit > 0.5 ? G.accent : '#ffffff'}
        stroke={lit > 0.5 ? G.accent : G.hairlineStrong}
        strokeWidth={2}
        style={{ filter: 'drop-shadow(0 3px 10px rgba(20,22,28,0.10))' }}
      />
      <text
        y={12}
        textAnchor="middle"
        fontFamily={FONT.sans}
        fontSize={38}
        fontWeight={700}
        letterSpacing={tracking(38)}
        fill={lit > 0.5 ? '#ffffff' : G.ink1}
      >
        {nd.label}
      </text>
      {/* 副标：节点下方 */}
      <text
        y={126}
        textAnchor="middle"
        fontFamily={FONT.sans}
        fontSize={24}
        fill={G.ink3}
      >
        {nd.sub}
      </text>
      {/* 序号小点：左上角，圈序 */}
      <circle cx={-64} cy={-64} r={17} fill={lit > 0.5 ? G.accent : G.fill2} />
      <text
        x={-64}
        y={-57}
        textAnchor="middle"
        fontFamily={FONT.mono}
        fontSize={19}
        fontWeight={600}
        fill={lit > 0.5 ? '#ffffff' : G.ink2}
      >
        {i + 1}
      </text>
      {active > 0 && (
        <circle r={96 + active * 26} fill="none" stroke={G.accent} strokeWidth={3} opacity={1 - active} />
      )}
    </g>
  );
};

const Segment: React.FC<{ i: number; frame: number }> = ({ i, frame }) => {
  const s = segStart(i);
  const p = interpolate(frame, [s, s + SEG_DRAW], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.quad),
  });
  if (p <= 0) return null;
  // 用 dasharray 让弧"画出来"：总长近似为弦长 × 1.25
  const [x0, y0] = nodePos(i);
  const [x1, y1] = nodePos((i + 1) % N);
  const approx = Math.hypot(x1 - x0, y1 - y0) * 1.3;
  const active = p >= 1 ? 0 : 0;
  return (
    <g>
      <path
        d={arcPath(i)}
        fill="none"
        stroke={G.accent}
        strokeWidth={5}
        strokeLinecap="round"
        strokeDasharray={`${(approx * p).toFixed(2)} ${approx.toFixed(2)}`}
        opacity={0.9}
      />
      {/* 箭头三角：画到 85% 之后才出现，指向前方 */}
      {p > 0.85 && (
        <polygon
          points="0,-11 22,0 0,11"
          fill={G.accent}
          transform={`translate(${x1.toFixed(2)} ${y1.toFixed(2)}) rotate(${(Math.atan2(y1 - y0, x1 - x0) * 180) / Math.PI}) translate(-88 0)`}
        />
      )}
      {/* 段标签：画完后停在弧的中点外侧 */}
      {p >= 1 && (() => {
        const [lx, ly] = bez(i, 0.5);
        const dx = lx - CX;
        const dy = ly - CY;
        const l = Math.hypot(dx, dy) || 1;
        const tx = lx + (dx / l) * 54;
        const ty = ly + (dy / l) * 54;
        return (
          <g>
            <rect
              x={tx - 62}
              y={ty - 24}
              width={124}
              height={48}
              rx={10}
              fill="#ffffff"
              stroke={G.hairline}
              style={{ filter: 'drop-shadow(0 2px 6px rgba(20,22,28,0.08))' }}
            />
            <text
              x={tx}
              y={ty + 8}
              textAnchor="middle"
              fontFamily={FONT.sans}
              fontSize={25}
              fontWeight={600}
              letterSpacing={tracking(25)}
              fill={G.ink1}
            >
              {NODES[(i + 1) % N].sub.split(' → ')[0]}
            </text>
          </g>
        );
      })()}
    </g>
  );
};

export const ProcessDiagramFlow: React.FC = () => {
  const frame = useCurrentFrame();

  // 令牌：第一圈跟着画（走到刚画完的那段末端），画完后自由绕圈
  let token: [number, number] | null = null;
  let tokenOp = 0;
  if (frame >= segStart(0)) {
    if (frame < FIRST_LAP_END) {
      // 跟画阶段：找到当前正在画的那一段
      let seg = 0;
      for (let i = 0; i < N; i++) {
        if (frame >= segStart(i) && frame < segStart(i) + SEG_DRAW) seg = i;
      }
      const local = Math.min(1, (frame - segStart(seg)) / SEG_DRAW);
      token = bez(seg, local);
      tokenOp = 1;
    } else {
      const laps = frame - FIRST_LAP_END;
      const lapLen = LAP_DUR * (laps < LAP_DUR ? 1 : 0.7); // 第二圈起放慢
      const total = laps < LAP_DUR ? laps : LAP_DUR + (laps - LAP_DUR) * 0.7;
      const p = (total % lapLen) / lapLen;
      token = tokenAt(Math.floor(total / lapLen), p);
      tokenOp = 1;
    }
  }

  const headIn = interpolate(frame, [0, 16], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const centerIn = interpolate(frame, [FIRST_LAP_END, FIRST_LAP_END + 18], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const lapCount = frame >= FIRST_LAP_END
    ? 1 + Math.floor((frame - FIRST_LAP_END) / LAP_DUR)
    : 0;

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.45, y: 0.3 }} accent={G.accent} vignette={0.12} grain={0} />

      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        {/* 底层：一整圈虚线轮廓（"这会走一圈"的预告） */}
        <circle
          cx={CX}
          cy={CY}
          r={RING_R}
          fill="none"
          stroke={G.hairline}
          strokeWidth={2}
          strokeDasharray="6 10"
          opacity={0.8}
        />

        {/* 箭头：按次序画 */}
        {NODES.map((_, i) => (
          <Segment key={`seg-${NODES[i].key}`} i={i} frame={frame} />
        ))}

        {/* 节点 */}
        {NODES.map((_, i) => (
          <Node key={`nd-${NODES[i].key}`} i={i} frame={frame} active={0} />
        ))}

        {/* 令牌 */}
        {token && (
          <g opacity={tokenOp}>
            <circle cx={token[0]} cy={token[1]} r={22} fill={G.accent} opacity={0.16} />
            <circle cx={token[0]} cy={token[1]} r={10} fill={G.accent} />
          </g>
        )}

        {/* 中心：圈名 + 圈数 */}
        <text
          x={CX}
          y={CY - 10}
          textAnchor="middle"
          fontFamily={FONT.sans}
          fontSize={30}
          fontWeight={650}
          letterSpacing={tracking(30, true)}
          fill={G.accent}
          opacity={centerIn}
        >
          THE WATER CYCLE
        </text>
        <text
          x={CX}
          y={CY + 52}
          textAnchor="middle"
          fontFamily={FONT.sans}
          fontSize={54}
          fontWeight={720}
          letterSpacing={tracking(54)}
          fill={G.ink1}
          opacity={centerIn}
        >
          {lapCount === 0 ? '走一遍' : `第 ${lapCount} 圈`}
        </text>
      </svg>

      {/* 固定页眉 */}
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
          It never stops
        </div>
        <div style={{ marginTop: 8, fontSize: 62, fontWeight: 720, letterSpacing: tracking(62), color: G.ink1 }}>
          这一圈一直在转
        </div>
      </div>

      {/* 右侧图例：次序清单（补"箭头画得太快看不清"的那半边） */}
      <div style={{ position: 'absolute', right: 120, top: 300, width: 380 }}>
        <div
          style={{
            fontSize: 22,
            fontWeight: 650,
            letterSpacing: tracking(22, true),
            textTransform: 'uppercase',
            color: G.ink3,
            marginBottom: 18,
          }}
        >
          Order
        </div>
        {NODES.map((nd, i) => {
          const on = frame >= segStart((i - 1 + N) % N) + SEG_DRAW * 0.7;
          return (
            <div
              key={nd.key}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 14,
                padding: '11px 0',
                borderTop: `1px solid ${G.hairline}`,
                opacity: on ? 1 : 0.34,
                transition: 'none',
              }}
            >
              <div
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: 15,
                  background: on ? G.accent : G.fill2,
                  color: on ? '#fff' : G.ink2,
                  fontFamily: FONT.mono,
                  fontSize: 17,
                  fontWeight: 600,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flex: 'none',
                }}
              >
                {i + 1}
              </div>
              <div>
                <div style={{ fontSize: 27, fontWeight: 650, color: G.ink1, letterSpacing: tracking(27) }}>{nd.label}</div>
                <div style={{ fontSize: 21, color: G.ink3, marginTop: 1 }}>{nd.sub}</div>
              </div>
            </div>
          );
        })}
      </div>

      <Grain opacity={0.04} />
    </div>
  );
};
