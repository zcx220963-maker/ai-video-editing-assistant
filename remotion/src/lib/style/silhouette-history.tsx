/**
 * `@dg/lib/style/silhouette-history` —— 「剪影年轮」模板（2026-10-19）。
 *
 * 这套风格包的视觉契约只有一句话：**同一个人、同一个姿势、同一处光、同一条字幕，
 * 从一层层时代背景里走过去**。它适合讲长时段的历史、技术或生活变迁——观众看见的
 * 不是「又换了一个场景」，而是「同一件事换了一副外壳」。
 *
 * 为什么要有这一件：逐镜写码时，二十几镜各自从零画一个人、一块光、一条字幕，
 * 身高、脚底线、光位、字号一定会漂；而风格包正文只能劝，劝不住。模板把这几样写成
 * **同一份实现与同一组数**，镜头代码只画这一镜的时代背景和那一下代价动作。
 *
 * 坐标约定与 `@dg/lib/walk`、`@dg/lib/scene` 一致：成片设备像素。模板自己的几何
 * 参数（人物位置、光块位置、地平线、字幕宽）用**画面比例**给，0~1。
 *
 * 纪律：不许 `Math.random`、不许 `Date.now()`；所有变化都是帧的确定性函数。
 * 同一份代码渲两次，逐像素相同。
 */
import React from 'react';
import {
  AbsoluteFill, getInputProps, interpolate, useCurrentFrame, useVideoConfig,
} from 'remotion';
import { resolveFilmStyle } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { WalkingFigure } from '@dg/lib/walk';
import type { WalkOutfit, WalkRender, WalkWardrobe } from '@dg/lib/walk';
import { Grain, LightPool, Vignette } from '@dg/lib/brush';
import { CaptionText } from '@dg/film';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

/** 三档天空色：天顶 / 中段 / 贴地平线。 */
export type SkyTriple = [string, string, string];

export interface SilhouetteStageProps {
  /** 这一镜的世界：背景 + 人物 + 光块。会跟着相机一起推近/横移。 */
  children?: React.ReactNode;
  /** 屏幕固定层：字幕、年代铭文。不跟相机走，字号与角落位置整片一致。 */
  overlay?: React.ReactNode;
  /** 天空三段色。缺省取整片纸/墨色推出来的暖灰。 */
  sky?: SkyTriple;
  /** 地面色。缺省近黑。 */
  ground?: string;
  /** 地平线（画面高比例），缺省 0.62。 */
  horizon?: number;
  /** 地面起始（画面高比例），缺省 0.84——地面只占底部 16%，不是四分之一。 */
  groundY?: number;
  /** 运镜：`still` 不动、`push` 缓慢推近（1.02→1.06）、`pan` 轻微横移。 */
  camera?: 'still' | 'push' | 'pan';
  /** 颗粒强度 0~1，0 = 不叠。缺省 0.12。 */
  grain?: number;
  /** 暗角强度 0~1，0 = 不叠。缺省 0.35。 */
  vignette?: number;
  /** 透明底模式：不画天空/地面/颗粒/暗角，只留人物、光块、铭文、字幕。
   *  这对应风格包 manifest 的 `output: "transparent"`，不是另一条管道。 */
  transparent?: boolean;
}

/**
 * 舞台：天空 → 地面 → 地平线 → 世界 → 颗粒 → 暗角，外面再叠一层屏幕固定层。
 *
 * 相机只作用于**世界**那一层。字幕与铭文若跟着缩放，同一句字在每一镜的字号都会
 * 不一样——参考片里它们恰恰是画面里最稳定的部分。
 */
