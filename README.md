# 智能创作助手(AI Video Editing Assistant)

一句话驱动的视频创作 Agent:**说一句需求 → 自动出计划卡 → 确认后查资料/写文案/剪视频/渲染出片**。

剪辑不是聊天插件:它有 21 个真实工序节点、分钟级的渲染、可回溯的执行链,以及"说完成没完成要有证据"的
记账契约。本仓库围绕这三件事设计。

## 架构总览

```
                       浏览器(Vue 3 SPA,登录拿 JWT)
                             │  :8000 同源(静态页 + REST + WebSocket)
                             ▼
   ┌────────────────────────────────────────────┐   MCP(streamableHttp)   ┌──────────────────────────────┐
   │ :8000 主服务 = Agent 大脑                    │ ──────────────────────▶ │ :8001 剪辑服务 = 双手          │
   │  登录鉴权 · 对话主循环 · 计划门 · 技能 · 记忆 │      tools/list         │  24 节点 DAG + 契约            │
   │  checkpoint · 工具注册表 · WS 回投 · 配额    │      tools/call         │  ffmpeg · MoviePy · whisper    │
   │  判断模型(第二意见) · 存储层                  │ ◀────────────────────── │  渲染提交+轮询+看门狗+证据账    │
   └───────────┬───────────────┬────────────────┘      产物/进度回传        └───────────────┬──────────────┘
               ▼               ▼                                                           ▼
     PostgreSQL(全部元数据)  MinIO(媒体字节)  [Redis 可选:跨副本回投广播]      ffmpeg / 本地模型(进程内)
```

- **对外只有一个端口 :8000**——前端 build 后由主服务托管,没有独立前端端口;
- :8001 是后端内部的剪辑工序端口,只监听本机。渲染是分钟级 CPU 重活,独立进程保证它卡死时不影响对话;
- **必须先起 :8001 再起 :8000**:主服务在启动阶段建立 MCP 连接并拉取节点清单与 DAG 契约,连不上就是
  "无剪辑能力"(启动日志会明说),而不是运行到一半才失败;
- Redis 是**可选服务**:单副本不带 `--broadcast-redis-url` 是设计口径,不是欠账。
- **编辑口只有一条,是自研的那条**:成片卡与「时间线」面板上的「🎬 可视化编辑」开的都是同一个
  `MotionEditor` 选区改(§17)——它按成片自己带的命中表切形态:图形科普片露**画面上的框**
  (`motion-hitmap/1`,有空间命中层),素材 / 口播片露**轨道上的段**(`segment-hitmap/1`,
  `hit_mode="track"`,没有空间层);两者都只重烧受影响的那几块像素,第三方剪辑器已全部摘除,
  也没有内嵌的外部编辑会话。

## 目录结构

```
backend/                  后端(Python 3.12+)
  agent_framework/        Agent 框架:主循环 / 工具 / 上下文 / 记忆 / 技能 / checkpoint /
                          团队 / 计划门(plan/) / 判断模型 / WS 与 HTTP 层 / 存储层(storage/)
    plan/                 计划门(12 个模块:gate·vocab·validate·compile·prompt·reconcile·…)
    tools/                内置工具与 MCP 桥(mcp.py 把远程节点包成本地 Tool)
    storage/              PG 仓储 + MinIO 对象存储 + schema.sql(22 张表)
  storyline_server/       MCP 剪辑服务:25 个节点、渲染分发器、看门狗、能力配置
                          (motion/ 子包 = 零素材出片的契约·净化·图示·排版·出片五层)
  prompts/                提示词库(8 份 markdown,改文案不改代码)
  examples/               剪辑服务配置(config.toml / config.docker.toml)+ 6 个示例技能
                          (含 motion_explainer:零素材出片的工作流技能)
  run_server.py           主服务入口(:8000)
  run_storyline.py        剪辑服务入口(:8001)
frontend/                 前端(Vue 3 + Vite;构建产物 dist/ 由主服务托管)
deploy/Caddyfile          公网反代(:80 → app:8000,HTTPS 块预留)
docker-compose.yml        postgres + minio + redis(存储层);editor/app/caddy 走 profile "app"
Dockerfile                两阶段:node:20-alpine 出 dist → python:3.12-slim 跑后端
.env.example              环境变量模板(复制为 .env)
```

> **哪些东西不入库**:`backend/tests/`、`backend/.smoke/`、`backend/scripts/`、`backend/docs/`、
> `backend/pytest.ini`、`backend/start.bat` 都在 `.gitignore` 里(见"测试与验证")。

## 完整链路:从一句话到成片

这是读代码前先要握住的那条线。左边是**发生了什么**,右边是**在哪**。

| # | 环节 | 位置 |
|---|---|---|
| 1 | 浏览器登录拿 JWT(存 localStorage),开 `WS /ws/{conv_id}?token=…` | `server.py` `ws_endpoint`;凭证走查询参数是因为浏览器 WS 不能带自定义头 |
| 2 | 用户发一句需求 → `POST /chat`,user_id 由服务端从凭证反查 | `server.py` `/chat` |
| 3 | 投递 MQ:topic `chat`,分区键 `{user_id}:{conversation_id}` | `consumer.session_key` + `mq.InMemoryMessageQueue` |
| 4 | 消费者取出消息,按 `action.op` 分流。**没有 action 的默认入口是规划轮** | `consumer.SessionConsumer._handle` |
| 5 | 建 run:`checkpoint.begin` 落指针行 + 初始快照;执行身份(含 user_id)绑进 ContextVar,贯穿整条 await 链 | `agent.AgentOnceRun.run` + `identity.use_identity_or_inherit` |
| 6 | 拼上下文(系统提示 + 技能清单 + 记忆 + 跨轮历史 + 本轮专属段),user 行先落库 | `context.ContextBuilder.build` |
| 7 | 主循环 `_drive`:**每次叫 LLM 前**落一个一致点 → 调 `llm.complete_stream` | `agent.py`;一致点保证任何一次中断都能从最近的确定状态续/分叉 |
| 8 | 逐字增量 → `OutboundStreamHook` 发 `delta` 帧 → OutBound 投递到该会话的 WS | `hooks.py` + `connection_manager.py` |
| 9 | 模型要调工具 → 三道门按序过:审批门 / 渲染前确认门 / 正常执行 | `agent._tool_round` |
| 10 | 执行器按**读写集**把一批调用切成"组内并发、组间按序";`ToolRegistry.execute` 做类型校验,失败一律 `ToolError` | `tool.plan_batches` / `conflicts` |
| 11 | 剪辑节点调用经 `MCPTool` 出网到 :8001;`user_id`/`conversation_id`/`artifact_id` 由包装层**覆写**,模型自报的值不作数 | `tools/mcp.py` |
| 12 | 剪辑服务端按节点契约生成 `inputSchema`,拦截器递归补齐缺失上游(6 个"必须显式调用"的节点除外) | `storyline_server/server._register_one` + `orchestration` |
| 13 | 产物写 `artifacts` 表(键 + 对象字节在 MinIO);回执里的 `media_url` 是一次性预签直链,**不落共享库** | `storage/` + 节点 `ephemeral` |
| 14 | 渲染是长耗时:`render_video` 提交到后台执行位,内联等 20 秒;没跑完回 `status=queued/running`,由 `render_status` 续问 | `server._invoke_long` + `RenderDispatcher` |
| 15 | 成片由 `MediaCardHook` 发 `media` 帧;卡片带五级证据账本(哪些机器验过、哪些只能人眼人耳) | `hooks.MediaCardHook` + `core_nodes.evidence_ledger` |
| 16 | 收尾前:四条假称硬保证 + 判断模型第二意见;渲染未达终态**不许**结束本轮 | `agent._nudge_back` + `_follow_inflight_renders` |
| 17 | 终答落 `messages`(含媒体部件),指针行置 `completed`;刷新后按 `qa.parts` 重放成片卡 | `agent._deliver` + `media_replay` |
| 18 | **交付之后还有第二条回路**:成片卡带「✂ 选区改」→ 编辑器读这一版自带的命中表 → 零素材片在画面上框出一格、口播片在轨道上点住一段,拿到的都是**字段指针** → `POST /motion/patch` / `POST /timeline/patch` fork 新版本(**只重烧受影响的那几镜 / 那几窗**)→ 新版本又成一张卡 | `MotionEditor.vue` + `server.motion_patch` / `server.timeline_patch` + `nodes.PatchMotionVideoNode` / `nodes.PatchVideoNode`,详见"核心机制 17" |

**出片有两条终点通道**:素材在库里(或随消息给)走 `render_video`;用户只给一个**选题**、没有任何素材,
走 `plan_motion` → `render_motion_video`——画面是排版画出来的,详见"核心机制 15"。**一条片子只走一条通道**,
但"一个终点"不等于"一份产物":两条通道**都**能被选区改继续 fork 出版本链(§17),区别只在命中表
是画面上的框还是轨道上的段。

**回投帧的类型**(后端产出、前端消费):
`connected` · `delta` / `stream_end` · `tool_call` / `tool_result`(带 `call_id`,并行同名调用靠它配对)·
`plan`(计划卡)· `approval`(审批/弹窗)· `media`(成片与素材卡)· `answer` · `error` ·
`run_adopted`(崩溃接手)· `subagent_spawn` / `subagent_end`。渲染进度不是独立帧,而是挂在工具帧的
`render` 字段上 + 前端另轮 `/render_status`。

## 核心机制(重点)

### 1. 计划门:两次 run,不造"暂停"状态

剪辑诉求默认先出**计划卡**再动手。实现上刻意不做暂停态——它是**两次独立的 run**:

- **Run A(规划轮)**:注册表被**物理过滤**(`plan/vocab.py`)——所有写产物节点与剪辑契约节点直接不给它,
  只剩只读事实工具 + `submit_plan`。模型想在这一轮剪片子,调用会以 `UnknownToolError` 结束,
  且报错文案明说"本轮按设计不提供",它就不会反问用户;
- 候选计划落 `checkpoints.plan`,由服务端四重校验器把关节点是否存在、依赖是否闭合、参数是否在枚举内;
- **Run B(执行轮)**:按 `plan_run_id` 从库里取回**服务端自己校验过的那份候选**——浏览器只回传"选了哪版、
  改了哪个开关",不是可信输入的搬运;
