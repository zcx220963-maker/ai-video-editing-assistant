/**
 * 闸⑬ 的先证红再证绿用例：**用了图但画面没图**这一族，每一支都要有它自己的红。
 *
 * 契约（§6 R4-T8）点名的三张红样，一张都不能少：
 *   · 1×1 PNG           —— 尺寸就不构成画面主体
 *   · 全白图            —— 有尺寸、有字节，但纯色占位
 *   · manifest 有记录但文件缺 —— 账上有、盘上没有，链路的第二段断了
 * 三支都要**各出一句人话**：合成一句「资产不存在」等于把「换张图」和「修投递」
 * 混成同一个待办，拿到回执的人只能靠猜。
 *
 * 另外三支是「不许把未判读成通过」与「不该动的别动」：
 *   · 渐进式 JPEG / 拿不到 manifest → `undetermined`，**必须**与 ok 分开；
 *   · 一镜没素材 → ok（没有可判的东西，不是未判）；
 *   · sha16 与盘上字节不符 → 拒（这条是 ⑬ 与投递申请口各算一遍的那个数）。
 *
 * 图是**现造的**，不从仓里读也不调 ffmpeg：判据的边界（1×1 / 纯色 / 两档主色 / 单色 SVG）
 * 要能被这支脚本随意摆布，而不是被某张现成素材的长相绑住。
 *
 * 跑法：`node tests/gate13-assets.mjs`（退出码即结论；Dockerfile 里进硬验链）。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { createHash } from 'node:crypto';

// DG_PUBLIC_ROOT 必须在 import assetgate **之前**设好：PUBLIC 是模块加载时算的。
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'gate13-'));
process.env.DG_PUBLIC_ROOT = path.join(TMP, 'public');

const { gateAssets, writeManifest, shotPublicDir } = await import('../assetgate.mjs');
const { fileExt } = await import('../imagecheck.mjs');
const { JPG_WHITE_B64, JPG_GRAD_B64 } = await import('./fixtures-gate13.mjs');

// ---------------------------------------------------------------------------
// 现造一张真 PNG（8bit RGB，无滤波），保证判据那边解出来的是我摆的那些像素
// ---------------------------------------------------------------------------
const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();
const crc32 = (buf) => {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
};

function pngChunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'latin1'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

/** `px(x,y) -> [r,g,b]`。 */
function makePng(w, h, px) {
  const stride = w * 3 + 1;
  const raw = Buffer.alloc(stride * h);
  for (let y = 0; y < h; y++) {
    raw[y * stride] = 0;
    for (let x = 0; x < w; x++) {
      const [r, g, b] = px(x, y);
      const o = y * stride + 1 + x * 3;
      raw[o] = r; raw[o + 1] = g; raw[o + 2] = b;
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', zlib.deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

const PNG_1X1 = makePng(1, 1, () => [12, 34, 56]);
const PNG_WHITE = makePng(16, 16, () => [255, 255, 255]);
/** 两档主色的正常图：上下各一半。这是「正常图过」那一支。 */
const PNG_TWO = makePng(16, 16, (x, y) => (y < 8 ? [30, 60, 120] : [220, 170, 40]));
/** 平滑渐变：占优格子占比很低。它**不是**纯色，不该被纯色那条误杀。 */
const PNG_GRAD = makePng(64, 64, (x) => [x * 4, x * 4, 128]);

const SVG_FLAT = Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
  + '<rect width="100" height="100" fill="#ffffff"/></svg>', 'utf8');
const SVG_RICH = Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="150" viewBox="0 0 200 150">'
  + '<rect width="200" height="150" fill="#123456"/>'
  + '<circle cx="60" cy="60" r="40" fill="#e8b04b"/>'
  + '<path d="M0 150 L200 20" stroke="#2fae7a" stroke-width="8"/></svg>', 'utf8');
const SVG_NOCOLOR = Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
  + '<rect width="10" height="10"/></svg>', 'utf8');

// 真 JPEG 的字节：单色（全白）与渐变各一，用来证 DC 级取色这条路真的通。
// 字节在 `fixtures-gate13.mjs` 里（ffmpeg 编码器输出、机械内联），测试不依赖
// 容器里有 ffmpeg，也不依赖我手工誊写有没有错一个字符。
const JPG_WHITE = Buffer.from(JPG_WHITE_B64, 'base64');
const JPG_GRAD = Buffer.from(JPG_GRAD_B64, 'base64');

// ---------------------------------------------------------------------------
let checks = 0;
let fails = 0;
const check = (cond, label) => {
  checks++;
  console.log(`  ${cond ? '✓' : '✗'} ${label}`);
  if (!cond) fails++;
};

