# 智能创作助手（AI Video Editing Assistant）

一句话驱动的视频创作 Agent。输入一句需求，Agent 自行检索资料、撰写文案、挑选画面、配置字幕与配乐，
在渲染前给出带预览图的计划确认卡，确认后成片。

需求中未提及的创作决定，包括时长、风格、配色、镜数、字幕样式与转场，均由 Agent 依选题自行确定，
过程中不再追问；渲染前的计划确认卡是唯一的强制确认点。

## 功能特性

- **两条出片通道**
  - **素材剪辑**：上传视频或音频（也可粘贴链接），自动切分镜头、语音转写、理解画面、筛选分组、撰写文案、配音配乐、编排时间线并渲染。
  - **零素材图形科普片**：只需一个选题，Agent 联网检索资料，逐镜编写 React/TSX 动画代码，由 [Remotion](https://www.remotion.dev/) 逐帧渲染成片。
- **渲染前计划确认卡**：每镜一张预览静帧，附文案与时长、画幅、字幕、配乐摘要，确认后才开始渲染。
- **字幕**：撕纸高亮、整句、文字进画面、无字幕四种形态；位置（底部/居中/顶部）与入场动画（淡入/上浮/弹出/逐字卡拉 OK）可逐镜设置。
- **配乐卡点**：检测背景音乐节拍，将镜头切点对齐到拍上。
- **选区改**：成片后在画面上框选，或在时间轴上点选一段，仅重烧受影响的部分。
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
2. 在右上角「设置」中填入模型 API Key，保存后立即生效；
3. 在输入框发送需求，例如：

   > 做一条竖屏科普短视频，主题：为什么天空是蓝色的，2 分钟左右，字幕用撕纸高亮。

**验证启动**：`docker ps` 中 `creation-app`、`creation-editor`、`creation-remotion` 均为 `healthy`，
且 `docker logs creation-app` 出现 `[startup] Storyline 已接入 … 个剪辑节点`。若提示「无剪辑能力」，见常见问题。

**修改代码后**需重建镜像与容器，仅重建镜像不会替换正在运行的进程：

```bash
docker compose --profile app up -d --build
```

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

- **全自动 / 先出计划**：输入框旁的切换按钮。默认「全自动」，Agent 直接执行至渲染前；「先出计划」会先给出可逐项调整的计划卡再执行。服务端缺省入口通过 `run_server.py --entry auto|plan` 设置。
- **计划确认卡**：渲染前弹出，含每镜预览图、文案与整片参数。选择「我还要改」并说明修改点，Agent 修改后会再次请求确认。
- **配乐**：上传音频，或将歌曲放入曲库后直接以曲名指认；成片长于配乐时自动循环。
- **长片**：无旁白时整片时长由每镜停留时间累加得出，系统据此对齐目标时长；4 分钟左右建议 20~30 镜。

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
| `MOTION_QC` | 图形片质检档位。`preview`：渲染前由预览图人工把关，渲染后质检仅告警；`strict`：无人值守，质检不通过即打回 | `preview` |
| `MOTION_PREVIEW_STILLS` | 分镜通过后是否为每镜渲染预览静帧 | `1` |
| `MOTION_RENDER_PARALLEL` / `DG_RENDER_SLOTS` | 图形片并发渲染的镜数，两项需同步调整；16 GB 内存下保持 2 | `2` |

剪辑能力（视觉理解模型、ASR 模型、TTS 音色、场景切分阈值等）在 `backend/examples/storyline/config.toml`（本机）
与 `config.docker.toml`（容器）中配置，两份文件的节点白名单需保持一致。主服务的启动参数见 `python run_server.py --help`。

### 公网部署

1. 在 `.env` 中设置强密码（`PG_PASSWORD`、`MINIO_SECRET_KEY` 及对应的连接串）、`JWT_SECRET`，并设 `REGISTER_OPEN=0` 与 `QUOTA_LIMIT`；
2. 设置 `SITE_DOMAIN`，按 `deploy/Caddyfile` 中的注释启用 HTTPS 块；域名 A 记录指向本机并放行 80/443；
3. 数据库与 MinIO 端口仅绑定本机回环，对公网只暴露 Caddy 的 80/443。

## 目录结构

```
backend/
  agent_framework/       Agent 框架：对话主循环、工具注册与并发、上下文与压缩、技能、记忆、
                         执行快照（checkpoint）、计划门（plan/）、确认门、HTTP/WS 服务、存储层（storage/）
  storyline_server/      剪辑服务（MCP）：剪辑节点（nodes/）、图形片代码渲染链（motion/）、渲染调度
  prompts/               提示词库（Markdown，改文案不改代码）
  examples/
    skills/              内置技能，含零素材图形科普片 motion_explainer 及其镜头卡库与示例代码
    storyline/           剪辑服务配置（config.toml / config.docker.toml）
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

- **构建期自检**：渲染服务镜像构建时运行镜头代码检查规则的自测（`remotion/tests/gate1-lint.mjs`），不通过则构建失败。
- 仓库仅包含构建与运行所需的文件。后端测试套件、探针脚本、素材巡检脚本与设计文档在本地维护，不随仓库分发，详见 `.gitignore`。

## 常见问题

| 现象 | 处理 |
|---|---|
| 回复「无剪辑能力」 | 剪辑服务未启动或未连接。本机方式需先起 :8001 再起 :8000；容器方式确认 `creation-editor` 为 `healthy` 后重启 `creation-app` |
| 所有对话返回 402 或鉴权失败 | 模型账户余额不足或密钥错误，在「设置」中重新填写，无需重启 |
| 页面可打开但媒体无法播放 | `MINIO_PUBLIC_ENDPOINT` 不是浏览器可达的地址；从其他机器访问时改为本机局域网 IP:9000，并放行该端口 |
| 修改代码后未生效 | 容器方式需执行 `docker compose --profile app up -d --build` 重建容器；修改技能或提示词后同样需要重启 `app` 容器，它在启动时将技能同步进数据库 |
| 首次语音转写耗时较长 | 首次运行会下载 faster-whisper 模型权重，此后常驻本地 |
| 构建时 apt 报 `Failed to fetch` | 网络或代理对个别软件包丢包，重试构建或更换网络 |
| 图形片渲染耗时较长 | 渲染服务使用 CPU 软件渲染，约 0.1 秒/帧；可适当提高 `DG_RENDER_SLOTS` 与 `MOTION_RENDER_PARALLEL`，需注意内存占用 |

## 许可证

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，为非商业许可证：

- **允许**：学习、阅读、克隆运行、个人研究、教学、非营利组织内部使用，以及在此基础上修改。
- **禁止**：任何商业用途，包括出售、提供付费服务，或用于以营利为目的的产品与业务。
- **须保留**：版权与许可声明，即 `LICENSE` 顶部的 Required Notice 一行；修改之处需明确标注。

商业授权请联系作者。第三方依赖各依其自身许可证（MIT、Apache-2.0 等），不受本许可证约束。
