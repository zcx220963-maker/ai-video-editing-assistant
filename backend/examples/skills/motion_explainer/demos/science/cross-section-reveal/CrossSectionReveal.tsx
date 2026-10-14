// cross-section-reveal —— 剖面揭示（科普向）
//
// 治的是什么：现有 124 张卡全是产品宣传片语汇（镜头/UI 入场/字体/转场），
// 讲"一个东西里面是什么"时一张都挑不出来，模型只能硬套 ui-entrance 的骨架——
// 表现出来就是"科普片做出了产品发布会的样子"。这一族补的是**剖面**。
//
// 机制（不是 opacity 定时器）：
//   一片剖切面自左向右扫过球体。每一层是一个同心圆环，**只有被扫到的部分显影**——
//   用一个随帧增长的 clipPath 矩形做裁剪，所以"切到哪里显到哪里"是几何上成立的。
//   层与层之间留 3f 的错峰，让"逐层揭开"读得出次序而不是一次性全亮。
//   标签带引线，在该层被扫过 60% 时画入；读数（半径/温度/厚度）用 tabular-nums。
//
// 约束（科普向的纪律，与产品片不同）：
//   · 不用 ShotcraftMark、不用产品截图、不用"功能卡"——画面里只有这一颗球和它的标签；
//   · 每层的数值必须是**这一帧算出来的**（半径 = 常量，但显影进度 = 帧的函数）；
//   · 收尾 ≥30f 真静止：剖面图尾漂读作"还没切完"。
//
// 调校起点：扫过全程 108f（3.6s），末 30f 静止，总长 150f（5s）。
import React from 'react';
import { useCurrentFrame, interpolate, Easing } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, innerHighlight, softShadow, tracking } from '@dg/polish';

export const CROSS_SECTION_DURATION = 150;

const W = 1920;
const H = 1080;
const CX = 820;          // 球心 x（右侧留给标签列）
const CY = 540;          // 球心 y
const R_OUTER = 400;     // 整体半径

// 层定义：从外到内。r = 该层外半径，c = 该层填充色，label = 标签，read = 读数。
// 数值是**示意**（以地球为模子）：换选题时改这一张表即可，机制不动。
const LAYERS = [
  { key: 'crust', r: 400, c: '#b9c4a8', label: '地壳', read: '5–70 km', note: '我们站在这里' },
  { key: 'mantle', r: 300, c: '#c98f5a', label: '地幔', read: '2 900 km', note: '缓慢对流，拖动板块' },
  { key: 'outer', r: 190, c: '#e07a4b', label: '外核', read: '4 000–5 000 °C', note: '液态铁，发电机就在这里' },
  { key: 'inner', r: 95, c: '#f2c14e', label: '内核', read: '5 400 °C', note: '固态，和太阳表面一样热' },
];

const SWEEP_START = 8;    // 剖切面起扫帧
const SWEEP_END = 116;    // 扫到最右
const STAGGER = 4;        // 层与层的错峰（帧）
const HOLD = 30;          // 收尾静止

