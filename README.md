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
   │  登录鉴权 · 对话主循环 · 计划门 · 技能 · 记忆 │      tools/list         │  25 节点 DAG + 契约            │
   │  checkpoint · 工具注册表 · WS 回投 · 配额    │      tools/call         │  ffmpeg · MoviePy · whisper    │
   │  判断模型(第二意见) · 存储层                  │ ◀────────────────────── │  渲染提交+轮询+看门狗+证据账    │
   └───────────┬───────────────┬────────────────┘      产物/进度回传        └───────────────┬──────────────┘
               ▼               ▼                                                           ▼
     PostgreSQL(全部元数据)  MinIO(媒体字节)  [Redis 可选:跨副本回投广播]      ffmpeg / 本地模型(进程内)
```

- **对外只有一个端口 :8000**——前端 build 后由主服务托管,没有独立前端端口;
- :8001 是后端内部的剪辑工序端口,只监听本机。渲染是分钟级 CPU 重活,独立进程保证它卡死时不影响对话;
- **图形科普片的像素不在 :8001 里烧**:那一侧只是 HTTP 客户端,真正逐帧执行的是第三个进程
  `creation-remotion`(Node 24 + Remotion,:8010,**不做宿主机映射**,只在 compose 网内可达)。
  它没就绪只让这一镜渲失败,不带走整站的剪辑能力——这也是它刻意不进 `depends_on` 的理由(§15);
- **必须先起 :8001 再起 :8000**:主服务在启动阶段建立 MCP 连接并拉取节点清单与 DAG 契约,连不上就是
  "无剪辑能力"(启动日志会明说),而不是运行到一半才失败;
- Redis 是**可选服务**:单副本不带 `--broadcast-redis-url` 是设计口径,不是欠账。
- **可视化选区编辑口只有一条,是自研的那条**:成片卡上的「✂ 选区改」开 `MotionEditor`(§17)
  ——它按成片自己带的命中表切形态:图形科普片露**画面上的框**(`motion-hitmap/1`,
  有空间命中层),素材 / 口播片露**轨道上的段**(`segment-hitmap/1`,`hit_mode="track"`,
  没有空间层);两者都只重烧受影响的那几块像素。第三方剪辑器 OpenChatCut 已全部摘除——服务、
  MCP 注册、以及**前端那条打开外部 iframe 的「🎬 可视化编辑」入口**(2026-10-11 补齐,之前
  按钮仍指向已删除的剪辑器)。「时间线」面板不再内嵌任何外部编辑会话,它只列出已保存的时间线,
  点开是内联的字段表单(字幕 / 配乐等)。

## 目录结构

```
backend/                  后端(Python 3.12+)
  agent_framework/        Agent 框架:主循环 / 工具 / 上下文 / 记忆 / 技能 / checkpoint /
                          团队 / 计划门(plan/) / 判断模型 / WS 与 HTTP 层 / 存储层(storage/)
    plan/                 计划门(12 个模块:gate·vocab·validate·compile·prompt·reconcile·…)
    tools/                内置工具与 MCP 桥(mcp.py 把远程节点包成本地 Tool)
    storage/              PG 仓储 + MinIO 对象存储 + schema.sql(22 张表)
  storyline_server/       MCP 剪辑服务:25 个节点、渲染分发器、看门狗、能力配置
                          (motion/ 子包 = 代码镜链:契约与闸⑦ code_spec、执行层与闸②③④⑤⑥
                           code_render、局部改 code_patch、渲染容器客户端 remotion_client、
                           与像素来源无关的共用件 render)
  prompts/                提示词库(8 份 markdown,改文案不改代码)
  examples/               剪辑服务配置(config.toml / config.docker.toml)+ 6 个示例技能
                          (含 motion_explainer:零素材出片的工作流技能)
  run_server.py           主服务入口(:8000)
  run_storyline.py        剪辑服务入口(:8001)
remotion/                 图形链的代码渲染服务(Node 24 + Remotion,常驻容器 :8010)
                          server.mjs(四张口)· lint.mjs/lint-rules.mjs(闸①)·
                          src/{entry.tsx,FilmLayout.tsx,tokens.ts,user-shots/shot.tsx}
frontend/                 前端(Vue 3 + Vite;构建产物 dist/ 由主服务托管)
deploy/Caddyfile          公网反代(:80 → app:8000,HTTPS 块预留)
docker-compose.yml        postgres + minio + redis(存储层);editor/remotion/app/caddy 走 profile "app"
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
| 18 | **交付之后还有第二条回路**:成片卡带「✂ 选区改」→ 编辑器读这一版自带的命中表 → 零素材片在画面上框出一格、口播片在轨道上点住一段,拿到的都是**字段指针** → `POST /motion/patch` / `POST /timeline/patch` fork 新版本(**只重烧受影响的那几镜 / 那几窗**)→ 新版本又成一张卡 | `MotionEditor.vue` + `server.motion_patch` / `server.timeline_patch` + `nodes.PatchCodeMotionVideoNode` / `nodes.PatchVideoNode`,详见"核心机制 17" |

**出片有两条终点通道**:素材在库里(或随消息给)走 `render_video`;用户只给一个**选题**、没有任何素材,
走 `plan_motion` → `render_motion_code`——画面是模型逐镜交的 React/TSX 代码,由 `creation-remotion`
容器逐帧渲染,详见"核心机制 15"。**一条片子只走一条通道**,
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
  拦截点在 `agent_framework/hooks.py` 的 `QuotaHook.before_iteration`:**每次调 LLM 之前**按身份聚合
  `token_usage` 近 `QUOTA_WINDOW` 秒的 `total_tokens`,到线就抛 `QuotaExceeded` 收尾——注意它拦的是
  「下一条消息」而不是「这一帧像素」,所以被炸掉的那条 run 往往**一个节点都没跑完**,用户看到的是
  「确认了计划,然后什么也没产出」;
- **上限本身按身份热读,三层**(`secrets.resolve_quota`):① 页面「设置 → 配额」写 PG(`app_secrets`
  的 `quota_limit`/`quota_window`)→ ② 环境变量 `QUOTA_LIMIT`/`QUOTA_WINDOW` → ③ 启动参数默认。
  每次 `before_iteration` 现读,**改上限不必重启也不必重烤镜像**。取值是三态的,不是「填个数」:
  请求里**不带这个字段**＝这一项不动;**空串**＝清掉页面覆盖、回落到 env/启动参数;**`"0"`**＝明确关拦截
  (只记账)。这个区分是「页面填 0 压过 env 正数」能成立的前提,所以 `parse_int` 必须把 `None` 与 `0`
  分开——把它们混成一样就等于关掉了页面这一层。窗口下限 60 秒,更小的窗口会让「一次长 run 自己把自己
  拦掉」;
- **超限时先报出路,再报数**:旧文案只有「已用 X,上限 Y」,而那句里的 Y 当时**只能改环境变量再重烤镜像**
  ——用户手里没有任何旋钮,于是唯一能想到的动作就是刷新页面重跑,而账是服务端按身份滑动窗口聚合的,
  刷新、换会话、重开标签页都动不了它一笔。这就是 2026-10-11 那句「怎么刷新后跑还是不行」的全部成因,
  不是前端缓存。现在 `quota_status` 在窗口内按时间升序累计到刚好越过上限的那一笔,给出
  **最早一笔滚出窗口的时刻**(`frees_at`,UTC 落纸 + 折成分钟数 `frees_in_minutes`)与还剩多少,
  `QuotaExceeded` 的文案里明写「刷新页面、换会话、重开标签页都不会让额度回来」+ 何时自然释放 +
  上限来源是哪一层 + 去哪儿改(设置 → 配额)。
- **配额怎么被一次空转烧穿的(2026-10-11 真机,身份 `u-6d962435c873`)**:执行轮开局 Store 是空的,
  模型把 `read_node_history` 当成「查查 Store 里有哪些键」的浏览器用,连着猜了 25 个键名
  (`plan_motion` → `motion_spec` → `plan_motion_spec_draft_v2` …)。那一轮 29 次 LLM 调用挤在 **26 秒**里,
  每次只回 40 个 completion token(就是一个工具调用),而上文每轮从 3.0 万涨到 4.35 万 token——
  **一条 run 烧 121 万**,整个身份 24 小时累计 802.2856 万,越过 800 万上限后连收尾那句都发不出去。
  账目里 97% 是 prompt(777.2 万 / 802.3 万):ReAct 每轮重发全部上文,所以死循环的代价按**平方**长;
- **为什么旧的「连续失败守卫」没拦住,以及现在的修法**:守卫在 `agent.py::_run_tool_round`,阈值
  `_MAX_CONSECUTIVE_FAILURES=4`,数的是 `is_tool_error(返回值)`——而判定口 `ToolRegistry.execute`
  只认**以 `Error` 开头**的字符串。服务端那一路的 `read_node_history` 当时回的是 `{"error": ..., "executed": []}`
  这种 JSON 串,前缀判定看不见它,于是每一次猜空都被记成「工具调用成功」、计数每轮清零,守卫一次都没触发
  (`agent.py:52` 那段注释记录的「规划轮猜六十次」事故,治的是抛异常那条路,这条路是漏的)。
  现在两处 `read_node_history`(主 Agent 侧与服务端 MCP 侧)读不到键时统一回 `Error: ` 前缀的纯文本,
  并把出路写进文案(空列表＝产物还没生成,换键名也换不出来,直接调用产出它的节点);执行轮的
  `<approved_plan>` 承诺段同步补这条纪律。`test_tool_failure_guard` ⑧ 钉住正例(第 5 次起不再真打)
  与负例(**回 JSON 错误体时守卫全盲,六次全真打**)——所以将来任何新控制面工具想被守卫计入,
  回执必须走同一个 `Error: ` 前缀约定,这不是可选风格而是守卫的接口。

