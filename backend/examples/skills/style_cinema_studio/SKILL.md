---
name: style_cinema_studio
display: 暗房微距
kind: style
caption_style: outlined
highlight_style: plain
description: 【STYLE SKILL】一件真实的实物放在深色台面上，被一束暖光打亮：有体积、有材质、有投影、有景深、有颗粒。整片像夜里的微距静物摄影，适合讲健康、身体、器物这类需要「看见实物」的科普。
style_manifest: {"version": 1, "template": "cinema-studio", "use_template": true, "draw_mode": "template", "bleed": true, "palette_name": "darkroom", "texture": "film", "caption_style": "outlined", "highlight_style": "plain", "palette": {"page": "#07080a", "surface": "#14100d", "field": "#1a1512", "text": "#f4efe6", "muted": "#a89c8c", "accent": "#ffcf6b", "border": "#2a241f", "light": "#ffc994", "rim": "#5f92ff"}, "composition": {"subject_width": [0.35, 0.55], "subject_height": [0.35, 0.55], "horizon": 0.55, "camera": "slow push-in, one shot one move, no cuts"}, "recurring_prop": "一张深色台面与一束从后上方来的暖光，全片同一光位", "caption": {"look": "overlay", "position": "bottom-center", "width": 0.62, "size": 0.44, "order": "en-zh"}, "motion": {"camera": "push / pan / orbit，缓慢，一镜一个动作", "grain": 0.14, "vignette": 0.55, "haze": 0.45}, "facets": {"visual": {"invariants": [{"name": "舞台", "value": "每一镜都是同一张深色台面、同一束后上方暖光、同一圈冷色边光；背景是一块压暗的幕，不是色块拼贴"}, {"name": "实物", "value": "一镜一件主角实物，占画面宽 35–55%；有真实厚度与圆角，不存在漂浮在空中的平面图标"}, {"name": "材质", "value": "玻璃透、金属有反射、陶瓷半哑、木有纹理、颗粒物有堆积感；粗糙度有变化，不要一整套塑料高光"}, {"name": "光", "value": "单主光，明暗比约 4:1；主角有受光面、边缘光和落在台面上的接触阴影；最亮点只留给光源或金属高光"}, {"name": "景深", "value": "背景与前景掠过的物件是虚的，主角锐利；靠雾、暗角与暖冷分离把主体从背景里剥出来"}, {"name": "颗粒", "value": "整片细颗粒、轻暗角、极淡的暖色雾；不要干净得像三维软件默认渲染"}]}}, "avoid": ["平涂图标", "PPT 卡片", "纯色块堆叠", "卡通描边", "外部图片素材", "第二条字幕", "漂浮在空中的物件", "塑料感均匀高光", "过曝的死白高光"], "narration_avoid": []}
---

# 暗房微距：这套视觉怎么用

这是一套**夜里的微距静物摄影**。画面里只有一件真实的实物，放在一张深色
台面上，被一束从后上方来的暖光照亮。观众先看见光落在材质上的样子，再看见
那件东西是什么。

不是示意图，不是卡片，不是平涂图标。有体积、有反射、有投影、有景深。

## 舞台只搭一次

每一镜都用 `StudioStage`。背景幕、台面、灯光、相机、浮尘、暗角、颗粒、
背景光斑都已经在里面了，**不要自己再写 `ThreeCanvas`、灯和背景幕**：

```tsx
<FilmLayout text={text} textEn={textEn} aspect={aspect}>
  <StudioStage aspect={aspect} camera="push" light="night" floor="wood">
    {/* 这一镜的实物 */}
  </StudioStage>
</FilmLayout>
```

`camera` 取 `still` / `push` / `pan` / `orbit` / `handheld`；`light` 取
`night` / `dawn` / `clinical`；`floor` 取 `wood` / `stone` / `tile` / `dark`。
一镜只用一个运镜，缓慢，中间不切。字幕由母版画，镜头代码只把 `text` /
`textEn` 交给 `FilmLayout`，**不要自己再画第二行字幕**。

取景不合适时给 `cam`，不要另建相机：

```tsx
cam={{ from: [-0.06, 0.30, 0.98], to: [0.02, 0.285, 0.90], look: [-0.02, 0.12, 0] }}
```

