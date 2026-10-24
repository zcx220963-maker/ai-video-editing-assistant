/**
 * `@dg/lib/style/human-sketch` —— 2.5D 人形（剪影 / 上色 / 线描），摆在屏幕层。
 *
 * 为什么不是三维的 `HumanFigure`（2026-10-22，用户第二次打回）：
 * 用户拿参考片对照，原话是「就是 2.5d 那种素描人形，要写实，你这种圆头和其他
 * 地方和真人根本不像」「连人影都这么写实」。参考片里的人没有五官、没有材质、
 * 甚至没有明暗——它靠的是**外轮廓**：额→眉弓→鼻梁→鼻尖→唇→下颌的转折、肩的
 * 斜度、腰的收、四肢的收分、脚有鞋的形。三维球面把这一切抹平了，所以读起来是
 * 玩偶；剪影只要轮廓对，一眼就是真人。
 *
 * 这一件只做一件事：**用一条对的外轮廓画一个人**。轮廓比例取自
 * `@dg/lib/walk` 的 `WalkingFigure`（1/7.5 头身，2026-10-20 已对着参考片调过），
 * 但那件只会走；这里补上「摆好不动的姿势」：站、坐、坐地、蹲、跪、指、伸、抱、靠，
 * 外加会走的 `pose='walk'`（同一组步态常数）。同镜可以混用：远的人走、近的人站。
 *
 * 三档画法（`render`）：
 *
 *   · `silhouette`（缺省）：单色实心剪影，参考片那一档。
 *   · `wash`：剪影 + 一层彩色（皮肤/上衣/裤子），像素描上色；远侧肢体压暗一档，
 *     2.5D 的前后关系靠这一层读出来。
 *   · `line`：只画轮廓线，不上色。
 *
 * 坐标与 `WalkingFigure` 一致：`x` 脚底中点的像素横坐标、`y` 脚底那条线、
 * `h` 身高像素。所有比例按 `h` 缩放，不写死像素。
 *
 * 纪律：不用 `Math.random`；走动只读 `useCurrentFrame`，同一帧两次渲逐像素相同。
 */
import React from 'react';
import { useCurrentFrame, useVideoConfig } from 'remotion';
import {
  rot, lerp2, norm, B, TORSO, HEAD, HAIR_SHORT, HAIR_BUN, HAIR_LONG,
  STAND, POSES, gait,
} from './human-shape';
import type { Pt, PoseName, Joints, Outfit } from './human-shape';

/** 2.5D 那一档的姿势名，与三维共用同一张表（`human-shape`）。 */
export type SketchPose = PoseName;

export type SketchRender = 'silhouette' | 'wash' | 'line';

export type SketchOutfit = Outfit;

export interface HumanSketchProps {
  /** 脚底中点的 x（成片像素）。缺省画面正中。 */
  x?: number;
  /** 脚底那条线的 y（成片像素）。缺省画面高 88%。 */
  y?: number;
  /** 身高（成片像素）。缺省画面高 46%。 */
  h?: number;
  pose?: SketchPose;
  /** 朝向：1/'right'（缺省）或 -1/'left'。 */
  facing?: 1 | -1 | 'left' | 'right';
  /** `pose='walk'` 时才用：一个步态周期几秒、第 0 帧的相位。 */
  strideSec?: number;
  phase?: number;
  /** `pose='walk'` 时传 false＝站着不动。 */
  walking?: boolean;
  render?: SketchRender;
  /** 剪影色 / 墨线色。 */
  ink?: string;
  /** `render='wash'` 时的皮肤与衣着色。 */
  skin?: string;
  top?: string;
  bottom?: string;
  hair?: string;
  shoes?: string;
  /** 换装：只换外层轮廓，骨架与比例不动。 */
  outfit?: SketchOutfit;
  /** 脚下一块软阴影。缺省画。 */
  shadow?: boolean;
  /** svg 尺寸，缺省整帧。 */
  width?: number;
  heightPx?: number;
  opacity?: number;
  style?: React.CSSProperties;
}

