// 闸⑧ 版面回读的探针夹具：把三条判据各钉一次「必须响」，再各钉一次「必须不响」。
//
// 为什么这些夹具是判据的一部分而不是演示：`tokens.ts` 里 `overlap_ratio = 0.4` 那条注释
// 写的就是「这个数是判据选型，不是量出来的，由两支夹具钉住」。没有这两支，0.4 只是
// 一句好看的话；改阈值的人也不会知道自己放宽了什么。
//
// 用法（容器内，代码已烤进镜像）：
//     docker exec creation-remotion sh -c 'cd /srv && node tests/layout-probe.mjs'
// 用法（宿主，服务已发布 8010）：
//     node tests/layout-probe.mjs
//
// 每一支都真渲一镜（几秒到几十秒），因为判据读的是**浏览器里的 DOM**：JSDOM 里
// getBoundingClientRect 全是 0，量不出「压在一起」这件事。
//
// 最后一段（R7-T22）判的是另一件事：母版那层整片进出场**不许改动**上面这些读数。
// 它是 `toCodeSpace` 那一步的存在理由——没有那一步，一个「整片轻拉远」的片子会让
// ⑧ 把好版式报成「被裁掉」。那一支的绿是**相等**（加不加出场，读数落在同一处），
// 不是「没报错」。
const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';

// 这份夹具**自己也要过闸①**：`/render-shot` 口内部会再 lint 一遍（server.mjs），
// 长得不像一镜的话，探针在「版面判据」判之前就被打回来了——于是红的是「探针坏了」，
// 与「版面判据坏了」混成一个红。两条规矩（与 `effects-probe.mjs` 那几个夹具同规）：
//   · `dg/must-use-motion`：`@dg/motion` 的工具要 import **且真用上**（这里用 `useT`）；
//   · `dg/no-raw-styled-div`：`<div style={{background:'#…'}}>` 那种**纯色字面量**不许用，
//     需要一块底板时写成**渐变**（规则明文放行 gradient）。
const HEAD = `import React from 'react';
import { FilmLayout, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { useT } from '@dg/motion';

const Box = ({ x, y, w, h, children }) => (
  <div style={{ position: 'absolute', left: x, top: y, width: w, height: h,
                color: TOKENS.ink, fontSize: 40,
                background: 'linear-gradient(180deg, rgba(255,255,255,0.82), rgba(255,255,255,0.82))' }}>
    {children}
  </div>
);
`;

/** 把一镜的画布片段包成完整镜头代码。 */
const shot = (inner) => `${HEAD}
const Shot = ({ text = '' }) => {
  const body = useBody();
  // 闸① 的 must-use-motion：import 了 @dg/motion 的工具就得真用上。这一支判的是版面，
  // 不拍动效，于是把 t 摊在一个**不参与版面**的属性上（探针读的是盒的几何与文字，
  // 不是它）——既有真引用，也不给读数添一个变量。
  const t = useT();
  return (
    <FilmLayout text={text} aspect="portrait">
      <div style={{ position: 'relative', width: '100%', height: '100%' }}
           data-t={t.toFixed(3)}>
${inner}
      </div>
    </FilmLayout>
  );
};
export default Shot;
`;

