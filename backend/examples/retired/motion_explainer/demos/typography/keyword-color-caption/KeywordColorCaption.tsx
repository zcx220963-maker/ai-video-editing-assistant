/**
 * 关键词换色 —— 字幕**压在画面上**，只有「要强调的那几个词」换颜色，字底不留纸条。
 *
 * 这是推荐的默认观感，也是母版下线那一轮专门写给镜头代码看的一式。它替代的是老做法：
 * 高亮词背后一块粉色色块（`<CaptionText/>` 的缺省 `emphasis="block"`）。两者的差别不是
 * 口味——**一块色块压在图上是贴纸，换字色才是强调**：色块自带一个矩形边界，压在插画、
 * 照片、图表上时与画面的形状语言打架；而字色只改字形内部的填充，字仍然长在画面上。
 *
 * 三件事各归一件东西管（与「字压图」那一卡同一套分工）：
 *   ① 字 → `<CaptionText emphasis="color"/>`：逐词点亮、按可用宽度求字号、把高亮词换成
 *      `accent` 那一色，与母版那条字幕**同一份实现**（不会两处断行、两个字号）；
 *   ② 底 → 画面自己。这一式**没有**纸条，字直接站在画面上——所以可读性靠构图解决：
 *      把字压在画面里较平的暗部/亮部（这里是一块薄柔光 + 一块自绘的暗压），
 *      别压在花纹最密的地方。这也正是它与「白字黑边」的分工：那一式靠描边兜底，
 *      这一式靠构图兜底；
 *   ③ 图 → `useCaptionTiming()`：图里每一块对应句子的一个片段，念到哪一块哪一块就换成
 *      强调色。「字换色」与「图换色」用**同一个 accent、同一份词时间轴**，观众看到的才是
 *      「同一件事在两处同时发生」，而不是两段各动各的动画。
 *
 * 改一镜的观感只需要改两处：`accent`（强调色，通常取这一镜色板的 accent）与字的落位。
 */
import React from 'react';
import { AbsoluteFill, interpolate, random, useCurrentFrame, useVideoConfig } from 'remotion';
import { CaptionText, useCaptionTiming } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { E, seg, useT } from '@dg/motion';

export const KEYWORD_COLOR_CAPTION_DURATION = 150;

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
}

const Shot: React.FC<ShotProps> = ({ text = '', highlight = [], words }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const t = useT();
  const { pieces, charFrames, reached } = useCaptionTiming({ text, highlight, words });

  const accent = TOKENS.accent;
  const pad = Math.round(width * 0.08);
  const textW = width - pad * 2;
  const textIn = seg(t, 0.02, 0.24, E.outCubic);

  // 画面的主体：一幅「条纹谱」——句子的每个片段对应一条，高亮片段那条本来就是强调色。
  // 它给这一镜一个能认出来的东西（闸④ 按像素判空），也给字一个**有形状的底**：
  // 字压在条纹的留白处，而不是压在空场地上。
  const bars = pieces.map(({ p, start }, i) => {
    const word = highlight.some((x) => x === p);
    const rise = charFrames
      ? seg(frame, charFrames[start], charFrames[start] + 8, E.outCubic)
      : seg(t, 0.10 + i * 0.05, 0.40 + i * 0.05, E.outCubic);
    return { key: `${start}-${p.length}`, word, lit: reached(start), rise,
             tall: 0.30 + random(`bar-${i}`) * 0.34 };
  });

  return (
    <AbsoluteFill style={{ background: TOKENS.bg, fontFamily: TOKENS.fontStack, overflow: 'hidden' }}>
      {/* 一层斜向的大色块：画面因此有明暗分区，字压在它较平的一段上 */}
      <div
        style={{
          position: 'absolute', left: -width * 0.15, top: -height * 0.05,
          width: width * 1.3, height: height * 0.62,
          background: TOKENS.paper, opacity: 0.55,
          transform: `rotate(${-8 + t * 3}deg)`,
        }}
      />

      {/* 条纹谱：念到哪一段，哪一段换成强调色 */}
      <div
        style={{
          position: 'absolute', left: pad, right: pad, bottom: Math.round(height * 0.22),
          height: Math.round(height * 0.16), display: 'flex', alignItems: 'flex-end',
          gap: Math.round(width * 0.012),
        }}
      >
        {bars.map((b) => (
          <div
            key={b.key}
            style={{
              flex: 1, height: `${Math.round(b.tall * b.rise * 100)}%`,
              background: b.lit && b.word ? accent : TOKENS.ink,
              opacity: b.lit && b.word ? 0.95 : 0.22,
              borderRadius: Math.round(width * 0.006),
            }}
          />
        ))}
      </div>

      {/* 字：压在画面的上半部，**没有纸条**。`emphasis="color"` 一件事就够——
          高亮词的字色换成 accent，底色与下划线都不给。 */}
      <div
        style={{
          position: 'absolute', left: pad, top: Math.round(height * 0.30), width: textW,
          opacity: textIn,
          transform: `translateY(${Math.round((1 - textIn) * height * 0.02)}px)`,
        }}
      >
        <div style={{ fontWeight: 700, lineHeight: 1.32, color: TOKENS.ink }}>
          <CaptionText
            text={text}
            highlight={highlight}
            words={words}
            emphasis="color"
            accent={accent}
            fitWidth={textW}
          />
        </div>
      </div>

      {/* 一根细基线，给画面收个底（不是纸条，只是一条线） */}
      <div
        style={{
          position: 'absolute', left: pad, right: pad, bottom: Math.round(height * 0.19),
          height: Math.max(2, Math.round(height * 0.0016)),
          background: TOKENS.ink, opacity: interpolate(t, [0.3, 0.6], [0, 0.28],
            { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
        }}
      />
    </AbsoluteFill>
  );
};

export const UserShot = Shot;
export default Shot;
