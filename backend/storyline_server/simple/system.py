# -*- coding: utf-8 -*-
"""设计系统：29 个可调组件，作为**字符串**拼进生成的模块里。

为什么不放进 `remotion/src/system/` 再 import：
`remotion/` 没有挂载，加文件必须重建镜像；而这条线的迭代速度取决于"改完就能渲"。
内联进同一个模块的另一个好处是**自包含**——lint 与渲染看到的是同一份东西，
不存在"组件在镜像里、调用在新文件里"这种半吊子状态。

代价：组件只能在这里改，Remotion Studio 里预览不到。可以接受——
它们的消费者是模型，不是人。

组件的统一契约：
    ({ t, ...props }) => JSX
`t` 是**这一镜的进度 0~1**（不是全片）。每个组件自己按 `at` 决定什么时候进场，
所以一镜里几个组件可以错开，天然形成"两拍"。

所有组件都**不写死尺寸**，一律按 `useVideoConfig()` 的 width/height 取比例，
这样竖屏/横屏/方图同一套代码都能用。
"""

from __future__ import annotations

import re

#: 组件源码。**不要 import**——它在模块里与场景共用同一个作用域。
SYSTEM_TSX = r"""
// ─────────────────────────────────────────────────────────────────────────────
// 设计系统（29 件）。约定：每个组件收 { t, ...props }，t 是这一镜的进度 0~1。
// 尺寸一律按 width/height 取比例，不写死像素。
// ─────────────────────────────────────────────────────────────────────────────

const INK = '#EAFBFF';
const MUTED = '#8FA3B8';

const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
const at = (t: number, start: number, dur = 0.28) => clamp01((t - start) / dur);
const easeOut = (x: number) => 1 - Math.pow(1 - x, 3);

/** 比例参数兜底。**这是"模型传错单位"的保险**：
 *  它会把 `y`（0~1 的比例）传成像素（`height * 0.72` = 1382），于是
 *  `height * (1 - y)` 变成 −265 万，那个 `<rect>` 直接不渲染、画面缺一块，
 *  而 lint 与渲染都返回正常——只有翻浏览器控制台才看得到。
 *  规则：是数且在 0~1 之间就用，否则退回默认值（**不猜单位**，猜错更糟）。 */
const R = (v: any, d = 0.5): number => {
  const n = typeof v === 'number' && isFinite(v) ? v : d;
  return n < 0 || n > 1 ? d : n;
};
/** 计数参数兜底：正数、取整、夹上限（模型会传 0 或 9999）。 */
const N = (v: any, d = 5, hi = 40): number => {
  const n = typeof v === 'number' && isFinite(v) ? Math.round(v) : d;
  return Math.max(1, Math.min(hi, n));
};
/** 缩放倍率兜底：夹在 0.7~1.4。**不能过 `R()`**——`R` 只认 0~1，
 *  而缩放倍率天然在 1 附近（1.0 不推、1.1 推近 10%），过 `R` 会被当成非法值退回默认。 */
const S = (v: any, d = 1): number => {
  const n = typeof v === 'number' && isFinite(v) ? v : d;
  return Math.max(0.7, Math.min(1.4, n));
};

/** 把一个颜色往白里提 k（0~1）。**这是"画面不发死黑"的保险**：
 *  模型经常把 `hue` 传成 `#0a0c12` 这类近黑值，直接用的话整帧就是一片黑。 */
const lift = (hex: string, k = 0.5): string => {
  const s = String(hex || '').replace('#', '');
  if (!/^[0-9a-fA-F]{3}$|^[0-9a-fA-F]{6}$/.test(s)) return hex;
  const n = s.length === 3 ? s.split('').map((c) => c + c).join('') : s;
  const r = parseInt(n.slice(0, 2), 16);
  const g = parseInt(n.slice(2, 4), 16);
  const b = parseInt(n.slice(4, 6), 16);
  const f = (v: number) => Math.round(v + (255 - v) * k);
  return `rgb(${f(r)},${f(g)},${f(b)})`;
};

/** 满幅背景：底色 + 顶部光晕 + 底部色带 + 暗角。**任何一镜都应该先铺它**，
 *  否则边缘会露黑边、整帧发死黑（实测：只画主体不铺底，抽帧出来一片黑）。 */
const Backdrop = ({ hue = '#22D3EE', glow = 0.8, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const pulse = 0.88 + 0.12 * Math.sin(t * Math.PI * 2);
  const g = R(glow, 0.8);
  const lit = lift(hue, 0.42);
  const deep = lift(hue, 0.12);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <defs>
        <radialGradient id="bd" cx="50%" cy="16%" r="78%">
          <stop offset="0%" stopColor={lit} stopOpacity={0.62 * g * pulse} />
          <stop offset="45%" stopColor={deep} stopOpacity={0.30} />
          <stop offset="100%" stopColor="#0a0c12" stopOpacity={1} />
        </radialGradient>
        <linearGradient id="bd2" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor={lit} stopOpacity={0.26} />
          <stop offset="60%" stopColor={lit} stopOpacity={0} />
        </linearGradient>
      </defs>
      <rect x={0} y={0} width={width} height={height} fill="#0a0c12" />
      <rect x={0} y={0} width={width} height={height} fill="url(#bd)" />
      <rect x={0} y={0} width={width} height={height} fill="url(#bd2)" />
    </svg>
  );
};

/** 逐行字幕：从下往上滑入，可给一个词上色。位置按画面比例给。
 *  `size` 是**占画面宽度的比例**，内部夹在 0.03~0.14——模型会传 0.72 这种值（把比例
 *  当成 0~1 的任意刻度），不夹的话一个字就顶满整屏、还被裁掉（实测踩过）。 */
const Caption = ({ text = '', t = 1, start = 0.05, size = 0.072, color = INK,
                   x = 0.5, y = 0.84, align = 'center', weight = 700 }: any) => {
  const { width } = useVideoConfig();
  const p = easeOut(at(t, start, 0.3));
  const cx = R(x, 0.5);
  const cy = R(y, 0.84);
  const fs = width * Math.min(0.14, Math.max(0.03, R(size, 0.072)));
  if (!text) return null;
  return (
    <div style={{
      position: 'absolute', left: width * (cx - 0.44), width: width * 0.88,
      top: `calc(${cy * 100}% - ${fs * 0.6}px)`,
      textAlign: align, fontFamily: TOKENS.fontStack, fontWeight: weight,
      fontSize: fs, lineHeight: 1.28, color,
      opacity: p, transform: `translateY(${(1 - p) * 26}px)`,
      textShadow: '0 6px 28px rgba(0,0,0,0.85)',
    }}>{text}</div>
  );
};

/** 章节号 + 标题：大号数字打底，标题逐字拉开。用来切段落。 */
const ChapterTitle = ({ index = '01', title = '', t = 1, hue = '#FF6B2C' }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, 0.02, 0.34));
  const q = easeOut(at(t, 0.16, 0.34));
  return (
    <>
      <div style={{
        position: 'absolute', left: width * 0.1, top: height * 0.34,
        fontFamily: TOKENS.fontStack, fontSize: width * 0.30, fontWeight: 800,
        color: hue, opacity: 0.20 * p, lineHeight: 1,
      }}>{index}</div>
      <div style={{
        position: 'absolute', left: width * 0.1, top: height * 0.52, width: width * 0.8,
        fontFamily: TOKENS.fontStack, fontSize: width * 0.085, fontWeight: 700,
        color: INK, letterSpacing: `${(1 - q) * 14}px`, opacity: q,
      }}>{title}</div>
      <div style={{
        position: 'absolute', left: width * 0.1, top: height * 0.615,
        width: Math.max(0, width * 0.22 * p), height: 4,
        backgroundImage: `linear-gradient(${hue}, ${hue})`,
      }} />
    </>
  );
};

/** 数据卡：一个数字 + 一行说明，从下方弹入。数字可以带单位。 */
const StatCard = ({ value = '', label = '', t = 1, start = 0.1, hue = '#22D3EE',
                    x = 0.5, y = 0.42 }: any) => {
  const { width } = useVideoConfig();
  const p = easeOut(at(t, start, 0.3));
  const cardW = width * 0.62;
  const cx = R(x, 0.5);
  const cy = R(y, 0.42);
  return (
    <div style={{
      position: 'absolute', left: width * cx - cardW / 2, top: `${cy * 100}%`,
      width: cardW, padding: width * 0.05,
      backgroundImage: 'linear-gradient(rgba(12,18,26,0.82), rgba(12,18,26,0.82))',
      border: `2px solid ${hue}`, borderRadius: width * 0.028,
      opacity: p, transform: `translateY(${(1 - p) * 40}px) scale(${0.94 + 0.06 * p})`,
      fontFamily: TOKENS.fontStack, textAlign: 'center',
      boxShadow: `0 18px 60px ${hue}44`,
    }}>
      <div style={{ fontSize: width * 0.13, fontWeight: 800, color: hue, lineHeight: 1.1 }}>{value}</div>
      <div style={{ fontSize: width * 0.036, color: MUTED, marginTop: width * 0.018 }}>{label}</div>
    </div>
  );
};

/** 柱状图：一排柱子按顺序长出来，可标数值。 */
const BarChart = ({ values = [0.4, 0.7, 0.5], labels = [], t = 1, start = 0.08,
                    hue = '#FF6B2C', x = 0.5, y = 0.62, w = 0.78, h = 0.3 }: any) => {
  const { width, height } = useVideoConfig();
  const vals: number[] = Array.isArray(values) && values.length ? values : [0.4, 0.7, 0.5];
  const n = Math.max(1, vals.length);
  const cx = R(x, 0.5);
  const cy = R(y, 0.62);
  const cw = R(w, 0.78);
  const chh = R(h, 0.3);
  const gap = (width * cw) / n;
  const barW = gap * 0.56;
  const maxV = Math.max(0.0001, ...vals.map((v: number) => Math.abs(Number(v) || 0)));
  const baseY = height * (cy + chh);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <line x1={width * (cx - cw / 2)} y1={baseY} x2={width * (cx + cw / 2)} y2={baseY}
            stroke={MUTED} strokeWidth={2} opacity={0.5} />
      {vals.map((v: number, i: number) => {
        const p = easeOut(at(t, start + i * 0.06, 0.26));
        const bh = Math.max(0, (Math.abs(Number(v) || 0) / maxV) * height * chh * p);
        const bx = width * (cx - cw / 2) + gap * i + (gap - barW) / 2;
        return (
          <g key={i}>
            <rect x={bx} y={baseY - bh} width={barW} height={bh} rx={6}
                  fill={i % 2 === 0 ? hue : '#FFC93C'} opacity={0.92} />
            {labels[i] ? (
              <text x={bx + barW / 2} y={baseY + 34} textAnchor="middle"
                    fontFamily={TOKENS.fontStack} fontSize={width * 0.028} fill={MUTED}>
                {labels[i]}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
};

/** 环绕图：中心一个主体，外圈若干点按轨道转。讲"围绕/层级/轨道"用它。 */
const OrbitDiagram = ({ count = 5, t = 1, start = 0.06, hue = '#22D3EE',
                        x = 0.5, y = 0.46, r = 0.22 }: any) => {
  const { width, height } = useVideoConfig();
  const cnt = N(count, 5, 16);
  const cx = width * R(x, 0.5);
  const cy = height * R(y, 0.46);
  const orbR = width * R(r, 0.22);
  const p = easeOut(at(t, start, 0.3));
  const spin = t * Math.PI * 1.6;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <circle cx={cx} cy={cy} r={orbR} fill="none" stroke={hue} strokeWidth={2}
              opacity={0.28 * p} strokeDasharray="10 12" />
      <circle cx={cx} cy={cy} r={width * 0.075} fill={hue} opacity={0.95 * p} />
      <circle cx={cx} cy={cy} r={width * 0.115} fill={hue} opacity={0.18 * p} />
      {Array.from({ length: cnt }).map((_, i) => {
        const a = spin + (i * Math.PI * 2) / cnt;
        const q = easeOut(at(t, start + i * 0.05, 0.3));
        return (
          <circle key={i} cx={cx + Math.cos(a) * orbR} cy={cy + Math.sin(a) * orbR}
                  r={width * 0.018} fill="#FFC93C" opacity={0.9 * q} />
        );
      })}
    </svg>
  );
};

/** 折线/路径生长：给一串点，连线按进度画出来。讲"趋势/路径/传导"用它。 */
const LineTrace = ({ points = [[0.1, 0.7], [0.4, 0.4], [0.7, 0.55], [0.9, 0.2]],
                     t = 1, start = 0.06, hue = '#34D399', width: sw = 6 }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.42));
  const pts0: number[][] = (Array.isArray(points) ? points : [])
    .filter((pt: any) => Array.isArray(pt) && pt.length >= 2)
    .map((pt: number[]) => [R(pt[0], 0.5), R(pt[1], 0.5)]);
  const pts2 = pts0.length >= 2 ? pts0 : [[0.1, 0.7], [0.4, 0.4], [0.7, 0.55], [0.9, 0.2]];
  const pts = pts2.map((pt: number[]) => `${width * pt[0]},${height * pt[1]}`).join(' ');
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <polyline points={pts} fill="none" stroke={hue} strokeWidth={sw}
                strokeLinecap="round" strokeLinejoin="round"
                pathLength={1} strokeDasharray={1} strokeDashoffset={1 - p} />
      {pts2.map((pt: number[], i: number) => {
        const q = easeOut(at(t, start + (i / pts2.length) * 0.4, 0.2));
        return <circle key={i} cx={width * pt[0]} cy={height * pt[1]}
                       r={sw * 1.6 * q} fill="#FFC93C" opacity={q} />;
      })}
    </svg>
  );
};

/** 光球：一团会呼吸的辉光。当"能量/太阳/焦点"用，也用来给空画面加密度。 */
const GlowOrb = ({ t = 1, start = 0, hue = '#FF6B2C', x = 0.5, y = 0.42, r = 0.18 }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.34));
  const breathe = 1 + 0.06 * Math.sin(t * Math.PI * 3);
  const ox = R(x, 0.5);
  const oy = R(y, 0.42);
  const orbR = width * R(r, 0.18) * p * breathe;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <defs>
        <radialGradient id="og">
          <stop offset="0%" stopColor={hue} stopOpacity={0.95} />
          <stop offset="45%" stopColor={hue} stopOpacity={0.35} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </radialGradient>
      </defs>
      <circle cx={width * ox} cy={height * oy} r={orbR * 2.1} fill="url(#og)" />
      <circle cx={width * ox} cy={height * oy} r={orbR} fill={hue} opacity={0.92} />
    </svg>
  );
};

/** 扫描网格：透视地面网格 + 一条扫描线。科技感的底子。 */
const ScanGrid = ({ t = 1, start = 0, hue = '#22D3EE', y = 0.68 }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.36));
  const gy = R(y, 0.68);
  const rows = 9;
  const lineY = height * (gy + ((t * 1.4) % 1) * (1 - gy));
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <rect x={0} y={height * gy} width={width} height={height * (1 - gy)}
            fill="#0d141d" opacity={0.9 * p} />
      {Array.from({ length: rows }).map((_, i) => {
        const ry = height * gy + (height * (1 - gy) * i) / rows;
        return <line key={`h${i}`} x1={0} y1={ry} x2={width} y2={ry}
                     stroke={hue} strokeWidth={1} opacity={0.16 * p} />;
      })}
      {Array.from({ length: 9 }).map((_, i) => (
        <line key={`v${i}`} x1={width * (i / 8)} y1={height * gy}
              x2={width * (0.5 + (i / 8 - 0.5) * 2.6)} y2={height}
              stroke={hue} strokeWidth={1} opacity={0.14 * p} />
      ))}
      <line x1={0} y1={lineY} x2={width} y2={lineY} stroke={hue}
            strokeWidth={3} opacity={0.55 * p} />
    </svg>
  );
};

/** 粒子场：确定性撒点，按 t 缓慢上浮。给"物质/尘埃/数据流"当底。 */
const Particles = ({ count = 90, t = 1, start = 0, hue = '#22D3EE', seed = 7 }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.3));
  const cnt = N(count, 90, 200);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      {Array.from({ length: cnt }).map((_, i) => {
        const rx = random(`x${seed}-${i}`);
        const ry = random(`y${seed}-${i}`);
        const rr = 1.5 + random(`r${seed}-${i}`) * 3.5;
        const drift = ((ry - t * 0.16) % 1 + 1) % 1;
        return <circle key={i} cx={rx * width} cy={drift * height} r={rr}
                       fill={i % 3 === 0 ? '#FFC93C' : hue} opacity={0.5 * p} />;
      })}
    </svg>
  );
};

/** 斜色带：一条高饱和的斜向色块，占画面 20~28%。
 *  **这是"画面有颜色"最快的一件**——抽帧质检里"寡淡"的镜（亮且有色 <11%）
 *  十有八九就是缺它：深蓝渐变 + 白字，content 够、vivid 不够。 */
const Ribbon = ({ t = 1, start = 0.04, hue = '#FF6B2C', angle = -18,
                  thick = 0.26, y = 0.62, opacity = 0.92 }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.36));
  const tk = R(thick, 0.26);
  const ry = R(y, 0.62);
  const ang = Math.max(-45, Math.min(45, typeof angle === 'number' && isFinite(angle) ? angle : -18));
  const th = Math.max(0, height * tk * p);
  const shift = (1 - p) * width * 0.45;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <g transform={`rotate(${ang} ${width / 2} ${height * ry}) translate(${shift} 0)`}>
        <rect x={-width * 0.3} y={height * ry - th / 2} width={width * 1.6}
              height={th} fill={hue} opacity={opacity} />
      </g>
    </svg>
  );
};

/** 前景 HUD：四角 L 形标记 + 右侧刻度。**前景层的标准件**——
 *  一镜里加了它，画面立刻有"三层"（底/中景/前景），元素数也上去了。
 *  它不抢主体，所以任何镜都能加。 */
const HudFrame = ({ t = 1, start = 0.02, hue = '#EAFBFF', inset = 0.055 }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.4));
  const m = width * R(inset, 0.055);
  const L = width * 0.075 * p;
  const sw = Math.max(2, width * 0.0035);
  const corners: [number, number, number, number][] = [
    [m, m, 1, 1], [width - m, m, -1, 1],
    [m, height - m, 1, -1], [width - m, height - m, -1, -1],
  ];
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      {corners.map((c, i) => (
        <g key={i} opacity={0.7 * p} stroke={hue} strokeWidth={sw} fill="none">
          <line x1={c[0]} y1={c[1]} x2={c[0] + c[2] * L} y2={c[1]} />
          <line x1={c[0]} y1={c[1]} x2={c[0]} y2={c[1] + c[3] * L} />
        </g>
      ))}
      {Array.from({ length: 7 }).map((_, i) => {
        const ty = height * 0.30 + i * height * 0.058;
        return <line key={`s${i}`} x1={width - m} y1={ty}
                     x2={width - m + (i % 2 ? width * 0.018 : width * 0.034) * p}
                     y2={ty} stroke={hue} strokeWidth={sw * 0.7} opacity={0.4 * p} />;
      })}
    </svg>
  );
};

/** 章节标记：右上角「第 183 号文明 / 属于"三日凌空"」。叙事片靠它分段。 */
const ChapterMark = ({ index = '', title = '', sub = '', t = 1, start = 0.04,
                       hue = '#E8C88A' }: any) => {
  const { width } = useVideoConfig();
  const p = easeOut(at(t, start, 0.34));
  if (!index && !title && !sub) return null;
  return (
    <div style={{
      position: 'absolute', right: width * 0.045, top: width * 0.04,
      textAlign: 'right', fontFamily: TOKENS.fontStack, opacity: p,
      transform: `translateY(${(1 - p) * 10}px)`,
    }}>
      {index ? (
        <div style={{ fontSize: width * 0.0155, color: hue, letterSpacing: '0.14em' }}>
          — {index}
        </div>
      ) : null}
      {title ? (
        <div style={{ fontSize: width * 0.0195, color: '#F2F5F8', fontWeight: 600,
                      marginTop: width * 0.005 }}>{title}</div>
      ) : null}
      {sub ? (
        <div style={{ fontSize: width * 0.0128, color: 'rgba(232,200,138,0.72)',
                      marginTop: width * 0.004 }}>{sub}</div>
      ) : null}
    </div>
  );
};

/** 时间标记：左下角「地球时间 1975 年」。给叙事一条可数的坐标。 */
const TimeMark = ({ text = '', t = 1, start = 0.1, hue = '#B9C4CF' }: any) => {
  const { width, height } = useVideoConfig();
  const p = easeOut(at(t, start, 0.4));
  if (!text) return null;
  return (
    <div style={{
      position: 'absolute', left: width * 0.045, bottom: height * 0.032,
      fontFamily: TOKENS.fontStack, fontSize: Math.min(width, height) * 0.022,
      color: hue, letterSpacing: '0.1em', opacity: 0.85 * p,
    }}>{text}</div>
  );
};

/** 带高亮的字幕：`text` 里出现 `hi` 的那一段换成 `hiColor`。
 *  参考片每一句都有一个词是暖色的（"一封信"、"警告"）——那一下就是视觉重音。
 *
 *  **不铺暗带，靠描边 + 双层阴影**（2026-10-25 改）。原来是底部 36% 高的
 *  `rgba(3,5,9,0.80)` 渐变——它确实保证了可读，但它把画面下三分之一整块压暗，
 *  看起来像"字幕把画面割开了"。参考片没有这条带子，靠的就是描边与阴影。
 *  `scrim>0` 仍可显式打开（比如字幕正好压在大太阳上那种极端情况）。 */
const RichCaption = ({ text = '', hi = '', hiColor = '#E8A33D', t = 1, start = 0.04,
                       size = 0.047, y = 0.845, bottom = 0.08, color = '#F2F5F8',
                       align = 'center', scrim = 0, stroke = 1 }: any) => {
  const { width, height } = useVideoConfig();
  // 字号按**短边**算：原来按宽度算，同一个 0.038 在 1920 宽是 73px、
  // 1280 宽是 48.6px、720 竖屏是 27px——观感差三倍。按短边归一化之后，
  // 横屏与竖屏的字占画面**比例**一致。参考片实测：1280×720、约 34px → 34/720≈0.047。
  const base = Math.min(width, height);
  const p = easeOut(at(t, start, 0.3));
  const fs = base * Math.min(0.075, Math.max(0.026, R(size, 0.047)));
  const cy = R(y, 0.845);
  const bt = R(bottom, 0.08);
  const sc = R(scrim, 0);
  const st = R(stroke, 1);
  const src = String(text || '');
  const key = String(hi || '');
  if (!src) return null;
  const parts = key && src.includes(key) ? src.split(key) : null;
  // **`bottom` 优先**：叙事片的字幕是整句话（30~45 字），按 `top` 定位时
  // 3 行会从画面下沿漏出去（真机踩过）。按底部锚定，字多就往上长，永远不会出画。
  // 2026-10-25：`bottom` 的单位从 `width` 改成 `height`——底边距本来就该按高算，
  // 按宽算在横屏上会偏出 1.8 倍。
  const pos: any = bt > 0
    ? { bottom: height * bt }
    : { top: `calc(${cy * 100}% - ${fs * 0.6}px)` };
  return (
    <>
      {sc > 0 ? (
        <div style={{
          position: 'absolute', left: 0, right: 0, bottom: 0,
          height: `${18 + 18 * sc}%`, opacity: p, pointerEvents: 'none',
          backgroundImage: `linear-gradient(to top, rgba(3,5,9,${0.80 * sc}), rgba(3,5,9,0))`,
        }} />
      ) : null}
      <div style={{
        position: 'absolute', left: width * 0.06, width: width * 0.88,
        ...pos, textAlign: align, fontFamily: TOKENS.fontStack,
        fontWeight: 600, fontSize: fs, lineHeight: 1.34, color, opacity: p,
        transform: `translateY(${(1 - p) * 14}px)`,
        // 折行要均衡：长句刚好超一点时会留下孤零零的一两个字（"缝。"单独一行）。
        // `balance` 让两行长度接近；浏览器不认就忽略，没有副作用。
        textWrap: 'balance',
        // **不要黑条，要描边 + 双层阴影**：`scrim` 那条 36% 高的黑渐变会把画面
        // 下三分之一整块压暗，看起来像"字幕把画面割开了"。参考片没有它，
        // 靠的就是这一组描边与阴影。压在暗画面上，白字本来就有对比。
        textShadow: '0 2px 8px rgba(0,0,0,0.95), 0 0 26px rgba(0,0,0,0.80)',
        WebkitTextStroke: st ? '0.55px rgba(0,0,0,0.55)' : undefined,
        paintOrder: 'stroke fill',
      }}>
      {parts ? (
        <>
          {parts.map((seg: string, i: number) => (
            <React.Fragment key={i}>
              {i ? <span style={{ color: hiColor }}>{key}</span> : null}{seg}
            </React.Fragment>
          ))}
        </>
      ) : src}
      </div>
    </>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// 电影感场景零件（叙事片用）。**这一组是"画具象东西"的**，上面那一组是"排版信息"的。
//
// 为什么需要两组：参考片（三体叙事 135 秒）放大看是纯代码画的——抛物面是标准椭圆弧
// 加均匀描边、星星是完美圆点、鸟是几笔折线、地平线是纯矩形、天空是线性渐变。
// 它不是插画，是**剪影 + 渐变 + 光**。我们原来只有信息图零件，所以只能画"圆 + 色带"。
// ─────────────────────────────────────────────────────────────────────────────

/** 天空：竖向三段渐变 + 一处光晕 + 可选云带。叙事片的底，永远第一个铺。 */
const Sky = ({ top = '#0a1020', mid = '#2a2340', low = '#7a4630',
               glowY = 0.18, glowHue = '#ffd9a0', glow = 0.55, glowR = 0.5,
               t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const g = R(glow, 0.55);
  const gy = R(glowY, 0.18);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <defs>
        <linearGradient id="sk" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={top} />
          <stop offset="55%" stopColor={mid} />
          <stop offset="100%" stopColor={low} />
        </linearGradient>
        <radialGradient id="skg">
          <stop offset="0%" stopColor={glowHue} stopOpacity={0.9 * g} />
          <stop offset="55%" stopColor={glowHue} stopOpacity={0.22 * g} />
          <stop offset="100%" stopColor={glowHue} stopOpacity={0} />
        </radialGradient>
      </defs>
      <rect x={0} y={0} width={width} height={height} fill="url(#sk)" />
      <circle cx={width * 0.5} cy={height * gy} r={width * R(glowR, 0.5)}
              fill="url(#skg)" />
    </svg>
  );
};

/** 太阳/月亮：一个实心圆 + 一圈柔光。`r` 是半径占宽度的比例。 */
const Sun = ({ x = 0.18, y = 0.2, r = 0.055, hue = '#ffe6b0', halo = 0.5, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const cx = width * R(x, 0.18);
  const cy = height * R(y, 0.2);
  const rr = width * R(r, 0.055);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <defs>
        <radialGradient id="sunh">
          <stop offset="0%" stopColor={hue} stopOpacity={0.55 * R(halo, 0.5)} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </radialGradient>
      </defs>
      <circle cx={cx} cy={cy} r={rr * 4.2} fill="url(#sunh)" />
      <circle cx={cx} cy={cy} r={rr} fill={hue} opacity={0.96} />
    </svg>
  );
};

/** 地平线：一条硬边 + 下方压暗 + 横向反光条（水面/海面）。 */
const Horizon = ({ y = 0.72, ground = '#0a0d14', glowHue = '#ffb066',
                   ripple = 0.4, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const gy = height * R(y, 0.72);
  const rp = R(ripple, 0.4);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <rect x={0} y={gy} width={width} height={Math.max(0, height - gy)} fill={ground} />
      {Array.from({ length: 7 }).map((_, i) => {
        const ry = gy + (height - gy) * (0.08 + i * 0.13);
        const w = width * (0.34 - i * 0.03) * (1 + 0.1 * Math.sin(t * 3 + i));
        return (
          <rect key={i} x={width * 0.5 - w / 2} y={ry} width={Math.max(0, w)}
                height={Math.max(1, height * 0.004)} fill={glowHue}
                opacity={0.22 * rp * (1 - i * 0.11)} />
        );
      })}
    </svg>
  );
};

/** 星点场：确定性撒点 + 轻微闪烁。**参考片里那些完美圆点就是这个**。 */
const StarField = ({ count = 140, seed = 7, t = 1, hue = '#dfeaff', opacity = 0.9 }: any) => {
  const { width, height } = useVideoConfig();
  const n = N(count, 140, 400);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      {Array.from({ length: n }).map((_, i) => {
        const x = random(`sx${seed}-${i}`) * width;
        const y = random(`sy${seed}-${i}`) * height;
        const r = 0.6 + random(`sr${seed}-${i}`) * 1.8;
        const tw = 0.55 + 0.45 * Math.sin(t * 6 + i);
        return <circle key={i} cx={x} cy={y} r={r} fill={hue}
                       opacity={opacity * tw * (0.35 + random(`so${seed}-${i}`) * 0.65)} />;
      })}
    </svg>
  );
};

/** 光束：从一点射出的锥形光（探照灯 / 信号射线 / 逆光）。 */
const Beam = ({ x = 0.5, y = 0.2, angle = 90, spread = 7, length = 0.9,
                hue = '#8ff0d8', opacity = 0.4, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const cx = width * R(x, 0.5);
  const cy = height * R(y, 0.2);
  const L = width * R(length, 0.9);
  const a = (typeof angle === 'number' ? angle : 90) * Math.PI / 180;
  const sp = (Math.max(0.5, Math.min(30, spread)) * Math.PI / 180);
  const x1 = cx + Math.cos(a - sp) * L;
  const y1 = cy + Math.sin(a - sp) * L;
  const x2 = cx + Math.cos(a + sp) * L;
  const y2 = cy + Math.sin(a + sp) * L;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <defs>
        <linearGradient id="bm" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={hue} stopOpacity={0.55 * R(opacity, 0.4)} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </linearGradient>
      </defs>
      <polygon points={`${cx},${cy} ${x1},${y1} ${x2},${y2}`} fill="url(#bm)" />
    </svg>
  );
};

/** 废墟天际线：一排高低不一的剪影 + 若干火点。城市/基地的远景。 */
const Ruin = ({ y = 0.72, seed = 3, count = 18, hue = '#ff7a2f',
                fires = 5, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const n = N(count, 18, 40);
  const base = height * R(y, 0.72);
  const f = N(fires, 5, 12);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      {Array.from({ length: n }).map((_, i) => {
        const w = width * (0.018 + random(`rw${seed}-${i}`) * 0.035);
        const h = height * (0.03 + random(`rh${seed}-${i}`) * 0.17);
        const x = (width + w * 2) * (i / n) - w;
        return <rect key={i} x={x} y={base - h} width={Math.max(1, w)}
                     height={Math.max(1, h)} fill="#04060a" />;
      })}
      {Array.from({ length: f }).map((_, i) => {
        const x = width * (0.06 + random(`fx${seed}-${i}`) * 0.86);
        const fy = base - height * (0.01 + random(`fy${seed}-${i}`) * 0.06);
        const fl = 0.7 + 0.3 * Math.sin(t * 9 + i * 2);
        return <ellipse key={`f${i}`} cx={x} cy={fy} rx={width * 0.012 * fl}
                        ry={height * 0.012 * fl} fill={hue} opacity={0.85} />;
      })}
    </svg>
  );
};

/** 射电望远镜剪影：抛物面 + 支架 + 基座。**参考片第 2 镜那个**。 */
const Dish = ({ x = 0.66, y = 0.62, r = 0.24, tilt = -18, rim = '#7fe6cf',
                rimW = 3, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const cx = width * R(x, 0.66);
  const cy = height * R(y, 0.62);
  const R0 = width * R(r, 0.24);
  const rot = Math.max(-60, Math.min(60, typeof tilt === 'number' ? tilt : -18));
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <g transform={`rotate(${rot} ${cx} ${cy})`}>
        {/* 抛物面：一个椭圆 + 一条上缘弧光 */}
        <ellipse cx={cx} cy={cy} rx={R0} ry={R0 * 0.92} fill="#05070c" />
        <path d={`M ${cx - R0} ${cy} A ${R0} ${R0 * 0.92} 0 0 1 ${cx + R0} ${cy}`}
              fill="none" stroke={rim} strokeWidth={rimW} opacity={0.9} />
        {/* 支架 */}
        <line x1={cx} y1={cy} x2={cx + R0 * 0.55} y2={cy - R0 * 0.95}
              stroke="#05070c" strokeWidth={Math.max(3, width * 0.004)} />
        <line x1={cx} y1={cy} x2={cx - R0 * 0.2} y2={cy - R0 * 0.9}
              stroke="#05070c" strokeWidth={Math.max(3, width * 0.004)} />
        <circle cx={cx + R0 * 0.55} cy={cy - R0 * 0.95} r={width * 0.008}
                fill={rim} opacity={0.85} />
      </g>
      {/* 基座 */}
      <rect x={cx - R0 * 0.09} y={cy} width={Math.max(2, R0 * 0.18)}
            height={Math.max(2, height * (0.98 - y * 0.98))} fill="#05070c" />
    </svg>
  );
};

/** 行星：球体 + 明暗交界 + 可选环。太空镜的主体。 */
const Planet = ({ x = 0.66, y = 0.36, r = 0.2, hue = '#c9a06a',
                  dark = '#0a0a0c', ring = false, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const cx = width * R(x, 0.66);
  const cy = height * R(y, 0.36);
  const rr = width * R(r, 0.2);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <defs>
        <radialGradient id="pl" cx="34%" cy="32%" r="76%">
          <stop offset="0%" stopColor={hue} stopOpacity={0.95} />
          <stop offset="58%" stopColor={hue} stopOpacity={0.55} />
          <stop offset="100%" stopColor={dark} stopOpacity={1} />
        </radialGradient>
      </defs>
      <circle cx={cx} cy={cy} r={rr} fill="url(#pl)" />
      {ring ? (
        <ellipse cx={cx} cy={cy} rx={rr * 1.55} ry={rr * 0.28} fill="none"
                 stroke={hue} strokeWidth={Math.max(1.5, width * 0.0018)}
                 opacity={0.5} />
      ) : null}
    </svg>
  );
};

/** 人影：头 + 肩 + 躯干的剪影，可指定朝向与高度。叙事片的"有人在场"。 */
const Figure = ({ x = 0.2, y = 0.96, h = 0.5, flip = false, rim = '',
                  t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const cx = width * R(x, 0.2);
  const baseY = height * R(y, 0.96);
  const H = height * R(h, 0.5);
  const w = H * 0.42;
  const dir = flip ? -1 : 1;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      <g transform={`translate(${cx} ${baseY}) scale(${dir} 1)`}>
        {/* 头 */}
        <ellipse cx={0} cy={-H * 0.9} rx={H * 0.075} ry={H * 0.095} fill="#04060a" />
        {/* 颈 + 肩 + 躯干（一条闭合路径，肩线是斜的，看起来才像人） */}
        <path d={`M ${-H * 0.05} ${-H * 0.82} L ${-w * 0.5} ${-H * 0.72}
                  L ${-w * 0.42} ${-H * 0.28} L ${-w * 0.3} 0 L ${w * 0.3} 0
                  L ${w * 0.42} ${-H * 0.28} L ${w * 0.5} ${-H * 0.72}
                  L ${H * 0.05} ${-H * 0.82} Z`} fill="#04060a" />
        {rim ? (
          <path d={`M ${-w * 0.5} ${-H * 0.72} L ${-w * 0.42} ${-H * 0.28}
                    L ${-w * 0.3} 0`} fill="none" stroke={rim}
                strokeWidth={Math.max(1, width * 0.0016)} opacity={0.55} />
        ) : null}
      </g>
    </svg>
  );
};

/** 舰队/点阵：一片按透视排布的小点。太空战的"两千艘战舰"。 */
const Fleet = ({ y = 0.78, count = 260, seed = 5, hue = '#cfd8e3',
                 spread = 1.0, t = 1 }: any) => {
  const { width, height } = useVideoConfig();
  const n = N(count, 260, 600);
  const base = height * R(y, 0.78);
  const sp = R(spread, 1.0);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0 }}>
      {Array.from({ length: n }).map((_, i) => {
        const px = width * (0.5 + (random(`fx${seed}-${i}`) - 0.5) * 1.8 * sp);
        const py = base - height * random(`fy${seed}-${i}`) * 0.22 * sp;
        const r = 0.8 + random(`fr${seed}-${i}`) * 1.6;
        return <circle key={i} cx={px} cy={py} r={r} fill={hue}
                       opacity={0.35 + random(`fo${seed}-${i}`) * 0.5} />;
      })}
    </svg>
  );
};

/** 整镜运镜：把整个场景包起来做缓慢推拉。
 *  **长镜靠它撑住**——一张不动的画面挂 10 秒就是幻灯片；推拉 8% 就"活"了。 */
const Camera = ({ children, z0 = 1, z1 = 1.08, panX = 0, panY = 0 }: any) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const p = durationInFrames > 1 ? frame / (durationInFrames - 1) : 0;
  const a = S(z0, 1);
  const b = S(z1, 1.08);
  const s = a + (b - a) * p;
  const dx = Math.max(-1, Math.min(1, panX)) * 40 * (1 - p) * 0.5;
  const dy = Math.max(-1, Math.min(1, panY)) * 30 * (1 - p) * 0.5;
  return (
    <AbsoluteFill style={{ transform: `scale(${s}) translate(${dx}px, ${dy}px)` }}>
      {children}
    </AbsoluteFill>
  );
};

/** 跨镜接力：把「贯穿物件」从上一镜的位置**接到**下一镜的位置。
 *
 *  为什么需要它：每一镜都是自包含组件，切镜时上一镜整个卸载——观众的眼睛断了。
 *  这一层画在 `TransitionSeries` **外面**，只在切点前后各 `dur/2` 帧里出现：
 *  从 A 的位置/大小插值到 B 的位置/大小，两头淡、中间亮。
 *  它不画新东西，只画一团柔光——所以不会抢主体的戏，只负责"跟得住"。
 *
 *  `hops` 由 Python 侧按每镜声明的 `MOTIF_AT` 生成（见 `story.parse_motif_at`）。 */
const SharedMorph = ({ hops = [] }: any) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  return (
    <>
      {hops.map((h: any, i: number) => {
        const half = Math.max(2, h.dur / 2);
        const p = (frame - (h.at - half)) / (half * 2);
        if (p <= 0 || p >= 1) return null;
        const q = p * p * (3 - 2 * p);            // smoothstep：起止都缓
        const a = h.from, b = h.to;
        const x = (a[0] + (b[0] - a[0]) * q) * width;
        const y = (a[1] + (b[1] - a[1]) * q) * height;
        const r = Math.max(1, (a[2] + (b[2] - a[2]) * q) * width);
        const op = Math.sin(p * Math.PI) * 0.55;  // 两头淡、中间亮；峰值别太高
        return (
          <svg key={i} viewBox={`0 0 ${width} ${height}`} width={width} height={height}
               style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
            <defs>
              <radialGradient id={`sm${i}`}>
                <stop offset="0%" stopColor={h.hue} stopOpacity={0.85 * op} />
                <stop offset="52%" stopColor={h.hue} stopOpacity={0.26 * op} />
                <stop offset="100%" stopColor={h.hue} stopOpacity={0} />
              </radialGradient>
            </defs>
            {/* 半径收在 1.7×：2.6× 时整帧被洗白，看着像镜头光斑而不是"那件东西在走" */}
            <circle cx={x} cy={y} r={Math.max(2, r * 1.7)} fill={`url(#sm${i})`} />
            <circle cx={x} cy={y} r={Math.max(1, r * 0.42)} fill={h.hue}
                    opacity={0.85 * op} />
          </svg>
        );
      })}
    </>
  );
};

/** 胶片颗粒：全片统一的一层噪点，压住"太干净"的数字感。 */
const Grain = ({ opacity = 0.06, seed = 3 }: any) => {
  const { width, height } = useVideoConfig();
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      {Array.from({ length: 240 }).map((_, i) => (
        <rect key={i} x={random(`gx${seed}-${i}`) * width}
              y={random(`gy${seed}-${i}`) * height}
              width={2 + random(`gw${seed}-${i}`) * 3}
              height={2 + random(`gh${seed}-${i}`) * 3}
              fill={i % 2 ? '#FFFFFF' : '#000000'} opacity={opacity} />
      ))}
    </svg>
  );
};

/** 暗角：四角压暗，把视线收到中间。和 Grain 一起构成"全局层"。 */
const Vignette = ({ strength = 0.55 }: any) => {
  const { width, height } = useVideoConfig();
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
         style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      <defs>
        <radialGradient id="vg" cx="50%" cy="50%" r="72%">
          <stop offset="55%" stopColor="#000000" stopOpacity={0} />
          <stop offset="100%" stopColor="#000000" stopOpacity={strength} />
        </radialGradient>
      </defs>
      <rect x={0} y={0} width={width} height={height} fill="url(#vg)" />
    </svg>
  );
};

/** 对照表：两三列、最多六行。**这是信息图的主力**，也是模型自己画不出来的那一类——
 *  表格是「数据 → 坐标」的映射，让模型每次重画必然歪（列宽不齐、行高不一、数值不对齐）。
 *  所以它做成**确定性组件**：分镜只给数据，排版由这里算。
 *
 *  `cols` 两到三列；`rows` 每行与 `cols` 等长。第一列左对齐，其余列：能认出是数字
 *  （含 % 与千分位）就右对齐并染成暖色——那一列就是这张表的重点。
 *  `link` 给了文字就在左侧画一条竖虚线 + 端点圆，表示「这些项都出自同一处」。
 *  行逐个淡入，`start + 0.10 + i*0.05`，让眼睛跟着往下走。 */
const CompareTable = ({ title = '', cols = [], rows = [], x = 0.5, y = 0.52,
                        w = 0.46, link = '', t = 1, start = 0.08,
                        hue = '#E8A33D' }: any) => {
  const { width, height } = useVideoConfig();
  const base = Math.min(width, height);
  const p = easeOut(at(t, start, 0.4));
  const cs = (Array.isArray(cols) ? cols : []).map((c: any) => String(c ?? ''));
  const rs = (Array.isArray(rows) ? rows : [])
    .filter((r: any) => Array.isArray(r) && r.length)
    .slice(0, 6)
    .map((r: any) => r.map((c: any) => String(c ?? '')));
  if (!cs.length || !rs.length) return null;
  const nCol = Math.min(3, Math.max(2, cs.length));
  const boxW = width * R(w, 0.46);
  const rowH = base * 0.038;
  const fsHead = base * 0.016;
  const fsCell = base * 0.019;
  const fsTitle = base * 0.022;
  const padX = base * 0.022;
  const isNum = (s: string) => /^[\d.,%+\-]+$/.test(s.trim());
  const left = width * R(x, 0.5) - boxW / 2;
  const top = height * R(y, 0.52) - (rowH * (rs.length + 1)) / 2;
  return (
    <div style={{ position: 'absolute', left, top, width: boxW, opacity: p,
                  fontFamily: TOKENS.fontStack,
                  // **不铺底色**：`dg/no-raw-styled-div` 禁止用 div 带纯色 background
                  // 画色块（要用 SVG rect + 主题色板）。而这里铺一块半透明底本来也
                  // 不是必需的——画面是暗的，白字加描边直接读得清，铺底反而遮画面。
                  // 表格靠一圈细线立起来，跟参考片那种"信息层"一个意思。
                  border: '1px solid rgba(255,255,255,0.14)',
                  borderRadius: base * 0.008,
                  padding: `${base * 0.018}px ${padX}px`,
                  boxSizing: 'border-box' }}>
      {title ? (
        <div style={{ fontSize: fsTitle, color: hue, letterSpacing: '0.12em',
                      marginBottom: base * 0.012 }}>{title}</div>
      ) : null}
      <div style={{ display: 'flex', fontSize: fsHead, color: 'rgba(242,245,248,0.55)',
                    letterSpacing: '0.10em', paddingBottom: base * 0.008,
                    borderBottom: '1px solid rgba(255,255,255,0.14)' }}>
        {cs.slice(0, nCol).map((c: string, i: number) => (
          <div key={i} style={{ flex: i ? 1 : 1.4,
                                textAlign: i ? 'right' : 'left' }}>{c}</div>
        ))}
      </div>
      {rs.map((r: string[], ri: number) => (
        <div key={ri} style={{
          display: 'flex', fontSize: fsCell, height: rowH, alignItems: 'center',
          opacity: easeOut(at(t, start + 0.10 + ri * 0.05, 0.25)),
          borderBottom: ri < rs.length - 1 ? '1px solid rgba(255,255,255,0.07)' : 'none',
        }}>
          {r.slice(0, nCol).map((c: string, i: number) => (
            <div key={i} style={{ flex: i ? 1 : 1.4,
                                  textAlign: i ? 'right' : 'left',
                                  color: i && isNum(c) ? hue : '#F2F5F8',
                                  fontVariantNumeric: 'tabular-nums' }}>{c}</div>
          ))}
        </div>
      ))}
      {link ? (
        <>
          <div style={{ position: 'absolute', left: -base * 0.05, top: base * 0.02,
                        bottom: base * 0.02, width: 0,
                        borderLeft: `1px dashed ${hue}66` }} />
          <div style={{ position: 'absolute', left: -base * 0.05 - base * 0.007,
                        top: base * 0.02, width: base * 0.014, height: base * 0.014,
                        borderRadius: '50%', background: hue, opacity: 0.85 }} />
          <div style={{ position: 'absolute', left: -base * 0.05 - base * 0.09,
                        top: base * 0.016, fontSize: fsHead, color: hue,
                        opacity: 0.9, whiteSpace: 'nowrap' }}>{link}</div>
        </>
      ) : null}
    </div>
  );
};
"""

