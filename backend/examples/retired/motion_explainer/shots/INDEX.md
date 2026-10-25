# shot 卡索引（136 张，按类别）

每行：`<类别>/<卡名>` [能量] 时长 — 一句话描述 | 适用 | demo 文件 relpath + 字节数

**选卡看这一份就够**：`card` 一栏照抄行首那个 `<类别>/<卡名>`；
`derived_from` 一栏照抄 demo 栏里的那个 relpath（`demos/` 前缀可有可无，一镜引多份就都写上）。
**demo 栏给的是盘上真有的文件名，照抄即可——别拿卡名去拼 `<卡名>.tsx`**：
136 张卡里有 44 张的文件名与卡名对不上，照卡名拼出来的名字会被取材轮当场打回。
不要再逐张打开卡全文——SKILL.md 写明「不要读超过 30 张卡」。

## camera（10 张）

- `camera/basic-3d-scene` [中] — impress.js 式空间演示：卡片以不同位置/旋转/缩放散布 3D 空间，相机取各步姿态之逆依次飞行对齐，末步拉到 OVERVIEW 总览 | 时长 约 7.5s（225f@30fps；四站，… | 适用 概念/路线图/三步法的空间化讲述；替代平面 slides 的"每一… | demo `camera/basic-3d-scene/Basic3DScene.tsx` 14,257 B
- `camera/crash-zoom-punch` [高] — 全景一拍急推到目标特写（6f），落位二选一——过冲回弹（弹性）或撞停震屏（重量） | 时长 约 0.5s 动作 + 前后 hold（动… | 适用 功能段"点名"镜头——把观众视线一拍按到目标卡/模块上；强调级用撞停 | demo `camera/crash-zoom-punch/CrashImpactReal.tsx camera/crash-zoom-punch/CrashZoomReal.tsx` 36,521 B
- `camera/cursor-flyover` [中] — 整页俯瞰后，相机依次飞到四个功能区 zoom-in 特写，SVG 光标先一步到位指点、点击留下涟漪并触发界面响应，最后拉回俯瞰 | 时长 约 8.0s（240f@30fps；俯瞰 … | 适用 单页产品的功能巡览：一镜带观众看完四个功能区，光标当"导游手指" | demo `camera/cursor-flyover/CursorFlyover.tsx` 21,682 B
- `camera/depth-layer-moves` [中] — 分层深度运镜——多层视差滑轨：3 层速度梯度横移出纵深，滑轨落定时主角浮起 | 时长 4–5.5s 持续（demo 165f） | 适用 平面截图要"有厚度"的段落：卡组 / 列表 / 整页的横向巡览 | demo `camera/depth-layer-moves/MultiplaneReal.tsx` 8,710 B
- `camera/graze-face-tour` [中高] — 大倾角贴面游走特写——镜头贴着 UI 表面低飞掠过（侧栏树/顶栏/列表当地形），页面文字初始悬浮在界面上空带同形软影，随镜头行进先后加速贴落回… | 时长 单段 4–5s；可多段接力延长 | 适用 功能区巡礼（把 UI 当地景飞掠）；配合暗场+霓虹缘光做产品"内部… | demo `camera/graze-face-tour/GrazeFaceTour.tsx` 27,932 B
- `camera/overhead-camera-moves` [A 中 / B…] — 俯拍揭示两式——tilt-reveal 俯仰抬正揭示、overhead-tabletop-drop 桌面卡阵横滑骤降扎入 | 时长 A 120f（4s）/ B 140f（4.… | 适用 用"俯仰角"讲故事的开场/转场：单页 establishing 用… | demo `camera/overhead-camera-moves/OverheadTabletopDrop.tsx camera/overhead-camera-moves/TiltReveal.tsx` 36,481 B
- `camera/space-camera-moves` [高] — 3D 空间化运镜两式——exploded-view 爆炸分解（构件沿 Z 炸开再合体）、drone-dive-landing 无人机俯冲降落 | 时长 A 170f（5.7s，炸开-悬停-合体-… | 适用 把平面页面当 3D 实体拍的高光段落；两式都是"大动作"，一支片合… | demo `camera/space-camera-moves/DroneDiveLanding.tsx camera/space-camera-moves/ExplodedView.tsx` 28,396 B
- `camera/steep-tilt-glide` [中高] — 固定镜头下直立页面以 60° 强透视侧立（右近左远），页面自身沿其 3D 横面方向滑移掠过镜头（物动镜不动），滑移带速度重影、文字组件悬空贴落… | 时长 4s（120f）单镜；页面越宽越可拉长 | 适用 长页面/多区块 UI 的炫技巡览（内容依次滑过固定机位）；暗场霓虹… | demo `camera/steep-tilt-glide/SteepTiltGlide.tsx` 20,240 B
- `camera/tension-camera-moves` [A 高 / B…] — 情绪运镜两式——bullet-time 冻结环绕、pull-back 拉远孤立，相机替观众"感受"而非"看" | 时长 单式 5–6s（150–170f）；全片合… | 适用 情绪节点（震撼/收束）的运镜语言；与 space-camera-m… | demo `camera/tension-camera-moves/BulletTimeFreezeOrbit.tsx camera/tension-camera-moves/PullBackIsolation.tsx` 22,795 B
- `camera/terminal-3d` [中] — 三个终端窗散布 3D 空间，相机窗间飞行、途中正弦拉远，每到一窗打字机敲命令、结果逐行滑出——命令执行的空间叙事流 | 时长 约 8.2s（246f@30fps；三站各… | 适用 开发者产品的 CLI/工作流演示：把"三步命令"拍成三站空间旅程 | demo `camera/terminal-3d/Terminal3D.tsx` 20,483 B

## data（5 张）

- `data/before-after-slider-scrub` [中] — 前后对比拉杆——"处理前/后"两版叠放，分割杆先猛甩后慢扫，杆过处新版"显影"揭出 | 时长 4–5s | 适用 AI 增强/优化/重构类功能的效果对比段落（"用前 vs 用后"一… | demo `data/before-after-slider-scrub/BeforeAfterSliderScrub.tsx` 6,814 B
- `data/chart-live-moves` [中高] — 活体图表三式——oscilloscope-stream 示波流线（曲线右端实时写入+突发尖峰）、unit-dot-swarm-regroup … | 时长 各 165–192f（5.5–6.4s） | 适用 数据叙事段落；分别讲"实时性"、"每个数字是一个人"、"增长装不下" | demo `data/chart-live-moves/AxisRescaleShockV2.tsx data/chart-live-moves/OscilloscopeStreamV2.tsx data/chart-live-moves/UnitDotSwarmRegroupV2.tsx` 46,666 B
- `data/gauge-readout-moves` [中高] — 仪表读数两式——needle-sweep-selftest 满弧扫针（点火自检指针甩满全弧再回落真值）与 tape-scroll-fixed-… | 时长 A 140f（4.7s）/ B 150f（… | 适用 dashboard 开场仪式/性能指标揭晓；A 多表盘开机感，B … | demo `data/gauge-readout-moves/NeedleSweepSelftest.tsx data/gauge-readout-moves/TapeScrollFixedPointer.tsx` 25,218 B
- `data/scroll-brake-moves` [高开中收] — 长卷急刹两式——changelog-scroll-brake 基本款（高速长卷指数减速精准停位+目标抬升）与 brake-reticle-lo… | 时长 A 4–5s / B 5s | 适用 changelog/发布史/长列表段落："一直在发货，今天这条最大… | demo `data/scroll-brake-moves/BrakeReticleLock.tsx data/scroll-brake-moves/ChangelogScrollBrake.tsx` 22,480 B
- `data/timeline-travel` [中高] — 时间轴横移——镜头沿水平刻度轴加速掠过版本刻度，每过一格卡片弹立短停，末刻度急停推近 | 时长 4–5s | 适用 changelog/里程碑/发展史段落（"我们一直在发货"的另一种… | demo `data/timeline-travel/TimelineTravel.tsx` 12,551 B

## effects（15 张）