export const SilhouetteStage: React.FC<SilhouetteStageProps> = ({
  children, overlay, sky, ground, horizon = 0.62, groundY = 0.84,
  camera = 'push', grain = 0.12, vignette = 0.35, transparent = false,
}) => {
  const frame = useCurrentFrame();
  const { width, height, durationInFrames } = useVideoConfig();
  const film = resolveFilmStyle(getInputProps());
  const ink = film.ink ?? TOKENS.ink;
  const paper = film.paper ?? TOKENS.paper;
  const skyColors: SkyTriple = sky ?? [ink, paper, '#e8a06a'];
  const groundColor = ground ?? '#07070a';

  const t = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], clamp);
  const transform = camera === 'push'
    ? `scale(${(1.02 + t * 0.04).toFixed(4)})`
    : camera === 'pan'
      ? `translateX(${(-width * 0.015 + t * width * 0.03).toFixed(2)}px) scale(1.02)`
      : undefined;

  return (
    <AbsoluteFill style={{ overflow: 'hidden' }}>
      {!transparent ? (
        <>
          <AbsoluteFill
            style={{
              backgroundImage: `linear-gradient(180deg, ${skyColors[0]} 0%, ` +
                `${skyColors[1]} ${Math.round(horizon * 100)}%, ${skyColors[2]} 100%)`,
            }}
          />
          <AbsoluteFill
            style={{
              top: `${groundY * 100}%`,
              background: groundColor,
            }}
          />
          {/* 地平线那道极淡的亮线：参考片地面顶边有一条 1~2px 的路沿光。它把
              「地面」和「天空」分开，读者才认得出这是一片地，而不是一块留白。 */}
          <AbsoluteFill
            style={{
              top: `${groundY * 100}%`,
              height: 2,
              background: paper,
              opacity: 0.16,
            }}
          />
          <AbsoluteFill
            style={{
              top: `${horizon * 100}%`,
              height: 2,
              background: paper,
              opacity: 0.12,
            }}
          />
        </>
      ) : null}
      <AbsoluteFill style={transform ? { transform, transformOrigin: '50% 50%' } : undefined}>
        {children}
      </AbsoluteFill>
      {/* 前景层：草沿 + 近景山脊。**必须画在世界之后**，这是 2026-10-23 那次的教训：
          原先它们排在 `children` 前面，而任何一镜的地形（山脊、地面色块）都会一路填到
          画面底边——于是草和山脊被自己的地形整片盖掉，渲出来还是「一块纯色矩形」。
          参考片里草是**长在镜头前**的：它压住栅栏、压住人脚，才让底部那 10% 有纵深。
          草高是 `i` 的确定性函数（模板纪律：不许 Math.random，同一份代码渲两次逐像素相同）。 */}
      {!transparent ? (
        <>
          <svg
            width={width}
            height={Math.max(10, Math.round(height * 0.055))}
            viewBox={`0 0 ${width} ${Math.max(10, Math.round(height * 0.055))}`}
            style={{
              position: 'absolute',
              top: `${groundY * 100}%`,
              left: 0,
              marginTop: -Math.round(height * 0.05),
              pointerEvents: 'none',
            }}
          >
            {/* 一根一根的草，不是一条锯齿带。旧的写法把三角形首尾相接铺满整宽，
                渲出来是连续的锯齿轮廓——和「画面被切了一刀」的观感一模一样。
                参考片的草是**稀疏的细叶**：叶子之间露着地，偶尔一丛高的。
                位置/宽度/高度都取自 `Math.sin` 哈希（帧无关、代码无关、可复现），
                模板纪律不许 Math.random。 */}
            {Array.from({ length: Math.max(90, Math.round(width / 9)) }).map((_, i) => {
              const r = (n: number) => {
                const v = Math.sin((i + 1) * 12.9898 + n * 78.233) * 43758.5453;
                return v - Math.floor(v);
              };
              const bx = r(1) * width;
              const bw = 1.2 + r(2) * 2.4;
              // 每 7 根里有一根是「丛」：高出一截，让草沿有起伏。
              const tall = i % 7 === 0 ? 1.7 : 1;
              const bh = Math.max(3, height * (0.008 + r(3) * 0.026) * tall);
              const lean = (r(4) - 0.5) * bw * 1.6;
              return (
                <path
                  key={i}
                  d={`M ${(bx - bw / 2).toFixed(2)} ${bh.toFixed(2)} `
                    + `L ${(bx + lean).toFixed(2)} 0 `
                    + `L ${(bx + bw / 2).toFixed(2)} ${bh.toFixed(2)} Z`}
                  fill={groundColor}
                />
              );
            })}
          </svg>
          {/* 近景山脊：参考片底部从来不是一块平板，而是「中景山 → 更暗的近景山 →
              纯黑前景」一层层压下去。这里补最靠前的那一层，顶边是一条确定性的起伏线。 */}
          <svg
            width={width}
            height={Math.max(10, Math.round(height * (1 - groundY - 0.05)))}
            viewBox={`0 0 ${width} ${Math.max(10, Math.round(height * (1 - groundY - 0.05)))}`}
            style={{
              position: 'absolute',
              top: `${(groundY + 0.05) * 100}%`,
              left: 0,
              pointerEvents: 'none',
            }}
          >
            <path
              d={(() => {
                const n = 24;
                const hh = Math.max(10, Math.round(height * (1 - groundY - 0.05)));
                const pts = Array.from({ length: n + 1 }).map((_, i) => {
                  const px = (width * i) / n;
                  const py = hh * (0.08
                    + 0.05 * Math.sin(i * 1.7)
                    + 0.035 * Math.sin(i * 0.63 + 1.1));
                  return `${px.toFixed(1)} ${py.toFixed(1)}`;
                });
                return `M 0 ${hh} L ${pts.join(' L ')} L ${width} ${hh} Z`;
              })()}
              fill="#000000"
              opacity={0.55}
            />
          </svg>
        </>
      ) : null}
      {!transparent && grain > 0 ? <Grain opacity={grain} /> : null}
      {!transparent && vignette > 0 ? <Vignette strength={vignette} /> : null}
      {overlay ? <AbsoluteFill>{overlay}</AbsoluteFill> : null}
    </AbsoluteFill>
  );
};

