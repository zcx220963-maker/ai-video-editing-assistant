// T16b：`@remotion/effects` 的**先验**——它进不进白名单，由这一趟真渲说了算。
//
// 为什么它不能直接进名单（§3.1 那张表把「采纳」列写成「先验后采（R5-T16b）」就是为此）：
//
//   ① **`glow 染品红` 前例**：这一族效果是 WebGL2 着色器，历史上的坏法不是崩，是
//      **悄悄染上一层颜色**——画面出得来、帧数也对，只是颜色不是声明的那些。
//      这种缺陷任何「渲染返回 200」都判不出来，只能拿**像素**和**参数**对账。
//   ② **`swangle` 是软 GL**（`tests/gl-probe.mjs` 实测：angle/egl/swiftshader 三档
//      都拿不到 WebGL context，只有 swangle 给 200）。软实现上的着色器行为必须实测。
//   ③ 容器里的 Chromium 是镜像自带的，**换浏览器就得重跑本探针**（同 gl-probe）。
//
// 判据三条，都判在**真渲出来的像素**上：
//
//   A `paper()`            —— 不崩，且画面里**没有它参数之外的颜色**（品红探测器）；
//   B `halftone()` solid   —— 不崩，点真画出来了（防「effect 静默没生效」），
//                            且除声明的点色与底色之外**没有第三种颜色**；
//   C `effects` 挂在**非官方宿主**上 —— **必须与不挂时逐字节相同**（钉住它是静默空操作）。
//
// C 那一条为什么要钉：官方文档写明 `effects` 只认 `<Img>/<Solid>/<HtmlInCanvas>`，
// 但 React 对未知 DOM 属性既不报错也不警告——挂错了地方就是**一声不响地没效果**，
// 出片看着正常、颜色也对，只是滤镜从没跑过。这种「看着对」的静默正是这条链一路在治的。
//
// 还有一问：上面三条全绿，会不会只是因为**判据从不说话**？所以末尾有两支**该被拒**的
// 自检样本（D1 拿真帧把绿通道压一半〔人工染品红〕，D2 干脆不挂 effects），判据对它们必须红——那才是
// 「判据真在判」的取证。绿而判据是哑的，与红一样没用。
//
// **第二件要先验的事**（§9-Q89）：`paper`/`halftone` **不在** `@remotion/effects` 的桶
// 文件里。4.0.534 的 `dist/index.d.ts` 只重导 11 件，这两件只能从 `@remotion/effects/paper`
// 这样拿；写包名拿到的是 `undefined`，**打包期不报错**，渲染时才炸一句
// 「paper is not a function」。所以白名单按**子路径逐件**放行，不透支桶名。
//
// 用法（容器内，代码已烤进镜像）：
//     docker exec -e DG_BASE=http://127.0.0.1:8010 creation-remotion sh -c 'cd /srv && node tests/effects-probe.mjs'
//
// 退出码：0 三条判据 + 两条自检全过（**这是进白名单的前提**）；1 有红（**不进名单**，永久停在先验后采）。
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'effectsprobe-'));

//: 探针源图：一块**中性灰**。刻意不用彩色——B 那一条要判「除声明的颜色外没有第三种」，
//: 源图自己带颜色就会把这条判据糊掉。走 data URI 而不是 staticFile：这一趟判的是
//: **效果**，投递腿（T4/P6）另有一整套判据，把两件事绑在一起会让红指向错的地方。
const GRAY = '#7f7f7f';
const SVG = 'data:image/svg+xml,' + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800">` +
  `<rect width="800" height="800" fill="${GRAY}"/></svg>`);

