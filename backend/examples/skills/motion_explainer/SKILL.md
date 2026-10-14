---
name: motion_explainer_skill
display: 图文科普片（零素材出片）
description: 【WORKFLOW SKILL】没有视频素材、用户只给一个选题时的出片流程：自己查资料 → 写每镜文案 → 每镜选一张shot配方卡+填参数（不写渲染代码）→ 人声旁白配逐词字幕 → 一条成片。适用于科普、历史、概念解释类短片，或用户明确说「我没有素材，你自己找内容做一条」。A zero-material workflow: research, write per-shot copy, select shot recipe cards and fill params, narrate and render.
sections: film-knobs, patch-motion, sound-design, image-channel, asset-motion, asset-parts, scale-safe, whiteboard-imagegen, subject-naming, motion-anchors, text-only-quota
---

# 何时用这条通道（与常规剪辑的分界）

只有一条判据：**这条片子的画面是不是代码画得出来的（不需要现成影像）。**

- 用户随消息给了素材、或素材库里检索得到要剪的影像 → 走 default_editing_workflow_skill
  （load_media → … → plan_timeline → render_video）。
- 用户给的是一个**选题**（「讲讲活字印刷怎么来的」「用 60 秒解释什么是 RPC」），
  没有任何素材 → 走本通道：**plan_motion → render_motion_code**。
- 两条路是两个终点，不要混着排：一条片子只出一个成片通道。

# 流程

1. **查资料**：web_search / fetch_url，可以多轮。每条要点记下出处（标题 + 链接）。
2. **写分镜** plan_motion：每镜一句文案 + **一整段适配后的 .tsx 代码** + 高亮词 + 可选旋钮 + 出处。
   代码不是从零写的——是从 shot 配方卡的 demo 实现适配来的（见下面「镜头代码合同」）。
3. **对账**：读返回里的 `estimated_sec`、`target_warnings`、`style_channels`、`style_warnings`，
   有偏差就先改分镜。
4. **出片** render_motion_code：分钟级到十分钟级。返回 `status=queued/running` 时
   **继续调 render_status**（同 artifact_id）直到 done/failed。
5. **出片之后还要改** → 看下面「局部改」。

# 镜头代码合同：选卡 + 适配（不从零写代码）

## 核心原则

**你不写渲染代码，你选 shot 配方卡 + 适配参数。**

项目内置 **124 张 shot 配方卡**（`shots/` 目录）+ **158 个 demo 实现**（技能目录 `demos/`）+
**8 套主题色板**（`remotion/src/themes/palettes.json`）。每张卡有调校过的参数、缓动、时间轴，
demo 实现是参数的真相——你的工作是**选卡 + 把 demo 代码适配到用户的文案和主题**，不是从零写。

## 124 张 shot 卡分 10 类

| 类别 | 卡数 | 用途 | 示例卡 |
|------|------|------|--------|
| **opening** | 11 | 开场镜/标题入场 | spotlight-hero-card, brand-ink-open, fracture, letterspace-materialize |
| **camera** | 10 | 3D空间运镜/2.5D相机 | basic-3d-scene, crash-zoom-punch, depth-layer-moves, space-camera-moves |
| **effects** | 15 | 视觉特效/光影/转场重音 | aurora-bloom-bg-flip, spotlight-sweep-moves, riso-print-hits, light-play-moves |
| **typography** | 22 | 文字动效/逐词入场 | flying-words, blur-slide, typewriter-moves, gradient-word-sweep, lead-word-zoom-assemble |
| **data** | 5 | 数据可视化动效 | chart-live-moves, before-after-slider-scrub, gauge-readout-moves, timeline-travel |
| **transition** | 15 | 镜间转场 | card-flip-reveal, circle-match-iris, gradient-transition, page-turn-transitions |
| **rhythm** | 8 | 节奏卡点/变速 | beat-cut-moves, speed-ramp-freeze, quad-split-parallel-scenes, sakuga-timing-shift |
| **ui-entrance** | 21 | UI元素入场/卡片堆叠 | deck-deal-flyin, card-stack, carousel-3d, wall-reveal-moves, row-embed |
| **interaction** | 11 | 交互模拟/光标/协作 | collab-cursor-moves, theme-switch-moves, glass-pill-dictation-typing |
| **outro** | 6 | 收尾/落款 | logo-shrink-wordmark-lockup, grain-dissolve, neon-triple-marquee |

## 选卡方法

**先读 `shots/INDEX.md`（一次加载技能调用）**——它列了全部 124 张卡的卡名、能量、一句话描述，
以及每张卡 demo 的**文件名**（`derived_from` 就照抄那一栏），扫完就能选卡，
不要逐张调「加载技能」读卡全文。

选卡流程：
1. 读 `shots/INDEX.md`（1 次调用）→ 按选题选 3-5 个类别
2. 从选中类别里挑 1-3 张卡（看能量搭配：开场高→中段混合→收尾中）
3. 只读选中卡的全文（每张卡 1 次调用，总共 ≤ 15 次）
4. 卡全文有：意图、动效核心、参数表（`| 参数 | 典型值 | 调节手感 |`）、已知坑、参考实现路径

**不要读超过 15 张卡。** 124 张卡不需要全看，INDEX.md 的一句话描述足够选卡。
怎么选、选多少由你根据选题自己决定。

## 反 PPT 语汇清单（选卡时先排除）

卡库是 video-shotcraft 自家的**产品宣传片**语汇库（约 85/158 份 demo 里带着它自己的 logo），
而这条通道做的是**科普片**。所以选卡与适配时要主动把下面这批**产品宣传片语汇**挡在门外——
它们不是"不好看"，是**与选题无关**：观众看到侧栏、看板、Changelog，会以为这是一场产品发布会。
这是成片「像产品发布会」的机械原因，比母版版式更致命。

**禁**（出现在科普片里就是错位）：

- **产品界面**：侧栏 / 顶栏 / 看板 / Dock / ⌘K 命令面板 / 通知气泡 / 设置面板 / 标签页
- **发布与版本**：Changelog / Release notes / 版本号时间轴 / 「New in …」海报
- **增长与运营**：价格表 / 套餐卡 / 用户数看板 / 留存曲线 / 评分星条
- **协作与光标**：多光标协作 / 头像组 / 批注气泡 / 打字指示器
- **卡片套路**：卡片堆叠 / 轮播 carousel / 九宫格功能墙 / 特性列表打勾

**改用**（同一件事，换成科普片自己的说法）：

| 不要 | 改用 |
|------|------|
| 侧栏 / 看板 | **剖面**、**分层示意图**（把结构切开看内部） |
| Changelog / 版本时间轴 | **真实历史时间轴**（有年份、有实物） |
| 增长看板 / 留存曲线 | **数据曲线**、**对照图**（把量画出来） |
| 卡片堆叠 / carousel | **地图**、**流程示意**、**比喻图**（一个东西变成另一个东西） |
| 界面高亮 / 点击演示 | **器物特写**、**手绘描线**（白板族） |

判据是「这一镜画的是**选题里的那个东西**，还是**某个产品的界面**」——卡库给了你骨架，
但骨架要装的是**你的选题**，不是 demo 里那家公司的界面。同一张卡换掉里面的「界面元素」
与「文案」就是一张科普卡，这一步就是下面「适配 demo 代码」的第 3、4 条。

## 适配 demo 代码

每张卡的「参考实现」指向 `demos/<category>/<card-name>/` 下的 TSX 文件。
**目录名是卡名，里面的文件名不是**——`camera/basic-3d-scene` 下那份叫 `Basic3DScene.tsx`、
`camera/depth-layer-moves` 那份叫 `MultiplaneReal.tsx`，**多数卡都对不上**。所以 `derived_from`
**照抄 `shots/INDEX.md` demo 栏里的 relpath**（那一栏写的就是盘上真有的文件名），
别照卡名拼 `<卡名>.tsx`——拼错的名字取材轮当场打回。适配步骤：

