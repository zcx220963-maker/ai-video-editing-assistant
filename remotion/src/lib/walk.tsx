/**
 * `<WalkingFigure/>` —— **同一个主角**在整片里走过去的那个身影（2026-10-19）。
 *
 * 为什么单独一件：参考片的力量不在某一镜画得多细，而在**同一个人**从洞穴走到蒸汽
 * 时代、再走到火箭与手机时代——身高、步态、朝向、脚底那条线始终是同一个，只有背景
 * 与身上的衣服在换。二十几镜各自独立写码时，这件事靠提示词是守不住的：每个人都会
 * 画一个「大概这么高」的人，切点一过，身高、步幅与朝向就漂了。
 *
 * 这一件把「走路」写成一份**确定性的步态**：读 `useCurrentFrame()`，两条腿与两条臂
 * 做反相摆动，身体随步频上下起伏，衣摆跟着摆。所有镜头都用同一件、同一组参数，
 * 切点处的身姿就自然对得上。
 *
 * **2026-10-20 改版：从「圆头火柴人」改成参考片那一档的侧面人物。**
 * 用户拿《无足鸟》三条件对照，原话是「那种圆头看起来很假」「服饰着装可以更改，
 * 而不是写死的」。改的是三件事：
 *
 *   ① **侧面头**：额头、眉弓、鼻梁、鼻尖、人中、下颌、后脑各有转折，不是一颗圆；
 *      头发/帽子在同一个填色里长出轮廓（剪影档看不出材质，只看外轮廓）。
 *   ② **有粗细的四肢**：大腿→膝→小腿→踝、上臂→肘→腕都收分，脚有鞋/靴的形，
 *      不再是等宽折线。真实感来自这些收分与关节，不来自贴图。
 *   ③ **衣服是一组参数，不是几个写死的枚举**：`outfit` 里给版型（长袍/托加/外套/
 *      连衣裙/西装/连帽衫/甲胄/兽皮）、袖型、长度、下摆、领口、头饰、发型、持物。
 *      同一副骨架换这一层，就是「同一个人穿过各时代」。
 *
 * **坐标**：`x` 是脚底中点的横坐标，`footY` 是脚底那条线，`height` 是身高——三者都是
 * 成片设备像素（与 `@dg/lib/draw/*` 同一套）。缺省位置是画面正中偏下、身高约 46%，
 * 与参考片里那个大剪影一致：**人要够大**，缩到 15% 高的那个是图标，不是主角。
 *
 * **连续性靠 `phase` 与 `strideSec`**：一套步态周期是 `strideSec` 秒。每一镜都传同样的
 * `strideSec` 与 `phase`（默认 0），切点就落在同一个步态相位上——上一镜收在「左脚在前」，
 * 下一镜也从「左脚在前」起步。相机可以横移、背景可以换，人不会在切点处抖一下。
 *
 * ```tsx
 * import { WalkingFigure } from '@dg/lib/walk';
 * <WalkingFigure x={W * 0.5} footY={H * 0.86} height={H * 0.48}
 *                outfit={{ garment: 'robe', sleeve: 'wide', length: 'ankle',
 *                          headwear: 'futou', hair: 'bun', prop: 'scroll' }}
 *                facing="right" strideSec={1.0} rim="#ffb15c" />
 * ```
 *
 * 旧参数 `wardrobe="coat"` 仍然能用（内部映射成同一套 `outfit`），老镜头代码不用改。
 *
 * **它不是唯一画法，也不是一张脸**：缺省 `render="silhouette"` 画剪影；要上色、
 * 要体积就传 `flat` / `lit` / `line`（同一副几何，只换颜色与明度）。非人的贯穿主体
 * （鹿、车、器物、一块发光的矩形）**不必套这一件**——在镜头代码里自由画，
 * 守住同一轮廓、比例与朝向即可。
 * 颜色缺省取整片那一档的墨色；`rim` 是那束跟着主体走的光（火把橙/车窗暖黄/屏幕冷蓝），
 * 由镜头代码按时代给，不要所有时代都用同一个强调色。
 *
 * 纪律与 `@dg/lib/brush`、`@dg/lib/scene` 同源：**不许 `Math.random`**，所有摆动都是
 * 帧的确定性函数；同一份代码渲两次逐像素相同。
 */
import React from 'react';
import { getInputProps, useCurrentFrame, useVideoConfig } from 'remotion';
import { resolveFilmStyle } from '@dg/film';
import { TOKENS } from '@dg/tokens';

/** 主角身上那件衣服的旧名字（按时代换，轮廓不变）。新代码用 `outfit`。 */
export type WalkWardrobe = 'plain' | 'cloak' | 'cap' | 'coat' | 'hoodie';

/** 版型。剪影档里版型靠**下摆长度、外扩、袖型、头饰**读出来，不靠花纹。 */
export type WalkGarment =
  | 'none'   // 不穿外衣，只有身体
  | 'tunic'  // 直筒短袍
  | 'robe'   // 长袍（中式/神职/学者）
  | 'toga'   // 托加：一肩裸露，斜披
  | 'coat'   // 长外套：收腰、下摆外扩、翻领
  | 'dress'  // 连衣裙：上身贴身、裙摆 A 形
  | 'suit'   // 西装：上衣到胯，裤子与上衣同色
  | 'hoodie' // 连帽衫（默认手里有手机）
  | 'armor'  // 甲胄：肩甲 + 甲裙
  | 'pelt';  // 兽皮/斗篷：下摆参差

export type WalkSleeve = 'none' | 'fitted' | 'wide' | 'bell' | 'puffed';
export type WalkLength = 'hip' | 'knee' | 'calf' | 'ankle' | 'floor';
export type WalkCollar = 'none' | 'v' | 'lapel' | 'high' | 'shawl';
export type WalkHeadwear =
  | 'none' | 'cap' | 'futou' | 'tricorn' | 'top' | 'brim'
  | 'helmet' | 'hood' | 'straw' | 'crown';
export type WalkHair = 'short' | 'bun' | 'ponytail' | 'long' | 'bald';
export type WalkProp =
  | 'none' | 'cup' | 'book' | 'staff' | 'cane' | 'umbrella'
  | 'lantern' | 'torch' | 'phone' | 'bag' | 'scroll' | 'tool';
/** 骨架：同一部片里只选一档，逐镜不要换。 */
export type WalkBuild = 'male' | 'female' | 'neutral';

/**
 * 衣服与随身物。**这是内容，不是画法**：用户说「明代的读书人，青衫、乌纱帽、
 * 手里一卷书」，写手就翻成 `{garment:'robe', length:'ankle', sleeve:'wide',
 * headwear:'futou', hair:'bun', prop:'scroll'}`；说「18 世纪的欧洲学者」就换成
 * `{garment:'coat', length:'calf', collar:'lapel', headwear:'tricorn', prop:'book'}`。
 *
 * 缺省值按「素色短袍、无帽、短发」给；只写关心的几项，其余落缺省。
 */
