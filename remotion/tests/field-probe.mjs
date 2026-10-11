// G3/G4 的真机探针（不是长期用例，长期用例在 backend/tests/test_code_fields.py）：
// 渲一镜带两个字段的镜头，验 `<F>` 的盒回读 + 闸① 的声明名单这一侧真跑通了。
// 用法：docker exec -w 不行（MSYS 改路径），走 sh -c。
const CODE = `import React from 'react';
import { interpolate, useCurrentFrame } from 'remotion';
import { FilmLayout, F } from '@dg/film';
import { TOKENS } from '@dg/tokens';

const Shot = ({ fields = {}, text = '' }) => {
  const frame = useCurrentFrame();
  const { radius } = fields;
  const grow = interpolate(frame, [0, 40], [0.4, 1], { extrapolateRight: 'clamp' });
  const r = radius * grow;
  return (
    <FilmLayout text={text} aspect="portrait">
      <div style={{ position: 'relative', width: '100%', height: '100%' }}>
        <F name="radius" value={radius}>
          <div style={{ position: 'absolute', left: 200, top: 300,
                        width: r * 2, height: r * 2, borderRadius: '50%',
                        background: TOKENS.accent }} />
        </F>
        <F name="accent" value={fields.accent}>
          <div style={{ position: 'absolute', left: 700, top: 900,
                        width: 320, height: 160, background: fields.accent }} />
        </F>
      </div>
    </FilmLayout>
  );
};
export default Shot;
`;

const post = async (path, payload) => {
  const r = await fetch(`http://127.0.0.1:8010${path}`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { status: r.status, json: await r.json() };
};

// 1) /lint 与 /render-shot 两条口必须同判：声明名单没传全时两边都要红
const halfList = await post('/lint', {
  code: CODE, declared_fields: ['radius'],
  input_props: { fields: { radius: 90, accent: '#123456' } },
});
console.log('[lint 只给一半名单] ok=' + halfList.json.ok,
  (halfList.json.errors || []).map((e) => `${e.rule}::${e.msg.slice(0, 40)}`));

const good = await post('/lint', {
  code: CODE, declared_fields: ['radius', 'accent'],
  input_props: { fields: { radius: 90, accent: '#123456' } },
});
console.log('[lint 名单齐全] ok=' + good.json.ok + ' declared=' + good.json.declared,
  (good.json.errors || []).map((e) => `${e.rule}::${e.msg.slice(0, 60)}`));
if (!good.json.ok) { console.log('HALT: lint 不绿，没进渲染'); process.exit(1); }

// 2) 真渲一镜，看 fields_used 的盒
const t0 = Date.now();
const rendered = await post('/render-shot', {
  code: CODE,
  cache_key: `fieldprobe${Date.now()}`,
  declared_fields: ['radius', 'accent'],
  input_props: {
    fps: 30, durationInFrames: 45, aspect: 'portrait', text: '这一镜有两个旋钮',
    fields: { radius: 90, accent: '#123456' },
  },
});
const j = rendered.json;
console.log(`[render] status=${rendered.status} 用时=${((Date.now() - t0) / 1000).toFixed(1)}s`);
if (!j.ok) { console.log('HALT: ' + j.error, j.errors || ''); process.exit(1); }
console.log('[render] bytes=' + j.bytes + ' probed=' + JSON.stringify(j.probed));
console.log('[render] warnings=' + JSON.stringify(j.warnings));
console.log('[render] fields_used=' + JSON.stringify(j.fields_used, null, 1));
console.log('[render] tokens=' + (j.tokens ? '有' : '无'));

const marks = (j.fields_used || {}).marks || [];
const need = (n) => marks.find((m) => m.name === n);
let fail = 0;
const say = (ok, msg) => { if (!ok) fail++; console.log(`${ok ? '  ok ' : '  XX '} ${msg}`); };
say(!!need('radius'), 'radius 有回读');
say(!!need('accent'), 'accent 有回读');
say(need('radius')?.value === 90, `radius 回读的值就是注入的 90（实得 ${JSON.stringify(need('radius')?.value)}）`);
say(need('accent')?.value === '#123456', `accent 回读的值（实得 ${JSON.stringify(need('accent')?.value)}）`);
say((need('radius')?.box?.w ?? 0) > 100, `radius 的盒宽度非零（实得 ${JSON.stringify(need('radius')?.box)}）`);
say((need('accent')?.box?.w ?? 0) > 100, `accent 的盒宽度非零（实得 ${JSON.stringify(need('accent')?.box)}）`);
say(j.fields_used?.frame === 43, `探针帧 = durationInFrames-2 = 43（实得 ${j.fields_used?.frame}）`);
say(marks.length === 2, `marks 条数 = 2（实得 ${marks.length}）`);
console.log(fail === 0 ? '\nPASS' : `\nFAIL ${fail}`);
process.exitCode = fail === 0 ? 0 : 1;
