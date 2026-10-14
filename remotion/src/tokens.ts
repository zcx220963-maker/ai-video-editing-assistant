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
export const FILM_GEOMETRY = {
  portrait: {
    topSafe: 96, bottomSafe: 300, gutter: 72,
    caption: { height: 132, fontSize: 44, side: 72, radius: 14, padX: 34, padY: 18 },
    bleedCaptionBottom: 120,
  },
  landscape: {
    topSafe: 48, bottomSafe: 300, gutter: 60,
    caption: { height: 240, fontSize: 78, side: 108, radius: 25, padX: 60, padY: 32 },
    bleedCaptionBottom: 72,
  },
  square: {
    topSafe: 56, bottomSafe: 260, gutter: 64,
    caption: { height: 132, fontSize: 44, side: 72, radius: 14, padX: 34, padY: 18 },
    bleedCaptionBottom: 96,
  },
} as const;

/** 字幕形态：与旧链同一套语义，值从整片开关注入，镜头代码碰不到。
 *
 *  R14 加第四档 `inline`：**母版不画字幕条**，把 text/highlight/words 交回镜头代码
 *  自己排（那三个值本来就在 `props` 里，镜头代码一直拿得到）。这是「文字进画面、
 *  随构图摆」那一档——参考片普遍这么做，而前三档里它做不到：
 *  `full`/`torn_highlight` 都是母版在画面带下方那条固定白底圆角条里画，
 *  `none` 是整片没有字幕。`inline` 是唯一能让「这一镜的字压在画面上」成立的档。 */
export const SUBTITLE_MODES = ['full', 'torn_highlight', 'none', 'inline'] as const;

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
  max_findings: 8,
} as const;

/** 闸⑪ 资产动效回读的行首标记。与闸⑧ 同一条 console.log 通道。
 *  在首帧与探针帧各扫一遍 <img>/<video>，记录盒与 transform；
 *  server.mjs 比对两帧的快照，判定三态。 */
export const ASSET_LOG_MARKER = 'DG_ASSETS:';

/** 闸㉗ 分区揭示回读的行首标记（R12-T60/T61，与闸⑧⑪ 同一条 console.log 通道）。
 *
 *  与其它几条回读最大的不同：它**不在探针帧上扫地**，而是只在 Python 点名的
 *  那几个帧（`revealProbeFrames`）各报一行——㉗ 要的是「区域到点/未到点的那一刻
 *  画面上是什么」，那一刻是**算出来的**（区域自己的 start/duration + fps），
 *  不是末帧那一格。母版报的是**它自己算出来的允许掩码**（成片像素、带内坐标），
 *  Python 拿它去采样比色——「区域落在哪儿」只有一处实现，就是这里（两处各算一遍
 *  就是两把尺：母版按 img 的实时盒换算，Python 没有那个盒）。 */
export const REVEAL_LOG_MARKER = 'DG_REVEAL:';

/**
 * R14 风格档：把母版的三件事（底色 / 画面带 / 字幕形态）从常量改成**按档排**。
 *
 * 为什么走**注入**而不是 props：与 `reveal` / `entrance` 同一条理由——它不是
 * 「镜头代码自己的数据」，而是整片（或这一镜）的一个观感决定。镜头代码转交不了它，
 * 所以母版自己从 `getInputProps()` 里取，`useBody()` 也取同一份，两侧不可能不一致。
 *
 * 为什么只有两个字段、**没有 palette**：色板今天由镜头代码 `import
 * "@dg/themes/palettes.json"` 自己选（闸⑩ 就是照代码里绑的名字去解析颜色的）。
 * 母版再收一个 palette 名，同一件事就有两个出处，迟早对不上。母版真正缺的只是
 * 「底色是什么」——知道底色，它就能按亮度自己算字幕条该用深墨还是浅墨。
 *
 * 为什么不进 `used` 的跨镜比对：`bleed`/`bg` **允许逐镜不同**（那正是它们存在的
 * 理由），而闸⑤ 判的是「20 镜排版有没有漂移」。Python 侧 `check_token_drift` 有一份
 * 显式的逐镜白名单 `TOKEN_PER_SHOT_KEYS`，这两个键在里面。
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
}

/** 风格档的缺省值 —— **逐字等于今天**：不传 `film_style` 时母版读到的就是这一份，
 *  于是既有片子的像素与 token 读数一个字节都不变。 */
export const FILM_STYLE_DEFAULT: FilmStyle = { bleed: false, bg: '' };

export type SubtitleMode = (typeof SUBTITLE_MODES)[number];
