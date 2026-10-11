/**
 * P4 夹具 2/3 —— three.js：四棱锥转镜头，顶点标号可读、遮挡关系正确。
 *
 * 对应 2025 新高考 I 卷那一幕：一个四面体绕轴旋转，五个顶点（S/A/B/C/D）
 * 标号始终可读，近面遮挡远面。3D 由 @remotion/three 的 ThreeCanvas 渲染，
 * 标号是 HTML 叠层——按帧算出旋转后的顶点 3D 坐标，透视投影到 2D。
 * 字段：pyramidColor（锥面色）、rotationSpeed（转速倍率）。
 */
import React from 'react';
import { useCurrentFrame } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import { FilmLayout, F, useBody } from '@dg/film';
import { TOKENS } from '@dg/tokens';

const RADIUS = 1.5;
const HALF_H = 1.4;

const VERTICES: { label: string; pos: [number, number, number] }[] = [
  { label: 'S', pos: [0, HALF_H, 0] },
  { label: 'A', pos: [RADIUS, -HALF_H, 0] },
  { label: 'B', pos: [0, -HALF_H, RADIUS] },
  { label: 'C', pos: [-RADIUS, -HALF_H, 0] },
  { label: 'D', pos: [0, -HALF_H, -RADIUS] },
];

const CAM_Y = 1.6;
const CAM_Z = 5;
const FOV = 45;

function project(
  v: [number, number, number],
  rx: number,
  ry: number,
  cw: number,
  ch: number,
): { x: number; y: number; z: number; visible: boolean } {
  const y1 = v[1] * Math.cos(rx) - v[2] * Math.sin(rx);
  const z1 = v[1] * Math.sin(rx) + v[2] * Math.cos(rx);
  const x2 = v[0] * Math.cos(ry) + z1 * Math.sin(ry);
  const z2 = -v[0] * Math.sin(ry) + z1 * Math.cos(ry);
  const vx = x2;
  const vy = y1 - CAM_Y;
  const vz = z2 - CAM_Z;
  if (vz >= -0.01) return { x: 0, y: 0, z: vz, visible: false };
  const fl = (ch / 2) / Math.tan((FOV * Math.PI) / 360);
  const sx = (vx / -vz) * fl + cw / 2;
  const sy = (-vy / -vz) * fl + ch / 2;
  return { x: sx, y: sy, z: vz, visible: true };
}

const Shot: React.FC<{
  text?: string;
  aspect?: 'portrait' | 'landscape' | 'square';
  fields?: Record<string, unknown>;
}> = ({ text = '', aspect = 'portrait', fields = {} }) => {
  const frame = useCurrentFrame();
  const body = useBody(aspect);

  const cw = Math.round(body.w);
  const ch = Math.round(body.h);

  const speed = (fields.rotationSpeed as number) ?? 1;
  const rx = (frame * speed) / 24;
  const ry = (frame * speed) / 14;
  const color = (fields.pyramidColor as string) ?? TOKENS.accent;

  const labels = VERTICES.map((v) => {
    const p = project(v.pos, rx, ry, cw, ch);
    const opacity = p.visible ? Math.max(0.25, Math.min(1, -p.z / 4)) : 0;
    return { label: v.label, x: p.x, y: p.y, visible: p.visible, opacity };
  });

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
        <ThreeCanvas
          width={cw}
          height={ch}
          camera={{ position: [0, CAM_Y, CAM_Z], fov: FOV }}
        >
          <ambientLight intensity={0.5} />
          <directionalLight position={[4, 6, 5]} intensity={2.2} color="#ffffff" />
          <mesh rotation={[rx, ry, 0]}>
            <coneGeometry args={[RADIUS, HALF_H * 2, 4]} />
            <meshStandardMaterial
              color={color}
              roughness={0.45}
              metalness={0.1}
            />
          </mesh>
          <mesh position={[0, -HALF_H - 0.4, 0]} rotation={[-Math.PI / 2, 0, 0]}>
            <planeGeometry args={[14, 14]} />
            <meshStandardMaterial color="#f6f1e4" roughness={0.9} />
          </mesh>
        </ThreeCanvas>

        <div
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            width: cw,
            height: ch,
            pointerEvents: 'none',
          }}
        >
          {labels.map((l) => {
            if (!l.visible) return null;
            const style: React.CSSProperties = {
              position: 'absolute',
              left: l.x - 14,
              top: l.y - 14,
              width: 28,
              height: 28,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: TOKENS.paper,
              borderRadius: '50%',
              fontSize: 15,
              fontWeight: 600,
              color: TOKENS.ink,
              opacity: l.opacity,
              zIndex: 10,
            };
            if (l.label === 'S') {
              return (
                <F key={l.label} name="pyramidColor" value={fields.pyramidColor}>
                  <div style={style}>{l.label}</div>
                </F>
              );
            }
            if (l.label === 'A') {
              return (
                <F key={l.label} name="rotationSpeed" value={fields.rotationSpeed}>
                  <div style={style}>{l.label}</div>
                </F>
              );
            }
            return (
              <div key={l.label} style={style}>
                {l.label}
              </div>
            );
          })}
        </div>
      </div>
    </FilmLayout>
  );
};

export const UserShot = Shot;
export default Shot;