// 带内坐标系：竖屏 1080×1920 → 画面带 936×1524（母版与镜头代码同用 filmBody()）。
// 下面每个数字都按这个系写，findings 的 box/at 也报这个系（FilmLayout 的 toBand）。
const CASES = [
  {
    name: '叠字（必须报）',
    // 两盒各 300×120，横向交 150 → 交集 18000 / 较小盒 36000 = 0.5 ≥ 0.4
    code: shot(`        <Box x={100} y={200} w={300} h={120}>中子星</Box>
        <Box x={250} y={200} w={300} h={120}>一茶匙</Box>`),
    expect: { kinds: ['overlap'], ratioMin: 0.45, ratioMax: 0.55 },
  },
  {
    name: '轻微交叠 0.2（必须不响：钉住 0.4 这条线）',
    // 交 60px → 6000 / 36000 ≈ 0.167；两盒仍都算「文字」，扫描条数得非零
    code: shot(`        <Box x={100} y={200} w={300} h={120}>左上标签</Box>
        <Box x={340} y={200} w={300} h={120}>右下标签</Box>`),
    expect: { kinds: [], minTexts: 2 },
  },
  {
    name: '排开 + 引线接好（必须不响）',
    // 标签 → 色块：一端挨文字、一端挨图元，两半都「有落点」，不是悬空；
    // 第二个标签离一切都远，用来钉住「两处文字各排各的」不该报叠字。
    code: shot(`        <Box x={620} y={480} w={200} h={90}>标签甲</Box>
        <Box x={100} y={1100} w={200} h={90}>标签乙</Box>
        <div style={{ position: 'absolute', left: 300, top: 800, width: 240,
                      height: 180, background: TOKENS.accent }} />
        <svg width={body.w} height={body.h} style={{ position: 'absolute', left: 0, top: 0,
                                                     overflow: 'visible' }}>
          <line x1={620} y1={570} x2={500} y2={800} stroke={TOKENS.ink} strokeWidth={3} />
        </svg>`),
    expect: { kinds: [], minTexts: 2, minLeaders: 1, minDrawn: 1 },
  },
  {
    name: '引线悬空（必须报 dangling）',
    // 起端在标签右下角（挨文字），末端 (800,1300) 半径 20px 内什么都没有
    code: shot(`        <Box x={100} y={200} w={240} h={90}>悬空标签</Box>
        <svg width={body.w} height={body.h} style={{ position: 'absolute', left: 0, top: 0,
                                                     overflow: 'visible' }}>
          <line x1={200} y1={290} x2={800} y2={1300} stroke={TOKENS.ink} strokeWidth={3} />
        </svg>`),
    expect: { kinds: ['dangling'], minLeaders: 1 },
  },
  {
    name: '引线藏在 <g transform> 里（必须照样报：钉住 getScreenCTM 映射）',
    // 同一条线写进被 translate/scale 过的组里：属性坐标是组的 user space，
    // 端点必须经 CTM 换算后才与文字盒同屏可比。不映射的话这里会「看起来没悬空」。
    code: shot(`        <Box x={100} y={200} w={240} h={90}>变换组标签</Box>
        <svg width={body.w} height={body.h} style={{ position: 'absolute', left: 0, top: 0,
                                                     overflow: 'visible' }}>
          <g transform="translate(40, 60) scale(0.8)">
            <line x1={200} y1={290} x2={950} y2={1550} stroke={TOKENS.ink} strokeWidth={4} />
          </g>
        </svg>`),
    expect: { kinds: ['dangling'] },
  },
  {
    name: '标签越过画面带右缘（必须报 clipped right）',
    // 带内 x=760、宽 300 → 右缘 1060 > 936，越界 124px > clip_px 3
    code: shot(`        <Box x={760} y={300} w={300} h={90}>被切掉的长标签文字</Box>`),
    expect: { kinds: ['clipped'], side: 'right', pxMin: 100, pxMax: 140 },
  },
  // ---- R8-T30：⑲-a 的两个读数必须是真的（判据在 Python，这里只验读数）----
  {
    name: '主体件铺开（⑲-a 读数必须大）',
    // 一个半径 300 的圆落在带中央：并集外接盒 600×600 = 360000，带 936×1524 = 1426464
    // → ≈0.25，过 0.18 那条线。判的是「母版真把 SVG 形状量进去了」。
    code: shot(`        <svg width={body.w} height={body.h}
                             style={{ position: 'absolute', left: 0, top: 0 }}>
          <circle cx={468} cy={762} r={300} fill="none" stroke={TOKENS.ink} strokeWidth={6} />
        </svg>`),
    expect: { kinds: [], subjectMin: 0.18 },
  },
  {
    name: '卡片+文字（⑲-a 读数必须小：div 不算主体）',
    // 一整块渐变底板 + 四处文字，**一个 SVG 形状都没有**。读数必须是小的那一档——
    // 把涂了底色的 div 算进主体，这一支就会报出接近 1 的占比，「卡片+文字」从此绿得非常稳。
    code: shot(`        <div style={{ position: 'absolute', left: 60, top: 60, width: 816,
                      height: 1404,
                      background: 'linear-gradient(180deg, rgba(20,20,24,0.9), rgba(20,20,24,0.9))' }} />
        <Box x={140} y={140} w={640} h={140}>第一步：先说清结论</Box>
        <Box x={140} y={520} w={640} h={140}>第二步：再给一个数</Box>
        <Box x={140} y={900} w={640} h={140}>第三步：最后收一句</Box>
        <Box x={140} y={1280} w={640} h={150}>这几行字就是全部画面</Box>`),
    // 两个读数要落在**被判据拒掉的那一格**上：主体 0（<0.18）且文字 >0.45。
    // 构造得刚刚好只说明夹具会写数，落在那一格才说明这道闸真有东西可判。
    expect: { kinds: [], minTexts: 4, subjectMax: 0.17, textMin: 0.45 },
  },
];

