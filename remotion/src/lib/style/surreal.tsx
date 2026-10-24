/**
 * `@dg/lib/style/surreal` —— 超现实写实的能力件。
 *
 * 设计原则（2026-10-22，用户原话「要像 skill 那样只给各种能力，而不是写死每一步怎么用」）：
 *
 *   **这里没有「舞台」，也没有「一镜一件事物」的版式。** 每一件都只回答一个技术问题
 *   （光怎么穿空气、灰怎么飘、火怎么亮、水怎么反光、镜头怎么绕），场景长什么样、
 *   主体是谁、东西怎么摆，全归这一镜的文案与 brief。用户换一个题材，这一层一个字都不用改。
 *
 * 纪律：
 *   · 不许 `Math.random`、不许 `Date.now()`——所有随机量由 `seed` 与元素下标决定，
 *     同一份代码渲两次逐像素相同（remotion 的 `{crf:1, concurrency:1}` 确定性测试靠它）。
 *   · 不读外部图片、不引新依赖：屏幕层全是 SVG / CSS 渐变 / `filter`，三维层只用
 *     仓库里已有的 `three` + `@react-three/fiber` + `@remotion/three`。
 *   · 每件都能单独用。没有 `SurrealWorld` 也可以只用 `LightShaft` + `EmberField`；
 *     反过来只有 `SurrealWorld`、里面什么都不放也成立（它就是一束光、一层雾和一个镜头）。
 *
 * 典型用法（**只是其中一种拼法**，不是规定）：
 *
 * ```tsx
 * import { SurrealWorld, LightShaft, EmberField, SmokePlume, Halo, Bloom }
 *   from '@dg/lib/style/surreal';
 *
 * <FilmLayout text={text} textEn={textEn} aspect={aspect}>
 *   <SurrealWorld cam={{ from: [0, 0.4, 2.2], to: [0.1, 0.5, 1.7] }} fog={{ color: '#1a120c', near: 1.2, far: 4 }}>
 *     <mesh position={[0, 0.2, 0]}>…</mesh>
 *   </SurrealWorld>
 *   <LightShaft from={{ x: 0.18, y: 0 }} to={{ x: 0.62, y: 1 }} color="#ffca8a" />
 *   <EmberField count={60} area={{ x: 0.3, y: 0.4, w: 0.4, h: 0.6 }} />
 *   <SmokePlume x={0.5} y={0.55} />
 *   <Bloom radius={18} intensity={0.55} />
 * </FilmLayout>
 * ```
 */
import React, { useEffect, useMemo } from 'react';
import {
  AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, Easing,
} from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { Grain, Vignette } from '@dg/lib/brush';
import type { Aspect } from '@dg/tokens';

/* ------------------------------------------------------------------ */
/* 确定性随机                                                          */
/* ------------------------------------------------------------------ */

/** 同 `cinema-studio.seeded`：整数 → 0..1，跨进程一致。 */
export const seeded = (i: number): number => {
  const x = Math.sin(i * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};

const seedOf = (s: number | string | undefined): number => {
  if (typeof s === 'number') return s;
  const text = String(s ?? 'surreal');
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h % 100000) + 1;
};

/** 场景里可用的**光位**。真实感来自「一束主光 + 一点轮廓光 + 很低的底光」这个比例，
 *  而不是把场景照亮。`key` 是主光强度，`fill` 应显著小于它（约 1:4）。 */
export type SurrealLight = {
  key?: [number, number, number];
  keyColor?: string;
  keyIntensity?: number;
  rim?: [number, number, number];
  rimColor?: string;
  rimIntensity?: number;
  ambient?: number;
  /** 环境光贴图（IBL）的强度。缺省 0.55；0 = 关掉（关掉后金属没有东西可反射，会渲成一块黑）。
   *  它管的是**所有物体身上的反射与环境色**——写实里「材质有东西可照」全靠这一层，
   *  和 `key` 的主光不冲突：主光决定亮面，IBL 决定暗面与高光里出现什么。 */
  envIntensity?: number;
  /** 曝光。缺省 1.0；夜景压到 0.8~0.9，日光推到 1.1。 */
  exposure?: number;
};

/* ------------------------------------------------------------------ */
/* 三维世界：相机 + 雾 + 光 + 调色                                      */
/* ------------------------------------------------------------------ */

type CamPath = {
  /** 起点（世界单位，米）。缺省 [0, 0.35, 2.2]。 */
  from?: [number, number, number];
  /** 终点。缺省与 from 相同（静止）。 */
  to?: [number, number, number];
  /** 看向哪。缺省 [0, 0.15, 0]。 */
  look?: [number, number, number];
  /** 焦距。缺省 35；超现实常用 24（广角压迫）或 85（压缩纵深）。 */
  fov?: number;
  /** 手持微晃的幅度（世界单位）。缺省 0。 */
  shake?: number;
};

export interface SurrealWorldProps {
  children?: React.ReactNode;
  /** 相机路径。一镜一个动作，`from`→`to` 走完整个镜长。 */
  cam?: CamPath;
  /** 体积雾。`density` 越大越「水下」，`color` 决定空气偏什么色。 */
  fog?: { color?: string; near?: number; far?: number };
  /** 背景（画布清屏色）。缺省透明，交给下层 `Atmosphere` 或自定义底。 */
  sky?: string | null;
  lights?: SurrealLight;
  /** 三维层像素比。**这是整帧最贵的一段**（软件 WebGL 约 1.4 s/帧 @1920）。
   *  缺省 0.7：按 1344×756 渲三维再放大，屏幕层仍满分辨率。要逐像素一致才写 1。 */
  dpr?: number;
  shadows?: boolean;
  /** 画布上方的屏幕层（DOM/SVG），不跟相机走。 */
  overlay?: React.ReactNode;
  /** 最顶上的调色层：颗粒、暗角、雾。缺省都开一点点。 */
  grain?: number;
  vignette?: number;
  style?: React.CSSProperties;
}