export interface WalkOutfit {
  build?: WalkBuild;
  garment?: WalkGarment;
  sleeve?: WalkSleeve;
  length?: WalkLength;
  /** 下摆外扩 0~1（0 直筒，1 大摆）。缺省按版型给。 */
  flare?: number;
  belt?: boolean;
  collar?: WalkCollar;
  headwear?: WalkHeadwear;
  hair?: WalkHair;
  prop?: WalkProp;
  /** 持物在哪只手里。缺省 front（近端、看得见的那只）。 */
  propHand?: 'front' | 'back';
}

/**
 * 画法方向（与 `code_spec.PROTAGONIST_RENDERS` 逐项一致）：
 *
 * - `silhouette`：单色实心剪影（缺省，参考片那一档）；
 * - `flat`：平涂上色，主体有固有色，明暗只分两档；
 * - `lit`：受光面 / 背光面两档以上，画出体积与材质——「不是阴影，是真实的样子」
 *   这一档是代码能做到的那一端；
 * - `line`：只画轮廓与结构线。
 *
 * 它只决定**这一件**怎么画；非人的主体（鹿、车、器物）不必用它，写手在镜头代码里
 * 自由画即可。要跨镜一致的是轮廓、比例、朝向与主色，不是「必须调这个组件」。
 */
export type WalkRender = 'silhouette' | 'flat' | 'lit' | 'line';

export interface WalkingFigureProps {
  /** 脚底中点的 x（成片像素）。缺省画面正中 */
  x?: number;
  /** 脚底那条线的 y（成片像素）。缺省画面高 88% */
  footY?: number;
  /** 身高（成片像素）。缺省画面高 46%——主角要够大 */
  height?: number;
  /** 面朝：1 / 'right'（缺省）或 -1 / 'left' */
  facing?: 1 | -1 | 'left' | 'right';
  /**
   * 第 0 帧的步态相位（0~1，一个完整周期是 1）。每一镜传同一个值＝切点对得上。
   * 缺省 0。
   */
  phase?: number;
  /** 一个完整步态周期几秒。每一镜传同一个值＝步频一致。缺省 1.0 */
  strideSec?: number;
  /** 是否在走。停下看一件事、等一个结果时传 false（腿并拢、手臂自然下垂） */
  walking?: boolean;
  /**
   * 衣服与随身物（新写法，见 `WalkOutfit`）。整片只换这一层，骨架与步态不动。
   */
  outfit?: WalkOutfit;
  /**
   * 旧写法：`plain` / `cloak` / `cap` / `coat` / `hoodie`。内部映射到 `outfit`；
   * 新代码请直接用 `outfit`。也可以直接传一个 `WalkOutfit` 对象。
   */
  wardrobe?: WalkWardrobe | WalkOutfit;
  /** 画法方向。缺省 silhouette（剪影）；要上色/体积就传 flat/lit/line */
  render?: WalkRender;
  /** 主体固有色（剪影色 / 衣服主色）。缺省整片那档的墨色 */
  fill?: string;
  /** 露出皮肤的部分（头、手）。只对 flat/lit 生效；缺省给一档暖肤色的代码色，
   *  要跟这一镜的色板走就显式传。silhouette/line 不画皮肤 */
  skin?: string;
  /** 头发/帽子的颜色。只对 flat/lit 生效。缺省比 `fill` 深一档 */
  hair?: string;
  /** 勾边光（跟着人走的那束光的颜色）。缺省整片强调色 */
  rim?: string;
  /** 光从人的前面还是后面来：front（缺省）＝朝行进方向打，back＝逆光 */
  rimSide?: 'front' | 'back';
  /** 落地影。缺省画 */
  shadow?: boolean;
  /** 容器宽高。缺省整帧；放进 `<CaptionFrame>` 的画面区时传 `art.w`/`art.h` */
  width?: number;
  heightPx?: number;
}

