---
name: motion_explainer_skill
display: 图文科普片（零素材出片）
description: 【WORKFLOW SKILL】没有视频素材、用户只给一个选题时的出片流程：自己查资料 → 写每镜文案 → 每镜选一张shot配方卡+填参数（不写渲染代码）→ 人声旁白配逐词字幕 → 一条成片。适用于科普、历史、概念解释类短片，或用户明确说「我没有素材，你自己找内容做一条」。A zero-material workflow: research, write per-shot copy, select shot recipe cards and fill params, narrate and render.
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
扫完就能选卡，不要逐张调「加载技能」读卡全文。

选卡流程：
1. 读 `shots/INDEX.md`（1 次调用）→ 按选题选 3-5 个类别
2. 从选中类别里挑 1-3 张卡（看能量搭配：开场高→中段混合→收尾中）
3. 只读选中卡的全文（每张卡 1 次调用，总共 ≤ 15 次）
4. 卡全文有：意图、动效核心、参数表（`| 参数 | 典型值 | 调节手感 |`）、已知坑、参考实现路径

**不要读超过 15 张卡。** 124 张卡不需要全看，INDEX.md 的一句话描述足够选卡。
怎么选、选多少由你根据选题自己决定。

## 适配 demo 代码

每张卡的「参考实现」指向 `demos/<category>/<card-name>/` 下的 TSX 文件。适配步骤：

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

## 3 档风格组合

`remotion/src/themes/palettes.json` 的 `preset` 段定义三档**固定组合**（色板 + 质感 + 字幕档 + 主体来源 + 素材源白名单）。
模型只选档，不自由搭配——这是"多风格"与"风格漂移"的分界。

| 档 | 色板 | texture | 字幕档 | 主体来源 | 适用选题 |
|----|------|---------|--------|----------|----------|
| `ink-archive` | `vintage-kraft` / `modern-light` | `film` 或 `bleach` | `torn_highlight` | 位图优先：老照片、纸纹、手绘 | 历史、器物、人物传记 |
| `tech-data` | `modern-light` / `color-play` | `none` | `full` | 代码优先：矢量图、公式、数据卡、图标 | 概念解释、数据对比、流程 |
| `night-concept` | `deep-ocean` / `midnight` / `obsidian-violet` | `glow` | `torn_highlight` | 3D/光优先：三维、光效、人物剪影 | 科学、宇宙、抽象概念 |

选档时把 `palette`/`texture`/`subtitle_mode` 写进 spec 顶层。档位→色板/质感/字幕/主体来源的对应关系是固定组合，见 `remotion/src/themes/palettes.json` 的 `preset` 段——模型只选档，不自由搭配。

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

# 一镜只有七栏

`shots[]` 每一项：**id**（卡的 `card` 标识，必填）、**code**（必填）、**text**（必填）、
**highlight**、**fields**、**min_duration_sec**、**texture**（省略=跟整片默认）、**source**。

另有 **card**（选的哪张 shot 卡，必填）、**derived_from**（适配自哪个 demo，必填）、
**assets**（本镜用到的资产列表，可空）。

- `text` 容量按画幅：竖屏与方屏 ≤ 60 字、横屏 ≤ 110 字。**只进母版字幕带，不进镜头画面**。
- `highlight` 里每个词**必须是该镜文案的子串**，每镜 ≤ 4 个。
- **有旁白时不要写每镜时长**。镜头长度归真实语音时长。
- **无旁白时必须给每镜 `min_duration_sec`**：画面要给观众时间读字幕+看画面，每镜 5~15 秒。
- 首尾各留一镜：开场与收尾的呼吸镜（`min_duration_sec` 给 1.5~2.5）。
- `source` 如实填。没查到的不要写上去。
- `card` 填卡名（如 `aurora-bloom-bg-flip`），`derived_from` 填 demo 的 relpath
  （如 `effects/aurora-bloom-bg-flip/AuroraBloomBgFlip.tsx`——**相对 `demos/` 的写法**，
  与 `demos/MANIFEST.json` 里的键一致；带 `demos/` 前缀的写法也收，会归一）。
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

3 档：`full`（全字幕）、`torn_highlight`（撕纸高亮，推荐）、`none`（无字幕）。
字幕带在母版 `<FilmLayout>` 里，镜头代码不用管字幕。

# 局部改

出片后要改几个字或几镜，**不要重跑整条流程**：

1. **改文案/高亮词** → patch_motion_code（只改指定镜的 text/highlight，不重渲结构）
2. **改一镜的画面** → 重新选卡/适配那一段代码 → patch_motion_code
3. **改整体风格** → 换主题色板 → patch_motion_code（改 palette 引用）

# 十一道闸

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

闸 ⑦⑨⑩⑮⑯ 在 `plan_motion` 时就**硬拦**（不烧像素），改十遍只花几秒。
闸 ① 在容器 lint（渲染前）。闸 ②③④⑤⑥⑧ 在渲染时判。闸 ⑪ 在出片后判。

**未判不是通过**：闸 ⑪ 返回 `undetermined`（探针采不到资产盒）时，不是"通过了"——
是这一镜没有可判的资产，闸 ⑪ 对它没有发言权。只有 `animated` 才是通过。
镜数不到 6 不判配额——这一档不判配额，模型才知道未判不是通过。

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

# 资产与图像通道

本链支持图像资产——用 `staticFile` 引本镜 `assets` 声明过的本地文件。

## 素材从哪来（供给优先级，写死——**不要问用户**）

一镜要有画面主体时，按这个顺序自动往下走，不要就「用库里的还是画一张」去问用户：

1. **先检索素材库**：`search_illustration` 打本镜的**具体名词**（主体名 + 场景，不是
   主题词）。命中就用 `select_illustration` 取那一件——它把字节落进本会话素材册并回带
   `assets` 三键，免费、不联网、可溯源。
2. **未命中再出图**：`imagegen` 按提示词出一张（要页面「设置 → 出图后端」配好 provider
   与密钥；`model`/`size` 留空＝用该后端默认值）。回执里的 `asset` 三键可直接贴进
   `shots[].assets`。
3. **没配后端**：`imagegen` 回 `ok:false` + `degraded:true`，人话里写明「图像通道降级为素材库检索，本片 N 镜无图」。**这时不许自己编一句「图中显示了…」**——那是把没有的东西写成有（静默出一支没有图的片子才是失败，降级本身不是）。要么把这一镜改用四条画法通道（rough/three/katex/vector）画出主体，要么它就撞闸⑦（通道种数、纯文字镜占比上限三分之一）；纯文字镜虽然允许占 ≤1/3，`subject` 那一栏照样要填（字也可以是主体，闸⑯）。

这条顺序是**合同冻结**的（§9-Q27），实现上落在 `imagegen` 的工具描述与上面这三步：
模型读得到、机器判得动，所以既不必问用户，也不会滑出第四条路。

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
