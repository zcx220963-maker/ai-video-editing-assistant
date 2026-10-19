/**
 * 版面回读（闸⑧）——**从母版里搬出来的那一块**，加了「字压图」一条。
 *
 * **为什么搬。** 这些函数原来全住在 `FilmLayout.tsx` 里，而三条回读 effect 也住在
 * `<FilmLayout>` 组件体内。母版下线之后镜头代码自包含（裸 `<AbsoluteFill>`），
 * 于是这些 effect 永不执行：自包含镜的版面读数恒为 `status:"missing"`——
 * 闸⑲-a / ⑳(c) / ㉕ 与回看环**一个数都没有**。这不是「这一镜没扫到」，是
 * 「整条通道对这一代镜头不存在」。搬出来挂到 Composition 上（见 `LayoutProbe`），
 * 每一镜就都有了同一份读数，无论它套不套母版。
 *
 * **行为不许变。** 上面的工具函数与 `probeLayout` 的三条既有判据（叠字 / 裁切 / 引线
 * 悬空）逐字搬过来，阈值仍在 `tokens.LAYOUT_PROBE` 一处（**不许两把尺**）。
 * 母版那条路改走 import，读数与从前逐字节相同。
 *
 * **新增的一条：`overlap_subject`（字压图）。** 原来 `subjectBoxes()` 量到的图形盒
 * 只当读数上报（`scanned.subjects` / `subject_share` / `subject_boxes`），**从不与文字盒
 * 相交**——「字幕压在插画上」在全仓没有任何一处代码在问。见 `probeLayout` 第 5 段。
 *
 * 输出仍是**记账不拦截**的 findings；逐条人话的单源在 Python 侧
 * `code_render.describe_layout`，这里只交结构化读数。
 */
import React from 'react';
import { useCurrentFrame, useVideoConfig } from 'remotion';
import { FIELD_PROBE_TAIL, LAYOUT_LOG_MARKER, LAYOUT_PROBE } from '../tokens';

/** 成片像素坐标系里的一个盒，左上角 (x,y)。 */
export interface PxBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 一个标注元素在成片像素坐标系里的盒；量不到返回 null（不是 0×0）。 */
export function measureBox(el: Element): PxBox | null {
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

/**
 * 屏幕盒 → **成片像素坐标**（`useBody()` 报的那一套：画面带左上角在 (gutter, topSafe)）。
 *
 * 为什么要有这一步：R7-T22 的进出场是母版套在整帧上的一层 `transform`，
 * 而 `getBoundingClientRect()` 量出来的是**变换之后**的屏幕盒。不折回去的话，
 * 一个「整片淡入 + 上浮」的片子会让闸⑧ 报一片「文字被画面带裁掉」——其实那是被
 * 那层变换推出去了，一条假读数。
 *
 * 折法：同一帧里量一次画面带自己的盒（`live`），再按它的缩放 `s = live.w / 静态宽`
 * 把子元素的盒还原。带子与子元素被同一个变换影响，所以相除之后剩下的就是**代码写下的
 * 那个位置**——「整片有进出场」于是对判据完全透明。没有进出场的片子
 * `live` 就等于静态盒、`s = 1`，这一步是恒等变换（老片子的读数一个字节不变）。
 */
export function toCodeSpace(box: PxBox, live: PxBox, base: PxBox): PxBox {
  const s = base.w > 0 && live.w > 0 ? live.w / base.w : 1;
  const round = (n: number) => Math.round(n * 100) / 100;
  return {
    x: round((box.x - live.x) / s + base.x),
    y: round((box.y - live.y) / s + base.y),
    w: round(box.w / s),
    h: round(box.h / s),
  };
}

/** 画面带这一帧**实测**的盒（含母版进出场那层 transform）。量不到给 null，
 *  调用方按「这一帧没有变换」处理（等于退回静态盒，宁可少折一次也不要报假读数）。 */
export function liveBand(el: HTMLElement | null): PxBox | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (!(r.width > 0) || !(r.height > 0)) return null;
  return { x: r.left, y: r.top, w: r.width, h: r.height };
}

