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
const eslintFor = (() => {
  const cache = new Map();
  return (declared) => {
    const key = declared.join('\u0000');
    let inst = cache.get(key);
    if (inst) return inst;
    const fieldOpts = () => ['error', { declared }];
    inst = new ESLint({
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
        rules: {
          'dg/only-allowed-imports': 'error',
          'dg/needs-shot-export': 'error',
          'dg/no-external-urls': 'error',
          'dg/must-use-motion': 'error',
          'dg/no-raw-styled-div': 'error',
          'dg/fields-declared-unused': fieldOpts(),
          'dg/fields-undeclared-read': fieldOpts(),
          'dg/field-mark-not-reading': fieldOpts(),
          'dg/staticfile-font-only': 'error',
          'dg/font-family-whitelist': 'error',
          'dg/font-load-check': 'error',
          'no-restricted-globals': ['error', ...RESTRICTED_GLOBALS],
          'no-restricted-syntax': ['error', ...RESTRICTED_SYNTAX],
        },
      },
      cwd: process.cwd(),
    });
    if (cache.size > 64) cache.delete(cache.keys().next().value);
    cache.set(key, inst);
    return inst;
  };
})();

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
