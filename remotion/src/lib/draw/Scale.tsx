/**
 * `<Scale/>` —— 一架天平。
 *
 * 「权衡」「公平」「代价」这些词在画面上是一架天平：柱子、横梁、两只托盘。
 * `tilt` 是倾斜度（-1 左边沉 / 0 平 / 1 右边沉），镜头代码可以把帧喂进来做「慢慢倾斜」。
 * 托盘上放什么由镜头代码自己加（`<Coin/>`、`<Book/>` 都行）。
 *
 * 坐标：`x`/`y` 是**底座中点**（天平立在哪条地平线上），`w` 是横梁跨度。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface ScaleProps extends Inked {
  x: number;
  y: number;
  /** 横梁跨度（成片像素），缺省 520 */
  w?: number;
  /** 倾斜：-1 左沉、0 平、1 右沉；直接给角度（如 `tilt={12}`）也认，按 sin 折算 */
  tilt?: number;
  seed?: number;
}

export const Scale: React.FC<ScaleProps> = (props) => {
  const { x, y, w = 520, tilt = 0, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 151);
  const half = w / 2;
  const top = -w * 0.62;                     // 横梁离地的高度
  const u = w / 100;
  // 写手有时把「倾斜 11 度」直接当 `tilt` 传（见 2026-10-18 真机预览：0→11 被钳成
  // 瞬间满倾）。超出 -1~1 的按角度折算，让「慢慢斜」真的慢慢斜。
  const raw = Math.abs(tilt) > 1 ? Math.sin((tilt * Math.PI) / 180) : tilt;
  const t = Math.max(-1, Math.min(1, raw));
  const lx = x - half;                       // 横梁两端
  const rx = x + half;
  // 文档说「1 右沉」，所以 t>0 时**右端 y 变大（下沉）、左端变小（抬起）**。
  // 原先两行写反了：表格让镜头代码传正数把天平压向右边，画面上却左边沉
  // （2026-10-18 真机预览：面粉那一侧翘着）。
  const ly = y + top - t * half * 0.22;
  const ry = y + top + t * half * 0.22;
  const out: Stroke[] = [];

  // 底座 + 立柱
  out.push(b.line(x - 22 * u, y, x + 22 * u, y, undefined));
  out.push(b.poly([[x - 14 * u, y], [x, y + top], [x + 14 * u, y]], { color: ink.paper }));
  out.push(b.circle(x, y + top + 6 * u, 12 * u, { color: ink.accent }));
  // 横梁
  out.push(b.line(lx, ly, rx, ry));
  // 两只托盘：两条垂线 + 一个弧
  for (const [px, py] of [[lx, ly], [rx, ry]] as [number, number][]) {
    out.push(b.line(px, py, px, py + 44 * u));
    out.push(b.arc(px, py + 44 * u, 62 * u, 26 * u, 0, 180));
  }
  return <Drawn strokes={out} />;
};

export default Scale;
