/**
 * 纸底片壳（`shell: "paper-flat"`）——整片**不变**的那一层外框。
 *
 * 为什么要有这一层：从前「纸底 + 顶部色条 + 左上圆钟 + 期号小字 + 底部字幕」是写进
 * 每一镜提示词、由五十份模型现写的代码各自画的。同一件事画五十遍，就会有五十种钟的
 * 位置、五十种色条高度、五十种字幕落点；于是只能在出图之后再找一台「评审」去把它们
 * 挑出来——那是兜底，不是设计。
 *
 * 这一层把外框从镜头手里拿走，结构上就画不错：
 *   · 底色、字幕带、安全区由 `<FilmLayout>` 独占（镜头代码只能往画面带里填内容）；
 *   · 色条、圆钟、期号画在 `<FilmLayout>` **之后**，永远在最上面，镜头代码碰不到；
 *   · 镜头拿到的画布是 `useBody()` 报的那条带子，越界由母版裁掉。
 * 于是「字幕被主体压住」「钟跑到右下角」「这一镜忘了画色条」在结构上不可能发生，
 * 不需要任何看图返工。
 *
 * 不进 `FILM_KNOBS`：它是整片开关，但一旦无条件进键，所有没选片壳的老片子缓存键
 * 都会漂一次、整片白烧——与 `film_style`/`style_pack` 同一条纪律，只在真选了片壳
 * 时才进键（见 `code_spec.shot_cache_key`）。
 *
 * 颜色不写死：底色/墨色/强调色从注入的 `film_style` 取（认得出色板时 Python 侧会带
 * 三色），取不到才落 `TOKENS`。钟面与标签的位置来自 `FILM_GEOMETRY`，与母版的
 * 安全区是同一份数——两套几何迟早会分叉，而分叉的那一天没人会想到是这里。
 */
import React from 'react';
import { AbsoluteFill, getInputProps, useCurrentFrame, useVideoConfig } from 'remotion';
import { FilmLayout } from '../../FilmLayout';
import type { FilmLayoutProps } from '../../FilmLayout';
import { FILM_GEOMETRY, TOKENS } from '../../tokens';
import type { Aspect, FilmStyle } from '../../tokens';

export interface PaperFlatShellProps extends FilmLayoutProps {
  /** 整片不变的那行小字，如「第 001 期 · 风险不是概率」。由 spec.shell_label 注入。 */
  shellLabel?: string;
  /** 这一镜在整片时间轴上的起点（秒）。圆钟按绝对时间走，切点不重新起针。 */
  filmStartSec?: number;
}

/** 画幅判定：与母版同一个来源（宽高比），不读 `aspect` prop——那一栏镜头代码可以漏传。 */
function aspectOf(width: number, height: number): Aspect {
  const r = width / Math.max(1, height);
  if (r > 1.4) return 'landscape';
  if (r < 0.8) return 'portrait';
  return 'square';
}

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

/** 这一片的三种颜色：与 `FilmLayout` 读同一份注入（`film_style`/`artDirection`）。 */
function shellColors(): { bg: string; ink: string; accent: string; paper: string } {
  const raw = getInputProps() as Record<string, unknown>;
  const fs = (raw.film_style && typeof raw.film_style === 'object'
    ? raw.film_style : {}) as Partial<FilmStyle> & Record<string, unknown>;
  const hex = (v: unknown): string => {
    const t = String(v ?? '').trim().toLowerCase();
    return /^#[0-9a-f]{6}$/.test(t) ? t : '';
  };
  const pinned = hex(raw.highlightColor);
  return {
    bg: hex(fs.bg) || TOKENS.bg,
    ink: hex(fs.ink) || TOKENS.ink,
    accent: hex(fs.accent) || pinned || TOKENS.accent,
    paper: hex(fs.paper) || '#fffdf7',
  };
}

/**
 * 左上角那只圆钟。位置与直径全片一样，指针按**整片绝对时间**走：
 * `filmStartSec + frame/fps`，所以上一镜最后一帧与下一镜第一帧是连续的两秒，
 * 不会在切点重上一次弦。纯函数、无随机，逐帧可复现。
 */
