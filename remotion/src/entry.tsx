import React from 'react';
import { Composition } from 'remotion';
import type { CalculateMetadataFunction } from 'remotion';
import * as ShotModule from './user-shots/shot';
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

export const RemotionRoot: React.FC = () => (
  <Composition
    id="shot"
    component={UserShot as React.ComponentType}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={{ text: '', aspect: 'portrait' }}
    calculateMetadata={shotMeta}
  />
);
