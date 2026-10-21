/**
 * 母版设计 token —— 全片唯一排版真源。
 *
 * 闸⑤（跨镜一致性）比对的就是这张表：`<FilmLayout>` 把它**实际用上的**值序列化进
 * `<meta name="dg-tokens">`，渲染完读回来落进逐镜账。镜头代码读不到这些数，
 * 所以「20 镜字幕位不差一像素」是结构上的，不是每镜运气好。
 *
 * 单位分两套：**字阶与配色**按 `width/1080` 整体缩放（写死成片像素会让竖屏/横屏/
 * 方图之间字号跳一位——旧链踩过，同一件事）；**版面几何**不缩放，按画幅查
 * `FILM_GEOMETRY` 的设备像素。下面 `gutter/safeTop/safeBottom/bandHeight` 四个
 * 因此只是竖屏基准值，真正排出去的是那张表——留着是给历史镜头与人工对账看参照。
 */

export const TOKENS = {
  /** 字体栈：只用镜像里真装着的字族。列一个没装的名字＝静默回落到宋体（旧链 THEME_FONTS 的教训） */
  fontStack: '"Noto Sans CJK SC","Noto Sans SC",sans-serif',
  fontSerif: '"Noto Serif CJK SC","Noto Serif SC",serif',
  fontMono: '"Noto Sans Mono CJK SC",monospace',

  /** 字阶（设计像素） */
  sizeTitle: 96,
  sizeBody: 44,
  sizeCaption: 44,
  sizeNote: 28,

  /** 配色 */
  bg: '#f6f1e4',
  paper: 'rgba(255,255,255,0.82)',   // 字幕条与纸片底色
  ink: '#23211c',
  accent: '#a8352c',
  hl: '#f0b7bd',

  /** 版面 */
  gutter: 72,          // 页边距
  safeTop: 96,         // 上安全区
  safeBottom: 320,     // 下安全区：字幕带 + 驻留呼吸
  bandHeight: 132,     // 字幕带高度
  bandRadius: 14,
} as const;

export type TokenKey = keyof typeof TOKENS;
export type Tokens = typeof TOKENS;

/** 画幅 → 成片像素与 fps 之外的几何参数。 */
export const ASPECTS = {
  portrait: { width: 1080, height: 1920 },
  landscape: { width: 1920, height: 1080 },
  square: { width: 1080, height: 1080 },
} as const;

export type Aspect = keyof typeof ASPECTS;

/**
 * 画幅 → **成片设备像素**的版面几何。
 *
 * 为什么不按 `TOKENS.safeTop * (width/1080)` 换算：横屏那个系数是 1.7778，
 * 它把上下安全区一起放大成 171 / 569px，正文带只剩 1664×340——占画面高 31%，
 * 于是每个母题都被裁成画面的一小条（实测成片里约占 9% 面积）。安全区是给
 * 字幕与呼吸留的，它的合理尺寸跟画面**高度**有关、不跟宽度有关，等比缩放
 * 恰好把它按错的轴放大了。旧声明式链本来另有一套 16:9 版式（带子 1760×658），
 * 迁到母版时退成了这一条等比缩放——这张表就是把那一步找回来的地方。
 *
 * 数值来源：`bottomSafe` 由字幕带容器高 + 上下留白反推（不是调参调出来的），
 * `caption.fontSize` 沿用等比缩放原本的观感（横屏 44×1.7778≈78），所以
 * **只有画面带变大了，字幕没变样**。
 */
export interface CaptionGeometry {
  height: number;
  fontSize: number;
  side: number;
  radius: number;
  padX: number;
  padY: number;
}

export interface FilmGeometry {
  topSafe: number;
  bottomSafe: number;
  gutter: number;
  caption: CaptionGeometry;
  bleedCaptionBottom: number;
}