- 枚举值是**承诺**(校验后可直接进节点参数),卡上"其他…"填的自由文本是**诉求**(进注入段,
  模型能力内翻译、做不到必须明说);
- 对账:`after_tool_call` 比对计划与实际调用,多出来的标"计划外步骤"并给偏离理由。

入口:`POST /plans/{id}/confirm`(→ `op=execute_plan`)、`POST /plans/{id}/revise`(→ 再出一版)。

### 2. 状态与时间旅行:指针行 + 一致点增量链

- `checkpoints` = 一条 run 一行的**指针行**(status: running / completed / failed / awaiting_approval /
  superseded);`checkpoint_entries` = **增量链**(`kind` 为 delta 或 full,带 `parent_seq`),重放即还原任意一步;
- `fork` 换的是**产物作用域**:克隆产物集 + 按契约下游作废要重算的节点 + 把父 run 标 `superseded`,
  上游产物**逐字节复用**不重算;入口 `CheckpointManager.fork` ← `POST /runs/{id}/fork`;
- 崩溃恢复:开机原子认领可接手的 run(`claim_recoverable`,多副本靠租约互斥),接手后从一致点续跑。

### 3. 剪辑 DAG:契约驱动,不靠提示词排队

- 每个节点声明 `required_nodes` / `reads` / `writes` / `ephemeral` / `require_explicit_call`,
  通过 `dag_contract` 一次性外发给主服务;
- 拦截器 `_ensure_deps` 递归补上游;但 **6 个节点要的是创意决策参数,服务端替不了**,标了
  `require_explicit_call`:转写修字、片段筛选、片段分组、脚本模板推荐、转场推荐、花字推荐。
  这份名单会写进工具说明,模型第一次就能按对顺序——这是省一轮撞墙的软约束,硬拒绝仍在拦截器;
- 并发分批按**状态键**判定:写集相交、或写撞读,才拆开;没声明读写集的工具独占一批。

### 4. 渲染:提交 + 轮询 + 尝试令牌 + 看门狗

- `RenderDispatcher.submit` **一定先落一条 `render_jobs` 行**(界面与看门狗都靠它);
- 每次尝试带 `attempt` 令牌,所有进度/成功/失败写入都带它——令牌被换掉(判死、重开)之后迟到的写自动作废,
  不会出现"死透的任务又回一条 done";
- 内联等待 `render_grace_sec=20s`,调用方可要 `wait_sec` 但被 `render_wait_max_sec=300s` 夹住;
  并发渲染 `render_max_concurrent=2`(ffmpeg 是 CPU 密集型);
- **停滞看门狗**:连续 `render_stall_sec=600s` 无进度即按失败收口(排队行先续期);崩溃遗留另有 30 分钟
  的 `reap_hanging`;
- **硬保证**:渲染未达终态,本轮**不许**收尾(`_follow_inflight_renders` 代查中间态,不占模型上下文)。

### 5. 音画同步:先自动校准,再硬闸打回

`SYNC_TOLERANCE_SEC = 0.25`。渲染前 `resync_original_audio_timeline` 按"声音正在播哪一秒"重排口播出镜段,
再跑 `av_sync_check` 出 (最大偏差, 违规清单):

- 超阈值 → **不静默修**:写一条 `failed` 任务行,把逐条原因 + "校准已自动改回 N 段" 回喂模型再改编排;
- 成片指纹(实际消费的时间线摘要:段数/mode/最大偏差/校准改动段数)随结果入库,事后能核"渲的到底是哪一版";
- `target_duration_sec` 在时间线**出口**裁事件本体——兜底渲染路径不看 `duration` 字段,只改字段等于没改。

### 6. 覆盖层 + 词锚点:主轨声音不动

`overlay_events: [{segments:[asr 段 id], path, fit, src_start/src_end}]`。
`resolve_overlay_anchors` 把段 id 换成**成片输出轴上的秒**(先看声音轨 `audio_events`,再落画面轨 `events`),
认不出 id 就报错并列出真实 id。主轨画面与口播声音都不动;覆盖层**永远静音**
(MoviePy 侧 `audio=False`,ffmpeg 兜底只接 `[k:v]` 并 `-an`)——自带音轨就会和口播抢同一个声床。

### 7. 转写修字闸:`correct_transcript`

ASR 之后、粗剪之前的可选节点。**只改字,不改时间与词 id**:

- 容差按字数比例:`max(3, 30%×原字数)`(字数口径 = 中文逐字 + 拉丁按词);时间被改、段落被删、
  text 被清空一律打回——修字不是重排;
- 产出**修字表** `corrections`(改前 / 改后 / 落在第几秒 / 改了几个字),另出 `unchanged_suspects`
  (机器判不出对错,但可疑的词照实列出);
- **不覆写 `asr` 产物**:原转写永远留在库里可查,下游优先读修字表、没有才回退原转写,于是字幕自动跟着改对。

### 8. 五级证据分级:没验过的必须挂问号

渲染产物(真渲与 dry-run 都是)除账本外带一本 `evidence`,每条主张标注它凭什么:

| 级别 | 含义 | 例 |
|---|---|---|
| `machine` | 机器算过 | 段数、秒数、锚点换算、同步偏差 |
| `byte` | 量过字节 | 对象取得到、真实时长够、文件大小 |
| `frame` | 抽帧看过 | 15%/50%/85% 三帧不黑屏、非冻结(指纹相同) |
| `listening` | 只有人耳 | 听感、口型自然度、音乐切点 |
| `eyeball` | 只有人眼 | 字幕排得对不对、内容对不对题 |

后两级**永远不标已验**。这条是给"模型看到 `status=done` 就声称严格音画同步"设的闸——界面自己会说
「N 项有证据 · M 项没验」。

### 9. dry-run:先看出片计划,不烧像素

`render_video` 传 `dry_run=true`(工具面与 `POST /render_direct` 都支持):真渲染会跑的每一道校验全跑一遍
——锚点换算、同步校准与闸门、对象键是否存在、覆盖层落点——回一份"将要渲成什么样"的账,外加
`not_checked` 明说这次**没检查什么**。不下载字节、不编码、不产出成片。

关键的一条在**路由层**:dry-run 必须绕开 `RenderDispatcher.submit`。那条路一定落一条 `queued` 任务行,
而 dry-run 的节点体刻意不开任务行也不推进度——那一行就永远停在 queued(界面显示"正在出片"、看门狗按
停滞判死),而它真正该回的计划账被进度视图盖掉。

### 10. HITL:三扇门,同一个收口

- **主动提问** `ask_user`(模型可调)· **渲染前确认门**(系统按策略拦,同一 run 只问一次且"问过"落盘)·
  **审批门**(工具自声明 `requires_approval` 或 `--approve-tools` 按名覆盖);
- 三者都汇到 `_pause`:`checkpoints.approval` 存 pending 调用 + 状态置 `awaiting_approval`,
  作答走 `POST /runs/{id}/approve` → **原地续跑**,不是重新问一遍;
- 服务端硬校验"答案属于当前这道题":陈旧答案必须被拒,否则会出现"用户选了 A、系统按上一道题的 B 跑"。
- **用户看到的"计划卡"是同一张统一提问卡**(`.qc-inline`),不是一种新弹窗:计划确认走 `origin=plan_confirm`,
  可能**分多页**(每步的参数一页),按钮文案随阶段变——非末页是「下一题」、`plan_confirm` 末页是「按此执行」、
  渲染前确认门是「确认」,旁边固定配「跳过这题 / 稍后再说」两颗退路。真机验收脚本按
  `/确认|提交|继续/` 找提交键时,只选中了推荐项、从没提交,卡片永远停在第 1/6 页——**驱动 UI 的自动化必须
  按"选中推荐项 → 点 `.qc-actions` 里那颗非 ghost 的实心键"两步真点**,且先 `scrollIntoView` 再量坐标
  (按钮压在视口折线以下时,真鼠标事件落不到它上)。

### 11. 收尾的四条硬保证 + 判断模型第二意见

词面判据(零成本、零延迟)在收尾前拦四类假话:未出卡不许声称已出卡 · 未调步骤不许声称已执行 ·
索要回答必须走弹窗 · 渲染未达终态不许收尾。每类各有 nudge 预算,用尽则在终答末尾**补事实**而不是继续打回。

配置了判断模型(Jev 类"系统一"模型)时,词面**没拦**的收尾答复再交给它裁决:

- 通道:TypeSafe `POST /v1/systemone`,请求 `{model, state, questions}`,问题用 **Noul 原语**
  (返回 0-1 概率,无独立 confidence),映射 `verdict = noul > 0.5`、`confidence = noul`;
- 输入是**服务端核到的事实 + 待裁决文本**,它只做"文本 vs 事实"的一致性判断,不参与生成;
- 三条通道任一可用即启用:env `JUDGE_*` 三项 / 当前用户在设置页配的三件套 / 有能出网的主模型密钥
  (没配 Jev 时用主 LLM 做第二意见);低置信(默认 `< 0.8`)**不行动**——宁可漏纠不误伤;
  任何故障退回词面判据,绝不阻塞交付;
- 成本:只在"该拦没拦"的形状出现时才调,健康轮次一次都不调。

### 12. 用户面不出现机器名:一张词表 + 一个出口

`ToolCatalog`(`catalog.py`)是**三源单汇**:剪辑节点 `display_name` + `Tool` 基类属性 + 技能 frontmatter。
替换器挂在所有出口(WS 帧序列化、HTTP 响应、工具文本),按名长倒序替换防前缀遮蔽;流式带挂起缓冲,
`split_sho`|`ts` 这种被切断的 token 出口仍是中文、无残片。参数标签(`arg_labels`)同源。
未声明中文名的条目**如实退回机器名**,不编。

### 13. 身份、密钥与多用户隔离

- **登录**:账号密码注册/登录,签发手写 HS256 JWT(TTL 7 天);密码用标准库
  **PBKDF2-HMAC-SHA256、30 万轮 + 随机盐**;兼容旧的匿名 32 字节随机 token(库里存 sha256);
  `REGISTER_OPEN=0` 可关自助注册;