- `effects/assemble-then-type-flyin` [中高] — 空的暗底网格上，无文字的组件骨架先从四面八方飞入贴合；随后各处文字逐字从 3D 空间旋转着飞来落位，先大标题后小标注，全部落位后页面成形 | 时长 约5.6s（168f@30fps） | 适用 页面/海报"自己长出来"的开场；排版类产品的能力展示；从骨架到成稿… | demo `effects/assemble-then-type-flyin/AssembleThenTypeFlyin.tsx` 17,720 B
- `effects/aurora-bloom-bg-flip` [由低到高] — 浅灰底从底部升起紫橙柔焦 blob，随后整个底色在约 0.36s 内压暗到近黑、blob 压成余晖；文案同步 blur-out 换句 blur… | 时长 约5.2s（156f@30fps） | 适用 叙事转折点（"多年以来…→一切都变了"）；品牌片从铺垫拉到重音的那… | demo `effects/aurora-bloom-bg-flip/AuroraBloomBgFlip.tsx` 8,059 B
- `effects/brand-frame-snap` [中] — 品牌色画框语法——一圈粗纯色画框先于内容长出包住全屏，录屏窗口落进框内；模式切换时整圈画框同帧硬翻色+窗内布局同帧换，一个 borderCol… | 时长 单次翻色 ~4.7s（140f）；画框本身… | 适用 双模式/双章节产品片的全片包装层（蓝=模式A、绿=模式B 颜色编码… | demo `effects/brand-frame-snap/BrandFrameSnap.tsx` 29,926 B
- `effects/fui-hud-moves` [中高] — FUI/HUD 准星咬合——reticle-lock-on 四个 L 形角标组成的取景框从画外飞入、超调回弹后咬合锁定目标 | 时长 2–3s（demo 90f） | 适用 任何"看这里"的目标点名（替代箭头圈红，画面不冻结） | demo `effects/fui-hud-moves/ReticleLockOn.tsx` 9,234 B
- `effects/glow-flyline-moves` [A 低] — 暗场光斑与飞线三式——glow-orb-ambient 光斑底噪、flyline-arc 飞线连接、orb-flyline-relay 同帧共… | 时长 A 170f（~5.7s）/ B 140f… | 适用 全片唯一暗场段落的氛围与数据叙事：铺底噪用 A、讲数据流向用 B、… | demo `effects/glow-flyline-moves/FlylineArc.tsx effects/glow-flyline-moves/GlowOrbAmbient.tsx effects/glow-flyline-moves/OrbFlylineRelay.tsx` 34,073 B
- `effects/icon-performance-moves` [蓄势引入] — 图标表演——attention-bounce 求关注弹跳（图标连跳递增+落地压扁+镜头被吸引） | 时长 156f（5.2s） | 适用 半屏级 icon 特写段落；新功能引出 | demo `effects/icon-performance-moves/AttentionBounce.tsx` 15,389 B
- `effects/impact-feedback` [高] — 命中反馈两式——hit-counter 连招计数（顿帧+伤害数字+combo 跳字）、anime-impact 动漫打击帧（负片+集中线+色散） | 时长 n/a（元素级技法，寄生在落位动作上；各式… | 适用 元素落位/撞击的"命中一瞬"——给砸入、撞停加游戏级手感；按强度阶… | demo `effects/impact-feedback/AnimeImpact.tsx effects/impact-feedback/HitCounter.tsx` 27,456 B
- `effects/light-play-moves` [A 中 / B…] — 光效两式——spotlight-sweep 聚光扫字、sheen 单点扫光 | 时长 A 170f ~5.7s / B 150f… | 适用 把光当另一种笔触（扫/擦）：暗场标题揭示（A）、主角卡加冕（B） | demo `effects/light-play-moves/SheenSweepRetry.tsx effects/light-play-moves/SpotlightSweepReveal.tsx` 26,470 B
- `effects/radial-ripple-phone-chips` [低] — 暖沙底四层同心圆错相呼吸如水波，中央手机 mockup 屏内 feed 自动缓滚，两侧白色 chip 先后 spring pop 入场并悬浮 | 时长 约5.6s（168f@30fps） | 适用 移动端产品的"这就是它"定格镜头；功能点分列两侧的介绍段；片头/片… | demo `effects/radial-ripple-phone-chips/RadialRipplePhoneChips.tsx` 20,294 B
- `effects/riso-print-hits` [高] — 套印错位冲击帧——riso-misregistration-hit 单发：标题撞停瞬间裂成双色 riso 印版，抖两下"啪"地套准 | 时长 4s（120f，单发） | 适用 标题/卡片的命中强调，纸墨审美版的"故障闪"；全片 1–2 次的单… | demo `effects/riso-print-hits/RisoMisregistrationHit.tsx` 11,888 B
- `effects/scan-bracket-sweep` [中低] — 文档弹到中央，四角落下 L 形取景括号，一条扫描光带拖着渐变尾迹在文档上往复扫 5 趟——文档全程静止，只有光在读它 | 时长 约5.0s（150f@30fps） | 适用 "正在解析/校验这份内容"的过程镜头；文档类产品的能力演示；上传→… | demo `effects/scan-bracket-sweep/ScanBracketSweep.tsx` 13,385 B
- `effects/scanline-annotate-focus` [中] — 一条扫描线自上而下匀速掠过已存在的页面，越过哪块区块的下缘，哪块就被四角取景框从外扩处弹簧收拢对准（一次过冲），随后贴出一枚逐字打出的机器标签… | 时长 约5s（150f@30fps） | 适用 "AI 正在读你的页面/品牌"的分析镜头；设计系统/品牌规范的拆解… | demo `effects/scanline-annotate-focus/ScanlineAnnotateFocus.tsx` 20,314 B
- `effects/scanline-assemble-flyin` [中高] — 页面开场是一张只有网格与虚线槽位的蓝图画板，一条亮扫描线自上而下掠过；扫到每个槽位，该处组件就从画外飞入过冲贴合，带方向性残影模糊与落位咬合角… | 时长 约5.2s（156f@30fps） | 适用 "页面自己生成"的开场；AI 建站/自动排版类产品的核心演示；从空… | demo `effects/scanline-assemble-flyin/ScanlineAssembleFlyin.tsx` 20,188 B
- `effects/slam-entrance-moves` [高] — 高能砸入三式——kanada-perspective-snap 金田透视急停、score-slam 比分砸落、impact-burst-kit… | 时长 单式动作段 6–22f + 冲击余波 ~1… | 适用 主角卡/KPI 卡的重拳入场；impact-feedback 管落… | demo `effects/slam-entrance-moves/ImpactBurstKit.tsx effects/slam-entrance-moves/KanadaPerspectiveSnap.tsx effects/slam-entrance-moves/ScoreSlam.tsx` 46,133 B
- `effects/spotlight-sweep-moves` [中低] — 暗场聚光显影三式——A 醒睡扫过（光到即亮光走即暗）、B 贴边泛光横摇（紫光贴 UI 边缘渗入+聚光匀速右移）、C 角落匀速显影（径向聚光从角… | 时长 单式 3.5–4.5s；A/B 可串联成巡… | 适用 暗色调品牌片里逐个介绍 UI 面板/功能区；黑场开场把界面"点亮"… | demo `effects/spotlight-sweep-moves/CornerSpotlightReveal.tsx effects/spotlight-sweep-moves/GlowWakeSleepPanel.tsx effects/spotlight-sweep-moves/SlideSpotlightPan.tsx` 24,271 B

## interaction（11 张）

