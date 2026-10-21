/**
 * 片壳注册表：`spec.shell` 的值 → 真正包住镜头的那一层。
 *
 * 名单是**静态**的，只由镜像决定：模型只能选一个名字，不能现场注册组件——与
 * 风格包「模板只能从系统注册表选」同一条纪律。Python 侧
 * `code_spec.SHELLS` 抄着同一份名单（schema 的 `enum` 用它），两处一起改。
 *
 * 为什么壳在 `entry.tsx` 这一层选，而不是让镜头代码自己 `<FilmLayout>`：
 * 镜头代码是模型现写的，它可以选择「这镜不套壳」；壳要是能漏，外框就又会回到
 * 「五十镜各画五十遍」的老路。挂在这里，每一镜都必经，选没选只看注入的 prop。
 */
import type React from 'react';
import { PaperFlatShell } from './style/paper-flat';

/** 壳拿到的就是整份 `input_props`（与镜头代码同一份），外加 children。 */
export type ShellComponent = React.ComponentType<Record<string, unknown>>;

export const SHELLS: Record<string, ShellComponent> = {
  'paper-flat': PaperFlatShell as unknown as ShellComponent,
};

/** 给启动期一致性检查与文档用的名单（与 `code_spec.SHELLS` 逐字相同）。 */
export const SHELL_NAMES = Object.keys(SHELLS);
