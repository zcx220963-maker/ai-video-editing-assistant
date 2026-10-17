---
name: motion_explainer_skill
display: 图文科普片（零素材出片）
description: 【WORKFLOW SKILL】没有视频素材、用户只给一个选题时的出片流程：自己查资料 → 写每镜文案 → 每镜选一张shot配方卡+填参数（不写渲染代码）→ 人声旁白配逐词字幕 → 一条成片。适用于科普、历史、概念解释类短片，或用户明确说「我没有素材，你自己找内容做一条」。A zero-material workflow: research, write per-shot copy, select shot recipe cards and fill params, narrate and render.
sections: film-knobs, patch-motion, sound-design, subject-naming, motion-anchors, draw-floor
---

# 何时用这条通道（与常规剪辑的分界）

只有一条判据：**这条片子的画面是不是代码画得出来的（不需要现成影像）。**

- 用户随消息给了素材（要剪的影像/音频） → 走 default_editing_workflow_skill
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

**被打回时怎么办（这条通道不会被熔断，别换通道）**：
- plan_motion / render_motion_code 的打回都是**带清单的改法**，按清单改了再交就是正路——
  这两个工具的校验打回**不算失败、不会被熔断**。**不要**因此改走 `render_web` 或别的出片通道：
  那条路出的是 HTML 幻灯片，丢了这条链的全部画面能力。
- **只改几镜就用 `shots_patch`**：只交那几镜（按 id 替换），不必把几十镜代码整份重交——
  整份重交一次就是两百多 KB，慢且容易在改别处时带进新错。
- **字色读不出（闸⑩）系统会自动修**：浅底白字、深底深字会被就地换成白或近黑，回执的
  `autofix_notes` 里写着改了哪镜。所以浅色板照样可以用，不必为了躲闸把配色收窄成几套深色板。
- 取 demo 全文：`load_skill(name, path=…)` 的 path 带不带 `demos/` 前缀都认，照抄取材回执里的
  `derived_from` 即可。

# 镜头代码合同：选卡 + 适配（不从零写代码）

## 核心原则

**你不写渲染代码，你选 shot 配方卡 + 适配参数。**

项目内置 **133 张 shot 配方卡**（`shots/` 目录）+ **161 个 demo 实现**（技能目录 `demos/`）+
**8 套主题色板**（`remotion/src/themes/palettes.json`）。每张卡有调校过的参数、缓动、时间轴，
demo 实现是参数的真相——你的工作是**选卡 + 把 demo 代码适配到用户的文案和主题**，不是从零写。

## 133 张 shot 卡分 11 类

| 类别 | 卡数 | 用途 | 示例卡 |
|------|------|------|--------|
| **opening** | 11 | 开场镜/标题入场 | spotlight-hero-card, spotlight-title-variant, brand-ink-open, fracture |
| **camera** | 10 | 3D空间运镜/2.5D相机 | basic-3d-scene, crash-zoom-punch, depth-layer-moves, space-camera-moves |
| **effects** | 15 | 视觉特效/光影/转场重音 | aurora-bloom-bg-flip, spotlight-sweep-moves, riso-print-hits, light-play-moves |
| **typography** | 22 | 文字动效/逐词入场 | flying-words, blur-slide, typewriter-moves, gradient-word-sweep, lead-word-zoom-assemble |
| **data** | 5 | 数据可视化动效 | chart-live-moves, before-after-slider-scrub, gauge-readout-moves, timeline-travel |
| **transition** | 15 | 镜间转场 | card-flip-reveal, circle-match-iris, gradient-transition, page-turn-transitions |
| **rhythm** | 8 | 节奏卡点/变速 | beat-cut-moves, speed-ramp-freeze, quad-split-parallel-scenes, sakuga-timing-shift |
| **ui-entrance** | 21 | UI元素入场/卡片堆叠 | deck-deal-flyin, card-stack, carousel-3d, wall-reveal-moves, row-embed |
| **interaction** | 11 | 交互模拟/光标/协作 | collab-cursor-moves, theme-switch-moves, glass-pill-dictation-typing |
| **outro** | 6 | 收尾/落款 | logo-shrink-wordmark-lockup, grain-dissolve, neon-triple-marquee |
| **science** | 9 | 科普图示：剖面/地图/流程/曲线/比喻 | cross-section-reveal, map-terrain-build, process-diagram-flow, curve-function-trace, metaphor-bridge |

## 选卡方法

**先读 `shots/INDEX.md`（一次加载技能调用）**——它列了全部 133 张卡的卡名、能量、一句话描述，
以及每张卡 demo 的**文件名**（`derived_from` 就照抄那一栏），扫完就能选卡，
不要逐张调「加载技能」读卡全文。

选卡流程：
1. 读 `shots/INDEX.md`（1 次调用）→ 按选题选 3-5 个类别
2. 从选中类别里挑 1-3 张卡（看能量搭配：开场高→中段混合→收尾中）
3. 只读选中卡的全文（每张卡 1 次调用，总共 ≤ 30 次）
4. 卡全文有：意图、动效核心、参数表（`| 参数 | 典型值 | 调节手感 |`）、已知坑、参考实现路径

**不要读超过 30 张卡。** 133 张卡不需要全看，INDEX.md 的一句话描述足够选卡。
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
6. **顶层是自包含的 `<AbsoluteFill>`**：底色、边距、字幕位都由这一镜自己定。
   母版 `<FilmLayout>` 已下线——**不要再 import 它、也不要指望有个壳替你排字**。
   `@dg/film` 现在是零件包：字幕三件（`CaptionText` / `CaptionLayer` / `useCaptionTiming`）
   与字段标注 `F`。全片一致靠 `@dg/tokens` 的 `TOKENS`（底色/墨色/强调色）与卡库配方，
   不靠一个壳（见下面「字自己排」一节）。
7. **把 demo 的设计坐标换算到整帧**（这一步漏了就出白片）。demo 是在
   **1920×1080 的设计稿**上写的，坐标是那个坐标系里的绝对值；你画的是**整帧**
   （竖屏 1080×1920、横屏 1920×1080、方屏 1080×1080，从 `useVideoConfig()` 拿）。
   直接把 demo 的 `x/y/width/height` 原样搬进来，等于按 1920 的尺画 1080 的纸：
   右边和下边跑出画面。换算就两行，放在组件顶上，demo 里的坐标一律乘它：

   ```tsx
   const { width, height } = useVideoConfig();
   const K = width / 1920;                        // 设计稿 → 整帧的等比系数
   const top = (height - 1080 * K) / 2;           // 设计稿在整帧里垂直居中
   ```

   宽度贴合（`K = width / 1920`）会把竖屏的内容压到只占画面约三成——画面显得小、
   而且闸⑭ 数的是整帧的平均差，同样的动效会被上下空边稀释约 3 倍。把 `K` 改成
   `Math.max(width / 1920, height / 1080)`（覆盖式）确实能让 ⑭ 过：实测同一镜从
   「只数得出入场那 1 段」变成「数得出 2 段」——**但那是拿裁掉内容换的**：竖屏下
   `K` 变成 1.41，1920 宽的设计稿只留得下中间 34%，卡库那几份左右排开的 demo 一裁就把
   标题和右列的数值裁到画面外（`Recipe` 少了首字母、`uuid/enum` 整列没了）。
   闸⑧ 会把这种裁字记下来，但它**只记账不拦截**，所以没人会替你拦住。
   竖屏要满幅观感，正解是**按整帧重排**（把 demo 里左右排开的两块改成上下排、
   字号按 `width` 重算），或选一份本来就纵向铺得开、动效幅度大的 demo。

   **闸④ 的填充度这条线，宽度贴合的 16:9 画布是天生迈不过去的**：它的读数是
   `min(内容外接盒宽 ÷ 画宽, 外接盒高 ÷ 画高)`，取三帧里最满那一帧，低于
   `SPAN_MIN = 0.45` 且最低那帧的像素占比低于 2% 就打回。宽度贴合下画布高只有
   `1080 × (1080/1920) = 608`，`608 ÷ 1920 = 0.317`——**demo 把画布画得再满，
   这一项也封顶在 0.317**。所以竖屏的构图不能只有那块画布：要让它落在**一块铺满画面的
   底板**上（卡片、色块、上下两段留白都算），底板的高宽比才是被量的那个数：
   底板取画面宽 94%、画面高 80%，这一项就按几何是 `0.80`（受短边比约束前）而不是 0.317。
   注意量的是**画面里所有内容合起来**的外接盒，因此把两样东西分别摆到画面的上下两端
   也能把这一项顶上去（实测有一镜靠压在画面底的那道公式顶到 0.59）——这只说明判据读
   的是外接盒，不代表那样摆的构图就好。

   **用了 `DesignStage` 的 demo（卡库里 31 份）要额外处理**。夹具件
   `demos/_fixtures/Motion.tsx:61` 的算法是 `scale = useVideoConfig().width / w`，
   外面还套一层 `<AbsoluteFill>`。折进一镜时这两层会**各放大一次**：它自己按
   「整帧宽 ÷ `w`」缩一次，外面那块 1920×1080 的画布再缩一次。按代码算出来的形状：
   内容缩在画布左上角一块、其余面积成它自带的底色。**传大 `w` 救不了它**：它的缩放因子
   就是「整帧宽 ÷ `w`」，所以放大之后的实际宽度恒等于整帧宽，`w` 只改设计稿坐标系的疏密。
   修法只有两条：**要么**把 `<DesignStage>` 换成只留底色的 `<div>`（缩放交给外层那一次），
   **要么**让外层画布的宽度本来就等于整帧宽（包装层按整帧宽铺，缩放系数跟着换成
   `width / 1080`）。这一段是按代码读出来的，不是实测读数；改完必须自己抽一帧看。
