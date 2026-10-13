// 闸⑭ 动效事件密度的探针夹具：把四条判据（地板线、滞回、合并、相邻间隔）
// 各钉一次「必须拒」，再钉一次「必须放」。
//
// 为什么这个文件是判据的一部分而不是演示：`motionsample.mjs` 里 `MOTION_DIFF_MIN = 1.5`
// 那条注释写的就是「这个数是判据选型，不是量出来的，由 tests/motion-probe.mjs 钉住」。
// 没有这几支，1.5 只是一句好看的话；改阈值的人不会知道自己放宽了什么。
//
// 两半：
//   A 半（离线、必跑）：**构造的差值曲线**过纯函数段。判据本体（分段规则）与 ffmpeg 无关，
//     所以它必须能在没有容器、没有浏览器、没有真片的地方被证红证绿。
//   B 半（真渲，要服务）：三支真镜头——「0 帧到位」的静帧镜、「三件齐发」的跳变镜
//     （这两支是判据的红样）、与「先后三件」的动效镜（绿样）——走 /render-shot，
//     读它回传的 `motion`。这一半跑不了时**记未判并列出**，不算通过。
//
// 用法（容器内）：docker exec creation-remotion sh -c 'cd /srv && node tests/motion-probe.mjs'
// 用法（宿主，服务已发布 8010）：DG_BASE=http://127.0.0.1:8010 node tests/motion-probe.mjs
import {
  motionEvents, judgeDensity, motionDensity, MOTION_SAMPLE_HZ, MOTION_DIFF_MIN,
} from '../motionsample.mjs';
import { STATIC_SHOT, SIMULTANEOUS_SHOT, SEQUENCED_SHOT } from './fixtures-motion.mjs';

const FPS = 30;
const HZ = MOTION_SAMPLE_HZ;
const unverified = [];
let fail = 0;
let n = 0;

const check = (cond, label) => {
  n++;
  console.log(`  ${cond ? '✓' : '✗'} ${label}`);
  if (!cond) fail++;
};

/** 按「第几格高、多高」拼一条差值曲线。 */
const curve = (len, peaks) => {
  const a = new Array(len).fill(0);
  for (const [i, v] of Object.entries(peaks)) a[Number(i)] = v;
  return a;
};

console.log('=== A 半：构造曲线过判据（离线）===');

// A1 静帧：从头到尾没有可测的变化——「所有元素 0 帧到位」那一支红样。
{
  const ev = motionEvents(curve(40, {}), { fps: FPS });
  const v = judgeDensity(ev, { fps: FPS });
  check(ev.length === 0, `静帧数出 0 段（实得 ${ev.length}）`);
  check(v.status === 'reject' && v.reason.includes('静帧'),
        `静帧必须拒，且理由说得出是静帧（实得 ${v.status}：${v.reason?.slice(0, 40)}）`);
}

// A2 三件齐发：三段变化挤在一起，像素上就是**一段**长的——必须拒。
{
  const peaks = {};
  for (let i = 8; i <= 12; i++) peaks[i] = 40;
  const ev = motionEvents(curve(40, peaks), { fps: FPS });
  const v = judgeDensity(ev, { fps: FPS });
  check(ev.length === 1, `齐发只数出 1 段（实得 ${ev.length}）`);
  check(v.status === 'reject' && v.reason.includes('跳变'),
        `齐发必须拒，理由指向「一次跳变」（实得 ${v.reason?.slice(0, 40)}）`);
}

// A3 先后三件：三段分开、间隔够——放。
{
  const peaks = { 4: 30, 5: 22, 16: 35, 17: 20, 28: 28, 29: 12 };
  const ev = motionEvents(curve(40, peaks), { fps: FPS });
  const v = judgeDensity(ev, { fps: FPS });
  check(ev.length === 3, `先后三件数出 3 段（实得 ${ev.length}）`);
  check(v.status === 'ok', `先后三件放行（实得 ${v.status}：${v.reason ?? ''}`);
}

// A4 挨太近的两段：各自都成立，但起点只隔 11 帧（< 0.4 s = 12 帧）——拒。
//    这一支钉的是「相邻 ≥0.4 s」那条判据真的会响，而不是被合并规则吃掉了。
{
  const ev = motionEvents(curve(40, { 6: 30, 9: 30 }), { fps: FPS });
  const v = judgeDensity(ev, { fps: FPS });
  const gap = ev.length >= 2 ? ev[1].start_frame - ev[0].start_frame : -1;
  check(ev.length === 2, `两段没被并成一段（实得 ${ev.length}）`);
  check(gap === 11 && v.status === 'reject' && v.reason.includes('隔'),
        `间隔 11 帧必须拒，且理由报出这个数（实得 gap=${gap}，${v.reason?.slice(0, 50)}）`);
}

// A5 滞回：一次动效的中段慢下来（差掉到地板线下但没停）——**不许**被切成两段。
//    没有回落线的话，这条曲线会被读成「两段相隔 0.25 s」，一支本来合法的缓动就此被判红。
{
  const ev = motionEvents(curve(40, { 4: 10, 5: 8, 6: 1, 7: 1, 8: 6, 9: 9 }), { fps: FPS });
  check(ev.length === 1, `中段慢下来的那一支仍算 1 段（实得 ${ev.length}）`);
}

