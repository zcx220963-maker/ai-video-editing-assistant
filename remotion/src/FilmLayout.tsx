import React from 'react';
import {
  AbsoluteFill,
  Easing,
  getInputProps,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import { fitText as remotionFitText } from '@remotion/layout-utils';
// 副作用 import（R5-T16）：把 webfont 注册表拉进每一镜的依赖图。母版是硬性包裹层，
// 所以「用了母版就一定有字体」。TOKENS.fontStack 里那个 `"Noto Sans SC"` 过去是个
// **装不出来的名字**（镜像的 fc-list 里只有 "Noto Sans CJK SC"），浏览器不报错、
// 直接回落——这正是第六根因。现在它由 @dg/fonts 当场加载进来，名字与字节都对得上。
import '@dg/fonts';
import {
  ASPECTS,
  ASSET_LOG_MARKER,
  FIELD_LOG_MARKER,
  FIELD_PROBE_TAIL,
  FILM_GEOMETRY,
  FILM_STYLE_DEFAULT,
  LAYOUT_LOG_MARKER,
  LAYOUT_PROBE,
  REVEAL_LOG_MARKER,
  TOKENS,
  TOKEN_LOG_MARKER,
} from './tokens';
import type { Aspect, FilmStyle, SubtitleMode } from './tokens';

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
  // R14：风格档也从注入里取，与母版同一份。`useBody()` 是镜头代码唯一量得到带子的口，
  // 它必须知道「这一镜是不是满幅」——否则满幅片的镜头代码会按 936×1524 排版，
  // 而母版实际裁的是 1080×1920，两侧各算一套几何就正是这条链最怕的漂移。
  return filmBody(width, height, aspect, resolveFilmStyle(getInputProps()).bleed);
}

/** `useBody()` 的无钩子版本，母版自己也用它一次，于是两侧不可能算出两个数。 */
export function filmBody(width: number, height: number, aspect?: Aspect,
                          bleed = false): BodyBox {
  const key = aspect ?? aspectFor(width, height);
  // 满幅：没有页边距、没有上下安全区，整帧就是可画区。字幕条（若这一镜还有）
  // 改成**叠在画面上**，位置见 `FILM_GEOMETRY[..].bleedCaptionBottom`。
  if (bleed) return { x: 0, y: 0, w: width, h: height, aspect: key };
  const g = FILM_GEOMETRY[key];
  return {
    x: g.gutter,
    y: g.topSafe,
    w: width - 2 * g.gutter,
    h: height - g.topSafe - g.bottomSafe,
    aspect: key,
  };
}

/**
 * R14 风格档的注入解析 —— 与 :func:`resolveReveal` / `resolveShotMotion` 同一套写法。
 *
 * 认不出的写法一律挡回缺省值，**不抛也不猜**：风格档的每个字段都有「不设 = 今天」
 * 的语义，一个写坏的 `bg` 不该让整镜渲不出来，而应该退回母版原本的底色。
 * `bg` 只收 `#rrggbb`：闸④ 拿它当判空的基准色（`code_render._bg_of` 读不到基准色
 * 就拒绝判空），所以 `rgb()`/命名色/渐变一律不收——那会让闸④ 在这一镜上闭嘴。
 */
export function resolveFilmStyle(injected: unknown): FilmStyle {
  const src = (injected && typeof injected === 'object' ? injected : {}) as Record<string, unknown>;
  const raw = src.film_style;
  const s = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const bg = String(s.bg ?? '').trim();
  return {
    bleed: s.bleed === true,
    bg: /^#[0-9a-fA-F]{6}$/.test(bg) ? bg.toLowerCase() : FILM_STYLE_DEFAULT.bg,
  };
}

