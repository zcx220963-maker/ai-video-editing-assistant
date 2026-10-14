// compare-duel —— 对比对擂（科普向）
//
// 治的是什么：科普片里"这两样东西有什么区别"是最高频的句子之一——
// 有性 vs 无性、内燃机 vs 电动机、近视 vs 远视、燃烧 vs 呼吸。
// 现有 128 张卡全是产品宣传片语汇，讲"区别"时模型只能把两栏并排放，
// 各自淡入——观众要自己在两栏之间来回找对应项，读作"两张名片"而不是"一场对擂"。
// 这一族补的是**对位**：每一维先亮一侧，再亮另一侧，读数真的对得上。
//
// 机制（不是两栏各自淡入）：
//   ① 三条对比轨，每轨一维（如 效率 / 排放 / 响应）。一条轨 = 左右两个读数块
//      + 中间一条"对位线"；
//   ② 逐轨亮起（错峰 14f）：左侧读数块先 pop（spring 过冲），紧接着右侧读数块
//      沿对位线滑入咬合——"这一维两边的读数现在对上了"是看得见的；
//   ③ 每轨亮起时，该轨的"优势标记"从胜出一侧弹出（不是提前写在版面上）；
//   ④ 三轨之后底部总评轨亮起：三个优势标记同帧归位到总评栏，逐条报出
//      "这一维谁赢、赢多少"——结论是数出来的，不是一句话贴上去的。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这张对擂表和它的读数；
//   · 每个读数必须是**这一维的真实数值**（示意模子：内燃机 vs 电动机），
//     换选题改 ROWS 一张表，机制不动；
//   · 两侧读数用 tabular-nums 等宽，逐位对位时才读得出"90% 对 30%"；
//   · 收尾 ≥28f 真静止：对擂表尾漂读作"还没判完"。
//
// 调校起点：三轨各 46f（错峰 14f）+ 总评 30f + 收尾静止 30f，总长 208f（约 7s）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing, spring } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, tracking } from '@dg/polish';

export const COMPARE_DUEL_DURATION = 208;

const W = 1920;
const H = 1080;

// 对擂双方：以"内燃机 vs 电动机"为模子。换选题改这三张表，机制不动。
const LEFT = { key: 'ice', name: '内燃机', sub: '烧油', color: '#c96f4a' };
const RIGHT = { key: 'ev', name: '电动机', sub: '用电', color: '#3d7ea6' };

// 三条对比轨：d = 维度，lv = 左侧读数，rv = 右侧读数，win = 这一维谁赢，
// delta = 赢的那一句（"多 3 倍"这种真实倍数差）。
// 数值是**示意**（工程近似量级）：换选题必须回填真实数值。
const ROWS = [
  { d: '效率', lv: '30%', rv: '90%', win: 'right', delta: '多 3 倍' },
  { d: '排放', lv: '有 CO₂', rv: '零直接', win: 'right', delta: '没有尾气' },
  { d: '响应', lv: '0.4 s', rv: '0.05 s', win: 'right', delta: '快 8 倍' },
];

// ── 时刻表 ──────────────────────────────────────────────────────────────────
const ROW_GAP = 60;          // 轨间距
const ROW_H = 148;           // 单轨高度
const TOP = 300;             // 第一条轨的顶 y
const rowY = (i: number): number => TOP + i * (ROW_H + ROW_GAP);
const POP_IN = 22;           // 单侧读数块 pop 时长
const SLIDE = 18;            // 右侧读数块滑入咬合时长
const STAGGER = 14;          // 轨与轨错峰
const rowStart = (i: number): number => 26 + i * STAGGER;
const leftLitAt = (i: number): number => rowStart(i);
const rightLitAt = (i: number): number => leftLitAt(i) + POP_IN + 6;
const winAt = (i: number): number => rightLitAt(i) + SLIDE + 8;
const SUM_AT = rowStart(ROWS.length - 1) + POP_IN + SLIDE + 26;   // 总评轨亮起
const HOLD = 30;

// 读数块 pop：scale 0.82→1.04→1（spring）
const popScale = (frame: number, at: number): number => {
  const s = spring({
    frame: frame - at,
    fps: 30,
    config: { damping: 12, stiffness: 220, mass: 0.7 },
    durationInFrames: POP_IN,
  });
  return 0.82 + 0.22 * s;
};