/** 光块的种类。它只换**是什么在发光**，不换位置与大小。 */
export type GlowKind =
  | 'fire' | 'torch' | 'window' | 'lamp' | 'screen' | 'crt' | 'phone' | 'rect';

export interface GlowRectProps {
  /** 光块中心 x（画面宽比例），缺省 0.70。 */
  x?: number;
  /** 光块中心 y（画面高比例），缺省 0.34。 */
  y?: number;
  /** 宽（画面宽比例），缺省 0.11。 */
  w?: number;
  /** 高（画面高比例），缺省 0.16。 */
  h?: number;
  kind?: GlowKind;
  /** 光色。缺省暖橙。 */
  color?: string;
  /** 光晕强度 0~1，0 = 不画光池。缺省 0.32——参考片是哑光平涂，不是舞台追光；
   *  旧缺省 0.55 会在画面正中压出一大团径向光，把「平涂剪影」变成「打光摄影」。 */
  glow?: number;
  /** 闪烁幅度 0~1（火的呼吸感）。0 = 完全静止。缺省 0.08。 */
  flicker?: number;
  /** 透明底模式：不画光池（光池会在大片区域留下半透明像素）。 */
  transparent?: boolean;
}

/**
 * 那块贯穿全片的光。**位置与大小每镜原样传**，只换 `kind` 与 `color`。
 *
 * 它同时是这一镜的光源：光池、人物勾边光与落地影的方向都该按它来。
 * 形状都走裸 SVG，没有外部素材、没有随机数；闪烁是帧的正弦函数。
 */
export const GlowRect: React.FC<GlowRectProps> = ({
  x = 0.70, y = 0.34, w = 0.11, h = 0.16,
  kind = 'rect', color = '#ffb15c', glow = 0.32, flicker = 0.08,
  transparent = false,
}) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const cx = width * x;
  const cy = height * y;
  const bw = Math.max(2, width * w);
  const bh = Math.max(2, height * h);
  const left = cx - bw / 2;
  const top = cy - bh / 2;
  // 确定性闪烁：两个不同周期的正弦叠加，帧相同则值相同。
  const pulse = 1 + Math.sin(frame / 7.3) * flicker + Math.sin(frame / 3.1) * flicker * 0.4;
  // 半径**不**跟着闪烁走：光池一胀一缩，「同一处光」的质心会随阈值漂（实测 12px）。
  // 闪烁只调亮，光位的几何始终是同一组数。
  const radius = Math.max(bw, bh) * 1.9;

  const body = (() => {
    switch (kind) {
      case 'fire':
        return (
          <>
            <path
              d={`M${left + bw * 0.5} ${top + bh} C${left + bw * 0.08} ${top + bh * 0.72}, `
                + `${left + bw * 0.22} ${top + bh * 0.22}, ${left + bw * 0.5} ${top} `
                + `C${left + bw * 0.78} ${top + bh * 0.22}, ${left + bw * 0.92} ${top + bh * 0.72}, `
                + `${left + bw * 0.5} ${top + bh} Z`}
              fill={color}
              opacity={0.9}
            />
            <path
              d={`M${left + bw * 0.5} ${top + bh * 0.94} C${left + bw * 0.3} ${top + bh * 0.7}, `
                + `${left + bw * 0.38} ${top + bh * 0.42}, ${left + bw * 0.5} ${top + bh * 0.26} `
                + `C${left + bw * 0.62} ${top + bh * 0.42}, ${left + bw * 0.7} ${top + bh * 0.7}, `
                + `${left + bw * 0.5} ${top + bh * 0.94} Z`}
              fill="#fff3cf"
              opacity={0.85}
            />
          </>
        );
      case 'torch':
        return (
          <>
            <rect x={left + bw * 0.42} y={top + bh * 0.34} width={bw * 0.16}
              height={bh * 0.66} fill="#1b1410" />
            <path
              d={`M${left + bw * 0.5} ${top} C${left + bw * 0.2} ${top + bh * 0.2}, `
                + `${left + bw * 0.3} ${top + bh * 0.36}, ${left + bw * 0.5} ${top + bh * 0.38} `
                + `C${left + bw * 0.7} ${top + bh * 0.36}, ${left + bw * 0.8} ${top + bh * 0.2}, `
                + `${left + bw * 0.5} ${top} Z`}
              fill={color}
            />
          </>
        );
      case 'window':
        return (
          <>
            <rect x={left} y={top} width={bw} height={bh} fill={color} opacity={0.86} />
            <rect x={left + bw * 0.47} y={top} width={bw * 0.06} height={bh} fill="#120d0a" />
            <rect x={left} y={top + bh * 0.47} width={bw} height={bh * 0.06} fill="#120d0a" />
          </>
        );
      case 'lamp':
        return (
          <>
            <path
              d={`M${left} ${top + bh * 0.46} L${left + bw} ${top + bh * 0.46} `
                + `L${left + bw * 0.72} ${top} L${left + bw * 0.28} ${top} Z`}
              fill={color}
              opacity={0.5}
            />
            <circle cx={cx} cy={top + bh * 0.62} r={Math.min(bw, bh) * 0.2} fill={color} />
            <rect x={cx - bw * 0.02} y={top + bh * 0.72} width={bw * 0.04}
              height={bh * 0.28} fill="#1b1410" />
          </>
        );
      case 'screen':
        return (
          <>
            <rect x={left} y={top} width={bw} height={bh} rx={bh * 0.04} fill={color} />
            <rect x={left + bw * 0.08} y={top + bh * 0.1} width={bw * 0.84}
              height={bh * 0.8} fill="#0a0a12" opacity={0.35} />
          </>
        );
      case 'crt':
        return (
          <>
            <rect x={left} y={top} width={bw} height={bh * 0.78} rx={bh * 0.07}
              fill="#12100e" />
            <rect x={left + bw * 0.08} y={top + bh * 0.08} width={bw * 0.84}
              height={bh * 0.6} rx={bh * 0.05} fill={color} />
            <rect x={cx - bw * 0.28} y={top + bh * 0.8} width={bw * 0.56}
              height={bh * 0.16} fill="#12100e" />
          </>
        );
      case 'phone':
        return (
          <>
            <rect x={left + bw * 0.2} y={top} width={bw * 0.6} height={bh} rx={bh * 0.08}
              fill="#0b0b10" />
            <rect x={left + bw * 0.25} y={top + bh * 0.06} width={bw * 0.5}
              height={bh * 0.84} rx={bh * 0.05} fill={color} />
          </>
        );
      default:
        return <rect x={left} y={top} width={bw} height={bh} fill={color} />;
    }
  })();

  return (
    <AbsoluteFill style={{ pointerEvents: 'none' }}>
      {!transparent && glow > 0 ? (
        <LightPool x={cx} y={cy} radius={radius} color={color}
          opacity={glow * pulse} blend="screen" />
      ) : null}
      <svg width={width} height={height} style={{ position: 'absolute', inset: 0 }}>
        {/* 本体与光池按**同一个 pulse** 调亮，两者的相对权重不随帧变——光块的整体
            质心因此钉在原地；若本体常亮、只有光池闪，质心会在两者之间来回漂。 */}
        <g opacity={Math.min(1, 0.88 * pulse)}>{body}</g>
      </svg>
    </AbsoluteFill>
  );
};