- `interaction/autolayout-gap-dial` [中] — 间距拨盘驱动布局——一排链接块带框选描边+缝隙间距标注，徽章数字逐格跳动、块被参数实时推开再弹簧回弹归位；"参数驱动布局"的可视化 | 时长 ~5s（150f：框选入场 + 预备回收 … | 适用 设计工具/低代码产品的"改一个数、界面跟着动"卖点镜头；"用设计工… | demo `interaction/autolayout-gap-dial/AutolayoutGapDial.tsx` 16,583 B
- `interaction/canvas-materialize-moves` [中] — 内容"物化上画布"两式——panel-to-canvas 行倒卡（面板表格行沿弧线飞出、跨容器变形成画布卡片）与 diagram-cascad… | 时长 A ~5s（150f）/ B ~6s（18… | 适用 AI/协作工具"生成结果落到画布上"的叙事段落；A 式讲"已有内容… | demo `interaction/canvas-materialize-moves/DiagramCascadeBuild.tsx interaction/canvas-materialize-moves/PanelToCanvasMaterialize.tsx` 40,179 B
- `interaction/chip-grid-single-select-blackout` [中低] — 五个选项 chip 以 3+2 居中排布逐个淡入；选中帧先插一帧灰色按压块，紧接数帧内底色变墨黑、文字变白并做 1→1.04→1 极轻回弹，其… | 时长 约5.5s（165f@30fps） | 适用 单选/套餐/档位选择的交互演示；"选了它之后会怎样"的因果镜头；价… | demo `interaction/chip-grid-single-select-blackout/ChipGridSingleSelectBlackout.tsx` 12,804 B
- `interaction/collab-cursor-moves` [A 低中] — 协作光标当演员——cast-ensemble 五光标群演氛围层（错峰飞入+正弦漂移+打字 cameo+聚拢围观） | 时长 A ~5.2s（156f） | 适用 协作/多人主题的画布场景，给画面铺"团队在场"体温 | demo `interaction/collab-cursor-moves/CursorCastEnsemble.tsx` 18,094 B
- `interaction/glass-pill-dictation-typing` [低] — 一条定宽玻璃胶囊以约 1.25 倍略大弹出后缓落到位，内部自左淡到右浓铺一层强调色暖光；光标先行、随后匀速打出听写句，光（连同胶囊背后的环境光… | 时长 约3.3s（100f@30fps） | 适用 语音/AI 输入框的登场；"跟它说话"的交互提示镜头；高能段之间的… | demo `interaction/glass-pill-dictation-typing/GlassPillDictationTyping.tsx` 9,566 B
- `interaction/hashtag-to-pill-materialize` [中] — 话题词打字实体化——居中打出 "#word"（红实心光标恒亮），1 帧硬切变成宽大胶囊标签，hold 后缩小左移落到页面标签位，再 1 帧硬切… | 时长 120f（4s）——打字 ~40f + 硬… | 适用 标签/分类/关键词功能的演示段（笔记 app 打 tag、话题聚合… | demo `interaction/hashtag-to-pill-materialize/HashtagToPillMaterialize.tsx` 10,934 B
- `interaction/picker-carousel-feature-cycle` [中] — 移动端风竖向选择器——焦点药丸不动、内容穿过它，每项带明显 outQuint 减速吸附后完全静止，按到中心距离分层控制透明度/字号/灰度，落定… | 时长 约3.6s（108f@30fps） | 适用 逐个念出功能名/场景名的列表镜头；"选一个"的交互演示；移动端产品… | demo `interaction/picker-carousel-feature-cycle/PickerCarouselFeatureCycle.tsx` 9,386 B
- `interaction/segmented-thumb-hero` [中] — 分段控件 thumb 位移当主角特写——超大胶囊 segmented control 弹簧浮入，描边箭头光标画外滑入按下，白 thumb 8f… | 时长 ~3.5s（demo 110f：浮入 18… | 适用 "模式切换/二选一"功能的宣告镜头（demo 的 video-sh… | demo `interaction/segmented-thumb-hero/SegmentedThumbHero.tsx` 11,149 B
- `interaction/theme-switch-moves` [A 中 / B…] — 主题切换两式——theme-sweep 斜向扫场（边界扫过处就地换肤）与 palette-ripple 组合款（⌘K 面板收缩成点、涟漪从点荡… | 时长 A 140f（4.7s）/ B 156f（… | 适用 深色模式/主题功能的叙事段落；同一 UI "在你眼前变色"而非切到… | demo `interaction/theme-switch-moves/PaletteThemeRipple.tsx interaction/theme-switch-moves/ThemeSweepToggle.tsx` 55,255 B
- `interaction/type-and-filter` [中] — 真实 UI 上打字搜索、网格自己收敛成一张卡、点击穿透进详情页 | 时长 约 2.5s（118–190f） | 适用 功能演示的"操作叙事"段；搜索/筛选/进入详情的任何交互链路 | demo `interaction/type-and-filter/TypeAndFilter.tsx` 12,929 B
- `interaction/voice-waveform-live` [中] — 录音胶囊实时声纹——64 根细竖条随"说话"起伏，说话时中部高耸、停顿缩成点线，波形从右往左滚动；说→停→说→提交塌缩的完整表演 | 时长 172f（~5.7s：入场 14f + 说… | 适用 语音输入/AI 助手"正在听你说"的功能镜头；无 UI 内容可展示… | demo `interaction/voice-waveform-live/VoiceWaveformLive.tsx` 15,163 B

## opening（11 张）

- `opening/brand-ink-open` [低] — 墨线十字准星描画→字标逐字压印→打字机副标→满一秒静止再上浮消散 | 时长 约 2.8s（83f） | 适用 品牌开场；任何"先立名号再进产品"的片头 | demo `typography/brand-ink-open/BrandInkOpen.tsx` 6,262 B
- `opening/crane-rise-reveal` [中高] — 升降臂拉升揭示——开场怼在一行数据特写，相机沿 Y 轴减速升起后拉，行行涌入直到整面 dashboard 铺满全幅 | 时长 5.5s（165f：特写 hold 24f… | 适用 "从细节到全局"的开场定场；与 drone-dive-landin… | demo `opening/crane-rise-reveal/CraneRiseReveal.tsx` 19,276 B
- `opening/dataviz-landscape-open` [低开缓升] — 暗场支流线束地景开场——多条流线汇入主干、虚构 ID 标签浮在线上、相机重景深低速飞越 | 时长 5–8s（开场氛围段，一支片 ≤1 次；d… | 适用 品牌级抽象开场（"数据宇宙"隐喻），接亮场产品段或字标；与 glo… | demo `opening/dataviz-landscape-open/DatavizLandscapeOpen.tsx` 19,070 B
- `opening/fracture` [高] — 5×5 瓦片从 3D 碎片态按中心波纹逐圈聚合成整面海报，停一拍亮字，随后全部碎片背离中心加速旋转飞出画面 | 时长 约 5.2s（156f@30fps；聚合 … | 适用 开场第一镜"从混沌到成形"的品牌/海报揭示；倒放或只取后半可作硬转场 | demo `opening/fracture/Fracture.tsx` 9,418 B
- `opening/icon-field-colorize` [中] — 灰阶小图标点阵错峰浮现铺满全屏，停一拍后多道品牌色横带波纹极快向下扫翻全场——"功能全景先摆满，品牌一瞬间点亮"的开场/收束卡 | 时长 浮现 ~45f 错峰 + 静置 ~10f … | 适用 开场铺陈产品能力面（图标=功能宇宙）再一举打上品牌色；功能集合页、… | demo `opening/icon-field-colorize/IconFieldColorize.tsx` 12,582 B
- `opening/letterspace-materialize` [低] — 大字距字标全字符并行连续描画结晶——所有字母同帧起笔、笔画像手写一样连续生长、同帧齐收成词；氛围底景上的品牌字标显影 | 时长 静置 ~15f + 描画 ~50f + 终… | 适用 片尾/片头品牌字标登场（大字距细骨架字标；demo 为 video… | demo `opening/letterspace-materialize/LetterspaceMaterialize.tsx` 10,711 B
- `opening/magician-card-flourish` [高] — 午夜蓝暗场上蓝色星芒闪现 0.3s（X 形针状光束旋转 90°+中心辉光放射小光芒），卡片从闪光点弹射而出——极速自旋弧线飞向镜头、自旋随靠近… | 时长 156f（闪光 0.3s + 飞行 1.6… | 适用 单张卡片/海报/封面的魔术性登场（片头主视觉、产品卡揭晓）；纯黑暗… | demo `opening/magician-card-flourish/MagicianCardFlourish.tsx` 19,734 B
- `opening/orbit-ring-title-open` [中] — 石墨暗场上八张 16:9 暗色内容卡按 45° 均布在 720×380 椭圆上匀速公转（卡身永不倾斜，纵深只由 sin θ 给出 ±12% 缩… | 时长 150f（5s@30fps；马克块 f64… | 适用 开场第一镜 = 「这个产品有一整套东西」；素材库/模板库/功能矩阵… | demo `opening/orbit-ring-title-open/OrbitRingTitleOpen.tsx` 22,696 B
- `opening/spotlight-hero-card` [中] — 关灯的剧场里一束钨丝聚光（带体积光柱）扫过页面锁定一张卡，斜侧推进后卡片弹起悬浮、香槟色光束沿轮廓两圈、连同 3D 注记一起贴回原位 | 时长 172f（约 5.7s；锁定 f34 → … | 适用 "单一主角"式产品开场；把一个核心对象（卡片/条目/模块）立成全片… | demo `opening/spotlight-hero-card/SpotlightHeroCard.tsx` 20,383 B
- `opening/stroke-segment-build` [低起中收] — 断笔成字——标题拆成十几段互不相连的笔画乱序逐段点亮，前 70% 不可读，末段落位瞬间语义"啪"地成立 | 时长 165f（5.5s；揭晓 f105 后 h… | 适用 开场吊悬念的产品名/大数字揭晓；一支片 ≤1 次；与 type-a… | demo `opening/stroke-segment-build/StrokeSegmentBuild.tsx` 11,107 B
- `opening/text-as-mask` [中高] — 文字视频遮罩——瓷白舞台上切出超粗大标题字，字内部透出缓慢平移的暗色实时产品面板，结尾吸一口气后字形指数冲入放大 46 倍溢出、内部画面接管全屏 | 时长 165f（入场 18f + 字内漂移 78… | 适用 品牌词/口号与产品画面二合一的开场或章节卡；字是门、产品在门里 | demo `opening/text-as-mask/TextAsMask.tsx` 15,455 B

