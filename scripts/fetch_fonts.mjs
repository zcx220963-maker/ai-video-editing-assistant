#!/usr/bin/env node
// T16：把 NotoSansSC 的 webfont 片拉进 remotion/public/fonts/，并生成切片表。
//
// 为什么是「一片一片」而不是一个文件：CJK 网络字体**没有单文件形态**。Noto Sans SC
// 在 Google Fonts 那边就是一个 wght 可变字体，按 unicode-range 切成 101 片分别下发
// （浏览器只下它真用到的那几片）。硬凑成单文件要么走 `text=` 子集（只含你点名的那几个
// 字，别的字再落到系统字体——等于把「第六根因：静默回落」又造一遍），要么自己转
// （本机没有 fonttools/brotli，且转换结果与上游不再可比对）。所以照上游的切法照收，
// 101 片共约 4.3 MB，**全覆盖**。这条决定登记在 §9-Q75。
//
// 生成物两份（都进仓库、都进镜像）：
//   remotion/public/fonts/NotoSansSC-NNN.woff2   —— 字节（`public/fonts/` 不在
//        .dockerignore 里，`COPY . .` 会带进镜像；只有 `public/assets/*` 被挡）
//   remotion/src/fonts.notosanssc.generated.ts   —— 切片表（文件名 + unicode-range）
//
// 用法：
//   node scripts/fetch_fonts.mjs            # 联网拉取 + 重新生成
//   node scripts/fetch_fonts.mjs --check    # 不联网：校验盘上文件与表逐项对得上
//
// 为什么 unicode-range 要从 CSS 里抠、不许手抄：这 101 段是上游切的，抄错一段的后果
// 是那一段的字静默落到系统字体——与不装 webfont 是同一种病，只是更隐蔽。

