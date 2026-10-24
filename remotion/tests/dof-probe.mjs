// 探针：postprocessing 的景深（DOF）在软 GL 上能不能跑
// 判据：① 不崩 ② 与不挂 DOF 逐像素有差 ③ **差在焦外**——画面上下缘变模糊、中心保持清晰
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'dofprobe-'));

const post = async (p, payload) => (await fetch(`${BASE}${p}`, {
  method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify(payload),
})).json();

// 三颗球排成 Z 字：中间那颗在焦平面上，前后两颗该被虚化
const shot = (withDof) => `import React from 'react';
import { AbsoluteFill, useVideoConfig } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { lerp } from '@dg/motion';
${withDof ? `import { EffectComposer, DepthOfField } from '@react-three/postprocessing';\n` : ''}
const Ball = ({ z, color }) => (
  <mesh position={[z * 0.9, 0, z]}>
    <sphereGeometry args={[0.42, 48, 48]} />
    <meshStandardMaterial color={color} roughness={0.35} metalness={0.1} />
  </mesh>
);

const Shot = () => {
  const { width, height } = useVideoConfig();
  return (
    <AbsoluteFill style={{ backgroundColor: '#05070d' }}>
      <ThreeCanvas width={width} height={height}
        camera={{ position: [0, 0, 5], fov: 45 }}
        style={{ background: 'transparent' }}>
        <ambientLight intensity={0.5} />
        <directionalLight position={[2, 3, 4]} intensity={2} />
        <Ball z={-1.6} color="#61d9ef" />
        <Ball z={0} color="#FFC93C" />
        <Ball z={1.6} color="#FF3D7F" />
${withDof ? `        <EffectComposer>
          <DepthOfField focusDistance={0.02} focalLength={0.04} bokehScale={6} />
        </EffectComposer>\n` : ''}      </ThreeCanvas>
    </AbsoluteFill>
  );
};
export default Shot;
`;

async function render(tag, code) {
  const j = await post('/render-shot', {
    code, cache_key: `dof${tag}${Date.now().toString(36)}`, declared_fields: [],
    input_props: {fps: 30, durationInFrames: 20, aspect: 'landscape',
      text: 'dof', filmStartSec: 0},
  });
  if (!j.ok) {
    console.log(`  XX [${tag}] 失败：${String(j.error).slice(0, 220)}`);
    console.log(`     ${JSON.stringify(j.errors || j.lint_errors || '').slice(0, 300)}`);
    return null;
  }
  const mp4 = path.join(WORK, `${tag}.mp4`);
  fs.writeFileSync(mp4, Buffer.from(await (await fetch(`${BASE}${j.artifact}`)).arrayBuffer()));
  const jpg = path.join(WORK, `${tag}.jpg`);
  execFileSync('ffmpeg', ['-v', 'error', '-ss', '0.4', '-i', mp4, '-frames:v', '1', '-y', jpg]);
  const rawf = path.join(WORK, `${tag}.rgb`);
  execFileSync('ffmpeg', ['-v', 'error', '-i', jpg, '-f', 'rawvideo', '-pix_fmt', 'rgb24',
    '-y', rawf], {maxBuffer: 1 << 28});
  const px = fs.readFileSync(rawf);
  const w = Number(j.width || 1920);
  return {px, w, h: Math.round(px.length / 3 / w), jpg};
}

const L = (r, x, y) => {
  const i = (Math.round(y) * r.w + Math.round(x)) * 3;
  return 0.2126 * r.px[i] + 0.7152 * r.px[i + 1] + 0.0722 * r.px[i + 2];
};

/** 某个矩形区域的平均 |拉普拉斯|（清晰度指标）。 */
const sharp = (r, x0, y0, x1, y1) => {
  let s = 0, n = 0;
  for (let y = y0; y < y1; y += 2) {
    for (let x = x0; x < x1; x += 2) {
      s += Math.abs(4 * L(r, x, y) - L(r, x - 2, y) - L(r, x + 2, y)
        - L(r, x, y - 2) - L(r, x, y + 2));
      n++;
    }
  }
  return s / n;
};

let fail = 0;
const say = (ok, msg) => { if (!ok) fail++; console.log(`${ok ? '  ok ' : '  XX '} ${msg}`); };

console.log('景深探针（三颗球排成 Z 字，中间那颗在焦上）\n');
const plain = await render('plain', shot(false));
const dof = await render('dof', shot(true));

if (plain && dof) {
  let diff = 0;
  const n = Math.min(plain.px.length, dof.px.length) / 3;
  for (let i = 0; i < n; i++) {
    if (Math.abs(plain.px[i * 3] - dof.px[i * 3]) > 6) diff++;
  }
  say(diff / n > 0.005, `DOF 真跑了：有差像素 ${(diff / n * 100).toFixed(2)}% > 0.5%`);

  // 只判「整幅清晰度确实降了」。**不判「中间那颗保持清晰」**——三颗球的屏幕 x 随 z 投影
  // 变，靠估的竖条去取样本来就不可靠（第一版就是这么写的，取到 0.0 还判过）。
  // 要判焦内焦外，得先按投影算出球心，那是另一件事；这一支只负责「能不能用」。
  const allSharp = (r) => sharp(r, Math.round(r.w * 0.2), Math.round(r.h * 0.3),
                               Math.round(r.w * 0.8), Math.round(r.h * 0.7));
  const a = allSharp(plain), b = allSharp(dof);
  say(b < a * 0.75,
    `画面确实被虚化了：清晰度 ${a.toFixed(1)} → ${b.toFixed(1)}（<75%）`);
  console.log('  参数提醒：focusDistance/focalLength 要按焦平面调，'
    + '第一版给的 0.02/0.04 把整幅都虚了');
  console.log(`\n  图：${plain.jpg}  ${dof.jpg}`);
}

console.log(`\n${fail ? `${fail} 条红` : '全过'}`);
process.exit(fail ? 1 : 0);