## outro（6 张）

- `outro/grain-dissolve` [中高] — 整行字爆裂成沸腾颗粒噪点并浮现斜纹选区框，噪点云急速凝聚成更大号发光短字标，位移衰减归零定格 | 时长 约 2.0s（60f@30fps；砂化 0… | 适用 收尾"XX. Now Live"式上线宣告；长句信息压缩成品牌短标… | demo `outro/grain-dissolve/GrainDissolve.tsx` 10,450 B
- `outro/logo-shrink-wordmark-lockup` [中] — 霓虹切口大环快速收束成中央实心小白 O、过冲刹车时换成 video-shotcraft 标志，图标左移让位，字标字母逐个滑入完成 lockup… | 时长 约 4.4s（132f@30fps；收束 … | 适用 片尾品牌定妆：从满屏图形能量收束到"图标+字标+标语"的标准 lo… | demo `outro/logo-shrink-wordmark-lockup/LogoShrinkWordmarkLockup.tsx` 10,417 B
- `outro/neon-triple-marquee` [中高] — 三行对向霓虹跑马灯 recap——三连词空心霓虹灯管巨字上中下排满一面斜拍的砖墙，奇偶行反向匀速无限横滚，三行按 1/3 相位轮流点亮，结尾逐… | 时长 4–5s（demo 150f：18f 逐行… | 适用 片尾主题词复读机段落；三连词口号的"余韵"拍法；音乐段无旁白铺陈 | demo `outro/neon-triple-marquee/NeonTripleMarquee.tsx` 11,975 B
- `outro/outro-group-photo-launch` [峰值] — 全片元素从四面八方飞来围住字标合影，crane 落机位+舞台光+金尘做成发布会收场 | 时长 约 4.8s（145f） | 适用 outro/品牌收尾；多功能产品的"全家福"式终镜 | demo `outro/outro-group-photo-launch/OutroGroupPhotoLaunch.tsx` 16,912 B
- `outro/ui-strip-away-outro` [中] — 减法式收尾——点击 Publish 后整个编辑器 UI 从外围到中心层层错峰蒸发，黑场上只剩那颗按钮滑到屏心放大，按钮再淡出交棒字标定版 | 时长 ~4.8s（144f：光标就位 34f →… | 适用 "发布/完成"语义的 outro；想讲"一键之后一切复杂性消失"的… | demo `outro/ui-strip-away-outro/UiStripAwayOutro.tsx` 18,455 B
- `outro/ui-to-brand-morph` [中高] — UI 变品牌两式——icon-flip-bloom 图标 Y 轴翻扁成竖线、从竖线撑开成品牌标志（琥珀斜切随后划入）+ wordmark 逐字… | 时长 A ~4.3s（130f）/ B ~5.6… | 适用 品牌收尾/outro 前最后一拍；"你每天用的那个 UI 就是这个… | demo `outro/ui-to-brand-morph/IconFlipBloomLogo.tsx outro/ui-to-brand-morph/InputMorphsIntoLogo.tsx` 27,621 B

## rhythm（8 张）

- `rhythm/beat-cut-moves` [高] — 硬切当节拍乐器——连闪定格：三次白闪各硬切同一画面的一个裁切（全景→卡片→特写），快门余韵沉降后停在特写收束 | 时长 全程 140f ≈4.7s（活素材 30f… | 适用 高光段落把"切"本身打成鼓点；颁奖连拍式的仪式感、关键信息的加冕 | demo `rhythm/beat-cut-moves/PaparazziFlash.tsx` 16,441 B
- `rhythm/beat-step-list-theme-cycle` [高] — 三通道节拍器——深色场形容词列表逐拍上移一行，视口中央固定胶囊"接住"下一个词并换色，整场底色同拍跟换；行、色、场三通道锁死同一拍点 | 时长 铺垫 30f + 每拍 18f × 拍数 … | 适用 "同一产品多种气质/多主题展示"段落（modern/playful… | demo `rhythm/beat-step-list-theme-cycle/BeatStepListThemeCycle.tsx` 16,474 B
- `rhythm/panel-grid-moves` [中高] — 分格节奏——comic-panel-split 漫画斜格劈出同一主体的三个机位，咔咔咔三格定格后末格斜边吃屏成海报 | 时长 156f ≈5.2s（全屏侧拍 20f +… | 适用 把"格子"当节奏器：同主体多机位定格并列，逐格踩拍劈出；吃拍点 | demo `rhythm/panel-grid-moves/ComicPanelSplit.tsx` 24,301 B
- `rhythm/quad-split-parallel-scenes` [高] — 画面硬切 2×2 四宫格，四个象限并行跑各自的微场景（打字、急推、逐词、交互链），关键节拍错开 3–6 帧制造信息轰炸 | 时长 90f（3s@30fps：0–62f 并行… | 适用 节奏段"功能很多、同时发生"的蒙太奇拍；预告片中段的密度峰值 | demo `rhythm/quad-split-parallel-scenes/QuadSplitParallelScenes.tsx` 15,463 B
- `rhythm/rhythm-interrupt-moves` [A 中] — 打断节奏——jump-cut-punch-in 三级跳切推近：同构图三次无补间硬跳放大，逐级逼近目标 | 时长 A 135f ≈4.5s | 适用 用"打断连续性"本身当节奏器：顿挫推近（A）；与 beat-cut… | demo `rhythm/rhythm-interrupt-moves/JumpCutPunchIn.tsx` 4,994 B
- `rhythm/sakuga-timing-shift` [中高] — 一拍三转一拍一——元素先以每 3 帧一步的手翻书顿挫移动，高潮瞬间切成逐帧丝滑冲刺，帧率量化的突变本身就是看点 | 时长 150f（5s） | 适用 单元素的强调性位移（卡片入场、指标冲线）；需要"手工感→高潮爆发"… | demo `rhythm/sakuga-timing-shift/SakugaTimingShift.tsx` 15,641 B
- `rhythm/speed-ramp-freeze` [中高] — 帧号非线性 remap 的节奏手法——定格标注（流动→定格圈注→解冻） | 时长 定格标注全程 158f（≈5.3s，定格段… | 适用 卡片流/长横移中把一个重点"停下给人看"；教学解说、评审语境 | demo `rhythm/speed-ramp-freeze/FreezeAnnotateReal.tsx` 15,887 B
- `rhythm/trailer-grammar-moves` [A 高] — 预告片语法一式——trailer-bumper 前置速剪钩子：三镜 9f 速剪 + 黑场静默一拍，再开场标题 | 时长 A ~4.7s（140f） | 适用 预告片的开场钩子（cold open）：正片前先塞三个最抓眼的镜头… | demo `rhythm/trailer-grammar-moves/TrailerBumper.tsx` 6,332 B

## science（9 张）