### 14. 回投与多副本

OutBound 帧先给本进程登记的 WS 连接;`--broadcast-redis-url` 打开后走 Redis 频道 `outbound:broadcast`,
各实例只投自己挂着连接的会话。**没挂 WS 的会话帧也进有界影子缓冲**,刷新/换副本后才看得见在途进度;
`answer` 帧落定即清该 run 的缓冲,重放只补在途轮次。MQ 按会话哈希分区:同会话串行、不同会话并行。

### 15. 零素材图形科普片:画面是模型逐镜写的代码,八道闸把它拦回可放映

用户只给一个**选题**、没有任何视频素材时走这条通道(`plan_motion` → `render_motion_code`,
局部改走 `patch_motion_code`)。判据只有一条:这条片子的画面排版画不出来,得由代码画。给素材请走剪辑 DAG,
一条片子只出一个终点通道。旧的声明式链(22 张内置卡 + 参数展开 + SVG 净化 + 虚拟时钟截图)
**已整条删除**,`motion/spec.py`/`art.py`/`diagrams.py`/`canvas.py`/`graphics.py`/`templates.py` 都不在了;
它那条验收要求——每镜要有能认出的画面主体、整片版式要有配额、一处以上先后动效——与实现无关,
现在由**闸④ + 闸⑦**接着判,依然成立。

- **两个工具、两次决定**:`plan_motion` **不烧像素**——它校验、归一、把 spec 落进 Store,并回一份账
  (镜头数、总字数、按字数估的秒数区间、与目标时长的偏离提醒)。带病的 spec 不入库,所以永远不会
  「渲了二十分钟才发现高亮词写错了」;改文案只需重调本工具。`render_motion_code` 是唯一烧像素的口子;
- **一镜一段代码,渲染在一旁的女仆容器里**:每镜交一段 React/TSX(`code` ≤ 40000 字节),
  由常驻服务 `creation-remotion`(:8010)逐帧渲。它只开四个口——`/healthz`(报 node/remotion/chromium/gl,
  以及**母版哈希与依赖锁哈希**)、`/lint`(闸①)、`/render-shot`(渲一镜,服务端**串行**,一次一镜进浏览器)、
  `/artifact/{cache_key}`(取切片字节)。**这条链没有默认版式可退**:哪一镜没交代码,分镜校验就带镜号退回。
  切片先由 Python 抓到本地再进缓存与拼接,因为闸③ 数的是**本地那份字节**,而混音在本地跑;
  缓存对象落 MinIO `motion-shots/{会话}/{镜头键}/{文件}`,随会话级联回收;
- **版式归母版 `<FilmLayout>`,模型只画带子里那一块**:母版 Composition 垄断字幕、画幅、字体与配色 token,
  镜头代码**必须**包一层 `<FilmLayout>`(闸① 的强制规则之一),画面只能画在 `useBody()` 报回来的那条带子里,
  坐标系已是设备像素——早先那批 12 镜每张自己猜 `1080×620`,横屏母题被压成整页的 9%,就是没有这条垄断的代价。
  字幕容量随画幅分档,并由契约拦:竖屏/方屏 60 字(`MAX_TEXT_CHARS`)、横屏 110 字(`MAX_TEXT_CHARS_WIDE`);
- **一帧 = `useCurrentFrame()` 的纯函数**:禁 `requestAnimationFrame`/`setTimeout`/`Date.now`(闸① 拦),
  随机只许 `random(seed)`。理由是真机测出来的:headless Chrome 在 `--virtual-time-budget` 下只发得出
  1~2 个 rAF 回调就饿死,挂在 rAF 上的动画会让每镜都截成起始帧;SMIL(`<animate>`)按墙钟走,同理渲不动;
- **镜头时长不由模型给**:有旁白时时钟归 edge-tts 的**实测语音时长 + 词级时间轴**(话没说完画面不切走),
  模型写的只降级成 `min_duration_sec` 下限参考。字幕逐词点亮用的就是这份词时间轴,
  **字幕与逐词高亮是母版的事**,镜头代码不参与;
- **八道闸,阈值都来自真机读数**（最后一道只记账不拦截）:

  | 闸 | 判什么 | 判据 / 阈值 |
  | --- | --- | --- |
  | ① lint | 代码字节 | 容器里 7 条 `dg/*` 规则:禁墙钟、禁裸 DOM、import 白名单 `ALLOWED_IMPORTS`、必须包 `<FilmLayout>`、字段声明与代码读值对账。改代码**必重过**,只改字段值不重跑 |
  | ② 帧预算 | 烧多少帧 | 全片 `MAX_FRAMES_TOTAL = 4000` 硬报错不静默砍,只累加**未命中缓存**的那几镜 |
  | ③ 帧账回读 | 计划帧数 vs 实际 | 本地 ffprobe 数字节:>1 帧差或宽高/fps 对不上就点名那一镜 |
  | ④ 画面两半 | 画了没、画开没 | 三帧取样(0.15 / 0.5 / 0.85)只在**画面带**内数非底色像素(字幕盒约占整幅 11%,量全帧等于给空画面发通行证)。空壳:最低占比 <2% **且** 内容外接盒铺开 <45%;小气:铺开 <30% **占比够也拦**;60% 是技能正文的目标值,只记账不打回 |
  | ⑤ 跨镜一致性 | 母版回读 token | 逐键比,**容差 0**;判在 `concat` **之前**,红一次省掉母带那一趟编码 |
  | ⑥ 字段对账 | 注入值 vs 画面自报 | 母版在 `durationInFrames - PROBE_TAIL`(=2)那一帧回读 `<F>` 盒,状态 `ok`/`no_geometry`/`unreported`/`mismatch` |
  | ⑦ 风格配额 | 整片画风分布 | **判在烧像素之前**,判据是代码字节而不是模型自报的 `style`(自报会漂:写了 `style: rough` 而代码里一个 roughjs 都没有)。roughjs / three / katex 三条按 import 认,vector 按「代码里真在画几何图形」的标记认(裸 `<svg>`/`<path>` 算,`<div>` 加 `background` 的色块不算),纯文字色块是第五类 `text`。配额:≥6 镜至少 2 条非文字通道、≥10 镜至少 3 条;不足 6 镜整道关闭且显示「未判」而不是「通过」;纯文案镜占**中间镜**(去掉首尾)>1/3 拦,具体项占比 <40% 只提醒——那是需要分词的软判据,做硬闸等于把「什么算具体」写进代码 |
  | ⑧ 版面回读 | 探针帧上的真 DOM 盒 | **八道里唯一只记账不拦截**:判据是几何猜的,刻意叠印、指向画面外的暗示都可能是合法设计,误报的代价是模型与用户开始无视整条通道。三条:两处文字盒交叠 ≥ 较小盒 **40%**、文字越出画面带任一边 ≥ **3px**、引线的某一端在 **max(带短边×2%, 20px)** 内既不挨字也不挨画得出的图元。坐标一律**带内**(与 `useBody()` 同系,`<g transform>` 里的线跟着 `getScreenCTM` 走)。**三态必须分开**:`missing`(容器压根没回传版面行=这一镜的版面等于没验过)/`unscanned`(收到行但 ref 没挂上,这一帧没扫)/`scanned`(真扫了,空表才是「扫过且干净」);阈值随读数一起回传,单源在 `remotion/src/tokens.ts` 的 `LAYOUT_PROBE`,Python 侧不抄常量表 |

  出处闸不占编号:它判在分镜层(见 §16),局部改时只重判**被改过的那几镜**。
- **起手模板 + 四条画法通道**:设计文档原列的六份「版式骨架」没做,换成了两样东西——
  `remotion/src/user-shots/shot.tsx`(每次渲染前覆写的落点,同时是形状参考:强制包 `<FilmLayout>`、
  坐标取 `useBody()`、时刻只从 `useCurrentFrame()` 拿、随机用 `random(seed)`)与技能正文点名的四条通道
  (手绘 roughjs / 真 3D three / 公式 katex / 纯 SVG 图示)。这四条通道名与闸⑦ 的 import 表**逐行同源**
  (`tests/test_code_spec.py` 第⑦节拿正文比对),所以教程与判据不可能各说一套;
- **字段通道 = 画面上的旋钮**:一镜最多 12 个字段(`number`/`color`/`text`/`bool`/`select` 五种),
  代码里读 `fields.xxx`、渲染点用 `<F>` 标注,闸⑥ 才比得上、命中表才给得出 `/shots/i/fields/<name>/value` 指针。
  改一个数字只重烧那一镜——这条链的卖点就在这;
- **缓存键按字节算,五段身份缺一不可**:`sha256(代码字节 + 该镜 `SHOT_KNOBS`(含字段值扁表) + 整片 `FILM_KNOBS` +
  母版哈希 + 依赖锁哈希)[:16]`。后两段从 `/healthz` **现读、不在 Python 侧重算**——改一次母版 token 或换一版
  依赖＝全片换像素,不进键就会命中旧母版渲出来的切片,而改动账和逐镜比较全都看不出来(与 `44`/`44.0` 那次键漂移
  同族,所以按字节算的键要按字节断言)。命中表**跟着切片走**:切片没重烧就没有新命中表可给;
