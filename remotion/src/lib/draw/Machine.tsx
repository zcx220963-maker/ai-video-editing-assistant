/**
 * `<Machine/>` —— 一台机器。四款：蒸汽机 / 冲床 / 机械臂 / 涡轮。
 *
 * 「工业革命」「机器换人」「流水线」这类镜头缺的从来不是色块，是**一台机器的轮廓**：
 * 锅炉是一条竖着的粗方块，飞轮是一个圆，烟囱比锅炉还高——观众要的就是这三条。
 * 所以这一件给的是**能被认出来的剪影**，不是机械制图。
 *
 * 坐标：`x`/`y` 是**落地点中点**（机器站在哪条地平线上），`w` 是整机宽度。
 * 零件不读帧：要它动，由镜头代码把帧算成 props 传进来（`<Machine smoke={...}/>`）。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Fill, type Inked, type Stroke } from './_pen';

export type MachineKind = 'steam' | 'press' | 'arm' | 'turbine';

export interface MachineProps extends Inked {
  x: number;
  y: number;
  /** 整机宽度（成片像素），缺省 520 */
  w?: number;
  kind?: MachineKind;
  /** 烟有多浓：0~1（只对 `steam` 有意义），镜头代码可以把帧喂进来 */
  smoke?: number;
  seed?: number;
}

export const Machine: React.FC<MachineProps> = (props) => {
  const { x, y, w = 520, kind = 'steam', smoke = 0.6, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 37);
  const u = w / 100;                     // 局部坐标一律用「整机宽度的百分之几」
  const out: Stroke[] = [];
  // 局部坐标（原点＝落地点中点，+y 向下）→ 成片像素
  const ln = (ax: number, ay: number, bx: number, by: number) =>
    b.line(x + ax * u, y + ay * u, x + bx * u, y + by * u);
  const rc = (px: number, py: number, pw: number, ph: number, fill?: Fill) =>
    b.rect(x + px * u, y + py * u, pw * u, ph * u, fill);
  const cc = (cx: number, cy: number, d: number, fill?: Fill) =>
    b.circle(x + cx * u, y + cy * u, d * u, fill);

  if (kind === 'steam') {
    out.push(rc(-50, -9, 100, 9));
    out.push(rc(-46, -62, 58, 53, { color: ink.paper }));      // 锅炉
    out.push(rc(-40, -146, 15, 84));                           // 烟囱
    out.push(rc(-43, -151, 21, 7, { color: ink.accent }));     // 囱口
    const puffs = Math.round(1 + smoke * 3);
    for (let i = 0; i < puffs; i++) {
      out.push(cc(-32 - i * 6, -158 - i * 11, (14 + i * 8) * (0.7 + smoke * 0.3)));
    }
    out.push(cc(28, -40, 54, { color: ink.paper }));           // 飞轮
    out.push(cc(28, -40, 12, { color: ink.accent }));          // 轮毂
    out.push(ln(2, -40, 12, -40));                             // 活塞杆
    out.push(ln(2, -52, 2, -28));                              // 汽缸
  } else if (kind === 'press') {
    out.push(rc(-50, -7, 100, 7));
    out.push(rc(-46, -86, 92, 14, { color: ink.paper }));      // 顶梁
    out.push(rc(-42, -80, 12, 73));                            // 左立柱
    out.push(rc(30, -80, 12, 73));                             // 右立柱
    out.push(rc(-18, -66, 36, 30, { color: ink.accent }));     // 冲头
    out.push(ln(-30, -35, 30, -35));                           // 工作台
    out.push(rc(-14, -30, 28, 23, { color: ink.paper }));      // 工件
  } else if (kind === 'arm') {
    out.push(rc(-24, -14, 48, 14, { color: ink.paper }));      // 底座
    out.push(ln(0, -14, 34, -62));                             // 大臂
    out.push(cc(0, -14, 14, { color: ink.accent }));           // 肩关节点
    out.push(ln(34, -62, 78, -46));                            // 小臂
    out.push(cc(34, -62, 13, { color: ink.accent }));          // 肘关节点
    out.push(ln(78, -46, 88, -54), ln(78, -46, 88, -38));      // 夹爪
  } else {
    out.push(cc(0, -46, 92, { color: ink.paper }));
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2;
      out.push(ln(Math.cos(a) * 14, -46 + Math.sin(a) * 14,
                  Math.cos(a) * 44, -46 + Math.sin(a) * 44));
    }
    out.push(cc(0, -46, 26, { color: ink.accent }));
    out.push(rc(-34, -7, 68, 7));
  }
  // 地平线画在最后：它是「站在地上」那一条，压在所有件下面读起来才稳
  out.push(b.line(x - 56 * u, y, x + 56 * u, y, undefined));
  return <Drawn strokes={out} />;
};

export default Machine;