// A6 地板线：整条曲线都在 1.0（低于 1.5）——不算「在动」。
{
  const ev = motionEvents(curve(40, Object.fromEntries(
    Array.from({ length: 40 }, (_, i) => [i, 1.0]))), { fps: FPS });
  check(ev.length === 0, `1.0 的抖动数出 0 段（地板线 ${MOTION_DIFF_MIN} 实得 ${ev.length}）`);
}

// A8 只隔**一格**真安静：仍然是两段，且按「相邻 ≥0.4 s」判红。
//    这一支是补上的——原来有一条「隔 ≤1 格就合并」的规则，真渲一支三件先后滑入的镜
//    （每次之间恰好一格 0）被它并成一段、整镜判红。规则删了，这里把它钉住：
//    8 Hz 下一格安静就是判据的全部分辨率，谁再想「平滑一下」就得先推翻这一支。
{
  const ev = motionEvents(curve(40, { 6: 30, 8: 30 }), { fps: FPS });
  const v = judgeDensity(ev, { fps: FPS });
  check(ev.length === 2,
        `中间只安静一格的两段没被并掉（实得 ${ev.length} 段，起于 ${ev.map((e) => e.start_frame).join('/')} 帧）`);
  check(v.status === 'reject' && v.reason.includes('隔'),
        `隔 7 帧（0.23 s）的两段判红且理由报出间隔（实得 ${v.status}：${v.reason?.slice(0, 40)}）`);
}

// A7 未判：拿不到采样时，判据必须说「未判」，而且不许等于通过。
{
  const got = motionDensity('/nonexistent/never.mp4',
                            { execFileSync: () => { throw new Error('no ffmpeg'); }, fps: FPS,
                              srcWidth: 1080, srcHeight: 1920 });
  check(got.status === 'undetermined' && !got.reason?.includes('ok'),
        `抽帧拿不到 → undetermined（实得 ${got.status}：${String(got.reason).slice(0, 40)}）`);
  check(got.status !== 'ok', 'undetermined 不等于通过');
}

console.log(`\nA 半：${n - fail}/${n} 通过`);

// ---- B 半：真渲两镜 ----
const BASE = process.env.DG_BASE || 'http://127.0.0.1:8010';

const post = async (p, body) => {
  const r = await fetch(`${BASE}${p}`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { status: r.status, json: await r.json().catch(() => null) };
};

async function live() {
  console.log('\n=== B 半：真渲两镜（要 creation-remotion 服务）===');
  const health = await fetch(`${BASE}/healthz`).then((r) => r.json()).catch(() => null);
  if (!health?.ok) {
    unverified.push(`B 半未跑：${BASE}/healthz 不可达——两个红样与一个绿样都还没在真像素上验过`);
    return;
  }
  const run = async (name, code, key) => {
    const r = await post('/render-shot', {
      code, cache_key: `motionprobe-${key}`,
      input_props: { fps: FPS, durationInFrames: 2 * FPS, shotId: 'motionprobe', text: '' },
      fields: [],
    });
    if (r.status !== 200 || !r.json?.ok) {
      unverified.push(`B 半「${name}」渲染没成功（HTTP ${r.status}：${JSON.stringify(r.json).slice(0, 120)}）`);
      return null;
    }
    const m = r.json.motion;
    if (!m) {
      unverified.push(`B 半「${name}」：容器没回传 motion 字段——镜像里的 server.mjs 比这条判据旧`);
      return null;
    }
    return m;
  };
  // 未判的两支**不进 check 计数**：写 `check(st === null || …)` 看上去稳，
  // 实际是「拿不到判据就算通过」——正是这条链一路在治的那种绿。渲不成的原因
  // 已经进未判清单了，分母里不该有它。
  const st = await run('静帧镜', STATIC_SHOT, 'static');
  if (st) {
    check(st.status === 'reject',
          `静帧镜被 ⑭ 拒（实得 ${st.status}：${String(st.reason).slice(0, 60)}）`);
  }
  const sim = await run('三件齐发镜', SIMULTANEOUS_SHOT, 'simultaneous');
  if (sim) {
    check(sim.status === 'reject' && String(sim.reason).includes('跳变'),
          `三件齐发的镜被拒，理由是「一次跳变」（实得 ${sim.status}：` +
          `${String(sim.reason ?? '').slice(0, 60)}，数到 ${sim.events?.length ?? '?'} 段）`);
  }
  const sq = await run('先后三件', SEQUENCED_SHOT, 'sequenced');
  if (sq) {
    check(sq.status === 'ok',
          `先后三件的镜放行（实得 ${sq.status}：${String(sq.reason ?? '').slice(0, 60)}，` +
          `数到 ${sq.events?.length ?? '?'} 段）`);
  }
}

await live();

console.log();
if (unverified.length) {
  console.log('未判清单（跳过 ≠ 通过）：');
  for (const s of unverified) console.log(`  · ${s}`);
}
// 「PASS」这两个字只许在没有未判项时说：B 半没跑（服务不可达）时，上面那两行
// 判据一次都没执行过，打一个光秃秃的 PASS 就是拿未判当通过。
console.log(fail === 0
  ? (unverified.length ? 'A 半通过；B 半未判（不算通过，见上）' : 'PASS')
  : `FAIL ${fail}`);
process.exitCode = fail === 0 ? 0 : 1;
