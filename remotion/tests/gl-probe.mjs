// WebGL 探针：容器里哪一档 gl 画得出真 3D，是 spec §10 里最贵的一条未知。
// 用法：docker exec -e DG_BASE=http://127.0.0.1:8010 -e DG_KEY=… -e DG_FRAMES=90 creation-remotion sh -c 'cd /srv && node tests/gl-probe.mjs'
//
// 2026-10-10 容器实测（镜像内 Chromium 154.0.8037.92，1080×1920）：
//   angle        → 500 THREE.WebGLRenderer: Error creating WebGL context.
//   egl          → 同上
//   swiftshader  → 同上
//   swangle      → 200，抽帧看得到四棱锥的逐面明暗与正确遮挡 → 镜像默认改成这一档
//   90 帧耗时：bundle 7507ms + select 826ms + render 13752ms ≈ 152.8 ms/帧
//   （首帧族 2982ms 是着色器编译的一次性成本，后面摊薄）
//
// swangle 那趟的浏览器日志里有一条反复出现的警告：
//   "Automatic fallback to software WebGL has been deprecated. Please use the
//    --enable-unsafe-swiftshader flag…"
// 这一版 @remotion/renderer 只在 gl:'angle' 且开 V5 breaking changes 时才带那个 flag
// （见 node_modules/@remotion/renderer/dist/open-browser.js 的 getOpenGlRenderer），
// 也没有任何环境变量能追加 Chrome 参数。所以：**换 Chromium 后必须重跑本探针**；
// 哪天回退真被移除，出路是升 Remotion 版本或换 gl 档，不是继续用这个镜像。
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const CODE = `import React from 'react';
import { useCurrentFrame, useVideoConfig } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { FilmLayout } from '@dg/film';

const Shot: React.FC<{ text?: string; aspect?: 'portrait' | 'landscape' | 'square' }> = ({
  text = '',
  aspect = 'portrait',
}) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  return (
    <FilmLayout text={text} aspect={aspect}>
      <ThreeCanvas width={width} height={height} camera={{ position: [0, 1.6, 5], fov: 45 }}>
        <ambientLight intensity={0.5} />
        <directionalLight position={[4, 6, 5]} intensity={2.2} color="#ffffff" />
        <mesh rotation={[frame / 24, frame / 14, 0]}>
          <coneGeometry args={[1.5, 2.8, 4]} />
          <meshStandardMaterial color="#a8352c" roughness={0.45} metalness={0.1} />
        </mesh>
        <mesh position={[0, -1.8, 0]} rotation={[-Math.PI / 2, 0, 0]}>
          <planeGeometry args={[14, 14]} />
          <meshStandardMaterial color="#f6f1e4" roughness={0.9} />
        </mesh>
      </ThreeCanvas>
    </FilmLayout>
  );
};
export const UserShot = Shot;
export default Shot;
`;

const base = process.env.DG_BASE || 'http://127.0.0.1:8011';
const key = process.env.DG_KEY || 'gl-probe-001';
const frames = Number(process.env.DG_FRAMES || 60);

const t0 = Date.now();
const res = await fetch(`${base}/render-shot`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({
    code: CODE,
    cache_key: key,
    input_props: { fps: 30, durationInFrames: frames, aspect: 'portrait', text: '四棱锥转镜头' },
  }),
});
const j = await res.json();
console.log(`HTTP ${res.status} in ${((Date.now() - t0) / 1000).toFixed(1)}s  key=${key} frames=${frames}`);
console.log(JSON.stringify({ bytes: j.bytes, probed: j.probed, timings: j.timings, warnings: j.warnings, error: j.error }, null, 2));
if (j.ok) console.log(`产物：${path.basename(j.artifact ?? '')}（抽帧看得到几何体与光影才算 3D 可用）`);
