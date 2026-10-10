# Remotion 代码渲染链设计（图形链整体迁移）

日期：2026-10-10　状态：**设计已批准，零实现**
决策人：用户（六轮选择全部取最大化项，并两次推翻我的保守建议）

---

## 0. 一句话结论

图形科普片这条链**整体换成 Remotion**：模型逐镜交 React/TSX 代码，由新增的 `creation-remotion` 容器逐帧渲染成镜粒度切片；旧声明式链（22 种卡型、七种内置图示、四族画笔、theme/overlay 两栏、字段级 pointer）**全部删除**；跨镜一致性从"共用模板的结构保证"改由**母版 Composition + 五道闸**事后拦回。口播链与母带层一行不动。

---

## 1. 触发点与决策链（把推翻过的东西留档，免得下次重新吵一遍）

### 1.1 触发

用户发来一段 6 分 21 秒的解读片（`微信视频2026-10-10_095655_736.mp4`，1920×1080，含音轨）。逐帧读下来它讲的是"Opus 5.5 做视频"的技术底座，结论页自己写了分工：

| 组件 | 负责 |
| --- | --- |
| **Remotion** | 总体时间线（make videos programmatically） |
| **SVG + Rough.js** | 2D 动画（含 `values`/`keyTimes` 关键帧、抖动重描、斜纹填充） |
| **Three.js** | 3D（立体几何四棱锥、波面、带材质光照的写实场景） |

即"模型写代码 + 真浏览器逐帧执行"。用户问能否借鉴以完成"完整实现"，并选定：**A+B+C+D 全做，且按视频整套换成 Remotion**。

### 1.2 我们已实测的硬前置（不是估计，是这轮量出来的）

| 事实 | 数值 / 结论 |
| --- | --- |
| `creation-app` 镜像里有 Node 吗 | **没有**（`node: not found`），但有 `/usr/bin/chromium`，版本 **Chromium 154.0.8037.92**（Debian 13） |
| 镜像体积 | 2.62 GB（`8c8071b69b2d`） |
| 中文字体 | 30 个 CJK 字面：Noto Sans/Serif/Mono CJK 全家族（SC/TC/HK/JP/KR） |
| 运行期能否直连 npm | **能**。容器内无 proxy 环境变量，`registry.npmjs.org/remotion` 与 `registry.npmmirror.com/remotion` 均 200 |
| 构建期能否直连 npm | 未知且有风险：已知 `compose build` 注入的 `127.0.0.1:3067` 代理对个别 .deb 稳定 502（见「重烤镜像的代理坑」） |
| 包可用性 | `remotion` / `@remotion/cli` / `@remotion/renderer` / `@remotion/three` / `@remotion/eslint-plugin` 均 **4.0.534**；`three` 0.186.1；`@react-three/fiber` 9.8.1；`roughjs` 4.6.6；`katex` 0.19.0 |
| 现有截帧通道的硬约束 | `--virtual-time-budget` 下 Chrome 一次只发得出 1~2 个 `requestAnimationFrame` 就饿死（`web_nodes.py` 与 `render.py` 开头都记着这条实测），所以旧链禁 SMIL、禁 rAF 自走，时刻靠 `?t=毫秒` 注入。**Remotion 走 CDP 虚拟时间策略，不受这条限制**——这正是它能跑 rAF 动画与 WebGL 的技术原因 |

### 1.3 被推翻的建议（保留理由，别当成没发生过）

1. 我先推荐**双轨并存**（新链默认、旧链留作确定性档）。用户问："为什么要保留旧的，全部移到新的不行吗"。
2. 我复核后**撤回确定性这条理由**：lint 禁随机与时钟 + lockfile 锁版本 + CI 复渲比哈希，已把确定性做到接近结构保证，为它留一整条旧链不成立。
3. 当时真正在保的只剩两件事：**跨镜一致性**（20 镜共用一套模板，天然像一部片；逐镜自由写代码后，字号/留白/字幕位置每镜重新决定，且这种漂移单镜抽帧看不出来）与**失败半径**（旧链失败模式被模板收敛成"过闸或带镜号退回"；代码链的失败模式是"不报错，就是难看"）。
4. 这两件**都能在链内补回**，不必靠保留旧链：补法即下面的 §4 母版 + §3 闸⑤。用户据此选定 **甲：全移 + 母版 + lint 强制包裹，旧链删**。

---

## 2. 架构

