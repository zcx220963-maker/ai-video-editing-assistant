/**
 * `@dg/lib/style/human` —— 三维人形能力件（**人影式**：把已经认可的侧面剪影加一层体积）。
 *
 * 2026-10-22 用户第四次说三维人形，原话是：
 *
 *   「这样的2d做成3d就行了,整体都是一个颜色懂不懂,默认黑色和白光去凸显3d感即可」
 *
 * 所以这一件不是「更精细的模型」，而是**参考片里那个黑色剪影的立体版**：
 *
 *   ① **轮廓只有一份。** 侧面轮廓（躯干、头、四肢、鞋、衣服）全部来自
 *      `./human-shape`——2.5D 的 `HumanSketch` 用的也是这一份。两份各写一套比例，
 *      迟早又会「三维不像二维」（前三版就是这么来的）。
 *   ② **体积是横截面放样出来的，不是球和胶囊拼的。** 每个高度取一条横截面：
 *      前后缘来自侧面轮廓，左右半宽来自一张宽度表（肩宽、腰收、胯宽），
 *      截面是椭圆，所以侧看是那条轮廓、正看有肩腰胯的起伏。四肢沿骨链扫椭圆。
 *      四版里「胳膊像木板」就是绕向反了：背面被剔除，只剩半张壳。
 *   ③ **默认一个颜色。** 参考片是纯黑剪影，三维感靠**白光**在肩、额、胯上打出
 *      一层柔和的高光。默认 `color='#0b0b0d'`，整具人一个材质；要换也只换
 *      外轮廓（`outfit`）与颜色（`color` / `wardrobe`），不改比例。
 *   ④ **不画五官。** 头是颅骨轮廓放样，鼻只在正面中线起一道脊；眼、眉、嘴一概
 *      不画——贴上去只会读成面具。
 *
 *   · `HumanFigure` —— 全身：站/坐/蹲/跪/走/指/伸/抱/靠/卧，或关节角度覆盖。
 *   · `HumanHead` —— 单独的头（近景）：同一张头部轮廓，颈根在原点，脸朝 +Z。
 *   · `HumanHand` —— 近景手：掌弓、四指三节、拇指对掌、指甲。三维人形的手也用它。
 *   · `useSkin` / `useCloth` —— 需要上色的镜头用的材质（默认剪影不用）。
 *   · `ContactShadow` —— 脚下一块软接触阴影。
 *
 * 纪律：不读帧、不用 `Math.random`/`Date.now`；所有随机量来自 `seed`，
 * 同一份代码渲两次逐像素相同。
 */
import React, { useMemo } from 'react';
import * as THREE from 'three';
import {
  B, HEAD, POSES, STAND, TORSO, gait, lerp2, limbChain, norm, rot,
} from './human-shape';
import { surfaceNets } from './surface-nets';
import type { Joints, Outfit, OutfitHair, Pt, PoseName, ShellSpec } from './human-shape';

const D = Math.PI / 180;
type Vec3 = [number, number, number];

export type HumanPoseName = PoseName;

/** 关节角度覆盖（单位度）。只有矢状面：正数＝向后，所以前屈是负数。 */
export type HumanJoints = {
  spine?: Vec3;
  neck?: Vec3;
  head?: Vec3;
  shoulderL?: Vec3;
  shoulderR?: Vec3;
  elbowL?: number;
  elbowR?: number;
  wristL?: Vec3;
  wristR?: Vec3;
  hipL?: Vec3;
  hipR?: Vec3;
  kneeL?: number;
  kneeR?: number;
  ankleL?: number;
  ankleR?: number;
  /** 盆骨整体升降（按身高比例，正值向上）。坐/蹲/跪时用它把人放到座面上。 */
  pelvisY?: number;
};

export type HumanBuild = 'slim' | 'regular' | 'heavy' | 'child' | 'elder';

/** 体型只改厚度与宽窄，不改骨长：骨长一动，脚就离地了。 */
const BUILDS: Record<HumanBuild, { w: number; d: number; limb: number }> = {
  slim: { w: 0.92, d: 0.94, limb: 0.90 },
  regular: { w: 1.0, d: 1.0, limb: 1.0 },
  heavy: { w: 1.16, d: 1.12, limb: 1.10 },
  child: { w: 0.88, d: 0.90, limb: 0.92 },
  elder: { w: 0.96, d: 1.0, limb: 0.96 },
};

/** 站着不动的走姿（三维件不读帧；要动就由镜头逐帧改 `pose` 或位置）。 */
const WALK_JOINTS: Joints = gait(2.2);

/** 把 3D 风格的关节覆盖换算成侧面骨架。 */
const jointsFrom = (src: HumanJoints): Joints => ({
  lean: src.spine ? src.spine[0] : STAND.lean,
  head: (src.neck?.[0] || 0) + (src.head?.[0] || 0),
  armNear: src.shoulderR ? [-src.shoulderR[0], -(src.elbowR || 0)] : STAND.armNear,
  armFar: src.shoulderL ? [-src.shoulderL[0], -(src.elbowL || 0)] : STAND.armFar,
  legNear: src.hipR ? [-src.hipR[0], src.kneeR ?? 0] : STAND.legNear,
  legFar: src.hipL ? [-src.hipL[0], src.kneeL ?? 0] : STAND.legFar,
  footNear: src.ankleR ?? STAND.footNear,
  footFar: src.ankleL ?? STAND.footFar,
  pelvis: src.pelvisY ?? STAND.pelvis,
  bob: 0,
});

/* ------------------------------------------------------------------ */
/* 材质（需要上色时才用；默认剪影只用 `color`）                        */
/* ------------------------------------------------------------------ */

const NOISE = new Map<string, THREE.Texture>();

