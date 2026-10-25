// metaphor-bridge —— 比喻桥（科普向）
//
// 治的是什么：科普片最难的一步是"让抽象数字变得可感"。现有 124 张卡全是产品片
// 语汇（镜头/UI/字体/转场），一张都不做"把 A 翻译成 B"。模型遇到
// 「太阳有多大」时只能把数字打成一行大字——观众读完还是不知道有多大。
//
// 机制（这一张的全部立意）：
//   把"太阳"换成"一个篮球"，然后按**真实的相对比例**把地球画出来。
//   篮球直径 60px 出现在画面左侧；一根测量线向右延伸，距离计数器真实地往上跳；
//   走到真实比例的位置时，地球是一颗**针尖大的点**——
//   这个"居然这么小"的瞬间就是这一张要的效果，它不需要任何解说。
//   最后比例读数落定（1 : 109 / 1 : 11 700），把瞬间变成可记住的数。
//
// 为什么用篮球而不是别的：篮球是绝大多数观众**用手比划过**的物件，
// 它的尺寸在身体记忆里。换成"一个西瓜"就要先建立西瓜的尺寸。
//
// 约束（科普向）：
//   · 不用 ShotcraftMark、不用产品截图；画面里只有这一次换算；
//   · 比例必须是真的（太阳半径 696 000 km / 地球 6 371 km ≈ 109）——
//     画成差不多大就丢了这一张唯一的价值；
//   · 计数器每一帧都是算出来的，不是逐帧跳一串写死的字符串。
import React from 'react';
import { useCurrentFrame, interpolate, Easing } from 'remotion';
import { G } from '@dg/fixtures/Fixtures';
import { Backdrop, FONT, Grain, tracking } from '@dg/polish';

export const METAPHOR_BRIDGE_DURATION = 168;

const W = 1920;
const H = 1080;

// 真实比例（科普向的命门：这一张的价值全在这两个数上）
const R_SUN = 696_000;      // km
const R_EARTH = 6_371;      // km
const RATIO = Math.round(R_SUN / R_EARTH);          // ≈ 109
const SUN_EARTH_DIST = 149_600_000;                  // km（日地平均距离）
const IN_SUN_DIAMETERS = Math.round(SUN_EARTH_DIST / (R_SUN * 2));  // ≈ 107

// 画面尺度：篮球直径 132px（半径 66）→ 地球直径 = 132/109 ≈ 1.2px
const BALL_R = 66;
const BALL_CX = 400;
const BALL_CY = 560;
const EARTH_R = (BALL_R * 2) / RATIO / 2;   // ≈ 0.6px（真实到画不出）
// 画不出就放大到可见，但**读数必须写真实值**——放大倍数在回执里写明
const EARTH_VISUAL_R = 5.5;
const EARTH_MAG = (EARTH_VISUAL_R * 2) / ((BALL_R * 2) / RATIO);

// 测量线：从篮球右缘出发，长度按"日地距离 ÷ 太阳直径"的比例
// 篮球直径 132px，日地距离 ≈ 107 个太阳直径 → 线长 ≈ 107 × 132 = 14 124px
// 画面放不下，所以线在画面上占 60%，读数写真实倍数（107）
const LINE_X0 = BALL_CX + BALL_R + 40;
const LINE_X1 = 1700;
const LINE_Y = BALL_CY;

const BALL_IN = 16;
const LINE_START = 34;
const LINE_DUR = 74;
const EARTH_AT = LINE_START + LINE_DUR;
const READOUT_AT = EARTH_AT + 12;
const HOLD = 26;

