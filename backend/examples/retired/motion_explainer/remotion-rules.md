# Remotion 写法手册（官方规则摘编）

这一份是**写镜头代码时的手艺**，随镜像走（渲染容器不联网，不能临时去拉官方 skills）。
它只讲 API 怎么用对；**画什么、怎么画好看**在 `SKILL.md` 的「视觉手艺」一节。

版本：Remotion 4.0.534 / React 19。下面出现的 API 都在镜像里装着。

## 1 时间与缓动

- 时间只有两个来源：`useCurrentFrame()`（这一镜内的帧号，从 0 起）与
  `useVideoConfig()`（`fps` / `durationInFrames` / `width` / `height`）。
- **不要用 CSS `transition`/`animation`、`setTimeout`、`requestAnimationFrame`**：
  Remotion 是逐帧截图，墙钟时间与帧不同步，这些写法在成片里要么不出现，要么每次不同。
- `interpolate(frame, [a, b], [x, y], options)` 的 `options` **必须显式 clamp**：

  ```tsx
  import { interpolate, Easing } from 'remotion';
  const x = interpolate(frame, [0, 30], [0, 200], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.cubic),
  });
  ```

- 物理感用 `spring`：

  ```tsx
  import { spring } from 'remotion';
  const s = spring({ frame, fps, config: { damping: 18, mass: 0.8, stiffness: 120 } });
  ```

  `spring` 默认从 0 长到 1，超过 1 后回弹；要「落定不回弹」加
  `overshootClamping: true`。**同一层的元素共用一套 spring 参数**，只有 stagger 不同。
- 多段动画用 `interpolate` 的多个区间，或 `Easing.bezier(...)`；不要把逻辑塞进
  `useEffect`。

## 2 序列与时间轴

- `<Sequence from={f} durationInFrames={n}>` 把子节点的时间轴**平移**：子节点的
  `useCurrentFrame()` 从 0 起。只想要位移、不想多包一层 DOM 时加 `layout="none"`。
- 连续的几段用 `<Series>`：

  ```tsx
  <Series>
    <Series.Sequence durationInFrames={90}><A /></Series.Sequence>
    <Series.Sequence durationInFrames={60}><B /></Series.Sequence>
  </Series>
  ```

- 提前挂载（避免第一帧字体/三维还没准备好）用 `<Sequence premountFor={fps}>`。
- 这一镜的总时长是 `useVideoConfig().durationInFrames`，**不要写死帧数**；各段时长按
  比例从总时长算。

## 3 字体与文本

- 字族白名单见 `SKILL.md`「字体白名单」。中文字体的真源是 `@dg/fonts`（根文件已经
  副作用加载）；要显式加载某一份用：

  ```tsx
  import { loadFont } from '@remotion/fonts';
  import { staticFile } from 'remotion';
  loadFont({ family: 'Noto Sans SC', url: staticFile('fonts/xxx.woff2') });
  ```

- 量文字宽度用 `@remotion/layout-utils` 的 `measureText({ text, fontFamily, fontSize, fontWeight })`
  与 `fitText({ text, withinWidth, fontFamily, fontWeight })`，**不要**用 DOM 的
  `getBoundingClientRect()`（截图环境里布局时序不可靠）。
- 行高、字距写 inline style；`letterSpacing` 是像素，不是 em。
- 中文断行按语义手动切，不要依赖 `word-break`；一行最多 18~22 个汉字（按字号与画宽调）。

## 4 图表与路径

- 折线/曲线用 `@remotion/paths`：

  ```tsx
  import { getLength, getPointAtLength, evolvePath } from '@remotion/paths';
  const { strokeDasharray, strokeDashoffset } = evolvePath(progress, d);
  ```

  `progress` 从 0 到 1 就是「线一点点长出来」。要一个点在线上走，用
  `getPointAtLength(d, progress * getLength(d))`。
