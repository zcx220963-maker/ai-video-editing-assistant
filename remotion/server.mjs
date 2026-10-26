/**
 * creation-remotion —— 图形科普片代码渲染链的常驻服务。
 *
 * 三个口（spec §2.2）：
 *   GET  /healthz       → {ok, node, remotion, chromium, gl, master_hash, lockfile_hash}
 *   POST /lint          → {ok, errors:[{rule,line,msg}]}
 *   POST /render-shot   → 渲一镜，回元数据；字节留在 /out，由 GET /artifact 取
 *
 * 两条纪律写在这里而不是调用方：
 * 1. **render-shot 口自己再 lint 一遍**。否则「先调 /lint 再调渲染」是调用方的
 *    自觉，而不是闸——绕过一次就等于闸① 不存在。
 * 2. **打包串行、渲染限路并发**。打包要写同一份 `src/user-shots/shot.tsx`，必须一镜一镜来
 *    （每镜 1~3 秒）；打包产物是独立目录，之后的逐帧渲染（每镜 17~35 秒，真正的大头）
 *    按 `DG_RENDER_SLOTS` 路并发（缺省 2）。无头 WebGL 走软件渲染，开太多路会把容器
 *    打到 OOM——路数是给机器配的，不是越大越好。
 *
 * 另有 POST /render-stills：同一套打包，只渲几帧小 JPEG（渲染前计划确认卡上的预览图）。
 * 成本约是整镜渲染的百分之一，这是 Remotion 官方推荐的「抽几帧看」的验证方式。
 * 它还带一个可选的 `variants` 分段口（见 `stillJobs`）：一段代码配多组 props，各渲
 * 一张——「文字库」的六档字幕样式就是这么一次打包渲完的。
 * 它顺带回一行版面读数（`layout`，见 `renderStills`）——那是修一处版面最便宜的时刻。
 */
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { lintShotCode, lintTemplateCode } from './lint.mjs';
import { motionDensity } from './motionsample.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(ROOT, 'out');
// 渲染期 `staticFile()` 的根。供图通道撤掉后每一镜没有自己的一份素材字节，
// 这个根无条件就是 `public/`（字体住在 `public/fonts/`，随镜像发）。
const PUBLIC = path.join(ROOT, 'public');
const recursiveDir = { recursive: true };
const SHOT_FILE = path.join(ROOT, 'src', 'user-shots', 'shot.tsx');
// 用户沉淀的模板：单独一个目录，**只由 /templates 口写入**，随镜像发布的内置模板
// 不在这里。目录挂命名卷——容器重建后文件还在；真丢了也会被 `sync_validated()`
// 从 PG 重推，所以这里不是真相，只是缓存。
const GENERATED_DIR = path.join(ROOT, 'src', 'lib', 'style', 'generated');
// 脚本产出的素材：**只由 /assets 口写入**，落在 `public/assets/`。
//
// 为什么要有这一条（2026-10-23）：`run` 工具让技能脚本能跑了，但跑出来的 PNG/SVG
// 进不了画面——`public/` 里原本只有 `fonts/`，`staticFile()` 指别的名字运行时就是
// 一张空图（`dg/staticfile-font-only` 拦的就是这个）。这个口补的是「出文件 → 进画面」
// 那一段：脚本产物推上来，镜头代码用 `staticFile('assets/xxx.png')` 就能拿到。
const ASSETS_DIR = path.join(ROOT, 'public', 'assets');
const ASSET_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,60}$/;
// 一张图正常几十 KB；8MB 够放一张 4K PNG，又不至于把 public/ 变成用户网盘。
const ASSET_MAX_BYTES = 8 * 1024 * 1024;
const TEMPLATE_NAME_RE = /^[a-z][a-z0-9-]{1,40}$/;
// 一份模板单文件，正常 5~30KB；200KB 足够画复杂场景，又不至于让写盘/推送变成 DOS。
const TEMPLATE_MAX_BYTES = 200 * 1024;
const PORT = Number(process.env.PORT || 8010);
// 无 GPU 的容器里实测：angle/egl/swiftshader 三档都拿不到 WebGL 上下文（three.js 镜
// 直接 500），只有 swangle 画得出带光影的几何体。改这一档等于决定 3D 通道在不在。
const GL = process.env.DG_GL || 'swangle';
const BROWSER = process.env.DG_BROWSER || '/usr/bin/chromium';

/** 三条回读通道的行首标记。**与 `src/tokens.ts` 的同名常量一起改**——写在这里而不是
 *  某个处理函数里，是因为 `/render-shot` 与 `/render-stills` 两个口都要认它们
 *  （版面那一路 2026-10-16 起两个口都接）。 */
const TOKEN_MARKER = 'DG_TOKENS:';   // = TOKEN_LOG_MARKER
const FIELD_MARKER = 'DG_FIELDS:';   // = FIELD_LOG_MARKER
const LAYOUT_MARKER = 'DG_LAYOUT:';  // = LAYOUT_LOG_MARKER

/** 探针帧 = 末帧往前这一格（= `src/tokens.ts` 的 `FIELD_PROBE_TAIL`）。
 *  为什么这个数在服务端也要有一份：`/render-stills` 得知道**该抽哪一帧**才拿得到回读行
 *  ——探针只在那一帧跑，抽别的帧回来的是「这一镜没扫过」。 */
const PROBE_TAIL = 2;

/** 版面那一行**有两个上报者**（`src/lib/LayoutProbe.tsx` 的 `by`）：母版量画面带，
 *  测量壳量整帧。套了母版的镜两行都会来，所以「谁说了算」得有个明确的规矩，
 *  不能靠「后一行覆盖前一行」——那等于把胜负交给 effect 的执行次序，
 *  而这个次序正是 2026-10-16 那次真机事故的成因（壳的整帧口径盖掉了母版的画面带口径，
 *  母版镜的 `band` 全报成整帧、进出场那层变换也没折回去）。
 *
 *  规矩：**先要真扫到的，再要母版的**。
 *   - 两行都真扫到 → 母版那一行（画面带口径才是这一镜的版面，壳的整帧口径是把
 *     母版的 gutter 与字幕带也算进去了）；
 *   - 只有一行真扫到 → 那一行（母版那条「画面带 ref 未挂载」的落空行不该把一份
 *     真读数挤掉）；
 *   - 两行都没扫到 → 母版那一行（它的 `reason` 才是这一镜的真实原因）。
 *  一个都没有 → null（Python 侧落 `missing`，不是「干净」）。 */
function pickLayout(lines) {
  if (!lines.length) return null;
  const scanned = (l) => !!l && typeof l === 'object' && l.scanned
    && typeof l.scanned === 'object';
  const pool = lines.filter(scanned);
  const from = pool.length ? pool : lines;
  return from.find((l) => l.by === 'master') ?? from[from.length - 1];
}

const require = createRequire(import.meta.url);
const remotionVersion = require('remotion/package.json').version;

let remotionApi = null;
async function loadRemotion() {
  if (remotionApi) return remotionApi;
  // 具名导出：这一版 @remotion/renderer 没有一个叫 renderer 的命名空间，
  // 解构 { renderer } 会得到 undefined，取它的方法时进程直接没了。
  const [{ bundle }, { selectComposition, renderMedia, renderStill, openBrowser }] =
    await Promise.all([
      import('@remotion/bundler'),
      import('@remotion/renderer'),
    ]);
  remotionApi = { bundle, selectComposition, renderMedia, renderStill, openBrowser };
  return remotionApi;
}

