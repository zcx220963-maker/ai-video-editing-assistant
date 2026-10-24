---
name: glow-bloom-html-canvas
一句话: 真光晕三式——A glow-core 单发光体大片柔光、B glow-hud 读数条各发本色、C glow-warm-spill 暖光溢到相邻色块
适用: 画面里有一个「发光的东西」当主角时（行星/灯泡/读数核/播放键）；参考片那种「亮部往外渗一大圈柔光」的观感，靠渐变叠不出来，只能靠这一条
时长: A 150f（5s）/ B 120f（4s）/ C 165f（5.5s）
能量: 低到中（光在呼吸，不是靠位移抢戏）
标签: effects
---

## 意图

**这是唯一能出「真光晕」的做法。** 手工叠多层径向渐变只能做出「一块渐变」——
边缘是硬过渡，暗部不会溢光。参考片里那种发光体外围一大圈柔光的观感，来自
**按亮度阈值提取 + 模糊 + 加色**，只有 `@remotion/effects/glow` 能做到。

**它只挂在三个宿主上**：`<HtmlInCanvas>` / `<Img>` / `<Solid>`。
写在 `<div>` 或 `<svg>` 上是**静默空操作**——不报错，也没有任何效果。这是这张卡
存在的主要理由：不知道这条的人会写一版"看起来很对但什么也没发生"的代码。

`<Solid>` 不能用：它是纯色矩形，glow 只会把它整体提亮、颜色跑偏（橙会烧成黄），
**没有向外扩散的光晕**（2026-10-24 真渲对比过）。所以只有 `<HtmlInCanvas>` 这一条路。

## 三式选型

| 式 | 做法 | 适用 |
|----|------|------|
| A glow-core | 一个亮核（圆/圆角块）放在画面主位，`radius` 给到 220~300，核本身与光晕一起呼吸（`intensity` 1.4→2.0 缓慢往复） | 行星、灯泡、播放键、单颗读数核 |
| B glow-hud | HUD 的读数条/刻度条各自发光：整体一层 glow，`color` 取**这一镜主色**；条本身用饱和色，暗部保持干净 | 仪表盘、数据条、刻度尺 |
| C glow-warm-spill | 暖色主光 + 一块冷色副块，glow 的 `color` 取暖色——冷块边缘会染上一点暖光，读作「同一空间里的两盏灯」 | 双色对峙、暖冷对比段 |

## 参数表

| 参数 | 典型值 | 调节手感 |
|------|--------|----------|
| `radius` | **480~640**（A 取 520~640，B 取 400~480） | **320 太小**：圆外 240px 只剩 10/255，看着像一圈描边；560 在圆外 240px 还有 30/255 才铺得开 |
| `intensity` | 1.8~2.4 | 只抬近处亮度（2.4 时圆外 80px 到 102），铺开靠 radius；超过 2.6 橙会烧成黄、青会烧成白 |
| `threshold` | 0.3~0.4 | 源本身是亮的时候 0.2 与 0.35 **逐像素相同**，别再往下调；调低只会把中间调拖进光晕、画面发糊 |
| `color` | **这一镜的主色** | glow 是**整幅染色**，写死一个色会让画面里别的颜色也染上它；一镜只用一组 |
| `width` / `height` | 与画幅一致（1920×1080） | 不一致会缩放，边缘出现一圈黑 |

### 实测曲线（1920×1080 / `#05070d` 底 / 白圆半径 160 / 量圆外亮度）

| radius | intensity | 圆外 80px | 圆外 240px | 整幅平均 |
|---|---|---|---|---|
| 320 | 2.0 | 81 | **10** | 25.4 |
| 560 | 2.0 | 86 | **30** | 30.0 |
| 800 | 2.0 | 63 | 37 | 31.3 |
| 560 | 2.4 | 102 | 35 | 32.8 |
| 560（threshold 0.2） | 2.0 | 86 | 30 | 30.0 |

判读：**想把光晕铺开就加 radius，想让它更亮就加 intensity**，threshold 在源本来就亮时是个死参数。

## 代码骨架

```tsx
import { AbsoluteFill, HtmlInCanvas, useCurrentFrame, useVideoConfig, interpolate } from 'remotion';
import { E } from '@dg/motion';                 // 闸① 要求：必须 import @dg/motion
import { glow } from '@remotion/effects/glow';

const Shot = () => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const p = interpolate(frame, [0, 40], [0, 1],
                        { extrapolateRight: 'clamp', easing: E.standard });
  return (
    <AbsoluteFill style={{ backgroundColor: '#090f1a' }}>
      <HtmlInCanvas
        width={width}
        height={height}
        effects={[glow({ radius: 560, intensity: 2.0 + 0.4 * p,
                         threshold: 0.35, color: '#FF6B2C' })]}
      >
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
          <rect width={width} height={height} fill="#090f1a" />
          <circle cx={960} cy={540} r={120 + 20 * p} fill="#FF6B2C" />
          <rect x={120} y={120} width={560} height={64} rx={6} fill="#22D3EE" />
        </svg>
      </HtmlInCanvas>
      {/* HUD / 读数 / 字幕 / <F> 字段标注：DOM 叠在 HtmlInCanvas 外面 */}
    </AbsoluteFill>
  );
};

export default Shot;
```

## 两条闸① 会拦的写法

- **不 import `@dg/motion`** → `dg/must-use-motion`。哪怕只用到 `E.standard` 也要 import。
- **在 HtmlInCanvas 里用带 `background` 的裸 `<div>` 画色块** → `dg/no-raw-styled-div`。
  用 SVG `<rect fill>` / `<circle fill>`。

## 代价

`HtmlInCanvas` 每帧要把 HTML 栅格化一遍。实测（2026-10-24，2 帧静帧含冷启动）：
`HtmlInCanvas` 4.0~6.3s，`<Img>` 那条路 3.4~5.3s，同一档。**只给发光体是主角的镜用**，
整片控制在 1/3 以内；其余镜继续用「多层径向渐变 + `screen` 混合」的手工近似就够了。

## 两条实测过的死路（别再试）

- **`<Img>` 装透明底 SVG 再挂 glow**：光晕被 alpha 挡在形状里面。同一颗圆实测圆外 60px
  只亮到 4/255，等于没有。要不透明底——但那样它就会盖住下层，只能当整幅底版。
- **`<Solid>`**：纯色矩形，glow 只会整体提亮（橙烧成黄），没有向外扩散的光晕。