- **真数据图不要自己算贝塞尔**，用 `d3-shape` + `d3-scale`（2026-10-23 起可用）：

  ```tsx
  import { line, area, pie, arc, curveBasis } from 'd3-shape';
  import { scaleLinear } from 'd3-scale';
  const x = scaleLinear().domain([0, 1]).range([W * 0.1, W * 0.9]);
  const y = scaleLinear().domain([0, max]).range([H * 0.75, H * 0.25]);
  const d = line().x((p) => x(p.t)).y((p) => y(p.v)).curve(curveBasis)(data) ?? '';
  ```

  `line` / `area` / `pie` / `arc` 都返回路径字符串，直接喂 `<path d={d}>`。
  刻度、对数轴、band 轴（等距柱子）全在 `d3-scale` 里，不要手写分段。
- **两条路径之间形变**用 `d3-interpolate-path`（同点数）或 `flubber`（任意形状）：

  ```tsx
  import { interpolatePath } from 'd3-interpolate-path';
  import { interpolate as morph } from 'flubber';
  const d = interpolatePath(progress, pathA, pathB);       // 点数相同
  const d2 = morph(pathA, pathB)(progress);                 // 任意轮廓
  ```

  手写形变会在两点数不一致时直接崩，这两件是唯一干净的解法。
- **图标不要自己画**：`lucide-react` 有 1400+ 个，纯 SVG 组件、可树摇：

  ```tsx
  import { Sun, ArrowRight, Activity, TrendingUp } from 'lucide-react';
  <Sun size={120} color="#ffb15c" strokeWidth={1.6} />
  ```

  `size` 是像素。`color` 走 `currentColor`，`strokeWidth` 控制线粗。
- **配色推导不要手算 RGB**：`chroma-js` 做明度、混合、对比度：

  ```tsx
  import chroma from 'chroma-js';
  const dim = chroma('#ffb15c').darken(0.6).hex();
  const mix = chroma.mix('#0b1020', '#c4611f', 0.4).hex();
  ```
- 基础形状（圆、矩形、星、多边形）用 `@remotion/shapes`，它们返回 `path`，可以接
  `strokeDasharray` 做描线。
- 手绘感叠一层 `roughjs`：`rough.generator()` 的每个 shape 给固定 `seed`；
  `rough.svg(el)` 产出的节点直接塞进 SVG。
- 柱状图从基线长：`scaleY` 的锚点用 `transformOrigin: 'bottom'`，或直接算高度。

## 4.5 转场

`@remotion/transitions` 与 `remotion` 同版本（2026-10-23 起可用）。**整片是硬切时不要用**；
镜与镜之间确实要叠的时候：

```tsx
import { TransitionSeries, linearTiming } from '@remotion/transitions';
import { fade } from '@remotion/transitions/fade';
import { slide } from '@remotion/transitions/slide';
import { wipe } from '@remotion/transitions/wipe';

<TransitionSeries>
  <TransitionSeries.Sequence durationInFrames={90}><ShotA /></TransitionSeries.Sequence>
  <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: 15 })} />
  <TransitionSeries.Sequence durationInFrames={90}><ShotB /></TransitionSeries.Sequence>
</TransitionSeries>
```

`presentation` 可用：`fade()` / `slide({ direction })` / `wipe({ direction })` / `flip()` /
`clockWipe({ width, height })`。**注意**：转场会让前后两镜的时间重叠，整片总长会变短
（两段各 90 帧 + 15 帧转场 = 165 帧，不是 180）——分镜给的时长要按重叠后的实际值算。

## 5 三维

- `@remotion/three` 的 `<ThreeCanvas>` 要**显式给相机**：

  ```tsx
  <ThreeCanvas width={width} height={height} camera={{ position: [0, 0, 4], fov: 45 }}>
    <ambientLight intensity={0.6} />
    <directionalLight position={[2, 3, 4]} intensity={2.2} />
    <mesh rotation={[0, frame / 60, 0]}>...</mesh>
  </ThreeCanvas>
  ```

