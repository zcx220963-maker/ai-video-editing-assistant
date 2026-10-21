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
  /** 地面起始（画面高比例），缺省 0.82。 */
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
  children, overlay, sky, ground, horizon = 0.62, groundY = 0.82,
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
  /** 光晕强度 0~1，0 = 不画光池。缺省 0.55。 */
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
  kind = 'rect', color = '#ffb15c', glow = 0.55, flicker = 0.08,
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
  /** 脚底线（画面高比例），缺省 0.82。 */
  groundY?: number;
  /** 入画位置（画面宽比例），缺省 0.10。 */
  fromX?: number;
  /** 出画位置（画面宽比例），缺省 0.86。 */
  toX?: number;
  /** 停下的位置（画面宽比例），缺省 0.36——在光块左边一点。 */
  pauseX?: number;
  /** 停顿发生在镜头的中段（0~1），缺省 0.5。 */
  pauseAt?: number;
  /** 停顿时长（占整镜比例），缺省 0.18。 */
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
 * 从头走到尾的那个剪影：入画 → 在光旁停 → 前倾 → 继续走。
 *
 * 时间线按**整镜比例**给，不按秒：同一套「停在中段」的走法，在 10 秒与 16 秒的镜里
 * 都成立。停顿时传 `walking={false}` 给 `WalkingFigure`（腿并拢、手臂下垂），
 * 外层再绕脚底转 `lean` 度——不改 `walk.tsx` 已验证的步态。
 */
export const SilhouetteWalk: React.FC<SilhouetteWalkProps> = ({
  figureH = 0.46, groundY = 0.82, fromX = 0.10, toX = 0.86,
  pauseX = 0.36, pauseAt = 0.5, pauseLen = 0.18, lean = 8,
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
  const canPause = last >= 6;
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
        fill={fill}
        rim={rim}
        shadow={shadow}
      />
    </AbsoluteFill>
  );
};

export interface EraInscriptionProps {
  /** 一行「年代 · 地点」。 */
  text: string;
  /** 缺省右上角。 */
  position?: 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left';
  /** 字号（画面高比例），缺省 0.022。 */
  size?: number;
  color?: string;
  /** 缺省衬线。 */
  fontFamily?: string;
  opacity?: number;
}

/** 年代铭文：右上角一行小字。它是画面的一部分，**不参与字幕**，也不要和字幕抢位置。 */
export const EraInscription: React.FC<EraInscriptionProps> = ({
  text, position = 'top-right', size = 0.022, color,
  fontFamily = '"Noto Serif CJK SC", serif', opacity = 0.86,
}) => {
  const { width, height } = useVideoConfig();
  const film = resolveFilmStyle(getInputProps());
  const vertical = position.startsWith('top') ? { top: height * 0.06 } : { bottom: height * 0.06 };
  const horizontal = position.endsWith('right') ? { right: width * 0.05 } : { left: width * 0.05 };
  return (
    <div
      style={{
        position: 'absolute',
        ...vertical,
        ...horizontal,
        fontFamily,
        fontSize: Math.round(height * size),
        letterSpacing: '0.08em',
        // 缺省取纸色：这条模板的天顶是近黑（#0b1020），墨色铭文压上去等于没写
        // ——真渲静帧里实测过。浅色天空的风镜要显式传 `color`。
        color: color ?? film.paper ?? TOKENS.paper,
        opacity,
        textAlign: position.endsWith('right') ? 'right' : 'left',
        pointerEvents: 'none',
      }}
    >
      {text}
    </div>
  );
};

export interface StyleCaptionProps {
  /** 这一镜的整句字幕。不传＝注入里的 `text`。 */
  text?: string;
  highlight?: string[];
  /** 字幕宽（画面宽比例），缺省 0.62。 */
  width?: number;
  /** 距底边（画面高比例），缺省 0.035。 */
  bottom?: number;
  /** 描边色。浅底上读不清时传 `#000000`。 */
  outline?: string;
  color?: string;
}

/**
 * 贴画面的那一条字幕：底部居中、宽度约 0.62、字号由 `<CaptionText/>` 自己求。
 *
 * **不要**再用 `CaptionFrame` / `CaptionLayer` 画第二条——`<CaptionText/>` 已经是
 * 用户选的那一档字幕样式，多画一份就是同屏两条字。
 */
export const StyleCaption: React.FC<StyleCaptionProps> = ({
  text, highlight, width = 0.62, bottom = 0.035, outline, color,
}) => {
  const { width: W, height: H } = useVideoConfig();
  // 字幕落在**地面**上（这条模板的地面是近黑的 #07070a），字色必须取纸色。
  // 不显式给就继承整片墨色——墨色压墨地，等于没有字幕（探针静帧里实测过）。
  const film = resolveFilmStyle(getInputProps());
  const inkOnGround = color ?? film.paper ?? TOKENS.paper;
  // **只排一行**：`CaptionText` 的自动求值允许两行（占画面高 20%），两行就比参考片
  // 那条字幕厚一倍，还会把画面下沿压住。这里按字宽自己算一个「这一行的字刚好装满」
  // 的字号：中日文按 1em、西文按 0.55em 估，留 4% 余量；再用画面高 5% 封顶
  // （参考片字幕约占 4%）。
  const ems = Array.from(text ?? '').reduce(
    (sum, ch) => sum + (/[⺀-￿]/.test(ch) ? 1 : 0.55), 0);
  const oneLine = Math.max(12, Math.round(
    Math.min((W * width * 0.92) / Math.max(1, ems), H * 0.05)));
  return (
    <div
      style={{
        position: 'absolute',
        left: (W - W * width) / 2,
        width: W * width,
        bottom: H * bottom,
        display: 'flex',
        justifyContent: 'center',
        color: inkOnGround,
      }}
    >
      <CaptionText text={text} highlight={highlight} fitWidth={W * width}
        fontSize={oneLine} sizeScale={1} outline={outline} />
    </div>
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
    figure_x: 0.32, figure_h: 0.46, ground_y: 0.82, horizon: 0.62,
    camera: 'slow push-in 1.02x to 1.06x, no cuts',
  },
  repeat_composition: true,
  recurring_pose: 'walk left to right, pause beside the glow, lean 8 degrees, continue',
  recurring_prop: 'a glowing rectangle at 0.70W / 0.34H, same size and position every shot',
  caption: { look: 'overlay', position: 'bottom-center', width: 0.62 },
  inscription: { position: 'top-right', style: 'small serif, 0.022H' },
  motion: { camera: '1.02 to 1.06 slow push, no cuts', grain: 0.12, vignette: 0.35 },
  avoid: ['CaptionFrame band', 'PPT cards', 'face details', 'external images', 'camera shake'],
  narration_avoid: ['沉迷', '上瘾', '危害'],
} as const;
