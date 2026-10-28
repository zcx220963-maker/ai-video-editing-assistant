# 智能创作助手（AI Video Editing Assistant）

一句话驱动的视频创作 Agent。给一句选题，它自己分镜、逐镜写画面、配旁白与配乐，一次渲成片；
也可以**先看分镜表、改完再渲**。

需求里没提的创作决定——镜数、构图、配色、字幕位置、转场——由 Agent 依选题自己定，过程中不再追问。
**唯一该由人拍板的是分镜**：十五句话读下来，对不对十秒就知道；读着不对，就不烧那十几分钟渲染。

## 功能特性

- **两条出片通道**
  - **素材剪辑**：上传视频或音频（也可粘贴链接），自动切分镜头、语音转写、理解画面、筛选分组、
    撰写文案、配音配乐、编排时间线并渲染。
  - **零素材出片**：只需一个选题。**画法不预设**——镜数、镜长、配色、构图、笔触全由导演
    （一次模型调用）按选题定：`tone` 写清底色/主色/构图习惯/笔触，`caption` 给全片字幕规格。
    画面**逐镜现写 SVG**，**不给零件清单**（实测 15 镜出 15 种画法，不是把同一批组件重排）。
- **先看分镜再渲**：`POST /simple/plan` 只出分镜，**1 次调用、约 10 秒**。分镜表里每句都能直接改，
  确认后才开始写画面并渲染。分镜错了只花 1 次调用，而不是整条重跑二十几次。
- **字幕三层**：`overlay`（外层统一加，字号按**短边**与字数算）、`drawn`（**默认**：模型自己画进画面——
  题款、批注、碑文、纸卡、屏幕上的字）、`none`（这一镜不要字幕）。**没有底部黑条**，
  靠描边与双层阴影保证可读，不把画面下三分之一压暗。
- **硬数据自己画**：对照表/对照卡由写手按分镜数据用 SVG 画（不再由固定组件注入）——
  三列以内、数值右对齐、细线分隔、压在画面一角。
- **卡拍**：点名配乐时先检拍点（`simple/beats.py`），把每镜切点往最近的拍点吸
  （**只往后补、封顶 0.45 秒**），切点跟着鼓点走。
- **改一镜**：`reshoot_shot` 只重写第 N 镜并出 2 张静帧，**1 次调用**；满意了再
  `render_short_film` 重渲整片（音轨照用，不重新生成）。
- **导出工程**：`export_film_project` 导成能直接 `npm i && npx remotion render` 的工程。
- **曲库配乐**：从 `materials`（`origin=bgm`）按主题挑曲。旁白与配乐是**两条独立可选轨**：
  有旁白时侧链闪避到人声之下；无旁白时配乐单独成轨（独奏音量、首尾淡入淡出、接缝交叉淡化后循环铺满）。
- **随时叫停、从一致点续跑**：任务跑起来后，输入框那颗「落笔」变成方块停止键，点一下在最近的
  一致性点收手——状态记 `cancelled`，**不进崩溃恢复队列**（不会被自动重放烧 token），
  气泡里给「从最新一致点续跑」，点了接着跑。
- **出处账**：每个数字都要能说清从哪来；缺出处的镜会在报告里点名，**不假装核实过**。
- **逐帧质检 + 三层兜底**：每镜渲 3 帧量内容占比与平均亮度，不合格就带**具体读数**打回重写
  （"亮度只有 17/255"而不是"亮一点"）；重写两轮仍不合格才用兜底，并在报告里单独列出来。
- **工程能力**：崩溃可续跑的执行快照、多用户隔离、模型密钥按用户配置、技能按需加载、MCP 扩展、长期记忆。

## 技术栈

Python 3.12 · FastAPI · SQLAlchemy（async）+ PostgreSQL 16 · MinIO · MCP（fastmcp / mcp）· OpenAI 兼容 SDK ·
ffmpeg · MoviePy · faster-whisper · edge-tts · Remotion 4 + React 19 · Vue 3 + Vite · Docker Compose · Caddy