/** 这个组件类型画的是 DOM 还是三维（与 `cinema-studio` 同一判据，理由见那里的注释）：
 *  字符串标签（`svg`/`div`…）是 DOM；自带 `domLayer` 静态标记的组件也是 DOM。
 *  把 `<svg>` 直接塞进 Canvas 会抛 `R3F: Svg is not part of the THREE namespace`。
 *
 *  **三维元素也是字符串标签**：`<mesh>` / `<group>` / `<boxGeometry>` 在 JSX 里同样是
 *  `'mesh'`、`'group'`，所以「是字符串就是 DOM」会把这一镜的三维内容整块挪到屏幕层，
 *  在 DOM 里渲染成几个不显示的未知标签——画面只剩 SVG 与光效。2026-10-22 探针实测：
 *  一个 1×1 的红方块摆在原点，渲出来整幅只有底色。这就是「超现实写实」整片看起来
 *  像矢量插画的真因。改成白名单：只有认得出来的 HTML/SVG 标签才算 DOM。 */
const DOM_TAGS = new Set([
  'svg', 'g', 'defs', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline',
  'polygon', 'text', 'tspan', 'textPath', 'foreignObject', 'image', 'use',
  'pattern', 'mask', 'clipPath', 'linearGradient', 'radialGradient', 'stop',
  'filter', 'feGaussianBlur', 'feTurbulence', 'feColorMatrix', 'feBlend',
  'feOffset', 'feMerge', 'feMergeNode', 'feComponentTransfer', 'feFuncA',
  'div', 'span', 'section', 'article', 'p', 'b', 'i', 'em', 'strong', 'small',
  'br', 'img', 'canvas', 'video', 'sub', 'sup', 'pre', 'code',
]);

type DomLayerType = { domLayer?: boolean };
const isDomLayerType = (type: unknown): boolean => (
  typeof type === 'string'
    ? DOM_TAGS.has(type)
    : Boolean((type as DomLayerType | null)?.domLayer)
);

const splitChildren = (children: React.ReactNode) => {
  const dom: React.ReactNode[] = [];
  const three: React.ReactNode[] = [];
  const walk = (node: React.ReactNode): void => {
    React.Children.forEach(node, (child) => {
      if (!React.isValidElement(child)) {
        if (child === null || child === undefined || typeof child === 'boolean') return;
        three.push(child);
        return;
      }
      if (child.type === React.Fragment) {
        walk((child.props as { children?: React.ReactNode }).children);
        return;
      }
      if (isDomLayerType(child.type)) dom.push(child);
      else three.push(child);
    });
  };
  walk(children);
  return { dom, three };
};

const Rig: React.FC<{
  lights?: SurrealLight; cam: CamPath; frame: number; shadows?: boolean;
}> = ({ lights, cam, frame, shadows = false }) => {
  // 相机与色调映射都改**默认相机**：`<ThreeCanvas>` 自带一台 perspectiveCamera，
  // 再 attach 一台会同时存在两台、`useThree().camera` 指向的那台却没被摆过。
  const { camera, gl, scene } = useThree();
  const { durationInFrames, width, height } = useVideoConfig();
  gl.toneMapping = THREE.ACESFilmicToneMapping;
  gl.toneMappingExposure = lights?.exposure ?? 1.0;
  if (shadows) gl.shadowMap.type = THREE.PCFSoftShadowMap;
  // 环境贴图（IBL）：写实感里最容易被漏掉的一层。没有它，`metalness` 高的材质
  // 在暗面没有东西可反射，渲出来是一块死板的灰；有了它，同一套几何立刻像「被拍到的」。
  // RoomEnvironment 是 three 自带的程序化房间（不读图片），一次生成、逐镜复用。
  const envMap = useMemo(() => {
    const pmrem = new THREE.PMREMGenerator(gl);
    const tex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
    return tex;
  }, [gl]);
  const envIntensity = lights?.envIntensity ?? 0.55;
  useEffect(() => {
    scene.environment = envMap;
    scene.environmentIntensity = envIntensity;
    return () => { scene.environment = null; };
  }, [scene, envMap, envIntensity]);
  const t = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], {
    easing: Easing.inOut(Easing.ease), extrapolateRight: 'clamp',
  });
  const from = cam.from ?? [0, 0.35, 2.2];
  const to = cam.to ?? from;
  const look = cam.look ?? [0, 0.15, 0];
  const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
  let x = lerp(from[0], to[0], t);
  let y = lerp(from[1], to[1], t);
  const z = lerp(from[2], to[2], t);
  if ((cam.shake ?? 0) > 0) {
    // 手持的呼吸感：两路不同频率的正弦。不用随机——逐帧随机会抖成噪点，也不可复现。
    const amt = cam.shake ?? 0;
    x += (Math.sin(frame / 23) * 0.7 + Math.sin(frame / 9) * 0.3) * amt;
    y += Math.cos(frame / 31) * amt * 0.6;
  }
  camera.position.set(x, y, z);
  camera.lookAt(look[0], look[1], look[2]);
  // `fov` 从前只是文档里的一行：`<ThreeCanvas>` 不给相机，r3f 缺省是 75° 的广角，
  // 于是写了 `fov: 35` 的镜头实际仍是 75°——人只有应有的一半大，构图全靠猜。
  // 三件必须在校准前一起写：焦距、画幅比、近远裁剪。
  const cam3 = camera as THREE.PerspectiveCamera;
  cam3.fov = cam.fov ?? 35;
  cam3.aspect = width / height;
  cam3.near = 0.05;
  cam3.far = 400;
  camera.updateProjectionMatrix();
  const light = lights ?? {};
  return (
    <>
      <ambientLight intensity={light.ambient ?? 0.22} />
      <directionalLight
        position={light.key ?? [-1.1, 1.6, 0.9]}
        color={light.keyColor ?? '#ffd9b0'}
        intensity={light.keyIntensity ?? 2.4}
        castShadow={shadows}
        shadow-mapSize-width={1024}
        shadow-mapSize-height={1024}
        shadow-bias={-0.0008}
        shadow-normalBias={0.02}
        shadow-radius={4}
        shadow-camera-left={-3}
        shadow-camera-right={3}
        shadow-camera-top={3}
        shadow-camera-bottom={-3}
        shadow-camera-near={0.1}
        shadow-camera-far={12}
      />
      <directionalLight
        position={light.rim ?? [1.2, 0.9, -1.3]}
        color={light.rimColor ?? '#7fb0ff'}
        intensity={light.rimIntensity ?? 0.65}
      />
    </>
  );
};


