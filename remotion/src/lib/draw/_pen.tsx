/**
 * 简笔画零件库的**笔** —— `@dg/lib/draw/*` 下所有件共用的这一层。
 *
 * **为什么单独一层。** 手绘线的算法只有一处（roughjs 的 generator，纯计算、不碰
 * DOM），零件只回答「这个东西由哪几笔构成」。零件文件因此每支都只有几十行，全是
 * 几何——「蒸汽机 = 锅炉 + 烟囱 + 飞轮 + 活塞杆」这种拆法写在零件里，
 * 「怎么把它画成一条抖动的线」写在笔里，两件事不搅在一起。
 *
 * **三条纪律，与镜头代码同一套：**
 *
 * 1. **时刻不从这里来。** 零件是纯函数（props → 几何），没有 `useCurrentFrame()`。
 *    要让某个部件动，由镜头代码把帧算出来的值当 props 传进来（`spin` / `lift` /
 *    `swing`）。这样同一支零件既能静止摆着，也能被转起来，而「谁在动」这个问题
 *    的答案永远在镜头代码里。
 * 2. **随机必须给 seed。** roughjs 不给 seed 就用 `Math.random`，逐帧渲染会**抖**
 *    （画面像有噪点，且两次渲染不一致）。这里每一笔的 seed 都由「零件内的笔序号 +
 *    零件自己的 `seed` 基数」拼出来，缺省基数固定，所以同一份代码渲两次逐像素相同。
 * 3. **颜色不写死。** 缺省从整片那一档取（`resolveFilmStyle`），与镜头代码同一处
 *    ——用户在计划卡上换色板，这里画的每一件跟着换。要覆盖就传 `ink`。
 *
 * 坐标系就是**成片设备像素**：与镜头代码写在同一个 `<svg>` 里，位置直接给像素。
 */
import React from 'react';
import { getInputProps } from 'remotion';
import rough from 'roughjs';
import { resolveFilmStyle } from '@dg/film';
import { TOKENS } from '@dg/tokens';

const gen = rough.generator();

/** 一件东西能画得出来的那几笔，用的是 roughjs 的 Drawable（纯数据）。 */
export type Stroke = Parameters<typeof gen.toPaths>[0];

/** 零件能收的配色。**都可省**——省了就取整片那一档。 */
export interface Ink {
  /** 线色。缺省＝这一镜的字色（整片那一档给的 `ink`），再缺省＝`TOKENS.ink` */
  stroke?: string;
  /** 强调色（高亮的那一件、需要被指着的那一件）。缺省＝这一镜的强调色 */
  accent?: string;
  /** 纸色（浅色填充块）。缺省＝这一镜的纸色 */
  paper?: string;
  /** 线宽（成片像素）。缺省 3 */
  strokeWidth?: number;
  /** 手绘抖动：0 是尺子画的直线，1.4 是缺省的自由手 */
  roughness?: number;
}

/**
 * 零件共有的那一样东西：可省的配色。每一件的 props 都 `extends Inked`，
 * 所以调用写法处处相同——`<Gear x={…} y={…} r={…} ink={{ accent: … }} />`。
 */
export interface Inked {
  /** 配色覆盖：写了的字段盖过整片那一档，没写的不动。**一般不传** */
  ink?: Ink;
}

/** 解析之后的配色——零件内部拿到的是这一份，不再有「可省」这回事。 */
export interface ResolvedInk {
  stroke: string;
  accent: string;
  paper: string;
  strokeWidth: number;
  roughness: number;
}

/** 零件能收的填充方式：不给＝只描线，给了＝填（`hachure` 是手绘斜纹）。 */
export type Fill = { color: string; hachure?: boolean; gap?: number } | undefined;

/**
 * `Ink`（可省）→ 解析好的配色。
 *
 * 镜头代码调用零件时一般不传 `ink`：整片那一档已经规定了这条片子长什么样，
 * 零件与画面里别的东西因此天然同色。要**强调**某一件（「就是它」）才传
 * `ink={{ accent: … }}` 换个色。
 */
