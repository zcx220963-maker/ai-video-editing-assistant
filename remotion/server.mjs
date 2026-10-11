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
 * 2. **同一时刻只渲一镜**。无头 WebGL 走软件渲染，并发会把容器打到 OOM，
 *    而 OOM 现场比排队慢一点难查得多。
 */
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { lintShotCode } from './lint.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(ROOT, 'out');
const recursiveDir = { recursive: true };
const SHOT_FILE = path.join(ROOT, 'src', 'user-shots', 'shot.tsx');
const PORT = Number(process.env.PORT || 8010);
// 无 GPU 的容器里实测：angle/egl/swiftshader 三档都拿不到 WebGL 上下文（three.js 镜
// 直接 500），只有 swangle 画得出带光影的几何体。改这一档等于决定 3D 通道在不在。
const GL = process.env.DG_GL || 'swangle';
const BROWSER = process.env.DG_BROWSER || '/usr/bin/chromium';

const require = createRequire(import.meta.url);
const remotionVersion = require('remotion/package.json').version;

let remotionApi = null;
async function loadRemotion() {
  if (remotionApi) return remotionApi;
  // 具名导出：这一版 @remotion/renderer 没有一个叫 renderer 的命名空间，
  // 解构 { renderer } 会得到 undefined，取它的方法时进程直接没了。
  const [{ bundle }, { selectComposition, renderMedia }] = await Promise.all([
    import('@remotion/bundler'),
    import('@remotion/renderer'),
  ]);
  remotionApi = { bundle, selectComposition, renderMedia };
  return remotionApi;
}

/** 串行渲染闸：一次只放一镜进去。 */
let chain = Promise.resolve();
const serialize = (fn) => {
  const run = chain.then(fn, fn);
  chain = run.then(() => {}, () => {});
  return run;
};

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
 *  母版 = `<FilmLayout>` + token。改这两个文件＝全片换像素但不改任何一镜的代码，
 *  不进键就会命中旧母版渲出的切片。lockfile 同理：换一版 three.js 也是换像素。
 *
 *  **每次现读，不缓存。** 进程内缓存＝`docker cp` 换过母版后 /healthz 还在报旧哈希，
 *  于是新母版渲出的片子拿的还是旧键——命中缓存把旧切片发给新请求，正是这条链最贵的
 *  那种错（看不出来，且只在第二次之后出现）。两个小文件的读盘钱不买这个错。
 *
 *  读不到就抛：宁可 /healthz 500，也不许报一个「空的但看起来能用」的哈希。 */
async function versions() {
  const [layout, tokens, lock] = await Promise.all([
    fs.readFile(path.join(ROOT, 'src', 'FilmLayout.tsx')),
    fs.readFile(path.join(ROOT, 'src', 'tokens.ts')),
    fs.readFile(path.join(ROOT, 'package-lock.json')),
  ]);
  return { master_hash: sha16(Buffer.concat([layout, tokens])),
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
  const outputLocation = path.join(OUT, `${key}.mp4`);

  const { bundle, selectComposition, renderMedia } = await loadRemotion();
  await fs.mkdir(path.dirname(SHOT_FILE), recursiveDir);
  await fs.mkdir(OUT, recursiveDir);
  await fs.writeFile(SHOT_FILE, code, 'utf8');

  const warnings = [];
  let tokens = null;
  const now = () => Number(process.hrtime.bigint() / 1000000n);
  const t0 = now();
  const serveUrl = await bundle({
    entryPoint: path.join(ROOT, 'src', 'index.ts'),
    publicDir: false,
    webpackOverride: (cfg) => ({
      ...cfg,
      resolve: {
        ...cfg.resolve,
        alias: {
          ...(cfg.resolve?.alias ?? {}),
          '@dg/film': path.join(ROOT, 'src', 'FilmLayout.tsx'),
          '@dg/tokens': path.join(ROOT, 'src', 'tokens.ts'),
        },
      },
    }),
  });
  const bundleMs = now() - t0;

  const t1 = now();
  const composition = await selectComposition({
    serveUrl,
    id: 'shot',
    inputProps,
    browserExecutable: BROWSER,
    chromiumOptions: { gl: GL, headless: true },
  });
  const selectMs = now() - t1;

  // 闸⑤ 的原料：母版渲染时会 console.log 一行 `DG_TOKENS:<json>`，
  // 这一版 renderer 没有 onBrowserOpen，而 onBrowserLog 是公开口、且只在真渲染时经过，
  // 读到的就是这一镜实际用上的值（不是根页面里母版的默认值）。
  const TOKEN_MARKER = 'DG_TOKENS:';   // 与 src/tokens.ts 的 TOKEN_LOG_MARKER 同名，两处一起改
  const FIELD_MARKER = 'DG_FIELDS:';   //   与 src/tokens.ts 的 FIELD_LOG_MARKER 同名，两处一起改
  const LAYOUT_MARKER = 'DG_LAYOUT:';  //   与 src/tokens.ts 的 LAYOUT_LOG_MARKER 同名，两处一起改
  let browserLogTruncated = false;
  let fieldsUsed = null;
  let layoutUsed = null;
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
        layoutUsed = JSON.parse(text.slice(lat + LAYOUT_MARKER.length));
      } catch (err) {
        warnings.push(`版面行解析失败：${String(err).slice(0, 120)}`);
      }
      return;   // 版面这一路是记账用的 warning，重试那一遍更稳，直接覆盖，不记不一致
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
      onBrowserLog,
    });
  } catch (err) {
    const e = new Error(`Remotion 渲染失败：${err?.message?.slice(0, 300) ?? err}`);
    e.status = 500;
    throw e;
  }
  const renderMs = now() - t2;
  const frames = composition.durationInFrames;
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
    artifact: `/artifact/${encodeURIComponent(key)}`,
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
    warnings,
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
  try {
    if (req.method === 'GET' && url.pathname === '/healthz') {
      return send(res, 200, { ok: true, node: process.version, remotion: remotionVersion,
                              chromium: await chromiumVersion(), gl: GL,
                              ...(await versions()) });
    }
    if (req.method === 'POST' && url.pathname === '/lint') {
      const body = await readBody(req);
      // 与 render-shot 内部那一遍共用 declaredFrom：否则单独调 /lint 的调用方拿到的是
      // 「三条字段闸根本没开」的绿，而渲染口同一份代码却是红的——两条口必须同判。
      return send(res, 200, await lintShotCode(body.code ?? '', declaredFrom(body)));
    }
    if (req.method === 'POST' && url.pathname === '/render-shot') {
      const body = await readBody(req);
      const result = await serialize(() => renderShot(body));
      return send(res, 200, result);
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

await fs.mkdir(OUT, { recursive: true });
server.listen(PORT, () => {
  console.log(`[remotion] listening :${PORT} gl=${GL} browser=${BROWSER}`);
});
