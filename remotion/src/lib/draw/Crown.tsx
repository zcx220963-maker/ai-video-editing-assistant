/**
 * `<Crown/>` —— 一顶王冠。
 *
 * 「地位」「权力」「顶端」这些词需要一件一眼认得的象征物，王冠是其中最省笔墨的那个：
 * 一条底带 + 三个尖 + 几颗宝石。谁戴它由镜头代码决定——放在 `<Face hat="crown"/>`
 * 头上，或者单独摆在画面里当作「冠军」「第一」的符号。
 *
 * 坐标：`x`/`y` 是**底带下缘中点**，`w` 是宽度。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface CrownProps extends Inked {
  x: number;
  y: number;
  /** 宽度（成片像素），缺省 300 */
  w?: number;
  /** 尖的个数：3 或 5，缺省 3 */
  points?: 3 | 5;
  /** 带宝石（尖顶上的小球） */
  gems?: boolean;
  seed?: number;
}

export const Crown: React.FC<CrownProps> = (props) => {
  const { x, y, w = 300, points = 3, gems = true, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 179);
  const h = w * 0.52;
  const out: Stroke[] = [];
  const at = (px: number, py: number): [number, number] => [x + px * w, y + py * h];

  // 底带
  out.push(b.rect(x - w / 2, y - h * 0.34, w, h * 0.34, { color: ink.accent }));
  // 尖：底带上缘 → 顶点 → 底带上缘，一个一个连成一条折线
  const pts: [number, number][] = [];
  const n = points;
  for (let i = 0; i <= n; i++) {
    const px = -0.5 + i / n;
    pts.push(at(px, -0.34));
    if (i < n) pts.push(at(px + 1 / n / 2, -1));
  }
  out.push(b.poly(pts, { color: ink.paper }));
  if (gems) {
    for (let i = 0; i < n; i++) {
      const [gx, gy] = at(-0.5 + (i + 0.5) / n, -1);
      out.push(b.circle(gx, gy, w * 0.055, { color: ink.accent }));
    }
    out.push(b.circle(x, y - h * 0.17, w * 0.06, { color: ink.stroke }));
  }
  return <Drawn strokes={out} />;
};

export default Crown;