8. **three 镜的单位纪律**。`@react-three/fiber` 的默认相机在 `z=5`、`far=1000`，而 demo
   里的球/盒半径常写成几百**世界单位**。照搬过来＝相机在几何体**内部**，而球只画外表面
   ⇒ 整帧一个像素都不变，闸⑭ 数出 0 段、闸④ 判空。规则：几何体取 **0.2~1.0 这一档**
   单位，并在 `<ThreeCanvas>` 上**显式给相机**（`camera={{ position: [0, 0, 3], fov: 45 }}`），
   让「物体大小」和「相机距离」是一起算过的，不是各自抄来的；灯光强度同理，
   标准材质在物理光照下要点光 `intensity` 到几十才看得见。
8. **末行必须默认导出**：每一镜的文件要有一个默认导出（`export default S07;`，组件名随你），
   否则闸① 判 `dg/needs-shot-export`。**demo 是片段，171 份里只有 5 份自带这一行**——照抄完
   自己补上，别等逐镜返工（2026-10-16 真机 run 9272c7f59501，20 镜那次：这一条单独烧掉了
   20 轮 `edit_file`，每轮只加一行）。

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

// 简笔画零件库：一件＝一个能被认出的东西的轮廓（人、机器、厂房、齿轮、图表…）。
// 见「每一镜都要画出文案点到的那件东西」一节——这是「画得像」的正路
import { Figure } from "@dg/lib/draw/Figure";
import { Machine } from "@dg/lib/draw/Machine";
import { Building } from "@dg/lib/draw/Building";
import { Chart } from "@dg/lib/draw/Chart";
import { Gear } from "@dg/lib/draw/Gear";
// 另有 Face / Vehicle / Arrow / Coin / Book / Scale / Clock / Crown

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

// 零件包（字幕三件 + 字段标注 + 整片那一档的读取）。母版已下线：没有必须套的壳，
// `resolveFilmStyle` 拿的就是「用户为这条片子挑的底色与强调色」，起手模板用的就是它
import { CaptionText, CaptionLayer, useCaptionTiming, resolveFilmStyle, F } from "@dg/film";
import { TOKENS } from "@dg/tokens";

// Remotion 核心
import { interpolate, useCurrentFrame, useVideoConfig, random, AbsoluteFill, staticFile } from "remotion";

// 画法通道（在白名单里，按需引入）
import { fitText as remFitText } from "@remotion/layout-utils";
```

## 8 套主题色板（整片挑一档，个别镜可覆盖）

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

**整片的底色与字幕配色由计划卡上一个字段定**：`palette`（八套之一；写 `none` = 不指定，
落回 `TOKENS` 的米白）。用户挑了哪一套，那一套就是**这条片子二十镜的默认底色**——
引擎把它换算成 `bg`/`paper`/`ink`/`accent` 四色注入每一镜，代码里用
`resolveFilmStyle(getInputProps())` 取（起手模板就是这么写的）：
`const film = resolveFilmStyle(getInputProps()); const bg = film.bg || TOKENS.bg;`。
**不要写死颜色**——写死了用户在卡上换色板，你这一镜不动，整片就它一个色。

**个别镜要另调就在那一镜覆盖**。三档优先级，从高到低：

1. 这一镜的代码里绑了 `const palette = palettes["…"];`（`@dg/themes/palettes.json`）；
2. 这一镜自己申报的 `bg`；
3. 整片那一档 `palette`。

所以「整片一套底色，某几镜另有理由（比如闪回/数据页）才换」是可以的、也是被鼓励的形状；
**逐镜八套轮着配不是**——2026-10-17 起闸⑲ **不再**数色板种数，那一条是反方向的。

## 3 档风格组合（整片默认，不是逐镜配额）

`remotion/src/themes/palettes.json` 的 `preset` 段定义三档**推荐组合**（色板 + 质感 + 字幕两维 + 主体来源）。
这三档是**整片默认这一份怎么填**的参照：挑定一档，把它的 `palette` / `texture` /
`caption_style` / `highlight_style` 一并写进 spec 顶层——**整片就统一到那一套**，
这是用户一眼看得出来的那种一致（底色与字幕）。

| 档 | 色板 | texture | 字幕（样式 + 高亮） | 主体来源 | 适用选题 |
|----|------|---------|--------|----------|----------|
| `ink-archive` | `vintage-kraft` / `modern-light` | `film` 或 `bleach` | `paper` + `ink` | 手绘纸感优先：纸纹、线条插画、档案图纸 | 历史、器物、人物传记 |
| `tech-data` | `modern-light` / `color-play` | `none` | `on_picture` + `color` | 代码优先：矢量图、公式、数据卡、图标 | 概念解释、数据对比、流程 |
| `night-concept` | `deep-ocean` / `midnight` / `obsidian-violet` | `glow` | `film_line` + `color` | 3D/光优先：三维、光效、人物剪影 | 科学、宇宙、抽象概念 |

**逐镜覆盖**写在那一镜上：底色改那一镜代码里的 `const palette = palettes["…"];` 或它申报的
`bg`；质感与字幕两维在这一镜单独申报（只有那一镜确有理由才换）。跨镜一致性的账由 ⑩（字色对
底色的对比度）与 ⑨（跨镜同构）来记；闸⑲ 管的是**构图**那一侧——满幅镜数、质感种数、
`bg` 申报率，**不管色板种数**：「二十镜共用一个调子」不再是错，那正是用户要的东西。

## 代码模板

`remotion/src/user-shots/shot.tsx` 就是这份形状的活样例（每次渲染前被覆写，改它就是改起手模板）。

```tsx
import React from "react";
import { AbsoluteFill, getInputProps, interpolate, random, useCurrentFrame, useVideoConfig } from "remotion";
import { CaptionLayer, resolveFilmStyle } from "@dg/film";
import { TOKENS } from "@dg/tokens";
import { E } from "@dg/motion";

