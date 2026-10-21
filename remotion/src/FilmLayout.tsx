import React, { useLayoutEffect, useMemo, useRef, useState } from 'react';
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
  ART_BRUSHES,
  ART_DEFAULT,
  ART_LIGHTS,
  ART_MOTIONS,
  ART_TEXTURES,
  ART_TYPES,
  ASPECTS,
  CAPTION_STYLES,
  HIGHLIGHT_STYLES,
  FIELD_LOG_MARKER,
  FIELD_PROBE_TAIL,
  FILM_GEOMETRY,
  FILM_STYLE_DEFAULT,
  TOKENS,
  TOKEN_LOG_MARKER,
} from './tokens';
import type {
  ArtDirection, Aspect, CaptionStyle, FilmStyle, HighlightStyle,
} from './tokens';
// 版面那一套（判据 + 阈值 + 那一行 JSON 的形状）2026-10-16 整体搬到 `./lib/LayoutProbe`：
// `LAYOUT_PROBE` / `LAYOUT_LOG_MARKER` 不再从这里 import——母版与新测量壳共用同一份，
// 免得「母版改了阈值、壳还在用旧的」这种一比就分叉的两把尺。
import { emitLayout, emitLayoutMissing, liveBand, measureBox, toCodeSpace } from './lib/LayoutProbe';
import type { Convert, PxBox } from './lib/LayoutProbe';

/** 高亮词的**显示方式**（`HIGHLIGHT_LOOK` 的值域，也是 `<CaptionText emphasis/>` 的值域）。
 *  - `'block'`：一块底色（`hl`）；
 *  - `'color'`：**关键词换色**——不给底，高亮词的字色换成 `accent`。字压在画面上、与图
 *    共用一块底色时用这一档：一块色块压在图上是「贴纸」，换字色才是「强调」；
 *  - `'ink'`：词后一条纸感笔触（`accent` 原色），无底——「撕纸高亮」；
 *  - `'none'`：不强化，整句一个样。
 */
export type CaptionEmphasis = 'block' | 'color' | 'ink' | 'none';

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
 * R14 风格档的注入解析 —— 与 :func:`resolveShotMotion` 同一套写法。
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
  const hex = (v: unknown) => {
    const t = String(v ?? '').trim().toLowerCase();
    return /^#[0-9a-f]{6}$/.test(t) ? t : undefined;
  };
  const paper = hex(s.paper);
  const ink = hex(s.ink);
  const style: FilmStyle = {
    bleed: s.bleed === true,
    bg: /^#[0-9a-fA-F]{6}$/.test(bg) ? bg.toLowerCase() : FILM_STYLE_DEFAULT.bg,
  };
  // paper/ink 成对才收：只来一半时字幕条退回按亮度二档，不出「浅底浅字」这种读不出来的组合。
  if (paper && ink) { style.paper = paper; style.ink = ink; }
  const accent = hex(s.accent);
  if (accent) style.accent = accent;
  // 字幕字号与两行次序（2026-10-21）：这两件是**风格包的决定**，由 manifest 的
  // `caption.size` / `caption.order` 注入，母版只做范围兜底。缺席时落 `CAPTION_LOOK`
  // 与「英文在上」——没声明过的片子一个字节都不变。
  const capSize = Number(s.captionSize);
  if (Number.isFinite(capSize) && capSize >= 0.2 && capSize <= 1.5) {
    style.captionSize = capSize;
  }
  const capOrder = String(s.captionOrder ?? '').trim().toLowerCase();
  if (capOrder === 'en-zh' || capOrder === 'zh-en') style.captionOrder = capOrder;
  // 字幕离画面底多远、字号硬上限（2026-10-21）：与上面两件同一通道（manifest 的
  // `caption.bottom` / `caption.cap`），母版只做范围兜底，声明过才生效。
  const capBottom = Number(s.captionBottom);
  if (Number.isFinite(capBottom) && capBottom >= 0.005 && capBottom <= 0.3) {
    style.captionBottom = capBottom;
  }
  const capCap = Number(s.captionCap);
  if (Number.isFinite(capCap) && capCap >= 0.02 && capCap <= 0.08) {
    style.captionCap = capCap;
  }
  // 整片美术方向（2026-10-18）：只在注入里真有 `artDirection` 时才合并，
  // 且**补全每一栏**——镜头代码读 `film.art.brush` 时不必再判 undefined。
  // 认不出的值一律落缺省（与上面风格档同一条纪律：写坏一栏不该让整镜渲不出来）。
  const rawArt = src.artDirection;
  if (rawArt && typeof rawArt === 'object') {
    const a = rawArt as Record<string, unknown>;
    const pickArt = <T extends string>(v: unknown, allowed: readonly T[], dflt: T): T => {
      const t = String(v ?? '').trim().toLowerCase();
      return (allowed as readonly string[]).includes(t) ? (t as T) : dflt;
    };
    const look = String(a.look ?? '').trim().slice(0, 160);
    const note = String(a.palette_note ?? '').trim().slice(0, 120);
    const art: ArtDirection = {
      brush: pickArt(a.brush, ART_BRUSHES, ART_DEFAULT.brush),
      light: pickArt(a.light, ART_LIGHTS, ART_DEFAULT.light),
      texture: pickArt(a.texture, ART_TEXTURES, ART_DEFAULT.texture),
      type: pickArt(a.type, ART_TYPES, ART_DEFAULT.type),
      motion: pickArt(a.motion, ART_MOTIONS, ART_DEFAULT.motion),
    };
    if (look) art.look = look;
    if (note) art.palette_note = note;
    style.art = art;
  }
  return style;
}

/** `#rrggbb` + 透明度 → `rgba()`。字幕的压暗层要用这一镜自己的墨色：牛皮纸底上
 *  压纯黑会显脏，压同一套的墨才是「一张印刷品」。认不出的色按黑色回退（原行为）。 */