## 架构

```
浏览器（Vue 3 单页，登录获取 JWT）
   │  HTTP + WebSocket
   ▼
┌─────────────────────────────┐   MCP（streamable HTTP）   ┌─────────────────────────────┐
│ 主服务 :8000（Agent 框架）  │ ───────────────────────▶   │ 剪辑服务 :8001（MCP 节点）  │
│ 对话循环 · 技能 · 记忆      │ ◀───────────────────────   │ 节点 DAG · ffmpeg · MoviePy │
│ 确认门 · 执行快照 · 鉴权    │         产物 / 进度        │ 本地 ASR · TTS · 场景切分   │
└─────────────┬───────────────┘                            └─────────────┬───────────────┘
              ▼                                                                        ▼
   PostgreSQL（元数据）  MinIO（媒体文件）  Redis（可选）
                                                           Remotion 渲染服务 :8010
                                                           （Node + Chromium 逐帧渲染）
```

- 对外只暴露主服务一个入口，前端构建产物由主服务托管。
- 剪辑服务与渲染服务为独立进程：渲染是分钟级任务，其负载不影响对话。

## 快速开始（Docker，推荐）

**前置条件**

- Docker（含 Compose v2）
- 内存建议 16 GB 及以上，渲染服务使用软件 WebGL 逐帧渲染
- 一个 OpenAI 兼容接口的模型 API Key，默认对接 DeepSeek

```bash
git clone <仓库地址> && cd <仓库目录>
cp .env.example .env                          # 本机使用可直接沿用默认值
docker compose up -d                          # 存储层：PostgreSQL + MinIO + Redis
docker compose --profile app up -d --build    # 应用层：剪辑服务 + 渲染服务 + 主服务 + Caddy
```

首次构建需下载依赖与浏览器，约 10~20 分钟。启动后：

1. 打开 <http://localhost> 并注册账号；
2. 在左下角「设置」图标中填入模型 API Key，保存后立即生效；
3. 在输入框发送需求，例如：

   > 做一条竖屏科普短视频，主题：为什么天空是蓝色的，2 分钟左右，字幕用撕纸高亮。

**验证启动**：`docker ps` 中 `creation-app`、`creation-editor`、`creation-remotion` 均为 `healthy`，
且 `docker logs creation-app` 出现 `[startup] Storyline 已接入 … 个剪辑节点`。若提示「无剪辑能力」，见常见问题。

**修改代码后**分两种：

- 后端（`agent_framework/`、`open_storyline/`、`examples/`、`prompts/`）已 bind mount 进容器，
  改完 `docker compose restart app`（涉及剪辑服务时连 `editor` 一起）几秒生效，不必重建镜像；
- 前端产物（`frontend/dist`）烤在镜像里，改了前端要重建：`docker compose --profile app up -d --build`。
  依赖（`requirements*.txt`、`package-lock.json`）变了同样要重建。

## 本机开发

```bash
docker compose up -d                          # 仅启动存储层
cp .env.example .env

cd backend
pip install -r requirements.txt -r requirements-storyline.txt
# 需要 PATH 上有 ffmpeg / ffprobe；首次语音转写会下载 faster-whisper 模型权重

python run_storyline.py --config examples/storyline/config.toml   # ① 先起剪辑服务 :8001
python run_server.py --port 8000                                  # ② 再起主服务 :8000

cd ../frontend && npm install && npm run build                    # 前端产物由 :8000 托管
```

打开 <http://127.0.0.1:8000>。

- 必须先启动 :8001 再启动 :8000：主服务在启动时连接剪辑服务并拉取节点清单。
- 零素材图形科普片依赖 Remotion 渲染服务，该服务默认仅在容器网络内可达。本机开发若需使用这条链路，
  建议采用 Docker 方式，或自行映射 `remotion` 服务的 8010 端口并设置 `REMOTION_URL=http://127.0.0.1:8010`。
