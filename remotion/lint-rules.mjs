/**
 * 闸① 的自定义规则（文件级 + 三条字段对账）。
 *
 * 为什么禁项走 ESLint 内置的 `no-restricted-*`，而这些要自研：
 * 它们问的不是「用了哪个危险 API」，而是「这个文件整体上像不像一个合法镜头」——
 * 白名单、导出、外链都是**文件级**事实，选择器表达不了。
 * （原先还有第五条「顶层必须包住 `<FilmLayout>`」；母版下线那一轮删了它——
 * 镜头代码现在自包含，包不包壳是写法不是合法性。）
 *
 * 三条字段对账规则另有一层：它们要拿**分镜契约里声明的字段名清单**比，而那份清单
 * 不在代码字节里。所以 lint 的入参是 `(code, declaredFields)`（`lint.mjs` 把它
 * 塞进规则的配置项），漏传就等于把这三条闸关掉——这是它们与上面四条唯一的差别，
 * 也是为什么这一份清单必须由调用方（Python 侧的 `code_spec`）交过来而不是从代码里猜。
 */

import { readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** 构建期 fc-list :family 生成的字体白名单。Dockerfile 在构建末尾用
 *  fc-list 现生成、覆写这份文件，同时硬验 Noto Sans/Serif/Mono CJK SC 都在。
 *  本地没这份文件时白名单为空集，font-family-whitelist 规则让路（不报错）——
 *  真正的拦截发生在 Docker 构建期，那里白名单一定非空。 */
let FONT_WHITELIST = new Set();
try {
  const __dirname = dirname(fileURLToPath(import.meta.url));
  FONT_WHITELIST = new Set(
    JSON.parse(readFileSync(join(__dirname, 'font-whitelist.json'), 'utf8')),
  );
} catch {}

/** R5-T16：随镜像发出去的自有 webfont 字族，从注册表 `src/fonts.ts` 里读。
 *
 *  为什么从源文件读、不另写一份 JSON：这份名单的**唯一判据是「本仓库当场加载了它」**。
 *  另写一份清单就有了两处真相——注册表加了字族而清单没加，镜头里写它就误红；
 *  反过来清单加了而没人加载，就是「写了但没人接」那种假绿。读同一个文件，
 *  两份名单天然同步。正则而不是 import：这里是纯 Node（ESLint 进程），没有打包器，
 *  而 `src/fonts.ts` 是 TS + JSX 生态里的文件，import 不动。 */
let WEB_FONT_FAMILIES = new Set();
try {
  const __dirname = dirname(fileURLToPath(import.meta.url));
  const fontsTs = readFileSync(join(__dirname, 'src', 'fonts.ts'), 'utf8');
  const m = /export const WEB_FONT_FAMILIES\s*=\s*\[([^\]]*)\]/.exec(fontsTs);
  if (m) WEB_FONT_FAMILIES = new Set(
    [...m[1].matchAll(/['"]([^'"]+)['"]/g)].map((x) => x[1]),
  );
} catch {}

/** webfont 字节的落点。`DG_PUBLIC_ROOT` 只给测试用（与 shipped 同一个口子）：
 *  想造「loadFont 指着一个不存在的文件」这个红样，不该先往真 public/ 里摆假货。 */
const PUBLIC_ROOT = (() => {
  const __dirname = dirname(fileURLToPath(import.meta.url));
  return process.env.DG_PUBLIC_ROOT
    ? join(process.env.DG_PUBLIC_ROOT)
    : join(__dirname, 'public');
})();

/** CSS 泛型字族——永远放行，不在白名单里也不报。 */
const GENERIC_FAMILIES = new Set([
  'sans-serif', 'serif', 'monospace', 'cursive', 'fantasy',
  'system-ui', 'ui-monospace', 'ui-sans-serif', 'ui-serif', 'ui-rounded',
  'math', 'emoji', 'fangsong', 'inherit', 'initial', 'unset', 'revert',
]);

/** 把 CSS font-family 字符串拆成家族名数组。
 *  '"Noto Sans CJK SC", sans-serif' → ['Noto Sans CJK SC', 'sans-serif'] */