//: A 那一条要 `paper()` 的**两个声明色都不满足品红签名**（见那条注释），所以给了
//: `#c0392b` 红棕 + 暖白。暖白这里写的是字面量而不是母版底色：它是**渲染入参**，
//: 得在拼代码字符串的时候就定下来。B 那一侧不同——它判的是「画面里有没有第三种颜色」，
//: 那个底色是从母版回读拿的（`tokens.bg`），见 `render()` 的回读。
//: **这一趟先验的第一个发现**：`paper`/`halftone` 不在 `@remotion/effects` 的桶文件里。
//: 4.0.534 的 `dist/index.d.ts` 只重导 11 件（checkerboard / pattern / tile / rings /
//: starburst / lightLeak / gridlines / zigzag / linearGradient / linearGradientTint /
//: cornerPin），其余 ~60 件（含 paper、halftone、blur、glow、vignette…）**只能走子路径**。
//: 所以 `import { paper } from '@remotion/effects'` 拿到的是 `undefined`——渲染器的报错
//: 是「`paper` is not a function」，而**打包期不报错**。白名单要是按桶名放行，
//: 就是放行一行注定运行期炸的 import（§9-Q89）。
const HEAD = `import React from 'react';
import { Img, AbsoluteFill } from 'remotion';
import { FilmLayout, useBody } from '@dg/film';
import { useT } from '@dg/motion';
import { paper } from '@remotion/effects/paper';
import { halftone } from '@remotion/effects/halftone';

const SRC = ${JSON.stringify(SVG)};
`;

//: 这份夹具**自己也要过闸①**（`/render-shot` 口内部再 lint 一遍，见 server.mjs:139）。
//: 所以它必须长得像一镜：`@dg/motion` 的工具要 import **且用了**（`useT`），纯色块不能
//: 用 `<div style={{background:'#…'}}>` 画（`dg/no-raw-styled-div`）——底色交给母版，
//: 需要一块灰时用**渐变**（规则明文放行 gradient）。探针自己过不了闸①就成了
//: 「探针坏了」，与「效果坏了」是两件事，不能混成一个红。
const shot = (inner) => `${HEAD}
const Shot = () => {
  const body = useBody();
  // 闸① 的 must-use-motion：import 了 @dg/motion 的工具就得**真用上**。
  // 这一支不拍动效，于是把 t 用在字幕文本里——字幕在帧 0 是 0 透明度（母版自带的
  // 入场），既不占画面带的像素，也还是个真引用。
  const t = useT();
  return (
    <FilmLayout text={'效果探针 t=' + t.toFixed(3)} aspect="portrait">
${inner}
    </FilmLayout>
  );
};
export default Shot;
`;

const CASES = [
  {
    name: 'A paper()',
    // 两个声明色都满足 **B 通道 < G 通道**（#c0392b 的红棕与纸色的暖白）。于是整个
    // 合法色域都落在「蓝不超过绿」这一侧——**一个品红像素（B 远大于 G）就是参数外的颜色**，
    // 不用去猜这个效果的调色逻辑。
    code: shot(`      <Img src={SRC} effects={[paper({ colorFront: '#c0392b',
        colorBack: '#f6f1e4', contrast: 0.3, roughness: 0.4, seed: 6 })]}
        style={{ width: body.w, height: body.h }} />`),
    check: (px, w, h) => {
      const n = w * h;
      let magenta = 0, minR = 255, maxR = 0;
      for (let i = 0; i < n; i++) {
        const r = px[i * 3], g = px[i * 3 + 1], b = px[i * 3 + 2];
        // 「染品红」的签名：红高、蓝高、绿低。两个声明色里没有一个是这样。
        if (r > 140 && b > 140 && g + 30 < b) magenta++;
        if (r < minR) minR = r;
        if (r > maxR) maxR = r;
      }
      return [
        [maxR - minR > 40,
          `effect 真跑了：红通道跨度 ${maxR - minR}（纯灰源本该是 0）`],
        [magenta / n < 0.005,
          `参数外颜色（品红签名）占比 ${(magenta / n * 100).toFixed(3)}% < 0.5%`],
      ];
    },
  },
  {
    name: 'B halftone() solid',
    // solid 模式：点只有 dotColor 一种颜色，其余是透过来的底色。**声明了几种颜色，
    // 画面里就只许有几种**（留一点抗锯齿容差）。底色不写死——用母版回读的 `tokens.bg`，
    // 那才是透过来的那一层。
    code: shot(`      <Img src={SRC} effects={[halftone({ colorMode: 'solid',
        dotColor: '#102030', dotSize: 40, dotSpacing: 40 })]}
        style={{ width: body.w, height: body.h }} />`),
    check: (px, w, h, meta) => {
      const n = w * h;
      const [dr, dg, db] = [0x10, 0x20, 0x30];
      const bg = hex3(meta.bg);
      const near = (r, g, b, R, G, B) =>
        Math.abs(r - R) + Math.abs(g - G) + Math.abs(b - B) < 60;
      let dots = 0, foreign = 0;
      for (let i = 0; i < n; i++) {
        const r = px[i * 3], g = px[i * 3 + 1], b = px[i * 3 + 2];
        if (near(r, g, b, dr, dg, db)) dots++;
        else if (!near(r, g, b, bg[0], bg[1], bg[2])) foreign++;
      }
      return [
        [dots / n > 0.02,
          `点真画出来了：点色占 ${(dots / n * 100).toFixed(1)}%（>2%；静默没生效的话是 0）`],
        [foreign / n < 0.05,
          `除「点色 + 底色（母版回读的 ${meta.bg}）」之外的第三色占 ` +
          `${(foreign / n * 100).toFixed(2)}% < 5%`],
      ];
    },
  },
  {
    name: 'C effects 挂在 <AbsoluteFill> 上',
    // 官方只认 <Img>/<Solid>/<HtmlInCanvas>。挂错宿主**必须与不挂时逐字节相同**。
    // 两支分别渲一次，比的是真字节。
    code: shot(`      <AbsoluteFill effects={[paper({ seed: 6 })]}
        style={{ background: 'linear-gradient(180deg, ${GRAY}, ${GRAY})' }} />`),
    codeWithout: shot(`      <AbsoluteFill
        style={{ background: 'linear-gradient(180deg, ${GRAY}, ${GRAY})' }} />`),
    checkNoop: (a, b) => {
      let diff = 0;
      for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] !== b[i]) diff++;
      return [[diff === 0 && a.length === b.length,
        `挂与不挂逐字节相同（${a.length} 字节里差 ${diff}）——挂错宿主是静默空操作`]];
    },
  },
];