const Shot = () => {
  const frame = useCurrentFrame();
  const { width, height, durationInFrames } = useVideoConfig();   // 整帧；坐标就是设备像素
  const t = frame / Math.max(1, durationInFrames - 1);
  // 整片那一档：底色 + 强调色。缺了就落 TOKENS 的米白
  const film = resolveFilmStyle(getInputProps());
  const bg = film.bg || TOKENS.bg;
  const accent = film.accent || TOKENS.accent;

  return (
    <AbsoluteFill style={{ backgroundColor: bg, fontFamily: TOKENS.fontStack }}>
      {/* === 从 shot 卡 demo 适配的动效代码（这一层自己铺满整帧）=== */}
      {/* === 只画手边没有的：整片底色别写死，强调色别写死 === */}
      <CaptionLayer />
    </AbsoluteFill>
  );
};
export default Shot;
```

**这一镜要另配色板**（个别镜覆盖）就在这一镜里绑一套，别改整片那一档：

```tsx
import palettes from "@dg/themes/palettes.json";
const palette = palettes["deep-ocean"];        // 只对**这一镜**生效
// 此后底色用 palette.page、强调色用 palette.accent
```

## 禁止事项

- **禁止从零写渲染代码**：必须从 shot 卡 demo 适配。先读卡 → 读 demo → 适配。
- **禁止 Math.random / Date.now / new Date**：用 `rand(seed)` 或 remotion 的 `random(seed)`。seed 必须给固定值。
- **禁止裸写颜色字面量**：底色、字色、描边一律从 `palettes.json` 取（`const palette =
  palettes["…"];`，键 `page`/`surface`/`field`/`text`/`muted`/`accent`/`border`）。
  闸⑩ 逐镜量字色对底色的对比度，写死的色值没有色板可对账。
  （`DesignStage` 是 demo 夹具件，按**整帧宽度**缩放——折进一镜时别让它与你的画布各放大一次，见适配步骤 7。）
- **禁止降档命门参数**：shot 卡「已知坑」里标注的参数不得低于典型值。
- **禁止外链**：镜头代码里不许出现 http/https 地址取图、取字体、取脚本——渲染容器不联网。
  `staticFile` 只用来取**字体**（`@remotion/fonts` 的 `loadFont` 要 `staticFile('fonts/…')`）。
- **字体必须用白名单字族**：`Noto Sans CJK SC`、`Noto Serif CJK SC`、`Noto Sans Mono CJK SC`、
  `DejaVu Sans`、`DejaVu Sans Mono`、`DejaVu Serif` 等 18 个镜像内已装字族。
  lint 规则 `dg/font-family-whitelist` 逐属性检查，非白名单字族逐镜打回。
  用 `fitText` 时默认走 `TOKENS.fontStack`，不用手写字族名。

## 写法纪律（lint 规则在渲染前判）

- **字段标注**：用 `<F name="字段名" value={fields.字段名}>` 标注可调旋钮的位置，
  `F` 从 `@dg/film` 导入。旋钮先在 `fields` 那一栏声明，每项 `{name, type, value, label?}`：
  `type` 只能取 `number`/`color`/`text`/`bool`/`select` **五种**，声明的值落 `fields.<name>`
  （代码里就认这一个名字）。lint 规则 `dg/field-drift` 检查：
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

另有 **card**（选的哪张 shot 卡，必填）、**derived_from**（适配自哪个 demo，必填），
以及两个**版式档** **bleed** 与 **bg**（满幅与底色，省略=常规版式，见下面「版式档」那一节）。

- `text` 容量按画幅：竖屏与方屏 ≤ 60 字、横屏 ≤ 110 字。这句话由**这一镜自己排成一条字**
  （见「字自己排」那一节；排法自选，但整句只许画一处）。
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

# 时长与镜数

- 「X 分钟以内」是**上限**不是**目标**——90 秒也是「4 分钟以内」；「X 分钟左右」才是目标。
- **`target_duration_sec` 每一次调 plan_motion 都要带**（取材轮与写码轮都带），也可写在 spec 顶层。
  时长在 plan_motion **入库前**就对齐与把关，过了这一关，出片时不会再因时长被拦。
- **没旁白**：每镜时长＝`max(min_duration_sec, 2.6 + 0.16×字数)`，是确定值不是估算。总长对不上目标时
  **系统会按比例自动调各镜驻留**（每镜最多 20 秒），你不用为几秒的差去重写分镜。
  一镜驻留超过 15 秒会显得拖：长片优先**加镜**，经验值是 4 分钟 ≈ 20~30 镜。
- **有旁白**：时长归真实语音，估算是「每字 0.2~0.3 秒 + 每镜 0.6~1.2 秒」；偏离太多会在 plan_motion 打回，
  按回执里的镜数建议加镜或加长文案。
- **镜多时用 `code_from` 复用版式**：一镜写 `"code_from": "s03"`、不写 `code`，就复用 s03 的代码，只换
  `text` / `fields` / 色板。十几种版式撑几十镜，一次调用装得下；别为凑镜数把同一段代码抄几十遍。
- 用户给的时长/画幅/帧率是约束条件，约束之间有冲突时你自己判断怎么处理，在答复里说明。

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

# 字自己排

**字归镜头代码排**——母版下线之后就这一条：`text`（这一镜整句）、`highlight`（要强调的词）、
`words`（TTS 逐词帧）是注入进来的。**缺省让 `<CaptionLayer/>` 排**（不带 props，见下）；
只有这一镜的构图要求把字与图形编在一起时，才自己摆 `<CaptionText/>`。自己摆的话，两种观感
覆盖绝大多数段落，都有卡和 demo 可抄（`typography/keyword-color-caption`、
`typography/outline-caption`）：

| 观感 | 什么时候用 | 怎么做 |
|------|-----------|--------|
| **关键词换色** | 底干净、能腾出一块平的地方；浅底图文镜、概念讲述、数据叙述 | 字压在画面上、**不留纸条**；高亮词换这一镜的强调色（`<CaptionText emphasis="color" accent={…} />`），画面里对应的图形用**同一个 accent** 跟着念到的词换色 |
| **白字黑边** | 画面本身就是主角、明暗乱跳、没有干净区域（实录/插画/照片满屏、口播密集） | 白字 + 一圈黑描边（`outline="#000000"`，元件自己按字号画八向 `textShadow`），底部压一条**渐隐**（不是纸条），强调词换暖黄 |

两者的分工只有一句话：**底干净就换色，底乱就描边**。同片别混着用——一镜换色一镜描边，
观众读到的不是两种风格，是两种字幕。

## 零件：三件字幕元件

`@dg/film` 导出三件，与母版当年画的那条字幕**是同一份实现**（同样逐词点亮、同样按可用宽度
求字号、同样跟着色板配色）。所以「字自己排」不等于「自己重写一套字幕」：

| 元件 | 是什么 | 什么时候用 |
|------|--------|-----------|
| `<CaptionLayer />` | **整条**：底 + 字 + 入场动画，位置/字号/安全区**全按「字幕样式」那一档排** | **默认就用它**（起手模板摆的就是它）：不带 props 即取注入的那一份，整片二十镜因此同一处字号与位置 |
| `<CaptionText />` | **只有字**：逐词点亮 / 关键词换色 / 底色块 / 撕纸笔触 / 黑描边 / 自动字号 | 自己配底（画面、柔光、渐隐、描边），或让字与图形一起排——只在确有构图理由时才放弃 `<CaptionLayer/>` |
| `useCaptionTiming()` | 这一镜的**词时间轴**：`{ timed, reached(i), charFrames, pieces }` | 让画面里别的东西跟着念到的词动 |

```tsx
import { CaptionText, CaptionLayer, useCaptionTiming } from '@dg/film';

// ① 默认：整条字幕交给它（不带 props）。位置/字号/底/入场全按注入的
//    「字幕样式」那一档排——它认整帧（自包含镜没有画面带，没量到带就按整帧算）。
<CaptionLayer />

// ② 要按自己的构图摆：套一个定位盒子，`bottom` 量的是**这个盒子**的下缘
//    （绝对定位认最近的定位祖先）。字号与形态仍归那一档。
<div style={{ position: 'relative', width: w, height: h }}>
  <CaptionLayer bottom={Math.round(h * 0.08)} />
</div>

// ③ 只在确有构图理由时（字要与图形一起排）才自己摆：这时用 `<CaptionText/>`
//    关键词换色，高亮词换成强调色
<div style={{ position: 'absolute', left: x, top: y, width: textW,
              fontWeight: 700, lineHeight: 1.32, color: TOKENS.ink }}>
  <CaptionText text={text} highlight={highlight} words={words}
               emphasis="color" accent={palette.accent} fitWidth={textW} />
</div>
//    `emphasis` 省掉就跟着整片那一档的高亮样式走

// ④ 让图形跟着念到的词动——不必自己再实现一遍逐字时间
const { reached, charFrames } = useCaptionTiming();
<div style={{ transform: `translateY(${reached(0) ? 0 : 40}px)` }}>…</div>
```

三个元件的 props **全部可省**：省了就取注入的那一份（这一镜的整句、高亮词、词时间轴、
色板），所以 `<CaptionText/>` 就是「这一镜的字，摆在这儿」。`fitWidth` 是给它求字号的可用
宽度（设备像素）；要与画面里别的元素精确对齐就改用 `fontSize` 直接给——**描边与下划线的
宽度要按像素算，别用 `em`**（字号是元件求的，`em` 会在外层先解析成外层那个字号）。

四条纪律：
- **整句只画一处**：`text` 是注入的，画面里另画一份就是同一句话两处（重影）。
- **别手搓第二份字幕**：自己 `splitByHighlights` + 自己乘字号，两次求出来的字号、两处断行
  迟早对不上，而这类不一致只在成片上看得见（静帧预览看不出来）。要摆字就用那两个元件。
- **二十镜同一处字号与位置**：整片的一致性就落在「都摆 `<CaptionLayer/>`、都不给自己写
  `top`/`fontSize`」这一条上——各镜各写一个 `top: height * 0.68`、`height * 0.30`，
  用户一眼就看得出这不是一条片子。要挪就挪**那一档**（用户在计划卡上挑的字幕样式），
  不是挪一镜。
- **申报 `caption_style: "none"`（不上字幕）的镜别再摆这一件**：它自守不画，闸㉜ 会把那种
  自相矛盾的代码打回。母版已下线，今天没有「同屏两条」这回事——画面上有没有字幕，
  只看你摆没摆这一件。

## 字幕两维：字幕样式 × 高亮样式（整片默认 + 逐镜覆盖）

分镜表里与字幕有关的是**两栏**，它们是**正交**的两件事：

| 栏 | 取值 | 说的是 |
|----|------|--------|
| `caption_style` | `on_picture` 贴画面无底（缺省）/ `outlined` 白字黑边 / `paper` 纸条底 / `film_line` 整句电影感 / `big_center` 大字压屏 / `top_banner` 顶部横幅 / `none` 不上字幕 | 这行字**长什么样、站在哪**（底、位置、字号、入场都在这一档里） |
| `highlight_style` | `color` 关键词换色（缺省）/ `karaoke` 逐词点亮 / `block` 底色块 / `ink` 撕纸高亮 / `plain` 不强化 | 被点名的词**怎么被点出来** |

两维相乘＝35 种组合都说得出口：要「关键词换色但不要纸条」就写
`caption_style: "on_picture"` + `highlight_style: "color"`。**别把它们想成一排档**——没有
哪一档同时锁死这两件事，所以也没有「选了这个就必须接受那个」的时候。

这两维说的是「**怎么**点出来」，还有一个整片级的 `highlight_color`（`#rrggbb`，空＝跟这一镜
的色板）说的是「**用哪个色**」——它由用户在「文字库」钉，钉住时**盖过高亮词那一色**，连你在
`accent` 那一栏写的那一色也盖过。所以你照常写 `palette.accent` 即可，不必去读它、也不必为它
改构图：那一栏写的是「没有用户偏好时该长什么样」。