1. **先取材，再写码**：把这一版分镜写成**只有骨架**的一份（每镜带 `id`/`text`/`card`/
   `derived_from`，**一镜都不带 `code`**）交给 `plan_motion`。这一轮叫**取材轮**：它不回
   分镜账、不落库，只把每一镜被引用 demo 的**全文**（逐件带 `relpath`/`bytes`/`sha16`/`text`）
   回给你。然后再把每镜的 `code` 补上重调一次——**取材轮与分镜轮的所有镜都必须是同一种
   形状**（要么都不带 code，要么都带），混着交会被打回。
   为什么必须这样：闸（`derived_from`）会按**代码骨架与所声明 demo 的相似度**核对你的
   声明。没读到 demo 就写出来的代码，要么过不了闸，要么只能靠猜着"像"它——那正是要避免的。
   取材轮单件的体积有上限，超线的几件在 `forage_oversize` 里点名：它们是**没给**不是
   **没内容**，用 `load_skill(name="motion_explainer_skill", path="<relpath>")` 逐件取，
   不要凭记忆写那几镜。
2. **替换 import 路径**：demo 用相对路径（`../../_fixtures/Motion`），你必须改成别名：
   - `../../_fixtures/Motion` → `@dg/motion`
   - `../../_fixtures/Polish` → `@dg/polish`
   - `../../_fixtures/Brand` → `@dg/brand`
   - `../../_fixtures/Look` → `@dg/fixtures/Look`
   - `../../_fixtures/Fixtures` → `@dg/fixtures/Fixtures`
   - `../../_fixtures/PageCam2D` → `@dg/fixtures/PageCam2D`
3. **替换文案**：demo 里的硬编码文字（如 `"Promos took weeks"`）换成用户的文案
4. **替换颜色**：demo 里的颜色常量换成所选主题的色板值
5. **调参数**：按参数表和用户需求微调时长、缓动、幅度
6. **包 FilmLayout**：把适配后的组件包进 `<FilmLayout>` 母版（字幕带、安全区、字阶都在母版里）

## 原创哨兵 `<original>`（卡库表达不了的那几镜）

`derived_from` 必填，默认写「这一镜适配自哪份 demo」的 relpath。但卡库是**产品宣传片**语汇库，
总有几镜的母题它表达不了（一条真实的数据曲线、一张剖面图、一个比喻）。这几镜可以走**原创哨兵**：

- 把 `derived_from` 写成**单独一项** `["<original>"]`——声明「这一镜不从任何 demo 派生」，
  闸（derived_from）就跳过骨架相似度比对，你按选题从零写这一段代码。
- **原创不等于免检**：lint（闸①）、闸④（画面非空/带内填充）、闸⑧（版面）、闸⑩（字色对比度）
  照样逐条判它。哨兵免掉的**只有**「必须像某份 demo」这一条。
- **配额**：一条片子最多 20% 的镜走原创（闸⑳；6 镜片的上限是 1 镜）。卡库是这条链的
  风格上限，哨兵是给「卡库表达不了的那几镜」开的**窄口子**，不是绕开取材的常规路。
  占多数的镜仍须照 `shots/INDEX.md` 那一栏声明它派生自哪份 demo。
- **哨兵必须单独出现**：`["<original>", "data/chart-live-moves/X.tsx"]` 这种混写会被打回
  ——那等于「每镜都挂哨兵」只差一步。出片回执里 `original_shots` 如实报出哪几镜走了原创。

## 可用 import

```tsx
// 基础动效工具（从 video-shotcraft 移植）
import { E, seg, lerp, rand, useT, DesignStage } from "@dg/motion";
import { EASE, Grain, Vignette, bezier, ramp, mix, fitText } from "@dg/polish";
import { BRAND } from "@dg/brand";

// 可复用组件库
import { Caption } from "@dg/lib/Caption";
import { FlashCut } from "@dg/lib/FlashCut";
import { DigitRoll } from "@dg/lib/DigitRoll";
import { ClipCard } from "@dg/lib/ClipCard";
import { FlatPanel } from "@dg/lib/FlatPanel";
import { PageCam } from "@dg/lib/PageCam";
import { VerticalTicker } from "@dg/lib/VerticalTicker";

// 主题色板
import palettes from "@dg/themes/palettes.json";

// 母版（字幕带、安全区、字阶、配色）
import { FilmLayout, useBody, F } from "@dg/film";
import { TOKENS } from "@dg/tokens";

// Remotion 核心
import { interpolate, useCurrentFrame, useVideoConfig, random, AbsoluteFill, staticFile } from "remotion";

// 画法通道（在白名单里，按需引入）
import { fitText as remFitText } from "@remotion/layout-utils";
```

## 8 套主题色板

每套 7 键：`page`/`surface`/`field`/`text`/`muted`/`accent`/`border`。

| 色板 | page | accent | 风格 |
|------|------|--------|------|
| `vintage-kraft` | `#c8aa7d` | `#8b4937` | 复古牛皮纸 |
| `deep-ocean` | `#0b1620` | `#7cc8b4` | 深海科技 |
| `obsidian-violet` | `#14121b` | `#bca0e8` | 黑曜紫 |
| `modern-light` | `#f4f7fb` | `#2563eb` | 现代浅色 |
| `midnight` | `#090f1a` | `#61d9ef` | 午夜蓝 |
| `solar-pop` | `#f9faf6` | `#527434` | 阳光活泼 |
| `coral-burst` | `#fffffe` | `#d94e59` | 珊瑚迸发 |
| `color-play` | `#f7f5fb` | `#7354b5` | 色彩游乐 |

科普视频推荐主题：`deep-ocean`（科技感）、`midnight`（数据感）、`obsidian-violet`（概念感）。

## 3 档风格组合（整片默认，不是逐镜配额）

`remotion/src/themes/palettes.json` 的 `preset` 段定义三档**推荐组合**（色板 + 质感 + 字幕档 + 主体来源 + 素材源白名单）。
这三档只用来定**整片默认**那一个值（写进 spec 顶层那一份）；**逐镜的色板与质感是自由的**——
每一镜照它自己的题材从八套色板里挑一套，写在**那一镜的代码**里（`const palette = palettes["…"];`）。

| 档 | 色板 | texture | 字幕档 | 主体来源 | 适用选题 |
|----|------|---------|--------|----------|----------|
| `ink-archive` | `vintage-kraft` / `modern-light` | `film` 或 `bleach` | `torn_highlight` | 位图优先：老照片、纸纹、手绘 | 历史、器物、人物传记 |
| `tech-data` | `modern-light` / `color-play` | `none` | `full` | 代码优先：矢量图、公式、数据卡、图标 | 概念解释、数据对比、流程 |
| `night-concept` | `deep-ocean` / `midnight` / `obsidian-violet` | `glow` | `torn_highlight` | 3D/光优先：三维、光效、人物剪影 | 科学、宇宙、抽象概念 |

整片默认把 `palette`/`texture`/`subtitle_mode` 写进 spec 顶层；**逐镜覆盖**写在那一镜上。
「多风格」与「风格漂移」的分界不在「只选档」，而在闸——⑲ 数**色板种数 ≥ `ceil(镜数/4)`**、
⑨ 判跨镜同构、⑩ 判字色对比度。**八套色板都要用上**：一条 20 镜片里 4~8 套轮着配，
二十镜共用一个调子会被闸⑲ 打回（那正是「同一张 PPT 换字」）。

**关于「素材源白名单」**：preset 里那两栏只是**登记意图**，不是你能点的源。真正能传给
`search_illustration` 的 `source` 只有一个地方说了算——**工具自己的枚举**（今天只有
`wikimedia-commons` 与 `iconify` 两条腿在活库里有行）。preset 里那几个「待跑」的名字
（`ambientcg`/`open-peeps`/`polyhaven`）腿还没通，点了会收到 `reason=source_not_available`。
**别照 preset 里的名字填 `source`**，也别因为点不到就以为「库里没有这类素材」——那两件事的
回执 `reason` 不一样。大部分母题本来就走**代码画**（见「画面丰富度合同与风格配额」），
不依赖这几个源。