`from` 是起点、`to` 是终点（`camera="still"` 时只给 `from` 即可）、`look`
是看向世界里的哪一点。坐标单位是米，台面在 `y = 0`。

`beam`（缺省开）是从左上斜下来的一束空气光，`bokeh`（缺省开）是背景虚化光斑，
`grain` / `vignette` / `haze` 是颗粒、暗角与雾。它们都是屏幕层，不参与三维。

## 实物

一镜一件主角。用几何体拼出真实厚度：`cylinderGeometry`、`sphereGeometry`、
`boxGeometry`、`torusGeometry` 都可以，但要有倒角、要有厚度、要落在台面上
（`y = 0` 是台面）。主角占画面宽 35–55%，另一侧留出空气和虚化的背景。

材质直接用模板导出的这几件，不要每次重写参数：

- `GlassMaterial`：玻璃瓶、杯、窗、镜片。
- `MetalMaterial`：不锈钢、黄铜、铝；`tone` 取 `steel` / `brass` / `aluminum`。
- `CeramicMaterial`：瓷碟、药片、搪瓷、纸。
- `WoodMaterial`：木台、木勺、木盒。
- `GranularMaterial`：盐、糖、沙、灰、粉末这类堆积物。

每一件落在台面上的实物，底下都要有一片 `ContactShadow`；差这一片，东西就
像浮着。散落的小颗粒用 `Scatter`，堆成一撮时给 `dome`（中心高几米，向外落回
台面），盐、糖、沙、灰都靠这个才不是一坨光滑的球。位置由固定种子生成，
不要写 `Math.random`；需要自己撒点时用 `seeded(i)`。

玻璃在容器这一档（swangle）里折射不稳，`GlassMaterial` 是用透明度与环境反射
装出来的：适合做杯壁、窗、镜片这类**薄**的东西，不要指望它像照片一样透。
装水的杯子里不要塞太多层透明体，前后排序会乱。

## 光

主光已经由舞台给好：后上方、暖色、带阴影。**不要再加第二盏主光**。只有
画面里真有一件发光物（屏幕、灯、火）时，才在它自己的位置补一盏小范围的
`pointLight`，让周围的材质吃到它的颜色。

金属和玻璃靠环境反射才「活」，舞台已经挂好了环境贴图；它们的 `metalness` /
`roughness` 要在合理范围内，不要全都做成镜面。暗部要能看出层，不要糊成
一个黑块。

## 屏幕层

`Mark` 画手绘标注（圈、下划线、箭头、括号），线条沿路径长出来；`LineDraw`
画一条随帧生长的 SVG 路径。两者都贴在屏幕上、不跟相机走，一镜最多用一两处，
只用来指「看这里」，不要当装饰铺满。

它们**直接写在 `StudioStage` 的 children 里就行**，舞台会把它们挪到画布上方
的屏幕层。位置按画面比例给（0~1）：`x` / `y` 是左上角，`w` / `h` 是宽高；
只给 `size` 时按正方形。`Mark` 的线条按 `at`（起始帧）+ `dur`（画完用多少帧）
长出来；已经自己算好进度时直接给 `progress`（0~1）。

你自己写的组件如果返回 `<svg>` 或 `<div>`，放进 `StudioStage` 时要用 `<Screen>`
包一层，否则它会被当成三维对象，整镜渲染直接报错：

```tsx
<StudioStage …>
  <Screen>
    <MySvgLayer />
  </Screen>
</StudioStage>
```

## 颜色

底色是近黑（#07080a），台面是深木或深石。暖琥珀（#ffc994）只留给光源与
受光面，冷蓝（#5f92ff）只留在边光和远处。强调色（#ffcf6b）只给需要读的
那一处。整片底色不变，变的只是这一镜的实物与它的光色。

## 连续性与禁区

整片同一张台面、同一束光、同一条底部字幕。不要放照片、贴图、外部素材；
不要一镜写实、下一镜变成平涂图标；不要给某一镜加第二个人、第二块主光或
第二条字幕。

## 与流程技能的关系

这套视觉只回答「画面长什么样」。分镜、逐镜写码、闸、渲染仍由流程技能负责；
风格包不替你做分镜，也不改变出片流程。每一镜画什么按这一镜的 brief 来，
舞台、材质、光位、景深与调色按这里的约定守。