function parseFontFamilies(str) {
  return str.split(',')
    .map((s) => s.trim().replace(/^["']|["']$/g, '').trim())
    .filter(Boolean);
}

/** 预装进 creation-remotion 镜像的那批包 —— 白名单外的 import 一律退回。
 *
 * 这份名单的**唯一判据是「镜像里真装着了」**，不是「Remotion 有这个东西」。
 * 曾经列过两个没装的：`@remotion/sequence` 在 npm 上根本不存在（`<Sequence>`
 * 是 `remotion` 核心的导出，已经在下面），`@remotion/preload` 是浏览器侧
 * 预加载用的、服务端这条链没有用途。留着它们等于闸① 放行一写就必然在
 * 打包期报「解析失败」的 import——而且报错发生在 lint 通过之后，模型收到的
 * 是一句谁也看不懂的 bundle 错误。谁想开新包，先装进镜像再来加这一行，
 * 构建层末尾的那条 resolve 检查会替两边盯着。
 */
export const ALLOWED_IMPORTS = new Set([
  'react',
  'react-dom',
  'remotion',
  '@remotion/paths',
  '@remotion/shapes',
  '@remotion/motion-blur',
  '@remotion/layout-utils',
  // R5-T16：官方写法块点名的四件 + 字幕 token 化的那件（§3.1 的「新增进白名单」集合）。
  // fonts 治第六根因（静默回落）；rough-notation 是手绘标注族；noise 的 noise2D/3D/4D
  // 与「禁 Math.random」同向（种子确定）；animation-utils 目前只有 getSpringFrame。
  '@remotion/fonts',
  '@remotion/captions',
  '@remotion/rough-notation',
  '@remotion/noise',
  '@remotion/animation-utils',
  // R5-T16b：官方效果件。入场券是 `tests/effects-probe.mjs` 那三条**真渲**判据
  // （不崩 / 画面里没有参数外的颜色 / 挂错宿主是静默空操作），不是「Remotion 有这个东西」
  // ——这一族是 WebGL2 着色器，历史上的坏法是**悄悄染一层颜色**，任何「渲染返回 200」
  // 都判不出来。只在 `<Img>/<Solid>/<HtmlInCanvas>` 上生效，别处是静默空操作。
  //
  // **为什么按子路径逐件列、而不是放行包名 `@remotion/effects`**（§9-Q89）：
  // 4.0.534 的桶文件 `dist/index.d.ts` 只重导 11 件，`paper`/`halftone` 都不在其中，
  // 它们只能从 `@remotion/effects/paper` 这样拿。放行桶名等于同时放行两种坏：
  // ① 那 11 件没经过先验就先得了票；② 更要命的是**看着像放行了 paper，实际拿到
  // `undefined`**——`import { paper } from '@remotion/effects'` 打包期不报错，
  // 到渲染时才炸一句「paper is not a function」。所以这里只列**先验过的**两件。
  '@remotion/effects/paper',
  '@remotion/effects/halftone',
  // 第二张表（中性色效果）：源图三块中性色，判据是「没崩 / 色度不跑 / 与不挂效果有差」
  // ——见 `tests/effects-probe.mjs` 的 `EFFECT_TABLE`。按设计改色相的四件
  // （duotone / lut / light-leak / chromatic-aberration）不在其中：它们要另一份
  // 「声明调色板 vs 画面颜色」的判据，中性色判据对它们没有意义。
  '@remotion/effects/glow',
  '@remotion/effects/vignette',
  '@remotion/effects/noise',
  '@remotion/effects/speckle',
  '@remotion/effects/roughen-edges',
  '@remotion/effects/shadows-highlights',
  '@remotion/effects/blur',
  // 画法通道：真三维与手绘斜纹。这两个家族在 package.json + lockfile 里躺着，
  // 且 2026-10-11 那批六支真渲夹具（roughjs / three / KaTeX × 两画幅）就是它们渲出来的
  'three',
  '@react-three/fiber',
  '@remotion/three',
  'roughjs',
  'katex',
  'katex/dist/katex.min.css',
  // 母版与 token 走别名
  '@dg/film',
  '@dg/tokens',
  // R5-T16：webfont 注册表。镜头代码写 `@dg/fonts` 的那一下，就是把
  // loadFont + 101 片 unicode-range 拉进依赖图——字族白名单也读这个文件。
  '@dg/fonts',
  // shot 配方卡的基础动效件（_fixtures 是真实现，不是桩）
  '@dg/motion',
  '@dg/polish',
  '@dg/brand',
  '@dg/fixtures/Motion',
  '@dg/fixtures/Polish',
  '@dg/fixtures/Brand',
  '@dg/fixtures/Fixtures',
  '@dg/fixtures/Look',
  '@dg/fixtures/PageCam2D',
  '@dg/lib/Caption',
  '@dg/lib/ClipCard',
  '@dg/lib/DigitRoll',
  '@dg/lib/FlashCut',
  '@dg/lib/PageCam',
  '@dg/lib/VerticalTicker',
  '@dg/lib/FlatPanel',
  '@dg/lib/helpers/motion',
  '@dg/lib/helpers/rand',
  '@dg/lib/helpers/shake',
  '@dg/lib/helpers/camera',
  // 手绘笔刷与场景底板（2026-10-18）。这两个**是带 index 的目录**，所以列目录名就能
  // 解析（与上面 `@dg/lib/draw` 那批不同：那个目录没有 index 文件，只能逐件列）。
  // 它们是可选参考件，不是闸⑦ 的入场券——镜头代码裸写 SVG/Canvas 同样算画。
  '@dg/lib/brush',
  '@dg/lib/scene',
  // 同一个主角的走路件（2026-10-19）。它把步态、身高与朝向写成一份确定性实现，
  // 二十几镜各自写码时才不会切一镜换一个身材。与上面两件同属 `vector` 通道。
  '@dg/lib/walk',
  // 风格模板（2026-10-19）：系统注册的 `silhouette-history`。用户不能上传 TSX，
  // 这张白名单就是全部可用模板；新增模板时与 `backend/.../code_spec.py` 的
  // `_ALLOWED_IMPORTS` 同步。
  '@dg/lib/style/silhouette-history',
  // 简笔画零件库（2026-10-17）。**逐件列，与 `@dg/lib/*` 同一口径**：每件是一个
  // 文件级的入口，模型 import 一件就拿到一支能被认出的东西的轮廓（人、机器、厂房、
  // 齿轮、图表…）。它们底下走的是 roughjs 的手绘线，所以这些件的 import 在
  // `code_spec.PICTORIAL_CHANNELS` 里记成 `rough` 通道——闸⑦「每镜必画」与闸⑯
  // 「主体具名」认的就是那一条，不然「用库里的人偶画画」会被记成「只画了文字与色块」。
  '@dg/lib/draw/Figure',
  '@dg/lib/draw/Face',
  '@dg/lib/draw/Machine',
  '@dg/lib/draw/Building',
  '@dg/lib/draw/Vehicle',
  '@dg/lib/draw/Gear',
  '@dg/lib/draw/Chart',
  '@dg/lib/draw/Arrow',
  '@dg/lib/draw/Coin',
  '@dg/lib/draw/Book',
  '@dg/lib/draw/Scale',
  '@dg/lib/draw/Clock',
  '@dg/lib/draw/Crown',
  '@dg/themes/palettes.json',
]);

const isRelative = (src) => typeof src === 'string' && (src.startsWith('.') || src.startsWith('/'));

/** 用户沉淀的模板落在 `src/lib/style/generated/<name>.tsx`，镜头按这个名字 import。
 *  名字必须与渲染容器 `POST /templates` 的校验同一口径（小写字母开头，字母数字与 `-`），
 *  否则一条带路径分隔符的 import 会把 bundler 带到生成目录之外。 */
export const USER_TEMPLATE_RE = /^@dg\/lib\/style\/generated\/[a-z][a-z0-9-]{1,40}$/;

const isAllowedImport = (src) =>
  ALLOWED_IMPORTS.has(src) || USER_TEMPLATE_RE.test(String(src || ''));

/** 字段通道三条规则的共用静态分析。
 *
 * 它只做一件做得到的事：**从代码字节里认出「哪些字段被读了、被标在哪」**。
 * 认不出来的写法（`fields[k]` 的 k 是变量、`...fields`、把 fields 整个传给函数）
 * 一律置 `dynamic`，让「声明了却没人读」这条规则**让路**而不是报错——
 * 一条会误伤的闸最后只会被人整条关掉，而闸⑥ 在动态那一侧本来就要再对一次账。
 */
function newFieldAnalysis(declared) {
  const declaredSet = new Set(declared);
  const roots = new Set(['fields']);          // 注入表的根名（含别名）
  const reads = new Map();                     // fieldName -> [node]
  const bindings = new Map();                  // 局部变量名 -> fieldName
  const marks = [];                            // {node, name|dynamicName, valueNode|missing}
  const fNames = new Set();                    // 从 @dg/film 导入的 F / Field 的局部名
  const rootRefs = [];                          // 所有叫根名的标识符节点
  const consumed = new Set();                   // 被识别成「读表动作」的根节点
  const state = { dynamic: false, declaredSet, reads, bindings, marks, fNames };

  const isRoot = (node) => !!node && node.type === 'Identifier' && roots.has(node.name);
  const noteRead = (name, node) => {
    if (!name) return;
    if (!reads.has(name)) reads.set(name, []);
    reads.get(name).push(node);
  };

  return Object.assign(state, {
    onImport(node) {
      if (node.source.value !== '@dg/film') return;
      for (const s of node.specifiers || []) {
        if (s.type === 'ImportSpecifier' && (s.imported.name === 'F' || s.imported.name === 'Field')) {
          fNames.add(s.local.name);
        }
      }
    },

    onIdentifier(node) {
      if (!roots.has(node.name)) return;
      const p = node.parent;
      if (!p) return;
      // `a.fields` 里那个 property、`{fields: x}` 里那个 key 都不是「表本身」
      if (p.type === 'MemberExpression' && p.property === node && !p.computed) return;
      if (p.type === 'Property' && p.key === node && !p.computed) return;
      // 下面两处同样是**绑定位置**而不是读表动作。漏掉它们的后果实测过：
      // 起手契约教的 `const Shot = ({ fields = {} }) => …` 会因为这个带默认值的
      // 解构项被判成「整块用掉了表」，于是「声明了却没人读」这条闸对**所有**镜头
      // 静默让路——三条对账剩两条，而打印出来一切正常。
      if (p.type === 'AssignmentPattern' && p.left === node) return;
      if (p.type === 'VariableDeclarator' && p.id === node) return;
      if (Array.isArray(p.params) && p.params.includes(node)) return;
      rootRefs.push(node);
    },

    onMember(node) {
      if (!isRoot(node.object)) return;
      consumed.add(node.object);
      if (!node.computed && node.property.type === 'Identifier') {
        noteRead(node.property.name, node);
      } else if (node.computed && node.property.type === 'Literal'
                 && typeof node.property.value === 'string') {
        consumed.add(node.property);
        noteRead(node.property.value, node);
      } else {
        state.dynamic = true;   // fields[变量]：不知道读的是哪个，只能让路
      }
    },

    onDeclarator(node) {
      const init = node.init;
      if (!init) return;
      if (isRoot(init)) {
        consumed.add(init);
        if (node.id.type === 'ObjectPattern') {
          for (const p of node.id.properties) {
            if (p.type === 'RestElement') { state.dynamic = true; continue; }
            const key = p.key;
            const fieldName = key.type === 'Identifier' ? key.name
              : (key.type === 'Literal' ? String(key.value) : null);
            if (!fieldName) { state.dynamic = true; continue; }
            consumed.add(key);
            noteRead(fieldName, node);
            const local = p.value && p.value.type === 'Identifier' ? p.value.name
              : (key.type === 'Identifier' ? key.name : null);
            if (local) bindings.set(local, fieldName);
          }
        } else if (node.id.type === 'Identifier') {
          roots.add(node.id.name);   // const f = fields —— f.a 也算读表
        }
        return;
      }
      // const x = fields.a / const x = f.a
      if (init.type === 'MemberExpression' && isRoot(init.object)
          && !init.computed && init.property.type === 'Identifier'
          && node.id.type === 'Identifier') {
        bindings.set(node.id.name, init.property.name);
      }
    },

    onSpread(node) {
      if (isRoot(node.argument)) { consumed.add(node.argument); state.dynamic = true; }
    },

    onJsx(node) {
      const id = node.name && node.name.type === 'JSXIdentifier' ? node.name.name : null;
      if (!id || !fNames.has(id)) return;
      const attrs = new Map();
      for (const a of node.attributes || []) {
        if (a.type !== 'JSXAttribute' || !a.name) continue;
        attrs.set(a.name.name, a);
      }
      const nameAttr = attrs.get('name');
      const valueAttr = attrs.get('value');
      let name = null;
      let dynamicName = false;
      if (nameAttr && nameAttr.value && nameAttr.value.type === 'Literal'
          && typeof nameAttr.value.value === 'string') {
        name = nameAttr.value.value;
      } else if (nameAttr) {
        dynamicName = true;
      }
      // 「value 是写死的」有两种写法，都得算同一种错：`value="40"` 与 `value={40}`。
      // 只认前者的话，后者会掉进「假指针」分支——同样是红，但报的是另一条理由，
      // 模型照着错误信息去改就会改成 `value={40.0}` 那一类，白返工一轮。
      const rawValue = valueAttr && valueAttr.value;
      const expr = rawValue && rawValue.type === 'JSXExpressionContainer'
        ? rawValue.expression : null;
      const isFixed = expr
        ? (expr.type === 'Literal' ||
           (expr.type === 'TemplateLiteral' && (expr.expressions || []).length === 0))
        : true;
      const valueLiteral = expr
        ? (expr.type === 'Literal' ? expr.value : undefined)
        : (rawValue && rawValue.type === 'Literal' ? rawValue.value : undefined);

      marks.push({
        node,
        name,
        dynamicName,
        missingName: !nameAttr,
        valueNode: expr && !isFixed ? expr : null,
        valueLiteral,
        hasValue: !!valueAttr,
      });
    },

    /** 表达式是不是「就是这个字段的那份值」。 */
    valueCarries(mark, fieldName) {
      const e = mark.valueNode;
      if (!e) return false;
      if (e.type === 'Identifier') return bindings.get(e.name) === fieldName;
      if (e.type === 'MemberExpression' && !e.computed && e.property.type === 'Identifier') {
        if (isRoot(e.object)) return e.property.name === fieldName;
        const bound = e.object.type === 'Identifier' ? bindings.get(e.object.name) : null;
        if (bound) return false;              // fields.a.b 这类：不是表本身，判不出来
      }
      return false;
    },

    finish(context, kind) {
      // 没被识别成读表动作的根引用＝这份表被整块用掉了，静态判不了
      for (const node of rootRefs) if (!consumed.has(node)) state.dynamic = true;

      const loc = (node) => (node && node.loc ? { line: node.loc.start.line, column: node.loc.start.column } : { line: 1, column: 0 });

      if (kind === 'unused') {
        if (state.dynamic || !declared.length) return;
        const dead = declared.filter((n) => !reads.has(n));
        if (dead.length) {
          context.report({
            loc: { line: 1, column: 0 },
            messageId: 'unused',
            data: { names: dead.join(', '), reads: dead.map((n) => `fields.${n}`).join('、') },
          });
        }
      }

      if (kind === 'undeclared') {
        for (const [name, nodes] of reads) {
          if (declaredSet.has(name)) continue;
          context.report({
            loc: loc(nodes[0]),
            messageId: 'undeclared',
            data: { name, alt: [...declaredSet].join(', ') || '（这一镜没声明任何字段）' },
          });
        }
      }

      if (kind === 'mark') {
        for (const mark of marks) {
          if (mark.missingName) { context.report({ loc: loc(mark.node), messageId: 'noName' }); continue; }
          if (mark.dynamicName) { context.report({ loc: loc(mark.node), messageId: 'dynName' }); continue; }
          if (!declaredSet.has(mark.name)) {
            context.report({
              loc: loc(mark.node), messageId: 'markUndeclared',
              data: { name: mark.name, alt: [...declaredSet].join(', ') || '（这一镜没声明任何字段）' },
            });
            continue;
          }
          if (!mark.hasValue || mark.valueNode === null) {
            context.report({
              loc: loc(mark.node), messageId: 'noReading',
              data: { name: mark.name, lit: mark.hasValue ? ` value="${mark.valueLiteral}"` : '' },
            });
            continue;
          }
          if (!this.valueCarries(mark, mark.name)) {
            context.report({
              loc: loc(mark.node), messageId: 'notThis',
              data: { name: mark.name },
            });
          }
        }
      }
    },
  });
}

const FIELD_RULE_SCHEMA = [{
  type: 'object',
  properties: { declared: { type: 'array', items: { type: 'string' } } },
  additionalProperties: false,
}];

const fieldRule = (kind, messages) => ({
  meta: { type: 'problem', schema: FIELD_RULE_SCHEMA, messages },
  create(context) {
    const declared = (context.options[0] && context.options[0].declared) || [];
    const a = newFieldAnalysis(declared);
    return {
      ImportDeclaration: (n) => a.onImport(n),
      Identifier: (n) => a.onIdentifier(n),
      MemberExpression: (n) => a.onMember(n),
      VariableDeclarator: (n) => a.onDeclarator(n),
      SpreadElement: (n) => a.onSpread(n),
      JSXOpeningElement: (n) => a.onJsx(n),
      'Program:exit'() { a.finish(context, kind); },
    };
  },
});

export const dgPlugin = {
  rules: {
    'only-allowed-imports': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          denied:
            'import 了白名单外的 "{{source}}"。新链只预装了这些包：{{allowed}}。' +
            '要用别的库请先装进渲染容器——在这里写 import 只会在浏览器里报解析失败。',
        },
      },
      create(context) {
        return {
          ImportDeclaration(node) {
            const src = node.source.value;
            if (isRelative(src) || isAllowedImport(src)) return;
            context.report({
              node,
              messageId: 'denied',
              data: { source: src, allowed: [...ALLOWED_IMPORTS].join(', ') },
            });
          },
          // require() 也在禁项里，但万一有人从 CommonJS 绕
          CallExpression(node) {
            if (node.callee.name !== 'require' || !node.arguments.length) return;
            const arg = node.arguments[0];
            const src = arg.type === 'Literal' ? arg.value : undefined;
            if (src === undefined || isRelative(src) || isAllowedImport(src)) return;
            context.report({
              node,
              messageId: 'denied',
              data: { source: src, allowed: [...ALLOWED_IMPORTS].join(', ') },
            });
          },
        };
      },
    },

    'needs-shot-export': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          noExport:
            '镜头文件没有可挂载的导出。请写 `export default Shot;`' +
            '（兼容写法 `export const UserShot = Shot;` 也认）。' +
            '没有默认导出时 Remotion 只会说「component 收到了 undefined」，' +
            '那句错隔了打包与渲染两层，看不出是你这一行没写。',
        },
      },
      create(context) {
        let hasDefault = false;
        let hasNamed = false;
        return {
          ExportDefaultDeclaration() {
            hasDefault = true;
          },
          ExportNamedDeclaration(node) {
            const decl = node.declaration;
            if (decl && decl.type === 'VariableDeclaration') {
              if ((decl.declarations || []).some((d) =>
                d.id && d.id.name === 'UserShot')) hasNamed = true;
            }
            (node.specifiers || []).forEach((s) => {
              if (s.exported && s.exported.name === 'UserShot') hasNamed = true;
            });
          },
          'Program:exit'() {
            if (!hasDefault && !hasNamed) {
              context.report({ loc: { line: 1, column: 0 }, messageId: 'noExport' });
            }
          },
        };
      },
    },

    'no-external-urls': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          external:
            '引用了外部资源 "{{url}}"。渲染容器离线运行，外链字体/图片/脚本会**静默回落**：' +
            '你在代码里写的字体和画面上出现的不是一回事。字体只用镜像里已有的 Noto CJK。',
        },
      },
      create(context) {
        const RE = /https?:\/\/[^\s"'`)<]+|url\(\s*['"]?https?:|@import/gi;
        const check = (node, value) => {
          if (typeof value !== 'string') return;
          const hit = value.match(RE);
          if (hit) {
            context.report({ node, messageId: 'external', data: { url: hit[0].slice(0, 80) } });
          }
        };
        return {
          Literal: (node) => check(node, node.value),
          TemplateElement: (node) => check(node, node.value.raw),
        };
      },
    },

    'must-use-motion': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          noImport:
            '镜头代码必须 import 至少一个工具 from "@dg/motion" 或 "@dg/polish"。' +
            '这些是 shot 卡动效的基础工具（E/seg/lerp/DesignStage/EASE/Grain/Vignette）。' +
            '从 shot 配方卡适配 demo 代码时，demo 已经 import 了这些。',
          noUse:
            'import 了 @dg/motion 或 @dg/polish 但代码里没用到任何工具。',
        },
      },
      create(context) {
        let imported = false;
        let usedTools = new Set();
        const MOTION_EXPORTS = new Set([
          'E', 'seg', 'lerp', 'rand', 'useT', 'DesignStage',
        ]);
        const POLISH_EXPORTS = new Set([
          'EASE', 'Grain', 'Vignette', 'bezier', 'ramp', 'mix', 'FONT', 'fitText',
        ]);
        return {
          ImportDeclaration(node) {
            if (node.source.value === '@dg/motion' || node.source.value === '@dg/polish') {
              imported = true;
            }
          },
          Identifier(node) {
            if (MOTION_EXPORTS.has(node.name) || POLISH_EXPORTS.has(node.name)) {
              usedTools.add(node.name);
            }
          },
          'Program:exit'() {
            if (!imported) {
              context.report({ loc: { line: 1, column: 0 }, messageId: 'noImport' });
            } else if (usedTools.size === 0) {
              context.report({ loc: { line: 1, column: 0 }, messageId: 'noUse' });
            }
          },
        };
      },
    },

    'no-raw-styled-div': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          rawBg:
            '禁止用 <div> 带 background/backgroundColor 画纯色块——纯色填充走 SVG ' +
            '<rect>/<circle>/<path> 的 fill（颜色取主题色板 palettes.json），' +
            '或用 AbsoluteFill 的渐变 backgroundImage。渐变（linear-gradient/radial-gradient）可以。',
          rawBgLiteral:
            '禁止用 <div> 带 background="{{val}}" 画纯色块——用 SVG <rect fill> + 主题色板。',
        },
      },
      create(context) {
        const isPureColor = (val) => {
          if (typeof val !== 'string') return false;
          const s = val.trim();
          return s.startsWith('#') ||
            /^(rgb|hsl)a?\(/i.test(s) ||
            !s.includes('(') && s.length > 0;
        };
        return {
          JSXOpeningElement(node) {
            if (!node.name || node.name.name !== 'div') return;
            for (const attr of node.attributes || []) {
              if (attr.type !== 'JSXAttribute' || attr.name.name !== 'style') continue;
              const val = attr.value;
              if (!val || val.type !== 'JSXExpressionContainer') continue;
              const expr = val.expression;
              if (!expr || expr.type !== 'ObjectExpression') continue;
              for (const prop of expr.properties || []) {
                if (!prop.key) continue;
                const keyName = prop.key.name || prop.key.value;
                if (keyName === 'backgroundColor') {
                  context.report({ node, messageId: 'rawBg' });
                }
                if (keyName === 'background') {
                  const v = prop.value;
                  if (!v) continue;
                  if (v.type === 'Literal' && isPureColor(v.value)) {
                    context.report({ node, messageId: 'rawBgLiteral', data: { val: String(v.value).slice(0, 40) } });
                  }
                  if (v.type === 'JSXExpressionContainer' && v.expression.type === 'Literal' && isPureColor(v.expression.value)) {
                    context.report({ node, messageId: 'rawBgLiteral', data: { val: String(v.expression.value).slice(0, 40) } });
                  }
                }
              }
            }
          },
        };
      },
    },

    // （原来这里有一条 `no-svg`，2026-10-12 撤掉。）撤它的理由是数出来的：迁进来的
    // 226 个 demo 里 **123 个用 `<svg>`/`<path>` 画图**，硬禁等于把刚搬进来的语料判掉一半，
    // 而这条链上「认得出的画面主体」主要就靠这些几何件。坏画面（空壳、挤成一团）已经有
    // 闸④ 在像素上判（`EMPTY_MIN_RATIO` / `SPAN_MIN` / `TIGHT_MIN`），不必在语法层再关一种画法。

    // ---- 字段参数通道的三条对账（spec §12.5）----
    // 这一族与上面四条的唯一区别是它们要拿分镜声明的字段名比，所以 lint 的调用方
    // 必须把那份名单传进来（`lint.mjs` 塞进规则配置）。名单漏传时 declared 是空数组：
    // 规则 1 无事可判、规则 3 会把每一处标注都报成「没声明」——**不会静默放行**。
    'fields-declared-unused': fieldRule('unused', {
      unused:
        '分镜声明了字段 {{names}}，但这段代码一次都没读它（读法要写成 {{reads}}）。' +
        '于是界面上这一栏改了值、重烧了这一镜，画面上却一点没变——声明了就得真用它，' +
        '用不上的旋钮别交出来。',
    }),

    'fields-undeclared-read': fieldRule('undeclared', {
      undeclared:
        '代码读了 fields.{{name}}，但这一镜的 fields 里没有这一项（已声明：{{alt}}）。' +
        '于是界面没有控件能改它，闸⑥ 也没有期望值可对——要么在分镜里声明它，' +
        '要么把这个读取去掉。',
    }),

    'field-mark-not-reading': fieldRule('mark', {
      noName:
        '<F> 标注没写 name，母版回读时不知道这是哪个字段——闸⑥ 与字段级命中表都只能' +
        '把它当没标。写 `<F name="字段名" value={fields.字段名}>`。',
      dynName:
        '<F> 的 name 写成了表达式。静态这一层跟不住变量，这一处标注无法对账；' +
        '请写成字符串字面量。',
      markUndeclared:
        '<F name="{{name}}"> 标的字段没在分镜里声明（已声明：{{alt}}）。' +
        '标出来的东西界面改不到、闸⑥ 也没有期望值——声明它，或去掉这处标注。',
      noReading:
        '<F name="{{name}}">{{lit}}…</F> 的 value 没带上注入的那份值。' +
        '这样闸⑥ 比的是字面量自己（永远相等），而不是「我们注入的是几、画上用的是几」' +
        '——整条通道的证据就没了。写 `value={fields.{{name}}}`。',
      notThis:
        '<F name="{{name}}"> 的 value 不是 `fields.{{name}}`，也不是绑到它的那个变量。' +
        '这是一个假指针：盒指着这个字段，像素却来自别处，改值时画面上动的不是它。',
    }),

    'staticfile-font-only': {
      // 供图通道撤掉后，随镜铺进 `public/` 的只剩 `fonts/`。所以 `staticFile` 还能指的
      // 就只有字体——指别的名字，运行时那个文件根本不在，渲出来是一张空图。原先这条规则
      // 是「声明 vs 投递两份账对账」（`dg/staticfile-asset-check`）；那两份账（分镜里的
      // `assets` 与投递 manifest）随通道一起没了，判据就缩成这一条还留得下对象的话。
      meta: { type: 'problem', schema: [],
              messages: {
                notFont: 'staticFile("{{arg}}") 指的不是字体。供图通道撤掉后，随镜像铺进这一镜 '
                  + 'public/ 的只有 `public/fonts/`；画面得由你自己写代码画出来，摆一张图不叫画'
                  + '（闸⑦「每镜必画」）。要取字体请写 staticFile(\'fonts/….woff2\')。',
                nonLiteral: 'staticFile 的实参必须是字符串字面量，变量拼路径让机器核对不了它指的是不是字体。',
              } },
      create(context) {
        const SHIPPED = 'fonts/';
        return {
          CallExpression(node) {
            if (node.callee.type !== 'Identifier' || node.callee.name !== 'staticFile') return;
            const arg = node.arguments[0];
            if (!arg || arg.type !== 'Literal' || typeof arg.value !== 'string') {
              context.report({ node, messageId: 'nonLiteral' });
              return;
            }
            if (!arg.value.startsWith(SHIPPED)) {
              context.report({ node, messageId: 'notFont', data: { arg: arg.value } });
            }
          },
        };
      },
    },

    'font-family-whitelist': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          notInWhitelist:
            'fontFamily 里的家族 "{{family}}" 不在镜像字体白名单内（可用：{{allowed}}）。' +
            '镜像里没装的字体在渲染时会静默回落成默认字族——画面上出现的和代码里写的不是一回事。',
        },
      },
      create(context) {
        // 两类合法字族：① 镜像里装着的（构建期 fc-list 生成的白名单）；
        // ② 本仓库当场加载的（`src/fonts.ts` 的 WEB_FONT_FAMILIES，R5-T16）。
        // 少了第②类，随我们发出去的 webfont 就永远写不进 fontFamily——治第六根因
        // 的那一半会被自己的闸挡住。
        const allowed = new Set([...FONT_WHITELIST, ...WEB_FONT_FAMILIES]);
        if (allowed.size === 0) return {};
        const constStrings = new Map();

        const check = (node, value) => {
          if (typeof value !== 'string') return;
          for (const name of parseFontFamilies(value)) {
            if (GENERIC_FAMILIES.has(name.toLowerCase())) continue;
            if (allowed.has(name)) continue;
            context.report({
              node,
              messageId: 'notInWhitelist',
              data: { family: name, allowed: [...allowed].join(', ') },
            });
          }
        };

        const resolveVal = (val) => {
          if (!val) return null;
          if (val.type === 'Literal' && typeof val.value === 'string') return val.value;
          if (val.type === 'Identifier' && constStrings.has(val.name)) return constStrings.get(val.name);
          if (val.type === 'TemplateLiteral' && val.expressions.length === 0)
            return val.quasis.map((q) => q.value.raw).join('');
          return null;
        };

        return {
          VariableDeclarator(node) {
            if (node.id.type !== 'Identifier' || !node.init) return;
            const v = resolveVal(node.init);
            if (v != null) constStrings.set(node.id.name, v);
          },
          Property(node) {
            if (!node.key) return;
            if ((node.key.name || node.key.value) !== 'fontFamily') return;
            const v = resolveVal(node.value);
            if (v != null) check(node.value, v);
          },
          JSXAttribute(node) {
            if (!node.name || node.name.name !== 'fontFamily') return;
            const v = resolveVal(node.value);
            if (v != null) check(node.value, v);
            if (node.value && node.value.type === 'JSXExpressionContainer') {
              const v2 = resolveVal(node.value.expression);
              if (v2 != null) check(node.value.expression, v2);
            }
          },
        };
      },
    },

    // ── R5-T16「加载即断言」───────────────────────────────────────────────
    // 上面那条管「写了哪个字族的名字」，这条管「那个字族的字节到底在不在」。
    // 两件事都会让画面上的字与代码里写的不是一个东西，但红法不同：
    // 前者是名字没登记，后者是**登记了却指空**——`loadFont` 指向一个不存在的
    // 文件时，Remotion 不会报错，浏览器只是继续用回落字族，而那次渲染照样出片。
    'font-load-check': {
      meta: {
        type: 'problem',
        schema: [],
        messages: {
          notStaticFile:
            'loadFont 的 url 必须是 `staticFile(\'fonts/….woff2\')`。这里是 {{what}}——' +
            '渲染容器没有外网（也没有浏览器侧的 CDN），远端字体只会静默加载失败，' +
            '然后浏览器拿回落字族把这一镜画完。',
          outsideFonts:
            'loadFont 指向 `{{path}}`，但 webfont 的落脚点是 public/fonts/（`staticFile` 的根是 ' +
            'public/）。放别处的字体进不了镜像的约定位置——只有 public/fonts/ 是随镜像发出去的。',
          missingFile:
            'loadFont 指向 `{{path}}`，但 public/ 下没有这个文件。**这条不会在渲染时报错**：' +
            '浏览器静默回落到别的字族，出片看着正常，只是字不对。要发字体先跑 ' +
            '`node scripts/fetch_fonts.mjs`。',
          badFormat:
            'loadFont 的 format 是 {{format}}，与文件后缀 `{{ext}}` 对不上。写错的 format ' +
            '会让浏览器拒收这份字节，同样静默回落。',
        },
      },
      create(context) {
        const files = new Map();      // 已判过的路径，省掉重复 stat
        const exists = (relPath) => {
          if (!files.has(relPath)) {
            try {
              files.set(relPath, statSync(join(PUBLIC_ROOT, relPath)).isFile());
            } catch {
              files.set(relPath, false);
            }
          }
          return files.get(relPath);
        };
        const EXT_FORMAT = {
          '.woff2': 'woff2', '.woff': 'woff',
          '.ttf': 'truetype', '.otf': 'opentype', '.eot': 'embedded-opentype',
        };

        return {
          CallExpression(node) {
            const callee = node.callee;
            if (!callee || callee.type !== 'Identifier') return;
            if (callee.name !== 'loadFont') return;      // 名字判据：官方 API 就叫这个
            const arg = node.arguments[0];
            if (!arg || arg.type !== 'ObjectExpression') return;

            let urlNode = null;
            let formatValue = null;
            for (const p of arg.properties) {
              if (!p.key) continue;
              const key = p.key.name || p.key.value;
              if (key === 'url') urlNode = p.value;
              if (key === 'format' && p.value && p.value.type === 'Literal') {
                formatValue = p.value.value;
              }
            }
            if (!urlNode) return;

            let relPath = null;
            if (urlNode.type === 'CallExpression'
                && urlNode.callee && urlNode.callee.name === 'staticFile') {
              const a = urlNode.arguments[0];
              if (a && a.type === 'Literal' && typeof a.value === 'string') relPath = a.value;
            }
            if (relPath == null) {
              const what = urlNode.type === 'Literal' && typeof urlNode.value === 'string'
                && /^(https?:)?\/\//.test(urlNode.value)
                ? '一个直接写出来的远端 URL' : '一段运行时才算得出来的表达式';
              context.report({ node: urlNode, messageId: 'notStaticFile', data: { what } });
              return;
            }
            if (!relPath.startsWith('fonts/')) {
              context.report({
                node: urlNode, messageId: 'outsideFonts', data: { path: relPath },
              });
            }
            if (!exists(relPath)) {
              // 最后一条也报：路径与存在性是两件事，一句话只说一件事，退回的人才改得动
              context.report({
                node: urlNode, messageId: 'missingFile', data: { path: relPath },
              });
            }
            const dot = relPath.lastIndexOf('.');
            const ext = dot >= 0 ? relPath.slice(dot).toLowerCase() : '';
            if (formatValue && EXT_FORMAT[ext] && formatValue !== EXT_FORMAT[ext]) {
              context.report({
                node: urlNode, messageId: 'badFormat',
                data: { format: formatValue, ext },
              });
            }
          },
        };
      },
    },
  },
};