```
creation-app (Python, 镜像不变 2.62 GB)
   ├─ plan_motion → shots[i] = {code, text, highlight, source}
   ├─ 闸① lint / 闸② 帧预算 ──▶ creation-remotion  POST /lint
   ├─ 逐镜渲染 ────────────────────────────────────  POST /render-shot
   │     └─ 产物由该容器直接 publish 进 MinIO
   │        renders/{会话}/shots/{镜号}-{cache_key}.mp4
   ├─ 闸③ 帧账回读 / 闸④ 非空 / 闸⑤ 一致性表（Python 侧，读切片与回读表）
   └─ 母带层（一行不改）：拼接 → texture → BGM → 闪避 → 音画同步硬闸 → 入库 → 级联回收
```

### 2.1 `creation-remotion` 容器

- 与 `creation-editor` **完全同构**：独立镜像 `creation-remotion:latest`、compose 里 `profiles: ["app"]`、healthcheck、Python 侧只多一个 HTTP client。
- 基础：Debian + Node 20 + **`npm ci`（锁 lockfile，不是 `npm install`）**。
  lockfile 进 cache_key（§5），因为换依赖版本等于换像素。
- 预装依赖（= import 白名单的物理边界）：`react`、`remotion`、`@remotion/cli`、`@remotion/renderer`、`@remotion/three`、`@remotion/paths`、`@remotion/shapes`、`@remotion/sequence`、`three`、`@react-three/fiber`、`roughjs`、`katex`。
- 浏览器：`Config.setBrowserExecutable()` 指向镜像内系统 Chromium，**不让 Remotion 构建期再下一份 headless shell**（省体积 + 免构建期外网下载）。
- WebGL：无 GPU，走软件渲染。`chromiumOptions.gl` 的可用档（`swangle` / `angle` / `noop`）**P1 阶段实测确定，不预设**；实测数据进 §9.7 的性能表。
- 字体：只用镜像内已有的 Noto CJK 家族。外链字体在闸①里是禁项——离线渲染时它会**静默回落**成默认字族，用户在界面上选的字体和画面上出现的不是一回事（旧链 `THEME_FONTS` 早就为这个写过一条注释）。
- 故障域：Remotion OOM / 崩渲染只影响图形链；口播链、母带层、素材库、登录不受波及。

### 2.2 HTTP 契约（三个口）

| 口 | 入 | 出 |
| --- | --- | --- |
| `POST /lint` | `{code, fps, width, height}` | `{ok, errors:[{rule, line, msg}]}` |
| `POST /render-shot` | `{shot_id, code, input_props:{fps, duration_in_frames, text, highlight[], tokens, subtitle_mode, aspect}, cache_key}` | `{ok, object_key, frames, duration_sec, tokens_used, warnings[]}` |
| `GET /healthz` | — | `{ok, node, remotion, chromium, gl}` |

字节不过 Python 的手：切片由该容器直接 publish 到 MinIO 的 `renders/{会话}/shots/` 前缀，与现有切片同一条前缀约定，因此 `session_gc` 的级联回收**天然覆盖新链，无需改动**。

---

## 3. 五道闸

删掉旧链时，旧链上有四道东西是真在干活的，逐道给替身；不允许"只是没了"。

| 旧链那道 | 新链替身 |
| --- | --- |
| 卡型白名单 + visual 死键闸（写错键出空壳） | 闸① lint（含强制包裹母版） |
| 分镜空壳闸（visual 没内容不许出片） | 闸④ 画面非空（按像素判，比旧链更严） |
| 版式模板天然统一 | 闸⑤ 跨镜一致性（按母版回读表判） |
| 镜头长度归真实语音时长 | 闸② 帧预算（外部注入时长） |
| （旧链不需要） | 闸③ 渲完回读对账 |

**闸① lint（渲染前，秒级，带镜号退回）**
实现走 ESLint 扁平配置的 `no-restricted-syntax` + `no-restricted-globals` + `no-restricted-imports`，配 `@remotion/eslint-plugin`，**不需要自研规则**。禁项：

- `Math.random`（必须用 Remotion 的种子确定版 `random(seed)`）
- `Date.now` / `new Date()` / `performance.now`
- `setTimeout` / `setInterval` / `requestAnimationFrame` **用于驱动时长或动画进度**（时刻只能来自 `useCurrentFrame()`）
- `fetch` / `XMLHttpRequest` / `WebSocket` / `EventSource`
- `fs` / `child_process` / `os` / `net` / `path` 等 Node 能力
- `eval` / `new Function`
- `localStorage` / `sessionStorage`
- 外链资源：`url(http…)` / `@import` / `<link>` / `<script src>`
- `import` 白名单外的任何包
- **顶层必须 import 并包裹 `<FilmLayout>`**（§4），不包就不给渲

