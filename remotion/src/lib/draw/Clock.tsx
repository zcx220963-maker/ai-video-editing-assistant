/**
 * `<Clock/>` —— 一只钟。计时器、历史节点、年代感都用它。
 *
 * 指针的指向是 props，不是 `Date.now()`（渲染必须逐帧确定）：镜头代码把帧算成
 * `hour`/`minute` 传进来，想让针扫一圈就扫一圈，想让它停在 1850 年就停住。
 *
 * ```tsx
 * const sweep = interpolate(frame, [0, 90], [0, 720]);
 * <Clock x={960} y={500} r={220} hour={9} minute={sweep} />
 * ```
 *
 * 坐标：`x`/`y` 是**表盘中心**，`r` 是半径。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface ClockProps extends Inked {
  x: number;
  y: number;
  /** 半径（成片像素），缺省 220 */
  r?: number;
  /** 时针指到几（0~12，可带小数） */
  hour?: number;
  /** 分针指到几分（0~60，可带小数） */
  minute?: number;
  /** 画不画 12 个刻度 */
  ticks?: boolean;
  seed?: number;
}

export const Clock: React.FC<ClockProps> = (props) => {
  const { x, y, r = 220, hour = 10, minute = 10, ticks = true, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 163);
  const out: Stroke[] = [];
  const hand = (frac: number, len: number) => {
    const a = frac * Math.PI * 2 - Math.PI / 2;
    return b.line(x, y, x + Math.cos(a) * len, y + Math.sin(a) * len);
  };

  out.push(b.circle(x, y, r * 2, { color: ink.paper }));
  out.push(b.circle(x, y, r * 1.82));
  if (ticks) {
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
      const long = i % 3 === 0;
      const r1 = r * (long ? 0.72 : 0.8);
      out.push(b.line(x + Math.cos(a) * r1, y + Math.sin(a) * r1,
                      x + Math.cos(a) * r * 0.88, y + Math.sin(a) * r * 0.88));
    }
  }
  out.push(hand(((hour % 12) + (minute % 60) / 60) / 12, r * 0.44));   // 时针
  out.push(hand((minute % 60) / 60, r * 0.66));                        // 分针
  out.push(b.circle(x, y, r * 0.1, { color: ink.accent }));
  return <Drawn strokes={out} />;
};

export default Clock;
