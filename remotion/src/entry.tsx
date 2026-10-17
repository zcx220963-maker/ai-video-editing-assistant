import React from 'react';
import { Composition } from 'remotion';
import type { CalculateMetadataFunction } from 'remotion';
// 字体**挂在根上**，不挂在母版上（母版下线那一轮改的）：`@dg/fonts` 是随镜像发的
// 101 片 webfont 的注册表，副作用 import 一下才算把字族与字节拉进依赖图。以前它在
// `FilmLayout.tsx` 里——于是「镜头代码不套母版」就等于这一镜的字回落成默认字族，
// 而这种回落**不报错**，只在成片上看着不对。根文件是每一镜都必经的那一处。
import '@dg/fonts';
import * as ShotModule from './user-shots/shot';
import { LayoutProbe } from './lib/LayoutProbe';
import { ASPECTS } from './tokens';
import type { Aspect } from './tokens';

/** 镜头组件的取法：`export default` 优先（契约教的是这个），兼容模板与历史镜头的
 *  具名 `UserShot`。闸① 的 `dg/needs-shot-export` 在渲染前就拦掉「两个都没写」的文件，
 *  这段兜底只保证真漏过时报的是「这一镜没有可挂载的导出」，而不是 Remotion 内部那句
 *  「component 收到了 undefined」。 */
const resolved = (ShotModule as Record<string, unknown>).default
  ?? (ShotModule as Record<string, unknown>).UserShot;
if (typeof resolved !== 'function') {
  throw new Error('这一镜没有可挂载的导出：请写 `export default Shot;`'
    + '（或 `export const UserShot = Shot;`）');
}
const UserShot = resolved as React.FC<Record<string, unknown>>;

/** 一次请求 = 一镜 = 一个 Composition。
 *  时长/画幅/fps 全由调用方注入（闸②的落地处）：镜头代码里没有决定时长的权力。
 *  注意这一版的参数名是 `props` 而不是旧文档里的 `inputProps` —— 4.0.534 已把
 *  inputProps 合并进 props（Composition.d.ts:29），写 inputProps 会 TS2339 编译失败。 */
const shotMeta: CalculateMetadataFunction<Record<string, unknown>> = ({ props }) => {
  const p = (props ?? {}) as Record<string, unknown>;
  const aspect = (p.aspect as Aspect) ?? 'portrait';
  const geom = ASPECTS[aspect] ?? ASPECTS.portrait;
  return {
    durationInFrames: Number(p.durationInFrames ?? 150),
    fps: Number(p.fps ?? 30),
    width: Number(p.width ?? geom.width),
    height: Number(p.height ?? geom.height),
    props: { ...p, aspect },
  };
};

/** 镜头外面那一层**测量壳**（闸⑧ 的落点，2026-10-16 从母版里搬出来的）。
 *
 *  为什么必须在这一层：版面回读原来只在 `<FilmLayout>` 组件体内跑，而母版已下线、
 *  镜头代码自包含——于是自包含镜的版面读数恒为 `status:"missing"`，闸⑲-a / ⑳(c) / ㉕
 *  与回看环**一个数都没有**。entry 是每一镜都必经的那一处（字体挂在根上时用的就是
 *  同一个理由），所以探针挂在这里。
 *
 *  壳本身不改一个像素（`display: contents`，见 `LayoutProbe`），套了母版的镜由
 *  `LayoutProbe` 让位、不重复上报。 */
const MeasuredShot: React.FC<Record<string, unknown>> = (props) => (
  <LayoutProbe>
    <UserShot {...props} />
  </LayoutProbe>
);

export const RemotionRoot: React.FC = () => (
  <Composition
    id="shot"
    component={MeasuredShot as React.ComponentType}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={{ text: '', aspect: 'portrait' }}
    calculateMetadata={shotMeta}
  />
);
