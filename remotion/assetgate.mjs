/**
 * 闸⑬ 与「按镜投递」的落盘约定（§4-5「投递方向唯一」的渲染侧那一半）。
 *
 * 单独成模块的理由与 `lint.mjs` 一样：**闸要能从测试里直接调**。
 * `server.mjs` 一被 import 就会起监听，测试里没法用它去验一条判据——
 * 于是判据住在这里，`server.mjs` 只负责把它接到口上。
 *
 * 目录约定（Python 侧 `code_render._stage_shot_assets` 投递的目标）：
 *
 *     <root>/public/assets/<镜号>/<名字>          ← 素材字节
 *     <root>/public/assets/<镜号>/_manifest.json  ← {sid, items:[{name,sha16,bytes,ext,motion,canvas_check}]}
 *
 * 一镜一个子目录，是因为服务端的 `/stage-assets` 对同名文件走幂等短路（存在即回 ok、
 * 不比对字节）：两镜撞名时，平铺布局会让后投的那一镜静默拿到先投的字节。
 *
 * `DG_PUBLIC_ROOT` 只给测试用：把 public/ 指到临时目录，免得测一条判据要先
 * 污染真容器的 public/。
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { judgeAsset, fileExt } from './imagecheck.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
export const PUBLIC = process.env.DG_PUBLIC_ROOT
  ? path.resolve(process.env.DG_PUBLIC_ROOT)
  : path.join(ROOT, 'public');

const sha16 = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 16);

/** 一镜一个子目录的那一层。`public/` 下**除它以外**的目录都是随镜像发的公共
 *  命名空间——任何一镜都能用、也不需要谁声明（目前只有 `fonts/`）。 */
export const SHOT_LOCAL_DIR = 'assets';

/** 这一镜的素材落在哪儿。镜号缺失时退回 `public/`（手写的裸请求仍可用，
 *  只是拿不到按镜隔离）。 */
export function shotPublicDir(sid) {
  return sid ? path.join(PUBLIC, SHOT_LOCAL_DIR, String(sid)) : PUBLIC;
}

/**
 * 把随镜像发的公共命名空间接进**这一镜的** publicDir。
 *
 * 为什么必须有这一步：`bundle({publicDir})` 只把一个目录铺成渲染期 `staticFile()`
 * 的根（Remotion 把 publicDir 整份拷进打包目录的 `public/`，见
 * `@remotion/bundler/dist/copy-dir.js`）。而这一镜的 publicDir 是
 * `public/assets/<镜号>/`——素材必须落在**根**上，模型才写得成 `staticFile("<name>")`
 * （SKILL.md:378）——字体却在 `public/fonts/`。两个根，Remotion 只认一个。
 *
 * 不接这一步的后果是**每一镜都 500**：母版 `FilmLayout` 侧效 import `@dg/fonts`，
 * 于是每个镜头都加载字体，而 `staticFile('fonts/…')` 在渲染期是 404。实测过一次
 * （闸⑭ 的真渲探针）：两支夹具都拿到
 * `Failed to load font "/public/fonts/NotoSansSC-025.woff2": 404 Not Found`。
 * 判据那边「渲染没成功」进的是未判清单，正是靠它才没被当成通过。
 *
 * 用软链而不是拷贝：字体是 101 片 4.5 MB 的构建产物，每渲一镜复制一遍既慢、
 * 又让同一份字节在盘上存 N 份（还要随 session_gc 一起清）。Remotion 的 `copyDir`
 * 会把软链按 **realpath** 转投到打包目录（`symlink(await realpath(srcPath), dest)`），
 * 而 realpath 在容器里指得到，所以静态文件服务照读不误。
 *
 * 没有软链权限时（Windows 宿主、某些挂载）这里会抛——**就该抛**：接不上就等于
 * 字体没加载，那一次渲染是坏的，宁可在这行失败也不要渲出一张字体不对的片子。
 * 返回接上了哪几个名字，供日志留痕。
 */
