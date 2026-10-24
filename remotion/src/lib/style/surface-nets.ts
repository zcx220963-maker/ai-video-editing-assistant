/**
 * Surface Nets —— 把一个**有符号距离场**连成一张连续网格。
 *
 * 为什么需要它：人形原来是把躯干、胳膊、腿各做成一个闭合的放样体，再摆到一起。
 * 两个闭合实体相交，交线一定是一道折——肩头那道棱、腋下那道棱、胯上那道棱，
 * 就是「乐高感」的来源，怎么调比例都去不掉，因为它是这个做法本身长出来的。
 *
 * 距离场解决的就是这个：各段还是那几段，但合并用 smooth-min，交界是**融**进去的，
 * 得到的是一张连续的皮，法线连续，没有棱。
 *
 * 为什么用 Surface Nets 而不是 Marching Cubes：后者要 256 行的三角表，抄错一行
 * 就是一个洞；Surface Nets 只在每个穿越格子里放一个顶点、按穿越的边连四边形，
 * 代码短、没有表。顶点法线直接取距离场的梯度，比三角面平均出来的更顺。
 */
import * as THREE from 'three';

type V3 = [number, number, number];

/** 角落序号：0..3 底(z-)，4..7 顶(z+)，低三位是 (x, y)。 */
const CO: V3[] = [
  [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0],
  [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1],
];
const ED: Array<[number, number]> = [
  [0, 1], [2, 3], [4, 5], [6, 7],
  [0, 2], [1, 3], [4, 6], [5, 7],
  [0, 4], [1, 5], [2, 6], [3, 7],
];

/**
 * @param f    距离场：负值在表面内、正值在外，单位与 `min`/`max` 一致（米）
 * @param min  采样盒的下角
 * @param max  采样盒的上角
 * @param step 网格边长（米）。人形全身 0.008 已经看不出棱；远景可以粗一档
 */
