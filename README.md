# 智能创作助手（AI Video Editing Assistant）

一句话驱动的视频创作 Agent：**说出需求 → Agent 自己查资料、写文案、选画面、配字幕和音乐 → 渲染前给你一张计划确认卡 → 出片**。

没提到的创作决定（时长、风格、配色、镜数、字幕样式、转场……）由 Agent 按选题自己定，中途不追问；
你只在渲染前看一眼带预览图的计划卡，点确认即可。

## 功能

- **两条出片通道**
  - **素材剪辑**：上传视频/音频（或贴链接），自动切镜、转写、理解画面、筛选分组、写文案、配音、配乐、编排时间线并渲染。
  - **零素材图形科普片**：只给一个选题，Agent 联网查资料、逐镜写 React/TSX 动画代码，由 [Remotion](https://www.remotion.dev/) 逐帧渲染成片。
- **渲染前计划确认卡**：每镜一张预览静帧 + 文案 + 时长/画幅/字幕/配乐摘要，唯一的强制确认点。
- **字幕**：撕纸高亮、整句、文字进画面、无字幕四种形态；位置（底部/居中/顶部）与入场动画（淡入/上浮/弹出/逐字卡拉 OK）可逐镜设置。
- **配乐卡点**：自动检测背景音乐节拍，把镜头切点对齐到拍上。
- **选区改**：成片后在画面上框选或在轨道上点选一段，只重烧受影响的部分。
- **工程能力**：崩溃可续跑的执行快照、多用户隔离、按身份热读的模型密钥、技能（Skill）按需加载、MCP 扩展、长期记忆。

## 架构

```
浏览器（Vue 3 单页，登录拿 JWT）
   │  HTTP + WebSocket
   ▼
┌───────────────────────────┐   MCP（streamable HTTP）   ┌───────────────────────────┐
│ 主服务 :8000（Agent 大脑） │ ─────────────────────────▶ │ 剪辑服务 :8001（双手）     │
│ 对话循环 · 技能 · 记忆     │ ◀───────────────────────── │ 剪辑节点 DAG · ffmpeg ·    │
│ 确认门 · 执行快照 · 鉴权   │        产物 / 进度          │ MoviePy · 本地 ASR · TTS   │
└──────────┬────────────────┘                             └─────────────┬─────────────┘
           ▼                                                            ▼
   PostgreSQL（元数据）  MinIO（媒体文件）  Redis（可选）       Remotion 渲染服务 :8010
                                                               （Node + Chromium，逐帧渲染）
```

- 对外只有主服务一个入口；前端构建产物由主服务托管。
- 剪辑服务与渲染服务是独立进程：渲染是分钟级的重活，卡住也不影响对话。

## 快速开始（Docker，推荐）

**前置条件**：Docker（含 Compose v2），建议内存 ≥ 16 GB（渲染容器用软件 WebGL 逐帧渲染），一把 OpenAI 兼容的模型 API Key（默认对接 DeepSeek）。

```bash
git clone <仓库地址> && cd <仓库目录>
cp .env.example .env                          # 本机使用可直接用默认值
docker compose up -d                          # 存储层：PostgreSQL + MinIO + Redis
docker compose --profile app up -d --build    # 应用层：剪辑服务 + 渲染服务 + 主服务 + Caddy
```

首次构建需要下载依赖与浏览器，约 10~20 分钟。启动后：

1. 打开 **http://localhost**，注册一个账号；
2. 点右上角「设置」，填入模型 API Key（保存即生效，不用重启）；
3. 发一句需求，例如：

   > 做一条竖屏科普短视频，主题：为什么天空是蓝色的，2 分钟左右，杂志风，字幕用撕纸高亮。

**确认启动成功**：`docker ps` 里 `creation-app`、`creation-editor`、`creation-remotion` 均为 `healthy`；
`docker logs creation-app` 中出现 `[startup] Storyline 已接入 … 个剪辑节点`。若显示「无剪辑能力」，见下方常见问题。

**更新代码后**要重建镜像并重建容器，只建镜像不会替换正在运行的进程：

```bash
docker compose --profile app up -d --build
```

## 本机开发

```bash
docker compose up -d                          # 只起存储层
cp .env.example .env

cd backend
pip install -r requirements.txt -r requirements-storyline.txt
# PATH 上需要 ffmpeg / ffprobe；首次语音转写会下载 faster-whisper 模型权重

python run_storyline.py --config examples/storyline/config.toml   # ① 先起剪辑服务 :8001
python run_server.py --port 8000                                  # ② 再起主服务 :8000

cd ../frontend && npm install && npm run build                    # 前端产物由 :8000 托管
```

打开 **http://127.0.0.1:8000**。

- **必须先起 :8001 再起 :8000**：主服务启动时连接剪辑服务并拉取节点清单。
- 零素材图形科普片依赖 Remotion 渲染服务，它只在容器网络内可达；本机开发要用这条链，请用 Docker 方式，
  或自行把 `remotion` 服务的 8010 端口映射出来并设置 `REMOTION_URL=http://127.0.0.1:8010`。
- 依赖版本：Python 3.12+，Node 20+（前端），渲染服务镜像内为 Node 24。

## 使用说明

- **全自动 / 先出计划**：输入框旁的切换按钮。默认「全自动」，Agent 一路做到渲染前；「先出计划」会先给一张可逐项调整的计划卡再执行。服务端缺省入口可用 `run_server.py --entry auto|plan` 设置。
- **确认卡**：渲染前弹出，包含每镜预览图、文案与整片参数。选「我还要改」并写一句要改什么，Agent 改完会再给你确认。
- **配乐**：上传音频，或在曲库里放好歌曲后直接说曲名（如「背景音乐用 We Never」）。配乐比成片短时会自动循环。
- **长片**：无旁白时总时长由每镜停留时间决定，系统会按目标时长自动对齐；4 分钟左右建议 20~30 镜。

## 配置

所有配置都在 `.env`（由 `.env.example` 复制），模板里每一项都有注释。常用项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `PG_*`、`MINIO_*` | 数据库与对象存储连接 | 与 compose 默认凭证一致 |
| `MINIO_PUBLIC_ENDPOINT` | 给浏览器签媒体链接用的地址；从其他机器访问时改成本机局域网 IP:9000 | `127.0.0.1:9000` |
| `OPENAI_API_KEY` | 模型密钥的环境变量回落位；日常在页面「设置」里填即可 | 空 |
| `OPENAI_THINKING` | 模型思考模式 | `off` |
| `REGISTER_OPEN` | 是否开放自助注册，公网部署建议设 `0` | `1` |
| `QUOTA_LIMIT` / `QUOTA_WINDOW` | 每用户在窗口期内的 token 上限，`0` 表示不限制；公网部署务必设置 | `0` / `86400` |
| `JWT_SECRET` | 登录令牌签名密钥；留空则首次启动自动生成并入库 | 空 |
| `SITE_DOMAIN` | 公网域名（配合 `deploy/Caddyfile` 开启 HTTPS） | 空 |
| `MOTION_QC` | 图形片质检档：`preview` 渲染前人看预览图、渲后质检只提醒；`strict` 无人值守、质检不过就打回 | `preview` |
| `MOTION_PREVIEW_STILLS` | 分镜通过后是否给每镜渲预览静帧 | `1` |
| `MOTION_RENDER_PARALLEL` / `DG_RENDER_SLOTS` | 图形片同时渲染的镜数（两者一起调），16 GB 内存保持 2 | `2` |

剪辑能力（视觉理解模型、ASR 模型、TTS 音色、场景切分阈值等）在
`backend/examples/storyline/config.toml`（本机）与 `config.docker.toml`（容器）里配置，两份的节点白名单需保持一致。
主服务的启动参数可用 `python run_server.py --help` 查看。

### 公网部署

1. `.env` 中设置强密码（`PG_PASSWORD`、`MINIO_SECRET_KEY` 及对应的连接串）、`JWT_SECRET`、`REGISTER_OPEN=0`、`QUOTA_LIMIT`；
2. 设置 `SITE_DOMAIN`，按 `deploy/Caddyfile` 中的注释启用 HTTPS 块；域名 A 记录指向本机并放行 80/443；
3. 数据库与 MinIO 端口只绑定在本机回环，对公网只暴露 Caddy 的 80/443。

## 目录结构

```
backend/
  agent_framework/       Agent 框架：对话主循环、工具注册与并发、上下文与压缩、技能、记忆、
                         执行快照（checkpoint）、计划门（plan/）、确认门、HTTP/WS 服务、存储层（storage/）
  storyline_server/      剪辑服务（MCP）：剪辑节点（nodes/）、图形片代码渲染链（motion/）、渲染调度
  prompts/               提示词库（Markdown，改文案不改代码）
  examples/
    skills/              内置技能（含零素材图形科普片 motion_explainer 及其镜头卡库与示例代码）
    storyline/           剪辑服务配置（config.toml / config.docker.toml）
  run_server.py          主服务入口（:8000）
  run_storyline.py       剪辑服务入口（:8001）
remotion/                Remotion 渲染服务（server.mjs、母版 FilmLayout、设计 token、代码检查规则、
                         动效与质感夹具 src/demos/_fixtures、构建期自测 tests/）
frontend/                前端（Vue 3 + Vite，单文件 App.vue）
deploy/Caddyfile         反向代理配置
docker-compose.yml       存储层（默认）+ 应用层（profile: app）
Dockerfile               主服务与剪辑服务镜像（两阶段：构建前端 → Python 运行时）
.env.example             环境变量模板
```

## 测试

- **构建期自检**：渲染服务镜像构建时会运行镜头代码检查规则的自测（`remotion/tests/gate1-lint.mjs`），不通过则镜像构建失败。
- 仓库只包含构建与运行所需的文件；后端测试套件、探针脚本、素材巡检脚本与设计文档在本地维护，不随仓库分发（见 `.gitignore`）。

## 常见问题

| 现象 | 处理 |
|---|---|
| 回复「无剪辑能力」 | 剪辑服务没起来或没连上。本机方式要先起 :8001 再起 :8000；容器方式确认 `creation-editor` 为 `healthy` 后重启 `creation-app` |
| 所有对话报 402 / 鉴权失败 | 模型账户余额不足或密钥错误，在「设置」里重新填写即可，无需重启 |
| 页面能打开但媒体无法播放 | `MINIO_PUBLIC_ENDPOINT` 不是浏览器能访问到的地址；从其他机器访问时改为本机局域网 IP:9000，并放开该端口 |
| 改了代码没生效 | 容器方式需 `docker compose --profile app up -d --build` 重建容器；修改技能或提示词后也要重启 `app` 容器，它在启动时把技能同步进数据库 |
| 首次语音转写很慢 | 首次运行会下载 faster-whisper 模型（数百 MB），之后常驻 |
| 构建时 apt 报 `Failed to fetch` | 网络或代理对个别软件包丢包，重试构建或更换网络即可 |
| 图形片渲染很慢 | 渲染容器用 CPU 软件渲染，约 0.1 秒/帧；可适当提高 `DG_RENDER_SLOTS` 与 `MOTION_RENDER_PARALLEL`（注意内存） |

## 技术栈

Python 3.12 · FastAPI · SQLAlchemy（async）+ PostgreSQL 16 · MinIO · MCP（fastmcp / mcp）· OpenAI 兼容 SDK ·
ffmpeg · MoviePy · faster-whisper · edge-tts · Remotion 4 + React 19 · Vue 3 + Vite · Docker Compose · Caddy

## 许可证

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)（非商业许可证）。简单说：

- **可以**：学习、阅读、克隆下来自己跑、个人研究、教学、非营利组织内部使用，以及在此基础上改。
- **不可以**：任何商业用途——拿去卖、拿它做付费服务、用在以营利为目的的产品或公司业务里。
- **需要保留**：本仓库的版权声明与许可证声明（`LICENSE` 顶部的 Required Notice 一行），改过的地方要说明改了什么。

商业授权请另行联系作者。第三方依赖各依其自身许可证（MIT / Apache-2.0 等），不受本许可证影响。