#: 给模型看的能力清单。**必须与上面的实现一一对应**——名字写错它就调不到，
#: 而这不会让 lint 报错，只会让那一镜退回"自己画"。
MANIFEST: list[dict[str, str]] = [
    {"name": "Backdrop", "desc": "满幅背景：底色+顶部光晕+暗角。**每一镜第一件都该用它**",
     "props": "hue, glow(0~1), t"},
    {"name": "Caption", "desc": "字幕：滑入。位置按画面比例给，不要每镜都贴底部",
     "props": "text, t, start, size(占宽比例), color, x, y, align"},
    {"name": "ChapterTitle", "desc": "章节号+标题，用来切段落",
     "props": "index(如'01'), title, t, hue"},
    {"name": "StatCard", "desc": "一个数字+一行说明的卡片，弹入",
     "props": "value, label, t, start, hue, x, y"},
    {"name": "BarChart", "desc": "柱状图，柱子依次长出来",
     "props": "values(0~1数组), labels, t, start, hue, x, y, w, h"},
    {"name": "OrbitDiagram", "desc": "中心主体+外圈轨道点，讲围绕/层级/轨道",
     "props": "count, t, start, hue, x, y, r"},
    {"name": "LineTrace", "desc": "折线按进度生长，讲趋势/路径/传导",
     "props": "points([[x,y]…]0~1), t, start, hue"},
    {"name": "GlowOrb", "desc": "会呼吸的光球，当能量/太阳/焦点",
     "props": "t, start, hue, x, y, r"},
    {"name": "ScanGrid", "desc": "透视地面网格+扫描线，科技感的底子",
     "props": "t, start, hue, y"},
    {"name": "Particles", "desc": "确定性粒子场，讲物质/尘埃/数据流",
     "props": "count, t, start, hue, seed"},
    {"name": "Ribbon", "desc": "斜向高饱和色带，占画面 20~28%。**画面没颜色就用它**",
     "props": "t, start, hue, angle, thick(0.2~0.3), y, opacity"},
    {"name": "HudFrame", "desc": "前景四角标记+刻度，给画面加前景层。任何镜都能加",
     "props": "t, start, hue, inset"},
    {"name": "ChapterMark", "desc": "右上角章节标记（第183号文明 / 属于三日凌空）。叙事分段用",
     "props": "index, title, sub, t, start, hue"},
    {"name": "TimeMark", "desc": "左下角时间标记（地球时间 1975 年）",
     "props": "text, t, start, hue"},
    {"name": "RichCaption", "desc": "字幕，其中一个词换成暖色高亮（视觉重音）",
     "props": "text, hi, hiColor, t, start, size, y, color, align"},
    # ---- 电影感场景零件（叙事片）----
    {"name": "Sky", "desc": "**天空**：竖向三段渐变 + 光晕。叙事片永远第一个铺它",
     "props": "top, mid, low(三个色), glowY, glowHue, glow, glowR"},
    {"name": "Sun", "desc": "太阳/月亮：实心圆 + 柔光晕",
     "props": "x, y, r, hue, halo"},
    {"name": "Horizon", "desc": "地平线/水面：硬边 + 下方压暗 + 横向反光条",
     "props": "y, ground, glowHue, ripple"},
    {"name": "StarField", "desc": "星点场（确定性撒点 + 闪烁）。太空镜的底",
     "props": "count, seed, hue, opacity"},
    {"name": "Beam", "desc": "锥形光束（探照灯/信号射线/逆光）",
     "props": "x, y, angle(度), spread(度), length, hue, opacity"},
    {"name": "Ruin", "desc": "废墟天际线：高低剪影 + 火点。城市/基地远景",
     "props": "y, seed, count, hue, fires"},
    {"name": "Dish", "desc": "**射电望远镜剪影**：抛物面 + 支架 + 基座",
     "props": "x, y, r, tilt, rim, rimW"},
    {"name": "Planet", "desc": "行星：球体明暗 + 可选环",
     "props": "x, y, r, hue, dark, ring"},
    {"name": "Figure", "desc": "**人影剪影**：头+肩+躯干。叙事片的“有人在场”",
     "props": "x, y(脚底), h(高度占画面), flip, rim"},
    {"name": "Fleet", "desc": "舰队点阵：一片按透视排布的小点",
     "props": "y, count, seed, hue, spread"},
    {"name": "Camera", "desc": "**整镜运镜**：把整个场景包起来缓慢推拉。长镜靠它撑住",
     "props": "z0(起始 1), z1(结束 1.08), panX, panY；children"},
    {"name": "Grain", "desc": "胶片颗粒（全局层已有一层，单镜一般不用）",
     "props": "opacity, seed"},
    {"name": "Vignette", "desc": "四角压暗（全局层已有一层，单镜一般不用）",
     "props": "strength"},
    {"name": "CompareTable", "desc": "**对照表**：两三列、最多六行，数值列右对齐并染色。"
                                    "信息图的主力（地名+件数 / 古今读音 / 参数对比）",
     "props": "title, cols[2~3], rows[[...]], x, y, w, link(左侧虚线锚点文字)"},
]