- **质感是逐镜的,而且故意在缓存键之外**:档位名单在 `code_spec.TEXTURES`
  (`none`/`film` 颗粒暗角暖偏 /`tv` 扫描线色偏 /`glow` 柔光泛开 /`bleach` 漂白硬对比),滤镜串单源在
  `render.TEXTURE_FILTERS`,「这一镜到底跑哪档」只由 `code_spec.shot_texture(shot, film)` 一处回答。
  一镜的 `texture` 有三种形态:**缺省或空串**跟随整片默认、`"none"` 明确不加、其余是点名换档——
  `"none"` 与 `""` 是两件事,界面上也是两个格子(要撤回点名得写 `""`,指针做不了"删掉这一栏")。
  质感**不进 `SHOT_KNOBS`/`FILM_KNOBS`**——进了它,用户选一次「老胶片」就等于 20 镜全部重烧;
  **缓存里存的是加质感之前的像素**,所以命中切片的镜照样被加上新档。代价说清:这一趟是每一镜多一次
  ffmpeg 编码(concat 本来就把每帧重编一遍),`glow` 那一路带 `gblur`+`blend` 最贵。账上把**整片默认档**、
  **每镜实跑档**与**实际消费的滤镜串**分开记(`texture` / `texture_by_shot` / `texture_filter`,
  后两样是运行时事实,只活在账上、不在缓存对象里),所以「选了 film 却原样拷贝」这类失败看得见,
  不会退化成「跑了但没人知道」;
- **七个开关走顶层入参**(aspect / fps / narration / voice / rate / subtitle_mode / texture):嵌在 spec 里的字段对
  计划卡不存在,卡上勾选的值**覆盖**模型写在 spec 里的同名字段——那是用户的决定;`narration` 的终值是字符串
  `"false"`,所以合并时用 None 哨兵判"有没有传"并按文本解析,否则会把开关读反;
- **配乐两种写法都收**:`bgm`(与 `spec.bgm.ref`)给 `obj:` 引用,或直接给**消息附件/曲库歌曲的
  material_id**——零素材链路里模型手上只有 material_id,而唯一产 `obj:` 引用的 `select_BGM` 绑在
  有素材那条链上(`required_nodes=generate_script`),调它会连带补齐 ASR 一串上游。取不到字节直接
  失败,不会静默出成无声片;不传就是纯人声。曲库条目**不挂会话**(`materials.conv_id` NULL),
  归属只认 owner——`resolve` 里曾按 `origin=bgm` 无条件放行,那等于任何人拿 6 位十六进制 id
  就能把别人的歌当自己附件解析并签出可播直链;后门已拆;
- **`source` 如实填**:机器不判史实真伪。出处会如实出现在**分镜卡与逐镜账**上,但**不会自动排进画面**
  (要进画面得自己画进那一镜的代码里);成片回执的五级证据账本里「音画偏差/帧复用/降级/质感」是机器验过的,
  「画面在动、无黑屏」标 UNVERIFIED 交人眼——闸④ 量的是取样那三帧,不是整片;