const Row: React.FC<{ i: number; frame: number }> = ({ i, frame }) => {
  const R = ROWS[i];
  const y = rowY(i);
  const lAt = leftLitAt(i);
  const rAt = rightLitAt(i);
  const wAt = winAt(i);
  const lPop = frame >= lAt ? popScale(frame, lAt) : 0.82;
  const rPop = frame >= rAt ? popScale(frame, rAt) : 0.82;
  // 右侧读数块沿对位线滑入：从中央线左侧 40px 滑到自己的位置
  const rSlide = frame >= rAt
    ? interpolate(frame, [rAt, rAt + SLIDE], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.out(Easing.cubic),
      })
    : 0;
  const wIn = frame >= wAt
    ? interpolate(frame, [wAt, wAt + 14], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.out(Easing.back(2)),
      })
    : 0;
  const winSide = R.win === 'right' ? RIGHT : LEFT;
  const winColor = R.win === 'right' ? RIGHT.color : LEFT.color;

  return (
    <g>
      {/* 轨底：一条横贯的浅槽（"这是一维"） */}
      <rect
        x={220}
        y={y}
        width={W - 440}
        height={ROW_H}
        rx={18}
        fill="#ffffff"
        stroke={G.hairline}
        style={{ filter: 'drop-shadow(0 3px 10px rgba(20,22,28,0.06))' }}
      />
      {/* 维度名：轨左端 */}
      <text
        x={264}
        y={y + ROW_H / 2 + 10}
        fontFamily={FONT.sans}
        fontSize={36}
        fontWeight={700}
        letterSpacing={tracking(36)}
        fill={G.ink1}
      >
        {R.d}
      </text>

      {/* 左侧读数块 */}
      <g transform={`translate(470 ${y + ROW_H / 2}) scale(${lPop.toFixed(4)})`} opacity={frame >= lAt ? 1 : 0}>
        <rect x={-150} y={-54} width={300} height={108} rx={16} fill={G.fill2} stroke={LEFT.color} strokeWidth={1.5} />
        <text x={-150} y={-14} textAnchor="middle" fontFamily={FONT.mono} fontSize={30} fontWeight={650} fill={LEFT.color} style={{ fontVariantNumeric: 'tabular-nums' }}>
          {R.lv}
        </text>
        <text x={-150} y={30} textAnchor="middle" fontFamily={FONT.sans} fontSize={21} fill={G.ink3}>
          {LEFT.name}
        </text>
      </g>

      {/* 对位线：左侧亮起后才画出 */}
      <line
        x1={640}
        y1={y + ROW_H / 2}
        x2={W - 640 - 40 * (1 - rSlide)}
        y2={y + ROW_H / 2}
        stroke={G.hairlineStrong}
        strokeWidth={2}
        strokeDasharray="5 7"
        opacity={frame >= lAt ? 0.9 : 0}
      />

      {/* 右侧读数块：滑入咬合 */}
      <g transform={`translate(${(W - 470 - 40 * (1 - rSlide)).toFixed(1)} ${y + ROW_H / 2}) scale(${rPop.toFixed(4)})`} opacity={frame >= rAt ? 1 : 0}>
        <rect x={-150} y={-54} width={300} height={108} rx={16} fill={G.fill2} stroke={RIGHT.color} strokeWidth={1.5} />
        <text x={150} y={-14} textAnchor="middle" fontFamily={FONT.mono} fontSize={30} fontWeight={650} fill={RIGHT.color} style={{ fontVariantNumeric: 'tabular-nums' }}>
          {R.rv}
        </text>
        <text x={150} y={30} textAnchor="middle" fontFamily={FONT.sans} fontSize={21} fill={G.ink3}>
          {RIGHT.name}
        </text>
      </g>

      {/* 优势标记：胜出一侧弹出（不提前写在版面上） */}
      {wIn > 0 && (
        <g transform={`translate(${R.win === 'right' ? W - 470 : 470} ${y + ROW_H / 2})`} opacity={wIn}>
          <rect x={R.win === 'right' ? 120 : -240} y={-78} width={120} height={40} rx={20} fill={winColor} />
          <text x={R.win === 'right' ? 180 : -180} y={-50} textAnchor="middle" fontFamily={FONT.sans} fontSize={22} fontWeight={700} fill="#ffffff">
            {R.delta}
          </text>
          <polygon
            points={R.win === 'right' ? '-14,-6 14,0 -14,6' : '14,-6 -14,0 14,6'}
            fill={winColor}
            transform={`translate(${R.win === 'right' ? 126 : -126} ${y + ROW_H / 2 + 0})`}
          />
        </g>
      )}
    </g>
  );
};

export const CompareDuel: React.FC = () => {
  const frame = useCurrentFrame();
  const headIn = interpolate(frame, [0, 18], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const sumIn = frame >= SUM_AT
    ? interpolate(frame, [SUM_AT, SUM_AT + 22], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.out(Easing.cubic),
      })
    : 0;

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.5, y: 0.28 }} accent={G.accent} vignette={0.12} grain={0} />

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
          Head to head
        </div>
        <div style={{ marginTop: 8, fontSize: 62, fontWeight: 720, letterSpacing: tracking(62), color: G.ink1 }}>
          逐条对上
        </div>
      </div>

      {/* 双方名牌：顶栏左右 */}
      <div style={{ position: 'absolute', left: 470, top: 190, transform: 'translateX(-50%)', opacity: headIn, textAlign: 'center' }}>
        <div style={{ fontSize: 44, fontWeight: 720, color: LEFT.color, letterSpacing: tracking(44) }}>{LEFT.name}</div>
        <div style={{ fontSize: 22, color: G.ink3, marginTop: 2 }}>{LEFT.sub}</div>
      </div>
      <div style={{ position: 'absolute', left: W - 470, top: 190, transform: 'translateX(-50%)', opacity: headIn, textAlign: 'center' }}>
        <div style={{ fontSize: 44, fontWeight: 720, color: RIGHT.color, letterSpacing: tracking(44) }}>{RIGHT.name}</div>
        <div style={{ fontSize: 22, color: G.ink3, marginTop: 2 }}>{RIGHT.sub}</div>
      </div>

      {/* 对比轨 */}
      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        {ROWS.map((_, i) => (
          <Row key={ROWS[i].d} i={i} frame={frame} />
        ))}

        {/* 总评轨：三轨亮完后归位 */}
        {sumIn > 0 && (
          <g opacity={sumIn} transform={`translate(${W / 2} ${rowY(ROWS.length - 1) + ROW_H + ROW_GAP + 10})`}>
            <rect x={-460} y={-44} width={920} height={88} rx={20} fill={G.ink1} />
            <text x={0} y={10} textAnchor="middle" fontFamily={FONT.sans} fontSize={34} fontWeight={700} fill="#ffffff" letterSpacing={tracking(34)}>
              三条轨的结论：{RIGHT.name}在{' '}
              {ROWS.map((R) => (R.win === 'right' ? R.d : '')).filter(Boolean).join('、')}
              {' '}上占优
            </text>
          </g>
        )}
      </svg>

      <Grain opacity={0.04} />
    </div>
  );
};
