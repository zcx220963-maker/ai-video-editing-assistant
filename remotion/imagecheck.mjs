/**
 * 闸⑬ 的原料：**不依赖任何第三方包**地读出一张图的「尺寸 + 主色档数」。
 *
 * 为什么自己写解码器，而不是装一个 sharp/pngjs：
 *   ① 这两个服务跑在离线容器里，镜像里没有编译工具链，sharp 装不上；
 *   ② 闸⑬ 要判的是**纯色占位**，不是重编码图片——用不着完整解码，够判即可；
 *   ③ 依赖卫生（T49）要求每个新包都进白名单并核许可，为一个「够用就好」的判据
 *      去背一个 C++ 扩展的维护面，不划算。
 * 代价写在明处：**能力边界就是判据的边界**。解不出来的格式一律回 `undetermined`
 * （D-未判不绿），绝不猜一个「看起来是彩的」放行。
 *
 * 三种格式各走各的路（库里的实际分布：svg 102 / jpg 54 / png 2 / jpeg 2）：
 *   · PNG：zlib 解 IDAT + 反滤波，逐像素真采样。8bit、非隔行这一档是完整的。
 *   · JPEG：基线 DC 级解码——只取每个 8×8 块的直流分量（＝该块的平均色），
 *           不做 IDCT。判「纯色 / 两档主色」要的正是块的均值，AC 系数全丢掉
 *           不影响这个判断。渐进式 JPEG 不做（要整张重排，代价不划算）→ 未判。
 *   · SVG：矢量没有「像素」，按它**声明的颜色**计（fill/stroke/stop-color/hex/rgb）。
 *          一个色 → 纯色；一个颜色都不声明 → 未判（不装成「清白」）。
 */

import zlib from 'node:zlib';

const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** 一档主色的占比门槛（量化到 4bit/通道后的格子里占了多少采样点）。
 *  取 2%：够把「有内容的图」和「纯色/近纯色占位」分开，又不至于把低对比度的
 *  真实素材误拒。这个数是**判据的旋钮**，改了要连同红样一起重标。 */
const DOMINANT_SHARE = 0.02;
/** 单一格子的占比超过这个数，直接当纯色占位——留 2% 是给 JPEG 压缩噪声。 */
const SOLID_SHARE = 0.98;

function summarize(buckets, n) {
  if (!n) return null;
  const sorted = [...buckets.entries()].sort((a, b) => b[1] - a[1]);
  const top_share = sorted[0][1] / n;
  const dominant = sorted.filter(([, c]) => c / n >= DOMINANT_SHARE).length;
  return {
    kind: 'pixels',
    distinct: dominant,
    top_share: Number(top_share.toFixed(4)),
    buckets: buckets.size,
    top: sorted.slice(0, 4).map(([k, c]) => ({
      rgb: [(k >> 8) & 15, (k >> 4) & 15, k & 15].map((v) => v * 17),
      share: Number((c / n).toFixed(4)),
    })),
  };
}

// ---------------------------------------------------------------------------
// PNG
// ---------------------------------------------------------------------------
function pngUnfilter(raw, w, h, chan) {
  const stride = w * chan;
  const out = Buffer.alloc(stride * h);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < h; y++) {
    const at = y * (stride + 1);
    const ft = raw[at];
    const cur = out.subarray(y * stride, (y + 1) * stride);
    for (let i = 0; i < stride; i++) {
      const v = raw[at + 1 + i];
      const a = i >= chan ? cur[i - chan] : 0;
      const b = prev[i];
      const c = i >= chan ? prev[i - chan] : 0;
      if (ft === 0) cur[i] = v;
      else if (ft === 1) cur[i] = (v + a) & 255;
      else if (ft === 2) cur[i] = (v + b) & 255;
      else if (ft === 3) cur[i] = (v + ((a + b) >> 1)) & 255;
      else if (ft === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        cur[i] = (v + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c)) & 255;
      } else return null;                      // 未知滤波类型：不许当 0 滤过去
    }
    prev = cur;
  }
  return out;
}