## 代码模板

```tsx
import React from "react";
import { useCurrentFrame, AbsoluteFill, staticFile } from "remotion";
import { FilmLayout, useBody, F } from "@dg/film";
import { TOKENS } from "@dg/tokens";
import { E, seg, lerp, useT, DesignStage } from "@dg/motion";
import { EASE, Grain, Vignette, fitText } from "@dg/polish";
import palettes from "@dg/themes/palettes.json";

const palette = palettes["deep-ocean"];

const Shot = (props: any) => {
  const { text, highlight = [], words, subtitleMode, aspect, fields = {}, assets = [] } = props;
  const t = useT();
  const body = useBody(aspect);

  // === 从 shot 卡 demo 适配的动效代码 ===
  const flip = seg(t, 0.57, 0.64, E.inOutCubic);
  const rise = seg(t, 0.04, 0.56, E.outCubic);

  // fitText 自动按画幅缩放字号，字族用 TOKENS.fontStack
  const titleFit = fitText({ text: text, maxLines: 2, maxFontSize: TOKENS.sizeTitle });

  return (
    <FilmLayout text={text} highlight={highlight} words={words}
                subtitleMode={subtitleMode} aspect={aspect}>
      <DesignStage bg={palette.page}>
        {/* 适配后的画面内容 */}
        {/* 用 palette.page/palette.surface/palette.accent/palette.text 等主题色 */}
        {/* 用 E/seg/lerp/EASE 等动效工具 */}
        {/* 有资产时用 staticFile(assets[0].relpath) 引图，闸⑪ 会判它动没动 */}
      </DesignStage>
    </FilmLayout>
  );
};
export default Shot;
```

## 禁止事项

- **禁止从零写渲染代码**：必须从 shot 卡 demo 适配。先读卡 → 读 demo → 适配。
- **禁止 Math.random / Date.now / new Date**：用 `rand(seed)` 或 remotion 的 `random(seed)`。seed 必须给固定值。
- **禁止裸 div 画色块**：用 `DesignStage` + 主题色板。
- **禁止降档命门参数**：shot 卡「已知坑」里标注的参数不得低于典型值。
- **禁止外链**：所有资源必须通过 `staticFile` 引本镜 `assets` 声明过的本地资产，不引 http/https URL。
- **字体必须用白名单字族**：`Noto Sans CJK SC`、`Noto Serif CJK SC`、`Noto Sans Mono CJK SC`、
  `DejaVu Sans`、`DejaVu Sans Mono`、`DejaVu Serif` 等 18 个镜像内已装字族。
  lint 规则 `dg/font-family-whitelist` 逐属性检查，非白名单字族逐镜打回。
  用 `fitText` 时默认走 `TOKENS.fontStack`，不用手写字族名。

## 写法纪律（lint 规则在渲染前判）

- **字段标注**：用 `<F name="字段名" value={fields.字段名}>` 标注可调旋钮的位置，
  `F` 从 `@dg/film` 导入。lint 规则 `dg/field-drift` 检查：
  声明了却没人读（fields 里有但代码没引）、读了却没声明（代码引了但 fields 里没有）、
  不是这个字段自己的值（`<F name="x">` 里读的不是 `fields.x`）。
- **随机种子**：`rough.generator` 等 API 要传 seed，`seed 必须给`固定值，保证逐帧可复现。
- **音频时长**：长样本（>5s）要显式给 `durationInFrames`，否则会拖到动作结束后还在响。

## 官方写法（六条，只此六条）

1. 动效全由 `useCurrentFrame()`+`interpolate()` 承载——CSS `transition`/`animation` 逐帧渲不对。
2. 默认缓动取 `@dg/motion` 的共享件 `E.standard`（`Easing.bezier(0.16,1,0.3,1)`）与 `E.spring200`（`damping:200`），别自选曲线。
3. 观感线性给 `output:'perceptual-scale'`；台阶感给 `posterize:3`。
4. 位移/缩放/旋转用独立属性（`scale`/`translate`/`rotate`），不拼 `transform` 串。
5. `interpolate()` 写在 `style` 内联里（闸⑥ 靠它读字段标记）。
6. 秒→帧用 `useVideoConfig().fps` 换，不许写死 30。

# 一镜有哪些栏

`shots[]` 每一项：**id**（卡的 `card` 标识，必填）、**code**（必填）、**text**（必填）、
**highlight**、**fields**、**min_duration_sec**、**texture**（省略=跟整片默认）、**source**。

另有 **card**（选的哪张 shot 卡，必填）、**derived_from**（适配自哪个 demo，必填）、
**assets**（本镜用到的资产列表，可空），以及两个**版式档** **bleed** 与 **bg**
（满幅与底色，省略=常规版式，见下面「版式档」那一节）。

- `text` 容量按画幅：竖屏与方屏 ≤ 60 字、横屏 ≤ 110 字。**默认只进母版字幕带，不进镜头画面**
  （例外：`subtitle_mode: inline` 那一档，见「字幕模式」）。
- `highlight` 里每个词**必须是该镜文案的子串**，每镜 ≤ 4 个。
- **有旁白时不要写每镜时长**。镜头长度归真实语音时长。
- **无旁白时必须给每镜 `min_duration_sec`**：画面要给观众时间读字幕+看画面，每镜 5~15 秒。
- 首尾各留一镜：开场与收尾的呼吸镜（`min_duration_sec` 给 1.5~2.5）。
- `source` 如实填。没查到的不要写上去。
- `card` 填卡名（如 `aurora-bloom-bg-flip`），`derived_from` 填 demo 的 relpath
  （如 `camera/depth-layer-moves/MultiplaneReal.tsx`——**相对 `demos/` 的写法**，
  与 `demos/MANIFEST.json` 里的键一致；带 `demos/` 前缀的写法也收，会归一。
  **照抄 `shots/INDEX.md` demo 栏**：文件名与卡名多数对不上，别拿卡名去拼）。
  闸 `derived_from` 校验白名单读的就是那份 MANIFEST，不在清单里的路径逐镜打回。
- `assets` 每项含 `{name, sha256, ext}`（可选 `source`/`alpha`/`layer`）。`staticFile(name)`
  引图时，lint 规则 `dg/staticfile-asset-check` 双向对账：代码里引了但 `assets` 没声明 →
  打回；`assets` 声明了但代码没引 → 提醒。`sha256` 要**完整 64 位十六进制**——投递腿按它
  取字节、渲染前按它逐件对账，短一位整条都对不上。

# 时长与镜数

- 「X 分钟以内」是**上限**不是**目标**——90 秒也是「4 分钟以内」。`目标时长（秒）` 传给 plan_motion 的是上限。
- 没旁白时总时长 = 各镜 `min_duration_sec` 之和。
- 用户给的时长/画幅/帧率是约束条件。约束之间有冲突或偏差时，你自己判断怎么处理——能自决就自决，需要用户定就问。

# 文案合同（每镜那句话要撑起整条片子）

- **每镜至少落一个具体项**：数字、年份、地名、人名、器物名、专名、比例、因果链上的一环。
- **不要写引子镜**。开场那一镜也要给东西。
- **一句话只说一件事**。字幕条放不下第二件事——拆成两镜。
- **文案与画面互为注解，不是互相复述**。文案说「人口从 1 亿涨到 4 亿」，画面就该把曲线画出来。
- **术语第一次出现要给一句白话**。
- 相邻两镜**不要同一主语的两种说法**。

# 查资料：后端链与出处账

- web_search 的每条结果都带标题与网址（第一行是标题，其下是该页 URL），调 plan_motion 时把这些原样填进 `source`。
- 查不到就说查不到，不要编。`source` 留空比编一个强。
- 出处不进画面（除非用户要求），但进分镜卡和逐镜账。

<!-- section: film-knobs -->

# 画面质感档 texture

每镜可选 `texture`（省略=跟整片默认），5 档：

