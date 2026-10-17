/**
 * 镜头代码的落点 —— 每次渲染前由 server 覆写这个文件。
 *
 * 这份默认实现同时是**起手模板**：模型该照它的形状写，而不是自己发明一套。
 * 它演示了六件事：
 *   1. **自包含**：顶层是 `<AbsoluteFill>`，画面由这一镜自己画。母版 `<FilmLayout>`
 *      已下线——不再有一个「必须套进去的壳」替你排版；`@dg/film` 现在只是**零件包**
 *      （字幕件 + 整片那一档的读取 + 字段标注 `F`），用不用随你；
 *   2. 画幅从 `useVideoConfig()` 拿（`width`/`height`/`fps`/`durationInFrames`），
 *      坐标就是整帧设备像素。**不要再按 1920×1080 的设计稿猜**，也不要再乘
 *      「画面带系数」——带子那一层没有（旧模板的 `useBody()` 已退役）；
 *   3. **底色与强调色从整片那一档取**：`resolveFilmStyle(getInputProps())` 拿到的
 *      `bg`/`accent` 就是用户为这条片子选的那一套（他在计划卡上挑的「色板」，
 *      或者你在这一镜代码里 `const palette = palettes["…"]` 自己绑的那一套）。
 *      缺了它这一镜就落回 `TOKENS` 的米白——一整片里就它一个色，看着像贴错了。
 *      **不要写死颜色**：写死之后用户在卡上换色板，你这一镜不动；
 *   4. 字幕交给 `<CaptionLayer/>`（不带 props）：整句、高亮词、逐词时间轴、
 *      字幕样式与高亮样式、字号与位置**全在它里面**，取的就是注入的那一份。
 *      要按自己的构图摆，就把它放进一个 `position: relative` 的盒子里；
 *      要连字的形态都自己定，才用 `<CaptionText/>` 自己排
 *      （见技能正文「字自己排」一节）；
 *   5. **画面要有那件东西**：下面这三件来自 `@dg/lib/draw/*`（简笔画零件库），
 *      它们画的是**能被认出的东西**。这一镜该画什么由**文案**决定——
 *      照「每一镜都要画出文案点到的那件东西」那一节：外轮廓 → 标志物 → 3~6 个部件。
 *      闸㉝ 要求每一镜至少用上一件库里的件（下面这个 `Gear` 就是那一件）；
 *   6. 时刻只从 `useCurrentFrame()` 拿（禁 rAF/setTimeout/Date）——进度用 `@dg/motion`
 *      的 `useT()`（整片归一化到 0~1，末帧恰为 1），分段用 `seg(t, t0, t1, E.…)`，
 *      缓动曲线取共享件 `E.standard` / `E.spring200`，**别自选曲线**：二十镜各挑
 *      一条曲线，整片就没有统一的手感了。
 *      随机要确定性 —— 用 `remotion` 的 `random(seed)` 或零件自己的 `seed`，
 *      不用 `Math.random`。
 */
import React from 'react';
import { AbsoluteFill, getInputProps, useVideoConfig } from 'remotion';
import { E, lerp, seg, useT } from '@dg/motion';
import { CaptionLayer, resolveFilmStyle } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { Figure } from '@dg/lib/draw/Figure';
import { Gear } from '@dg/lib/draw/Gear';
import { Arrow } from '@dg/lib/draw/Arrow';

interface ShotProps {
  text?: string;
  highlight?: string[];
  words?: { text: string; startFrame: number }[];
  aspect?: 'portrait' | 'landscape' | 'square';
}

const Shot: React.FC<ShotProps> = () => {
  const { width, height } = useVideoConfig();
  const t = useT();                     // 整片进度 0~1（末帧恰为 1）
  // 整片那一档（注入给每一镜的 `bg`/`accent`）。认不出就落 `TOKENS` 的缺省。
  const film = resolveFilmStyle(getInputProps());
  const bg = film.bg || TOKENS.bg;
  const accent = film.accent || TOKENS.accent;
  // 零件不读帧：要它们动，就把帧算成一个数传进去
  const spin = lerp(t, 0, 160);
  const grow = seg(t, 0.1, 0.55, E.standard);

  return (
    <AbsoluteFill style={{ backgroundColor: bg, fontFamily: TOKENS.fontStack }}>
      {/* ---- 画面：把**文案点到的那个东西**画出来（下面三件只是占位，照文案换成对的） ---- */}
      <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}
           style={{ display: 'block' }}>
        {/* 一台在转的齿轮：库里的件自己带手绘线与整片配色 */}
        <Gear x={width * 0.72} y={height * 0.32} r={width * 0.07} spin={spin} />
        {/* 一个正在指着它的人 */}
        <Figure x={width * 0.28} y={height * 0.78} h={height * 0.24} pose="point" />
        {/* 一支把两件事连起来的箭头：不传色它就跟整片那一档走，
            这里显式传 `accent` 是因为「就是它」——零件收 `ink`，只覆盖你给的那一笔 */}
        <Arrow x1={width * 0.36} y1={height * 0.6} x2={width * 0.64} y2={height * 0.42}
               upto={grow} ink={{ accent }} />
      </svg>

      {/* ---- 字幕：交给零件包里那一件。整句、高亮词、逐词时间轴、样式与位置
          全在 `<CaptionLayer/>` 里（不带 props 就取注入的那一份，与用户为整片挑的
          字幕样式同一处）。**不要再手摆一条自己的字幕**：那样整片二十镜会有二十个
          字号与位置，而这正是用户会一眼看出来的那种不一致。
          它按最近的定位祖先摆——要挪到自己的构图里就套一个 `position: relative`。 */}
      <CaptionLayer />

      {/* 上面那三件的颜色都跟着整片那一档走；要强调其中一件才显式传色： */}
      {/* <Gear x={…} y={…} r={…} ink={{ accent }} /> */}
    </AbsoluteFill>
  );
};

export const UserShot = Shot;
export default Shot;
