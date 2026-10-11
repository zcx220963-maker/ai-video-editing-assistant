/**
 * 镜头代码的落点 —— 每次渲染前由 server 覆写这个文件。
 *
 * 这份默认实现同时是**起手模板**：模型该照它的形状写，而不是自己发明一套。
 * 它演示了四件事：
 *   1. 顶层必须 import 并包裹 `<FilmLayout>`（闸① 强制）；
 *   2. 能画的地方只有 `useBody()` 报回来的那条带子，坐标系已是设备像素，
 *      **不要再乘 u 去猜高度**（旧链那 12 镜全猜了 `1080×620`，横屏母题被压成 9%）；
 *   3. 时刻只从 `useCurrentFrame()` 拿（禁 rAF/setTimeout/Date）；
 *   4. 随机要确定性 —— 用 `remotion` 的 `random(seed)`，不用 `Math.random`。
 */
import React from 'react';
import { interpolate, random, useCurrentFrame, useVideoConfig } from 'remotion';
import { FilmLayout, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
  subtitleMode?: 'full' | 'torn_highlight' | 'none';
  aspect?: 'portrait' | 'landscape' | 'square';
}

const Shot: React.FC<ShotProps> = ({ text = '', highlight = [], words, subtitleMode, aspect }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const body = useBody(aspect);
  const t = frame / Math.max(1, durationInFrames - 1);

  return (
    <FilmLayout
      text={text}
      highlight={highlight}
      words={words}
      subtitleMode={subtitleMode}
      aspect={aspect}
    >
      <svg
        viewBox={`0 0 ${body.w} ${body.h}`}
        width={body.w}
        height={body.h}
        style={{ display: 'block' }}
      >
        {Array.from({ length: 24 }).map((_, i) => {
          // 母题铺满这条带子，而不是缩在左上角一小块：占画面比例本身就是验收项。
          const cx = (random(`px-${i}`) * 0.88 + 0.06) * body.w;
          const cy = (random(`py-${i}`) * 0.82 + 0.09) * body.h;
          const r = body.w * (0.012 + random(`pr-${i}`) * 0.03);
          const rise = interpolate(t, [i / 48, i / 48 + 0.35], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          });
          return (
            <circle
              key={i}
              cx={cx}
              cy={cy - (1 - rise) * body.h * 0.16}
              r={r}
              fill={i % 5 === 0 ? TOKENS.accent : TOKENS.ink}
              opacity={0.15 + rise * 0.6}
            />
          );
        })}
      </svg>
    </FilmLayout>
  );
};

export const UserShot = Shot;
export default Shot;
