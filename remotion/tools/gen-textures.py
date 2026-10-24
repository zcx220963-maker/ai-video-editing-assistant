#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成随镜像发的内置贴图（`public/assets/lib/`）。

为什么要有这一包（2026-10-24）：three 的材质只有一个纯色时，渲出来就是**塑料感**
——`meshStandardMaterial color="#fff"` 全身明暗一致、没有一个高频起伏，这正是
「3D 玩具模型」的来源。真实感的第一层不是多边形数量，是**表面有变化**：
粗糙度起伏、法线颗粒、金属拉丝。

三条资产通道（素材库取字节 / 用户上传 / 脚本产出）都拆掉了，`public/` 里只剩字体，
所以镜头代码一个贴图都拿不到。这一包是**第四条**：随镜像发的通用贴图，
写手按固定名字取，不依赖网络、不依赖用户上传。

全部程序化生成（纯 PIL，无 numpy），所以可复现：改参数重跑即可。
用法：
    python tools/gen-textures.py            # 写进 public/assets/lib/
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "public" / "assets" / "lib"
SIZE = 512


def _octave(rng: random.Random, cells: int, weight: float) -> list[float]:
    """一层值噪声：cells×cells 个随机数双三次放大到 SIZE×SIZE。"""
    small = Image.new("L", (cells, cells))
    small.putdata([rng.randrange(256) for _ in range(cells * cells)])
    big = small.resize((SIZE, SIZE), Image.BICUBIC)
    return [v * weight for v in big.getdata()]


def fractal(seed: int, octaves=((4, 0.30), (8, 0.20), (16, 0.16), (32, 0.14),
                                (128, 0.12), (256, 0.10)), grain: int = 14) -> Image.Image:
    """多倍频叠加的灰度场：低频给大块起伏，高频给颗粒。

    **高频那两层是必须的**（2026-10-24 踩的）：第一版最高只到 32 格，放大到 512 之后
    整张图是光滑的低频渐变——贴到球上肉眼看不出来，探针量出「有差像素 0.5%、
    高频能量 0.90→0.91」，等于没贴。球的 UV 把 512 texel 铺满约 600 px，
    要让它看得见，纹理本身就得有**接近逐像素**的内容。
    `grain` 是最后叠的白噪声：给法线贴图提供真正的梯度，光扫过才有颗粒跳动。
    """
    rng = random.Random(seed)
    acc = [0.0] * (SIZE * SIZE)
    total = sum(w for _, w in octaves)
    for cells, weight in octaves:
        for i, v in enumerate(_octave(rng, cells, weight / total)):
            acc[i] += v
    if grain:
        acc = [v + rng.uniform(-grain, grain) for v in acc]
    lo, hi = min(acc), max(acc)
    span = (hi - lo) or 1.0
    img = Image.new("L", (SIZE, SIZE))
    img.putdata([int((v - lo) / span * 255) for v in acc])
    return img


