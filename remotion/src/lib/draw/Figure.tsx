/**
 * `<Figure/>` —— 一个简笔人。
 *
 * **它能做什么、不能做什么，先说清楚。** 它能画出**在做一件事的人**：站着、走着、
 * 指着、扛着东西、坐着、举手。它**不是**一张脸，也不指代任何一个具体的人——
 * 「马斯克」这三个字对应的那张脸，靠几笔线是画不出来的（那需要现成像素，
 * 而取材那三条路已经从产品里拿掉了）。要指一个人，画 `<Figure/>` 加一句名字标注，
 * 或者用 `<Face/>`（极简头像，配名字读作「这是某人」而不是「这长得像某人」）。
 *
 * 坐标：`x`/`y` 是**脚底中点**（人站在哪条地平线上），`h` 是身高。两者都是成片像素
 * ——镜头代码里 `useVideoConfig()` 给的宽高就是整帧，直接写。
 *
 * ```tsx
 * <Figure x={360} y={760} h={300} pose="carry" />                 // 扛东西的工人
 * <Figure x={1240} y={760} h={300} facing={-1} hold="tool" />      // 面向左、手里握着工具
 * ```
 *
 * 颜色不写死：不传 `ink` 就用整片那一档的字色与强调色（见 `_pen.tsx` 的纪律三）。
 * 要动起来就把帧算出来的值传进来——`lift`（整体升降）与 `lean`（前倾角度）是留给
 * 镜头代码的两个口子，零件本身不读帧。
 */
import React from 'react';
import { Drawn, filmInk, pen, type Inked, type Stroke } from './_pen';

export type FigurePose = 'stand' | 'walk' | 'point' | 'raise' | 'carry' | 'sit';

export interface FigureProps extends Inked {
  /** 脚底中点的 x（成片像素） */
  x: number;
  /** 脚底中点的 y */
  y: number;
  /** 身高，缺省 280 */
  h?: number;
  pose?: FigurePose;
  /** 面朝：1 向右（缺省）、-1 向左 */
  facing?: 1 | -1;
  /** 手里/身前的附作物，缺省不带 */
  hold?: 'none' | 'tool' | 'banner' | 'book';
  /** 头是不是实心的（实心头在远处读起来更醒目） */
  headFill?: 'none' | 'accent' | 'paper';
  /** 整体升降（像素，正值向上）——让镜头代码把帧喂进来 */
  lift?: number;
  /** 前倾角度（度，正值向前） */
  lean?: number;
  /** 同一帧里出现好几个 `<Figure/>` 时给不同的数，线条不重样 */
  seed?: number;
}

const poses: Record<FigurePose, {
  /** 手的位置（身高百分比；x 正＝朝前） */
  hands: [number, number][];
  /** 腿：[膝, 脚] 两组，各是 [x, y] 百分比 */
  legs: [number, number][][];
}> = {
  stand: { hands: [[-15, -45], [15, -45]], legs: [[[-6, -24], [-10, 0]], [[6, -24], [10, 0]]] },
  walk: { hands: [[-17, -52], [12, -58]], legs: [[[-4, -23], [-16, -2]], [[8, -27], [14, 0]]] },
  point: { hands: [[-13, -46], [30, -80]], legs: [[[-6, -24], [-11, 0]], [[6, -24], [11, 0]]] },
  raise: { hands: [[-14, -46], [17, -99]], legs: [[[-6, -24], [-11, 0]], [[6, -24], [11, 0]]] },
  carry: { hands: [[20, -66], [22, -60]], legs: [[[-7, -24], [-12, 0]], [[7, -24], [12, 0]]] },
  sit: { hands: [[-14, -54], [15, -56]], legs: [[[-2, -32], [-16, -30]], [[2, -32], [16, -30]]] },
};

export const Figure: React.FC<FigureProps> = (props) => {
  const {
    x, y, h = 280, pose = 'stand', facing = 1, hold = 'none',
    headFill = 'none', lift = 0, lean = 0, seed = 1,
  } = props;
  const ink = filmInk(props.ink);
  const b = pen(ink, seed + 11);
  const p = h / 100;                       // 1 个「身高百分点」
  const rad = (lean * Math.PI) / 180;
  // 以脚底为原点的局部坐标 → 成片像素：先绕脚底前倾，再镜像，再平移到 (x, y)
  const at = (px: number, py: number): [number, number] => {
    const rx = px * Math.cos(rad) - py * Math.sin(rad);
    const ry = px * Math.sin(rad) + py * Math.cos(rad);
    return [x + facing * rx * p, y + ry * p - lift];
  };
  const put = (points: [number, number][]) => points.map(([a, c]) => at(a, c));

  const fillOf = (kind: 'none' | 'accent' | 'paper') =>
    kind === 'none' ? undefined : { color: kind === 'accent' ? ink.accent : ink.paper };
  const { hands, legs } = poses[pose];
  const out: Stroke[] = [];

  // 腿：髋 → 膝 → 脚，两条
  for (const [knee, foot] of legs) {
    const [hx, hy] = at(0, -50);
    const [kx, ky] = at(knee[0], knee[1]);
    const [fx2, fy2] = at(foot[0], foot[1]);
    out.push(b.line(hx, hy, kx, ky), b.line(kx, ky, fx2, fy2));
  }
  // 脊
  const [nx, ny] = at(0, -78);
  const [wx, wy] = at(0, -50);
  out.push(b.line(nx, ny, wx, wy));
  // 臂：肩 → 手，两条
  const [sx, sy] = at(0, -74);
  for (const [hx, hy] of hands) {
    const [ex, ey] = at(hx, hy);
    out.push(b.line(sx, sy, ex, ey));
  }
  // 头（画在最后＝压在最上层，颈子那条线不会穿出头顶）
  const [cx, cy] = at(0, -87);
  out.push(b.circle(cx, cy, 18 * p, fillOf(headFill)));

  // 手里的那一件
  if (hold !== 'none') {
    const [hx, hy] = at(hands[1][0], hands[1][1]);
    if (hold === 'tool') {
      out.push(b.line(hx, hy, hx + facing * 22 * p, hy - 26 * p));
    } else if (hold === 'book') {
      out.push(b.rect(hx - 10 * p, hy - 6 * p, 22 * p, 15 * p, { color: ink.paper }));
    } else {
      const [topX, topY] = at(hands[1][0] + 12, hands[1][1] - 34);
      out.push(b.line(hx, hy, topX, topY), b.line(topX, topY, topX + facing * 30 * p, topY));
    }
  }
  // 坐着：补一条座面，否则那两条折线读不出「坐在哪」
  if (pose === 'sit') {
    const [a1, a2] = at(-20, -30);
    const [c1, c2] = at(20, -30);
    out.push(b.line(a1, a2, c1, c2));
  }
  return <Drawn strokes={out} />;
};

export default Figure;