整片那一档写在 spec 顶层当默认（用户在「文字库」里挑的就是它），某一镜要换就写在**那一镜里**
（逐镜那一份赢）。两栏是**注入进来的值**：`captionStyle` / `highlightStyle` 就在 props 里，
接不接是你的决定：

- **自排一条字**（上面那两式）时多半**不接**：位置就是你那个盒子的位置，入场就是你自己的
  `interpolate`；但高亮那一维仍建议交给元件——`<CaptionText/>` 不传 `emphasis` 就跟着注入走。
- **整条要用**就摆 `<CaptionLayer/>`，位置/底/入场按那一档照排——**这是缺省写法**，
  不带 props 时取的就是注入的那一份，所以整片二十镜同一处字号与位置。
- 自排的字若要**精确对齐画面里别的元素**，用 `fontSize` 直接给字号；`fitWidth` 给的是
  「按两行求」的可用宽度。

字号由元件按「最多两行」自动排（夹在几何表字号的 0.62~1.25 倍之间，再乘这一档自己的系数），
不会再出现长句缩成蚊子字、短句撑破纸条。**逐词点亮**要有词时间轴才动得起来，没旁白时它
退化成整句常亮——要做卡拉 OK 就同时给 `words`；没旁白就别假装逐词。

`none` 那一档是「这一镜不上字幕」：`<CaptionLayer/>` 在这一档自守不画（摆着也不会出字）。
要摆的是**画面里的
字**（`<CaptionText/>`）就不必改成 `none`——闸㉜ 只管整条那件。

## 字不许压在图上

**判据**：一块字被**画出来的图形**盖住**这块字自己的 30% 以上** ⇒ 这一镜被打回重看（闸⑧ 的
第四类命中 `overlap_subject`）。

「画出来的图形」＝ SVG 图元（`path` / `rect` / `circle` / `ellipse` / `polygon` / `polyline` /
`line`）、`<canvas>`、`<img>`、`<video>`。**涂了底色的 `<div>` 不算**（那是卡片自身，不算主体，
口径同 ⑲-a）。两条剔除，不剔除就会满屏误报：

- **铺满整帧的图形件不算主体，算底色**（占画面带 **≥85%** 的按底色放过）。你每一镜都铺着一层
  底纹 `<rect>`，字当然压在底色上——不放过它，**每一条字幕**都会报。
- **与这块字有祖孙关系的图形件跳过**（字自己的底板就是这一种）。

**怎么排才不报**：

1. **给字留一块空地**。主体（人/物/图）画在一侧，字幕带在另一侧——这是每个镜都该先想的一步，
   不是排完再挪。
2. **要用衬底就用 `div`，别用 SVG 图形件**。SVG `<rect>` 在账上算「画出来的图形」：它压在
   你的字上（或你的字压在它上）就会报，哪怕它本来只是块底纹。做纸条/底板一律写成
   `div` + `background: linear-gradient(...)`（lint 也要求纯色底板写成渐变）。
3. **要压在图上当标注**，就把这块字的盒子**缩到图的一角**——被盖住的面积占比自然就下去了。
   判据判的是「这块字还读不读得出来」，不是「字和图形有没有碰过」。

读数在回执与预览静帧那一栏都能看到（`layout_warnings`；`layout_unscanned` 单独一栏，
「没扫」与「扫了且干净」是两件事）。预览静帧带上读数意味着**还没烧整镜就能看见这条**——
那是最便宜的改法。

# 版式档：满幅 bleed 与底色 bg

这两栏**每镜可选**，它们申报的是**构图**（画面铺多大、底色是什么），不是滤镜——
`texture` 那一档改的才是画面上的颗粒与暗角。母版下线后**没有「常规版式」这个缺省**：
省略＝没有额外要求，构图怎么排是你的事，这两栏是**你做完了报给闸听的**。

| 栏 | 取值 | 省略时 |
|----|------|--------|
| `bleed` | `true`（布尔，**不是字符串**）| 你自己定（闸⑲ 也就不把这一镜算作满幅） |
| `bg` | `#rgb` / `#rrggbb` | 闸④ 判空的基准色退到 `TOKENS.bg`（回读也拿不到时） |

**这两栏是申报，实现归你**：`bleed: true` 的实际含义是「**这一镜的画面铺满整帧**」，
而铺满靠你在代码里铺
（`<AbsoluteFill>` 顶到底、不留边距）。**申报与代码必须对得上**：闸⑲ 按这一栏数满幅镜、
回执也按它报，申报了却没铺满就是「账上写满幅、成片上看到留边」。
`bg` 同理，是这一镜的**底色宣言**：闸④ 判空拿它当基准色（母版回读拿不到时退到它，
再退到 `TOKENS.bg`），所以代码里的底色要与它一致——不一致时判空比的是另一个颜色，
`空壳` 与 `小气` 两条的读数都会歪。

**`bleed` 是纪律，不是「想写才写」的可选项**——闸⑲ 会数它，两条下限：

- **用深色板的镜必须写**：`midnight` / `deep-ocean` / `obsidian-violet` 这三套的 `page` 都是
  深底，配一个四周留边的居中构图就是「页边一圈奶油、中间一块深色」。这几镜写
  `bleed: true` 并同时给 `bg`（用该镜色板的 `page` 值）。
- **整片每 5 镜至少 1 镜满幅**：满幅镜数 ≥ `ceil(镜数/5)`（20 镜 → 4 镜、10 镜 → 2 镜、
  6 镜 → 2 镜）。画**整屏插画、满版描线、大场景、背景要铺满**的镜头一定写它；
  常规图文镜可以留省略，但上面这两条下限照样要满足。

两条要注意的：
- **满幅镜的字幕会贴到画面底部**（你自己摆的那条字也一样）：构图主体别压在最底下那一带
  （竖屏约底部 120px、横屏约 72px）——你摆字的地方就是观众视线最后的落点。
- **`bleed` 写成 `"true"` / `1` 一律不生效**（判的是严格的布尔 `true`），而且校验期就会
  被打回——别用字符串。

**`bg` 只收十六进制**：闸④ 拿它当「这一镜画了东西没有」的基准色，
写 `rgb()`/命名色/渐变会被打回（收了等于让闸④ 在这一镜上闭嘴）。深底镜给了 `bg`，
`texture` 那档就**别再加 `film`**（`film` 自带暗角与颗粒，压深底会糊成一片）。

**最有用的两个组合**：
- `bleed: true` ＝**整屏插画 / 白板板书**，文字与图一起构图（字用 `<CaptionText/>` 摆，
  别手搓——见「字自己排」那一节）。
- `bg` + 一点留边的居中构图 ＝ 在一支浅底片子里给某几镜**换底色**（夜里的一镜、切场景的一镜）。

```tsx
// 满幅镜：整帧都是你的画布，坐标从 0 到整帧尺寸
const { width, height } = useVideoConfig();   // 竖屏 1080×1920、横屏 1920×1080、方屏 1080×1080
```

# 转场与进出场（转场可逐接缝，进出场整片一档）

这两件**默认都在计划卡上勾**（整片的观感决定，让二十份模型现写的代码各自插一小段动画，
等于交给二十次掷骰子）。但**只有转场是「一行都不用写」**——它落在合成成片那一步，
逐镜切片一个字节都不动；**进出场落在每一镜的画面里**，所以它只能是「注入给你的两个数、
你自己画」（见本节末段）。逐镜覆盖那一档也只属于转场：覆盖写在分镜里、不进镜头代码，
掷骰子的问题不存在。

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
**那些注入项都不用你转交**：`text` / `highlight` / `words` 与各档档位都由渲染侧
**注入成这一镜的 props**——自包含的镜直接从自己的入参里取，不用声明、不用透传。

