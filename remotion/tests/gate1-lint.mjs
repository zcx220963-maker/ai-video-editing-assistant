// 闸① 的先证红再证绿用例：起手模板必须绿，四类违规必须红并带规则名与行列号。
import { readFileSync } from 'node:fs';
import { lintShotCode } from '../lint.mjs';

const template = readFileSync(new URL('../src/user-shots/shot.tsx', import.meta.url), 'utf8');

const bad = `import React from 'react';
import _ from 'lodash';
import { FilmLayout } from '@dg/film';
export const UserShot = () => {
  const t = Math.random();
  const now = Date.now();
  const d = new Date();
  const p = performance.now();
  setTimeout(() => {}, 10);
  requestAnimationFrame(() => {});
  fetch('/x');
  const css = "font-family: 'Inter'; src: url(https://fonts.gstatic.com/x.woff2)";
  return <FilmLayout><div data-t={t} data-n={now} data-d={d} data-p={p}>{css}</div></FilmLayout>;
};
`;

/** 自包含镜：顶层直接是 `<AbsoluteFill>`，不 import 也不包母版。
 *  母版下线那一轮之前这形状是**红**的（`dg/must-wrap-film-layout`），现在是**正解**——
 *  起手模板就是这个形状，闸① 只留「合法镜头」那几条（白名单、导出、动效、字体）。 */
const selfContained = `import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { E } from '@dg/motion';
const _t = E.linear(0);
const Shot = () => {
  const frame = useCurrentFrame();
  const op = interpolate(frame, [0, 20], [0, 1], { extrapolateRight: 'clamp' });
  return <AbsoluteFill style={{ backgroundColor: '#f6f1e4', opacity: op }}><div>hi</div></AbsoluteFill>;
};
export default Shot;
`;

const noExportAtAll = `import React from 'react';
import { FilmLayout } from '@dg/film';
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
`;

const defaultOnly = `import React from 'react';
import { FilmLayout } from '@dg/film';
import { E } from '@dg/motion';
const _t = E.linear(0);
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
export default Shot;
`;

// ---- 字段参数通道（闸① 新增三条规则）----
// 每条规则都要有自己的红：这三条判的是「声明 vs 代码」两头对不对得上，
// 只测绿的那一侧等于没测——规则写错成「永远通过」和写对，打印出来是一模一样的。
const FIELDS_HEAD = `import React from 'react';
import { useCurrentFrame } from 'remotion';
import { FilmLayout, F } from '@dg/film';
import { E } from '@dg/motion';
const _t = E.linear(0);
`;

