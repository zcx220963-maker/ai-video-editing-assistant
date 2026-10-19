/**
 * `@dg/lib/brush` —— 手绘笔刷与材质层。
 *
 * **它是什么。** 上一代零件库（`@dg/lib/draw/*`）只有 roughjs 的等宽描线；这一层补的是
 * 「画出来像手画的」所需的那几样：可变宽度的笔触、水彩渗染、纸纹与颗粒、光池、柔影、
 * 暗角。零件是**可选参考**，镜头代码也可以完全不用它们、直接写 SVG/Canvas。
 *
 * **纪律（与镜头代码同一套）：**
 * 1. 不许 `Math.random`：所有抖动、散点都从 `seed` 走 `./rng` 的 mulberry32。
 * 2. 颜色不写死：缺省从 `resolveFilmStyle(getInputProps())` 取墨色/纸色/强调色。
 * 3. 不联网、不读 DOM 尺寸：坐标就是成片设备像素。
 * 4. 纹理用固定 seed 的 `feTurbulence`；同一份代码渲两次逐像素一致。
 *
 * ```tsx
 * import { BrushStroke, PaperTexture, LightPool, Wash } from '@dg/lib/brush';
 *
 * <PaperTexture opacity={0.35} />
 * <LightPool x={1400} y={300} radius={900} />
 * <BrushStroke points={[[200, 700], [700, 640], [1200, 760]]} width={14} brush="pencil" />
 * ```
 */
import React from 'react';
import { AbsoluteFill, getInputProps } from 'remotion';
import { resolveFilmStyle } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { hashSeed, jitter, rng } from './rng';

export type BrushKind = 'pencil' | 'marker' | 'watercolor';

/** 缺省墨色：整片那一档的 `ink`，再缺省 tokens。 */
function inkColor(color?: string): string {
  if (color) return color;
  return resolveFilmStyle(getInputProps()).ink ?? TOKENS.ink;
}
function accentColor(color?: string): string {
  if (color) return color;
  return resolveFilmStyle(getInputProps()).accent ?? TOKENS.accent;
}

export interface BrushStrokeProps {
  /** 中心线控制点（成片像素）。镜头代码按帧算好传进来。 */
  points: Array<[number, number]>;
  /** 笔触最宽处（像素），缺省 8 */
  width?: number;
  /** 笔性：铅笔有颗粒、马克笔实心、水彩有渗染。缺省 pencil */
  brush?: BrushKind;
  color?: string;
  opacity?: number;
  /** 同一帧里多支笔要给不同的数，抖动才不重样 */
  seed?: number | string;
  /** 闭合形状（轮廓、圈）——两端不收笔 */
  closed?: boolean;
  /** 两端收笔强度 0~1，缺省 0.55；0 = 等宽 */
  taper?: number;
  /** 手抖幅度（像素），缺省 1.6 */
  wobble?: number;
}

/** 中心线 → 可变宽度多边形。两端按 `taper` 收细，中间压重。 */
function ribbonPath(
  points: Array<[number, number]>,
  width: number,
  { closed = false, taper = 0.55, wobble = 1.6, seed = 0 }: {
    closed?: boolean; taper?: number; wobble?: number; seed?: number | string;
  },
): string {
  const n = points.length;
  if (n < 2) return '';
  const noise = jitter(hashSeed(seed), n * 2 + 4);
  const left: string[] = [];
  const right: string[] = [];
  for (let i = 0; i < n; i += 1) {
    const [px, py] = points[i];
    const prev = points[Math.max(0, i - 1)];
    const next = points[Math.min(n - 1, i + 1)];
    const dx = next[0] - prev[0];
    const dy = next[1] - prev[1];
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    const t = n === 1 ? 0.5 : i / (n - 1);
    const end = closed
      ? 1
      : 1 - taper + taper * Math.pow(Math.sin(Math.PI * t), 0.6);
    const half = Math.max(0.35, (width / 2) * end);
    // 手抖：沿法线方向的低频偏摆 + 一点点沿线的高频毛刺
    const off = noise[i] * wobble + noise[i + n] * wobble * 0.35;
    const cx = px + nx * off;
    const cy = py + ny * off;
    left.push(`${(cx + nx * half).toFixed(2)} ${(cy + ny * half).toFixed(2)}`);
    right.push(`${(cx - nx * half).toFixed(2)} ${(cy - ny * half).toFixed(2)}`);
  }
  const back = right.reverse();
  return `M ${left[0]} L ${left.slice(1).join(' L ')} L ${back.join(' L ')} Z`;
}

