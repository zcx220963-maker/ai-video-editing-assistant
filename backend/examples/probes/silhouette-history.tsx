import React from 'react';
import { FilmLayout } from '@dg/film';
import {
  SilhouetteStage, GlowRect, SilhouetteWalk, EraInscription,
} from '@dg/lib/style/silhouette-history';

/** 剪影年轮的预览样例：一镜一个时代站，只验模板本身的语法。
 *  它同时是「风格工作室」三帧预览的探针——改 manifest 的字幕/底色，这里立刻看得到。 */
const Shot: React.FC<{
  text?: string;
  textEn?: string;
  aspect?: 'portrait' | 'landscape' | 'square';
}> = ({ text = '', textEn = '', aspect = 'landscape' }) => (
  <FilmLayout text={text} textEn={textEn} aspect={aspect}>
    <SilhouetteStage camera="push" horizon={0.62} groundY={0.82} grain={0.12} vignette={0.35}>
      <GlowRect kind="fire" x={0.70} y={0.34} w={0.11} h={0.16} glow={0.55} flicker={0.08} />
      <SilhouetteWalk figureH={0.46} groundY={0.82} fromX={0.10} toX={0.86} pauseX={0.36} />
      <EraInscription text="约 2 万年前 · 某处洞穴" position="top-right" size={0.022} />
    </SilhouetteStage>
  </FilmLayout>
);

export default Shot;