**代价差在哪**（这条决定你什么时候可以随便改）：**换转场不重烧任何一镜**——
转场落在合成成片那一步，逐镜的切片一个字节都不动，改十次也只多跑十次拼接；
**进出场则要进缓存键**，因为它改的是每一镜切片的像素（`entranceFrames` 那一对数注入给
镜头代码），改一次就是整片重烧。
所以「先定进出场、再调转场」比反过来便宜得多。

**进出场也是「申报，实现归你」**：整片那一档只是被算成两个数——`entranceFrames` /
`exitFrames`（上限 15 帧或这一镜帧数的 10%，取小）——注入进这一镜的 props，
**没有人替你画**。要淡入就用 `interpolate(frame, [0, entranceFrames], …)` 自己乘进整体
`opacity`；不画就是硬切。镜头级那两栏在自包含的镜上**没有**任何自动效果。

<!-- section: patch-motion -->

# 局部改

出片后要改几个字或几镜，**不要重跑整条流程**：

1. **改文案/高亮词** → patch_motion_code（只改指定镜的 text/highlight，不重渲结构）
2. **改一镜的画面** → 重新选卡/适配那一段代码 → patch_motion_code
3. **改整体风格** → 换主题色板 → patch_motion_code（改 palette 引用）
4. **改某几镜的构图/字幕/接缝** → patch_motion_code 的 `shot_sets`（整镜改写，一条
   `{shot, set}`）。可改的栏以工具 schema 列出的名单为准，`bleed` / `bg` /
   `caption_style` / `highlight_style` / `transition` / `texture` 都在里面。两处容易写错：`transition`
   写的是**这一镜之后**那处接缝（末镜写了不生效）；`bleed` 必须真是布尔。
   改 `transition` / `texture` **不重烧这一镜**，改 `caption_style` / `highlight_style` /
   `bleed` / `bg` **要重烧**。

<!-- /section -->

# 闸表（模型看得到的全部闸）

## 质检档 `qc`：预览档（缺省）与严格档

- **`preview`（缺省，有人在看）**：主流做法——`plan_motion` 过了契约闸后会给**每镜渲一张预览静帧**，
  渲染前的计划确认卡上把这些图摆给用户看；人眼替掉了机器的品味判断。所以这一档里：
  - 契约层的 ⑨ ⑫ ⑯ 只出「（建议）」不打回；**⑦「每镜必画」在这一档照旧打回**（见下）；
  - 渲染时的像素闸（④ ⑥ ⑭ ㉑，以及拼接前的 ⑤ ⑰ ⑲-a ⑳(c) ㉕、转场生效复核）**只记进 `qc_warnings`，照常出片**。
  - 交付时把 `qc_warnings` 里要紧的几条照实转告用户；**不要**自己据此改码重渲——要不要改由用户看过成片再说。
- **`strict`（无人值守）**：spec 顶层写 `"qc": "strict"`（或服务端设 `MOTION_QC=strict`），所有机器判据照旧硬拦，
  换「没人看也敢出片」的保证。定时任务、批量出片用这一档。
- 两档都硬拦的：lint、帧账、对比度⑩、转场会吃掉话尾。

**⑮ ⑲ ⑳ 三道多样性配额只提醒、不打回**（回执里带「（建议）」前缀，渲染前计划确认卡上会列给用户看）：
整片有意统一色调、统一版式族、或需要更多原创镜时可以不满足它们，但要在答复里说一句为什么。
**⑦「每镜必画」是质量底线，两档都是硬闸**——它拦的不是品味，是「这一镜画面上根本没有东西」，
那件事没有「人眼替机器判」的余地；其余各闸也都是硬闸。

判点分四档，按**烧不烧像素**排：契约闸（一帧不烧，改十遍只花几秒）→ 容器 lint →
逐镜渲染 → 拼接之前。能在前一档修好的错别留到后一档——后一档每改一次都是十几秒的真渲染。

## 契约层：`plan_motion` 入库前、烧像素前、`patch_motion_code` 落盘前，三处读同一份判据

| 闸 | 判什么 | 违规怎么办 |
|----|--------|-----------|
| ⑦ | **每镜必画**：每一镜都得用代码画出东西来（`rough`/`three`/`katex`/`vector` 四条画法通道至少命中一条）。只有文字与色块的镜逐镜点名打回，镜数多少都判 | 给那一镜补画 |
| ⑨ | 跨镜代码骨架同构（`STRUCT_SAME=0.88`，≥3 镜一组当场打回） | 换卡/调参数 |
| ⑩ | 字色对比度（`3:1` 正文 / `4.5:1` 强调，正文与强调各按所有底色取最好一档） | 换色 |
| ⑫ | `derived_from` 骨架相似：本镜代码 vs 所声明 demo 的最高相似度 ≥ `DERIVED_MIN_SIM=0.61` | 先读那张 demo 再写码 |
| ⑮ | 版式族配额（卡名种类 ≥ `ceil(镜数/4)`；同一**族** ≤2 镜） | 换卡 |
| ⑯ | 主体具名（每镜 `subject` 必填；有图形主体的镜 `subject_channel` 与代码对账） | 补 subject / 改申报 |
| ㉝ | **具名件**：每一镜的代码里至少用上一件 `@dg/lib/draw/*`（人/机器/厂房/齿轮/图表…），import 了还得真用上 | 照「每一镜都要画出文案点到的那件东西」那一节拆零件 |
| ⑲ | 风格配额（**构图侧**）：满幅镜数 ≥ `ceil(镜数/5)`（打回）；质感种数、`bg` 申报率只提醒。**不数色板种数**——整片一套底色是缺省，不是错 | 写 `bleed` / 补申报 |
| ⑳ | 原创配额：走 `<original>` 哨兵的镜 ≤ 整片的 20% | 改回派生自 demo |
| ㉑（闸前那一半） | 动效事件的**锚**是不是合法字段（纯契约，一次 ffmpeg 都不开） | 改锚 |
| ㉜ | 字幕归属：这一镜申报了 `caption_style: none`（不上字幕），代码里却摆着整条 `<CaptionLayer/>`（它自守不画，那条字幕永远不会出现） | 改成要画的那一档，或改用 `<CaptionText/>`，或把那处删掉 |

## 容器 lint（渲染前，一帧不烧）

| 闸 | 判什么 | 违规怎么办 |
|----|--------|-----------|
| ① | 镜头代码能编译、有默认导出、import 在白名单内、字族在白名单内、`fields` 声明与代码读/标注对账 | 改到过 |

## 逐镜渲染时判（从这一档开始烧像素）

| 闸 | 判什么 | 违规怎么办 |
|----|--------|-----------|
| ② | 帧预算：旁白按 TTS 实测算帧（`narration_for`），全片累计 ≤ `MAX_FRAMES_TOTAL`；时长归声音不归模型 | 减镜 / 缩文案 |
| ③ | **帧账对账**：本地 ffprobe 实测的帧数与尺寸 vs 分镜账，**不静默截帧、不静默补帧** | 查渲染腿后重烧 |
| ⑥ | 字段对账：回读的自报值 vs 注入值逐项比（`number` 容差 **0**）。**只对套母版的镜生效**：自包含的镜没人回读 `fields`，记「未回读」，不按通过算 | 补/删字段 |
| ⑭ | 动效密度：8 Hz、64 px 宽灰度采样，逐帧平均差 0–255；入场门槛 `1.5`、驻留 `0.6`；要数得出 **≥2 个先后变化段**，相邻段起点相差 **≥0.4 s** | 见「动效密度（闸⑭）」 |
| ㉑（闸后那一半） | 动效**触发帧**在不在申报锚的首次可读窗口里（读的就是 ⑭ 刚数出来的段） | 改锚或改时序 |
| ④ | 画面非空 + 带内填充（`EMPTY_MIN_RATIO=0.02`→2%、`FILL_TARGET=0.60`→60%、`SPAN_MIN=0.45`→45%、`TIGHT_MIN=0.30`→30%；基准色三级取：回读的实际底色 → 这一镜申报的 `bg` → `TOKENS.bg`） | 换卡/加内容 |
| ⑤ | 跨镜一致性：回读 token 逐键比，**容差 0**。**只对套母版的镜生效**：自包含的镜没人回读 token，整片都自包含时这一格记「未判」，不按通过算 | 补 props |
| ⑧ | 版面回读：叠字 / 被画面带裁 / 引线悬空 / **字压图**。前三条**只记账，永不拦截**；**字压图**（一块字被画出来的图形盖住 ≥30%）会把你这一镜**打回重看**。**每一镜都有读数**——探针挂在 Composition 上，套不套母版都扫得到（`by` 栏说这一行是谁报的：母版量画面带、测量壳量整帧） | 拿读数自己改；字压图见「字不许压在图上」 |
| ④-b | 静帧审美（**默认关**，开了才跑）：判红是**提醒**不是打回 | — |

