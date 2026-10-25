# -*- coding: utf-8 -*-
"""把一条片子导成**可跑的 Remotion 工程**。

为什么要有这一步：交 mp4 是黑箱，交工程才叫交付。
Claude + Remotion 那条路的实质就是"产物是一个工程"——客户能改一个字、
重渲 30 秒，而不是重跑整条链。这是我们和它最大的形态差别。

从 `project.py` 拆出来（2026-10-25）：那边只该管"工程落盘/读回"，
导出是另一个关注点，混在一起两边都长不大。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from .project import dir_of, load

# ---------------------------------------------------------------------------
# 导出成**可跑的 Remotion 工程**
# ---------------------------------------------------------------------------
#
# 为什么要有这一步：交 mp4 是黑箱，交工程才叫交付。
# Claude + Remotion 那条路的实质就是"产物是一个工程"——客户能改一个字、
# 重渲 30 秒，而不是重跑整条链。这也是我们和它最大的形态差别。

_PKG = """{
  "name": "vibe-film-%(id)s",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "studio": "remotion studio",
    "render": "remotion render film out/film.mp4",
    "still": "remotion still film out/frame.png --frame=%(mid)d"
  },
  "dependencies": {
    "@remotion/transitions": "4.0.534",
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "remotion": "4.0.534"
  },
  "devDependencies": {
    "@types/react": "^19.0.0",
    "typescript": "^5.6.0"
  }
}
"""

_INDEX_TS = """import { registerRoot } from 'remotion';
import { RemotionRoot } from './Root';

registerRoot(RemotionRoot);
"""

_ROOT_TSX = """import React from 'react';
import { Composition } from 'remotion';
import Shot from './film';

/** 这一条片子的参数：%(secs)gs / %(fps)d fps / %(w)dx%(h)d。 */
export const RemotionRoot: React.FC = () => (
  <Composition
    id="film"
    component={Shot as React.ComponentType<Record<string, unknown>>}
    durationInFrames={%(frames)d}
    fps={%(fps)d}
    width={%(w)d}
    height={%(h)d}
  />
);
"""

_CFG = """import { Config } from '@remotion/cli/config';
Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
"""

_TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "noEmit": true
  },
  "include": ["src"]
}
"""

#: `@dg/*` 是我们仓库内部的路径别名，导出的独立工程里没有。
#: 所以给一份**最小可跑的实现**，并把生成模块里的 import 改成相对路径。
#: 只实现被真正用到的四个导出（E / lerp / seg / TOKENS）——多写一行都是负担。
_DG_MOTION = """import { Easing } from 'remotion';

/** 这一份是从主仓 `@dg/motion` 抄来的**最小实现**，只保留生成代码用到的四个导出。 */
export const E = {
  standard: Easing.bezier(0.4, 0, 0.2, 1),
  out: Easing.out(Easing.cubic),
  inOut: Easing.inOut(Easing.cubic),
};

export const lerp = (t: number, a: number, b: number) => a + (b - a) * t;

export const seg = (t: number, a: number, b: number, ease?: (x: number) => number) => {
  const x = Math.max(0, Math.min(1, (t - a) / Math.max(1e-6, b - a)));
  return ease ? ease(x) : x;
};
"""

_DG_TOKENS = """export const TOKENS = {
  fontStack: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", system-ui, sans-serif',
  fontSerif: '"Noto Serif SC", "Source Han Serif SC", serif',
};
"""