export const FILM_GEOMETRY: Record<Aspect, FilmGeometry> = {
  portrait: {
    topSafe: 96, bottomSafe: 300, gutter: 72,
    caption: { height: 132, fontSize: 44, side: 72, radius: 14, padX: 34, padY: 18 },
    bleedCaptionBottom: 86,
  },
  landscape: {
    topSafe: 48, bottomSafe: 300, gutter: 60,
    caption: { height: 240, fontSize: 78, side: 108, radius: 25, padX: 60, padY: 32 },
    bleedCaptionBottom: 48,
  },
  square: {
    topSafe: 56, bottomSafe: 260, gutter: 64,
    caption: { height: 132, fontSize: 44, side: 72, radius: 14, padX: 34, padY: 18 },
    bleedCaptionBottom: 48,
  },
} as const;

/** 字幕样式：这一行字长什么样、站在哪。与 `code_spec.CAPTION_STYLES` 是同一份枚举，
 *  两处一起改（`tests/test_code_spec.py` 第 ⑤ 节拿正则把这一行抠出来逐项比）。
 *
 *  2026-10-16 面板重做那一轮，原先揉在 `subtitle_mode`/`caption_position`/`caption_anim`
 *  三个旋钮里的选择拆成两件不相干的事——「字长什么样」（这里）与「被点名的词怎么被
 *  点出来」（`HIGHLIGHT_STYLES`）。拆开之后「要关键词换色、但不要纸条」这类组合才说得出口。
 *
 *  每一档落到画面上是什么样，由本文件下方 `CAPTION_LOOK` **一处**说了算。 */
export const CAPTION_STYLES = ['on_picture', 'outlined', 'paper', 'film_line',
                               'big_center', 'top_banner', 'none'] as const;
export type CaptionStyle = (typeof CAPTION_STYLES)[number];

/** 高亮样式：被点名的词怎么被点出来。与上面那一维**正交**，取值域互不相干。
 *  落到 `CaptionText` 上就是把 `emphasis` 与「要不要逐字点亮」定下来（见 `HIGHLIGHT_LOOK`）。 */
export const HIGHLIGHT_STYLES = ['color', 'karaoke', 'block', 'ink', 'plain'] as const;
export type HighlightStyle = (typeof HIGHLIGHT_STYLES)[number];

/** 闸⑤ 回读行的行首标记。server.mjs 里是同名字面量，两处必须一起改。 */
export const TOKEN_LOG_MARKER = 'DG_TOKENS:';

/** 闸⑥ 与字段级命中表的回读行（走与 token 同一条 console.log 通道）。
 *  为什么不再开一个浏览器通道：第二次开页抓 DOM 等于把这一镜的渲染成本再加一半，
 *  而这条链的固定成本本来就压在 bundle 与浏览器启动上。 */
export const FIELD_LOG_MARKER = 'DG_FIELDS:';

/** 探针帧 = 末帧往前这一格。母版在这一帧扫一遍 `[data-dg-field]` 的盒，
 *  Python 侧的代表帧也从同一帧取——于是「界面点到的位置」与「用户看到的这一帧」
 *  是同一个时刻，不必再解释为什么框子对不上画面。 */
export const FIELD_PROBE_TAIL = 2;

/** 闸⑧ 版面回读的行首标记（与闸⑤⑥ 同一条 console.log 通道，同一探针帧）。
 *  server.mjs 里是同名字面量，两处必须一起改。 */
export const LAYOUT_LOG_MARKER = 'DG_LAYOUT:';