- 依赖版本：Python 3.12+，前端 Node 20+，渲染服务镜像内为 Node 24。

## 使用说明

- **零素材出片的三步**：写一句选题（带讲法更好："从瑞利散射讲到日落变红"）→ 选画法（叙事片 / 动效科普）
  与时长、画幅 → 点「先看分镜」拿到分镜表，每句都能直接改，确认后出片。
  直接点「直接出片」也可以，那就跳过看分镜这一步。
- **全自动 / 先出计划**：输入框旁的切换按钮。默认「全自动」，Agent 直接执行；「先出计划」会先给出
  可逐项调整的计划卡再执行。服务端缺省入口通过 `run_server.py --entry auto|plan` 设置。
- **一个输入口**：画面、图片、文字都可以直接作为参考放进对话；不需要先去素材库整理。
- **中途停下**：任务运行期间输入框右下角是方块停止键（`POST /runs/{id}/cancel`），点一下在最近的
  一致性点停住，气泡里出现「从最新一致点续跑」。计划卡等你确认那一格没有任务在跑，此时点停止无效；
  若这一跑刚好自己收尾（`409`），前端也当作正常结束、不报错。
- **计划确认卡**：渲染前弹出，含每镜预览图、文案与整片参数。选择「我还要改」并说明修改点，
  Agent 修改后会再次请求确认。
- **配乐**：上传音频，或将歌曲放入曲库后直接以曲名指认；成片长于配乐时自动循环，并按旁白闪避。
- **文字库**：左侧栏挑选字幕样式与朗读音色，挑过的维度在本对话内钉住；「恢复默认」交回模型。
  零素材出片的字幕由分镜的 `caption` 字段决定（`overlay` / `drawn` / `none`），不再逐镜套模板。
- **长片**：有旁白时整片时长由每句话要说多久决定，系统会把缺口摊成每镜说完之后的留白；
  无旁白时由每镜停留时间累加得出。上限 240 秒（叙事 24 镜 × 12 秒 / 动效 56 镜 × 5.2 秒）。

## 配置

全部配置位于 `.env`，由 `.env.example` 复制而来，模板中每项均有注释。常用项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `PG_*`、`MINIO_*` | 数据库与对象存储连接 | 与 compose 默认凭证一致 |
| `MINIO_PUBLIC_ENDPOINT` | 向浏览器签发媒体链接所用的地址；从其他机器访问时改为本机局域网 IP:9000 | `127.0.0.1:9000` |
| `OPENAI_API_KEY` | 环境变量形式的模型密钥，日常在页面「设置」中按用户填写 | 空 |
| `OPENAI_THINKING` | 模型思考模式 | `off` |
| `REGISTER_OPEN` | 是否开放自助注册，公网部署建议设为 `0` | `1` |
| `QUOTA_LIMIT` / `QUOTA_WINDOW` | 每用户在窗口期内的 token 上限，`0` 表示不限制；公网部署须设置 | `0` / `86400` |
| `JWT_SECRET` | 登录令牌签名密钥；留空则首次启动自动生成并入库 | 空 |
| `SITE_DOMAIN` | 公网域名，配合 `deploy/Caddyfile` 启用 HTTPS | 空 |
| `MOTION_QC` | `check_film` 工具的质检档位（`preview` / `strict`）。零素材出片走自己的逐帧判据，不受它影响 | `preview` |
| `MOTION_RENDER_PARALLEL` / `DG_RENDER_SLOTS` | 渲染容器逐帧渲染的并发路数，两项需同步调整。每路约 5 个核、2 GB；16 GB 内存下 3 路稳妥 | `4` / `4` |
| `SIMPLE_MAX_JOBS` | 同时跑的「一句话出片」作业数上限（渲染容器本身串行，这个数只防连点） | `3` |
| `SIMPLE_JOB_TTL_SEC` | 作业在内存里留多久（秒）。重启丢作业、不丢已上传的成片 | `7200` |

