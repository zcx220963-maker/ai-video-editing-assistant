// 一次性渲染预检：走「bundle → selectComposition → renderMedia → onBrowserLog token → ffprobe」整条流水线。
// 本机（宿主 Chrome + 宿主 ffmpeg）与容器内都能用，只看 DG_BASE 指谁：
//   本机:  node tests/hosted-precheck.mjs                       （DG_BASE 默认 http://127.0.0.1:8011）
//   容器:  docker exec -e DG_BASE=http://127.0.0.1:8010 -e DG_KEY=container-precheck-001 …
// 本机的这份不能替代容器验收——gl 档、每帧毫秒、中文字面都在容器里才算数。
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const code = readFileSync(path.join(ROOT, 'src', 'user-shots', 'shot.tsx'), 'utf8');
const base = process.env.DG_BASE || 'http://127.0.0.1:8011';
const key = process.env.DG_KEY || 'hosted-precheck-001';

const t0 = Date.now();
const res = await fetch(`${base}/render-shot`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({
    code,
    cache_key: key,
    input_props: { fps: 30, durationInFrames: 30, aspect: 'portrait', text: '清醒的堕落最可怕', highlight: ['堕落'] },
  }),
});
const j = await res.json();
console.log(`HTTP ${res.status} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
console.log(JSON.stringify(j, null, 2).slice(0, 2000));