export interface SilhouetteWalkProps {
  /** 人物高度（画面高比例），缺省 0.46——主角要够大。 */
  figureH?: number;
  /** 脚底线（画面高比例），缺省 0.84。 */
  groundY?: number;
  /** 入画位置（画面宽比例），缺省 0.30。 */
  fromX?: number;
  /**
   * 出画位置（画面宽比例），**缺省 0.36**——整镜只挪 6% 画面宽。
   *
   * 2026-10-23 逐帧量参考片量出来的：**他原地踏步，不横穿画面**。
   *
   * - 《094043》同一个镜头里，士兵横坐标钉在 0.10–0.13，两秒挪不到 3%；
   *   背景那排举旗的剪影墙一动不动，换时代靠**硬切**，不靠人走出去。
   * - 《132442》走路那个人两秒内 0.10 → 0.12，腿一直在走，人几乎不动。
   *
   * 改之前缺省是 `-0.08 → 1.08`（从画外进来、横穿、走出画外）——那是我按「他在穿越
   * 时代」想出来的，参考片里**一次都没有发生过**。人一横穿，画面就变成「他在走」，
   * 而不是「时代在他身后换」。用户看片原话：「这三个是一直是一个人在原地踏步的走，
   * 背景在换服装在换，而且走的很慢。」
   *
   * 真要让他走进/走出（开场、收尾），显式传 `fromX={-0.1} toX={1.1}`。
   */
  toX?: number;
  /** 停下的位置（画面宽比例），缺省 0.36——在光块左边一点。只有 `pauseLen>0` 才用。 */
  pauseX?: number;
  /** 停顿发生在镜头的中段（0~1），缺省 0.5。只有 `pauseLen>0` 才用。 */
  pauseAt?: number;
  /**
   * 停顿时长（占整镜比例）。**缺省 0：不停，整镜匀速走到底。**
   *
   * 这一条是 2026-10-23 改的，改之前缺省 0.18——每一镜都在中景停 18% 的时间、
   * 还前倾 8°。三镜拼起来是「走 → 停 → 切 → 又走」，用户看片原话是
   * 「没有一直在走的人影」。参考片里的那个人是从头走到尾的，停是例外，不是缺省。
   */
  pauseLen?: number;
  /** 停顿时的前倾角度（度），缺省 8。 */
  lean?: number;
  /** 这一镜走不走。整镜站着就传 `false`。 */
  walking?: boolean;
  facing?: 1 | -1 | 'left' | 'right';
  strideSec?: number;
  /**
   * 步态相位（0~1）。**不传就接全片时钟**：`((filmStartSec + t) / strideSec) % 1`，
   * 这样切镜时不会从「左脚在前」重新起步。只有想让这一镜单独起步时才显式传。
   */
  phase?: number;
  /** 旧写法：plain / cloak / cap / coat / hoodie。 */
  wardrobe?: WalkWardrobe;
  /** 衣服与随身物（推荐）：同一副骨架，逐镜只换这一层。 */
  outfit?: WalkOutfit;
  render?: WalkRender;
  /** 剪影色。缺省整片墨色。 */
  fill?: string;
  /** 勾边光颜色（来自这一镜的光块）。缺省整片强调色。 */
  rim?: string;
  shadow?: boolean;
}