const sha16 = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 16);

/** 摆一镜：把素材写进 `<public>/assets/<sid>/`，并写 manifest。 */
function stage(sid, entries) {
  const dir = shotPublicDir(sid);
  fs.mkdirSync(dir, { recursive: true });
  const items = [];
  for (const [name, buf, over] of entries) {
    if (buf !== null) fs.writeFileSync(path.join(dir, name), buf);
    items.push({
      name,
      sha16: over?.sha16 ?? sha16(buf),
      bytes: buf ? buf.length : 0,
      ext: over?.ext ?? fileExt(name),
      motion: over?.motion ?? 'undetermined',
    });
  }
  fs.writeFileSync(path.join(dir, '_manifest.json'), JSON.stringify({ sid, items }));
  return dir;
}

// ---------------------------------------------------------------------------
console.log('闸⑬ / lint 的素材判据：三张红样 + 不许把未判读成通过\n');

// --- T8 红样 ①：1×1 PNG -----------------------------------------------------
{
  console.log('=== 红样① 1×1 PNG：尺寸不构成画面主体 ===');
  stage('s01', [['one.png', PNG_1X1]]);
  const g = await gateAssets('s01');
  check(g.status === 'rejected', `整体判 rejected（实际 ${g.status}）`);
  const it = g.items[0] ?? {};
  check(it.verdict === 'reject' && /1×1/.test(it.reason),
        `逐件理由点名了 1×1：${String(it.reason).slice(0, 60)}`);
  check(g.reason.includes('one.png'),
        `整体回话点得出是哪一件：${String(g.reason).slice(0, 60)}`);
}

// --- T8 红样 ②：全白图 ------------------------------------------------------
{
  console.log('\n=== 红样② 全白 PNG：有尺寸、有字节，但纯色 ===');
  stage('s02', [['white.png', PNG_WHITE]]);
  const g = await gateAssets('s02');
  check(g.status === 'rejected', `整体判 rejected（实际 ${g.status}）`);
  const it = g.items[0] ?? {};
  check(it.verdict === 'reject' && /纯色/.test(it.reason),
        `逐件理由说的是纯色（不是「不存在」）：${String(it.reason).slice(0, 60)}`);
  check(it.colors && it.colors.top_share === 1,
        `回执带上了实测占比 top_share=${it.colors?.top_share}`);
  // 同一支判据对 JPEG 也要成立——DC 级取色那条路走通才说明「全白图」不是只防 PNG。
  stage('s02b', [['white.jpg', JPG_WHITE]]);
  const gj = await gateAssets('s02b');
  check(gj.status === 'rejected' && /纯色/.test(gj.items[0]?.reason ?? ''),
        `全白 JPEG 同样拒（DC 级取色）：${String(gj.items[0]?.reason).slice(0, 60)}`);
  // 反过来说：真编码器出的多色 JPEG 必须**过**——只验「纯色被拒」的话，
  // 一个「永远返回纯色」的解码器也能让上面那条绿。
  stage('s02c', [['grad.jpg', JPG_GRAD]]);
  const gg = await gateAssets('s02c');
  check(gg.status === 'ok' && (gg.items[0]?.colors?.distinct ?? 0) >= 2,
        `多色 JPEG 过（真 encoder 字节，DC 级取到 ${gg.items[0]?.colors?.distinct} 档主色）`);
}

// --- T8 红样 ③：manifest 有记录但文件缺 -------------------------------------
{
  console.log('\n=== 红样③ manifest 有记录、盘上没文件 ===');
  const dir = shotPublicDir('s03');
  fs.mkdirSync(dir, { recursive: true });
  // 故意**不**写 chart.png，只写 manifest
  fs.writeFileSync(path.join(dir, '_manifest.json'), JSON.stringify({
    sid: 's03',
    items: [{ name: 'chart.png', sha16: sha16(PNG_TWO), bytes: PNG_TWO.length,
              ext: 'png', motion: 'undetermined' }],
  }));
  const g = await gateAssets('s03');
  check(g.status === 'rejected', `整体判 rejected（实际 ${g.status}）`);
  const it = g.items[0] ?? {};
  check(it.verdict === 'reject' && /盘上没有这个文件/.test(it.reason),
        `理由说的是「盘上没有」（不是「格式不对」）：${String(it.reason).slice(0, 70)}`);
}