#: 全局层：叠在**整片**之上（不是每镜）。七层一次渲完才有资格做这件事。
#:
#: 1 手持微动（整片轻推） 2 扫光 3 快门闪（切点白闪） 4 暗场呼吸
#: 5 颗粒 6 暗角 7 扫描线
GLOBAL_FX_TSX = r"""
const GlobalFX: React.FC<{ cuts?: number[] }> = ({ cuts = [] }) => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const t = durationInFrames > 0 ? frame / durationInFrames : 0;

  // 3 快门闪：切点前后 1 帧白闪，模拟剪辑点
  const nearCut = cuts.some((c) => Math.abs(frame - c) <= 1);
  // 4 暗场呼吸：整片亮度缓慢起伏
  const breath = 0.03 + 0.03 * Math.sin(t * Math.PI * 6);
  // 2 扫光：每 150 帧斜向扫过一次
  const sweepPos = ((frame % 150) / 150) * 1.6 - 0.3;

  return (
    <>
      {/* 2 扫光 */}
      <div style={{
        position: 'absolute', inset: 0, pointerEvents: 'none',
        backgroundImage: `linear-gradient(115deg, rgba(255,255,255,0) ${sweepPos * 100}%, rgba(255,255,255,0.055) ${sweepPos * 100 + 8}%, rgba(255,255,255,0) ${sweepPos * 100 + 16}%)`,
      }} />
      {/* 4 暗场呼吸 */}
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none',
                    backgroundImage: `linear-gradient(rgba(0,0,0,${Math.max(0, breath)}), rgba(0,0,0,${Math.max(0, breath)}))` }} />
      {/* 3 快门闪 */}
      {nearCut ? (
        <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none',
                      backgroundImage: 'linear-gradient(rgba(255,255,255,0.32), rgba(255,255,255,0.32))' }} />
      ) : null}
      {/* 5 颗粒 + 6 暗角 */}
      <Grain opacity={0.05} seed={11} />
      <Vignette strength={0.48} />
      {/* 7 扫描线 */}
      <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
           style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        <rect x={0} y={(t * 130) % 100 * height / 100} width={width} height={2}
              fill="#22D3EE" opacity={0.05} />
      </svg>
    </>
  );
};
"""

