---
name: style_silhouette_history
display: 剪影年轮
kind: style
caption_style: on_picture
description: 【STYLE SKILL】侧向行走的剪影穿过一层层时代背景，一块发光矩形贯穿全片；同一个姿势、同一个构图、同一处字幕，只换时代与道具。适合讲一段长时段的历史、技术或生活变迁。
style_manifest: {"version": 1, "template": "silhouette-history", "palette": {"sky": ["#0b1020", "#3a2a3f", "#e8a06a"], "ground": "#07070a", "ink": "#050507", "accent": "#ff7a2f", "paper": "#f2e6d0"}, "composition": {"figure_x": 0.32, "figure_h": 0.46, "ground_y": 0.82, "horizon": 0.62, "camera": "slow push-in 1.02x to 1.06x, no cuts"}, "repeat_composition": true, "recurring_pose": "walk left to right, pause beside the glow, lean 8 degrees, continue", "recurring_prop": "a glowing rectangle at 0.70W / 0.34H, same size and position every shot", "caption": {"look": "overlay", "position": "bottom-center", "width": 0.62}, "inscription": {"position": "top-right", "style": "small serif, 0.022H"}, "motion": {"camera": "1.02 to 1.06 slow push, no cuts", "grain": 0.12, "vignette": 0.35}, "avoid": ["CaptionFrame band", "PPT cards", "face details", "external images", "camera shake"], "narration_avoid": ["沉迷", "上瘾", "危害"]}
---

# 剪影年轮：这套视觉怎么用

这是一套**关于时间的视觉语法**，不是一组装饰。它要做的只有一件事：让观众发现
「照亮自己的东西换了很多次，而走过去的姿势一直没变」。

## 画面

全片只有一个主角：一个**侧向行走的人的剪影**。他从画面左侧走进来，走到那块光旁边
停一下，身体前倾一点，再继续往右走。每一镜都是同一个人、同一高度、同一条脚底线、
同一个朝向。观众不该在第二镜里问「这是另一个人吗」。

背景是**分层**的：远景是天与雾，中景是这一镜的时代轮廓（山洞、石柱、街屋、客厅、
街机厅、地铁车厢），近景是地面与一点点前景遮挡。层与层之间靠明度和空气透视分开，
不靠描边。剪影永远是画面里最暗的那一块，勾边光来自画面里那处光源。

那块**发光的矩形**贯穿全片：同一位置、同一大小，只换它是什么——篝火、窗、灯泡、
电视、显示器、手机。它是这一镜的「时代」，也是光位的唯一来源。

## 一致性

同一构图是**刻意的**：人物位置、光块位置、地平线、字幕位置每一镜都不动。
换的是时代背景、服装轮廓、道具内容与光色。不要为了「有变化」换机位、换主体位置，
也不要给某一镜加第二个人、第二块光、第二条字幕——那会立刻把这套语法打散。

## 字幕与铭文

字幕只有一条，**叠在画面上**，底部居中，宽度约画面宽的 0.62，字号小于主体，
不要字幕带、不要横幅、不要卡片。右上角另有一行小号衬线铭文，写「年代 · 地点」，
它是画面的一部分，不参与字幕，也不要和字幕抢位置。

## 留白与质感

全片低饱和，唯一的高饱和色留给那处光。画面要有细颗粒与轻暗角，别做干净的矢量图；
地面上要有主体的落地影。运镜只有缓慢推近，没有快切、没有抖动、没有转场特效——
镜与镜之间靠光色和背景换，不靠剪辑花活。

## 与流程技能的关系

这套视觉只回答「画面长什么样」。分镜、逐镜写码、闸、渲染仍由流程技能负责；
风格包不会替你做决定，也不会改变出片流程。模板已经提供了人物、光块、铭文与字幕，
每一镜只需要画这一镜的时代背景和那一下代价动作。