| 档 | 画面特征 | 适用 |
|----|---------|------|
| `none` | 无滤镜 | 默认 |
| `film` | 暗底+辉光+景深+颗粒+暗角 | 概念讲述、主线叙事 |
| `tv` | 电视质感+扫描线 | 复古、新闻感 |
| `glow` | 辉光增强 | 科幻、数据高亮 |
| `bleach` | 漂白+高对比 | 历史、纪实 |

# 字幕模式

4 档：`full`（全字幕）、`torn_highlight`（撕纸高亮，推荐）、`none`（无字幕）、
`inline`（**文字进画面**）。

前三档字幕带都在母版 `<FilmLayout>` 里画（位置、底色、字阶由母版定），镜头代码不用管字幕。
**第四档 `inline` 是唯一反过来的**：母版**不画**字幕带，`text` 交回镜头代码，由你自己排进
构图里——文字成为画面的一部分（压在插画上、跟着描线出现、排在版心一侧），而不是永远躺在
画面下方那条固定的白底圆角条里。

什么时候用 `inline`：这一镜的**文字本身就是画面**（标题镜、白板讲解的板书、大字压图），
或者你要的构图与「下面一条字幕带」打架（满幅插画、整屏场景）。
什么时候**不要**用：需要逐词点亮（`highlight` 靠母版的 `torn_highlight` 那套画法，`inline`
下它不会自动生效）、或者你只是不想排版——那用前三档，让母版替你排。
`inline` 下 `text` 的容量上限**照样按画幅算**（竖屏 ≤60 字），超了还是拆镜：上限是排版空间
的约束，不是字幕带专属的。

**这一档可以逐镜覆盖**：整片那一档写在 spec 顶层，某一镜要换就写**在那一镜里**
（`{"id": "s09", "subtitle_mode": "inline"}`）——逐镜那一份赢。典型配方是
「整片 `torn_highlight` + 标题镜与满幅插画镜 `inline` + 纯场景镜 `none`」。
没写覆盖的镜照旧跟整片那一档（`省略` 与 `写了和整片一样` 在画面上是同一件事）。
**注意成本**：字幕带是母版**画进这一镜切片**的像素，所以**改字幕档要重烧被改的那几镜**
（改整片那一档＝整片重烧）——它和「换转场 0 镜重烧」正好相反，定稿后再动。

# 版式档：满幅 bleed 与底色 bg

这两栏**每镜可选，省略＝今天的常规版式**（画面居中一条带子、左右留页边距、底色是母版的
`TOKENS.bg`）。它们改的是**构图**，不是滤镜——`texture` 那一档改的是画面上的颗粒与暗角，
这一档改的是画面**铺多大、底色是什么**。

| 栏 | 取值 | 省略时 |
|----|------|--------|
| `bleed` | `true`（布尔，**不是字符串**）| 有安全区的常规版式 |
| `bg` | `#rgb` / `#rrggbb` | 母版 `TOKENS.bg` |

**`bleed: true` ＝满幅**：画面带就是整帧，左右页边距与上下安全区全部去掉。为什么要有它——
不写这一栏时，二十镜二十个母题全被裁进同一个居中的框里，这是成片「像 PPT」的结构原因。
**这一栏是纪律，不是「想写才写」的可选项**——闸⑲ 会数它，两条下限：

- **用深色板的镜必须写**：`midnight` / `deep-ocean` / `obsidian-violet` 这三套的 `page` 都是
  深底，配常规版式就是「页边一圈奶油、中间一块深色」。这几镜写 `bleed: true` 并同时给 `bg`
  （用该镜色板的 `page` 值）。
- **整片每 5 镜至少 1 镜满幅**：满幅镜数 ≥ `ceil(镜数/5)`（20 镜 → 4 镜、10 镜 → 2 镜、
  6 镜 → 2 镜）。画**整屏插画、满版描线、大场景、背景要铺满**的镜头一定写它；
  常规图文镜可以留省略，但上面这两条下限照样要满足。

两条要注意的：
- **满幅镜的字幕带会贴到底部**（不再居中），母版会**按你给的 `bg` 亮度自动换字幕字色**
  （深底→浅字）。构图主体别压在最底下那一条上：竖屏约底部 120px、横屏约 72px 是字幕带
  占的地儿。母版已经把这条带子的位置算进 `useBody()`，**用 `useBody()` 量带子就不会压上**。
- **`bleed` 写成 `"true"` / `1` 一律不生效**（母版判的是严格的布尔 `true`），而且校验期就会
  被打回——别用字符串。

**`bg` 只收十六进制**：母版拿它当底色、闸④ 还拿它当「这一镜画了东西没有」的基准色，
写 `rgb()`/命名色/渐变会被打回（收了等于让闸④ 在这一镜上闭嘴）。深底镜给了 `bg`，
`texture` 那档就**别再加 `film`**（`film` 自带暗角与颗粒，压深底会糊成一片）。

**最有用的两个组合**：
- `bleed: true` + `subtitle_mode: inline` ＝**整屏插画 / 白板板书**，文字与图一起构图。
- `bg` + 常规版式 ＝ 在一支浅底片子里给某几镜**换底色**（夜里的一镜、切场景的一镜）。

```tsx
// 满幅 + 文字进画面：镜头代码自己铺满整帧、自己排字
const { x, y, w, h } = useBody();   // bleed 下就是 0,0,1080,1920（整帧）
```

# 转场与进出场（转场可逐接缝，进出场整片一档）

这两件事**默认都在计划卡上勾**，镜头代码一行都不用写——它们是整片的观感决定，
让二十份模型现写的代码各自插一小段动画，等于交给二十次掷骰子。
**唯一的例外是转场**：它在某几处接缝上可以逐镜覆盖（下面第四段），因为那个覆盖
写在分镜里、不进镜头代码，掷骰子的问题不存在。

**转场**（`transition`，镜与镜之间怎么接）：

| 档 | 观众看到什么 |
|----|-------------|
| `none` | 硬切，镜镜直接接（默认） |
| `fade` | 淡叠·前一镜淡出后一镜淡入 |
| `dissolve` | 溶解·两边互相化开 |
| `wipeleft` | 左擦·新画面从右往左推过来 |
| `wiperight` | 右擦·新画面从左往右推过去 |
| `slideup` | 上滑·新画面自下而上顶入 |
| `circleopen` | 圆开·从中心扩开 |

**叠多久**（`transition_sec`，只给四个档位）：0.2 / 0.4 / 0.6 / 0.9 秒。
转场是拿相邻两镜的**重叠区**做混合的，所以叠的时长必须短于相邻较短那一镜的一半——
3 秒的镜头配 0.9 秒已经接近上限，短镜（开场呼吸镜那种）要配更短的一档。
给了配不上短镜的档位，会在**拼接之前**被打回并指出是哪一处接缝。

**逐接缝覆盖**（`transition` 写在**某一镜**上，表示**它之后**那一处接缝用什么）：
专业动效里转场本来就是逐接缝的——整片统一淡叠，但「结论那一镜」用圆开推上去，
是常见做法。写法：把 `transition` 写在那一镜里（`{"id": "s07", "transition": "circleopen"}`），
它只改 s07 之后那一处接缝，其余各处照旧跟整片那一档；时长沿用整片 `transition_sec`。
**末镜写这一栏无效**（它后面没有接缝了）。
整片压根不转场（`none`）时，被覆盖的那一处取默认时长——所以「整片硬接、只有一处圆开」
是写得出来的。**成本优势在这一栏上兑现**：换它照样 0 镜重烧，改十次只多跑十次拼接。

**进出场**（`entrance` / `exit`，这一镜自己怎么开始、怎么结束）：
各 5 档——`none` / `fade`（淡入 / 淡出）/ `rise`（自下浮入 / 向上浮出）/
`drop`（自上落入 / 向下落出）/ `zoom`（轻推近 / 轻拉远）。
同一档名在入场是「进来」、在出场是「出去」，方向刚好相反。
窗口很短（每镜帧数的 10%、封顶 15 帧），是给整片定调用的，不是让观众盯着看的效果。
**这四条也不用转交**：母版自己去渲染入参里取（镜头代码里写了 `<FilmLayout text={…}>`
就够；多传也行，传了以你传的为准）。