export const surfaceNets = (
  f: (x: number, y: number, z: number) => number,
  min: V3,
  max: V3,
  step: number,
): THREE.BufferGeometry => {
  const nx = Math.max(2, Math.ceil((max[0] - min[0]) / step) + 1);
  const ny = Math.max(2, Math.ceil((max[1] - min[1]) / step) + 1);
  const nz = Math.max(2, Math.ceil((max[2] - min[2]) / step) + 1);

  const vals = new Float32Array(nx * ny * nz);
  const gid = (i: number, j: number, k: number): number => i + nx * (j + ny * k);
  for (let k = 0; k < nz; k += 1) {
    const z = min[2] + k * step;
    for (let j = 0; j < ny; j += 1) {
      const y = min[1] + j * step;
      for (let i = 0; i < nx; i += 1) {
        vals[gid(i, j, k)] = f(min[0] + i * step, y, z);
      }
    }
  }

  const cx = nx - 1;
  const cy = ny - 1;
  const cz = nz - 1;
  const cid = (i: number, j: number, k: number): number => i + cx * (j + cy * k);

  const cell = new Int32Array(cx * cy * cz).fill(-1);
  const pos: number[] = [];
  const cv = new Float32Array(8);

  for (let k = 0; k < cz; k += 1) {
    for (let j = 0; j < cy; j += 1) {
      for (let i = 0; i < cx; i += 1) {
        let mask = 0;
        for (let c = 0; c < 8; c += 1) {
          const o = CO[c];
          const v = vals[gid(i + o[0], j + o[1], k + o[2])];
          cv[c] = v;
          if (v < 0) mask |= 1 << c;
        }
        if (mask === 0 || mask === 255) continue;
        // 顶点放在所有「穿过表面」的棱的交点平均处。
        let sx = 0;
        let sy = 0;
        let sz = 0;
        let n = 0;
        for (let e = 0; e < 12; e += 1) {
          const a = ED[e][0];
          const b = ED[e][1];
          const va = cv[a];
          const vb = cv[b];
          if ((va < 0) === (vb < 0)) continue;
          const t = va / (va - vb);
          sx += CO[a][0] + (CO[b][0] - CO[a][0]) * t;
          sy += CO[a][1] + (CO[b][1] - CO[a][1]) * t;
          sz += CO[a][2] + (CO[b][2] - CO[a][2]) * t;
          n += 1;
        }
        if (!n) continue;
        const id = pos.length / 3;
        pos.push(
          min[0] + (i + sx / n) * step,
          min[1] + (j + sy / n) * step,
          min[2] + (k + sz / n) * step,
        );
        cell[cid(i, j, k)] = id;
      }
    }
  }

  // 绕向不靠推公式：每个四边形拿「面法线 · 距离场梯度」判一次。距离场负在里面，
  // 梯度天然朝外，所以这个点积的符号就是该不该翻。省掉一整类绕反了的内外翻转 bug。
  const grad = (x: number, y: number, z: number, e: number): V3 => {
    const gx = f(x + e, y, z) - f(x - e, y, z);
    const gy = f(x, y + e, z) - f(x, y - e, z);
    const gz = f(x, y, z + e) - f(x, y, z - e);
    return [gx, gy, gz];
  };

  /** 格 → 顶点号。`cid` 只算下标，顶点号要从 `cell` 里取——这两者差一步，
   *  写漏了就是把「第几个格子」当「第几个顶点」用，连出来的面飞到体外。 */
  const at = (i: number, j: number, k: number): number => {
    if (i < 0 || j < 0 || k < 0 || i >= cx || j >= cy || k >= cz) return -1;
    return cell[cid(i, j, k)];
  };

  const idx: number[] = [];
  const quad = (a: number, b: number, c: number, d: number): void => {
    if (a < 0 || b < 0 || c < 0 || d < 0) return;
    const ax = pos[a * 3];
    const ay = pos[a * 3 + 1];
    const az = pos[a * 3 + 2];
    const bx = pos[b * 3];
    const by = pos[b * 3 + 1];
    const bz = pos[b * 3 + 2];
    const cxx = pos[c * 3];
    const cyy = pos[c * 3 + 1];
    const czz = pos[c * 3 + 2];
    const e1x = bx - ax;
    const e1y = by - ay;
    const e1z = bz - az;
    const e2x = cxx - ax;
    const e2y = cyy - ay;
    const e2z = czz - az;
    const nX = e1y * e2z - e1z * e2y;
    const nY = e1z * e2x - e1x * e2z;
    const nZ = e1x * e2y - e1y * e2x;
    const g = grad((ax + cxx) / 2, (ay + cyy) / 2, (az + czz) / 2, step * 0.5);
    if (nX * g[0] + nY * g[1] + nZ * g[2] < 0) idx.push(a, c, b, a, d, c);
    else idx.push(a, b, c, a, c, d);
  };

  for (let k = 1; k < nz - 1; k += 1) {
    for (let j = 1; j < ny - 1; j += 1) {
      for (let i = 1; i < nx - 1; i += 1) {
        const v0 = vals[gid(i, j, k)];
        const inside = v0 < 0;
        // 一条网格棱穿过表面，围着它的四个格子各有一个顶点，连成一个四边形。
        if (inside !== (vals[gid(i + 1, j, k)] < 0)) {
          quad(at(i, j - 1, k - 1), at(i, j, k - 1), at(i, j, k), at(i, j - 1, k));
        }
        if (inside !== (vals[gid(i, j + 1, k)] < 0)) {
          quad(at(i - 1, j, k - 1), at(i, j, k - 1), at(i, j, k), at(i - 1, j, k));
        }
        if (inside !== (vals[gid(i, j, k + 1)] < 0)) {
          quad(at(i - 1, j - 1, k), at(i, j - 1, k), at(i, j, k), at(i - 1, j, k));
        }
      }
    }
  }

  // 法线取距离场梯度：这是曲面**真正**的法线，比三角面平均出来的顺，
  // 白光沿边打过去是一圈连续的亮边，不会有面片感。
  const nrm = new Float32Array(pos.length);
  for (let i = 0; i < pos.length / 3; i += 1) {
    const g = grad(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2], step * 0.5);
    const l = Math.hypot(g[0], g[1], g[2]) || 1;
    nrm[i * 3] = g[0] / l;
    nrm[i * 3 + 1] = g[1] / l;
    nrm[i * 3 + 2] = g[2] / l;
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('normal', new THREE.Float32BufferAttribute(nrm, 3));
  geo.setIndex(idx);
  return geo;
};

export default surfaceNets;
