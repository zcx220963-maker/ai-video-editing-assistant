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
const Shot = () => <FilmLayout><div>hi</div></FilmLayout>;
export default Shot;
`;

// ---- 字段参数通道（闸① 新增三条规则）----
// 每条规则都要有自己的红：这三条判的是「声明 vs 代码」两头对不对得上，
// 只测绿的那一侧等于没测——规则写错成「永远通过」和写对，打印出来是一模一样的。
const FIELDS_HEAD = `import React from 'react';
import { useCurrentFrame } from 'remotion';
import { FilmLayout, F } from '@dg/film';
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
];

let fail = 0;
for (const [name, code, predicate, declared] of cases) {
  const r = await lintShotCode(code, declared);
  console.log(`\n=== ${name} → ok=${r.ok}${declared ? ` declared=[${r.declared}]` : ''}`);
  for (const e of r.errors) console.log(`  [${e.rule}] ${e.line}:${e.column} ${e.msg.slice(0, 70)}`);
  if (predicate && !predicate(r.errors)) { console.log('  !! 不符合预期'); fail++; }
  if (name.startsWith('违规') && r.ok) { console.log('  !! 违规样本竟然过了'); fail++; }
}
console.log(fail === 0 ? '\nPASS' : `\nFAIL ${fail}`);
// 构建层末尾用的是本脚本的退出码（`node tests/gate1-lint.mjs`）。只打印不改码
// 等于闸① 的红用例失败了镜像照样烤成功——那层「硬验」就白写了。
process.exitCode = fail === 0 ? 0 : 1;
