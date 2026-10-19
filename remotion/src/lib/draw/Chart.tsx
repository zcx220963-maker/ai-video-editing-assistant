/**
 * `<Chart/>` —— 一张图：坐标轴 + 刻度 + 一条折线或一组柱。
 *
 * 「涨了 4 倍」「占比过半」这类文案，画面上要的是一条**看得出走向的线**。这一件给
 * 轴、刻度、数据三样，数字与标题仍由镜头代码自己写（那是文案，不是零件）。
 *
 * 坐标：`x`/`y` 是**绘图区左上角**（不含轴外的字），`w`/`h` 是绘图区大小。
 * `values` 是 0~1 的序列——它们**不标注数值**，只决定形状。
 *
 * ```tsx
 * <Chart x={300} y={420} w={900} h={420} kind="line" values={[0.1, 0.3, 0.9, 1]} />
 * ```
 */
import React from 'react';
import { Drawn, filmInk, pen, type Fill, type Inked, type Stroke } from './_pen';

export interface ChartProps extends Inked {
  x: number;
  y: number;
  w?: number;
  h?: number;
  kind?: 'line' | 'bar';
  /** 0~1 的序列；给几段就画几段 */
  values?: number[];
  /** 画到哪为止：0~1 的比例（推荐，镜头代码把帧喂进来）或点数（>1 时按点数） */
  upto?: number;
  /** Y 轴刻线几条，缺省 4 */
  ticks?: number;
  seed?: number;
}

export const Chart: React.FC<ChartProps> = (props) => {
  const {
    x, y, w = 900, h = 420, kind = 'line',
    values = [0.15, 0.35, 0.55, 0.85, 1], upto, ticks = 4, seed = 1,
  } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 101);
  const out: Stroke[] = [];
  const vs = values.length ? values : [0.5];
  // `upto` 两种写法都认：0~1 是「长到几成」（与 `<Arrow/>` 同一套，镜头代码直接喂帧），
  // >1 是「画到第几个点」。原先只认点数，`upto={grow}`（0~1）在整段动画里只画出 1 个点，
  // 曲线整条消失（2026-10-18 真机预览）。
  const u = upto ?? vs.length;
  const n = Math.max(1, Math.min(vs.length, u <= 1 ? Math.ceil(u * vs.length) : Math.round(u)));
  const rc = (px: number, py: number, pw: number, ph: number, fill?: Fill) =>
    b.rect(x + px, y + py, pw, ph, fill);
  const px = (i: number) => (i / Math.max(1, vs.length - 1)) * w;
  const py = (v: number) => h - Math.max(0, Math.min(1, v)) * h;

  for (let i = 0; i <= ticks; i++) {                            // Y 轴刻度线
    const yy = y + (i / ticks) * h;
    out.push(b.line(x - 12, yy, x, yy));
    if (i < ticks) out.push(b.line(x, yy, x + w, yy, undefined));
  }
  out.push(b.line(x, y, x, y + h));                             // Y 轴
  out.push(b.line(x, y + h, x + w + 10, y + h));                // X 轴

  if (kind === 'bar') {
    const slot = w / vs.length;
    const bw = slot * 0.5;
    for (let i = 0; i < n; i++) {
      const top = py(vs[i]);                                    // 相对绘图区的上缘
      out.push(rc(i * slot + (slot - bw) / 2, top, bw, h - top,
                  { color: i === n - 1 ? ink.accent : ink.paper }));
    }
  } else {
    const pts: [number, number][] = [];
    for (let i = 0; i < n; i++) pts.push([x + px(i), y + py(vs[i])]);
    if (pts.length > 1) out.push(b.curve(pts));
    for (const [cx, cy] of pts) out.push(b.circle(cx, cy, 14, { color: ink.accent }));
  }
  return <Drawn strokes={out} />;
};

export default Chart;
