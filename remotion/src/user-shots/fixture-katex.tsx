/**
 * P4 夹具 3/3 —— KaTeX：公式字形正确 + 波面动画。
 *
 * 上半：用 katex.renderToString 渲染 y_i(t)=amp1·sin(1.5x_i−2t)+amp2·sin(1.1z_i−1.3t)，
 *       字形不是豆腐块（容器内装了 Noto + KaTeX 字体）。
 * 下半：同一公式驱动的波面网格——20×20 点阵做等距投影画成 SVG wireframe，
 *       每帧 t=frame/fps 推进。
 * 字段：waveAmp1（第一振幅 0.3）、waveAmp2（第二振幅 0.2）。
 */
import React from 'react';
import { useCurrentFrame, useVideoConfig } from 'remotion';
import { FilmLayout, F, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import katex from 'katex';
import 'katex/dist/katex.min.css';

const GRID = 20;
const X_RANGE = 3;
const Z_RANGE = 3;

function waveY(
  x: number,
  z: number,
  t: number,
  amp1: number,
  amp2: number,
): number {
  return amp1 * Math.sin(1.5 * x - 2 * t) + amp2 * Math.sin(1.1 * z - 1.3 * t);
}

function isoProject(
  x: number,
  y: number,
  z: number,
  scale: number,
  yScale: number,
  cx: number,
  cy: number,
): { sx: number; sy: number } {
  const sx = (x - z) * Math.cos(Math.PI / 6) * scale + cx;
  const sy = (x + z) * Math.sin(Math.PI / 6) * scale - y * yScale + cy;
  return { sx, sy };
}

const Shot: React.FC<{
  text?: string;
  aspect?: 'portrait' | 'landscape' | 'square';
  fields?: Record<string, unknown>;
}> = ({ text = '', aspect = 'portrait', fields = {} }) => {
  const frame = useCurrentFrame();
  const { width, fps } = useVideoConfig();
  const u = width / 1080;
  const body = useBody(aspect);

  const amp1 = (fields.waveAmp1 as number) ?? 0.3;
  const amp2 = (fields.waveAmp2 as number) ?? 0.2;
  const t = frame / fps;

  const formulaLatex = `y_i(t) = ${amp1}\\sin(1.5x_i - 2t) + ${amp2}\\sin(1.1z_i - 1.3t)`;
  const formulaHtml = katex.renderToString(formulaLatex, {
    displayMode: true,
    throwOnError: false,
  });

  const cw = Math.round(body.w);
  const formulaH = Math.round(body.h * 0.18);
  const waveH = Math.round(body.h - formulaH);
  const waveCx = cw / 2;
  const waveCy = waveH / 2;
  const scale = (cw * 0.35) / (2 * X_RANGE);
  const yScale = scale * 1.5;

  const rows: string[] = [];
  const cols: string[] = [];
  for (let i = 0; i <= GRID; i++) {
    const x = -X_RANGE + (2 * X_RANGE * i) / GRID;
    const rowPts: string[] = [];
    for (let j = 0; j <= GRID; j++) {
      const z = -Z_RANGE + (2 * Z_RANGE * j) / GRID;
      const y = waveY(x, z, t, amp1, amp2);
      const p = isoProject(x, y, z, scale, yScale, waveCx, waveCy);
      rowPts.push(`${p.sx.toFixed(1)},${p.sy.toFixed(1)}`);
    }
    rows.push(rowPts.join(' '));
  }
  for (let j = 0; j <= GRID; j++) {
    const z = -Z_RANGE + (2 * Z_RANGE * j) / GRID;
    const colPts: string[] = [];
    for (let i = 0; i <= GRID; i++) {
      const x = -X_RANGE + (2 * X_RANGE * i) / GRID;
      const y = waveY(x, z, t, amp1, amp2);
      const p = isoProject(x, y, z, scale, yScale, waveCx, waveCy);
      colPts.push(`${p.sx.toFixed(1)},${p.sy.toFixed(1)}`);
    }
    cols.push(colPts.join(' '));
  }

  return (
    <FilmLayout text={text} aspect={aspect}>
      <div
        style={{
          position: 'relative',
          width: '100%',
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
        }}
      >
        <F name="waveAmp1" value={fields.waveAmp1}>
          <div
            style={{
              width: '100%',
              height: formulaH,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 28 * u,
              color: TOKENS.ink,
            }}
            dangerouslySetInnerHTML={{ __html: formulaHtml }}
          />
        </F>
        <F name="waveAmp2" value={fields.waveAmp2}>
          <svg
            width={cw}
            height={waveH}
            style={{ display: 'block', marginTop: 8 * u }}
          >
            {rows.map((pts, i) => (
              <polyline
                key={`r${i}`}
                points={pts}
                fill="none"
                stroke={TOKENS.ink}
                strokeWidth={1}
                opacity={0.55}
              />
            ))}
            {cols.map((pts, j) => (
              <polyline
                key={`c${j}`}
                points={pts}
                fill="none"
                stroke={TOKENS.accent}
                strokeWidth={1}
                opacity={0.35}
              />
            ))}
          </svg>
        </F>
      </div>
    </FilmLayout>
  );
};

export const UserShot = Shot;
export default Shot;