**代价差在哪**（这条决定你什么时候可以随便改）：**换转场不重烧任何一镜**——
转场落在合成成片那一步，逐镜的切片一个字节都不动，改十次也只多跑十次拼接；
**进出场则要进缓存键**，因为它由母版逐帧画进每一镜的切片里，改一次就是整片重烧。
所以「先定进出场、再调转场」比反过来便宜得多。

<!-- section: patch-motion -->

# 局部改

出片后要改几个字或几镜，**不要重跑整条流程**：

1. **改文案/高亮词** → patch_motion_code（只改指定镜的 text/highlight，不重渲结构）
2. **改一镜的画面** → 重新选卡/适配那一段代码 → patch_motion_code
3. **改整体风格** → 换主题色板 → patch_motion_code（改 palette 引用）
4. **改某几镜的构图/字幕/接缝** → patch_motion_code 的 `shot_sets`（整镜改写，一条
   `{shot, set}`）。可改的栏以工具 schema 列出的名单为准，`bleed` / `bg` /
   `subtitle_mode` / `transition` / `texture` 都在里面。两处容易写错：`transition`
   写的是**这一镜之后**那处接缝（末镜写了不生效）；`bleed` 必须真是布尔。
   改 `transition` / `texture` **不重烧这一镜**，改 `subtitle_mode` / `bleed` / `bg` **要重烧**。

<!-- /section -->

# 闸表（模型看得到的全部闸）

| 闸 | 判什么 | 违规怎么办 |
|----|--------|-----------|
| ① | 镜头代码能编译、有默认导出、import 在白名单内、字族在白名单内 | 改到过 |
| ② | 时长/画幅/fps 合法 | 改参数 |
| ③ | 高亮词是文案子串 | 改 highlight 或 text |
| ④ | 画面非空+带内填充（`FILL_TARGET=0.60`→60%、`SPAN_MIN=0.45`→45%、`TIGHT_MIN=0.30`→30%、`EMPTY_MIN_RATIO=0.02`→2%） | 换卡/加内容 |
| ⑤ | 母版 token 完整、跨镜一致 | 补 props |
| ⑥ | 字段对账（fields 声明 vs 代码使用） | 补/删字段 |
| ⑦ | 画法通道达标（配额 `((6,3),(10,4))`） | 换卡/加通道 |
| ⑧ | 版面铺满、无叠字裁字 | 调参数/换卡 |
| ⑨ | 跨镜同构（`STRUCT_SAME=0.88`） | 换卡/调参数 |
| ⑩ | 字色对比度（`3:1` 正文 / `4.5:1` 强调） | 换色 |
| ⑪ | 资产动效（三态：static 打回 / animated 通过 / undetermined 未判） | 给资产加动效 |
| ⑮ | 版式族配额（卡名种类 ≥ `ceil(镜数/4)`；同一**族** ≤2 镜） | 换卡 |
| ⑯ | 主体具名（每镜 `subject` + 有图形主体的镜 `subject_channel` 与代码对账） | 补 subject / 改申报 |
| ⑲ | 风格配额（色板种数 ≥ `ceil(镜数/4)`、满幅镜数 ≥ `ceil(镜数/5)`；质感种数与 `bg` 申报率只提醒） | 换色板 / 写 `bleed` |
| ⑳ | 原创配额（走 `<original>` 哨兵的镜 ≤ 整片的 20%） | 改回派生自 demo |

闸 ⑦⑨⑩⑮⑯⑲⑳ 在 `plan_motion` 时就**硬拦**（不烧像素），改十遍只花几秒。
闸 ① 在容器 lint（渲染前）。闸 ②③④⑤⑥⑧ 在渲染时判。闸 ⑪ 在出片后判。

**未判不是通过**：闸 ⑪ 返回 `undetermined`（探针采不到资产盒）时，不是"通过了"——
是这一镜没有可判的资产，闸 ⑪ 对它没有发言权。只有 `animated` 才是通过。
镜数不到 6 不判配额——这一档不判配额，模型才知道未判不是通过。

# 画面丰富度合同与风格配额（四类母题怎么选通道）

一条片子要有画面，靠的是**每一镜都真的画了东西**，而且**画法不重样**。四类母题各有最顺手的
那条通道——先照这张表想，想不出怎么画再考虑出图（顺序见「素材从哪来」）：

| 母题 | 用哪条通道 | 怎么写 |
|------|-----------|--------|
| 器物、材质、草图、手绘感的形状 | `rough`（`roughjs`） | `rough.generator(...)` 画出的每个形状都要给 `seed`，不给每帧都在抖 |
| 立体体、场景、空间关系 | `three` + `@react-three/fiber` + `@remotion/three` | 真 3D，最慢的一档，只在真要立体感时用 |
| 公式、推导 | `katex` | 必须 `import "katex/dist/katex.min.css"`，不然渲出来是没上下标的纯文本 |
| 流程、曲线、几何、图示、白板描线 | `vector`（裸 `<svg>`/`<path>`/`<rect>`）或 `@remotion/shapes` | 白板描线那套见「白板 / 简笔描线族」 |
| 写实照片、实景、真人、真实材质 | `image`（`staticFile`） | **只在这几种要写实观感时才走**，见「素材从哪来」 |

**只 import 不调用那一行不算通道**——判据读的是代码体里真用得上的那个名字。配额（闸⑦，
≥6 镜三种通道、≥10 镜四种）与两条文字镜约束见下一节。

# 通道表（闸⑦）

5 条画法通道 + 1 条文字通道：

| 通道 | import 标志 | 用途 |
|------|------------|------|
| `rough` | `roughjs` | 手绘斜纹、草图质感 |
| `three` | `three`/`@react-three/fiber`/`@remotion/three` | 真 3D 场景、WebGL 渲染 |
| `katex` | `katex` | 数学公式 |
| `vector` | `<svg>`/`<path>`/`<rect>` 等 | 矢量图形 |
| `image` | `staticFile` | 图像/视频资产（本镜 `assets` 声明过的） |
| `text` | （fallback） | 纯文字+色块 |

配额：`CHANNEL_QUOTA = ((6, 3), (10, 4))`——`MIN_SHOTS_FOR_QUOTA=6`，
≥6 镜需三种画法通道，≥10 镜需四种。镜数不到 6 不判配额。
每镜用了哪些通道由代码字节里的 import 与标记自动识别。

另有两条文字镜约束（首尾镜不计入分子也不计入分母）：
- `MAX_TEXT_ONLY_SHARE = 1/3`：纯文字/纯色块镜不超过中间镜的三分之一。这两条是**打回**的。
- `CONCRETE_SHARE_TARGET = 0.40`（40%）：中间镜里含具体项（数字/引号术语/拉丁缩写）的占比目标 40%。这条只出提醒，不打回——地名人名机器认不出，所以不拦。

# 白板 / 简笔描线族（**用代码画，不出图**）

「白板动画」那种**一支笔把东西当场画出来**的观感，这条链**不需要出图**——`vector` 通道的
虚线偏移（`pathLength={1}` + `strokeDasharray="1"` + `dashoffset` 从 1 走到 0）就是白板描线
本身。三件套：

- **免测周长**：路径设 `pathLength={1}`、`strokeDasharray="1"`，让 `dashoffset` 1→0 跑一遍，
  线就长出来了——不用量真实周长，也不用 `getTotalLength()`。
- **笔头**：同一条路径再叠**三段渐细短虚线**跑在最前 + 一颗按弧长定位的实心笔尖。
  **没有笔头就只是「边框在变长」，有笔头才是「有人在画」**——这一颗是白板感的关键。
- **闭合闪**：描完那 2 帧把线冲深、加粗再回落，给「画完了」一个句号。

