/**
 * `@dg/lib/style/human-shape` —— 人形的**侧面轮廓**（唯一一份）。
 *
 * 为什么单独一层：2.5D 的 `HumanSketch` 与三维的 `HumanFigure` 必须是同一个人。
 * 2026-10-22 用户第三次打回三维人形，原话是「要那种人影式3d……就是之前给你看的
 * 2d人影的3d版，顶多换服饰外形颜色就行了」——三维那一件要做的不是另一套比例，
 * 而是把这一份已经被认可的侧面轮廓**加一层厚度**。两份各写一套比例，迟早又会
 * 出现「三维不像二维」。
 *
 * 坐标：`Pt = [u, v]`，u 朝前为正、v 向上为正，单位都是身高比例（身高 = 1）。
 * 盆骨在 v = `B.hip`；头用头局部坐标（颈根在原点）。角度单位**度**，正＝向前。
 *
 * 这一层只有数表与纯函数：不读帧、不用 `Math.random`，可以原样被三维与 SVG 共用。
 */

export type Pt = [number, number];

export const D = Math.PI / 180;

export const rot = (p: Pt, deg: number): Pt => {
  const c = Math.cos(deg * D);
  const s = Math.sin(deg * D);
  return [p[0] * c - p[1] * s, p[0] * s + p[1] * c];
};

export const lerp2 = (a: Pt, b: Pt, t: number): Pt => [
  a[0] + (b[0] - a[0]) * t,
  a[1] + (b[1] - a[1]) * t,
];

export const norm = (a: Pt): Pt => {
  const l = Math.hypot(a[0], a[1]) || 1;
  return [a[0] / l, a[1] / l];
};

/* ------------------------------------------------------------------ */
/* 姿势：关节角度（度，正＝向前）                                      */
/* ------------------------------------------------------------------ */

export type PoseName =
  | 'stand' | 'sit' | 'sitGround' | 'crouch' | 'kneel'
  | 'walk' | 'point' | 'reach' | 'hold' | 'lean' | 'lie';

/** 一具骨架的全部自由度。三维那一件把它换算成体积，SVG 那一件把它换算成路径。 */
export type Joints = {
  /** 躯干前倾（相对竖直）。 */
  lean: number;
  /** 头相对躯干的角度。 */
  head: number;
  armNear: [number, number];
  armFar: [number, number];
  legNear: [number, number];
  legFar: [number, number];
  footNear: number;
  footFar: number;
  /** 盆骨相对站姿的升降（身高比例）。 */
  pelvis: number;
  /** 上下起伏（身高比例）。 */
  bob: number;
};

export const STAND: Joints = {
  lean: 2, head: 0,
  armNear: [1, 6], armFar: [-1, 6],
  legNear: [2, 4], legFar: [-2, 4],
  footNear: 0, footFar: 0,
  pelvis: 0, bob: 0,
};

export const POSES: Record<Exclude<PoseName, 'walk'>, Joints> = {
  stand: STAND,
  // 坐：胯落到椅面高度（0.285H ≈ 51cm/1.8m），大腿近水平、小腿垂直落地。
  sit: {
    lean: 6, head: 0,
    armNear: [30, 40], armFar: [26, 36],
    legNear: [82, 84], legFar: [80, 86],
    footNear: 0, footFar: 0,
    pelvis: -0.235, bob: 0,
  },
  // 坐地：腿伸向前、膝略屈，脚跟落地。
  sitGround: {
    lean: 10, head: -4,
    armNear: [42, 24], armFar: [38, 22],
    legNear: [84, 8], legFar: [82, 12],
    footNear: 8, footFar: 6,
    pelvis: -0.410, bob: 0,
  },
  // 蹲：膝折到底，脚跟略提。
  crouch: {
    lean: 26, head: -6,
    armNear: [36, 48], armFar: [32, 46],
    legNear: [84, 104], legFar: [80, 104],
    footNear: -8, footFar: -10,
    pelvis: -0.268, bob: 0,
  },
  // 跪：前腿大腿近水平、小腿垂直；后腿膝着地、脚背朝后。
  kneel: {
    lean: 4, head: 0,
    armNear: [10, 18], armFar: [6, 16],
    legNear: [81, 81], legFar: [4, 86],
    footNear: 0, footFar: -40,
    pelvis: -0.240, bob: 0,
  },
  point: {
    lean: 4, head: -4,
    armNear: [82, 4], armFar: [8, 12],
    legNear: [4, 3], legFar: [-4, 4],
    footNear: 0, footFar: 0,
    pelvis: 0, bob: 0,
  },
  reach: {
    lean: 8, head: -6,
    armNear: [100, 6], armFar: [10, 14],
    legNear: [6, 4], legFar: [-6, 5],
    footNear: 0, footFar: 0,
    pelvis: 0, bob: 0,
  },
  hold: {
    lean: 3, head: 2,
    armNear: [48, 86], armFar: [44, 84],
    legNear: [2, 4], legFar: [-2, 4],
    footNear: 0, footFar: 0,
    pelvis: 0, bob: 0,
  },
  lean: {
    lean: 17, head: -9,
    armNear: [-6, 14], armFar: [-10, 14],
    legNear: [9, 5], legFar: [-9, 7],
    footNear: 0, footFar: 0,
    pelvis: -0.012, bob: 0,
  },
  // 仰卧：躯干向后放平、腿向前伸直。盆骨落到身体厚度那一档（约 0.12H）。
  lie: {
    lean: -88, head: 6,
    armNear: [86, 6], armFar: [84, 8],
    legNear: [88, 4], legFar: [86, 6],
    footNear: 18, footFar: 16,
    pelvis: -0.400, bob: 0,
  },
};