export const BrushStroke: React.FC<BrushStrokeProps> = ({
  points,
  width = 8,
  brush = 'pencil',
  color,
  opacity,
  seed = 1,
  closed = false,
  taper = 0.55,
  wobble = 1.6,
}) => {
  const stroke = inkColor(color);
  const base = hashSeed(seed);
  const d = ribbonPath(points, width, { closed, taper, wobble, seed });
  if (!d) return null;
  const layers =
    brush === 'watercolor'
      ? [0, 1, 2].map((i) => {
          const r = rng(base + i * 97);
          const dx = (r() * 2 - 1) * width * 0.12;
          const dy = (r() * 2 - 1) * width * 0.12;
          return {
            d: ribbonPath(points.map(([x, y]) => [x + dx, y + dy] as [number, number]),
              width * (0.85 + r() * 0.4),
              { closed, taper, wobble: wobble * 1.4, seed: base + i * 31 }),
            opacity: (opacity ?? 0.55) * (i === 0 ? 0.9 : 0.42),
            filter: `url(#dg-wash-${base + i})`,
          };
        })
      : brush === 'pencil'
        ? [
            { d, opacity: opacity ?? 0.88, filter: undefined },
            {
              d: ribbonPath(points, width * 0.42, { closed, taper, wobble: wobble * 1.8, seed: base + 7 }),
              opacity: (opacity ?? 0.88) * 0.32,
              filter: undefined,
            },
          ]
        : [{ d, opacity: opacity ?? 0.92, filter: undefined }];
  return (
    <g>
      {brush === 'watercolor'
        ? layers.map((layer, i) => (
            <defs key={i}>
              <filter id={`dg-wash-${base + i}`} x="-20%" y="-20%" width="140%" height="140%">
                <feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="2"
                  seed={base + i} stitchTiles="stitch" result="noise" />
                <feDisplacementMap in="SourceGraphic" in2="noise" scale={width * 0.55}
                  xChannelSelector="R" yChannelSelector="G" />
              </filter>
            </defs>
          ))
        : null}
      {layers.map((layer, i) => (
        <path key={i} d={layer.d} fill={stroke} opacity={layer.opacity}
          filter={layer.filter} style={{ mixBlendMode: brush === 'watercolor' ? 'multiply' : undefined }} />
      ))}
    </g>
  );
};

// ---------------------------------------------------------------------------
// 材质：纸纹 / 颗粒 / 水彩填充
// ---------------------------------------------------------------------------