/**
 * 闸⑧ 版面回读的判据阈值。全部走「宁可漏报，不许误报」这一侧：
 * 这一路的产出是**记账的 warning**，不是拦截，所以误报的代价是模型与用户开始
 * 无视整条通道，比漏报一条引线贵得多。
 *
 * - `overlap_ratio`：两处文字盒的交集面积占**较小那盒**的比例门槛。这个数是判据选型，
 *   不是量出来的：它由 `tests/layout-probe.mjs` 两支夹具钉住——叠字的镜必须报、
 *   正常排开的镜必须一声不响。改这个数就得同时改那两支，否则它只是一句好看的话。
 * - `min_box_px`：边长小于这个数的盒不当文字（动效刚开始的一瞬、1px 的占位）。
 * - `clip_px`：文字盒越出画面带**多少**像素才算被裁。母版那层是 `overflow:hidden`，
 *   越界就是真被切，不是留白；给 3px 是为了躲开亚像素取整。
 * - `anchor_ratio` / `anchor_min_px`：引线端点算「挨着某物」的容差，按画面带短边
 *   的比例给（横竖屏同一判据），再兜一个绝对下限。
 * - `leader_min_px`：短于这条长度的线不叫引线（那是下划线/分隔线），不判悬空。
 * - `subject_overlap_ratio`（2026-10-16 新增）：一块字**被图形件压掉**的面积占
 *   **这块字自己**的比例门槛。分母取文字盒而不是图形件：一件铺得很开的装饰形状
 *   会把「÷ 图形件」压到接近 0，于是这条判据永远不响——问的必须是「这块字还读得出吗」。
 * - `subject_max_share`（同上）：面积占画面带 **超过**这个比例的图形件不算主体，是**底色**。
 *   没有这一道，每一镜那层铺满整帧的 `<rect>` 会让**每一条字幕**都报「字压图」，
 *   而这条通道的产出是账不是拦截——一开始误报，模型与用户会一起无视整条通道。
 * - `max_findings`：一行 JSON 最多带几条，多了只报条数——响应要进逐镜账，
 *   一段狂叠的版式不该把整镜的账撑大。
 */
export const LAYOUT_PROBE = {
  overlap_ratio: 0.4,
  min_box_px: 6,
  clip_px: 3,
  anchor_ratio: 0.02,
  anchor_min_px: 20,
  leader_min_px: 40,
  subject_overlap_ratio: 0.3,
  subject_max_share: 0.85,
  max_findings: 8,
} as const;

/**
 * R14 风格档：把母版的三件事（底色 / 画面带 / 字幕形态）从常量改成**按档排**。
 *
 * 为什么走**注入**而不是 props：与 `entrance` 同一条理由——它不是
 * 「镜头代码自己的数据」，而是整片（或这一镜）的一个观感决定。镜头代码转交不了它，
 * 所以母版自己从 `getInputProps()` 里取，`useBody()` 也取同一份，两侧不可能不一致。
 *
 * 为什么**没有 palette 名**、只有三个颜色值：色板由镜头代码 `import
 * "@dg/themes/palettes.json"` 自己选（闸⑩ 就是照代码里绑的名字去解析颜色的）。
 * 母版再收一个 palette 名，同一件事就有两个出处，迟早对不上。所以 Python 侧
 * 拿这一镜申报的 `bg` 去 `palettes.json` 反查（唯一出处），命中就把该色板的
 * `surface/text/accent` 三个**值**塞进来——母版不认名字，只认色值。
 * 没命中（自造底色）时三栏缺席，字幕条退回按亮度二档的老行为。
 *
 * 为什么不进 `used` 的跨镜比对：`bleed`/`bg` 与它们派生的字幕条四色
 * （`paper`/`captionInk`/`accent`/`hl`）**允许逐镜不同**（那正是它们存在的理由），
 * 而闸⑤ 判的是「20 镜排版有没有漂移」。Python 侧 `check_token_drift` 有一份显式的
 * 逐镜白名单 `TOKEN_PER_SHOT_KEYS`，这些键在里面；`used.ink`（母版的正文常量）不在，
 * 它正是闸⑤ 要比的那一类。
 */
