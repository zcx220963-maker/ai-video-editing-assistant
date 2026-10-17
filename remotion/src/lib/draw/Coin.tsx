/**
 * `<Coin/>` —— 一枚钱。三款：单枚 / 一摞 / 一枚带符号的。
 *
 * 「财富」「成本」「贬值」这些词在画面上就是钱：一枚带内圈的圆，或一摞叠起来的圆。
 * 面值那种字由镜头代码自己写（那是文案）；这一件只给形状。
 *
 * 坐标：`x`/`y` 是**圆心**（`stack` 时是最下面那一枚的圆心），`r` 是半径。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface CoinProps extends Inked {
  x: number;
  y: number;
  /** 半径（成片像素），缺省 110 */
  r?: number;
  kind?: 'plain' | 'ring' | 'stack';
  /** `stack` 的枚数，缺省 4 */
  count?: number;
  seed?: number;
}

export const Coin: React.FC<CoinProps> = (props) => {
  const { x, y, r = 110, kind = 'ring', count = 4, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 127);
  const out: Stroke[] = [];
  const stack = kind === 'stack' ? Math.max(1, Math.min(8, count)) : 1;

  for (let i = 0; i < stack; i++) {
    const cy = y - i * r * 0.34;
    out.push(b.circle(x, cy, r * 2, { color: ink.paper }));
    if (kind === 'ring' || i === stack - 1) out.push(b.circle(x, cy, r * 1.5));
    if (kind === 'ring' && i === stack - 1) {
      // 币面上一个「¥」——两横一竖，能认出来就够
      out.push(b.line(x, cy - r * 0.42, x - r * 0.3, cy - r * 0.02),
               b.line(x, cy - r * 0.42, x + r * 0.3, cy - r * 0.02));
      out.push(b.line(x, cy - r * 0.12, x, cy + r * 0.42));
      out.push(b.line(x - r * 0.34, cy + r * 0.06, x + r * 0.34, cy + r * 0.06));
      out.push(b.line(x - r * 0.34, cy + r * 0.24, x + r * 0.34, cy + r * 0.24));
    }
  }
  return <Drawn strokes={out} />;
};

export default Coin;
