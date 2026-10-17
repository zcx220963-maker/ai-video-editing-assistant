/**
 * 字压图 —— 字幕进画面的一式：整句话**压在画面上**、自带一层柔底，图形跟着念到的字动。
 *
 * 这一份 demo 与同目录其他 demo 有一处**故意不同**，读之前先知道：
 * 别的 demo 是 1920×1080 的独立片段（自己铺底、自己量尺寸），因为它教的是「一段动效怎么做」；
 * 这一份教的是**母版留给镜头代码的那套 API 怎么用**（`@dg/film` 的字幕三件），所以它按
 * 「一镜」的形状写：`<FilmLayout>` 包裹、`useBody()` 量带子、文案与词时间轴从注入里来。
 * 折成独立片段反而会把要学的东西折掉——这里要学的恰恰是「怎么接母版」。
 *
 * 三件事各归一件东西管，这就是「字幕与画面长在一起」的全部秘密：
 *   ① 字 → `<CaptionText/>`：逐词点亮 / 撕纸高亮 / 按容量求字号，与母版那条字幕**同一份实现**；
 *   ② 底 → 镜头代码自己铺（这里是半透明的柔光板）：母版那张纸条归字幕带，压在插画上的观感
 *      要自己给。这便是 `<CaptionText/>` 与 `<CaptionLayer/>` 的分工；
 *   ③ 图形 → `useCaptionTiming()`：这一段第一个字被念到的那一帧（`charFrames`）驱动柱子的
 *      高度，`reached()` 驱动它头上那颗点。「字幕与画面交互」的时间维就落在这一件上，
 *      不必自己再实现一遍 `charStartFrames`——那正是这条链最怕的第二份出处。
 *
 * 前提：这一式的镜头要在分镜表里声明 `caption_style: "none"`（母版那条才不画）。
 * 闸㉜ 不管这一件——它只判「申报了 none 却摆整条 `<CaptionLayer/>`」那种自相矛盾，
 * 而这里摆的是**只有字**的 `<CaptionText/>`。不声明也不会红，但成片上会同时出现母版的
 * 字幕与你压上去的这行字——同一句话两处，是每镜预览静帧上一眼能看见的事。
 */
import React from 'react';
import { useCurrentFrame } from 'remotion';
import { CaptionText, FilmLayout, useBody, useCaptionTiming } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { E, rand, seg, useT } from '@dg/motion';
import { Grain, Vignette } from '@dg/polish';

export const TEXT_OVER_ART_DURATION = 150;

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
  aspect?: 'portrait' | 'landscape' | 'square';
}

const Shot: React.FC<ShotProps> = ({ text = '', highlight = [], words, aspect }) => {
  const frame = useCurrentFrame();
  const t = useT();
  const body = useBody(aspect);
  const { pieces, charFrames, reached } = useCaptionTiming({ text, highlight, words });

  const w = body.w;
  const h = body.h;
  const pad = Math.round(w * 0.06);
  const textW = w - pad * 2;
  const textIn = seg(t, 0.02, 0.26, E.outCubic);

  // 谱：整句按高亮词切成的**片段**一段一根柱子 —— 片段与「念到哪里」一一对应，
  // 所以柱子的顺序就是句子的顺序。
  const bars = pieces.map(({ p, start }, i) => {
    const word = highlight.some((x) => x === p);
    // 有词时间轴：这一段的第一个字被念到的那一帧起跳；
    // 没有词时间轴（`charFrames` 为 null）：退回按片段序号错峰——这也是为什么
    // `reached()` 在没有词时间轴时**恒为 true**（整句一起亮，没有「念到」可言）。
    const rise = charFrames
      ? seg(frame, charFrames[start], charFrames[start] + 9, E.outCubic)
      : seg(t, 0.12 + i * 0.05, 0.44 + i * 0.05, E.outCubic);
    return {
      key: `${start}-${p.length}`,
      word,
      lit: reached(start),
      rise,
      tall: 0.34 + rand(i * 7 + 3) * 0.5,
    };
  });

  return (
    <FilmLayout text={text} highlight={highlight} words={words} aspect={aspect}>
      <div
        style={{
          position: 'absolute', inset: 0, background: TOKENS.bg,
          fontFamily: TOKENS.fontStack, overflow: 'hidden',
        }}
      >
        {/* 一团侧光：底面有明暗，字才看得出是**压在画面上**而不是压在空场上 */}
        <div
          style={{
            position: 'absolute', left: -w * 0.2, top: -h * 0.12,
            width: w * 0.86, height: h * 0.5, borderRadius: '50%',
            background: TOKENS.paper, opacity: 0.18, filter: 'blur(70px)',
          }}
        />

        {/* ① 字压在画面上半部。柔底由镜头代码给（②），字本身交给 `CaptionText`——
            它与母版那条字幕共用「逐词点亮 / 撕纸高亮 / 求字号」那一份实现。 */}
        <div
          style={{
            position: 'absolute', left: pad, top: Math.round(h * 0.1), width: textW,
            opacity: textIn, transform: `translateY(${Math.round((1 - textIn) * h * 0.03)}px)`,
          }}
        >
          <div
            style={{
              position: 'absolute', left: -Math.round(pad * 0.5), right: -Math.round(pad * 0.5),
              top: -Math.round(h * 0.022), bottom: -Math.round(h * 0.026),
              background: TOKENS.paper, opacity: 0.34,
              borderRadius: Math.round(w * 0.03), filter: 'blur(18px)',
            }}
          />
          <div style={{ position: 'relative', fontWeight: 600, color: TOKENS.ink, lineHeight: 1.34 }}>
            <CaptionText text={text} highlight={highlight} words={words} fitWidth={textW} />
          </div>
        </div>

        {/* ③ 谱：图形跟着念到的字动。高度走 `charFrames`（能提前起跳、能缓动），
            头上那颗点走 `reached()`（布尔，适合开关式的显隐）。 */}
        <div
          style={{
            position: 'absolute', left: pad, right: pad, top: Math.round(h * 0.66),
            height: Math.round(h * 0.16), display: 'flex', alignItems: 'flex-end',
            gap: Math.round(w * 0.014),
          }}
        >
          {bars.map((b) => (
            <div
              key={b.key}
              style={{
                flex: 1, height: '100%', display: 'flex', flexDirection: 'column',
                justifyContent: 'flex-end', alignItems: 'center',
                gap: Math.round(h * 0.008),
              }}
            >
              <div
                style={{
                  width: Math.max(3, Math.round(w * 0.008)), height: Math.max(3, Math.round(h * 0.004)),
                  background: TOKENS.accent, borderRadius: 99,
                  opacity: b.lit ? 1 : 0.2,
                }}
              />
              <div
                style={{
                  width: '100%', height: `${Math.round(b.tall * b.rise * 100)}%`,
                  background: b.word ? TOKENS.accent : TOKENS.ink,
                  opacity: b.word ? 0.92 : 0.3,
                  borderRadius: Math.round(w * 0.008),
                }}
              />
            </div>
          ))}
        </div>

        {/* 一根基线：柱子站在它上面，画面下半部不空着 */}
        <div
          style={{
            position: 'absolute', left: pad, right: pad, top: Math.round(h * 0.822),
            height: Math.max(2, Math.round(h * 0.002)), background: TOKENS.ink, opacity: 0.22,
          }}
        />

        <Grain opacity={0.05} />
        <Vignette strength={0.24} inner={0.58} />
      </div>
    </FilmLayout>
  );
};

export const UserShot = Shot;
export default Shot;
