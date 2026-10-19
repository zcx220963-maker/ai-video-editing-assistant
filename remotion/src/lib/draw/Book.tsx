/**
 * `<Book/>` —— 一本书。合着的与摊开的两种。
 *
 * 「知识」「典籍」「笔记」这些词需要一件器物落地，那就是书：合着的是一个带书脊的
 * 方块，摊开的是两页带文字的纸。书名那行字由镜头代码自己写（那是文案）。
 *
 * 坐标：`x`/`y` 是**书的下缘中点**（摊开时是两页交界处），`w` 是宽度。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export interface BookProps extends Inked {
  x: number;
  y: number;
  /** 宽度（成片像素），缺省 260 */
  w?: number;
  open?: boolean;
  /** 摊开时翻起来的那一页转到哪（0~1），镜头代码可以把帧喂进来 */
  flip?: number;
  seed?: number;
}

export const Book: React.FC<BookProps> = (props) => {
  const { x, y, w = 260, open = false, flip = 0, seed = 1 } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 139);
  const h = w * 1.34;                        // 合着时的高度
  const out: Stroke[] = [];
  const ln = (ax: number, ay: number, bx: number, by: number) =>
    b.line(x + ax, y + ay, x + bx, y + by);

  if (!open) {
    out.push(b.rect(x - w / 2, y - h, w, h, { color: ink.paper }));
    out.push(b.line(x - w / 2 + w * 0.16, y - h, x - w / 2 + w * 0.16, y));  // 书脊
    out.push(b.line(x - w * 0.24, y - h * 0.72, x + w * 0.3, y - h * 0.72));
    out.push(b.line(x - w * 0.24, y - h * 0.56, x + w * 0.16, y - h * 0.56));
    out.push(b.rect(x - w * 0.24, y - h * 0.4, w * 0.34, h * 0.16, { color: ink.accent }));
  } else {
    // 两页：各自是一个向上收一点的四边形，交线在正中
    const half = w / 2;
    const top = -h * 0.46;
    out.push(b.poly([[-half, 0], [-half * 0.92, top], [0, top * 0.84], [0, 0]],
                    { color: ink.paper }));
    out.push(b.poly([[half, 0], [half * 0.92, top], [0, top * 0.84], [0, 0]],
                    { color: ink.paper }));
    for (let i = 0; i < 4; i++) {
      const yy = top * 0.72 + i * h * 0.09;
      out.push(ln(-half * 0.72, yy, -half * 0.2, yy + h * 0.02));
      out.push(ln(half * 0.2, yy + h * 0.02, half * 0.72, yy));
    }
    // 翻起来的那一页：以交线为轴，按 flip 转到另一侧
    if (flip > 0) {
      const k = Math.min(1, flip);
      const tip = half * (1 - 2 * k);
      out.push(b.poly([[0, 0], [0, top * 0.84], [tip * 1.05, top * 0.9], [tip, 0]],
                      { color: ink.accent }));
    }
  }
  return <Drawn strokes={out} />;
};

export default Book;