const post = async (p, payload) => {
  const r = await fetch(`${BASE}${p}`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { status: r.status, json: await r.json() };
};

let fail = 0;
const say = (ok, msg) => { if (!ok) fail++; console.log(`${ok ? '  ok ' : '  XX '} ${msg}`); };

console.log('闸⑧ 版面回读探针（每支真渲一镜）');
console.log('==============================\n');

for (const c of CASES) {
  const t0 = Date.now();
  const res = await post('/render-shot', {
    code: c.code,
    cache_key: `layoutprobe-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    declared_fields: [],
    input_props: { fps: 30, durationInFrames: 30, aspect: 'portrait', text: c.name },
  });
  const j = res.json;
  console.log(`[${c.name}] 用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  if (!j.ok) {
    say(false, `渲染就失败了：${String(j.error).slice(0, 200)} ${JSON.stringify(j.errors || '')}`);
    continue;
  }
  const lay = j.layout_used;
  if (!lay) {
    say(false, '响应里没有 layout_used——母版那侧的探针没跑起来（镜像里的母版是旧的？）');
    continue;
  }
  if (!lay.scanned) {
    say(false, `版面没扫：${lay.reason}`);
    continue;
  }
  const kinds = (lay.findings || []).map((f) => f.kind);
  say(JSON.stringify(lay.thresholds) === JSON.stringify({
    overlap_ratio: 0.4, min_box_px: 6, clip_px: 3, anchor_ratio: 0.02,
    anchor_min_px: 20, leader_min_px: 40, max_findings: 8,
  }), `判据阈值随读数一起回传且与 tokens.ts 的 LAYOUT_PROBE 同值（实得 ${JSON.stringify(lay.thresholds)}）`);

  const want = c.expect.kinds;
  if (want.length === 0) {
    say(kinds.length === 0, `不该有 finding，实得 ${JSON.stringify(lay.findings)}`);
  } else {
    for (const k of want) {
      say(kinds.includes(k), `finding 里有 ${k}（实得 ${JSON.stringify(lay.findings)}）`);
    }
  }
  if (c.expect.ratioMin != null) {
    const r = (lay.findings.find((f) => f.kind === 'overlap') || {}).ratio ?? -1;
    say(r >= c.expect.ratioMin && r <= c.expect.ratioMax,
        `overlap 的交叠比例落在 ${c.expect.ratioMin}~${c.expect.ratioMax}（实得 ${r}）`);
  }
  if (c.expect.side) {
    const f = lay.findings.find((x) => x.kind === 'clipped') || {};
    say(f.side === c.expect.side, `clipped 报的是 ${c.expect.side} 缘（实得 ${f.side}）`);
    if (c.expect.pxMin != null) {
      say(f.px >= c.expect.pxMin && f.px <= c.expect.pxMax,
          `越界像素在 ${c.expect.pxMin}~${c.expect.pxMax} 之间（实得 ${f.px}）`);
    }
  }
  if (c.expect.minTexts != null) {
    say(lay.scanned.texts >= c.expect.minTexts,
        `扫到 ≥${c.expect.minTexts} 处文字（实得 ${lay.scanned.texts}）——零处等于「没扫到东西」而不是「干净」`);
  }
  if (c.expect.minLeaders != null) {
    say(lay.scanned.leaders >= c.expect.minLeaders,
        `扫到 ≥${c.expect.minLeaders} 条引线（实得 ${lay.scanned.leaders}）`);
  }
  if (c.expect.minDrawn != null) {
    say(lay.scanned.drawn >= c.expect.minDrawn,
        `扫到 ≥${c.expect.minDrawn} 个画得出的图元（实得 ${lay.scanned.drawn}）`);
  }
  // 闸⑲-a / ⑳(a) 的两个读数（R8-T30）：这里钉的是**读数本身是真的**——
  // 判据在 Python（阈值在 code_spec），母版这一侧只负责量。读数为 undefined
  // 就是「母版没带这一栏」，与「量出来是 0」必须分开报，所以下面两句都用
  // `Number.isFinite` 而不是 `!= null`。
  if (c.expect.subjectMin != null) {
    const s = lay.scanned.subject_share;
    say(Number.isFinite(s) && s >= c.expect.subjectMin,
        `主体件占带 ≥${c.expect.subjectMin}（实得 ${s}，图元数 ${lay.scanned.subjects}）`);
  }
  if (c.expect.subjectMax != null) {
    const s = lay.scanned.subject_share;
    say(Number.isFinite(s) && s <= c.expect.subjectMax,
        `主体件占带 ≤${c.expect.subjectMax}（实得 ${s}，图元数 ${lay.scanned.subjects}）`);
  }
  if (c.expect.textMin != null) {
    const t = lay.scanned.text_share;
    say(Number.isFinite(t) && t >= c.expect.textMin,
        `文字占带 ≥${c.expect.textMin}（实得 ${t}）`);
  }
  // 悬空那条必须落在**带内坐标**：末端 (800,1300) 与变换组那条 (≈800,≈1300)
  const d = (lay.findings || []).find((f) => f.kind === 'dangling');
  if (d) {
    say(Number.isFinite(d.at?.x) && d.at.x >= 0 && d.at.y >= 0 &&
        d.at.x <= lay.scanned.band.w && d.at.y <= lay.scanned.band.h,
        `dangling 的空端报的是带内坐标且落在带子里（实得 ${JSON.stringify(d.at)}，` +
        `带 ${JSON.stringify(lay.scanned.band)}）`);
  }
  // finding 里的盒也必须是带内坐标（与 at 同一个系）
  const o = (lay.findings || []).find((f) => f.kind === 'overlap');
  if (o) {
    say(o.box && o.box.x < lay.scanned.band.w,
        `overlap 的盒是带内坐标（实得 ${JSON.stringify(o.box)}）`);
  }
  console.log('');
}

// ---------------------------------------------------------------------------
// R7-T22 整片进出场不许改动版面读数
//
// 母版给每一镜套的那层进出场是**整帧**的 transform，而 getBoundingClientRect() 量的是
// 变换**之后**的屏幕盒——不折回去，一个「整片轻拉远」的片子会让闸⑧ 报一片「文字被画面带
// 裁掉」（其实是被那层变换推出去了）、闸⑪ 把一张静态图判成「参与动效」。所以母版把每个盒
// 按当帧实测的画面带折回成片像素（FilmLayout 的 toCodeSpace）。这支夹具钉的就是那一步：
// **同一段代码，加不加出场，findings 必须落在同一处**。
//
// 为什么只做出场：探针帧在片尾（durationInFrames-2），能扰到它的只有出场那一档。
// 为什么挑 zoom 而不是 fade：fade 只动透明度、不动几何，不折回去也看不出差别——
// 而 zoom 是缩放，屏幕上每个盒的位置与尺寸都在变，不折回去必然对不上。
// ⑪（资产动效）走的是同一个 convert，但它要一件真资产才量得出盒；那一半留给真机 E2E。
console.log('T22 整片出场不许改动版面读数');
console.log('------------------------------');
{
  const DUR = 60;           // 出场窗口 = min(60×10%, 15) = 6 帧 → [54, 60)，探针帧 58 在动作里
  const CODE = shot(`        <Box x={100} y={200} w={300} h={120}>中子星</Box>
        <Box x={250} y={200} w={300} h={120}>一茶匙</Box>`);
  const shoot = async (props) => {
    const r = await post('/render-shot', {
      code: CODE,
      cache_key: `layoutprobe-t22-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      declared_fields: [],
      input_props: { fps: 30, durationInFrames: DUR, aspect: 'portrait',
                     text: 'T22 出场', ...props },
    });
    if (!r.json?.ok || !r.json?.layout_used) {
      say(false, `渲染/回读没成功：HTTP ${r.status} ${String(r.json?.error).slice(0, 160)}`);
      return null;
    }
    return r.json.layout_used;
  };
  const still = await shoot({});
  const moved = await shoot({ exit: 'zoom', exitFrames: 6 });
  if (still && moved) {
    say((still.findings || []).length > 0 && still.scanned?.texts >= 2,
        `基线这一支真的量到了东西（${(still.findings || []).length} 条 finding、` +
        `${still.scanned?.texts} 处文字）——零条的话下面那条相等是废话`);
    const kinds = (l) => JSON.stringify((l.findings || []).map((f) => f.kind));
    say(kinds(still) === kinds(moved),
        `出场前后的 finding 种类一致（静止 ${kinds(still)} / 出场 ${kinds(moved)}）`);
    const drop = (l) => (l.findings || []).map(
      (f) => `${f.kind}@${JSON.stringify(f.box || f.at)}:${f.px ?? f.ratio ?? ''}`).join('|');
    const coords = (l) => (l.findings || []).flatMap(
      (f) => [f.box, f.at].filter(Boolean).flatMap((b) => [b.x, b.y, b.w, b.h]));
    const a = coords(still);
    const b = coords(moved);
    const worst = a.length === b.length
      ? Math.max(0, ...a.map((v, i) => Math.abs(v - b[i])))
      : Infinity;
    say(worst <= 0.5,
        `出场那一帧的盒折回成片像素后与静止时逐值对齐（最大差 ${worst}px，` +
        `容差 0.5px）\n        静止 ${drop(still)}\n        出场 ${drop(moved)}`);
    say(JSON.stringify(still.scanned?.band) === JSON.stringify(moved.scanned?.band),
        `报出去的带子尺寸还是**静止**那一份（${JSON.stringify(moved.scanned?.band)}）——` +
        '判据的容差全按它算，缩放过的带子会让每一条阈值都跟着漂');
    say(JSON.stringify(still.thresholds) === JSON.stringify(moved.thresholds),
        '阈值不随出场动作变（判据是判据，画面在动不该改判据）');
  }
  console.log('');
}

// ---------------------------------------------------------------------------
// R8-T32 画面指纹（闸⑳(c) 的真机取证）
//
// 闸⑳(c) 判的是「两镜**代码不同、画面上却是同一张卡**」。判据在 Python（阈值、配对、
// 措辞都在 `code_spec.check_degenerate_forms`），这里只钉**母版量出来的那三个读数**：
//
//   ① 「同一张卡换个写法」必须量出**同一个**指纹。这是这一整道闸的立足点——指纹要是
//      跟着写法走，它判的就成了「代码像不像」，而闸⑨ 已经在判那个了（⑨ 比骨架、还要
//      ≥3 镜才定罪；⑳(c) 比画面、两镜即算，两个问题的答案不一样）。
//   ② 「不同的画面」必须量出**不同的**指纹。少了这一句，一个恒定的指纹也能让 ① 全绿。
//   ③ 三支都要量得到（`Number.isFinite` / 非空），否则「没量到」会被读成「没重复」——
//      而颜色那一栏尤其要注意：这张丑卡除了文字色之外一个声明的颜色都没有，母版要是
//      只收 background/fill/stroke，这一栏就是空的，而空栏在 Python 侧落**未判**，
//      这条闸就恰好在它该响的地方闭嘴。
// 代码必须真的不一样（下面有一条断言钉住这件事）——「代码不同、画面同一张」这句话，
// 两半都得是真的。
console.log('R8-T32 画面指纹（⑳(c)：代码不同、画面同一张）');
console.log('------------------------------------------');
{
  const shoot = async (code, tag) => {
    const r = await post('/render-shot', {
      code,
      cache_key: `layoutprobe-t32-${tag}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      declared_fields: [],
      input_props: { fps: 30, durationInFrames: 30, aspect: 'portrait', text: 'T32' },
    });
    if (!r.json?.ok || !r.json?.layout_used?.scanned) {
      say(false, `${tag}：渲染/回读没成功 HTTP ${r.status} `
        + `${String(r.json?.error || r.json?.layout_used?.reason).slice(0, 160)}`);
      return null;
    }
    return r.json.layout_used.scanned;
  };

  // 甲：那张丑卡（一块渐变底板 + 四行字，**一个 SVG 形状都没有**）。
  const CARD_A = shot(`        <div style={{ position: 'absolute', left: 60, top: 60, width: 816,
                      height: 1404,
                      background: 'linear-gradient(180deg, rgba(20,20,24,0.9), rgba(20,20,24,0.9))' }} />
        <Box x={140} y={140} w={640} h={140}>第一步：先说清结论</Box>
        <Box x={140} y={520} w={640} h={140}>第二步：再给一个数</Box>
        <Box x={140} y={900} w={640} h={140}>第三步：最后收一句</Box>
        <Box x={140} y={1280} w={640} h={150}>这几行字就是全部画面</Box>`);
  // 甲′：**同一张卡、另一份代码**——多一层包壳、坐标各偏 4px、元素换了顺序、字号写法也不同。
  // 偏 4px 仍然落在同一格里（格 156×254），这正是「用粗网格而不是精确坐标」那句话的取证。
  const CARD_A2 = shot(`        <div style={{ position: 'absolute', left: 0, top: 0, width: '100%',
                      height: '100%' }}>
          <div style={{ position: 'absolute', left: 64, top: 64, width: 808, height: 1396,
                        background: 'linear-gradient(180deg, rgba(20,20,24,0.9), rgba(20,20,24,0.9))' }} />
          <Box x={144} y={1284} w={640} h={150}>这几行字就是全部画面</Box>
          <Box x={144} y={904} w={640} h={140}>第三步：最后收一句</Box>
          <Box x={144} y={524} w={640} h={140}>第二步：再给一个数</Box>
          <Box x={144} y={144} w={640} h={140}>第一步：先说清结论</Box>
        </div>`);
  // 乙：**另一个画面**——两块底板横着摆、字在左边。差得足够远，指纹必须分得开。
  const CARD_B = shot(`        <svg width={body.w} height={body.h}
                             style={{ position: 'absolute', left: 0, top: 0 }}>
          <circle cx={468} cy={762} r={300} fill="none" stroke={TOKENS.ink} strokeWidth={6} />
        </svg>
        <Box x={100} y={180} w={420} h={110}>上半句结论</Box>
        <Box x={100} y={1600} w={420} h={110}>下半句收尾</Box>`);

  const a = await shoot(CARD_A, 'a');
  const a2 = await shoot(CARD_A2, 'a2');
  const b = await shoot(CARD_B, 'b');
  if (a && a2 && b) {
    say(CARD_A !== CARD_A2,
        '甲与甲′的**代码文本确实不同**（否则「代码不同、画面同一张」这半句是空话）');
    const ok3 = (s) => Number.isFinite(s.grid_text) && Number.isFinite(s.grid_drawn)
      && Array.isArray(s.colors);
    say(ok3(a) && ok3(a2) && ok3(b),
        `三支都量到了画面指纹（甲 0x${(a.grid_text >>> 0).toString(16)}/`
        + `0x${(a.grid_drawn >>> 0).toString(16)}、甲′ 0x${(a2.grid_text >>> 0).toString(16)}/`
        + `0x${(a2.grid_drawn >>> 0).toString(16)}、乙 0x${(b.grid_text >>> 0).toString(16)}/`
        + `0x${(b.grid_drawn >>> 0).toString(16)}）`);
    const setEq = (x, y) => JSON.stringify([...x].sort()) === JSON.stringify([...y].sort());
    say(a.grid_text !== 0 && a.grid_drawn === 0,
        `甲这一支的文字有格子、图元一个都没有（文字 0x${(a.grid_text >>> 0).toString(16)}、`
        + `图元 0x${(a.grid_drawn >>> 0).toString(16)}）——那块底板是 div，**不算图元**，`
        + `这正是「卡片+文字」的样子`);
    say(a.colors.length > 0,
        `甲的主色量得出来（${JSON.stringify(a.colors)}）——那张卡除了文字色没有别的声明颜色，`
        + `母版要是不收 color，这一栏就是空的，而空栏在 Python 侧落**未判**`);
    say(a.grid_text === a2.grid_text && a.grid_drawn === a2.grid_drawn
      && setEq(a.colors, a2.colors),
        '甲与甲′（同一张卡、另一份代码、坐标各偏 4px、元素次序不同）指纹**完全相同**——'
        + '所以这条判的是画面而不是写法');
    say(a.grid_text !== b.grid_text || a.grid_drawn !== b.grid_drawn
      || !setEq(a.colors, b.colors),
        '甲与乙（另一个画面）指纹**分得开**——否则一个恒定的指纹也能让上面那条全绿');
  }
  console.log('');
}

// ---------------------------------------------------------------------------
// R11 ㉕ 徽章构图（T50 那一卷）——母版必须报出**逐件**主体盒
//
// ㉕ 要判的形状是「一枚居中、近方形的单件撑满画面」：把一件素材放大铺满，屏幕上出现的
// 是一枚徽章或一块色斑，不是「一个场景」。**判据（四条联合、三个阈值）在 Python**
// （`code_spec.check_badge_composition`），这一份夹具只钉母版那侧的读数——理由与 ⑲-a
// 那两支同规：读数为 `undefined` 是「母版没带这一栏」（Python 侧落**未判**），与
// 「量出来是 0」是两件事，夹具混了这两件事，闸就会在它该响的地方静默。
//
// 为什么要**逐件**盒而不是上面已经有的 `subjects`/`subject_share`：后者只有个数与并集
// 外接盒的占比，「一枚圆形徽章铺满画面」与「两件部件左上/右下」在这两栏里长得很像
// （个数不同而已，占比都能很大），只有拿到件本身的盒（宽高比、离带中心多远）才分得开。
//
// 下面 `expect` 里的 0.22 / 0.08 / 1.35 是**夹具期望值**（同 `subjectMin: 0.18` 那条的
// 写法），判据仍在 Python 一处；这里出现它们是为了让「改阈值的人能看见自己放宽了什么」。
console.log('R11 ㉕ 徽章构图（母版必须报出逐件盒）');
console.log('----------------------------------');
{
  const shoot = async (code, tag) => {
    const r = await post('/render-shot', {
      code,
      cache_key: `layoutprobe-r11-${tag}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      declared_fields: [],
      input_props: { fps: 30, durationInFrames: 30, aspect: 'portrait', text: 'R11 ㉕' },
    });
    if (!r.json?.ok || !r.json?.layout_used?.scanned) {
      say(false, `${tag}：渲染/回读没成功 HTTP ${r.status} `
        + `${String(r.json?.error || r.json?.layout_used?.reason).slice(0, 160)}`);
      return null;
    }
    return r.json.layout_used.scanned;
  };

  // ① 一枚徽章：**单件**、摆带正中、近方形、铺到带的四分之一（圆 r=300 → 600×600，
  //    600×600/(936×1524) ≈ 0.2524，过 0.22 那条线）。这四个读数**同时**成立时，
  //    Python 侧必须打回；少任何一个，就是合法构图。
  const BADGE = shot(`        <svg width={body.w} height={body.h}
                             style={{ position: 'absolute', left: 0, top: 0 }}>
          <circle cx={468} cy={762} r={300} fill="none" stroke={TOKENS.ink} strokeWidth={6} />
        </svg>`);
  // ② 两件部件、左上/右下错开、各自都不方：这是「拼出来的场景」那一档，㉕ 必须放行。
  //    它的存在意义是钉住 ①「不是只要画面上有图形就报」——错的形状得量成错的读数。
  const PARTS = shot(`        <svg width={body.w} height={body.h}
                             style={{ position: 'absolute', left: 0, top: 0 }}>
          <rect x={120} y={180} width={260} height={200} fill="none"
                stroke={TOKENS.ink} strokeWidth={6} />
          <circle cx={640} cy={1120} r={130} fill="none" stroke={TOKENS.accent}
                  strokeWidth={6} />
        </svg>`);

  const badge = await shoot(BADGE, 'badge');
  const parts = await shoot(PARTS, 'parts');
  if (badge && parts) {
    const isBox = (b) => b && ['x', 'y', 'w', 'h'].every(
      (k) => Number.isInteger(b[k]));
    // 「逐件盒」这一栏：缺它（`undefined`）＝母版旧，Python 侧落未判；有它但空数组
    // 才是「这一镜画面上没有主体件」——两者不是一件事，所以分开报。
    say(Array.isArray(badge.subject_boxes) && Array.isArray(parts.subject_boxes),
        `两支都带回了 \`subject_boxes\` 这一栏（徽章 ${JSON.stringify(badge.subject_boxes)}、`
        + `两件 ${JSON.stringify(parts.subject_boxes)}）——缺这栏即「母版旧」，`
        + `Python 侧落**未判**而不是「没问题」`);
    if (Array.isArray(badge.subject_boxes) && Array.isArray(parts.subject_boxes)) {
      say(badge.subject_boxes.every(isBox) && parts.subject_boxes.every(isBox),
          '逐件盒报的是**带内整数像素**（与 findings 的 box/at 同一个系、同一个取整口径）');
      const bw = badge.band?.w, bh = badge.band?.h;
      const bx = badge.subject_boxes[0] || {};
      say(Number.isFinite(bw) && bw > 0 && Number.isFinite(bh) && bh > 0
        && badge.subject_boxes.every((b) => b.x >= 0 && b.y >= 0 && b.x + b.w <= bw + 1
          && b.y + b.h <= bh + 1),
          `盒落在带子里（带 ${JSON.stringify(badge.band)}，徽章盒 ${JSON.stringify(badge.subject_boxes[0])}）`);
      // ① 的四个读数必须**同时**落在徽章那一格——这是 Python 打回它的输入。
      const cdx = Math.abs(bx.x + bx.w / 2 - bw / 2) / bw;
      const cdy = Math.abs(bx.y + bx.h / 2 - bh / 2) / bh;
      const aspect = Math.max(bx.w, bx.h) / Math.min(bx.w, bx.h);
      say(badge.subjects === 1 && badge.subject_boxes.length === 1,
          `徽章这一支量出**单件**（subjects=${badge.subjects}，`
          + `逐件盒 ${badge.subject_boxes.length} 件）`);
      say(Number.isFinite(badge.subject_share) && badge.subject_share >= 0.22,
          `徽章这一支铺开 ≥0.22（实得 ${badge.subject_share}）`);
      say(cdx <= 0.08 && cdy <= 0.08,
          `徽章这一支摆在带正中（横偏 ${(cdx * 100).toFixed(1)}%、纵偏 `
          + `${(cdy * 100).toFixed(1)}%，容差 8%）`);
      say(aspect <= 1.35,
          `徽章这一支近似方形（长边/短边 ${aspect.toFixed(2)}，容差 1.35，`
          + `盒 ${bx.w}×${bx.h}）——这四条同时成立，Python 侧才打回`);
      // ② 的「单件」那条**不成立**：㉕ 的判据是四条联合，这一条挂了就整条放行。
      say(parts.subjects === 2 && parts.subject_boxes.length === 2,
          `两件那一支量出 2 件（subjects=${parts.subjects}，`
          + `盒 ${JSON.stringify(parts.subject_boxes)}）——「单件」不成立，㉕ 因此放行`);
      say(!(parts.subject_boxes.length === 1),
          '两件那一支**没有**被量成一件（逐件盒是逐元素量的，不是并集外接盒——'
          + '并集那一栏是 `subject_share`，判徽章用不上它）');
    }
  }
  console.log('');
}

// ---------------------------------------------------------------------------
// R12-T60 分区揭示（母版侧）：逐帧算出「这一块此刻允许被看见的那部分」
//
// 判据在 Python 侧（㉗ 拿这几帧的账去抽帧比色），这一支钉的是**母版的读数**与
// **选型本身**。它必须存在，理由是 T60 原文里那句前置条件：
//
//   「先用一支真渲夹具证明 `clip-path`/`mask-image` 在 headless Chrome 里逐帧稳定，
//     证不出就不许写进合同当保证」
//
// 我们选了 `clip-path: path()`（`mask-image` + `mask-composite` 在各版本 headless
// Chrome 上行为不一，见 `FilmLayout.clipPathOf` 上面那段），而且**没有**按原文写
// 「两层 mask 相减」：相减是在 JS 几何里做的（`subtractRects`），落到浏览器只有一条
// 路径。这条选型是整族风格的地基——它不稳，㉗ 那二十多 KB 的比色器判的就是一份不存在
// 的几何。所以这一支真渲一镜，量五件事：没申报时一行都不发 · 只报点名帧 · 未到点的块
// 一个像素都不许有 · 扣掉后续块之后不越界 · 同一份申报渲两遍掩码逐字相等。
//
// 坐标系：夹具刻意让**插画原图尺寸 = 画面带尺寸**（canvas 936×1524），于是母版那次
// 「画布像素 → 成片像素」的换算是 sx=sy=1——断言里写的矩形就是申报里的区域矩形本身。
// 那次映射对不对不归这一支判（同一行代码在 ㉗ 的真帧比色里另有一支夹）。
console.log('R12-T60 分区揭示（母版逐帧允许掩码）');
console.log('----------------------------------');
{
  // 一张**平铺满带**的插画：母版只对 `<img>` 套掩码（这一族的插画就是它），所以夹具
  // 不能拿 svg 形状代替——用 `data:` 而不是 `staticFile`：这一趟判的是几何，
  // 投递腿另有整套判据，绑在一起会让红指向错的地方（与 `effects-probe` 同规）。
  const PIC = 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="936" height="1524">'
    + '<rect width="936" height="1524" fill="#F5EBD7"/>'
    + '<circle cx="320" cy="360" r="150" fill="none" stroke="#3A3A3A" stroke-width="10"/>'
    + '<rect x="520" y="1080" width="300" height="260" fill="none" stroke="#3A3A3A"'
    + ' stroke-width="10"/></svg>');

  // 两块、上下不重叠、各 1 s：这一份申报的合法性由 ㉘ 判（Python 侧），这里只借它当输入。
  const REVEAL = {
    canvas: { w: 936, h: 1524 },
    scene_duration_ms: 2000,
    regions: [
      { id: 'block-a', sequence: 1, region: { x: 0, y: 0, w: 936, h: 762 },
        direction: 'ltr', start_ms: 0, duration_ms: 1000 },
      { id: 'block-b', sequence: 2, region: { x: 0, y: 762, w: 936, h: 762 },
        direction: 'ltr', start_ms: 1000, duration_ms: 1000 },
    ],
  };
  // 三个点名帧（30 fps、60 帧 = 2000 ms，与 scene_duration_ms 对齐）：
  // 第 6 帧 = 200 ms（块 b **开始之前**，T60 红法那一支）· 第 30 帧 = 1000 ms（块 a 到点）·
  // 第 45 帧 = 1500 ms（块 b 画到一半）。
  const PROBE = [6, 30, 45];

  const REVEAL_SHOT = shot(`        <img src={${JSON.stringify(PIC)}}
          style={{ position: 'absolute', left: 0, top: 0, width: body.w, height: body.h }} />`);

  const render = async (tag, extra) => post('/render-shot', {
    code: REVEAL_SHOT,
    cache_key: `layoutprobe-r12-${tag}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    declared_fields: [],
    input_props: { fps: 30, durationInFrames: 60, aspect: 'portrait',
                   text: `R12 ${tag}`, ...extra },
  });

  const first = await render('reveal', { reveal: REVEAL, revealProbeFrames: PROBE });
  const j = first.json;
  if (!j?.ok) {
    say(false, `分区揭示那一支没渲成：HTTP ${first.status} `
      + `${String(j?.error || JSON.stringify(j?.errors)).slice(0, 200)}`);
  } else {
    const rev = j.reveal_used;
    say(rev !== null && typeof rev === 'object',
        '申报了这一族的镜，母版回了 `reveal_used` 账——没读到（null）的正确读法是'
        + '「通道没跑起来」，Python 侧落**未判**而不是「每一块都没问题」');
    if (rev) {
      const row = (f) => rev[String(f)];
      const at = (f, id) => (row(f)?.regions || []).find((r) => r.id === id);
      const area = (rs) => (rs || []).reduce(
        (s, r) => s + Math.max(0, r.w) * Math.max(0, r.h), 0);
      const frames = Object.keys(rev).map(Number).sort((a, b) => a - b);
      say(JSON.stringify(frames) === JSON.stringify(PROBE),
          `只在**点名的帧**出声（点名 ${JSON.stringify(PROBE)}，实得 ${JSON.stringify(frames)}）`
          + '——毎帧一行会把逐镜账撑成噪声，而 ㉗ 只需要那几个时刻');

      const bx = row(6)?.boxes?.[0];
      say(!!bx && !!bx.box && bx.box.w > 0 && bx.box.h > 0,
          `点名帧那一行带回了插画元素的**实况盒**（${JSON.stringify(bx?.box)}）——`
          + '元素还没量到尺寸时母版**不裁**（裁一个 0 宽的盒等于把整张图抹掉，'
          + '而 ㉗ 会把那一帧读成「未开始的区域是纸底」，一条假绿）');

      // ① T60 红法原样：块 b 开始之前，它一个像素都不许有。「空表」与「零面积」都算
      //    ——`revealMasks` 对没到点的块返空表，卡在 start_ms 那一帧返的是推进 0 的
      //    零宽矩形，两者都不是「看得见」。
      say(area(at(6, 'block-b')?.rects) === 0,
          `第 6 帧（块 b 开始之前）块 b 的允许掩码**零面积**`
          + `（实得 ${JSON.stringify(at(6, 'block-b')?.rects)}）——这一条不成立，`
          + '就是「整图第 0 帧就全显」，也就是这一族风格根本不存在');

      // ② 进度按**同一帧时钟**线性插值：第 6 帧 = 200 ms / 1000 ms = 0.2 → 已画宽 187.2。
      const a6 = at(6, 'block-a')?.rects || [];
      say(a6.length === 1 && a6[0].x === 0 && a6[0].y === 0 && a6[0].h === 762
        && Math.abs(a6[0].w - 187.2) < 0.5,
          `第 6 帧块 a 画到 0.2（\`ltr\` → 已画宽 187.2，实得 ${JSON.stringify(a6)}）`);

      // ③ 到点那一刻铺满自己的区域，而后一块仍是零面积——「画满了」与「还没开始」
      //    必须分得开，㉗ 的两句话就是按这两个时刻写的。
      const a30 = at(30, 'block-a')?.rects || [];
      say(a30.length === 1 && Math.abs(area(a30) - 936 * 762) < 1,
          `第 30 帧（1 s，块 a 的 duration 到点）块 a 铺满自己的区域`
          + `（实得 ${JSON.stringify(a30)}）`);
      say(area(at(30, 'block-b')?.rects) === 0,
          `同一帧块 b 仍是零面积（实得 ${JSON.stringify(at(30, 'block-b')?.rects)}）`);

      // ④ 扣掉后续块之后**不越界**（T60 验收口径里那句的机器形状）：每一片都落在它
      //    自己的区域里、且落在 canvas 内。区域横竖各跨半屏，扣完必须还是矩形并集。
      const within = (r, g) => r.x >= g.x - 0.01 && r.y >= g.y - 0.01
        && r.x + r.w <= g.x + g.w + 0.01 && r.y + r.h <= g.y + g.h + 0.01;
      const all = Object.values(rev).flatMap((v) => v.regions || []);
      const bad = all.filter((r) => {
        const g = REVEAL.regions.find((x) => x.id === r.id);
        return !g || (r.rects || []).some((p) => !within(p, g.region)
          || p.x < -0.01 || p.y < -0.01 || p.x + p.w > 936.01 || p.y + p.h > 1524.01);
      });
      say(all.length === PROBE.length * REVEAL.regions.length && bad.length === 0,
          `${PROBE.length} 帧 × ${REVEAL.regions.length} 块 = ${all.length} 条读数都在账上，`
          + `且每一片都落在自己的区域里、不越 canvas 边界（越界的 ${bad.length} 条）`);

      // ⑤ 「逐帧稳定」的可判形状：同一份申报渲两遍，掩码逐字相等。
      const again = await render('reveal-again', { reveal: REVEAL, revealProbeFrames: PROBE });
      const rev2 = again.json?.reveal_used;
      say(!!rev2 && JSON.stringify(rev2) === JSON.stringify(rev),
          '同一份申报渲两遍，逐帧掩码**逐字相等**（`clip-path: path()` 这条选型的稳定性）'
          + '——不等即「同一帧画不出同一份几何」，㉗ 的比色器就没有地基'
          + `（第二遍 ${JSON.stringify(rev2).slice(0, 140)}）`);

      const dup = (j.warnings || []).filter((w) => String(w).includes('分区揭示回读'));
      say(dup.length === 0,
          `没有「同一帧两次算出不同的允许掩码」的记账（${JSON.stringify(dup)}）`);
    }

    // ⑥ 没申报这一族的镜：**一行都不发**（`code_render` 那侧也只在真写了 `reveal`
    //    时才注入这两项，理由是不给母版一个「有没有这一栏」都分不出来的输入）。
    const plain = await render('no-reveal', {});
    say(plain.json?.reveal_used === null,
        `没申报的镜母版回的是 null（实得 ${JSON.stringify(plain.json?.reveal_used)}）`
        + '——null 与「掩码全是空表」是两件事，混了就等于把「这一镜没有分区揭示」'
        + '读成「每一块都没露出来」');
  }
  console.log('');
}

console.log(fail === 0 ? '\nALL PASSED' : `\n${fail} FAILED`);
process.exitCode = fail === 0 ? 0 : 1;
