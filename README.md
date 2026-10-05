# 智能创作助手(AI Video Editing Assistant)

一句话驱动的视频创作 Agent:**说一句需求 → 自动出计划卡 → 确认后查资料/写文案/剪视频/渲染出片**。

## 架构总览

```
   浏览器
     │  访问 :8000(前端静态页 + API + WebSocket 同源)
     ▼
┌──────────────────────────┐        MCP 协议         ┌──────────────────────────┐
│ :8000  主服务(Agent 大脑)  │ ─────────────────────▶ │ :8001  剪辑服务(双手)       │
│ 对话·计划门·技能·记忆      │ ◀───────────────────── │ 21 节点剪辑 DAG            │
│ checkpoint·执行记录·工具库 │      产物与状态回传      │ ffmpeg·whisper·MoviePy     │
└───────────┬──────────────┘                         │ 渲染提交+轮询+看门狗         │
            ▼                                        └───────────┬──────────────┘
   ┌─────────────────────┐                                       ▼
   │ PostgreSQL(元数据)   │                            ffmpeg / 本地模型(进程内)
   │ MinIO(媒体字节)      │
   └─────────────────────┘
```

- **对外只有一个端口 :8000**(前端 build 后由它托管,没有独立前端端口);
- :8001 是后端内部的剪辑工序端口,只监听本机——渲染是分钟级 CPU 重活,独立进程保证它卡死时不影响对话。

## 目录结构

```
backend/               后端(Python 3.12+)
  agent_framework/     Agent 框架:主循环/工具/上下文/记忆/技能/checkpoint/团队/Web 层/存储
  storyline_server/    MCP 剪辑服务:21 个节点(DAG 契约)+ 渲染分发器
  prompts/             提示词库(markdown,改文案不改代码)
  examples/            剪辑服务配置 + 技能示例
  run_server.py        主服务入口(:8000)
  run_storyline.py     剪辑服务入口(:8001)
frontend/              前端(Vue 3 + Vite,构建产物由主服务托管)
docker-compose.yml     PostgreSQL 16 + MinIO(+ 可选 Redis)
.env.example           环境变量模板(复制为 .env)
```

## 快速开始

```bash
# ① 存储层
docker compose up -d
cp .env.example .env          # 本地默认值可直接用,上线改自己的凭证

# ② 后端(Python 3.12+)
cd backend
pip install -r requirements.txt -r requirements-storyline.txt
# PATH 上需有 ffmpeg/ffprobe;首次运行 ASR 需联网下载 faster-whisper 模型
python run_storyline.py --config examples/storyline/config.toml   # 先起剪辑服务(:8001)
python run_server.py --port 8000                                  # 再起主服务(:8000)

# ③ 前端(Node 18+)
cd frontend
npm install && npm run build   # 产物 dist/ 由主服务托管
```

打开 **http://127.0.0.1:8000**,页面右上「设置」里填一次模型密钥(写库即生效,
两个服务热读),或填在 `.env` 的 `OPENAI_API_KEY`。

## 使用流程

1. **给素材**:上传视频(大文件自动分片续传),或贴一条链接让 Agent 自己取;
2. **说需求**:一句话描述(如"把采访和空镜混剪,风景 80%,某首歌做配乐");
3. **计划卡**:Agent 先出剪辑方案卡(步骤 + 参数开关),你确认或要求换一版——确认前不会动手;
4. **执行出片**:按确认的计划调用剪辑节点(缺参数会弹窗问你),渲染进度条实时可见,成片卡带证据分级(哪些机器验过、哪些要人耳/人眼判断);
5. **迭代修改**:直接继续说("BGM 声音大一点"),或用「执行记录」回到任意一步换参数重跑——上游产物逐字节复用,不重算;
6. **沉淀复用**:一键把这次执行的流程提炼成技能(自动剔除失败重试,保留修正后的最终做法),下次同类任务直接复用;
7. **工具库**:前端可管理工具 / 技能(✎ 增删改查、上传技能包)/ 动态注册 MCP 服务(热连热断)。

## 核心能力

| 能力 | 说明 |
|---|---|
| 计划门 | 剪辑诉求先出计划卡(参数开关可调),确认后才开工——两段式,不造暂停态 |
| 剪辑 DAG | 21 个节点按依赖自动补齐,起点终点不唯一,支持提前终止 |
| 时间旅行 | 每轮一致点落库;回到任意一步换参数重跑,上游产物逐字节复用 |
| HITL | ask_user 弹窗 / 渲染前确认 / 审批门——挂起持久化,作答原地续跑 |
| 执行沉淀 | 一键把会话的执行流水提炼成可复用技能(净化→合并→泛化,LLM 润色可选) |
| 动态扩展 | 前端注册 MCP 服务(热连/热断)、上传技能包(.zip)、管理工具库 |
| 可恢复 | 崩溃自动接手重跑(多副本租约互斥);分片上传断点续传 |
| 弹窗硬保证 | 缺参数/要决策必弹窗;声称完成必有证据(五级证据分级) |

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · WebSocket · 自研 MQ(可切 Kafka) |
| 存储 | PostgreSQL(SQLAlchemy async)· MinIO(S3) |
| Agent | OpenAI 兼容 LLM(默认 DeepSeek)· MCP(FastMCP + 自研客户端) |
| 剪辑 | ffmpeg/ffprobe · MoviePy · faster-whisper(本地 ASR)· edge-tts |
| 前端 | Vue 3 · Vite |

## 环境变量(`.env`)

| 变量 | 说明 |
|---|---|
| `PG_DSN` | PostgreSQL 连接串(默认指向本机容器) |
| `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` | MinIO 四件套 |
| `OPENAI_API_KEY` | 模型密钥(OpenAI 兼容,默认 DeepSeek;页面「设置」可覆盖) |
| `YTDLP_JS_RUNTIMES` | 可选:链接取料所需 JS 运行时(deno/node) |

完整说明见 `.env.example` 注释。

## 系统依赖

- Python 3.12+;Node 18+;Docker
- **ffmpeg / ffprobe** 在 PATH(渲染与素材元数据)
- ASR 用本地 faster-whisper(首次自动下载模型);配音用 edge-tts(免费在线服务)
- YouTube 取料需网络出口与 JS 运行时(见 `.env.example` 注释)

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 回复「无剪辑能力」 | :8001 没起或没连上。**必须先起 :8001 再起 :8000**(主服务启动时建立 MCP 连接) |
| 一切对话都报 402 | 模型账户余额不足;页面「设置」重填密钥即热生效,不用重启 |
| 渲染进度不动 | 进度是真实上报的(逐帧);600 秒无进展会被看门狗按失败收口,不会永远挂着 |
| 首次 ASR 很慢 | faster-whisper 模型首次运行需下载(几百 MB),之后常驻 |
| 页面打不开 | 前端没 build(`cd frontend && npm run build`),或 :8000 没起 |
| YouTube 取料失败 | 需要网络出口;缺 JS 运行时按报错装 deno/node |

## 设计文档

架构设计与决策记录在 `backend/docs/`(开发者本地保留,不入库);核心机制:
checkpoint 指针行 + 一致点增量链、DAG 拦截器递归补齐、计划门两次 run、
渲染提交+轮询+看门狗、证据五级分级。