/** 走路：与 `WalkingFigure` 同一组步态常数（那里对着参考片调过）。 */
export const gait = (theta: number): Joints => {
  const leg = (off: number): [number, number] => {
    const t = theta + off;
    const hip = (0.42 * Math.sin(t)) / D;
    const swing = Math.max(0, Math.sin(t));
    const knee = (0.05 + 0.88 * swing * swing) / D;
    return [hip, knee];
  };
  const arm = (off: number): [number, number] => {
    const t = theta + off;
    const up = (-0.46 * Math.sin(t)) / D + 3;
    const bend = (0.13 + 0.30 * Math.max(0, Math.sin(t + Math.PI / 2))) / D;
    return [up, bend];
  };
  return {
    lean: 5, head: 0,
    armNear: arm(0), armFar: arm(Math.PI),
    legNear: leg(0), legFar: leg(Math.PI),
    footNear: 8, footFar: -20,
    pelvis: 0,
    bob: 0.012 * (0.5 - 0.5 * Math.cos(2 * theta)),
  };
};

/* ------------------------------------------------------------------ */
/* 换装：只换外层轮廓，骨架与比例不动                                  */
/* ------------------------------------------------------------------ */

export type OutfitHair = 'short' | 'bun' | 'long' | 'bald';

/** 一层外衣 = 一种**形状描述**，不是一种样式名。
 *
 *  为什么不是枚举：`garment: 'coat' | 'robe' | ...` 每加一种就要改组件，
 *  而「随便一个样式」永远会超出列表。形状本身只有两件事——长到哪、有多宽，
 *  其余都是这两件事的组合。所以这里只收形状，不收名字：
 *
 *  · 日常衣服给四个数：`hem`（衣摆高度）/ `ease`（比身体松多少）/
 *    `flare`（下摆往外开多少）/ `top`（从哪开始），在已认可的身体轮廓上做偏移；
 *  · 任意外形给一条侧面轮廓 `outline` + 半宽表 `width`，模型按需求现场写。
 *
 *  单位：`[u, v]`，u 朝前为正、v 是距**胯关节**的高度，都是身高比例（身高 = 1）；
 *  `width`/`ease`/`flare` 同理，给的是半宽（不是全宽）。 */
export type ShellSpec = {
  /** 自定义侧影。不给就按 `hem/top/ease/flare` 从身体轮廓推。 */
  outline?: Pt[];
  width?: Array<[number, number]>;
  rings?: number;
  /** 衣摆高度（胯局部，负数＝胯下）。缺省 -0.04（刚过胯）。 */
  hem?: number;
  /** 从哪个高度开始算这件衣服（缺省到肩顶）。 */
  top?: number;
  /** 比身体松多少（半宽，身高比例）。T 恤 0.006，大衣 0.03。 */
  ease?: number;
  /** 下摆比上口往外开多少（半宽）。直筒 0，长裙 0.1。 */
  flare?: number;
};