import {createHash} from 'node:crypto';
import {existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const REMOTION = path.join(ROOT, 'remotion');
const FONT_DIR = path.join(REMOTION, 'public', 'fonts');
const TABLE = path.join(REMOTION, 'src', 'fonts.notosanssc.generated.ts');

//: 上游字族名。产物里的 `family` 必须是这个名字——它与 src/tokens.ts 的 fontStack
//: 第二项逐字相同，所以「画面上的字」与「token 声明的字」是同一件事。
const FAMILY = 'Noto Sans SC';
const WEIGHT = '100 900';                       // 可变字体的 wght 轴
//: 上游版本号钉住：URL 里带 vNN，不钉就是「今天构建明天构建两张不同像素」。
const CSS_URL = 'https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;700&display=swap';
//: 现代 UA —— 老 UA 会被下发 ttf/eot，那是另一个格式与另一批 URL。
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

/** 从 css2 回执里抠出 [url, unicodeRange] 对（同一 url 的多段必须一致，否则报错）。 */
function parseCss(css) {
  const byUrl = new Map();
  const blocks = css.split('@font-face').slice(1);
  for (const b of blocks) {
    const url = /url\((https:[^)]+\.woff2)\)/.exec(b);
    const range = /unicode-range:\s*([^;]+);/.exec(b);
    if (!url || !range) continue;
    const key = url[1];
    const val = range[1].replace(/\s+/g, ' ').trim();
    if (byUrl.has(key) && byUrl.get(key) !== val) {
      throw new Error(`同一个 url 出现两段不同的 unicode-range：${key}`);
    }
    byUrl.set(key, val);
  }
  return [...byUrl.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
}

function fileNameFor(i) {
  return `NotoSansSC-${String(i).padStart(3, '0')}.woff2`;
}

function renderTable(slices, totalBytes) {
  const rows = slices
    .map((s) => `  {file: ${JSON.stringify(s.file)}, range: ${JSON.stringify(s.range)}},`)
    .join('\n');
  return `// 由 scripts/fetch_fonts.mjs 生成，勿手改（改了就重跑那个脚本）。
//
// 上游：${CSS_URL}
// ${slices.length} 片 ${totalBytes} 字节（合计），字族 ${FAMILY}，wght ${WEIGHT}。
// unicode-range 逐段照抄上游 CSS——抄错一段，那一段的字就静默落到系统字体。

export const NOTO_SANS_SC = ${JSON.stringify(FAMILY)};
export const NOTO_SANS_SC_WEIGHT = ${JSON.stringify(WEIGHT)};
export const NOTO_SANS_SC_SOURCE = ${JSON.stringify(CSS_URL)};

export type FontSlice = {file: string; range: string};

export const NOTO_SANS_SC_SLICES: readonly FontSlice[] = [
${rows}
];
`;
}

async function fetchTo(url) {
  const r = await fetch(url, {headers: {'User-Agent': UA}});
  if (!r.ok) throw new Error(`HTTP ${r.status} ${url}`);
  return Buffer.from(await r.arrayBuffer());
}

/** 写进盘前先认字：不是 wOF2 就整批失败，绝不把半截文件留在 public 里。 */
function assertWoff2(buf, name) {
  if (buf.length < 4 || buf.toString('latin1', 0, 4) !== 'wOF2') {
    throw new Error(`${name} 不是 woff2（magic=${buf.toString('latin1', 0, 4)}）`);
  }
}

/** 幂等：内容一样就不重写（否则每次跑都动 mtime，git 里是一堆假改动）。 */
function writeIfChanged(file, buf) {
  if (existsSync(file) && readFileSync(file).equals(buf)) return false;
  writeFileSync(file, buf);
  return true;
}

async function main() {
  const checkOnly = process.argv.includes('--check');
  mkdirSync(FONT_DIR, {recursive: true});

  if (checkOnly) {
    if (!existsSync(TABLE)) throw new Error(`缺切片表：${TABLE}`);
    const src = readFileSync(TABLE, 'utf8');
    const pairs = [...src.matchAll(/\{file: ("[^"]+"), range: ("[^"]+")\}/g)]
      .map((m) => ({file: JSON.parse(m[1]), range: JSON.parse(m[2])}));
    if (pairs.length === 0) throw new Error('切片表里一片都没有');
    let total = 0;
    for (const p of pairs) {
      const f = path.join(FONT_DIR, p.file);
      if (!existsSync(f)) throw new Error(`切片表里有、盘上没有：${p.file}`);
      assertWoff2(readFileSync(f), p.file);
      total += statSync(f).size;
    }
    const onDisk = readdirSync(FONT_DIR).filter((n) => n.endsWith('.woff2')).sort();
    if (onDisk.length !== pairs.length) {
      throw new Error(`盘上 ${onDisk.length} 片、表里 ${pairs.length} 片，数目对不上`);
    }
    console.log(`✓ 切片表与盘上 ${pairs.length} 片逐项对得上（合计 ${total} 字节）`);
    return 0;
  }

  const css = await (await fetch(CSS_URL, {headers: {'User-Agent': UA}})).text();
  const pairs = parseCss(css);
  if (pairs.length < 10) throw new Error(`只解析出 ${pairs.length} 片，CSS 结构变了`);

  const slices = [];
  let total = 0;
  for (let i = 0; i < pairs.length; i++) {
    const [url, range] = pairs[i];
    const file = fileNameFor(i);
    const buf = await fetchTo(url);
    assertWoff2(buf, file);
    total += buf.length;
    const wrote = writeIfChanged(path.join(FONT_DIR, file), buf);
    slices.push({file, range, sha: createHash('sha256').update(buf).digest('hex').slice(0, 16), wrote, url});
  }

  writeFileSync(TABLE, renderTable(slices, total), 'utf8');
  console.log(`${slices.length} 片，合计 ${total} 字节（${(total / 1048576).toFixed(2)} MB），`
    + `新写 ${slices.filter((s) => s.wrote).length} 片`);
  console.log(`字族 ${FAMILY} → ${path.relative(ROOT, FONT_DIR)}/，表 ${path.relative(ROOT, TABLE)}`);
  return 0;
}

main().then((c) => process.exit(c)).catch((e) => {
  console.error('拉取失败：' + e.message);
  process.exit(1);
});