/**
 * 一直在走的那个剪影：**站在画面里原地踏步，整镜只挪几个百分点**。
 *
 * 时间线按**整镜比例**给，不按秒：同一套走法，在 10 秒与 16 秒的镜里都成立。
 *
 * **缺省不横穿。** 2026-10-23 逐帧量过三条参考片：人在 0.10–0.13 之间钉着，
 * 两秒挪不到 3%；换时代靠硬切，不靠他走出去。他要「一直在走」，但**不往前走**——
 * 往前走的是时代，不是他。`fromX`/`toX` 缺省 0.30/0.36 就是这个意思。
 *
 * **缺省不停。** 想让他「在光旁停一下再走」，显式传 `pauseLen={0.18}`（并配合
 * `pauseAt` / `pauseX` / `lean`）；停顿时内部传 `walking={false}` 给 `WalkingFigure`
 * （腿并拢、手臂下垂），外层再绕脚底转 `lean` 度——不改 `walk.tsx` 已验证的步态。
 * 缺省给 0 是因为参考片里的那个人从头到尾都在走，停是例外；2026-10-23 之前缺省
 * 0.18，每一镜都在中景站住，用户看片原话是「没有一直在走的人影」。
 */
export const SilhouetteWalk: React.FC<SilhouetteWalkProps> = ({
  figureH = 0.46, groundY = 0.84, fromX = 0.30, toX = 0.36,
  pauseX = 0.36, pauseAt = 0.5, pauseLen = 0, lean = 8,
  walking = true, facing = 'right', strideSec = 1.0, phase,
  wardrobe = 'plain', outfit, render = 'silhouette', fill, rim, shadow = true,
}) => {
  const frame = useCurrentFrame();
  const { width, height, durationInFrames, fps } = useVideoConfig();
  // 缺省相位接**全片时钟**（2026-10-20）：写手常常不传 `phase`，而缺省 0 会让
  // 每一镜都从「左脚在前」重新起步，切点处换脚——这正是「不是同一个人在一直走」
  // 的一半来源。`filmStartSec` 由渲染侧按真实时间轴注入；显式传 `phase` 仍优先。
  const { filmStartSec = 0 } = getInputProps() as { filmStartSec?: number };
  const phaseNow = phase ?? ((filmStartSec + frame / fps) / strideSec) % 1;
  const last = Math.max(1, durationInFrames - 1);
  // `interpolate` 要求输入区间**严格递增**。比例换算出来的两个停顿点，在极短的镜、
  // 或 `pauseAt` 贴着两头时可能相等甚至反序（真机 30 帧的探针就撞上 [12,17,12,17]）：
  // 那是渲染直接抛错，不是画面难看。两种情况都退化成「整镜匀速走」——少一拍停顿，
  // 但片还在。前倾同理：过渡帧取窗口的一半再留 1 帧，短到放不下就不做了。
  // `pauseLen` 缺省 0（不停）：没有停顿就没有停顿窗口，这里必须一起关掉——
  // 只改缺省值不关这一条，`start == end` 仍会命中一帧 `stopped`，脚步在那一帧抖一下。
  const canPause = pauseLen > 0.001 && last >= 6;
  const start = canPause
    ? Math.min(last - 2, Math.max(1, Math.round((pauseAt - pauseLen / 2) * last)))
    : last;
  const end = canPause
    ? Math.max(start + 1, Math.min(last - 1, Math.round((pauseAt + pauseLen / 2) * last)))
    : last;
  const span = end - start;
  const ramp = Math.min(6, Math.max(1, Math.floor((span - 1) / 2)));

  const xFrac = !walking
    ? pauseX
    : (canPause
      ? interpolate(frame, [0, start, end, last], [fromX, pauseX, pauseX, toX], clamp)
      : interpolate(frame, [0, last], [fromX, toX], clamp));
  const stopped = walking && canPause && frame >= start && frame <= end;
  const atRest = walking ? stopped : true;
  // 前倾是「停下来的那一下」：前后各留几帧过渡，避免切点处硬折。
  const leanAmt = atRest
    ? (canPause && span >= 3
      ? interpolate(frame, [start, start + ramp, end - ramp, end], [0, lean, lean, 0], clamp)
      : lean)
    : (walking
      ? interpolate(frame, [0, Math.min(6, last)], [lean, 0], clamp)
      : lean);

  const footY = height * groundY;
  const x = width * xFrac;
  // 剪影档必须**纯黑**，不能跟着 `TOKENS.ink` 走：那是 `#23211c` 的暖炭色，
  // 压在 `#07070a` 的地面上会读成「一块灰的」——同一镜里手画的床是 #000000，
  // 模板画的人是灰的，成片里就是「有的人有、有的人没有」的另一半。
  // 2026-10-23 真机：注入里没带 `film_style` 时 `film.ink` 是 undefined，
  // 于是落到 TOKENS.ink，人比地面亮两档。这里给剪影档钉死 #000000。
  const figFill = fill ?? (render === 'silhouette' ? '#000000' : undefined);

  return (
    <AbsoluteFill
      style={{
        transformOrigin: `${x}px ${footY}px`,
        transform: leanAmt ? `rotate(${leanAmt.toFixed(2)}deg)` : undefined,
      }}
    >
      <WalkingFigure
        x={x}
        footY={footY}
        height={height * figureH}
        facing={facing}
        strideSec={strideSec}
        phase={phaseNow}
        walking={walking && !stopped}
        wardrobe={wardrobe}
        outfit={outfit}
        render={render}
        fill={figFill}
        rim={rim}
        shadow={shadow}
      />
    </AbsoluteFill>
  );
};