const Clock: React.FC<{
  x: number; y: number; d: number; ink: string; paper: string; accent: string;
  elapsedSec: number;
}> = ({ x, y, d, ink, paper, accent, elapsedSec }) => {
  const r = d / 2;
  // 12 小时一圈：起点 12:00，之后按真实秒数走。
  const minuteAngle = ((elapsedSec % 3600) / 3600) * 360;
  const hourAngle = ((elapsedSec % 43200) / 43200) * 360;
  const hand = (angle: number, len: number, width: number, color: string) => (
    <line
      x1={0} y1={0}
      x2={0} y2={-len}
      stroke={color}
      strokeWidth={width}
      strokeLinecap="round"
      transform={`rotate(${angle})`}
    />
  );
  return (
    <svg
      width={d} height={d}
      viewBox={`${-r} ${-r} ${d} ${d}`}
      style={{ position: 'absolute', left: x, top: y }}
    >
      <circle r={r} fill={paper} stroke={ink} strokeWidth={Math.max(1.2, d * 0.035)} />
      {Array.from({ length: 12 }, (_, i) => (
        <line
          key={i}
          x1={0} y1={-r * 0.84} x2={0} y2={-r * 0.94}
          stroke={ink}
          strokeWidth={Math.max(1, d * 0.018)}
          transform={`rotate(${i * 30})`}
          opacity={0.75}
        />
      ))}
      {hand(hourAngle, r * 0.5, Math.max(1.6, d * 0.05), ink)}
      {hand(minuteAngle, r * 0.74, Math.max(1.2, d * 0.032), ink)}
      {/* 秒针用强调色，是整片唯一一处会动的强调色——一眼能看出「时间在走」 */}
      {hand((elapsedSec % 60) / 60 * 360, r * 0.8, Math.max(0.8, d * 0.014), accent)}
      <circle r={Math.max(1.2, d * 0.035)} fill={accent} />
    </svg>
  );
};

/**
 * 片壳本体。`entry.tsx` 在 `props.shell === "paper-flat"` 时用它包住镜头代码；
 * 没选片壳的片子仍走原来的 `<LayoutProbe><UserShot/></LayoutProbe>`，一个像素都不变。
 */
export const PaperFlatShell: React.FC<PaperFlatShellProps> = ({
  children,
  shellLabel = '',
  filmStartSec = 0,
  ...filmProps
}) => {
  const frame = useCurrentFrame();
  const { width, height, fps } = useVideoConfig();
  const aspect = aspectOf(width, height);
  const geo = FILM_GEOMETRY[aspect];
  const { ink, accent, paper } = shellColors();

  // 色条 6~10px：横屏 8、竖屏 10。它压在最顶上，不进安全区。
  const stripe = Math.round(clamp(height * 0.0075, 6, 10));
  // 圆钟直径跟画面走，但不小于 28、不超过 72。位置落在左页边距的中线上。
  const d = Math.round(clamp(width * 0.04, 28, 72));
  const clockX = Math.round(geo.gutter / 2);
  const clockY = Math.round(Math.max(stripe + 4, geo.topSafe / 2 - d / 2));
  const labelSize = Math.round(clamp(Math.min(width, height) * 0.026, 18, 30));
  const label = String(shellLabel || '').trim();

  return (
    <AbsoluteFill style={{ fontFamily: TOKENS.fontStack }}>
      <FilmLayout {...filmProps}>{children}</FilmLayout>
      {/* 外框层：在母版之后画，镜头代码永远盖不住这三样。 */}
      <AbsoluteFill style={{ pointerEvents: 'none' }}>
        <div style={{
          position: 'absolute', left: 0, top: 0,
          width: '100%', height: stripe, background: accent,
        }} />
        <Clock
          x={clockX} y={clockY} d={d} ink={ink} paper={paper} accent={accent}
          elapsedSec={Math.max(0, Number(filmStartSec) || 0) + frame / Math.max(1, fps)}
        />
        {label ? (
          <div style={{
            position: 'absolute',
            right: Math.round(geo.gutter / 2),
            top: Math.round(Math.max(stripe, (geo.topSafe - labelSize) / 2)),
            maxWidth: Math.round(width * 0.52),
            fontFamily: TOKENS.fontStack,
            fontSize: labelSize,
            lineHeight: 1.15,
            letterSpacing: Math.round(labelSize * 0.06),
            color: ink,
            opacity: 0.78,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            textAlign: 'right',
          }}>
            {label}
          </div>
        ) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