要抄这套手法，选这几张卡（全表见 `shots/INDEX.md`）：

| 卡 | 干什么 | 什么时候用 |
|----|--------|-----------|
| `ui-entrance/draw-svg-trace` | 一条带笔头的墨线沿**单个元素**轮廓跑一圈，把它「画」出来 | 点名一个器物/图表/卡片；元素级特写 |
| `ui-entrance/wall-reveal-moves`（B 式蓝图描线） | **整页**线框逐段画 + 区域点亮 | 一整屏蓝图/界面/地图成形 |
| `typography/marker-underline-title` | 马克笔给关键词画一笔下划线 | 标题里强调一个词 |
| `opening/stroke-segment-build` | 笔画分段搭建 | 开场把一个形状搭出来 |

两条纪律：① **描边路径必须与元素真实轮廓吻合**（圆角 `rx` 要一致）——线画的是 14px 圆角、
元素是 8px，交棒瞬间错型穿帮；② **一次点名画一个主体**，同屏多元素各画一圈读作加载动画。

**这一族配上 `bleed: true` 才是它该有的样子**：常规版式下画面被裁进中间一条带子里，满版描线
只能缩着画。三个开关合起来就是「白板讲解」那种观感——`bleed: true`（铺满整帧）+
`subtitle_mode: inline`（文字跟着笔迹进画面）+ 上面这几张卡之一。

# 版式族与主体具名（闸⑮⑯）

**每镜两个新字段**（与 `card`/`derived_from` 平级，缺 `subject` 会被 schema 直接拒）：

- `subject`：这一镜画面上**能被认出的那个东西**（名词短语，2~24 字）。写成
  「活字盘」「三条等高线」「一张 1900 年的地图」这种**器物/图形**的名字；
  **不许写抽象词**（理念/趋势/意义/文化/效率…——既画不出来也认不出来，闸 ⑯ 有一份
  拒绝名单）。这一格是给人看的：用户扫一眼卡面就知道每镜到底画了什么。
- `subject_channel`：**主体是用哪条通道画的**，取值五选一（`rough`/`three`/`katex`/
  `vector`/`image`）。规则两向都判：
  - 这一镜的代码里**有**画法通道 → 必填，且必须是代码里真用到的那条（`style` 那栏就是
    代码里认得的通道）。写着 `rough` 而代码里一个 `roughjs` 都没有 → 打回。
  - 这一镜只有文字与色块（⑦ 允许的那 ≤1/3）→ **不要填**。填了就是自报无证据。
    这一档的 `subject` 照样要写（字也可以是主体，如「「毕昇」两个字」）。

**版式族配额**（闸⑮，跟 ⑦ 一样在 `plan_motion` 时就硬拦）：

- 卡名（`card` 的完整值）种类 ≥ `ceil(镜数/4)`；
- 同一**族**——就是卡库的那个类别目录名（`card` 写成 `<类别>/<卡名>`，
  如 `data/chart-live-moves` 的族是 `data`）——**最多 2 镜**。

族配额是「整片不许一个版式重复摆」那条要求的机器形态：8 镜片只用了 `data` 一族、
或者同一个卡名摆三镜，都在 `plan_motion` 就被退回（不烧像素）。选卡时**先看类别**：
一条 10 镜片至少要用到 5 个不同的类别，卡库按类别分了十类，见 `shots/INDEX.md`。

# 镜头代码的 import 名单

lint 白名单 44 项（`ALLOWED_IMPORTS`），镜头代码只能引这些：

`react`、`react-dom`、`remotion`、`@remotion/paths`、`@remotion/shapes`、
`@remotion/motion-blur`、`@remotion/layout-utils`、
`@remotion/fonts`、`@remotion/captions`、`@remotion/rough-notation`、
`@remotion/noise`、`@remotion/animation-utils`、
`@remotion/effects/paper`、`@remotion/effects/halftone`、
`three`、`@react-three/fiber`、`@remotion/three`、`roughjs`、
`katex`、`katex/dist/katex.min.css`、
`@dg/film`、`@dg/tokens`、`@dg/fonts`、`@dg/motion`、`@dg/polish`、`@dg/brand`、
`@dg/fixtures/Motion`、`@dg/fixtures/Polish`、`@dg/fixtures/Brand`、
`@dg/fixtures/Fixtures`、`@dg/fixtures/Look`、`@dg/fixtures/PageCam2D`、
`@dg/lib/Caption`、`@dg/lib/ClipCard`、`@dg/lib/DigitRoll`、`@dg/lib/FlashCut`、
`@dg/lib/PageCam`、`@dg/lib/VerticalTicker`、`@dg/lib/FlatPanel`、
`@dg/lib/helpers/motion`、`@dg/lib/helpers/rand`、`@dg/lib/helpers/shake`、
`@dg/lib/helpers/camera`、`@dg/themes/palettes.json`。

两条注意（都是**静默失效**那一类，写错了不会报错、只是画面不对）：

- **效果件按子路径引**：`import { paper } from '@remotion/effects/paper'`、
  `import { halftone } from '@remotion/effects/halftone'`。写包名 `'@remotion/effects'`
  在**打包期不报错**，到渲染时才炸一句「`paper` is not a function」——那两件不在桶文件里。
- **`effects` 只挂在 `<Img>`/`<Solid>`/`<HtmlInCanvas>` 上**。挂到 `<div>`/`<AbsoluteFill>`
  上 React 既不报错也不警告，只是**一声不响地没效果**（实测两支渲出来逐字节相同）。

# TOKENS

`TOKENS` 是扁平对象，颜色在顶层不在 `.colors` 下。可用键：
`TOKENS.fontStack`/`fontSerif`/`fontMono`（字体栈）、
`TOKENS.sizeTitle`/`sizeBody`/`sizeCaption`/`sizeNote`（字阶）、
`TOKENS.bg`（底色）、`TOKENS.paper`（字幕条与纸片底色）、
`TOKENS.ink`（正文主色）、`TOKENS.accent`（强调色）、`TOKENS.hl`（高亮色）、
`TOKENS.gutter`/`safeTop`/`safeBottom`/`bandHeight`/`bandRadius`（版面尺寸）。

要版面尺寸就问 `useBody()`——它报回来的就是母版真正裁出来的那个盒。

母版画面带尺寸（去掉安全区与 gutter 后的画面区）：
- 竖屏 portrait：936×1524
- 横屏 landscape：1800×732
- 方屏 square：952×764

<!-- section: image-channel -->

# 资产与图像通道

本链支持图像资产——用 `staticFile` 引本镜 `assets` 声明过的本地文件。

## 素材从哪来（供给优先级，写死——**不要问用户**）

一镜要有画面主体时，按这个顺序自动往下走，不要就「用库里的还是画一张」去问用户：

1. **先用代码画出来——这是默认那条路，不是兜底。** 四条画法通道（`rough` 手绘斜纹 /
   `vector` 矢量 / `three` 真 3D / `katex` 公式）能画器物、曲线、示意图、几何、公式、地图、
   流程、剖面、时间轴——**绝大多数科普母题都在这四条通道的能力范围内**。代码画出来的东西
   有三个别处换不来的好处：**能动**（描线、生长、变形、跟着数据变）、**零成本零等待**
   （不出网、不抽卡、改了立刻重渲）、**可溯源**（画面出自你写的这段代码，同一个 prompt
   跑两遍是同一支片子）。**先在这条路上想一遍**，想不出怎么画再往下走。
2. **先检索素材库**（只有这一镜要的是写实/摄影/电影感时才走这一步）：真人、实景、真实材质、
   照片级光影——这四样代码画不出来。`search_illustration` 打本镜的**具体名词**（主体名 + 场景，
   不是主题词）。命中就用 `select_illustration` 取那一件——它把字节落进本会话素材册并回带
   `assets` 三键，免费、不联网、可溯源。