- 默认相机在 `z=5`，几何体取 0.2~1.0 单位；标准材质的灯光 `intensity` 要到几才看得见。
- 三维里**不要**用 `useFrame` 驱动时间：帧号从 `useCurrentFrame()` 算，再传给
  `rotation`/`position`。
- 贴图只能用代码生成的 canvas / SVG data URI；不联网、不读本地文件。

## 6 字幕

- 本仓的字幕是**画面的一部分**：`<CaptionText/>` 把这一镜的整句排进构图。逐词点亮用
  `highlight` 与 `useCaptionTiming()`；不要在底部再放一条全局预设。
- 字幕的时间轴从 `useCaptionTiming()` 拿；需要按字出场时用 `charStartFrames()`。
- 字幕与背景的对比度至少 4.5:1；压在复杂画面上时先加一层低透明度的纸色/墨色底，
  不要用纯黑阴影硬描边。

## 7 测量与自适应

- 画幅从 `useVideoConfig()` 拿，所有坐标按 `width`/`height` 的比例写。
- 字号 = `TOKENS.sizeBody * (width / 1080)` 这类比例换算；版心边距同理。
- 需要「刚好放下」时用 `fitText`，不要让文字溢出后被裁掉。

## 8 确定性与性能

- **禁 `Math.random`**：用 `random(seed)`（Remotion 的确定性随机）或自己的 mulberry32。
  纹理的 `feTurbulence` 必须写死 `seed`。
- 禁 `Date.now()`、`performance.now()`、`setInterval`、`fetch`、`eval`、`new Function`。
- 大面积高斯模糊/位移滤镜很贵：一镜最多一两层，且尽量作用在小元素上；能拿渐变近似的
  就别用 `filter`。
- 逐帧重算的几何/路径，尽量用 `useMemo` 按 `frame` 缓存；不要在每个子元素里重复算同一组常量。
- 一份代码渲两遍必须逐像素一致——这是判断「有没有把随机/墙钟混进来」最直接的办法。

## 9 一镜多实体：把文案画成一个场景

参考片的一帧常常同时有 4~6 件东西（鸟、巨型蛋、三枚小蛋、草丛、落日），它们**同处一个
空间**。多画几件不是为了填满，而是让「谁对谁做了什么」一眼读得出来。

- **先摆空间，再画细节**：定视平线 → 分远/中/近三层 → 把实体按「离镜头多远」放进层里。
  近的压住远的（遮挡），远的加空气透视（降低对比、偏底色）。
- **实体之间要有关系**：朝向（看/被看）、接触（站在/靠着/压着）、大小对比（巨蛋 vs 小鸟）、
  遮挡（草丛挡住半个蛋）。避免「一排等大的图标 + 各自标签」——那是图例，不是场景。
- **每件实体至少三个可认细节**：外轮廓 + 一个标志物 + 一个朝向/动作（鸟的喙与红眼、
  蛋上的斑点、桶的箍与提手）。两件实体不要用同一套轮廓，否则观众读成「一样的东西」。
- **一套光**：整帧只有一个主光源方向；每件实体都有亮面/暗面，落地的有接触影
  （贴地的小而深）和长影（沿光方向）。影子是让物体「站进画面」的最便宜的一招。
- **一套色**：实体颜色从 `resolveFilmStyle` / 色板取，靠明度与饱和度分层，不要每个实体
  一个高饱和色；强调色只给文案里最该被看见的那一件。
- **动效分两拍以上**：实体 stagger 入画（远的先、近的后，或按叙述顺序）→ 主运动（镜头
  缓推、视差、物体呼吸/转动/被风吹动）→ 收住。不要整帧一个 `opacity` 淡入到底。
- **文字是场景的一部分**：字幕/标签贴着对应的实体排（引线、局部标注），不要盖住实体，
  也不要把所有字堆在画面底部。