#: 整片外层的"手持微动"：内容整块轻推，模拟镜头不绝对静止。
CAMERA_DRIFT_TSX = r"""
const Drift: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const frame = useCurrentFrame();
  const x = Math.sin(frame / 97) * 4;
  const y = Math.cos(frame / 131) * 3;
  const s = 1.008;
  return (
    <AbsoluteFill style={{ transform: `translate(${x}px, ${y}px) scale(${s})` }}>
      {children}
    </AbsoluteFill>
  );
};
"""


def manifest_block() -> str:
    """给写手的那一段清单。一行一件，含 props。"""
    rows = [f"· `{m['name']}` —— {m['desc']}｜props: {m['props']}" for m in MANIFEST]
    return "\n".join(rows)


def used_components(code: str) -> list[str]:
    """这一镜用到了哪几件。按 `<Name` 出现判断（自闭合与开标签都算）。"""
    return sorted({m["name"] for m in MANIFEST
                   if re.search(r"<" + m["name"] + r"[\s/>]", code)})


#: SVG 图元。`g` 不算——它是分组容器，不是看得见的一个元素。
_PRIM_RE = re.compile(r"<(rect|circle|ellipse|line|path|polygon|polyline|text|image)\b")
_COMP_RE = re.compile(r"<(" + "|".join(m["name"] for m in MANIFEST) + r")[\s/>]")
#: 用 `map(` / `Array.from(` 生成的一批元素按这个数计：代码里只写了一个 `<circle>`，
#: 实际渲出来可能是几十个。不补这一档，粒子/网格那种镜会被误判成"只有两个元素"。
_LOOP_BONUS = 8