export interface EraInscriptionProps {
  /** 首行「年代 · 地点」，或「日期」。 */
  text: string;
  /**
   * 第二行（事件名）。参考片是**两行右对齐**：首行「1937.11.8」、次行「太原失守」。
   * 不给就只有一行。
   */
  event?: string;
  /** 首行前面那道红色短横。缺省开（参考片每一镜都有）。 */
  dash?: boolean;
  /** 缺省右上角。 */
  position?: 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left';
  /** 字号（画面高比例），缺省 0.024（参考片约 0.028）。 */
  size?: number;
  color?: string;
  /** 短横的颜色，缺省砖红。 */
  dashColor?: string;
  /** 缺省衬线。 */
  fontFamily?: string;
  opacity?: number;
}

/** 年代铭文：右上角两行小字。它是画面的一部分，**不参与字幕**，也不要和字幕抢位置。 */
export const EraInscription: React.FC<EraInscriptionProps> = ({
  text, event, dash = true, position = 'top-right', size = 0.024, color,
  dashColor = '#b33a2b',
  fontFamily = '"Noto Serif CJK SC", serif', opacity = 0.9,
}) => {
  const { width, height } = useVideoConfig();
  const film = resolveFilmStyle(getInputProps());
  const fs = Math.round(height * size);
  const vertical = position.startsWith('top') ? { top: height * 0.045 } : { bottom: height * 0.06 };
  const horizontal = position.endsWith('right') ? { right: width * 0.035 } : { left: width * 0.035 };
  const align = position.endsWith('right') ? 'right' : 'left';
  const inkColor = color ?? film.paper ?? TOKENS.paper;
  return (
    <div
      style={{
        position: 'absolute',
        ...vertical,
        ...horizontal,
        fontFamily,
        fontSize: fs,
        letterSpacing: '0.06em',
        // 缺省取纸色：这条模板的天顶是近黑（#0b1020），墨色铭文压上去等于没写
        // ——真渲静帧里实测过。浅色天空的风镜要显式传 `color`。
        color: inkColor,
        opacity,
        textAlign: align,
        lineHeight: 1.5,
        pointerEvents: 'none',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: align === 'right' ? 'flex-end' : 'flex-start', gap: Math.round(fs * 0.5) }}>
        {dash ? (
          <span
            style={{
              display: 'inline-block',
              width: Math.round(fs * 1.6),
              height: Math.max(1, Math.round(fs * 0.09)),
              background: dashColor,
            }}
          />
        ) : null}
        <span>{text}</span>
      </div>
      {event ? <div style={{ opacity: 0.88 }}>{event}</div> : null}
    </div>
  );
};