- `science/compare-duel` [中高] — 对比对擂——三条对比轨逐维亮起，左侧读数先 pop、右侧读数沿对位线滑入咬合，优势标记从胜出一侧弹出，三轨之后总评轨归位报结论 | 时长 208f（约 7s）；三轨各 46f（错峰… | 适用 讲"这两样东西有什么区别"：有性/无性生殖、内燃机/电动机、近视/… | demo `science/compare-duel/CompareDuel.tsx` 10,966 B
- `science/cross-section-reveal` [中高] — 剖面揭示——一片剖切面自左向右扫过球体，扫到哪层显影哪层（地壳→地幔→外核→内核），标签带引线在该层被扫过 55% 时画入，收尾 30f 真静止 | 时长 150f（5s）；扫过全程 108f + … | 适用 讲"一个东西里面是什么"：地球内部、细胞结构、引擎剖面、地层、脑区… | demo `science/cross-section-reveal/CrossSectionReveal.tsx` 11,360 B
- `science/curve-function-trace` [中高] — 函数曲线逐笔绘制——坐标轴与包络线先入，主曲线用 clipPath 从左到右一笔笔画出，关键点等曲线"画到"它才弹出标注 | 时长 198f（6.6s）；轴 18f + 包络… | 适用 讲"这个函数长什么样"的段落：阻尼振荡、抛物线、指数衰减、波形演化… | demo `science/curve-function-trace/CurveFunctionTrace.tsx` 11,398 B
- `science/exploded-assembly` [中高] — 拆解合体——整体先合体呈现，各构件沿半径方向撑开（轨道半径 ×1.45、核 ×1.18），每一件的引线标签在它到位时画入，电子始终绕核旋转，合… | 时长 180f（6s）；合体 24f + 炸开 … | 适用 讲"一个东西由哪些部分组成、各部分在哪"：原子结构、细胞器、引擎拆… | demo `science/exploded-assembly/ExplodedAssembly.tsx` 10,140 B
- `science/growth-morph` [中高] — 生长变形——形态按阶段逐个构件长出（stagger 4f），阶段切换交叉淡入 + 微移，底部生长轴四节点连线并显示真实天数，末阶段成虫翅膀展开… | 时长 224f（约 7.5s）；每阶段 44f（… | 适用 讲"这个东西是怎么一步步长成的"：完全变态、生命周期、恒星演化、材… | demo `science/growth-morph/GrowthMorph.tsx` 13,622 B
- `science/map-terrain-build` [中] — 地图地形——等高线自谷底向山顶逐条立起（scale+上移+淡入，顺序=高程），一条河用 dasharray 把自己画出来并带跟随亮点，三个地名… | 时长 158f（5.3s）；等高线 7 条 ×1… | 适用 讲地理/地质/流域/气候：一座山怎么长出来、水往哪走、地形如何决定… | demo `science/map-terrain-build/MapTerrainBuild.tsx` 12,648 B
- `science/metaphor-bridge` [高] — 比喻桥——把"太阳"换成"一个篮球"再按真实相对比例量出地球：测量线延伸时距离读数真实上跳，走到的位置是一颗针尖大的点（图上放大约 9×，位置… | 时长 168f（5.6s）；篮球入场 16f +… | 适用 讲"这个东西到底有多大/多远/多少"：天体尺度、细胞与细菌、原子与… | demo `science/metaphor-bridge/MetaphorBridge.tsx` 13,619 B
- `science/process-diagram-flow` [中高] — 流程示意（循环型）——节点先全部落位，箭头按次序逐段用 dasharray 画出来（画的次序=流程次序），一个令牌沿已点亮路径行进并让所在段变… | 时长 176f（5.9s）；4 节点落位 18f… | 适用 讲"这个过程怎么走一圈"：水循环、碳循环、食物链、经济循环、呼吸/… | demo `science/process-diagram-flow/ProcessDiagramFlow.tsx` 14,967 B
- `science/timeline-evolution` [中高] — 演化时间轴——水平轴按真实年代比例定位，视口沿轴行进，节点滚到中心时卡片交替弹出并带引线，顶部年份读数随帧真实跳动，末节点急停推近 | 时长 276f（约 9.2s）；入场 20f +… | 适用 讲"这件事是怎么一步步变成这样的"：生命演化、恒星一生、元素发现史… | demo `science/timeline-evolution/TimelineEvolution.tsx` 11,090 B

## transition（15 张）

- `transition/bottom-push-stack-wipe` [中] — 底边上推换章——新场景连底色整屏从底边向上推入，把旧场景物理顶出画外，连推数章各配一种饱和底色，内容钉死在各自色底坐标系里随底色走 | 时长 demo 三连推 170f（~5.7s）；… | 适用 多章节产品片的换章骨架（每章一个卖点一种底色）；需要"翻页节奏感"… | demo `transition/bottom-push-stack-wipe/BottomPushStackWipe.tsx` 18,501 B
- `transition/card-flip-reveal` [中高] — 功能卡 3D 翻面揭示——卡片沿 Y 轴翻 180°，正面 UI 翻到侧棱最薄处掠过一道随角度移动的受光，背面揭出大号结论数字，逐张错峰扫过整排 | 时长 全程 150f（~5s，含 hold）；单… | 适用 "功能→成果"的成对叙事：一排功能卡逐张翻出各自的指标/结论；元素… | demo `transition/card-flip-reveal/CardFlipReveal.tsx` 15,230 B
- `transition/card-flock-tumble` [极高] — 三张 UI 页卡从侧棱薄边 3D 翻飞成阶梯站定（全程清晰、样条连续丝滑），站定后保持慢转不停，快速收束吸入中心，炸出单个湍流烟雾环扩散，巨字… | 时长 翻飞 ~1.5s + 慢转展示 ~0.3s… | 适用 能量高潮段（功能页群→品牌口号的爆点转场）；霓虹/极光暗场调性；"… | demo `transition/card-flock-tumble/CardFlockTumble.tsx` 26,055 B
- `transition/circle-match-iris` [中高] — 圆心匹配光圈切——光圈从页面上圆形元素的圆心炸开，圈内新页的圆形图表接在同一个圆上；匹配剪辑给光圈一个语义锚点 | 时长 5.5s / 165f（入场+定睛 40f… | 适用 前景有圆形元素（头像/图标/圆钮）、后景有圆形主体（donut 图… | demo `transition/circle-match-iris/CircleMatchIris.tsx` 18,573 B
- `transition/gradient-transition` [低] — 背景在 linear、radial、conic 三类 CSS 渐变间平滑过渡——角度、色标、中心、半径逐参数插值，段间交叉淡化换类型 | 时长 约 6.7s（200f@30fps；lin… | 适用 氛围底/章节底色的连续变奏；给静态排版段落提供"活着"的背景层 | demo `transition/gradient-transition/GradientTransition.tsx` 9,396 B
- `transition/line-carry-transition` [中] — 线条接力横移转场——场景 A 的进度条延伸出画，镜头跟线横移，线在移动中拐角围出场景 B 的卡框，全程无剪切 | 时长 ~6.3s / 190f（进度条走满 36… | 适用 两个有图形亲缘的场景之间（进度条→卡框、下划线→图表轴）；一支片子… | demo `transition/line-carry-transition/LineCarryTransition.tsx` 13,619 B
- `transition/mosaic-reframe` [中] — 12 张瓦片在规则网格、feature mosaic、对角瀑布串三种排版间连续变形，位置宽高各自插值、逐片微错峰，段间留 hold | 时长 约 7.1s（214f@30fps；浮现 … | 适用 "同一批内容多种看法"的陈列转场：作品集/模板库/相册产品的布局能… | demo `transition/mosaic-reframe/MosaicReframe.tsx` 20,087 B
- `transition/page-turn-transitions` [中高] — 整页体块转场——barn-door-split 对开门裂幕（旧页裂两半滑出、新页迎上） | 时长 单式 150f（前态建立 30–40f +… | 适用 章节级换页：新旧交替的两个大段落之间的"翻篇"仪式；与 shot-… | demo `transition/page-turn-transitions/BarnDoorSplit.tsx` 12,703 B
- `transition/print-texture-transitions` [中] — 印刷质感转场——ink-bleed-reveal 墨渗揭示（须状渗边洇开吃掉旧景） | 时长 4–4.5s（洇开段 55–80f + 静… | 适用 换景接缝的纸墨审美款；与交棒三式/穿越式并列的第三族——"介质显影… | demo `transition/print-texture-transitions/InkBleedReveal.tsx` 9,705 B
- `transition/shot-transitions` [n/a] — 镜头交棒三式——推进流白、whip-pan 甩镜（急刹款）、mask-wipe 穿窗（纵深款），按能量落差与接缝语义选型 | 时长 n/a（技法卡；各式占用帧数见参数表，从相… | 适用 任何两镜衔接处（技法卡，分镜阶段排完镜头后逐个接缝选一式） | demo `transition/shot-transitions/WhipBrakeReal.tsx transition/shot-transitions/PortalWipeV2.tsx` 27,793 B
- `transition/tear-streak-transitions` [高] — 撕裂转场——glitch-displace 噪声撕裂（16 横条错位抖动中硬切），数字故障语义的条带级撕裂 | 时长 前态 ≥40f + 撕裂 16–24f +… | 适用 高能换页：数字故障/断裂语义；页面完整性不破、能量拉满的条带级撕裂 | demo `transition/tear-streak-transitions/GlitchDisplace.tsx` 19,366 B
- `transition/transition-hidden-cut` [n/a] — 藏切点转场三式——前景遮挡隐形切、对撞开屏、暖色漏光，硬切藏进遮挡/撞击/光峰的 1-3 帧里，观众看不见剪刀 | 时长 n/a（技法卡；各式占用帧数见参数表，从相… | 适用 两镜衔接处需要"无痕换景"或"仪式感开屏"时（技法卡，与 shot… | demo `transition/transition-hidden-cut/InvisibleCut.tsx transition/transition-hidden-cut/LightLeakBurn.tsx transition/transition-hidden-cut/VersusSlam.tsx` 43,617 B
- `transition/transition-travel` [n/a] — 穿越式转场——字腔穿越：巨型章节标题的字形就是一扇窗，镜头钻进字形笔画完成换景 | 时长 n/a（技法卡；demo 150f，主推动… | 适用 前后两镜存在"元素/容器"级空间关系的接缝（技法卡，与 shot-… | demo `transition/transition-travel/LetterformZoom.tsx` 16,265 B
- `transition/white-flash-logo-simplify-cut` [中高] — 液态金属质感的彩色词标静置流光，蓄力后从字心炸出一记白闪过曝，曝光回落时同一位置已是扁平版字标——一次闪白完成质感降维 | 时长 约 4s（120f@30fps；演出 0–… | 适用 品牌段落收束（华丽演绎→干净定妆）；情绪从炫技切换到正式宣告的转场拍 | demo `transition/white-flash-logo-simplify-cut/WhiteFlashLogoSimplifyCut.tsx` 12,191 B
- `transition/wipe-transitions` [中] — 几何擦除转场——blinds-slice 百叶窗切条（12 根 3D 叶片错峰翻面成波，波扫过后整页由 A 换成 B） | 时长 前态 ≥20f + 擦除 ~56f + 收… | 适用 新旧页都不动、一条几何边界扫过完成交接的通用转场；不依赖构图里有合… | demo `transition/wipe-transitions/BlindsSlice.tsx` 10,251 B

