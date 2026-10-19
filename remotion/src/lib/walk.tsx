/**
 * `<WalkingFigure/>` —— **同一个主角**在整片里走过去的那个身影（2026-10-19）。
 *
 * 为什么单独一件：参考片的力量不在某一镜画得多细，而在**同一个人**从洞穴走到蒸汽
 * 时代、再走到火箭与手机时代——身高、步态、朝向、脚底那条线始终是同一个，只有背景
 * 与身上的衣服在换。二十几镜各自独立写码时，这件事靠提示词是守不住的：每个人都会
 * 画一个「大概这么高」的人，切点一过，身高、步幅与朝向就漂了。
 *
 * 这一件把「走路」写成一份**确定性的步态**：读 `useCurrentFrame()`，两条腿与两条臂
 * 做反相摆动，身体随步频上下起伏，披风/衣摆跟着摆。所有镜头都用同一件、同一组参数，
 * 切点处的身姿就自然对得上。
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
 *                wardrobe="cloak" facing="right" strideSec={1.0}
 *                rim="#ffb15c" />
 * ```
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

/** 主角身上那件衣服：按时代换，轮廓不变。 */
export type WalkWardrobe = 'plain' | 'cloak' | 'cap' | 'coat' | 'hoodie';

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
  /** 衣服。按 `spec.protagonist.wardrobe` 那条演进线挑：cloak 兽皮/斗篷、cap 工装帽、
   *  coat 长外套、hoodie 连帽衫（手里有手机）、plain 无标记 */
  wardrobe?: WalkWardrobe;
  /** 画法方向。缺省 silhouette（剪影）；要上色/体积就传 flat/lit/line */
  render?: WalkRender;
  /** 主体固有色（剪影色 / 衣服主色）。缺省整片那档的墨色 */
  fill?: string;
  /** 露出皮肤的部分（头、手）。只对 flat/lit 生效；缺省给一档暖肤色的代码色，
   *  要跟这一镜的色板走就显式传。silhouette/line 不画皮肤 */
  skin?: string;
  /** 头发/帽子下的发色。只对 flat/lit 生效。缺省比 `fill` 深一档 */
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