export interface StyleCaptionProps {
  /** 这一镜的整句字幕。不传＝注入里的 `text`。 */
  text?: string;
  /**
   * 逐行给。**参考片是两行**，整句塞一行会挤成一条。
   * 不给就按下面的规矩自动排。
   */
  lines?: string[];
  highlight?: string[];
  /**
   * 字幕落点。**缺省 `top-left`**。
   *
   * 三条参考片里两种都有：《094043》在左上，《080632》《132442》在底部居中。
   * 选左上是因为**底部那条路会踩到一个结构性坑**：这一套的地面是纯黑实心块
   * （`groundY` 以下全黑），字幕压上去，只要那一镜的地面没有草沿/山脊撑住，
   * 整张图就会被读成「为了放字专门切掉了一条」——真机 s05 与 2026-10-23 那一版
   * 预览都是这个观感，用户点名两次。
   * 摆到天上就没有这个问题：天空自带渐变，字幕压上去永远是「压在画面上」。
   * 想要参考片《132442》那种贴底双语，显式传 `position="bottom-center"` 即可，
   * 但那一镜的地面必须有内容。
   */
  position?: 'top-left' | 'top-right' | 'bottom-center';
  /** 左右边距（画面宽比例），缺省 0.06。 */
  x?: number;
  /** 上边距（画面高比例，top-* 档），缺省 0.115。 */
  top?: number;
  /** 距底边（画面高比例，仅 bottom-center 档），缺省 0.042。 */
  bottom?: number;
  /** 字幕宽（画面宽比例），缺省 0.62。 */
  width?: number;
  /** 字号上限（画面高比例），缺省 0.042——参考片量出来约 0.040。 */
  sizeCap?: number;
  /**
   * 描边色。**缺省不给**：参考片是无描边白字，只带一层淡投影。
   * 传了就退化成 16 向细描边（浅天空上要硬保读性时才用）。
   */
  outline?: string;
  color?: string;
  /** 亮底上读不清时铺一层渐隐（不是色块，没有边界）。缺省不开。 */
  scrim?: boolean;
  /** 行距。缺省 1.28（参考片两行贴得比 1.5 紧）。 */
  lineGap?: number;
  align?: 'left' | 'center' | 'right';
}

/**
 * 压在画面上的那一条字幕：**贴左上、两行、不占位**。
 *
 * 形状按参考片量：白字、**无描边**（只有一层淡投影）、字号约画面高 4.2%、
 * 两行。两行是「外文 + 中文」——注入里有 `text_en` 就自动这么排，外文那行 0.62 倍字号。
 *
 * 三处历史故障都在这个组件里：
 * 1. 旧实现把整句压成一行（字号按整句求），于是长句变成一条比画面还挤的字——
 *    真机 s02 的样子。
 * 2. 旧实现给了 16 向粗黑边。参考片是「白字 + 一层淡投影」，粗黑边一上就像贴纸。
 * 3. **贴底是这条链上最贵的那个坑。** 这一套的地面是纯黑实心块，字幕压上去，
 *    只要那一镜的地面没有草沿/山脊撑住，整张图就被读成「为了放字切掉了一条」。
 *    2026-10-23 那一版预览正是这样：`composition.ground_ratio` 写了 0.25，
 *    底部四分之一全是黑的，字幕钉在 bottom 4.5% —— 用户原话「字幕不在画面上，
 *    而是截断了画面」。所以缺省挪到天上；`bottom-center` 留着，但要自己保证
 *    地面有内容（草沿/亮线/山脊）。
 *
 * **不要**再用 `CaptionFrame` / `CaptionLayer` 画第二条：`<CaptionText/>` 已经是
 * 用户选的那一档字幕样式，多画一份就是同屏两条字（叠在同一条线上，看起来只是
 * 变粗、发虚，最容易漏过去的一种错）。闸在 `lint-rules.mjs` 的 `caption-double`。
 */