3. **未命中再出图**：`imagegen` 按提示词出一张（要页面「设置 → 出图后端」配好 provider
   与密钥；`model`/`size` 留空＝用该后端默认值）。回执里的 `asset` 三键可直接贴进
   `shots[].assets`。
4. **没配后端**：`imagegen` 回 `ok:false` + `degraded:true`，人话里写明「图像通道降级为素材库检索，本片 N 镜无图」。**这时不许自己编一句「图中显示了…」**——那是把没有的东西写成有（静默出一支没有图的片子才是失败，降级本身不是）。降级之后**回到第 1 步**：用四条画法通道把主体画出来，而不是硬塞一个空镜去撞闸⑦（纯文字镜虽然允许占 ≤1/3，`subject` 那一栏照样要填——字也可以是主体，闸⑯）。

这条顺序是**合同冻结**的（§9-Q27），实现上落在 `imagegen` 的工具描述与上面这四步：
模型读得到、机器判得动，所以既不必问用户，也不会滑出第五条路。

## assets 栏

每镜可声明 `assets` 数组，每项含：
- `name`：文件名（代码里 `staticFile("<name>")` 引用，也是渲染容器 `public/` 下的相对路径）
- `sha256`：完整 64 位十六进制 sha256——渲染前由执行层按它取字节并投递，模型只管填对
- `ext`：扩展名（如 `png`、`svg`）

可选字段：`source`（来源信息对象）、`alpha`（是否有 alpha 通道）、`layer`（`fg`/`bg`/`cutout`）。

## staticFile 对账

lint 规则 `dg/staticfile-asset-check` 双向对账：
- 代码里 `staticFile("foo.png")` 但 `assets` 没声明 `foo.png` → **打回**
- `assets` 声明了但代码没引 → **提醒**（可能是死声明）

## image 字段类型

`fields` 里可声明 `type: "image"` 的字段（`FIELD_TYPES` 6 档：
`number`/`color`/`text`/`bool`/`select`/`image`），让用户从素材库选图，值落 `fields.<name>`。

## 资产入库

渲染前，执行层按每镜 `assets` 栏的 `sha256` 取字节，经 `POST /stage-assets` 投到渲染容器
`public/<name>`（按 sha256 前 16 位对账 + 幂等落盘）。素材必须先入全局素材库；填一个库里没有的
sha256，渲染前一帧都不烧就会红。素材来源分类 `materials.origin` 支持 `'art'`（代码生成的素材）。

<!-- /section -->

# 字体白名单

字族只许从这两类里取，写别的**逐镜打回**（lint 规则 `dg/font-family-whitelist`，
逐 `fontFamily` 属性检查，含 JSX 属性与常量解析）：

**① 镜像里装着的**（构建期 `fc-list : family` 生成 `font-whitelist.json`，18 族）：
`Noto Sans CJK SC`、`Noto Serif CJK SC`、`Noto Sans Mono CJK SC`、
`DejaVu Sans`、`DejaVu Sans Mono`、`DejaVu Serif`、`DejaVu Math Text`、
`DejaVu Sans Condensed`、`DejaVu Sans Narrow`、`DejaVu Serif Condensed`、
`DejaVu Serif Display`、`DejaVu Sans Extralight`、
`Liberation Sans`、`Liberation Mono`、`Liberation Serif`
（CJK 另有 HK/JP/KR/TC 四个地区变体，同样在册）。

**② 我们自己随镜像发出去的 webfont**：`Noto Sans SC`。
它由母版 `@dg/film` 副作用 import `@dg/fonts` 当场加载（`@remotion/fonts` 的 `loadFont`，
101 片 woff2 落在 `public/fonts/`，`display: 'block'`——字形没到不先用回落字族顶着画）。
`TOKENS.fontStack` 的第二项就是它，所以走 `fitText`/`TOKENS.fontStack` 的写法天然用它。

**别写 `SF Pro Display`/`Avenir`/`Google Fonts` 那一类名字**：镜像里没有、渲染容器也不联网，
浏览器**不报错**、直接回落成默认字族——代码里写的和画面上出现的不是一回事（闸① 的
`dg/font-load-check` 就是为这条：`loadFont` 指的字节必须在 `public/fonts/` 里真存在）。

<!-- section: asset-motion -->

# 闸⑪：资产动效

闸 ⑪ 判**资产是否参与了动效**——静态摆图不算"用了资产"。

## 三态

| 态 | 含义 | 处置 |
|----|------|------|
| `static` | 资产在首帧与探针帧的盒/transform/opacity 完全相同 | **打回**：给资产加动效（位移/缩放/淡入/旋转） |
| `animated` | 资产在两帧之间有可观测的变化 | **通过** |
| `undetermined` | 探针采不到资产盒（没有 `<img>`/`<video>` 或盒为空） | **未判**：闸 ⑪ 对这镜没有发言权 |

**未判不是通过**。只有 `animated` 才是通过。

## 探针机制

母版 `FilmLayout` 在 `probeFrame - 2`（首帧附近）和 `probeFrame`（末帧附近）两个时点
扫一遍 `<img>`/`<video>`，记录 tag/src/box/transform/opacity。
`server.mjs` 比对两帧快照，按 src 匹配，比较盒/transform/opacity 是否变化。
<!-- /section -->

<!-- section: asset-parts -->
# 部件拼场景（闸㉕）：先取件，拼不出就明说

一镜要有「一个场景」的样子，**先取件再画**——这是默认写法，不是备选：

1. **取件**：`search_illustration(query="<本镜的具体物件名>", asset_role="part")`。
   `asset_role` 五档：`icon` 一枚符号 / `part` 拼图部件 / `scene` 一整幅画面 /
   `texture` 质感底 / `bg` 铺满屏的底图；空串＝未标。**按角色找件就点上这一栏**
   ——不传时未标的件也会进结果（「随便搜搜」不该因为这一栏空着就搜不到东西）。
   回执 `found[]` 每件带 `asset_role`、`scale_safe`、`width`/`height`、`sha256`。
2. **拼场景**：`select_illustration(asset_id=…)` 取 **4~6 件**，摆到不同位置、大小错开、
   彼此有前后或相邻关系。**一件放大铺满画面不是场景，是一枚徽章**——闸㉕ 判的就是这个
   形状：整镜只有 1 件主体 + 它铺了带的 ≥22% + 中心离带正中 ≤8% + 长边/短边 ≤1.35，
   四条同时成立 → 打回，回执把四个读数一起摆出来（`subject_boxes` 逐件盒 + `band`）。
3. **拼不出来就明说无图**：回执的 `reason` 是三态——`hit` 命中 / `no_match` 库内确无此
   类目（`note` 里带「这个源一共多少行」的证据）/ `source_not_available` 这条腿今天没通。
   后两态**都不是**「那你画一个吧」：改走四条画法通道（`rough`/`three`/`katex`/`vector`）
   把主体画出来，或如实降级——闸⑦ 的纯文字配额管占比，纯文字镜的 `subject` 照样要填
   （闸⑯）。
4. **不许自己写 `<path>` 拼一件「看起来像素材」的东西顶上**。这一条**今天没有闸在读**：
   闸㉕ 看的是形状（多大、多正、多居中），不是「这件是谁画的」。所以它靠你守——理由不是
   规矩，是效果：手画一件的真实物件画不出该有的细节，代价还比取件高；取不到件的那一镜，
   正确的动作是第 3 条里那两条。

拼出来的场景照样要填 `subject`（闸⑯ 每镜必填）。`subject_terms`（⑲-b 盲描述用的词族）
**今天 schema 里没有这一栏**，别写——同「主体具名」那一节。
<!-- /section -->

<!-- section: scale-safe -->
# 部件档的缩放实测（`scale_safe`）：不过线的件不得用作主体

`scale_safe` 是**量出来的**，不是谁填的：把这件素材缩到它在片子里的最小尺寸（部件档＝
画布短边 1080 的 0.18，即 194 px 那一档），量**最细笔画还剩几像素**，判据 ≥2 px
（片子可能被平台二次压缩，1 px 的线压一刀就成灰雾）。库里**没有任何一条写入路径接受人工
传值**——「我觉得这个够粗」不是一个输入。