// ---------------------------------------------------------------------------
// 闸⑧ 版面回读：叠字 / 被画面带裁掉 / 引线指向空白 / 字压图
//
// 这几条都是「抽帧看得出来、生成时看不出来」的画质账：镜头代码是模型现写的，
// 它排版时手里只有坐标，没有像素。探针帧是这一镜唯一既有 DOM 又有真像素的时刻，
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
export interface LayoutFinding {
  kind: 'overlap' | 'clipped' | 'dangling' | 'overlap_subject';
  texts: string[];
  box: PxBox | null;
  /** overlap / overlap_subject 的交叠比例；clipped 的边与像素；dangling 的空端坐标 */
  ratio?: number;
  side?: string;
  px?: number;
  at?: { x: number; y: number };
  /** overlap_subject：压住这块字的那个图形件占带面积的比例（用来判它是不是一块底色） */
  share?: number;
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

/** 屏幕像素 → 成片像素的换算（见 :func:`toCodeSpace`）；纯点也走它，
 *  所以「盒」与「引线端点」不会各折一次、折出两个口径。 */
export type Convert = (box: PxBox) => PxBox;

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
function drawnBoxes(root: Element, convert: Convert): { el: Element; box: PxBox }[] {
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
    if (box && box.w >= 1 && box.h >= 1) out.push({ el, box: convert(box) });
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

/** 闸⑲-a 的**主体件**：画得出**图形**的那几类元素（SVG 形状 / 位图 / 三维画布）。

 *  纯 `<div>` 加背景色**不算**，这一条是判据的命门：卡片就是一张涂了底色的 div，
 *  把 div 算进主体，等于这条闸自己把要拦的东西放行（「卡片+文字」镜会绿得非常稳）。
 *  同一口径也写在 Python 侧（`code_spec._VECTOR_MARKUP_RE` 那句「色块不是图形主体」）。
 *  这里只报**读数**（个数与包围盒占比），阈值归 Python——这条链的规矩是「母版报原始
 *  读数、判据与数字在 Python 一处」，两边各写一个 0.18 就会出现两把尺。
 */
const SUBJECT_TAGS = new Set([
  'path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line',
  'canvas', 'img', 'video',
]);

/** 主体件**带元素身份**（`subjectBoxes` 只是它的 map）。
 *  字压图那条判据要按身份剔两种件：与文字有祖孙关系的（字幕自己的衬底）、
 *  以及铺满整帧的那种（底色 rect）——只有盒、没有元素，这两条都判不了。 */
function subjectPieces(root: Element, convert: Convert): { el: Element; box: PxBox }[] {
  const out: { el: Element; box: PxBox }[] = [];
  for (const el of Array.from(root.querySelectorAll('*'))) {
    if (!SUBJECT_TAGS.has(el.tagName.toLowerCase())) continue;
    if (!paintedWithin(root, el)) continue;
    const raw = measureBox(el);
    if (!raw || raw.w < 1 || raw.h < 1) continue;
    out.push({ el, box: convert(raw) });
  }
  return out;
}

function subjectBoxes(root: Element, convert: Convert): PxBox[] {
  return subjectPieces(root, convert).map((p) => p.box);
}

/** 闸⑳(c) 的**画面指纹**：把画面带切成 6×6 的粗网格，记下文字盒与图元盒各压到哪些格。

 *  为什么是「哪几格」而不是精确坐标：⑳(c) 要判的是「**画面上**是不是同一张卡，只是代码
 *  写法不同」——同一张卡换个写法，坐标会差几个像素、格子在不在还是会一样。反过来精确坐标
 *  会让一条 3px 的位移把两组判成不同，判据就成了「代码像不像」的另一个马甲。
 *  闸⑨ 比代码骨架且要 ≥3 镜才定罪；⑳(c) 比**画面**且两镜即算——两个问题的答案不一样，
 *  所以两条闸并存（这也是「不许合并、不许重复实现」那句话的落点）。
 */
const DEGEN_GRID = 6;

function gridSignature(boxes: PxBox[], band: PxBox): number {
  if (band.w <= 0 || band.h <= 0) return 0;
  let bits = 0;
  const cw = band.w / DEGEN_GRID;
  const ch = band.h / DEGEN_GRID;
  for (const b of boxes) {
    const x0 = Math.floor((b.x - band.x) / cw);
    const x1 = Math.floor((b.x + b.w - band.x) / cw);
    const y0 = Math.floor((b.y - band.y) / ch);
    const y1 = Math.floor((b.y + b.h - band.y) / ch);
    for (let gy = Math.max(0, y0); gy <= Math.min(DEGEN_GRID - 1, y1); gy++) {
      for (let gx = Math.max(0, x0); gx <= Math.min(DEGEN_GRID - 1, x1); gx++) {
        bits |= 1 << (gy * DEGEN_GRID + gx);
      }
    }
  }
  return bits;
}

/** 闸⑳(c) 的**主色直方图**：这一镜画面上真声明了哪些颜色，按出现次数从多到少取前几个。

 *  说清楚它量的是什么：**不是**对像素做直方图，而是把 DOM 里画得出来的元素身上声明的
 *  颜色（`background-color`/`fill`/`stroke`/`color`，以及 SVG 属性写法）收起来数频次。
 *  两者在「同一张卡换个写法」这件事上等价（卡的颜色是写出来的），而它不需要读像素——
 *  省一整趟解码，也就能在探针那一帧顺手算完。渐变与位图在这条判据里认不出颜色：那种镜
 *  的这一栏是空的，而空栏**不参与**判定（见 Python 侧 `check_degenerate_forms`：颜色
 *  认不出时只按网格判，并把「颜色没量到」如实记在账上）。
 *
 *  `color`（文字色）也在里面，而且**必须**在：⑳(c) 要抓的那张丑卡恰恰是「一块底板 + 几
 *  行字、一个 SVG 形状都没有」，那种镜除了文字色之外一个声明的颜色都没有——不收它，
 *  这一栏就是空的，而空栏落「未判」，这条闸就恰好在它该响的地方闭嘴。
 *  收的是**声明值**，所以 `color` 会沿继承链在每个元素上各记一次——那正好是「这镜的墨色
 *  是什么」的频次，判据那边按**集合**比，不看谁排第一。
 */
function colorHistogram(root: Element): string[] {
  const count = new Map<string, number>();
  const bump = (raw: string) => {
    if (!raw) return;
    const v = raw.trim().toLowerCase();
    if (!v || v === 'transparent' || v === 'none' || v.startsWith('url(')) return;
    if (v.startsWith('rgba(0, 0, 0, 0)')) return;
    count.set(v, (count.get(v) || 0) + 1);
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';
  for (const el of Array.from(root.querySelectorAll('*'))) {
    const s = getComputedStyle(el);
    // `fill`/`stroke` **只对 SVG 元素收**：CSS 里这两个属性的初值就是黑色，于是每一个
    // 普通 div 都会贡献一次 `rgb(0, 0, 0)`——真机实测那一栏会变成「所有颜色都被黑色挤掉
    // 前四名里的一半」，而这条读数要回答的是「这镜画了什么颜色」。
    if (el.namespaceURI === SVG_NS) {
      bump(s.fill);
      bump(s.stroke);
      bump(el.getAttribute('fill') || '');
      bump(el.getAttribute('stroke') || '');
    }
    bump(s.backgroundColor);
    bump(s.color);
    bump(el.getAttribute('color') || '');
  }
  return Array.from(count.entries())
    .sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]))
    .slice(0, 4)
    .map(([c]) => c);
}