// --- 绿：两档主色的正常图 ---------------------------------------------------
{
  console.log('\n=== 绿：两档主色的 PNG 过 ===');
  stage('s04', [['chart.png', PNG_TWO]]);
  const g = await gateAssets('s04');
  check(g.status === 'ok', `整体判 ok（实际 ${g.status}：${g.reason.slice(0, 60)}）`);
  check(g.items[0].verdict === 'pass' && g.items[0].colors.distinct >= 2,
        `回执带上尺寸与主色档数：${g.items[0].width}×${g.items[0].height} `
        + `${g.items[0].colors?.distinct} 档`);
}

// --- 绿：平滑渐变不该被「纯色」那条误杀 -------------------------------------
{
  console.log('\n=== 绿：平滑渐变不是纯色（占优格子占比很低）===');
  stage('s05', [['grad.png', PNG_GRAD]]);
  const g = await gateAssets('s05');
  check(g.status === 'ok',
        `渐变图判 ok（占优 ${g.items[0]?.colors?.top_share}）——`
        + '判据是「有没有纯色占位」，不是「有没有强对比」');
}

// --- 绿：多色 SVG -----------------------------------------------------------
{
  console.log('\n=== 绿/红：矢量图按它声明的颜色判 ===');
  stage('s06', [['art.svg', SVG_RICH]]);
  const g = await gateAssets('s06');
  check(g.status === 'ok', `三种颜色的 SVG 过（${g.items[0]?.colors?.distinct} 种）`);
  stage('s07', [['flat.svg', SVG_FLAT]]);
  const g2 = await gateAssets('s07');
  check(g2.status === 'rejected' && /种颜色/.test(g2.items[0]?.reason ?? ''),
        `单色 SVG 拒，且措辞是「声明了几种颜色」而不是像素口径：`
        + `${String(g2.items[0]?.reason).slice(0, 60)}`);
}

// --- 未判：不许读成通过 -----------------------------------------------------
{
  console.log('\n=== 未判档：拿不到判据的，一律 undetermined（不是 ok）===');
  stage('s08', [['weird.svg', SVG_NOCOLOR]]);
  const g = await gateAssets('s08');
  check(g.status === 'undetermined',
        `一个颜色都没声明的 SVG → undetermined（实际 ${g.status}）`);
  const g2 = await gateAssets('s-no-manifest');
  check(g2.status === 'undetermined',
        `manifest 读不着 → undetermined（实际 ${g2.status}：${g2.reason.slice(0, 50)}）`);
  const g3 = await gateAssets('');
  check(g3.status === 'undetermined',
        `没有镜号 → undetermined（实际 ${g3.status}）`);
  // 空 items：这一镜本来就没素材，那是 ok 而不是未判
  await writeManifest('s09', []);
  const g4 = await gateAssets('s09');
  check(g4.status === 'ok',
        `空 manifest → ok（没有可判的东西 ≠ 没判）（实际 ${g4.status}）`);
}

// --- 拒：账与盘不是同一份字节 -----------------------------------------------
{
  console.log('\n=== 红：manifest 登记的 sha16 与盘上字节不符 ===');
  // 盘上放 A，manifest 里写 B 的 sha16
  stage('s10', [['chart.png', PNG_TWO, { sha16: sha16(PNG_WHITE) }]]);
  const g = await gateAssets('s10');
  check(g.status === 'rejected' && /sha16/.test(g.items[0]?.reason ?? ''),
        `拒，且两个数都在回话里：${String(g.items[0]?.reason).slice(0, 90)}`);
}

// --- 拒：素材名越界 ---------------------------------------------------------
{
  console.log('\n=== 红：manifest 里的名字带路径穿越 ===');
  const dir = shotPublicDir('s11');
  fs.mkdirSync(path.join(TMP, 'public'), { recursive: true });
  fs.writeFileSync(path.join(TMP, 'secret.png'), PNG_TWO);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, '_manifest.json'), JSON.stringify({
    sid: 's11',
    items: [{ name: '../secret.png', sha16: sha16(PNG_TWO), bytes: PNG_TWO.length, ext: 'png' }],
  }));
  const g = await gateAssets('s11');
  check(g.status === 'rejected' && /越界/.test(g.items[0]?.reason ?? ''),
        `拒，理由是名字越界：${String(g.items[0]?.reason).slice(0, 60)}`);
}

// --- 收尾：临时目录清掉 -----------------------------------------------------
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* 清不掉不影响结论 */ }

console.log(fails === 0 ? `\nPASS ${checks}/${checks}` : `\nFAIL ${fails}/${checks}`);
process.exitCode = fails === 0 ? 0 : 1;