## typography（25 张）

- `typography/blur-slide` [低] — 标题逐词入场，y 0.38em→0 + blur 18→0 + opacity 0→1 三通道走同一条 ease-out 同步收敛，词间隔约 … | 时长 约 4.0s（120f@30fps） | 适用 几乎所有标题/副标题成对出现的场合；产品页首屏文案、章节小标题；需… | demo `typography/blur-slide/BlurSlide.tsx` 8,946 B
- `typography/brace-expand` [中] — 一对紧贴的小花括号先在正中出现，随即带一次过冲向左右弹开并放大到标题级，文字 clip 宽度严格绑括号内缘、像被拉开幕布般从中间揭示，落定后字… | 时长 约 4s（120f@30fps：光标 0–… | 适用 开发者/技术产品的标题字卡；章节开场；需要"一个符号完成揭示"的极… | demo `typography/brace-expand/BraceExpand.tsx` 8,089 B
- `typography/caption-in-frame` [中低] — 字幕进画面两式——字压图 text-over-art（整句压在图上、自带柔底，图形跟着念到的字动）与字幕搬家 caption-pill-rel… | 时长 单式 5s（demo 150f）；持续型，… | 适用 讲的东西本身就是"图上这几个字"的段落（示意图标注、流程里的词、界… | demo `typography/caption-in-frame/CaptionPillRelocated.tsx typography/caption-in-frame/TextOverArt.tsx` 13,277 B
- `typography/countdown-arc-scatter` [高] — 纸面表盘 9 个等大数字沿大弧切向排布，整盘扫过 96° 后减速急停，"5" 停在弧顶随即平移落位成标题首字符，其余数字带 blur 原地散去… | 时长 约 3.3s（100f@30fps：扫动 … | 适用 倒计时/时长承诺类文案（"5 min to install"）；数… | demo `typography/countdown-arc-scatter/CountdownArcScatter.tsx` 11,178 B
- `typography/document-typewriter-reveal` [低中] — 悬浮在暗场里的整页真排版文档窗口在光标后自己"写"出来、侧栏跟进、历史条目逐个落入轨道，相机从斜侧标题特写一路后撤到全窗 | 时长 150f（约 5s，含 history-l… | 适用 文档/报告/笔记类功能镜头；信息密度最高的一拍 | demo `ui-entrance/document-typewriter-reveal/DocumentTypewriterReveal.tsx` 15,509 B
- `typography/flying-words` [高] — 22 个关键词按黄金角铺在扁椭圆截面上，沿 z 轴从远处飞到相机前擦身而过，透明度走 [0,1,0.5,0.2,0] 生命曲线，跑满 2 整圈… | 时长 约 6.0s（180f@30fps，2 个… | 适用 关键词云/能力清单的动态背景；片头片尾的"信息量"垫底层；需要纵深… | demo `typography/flying-words/FlyingWords.tsx` 10,299 B
- `typography/glitch-cycle` [中高] — 同一行等宽槽位循环轮播 4 条状态短语，每条头尾按概率关键帧 [1,0,0,0.1,0,0,1] 全乱码、中段偶发单字抖动，切换瞬间叠 RGB… | 时长 约 5.6s（168f@30fps；dem… | 适用 加载/构建/部署过程的状态播报；技术型片头的"系统自述"；需要机器… | demo `typography/glitch-cycle/GlitchCycle.tsx` 11,590 B
- `typography/gradient-word-sweep` [高] — 黑底标语里关键词被渐变彩光从左到右快速扫过"充能"——波前字符辉光最强向后衰减，填满后字符间勾连细紫红闪电、整词稳态泛光呼吸 | 时长 扫充 ~15–20f（要快）+ 稳态闪电呼… | 适用 标语里给单个动词/卖点词充能（Supercharged/faste… | demo `typography/gradient-word-sweep/GradientWordSweep.tsx` 12,637 B
- `typography/keyword-color-caption` [中低] — 关键词换色——字幕压在画面上、不留纸条，只有那几个要强调的词换成强调色，画面里对应的图形同步换色 | 时长 持续型，随句长拉长（demo 150f /… | 适用 默认观感（首选的排字方式）：任何「字要压在画面上、又要读得出来」的… | demo `typography/keyword-color-caption/KeywordColorCaption.tsx` 5,812 B
- `typography/lead-word-zoom-assemble` [中高] — 首词以 2.3 倍字号占据画面中央、hold 期间继续推近 6%，随后一条曲线同时完成「缩回终字号」与「整行左滑归位」，后续词各自从槽位右侧 … | 时长 约 2.8s（84f@30fps；组句 f… | 适用 品牌名/产品名的 Introducing 字卡；发布会式开场第二镜… | demo `typography/lead-word-zoom-assemble/LeadWordZoomAssemble.tsx` 10,715 B
- `typography/marker-underline-title` [低] — 大标题落定后，关键词下方马克笔下划线从左到右快速描画——变宽笔形、毛糙边缘、微上斜跟随斜体字势，贴着字底 | 时长 标题落定 +4~8f 后起笔，划线 8–1… | 适用 标题里强调单个关键词（new/free/AI…）；手写感/人味的品… | demo `typography/marker-underline-title/MarkerUnderlineTitle.tsx` 6,138 B
- `typography/outline-caption` [低] — 白字黑边——白字套一圈黑描边、底部压一条渐隐，压在照片/插画/任意乱底上都读得出来；强调词换成暖黄 | 时长 持续型，随句长拉长（demo 150f /… | 适用 实录、插画、照片满屏、口播密集、观众要跟读的段落；底太乱（没有干净… | demo `typography/outline-caption/OutlinedCaption.tsx` 6,025 B
- `typography/outline-word-fill` [高] — 空心词（细暖灰描边、500 字重）从 3.2 倍急缓收缩落位，虚线大圆随后从 2.8 倍收到字周围并缓慢自转，左右水平虚线从画框边缘内伸；描边… | 时长 约 3.0s（90f@30fps，短促单拍… | 适用 单词式利益点/口号的重锤一拍；节奏卡点上的"钉子"镜；深底品牌片的… | demo `typography/outline-word-fill/OutlineWordFill.tsx` 9,954 B
- `typography/paper-title-card` [低] — 一句话逐词压印上纸、一个词标强调色斜体、短划线收束 | 时长 1.7–1.8s（50–55f） | 适用 章节转场/价值主张字卡；重要功能出场前的引导卡；全片呼吸位 | demo `typography/paper-title-card/PaperTitleCard.tsx` 6,275 B
- `typography/pill-chip-slot-cycle-handled` [中] — 白底句式 "Your `chip` Handled" 里深色胶囊内词竖向滚轮轮换，胶囊宽度按预量文本宽插值平滑伸缩、两侧文字被自然挤开收拢，胶… | 时长 约 5.0s（150f@30fps：静置 … | 适用 "我们替你搞定 ___" 这类句式卖点；SaaS 功能列举的一句话… | demo `typography/pill-chip-slot-cycle-handled/PillChipSlotCycleHandled.tsx` 5,221 B
- `typography/pill-slot-cycle` [中] — 句中词槽轮换——固定句干钉死不动，句尾 pill 徽章每 ~0.7s 老虎机滚一格（旧的上飞加速淡出、新的从下带模糊滑入），连换 N 个功能词… | 时长 入场 12f + 每拍 21f × 词数 … | 适用 "功能列举"类文案的最优雅解法（比逐条列表快、比字符级滚动有语义）… | demo `typography/pill-slot-cycle/PillSlotCycle.tsx` 12,311 B
- `typography/text-column-converge` [中低] — 双词对峙合拢——左"NEW"右特性词钉死在等屏边距两侧硬切轮换、全程零收缩，换到最后一词才唯一一次 ease-in-out 滑到居中咬合成短语… | 时长 轮换 7–16f/词 × 8–9 词 + … | 适用 特性清单收束到产品名/口号的段落（"NEW × 一串特性 → NE… | demo `typography/text-column-converge/TextColumnConverge.tsx` 8,434 B
- `typography/title-demote-to-label` [低中] — 大标题降格为节标签两式——A 大标题居中显影站稳一拍后连续缩小 0.3x 平移到左上角落成小节标签、内容区在其下生长；B 同套路但登场时带文本… | 时长 A ~3.7s（112f）/ B ~4.5… | 适用 章节开场（标题先当主角再让位给内容）；教程/功能演示片的小节交接；… | demo `typography/title-demote-to-label/TitleDemoteToLabel.tsx` 17,145 B
- `typography/type-assembly-moves` [A 中 / B…] — 文字集结三式——split-text-stagger 逐字裂升、letterform-drift-assembly 漂移合拢、text-on-… | 时长 单式 4–5.5s（动作段 A ~56f … | 适用 大标题/标语的入场；与 type-entrance-moves、d… | demo `typography/type-assembly-moves/LetterformDriftAssembly.tsx typography/type-assembly-moves/SplitTextStagger.tsx typography/type-assembly-moves/TextOnPath.tsx` 24,768 B
- `typography/type-entrance-moves` [中高] — 标题文字入场一式——letter-drop-physics 字符坠落（重力砸落、弹跳、歪斜站定，最后一拍集体立正归位） | 时长 单式 5s（含 hold 与静止收尾；动作… | 适用 大标题/章节字卡的入场；与 type-assembly-moves… | demo `typography/type-entrance-moves/LetterDropPhysics.tsx` 8,584 B
- `typography/type-rhythm-sync` [A 高] — 文字随声同步两式——font-weight-pump 字重脉冲（笔画随鼓点变粗弹回）与 karaoke-fill-sync 卡拉OK填色（词随… | 时长 A FontWeightPump 165f… | 适用 标题/标语与音轨强绑定的段落；A 绑节拍（鼓点），B 绑语音（旁白… | demo `typography/type-rhythm-sync/FontWeightPump.tsx typography/type-rhythm-sync/KaraokeFillSync.tsx` 17,789 B
- `typography/typewriter-moves` [中低] — 打字机 error-retype——误删重打的"改口"三幕剧：打出平庸词 → 犹豫 → 退格删掉 → 更快打出卖点词 | 时长 ~5.3s（160f） | 适用 slogan/卖点字卡、否定式文案；文字自带时间性的入场 | demo `typography/typewriter-moves/TypewriterErrorRetype.tsx` 6,042 B
- `typography/vertical-word-roll-blur-cycle` [中] — 句尾词换成竖向滚轮，3 次换词各 0.55s（outQuint 七成 + outBack 三成，前快后极慢带微过冲），相邻行按距离上垂直 bl… | 时长 约 5.0s（150f@30fps：静置 … | 适用 "Crafted for ___" / "Built for __… | demo `typography/vertical-word-roll-blur-cycle/VerticalWordRollBlurCycle.tsx` 6,124 B
- `typography/word-relay-filmstrip` [中低] — 左列黑白相间等高页面卡步进滚动（焦点卡全亮、邻卡离焦压暗）、右侧衬线大词原位接力（名词恒定+动词轮换）——切词瞬间才滚动一格，词块垂直中心与当… | 时长 每词期 ~1.5–2s × 3–4 词；d… | 适用 "一个主体 × 多种能力"的枚举段（Shotcraft frame… | demo `typography/word-relay-filmstrip/WordRelayFilmstrip.tsx` 27,217 B
- `typography/word-relay-geometry` [中高] — 三个利益词各带一套专属几何接力——虚线大圆减速自转 + 彗星弧飞跑 → 三实线圆 trim 依次生长后收拢咬紧 → 镀铬金属 sheen 扫过… | 时长 约 6.0s（180f@30fps：三段各… | 适用 三点式利益陈述（更快/更好/更强）；品牌价值观段落；需要"一词一世… | demo `typography/word-relay-geometry/WordRelayGeometry.tsx` 15,801 B