/** 闭合平滑路径（二次贝塞尔穿各点中点）——披风、衣摆用它，避免折线感。 */
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
    wardrobe = 'plain',
    render = 'silhouette',
    rimSide = 'front',
    shadow = true,
  } = props;

  const film = resolveFilmStyle(getInputProps());
  const colored = render === 'flat' || render === 'lit';
  const ink = props.fill || film.ink || TOKENS.ink;
  // 上色/受光两档不能拿「墨色」当固有色：墨色是给剪影档准备的，做成实心主体就是
  // 一团黑，明暗两档根本没地方落。没显式给 `fill` 时把它往白里提一档（仍是同一
  // 色相、仍从整片墨色派生），体积才画得出来。
  const fill = props.fill || (colored ? shade(ink, 0.55) : ink);
  const rim = props.rim || film.accent || TOKENS.accent;
  // 披风/长外套比身体浅一档——同一色相的实色，不是透明度（见上 `shade`）。
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

  // ------------------------------------------------------------------ 腿
  const legOf = (offset: number): { knee: Pt; ankle: Pt; toe: Pt } => {
    const t = theta + offset;
    const hipAng = 0.44 * gait * Math.sin(t);
    const swing = Math.max(0, Math.sin(t));
    const kneeBend = gait * (0.06 + 0.95 * swing * swing);
    const shin = hipAng - kneeBend;
    const kneeU = Math.sin(hipAng) * 0.25;
    const kneeV = 0.5 - Math.cos(hipAng) * 0.25;
    const ankleU = kneeU + Math.sin(shin) * 0.25;
    const ankleV = kneeV - Math.cos(shin) * 0.25;
    return {
      knee: pt(kneeU, kneeV),
      ankle: pt(ankleU, ankleV),
      toe: pt(ankleU + 0.075, Math.max(0.012, ankleV)),
    };
  };

  // ------------------------------------------------------------------ 臂
  const armOf = (offset: number): { elbow: Pt; hand: Pt; handUV: Pt } => {
    const t = theta + offset;
    // 同侧手臂与同侧腿反相（左腿在前时左臂在后）。
    const upperAng = -0.5 * gait * Math.sin(t) + 0.06;
    const bend = 0.12 + gait * 0.30 * Math.max(0, Math.sin(t + Math.PI / 2));
    const elbowU = Math.sin(upperAng) * 0.17;
    const elbowV = 0.8 - Math.cos(upperAng) * 0.17;
    const foreAng = upperAng + bend;
    const handU = elbowU + Math.sin(foreAng) * 0.16;
    const handV = elbowV - Math.cos(foreAng) * 0.16;
    return { elbow: pt(elbowU, elbowV), hand: pt(handU, handV), handUV: [handU, handV] };
  };

  const legBack = legOf(0);
  const legFront = legOf(Math.PI);
  const armBack = armOf(Math.PI);
  const armFront = armOf(0);

  // ------------------------------------------------------------------ 躯干
  const torso = poly([
    pt(-0.094, 0.81), pt(0.112, 0.795), pt(0.100, 0.70),
    pt(0.080, 0.56), pt(0.070, 0.495), pt(-0.070, 0.495),
    pt(-0.080, 0.56), pt(-0.092, 0.70),
  ]);
  const neck: Pt[] = [pt(0.018, 0.79), pt(0.03, 0.862)];
  const headC = pt(0.034, 0.90);
  const headR = H * 0.066;

  // ------------------------------------------------------------------ 衣服
  // 披风只到膝上（v≈0.40）：再长就把两条腿整个盖住，走路读不出来。
  // 宽度也收窄——臂在两侧摆动，披风太宽会把手臂并进同一块剪影里。
  const cloak = smoothLoop([
    pt(-0.10, 0.815),
    pt(-0.145 + hemSway / H, 0.60),
    pt(-0.185 + hemSway / H * 1.4, 0.425),
    pt(-0.10 + hemSway / H, 0.395),
    pt(0, 0.415 - hemSway / H * 0.5),
    pt(0.10 + hemSway / H, 0.395),
    pt(0.185 + hemSway / H * 1.4, 0.425),
    pt(0.145 + hemSway / H, 0.60),
    pt(0.10, 0.815),
  ]);
  // 长外套也留出膝下的两截腿；下摆中间开一道衩，才不是一只钟。
  const coat = poly([
    pt(-0.105, 0.80), pt(0.115, 0.785), pt(0.145, 0.40),
    pt(0.025, 0.375), pt(0, 0.44), pt(-0.025, 0.375),
    pt(-0.145, 0.40),
  ]);
  const capBrim = poly([
    pt(0.028, 0.928), pt(0.162, 0.925),
    pt(0.168, 0.908), pt(0.03, 0.90),
  ]);
  const hood = smoothLoop([
    pt(-0.095, 0.855), pt(-0.09, 0.955), pt(0.01, 0.99),
    pt(0.10, 0.955), pt(0.095, 0.86), pt(0.065, 0.825),
    pt(-0.06, 0.825),
  ]);
  const coatCollar = poly([
    pt(-0.065, 0.80), pt(0.09, 0.79), pt(0.055, 0.735), pt(-0.04, 0.75),
  ]);
  const [phoneX, phoneY] = pt(armFront.handUV[0] + 0.028, armFront.handUV[1] - 0.085);

  /** 发型/帽子下那层头发：只在上色档露出来，剪影档不画。 */
  const hairShape = smoothLoop([
    pt(-0.030, 0.895), pt(-0.020, 0.962), pt(0.040, 0.985),
    pt(0.100, 0.958), pt(0.098, 0.902), pt(0.060, 0.928), pt(0.016, 0.936),
  ]);
  const eye = pt(0.086, 0.912);

  /**
   * 一副身形。`bodyFill/bodyStroke` 给四肢、躯干与头；`garment` 是披风/外套那一层
   * （上色档比身体浅一档，剪影档靠这点明度差让手臂从披风里读出来）。
   *
   * 四个画法共用这一副几何：剪影、平涂、受光两档、线稿之间切换的只是颜色，
   * **不是身材**——这正是「同一个人穿过各时代」要守住的东西。
   */
  const body = (bodyFill: string, bodyStroke: string, garmentColor: string,
                headColor: string) => (
    <g fill={bodyFill} stroke={bodyStroke} strokeLinecap="round" strokeLinejoin="round">
      {/* 披风/长外套比身体浅一档（同一色相的实色）：手臂在它前面摆动时
          才不会并进同一块黑里——轮廓让人认出「在走路」，明度差让手臂读得出来。 */}
      {wardrobe === 'cloak' && (
        <path d={cloak} fill={garmentColor} stroke={bodyStroke} strokeWidth={H * 0.012} />
      )}
      {wardrobe === 'coat' && (
        <path d={coat} fill={garmentColor} stroke={bodyStroke} strokeWidth={H * 0.01} />
      )}
      {wardrobe === 'hoodie' && <path d={hood} strokeWidth={H * 0.01} />}

      {/* 远端的手与腿先画，近端的压在上面——剪影因此有前后关系 */}
      <polyline points={`${armBack.elbow[0]},${armBack.elbow[1]} ${armBack.hand[0]},${armBack.hand[1]}`}
                fill="none" strokeWidth={H * 0.046} />
      {colored && (
        <circle cx={armBack.hand[0]} cy={armBack.hand[1]} r={H * 0.022}
                fill={skin} stroke="none" />
      )}
      <polyline points={`${pt(0, 0.5)[0]},${pt(0, 0.5)[1]} ${legBack.knee[0]},${legBack.knee[1]} ${legBack.ankle[0]},${legBack.ankle[1]}`}
                fill="none" strokeWidth={H * 0.074} />
      <line x1={legBack.ankle[0]} y1={legBack.ankle[1]}
            x2={legBack.toe[0]} y2={legBack.toe[1]} strokeWidth={H * 0.055} />

      <path d={torso} strokeWidth={H * 0.018} />
      {wardrobe === 'coat' && <path d={coatCollar} strokeWidth={H * 0.008} />}
      <line x1={neck[0][0]} y1={neck[0][1]} x2={neck[1][0]} y2={neck[1][1]}
            strokeWidth={H * 0.05} />
      <circle cx={headC[0]} cy={headC[1]} r={headR} fill={headColor} strokeWidth={H * 0.01} />
      {colored && (
        <>
          <path d={hairShape} fill={hair} stroke={hair} strokeWidth={H * 0.006} />
          {/* 上色档给一个极简的眼点：不画五官，但让头不再是一块圆 */}
          <circle cx={eye[0]} cy={eye[1]} r={H * 0.008} fill={hair} stroke="none" />
        </>
      )}
      {wardrobe === 'cap' && <path d={capBrim} strokeWidth={H * 0.008} />}
      {wardrobe === 'cloak' && (
        <path d={smoothLoop([pt(-0.08, 0.86), pt(-0.075, 0.95), pt(0.02, 0.975),
                             pt(0.095, 0.94), pt(0.085, 0.855), pt(-0.02, 0.82)])}
              strokeWidth={H * 0.008} />
      )}

      <polyline points={`${pt(0, 0.5)[0]},${pt(0, 0.5)[1]} ${legFront.knee[0]},${legFront.knee[1]} ${legFront.ankle[0]},${legFront.ankle[1]}`}
                fill="none" strokeWidth={H * 0.078} />
      <line x1={legFront.ankle[0]} y1={legFront.ankle[1]}
            x2={legFront.toe[0]} y2={legFront.toe[1]} strokeWidth={H * 0.058} />

      <line x1={pt(0.02, 0.79)[0]} y1={pt(0.02, 0.79)[1]}
            x2={armFront.elbow[0]} y2={armFront.elbow[1]} strokeWidth={H * 0.048} />
      <line x1={armFront.elbow[0]} y1={armFront.elbow[1]}
            x2={armFront.hand[0]} y2={armFront.hand[1]} strokeWidth={H * 0.046} />
      {colored && (
        <circle cx={armFront.hand[0]} cy={armFront.hand[1]} r={H * 0.023}
                fill={skin} stroke="none" />
      )}

      {wardrobe === 'hoodie' && (
        <>
          <rect x={phoneX} y={phoneY} width={H * 0.05} height={H * 0.088}
                rx={H * 0.009} fill={bodyStroke} />
          <rect x={phoneX + H * 0.008} y={phoneY + H * 0.009}
                width={H * 0.034} height={H * 0.07} rx={H * 0.004}
                fill={shade(rim, 0.25)} />
        </>
      )}
    </g>
  );

  const rimOffX = dir * H * 0.014 * (rimSide === 'front' ? 1 : -1);
  const rimOffY = -H * 0.011;

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
          : bodyArgs(fill, fill, garment, fill);
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
        <ellipse cx={x} cy={footY} rx={H * 0.20} ry={H * 0.024}
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
