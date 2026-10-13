// 「随镜像发的公共命名空间」接进按镜 publicDir 的那一步：先证红，再证绿。
//
// 要判的是 `staticFile('fonts/…')` 在**渲染期**解不解得开。渲染期 `staticFile` 的根
// 是这一镜的 publicDir（`public/assets/<镜号>/`），而字体住在 `public/fonts/`——
// 不接这一步，每一个镜头都会因为母版加载字体而 404（实测过一次：闸⑭ 的真渲探针
// 两支夹具都拿到 `Failed to load font "/public/fonts/NotoSansSC-025.woff2": 404`）。
//
// 为什么这一支能在毫秒里判掉，而不是靠真渲：判据就是「软链接上之后，
// `<镜号>/fonts/<片>` 读不读得到」。读得到 == 静态文件服务读得到（同一份字节、
// 同一个 realpath）。真渲那一侧由 `tests/motion-probe.mjs` 的 B 半负责。
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { readFileSync } from 'node:fs';

const here = (p) => new URL(p, import.meta.url);
let fail = 0, n = 0;
const check = (cond, label) => {
  n++;
  console.log(`  ${cond ? '✓' : '✗'} ${label}`);
  if (!cond) fail++;
};
const readOrNull = async (p) => { try { return await fs.readFile(p); } catch { return null; } };

// 用例都跑在临时根上（`DG_PUBLIC_ROOT`）——真 `public/assets/` 是运行时投递目标，
// 一条测试不该往里写东西。真字节那一支在最后单独用真根跑，跑完自己清掉。
const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'dg-shipped-'));
process.env.DG_PUBLIC_ROOT = tmp;
const { linkShippedNamespaces, shotPublicDir, PUBLIC } =
  await import('../assetgate.mjs?tmp-root');

const STUB = Buffer.from('stub-font-bytes'.repeat(8));
await fs.mkdir(path.join(tmp, 'assets'), { recursive: true });
await fs.mkdir(path.join(tmp, 'fonts'), { recursive: true });
await fs.writeFile(path.join(tmp, 'fonts', 'NotoSansSC-000.woff2'), STUB);
// 第二个命名空间：这一步不许只认识 `fonts`——它判的是「assets/ 以外的一切」。
await fs.mkdir(path.join(tmp, 'textures', 'live'), { recursive: true });
await fs.writeFile(path.join(tmp, 'textures', 'live', 'paper.png'), STUB);

console.log('=== 绿：接上之后 staticFile 的根读得到 ===');
{
  const got = await linkShippedNamespaces('shot1');
  check([...got].sort().join(',') === 'fonts,textures',
        `接上了 assets/ 以外的全部命名空间（实得 [${got.join(', ')}]）`);
  const viaLink = await readOrNull(
    path.join(shotPublicDir('shot1'), 'fonts', 'NotoSansSC-000.woff2'));
  check(viaLink !== null && viaLink.equals(STUB),
        'staticFile("fonts/NotoSansSC-000.woff2") 的落脚点读得到同一份字节');
  const deep = await readOrNull(
    path.join(shotPublicDir('shot1'), 'textures', 'live', 'paper.png'));
  check(deep !== null && deep.equals(STUB),
        '嵌套子目录（textures/live/）也通——软链接的是整棵子树');
  const selfLink = await fs.lstat(path.join(shotPublicDir('shot1'), 'assets'))
    .then(() => true, () => false);
  check(!selfLink, '不把 assets/ 自己接进镜目录（否则就成了 assets/<镜号>/assets/ 的死环）');
  const again = await linkShippedNamespaces('shot1');
  check(again.length === 0, `重复调用不再动手（实得 [${again.join(', ')}]）`);
}

console.log('\n=== 红：源头缺了，同一个断言必须塌 ===');
{
  await fs.rm(path.join(tmp, 'fonts'), { recursive: true, force: true });
  const got = await linkShippedNamespaces('shot2');
  check(!got.includes('fonts'), `源头没有 fonts/ 时不会凭空接出来（实得 [${got.join(', ')}]）`);
  const missing = await readOrNull(
    path.join(shotPublicDir('shot2'), 'fonts', 'NotoSansSC-000.woff2'));
  check(missing === null,
        '上面那条绿断言在这一版确实塌了——它不是一个恒真的检查');
  await fs.mkdir(path.join(tmp, 'fonts'), { recursive: true });
  await fs.writeFile(path.join(tmp, 'fonts', 'NotoSansSC-000.woff2'), STUB);
  const back = await linkShippedNamespaces('shot3');
  check(back.includes('fonts'), `源头补回来之后又接得上（实得 [${back.join(', ')}]）`);
}

console.log('\n=== 真字节：镜目录里的字体就是随镜像发的那 101 片 ===');
{
  delete process.env.DG_PUBLIC_ROOT;
  const real = await import('../assetgate.mjs?real-root');
  const sid = '__shipped_namespace_probe__';
  const dir = real.shotPublicDir(sid);
  try {
    const shipped = await fs.readdir(path.join(real.PUBLIC, 'fonts'));
    // 注册表（由 scripts/fetch_fonts.mjs 生成）说几片，盘上就得是几片：
    // 少一片就是一个 unicode 段静默回落，而这条链一路上在防的就是静默回落。
    const gen = readFileSync(here('../src/fonts.notosanssc.generated.ts'), 'utf8');
    const declared = (gen.match(/\{file: "/g) ?? []).length;
    check(declared > 0 && declared === shipped.length,
          `注册表 ${declared} 片 == public/fonts/ 盘上 ${shipped.length} 片`);
    const got = await real.linkShippedNamespaces(sid);
    check(got.includes('fonts'), `真根上接上了 fonts（实得 [${got.join(', ')}]）`);
    const viaLink = await fs.readdir(path.join(dir, 'fonts'));
    check(viaLink.length === shipped.length,
          `经镜目录看到的还是 ${shipped.length} 片（实得 ${viaLink.length}）`);
    const a = await fs.readFile(path.join(dir, 'fonts', 'NotoSansSC-000.woff2'));
    const b = await fs.readFile(path.join(real.PUBLIC, 'fonts', 'NotoSansSC-000.woff2'));
    check(a.equals(b), `NotoSansSC-000.woff2 两处字节一致（${a.length} B）`);
  } finally {
    // 先逐个 unlink 掉接上去的软链，再删目录：`unlink` 删的**是链接本身**，
    // 绝不会顺着它把 public/fonts/ 里那 101 片真字节带走。
    for (const name of await fs.readdir(dir).catch(() => [])) {
      await fs.unlink(path.join(dir, name)).catch(() => {});
    }
    await fs.rmdir(dir).catch(() => {});
  }
}

await fs.rm(tmp, { recursive: true, force: true });
console.log(`\n${fail === 0 ? 'PASS' : `FAIL ${fail}`}  ${n - fail}/${n}`);
process.exitCode = fail === 0 ? 0 : 1;