/** 一张可平铺的噪声纹理（data URI）。模块级生成一次，逐帧复用同一串字节。 */
function noiseTile(freq: number, octaves: number, seed: number, alpha: number): string {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240">` +
    `<filter id="n"><feTurbulence type="fractalNoise" baseFrequency="${freq}" ` +
    `numOctaves="${octaves}" seed="${seed}" stitchTiles="stitch"/>` +
    `<feColorMatrix type="saturate" values="0"/>` +
    `<feComponentTransfer><feFuncA type="linear" slope="${alpha}"/></feComponentTransfer></filter>` +
    `<rect width="100%" height="100%" filter="url(#n)"/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

export interface TextureProps {
  opacity?: number;
  /** 纹理密度（像素），缺省 240 */
  scale?: number;
  seed?: number;
  /** multiply 压暗（纸纹）/ overlay 提亮（颗粒） */
  blend?: 'multiply' | 'overlay' | 'soft-light' | 'screen';
}

/** `<PaperTexture/>`：整帧纸纹。叠在底色上，`multiply` 让纸的纤维显出来。 */
export const PaperTexture: React.FC<TextureProps> = ({
  opacity = 0.32, scale = 240, seed = 7, blend = 'multiply',
}) => (
  <AbsoluteFill
    style={{
      backgroundImage: noiseTile(0.82, 2, seed, 0.55),
      backgroundSize: `${scale}px ${scale}px`,
      opacity,
      mixBlendMode: blend,
      pointerEvents: 'none',
    }}
  />
);

/** `<Grain/>`：胶片颗粒。比纸纹更细、更亮。 */
export const Grain: React.FC<TextureProps> = ({
  opacity = 0.14, scale = 180, seed = 19, blend = 'overlay',
}) => (
  <AbsoluteFill
    style={{
      backgroundImage: noiseTile(0.95, 1, seed, 0.7),
      backgroundSize: `${scale}px ${scale}px`,
      opacity,
      mixBlendMode: blend,
      pointerEvents: 'none',
    }}
  />
);

export interface WashProps {
  /** SVG path（这一镜自己画的形状） */
  d: string;
  color?: string;
  opacity?: number;
  seed?: number;
  /** 毛边位移幅度（像素），缺省 6 */
  bleed?: number;
}

/** `<Wash/>`：水彩填充——把一条路径灌色，边缘用 `feTurbulence` 撕出渗染。 */
export const Wash: React.FC<WashProps> = ({
  d, color, opacity = 0.5, seed = 3, bleed = 6,
}) => {
  const id = `dg-washfill-${hashSeed(seed)}`;
  return (
    <g>
      <defs>
        <filter id={id} x="-20%" y="-20%" width="140%" height="140%">
          <feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="2"
            seed={hashSeed(seed)} stitchTiles="stitch" result="noise" />
          <feDisplacementMap in="SourceGraphic" in2="noise" scale={bleed}
            xChannelSelector="R" yChannelSelector="G" />
        </filter>
      </defs>
      <path d={d} fill={accentColor(color)} opacity={opacity} filter={`url(#${id})`} />
    </g>
  );
};

// ---------------------------------------------------------------------------
// 光与影
// ---------------------------------------------------------------------------

export interface LightPoolProps {
  x: number;
  y: number;
  radius?: number;
  color?: string;
  opacity?: number;
  blend?: 'screen' | 'soft-light' | 'overlay';
}

/** `<LightPool/>`：一处柔和光池（径向渐变），给画面一个明确的光源方向。 */
export const LightPool: React.FC<LightPoolProps> = ({
  x, y, radius = 700, color, opacity = 0.5, blend = 'screen',
}) => (
  <AbsoluteFill
    style={{
      backgroundImage: `radial-gradient(circle at ${x}px ${y}px, ${accentColor(color)} 0%, transparent ${radius}px)`,
      opacity,
      mixBlendMode: blend,
      pointerEvents: 'none',
    }}
  />
);

export interface SoftShadowProps {
  x: number;
  y: number;
  rx?: number;
  ry?: number;
  color?: string;
  opacity?: number;
}

/** `<SoftShadow/>`：物件脚下的柔影（椭圆的径向渐变，不需要模糊滤镜）。 */
export const SoftShadow: React.FC<SoftShadowProps> = ({
  x, y, rx = 220, ry = 46, color = '#000000', opacity = 0.18,
}) => {
  const id = `dg-shadow-${Math.round(x)}-${Math.round(y)}-${Math.round(rx)}`;
  return (
    <g>
      <defs>
        <radialGradient id={id}>
          <stop offset="0%" stopColor={color} stopOpacity={opacity} />
          <stop offset="70%" stopColor={color} stopOpacity={opacity * 0.35} />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </radialGradient>
      </defs>
      <ellipse cx={x} cy={y} rx={rx} ry={ry} fill={`url(#${id})`} />
    </g>
  );
};

export interface VignetteProps {
  /** 边缘压暗强度 0~1，缺省 0.35 */
  strength?: number;
  /** 中心透明区的半径（像素），缺省 620 */
  inner?: number;
  color?: string;
}

/** `<Vignette/>`：暗角。整片统一的「镜头感」，压住四角、把视线收回中心。 */
export const Vignette: React.FC<VignetteProps> = ({
  strength = 0.35, inner = 620, color = '#000000',
}) => (
  <AbsoluteFill
    style={{
      backgroundImage: `radial-gradient(circle at 50% 50%, transparent 0%, transparent ${inner}px, ${color} ${inner * 2.2}px)`,
      opacity: strength,
      pointerEvents: 'none',
    }}
  />
);
