/**
 * `<Face/>` —— 极简头像，用来**指代一个具体的人**。
 *
 * **说清楚它做不到什么**：它画不出「像某个人」的那张脸。几笔线画不出五官的个体差异，
 * 而唯一能给出真实长相的路（真人的照片/画像像素）已经从产品里拿掉了。所以它的用法是
 * **指代**，不是**写实**：画一个头像，旁边（或下面）写名字——观众读到的是
 * 「这一镜说的是这个人」，而不是「这个头像长得像那个人」。
 *
 * 要表达「一个在做事的普通人」用 `<Figure/>`（有身体，能走能扛）。
 * 这一件只有头：适合讲话人、名人、证件照那一类**以一个人为焦点**的画面。
 *
 * 坐标：`x`/`y` 是**头的中心**，`size` 是头的宽度（成片像素）。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface FaceProps extends Inked {
  x: number;
  y: number;
  /** 头宽（成片像素），缺省 200 */
  size?: number;
  /** 表情：平静 / 微笑 / 严肃 */
  mood?: 'calm' | 'smile' | 'stern';
  /** 头上的那一件：无 / 帽子 / 学位帽 / 王冠 */
  hat?: 'none' | 'cap' | 'mortar' | 'crown';
  glasses?: boolean;
  beard?: boolean;
  seed?: number;
  /** 整体上下浮动（像素，正值向上），留给镜头代码喂帧 */
  lift?: number;
}

export const Face: React.FC<FaceProps> = (props) => {
  const {
    x, y, size = 200, mood = 'calm', hat = 'none',
    glasses = false, beard = false, seed = 1, lift = 0,
  } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 23);
  const s = size;
  const cy = y - lift;
  const out: Stroke[] = [];

  // 颈与肩：没有它们，一个孤零零的头读起来像贴纸
  const half = s / 2;
  out.push(b.line(x, cy + half * 1.05, x, cy + half * 1.35));
  out.push(b.line(x - half * 0.85, cy + half * 1.62, x + half * 0.85, cy + half * 1.62));
  // 头
  out.push(b.ellipse(x, cy, s, s * 1.16, { color: ink.paper }));
  // 耳
  out.push(b.arc(x - half * 0.98, cy + s * 0.02, s * 0.2, s * 0.24, 90, 270));
  out.push(b.arc(x + half * 0.98, cy + s * 0.02, s * 0.2, s * 0.24, -90, 90));
  // 头发（贴着头顶的一道弧）
  out.push(b.arc(x, cy - s * 0.12, s * 0.96, s * 0.62, 200, 340));
  // 眉
  const brow = mood === 'stern' ? 0.05 : 0;
  out.push(b.line(x - s * 0.26, cy - s * 0.16 + s * brow, x - s * 0.09, cy - s * 0.19 - s * brow));
  out.push(b.line(x + s * 0.09, cy - s * 0.19 - s * brow, x + s * 0.26, cy - s * 0.16 + s * brow));
  // 眼
  out.push(b.circle(x - s * 0.17, cy - s * 0.06, s * 0.055, { color: ink.stroke }));
  out.push(b.circle(x + s * 0.17, cy - s * 0.06, s * 0.055, { color: ink.stroke }));
  // 鼻
  out.push(b.line(x, cy - s * 0.02, x - s * 0.03, cy + s * 0.12));
  // 嘴
  if (mood === 'smile') out.push(b.arc(x, cy + s * 0.13, s * 0.28, s * 0.16, 20, 160));
  else if (mood === 'stern') out.push(b.arc(x, cy + s * 0.26, s * 0.28, s * 0.16, 200, 340));
  else out.push(b.line(x - s * 0.13, cy + s * 0.2, x + s * 0.13, cy + s * 0.2));

  if (glasses) {
    out.push(b.circle(x - s * 0.17, cy - s * 0.06, s * 0.24));
    out.push(b.circle(x + s * 0.17, cy - s * 0.06, s * 0.24));
    out.push(b.line(x - s * 0.05, cy - s * 0.06, x + s * 0.05, cy - s * 0.06));
  }
  if (beard) {
    out.push(b.arc(x, cy + s * 0.28, s * 0.44, s * 0.34, 20, 160));
  }
  if (hat === 'cap') {
    out.push(b.arc(x, cy - s * 0.32, s * 0.9, s * 0.7, 180, 360, true, { color: ink.accent }));
    out.push(b.line(x - s * 0.5, cy - s * 0.32, x + s * 0.34, cy - s * 0.32));
  } else if (hat === 'mortar') {
    out.push(b.poly([[x - s * 0.62, cy - s * 0.5], [x, cy - s * 0.72], [x + s * 0.62, cy - s * 0.5],
                     [x, cy - s * 0.3]], { color: ink.accent }));
    out.push(b.line(x + s * 0.5, cy - s * 0.56, x + s * 0.5, cy - s * 0.2));
  } else if (hat === 'crown') {
    out.push(b.poly([[x - s * 0.42, cy - s * 0.52], [x - s * 0.28, cy - s * 0.74],
                     [x, cy - s * 0.54], [x + s * 0.28, cy - s * 0.74],
                     [x + s * 0.42, cy - s * 0.52]], { color: ink.accent }));
  }
  return <Drawn strokes={out} />;
};

export default Face;