_README = """# %(title)s

一句话出的片子，导成了**可跑的 Remotion 工程**。改一个像素都不用重跑整条链。

    npm i
    npm run studio     # 打开 Remotion Studio 实时预览
    npm run render     # 渲成 out/film.mp4（%(frames)d 帧 / %(secs)gs / %(fps)dfps）

    npm run still      # 只渲一帧（out/frame.png），改完先看这一帧，几秒钟

## 里面有什么

| 文件 | 作用 |
|---|---|
| `src/film.tsx` | **整片**：%(shots)d 镜 + 转场 + 全局层，一次渲完 |
| `src/Root.tsx` | Composition 注册（时长/画幅/帧率） |
| `src/dg/motion.ts` | `@dg/motion` 的最小替身（E / lerp / seg） |
| `src/dg/tokens.ts` | 字体栈 |

## 改哪里

- 改某一句字幕：`src/film.tsx` 里搜那句话，`text="…"` 直接改。
- 改某一镜的画面：找 `const S07 = …`，里面就是那一镜的全部代码。
- 改节奏：`Root.tsx` 的 `durationInFrames`，以及 `film.tsx` 里各 `Series.Sequence` 的时长。
- 换配色：搜 `#0a1020` 这类色值，或改 `Sky` 的三段渐变。

> 这一份是**自包含**的：除了 `remotion` / `react` / `@remotion/transitions`，
> 不依赖主仓任何东西。
"""


def export_remotion(film_id: str, *, out: Path | None = None) -> Path:
    """把一条片子导成可跑的 Remotion 工程目录，返回目录路径。

    做的事只有三件：把 `film.tsx` 里的 `@dg/*` 别名换成相对路径、
    补上 Root/入口/配置、写一份 README。**不改画面代码一行**——
    改了就不是"同一个片子"了。
    """
    doc = load(film_id)
    src_dir = dir_of(film_id)
    film_tsx = src_dir / "film.tsx"
    if not film_tsx.exists():
        raise FileNotFoundError(f"工程 {film_id} 里没有 film.tsx")

    dst = Path(out) if out else (src_dir / "remotion")
    (dst / "src" / "dg").mkdir(parents=True, exist_ok=True)

    code = film_tsx.read_text(encoding="utf-8")
    code = (code.replace("from '@dg/motion'", "from './dg/motion'")
                .replace('from "@dg/motion"', 'from "./dg/motion"')
                .replace("from '@dg/tokens'", "from './dg/tokens'")
                .replace('from "@dg/tokens"', 'from "./dg/tokens"'))
    (dst / "src" / "film.tsx").write_text(code, encoding="utf-8")

    aspect = str(doc.get("aspect") or "portrait")
    w, h = {"portrait": (1080, 1920), "landscape": (1920, 1080),
            "square": (1080, 1080)}.get(aspect, (1080, 1920))
    fps = int(doc.get("fps") or 30)
    secs = float(doc.get("seconds") or 0) or sum(doc.get("secs") or [])
    frames = max(1, int(round(secs * fps)))
    shots = len(doc.get("bodies") or [])
    title = str(doc.get("title") or doc.get("prompt") or film_id)[:60]

    (dst / "package.json").write_text(_PKG % {"id": film_id, "mid": frames // 2},
                                      encoding="utf-8")
    (dst / "src" / "index.ts").write_text(_INDEX_TS, encoding="utf-8")
    (dst / "src" / "Root.tsx").write_text(
        _ROOT_TSX % {"frames": frames, "fps": fps, "w": w, "h": h, "secs": secs},
        encoding="utf-8")
    (dst / "src" / "dg" / "motion.ts").write_text(_DG_MOTION, encoding="utf-8")
    (dst / "src" / "dg" / "tokens.ts").write_text(_DG_TOKENS, encoding="utf-8")
    (dst / "remotion.config.ts").write_text(_CFG, encoding="utf-8")
    (dst / "tsconfig.json").write_text(_TSCONFIG, encoding="utf-8")
    (dst / "README.md").write_text(
        _README % {"title": title, "frames": frames, "secs": secs, "fps": fps,
                   "shots": shots}, encoding="utf-8")
    return dst


def zip_dir(src: Path, dst: Path) -> Path:
    """把一个目录压成 zip（导出工程给用户下载用）。"""
    import zipfile

    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src.parent))
    return dst
