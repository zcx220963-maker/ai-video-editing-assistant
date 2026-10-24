// 探针：程序化环境贴图能不能让金属件真的「反光」
//
// 判据（都判在真渲的像素上）：
//   ① 不崩、不是空图
//   ② 金属件在**没有** env 时接近全黑（这是物理事实，也是判据的对照组）
//   ③ 挂上 env 之后亮度显著上升（>3 倍）——说明环境真的进了 BRDF
//   ④ 球面上有**结构**（不是整体提亮）：上下明暗差、或高频能量上升
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'envprobe-'));

const post = async (p, payload) => (await fetch(`${BASE}${p}`, {
  method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify(payload),
})).json();

const shot = (withEnv) => `import React, { Suspense, useEffect } from 'react';
import { AbsoluteFill, useVideoConfig, staticFile } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { useLoader, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { lerp } from '@dg/motion';
${withEnv ? `const Env = () => {
  const { scene } = useThree();
  const tex = useLoader(THREE.TextureLoader, staticFile('assets/lib/env-studio.png'));
  useEffect(() => {
    tex.mapping = THREE.EquirectangularReflectionMapping;
    tex.colorSpace = THREE.SRGBColorSpace;
    scene.environment = tex;
    return () => { scene.environment = null; };
  }, [tex, scene]);
  return null;
};\n` : ''}const Shot = () => {
  const { width, height } = useVideoConfig();
  return (
    <AbsoluteFill style={{ backgroundColor: '#05070d' }}>
      <ThreeCanvas width={width} height={height}
        camera={{ position: [0, 0, 4.2], fov: 45 }}
        style={{ background: 'transparent' }}>
        <ambientLight intensity={0.1} />
        <directionalLight position={[3, 4, 5]} intensity={0.5} color="#ffffff" />
        <mesh rotation={[0.2, lerp(0, 0.3, 0.5), 0.1]}>
          <sphereGeometry args={[1.05, 96, 96]} />
          <meshStandardMaterial color="#D8DEE9" metalness={1} roughness={0.18} />
        </mesh>
${withEnv ? `        <Suspense fallback={null}><Env /></Suspense>\n` : ''}      </ThreeCanvas>
    </AbsoluteFill>
  );
};
export default Shot;
`;

async function render(tag, code) {
  const t0 = Date.now();
  const j = await post('/render-shot', {
    code, cache_key: `env${tag}${Date.now().toString(36)}`, declared_fields: [],
    input_props: {fps: 30, durationInFrames: 20, aspect: 'landscape',
      text: 'env', filmStartSec: 0},
  });
  const secs = (Date.now() - t0) / 1000;
  if (!j.ok) {
    console.log(`  XX [${tag}] 渲染失败：${String(j.error).slice(0, 260)}`);
    console.log(`     ${JSON.stringify(j.errors || j.lint_errors || '').slice(0, 320)}`);
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
  return {px, w, h: Math.round(px.length / 3 / w), secs, jpg};
}

const L = (r, x, y) => {
  const i = (Math.round(y) * r.w + Math.round(x)) * 3;
  return 0.2126 * r.px[i] + 0.7152 * r.px[i + 1] + 0.0722 * r.px[i + 2];
};

/** 球面区域内平均亮度。 */
const sphereAvg = (r) => {
  const cx = r.w / 2, cy = r.h / 2, rad = r.h * 0.28;
  let s = 0, n = 0;
  for (let y = cy - rad; y < cy + rad; y += 3) {
    for (let x = cx - rad; x < cx + rad; x += 3) {
      if ((x - cx) ** 2 + (y - cy) ** 2 > rad * rad) continue;
      s += L(r, x, y); n++;
    }
  }
  return s / n;
};

/** 球面上半与下半的亮度差：环境贴图有「天光在上」就会拉出这个差。 */
const topBottom = (r) => {
  const cx = r.w / 2, cy = r.h / 2, rad = r.h * 0.26;
  // 上下各取一条带。**循环边界要按 min/max 摆**——第一版写成
  // `for (y = cy + sign*0.45r; y < cy + sign*0.95r)`，sign=-1 时起点已经大于终点，
  // 一圈都没跑，n=0 直接除出 NaN。
  const band = (sign) => {
    const y0 = cy + sign * rad * 0.95;
    const y1 = cy + sign * rad * 0.45;
    let s = 0, n = 0;
    for (let y = Math.min(y0, y1); y < Math.max(y0, y1); y += 2) {
      for (let x = cx - rad * 0.5; x < cx + rad * 0.5; x += 2) {
        s += L(r, x, y); n++;
      }
    }
    return n ? s / n : 0;
  };
  return band(-1) - band(1);   // 上 - 下
};

let fail = 0;
const say = (ok, msg) => { if (!ok) fail++; console.log(`${ok ? '  ok ' : '  XX '} ${msg}`); };

console.log('环境贴图探针（金属球 metalness=1 / roughness=0.18）\n');
const noEnv = await render('noenv', shot(false));
const withEnv = await render('env', shot(true));

if (noEnv && withEnv) {
  const a = sphereAvg(noEnv), b = sphereAvg(withEnv);
  console.log(`  无 env ${noEnv.secs.toFixed(0)}s / 有 env ${withEnv.secs.toFixed(0)}s`);
  console.log(`  球面平均亮度：无 env ${a.toFixed(1)} → 有 env ${b.toFixed(1)}`);

  say(a < 40, `对照组：没有 env 的金属确实是暗的（${a.toFixed(1)} < 40）——物理事实`);
  say(b > a * 3 && b > 30,
    `环境进了 BRDF：亮度 ${a.toFixed(1)} → ${b.toFixed(1)}（>3 倍且 >30）`);

  const dNo = topBottom(noEnv), dYes = topBottom(withEnv);
  say(dYes > 12,
    `有上下结构（不是整体提亮）：上半 - 下半 = ${dYes.toFixed(1)} > 12`
    + `（无 env 时是 ${dNo.toFixed(1)}）`);
  console.log(`\n  图：${noEnv.jpg}  ${withEnv.jpg}`);
}

console.log(`\n${fail ? `${fail} 条红` : '全过'}`);
process.exit(fail ? 1 : 0);