function pngProbe(buf) {
  if (buf.length < 33) return { error: 'PNG 头不完整' };
  let off = 8;
  let w = 0;
  let h = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlace = 0;
  const idat = [];
  let plte = null;
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    if (off + 12 + len > buf.length) break;
    const type = buf.toString('latin1', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (type === 'IHDR') {
      w = data.readUInt32BE(0); h = data.readUInt32BE(4);
      bitDepth = data[8]; colorType = data[9]; interlace = data[12];
    } else if (type === 'PLTE') plte = data;
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    off += 12 + len;
  }
  if (!w || !h) return { error: 'IHDR 缺失或尺寸为 0' };
  const dims = { width: w, height: h };
  if (interlace) return { ...dims, colors: null, undetermined: '隔行（Adam7）PNG 不做像素判据' };
  const chan = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (!chan) return { ...dims, colors: null, undetermined: `未知 colorType ${colorType}` };
  if (bitDepth !== 8) return { ...dims, colors: null, undetermined: `位深 ${bitDepth} 不做像素判据` };
  if (!idat.length) return { ...dims, colors: null, undetermined: '没有 IDAT' };
  if (colorType === 3 && !plte) return { ...dims, colors: null, undetermined: '调色板 PNG 缺 PLTE' };
  let raw;
  try {
    raw = zlib.inflateSync(Buffer.concat(idat));
  } catch {
    return { ...dims, colors: null, undetermined: 'IDAT 解压失败' };
  }
  const stride = w * chan;
  if (raw.length < (stride + 1) * h) return { ...dims, colors: null, undetermined: '像素数据长度不足' };
  const px = pngUnfilter(raw, w, h, chan);
  if (!px) return { ...dims, colors: null, undetermined: '未知 PNG 滤波类型' };

  const buckets = new Map();
  let n = 0;
  // 大图不必逐点：按面积抽样，采样点不少于 ~64×64 已足够定主色。
  const step = Math.max(1, Math.floor(Math.min(w, h) / 64));
  for (let y = 0; y < h; y += step) {
    for (let x = 0; x < w; x += step) {
      const i = (y * w + x) * chan;
      let r; let g; let b;
      if (colorType === 2 || colorType === 6) { r = px[i]; g = px[i + 1]; b = px[i + 2]; }
      else if (colorType === 0 || colorType === 4) { r = g = b = px[i]; }
      else {
        const pi = px[i] * 3;
        if (pi + 2 >= plte.length) return { ...dims, colors: null, undetermined: 'PLTE 索引越界' };
        r = plte[pi]; g = plte[pi + 1]; b = plte[pi + 2];
      }
      const key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
      buckets.set(key, (buckets.get(key) ?? 0) + 1);
      n++;
    }
  }
  return { ...dims, colors: summarize(buckets, n) };
}

// ---------------------------------------------------------------------------
// JPEG（基线，仅 DC）
// ---------------------------------------------------------------------------
const ZIGZAG0 = 0;                             // 直流分量在自然序里的位置就是 0

function buildHuff(counts, syms) {
  const table = new Map();
  let code = 0;
  let idx = 0;
  for (let len = 1; len <= 16; len++) {
    for (let i = 0; i < counts[len - 1]; i++) {
      table.set(len * 65536 + code, syms[idx++]);
      code++;
    }
    code <<= 1;
  }
  return table;
}

class BitReader {
  constructor(buf, pos) { this.buf = buf; this.pos = pos; this.bit = 0; this.cur = 0; }
  _fill() {
    if (this.pos >= this.buf.length) { this.cur = 0; this.bit = 8; return; }
    const b = this.buf[this.pos];
    if (b === 0xff) {
      const n = this.buf[this.pos + 1];
      if (n === 0x00) { this.pos += 2; this.cur = 0xff; this.bit = 8; return; }
      // 撞上标记：熵数据到此为止，喂 0 让调用方发现并停
      this.cur = 0; this.bit = 8; this.hitMarker = n; return;
    }
    this.pos++; this.cur = b; this.bit = 8;
  }
  bit1() { if (this.bit === 0) this._fill(); this.bit--; return (this.cur >> this.bit) & 1; }
  bits(n) { let v = 0; for (let i = 0; i < n; i++) v = (v << 1) | this.bit1(); return v; }
  align() { this.bit = 0; }
  /** 跳过重启标记 RSTn（0xFFD0-0xFFD7）。 */
  skipRestart() {
    this.bit = 0;
    while (this.pos + 1 < this.buf.length) {
      if (this.buf[this.pos] === 0xff && this.buf[this.pos + 1] >= 0xd0
        && this.buf[this.pos + 1] <= 0xd7) { this.pos += 2; this.hitMarker = null; return true; }
      this.pos++;
    }
    return false;
  }
}