/* ------------------------------------------------------------------ */
/* SVG 路径小工具（数表与姿势在 `human-shape`，这里只管画）            */
/* ------------------------------------------------------------------ */

/** 闭合折线 → 平滑路径（Catmull-Rom 转三次贝塞尔）。轮廓的有机感靠它。 */
const smoothLoop = (pts: Pt[], tension = 1): string => {
  const n = pts.length;
  if (n < 3) return '';
  let d = `M ${pts[0][0].toFixed(4)} ${pts[0][1].toFixed(4)}`;
  for (let i = 0; i < n; i += 1) {
    const p0 = pts[(i - 1 + n) % n];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % n];
    const p3 = pts[(i + 2) % n];
    const c1x = p1[0] + ((p2[0] - p0[0]) / 6) * tension;
    const c1y = p1[1] + ((p2[1] - p0[1]) / 6) * tension;
    const c2x = p2[0] - ((p3[0] - p1[0]) / 6) * tension;
    const c2y = p2[1] - ((p3[1] - p1[1]) / 6) * tension;
    d += ` C ${c1x.toFixed(4)} ${c1y.toFixed(4)} ${c2x.toFixed(4)} ${c2y.toFixed(4)} ${p2[0].toFixed(4)} ${p2[1].toFixed(4)}`;
  }
  return `${d} Z`;
};

/** 一段有收分的肢体轮廓：A→B→C，半径 rA/rB/rC。返回闭合路径。 */
const limbPath = (a: Pt, b: Pt, c: Pt, ra: number, rb: number, rc: number): string => {
  const left: Pt[] = [];
  const right: Pt[] = [];
  const segs: Array<[Pt, Pt, number, number]> = [[a, b, ra, rb], [b, c, rb, rc]];
  for (const [p, q, rp, rq] of segs) {
    const d = norm([q[0] - p[0], q[1] - p[1]]);
    const n: Pt = [-d[1], d[0]];
    const steps = 5;
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      const m = lerp2(p, q, t);
      const r = rp + (rq - rp) * t;
      left.push([m[0] + n[0] * r, m[1] + n[1] * r]);
      right.push([m[0] - n[0] * r, m[1] - n[1] * r]);
    }
  }
  return smoothLoop([...left, ...right.reverse()], 0.75);
};

/** 把颜色压暗一档（远侧肢体）。只认 #rrggbb，其余原样返回。 */
const darken = (c: string): string => {
  const m = /^#([0-9a-f]{6})$/i.exec(c.trim());
  if (!m) return c;
  const v = parseInt(m[1], 16);
  const r = Math.round(((v >> 16) & 255) * 0.74);
  const g = Math.round(((v >> 8) & 255) * 0.74);
  const b = Math.round((v & 255) * 0.74);
  return `#${((r << 16) | (g << 8) | b).toString(16).padStart(6, '0')}`;
};

/* ------------------------------------------------------------------ */
/* 组件                                                                */
/* ------------------------------------------------------------------ */