/** `#rrggbb` → 相对亮度 0~1（sRGB 加权）。够用来判「这个底是深的还是浅的」。 */
function luma(hex: string): number {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/.exec(hex.toLowerCase());
  if (!m) return 1;
  const [r, g, b] = [1, 2, 3].map((i) => parseInt(m[i], 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * 字幕条的两个颜色，**按底色亮度算**，不查色板。
 *
 * 为什么按亮度而不是收一个色板名：色板今天由镜头代码 import（闸⑩ 照代码里绑的
 * 名字解析颜色），母版再收一份就是同一件事两个出处。而母版真正需要知道的只是
 * 「底深还是底浅」——知道这个，字幕条该用深墨还是浅墨就定了，且**不会出现
 * 深底 + 深墨**那种读不出来的组合（闸⑩ 判的正是这个对比度）。
 */
function captionTone(bg: string): { paper: string; ink: string } {
  return luma(bg) < 0.5
    ? { paper: 'rgba(16,14,12,0.72)', ink: '#f4f1ea' }
    : { paper: TOKENS.paper, ink: TOKENS.ink };
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
  /** R7-T22 整片进出场档名（none/fade/rise/drop/zoom），由整片开关注入 */
  entrance?: ShotMotionStyle;
  exit?: ShotMotionStyle;
  /** 入场/出场的窗口**帧数**。由 Python 侧算好注入（`cspec.shot_motion_window`）：
   *  那条算术（这一镜的 10%、封顶 15 帧、不足 2 帧算没有）与闸④ 的采样点绑着，
   *  母版这边再算一遍就有了第二个出处。0 = 这一镜不动作。 */
  entranceFrames?: number;
  exitFrames?: number;
  /** 镜头代码填进画面带的主体内容 */
  children?: React.ReactNode;
}

// ---------------------------------------------------------------------------
// R7-T22 出场/入场：**母版**给每一镜套的那一小段动作
//
// 为什么在母版而不是镜头代码里：入场/出场是整片的一个观感决定（用户在计划卡上勾一次），
// 让 20 份模型现写的代码各自插值一小段，等于把这件决定交给 20 次掷骰子，而闸⑤ 也判不出
// 「这一镜没做入场」——它比的是 token。放在母版里，它是结构性的：勾了就有。
//
// 为什么不引 `@remotion/transitions`：那套 `<TransitionSeries>` 要求相邻片段在同一棵
// React 树里，而我们逐镜独立渲、按 `shot_cache_key` 缓存——树内转场等于把逐镜缓存整条
// 保证作废（§3.1 / §9-Q11）。镜头**之间**的转场因此落在拼接层（ffmpeg xfade，
// 见 `mediaops._concat_xfade`），这里只做「这一镜自己怎么进、怎么出」。
// ---------------------------------------------------------------------------

export type ShotMotionStyle = 'none' | 'fade' | 'rise' | 'drop' | 'zoom';

/** 动一动的幅度：画面高的 4%（竖屏 1920 → 77px）。按帧高而不是常量，
 *  三种画幅下才是同一个观感——与字阶按 `width/1080` 缩放同一条理由。 */
const MOTION_SHIFT_RATIO = 0.04;
/** zoom 的两端：入场从 0.94 推上来，出场再推到 1.04。 */
const MOTION_ZOOM_IN = 0.94;
const MOTION_ZOOM_OUT = 0.04;

export interface ShotMotion {
  opacity: number;
  transform: string;
}

/** 进度 → 这一档在这一刻的透明度与位移/缩放。纯函数，方便真渲夹具之外单独核。 */
function motionAt(style: ShotMotionStyle, prog: number, entering: boolean,
                  shift: number): ShotMotion {
  if (style === 'none' || prog <= 0) return { opacity: 1, transform: 'none' };
  const p = Math.min(1, Math.max(0, prog));
  const dy = style === 'rise' ? (entering ? (1 - p) * shift : -p * shift)
    : style === 'drop' ? (entering ? -(1 - p) * shift : p * shift) : 0;
  const scale = style === 'zoom'
    ? (entering ? MOTION_ZOOM_IN + (1 - MOTION_ZOOM_IN) * p : 1 + MOTION_ZOOM_OUT * p)
    : 1;
  const parts = [
    dy ? `translateY(${Math.round(dy * 100) / 100}px)` : '',
    scale !== 1 ? `scale(${Math.round(scale * 10000) / 10000})` : '',
  ].filter(Boolean);
  return {
    opacity: style === 'fade' ? 1 - p : 1,
    transform: parts.length ? parts.join(' ') : 'none',
  };
}

/** 一镜的进出场合成。两个窗口分居这一镜的首尾（各 ≤10%），不可能同时进行；
 *  出场用 `Easing.in(cubic)`（起步慢、走得快）是有意的：末段代表帧与字段盒探针
 *  都在 `durationInFrames - 2` 那一帧上，缓入让那一帧离「落定」更近一点。 */
export function shotMotion(
  style: ShotMotionStyle, frames: number, frame: number,
  durationInFrames: number, height: number, entering: boolean,
): ShotMotion {
  if (style === 'none' || frames < 2) return { opacity: 1, transform: 'none' };
  const at = entering ? [0, frames] : [durationInFrames - frames, durationInFrames];
  const prog = interpolate(frame, at, [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: entering ? Easing.out(Easing.cubic) : Easing.in(Easing.cubic),
  });
  return motionAt(style, prog, entering, height * MOTION_SHIFT_RATIO);
}

/** 把两段动作叠在一起：透明度相乘、transform 依次套。 */
function composeMotion(a: ShotMotion, b: ShotMotion): ShotMotion {
  const parts = [a.transform, b.transform].filter((t) => t && t !== 'none');
  return {
    opacity: a.opacity * b.opacity,
    transform: parts.length ? parts.join(' ') : 'none',
  };
}

/** 进出场的四条取值，解析完的形态。 */
interface ShotMotionInputs {
  entrance: ShotMotionStyle;
  exit: ShotMotionStyle;
  entranceFrames: number;
  exitFrames: number;
}

const MOTION_STYLE_SET = new Set<string>(['none', 'fade', 'rise', 'drop', 'zoom']);

/**
 * 进出场四条：**显式 props 优先，没给就取 `/render-shot` 注入的那一份**
 * （`code_render.shot_input_props`）。
 *
 * 为什么要这条回退：这四条与 `text`/`words` 不是一类——它们不是「镜头代码自己的数据」，
 * 而是**整片开关**决定的母版输入。可镜头代码要拿到注入值，就得在自己的 `Shot(props)`
 * 里把它们再抄一遍交给 `<FilmLayout>`，而模板里漏一行**画面只是静默地不演**：「没演」与
 * 「这一镜没选进出场」在成片上长得一模一样，闸④/⑤/⑥ 一个都判不出来（token、版面读数、
 * 空壳率全都不变）。母版自己从注入里取，这条链就只剩一处可能出错，且错在母版里、看得见。
 *
 * 显式 props 仍然优先：探针与单测靠它钉值，而生产里模板怎么写都不影响结果。
 * `none` 与「没给」在这里是**两回事**（没给才回退），所以调用方不能再给默认值。
 */
function resolveShotMotion(props: Partial<ShotMotionInputs>, injected: unknown): ShotMotionInputs {
  const src = (injected && typeof injected === 'object' ? injected : {}) as Record<string, unknown>;
  const name = (v: unknown, fallback: unknown): ShotMotionStyle => {
    const raw = v === undefined || v === null ? fallback : v;
    const s = String(raw ?? 'none');
    return (MOTION_STYLE_SET.has(s) ? s : 'none') as ShotMotionStyle;
  };
  const frames = (v: unknown, fallback: unknown): number => {
    const raw = v === undefined || v === null ? fallback : v;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
  };
  return {
    entrance: name(props.entrance, src.entrance),
    exit: name(props.exit, src.exit),
    entranceFrames: frames(props.entranceFrames, src.entranceFrames),
    exitFrames: frames(props.exitFrames, src.exitFrames),
  };
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

// ---------------------------------------------------------------------------
// R12-T60 分区揭示：**逐区域 mask**（闸㉘ 的申报 → 闸㉗ 判它）
//
// 这一族的画法是「把画面当纸，一块一块按顺序画出来」。母版负责的是最后一步：
// 按 `reveal.regions[]` 逐帧算出**每一块此刻允许被看见的那部分**，把它当作剪切路径
// 套到这一镜的插画（`<img>`）上。
//
// 三条设计决定，逐条都有理由：
//
// 1. **坐标只在母版里换算一次**。区域的四栏是「插画原图像素」（`canvas`），母版按
//    这张图**这一帧的实况盒**折算成成片像素——它是唯一拿得到那个盒的一侧。Python 侧
//    （㉗）不自己算这套换算：算第二遍就是两把尺，而母版算的是实况、Python 算的是估算，
//    两把尺迟早对不上。所以母版把算出来的结果**逐帧报出去**（`DG_REVEAL:` 行），
//    ㉗ 采样的就是这份账。
// 2. **允许掩码 = 块 − 后续块 − 受保护的块**（闸㉘ 的原话）。「后续块」扣掉是因为
//    后面那一块到点才该画：不扣的话，一块的推进会先把后面那块的位置刷出来，
//    画面上提前露出——正是 ㉗ 要抓的那件事。
// 3. **读同一帧时钟**：进度按下式算，`frame` 就是 `useCurrentFrame()`。
//    `code_render.py` 那条「命中的盒与代表帧说同一时刻」是同一句话。
// ---------------------------------------------------------------------------

/** 分区揭示的最小输入（与 Python `code_spec.check_reveal_regions` 的栏位同名）。 */
export interface RevealRegion {
  id: string;
  sequence: number;
  region: { x: number; y: number; w: number; h: number };
  direction: 'ltr' | 'rtl' | 'ttb' | 'btt' | 'whole';
  start_ms: number;
  duration_ms: number;
  protected?: string[];
  label?: string;
}

export interface RevealSpec {
  canvas: { w: number; h: number };
  scene_duration_ms?: number;
  regions?: RevealRegion[];
}

/** 一带内的一个矩形（成片像素，左上角）。与 `region` 同形，单独起名只为读起来清楚。 */
export interface RevealRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const REVEAL_DIRECTIONS = new Set(['ltr', 'rtl', 'ttb', 'btt', 'whole']);

/** 注入的 props → 分区揭示的输入（把认不出的写法挡在外面，与母版别的回退同规）。
 *
 *  `revealProbeFrames` 是 Python 点名要账的那几帧（㉗ 的采样时刻）。**它不是画法的
 *  一部分**：不进缓存键（键在 Python 侧按 `reveal` 算），也不改变任何像素——它只决定
 *  母版在哪几帧多发一行 console.log。所以故意做成与 `reveal` 并列的第二个注入项，
 *  不塞进 `reveal` 里：塞进去的话「同一份申报、不同的采样时刻」会变成两份不同的申报。 */
export function resolveReveal(injected: unknown):
{ spec: RevealSpec | null; probeFrames: number[] } {
  const src = (injected && typeof injected === 'object' ? injected : {}) as Record<string, unknown>;
  const rawReveal = src.reveal;
  let spec: RevealSpec | null = null;
  if (rawReveal && typeof rawReveal === 'object') {
    const r = rawReveal as RevealSpec;
    const canvas = r.canvas;
    const okCanvas = !!canvas && Number(canvas.w) > 0 && Number(canvas.h) > 0;
    const regions = Array.isArray(r.regions) ? r.regions : [];
    if (okCanvas && regions.length) {
      spec = {
        canvas: { w: Number(canvas.w), h: Number(canvas.h) },
        scene_duration_ms: Number(r.scene_duration_ms ?? 0),
        regions: regions.filter((g) => !!g && !!g.region && REVEAL_DIRECTIONS.has(g.direction)),
      };
      if (!spec.regions!.length) spec = null;
    }
  }
  const rawFrames = Array.isArray(src.revealProbeFrames) ? src.revealProbeFrames : [];
  const probeFrames = rawFrames
    .map((v) => Number(v))
    .filter((v) => Number.isFinite(v) && v >= 0)
    .map((v) => Math.round(v));
  return { spec, probeFrames };
}

/** 这一块在 `progress`（0~1）时**已经被画到**的那部分；`whole` 只有全有全无。
 *
 *  推进方向是**这一块的 mask 前沿往哪走**，不是画面往哪移。四种都按同一份 region 盒
 *  切一刀：左→右就是「已画宽度 = w×进度」。 */
export function sweepRect(region: RevealRect, direction: string, progress: number): RevealRect {
  const p = Math.min(1, Math.max(0, progress));
  if (direction === 'whole') return { ...region };
  if (direction === 'rtl') return { x: region.x + region.w * (1 - p), y: region.y, w: region.w * p, h: region.h };
  if (direction === 'ttb') return { x: region.x, y: region.y, w: region.w, h: region.h * p };
  if (direction === 'btt') return { x: region.x, y: region.y + region.h * (1 - p), w: region.w, h: region.h * p };
  return { x: region.x, y: region.y, w: region.w * p, h: region.h };
}

/** `a` 减去若干矩形 → 剩下的矩形清单（可能 0 片）。
 *
 *  一进一出的扣法（每次切最多成 4 片：上/下/左/右），所以 n 次扣除最多 4n 片。
 *  为什么不用「多边形布尔运算」：这一族只需要矩形，而一个多边形裁剪库引进来是给整个
 *  渲染容器加一条依赖——这四行算术自己就是它的全部用处。小数坐标一路留着，
 *  取整只发生在最后写进 `clip-path` 那一刻（中途取整会在扣出的小片上积累缝隙）。 */
export function subtractRects(a: RevealRect, others: RevealRect[]): RevealRect[] {
  let pieces: RevealRect[] = [a];
  for (const o of others) {
    const next: RevealRect[] = [];
    for (const p of pieces) {
      const ix = Math.max(p.x, o.x);
      const iy = Math.max(p.y, o.y);
      const ir = Math.min(p.x + p.w, o.x + o.w);
      const ib = Math.min(p.y + p.h, o.y + o.h);
      if (ir <= ix || ib <= iy) { next.push(p); continue; }   // 不相交：原样留下
      const pr = p.x + p.w;
      const pb = p.y + p.h;
      if (iy > p.y) next.push({ x: p.x, y: p.y, w: p.w, h: iy - p.y });            // 上
      if (ib < pb) next.push({ x: p.x, y: ib, w: p.w, h: pb - ib });              // 下
      if (ix > p.x) next.push({ x: p.x, y: iy, w: ix - p.x, h: ib - iy });        // 左
      if (ir < pr) next.push({ x: ir, y: iy, w: pr - ir, h: ib - iy });           // 右
    }
    pieces = next;
  }
  return pieces.filter((p) => p.w > 0.5 && p.h > 0.5);
}

/** 一块在时刻 `tMs` 的进度（线性，无缓动）。
 *
 *  为什么**不加缓动**：㉗ 判的是「到点的那一刻画满了没有」，缓动只会让「到点」这个
 *  时刻在画面上变得更模糊（末端仍在慢慢逼近）。这一族的观感来自区域划分，不来自
 *  这一条的缓动曲线——要缓动是母版 `entrance` 那一档的事。
 *  `duration_ms=0` 定义为「瞬时」（t ≥ start 就整块可见），不做除零。 */
export function revealProgress(region: RevealRegion, tMs: number): number {
  if (region.direction === 'whole' || !region.duration_ms) return tMs >= region.start_ms ? 1 : 0;
  if (tMs <= region.start_ms) return 0;
  return Math.min(1, (tMs - region.start_ms) / region.duration_ms);
}

/** 某一帧、每一块**允许被看见**的那部分，单位是**画布像素**（＝申报的坐标系）。
 *
 *  刻意留在画布像素里：换算是**两个不同的去处**（`clip-path` 要元素本地像素、
 *  `DG_REVEAL:` 那一行要成片像素），在这里乘上任何一个比例都会让另一个去处拿错尺。
 *  于是这一层只算「哪一块、到点没有、被扣成哪几片」，换算是调用方两行乘法的事。
 *
 *  返 `{id, sequence, rects}`——按 sequence 升序，**没到点的块也在**（`rects: []`）：
 *  「这一块此刻什么都不该显示」与「这一块不在这次申报里」在 ㉗ 的账上是两件事。 */
export function revealMasks(spec: RevealSpec, frame: number, fps: number):
Array<{ id: string; sequence: number; rects: RevealRect[] }> {
  const regions = (spec.regions ?? []).slice().sort((a, b) => a.sequence - b.sequence);
  const tMs = fps > 0 ? (frame / fps) * 1000 : 0;
  const byId = new Map(regions.map((r) => [r.id, r]));
  return regions.map((r) => {
    if (tMs < r.start_ms) return { id: r.id, sequence: r.sequence, rects: [] };
    const drawn = sweepRect(r.region, r.direction, revealProgress(r, tMs));
    const cut: RevealRect[] = [];
    for (const later of regions) {
      if (later.sequence > r.sequence) cut.push(later.region);
    }
    for (const pid of r.protected ?? []) {
      const g = byId.get(String(pid));
      if (g && g.id !== r.id) cut.push(g.region);
    }
    const rects = subtractRects(drawn, cut).filter((p) => p.w > 0 && p.h > 0);
    // 一片都没被扣掉时**原样返回推进到哪**，不走减法：减法里的浮点误差会让「画满了」
    // 少掉最后一根发丝（w 从 808 变成 807.99999），而 ㉗ 的采样网格是贴着这条边排的。
    if (!cut.length) return { id: r.id, sequence: r.sequence, rects: [drawn] };
    return { id: r.id, sequence: r.sequence, rects };
  });
}

/** 画布像素 → 另一套坐标（`sx`/`sy` 是「一个画布像素跨几个目标像素」，`ox`/`oy` 是原点）。
 *  两个去处共用这一行乘法，两处各写一遍就会在某一处漏掉原点。 */
export function mapRects(rects: RevealRect[], sx: number, sy: number,
                         ox = 0, oy = 0): RevealRect[] {
  const q = (n: number) => Math.round(n * 100) / 100;
  return rects.map((r) => ({ x: q(ox + r.x * sx), y: q(oy + r.y * sy),
                             w: q(r.w * sx), h: q(r.h * sy) }));
}

/** 一帧的允许掩码（元素本地像素）→ `clip-path` 的路径串（多子路径＝并集）。
 *
 *  为什么用 `path()` 而不是几个 `inset()`：并集没法用一条 `inset()` 表达（它只切一个
 *  矩形），而 `mask-image` + `mask-composite` 在 headless Chrome 里各版本行为不一。
 *  `clip-path: path()` 是同一份几何的最简表达，且它在 SVG/HTML 上是同一套规则。
 *  **空清单返回 `'none'`（不裁）而不是空串**：空串是非法值，浏览器整条忽略——
 *  于是「本该全是纸底」的一帧反而全显示，而这一族唯一的验收靠的就是那一帧。 */
export function clipPathOf(rects: RevealRect[]): string {
  if (!rects.length) return 'none';
  const seg = rects.map((r) => (`M${r.x},${r.y} H${r.x + r.w} V${r.y + r.h} H${r.x} Z`));
  return `path("${seg.join(' ')}")`;
}

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

/**
 * 屏幕盒 → **成片像素坐标**（`useBody()` 报的那一套：画面带左上角在 (gutter, topSafe)）。
 *
 * 为什么要有这一步：R7-T22 的进出场是母版套在整帧上的一层 `transform`，
 * 而 `getBoundingClientRect()` 量出来的是**变换之后**的屏幕盒。不折回去的话，
 * 一个「整片淡入 + 上浮」的片子会让闸⑧ 报一片「文字被画面带裁掉」（其实是被那层
 * 变换推出去了）、闸⑪ 把一张**静态**图判成「参与动效」（两帧的盒差了那点位移）——
 * 两条都是假读数，而它们读的是同一个 DOM。
 *
 * 折法：同一帧里量一次画面带自己的盒（`live`），再按它的缩放 `s = live.w / 静态宽`
 * 把子元素的盒还原。带子与子元素被同一个变换影响，所以相除之后剩下的就是**代码写下的
 * 那个位置**——「整片有进出场」于是对这两条判据完全透明。没有进出场的片子
 * `live` 就等于静态盒、`s = 1`，这一步是恒等变换（老片子的读数一个字节不变）。
 *
 * 结果是四舍五入到 0.01px 的，而 ⑪ 是**逐字节**比两帧的盒：两帧的 `s` 不同，浮点尾数
 * 就可能落在舍入边界的两侧（量级 1e-7/盒），那是噪声不是位移——真动起来的东西两帧差的是
 * 若干个像素。这一层不做「容差比对」是有意的：容差会把「静态图在动与不动的边界上」这件事
 * 一起放宽，而 ⑪ 要判的正是那个边界（`server.mjs` 的 `boxSame && transformSame &&
 * opacitySame`）。
 */
function toCodeSpace(box: PxBox, live: PxBox, base: PxBox): PxBox {
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
function liveBand(el: HTMLElement | null): PxBox | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (!(r.width > 0) || !(r.height > 0)) return null;
  return { x: r.left, y: r.top, w: r.width, h: r.height };
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

/** 屏幕像素 → 成片像素的换算（见 :func:`toCodeSpace`）；纯点也走它，
 *  所以「盒」与「引线端点」不会各折一次、折出两个口径。 */
type Convert = (box: PxBox) => PxBox;

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

function subjectBoxes(root: Element, convert: Convert): PxBox[] {
  const out: PxBox[] = [];
  for (const el of Array.from(root.querySelectorAll('*'))) {
    if (!SUBJECT_TAGS.has(el.tagName.toLowerCase())) continue;
    if (!paintedWithin(root, el)) continue;
    const raw = measureBox(el);
    if (!raw || raw.w < 1 || raw.h < 1) continue;
    out.push(convert(raw));
  }
  return out;
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

/** 母版在探针帧上做一次版面回读。
 *
 *  `convert` 把这一帧量到的屏幕盒折回**成片像素**（见 :func:`toCodeSpace`）：
 *  母版那层进出场变换是整帧一起动的，折回去之后三条判据读到的还是「代码写下的版面」。
 *  没有进出场的片子 `convert` 是恒等变换，读数与从前逐字节相同。
 */
function probeLayout(root: Element, band: PxBox, convert: Convert) {
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
    const raw = measureBox(el);
    if (!raw || raw.w < LAYOUT_PROBE.min_box_px || raw.h < LAYOUT_PROBE.min_box_px) continue;
    texts.push({ text: text.slice(0, 24), box: convert(raw), el });
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
  const drawn = drawnBoxes(root, convert);
  // 闸⑲-a / ⑳(a) 的两个读数（T30）：主体件占带多少、文字占带多少。判据在 Python，
  // 这里只把这一帧量到的两个数如实报上去（量不到就是 0，别编一个「差不多」的数）。
  const subject = subjectBoxes(root, convert);
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

/**
 * 母版 Composition —— 新链里唯一有排版权的东西。
 *
 * 独占：背景、安全区、字阶、配色、字幕带的位置与高度。镜头代码只能通过 children
 * 往画面带里填东西，拿不到字幕绘制权 —— 于是「这一镜字幕比上一镜高 3 像素」这类
 * 漂移在结构上不可能发生，而不是靠闸⑤事后抓。
 *
 * R14 起这三件事改成**按风格档排**（底色 / 满幅 / 字幕形态，见 `FilmStyle`）：
 * 档从注入里读，镜头代码仍旧碰不到它。唯一的例外是 `subtitleMode='inline'`——
 * 那一档母版**故意不画**字幕，把绘制权还给镜头代码（文字要随构图摆，只有它知道
 * 主体在哪）。「独占」这句因此改成「独占**默认**的排版」：不申报风格档时，
 * 上面那份独占逐字不变。
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
  // 这四条**不给默认值**：`resolveShotMotion` 要能分清「镜头代码没转交」与「转交了 none」，
  // 前者回退到注入的那一份（模板漏抄一行不该让整片静默地不演）。
  entrance,
  exit,
  entranceFrames,
  exitFrames,
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
  // R14：风格档（底色 / 满幅）与字幕形态。三件事都从注入里取，镜头代码碰不到——
  // 与 `reveal`/`entrance` 同一条回退逻辑。不传 `film_style` 时 `style` 就是缺省值，
  // `bg` 落回 `TOKENS.bg`、`bleed` 为 false，于是老片子的读数一个字节都不变。
  const style = resolveFilmStyle(getInputProps());
  const bg = style.bg || TOKENS.bg;
  const tone = captionTone(bg);
  const body = filmBody(width, height, aspect, style.bleed);
  // 满幅时字幕条**叠在画面上**（没有下安全区可站），位置由几何表给；
  // 非满幅时仍旧站在下安全区正中——今天的行为逐字不变。
  const captionBottom = style.bleed
    ? geo.bleedCaptionBottom
    : (geo.bottomSafe - geo.caption.height) / 2;

  const captionIn = interpolate(frame, [0, 0.25 * fps], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  // R7-T22 整片进出场：画面带与字幕带一起动（整帧的一个观感决定），背景留在外面。
  // 窗口帧数由 Python 侧算好注入 —— 这里再算一遍那条 10%/15 帧的算术就有了第二个出处。
  // 四条取值走 `resolveShotMotion`：镜头代码转交了就用它给的，没转交就取注入的那份。
  const motionIn = resolveShotMotion({ entrance, exit, entranceFrames, exitFrames },
                                     getInputProps());
  // R12-T60：分区揭示的申报（母版按它逐帧算 mask）。与上面那条同一条回退逻辑：
  // 镜头代码转交不了它（它本来就不是代码的数据），一律读注入的那一份。
  const revealIn = resolveReveal(getInputProps());
  const motion = composeMotion(
    shotMotion(motionIn.entrance, motionIn.entranceFrames, frame, durationInFrames, height, true),
    shotMotion(motionIn.exit, motionIn.exitFrames, frame, durationInFrames, height, false),
  );

  /**
   * 这一帧的「屏幕 → 成片像素」换算（见 :func:`toCodeSpace`）。
   *
   * 闸⑥ 的字段盒、闸⑧ 的版面盒、闸⑪ 的资产盒**共用这一份**：三条判据读的是同一帧的
   * 同一个 DOM，各量一次画面带就可能量出三个口径。量不到画面带时退回恒等变换
   * （宁可少折一次，也不要报一个自己造出来的读数）。
   * 在 effect 里调用（而不是渲染时算好）：`liveBand` 量的必须是**这一帧**的实况。
   */
  const frameConvert = (): Convert => {
    const base = { x: body.x, y: body.y, w: body.w, h: body.h };
    const live = liveBand(bodyRef.current);
    return live ? (b: PxBox) => toCodeSpace(b, live, base) : (b: PxBox) => b;
  };

  const used = {
    version: 'film-layout@3',
    fontStack: TOKENS.fontStack,
    ink: TOKENS.ink,
    // R14：`bg` 报的是**这一镜实际排出去的底色**（风格档给了就用它），因为闸④ 拿
    // 这一栏当判空的基准色。`bleed` 同理是这一镜的实际取值。
    // 这两个键允许逐镜不同，Python 侧 `check_token_drift` 有一份显式白名单
    // （`TOKEN_PER_SHOT_KEYS`）把它们排除在跨镜比对之外——不排除的话，
    // 「这镜满幅、那镜有安全区」会被闸⑤ 判成「母版常量漂移」，新能力当场判死。
    bg,
    bleed: style.bleed,
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
    const convert = frameConvert();
    const marks = Array.from(document.querySelectorAll('[data-dg-field]')).map((el) => {
      const raw = el.getAttribute('data-dg-value');
      let value: unknown = raw;
      try {
        value = JSON.parse(raw ?? 'null');
      } catch {
        value = raw;   // 标注写成非 JSON 时如实回读原串，由闸⑥ 报「对不上」
      }
      const box = measureBox(el);
      return {
        name: el.getAttribute('data-dg-field') ?? '',
        value,
        // 盒折回成片像素：闸⑥ 比的是「模型说它把 value 画在哪」与注入的版面，
        // 整片那层进出场变换不该把这份对账搅成假读数（见 toCodeSpace）。
        box: box ? convert(box) : null,
      };
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
    const { findings, scanned } = probeLayout(
      root,
      { x: body.x, y: body.y, w: body.w, h: body.h },
      frameConvert(),
    );
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
    // 盒折回成片像素：两帧的**同一张静态图**在整片进出场下屏幕盒不同，
    // 不折的话判据（逐字节比两帧的盒）会把静态摆图判成「参与动效」——一条假绿。
    // 元素自身的 transform/opacity 照原样上报：母版那层变换挂在祖先上，读不到这里。
    const convert = frameConvert();
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
          box: box ? convert(box) : null,
          transform: s.transform,
          opacity: Number(s.opacity === '' ? 1 : s.opacity),
        };
      });
    console.log(`${ASSET_LOG_MARKER}${JSON.stringify({ frame, assets })}`);
  }, [frame, assetProbeEarly, probeFrame]);

  // 闸㉗ 的原料：分区揭示的**允许掩码**（R12-T60）。三个决定写在这里：
  //
  //   1. `useLayoutEffect` 而不是 `useEffect`：这一条改的是**这一帧的像素**，
  //      必须在浏览器画这一帧之前落下去。`useEffect` 在 paint 之后跑，逐帧截图时
  //      会慢一帧（截图拿到的是上一帧的 mask）——而 ㉗ 判的正是「这一帧该不该露」，
  //      慢一帧就等于把判据整体挪了一格。
  //   2. 只改 `<img>`（这一族的插画就是它）。母版不知道镜头代码把图画在哪儿，
  //      所以位置的换算按**元素自己的盒**做（canvas → 元素本地像素），
  //      与镜头代码怎么摆无关——「区域画在哪儿」于是只有一处实现。
  //   3. Python 点名的帧各发一行 `DG_REVEAL:`（它自己去抽那一帧来比色），
  //      别的帧只改像素不出声：毎帧一行会把逐镜账撑成噪声，而 ㉗ 只需要几个时刻。
  React.useLayoutEffect(() => {
    const root = bodyRef.current;
    if (!root) return;
    const { spec, probeFrames } = revealIn;
    const imgs = Array.from(root.querySelectorAll('img'))
      .filter((el) => paintedWithin(root, el)) as HTMLImageElement[];
    if (!spec) return;
    const masks = revealMasks(spec, frame, fps);
    const all = masks.flatMap((m) => m.rects);
    for (const el of imgs) {
      const sx = el.offsetWidth / Number(spec.canvas.w);
      const sy = el.offsetHeight / Number(spec.canvas.h);
      // 元素自己还没量到尺寸（图未加载）时**不裁**：裁一个 0 宽的盒等于把整张图抹掉，
      // 而 ㉗ 会把那一帧读成「未开始的区域是纸底」——一条假绿，正好是本闸要拦的那件事。
      el.style.clipPath = (sx > 0 && sy > 0) ? clipPathOf(mapRects(all, sx, sy)) : 'none';
    }
    if (!probeFrames.includes(frame)) return;
    const convert = frameConvert();
    const boxes = imgs.map((el) => {
      const b = measureBox(el);
      return { src: (el.src ?? '').slice(-80), box: b ? convert(b) : null,
               local: { w: el.offsetWidth, h: el.offsetHeight } };
    });
    console.log(`${REVEAL_LOG_MARKER}${JSON.stringify({
      frame, canvas: spec.canvas, boxes,
      // 成片像素（带内坐标）：㉗ 拿它加画面带原点就是能在帧上取样的矩形。
      // 用**成片像素**而不是元素本地像素：本地的原点在元素自己的角上，
      // 而元素的位置是镜头代码摆的——那一侧的读数会随排版漂，这一侧不会。
      regions: masks.map((m) => ({
        id: m.id, sequence: m.sequence,
        rects: mapRects(m.rects, (boxes[0]?.box?.w ?? 0) / Number(spec.canvas.w),
                        (boxes[0]?.box?.h ?? 0) / Number(spec.canvas.h),
                        boxes[0]?.box?.x ?? 0, boxes[0]?.box?.y ?? 0),
      })),
    })}`);
  }, [frame, fps, revealIn]);

  return (
    <AbsoluteFill style={{ background: bg, fontFamily: TOKENS.fontStack }}>
      {/* 闸⑤ 的回读锚点：必须是「实际用上的值」，不是常量表副本 */}
      <meta name="dg-tokens" content={JSON.stringify(used)} />

      {/* R7-T22 整片进出场：画面带 + 字幕带一起动，**背景留在这一层之外**——
          连背景一起动的话位移就会露出画布边缘，一股「贴纸被推进来」的味道。
          也没动 `<meta>`：闸⑤ 读的是排出去的几何，不是这一帧的瞬时状态。 */}
      <AbsoluteFill style={{ opacity: motion.opacity, transform: motion.transform }}>
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

        {/* R14：`inline` 档母版**不画字幕**——文字由镜头代码自己排进画面。
            为什么不是母版换个位置画：参考片那种「文字压在画面上、随构图摆」
            本质上是**这一镜的构图决定**，只有镜头代码知道它把主体放在哪。
            母版插一脚就又是「每镜一条固定字幕带」。text/highlight/words 本来
            就在 `props` 里，镜头代码一直拿得到，这一档只是把绘制权还回去。 */}
        {subtitleMode !== 'none' && subtitleMode !== 'inline' && text ? (
          <Caption
            text={text}
            highlight={highlight}
            words={words}
            mode={subtitleMode}
            opacity={captionIn}
            geo={geo}
            bottom={captionBottom}
            tone={tone}
          />
        ) : null}
      </AbsoluteFill>
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
  /** 字幕条底边到画面底的距离。非满幅时是下安全区正中（今天的行为），
   *  满幅时是几何表给的叠层位置——母版算好传进来，不在这里再判一次满幅。 */
  bottom: number;
  /** 字幕条的两个颜色，按这一镜底色亮度算（见 `captionTone`）。 */
  tone: { paper: string; ink: string };
}> = ({ text, highlight, words, mode, opacity, geo, bottom, tone }) => {
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
        bottom,
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
          background: tone.paper,
          borderRadius: cap.radius,
          color: tone.ink,
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
