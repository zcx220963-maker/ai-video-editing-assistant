import React, { useMemo } from 'react';
import * as THREE from 'three';
import { useCurrentFrame, interpolate } from 'remotion';
import { FilmLayout } from '@dg/film';
import {
  StudioStage, ContactShadow, Scatter, Dust, Mark,
  MetalMaterial, CeramicMaterial, GlassMaterial,
} from '@dg/lib/style/cinema-studio';

/** 表盘刻度：一圈小铜钉，位置由固定步长算出来，不用随机。 */
const TICKS = Array.from({ length: 36 }, (_, i) => {
  const a = (i / 36) * Math.PI * 2;
  return { a, major: i % 3 === 0 };
});

/** 记录纸上的横线：四道印刷线，位置固定。 */
const RULES = [0.030, 0.010, -0.010, -0.030];

const Shot: React.FC<{
  text?: string;
  textEn?: string;
  aspect?: 'portrait' | 'landscape' | 'square';
  fields?: Record<string, unknown>;
}> = ({ text = '', textEn = '', aspect = 'landscape' }) => {
  const frame = useCurrentFrame();
  const swing = interpolate(frame, [12, 62, 90], [0, 1.12, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const needle = -Math.PI * 0.62 + swing * Math.PI * 1.18;

  // 橡胶管：一条从表冠垂到台面外的曲线，Tube 让它真的是一根圆管。
  const hose = useMemo(() => {
    const curve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.02, 0.115, 0.10),
      new THREE.Vector3(0.10, 0.075, 0.13),
      new THREE.Vector3(0.19, 0.016, 0.16),
      new THREE.Vector3(0.30, 0.012, 0.10),
      new THREE.Vector3(0.40, 0.010, 0.02),
    ]);
    return new THREE.TubeGeometry(curve, 72, 0.0105, 16, false);
  }, []);

  return (
    <FilmLayout text={text} textEn={textEn} aspect={aspect}>
      <StudioStage aspect={aspect} camera="push" light="night" floor="wood"
                   exposure={0.94} haze={0.5} grain={0.14} vignette={0.6}
                   cam={{ from: [-0.02, 0.30, 1.02], to: [0.01, 0.29, 0.92],
                          look: [-0.03, 0.11, 0.02] }}>
        {/* 表壳：金属外圈、下沉的瓷面、一层玻璃罩 */}
        <group position={[-0.06, 0.112, 0.02]} rotation={[Math.PI * 0.5 - 0.26, 0, 0.05]}>
          <mesh castShadow receiveShadow>
            <cylinderGeometry args={[0.108, 0.108, 0.040, 72]} />
            <MetalMaterial tone="steel" rough={0.30} env={1.2} />
          </mesh>
          <mesh position={[0, 0.0195, 0]} rotation={[-Math.PI * 0.5, 0, 0]} castShadow>
            <torusGeometry args={[0.101, 0.0052, 20, 72]} />
            <MetalMaterial tone="steel" rough={0.24} env={1.5} />
          </mesh>
          {/* 下沉的盘面：比外圈低 4mm，边上留一道暗缝 */}
          <mesh position={[0, 0.0145, 0]}>
            <cylinderGeometry args={[0.096, 0.096, 0.005, 72]} />
            <CeramicMaterial color="#ddd4c2" rough={0.55} />
          </mesh>
          {TICKS.map((t, i) => (
            <mesh key={i}
                  position={[Math.sin(t.a) * 0.079, 0.0176, Math.cos(t.a) * 0.079]}
                  rotation={[Math.PI * 0.5, -t.a, 0]}>
              <boxGeometry args={[t.major ? 0.006 : 0.003, 0.0012,
                                  t.major ? 0.019 : 0.010]} />
              <meshStandardMaterial color="#312a22" roughness={0.62} metalness={0} />
            </mesh>
          ))}
          <group rotation={[0, needle, 0]} position={[0, 0.0195, 0]}>
            <mesh position={[0, 0, 0.031]} castShadow>
              <boxGeometry args={[0.0036, 0.0018, 0.060]} />
              <meshStandardMaterial color="#8a2a1e" roughness={0.42} metalness={0.08} />
            </mesh>
          </group>
          <mesh position={[0, 0.021, 0]}>
            <cylinderGeometry args={[0.0085, 0.0085, 0.007, 24]} />
            <MetalMaterial tone="brass" rough={0.34} />
          </mesh>
          {/* 玻璃罩：只吃高光，不指望它真的折射 */}
          <mesh position={[0, 0.026, 0]}>
            <cylinderGeometry args={[0.098, 0.098, 0.003, 72]} />
            <GlassMaterial opacity={0.10} env={2.2} />
          </mesh>
          <mesh position={[0, -0.024, 0.104]} rotation={[Math.PI * 0.5, 0, 0]}>
            <cylinderGeometry args={[0.013, 0.015, 0.022, 24]} />
            <MetalMaterial tone="brass" rough={0.4} />
          </mesh>
        </group>

        {/* 橡胶管：一根真的圆管，从表冠垂到台面 */}
        <mesh geometry={hose} castShadow receiveShadow>
          <meshStandardMaterial color="#1b1917" roughness={0.82} metalness={0.02} />
        </mesh>

        {/* 台面上的记录纸：有厚度、有投影，纸上有四道印线 */}
        <group position={[-0.30, 0.0012, 0.13]} rotation={[0, 0.20, 0]}>
          <mesh castShadow receiveShadow>
            <boxGeometry args={[0.20, 0.0024, 0.13]} />
            <CeramicMaterial color="#c9c1b0" rough={0.86} />
          </mesh>
          {RULES.map((z, i) => (
            <mesh key={i} position={[0, 0.0026, z]}>
              <boxGeometry args={[0.15, 0.0009, 0.0016]} />
              <meshStandardMaterial color="#7c7362" roughness={0.8} />
            </mesh>
          ))}
        </group>

        <ContactShadow x={-0.06} z={0.03} size={0.36} opacity={0.85} />
        <ContactShadow x={-0.30} z={0.13} size={0.24} opacity={0.55} />
        <Scatter count={40} cx={0.10} cz={0.14} radius={0.14} size={0.002} color="#d9d1c0" />
        <Dust count={44} t={frame / 240} />
      </StudioStage>
      <Mark kind="circle" x={0.335} y={0.385} w={0.158} h={0.238}
            at={18} dur={26} color="#ffcf6b" width={5} opacity={0.85} />
    </FilmLayout>
  );
};

export default Shot;