/** 三维世界：相机路径 + 雾 + 一套光 + 调色。里面放什么由这一镜决定。
 *
 *  它**不是**模板：没有台面、没有墙、没有「一件实物」的构图。空着也成立（光穿空气）。 */
export const SurrealWorld: React.FC<SurrealWorldProps> = ({
  children, cam = {}, fog, sky = null, lights, dpr = 0.7, shadows = false,
  overlay, grain = 0.08, vignette = 0.32, style,
}) => {
  const { width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const { dom, three } = useMemo(() => splitChildren(children), [children]);
  const fogColor = fog?.color ?? '#141018';
  return (
    <AbsoluteFill style={{ background: sky ?? 'transparent', ...style }}>
      <ThreeCanvas
        width={width}
        height={height}
        dpr={dpr}
        shadows={shadows}
        gl={{ antialias: false, alpha: sky === null, preserveDrawingBuffer: true }}
        style={{ position: 'absolute', inset: 0 }}
      >
        <Rig lights={lights} cam={cam} frame={frame} shadows={shadows} />
        {fog ? (
          <fog attach="fog" args={[fogColor, fog.near ?? 1.4, fog.far ?? 6]} />
        ) : null}
        {three}
      </ThreeCanvas>
      {dom.length ? (
        <AbsoluteFill style={{ pointerEvents: 'none' }}>{dom}</AbsoluteFill>
      ) : null}
      {overlay ? <AbsoluteFill style={{ pointerEvents: 'none' }}>{overlay}</AbsoluteFill> : null}
      {grain > 0 ? <Grain opacity={grain} /> : null}
      {vignette > 0 ? <Vignette strength={vignette} /> : null}
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ */
/* 光                                                                  */
/* ------------------------------------------------------------------ */

export interface LightShaftProps {
  /** 光源位置（画面比例 0–1）。 */
  from?: { x: number; y: number };
  /** 光束落地/散开的位置（画面比例 0–1）。 */
  to?: { x: number; y: number };
  /** 光束在 `to` 处的半宽（画面宽度比例）。 */
  width?: number;
  color?: string;
  /** 0–1。0.35 左右是「空气里有灰」；0.7 以上会糊掉主体。 */
  intensity?: number;
  /** 缓慢摆动的幅度（像素）。缺省 0（不动）。 */
  sway?: number;
  /** 光束中间那几道更亮的丝。缺省 3。 */
  veins?: number;
  seed?: number | string;
  /** 光源处的一团光晕半径（画面宽度比例）。缺省 0.12；0 = 不画。 */
  halo?: number;
  style?: React.CSSProperties;
}

/** 穿过空气的一束光。屏幕层 SVG，**不参与三维**、不吃 GPU。
 *
 *  真实感的来源不是「画一条亮带」，而是：边缘比中心暗、里面有几道更亮的丝、
 *  落到画面边缘时被雾吃掉一点——这三件事在这里都做了。 */
export const LightShaft: React.FC<LightShaftProps> = ({
  from = { x: 0.15, y: -0.05 }, to = { x: 0.6, y: 1.05 }, width = 0.22,
  color = '#ffd9a8', intensity = 0.4, sway = 0, veins = 3, seed = 'shaft',
  halo = 0.12, style,
}) => {
  const { width: W, height: H, fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const t = frame / Math.max(1, fps);
  const n = seedOf(seed);
  const sx = Math.sin(t * 0.5 + n) * sway;
  const x0 = from.x * W + sx;
  const y0 = from.y * H;
  const x1 = to.x * W + sx * 1.6;
  const y1 = to.y * H;
  const dx = x1 - x0;
  const dy = y1 - y0;
  const len = Math.max(1, Math.hypot(dx, dy));
  const nx = -dy / len;
  const ny = dx / len;
  const half = width * W * 0.5;
  const quad = (k: number) => {
    const w0 = half * 0.18 * k;
    const w1 = half * k;
    return `${x0 + nx * w0},${y0 + ny * w0} ${x1 + nx * w1},${y1 + ny * w1} `
      + `${x1 - nx * w1},${y1 - ny * w1} ${x0 - nx * w0},${y0 - ny * w0}`;
  };
  const grad = `shaft-${Math.round(n)}`;
  return (
    <svg width={W} height={H} style={{ position: 'absolute', inset: 0, pointerEvents: 'none', ...style }}>
      <defs>
        <linearGradient id={grad} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity={intensity} />
          <stop offset="0.45" stopColor={color} stopOpacity={intensity * 0.55} />
          <stop offset="1" stopColor={color} stopOpacity={0} />
        </linearGradient>
        <filter id={`${grad}-blur`} x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation={Math.max(6, W * 0.012)} />
        </filter>
      </defs>
      <g filter={`url(#${grad}-blur)`}>
        <polygon points={quad(1)} fill={`url(#${grad})`} />
        {Array.from({ length: Math.max(0, veins) }, (_, i) => {
          const k = 0.18 + seeded(n + i * 7.3) * 0.42;
          const off = (seeded(n + i * 3.1) - 0.5) * half * 1.1;
          return (
            <polygon
              key={i}
              points={quad(k)}
              fill={`url(#${grad})`}
              opacity={0.35 + seeded(n + i * 11.7) * 0.4}
              transform={`translate(${nx * off}, ${ny * off})`}
            />
          );
        })}
      </g>
      {halo > 0 ? <Halo x={from.x} y={from.y} r={halo} color={color} intensity={intensity * 1.6} /> : null}
    </svg>
  );
};

export interface HaloProps {
  x: number; y: number; r: number;
  color?: string;
  intensity?: number;
  /** 呼吸周期（秒）。缺省 0（不呼吸）。 */
  pulse?: number;
  style?: React.CSSProperties;
}

/** 一团柔和的光晕（光源、屏幕反光、火点）。位置与半径都是画面比例。 */
export const Halo: React.FC<HaloProps> = ({
  x, y, r, color = '#ffcf8f', intensity = 0.6, pulse = 0, style,
}) => {
  const { width: W, height: H, fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const k = pulse > 0 ? 1 + Math.sin((frame / Math.max(1, fps)) * Math.PI * 2 / pulse) * 0.12 : 1;
  const px = x * W;
  const py = y * H;
  const pr = r * W * k;
  const id = `halo-${Math.round(px)}-${Math.round(py)}`;
  return (
    <g style={style}>
      <defs>
        <radialGradient id={id}>
          <stop offset="0" stopColor={color} stopOpacity={Math.min(1, intensity)} />
          <stop offset="0.35" stopColor={color} stopOpacity={Math.min(1, intensity * 0.45)} />
          <stop offset="1" stopColor={color} stopOpacity={0} />
        </radialGradient>
      </defs>
      <ellipse cx={px} cy={py} rx={pr} ry={pr} fill={`url(#${id})`} />
    </g>
  );
};

/* ------------------------------------------------------------------ */
/* 粒子与烟                                                            */
/* ------------------------------------------------------------------ */

export interface EmberFieldProps {
  count?: number;
  /** 活动区域（画面比例）。 */
  area?: { x: number; y: number; w: number; h: number };
  color?: string;
  /** 起始半径（像素），实际在 0.3~1.4 倍间抖动。 */
  size?: number;
  /** 上升速度（画面高度/秒）。缺省 0.08。负值=下落（雨、灰）。 */
  rise?: number;
  seed?: number | string;
  /** 左右摆动幅度（画面宽度比例）。缺省 0.02。 */
  drift?: number;
  /** 每颗粒子的不透明度。缺省 0.85。 */
  opacity?: number;
}

/** 火点 / 灰烬 / 孢子 / 雨：一串会自己循环的发光颗粒。
 *
 *  与 `@dg/lib/scene` 的 `Particles` 分工：那一个是「空气里的浮尘」（暗、小、慢），
 *  这一件是**带光晕的**（火、萤火、雪、雨），每颗有长短不一的寿命与亮度。 */
export const EmberField: React.FC<EmberFieldProps> = ({
  count = 50, area = { x: 0, y: 0, w: 1, h: 1 }, color = '#ffb86b', size = 3,
  rise = 0.08, seed = 'ember', drift = 0.02, opacity = 0.85,
}) => {
  const { width: W, height: H, fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const t = frame / Math.max(1, fps);
  const n = seedOf(seed);
  const ax = area.x * W;
  const ay = area.y * H;
  const aw = Math.max(1, area.w * W);
  const ah = Math.max(1, area.h * H);
  const dots = Array.from({ length: Math.max(0, count) }, (_, i) => {
    const speed = rise * (0.5 + seeded(n + i * 5.7) * 1.4);
    const phase = seeded(n + i * 3.3) * ah;
    const y = ay + ah - (((phase + t * speed * ah) % ah) + ah) % ah;
    const x = ax + seeded(n + i * 9.1) * aw
      + Math.sin(t * (0.7 + seeded(n + i * 2.1)) + i) * drift * W;
    const r = size * (0.3 + seeded(n + i * 7.7) * 1.1);
    // 颗粒在区域顶/底淡出：不淡出的话循环出口处会「跳」一下。
    const edge = Math.min(1, Math.min(y - ay, ay + ah - y) / (ah * 0.18));
    return { key: i, x, y, r, o: opacity * Math.max(0, edge) };
  });
  return (
    <svg width={W} height={H} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      <defs>
        <radialGradient id={`ember-${Math.round(n)}`}>
          <stop offset="0" stopColor="#ffffff" stopOpacity={0.9} />
          <stop offset="0.3" stopColor={color} stopOpacity={0.85} />
          <stop offset="1" stopColor={color} stopOpacity={0} />
        </radialGradient>
      </defs>
      {dots.map((d) => (
        <circle key={d.key} cx={d.x} cy={d.y} r={d.r * 3}
          fill={`url(#ember-${Math.round(n)})`} opacity={d.o} />
      ))}
    </svg>
  );
};

export interface SmokePlumeProps {
  /** 烟柱底部（画面比例）。 */
  x: number; y: number;
  /** 往上飘多远（画面高度比例）。缺省 0.5。 */
  height?: number;
  /** 侧向风的强度（画面宽度比例）。缺省 0.06。 */
  wind?: number;
  color?: string;
  /** 团数。缺省 9。 */
  puffs?: number;
  /** 每团的最大半径（画面宽度比例）。缺省 0.11。 */
  size?: number;
  opacity?: number;
  /** 循环周期（秒）。缺省 5。 */
  period?: number;
  seed?: number | string;
}

/** 烟 / 蒸汽 / 雾柱：一叠渐大渐淡的模糊团块，循环上飘。
 *
 *  用叠团而不是流体着色器：容器里是软件 WebGL，一层流体能把一镜从 2 分钟拖到 20 分钟。 */
export const SmokePlume: React.FC<SmokePlumeProps> = ({
  x, y, height = 0.5, wind = 0.06, color = '#d9d2c6', puffs = 9, size = 0.11,
  opacity = 0.32, period = 5, seed = 'smoke',
}) => {
  const { width: W, height: H, fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const t = frame / Math.max(1, fps);
  const n = seedOf(seed);
  const p = Math.max(0.4, period);
  const items = Array.from({ length: Math.max(0, puffs) }, (_, i) => {
    const k = ((t / p + seeded(n + i * 4.7)) % 1 + 1) % 1;
    const cy = (y - k * height) * H;
    const cx = (x + Math.sin(k * Math.PI * 1.6 + i) * wind * 0.6 + k * wind) * W;
    const r = size * W * (0.35 + k * 1.25) * (0.8 + seeded(n + i * 8.3) * 0.4);
    // `opacity` 是**整柱**的浓度，不是每一团的：叠了十几团之后 alpha 会累加到一条
    // 实心白墙（真机 2026-10-22：一张 s02 的烟把整幅画面糊掉）。按团数开方分摊。
    const o = (opacity / Math.max(1, Math.sqrt(Math.max(1, puffs))))
      * Math.sin(Math.PI * k);
    return { key: i, cx, cy, r, o };
  });
  return (
    <svg width={W} height={H} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      <defs>
        <filter id={`smoke-${Math.round(n)}`} x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation={Math.max(8, W * 0.018)} />
        </filter>
      </defs>
      <g filter={`url(#smoke-${Math.round(n)})`}>
        {items.map((d) => (
          <ellipse key={d.key} cx={d.cx} cy={d.cy} rx={d.r} ry={d.r * 0.82}
            fill={color} opacity={d.o} />
        ))}
      </g>
    </svg>
  );
};

/* ------------------------------------------------------------------ */
/* 表面                                                                */
/* ------------------------------------------------------------------ */

export interface WaterSurfaceProps {
  /** 水面高度（画面比例 0–1）。 */
  y?: number;
  /** 水深（从 y 到底边）。缺省 0.45。 */
  depth?: number;
  /** 水的基色。 */
  color?: string;
  /** 高光色（天光、灯）。 */
  light?: string;
  /** 波纹幅度（像素）。缺省 6。 */
  amplitude?: number;
  /** 波纹行数。缺省 26。 */
  rows?: number;
  seed?: number | string;
}

/** 水面：一层渐变 + 一组正弦波线 + 随机分布的高光短横。
 *
 *  俯视/平视都能用；`y` 是水面与「空气」的分界。远景里的水靠 `rows` 少一点就够。 */
export const WaterSurface: React.FC<WaterSurfaceProps> = ({
  y = 0.62, depth = 0.38, color = '#0d1b24', light = '#cfe6ff',
  amplitude = 6, rows = 26, seed = 'water',
}) => {
  const { width: W, height: H, fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const t = frame / Math.max(1, fps);
  const n = seedOf(seed);
  const top = y * H;
  const bottom = Math.min(H, (y + depth) * H);
  const lines = Array.from({ length: Math.max(1, rows) }, (_, i) => {
    const k = i / Math.max(1, rows - 1);
    // 越近（越靠下）波越大、越疏：透视的正确写法，也让画面有纵深。
    const amp = amplitude * (0.25 + k * 1.4);
    const step = Math.max(8, W / (14 + k * 12));
    const pts: string[] = [];
    for (let x = -step; x <= W + step; x += step) {
      const yy = top + (bottom - top) * k
        + Math.sin(x / (step * 1.7) + t * (0.8 + k) + seeded(n + i) * 6) * amp;
      pts.push(`${x.toFixed(1)},${yy.toFixed(1)}`);
    }
    return { key: i, pts: pts.join(' '), o: 0.06 + k * 0.16 };
  });
  const specks = Array.from({ length: 40 }, (_, i) => {
    const k = seeded(n + i * 3.9);
    const cx = seeded(n + i * 6.1) * W;
    const cy = top + (bottom - top) * k;
    const len = 6 + seeded(n + i * 9.3) * (10 + k * 40);
    return { key: i, cx, cy, len, o: 0.10 + seeded(n + i * 2.7) * 0.35 };
  });
  return (
    <svg width={W} height={H} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      <defs>
        <linearGradient id={`water-${Math.round(n)}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity={0.92} />
          <stop offset="1" stopColor={color} stopOpacity={1} />
        </linearGradient>
      </defs>
      <rect x={0} y={top} width={W} height={Math.max(0, bottom - top)}
        fill={`url(#water-${Math.round(n)})`} />
      {lines.map((l) => (
        <polyline key={l.key} points={l.pts} fill="none" stroke={light}
          strokeWidth={1.2} opacity={l.o} />
      ))}
      {specks.map((s) => (
        <rect key={s.key} x={s.cx} y={s.cy} width={s.len} height={1.4}
          fill={light} opacity={s.o} />
      ))}
    </svg>
  );
};

/* ------------------------------------------------------------------ */
/* 后期                                                                */
/* ------------------------------------------------------------------ */

export interface BloomProps {
  children?: React.ReactNode;
  /** 光晕扩散半径（像素）。缺省 16。 */
  radius?: number;
  /** 0–1。缺省 0.5。 */
  intensity?: number;
  /** 屏幕混合 = 更亮更「发光」；`screen` 缺省。 */
  blend?: 'screen' | 'plus-lighter' | 'normal';
}

/** 高光溢出（bloom/halation）：把画面本身再叠一层模糊、提亮后的自己。
 *
 *  写实片的「亮得有空气感」九成来自这一步——没有它，光源再亮也是一块死白。
 *  代价很低（一层 CSS filter + mix-blend），可以整片开着。 */
export const Bloom: React.FC<BloomProps> = ({
  children, radius = 16, intensity = 0.5, blend = 'screen',
}) => (
  <AbsoluteFill>
    {children}
    <AbsoluteFill
      style={{
        filter: `blur(${radius}px) brightness(1.25)`,
        opacity: Math.max(0, Math.min(1, intensity)),
        mixBlendMode: blend,
        pointerEvents: 'none',
      }}
    >
      {children}
    </AbsoluteFill>
  </AbsoluteFill>
);

/* ------------------------------------------------------------------ */
/* 材质：表面细节                                                        */
/* ------------------------------------------------------------------ */

/** 一镜里最常见的几种表面。名字只回答「这是什么材料」，怎么摆、给谁用归这一镜。 */
export type SurfaceKind =
  | 'soil' | 'leaf' | 'bark' | 'cloth' | 'leather' | 'metal'
  | 'stone' | 'ceramic' | 'paper' | 'skin' | 'glass' | 'rust';

type SurfacePreset = {
  color: string;
  roughness: number;
  metalness: number;
  /** 起伏强度：bump 的 scale（世界单位量级，0.01~0.08 合适）。 */
  bump: number;
  /** 噪声颗粒的物理尺度：数值越大颗粒越碎。 */
  grain: number;
  /** 颜色受噪声影响的比例（0 = 纯色）。 */
  tint: number;
  repeats?: number;
};

const SURFACE_PRESETS: Record<SurfaceKind, SurfacePreset> = {
  soil: { color: '#241b14', roughness: 0.94, metalness: 0.02, bump: 0.035, grain: 26, tint: 0.22, repeats: 4 },
  leaf: { color: '#2f3d22', roughness: 0.58, metalness: 0.03, bump: 0.012, grain: 60, tint: 0.16, repeats: 3 },
  bark: { color: '#3a2a1c', roughness: 0.88, metalness: 0.02, bump: 0.03, grain: 22, tint: 0.2, repeats: 3 },
  cloth: { color: '#2b3038', roughness: 0.86, metalness: 0.0, bump: 0.014, grain: 90, tint: 0.1, repeats: 4 },
  leather: { color: '#3a2a22', roughness: 0.62, metalness: 0.04, bump: 0.008, grain: 110, tint: 0.12, repeats: 3 },
  metal: { color: '#8d939c', roughness: 0.34, metalness: 0.92, bump: 0.004, grain: 120, tint: 0.08, repeats: 2 },
  stone: { color: '#4b4b48', roughness: 0.84, metalness: 0.02, bump: 0.02, grain: 40, tint: 0.18, repeats: 3 },
  ceramic: { color: '#d9cfc0', roughness: 0.26, metalness: 0.0, bump: 0.003, grain: 160, tint: 0.04, repeats: 2 },
  paper: { color: '#e6ddc8', roughness: 0.92, metalness: 0.0, bump: 0.004, grain: 180, tint: 0.06, repeats: 3 },
  skin: { color: '#c08562', roughness: 0.55, metalness: 0.0, bump: 0.004, grain: 200, tint: 0.05, repeats: 2 },
  glass: { color: '#cfe0f0', roughness: 0.06, metalness: 0.0, bump: 0.0, grain: 220, tint: 0.02, repeats: 2 },
  rust: { color: '#7a4326', roughness: 0.78, metalness: 0.35, bump: 0.02, grain: 48, tint: 0.3, repeats: 3 },
};

/** 一张确定性的灰度噪声画布（0–255）。同一 seed 在任何机器上用同样的数算出同样的图，
 *  所以「同一份代码渲两次逐像素相同」这条纪律不受影响。 */
const noiseCanvas = (size: number, seed: number, grain: number): HTMLCanvasElement => {
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) return canvas;
  const img = ctx.createImageData(size, size);
  const base = Math.max(1, seed);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const i = (y * size + x) * 4;
      // 两个频率叠在一起：低频负责「成片的不均匀」，高频负责颗粒。
      const n1 = seeded(((x * grain) % 997) + ((y * grain * 1.7) % 991) + base);
      const n2 = seeded((x * 7 + y * 11) % 1009 + base * 3);
      const v = Math.round(255 * (0.62 * n1 + 0.38 * n2));
      img.data[i] = v;
      img.data[i + 1] = v;
      img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
};

const shade = (hex: string, k: number): string => {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => (
    Math.max(0, Math.min(255, Math.round(c * (1 + k))))
  ));
  return `#${ch.map((c) => c.toString(16).padStart(2, '0')).join('')}`;
};

/** 一块有表面细节的材质。用法是把返回值摊进 `<meshStandardMaterial>`：
 *
 *  ```tsx
 *  const soil = useSurface('soil');
 *  <meshStandardMaterial {...soil} />
 *  ```
 *
 *  为什么需要它：`<meshStandardMaterial color="#241b14" roughness={0.9} />` 渲出来是
 *  一块光滑的塑料——写实里判断「这是不是真的」的第一眼线索不是形状，是**同一表面上
 *  roughness 与颜色在像素级上的不均匀**。这里用确定性的噪声画布补这一层：
 *  颜色微差（tint）、粗糙度起伏、法线起伏（bump）。
 *  `grain` 控制颗粒粗细，`repeats` 控制花纹在物体上重复几次，两者按物体大小调。 */
const SURFACE_CACHE = new Map<string, {
  map: THREE.Texture; roughnessMap: THREE.Texture; bumpMap: THREE.Texture;
}>();

export const useSurface = (
  kind: SurfaceKind = 'stone',
  opts: { seed?: number; color?: string; repeats?: number; tint?: number } = {},
): {
  color: string; roughness: number; metalness: number;
  map?: THREE.Texture; roughnessMap?: THREE.Texture; bumpMap?: THREE.Texture; bumpScale?: number;
} => {
  const preset = SURFACE_PRESETS[kind] ?? SURFACE_PRESETS.stone;
  const seed = seedOf(opts.seed ?? kind);
  const repeats = opts.repeats ?? preset.repeats ?? 3;
  const tint = opts.tint ?? preset.tint;
  // Remotion 是**逐帧重新挂载**组件树的：写在 `useMemo` 里的贴图会每帧重算一遍
  // （三张 256² 的噪声画布 ≈ 每帧几十毫秒，成片直接多出几十分钟）。所以贴图放
  // 模块级缓存，按料名 + seed + 重复数 + 色偏取——同一镜的每一帧拿的是同一批对象。
  const cacheKey = `${kind}|${seed}|${repeats}|${tint}`;
  const tex = SURFACE_CACHE.get(cacheKey) ?? buildSurface(
    cacheKey, preset, seed, repeats, tint);
  return {
    color: opts.color ?? preset.color,
    roughness: preset.roughness,
    metalness: preset.metalness,
    map: tex.map,
    roughnessMap: tex.roughnessMap,
    bumpMap: tex.bumpMap,
    bumpScale: preset.bump,
  };
};

/** 真正画那三张贴图。放模块级缓存的外面：Remotion 是**逐帧重新挂载**组件树的，
 *  贴图若用 `useMemo` 就是每帧重算三张 256² 噪声画布（几十毫秒 × 几千帧）。 */
const buildSurface = (
  cacheKey: string, preset: SurfacePreset, seed: number, repeats: number, tint: number,
) => {
  const height = noiseCanvas(256, seed, Math.max(2, preset.grain) / 4);
  const rough = noiseCanvas(256, seed + 17, Math.max(4, preset.grain));
  const setRepeat = (t: THREE.Texture) => {
    t.wrapS = THREE.RepeatWrapping;
    t.wrapT = THREE.RepeatWrapping;
    t.repeat.set(repeats, repeats);
    t.anisotropy = 4;
    return t;
  };
  const h = setRepeat(new THREE.CanvasTexture(height));
  const r = setRepeat(new THREE.CanvasTexture(rough));
  // 颜色图：底色 + 极轻的噪声纹理（只用来打破「一块纯色」）。
  const hue = noiseCanvas(256, seed + 29, Math.max(6, preset.grain / 2));
  const hctx = hue.getContext('2d');
  const tex = setRepeat(new THREE.CanvasTexture(hue));
  tex.colorSpace = THREE.SRGBColorSpace;
  if (hctx) {
    hctx.globalCompositeOperation = 'multiply';
    hctx.fillStyle = shade(preset.color, tint);
    hctx.fillRect(0, 0, 256, 256);
    tex.needsUpdate = true;
  }
  h.colorSpace = THREE.NoColorSpace;
  r.colorSpace = THREE.NoColorSpace;
  const out = { map: tex, roughnessMap: r, bumpMap: h };
  SURFACE_CACHE.set(cacheKey, out);
  return out;
};

/** 带倒角的方盒几何：写实里几乎没有真正的直角，倒角把高光拉成一条细线，
 *  这是「塑料玩具」与「一个真的物件」之间最便宜的那一步。
 *
 *  ```tsx
 *  <mesh><BevelGeometry args={[0.4, 0.2, 0.12]} /></mesh>
 *  ```
 *  参数：宽、高、深、倒角半径（缺省 0.012）、分段（缺省 3）。 */
const BEVEL_CACHE = new Map<string, THREE.BufferGeometry>();

export const BevelGeometry: React.FC<{
  args?: [number, number, number]; radius?: number; segments?: number;
}> = ({ args = [1, 1, 1], radius = 0.012, segments = 3 }) => {
  const gkey = `${args.join('x')}|${radius}|${segments}`;
  let geometry = BEVEL_CACHE.get(gkey);
  if (!geometry) {
    const [w, h, d] = args;
    const r = Math.min(radius, Math.min(w, h, d) / 2.2);
    const g = new THREE.BoxGeometry(w, h, d, segments, segments, segments);
    // 用位移把顶点推到「圆角盒」上：沿法线内缩 r，再补一次球面外推。
    // 不引 `three/examples` 的 RoundedBoxGeometry：那一份还会把 UV 重排一遍，
    // 和我们用的程序化贴图不在同一个尺度上。
    const pos = g.attributes.position as THREE.BufferAttribute;
    const v = new THREE.Vector3();
    const half = new THREE.Vector3(w / 2 - r, h / 2 - r, d / 2 - r);
    for (let i = 0; i < pos.count; i += 1) {
      v.fromBufferAttribute(pos, i);
      const clamped = new THREE.Vector3(
        Math.max(-half.x, Math.min(half.x, v.x)),
        Math.max(-half.y, Math.min(half.y, v.y)),
        Math.max(-half.z, Math.min(half.z, v.z)),
      );
      const dir = v.clone().sub(clamped);
      const len = dir.length();
      if (len > 1e-6) {
        dir.multiplyScalar(r / len);
        v.copy(clamped).add(dir);
      }
      pos.setXYZ(i, v.x, v.y, v.z);
    }
    pos.needsUpdate = true;
    g.computeVertexNormals();
    geometry = g;
    BEVEL_CACHE.set(gkey, g);
  }
  return <primitive object={geometry} attach="geometry" />;
};

/* ------------------------------------------------------------------ */
/* 景深                                                                */
/* ------------------------------------------------------------------ */

export interface DepthOfFieldProps {
  /** 合焦处（画面比例 0–1）。通常就是主体/脸/手在画面里的位置。 */
  focus?: { x: number; y: number };
  /** 清晰区半径（画面宽度比例）。微距 0.18、中景 0.34、广角 0.5。 */
  radius?: number;
  /** 边缘虚化强度（像素）。8–18 是「浅景深」，3–6 是「轻微虚化」。 */
  blur?: number;
  /** 清晰→虚的过渡宽度（画面宽度比例）。缺省 0.16；太小会看到一圈硬边。 */
  falloff?: number;
}

/** 焦外虚化：把**合焦区以外**的画面模糊掉。
 *
 *  为什么要有它：真实照片的「像被拍到」很大一半来自景深——主体锐利、前后景化开。
 *  三维层里做真正的 DOF 要读深度缓冲，软件 WebGL 上太贵；屏幕层用一层带径向蒙版
 *  的 `backdrop-filter` 就够：合焦处完全不动，离开合焦处逐渐糊掉，边缘再叠回颗粒与
 *  暗角，读起来就是浅景深。放在 `SurrealWorld` 的 children 里（它认 `domLayer`），
 *  会和其他屏幕层元素一起浮在三维画面上。
 *
 *  ```tsx
 *  <DepthOfField focus={{ x: 0.46, y: 0.52 }} radius={0.26} blur={12} />
 *  ```
 *
 *  一镜只用一层：两层叠加会让边缘糊成一片，反而像没对上焦。 */
export const DepthOfField: React.FC<DepthOfFieldProps> = ({
  focus = { x: 0.5, y: 0.5 }, radius = 0.34, blur = 10, falloff = 0.16,
}) => {
  const { width } = useVideoConfig();
  const r = Math.max(0.02, Math.min(0.92, radius)) * width;
  const f = Math.max(0.01, falloff) * width;
  const fx = Math.max(0, Math.min(1, focus.x)) * 100;
  const fy = Math.max(0, Math.min(1, focus.y)) * 100;
  const b = Math.max(0, Math.min(40, blur));
  const mask = `radial-gradient(circle ${r}px at ${fx}% ${fy}%,`
    + ` transparent 0, transparent ${r}px, black ${r + f}px, black 100%)`;
  if (b <= 0) return null;
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        pointerEvents: 'none',
        backdropFilter: `blur(${b}px)`,
        WebkitBackdropFilter: `blur(${b}px)`,
        maskImage: mask,
        WebkitMaskImage: mask,
      }}
    />
  );
};
// 让 `SurrealWorld` 把它挪到屏幕层（与 `<svg>`/`<div>` 同一档），而不是塞进 three 场景。
(DepthOfField as unknown as { domLayer?: boolean }).domLayer = true;

/* ------------------------------------------------------------------ */
/* 人形能力件（同一模块再导出，写手仍然只 import 这一个路径）          */
/* ------------------------------------------------------------------ */
/*
 * 为什么放在这里而不是让写手 import 第二个模块：
 *
 *   ① 白名单是「每个路径一条」，多一个路径就要同时改 `remotion/lint-rules.mjs`
 *      与 `backend/storyline_server/motion/code_spec.py` 两份镜像名单；少改一处
 *      的表现是**整镜代码被拒**，而模型只会看到「import 不在白名单」再猜一轮。
 *   ② 模板行的 `module` 只有 `@dg/lib/style/surreal` 一条：组件清单里点到
 *      `HumanFigure` 时，写手会按模板模块去 import；从同一个模块导出，清单里
 *      的每一件都能真的拿到。
 *
 * 件本身在 `./human.tsx`：那一份只回答「人怎么站、手怎么扣、脸朝哪」，
 * 与光/烟/粒子/材质互不依赖，可以单独拿去别的风格里用。
 */
export {
  HumanFigure,
  HumanHand,
  HumanHead,
  ContactShadow,
  useSkin,
  useCloth,
} from './human';
export type {
  HumanFigureProps,
  HumanHandProps,
  HumanHeadProps,
  ContactShadowProps,
  HumanPoseName,
  HumanJoints,
  HumanBuild,
  SkinMaterial,
  ClothMaterial,
} from './human';

/*
 * 2.5D 人形（2026-10-22）：三维那三件做的是「有体积的人」，用户拿参考片一比就说
 * 「圆头和其他地方和真人根本不像」「连人影都这么写实」。参考片里的人靠的是侧面
 * 外轮廓，不是表面细节，所以另起一件横切面的人：`silhouette` 剪影 / `wash` 上色 /
 * `line` 线描，姿势遍布站坐蹲跪指伸抱靠，会走的姿势复用同一组步态常数。
 *
 * 它标了 `domLayer`，放进 `<SurrealWorld>` 会落到屏幕层（和字幕/颗粒同层），
 * 不再被分进 three 场景。三维那三件保留给真正需要体积的镜头，不删。
 */
export { HumanSketch } from './human-sketch';
export type { HumanSketchProps, SketchPose, SketchRender, SketchOutfit } from './human-sketch';