/** 打包串行闸：打包要写同一份 shot.tsx，一次只放一镜进去。 */
let chain = Promise.resolve();
const serialize = (fn) => {
  const run = chain.then(fn, fn);
  chain = run.then(() => {}, () => {});
  return run;
};

/** 渲染限路闸：打包完的产物是独立目录，逐帧渲染可以多路并发。
 *  释放时把名额**直接交给**下一个等待者（不先减再加），避免两者之间被第三个插队超额。 */
const RENDER_SLOTS = Math.max(1, Number(process.env.DG_RENDER_SLOTS || 4));

/** `delayRender` 的超时（毫秒）。Remotion 缺省 30 s，这里放到 3 min。
 *
 *  为什么：`@dg/fonts` 把中文字体按 unicode-range 切成 101 片，每片一次 `loadFont`
 *  ＝一次 `delayRender` ＋一次 fetch。这些 font 文件本身很小（000 号 8 KB，最大的
 *  几十 KB），**单独载一次连 0.1 s 都用不上**；但成片渲染时是 4 路镜头切片 + 8 路
 *  带图自检静帧同时在烤软件 WebGL，每个标签页还各开一份独立上下文（字体缓存不互通），
 *  一个页面可能连续几十秒分不到 CPU，30 s 的窗口就被耗光。
 *
 *  注意这个窗口管两件事：`delayRender` 本身，以及「等页面把 React 组件渲染出来」
 *  （Remotion 内部用 `timeoutInMilliseconds + 3000`）。两次真机失败正好各撞上一个：
 *  `A delayRender() "Loading font Noto Sans SC (url: /public/fonts/NotoSansSC-000.woff2 …"`
 *  与 `waiting for the page to render the React component failed: timeout 33000ms exceeded`
 *  （2026-10-21 成片渲染）。同一次渲染里其余切片正常出片，说明不是挂死，是排队。
 *
 *  放长窗口＝把「排队等一会儿就能过」和「真的卡死」分开：真卡死的那一路仍会在 3 min
 *  内失败并报出来，不会拖成无限等。要更紧/更松就设 `DG_DELAY_TIMEOUT_MS`。 */
const DELAY_TIMEOUT_MS = (() => {
  const n = Number(String(process.env.DG_DELAY_TIMEOUT_MS ?? '').trim());
  if (!Number.isFinite(n) || n <= 0) return 180000;
  return Math.min(Math.max(Math.round(n), 10000), 600000);
})();
let slotsBusy = 0;
const slotWaiters = [];
function releaseSlot() {
  const next = slotWaiters.shift();
  if (next) next();
  else slotsBusy -= 1;
}
async function acquireSlot() {
  if (slotsBusy < RENDER_SLOTS) {
    slotsBusy += 1;
    return;
  }
  await new Promise((resolve) => slotWaiters.push(resolve));
}

/** 片壳模式的**结构**检查：外框与字幕归片壳，镜头代码里不能再出现 `FilmLayout`
 *  或整条字幕（`CaptionLayer`/`CaptionFrame`）。
 *
 *  为什么在渲染入口判而不是交给「看图评审」：这是同一段代码的静态事实，判起来零成本；
 *  而看图评审要真渲一帧、再花一次模型调用，还把「有没有两条字幕」这种确定的事变成了
 *  一次猜测。`CaptionText` 仍然允许——招牌、图表上的字是画里的东西，不是字幕。
 *  报错直接 400，写手拿到一句能照着改的话，不必烧像素。 */
function assertShellContract(code, inputProps) {
  const shell = String(inputProps?.shell ?? '').trim();
  if (!shell) return;
  const hits = [];
  if (/<\s*FilmLayout\b/.test(code)) hits.push('FilmLayout');
  if (/<\s*CaptionLayer\b/.test(code)) hits.push('CaptionLayer');
  if (/<\s*CaptionFrame\b/.test(code)) hits.push('CaptionFrame');
  if (!hits.length) return;
  const err = new Error(
    `这一片用了统一片壳（${shell}），画面外框和底部字幕由片壳统一画；`
    + `镜头代码里不能再出现 ${hits.join(' / ')}——只画这一镜的主体。`);
  err.status = 400;
  throw err;
}

/** 打包缓存：同一段代码只 bundle 一次（2026-10-20）。
 *
 *  一镜的打包是 1~3 秒的固定成本，而同一段代码在一条流程里至少会被打包两遍：
 *  预览静帧一遍、整镜渲染一遍；重试与「预览后改一镜」还会再打。缓存的是
 *  `bundle()` 的产物目录（`serveUrl`），不是代码本身。
 *
 *  **键里必须带库指纹（2026-10-23 修）。** 原先的键只有镜头代码的哈希，注释里写着
 *  「别的输入（entry/母版/别名/依赖）在同一个进程里不会变」——这句话只在镜像不变时
 *  成立。改 `src/lib/style/*` 下的风格包、改母版 `FilmLayout.tsx`、改 `tokens.ts`，
 *  镜头代码一个字没动，哈希就不变，于是**拿回上一份旧产物**：改了半天，成片一模一样。
 *  真机上就是这样踩的——`StyleCaption` 从贴底改成贴角，重渲出来的静帧与旧图逐字节
 *  相同。指纹扫的是「会被打进包、但不属于镜头代码」的那几处，一秒内复用一次，
 *  两次请求（静帧 + 整镜）不会各扫一遍。
 *
 *  只缓最近的若干份：一份产物是几 MB，几十镜全留会白占内存；淘汰只从表里摘掉，
 *  磁盘目录还在，正在渲的那一份不会因为表里没了而消失。 */
const BUNDLE_CACHE_MAX = Math.max(2, Number(process.env.DG_BUNDLE_CACHE || 24));
const bundleCache = new Map();   // codeHash:libStamp -> { serveUrl, bundleMs, at }

/** 库指纹覆盖的路径：会被打进包、但不属于镜头代码的那些。 */
const STAMP_FILES = [
  'src/index.ts', 'src/entry.tsx', 'src/FilmLayout.tsx', 'src/tokens.ts',
  'src/fonts.ts',
];
// `public` 必须在册（2026-10-24 真机踩的）：`bundle()` 打包时会把 `publicDir` 整个
// **复制进包**，所以贴图/字体换了一份、镜头代码一个字没动，指纹不变 → 复用旧 bundle
// → 渲出来还是旧图。改内置贴图那会儿就是这样：`env-studio.png` 从 29KB 换成 32KB，
// 探针量出的球面亮度两次**一模一样**（25.6），图也逐字节相同。
// 对生产更要紧：`/assets` 口上传的新素材同样会被旧 bundle 挡住，而且一点提示都没有。
const STAMP_DIRS = ['src/lib', 'src/themes', 'src/demos/_fixtures', 'public'];

let stampMemo = { at: 0, stamp: '' };

