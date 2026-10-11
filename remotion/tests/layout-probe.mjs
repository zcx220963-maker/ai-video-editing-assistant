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
const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';

const HEAD = `import React from 'react';
import { FilmLayout, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';

const Box = ({ x, y, w, h, children }) => (
  <div style={{ position: 'absolute', left: x, top: y, width: w, height: h,
                color: TOKENS.ink, fontSize: 40, background: 'rgba(255,255,255,0.82)' }}>
    {children}
  </div>
);
`;

/** 把一镜的画布片段包成完整镜头代码。 */
const shot = (inner) => `${HEAD}
const Shot = ({ text = '' }) => {
  const body = useBody();
  return (
    <FilmLayout text={text} aspect="portrait">
      <div style={{ position: 'relative', width: '100%', height: '100%' }}>
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

console.log(fail === 0 ? '\nALL PASSED' : `\n${fail} FAILED`);
process.exitCode = fail === 0 ? 0 : 1;
