/**
 * `@dg/lib/scene` —— 场景底板：前中后三层、视平线、空气里的浮尘。
 *
 * 镜头代码当然可以自己 `AbsoluteFill` 叠层；这里给的是一组**约定好的坐标与层次**，
 * 让 24 镜的远景/中景/近景有同一套纵深语义，而不是每镜各自发明。
 *
 * ```tsx
 * import { Scene, Horizon, Particles } from '@dg/lib/scene';
 *
 * <Scene
 *   horizon={760}
 *   far={<Sky />}       // 天空、远山、逆光
 *   mid={<Room />}      // 主体所在的平面
 *   near={<Foreground />} // 前景遮挡、光斑
 * />
 * ```
 */
import React from 'react';
import { AbsoluteFill, getInputProps, useCurrentFrame, useVideoConfig } from 'remotion';
import { resolveFilmStyle } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { BrushStroke } from './brush';
import { hashSeed, rng } from './brush/rng';

export interface SceneProps {
  /** 远景：天空、远山、雾。整幅铺满，最底一层。 */
  far?: React.ReactNode;
  /** 中景：主体所在的平面。 */
  mid?: React.ReactNode;
  /** 近景：前景遮挡、光斑、颗粒。最上一层。 */
  near?: React.ReactNode;
  /** 视平线（成片像素），缺省 62% 高度。只影响 haze 带的位置。 */
  horizon?: number;
  /** 地平线附近的空气透视（雾带）强度 0~1，缺省 0.35；0 = 不画 */
  haze?: number;
  /** 天空/底色。缺省这一镜的 `film_style.bg`。 */
  bg?: string;
}

/** 三层场景：远 → 中 → 近。所有子节点都用**成片设备像素**坐标。 */
export const Scene: React.FC<SceneProps> = ({
  far, mid, near, horizon, haze = 0.35, bg,
}) => {
  const { width, height } = useVideoConfig();
  const style = resolveFilmStyle(getInputProps());
  const y = horizon ?? Math.round(height * 0.62);
  const base = bg ?? style.bg ?? TOKENS.bg;
  const paper = style.paper ?? TOKENS.paper;
  return (
    <AbsoluteFill>
      {/* 天光：上亮下暖，从第 0 帧就定住底色，不给「先白后画」的闪帧 */}
      <AbsoluteFill
        style={{
          backgroundImage:
            `linear-gradient(180deg, ${base} 0%, ${paper} 58%, ${base} 100%)`,
        }}
      />
      <AbsoluteFill>{far}</AbsoluteFill>
      {haze > 0 ? (
        <AbsoluteFill
          style={{
            backgroundImage:
              `linear-gradient(180deg, transparent ${Math.max(0, y - height * 0.18)}px, ` +
              `${paper} ${y}px, transparent ${Math.min(height, y + height * 0.14)}px)`,
            opacity: haze,
            pointerEvents: 'none',
          }}
        />
      ) : null}
      <AbsoluteFill>{mid}</AbsoluteFill>
      <AbsoluteFill>{near}</AbsoluteFill>
    </AbsoluteFill>
  );
};

export interface HorizonProps {
  /** 视平线 y（成片像素），缺省 62% 高度 */
  y?: number;
  color?: string;
  /** 线宽（像素），缺省 5 */
  thickness?: number;
  opacity?: number;
  seed?: number | string;
}

/** 一条手画的视平线：不是直尺线，两端淡出、中间有起落。 */
export const Horizon: React.FC<HorizonProps> = ({
  y, color, thickness = 5, opacity = 0.5, seed = 'horizon',
}) => {
  const { width, height } = useVideoConfig();
  const style = resolveFilmStyle(getInputProps());
  const yy = y ?? Math.round(height * 0.62);
  const points = Array.from({ length: 9 }, (_, i) => {
    const t = i / 8;
    return [Math.round(-width * 0.04 + t * width * 1.08),
      Math.round(yy + Math.sin(t * Math.PI * 1.7 + hashSeed(seed) % 7) * height * 0.004)] as [number, number];
  });
  return (
    <svg width={width} height={height} style={{ position: 'absolute', inset: 0 }}>
      <BrushStroke
        points={points}
        width={thickness}
        brush="pencil"
        color={color ?? style.ink ?? TOKENS.ink}
        opacity={opacity}
        seed={seed}
        wobble={1.2}
      />
    </svg>
  );
};

export interface ParticlesProps {
  /** 颗粒数，缺省 40。真机上 40~120 够用，再多是噪点不是氛围。 */
  count?: number;
  seed?: number | string;
  color?: string;
  /** 基础半径（像素），实际在 0.4~1.6 倍之间抖动 */
  size?: number;
  opacity?: number;
  /** 上升速度（像素/秒），缺省 14；负值 = 下沉 */
  rise?: number;
  /** 活动区域 [x0, y0, x1, y1]，缺省整幅。
   *  也接受 `{x, y, w, h}`：沉淀出来的模板常把矩形写成对象字面量，而这里过去只认
   *  四元组，解构一个普通对象会直接抛 `object is not iterable`，整条沉淀在
   *  `/render-stills` 变成没有上下文的 HTTP 500。多认一种写法比让模型重写三遍便宜。 */
  area?: [number, number, number, number]
    | { x: number; y: number; w: number; h: number };
}

/** 空气里的浮尘/光点。位置由 seed 决定，随帧缓慢上升，循环回到底部。 */
export const Particles: React.FC<ParticlesProps> = ({
  count = 40, seed = 'dust', color, size = 3, opacity = 0.5, rise = 14, area,
}) => {
  const { width, height, fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const style = resolveFilmStyle(getInputProps());
  const [x0, y0, x1, y1] = Array.isArray(area)
    ? area
    : area
      ? [area.x, area.y, area.x + area.w, area.y + area.h]
      : [0, 0, width, height];
  const spanX = Math.max(1, x1 - x0);
  const spanY = Math.max(1, y1 - y0);
  const next = rng(hashSeed(seed));
  const t = frame / Math.max(1, fps);
  const dots = Array.from({ length: count }, (_, i) => {
    const bx = next();
    const by = next();
    const r = size * (0.4 + next() * 1.2);
    const speed = rise * (0.5 + next());
    const phase = next() * Math.PI * 2;
    const drift = Math.sin(phase + t * (0.6 + next() * 0.8)) * size * 5;
    const yy = y1 - (((y0 + (1 - by) * spanY) - t * speed) % spanY + spanY) % spanY;
    return { key: i, cx: x0 + bx * spanX + drift, cy: yy, r };
  });
  return (
    <svg width={width} height={height} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      {dots.map((d) => (
        <circle key={d.key} cx={d.cx} cy={d.cy} r={d.r}
          fill={color ?? style.accent ?? TOKENS.accent} opacity={opacity} />
      ))}
    </svg>
  );
};