export function filmInk(ink: Ink = {}): ResolvedInk {
  const film = resolveFilmStyle(getInputProps());
  return {
    stroke: ink.stroke ?? film.ink ?? TOKENS.ink,
    accent: ink.accent ?? film.accent ?? TOKENS.accent,
    paper: ink.paper ?? film.paper ?? TOKENS.paper,
    strokeWidth: ink.strokeWidth ?? 3,
    roughness: ink.roughness ?? 1.4,
  };
}

/** 一支笔：`seed` 每次取用**自增**，所以同一支东西里没有两笔用同一个种子。 */
export interface Pen {
  line(x1: number, y1: number, x2: number, y2: number, fill?: Fill): Stroke;
  rect(x: number, y: number, w: number, h: number, fill?: Fill): Stroke;
  circle(x: number, y: number, d: number, fill?: Fill): Stroke;
  ellipse(x: number, y: number, w: number, h: number, fill?: Fill): Stroke;
  poly(points: [number, number][], fill?: Fill): Stroke;
  curve(points: [number, number][], fill?: Fill): Stroke;
  arc(x: number, y: number, w: number, h: number, start: number, stop: number,
      closed?: boolean, fill?: Fill): Stroke;
}

/**
 * `pen(ink, seed)` —— 这一件东西的笔。零件里只做三件事：起笔、画几笔、交给
 * `<Drawn/>`。**每一笔自带 seed**，所以零件里不必再管随机。
 */
export function pen(ink: ResolvedInk, seed = 1): Pen {
  let n = seed * 1009;
  const next = () => (n += 1);
  const opts = (fill: Fill) => ({
    seed: next(),
    roughness: ink.roughness,
    stroke: ink.stroke,
    strokeWidth: ink.strokeWidth,
    fill: fill?.color,
    fillStyle: fill?.hachure ? 'hachure' : 'solid',
    hachureGap: fill?.gap ?? 9,
    bowing: 1,
    curveFitting: 0.95,
  });
  return {
    line: (x1, y1, x2, y2, fill) => gen.line(x1, y1, x2, y2, opts(fill)),
    rect: (x, y, w, h, fill) => gen.rectangle(x, y, w, h, opts(fill)),
    circle: (x, y, d, fill) => gen.circle(x, y, d, opts(fill)),
    ellipse: (x, y, w, h, fill) => gen.ellipse(x, y, w, h, opts(fill)),
    poly: (points, fill) => gen.polygon(points, opts(fill)),
    curve: (points, fill) => gen.curve(points, opts(fill)),
    arc: (x, y, w, h, start, stop, closed, fill) =>
      gen.arc(x, y, w, h, start, stop, closed ?? false, opts(fill)),
  };
}

/**
 * 把几笔落成 SVG。零件全都以它收尾。
 *
 * 线宽为零的那几条路径退回 0.6：roughjs 的填充路径在 `strokeWidth: 0` 下会渲成一串
 * 看不见的疙瘩，而「只要填、不要边」是一种正当写法（`ink={{ strokeWidth: 0 }}`
 * 加一个填色）。0.6px 在成片上就是一条发丝边，读起来仍是纯色块。
 */
export function Drawn(props: { strokes: Stroke[] }): React.ReactElement {
  const paths: React.ReactNode[] = [];
  props.strokes.forEach((s, i) => {
    gen.toPaths(s).forEach((p, j) => {
      paths.push(
        <path
          key={`${i}-${j}`}
          d={p.d}
          stroke={p.stroke}
          strokeWidth={p.strokeWidth || 0.6}
          fill={p.fill ?? 'none'}
          strokeLinecap="round"
          strokeLinejoin="round"
        />,
      );
    });
  });
  return <g>{paths}</g>;
}