/** 绿样例：两个字段都真读了、都标在该值自己的位置上。 */
const fieldsOk = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => {
  const frame = useCurrentFrame();
  const { radius } = fields;
  return (
    <FilmLayout text="半径">
      <F name="radius" value={radius}>
        <div style={{ width: radius * 2, height: radius * 2, borderRadius: '50%',
                      background: fields.accent, transform: \`translateY(\${frame}px)\` }} />
      </F>
      <F name="accent" value={fields.accent}><span>色</span></F>
    </FilmLayout>
  );
};
export default Shot;
`;

/** 红：声明了 speed 却一次都没读——界面能改这一栏，画面上一点不变。 */
const fieldsUnused = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径">
    <F name="radius" value={fields.radius}><div style={{ width: fields.radius }} /></F>
  </FilmLayout>
);
export default Shot;
`;

/** 红：代码读了没声明的字段——界面没有控件能改它，闸⑥ 也没有期望值。 */
const fieldsUndeclared = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径"><div style={{ width: fields.magic }} /></FilmLayout>
);
export default Shot;
`;

/** 红：`<F>` 标了一个没声明的名字。 */
const markUndeclared = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径">
    <F name="ghost" value={fields.radius}><div style={{ width: fields.radius }} /></F>
  </FilmLayout>
);
export default Shot;
`;

/** 红：value 写死字面量——闸⑥ 比的是字面量自己，永远相等，证据就没了。 */
const markLiteral = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径">
    <F name="radius" value={40}><div style={{ width: fields.radius }} /></F>
  </FilmLayout>
);
export default Shot;
`;

/** 红：盒标在 radius 上、值取自 accent——一个假指针，改值时动的不是它。 */
const markWrongValue = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径">
    <F name="radius" value={fields.accent}><div style={{ width: fields.radius }} /></F>
  </FilmLayout>
);
export default Shot;
`;

/** 红：name 写成表达式，静态这一层跟不住，这一处标注无法对账。 */
const markDynamicName = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => {
  const k = 'radius';
  return (
    <FilmLayout text="半径">
      <F name={k} value={fields[k]}><div style={{ width: 20 }} /></F>
    </FilmLayout>
  );
};
export default Shot;
`;

/** 绿（让路）：整块用掉 fields，静态判不出读了谁——这时「声明了却没人读」必须闭嘴，
 *  而不是误伤。误伤的后果是这一条闸最后被人整个关掉。 */
const fieldsDynamic = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径">
    <div>{Object.keys(fields).map((k) => <span key={k}>{String(fields[k])}</span>)}</div>
  </FilmLayout>
);
export default Shot;
`;

/** 红（名单漏传不许静默放行）：没传声明名单时，代码里任何 fields.x 都算没声明。 */
const noListGiven = `${FIELDS_HEAD}
const Shot = ({ fields = {} }) => (
  <FilmLayout text="半径"><div style={{ width: fields.radius }} /></FilmLayout>
);
export default Shot;
`;

/** 红：fontFamily 用了不在镜像白名单里的字族——渲染时静默回落。 */
const badFont = `${FIELDS_HEAD}
const Shot = () => <FilmLayout><div style={{ fontFamily: '"SF Pro Display", sans-serif' }}>hi</div></FilmLayout>;
export default Shot;
`;

/** 绿：fontFamily 用了白名单里的字族。 */
const okFont = `${FIELDS_HEAD}
const SANS = '"Noto Sans CJK SC", sans-serif';
const Shot = () => <FilmLayout><div style={{ fontFamily: SANS }}>hi</div></FilmLayout>;
export default Shot;
`;

// ---- webfont：加载即断言（R5-T16）----
// 上面那对样例判的是「名字有没有登记」，这一批判的是「登记的那个字族的字节到底在不在」。
// 第二种失效**比第一种更隐蔽**：loadFont 指向一个不存在的文件时，Remotion 不报错、
// 浏览器默默用回落字族，成片照样出——只有这一条规则会说话。所以四个红样各管一句人话，
// 逼着实现把「指了远端」「放错目录」「文件不在」「格式写错」分成四句不同的话。
const FONT_LOAD = 'dg/font-load-check';

/** 绿（T16 的驱动红→绿）：`TOKENS.fontStack` 第二项那个名字，现在真有人加载了。
 *  在这条实现之前它是红的——镜像 fc-list 里只有 "Noto Sans CJK SC"，
 *  "Noto Sans SC" 在镜像里根本不存在，而**我们自己的母版就在写它**。 */
const webFontOk = `${FIELDS_HEAD}
import { loadFont } from '@remotion/fonts';
import { staticFile } from 'remotion';
loadFont({ family: 'Noto Sans SC', url: staticFile('fonts/NotoSansSC-003.woff2'), format: 'woff2' });
const Shot = () => <FilmLayout><div style={{ fontFamily: '"Noto Sans SC", sans-serif' }}>hi</div></FilmLayout>;
export default Shot;
`;

/** 绿：webfont 字族由**根文件**加载（`src/entry.tsx` 副作用 import `@dg/fonts`），
 *  所以镜头自己不写 loadFont 也不该被误杀——它是「随镜像发的字族」，
 *  不是「这个文件加载的字族」。当初把判据写成「同文件必须 loadFont」的话，
 *  这一条就会红，而它红得没有道理：注册表挂在每一镜都必经的那个根上。
 *  （母版下线之前它挂在 `FilmLayout` 上——那正是「不套母版就没字」的那条暗沟，
 *  随母版一起挪到了根文件。） */
const webFontFromRoot = `${FIELDS_HEAD}
const Shot = () => <div style={{ fontFamily: '"Noto Sans SC", sans-serif' }}>hi</div>;
export default Shot;
`;

/** 红：名字**长得像**我们的字族但不是（白名单是逐字相等，不是前缀）。 */
const webFontNearMiss = `${FIELDS_HEAD}
const Shot = () => <FilmLayout><div style={{ fontFamily: '"Noto Sans SCX", sans-serif' }}>hi</div></FilmLayout>;
export default Shot;
`;

/** 红：url 直接写了远端地址。 */
const fontRemote = `${FIELDS_HEAD}
import { loadFont } from '@remotion/fonts';
loadFont({ family: 'X', url: 'https://fonts.gstatic.com/x.woff2', format: 'woff2' });
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
export default Shot;
`;

/** 红：字体放错目录（不在 public/fonts/ 下）。 */
const fontOutside = `${FIELDS_HEAD}
import { loadFont } from '@remotion/fonts';
import { staticFile } from 'remotion';
loadFont({ family: 'X', url: staticFile('assets/x.woff2'), format: 'woff2' });
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
export default Shot;
`;

/** 红：指着一个 public/fonts/ 里没有的切片。 */
const fontMissing = `${FIELDS_HEAD}
import { loadFont } from '@remotion/fonts';
import { staticFile } from 'remotion';
loadFont({ family: 'X', url: staticFile('fonts/NotoSansSC-999.woff2'), format: 'woff2' });
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
export default Shot;
`;

/** 红：format 与后缀对不上（浏览器会拒收这份字节）。 */
const fontBadFormat = `${FIELDS_HEAD}
import { loadFont } from '@remotion/fonts';
import { staticFile } from 'remotion';
loadFont({ family: 'X', url: staticFile('fonts/NotoSansSC-003.woff2'), format: 'truetype' });
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
export default Shot;
`;

// ---- `staticFile` 只许指字体（R2-T6 退化后的那一半）----
// 供图通道撤掉后，随镜铺进 `public/` 的只剩 `fonts/`，`staticFile` 还能指的也就剩字体。
// 一条红一条绿：绿的那条证「字体照旧放行」，红的那条证「别的名字真被打回」——
// 只测绿的那一侧等于没测，规则写错成「永远通过」和写对，打印出来一模一样。
const FONT_ONLY_HEAD = `import React from 'react';
import { staticFile } from 'remotion';
import { FilmLayout } from '@dg/film';
import { E } from '@dg/motion';
const _t = E.linear(0);
`;
/** 绿：只指字体——这一镜 public/ 里随镜像发出去的那个命名空间。 */
const staticFontOk = `${FONT_ONLY_HEAD}const Shot = () => <FilmLayout><img src={staticFile("fonts/NotoSansSC-000.woff2")} /></FilmLayout>;
export default Shot;
`;
/** 红：指了一张图。供图通道没了，运行时它根本不在，画出来是空的——摆一张图不叫画。 */
const staticNonFont = `${FONT_ONLY_HEAD}const Shot = () => <FilmLayout><img src={staticFile("logo.png")} /></FilmLayout>;
export default Shot;
`;
// 2026-10-23：`run` 把脚本产出的图推到 `public/assets/`，镜头按 staticFile 取。
// 这条通道必须绿；而 `assets/` 下不是图片的（.json/.html）仍要红。
const staticAsset = `${FONT_ONLY_HEAD}const Shot = () => <FilmLayout><img src={staticFile("assets/ab12cd-sun.svg")} /></FilmLayout>;
export default Shot;
`;
const staticAssetBad = `${FONT_ONLY_HEAD}const Shot = () => <FilmLayout><img src={staticFile("assets/ab12cd-page.html")} /></FilmLayout>;
export default Shot;
`;

// ---- 字幕只许一条（2026-10-23）----
// 「同屏两条字幕」在成片上只表现为「字变粗、发虚」：两套实现的位置都取自同一张
// 几何表，几乎逐像素重合。人工看片极易漏过，所以必须有一条静态规则硬拦。
// 两个入口都要测：只测绿的那一侧，规则写坏成「永远通过」也照样打印 PASS。
const CAPTION_HEAD = `import React from 'react';
import { AbsoluteFill } from 'remotion';
import { E } from '@dg/motion';
import { StyleCaption } from '@dg/lib/style/silhouette-history';
import { CaptionLayer } from '@dg/film';
const _t = E.linear(0);
`;
/** 绿：只有模板那一条。 */
const captionSingle = `${CAPTION_HEAD}const Shot = () => (
  <AbsoluteFill>
    <StyleCaption text="一句话。" />
  </AbsoluteFill>
);
export default Shot;
`;
/** 红：模板那条 + 母版那条，位置几乎重合（真机 s05 的原样）。 */
const captionDouble = `${CAPTION_HEAD}const Shot = () => (
  <AbsoluteFill>
    <StyleCaption text="面包是发的；钱从粮税里出。" />
    <CaptionLayer />
  </AbsoluteFill>
);
export default Shot;
`;

// ---- 通栏矩形不许盖住主体（2026-10-23，只对剪影系生效）----
// 这条规则要同时证明三件事，缺一条就等于没测：
//   ① 剪影系里「通栏带排在最后」必须红；
//   ② 同一段代码不 import 风格包（手写风格）必须绿——这就是「换风格不会被拦」；
//   ③ 声明了 `data-front` 的前景带必须绿——误伤要有一句话就能解掉的口子。
const BAND_HEAD = `import React from 'react';
import { AbsoluteFill, useVideoConfig } from 'remotion';
import { E } from '@dg/motion';
const _t = E.linear(0);
`;
const SIL_IMPORT = "import { SilhouetteStage } from '@dg/lib/style/silhouette-history';\n";
const BAND = '<rect x={0} y={H * 0.4} width={W} height={H * 0.2} fill="#c0392b" opacity={0.9} />';
const SUBJECT = '<path d={`M0 0 L${W} 0 L${W} ${H} Z`} fill="#111111" />';
/** `first`/`last` 决定通栏带画在主体前还是后。 */
const bandShot = (importLine, first, last) => `${BAND_HEAD}${importLine}const Shot = () => {
  const { width: W, height: H } = useVideoConfig();
  return (
    <AbsoluteFill>
      <svg width={W} height={H}>
        <rect x={0} y={H * 0.7} width={W} height={H * 0.3} fill="#000000" />
        ${first}
        ${last}
      </svg>
    </AbsoluteFill>
  );
};
export default Shot;
`;
/** 红：剪影系，通栏带画在最后（真机 s05 的形状）。 */
const bandOver = bandShot(SIL_IMPORT, SUBJECT, BAND);
/** 绿：同一条带，画在主体之前。 */
const bandBehind = bandShot(SIL_IMPORT, BAND, SUBJECT);
/** 绿：一模一样，但不 import 任何风格包（手写风格的镜头）——规则必须让路。 */
const bandNoStyle = bandShot('', SUBJECT, BAND);
/** 绿：剪影系、带在最后，但声明了 `data-front`（前景剪影带）。 */
const bandDeclared = bandShot(SIL_IMPORT, SUBJECT, BAND.replace('/>', 'data-front />'));

// 2026-10-23 真机：12 镜里 6 镜全卡在 band-over-subject 上，每镜重写 3 次仍红。
// 查下来是**地面带被误判**——「最后画一条通栏黑块当地面」是剪影系最正当的画法，
// 它盖住的只是地平线以下。这两个用例把「地面带必须绿」钉住，否则同一个误伤会再来一次。
const groundBandShot = (yExpr) => `${BAND_HEAD}${SIL_IMPORT}const Shot = () => {
  const { width: W, height: H } = useVideoConfig();
  return (
    <AbsoluteFill>
      <svg width={W} height={H}>
        ${SUBJECT}
        <rect x={0} y={${yExpr}} width={W} height={H * 0.16} fill="#000000" />
      </svg>
    </AbsoluteFill>
  );
};
export default Shot;
`;
/** 绿：地面带写在最后，y 是能认出来的下半部（真机那 6 镜的形状）。 */
const bandGroundNum = groundBandShot('H * 0.84');
/** 绿：同一个地面带，y 用变量名写的。 */
const bandGroundIdent = groundBandShot('groundY');

const R = 'dg/fields-declared-unused';
const U = 'dg/fields-undeclared-read';
const M = 'dg/field-mark-not-reading';
const has = (rule, like) => (e) =>
  e.some((x) => x.rule === rule && (!like || x.msg.includes(like)));

const cases = [
  ['起手模板（必须绿）', template, (e) => e.length === 0],
  ['违规样本（必须红，且逐条命中）', bad, null],
  ['自包含镜（不包母版，必须绿）', selfContained, (e) => e.length === 0],
  // 契约（_CODE_DOC）教的是「顶层一个默认导出」，entry.tsx 也必须认这一种，
  // 否则模型照文档写出来的镜头会走到 Remotion 内部报「component 收到 undefined」
  ['只有 export default（契约教的写法，必须绿）', defaultOnly, (e) => e.length === 0],
  ['任何导出都没有（必须红 needs-shot-export）', noExportAtAll,
    (e) => e.some((x) => x.rule === 'dg/needs-shot-export')],
  ['字段：声明与读/标全对上（必须绿）', fieldsOk, (e) => e.length === 0, ['radius', 'accent']],
  ['字段：声明了没人读（必须红 declared-unused）', fieldsUnused, has(R, 'speed'), ['radius', 'accent', 'speed']],
  ['字段：读了没声明的（必须红 undeclared-read）', fieldsUndeclared, has(U, 'magic'), ['radius']],
  ['字段：<F> 标了没声明的名字（必须红）', markUndeclared, has(M, 'ghost'), ['radius', 'accent']],
  ['字段：<F> 的 value 是写死的字面量（必须红）', markLiteral, has(M, 'value 没带上'), ['radius', 'accent']],
  ['字段：<F> 标的不是这个字段自己的值（必须红 假指针）', markWrongValue, has(M, '假指针'), ['radius', 'accent']],
  ['字段：<F> 的 name 是表达式（必须红）', markDynamicName, has(M, '表达式'), ['radius', 'accent']],
  ['字段：整块用掉 fields（判不出就必须让路，必须绿）', fieldsDynamic, (e) => e.length === 0, ['radius', 'accent']],
  ['字段：声明名单漏传（不许静默放行，必须红）', noListGiven, has(U, 'radius')],
  ['字体：非白名单字族（必须红 font-family-whitelist）', badFont,
    (e) => e.some((x) => x.rule === 'dg/font-family-whitelist')],
  ['字体：白名单字族（必须绿）', okFont, (e) => e.length === 0],
  ['字体：随镜像发的 webfont 字族 + 真加载（必须绿）', webFontOk, (e) => e.length === 0],
  ['字体：webfont 字族由根文件加载，镜头不 import 也不误杀（必须绿）', webFontFromRoot,
    (e) => e.length === 0],
  ['字体：名字长得像但不是（必须红 逐字相等不是前缀）', webFontNearMiss,
    has('dg/font-family-whitelist', '不在镜像字体白名单内')],
  ['字体：loadFont 指了远端 URL（必须红 不落静态文件）', fontRemote,
    has(FONT_LOAD, '必须是')],
  ['字体：loadFont 指向 public/fonts/ 之外（必须红 落脚点）', fontOutside,
    has(FONT_LOAD, '落脚点')],
  ['字体：loadFont 指着不存在的切片（必须红 文件不在）', fontMissing,
    has(FONT_LOAD, '没有这个文件')],
  ['字体：loadFont 的 format 与后缀对不上（必须红 格式）', fontBadFormat,
    has(FONT_LOAD, '对不上')],
  ['staticFile：只指字体（必须绿）', staticFontOk, (e) => e.length === 0],
  ['staticFile：指了非字体（必须红 staticfile-font-only）', staticNonFont,
    has('dg/staticfile-font-only', '指的东西不在')],
  ['staticFile：脚本产出的图 assets/*.svg（必须绿）', staticAsset, (e) => e.length === 0],
  ['staticFile：assets 下但不是图片（必须红）', staticAssetBad,
    has('dg/staticfile-font-only', '指的东西不在')],
  ['字幕：只摆模板那一条（必须绿）', captionSingle, (e) => e.length === 0],
  ['字幕：模板 + 母版两条（必须红 caption-double）', captionDouble,
    has('dg/caption-double', '同屏两条字')],
  // 2026-10-23：这条闸降级成 warning。判据是「通栏 + 不透明 + 排在最后」，而静态代码
  // 里判不出主体在哪——剪影系里「最后画一条通栏黑块当地面」是最正当的画法。6 镜被误判、
  // 每镜重写 3 次，一次分镜烧 326 秒。现在它只记账：不进 errors、不拦、回执里看得见。
  ['通栏带：剪影系画在主体之后（只记账，不许拦）', bandOver,
    (e, r) => e.length === 0
      && (r.warnings || []).some((x) => x.rule === 'dg/band-over-subject')],
  ['通栏带：剪影系画在主体之前（必须绿）', bandBehind, (e) => e.length === 0],
  ['通栏带：不 import 风格包（必须绿，别的风格不许拦）', bandNoStyle, (e) => e.length === 0],
  ['通栏带：剪影系但标了 data-front（必须绿）', bandDeclared, (e) => e.length === 0],
  ['通栏带：地面带画在最后 y=H*0.84（必须绿，不许误判）', bandGroundNum, (e) => e.length === 0],
  ['通栏带：地面带画在最后 y=groundY（必须绿，不许误判）', bandGroundIdent, (e) => e.length === 0],
];

let fail = 0;
for (const [name, code, predicate, declared] of cases) {
  const r = await lintShotCode(code, declared);
  console.log(`\n=== ${name} → ok=${r.ok}${declared ? ` declared=[${r.declared}]` : ''}`);
  for (const e of r.errors) console.log(`  [${e.rule}] ${e.line}:${e.column} ${e.msg.slice(0, 70)}`);
  if (predicate && !predicate(r.errors, r)) { console.log('  !! 不符合预期'); fail++; }
  if (name.startsWith('违规') && r.ok) { console.log('  !! 违规样本竟然过了'); fail++; }
}

console.log(fail === 0 ? '\nPASS' : `\nFAIL ${fail}`);
// 构建层末尾用的是本脚本的退出码（`node tests/gate1-lint.mjs`）。只打印不改码
// 等于闸① 的红用例失败了镜像照样烤成功——那层「硬验」就白写了。
process.exitCode = fail === 0 ? 0 : 1;