/** 库指纹：这些文件的 (size, mtime) 列表哈希。文件没变 → 同一个值。 */
async function libStamp() {
  if (Date.now() - stampMemo.at < 1000) return stampMemo.stamp;
  const parts = [];
  for (const rel of STAMP_FILES) {
    try {
      const s = await fs.stat(path.join(ROOT, rel));
      parts.push(`${rel}:${s.size}:${Math.round(s.mtimeMs)}`);
    } catch {
      parts.push(`${rel}:-`);
    }
  }
  for (const dir of STAMP_DIRS) {
    try {
      const names = (await fs.readdir(path.join(ROOT, dir), recursiveDir)).sort();
      for (const n of names) {
        const abs = path.join(ROOT, dir, n);
        try {
          const s = await fs.stat(abs);
          if (s.isFile()) parts.push(`${dir}/${n}:${s.size}:${Math.round(s.mtimeMs)}`);
        } catch { /* 扫的过程中被删了，跳过 */ }
      }
    } catch {
      parts.push(`${dir}:-`);
    }
  }
  const stamp = sha16(Buffer.from(parts.join('|'), 'utf8'));
  stampMemo = { at: Date.now(), stamp };
  return stamp;
}

/** 打包一镜：写 shot.tsx → bundle()。调用方负责把它放进 `serialize`。 */
async function bundleShot(code) {
  const { bundle } = await loadRemotion();
  const codeHash = `${sha16(Buffer.from(String(code), 'utf8'))}:${await libStamp()}`;
  const hit = bundleCache.get(codeHash);
  if (hit) {
    hit.at = Date.now();
    return { serveUrl: hit.serveUrl, bundleMs: 0, bundleCached: true };
  }
  await fs.mkdir(path.dirname(SHOT_FILE), recursiveDir);
  await fs.mkdir(OUT, recursiveDir);
  // publicDir 是 bundle 的入参：目录不存在 bundler 会当场炸。`public/` 随镜像发出去，
  // 里面住着 `fonts/`——`staticFile('fonts/…')` 的根就是它。供图通道撤掉后每一镜
  // 不再有自己的一份素材字节，这个根无条件就是 `public/`。
  await fs.mkdir(PUBLIC, recursiveDir);
  await fs.writeFile(SHOT_FILE, code, 'utf8');
  const t0 = Number(process.hrtime.bigint() / 1000000n);
  const serveUrl = await bundle({
    entryPoint: path.join(ROOT, 'src', 'index.ts'),
    publicDir: PUBLIC,
    webpackOverride: (cfg) => ({
      ...cfg,
      resolve: {
        ...cfg.resolve,
        alias: {
          ...(cfg.resolve?.alias ?? {}),
          '@dg/film': path.join(ROOT, 'src', 'FilmLayout.tsx'),
          '@dg/tokens': path.join(ROOT, 'src', 'tokens.ts'),
          '@dg/fonts': path.join(ROOT, 'src', 'fonts.ts'),
          '@dg/motion': path.join(ROOT, 'src', 'demos', '_fixtures', 'Motion.tsx'),
          '@dg/polish': path.join(ROOT, 'src', 'demos', '_fixtures', 'Polish.tsx'),
          '@dg/brand': path.join(ROOT, 'src', 'demos', '_fixtures', 'Brand.tsx'),
          '@dg/fixtures': path.join(ROOT, 'src', 'demos', '_fixtures'),
          '@dg/lib': path.join(ROOT, 'src', 'lib'),
          '@dg/themes': path.join(ROOT, 'src', 'themes'),

        },
      },
    }),
  });
  const bundleMs = Number(process.hrtime.bigint() / 1000000n) - t0;
  bundleCache.set(codeHash, { serveUrl, bundleMs, at: Date.now() });
  while (bundleCache.size > BUNDLE_CACHE_MAX) {
    const oldest = [...bundleCache.entries()].sort((a, b) => a[1].at - b[1].at)[0];
    if (!oldest) break;
    bundleCache.delete(oldest[0]);
  }
  return { serveUrl, bundleMs, bundleCached: false };
}

/** 进程内共用一个 chromium（2026-10-20）。
 *
 *  预览这一路从前是「一个请求开一个浏览器」：一条 50 镜的片子要开 50 次，
 *  每次启动 chromium 是几百毫秒到一秒的固定成本，而真正画一帧只要几十毫秒。
 *  共用一个实例、每次请求开自己的 page，启动成本就摊成一次。
 *
 *  失败即丢弃：共享实例最坏的形态是「崩了之后所有人继续用」，下一个请求重开一个
 *  就好。这只影响性能，不影响正确性。
 *
 *  **丢弃要等最后一个在飞的请求**：预览是并发的（`MOTION_PREVIEW_PARALLEL`），
 *  一个请求遇到自己的错（比如一帧编码失败）就把共享浏览器关掉，会把同批其余请求
 *  一起打断——它们看到的是 `Target closed`，而真正的原因在别人那一帧。所以只在
 *  `stillsInflight <= 1`（自己就是最后一个）时丢弃；真崩了的浏览器会让所有请求各自
 *  失败，最后一个是它自己关掉的那个。 */
let sharedBrowser = null;
let sharedBrowserPromise = null;
let stillsInflight = 0;
async function sharedChrome() {
  if (sharedBrowser) return sharedBrowser;
  if (!sharedBrowserPromise) {
    sharedBrowserPromise = (async () => {
      const { openBrowser } = await loadRemotion();
      const b = await openBrowser('chrome', {
        browserExecutable: BROWSER, chromiumOptions: { gl: GL, headless: true },
      });
      sharedBrowser = b;
      return b;
    })();
  }
  try {
    return await sharedBrowserPromise;
  } catch (err) {
    sharedBrowserPromise = null;
    throw err;
  }
}
async function dropSharedChrome() {
  const b = sharedBrowser;
  sharedBrowser = null;
  sharedBrowserPromise = null;
  if (b) await b.close({ silent: true }).catch(() => {});
}

/** 一组要渲的静帧：分段（每个 props 各渲自己的帧）与不分段（一份 props 渲几帧）在
 *  这里归一成同一个形状，后面就只有一条渲染循环。`props` 里的 fps/durationInFrames
 *  是母版算时长的输入（闸②），缺一个就 400——**每一组各自判**，一段参数不全不该被
 *  另一段的合法参数掩盖过去。 */
function stillJobs(payload) {
  const norm = (raw) => {
    const p = {
      ...(raw ?? {}),
      fps: Number((raw ?? {}).fps ?? 30),
      durationInFrames: Number((raw ?? {}).durationInFrames ?? 0),
    };
    if (!p.durationInFrames) {
      const err = new Error('durationInFrames 必须由调用方注入');
      err.status = 400;
      throw err;
    }
    return p;
  };
  const variants = Array.isArray(payload.variants) ? payload.variants.slice(0, 16) : [];
  if (variants.length) {
    // 分段口：同一段代码、不同的 props，各渲一帧。**只打包一次**是它存在的唯一理由
    // ——「文字库」的六档字幕样式共用同一份样片代码，逐档各发一次请求就要各打包一遍
    // （1~3 秒），一屏样张要等十几秒。
    return variants.map((v, i) => {
      const props = norm(v?.input_props);
      const raw = Number(v?.frame);
      const frame = Number.isFinite(raw) ? Math.round(raw)
        : Math.floor(props.durationInFrames * 0.7);
      return { props, frames: [frame], variant: i };
    });
  }
  const props = norm(payload.input_props);
  const frames = (Array.isArray(payload.frames) ? payload.frames : [])
    .map(Number).filter(Number.isFinite).slice(0, 4);
  if (!frames.length) frames.push(Math.floor(props.durationInFrames * 0.7));
  return [{ props, frames, variant: null }];
}