## 拼接之前（每镜像素已烧完，但一帧都不拼）

| 闸 | 判什么 | 违规怎么办 |
|----|--------|-----------|
| ⑰ | 声画对账：旁白关着切片却响着电平 / 旁白开着却没有一镜带音轨 / BGM 短于成片 / 转场把字切掉 | 改 `bgm` / 时长 |
| ⑲-a | 主体在场（几何）：**可画件**（`path/rect/circle/ellipse/polygon/polyline/line/img/canvas/video`）的并集外接盒占带 <18% **且**文字盒占带 >45% ⇒ 判成「卡片+文字」；申报了 `subject_channel` 而探针数到 0 件 ⇒ 主体空壳 | 把主体按画面带重排；**用 div 画的色块条/卡片在账上算 0 主体** |
| ⑳(c) | 画面同构：两镜的 DOM 指纹相同＝同一张卡复制了一份 | 换卡 |
| ㉕ | 徽章构图：整镜只有 **1 件主体** + 它铺到带的 **≥22%** + 中心离带正中 **≤8%** + 长边/短边 **≤1.35**，四条同时成立 ⇒ 那是一枚徽章（或一块色斑）不是一个场景（读数：`subject_boxes` 逐件盒 + `band`） | 加背景、加第二三件、加标注，把这一件扩成一个场景 |
| ㉖ | 回看环（**默认关**）：红/未判的镜先回看改码，改完**只重烧那一镜**；判据不可用时在烧像素之前打回 | — |

**未判不是通过**：⑭、⑰、⑲、⑲-a、⑳(c)、㉕ 都是**三出口**（通过 / 打回 / 未判），
回执里未判单列一栏，不等于绿——「没量到」与「量到了没问题」是两件事，并成一格就是把
没量的说成合格的。闸⑮ 那条族配额在镜数不到 6 时**不判**（三镜片谈「族配额」连分母都不成立），
但闸⑦ 的「每镜必画」没有这一档——三镜片也逐镜判。

# 画面丰富度合同（四类母题怎么选通道）

一条片子要有画面，靠的是**每一镜都真的画了东西**，而画面只有一条路：**你逐镜写 TSX 代码把它画出来**。
四类母题各有最顺手的那条通道，先照这张表想：

| 母题 | 用哪条通道 | 怎么写 |
|------|-----------|--------|
| 器物、材质、草图、手绘感的形状 | `rough`（`roughjs`） | `rough.generator(...)` 画出的每个形状都要给 `seed`，不给每帧都在抖 |
| 立体体、场景、空间关系 | `three` + `@react-three/fiber` + `@remotion/three` | 真 3D，最慢的一档，只在真要立体感时用 |
| 公式、推导 | `katex` | 必须 `import "katex/dist/katex.min.css"`，不然渲出来是没上下标的纯文本 |
| 流程、曲线、几何、图示、白板描线 | `vector`（裸 `<svg>`/`<path>`/`<rect>`）或 `@remotion/shapes` | 白板描线那套见「白板 / 简笔描线族」 |

**上面四类没有一类是「摆一张图」**：这条链没有取材通道——不检索素材库、不出图、用户上传的图也不上屏
（上传件那一栏因此在图形片里没有读者）。要写实观感的器物也是**画**出来的，不是找来的。

**每一镜都得用上面四条里至少一条画出东西来**（闸⑦「每镜必画」，逐镜判、**打回**）。
「左边一张卡片 + 右边一段文字」那种只有文字与色块的镜，**没有配额可摊**——落在哪一镜，
那一镜就点名打回，镜数多少都判（三镜片也判）。画法怎么选由你按题材定，不重样更好，
但那是你自己的取舍，没有闸在数字号。

**只 import 不调用那一行不算通道**——判据读的是代码体里真用得上的那个名字。

# 通道表（闸⑦）

4 条画法通道 + 1 条文字通道：

| 通道 | import 标志 | 用途 |
|------|------------|------|
| `rough` | `roughjs` | 手绘斜纹、草图质感 |
| `three` | `three`/`@react-three/fiber`/`@remotion/three` | 真 3D 场景、WebGL 渲染 |
| `katex` | `katex` | 数学公式 |
| `vector` | `<svg>`/`<path>`/`<rect>` 等 | 矢量图形 |
| `text` | （fallback） | 一条画法通道都没命中：只有文字与色块 |

**「每镜必画」的判据就是这张表**：一镜落在 `text` 那一档＝打回，点名到镜号。
用了几条、哪几条由**代码字节里的 import 与标记**自动识别，不看自报的字段。
`staticFile` 照样能用，但它只用来取**字体**（`@remotion/fonts` 的 `loadFont` 要求
`staticFile('fonts/…')`）——它不是画法通道，**摆一张图不叫画**。

另有一条文字约束（只提醒，不打回）：
- `CONCRETE_SHARE_TARGET = 0.40`（40%）：整片里含具体项（数字/引号术语/拉丁缩写）的镜占比目标 40%。这条只出提醒——地名人名器物名机器认不出，拦它会误伤一整轮重规划。

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
`caption_style: on_picture`（无底贴画面，字自己摆 `<CaptionText/>`，逐词点亮与撕纸高亮
照旧可用）+ 上面这几张卡之一。

# 版式族与主体具名（闸⑮⑯）

**每镜两个新字段**（与 `card`/`derived_from` 平级，缺 `subject` 会被 schema 直接拒）：

- `subject`：这一镜画面上**能被认出的那个东西**（名词短语，2~24 字）。写成
  「活字盘」「三条等高线」「一张 1900 年的地图」这种**器物/图形**的名字；
  **不许写抽象词**（理念/趋势/意义/文化/效率…——既画不出来也认不出来，闸 ⑯ 有一份
  拒绝名单）。这一格是给人看的：用户扫一眼卡面就知道每镜到底画了什么。
- `subject_channel`：**主体是用哪条通道画的**，取值四选一（`rough`/`three`/`katex`/
  `vector`）。规则两向都判：
  - 这一镜的代码里**有**画法通道 → 必填，且必须是代码里真用到的那条（`style` 那栏就是
    代码里认得的通道）。写着 `rough` 而代码里一个 `roughjs` 都没有 → 打回。
  - 这一镜只有文字与色块 → **不要填**。填了就是自报无证据。这一档**今天不存在**：
    闸⑦「每镜必画」已经把它逐镜打回了，⑯ 这一支是安全网。`subject` 照样要写
    （字也可以是主体，如「「毕昇」两个字」）。

**版式族配额**（闸⑮，跟 ⑦ 一样在 `plan_motion` 时就硬拦）：

- 卡名（`card` 的完整值）种类 ≥ `ceil(镜数/4)`；
- 同一**族**——就是卡库的那个类别目录名（`card` 写成 `<类别>/<卡名>`，
  如 `data/chart-live-moves` 的族是 `data`）——**最多 2 镜**。

族配额是「整片不许一个版式重复摆」那条要求的机器形态：8 镜片只用了 `data` 一族、
或者同一个卡名摆三镜，都在 `plan_motion` 就被退回（不烧像素）。选卡时**先看类别**：
一条 10 镜片至少要用到 5 个不同的类别，卡库按类别分了十类，见 `shots/INDEX.md`。

# 镜头代码的 import 名单

lint 白名单 57 项（`ALLOWED_IMPORTS`），镜头代码只能引这些：

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
`@dg/lib/draw/Figure`、`@dg/lib/draw/Face`、`@dg/lib/draw/Machine`、
`@dg/lib/draw/Building`、`@dg/lib/draw/Vehicle`、`@dg/lib/draw/Gear`、
`@dg/lib/draw/Chart`、`@dg/lib/draw/Arrow`、`@dg/lib/draw/Coin`、
`@dg/lib/draw/Book`、`@dg/lib/draw/Scale`、`@dg/lib/draw/Clock`、
`@dg/lib/draw/Crown`、
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

要版面尺寸就问 `useVideoConfig()`——自包含的镜画的就是**整帧**（1920×1080 的设计稿怎么
换算到实际画幅，见「适配 demo 代码」第 7 条）。`useBody()` 是**老跑法**（把这一镜包进
母版 `<FilmLayout>`）才有的东西，新写法不要用它：母版已下线，没有人为你裁画面带。

母版画面带尺寸（**只有套母版的镜才成立**，列在这里只是让老代码对得上）：
- 竖屏 portrait：936×1524
- 横屏 landscape：1800×732
- 方屏 square：952×764

# 画面从哪来（只有一路）

