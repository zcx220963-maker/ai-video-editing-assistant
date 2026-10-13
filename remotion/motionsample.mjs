/**
 * 闸⑭ 动效事件密度 —— 把「有动效但看不出动」变成像素上可数的两个数。
 *
 * 判的是**画面**，不是代码：一段把每个元素的 `interpolate()` 都写成 `[0, 0]`
 * 的代码在声明上「有动效」，画面上从第 0 帧就是终态；反过来，一段把三件事写成
 * 同一帧齐发的代码也「有动效」，但看起来是一次跳变。两种都不该被算成通过，
 * 而它们只在像素上分得开。
 *
 * 办法：一次 ffmpeg 把整片抽成**固定采样率的灰度小图**（rawvideo，每帧
 * `WIDTH×h` 字节），逐对相邻采样算平均绝对差，得到一条「这一格变了多少」的曲线；
 * 再按地板线把它切成**先后变化段**（进入过地板线、离开过回落线，中间不切）。
 * 判据两条：段数 ≥2、相邻段的起点间隔 ≥0.4 s。
 *
 * 三件事刻意**不**做：
 *   · 不用代码里 `card.motion` 的声明去数事件——那是自报，不是证据；
 *   · 不做颜色/边缘检测——灰度差已经够了，多一层只是多一处会漂的东西；
 *   · 不把「差多大」当成「动得好不好」——那是审美，这条闸只回答「动没动、是不是先后」。
 */

/** 采样率（每秒几张）。这个数由判据反推：最小的相邻事件间隔是 0.4 s，
 *  采样必须比它密出「还留得下一次切分」的余量——8 Hz 下一格 0.125 s，
 *  两次事件最少能相隔 0.25 s 就被判出来（4 Hz 时最小可分辨间隔是 0.5 s，
 *  比要判的 0.4 s 还宽，那条判据就成了摆设）。 */
export const MOTION_SAMPLE_HZ = 8;

/** 采样图宽度（高度按画幅等比算出）。64 px 宽足够看出「元素动了没有」，
 *  又小到 20 秒的片子整条采样只几百 KB 内存（8 Hz × 0.5 秒/帧的灰度小图）。 */
export const MOTION_SAMPLE_W = 64;

/** 一格的平均绝对差超过它才算「这一格在变」（0–255 灰度）。
 *  这个数是**判据选型，不是量出来的**：一支元素横穿画面的镜，单格差在几十；
 *  一次 0.5 s 的淡入（元素占画面一成、亮度变化 20 级）折算到整幅约 2。
 *  取 1.5 是「宁可漏报小的、不许把静止判成在动」这一侧的地板线。
 *  它由 `tests/motion-probe.mjs` 的几支构造样本钉住（静帧必须 0 段、齐发必须 1 段、
 *  先后三件必须 3 段）——改这个数就得同时改那几支。 */
export const MOTION_DIFF_MIN = 1.5;

/** **回落线**（滞回的下沿，取地板线的 0.4 倍）：一段动效收尾时差会一路衰减，
 *  若按同一条地板线判「还在不在动」，缓出的尾巴会被切成好几段假事件。
 *  所以进来的门槛是 `diff_min`、出去的门槛是这一条，中间不切分。 */
export const MOTION_DIFF_END = MOTION_DIFF_MIN * 0.4;

// 这里原来还有一条「相隔 ≤1 格的两段合并成一段」（防亚像素抖动把一段切碎）。
// **真渲把它证伪了**，所以删掉而不是调小：
//   一支三件先后滑入（每件 0.35 s、彼此隔 0.6 s）的镜，8 Hz 采出来是
//   [0,0,4.9,3.8,4.4, 0, 1.7,5.0,6.8,4.3, 0, 3.8,16.1,22.8,8.7]
//   ——三次事件之间**各只有一格真安静**（值就是 0）。合并规则 (gap ≤ 1) 把它们
//   并成一段，判据于是说「所有元素挤在同一段时间里一起变」，把一支合法片判红。
//   而 8 Hz 下一格是 0.125 s：判据要的「相邻 ≥0.4 s」在画面上本来就只剩 1~2 格
//   安静可言，合并规则吃掉的正是判据要量的那个间隔——两支事件挨得越近、越是
//   判据要拦的那种，越容易被它并掉。**这条规则与判据互相抵消。**
// 防「切碎」由回落线负责（A5 那支钉着：中段差掉到地板线下但没停，仍算一段）；
// 真碎成一格一格的抖动过不了 1.5 的**进入**门槛，回不到段里。

/** 判据：每镜至少这么多个**先后**变化段。 */
export const MOTION_MIN_EVENTS = 2;

/** 判据：相邻变化段的起点至少隔这么久。 */
export const MOTION_MIN_GAP_SEC = 0.4;

/** 采样序号 → 帧号。ffmpeg 的 `fps=` 抽帧落在 t = i/hz，乘以 fps 就是帧。 */
export const frameOfSample = (i, fps, hz = MOTION_SAMPLE_HZ) =>
  Math.round((i * fps) / hz);

/** 一串「相邻采样之间的平均绝对差」→ 先后变化段（**纯函数**，不碰 ffmpeg）。
 *
 *  为什么先做纯函数：这一层才是判据本体（地板线、分段、合并），而它完全不需要
 *  真渲一支片就能证红证绿。把 ffmpeg 那一段单独隔在 `sampleGray()` 里，
 *  闸的判据就不必靠「先烧 30 秒视频」才能被测到。
 *
 *  `diffs[i]` 描述的是采样 `i` 到 `i+1` 之间的变化。一段覆盖 `diffs[a..b]`，
 *  起止帧取 `frameOfSample(a)` 与 `frameOfSample(b + 1)`。
 */