/** 一组盒的**并集外接盒**与画面带相交之后，占带面积的比例（0~1）。
 *
 *  为什么取并集外接盒而不是并集面积：判据问的是「主体件铺开了多大一片画面」，
 *  而不是「主体的墨水占了多少面积」——一只描边的圆，墨水占比接近 0，但它确实
 *  占了画面里很大一块。两条口径给出的数差一个数量级，取哪个必须先说清楚。
 */
function shareOfBoxes(boxes: PxBox[], band: PxBox): number {
  if (!boxes.length || band.w <= 0 || band.h <= 0) return 0;
  const x = Math.max(band.x, Math.min(...boxes.map((b) => b.x)));
  const y = Math.max(band.y, Math.min(...boxes.map((b) => b.y)));
  const right = Math.min(band.x + band.w, Math.max(...boxes.map((b) => b.x + b.w)));
  const bottom = Math.min(band.y + band.h, Math.max(...boxes.map((b) => b.y + b.h)));
  if (right <= x || bottom <= y) return 0;
  return ((right - x) * (bottom - y)) / (band.w * band.h);
}

/** 在探针帧上做一次版面回读。
 *
 *  `convert` 把这一帧量到的屏幕盒折回**成片像素**（见 :func:`toCodeSpace`）：
 *  母版那层进出场变换是整帧一起动的，折回去之后几条判据读到的还是「代码写下的版面」。
 *  没有进出场的片子 `convert` 是恒等变换，读数与从前逐字节相同。
 */
