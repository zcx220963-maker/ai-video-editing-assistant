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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items = {
        "roughness-noise.png": roughness(),
        "normal-grain.png": normal(),
        "metal-brushed.png": brushed(),
    }
    for name, img in items.items():
        p = OUT / name
        img.save(p, optimize=True)
        print(f"  {name:<24} {img.size[0]}×{img.size[1]}  {p.stat().st_size // 1024} KB")
    print(f"\n共 {len(items)} 张，写到 {OUT}")


if __name__ == "__main__":
    main()