export const HumanSketch: React.FC<HumanSketchProps> = ({
  x,
  y,
  h,
  pose = 'stand',
  facing = 1,
  strideSec = 1,
  phase = 0,
  walking = true,
  render = 'silhouette',
  ink = '#141016',
  skin = '#c08a5e',
  top = '#5b6470',
  bottom = '#2b3038',
  hair = '#1d1a18',
  shoes = '#141416',
  outfit,
  shadow = true,
  width,
  heightPx,
  opacity = 1,
  style,
}) => {
  const video = useVideoConfig();
  const frame = useCurrentFrame();
  const W = width ?? video.width;
  const H = heightPx ?? video.height;
  const px = x ?? W * 0.5;
  const py = y ?? H * 0.88;
  const height = h ?? H * 0.46;
  const dir: 1 | -1 = facing === -1 || facing === 'left' ? -1 : 1;

  const cycle = Math.max(0.4, Math.min(3, strideSec));
  const fps = video.fps || 30;
  const theta = 2 * Math.PI * (frame / (fps * cycle) + phase);
  const joints: Joints = pose === 'walk'
    ? (walking ? gait(theta) : { ...STAND })
    : POSES[pose] ?? STAND;

  /** 局部坐标（u 朝前、v 离地，单位＝身高）→ 成片像素。 */
  const p = (u: number, v: number): Pt => [
    px + dir * u * height,
    py - (v + joints.bob) * height,
  ];

  const hipV = B.hip + joints.pelvis;
  const pelvis: Pt = p(0, hipV);

  /** 盆骨局部（相对盆骨、u 朝前）→ 像素，随躯干前倾旋转。 */
  const body = (q: Pt): Pt => {
    const r = rot(q, -joints.lean);
    return [pelvis[0] + dir * r[0] * height, pelvis[1] - r[1] * height];
  };

  const torso = smoothLoop(TORSO.map(body), 0.9);

  // 颈：肩上到颈根（WalkingFigure 用同样一段补肩与头之间的空）
  // 起点压在肩线以下（0.278 ≈ 0.798H），否则肩与颈之间会露出一条底。
  const neck = limbPath(
    body([0.002, 0.272]), body([0.010, 0.306]), body([0.018, 0.338]),
    0.034 * height, 0.030 * height, 0.025 * height,
  );

  // 头：颈根在躯干局部 (0.010, 0.325)
  const headBase: Pt = [0.010, 0.325];
  const headLocal = (q: Pt): Pt => {
    const r = rot(q, -joints.head);
    return body([headBase[0] + r[0], headBase[1] + r[1]]);
  };
  const head = smoothLoop(HEAD.map(headLocal), 0.85);

  const hairKind = outfit?.hair ?? 'short';
  const hairShape = hairKind === 'bald'
    ? ''
    : smoothLoop((hairKind === 'bun' ? HAIR_BUN : hairKind === 'long' ? HAIR_LONG : HAIR_SHORT)
      .map(headLocal), 0.85);

  // 四肢关节（成片像素）
  const armOf = (ang: [number, number], back: boolean) => {
    const [up, bend] = ang;
    const sh: Pt = body([0.015 + (back ? -0.012 : 0.004), 0.255]);
    const d1 = rot([0, -B.upperArm], up);
    const elbow: Pt = [sh[0] + dir * d1[0] * height, sh[1] - d1[1] * height];
    const d2 = rot([0, -B.forearm], up + bend);
    const wrist: Pt = [elbow[0] + dir * d2[0] * height, elbow[1] - d2[1] * height];
    return { sh, elbow, wrist };
  };

  const legOf = (ang: [number, number], foot: number, back: boolean) => {
    const [hipA, knee] = ang;
    const hp: Pt = p(0.004 + (back ? -0.014 : 0.0), hipV);
    const d1 = rot([0, -B.thigh], hipA);
    const kneeP: Pt = [hp[0] + dir * d1[0] * height, hp[1] - d1[1] * height];
    const d2 = rot([0, -B.shin], hipA - knee);
    const ankle: Pt = [kneeP[0] + dir * d2[0] * height, kneeP[1] - d2[1] * height];
    return { hp, kneeP, ankle, foot };
  };

  const armFar = armOf(joints.armFar, true);
  const armNear = armOf(joints.armNear, false);
  const legFar = legOf(joints.legFar, joints.footFar, true);
  const legNear = legOf(joints.legNear, joints.footNear, false);

  const legShape = (l: ReturnType<typeof legOf>) =>
    limbPath(l.hp, l.kneeP, l.ankle, 0.054 * height, 0.035 * height, 0.023 * height);

  /** 手：一块圆掌，沿前臂方向伸出。 */
  const handShape = (a: ReturnType<typeof armOf>, ang: [number, number]) => {
    const d = norm(rot([0, -1], ang[0] + ang[1]));
    const palm = 0.029 * height;
    const width = 0.021 * height;
    const n: Pt = [-d[1], d[0]];
    const c = a.wrist;
    const f = (k: number, s: number): Pt => [
      c[0] + dir * d[0] * palm * k + n[0] * width * s,
      c[1] - d[1] * palm * k + n[1] * width * s,
    ];
    return smoothLoop([f(0, 1), f(0.9, 1.1), f(1.75, 0), f(0.9, -1.1), f(0, -1)], 0.9);
  };

  /** 鞋：从踝到脚尖的楔形，脚跟比脚尖略窄。 */
  const shoeShape = (l: ReturnType<typeof legOf>) => {
    const f = rot([1, 0], -l.foot);
    const tip: Pt = [l.ankle[0] + dir * f[0] * B.foot * height, l.ankle[1] - f[1] * B.foot * height];
    const sole = 0.016 * height;
    const topH = 0.030 * height;
    const heel: Pt = [l.ankle[0] - dir * 0.020 * height, l.ankle[1]];
    return smoothLoop([
      [heel[0], heel[1] - sole],
      [tip[0], tip[1] - sole * 0.55],
      [tip[0] + dir * 0.010 * height, tip[1] + topH * 0.45],
      [l.ankle[0] + dir * 0.012 * height, l.ankle[1] + topH * 0.55],
      [heel[0] + dir * 0.004 * height, heel[1] + sole * 0.4],
    ], 0.8);
  };

  // 衣服：只换外层轮廓，骨架不动
  const garment = outfit?.garment ?? 'none';
  const lenV = 0.52 - (outfit?.length === 'ankle' ? 0.06 : outfit?.length === 'knee' ? 0.22 : 0.02);
  const hemV = garment === 'robe' || garment === 'dress' ? 0.10 : garment === 'coat' ? 0.28 : lenV;
  const garmentShape = garment === 'none' ? '' : smoothLoop([
    body([-0.098, 0.238]), body([-0.030, 0.284]), body([0.032, 0.280]),
    body([0.108, 0.232]), body([0.086, 0.120]),
    body([0.096, hemV - B.hip]), body([-0.096, hemV - B.hip]),
    body([-0.080, 0.120]),
  ], 0.9);

  const headwear = outfit?.headwear ?? 'none';
  const headwearShape = (() => {
    switch (headwear) {
      case 'straw':
        return smoothLoop([
          headLocal([-0.130, 0.106]), headLocal([-0.050, 0.124]), headLocal([0.010, 0.152]),
          headLocal([0.056, 0.130]), headLocal([0.126, 0.100]), headLocal([0.060, 0.098]),
          headLocal([-0.060, 0.100]),
        ], 0.9);
      case 'brim':
        return smoothLoop([
          headLocal([-0.110, 0.112]), headLocal([-0.044, 0.146]), headLocal([0.014, 0.150]),
          headLocal([0.050, 0.130]), headLocal([0.088, 0.116]), headLocal([0.020, 0.114]),
          headLocal([-0.050, 0.110]),
        ], 0.85);
      case 'hood':
        return smoothLoop([
          headLocal([-0.078, 0.020]), headLocal([-0.070, 0.108]), headLocal([-0.010, 0.156]),
          headLocal([0.048, 0.134]), headLocal([0.060, 0.086]), headLocal([0.040, 0.040]),
          headLocal([0.006, 0.062]), headLocal([-0.030, 0.030]),
        ], 0.9);
      case 'cap':
        return smoothLoop([
          headLocal([-0.052, 0.108]), headLocal([-0.032, 0.148]), headLocal([0.014, 0.154]),
          headLocal([0.048, 0.132]), headLocal([0.098, 0.124]), headLocal([0.096, 0.116]),
          headLocal([0.024, 0.118]), headLocal([-0.026, 0.106]),
        ], 0.85);
      default:
        return '';
    }
  })();

  const colored = render === 'wash';
  const lineOnly = render === 'line';
  const stroke = lineOnly ? ink : undefined;
  const sw = lineOnly ? Math.max(1, height * 0.005) : 1;
  const fillOf = (c: string) => (lineOnly ? 'none' : c);
  const topFill = colored ? top : ink;
  const bottomFill = colored ? bottom : ink;
  const skinFill = colored ? skin : ink;
  const hairFill = colored ? hair : ink;
  const shoeFill = colored ? shoes : ink;
  const farTop = colored ? darken(topFill) : ink;
  const farBottom = colored ? darken(bottomFill) : ink;
  const farSkin = colored ? darken(skin) : ink;
  const sleeve = outfit?.sleeve ?? 'long';

  const drawArm = (
    a: ReturnType<typeof armOf>,
    ang: [number, number],
    sleeveColor: string,
    skinColor: string,
  ) => (
    <>
      <path
        d={limbPath(a.sh, lerp2(a.sh, a.elbow, 0.55), a.elbow,
          0.033 * height, 0.030 * height, 0.026 * height)}
        fill={fillOf(sleeve === 'none' ? skinColor : sleeveColor)}
      />
      {sleeve === 'long' ? (
        <path
          d={limbPath(a.elbow, lerp2(a.elbow, a.wrist, 0.55), lerp2(a.elbow, a.wrist, 0.94),
            0.026 * height, 0.022 * height, 0.017 * height)}
          fill={fillOf(sleeveColor)}
        />
      ) : (
        <path
          d={limbPath(a.elbow, lerp2(a.elbow, a.wrist, 0.5), a.wrist,
            0.025 * height, 0.020 * height, 0.016 * height)}
          fill={fillOf(skinColor)}
        />
      )}
      <path d={handShape(a, ang)} fill={fillOf(skinColor)} />
    </>
  );

  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      style={{ position: 'absolute', left: 0, top: 0, overflow: 'visible', opacity, ...style }}
    >
      {shadow ? (
        <ellipse
          cx={px}
          cy={py + 2}
          rx={height * (pose === 'sit' || pose === 'crouch' || pose === 'kneel' ? 0.20 : 0.17)}
          ry={height * 0.018}
          fill={ink}
          opacity={0.26}
        />
      ) : null}

      <g stroke={stroke} strokeWidth={sw} strokeLinejoin="round">
        {/* 远侧：先画，上色时压暗一档。线描档不画——没有填色挡着，远侧的线会
            横穿躯干，读成一堆多余的轮廓。 */}
        {lineOnly ? null : (
          <>
            <path d={legShape(legFar)} fill={fillOf(farBottom)} />
            <path d={shoeShape(legFar)} fill={fillOf(colored ? darken(shoes) : ink)} />
            {drawArm(armFar, joints.armFar, farTop, farSkin)}
          </>
        )}

        {/* 近侧腿 */}
        <path d={legShape(legNear)} fill={fillOf(bottomFill)} />
        <path d={shoeShape(legNear)} fill={fillOf(shoeFill)} />

        {/* 躯干、颈、衣 */}
        <path d={torso} fill={fillOf(topFill)} />
        <path d={neck} fill={fillOf(skinFill)} />
        {garmentShape ? <path d={garmentShape} fill={fillOf(topFill)} /> : null}

        {/* 近侧臂 */}
        {drawArm(armNear, joints.armNear, topFill, skinFill)}

        {/* 头、发、帽 */}
        <path d={head} fill={fillOf(skinFill)} />
        {hairShape ? <path d={hairShape} fill={fillOf(hairFill)} /> : null}
        {headwearShape ? <path d={headwearShape} fill={fillOf(hairFill)} /> : null}
      </g>
    </svg>
  );
};

// 让 `SurrealWorld` 把它挪到屏幕层（与 `<svg>`/`<div>` 同一档），而不是塞进 three 场景。
(HumanSketch as unknown as { domLayer?: boolean }).domLayer = true;

export default HumanSketch;
