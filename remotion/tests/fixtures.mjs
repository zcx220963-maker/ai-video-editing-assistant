/**
 * P4 三幕夹具验证：lint → render → 抽帧 → 性能实测表。
 *
 * 用法（容器内）:  cd /srv && node tests/fixtures.mjs
 * 用法（宿主）:    DG_BASE=http://127.0.0.1:8011 node tests/fixtures.mjs
 *
 * 产物：
 *   out/{key}.mp4           渲染成片
 *   out/{key}-f0.png        首帧
 *   out/{key}-fmid.png      中帧
 *   out/{key}-flast.png     末帧
 *   控制台打印 lint 结果 + 性能表
 */
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const OUT = path.join(ROOT, 'out');

const FIXTURES = [
  {
    name: 'rough.js',
    desc: '斜纹填充 + 两种眼形关键帧切换',
    file: path.join(ROOT, 'src', 'user-shots', 'fixture-rough.tsx'),
    declared: ['hatchGap', 'eyeSize'],
    fields: { hatchGap: 8, eyeSize: 0.7 },
    props: { fps: 30, durationInFrames: 120, text: '两种眼形的关键帧切换' },
  },
  {
    name: 'three.js',
    desc: '四棱锥转镜头，顶点标号可读',
    file: path.join(ROOT, 'src', 'user-shots', 'fixture-three.tsx'),
    declared: ['pyramidColor', 'rotationSpeed'],
    fields: { pyramidColor: '#a8352c', rotationSpeed: 1.0 },
    props: { fps: 30, durationInFrames: 90, text: '四棱锥转镜头' },
  },
  {
    name: 'KaTeX',
    desc: '公式字形 + 波面动画',
    file: path.join(ROOT, 'src', 'user-shots', 'fixture-katex.tsx'),
    declared: ['waveAmp1', 'waveAmp2'],
    fields: { waveAmp1: 0.3, waveAmp2: 0.2 },
    props: { fps: 30, durationInFrames: 150, text: '波面动画' },
  },
];

// 每种风格跑两个画幅：横屏那条画面带刚从「TOKENS 乘 u」换成按画幅查设备像素表
// （1664×340 → 1800×732），只在竖屏验过等于没验。
const ASPECTS_RUN = ['portrait', 'landscape'];

