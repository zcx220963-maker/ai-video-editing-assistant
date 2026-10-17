/**
 * `<Building/>` —— 一栋建筑。四款：厂房 / 塔 / 民居 / 天际线。
 *
 * 「19 世纪的工业革命」那一镜里，厂房是**锯齿轮廓的屋顶 + 一根冒烟的烟囱**；
 * 「城市化」那一镜是**一排越来越高的方块**。这一件给的就是这些能被一眼认出的轮廓。
 *
 * 坐标：`x`/`y` 是**地面线上的中点**（建筑立在哪条地平线上），`w` 是宽度。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Fill, type Inked, type Stroke } from './_pen';

export type BuildingKind = 'factory' | 'tower' | 'house' | 'skyline';

export interface BuildingProps extends Inked {
  x: number;
  y: number;
  /** 宽度（成片像素），缺省 620；`skyline` 时是一整排的总宽 */
  w?: number;
  kind?: BuildingKind;
  /** 烟囱冒不冒烟：0~1（只对 `factory` 有意义） */
  smoke?: number;
  seed?: number;
}

export const Building: React.FC<BuildingProps> = (props) => {
  const { x, y, w = 620, kind = 'factory', smoke = 0.6, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 53);
  const u = w / 100;
  const out: Stroke[] = [];
  const ln = (ax: number, ay: number, bx: number, by: number) =>
    b.line(x + ax * u, y + ay * u, x + bx * u, y + by * u);
  const rc = (px: number, py: number, pw: number, ph: number, fill?: Fill) =>
    b.rect(x + px * u, y + py * u, pw * u, ph * u, fill);
  const cc = (cx: number, cy: number, d: number, fill?: Fill) =>
    b.circle(x + cx * u, y + cy * u, d * u, fill);
  const win = (px: number, py: number, s: number) =>
    rc(px, py, s, s, { color: ink.accent });

  if (kind === 'factory') {
    rc(-50, -52, 100, 52, { color: ink.paper });               // 主体
    for (let i = 0; i < 4; i++) {                              // 锯齿屋顶
      const x0 = -50 + i * 25;
      out.push(b.poly([
        [x + x0 * u, y - 52 * u], [x + (x0 + 25) * u, y - 52 * u],
        [x + (x0 + 25) * u, y - 74 * u],
      ], { color: ink.paper }));
    }
    rc(28, -140, 16, 66);                                      // 烟囱
    const puffs = Math.round(1 + smoke * 3);
    for (let i = 0; i < puffs; i++) {
      out.push(cc(36 + i * 6, -152 - i * 11, (14 + i * 8) * (0.7 + smoke * 0.3)));
    }
    for (let i = 0; i < 3; i++) win(-42 + i * 22, -40, 11);    // 窗
    for (let i = 0; i < 3; i++) win(-42 + i * 22, -22, 11);
    rc(-6, -20, 14, 20, { color: ink.paper });                 // 门
  } else if (kind === 'tower') {
    out.push(b.poly([[-16, 0], [-10, -96], [10, -96], [16, 0]], { color: ink.paper }));
    for (let i = 1; i <= 4; i++) {
      const t = i / 5;
      ln(-13 - t * 3, -96 * t, 13 + t * 3, -96 * t);
    }
    ln(0, -96, 0, -118);
    cc(0, -120, 6, { color: ink.accent });
  } else if (kind === 'house') {
    rc(-32, -40, 64, 40, { color: ink.paper });
    out.push(b.poly([[-38, -40], [0, -70], [38, -40]], { color: ink.accent }));
    rc(-9, -22, 18, 22, { color: ink.paper });
    win(-24, -34, 10);
    win(14, -34, 10);
  } else {
    const tops = [-70, -104, -56, -88, -46];
    const bw = 100 / tops.length;
    tops.forEach((top, i) => {
      const x0 = -50 + i * bw + 1.5;
      rc(x0, top, bw - 3, -top, { color: ink.paper });
      const rows = Math.floor(-top / 20);
      for (let r = 1; r <= rows; r++) {
        win(x0 + bw * 0.22, top + r * 18, 4);
        win(x0 + bw * 0.58, top + r * 18, 4);
      }
    });
  }
  out.push(b.line(x - 54 * u, y, x + 54 * u, y, undefined));
  return <Drawn strokes={out} />;
};

export default Building;