/** 预览静帧：同一套打包，渲几帧小 JPEG（base64 内联回传，不落 /out）。

 *  `variants` 是可选的**分段口**：给了就按段渲（每段自己的 input_props 与帧号），
 *  没给就是老样子（一份 props 渲几帧）。两路的返回形状是同一个 `stills` 数组，
 *  分段那一路每项多一个 `variant` 序号——调用方按序号对号入座，不靠数组次序猜。
 *
 *  **版面回读（2026-10-16 新接）。** 这个口从前完全没接 `onBrowserLog`，于是模型在
 *  「计划还没确认、像素还没烧」的时候看不到任何版面问题，而那是修一处版面**最便宜**的
 *  时刻（一张静帧的成本约是整镜的百分之一，改法就是把两行代码错开）。
 *
 *  接法有两处讲究，都不许省：
 *  1. **必须真渲探针帧。** 探针只在 `durationInFrames - PROBE_TAIL` 那一帧跑，而
 *     不分段那一路缺省抽的是 `0.7 × 时长`——两个帧号不是一回事。只挂 `onBrowserLog`
 *     而不加帧，回应里永远是一条空读数，而空读数长得**像**「这一镜版面干净」。
 *  2. **探针帧不进 `stills`。** 它是为了取回读才渲的，不是调用方要的那张图；
 *     混进数组会改调用方已有的「按 frame 对号」假设。多渲一张的代价留在服务端这一层。
 *  分段口不加：那一路是「同一段代码、六档字幕样式」，各档各渲一遍探针帧等于把样张
 *  成本翻三倍，而它要回答的问题（这六档长什么样）跟版面无关。那一路的 `layout` 如实为 null。 */
async function renderStills(payload) {
  const code = String(payload.code ?? '');
  const jobs = stillJobs(payload);
  if (jobs.length) assertShellContract(code, jobs[0].props);
  const scale = Math.min(1, Math.max(0.2, Number(payload.scale ?? 0.4)));
  const { selectComposition, renderStill } = await loadRemotion();
  const { serveUrl, bundleMs } = await serialize(() => bundleShot(code));
  await acquireSlot();
  const browser = await sharedChrome();
  stillsInflight += 1;
  try {
    const t0 = Number(process.hrtime.bigint() / 1000000n);
    const stills = [];
    let layout = null;        // 只由不分段那一路填（见上面第 2 条）
    const layoutLines = [];
    const logNotes = [];
    const onBrowserLog = (log) => {
      const text = String(log?.text ?? '');
      const lat = text.indexOf(LAYOUT_MARKER);   // 实测可能带 " Tab N, file:line " 前缀
      if (lat >= 0) {
        try {
          layoutLines.push(JSON.parse(text.slice(lat + LAYOUT_MARKER.length)));
        } catch (err) {
          if (logNotes.length < 4) logNotes.push(`版面行解析失败：${String(err).slice(0, 120)}`);
        }
        return;
      }
      // 2026-10-26：原先这里 `return`，把组件运行时的 console.error / 警告全丢了——
      // 静帧渲成一片黑、is_blank 打回、模型重写三次还是黑，却没有任何线索。
      // 现在把 error / warning 带回（附 file:line），调用方据此能直接定位到 sNN.tsx 的某一行。
      const type = String(log?.type ?? 'log');
      if (type !== 'error' && type !== 'warning') return;
      if (logNotes.length >= 40) return;
      const loc = log?.location;
      const where = loc ? ` @ ${loc.url || ''}:${loc.lineNumber ?? '?'}:${loc.columnNumber ?? '?'}` : '';
      logNotes.push(`[browser:${type}]${where} ${text.slice(0, 400)}`);
    };
    for (const job of jobs) {
      // 分段口里每一段的时长可以不同（时长由 props 决定），所以组合要按段选一次；
      // 不分段那一路只有一段，行为与从前逐字相同。
      const composition = await selectComposition({
        serveUrl, id: 'shot', inputProps: job.props, puppeteerInstance: browser,
        browserExecutable: BROWSER, chromiumOptions: { gl: GL, headless: true },
        // 选组合这一步就会跑一遍镜头代码，字体那 101 个 delayRender 在这里挂着。
        timeoutInMilliseconds: DELAY_TIMEOUT_MS,
      });
      const last = composition.durationInFrames - 1;
      const clamp = (f) => Math.max(0, Math.min(last, Math.round(f)));
      // 探针帧：不分段那一路无条件渲一张（只为取回读行）。
      // 调用方恰好在 `frames` 里点了同一帧就**不重复渲**，那一张照进 stills。
      const probeFrame = job.variant === null ? clamp(composition.durationInFrames - PROBE_TAIL) : null;
      const want = job.frames.map(clamp);
      const all = probeFrame !== null && !want.includes(probeFrame) ? [...want, probeFrame] : want;
      for (const frame of all) {
        const { buffer } = await renderStill({
          serveUrl, composition, inputProps: job.props, frame, puppeteerInstance: browser,
          // 90 而不是 80：这些静帧既是确认卡上给人看的图，也是带图判官的输入。
          // 80 在 0.4 倍那档上还看不出来，scale 提到 1 之后细线插画与描边的边缘
          // 会开始糊——那正是「画面质量低」的第一眼来源。每张多几十 KB，可以接受。
          imageFormat: 'jpeg', jpegQuality: 90, scale,
          browserExecutable: BROWSER, chromiumOptions: { gl: GL, headless: true },
          timeoutInMilliseconds: DELAY_TIMEOUT_MS,
          onBrowserLog,
        });
        // 只为取回读渲的那一张不进 stills：调用方按 `frame` 对号入座，多塞一张会改掉
        // 它已有的假设（而且它并没有要这张图）。
        if (!want.includes(frame)) continue;
        stills.push({
          frame, jpeg_b64: buffer ? Buffer.from(buffer).toString('base64') : '',
          ...(job.variant === null ? {} : { variant: job.variant }),
        });
      }
    }
    const ms = Number(process.hrtime.bigint() / 1000000n) - t0;
    // 与 `/render-shot` 同一条规矩（`pickLayout`）：母版与测量壳各发一行时，母版优先。
    layout = layoutLines.length ? pickLayout(layoutLines) : null;
    console.log(`[stills] ${stills.length}帧 layout=${layout ? 'yes' : 'no'} `
      + `bundle=${bundleMs}ms render=${ms}ms`);
    return {
      ok: true, stills, layout,
      ...(logNotes.length ? { warnings: logNotes } : {}),
      timings: { bundle_ms: bundleMs, render_ms: ms },
    };
  } catch (err) {
    // 共享实例出过错就丢掉，下一个请求重开——留着崩溃的浏览器会让后面每一镜都失败。
    // 但只在**自己是最后一个在飞请求**时丢（见 `stillsInflight` 那段）：同批别的请求
    // 正用着这只浏览器，为一个局部错误关掉它等于把故障放大到整批。
    if (stillsInflight <= 1) await dropSharedChrome();
    throw err;
  } finally {
    stillsInflight -= 1;
    releaseSlot();
  }
}

/** 浏览器版本：整个进程生命周期只探一次并缓存。
 *  实测教训：Windows 上 chrome.exe --version 会把请求转交给已在跑的实例、永不返回，
 *  同步调用一次就能把 event loop 卡死（健康检查口拖垮全部渲染）。
 *  Linux 容器里 chromium --version 正常退出，但「探针不许无界地卡」这条与平台无关。 */
