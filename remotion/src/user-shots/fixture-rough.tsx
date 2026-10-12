/**
 * P4 夹具 1/3 —— rough.js：斜纹填充 + 两种眼形关键帧切换。
 *
 * 对应视频里 SMIL `values`/`keyTimes` 那一幕：一只眼在前半段睁开（椭圆 + 瞳孔，
 * 椭圆内填斜纹 hachure），后半段闭上（粗线 + 上下斜纹）。
 * 时刻只从 useCurrentFrame() 拿；随机不出现（本镜确定性）。
 * 字段：hatchGap（斜纹间距）、eyeSize（眼宽）。
 */
import React, { useRef, useEffect } from 'react';
import { useCurrentFrame, useVideoConfig } from 'remotion';
import { FilmLayout, F, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';
import { E } from '@dg/motion';
import rough from 'roughjs';

const Shot: React.FC<{
  text?: string;
  aspect?: 'portrait' | 'landscape' | 'square';
  fields?: Record<string, unknown>;
}> = ({ text = '', aspect = 'portrait', fields = {} }) => {
  const frame = useCurrentFrame();
  const { width, durationInFrames } = useVideoConfig();
  const ease = E.inOutCubic(frame / Math.max(1, durationInFrames - 1));
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const u = width / 1080;
  // 画布 = 母版真正裁出来的那条画面带，不再手写 144/416 这种猜测值
  const body = useBody(aspect);

  const midFrame = Math.floor(durationInFrames / 2);
  const isOpen = frame < midFrame;

  const cw = Math.round(body.w);
  const ch = Math.round(body.h);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const rc = rough.canvas(canvas);

    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    // eyeSize 是「填充比」不是像素数：像素数换画幅就失去意义（这份代码原来写死
    // 280，横屏带 1800×732 时那只眼只占 15% 宽——和模型犯的同一个错）。
    // 0.55 是下面椭圆的高宽比，所以可用轴取「带宽」与「带高÷0.55」里的小的那个。
    const fill = (fields.eyeSize as number) ?? 0.7;
    const eyeSize = fill * Math.min(canvas.width, canvas.height / 0.55);
    // 斜纹间距随眼宽等比：否则横屏那只眼的纹会密成一团黑
    const hatchGap = ((fields.hatchGap as number) ?? 8) * (eyeSize / 655);

    if (isOpen) {
      rc.ellipse(cx, cy, eyeSize, eyeSize * 0.55, {
        fill: TOKENS.accent,
        fillStyle: 'hachure',
        hachureGap: hatchGap,
        stroke: TOKENS.ink,
        strokeWidth: 3 * u,
      });
      rc.circle(cx, cy, eyeSize * 0.28, {
        fill: TOKENS.ink,
        stroke: TOKENS.ink,
      });
    } else {
      rc.line(cx - eyeSize / 2, cy, cx + eyeSize / 2, cy, {
        stroke: TOKENS.ink,
        strokeWidth: 4 * u * ease,
      });
      for (let i = 1; i <= 3; i++) {
        const inset = i * 12;
        rc.line(
          cx - eyeSize / 2 + inset,
          cy - i * hatchGap,
          cx + eyeSize / 2 - inset,
          cy - i * hatchGap,
          { stroke: TOKENS.accent, strokeWidth: 1.5 * u },
        );
        rc.line(
          cx - eyeSize / 2 + inset,
          cy + i * hatchGap,
          cx + eyeSize / 2 - inset,
          cy + i * hatchGap,
          { stroke: TOKENS.accent, strokeWidth: 1.5 * u },
        );
      }
    }
  }, [frame, isOpen, fields, u, cw, ch]);

  return (
    <FilmLayout text={text} aspect={aspect}>
      <div
        style={{
          position: 'relative',
          width: '100%',
          height: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <canvas
          ref={canvasRef}
          width={cw}
          height={ch}
          style={{ display: 'block' }}
        />
        <F name="hatchGap" value={fields.hatchGap}>
          <div
            style={{
              position: 'absolute',
              left: 16,
              top: 16,
              width: 64,
              height: 28,
              background: TOKENS.paper,
              borderRadius: 4,
              fontSize: 13,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: TOKENS.ink,
            }}
          >
            {`gap ${fields.hatchGap ?? 8}`}
          </div>
        </F>
        <F name="eyeSize" value={fields.eyeSize}>
          <div
            style={{
              position: 'absolute',
              left: 88,
              top: 16,
              width: 80,
              height: 28,
              background: TOKENS.paper,
              borderRadius: 4,
              fontSize: 13,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: TOKENS.ink,
            }}
          >
            {`fill ${fields.eyeSize ?? 0.7}`}
          </div>
        </F>
      </div>
    </FilmLayout>
  );
};

export const UserShot = Shot;
export default Shot;