- **空画面不许出片**:这条链没有 `visual` 可校验,判据改成量像素(闸④)。当年卡片链上「`visual` 写了这张卡
  不认的键 → `ok=True` 的空画面」那类缺陷(#102),现在由「三帧取样、带内数像素」接管:静默放过等于把缺陷
  当产物交出去,所以**闸失败是失败,不是降级**,一律抛错带镜号回清单;
- **成本口径**:帧数仍是唯一的成本项(`MAX_FRAMES_TOTAL = 4000`),但这一层没有 `settle_ms` 那种
  「之后的帧复用末帧 PNG」的省法——Remotion 按注入的 `durationInFrames` 逐帧渲,所以动效做不做都在花这笔钱。
  真机测到的固定成本是**每次请求打包 4.6~7.5 秒**(bundle 与镜头内容无关地贵),逐帧开销由账上的
  `per_frame_ms_max` 报出来;软件 WebGL 只有 `DG_GL=swangle` 出得来画面,其余后端在容器里渲成黑帧;
- **确定性 = 约定 + 实测**:同输入同图靠闸① 禁掉墙钟与 `Math.random`、要求 `random(seed)`,再加缓存键按字节算。
  这不是运行时保证(浏览器与依赖版本会变),所以母版哈希与依赖锁进键、跨镜一致性按容差 0 比——
  版本一漂移就有镜对不上,而不是悄悄出两片不一样的。

代码位:契约与闸⑦ `motion/code_spec.py`、执行层与闸②③④⑤⑥ `motion/code_render.py`、
局部改 `motion/code_patch.py`、容器客户端 `motion/remotion_client.py`、命中表 `motion/hitmap.py`、
与像素来源无关的共用件(质感滤镜串/混音/闪避) `motion/render.py`;
节点 `nodes/motion_plan.py` / `nodes/motion_nodes.py` / `nodes/motion_code_nodes.py`;
渲染服务 `remotion/server.mjs`、闸① 规则 `remotion/lint-rules.mjs`、母版
`remotion/src/{FilmLayout.tsx,tokens.ts,entry.tsx}`、落点模板 `remotion/src/user-shots/shot.tsx`;
技能 `examples/skills/motion_explainer/SKILL.md`;离线用例
`tests/test_code_{spec,gates,fields,texture}.py` + 两条链共用的 HTTP 口 `tests/test_patch_endpoints.py`;
样片冒烟
`.smoke/b22_code_chain_render_smoke.py`(整链出片)与 `.smoke/b24_code_ui_smoke.py`(界面),
旧声明式链的夹具归档在 `.smoke/_archived_p4_declarative/`。

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
- **`fetch_url` 抓回来的字节先按 `Content-Encoding` 解压**(`tools/web.py` 的 `_decode_body`):
  urllib **不像 requests 那样替你解码**,而 B 站这类 CDN 无视 `Accept-Encoding: identity` 照发 gzip——
  2026-10-11 真机就是撞在这上面:页格里是一串乱码,紧接着一条「PG 执行失败:unsupported Unicode escape
  sequence」。两条同一个根因:压缩流里有 0x00 字节,`decode(errors="replace")` 把它原样留成 U+0000,
  回喂文本进 jsonb 列时 `json.dumps` 转成 `\u0000`,PG 拒收(`\u0000 cannot be converted to text`)。
  现在请求显式声明只收 `gzip, deflate`(br 没有解码器就不给服务端发的理由),`max_bytes` 按**解压后**的
  正文计,工具出口再统一剔掉控制字符(`\t\n\r` 除外)——外部字节进数据库之前的这一刀落在抓取这一层,
  不指望下游各自防;
- **解压只治了乱码的一半,另一半是字符集**(`tools/web.py` 的 `_decode_text`):中文站今天仍在发
  GBK/GB18030 页,而旧代码无条件 `decode("utf-8", errors="replace")`。这一类比 gzip 更阴——解出来的碎片
  (`浣撹偛` 这种看着像日文的字)**不含 0x00**,PG 照收、`json.dumps` 照转,错误一路无声穿到模型面前,
  直到人眼看页面才发现。现在按两级声明取编码:HTTP 头里的 `charset=` 优先,头里没写就认正文 `<meta>`
  里的声明;`gb2312`/`gbk`/`windows-936` 一律升一档到超集 `gb18030`(老页混简体扩展字不会解不开);
  编码名认不出、或按声明解不开时退回 UTF-8 兜底而不是抛异常——**抓取宁可回得难看,也不因编码问题整条失败**。
  声明 `utf-8` 时与旧行为逐字相同,这条改动只对「声明了非 UTF-8」的页面生效;
- **抓取的回喂分三类,不共用一句「失败」**:HTTP 403/429 这类错误页当正文取回来再判,回「这一页被挡在
  门外(反爬/限流),不代表网上没有这条内容」;正文抽不到字但页面是 JS 渲染的壳(YouTube -watch 这类),
  退回 `<title>`/`<meta>` 并明说是壳;两者都不是「这页没有内容」——只有 status 与正文都支持时才那么回。
  文案判据(如 `captcha` 字样)只在短页面上生效,长正文里出现这个词是内容本身;
- **检索账与出处闸**(`agent_framework/retrieval.py` + `nodes/motion_plan.py`):`web_search` 命中的链接与
  `fetch_url` 真打开过的页面按会话记进 PG `retrieval_hits`(带 backend 与时间)。分镜 `source` 里的 URL
  **只有能在这笔账里反查到才算查证过**,对不上就被出处闸退回——编一个看着像真的链接骗得过排版,骗不过账本;
  没查到的就标「未核实」,不许在答复里替它背书。

代码位:后端与链 `backend/agent_framework/tools/web.py`,按身份热读 `backend/agent_framework/secrets.py`,
记账与查询 `backend/agent_framework/retrieval.py`,设置页读写 `backend/agent_framework/server.py`
(`/settings/search` 写、`/settings/search/test` 让页面当场拿一条查询试这路后端通不通),前端在「设置」页的检索后端区块。

- **订正(2026-10-11,这一节先前是超前于代码写的)**:上面那句里的 `POST /settings/search`、
  `POST /settings/search/test` 和「设置」页的检索后端区块,**在这批之前一个都不存在**——此前只有
  `GET /settings` 的读侧把这三项掩码回显出来,页面上没有能填它们的框,后端也没有写入口,
  配置检索后端只能改环境变量再重烤镜像。文档描述的是设计意图而不是当时的实现,读者按它找不到入口。
  现在两处口与区块都落地了,校验口径与工具侧同源:provider 走 `KEYED_SEARCH_PROVIDERS` 白名单、
  自建地址必须带协议、密钥含空白拒收、**给了 key 或地址却没选后端直接 400**——`SearchTool._runner`
  本来就拒绝猜服务商,口子上若猜了就是两处口径打架。`/settings/search/test` 同样按
  `resolve_search_config` 那一层装配、不自创优先级:没配后端时自检的是免 key 回落链并在回执里明说;
  0 条结果算「连通」(key 与地址都对,只是这条查询没命中)而不算失败。

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
- **它是右侧停靠栏,不是整屏弹窗**:`App.vue` 把它挂在 `.shell` 的最后一列(`.editor-dock`,占壳宽 58%),
  会话区 `flex:1` 自动让宽——剪片时用户还要看得见上下文与进度,盖一层遮罩等于把刚说过的话藏起来。
  打开时顺手收起左侧卷宗栏(正在剪片子的人不需要会话历史列表),关掉再还原成**打开前**的状态,
  且不动 `ca.railOpen`(那是用户自己设的偏好,不该被一次剪辑改掉)。
  「渲染完成自动弹出」和「点卡片上的✂ 选区改」走同一个 `openEditorFor()`,两条路同一形态;
  面板实宽 <900px 时容器查询把右侧 400px 表单挪到画面下方,而不是把画面挤没;
- **改动拼成一次补丁,四种动作**(`POST /motion/patch`,代码镜那条的校验在 `motion/code_patch.py`):
  ①`edits` 逐格指针改写(框选的主场) ②`shot_sets` 整镜栏位改写(前端 `setFor` 逐栏与原值比 JSON,
  没变的一栏不进补丁) ③`remove_shots` 删镜 ④`reorder` 换序。**空补丁直接拒**;
  可改的镜内字段是白名单 `EDITABLE_SHOT_FIELDS` 那七项(`code` / `text` / `highlight` / `fields` /
  `min_duration_sec` / `texture` / `source`),**`id` 不许改**——它是命中表、缓存键与
  时间线三处共用的锚;
- **重烧的账按缓存键算**:`shot_cache_key` 哈希**代码字节** + 该镜 `SHOT_KNOBS`(`text`/`highlight`/
  `min_duration_sec`/字段值扁表) + 整片 `FILM_KNOBS`(画幅/fps/旁白开关/配音/语速/字幕模式) +
  **母版哈希** + **依赖锁哈希**。`voice`/`rate` 这类只在有旁白时才起作用的开关
  **也一律计入**——不玩「这次没旁白就可以不算」的条件哈希,少算一个键就是拿旧片冒充新片。
  质感与配乐**故意不在名单里**(见 §15):换档、换 BGM 都一镜不重烧。
  所以改 `min_duration_sec` 会重烧那一镜,改文案会,而**删镜与换序零像素成本**——它们只是把已经烤好的
  切片重新拼一遍。顶部改动账就照这个口径报(「未提交:逐格 1 处 · 预计重烧 1 镜」/「重排不烧像素(拼而已)」);
- **时长那一栏有地板**:`sec = max(min_duration_sec, 实测语音时长)`。有旁白时时钟归配音,
  拖到 `speech_sec` 以下**不会有任何效果**,轨道块当场写明「拖到 X.Xs 以下无效:这一镜的时钟归配音」,
  而不是让用户拖完再奇怪为什么片子没变;
- **判据在轮询端**:局部改是长任务,闸门退回(比如字段值写出该字段声明的范围、或改代码后没过闸①)也回一路 200 +
  `status=queued`,真判决在之后 `GET /render_status` 的 `error` 正文里——前端把那句话**原样贴出来**,
  不替它解释;等超时也不谎称取消:提示「这次局部改还在跑,关掉这里不会取消它」。
- **改前/改后靠代表帧**:补丁视图带回 `before_frames`(基线那一版各镜代表帧的对象键),
  编辑器分别向 `/motion/frame` 要旧版和新版的字节,**按镜分列**贴出来(一镜一列,列内改前在上、
  改后在下——只改一镜时看到的就是上下两张,不是左右一对)。**帧字节走 fetch + Bearer 再转 blob**,
  因为 `<img>` 的 src 挂不上凭证。

代码位——**图形科普片那条**：命中表 `backend/storyline_server/motion/hitmap.py`、切片缓存与代表帧
`backend/storyline_server/nodes/motion_nodes.py`(缓存键 `motion/code_spec.py::shot_cache_key`,
对象前缀 `motion-shots/{会话}/{镜头键}/`)、补丁校验与指针白名单 `motion/code_patch.py`,
渲染与八道闸 `motion/code_render.py`;
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
图形链这半边的界面验收**两跑都在代码链的真实页面上**(旧声明式链那一档随 P4 删链一起归档在
`.smoke/_archived_p4_declarative/`,它不再替现值背书):
- **模型端到端那一跑** `.smoke/s3_ui_cdp.mjs`(本机 Chrome headless 经 CDP 驱动真实页面):注册一次性身份 →
  挂一把可用的模型密钥(只报 uid,值不打印)→ 提需求 → 逐页点规划卡 → 等模型自己写出三镜代码分镜 → 成片卡 →
  **鼠标按住拖出框选** → 改值 → 提交 → 轮询到新版本 → 看改前/改后帧 → 「接着改这一版」换基线 →
  到〈文案 · 出处〉页改写整镜文案 → 再采纳 → **故意加一个不含在文案里的高亮词**(闸门原话逐字贴在面板上,
  一帧都没开始渲,也没多出一个版本)→ 「清空改动」→ 拖块换序(只记顺序账,不烧像素)→ 关闭。
  最新一轮 `.smoke/s3_ui_run12.log`:**45 PASS / 0 FAIL**(身份 `u-ebbfe7487e9a`,两版 `4ff5a567`(改一个字段,
  只重烧 s01)与 `d0b31a35`(整镜改写,基于 `4ff5a567`),退回原话 `s01：高亮词 '绝无此词验收' 不是文案的子串,
  画面上永远亮不起来`)。规划轮要十几二十分钟,所以脚本留着 `ATTACH=1` 的**接回模式**:不重开浏览器,不注册新身份,
  连到端口上那个还活着的页面(token 在 profile 里)。接回时**页面上已经躺着成片卡就不点任何确认**——
  那张待确认卡点下去等于白烧一遍像素(真机踩过两次)。
- **夹具那一跑** `.smoke/s5_code_ui_cdp.mjs`(不要模型密钥,出片由容器里的夹具驱动):先把一条代码镜片子跑到终态
  并按浏览器登录的那个身份入库,再到真实页面上量〈字段〉〈代码〉〈文案 · 出处〉〈质感〉〈配乐〉五个页签与
  「点元素反查字段」,含**按控件形态落笔**(带 min/max/step 的 number→滑杆,color→取色块 + 那条 hex 文本框,
  bool→开关,select→下拉,text→单行),`<F>` 标在 `display:none` 上→表里 `box: null` 且前端把它列进「无空间命中」,
  把代码读的字段名改错→闸① 原话贴回面板且**红的那一趟不多出版本**,拖 `.me-edge` 改时长,配乐页音量滑杆按合同 0~0.6。
  最新一轮 `.smoke/s5_run4.log`:**91 PASS / 0 FAIL**(身份 `u-564fe29890d2`,产物 `art-s5l97f5v`,
  跑在母版哈希 `74325dff8d3184bc` 上——即下面那条字幕修复之后的母版)。

这两跑之外,**真去看了渲染出来的帧**,查出两条与界面无关的画质账,都已落地并留了红→绿取证:
- **高亮词被断行撕开**:母版 `Caption` 逐段渲染时把命中的词组折了行,粉色色块只落在前半截
  (红证 `.smoke/caption-old2.png`,绿证 `.smoke/caption-new.png`,探针 `remotion/tests/caption-wrap-probe.mjs`)。
  改成命中的词组逐段 `whiteSpace: nowrap`。母版字节变了 → `shot_cache_key` 里的 `master_hash`
  从 `f70a1c13969ac71f` 变成 `74325dff8d3184bc`,**于是整片重烧,这是设计如此,不是回归**。
- **跨量级读数丢了刻度纪律**:P4 删旧链时把「数值差到两个数量级必须换对数刻度」那条连同旧卡一起删了,
  模型自己写代码后又画出线性柱状(5 克与 10 亿吨在同一根轴上,小的一根看不见)。这条纪律现在写在技能正文
  `examples/skills/motion_explainer/SKILL.md`「# 数值画上画面:跨量级必须换对数刻度」一节,运行时以 PG 里那份为准
  (`POST /skills/reload` 同步;已核过库内正文与文件正文只差加载器的 `.strip()`)。
  **它是提示词层的纪律,不是闸**:现值仍然是模型可以交出线性轴,闸④ 只卡「内容铺开占带短边多少」,不认刻度。

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
③ 零素材那条的表**只有两种条目**:整镜一条(`box` = 整画布,指针 `/shots/<i>/code`)+ 这一镜
**声明出来的每个字段**一条。字段的盒是母版在闸⑥ 那趟浏览器回读里量 `[data-dg-field]` 元素顺手得到的
(`code_render._shot_table`),量不到几何的字段**照样出一条**(`box=null` + `field_status` 记着为什么),
前端把它们落进「无空间命中」栏——点不中但要能改是合法形态。所以能不能框中选中,取决于
「模型有没有把这个旋钮声明成 `fields` 并用 `<F>` 标注挂在一个有尺寸的元素上」,**不是排版落点**;
代码里自己画的、没声明成字段的图形与文字在表里**没有条目**,只能走整镜那条指针改代码;
④ 口播那条的表**带着框但不画框**:字幕条的框是按排版公式**估**出来的(`timeline_edit.subtitle_box`,
`box_precision="estimated"`),其余轨道给整画布框(`derived`)——这张框只用来"万一以后要按空间点"时
标明自己有多可信,当前 `hit_mode="track"` 下前端不画空间层,字段指针直接从 `doc` 取,与框无关;
⑤ 画面上改了数字不会改旁白念的话——文案 `text` 与画面 `code`（含 `fields` 的值）是两处,
只改画面那一处时这一镜会连配音一起重合成一遍(`code_render.narration_for`,缓存未命中就不复用音频),
但念的仍是原句、这一镜的秒数也照原句算。这是选区改的语义,不是 bug,界面上两栏分开摆着。

### 18. 成片入库 + 按会话回收:删对话清的是工作区,不是用户已经拥有的东西

两件事是同一条约束的两半:**烧出来的那版片子必须能在库里长久找到**,**它的对话删掉时其余产物要跟着走**。
过去两头都缺——渲染终态只写 `render_jobs`/`artifacts`(都只挂 `session_id`),而
`DELETE /convs/{id}` 只删 `messages` + `conversations` 两行,于是每条被删的对话都在库和桶里留下
永远没人再来清的账。

- **入库落在渲染终态本身**(`agent_framework/render_library.py::register_render`),不在前端:
  只有出片那一侧知道「这一版字节确实烧出来了」,而登记晚一步就可能赶在删对话之后,把这版片子
  当会话垃圾清掉。两个落点:口播链 `nodes/core_nodes.py::RenderVideoNode.process`(2528 前后)与
  图形科普片链 `nodes/motion_code_nodes.py::_render`(214 前后);两条「选区改」局部改链复用各自的出片体
  (图形那条的 `PatchCodeMotionVideoNode` 直接继承出片节点,走的就是同一个 `_render`),
  对象键不变 → 按 `object_key` 命中同一行,不重复登记。`dry_run` 在写终态之前就 return,不入库。
  素材行 `origin='render'`(`materials.origin` 枚举扩了一格,schema 与 `migrations.sql` 同步),
  前端素材卡按 origin 显示「成片 / 上传 / 链接取料 / 曲库 / 配乐」;展示名取本次标题
  (路径分隔符与控制字符换成 `_`,截 80 字符)。
  **入库失败不改判这次渲染**:片子已发布、终态行已 done,素材库少一行只是少一个「以后还能在库里
  找到它」的入口——所以回一句说明,由调用方并进 `notes` 对用户明说,不静默吞。
- **回收按会话,但两种拼法都得清**:`artifacts`/`render_jobs` 与对象键用剪辑侧的
  `u:{user}:c:{conv}`,`checkpoints`/`token_usage` 用 Agent 侧的 `{user}:{conv}`——
  只按一种删就是永远只清掉一半账。`identity.session_forms()` 把一个会话展开成它的两副面孔,
  `session_gc.purge_session()` 据此删四张表的行,并顺带清 `checkpoint_entries`(按先取出的
  `run_id`)、`tasks`/`task_edges`/`subagents`(按 `scope`),再按前缀收桶里的字节:
  `renders/{会话}/`、`derived/{会话}/`、`motion-shots/{会话}/`、`render-windows/{会话}/`
  (前缀一律带尾斜杠,否则 `sess-1` 会误配 `sess-12`)与 `uploads/{用户}/convs/{对话}/` 的分片。
  **素材字节的 `users/{用户}/convs/{对话}/` 根本不在名单里**;名单之内还有一层保护名单:
  `materials.object_key` 指向的字节一个都不删——所以「删对话」清掉的是工作区,不是用户已有的东西。
  回收回一份账(删了几行、几个对象、护住几个),接口把它带回响应,前端在「这册已删」之外
  多吐一句说明。
- **在途渲染优先于回收**:`render_jobs` 里有 `queued`/`running` 的行时整体跳过并说明原因——
  此刻删字节会让那次渲染跑完时只剩一张空账。跳过不是放弃:后台对账稍后会再扫一次。
- **两处调用共用这一份口径**(`agent_framework/session_gc.py`):删对话时即时回收
  (`server.py::conv_drop` 删两行之后 `purge_conversation`);以及对账扫描——启动先扫一次、
  之后按 `--upload-sweep-sec` 周期再扫(和上传分片回收同一个循环 `_reap_garbage`)。
  对账兜两种崩法:库里有行但对话已不在(`orphan_sessions`,两种拼法各算一条,回收时按拼法集合去重),
  以及**字节落了桶、行却没落**的崩溃残留(`orphan_scopes`——成片先 publish、`artifacts` 行随后才写,
  中间进程没了就只剩字节)。只剩已入库字节的作用域**不算孤儿**:那版字节归素材行管,
  否则每次开机都重扫一个永远删不动的作用域,对账就不幂等了。

**边界(不粉)**:① 回收的触发点是「删对话」和「开机/周期对账」,**没有 TTL**——对话不删,
产物与切片就一直涨(`render-windows/*`、`motion-shots/*` 的保留期仍未定,见 §17 边界②);
② `timelines` 行的 `conv_id` 是 `ON DELETE SET NULL`,删对话后那一行仍长存(它按 `material_id`
归属,不归本回收口径);③ 在途渲染期间删对话,`register_render` 会因对话行已删而失败,
那一版成片**不入库**(只回说明,渲染本身仍算成功)——想留住它得等渲染收口后再删;
④ 历史 `tasks`/`subagents` 里 `scope-xxxxxxxx` 那种对不回身份的形态不在回收范围内;
⑤ 回收发生在容器/卷内部,**Docker 的 vhdx 不会自动缩小**——库里清了,宿主机磁盘文件不回落;
⑥ `scripts/purge_identities.py` 按 `users/{用户}/` 前缀收字节,而渲染成片在 `renders/{会话}/` 下——
删身份时那版成片字节**不归它删**(实测报「对象 0 个」),要等下一轮对账:身份删掉后 `materials`
行随级联消失、保护名单不再护它,`orphan_scopes` 就把它当无主字节收掉。

**真机回执**(`.smoke/_archived_p4_declarative/b21_render_persist_gc_smoke.py`,真 PG + 真 MinIO,27 条断言全过):
零素材分镜走**当时那条声明式链**的 `render_motion_video` 出 1080×1920 / 8.865s / 138,726B 的一版,
回执带 `material_id`、库里 `origin='render'`、字节与 sha256 取自对象本身;
(入库与级联回收这一层**不认像素来源**——它只认 `renders/{会话}/` 的对象键与 `artifacts` 行,
所以那份回执对代码镜链同样成立;但**代码链上的端到端复验还没跑**,与 §15 那条「真机样片」同批补。)
改标题重烧 → 同一 `object_key` 仍恰一行、id 不变、展示名刷新;`DELETE /convs/{cid}` 之后
`artifacts`/`render_jobs`/`checkpoints`/`token_usage` 两副拼法都清零、`checkpoint_entries` 随 run 清、
该会话前缀 11 个字节回收、**素材行留下且 `conv_id` 被真库置成 NULL、成片字节仍可 `get_stream` 读回并通过 ffprobe**。
开机对账实测:埋一行孤儿 `render_jobs` 与一个只有字节的 `renders/{无主会话}/` 再重启,
日志回「孤儿会话对账:回收 2 个作用域(删行 1 条、删对象 1 个…)」,复查 `orphan_scopes()` /
`orphan_sessions()` 都是空集合(对账幂等,不会每次开机重扫同一坨)。
界面这一格另有实测:素材库面板出现「🎬 中子星·入库可见.mp4 · 135KB · 9s · **成片**」,
`<video>` 从预签名地址真播到 2.3s(readyState 4、无 error)。

## 剪辑节点(25 个 DAG 节点)

`available_nodes` 白名单里是 **25 个 DAG 节点**(`core_nodes.py` 的 21 个 + `web_nodes.py` 的 `render_web`
+ `motion_plan.py`/`motion_code_nodes.py` 的 `plan_motion`、`render_motion_code`、`patch_motion_code`)
**+ 2 个控制面工具**(`read_node_history`、`render_status`)—— 主服务那行启动日志报的
「已接入 27 个剪辑节点」就是这 27 个。剪辑服务自己那行 `[storyline] nodes=… tools=…` 是另一个口径:
`nodes` 数**注册表里的节点类**(25),`tools` 数 MCP 口上挂的 handler(28 = 25 节点 + 两个控制面 +
`dag_contract` 编排预览口,最后这个没有包成模型工具)。★ = 必须显式调用(不会被自动补齐)。
2026-10-11 重烤镜像并 recreate 之后,两台服务的启动日志实测是:剪辑服务 `[storyline] nodes=25 tools=28`,
主服务 `[startup] Storyline 已接入 27 个剪辑节点（DAG 契约 25 节点，rerun_from 可用）`。
这里曾经有一个真缺陷:P4 删旧链之后有一段时间 `creation-editor` 跑的仍是旧镜像(报 `nodes=27 tools=30`,
注册表里躺着已下线的 `render_motion_video` / `patch_motion_video`),白名单不含它们所以模型看不见,
但读注册表的一切判据都对不上——镜像与工作树不同步时,以哪一版为准必须有日志作证,不能靠 README 的一句话。

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
| `render_motion_code` | 图形科普片出片（代码镜） | 画面是模型逐镜交的 React/TSX,交给 `creation-remotion` 容器逐帧渲,出片前过八道闸,其中闸⑧ 只记账不拦截(长耗时,提交后由 `render_status` 续问;支持顶层七开关覆盖) |
| `patch_motion_code` | 图形科普片局部改（代码镜） | 选区改的后端:fork 基线 spec → 按命中表指针改几格(五条指针) → **只重烧受影响的那几镜**(其余从切片缓存直接取),`POST /motion/patch` 提交的就是它。改 `code` 才重过闸①,改字段值/质感档不重过 |
| `read_node_history` | 读取节点产物 | 控制面(非 DAG 节点):按节点名读该节点已产出的 Store 结果 |
| `render_status` | 渲染进度查询 | 控制面:轮询出片与局部改提交的任务(`render_video` / `render_motion_code` / `patch_motion_code` / `patch_video`),回 `status/stage/percent`,`done` 时同批回成片。**选区改的判决也走这里**——闸门退回的话在 `error` 正文里 |

旧声明式链的两个工具 `render_motion_video` / `patch_motion_video` 已随 P4 下线(节点类、白名单行、
界面入口一并摘掉);`patch_video` 的名字没变,它一直是素材 / 口播那条链的局部改。

## 数据模型(22 张表)

| 分组 | 表 | 要点 |
|---|---|---|
| 身份与对话 | `users` `conversations` `messages` | 消息挂 `conv_id`;`qa.parts` 存媒体部件供刷新重放 |
| 素材 | `materials` `upload_sessions` `timelines` | 归属列是 `owner_user_id`;`object_key` UNIQUE,`origin` 枚举含 `render`(渲染终态把成片登记进同一张表,同对象键重烧只更新一行);分片续传以桶内进度为准 |
| 产物与渲染 | `artifacts` `render_jobs` | 产物按 `(作用域, 产物集)` 隔离;`render_jobs` 有 `attempt` 令牌与 `UNIQUE(session_id, artifact_id)`;`session_id` 有两种拼法(`u:{u}:c:{c}` 与 `{u}:{c}`),删对话时两种一起回收(机制 18) |
| 执行链 | `checkpoints` `checkpoint_entries` | 指针行 + 增量链(`delta`/`full` + `parent_seq`) |
| 团队与调度 | `inbox_messages` `tasks` `task_edges` `subagents` `scheduled_jobs` | 消息中心 / 任务图 / 常驻子 Agent 租约 |
| 记忆与扩展 | `memories` `skills` `skill_files` `mcp_servers` `app_secrets` | 记忆 PK `(user_id, category)`;技能与 MCP 按用户隔离,系统内置的 `owner_user_id` 为空 |
| 检索账 | `retrieval_hits` | 本会话 `web_search` 命中的链接与 `fetch_url` 真打开过的页面。PK `(session_key, url)`,带 `title`/`backend`/`checked_at`;出处闸只认这笔账,按 `user_id` 级联删 |
| 计量 | `token_usage` | 配额闸的数据源 |

## 对外接口(节选)

- **鉴权**:`POST /auth/register` · `POST /auth/login` · `POST /register`(匿名 token,可被 `REGISTER_OPEN=0` 关) · `GET /whoami`
- **对话**:`POST /chat`(投 MQ) · `POST /chat/sync`(同步版,给脚本用) · `WS /ws/{conv_id}?token=…` · `GET /sessions`
- **会话与执行**:`/convs*`(其中 `DELETE /convs/{cid}` 除删消息与对话行外按会话回收,响应带 `gc` 账;
  在途渲染被跳过时另带 `gc_notice`) · `/convs/{cid}/runs[/active]` · `/runs/{id}[/history|/fork|/resume|/approve]` · `GET /preview/{cid}`
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
  `patch_motion_code` 与 `patch_video`。**长任务,判据在轮询端的正文里**:
  闸门退回也表现为一路 200 + `status=failed` + `error`,HTTP 状态码不作判据
- **扩展管理**:`/tools` · `/skills*`(含 .zip 技能包) · `/mcp/servers*`(热连热断)
- **设置**:`/settings*`(模型密钥、主模型选型、判断模型三件套 + `POST /settings/judge/test` 连通性自检;
  检索后端三件套 `POST /settings/search` 写 + `POST /settings/search/test` 试一路;
  配额 `POST /settings/quota`——字段**缺省＝不动、空串＝清页面覆盖、`"0"`＝关拦截**;
  后两组与「设置」页对应区块都是 2026-10-11 落地的,此前文档里写过但代码没有,见 §16 订正)
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
| 零素材出片 | **Remotion 4.0.534** + React 19.2(TypeScript),常驻独立服务 `creation-remotion` | 画面是模型逐镜交的 TSX,由该容器逐帧渲(`@remotion/bundler` + `@remotion/renderer`,Node 侧一次一镜进浏览器)。风格通道依赖:`roughjs 4.6.6` / `three 0.186.1` + `@react-three/fiber 9.8.1` + `@remotion/three` / `katex 0.19.0` / `@remotion/shapes`+`@remotion/paths`。Python 侧只是一个 HTTP 客户端(`motion/remotion_client.py`),**它崩了不带走后端的剪辑能力**;地址走 `REMOTION_URL`(compose 内 `http://remotion:8010`,不映射宿主端口) |
| 网页出片 | headless Chromium 逐帧截图 | `render_web` 仍走这条路:镜像自带的 `chromium`(root 身份自动补 `--no-sandbox`,恒带 `--disable-dev-shm-usage`);本机想用别的浏览器用 `STORYLINE_CHROME` 指过去。不引 Playwright/Selenium,也不引生成式图像模型 |
| 视觉理解 | 与主 LLM 同源的多模态模型 | 默认 `deepseek-flash`(实测能吃 `image_url`) |
| 链接取料 | yt-dlp ≥2026.8.19 | YouTube 需 JS 运行时 + 网络出口 |
| 前端 | Vue 3.5 + Vite 6(无组件库、无路由/状态库) | 单文件 `App.vue`,build 产物由主服务托管 |
| 部署 | Docker Compose · 两阶段 Dockerfile · Caddy | 反代 :80/:443;三个常驻容器 `creation-app`(python:3.12-slim + ffmpeg + `fonts-noto-cjk` + `chromium` 供 `render_web`)、`creation-editor`(同镜像另一条命令)、**`creation-remotion`**(`node:24-slim` + chromium + `fonts-noto-cjk` + ffmpeg,`build: ./remotion`,不映射宿主端口)。字体两个镜像各自装一份——母版字体栈只认镜像里真装着的字族 |
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
| 账号与配额 | `JWT_SECRET` · `REGISTER_OPEN`(默认 1) · `QUOTA_LIMIT`(默认 0=不拦) · `QUOTA_WINDOW` | `JWT_SECRET` 缺省时首启自动生成入库;`QUOTA_*` 只是**中间一层**,页面「设置 → 配额」按身份压过它——改 env 要重烤镜像,改页面不用 |
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
docker compose --profile app up -d --build    # + editor(:8001) + remotion(:8010，只走容器网) + app(:8000) + caddy(:80)
```

- 存储端口**只绑 127.0.0.1**(PG 5432、MinIO 9000/9001),**Redis 不做宿主机映射**——本机原生 Redis
  占 6379 时不会互相撞死;公网暴露面只有 Caddy 的 80/443;
- `app` 的启动命令**必须带** `--storyline-config examples/storyline/config.docker.toml`:默认
  `config.toml` 的 `connect_host` 是 `127.0.0.1`,在容器里那是它自己,启动期 MCP 探测必然
  `Connection refused` → 整站「无剪辑能力」(计划门也不会出卡)。`config.docker.toml` 的唯一差别就是
  `connect_host = "editor"`;
- `editor` 带 TCP 就绪探针、`app` 依赖它 `service_healthy`——两者同秒拉起时,不等就绪就会撞同一个坑;
- `remotion` 反过来**刻意不进** `depends_on`:存储没就绪服务必然起不来,而渲染服务没就绪只是「这一镜渲失败」,
  做成启动依赖等于让图形链的故障半径扩大到整站。它的 healthcheck 用 node 自带 fetch 真打 `/healthz`
  (端口开着不等于应答),地址走 `REMOTION_URL`(默认 `http://remotion:8010`,`app` 与 `editor` 共用同一份环境锚);
- 接线成功的判据(启动日志明说,2026-10-11 重烤后实测):
  `[startup] Storyline 已接入 27 个剪辑节点（DAG 契约 25 节点，rerun_from 可用）`
  + `[startup] 中文名词表已装载：57 条（工具 / 剪辑节点 / 技能），参数标签 92 条`;
  数字跟着白名单走(`examples/storyline/config.toml`:25 个 DAG 节点 + 2 个控制节点),改名单就得回来看这一行;
- `deploy/Caddyfile` 默认 `:80 reverse_proxy app:8000`,配 `SITE_DOMAIN` 后取消注释出 HTTPS;
- `WHISPER_PRELOAD` 构建参数可预下载 ASR 权重;`caddy` 没有 healthcheck(它不是依赖项)。

## 测试与验证

| 层 | 内容 | 怎么跑 |
|---|---|---|
| 离线回归 | `backend/tests/` **93 份**脚本式套件(2026-10-11 实测 **1105 条断言全过、红文件数 0**)——每份自带 `asyncio.run(main())` 与断言计数,pytest 把**整份脚本**收成一个用例、以子进程执行(全量输出落 `.tmp/reg_pytest/`)。pytest 从脚本内部还会解析出 15 个假用例(`async def` 缺插件 / 缺 `tmp` fixture),由 `conftest.py` 在收集后丢弃——否则标准入口的结论全是假故障 | **以 `creation-app` 容器内那一趟为准**:镜像里没装 pytest,逐份 `PYTHONPATH=/app python tests/test_xxx.py`(宿主机 python 的版本与依赖都不是运行时那份)。宿主机标准入口:`cd backend && python -m pytest` |
| 渲染容器自闸 | `remotion/tests/gate1-lint.mjs`——闸① 的先证红再证绿:**落点模板 `src/user-shots/shot.tsx` 必须绿**(模板自己红,用户写第一镜就挨打),四类违规必须红并带规则名与行列号(未包裹 `<FilmLayout>`、越界 import、代码里取随机/时间/网络/外部字体、字段声明与代码读/标注三相对不上账)。**它跑在 Dockerfile 层末**——红就烤不出镜像,报警发生在构建期而不是模型写完代码那一趟 | `docker build -t creation-remotion:latest ./remotion`(或容器内 `cd /srv && node tests/gate1-lint.mjs`) |
| 真机冒烟 | `backend/.smoke/` **24 个** `*_smoke.py`(b3~b16 + `settings_key`/`thinking_off`/`vl_shared_key` + `b22_code_chain_render`/`b23_field_patch`/`b24_code_ui` 这条代码链 + `t5_track_patch` 口播选区改)+ **23 个**探针/种子脚本(含 b18 搜索链六路探针);**旧声明式链的 17 份夹具与用例归档在 `.smoke/_archived_p4_declarative/`**(含 b17/b19/b21 与四条零素材样片),归档不是可跑路径,跑前先读那份 README | 依赖模型的那批带 `credit_gate()` 前置闸:额度不就绪直接 SKIP 并打印原因,不会把"账户空了"报成"代码坏了" |
| 关键路径的钉子 | 计划门(`test_plan_gate`)· 出口中文(`test_display_outlets`)· 存储契约(`test_storage_contract` 212 项)· 渲染跟随(`test_render_follow`)· dry-run 路由(`test_dry_run_dispatch`)· 修字(`test_transcript_correction`)· 复盘(`test_plan_retrospective`)· 判断模型(`test_judge_gate`)· 代码镜契约与缓存键(`test_code_spec` 168 项:字节级幂等、闸⑦ 配额与技能正文逐行比对)· 八道闸本体(`test_code_gates` 109 项:每道判红的闸一条先证红再证绿)、闸⑧ 版面回读三态与只记账(`test_code_layout` 50 项:missing/unscanned/scanned 两两不同、畸形读数不许抛、措辞不抄常量表、layout 在缓存还原名单里)· 字段通道与闸⑥(`test_code_fields` 138 项)· 逐镜质感(`test_code_texture` 48 项)· 两条链的 HTTP 口(`test_patch_endpoints` 33 项:三口都在、点名跟着真实节点走)· 图形通道与链无关的那批约定(`test_motion_channel` 217 项:开关名单同源、七个开关真上计划卡、契约标签走到确认卡,外加旧链下线后由它代管的六件事——出处闸的凭据语义、局部改读哪一版、命中表与代表帧发布、切片缓存的收发协议与质感滤镜、JSON 指针工具、词时间轴贴字符)· 成片入库与按会话回收(`test_session_gc` 56 项:两种 `session_id` 拼法都清、同对象键重烧只更新一行、`materials.object_key` 的保护名单护住已入库字节、在途渲染拒绝回收、开机对账幂等) | 见各文件头的"钉住 N 件事" |
| 提示词回归 | `tests/data/planning_round_golden.json` 金样逐字比对 + `scripts/run_eval.py` 的 prompt fingerprint | 改提示词后先跑这两个 |

> **重要**:`backend/tests/`、`backend/.smoke/`、`backend/scripts/`、`backend/docs/`、`backend/pytest.ini`、
> `backend/start.bat` 都在 `.gitignore` 里——**克隆出去的仓库不含这批**,`git ls-files backend` 只有 117 个文件
> (这是**已跟踪**的数;图形链新增的那几个文件此刻还在工作树里,入库后这个数还要涨)。
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
| 构建时 apt 报 `Failed to fetch … 502/500` 后 `exit code 100` | 是网络路(本机代理/CDN)对个别 `.deb` 丢包,不是 Dockerfile 坏了——每次失败的文件还不一样。`ffmpeg` 与 `chromium` 两层已改成**层内最多装三遍**:失败字节留在 apt 缓存里,后一遍只补没取到的那几个(实测第二遍就过),层末再用 `ffmpeg -version`/`chromium --version` 硬验真装上。三遍仍红就换网络,或把构建走的那个代理关掉再试 |

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
- **零素材图形科普片(代码镜链)的质量上限就是模型 TSX 的上限**:这条链**没有内置版式可退**,
  闸④ 只拦「带子里几乎没像素」与「内容挤成一团」,闸⑦ 只拦「通道扎堆」与「纯文字镜过半」,
  **好不好看、构图对不对不在任何一道闸的判据里**——那一路只能靠技能正文与逐镜账交人眼;
- **真 3D 靠软件 WebGL**:容器里只有 `DG_GL=swangle` 出得来画面,其余后端渲成黑帧。
  **闸② 没有按通道限 3D 的帧率**(整片帧预算 4000 是唯一硬闸),一条 three 镜写 30fps 长镜头照样能
  把整片预算吃光——这是设计文档 §10 记着的**未完项**,现在靠技能正文那句「3D 镜压 fps」兜着;
- **渲染是串行的**:渲染服务一次只放一镜进浏览器(避开软件 WebGL 的内存峰),外加**每次请求 4.6~7.5 秒**
  的打包固定成本(与镜头内容无关)。所以这条链的账是「镜数 × (打包 + 帧数 × 逐帧开销)」,
  逐帧开销由账上 `per_frame_ms_max` 报出来,别拿旧链那句「每帧 0.8~0.9 秒」当现值——量法换了;
- **质感只有那五档固定滤镜串**,不开放自定义参数(要自定义等于把滤镜链交给模型,那条链上没有静态校验),
  `glow` 那一路带 `gblur`+`blend` 最贵。**2026-10-11 起质感跑在每一镜的切片上**(拼接之前、缓存之后),
  代表帧也从加了质感的那份切片重抽(`code_render._shot_texture` 的第四个返回值),所以
  **编辑器里的缩略图与改前/改后对比帧现在看得见这层观感**——旧声明式链那句「母带层滤镜、缩略图看不到」**已作废**;
- **逐镜质感 ≠ 任意质感**:一档只有那五个取值(外加"跟随整片"与"这一镜不加"两种形态),
  前端**不许自己重算** `code_spec.shot_texture`——两处各写一遍「镜头有就用、没有问整片」,
  就会出现界面显示一档、账上是另一档、画面上是第三档;
- **这一批的证据分两档,不许混着说**:
  ①**离线全绿**(容器内 `PYTHONPATH=/app` 才算):`test_code_spec`(契约、缓存键字节级幂等、
  闸⑦ 配额与技能正文逐行比对)、`test_code_gates` 109 条(判红的闸各一条先证红再证绿)、`test_code_layout` 50 条(闸⑧ 三态与不拦截)、
  `test_code_fields` 138 条(字段通道 + 闸⑥)、`test_code_texture`(逐镜质感三形态、账与滤镜同源)、
  `test_patch_endpoints`(两条链共用的三个 HTTP 口:读表/取帧/提交,钉的是三口都在、
  点名跟着真实节点走、body 键原样透传、base 号一定换新);
  ②**真渲过的**只有单镜级与短片级:`.smoke/b22_code_chain_render_smoke.py`(整链出片)、
  `b23_field_patch_smoke.py`(改一个字段只重烧一镜、`cached_shots=1`)、B1-3 的三条风格通道 × 两画幅六支、
  `remotion/tests/layout-probe.mjs` 六支(闸⑧:叠字/越带/引线悬空各真渲一镜,含两支**必须不响**的钉住 0.4 与
  3px 那条线、一支把引线藏在 `<g transform>` 里钉住 `getScreenCTM` 映射)。同一批按纪律先证过红:
  把母版的 `ref={bodyRef}` 摘掉再跑,六支全部退回「版面没扫:画面带 ref 未挂载」,恢复后 ALL PASSED。
  **整片端到端的「多风格 + 配音」真机样片还没跑**——那句「可以生成高质量多风格视频」要等这一跑才能声称,
  §15 与设计文档 §8 都记着这笔;
- **旧声明式链的两份 2026-10-10 真机账已归档**(`.smoke/_archived_p4_declarative/`,代码与夹具一起):
  一份是四族连排 + `texture=film` 的整片账(成片 17.13s、真截 171 帧、`film→tv` 换档复用全部 5 镜),
  一份是「局部改其实整片重烧」那次静默失败的根因(归一化不幂等:`theme` 的默认值出 int `44`、
  用户显式填的值走 `_clean_num` 出 float `44.0`,而 `44 == 44.0` 为真——改动账、逐镜比较、时长帧数全看不出异常,
  只有 `shot_cache_key` 认 JSON 字节,于是「只重烧受影响镜」这条合同是假的)。
  **教训与实现无关、今天仍然成立:按字节算的键必须按字节断言,`==` 看不见 `44` 与 `44.0`**;
  但当时修它的代码(`_norm_theme`/`THEME_PX`/22 卡守卫)随那条链一起删了,新链上守这条的是
  `tests/test_code_spec.py` 里「同镜同参数两次算出同一个键」那条字节断言。
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
- **`fetch_url` 对「反爬挡住」与「JS 壳页」两类仍然取不到正文**:2026-10-11 真机三条链接实测——
  B 站视频页修完解压后拿到 3.2 KB 可读正文(标题、UP 主、简介都在),知乎专栏回 403 被挡在门外,
  YouTube -watch 连 `<title>`/`<meta>` 都没给可读正文。这一批改的只是**把三种情况分开报**,
  不让模型把「我们的流量被挡住 / 这页靠脚本渲染」读成「网上没有这条内容」;真要把后两类也读进来,
  需要带 JS 渲染的浏览器抓取那一路,不是再改一次错误文案。知乎那把锁**换 UA 撬不开**——同一时刻两档
  UA(桌面 Chrome / 工具自报)在专栏页与问题页上全部 403,与 DDG 那一路同档,是出口信誉与 cookie 的事;
- **出处闸只对账,不判真伪**:它能保证"没真打开过的链接不许写成查证过",不能保证查到的内容是对的。
  史实对错仍归模型与人工;
- **出处闸认的是标记表里的连续字样**,措辞差两个字就算"声称查证过":2026-10-10 真机上冒烟夹具把出处写成
  「本机核对夹具(未对外查证)」被打回(标记表收「未核实 / 未查证 / 凭记忆」这类连续短语,中间插了「对外」就不认)。
  方向是保守的——宁可错拦诚实的措辞,不错放声称查证的猜测——但写出处请照闸门给的原话
  「未核实:<依据一句话>」;
- **出处闸挂在三处,判据同一份**:`plan_motion` 入库前(全量,代码分镜也在这一步收——清单 #159 已于
  2026-10-10 做完,这条链不再有"过不了计划门"的旁路)、`render_motion_code` 收到**手写 inline spec**
  时就地补走一次(冒充查证过的出处在烧像素之前带镜号打回)、局部改(只复核本次改到的镜)。旧链那套靠
  渲染节点 `required_nodes=["plan_motion"]` 兜的写法随 P4 删链一起退场了。**闸仍然只对账不判真伪**,
  脚本直调这一路的判据也只剩"这个 URL 本会话真打开过没有"。
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
  **但这是清账不是修好**：这些表按 run 只增不减。2026-10-09 之后堵上了两条自动回收的路
  （机制小节 18）：删对话时按会话级联清行与前缀字节，启动/周期对账再扫「对话已不存在」的残留
  与只有字节没有行的崩溃残留。**仍然会涨的是不删对话的那批**——没有 TTL，一条长会话跑久了
  它的 `motion-shots/*`、`render-windows/*` 切片与 artifacts 行就一直累积，清理口径未定。
  两条口径记在这里：`scheduled_jobs` 里那行 `heartbeat` 是系统自己的 30 秒任务，**不属于历史产物**，
  清理名单故意不含它；`default` / `cron` 两个内部身份删了会在下次启动自检时重新登记，不算数据丢失。
  落地时踩到的两点:`purge_identities.py` 刻意只认逐个点名的 id(批量口径要自己生成清单再喂 `xargs`);
  删完桶里会留下**0 字节的目录标记**——`objects.head(前缀/)` 仍然打得开而 `list_prefix` 不列它,
  拿 head 判「对象还在不在」会误判成没删干净。
- 历史日志(早于本轮掩码改造的 `.tmp`/`.log`)里存在当时明文写下的 WS token,属历史数据,未回溯清洗。
- **选区改「换配乐」会重烧全部窗口**:配乐混在母带那一层,`timeline_edit.window_inputs` 把它算进**每一窗**自己的缓存输入(`core_nodes.py` 的逐窗台账同源)——所以拧一次配乐音量,逐窗输入清单全部不同,没有一窗能复用。这不是退化成整片重烧的 bug,是"配乐影响整条片子"的正常结果;嫌慢就接受"换配乐=重新出片"。
- **手改过的段,同步闸不再判、只能人眼验**:段上带 `manual` 标(`timeline_edit.MANUAL_MARK`)时,`resync_original_audio_timeline` 会跳过它不替用户改回去、`av_sync_check` 也跳过它不判——用户刚拧好的取景/音量不能被自动校准抹掉。代价是这一段的口型对不对,机器不再背书,账上如实记一条 UNVERIFIED(`eyeball`)。要机器重新接管,得去掉手动标重渲。
- **图形科普片的样式同步只认「同名 + 同类」的字段**:代码镜没有 card 也没有 theme,能同步的只有
  **别的镜也交出来的同名字段**(`MotionEditor.vue::fieldSiblings`,name 与 type 都对上才算同一个旋钮)。
  没交这个字段的镜不参与——硬塞进去等于替它改代码;取值还要过**它自己**声明的 min/max/options,
  过不了就落进「没改」那一栏并写明数字(不拿这一镜的刻度去量另一镜)。范围在保存前必须问一次
  (全片同名字段 / 只这一镜),默认任何一个方向都会替另一批用户做决定。
  **旧声明式链在编辑器里的形状已于 2026-10-11 清掉**:「整镜」「样式」两块面板、画面上那圈带编号的
  叠加图层框,以及询问条里「卡面样式同步到同 card 的镜」那一档。它们本来就不是「暂时没人点」而是
  **渲染就会空掉**的分支——模板引用的 `themeCtl`/`themeSet`/`curTheme`/`curOverlay`/`laySet`/
  `stageLayers`/`renameKey` 这批助手早随旧链一起从 `<script setup>` 删了,`v-for` 拿到 undefined
  只是安静地画不出东西。同一趟还删了「按文字近似对上,请核对 / 多处同字」两档提示:现行两张表
  (代码镜字段表、`segment-hitmap/1`)的指针**恒为 `confidence=exact`**,没有 ambiguous 这个键,
  留着等于在界面上宣传一个已经不存在的数据缺陷。「选中段的字段」(口播轨)照旧。
  前端产物烤进 `creation-app` 镜像(`Dockerfile` 的 web 阶段),所以这一条与重烤是同一批做完的。
- **版本列表只活在本次编辑器会话内**:`MotionEditor.vue` 的 `chain` 是内存里的,只列"本次编辑器里走过的版本";服务端**没有**"列出某会话全部版本"的口,跨刷新/重开面板找旧版要在对话里报产物号(`adopt(artifact_id)` 才打得开)。
- **工具调用气泡的参数值仍会露机器名**:参数**名**有三级中文兜底(`arg_labels` 帧内 → `/tools` 的 `params_display` → 本地 `ARG_LABELS` 白名单),`App.vue::summarizeArgs` 末尾 `|| k` 意味着**不在词表里的参数名直接露英文原名**;参数**值**按原样 `JSON.stringify`,对象键、段 id(`ev-…`/`su-…`)、机器枚举值都会原样出现在气泡里——这一条目前没有做,属如实保留的展示层粗糙。
- **拧声音段的取源区间会把画面搬走、并按新内容改名**:`resync_original_audio_timeline` 为了让嘴型对上,把压在改过的声音段底下的画面段钉到新的声音位置;画面段 id 是按「素材 + 取源区间」算的,内容一换名就换(`ev-52cea4bed2` → `ev-12c33fa9e5` 是真机那一次)。若两段因此取到**同一段素材的同一区间**,`assign_segment_ids` 去重成 `ev-xxx#2`——而 `_safe()` 落对象键时把 `#` 换成 `_`,所以帧文件名与指针里的名字**写法不同**,跨机器比对要认段 id 而不是文件名。
- **「改了哪几窗」以渲染器台账为准**:调用方改前算出的 `expected_rebuild` 用的是旧名字,名字一漂就点不到(那一窗会被当成没改、从旧片切进来冒充)。所以 `render_windowed` 额外按**秒区间**比对两份时间线的逐窗输入清单,补出 `planned_rebuild`;证据与对比帧都按这份取,新旧名的映射由后端在 `patch.before_frame_shots` 里给(`MotionEditor.vue::baseShotOf` 读它)。**副作用**:改名后的那一窗必然是重烧的,复用率比不漂名时低。
- **一格指针的取值收口在入口**:编辑器拖一个把手只发**一个数**,`apply_patch` 负责成对——源区间塌成零/负长度时另一端按原输出长度平移回去、顶出负起点时整段回 `0~长度`(旧写法会落出一段零长度源段,渲到那儿 MoviePy 在合成音频时抛 `zero-size array … no identity`,补丁却已落库)。**仍然存在的边界**:源文件比请求的区间短,只能渲到素材尽头,入口不预知素材时长。

## 设计文档

架构决策与逐轮审计记录在 `backend/docs/`(本地保留,不入库):剪辑流程根因报告、稳定性优化报告、
第三轮优化报告、缺陷修复记录、完善度体检。核心机制的口径速记:

checkpoint 指针行 + 一致点增量链 · DAG 拦截器递归补齐 · 计划门两次 run ·
渲染提交+轮询+尝试令牌+看门狗 · 五级证据分级 · 出口中文单源 · 凭证按身份热读。