剪辑能力（视觉理解模型、ASR 模型、TTS 音色、场景切分阈值等）在 `backend/examples/storyline/config.toml`（本机）
与 `config.docker.toml`（容器）中配置，两份文件的节点白名单需保持一致。主服务的启动参数见 `python run_server.py --help`。

### 启动开关：真相存在哪

下面几项决定「单机能不能跑、多副本要不要换后端」。默认值都是可单机跑的那一档；多副本部署要逐项切换。

| 开关 | 可选值 | 默认 | 说明 |
|---|---|---|---|
| `--mq`（或 `MQ_BACKEND`） | `kafka` / `memory` | `kafka` | 生产走 Kafka；本地单进程或离线测试显式 `--mq memory`。broker 不可达时 `AgentWorker` 快速失败，不会假装在跑 |
| `--bootstrap-servers` | 地址 | `localhost:9092` | Kafka broker |
| `--cron-store` | `file` / `pg` | `file` | 定时任务：文件为真相（单机或共享卷）。跨主机必须 `pg`——文件没有原子 claim |
| `--team-store` | `file` / `pg` | `file` | Agent Team 的任务板 / 收件箱 / 子 Agent 落点。多副本必须 `pg` |
| `--team-root` | 目录 | `.runtime/team` | 文件版团队三件套的根目录（`tasks/` `messages/` `agents/`） |
| `NODE_PROFILE`（**环境变量，不是 CLI 参数**） | `product` / `design_baseline` | `product` | `design_baseline` 只暴露设计 §22 的 18 项节点，用于对齐验收；产品档是全集 |
| `--no-memory-iteration` | 开关 | 关（默认每轮刷新） | 默认除装配期注入外，每次 LLM 调用前按当前记忆原地刷新 `<memory>` 块；加此开关退回「只在装配期注入一次」 |
| `--no-memory` | 开关 | 关 | 整条记忆能力关闭（不读、不写、不注入） |

### 公网部署

1. 在 `.env` 中设置强密码（`PG_PASSWORD`、`MINIO_SECRET_KEY` 及对应的连接串）、`JWT_SECRET`，并设 `REGISTER_OPEN=0` 与 `QUOTA_LIMIT`；
2. 设置 `SITE_DOMAIN`，按 `deploy/Caddyfile` 中的注释启用 HTTPS 块；域名 A 记录指向本机并放行 80/443；
3. 数据库与 MinIO 端口仅绑定本机回环，对公网只暴露 Caddy 的 80/443。

## 目录结构

```
backend/
  agent_framework/       Agent 框架：对话主循环、工具注册与并发、上下文与压缩、技能、记忆、
                         执行快照（checkpoint）、计划门（plan/）、确认门、HTTP/WS 服务、存储层（storage/）
  open_storyline/        剪辑服务（MCP）：剪辑节点（nodes/）、零素材出片链（simple/）、渲染调度
                          （motion/：代码渲染、逐帧闸、拍点与母版）
    simple/              **零素材出片的全部实现**（约 5,300 行，不含 TSX 载荷）
                         showrunner 导演（分镜/写镜/判定）· film 编排 · story 画法 · scene 包装
                         caption 字幕 · beats 拍点 · voice 旁白 · mix 配乐 · bgmloop 无缝循环
                         facts 出处账 · qa 逐帧质检 · review 视觉自检 · morph 跨镜接力
                         edit 改一镜 · export 导出工程 · project 工程落盘 · kernel 渲染 · deliver 上传
  prompts/               提示词库（Markdown，改文案不改代码）
  examples/
    skills/              内置技能（含零素材出片 short_video_skill）
    storyline/           剪辑服务配置（config.toml / config.docker.toml）
  tests/                 后端测试套件（163 个文件，直接 `python tests/test_xxx.py` 跑，不需要 pytest）
  run_server.py          主服务入口（:8000）
  run_storyline.py       剪辑服务入口（:8001）
remotion/                Remotion 渲染服务（server.mjs、母版 FilmLayout、设计 token、代码检查规则、
                         动效与质感夹具 src/demos/_fixtures、构建期自测 tests/）
frontend/                前端（Vue 3 + Vite，单文件 App.vue）
deploy/Caddyfile         反向代理配置
docker-compose.yml       存储层（默认）+ 应用层（profile: app）
Dockerfile               主服务与剪辑服务镜像，两阶段构建：构建前端 → Python 运行时
.env.example             环境变量模板
```

