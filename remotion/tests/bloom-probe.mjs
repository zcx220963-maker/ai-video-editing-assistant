// `@react-three/postprocessing` 的入场券：它在 swangle 软 GL 上能不能出**真 bloom**。
//
// 为什么单独一支（2026-10-24）：`@remotion/effects/glow` 只认 `<Img>/<Solid>/<HtmlInCanvas>`，
// **管不到 `<ThreeCanvas>` 里的东西**——three 场景要光晕只能走 EffectComposer，
// 而这一族是 WebGL2 后处理，在软件 GL 上的坏法同样是「不崩、渲出一张看着对的图」。
//
// 判据三条，都判在真渲的像素上：
//   ① 不崩，且与「不挂 EffectComposer」那一版**逐像素有差**（没差＝静默空操作）；
//   ② 差在**亮部外溢**：亮球外侧一圈被提亮（15~45 是「一圈柔光」，>60 是「整幅糊了」）；
//   ③ 每帧成本（150 帧的耗时差 ÷ 150）。
//
// 用法（容器内）：
//     docker exec -e DG_BASE=http://127.0.0.1:8010 creation-remotion \
//       sh -c 'cd /srv && node tests/bloom-probe.mjs'
// 退出码：0 全过；1 有红。
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'bloomprobe-'));
const BG = '#05070d';
const FRAMES = 150;

const post = async (p, payload) => (await fetch(`${BASE}${p}`, {
  method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify(payload),
})).json();

const shot = (withFx) => `import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { lerp } from '@dg/motion';
${withFx ? `import { EffectComposer, Bloom } from '@react-three/postprocessing';\n` : ''}
const Shot = () => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const spin = lerp(frame, 0, 0.9);
  return (
    <AbsoluteFill style={{ backgroundColor: '${BG}' }}>
      <ThreeCanvas width={width} height={height}
        camera={{ position: [0, 0, 4.2], fov: 45 }}
        style={{ background: 'transparent' }}>
        <ambientLight intensity={0.35} />
        <directionalLight position={[3, 4, 5]} intensity={2.4} color="#ffffff" />
        <mesh rotation={[0.2, spin, 0.1]}>
          <sphereGeometry args={[1.05, 48, 48]} />
          <meshStandardMaterial color="#ffffff" emissive="#FFC93C"
            emissiveIntensity={1.8} roughness={0.3} metalness={0.1} />
        </mesh>
${withFx ? `        <EffectComposer>
          <Bloom intensity={0.9} luminanceThreshold={0.6} luminanceSmoothing={0.25}
            mipmapBlur radius={0.45} />
        </EffectComposer>\n` : ''}      </ThreeCanvas>
    </AbsoluteFill>
  );
};
export default Shot;
`;

async function render(tag, code) {
  const t0 = Date.now();
  const j = await post('/render-shot', {
    code, cache_key: `bloom${tag}${Date.now().toString(36)}`, declared_fields: [],
    input_props: {fps: 30, durationInFrames: FRAMES, aspect: 'landscape',
      text: 'bloom', filmStartSec: 0},
  });
  const secs = (Date.now() - t0) / 1000;
  if (!j.ok) {
    console.log(`  XX [${tag}] 渲染失败：${String(j.error).slice(0, 240)}`);
    console.log(`     ${JSON.stringify(j.errors || j.lint_errors || '').slice(0, 300)}`);
    return null;
  }
  const mp4 = path.join(WORK, `${tag}.mp4`);
  fs.writeFileSync(mp4, Buffer.from(await (await fetch(`${BASE}${j.artifact}`)).arrayBuffer()));
  const jpg = path.join(WORK, `${tag}.jpg`);
  execFileSync('ffmpeg', ['-v', 'error', '-ss', '2.5', '-i', mp4, '-frames:v', '1', '-y', jpg]);
  const rawf = path.join(WORK, `${tag}.rgb`);
  execFileSync('ffmpeg', ['-v', 'error', '-i', jpg, '-f', 'rawvideo', '-pix_fmt', 'rgb24',
    '-y', rawf], {maxBuffer: 1 << 28});
  const px = fs.readFileSync(rawf);
  const w = Number(j.width || 1920);
  return {px, w, h: Math.round(px.length / 3 / w), secs};
}

const L = (r, x, y) => {
  const i = (Math.round(y) * r.w + Math.round(x)) * 3;
  return 0.2126 * r.px[i] + 0.7152 * r.px[i + 1] + 0.0722 * r.px[i + 2];
};

/** 球外一圈（半径 = 画面高 × k）的平均亮度。 */
const ringAvg = (r, k) => {
  let s = 0, c = 0;
  for (let a = 0; a < Math.PI * 2; a += 0.1) {
    s += L(r, r.w / 2 + Math.cos(a) * r.h * k, r.h / 2 + Math.sin(a) * r.h * k);
    c++;
  }
  return s / c;
};

let fail = 0;
const say = (ok, msg) => {
  if (!ok) fail++;
  console.log(`${ok ? '  ok ' : '  XX '} ${msg}`);
};

console.log('`@react-three/postprocessing` 探针（1920×1080 / 白球 / 150 帧）\n');
const plain = await render('plain', shot(false));
const bloom = await render('bloom', shot(true));

if (plain && bloom) {
  console.log(`  不挂 ${plain.secs.toFixed(0)}s / 挂 ${bloom.secs.toFixed(0)}s`);

  let diff = 0;
  const n = Math.min(plain.px.length, bloom.px.length) / 3;
  for (let i = 0; i < n; i++) {
    if (Math.abs(plain.px[i * 3] - bloom.px[i * 3]) > 6) diff++;
  }
  say(diff / n > 0.01, `效果真跑了：有差像素 ${(diff / n * 100).toFixed(1)}% > 1%`);

  const near = ringAvg(plain, 0.42), nearB = ringAvg(bloom, 0.42);
  say(nearB > near * 2 && nearB > 8,
    `亮部外溢：球外一圈 ${near.toFixed(1)} → ${nearB.toFixed(1)}（>8 且至少翻倍）`);
  say(nearB < 60, `没有糊：球外一圈 ${nearB.toFixed(1)} < 60（>60 就是整幅泛白）`);

  const perFrame = (bloom.secs - plain.secs) / FRAMES;
  say(perFrame < 0.5, `每帧成本 +${perFrame.toFixed(2)}s < 0.5s`);
  console.log(`\n  图：${WORK}`);
}

console.log(`\n${fail ? `${fail} 条红——不放进白名单` : '全过'}`);
process.exit(fail ? 1 : 0);