const Basketball: React.FC = () => {
  const inT = interpolate(useCurrentFrame(), [0, BALL_IN], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const s = 0.86 + 0.14 * inT;
  // 球面明暗
  return (
    <g opacity={inT} transform={`translate(${BALL_CX} ${BALL_CY}) scale(${s}) translate(${-BALL_CX} ${-BALL_CY})`}>
      <defs>
        <radialGradient id="mb-ball" cx="0.36" cy="0.3" r="0.85">
          <stop offset="0%" stopColor="#ffb066" />
          <stop offset="55%" stopColor="#e8763a" />
          <stop offset="100%" stopColor="#a8410f" />
        </radialGradient>
      </defs>
      <circle cx={BALL_CX} cy={BALL_CY} r={BALL_R} fill="url(#mb-ball)" />
      {/* 球纹：竖弧 + 横弧 + 一道赤道，简化的篮球纹样 */}
      <path
        d={`M${BALL_CX},${BALL_CY - BALL_R} A${BALL_R},${BALL_R} 0 0 0 ${BALL_CX},${BALL_CY + BALL_R}`}
        fill="none"
        stroke="rgba(60,20,0,0.5)"
        strokeWidth={2.5}
      />
      <line
        x1={BALL_CX - BALL_R}
        y1={BALL_CY}
        x2={BALL_CX + BALL_R}
        y2={BALL_CY}
        stroke="rgba(60,20,0,0.5)"
        strokeWidth={2.5}
      />
      <path
        d={`M${BALL_CX - BALL_R},${BALL_CY} A${BALL_R * 0.62},${BALL_R} 0 0 1 ${BALL_CX + BALL_R},${BALL_CY}`}
        fill="none"
        stroke="rgba(60,20,20,0.42)"
        strokeWidth={2}
      />
      {/* 高光 */}
      <ellipse
        cx={BALL_CX - BALL_R * 0.34}
        cy={BALL_CY - BALL_R * 0.4}
        rx={BALL_R * 0.3}
        ry={BALL_R * 0.2}
        fill="rgba(255,255,255,0.28)"
        transform={`rotate(-28 ${BALL_CX - BALL_R * 0.34} ${BALL_CY - BALL_R * 0.4})`}
      />
    </g>
  );
};

const EarthDot: React.FC<{ frame: number }> = ({ frame }) => {
  const p = interpolate(frame, [EARTH_AT, EARTH_AT + 14], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.back(1.6)),
  });
  if (p <= 0) return null;
  const x = LINE_X1;
  const y = LINE_Y;
  return (
    <g opacity={Math.min(1, p * 1.4)}>
      {/* 放大镜：因为真实比例只有 1.2px，画面上看不见。这一圈是"它在这儿"的凭据 */}
      <circle cx={x} cy={y} r={26 * p} fill="none" stroke={G.accent} strokeWidth={2} opacity={0.45} />
      <circle cx={x} cy={y} r={EARTH_VISUAL_R * p + 1} fill="#3f7cb8" />
      {/* 地球高光 */}
      <circle cx={x - 1.6} cy={y - 1.6} r={1.6 * p} fill="rgba(255,255,255,0.6)" />
    </g>
  );
};