## 测试与自检

仓库只放「clone 下来能跑 + 能跑测试」的那一份：后端代码、内置技能、渲染服务、前端、compose 配置，
以及 `backend/tests/`。

```bash
cd backend
python tests/test_simple_boundary.py     # 简洁线的边界（单文件上限 / import 边界 / 组件清单一致）
python tests/test_simple_film.py         # 编排、时间表算术、判据、字幕、补留白
python tests/test_story_mode.py          # 叙事画法、字幕三层、对照表、跨镜接力
python tests/test_run_cancel.py          # 停止：三处检查点、cancelled 终态、续跑
python tests/test_import_all.py          # 所有模块能 import（删东西时最先红的那条）
```

测试是**自带 `check()` 的独立脚本**，直接 `python tests/test_xxx.py` 跑，不需要 pytest。
容器里跑要带上 `PYTHONPATH=/app`（测试自己只把 `backend/` 放进搜索路径，主包名要从 `/app` 起才算对）：

```bash
docker exec -e PYTHONPATH=/app -e PYTHONIOENCODING=utf-8 <容器> python /app/tests/test_xxx.py
```

- **构建期自检**：渲染服务镜像构建时会跑镜头代码检查规则的自测（`remotion/tests/gate1-lint.mjs`、
  `draw-parts.mjs`），不通过则构建失败。
- **探针、退役语料、开发文档不入库**：判据是"能不能重跑"。要用的探针在 git 历史里
  （`git show 210ee01:tools/probe_layout.py`），详见 `.gitignore`。

## 常见问题

| 现象 | 处理 |
|---|---|
| 回复「无剪辑能力」 | 剪辑服务未启动或未连接。本机方式需先起 :8001 再起 :8000；容器方式确认 `creation-editor` 为 `healthy` 后重启 `creation-app` |
| 所有对话返回 402 或鉴权失败 | 模型账户余额不足或密钥错误，在「设置」中重新填写，无需重启 |
| 页面可打开但媒体无法播放 | `MINIO_PUBLIC_ENDPOINT` 不是浏览器可达的地址；从其他机器访问时改为本机局域网 IP:9000，并放行该端口 |
| 修改代码后未生效 | 后端代码是 bind mount，`docker compose restart app` 即可（技能与提示词在启动时同步进数据库，也要重启）；前端改了必须重建镜像（`--build`），因为 `frontend/dist` 烤在镜像里 |
| 首次语音转写耗时较长 | 首次运行会下载 faster-whisper 模型权重，此后常驻本地 |
| 构建时 apt 报 `Failed to fetch` | 网络或代理对个别软件包丢包，重试构建或更换网络 |
| 零素材出片渲染耗时较长 | 渲染服务用 CPU 软件渲染，约 0.4~0.5 秒/帧；60 秒片子约 15 分钟。可提高 `DG_RENDER_SLOTS`，需注意内存 |
| 出片没有配乐 | 曲库是空的。在「音乐库」面板导入几首，或直接上传音频；没有曲库时这一条片子就静音出 |

## 许可证

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，为非商业许可证：

- **允许**：学习、阅读、克隆运行、个人研究、教学、非营利组织内部使用，以及在此基础上修改。
- **禁止**：任何商业用途，包括出售、提供付费服务，或用于以营利为目的的产品与业务。
- **须保留**：版权与许可声明，即 `LICENSE` 顶部的 Required Notice 一行；修改之处需明确标注。

商业授权请联系作者。第三方依赖各依其自身许可证（MIT、Apache-2.0 等），不受本许可证约束。