三态，只有一态能用：

| `scale_safe` | 含义 | 用法 |
|----|------|------|
| `true` | 实测过、过线 | 可作主体（部件档最小的那一档也用得住） |
| `false` | 实测过、不过线 | **不得用作主体**。缩到 0.18 会糊成一道灰雾——`select_illustration` 对 `asset_role` 是 `icon`/`part` 的件**当场拒收**。换一件，或改当 `scene`/`bg` 用（那一档按整幅画面放，不走 0.18 的缩放） |
| `null` | **未判**：栅格件、纯填充路径量不出最细笔画 | 未判**不拦**，但回执 `scale_note` 会写明它没量过——别把「没量」当「合格」（D-未判不绿） |

⚠️ 那条拒收**只住在 `select_illustration` 这一个节点里**：不经它、直接把 `asset_id`
填进 `shots[].assets` 的话，今天没有任何一处会替你把这一栏看一眼。

**别把两件事混成一件**：`scale_safe` 今天量的只是「最细笔画 ≥2 px」；墨色与所在底色的
对比是**另一条**判据（`code_spec.contrast_ratio` / `CONTRAST_MIN`，在片子那侧按实际配色
算，不在这一栏里）。
<!-- /section -->

<!-- section: whiteboard-imagegen -->
# 出图腿的视觉规范（手绘纸底那一族）

**先看这一条**：手绘纸底这种观感，**用代码画（见「白板 / 简笔描线族」）比出图更好**——能动、
可溯源、零成本。这一节只管「供给优先级」里第 2/3 步那条**真出图**的路，什么时候走那两步
见上面那张顺序表（要写实/摄影/电影感，或者要一整幅有大量细节的手绘插画）。

走「一整幅手绘插画 + 按叙事顺序把它露出来」这条路时，规范写进 `imagegen` 的 `prompt`。

**四条硬规矩**：① 纸底 `#F5EBD7`，**禁纯白**（纯白底压一刀就不是纸了）；② 深灰线描
（`#3A3A3A` 一带），线条是主角；③ 点缀色**上限三色**（红/橙/蓝一档），颜色是注解；
④ **画面内零文字**——字母、数字、标签、刻度都不许有，文字全交给字幕层。
外加两禁：禁写实/摄影/三维，禁复杂纹理。

**三句话要一起写**：画面描述写「能被认出的东西」（闸⑯ 对 `subject` 用同一把尺）；
一张图 = **一整幕**，配 `staticFile` 引用 + 一份 `reveal` 申报（没有 `reveal`，它只是静图）；
`reveal.canvas` **必须等于这张图的真实像素尺寸**（闸㉘ 后半段拿库行 `width`/`height` 对账，
对不上直接退回；库行没尺寸记**未判**，不是通过）。

**这一格今天只有提醒**：「画面内零文字」没有判据。⑲-a 的 `text_share` **把母版字幕层
也算进去了**，分不清画面内文字与字幕层，拿它打回必然误伤——别拿它自证清白或认罪（Q119）。
<!-- /section -->

<!-- section: subject-naming -->
# 主体具名（闸⑯）：每一镜的那个「能被认出的东西」怎么写

- `subject` **每镜必填**，2~24 字，写**画面上那个东西**的名词短语（`活字盘`、`三条等高线`、
  `一张 1900 年的地图`），不要写主题词（`印刷术`）。`理念`/`趋势`/`文化` 这类抽象词在
  拒绝名单里，闸⑯ 见一个退一个——它们既画不出来也认不出来。
- `subject_channel` **有图形主体的镜必填**，五选一：`rough` / `three` / `katex` /
  `vector` / `image`。它必须是**这一镜代码里真用上的那条**（import 了还得真调用），
  闸⑯ 拿代码逐镜核你申报的那条；写着 `rough` 而代码里一个 `roughjs` 都没用，等于自报。
- **纯文字/纯色块镜不许填 `subject_channel`**（那一档由闸⑦ 的纯文字配额管）：这类镜
  一条画法通道都没有，填了就是无证据的申报，闸⑯ 两向都判。`subject` 照样要写——
  「「毕昇」两个字」也是个能被认出的主体。
- `subject_terms`（⑲-b 盲描述用的词族）**今天的 schema 里还没有这一栏**：合同里写的是
  它，实现只落了 `subject` / `subject_channel` 两格。**别写它**，写了会被 schema 退回来。
<!-- /section -->

<!-- section: motion-anchors -->
# 动效锚定（闸㉑）：每一条动效为**哪个数据**动

镜级 `motion` 是一张清单，每条 `{kind, anchors}`：

- `kind` 必填且非空（说不出是什么动效，就没法说它服务谁）。
- `anchors` **必填、不能空、最多 3 个**。空锚＝这条动效不为任何数据服务（历史上被驳回过的
  形态）；挂超过 3 个上限就退——判据是「**每一个**锚都得落在窗口内」，挂满了等于没锚。
- 只认三种写法（**白名单**，写错一个字符当场退）：`fields.<本镜声明过的字段名>`、
  `subtitle:<本镜第几个词>`、`beat:<第几个节拍>`。序号从 **1** 起，不写零、不写小数。
  锚到这一镜没声明过的字段，与没锚是同一件事。
- **窗口**：这条动效的触发帧要落在它锚的数据「首次可读」的 ±k 帧内，k = fps ÷ 3
  （30fps 就是 10 帧 ≈ 0.33 秒）——人对齐的精度是按时间走的，所以容差跟着帧率走。
- **今天真判得动的只有 `subtitle:` 那一档**：`fields.` 锚落「未判」（「这个字段什么时候
  在画面上变得可读」要人判，不是这一层能反推的），`beat:` 锚也落「未判」（这条链没做
  节拍检测，节拍表不存在）。**未判不是通过**——别把「挂到必然存在的字段上」当成过闸的
  写法；该申报就如实申报，⑲-b 那条盲测通道上线后这两档才会真有读数。
<!-- /section -->

<!-- section: text-only-quota -->
# 纯文字镜的配额（闸⑦ 第三档）：中间镜最多三分之一

- 判据只数**中间镜**：**首尾两镜不计入分子也不计入分母**。开场与收尾用纯文字/纯色块
  是允许的（那两镜本来就常常是标题与落款）。
- 中间镜里，纯文字/纯色块镜超过 **1/3** 就打回，回执点名是哪几镜——补画面主体，
  别把整片做成「卡片 + 文字」。
- 这条只在 **≥6 镜**时判（更短的片子不判通道配额）。
- 与本条同族的还有「画法通道种数」（≥6 镜要 3 种、≥10 镜要 4 种，见「通道表（闸⑦）」）。
- 「纯文字镜**不得连续 3 镜**」这句**今天没有判据**：连续与否没有任何闸在读，全靠这条
  配额。按配额写就够，别指望有人替你数字号。
<!-- /section -->

<!-- section: sound-design -->
# 声音：旁白与配乐

- **配乐只从曲库选**：`select_BGM` 挑一条填进 `render_motion_code` 的 `bgm`。曲库里没有
  合适的就**问用户**——绝不去网上下载（`fetch_media` / `web_search` / `fetch_url` 都不许
  用来找音乐）。
- **有旁白**（`narration=true`）：人声与逐词字幕由母版合成，**不要写每镜时长**（镜头长度
  归真实语音时长）。第几个词可用 `subtitle:<i>` 锚动效——那是今天唯一真判得动的一档锚。
- **没旁白**（`narration=false`）：没有语音时长可依，**每镜必须给 `min_duration_sec`**
  （5~15 秒，给观众读字幕的时间），全片总长就是各镜 `min_duration_sec` 之和。
- **声音设计的细节不在这份正文里**（响度、BGM 压低、停顿、语气）：要动它们就
  `load_skill(name="motion_explainer_skill", path="sound-design.md")` 取附件全文再动手。
<!-- /section -->
