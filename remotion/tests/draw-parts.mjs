// 零件库的静态守卫：**每一笔画出来都得入账**。
//
// 2026-10-18 真机预览暴露的那一类：`Building` 的主体/窗/门、`Vehicle` 的车体/轮子/轨、
// `Book` 摊开页上的字行，写成了裸调用 `rc(...)`/`win(...)`/`ln(...)`——笔画算出来了，
// 却被丢掉。静态 lint 绿、闸⑦「画了没有」绿（锯齿屋顶还在）、闸㉝ 绿（件 import 了也用了），
// 唯独画面上少了一整栋厂房。这一道专门拦这一类：**零件里不许有「算了但没 push」的笔画**。
//
// 判据：在 `src/lib/draw/*.tsx` 里，以局部画笔助手（`rc`/`ln`/`cc`/`win`）开头、
// 独占一行、没有进 `out.push(...)` 的语句，一律红。助手名从文件里现取（`const X = (...) =>`），
// 新增助手也自动在管辖内。
//
// 用法：node tests/draw-parts.mjs      （构建期会跑，见 Dockerfile）
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const drawDir = path.join(here, '..', 'src', 'lib', 'draw');

let checks = 0;
let fails = 0;
const check = (ok, label) => {
  checks += 1;
  if (!ok) fails += 1;
  console.log(`  ${ok ? '✓' : '✗'} ${label}`);
};

const files = fs.readdirSync(drawDir).filter((f) => f.endsWith('.tsx') && !f.startsWith('_')).sort();

console.log(`零件库 ${files.length} 件：${files.join(' ')}`);
check(files.length >= 13, `至少 13 件（实到 ${files.length} 件）`);

for (const file of files) {
  const src = fs.readFileSync(path.join(drawDir, file), 'utf8');
  const lines = src.split('\n');

  // 本文件里的画笔助手：`const rc = (...) => ...`（可能折成两行，箭头在下一行）
  const helpers = new Set();
  lines.forEach((line, i) => {
    const m = line.match(/^\s*const (\w+)\s*=\s*\(/);
    if (!m) return;
    const tail = line.slice(line.indexOf(m[0]) + m[0].length);
    const next = lines[i + 1] || '';
    if (line.includes('=>') || /^\s*b\.\w+\(/.test(next)) helpers.add(m[1]);
  });

  const offenders = [];
  lines.forEach((line, i) => {
    const m = line.match(/^\s*(\w+)\s*\(/);
    if (!m || !helpers.has(m[1])) return;
    if (/^\s*(const|return|out\.push)\b/.test(line)) return;
    // 助手自身的折行函数体：`const win = (...) =>` 的下一行就是它的实现，不是漏掉的笔画
    if (/=>\s*$/.test(lines[i - 1] || '')) return;
    offenders.push(`${file}:${i + 1}  ${line.trim()}`);
  });
  check(offenders.length === 0,
        `${file}：${offenders.length === 0 ? '每一笔都进了 out' : '有笔画被丢掉'}`);
  offenders.forEach((o) => console.log(`      ${o}`));

  check(/export (const|default)/.test(src), `${file}：有导出`);
}

console.log();
console.log(`${checks - fails}/${checks} 通过`);
process.exit(fails ? 1 : 0);