/** `#f6f1e4` → `[246, 241, 228]`。回读不到就抛——拿猜的底色判颜色，判的是猜色。 */
function hex3(s) {
  const m = /^#([0-9a-f]{6})$/i.exec(String(s || '').trim());
  if (!m) throw new Error(`母版没回读到底色 tokens.bg=${JSON.stringify(s)}——没有基准色就不判颜色`);
  const v = parseInt(m[1], 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

const post = async (p, payload) => {
  const r = await fetch(`${BASE}${p}`, {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify(payload),
  });
  return {status: r.status, json: await r.json()};
};

let fail = 0;
const say = (ok, msg) => {
  if (!ok) fail++;
  console.log(`${ok ? '  ok ' : '  XX '} ${msg}`);
};

console.log('T16b `@remotion/effects` 先验（每支真渲一镜，判在像素上）');
console.log('=======================================================\n');

const rendered = new Map();

async function render(name, code) {
  const key = `effectsprobe-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const t0 = Date.now();
  const res = await post('/render-shot', {
    code,
    cache_key: key,
    declared_fields: [],
    input_props: {fps: 30, durationInFrames: 2, aspect: 'portrait', text: name},
  });
  const j = res.json;
  console.log(`[${name}] 用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  if (!j.ok) {
    say(false, `渲染失败（**这一条就是判红**）：${String(j.error).slice(0, 300)} ` +
      JSON.stringify(j.errors || j.lint_errors || '').slice(0, 300));
    return null;
  }
  if (!j.artifact) {
    say(false, `响应里没有 artifact：${JSON.stringify(j).slice(0, 200)}`);
    return null;
  }
  const clip = path.join(WORK, `${name.replace(/\W+/g, '_')}.mp4`);
  const art = await fetch(`${BASE}${j.artifact}`);
  if (!art.ok) {
    say(false, `取产物失败：HTTP ${art.status}`);
    return null;
  }
  fs.writeFileSync(clip, Buffer.from(await art.arrayBuffer()));
  const raw = path.join(WORK, `${name.replace(/\W+/g, '_')}.rgb`);
  execFileSync('ffmpeg', ['-v', 'error', '-i', clip, '-frames:v', '1',
    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-y', raw]);
  const px = fs.readFileSync(raw);
  const w = Number(j.width || j.probed?.width || 1080);
  const h = px.length / 3 / w;
  console.log(`  产物 ${px.length} 字节 ≈ ${w}×${h}`);
  // 底色从**母版回读**里拿（`tokens` 就是母版 console.log 的那份），不写死常量：
  // 写死的话母版改了配色，探针会拿旧色去判「有没有第三种颜色」，红了也指错地方。
  return {px, w, h, bg: j.tokens?.bg};
}

for (const c of CASES) {
  const got = await render(c.name, c.code);
  rendered.set(c.name, got);
  if (!got) continue;
  const results = c.checkNoop
    ? null
    : c.check(got.px, got.w, got.h, {bg: got.bg});
  if (results) for (const [ok, msg] of results) say(ok, msg);
  console.log('');
}

// C 的对照那一支要单独渲一次，才能比字节。
const cCase = CASES.find((c) => c.name.startsWith('C'));
const withFx = rendered.get(cCase.name);
if (withFx) {
  const without = await render('C 对照（不挂 effects）', cCase.codeWithout);
  if (without) {
    for (const [ok, msg] of cCase.checkNoop(withFx.px, without.px)) say(ok, msg);
  }
}

// ── 判据自检（D-红先）───────────────────────────────────────────────────────
//
// 上面每一条绿，都可能是「判据从不说话」——探测器写得过松、或者它判的那个量
// 在画面里根本不为零。所以这里造三个**该被拒**的样本，看判据是不是真红。
//
// 关于 D1 为什么不是「把 colorFront 改成品红再渲一镜」：试过了，**判绿**。
// `paper()` 把正面色揉进纸纹里、又和 colorBack 混，正面色是品红时画面仍是一片暖白，
// 品红探测器抓不到。这条「构造样本」看着像在测判据，其实测的是**效果肯不肯露出
// 那个颜色**——它红不了不代表判据哑。所以改成直接把真帧的绿通道压一半：这正是
// `glow 染品红` 的签名（绿被压、蓝相对抬起来），也是这条判据声称要抓的东西。
//
// 三支的「该红」由 `judge` 写死；红对了才 `say(true)`，绿了就是判据哑，红不了就是红。
console.log('判据自检（D-红先：造该被拒的样本，看判据真不真红）');
const magentaify = (px) => {
  const out = Buffer.from(px);
  for (let i = 1; i < out.length; i += 3) out[i] = Math.round(out[i] * 0.5);
  return out;
};
const gotA = rendered.get(CASES[0].name);
const SELF = [];
if (gotA) {
  SELF.push({
    name: 'D1 真帧人工染品红（绿通道压一半）',
    // 不重渲：拿 A 那支真渲出来的帧改通道——判据要抓的就是**颜色**，而这一改
    // 恰好就是那个坏法的签名。
    px: magentaify(gotA.px),
    probe: CASES[0].check,
    meta: {bg: gotA.bg},
    judge: (r) => r[1][0] === false,   // A 的第 2 条：品红探测器
    label: '品红探测器对「染品红」的帧真红（判据不是从来不说话）',
  });
}
SELF.push({
  name: 'D2 同一张图不挂 effects',
  code: shot(`      <Img src={SRC}
        style={{ width: body.w, height: body.h }} />`),
  probe: CASES[1].check,
  judge: (r) => r[0][0] === false,     // B 的第 1 条：点真画出来了
  label: '「点真画出来了」对没挂效果的样本真红（这正是「静默没生效」的画面）',
});
for (const s of SELF) {
  let px = s.px, w = gotA?.w, h = gotA?.h, meta = s.meta;
  if (!px) {
    const got = await render(s.name, s.code);
    if (!got) continue;
    px = got.px; w = got.w; h = got.h; meta = {bg: got.bg};
  }
  const r = s.probe(px, w, h, meta);
  console.log(`[${s.name}]`);
  for (const [, msg] of r) console.log(`   · 判据读到：${msg}`);
  say(s.judge(r), s.label);
  console.log('');
}

fs.rmSync(WORK, {recursive: true, force: true});
console.log(fail === 0
  ? '\nALL PASSED —— 三条判据 + 两条自检全过，**这是进白名单的前提**（还要同步四处名单）'
  : `\n${fail} FAILED —— **不进白名单**，永久停在 §3.1 的「先验后采」`);
process.exitCode = fail === 0 ? 0 : 1;