const huffDecode = (br, table) => {
  let code = 0;
  for (let len = 1; len <= 16; len++) {
    code = (code << 1) | br.bit1();
    const v = table.get(len * 65536 + code);
    if (v !== undefined) return v;
  }
  throw new Error('Huffman 码表越界');
};

const extend = (v, t) => {
  if (t === 0) return 0;
  const vt = 1 << (t - 1);
  return v < vt ? v - (1 << t) + 1 : v;
};

function jpegProbe(buf) {
  let p = 2;
  let frame = null;
  let progressive = false;
  const dqt = new Map();
  const dht = new Map();
  let sos = null;
  let dri = 0;
  while (p + 3 < buf.length) {
    if (buf[p] !== 0xff) { p++; continue; }
    let m = buf[p + 1];
    while (m === 0xff) { p++; m = buf[p + 1]; }
    p += 2;
    if (m === 0x01 || (m >= 0xd0 && m <= 0xd7)) continue;
    if (m === 0xd9) break;
    if (p + 2 > buf.length) break;
    const len = buf.readUInt16BE(p);
    if (len < 2 || p + len > buf.length) break;
    const seg = buf.subarray(p + 2, p + len);
    if (m === 0xc0 || m === 0xc1 || m === 0xc2) {
      if (m === 0xc2) progressive = true;
      frame = { precision: seg[0], height: seg.readUInt16BE(1), width: seg.readUInt16BE(3), comps: [] };
      const nc = seg[5];
      for (let i = 0; i < nc; i++) {
        const o = 6 + i * 3;
        frame.comps.push({ id: seg[o], h: seg[o + 1] >> 4, v: seg[o + 1] & 15, q: seg[o + 2] });
      }
    } else if (m === 0xc4) {
      let o = 0;
      while (o + 17 <= seg.length) {
        const tc = seg[o] >> 4; const th = seg[o] & 15;
        const counts = [];
        for (let i = 0; i < 16; i++) counts.push(seg[o + 1 + i]);
        const total = counts.reduce((a, b) => a + b, 0);
        if (o + 17 + total > seg.length) break;
        dht.set(tc * 16 + th, buildHuff(counts, seg.subarray(o + 17, o + 17 + total)));
        o += 17 + total;
      }
    } else if (m === 0xdb) {
      let o = 0;
      while (o + 65 <= seg.length) {
        const pq = seg[o] >> 4; const tq = seg[o] & 15;
        const q = new Int32Array(64);
        const need = pq ? 129 : 65;
        if (o + need > seg.length) break;
        for (let i = 0; i < 64; i++) q[i] = pq ? seg.readUInt16BE(o + 1 + i * 2) : seg[o + 1 + i];
        dqt.set(tq, q);
        o += need;
      }
    } else if (m === 0xdd) {
      dri = seg.readUInt16BE(0);
    } else if (m === 0xda) {
      const ns = seg[0];
      sos = { ns, comps: [], ss: seg[1 + ns * 2], se: seg[2 + ns * 2] };
      for (let i = 0; i < ns; i++) {
        const o = 1 + i * 2;
        sos.comps.push({ id: seg[o], dc: seg[o + 1] >> 4, ac: seg[o + 1] & 15 });
      }
      p += len;
      break;
    }
    p += len;
  }
  if (!frame) return { error: '没有 SOF 帧头' };
  const dims = { width: frame.width, height: frame.height };
  if (progressive) return { ...dims, colors: null, undetermined: '渐进式 JPEG 不做 DC 级取色' };
  if (frame.precision !== 8) return { ...dims, colors: null, undetermined: `采样精度 ${frame.precision} bit` };
  if (!sos || sos.ss !== 0) return { ...dims, colors: null, undetermined: '非基线顺序扫描' };
  if (!(sos.ns === frame.comps.length || frame.comps.length === 1)) {
    return { ...dims, colors: null, undetermined: `非交错扫描（扫描分量 ${sos.ns} / 帧分量 ${frame.comps.length}）` };
  }

  const byId = new Map(frame.comps.map((c) => [c.id, c]));
  const scan = sos.comps.map((s) => ({ ...s, comp: byId.get(s.id) }));
  if (scan.some((s) => !s.comp)) return { ...dims, colors: null, undetermined: '扫描分量对不上帧分量' };
  const hmax = Math.max(...frame.comps.map((c) => c.h));
  const vmax = Math.max(...frame.comps.map((c) => c.v));
  const single = frame.comps.length === 1;
  const mcusX = Math.ceil(frame.width / (8 * (single ? scan[0].comp.h : hmax)));
  const mcusY = Math.ceil(frame.height / (8 * (single ? scan[0].comp.v : vmax)));
  if (!mcusX || !mcusY) return { ...dims, colors: null, undetermined: 'MCU 数为 0' };

  const br = new BitReader(buf, p);
  const pred = new Map();
  // 一个 MCU 里某分量可能有 h×v 个块（4:2:0 的 Y 就是 4 个）。这个 MCU 的代表色
  // 是那几块的平均，不是「最后一块」——只取最后一块等于每次只看了 MCU 的四分之一，
  // 一块纯色、其余三块是内容的窄条素材会被误判成纯色。
  const acc = new Map(scan.map((s) => [s.id, { sum: new Float64Array(mcusX * mcusY), n: 0 }]));
  const planes = new Map(scan.map((s) => [s.id, new Float64Array(mcusX * mcusY)]));
  let bad = null;
  try {
    for (let my = 0; my < mcusY && !bad; my++) {
      for (let mx = 0; mx < mcusX && !bad; mx++) {
        const mi = my * mcusX + mx;
        if (dri > 0 && mi > 0 && mi % dri === 0) { br.skipRestart(); pred.clear(); }
        for (const s of scan) {
          const comp = s.comp;
          if (!single && !(comp.h && comp.v)) { bad = '分量采样因子为 0'; break; }
          const cy = single ? 1 : comp.h;
          const cx = single ? 1 : comp.v;                 // 交错扫描里按 h×v 个块
          for (let biy = 0; biy < cy && !bad; biy++) {
            for (let bix = 0; bix < cx && !bad; bix++) {
              const t = huffDecode(br, dht.get(0 * 16 + s.dc) ?? dht.get(0));
              if (t === undefined) { bad = 'DC 哈夫曼表缺失'; break; }
              const diff = t === 0 ? 0 : extend(br.bits(t), t);
              const dc = (pred.get(s.id) ?? 0) + diff;
              pred.set(s.id, dc);
              const q = dqt.get(comp.q);
              const q0 = q ? q[ZIGZAG0] : 16;
              // 反量化后的 DC / 8 = 该块均值，再加 JPEG 的电平位移 128。
              const a = acc.get(s.id);
              a.sum[mi] += Math.max(0, Math.min(255, (dc * q0) / 8 + 128));
              a.n++;
              // AC 全丢掉，但必须完整走一遍位流，否则下一个块的 DC 从错的地方读起。
              // 游程推进是 `k += r`（跳过 r 个零系数）再 `k++`（吃掉这个非零系数），
              // 两处各加一次；写成 `k += r+1` 就会多跳一格，跑到块外去。
              let k = 1;
              const acTable = dht.get(1 * 16 + s.ac) ?? dht.get(1);
              while (k < 64) {
                if (!acTable) { bad = 'AC 哈夫曼表缺失'; break; }
                const rs = huffDecode(br, acTable);
                const r = rs >> 4; const sz = rs & 15;
                if (sz === 0) { if (r === 15) { k += 16; continue; } break; }
                k += r;
                if (k > 63) { bad = 'AC 游程越界'; break; }
                br.bits(sz);
                k++;
              }
            }
          }
        }
      }
    }
  } catch (err) {
    bad = String(err?.message ?? err).slice(0, 80);
  }
  if (bad) return { ...dims, colors: null, undetermined: `JPEG 熵解码中断：${bad}` };

  // 每个 MCU 内该分量有几个块（Y 在 4:2:0 下是 4 个），据此把累加值摊成均值。
  for (const s of scan) {
    const perMcu = single ? 1 : s.comp.h * s.comp.v;
    const a = acc.get(s.id);
    const pl = planes.get(s.id);
    for (let i = 0; i < pl.length; i++) pl[i] = a.sum[i] / perMcu;
  }

  const ids = scan.map((s) => s.id);
  const isRGB = ids.length === 3 && ids[0] === 0x52 && ids[1] === 0x47 && ids[2] === 0x42; // 'R','G','B'
  if (ids.length === 4) return { ...dims, colors: null, undetermined: '四分量 JPEG（CMYK/YCCK）不做取色' };
  const buckets = new Map();
  let n = 0;
  for (let i = 0; i < mcusX * mcusY; i++) {
    let r; let g; let b;
    if (single) { r = g = b = planes.get(ids[0])[i]; } else if (isRGB) {
      r = planes.get(ids[0])[i]; g = planes.get(ids[1])[i]; b = planes.get(ids[2])[i];
    } else if (ids.length === 3) {
      const Y = planes.get(ids[0])[i]; const Cb = planes.get(ids[1])[i] - 128; const Cr = planes.get(ids[2])[i] - 128;
      r = Y + 1.402 * Cr;
      g = Y - 0.344136 * Cb - 0.714136 * Cr;
      b = Y + 1.772 * Cb;
    } else { return { ...dims, colors: null, undetermined: `分量数 ${ids.length} 不做取色` }; }
    const cl = (v) => Math.max(0, Math.min(255, Math.round(v)));
    const key = ((cl(r) >> 4) << 8) | ((cl(g) >> 4) << 4) | (cl(b) >> 4);
    buckets.set(key, (buckets.get(key) ?? 0) + 1);
    n++;
  }
  return { ...dims, colors: summarize(buckets, n), colors_from: 'dc_only' };
}