报错形态：`{镜号, 规则名, 行号, 一句话}` 列表，原样退回模型。

**闸② 帧预算（渲染前）**
`duration_in_frames = round(真实语音时长 × fps)`，由 Python 侧从 TTS 实测时长算好、经 `input_props` 注入。沿用旧链口径：**有旁白时模型写的秒数只当下限参考，镜头长度归真实语音**。上限 `MAX_FRAMES_TOTAL = 4000` 保留，超了要求用户降 fps 或缩片子，**不静默砍**。

**闸③ 渲完回读对账（渲染后、进母带前）**
`ffprobe` 读切片实际帧数与时长，与分镜账比对，差 > 1 帧 → 该镜退回。**不静默截帧、不静默补帧**——与音画同步硬闸（#60）同一个脾气。

**闸④ 画面非空（渲染后，按像素）**
基准色取母版本帧的背景（`bg` token 解析后的实际色值，不是猜的 #fff）；对切片的前/中/末三帧算"与该基准色差异超过一帧间噪声阈值的像素占比"，**低于 2% 即退回**（2% 是起步值，P1 用三幕夹具量出的实际占比分布定终值）。
这条比旧链的空壳闸更严：旧链只能查"字段有没有写"，这条查"画面上到底有没有东西"。旧链真机查出的静默缺陷 #102（`visual` 写了这张卡不认的键 → 出空画面且 `ok=True`）在新链由这道接管。

**闸⑤ 跨镜一致性（渲染后，按回读表）**
`<FilmLayout>` 把它**实际使用的** token（字号阶、字色、字幕带高度与位置、边距、安全区）写进渲染产物（`<meta name="dg-tokens">` 或等价物），`renderMedia` 的 `evaluate` 回调读回来，落进逐镜表。
**容差为 0**：这些值是母版的常量，不是每镜可调的近似量——同一 token 跨镜取值只要不完全相等就带镜号退回。之所以不给"差 1 像素算通过"这种容差：真出问题时那 1 像素正是我们要找的东西，容差只会把缺陷放过去。
**能力边界要写清**：闸⑤ 保证的是**数值一致**，管不了**审美一致**。

---

## 4. 母版 Composition（`<FilmLayout>`）

新链里唯一有排版权的东西。

- **独占**：字幕带、安全区、页边距、字阶、配色 token、画幅适配（竖/横/方）。
- **镜头代码只能通过插槽填内容**（主体区、装饰层），拿不到字幕绘制权，也拿不到排版数值。
- `text` / `highlight` / `subtitle_mode` / `caption_size` / `aspect` / `texture` 全部从 `input_props` 注入母版；镜头代码读不到就不可能各写各的。
- **字幕位置 20 镜不差一像素**，靠的是这个独占权，不是靠运气。
- 母版本身有版本号（内容哈希），进 cache_key（§5）：改一次母版 token 等于全片换像素，不进键就会命中旧母版渲出来的切片。

**起手模板（不是闸）**：把旧链最常吃的六种版式——`title` / `archive` / `compare` / `flow` / `timeline` / `chart`——翻成六份可直接 import 的最小 `.tsx`，连同设计 token 一起放进新容器项目，技能文档点名优先使用。

> **这条要如实记账**：示例是"可以抄"，不是"必须抄"。模型不抄也合法，那时一致性只剩闸⑤在事后拦；而闸⑤拦下来的返工是**重交一段代码**，不是旧链那种**改一个字段**。这是全移方案相对双轨唯一实打实的退步。

---

## 5. 分镜契约、缓存与账

### 5.1 新的 shot 结构（全部字段）

```
shots[i] = {
  code:      "<完整 .tsx，顶层包裹 <FilmLayout>>",   # 必填
  text:      "这一镜要说的一句话",                    # 进母版字幕带，不进镜头
  highlight: ["逐词点亮的词"],                        # 必须是 text 子串，≤4 个
  source:    {"标题": …, "链接": …},                  # 出处闸照旧：必须对得上本会话检索账
  min_duration_sec: 数值                              # 只当下限参考
}
```

- 竖屏 `text` ≤ 60 字、横屏 ≤ 110 字、`highlight` ≤ 4 个、首尾各留一镜（开场/收尾呼吸镜）——这四条纪律原样搬。
- **删除的字段**：`card`、`visual`、`theme`、`overlay`、`duration_sec`（有旁白时本来就不该写）。
- 数值只给数字、单位写进 label 这条纪律，随内置图示一起消失；对数刻度的需要挪进 `chart` 起手模板里实现。

