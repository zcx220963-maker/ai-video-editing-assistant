/**
 * `<Arrow/>` —— 一支箭头。
 *
 * 指、连、推、变——讲因果与流向时，画面里那一笔就是箭头。三点给完整：
 * 起点、终点、弯多少。要它「一帧一帧长出来」，把 `upto`（0~1）喂帧进去即可。
 *
 * ```tsx
 * const grow = interpolate(frame, [0, 24], [0, 1]);
 * <Arrow x1={420} y1={700} x2={1180} y2={480} upto={grow} />
 * ```
 *
 * 坐标：起终点都是成片像素。`bend` 为 0 是直线，正数向上弯。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface ArrowProps extends Inked {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** 弯曲量（像素，正数向上弯），缺省 0 */
  bend?: number;
  /** 画到多长：0~1，缺省 1（整支） */
  upto?: number;
  /** 箭头张开的大小（像素），缺省 22 */
  head?: number;
  seed?: number;
}

export const Arrow: React.FC<ArrowProps> = (props) => {
  const { x1, y1, x2, y2, bend = 0, upto = 1, head = 22, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 113);
  const out: Stroke[] = [];
  const t = Math.max(0, Math.min(1, upto));
  // 中点往法线方向偏移 ＝ 一条二次曲线；t 截断时沿同一支曲线取前一段
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2 - bend;
  const at = (k: number): [number, number] => [
    (1 - k) * (1 - k) * x1 + 2 * (1 - k) * k * mx + k * k * x2,
    (1 - k) * (1 - k) * y1 + 2 * (1 - k) * k * my + k * k * y2,
  ];
  const samples = 24;
  const pts: [number, number][] = [];
  for (let i = 0; i <= samples * t; i++) pts.push(at(i / samples));
  if (pts.length > 1) out.push(b.curve(pts));
  if (t > 0.5) {
    const [ex, ey] = at(t);
    const [ax, ay] = at(Math.max(0, t - 0.06));
    const a = Math.atan2(ey - ay, ex - ax);
    for (const off of [2.6, -2.6]) {
      out.push(b.line(ex, ey,
                      ex + Math.cos(a + off) * head, ey + Math.sin(a + off) * head));
    }
  }
  return <Drawn strokes={out} />;
};

export default Arrow;