const post = async (p, payload) => {
  const r = await fetch(`${BASE}${p}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { status: r.status, json: await r.json() };
};

const extractFrame = (mp4, frame, outPng) => {
  try {
    execFileSync('ffmpeg', [
      '-y', '-i', mp4,
      '-vf', `select=eq(n\\,${frame})`,
      '-vframes', '1',
      outPng,
    ], { encoding: 'utf8', timeout: 15000, stdio: 'pipe' });
    return existsSync(outPng);
  } catch {
    return false;
  }
};

const RUNS = FIXTURES.flatMap((fx) => ASPECTS_RUN.map((aspect) => ({ ...fx, aspect })));

console.log('风格 × 画幅 夹具验证');
console.log('==================\n');

const results = [];
let allOk = true;

for (let i = 0; i < RUNS.length; i++) {
  const fx = RUNS[i];
  const code = readFileSync(fx.file, 'utf8');
  const idx = `[${i + 1}/${RUNS.length}]`;

  console.log(`${idx} ${fx.name} · ${fx.aspect} — ${fx.desc}`);

  // 1) lint
  const lintRes = await post('/lint', {
    code,
    declared_fields: fx.declared,
    input_props: { fields: fx.fields, aspect: fx.aspect },
  });
  const lintOk = lintRes.json.ok;
  if (!lintOk) allOk = false;
  console.log(`  lint: ${lintOk ? '✓ PASS' : '✗ FAIL'} (declared: ${fx.declared.join(', ')})`);
  if (!lintOk) {
    for (const e of lintRes.json.errors || []) {
      console.log(`    ${e.rule} L${e.line}: ${e.msg.slice(0, 80)}`);
    }
    results.push({ ...fx, lintOk, renderOk: false, timings: null, bytes: 0 });
    console.log('');
    continue;
  }

  // 2) render
  const cacheKey = `p4-${fx.aspect}-${fx.name.replace(/[^a-z0-9]/gi, '')}-${Date.now()}`;
  const t0 = Date.now();
  const renderRes = await post('/render-shot', {
    code,
    cache_key: cacheKey,
    declared_fields: fx.declared,
    input_props: {
      ...fx.props,
      aspect: fx.aspect,
      fields: fx.fields,
    },
  });
  const renderMs = Date.now() - t0;
  const j = renderRes.json;
  const renderOk = j.ok;
  if (!renderOk) allOk = false;

  console.log(`  render: ${renderOk ? '✓' : '✗'} ${fx.props.durationInFrames} frames, ${(fx.props.durationInFrames / fx.props.fps).toFixed(1)}s${renderOk ? `, ${(j.bytes / 1048576).toFixed(1)}MB` : ''}`);

  if (renderOk) {
    const tm = j.timings || {};
    console.log(`  timings: bundle=${tm.bundle_ms}ms select=${tm.select_ms}ms render=${tm.render_ms}ms per_frame=${tm.per_frame_ms}ms`);
    if (j.warnings?.length) {
      console.log(`  warnings: ${j.warnings.join('; ').slice(0, 120)}`);
    }

    // 3) extract frames
    const mp4 = path.join(OUT, `${cacheKey}.mp4`);
    const frames = [0, Math.floor(fx.props.durationInFrames / 2), fx.props.durationInFrames - 1];
    const frameFiles = frames.map((f, k) => {
      const tag = ['f0', 'fmid', 'flast'][k];
      const png = path.join(OUT, `${cacheKey}-${tag}.png`);
      const ok = extractFrame(mp4, f, png);
      return `frame-${tag}.png:${ok ? 'ok' : 'skip'}`;
    });
    console.log(`  frames: ${frameFiles.join(', ')}`);

    results.push({
      ...fx,
      lintOk,
      renderOk,
      timings: tm,
      bytes: j.bytes,
      renderMs,
    });
  } else {
    console.log(`  error: ${j.error?.slice(0, 120)}`);
    results.push({ ...fx, lintOk, renderOk: false, timings: null, bytes: 0, renderMs });
  }
  console.log('');
}

// 性能实测表
console.log('性能实测表');
console.log('==========');
console.log('| 夹具       | 帧数 | 总秒数 | bundle(ms) | select(ms) | render(ms) | 每帧(ms) |');
console.log('|------------|------|--------|------------|------------|------------|----------|');
for (const r of results) {
  const label = `${r.name}·${r.aspect}`;
  if (!r.timings) {
    console.log(`| ${label.padEnd(18)} |  ${String(r.props.durationInFrames).padStart(4)} | ${(r.props.durationInFrames / r.props.fps).toFixed(1).padStart(5)}s | ${'—'.padStart(10)} | ${'—'.padStart(10)} | ${'—'.padStart(10)} | ${'—'.padStart(7)} |`);
    continue;
  }
  const tm = r.timings;
  console.log(`| ${label.padEnd(18)} |  ${String(r.props.durationInFrames).padStart(4)} | ${(r.props.durationInFrames / r.props.fps).toFixed(1).padStart(5)}s | ${String(tm.bundle_ms).padStart(10)} | ${String(tm.select_ms).padStart(10)} | ${String(tm.render_ms).padStart(10)} | ${String(tm.per_frame_ms).padStart(7)} |`);
}

// 3D 性能告警：两个画幅分别看，横屏的 WebGL 更贵，只看竖屏会漏掉最坏那一档
const targetPerFrame = 1000 / 30;
for (const r of results.filter((x) => x.name === 'three.js' && x.timings?.per_frame_ms)) {
  const pf = r.timings.per_frame_ms;
  if (pf > targetPerFrame) {
    console.log(`\n⚠ three.js·${r.aspect} 每帧 ${pf}ms > 30fps 预算 ${targetPerFrame.toFixed(0)}ms`);
    console.log(`  建议：闸② 限该画幅 3D 镜 fps 至 ${Math.floor(1000 / pf)} 或限时长至 60 帧（2s@30fps）`);
  }
}

console.log(`\n${allOk ? `PASS — ${RUNS.length} 支夹具全部通过` : 'FAIL — 有夹具未通过'}`);
process.exitCode = allOk ? 0 : 1;