// 剖切面 x：缓入急出（前段慢让第一层看清，末段快收）
const cutXAt = (f: number): number => {
  const t = interpolate(f, [SWEEP_START, SWEEP_END], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const eased = interpolate(t, [0, 0.18, 0.8, 1], [0, 0.07, 0.86, 1], {
    easing: Easing.inOut(Easing.quad),
  });
  return CX - R_OUTER + eased * (R_OUTER * 2);
};

// 第 i 层被"扫过"的进度 0→1（该层左缘被剖切面越过的比例）
const sweepOf = (f: number, i: number): number => {
  const left = CX - LAYERS[i].r;
  const right = CX + LAYERS[i].r;
  const x = cutXAt(f - i * STAGGER);
  return Math.max(0, Math.min(1, (x - left) / (right - left)));
};

const LayerRing: React.FC<{ i: number; frame: number }> = ({ i, frame }) => {
  const L = LAYERS[i];
  const p = sweepOf(frame, i);
  if (p <= 0) return null;
  // 层显影时轻微"鼓起"（1 → 1.03），给"被切开后露出来"一点体积感
  const pop = interpolate(p, [0, 0.35], [0.97, 1.0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  // 剖切面处的高光边（越靠近刀口越亮）
  const edge = Math.max(0, 1 - Math.abs(p - 0.5) * 2.4);
  return (
    <circle
      cx={CX}
      cy={CY}
      r={L.r * pop}
      fill={L.c}
      stroke={`rgba(23,24,28,${(0.16 + edge * 0.5).toFixed(3)})`}
      strokeWidth={1.5 + edge * 2}
    />
  );
};

const LayerLabel: React.FC<{ i: number; frame: number }> = ({ i, frame }) => {
  const L = LAYERS[i];
  const p = sweepOf(frame, i);
  // 该层被扫过 55% 才画标签——早于这个时刻，标签指的地方还是实体，读作标错了
  const at = 0.55;
  const shown = p >= at;
  const inT = interpolate(frame, [0, 1], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const draw = shown
    ? interpolate(p, [at, 1], [0, 1], {
        extrapolateLeft: 'clamp',
        extrapolateRight: 'clamp',
        easing: Easing.out(Easing.cubic),
      })
    : 0;
  if (!shown) return null;
  // 引线：从该层右缘出发，先水平出一段，再斜上/斜下到标签锚点
  const yFrom = CY - L.r * 0.55;
  const x0 = CX + L.r;
  const x1 = CX + R_OUTER + 90;
  const yTo = 250 + i * 168;
  const bend = (x0 + x1) / 2;
  const d = `M${x0.toFixed(1)},${yFrom.toFixed(1)} L${bend.toFixed(1)},${yFrom.toFixed(1)} L${bend.toFixed(1)},${yTo.toFixed(1)} L${(x1 - 26 * draw).toFixed(1)},${yTo.toFixed(1)}`;
  const hero = i === LAYERS.length - 1;
  return (
    <g opacity={draw}>
      {/* 锚点：层右缘一个小圆点，先于引线出现 */}
      <circle cx={x0} cy={yFrom} r={6 * draw} fill={hero ? G.accent : G.ink1} />
      <path
        d={d}
        fill="none"
        stroke={hero ? G.accent : G.hairlineStrong}
        strokeWidth={hero ? 2.5 : 1.5}
        strokeLinecap="round"
      />
      <text
        x={x1 + 6}
        y={yTo - 14}
        fontFamily={FONT.sans}
        fontSize={40}
        fontWeight={720}
        letterSpacing={tracking(40)}
        fill={hero ? G.accent : G.ink1}
      >
        {L.label}
      </text>
      <text
        x={x1 + 6}
        y={yTo + 26}
        fontFamily={FONT.mono}
        fontSize={26}
        fill={G.ink2}
      >
        {L.read}
      </text>
      <text
        x={x1 + 6}
        y={yTo + 60}
        fontFamily={FONT.sans}
        fontSize={23}
        fill={G.ink3}
      >
        {L.note}
      </text>
      {/* 标签行的强调短线 */}
      <rect
        x={x1 - 34}
        y={yTo - 30}
        width={4 * draw}
        height={64}
        rx={2}
        fill={hero ? G.accent : G.ink3}
      />
    </g>
  );
};

export const CrossSectionReveal: React.FC = () => {
  const frame = useCurrentFrame();
  const cutX = cutXAt(frame);

  // 剖切面本体：一根带光晕的竖线 + 顶端小刀旗
  const bladeOp = interpolate(frame, [0, 10], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  // 标题：切片开始后才出现（这一镜的主角是球，不是标题）
  const headIn = interpolate(frame, [SWEEP_START + 6, SWEEP_START + 26], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="light" light={{ x: 0.42, y: 0.3 }} accent={G.accent} vignette={0.14} grain={0} />

      {/* 整球轮廓：始终在（它是"里面有什么"的那个"什么"），未剖部分画成实体灰 */}
      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        <defs>
          {/* 显影裁剪：宽度随剖切面增长 */}
          <clipPath id="cs-reveal">
            <rect x={0} y={0} width={Math.max(0, cutX)} height={H} />
          </clipPath>
          <radialGradient id="cs-shade" cx="0.36" cy="0.3" r="0.85">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.5" />
            <stop offset="55%" stopColor="#ffffff" stopOpacity="0.06" />
            <stop offset="100%" stopColor="#000000" stopOpacity="0.2" />
          </radialGradient>
        </defs>

        {/* 未剖的实体：整球一个中性灰，扫过之后被下层替换 */}
        <circle cx={CX} cy={CY} r={R_OUTER} fill="#d8d8d4" stroke={G.hairlineStrong} strokeWidth={1.5} />

        {/* 被扫过的层（由 clipPath 裁） */}
        <g clipPath="url(#cs-reveal)">
          {LAYERS.map((_, i) => (
            <LayerRing key={LAYERS[i].key} i={i} frame={frame} />
          ))}
          {/* 球面明暗，压在层之上：让"这是一颗球"读得出来 */}
          <circle cx={CX} cy={CY} r={R_OUTER} fill="url(#cs-shade)" pointerEvents="none" />
        </g>

        {/* 层间分隔线（同样受裁剪） */}
        <g clipPath="url(#cs-reveal)">
          {LAYERS.slice(0, -1).map((L, i) => (
            <circle
              key={L.key}
              cx={CX}
              cy={CY}
              r={L.r}
              fill="none"
              stroke="rgba(23,24,28,0.14)"
              strokeWidth={1}
            />
          ))}
        </g>

        {/* 标签与引线（不受裁剪：标签在球外） */}
        {LAYERS.map((_, i) => (
          <LayerLabel key={`lb-${LAYERS[i].key}`} i={i} frame={frame} />
        ))}

        {/* 剖切面：刀光 */}
        <g opacity={bladeOp}>
          <line
            x1={cutX}
            y1={CY - R_OUTER - 70}
            x2={cutX}
            y2={CY + R_OUTER + 70}
            stroke="#ffffff"
            strokeWidth={7}
            strokeLinecap="round"
          />
          <line
            x1={cutX}
            y1={CY - R_OUTER - 70}
            x2={cutX}
            y2={CY + R_OUTER + 70}
            stroke={G.accent}
            strokeWidth={2}
            strokeLinecap="round"
          />
          {/* 顶端刀旗 */}
          <path
            d={`M${cutX.toFixed(1)},${(CY - R_OUTER - 70).toFixed(1)} l-15,-30 l30,0 z`}
            fill={G.accent}
          />
        </g>
      </svg>

      {/* 固定页眉：只有一句（科普向不摆功能导航） */}
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
          Inside the Earth
        </div>
        <div
          style={{
            marginTop: 8,
            fontSize: 62,
            fontWeight: 720,
            letterSpacing: tracking(62),
            color: G.ink1,
          }}
        >
          切开这一颗
        </div>
      </div>

      {/* 右下角：剖切进度读数（这一帧算出来的，不是写死的字符串） */}
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
          Cut
        </div>
        <div style={{ marginTop: 4, fontSize: 44, color: G.ink1, fontWeight: 600 }}>
          {Math.round(((cutX - (CX - R_OUTER)) / (R_OUTER * 2)) * 100)}%
        </div>
      </div>

      <Grain opacity={0.045} />
    </div>
  );
};
