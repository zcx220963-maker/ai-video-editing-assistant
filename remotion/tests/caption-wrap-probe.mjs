/**
 * 字幕断行探针：验「高亮词整体不被行末劈开」这一条（母版 Caption 的 white-space）。
 *
 * 用法（容器内）:  cd /srv && node tests/caption-wrap-probe.mjs [tag]
 * 产物：out/caption-{tag}.png（中帧，字幕已全显）
 *
 * 文案与高亮词是从真机 s03 那一帧抄来的原句——它在旧母版上把「一座城市」劈成
 * 行尾一个「一」字块 + 行首「座城市。」，正是这条探针要盯住的形状。
 */
import { existsSync, mkdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const OUT = path.join(ROOT, 'out');
const TAG = process.argv[2] || 'now';

const TEXT = '中子星是恒星坍缩后的残骸，把太阳压进一座城市。';
const HIGHLIGHT = ['恒星坍缩', '一座城市'];

mkdirSync(OUT, { recursive: true });

// 夹具那份只转交 text，不转交 highlight——拿它渲出来的字幕永远没有粉色块，
// 断行形状根本量不到。探针自己带一段最小镜头（实心圆保证画面非空）。
const code = `import React from 'react';
import { FilmLayout } from '@dg/film';
const Shot: React.FC<{ text?: string; highlight?: string[]; aspect?: 'portrait' | 'landscape' | 'square' }> =
  ({ text = '', highlight = [], aspect = 'portrait' }) => (
    <FilmLayout text={text} highlight={highlight} aspect={aspect}>
      <svg viewBox="0 0 100 60" style={{ width: '78%', height: '78%' }}>
        <circle cx="50" cy="30" r="22" fill="#a8352c" />
      </svg>
    </FilmLayout>
  );
export const UserShot = Shot;
export default Shot;
`;
const key = `caption-${TAG}-${Date.now()}`;
const r = await fetch(`${BASE}/render-shot`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({
    code,
    cache_key: key,
    declared_fields: [],
    input_props: {
      fps: 10, durationInFrames: 20, aspect: 'portrait',
      text: TEXT, highlight: HIGHLIGHT,
    },
  }),
});
const j = await r.json();
if (!j.ok) {
  console.log(`✗ render 失败：${JSON.stringify(j).slice(0, 300)}`);
  process.exit(1);
}
// 字节留在服务自己的 /srv/out/{cache_key}.mp4，响应里只有元数据
const mp4 = path.join(OUT, `${key}.mp4`);
console.log(`render ok bytes=${j.bytes} mp4=${mp4}`);
const png = path.join(OUT, `caption-${TAG}.png`);
execFileSync('ffmpeg', ['-y', '-i', mp4, '-vf', 'select=eq(n\\,15)', '-vframes', '1', png],
  { stdio: 'pipe', timeout: 30000 });
console.log(`${existsSync(png) ? '✓' : '✗'} ${png}`);
