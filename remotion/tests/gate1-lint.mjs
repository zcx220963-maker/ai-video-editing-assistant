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

const unwrapped = `import React from 'react';
import { useCurrentFrame } from 'remotion';
export const UserShot = () => <div>{useCurrentFrame()}</div>;
`;

const noWrapImportOnly = `import React from 'react';
import { FilmLayout } from '@dg/film';
export const UserShot = () => <div>hi</div>;
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

/** 绿：webfont 字族由**母版**加载（`@dg/film` 副作用 import `@dg/fonts`），
 *  所以镜头自己不写 loadFont 也不该被误杀——它是「随镜像发的字族」，
 *  不是「这个文件加载的字族」。当初把判据写成「同文件必须 loadFont」的话，
 *  这一条就会红，而它红得没有道理：母版是硬性包裹层，每一镜都带着它。 */
const webFontFromFilm = `${FIELDS_HEAD}
const Shot = () => <FilmLayout><div style={{ fontFamily: '"Noto Sans SC", sans-serif' }}>hi</div></FilmLayout>;
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

/** 素材：staticFile 引用的名字与「本镜声明 + 真投递」两份账逐一对上（R2-T6）。
 *
 *  为什么要两份账、且要**两句不同的**拒绝理由：名字写成代码里没有的那个，是模型自己
 *  编的（改代码就行）；名字声明了、投递腿却没把字节送进容器，是链路的错（改代码没用）。
 *  一句「资产不存在」把这两件事糊在一起，拿到它的人只能靠猜该动哪边。
 */
const ASSET_HEAD = `import React from 'react';
import { staticFile } from 'remotion';
import { FilmLayout } from '@dg/film';
import { E } from '@dg/motion';
const _t = E.linear(0);
`;
/** 绿：引用了声明里有的名字，而且那件确实投到了。 */
const assetOk = `${ASSET_HEAD}const Shot = () => <FilmLayout><img src={staticFile("chart.png")} /></FilmLayout>;
export default Shot;
`;
/** 红：staticFile 的名字不在本镜声明里（模型编的）。 */
const assetUndeclared = `${ASSET_HEAD}const Shot = () => <FilmLayout><img src={staticFile("ghost.png")} /></FilmLayout>;
export default Shot;
`;
/** 红：名字声明了，manifest 里却没有它——声明与投递是两条腿，这条说的是第二条断了。 */
const assetDeclared = `${ASSET_HEAD}const Shot = () => <FilmLayout><img src={staticFile("chart.png")} /></FilmLayout>;
export default Shot;
`;

const R = 'dg/fields-declared-unused';
const U = 'dg/fields-undeclared-read';
const M = 'dg/field-mark-not-reading';
const has = (rule, like) => (e) =>
  e.some((x) => x.rule === rule && (!like || x.msg.includes(like)));

const cases = [
  ['起手模板（必须绿）', template, (e) => e.length === 0],
  ['违规样本（必须红，且逐条命中）', bad, null],
  ['没有母版 import（必须红 noImport）', unwrapped, (e) => e.some((x) => x.rule === 'dg/must-wrap-film-layout')],
  ['有 import 但没包裹（必须红 noWrap）', noWrapImportOnly, (e) => e.some((x) => x.msg.includes('顶层 JSX'))],
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
  ['字体：webfont 字族由母版加载，镜头不 import 也不误杀（必须绿）', webFontFromFilm,
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
];

let fail = 0;
for (const [name, code, predicate, declared] of cases) {
  const r = await lintShotCode(code, declared);
  console.log(`\n=== ${name} → ok=${r.ok}${declared ? ` declared=[${r.declared}]` : ''}`);
  for (const e of r.errors) console.log(`  [${e.rule}] ${e.line}:${e.column} ${e.msg.slice(0, 70)}`);
  if (predicate && !predicate(r.errors)) { console.log('  !! 不符合预期'); fail++; }
  if (name.startsWith('违规') && r.ok) { console.log('  !! 违规样本竟然过了'); fail++; }
}

// 素材那三条单开一轮：它们要传第四个参数（投递账 staged），与上面那批的调用形状不同。
// 三条各自的**红法**才是重点：编的名字要判成「没声明」，真名字没投上去要判成「没投递」，
// 而且必须是**两句不同的话**——合成一句就等于把「改代码」和「修链路」混成同一个待办。
const ASSET_RULE = 'dg/staticfile-asset-check';
const assetCases = [
  ['素材：声明了且投到了（必须绿）', assetOk, ['chart.png'], ['chart.png'], (e) => e.length === 0],
  ['素材：staticFile 引用了没声明的名字（必须红 未声明）', assetUndeclared, ['chart.png'],
    ['chart.png'], has(ASSET_RULE, '未声明')],
  ['素材：声明了但没投到容器（必须红 没投到渲染容器）', assetDeclared, ['chart.png'],
    [], has(ASSET_RULE, '没投到渲染容器')],
  ['素材：投递账看不见（staged=null，只问代码合规的口）→ 不误杀，绿',
    assetDeclared, ['chart.png'], null, (e) => e.length === 0],
];
for (const [name, code, assets, staged, predicate] of assetCases) {
  const r = await lintShotCode(code, [], assets, staged);
  console.log(`\n=== ${name} → ok=${r.ok}`);
  for (const e of r.errors) console.log(`  [${e.rule}] ${e.line}:${e.column} ${e.msg.slice(0, 70)}`);
  if (predicate && !predicate(r.errors)) { console.log('  !! 不符合预期'); fail++; }
}
console.log(fail === 0 ? '\nPASS' : `\nFAIL ${fail}`);
// 构建层末尾用的是本脚本的退出码（`node tests/gate1-lint.mjs`）。只打印不改码
// 等于闸① 的红用例失败了镜像照样烤成功——那层「硬验」就白写了。
process.exitCode = fail === 0 ? 0 : 1;