def roughness(seed: int = 7) -> Image.Image:
    """粗糙度贴图：中灰为主、有起伏。全黑=镜面、全白=完全漫反射，两端都不好看。"""
    f = fractal(seed)
    # 压到 [70, 215]：太黑会像抛光塑料，太白会把高光吃光
    return f.point(lambda v: 70 + v * 145 // 255)


def normal(seed: int = 11, strength: float = 2.2) -> Image.Image:
    """法线贴图：把同一张高度场求梯度，按 (x, y, z) 编成 RGB。

    没有它，`roughnessMap` 只是明暗变化；有了它，光扫过表面会有**颗粒感**的明暗跳动
    ——这才是「表面不是塑料」的观感来源。
    """
    h = fractal(seed)
    px = list(h.getdata())
    w = SIZE
    out = Image.new("RGB", (SIZE, SIZE))
    buf = []
    for y in range(SIZE):
        for x in range(SIZE):
            hl = px[y * w + (x - 1) % w]
            hr = px[y * w + (x + 1) % w]
            hu = px[((y - 1) % SIZE) * w + x]
            hd = px[((y + 1) % SIZE) * w + x]
            nx = (hl - hr) / 255.0 * strength
            ny = (hu - hd) / 255.0 * strength
            nz = 1.0
            ln = (nx * nx + ny * ny + nz * nz) ** 0.5
            buf.append((int((nx / ln * 0.5 + 0.5) * 255),
                        int((ny / ln * 0.5 + 0.5) * 255),
                        int((nz / ln * 0.5 + 0.5) * 255)))
    out.putdata(buf)
    return out


def brushed(seed: int = 23) -> Image.Image:
    """金属拉丝：横向细条纹 + 轻微噪声。给 `metalness=1` 的材质用。"""
    rng = random.Random(seed)
    base = fractal(seed, octaves=((2, 0.2), (4, 0.2)))
    # 纵向抹开 → 横纹；再叠一层细噪声
    streaks = base.filter(ImageFilter.GaussianBlur(0.6))
    streaks = streaks.resize((SIZE // 2, SIZE // 4), Image.BICUBIC).resize(
        (SIZE, SIZE), Image.BICUBIC)
    px = list(streaks.getdata())
    out = [min(255, max(0, v + rng.randint(-14, 14))) for v in px]
    img = Image.new("L", (SIZE, SIZE))
    img.putdata(out)
    return img


def env_studio(w: int = 256, h: int = 128) -> Image.Image:
    """等距柱状（equirect）环境贴图：一块「暗房 + 一盏主光 + 一块冷补光」的假影棚。

    为什么需要它（2026-10-24）：`metalness` 的物理含义是「这个表面反射环境」——
    **没有环境贴图时金属就是黑的**。所以纯色材质做金属件只有两条路：渲成黑块，
    或者干脆不敢用金属。有了这张，金属/玻璃才有上下明暗与高光条，这是「写实感」
    的第二层（第一层是法线贴图的表面颗粒）。

    equirect 的坐标约定：u 是经度（0→2π），v 是纬度（0=天顶，1=天底）。
    所以：
      · 顶部一圈偏亮、底部压黑——这就是「天光 + 地面吸收」，金属球靠它分出上下；
      · 主光在 u≈0.25 的斜上方，暖白，负责高光；
      · 补光在 u≈0.75，冷青，负责暗部的色相（不给就是死黑）。
    做成小图再双三次放大：这些亮斑本来就该是软的，逐像素算 1024×512 只是浪费。
    """
    px = []
    for y in range(h):
        v = y / (h - 1)                     # 0 天顶 → 1 天底
        for x in range(w):
            u = x / (w - 1)
            # 天光：天顶最亮、地平线次之、天底压黑。**用 smoothstep 过渡**——
            # 第一版是 `max(0, 1-|v-0.5|*4)`，渲到球上是一条硬邦邦的横线
            # （第一张图就是那样），像贴了一张纸。
            if v < 0.5:
                t = v / 0.5
                base = 0.62 + (0.30 - 0.62) * (t * t * (3 - 2 * t))
            else:
                t = (v - 0.5) / 0.5
                base = 0.30 + (0.05 - 0.30) * (t * t * (3 - 2 * t))
            # 地平线一圈柔和的辉光（真实环境里那是地面反光）
            base += 0.10 * 2.718281828 ** (-((v - 0.5) / 0.11) ** 2)
            r, g, b = base * 150, base * 168, base * 205      # 冷蓝的天光
            # 主光（暖白）：u=0.25、v=0.22
            d = ((u - 0.25) * 2.2) ** 2 + ((v - 0.22) * 1.4) ** 2
            key = max(0.0, 1 - d / 0.075) ** 2 * 2.2
            r += key * 255; g += key * 240; b += key * 210
            # 补光（冷青）：u=0.75、v=0.38，弱一些
            d2 = ((u - 0.75) * 2.4) ** 2 + ((v - 0.38) * 1.5) ** 2
            fill = max(0.0, 1 - d2 / 0.12) ** 2 * 0.70
            r += fill * 60; g += fill * 190; b += fill * 220
            px.append((min(255, int(r)), min(255, int(g)), min(255, int(b))))
    img = Image.new("RGB", (w, h))
    img.putdata(px)
    return img.resize((1024, 512), Image.BICUBIC)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items = {
        "roughness-noise.png": roughness(),
        "normal-grain.png": normal(),
        "metal-brushed.png": brushed(),
        "env-studio.png": env_studio(),
    }
    for name, img in items.items():
        p = OUT / name
        img.save(p, optimize=True)
        print(f"  {name:<24} {img.size[0]}×{img.size[1]}  {p.stat().st_size // 1024} KB")
    print(f"\n共 {len(items)} 张，写到 {OUT}")


if __name__ == "__main__":
    main()