// ---------------------------------------------------------------------------
// SVG
// ---------------------------------------------------------------------------
function svgProbe(buf) {
  const text = buf.toString('utf8', 0, Math.min(buf.length, 512 * 1024));
  if (!/<svg[\s>]/i.test(text)) return { error: '没有 <svg> 根元素' };
  let width = null;
  let height = null;
  const vb = /\bviewBox\s*=\s*["']\s*([-\d.]+)[\s,]+([-\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(text);
  const wi = /\bwidth\s*=\s*["']\s*([\d.]+)/i.exec(text);
  const hi = /\bheight\s*=\s*["']\s*([\d.]+)/i.exec(text);
  if (wi) width = Number(wi[1]);
  if (hi) height = Number(hi[1]);
  if ((!width || !height) && vb) { width = width || Number(vb[3]); height = height || Number(vb[4]); }
  const dims = { width, height };
  const colors = new Set();
  const add = (s) => { const v = String(s).toLowerCase().replace(/\s+/g, ''); if (v) colors.add(v); };
  for (const m of text.match(/#[0-9a-fA-F]{6}(?![0-9a-fA-F])/g) ?? []) add(m);
  for (const m of text.match(/#[0-9a-fA-F]{3}(?![0-9a-fA-F])/g) ?? []) add(m);
  for (const m of text.match(/rgba?\([^)]*\)/g) ?? []) add(m);
  const paint = /\b(?:fill|stroke|stop-color|color)\s*[:=]\s*["']?\s*([a-z]+)/gi;
  for (const m of text.matchAll(paint)) {
    const v = m[1].toLowerCase();
    if (v === 'none' || v === 'transparent' || v === 'currentcolor' || v === 'inherit') continue;
    add(v);
  }
  if (!colors.size) {
    return { ...dims, colors: null,
             undetermined: 'SVG 没有声明任何颜色（默认黑填充 / 只有内嵌位图）——按声明的颜色判，看不到就不判' };
  }
  return { ...dims,
           colors: { kind: 'declared', distinct: colors.size, declared: [...colors].slice(0, 12) },
           colors_from: 'svg_declared' };
}

// ---------------------------------------------------------------------------
export function fileExt(name) {
  const m = /\.([A-Za-z0-9]+)\s*$/.exec(String(name ?? ''));
  return m ? m[1].toLowerCase() : '';
}

/** 读一张图。返回 `{width,height,colors,format}` 或 `{error}` 或 `{undetermined}`。
 *
 *  `colors` 为 null ＋ `undetermined` 有值 ＝ **这一件没判**（不是通过、也不是拒绝）。
 *  调用方必须把这一档如实回传，不许折叠成 ok。 */
export function probeImage(buf, ext) {
  const e = String(ext ?? '').toLowerCase().replace(/^\./, '');
  try {
    if (e === 'png' || (buf.length > 8 && buf.subarray(0, 8).equals(PNG_SIG))) {
      const r = pngProbe(buf);
      return { ...r, format: 'png' };
    }
    if (e === 'jpg' || e === 'jpeg' || (buf[0] === 0xff && buf[1] === 0xd8)) {
      const r = jpegProbe(buf);
      return { ...r, format: 'jpeg' };
    }
    if (e === 'svg') return { ...svgProbe(buf), format: 'svg' };
  } catch (err) {
    return { error: `${e || '未知'} 解析异常：${String(err?.message ?? err).slice(0, 100)}` };
  }
  return { error: `不认识的图片格式 ${e || '(无扩展名)'}` };
}

/** 闸⑬ 的判定：一张图的字节够不够当「画面主体」用。
 *
 *  返回 `{verdict, reason}`——verdict ∈ `pass | reject | undetermined`。
 *  「至少两档主色」与「非 1×1」两句判据都在这里，措辞只此一处。 */
export function judgeAsset(buf, ext) {
  const r = probeImage(buf, ext);
  if (r.error) return { verdict: 'undetermined', reason: r.error, probe: r };
  const w = Number(r.width);
  const h = Number(r.height);
  if (w && h && (w < 2 || h < 2)) {
    return { verdict: 'reject', reason: `占位图：${w}×${h}（1×1 这类像素不构成画面主体）`, probe: r };
  }
  if (!r.colors) {
    return { verdict: 'undetermined', reason: r.undetermined ?? '取色失败', probe: r };
  }
  const c = r.colors;
  const size = `${r.format} ${w || '?'}×${h || '?'}`;
  if (c.kind === 'declared') {
    // 矢量图没有「像素」这回事：它声明几个颜色就是几档。因此措辞也不该抄像素那套。
    if (c.distinct < 2) {
      return { verdict: 'reject',
               reason: `${size}：只声明了 ${c.distinct} 种颜色 `
                       + `（${(c.declared ?? []).join(' / ')}）——矢量画面主体至少要两种`,
               probe: r };
    }
    return { verdict: 'pass',
             reason: `${size}，声明了 ${c.distinct} 种颜色（${(c.declared ?? []).slice(0, 4).join(' / ')})`,
             probe: r };
  }
  if (c.top_share >= SOLID_SHARE) {
    return { verdict: 'reject',
             reason: `纯色占位：${(c.top_share * 100).toFixed(1)}% 的像素是同一个颜色 `
                     + `${JSON.stringify(c.top[0]?.rgb ?? [])}（${r.colors_from ?? 'pixel'}）`,
             probe: r };
  }
  if (c.distinct < 2) {
    return { verdict: 'reject',
             reason: `只有 ${c.distinct} 档主色（占优色 ${(c.top_share * 100).toFixed(1)}%），`
                     + '画面主体至少要有两档',
             probe: r };
  }
  return { verdict: 'pass',
           reason: `${size}，${c.distinct} 档主色（占优 ${(c.top_share * 100).toFixed(1)}%）`,
           probe: r };
}

export const THRESHOLDS = { DOMINANT_SHARE, SOLID_SHARE };
