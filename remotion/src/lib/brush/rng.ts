/**
 * 笔刷层的确定性随机源。
 *
 * 纪律与 `@dg/lib/draw/_pen` 同源：**不许 `Math.random`**。逐帧渲染会重跑同一份代码，
 * 用全局随机的话画面会逐帧抖，两次渲染也不是同一部片。这里给一个 mulberry32：
 * 同一个 `seed` 永远产出同一串数，跨机器、跨进程一致。
 */

/** 32 位整数哈希：把任意字符串/数字变成 seed（不同零件给不同基数即可错开）。 */
export function hashSeed(value: string | number): number {
  const text = String(value);
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** mulberry32：返回 [0,1) 的确定性序列。 */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 一段确定性抖动：同一个 seed 每次得到同一组 [-1,1] 的数。 */
export function jitter(seed: number, count: number): number[] {
  const next = rng(seed);
  return Array.from({ length: count }, () => next() * 2 - 1);
}
