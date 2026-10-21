import { ESLint } from 'eslint';
import tsParser from '@typescript-eslint/parser';
import { dgPlugin, RESTRICTED_GLOBALS, RESTRICTED_SYNTAX } from './lint-rules.mjs';

/** 每一次 lint 都按「这一镜声明了哪些字段」现配一份规则表。
 *
 *  三条字段对账规则要拿声明名单比，而名单不在代码字节里。把配置做成模块级常量
 *  ＝名单永远取那份写死的空数组，规则 1 无事可判、规则 2/3 形同虚设，且**看起来
 *  一切正常**（lint 照样回 ok:true）。所以这里刻意不设默认配置。
 *
 *  实例按名单缓存：ESLint 构造不便宜，而同一镜返工时名单通常不变。
 *  键用 \u0000 连接——字段名自己不许含控制字符（`code_spec` 的 FIELD_NAME_RE 管着）。
 */
function makeEslint(rules) {
  return new ESLint({
    overrideConfigFile: true,
    overrideConfig: {
      files: ['**/*.ts', '**/*.tsx', '**/*.js', '**/*.jsx'],
      languageOptions: {
        parser: tsParser,
        ecmaVersion: 2022,
        sourceType: 'module',
        parserOptions: { ecmaFeatures: { jsx: true } },
      },
      plugins: { dg: dgPlugin },
      rules,
    },
    cwd: process.cwd(),
  });
}

/** 镜头代码与模板共用的一组规则；差异只在「导出契约」与「是否必须用动效件」。 */
function commonRules() {
  return {
    'dg/only-allowed-imports': 'error',
    'dg/no-external-urls': 'error',
    'dg/no-raw-styled-div': 'error',
    'dg/staticfile-font-only': 'error',
    'dg/font-family-whitelist': 'error',
    'dg/font-load-check': 'error',
    'no-restricted-globals': ['error', ...RESTRICTED_GLOBALS],
    'no-restricted-syntax': ['error', ...RESTRICTED_SYNTAX],
    // 2026-10-20 真机：s05 一镜里 `const backX` 写了两次，lint 回 ok:true，一路走到
    // 打包才炸（`The symbol "backX" has already been declared`）——而那一炸发生在
    // 预览静帧那一趟，确认卡上这一镜就只剩「无预览」，渲染时还会整片中停。核心规则
    // 的 `no-redeclare` 在 TS 解析器下就能报这一句（`'x' is already defined.`），
    // 零成本、不误伤（它只认同一作用域里的重复绑定）。
    'no-redeclare': 'error',
  };
}

const eslintFor = (() => {
  const cache = new Map();
  return (declared) => {
    const key = declared.join('\u0000');
    let inst = cache.get(key);
    if (inst) return inst;
    const fieldOpts = () => ['error', { declared }];
    inst = makeEslint({
      ...commonRules(),
      'dg/needs-shot-export': 'error',
      'dg/must-use-motion': 'error',
      'dg/fields-declared-unused': fieldOpts(),
      'dg/fields-undeclared-read': fieldOpts(),
      'dg/field-mark-not-reading': fieldOpts(),
    });
    if (cache.size > 64) cache.delete(cache.keys().next().value);
    cache.set(key, inst);
    return inst;
  };
})();

/** 模板 lint：模板导出的是**组件**，不是一镜的默认 Shot，也没有义务用 `@dg/motion`。
 *  其余规则与镜头代码同一份——用户沉淀的模板绝不能靠「换个入口」绕开白名单、
 *  外链、字族与受限语法。 */
const templateEslint = makeEslint(commonRules());

/**
 * 闸① lint：一段镜头代码 + 这一镜声明的字段名，进；带规则名与行号的错误列表出。
 *
 * 语法错也当 lint 失败回（ruleId 为 null 时标 parse-error）——否则模型交回一段
 * 括号没闭合的代码，我们会一路走到 bundler 才炸，那一次返工比 lint 贵两个数量级。
 *
 * `declaredFields` 缺省成空数组是**有意的严格**：没声明就是没声明，此时代码里出现
 * 任何 `fields.x` 或 `<F>` 都会被打回。想关掉这三条的唯一办法是显式传名单。
 */
export async function lintShotCode(code, declaredFields = []) {
  const declared = [...new Set((Array.isArray(declaredFields) ? declaredFields : [])
    .map((s) => String(s)))].filter(Boolean);
  let reports;
  try {
    reports = await eslintFor(declared)
      .lintText(String(code ?? ''), { filePath: 'shot.tsx' });
  } catch (err) {
    return { ok: false, errors: [{ rule: 'lint-crash', line: 1, msg: String(err?.message ?? err) }] };
  }
  const messages = reports[0]?.messages ?? [];
  const errors = messages.map((m) => ({
    rule: m.ruleId ?? 'parse-error',
    line: m.line ?? 1,
    column: m.column ?? 1,
    msg: m.message,
  }));
  return { ok: errors.length === 0, errors, declared };
}

/**
 * 模板 lint：比镜头代码少两条（`needs-shot-export` / `must-use-motion`），
 * 但**多两条**模板特有的红线：
 *
 *   · 必须至少有一个导出（否则镜头 import 到 undefined，报错发生在两层之后）；
 *   · 不许 import 另一份风格模板（`@dg/lib/style/...`）——模板之间互相依赖会让
 *     「哪一份坏了」变成一个跨文件的谜，也让用户模板有机会借内置模板绕过校验。
 */
export async function lintTemplateCode(code) {
  const text = String(code ?? '');
  let reports;
  try {
    reports = await templateEslint.lintText(text, { filePath: 'template.tsx' });
  } catch (err) {
    return { ok: false, errors: [{ rule: 'lint-crash', line: 1, column: 1, msg: String(err?.message ?? err) }] };
  }
  const errors = (reports[0]?.messages ?? []).map((m) => ({
    rule: m.ruleId ?? 'parse-error',
    line: m.line ?? 1,
    column: m.column ?? 1,
    msg: m.message,
  }));
  if (!/\bexport\s+(default|const|function|class|\{)/m.test(text)) {
    errors.push({ rule: 'dg/template-no-export', line: 1, column: 1,
      msg: '模板文件没有任何导出。至少导出组件，镜头代码才 import 得到。' });
  }
  const styleImport = /\bfrom\s+['"]@dg\/lib\/style\//.exec(text);
  if (styleImport) {
    errors.push({ rule: 'dg/template-no-style-import', line: 1, column: 1,
      msg: '模板之间不能互相 import（包括内置模板）。需要的零件请放进模板自己文件里。' });
  }
  return { ok: errors.length === 0, errors };
}