export interface FilmStyle {
  /** 满幅：画面带 = 整帧，去掉 gutter 与上下安全区。
   *  「满幅」是画得出整屏插画/描线的前提——不设它时所有镜头都被裁进中间一条带子里，
   *  这正是成片「每镜都是同一个框」的结构原因。 */
  bleed: boolean;
  /** 底色。空串 = 用 `TOKENS.bg`（今天的行为）。
   *  必须是 `#rrggbb`：闸④ 拿它当判空的基准色，读不到基准色它拒绝判（见
   *  `code_render._bg_of`）。 */
  bg: string;
  /** 字幕条底色（色板的 surface）。缺省 = 按 `bg` 亮度二档（`captionTone`）。 */
  paper?: string;
  /** 字幕条字色（色板的 text）。与 `paper` 成对出现才生效。 */
  ink?: string;
  /** 强调色（色板的 accent）：高亮词块＝它加 40% 透明，撕纸下划线＝它原色。 */
  accent?: string;
  /** 字幕字号系数（乘在 `fitCaptionFontSize` 上）。**只有风格包在 manifest 的
   *  `caption.size` 里声明了才出现**（Python：`code_spec.film_style_of` →
   *  `shot_input_props` 的 `film_style`）。缺席 = 用 `CAPTION_LOOK` 那一档的缺省，
   *  于是没声明过的片子逐镜像素不变。 */
  captionSize?: number;
  /** 中英两行的次序：`en-zh`＝英文在上（缺省）、`zh-en`＝中文在上。同样只在
   *  风格包 manifest 的 `caption.order` 声明了才出现。 */
  captionOrder?: 'en-zh' | 'zh-en';
  /** 满幅字幕**字块底边到画面底**的距离（占画面高的比例，0.005–0.3）。缺省落
   *  `FILM_GEOMETRY[..].bleedCaptionBottom`（一行字）。同样只在 manifest 的
   *  `caption.bottom` 声明了才出现。 */
  captionBottom?: number;
  /** 字幕字号硬上限（占画面高的比例，0.02–0.08）。缺省中英两行 0.036、只有中文 0.04。
   *  只在 manifest 的 `caption.cap` 声明了才出现。 */
  captionCap?: number;
  /** 整片美术方向。**只有 spec 真写了 `art` 才出现**（Python：`code_spec.norm_art`
   *  → `code_render.shot_input_props` 的 `artDirection`）。没写的片子这一栏缺席，
   *  于是所有既有镜的 `film` 读数与像素一个字节都不变。 */
  art?: ArtDirection;
}

/** 整片美术方向的取值（与 `backend/.../code_spec.py` 的 `ART_*` 逐项一致）。 */
export const ART_BRUSHES = ['graphite', 'marker', 'watercolor', 'ink'] as const;
export const ART_LIGHTS = ['top-left', 'top', 'top-right', 'left', 'front',
  'right', 'bottom-left', 'bottom', 'bottom-right', 'soft'] as const;
export const ART_TEXTURES = ['none', 'paper', 'grain'] as const;
export const ART_TYPES = ['serif-display', 'serif-editorial', 'sans-display',
  'sans-editorial'] as const;
export const ART_MOTIONS = ['spring', 'linear'] as const;

export type ArtBrush = (typeof ART_BRUSHES)[number];
export type ArtLight = (typeof ART_LIGHTS)[number];
export type ArtTexture = (typeof ART_TEXTURES)[number];
export type ArtType = (typeof ART_TYPES)[number];
export type ArtMotion = (typeof ART_MOTIONS)[number];

/** 各栏都可省：镜头代码读到的这一份**已经补全**（见 `ART_DEFAULT`），
 *  所以 `film.art.brush` 永远是一个合法枚举值，不必再判 undefined。 */
export interface ArtDirection {
  /** 整片画风那句自由描述（模型自己组合出来的语言，不受枚举限制）。 */
  look?: string;
  brush?: ArtBrush;
  light?: ArtLight;
  texture?: ArtTexture;
  type?: ArtType;
  motion?: ArtMotion;
  palette_note?: string;
}

/** 美术方向的缺省补齐。只在一镜**真写了 art** 时使用——没写 `art` 的片子
 *  `film.art` 整个缺席，所以这份缺省不会给老片子加一层「默认笔触」。 */
export const ART_DEFAULT: Required<Omit<ArtDirection, 'look' | 'palette_note'>> = {
  brush: 'graphite',
  light: 'top-left',
  texture: 'paper',
  type: 'serif-editorial',
  motion: 'spring',
};

/** 风格档的缺省值 —— **逐字等于今天**：不传 `film_style` 时母版读到的就是这一份，
 *  于是既有片子的像素与 token 读数一个字节都不变。 */
export const FILM_STYLE_DEFAULT: FilmStyle = { bleed: false, bg: '' };
