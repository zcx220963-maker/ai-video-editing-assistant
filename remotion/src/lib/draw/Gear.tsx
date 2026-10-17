/**
 * `<Gear/>` —— 一个齿轮。
 *
 * 「运转」「协作」「工业」这些说法在画面上就是一个齿轮，两三个咬在一起更好。
 * 要它转：把帧算成角度传进 `spin`（度）——零件不读帧，所以「转多快、什么时候开始转」
 * 全在镜头代码里，一眼看得见。
 *
 * ```tsx
 * const spin = interpolate(frame, [0, 60], [0, 90]);
 * <Gear x={620} y={420} r={140} spin={spin} />
 * <Gear x={860} y={470} r={96} spin={-spin * 1.46} />   // 咬合的那个反着转
 * ```
 *
 * 坐标：`x`/`y` 是**圆心**，`r` 是半径（成片像素）。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface GearProps extends Inked {
  x: number;
  y: number;
  /** 半径（成片像素），缺省 140 */
  r?: number;
  /** 齿数，缺省 12 */
  teeth?: number;
  /** 旋转角（度） */
  spin?: number;
  /** 齿是不是实心的 */
  solid?: boolean;
  seed?: number;
}

export const Gear: React.FC<GearProps> = (props) => {
  const { x, y, r = 140, teeth = 12, spin = 0, solid = false, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 89);
  const out: Stroke[] = [];
  const rad = (spin * Math.PI) / 180;
  const ring = r * 0.74;
  const tooth = r * 0.26;

  // 齿：从齿根圈往外伸的一小段
  for (let i = 0; i < teeth; i++) {
    const a = rad + (i / teeth) * Math.PI * 2;
    out.push(b.line(x + Math.cos(a) * ring, y + Math.sin(a) * ring,
                    x + Math.cos(a) * (ring + tooth), y + Math.sin(a) * (ring + tooth)));
  }
  out.push(b.circle(x, y, ring * 2, solid ? { color: ink.paper } : undefined));
  out.push(b.circle(x, y, r * 0.28, { color: ink.accent }));   // 轴孔
  // 三条辐条：有它们才读得出「这是个齿轮」而不是一个圆盘
  for (let i = 0; i < 3; i++) {
    const a = rad + (i / 3) * Math.PI * 2;
    out.push(b.line(x + Math.cos(a) * r * 0.16, y + Math.sin(a) * r * 0.16,
                    x + Math.cos(a) * ring * 0.94, y + Math.sin(a) * ring * 0.94));
  }
  return <Drawn strokes={out} />;
};

export default Gear;