let chromiumVersionCache = null;
function chromiumVersion() {
  if (chromiumVersionCache !== null) return chromiumVersionCache;
  chromiumVersionCache = new Promise((resolve) => {
    execFile(BROWSER, ['--version'], { encoding: 'utf8', timeout: 5000, killSignal: 'SIGKILL' },
      (err, stdout) => {
        resolve(err ? `unavailable: ${err?.message?.slice(0, 80) ?? err}` : String(stdout).trim());
      });
  });
  return chromiumVersionCache;
}

async function readBody(req, limitBytes = 4 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > limitBytes) throw new Error('请求体过大');
    chunks.push(c);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

const send = (res, status, obj) => {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(body);
};

const sha16 = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 16);

/** 缓存键要的两段身份哈希（spec §5.2），Python 侧从这里取，不把文件字节再搬一遍。
 *
 *  母版 = `<FilmLayout>` + token + **版面探针**。改这几个文件＝全片换像素/换读数但不改
 *  任何一镜的代码，不进键就会命中旧母版渲出的切片。lockfile 同理：换一版 three.js
 *  也是换像素。
 *
 *  `lib/LayoutProbe.tsx` 2026-10-16 进这一份，不是顺手加：探针是**搬出去**的（原来在
 *  `FilmLayout.tsx` 里），只哈希母版的话，「改探针」不再改哈希——于是改完探针重渲，
 *  拿的还是旧键，命中的旧切片里 `layout_used` 仍是 `missing`，而新判据一条都不会响。
 *  这个错看不出来、且只在第二次之后出现，正是这条注释下面那段说的同一种。
 *
 *  **每次现读，不缓存。** 进程内缓存＝`docker cp` 换过母版后 /healthz 还在报旧哈希，
 *  于是新母版渲出的片子拿的还是旧键——命中缓存把旧切片发给新请求，正是这条链最贵的
 *  那种错（看不出来，且只在第二次之后出现）。几个小文件的读盘钱不买这个错。
 *
 *  读不到就抛：宁可 /healthz 500，也不许报一个「空的但看起来能用」的哈希。 */
async function versions() {
  const [layout, probe, tokens, lock] = await Promise.all([
    fs.readFile(path.join(ROOT, 'src', 'FilmLayout.tsx')),
    fs.readFile(path.join(ROOT, 'src', 'lib', 'LayoutProbe.tsx')),
    fs.readFile(path.join(ROOT, 'src', 'tokens.ts')),
    fs.readFile(path.join(ROOT, 'package-lock.json')),
  ]);
  return { master_hash: sha16(Buffer.concat([layout, probe, tokens])),
           lockfile_hash: sha16(lock) };
}

/** 闸① 字段对账三条规则的第二个输入：这一镜声明了哪些字段名。
 *
 *  调用方（Python 侧 `code_spec.field_values`）显式传 `declared_fields`；没传就退回
 *  注入的那份 `input_props.fields` 的键。这两份本必须由同一段代码算出，退回的意义是
 *  「少传一个键也不会把这三条闸静默关掉」——关掉它们的后果是界面能改一个画面上
 *  根本不生效的旋钮，而那正是这条通道要防的事。
 */
const declaredFrom = (payload) => {
  const raw = Array.isArray(payload.declared_fields) && payload.declared_fields.length
    ? payload.declared_fields
    : Object.keys(payload.input_props?.fields ?? {});
  return raw.map((s) => String(s));
};