export async function linkShippedNamespaces(sid) {
  const dir = shotPublicDir(sid);
  if (dir === PUBLIC) return [];        // 没给镜号：publicDir 就是 public/ 本身，无需接
  await fs.mkdir(dir, { recursive: true });
  let entries;
  try {
    entries = await fs.readdir(PUBLIC, { withFileTypes: true });
  } catch {
    return [];                          // public/ 还没有（例如测试指向的临时根）
  }
  const out = [];
  for (const e of entries) {
    if (e.name === SHOT_LOCAL_DIR) continue;
    const link = path.join(dir, e.name);
    try {
      await fs.lstat(link);             // lstat：坏的软链也算「在了」，不重复接
      continue;
    } catch { /* 不在，接上去 */ }
    try {
      // Windows 上建**目录**软链要开发模式/管理员权限，普通账户是 EPERM；`'junction'`
      // 那一种不需要，且它只接受绝对路径——正好是我们给的形态。Linux 忽略这个参数。
      await fs.symlink(path.join(PUBLIC, e.name), link,
                       process.platform === 'win32' ? 'junction' : undefined);
    } catch (err) {
      throw new Error(`把 public/${e.name}/ 接进 ${dir} 失败（${err?.code ?? err?.message}）：`
        + '这一步接不上，这一镜渲染时母版加载字体就是 404。'
        + '（Windows 宿主建目录软链需要开发模式或管理员权限；渲染容器是 Linux，不受影响。）');
    }
    out.push(e.name);
  }
  return out;
}

/** 读这一镜的投递 manifest。返回**整份 items**
 *  （`{name,sha16,bytes,ext,motion,canvas_check}`）；`null` = 拿不到这份账
 *  （镜号没给、或 manifest 读不着）。
 *
 *  null 与 [] 是两个意思：[] 是「这一镜确实投过，一件都没成」，而 null 是
 *  「这一镜的投递状态未知」——lint 与闸⑬ 对这两者的态度必须不同，否则手写请求
 *  或者补跑 lint 的那条路会因为「没看见 manifest」被判成「没投递」。
 *  闸⑬ 要 sha16 与 ext，所以账在这一层留整份原样，不在下面截成名字。
 *
 *  **这一层按名字取栏，不做「字段集全等」的判断。** 判据不许对新增栏过敏：
 *  `canvas_check` 是 R12-T59 第二半加的（揭示画布 vs 素材真实尺寸，由 Python 侧
 *  `code_render._check_reveal_canvas` 写入），JS 侧今天没人读它；将来再加栏也一样。
 *  真要冻结字段集，那一份断言在 Python 侧（`tests/test_asset_delivery.py`），
 *  与写入处同一门语言——两边各冻一份就是两份真相。 */
export async function readAssetManifestItems(sid) {
  if (!sid) return null;
  try {
    const raw = await fs.readFile(path.join(shotPublicDir(sid), '_manifest.json'), 'utf8');
    const items = JSON.parse(raw)?.items;
    if (!Array.isArray(items)) return null;
    return items.filter((i) => i && typeof i === 'object');
  } catch {
    return null;
  }
}

/** 只要名字的那一路（闸① 的 `staticfile-asset-check` 用）。 */
export async function readAssetManifest(sid) {
  const items = await readAssetManifestItems(sid);
  if (items === null) return null;
  return items.map((i) => String(i?.name ?? '')).filter(Boolean);
}

/** 闸⑬ 资产存在且非纯色：**投递腿落盘的字节**够不够当画面主体用。
 *
 *  判的是盘上的字节，不是响应。三件事各自出账：
 *    ① 存在：manifest 里记了、盘上没有 → 拒（「manifest 有记录但文件缺」）；
 *    ② 对得上：盘上字节现算的 sha16 必须等于 manifest 里登记的那个 ——
 *       不等的意思是「投上去的」与「账上写的」不是同一份字节；
 *    ③ 够用：尺寸不是 1×1、主色至少两档（判据与措辞在 `imagecheck.mjs` 单源）。
 *
 *  整体三态与闸⑪ 同形：`rejected`（拒，调用方抛 400）/ `undetermined`（这一档没判，
 *  D-未判不绿，**不许**折算成 ok）/ `ok`。空 manifest ＋ 闸① 已过 ＝ 这一镜本来
 *  就没有素材要判，那是 ok 而不是 undetermined。
 *
 *  跑在熔合之前一步：字节在 `/stage-assets` 那一刻就落在 `public/assets/<sid>/` 上，
 *  此后渲染不再动它，所以这里读到的与烧完读到的**是同一批字节**——而一次拒掉
 *  能省下一整镜的烧渲染。（契约把⑬ 记在「烧之后」，判据本身不依赖烧，故前移；
 *  这一处偏差登记在 §9。） */