### 5.2 cache_key

```
cache_key = sha256(json.dumps({
    "code": 代码字节,
    "knobs": 该镜旋钮（text/highlight/min_duration_sec）,
    "fps": fps, "aspect": 画幅,
    "master": 母版内容哈希,
    "deps":   lockfile 哈希,
}, sort_keys=True, default=str))[:16]
```

**母版哈希与 lockfile 哈希必须进键**，理由同昨天那个 `44` / `44.0` 键漂移 bug：不进键，就会命中一份"用旧母版 / 旧依赖"渲出来的切片，而改动账和逐镜比较全都看不出来。

### 5.3 重烧口径

- 改一镜代码 → 只有那一镜 cache_key 变 → 只重烧那一镜。
- 换整片质感档（`texture`）→ **0 镜重烧**：它挂母带、故意不进缓存键（旧链既有设计，照抄）。
- 改母版 token / 换依赖版本 → 全片重烧，这是**正确行为**，不是缺陷（像素确实变了）。
- 逐镜切片与代表帧是**加质感之前**的像素——旧链已有的这条边界在新链同样存在，界面上要写。
- 逐镜账、证据分级（machine / frame / listening / eyeball）、未做的标 UNVERIFIED，全部照旧。

---

## 6. 前端与编辑面

- **保留**：时间线、轨道、框选、对比帧、刷新重放。它们只认"镜"这个粒度，与哪条链出画面无关。
- **右侧停靠栏**：按镜形态给一种页签——〈代码〉〈文案〉〈出处〉。旧链那套字段表单（theme 七旋钮、overlay 图层、visual 结构体）随旧链一起删。
- **patch 口白名单**：代码镜只允许 `/shots/i/code`、`/shots/i/text`、`/shots/i/source` 三种指针；**`code` 的改动必须重过闸①才允许落盘**——否则改一行代码绕过 lint 等于闸①不存在。
- **整片开关**（`aspect` / `fps` / `narration` / `voice` / `rate` / `subtitle_mode` / `texture`）**只在 `plan_motion` 的 `param_options` 出现一次**，渲染节点继承。旧链这条纪律原样搬（否则计划卡上同一组选项弹两遍）。
- **逐镜账上要能看出这一镜是哪条链烧的**——迁移期与之后排查问题都需要这一列。

---

## 7. 删除清单 / 保留清单

### 7.1 删（旧声明式图形链）

| 目标 | 处置 |
| --- | --- |
| `motion/spec.py` 的 `CARD_KINDS`（22 种）、`STYLES`、`THEME_*`、`OVERLAY_*`、`_VISUAL_DOC`、空壳闸 | 删；文件重写为代码镜契约校验器 |
| `motion/templates.py`（虚拟时钟版式） | 删 |
| `motion/diagrams.py`（flow/compare/timeline/levels/chart/scatter/orbit 七种内置图示） | 删；`chart`/`timeline` 的排版思路进起手模板 |
| `motion/graphics.py`（pixel/burst/net/brush 四族画笔） | 删；**A（brush 斜纹/实心填充）随之一并不做**——新链里它是 `rough.js` 的一个 fill 参数 |
| `theme` / `overlay` 两栏及其归一化（含昨天刚加的 `round(float(...), 3)`） | 删；对应能力由母版 token + inputProps 承担 |
| `motion/hitmap.py` 的字段级探针回读 | 删到只剩镜粒度；新增母版 token 回读通道（闸⑤ 用） |
| `patch_motion_video` 的字段级 JSON pointer | 退成镜粒度三指针（§6） |
| 前端字段表单停靠栏 | 换成代码框页签 |

### 7.2 留（与"哪条链出画面"无关，一行不动）

母带层全套（拼接 / texture 五档 / BGM / 闪避 / 音画同步硬闸）、TTS 与词级时间戳、成片入库（`origin=render`）、`session_gc` 级联回收、`render_gate` 出片门、逐镜账与证据分级、长任务 submit+poll 与进度条、**口播链 `render_video` 整条**（它剪的是真素材，本来就不在图形链上）。

---

## 8. 分期（段段可验，不是一次跑完）