export function motionEvents(diffs, {
  fps, hz = MOTION_SAMPLE_HZ, min = MOTION_DIFF_MIN, end = MOTION_DIFF_END,
} = {}) {
  const cells = diffs ?? [];
  const runs = [];
  let run = null;
  cells.forEach((d, i) => {
    const v = Number.isFinite(d) ? d : 0;
    if (!run) {
      if (v > min) run = { first: i, last: i, peak: v };          // 进入：过地板线
      return;
    }
    if (v > end) {                                                 // 留在里面：过回落线
      run.last = i;
      run.peak = Math.max(run.peak, v);
    } else {
      runs.push(run);                                              // 退出
      run = null;
    }
  });
  if (run) runs.push(run);

  return runs.map((r) => ({
    start_frame: frameOfSample(r.first, fps, hz),
    end_frame: frameOfSample(r.last + 1, fps, hz),
    start_sec: Number((r.first / hz).toFixed(3)),
    end_sec: Number(((r.last + 1) / hz).toFixed(3)),
    peak_diff: Number(r.peak.toFixed(2)),
  }));
}

/** 变化段 → 判据结论。`{status: 'ok'|'reject', reason}`；理由是一句人话。 */
export function judgeDensity(events, {
  fps, minEvents = MOTION_MIN_EVENTS, minGapSec = MOTION_MIN_GAP_SEC,
} = {}) {
  const list = events ?? [];
  if (list.length < minEvents) {
    return {
      status: 'reject',
      reason: `全片只数到 ${list.length} 个先后变化段（要 ≥${minEvents} 个）——` +
        (list.length === 0
          ? '整镜从头到尾没有可测的像素变化：所有元素第 0 帧就已到位，这是一张静帧。'
          : '所有元素挤在同一段时间里一起变，看起来是一次跳变而不是「先后动效」。'),
      events: list,
    };
  }
  const gapFrames = Math.round(minGapSec * fps);
  for (let i = 1; i < list.length; i++) {
    const gap = list[i].start_frame - list[i - 1].start_frame;
    if (gap < gapFrames) {
      return {
        status: 'reject',
        reason: `第 ${i} 与第 ${i + 1} 个变化段的起点只隔 ${gap} 帧（帧 ${list[i - 1]
          .start_frame} → ${list[i].start_frame}，要 ≥${gapFrames} 帧 = ${minGapSec} s）` +
          '——挨得太近的两段在画面上读不出先后。',
        events: list,
      };
    }
  }
  return { status: 'ok', events: list };
}

/** 抽帧并算相邻差。→ `{ok: true, width, height, samples, diffs}` 或
 *  `{ok: false, reason}`（拿不到可判的采样时**不许**折成「没问题」）。 */
export function sampleGray(file, {
  execFileSync, fps, srcWidth, srcHeight, hz = MOTION_SAMPLE_HZ, width = MOTION_SAMPLE_W,
} = {}) {
  const height = Math.max(2, Math.round((width * srcHeight) / srcWidth / 2) * 2);
  const want = `fps=${hz},scale=${width}:${height}`;
  let buf;
  try {
    buf = execFileSync('ffmpeg', [
      '-v', 'error', '-i', file, '-vf', want, '-f', 'rawvideo', '-pix_fmt', 'gray', '-',
    ], { maxBuffer: 64 * 1024 * 1024, timeout: 60000, killSignal: 'SIGKILL' });
  } catch (err) {
    return { ok: false, reason: `ffmpeg 抽帧失败：${String(err?.message ?? err).slice(0, 160)}` };
  }
  const per = width * height;
  const n = Math.floor(buf.length / per);
  if (n < 2) {
    return { ok: false, reason: `只抽到 ${n} 张采样（需要 ≥2 张）——片子太短或抽帧没生效` };
  }
  const diffs = [];
  for (let i = 0; i + 1 < n; i++) {
    const a = i * per;
    const b = (i + 1) * per;
    let sum = 0;
    for (let p = 0; p < per; p++) sum += Math.abs(buf[a + p] - buf[b + p]);
    diffs.push(sum / per);
  }
  return {
    ok: true,
    width, height, samples: n, hz,
    diffs: diffs.map((d) => Number(d.toFixed(3))),
  };
}

/** 一个整镜的口：抽帧 → 分段 → 判。返回进逐镜账的那一份（含原文与理由）。 */
export function motionDensity(file, {
  execFileSync, fps, srcWidth, srcHeight, hz = MOTION_SAMPLE_HZ,
} = {}) {
  const got = sampleGray(file, { execFileSync, fps, srcWidth, srcHeight, hz });
  if (!got.ok) return { status: 'undetermined', reason: got.reason, samples: 0 };
  const events = motionEvents(got.diffs, { fps, hz });
  const verdict = judgeDensity(events, { fps });
  return {
    status: verdict.status === 'ok' ? 'ok' : 'reject',
    reason: verdict.reason ?? null,
    samples: got.samples,
    sample_hz: hz,
    // 曲线与分段都留下：红的时候要能看出「它数到的是几段、每段多大」，
    // 只回一句「密度不够」是没法让人改的。
    diffs: got.diffs,
    events,
    thresholds: {
      diff_min: MOTION_DIFF_MIN, min_events: MOTION_MIN_EVENTS,
      min_gap_sec: MOTION_MIN_GAP_SEC, sample_hz: hz,
    },
  };
}
