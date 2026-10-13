import React from 'react';
import {
  AbsoluteFill,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import { fitText as remotionFitText } from '@remotion/layout-utils';
import {
  ASPECTS,
  ASSET_LOG_MARKER,
  FIELD_LOG_MARKER,
  FIELD_PROBE_TAIL,
  FILM_GEOMETRY,
  LAYOUT_LOG_MARKER,
  LAYOUT_PROBE,
  TOKENS,
  TOKEN_LOG_MARKER,
} from './tokens';
import type { Aspect, SubtitleMode } from './tokens';

/** 镜头代码能拿到的画面带：成片设备像素，左上角是 (0,0)。 */
export interface BodyBox {
  x: number;
  y: number;
  w: number;
  h: number;
  aspect: Aspect;
}

/**
 * `const body = useBody()` —— 这一镜能画的那条带子到底多大。
 *
 * 为什么必须有这个口子：母版按画幅裁出画面带后用 `overflow:hidden` clip，
 * 而**带子尺寸从来没人告诉镜头代码**。模型只能猜——它猜了 `viewBox="0 0 1080 620"`
 * 并且 12 镜全照抄，横屏那条带子是 1800×732，于是每个母题都被压成画面约 9% 的
 * 一小条。有了这个盒，镜头代码写 `viewBox={`0 0 ${body.w} ${body.h}`}` 就够了：
 * **坐标系已经是设备像素，没有 `u` 可乘**（`aspect` 本来就在缓存键里，
 * 横竖屏的同一份代码是两个缓存条目，不会互串）。
 *
 * 为什么是纯计算而不是 React context：镜头组件写在 `<FilmLayout>` 的**外面**
 * （`return <FilmLayout>…</FilmLayout>`），provider 在它下面，那一层根本取不到盒。
 * 这里读的是与母版同一张 `FILM_GEOMETRY` 和同一个 `useVideoConfig()` 宽高，
 * 所以「镜头以为的带子」与「母版真正 clip 出来的带子」在结构上是同一个数。
 */
export function useBody(aspect?: Aspect): BodyBox {
  const { width, height } = useVideoConfig();
  return filmBody(width, height, aspect);
}

/** `useBody()` 的无钩子版本，母版自己也用它一次，于是两侧不可能算出两个数。 */
export function filmBody(width: number, height: number, aspect?: Aspect): BodyBox {
  const key = aspect ?? aspectFor(width, height);
  const g = FILM_GEOMETRY[key];
  return {
    x: g.gutter,
    y: g.topSafe,
    w: width - 2 * g.gutter,
    h: height - g.topSafe - g.bottomSafe,
    aspect: key,
  };
}

/** 从成片像素尺寸反推画幅：表以三种标准尺寸为准则，取最接近的那个。 */
function aspectFor(width: number, height: number): Aspect {
  let best: Aspect = 'portrait';
  let bestGap = Infinity;
  for (const key of Object.keys(ASPECTS) as Aspect[]) {
    const a = ASPECTS[key];
    const gap = Math.abs(a.width / a.height - width / height)
      + Math.abs(a.width - width) / a.width;
    if (gap < bestGap) {
      bestGap = gap;
      best = key;
    }
  }
  return best;
}

/** TTS 词级时间戳（帧）。缺省时整句从第 0 帧就在，高亮词也直接点亮。 */
export interface WordTiming {
  text: string;
  startFrame: number;
}

export interface FilmLayoutProps {
  /** 这一镜要说的一句话 —— 只进字幕带，绝不进镜头画面 */
  text: string;
  /** 逐词点亮的词，每个都必须出现在 text 里；≤4 个 */
  highlight?: string[];
  /** 词级时间戳，由 Python 侧按真实语音时长算好后注入 */
  words?: WordTiming[];
  subtitleMode?: SubtitleMode;
  aspect?: Aspect;
  /** 镜头代码填进画面带的主体内容 */
  children?: React.ReactNode;
}

/** `<F>` 的两个属性。 */
export interface FieldMarkProps {
  /** 必须与分镜 `fields[].name` 同名 —— 闸① 的对账规则判的就是这层对应关系 */
  name: string;
  /** 必须写注入的那份值本身（`fields.<name>`），不许在这里填别的字面量：
   *  闸⑥ 比的就是「代码说它画上的是几」与「我们注入的是几」 */
  value: unknown;
  children?: React.ReactNode;
}

/** 标注值进 DOM 的形态：JSON 文本，保住类型。
 *  数字 40 与字符串 "40" 在界面上是两种东西（滑杆 vs 单行框），闸⑥ 也是两种比法，
 *  所以不能一律 `String(value)` 掉了类型。`undefined` 归成 `null`：DOM 属性只能
 *  回读成字符串，留一个"读不出来的写法"不如留一个明确没有值。 */
function markValue(value: unknown): string {
  return JSON.stringify(value === undefined ? null : value);
}

/**
 * `<F name="radius" value={fields.radius}>…</F>` —— 字段标注。
 *
 * 为什么用 cloneElement 把属性挂到子元素上，而不是老老实实包一层容器：
 * 包一层要不成盒才不改布局（`display: contents`），而不成盒的元素自己
 * `getBoundingClientRect()` 是空的，量到的盒就得绕路；属性直接落在真元素上，
 * 量它就是量那块画面。SVG 图元（`<circle>` 等）也吃 `data-*`，所以图示与 3D
 * 之外的 2D 通道都能标。
 *
 * 子节点不是单个可克隆元素时（比如裸文本）退化成 `display: contents` 的 span，
 * 由 `measureBox` 走 Range 那条路量子内容——两种写法都拿得到盒，只是第二条绕一点。
 *
 * 标在自定义组件上（`<F …><MyThing/></F>`）时属性会被 MyThing 吃掉（它没往下传就
 * 没有 data-dg-field），回读表里这个字段就是 `no_geometry`。闸⑥ 不为此打红
 * （画面是对的，只是点不到），但契约文档教的是标在真元素上。
 */
export const F: React.FC<FieldMarkProps> = ({ name, value, children }) => {
  const kids = React.Children.toArray(children);
  const only = kids.length === 1 ? kids[0] : null;
  if (only !== null && React.isValidElement(only)) {
    return React.cloneElement(only as React.ReactElement<Record<string, unknown>>, {
      'data-dg-field': name,
      'data-dg-value': markValue(value),
    });
  }
  return (
    <span
      data-dg-field={name}
      data-dg-value={markValue(value)}
      style={{ display: 'contents' }}
    >
      {children}
    </span>
  );
};

/** 成片像素坐标系里的一个盒，左上角 (x,y)。 */
interface PxBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 一个标注元素在成片像素坐标系里的盒；量不到返回 null（不是 0×0）。 */
function measureBox(el: Element): PxBox | null {
  const round = (n: number) => Math.round(n * 100) / 100;
  const own = el.getBoundingClientRect();
  let rects: DOMRect[] = own.width > 0 || own.height > 0 ? [own] : [];
  if (!rects.length) {
    // display:contents 的退化写法：自己不成盒，量它内容区间的每一段
    try {
      const range = document.createRange();
      range.selectNodeContents(el);
      rects = Array.from(range.getClientRects());
    } catch {
      rects = [];
    }
  }
  if (!rects.length) return null;
  const x = Math.min(...rects.map((r) => r.left));
  const y = Math.min(...rects.map((r) => r.top));
  const right = Math.max(...rects.map((r) => r.right));
  const bottom = Math.max(...rects.map((r) => r.bottom));
  return { x: round(x), y: round(y), w: round(right - x), h: round(bottom - y) };
}

// ---------------------------------------------------------------------------
// 闸⑧ 版面回读：叠字 / 被画面带裁掉 / 引线指向空白
//
// 这三条都是「抽帧看得出来、生成时看不出来」的画质账：镜头代码是模型现写的，
// 它排版时手里只有坐标，没有像素。母版是这一镜唯一既有 DOM 又有真像素的地方，
// 所以判据放在这里；判据本身是纯几何（盒与盒相交、端点离东西多远），不猜语义。
//
// 输出是**记账不拦截**的 findings（与闸⑦ 同一档）：这一层只交事实（判据命中的条目）、
// 扫描条数与本次实际编译进去的阈值；逐条人话的单源在 Python 侧
// `code_render.describe_layout`，`server.mjs` 只搬回结构化读数——措辞写两遍迟早不一样。
// ---------------------------------------------------------------------------

/**
 * 一条版面发现。`kind` 是给下游分档用的，`texts` 是给人看的证据（截断过的原文）。
 *
 * 坐标口径：`box` / `at` 都是**带内坐标**（画面带左上角是 (0,0)），不是成片像素。
 * 理由很直接——镜头代码写的就是这个系（`useBody()` 报的 `body.w/body.h` 是它的画布），
 * 报成片像素的话模型还得自己减掉 gutter，而减错方向就是另一处版面问题。
 * 闸⑥ 的字段盒用的是成片像素，那是 Python 侧归一化要点用的，两个口径各有归口。
 */
interface LayoutFinding {
  kind: 'overlap' | 'clipped' | 'dangling';
  texts: string[];
  box: PxBox | null;
  /** overlap 的交叠比例 / clipped 的边与像素 / dangling 的空端坐标 */
  ratio?: number;
  side?: string;
  px?: number;
  at?: { x: number; y: number };
}

const areaOf = (b: PxBox) => b.w * b.h;

/** 两个盒的相交面积；不相交给 0。 */
function interArea(a: PxBox, b: PxBox): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

/** 点到盒的距离（盒内为 0）。 */
function distToBox(p: { x: number; y: number }, b: PxBox): number {
  const dx = Math.max(b.x - p.x, 0, p.x - (b.x + b.w));
  const dy = Math.max(b.y - p.y, 0, p.y - (b.y + b.h));
  return Math.hypot(dx, dy);
}

/**
 * 这一路只扫「画得出东西」的元素：从自己往上到画面带，任一层透明或隐藏就跳过。
 *
 * 为什么要往上查而不是只看自己：动效几乎都写在祖先 `<g>`/容器上（整块淡入），
 * 只查自己会把「此刻还没亮」的标签当成叠字报出去。
 * `opacity` 用 <0.08 而不是 ===0：淡入到 3% 的东西肉眼本来也看不见，
 * 而它的盒已经完整存在，不躲就会误报。
 */
function paintedWithin(root: Element, el: Element): boolean {
  for (let cur: Element | null = el; cur && root.contains(cur); cur = cur.parentElement) {
    const s = getComputedStyle(cur);
    if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (s.display === 'none') return false;
    const o = Number(s.opacity === '' ? 1 : s.opacity);
    if (!Number.isNaN(o) && o < 0.08) return false;
  }
  return true;
}

/** 元素自己直接挂着的非空文本（不含子元素的文本，于是父子盒不会重复计）。 */
function ownText(el: Element): string {
  return Array.from(el.childNodes)
    .filter((n) => n.nodeType === 3 && (n.textContent ?? '').trim().length > 0)
    .map((n) => (n.textContent ?? '').trim())
    .join(' ')
    .trim();
}

/**
 * 「有可见画法」的叶子图元盒 —— 引线另一端的落点就拿这些判。
 *
 * 只收叶子：`<g>` 的盒是子元素的外接盒，收进来等于把整组都算成落点，
 * 悬空判据会永远不响（漏报到无用，不如不判）。
 * SVG 图元看 fill/stroke 是否真画得出；HTML 看有没有背景或描边——
 * 透明div 的盒是布局用的，不是画面。
 *
 * 结果带元素身份（而不只是盒）：判引线悬空时**必须把这条线自己剔掉**——线的端点天然
 * 落在自己的外接盒上，不剔就成了「另一端挨着画得出的东西」，dangling 这一路恒不响应。
 */
function drawnBoxes(root: Element): { el: Element; box: PxBox }[] {
  const out: { el: Element; box: PxBox }[] = [];
  const svgTags = new Set(['path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'image', 'text', 'tspan']);
  for (const el of Array.from(root.querySelectorAll('*'))) {
    if (el.childElementCount > 0) continue;
    const s = getComputedStyle(el);
    const painted = svgTags.has(el.tagName.toLowerCase())
      ? (s.fill !== 'none' && s.fill !== 'transparent')
        || (s.stroke !== 'none' && s.stroke !== 'transparent')
      : (s.backgroundColor !== 'transparent' && s.backgroundColor !== 'rgba(0, 0, 0, 0)')
        || parseFloat(s.borderTopWidth) > 0
        || parseFloat(s.borderLeftWidth) > 0;
    if (!painted) continue;
    if (!paintedWithin(root, el)) continue;
    const box = measureBox(el);
    if (box && box.w >= 1 && box.h >= 1) out.push({ el, box });
  }
  return out;
}

/**
 * 引线端点，屏幕像素。
 *
 * 三种写法覆盖模型实际会画的引线：`<line>`（直）、`<polyline>`（折）、`<path>`（弧）。
 * 属性值与 `getPointAtLength` 都在**元素自己的 user space**，`getScreenCTM()`
 * 正是从这个空间映到屏幕，所以 `<g transform>` / viewBox 缩放都跟着走 ——
 * 这条映射由 tests/layout-probe.mjs 拿一支带 transform 的夹具钉住。
 */
function lineEnds(el: Element): [{ x: number; y: number }, { x: number; y: number }] | null {
  const svgEl = el as SVGGraphicsElement & SVGGeometryElement;
  const ctm = typeof svgEl.getScreenCTM === 'function' ? svgEl.getScreenCTM() : null;
  if (!ctm) return null;
  const toScreen = (x: number, y: number) => {
    try {
      const p = new DOMPoint(x, y).matrixTransform(ctm as unknown as DOMMatrix);
      return { x: Math.round(p.x * 100) / 100, y: Math.round(p.y * 100) / 100 };
    } catch {
      return null;
    }
  };
  const num = (name: string) => Number(svgEl.getAttribute(name) ?? 0) || 0;

  let pair: { x: number; y: number }[] | null = null;
  if (svgEl.tagName.toLowerCase() === 'line') {
    pair = [{ x: num('x1'), y: num('y1') }, { x: num('x2'), y: num('y2') }];
  } else if (svgEl.tagName.toLowerCase() === 'polyline') {
    const pts = String(svgEl.getAttribute('points') ?? '')
      .trim().split(/[\s,]+/).map(Number).filter((n) => !Number.isNaN(n));
    if (pts.length >= 4) {
      pair = [
        { x: pts[0], y: pts[1] },
        { x: pts[pts.length - 2], y: pts[pts.length - 1] },
      ];
    }
  } else if (typeof svgEl.getTotalLength === 'function') {
    // path（以及任何有长度可求的图元）：起终点是引线的两端
    try {
      const total = svgEl.getTotalLength();
      if (total > 0) {
        const a = svgEl.getPointAtLength(0);
        const b = svgEl.getPointAtLength(total);
        pair = [{ x: a.x, y: a.y }, { x: b.x, y: b.y }];
      }
    } catch {
      pair = null;
    }
  }
  if (!pair) return null;
  const p0 = toScreen(pair[0].x, pair[0].y);
  const p1 = toScreen(pair[1].x, pair[1].y);
  return p0 && p1 ? [p0, p1] : null;
}

/** 母版在探针帧上做一次版面回读。 */
function probeLayout(root: Element, band: PxBox) {
  const findings: LayoutFinding[] = [];
  let dropped = 0;
  const push = (f: LayoutFinding) => {
    if (findings.length < LAYOUT_PROBE.max_findings) findings.push(f);
    else dropped++;
  };
  const round = (n: number) => Math.round(n * 100) / 100;
  /** finding 里的盒一律换成**带内坐标**——镜头代码写的就是那个系（`useBody()` 报的
   *  `body.w/body.h`），报屏幕像素等于让模型自己减 gutter，减错方向又是一处版面问题。
   *  三条判据共用这一个换算，别出现「只有 dangling 减了」那种分裂口径。 */
  const toBand = (b: PxBox): PxBox => ({
    x: round(b.x - band.x), y: round(b.y - band.y), w: b.w, h: b.h,
  });

  // 1) 文字盒
  const texts: { text: string; box: PxBox; el: Element }[] = [];
  for (const el of Array.from(root.querySelectorAll('*'))) {
    const text = ownText(el);
    if (!text) continue;
    if (!paintedWithin(root, el)) continue;
    const box = measureBox(el);
    if (!box || box.w < LAYOUT_PROBE.min_box_px || box.h < LAYOUT_PROBE.min_box_px) continue;
    texts.push({ text: text.slice(0, 24), box, el });
  }

  // 2) 叠字：两处各写各的文字压在一起
  for (let i = 0; i < texts.length; i++) {
    for (let j = i + 1; j < texts.length; j++) {
      const a = texts[i];
      const b = texts[j];
      // 父子不重复报：容器盒本来就罩住自己的文字，那不是「两处标签互相盖」
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const smaller = Math.max(1, Math.min(areaOf(a.box), areaOf(b.box)));
      const ratio = interArea(a.box, b.box) / smaller;
      if (ratio < LAYOUT_PROBE.overlap_ratio) continue;
      push({ kind: 'overlap', texts: [a.text, b.text], box: toBand(b.box), ratio: round(ratio) });
    }
  }

  // 3) 被画面带裁掉：母版那层是 overflow:hidden，越界就是真被切
  for (const t of texts) {
    const over = {
      left: band.x - t.box.x,
      top: band.y - t.box.y,
      right: t.box.x + t.box.w - (band.x + band.w),
      bottom: t.box.y + t.box.h - (band.y + band.h),
    };
    const side = (['left', 'top', 'right', 'bottom'] as const)
      .find((k) => over[k] > LAYOUT_PROBE.clip_px);
    if (!side) continue;
    push({ kind: 'clipped', texts: [t.text], box: toBand(t.box), side,
           px: Math.round(over[side]) });
  }

  // 4) 引线悬空：一端挨着文字，另一端既不挨文字也不挨任何画得出的东西
  const anchor = Math.max(
    LAYOUT_PROBE.anchor_min_px,
    Math.round(LAYOUT_PROBE.anchor_ratio * Math.min(band.w, band.h)),
  );
  const drawn = drawnBoxes(root);
  const nearText = (p: { x: number; y: number }) => texts.find((t) => distToBox(p, t.box) <= anchor) ?? null;
  /** 落点判「挨着画得出的东西」——**这条线自己不算**：它的端点天然落在自己的盒上。 */
  const nearDrawn = (p: { x: number; y: number }, self: Element) =>
    drawn.some((d) => d.el !== self && distToBox(p, d.box) <= anchor);

  let leaders = 0;
  for (const el of Array.from(root.querySelectorAll('line, polyline, path'))) {
    const s = getComputedStyle(el);
    const strokeWidth = Number(s.strokeWidth === '' ? 1 : parseFloat(s.strokeWidth)) || 0;
    if (s.stroke === 'none' || s.stroke === 'transparent' || strokeWidth <= 0) continue;
    if (!paintedWithin(root, el)) continue;
    const ends = lineEnds(el);
    if (!ends) continue;
    if (Math.hypot(ends[0].x - ends[1].x, ends[0].y - ends[1].y) < LAYOUT_PROBE.leader_min_px) continue;
    leaders++;
    // 只报「一端明确是标签、另一端明确是空白」这一种：两端都空的是装饰线，
    // 两端都不空的是接好的线，两端都挨文字的也不该响——判据越窄越不会误报。
    const hits = ends.map((p) => ({ p, text: nearText(p), drawn: nearDrawn(p, el) }));
    const anchored = hits.filter((h) => h.text);
    const free = hits.filter((h) => !h.text && !h.drawn);
    if (anchored.length === 1 && free.length === 1) {
      push({
        kind: 'dangling',
        texts: [anchored[0].text!.text],
        box: null,
        at: { x: round(free[0].p.x - band.x), y: round(free[0].p.y - band.y) },
      });
    }
  }

  return {
    findings,
    scanned: {
      texts: texts.length,
      leaders,
      drawn: drawn.length,
      band: { w: Math.round(band.w), h: Math.round(band.h) },
      dropped,
    },
  };
}

/**
 * 母版 Composition —— 新链里唯一有排版权的东西。
 *
 * 独占：背景、安全区、字阶、配色、字幕带的位置与高度。镜头代码只能通过 children
 * 往画面带里填东西，拿不到字幕绘制权 —— 于是「这一镜字幕比上一镜高 3 像素」这类
 * 漂移在结构上不可能发生，而不是靠闸⑤事后抓。
 *
 * 闸⑤读的是这里回写的 `<meta name="dg-tokens">`：写的是**实际用上的值**，
 * 不是常量表的副本 —— 母版哪天被改坏，回读表会先红。
 */
export const FilmLayout: React.FC<FilmLayoutProps> = ({
  text,
  highlight = [],
  words,
  subtitleMode = 'full',
  aspect = 'portrait',
  children,
}) => {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();
  // 闸⑧ 的扫描范围锚点：只扫画面带里面，母版自己的字幕带不算在镜头代码的账上。
  const bodyRef = React.useRef<HTMLDivElement>(null);
  // u 只管**字阶与配色**（横竖屏字号才跳不了位）；版面几何走 FILM_GEOMETRY 的设备像素，
  // 不乘 u——见 tokens.ts 那条注释，乘 u 会把上下安全区按宽轴放大，横屏正文带塌到 31%。
  const u = width / 1080;
  const geo = FILM_GEOMETRY[aspect];
  const body = filmBody(width, height, aspect);

  const captionIn = interpolate(frame, [0, 0.25 * fps], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const used = {
    version: 'film-layout@2',
    fontStack: TOKENS.fontStack,
    ink: TOKENS.ink,
    bg: TOKENS.bg,
    paper: TOKENS.paper,
    accent: TOKENS.accent,
    hl: TOKENS.hl,
    scale: Number(u.toFixed(6)),
    // 以下全是**这一版真正排出去的设备像素**：闸⑤ 容差 0 跨镜比对，
    // 母版被改坏时这里会先红，而不是等抽帧去猜。
    aspect,
    subtitleMode,
    bodyWidth: body.w,
    bodyHeight: body.h,
    topSafe: geo.topSafe,
    bottomSafe: geo.bottomSafe,
    gutter: geo.gutter,
    captionHeight: geo.caption.height,
    captionSize: geo.caption.fontSize,
  };

  // 闸⑤ 的回读有两条通道，各给不同的人读：
  //   · `<meta>` —— 给 Studio 预览和人工排查看（浏览器里能直接查）；
  //   · console.log —— 给渲染服务读。renderMedia 的 onBrowserLog 是公开口，
  //     而这一版没有 onBrowserOpen，服务端自己开页抓 DOM 抓不到本镜的 inputProps。
  React.useEffect(() => {
    console.log(`${TOKEN_LOG_MARKER}${JSON.stringify(used)}`);
  }, []);

  // 闸⑥ 与字段级命中表的原料：探针帧上把每一处 `[data-dg-field]` 的名字、
  // DOM 上真写着的值、以及它在成片上的盒一次性回读。
  //
  // 为什么由母版发一行而不是每个 `<F>` 各发一条：这条通道每条浏览器消息都要过
  // 一遍 onBrowserLog，而服务端认的是「最后一条命中」——一份 JSON 说完更省事，
  // 也让「同一帧上的所有盒」天然在同一时刻。
  // 为什么不设重入闸：一帧被重试时应让后一次、更稳的测量覆盖前一次。
  // 盒报的是成片像素，归一成 0~1 是 Python 侧的活（它才知道这一镜的最终宽高）。
  // Studio 里人工排查直接查 DOM 的 data-dg-field/data-dg-value 就行，这一行是给机器读的。
  const probeFrame = Math.max(0, durationInFrames - FIELD_PROBE_TAIL);
  React.useEffect(() => {
    if (frame !== probeFrame) return;
    const marks = Array.from(document.querySelectorAll('[data-dg-field]')).map((el) => {
      const raw = el.getAttribute('data-dg-value');
      let value: unknown = raw;
      try {
        value = JSON.parse(raw ?? 'null');
      } catch {
        value = raw;   // 标注写成非 JSON 时如实回读原串，由闸⑥ 报「对不上」
      }
      return { name: el.getAttribute('data-dg-field') ?? '', value, box: measureBox(el) };
    });
    console.log(`${FIELD_LOG_MARKER}${JSON.stringify({ frame, marks })}`);
  }, [frame, probeFrame]);

  // 闸⑧ 版面回读：与闸⑥ 同一帧、同一条通道，只是扫的是**画面带以内**的东西
  // （字幕带在母版自己手里，不在这条判据的账上）。
  //
  // 为什么带 scanned：findings 为空有两种完全不同的意思——「扫了 12 处文字都没事」
  // 与「这一镜压根没有文字元素」（纯图元/3D 通道）。只回 findings 数组的话，
  // 下游会把后者当成前者，回执上就成了替没验过的画面背书。
  // 画面带 ref 取不到时（理论上不会）如实发 reason，别发一条假的「干净」。
  React.useEffect(() => {
    if (frame !== probeFrame) return;
    const root = bodyRef.current;
    if (!root) {
      console.log(`${LAYOUT_LOG_MARKER}${JSON.stringify({
        frame, findings: [], scanned: null, thresholds: LAYOUT_PROBE,
        reason: '画面带 ref 未挂载，这一帧没扫版面',
      })}`);
      return;
    }
    const { findings, scanned } = probeLayout(root, { x: body.x, y: body.y, w: body.w, h: body.h });
    console.log(`${LAYOUT_LOG_MARKER}${JSON.stringify({ frame, findings, scanned, thresholds: LAYOUT_PROBE })}`);
  }, [frame, probeFrame, body.x, body.y, body.w, body.h]);

  // 闸⑪ 资产动效：在两个时点各扫一遍 <img>/<video>，记录盒与 transform。
  // server.mjs 比对两帧快照：盒/transform 全同 → 静态摆图打回；有变 → 参与动效绿；
  // 采不到任何资产盒 → 未判（不许记成通过）。
  // 用两个靠近末尾的帧而非首/末帧：首帧 <img> 可能未加载完（box=y:-999903），
  // 与探针帧的盒不同会被误判为 animated。两帧都靠近末尾时图片已加载、布局已稳定。
  // 选 probeFrame-2 与 probeFrame：间隔 2 帧足够检测仍在进行的动效，
  // 且都在渲染稳定区。对短镜（duration≤4）退化为前半与后半各取一帧。
  const assetProbeEarly = probeFrame > 4 ? probeFrame - 2 : Math.min(1, Math.floor(durationInFrames / 2));
  React.useEffect(() => {
    if (frame !== assetProbeEarly && frame !== probeFrame) return;
    const root = bodyRef.current;
    if (!root) return;
    const assets = Array.from(root.querySelectorAll('img, video'))
      .filter((el) => paintedWithin(root, el))
      .map((el) => {
        const box = measureBox(el);
        const s = getComputedStyle(el);
        const src = el.tagName.toLowerCase() === 'img'
          ? (el as HTMLImageElement).src
          : (el as HTMLVideoElement).src;
        return {
          tag: el.tagName.toLowerCase(),
          src: src?.slice(-80),
          box,
          transform: s.transform,
          opacity: Number(s.opacity === '' ? 1 : s.opacity),
        };
      });
    console.log(`${ASSET_LOG_MARKER}${JSON.stringify({ frame, assets })}`);
  }, [frame, assetProbeEarly, probeFrame]);

  return (
    <AbsoluteFill style={{ background: TOKENS.bg, fontFamily: TOKENS.fontStack }}>
      {/* 闸⑤ 的回读锚点：必须是「实际用上的值」，不是常量表副本 */}
      <meta name="dg-tokens" content={JSON.stringify(used)} />

      {/* 画面带：镜头代码的全部可视范围就这一条，越界由母版裁掉。
          盒的尺寸就是 useBody() 报出去的那一个——两侧同一个函数算的。 */}
      <div
        ref={bodyRef}
        style={{
          position: 'absolute',
          left: body.x,
          top: body.y,
          width: body.w,
          height: body.h,
          overflow: 'hidden',
        }}
      >
        {children}
      </div>

      {subtitleMode !== 'none' && text ? (
        <Caption
          text={text}
          highlight={highlight}
          words={words}
          mode={subtitleMode}
          opacity={captionIn}
          geo={geo}
        />
      ) : null}
    </AbsoluteFill>
  );
};

const Caption: React.FC<{
  text: string;
  highlight: string[];
  words?: WordTiming[];
  mode: SubtitleMode;
  opacity: number;
  geo: (typeof FILM_GEOMETRY)[Aspect];
}> = ({ text, highlight, words, mode, opacity, geo }) => {
  const frame = useCurrentFrame();
  const { width } = useVideoConfig();
  const cap = geo.caption;

  // fitText：用 Noto CJK 实测字宽，按可用宽度自适应字号。
  // 母版原来用固定 cap.fontSize，长文案撑破字幕盒、短文案字偏小——闸⑧ 叠字的根因。
  const availW = width - 2 * cap.side - 2 * cap.padX;
  let fontSize = cap.fontSize;
  try {
    const ft = remotionFitText({
      text,
      maxWidth: availW,
      fontFamily: '"Noto Sans CJK SC", sans-serif',
      fontWeight: 400,
      maxLines: 2,
      maxFontSize: cap.fontSize,
      minFontSize: Math.round(cap.fontSize * 0.6),
    });
    fontSize = ft.fontSize;
  } catch {
    // lint / 构建期没有浏览器环境，退回固定字号
  }

  const lit = (piece: string) => {
    const hit = highlight.find((h) => h && piece.includes(h));
    if (!hit) return null;
    if (!words?.length) return true;
    const w = words.find((x) => x.text && hit.includes(x.text));
    return w ? frame >= w.startFrame : false;
  };

  const pieces = splitByHighlights(text, highlight);

  return (
    <div
      style={{
        position: 'absolute',
        left: cap.side,
        right: cap.side,
        bottom: (geo.bottomSafe - cap.height) / 2,
        height: cap.height,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        opacity,
      }}
    >
      <div
        style={{
          maxWidth: '100%',
          padding: `${cap.padY}px ${cap.padX}px`,
          background: TOKENS.paper,
          borderRadius: cap.radius,
          color: TOKENS.ink,
          fontSize: fontSize,
          lineHeight: 1.35,
          textAlign: 'center',
        }}
      >
        {pieces.map((p, i) => {
          const on = lit(p);
          const shown = !words?.length || frame >= (words[i]?.startFrame ?? 0);
          // 高亮词整体占一块粉色：中文可任意断行，「一座城市」被劈成两行时行尾会留下
          // 一个只包着「一」字的孤立色块。短词锁住不拆；长片段照旧折行，免得撑破字幕盒。
          const keep = p.length <= 12 && highlight.some((h) => h && h === p);
          return (
            <span
              key={i}
              style={{
                opacity: shown ? 1 : 0,
                whiteSpace: keep ? 'nowrap' : undefined,
                background: on ? TOKENS.hl : 'transparent',
                // torn_highlight：词点亮后留一条下划红线，逐词「撕开」的观感。
                // 粗细跟字号走，三种画幅下才是同一条线。
                borderBottom:
                  on && mode === 'torn_highlight'
                    ? `${Math.max(2, Math.round(fontSize / 26))}px solid ${TOKENS.accent}`
                    : 'none',
              }}
            >
              {p}
            </span>
          );
        })}
      </div>
    </div>
  );
};

/** 按高亮词把整句切成片段；高亮词本身单独成段，才能一块一块点亮。 */
export function splitByHighlights(text: string, highlight: string[]): string[] {
  const words = highlight.filter((h) => h && text.includes(h));
  if (!words.length) return [text];
  const out: string[] = [];
  let rest = text;
  while (rest) {
    let best: { w: string; at: number } | null = null;
    for (const w of words) {
      const at = rest.indexOf(w);
      if (at >= 0 && (!best || at < best.at)) best = { w, at };
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.at > 0) out.push(rest.slice(0, best.at));
    out.push(best.w);
    rest = rest.slice(best.at + best.w.length);
  }
  return out;
}