/** 确定性禁项：任何按墙钟走的东西都会让「同输入同像素」变成运气。 */
export const RESTRICTED_GLOBALS = [
  'fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource',
  'localStorage', 'sessionStorage', 'indexedDB', 'navigator',
  // performance.now() 也是墙钟。本来想用选择器拦 CallExpression，esquery 不接受
  // 那种点号路径（会把整条规则集弄崩），改成禁这个全局标识符。
  'performance',
];

export const RESTRICTED_SYNTAX = [
  { selector: "CallExpression[callee.object.name='Math'][callee.property.name='random']",
    message: 'Math.random() 按墙钟播种，两次渲染不同像素。要用随机请写 remotion 的 random(seed)。' },
  { selector: "CallExpression[callee.object.name='Date'][callee.property.name='now']",
    message: 'Date.now() 是墙钟，逐帧 seek 下不确定。' },
  { selector: 'NewExpression[callee.name="Date"]',
    message: 'new Date() 是墙钟，逐帧 seek 下不确定。' },
  { selector: "CallExpression[callee.name=/^(setTimeout|setInterval|requestAnimationFrame|requestIdleCallback)$/]",
    message: '定时器/rAF 驱动动画在本容器里推不动（实测 Chrome 一次只发得出 1~2 个 rAF 就饿死），' +
             '而且按墙钟走。时刻只能来自 useCurrentFrame()。' },
  { selector: "CallExpression[callee.name='eval']", message: 'eval 一律禁。' },
  { selector: "NewExpression[callee.name='Function']", message: 'new Function 是换了个名字的 eval。' },
  { selector: "CallExpression[callee.object.name='process']",
    message: '镜头代码拿不到宿主进程。' },
];
// R2: staticFile 只许指字体（供图通道撤掉后，随镜铺进 public/ 的只剩 fonts/）
export const STATICFILE_RULE = 'dg/staticfile-font-only';
// R4: 字体白名单规则
export const FONT_RULE = 'dg/font-family-whitelist';
// R5: webfont「加载即断言」规则（loadFont 指的字节必须在 public/fonts/ 里真存在）
export const FONT_LOAD_RULE = 'dg/font-load-check';
