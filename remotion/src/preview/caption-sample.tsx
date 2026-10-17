/**
 * 字幕样张的底稿 —— 「文字库」面板上那张预览图画的就是它。
 *
 * `storyline_server/motion/caption_preview.py` 拿它当底稿，配上一对
 * 「字幕样式 × 高亮样式」与一套色板渲一张静帧，给人看那一对渲出来是什么样。
 *
 * 为什么另存一份而不是复用 `src/user-shots/shot.tsx`：那一份是渲染容器**每次出片
 * 覆写的落点文件**（`server.mjs` 的 `SHOT_FILE`）。真机上一镜正在渲、这边又去读它，
 * 读到的是别人那一镜的代码——样张会随缘画成别的片子的某一镜。
 *
 * 改这个文件时守四条：
 *
 * 1. **底上除了字幕什么都没有**。这里是一个**空舞台**：一块底色，一层柔光。
 *    从前这里摆过一张卡、一排柱子、一行注解（当时的理由是「像一帧真片子，才看得出
 *    对比度」）——那是错的：那些图形看着就像「一种可选的画面风格」，而面板上这一排
 *    卡管的是**字幕**、不是画面。画面归镜头代码（一镜一份 TSX，模型自由写），
 *    这张图上不该出现任何会被读成「画面风格」的图形，也不该有第二处文字。
 * 2. **自包含**：顶层就是 `<AbsoluteFill>`（整帧），**不包 `<FilmLayout>`**。
 *    母版那层壳会顺手把别的东西（安全区、进出场、token 回读）也带进这张图里，
 *    而它们与「这一对样式」无关。字幕本身仍旧调**同一件元件** `<CaptionLayer/>`
 *    ——自包含不等于自己画一份字幕，那份实现只有一处。
 * 3. **明暗还是要有，但只有一层**。纯色空场上看不出「白字黑边到底压住了什么」，
 *    所以留一团柔光让画面**上亮下暗**（下沿那几档字幕就落在底色那一块上）。
 *    这是唯一留下的图形——再多一样，就又变成「一幅画」了。
 * 4. **不绑色板名**（不写 `const palette = palettes["…"]`）。一张样张要能在任意一套
 *    色板下渲，所以**它用得到的那两色**从注入的 `fields` 进来（`page` / `surface`）；
 *    注入没给（「默认」那一档）才落回 `TOKENS`。字幕那几色不从这里来——它们走
 *    `film_style`（`caption_preview.film_style()`），与成片同一条。
 */
import React from 'react';
import { AbsoluteFill, useVideoConfig } from 'remotion';
import { CaptionLayer } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { Grain, Vignette } from '@dg/polish';

interface SampleFields {
  page?: string;
  surface?: string;
}

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
  aspect?: 'portrait' | 'landscape' | 'square';
  fields?: SampleFields;
}

const Shot: React.FC<ShotProps> = (props) => {
  const { text = '', highlight = [], words, aspect = 'portrait', fields = {} } = props;
  const { width, height } = useVideoConfig();

  // 底只用两色：底色（page）与那一层柔光（surface）。注入优先，没给就落 `TOKENS`
  // （「默认」那一档走这条路）。
  const page = fields.page || TOKENS.bg;
  const surface = fields.surface || TOKENS.paper;

  return (
    <AbsoluteFill style={{ background: page, fontFamily: TOKENS.fontStack, overflow: 'hidden' }}>
      {/* 一团从左上斜打进来的柔光：整张图唯一的「画面」——它的作用只是让上亮下暗，
          于是「字压在这块底上读不读得出来」看得出来。 */}
      <div
        style={{
          position: 'absolute', left: -width * 0.3, top: -height * 0.16,
          width: width * 1.2, height: height * 0.72, borderRadius: '50%',
          background: surface, opacity: 0.5, filter: 'blur(90px)',
        }}
      />

      {/* 字幕：**同一件元件**（`<CaptionLayer/>`），裸调。样式两维、画幅、文案、词时间轴
          全部从注入取——与成片里那条走的是同一个回退、同一个 `CAPTION_LOOK` 配方。
          `caption_style: none` 那一档它自守不画，于是那张样张就是「这一档没字幕」该有的样子。 */}
      <CaptionLayer text={text} highlight={highlight} words={words} aspect={aspect} />

      <Grain opacity={0.05} />
      <Vignette strength={0.26} inner={0.56} />
    </AbsoluteFill>
  );
};

export const UserShot = Shot;
export default Shot;