type Pt = [number, number];

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** `#rrggbb` → [r,g,b]；认不出返回 null（`fill` 也可能是命名色）。 */
const rgbOf = (c: string): [number, number, number] | null => {
  const m = /^#([0-9a-f]{6})$/i.exec(String(c || '').trim());
  if (!m) return null;
  const v = parseInt(m[1], 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
};

/**
 * 把颜色朝黑或白混一档，返回**实色**。
 *
 * 为什么不用 `opacity`：半透明图层交给 Chromium 合成时，两次渲染的混合结果可能
 * 差一两个字节——同码两渲就不再逐像素相同（2026-10-19 真渲探针抓到的正是这一条）。
 * 披风与身体之间那点明度差必须是**画出来的颜色**，不是叠出来的透明度。
 */
const shade = (c: string, amount: number): string => {
  const rgb = rgbOf(c);
  if (!rgb) return c;
  const luma = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 255000;
  const target = luma < 0.5 ? 255 : 0;
  const mix = (v: number) => Math.round(v + (target - v) * amount)
    .toString(16).padStart(2, '0');
  return `#${mix(rgb[0])}${mix(rgb[1])}${mix(rgb[2])}`;
};

/** 闭合平滑路径（二次贝塞尔穿各点中点）——衣摆用它，避免折线感。 */
const smoothLoop = (points: Pt[]): string => {
  const n = points.length;
  if (n < 3) return '';
  let d = `M${points[0][0].toFixed(2)} ${points[0][1].toFixed(2)}`;
  for (let i = 1; i <= n; i += 1) {
    const p1 = points[i % n];
    const p2 = points[(i + 1) % n];
    const mx = (p1[0] + p2[0]) / 2;
    const my = (p1[1] + p2[1]) / 2;
    d += ` Q${p1[0].toFixed(2)} ${p1[1].toFixed(2)} ${mx.toFixed(2)} ${my.toFixed(2)}`;
  }
  return `${d} Z`;
};

const poly = (points: Pt[]): string =>
  points
    .map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(2)} ${py.toFixed(2)}`)
    .join(' ') + ' Z';

/** 一段有收分的肢体：a→b，两端半径 ra/rb。用实心四边形，不是等宽描边。 */
const limbSeg = (a: Pt, b: Pt, ra: number, rb: number): string => {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len;
  const ny = dx / len;
  return poly([
    [a[0] + nx * ra, a[1] + ny * ra],
    [b[0] + nx * rb, b[1] + ny * rb],
    [b[0] - nx * rb, b[1] - ny * rb],
    [a[0] - nx * ra, a[1] - ny * ra],
  ]);
};

/** 旧 `wardrobe` 的名字 → 新 `outfit` 的缺省。老镜头代码不用改。 */
const LEGACY_WARDROBE: Record<WalkWardrobe, WalkOutfit> = {
  plain: { garment: 'none', sleeve: 'fitted', length: 'knee', flare: 0.3 },
  cloak: { garment: 'pelt', sleeve: 'fitted', length: 'knee', flare: 0.55 },
  cap: { garment: 'none', sleeve: 'fitted', headwear: 'cap' },
  coat: { garment: 'coat', sleeve: 'fitted', length: 'knee', flare: 0.3, collar: 'lapel' },
  hoodie: { garment: 'hoodie', sleeve: 'fitted', length: 'hip', headwear: 'hood',
            prop: 'phone' },
};

const DEFAULT_OUTFIT: Required<Omit<WalkOutfit, 'flare'>> & { flare: number } = {
  build: 'neutral',
  garment: 'none',
  sleeve: 'fitted',
  length: 'knee',
  flare: 0.3,
  belt: false,
  collar: 'none',
  headwear: 'none',
  hair: 'short',
  prop: 'none',
  propHand: 'front',
};

/**
 * 归一化。**认不出的值落到缺省，不抛错**——写手偶尔会拼错一个词，画面难看一点
 * 也得把片渲出来（与「堵死比白烧更坏」同一条）。
 */
const normalizeOutfit = (
  base: WalkWardrobe | WalkOutfit | undefined,
  extra: WalkOutfit | undefined,
): typeof DEFAULT_OUTFIT => {
  const legacy = typeof base === 'string' ? LEGACY_WARDROBE[base] ?? {} : base ?? {};
  const merged: WalkOutfit = { ...legacy, ...(extra ?? {}) };
  const pick = <T extends string>(v: unknown, allowed: readonly T[], fallback: T): T =>
    typeof v === 'string' && (allowed as readonly string[]).includes(v)
      ? (v as T)
      : fallback;
  const flare = Number(merged.flare);
  return {
    build: pick(merged.build, ['male', 'female', 'neutral'] as const, DEFAULT_OUTFIT.build),
    garment: pick(merged.garment,
      ['none', 'tunic', 'robe', 'toga', 'coat', 'dress', 'suit', 'hoodie', 'armor',
       'pelt'] as const, DEFAULT_OUTFIT.garment),
    sleeve: pick(merged.sleeve,
      ['none', 'fitted', 'wide', 'bell', 'puffed'] as const, DEFAULT_OUTFIT.sleeve),
    length: pick(merged.length,
      ['hip', 'knee', 'calf', 'ankle', 'floor'] as const, DEFAULT_OUTFIT.length),
    flare: Number.isFinite(flare) ? clamp(flare, 0, 1) : DEFAULT_OUTFIT.flare,
    belt: Boolean(merged.belt),
    collar: pick(merged.collar,
      ['none', 'v', 'lapel', 'high', 'shawl'] as const, DEFAULT_OUTFIT.collar),
    headwear: pick(merged.headwear,
      ['none', 'cap', 'futou', 'tricorn', 'top', 'brim', 'helmet', 'hood', 'straw',
       'crown'] as const, DEFAULT_OUTFIT.headwear),
    hair: pick(merged.hair,
      ['short', 'bun', 'ponytail', 'long', 'bald'] as const, DEFAULT_OUTFIT.hair),
    prop: pick(merged.prop,
      ['none', 'cup', 'book', 'staff', 'cane', 'umbrella', 'lantern', 'torch',
       'phone', 'bag', 'scroll', 'tool'] as const, DEFAULT_OUTFIT.prop),
    propHand: pick(merged.propHand, ['front', 'back'] as const, DEFAULT_OUTFIT.propHand),
  };
};

/** 下摆高度（v，占身高）。 */
const LENGTH_V: Record<WalkLength, number> = {
  hip: 0.55, knee: 0.33, calf: 0.14, ankle: 0.045, floor: 0.012,
};

export const WalkingFigure: React.FC<WalkingFigureProps> = (props) => {
  const video = useVideoConfig();
  const frame = useCurrentFrame();
  const W = props.width ?? video.width;
  const Hpx = props.heightPx ?? video.height;

  const {
    x = W * 0.5,
    footY = Hpx * 0.88,
    height = Hpx * 0.46,
    facing = 'right',
    phase = 0,
    strideSec = 1.0,
    walking = true,
    render = 'silhouette',
    rimSide = 'front',
    shadow = true,
  } = props;

  const outfit = normalizeOutfit(props.wardrobe, props.outfit);
  const isWideSleeve = outfit.sleeve === 'wide' || outfit.sleeve === 'bell';

  const film = resolveFilmStyle(getInputProps());
  const colored = render === 'flat' || render === 'lit';
  const ink = props.fill || film.ink || TOKENS.ink;
  // 上色/受光两档不能拿「墨色」当固有色：墨色是给剪影档准备的，做成实心主体就是
  // 一团黑，明暗两档根本没地方落。没显式给 `fill` 时把它往白里提一档（仍是同一
  // 色相、仍从整片墨色派生），体积才画得出来。
  const fill = props.fill || (colored ? shade(ink, 0.55) : ink);
  const rim = props.rim || film.accent || TOKENS.accent;
  // 剪影档里衣服与身体**同色**（参考片就是一整块黑，明度差会读成两层塑料片）；
  // 只有上色档才给衣料一档不同明度。
  const garment = shade(fill, 0.18);
  const skin = props.skin || '#c08a5e';
  const hair = props.hair || shade(fill, -0.08);

  const dir: 1 | -1 = facing === -1 || facing === 'left' ? -1 : 1;
  const H = clamp(height, 40, Hpx * 1.6);
  const cycleSec = clamp(strideSec, 0.4, 3);
  const fps = video.fps || 30;
  // 同一相位在每一镜的第 0 帧出现：`phase` 是切点对得上的那一格。
  const theta = 2 * Math.PI * (frame / (fps * cycleSec) + phase);
  const gait = walking ? 1 : 0;
  // 身体在两次「双腿交叉」之间最高：低频一个周期两次。
  const bob = walking ? H * 0.012 * (0.5 - 0.5 * Math.cos(2 * theta)) : 0;
  // 衣摆/披风的摆动相位故意比脚步慢一点，免得像刚性板。
  const hemSway = gait * H * 0.018 * Math.sin(theta + 0.8);

  /** 局部坐标（u＝朝前为正当，v＝离地高度）→ 成片像素。 */
  const pt = (u: number, v: number): Pt => [x + dir * u * H, footY - v * H + bob];

  // ------------------------------------------------------------------ 骨架
  // 比例按成年人 1/7.5 头身：脚 0、膝 0.28、胯 0.52、肩 0.79、下巴 0.86、头顶 0.985。
  // 旧版头直径 0.116H（1/8.6），加上偏窄的肩，读出来是「大头娃娃」（2026-10-20
  // 用户拿参考片一比就说「头这么大根本不写实」）。这一版头高 0.125H，肩宽按骨架走。
  const HIP_V = 0.52;
  const SHOULDER_V = 0.775;
  const build = outfit.build;
  const shoulderW = build === 'female' ? 0.93 : build === 'male' ? 1.05 : 1;
  const hipW = build === 'female' ? 1.08 : build === 'male' ? 0.96 : 1;
  const waistW = build === 'female' ? 0.9 : 1;

  const legOf = (offset: number): { knee: Pt; ankle: Pt; toe: Pt;
                                     kneeU: number; ankleU: number; ankleV: number } => {
    const t = theta + offset;
    const hipAng = 0.42 * gait * Math.sin(t);
    const swing = Math.max(0, Math.sin(t));
    const kneeBend = gait * (0.05 + 0.88 * swing * swing);
    const shin = hipAng - kneeBend;
    const kneeU = Math.sin(hipAng) * 0.255;
    const kneeV = HIP_V - Math.cos(hipAng) * 0.255;
    const ankleU = kneeU + Math.sin(shin) * 0.24;
    const ankleV = kneeV - Math.cos(shin) * 0.24;
    return {
      knee: pt(kneeU, kneeV),
      ankle: pt(ankleU, ankleV),
      toe: pt(ankleU + 0.085, Math.max(0.012, ankleV * 0.35)),
      kneeU, ankleU, ankleV,
    };
  };

  const armOf = (offset: number, holding = false): {
    shoulder: Pt; elbow: Pt; hand: Pt; elbowU: number; elbowV: number;
    handU: number; handV: number;
  } => {
    const t = theta + offset;
    // 持物的那条手臂**不跟着步频甩到身后**：参考片里杯/书/卷一直在身前。
    // 甩到身后时持物会整块并进躯干，剪影里什么都看不见。
    const upperAng = holding
      ? 0.10 + 0.05 * Math.sin(t)
      : -0.46 * gait * Math.sin(t) + 0.05;
    const bend = holding
      ? 1.15
      : 0.13 + gait * 0.30 * Math.max(0, Math.sin(t + Math.PI / 2));
    const elbowU = 0.015 + Math.sin(upperAng) * 0.17;
    const elbowV = SHOULDER_V - Math.cos(upperAng) * 0.17;
    const foreAng = upperAng + bend;
    const handU = elbowU + Math.sin(foreAng) * 0.155;
    const handV = elbowV - Math.cos(foreAng) * 0.155;
    return {
      shoulder: pt(0.015, SHOULDER_V),
      elbow: pt(elbowU, elbowV),
      hand: pt(handU, handV),
      elbowU, elbowV, handU, handV,
    };
  };

  // 远端与近端反相：左腿在前时左臂在后。
  const legBack = legOf(Math.PI);
  const legFront = legOf(0);
  const holdFront = outfit.prop !== 'none' && outfit.propHand === 'front';
  const holdBack = outfit.prop !== 'none' && outfit.propHand === 'back';
  const armBack = armOf(Math.PI, holdBack);
  const armFront = armOf(0, holdFront);

  // ------------------------------------------------------------------ 躯干
  // 肩有斜度、腰收、胯略宽——剪影档不画肌肉，但轮廓要读得出「一个人」。
  const torso = smoothLoop([
    pt(-0.090 * shoulderW, 0.766),
    pt(-0.028, 0.804),
    pt(0.030, 0.800),
    pt(0.100 * shoulderW, 0.758),
    pt(0.098 * shoulderW, 0.70),
    pt(0.078 * waistW, 0.60),
    pt(0.090 * hipW, 0.515),
    pt(-0.086 * hipW, 0.515),
    pt(-0.072 * waistW, 0.60),
    pt(-0.093 * shoulderW, 0.70),
  ]);

  // 侧面头：额头 → 眉弓 → 鼻梁 → 鼻尖 → 人中 → 下颌 → 颈。剪影档没有五官，
  // 但这一圈转折就是「真人侧脸」与「圆头」的全部差别。
  const headProfile: Pt[] = [
    pt(-0.042, 0.956),
    pt(-0.020, 0.978),
    pt(0.012, 0.984),
    pt(0.034, 0.972),
    pt(0.044, 0.956),
    pt(0.042, 0.941),
    pt(0.062, 0.923),   // 鼻尖
    pt(0.049, 0.913),   // 鼻底
    pt(0.054, 0.905),   // 上唇
    pt(0.049, 0.897),   // 下唇
    pt(0.057, 0.882),   // 下巴
    pt(0.044, 0.868),   // 下颌角
    pt(0.022, 0.859),
    pt(0.020, 0.842),   // 颈前
    pt(-0.012, 0.842),  // 颈后
    pt(-0.042, 0.878),
    pt(-0.050, 0.916),
  ];
  const head = poly(headProfile);
  // 头发在同一个填色里长出来：剪影档只看轮廓，上色档才分颜色。
  const hairShort = smoothLoop([
    pt(-0.040, 0.948), pt(-0.014, 0.986), pt(0.020, 0.992), pt(0.042, 0.972),
    pt(0.032, 0.958), pt(0.008, 0.970), pt(-0.020, 0.966),
  ]);
  const hairBun = smoothLoop([
    pt(-0.070, 0.930), pt(-0.078, 0.966), pt(-0.052, 0.986), pt(-0.030, 0.972),
    pt(-0.032, 0.942),
  ]);
  const hairTail = smoothLoop([
    pt(-0.048, 0.944), pt(-0.070, 0.910), pt(-0.086, 0.860), pt(-0.072, 0.845),
    pt(-0.056, 0.880), pt(-0.038, 0.918),
  ]);
  const hairLong = smoothLoop([
    pt(-0.046, 0.950), pt(-0.072, 0.900), pt(-0.080, 0.830), pt(-0.070, 0.780),
    pt(-0.048, 0.800), pt(-0.050, 0.866),
  ]);

  // ------------------------------------------------------------------ 衣服
  const lenV = LENGTH_V[outfit.length];
  const hemW = 0.095 + outfit.flare * 0.075;
  const swayU = hemSway / H;
  const swayU2 = (hemSway / H) * 0.55;

  /**
   * 肩线：从后肩尖 → 后颈 → 前颈 → 前肩尖。肩尖比颈根低 3~4% 身高，
   * 这一段斜度就是「人的肩」与「衣架」的差别（第一版渲出来是平肩大块头，
   * 用户一眼看出假）。
   */
  const shoulders = (frontV: number, backV: number, neckV = 0.804): Pt[] => [
    pt(-0.096 * shoulderW, backV),
    pt(-0.028, neckV),
    pt(0.030, neckV - 0.004),
    pt(0.110 * shoulderW, frontV),
    pt(0.102 * shoulderW, 0.716),
  ];

  /** 长袍/短袍/外套共用的一件布。下摆是一条连续的边，不再挖中缝—— */
  /** 中缝在剪影里会读成一条尾巴（第一版真渲抓到的）。 */
  const robeLike = (frontV: number, backV: number, extraHem = 0): Pt[] => [
    ...shoulders(frontV, backV),
    pt(0.098 * shoulderW, 0.70),
    pt(0.082 * waistW, 0.61),
    pt(hemW + extraHem + swayU, lenV),
    pt(-hemW - extraHem + swayU, lenV),
    pt(-0.078 * waistW, 0.61),
    pt(-0.096 * shoulderW, 0.70),
  ];

  let garmentBody: Pt[] | null = null;
  switch (outfit.garment) {
    case 'robe':
      garmentBody = robeLike(0.760, 0.768);
      break;
    case 'tunic':
      garmentBody = robeLike(0.762, 0.770);
      break;
    case 'coat':
      garmentBody = robeLike(0.758, 0.766);
      break;
    case 'dress':
      garmentBody = [
        pt(-0.088 * shoulderW, 0.768), pt(-0.026, 0.804),
        pt(0.028, 0.800), pt(0.098 * shoulderW, 0.756),
        pt(0.076 * waistW, 0.62), pt(hemW + 0.015 + swayU, lenV),
        pt(-hemW - 0.015 + swayU, lenV),
        pt(-0.070 * waistW, 0.62),
      ];
      break;
    case 'suit':
      garmentBody = [
        ...shoulders(0.758, 0.766),
        pt(0.100 * shoulderW, 0.70), pt(0.086 * waistW, 0.60),
        pt(0.070 * hipW + swayU, 0.50), pt(-0.066 * hipW + swayU, 0.50),
        pt(-0.074 * waistW, 0.60), pt(-0.094 * shoulderW, 0.70),
      ];
      break;
    case 'hoodie':
      garmentBody = [
        ...shoulders(0.778, 0.786, 0.812),
        pt(0.102 * shoulderW, 0.70), pt(0.092 * hipW, 0.555),
        pt(0.030 + swayU2, 0.548), pt(-0.028 + swayU2, 0.548),
        pt(-0.090 * hipW, 0.555), pt(-0.098 * shoulderW, 0.70),
      ];
      break;
    case 'toga':
      // 一肩裸露：前胸的布从后肩斜披到前腋下，近端的肩膀与手臂露在布外。
      garmentBody = [
        pt(-0.100 * shoulderW, 0.770), pt(-0.028, 0.804),
        pt(0.010 * shoulderW, 0.760),
        pt(0.074 * shoulderW, 0.712), pt(0.084 * waistW, 0.61),
        pt(hemW + swayU, lenV), pt(-hemW + swayU, lenV),
        pt(-0.078 * waistW, 0.61), pt(-0.096 * shoulderW, 0.70),
      ];
      break;
    case 'armor':
      garmentBody = [
        ...shoulders(0.756, 0.764),
        pt(0.104 * shoulderW, 0.68), pt(0.090 * waistW, 0.56),
        pt(0.086 * hipW, 0.50), pt(-0.082 * hipW, 0.50),
        pt(-0.080 * waistW, 0.56), pt(-0.100 * shoulderW, 0.68),
      ];
      break;
    case 'pelt': {
      // 参差下摆：每一片毛尖都按固定的相位错开，不用随机数。
      const jag: Pt[] = [];
      const n = 7;
      for (let i = 0; i <= n; i += 1) {
        const u = hemW - (2 * hemW) * (i / n) + swayU;
        const drop = (i % 2 === 0 ? 0.028 : 0.002) + 0.008 * Math.sin(i * 2.1);
        jag.push(pt(u, lenV - drop));
      }
      garmentBody = [
        ...shoulders(0.762, 0.770),
        pt(0.100 * shoulderW, 0.70), pt(0.082 * waistW, 0.61),
        ...jag,
        pt(-0.078 * waistW, 0.61), pt(-0.098 * shoulderW, 0.70),
      ];
      break;
    }
    default:
      garmentBody = null;
  }

  // 甲胄的肩甲与甲裙：剪影档靠这两块读出「甲」，不靠花纹。
  // 肩甲是**有折角的板**，不是两个圆：第一版用 circle 渲出来像米老鼠的耳朵。
  const pauldronPaths = outfit.garment === 'armor'
    ? [
      poly([pt(-0.086 * shoulderW, 0.812), pt(-0.124 * shoulderW, 0.800),
            pt(-0.132 * shoulderW, 0.768), pt(-0.106 * shoulderW, 0.736),
            pt(-0.082 * shoulderW, 0.748), pt(-0.074 * shoulderW, 0.784)]),
      poly([pt(0.098 * shoulderW, 0.806), pt(0.136 * shoulderW, 0.790),
            pt(0.140 * shoulderW, 0.756), pt(0.112 * shoulderW, 0.730),
            pt(0.090 * shoulderW, 0.746), pt(0.084 * shoulderW, 0.780)]),
    ]
    : [];
  const armorSkirt = outfit.garment === 'armor'
    ? smoothLoop([pt(0.088 * hipW, 0.53), pt(0.098 * hipW, 0.40),
                  pt(0.060 * hipW, 0.36), pt(-0.056 * hipW, 0.36),
                  pt(-0.092 * hipW, 0.40), pt(-0.084 * hipW, 0.53)])
    : '';

  // ------------------------------------------------------------------ 头饰
  const headwearShape = (): React.ReactNode => {
    switch (outfit.headwear) {
      case 'cap':
        return (
          <>
            <path d={smoothLoop([pt(-0.046, 0.958), pt(-0.020, 0.990),
                                 pt(0.026, 0.994), pt(0.044, 0.968),
                                 pt(0.010, 0.972), pt(-0.024, 0.966)])} />
            <path d={poly([pt(0.026, 0.972), pt(0.150, 0.962),
                           pt(0.156, 0.944), pt(0.030, 0.948)])} />
          </>
        );
      case 'futou':
        // 乌纱帽：高帽身 + 两侧展角。剪影里这两片角是它唯一的识别特征。
        return (
          <>
            <path d={poly([pt(-0.032, 0.972), pt(0.040, 0.976),
                           pt(0.042, 1.026), pt(0.030, 1.034),
                           pt(-0.018, 1.034), pt(-0.034, 1.024)])} />
            {/* 展角是短的：第一版两片画出 0.12H，真渲像直升机的螺旋桨。 */}
            <path d={poly([pt(-0.030, 0.990), pt(-0.088, 0.998),
                           pt(-0.090, 1.006), pt(-0.028, 1.000)])} />
            <path d={poly([pt(-0.030, 0.990), pt(0.062, 0.998),
                           pt(0.064, 1.006), pt(-0.028, 1.000)])} />
          </>
        );
      case 'tricorn':
        return (
          <>
            <path d={poly([pt(-0.070, 0.958), pt(0.128, 0.966),
                           pt(0.110, 0.984), pt(0.030, 0.996),
                           pt(-0.052, 0.982)])} />
            <path d={smoothLoop([pt(-0.030, 0.972), pt(-0.006, 0.998),
                                 pt(0.034, 0.996), pt(0.044, 0.974)])} />
          </>
        );
      case 'top':
        return (
          <>
            <path d={poly([pt(-0.062, 0.962), pt(0.118, 0.960),
                           pt(0.116, 0.976), pt(-0.060, 0.978)])} />
            <path d={poly([pt(-0.030, 0.970), pt(0.058, 0.970),
                           pt(0.058, 1.060), pt(-0.030, 1.060)])} />
          </>
        );
      case 'brim':
        return (
          <>
            <path d={smoothLoop([pt(-0.092, 0.958), pt(-0.052, 0.968),
                                 pt(0.036, 0.970), pt(0.108, 0.958),
                                 pt(0.042, 0.946), pt(-0.052, 0.946)])} />
            <path d={smoothLoop([pt(-0.036, 0.966), pt(0.000, 0.996),
                                 pt(0.042, 0.982), pt(0.034, 0.964)])} />
          </>
        );
      case 'helmet':
        return (
          <>
            {/* 盔：比头大一圈的圆顶 + 后颈护片 + 一道脊。剪影里只看外轮廓。 */}
            <path d={smoothLoop([pt(-0.062, 0.938), pt(-0.040, 0.996),
                                 pt(0.010, 1.010), pt(0.052, 0.982),
                                 pt(0.056, 0.942), pt(0.020, 0.930),
                                 pt(-0.030, 0.930)])} />
            <path d={poly([pt(-0.010, 1.000), pt(0.002, 1.052),
                           pt(0.016, 1.000)])} />
            <path d={poly([pt(-0.062, 0.942), pt(-0.078, 0.900),
                           pt(-0.052, 0.906), pt(-0.040, 0.936)])} />
          </>
        );
      case 'hood':
        // 帽子戴在**脑后**：第一版从头顶一直包到额头，剪影档里整个人变成一颗球，
        // 侧脸全没了。这一版只留后脑一圈，脸照旧露出来。
        return (
          <path d={smoothLoop([pt(-0.074, 0.838), pt(-0.082, 0.900),
                               pt(-0.058, 0.948), pt(-0.018, 0.958),
                               pt(0.012, 0.930), pt(0.006, 0.884),
                               pt(-0.020, 0.856)])} />
        );
      case 'straw':
        return (
          <>
            <path d={poly([pt(-0.030, 1.000), pt(0.060, 0.960),
                           pt(-0.115, 0.950)])} />
            <path d={poly([pt(-0.115, 0.950), pt(0.120, 0.963),
                           pt(0.060, 0.956), pt(-0.030, 0.958)])} />
          </>
        );
      case 'crown':
        return (
          <path d={poly([pt(-0.046, 0.972), pt(-0.040, 1.020),
                         pt(-0.016, 0.992), pt(0.006, 1.028),
                         pt(0.026, 0.994), pt(0.046, 1.016),
                         pt(0.050, 0.972)])} />
        );
      default:
        return null;
    }
  };

  // ------------------------------------------------------------------ 持物
  const propOf = (handU: number, handV: number): React.ReactNode => {
    // 广袖把手腕整个罩住：持物还画在腕心就整块并进袖子，剪影里什么都不剩。
    // 往行进方向挪出袖口，杯/书/卷才真的在轮廓外面（参考片里也是这么读的）。
    const gap = isWideSleeve ? 0.088 : 0.012;
    const at = pt(handU + gap, handV);
    const scale = H;
    switch (outfit.prop) {
      case 'cup':
        return (
          <>
            <path d={poly([
              pt(handU - 0.012, handV + 0.010), pt(handU + 0.028, handV + 0.006),
              pt(handU + 0.020, handV - 0.030), pt(handU - 0.004, handV - 0.026),
            ])} />
            <line x1={at[0]} y1={at[1] + scale * 0.006}
                  x2={at[0]} y2={at[1] + scale * 0.032}
                  strokeWidth={H * 0.008} />
            <line x1={at[0] - scale * 0.012} y1={at[1] + scale * 0.034}
                  x2={at[0] + scale * 0.014} y2={at[1] + scale * 0.034}
                  strokeWidth={H * 0.008} />
          </>
        );
      case 'book':
        return <rect x={at[0] - H * 0.030} y={at[1] - H * 0.020}
                     width={H * 0.078} height={H * 0.052} rx={H * 0.004}
                     transform={`rotate(-8 ${at[0]} ${at[1]})`} />;
      case 'staff':
        return (
          <>
            <line x1={at[0] - dir * H * 0.010} y1={at[1] + H * 0.012}
                  x2={at[0] + dir * H * 0.020} y2={footY - H * 0.002}
                  strokeWidth={H * 0.012} />
            <line x1={at[0] - dir * H * 0.010} y1={at[1] + H * 0.012}
                  x2={at[0] - dir * H * 0.020} y2={at[1] - H * 0.090}
                  strokeWidth={H * 0.014} />
          </>
        );
      case 'cane':
        return (
          <line x1={at[0]} y1={at[1] + H * 0.008}
                x2={at[0] + dir * H * 0.048} y2={footY - H * 0.004}
                strokeWidth={H * 0.010} />
        );
      case 'umbrella':
        return (
          <>
            <line x1={at[0]} y1={at[1]} x2={at[0]} y2={at[1] - H * 0.160}
                  strokeWidth={H * 0.010} />
            <path d={smoothLoop([
              [at[0] - H * 0.150, at[1] - H * 0.150],
              [at[0] - H * 0.100, at[1] - H * 0.210],
              [at[0], at[1] - H * 0.232],
              [at[0] + H * 0.100, at[1] - H * 0.210],
              [at[0] + H * 0.150, at[1] - H * 0.150],
            ])} />
          </>
        );
      case 'lantern':
        return (
          <>
            <line x1={at[0]} y1={at[1]} x2={at[0]} y2={at[1] + H * 0.050}
                  strokeWidth={H * 0.006} />
            <rect x={at[0] - H * 0.034} y={at[1] + H * 0.048}
                  width={H * 0.068} height={H * 0.088} rx={H * 0.020} />
          </>
        );
      case 'torch':
        return (
          <>
            <line x1={at[0]} y1={at[1] + H * 0.010} x2={at[0]}
                  y2={at[1] - H * 0.090} strokeWidth={H * 0.014} />
            <path d={smoothLoop([
              [at[0] - H * 0.030, at[1] - H * 0.090],
              [at[0] - H * 0.012, at[1] - H * 0.145],
              [at[0] + H * 0.014, at[1] - H * 0.118],
              [at[0] + H * 0.026, at[1] - H * 0.082],
            ])} />
          </>
        );
      case 'phone':
        // 手机要**贴着腕心**：底部压在手心上方一点点，握得住；悬在手上面
        // 0.044H 会读成「一块飘在胸前的黑牌」。屏幕只在上色档亮。
        return (
          <>
            <rect x={at[0] + dir * H * 0.006 - H * 0.026} y={at[1] - H * 0.082}
                  width={H * 0.052} height={H * 0.096} rx={H * 0.010} />
            {colored && (
              <rect x={at[0] + dir * H * 0.006 - H * 0.018} y={at[1] - H * 0.073}
                    width={H * 0.036} height={H * 0.078} rx={H * 0.005}
                    fill={shade(rim, 0.25)} stroke="none" />
            )}
          </>
        );
      case 'bag':
        return (
          <>
            <rect x={at[0] - H * 0.048} y={at[1] + H * 0.020}
                  width={H * 0.096} height={H * 0.100} rx={H * 0.012} />
            <path d={`M${at[0] - H * 0.026} ${at[1] + H * 0.024}
                       Q${at[0]} ${at[1] - H * 0.020}
                        ${at[0] + H * 0.026} ${at[1] + H * 0.024}`}
                  fill="none" strokeWidth={H * 0.008} />
          </>
        );
      case 'scroll':
        return (
          <>
            <rect x={at[0] - H * 0.060} y={at[1] - H * 0.018}
                  width={H * 0.120} height={H * 0.036} rx={H * 0.014}
                  transform={`rotate(-20 ${at[0]} ${at[1]})`} />
            <line x1={at[0] - H * 0.066} y1={at[1] + H * 0.013}
                  x2={at[0] + H * 0.066} y2={at[1] - H * 0.013}
                  strokeWidth={H * 0.006} />
          </>
        );
      case 'tool':
        return (
          <>
            <line x1={at[0]} y1={at[1] + H * 0.010} x2={at[0] + dir * H * 0.020}
                  y2={at[1] - H * 0.100} strokeWidth={H * 0.014} />
            <rect x={at[0] - H * 0.030} y={at[1] - H * 0.124}
                  width={H * 0.075} height={H * 0.032} rx={H * 0.004} />
          </>
        );
      default:
        return null;
    }
  };

  /**
   * 一副身形。四个画法共用这一副几何：剪影、平涂、受光、线稿之间切换的只是颜色，
   * **不是身材**——这正是「同一个人穿过各时代」要守住的东西。
   */
  const body = (bodyFill: string, bodyStroke: string, garmentColor: string,
                headColor: string) => {
    const sleeveR = {
      none: [0.026, 0.022, 0.018],
      fitted: [0.040, 0.032, 0.024],
      wide: [0.048, 0.040, 0.040],
      bell: [0.048, 0.040, 0.040],
      puffed: [0.062, 0.034, 0.024],
    }[outfit.sleeve];
    const legR = [0.056, 0.042, 0.027];
    const shoeBoot = outfit.length === 'calf' || outfit.length === 'ankle'
      || outfit.length === 'floor' || outfit.garment === 'armor';

    const limb = (points: Pt[], radii: number[]) => (
      <>
        {points.slice(0, -1).map((p, i) => (
          <path key={`s${i}`}
                d={limbSeg(p, points[i + 1], radii[i] * H, radii[i + 1] * H)} />
        ))}
        {points.map((p, i) => (
          <circle key={`j${i}`} cx={p[0]} cy={p[1]} r={radii[i] * H} />
        ))}
      </>
    );

    /**
     * 宽袖/广袖。第一版把腕围直接放大到 0.098H 再补一个圆，真渲出来是两颗
     * 大球（明代那镜整个人像只企鹅）。袖子是**从肘往下垂的一块布**：上口收、
     * 下口宽、腕下再垂一截，外轮廓才是钟形而不是圆形。
     */
    const bellSleeve = (elbow: Pt, hand: Pt) => {
      const big = outfit.sleeve === 'bell';
      const r1 = (big ? 0.050 : 0.046) * H;
      const r2 = (big ? 0.078 : 0.066) * H;
      const drop = (big ? 0.080 : 0.058) * H;
      const dx = hand[0] - elbow[0];
      const dy = hand[1] - elbow[1];
      const len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len;
      const ny = dx / len;
      return (
        <path d={poly([
          [elbow[0] + nx * r1, elbow[1] + ny * r1],
          [hand[0] + nx * r2, hand[1] + ny * r2],
          [hand[0], hand[1] + drop],
          [hand[0] - nx * r2, hand[1] - ny * r2],
          [elbow[0] - nx * r1, elbow[1] - ny * r1],
        ])} />
      );
    };

    const shoe = (ankleU: number, ankleV: number) => {
      const heelU = ankleU - 0.040;
      const toeU = ankleU + 0.090;
      const topV = shoeBoot ? Math.min(0.14, ankleV + 0.075) : ankleV + 0.030;
      return (
        <path d={poly([
          pt(heelU - 0.006, Math.max(0.004, ankleV + 0.020)),
          pt(heelU + 0.010, topV),
          pt(ankleU + 0.018, topV),
          pt(toeU - 0.020, 0.030),
          pt(toeU, 0.018),
          pt(toeU + 0.010, 0.006),
          pt(toeU - 0.006, 0.000),
          pt(heelU, 0.000),
        ])} fill={bodyFill} stroke={bodyStroke} strokeWidth={H * 0.006} />
      );
    };

    const armLayer = (arm: ReturnType<typeof armOf>, isFront: boolean) => (
      <>
        {isWideSleeve ? (
          <>
            {limb([arm.shoulder, arm.elbow], [0.040, 0.034])}
            {bellSleeve(arm.elbow, arm.hand)}
          </>
        ) : (
          <>
            {limb([arm.shoulder, arm.elbow, arm.hand], sleeveR)}
            {outfit.sleeve === 'puffed' && (
              <circle cx={arm.shoulder[0]} cy={arm.shoulder[1]} r={H * 0.060} />
            )}
            {colored && (
              <circle cx={arm.hand[0]} cy={arm.hand[1]} r={H * 0.019}
                      fill={skin} stroke="none" />
            )}
          </>
        )}
        {outfit.prop !== 'none'
          && (outfit.propHand === 'front') === isFront
          && propOf(arm.handU, arm.handV)}
      </>
    );

    return (
      <g fill={bodyFill} stroke={bodyStroke} strokeLinecap="round"
         strokeLinejoin="round">
        {/* 远端的手与腿先画，近端的压在上面——剪影因此有前后关系 */}
        {armLayer(armBack, false)}
        {limb([pt(0, HIP_V), legBack.knee, legBack.ankle], legR)}
        {shoe(legBack.ankleU, legBack.ankleV)}

        <path d={torso} strokeWidth={H * 0.012} />

        {garmentBody && (
          <path d={outfit.garment === 'pelt' ? poly(garmentBody)
                                             : smoothLoop(garmentBody)}
                fill={garmentColor} stroke={bodyStroke} strokeWidth={H * 0.008} />
        )}
        {outfit.garment === 'armor' && (
          <>
            {pauldronPaths.map((d, i) => (
              <path key={`pauldron${i}`} d={d} fill={garmentColor}
                    stroke={bodyStroke} strokeWidth={H * 0.008} />
            ))}
            <path d={armorSkirt} fill={garmentColor} stroke={bodyStroke}
                  strokeWidth={H * 0.008} />
          </>
        )}

        {outfit.garment === 'hoodie' && (
          <path d={smoothLoop([pt(-0.090, 0.822), pt(-0.070, 0.900),
                               pt(-0.020, 0.930), pt(0.036, 0.916),
                               pt(0.070, 0.870), pt(0.050, 0.826)])}
                fill={garmentColor} stroke={bodyStroke} strokeWidth={H * 0.008} />
        )}

        {/* 颈与头。头是侧面轮廓，不是圆；头发/帽子在同一个填色里补外轮廓。 */}
        <path d={limbSeg(pt(0.004, 0.818), pt(0.020, 0.860), H * 0.030, H * 0.025)} />
        {outfit.hair === 'long' && <path d={hairLong} />}
        {outfit.hair === 'ponytail' && <path d={hairTail} />}
        <path d={head} fill={headColor} strokeWidth={H * 0.008} />
        {outfit.hair !== 'bald' && outfit.hair !== 'long' && outfit.hair !== 'ponytail' && (
          <path d={hairShort} />
        )}
        {outfit.hair === 'bun' && <path d={hairBun} />}
        {colored && outfit.hair !== 'bald' && (
          <circle cx={pt(0.074, 0.914)[0]} cy={pt(0.074, 0.914)[1]}
                  r={H * 0.006} fill={hair} stroke="none" />
        )}
        {headwearShape()}

        {limb([pt(0, HIP_V), legFront.knee, legFront.ankle], legR)}
        {shoe(legFront.ankleU, legFront.ankleV)}

        {armLayer(armFront, true)}

        {/* 上色档才画的结构线：领口、腰带、衣褶。剪影档一律不画。 */}
        {colored && outfit.garment !== 'none' && (
          <>
            {(outfit.collar === 'v' || outfit.collar === 'lapel') && (
              <path d={poly([
                pt(-0.020, 0.800), pt(0.056 * shoulderW, 0.786),
                pt(0.024, 0.690), pt(-0.006, 0.700),
              ])} fill={shade(garmentColor, -0.18)} stroke="none" />
            )}
            {outfit.belt && (
              <rect x={pt(-0.090 * waistW, 0.615)[0]} y={pt(0, 0.615)[1]}
                    width={Math.abs(pt(0.090 * waistW, 0.615)[0]
                                    - pt(-0.090 * waistW, 0.615)[0])}
                    height={H * 0.026}
                    fill={shade(garmentColor, -0.22)} stroke="none" />
            )}
            <path d={`M${pt(-0.050, 0.560)[0]} ${pt(-0.050, 0.560)[1]}
                       Q${pt(-0.020, 0.470)[0]} ${pt(-0.020, 0.470)[1]}
                        ${pt(-0.040, lenV + 0.03)[0]} ${pt(-0.040, lenV + 0.03)[1]}`}
                  fill="none" stroke={shade(garmentColor, -0.20)}
                  strokeWidth={H * 0.006} />
          </>
        )}
      </g>
    );
  };

  // 勾边光是**一条发丝**，不是一圈描边：参考片里只在朝向光的那一侧露 1~2 px。
  // 第一版挪 1.2% 身高（1080p 下人高 0.48 时约 6 px），读成贴纸轮廓。
  const rimOffX = dir * H * 0.004 * (rimSide === 'front' ? 1 : -1);
  const rimOffY = -H * 0.003;

  // 受光两档：光从行进方向的前方来（rimSide=front）时，亮面在朝前那一侧。
  // id 用几何量拼出来而不是 useId/随机数：两次渲染同一副身形得到同一个 id，
  // 同一帧里出现两个不同位置的 `WalkingFigure` 也不会互相引用错渐变。
  const lightFromRight = (dir > 0) === (rimSide === 'front');
  const gx1 = lightFromRight ? 1 : 0;
  const gx2 = 1 - gx1;
  const gradId = `walk-lit-${Math.round(x)}-${Math.round(footY)}-${Math.round(H)}`;
  const coatGradId = `${gradId}-coat`;
  const bodyFill = render === 'lit' ? `url(#${gradId})` : fill;
  const garmentFill = render === 'lit' ? `url(#${coatGradId})` : garment;
  const bodyArgs = (f: string, s: string, g: string, head: string) =>
    [f, s, g, head] as const;
  const mainColors =
    render === 'line' ? bodyArgs('none', fill, 'none', 'none')
      : render === 'lit' ? bodyArgs(bodyFill, fill, garmentFill, skin)
        : render === 'flat' ? bodyArgs(fill, fill, garment, skin)
          : bodyArgs(fill, fill, fill, fill);
  const rimColors = bodyArgs(shade(rim, 0.18), shade(rim, 0.18), shade(rim, 0.28),
                             shade(rim, 0.18));

  return (
    <svg
      width={W}
      height={Hpx}
      viewBox={`0 0 ${W} ${Hpx}`}
      style={{ position: 'absolute', left: 0, top: 0, overflow: 'visible', pointerEvents: 'none' }}
      aria-hidden="true"
    >
      {render === 'lit' && (
        <defs>
          <linearGradient id={gradId} x1={gx1} y1="0" x2={gx2} y2="0.35">
            <stop offset="0" stopColor={shade(fill, 0.42)} />
            <stop offset="0.55" stopColor={fill} />
            <stop offset="1" stopColor={shade(fill, -0.22)} />
          </linearGradient>
          <linearGradient id={coatGradId} x1={gx1} y1="0" x2={gx2} y2="0.35">
            <stop offset="0" stopColor={shade(garment, 0.34)} />
            <stop offset="0.55" stopColor={garment} />
            <stop offset="1" stopColor={shade(garment, -0.16)} />
          </linearGradient>
        </defs>
      )}
      {shadow && (
        <ellipse cx={x} cy={footY} rx={H * (0.19 + outfit.flare * 0.05)} ry={H * 0.024}
                 fill="#000000" opacity={0.26} />
      )}
      {/* 勾边光：同一副身形整体朝光源方向挪一点，垫在剪影底下，只露一圈边。
          上色档也垫这一层——它让主体从背景里分离出来，而不只是一层黑边。 */}
      <g transform={`translate(${rimOffX.toFixed(2)} ${rimOffY.toFixed(2)})`}>
        {body(...rimColors)}
      </g>
      {body(...mainColors)}
    </svg>
  );
};

export default WalkingFigure;
