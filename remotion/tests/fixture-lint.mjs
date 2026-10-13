// 夹具自己先过闸①——离线、不渲。为什么单开这一支：B 半那两支夹具要真渲几十秒,
// 而它们**最容易挂的其实是 lint**（写夹具的人不会记得每条规则）。先在几毫秒里把这件事
// 判掉，红的时候报的也是 lint 的行列号，不是「渲染 400」。
import { lintShotCode } from '../lint.mjs';
import { STATIC_SHOT, SIMULTANEOUS_SHOT, SEQUENCED_SHOT } from './fixtures-motion.mjs';

let fail = 0;
for (const [name, code] of [['静帧镜（红样）', STATIC_SHOT],
                            ['齐发镜（红样）', SIMULTANEOUS_SHOT],
                            ['先后三件（绿样）', SEQUENCED_SHOT]]) {
  const r = await lintShotCode(code, [], [], null);
  console.log(`\n=== ${name} → ok=${r.ok}`);
  for (const e of r.errors) console.log(`  [${e.rule}] ${e.line}:${e.column} ${e.msg.slice(0, 90)}`);
  if (!r.ok) fail++;
}
console.log(fail === 0 ? '\nPASS' : `\nFAIL ${fail}`);
process.exitCode = fail === 0 ? 0 : 1;
