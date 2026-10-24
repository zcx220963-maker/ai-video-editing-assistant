// 探针：内置贴图在 three 场景里到底贴不贴得上
//
// 要证的三件事（都判在真渲的像素上）：
//   ① 不崩、不是空图——`useTexture` 是 suspense 异步加载，最容易的坏法是「渲出一张空球」
//   ② 与纯色材质**逐像素有差**（贴图真进了材质，不是被忽略）
//   ③ 球面上的**高频能量**显著上升（粗糙度/法线真的在起作用，而不是整体变亮变暗）
//
// 用法（容器内）：
//   docker exec -e DG_BASE=http://127.0.0.1:8010 creation-remotion \
//     sh -c 'cd /srv && node tests/texture-probe.mjs'
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'texprobe-'));
const BG = '#05070d';
const FRAMES = 60;

const post = async (p, payload) => (await fetch(`${BASE}${p}`, {
  method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify(payload),
})).json();

const shot = (textured) => `import React, { Suspense } from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, staticFile } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { useLoader } from '@react-three/fiber';
import * as THREE from 'three';
import { lerp } from '@dg/motion';
${textured ? `const Textured = () => {
  const [rough, norm] = useLoader(THREE.TextureLoader, [
    staticFile('assets/lib/roughness-noise.png'),
    staticFile('assets/lib/normal-grain.png'),
  ]);
  return (
    <mesh rotation={[0.2, 0.4, 0.1]}>
      <sphereGeometry args={[1.05, 96, 96]} />
      <meshStandardMaterial color="#E8ECF5" metalness={0.15}
        roughness={1} roughnessMap={rough} normalMap={norm}
        normalScale={[1.5, 1.5]} />
    </mesh>
  );
};\n` : ''}const Shot = () => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const spin = lerp(frame, 0.2, 0.9);
  return (
    <AbsoluteFill style={{ backgroundColor: '${BG}' }}>
      <ThreeCanvas width={width} height={height}
        camera={{ position: [0, 0, 4.2], fov: 45 }}
        style={{ background: 'transparent' }}>
        <ambientLight intensity={0.35} />
        <directionalLight position={[3, 4, 5]} intensity={2.4} color="#ffffff" />
        <directionalLight position={[-4, -2, 2]} intensity={0.6} color="#61d9ef" />
${textured ? `        <Suspense fallback={null}>
          <Textured />
        </Suspense>` : `        <mesh rotation={[0.2, 0.4, 0.1]}>
          <sphereGeometry args={[1.05, 96, 96]} />
          <meshStandardMaterial color="#E8ECF5" metalness={0.15} roughness={0.45} />
        </mesh>`}
      </ThreeCanvas>
    </AbsoluteFill>
  );
};
export default Shot;
`;

async function render(tag, code) {
  const t0 = Date.now();
  const j = await post('/render-shot', {
    code, cache_key: `tex${tag}${Date.now().toString(36)}`, declared_fields: [],
    input_props: {fps: 30, durationInFrames: FRAMES, aspect: 'landscape',
      text: 'tex', filmStartSec: 0},
  });
  const secs = (Date.now() - t0) / 1000;
  if (!j.ok) {
    console.log(`  XX [${tag}] 渲染失败：${String(j.error).slice(0, 300)}`);
    console.log(`     ${JSON.stringify(j.errors || j.lint_errors || '').slice(0, 400)}`);
    return null;
  }
  const mp4 = path.join(WORK, `${tag}.mp4`);
  fs.writeFileSync(mp4, Buffer.from(await (await fetch(`${BASE}${j.artifact}`)).arrayBuffer()));
  const jpg = path.join(WORK, `${tag}.jpg`);
  execFileSync('ffmpeg', ['-v', 'error', '-ss', '1.2', '-i', mp4, '-frames:v', '1', '-y', jpg]);
  const rawf = path.join(WORK, `${tag}.rgb`);
  execFileSync('ffmpeg', ['-v', 'error', '-i', jpg, '-f', 'rawvideo', '-pix_fmt', 'rgb24',
    '-y', rawf], {maxBuffer: 1 << 28});
  const px = fs.readFileSync(rawf);
  const w = Number(j.width || 1920);
  return {px, w, h: Math.round(px.length / 3 / w), secs, jpg};
}

const L = (r, x, y) => {
  const i = (Math.round(y) * r.w + Math.round(x)) * 3;
  return 0.2126 * r.px[i] + 0.7152 * r.px[i + 1] + 0.0722 * r.px[i + 2];
};

/** 球面区域内的平均 |拉普拉斯|：高频能量的粗估。贴图上了这一项会明显变大。 */
const highFreq = (r) => {
  const cx = r.w / 2, cy = r.h / 2, rad = r.h * 0.30;
  let s = 0, n = 0;
  for (let y = cy - rad; y < cy + rad; y += 3) {
    for (let x = cx - rad; x < cx + rad; x += 3) {
      if ((x - cx) ** 2 + (y - cy) ** 2 > rad * rad) continue;
      s += Math.abs(4 * L(r, x, y) - L(r, x - 2, y) - L(r, x + 2, y)
        - L(r, x, y - 2) - L(r, x, y + 2));
      n++;
    }
  }
  return s / n;
};

let fail = 0;
const say = (ok, msg) => {
  if (!ok) fail++;
  console.log(`${ok ? '  ok ' : '  XX '} ${msg}`);
};

console.log('内置贴图探针（1920×1080 / 球面 / 60 帧）\n');
const plain = await render('plain', shot(false));
const tex = await render('tex', shot(true));

if (plain && tex) {
  console.log(`  纯色 ${plain.secs.toFixed(0)}s / 贴图 ${tex.secs.toFixed(0)}s`);

  // ② 逐像素有差
  let diff = 0;
  const n = Math.min(plain.px.length, tex.px.length) / 3;
  for (let i = 0; i < n; i++) {
    if (Math.abs(plain.px[i * 3] - tex.px[i * 3]) > 6) diff++;
  }
  say(diff / n > 0.02, `贴图进了材质：有差像素 ${(diff / n * 100).toFixed(1)}% > 2%`);

  // ③ 高频能量
  //    阈值定在 3 倍：实测 ns=1.5 时纯色 0.90 → 贴图 17.77（约 20 倍）。
  //    定 1.5 倍是给「纹理换了但变弱」留余地——低于 3 倍基本就是没贴上。
  const hp = highFreq(plain), ht = highFreq(tex);
  say(ht > hp * 3,
    `表面真的粗糙了：高频能量 ${hp.toFixed(2)} → ${ht.toFixed(2)}（>3 倍）`);

  // ① 不是空球
  say(L(tex, tex.w / 2, tex.h / 2) > 40,
    `不是空图：球心亮度 ${L(tex, tex.w / 2, tex.h / 2).toFixed(0)} > 40`);
  console.log(`\n  图：${plain.jpg}  ${tex.jpg}`);
}

console.log(`\n${fail ? `${fail} 条红` : '全过'}`);
process.exit(fail ? 1 : 0);