/** 确定性噪声贴图（bump 用）。模块级缓存：同一 seed 只生成一次。 */
const noiseTexture = (size: number, seed: number, grain: number): THREE.Texture => {
  const key = `${size}|${seed}|${grain}`;
  const hit = NOISE.get(key);
  if (hit) return hit;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (ctx) {
    const img = ctx.createImageData(size, size);
    for (let y = 0; y < size; y += 1) {
      for (let x = 0; x < size; x += 1) {
        const i = (y * size + x) * 4;
        const n1 = Math.sin(((x * grain) % 97) * 12.9898 + ((y * grain) % 89) * 78.233 + seed) * 43758.5453;
        const n2 = Math.sin((x * 3 + y * 5) * 0.7 + seed * 1.7) * 43758.5453;
        const v = Math.round(255 * (0.55 * (n1 - Math.floor(n1)) + 0.45 * (n2 - Math.floor(n2))));
        img.data[i] = v;
        img.data[i + 1] = v;
        img.data[i + 2] = v;
        img.data[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(3, 3);
  NOISE.set(key, tex);
  return tex;
};

export type SkinMaterial = {
  color: string;
  roughness: number;
  metalness: number;
  clearcoat: number;
  clearcoatRoughness: number;
  sheen: number;
  sheenColor: string;
  sheenRoughness: number;
  bumpMap?: THREE.Texture;
  bumpScale?: number;
};

/** 皮肤材质。摊进 `<meshPhysicalMaterial>`：
 *
 *  ```tsx
 *  const skin = useSkin('#c08a68');
 *  <meshPhysicalMaterial {...skin} />
 *  ```
 *
 *  写实的皮肤不是「光滑的高光」：底色偏红、粗糙度低但不为零、最亮的一层是
 *  很薄的清漆（汗与油），暗部带一点暖色透光（sheen 近似次表面）。 */
export const useSkin = (color = '#c08a68', seed = 3): SkinMaterial => {
  const bumpMap = useMemo(() => noiseTexture(128, seed, 9), [seed]);
  return {
    color,
    roughness: 0.47,
    metalness: 0.0,
    clearcoat: 0.16,
    clearcoatRoughness: 0.55,
    sheen: 0.28,
    sheenColor: '#ff9a72',
    sheenRoughness: 0.62,
    bumpMap,
    bumpScale: 0.0016,
  };
};

export type ClothMaterial = {
  color: string;
  roughness: number;
  metalness: number;
  sheen: number;
  sheenColor: string;
  sheenRoughness: number;
  bumpMap?: THREE.Texture;
  bumpScale?: number;
};

/** 布料材质：低金属度、高粗糙度、sheen 给一点绒毛感。 */
export const useCloth = (color = '#2b3038', seed = 7): ClothMaterial => {
  const bumpMap = useMemo(() => noiseTexture(128, seed, 22), [seed]);
  return {
    color,
    roughness: 0.92,
    metalness: 0.0,
    sheen: 0.5,
    sheenColor: '#cfd6e0',
    sheenRoughness: 0.85,
    bumpMap,
    bumpScale: 0.0024,
  };
};

/* ------------------------------------------------------------------ */
/* 轮廓 → 体积                                                         */
/* ------------------------------------------------------------------ */

type Map3 = (u: number, v: number, x: number) => Vec3;

/** 一张按 v 递增的宽度表，线性插值，两端夹住。 */
const tableAt = (rows: ReadonlyArray<readonly [number, number]>, v: number): number => {
  if (v <= rows[0][0]) return rows[0][1];
  for (let i = 1; i < rows.length; i += 1) {
    if (v <= rows[i][0]) {
      const a = rows[i - 1];
      const b = rows[i];
      const t = (v - a[0]) / (b[0] - a[0] || 1);
      return a[1] + (b[1] - a[1]) * t;
    }
  }
  return rows[rows.length - 1][1];
};

/** 闭合多边形在高度 v 处的横切：返回 [后缘 u, 前缘 u]。 */
const sliceU = (poly: Pt[], v: number): [number, number] | null => {
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = 0; i < poly.length; i += 1) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if ((a[1] < v && b[1] < v) || (a[1] > v && b[1] > v)) continue;
    if (Math.abs(b[1] - a[1]) < 1e-9) {
      lo = Math.min(lo, a[0], b[0]);
      hi = Math.max(hi, a[0], b[0]);
      continue;
    }
    const t = (v - a[1]) / (b[1] - a[1]);
    if (t < 0 || t > 1) continue;
    const u = a[0] + (b[0] - a[0]) * t;
    lo = Math.min(lo, u);
    hi = Math.max(hi, u);
  }
  return lo <= hi && Number.isFinite(lo) ? [lo, hi] : null;
};

/** 高度 v 处**抹平后**的横切（沿 v 取 5 点平均）。
 *
 *  侧面轮廓是多边形，每个折角放样出来就是绕身一圈的横棱（肩线、衣摆、腰）。
 *  躯干与衣料都必须用这一条，否则白光一扫就是一格一格的横带。 */
const smoothProfile = (poly: Pt[], v: number, v0: number, v1: number): [number, number] => {
  const step = (v1 - v0) * 0.07;
  let sc = 0;
  let sh = 0;
  let n = 0;
  for (const k of [-2, -1, 0, 1, 2]) {
    const s = sliceU(poly, Math.max(v0, Math.min(v1, v + k * step)));
    if (!s) continue;
    sc += (s[0] + s[1]) / 2;
    sh += (s[1] - s[0]) / 2;
    n += 1;
  }
  return n ? [sc / n, sh / n] : [0, 0];
};

/* ------------------------------------------------------------------ */
/* 距离场基元                                                          */
/* ------------------------------------------------------------------ */

type Sdf = (x: number, y: number, z: number) => number;

/** 平滑并集。`k` 是融合半径（米）：k=0 就是硬 min（交线），k=2cm 两个实体的
 *  交界在 2cm 内互相「长」过去，读起来是一块肉，而不是两个件贴在一起。 */
const smin = (a: number, b: number, k: number): number => {
  const t = Math.max(0, Math.min(1, 0.5 + (0.5 * (b - a)) / k));
  return b + (a - b) * t - k * t * (1 - t);
};

/** 两端半径不同的胶囊（近似圆锥胶囊）。四肢的每一段都是这个。 */
const sdSeg = (
  px: number, py: number, pz: number,
  a: Vec3, b: Vec3, ra: number, rb: number,
): number => {
  const bax = b[0] - a[0];
  const bay = b[1] - a[1];
  const baz = b[2] - a[2];
  const pax = px - a[0];
  const pay = py - a[1];
  const paz = pz - a[2];
  const den = bax * bax + bay * bay + baz * baz || 1e-9;
  const raw = (pax * bax + pay * bay + paz * baz) / den;
  const t = raw < 0 ? 0 : raw > 1 ? 1 : raw;
  const dx = pax - bax * t;
  const dy = pay - bay * t;
  const dz = paz - baz * t;
  return Math.hypot(dx, dy, dz) - (ra + (rb - ra) * t);
};

/** 一根**沿折线半径连续变化**的管。四肢必须用它，不能把一串胶囊用 smin 接起来：
 *  每接一次，smin 都会在接点鼓出一圈（k/2 的膨胀），六段接下来是一条糖葫芦。
 *  半径在结点处直接插值，表面就只剩半径表本身那点起伏，关节不会多出包。 */
const sdChain = (
  px: number, py: number, pz: number,
  pts: Vec3[], rad: number[],
): number => {
  let best = Infinity;
  for (let i = 0; i < pts.length - 1; i += 1) {
    const a = pts[i];
    const b = pts[i + 1];
    const bx = b[0] - a[0];
    const by = b[1] - a[1];
    const bz = b[2] - a[2];
    const den = bx * bx + by * by + bz * bz || 1e-9;
    const raw = ((px - a[0]) * bx + (py - a[1]) * by + (pz - a[2]) * bz) / den;
    const t = raw < 0 ? 0 : raw > 1 ? 1 : raw;
    const dx = px - (a[0] + bx * t);
    const dy = py - (a[1] + by * t);
    const dz = pz - (a[2] + bz * t);
    const d = Math.hypot(dx, dy, dz) - (rad[i] + (rad[i + 1] - rad[i]) * t);
    if (d < best) best = d;
  }
  return best;
};

/** 把折线的半径按 Catmull-Rom 加密：位置仍走折线（关节的角度变化很小），
 *  **半径**的斜率在结点处不再断——半径斜率一断，表面就在那一圈留一道折，
 *  膝、踝上那两道横线就是这么来的（正面白光一扫最明显）。 */
const densify = (
  pts: Vec3[], rad: number[], per: number,
): { pts: Vec3[]; rad: number[] } => {
  const n = pts.length;
  const r0 = (i: number): number => rad[Math.max(0, Math.min(n - 1, i))];
  const outP: Vec3[] = [pts[0]];
  const outR: number[] = [rad[0]];
  for (let i = 0; i < n - 1; i += 1) {
    const a = r0(i - 1);
    const b = r0(i);
    const c = r0(i + 1);
    const d = r0(i + 2);
    for (let k = 1; k <= per; k += 1) {
      const t = k / per;
      const t2 = t * t;
      const t3 = t2 * t;
      const r = 0.5 * (
        2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3
      );
      outR.push(Math.max(0.002, r));
      const p = pts[i];
      const q = pts[i + 1];
      outP.push([p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t]);
    }
  }
  return { pts: outP, rad: outR };
};

/** 用第一个三角形判一次绕向：法线必须背离中轴。反了就整份翻过来。
 *  （上一版的管体就是绕向反了，背面被剔除，渲出来像木板。） */
const finish = (pos: number[], idx: number[], center: Vec3): THREE.BufferGeometry => {
  const at = (i: number): Vec3 => [pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]];
  const a = at(idx[0]);
  const b = at(idx[1]);
  const c = at(idx[2]);
  const e1: Vec3 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  const e2: Vec3 = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
  const n: Vec3 = [
    e1[1] * e2[2] - e1[2] * e2[1],
    e1[2] * e2[0] - e1[0] * e2[2],
    e1[0] * e2[1] - e1[1] * e2[0],
  ];
  const dm: Vec3 = [a[0] - center[0], a[1] - center[1], a[2] - center[2]];
  if (n[0] * dm[0] + n[1] * dm[1] + n[2] * dm[2] < 0) {
    for (let i = 0; i < idx.length; i += 3) {
      const t = idx[i + 1];
      idx[i + 1] = idx[i + 2];
      idx[i + 2] = t;
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
};

/** 侧面闭合轮廓 → 竖直放样体：每个高度一条椭圆截面（前后缘来自轮廓，
 *  左右半宽来自 `width`）。两头顶上中心点封口。
 *
 *  `ridge`：脸上那道鼻梁只能在中线上存在。直接拿侧面轮廓放样，鼻子 5mm 的凸起会
 *  被铺满整个头的**宽度**，头上就出现一排横棱（上一版看起来像木乃伊）。给了
 *  `ridge` 就把「原始轮廓 − 高度上抹平后的轮廓」按 `|cosθ|^ridge` 衰减：中线处仍然
 *  是原始轮廓（侧影不变），越往两侧越只剩抹平后的那个蛋形。 */
const loftPart = (opts: {
  poly: Pt[];
  rings: number;
  seg: number;
  width: (v: number) => number;
  map: Map3;
  cap?: boolean;
  ridge?: number;
  /** 整圈都用抹平后的轮廓（躯干）：侧面轮廓是多边形，每个折角被放样成一整圈
   *  横向的折线，白光一扫就是一道腰带/护肩/胸肌。正脸那道鼻梁才要 `ridge`。 */
  smooth?: boolean;
}): THREE.BufferGeometry => {
  const { poly, rings, seg, width, map, cap = true, ridge = 0, smooth = false } = opts;
  let v0 = Infinity;
  let v1 = -Infinity;
  for (const p of poly) {
    v0 = Math.min(v0, p[1]);
    v1 = Math.max(v1, p[1]);
  }
  const smoothAt = (v: number): [number, number] => smoothProfile(poly, v, v0, v1);
  const rows: Array<{ v: number; c: number; hd: number; w: number; sc: number; sh: number }> = [];
  for (let i = 0; i <= rings; i += 1) {
    const v = v0 + (v1 - v0) * (i / rings);
    const s = sliceU(poly, v);
    if (!s) continue;
    const [sc, sh] = ridge > 0 || smooth ? smoothAt(v) : [0, 0];
    // 横切越薄，左右半宽也要跟着收。最底下那一圈如果只剩一个点、宽度却还是满的，
    // 就摊出一片 0 厚、5cm 宽的**刀片**——鞋底两根角上那根尖刺就是它。
    const thin = Math.min(1, (s[1] - s[0]) / 0.006);
    rows.push({
      v,
      c: (s[0] + s[1]) / 2,
      hd: Math.max(0.0015, (s[1] - s[0]) / 2),
      w: Math.max(0.002, width(v) * thin),
      sc,
      sh,
    });
  }
  return loftRows(rows, seg, map, { ridge, smooth, cap });
};

/** 一行环：v、前后中线 c、前后半厚 hd、左右半宽 w；`sc/sh` 是抹平后的那条
 *  （`smooth`/`ridge` 用）。行由轮廓切出来，也可以由调用方直接给——自定义外衣
 *  就是后者：它要先把身体的横切并进来，才不会被身体顶穿。 */
type LoftRow = { v: number; c: number; hd: number; w: number; sc: number; sh: number };

const loftRows = (
  rows: LoftRow[],
  seg: number,
  map: Map3,
  opts: { ridge?: number; smooth?: boolean; cap?: boolean } = {},
): THREE.BufferGeometry => {
  const { ridge = 0, smooth = false, cap = true } = opts;
  const pos: number[] = [];
  for (const r of rows) {
    for (let j = 0; j < seg; j += 1) {
      const a = (j / seg) * Math.PI * 2;
      const ca = Math.cos(a);
      let u = r.c + r.hd * ca;
      if (ridge > 0 || smooth) {
        const k = smooth && ridge === 0 ? 0 : Math.pow(Math.abs(ca), ridge);
        u = r.sc + r.sh * ca + (r.c - r.sc + (r.hd - r.sh) * ca) * k;
      }
      const x = r.w * Math.sin(a);
      const q = map(u, r.v, x);
      pos.push(q[0], q[1], q[2]);
    }
  }
  const idx: number[] = [];
  for (let i = 0; i < rows.length - 1; i += 1) {
    for (let j = 0; j < seg; j += 1) {
      const a = i * seg + j;
      const b = (i + 1) * seg + j;
      const c = i * seg + ((j + 1) % seg);
      const d = (i + 1) * seg + ((j + 1) % seg);
      idx.push(a, c, b, c, d, b);
    }
  }
  if (cap) {
    const first = rows[0];
    const last = rows[rows.length - 1];
    const bc = pos.length / 3;
    pos.push(...map(first.c, first.v, 0));
    for (let j = 0; j < seg; j += 1) idx.push(bc, (j + 1) % seg, j);
    const tc = pos.length / 3;
    pos.push(...map(last.c, last.v, 0));
    const base = (rows.length - 1) * seg;
    for (let j = 0; j < seg; j += 1) idx.push(tc, base + j, base + ((j + 1) % seg));
  }
  return finish(pos, idx, map(rows[0].c, rows[0].v, 0));
};

/** 骨链扫椭圆截面：矢状半轴来自侧面轮廓（`radii`），左右半轴按 `lat` 收窄。
 *  侧看轮廓与 2D 完全一致，正看是一根有肉的肢体。 */
const sweepLimb = (opts: {
  pts: Pt[];
  radii: number[];
  /** 左右中心（身高比例）；与点列同一坐标系，出几何时统一乘 `h`。 */
  x: number;
  h: number;
  lat: number;
  seg: number;
  sub?: number;
}): THREE.BufferGeometry => {
  const { pts, radii, x, h, lat, seg, sub = 4 } = opts;
  const S = h;
  const P: Pt[] = [];
  const R: number[] = [];
  for (let i = 0; i < pts.length - 1; i += 1) {
    for (let k = 0; k < sub; k += 1) {
      const t = k / sub;
      P.push([pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t]);
      R.push(radii[i] + (radii[i + 1] - radii[i]) * t);
    }
  }
  P.push([pts[pts.length - 1][0], pts[pts.length - 1][1]]);
  R.push(radii[radii.length - 1]);

  const pos: number[] = [];
  const n = P.length;
  for (let i = 0; i < n; i += 1) {
    const a = P[Math.max(0, i - 1)];
    const b = P[Math.min(n - 1, i + 1)];
    const du = b[0] - a[0];
    const dv = b[1] - a[1];
    const l = Math.hypot(du, dv) || 1;
    const nu = -dv / l;
    const nv = du / l;
    for (let j = 0; j < seg; j += 1) {
      const th = (j / seg) * Math.PI * 2;
      const sag = R[i] * Math.cos(th);
      const latR = R[i] * lat * Math.sin(th);
      pos.push((x + latR) * S, (P[i][1] + sag * nv) * S, (P[i][0] + sag * nu) * S);
    }
  }
  const idx: number[] = [];
  for (let i = 0; i < n - 1; i += 1) {
    for (let j = 0; j < seg; j += 1) {
      const a = i * seg + j;
      const b = (i + 1) * seg + j;
      const c = i * seg + ((j + 1) % seg);
      const d = (i + 1) * seg + ((j + 1) % seg);
      idx.push(a, c, b, c, d, b);
    }
  }
  // 两端封口（藏在躯干/手/鞋里的那两头；不封会看见壳内壁）。
  // 顶心往管里**退**一个半径：平切的圆盘正好落在管口平面上，肘/膝一折就从另一段
  // 里露出来，是一条发亮的白环（上一版肘部那道环就是它）。凹进去的锥面藏在管里。
  const capEnd = (ring: number, flip: boolean, center: Vec3, inward: Vec3, r: number) => {
    const ci = pos.length / 3;
    pos.push(center[0] + inward[0] * r * 0.8, center[1] + inward[1] * r * 0.8, center[2] + inward[2] * r * 0.8);
    for (let j = 0; j < seg; j += 1) {
      const a = ring * seg + j;
      const b = ring * seg + ((j + 1) % seg);
      if (flip) idx.push(ci, a, b);
      else idx.push(ci, b, a);
    }
  };
  const unit = (a: Vec3): Vec3 => {
    const l = Math.hypot(a[0], a[1], a[2]) || 1;
    return [a[0] / l, a[1] / l, a[2] / l];
  };
  const d0 = unit([0, P[1][1] - P[0][1], P[1][0] - P[0][0]]);
  const d1 = unit([0, P[n - 2][1] - P[n - 1][1], P[n - 2][0] - P[n - 1][0]]);
  capEnd(0, false, [x * S, P[0][1] * S, P[0][0] * S], d0, R[0]);
  capEnd(n - 1, true, [x * S, P[n - 1][1] * S, P[n - 1][0] * S], d1, R[n - 1]);
  return finish(pos, idx, [x * S, P[0][1] * S, P[0][0] * S]);
};

/** 躯干左右半宽（盆骨局部 v）。肩、腰、胯的起伏全在这张表里。
 *
 *  这是**正面**看过去的那条线，一整条连续曲线：胯 0.076 → 腰 0.065 →
 *  胸 0.070 → 肩 0.074 → 颈根 0.033。最宽的一档放在**肩**上，胸比肩窄
 *  半厘米：上一版最宽处就是胸（0.080，比肩还宽），正面的轮廓读成
 *  「胸比肩宽」——那是练过的人，不是薄肌。
 *
 *  肩**不另做球**：三角肌算进躯干这张表里，胳膊从肩峰下面伸出去。把肩头做成
 *  一根单独的管顶一个半球，正面上永远是「平肩 + 两边各一个球」，读出来就是
 *  人体模型／练过的胸肌；连着做，肩到脖子是一条往下的斜线，才是穿衣服的人。
 *  表里不许有折点：宽度斜率一变，白光就在腰上横着划一道（上一版的「腰带」）。 */
const TORSO_W: Array<[number, number]> = [
  // 2026-10-22 第二次量：上一版的「半宽」相当于 1.84m 的人肩宽 27cm、胯 28cm，
  // 正面看是一块板。按真人量：胯 33cm、腰 28cm、胸 32cm、肩 35cm（再加胳膊 45cm）。
  // 侧面轮廓（TORSO）不动——厚度那一档用户已经认可了。
  // 底端收到 0：躯干那一段在裆下必须是收成一个尖，插进两条大腿之间。
  // 收成一根 1.8cm 宽的平头，正面就是一个吊在两腿之间的圆筒端盖——上面那道
  // 亮边读成「裙摆线」，用户说的积木感有一半来自这里。
  [-0.052, 0.002], [-0.044, 0.018], [-0.032, 0.048], [-0.018, 0.078], [0.0, 0.090], [0.050, 0.089],
  [0.080, 0.084], [0.110, 0.078], [0.145, 0.080], [0.175, 0.084], [0.205, 0.088],
  // 肩那一档收得比上一版缓：0.248→0.285 这三厘米半里从 9.6cm 收到 2.4cm，
  // 斜度接近 75°，正面看是两条平肩加一个直角。真人从颈根到肩峰是 35~40°。
  [0.232, 0.093], [0.244, 0.095],
  [0.256, 0.090], [0.266, 0.079], [0.274, 0.062], [0.280, 0.044],
  [0.284, 0.030], [0.286, 0.022],
];

/** 头的左右半宽（头局部 v）。 */
const HEAD_W: Array<[number, number]> = [
  // 往下多两档：三维那一件的下颌要收成一个锥，插进脖子里（见 `HEAD_SKIN`）。
  [-0.024, 0.012], [-0.016, 0.015], [-0.010, 0.018], [0.0, 0.026], [0.020, 0.032], [0.040, 0.038], [0.060, 0.042],
  [0.080, 0.044], [0.100, 0.043], [0.115, 0.039], [0.127, 0.031], [0.136, 0.018],
];

/** 衣服的左右半宽：比躯干大一圈；长袍在胯下向外散开。 */
const garmentWidth = (v: number): number => {
  if (v > -0.01) return tableAt(TORSO_W, v) + 0.012;
  const t = Math.min(1, Math.max(0, (-0.01 - v) / 0.42));
  return 0.094 + 0.026 * t;
};

/** 躯干局部 (u,v) → 世界（含盆骨升降与前倾）。
 *
 *  三个分量**都要乘 h**。这里漏掉 x 的那一版，躯干只有真实宽度的一半（w 是身高
 *  比例，四肢走 `sweepLimb` 乘了 h、躯干没乘），胳膊就整根悬在躯干外面——正面看
 *  是一个窄瓶子两边各浮着一根管子，肩永远接不上。 */
const bodyMap = (h: number, hipV: number, lean: number): Map3 => (u, v, x) => {
  const r = rot([u, v], -lean);
  return [x * h, (hipV + r[1]) * h, r[0] * h];
};

/** 头部所在位置（盆骨局部）：颈根在 (0.010, 0.325) 上。 */
const HEAD_BASE: Pt = [0.010, 0.325];
const HEAD_TOP = 0.139;

/** 头部轮廓在某一高度的横切。长发垂到颈下（v < 0）时换成一块后帘。 */
const headRing = (v: number): { c: number; hd: number; w: number } => {
  const vv = Math.max(-0.003, Math.min(HEAD_TOP, v));
  const s = sliceU(HEAD, vv) ?? [0, 0.04];
  let c = (s[0] + s[1]) / 2;
  let hd = Math.max(0.003, (s[1] - s[0]) / 2);
  let w = Math.max(0.006, tableAt(HEAD_W, vv));
  if (v < -0.003) {
    c = -0.034;
    hd = 0.020;
    w = 0.050;
  }
  return { c, hd, w };
};

/** 三维这一件的颅骨轮廓：在共用侧影 `HEAD` 下面把下颌收成一个锥。
 *
 *  共用的 `HEAD` 在 v≈-0.003 处是平切——二维那边被身体挡住，三维这边是
 *  头顶上挂下来的一条实心边：脖子的圆柱和下颌的平底叠在一起，正面看是
 *  「头插在管子上」的一圈台阶，侧看下巴悬在脖子外面。补成锥之后，这段插进
 *  脖子里，露出来的只有下颌那一圈，接缝就没了。
 *  共用那份**不动**：它是二维剪影已被认可的形状。 */
const HEAD_SKIN: Pt[] = [
  ...HEAD.slice(0, 13),
  [0.018, -0.010], [0.012, -0.019], [-0.002, -0.022], [-0.020, -0.012],
  ...HEAD.slice(15),
];

/** 头发：贴着颅骨长出来的一层壳；前额发际线高、后颈低。长发在后脑垂到颈下。 */
const hairShell = (style: OutfitHair, scale: number): THREE.BufferGeometry | null => {
  if (style === 'bald') return null;
  const seg = 44;
  const rings = 10;
  const topV = HEAD_TOP + 0.003;
  const longBack = style === 'long';
  const hairline = (theta: number) => {
    const front = 0.02 + 0.092 * (1 + Math.cos(theta)) / 2;
    if (!longBack) return front;
    const back = Math.max(0, -Math.cos(theta));
    return front - 0.088 * back * back;
  };
  // 往外**加**一个绝对厚度，而不是按比例放大 5%：颅骨在环与环之间还会鼓出来，
  // 5% 在头顶只有半毫米，壳与颅骨互相穿插——上一版头上那几道横纹就是它。
  const lift = 0.0065;
  const pos: number[] = [];
  const idx: number[] = [];
  for (let j = 0; j < seg; j += 1) {
    const theta = (j / seg) * Math.PI * 2;
    const lo = hairline(theta);
    for (let i = 0; i <= rings; i += 1) {
      const t = i / rings;
      const v = lo + (topV - lo) * t;
      const r = headRing(v);
      // 发际线那一圈厚度收到 0：壳的**边缘**是敞开的，等厚收边会在额头横一条
      // 亮棱（读成头盔的帽檐）。收成 0，壳就从颅骨里长出来，没有边。
      const th = lift * Math.min(1, t / 0.22);
      const u = r.c + (r.hd + th) * Math.cos(theta);
      const x = (r.w + th) * Math.sin(theta);
      pos.push(x * scale, v * scale, u * scale);
    }
  }
  for (let j = 0; j < seg; j += 1) {
    const j2 = (j + 1) % seg;
    for (let i = 0; i < rings; i += 1) {
      const a = j * (rings + 1) + i;
      const b = j2 * (rings + 1) + i;
      idx.push(a, a + 1, b, a + 1, b + 1, b);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
};

/** 自定义发型：一条侧面轮廓放样成一块**实心**发块。
 *
 *  为什么不直接复用 `hairShell`：那是一件贴着颅骨的**壳**，靠边缘厚度读出发型。
 *  整具一个颜色时，壳的外沿就是一圈亮边——上一版在剪影档里读成「戴了顶毛线帽」，
 *  所以剪影档干脆不做壳、把颅骨放大一档糊过去了。可那样一来换发型也没得换：
 *  任何轮廓进去都没有地方长。
 *
 *  这里换成一个体积：轮廓从颅骨里长出去，宽度取头宽加一层，于是**任何**发型
 *  只要给一条侧面轮廓线就成立，不需要给 `hairStyle` 加枚举、也不需要改组件。
 *  长的部分（垂到颈下）按头那一套后帘宽度给，不然底下会收成一根尖。
 */
const hairSolid = (poly: Pt[], s: number): THREE.BufferGeometry | null => {
  if (poly.length < 3) return null;
  /** 头宽 + 一层头发；垂到颈下的部分不走头宽（那头已经收没了），
   *  按一条帘子缓收到 13cm 半宽，长发才不会收成一根尖。 */
  const width = (v: number): number => {
    const head = tableAt(HEAD_W, Math.max(-0.003, v)) + 0.0075;
    if (v >= -0.003) return head;
    const t = Math.min(1, (-0.003 - v) / 0.10);
    return head + (0.068 - head) * t;
  };
  return loftPart({
    poly,
    rings: 26,
    seg: 30,
    width,
    map: (u, v, x) => [x * s, v * s, u * s],
    smooth: true,
  });
};

/** 帽子/头巾的侧面轮廓（头局部）。只换外轮廓，不改头。 */
const headwearPoly = (kind: Outfit['headwear']): Pt[] => {
  switch (kind) {
    case 'straw':
      return [
        [-0.130, 0.106], [-0.050, 0.124], [0.010, 0.152],
        [0.056, 0.130], [0.126, 0.100], [0.060, 0.098],
        [-0.060, 0.100],
      ];
    case 'brim':
      return [
        [-0.110, 0.112], [-0.044, 0.146], [0.014, 0.150],
        [0.050, 0.130], [0.088, 0.116], [0.020, 0.114],
        [-0.050, 0.110],
      ];
    case 'hood':
      return [
        [-0.078, 0.020], [-0.070, 0.108], [-0.010, 0.156],
        [0.048, 0.134], [0.060, 0.086], [0.040, 0.040],
        [0.006, 0.062], [-0.030, 0.030],
      ];
    case 'cap':
      return [
        [-0.052, 0.108], [-0.032, 0.148], [0.014, 0.154],
        [0.048, 0.132], [0.098, 0.124], [0.096, 0.116],
        [0.024, 0.118], [-0.026, 0.106],
      ];
    default:
      return [];
  }
};

const headwearWidth = (kind: Outfit['headwear'], v: number): number => {
  const base = kind === 'straw' ? 0.104 : kind === 'brim' ? 0.082 : kind === 'hood' ? 0.078 : 0.062;
  const t = Math.min(1, Math.max(0, (v - 0.09) / 0.07));
  return base * (0.62 + 0.38 * t);
};

/** 鞋的侧面轮廓：外底一段是**平的**（脚跟到前掌），鞋头收圆、略抬。
 *
 *  两处是对着 2D 改回来的：
 *  ① 转向符号。2D 那边脚尖是 `ankle.v - f[1]`，这里原来是 `+ f[1]`——三维的脚
 *     上下装反了：走路时前脚脚尖往下扎、后脚脚尖翘着。改成同向以后，前脚平、
 *     后脚脚尖自然朝下。
 *  ② 外底原来是从脚跟斜到脚尖的一条边，放样到最底下退化成一个点，左右半宽
 *     却还是满的，鞋底就伸出两根刀片。现在外底先平着走一段，再收到鞋头。 */
const shoePoly = (ankle: Pt, foot: number): Pt[] => {
  const f = rot([1, 0], foot);
  const n = rot([0, 1], foot);
  const at = (a: number, b: number): Pt => [
    ankle[0] + f[0] * a + n[0] * b,
    ankle[1] + f[1] * a + n[1] * b,
  ];
  const sole = 0.021;
  return [
    at(-0.028, -sole),        // 脚跟外底
    at(0.052, -sole),         // 前掌外底（外底到这儿是平的）
    at(0.078, -sole * 0.45),  // 鞋头下沿
    at(0.074, 0.014),         // 鞋头上沿
    at(0.032, 0.032),         // 脚背最高
    at(-0.004, 0.034),        // 踝前
    at(-0.030, 0.010),        // 脚跟后上
  ];
};

const shoeWidth = (v: number, ankleV: number): number => {
  const t = Math.min(1, Math.max(0, (v - (ankleV - 0.022)) / 0.050));
  return 0.030 - 0.008 * t;
};

/* ------------------------------------------------------------------ */
/* 手                                                                  */
/* ------------------------------------------------------------------ */

export interface HumanHandProps {
  /** 手长（掌心 + 中指），世界单位。成人约 0.105 × 身高。 */
  len?: number;
  /** 1 = 右手，-1 = 左手（镜像）。 */
  side?: 1 | -1;
  /** 手指蜷曲 0（伸直）~1（握拳）。 */
  curl?: number;
  /** 拇指外展 0~1（0 贴掌，1 张开）。 */
  spread?: number;
  skin?: string;
  seed?: number;
  castShadow?: boolean;
  /** 整具一个颜色（剪影档）：指甲不用肉白，跟手掌同一个材质。 */
  mono?: boolean;
}

/** 指甲/头发那层「不同材质的亮面」只在要上色时出现。剪影档整具人一个材质，
 *  否则手是黑的、指甲是肉白的、头发带一圈棕光——「整体一个颜色」就破了。 */
const NAIL = { color: '#e6c3aa', roughness: 0.34, clearcoat: 0.5, clearcoatRoughness: 0.3 };
const HAIR = { roughness: 0.82, sheen: 0.26, sheenColor: '#5a4b3e', sheenRoughness: 0.8 };

/** 一根手指：掌指关节 + 三节指骨 + 指甲。局部：根在原点，指朝 -Y。 */
const Finger: React.FC<{
  len: number;
  r: number;
  bend: number;
  x: number;
  /** 指根的高度。**必须落在掌端**：挂回腕上（0）时，整根手指穿在掌心里，
   *  只有指尖从掌根外边探出来，远景就是一只连指手套。 */
  y: number;
  tilt: number;
  skin: Partial<SkinMaterial>;
  nail: Partial<SkinMaterial>;
  castShadow: boolean;
}> = ({ len, r, bend, x, y, tilt, skin, nail, castShadow }) => {
  const seg = [0.42, 0.33, 0.25];
  return (
    <group position={[x, y, 0]} rotation={[0, 0, tilt * D]}>
      <mesh castShadow={castShadow}>
        <sphereGeometry args={[r * 1.12, 12, 10]} />
        <meshPhysicalMaterial {...skin} />
      </mesh>
      <group rotation={[-bend * D, 0, 0]}>
        <mesh position={[0, -len * seg[0] / 2, 0]} castShadow={castShadow}>
          <capsuleGeometry args={[r, len * seg[0], 6, 12]} />
          <meshPhysicalMaterial {...skin} />
        </mesh>
        <group position={[0, -len * seg[0], 0]} rotation={[-bend * D * 1.15, 0, 0]}>
          <mesh position={[0, -len * seg[1] / 2, 0]} castShadow={castShadow}>
            <capsuleGeometry args={[r * 0.92, len * seg[1], 6, 12]} />
            <meshPhysicalMaterial {...skin} />
          </mesh>
          <group position={[0, -len * seg[1], 0]} rotation={[-bend * D * 0.9, 0, 0]}>
            <mesh position={[0, -len * seg[2] / 2, 0]} castShadow={castShadow}>
              <capsuleGeometry args={[r * 0.82, len * seg[2], 6, 12]} />
              <meshPhysicalMaterial {...skin} />
            </mesh>
            {/* 指甲：贴指背的一小片 */}
            <mesh position={[0, -len * seg[2] * 0.62, -r * 0.55]} rotation={[0.15, 0, 0]}>
              <boxGeometry args={[r * 1.5, len * seg[2] * 0.52, r * 0.32]} />
              <meshPhysicalMaterial {...nail} />
            </mesh>
          </group>
        </group>
      </group>
    </group>
  );
};

/** 近景手。局部坐标：腕在原点，手指朝 -Y，掌面朝 +Z（手背朝 -Z）。 */
export const HumanHand: React.FC<HumanHandProps> = ({
  len = 0.105,
  side = 1,
  curl = 0.25,
  spread = 0.5,
  skin = '#c08a68',
  seed = 5,
  castShadow = true,
  mono = false,
}) => {
  const skinFull = useSkin(skin, seed);
  const mat = mono ? { color: skin, roughness: 0.56, metalness: 0 } : skinFull;
  const nailMat = mono ? mat : NAIL;
  const palmL = 0.52 * len;
  const palmW = 0.46 * len;
  const palmD = 0.17 * len;
  const fingerL = [0.92, 1.0, 0.94, 0.78]; // 食指→小指
  const fingerX = [-0.36, -0.12, 0.12, 0.35];
  const c = Math.max(0, Math.min(1, curl));
  const s = Math.max(0, Math.min(1, spread));

  return (
    <group scale={[side, 1, 1]}>
      {/* 掌：掌弓是拱起来的，所以用两段扁椭球而不是一个方块 */}
      <mesh position={[0, -palmL * 0.46, 0]} scale={[palmW / 2, palmL / 2, palmD / 2]} castShadow={castShadow}>
        <sphereGeometry args={[1, 18, 14]} />
        <meshPhysicalMaterial {...mat} />
      </mesh>
      {/* 掌端那一排指关节：手指从这道梁上长出来，不从一块薄板的边缘长出来。 */}
      <mesh position={[0, -palmL * 0.92, 0.003]} scale={[palmW * 0.50, palmL * 0.17, palmD * 0.46]} castShadow={castShadow}>
        <sphereGeometry args={[1, 16, 12]} />
        <meshPhysicalMaterial {...mat} />
      </mesh>
      {/* 腕 */}
      <mesh position={[0, 0.012, 0]} castShadow={castShadow}>
        <cylinderGeometry args={[palmW * 0.27, palmW * 0.31, len * 0.15, 14]} />
        <meshPhysicalMaterial {...mat} />
      </mesh>
      {[0, 1, 2, 3].map((i) => (
        <Finger
          key={i}
          len={len * 0.44 * fingerL[i]}
          // 0.068 那一档的手指有 2.5cm 粗，比指间距还宽——四根糊成一块，远景就是
          // 一只连指手套。收到 1.8cm，指缝才透得出来。
          r={len * (0.050 - i * 0.0035)}
          bend={c * 58}
          x={fingerX[i] * palmW}
          y={-palmL * 0.94}
          tilt={fingerX[i] * 0.9 * side}
          skin={mat}
          nail={nailMat}
          castShadow={castShadow}
        />
      ))}
      {/* 拇指：对掌位，从掌心侧面斜出来 */}
      <group
        position={[palmW * 0.34, -palmL * 0.42, palmD * 0.10]}
        rotation={[0.1, 0.55, -0.95 + s * 0.35]}
      >
        <mesh castShadow={castShadow}>
          <sphereGeometry args={[len * 0.082, 12, 10]} />
          <meshPhysicalMaterial {...mat} />
        </mesh>
        <mesh position={[0, -len * 0.13, 0]} castShadow={castShadow}>
          <capsuleGeometry args={[len * 0.070, len * 0.16, 6, 12]} />
          <meshPhysicalMaterial {...mat} />
        </mesh>
        <group position={[0, -len * 0.26, 0]} rotation={[-0.55 - c * 0.5, 0, 0]}>
          <mesh position={[0, -len * 0.075, 0]} castShadow={castShadow}>
            <capsuleGeometry args={[len * 0.062, len * 0.12, 6, 12]} />
            <meshPhysicalMaterial {...mat} />
          </mesh>
          <mesh position={[0, -len * 0.15, 0]} castShadow={castShadow}>
            <capsuleGeometry args={[len * 0.052, len * 0.08, 6, 12]} />
            <meshPhysicalMaterial {...mat} />
          </mesh>
          <mesh position={[0, -len * 0.20, -len * 0.04]} rotation={[0.2, 0, 0]}>
            <boxGeometry args={[len * 0.09, len * 0.08, len * 0.025]} />
            <meshPhysicalMaterial {...nailMat} />
          </mesh>
        </group>
      </group>
    </group>
  );
};

/* ------------------------------------------------------------------ */
/* 头（单独用，近景）                                                  */
/* ------------------------------------------------------------------ */

export interface HumanHeadProps {
  /** 头高（世界单位）。成人约 0.139 × 身高。 */
  h?: number;
  skin?: string;
  hair?: string;
  /** 发型：短发 / 发髻 / 长发 / 光头。只换外轮廓。 */
  hairStyle?: OutfitHair;
  /** 自定义发型轮廓（头局部，颈根 v = 0）。给了就覆盖 `hairStyle`：
   *  轮廓直接放样成一块实心发块，换发型不必新增枚举。 */
  hairOutline?: Pt[];
  /** 脸朝向：'front' 朝 +Z（镜头），'left'/'right' 转向。 */
  facing?: 'front' | 'left' | 'right';
  /** 眼睛闭合 0~1。这一版不画眼，留着参数只为接口不变。 */
  blink?: number;
  seed?: number;
  castShadow?: boolean;
  /** 整具一个颜色（剪影档）：头发不再是另一种材质，跟脸同一个。 */
  mono?: boolean;
}

/** 头与脸。局部坐标：颈根在原点，头顶朝 +Y，脸朝 +Z。
 *
 *  不画眼球、眉、嘴：这一件要的是**轮廓**。正面只看得到鼻梁在中间起的一道脊，
 *  侧面是额头—眉弓—鼻—唇—下颌那条线。 */
export const HumanHead: React.FC<HumanHeadProps> = ({
  h = 0.150,
  skin = '#c08a68',
  hair = '#1d1a18',
  hairStyle = 'short',
  hairOutline,
  facing = 'front',
  seed = 11,
  castShadow = true,
  mono = false,
}) => {
  const skinFull = useSkin(skin, seed);
  const mat = mono ? { color: skin, roughness: 0.56, metalness: 0 } : skinFull;
  const hairMat = mono ? { color: hair, roughness: 0.55 } : { color: hair, ...HAIR };
  const s = h / HEAD_TOP;
  const yaw = facing === 'left' ? 26 : facing === 'right' ? -26 : 0;

  // 剪影档不另做头发壳：整具一个墨色，壳的外沿就是一圈看得见的边，读成
  // 「戴了顶毛线帽」。这一档把颅骨整体放大一档，头发算进轮廓里——放大只加在
  // 宽度和高度上，前后的脸型（鼻、下颌）基本不动。
  // 给了 `hairOutline` 就不放大：发型自己是一块体积，放大颅骨反而会从头发里顶出来。
  const bald = mono && !hairOutline;
  const monoW = bald ? 1.055 : 1;
  const monoV = bald ? 1.03 : 1;
  const monoS = bald ? 1.02 : 1;
  const skull = useMemo(
    () => loftPart({
      poly: HEAD_SKIN,
      rings: 30,
      seg: 34,
      width: (v) => tableAt(HEAD_W, v),
      map: (u, v, x) => [x * monoW * s, v * monoV * s, u * monoS * s],
      ridge: 12,
    }),
    [s, monoW, monoV, monoS],
  );
  const solid = useMemo(
    () => (hairOutline ? hairSolid(hairOutline, s) : null),
    [hairOutline, s],
  );
  const cap = useMemo(
    () => (mono || hairOutline ? null : hairShell(hairStyle, s)),
    [hairStyle, s, mono, hairOutline],
  );

  return (
    <group rotation={[0, yaw * D, 0]}>
      {/* 颈：接缝藏在领口与下颌之间。半径要比躯干那条颈**细**，否则从领口下面
          顶出一圈台阶（整具人用时看得见，单独用头时又被下颌挡住）。 */}
      {/* 0.115/0.132 那一版比身体的颈粗一圈：接上去在喉咙底下留一道环，读成
          「脖子套了个环」。这里只负责补下颌底下的空档，必须比身体的颈**细**。 */}
      <mesh position={[0, -h * 0.07, 0]} castShadow={castShadow}>
        <cylinderGeometry args={[h * 0.090, h * 0.100, h * 0.15, 16]} />
        <meshPhysicalMaterial {...mat} />
      </mesh>
      <mesh geometry={skull} castShadow={castShadow}>
        <meshPhysicalMaterial {...mat} />
      </mesh>
      {cap ? (
        <mesh geometry={cap} castShadow={castShadow}>
          <meshPhysicalMaterial {...hairMat} side={THREE.DoubleSide} />
        </mesh>
      ) : null}
      {solid ? (
        <mesh geometry={solid} castShadow={castShadow}>
          <meshPhysicalMaterial {...hairMat} />
        </mesh>
      ) : null}
      {hairStyle === 'bun' && !mono && !hairOutline ? (
        <mesh position={[0, h * 0.72, -h * 0.36]} castShadow={castShadow}>
          <sphereGeometry args={[h * 0.15, 16, 12]} />
          <meshPhysicalMaterial {...hairMat} />
        </mesh>
      ) : null}
    </group>
  );
};

/* ------------------------------------------------------------------ */
/* 全身                                                                */
/* ------------------------------------------------------------------ */

type ArmGeo = {
  upper: THREE.BufferGeometry;
  fore: THREE.BufferGeometry;
  /** 剪影档：整条胳膊一根管子（上臂/前臂两截的接缝在这一档是一道白环）。 */
  one: THREE.BufferGeometry | null;
  wrist: Vec3;
  palmAng: number;
  side: 1 | -1;
};

type LegGeo = {
  /** 剪影档为 null：那一档的腿已经并进 `skin` 那张皮里了。 */
  leg: THREE.BufferGeometry | null;
  shoe: THREE.BufferGeometry;
};

type FigureGeos = {
  /** 剪影档的整张皮（躯干+四肢+脖子+手）；上色档为 null，用下面几件分开的。 */
  skin: THREE.BufferGeometry | null;
  torso: THREE.BufferGeometry | null;
  garment: THREE.BufferGeometry | null;
  /** 调用方给的一层自定义外衣（斗篷、甲胄、唐装…）：轮廓进、体积出。 */
  shell: THREE.BufferGeometry | null;
  neck: THREE.BufferGeometry | null;
  arms: ArmGeo[];
  legs: LegGeo[];
  headPos: Vec3;
  headRot: number;
  headwear: THREE.BufferGeometry | null;
};

/** 一次把整具人搭出来（全部是纯函数；props 不变就不重建）。 */
const buildFigure = (p: {
  h: number; bw: number; bd: number; bl: number;
  hipV: number; lean: number; headAng: number;
  armNear: [number, number]; armFar: [number, number];
  legNear: [number, number]; legFar: [number, number];
  footNear: number; footFar: number;
  garment: NonNullable<Outfit['garment']>;
  length: NonNullable<Outfit['length']>;
  headwear: NonNullable<Outfit['headwear']>;
  /** 自定义外衣（轮廓 + 半宽表）。与 `garment` 并存，叠在它外面。 */
  shell: ShellSpec | null;
  /** 整具一个颜色：胳膊用一根管子，不留上臂/前臂的接缝。 */
  mono: boolean;
  /** 让最低的那只鞋踩住地面（站/走/指/伸/抱/靠这一档）。 */
  ground: boolean;
  /** 远景人群：距离场的网格粗一档，省一半时间，轮廓不变。 */
  coarse: boolean;
}): FigureGeos => {
  const { h, bw, bd, bl, lean, headAng } = p;
  // 腿是从胯垂下来的，没有 IK：走路那组角度下两只脚都悬在地面上方三四厘米，
  // 影子在地上、脚在半空。把整具人往下放到最低那只鞋踩住地面——「脚底在 y=0」
  // 是这一件的承诺。坐/蹲/跪/卧不走这一步：那几档的接触点是座面、膝、背。
  let drop = 0;
  if (p.ground) {
    const pairs: Array<[[number, number], number, number]> = [
      [p.legFar, p.footFar, -0.014], [p.legNear, p.footNear, 0],
    ];
    let lo = Infinity;
    for (const [ang, foot, uOff] of pairs) {
      const root: Pt = [0.004 + uOff, p.hipV];
      const chain = limbChain(root, ang, B.thigh, B.shin, -1);
      for (const q of shoePoly(chain.tip, foot)) lo = Math.min(lo, q[1]);
    }
    if (Number.isFinite(lo)) drop = Math.max(0, lo);
  }
  const hipV = p.hipV - drop;
  const bodyPt = (q: Pt): Pt => {
    const r = rot(q, -lean);
    return [r[0], hipV + r[1]];
  };
  const bmap = bodyMap(h, hipV, lean);

  // 躯干底不是一块平切在胯上的板：补一个裆点收成楔形，封口缩到两腿之间。
  // （二维那边腿会盖住，三维露出一条水平「裙摆线」，正面一眼就是积木。）
  // 底端也要跟着收成楔形：`TORSO_W` 已经收到 0，侧面轮廓不收就会先撞到侧面那块
  // 平底，裆下仍是一个带棱的台。
  const torsoPoly: Pt[] = [
    ...TORSO.slice(0, 6),
    [0.078, -0.006], [0.060, -0.028], [0.032, -0.045], [0.000, -0.052],
    [-0.040, -0.046], [-0.072, -0.030],
    ...TORSO.slice(7),
  ];
  // 剪影档不用放样体：整具人由下面 `buildSkin` 那张连续的皮出。
  const torso = p.mono ? null : loftPart({
    poly: torsoPoly,
    rings: 36,
    seg: 36,
    width: (v) => tableAt(TORSO_W, v) * bw,
    map: (u, v, x) => bmap(u * bd, v, x),
    smooth: true,
  });

  let garment: THREE.BufferGeometry | null = null;
  if (p.garment !== 'none') {
    const hemV = p.garment === 'robe' || p.garment === 'dress'
      ? 0.10
      : p.garment === 'coat'
        ? 0.28
        : 0.52 - (p.length === 'ankle' ? 0.06 : p.length === 'knee' ? 0.22 : 0.02);
    const lv = hemV - B.hip;
    garment = loftPart({
      // 衣料的厚度跟躯干一起收：衣服比身体深不了多少，收在 0.15H 上下。
      // 衣服比人厚一圈时，正面那团鼓包读成胸肌，而不是「穿了件外套」。
      poly: [
        [-0.072, 0.238], [-0.026, 0.284], [0.028, 0.280], [0.080, 0.232],
        [0.066, 0.120], [0.072, lv], [-0.070, lv], [-0.060, 0.120],
      ],
      rings: 30,
      seg: 32,
      width: (v) => garmentWidth(v) * bw,
      map: (u, v, x) => bmap(u * bd, v, x),
      // 衣料不跟身体走那套折线：肩线、衣摆都是布料自己垂下去的弧。不抹平的话，
      // 肩头和衣摆各是一条横贯全身的亮棱，读成纸板箱。
      smooth: true,
    });
  }

  // 自定义外衣。**两种给法，都不是样式名**：
  //   · 四个数（`hem/top/ease/flare`）：在已认可的身体轮廓上做偏移，
  //     T 恤、夹克、裙、大衣都是同一套数算出来的；
  //   · 一条侧面轮廓 + 半宽表（`outline/width`）：任意外形，调用方现场写。
  // 「换任何样式」靠的是换数据，不是给这一件加分支——所以它永远不会写不完。
  let shell: THREE.BufferGeometry | null = null;
  if (p.shell) {
    const spec = p.shell;
    const out = spec.outline && spec.outline.length >= 3 ? spec.outline : null;
    const wtab = spec.width;
    let bv0 = Infinity;
    let bv1 = -Infinity;
    for (const q of torsoPoly) {
      bv0 = Math.min(bv0, q[1]);
      bv1 = Math.max(bv1, q[1]);
    }
    let sv1 = bv1;
    let sv0 = Math.min(spec.hem ?? -0.04, bv1 - 0.02);
    if (out) {
      sv0 = Infinity;
      sv1 = -Infinity;
      for (const q of out) {
        sv0 = Math.min(sv0, q[1]);
        sv1 = Math.max(sv1, q[1]);
      }
    } else if (typeof spec.top === 'number') {
      sv1 = Math.min(spec.top, bv1);
    }
    const ease = spec.ease ?? 0;
    const flare = spec.flare ?? 0;
    const rings = spec.rings ?? 36;
    const rows: LoftRow[] = [];
    for (let i = 0; i <= rings; i += 1) {
      const v = sv0 + (sv1 - sv0) * (i / rings);
      // 衣服**必须把身体包在里面**：同一件墨色下，身体从布面里顶出来就是一道折，
      // 读成衣服破了。所以每一行都跟身体那一行取并集，再留 1cm 布料余量。
      // 身体那一行只取到胯为止：再往下是躯干收成裆尖的那一段，跟着它走，
      // 裙子会在裆下捏出一个尖——衣服是挂在胯上往下垂的。
      const vb = Math.max(0, Math.min(bv1, v));
      const bs = sliceU(torsoPoly, vb);
      const bc = bs ? (bs[0] + bs[1]) / 2 : 0;
      const bhd = bs ? (bs[1] - bs[0]) / 2 : 0;
      const bwd = tableAt(TORSO_W, vb) * bw;
      // 越往下越外扩：直线略缓，是一块布自己垂下去的弧，不是锥子。
      const tHem = sv1 > sv0 ? (sv1 - v) / (sv1 - sv0) : 0;
      const extra = ease + flare * tHem * tHem;
      let lo: number;
      let hi: number;
      let wRaw: number;
      if (out) {
        const s = sliceU(out, v);
        if (!s) continue;
        const [sc, sh] = smoothProfile(out, v, sv0, sv1);
        lo = Math.min(sc - sh, bc - bhd - 0.010);
        hi = Math.max(sc + sh, bc + bhd + 0.010);
        const thin = Math.min(1, (s[1] - s[0]) / 0.006);
        // 半宽表是绝对身高比例（与轮廓同一坐标系）；没给就按身体那一圈往外放 2cm，
        // 保证「只给轮廓」也是一件穿得上的衣服，而不是一条贴身的线。
        wRaw = (wtab && wtab.length >= 2 ? tableAt(wtab, v) : garmentWidth(v) * bw + 0.02) * thin;
      } else {
        lo = bc - bhd - 0.010 - extra;
        hi = bc + bhd + 0.010 + extra;
        wRaw = bwd + 0.010 + extra;
        if (wtab && wtab.length >= 2) wRaw = Math.max(wRaw, tableAt(wtab, v));
      }
      // 封口那一圈前后也收：轮廓是闭合线，最上面那一行会被封成一个平顶圆盘；
      // 顶着肩宽就是一块「衣架」，白光一扫横贯两肩。最后 3cm 收到身体顶端，
      // 封口缩进脖子与下颌之间，露出来的只有领口。
      const capT = Math.min(1, Math.max(0, (v - (sv1 - 0.030)) / 0.030));
      if (capT > 0) {
        lo += (bc - bhd - 0.008 - lo) * capT;
        hi += (bc + bhd + 0.008 - hi) * capT;
      }
      const neckW = tableAt(TORSO_W, bv1) * bw + 0.010;
      const wTop = wRaw + (neckW - wRaw) * Math.pow(capT, 1.4);
      rows.push({
        v,
        c: (lo + hi) / 2,
        hd: Math.max(0.0015, (hi - lo) / 2),
        w: Math.max(0.002, wTop, bwd + 0.012),
        sc: (lo + hi) / 2,
        sh: (hi - lo) / 2,
      });
    }
    if (rows.length >= 2) {
      shell = loftRows(rows, 34, (u, v, x) => bmap(u * bd, v, x), { smooth: true });
    }
  }

  const neckPts: Pt[] = [bodyPt([0.002, 0.266]), bodyPt([0.010, 0.306]), bodyPt([0.018, 0.340])];
  const neck = p.mono ? null : sweepLimb({
    pts: neckPts,
    radii: [0.030 * bw, 0.027 * bw, 0.025 * bw],
    x: 0,
    h,
    lat: 0.92,
    seg: 22,
  });

  /** 胳膊。剪影档（`mono`）整条**一根管子**：上臂与前臂是两个闭合放样体，
   *  只要它们相交，交线就是一圈看得见的折——上臂收细处与前臂鼓出处必然要穿一次，
   *  穿在哪儿都是一道「袖口」。一个颜色时没有理由分两截，就一次扫到底。
   *
   *  要上色时（皮肤/袖子分开）才分两截：那时的接缝正好是袖口，是应该有的。 */
  const armOf = (ang: [number, number], uOff: number, side: 1 | -1): ArmGeo => {
    const sh = bodyPt([0.012 + uOff, 0.222]);
    const chain = limbChain(sh, ang, B.upperArm, B.forearm);
    const x = side * 0.076 * bw;
    const up = norm([sh[0] - chain.mid[0], sh[1] - chain.mid[1]]);
    const down = norm([chain.tip[0] - chain.mid[0], chain.tip[1] - chain.mid[1]]);
    // 肩头按半球收顶（半径 ~ sqrt(1-(dy/R)²)）：一个点收不出圆顶，正上方会留一块平切。
    const dome = (dy: number): Pt => [sh[0] + up[0] * dy, sh[1] + up[1] * dy];
    // 0.030 那一档的胳膊有 10cm 粗，配收窄后的躯干会读成「练过」；薄肌收到 0.028。
    const R = 0.028 * bl;
    const elbowIn = lerp2(chain.mid, sh, 0.16);
    const elbowPast: Pt = [chain.mid[0] + down[0] * 0.016, chain.mid[1] + down[1] * 0.016];
    return {
      upper: sweepLimb({
        pts: [dome(0.020), dome(0.013), dome(0.006), sh, lerp2(sh, chain.mid, 0.55),
          chain.mid, elbowPast],
        radii: [0.02 * R, 0.74 * R, 0.96 * R, R, 0.86 * R, 0.72 * R, 0.58 * R],
        x, h, lat: 0.95, seg: 20,
      }),
      fore: sweepLimb({
        pts: [elbowIn, chain.mid, lerp2(chain.mid, chain.tip, 0.45), chain.tip],
        radii: [0.66 * R, 0.75 * R, 0.58 * R, 0.40 * R],
        x, h, lat: 0.95, seg: 20,
      }),
      one: p.mono ? sweepLimb({
        pts: [dome(0.020), dome(0.013), dome(0.006), sh,
          lerp2(sh, chain.mid, 0.5), lerp2(sh, chain.mid, 0.82), chain.mid,
          lerp2(chain.mid, chain.tip, 0.28), lerp2(chain.mid, chain.tip, 0.62), chain.tip],
        radii: [0.02 * R, 0.74 * R, 0.96 * R, R, 0.90 * R, 0.78 * R, 0.74 * R,
          0.62 * R, 0.50 * R, 0.38 * R],
        x, h, lat: 0.95, seg: 20,
      }) : null,
      wrist: [x * h, chain.tip[1] * h, chain.tip[0] * h],
      palmAng: -(ang[0] + ang[1]),
      side,
    };
  };

  /** 腿：髋头沿骨链上溯 3cm 埋进骨盆（胯与大腿之间不留平切面），膝下加一个小腿肚。 */
  const legOf = (ang: [number, number], foot: number, uOff: number, side: 1 | -1): LegGeo => {
    const root: Pt = [0.004 + uOff, hipV];
    const chain = limbChain(root, ang, B.thigh, B.shin, -1);
    const x = side * 0.041 * bw;
    // 剪影档的腿在距离场里，中线是 0.047；鞋要对齐自己那条腿，两个数各取各的。
    const sx = side * (p.mono ? 0.047 : 0.041) * bw;
    const up = norm([root[0] - chain.mid[0], root[1] - chain.mid[1]]);
    const hipTop: Pt = [root[0] + up[0] * 0.042, root[1] + up[1] * 0.042];
    const shin = norm([chain.tip[0] - chain.mid[0], chain.tip[1] - chain.mid[1]]);
    // 大腿要「粗得久一点」再收进膝：只在胯与膝之间插一段，正面看才有膝那道折。
    const midThigh = lerp2(root, chain.mid, 0.56);
    const calf: Pt = [
      chain.mid[0] + shin[0] * B.shin * 0.30 + shin[1] * 0.008,
      chain.mid[1] + shin[1] * B.shin * 0.30 - shin[0] * 0.008,
    ];
    return {
      leg: p.mono ? null : sweepLimb({
        // 膝上多插一段：只有「胯→膝」一条直线时，大腿是根直锥子，膝盖那道折
        // 会变成一道台阶。多这一段，收分在膝上先缓下来。
        pts: [hipTop, root, midThigh, lerp2(midThigh, chain.mid, 0.7), chain.mid, calf, chain.tip],
        radii: [0.030 * bl, 0.048 * bl, 0.043 * bl, 0.034 * bl,
          0.029 * bl, 0.027 * bl, 0.017 * bl],
        x,
        h,
        lat: 0.88,
        seg: 18,
      }),
      // 鞋必须落在**这条腿**的 x 上。上一版 `map` 里没有加 x，两只鞋都做在身体
      // 中线上、互相叠在一起——正面看是两腿之间一坨白色的东西，脚不见了。
      shoe: loftPart({
        poly: shoePoly(chain.tip, foot),
        rings: 8,
        seg: 18,
        width: (v) => shoeWidth(v, chain.tip[1]) * bw,
        map: (u, v, xv) => [(sx + xv) * h, v * h, u * h],
      }),
    };
  };

  const hb = bodyPt(HEAD_BASE);

  let headwear: THREE.BufferGeometry | null = null;
  if (p.headwear !== 'none') {
    const poly = headwearPoly(p.headwear);
    if (poly.length >= 3) {
      headwear = loftPart({
        poly,
        rings: 12,
        seg: 22,
        width: (v) => headwearWidth(p.headwear, v),
        map: (u, v, x) => [x * h, v * h, u * h],
      });
    }
  }

  /** 一张皮：躯干、两条胳膊（连手掌）、两条腿、脖子全并进**同一个**距离场，
   *  交界用 smooth-min 融掉。`surface-nets.ts` 顶部写了为什么必须这样。
   *
   *  比例一点没变：躯干还是那条 `torsoPoly` + 那张 `TORSO_W`，四肢还是原来那组
   *  骨链与半径，肩胯的位置也是原来那几个点。变的只有「怎么合并」——上一版是
   *  几个闭合实体互相插进去，交线就是肩头那道棱、腋下那道棱、胯上那道棱，
   *  读起来是拼装玩具；这一版交界是长过去的。 */
  const buildSkin = (): THREE.BufferGeometry => {
    const S = h;
    const lo: Vec3 = [Infinity, Infinity, Infinity];
    const hi: Vec3 = [-Infinity, -Infinity, -Infinity];
    const grow = (q: Vec3, r: number): void => {
      for (let i = 0; i < 3; i += 1) {
        lo[i] = Math.min(lo[i], q[i] - r);
        hi[i] = Math.max(hi[i], q[i] + r);
      }
    };

    // 躯干的行：和 `loftPart` 吃同一条 `torsoPoly`、同一张 `TORSO_W`、同一个抹平
    // 窗口。两份必须一致，否则又会出现「放样版和距离场版不是一个人」。
    let v0 = Infinity;
    let v1 = -Infinity;
    for (const q of torsoPoly) {
      v0 = Math.min(v0, q[1]);
      v1 = Math.max(v1, q[1]);
    }
    const span = v1 - v0;
    const rings = 160;
    const raw: Array<{ c: number; hd: number; w: number }> = [];
    for (let i = 0; i <= rings; i += 1) {
      const v = v0 + span * (i / rings);
      const s = sliceU(torsoPoly, Math.max(v0, Math.min(v1, v)));
      raw.push({
        c: s ? (s[0] + s[1]) / 2 : 0,
        hd: s ? (s[1] - s[0]) / 2 : 0,
        w: tableAt(TORSO_W, v) * bw,
      });
    }
    // 表是按结点给的，结点之间是**折线**：斜率一变，法线就横着跳一下，正面看是
    // 腰胯上几道软横带。140 行、5 点平均都压不住它——5 点平均只把折点抹圆一点点，
    // 斜率的不连续还在。这里换成一条真正的高斯：卷积之后形状是 C∞，表上那些
    // 2~3cm 一格的结点折点全部消失，而胯→腰→胸→肩那十几厘米一档的起伏不动。
    // 抹平半径按身高比例给（1.1cm），换身高不用改数。
    const sigmaV = span * 0.045;
    const dvRow = span / rings;
    const rk = Math.max(2, Math.round((sigmaV * 3) / dvRow));
    const ker: number[] = [];
    for (let k = -rk; k <= rk; k += 1) {
      ker.push(Math.exp(-0.5 * ((k * dvRow) / sigmaV) ** 2));
    }
    const tap = (get: (i: number) => number, i: number): number => {
      let s = 0;
      let ws = 0;
      for (let k = -rk; k <= rk; k += 1) {
        s += get(Math.max(0, Math.min(rings, i + k))) * ker[k + rk];
        ws += ker[k + rk];
      }
      return s / ws;
    };
    const rows: Array<{ v: number; c: number; hd: number; w: number }> = [];
    for (let i = 0; i <= rings; i += 1) {
      rows.push({
        v: v0 + span * (i / rings),
        c: tap((j) => raw[j].c, i),
        hd: Math.max(0.0015, tap((j) => raw[j].hd, i)),
        w: Math.max(0.002, tap((j) => raw[j].w, i)),
      });
    }
    const rn = rows.length;
    const r0 = rows[0].v;
    const r1 = rows[rn - 1].v;
    for (const row of rows) {
      grow(
        [0, (hipV + row.v) * S, row.c * bd * S],
        Math.max(row.w, row.hd * bd) * S + 0.02 * S,
      );
    }

    // 某一高度上取椭圆（前后缘来自侧面轮廓、左右半宽来自表），把点到椭圆的
    // 距离当距离场。行与行之间**插值**：不插的话 44 行会印出 44 圈横向台阶。
    const sdTorso = (x: number, y: number, z: number): number => {
      const dy = y / S - hipV;
      const rr = rot([z / S, dy], lean);
      const uu = rr[0];
      const vv = rr[1];
      const t = ((vv - r0) / (r1 - r0)) * (rn - 1);
      const i0 = t <= 0 ? 0 : t >= rn - 1 ? rn - 2 : Math.floor(t);
      const ft = Math.max(0, Math.min(1, t - i0));
      const ra = rows[i0];
      const rb = rows[i0 + 1];
      // 这一段全用**身高比例**算，最后一步才乘 S 换成米。混着用会把躯干按 S 倍放大
      // （第一版就是这样：24cm 厚的胸量出来 43cm，读成一只桶）。
      const w = ra.w + (rb.w - ra.w) * ft;
      const hd = (ra.hd + (rb.hd - ra.hd) * ft) * bd;
      const cz = (ra.c + (rb.c - ra.c) * ft) * bd;
      const e = Math.hypot(x / S / w, (uu - cz) / hd);
      const d = (e - 1) * Math.min(w, hd) * S;
      const dv = (vv < r0 ? r0 - vv : vv > r1 ? vv - r1 : 0) * S;
      return dv > 0 ? Math.hypot(d, dv) : d;
    };

    /** 一条胳膊：肩→肘→腕→掌。肩头不另做球，把肩点那一截半径放大一点，
     *  再和躯干融合——单独的球加躯干永远是「肩膀上顶着一个球」。 */
    const makeArm = (ang: [number, number], uOff: number, side: 1 | -1): Sdf => {
      const sh = bodyPt([0.012 + uOff, 0.222]);
      const chain = limbChain(sh, ang, B.upperArm, B.forearm);
      // 胳膊略往外挪：上一档整条上臂都埋进躯干侧面，肩到胯是一条直边，正面看
      // 是一块板。挪到 0.104 之后腋下留出一道缝，腰那一带的收分才看得见。
      const x0 = side * 0.112 * bw;
      const R = 0.032 * bl;
      const at = (q: Pt): Vec3 => [x0 * S, q[1] * S, q[0] * S];
      const a = at(sh);
      const b = at(chain.mid);
      const c = at(chain.tip);
      const dir = norm([
        chain.tip[0] - chain.mid[0],
        chain.tip[1] - chain.mid[1],
      ]);
      // 手掌落在比胳膊略靠里一点的位置：真实的手是垂在胯旁的，不是伸出去的。
      const xw = x0 * 0.94;
      const along = (t: number): Vec3 => [
        xw * S,
        (chain.tip[1] + dir[1] * t) * S,
        (chain.tip[0] + dir[0] * t) * S,
      ];
      // 手掌总长 0.064H（1.84m 的人是 12cm）：上一档 0.078 配上下垂的方向，
      // 走路时那片手伸到膝盖上方，像两片桨。收短一格、指节收窄，读成一只手。
      const knuckle = along(0.040);
      const palmEnd = along(0.072);
      const rSh = R * 1.06 * S;
      grow(a, rSh * 1.15);
      grow(b, R * S * 1.1);
      grow(c, R * S * 1.1);
      grow(knuckle, 0.062 * S);
      grow(palmEnd, 0.045 * S);
      // 手不是管子的圆头。真的手在指节那一带最宽、横向只有两三厘米厚：只沿 x
      // 压扁，前后方向留整只手的宽度。侧看是一片手，正看是一条细边；原来那颗
      // 半径 2.6cm 的圆头，正面读出来是一只球。
      const latP = 0.5;
      const xp = xw * S;
      const arm = densify([a, b, c], [rSh, R * 0.74 * S, R * 0.40 * S], 6);
      return (px, py, pz) => {
        const qx = xp + (px - xp) / latP;
        // 肩→肘→腕是一根半径连续的管：肘那种「一圈折」在白光下就是一道横线，
        // 是一堆零件里最显眼的一道。
        const d = sdChain(px, py, pz, arm.pts, arm.rad);
        // 腕→指节→指尖：两段，指节处最宽。全身尺度上手指只是一片，不做五根。
        // 手宽 0.024H（1.84m 的人 8.8cm）、长 0.072H（13cm）。上一档宽 10.6cm
        // 却只有 12cm 长，侧看是一块方板——走路时最显眼的就是那两片。
        const palm = smin(
          sdSeg(qx, py, pz, c, knuckle, 0.018 * S, 0.024 * S),
          sdSeg(qx, py, pz, knuckle, palmEnd, 0.024 * S, 0.013 * S),
          0.010 * S,
        ) * latP;
        // 腕口用一个小融合接上：夹在细腕和宽掌之间，硬 union 会留一圈台阶。
        return smin(d, palm, 0.012 * S);
      };
    };

    /** 一条腿：胯上一截埋进骨盆，膝上多一段让收分缓下来，小腿肚一个鼓包。
     *  这几段之间不再有台阶——融合半径比原来那圈硬交线宽。 */
    const makeLeg = (ang: [number, number], uOff: number, side: 1 | -1): Sdf => {
      const root: Pt = [0.004 + uOff, hipV];
      const chain = limbChain(root, ang, B.thigh, B.shin, -1);
      const x = side * 0.047 * bw;
      const up = norm([root[0] - chain.mid[0], root[1] - chain.mid[1]]);
      const hipTop: Pt = [root[0] + up[0] * 0.042, root[1] + up[1] * 0.042];
      const shin = norm([chain.tip[0] - chain.mid[0], chain.tip[1] - chain.mid[1]]);
      const midThigh = lerp2(root, chain.mid, 0.56);
      const calf: Pt = [
        chain.mid[0] + shin[0] * B.shin * 0.30 + shin[1] * 0.008,
        chain.mid[1] + shin[1] * B.shin * 0.30 - shin[0] * 0.008,
      ];
      const at = (q: Pt): Vec3 => [x * S, q[1] * S, q[0] * S];
      const pts = [
        hipTop, root, midThigh, lerp2(midThigh, chain.mid, 0.7), chain.mid, calf, chain.tip,
      ].map(at);
      const rad = [0.032, 0.050, 0.046, 0.038, 0.031, 0.033, 0.020]
        .map((r) => r * bl * S);
      grow(pts[0], rad[0] * 1.3);
      grow(pts[4], rad[4] * 1.3);
      grow(pts[6], rad[6] * 1.3);
      const lat = 0.88;
      const cx = x * S;
      // 整条腿一根半径连续的管：膝盖两段硬接会留一圈折、smin 接又会在膝上
      // 鼓出一个包（糖葫芦），只有把半径在结点处插值才是同一条腿。
      // 再加密一道，是为了把半径**斜率**的折点也磨掉（膝/踝上那两道横线）。
      const leg = densify(pts, rad, 6);
      return (px, py, pz) => {
        const qx = cx + (px - cx) / lat;
        return sdChain(qx, py, pz, leg.pts, leg.rad) * lat;
      };
    };

    const neckA: Vec3 = [0, neckPts[0][1] * S, neckPts[0][0] * S];
    const neckB: Vec3 = [0, neckPts[2][1] * S, neckPts[2][0] * S];
    grow(neckA, 0.05 * S);
    grow(neckB, 0.04 * S);
    const sdNeck: Sdf = (px, py, pz) =>
      sdSeg(px, py, pz, neckA, neckB, 0.031 * bw * S, 0.024 * bw * S) * 0.92;

    const armFar = makeArm(p.armFar, -0.012, -1);
    const armNear = makeArm(p.armNear, 0.004, 1);
    const legFar = makeLeg(p.legFar, -0.014, -1);
    const legNear = makeLeg(p.legNear, 0, 1);

    // 融合半径：肩/胯 2cm、膝 1.6cm。再大就「糊」——大腿根和另一条腿会长到一起；
    // 再小又回到硬交线。这几个数就是「一件」和「一堆件」之间的那条线。
    const kArm = 0.022 * S;
    const kLeg = 0.020 * S;
    const sdf: Sdf = (x, y, z) => {
      let d = sdTorso(x, y, z);
      d = smin(d, armNear(x, y, z), kArm);
      d = smin(d, armFar(x, y, z), kArm);
      d = smin(d, legNear(x, y, z), kLeg);
      d = smin(d, legFar(x, y, z), kLeg);
      d = smin(d, sdNeck(x, y, z), 0.018 * S);
      return d;
    };

    // 采样盒留一圈余量：贴着盒边的格子凑不出完整体素，边界上会缺一片。
    const pad = (p.coarse ? 0.03 : 0.018) * S;
    const step = (p.coarse ? 0.0090 : 0.0044) * S;
    return surfaceNets(
      sdf,
      [lo[0] - pad, lo[1] - pad, lo[2] - pad],
      [hi[0] + pad, hi[1] + pad, hi[2] + pad],
      step,
    );
  };

  return {
    skin: p.mono ? buildSkin() : null,
    torso,
    garment,
    shell,
    neck,
    arms: p.mono ? [] : [armOf(p.armFar, -0.012, -1), armOf(p.armNear, 0.004, 1)],
    legs: [legOf(p.legFar, p.footFar, -0.014, -1), legOf(p.legNear, p.footNear, 0, 1)],
    headPos: [0, hb[1] * h, hb[0] * h],
    headRot: (lean + headAng) * D,
    headwear,
  };
};

export interface HumanFigureProps {
  /** 身高（世界单位）。站姿头顶到脚底。 */
  h?: number;
  pose?: HumanPoseName | HumanJoints;
  build?: HumanBuild;
  facing?: 'front' | 'left' | 'right' | 'back' | number;
  /** 整具人的一个颜色。参考片那种黑剪影就用默认值。 */
  color?: string;
  /** 皮肤色（可选；不传就跟 `color` 同一个颜色）。 */
  skin?: string;
  /** 衣着颜色（可选；不传就跟 `color` 同一个颜色）。 */
  wardrobe?: {
    top?: string;
    bottom?: string;
    shoes?: string;
    hair?: string;
    sleeve?: 'long' | 'short' | 'none';
  };
  /** 换服：只换外层轮廓（衣摆、袖长、帽子、发型），骨架与比例不动。 */
  outfit?: Outfit;
  /** 手部细节开关：远景人群关掉可以省一半三角面。 */
  detail?: 'full' | 'simple';
  position?: Vec3;
  rotation?: Vec3;
  seed?: number;
  castShadow?: boolean;
  /** 脚下一块软接触阴影（没有 AO 时靠它读「落地」）。 */
  contactShadow?: boolean;
}

/** 一具人影式三维人。局部坐标：脚底在 y=0，脸朝 +Z，整具一个颜色（默认近黑）。 */
export const HumanFigure: React.FC<HumanFigureProps> = ({
  h = 1.75,
  pose = 'stand',
  build = 'regular',
  facing = 'front',
  color = '#0b0b0d',
  skin,
  wardrobe,
  outfit,
  detail = 'full',
  position = [0, 0, 0],
  rotation = [0, 0, 0],
  seed = 1,
  castShadow = true,
  contactShadow = true,
}) => {
  const j = useMemo<Joints>(
    () => (typeof pose === 'string'
      ? (pose === 'walk' ? WALK_JOINTS : (POSES[pose] ?? STAND))
      : jointsFrom(pose)),
    [pose],
  );
  const b = BUILDS[build] || BUILDS.regular;

  const garment = outfit?.garment ?? 'none';
  const sleeve = wardrobe?.sleeve ?? outfit?.sleeve ?? 'long';
  const length = outfit?.length ?? 'hip';
  const headwear = outfit?.headwear ?? 'none';
  const hairStyle = outfit?.hair ?? 'short';
  // 自定义外衣/发型按**内容**认：调用方每帧新建一个 outfit 字面量，按对象身份认
  // 会让整具人的距离场每帧重算（远景人群直接卡死），按内容就不会。
  const shell = outfit?.shell ?? null;
  const shellKey = shell ? JSON.stringify(shell) : '';
  const hairKey = outfit?.hairOutline ? JSON.stringify(outfit.hairOutline) : '';
  // 头发按内容解析一次：同一份轮廓每帧拿到同一个数组，`HumanHead` 的 `useMemo`
  // 才认得出来（否则每帧重建一次放样体，远景人群白烧）。
  const hairOutline = useMemo<Pt[] | null>(
    () => (hairKey ? (JSON.parse(hairKey) as Pt[]) : null),
    [hairKey],
  );

  const cSkin = skin || color;
  const cTop = wardrobe?.top || color;
  const cBottom = wardrobe?.bottom || color;
  const cShoe = wardrobe?.shoes || color;
  const cHair = wardrobe?.hair || color;

  // 只给了 `color`（没给皮肤/衣着色）＝剪影档：整具人**一个材质、一个颜色**。
  // 走 useSkin/useCloth 会带 sheen/clearcoat，黑也会被渲成灰、头发带一圈棕光，
  // 参考片那种「整个人是一块黑、只有边上一道白」就不成立了。
  const mono = !skin && !wardrobe;
  const ink = { color, roughness: 0.56, metalness: 0.0 };
  const skinM = useSkin(cSkin, seed);
  const topM = useCloth(cTop, seed + 2);
  const bottomM = useCloth(cBottom, seed + 4);
  const shoeM = useCloth(cShoe, seed + 6);
  const skinMat = mono ? ink : skinM;
  const topMat = mono ? ink : topM;
  const bottomMat = mono ? ink : bottomM;
  const shoeMat = mono ? ink : shoeM;

  const yaw = typeof facing === 'number'
    ? facing
    : facing === 'left' ? 90 : facing === 'right' ? -90 : facing === 'back' ? 180 : 0;

  const hipV = B.hip + j.pelvis + j.bob;

  // 站/走/指/伸/抱/靠：脚是接触点，最低那只鞋要落到地面；坐、蹲、跪、卧的接触点
  // 是座面/膝/背，不能让脚去找地。关节覆盖（逐帧走路）按站姿处理。
  const ground = typeof pose !== 'string'
    || !['sit', 'sitGround', 'crouch', 'kneel', 'lie'].includes(pose);

  const geos = useMemo(
    () => buildFigure({
      h,
      bw: b.w,
      bd: b.d,
      bl: b.limb,
      hipV,
      lean: j.lean,
      headAng: j.head,
      armNear: j.armNear,
      armFar: j.armFar,
      legNear: j.legNear,
      legFar: j.legFar,
      footNear: j.footNear,
      footFar: j.footFar,
      garment,
      length,
      headwear,
      shell,
      mono,
      ground,
      coarse: detail === 'simple',
    }),
    // 逐项列出来：调用方每帧新建一个 outfit/wardrobe 对象也不会重建几何。
    // `shell` 按内容（`shellKey`）认，不按对象身份。
    [h, b.w, b.d, b.limb, hipV, j, j.lean, j.head, j.armNear, j.armFar,
      j.legNear, j.legFar, j.footNear, j.footFar, garment, length, headwear, shellKey, mono,
      ground, detail],
  );

  const upperMat = sleeve === 'none' ? skinMat : topMat;
  const foreMat = sleeve === 'long' ? topMat : skinMat;

  return (
    <group position={position} rotation={rotation}>
      <group rotation={[0, yaw * D, 0]}>
        {geos.skin ? (
          <mesh geometry={geos.skin} castShadow={castShadow}>
            <meshPhysicalMaterial {...skinMat} />
          </mesh>
        ) : null}
        {geos.torso ? (
          <mesh geometry={geos.torso} castShadow={castShadow}>
            <meshPhysicalMaterial {...topMat} />
          </mesh>
        ) : null}
        {geos.neck ? (
          <mesh geometry={geos.neck} castShadow={castShadow}>
            <meshPhysicalMaterial {...skinMat} />
          </mesh>
        ) : null}
        {geos.garment ? (
          <mesh geometry={geos.garment} castShadow={castShadow}>
            <meshPhysicalMaterial {...topMat} />
          </mesh>
        ) : null}
        {geos.shell ? (
          <mesh geometry={geos.shell} castShadow={castShadow}>
            <meshPhysicalMaterial {...topMat} />
          </mesh>
        ) : null}

        {geos.arms.map((a) => (
          <group key={`arm${a.side}`}>
            {a.one ? (
              <mesh geometry={a.one} castShadow={castShadow}>
                <meshPhysicalMaterial {...upperMat} />
              </mesh>
            ) : (
              <>
                <mesh geometry={a.upper} castShadow={castShadow}>
                  <meshPhysicalMaterial {...upperMat} />
                </mesh>
                <mesh geometry={a.fore} castShadow={castShadow}>
                  <meshPhysicalMaterial {...foreMat} />
                </mesh>
              </>
            )}
            <group position={a.wrist} rotation={[a.palmAng * D, -a.side * Math.PI * 0.5, 0]}>
              <HumanHand
                len={0.100 * h}
                side={a.side}
                curl={0.34}
                skin={cSkin}
                seed={seed + 3}
                mono={mono}
                castShadow={castShadow}
              />
            </group>
          </group>
        ))}

        {geos.legs.map((l, i) => (
          <group key={`leg${i}`}>
            {l.leg ? (
              <mesh geometry={l.leg} castShadow={castShadow}>
                <meshPhysicalMaterial {...bottomMat} />
              </mesh>
            ) : null}
            <mesh geometry={l.shoe} castShadow={castShadow}>
              <meshPhysicalMaterial {...shoeMat} />
            </mesh>
          </group>
        ))}

        <group position={geos.headPos} rotation={[geos.headRot, 0, 0]}>
          <HumanHead
            h={HEAD_TOP * h}
            skin={cSkin}
            hair={cHair}
            hairStyle={hairStyle}
            hairOutline={hairOutline ?? undefined}
            facing="front"
            seed={seed + 1}
            mono={mono}
            castShadow={castShadow}
          />
          {geos.headwear ? (
            <mesh geometry={geos.headwear} castShadow={castShadow}>
              <meshPhysicalMaterial color={cHair} roughness={0.78} sheen={mono ? 0 : 0.3}
                sheenColor="#6b5a4a" sheenRoughness={0.8} side={THREE.DoubleSide} />
            </mesh>
          ) : null}
        </group>
      </group>
      {contactShadow ? (
        <ContactShadow w={h * 0.62} d={h * 0.34} y={0.002} opacity={0.5} seed={seed + 9} />
      ) : null}
    </group>
  );
};

/* ------------------------------------------------------------------ */
/* 接触阴影                                                            */
/* ------------------------------------------------------------------ */

const SHADOW_TEX = new Map<string, THREE.Texture>();

const softShadow = (seed: number): THREE.Texture => {
  const key = `cs${seed}`;
  const hit = SHADOW_TEX.get(key);
  if (hit) return hit;
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (ctx) {
    const g = ctx.createRadialGradient(size / 2, size / 2, size * 0.05, size / 2, size / 2, size * 0.5);
    g.addColorStop(0, 'rgba(0,0,0,0.85)');
    g.addColorStop(0.45, 'rgba(0,0,0,0.42)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
  }
  const tex = new THREE.CanvasTexture(canvas);
  SHADOW_TEX.set(key, tex);
  return tex;
};

export interface ContactShadowProps {
  w?: number;
  d?: number;
  y?: number;
  opacity?: number;
  seed?: number;
  color?: string;
}

/** 一块贴在表面上的软阴影（径向渐变）。没有 AO 时，它负责「东西是落在地上的」。 */
export const ContactShadow: React.FC<ContactShadowProps> = ({
  w = 1,
  d = 0.6,
  y = 0.002,
  opacity = 0.5,
  seed = 1,
  color = '#000000',
}) => {
  const tex = useMemo(() => softShadow(seed), [seed]);
  return (
    <mesh position={[0, y, 0]} rotation={[-Math.PI / 2, 0, 0]} renderOrder={1}>
      <planeGeometry args={[w, d]} />
      <meshBasicMaterial map={tex} transparent opacity={opacity} depthWrite={false} color={color} />
    </mesh>
  );
};

export default HumanFigure;

// TEMP-DEBUG
export const __buildFigure = buildFigure;