/** 渲一镜的完整流水线。lint 不合规直接抛 400，不进 bundler。 */
async function renderShot(payload) {
  const code = String(payload.code ?? '');
  const declaredFields = declaredFrom(payload);
  const lint = await lintShotCode(code, declaredFields);
  assertShellContract(code, payload.input_props);
  if (!lint.ok) {
    const err = new Error('闸① lint 未通过');
    err.status = 400;
    err.lint = lint.errors;
    throw err;
  }

  const inputProps = {
    ...(payload.input_props ?? {}),
    fps: Number(payload.input_props?.fps ?? 30),
    durationInFrames: Number(payload.input_props?.durationInFrames ?? 0),
  };
  if (!inputProps.durationInFrames) {
    const err = new Error('闸② 帧预算：durationInFrames 必须由调用方按真实语音时长注入，不许默认');
    err.status = 400;
    throw err;
  }

  const key = String(payload.cache_key ?? '');
  if (!/^[A-Za-z0-9._-]{6,64}$/.test(key)) {
    const err = new Error('cache_key 必须由调用方按字节算好传入（[A-Za-z0-9._-]，6~64 位）；' +
      '这里缺省成时间戳就等于缓存永不命中、产物无界堆积。');
    err.status = 400;
    throw err;
  }
  // 渲染参数透传口。这一层以前一个参数都不接，全吃 Remotion 默认（并发=min(8,核/2)、
  // crf 18、png、scale 1），于是**验收循环**只有一种档：改一镜也得按成片分辨率烧一遍，
  // 来回十几分钟。加这个口不是为省而省——`draft` 只换像素档，不换判据：
  //   · 闸⑭ 抽的是 64 px 宽的灰度差，输出多大都归一到同一张采样图；
  //   · 闸⑥⑧⑲-a/⑳/㉕ 读的是母版量出来的**比例盒**（占带的份额），与分辨率无关；
  //   · 母版几何按 `useBody()` 报回来的设备像素算 K 倍率，缩档是自相似的。
  // 草稿档唯一看不出来的是 1 px 发丝线与字体微调，那一档由最终成片渲染兜住。
  const rp = payload.render ?? {};
  const draft = rp.draft === true;
  const scale = rp.scale == null ? 1 : Number(rp.scale);
  if (!Number.isFinite(scale) || scale <= 0.2 || scale > 1) {
    const err = new Error(`render.scale 必须在 0.2~1 之间（收到 ${payload.render?.scale}）`);
    err.status = 400;
    throw err;
  }
  const imageFormat = rp.image_format ?? null;
  if (imageFormat !== null && !['png', 'jpeg', 'webp'].includes(imageFormat)) {
    const err = new Error(`render.image_format 只认 png/jpeg/webp（收到 ${imageFormat}）`);
    err.status = 400;
    throw err;
  }
  const jpegQuality = rp.jpeg_quality == null ? null : Number(rp.jpeg_quality);
  if (jpegQuality !== null && (!Number.isFinite(jpegQuality) || jpegQuality < 0 || jpegQuality > 100)) {
    const err = new Error(`render.jpeg_quality 必须在 0~100（收到 ${rp.jpeg_quality}）`);
    err.status = 400;
    throw err;
  }
  const crf = rp.crf == null ? null : Number(rp.crf);
  if (crf !== null && (!Number.isFinite(crf) || crf < 0 || crf > 51)) {
    const err = new Error(`render.crf 必须在 0~51（收到 ${rp.crf}）`);
    err.status = 400;
    throw err;
  }
  const concurrency = rp.concurrency == null ? null : Number(rp.concurrency);
  if (concurrency !== null && (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 32)) {
    const err = new Error(`render.concurrency 必须是 1~32 的整数（收到 ${rp.concurrency}）`);
    err.status = 400;
    throw err;
  }
  // 草稿与成片**不能共用一个产物名**：`/out/<key>.mp4` 是切片缓存的字节身份，
  // 让一份 0.4 倍的片子占住成片档的键，下一次命中缓存就是在拿草稿冒充成片。
  const fileKey = draft ? `${key}.draft` : key;
  const outputLocation = path.join(OUT, `${fileKey}.mp4`);

  const { selectComposition, renderMedia } = await loadRemotion();
  const { serveUrl, bundleMs } = await serialize(() => bundleShot(code));
  // 打包完之后的逐帧渲染按名额并发（见 RENDER_SLOTS）。
  await acquireSlot();
  try {

  const warnings = [];
  let tokens = null;
  const now = () => Number(process.hrtime.bigint() / 1000000n);

  const t1 = now();
  const composition = await selectComposition({
    serveUrl,
    id: 'shot',
    inputProps,
    browserExecutable: BROWSER,
    chromiumOptions: { gl: GL, headless: true },
    timeoutInMilliseconds: DELAY_TIMEOUT_MS,
  });
  const selectMs = now() - t1;

  // 闸⑤ 的原料：母版渲染时会 console.log 一行 `DG_TOKENS:<json>`，
  // 这一版 renderer 没有 onBrowserOpen，而 onBrowserLog 是公开口、且只在真渲染时经过，
  // 读到的就是这一镜实际用上的值（不是根页面里母版的默认值）。
  // 三个行首标记在模块顶部（`/render-stills` 也认版面那一个）。
  let browserLogTruncated = false;
  let fieldsUsed = null;
  const layoutLines = [];   // 版面那一行可能有两条（母版 + 测量壳），收齐了再 `pickLayout`
  const onBrowserLog = (log) => {
    const text = String(log?.text ?? '');
    const at = text.indexOf(TOKEN_MARKER);   // 实测可能带 " Tab N, file:line " 前缀，不按行首判
    if (at >= 0) {
      try {
        tokens = JSON.parse(text.slice(at + TOKEN_MARKER.length));
      } catch (err) {
        warnings.push(`token 行解析失败：${String(err).slice(0, 120)}`);
      }
      return;
    }
    const fat = text.indexOf(FIELD_MARKER);
    if (fat >= 0) {
      let parsed = null;
      try {
        parsed = JSON.parse(text.slice(fat + FIELD_MARKER.length));
      } catch (err) {
        warnings.push(`字段行解析失败：${String(err).slice(0, 120)}`);
        return;
      }
      // 同一帧被 seek 到两遍时取后一遍，但两遍不一样必须记账：那说明这一镜「同一帧
      // 画不出同一个盒」，而闸⑥ 与字段级命中表整个都建立在「这一行的盒就是像素」上。
      if (fieldsUsed && JSON.stringify(fieldsUsed) !== JSON.stringify(parsed)) {
        warnings.push('字段回读出现两遍且内容不一致（同一探针帧两次 seek 得到不同的盒），' +
                      '取最后一遍——这条通道的证据链在这一镜上不牢');
      }
      fieldsUsed = parsed;
      return;
    }
    const lat = text.indexOf(LAYOUT_MARKER);
    if (lat >= 0) {
      try {
        layoutLines.push(JSON.parse(text.slice(lat + LAYOUT_MARKER.length)));
      } catch (err) {
        warnings.push(`版面行解析失败：${String(err).slice(0, 120)}`);
      }
      return;
    }
    // 镜头代码里的 console.log 一律进 warnings（有上限，不让一段狂打的代码撑爆响应）
    if (warnings.length < 20) warnings.push(`[browser] ${log?.type}: ${text.slice(0, 200)}`);
    else browserLogTruncated = true;
  };

  const t2 = now();
  let renderResult = null;
  try {
    renderResult = await renderMedia({
      serveUrl,
      composition,
      inputProps,
      codec: 'h264',
      browserExecutable: BROWSER,
      outputLocation,
      overwrite: true,
      chromiumOptions: { gl: GL, headless: true },
      timeoutInMilliseconds: DELAY_TIMEOUT_MS,
      onBrowserLog,
      // 调用方没给的仍吃 Remotion 默认（scale 1、png、crf 18、并发 min(8,核/2)）。
      ...(scale !== 1 ? { scale } : {}),
      ...(imageFormat ? { imageFormat, ...(jpegQuality !== null ? { jpegQuality } : {}) } : {}),
      ...(crf !== null ? { crf } : {}),
      ...(concurrency !== null ? { concurrency } : {}),
    });
  } catch (err) {
    const e = new Error(`Remotion 渲染失败：${err?.message?.slice(0, 300) ?? err}`);
    e.status = 500;
    throw e;
  }
  const renderMs = now() - t2;
  const frames = composition.durationInFrames;
  // 版面行可能有两条（母版 + 测量壳），收齐之后按 `pickLayout` 的规矩挑一条。
  // 挑之前先记一句**同一上报者给了两个不同读数**的情况——那才是真不稳（同一探针帧被
  // seek 两遍、却量出不同的版面）。两个**不同**上报者同时出现是设计内的（见 `pickLayout`），
  // 不记账，否则每一个套母版的镜都会带一句无意义的告警。
  const layoutUsed = pickLayout(layoutLines);
  for (const by of ['master', 'shell']) {
    const mine = layoutLines.filter((l) => l.by === by);
    if (mine.length > 1 && new Set(mine.map((l) => JSON.stringify(l))).size > 1) {
      warnings.push(`版面回读出现两遍且内容不一致（${by} 在同一探针帧两次 seek 得到` +
                    '不同的版面），取最后一遍——这条通道的证据链在这一镜上不牢');
    }
  }
  const timings = {
    bundle_ms: bundleMs,
    select_ms: selectMs,
    render_ms: renderMs,
    per_frame_ms: frames ? Number((renderMs / frames).toFixed(1)) : null,
    slowest_frames: (renderResult?.slowestFrames ?? []).slice(0, 3),
  };
  console.log(`[render] ${key} ${frames}帧 bundle=${bundleMs}ms select=${selectMs}ms render=${renderMs}ms ` +
    `每帧≈${timings.per_frame_ms}ms token=${tokens ? '有' : '无'} ` +
    `字段=${fieldsUsed ? `${(fieldsUsed.marks ?? []).length}处/${declaredFields.length}声明` : `无/${declaredFields.length}声明`} ` +
    `版面=${layoutUsed ? (layoutUsed.scanned
      ? `${(layoutUsed.findings ?? []).length}告警/扫${layoutUsed.scanned.texts}字${layoutUsed.scanned.leaders}线`
      : `落空(${String(layoutUsed.reason ?? '无').slice(0, 24)})`) : '无'}`);
  if (browserLogTruncated) warnings.push('[browser] 日志超过 20 条，其余已略');
  if (!tokens) warnings.push('闸⑤ 回读落空：这一镜没读到母版 token，跨镜一致性比对对它跳过');
  // 声明了字段却没回读到行＝这一镜一处 `<F>` 标注都没标到（或母版那侧的通道没跑起来）。
  // 闸⑥ 对「没有盒」的处理是记 no_geometry 而不是判红，所以这句话只能在这里说，
  // 否则下游会把「一行都没读到」误读成「读到了且全对上了」。
  if (!fieldsUsed && declaredFields.length) {
    warnings.push(`闸⑥ 回读落空：声明了 ${declaredFields.length} 个字段（${declaredFields.join(', ')}），` +
                  '但这一镜没读回任何 <F> 标注行——字段级命中表对它没有几何，值对账也无从比');
  }

  // 闸⑧ 版面回读：**记账不拦截**。扫到叠字/裁字/悬空引线只写 warning，渲照样出——
  // 判据是几何猜的，宁可漏报误报也不许把一次好渲染打成失败。
  // 三种「没有 findings」必须分开说：没收到行（这一版镜像里的母版压根没有闸⑧）、
  // 收到了但画面带 ref 取不到（reason 在那一行里）、真扫了且干净（不吭声）。
  if (!layoutUsed) {
    warnings.push('闸⑧ 回读落空：这一镜没读回版面行（母版版本里没有这段探针？）。' +
                  '这一镜的版面等于没扫过，下游不许当成「扫过且干净」');
  } else if (!layoutUsed.scanned) {
    warnings.push(`闸⑧ 没扫版面：${String(layoutUsed.reason ?? '未知原因').slice(0, 120)}`);
  } else {
    // 逐条 finding 的人话由 **Python 侧 `code_render.describe_layout` 单源**产出
    // （整片回执与日志都用那一份措辞）。这里只回传结构化的 layout_used + 一条汇总——
    // 两处各拼一遍句子，迟早不一样。
    const scanned = layoutUsed.scanned;
    const n = (layoutUsed.findings ?? []).length;
    if (!layoutUsed.thresholds) {
      warnings.push('闸⑧ 版面行没有 thresholds（母版版本比这条链旧？）——判据取值无法核对');
    }
    if (n === 0) {
      warnings.push(`闸⑧ 版面：扫了 ${scanned.texts} 处文字 / ${scanned.leaders} 条引线，` +
                    `${scanned.drawn} 个画得出的图元，没有命中（这是「扫过且干净」，` +
                    '与上面那两种「压根没扫」不是一回事）');
    } else {
      warnings.push(`闸⑧ 版面：${n} 条告警（扫 ${scanned.texts} 处文字 / ` +
                    `${scanned.leaders} 条引线），逐条见 layout_used`);
    }
    if ((scanned.dropped ?? 0) > 0) {
      warnings.push(`闸⑧ 版面：单帧 finding 超过上限，母版那边丢了 ${scanned.dropped} 条`);
    }
  }

  // 闸⑭ 动效事件密度：抽帧算相邻差，切成「先后变化段」。
  // 为什么放在 ffprobe 前后无所谓、但**必须在渲染之后**：它判的是成片的像素，
  // 代码里的 `interpolate()` 窗口只是声明，把窗口全写成 [0,0] 的镜照样能「声明有动效」。
  // 抽帧失败时如实记 undetermined，**不许折成通过**——一个拿不到判据的闸闭嘴，
  // 与一个判成「没问题」的闸，在回执上必须是两种样子。
  const motion = motionDensity(outputLocation, {
    execFileSync, fps: composition.fps,
    srcWidth: composition.width, srcHeight: composition.height,
  });
  if (motion.status === 'reject') {
    warnings.push(`闸⑭ 动效事件密度：${motion.reason}`);
  } else if (motion.status === 'undetermined') {
    warnings.push(`闸⑭ 动效事件密度：未判——${motion.reason}`);
  }

  let probed = {};
  try {
    const out = execFileSync('ffprobe', [
      '-v', 'error', '-select_streams', 'v:0',
      '-show_entries', 'stream=nb_frames,width,height,avg_frame_rate',
      '-show_entries', 'format=duration', '-of', 'json', outputLocation,
    ], { encoding: 'utf8', timeout: 15000, killSignal: 'SIGKILL' });
    const j = JSON.parse(out);
    const fps = composition.fps;
    const durSec = Number(j.format?.duration ?? 0) || null;
    probed = {
      // mp4 的 nb_frames 常常压根不写（缺字段，不是 0）——所以缺字段时靠时长折算，
      // 而不是当成「渲了 0 帧」。闸③ 的对账在 Python 侧做，这一层只如实回读。
      nb_frames_field: Number(j.streams?.[0]?.nb_frames ?? 0) || null,
      duration_sec: durSec,
      duration_frames: durSec ? Math.round(durSec * fps) : null,
      width: j.streams?.[0]?.width ?? null,
      height: j.streams?.[0]?.height ?? null,
    };
  } catch (err) {
    warnings.push(`ffprobe 回读失败：${err?.message?.slice(0, 120) ?? err}`);
  }

  return {
    ok: true,
    cache_key: key,
    artifact: `/artifact/${encodeURIComponent(fileKey)}`,
    // 调用方按这两个数核对产物：草稿档的 mp4 尺寸是 `composition.* × scale`，
    // 拿成片宽高去对一份 0.4 倍的片子会误判成「渲染尺寸错了」。
    draft,
    render_scale: scale,
    ...(concurrency !== null ? { render_concurrency: concurrency } : {}),
    bytes: (await fs.stat(outputLocation)).size,
    composition: {
      fps: composition.fps,
      durationInFrames: composition.durationInFrames,
      width: composition.width,
      height: composition.height,
    },
    probed,
    timings,
    tokens,
    // 闸⑥ 的原料：母版在探针帧回读的 `{frame, marks:[{name, value, box}]}`。
    // 这一层只如实搬回，比对（值等不等、盒归一化）在 Python 侧做——像素口径在这里，
    // 契约口径在那里，两处各自演进。没读到就是 null，不硬造一个空表。
    fields_used: fieldsUsed,
    declared_fields: declaredFields,
    // 闸⑧ 的原料：母版在同一探针帧回读的 `{frame, findings, scanned, thresholds}`。
    // 与 fields_used 同一条道理——没读到就是 null，这一层的 warnings 里已经如实写了「落空」，
    // 下游不许把 null 读成「扫过且干净」。
    layout_used: layoutUsed,
    // 闸⑭ 动效事件密度的原料：抽帧曲线的**逐格差**、切出来的**先后变化段**、
    // 以及那一组阈值。判（段数 ≥2、相邻 ≥0.4 s）由 Python 侧做——那里才是闸的出口，
    // 而这一层只负责把像素变成可数的东西。阈值随行是为了让红的时候能核对
    // 「它按哪条线判的」，不是让人在回执里改的。
    motion,
    warnings,
  };
  } finally {
    releaseSlot();
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
  try {
    if (req.method === 'GET' && url.pathname === '/healthz') {
      return send(res, 200, { ok: true, node: process.version, remotion: remotionVersion,
                              chromium: await chromiumVersion(), gl: GL,
                              ...(await versions()) });
    }
    // ---- 用户沉淀的模板：写入 / 列举 / 删除 / 单独 lint ----
    // 这一组口只做「文件落地 + 语法/白名单闸」，**不判模板画得好不好**——那是产品侧
    // 的预览与探针的事。镜头代码能不能 import 到它，由 lint 的 generated 前缀放行，
    // 名字不合法在写盘前就 400。
    if (req.method === 'GET' && url.pathname === '/templates') {
      await fs.mkdir(GENERATED_DIR, recursiveDir);
      const names = (await fs.readdir(GENERATED_DIR)).filter((n) => n.endsWith('.tsx'));
      const templates = {};
      for (const file of names) {
        const source = await fs.readFile(path.join(GENERATED_DIR, file), 'utf8');
        templates[file.replace(/\.tsx$/, '')] =
          createHash('sha256').update(source).digest('hex').slice(0, 16);
      }
      return send(res, 200, { ok: true, templates });
    }
    if (req.method === 'POST' && url.pathname === '/templates') {
      const body = await readBody(req);
      const name = String(body.name ?? '');
      const source = String(body.source ?? '');
      if (!TEMPLATE_NAME_RE.test(name)) {
        return send(res, 400, { ok: false, error: `模板名不合法：${name.slice(0, 40)}（小写字母开头，字母数字与 -，2~41 位）` });
      }
      if (!source.trim()) return send(res, 400, { ok: false, error: '模板源码为空' });
      if (Buffer.byteLength(source, 'utf8') > TEMPLATE_MAX_BYTES) {
        return send(res, 400, { ok: false, error: `模板源码超过 ${TEMPLATE_MAX_BYTES} 字节` });
      }
      const lint = await lintTemplateCode(source);
      if (!lint.ok) return send(res, 400, { ok: false, error: '模板 lint 未通过', errors: lint.errors });
      await fs.mkdir(GENERATED_DIR, recursiveDir);
      const target = path.join(GENERATED_DIR, `${name}.tsx`);
      const tmp = `${target}.tmp-${process.pid}`;
      await fs.writeFile(tmp, source, 'utf8');
      await fs.rename(tmp, target);        // 原子替换：镜头不会读到半份模板
      const hash = createHash('sha256').update(source).digest('hex').slice(0, 16);
      console.log(`[template] ${name} ${Buffer.byteLength(source, 'utf8')}B ${hash}`);
      return send(res, 200, { ok: true, name, hash });
    }
    if (req.method === 'DELETE' && url.pathname.startsWith('/templates/')) {
      const name = decodeURIComponent(url.pathname.slice('/templates/'.length));
      if (!TEMPLATE_NAME_RE.test(name)) return send(res, 400, { ok: false, error: '模板名不合法' });
      await fs.rm(path.join(GENERATED_DIR, `${name}.tsx`), { force: true });
      return send(res, 200, { ok: true, name });
    }
    if (req.method === 'POST' && url.pathname === '/lint-template') {
      const body = await readBody(req);
      return send(res, 200, await lintTemplateCode(String(body.code ?? '')));
    }
    // ---- 脚本产出的素材：写入 / 列举 ----
    // 只做「文件落地 + 名字与大小校验」，不判这张图好不好看——那是人看片的事。
    if (req.method === 'GET' && url.pathname === '/assets') {
      await fs.mkdir(ASSETS_DIR, recursiveDir);
      const names = await fs.readdir(ASSETS_DIR);
      return send(res, 200, { ok: true, assets: names });
    }
    if (req.method === 'POST' && url.pathname === '/assets') {
      const body = await readBody(req);
      const name = String(body.name ?? '').toLowerCase();
      if (!ASSET_NAME_RE.test(name)) {
        return send(res, 400, {
          ok: false,
          error: `素材名不合法：${name.slice(0, 40)}（小写字母数字开头，可含 . _ -，≤61 位）`,
        });
      }
      let buf;
      try {
        buf = Buffer.from(String(body.b64 ?? ''), 'base64');
      } catch {
        return send(res, 400, { ok: false, error: 'b64 解不出来' });
      }
      if (!buf.length) return send(res, 400, { ok: false, error: '素材是空的' });
      if (buf.length > ASSET_MAX_BYTES) {
        return send(res, 400, {
          ok: false,
          error: `素材超过 ${Math.round(ASSET_MAX_BYTES / 1024 / 1024)}MB`,
        });
      }
      await fs.mkdir(ASSETS_DIR, recursiveDir);
      const target = path.join(ASSETS_DIR, name);
      const tmp = `${target}.tmp-${process.pid}`;
      await fs.writeFile(tmp, buf);
      await fs.rename(tmp, target);      // 原子替换：镜头不会读到半张图
      console.log(`[asset] ${name} ${buf.length}B`);
      return send(res, 200, { ok: true, name, bytes: buf.length, path: `assets/${name}` });
    }
    if (req.method === 'POST' && url.pathname === '/lint') {
      const body = await readBody(req);
      return send(res, 200, await lintShotCode(body.code ?? '', declaredFrom(body)));
    }
    if (req.method === 'POST' && url.pathname === '/render-shot') {
      const body = await readBody(req);
      // 打包在 renderShot 里串行、渲染限路并发——这里不再整条串行。
      const result = await renderShot(body);
      return send(res, 200, result);
    }
    if (req.method === 'POST' && url.pathname === '/render-stills') {
      const body = await readBody(req);
      return send(res, 200, await renderStills(body));
    }
    // 草稿档产物只喂闸、不留存：验收循环一镜要烧十几遍，靠这个口在批量探针收尾时
    // 把 /out 里的 `<key>.draft.mp4` 扫干净（成片档的 `<key>.mp4` 一个都不碰）。
    if (req.method === 'POST' && url.pathname === '/purge-drafts') {
      const names = (await fs.readdir(OUT)).filter((n) => n.endsWith('.draft.mp4'));
      let bytes = 0;
      for (const n of names) {
        const f = path.join(OUT, n);
        bytes += (await fs.stat(f)).size;
        await fs.rm(f, { force: true });
      }
      return send(res, 200, { ok: true, removed: names.length, bytes });
    }
    if (req.method === 'GET' && url.pathname.startsWith('/artifact/')) {
      const key = decodeURIComponent(url.pathname.slice('/artifact/'.length));
      const file = path.join(OUT, `${path.basename(key)}.mp4`);   // basename：不给路径穿越留口
      if (!file.startsWith(OUT)) return send(res, 400, { ok: false, error: '非法键' });
      const data = await fs.readFile(file);
      res.writeHead(200, { 'content-type': 'video/mp4', 'content-length': data.length });
      return res.end(data);
    }
    return send(res, 404, { ok: false, error: `未知路径 ${url.pathname}` });
  } catch (err) {
    return send(res, err.status ?? 500, {
      ok: false,
      error: String(err?.message ?? err),
      ...(err.lint ? { errors: err.lint } : {}),
    });
  }
});

