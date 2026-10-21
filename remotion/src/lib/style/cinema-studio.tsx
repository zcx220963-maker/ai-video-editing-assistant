/**
 * `@dg/lib/style/cinema-studio` —— 「暗房微距」模板（2026-10-21）。
 *
 * 这套风格包的视觉契约只有一句话：**一件实物，放在一张深色台面上，被一束真实的
 * 光打亮**。不是示意图，不是卡片：有体积、有高光、有投影、有景深、有颗粒。
 *
 * 为什么要有这一件：要求「写实」时，写手从零搭三维场景的失败率高得离谱——比例、
 * 光位、材质、色彩管理四件事每一件都能毁掉一镜，而且四镜之间一定漂。模板把
 * **舞台（背景 / 台面 / 灯光 / 相机 / 调色）、材质与颗粒**写成同一份实现，镜头代码
 * 只摆这一镜的实物与它的运动。与 `silhouette-history` 同一条纪律：模板负责一致性，
 * 写手负责内容。
 *
 * 世界坐标：1 单位 = 1 米。台面在 y = 0，主体落在 y 0.1–0.6、x ±0.5 之间。
 * 缺省相机在 (-0.62, 0.52, 1.42) 看向 (0.04, 0.25, 0)，约等于 50mm 微距视角。
 *
 * 纪律：不许 `Math.random`、不许 `Date.now()`；所有纹理、颗粒、粒子都由固定种子
 * 生成，同一份代码渲两次逐像素相同。所有材质都是程序化的，不读外部图片。
 */
import React, { useMemo } from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { useBody } from '@dg/film';
import type { Aspect } from '@dg/tokens';