- **凭证反查**:API 的 user_id 一律服务端从凭证解析,客户端传值无效。跨用户 → 读返回空、写 409、
  执行 404(不泄露存在性);
- **工具层**:执行身份经 ContextVar 绑整条 await 链,MCP 包装层覆写 `user_id`/`conversation_id`/
  `artifact_id`——模型伪造的身份到不了服务端;
- **对象层**:MinIO 键一律 `users/{user_id}/…`;媒体字节在 MinIO、转写与产物在 PG,
  本地目录只是可回源的缓存(删了不必重传/重转写);
- **模型密钥**:页面「设置」写 PG(`app_secrets`)→ 环境变量 → 回落位,**每次请求按当前身份回源**
  (5 秒级缓存)。两个进程都不必重启;`agent_framework/` 里任何文件都不含密钥值,日志与报错一律先过掩码,
  WS 访问日志的 `?token=` 在 lifespan 里被打码成 `token=***`;
- **检索后端**:`search_provider` / `search_api_key` / `search_base_url` 三项走**同一套按身份热读**
  (页面配置压过环境变量,见 §16)。之所以是三项而不是一把 key:自建 SearXNG 没有 key 只有地址,
  而 brave/tavily/serpapi 只有 key 没有地址——只存 key 的那套设计会把自建这条路走死;
- **配额**:`--quota-limit` / `QUOTA_LIMIT` 按身份记 `token_usage`,默认 0 = 不拦截但仍记量(刻意默认)。

### 14. 回投与多副本

OutBound 帧先给本进程登记的 WS 连接;`--broadcast-redis-url` 打开后走 Redis 频道 `outbound:broadcast`,
各实例只投自己挂着连接的会话。**没挂 WS 的会话帧也进有界影子缓冲**,刷新/换副本后才看得见在途进度;
`answer` 帧落定即清该 run 的缓冲,重放只补在途轮次。MQ 按会话哈希分区:同会话串行、不同会话并行。

### 15. 零素材图形科普片:分镜是校验闸,画面是纯排版

用户只给一个**选题**、没有任何视频素材时走这条通道(`plan_motion` → `render_motion_video`)。
判据只有一条:这条片子的画面是不是排版就能画出来。给素材请走剪辑 DAG,一条片子只出一个终点通道。

- **两个工具、两次决定**:`plan_motion` **不烧像素**——它校验、归一、把 spec 落进 Store,并回一份账
  (镜头数、总字数、按字数估的秒数区间、与目标时长的偏离提醒)。带病的 spec 不入库,所以永远不会
  「渲了二十分钟才发现高亮词写错了」;改文案只需重调本工具。`render_motion_video` 才是唯一烧像素的口子;
- **镜头时长不由模型给**:有旁白时时钟归 edge-tts 的**实测语音时长 + 词级时间轴**(话没说完画面不切走),
  模型写的 `duration_sec` 在入库时就降级成 `min_duration_sec` 下限参考。字幕逐词点亮用的就是这份词时间轴
  (`templates.align_words` 把 TTS 的分词贴回文案的字符区间);
- **一帧 = 时刻 t 的纯函数**:页面把 `?t=毫秒` 在解析期同步算成画面状态,**不靠 rAF**——实测 headless
  Chrome 在 `--virtual-time-budget` 下只发得出 1~2 个 rAF 回调就饿死,挂在 rAF 上的动画会让每镜都截成起始帧。
  同理 **SMIL(`<animate>`/`<set>`)一律拒**:它们按墙钟走,逐帧截图推不动;
- **三族版式 × 两套设计空间**(`motion/spec.py` 的 `CARD_KINDS` 共 18 个):
  档案家族 10 个(archive/dict_entry/stamp/book/print/theatre/webpage/silhouette/plain/title,
  写在 1080×1920 设计空间)、图示家族 7 个(flow/compare/timeline/levels/chart/scatter/orbit,
  写在 `0 0 1000 560` 的 viewBox 里)、`custom` 自画一张。**竖屏与方屏共用**竖排版式(方屏四周是同一张纸);
  **横屏是第二套版式**(1920×1080 + 中央 1760×658 画面带),`fit=box` 的档案卡**按自身像素落进带里**
  (早先按高度把整张竖构图缩 .35,实测一张 640×520 的封面卡在 1920 宽的纸上只剩 224px)、`fit=fill`
  的图示与自画按 viewBox 缩放——内置图示的 `1000×560`(1.79:1)在 2.67:1 的带里受**带高**卡住,
  实得 1175×658、两侧各留 292 纸(竖屏带宽正好 1080,那边才吃满宽度)。字幕容量随之分档:竖屏 60 字、横屏 110 字;
- **`card=custom` 有净化闸**(`motion/art.py`):白名单只放行绘图元素与绘图属性,`<script>`/
  `<foreignObject>`/`<image>`/外链 href 一律拒,根节点必须带 viewBox,上限 20000 字 / 400 个元素。
  不合格**带镜号硬报错**而不是"悄悄画少一块"——渲染层永远拿不到未清洗的字节(spec 入库前净化一次,
  出片页编译时再净化一次);
- **动效是声明式的**,12 种:`fade, rise, slide-x, draw, grow-x, grow-y, pop, count, wipe, orbit, pulse, pan`,
  配 `data-at`/`data-dur`/`data-dist`(可负)/`data-count-to`/`data-dp`/`data-num-unit`(≤8 字的数量级后缀)/
  `data-period`。带 `data-anim` 的元素**自身不能再写 `transform`**(CSS 会盖掉表现属性);
- **成本口径**:帧数是这条链路唯一的成本项(`MAX_FRAMES_TOTAL = 4000` 硬报错,不静默砍)。
  `settle_ms` 是「第一个什么都不变的时刻」,之后的帧直接复用末帧 PNG,所以花钱的是**动效的时长**而不是片子长度;
  `orbit`/`pulse`/`pan` 是连续运动,永不 settle → 整镜每帧真截(账上 `captured_frames`/`frames` 看得见这笔);
- **六个开关走顶层入参**(aspect / fps / narration / voice / rate / subtitle_mode):嵌在 spec 里的字段对
  计划卡不存在,卡上勾选的值**覆盖**模型写在 spec 里的同名字段——那是用户的决定;`narration` 的终值是字符串
  `"false"`,所以合并时用 None 哨兵判"有没有传"并按文本解析,否则会把开关读反;
- **配乐两种写法都收**:`bgm`(与 `spec.bgm.ref`)给 `obj:` 引用,或直接给**消息附件/曲库歌曲的
  material_id**——零素材链路里模型手上只有 material_id,而唯一产 `obj:` 引用的 `select_BGM` 绑在
  有素材那条链上(`required_nodes=generate_script`),调它会连带补齐 ASR 一串上游。取不到字节直接
  失败,不会静默出成无声片;不传就是纯人声。曲库条目**不挂会话**(`materials.conv_id` NULL),
  归属只认 owner——`resolve` 里曾按 `origin=bgm` 无条件放行,那等于任何人拿 6 位十六进制 id
  就能把别人的歌当自己附件解析并签出可播直链;后门已拆;
- **`source` 如实填**:机器不判史实真伪。出处会如实出现在**分镜卡与逐镜账**上,但版式不会自动把它排进
  画面(要进画面得写进所选 card 的 `visual`,档案卡的 `line3` 就是给这行留的弱色位);成片回执的五级证据
  账本里「音画偏差/帧复用/降级」是机器验过的,「画面在动、无黑屏」标 UNVERIFIED 交人眼;
- **空壳卡不许出片**:`text` 只进字幕条,卡片上看得见的字全在 `visual`(`title.zh`、`dict_entry.word`、
  `compare.left/right`、`levels[].value`…)。模板对空输入不报错——它照画一张只有边框的纸,`ok=True`
  的回包看不见,只有抽出成片帧才看得见,而那时像素已经烧完。所以 `spec._check_visual_content` 把三类
  情况带镜号退回:该版式必需的字段一个没填、数组项数少于模板能画的、数值字段写成中文(会被按 0 画)。
  真机第一版模型自写分镜 12 镜里空了 7 张卡,就是这一关补上的;
- **读数跨量级时自动换刻度**(`motion/diagrams._axis`):`levels`/`chart` 的值**写原数**,
  ≥1 万的读数自动收成 `万`/`亿`/`万亿` 后缀(靠 `data-count-to` + `data-dp` + `data-num-unit`
  三件套,后缀跟着滚动数字一起拼),同组最大最小差到 **100 倍**以上自动切**对数刻度**并在题注补
  「对数刻度:每格 10 倍」,`scale: "linear"|"log"` 可显式指定。起因是第一条真机样片:一座山
  (1e12 千克)、一茶匙中子星物质(1e9)、一个人(70)同框,等比刻度下后两根条只剩 6px,而 13 位数的
  `1000000000000` 直接糊在「单位:千克」那一行上——**条长不再等比,但读数永远是原数**;
- **竖屏 levels 的读数列右对齐在 `x=990`**,最长一条只画到 780:CJK 单位是全角宽,原先从左对齐
  `x=880` 起排时「1.00万亿」被裁出 viewBox 边缘,抽帧才看得见(离线用例断言的是不越界,看不见字宽)。

代码位:契约 `motion/spec.py`、净化与动画 `motion/art.py`、图示 `motion/diagrams.py`、
排版与页面 `motion/templates.py`、出片 `motion/render.py`;节点 `nodes/motion_plan.py` 与
`nodes/motion_nodes.py`;技能 `examples/skills/motion_explainer/SKILL.md`;
样片冒烟 `.smoke/motion_film_smoke.py`(竖屏·档案家族)与 `.smoke/motion_wide_smoke.py`
(横屏·图示·custom·连续运动);版式对照 `.smoke/motion_sheet_smoke.py`——18 个 card × 2 个画幅
各截一帧落 PNG,专查离线用例查不见的那一类错(字掉出可视区、卡缩成邮票)。

### 16. 联网检索:后端按身份热读,出处要对上检索账

零素材通道的第一步是「自己去查」,所以这条链子必须能如实回答**「这次是谁出的结果」**,
而不是假装存在一个万能搜索。

