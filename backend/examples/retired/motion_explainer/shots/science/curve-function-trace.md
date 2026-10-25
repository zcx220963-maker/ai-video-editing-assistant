---
name: curve-function-trace
一句话: 函数曲线逐笔绘制——坐标轴与包络线先入，主曲线用 clipPath 从左到右一笔笔画出，关键点等曲线"画到"它才弹出标注
适用: 讲"这个函数长什么样"的段落：阻尼振荡、抛物线、指数衰减、波形演化（数学/物理/数据叙事）
时长: 198f（6.6s）；轴 18f + 包络 22f + 绘制 120f + 标注滞后 8f + 收尾 30f
能量: 中高（一笔笔长出来的期待感）
---

## 机制

不是 opacity 淡入一张成品图，而是让观众看见"曲线一笔一笔长出来"。

1. **坐标轴与网格先淡入**（18f）：给"这是坐标系"一个锚定，x/y 刻度用 tabular-nums；
2. **包络线（虚线）先画入**（22f）："振幅在衰减"这条信息在主线之前先铺好；
3. **主曲线逐笔绘制**（120f）：用 SVG clipPath 矩形从左到右覆盖，每一帧推进一点，曲线像被一支无形的笔画出来；
4. **关键点逐个弹出**（滞后 8f）：峰 / 谷 / 零点 / 包络交点在曲线"画到"该点时弹出标注——不是提前排好的，是"画到这里才揭示"；
5. **函数表达式逐字打出**（与绘制同步）：底部 `y = e⁻⁰·³ˣ · sin(4πx)` 随帧逐字显现。

## 参数表

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `duration` | number | 198 | 总帧数（6.6s@30fps） |
| `function` | string | `"damped-sine"` | 函数类型：damped-sine / parabola / exponential-decay |
| `x_range` | [number, number] | [0, 4] | x 轴范围 |
| `y_range` | [number, number] | [-1.1, 1.1] | y 轴范围（自动钳位） |
| `key_points` | KeyPoint[] | 内置 | 关键点列表，每项含 x/label/sub/type |
| `show_envelope` | boolean | true | 是否画包络线 |
| `axis_in` | number | 18 | 轴淡入帧数 |
| `draw_speed` | number | 120 | 绘制段帧数 |
| `label_lag` | number | 8 | 标注滞后帧数 |
| `hold` | number | 30 | 收尾静止帧数 |

## 选型

- **阻尼振荡**（默认）：`y = e^(-0.3x) * sin(4πx)`，物理意义明确（弹簧、声波、电路），视觉上有"越振越弱"的叙事感；
- **抛物线**：`y = x²`，换选题时改函数定义即可，机制不动；
- **指数衰减**：`y = e^(-x)`，适合讲半衰期、RC 电路。

换选题 = 改 `fn()` + `envelopePos/Neg()` + `KEY_POINTS` 三张表，其余不动。

## 已知坑

1. **clipPath 矩形宽度必须略超曲线**：`clipW = drawP * (GW + 20)` 里的 `+20` 是为了避免最后一像素被裁掉——SVG clipPath 的边界是硬裁；
2. **采样点数要够**：`N_POINTS = 800` 对 1920px 宽的画布是底线，低于 400 高频段会呈锯齿；
3. **关键点的 x 必须在 `X_RANGE` 内**：超出范围的点永远不会被画到，标注也永远不会弹出——判据在 `kpFrame = DRAW_START + (kp.x / X_MAX) * (DRAW_END - DRAW_START)`；
4. **包络线的 dasharray 用预估值**：`2000` 是经验值，如果函数定义改成 x 范围很大的曲线（如 [0, 20]），需要调大这个数，否则包络线画不完。

## 参考实现
demos/science/curve-function-trace/
（CurveFunctionTrace.tsx）