def element_count(code: str) -> int:
    """这一镜有几个视觉元素（SVG 图元 + 设计系统组件实例）。

    为什么要有这个数：像素判据分得清"空 / 不空"和"有没有颜色"，**分不清"一层"与"三层"**。
    一镜只有一个圆 + 一行字时 content 与 vivid 都可能合格，看着还是像一页排版。
    把它变成可数的东西，`qa` 才能对"层次不够"下判据（`sparse`）。

    口径说清（都不精确，但**稳定**）：`<g>` 不计；`map(`/`Array.from(` 各补 8 个。
    """
    prims = len(_PRIM_RE.findall(code))
    comps = len(_COMP_RE.findall(code))
    bonus = _LOOP_BONUS if re.search(r"\.map\(|Array\.from\(", code) else 0
    return prims + comps + bonus


def coverage(codes: list[str]) -> dict[str, object]:
    """组件覆盖率：用了 ≥1 件设计系统的镜数 / 总镜数。"""
    per = [used_components(c) for c in codes]
    hit = [i for i, u in enumerate(per) if u]
    return {
        "ratio": round(len(hit) / max(1, len(codes)), 3),
        "used": len(hit),
        "total": len(codes),
        "per_shot": per,
        "missing": [i + 1 for i, u in enumerate(per) if not u],
    }