/** 确定性伪随机。`i` 相同 → 值相同，跨进程、跨次渲染都一致。 */
export const seeded = (i: number): number => {
  const x = Math.sin(i * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};

/** 台面材质：木纹 / 石板 / 瓷砖 / 纯深色。 */
export type FloorKind = 'wood' | 'stone' | 'tile' | 'dark';
/** 光位：夜里的暖灯 / 清晨的窗光 / 诊室的白光。 */
export type LightKind = 'night' | 'dawn' | 'clinical';
/** 运镜：静止 / 缓慢推近 / 横移 / 绕拍 / 手持微晃。 */
export type CameraKind = 'still' | 'push' | 'pan' | 'orbit' | 'handheld';

/* ------------------------------------------------------------------ */
/* 程序化贴图                                                          */
/* ------------------------------------------------------------------ */

/** 木纹：底下一条渐变，上面几百条年轮曲线，再撒一层细点。 */
export function makeWoodTexture(w = 1024, h = 1024): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d')!;
  const base = ctx.createLinearGradient(0, 0, 0, h);
  base.addColorStop(0, '#5c452f');
  base.addColorStop(0.5, '#6a5037');
  base.addColorStop(1, '#4f3b29');
  ctx.fillStyle = base;
  ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 220; i += 1) {
    const y0 = seeded(i * 3 + 1) * h;
    const amp = 3 + seeded(i * 3 + 2) * 16;
    const freq = 0.004 + seeded(i * 3 + 3) * 0.014;
    ctx.beginPath();
    for (let x = 0; x <= w; x += 6) {
      const y = y0 + Math.sin(x * freq + i) * amp;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    const r = 38 + Math.floor(seeded(i * 5) * 30);
    const g = 22 + Math.floor(seeded(i * 7) * 20);
    ctx.strokeStyle = `rgba(${r},${g},10,${0.05 + seeded(i * 11) * 0.13})`;
    ctx.lineWidth = 0.8 + seeded(i * 13) * 2.2;
    ctx.stroke();
  }
  for (let i = 0; i < 2600; i += 1) {
    ctx.globalAlpha = seeded(i * 7) * 0.10;
    ctx.fillStyle = 'rgba(0,0,0,0.05)';
    ctx.beginPath();
    ctx.arc(seeded(i * 3 + 1) * w, seeded(i * 3 + 2) * h,
            0.5 + seeded(i * 3 + 3) * 1.4, 0, 6.283);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 石板/水泥：大块的斑与细密颗粒。 */
export function makeStoneTexture(w = 1024, h = 1024): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d')!;
  ctx.fillStyle = '#4a4844';
  ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 900; i += 1) {
    const x = seeded(i * 5 + 1) * w;
    const y = seeded(i * 5 + 2) * h;
    const r = 4 + seeded(i * 5 + 3) * 40;
    const v = 40 + Math.floor(seeded(i * 5 + 4) * 45);
    ctx.fillStyle = `rgba(${v},${v - 1},${v - 4},${0.04 + seeded(i * 5 + 5) * 0.10})`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 6.283);
    ctx.fill();
  }
  for (let i = 0; i < 6000; i += 1) {
    const v = 70 + Math.floor(seeded(i * 3) * 70);
    ctx.fillStyle = `rgba(${v},${v},${v},${0.05 + seeded(i * 7) * 0.16})`;
    ctx.fillRect(seeded(i * 11) * w, seeded(i * 13) * h, 1.5, 1.5);
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 瓷砖：一张方砖 + 一道灰缝，靠 repeat 铺满台面。 */
export function makeTileTexture(w = 512): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = w;
  const ctx = c.getContext('2d')!;
  ctx.fillStyle = '#2b2b2c';
  ctx.fillRect(0, 0, w, w);
  const tile = w / 2 - 6;
  for (let ty = 0; ty < 2; ty += 1) {
    for (let tx = 0; tx < 2; tx += 1) {
      const x = tx * (w / 2) + 5;
      const y = ty * (w / 2) + 5;
      const g = ctx.createLinearGradient(x, y, x + tile, y + tile);
      g.addColorStop(0, '#6d6a64');
      g.addColorStop(0.5, '#5b5954');
      g.addColorStop(1, '#504e4a');
      ctx.fillStyle = g;
      ctx.fillRect(x, y, tile, tile);
      for (let i = 0; i < 220; i += 1) {
        ctx.fillStyle = `rgba(255,255,255,${seeded(i + tx * 300 + ty * 700) * 0.05})`;
        ctx.fillRect(x + seeded(i * 3 + tx) * tile, y + seeded(i * 5 + ty) * tile,
                     1 + seeded(i * 7) * 2, 1 + seeded(i * 11) * 2);
      }
    }
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 颗粒质感的贴图（盐、糖、沙、灰）。`dark` 控制基底明度。 */
export function makeGrainTexture(dark = 0.78): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = 512;
  const ctx = c.getContext('2d')!;
  const b = Math.round(255 * dark);
  ctx.fillStyle = `rgb(${b - 12},${b - 16},${b - 24})`;
  ctx.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 9000; i += 1) {
    const v = 140 + Math.floor(seeded(i) * 100);
    ctx.fillStyle = `rgba(${v},${v - 4},${v - 12},${0.25 + seeded(i * 3) * 0.6})`;
    ctx.beginPath();
    ctx.arc(seeded(i * 7) * 512, seeded(i * 11) * 512,
            0.6 + seeded(i * 5) * 1.8, 0, 6.283);
    ctx.fill();
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(2, 4);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 平滑的值噪声：给粗糙度与法线一层连贯的起伏，不是电视雪花。 */
function valueNoise(x: number, y: number, scale: number, seed: number): number {
  const gx = x / scale;
  const gy = y / scale;
  const x0 = Math.floor(gx);
  const y0 = Math.floor(gy);
  const fx = gx - x0;
  const fy = gy - y0;
  const sx = fx * fx * (3 - 2 * fx);
  const sy = fy * fy * (3 - 2 * fy);
  const h = (a: number, b: number) => seeded(a * 374761 + b * 668265 + seed * 977);
  const top = h(x0, y0) * (1 - sx) + h(x0 + 1, y0) * sx;
  const bot = h(x0, y0 + 1) * (1 - sx) + h(x0 + 1, y0 + 1) * sx;
  return top * (1 - sy) + bot * sy;
}

/** 粗糙度贴图：材质上的油渍、水痕、磨损。没有它，整套东西都是均匀的塑料。 */
export function makeRoughTexture(seed = 3, base = 0.62): THREE.CanvasTexture {
  const s = 256;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const ctx = c.getContext('2d')!;
  const img = ctx.createImageData(s, s);
  for (let y = 0; y < s; y += 1) {
    for (let x = 0; x < s; x += 1) {
      const n = 0.6 * valueNoise(x, y, 22, seed)
        + 0.3 * valueNoise(x, y, 6, seed + 11)
        + 0.1 * seeded(x * 3 + y * 131 + seed);
      const v = Math.max(0, Math.min(255, Math.round(255 * (base + (n - 0.5) * 0.42))));
      const i = (y * s + x) * 4;
      img.data[i] = v; img.data[i + 1] = v; img.data[i + 2] = v; img.data[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  return tex;
}

/** 法线贴图：从同一层噪声求梯度，让受光面出现细小的凹凸（釉面、木纹、金属拉丝）。 */
export function makeNormalTexture(seed = 3, strength = 1.4): THREE.CanvasTexture {
  const s = 256;
  const h = new Float32Array(s * s);
  for (let y = 0; y < s; y += 1) {
    for (let x = 0; x < s; x += 1) {
      h[y * s + x] = 0.55 * valueNoise(x, y, 20, seed)
        + 0.32 * valueNoise(x, y, 5, seed + 7)
        + 0.13 * seeded(x * 5 + y * 271 + seed);
    }
  }
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const ctx = c.getContext('2d')!;
  const img = ctx.createImageData(s, s);
  for (let y = 0; y < s; y += 1) {
    for (let x = 0; x < s; x += 1) {
      const xl = h[y * s + Math.max(0, x - 1)];
      const xr = h[y * s + Math.min(s - 1, x + 1)];
      const yt = h[Math.max(0, y - 1) * s + x];
      const yb = h[Math.min(s - 1, y + 1) * s + x];
      const nx = (xl - xr) * strength;
      const ny = (yt - yb) * strength;
      const i = (y * s + x) * 4;
      img.data[i] = Math.round(128 + nx * 127);
      img.data[i + 1] = Math.round(128 + ny * 127);
      img.data[i + 2] = 255;
      img.data[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  return tex;
}

/** 环境反射：夜色窗户一侧、暖灯另一侧。金属与玻璃靠它才有「活」的高光。 */
export function makeEnvTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = 512; c.height = 256;
  const ctx = c.getContext('2d')!;
  const sky = ctx.createLinearGradient(0, 0, 0, 256);
  sky.addColorStop(0, '#0b1421');
  sky.addColorStop(0.5, '#1d2733');
  sky.addColorStop(1, '#0a0b0d');
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, 512, 256);
  const cool = ctx.createRadialGradient(80, 92, 4, 80, 92, 110);
  cool.addColorStop(0, 'rgba(170,205,255,1)');
  cool.addColorStop(1, 'rgba(170,205,255,0)');
  ctx.fillStyle = cool;
  ctx.fillRect(0, 0, 230, 240);
  const warm = ctx.createRadialGradient(400, 90, 6, 400, 90, 120);
  warm.addColorStop(0, 'rgba(255,186,120,1)');
  warm.addColorStop(1, 'rgba(255,186,120,0)');
  ctx.fillStyle = warm;
  ctx.fillRect(250, 0, 262, 250);
  const tex = new THREE.CanvasTexture(c);
  tex.mapping = THREE.EquirectangularReflectionMapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 墙：大块的抹灰斑与细颗粒，压得很暗，只为在背景里留一层可读的质地。 */
export function makePlasterTexture(w = 768, h = 768): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = w;
  const ctx = c.getContext('2d')!;
  ctx.fillStyle = '#232120';
  ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 700; i += 1) {
    const x = seeded(i * 5 + 1) * w;
    const y = seeded(i * 5 + 2) * h;
    const r = 6 + seeded(i * 5 + 3) * 54;
    const v = 26 + Math.floor(seeded(i * 5 + 4) * 22);
    ctx.fillStyle = `rgba(${v},${v - 1},${v - 2},${0.05 + seeded(i * 5 + 5) * 0.12})`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 6.283);
    ctx.fill();
  }
  for (let i = 0; i < 4200; i += 1) {
    const v = 40 + Math.floor(seeded(i * 3) * 60);
    ctx.fillStyle = `rgba(${v},${v},${v},${0.03 + seeded(i * 7) * 0.10})`;
    ctx.fillRect(seeded(i * 11) * w, seeded(i * 13) * h, 1.4, 1.4);
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 接触阴影贴图：物体底下一圈柔软的暗。 */
export function makeContactTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = 256;
  const ctx = c.getContext('2d')!;
  const g = ctx.createRadialGradient(128, 128, 10, 128, 128, 126);
  g.addColorStop(0, 'rgba(0,0,0,0.62)');
  g.addColorStop(0.55, 'rgba(0,0,0,0.26)');
  g.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 256, 256);
  return new THREE.CanvasTexture(c);
}

/* ------------------------------------------------------------------ */
/* 相机                                                                */
/* ------------------------------------------------------------------ */

const CAM_PATH: Record<CameraKind, { from: [number, number, number]; to: [number, number, number];
                                    look: [number, number, number] }> = {
  still: { from: [-0.62, 0.52, 1.42], to: [-0.62, 0.52, 1.42], look: [0.04, 0.25, 0] },
  push: { from: [-0.62, 0.52, 1.46], to: [-0.50, 0.50, 1.26], look: [0.04, 0.25, 0] },
  pan: { from: [-0.86, 0.50, 1.40], to: [-0.34, 0.52, 1.40], look: [0.04, 0.25, 0] },
  orbit: { from: [-0.75, 0.55, 1.30], to: [0.62, 0.50, 1.30], look: [0.02, 0.24, 0] },
  handheld: { from: [-0.64, 0.53, 1.42], to: [-0.58, 0.50, 1.36], look: [0.04, 0.25, 0] },
};

/** 镜头代码可以自己给一个更紧的取景（`cam`），不给就走预设的那条轨。 */
export interface StudioCam {
  from?: [number, number, number];
  to?: [number, number, number];
  look?: [number, number, number];
}

const Rig: React.FC<{
  kind: CameraKind; t: number; frame: number; exposure: number; cam?: StudioCam;
}> = ({ kind, t, frame, exposure, cam }) => {
  const { camera, gl } = useThree();
  // ACES：把高光压住、暗部抬一点，画面才像摄影而不是三维软件的直接输出。
  gl.toneMapping = THREE.ACESFilmicToneMapping;
  gl.toneMappingExposure = exposure;
  const path = CAM_PATH[kind];
  const from = cam?.from ?? path.from;
  const to = cam?.to ?? cam?.from ?? path.to;
  const look = cam?.look ?? path.look;
  const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
  let x = lerp(from[0], to[0], t);
  let y = lerp(from[1], to[1], t);
  const z = lerp(from[2], to[2], t);
  if (kind === 'handheld') {
    // 手持的呼吸感：两个不同频率的正弦，幅度不到 1 厘米
    x += Math.sin(frame / 23) * 0.008 + Math.sin(frame / 9) * 0.003;
    y += Math.cos(frame / 31) * 0.005;
  }
  camera.position.set(x, y, z);
  camera.lookAt(look[0], look[1], look[2]);
  camera.updateProjectionMatrix();
  return null;
};

/* ------------------------------------------------------------------ */
/* 材质                                                                */
/* ------------------------------------------------------------------ */

/** 玻璃：清、透、有边缘高光。用于瓶、杯、窗。 */
export const GlassMaterial: React.FC<{
  color?: string; opacity?: number; thickness?: number; env?: number;
}> = ({ color = '#e8f3f5', opacity = 0.22, thickness = 0.14, env = 2.6 }) => (
  <meshPhysicalMaterial
    color={color}
    roughness={0.05}
    metalness={0}
    transparent
    opacity={opacity}
    envMapIntensity={env}
    clearcoat={1}
    clearcoatRoughness={0.04}
    // 容器里是 swangle；transmission 折射在那一档不稳，靠透明度 + 环境反射装出来。
    transmission={0}
    thickness={thickness}
    ior={1.5}
  />
);

/** 金属：不锈钢 / 黄铜 / 铝。`tone` 决定色温，`rough` 决定磨砂还是镜面。 */
export const MetalMaterial: React.FC<{
  tone?: 'steel' | 'brass' | 'aluminum';
  rough?: number; env?: number; seed?: number;
}> = ({ tone = 'steel', rough = 0.22, env = 1.8, seed = 9 }) => {
  const color = tone === 'brass' ? '#c8ab72' : tone === 'aluminum' ? '#c9c9c7' : '#dfe4e8';
  const wear = useMemo(() => {
    const r = makeRoughTexture(seed, 0.55);
    r.repeat.set(2, 2);
    return r;
  }, [seed]);
  return (
    <meshStandardMaterial color={color} roughness={rough} metalness={0.95}
                          envMapIntensity={env} roughnessMap={wear} />
  );
};

/** 陶瓷 / 纸 / 粉质固体：釉面上的水痕与极浅的起伏，靠粗糙度和环境反射。 */
export const CeramicMaterial: React.FC<{
  color?: string; rough?: number; seed?: number;
}> = ({ color = '#e9e4da', rough = 0.34, seed = 5 }) => {
  const maps = useMemo(() => {
    const r = makeRoughTexture(seed, 0.7);
    r.repeat.set(2, 2);
    const n = makeNormalTexture(seed, 0.8);
    n.repeat.set(4, 4);
    return { r, n };
  }, [seed]);
  return (
    <meshStandardMaterial color={color} roughness={rough} metalness={0}
                          envMapIntensity={1.1} roughnessMap={maps.r}
                          normalMap={maps.n} normalScale={new THREE.Vector2(0.1, 0.1)} />
  );
};

/** 颗粒堆积物（盐、糖、沙、灰）：贴图 + 高粗糙度 + 凹凸，不是发光的白柱。 */
export const GranularMaterial: React.FC<{
  dark?: number; color?: string; rough?: number; seed?: number;
}> = ({ dark = 0.78, color = '#e6e0d2', rough = 1, seed = 17 }) => {
  const maps = useMemo(() => {
    const map = makeGrainTexture(dark);
    const n = makeNormalTexture(seed, 2.2);
    n.repeat.set(3, 3);
    return { map, n };
  }, [dark, seed]);
  return <meshStandardMaterial map={maps.map} color={color} roughness={rough}
                               metalness={0} normalMap={maps.n}
                               normalScale={new THREE.Vector2(0.9, 0.9)} />;
};

/** 木材：贴图在组件里生成，两次挂载共用一个纹理。 */
export const WoodMaterial: React.FC<{
  color?: string; rough?: number; repeat?: number; seed?: number;
}> = ({ color = '#8a6647', rough = 0.5, repeat = 1.6, seed = 13 }) => {
  const maps = useMemo(() => {
    const tex = makeWoodTexture();
    tex.repeat.set(repeat, repeat);
    const n = makeNormalTexture(seed, 1.2);
    n.repeat.set(repeat * 2, repeat * 2);
    return { tex, n };
  }, [repeat, seed]);
  return <meshStandardMaterial map={maps.tex} color={color} roughness={rough}
                               metalness={0.02} envMapIntensity={1}
                               normalMap={maps.n}
                               normalScale={new THREE.Vector2(0.12, 0.12)} />;
};

/** 接触阴影：贴在台面上的软暗斑，`x/z` 是位置，`size` 是直径。 */
export const ContactShadow: React.FC<{
  x?: number; z?: number; y?: number; size?: number; opacity?: number;
}> = ({ x = 0, z = 0, y = 0.0015, size = 0.4, opacity = 0.75 }) => {
  const map = useMemo(() => makeContactTexture(), []);
  return (
    <mesh position={[x, y, z]} rotation={[-Math.PI / 2, 0, 0]}>
      <planeGeometry args={[size, size]} />
      <meshBasicMaterial map={map} transparent opacity={opacity} depthWrite={false} />
    </mesh>
  );
};

/* ------------------------------------------------------------------ */
/* 粒子                                                                */
/* ------------------------------------------------------------------ */

/** 浮尘：极暗的小点，慢慢飘。给空气一层体积感，别调亮。 */
export const Dust: React.FC<{
  count?: number; area?: [number, number, number]; t?: number;
  color?: string; size?: number; opacity?: number;
}> = ({ count = 64, area = [1.8, 0.8, 1.4], t = 0, color = '#ffd9a8',
       size = 0.008, opacity = 0.35 }) => {
  const geo = useMemo(() => {
    const arr = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      arr[i * 3] = (seeded(i * 3 + 101) - 0.5) * area[0];
      arr[i * 3 + 1] = 0.05 + seeded(i * 3 + 102) * area[1];
      arr[i * 3 + 2] = -0.4 + (seeded(i * 3 + 103) - 0.5) * area[2];
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(arr, 3));
    return g;
  }, [count, area[0], area[1], area[2]]);
  return (
    <points geometry={geo} position={[0, 0.04 * t, 0]}>
      <pointsMaterial size={size} color={color} transparent opacity={opacity}
                      sizeAttenuation depthWrite={false} />
    </points>
  );
};

/** 散落的小颗粒（盐粒、糖霜、纸屑、灰）：确定性分布，可选中心与半径。 */
export const Scatter: React.FC<{
  count?: number; cx?: number; cz?: number; radius?: number; y?: number;
  size?: number; color?: string; spread?: number;
  /** 堆成一个小丘：中心高 `dome` 米，向外落到 `y`。盐堆、糖霜、沙丘用它。 */
  dome?: number;
}> = ({ count = 140, cx = 0, cz = 0.08, radius = 0.3, y = 0.002,
       size = 0.005, color = '#f4f1ea', spread = 0.55, dome = 0 }) => {
  const grains = useMemo(() => {
    const out: { p: [number, number, number]; s: number; r: number }[] = [];
    for (let i = 0; i < count; i += 1) {
      const a = seeded(i * 2 + 11) * Math.PI * 2;
      const d = radius * (0.2 + seeded(i * 2 + 12) * 0.8);
      const lift = 1 - Math.min(1, d / Math.max(0.0001, radius));
      out.push({
        p: [cx + Math.cos(a) * d,
            y + seeded(i * 2 + 13) * 0.003 + dome * lift * lift,
            cz + Math.sin(a) * d * spread],
        s: size * (0.6 + seeded(i * 7 + 5) * 1.1),
        r: seeded(i * 9 + 7) * Math.PI,
      });
    }
    return out;
  }, [count, cx, cz, radius, y, size, spread, dome]);
  return (
    <group>
      {grains.map((g, i) => (
        <mesh key={i} position={g.p} rotation={[g.r, g.r * 1.7, g.r * 0.4]}>
          <octahedronGeometry args={[g.s, 0]} />
          <meshStandardMaterial color={color} roughness={0.42} metalness={0} />
        </mesh>
      ))}
    </group>
  );
};

/* ------------------------------------------------------------------ */
/* 舞台                                                                */
/* ------------------------------------------------------------------ */

/** 按节点类型把 `children` 分成「三维内容」和「屏幕层 DOM」。
 *
 *  为什么需要这一步：`<svg>` / `<div>` 这类 DOM 节点**不能**放进 `<ThreeCanvas>`，
 *  React Three Fiber 会把未知标签当成 THREE 对象，渲染时直接抛
 *  `R3F: Stop is not part of the THREE namespace`（`<stop>` 是 SVG 渐变里的节点）。
 *  2026-10-21 真机 run `14bc690bb035`：50 镜全部用 `<svg>` 画实物、且都放在
 *  `StudioStage` 的 children 里，于是 50 张预览静帧一张都没出来，整条 run 停摆。
 *
 *  分流规则只看节点类型，不猜内容：字符串标签（svg/div/span…）走画布上方的
 *  DOM 层（颗粒、暗角、调色仍会盖在它上面）；组件（材质、mesh、primitive）仍进
 *  Canvas。Fragment 会被展开，免得 `<>…</>` 把里面全判错。
 */
const splitStageChildren = (children: React.ReactNode) => {
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

/** 这个组件类型画的是 DOM 还是三维。
 *
 *  字符串标签（svg/div/span…）当然是 DOM。组件则要看它**自己声明的**归属：
 *  `Mark` / `LineDraw` / `Screen` 这类画 `<svg>` 的组件挂了 `domLayer` 标记。
 *  只看 `typeof type === 'string'` 是不够的——2026-10-21 真机 run `778575d9b8d0`
 *  23 镜全部把 `<Mark>` 直接放在 `StudioStage` 下（风格包正文就是这么教的），
 *  它虽然是组件，画的却是一张 `<svg>`，进了 Canvas 就抛
 *  `R3F: Svg is not part of the THREE namespace`，23 张预览静帧一张没出来。
 *
 *  自己的组件要画 `<svg>` / `<div>` 时，用 `<Screen>` 包一层，或者给它加
 *  `domLayer = true` 静态标记——两条路都在下面 `Screen` 的注释里。 */
type DomLayerType = { domLayer?: boolean };
const isDomLayerType = (type: unknown): boolean =>
  typeof type === 'string'
  || Boolean((type as DomLayerType | null)?.domLayer);

export interface StudioStageProps {
  /** 这一镜的三维内容（实物、装置）。台面在 y=0。
   *  `<svg>` / `<div>` 这类 DOM 节点会自动挪到画布上方的屏幕层（见 splitStageChildren）。 */
  children?: React.ReactNode;
  /** 屏幕固定层（标注、引线、大字），不跟相机走。 */
  overlay?: React.ReactNode;
  aspect?: Aspect;
  /** 运镜。缺省 `push`（缓慢推近，最不容易出错）。 */
  camera?: CameraKind;
  /** 光位。缺省 `night`。 */
  light?: LightKind;
  /** 台面。缺省 `wood`。 */
  floor?: FloorKind;
  /** 雾的浓度 0~1，0 = 关。缺省 0.45。 */
  haze?: number;
  /** 颗粒强度 0~1，0 = 不叠。缺省 0.14。 */
  grain?: number;
  /** 暗角强度 0~1。缺省 0.55。 */
  vignette?: number;
  /** 背景虚化光斑，缺省开。 */
  bokeh?: boolean;
  /** 曝光。缺省 1.0；换浅色台面时压到 0.8 左右。 */
  exposure?: number;
  /** 台面粉色（乘在贴图上）。缺省按 floor 取。 */
  floorTint?: string;
  /** 背后那面墙，缺省开。关掉是纯黑无边，只适合极简特写。 */
  wall?: boolean;
  /** 墙上的窗与灯光，缺省开；两者一起决定房间的纵深。 */
  window?: boolean;
  lamp?: boolean;
  /** 自定义取景：`from` 起点、`to` 终点（缺省不动）、`look` 看向哪。 */
  cam?: StudioCam;
  /** 从左上斜下来的一束空气光（屏幕层，不参与三维），缺省开。 */
  beam?: boolean;
  /** 三维画布的内部像素比（1 = 与输出同分辨率）。缺省 1。
   *
   *  这是**整帧最贵的一段**：软件 WebGL 上这一层约 1.4 s/帧，其余（矢量内容 + 颗粒 +
   *  暗角 + 光斑）加起来才 0.4 s/帧（2026-10-21 实测，1920×1080，24 帧一镜）。
   *  缺省 0.7：把三维层按 1344×756 渲再放大到 1920——**屏幕层（模型画的矢量
   *  画面、字幕）仍是满分辨率**。实测同一镜满分辨率与 0.7 档抽帧比较：逐像素平均差
   *  0.12/255，差超过 16 的像素占 0.00%（2026-10-21）。要逐像素一致、不在乎时间时才写 1。 */
  dpr?: number;
  /** 三维层是否投影。缺省开（台面接触影靠它）。关掉省一次阴影贴图渲染，但实物会「浮」。 */
  shadows?: boolean;
  /** 三维层多重采样抗锯齿。缺省开；软件 WebGL 上这是纯 CPU 成本，关掉边缘略糙。 */
  antialias?: boolean;
}

const LIGHTS: Record<LightKind, {
  ambient: number; key: [number, number, number]; keyColor: string; keyIntensity: number;
  rim: [number, number, number]; rimColor: string; rimIntensity: number;
  fill: [number, number, number]; fillColor: string; fillIntensity: number;
  bg: string; fog: string;
}> = {
  night: {
    ambient: 0.18,
    // 主光放在墙**前面**：放在墙后面时墙会挡住它，台面上会出现两道假的长阴影。
    key: [-1.18, 1.55, -0.30], keyColor: '#ffdcb8', keyIntensity: 155,
    rim: [1.45, 1.15, -0.50], rimColor: '#9db8ff', rimIntensity: 6,
    fill: [-0.75, 0.55, 1.05], fillColor: '#ffd9b0', fillIntensity: 3,
    bg: '#07080a', fog: '#0a0d12',
  },
  dawn: {
    ambient: 0.34,
    key: [-1.7, 1.5, 0.6], keyColor: '#ffd6a8', keyIntensity: 170,
    rim: [1.4, 0.8, -0.8], rimColor: '#8fb0ff', rimIntensity: 14,
    fill: [0.2, 0.5, 1.2], fillColor: '#ffe8ce', fillIntensity: 9,
    bg: '#0b0f16', fog: '#101722',
  },
  clinical: {
    ambient: 0.55,
    key: [-0.9, 2.2, 0.9], keyColor: '#f2f6ff', keyIntensity: 220,
    rim: [1.5, 0.9, -0.8], rimColor: '#cfe0ff', rimIntensity: 18,
    fill: [0, 0.6, 1.2], fillColor: '#eef4ff', fillIntensity: 12,
    bg: '#0c0f13', fog: '#141a21',
  },
};

const FLOOR_TINT: Record<FloorKind, string> = {
  wood: '#46362a', stone: '#4c4a46', tile: '#585651', dark: '#242220',
};

const Bokeh: React.FC<{ w: number; h: number }> = ({ w, h }) => (
  <div
    style={{
      position: 'absolute', inset: 0, pointerEvents: 'none',
      filter: 'blur(22px)', opacity: 0.38,
    }}
  >
    <div style={{
      position: 'absolute', right: '10%', top: '13%', width: '3%', height: '4.5%',
      borderRadius: '50%',
      background: 'radial-gradient(circle, rgba(255,186,120,0.85) 0%, '
        + 'rgba(255,150,80,0.25) 55%, rgba(0,0,0,0) 100%)',
    }} />
    <div style={{
      position: 'absolute', right: '4%', top: '22%', width: '3%', height: '5%',
      borderRadius: '50%',
      background: 'radial-gradient(circle, rgba(255,210,150,0.7) 0%, rgba(0,0,0,0) 100%)',
    }} />
    <div style={{
      position: 'absolute', left: '3%', bottom: '4%', width: '26%', height: '12%',
      borderRadius: '40%', background: 'rgba(0,0,0,0.55)',
    }} />
  </div>
);

/**
 * 舞台：背景幕 → 台面 → 灯光 → 三维内容 → 浮尘 → 调色（暗角 / 颗粒 / 光斑）
 * → 屏幕层。放在 `FilmLayout` 的画面带里（或整帧自排），占满父盒。
 */
export const StudioStage: React.FC<StudioStageProps> = ({
  children, overlay, aspect = 'landscape', camera = 'push', light = 'night',
  floor = 'wood', haze = 0.45, grain = 0.14, vignette = 0.55, bokeh = true,
  exposure = 1.0, floorTint, wall = true, window = true, lamp = true, cam,
  beam = true, dpr = 0.7, shadows = true, antialias = false,
}) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const body = useBody(aspect);
  const w = Math.round(body.w);
  const h = Math.round(body.h);
  const t = frame / Math.max(1, durationInFrames - 1);
  const cfg = LIGHTS[light];

  // 逐帧分流：三维内容进 Canvas，DOM（svg/div）走画布上方的屏幕层。
  // 不做 useMemo：children 本来就是每帧新建的节点树，缓存不会命中。
  const { dom: stageDom, three: stageThree } = splitStageChildren(children);

  const env = useMemo(() => makeEnvTexture(), []);
  const wallMap = useMemo(() => {
    const tex = makePlasterTexture();
    tex.repeat.set(2, 1.4);
    return tex;
  }, []);
  const floorMap = useMemo(() => {
    const tex = floor === 'stone' ? makeStoneTexture()
      : floor === 'tile' ? makeTileTexture()
        : floor === 'dark' ? makeStoneTexture()
          : makeWoodTexture();
    tex.repeat.set(floor === 'tile' ? 6 : 3, floor === 'tile' ? 6 : 3);
    return tex;
  }, [floor]);
  const floorWear = useMemo(() => {
    const r = makeRoughTexture(21, 0.58);
    r.repeat.set(4, 4);
    return r;
  }, []);

  return (
    <AbsoluteFill>
      <ThreeCanvas
        width={w}
        height={h}
        dpr={dpr}
        shadows={shadows}
        camera={{ position: CAM_PATH[camera].from, fov: 32, near: 0.02, far: 30 }}
        gl={{ antialias }}
        style={{ background: cfg.bg }}
      >
        <color attach="background" args={[cfg.bg]} />
        {/* 雾从镜头前 1.1 米就开始：墙与台面的交界不能是一条硬直线，否则
            再好的材质也会被读成「两块板子拼的布景」。 */}
        <fog attach="fog" args={[cfg.fog, 1.1, 1.1 + 3.4 * (1 - haze)]} />
        <primitive object={env} attach="environment" />
        <Rig kind={camera} t={t} frame={frame} exposure={exposure} cam={cam} />

        <ambientLight intensity={cfg.ambient} />
        <spotLight position={cfg.key} angle={0.6} penumbra={0.9}
                   intensity={cfg.keyIntensity} distance={10} decay={2}
                   color={cfg.keyColor} castShadow
                   shadow-mapSize={[2048, 2048]} shadow-bias={-0.0005} />
        <pointLight position={cfg.rim} intensity={cfg.rimIntensity} color={cfg.rimColor} />
        <pointLight position={cfg.fill} intensity={cfg.fillIntensity} color={cfg.fillColor} />

        {/* 背景幕：一圈内壁，没有方边 */}
        <mesh position={[0.1, 1.1, -0.5]}>
          <cylinderGeometry args={[3.2, 3.2, 4.2, 64, 1, true]} />
          <meshBasicMaterial color={cfg.bg} side={THREE.BackSide} toneMapped={false} />
        </mesh>

        {/* 背后的墙：只留一层质地，把画面上半从「黑场」变成一个房间 */}
        {wall ? (
          <mesh position={[0.1, 1.0, -0.62]} receiveShadow>
            <planeGeometry args={[6, 3]} />
            <meshStandardMaterial map={wallMap} color="#1b1917" roughness={0.96}
                                  metalness={0} envMapIntensity={0.14} />
          </mesh>
        ) : null}

        {/* 左侧的窗：一块冷色发光面 + 一点溢到墙上的光，给房间一个光源方向 */}
        {window ? (
          <group position={[-1.52, 0.92, -0.60]}>
            <mesh>
              <planeGeometry args={[0.62, 0.86]} />
              <meshBasicMaterial color={light === 'night' ? '#86abe6' : '#ffd7a6'}
                                 toneMapped={false} />
            </mesh>
            {[
              { p: [0, 0.44, 0.012], s: [0.68, 0.025, 0.02] },
              { p: [0, -0.44, 0.012], s: [0.68, 0.025, 0.02] },
              { p: [-0.34, 0, 0.012], s: [0.022, 0.9, 0.02] },
              { p: [0.34, 0, 0.012], s: [0.022, 0.9, 0.02] },
              { p: [0, 0, 0.012], s: [0.012, 0.88, 0.014] },
            ].map((b, i) => (
              <mesh key={i} position={b.p as [number, number, number]} castShadow>
                <boxGeometry args={b.s as [number, number, number]} />
                <meshStandardMaterial color="#1d1a17" roughness={0.6} metalness={0.15} />
              </mesh>
            ))}
            <pointLight position={[0.1, 0.05, 0.42]}
                        intensity={light === 'night' ? 10 : 14}
                        color={light === 'night' ? '#9dbbf0' : '#ffd9ae'}
                        distance={3.2} decay={2} />
          </group>
        ) : null}

        {/* 右侧远处的一盏小灯：唯一允许的第二个发光点，用来交代房间有多深 */}
        {lamp ? (
          <group position={[1.08, 0.74, -0.5]}>
            <mesh>
              <sphereGeometry args={[0.024, 24, 16]} />
              <meshBasicMaterial color="#ffbe7a" toneMapped={false} />
            </mesh>
            <pointLight position={[0, 0, 0.08]} intensity={18} color="#ffb066"
                        distance={2.4} decay={2} />
          </group>
        ) : null}

        {/* 台面 */}
        <mesh position={[0, 0, 0.3]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
          <planeGeometry args={[6, 4]} />
          {/* 台面不能像打了蜡的塑料：木/石挡的粗糙度压到 0.58、环境反射降到
              0.3，只留下木纹与一层很薄的漫反射。 */}
          <meshStandardMaterial map={floorMap} color={floorTint ?? FLOOR_TINT[floor]}
                                roughness={floor === 'tile' ? 0.34 : 0.58}
                                metalness={floor === 'tile' ? 0.05 : 0.02}
                                envMapIntensity={0.3} roughnessMap={floorWear} />
        </mesh>

        {stageThree}
        {haze > 0 ? <Dust t={t} opacity={0.35 * haze} /> : null}
      </ThreeCanvas>

      {/* 屏幕层：模型用 `<svg>` / `<div>` 画的整幅画面走这里——放进 `<ThreeCanvas>`
          会被当成三维对象，直接抛 `X is not part of the THREE namespace`（见
          splitStageChildren 的注释）。放在调色层之下，颗粒、暗角、光斑仍会盖在它上面。 */}
      {stageDom.length > 0 ? (
        <AbsoluteFill style={{ pointerEvents: 'none' }}>{stageDom}</AbsoluteFill>
      ) : null}

      {/* 调色：暗角 + 顶部冷色 + 胶片颗粒。全部确定性。 */}
      <div
        style={{
          position: 'absolute', inset: 0, pointerEvents: 'none',
          background:
            'linear-gradient(180deg, rgba(14,24,40,0.30) 0%, rgba(14,24,40,0.06) 32%, '
            + 'rgba(0,0,0,0) 55%), '
            + `radial-gradient(ellipse at 44% 46%, rgba(0,0,0,0) ${38}%, `
            + `rgba(0,0,0,${0.34 * vignette}) 74%, rgba(0,0,0,${1.2 * vignette}) 100%)`,
        }}
      />
      {bokeh ? <Bokeh w={w} h={h} /> : null}
      {beam ? (
        <div style={{
          position: 'absolute', inset: 0, pointerEvents: 'none',
          mixBlendMode: 'screen', opacity: 0.9,
          background:
            'linear-gradient(106deg, rgba(255,242,218,0) 34%, '
            + 'rgba(255,238,208,0.05) 42%, rgba(255,240,214,0.13) 48%, '
            + 'rgba(255,236,204,0.05) 54%, rgba(0,0,0,0) 62%)',
          filter: 'blur(26px)',
        }} />
      ) : null}
      {grain > 0 ? (
        <svg width={w} height={h}
             style={{ position: 'absolute', inset: 0, opacity: grain,
                      mixBlendMode: 'overlay' }}>
          <filter id="cs-grain">
            <feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="7" />
            <feColorMatrix type="saturate" values="0" />
          </filter>
          <rect width={w} height={h} filter="url(#cs-grain)" />
        </svg>
      ) : null}

      <AbsoluteFill style={{ pointerEvents: 'none' }}>{overlay}</AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ */
/* 2D 标注与线条：教程里「手绘标注 / SVG 线条生长」两族的通用件          */
/* ------------------------------------------------------------------ */

export interface MarkProps {
  /** `circle` 圈住、`underline` 划线、`arrow` 指过去、`bracket` 括住。 */
  kind?: 'circle' | 'underline' | 'arrow' | 'bracket';
  /** 位置与大小，按画面比例给（0~1）。只给 `size` 时按正方形（w = h = size）。 */
  x: number; y: number; w?: number; h?: number;
  /** 兼容写法：正方形边长的画面比例。给了 `w`/`h` 时以它们为准。 */
  size?: number;
  /** 开始画的帧，缺省 0。 */
  at?: number;
  /** 画完用多少帧，缺省 18。 */
  dur?: number;
  /** 兼容写法：直接给 0~1 的进度（画到几成），给了就不再看 `at`/`dur`。 */
  progress?: number;
  color?: string;
  /** 线宽（px），缺省 6。 */
  width?: number;
  opacity?: number;
}

/** 手绘标注：线条沿路径长出来，不是一次出现。
 *
 *  **它是屏幕层组件**（`domLayer`）：可以直接写在 `StudioStage` 的 children 里，
 *  `splitStageChildren` 会把它挪到画布上方，不会进 `<ThreeCanvas>`。 */
export const Mark: React.FC<MarkProps> = ({
  kind = 'circle', x, y, w, h, size, at = 0, dur = 18, color = '#ffcf6b',
  width = 6, opacity = 1, progress: progressIn,
}) => {
  const frame = useCurrentFrame();
  const { width: vw, height: vh } = useVideoConfig();
  const bw = w ?? size ?? 0; const bh = h ?? size ?? 0;
  const px = x * vw; const py = y * vh; const pw = bw * vw; const ph = bh * vh;
  // 显式 `progress` 优先：写手常把「画到几成」直接算好传进来（`progress={seg(...)}`），
  // 这时再按 `at`/`dur` 算一遍会把它的时间线推进重来一次。2026-10-21 run `778575d9b8d0`。
  const byClock = (frame - at) / Math.max(1, dur);
  const progress = Math.max(0, Math.min(1,
    typeof progressIn === 'number' ? progressIn : byClock));
  if (progress <= 0) return null;

  let d = '';
  if (kind === 'circle') {
    const cx = px + pw / 2; const cy = py + ph / 2;
    const rx = pw / 2; const ry = ph / 2;
    d = `M ${cx - rx} ${cy} C ${cx - rx} ${cy - ry}, ${cx + rx} ${cy - ry}, `
      + `${cx + rx} ${cy} C ${cx + rx} ${cy + ry}, ${cx - rx} ${cy + ry}, `
      + `${cx - rx} ${cy}`;
  } else if (kind === 'underline') {
    d = `M ${px} ${py + ph} C ${px + pw * 0.3} ${py + ph - ph * 0.25}, `
      + `${px + pw * 0.7} ${py + ph + ph * 0.15}, ${px + pw} ${py + ph - ph * 0.1}`;
  } else if (kind === 'bracket') {
    d = `M ${px + pw} ${py} L ${px} ${py} L ${px} ${py + ph} L ${px + pw} ${py + ph}`;
  } else {
    const midY = py + ph / 2;
    d = `M ${px} ${midY} L ${px + pw * 0.72} ${py + ph / 2}`;
  }

  return (
    <svg width={vw} height={vh}
         style={{ position: 'absolute', inset: 0, opacity }}>
      <path d={d} fill="none" stroke={color} strokeWidth={width}
            strokeLinecap="round" strokeLinejoin="round"
            strokeDasharray="4000" strokeDashoffset={4000 * (1 - progress)} />
      {kind === 'arrow' ? (
        <g opacity={progress > 0.85 ? 1 : 0}>
          <path d={`M ${px + pw * 0.72} ${py + ph / 2}
                     L ${px + pw * 0.72 - ph * 0.28} ${py + ph / 2 - ph * 0.22}
                     M ${px + pw * 0.72} ${py + ph / 2}
                     L ${px + pw * 0.72 - ph * 0.28} ${py + ph / 2 + ph * 0.22}`}
                fill="none" stroke={color} strokeWidth={width}
                strokeLinecap="round" />
        </g>
      ) : null}
    </svg>
  );
};

/** 屏幕层：把这一段放进画布上方的 DOM 层。
 *
 *  什么时候用：你自己写的组件返回 `<svg>` / `<div>`，又要放在 `StudioStage` 的
 *  children 里。`splitStageChildren` 只认字符串标签和挂了 `domLayer` 标记的组件，
 *  认不出你写的组件画的是什么——包一层就明说了。`Mark` / `LineDraw` 已经自带标记，
 *  不用再包。
 *
 *  ```tsx
 *  <StudioStage …>
 *    <group>{实物}</group>
 *    <Screen><MySvgLayer /></Screen>
 *  </StudioStage>
 *  ``` */
export const Screen: React.FC<{ children?: React.ReactNode }> = ({ children }) =>
  <>{children}</>;

export interface LineDrawProps {
  /** SVG path 的 `d`，坐标按 viewBox 给。 */
  d: string;
  /** 线从第几帧开始长、长多少帧。 */
  at?: number; dur?: number;
  color?: string; width?: number;
  /** 画布比例位置（0~1）。 */
  x?: number; y?: number; w?: number; h?: number;
  /** 缺省 `0 0 1000 1000`。 */
  viewBox?: string;
}

/** SVG 线条生长：一条路径沿 `stroke-dashoffset` 一点点画出来。 */
export const LineDraw: React.FC<LineDrawProps> = ({
  d, at = 0, dur = 30, color = '#ffffff', width = 4,
  x = 0, y = 0, w = 1, h = 1, viewBox = '0 0 1000 1000',
}) => {
  const frame = useCurrentFrame();
  const { width: vw, height: vh } = useVideoConfig();
  const p = Math.max(0, Math.min(1, (frame - at) / Math.max(1, dur)));
  return (
    <svg width={vw} height={vh} style={{ position: 'absolute', inset: 0 }}>
      <svg x={x * vw} y={y * vh} width={w * vw} height={h * vh} viewBox={viewBox}>
        <path d={d} fill="none" stroke={color} strokeWidth={width}
              strokeLinecap="round" strokeLinejoin="round"
              strokeDasharray="6000" strokeDashoffset={6000 * (1 - p)} />
      </svg>
    </svg>
  );
};

/* 屏幕层标记：这三个组件画的是 `<svg>`，必须走 DOM 层。
   标记挂成静态属性而不是另一张表——表会漏项，漏的那一项就是 23 张静帧全黑。 */
Object.assign(Mark, { domLayer: true as const });
Object.assign(LineDraw, { domLayer: true as const });
Object.assign(Screen, { domLayer: true as const });

/** 风格包 manifest 的缺省值（与 `examples/skills/style_cinema_studio/SKILL.md` 同源）。 */
export const cinemaStudioDefaults = {
  version: 1,
  template: 'cinema-studio',
  palette: {
    page: '#07080a',
    surface: '#14100d',
    text: '#f4efe6',
    accent: '#ffcf6b',
    rim: '#5f92ff',
    light: '#ffc994',
    border: '#2a241f',
  },
  composition: {
    subject_width: [0.35, 0.55],
    subject_height: [0.35, 0.55],
    camera: 'slow push-in, 1.0x to 1.08x',
    horizon: 0.55,
  },
  caption: {
    look: 'overlay', position: 'bottom-center', width: 0.62,
    size: 0.44, order: 'en-zh',
  },
  motion: { camera: 'slow push or pan', grain: 0.14, vignette: 0.55, haze: 0.45 },
  avoid: ['平涂图标', 'PPT 卡片', '纯色块堆叠', '卡通轮廓线', '外部图片素材', '第二条字幕'],
  narration_avoid: [],
} as const;