function rgba(hex: string, a: number): string {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/.exec(hex.toLowerCase());
  if (!m) return `rgba(0,0,0,${a})`;
  const [r, g, b] = [1, 2, 3].map((i) => parseInt(m[i], 16));
  return `rgba(${r},${g},${b},${a})`;
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

/**
 * 「镜头代码转交了就用它给的，没转交就取 Python 注入的那份」——母版与字幕元件
 * （`CaptionLayer` / `CaptionText`）共用这一条回退。
 *
 * 为什么提成模块级：元件也要读注入（`<CaptionLayer/>` 不带 props 就该等于母版那一档）。
 * 各写一份回退，就会有「母版认 `captionAnim`、元件认 `caption_anim`」这种只在没人转交时
 * 才发作的漂移——而这类漂移渲出来之前没人看得见。
 */
function pickInjected<T extends string>(prop: T | undefined, key: string,
                                        allowed: readonly T[], dflt: T): T {
  const v = prop ?? ((getInputProps() as Record<string, unknown>)[key] as T | undefined);
  return v && allowed.includes(v) ? v : dflt;
}

/** 注入里的画幅，只认 `ASPECTS` 里的那三个；认不出一律竖屏（与母版同一条回退）。 */
function injectedAspect(prop?: Aspect): Aspect {
  return pickInjected(prop, 'aspect', Object.keys(ASPECTS) as Aspect[], 'portrait');
}

/** 注入里的布尔开关。缺省 `false`——「没申报」与「申报了关」在像素上同一件事。 */
function injectedFlag(key: string): boolean {
  return (getInputProps() as Record<string, unknown>)[key] === true;
}

/**
 * 注入里的强调色（`highlightColor`，来自用户在「文字库」里钉的「强调色」那一维）：
 * 只认一条合法 `#rrggbb`，其余（空串、`rgb()`、命名色、写歪的值）一律 `undefined`
 * ＝**没钉**，跟着这一镜的色板走。
 *
 * 认不出就不猜、不换一个近似色：它是用户按下的一枚颜色，一个他没按过的颜色比缺省色
 * 更糟——缺省色至少有「我没设过」这个解释。
 */
function injectedAccent(): string | undefined {
  const raw = String((getInputProps() as Record<string, unknown>).highlightColor ?? '')
    .trim().toLowerCase();
  return /^#[0-9a-f]{6}$/.test(raw) ? raw : undefined;
}

/** 一条字幕的配方：**底**、**站在哪**、**怎么进场**、**多大**、**有没有描边**。
 *
 *  这是「字幕样式」这一维的**唯一一处**落点——`caption_style` 的七个取值到这里各自
 *  收成一组数，`<Caption>` 照着排。为什么要有这张表而不是在 `<Caption>` 里写七个
 *  `if`：样张、母版壳、镜头代码自己摆的 `<CaptionLayer/>` 三处都调同一个 `<Caption>`，
 *  配方散成条件分支之后，「这一档到底长什么样」就没有一处能读完了。 */
export interface CaptionStyleLook {
  /** 有没有那张纸条底。`false` = 字直接压在画面上（`background: transparent`）。 */
  band: boolean;
  /** 站在哪：`bottom` 下安全区 / `center` 画面正中 / `top` 顶部安全区。 */
  position: 'bottom' | 'center' | 'top';
  /** 整条入场：`fade` 淡入 / `rise` 浮上 / `pop` 弹出。 */
  anim: 'fade' | 'rise' | 'pop';
  /** 字号系数：乘在 `fitCaptionFontSize` 求出来的那个值上（只改字的大小，不改求法）。 */
  size: number;
  /** 字重与行距。纸条底那一档字轻、行紧；压屏大字那一档字重、行松。 */
  weight: number;
  lineHeight: number;
  /** 有值 = 白字黑边那一档：八向描边用这个颜色，底上再压一层渐隐。 */
  outline?: string;
  /** 字色。不填 = 跟着这一镜的纸条字色（`tone.ink`，色板命中时是色板的 `text`）。 */
  ink?: string;
}

export const CAPTION_LOOK: Record<CaptionStyle, CaptionStyleLook> = {
  // 用户否掉纸条之后的缺省：无底、下沿、淡入。字与画面共用一块底。
  // 字号系数 0.66（2026-10-19）：参考片里那条中文字幕约占画面高 4%，
  // 原先把两行 78px 的字压到 97px，像横幅不像字幕。这一档缩到约 51px。
  on_picture: { band: false, position: 'bottom', anim: 'fade', size: 0.66, weight: 700, lineHeight: 1.35 },
  // 白字黑边：亮底上也读得出来。底压一层下深上透的渐隐（不是纸条：没边界、不是色块）。
  // 2026-10-21：用户看过成片后连说两次「小一点」——0.66 在横屏上是 ~64px，
  // 短句还会顶到 1.25 倍的上限（97px），一条字幕吃掉画面下沿。现在缩到 0.44
  // （横屏约 32–36px，画面高 3–3.5%）；长句另有硬上限，见 `<Caption>`。
  outlined: { band: false, position: 'bottom', anim: 'fade', size: 0.44, weight: 800, lineHeight: 1.3,
              outline: '#000000', ink: '#ffffff' },
  // 老观感，留成选项、不当缺省。
  paper: { band: true, position: 'bottom', anim: 'fade', size: 0.72, weight: 500, lineHeight: 1.35 },
  // 整句电影感：字号略小、行距松、慢淡入。
  film_line: { band: false, position: 'bottom', anim: 'fade', size: 0.60, weight: 500, lineHeight: 1.62 },
  // 大字压屏：画面正中、字号放大、pop 入场。
  big_center: { band: false, position: 'center', anim: 'pop', size: 1.18, weight: 700, lineHeight: 1.24 },
  // 顶部横幅：纸条挪到顶部安全区、rise 入场。
  top_banner: { band: true, position: 'top', anim: 'rise', size: 0.72, weight: 500, lineHeight: 1.35 },
  // 不上字幕：母版与 `<CaptionLayer/>` 都自守不画（见闸㉜）。仍旧给一份配方——
  // `Record` 要铺满，取值取最素的那一档。
  none: { band: false, position: 'bottom', anim: 'fade', size: 0.66, weight: 500, lineHeight: 1.35 },
};

/** 被点名的词怎么被点出来（「高亮样式」那一维的落点，与字幕样式**正交**）。 */
export interface HighlightLook {
  emphasis: CaptionEmphasis;
  /** 逐字点亮：未读到的字淡显，读到就实显。 */
  karaoke: boolean;
}

export const HIGHLIGHT_LOOK: Record<HighlightStyle, HighlightLook> = {
  color: { emphasis: 'color', karaoke: false },
  // 逐词点亮与换色并不冲突：念到的词实显、还没念到的淡显，点名的词仍是强调色。
  karaoke: { emphasis: 'color', karaoke: true },
  block: { emphasis: 'block', karaoke: false },
  ink: { emphasis: 'ink', karaoke: false },
  plain: { emphasis: 'none', karaoke: false },
};

/** 八向描边：一像素一圈黑，字形自己一个像素都不吃。
 *
 *  为什么不用 `-webkit-text-stroke`：它的描边是**居中**画的，宽度吃掉的是字形自己的肉
 *  ——中文细笔画（一、十、人）描粗了会断成黑块；textShadow 只能往外长，字形不变。
 *  宽度按**字号**给、不用 `em`：字号是 `fitCaptionFontSize` 按可用宽度求出来的，
 *  `em` 在外层先解析成外层的字号，求出来多大跟写的不是一回事。（搬自
 *  `demos/typography/outline-caption`，那份 demo 现在调 `outline` prop，不再自己画。） */
function ring(color: string, w: number): string {
  // 16 向（每 22.5°）而不是 8 向。8 向只在正十字与四个角上有影子，圆弧笔画上
  // 相邻两个影子的间距到 0.7w，边缘是一圈「八瓣」——放大看就是重影/毛边。
  // 16 向把同一圈的影子铺密，粗细均匀；代价只是多 8 条 shadow，不增 DOM。
  const out: string[] = [];
  for (let i = 0; i < 16; i += 1) {
    const a = (Math.PI * 2 * i) / 16;
    out.push(`${Math.round(Math.cos(a) * w)}px ${Math.round(Math.sin(a) * w)}px 0 ${color}`);
  }
  return out.join(', ');
}

/**
 * 字幕条的三色：条底/字色（`tone`）、高亮块（`hl`）、撕纸下划线（`accent`）。
 *
 * 母版与字幕元件共用。元件自己再算一遍的话，「同一条字幕在母版里和在镜头代码摆的位置
 * 上不是同一个颜色」就是两处各取一次色的必然结果——而这两条字幕常常同屏。
 */
function captionLook(style: FilmStyle, bg: string):
    { tone: { paper: string; ink: string }; hl: string; accent: string } {
  // 强调色：**用户钉的那一枚赢过色板那一色**。用户钉的是一个明确的决定（面板上
  // 「强调色」那一行），而 `style.accent` 是色板（或镜头代码申报的底色反查）带出来的
  // 默认。没钉就走原路，一个字节都不变。
  //
  // 只管这三处用途（换色的字色 / 底色块 / 撕纸下划线），**不动条底与字色**——
  // 用户说的是「高亮词的颜色」。
  //
  // 钉住的那一枚**连 `accent` 那一栏也盖过**（`CaptionText`/`CaptionLayer` 里它排在
  // 最前）：照 SKILL 的推荐写法 `accent={palette.accent}` 写的镜是多数，若只赢在
  // 「没传 prop」时才生效，用户钉了色而绝大多数镜一点不变——比不提供这一维更坏。
  // 代价说清楚：用 `accent` 给画面图形换色的地方不受影响（Python 侧注入的
  // `film_style.accent` 仍是那一镜色板的），钉住的是**字幕**那个色。
  const accent = injectedAccent() ?? style.accent;
  return {
    tone: style.paper && style.ink ? { paper: style.paper, ink: style.ink } : captionTone(bg),
    hl: accent ? `${accent}66` : TOKENS.hl,
    accent: accent || TOKENS.accent,
  };
}

/** 字幕条底边到画面底的距离：满幅时叠在画面上（没有下安全区可站），位置由几何表给；
 *  风格包在 manifest 的 `caption.bottom` 里声明了「占画面高多少」时以它为准。 */
function captionBottomOf(geo: (typeof FILM_GEOMETRY)[Aspect], bleed: boolean,
                         height: number, frac?: number): number {
  if (bleed && frac !== undefined) return Math.round(height * frac);
  return bleed ? geo.bleedCaptionBottom : (geo.bottomSafe - geo.caption.height) / 2;
}

/** 字幕条的整条淡入（0.25 秒）。母版那条与镜头代码摆的那条共用同一条曲线。 */
function captionFadeIn(frame: number, fps: number): number {
  return interpolate(frame, [0, 0.25 * fps], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
}

/**
 * 字幕字号：按「最多两行」求，并夹在 [0.62, 1.25] × 几何表字号之间。
 *
 * 原先 fitText 直接取「整句塞进一行」的字号且不设上下限：60 字的句子缩到 14px 看不清，
 * 四五个字的短句放到 150px+ 撑破字幕盒。两行容量正是 MAX_TEXT_CHARS 的前提。
 * 母版与字幕元件共用——元件自己再求一遍，就会出现「镜头代码摆的那条字比母版那条小一号」。
 */
function fitCaptionFontSize(text: string, cap: { fontSize: number; side: number; padX: number },
                            width: number, fitWidth?: number, weight = 500): number {
  const availW = fitWidth ?? width - 2 * cap.side - 2 * cap.padX;
  let fontSize = cap.fontSize;
  try {
    const ft = remotionFitText({
      text,
      withinWidth: availW,
      fontFamily: '"Noto Sans CJK SC", sans-serif',
      // 按**真正画出来的字重**量（2026-10-21）：一直写死 500，而白字黑边那一档是 800。
      // 800 比 500 每字宽约 5%，长句排出来就顶出字幕盒两侧——真机上正是「字被切掉左右
      // 各一截」那个样子。量宽与画字用同一个字重，这半号才不会算错。
      fontWeight: weight,
    });
    const oneLine = ft.fontSize;
    // 单行放得下且不超上限 → 单行；否则按两行（单行字号 ×2 再留 8% 断行余量）。
    fontSize = oneLine >= cap.fontSize ? oneLine : oneLine * 2 * 0.92;
    // 两行容量是**几何硬上限**：每行最多 ceil(字数/2) 个字，一个汉字约一个字号宽。
    // `remotionFitText` 求的是整句排一行的字号，长句乘 1.84 后偶尔仍会超出半个字，
    // 于是最右边那个字被切掉（2026-10-20 真机：字幕左右各缺一截）。这里再按每行
    // 字数兜一次，宁可小半号，也不让字出盒。
    const perLine = Math.max(1, Math.ceil(Array.from(String(text || '')).length / 2));
    fontSize = Math.min(fontSize, availW / perLine);
  } catch {
    // lint / 构建期没有浏览器环境，退回固定字号
  }
  return Math.round(Math.min(cap.fontSize * 1.25, Math.max(cap.fontSize * 0.62, fontSize)));
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
  /** 同一句话的英文（中英双字幕的第二行）。空串/不传＝只排中文那一行。 */
  textEn?: string;
  /** 逐词点亮的词，每个都必须出现在 text 里；≤4 个 */
  highlight?: string[];
  /** 词级时间戳，由 Python 侧按真实语音时长算好后注入 */
  words?: WordTiming[];
  /** 字幕样式（贴画面 / 白字黑边 / 纸条底 / …）。不传＝注入那一档。 */
  captionStyle?: CaptionStyle;
  /** 高亮样式（关键词换色 / 逐词点亮 / 底色块 / 撕纸高亮 / 不强化）。不传＝注入那一档。 */
  highlightStyle?: HighlightStyle;
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
  if (style === 'none') return { opacity: 1, transform: 'none' };
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
    // 进出场方向必须分开：`fade` 从前写死成 `1 - p`，于是入场把这一镜淡**出**——
    // 首帧亮、窗口末帧全透明，后面整镜都看不见（真机上表现为满屏纸底 + 闸⑭ 数不到动效）。
    opacity: style === 'fade' ? (entering ? p : 1 - p) : 1,
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
  textEn = '',
  highlight = [],
  words,
  captionStyle: captionStyleProp,
  highlightStyle: highlightStyleProp,
  aspect: aspectProp,
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
  // 画幅 / 字幕档 / 字幕位置与动画：镜头代码转交了就用它给的，没转交就取 Python 注入的那份
  // （与 resolveShotMotion 同一条回退，写法见模块级的 `pickInjected`，字幕元件也走它）。
  // 原先 aspect 缺省写死 'portrait'：镜头代码漏传 aspect 时，横屏片会套竖屏的字幕几何
  // （44px 字、132 高的盒）。
  const aspect = injectedAspect(aspectProp);
  // 字幕两维（2026-10-16）：整片一档、逐镜可覆盖，注入时已经合成好（`shot_input_props`）。
  // 母版这边只是照着画——它不必知道「逐镜」这件事。
  const captionStyle = pickInjected<CaptionStyle>(captionStyleProp, 'captionStyle',
                                                  CAPTION_STYLES, 'outlined');
  const highlightStyle = pickInjected<HighlightStyle>(highlightStyleProp, 'highlightStyle',
                                                      HIGHLIGHT_STYLES, 'color');
  // 这一镜的字幕由**镜头代码自己摆**（代码里有 `<CaptionLayer/>`，Python 侧扫出来注入）。
  // 母版据此让位——否则同屏两条字幕，而那种错只在成片上才看得见。让位是不必的猜测：
  // 裸 `<CaptionLayer/>` 回落到同一份几何、同一档配方，所以「谁来摆」不影响那条字幕
  // 长什么样，闸⑤ 的跨镜一致照旧成立。
  const captionFromCode = injectedFlag('captionFromCode');
  // 闸⑧ 的扫描范围锚点：只扫画面带里面，母版自己的字幕带不算在镜头代码的账上。
  const bodyRef = React.useRef<HTMLDivElement>(null);
  // u 只管**字阶与配色**（横竖屏字号才跳不了位）；版面几何走 FILM_GEOMETRY 的设备像素，
  // 不乘 u——见 tokens.ts 那条注释，乘 u 会把上下安全区按宽轴放大，横屏正文带塌到 31%。
  const u = width / 1080;
  const geo = FILM_GEOMETRY[aspect];
  // R14：风格档（底色 / 满幅）与字幕形态。三件事都从注入里取，镜头代码碰不到——
  // 与 `entrance` 同一条回退逻辑。不传 `film_style` 时 `style` 就是缺省值，
  // `bg` 落回 `TOKENS.bg`、`bleed` 为 false，于是老片子的读数一个字节都不变。
  const style = resolveFilmStyle(getInputProps());
  const bg = style.bg || TOKENS.bg;
  // 字幕条配色：Python 侧认出这一镜的色板时（认的是代码里那句 `palettes["…"]` 绑定，
  // 与申没申报底色无关），条底/字色/强调色全部跟色板走；认不出退回按亮度二档——
  // 那一档是今天的行为，老片子一个字节不变。三色由 `captionLook` 一处给出，
  // 镜头代码用 `<CaptionLayer/>` 摆的那条也从它取——同屏两条字幕不会各是各的色。
  // 用户在「文字库」钉过「强调色」时，`captionLook` 里那一枚盖过色板的 accent
  // （只管强调色，条底与字色照旧）。
  const { tone, hl: hlColor, accent: accentColor } = captionLook(style, bg);
  const body = filmBody(width, height, aspect, style.bleed);
  // 满幅时字幕条**叠在画面上**，非满幅时仍旧站在下安全区正中——今天的行为逐字不变。
  const captionBottom = captionBottomOf(geo, style.bleed, height, style.captionBottom);

  const captionIn = captionFadeIn(frame, fps);

  // R7-T22 整片进出场：画面带与字幕带一起动（整帧的一个观感决定），背景留在外面。
  // 窗口帧数由 Python 侧算好注入 —— 这里再算一遍那条 10%/15 帧的算术就有了第二个出处。
  // 四条取值走 `resolveShotMotion`：镜头代码转交了就用它给的，没转交就取注入的那份。
  const motionIn = resolveShotMotion({ entrance, exit, entranceFrames, exitFrames },
                                     getInputProps());
  const motion = composeMotion(
    shotMotion(motionIn.entrance, motionIn.entranceFrames, frame, durationInFrames, height, true),
    shotMotion(motionIn.exit, motionIn.exitFrames, frame, durationInFrames, height, false),
  );

  /**
   * 这一帧的「屏幕 → 成片像素」换算（见 :func:`toCodeSpace`）。
   *
   * 闸⑥ 的字段盒、闸⑧ 的版面盒**共用这一份**：两条判据读的是同一帧的
   * 同一个 DOM，各量一次画面带就可能量出两个口径。量不到画面带时退回恒等变换
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
    // 以下四栏报的是**字幕条实际排出去的颜色**（色板命中时逐镜不同），不是 TOKENS
    // 常量副本——Python 侧把这四个键放进了 `TOKEN_PER_SHOT_KEYS`。
    //
    // `captionInk` 与上面的 `ink` **不是一回事**：`ink` 是母版的正文常量、全片一个值，
    // 闸⑤ 正是拿它比全片一致性；`captionInk` 是字幕条真正用来写字的那一色
    // （见下面 `Caption` 的 `color: tone.ink`），色板命中时它是色板的 `text`。
    // 只在 `ink` 里报常量，等于「字幕条读不读得出来」在回读表上无从判起。
    paper: tone.paper,
    captionInk: tone.ink,
    accent: accentColor,
    hl: hlColor,
    scale: Number(u.toFixed(6)),
    // 以下全是**这一版真正排出去的设备像素**：闸⑤ 容差 0 跨镜比对，
    // 母版被改坏时这里会先红，而不是等抽帧去猜。
    aspect,
    // 字幕两维：**逐镜可覆盖**，所以 Python 侧把它们放进了 `TOKEN_PER_SHOT_KEYS`
    // 白名单（`check_token_drift` 不拿它们比跨镜一致）——报的是这一镜**实际排出去的**
    // 那一对，不是整片开关的副本。
    captionStyle,
    highlightStyle,
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
      emitLayoutMissing(frame, '画面带 ref 未挂载，这一帧没扫版面', 'master');
      return;
    }
    // 判据与措辞的单源在 `./lib/LayoutProbe`（2026-10-16 从那两个文件里搬出去的）：
    // 自包含镜走同一份探针，只是 root 是整帧、`convert` 是恒等。两处共用 `emitLayout`，
    // 免得一处漏掉 `thresholds`（Python 侧 `describe_layout` 就靠它拿阈值）。
    emitLayout(root, { x: body.x, y: body.y, w: body.w, h: body.h }, frameConvert(), frame, 'master');
  }, [frame, probeFrame, body.x, body.y, body.w, body.h]);

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

        {/* 母版这条字幕。两条不画的路：
            · `caption_style: none` —— 整片（或这一镜）不上字幕；
            · 代码里摆了 `<CaptionLayer/>` —— 绘制权还给镜头代码，母版让位（否则同屏两条）。
            为什么让位这件事要由 Python 侧扫代码来决定：母版在运行期看不到那一镜的代码，
            而「有没有第二条」是**这一段代码的静态事实**。 */}
        {captionStyle !== 'none' && !captionFromCode && text ? (
          <Caption
            text={text}
            textEn={textEn}
            highlight={highlight}
            words={words}
            look={CAPTION_LOOK[captionStyle]}
            hlLook={HIGHLIGHT_LOOK[highlightStyle]}
            opacity={captionIn}
            geo={geo}
            bottom={captionBottom}
            tone={tone}
            hl={hlColor}
            accent={accentColor}
          />
        ) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Caption: React.FC<{
  text: string;
  /** 英文那一行（中英双字幕）。空串＝只排中文。 */
  textEn?: string;
  highlight: string[];
  words?: WordTiming[];
  /** 字幕样式那一维的配方（`CAPTION_LOOK`）：底 / 位置 / 入场 / 字号 / 描边。 */
  look: CaptionStyleLook;
  /** 高亮样式那一维的配方（`HIGHLIGHT_LOOK`）：`emphasis` 与要不要逐字点亮。 */
  hlLook: HighlightLook;
  opacity: number;
  geo: (typeof FILM_GEOMETRY)[Aspect];
  /** 字幕条底边到画面底的距离。非满幅时是下安全区正中（今天的行为），
   *  满幅时是几何表给的叠层位置——母版算好传进来，不在这里再判一次满幅。 */
  bottom: number;
  /** 字幕条的两个颜色：色板命中时是色板的 surface/text，否则按底色亮度二档（见 `captionTone`）。 */
  tone: { paper: string; ink: string };
  /** 高亮词块底色（色板 accent 加 40% 透明，或全局粉）。 */
  hl: string;
  /** 强调色：`emphasis: 'color'` 时是高亮词的字色，`'ink'` 时是那条笔触的颜色。 */
  accent: string;
}> = ({ text, textEn = '', highlight, words, look, hlLook, opacity, geo, bottom, tone, hl, accent }) => {
  const frame = useCurrentFrame();
  const { width, height, fps } = useVideoConfig();
  const cap = geo.caption;
  const position = look.position;
  // 风格包可以改两件事，母版只做兜底（2026-10-21）：字号系数与中英次序。
  // 都由 manifest 的 `caption.size` / `caption.order` 进来；没声明过的片子拿到的
  // 就是 `CAPTION_LOOK` 那一档与「英文在上」，像素与以前完全一致。
  const filmStyle = resolveFilmStyle(getInputProps());
  const sizeMul = filmStyle.captionSize ?? look.size;
  const enFirst = (filmStyle.captionOrder ?? 'en-zh') === 'en-zh';

  // 入场动画（整条字幕）：只在前 0.3 秒内生效，之后静止，不抢画面。
  const t = Math.min(1, frame / Math.max(1, Math.round(0.3 * fps)));
  const ease = 1 - Math.pow(1 - t, 3);
  const enterStyle: React.CSSProperties =
    look.anim === 'rise' ? { transform: `translateY(${(1 - ease) * cap.fontSize * 0.8}px)` }
      : look.anim === 'pop' ? { transform: `scale(${0.86 + 0.14 * ease})` }
        : {};

  const place: React.CSSProperties =
    position === 'center' ? { top: 0, bottom: 0 }
      : position === 'top' ? { top: geo.topSafe + Math.round(height * 0.02) }
        : { bottom };

  // 白字黑边这一档的配色：**任何时候都是纯白字、纯黑边**。
  //
  // 以前在浅底 + 认得出色板时换成这一镜的纸色与墨色，理由是「纯白纯黑压在牛皮纸上
  // 像综艺花字」。2026-10-20 用户否掉了这个自动换色：要的就是黑边白色字。换色的
  // 实际结果也不稳定——色板里的「纸／墨」是这一镜的印刷色，浅色色板上它是深底浅字，
  // 描边跑到了字形里面，看上去是「白边黑字」，与选的这一档正相反。
  // 所以这一档不再看色板：白就是 `#ffffff`，黑就是 `#000000`。
  const textColor = look.ink ?? tone.ink;
  const ringColor = look.outline;
  const scrimInk = null;

  // 中英双字幕（2026-10-21，用户定的样子）：**英文在上、中文在下，两行同号**、
  // 同宽同色同描边，按各自那句话排，不按句义重排。
  //
  // 字号在这里算一次、**显式传给** `<CaptionText/>`（`sizeScale=1`）：两行字号若各按
  // 自己的 `fitCaptionFontSize` 求一遍，就会出现「中文一档、英文另一档」这种只有成片
  // 才看得出来的错位。现在两行共用一个字号（`zhSize`），英文那行直接用同一个数。
  const en = String(textEn || '').trim();
  // 字号硬上限（2026-10-21）：按**画面高**给，不再按字数放行。两行同号之后合计
  // 约占 0.036H × 2.4；横屏 1080p 上是 39px。短句以前会顶到 1.25 倍上限，
  // 一条字比主体的字还大——这条盖子把它压回来。第二版再缩一档：用户看过样张后
  // 说「整体字号缩小」，中英两行一起落。
  const sizeCap = Math.round(height * (filmStyle.captionCap ?? (en ? 0.036 : 0.04)));
  const zhSize = Math.max(16, Math.min(sizeCap, Math.round(
    fitCaptionFontSize(text, cap, width, undefined, look.weight) * sizeMul)));
  const enSize = zhSize;
  const capGap = Math.round(zhSize * 0.14);

  // 英文那一行：与中文同号、同一圈描边，字重跟着中文那档走（白字黑边是 800），
  // 两行看上去是一块字幕。间距挂在英文那一侧：拆成 `en-zh` / `zh-en` 两种次序后，
  // 谁在上谁就吃「朝同伴那一侧」的留白。
  const enBlock = en ? (
    <div
      style={{
        marginTop: enFirst ? 0 : capGap,
        marginBottom: enFirst ? capGap : 0,
        fontSize: enSize,
        fontWeight: look.weight,
        lineHeight: 1.22,
        letterSpacing: 0.2,
        color: textColor,
        textAlign: 'center',
        overflowWrap: 'anywhere',
        // 与中文同一圈描边、同一宽度算法（按字号给）：两行在亮底上一样读得出来。
        textShadow: ringColor
          ? ring(ringColor, Math.max(2, Math.round(enSize * 0.085)))
          : undefined,
      }}
    >
      {en}
    </div>
  ) : null;

  // 字（逐词点亮 / 换色 / 底色块 / 撕纸笔触 / 自动字号）只有一份实现：`CaptionText`。
  // 镜头代码用 `<CaptionLayer/>` 摆出来的那条走的也是它——「母版这条字幕」与
  // 「镜头代码自己摆的那条」因此不可能长得不一样。
  const zhBlock = (
    <CaptionText text={text} highlight={highlight} words={words}
                 emphasis={hlLook.emphasis} karaoke={hlLook.karaoke}
                 hl={hl} accent={accent} cap={cap}
                 fontSize={zhSize} sizeScale={1} outline={ringColor} />
  );

  return (
    <div
      style={{
        position: 'absolute',
        left: cap.side,
        right: cap.side,
        ...place,
        // 盒高是下限不是定值：两行字时盒子长高，不再溢出到盒外。
        // 满幅（叠在画面上）时**不留这条带高**：`place.bottom` 就是字块的底边，
        // 想离画面底一行字就写一行字。留着 `cap.height` 的盒高等于把字往上顶半
        // 条带子（横屏 240/2=120px），`bleedCaptionBottom` 写多少都落不下来——
        // 2026-10-21 用户说「字幕位置下调，离底部一行字」，量了样张：字底离底 147px。
        minHeight: position === 'center' || filmStyle.bleed ? undefined : cap.height,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        opacity,
      }}
    >
      {/* 白字黑边那一档的压暗层：亮底上白字照样糊，把字所在的那一段压暗。
          **不是纸条**——没有边界、不是纯色块，是一条下深上透的渐隐；所以它跟着
          这一档走（`look.outline`），也只在下沿位置铺（压屏大字/顶部横幅不需要它）。
          铺满整帧宽：`left/right` 反向让出外层那两条 `cap.side`。 */}
      {look.outline && position === 'bottom' ? (
        <div
          style={{
            position: 'absolute',
            left: -cap.side, right: -cap.side,
            top: -Math.round(cap.height * 1.7),
            bottom: -Math.round(cap.height * 0.9),
            background: `linear-gradient(to top, ${rgba(scrimInk ?? '#000000', 0.66)} 0%, `
              + `${rgba(scrimInk ?? '#000000', 0.36)} 34%, `
              + `${rgba(scrimInk ?? '#000000', 0.12)} 66%, `
              + `${rgba(scrimInk ?? '#000000', 0)} 100%)`,
            pointerEvents: 'none',
          }}
        />
      ) : null}
      <div
        style={{
          position: 'relative',
          maxWidth: '100%',
          // 一行里塞了长英文/长数字时按容器断行，不让它顶出画面两侧
          // （2026-10-21：用户要「不要超出屏幕」）。
          overflowWrap: 'anywhere',
          wordBreak: 'break-word',
          // 纸条底那一档才有底色、内衬与圆角；无底那几档字直接压在画面上。
          // 内衬也一起去掉：无底还留着 34px 的内衬，字就比纸条档缩进一圈，
          // 同一个 `bottom` 下两种观感的字不在同一条线上。
          padding: look.band ? `${cap.padY}px ${cap.padX}px` : 0,
          background: look.band ? tone.paper : 'transparent',
          borderRadius: look.band ? cap.radius : 0,
          color: textColor,
          // 字号不在这里给：纸条只管底、内衬与圆角，字号归 `CaptionText`（一处）。
          // 行高/字重仍旧写在这里，由字继承——它们是这一档的排版。
          fontWeight: look.weight,
          lineHeight: look.lineHeight,
          textAlign: 'center',
          ...enterStyle,
        }}
      >
        {/* 两行按 `caption.order` 排（缺省英文在上，2026-10-21 用户点名）。两块各自
            带自己的间距：英文在上时隔一截再落中文，中文在上时反过来——次序换了，
            留白也得跟着换，否则会像两段不相干的字。 */}
        {enFirst ? enBlock : null}
        {zhBlock}
        {enFirst ? null : enBlock}
      </div>
    </div>
  );
};

/** 每个字符被读到的起始帧；没有词时间轴（或一个词都对不上）返回 null。
 *  词按出现顺序从游标往后找，找不到的词跳过；词之间的标点沿用前一个词的帧。 */
export function charStartFrames(text: string, words?: WordTiming[]): number[] | null {
  if (!words?.length || !text) return null;
  const n = text.length;
  const frames: (number | null)[] = new Array(n).fill(null);
  let cursor = 0;
  let matched = 0;
  for (const w of words) {
    if (!w.text) continue;
    const at = text.indexOf(w.text, cursor);
    if (at < 0) continue;
    for (let k = at; k < at + w.text.length; k++) frames[k] = w.startFrame;
    cursor = at + w.text.length;
    matched++;
  }
  if (!matched) return null;
  const out: number[] = new Array(n);
  let last = 0;
  for (let k = 0; k < n; k++) {
    if (frames[k] !== null) last = frames[k] as number;
    out[k] = last;
  }
  return out;
}

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

/**
 * 字幕的时间轴：这一镜的整句、高亮词切好的片段、每个字被读到的那一帧。
 *
 * 一次算齐，母版、字幕元件与镜头代码拿到的都是同一份。要「字幕与画面互动」的东西
 * ——图形跟着念到的词跳一下、进度条随词推进——用 `reached(i)` 就够了，
 * 不必自己再实现一遍 `charStartFrames`（那正是这条链最怕的第二份出处）。
 */
export interface CaptionTiming {
  /** 这一镜的整句（不传 props 时取自注入）。 */
  text: string;
  /** 高亮词（不传 props 时取自注入）。 */
  highlight: string[];
  words?: WordTiming[];
  /** 有词时间轴＝能逐词点亮；没有就只有整句一直亮（`reached` 恒 true）。 */
  timed: boolean;
  /** 每个字被读到的起始帧；没有词时间轴时为 null。 */
  charFrames: number[] | null;
  /** 第 `at` 个字这一刻被读到了没有。 */
  reached: (at: number) => boolean;
  /** 整句按高亮词切好的片段，`start` 是它在整句里的字符下标。 */
  pieces: { p: string; start: number }[];
}

export function useCaptionTiming(opts: {
  text?: string;
  highlight?: string[];
  words?: WordTiming[];
} = {}): CaptionTiming {
  const injected = getInputProps() as Record<string, unknown>;
  const text = opts.text ?? (typeof injected.text === 'string' ? injected.text : '');
  const highlight = opts.highlight
    ?? (Array.isArray(injected.highlight) ? (injected.highlight as string[]) : []);
  const words = opts.words ?? (injected.words as WordTiming[] | undefined);
  const frame = useCurrentFrame();
  // 逐字时间：把 TTS 词按出现顺序对到字符区间上，得到每个字被读到的那一帧。
  // 原先拿「片段序号」去取「词序号」的时间（words[i]），片段与词不是一回事；
  // 高亮也要求 TTS 词是高亮词的子串，TTS 分得比高亮词长时整镜不亮。
  const charFrames = useMemo(() => charStartFrames(text, words), [text, words]);
  let cursor = 0;
  const pieces = splitByHighlights(text, highlight).map((p) => {
    const start = cursor;
    cursor += p.length;
    return { p, start };
  });
  return {
    text,
    highlight,
    words,
    timed: charFrames !== null,
    charFrames,
    reached: (at: number) => charFrames === null || frame >= charFrames[at],
    pieces,
  };
}

export interface CaptionTextProps {
  /** 画面上的那行字。不传＝注入里的 `text`（这一镜的整句）。 */
  text?: string;
  highlight?: string[];
  words?: WordTiming[];
  /** 高亮词**怎么显示**。不传＝跟着注入的「高亮样式」那一维（`HIGHLIGHT_LOOK`）。
   *  取值与含义见 `CaptionEmphasis` 的注释。 */
  emphasis?: CaptionEmphasis;
  /** 逐字点亮（未读到的字淡显）。不传＝跟着注入的「高亮样式」那一维。 */
  karaoke?: boolean;
  /** 高亮块底色 / 撕纸笔触颜色。不传＝跟着这一镜的色板（与母版同源）。 */
  hl?: string;
  /** 强调色。`emphasis: 'color'` 时它就是**高亮词的字色**；`'ink'` 时是那条笔触的颜色。 */
  accent?: string;
  /** 字幕条的几何（字号基准、左右边距、内衬）。不传＝注入画幅的母版几何。 */
  cap?: { fontSize: number; side: number; padX: number };
  /** 求字号的可用宽度（设备像素）。不传＝母版字幕条的容量。 */
  fitWidth?: number;
  /** 直接给字号，跳过自动求值（要与画面里别的元素对齐时用）。 */
  fontSize?: number;
  /** 字号系数：乘在求出来的字号上。母版按 `CAPTION_LOOK[...].size` 传（大字压屏要放大
   *  一号、整句电影感要收一点），镜头代码自己摆字时缺省 1。 */
  sizeScale?: number;
  /** 八向描边色（白字黑边那一档）。给了就用 `ring()` 画一圈，字形自己一个像素不吃。 */
  outline?: string;
  /** 「字幕样式」那一维。不传＝注入那一档（`<CaptionFrame/>` 用它把外框的决定传进来）。 */
  captionStyle?: CaptionStyle;
  /** 「高亮样式」那一维。不传＝注入那一档。 */
  highlightStyle?: HighlightStyle;
}

/**
 * 字幕的**字**：逐词点亮、关键词换色、底色块、撕纸笔触、按两行容量求字号。
 *
 * 不画纸条底、不自带定位——底与位置由调用方给（母版给它那一档的底，镜头代码给自己那层
 * scrim / 笔刷 / 抠图）。「让文字压在画面上」这条路不必手搓：`<CaptionText/>`
 * 摆的是**画面里的文字**。两维样式**不传就跟着注入走**，所以它排出来的字与母版那条
 * （`<Caption>` → 也是它）在同一档下逐字相同。
 */
export const CaptionText: React.FC<CaptionTextProps> = ({
  text, highlight, words, emphasis: emphasisProp, karaoke: karaokeProp,
  hl: hlProp, accent: accentProp, cap: capProp, fitWidth, fontSize: sizeProp,
  sizeScale: sizeScaleProp, outline,
  captionStyle: captionStyleProp, highlightStyle: highlightStyleProp,
}) => {
  const { width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const t = useCaptionTiming({ text, highlight, words });
  // 字幕样式先定：白字黑边这一档的字色本来就是统一的，下面的高亮是否生效要参考它。
  const capStyle = pickInjected<CaptionStyle>(captionStyleProp, 'captionStyle', CAPTION_STYLES,
                                              'outlined');
  // 两维都不给 prop 时跟着**注入**那一档走：镜头代码把字摆进画面时，「高亮怎么显示」
  // 本来就是整片（或这一镜）的一个观感决定，不该由每一份现写的代码各答一次。
  // 缺省是 `plain`（2026-10-20：用户点名「字幕默认黑边白字」，此前缺省 color 会在
  // 白字里混进几枚强调色的词，整条字幕看着不统一）。
  const injectedHl = pickInjected<HighlightStyle>(highlightStyleProp, 'highlightStyle',
                                                   HIGHLIGHT_STYLES, 'plain');
  const hlLook = HIGHLIGHT_LOOK[injectedHl];
  // 白字黑边 + 整片 plain 时，镜头代码里残留的 `emphasis="color"` 不再生效。
  // 要关键词上色，就把整片的「高亮样式」改成 color，而不是每镜各写一份。
  const emphasis = (injectedHl === 'plain' && capStyle === 'outlined')
    ? 'none'
    : (emphasisProp ?? hlLook.emphasis);
  const karaoke = karaokeProp ?? hlLook.karaoke;
  const style = resolveFilmStyle(getInputProps());
  const look = captionLook(style, style.bg || TOKENS.bg);
  const hlColor = hlProp ?? look.hl;
  // 用户在「文字库」钉过的强调色**排在最前**：它在 `captionLook` 里已经盖过色板，
  // 这里再排到 `accent` 那一栏之前——否则照推荐写法（`accent={palette.accent}`）写的
  // 每一镜都会把用户挑的颜色顶掉，界面上钉了色、片子里一点没变（这一类「看着对、
  // 渲出来不是」正是这一处要治的病）。没钉过时这一枚是 `undefined`，一切照旧。
  const accentColor = injectedAccent() ?? accentProp ?? look.accent;
  const cap = capProp ?? FILM_GEOMETRY[injectedAspect()].caption;
  // 字幕样式那一维：`<CaptionText/>` 从前只认 `outline` 这个 prop，于是「文字库」里
  // 选了「白字黑边」，镜头代码自己摆的字照旧继承外层写的 `color: ink`——界面上选了、
  // 片子里还是黑字。这里让它与母版 `<Caption>` 走同一个配方：没传 `outline` 时取注入
  // 那一档的描边与字色。字色/描边**不再跟色板换**（理由见 `<Caption>` 里那一段：
  // 换完之后浅色色板上描边跑到字形里面，成了白边黑字）。
  const capLook = CAPTION_LOOK[capStyle];
  const ringColor = outline ?? capLook.outline;
  const textColor = ringColor ? (capLook.ink ?? '#ffffff') : undefined;
  const sizeScale = sizeScaleProp ?? capLook.size;
  // 字号：先按可用宽度求（最多两行），**再按画面高兜一次**。自排字幕（给了 `fitWidth`）
  // 的盒宽由镜头代码挑，字就跟着宽度一路长到 1.25 倍：真机上横屏两行 90px、高 245px，
  // 压住大半台手机。两行总高不超过画面高的 20%（横屏满字号下约等于几何表的 78px）是
  // 「字是画面的一部分、但不吃掉画面」的那条线。母版那条不带 `fitWidth`，不受这条。
  const autoSize = fitCaptionFontSize(t.text, cap, width, fitWidth,
                                       capLook.weight);
  const bounded = fitWidth ? Math.min(autoSize, (height * 0.2) / (2 * 1.4)) : autoSize;
  const fontSize = Math.round((sizeProp ?? bounded) * sizeScale);
  // 描边宽度按**字号**给（不是 `em`）：字号是上面求出来的，`em` 在外层先解析成外层的
  // 字号，写出来的与求出来的不是一回事。系数搬自 `outline-caption` 那份 demo。
  const shadow = ringColor
    ? ring(ringColor, Math.max(3, Math.round(fontSize * 0.085)))
    : undefined;

  // ---- 字幕安全网：**任何摆法都不许把字切在画外** ----
  //
  // 2026-10-18 真机成片：镜头代码把字幕盒写在 `top: height*0.86`，两行字落到底边
  // 之外，第二行被整条切掉；有的镜还把 0.62 宽的盒子从 0.45 宽处起排，右边出画。
  // 这不是某一镜的笔误——写手对「两行字有多高」没有可靠的数是常态，而预览版面
  // 读数只报不拦，最后只能让用户在看片时发现。
  //
  // 所以把兜底放在**字自己的组件**里：量一次真实包围盒，超出的部分平移回来。
  // 平移而不是改字号/改父盒：字号是 `fitCaptionFontSize` 一处的产物，父盒是镜头
  // 代码的构图；只有「整体挪一点」不改这两件事。量到的 `rect` 里包含祖先的入场
  // 动画（上浮/缩放）与自己的位移，先减掉 `shift` 才是**没有我们这一层**的位置。
  const boxRef = useRef<HTMLSpanElement>(null);
  const [shift, setShift] = useState({ x: 0, y: 0 });
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return;
    const mx = 24;
    const my = 24;
    const left = r.left - shift.x;
    const right = r.right - shift.x;
    const top = r.top - shift.y;
    const bottom = r.bottom - shift.y;
    let dx = 0;
    let dy = 0;
    if (right > width - mx) dx = width - mx - right;
    if (left + dx < mx) dx = mx - left;
    if (bottom > height - my) dy = height - my - bottom;
    if (top + dy < my) dy = my - top;
    if (Math.abs(dx - shift.x) > 0.5 || Math.abs(dy - shift.y) > 0.5) {
      setShift({ x: Math.round(dx * 10) / 10, y: Math.round(dy * 10) / 10 });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height, frame, text, fontSize, shift.x, shift.y]);

  return (
    <span
      ref={boxRef}
      style={{
        display: 'inline-block',
        maxWidth: '100%',
        // 中文长句必须真的在盒子里折行：只靠 CJK 的默认断行，一旦外层或高亮片段
        // 带了 `nowrap`，整句会排成一条比画面还宽的单行，安全网只能把它平移进画，
        // 两端仍会被切掉（2026-10-20 真机字幕左右各缺一截）。
        whiteSpace: 'normal',
        wordBreak: 'break-word',
        overflowWrap: 'anywhere',
        transform: shift.x || shift.y ? `translate(${shift.x}px, ${shift.y}px)` : undefined,
      }}
    >
      {t.pieces.map(({ p, start }, i) => {
        const isHl = t.highlight.some((h) => h && h === p);
        const on = isHl && t.reached(start);
        // 高亮词整体占一块底色：中文可任意断行，「一座城市」被劈成两行时行尾会留下
        // 一个只包着「一」字的孤立色块。短词锁住不拆；长片段照旧折行，免得撑破字幕盒。
        const keep = p.length <= 12 && isHl;
        const tinted = on && emphasis === 'color';
        const blocked = on && emphasis === 'block';
        // 撕纸笔触：词点亮后留一条下划线（色板 accent，未命中色板时是全局红），
        // 「撕开」的观感。粗细跟字号走，三种画幅下才是同一条线。
        const inked = on && emphasis === 'ink';
        const hlStyle: React.CSSProperties = {
          // 字号由**这里**给（不是由外层的纸条/盒子给）：`fitCaptionFontSize` 的产物
          // 只有一处，母版那张纸条与镜头代码自己那层盒子才可能排出同一个字号。
          // 行高/字重/颜色仍旧从外面继承——那些是调用方的排版。
          fontSize,
          whiteSpace: keep ? 'nowrap' : undefined,
          background: blocked ? hlColor : 'transparent',
          // 字色只在「白字黑边」那一档由这里给；其余档不写，继续继承调用方的排版
          // （镜头代码给这一镜挑的墨色/纸色）。
          color: tinted ? accentColor : textColor,
          textShadow: shadow,
          // 描边档的字重也归这里：母版那条在外层写了 800，镜头代码直接摆
          // `<CaptionText/>` 时没人写，字落回 400——细笔画的肉还没描边宽，白字
          // 只剩一根头发丝（2026-10-20 真机看片就是这么糊的）。只在这一档补。
          fontWeight: ringColor ? capLook.weight : undefined,
          borderBottom: inked
            ? `${Math.max(2, Math.round(fontSize / 26))}px solid ${accentColor}`
            : 'none',
        };
        if (!karaoke || !t.timed) {
          return <span key={i} style={hlStyle}>{p}</span>;
        }
        // 逐字点亮：未读到的字淡显，读到就实显——整句一直在，眼睛能提前扫读。
        return (
          <span key={i} style={hlStyle}>
            {Array.from(p).map((ch, j) => (
              <span key={j} style={{ opacity: t.reached(start + j) ? 1 : 0.38 }}>{ch}</span>
            ))}
          </span>
        );
      })}
    </span>
  );
};

/** `<CaptionFrame/>` 交给画面那一格：**这一镜真正能画的地方**（成片像素，原点 0,0）。
 *  与 `useBody()` 同一个形状，只是已经扣掉了字幕带与间距。 */
export type ArtBox = BodyBox;

export interface CaptionFrameProps {
  /** 画面。传函数时拿到这一格的尺寸（`w`/`h` 就是可用宽高，原点在画面区左上角）；
   *  传节点时它自己用 `useVideoConfig()` 减字幕带也可以，但函数形式是推荐写法。 */
  children: React.ReactNode | ((art: ArtBox) => React.ReactNode);
  /** 字幕带在哪条边：底（缺省）/ 顶。 */
  side?: 'bottom' | 'top';
  /** 字幕带高度（设备像素）。缺省按这一镜的字幕档算：两行字 + 内衬。 */
  band?: number;
  /** 画面与字幕带之间的空隙（设备像素）。缺省 = 画面高的 1.5%。 */
  gap?: number;
  /** 字幕带的左右内衬。画面不受影响（仍旧整宽）。 */
  inset?: number;
  /** `false` = 只分带、不画字（画面把字幕带也占掉）。 */
  caption?: boolean;
  /** 字幕在带里的对齐。缺省左对齐。 */
  align?: 'left' | 'center' | 'right';
  captionStyle?: CaptionStyle;
  highlightStyle?: HighlightStyle;
  /** 外框样式：**底色画在这一层**（画面区被裁到字幕带边上，画在里面的底色铺不到带里）。 */
  style?: React.CSSProperties;
}

/**
 * 画/字分带：**结构上**保证字幕与画面不互相遮挡。
 *
 * 为什么需要这一件。真机片里字幕压在插画上，不是「字太大了」这一件事，而是镜头代码
 * 把画面画满整帧、再把字绝对定位到 0.86 高的地方——两者都是绝对值，撞不撞全看写手
 * 那一刻的估算。评审会报、安全网会平移，但那两条都是事后补救：一个把字挪到主体上，
 * 一个只是记一笔。
 *
 * 分带把这件事变成**布局**：这一镜先划出字幕带（高度由字幕档算，不由写手估），画面
 * 只拿到剩下的那一格，并且**被裁在这一格里**（`overflow:hidden`）。于是「字压图」在
 * 结构上不发生，哪怕画面画得比这一格还大——超出部分被裁掉，而不是压到字上。
 *
 * 代价写清楚：画面会被裁（写手要按 `art.h` 排版，别按整帧高）；底色要画在 `style` 上，
 * 画在画面区里的底色铺不到字幕带。这两条都是「分带」这张契约本身的代价，不是缺陷。
 *
 * 不做成母版那样自动叠一条：位置、左右内衬、带高都还是这一镜的构图决定。要与画面
 * 咬合得更紧（字当图注、文字当图形）时，仍旧用 `<CaptionText/>` 自己摆。
 */
export const CaptionFrame: React.FC<CaptionFrameProps> = ({
  children, side = 'bottom', band, gap, inset, caption = true, align = 'left',
  captionStyle, highlightStyle, style,
}) => {
  const { width, height } = useVideoConfig();
  const film = resolveFilmStyle(getInputProps());
  const aspect = aspectFor(width, height);
  const geo = FILM_GEOMETRY[aspect];
  const cap = geo.caption;
  const capStyle = pickInjected<CaptionStyle>(captionStyle, 'captionStyle', CAPTION_STYLES,
                                              'outlined');
  // 字幕带高度：按这一档的字号上限算两行（与 `<CaptionText/>` 的「两行不超过画面高
  // 20%」同一条线），再加内衬。写手不必估「两行有多高」——这是它一直在估错的那个数。
  // 字号要乘上这一档的 `size`：`on_picture` 已经缩到 0.66，带子若仍按满字号算，
  // 就会空出一大条黑带（真机上正是「画面被压掉四分之一」的那一版）。
  const bandH = Math.max(1, band ?? Math.max(
    cap.height,
    Math.min(Math.round(height * 0.2),
             Math.round(cap.fontSize * CAPTION_LOOK[capStyle].size * 1.25 * 2 * 1.4))
      + cap.padY * 2,
  ));
  const gapPx = Math.max(0, gap ?? Math.round(height * 0.015));
  const insetPx = Math.max(0, inset ?? cap.side);
  // 「不上字幕」这一档不割地：带子不画，画面就用整帧（分带只在真有字要站的时候才有意义）。
  const showCaption = caption && capStyle !== 'none';
  // 带子 + 空隙不能吃掉整帧：留至少三分之一给画面。
  const carve = showCaption ? Math.min(bandH + gapPx, Math.round(height * 0.66)) : 0;
  const art: ArtBox = { x: 0, y: 0, w: width, h: Math.max(1, height - carve), aspect };
  return (
    <div style={{ position: 'absolute', inset: 0, background: film.bg || TOKENS.bg, ...style }}>
      {/* 画面区：**被裁在字幕带边上**。写手按 `art` 排版时它正好铺满这一格；
          画得比这一格大，多出来的部分裁掉，不会压到字上。 */}
      <div style={{
        position: 'absolute', left: 0, right: 0,
        top: side === 'top' ? carve : 0,
        bottom: side === 'bottom' ? carve : 0,
        overflow: 'hidden',
      }}>
        {typeof children === 'function' ? children(art) : children}
      </div>
      {showCaption ? (
        <div style={{
          position: 'absolute', left: insetPx, right: insetPx,
          ...(side === 'top' ? { top: gapPx } : { bottom: gapPx }),
          height: bandH,
          display: 'flex', alignItems: 'center',
          justifyContent: align === 'center' ? 'center'
            : align === 'right' ? 'flex-end' : 'flex-start',
        }}>
          <CaptionText captionStyle={capStyle} highlightStyle={highlightStyle}
                       fitWidth={width - 2 * insetPx} />
        </div>
      ) : null}
    </div>
  );
};

export interface CaptionLayerProps {
  /** 不传＝注入里的那一份（这一镜的整句）。 */
  text?: string;
  highlight?: string[];
  words?: WordTiming[];
  /** 字幕样式那一维。不传＝注入的那一档。**`none` 不画**（见下面的自守）。 */
  captionStyle?: CaptionStyle;
  /** 高亮样式那一维。不传＝注入的那一档。 */
  highlightStyle?: HighlightStyle;
  /** 纸条底与字色。不传＝跟着这一镜的色板（与母版同源）。 */
  tone?: { paper: string; ink: string };
  hl?: string;
  accent?: string;
  /** 画幅（决定字号与左右让出多少）。不传＝注入的那一档。 */
  aspect?: Aspect;
  /** 纸条底边到画面底的距离（只对下沿那一档生效）。不传＝母版这一档的位置。 */
  bottom?: number;
  /** 整条的透明度。不传＝与母版同一条 0.25 秒淡入。 */
  opacity?: number;
}

/**
 * 整条字幕：底 + 字 + 入场动画，与母版画的是**同一个组件**（`Caption`），
 * 因此「镜头代码摆的字幕」与「母版那条字幕」在结构上就是同一条，不可能漂移。
 *
 * 位置跟着**最近的定位祖先**走：把它放进自己 `position: relative` 的盒子里，字幕就
 * 相对那个盒子摆——这就是「随构图摆」要的自由度。不带 props 时全部取注入的那一份，
 * 于是裸 `<CaptionLayer/>` 就等于「母版这一档」。
 *
 * **摆了这个元件，母版就让位**：Python 侧扫到代码里有 `<CaptionLayer` 会给母版注入
 * `captionFromCode`，母版那条不再画。所以「谁来摆」由**代码的静态事实**定，运行期
 * 不必猜——同屏两条字幕那种只在成片上看得见的错，从结构上不可能发生。
 *
 * **自守**：生效的字幕样式是 `none` 时不画。那一档的意思就是「这一镜不上字幕」，
 * 画出来是自相矛盾。契约层还有闸㉜：申报了 `none` 却在代码里摆 `<CaptionLayer/>`
 * 直接打回，免得这段代码静悄悄地不生效。
 */
export const CaptionLayer: React.FC<CaptionLayerProps> = ({
  text, highlight, words, captionStyle: captionStyleProp,
  highlightStyle: highlightStyleProp, tone: toneProp, hl: hlProp, accent: accentProp,
  aspect: aspectProp, bottom: bottomProp, opacity,
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const aspect = injectedAspect(aspectProp);
  const geo = FILM_GEOMETRY[aspect];
  const captionStyle = pickInjected<CaptionStyle>(captionStyleProp, 'captionStyle',
                                                  CAPTION_STYLES, 'outlined');
  const highlightStyle = pickInjected<HighlightStyle>(highlightStyleProp, 'highlightStyle',
                                                      HIGHLIGHT_STYLES, 'color');
  const style = resolveFilmStyle(getInputProps());
  const look = captionLook(style, style.bg || TOKENS.bg);
  const t = useCaptionTiming({ text, highlight, words });

  // 一次一条，不逐帧刷屏：渲染服务的浏览器日志每条都要过一遍 onBrowserLog。
  React.useEffect(() => {
    if (captionStyle === 'none' && t.text) {
      console.warn('[dg] <CaptionLayer/> 在 caption_style="none" 下不画：这一镜申报了'
        + '不上字幕。要自己摆字幕，就把这一镜的 caption_style 换成别的档。');
    }
  }, [captionStyle, t.text]);

  if (captionStyle === 'none') return null;

  return (
    <Caption
      text={t.text}
      highlight={t.highlight}
      words={t.words}
      look={CAPTION_LOOK[captionStyle]}
      hlLook={HIGHLIGHT_LOOK[highlightStyle]}
      opacity={opacity ?? captionFadeIn(frame, fps)}
      geo={geo}
      bottom={bottomProp ?? captionBottomOf(geo, style.bleed, height, style.captionBottom)}
      tone={toneProp ?? look.tone}
      hl={hlProp ?? look.hl}
      accent={injectedAccent() ?? accentProp ?? look.accent}
    />
  );
};
