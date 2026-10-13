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

console.log(fail === 0 ? '\nALL PASSED' : `\n${fail} FAILED`);
process.exitCode = fail === 0 ? 0 : 1;