export const StyleCaption: React.FC<StyleCaptionProps> = ({
  text, lines, highlight, position = 'top-left', x = 0.06, top = 0.115,
  bottom = 0.042, width = 0.62, sizeCap = 0.042,
  outline, color, scrim = false, lineGap = 1.28, align,
}) => {
  const { width: W, height: H } = useVideoConfig();
  const props = getInputProps() as { text?: string; text_en?: string };
  const film = resolveFilmStyle(props);
  // 缺省取纸色（近白）。参考片压在暗天/暗地上，白字靠对比读，不靠黑边。
  const inkColor = color ?? film.paper ?? '#ffffff';

  // 排法：显式 `lines` 照用 → 注入里有 `text_en` 就排成「外文 / 中文」两行
  // → 否则按句末标点拆中文（最多两行，三行并进第二行）。
  // `k` 是这一行相对主行的字号倍率：外文那行 0.62（参考片就是这样）。
  type Row = { s: string; k: number };
  const rows: Row[] = (() => {
    if (lines && lines.length) {
      return lines.map((s) => String(s).trim()).filter(Boolean).map((s) => ({ s, k: 1 }));
    }
    const zh = String(text ?? props.text ?? '').trim();
    const en = String(props.text_en ?? '').trim();
    if (en) return [{ s: en, k: 0.62 }, ...(zh ? [{ s: zh, k: 1 }] : [])];
    if (!zh) return [];
    const parts = zh.split(/(?<=[。；！？!?;])/).map((s) => s.trim()).filter(Boolean);
    if (parts.length <= 1) return [{ s: zh, k: 1 }];
    const two = parts.length === 2 ? parts : [parts[0], parts.slice(1).join('')];
    return two.map((s) => ({ s, k: 1 }));
  })();

  // 字号按**每一行**各自求一遍再取最小：中日文按 1em、西文按 0.55em 估，留 8% 余量。
  // 整句求值会把两行压成一条小字；只按最长行求又会让外文那行溢出。
  const emsOf = (s: string) => Array.from(s).reduce(
    (sum, ch) => sum + (/[\u2E80-\uFFFF]/.test(ch) ? 1 : 0.55), 0);
  const fit = rows.reduce((acc, r) => Math.min(
    acc, (W * width * 0.92) / Math.max(1, emsOf(r.s)) / Math.max(0.1, r.k),
  ), Number.POSITIVE_INFINITY);
  const size = Math.max(12, Math.round(Math.min(Number.isFinite(fit) ? fit : H * sizeCap,
    H * sizeCap)));

  const textAlign = align ?? (position === 'top-right' ? 'right'
    : position === 'bottom-center' ? 'center' : 'left');
  const alignItems = textAlign === 'left' ? 'flex-start'
    : textAlign === 'right' ? 'flex-end' : 'center';
  const box = position === 'bottom-center'
    ? { left: (W - W * width) / 2, width: W * width, bottom: H * bottom }
    : {
      width: W * width,
      top: H * top,
      ...(position === 'top-right' ? { right: W * x } : { left: W * x }),
    };

  // 无描边时给一层淡投影：参考片就是这个量级，再重就变成「贴纸字」。
  const shadow = outline
    ? Array.from({ length: 16 }, (_, k) => {
      const a = (Math.PI * 2 * k) / 16;
      const rw = Math.max(2, Math.round(size * 0.06));
      return `${Math.round(Math.cos(a) * rw)}px ${Math.round(Math.sin(a) * rw)}px 0 ${outline}`;
    }).join(', ')
    : '0 2px 8px rgba(0,0,0,0.55), 0 0 2px rgba(0,0,0,0.35)';

  return (
    <>
      {scrim ? (
        <div
          style={{
            position: 'absolute',
            left: 0, right: 0,
            ...(position === 'bottom-center' ? { bottom: 0 } : { top: 0 }),
            height: H * 0.24,
            background: position === 'bottom-center'
              ? 'linear-gradient(to top, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0.26) 42%, rgba(0,0,0,0) 100%)'
              : 'linear-gradient(to bottom, rgba(0,0,0,0.5) 0%, rgba(0,0,0,0.22) 42%, rgba(0,0,0,0) 100%)',
            pointerEvents: 'none',
          }}
        />
      ) : null}
      <div
        style={{
          position: 'absolute',
          ...box,
          display: 'flex',
          flexDirection: 'column',
          alignItems,
          gap: Math.round(size * (lineGap - 1)),
          color: inkColor,
          textAlign,
        }}
      >
        {rows.map((row, i) => {
          const fs = Math.max(10, Math.round(size * row.k));
          return (
            <div
              key={i}
              style={{
                fontSize: fs,
                lineHeight: 1.22,
                fontWeight: 400,
                letterSpacing: '0.02em',
                color: inkColor,
                textShadow: shadow,
              }}
            >
              <CaptionText text={row.s} highlight={highlight} fitWidth={W * width}
                fontSize={fs} sizeScale={1} outline={undefined} />
            </div>
          );
        })}
      </div>
    </>
  );
};

/**
 * 风格包的缺省 manifest（与 `examples/skills/style_silhouette_history/SKILL.md`
 * 的那一行 JSON 同源）。前端「填入剪影模板」直接用它；镜头代码也可以按它的缺省值
 * 传参，两边不会各写一套数。
 */
export const silhouetteHistoryDefaults = {
  version: 1,
  template: 'silhouette-history',
  palette: {
    sky: ['#0b1020', '#3a2a3f', '#e8a06a'],
    ground: '#07070a',
    ink: '#050507',
    accent: '#ff7a2f',
    paper: '#f2e6d0',
  },
  composition: {
    figure_x: 0.32, figure_h: 0.34, ground_y: 0.84, horizon: 0.62,
    camera: 'slow push-in 1.02x to 1.06x, no cuts',
  },
  repeat_composition: true,
  recurring_pose: 'walk left to right from off-screen, never stop, never sit, never lie down',
  recurring_prop: 'a glowing rectangle at 0.70W / 0.34H, same size and position every shot',
  caption: { look: 'overlay', position: 'top-left', align: 'left', width: 0.62, lines: 2, size: 0.042 },
  inscription: { position: 'top-right', style: 'small serif, 0.024H, two lines: date / event' },
  motion: { camera: '1.02 to 1.06 slow push, no cuts', grain: 0.12, vignette: 0.35 },
  avoid: ['CaptionFrame band', 'PPT cards', 'face details', 'external images', 'camera shake'],
  narration_avoid: ['沉迷', '上瘾', '危害'],
} as const;
