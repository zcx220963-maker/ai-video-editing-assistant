/**
 * 白字黑边 —— 最常见的那种硬字幕：白字 + 一圈黑描边，压在**任何**底上都读得出来。
 *
 * 它解决的是「关键词换色」解决不了的那一半：字压在花纹最密、明暗乱跳的画面上时，
 * 靠构图让出干净区域是做不到的（画面本身就是主角）。描边把字与底**在像素上切开**，
 * 于是可读性不再依赖底是什么。
 *
 * 三件事：
 *   ① 描边：**`outline` prop**（八向 textShadow，一像素一圈黑）——这一份 demo 就是
 *      「白字黑边」那一档的参考实现，而那一档现在长在元件里（`CaptionText` 的 `outline`，
 *      宽度按**字号**算；母版的 `outline` 档调的就是它）。这里只把颜色给它，不再自己画
 *      一圈：同一件事写两处，改了一处另一处不动，样张与成片就会长得不一样。
 *      为什么不是 `-webkit-text-stroke`：它的描边**居中**画，宽度吃掉字形自己的肉——
 *      中文细笔画（一、十、人）描粗了会断成黑块；textShadow 只能往外长，字形不变。
 *   ② 底压一层：亮底上白字照样糊。所以底部压一条**渐隐**（下深上透），把字所在的那
 *      一段压暗——这不是纸条（没有边界、不是纯色块），是一条渐变。
 *   ③ 强调：这一式的强调**也用换色**（`emphasis="color"`）——白字里那几个词换成暖黄，
 *      黑描边仍在，读起来还是「硬字幕」的味道。
 *
 * 什么时候用：实录/插画/照片满屏、口播密集、观众要跟读的段落。什么时候不用：
 * 浅底图文镜（白字黑边在奶油底上显得吵）——那用「关键词换色」那一式。
 */
import React from 'react';
import { AbsoluteFill, random, useVideoConfig } from 'remotion';
import { CaptionText, useCaptionTiming } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { useT } from '@dg/motion';

export const OUTLINE_CAPTION_DURATION = 150;

/** 字幕字号。**写死一个数**：与画面里别的元素对齐时字号是设计决定，不该由容量求。 */
const CAPTION_SIZE = 58;

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
}

const Shot: React.FC<ShotProps> = ({ text = '', highlight = [], words }) => {
  const { width, height } = useVideoConfig();
  const t = useT();
  const { pieces, reached } = useCaptionTiming({ text, highlight, words });

  const pad = Math.round(width * 0.07);
  const textW = width - pad * 2;

  // 画面：一块深底 + 几团缓慢游动的光斑。它是「实录/夜景」那一类底的替身——
  // 明暗乱跳、没有干净区域，正是需要描边的那种底。
  const blobs = Array.from({ length: 7 }).map((_, i) => ({
    key: i,
    x: (random(`bx-${i}`) * 0.8 + 0.1) * width,
    y: (random(`by-${i}`) * 0.6 + 0.12) * height,
    r: width * (0.10 + random(`br-${i}`) * 0.14),
    c: i % 3 === 0 ? TOKENS.accent : TOKENS.paper,
    drift: Math.sin(t * Math.PI * 2 + i) * height * 0.02,
  }));

  return (
    <AbsoluteFill style={{ background: TOKENS.ink, fontFamily: TOKENS.fontStack, overflow: 'hidden' }}>
      {blobs.map((b) => (
        <div
          key={b.key}
          style={{
            position: 'absolute', left: b.x - b.r, top: b.y - b.r + b.drift,
            width: b.r * 2, height: b.r * 2, borderRadius: '50%',
            background: b.c, opacity: 0.16, filter: `blur(${Math.round(b.r * 0.5)}px)`,
          }}
        />
      ))}

      {/* 底部的渐隐：把字那一段压暗。**不是纸条**——没有边界、不是纯色块、跟着高度走 */}
      <div
        style={{
          position: 'absolute', left: 0, right: 0, bottom: 0, height: Math.round(height * 0.46),
          background: 'linear-gradient(to top, rgba(0,0,0,0.82) 0%, '
            + 'rgba(0,0,0,0.52) 30%, rgba(0,0,0,0.18) 62%, rgba(0,0,0,0) 100%)',
        }}
      />

      {/* 字：白字 + 黑描边，落在渐隐最实的那一段上（下四分之一）。 */}
      <div
        style={{
          position: 'absolute', left: pad, right: pad, bottom: Math.round(height * 0.13),
          textAlign: 'center', fontWeight: 800, lineHeight: 1.3,
          // 白字由这里给（外层排版决定），黑描边由下面那个 prop 给（宽度按字号算）。
          color: '#ffffff',
          fontSize: CAPTION_SIZE,
        }}
      >
        <CaptionText
          text={text}
          highlight={highlight}
          words={words}
          emphasis="color"
          accent="#ffd75e"
          outline="#000000"
          fontSize={CAPTION_SIZE}
        />
      </div>

      {/* 一句一响的细提示：念到的片段序号走到哪，右下角那一格亮到哪。
          它用的是**同一份词时间轴**——画面里任何「跟着念」的东西都从这里取时间。 */}
      <div
        style={{
          position: 'absolute', right: pad, bottom: Math.round(height * 0.075),
          display: 'flex', gap: Math.round(width * 0.008),
        }}
      >
        {pieces.map(({ p, start }) => (
          <div
            key={`${start}-${p.length}`}
            style={{
              width: Math.round(width * 0.014), height: Math.round(width * 0.014),
              borderRadius: Math.round(width * 0.007),
              background: '#ffffff',
              // 念到就亮、没念到就暗。`reached()` 是布尔，要缓动就自己把帧差成一段
              // （这里不缓动：硬字幕配硬提示，一帧切换才对味）。
              opacity: reached(start) ? 0.9 : 0.18,
            }}
          />
        ))}
      </div>
    </AbsoluteFill>
  );
};

export const UserShot = Shot;
export default Shot;