| 段 | 内容 | 出口判据 |
| --- | --- | --- |
| **P1** | `creation-remotion` 容器 + `<FilmLayout>` 母版 + 闸① lint | 能对一段真代码 lint 出红，能渲出一镜可播的 mp4；`gl` 档实测定下来 |
| **P2** | 闸②③④ + Python client + 新节点接进计划门与逐镜账 | 一条 run 从分镜跑到成片，五道闸各有红用例 |
| **P3** | 前端镜粒度编辑（代码框页签 + 三指针 patch + code 改动重过闸①） | 改一镜只重烧那一镜，界面能看能改 |
| **P4** | 三幕夹具 + 完整样片 + 性能实测 + 旧链删除与文档收口 | §9 全绿 |

旧链的删除放在 **P4**，不在 P1 就拆——迁移期间要有一条能出片的路可对照，且删除本身要靠 git 兜底而不是靠"反正删干净了"。

---

## 9. 验收线（全绿才叫做完；缺哪条列哪条，不用"完整"自称）

1. **三幕夹具**（对着视频逐幕复刻，抽帧看得到）：
   - rough.js：斜纹填充 + 两种眼形的关键帧切换（对应视频 `values`/`keyTimes` 那一幕）
   - three.js：四棱锥转镜头，顶点标号可读、遮挡关系正确（对应 2025 新高考 I 卷那一幕）
   - KaTeX：公式字形正确（不是豆腐块）+ 波面动画（对应 $y_i(t)=0.3\sin(1.5x_i-2t)+0.2\sin(1.1z_i-1.3t)$ 那一幕）
2. **一份完整中文样片**，题目仍用《为什么清醒的堕落最可怕》，含配音。
3. **五道闸各一个先证红再证绿的离线用例**：`Math.random` / 自定时长 / 帧数对不上 / 空画面 / 两镜字号不一。必须先把整条链跑红，再修绿。
4. **重烧对账真机**：改一镜代码 → 只重烧那一镜，其余 cache_key 一字不差；`film→tv` 换质感 → 0 镜重烧。
5. **闸⑤ 与闸④ 的证红记录**（含 #102 那类静默空画面在新链被拦下的证据）。
6. **旧链删除后的全量离线回归**（容器内带 `PYTHONPATH=/app` 跑才算数）。
7. **性能实测表**：swiftshader 无头 WebGL，逐镜报"帧数 / 总秒数 / 每帧毫秒"。3D 镜慢到不可接受就写进闸②（限 fps 或限时长），**不留给用户踩**。

证据分级沿用 machine / frame / listening / eyeball，未做的如实标 UNVERIFIED。

---

## 10. 残留边界（做完也不销账）

- **确定性**从"结构保证"降级为"约定 + 实测"：靠 lockfile 锁版本、lint 禁随机与时钟、CI 里同一 cache_key 复渲比前/中/末三帧 sha256。任何一道被绕过，同输入同像素就不成立。
- **字段级 pointer 永久消失**：编辑粒度退到镜。改一个颜色从"点一下表单"变成"改代码或跟模型说一句"。
- **审美一致性只有事后闸**（闸⑤ 管数值），没有事前模板；起手模板不是闸。
- **真 3D 是软件渲染的几何体**，不是视频里那种带材质光照的写实场景（f_030 那一档仍做不到）；且渲染耗时未实测，可能要限 fps。
- **镜粒度编辑的浏览器真机验证依赖用户登录态**，我方只能到"渲染产物 + DOM 几何断言"这一级。
- **Remotion 授权**：源码可用但公司规模超过一定人数需公司授权（具体阈值实现前核一次官网）。本项目是个人作品集性质，但这条要写在这里，不要到面试时被问住。
- **构建期 npm 可达性未验证**：运行期已实测直连 200，构建期那个注入代理会不会同样卡在 npm 拉包上，P1 才知道。

---

## 11. 与外部参考的对照（抄哪里、哪里必须改）

| 视频里的主张 | 我们照搬 | 我们必须改 |
| --- | --- | --- |
| Remotion 提供时间线 | 用它的逐帧 seek 与 `renderMedia` | 时长由外部按 TTS 实测注入，不让 composition 自定 |
| 模型自由写代码 | 保留自由（import 白名单内） | 加五道闸，旧链的四道各有替身 |
| SMIL `values`/`keyTimes` | —— | **仍禁**：那是墙钟驱动；新链里用 `useCurrentFrame()` 表达同样的关键帧轮播 |
| Rough.js 斜纹 | `roughjs` 预装 | 无 |
| Three.js 3D | `@remotion/three` + `@react-three/fiber` 预装 | 软件渲染，要实测限档 |
| KaTeX 公式 | `katex` 预装 | 字体不外链，公式渲染产物要过闸① 的外链检查 |
| 本机跑、开发者一次性排版 | —— | 我们跑容器、模型逐镜交码，一致性必须变成闸 |