这条链**没有任何取材通道**：不检索素材库、不出图、用户上传的图也不上屏。
每一镜的画面**只有一路来源**——**你逐镜写 TSX 代码把它画出来**，四条画法通道
`rough` / `three` / `katex` / `vector`（表见「通道表（闸⑦）」）。画法怎么选由你按题材定，
没有配额在数字号；但每一镜都**必须**画出东西来——闸⑦「每镜必画」逐镜判、打回。

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
它由**根文件**（`src/entry.tsx`，每一镜都必经的那一处）副作用 import `@dg/fonts` 当场加载
（`@remotion/fonts` 的 `loadFont`，
101 片 woff2 落在 `public/fonts/`，`display: 'block'`——字形没到不先用回落字族顶着画）。
`TOKENS.fontStack` 的第二项就是它，所以走 `fitText`/`TOKENS.fontStack` 的写法天然用它。

**别写 `SF Pro Display`/`Avenir`/`Google Fonts` 那一类名字**：镜像里没有、渲染容器也不联网，
浏览器**不报错**、直接回落成默认字族——代码里写的和画面上出现的不是一回事（闸① 的
`dg/font-load-check` 就是为这条：`loadFont` 指的字节必须在 `public/fonts/` 里真存在）。

<!-- section: subject-naming -->
# 主体具名（闸⑯）：每一镜的那个「能被认出的东西」怎么写

- `subject` **每镜必填**，2~24 字，写**画面上那个东西**的名词短语（`活字盘`、`三条等高线`、
  `一张 1900 年的地图`），不要写主题词（`印刷术`）。`理念`/`趋势`/`文化` 这类抽象词在
  拒绝名单里，闸⑯ 见一个退一个——它们既画不出来也认不出来。
- `subject_channel` **有图形主体的镜必填**，四选一：`rough` / `three` / `katex` /
  `vector`。它必须是**这一镜代码里真用上的那条**（import 了还得真调用），
  闸⑯ 拿代码逐镜核你申报的那条；写着 `rough` 而代码里一个 `roughjs` 都没用，等于自报。
- **一条画法通道都没用上的镜不许填 `subject_channel`**（闸⑯ 两向都判）：这类镜没有图形通道可指，
  填了就是无证据的申报。而 2026-10-16 起这类镜**本来就到不了这一步**——闸⑦「每镜必画」
  已经逐镜打回了。⑯ 这一支留着当安全网，措辞直接点名 ⑦，不是第二条规矩。
- `subject_terms`（有人以为 ⑲-b 要的词族）**不在 schema 里，别写它**，写了会被退回来。
  ⑲-b 落地时走的是**盲描述**：先让一个看不见答案的观察员说「你看见的是什么」，再拿你申报的
  `subject` 去比。它一个词族也不需要——**`subject` 写准写具体，就是你对 ⑲-b 唯一要做的事**。
<!-- /section -->

<!-- section: draw-parts -->
# 每一镜都要画出文案点到的那件东西（闸㉝）

**这一节治的是「画得不像」。** 画面只有代码一路，而代码画不出照片——它能画的是**物件**：
一个人、一台机器、一栋厂房、一个齿轮、一条曲线、一架天平。用户要的「像」在这条路上
具体就是这句话：**这一镜的文案提到了什么，画面上就要有那个东西的简笔画，而且看得出是它。**

真机上被点名的反面例子：一镜讲「19 世纪富态」，画面上是 180 个点拼成的一个椭圆，
标注「19 世纪·富态」——形状随意、认不出是什么。那就是这一节要拦的东西。

## 三步：先认东西，再拆零件，最后落笔

1. **认东西**。从这一镜的文案里挑出**那个能被画出来的具体事物**——`蒸汽机`、`厂房`、
   `一张曲线`、`一顶王冠`、`一个正在扛东西的工人`。它就是你填的 `subject`。
   **不许拿抽象形状顶替**：「一团表示压力的椭圆」「富态轮廓的点阵图」都不是东西。
2. **拆零件**。**外轮廓 → 标志物 → 3~6 个部件**：
   - **外轮廓**：这东西的整体形状（机器 = 一个长条 + 一个圆；厂房 = 方块 + 锯齿屋顶）；
   - **标志物**：**少了它就不像**的那一件（蒸汽机的烟囱、天平的横梁、钟的指针）；
   - **3~6 个部件**：剩下的细节。**别贪多**——简笔画的力气全在前两步。
3. **落笔**。优先用下面零件库里的件拼；库里没有的那一件才自己用 `<path>`/`<circle>` 画，
   并照同样的三步来。

## 零件库 `@dg/lib/draw/*`（一件＝一个能被认出的东西）

| 件 | 画的是什么 | 主要 props（都对得上 `useVideoConfig()` 的设备像素） |
|----|-----------|-----------|
| `<Figure/>` | 简笔人：站 / 走 / 指 / 举手 / 扛东西 / 坐着 | `x`（脚底中点）`y` `h`（身高）`pose` `facing` `hold` |
| `<Face/>` | 极简头像（**指代**一个人，不是写实） | `x` `y`（头心）`size` `mood` `hat` `glasses` `beard` |
| `<Machine/>` | 蒸汽机 / 冲床 / 机械臂 / 涡轮 | `x` `y`（落地点）`w` `kind` `smoke` |
| `<Building/>` | 厂房 / 塔 / 民居 / 天际线 | `x` `y`（地面）`w` `kind` `smoke` |
| `<Vehicle/>` | 手推车 / 汽车 / 火车头 / 船 | `x` `y` `w` `kind` `facing` |
| `<Gear/>` | 齿轮（可咬合成一对） | `x` `y`（圆心）`r` `teeth` `spin` |
| `<Chart/>` | 坐标轴 + 曲线或柱 | `x` `y`（绘图区左上）`w` `h` `kind` `values` `upto` |
| `<Arrow/>` | 箭头：指、连、推、变 | `x1` `y1` `x2` `y2` `bend` `upto` |
| `<Coin/>` | 钱：单枚 / 一摞 | `x` `y` `r` `kind` `count` |
| `<Book/>` | 书：合着 / 摊开（可翻页） | `x` `y`（下缘中点）`w` `open` `flip` |
| `<Scale/>` | 天平（可倾斜） | `x` `y`（底座）`w` `tilt` |
| `<Clock/>` | 钟（指针按你给的角度） | `x` `y` `r` `hour` `minute` |
| `<Crown/>` | 王冠 | `x` `y`（下缘中点）`w` `points` `gems` |

三件事它们**已经替你办好**，不要再各写一遍：

- **颜色**：不传 `ink` 就取整片那一档（字色 / 强调色 / 纸色），与画面里别的东西同色；
  要**强调**某一件才传 `ink={{ accent: … }}`。
- **手绘感**：底下是 roughjs 的手绘线，同一个 `seed` 渲两次逐像素相同（不会逐帧抖）。
- **坐标**：就是成片设备像素，与你的 `<svg viewBox={`0 0 ${width} ${height}`}>` 同一套。

零件**不读帧**：要让齿轮转、天平斜、曲线一点点长出来，由你把帧算成 props 传进去
（`spin` / `tilt` / `upto` / `smoke` / `flip`）。这样「这一镜动的是什么」永远写在
你的代码里，一眼看得见。

```tsx
import { Machine } from '@dg/lib/draw/Machine';
import { Building } from '@dg/lib/draw/Building';
import { Figure } from '@dg/lib/draw/Figure';
import { Gear } from '@dg/lib/draw/Gear';

const spin = interpolate(frame, [0, 90], [0, 120]);
const puff = interpolate(frame, [0, 60], [0, 1]);

<svg viewBox={`0 0 ${width} ${height}`} width={width} height={height}>
  <Building x={width * 0.26} y={height * 0.78} w={width * 0.34} kind="factory" smoke={puff} />
  <Machine x={width * 0.66} y={height * 0.78} w={width * 0.28} kind="steam" smoke={puff} />
  <Figure x={width * 0.44} y={height * 0.78} h={height * 0.22} pose="carry" />
  <Gear x={width * 0.87} y={height * 0.26} r={width * 0.045} spin={spin} />
</svg>
```

## 真人：`<Face/>` 是**指代**，不是写实

「马斯克」「牛顿」这种具体的人，**几笔线画不出那张脸**——真实长相得有现成像素，而取材
那三条路（检索素材 / 出图 / 上传件上屏）已经从产品里删掉了，不在这条链的能力范围内。
这一类镜头的正确画法是 `<Face/>`（或 `<Figure/>`）+ **名字标注**：观众读到的应该是
「画面上这个人在讲」，不是「这头像长得像他」。**不要**为了「像」去堆五官细节——
那既不像，又把画面弄脏。

## 闸㉝：每一镜至少用上一件库里的件（硬拦，烧像素之前）

判据是**静态**的：这一镜的代码里必须**真的用上** `@dg/lib/draw/*` 里的至少一件
（import 了还要在 JSX 里出现，与 `subject_channel` 同一套证据口径）。

- 库里的件**不够用**时照样自己画，但**同时也要用上至少一件**——哪怕只是一支 `<Arrow/>`
  指着你画的那个东西。这一道管的是「画面上有没有一件具名的东西」，不是「能不能自己画」。
