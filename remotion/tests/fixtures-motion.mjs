// 闸⑭ 的两支真渲夹具（先证红的那一半），单独一个文件是为了**判据与夹具能分开被读**：
// `tests/motion-probe.mjs` 拿它们渲真像素，别的脚本（比如离线过一遍闸①）也可以只 import
// 字符串，不必把整个探针跑起来。
//
// 两支都照起手模板 `src/user-shots/shot.tsx` 的形状写：`<FilmLayout>` 包裹 +
// `useBody()` 给的带子 + `<svg>` 画图元（`dg/no-raw-styled-div` 不许用带背景的
// `<div>` 直接当画布，理由见那一条规则自己）。

const HEAD = `import React from 'react';
import { interpolate, useCurrentFrame, useVideoConfig } from 'remotion';
import { FilmLayout, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { E } from '@dg/motion';
`;

/** 红样一：所有元素**从第 0 帧就在终态**——静止的一张图。 */
export const STATIC_SHOT = `${HEAD}
const Shot = ({ text = '' }) => {
  const body = useBody();
  const ease = E.standard(0.5);
  return (
    <FilmLayout text={text}>
      <svg viewBox={\`0 0 \${body.w} \${body.h}\`} width={body.w} height={body.h}>
        <rect x={body.w * 0.08} y={body.h * 0.12} width={body.w * 0.36} height={body.h * 0.3}
              rx={body.w * 0.02} fill={TOKENS.accent} opacity={ease} />
        <rect x={body.w * 0.56} y={body.h * 0.12} width={body.w * 0.36} height={body.h * 0.3}
              rx={body.w * 0.02} fill={TOKENS.ink} opacity={ease} />
        <rect x={body.w * 0.08} y={body.h * 0.58} width={body.w * 0.84} height={body.h * 0.3}
              rx={body.w * 0.02} fill={TOKENS.accent} opacity={ease} />
      </svg>
    </FilmLayout>
  );
};
export default Shot;
`;

/** 红样二：三件色块在**同一段时间**里齐发（都是 0.3 s → 0.65 s，错开 0.03 s 是
 *  为了避开「三件一模一样」被误当成静止的另一种机制——判据不靠这个错开，
 *  它在像素上就是**一段**长的变化）。
 *  「所有元素挤在同一段时间里一起变」正是判据第二条要拦的那种片子：
 *  单看代码「有动效」，看起来却是一次跳变。 */
export const SIMULTANEOUS_SHOT = `${HEAD}
const Shot = ({ text = '' }) => {
  const body = useBody();
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const drop = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' };
  const near = -body.h * 0.34;
  const ease = E.standard(0.5);
  const at = (lag) => \`translate(0 \${interpolate(frame, [fps * (0.3 + lag), fps * (0.65 + lag)], [near, 0], drop)})\`;
  return (
    <FilmLayout text={text}>
      <svg viewBox={\`0 0 \${body.w} \${body.h}\`} width={body.w} height={body.h}>
        <rect x={body.w * 0.08} y={body.h * 0.14} width={body.w * 0.36} height={body.h * 0.22}
              rx={body.w * 0.02} fill={TOKENS.accent} opacity={ease}
              transform={at(0)} />
        <rect x={body.w * 0.56} y={body.h * 0.14} width={body.w * 0.36} height={body.h * 0.22}
              rx={body.w * 0.02} fill={TOKENS.ink} opacity={ease}
              transform={at(0.01)} />
        <rect x={body.w * 0.08} y={body.h * 0.56} width={body.w * 0.84} height={body.h * 0.3}
              rx={body.w * 0.02} fill={TOKENS.accent} opacity={ease}
              transform={at(0.02)} />
      </svg>
    </FilmLayout>
  );
};
export default Shot;
`;

/** 绿样（应当是 3 段）：三件色块在 0.3 s / 0.9 s / 1.5 s **依次**滑入。
 *  每件滑 0.35 s、彼此隔 0.6 s——都过判据那两条线（≥2 段、相邻 ≥0.4 s）。 */
export const SEQUENCED_SHOT = `${HEAD}
const Shot = ({ text = '' }) => {
  const body = useBody();
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const drop = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' };
  const near = -body.h * 0.34;
  const ease = E.standard(0.5);
  return (
    <FilmLayout text={text}>
      <svg viewBox={\`0 0 \${body.w} \${body.h}\`} width={body.w} height={body.h}>
        <rect x={body.w * 0.08} y={body.h * 0.14} width={body.w * 0.36} height={body.h * 0.22}
              rx={body.w * 0.02} fill={TOKENS.accent} opacity={ease}
              transform={\`translate(0 \${interpolate(frame, [fps * 0.3, fps * 0.65], [near, 0], drop)})\`} />
        <rect x={body.w * 0.56} y={body.h * 0.14} width={body.w * 0.36} height={body.h * 0.22}
              rx={body.w * 0.02} fill={TOKENS.ink} opacity={ease}
              transform={\`translate(0 \${interpolate(frame, [fps * 0.9, fps * 1.25], [near, 0], drop)})\`} />
        <rect x={body.w * 0.08} y={body.h * 0.56} width={body.w * 0.84} height={body.h * 0.3}
              rx={body.w * 0.02} fill={TOKENS.accent} opacity={ease}
              transform={\`translate(0 \${interpolate(frame, [fps * 1.5, fps * 1.85], [near, 0], drop)})\`} />
      </svg>
    </FilmLayout>
  );
};
export default Shot;
`;