export function probeLayout(root: Element, band: PxBox, convert: Convert) {
  const findings: LayoutFinding[] = [];
  let dropped = 0;
  const push = (f: LayoutFinding) => {
    if (findings.length < LAYOUT_PROBE.max_findings) findings.push(f);
    else dropped++;
  };
  const round = (n: number) => Math.round(n * 100) / 100;
  /** finding 里的盒一律换成**带内坐标**——镜头代码写的就是那个系（`useBody()` 报的
   *  `body.w/body.h`），报屏幕像素等于让模型自己减 gutter，减错方向又是一处版面问题。
   *  几条判据共用这一个换算，别出现「只有 dangling 减了」那种分裂口径。 */
  const toBand = (b: PxBox): PxBox => ({
    x: round(b.x - band.x), y: round(b.y - band.y), w: b.w, h: b.h,
  });

  // 1) 文字盒
  //
  // `box` 是并集外接盒（裁切 / 字压图要用它）；`rects` 是**逐行**的盒
  // （`getClientRects()` 对行内元素给每个行盒一段）。两者都要：
  // 自动排版字幕是若干个相邻的行内片段，换行那个片段的**并集盒**会把左边那段
  // 整块罩住（第二行从行首起，外接盒于是从行首一直框到行尾）——拿并集去判叠字，
  // 「蒸汽机 / 把煤里的力量…」这种正常换行会被报成 ratio 1 的叠字。逐行比就没有
  // 这个问题：第一行两段相邻、第二行在下面，谁也不压谁。
  const texts: { text: string; box: PxBox; rects: PxBox[]; el: Element }[] = [];
  for (const el of Array.from(root.querySelectorAll('*'))) {
    const text = ownText(el);
    if (!text) continue;
    if (!paintedWithin(root, el)) continue;
    const raw = measureBox(el);
    if (!raw || raw.w < LAYOUT_PROBE.min_box_px || raw.h < LAYOUT_PROBE.min_box_px) continue;
    const box = convert(raw);
    const rects = Array.from(el.getClientRects())
      .map((r) => convert({ x: r.left, y: r.top, w: r.width, h: r.height }))
      .filter((b) => b.w >= LAYOUT_PROBE.min_box_px && b.h >= LAYOUT_PROBE.min_box_px);
    texts.push({ text: text.slice(0, 24), box, rects: rects.length ? rects : [box], el });
  }

  // 2) 叠字：两处各写各的文字压在一起
  for (let i = 0; i < texts.length; i++) {
    for (let j = i + 1; j < texts.length; j++) {
      const a = texts[i];
      const b = texts[j];
      // 父子不重复报：容器盒本来就罩住自己的文字，那不是「两处标签互相盖」
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      // 逐行配对取最大交叠比例：并集盒会把「相邻的行内片段」误报成叠字（见第 1 段）
      let ratio = 0;
      for (const ra of a.rects) {
        for (const rb of b.rects) {
          const smaller = Math.max(1, Math.min(areaOf(ra), areaOf(rb)));
          ratio = Math.max(ratio, interArea(ra, rb) / smaller);
        }
      }
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
  const drawn = drawnBoxes(root, convert);
  // 闸⑲-a / ⑳(a) 的两个读数（T30）：主体件占带多少、文字占带多少。判据在 Python，
  // 这里只把这一帧量到的两个数如实报上去（量不到就是 0，别编一个「差不多」的数）。
  const pieces = subjectPieces(root, convert);
  const subject = pieces.map((p) => p.box);
  const nearText = (p: { x: number; y: number }) => texts.find((t) => distToBox(p, t.box) <= anchor) ?? null;
  /** 落点判「挨着画得出的东西」——**这条线自己不算**：它的端点天然落在自己的盒上。 */
  const nearDrawn = (p: { x: number; y: number }, self: Element) =>
    drawn.some((d) => d.el !== self && distToBox(p, d.box) <= anchor);

  /** 端点也走同一个换算：`getScreenCTM()` 给的是屏幕像素，判据的容差全是成片像素。 */
  const convPoint = (p: { x: number; y: number }) => {
    const c = convert({ x: p.x, y: p.y, w: 0, h: 0 });
    return { x: c.x, y: c.y };
  };

  let leaders = 0;
  for (const el of Array.from(root.querySelectorAll('line, polyline, path'))) {
    const s = getComputedStyle(el);
    const strokeWidth = Number(s.strokeWidth === '' ? 1 : parseFloat(s.strokeWidth)) || 0;
    if (s.stroke === 'none' || s.stroke === 'transparent' || strokeWidth <= 0) continue;
    if (!paintedWithin(root, el)) continue;
    const screen = lineEnds(el);
    if (!screen) continue;
    const ends: [{ x: number; y: number }, { x: number; y: number }] =
      [convPoint(screen[0]), convPoint(screen[1])];
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

  // 5) 字压图：一块字压在画出来的图形件上。
  //
  // 原来这一侧没有任何判据：`subjectBoxes()` 量到的盒只当读数上报（`subjects` /
  // `subject_share` / `subject_boxes`），**从不与文字盒相交**——用户看到的
  // 「字幕压在插画上」在全仓没有任何一处代码在问，所以「说了也没有用」。
  //
  // 误报抑制三道（这条通道的产出是账、不是拦截，误报比漏报贵——`LAYOUT_PROBE`
  // 的注释原话）：一列只报一块字最大的一次相交，中间那些也报只会把 8 条额度烧光；
  //     · 铺满整帧的件不当主体（底色 `<rect>`、满幅装饰）——否则每一条字幕都报；
  //     · 与文字有祖孙关系的件跳过（字幕自己那层衬底就是这一种）；
  //     · 门槛判的是**这块字被压掉多少**（交叠 ÷ 文字盒面积），不是交叠 ÷ 图形件——
  //       一件铺得很开的装饰形状会把「÷ 图形件」压到很小，于是永远不响。
  const bandArea = band.w * band.h;
  for (const t of texts) {
    const tArea = Math.max(1, areaOf(t.box));
    let worst: { ratio: number; share: number } | null = null;
    for (const p of pieces) {
      if (p.el.contains(t.el) || t.el.contains(p.el)) continue;
      const share = bandArea > 0 ? areaOf(p.box) / bandArea : 0;
      if (share >= LAYOUT_PROBE.subject_max_share) continue;
      const ratio = interArea(t.box, p.box) / tArea;
      if (ratio < LAYOUT_PROBE.subject_overlap_ratio) continue;
      if (!worst || ratio > worst.ratio) worst = { ratio, share };
    }
    if (!worst) continue;
    push({
      kind: 'overlap_subject',
      texts: [t.text],
      box: toBand(t.box),
      ratio: round(worst.ratio),
      share: Math.round(worst.share * 10000) / 10000,
    });
  }

  return {
    findings,
    scanned: {
      texts: texts.length,
      leaders,
      drawn: drawn.length,
      band: { w: Math.round(band.w), h: Math.round(band.h) },
      dropped,
      // 闸⑲-a / ⑳(a)：主体件个数与两个占比（原始读数，阈值在 Python）。
      subjects: subject.length,
      subject_share: Math.round(shareOfBoxes(subject, band) * 10000) / 10000,
      text_share: Math.round(shareOfBoxes(texts.map((t) => t.box), band) * 10000) / 10000,
      // 闸⑳(c)：这一镜的**画面指纹**（粗网格上文字/图元各压到哪些格 + 主色前四）。
      // 判据在 Python：两条指纹撞上就是「代码不同、画面上是同一张卡」。
      grid_text: gridSignature(texts.map((t) => t.box), band),
      grid_drawn: gridSignature(drawn.map((d) => d.box), band),
      colors: colorHistogram(root),
      // 闸㉕ 徽章构图（R11）：**逐件**主体包围盒的原始读数，带内坐标、整数像素。
      // 为什么要逐件而不是复用上面那两个数：`subjects` 只有**个数**、`subject_share` 只有
      // **并集外接盒**的占比——「一枚圆形徽章铺满画面」与「两个部件左上一个右下」这两件
      // 事，在「个数 1 / 占比 0.28」这栏里长得很像，只有拿到件本身的盒（宽高比、离带中心
      // 多远）才分得开。判据（`BADGE_MIN_SHARE` / 居中容差 / 近方形容差）**全在 Python**
      // ——母版只报量到了什么，两处各写一个 0.25 就会出现两把尺（同 ⑲-a 那条规矩）。
      subject_boxes: subject.map((b) => {
        const t = toBand(b);
        return { x: Math.round(t.x), y: Math.round(t.y), w: Math.round(t.w), h: Math.round(t.h) };
      }),
    },
  };
}

/** 谁发的这一行：母版（画面带口径）还是测量壳（整帧口径）。 */
export type ProbeSource = 'master' | 'shell';

/** 探针帧上把版面的读数发一行出去（与闸⑤⑥ 同一条 `console.log` 通道）。
 *
 *  单独一个函数而不是写死在组件里：母版与新探针壳**必须发同一句话**，
 *  两处各写一遍迟早会一处漏掉 `thresholds`（Python 侧 `describe_layout` 就靠它拿阈值）。
 *
 *  `by` 是**必填**的。两个上报者量的是两个口径（母版量画面带、壳量整帧），
 *  同一镜要是发了两行，下游看数字是分不出「两个口径」还是「这一镜不稳定」的——
 *  而 `server.mjs` 那边是「后一行覆盖前一行」。带一个来源，那一行就成了自证的：
 *  出现两行且 `by` 不同，是「母版与壳同时报了」（说明壳的让位判据失效）；
 *  两行 `by` 相同，是同一帧被 seek 了两遍。两种都是要记账的事，别让它长得一样。
 */
export function emitLayout(
  root: Element, band: PxBox, convert: Convert, frame: number, by: ProbeSource,
): void {
  const { findings, scanned } = probeLayout(root, band, convert);
  console.log(`${LAYOUT_LOG_MARKER}${JSON.stringify({
    frame, by, findings, scanned, thresholds: LAYOUT_PROBE,
  })}`);
}

/** 这一帧**没扫成**的那一行。理由单独发，不塞成一条 finding——`findings: []` 与
 *  `scanned: null` 是两个不同的意思（「扫过、一处都没有」与「压根没扫」），
 *  Python 侧 `summarize_layout` 就是照 `scanned` 分这两档的。
 *
 *  与 `emitLayout` 摆在一起，是为了让「这行长什么样」只有一个出处：`thresholds`
 *  必须两路都带（`describe_layout` 靠它拿阈值报数），漏在一路就是那条通道上一个
 *  静默的洞。 */
export function emitLayoutMissing(frame: number, reason: string, by: ProbeSource): void {
  console.log(`${LAYOUT_LOG_MARKER}${JSON.stringify({
    frame, by, findings: [], scanned: null, thresholds: LAYOUT_PROBE, reason,
  })}`);
}

/**
 * **自包含镜的测量壳** —— Composition 的 component 外面包这一层，每一镜就都有了版面回读。
 *
 * 三件事说清楚：
 *
 * 1. **不改一个像素。** 壳是 `display: contents`（不生成盒子，子元素照原样参与布局），
 *    没有背景、没有 flex、没有字号——镜头代码在它里面与没有它时逐像素相同。
 *    这一条是硬要求：探针是加在渲染路径上的观测，观测不许改被测对象。
 * 2. **套了母版的镜会发两行，由 `server.mjs` 认 `by` 挑。** 这一层**不**去判「上面有没有
 *    母版」——它试过，而且那一条判据是错的：原来用母版必留的 `<meta name="dg-tokens">`
 *    当哨兵，可 React 19 会把 `<meta>` **提升到 `<head>`**，于是 `root.querySelector`
 *    永远找不到它，两行照发，而 `server.mjs` 那边当时是「后一行覆盖前一行」——
 *    结果是**测量壳的整帧口径盖掉了母版的画面带口径**，真机实测：母版镜的版面读数
 *    全成了整帧的（`band` 报 1080×1920、进出场那层变换也没折回去，T22 那一支红）。
 *    哨兵换成 `document.querySelector` 治不了这个病——那是一处**跨 composition 的全局**，
 *    上一个镜的字留不留在 head 里取决于渲染器怎么开页，比错更坏的是「有时对」。
 *    所以判据挪到有完整信息的那一层：两行都发，`server.mjs` 按 `by` 挑（母版优先）。
 *    代价是母版镜多扫一遍 DOM（毫秒级），买的是「不依赖 DOM 里有没有某个元素」。
 * 3. **闸⑥（字段回读）这一层不发。** 它报的是母版常量与 `[data-dg-field]` 的命中表，
 *    自包含镜既没有母版也没有那条 effect；这里顺手发一行会让 Python 侧把
 *    「没扫过」读成「扫过、一处都没有」——那是把判无写成通过。这一格**如实留着**。
 */
export const LayoutProbe: React.FC<{ children?: React.ReactNode }> = ({ children }) => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const ref = React.useRef<HTMLDivElement | null>(null);
  const probeFrame = Math.max(0, durationInFrames - FIELD_PROBE_TAIL);

  React.useEffect(() => {
    if (frame !== probeFrame) return;
    const root = ref.current;
    if (!root) return;
    const base: PxBox = { x: 0, y: 0, w: width, h: height };
    // **必须等一个 tick 再量。** Remotion 在提交帧那一瞬把画布放在
    // `position: fixed; top: -999999px`，而且**尺寸还是 0×0**；下一个 tick 才归位
    // （2026-10-18 实测：effect 里量到 `top=-999999, w=0`，`setTimeout(0)` 里是
    // `top=0, w=1920`）。同步量的后果不是「量不到」，而是**量到一份偏移了一百万像素
    // 的读数**：真机上每一条字幕都成了 `clipped top 999397px`，两段字还互相「叠」在
    // 一起——模型会去挪一处根本没问题的字。假的版面账比没有账更坏。
    //
    // 为什么不是「用带子折回去」（母版那条路）：此刻镜头根节点是 0×0，`liveBand`
    // 返回 null，没有可折的原点。归位之后再量，这一步就退回恒等变换。
    //
    // `setTimeout(0)` 而不是 `requestAnimationFrame`：后者在 Remotion 的 headless
    // 截图流程里不保证在截图前跑完；宏任务实测会被 `onBrowserLog` 收到（见
    // `render-stills` 的 `layoutLines`）。
    setTimeout(() => {
      const shot = root.firstElementChild as HTMLElement | null;
      const live = shot ? liveBand(shot) : null;
      const convert: Convert = live ? (b: PxBox) => toCodeSpace(b, live, base) : (b: PxBox) => b;
      emitLayout(root, base, convert, frame, 'shell');
    }, 0);
  }, [frame, probeFrame, width, height]);

  // `display: contents`：这一层不成盒子，子元素在它里面与没有它时同样排版。
  // 别改成 padded/flex 的容器——那会把镜头代码的版面挪几像素，而探针只是观测。
  return (
    <div ref={ref} style={{ display: 'contents' }}>
      {children}
    </div>
  );
};