// 一只崩掉的浏览器标签页不该带走整个渲染服务。
//
// Remotion 的 WebSocket 关闭回调会在请求**已经失败之后**再 reject 一个
// `ProtocolError (Target.closeTarget): Target closed`；那一枪没有任何人 await，
// Node 默认把未捕获的 rejection 当致命错误、直接退出进程。2026-10-21 真机
// run `1ff6f17ee840` 就是这样：6 路并发渲预览静帧，第一镜的标签页一死，
// 服务退出，余下 41 镜全部 `Connection refused`，确认卡上一张图都没有。
//
// 这里只记日志、丢掉那只共享浏览器让下一个请求重开；真正该失败的那次请求
// 已经在自己的 catch 里失败过了。`uncaughtException` 同样兜住：渲染服务是
// 常驻的，退出一次会让之后每一镜都失败，代价远大于让一个坏请求自己报错。
process.on('unhandledRejection', (err) => {
  console.error('[unhandledRejection]', err?.stack ?? err);
  if (/Target closed|Target\.closeTarget/.test(String(err?.message ?? err))) {
    dropSharedChrome().catch(() => {});
  }
});
process.on('uncaughtException', (err) => {
  console.error('[uncaughtException]', err?.stack ?? err);
});

await fs.mkdir(OUT, { recursive: true });
server.listen(PORT, () => {
  console.log(`[remotion] listening :${PORT} gl=${GL} browser=${BROWSER}`);
});