- **三层优先级,每次调用现读**(`tools/web.py` 的 `SearchTool._runner` → `secrets.resolve_search_config`):
  ① 页面「设置」配的搜索后端(`app_secrets` 三项)→ ② 环境变量 `SEARCH_PROVIDER` / `SEARCH_API_KEY` /
  `SEARCH_BASE_URL` → ③ 免 key 回落链。带 key 的四路是 `brave` / `tavily` / `serpapi` / 自建 `searxng`,
  配了 provider 就**只走那一路**;只给了 key 没选后端会被拒绝——猜服务商等于把这把 key 发到别人家门口。
  回执首行 `[检索后端:…]` 标出这次是哪一路出的;
- **免 key 链按查询文字路由**,顺序是真机探出来的而不是排的:中文走维基百科搜索 API,拉丁文字走
  Bing RSS(英文维基随后),DuckDuckGo 垫底。**DDG 在这台机器上两个端点都回人机验证壳**,换 UA、
  改 POST、换端点都一样——那是出口 IP 被后端判成爬虫的信誉问题,不是代码能修的问题;
- **失败口径写进返回值,不写成结论**:被挡时回「这是**这台机器的出口被判成爬虫**,不代表网上查不到」,
  0 条时回「免 key 后端的覆盖面只有百科与通用网页,新近数字与长尾内容本来就不在其中——
  **查不到不等于事实不存在**」。工具描述与技能文档同时给出三条出路:换关键词、`fetch_url` 打开已知页面、
  去设置页配一把搜索 key;
- **检索账与出处闸**(`agent_framework/retrieval.py` + `motion/spec.py`):`web_search` 命中的链接与
  `fetch_url` 真打开过的页面按会话记进 PG `retrieval_hits`(带 backend 与时间)。分镜 `source` 里的 URL
  **只有能在这笔账里反查到才算查证过**,对不上就被出处闸退回——编一个看着像真的链接骗得过排版,骗不过账本;
  没查到的就标「未核实」,不许在答复里替它背书。

代码位:后端与链 `backend/agent_framework/tools/web.py`,按身份热读 `backend/agent_framework/secrets.py`,
记账与查询 `backend/agent_framework/retrieval.py`,设置页读写 `backend/agent_framework/server.py`
(`/settings/search` 写、`/settings/search/test` 让页面当场拿一条查询试这路后端通不通),前端在「设置」页的检索后端区块。

### 17. 选区改:框住画面上的一块 / 点住轨道上的一段,只重烧受影响的那几块

两条出片通道共用**同一个编辑器**(`frontend/src/MotionEditor.vue`),差别只在命中表的形态:
零素材那条认的是**屏幕这一块**(`motion-hitmap/1`,带空间盒),素材 / 口播那条认的是
**轨道这一段**(`segment-hitmap/1`,`hit_mode="track"`,没有空间层)。用户想改的往往只是某一格的数字
或某一段的字幕——整片重做要再把几百帧截一遍、把没动的窗口重编一次,这不是「编辑」,是「重新出片」。
这一节是那条回路。

- **凭据先于交互**:出片时顺手量一张**元素命中表**(`nodes/motion_nodes.py` 的 `_publish_hitmap`),
  落 `renders/{会话}/{产物}/hitmap.json`——元素在画面上的盒(`x,y,w,h` 归一到 0..1)
  ↔ 它来自分镜的**哪个字段指针**(`/shots/0/panel/编号`)。指针按 JSON Pointer 解,解法与后端
  `hitmap._segs` 同一条码路(先解 `~1` 再解 `~0`),两边不会各猜一套。**表跟像素同源**:按镜缓存里存的是
  四件套 `clip.mp4 / frame.png / meta.json / hitmap.json`,命中缓存即「框和画面对得上」,
  重烧那一镜就必须连表一起换——否则「选着改」会照着旧框改错字段。**量不到就明说**:
  `confidence` 只有 `exact/contains/contained/none` 四档,对不回字段的条目留 `field=null`,
  前端把它画成死框、不给落笔;
- **卡片自己带话**:成片回投帧与持久化那条媒体记录都带 `artifact_id` + `hitmap`(bool)。
  「✂ 选区改」按钮的开关**从产物里带出来**,不由前端猜——没有命中表的版本(旧数据、别的通道)
  点了就是 404,那种卡片显示的是「这一版不能选着改(出片时没量到命中表)」;
  同一份判据要跟着**三条**出卡路走:①当轮 WS 帧(`hooks.py::MediaCardHook`)②刷新后从 assistant
  行的 `qa.parts` 重放(`server.py::_render_media_views`)③执行轮还停在待确认、messages 里**还没有**
  assistant 行时从 `artifacts` 表兜底补卡(`server.py::_orphan_render_media`)。第三条真机栽过:
  兜底那段只拼了 `media_url/title/duration/evidence`,于是刷新一次,刚刚还能选着改的那一版按钮凭空消失
  ——卡片看起来「不能改」会被用户读成「这一版坏了」,所以两个字段一个都不能省。
  同一条兜底路还查出两处**只有盯着渲染出来的界面才会发现**的静默失真,已一并修:
  ① 它把 artifacts 里的 `evidence` **原样**透给卡片,而卡片只认 `{claim,label,verified}` 这一份形状,
  库里存的是节点原始出账(`status` 字段)——不折算就等于把验过的全显示成没验:那张中子星的卡片刷新后
  读成「0 项有证据 · 14 项没验」,而它自己的账里明明有 9 条 `verified`。现在第三条路也过
  `hooks._evidence_view` 折一次,三条路同一形状(证据分级是「这条片子说到什么程度」的唯一凭据,
  把它显示成全没验,等于让用户白担心或白信);
  ② 「✂ 选区改」的悬停提示写死了「在画面上框住要改的那块」,那句对**口播 / 素材片是错的**
  (那条是在轨道上点住一段)。现在提示两条链各说一句——按钮的说明按链路形态给,不然用户照着提示去框,
  在轨道形态下什么也框不出来;
- **为什么不嵌第三方剪辑器**:第三方只认时间线与素材,框不出「屏幕这一块属于 `/shots/0/panel/编号`」。
  要的是「鼠标选中的那块就是待改的那格」,这件事只有自己的命中表说得出对应关系,所以编辑器自己画
  (`frontend/src/MotionEditor.vue`:命中层 + 框选 + 轨道 + 表单 + 对比帧);
- **改动拼成一次补丁,四种动作**(`POST /motion/patch`,校验在 `motion/patch.py`):
  ①`edits` 逐格指针改写(框选的主场) ②`shot_sets` 整镜栏位改写(前端 `setFor` 逐栏与原值比 JSON,
  没变的一栏不进补丁) ③`remove_shots` 删镜 ④`reorder` 换序。**空补丁直接拒**;
  可改的镜内字段是白名单 `EDITABLE_SHOT_FIELDS` 那九项,**`id` 不许改**——它是命中表、缓存键与
  时间线三处共用的锚;
- **重烧的账按缓存键算**:`shot_cache_key` 哈希整个镜的 dict + 九个顶层旋钮 `_CACHE_KNOBS`
  (画幅/宽/高/fps/版式/旁白开关/配音/语速/字幕模式)。`voice`/`rate` 这类只在有旁白时才起作用的开关
  **也一律计入**——不玩「这次没旁白就可以不算」的条件哈希,少算一个键就是拿旧片冒充新片。
  所以改 `min_duration_sec` 会重烧那一镜,改文案会,而**删镜与换序零像素成本**——它们只是把已经烤好的
  切片重新拼一遍。顶部改动账就照这个口径报(「未提交:逐格 1 处 · 预计重烧 1 镜」/「重排不烧像素(拼而已)」);
- **时长那一栏有地板**:`sec = max(min_duration_sec, 实测语音时长)`。有旁白时时钟归配音,
  拖到 `speech_sec` 以下**不会有任何效果**,轨道块当场写明「拖到 X.Xs 以下无效:这一镜的时钟归配音」,
  而不是让用户拖完再奇怪为什么片子没变;
- **判据在轮询端**:局部改是长任务,闸门退回(比如把数值字段写成中文)也回一路 200 +
  `status=queued`,真判决在之后 `GET /render_status` 的 `error` 正文里——前端把那句话**原样贴出来**,
  不替它解释;等超时也不谎称取消:提示「这次局部改还在跑,关掉这里不会取消它」。
- **改前/改后靠代表帧**:补丁视图带回 `before_frames`(基线那一版各镜代表帧的对象键),
  编辑器分别向 `/motion/frame` 要旧版和新版的字节,**按镜分列**贴出来(一镜一列,列内改前在上、
  改后在下——只改一镜时看到的就是上下两张,不是左右一对)。**帧字节走 fetch + Bearer 再转 blob**,
  因为 `<img>` 的 src 挂不上凭证。