- 用上库里件的镜，`subject_channel` 填 **`rough`**（那些件底下就是 roughjs）。
- 它**判不出「像不像」**：一件 `<Figure/>` 摆在角落、主体仍是一团点阵，这一道不响。
  所以别把「㉝ 过了」当成「画对了」。

## ⑲-b：还有一只眼在问「画面上那个东西，是它吗」

「像不像」落在判据上的那一格叫 **⑲-b**（`judge_identity`）。它的问法值得你知道，因为你
正是照它写代码的：**先有一个看不见你申报内容的观察员，只看画面回答「我看见了什么」**，
再拿你填的 `subject` 去比。所以：

- **`subject` 写的是画面上真出现了的那件东西**（「蒸汽机」），不是这一镜的主题（「工业化」）。
  观察员说出「一台带烟囱的机器」，就对得上；他说不出画的是什么，就直接算**不像**。
- 它**认形状与类别，不认写实**：「工业革命」那一镜画出厂房 + 工人 + 冒烟的烟囱，
  这是对得上的；画成一团「富态轮廓的点阵」是对不上的——**这一点是这一条最容易踩的地方**。
- 它**不认人脸**。具体真人的那几镜，用 `<Face/>` + 名字标注（见上一节），
  别指望画出长相。
- 这条判据**默认不开**（每镜一次带图调用是真钱）。开着的片子，红了的镜会回给你：
  「它看见的是 X，你写的是 Y」——照那句话改画面，不要改 `subject` 去迁就画面
  （改申报去迁就画面＝把判据关掉，闸⑯ 也会拦）。
<!-- /section -->

<!-- section: motion-density-gate -->
# 动效密度（闸⑭）：这一镜得在像素上数得出「先后动」

判据读的是**成片像素**，不是你写了几个 `interpolate()`：容器把切片按 **8 Hz** 抽帧、每帧
缩到 **64 px 宽**的灰度图，逐帧算平均绝对差（0–255），把连续高于门槛的采样点并成
「变化段」——**入场 1.5、驻留 0.6**；要数得出 **≥2 段**，且相邻两段的起点相差 **≥0.4 s**
（30fps 就是 12 帧）。三条写码纪律，不按它们来读数会和你眼睛看到的不一样：

1. **两段要真的隔开**。一整段匀速动效（从 0 动到末）只数得出 1 段，必红。把动效排成
   **先后来**：揭示/入场占前 40% 左右，第二段排在它之后 ≥0.4 s——位移、切换、收束都算。
   同一时刻一起动的五件事，在 8 Hz 下仍是一段。
2. **「淡入替你过关」这层兜底没有了**。2026-10-14 真机实测的是**套母版**的镜：`entrance: fade`
   在头 15 帧给出全片最大的峰值（多镜峰值 50–80 全落在第 0–15 帧），于是细线、低对比的动效
   被它盖过去也能过。母版下线后**没有人再替你画这一层**——`entrance` 只是注入的两个数
   （见「转场与进出场」），你不写 `interpolate` 就没有那一段。所以「两段都要由**画面内容**
   自己贡献」这条比从前更硬：连第一段都别指望默认动画。
3. **变化要够大**。采样量的是**整帧**的平均差，分母永远是整帧：动效只占画面一小块时，
   同样的位移平摊到整帧就被稀释（16:9 的 demo 等比缩进竖屏后只占三成面积，稀释约 3 倍）。
   描线、1 px 位移、缓慢色相变化都过不了 1.5 的入场门槛。要么把动效做在主体上（位移、
   缩放、明暗块面切换），要么按整帧重排这一镜的版面（见「适配 demo 代码」第 7 条——
   那里也写了为什么**不能**用裁切换过闸）。
4. **满幅底板能救闸④，但会从两个方向把画面读空**（这两条是 2026-10-14 一条 12 镜整片
   实测出来的，不是推的）：
   - 底板或遮罩**不透明地盖在会动的像素上面**，逐帧差直接归零。回执写「数得出 0 个变化段
     ……内容峰 0.00」就是被盖住了——**不是**"你没写动画"。底板要么 `fill="none"`，
     要么放在动内容**之下**，别让它在 z 序上压住主体。
   - 「先入场、后逐段揭开」那种节奏（前四成时间底板上还没东西）会让闸④ 的 **0.15 那个
     采样点读到空底**。这一镜照样能过④——它的判据是「最低帧 <2% **且** 最满帧 <45%」
     两条同时成立才打回，底板把后一条顶住了。于是账上会出现 `非底色 0.0%` 而闸是绿的。
     **过闸不等于画面不空**：观众实打实看到前四成时间是一张空卡。要满幅就在入场那一刻
     把内容铺进带子，不要靠"揭开"填时间。
5. **闸⑲-a 的主体读数取的是片尾那一帧**（探针落在 `durationInFrames - FIELD_PROBE_TAIL`），
   所以主体得**撑到结尾仍在场**；中途出现又撤走的主体，在账上等于没有。**自包含的镜没有
   这一份读数**（扫读数的是母版），⑲-a 对它们记未判——「主体撑到结尾」得你自己保证。
   另外只有可画图元进主体计数（`path/rect/circle/ellipse/polygon/polyline/line/img/canvas/video`），
   **div 铺的色块条算 0 主体**——用 div 画的卡片配满幅公式，正好落在「卡片+文字」那条
   联合判据上被打回（同一镜的图元只有 2 个时，折到成片上不足带的 1%）。

`exit` 与**代表帧**的取舍：缓存里那张代表帧取的是 `durationInFrames-2`，落款淡出会把这一帧
扫成接近空底；闸④ 的采样点又正好在 0.15/0.5/0.85。所以整片用 `"exit": "none"`、
结尾交给转场，不要靠逐镜淡出收尾。
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
  写法；该申报就如实申报，别指望把锚全挂到 `fields.` 上就能过。
<!-- /section -->

<!-- section: draw-floor -->
# 每镜必画（闸⑦）：每一镜都得有画出来的东西

- 判据**逐镜**走：这一镜的代码里得真用上 `rough` / `three` / `katex` / `vector` 四条画法通道
  里至少一条（判据见「通道表（闸⑦）」）。一条都没命中＝只有文字与色块＝**打回**，
  回执点名是哪几镜。**没有配额可摊**：不存在「几镜可以纯文字」这一档。
- **首尾镜也在内**。开场与收尾常常是标题与落款，那也要画——标题本身可以是一笔画出来的字形
  轮廓、一块有质感的底纹、一条描线勾出的框。**没有任何一镜豁免**。
- **镜数多少都判**：这条没有最低镜数（旧版那个「≥6 镜才判配额」的口子已经关掉），
  三镜片也逐镜判。
- 「纯文字镜**不得连续 3 镜**」这句**今天没有判据**，而且也不需要了——一镜纯文字都不许有。
- 与这条**成对**的是闸⑲（构图侧：满幅 / 质感 / `bg` 申报率）：⑦ 管地板（每一镜都有画面），
  ⑲ 管构图上的多样。④ 的像素判据（画面非空）是它在成片像素上的一次复核。
  **底色不归 ⑲ 管**：整片一套是缺省。
<!-- /section -->

<!-- section: sound-design -->
# 声音：旁白与配乐

- **配乐**：用户随消息附了配乐，就把那条附件的 material_id 直接填进 `render_motion_code`
  的 `bgm`，不用再选、也不用问。没附配乐时从曲库选一条（曲库歌曲的 material_id 同样能直接
  填 `bgm`）；曲库里也没有合适的就出纯人声片并在答复里说一句，**不要停下来问**。绝不去网上
  下载（`fetch_media` / `web_search` / `fetch_url` 都不许用来找音乐）。配乐比成片短时会自动
  循环，不会打回。
- **卡拍是自动的**：有配乐时渲染会先检测拍点，把每镜的切点往后补到最近的拍上（只补不裁，
  最多约半拍）。不想卡拍就在 spec 顶层写 `"beat_sync": false`。出片回执的 `beat_sync` 栏
  会报检到的速度与补过的镜。
- **有旁白**（`narration=true`）：人声由 TTS 合成、逐词时间轴随 props 注入（`words`），
  **不要写每镜时长**（镜头长度归真实语音时长）。第几个词可用 `subtitle:<i>` 锚动效
  ——那是今天唯一真判得动的一档锚。
- **没旁白**（`narration=false`）：没有语音时长可依，**每镜必须给 `min_duration_sec`**
  （5~15 秒，给观众读字幕的时间），全片总长就是各镜 `min_duration_sec` 之和。
- **声音设计的细节不在这份正文里**（响度、BGM 压低、停顿、语气）：要动它们就
  `load_skill(name="motion_explainer_skill", path="sound-design.md")` 取附件全文再动手。
<!-- /section -->