## ui-entrance（21 张）

- `ui-entrance/avatar-bracket-carousel` [中] — "Your ___ agent" 填空排版，四角对焦框钉在句中不动，头像队列在框内垂直 spring 轮换三次，入框放大清晰、出框按距离缩小淡… | 时长 约 5.2s（156f@30fps） | 适用 "一个位置，多种角色"的能力枚举；团队/身份/预设/人格类产品的核… | demo `ui-entrance/avatar-bracket-carousel/AvatarBracketCarousel.tsx` 11,054 B
- `ui-entrance/bezier-source-converge-merge` [中] — 左侧四个来源节点各有一条细贝塞尔曲线连向右侧同一汇聚点，曲线先错峰由左向右 draw-on，节点沿自己的曲线滑向汇聚点并三段式加速缩小到消失，… | 时长 约 5.6s（168f@30fps） | 适用 "多源整合/统一接入/数据汇聚"的核心机制镜头；集成、聚合、单一入… | demo `ui-entrance/bezier-source-converge-merge/BezierSourceConvergeMerge.tsx` 17,396 B
- `ui-entrance/card-stack` [中高] — 一组模板卡（demo 9 张）从屏幕下方逐张 spring 弹入、越落越快叠成一摞，全员落位后整摞一次性展成扇面——每张按序号偏转 6.5°、… | 时长 约 5.0s（150f@30fps） | 适用 需要交代"我们有一组东西"的产品段落；卡片墙/模板库/方案列表的建… | demo `ui-entrance/card-stack/CardStack.tsx` 17,874 B
- `ui-entrance/carousel-3d` [中] — 8 张卡排成圆环整环自转一圈（匀速底 + 每 45° 一次停靠的分步），每卡只绕 Y 公转、自身 billboard 朝外，正反两层同向贴图配… | 时长 约 5.6s（168f@30fps） | 适用 作品集/模板库/集成清单的循环展示；需要无缝 loop 的背景拍或… | demo `ui-entrance/carousel-3d/Carousel3D.tsx` 16,293 B
- `ui-entrance/cloner-depth-echo` [中] — 克隆纵队——主卡瞬间"复印"出 7 个半透明分身沿斜向纵深排开成队，停一拍后全体加速吸回本体合一+弹跳 | 时长 138f（约 4.6s） | 适用 "多副本/多租户/规模感/批量处理"卖点；一镜讲完"一个=很多" | demo `ui-entrance/cloner-depth-echo/ClonerDepthEcho.tsx` 17,130 B
- `ui-entrance/deck-deal-flyin` [高] — 暗色石墨桌面上的实体牌堆特写环绕开局，灯从牌堆亮到旁边的页面板，一摞卡像发牌一样硬加速甩进网格，相机追着发牌升起后仰、落在整块满板的跑道式俯瞰… | 时长 150f（约 5s：0–36 牌堆特写 +… | 适用 展示"内容量大/源源不断汇入"的列表页与卡片墙；建立信息密度的第一… | demo `ui-entrance/deck-deal-flyin/DeckDealFlyin.tsx` 18,781 B
- `ui-entrance/doc-park-left-pill-deal` [低] — 文档不淡出而是向左滑出只露约 35% 宽并微缩到 0.92，右侧按旁白节奏慢速发牌三张白底描边药丸（outBack 弹入），每张落定后其下方字… | 时长 约 5.8s（174f@30fps） | 适用 旁白驱动的"分析结论逐条给出"段落；文档理解、推荐理由、审阅意见类… | demo `ui-entrance/doc-park-left-pill-deal/DocParkLeftPillDeal.tsx` 16,797 B
- `ui-entrance/draw-svg-trace` [中] — 描边生长圈注——一条带笔头的墨线沿元素轮廓跑一圈把它"画"出来，闭合瞬间闪黑交棒、内容淡入；同套路可给标题画下划线 | 时长 132f（预告 10f + 描边 40f … | 适用 单个卡片/图表/标题的被点名入场；元素级手法（整页级蓝图描线归 w… | demo `ui-entrance/draw-svg-trace/DrawSvgTrace.tsx` 14,345 B
- `ui-entrance/element-body-moves` [A 低中] — 元素身体感——contact-shadow-lift 接触阴影离面抬升：卡抬起时桌上的阴影同步放大变淡，纸片离桌被阴影证明 | 时长 A 150f 5s | 适用 给"位置在变"之外补"身体在变"：卡片点名给悬浮证据；配 2.5D… | demo `ui-entrance/element-body-moves/ContactShadowLift.tsx` 12,719 B
- `ui-entrance/integration-hub-map` [中高] — 旧页面一次性快翻 180°（侧棱瞬间亮闪）落成新中枢页，五个集成 app 图标同帧弹现、随即五条彩虹光管同帧齐连，光管内输送脉冲持续流动——"… | 时长 前摇 ~0.5s + 快翻 ~1.2s +… | 适用 集成/生态能力段（一个产品连一切）；版本翻新叙事（旧页翻成新页）；… | demo `ui-entrance/integration-hub-map/IntegrationHubMap.tsx` 31,497 B
- `ui-entrance/list-reveal` [低] — 垂直菜单 6 项按 10f 的间隔依次 scale 找位、软 back 轻微过冲落定，同时整个列表容器全程匀速上移——逐项入场与整体漂移是两层… | 时长 约 4s（120f@30fps） | 适用 导航/侧边栏/设置面板的入场；任何"界面自己长出来"的 UI 段落… | demo `ui-entrance/list-reveal/ListReveal.tsx` 9,059 B
- `ui-entrance/list-stack-press` [中] — 列表卡从画面底部逐张飞上摞起，每张落地压弹整摞、计数器同步跳一格 | 时长 约 3s（18–88f） | 适用 feed/雷达/收件箱类"每天有新东西"的镜头；强调持续积累的资产… | demo `ui-entrance/list-stack-press/ListStackPress.tsx` 12,417 B
- `ui-entrance/neon-frame-forerun` [中高] — 强透视直角霓虹框自左缘两头奔画先行成型，页面在框内由暗转亮，同时框内组件/文字从 3D 上空带同形软影错峰贴落、随页面点亮同步完成贴合，背景霓… | 时长 框奔画 ~0.7s + 点亮&贴落 ~2.… | 适用 暗场品牌片里给 UI 面板做"登场仪式"（框先到、内容后落）；功能… | demo `ui-entrance/neon-frame-forerun/NeonFrameForerun.tsx` 18,581 B
- `ui-entrance/neon-frame-orbit-drop` [中高] — 霓虹框先行描框后，镜头绕页面左→右弧线旋转，页面全部组件/文字**同帧**从空中往下贴合（同形软影同步收敛）——整体登场式的框内安放 | 时长 描框 ~0.5s + 旋转&同时贴落 ~2… | 适用 单页 UI 的一次性隆重登场（与巡礼/逐区亮相相对）；暗场霓虹调性… | demo `ui-entrance/neon-frame-orbit-drop/NeonFrameForerunOrbit.tsx` 20,564 B
- `ui-entrance/page-waterfall-wall` [中] — 页面瀑布墙——真实页面截图切成 3–4 列在 3D 后仰墙面上差速反向无限滚动，视差 + 镜头缓推做"内容多到流不完"的一览 | 时长 4–6s（无限循环体，时长由段落需要裁） | 适用 "多页面/多功能/多模板"体量感段落；montage 中段铺陈或 … | demo `ui-entrance/page-waterfall-wall/PageWaterfallWall.tsx ui-entrance/page-waterfall-wall/VerticalTicker.tsx` 9,168 B
- `ui-entrance/product-card-progressive-assemble` [中] — 详情卡像被逐字段抓取般自建——一枚"填充光标"领先跳格，图→标题→分类 pill 依次 pop→原价出现后被划线降级、强调色新价 spring… | 时长 约 5.7s（172f@30fps） | 适用 商品/条目详情页的能力展示；"结构化抽取""自动填充""数据自己长… | demo `ui-entrance/product-card-progressive-assemble/ProductCardProgressiveAssemble.tsx` 17,115 B
- `ui-entrance/radial-wave` [中高] — 17×9 圆点阵列按到波源的欧氏距离错峰点亮，每点 scale 过冲到 1.5 再落回常亮，第一道波扫完后第二道亮蓝脉冲从外圈反向收拢回中心 | 时长 约 3.8s（114f@30fps） | 适用 产品/品牌开场的"系统上电"一拍；也用作数据网格、节点地图、覆盖范… | demo `ui-entrance/radial-wave/RadialWave.tsx` 1,998 B
- `ui-entrance/research-card-stack-scroll` [中高] — 深色论文卡每 12 帧一张沿右下轴线飞入中心叠压，落位带 1 帧压缩，只有最上一张全清晰渲染标题+作者+摘要，下方卡按堆积深度递增模糊变暗只露… | 时长 约 4.8s（144f@30fps） | 适用 "读了大量资料/处理了海量文档"的量级交代；研究类、检索类、批处理… | demo `ui-entrance/research-card-stack-scroll/ResearchCardStackScroll.tsx` 6,055 B
- `ui-entrance/row-embed` [中] — 内容行像卡片一样从空中降下、rotateX 收平、嵌入瞬间底边亮一道强调色的缝 | 时长 约 2s（12–68f） | 适用 "结构化数据长进页面"的详情页/列表镜头；行级内容的批量入场 | demo `ui-entrance/row-embed/RowEmbed.tsx` 9,150 B
- `ui-entrance/value-stagger-gradient` [中高] — 16 根柱入场时 delay 是时间错峰，同时高度/色相/位移/模糊四个属性各自铺成从首到末的数值梯度；第二拍把错峰原点换成中心，脉冲幅度以中… | 时长 约 5.0s（150f@30fps） | 适用 技法演示与参数化能力的展示镜头；也可直接当数据/频谱/均衡器类界面… | demo `ui-entrance/value-stagger-gradient/ValueStaggerGradient.tsx` 7,981 B
- `ui-entrance/wall-reveal-moves` [中] — 整墙批量入场两式——bento 逐格点亮、蓝图描线成形，全部原位显形不位移，与 deck-deal-flyin 的飞入位移型互补成品类矩阵 | 时长 单式约 5s（A 150f / B 150… | 适用 功能墙/卡片墙/整页界面的整体亮相；内容已在原位、要"显形"而非"… | demo `ui-entrance/wall-reveal-moves/BentoLightUp.tsx ui-entrance/wall-reveal-moves/WireframeDrawOn.tsx` 19,906 B

总计 demo 字节：2,241,047
