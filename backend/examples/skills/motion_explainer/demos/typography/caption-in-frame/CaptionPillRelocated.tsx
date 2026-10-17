/**
 * 字幕搬家 —— 字幕进画面的另一式：**整条字幕**（圆角纸条 + 字 + 入场动画）不再站在画面下沿，
 * 而是吊在构图里别的东西上（这里吊在一张深色卡的下缘，像一张贴上去的便条）。
 *
 * 与同目录 `TextOverArt` 的分工，一句话：
 *   只要**字**、底自己给（scrim / 笔刷 / 抠图）→ `<CaptionText/>`；
 *   要**整条**、连纸条底和入场动画一起搬 → `<CaptionLayer/>`（这一份）。
 * 两者画出来的字是同一个实现，所以搬家之后观感不会走样。
 *
 * 两条这一式独有的硬纪律：
 *   1. **摆了 `<CaptionLayer/>`，母版就让位——不必申报任何档位。** 入库前 Python 侧扫到
 *      这段代码里有 `<CaptionLayer`，会给这一镜注入 `captionFromCode`，母版那条不再画：
 *      「谁来摆」是**代码的静态事实**，不是运行期猜的，所以同屏两条字幕从结构上不可能。
 *      唯一要避开的自相矛盾：这一镜申报了 `caption_style: "none"`（不上字幕）却又摆这
 *      一件——它自守不画，闸㉜ 会把那种代码打回。
 *   2. **位置跟着最近的定位祖先走。** 下面把它放进那张卡片（卡片自己是 `position: absolute`，
 *      已经是定位祖先）里，`bottom` 于是量的是到**卡片下缘**的距离，`left/right` 量的是
 *      卡片内部的两侧——「随构图摆」要的就是这一层。想让它贴画面下沿、和母版一个位置，
 *      把它放回顶层、不带 props 即可。
 */
import React from 'react';
import { CaptionLayer, FilmLayout, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { E, rand, seg, useT } from '@dg/motion';
import { Grain, softShadow, Vignette } from '@dg/polish';

export const CAPTION_PILL_RELOCATED_DURATION = 150;

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
  aspect?: 'portrait' | 'landscape' | 'square';
}

const Shot: React.FC<ShotProps> = ({ text = '', highlight = [], words, aspect }) => {
  const t = useT();
  const body = useBody(aspect);
  const w = body.w;
  const h = body.h;

  const cardX = Math.round(w * 0.07);
  const cardW = w - cardX * 2;
  const cardY = Math.round(h * 0.09);
  const cardH = Math.round(h * 0.5);
  const pad = Math.round(cardW * 0.08);
  const cardIn = seg(t, 0.02, 0.3, E.outCubic);

  // 卡下的几张小票：错峰浮出，把画面下半部填住（与字幕无关的一层，只为构图）
  const slips = Array.from({ length: 4 }).map((_, i) => ({
    i,
    in: seg(t, 0.24 + i * 0.07, 0.5 + i * 0.07, E.outQuad),
    w: 0.18 + rand(i * 5 + 2) * 0.12,
  }));

  return (
    <FilmLayout text={text} highlight={highlight} words={words} aspect={aspect}>
      <div
        style={{
          position: 'absolute', inset: 0, background: TOKENS.bg,
          fontFamily: TOKENS.fontStack, overflow: 'hidden',
        }}
      >
        <div
          style={{
            position: 'absolute', right: -w * 0.24, top: -h * 0.06,
            width: w * 0.8, height: h * 0.42, borderRadius: '50%',
            background: TOKENS.accent, opacity: 0.12, filter: 'blur(80px)',
          }}
        />

        {/* 深色卡：卡片自己是定位祖先，字幕吊在它的下缘——「字幕跟着构图走」的全部机关 */}
        <div
          style={{
            position: 'absolute', left: cardX, top: cardY, width: cardW, height: cardH,
            padding: pad, boxSizing: 'border-box',
            background: TOKENS.ink, borderRadius: Math.round(w * 0.035),
            opacity: 0.35 + 0.65 * cardIn,
            transform: `translateY(${Math.round((1 - cardIn) * h * 0.04)}px)`,
            boxShadow: softShadow(18),
          }}
        >
          <div style={{ fontSize: Math.round(w * 0.038), color: TOKENS.accent, letterSpacing: '0.16em' }}>
            这一镜的一个物件
          </div>
          <div
            style={{
              fontSize: Math.round(w * 0.075), fontWeight: 600, color: TOKENS.paper,
              marginTop: Math.round(pad * 0.4), lineHeight: 1.2,
            }}
          >
            字幕吊在它的下缘
          </div>
          <div
            style={{
              marginTop: Math.round(pad * 0.5), height: Math.max(3, Math.round(w * 0.006)),
              width: Math.round(cardW * 0.26), background: TOKENS.accent,
            }}
          />

          {/* 整条字幕：底 / 字 / 入场动画与母版同一个组件。`bottom` 量到**这张卡**的下缘；
              不复用母版的位置，因为这一式的全部意义就是搬家。
              `captionStyle="paper"` 是这一式特意要的**纸条底**——搬的正是那张纸条；
              不传就跟着整片那一档（缺省「贴画面」，无底）。 */}
          <CaptionLayer
            text={text}
            highlight={highlight}
            words={words}
            captionStyle="paper"
            bottom={Math.round(cardH * 0.06)}
          />
        </div>

        {/* 卡下的小票：错峰浮出 */}
        <div
          style={{
            position: 'absolute', left: cardX, right: cardX, top: cardY + cardH + Math.round(h * 0.06),
            display: 'flex', gap: Math.round(w * 0.03), alignItems: 'flex-start',
          }}
        >
          {slips.map((s) => (
            <div
              key={s.i}
              style={{
                flex: s.w, height: Math.round(h * 0.1 * s.in),
                background: TOKENS.paper, opacity: 0.16 + 0.5 * s.in,
                borderRadius: Math.round(w * 0.014),
              }}
            />
          ))}
        </div>

        <Grain opacity={0.05} />
        <Vignette strength={0.24} inner={0.58} />
      </div>
    </FilmLayout>
  );
};

export const UserShot = Shot;
export default Shot;
