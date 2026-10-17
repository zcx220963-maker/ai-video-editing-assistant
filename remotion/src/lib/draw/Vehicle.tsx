/**
 * `<Vehicle/>` —— 一辆/一条载具。四款：手推车 / 汽车 / 火车头 / 船。
 *
 * 讲「运输」「铁路」「大航海」时，观众认的是**轮子与烟囱**：火车头是一个长条 + 一顶
 * 烟囱 + 三个轮，船是一段上宽下窄的船体 + 一根桅。这一件给的是这几条。
 *
 * 坐标：`x`/`y` 是**落地点中点**（轮子压着的那条线，船的吃水线），`w` 是长度。
 * `facing` 靠一个 `scale(-1,1)` 整件镜像——所以零件内部只需按「朝右」画一遍。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Fill, type Inked, type Stroke } from './_pen';

export type VehicleKind = 'cart' | 'car' | 'train' | 'ship';

export interface VehicleProps extends Inked {
  x: number;
  y: number;
  /** 长度（成片像素），缺省 480 */
  w?: number;
  kind?: VehicleKind;
  /** 车头朝哪边：1 向右（缺省）、-1 向左 */
  facing?: 1 | -1;
  seed?: number;
}

export const Vehicle: React.FC<VehicleProps> = (props) => {
  const { x, y, w = 480, kind = 'cart', facing = 1, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 71);
  const u = w / 100;                    // 局部坐标＝原点在落地点中点，按长度百分比
  const out: Stroke[] = [];
  const ln = (ax: number, ay: number, bx: number, by: number) => b.line(ax * u, ay * u, bx * u, by * u);
  const rc = (px: number, py: number, pw: number, ph: number, fill?: Fill) =>
    b.rect(px * u, py * u, pw * u, ph * u, fill);
  const cc = (cx: number, cy: number, d: number, fill?: Fill) => b.circle(cx * u, cy * u, d * u, fill);

  if (kind === 'cart') {
    rc(-34, -26, 44, 26, { color: ink.paper });                 // 斗
    cc(-24, -6, 14, { color: ink.paper });
    cc(2, -6, 14, { color: ink.paper });
    ln(10, -22, 40, -40);                                       // 把手
    ln(40, -40, 48, -36);
  } else if (kind === 'car') {
    out.push(b.poly([[-46, -20], [-40, -38], [-14, -38], [-4, -56], [18, -56],
                     [28, -38], [46, -38], [46, -20]], { color: ink.paper }));
    out.push(b.poly([[-12, -40], [14, -40], [20, -38], [-12, -38]], { color: ink.accent }));
    cc(-26, -18, 18, { color: ink.paper });
    cc(26, -18, 18, { color: ink.paper });
  } else if (kind === 'train') {
    rc(-46, -44, 84, 30, { color: ink.paper });                 // 车体
    rc(-44, -74, 22, 30, { color: ink.paper });                 // 前部
    rc(-40, -104, 12, 30);                                      // 烟囱
    rc(-43, -108, 18, 6, { color: ink.accent });
    rc(-14, -60, 22, 14, { color: ink.accent });                // 驾驶室窗
    cc(-32, -10, 20, { color: ink.paper });
    cc(-6, -10, 20, { color: ink.paper });
    cc(20, -10, 20, { color: ink.paper });
    ln(-52, 0, 52, 0);                                          // 轨
  } else {
    out.push(b.poly([[-50, -18], [-44, -30], [44, -30], [50, -18]], { color: ink.paper }));
    ln(-50, -18, 50, -18);
    ln(0, -30, 0, -86);                                         // 桅
    out.push(b.poly([[2, -84], [34, -46], [2, -46]], { color: ink.accent }));   // 帆
    for (let i = 0; i < 3; i++) {                               // 浪
      out.push(b.arc(-30 + i * 30, -12, 30, 12, 180, 360));
    }
  }
  out.push(b.line(-54 * u, 0, 54 * u, 0, undefined));
  return (
    <g transform={`translate(${x}, ${y}) scale(${facing}, 1)`}>
      <Drawn strokes={out} />
    </g>
  );
};

export default Vehicle;