export async function gateAssets(sid) {
  if (!sid) {
    return { status: 'undetermined', items: [],
             reason: '请求里没有 shotId——投递账看不见，闸⑬ 这一镜未判' };
  }
  const items = await readAssetManifestItems(sid);
  if (items === null) {
    return { status: 'undetermined', items: [],
             reason: `读不到 ${path.join('public/assets', String(sid), '_manifest.json')}——`
                     + '这一镜的投递状态未知，闸⑬ 未判（不等于通过）' };
  }
  if (!items.length) {
    return { status: 'ok', items: [], reason: '这一镜没有投递素材，没有可判的东西' };
  }
  const base = shotPublicDir(sid);
  const out = [];
  for (const it of items) {
    const name = String(it.name ?? '');
    const rec = { name, declared_sha16: String(it.sha16 ?? ''), ext: String(it.ext ?? '') };
    const file = path.join(base, name);
    if (!file.startsWith(base)) {                        // 名字里带 ../ 的
      out.push({ ...rec, verdict: 'reject', reason: `素材名越界：${name}` });
      continue;
    }
    let bytes;
    try {
      bytes = await fs.readFile(file);
    } catch {
      out.push({ ...rec, verdict: 'reject',
                 reason: `manifest 里记了 ${name}，盘上没有这个文件（投递那一趟没落盘？）` });
      continue;
    }
    rec.bytes = bytes.length;
    rec.actual_sha16 = sha16(bytes);
    if (rec.declared_sha16 && rec.actual_sha16 !== rec.declared_sha16) {
      out.push({ ...rec, verdict: 'reject',
                 reason: `${name} 盘上字节的 sha16=${rec.actual_sha16} 与 manifest 登记的 `
                         + `${rec.declared_sha16} 不符——写进账的和落到盘的不是同一份字节` });
      continue;
    }
    const j = judgeAsset(bytes, rec.ext || fileExt(name));
    out.push({ ...rec, verdict: j.verdict, reason: j.reason,
               width: j.probe?.width ?? null, height: j.probe?.height ?? null,
               colors: j.probe?.colors ?? null, format: j.probe?.format ?? null });
  }
  const rejected = out.filter((i) => i.verdict === 'reject');
  const undet = out.filter((i) => i.verdict === 'undetermined');
  if (rejected.length) {
    return { status: 'rejected', items: out,
             reason: `闸⑬ 拒 ${rejected.length}/${out.length} 件：`
                     + rejected.map((i) => `${i.name}（${i.reason}）`).join('；') };
  }
  if (undet.length) {
    return { status: 'undetermined', items: out,
             reason: `闸⑬：${undet.length}/${out.length} 件未判（`
                     + undet.map((i) => `${i.name}：${i.reason}`).join('；') + '）' };
  }
  return { status: 'ok', items: out,
           reason: `${out.length} 件素材都真在盘上、sha16 对得上、且至少两档主色` };
}

/** manifest 里登记一件素材（写盘用；真投递那一趟在 Python 侧，这里只给测试与
 *  本地复现用）。 */
export async function writeManifest(sid, items) {
  const dir = shotPublicDir(sid);
  await fs.mkdir(dir, { recursive: true });
  const blob = JSON.stringify({ sid: String(sid), items }, null, 0);
  await fs.writeFile(path.join(dir, '_manifest.json'), blob, 'utf8');
  return { path: path.join(dir, '_manifest.json'), sha16: sha16(Buffer.from(blob)) };
}

export { sha16 };