代码位——**图形科普片那条**：命中表 `backend/storyline_server/motion/hitmap.py`、切片缓存与代表帧
`backend/storyline_server/nodes/motion_nodes.py`(缓存键 `motion/render.py::shot_cache_key`,
对象前缀 `motion-shots/{会话}/{镜头键}/`)、补丁校验 `motion/patch.py`;
**素材 / 口播那条**：段 id 与命中表 `nodes/core_nodes.py`(`_seg_id` / `assign_segment_ids` /
`_publish_segment_map`)+ `backend/storyline_server/timeline_edit.py`(窗口切分 `windows`、
输入清单 `window_inputs`、缓存键 `segment_cache_key`、应重烧判定 `expected_rebuild_ids`、
补丁指针 `read_pointer`),逐窗缓存与按窗渲染 `nodes/core_nodes.py::SegmentSliceCache` /
`render_windowed`(前缀 `render-windows/{会话}/{窗口键}/`);
**两条共用**:出片回投 `backend/agent_framework/hooks.py`(`MediaCardHook`)、
HTTP 口 `backend/agent_framework/server.py`(`/motion/hitmap` `/motion/frame` `/motion/patch` ·
`/timeline/hitmap` `/timeline/frame` `/timeline/patch`——**读表与取帧两对是同一份实现**
(`_hitmap_bundle` / `_frame_png` 只按会话 + 产物 + id 取字节,两条链的差别全在表里有没有空间层),
只有 `/patch` 各走各的节点,
前端 `frontend/src/MotionEditor.vue`(成片卡上的入口在 `App.vue` 的媒体气泡里)。
真机验收 `.smoke/s3_ui_cdp.mjs`:本机 Chrome headless 经 CDP 驱动真实页面,注册一次性身份 →
模型自己出分镜 → 等成片卡 → **鼠标按住拖出框选** → 改值 → 提交 → 轮询到新版本 → 看改前/改后帧 →
拖块换序,截图落 `.smoke/s3_frames*/`。规划轮要十几二十分钟,所以有 `ATTACH=1` 的**接回模式**:
不重开浏览器、不注册新身份,连到端口上那个还活着的页面(token 在 profile 里)。接回时**页面上已经躺着
成片卡就不点任何确认**——那张待确认卡点下去等于白烧一遍像素(真机踩过两次,各多出一版渲染)。
最新一轮干净收口:**30 PASS / 0 FAIL**(`.smoke/s3_ui_run8.log`,截图 `s3_frames8/`),
覆盖"卡片带按钮 → 6 框叠在 3 段轨道上 → 框选出 `/shots/0/text` → 表单落值 → 出版本 A
(改了 1 镜 · 基于 `_default`,旧版字节原位还在)→ 改前/改后两张真帧 → 新版本又成一张成片卡 →
「接着改这一版」把基线换成 A → 切「整镜」改文案 → 出版本 B(基于 A)→ **故意把文案换成不含高亮词
的一句,闸门原话贴在面板上且一帧都没开始渲** → 「清空改动」收干净清单 → 拖块换序只记顺序账"。

口播 / 素材那条的真机验收是**两半**:
① 后端半边 `.smoke/t5_track_patch_smoke.py --user <身份>`——现造一条 6s/3×2s 带音轨的测试素材、
手写一份三窗时间线(一条字幕**故意横跨两窗**)落进 `_default` 作用域、经 **MCP 通道**走
`load_media` → `render_video(render_mode="segments")` → 轮询到终态,再断言:命中表有字节且
`schema=segment-hitmap/1` / `hit_mode=track`、逐窗代表帧 3 张全可 HEAD、首渲账
`segment_cache.windows=3` 且三窗全 `rebuilt`、表里 8 行(画面 3 / 声音 3 / 字幕 2)带 23 条可改字段、
每条标了 `box_precision`。**为什么手写时间线**:这个验证身份没有可用模型密钥,而这一节要验的是
「表 + 帧 + 逐窗缓存 + 补丁」这四件事,与谁来排时间线无关——预置一份 `plan_timeline` 产物正合
`render_video` 的 `required_nodes`,拦截器不会自动补齐,也就不会去敲模型的门槛。
**它不验证**「模型自己排出口播时间线」那一路,那一路由 §核心机制 12 的拦截器与既有真机用例覆盖。
宿主机没有到 :8000/:8001 的路由,所以这套夹具是 `docker cp` 进 `creation-app` 里跑的
(MCP 地址在容器内是 `http://editor:8001/mcp`)。最新一轮:**OK(0 failures)**。
② 界面半边在真实页面上走完:卡片「✂ 选区改」开编辑器 → 三条轨道按秒铺块、段 id 与表一致 →
点住那条横跨两窗的字幕(播放头落到 2.5s,当场写明落在画面段 `ev-22f922cf3f`)→ 表单给出
`/subtitles/1/{text,style,start,end}`,数值字段是数字输入框 → 改文本提交 → 出版本 `c5bd33d3`,
回执「改了 1 段(su-74c0c6a417) · 重烧 2 窗(ev-22f922cf3f、ev-11cd757080) · 复用 1 窗没动过的字节 ·
基于 `_default`,旧版字节原位还在」→ 改前/改后**成对**贴帧,且改后那两张肉眼可见带着
「第二句改过了:横跨两窗」——重烧范围与改动等价这件事,在这儿是看得见的。

- **两条链,两种命中形态,一个编辑器**:编辑器读表里的 `schema` 与 `hit_mode` 决定给不给空间层——
  `motion-hitmap/1` 画框、鼠标在画面上按住拖出即选中 `/shots/N/字段`;`segment-hitmap/1`
  (`hit_mode="track"`)不画框,选中的是**轨道上那一段**,指针形如 `/subtitles/1/text`、`/video/2/start`。
  两张表的键名刻意一致(`timeline` 排行、`shots/{id}.entries` 回指字段),所以前端只有一套面板代码,
  差别是"用鼠标框"还是"在轨道上点"。表由 `nodes/core_nodes.py::_publish_segment_map` 在成片发布之后
  顺手量出来,落 `renders/{会话}/{产物}/hitmap.json` + 逐窗 `frames/{窗口id}.png`;
  **口播链的段 id 跟内容走、不跟位置走**(`_seg_id` / `assign_segment_ids`):按下标寻址的东西
  会在用户拖一次顺序或改一句字幕后集体错位,结果是「改一处、重烧全片」;
- **口播链的重烧按「窗」算**:`render_mode="segments"` 时成片被切成固定窗口,每窗一份输入清单
  (`timeline_edit.py::window_inputs`)算出一个缓存键(`segment_cache_key`,键里带窗口 id 好让表和日志
  按行找回自己),切片由 `nodes/core_nodes.py::render_windowed` 烧、存进
  `SegmentSliceCache`(`render-windows/{会话}/{窗口键}/clip.mp4`)。补丁落笔后**逐窗比对输入清单**
  (`timeline_edit.py::expected_rebuild_ids`)决定哪些窗非重烧不可——判据不是"哪些段被点名改过",
  所以改一句字幕给出的是它压在的那一(跨边界就两)个画面窗。没动的窗按两级取字节:缓存命中直接取,
  否则从上一版成片里切(`from=base_cut`)。**重烧范围与改动等价**是可核对的事实,不是话术:
  真机改一条横跨两窗的字幕,回执「改了 1 段(su-74c0c6a417) · 重烧 2 窗(ev-22f922cf3f、ev-11cd757080)
  · 复用 1 窗没动过的字节」,账本在产物 `output.segment_cache`(`ledger/reused/rebuilt/windows`),
  `base_cut` 也计入复用,不谎称重烧;

- **两条链的「另起版号」是同一条规则**:局部改分叉时,传入的版号是空、是 `_default`、
  **或恰好等于本次的 base**,都得换一个新号——第三种最容易漏(前端「接着改这一版」把基线换成 A 之后
  传回来的就是 A 自己的号),沿用即把 A 覆盖掉,而 A 那一版字节是这次改动唯一的退路;
  已经是别的版本号则沿用,好让「接着改同一版」连续叠代。两条链的 `_fork` 各有一份离线用例钉它:
  图形科普片那份用桩存储把四种输入各钉一次(`tests/test_motion_channel.py`),
  口播那份直调 `_fork` 并回头核对 `v1` 的渲染任务行与成片字节都原位没动
  (`tests/test_patch_video_render.py` 的 ⑤);

**边界(不粉)**:① 服务端还没有「列出某会话全部版本」的口子,跨刷新找回旧版要在对话里报产物号;
② `motion-shots/*` 与 `render-windows/*` 的切片、代表帧**都没有保留期**,一次改一版就多存一份,
清理策略没定;
③ 零素材那条的命中表只覆盖排版画出来的那几族版式,`custom` 自画里的文字同样能回指,但**框选精度取决于排版落点**;
④ 口播那条的表**带着框但不画框**:字幕条的框是按排版公式**估**出来的(`timeline_edit.subtitle_box`,
`box_precision="estimated"`),其余轨道给整画布框(`derived`)——这张框只用来"万一以后要按空间点"时
标明自己有多可信,当前 `hit_mode="track"` 下前端不画空间层,字段指针直接从 `doc` 取,与框无关;
⑤ 画面上改了数字不会重跑配音——文案 `text` 与画面 `panel` 是两处,只改画面那一处时旁白仍念原话,
这是选区改的语义,不是 bug,界面上两栏分开摆着。

## 剪辑节点(25 个 DAG 节点)

`available_nodes` 白名单里是 **25 个 DAG 节点**(`core_nodes.py` 的 21 个 + `web_nodes.py` 的 `render_web`
+ `motion_plan.py`/`motion_nodes.py` 的 `plan_motion`、`render_motion_video`、`patch_motion_video`)
**+ 2 个控制面工具**(`read_node_history`、`render_status`)—— 主服务那行启动日志报的
「已接入 27 个剪辑节点」就是这 27 个。剪辑服务自己的 `nodes=25 tools=28` 多算了一个
`dag_status`(编排预览用,没有包成模型工具)。★ = 必须显式调用(不会被自动补齐)。

| 工具名 | 界面名 | 干什么 |
|---|---|---|
| `search_media` | 检索素材 | 按需求找可用素材 |
| `load_media` | 素材入库 | 登记并抽取媒体元数据 |
| `split_shots` | 镜头切分 | 按场景变化切段(`scene_threshold`) |
| `asr` | 语音转写 | 本地 faster-whisper,产出带 id 的段 |
| `correct_transcript` ★ | 转写修字 | 只改字不改时间与词 id,产出修字表 |
| `speech_rough_cut` | 口播粗剪 | 去停顿、按句挑可用段 |
| `understand_clips` | 画面理解 | 逐镜 VL 描述(与主 LLM 同源的一个 key) |
| `filter_clips` ★ | 片段筛选 | 按内容/质量挑段 |
| `group_clips` ★ | 片段分组 | 按主题/用途分组 |
| `script_template_rec` ★ | 脚本模板推荐 | 出叙事骨架 |
| `generate_script` | 文案生成 | 写配音稿与字幕文本 |
| `generate_voiceover` | 口播配音 | edge-tts 合成 + 对齐 |
| `generate_ai_transition` | AI转场生成 | 生成过渡素材 |
| `transition_rec` ★ | 转场推荐 | 选转场类型 |
| `text_rec` ★ | 花字推荐 | 选花字/字幕样式 |
| `select_BGM` | 背景音乐选择 | 从个人曲库挑,无曲库时生成占位轨 |
| `plan_timeline` | 时间线编排 | 排画面/声音/字幕/BGM 四轨 |
| `plan_timeline_pro` | 时间线编排·专业版 | 更细的编排策略 |
| `plan_timeline_ai_transition` | 时间线编排·AI转场 | 带 AI 转场的编排 |
| `render_video` | 成片渲染 | 出片(含 `dry_run` / `overlay_events` / `wait_sec`) |
| `patch_video` | 口播片局部改 | 选区改的后端:读那一版自带的分段命中表 → 按指针改几段 → fork 新版本 → **只重烧受影响的那几窗**(其余按输入清单比对,从切片缓存直接取字节),`POST /timeline/patch` 提交的就是它 |
| `render_web` | 网页渲染出片 | 浏览器侧出片路径 |
| `plan_motion` | 图形科普片分镜 | 零素材通道第一站:校验/归一分镜 spec 并入库,回时长估算与偏离提醒(**不渲染**) |
| `render_motion_video` | 图形科普片出片 | 逐帧截图出片(长耗时,提交后由 `render_status` 续问;支持顶层六开关覆盖) |
| `patch_motion_video` | 图形科普片局部改 | 选区改的后端:fork 基线 spec → 按命中表指针改几格 → **只重烧受影响的那几镜**(其余从切片缓存直接取),`POST /motion/patch` 提交的就是它 |
| `read_node_history` | 读取节点产物 | 控制面(非 DAG 节点):按节点名读该节点已产出的 Store 结果 |
| `render_status` | 渲染进度查询 | 控制面:轮询出片与局部改提交的任务(`render_video` / `render_motion_video` / `patch_motion_video` / `patch_video`),回 `status/stage/percent`,`done` 时同批回成片。**选区改的判决也走这里**——闸门退回的话在 `error` 正文里 |

## 数据模型(22 张表)

| 分组 | 表 | 要点 |
|---|---|---|
| 身份与对话 | `users` `conversations` `messages` | 消息挂 `conv_id`;`qa.parts` 存媒体部件供刷新重放 |
| 素材 | `materials` `upload_sessions` `timelines` | 归属列是 `owner_user_id`;分片续传以桶内进度为准 |
| 产物与渲染 | `artifacts` `render_jobs` | 产物按 `(作用域, 产物集)` 隔离;`render_jobs` 有 `attempt` 令牌与 `UNIQUE(session_id, artifact_id)` |
| 执行链 | `checkpoints` `checkpoint_entries` | 指针行 + 增量链(`delta`/`full` + `parent_seq`) |
| 团队与调度 | `inbox_messages` `tasks` `task_edges` `subagents` `scheduled_jobs` | 消息中心 / 任务图 / 常驻子 Agent 租约 |
| 记忆与扩展 | `memories` `skills` `skill_files` `mcp_servers` `app_secrets` | 记忆 PK `(user_id, category)`;技能与 MCP 按用户隔离,系统内置的 `owner_user_id` 为空 |
| 检索账 | `retrieval_hits` | 本会话 `web_search` 命中的链接与 `fetch_url` 真打开过的页面。PK `(session_key, url)`,带 `title`/`backend`/`checked_at`;出处闸只认这笔账,按 `user_id` 级联删 |
| 计量 | `token_usage` | 配额闸的数据源 |

## 对外接口(节选)

- **鉴权**:`POST /auth/register` · `POST /auth/login` · `POST /register`(匿名 token,可被 `REGISTER_OPEN=0` 关) · `GET /whoami`
- **对话**:`POST /chat`(投 MQ) · `POST /chat/sync`(同步版,给脚本用) · `WS /ws/{conv_id}?token=…` · `GET /sessions`
- **会话与执行**:`/convs*` · `/convs/{cid}/runs[/active]` · `/runs/{id}[/history|/fork|/resume|/approve]` · `GET /preview/{cid}`
- **计划门**:`GET /plans/{id}` · `POST /plans/{id}/confirm` · `POST /plans/{id}/revise`
- **素材**:`POST /upload` + `/upload/init|part|status|complete|abort`(分片续传) · `/materials*` · `POST /fetch_media`(按链接取料)
- **BGM 曲库**:`GET /bgm` · `/bgm/search`(多源聚合) · `/bgm/url` · `POST /bgm/import` · `DELETE /bgm/{id}`
  在线搜歌/取链走外站 `music-api.gdstudio.xyz`(joox → netease → bilibili 依次降级),
  用 `urllib` + 浏览器 User-Agent 直连——该站按 UA 黑名单挡机器人客户端,默认 UA 直接 403;
  **镜像里没有 curl**,任何 shell out 到 curl 的写法在容器内都是静默失败(`FileNotFoundError` 被逐源吞掉,表现为搜索恒空)
  源调不通**不伪装成"没这首歌"**:单个源挂了 → 200 + `hint` 里点名(带 HTTP 状态码,403 与 503 处置不同);
  三个源全挂 → 502「音乐源均无响应」。曲库条目不挂会话(`/bgm/import` 传 `conversation_id=None`),
  `materials.conv_id` 留 NULL、不凭空造 conversations 行。
  **2026-10-08 真机复验**(`backend/.smoke/bgm_ui_cdp.mjs`,11/11):容器里搜到 20 首、导入 7.2MB/187s 落进曲库并挂上可播直链、
  把 `/bgm/search` 打成 502 时面板显式报「音乐源均无响应:…」而不是退回"没这首歌"的空状态
- **渲染**:`POST /render_direct`(支持 `wait_sec` / `dry_run`) · `GET /render_status` · `/timelines*` · `GET /latest_timeline`
- **选区改(两条链,各一对口)**:
  图形科普片 `GET /motion/hitmap`(这一版的元素命中表,带空间盒) · `GET /motion/frame`(按镜代表帧,回 PNG 字节) ·
  `POST /motion/patch`(框选出来的补丁 → fork 新版本,只重烧受影响的那几镜);
  素材 / 口播 `GET /timeline/hitmap` · `GET /timeline/frame`(参数 `shot` 传的是**窗口 id**,与表里的行同名) ·
  `POST /timeline/patch`(轨道上点住的那一段 → fork 新版本,只重烧受影响的那几窗)。
  **读表与取帧两对共用同一份实现**(`server._hitmap_bundle` / `_frame_png`),只有 `/patch` 分头走
  `patch_motion_video` 与 `patch_video`。**长任务,判据在轮询端的正文里**:
  闸门退回也表现为一路 200 + `status=failed` + `error`,HTTP 状态码不作判据
- **扩展管理**:`/tools` · `/skills*`(含 .zip 技能包) · `/mcp/servers*`(热连热断)
- **设置**:`/settings*`(模型密钥、主模型选型、判断模型三件套 + `POST /settings/judge/test` 连通性自检;
  检索后端三件套 + `POST /settings/search/test` 试一路)
- **运维**:`GET /health` 存活探针。免凭证的只有它和三个**签发凭证**的入口
  (`/auth/login` · `/auth/register` · `/register`),其余全部要求凭证。

前端 vite 开发时代理上述 18 个前缀 + `/ws` 到 `127.0.0.1:8000`;生产不需要它。

## 技术栈

| 层 | 技术 | 版本 / 说明 |
|---|---|---|
| 语言运行时 | Python | 3.12+(容器固定 `python:3.12-slim`;本机实测 3.13) |
| Web | FastAPI · uvicorn · websockets | ≥0.136 / ≥0.47 / ≥15.0 |
| Agent 框架 | **自研**(`agent_framework/`) | 主循环 + 显式读写集并发 + 一致点 checkpoint + 计划门 + HITL;选它而不是 LangGraph:核心机制都要能被逐字段审计(见"设计文档") |
| 模型接入 | openai SDK(OpenAI 兼容) | ≥2.38,默认 DeepSeek;思考模式 `OPENAI_THINKING` 默认 off |
| 工具协议 | MCP:官方 `fastmcp` 服务端 + 自研客户端 | mcp ≥1.28;stdio 与 streamableHttp 两种传输 |
| 判断模型 | TypeSafe `/v1/systemone` Noul 原语 | 可选第二意见层,非 OpenAI 兼容口 |
| 消息队列 | 自研内存 MQ,可切 Kafka | `--mq memory\|kafka`;按会话哈希分区 |
| 存储 | SQLAlchemy(async) + asyncpg + PostgreSQL 16 | ≥2.0.49 / ≥0.30;连接池三条参数可配 + `pool_pre_ping` |
| 对象存储 | MinIO(S3 兼容) | ≥7.2;媒体字节与产物 |
| 缓存/广播 | Redis | 可选:仅跨副本回投广播 |
| 剪辑 | ffmpeg + ffprobe(PATH) · MoviePy ≥2.0 | 两条渲染路径:MoviePy 主路 + ffmpeg 兜底 |
| 语音 | faster-whisper ≥1.0(本地 ASR) · edge-tts ≥6.1(免费在线) | 首次运行需下载模型 |
| 零素材出片 | headless Chrome/Edge 逐帧截图 + 自包含 HTML/SVG 排版 | 不引 Playwright/Selenium,也不引生成式图像模型:一帧 = 一次 `--screenshot`。容器里跑的是镜像自带的 `chromium`(root 身份自动补 `--no-sandbox`,恒带 `--disable-dev-shm-usage`);本机想用别的浏览器用 `STORYLINE_CHROME` 指过去 |
| 视觉理解 | 与主 LLM 同源的多模态模型 | 默认 `deepseek-flash`(实测能吃 `image_url`) |
| 链接取料 | yt-dlp ≥2026.8.19 | YouTube 需 JS 运行时 + 网络出口 |
| 前端 | Vue 3.5 + Vite 6(无组件库、无路由/状态库) | 单文件 `App.vue`,build 产物由主服务托管 |
| 部署 | Docker Compose · 两阶段 Dockerfile · Caddy | 反代 :80/:443;`fonts-noto-cjk` 供字幕渲染、`chromium` 供逐帧截图,都进镜像 |
| 鉴权 | 手写 HS256 JWT + PBKDF2-HMAC-SHA256(30 万轮) | 不额外引 pyjwt/bcrypt:一个签发用途不值得多一个依赖 |

## 配置

### `.env`

模板 `.env.example` 里写全的是必填/常改的那批(存储、密钥、账号配额、域名、判断模型、yt-dlp);
下面表格里的**带 ★ 变量不在模板中**——它们是代码级 env 回落位,只在确实要改默认值时才手动加进 `.env`。

| 组 | 变量 | 说明 |
|---|---|---|
| 存储 | `PG_DSN` · `PG_USER/PASSWORD/DB` · `PG_POOL_SIZE` · `PG_POOL_MAX_OVERFLOW` · `PG_POOL_PRE_PING` · `PG_POOL_RECYCLE_SEC` | PG 连接与池 |
| 存储 | `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` · `MINIO_SECURE` | 对象存储 |
| 存储 | `MINIO_PUBLIC_ENDPOINT`(compose 默认 `127.0.0.1:9000`) · `MINIO_PUBLIC_SECURE` | **只用于给浏览器签媒体直链**;数据读写仍走 `MINIO_ENDPOINT`。预签 URL 的签名包含 Host,事后把 `minio:9000` 换成宿主地址必然 403,必须用浏览器解析得到的地址**签**。远程机器访问页面时改成本机 LAN IP,并放开 MinIO 端口(默认只发布在宿主回环) |
| 模型 | `OPENAI_API_KEY`(别名 `DEEPSEEK_API_KEY`/`SILICONFLOW_API_KEY`,同层回落) · `OPENAI_THINKING` | 页面「设置」永远压过环境变量 |
| 模型 ★ | `OPENAI_MODEL` · `OPENAI_BASE_URL` | `llm_openai.py:149,151` 的回落位;优先级在"前端配置 + 进程内常量"之后 |
| 判断模型 | `JUDGE_BASE_URL` · `JUDGE_MODEL` · `JUDGE_API_KEY` · `JUDGE_MIN_CONFIDENCE`(默认 0.80) | 三项配齐走 Jev,否则回落主 LLM |
| 账号与配额 | `JWT_SECRET` · `REGISTER_OPEN`(默认 1) · `QUOTA_LIMIT`(默认 0=不拦) · `QUOTA_WINDOW` | `JWT_SECRET` 缺省时首启自动生成入库 |
| 部署 | `SITE_DOMAIN` | 供 Caddy 出 HTTPS 块 |
| 取料 | `YTDLP_JS_RUNTIMES` · `YTDLP_COOKIES_FILE` | cookie 只写**文件路径**,凭证值不进代码/日志/库 |
| 运行开关 ★ | `MQ_BACKEND`(默认 memory) · `KAFKA_BOOTSTRAP_SERVERS` · `STORAGE_BACKEND`(默认 pg_minio) · `BROADCAST_REDIS_URL` · `OBJECT_CACHE_ROOT` · `WORKSPACE_ROOT` · `MAX_FETCH_MB`(默认 512) | 都是 `run_server.py` 里对应 CLI 参数的 env 默认值——`--mq/--storage/--broadcast-redis-url/--cache-root/--workspace-root/--max-fetch-mb` 显式传参时压过它们 |

### `backend/examples/storyline/config.toml`(剪辑能力)

`[capabilities]` 只写**环境变量名**、绝不写值。写在文件里的关键项:`vl_base/vl_model/vl_key_env`、
`vl_max_tokens=1024`(思考型模型预算太小会空返回)、`vl_thinking=false`
(逐镜一次请求,实测快约 2 倍、省约 4 倍输出 token)、`asr_model=small`、`asr_device=auto`(→cuda,失败回落 cpu+int8)、
`provider_timeout_sec=180.0`、`tts_voice`、`scene_threshold=0.3`、`max_pause_sec=1.0`、`bgm_volume=0.2`、`font_dirs`。

渲染的四个节流参数**不在文件里**——它们是 `storyline_server/settings.py` 的默认值,注释只说明"不写即用默认":
`render_grace_sec=20.0`(内联等待)、`render_wait_max_sec=300.0`(显式 `wait_sec` 上限,须明显小于 MCP 的 `timeout=600`)、
`render_max_concurrent=2`、`render_stall_sec=600.0`(0 = 关闭看门狗)。

`[storage]` 的 `workspace_root/cache_root` 是**可丢弃缓存**不是数据:内容缓存按 LRU 逐出、渲染工作区到终点即回收;
PG/MinIO 五项端点一律从环境变量读,不进本文件(本文件随仓库发布)。
缓存落盘**不能假设下载临时文件与缓存同卷**:容器里 `/` 是 overlay、`.runtime` 是挂载卷,
`os.replace` 跨设备直接 `EXDEV(Errno 18)`,而这条路的下游是"渲染前从桶里取素材字节"——
先 `rename`、失败退化成"拷进目标目录的 `.part` 再同目录 `replace`"(`storage/object_store.py`),
既不留半成品冒充完整文件,也不让每一次回源都炸在渲染路上。

### 常用开关(`python run_server.py --help` 可查全)

`--mq` · `--storage pg_minio|memory` · `--broadcast-redis-url` · `--port/--host` ·
`--approve-tools` · `--quota-limit/--quota-window` · `--no-resume`(启动不自动接手) ·
`--no-render-gate` · `--max-iterations`(默认 40) · `--max-context-tokens` ·
`--max-upload-mb`(默认 1024) · `--max-fetch-mb`(默认 512) · `--no-ytdlp` ·
`--no-storyline/--storyline-config` · `--no-mcp/--mcp-config` · `--no-skills/--skills-dir` · `--no-team` ·
`--instance-id`(多副本认领用)

## 快速开始

### A. 本机跑(开发)

```bash
docker compose up -d                     # 只起存储层(postgres + minio + redis)
cp .env.example .env                     # 本地默认值可直接用

cd backend
pip install -r requirements.txt -r requirements-storyline.txt
# PATH 上需有 ffmpeg/ffprobe;首次 ASR 会联网下载 faster-whisper 权重

python run_storyline.py --config examples/storyline/config.toml   # ① 先起剪辑服务 :8001
python run_server.py --port 8000                                  # ② 再起主服务 :8000

cd ../frontend && npm install && npm run build                    # 产物 dist/ 由 :8000 托管
```

打开 **http://127.0.0.1:8000**,注册账号后进「设置」填一次模型密钥(写库即生效,两个服务热读)。
Windows 下可用 `backend/start.bat`(pythonw 无窗口后台起,日志 `.storyline\storyline.log` 与
`.main_server.log`)——注意**别加 `--no-resume`**,那会让崩溃接手失效。

### B. 容器跑(单机 / 公网)

```bash
docker compose up -d                          # 存储层
docker compose --profile app up -d --build    # + editor(:8001) + app(:8000) + caddy(:80)
```

- 存储端口**只绑 127.0.0.1**(PG 5432、MinIO 9000/9001),**Redis 不做宿主机映射**——本机原生 Redis
  占 6379 时不会互相撞死;公网暴露面只有 Caddy 的 80/443;
- `app` 的启动命令**必须带** `--storyline-config examples/storyline/config.docker.toml`:默认
  `config.toml` 的 `connect_host` 是 `127.0.0.1`,在容器里那是它自己,启动期 MCP 探测必然
  `Connection refused` → 整站「无剪辑能力」(计划门也不会出卡)。`config.docker.toml` 的唯一差别就是
  `connect_host = "editor"`;
- `editor` 带 TCP 就绪探针、`app` 依赖它 `service_healthy`——两者同秒拉起时,不等就绪就会撞同一个坑;
- 接线成功的判据(启动日志明说):`[startup] Storyline 已接入 26 个剪辑节点（DAG 契约 24 节点，rerun_from 可用）`
  + 中文名词表从 29 条涨到 52 条;
- `deploy/Caddyfile` 默认 `:80 reverse_proxy app:8000`,配 `SITE_DOMAIN` 后取消注释出 HTTPS;
- `WHISPER_PRELOAD` 构建参数可预下载 ASR 权重;`caddy` 没有 healthcheck(它不是依赖项)。

## 测试与验证

| 层 | 内容 | 怎么跑 |
|---|---|---|
| 离线回归 | `backend/tests/` **84 份**脚本式套件——每份自带 `asyncio.run(main())` 与断言计数,pytest 把**整份脚本**收成一个用例、以子进程执行(全量输出落 `.tmp/reg_pytest/`)。pytest 从脚本内部还会解析出 15 个假用例(`async def` 缺插件 / 缺 `tmp` fixture),由 `conftest.py` 在收集后丢弃——否则标准入口的结论全是假故障 | `cd backend && python -m pytest`(或单跑 `python tests/test_xxx.py`) |
| 真机冒烟 | `backend/.smoke/` **24 个** `*_smoke.py`(b3~b17 + `settings_key`/`thinking_off`/`vl_shared_key` + `motion_film`/`motion_wide`/`motion_sheet` 三条零素材样片)+ **15 个**探针/种子脚本(含 b18 搜索链六路探针、b19 跨量级读数抽帧对照) | 依赖模型的那批带 `credit_gate()` 前置闸:额度不就绪直接 SKIP 并打印原因,不会把"账户空了"报成"代码坏了" |
| 关键路径的钉子 | 计划门(`test_plan_gate`)· 出口中文(`test_display_outlets`)· 存储契约(`test_storage_contract` 212 项)· 渲染跟随(`test_render_follow`)· dry-run 路由(`test_dry_run_dispatch`)· 修字(`test_transcript_correction`)· 复盘(`test_plan_retrospective`)· 判断模型(`test_judge_gate`)· 零素材通道(`test_motion_channel` 236 项:净化闸拒绝清单、动效停止时刻、横屏第二套版式、字幕容量分档、图示几何不越界、跨量级读数自动换对数刻度与「万/亿」收成) | 见各文件头的"钉住 N 件事" |
| 提示词回归 | `tests/data/planning_round_golden.json` 金样逐字比对 + `scripts/run_eval.py` 的 prompt fingerprint | 改提示词后先跑这两个 |

> **重要**:`backend/tests/`、`backend/.smoke/`、`backend/scripts/`、`backend/docs/`、`backend/pytest.ini`、
> `backend/start.bat` 都在 `.gitignore` 里——**克隆出去的仓库不含这批**,`git ls-files backend` 只有 105 个文件。
> 想连测试一起分发,把它们移出排除清单。
> 另一条已知足迹:离线套件默认用 `pg_minio` 装配,会往真 PG 写 `sess-*` 指针行(不进未完成集合,无害但可见)。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 回复「无剪辑能力」 | :8001 没起或没连上。**必须先起 :8001 再起 :8000**(主服务启动时建立 MCP 连接并取 DAG 契约) |
| 一切对话都报 402 | 模型账户余额不足。页面「设置」重填密钥即热生效,不用重启;判断模型也会如实退回词面判据 |
| 401 / 4401 | JWT 过期或凭证不对(签发 TTL 7 天);WS 握手不合法直接 close(4401),不 accept |
| 换了浏览器看不到之前的会话 | 会话跟登录账号走(锚点是 user_id),同一账号任何设备登录都能看到 |
| 渲染进度不动 | 进度是逐帧真实上报的;600 秒无进展会被看门狗按失败收口,不会永远挂着。要问进度:`GET /render_status?artifact_id=…` |
| 首次 ASR 很慢 | faster-whisper 权重首次运行需下载(几百 MB),之后常驻 |
| 页面打不开 / 是空白 | 前端没 build(`cd frontend && npm run build`),或 :8000 没起 |
| YouTube 取料失败 | 需要网络出口 + JS 运行时(`YTDLP_JS_RUNTIMES`,装 deno 或 node);登录态站点按 `YTDLP_COOKIES_FILE` 给 cookie 文件 |
| 界面上还是看到 `split_shots` 这类英文 | 说明那条工具没声明中文名(词表未覆盖 → 如实退回机器名);补 `display_name` 即可 |
| 改了 `agent_framework/` 代码没生效 | :8000 是常驻进程,**要重启才上线**;改剪辑节点则重启 :8001 |

## 已知边界(如实,不粉)

- **规划轮"该不该出卡"仍是模型判断**:词面判据 + 判断模型只能保证"没出卡不许说出了",
  不能保证"该出时一定出";
- **覆盖层锚点粒度是 ASR 段(句),不是逐词**;覆盖层比窗口长只截尾;覆盖层永远静音;
  覆盖层只参与音画同步判定,不参与画面语义(它盖住的是谁、对不对题,机器无从判断);
- **修字闸的容差是字数比例**,`max(3, 30%)`——真错字也可能不被列(机器判不了对错),
  修字结果也不覆写 `asr` 产物(靠下游优先读 `corrections`);
- **判断模型默认启用主 LLM 兜底**:没有独立关闭开关(要完全只留词面判据,目前没有配 `JUDGE_*` 且不配模型密钥
  以外的办法)——它只在"该拦没拦"的形状出现时才调;
- **`--quota-limit 0` 是刻意默认**:不拦截但仍记量;
- 前端只有**两个组件**:`App.vue`(约 5600 行,含全部面板与聊天窗)与 `MotionEditor.vue`(约 840 行,
  选区改面板,由 App.vue 挂上)。**没有路由、没有状态库**,功能对等,可读性有账;
- Python 版本要求只在 `Dockerfile` 与本文声明,**代码里没有运行期版本校验**;
- 多副本:MQ 分区是 `crc32 mod N` 的近似互斥,崩溃接手靠租约;跨副本接管有真机冒烟,
  但**没有做过大规模副本验证**;
- **零素材图形科普片**:当前只实现 `archive_card` **一套**纸质模板(`spec.STYLES` 单值),方屏是复用竖屏版式
  (四周留同一张纸)而不是第三套排版;
- 横屏的档案卡**按自身像素落在 1760 宽的带里**(卡本身 560~860 宽),两侧留纸是设计而不是没排满;
  图示卡在横屏也**铺不满带宽**(1000×560 的坐标按带高缩放 → 1175 宽)——真要吃满 1760 得自画宽幅
  viewBox(`custom` + `0 0 1600 600`),内置那七种不为横屏重画一版;`theatre`/`silhouette` 是满带深色,
  上沿正好接在状态面板第二行下面;
- 图示卡的项数有上限(`MAX_ITEMS = 8`、散点 `MAX_POINTS = 30`),超出**截断**并保留前若干项——
  一镜画不下 20 个数据点时应该拆镜,不是挤在一张图上;
- 逐帧截图是**串行**的:本机实测每帧约 0.8~0.9 秒(含编码),一条 2 分钟 15fps 的片子约 1800 帧 = 二十分钟级;
  整条片子的帧数上限 4000(`MAX_FRAMES_TOTAL`,超了直接报错不静默砍);
- `orbit`/`pulse`/`pan` 是连续运动,**永不 settle** → 该镜每帧都得真截,帧数 = 时长 × 帧率,
  别把它放在长镜头上(账上 `captured_frames == frames` 就是这笔);
- `card=custom` 只放行**矢量绘图**:位图、外链字体、外部 CSS、SMIL 动画一律没有,自画的图只能用
  SVG 基本形状 + `data-anim` 的 12 种声明式动效;
- 这条通道的配音依赖 edge-tts **出网**;离线环境里 `narration=true` 会失败,`narration=false` 仍可用;
- 画面字体吃镜像里的 `fonts-noto-cjk`;本机没装 Noto Serif SC 时回落 SimSun/雅黑,版面观感会变(字宽不同,
  字幕换行位置随之挪)。
- **2026-10-09 修掉的静默失败**:moviepy 出片路径原先只在 `font_dirs` 的**顶层**找字体,而 Debian/镜像里的
  `fonts-noto-cjk` 装在 `/usr/share/fonts/opentype/noto/` 两层之下——顶层一个字体文件都没有,
  于是退到根本不存在的 `arial`,`TextClip` 抛错又被逐条 `except` 吞掉:**成片没有字幕,账面一切正常**
  (真机在容器里跑了很久才被发现)。现在 `_find_font` 递归扫,且「请求了字幕却一条都没生成」时打
  `[storyline] 字幕 N 条全部没能生成(字体=…)` 一行警告;`tests/test_render_pipeline.py` 补了
  「字体藏在两层之下」的回归,容器内跑真渲染验过字幕层出得来。
- **同一天查出的第二个字幕缺陷(这次不是没字,是字被削平)**:moviepy 2.1.2 在 Pillow≥11 下反推 `caption`
  高度时走的是 `height = bottom - top` 的兜底分支(旧的 `draw._multiline_spacing` 已删),这个值比真实字形
  矮一个 `descent`——本机与容器各量三种文案(单行中文 / 带拉丁下伸 / 长文换行),**墨迹的最后一行正好是图的
  最后一行**,也就是每条字幕的下沿都被削掉一截。账面同样全绿,只有把帧存成图用眼睛看才发现。
  现在 `TextClip` 传 `margin=(0, 0, 0, 字号//2)`,底部垫出来(实测下沿留 5~13 px 空隙),字幕顺带离底边
  有了安全距离;`tests/test_render_pipeline.py` 的「⑦-b 字幕字形要画全」逐层量像素,墨迹顶到图边即红。
- 联网检索的**免 key 回落链覆盖面只有百科与通用网页**:新近数字、长尾中文网页本来就不在里面,
  DuckDuckGo 那一路在开发机上被出口 IP 信誉判成爬虫(换 UA / 改 POST / 换端点都一样)——**这不是代码能修的**。
  要稳定查证,必须在「设置」页配 brave / tavily / serpapi / 自建 SearXNG 其中一路;
- **出处闸只对账,不判真伪**:它能保证"没真打开过的链接不许写成查证过",不能保证查到的内容是对的。
  史实对错仍归模型与人工;
- **冒烟留下的数据分两种清法**:身份可以点名叫 `scripts/purge_identities.py` 删(默认 dry-run,
  `--apply` 才真删;PG 行按 `ON DELETE CASCADE`,桶里 `users/<uid>/` 前缀一起收)。但
  `artifacts` / `checkpoints` 只按 `sess-*` 连接会话记账,schema 里**没有身份列也没有外键**,删身份带不走它们。
  2026-10-09 按「只留一个真实账号」的口径清了能归属的那半:**3140 个身份 / 连带 4257 行 / 桶里 138 个对象 5.2 GB**,
  系统内置的 6 条技能与 1 条 MCP(`owner_user_id` 为 NULL)不在级联范围内,清完仍在。
  **剩下的堆积是 `sess-*` 那批**：artifacts、checkpoints + checkpoint_entries、render_jobs、subagents、tasks、
  task_edges，以及桶里不认身份的顶层前缀 `renders/`、`motion-shots/`、`derived/`
  —— 这批**没有任何字段能对回账号**(实测 0 行命中 `conversations.id`),所以「只保留某账号的」这个口径表达不出来,
  只有全留与全删两个选项。2026-10-09 选了全删，入口是新加的 `scripts/wipe_session_tables.py`
  （同样默认 dry-run，`--apply` 才动手；本机当天清掉 6.9k 行 + 293 个对象 / 2.05 GB，桶里只剩内置技能与一个账号）。
  **但这是清账不是修好**：这些表按 run 只增不减，没有 TTL 也没有自动回收，跑一阵就会重新涨回来。
  两条口径记在这里：`scheduled_jobs` 里那行 `heartbeat` 是系统自己的 30 秒任务，**不属于历史产物**，
  清理名单故意不含它；`default` / `cron` 两个内部身份删了会在下次启动自检时重新登记，不算数据丢失。
  落地时踩到的两点:`purge_identities.py` 刻意只认逐个点名的 id(批量口径要自己生成清单再喂 `xargs`);
  删完桶里会留下**0 字节的目录标记**——`objects.head(前缀/)` 仍然打得开而 `list_prefix` 不列它,
  拿 head 判「对象还在不在」会误判成没删干净。
- 历史日志(早于本轮掩码改造的 `.tmp`/`.log`)里存在当时明文写下的 WS token,属历史数据,未回溯清洗。

## 设计文档

架构决策与逐轮审计记录在 `backend/docs/`(本地保留,不入库):剪辑流程根因报告、稳定性优化报告、
第三轮优化报告、缺陷修复记录、完善度体检。核心机制的口径速记:

checkpoint 指针行 + 一致点增量链 · DAG 拦截器递归补齐 · 计划门两次 run ·
渲染提交+轮询+尝试令牌+看门狗 · 五级证据分级 · 出口中文单源 · 凭证按身份热读。