/** 一件衣服 = 外轮廓的几处替换：衣摆长度、袖长、帽子、头发。 */
export type Outfit = {
  garment?: 'none' | 'tunic' | 'robe' | 'coat' | 'dress' | 'suit';
  sleeve?: 'long' | 'short' | 'none';
  length?: 'hip' | 'knee' | 'ankle';
  headwear?: 'none' | 'cap' | 'straw' | 'brim' | 'hood';
  hair?: OutfitHair;
  /** 自定义的一层外衣，叠在 `garment` 外：斗篷、披风、甲胄、唐装…
   *  给了就不再受 `garment` 那几个名字的限制。 */
  shell?: ShellSpec;
  /** 自定义头发轮廓（头局部坐标，颈根 v = 0，身高比例）。给了就覆盖 `hair`。 */
  hairOutline?: Pt[];
};

/* ------------------------------------------------------------------ */
/* 比例与轮廓：1/7.5 头身，取自 WalkingFigure                          */
/* ------------------------------------------------------------------ */

export const B = {
  hip: 0.520,
  headTop: 0.984,
  upperArm: 0.170,
  forearm: 0.155,
  thigh: 0.255,
  shin: 0.240,
  foot: 0.108,
};

/** 躯干外轮廓（盆骨局部坐标；u 朝前为正，v 相对盆骨）。
 *
 *  2026-10-22 收回厚度：原稿胸到背 0.193H，1.8m 的人是 35cm 厚——侧看是一只桶胸，
 *  白光一扫就读成练过的胸肌（用户原话「太壮了，尤其是胸肌」）。这一版收到 0.133H
 *  （24cm），而且胸骨往下到小腹几乎是一条**直线**：薄肌靠的是这条直线，弧一鼓
 *  就回到练过的那一档。背面的腰窝与肩胛也按同一比例收。 */
export const TORSO: Pt[] = [
  [-0.062, 0.246], [-0.024, 0.284], [0.026, 0.280], [0.070, 0.238],
  [0.069, 0.180], [0.055, 0.080], [0.063, -0.005],
  [-0.060, -0.005], [-0.050, 0.080], [-0.064, 0.180],
];

/** 侧面头（颈根在原点）：额头→眉弓→鼻梁→鼻尖→人中→唇→下巴→下颌。 */
export const HEAD: Pt[] = [
  [-0.042, 0.111], [-0.020, 0.133], [0.012, 0.139], [0.034, 0.127],
  [0.044, 0.111], [0.042, 0.096], [0.062, 0.078], [0.049, 0.068],
  [0.054, 0.060], [0.049, 0.052], [0.057, 0.037], [0.044, 0.023],
  [0.022, 0.014], [0.020, -0.003], [-0.012, -0.003], [-0.042, 0.033],
  [-0.050, 0.071],
];

/** 短发：一顶从额顶盖到后颈的壳，外沿比颅骨大一点，侧影才有厚度。 */
export const HAIR_SHORT: Pt[] = [
  [0.040, 0.106], [0.040, 0.134], [0.012, 0.148], [-0.024, 0.141],
  [-0.049, 0.110], [-0.058, 0.062], [-0.053, 0.016], [-0.036, 0.010],
  [-0.040, 0.062], [-0.033, 0.100], [0.004, 0.124], [0.028, 0.114],
];
export const HAIR_BUN: Pt[] = [
  [-0.068, 0.092], [-0.080, 0.126], [-0.052, 0.144], [-0.028, 0.128], [-0.034, 0.098],
];
export const HAIR_LONG: Pt[] = [
  [-0.048, 0.104], [-0.072, 0.062], [-0.090, 0.006], [-0.080, -0.060],
  [-0.062, -0.048], [-0.056, 0.020], [-0.040, 0.070],
];

/* ------------------------------------------------------------------ */
/* 骨架求点                                                            */
/* ------------------------------------------------------------------ */

/** 一条两段肢体的关节链（侧面）：起点、末端与肘/膝点。
 *
 *  `flex` 是第二节相对第一节的转向：**肘是 +1（向前折），膝是 −1（向后折）**。
 *  姿势表沿用 `WalkingFigure` 的写法（大腿角度 − 膝角），所以腿必须传 −1；
 *  两边都按 +1 算，坐着和走路时的膝盖会朝前反折，小腿翘到身前。 */
export const limbChain = (
  root: Pt,
  angles: [number, number],
  upper: number,
  lower: number,
  flex: 1 | -1 = 1,
): { root: Pt; mid: Pt; tip: Pt } => {
  const [a, bend] = angles;
  const d1 = rot([0, -upper], a);
  const mid: Pt = [root[0] + d1[0], root[1] + d1[1]];
  const d2 = rot([0, -lower], a + flex * bend);
  const tip: Pt = [mid[0] + d2[0], mid[1] + d2[1]];
  return { root, mid, tip };
};