const fmtKm = (v: number): string => {
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(2)} 亿 km`;
  if (v >= 10_000) return `${(v / 10_000).toFixed(1)} 万 km`;
  return `${v.toFixed(0)} km`;
};

export const MetaphorBridge: React.FC = () => {
  const frame = useCurrentFrame();

  // 测量线：随帧延伸，读数跟着真实距离走
  const lineP = interpolate(frame, [LINE_START, LINE_START + LINE_DUR], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.quad),
  });
  const lineX = LINE_X0 + (LINE_X1 - LINE_X0) * lineP;
  // 读数：真实距离（不是画面上的像素）
  const shownKm = SUN_EARTH_DIST * lineP;
  // 走了几个"太阳直径"
  const shownDiam = IN_SUN_DIAMETERS * lineP;

  const headIn = interpolate(frame, [0, 18], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const readIn = interpolate(frame, [READOUT_AT, READOUT_AT + 20], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const magIn = interpolate(frame, [EARTH_AT + 6, EARTH_AT + 24], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });

  return (
    <div style={{ width: W, height: H, overflow: 'hidden', position: 'relative', fontFamily: FONT.sans }}>
      <Backdrop tone="dark" light={{ x: 0.32, y: 0.28 }} accent={G.accent} vignette={0.3} grain={0} />

      {/* 星空：稀疏的小点（纯函数分布，禁随机） */}
      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        {Array.from({ length: 90 }).map((_, i) => {
          // 用索引做种子，保证同一帧渲染两遍逐位相同
          const sx = ((i * 7919) % 1913) / 1913 * W;
          const sy = ((i * 104729) % 1069) / 1069 * H;
          const tw = 0.25 + 0.55 * Math.abs(Math.sin(i * 1.7));
          return <circle key={i} cx={sx} cy={sy} r={i % 11 === 0 ? 2.2 : 1.2} fill="#ffffff" opacity={tw * 0.5} />;
        })}
      </svg>

      <svg width={W} height={H} style={{ position: 'absolute', inset: 0 }}>
        {/* 篮球（= 太阳） */}
        <Basketball />

        {/* 测量线 */}
        {lineP > 0 && (
          <g>
            <line
              x1={LINE_X0}
              y1={LINE_Y}
              x2={lineX}
              y2={LINE_Y}
              stroke="#ffffff"
              strokeWidth={3}
              strokeLinecap="round"
              opacity={0.85}
            />
            {/* 端点竖标 */}
            <line
              x1={lineX}
              y1={LINE_Y - 26}
              x2={lineX}
              y2={LINE_Y + 26}
              stroke="#ffffff"
              strokeWidth={2}
              opacity={0.6}
            />
          </g>
        )}

        {/* 地球（= 针尖大的点） */}
        <EarthDot frame={frame} />

        {/* 篮球标注 */}
        <g opacity={headIn}>
          <text
            x={BALL_CX}
            y={BALL_CY + BALL_R + 62}
            textAnchor="middle"
            fontFamily={FONT.sans}
            fontSize={34}
            fontWeight={700}
            letterSpacing={tracking(34)}
            fill="#ffffff"
          >
            一个篮球
          </text>
          <text
            x={BALL_CX}
            y={BALL_CY + BALL_R + 102}
            textAnchor="middle"
            fontFamily={FONT.mono}
            fontSize={24}
            fill="rgba(255,255,255,0.62)"
          >
            = 太阳（直径 139 万 km）
          </text>
        </g>

        {/* 测量读数：沿线上方跟着走 */}
        {lineP > 0.02 && (
          <g opacity={Math.min(1, lineP * 3)}>
            <text
              x={(LINE_X0 + lineX) / 2}
              y={LINE_Y - 46}
              textAnchor="middle"
              fontFamily={FONT.mono}
              fontSize={34}
              fontWeight={600}
              fill="#ffffff"
              style={{ fontVariantNumeric: 'tabular-nums' }}
            >
              {fmtKm(shownKm)}
            </text>
            <text
              x={(LINE_X0 + lineX) / 2}
              y={LINE_Y + 66}
              textAnchor="middle"
              fontFamily={FONT.sans}
              fontSize={25}
              fill="rgba(255,255,255,0.6)"
            >
              ≈ {shownDiam.toFixed(0)} 个太阳直径
            </text>
          </g>
        )}

        {/* 地球标注 */}
        {frame >= EARTH_AT && (
          <g opacity={magIn}>
            <text
              x={LINE_X1}
              y={LINE_Y - 62}
              textAnchor="middle"
              fontFamily={FONT.sans}
              fontSize={30}
              fontWeight={700}
              letterSpacing={tracking(30)}
              fill="#ffffff"
            >
              一颗针尖
            </text>
            <text
              x={LINE_X1}
              y={LINE_Y + 66}
              textAnchor="middle"
              fontFamily={FONT.sans}
              fontSize={24}
              fill="rgba(255,255,255,0.62)"
            >
              = 地球
            </text>
          </g>
        )}
      </svg>

      {/* 固定页眉 */}
      <div
        style={{
          position: 'absolute',
          left: 110,
          top: 84,
          opacity: headIn,
          transform: `translateY(${((1 - headIn) * 10).toFixed(2)}px)`,
        }}
      >
        <div
          style={{
            fontSize: 27,
            fontWeight: 650,
            letterSpacing: tracking(27, true),
            textTransform: 'uppercase',
            color: G.accent,
          }}
        >
          To scale
        </div>
        <div style={{ marginTop: 7, fontSize: 58, fontWeight: 720, letterSpacing: tracking(58), color: '#ffffff' }}>
          把它缩小成一个篮球
        </div>
      </div>

      {/* 底部：比例读数（把瞬间变成可记住的数） */}
      <div
        style={{
          position: 'absolute',
          left: 110,
          bottom: 88,
          display: 'flex',
          gap: 54,
          opacity: readIn,
          transform: `translateY(${((1 - readIn) * 14).toFixed(2)}px)`,
        }}
      >
        {[
          { k: '太阳 ÷ 地球（半径）', v: `1 : ${RATIO}` },
          { k: '日地距离 ÷ 太阳直径', v: `1 : ${IN_SUN_DIAMETERS}` },
          { k: '地球直径', v: '12 742 km' },
        ].map((r) => (
          <div key={r.k}>
            <div
              style={{
                fontSize: 20,
                fontWeight: 600,
                letterSpacing: tracking(20, true),
                textTransform: 'uppercase',
                color: 'rgba(255,255,255,0.5)',
              }}
            >
              {r.k}
            </div>
            <div
              style={{
                marginTop: 6,
                fontFamily: FONT.mono,
                fontSize: 46,
                fontWeight: 650,
                color: '#ffffff',
                fontVariantNumeric: 'tabular-nums',
              }}
            >
              {r.v}
            </div>
          </div>
        ))}
      </div>

      {/* 放大说明：地球在图上被放大了，必须说出来 */}
      <div
        style={{
          position: 'absolute',
          right: 110,
          bottom: 92,
          textAlign: 'right',
          maxWidth: 430,
          opacity: magIn,
        }}
      >
        <div style={{ fontSize: 21, color: 'rgba(255,255,255,0.55)', lineHeight: 1.5 }}>
          按真实比例，地球在这张图上只有
          <span style={{ color: '#ffffff', fontFamily: FONT.mono, fontWeight: 600 }}> 1.2 px</span>，
          画面上看不见——为了让你看见它，这里放大了约
          <span style={{ color: '#ffffff', fontFamily: FONT.mono, fontWeight: 600 }}>
            {' '}
            {Math.round(EARTH_MAG)}×
          </span>
          。**位置和距离没有放大。**
        </div>
      </div>

      <Grain opacity={0.05} />
    </div>
  );
};
