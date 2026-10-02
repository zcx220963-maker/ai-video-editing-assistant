# 智能创作助手 · Agent 框架

面向内容创作者的 Agent：一句话驱动「查资料 → 写文案 → 剪视频 → 出片」。
外层是常驻 Agent 主循环（Message Queue → Session → AgentLoop → 工具），
剪辑能力由独立的 Storyline MCP Server 提供 19 个真实节点（切镜、画面理解、ASR、
文案、配音、BGM、三套时间线、渲染出片）。运行态全部落在 **PostgreSQL（元数据）+
MinIO（媒体字节）**，本机磁盘只留可丢弃的缓存与临时工作区。

设计文档：`docs/superpowers/specs/`
- `2026-09-19-real-storyline-server-design.md` — 真实剪辑节点与出片链路
- `2026-09-20-pg-minio-storage-design.md` — 上线存储层（PG + MinIO）、身份鉴权、批次 B1–B5
- `2026-09-23-checkpoint-fork-reducer-design.md` — checkpoint 指针行+一致点增量链、
  fork/时间旅行、剪辑 DAG 的读写集契约与并发分批、Storyline 作为剪辑节点唯一权威

---

## 1. 目录

```
agent_framework/        主框架：Agent / MQ / 上下文 / 记忆 / 技能 / checkpoint /
                        任务板 / 子 Agent / Web 接口层 / storage（PG + MinIO）
storyline_server/       真实剪辑节点（FastMCP server，:8001）
frontend/               Vue3 前端（构建产物 frontend/dist，由主服务直接托管）
examples/               配置样例与技能样例
prompts/                提示词库落盘的整段文案（系统提示 + 规划轮整段 + 子 Agent system + 5 类纠错说明，改文案不必动代码，见 §3.18）
docs/superpowers/specs/ 设计与决策记录
run_server.py           主服务装配（:8000）
run_storyline.py        Storyline MCP Server 装配（:8001）
scripts/                工具脚本（数据迁移、遗留产物引用改写 backfill_payload_refs.py、
                        BGM 导入、联网测试、框架演示、
                        runs_cli.py = 不开浏览器的执行记录 / 时间旅行客户端）
tests/                   离线验证（每个模块一份，`python tests/test_xxx.py` 直接跑）
.smoke/                 真机冒烟（需容器与模型密钥，判据钉在真 PG 行 / 真 MinIO 字节）
```

## 2. 前置

| 依赖 | 说明 |
|---|---|
| Python 3.12+ | `pip install -r requirements.txt -r requirements-storyline.txt` |
| ffmpeg / ffprobe | 在 `PATH` 上；渲染与素材元数据都靠它，**留在本地不进存储层** |
| Docker Desktop | 起 PostgreSQL + MinIO（`docker-compose.yml`）；也可换原生实例或云实例，只改 `.env` |
| 模型密钥 | **一把就够**：文案/对话与画面理解走同一个服务商的同一个多模态模型（`deepseek-flash`）。填在页面「设置」里（写进 PG `app_secrets`，两个服务热读，不用重启）；`OPENAI_API_KEY` 是回落位（同层还认 `DEEPSEEK_API_KEY` / `SILICONFLOW_API_KEY` 两个历史名字，按顺序取第一把非空的），见 §3.7。**代码与配置里不写死、不打印** |

`requirements.txt` 里的 `yt-dlp` 是「按链接取素材」的第三条策略（站点播放器解析）。
它的站点解析器会随对方改版失效——遇到 `yt-dlp 解析失败` 先 `pip install -U yt-dlp`
（2026-09-21 B 站取料报 HTTP 412 就是这个原因，2026.8.19 已修）。需要登录态的站点，
把浏览器导出的 cookie **文件路径**写进环境变量 `YTDLP_COOKIES_FILE`（凭证值不进代码、日志、数据库）。

**YouTube 还要一个 JS 运行时**：签名与 `n` 参数挑战必须由它解，缺了 yt-dlp 只会回一句
`Requires JavaScript`。取料时代码按 yt-dlp 自己的优先级探测 `PATH` 上的
`deno / node / quickjs / bun`（Node 需 ≥22），一个都没探到才报可操作的一条。
要显式指定或钉死路径就设 `YTDLP_JS_RUNTIMES=node;deno`（`name:可执行文件或所在目录`）；
deno 是 yt-dlp 的首选，装它只需一条官方 install 脚本，不装也能用现成的 Node。

## 3. 部署

### 3.1 起存储层

```bash
docker compose up -d        # creation-pg :5432 + creation-minio :9000（控制台 :9001）
# redis :6379 是可选的第三个服务，只有多副本回投广播才需要（§3.11）：
#   docker compose up -d redis
```

容器有两条本机事实要保留（已写在 `docker-compose.yml` 注释里）：镜像加速源对
`docker.io` 的 minio 库返回 403，所以 MinIO 固定用 `quay.io`；项目目录名含中文时
compose 无法从路径推导项目名，所以 yml 顶部写死了 `name: creation-storage`。

### 3.2 填配置

```bash
copy .env.example .env      # Windows；*nix 用 cp
```

`.env` 里必须齐的五项：`PG_DSN`、`MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、
`MINIO_SECRET_KEY`、`MINIO_BUCKET`（默认值与本机容器一致，上线改成自己的实例）。
`docker compose` 另读 `PG_USER` / `PG_PASSWORD` / `PG_DB`，改了要同步改 `PG_DSN`。

PG 连接池除 `PG_POOL_SIZE` 外还有三条**可选**参数（都有默认值，不改也能跑）：
`PG_POOL_MAX_OVERFLOW`（10，长任务与 HTTP handler 共用这一池，池满时溢出几条比排队等超时好）、
`PG_POOL_PRE_PING`（开，取连接前先 ping——容器重启或 PG 侧掐掉空闲连接后，池里那条是死的，
不 ping 就是一次 `StorageUnavailable`）、`PG_POOL_RECYCLE_SEC`（1800，连接活够久就换新）。

剪辑参数与能力在 `examples/storyline/config.toml`：`[storage]`（后端、临时工作区与
内容缓存根、LRU 上限）、`[capabilities]`（ASR/TTS/VL、超时、字幕字体目录
`font_dirs`——Linux 部署把 `C:/Windows/Fonts` 换成本机字体目录）、`[local_mcp_server]`
（端口与 19 节点白名单）。**这个文件随仓库发布，所以里面只有环境变量名，没有密钥值。**

### 3.3 启动顺序（两台服务）

```bash
# ① 先起 Storyline（:8001）——主服务在启动时建立 MCP 连接，晚起就要重启主服务
python -u run_storyline.py --config examples/storyline/config.toml

# ② 再起主服务（:8000）
python -u run_server.py --port 8000 --max-iterations 24
```

主服务启动日志要看到这三行，缺一行就是没接上：

```
[startup] storage=pg_minio 已连通并完成建表校验
[startup] 内部身份已登记：['default', 'cron']
[startup] 技能库已按 examples\skills 刷新 2 个技能：[…]
[startup] Storyline 已接入 N 个剪辑节点（DAG 契约 M 节点，rerun_from 可用）
```

（N 取自 `config.toml` 白名单里连上的工具数，M 是服务端返回的 DAG 契约节点数。**两者本就不该相等**：
白名单里除了 M 个 DAG 节点还额外放行 `read_node_history`、`render_status` 这类不进图的辅助工具，
所以本机现况是 N=21、M=19。N<M 才是真断了一半，N>M 是正常配置。）

`default` / `cron` 是进程内部身份（定时任务的执行身份、未进入任何 run 时的缺省作用域），
由启动时一次性登记，**业务写入路径不再自建用户行**——来路不明的 `user_id` 会被
`conversations` / `materials` / `memories` 的外键直接拒绝。它们带的是随机占位哈希，
不对应任何可用凭证：身份存在 ≠ 可登录。

Storyline 连不上时**没有兜底假节点**：启动打
`[startup][warn] Storyline MCP（…）未连通：本服务无剪辑能力`，工具表里一个剪辑节点都不注册，
`rerun_from` 与 `plan_editing_team` 会如实回绝而不是交一张空板；检索、文案、文件、
链接取料等其余链路照常可用。（以前"连不上就退回本地 mock"看着不断链，实际是第二套 DAG：
远程 MCP 工具绕过本地 Interceptor，依赖补齐与 `require_prior_kind` 校验只在 mock 侧生效——
同一件事有两个真相。`agent_framework/video_editing.py` 现在只剩离线测试夹具的身份。）

### 3.3.1 在同一套 PG 上另起实例时

崩溃恢复的口径是「接手库里**所有** `running`/`failed` 的 run」——单机部署正确，
但拿同一套 PG 另起临时实例（冒烟、调试、第二台开发机）时，它会替别人把在途执行重放一遍；
若那个身份恰好没配模型密钥，还会把人家那条 run 顶成 `failed`。
所以临时实例一律带 `--no-resume`（`.smoke/` 下的脚本都这么做）。

### 3.4 首次上线迁数据

老版本把运行态写在本地磁盘（`.runtime/`、`.storyline/`）。**先迁数据，再对外服务**：
遗留身份没有凭证，未迁的行在崩溃恢复时会被外键拒掉（只告警，不崩服务）。

```bash
python -u scripts/run_migrate.py --dry-run                 # 只扫只算，打印对账表，不写一行
python -u scripts/run_migrate.py                           # 真迁；幂等，可重跑
python -u scripts/run_migrate.py --emit-credentials f.txt  # 给遗留身份现发可登录 token，只写进该文件
python -u scripts/run_migrate.py --emit-credentials f.txt --reissue-unclaimed
                                                           # 库已迁完（行早在了、只有占位哈希）时补发
```

幂等按类别各有自然键（users=遗留 id、messages=`{文件}#{行号}`、checkpoints=`run_id`、
jobs=id+name、memories=(user,category)、artifacts=(session,artifact,node)、
materials=(owner,sha256)、render_jobs=(session,artifact)），第二遍只会看到「跳过」，
且**从不覆盖库里已有的行**。遗留 checkpoint 迁进来是两行协作：整条 `messages` 成为
`checkpoint_entries` 的一条 `full` 基准，`checkpoints` 只留指针行（`head_seq=0`）。
`--emit-credentials` 不传时，遗留身份拿到的是不可登录的占位哈希。
源文件迁完仍留在磁盘上，不自动删。

**迁完之后补凭证要 `--reissue-unclaimed`**：那时 `users()` 见行即跳，光给 `--emit-credentials`
一类零迁入、文件也不生成。加了它才把「磁盘上有遗留目录 + 库里只有占位哈希 + 那份文件里没记过」
的行轮换成真 token。库里只有一串 sha256，认不出它是占位还是真发过凭证，所以**那份文件本身就是
发放记录**：文件里写过的 id 永不轮换——换哈希等于把本人手上那把当场作废。

本机现状（2026-09-29 补凭证，**这条拍板已结**——选了「发」，不是「永久不做」）：
6 个遗留身份（`ask-link-user` / `smoke2` / `u-b2smoke` /
`u_smoke` 四个冒烟遗留 + `u-n2zpkh7s` / `u-uaaonqg0` 两个 B5 之前浏览器自造的 id）已轮换并
落盘到 `.runtime/legacy-credentials.txt`（6 行，token 值不落标准输出）；拿其中每一行打常驻
:8000 的 `GET /convs` 全部 200、各自的历史读得到；同一行命令再跑一遍是「迁入 0 / 文件仍 6 行」。
一处如实：`chmod 0600` 在 Windows 上是装饰性的，该文件实测 644——它的保护只有「落在本地
`.runtime/` 目录里」这一条，别把它同步进任何仓库。

本机现状（2026-09-23 复核）：`run_migrate.py --dry-run` 八类全部报**迁入 0**——users / messages /
scheduled_jobs / artifacts 都已在库（逐条「跳过：已存在，不覆盖库里较新的运行态」），
`.runtime/checkpoints/` 那 11 个 JSON 也 11/11 按 `run_id` 查得到，各成链首一条 `full` 基准。
盘上源文件按上面那条口径留在原地不删，要清盘得人工确认。幂等键由 `test_migrate.py` 钉住。

**迁完再补一刀引用**：迁移把遗留产物行原样搬进 `artifacts.payload`，其中引用本机路径的那些
不会自己变成对象键——`scripts/backfill_payload_refs.py`（§3.12）才是让旧产物在新机器上读得通
的那一步，dry-run 先看计数，再 `--apply`。

本机现状（2026-09-29 复核，账落到纸面）：`backfill_payload_refs.py` **不带参数就是 dry-run**
（写库要显式 `--apply`），当日在共享 PG 上重跑一遍的结果是「扫过产物行 547，其中 113 行带本机
绝对路径；命中 **0 处可改写**、14803 处救不回来」并明确标注「（dry-run：没有写库。加 --apply 才落。）」——
即**这台机器上这一步早已收口，今天没有改动任何一行数据**；剩下的 113 行是 §6 记的既不在盘上也不在
MinIO 的遗留引用，原样保留、不做假引用。

### 3.5 前端

```bash
cd frontend && npm install && npm run build        # 产物 frontend/dist
```

`run_server.py` 默认托管 `frontend/dist`（`--static-dir ""` 可关掉只提供 API）。
`frontend/dist/` 在 `.gitignore` 里（构建产物不入库），所以**新克隆的仓库必须先跑上面那条
`npm run build` 才打得出页面**——只起服务不 build，浏览器拿到的是 404 而 API 照常工作。
浏览器只需知道一件事：**`localStorage` 里只有 `ca.token`**，会话列表与历史全部由
服务端按 token 反查回来——换浏览器、换机器、清缓存都不丢历史。

### 3.6 凭证怎么用

```bash
# 1) 取一份身份（明文 token 只在这一次响应里出现，库里永远只有 sha256）
curl -X POST http://127.0.0.1:8000/register -H "Content-Type: application/json" \
     -d '{"device_name":"我的浏览器"}'          # → {"user_id":"u-…","token":"…"}

# 2) HTTP 一律带 Bearer；请求里没有 user_id 这个字段，它由 token 反查
curl -X POST http://127.0.0.1:8000/chat -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"conversation_id":"c1","message":"…"}'

# 3) 浏览器 WebSocket 不能带自定义头，所以凭证走查询参数
#    ws://127.0.0.1:8000/ws/{conversation_id}?token=$TOKEN
```

`/health` 与 `/register` 是仅有的两个不鉴权端点。跨 owner 的读取返回「空」而不是 403
（不泄露会话存在性），跨 owner 的写入返回 409，别人的 `run_id` 一律按 404 处理。
会话面：`GET /whoami`、`GET /convs`、
`GET /convs/{id}/messages`（附件回成带 presigned URL 的展示形状）、`POST /convs/rename`、
`DELETE /convs/{id}`。素材面：`POST /upload`（原始字节流直写 MinIO）、
`POST /fetch_media`（按链接取料），两者返回同一个 `material_id` 形状，
剪辑侧统一是 `load_media(material_ids=[…])`。设置面：`GET /settings`、
`POST /settings/api-key`、`POST /settings/test`（都只认 token 反查出的身份，见 §3.7）。
执行面（见 §3.9）：`GET /convs/{id}/runs`、`GET /convs/{id}/runs/active`、
`GET /runs/{id}`、`GET /runs/{id}/history`、`POST /runs/{id}/fork`、`POST /runs/{id}/resume`。
渲染面（见 §3.10）：`POST /render_direct`（跳过 LLM 直接渲染当前时间线，`wait_sec` 内联等一小段）、
`GET /render_status?artifact_id=&conv_id=`（进度与终态产物）。

### 3.7 模型密钥：页面「设置」里填一次

不用改环境变量、也不用重启两个进程。用户在页面右上「设置」里填 key →
`POST /settings/api-key` → 写进 PG 的 `app_secrets`（主键 `(user_id, key_name)`）→
主服务与 Storyline 各自在**每次请求前**按当前身份回源：

```
前端配置（app_secrets，按 user_id）
  → 环境变量（按 OPENAI_API_KEY → DEEPSEEK_API_KEY → SILICONFLOW_API_KEY 的顺序，取第一把非空的）
  → 进程内回落位
```

`agent_framework/secrets.py` 是这条优先级的唯一实现，`Storage.start()` 把存储句柄绑进去
（:8000 与 :8001 共用同一份启动路径，所以两边都能热读）。写入方所在进程靠 `invalidate()`
立刻生效，另一进程最多晚 `CACHE_TTL_SEC`（5 秒）跟上。存储层抖动不阻断模型调用——读库
异常只会退回后面的回落层。

三条口径：

- **对外一律掩码**。`GET /settings`、`POST /settings/api-key`、`POST /settings/test` 的响应、
  日志、报错文本里只出现 `sk-…****abcd` 这种形状（`SecretsRepo.mask`）与**来源名**，
  从不出现密钥值；`app_secrets` 里存明文是刻意的取舍——两个进程都要拿它真签发请求，
  而这张表按 `user_id` 外键归属，删用户即 CASCADE 删行。
- **测试连接真的发两次请求**（`agent_framework/model_probe.py`）：一次文本、一次**带图**
  （固定的一张 48×32 渐变图）。只测文本会漏掉「key 有效但模型不吃图」这一类故障，
  而那正是视觉理解整链静默降级、画面描述全靠占位的起点。两路各自回报耗时与服务端原文。
- **`/settings/test` 走的是生产同款通道**：文本那一路直接调运行时那个 LLM client
  （含请求期换 key 的 `with_options`），带图那一路打 `deepseek-flash` 的 `image_url`，
  所以它验的是「稍后真跑一条片子会怎么走」，不是另一个平行实现。

`.env` / `OPENAI_API_KEY` 仍然是有效回落层（容器化部署、CI、还没打开过页面的用户）；
同一层现在还认 `DEEPSEEK_API_KEY` 与 `SILICONFLOW_API_KEY` 这两个历史名字——原先**没有任何代码读它们**，
用户在 `.env` 里填了、静默无效（审计第 7 条）。顺序即优先级，且三者是同一层的三个名字、不是三把
不同的 key；来源名报的是**实际命中的那一个变量名**（`环境变量 DEEPSEEK_API_KEY`），诊断据此说得清
读到的是哪一把。`llm_openai.py` 顶部那个 `MY_API_KEY` 常量只剩「本机开发最后一级」的定位，留空即可。

### 3.8 思考模式：默认关，两条通道各留开关

`deepseek-flash` 是思考型模型，默认先写一大段 `reasoning_content` 再答题。这个项目的时间
花在**成百次小请求**上（`understand_clips` 逐镜一次请求，一段片子就是上百次带图往返），
所以两条通道都默认关掉思考：

```
文本/对话：llm_openai.OpenAICompatClient(thinking=…) → 环境变量 OPENAI_THINKING=on/off
剪辑画面：examples/storyline/config.toml → [capabilities] vl_thinking = true/false
```

真机实测（`.smoke/thinking_off_smoke.py`，交替采样取中位）：文本 488ms vs 1018ms（**2.1×**）、
带图 747ms vs 1293ms（**1.7×**），描述要素与 tool_calls 都没退化。

两个必须记住的坑：

- 生效的写法只有 `{"thinking": {"type": "disabled"}}`（或 `reasoning_effort: "none"`）。
  `enable_thinking=false` 与 `{"reasoning": {"effort": "none"}}` 服务端**默默收下但照样思考**。
- openai SDK 的 `create()` 没有 `**kwargs`，顶层传 `thinking=` 会 `TypeError`——必须走
  `extra_body`（`_kwargs` 里这么做，`test_llm_openai.py` 按真 SDK 的参数名表防漂移）；
  Storyline 侧是裸 HTTP，直接并进 payload。两处共用 `llm_openai.THINKING_OFF` 这一个常量。

需要长推理时把开关打开即可：主 LLM 传 `thinking=True`（或设 `OPENAI_THINKING=on`），
剪辑侧改 `vl_thinking = true`；`/settings/test` 的自检结果里也会报当前是开还是关。
页面右上「设置」同样显示当前值——它取自**运行时真正在用的那个 client**（`Agent` 把 llm
交给了 `agent.runner`，所以 `_runtime_llm()` 顺着 runner 找；只看 `agent.llm` 会永远落到共享默认）。

### 3.9 执行记录、时间旅行与分叉

一次 `run` = 一次 Agent 执行。它的状态分两处存：PG `checkpoints` 是**指针行**
（走到第几轮、链尾在哪、什么状态、占着哪份剪辑产物），`checkpoint_entries` 是**一致点增量链**
（每行只存自上一致点新增的消息；上下文压缩改写了旧消息时那一条退化成 `full` 基准）。
所以恢复不再是"把整条对话重写一遍"，回到第 3 步换分支也不再需要重跑一次 LLM。

```bash
# 本会话走过哪些执行（含分叉出来的），以及某次执行走过哪些一致点
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/convs/c1/runs
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/runs/RUN/history
# → {"points":[{"seq":0,"kind":"full","messages":3,"tools":[]},
#              {"seq":3,"kind":"delta","messages":2,"tools":["select_BGM"]},…]}
# tools = 那一步落下的工具调用，正是「回到它之前」时要重做的那些节点

# 回到 seq=4 那一步，重做 select_BGM 及其下游（切镜/ASR/画面理解全部复用）
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"at_seq":4,"rerun_nodes":["select_BGM"],"message":"换成钢琴版再出一版"}' \
     http://127.0.0.1:8000/runs/RUN/fork          # → {"run_id":"<子 run>","status":"queued"}
```

**不开浏览器的同一套入口**（`scripts/runs_cli.py`，走上面这些 HTTP 端点，不另开后端能力）：

```bash
python scripts/runs_cli.py register --token-file ~/.agent-tokens      # token 只写进文件，不上屏
export AGENT_TOKEN_FILE=~/.agent-tokens                               # 也可 --token-file 现给

python scripts/runs_cli.py chat  --conversation c1 --message "剪一条 30s 的" --wait --frames
python scripts/runs_cli.py runs  --conversation c1                        # 本会话全部执行（含分叉）
python scripts/runs_cli.py history --run RUN [--before-node select_BGM]   # 一致点链；顺带算出分叉 seq
python scripts/runs_cli.py fork   --run RUN --before-node select_BGM \
        --rerun-node select_BGM --message "换成钢琴版再出一版" --wait
python scripts/runs_cli.py resume --run RUN --at-seq 0                    # 崩溃恢复 = 不带 at_seq
```

`--before-node` 与 `--at-seq` 二选一，前者由客户端在链上找**该工具结果出现之前**的那个一致点——
和后端 `seq_before_tool` / 模型侧 `rerun_from` 是同一条规则；落在链首或链上没这个节点时直接回绝，
不硬回退到起点。凭证只从 `AGENT_TOKEN`、`--token-file`（一行 `user_id<TAB>token`）、`--token`
三处读（`--token` 是一次性调试位，值会出现在进程列表里），文件里追加过多个身份时用 `--user u-xxx` 选一个；任何情况下 token 的值都不打印。
`register` 对 `--token-file` 是**追加**不是覆写。真机那一段见 `.smoke/b12_cli_smoke.py`。

`fork`/`resume` 都**不直接执行**：服务端投一帧带 `action` 的 MQ 消息，流式回投、同会话串行、
错误回执全部复用 `/chat` 那一条路，子 `run_id` 先起好再回给客户端。三条口径：

- **子 run 换一份 `artifact_id`**，并把父 run 的产物集整份复制过去——试另一种 BGM 不会把
  父那一版改花。`rerun_nodes` 里每个节点都会连带其**全部下游**产物一起作废
  （下游由剪辑 DAG 契约算）；不作废的话服务端拦截器认为它们已完成，恰好跳过要求重跑的那一步。
- **父 run 就地置 `superseded`**：分叉出去后它已让位，不再进崩溃恢复与「继续」的候选。
- `resume` 带 `at_seq` 就是时间旅行，不带就是崩溃恢复——同一把入口。
  `/chat` 的 `resume:true` 表示"续跑本会话在途的那条 run"，不带这个标记的新消息**一定开新 run**。
- **分叉可以带一条新诉求**（`fork` 的 `message` / 工具的 `instruction`）：它既是子 run 指针行的
  `message`（执行记录面板上显示的就是它），也作为一条 user 消息接在回退后的上下文尾部。
  这条通道是真机冒烟踩出来的：一致点写在每次 LLM 调用之前，所以回到「select_BGM 之前」后
  链尾正是父那条旧指令——不带新诉求的模型分叉会原样重跑同一个选择，永远换不到另一首。

模型侧对应一个工具 `rerun_from(node, reason, instruction, run_id?)`：它只报节点名，分叉点由链上"该工具结果出现
之前"的一致点算出，`instruction` 就是这次重跑要照办的新要求；找不到该节点的执行记录、或它的结果落在
链首（前面没东西可复用），都如实回绝而不是硬回退到起点。走到第 8 步发现配乐不对时，模型自己就会用它。

`run_id` 让这一步跨出本轮：用户说「回到上次那一版换个配乐」时，分叉点在**另一条 run** 上，而模型手上
没有那个 id。做法不是把 id 塞进提示词，而是**让回绝文本当索引**——本轮链上没有该节点时，工具会列出
本会话里确实跑过它的那些执行（`run_id` / 一致点 seq / 状态 / 那条诉求 / 产物作用域），模型照着填回来重试。
跨轮只认**同一会话**的 run（别人的、已清理的、前缀有歧义的三种都如实回绝，绝不猜一条），并且
fork 让位的是被回溯那条老 run，本轮发起的那条由工具就地置 superseded——否则它停在 running，
崩溃恢复会重播一条已经被弃用的分支。真机那一段见 `.smoke/b10_cross_run_rerun_smoke.py`。

**前端入口**（`执行记录` 抽屉，就是这条后端链的可视化）：列本会话所有 run——状态（进行中/已完成/
已让位/失败）、轮次、一致点范围、产物集、`← 分自 父run 第 N 步`；点开一条看它的一致点链，每个点标出
那一步跑了哪些节点。点一个点即选它当分叉点：`at_seq` 自动取「这一步之前」，该步跑过的节点预勾上
（可改勾选、可另写一句新诉求），「从这里分叉重跑」打 `fork`、「从最新一致点续跑」打 `resume`，
分叉出的新 run 进度照常流回同一会话，父 run 在清单上就地变成「已让位」。

**同一批工具调用怎么并发**：剪辑节点从服务端取回机器可读契约
（`NodeContract(name, requires, reducer)` → `reads={store:<dep>}`、`writes={store:<self>}`），
调度规则只有两条——写集相交、或一方写集撞上另一方读集即冲突，冲突者退到下一批
（贪心且保持模型给出的顺序，`concurrency_safe=False` 的工具独占一批）。契约取不到时
保守串行：**正确性不依赖契约存在**。工具失败也不再是"以 Error 开头的字符串"，
而是类型化的 `ToolError(tool, detail)`。

### 3.10 渲染：提交 + 轮询

一次 1080p 渲染要跑几分钟，而 MCP 是一次阻塞的 JSON-RPC 往返：`render_video` 留在请求线程里，
客户端超时就是「一句话出片」稳定失败的那一面（历史上 `tool_timeout` 从 600s 一路抬到 1800s，
抬预算并不改变「一次调用等一部片子」这个形状）。现在执行位挪出请求生命周期：

- handler 只做**登记 + 起跑**（`storyline_server/render_jobs.py` 的 `RenderDispatcher`），
  渲染体仍在原节点里跑，MoviePy→ffmpeg 兜底、终态写库的语义都不变；
- 内联先等一小段（`render_grace_sec`，默认 20s，被 `render_wait_max_sec` 夹住）——短片一次调用
  就拿到 done，长片回一张进度视图加一句 `hint`，让模型继续查而不是提前收尾；
- 唯一真相是 PG `render_jobs` 行，所以**换个实例、进程重启之后照样查得到**；终态产物
  （`video/duration/width/height/title`）落在新增的 `result jsonb` 里，`media_url` 不进库——
  presigned 直链会过期，读时现签。

两个查询口同一形状（`Storage.render_view` 是它的唯一定义处）：

```bash
# 模型侧：MCP 工具（必须在 examples/storyline/config.toml 的 available_nodes 里，
# 否则模型收到「请调用 render_status」的 hint 却没有这个工具可点）
render_status(artifact_id="…")      # → {node, artifact_id, output, render:{status,stage,percent,…}}

# 页面/脚本侧：HTTP，同一个作用域算法（u:{user}:c:{conv} + artifact）
curl -H "Authorization: Bearer $TOKEN" \
     "http://127.0.0.1:8000/render_status?artifact_id=&conv_id=c1"
# → {"status":"running","stage":"encoding","percent":40,"seconds_since_update":2,…}
# → done 时把 result 平铺进 output 并补一条现签的 media_url
```

`POST /render_direct` 也换成同一形状（`wait_sec=0` 即纯提交）。配置项四个，都在
`[capabilities]`：`render_grace_sec`、`render_wait_max_sec`、`render_max_concurrent`
（渲染是 ffmpeg 级 CPU 活，同时开太多只会互相拖慢，默认 2）、`render_stall_sec`（停滞看门狗）。

悬挂行有两半，各由一个东西收：
**崩溃遗留**由启动时的 `reap_hanging` 收（提交后还没起跑就崩了，那一行同样在谎报「有活儿在干」，
超时未更新的 queued/running 一律置 failed，否则轮询端空等）；**进程活着但渲染再也不推进**的那一半
以前没人管——真机形状是一条 `running / encoding / 70` 挂了 23 分钟，`reap_hanging` 只在 :8001
启动时跑过，所以它既不会自己收口、等待方也就永不返回。现在 `RenderDispatcher` 起一个周期循环
（`[capabilities].render_stall_sec`，默认 600s），按「无进展时长」而不是「总耗时」收口：
编码阶段每 1% 就写一次行，真在跑的慢渲染不会被误杀；`render_stall_sec=0` 就是关掉，不留半个循环在读库。

这两个循环的**启停挂载点是进程，不是会话**（`_mount_process_lifespan` + `_booted` 闩）：mcp SDK 只把
`FastMCP(lifespan=)` 喂给 low-level Server，而后者由每个 streamable-http 会话各跑一遍——真机形状是
浏览器每重连一次就多起一个执行循环，且会话结束时把在跑的渲染任务连带取消、连接池也被关掉
（别的会话还在用）。挪到 ASGI 的 `app.router.lifespan_context` 之后，进程拉起/关掉各一次，
崩溃那一半仍交给下次启动的 `reap_hanging`。

渲染吞吐的另一半在**字幕层**：`_subtitle_layers` 把时间线里的字幕铺成与画面层并列的 TextClip
列表（一条字幕一层），交给最外层一次 `CompositeVideoClip` 合成。以前是逐条嵌套——每套上去一个
合成都要对整幅画面重跑一遍 PIL，五段字幕就是逐帧五遍，真机上一部 87 秒的 1080p 片子因此跑到半小时。

`encoding` 那一段的百分比是**真报上来的**，不是猜的：MoviePy 2.1.2 的 `write_videofile` 没有
`progress_callback` 参数，唯一能拿到逐帧计数的入口是 `logger=`（`default_bar_logger` 的 proglog
回调）。`_encode_logger` 就挂在那里，把「已编码帧 / 总帧」映射成 `render_jobs.percent` 的
70→98 区间并顺带刷新 `updated_at`——在此之前这一步只在进出时各写一次，进度条会整整几分钟停在
`编码输出 70%` 不动，看起来和卡死没有区别（控制台一个字符都不打印，UI 也就没有第二条信号源）。

前端：直接渲染那条路现在每 2s 轮一次 `/render_status`，按钮上走 `渲染中 42%`，终态才落成片卡；
30 分钟上限，到点如实停在进度视图。聊天窗口那条路的**代查**由 Agent 循环自己做
（`agent_framework/agent.py` 的 `_follow_inflight_renders`）：一轮工具跑完，只要结果里有
`render.status` 为 `queued`/`running` 的渲染视图，循环就自己按 `render_poll_sec`（默认 3s）调
`render_status`，最多再等 `render_follow_max_sec`（默认 1800s），直到 done/failed 才把那条结果
拼进上下文——所以「模型会不会自己去查」不再是这条链的成败点（见 §6）。三处出口
（`MediaCardHook` 的当轮回投、`record_rendered_media` 的 `qa.parts` 持久链接、WS 进度帧）
都照原路径触发，只是发起方从模型换成了循环。

聊天窗里那条**进度条**（不是按钮上那条）本轮把起步与推进的口径也换成硬事实，此前它一直不出现：

- `ToolTraceHook` 的 `tool_result` 帧在 `result` 之外**单独挂一条 `render` 视图**——`result` 是要
  进上下文的字符串，`_truncate(…, 600)` 会把尾部那个 `render` 块削掉，前端拿不到 status 就永远
  起步不了；现在进度字段独立成帧字段，截不截都与它无关。
- 起步不等 tool_result：`render_video` 的 **tool_call 帧**一到就先立起一条 `running` 的条
  （`artifact_id` 从入参里捞，捞不到用 `"_default"`），因为提交那一次调用本身要等 grace 才返回。
- 推进仍只认渲染视图（`queued/running/done/failed` + `stage/percent`），并且**不再对
  `"_default"` 放弃轮询**：`/render_status` 本来就能按会话作用域查行，特例只会让没有
  `artifact_id` 的那一类片子永远停在起步态。
- 收口：`done`/`failed` 落终态；tool_result 既无视图又带 `error` 时也收口成 `error`，
  否则一条报错会留下一个永不落下的进度条。

```bash
python tests/test_render_follow.py        # 离线 35 项：代查到 done、中间态不进上下文、
                                          # 预算用尽只如实说明、无 render_status 时行为不变
python tests/test_hooks.py                # 离线 21 项：含「result 截到 600 字后帧上的
                                          # render 视图仍完整、非渲染工具的 render 为 null」
PYTHONPATH=. python -u .smoke/b15_render_follow_smoke.py
# 真机：grace 关成 0 + 明令模型「拿到 queued 就收尾、不许查 render_status」，
# 成片卡与持久链接仍落地，且链上那些 render_status 的调用 id 全是 render_follow_*
python tests/test_render_watchdog.py      # 离线：进程内停滞看门狗收口、刚写过进度的慢渲染不误杀、
                                          # stall_sec=0 不建循环
# 真机聊天窗（:8000 浏览器 1s 采样）：0% → 编码输出 73% → 81% → 86% → ✓ 渲染完成 100%
```

`.smoke/b9_render_poll_smoke.py` 仍在，钉的是另一半：模型**自己**收到 queued + hint 时确实会去追
（真 DeepSeek 连查三次到 done）。两条合起来才说明「提示词那三处指向（节点描述、返回值 `hint`、
`available_nodes` 显注册 `render_status`）」是加分项而不是唯一依赖。

### 3.11 多副本回投广播（Redis）

单副本不用管这件事：OutBound 的一帧被本进程消费到，`Connection Manager` 直接在进程内的
`session_id → set[ws]` 表里找到连接逐条推。多副本（起 N 个 `run_server.py`，前面挂网关）
就断了——MQ 的消费组语义保证**一帧只被一个实例拿到**，而浏览器只登记在它所连的那个实例上，
于是帧被别的实例消费掉、本地查无连接，这一路流静默丢失（执行照跑、历史照落，用户看到的表现
是「发了消息不出字」）。

补法只加一跳，不改帧的形状：

```
消费到帧的那个实例      → PUBLISH outbound:broadcast  '<payload 原样 json>'
每个实例的订阅者        → 收到后先问本地：这条 session_id 有没有挂连接？
  有 → 走同一条 ConnectionManager.route（逐连接 send_json + 写环形缓冲）
  没有 → ConnectionManager.buffer_only：只写缓冲、不投递（占一块有界的影子预算，见下）
```

开广播时 OutBound 的消费入口从 `ConnectionManager` 换成 `OutboundBroadcaster`
（`agent_framework/broadcast.py`）：**同一 (topic, group) 只能有一个订阅者**，两处都订阅会在
Kafka 消费组里把一帧随机分给其中一个，另一处永远看不到它——所以是替换，不是叠加。没配
`--broadcast-redis-url` 时一切照旧，本地直连那条路径原样保留。

```bash
docker compose up -d redis                      # 可选服务，单副本不必起
python run_server.py --broadcast-redis-url redis://localhost:6379/0   # 每个副本都加
# 启动日志会如实宣告：回投广播: 经 Redis …（多副本） / 回投广播: 关闭（单副本直连…）
```

Redis 挂了不带走整帧：`OutboundBroadcaster.handle` 里 PUBLISH 抛异常时**退回本地投递**
（直接调本实例的 `connections.route`）。那正是广播之前的单副本口径——本实例挂着连接的会话照常
收到、环形缓冲照写，丢的只是跨副本那半程；broker 恢复后同一段代码重新走广播，不是退回一次就
永久本地。监听侧同理：频道读失败被吞掉、0.2s 后继续，不把服务带崩。

环形缓冲攒的是**完整一轮**：`answer` 帧一到，这一轮的中间帧（逐字 delta、进度）就地清掉，
**终答本身留着**（`ConnectionManager._prune_finished`，判据是 `run_id` 而不是「整条清空」）。
早期版本把 `answer` 直接排除在缓冲外，理由是「历史口 `GET /convs/{id}/messages` 会给全」——
但前端重连只重开 socket、不重拉历史，于是断线恰好落在收尾那几秒时答复哪儿都拿不到；
现在的取舍是「宁可让客户端按 `replayed: true` 去重，也不让答复消失」。同一会话里别的 run
正在写的帧不受影响（按 run 精确判，不按位置切）。

**影子预算的两条上界**：`shadow_sessions`（默认 64 条会话）与 `shadow_frames`（默认合计
2000 帧），超了**整条**丢掉最久没人碰的那条会话——不留半截进度，因为半截比没有更容易被误
当成全部。上界是常数，所以内存不随「会话数 × 副本数」涨；这也顺带堵掉两处更早存在的无界
增长：会话最后一次本地连接断开后它的 deque 以前永不受淘汰，两个观测计数（`sent` /
`published`）以前每帧永久追加一份。构造参数 `shadow_sessions=0` 可显式退回「整条跳过」，
但那是关掉功能的手段，不是默认。

两条边界，写明以免被当成已完成：

- **接管前后的进度**补得回了，但补的是「这个副本从订阅频道起见过的那段」：它启动之前
  发生的帧没有、被影子预算淘汰掉的（整条）也没有。要跨**重启**补全仍得把缓冲本身搬到
  共享存储——那仍是另一件事，本轮没做。
- **入站侧的用户亲和**不归这里：`/chat` 投进 InBound 后由消费组里任一实例执行，本来就不要求
  执行实例与 WS 实例相同；广播补的只是回投这半程。真正的 sticky 会话（同一用户固定打到同一副本）
  是网关层的配置，不影响正确性，只影响上面的第一条边界出现得多频繁。


### 3.12 产物里的文件引用：库里只存 `obj:` 对象键

`artifacts.payload` 是一次执行共享给下游、分叉与历史轮次的全部依据（fork 时 `clone_from` 把它
**逐字节复制**进新的作用域）。所以写进它的文件句柄必须是跨进程、跨机器、跨重启都认得的地址——
工作区绝对路径不是：它是本机临时目录，`sweep_stale` 会收、换实例就没有、分叉到另一台机器读不到。
现在的契约只有一种形状：

| 环节 | 形态 |
|---|---|
| 写（节点产出字节） | `Workspace.publish` / `publish_derived` 先把字节放进 MinIO，payload 里只留 `to_ref(object_key)`，即 `"obj:users/u_x/c_c_y/mat-50998b.mp4"` |
| 读（下游取字节） | `Workspace.localize_ref(value, dst_dir)` 是**唯一**的取字节入口：按引用从对象存储落到本次调用的目录，同名不互盖（sha1 槽位 + 安全名）、命中即直返、同目标并发有锁 |
| 渲染 | `_localize_timeline` 在 render 前把三条轨逐条 localize 进 `render/src/`，MoviePy/ffmpeg 只吃本地路径这件事被关在这一步里面 |

对象键只有三种布局（`mediaops.probe()` 从此不再返回 `path`——元数据里没有本机路径可用）：

```
users/{owner}/convs/{conv}/{material_id}{ext}          素材（上传/按链接取料）
renders/{safe_sess}/{artifact}.mp4                     成片
derived/{sess}/{artifact|_default}/{stage}/{name}      派生字节（voiceover / ai_transitions / bgm）
```

会失效的东西不当持久引用，这条也适用于链接：`media_url` 是一小时后就读不通的 presigned 直链，
`BaseNode.ephemeral` 让它**只回给调用方、不写进 `artifacts`**（读侧一律按 `video` 对象键现签，
见 §3.10 的 `render_view`）。

引用取不到字节 = 产物已失效，当场抛 `产物引用的字节不在本机：… 请重跑产出它的节点`，与渲染失败
走同一条收口（`render_jobs` 记 failed、回收 `render/`），**绝不产出引用死链的「成功」**。

迁移进来的老数据里那些绝对路径不会自己变干净：

```bash
python -u scripts/backfill_payload_refs.py --config examples/storyline/config.toml   # dry-run，只报不改
python -u scripts/backfill_payload_refs.py --config examples/storyline/config.toml --apply
```

改写只在**能证明字节还在**时发生：`material/mat-xxxx.ext` 反查 materials 表的 `object_key` 并
head 命中，其余「stage 目录里的本机文件仍可读」走 `publish_derived` 补成对象。两者都不成立就
原样留着并在报告里计数——编一条指向空气的引用比留个旧路径更坏。脚本幂等，`--apply` 后自动跑
第二遍复核（命中 0 处才算收口）。

### 3.13 大文件分片续传：进度在桶里，不在内存里

单请求 `POST /upload` 的失败半径是**整个文件**：GB 级素材断在半路，浏览器只能重头传一次，
服务端也已经吃掉一半天量的带宽。分片路径把失败半径收到**一片**，并且让「传到哪了」这个问题
有一个不需要谁来维护的答案。

```bash
# 1) 定切法：服务端说了算，回权威 part_size/part_count（片号从 0 起）
curl -X POST "http://127.0.0.1:8000/upload/init?conversation_id=c1&filename=片.mp4&size=201140573&sha256=<整文件指纹，可不给>"
#    → {"upload_id":"up-a35292cb","part_size":16777216,"part_count":12,"status":"uploading",
#       "parts":[{"index":0,"bytes":16777216,"sha256":"…"}],"received_bytes":…,"percent":…,"missing_parts":[11]}

# 2) 逐片 PUT：请求体就是这一片的原始字节（与 /upload 同样不做 multipart），query 带片号与该片指纹
curl -X PUT --data-binary @part11.bin -H "Authorization: Bearer $T" \
     "http://127.0.0.1:8000/upload/part?upload_id=up-a35292cb&part=11&sha256=<这一片的 sha256>"

# 3) 中途随时问进度（也可只重发 init——同一条活的会话会被原样接回来）
curl "http://127.0.0.1:8000/upload/status?upload_id=up-a35292cb"

# 4) 收齐 → 拼装入库，返回与 POST /upload 同形状的素材（material_id/object_key/url/…）
curl -X POST "http://127.0.0.1:8000/upload/complete?upload_id=up-a35292cb"
#    带内容校验的完整形状：请求体给一份**按序逐片**的本地摘要（条数必须等于 part_count）
curl -X POST "http://127.0.0.1:8000/upload/complete?upload_id=up-a35292cb" \
     -H "Content-Type: application/json" \
     -d '{"parts_sha256":["<第0片>","<第1片>","…共 12 条…"]}'
curl -X POST "http://127.0.0.1:8000/upload/abort?upload_id=up-a35292cb"     # 用户取消：字节与账本一起删净
```

| 事实 | 落在哪 |
|---|---|
| 「已有哪几片」 | `ObjectStore.list_prefix("uploads/{user}/convs/{conv}/{upload_id}/")` —— **桶是唯一真相**。片号定宽补零，所以字典序就是拼装顺序；`list_prefix` 一次列举带回 `bytes` 与 `x-amz-meta-sha256`（MinIO 用 `include_user_meta=True`，真机验过），不必逐片 `head` |
| 「该有多大」 | 由账本推算：除末片外都等于 `part_size`，末片是余数。没有任何需要人维护的进度状态，也就没有它说谎的机会 |
| 计划本身 | `upload_sessions`（`filename/total_bytes/part_size/part_count/sha256/status/material_id` + 归属与时戳）。**故意不建 UNIQUE 约束**：一条已完成的记录不该挡住日后同内容的再传 |
| 内容校验 | 三道，且**前端已把能开的都开上**：① 逐片 `sha256`（`PUT part?sha256=` 边写边核，不符回 422 并重传那一片）；② complete 时的**按序逐片摘要清单**（请求体 `parts_sha256`，与桶里真存下的字节逐片比，不符只作废那几片并回 422，`missing_parts` 立刻把它们还给续传逻辑）；③ init 时声明的整文件 `sha256`（拼装时核，不符回 422 且**不登记素材**）——②③ 是同一条整体校验的两种客户端形态，见下 |
| 成品 | 按序取回分片拼成一条流，喂给 `ingest_bytes(origin="upload")` —— 与 `POST /upload`、`/fetch_media` **同一条入库出口**，素材形状、探测、归一化、对象键布局全一致 |
| 收尸 | 三条：`complete` 当场删分片（删不净由清扫补）、`abort` 用户取消、后台清扫回收没动静的会话（启动即扫一次，之后按 `--upload-sweep-sec` 轮询，静置超过 `--upload-ttl-sec` 算过期；两者置 0 可关） |

代价说清楚：**拼装期间桶占用峰值约 2× 文件大小**（成品与分片短暂共存），换来的是断线只重传一片、
刷新页面/换浏览器/服务 `kill -9` 之后都能接回原处——后两条正是 `.smoke/b14_upload_resume_smoke.py`
真机钉住的（崩在 12 片里的第 11 片，重起的新进程只补那一片就出片）。

`--max-upload-mb` 现在同时管住 `/upload` 与分片路径（`init` 与 `complete` 都按它拒），
片长夹在 1MB～64MB、片数不超过 10000（超限自动抬片长而不是拒掉这次上传）。

前端（`frontend/src/App.vue`）按 **2MB** 分流：小文件仍走 `POST /upload` 单请求（四次往返对
短视频不值得），大文件走 `init → 逐片 PUT → complete`，单片用 `XMLHttpRequest` 发——`fetch`
报不出请求体写到哪了，进度就只能一片一跳。按钮上显 `…42%`，重选同一文件会回一句
「续传上了：服务端已有 N 片」，传输中旁边挂着**「取消上传」**按钮（→ `POST /upload/abort`，
已传分片当场从桶里删掉，不用等过期清扫收尸；小文件走单请求、没有会话可作废，故不显示此钮）。
逐片指纹走 `crypto.subtle`，非安全上下文（局域网 IP 直开）拿不到就如实降级为不声明。

**整体内容校验对浏览器是开着的**——以前那句「浏览器这侧不声明整文件 `sha256`」是个错的取舍：
它假设了"要核整文件就得把整个文件读进内存"，而 `crypto.subtle.digest` 没有流式 API，所以确实
算不出规范化的整文件摘要；但**逐片摘要早就在 `PUT` 的时候算过了**，把它们按序排成一列随
`complete` 交回去，内存 O(1)、零额外开销，校验强度对分片路径等价（每片都对上 ⇒ 整体对得上），
失败还比整文件指纹更精准——能对上是哪几片坏掉。于是服务端收下 `parts_sha256` 逐片比桶里真存下的
字节，不符就**只作废那几片**并回 422，`missing_parts` 把它们还给续传逻辑，客户端重传那几片再
`complete` 一次即可（`tests/test_upload_resume.py` ⑧b 与 `.smoke/b14_upload_resume_smoke.py` 都钉了
这条修复路径，包括"坏片删了、好片一块字节都没动"）。前端刻意**不拿服务端报回的指纹当本地基准**
（那等于让被校验的一方出题给自己判），而是重新读一遍本地切片现算。init 时声明整文件 `sha256`
那条路留着没删——它是一条更强的表述（一个值核死整体，不依赖客户端如实交一列清单），脚本/CLI
客户端仍走它；浏览器换成交逐片摘要列是**形态差异，不再是缺口**。

浏览器侧不是纸面接通，两条实例都真点过。**常驻 :8000（新代码）**：63.3MB 素材切成 64 片 ×1MB
传完，回「素材已就绪：断点续传验证.mp4（63.3MB，64 片内容与本地摘要逐片核对过）」，素材行的
文件名与拖进来的原名一字不差（拖拽路径的净化同经此验，A2 缺口清零）；传到约 25% 时刷新页面
（桶里留 18 片、账本仍 `uploading`）再拖同一文件，接回同一条 `upload_id` 并只补剩下的 46 片；
另起一条传到约 30% 点「取消上传」，回「服务端已传的 20 片分片已删除」，`uploads/` 前缀列举为空、
账本无 `uploading` 残留。**临时实例**（随机端口 + `--no-resume`）：43MB 切 42 片 ×1MB 全传完
（服务端日志 42 次 `PUT /upload/part` 每个都带 `sha256`，说明 `crypto.subtle` 在 localhost 这条路
上确实可用）；201MB 切 12 片 ×16MB，在第 11 片传输中 `Stop-Process` 打断服务并重起，页面回一句
「续传上了：服务端已有 11 片，只补了剩下的」，重启后那条会话只有 **1 次** PUT 加一次 `complete 200`，
附件条落出 `191.8MB · 140s`。测试期间自建的身份、素材行、分片会话与对象均已当场收回（复核为 0）。


### 3.14 界面上的中文名：一份词表、两个出口

以前「界面上别露机器名」是一条提示词期望（模型可以写「镜头切分」，也就可以不写），
外加前端一张手抄的 `TOOL_LABELS`。两处都会漏，而且漏的方式不同：提示词漏在概率上，
手抄表漏在「加了节点没人记得补」。现在这一层**全程无 LLM**：装配期汇总一张词表，
出口处按名字整词换轨。

```bash
# 词表就是 /tools 的回执（每个工具/skill 都挂一条 *_display）
curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/tools" | python -c "import json,sys;\
d=json.load(sys.stdin);print([(t['name'],t.get('name_display','')) for t in d['tools']][:6])"
#    → [('load_media', '素材入库'), ('split_shots', '镜头切分'), …]
# 启动日志同一件事报一次：[startup] 中文名词表已装载：47 条（工具 / 剪辑节点 / 技能）
```

三处声明汇入一张表（`run_server.sync_catalog`，可重复调用）：

| 来源 | 声明在哪 | 什么时候进表 |
|---|---|---|
| 框架工具 | 各 `Tool.display_name`（`ToolRegistry.displays()`） | 装配期 + `_startup` 各一次 |
| Storyline 剪辑节点 | MCP 标准的 `tool.title`（节点注册时写 `BaseNode.display_name`） | 连上 MCP 之后那次 `sync_catalog` |
| 技能 | `SKILL.md` frontmatter 的 `display` | 同上（`skill_displays()`） |

规划轮那一档特殊：`submit_plan` 只活在规划轮的独立注册表里，主注册表那次同步看不见它，
于是 `_planning_registry` 建表时顺手把自己的 `displays()` 并进词表（`agent.py`）。
漏掉这一步的现场就是浏览器上弹出「submit_plan」的工具气泡——离线全绿也照样露。

出口只有两处，覆盖全部回显面：

* **HTTP**：`create_app(default_response_class=CatalogJSONResponse)`——序列化前过一遍
  `rewrite_obj`，新加端点不可能"忘了包"；六个面（`/tools`、`/convs/*/messages`、
  `/runs/*`、`/plans/*`、`/artifacts`、`/render_status`）都走它。
* **WS**：`ConnectionManager._emit` 对非流式帧 `rewrite_obj`；流式 delta 走每连接一个
  `StreamRewriter`（残片扣住不发、代码段状态跨帧续判、`stream_end` 必 `flush()`）。

**双轨而不是改写**（`KEEP_KEYS` / `OPAQUE_KEYS`）：机器名是键不是文案——`store:{node}`、
`artifacts.node`、DAG 契约的 dict 键、fork 的 `rerun_nodes`、前端 `PIPE_ORDER` 的比对值，
换成中文会连带打断复用/作废/排序。所以键位决定三件事：在 `KEEP_KEYS` 里 → 原值保留并
另挂 `{键}_display`；在 `OPAQUE_KEYS` 里（文件名、对象键、链接、id）→ 那是用户内容，整条不动；
其余字符串 → 就地文本替换。自由文本里的英文原名由出口换轨，**不再**由计划校验打回。

前端那张 `TOOL_LABELS` 从「唯一来源」降级成**兜底**：第一来源是帧里的 `*_display` 与
`/tools` 的 `name_display`（启动时预热进 `serverLabels`），拉不到才退回手抄表、再退机器名。

**参数名走另一张表**（`PARAM_LABELS`，79 条），原因很实在：参数名大多是普通词
（`name` / `key` / `path` / `task` / `mode`），把它们并进文本替换的候选，正文里这些词也会被换掉
——「按 name 排序」变成「按 名称 排序」。所以参数只作为**键位**出现：出帧那一步给整帧挂一条
`arg_labels`（`{"target_duration_sec": "目标时长（秒）", …}`），前端按键位查，查不到才退本地
`ARG_LABELS`。工具气泡、计划卡与 `/tools` 的 `params_display` 都从这同一张表出，
「前端补了、后端没补」这一类不一致没有了来源。

### 3.15 计划门：剪辑诉求先出卡，点了才开工

剪辑类诉求不再"想想就动手"。默认入口就是规划轮（`consumer` 那侧写死，不是用户可关的开关），
规划轮的工具集里**物理不含**剪辑执行节点（`PlanGate.planning_registry` 把带 DAG 契约的工具
整批挡在门外），出口只有 `submit_plan` 或直接回答。两件事分开看：

* **两次 run，不造暂停**：规划轮就是一条普通 run（Run A，正常 `completed`），确认之后另起
  一条执行 run（Run B），指针行记 `plan_run_id` 指回父规划 run。没有"等待确认"这一格状态、
  没有 interrupt 状态机、checkpoint 那套一字未动，所以崩溃恢复与续跑语义不受影响。
* **枚举是承诺，自定义是诉求**：卡面上的参数开关值必须能在节点 schema（enum/布尔/带界数值）
  或 BGM 曲库真实标签里反查到——这些会直接拼进节点调用；自由文本永不进节点参数，
  只进 `<user_custom_requests>` 段（≤5 条、每条 ≤200 字，进 system 前闭合标签中和 + 尖括号转全角）。

```bash
# 1) 正常聊天入口投的就是规划轮（WS/HTTP 都行），出卡后 GET 这张卡
curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/plans/fa52a0d732d8"
#    → {"plan_run_id":"fa52a0d732d8","status":"completed","message":"把采访和空镜剪成一条旅行精华",
#       "plans":[{"plan_id":"p1","label":"…","goal":"…","steps":[
#          {"seq":1,"node":"load_media","node_display":"素材入库","why":"…","expectation":"…",
#           "tool_kind":"…","skills_hint":["highlight_extraction"],"skills_hint_display":["精华片段提取"],
#           "skippable":false,"skip_reason":"下游「镜头切分」依赖它的产物",
#           "param_options":[{"key":"bgm_style","display":"配乐风格",
#                             "options":[{"value":"piano","display":"钢琴","kind":"enum"}],
#                             "default":"piano"}],"requires":["asr"]}]}],
#       "warnings":["计划 p1 第 2 步（split_shots）本可跳过，但下游…，已置为不可跳。"]}
#    计划本体一律是服务端**自己校验过的那一份**；seq 从 1 起；无参数位时 plans[].steps[].param_options 为空。

# 2) 点确认：只回传「选了哪张卡、动了哪些开关、另填了什么」
curl -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  "http://127.0.0.1:8000/plans/fa52a0d732d8/confirm" \
  -d '{"selected_plan":"p1","param_finals":[{"step_seq":3,"key":"bgm_style","value":"none"}],
       "skips":[2],"overrides":[{"step_seq":4,"key":"target_duration_sec",
                                 "value":"片尾留一拍静默，别硬凑满 60 秒","kind":"custom_text"}],
       "message":"就这版，开工"}'
#    → {"run_id":"ed6631e85196","status":"queued"}        Run B，谱系指回 fa52a0d732d8
#    overrides 就是卡上的「其他…」：step_seq/key 可省（省 key 记作 _general 的整体补充），
#    kind 只能是 custom_text，条数与单条长度有上限；它永不进节点参数，只进诉求段。

# 3) 换一版：原话沿用那条规划 run 的（不用用户重打），feedback 是"为什么要另一版"
curl -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  "http://127.0.0.1:8000/plans/fa52a0d732d8/revise" -d '{"feedback":"太保守，想要节奏更快的一版"}'
#    → {"run_id":"f2129e4c37a9","status":"queued"}        新的规划 run、新的卡
```

`confirm` 的拒收**发生在跑模型之前**（`PlanGate.validate_execute`），所以伪造的 `selected_plan`、
跳过不可跳的步、第 6 条自定义诉求、不在选项里的枚举值都不会留下一条 run 行——
`/runs` 查不到、历史里也翻不出。`server` 那层只核归属与「卡存不存在」，枚举反查与转义都在门里。

四重校验（`PlanGate.validate`，纯代码，一次 LLM 都不叫）：① `node ∈ ToolRegistry ∩ Storyline
白名单`；② 步骤序与 `dag_contract.requires` 拓扑相容（前置排在后面就是错；前置**没上卡**允许，
拦截器会补齐）；③ `skills_hint` 存在且 `available` 且已登记中文名；④ `param_options` 的每个值
可反查枚举源，节点没有的参数不许在卡上造开关。`skippable` 只在没有下游踩着时成立，否则降级为
不可跳并把理由写进 `skip_reason`（同时出一条 warning——同一份 warnings 会落 plan 帧、Run A
指针行与落库片段，刷新后不会凭空少了告警）。

执行轮是**软约束**：两段注入是强提示，不是硬调度，不做逐步放行。事后由
`PlanReconcileHook` 算偏差（计划外步骤 / 声明了没调），**只观测不拦截**；`finalize_content`
把偏离理由补成终答原文，落 `cp.plan["audit"]` 与 `qa.parts`，实时那一路走 `plan reconciliation`
帧——三处同形，所以界面上的角标刷新后不消失。卡上声明的 `skills_hint` 在执行轮**预注入**正文，
不必模型自己去 `load_skill`。

对账与进度都只认**真的发生过**的调用，这靠两条帧上的事实撑着：

* `tool_call` / `tool_result` 各带一条 `call_id`（模型给的 `tool_call_id`）。同一批里两个同名调用
  （例如并行渲染两条时间线）以前只能按「第一个同名 `running` 气泡」配对，结果是谁先落地都能
  对上号——现在按 `call_id` 各回各家。
* 注册表里查无此名时抛 **`UnknownToolError`**（类型化，而不是混在一般异常里）：这种调用从未打到
  后端，于是帧上 `invoked=false`，前端不把它记进剪辑进度（规划轮挡下的一次试差不算「剪辑受阻」），
  `PlanReconcileHook` 也不把它收进 `calls_executed`——否则对账会把**没跑过的步当成跑过**。

执行轮**中途停下来问一句就收尾**是常态（要用户挑一版文案、确认一个口径），这类账不能就这么烂掉：
下一条用户消息进来时 `pending_continuation` 会查最近一条执行轮，若它确认过计划却仍有未履行步骤，
就在上下文里多挂一段 `<待续跑的执行轮>`——列出未履行的那几步、当时问的原话（摘要 + 闭合标签中和），
并说明「回话就是答复它，续跑那几步」。**只看最近一条执行轮**：它跑完了就没有待续，
不会把三条之前的旧账翻出来拦今天的新诉求。

「没出卡 / 没跑步骤也不许说成做了」是两句**服务端核对**，不靠提示词自觉：

* **规划轮**（`claims_plan_card`）：本轮没有一次 `submit_plan` 成功落地，终答的措辞却在声称卡
  已经给出（「计划卡已回投给你，请确认」「候选计划已经生成」）→ 该轮退回重写一次（预算 1），
  要么真出卡、要么如实说明没出卡；预算用尽就在终答末尾补一句事实，绝不带着假称收尾。
  反问（「要不要我先出计划卡？」）与「卡我暂时给不出：素材还没入库」不算假称——判据认的是
  完成态措辞，不是「计划」二字出现。
* **执行轮**（`claims_step_executed` + `_no_step_called`）：确认过的计划里至少有一步，本轮却
  一次步骤工具都没调（`render_status`、`read_node_history` 这类查询不算步骤），措辞在声称已产出
  （「渲染完成，成片返件如下」「时间线编排已经完成，时长 8.64 秒」）→ 同样退一次、再用尽补事实。
  判据**只看措辞、不要求答复点名节点**：真机漏过一条 `iteration=0` 的收尾，那句只说「渲染完成，
  成片返件如下」，点名判据认不出来就等于没卡照样交付。词表里认不到的名字不参与，所以宁可
  漏纠也不误伤正常回答。

旁路只有一条：`{"op":"execute"}`。它是给「到点自动跑」的定时任务用的——那条消息没有坐在屏幕前
的人来点确认，拦在规划轮里等于任务永不落地。`resume`（「继续」）也不算新诉求，仍续跑在途 run。

验证：离线 `tests/test_plan_gate.py`（145 项，四重校验与帧校验的每一件，含 ⑮ 节那两条假称判据的
词面——认得出真机漏过的说法，也不误伤反问与「给不出卡」）+
`tests/test_plan_flow.py`（68 项，真装配链路：规划轮工具集物理不含剪辑节点、两段注入与中和、
谱系、对账三处同形、consumer 分派、guard 退回一次与预算用尽补事实）
+ 真机 `.smoke/b16_plan_gate_smoke.py`（六段，含伪造确认帧
拒收于跑模型之前、`<approved_plan>` 探针注入、revise 出新卡，跑完自建数据零残留）。

代码不再住在一个 `plan_gate.py` 里（那一文件曾把 6 种职责混成 1526 行）：现在是
`agent_framework/plan/` 包，`gate.PlanGate` 只做装配与转发，事实/校验/编译/工具/文案/
对账/措辞判据/技能预注入/续跑查账各占一个模块，谁也不反向依赖装配层。对外的名字仍从
`agent_framework.plan` 取。同一轮把 `AgentOnceRun._drive` 从 325 行的 god method 收成
77 行骨架，切出去的六块各有名字（`_open_run` 组装本轮上下文、`_tool_round` 走三道门与
执行、`_suppress_repeat_render`、`_pause` 统一挂起、`_nudge_back` 退回重写、`_deliver`
收尾），控制流与原有注释照旧。**两份形状**（包依赖方向、`_drive` 不再膨胀、跨挂起的事实
不许再退回 `ctx.extras`）由 `tests/test_plan_layout.py`（31 项）钉着。

### 3.16 HITL：弹窗提问与渲染前确认（同一条 run 内挂起）

三条触发路径共用同一套挂起 / 回投 / 续跑 / 落盘机制——**同一条 run 停在中间等人工**。
这与 §3.15 计划门是两回事：计划门是「规划 run 出卡 + 执行 run 开工」两条 run，这里只有一条。

* **触发**：① `ask_user` 工具（模型主动提问，`agent_framework/ask_user.py`）；
  ② 渲染前确认门 `render_gate`（默认开，`--no-render-gate` 关）；
  ③ 工具自声明 `requires_approval=True`，或 `--approve-tools=render_video,delete_clip` 按名覆盖
  （**默认全关**，不擅自改变既有流程）。
* **挂起态 `awaiting_approval`**：`checkpoints.status` 新增的一格。`approval` 列同时存
  `pending_calls`（待批的工具调用描述）与结构化 `ask`（题面 / 选项 / `recommended` 徽标 /
  `custom_hint` / `allow_custom`；选项硬边界是「至少 1 个、至多 `MAX_OPTIONS=6` 个、key 不得重复」，
  打回文案里对模型的要求是「给 2~6 个有真实差异的选项」）+ `fallback_options`。
  挂起态刻意**不在** `_UNFINISHED` 里——
  人工挂着就是等人工，崩溃恢复不该把它当残留捞起来重跑。
* **刷新后重弹**：`GET /convs/{id}/runs/active` 的 `approval` 字段就是数据源（挂起态不在
  `running` 里，所以这个只读口要单独再看一眼 `awaiting_approval_for_session`）。
  2026-10-02 真机复验：挂在 :8000 的浏览器里刷新，「你手上的素材是什么情况？」那张卡原样回来，
  多题分页（`第 1 / 1 题`）、推荐徽标、✎ 自定义输入都在，WS 状态 `回投已连接`。
* **作答**：`POST /runs/{run_id}/approve`，体 `{decision, message, answers}`。
  `answers` 是「第几题选了哪个 key」的结构化回执，执行侧不必去解析前端拼的那句中文散文。
  与 `resume` 同构：这里只核归属与状态，投一帧 `action.op="approve"` 进 MQ，真正续跑在
  `Agent.approve`。非本人 404；状态不是 `awaiting_approval`（已批过 / 已结束 / 从未挂起）409。
* **答案怎么进模型上下文（两条硬纪律，都是真机踩出来的）**。`ask_user` 在挂起**之前**就执行完了，
  那条 tool 回执已经在链上，于是续跑时：
  ① **不得再补第二条同 `call_id` 的回执**（打真接口直接 400），用户的回话改以 `role=user` 追加。
  旧写法在「这批调用都已有回执」时什么都不写，模型只看到「问题已发给用户」而等不到答案，
  只能一遍遍重问同一道题——真机一条 run 连问 6 题、第 6 题还在重问第 1 题。
  ② `asked_this_run` 是每次 `_drive` 的局部量，续跑那一轮必须由 `_settle_pending` 把它**恢复回来**，
  否则那一轮正撞上 §3.17 的弹窗硬保证，续跑的每一轮都吃一条「但你没有调用 ask_user」的假打回。
  回归各钉一条判据：`tests/test_approval_gate.py` 用例 H（链上「答案」恰好一条 `role=user`、
  且不多第二条回执）、用例 I（续跑只多一次 LLM 调用、零条假打回）。
  修好后的真机形状（同一条 run 第 32–39 条消息）：`user "highlight"` →
  `assistant "你选了高光精选，方向定下来了"` → `tool ask_user(下一题)` → `user "…一次补齐…"` →
  不再重复提问，收尾给出方案，run 停在 `completed`（不是烧穿迭代预算）。
  **这条链上仍留下的边界**：修复之前落进链的那几题没有 `role=user` 答案（旧代码写的），
  模型看得到「问题已发给用户」却看不到回答，所以那条历史被污染的 run 会继续重问旧题——
  新答的会答一次少一次，老账只能靠重新发起会话。
* **渲染前确认**：`agent_framework/render_gate.py`。`RENDER_NODE="render_video"` 做成常量；
  `should_gate_render` 决定拦不拦，`build_render_ask` 把编排摘要写进题面（时长冲突会升级成一道
  专门的题），选项固定 `confirm_render` / `adjust_plan`，`decision_is_confirm` 才放行执行。
  复用 `ask` 那一套的好处是「刷新后重弹 / 多题分页 / 自定义输入」一次对两条路径都生效。
* **编排预览读模型**：`agent_framework/preview.py` + `GET /preview/{conversation_id}`。
  只读整理 Store 里已有的节点产物（`plan_timeline*` 时间线 / `group_clips` 分组 /
  `understand_clips` 画面 / `asr` 逐句 / `generate_voiceover`、`select_BGM` 声音），
  不触发剪辑、不调 LLM。`artifact_id` 留空时**不盲取 `_default`**——`rerun_from` 分叉给子 run
  换了 `art-xxxx` 作用域，盲取会让预览显示父 run 的旧编排而用户以为看的是这一次的；
  口径改成「本会话最近写过产物的那个作用域」。
* **离线 46 项**：`tests/test_approval_gate.py`（A approve 续跑 / B reject 跳过 / C 未标注不挂起 /
  D 按名覆盖 / E confirm 真执行挂起调用 / F `_repair_orphans` 不剥待批调用 /
  G ask_user 不补第二条回执 / H 答案以 `role=user` 进链 / I 续跑无假打回）。

### 3.17 弹窗硬保证：要用户拿主意就必须是弹窗

原先「弹不弹窗」完全由模型自觉：它调 `ask_user` 就弹，把问题写进正文就只是普通答复，代码不检查。
真机实测到过后者——正文里列「1. 风格：… 2. 时长：…」让用户自己打字挑。现在由
`agent_framework/ask_gate.py` 在**准备收尾那一刻**拦：

* **判据 `looks_like_asking_user`** 两级：强索取措辞（请你选 / 麻烦你 / 需要你上传 / 把链接发我 /
  素材在哪…）**不带问号也算**——用户点名的「要素材、贴链接」本来就是祈使句；
  弱索取（你想 / 要不要我 / 是否）必须**配问号**才算，否则「用户的诉求是…」这类叙述被误伤；
  以「你来定 / 听你的」结尾也算。正反例在 `.runtime/audit/calibrate_ask_gate.py` 上标定过。
* **打回**：本轮没有 `ask_user` 调用而文字在提问 → 追加一条服务端核对（`popup_nudge(planning)`，
  规划轮那版多一条正路「直接 submit_plan」）并要求重来。默认**只打回一次**
  （`popup_question_nudges=1`）——无上限会烧穿迭代预算。
* **用尽后如实交付**：不再打回，在正文末尾附一句 `NO_POPUP_NOTE`，让用户知道「这句本该是个弹窗」。
* **开关**：`AgentConfig.require_popup_questions`，默认 **True**。
* **离线 26 项**：`tests/test_ask_gate.py`（两级判据各自的正反例——含「需要你**知道**」这类
  告知不挨打、「请把链接发我」这种不带问号祈使句要挨打；打回恰好一次；预算用尽改附说明）。
* 与 §3.15 的 `claims_plan_card` 同族——都是「声称做了 X 就必须拿得出 X 的证据」。
  边界同样是词面判据：认得完成态说法，认不出一句刻意绕开的措辞；宁可漏纠也不误伤正常回答。

### 3.18 提示词库（`PromptLibrary`）：整段提示文本搬出 .py

`agent_framework/prompts.py`（`render_template` / `render_blocks` / `placeholders_in` /
`PromptLibrary` / `build_prompt_library`）+ 仓库自带的 `prompts/*.md`（8 份：`system_prompt.md`、
`planning_round.md`、`subagent_system.md`、`step_nudge.md`、`step_note.md`、`no_card_nudge.md`、
`no_card_note.md`、`no_card_structural_nudge.md`）。以前这些文案硬编码在 5 个 .py 里，改一句要动代码、走评审、
重新发布；`ContextBuilder` 的 `bootstrap_dir` 口子其实早就实现了，但生产装配从来没传过。

* **语义**：`render_template` **只替换明确提供的键，其余原样保留**。提示词里天然写着
  `{"plans": [...]}` 这类 JSON 片段，按 `format_map` 会 KeyError 或静默吃字符。
* **兜底**：目录不存在或某个文件缺失时落到内联默认值——提示词是核心资产，不能因为一个目录
  没拷过去就让模型收到空 system prompt。
* **接线**：`build_runtime(prompts_dir=)`；`None`（默认）= 用仓库自带的 `prompts/`，
  `False` = 关闭全走内联。启动日志第 (e2) 行会明说「提示词库已接线：N 份来自 …」或
  「提示词库未接目录：使用内联默认值」——这一条是给「动态加载」做证的，以前没人能从日志判断提示词从哪来。
* **规划轮整段也搬了**（2026-10-02，`prompts/planning_round.md`）。搬的边界是「措辞 + 哪一段
  出现」，不是「谁来算数据」：模板按空行分块，块首独占一行的 `{?key}` 是**守卫**——这个键没值
  就整块不出现；`render_blocks` 除了这条只有「渲染后为空的行不留空行」。节点白名单与可上卡开关
  仍由代码现取（`gate.whitelist()` / `gate.knob_facts()`，与卡面校验同一份判据）插进
  `{nodes}` / `{knobs}`。没有 if/for/嵌套——一旦要那些，就不是「改文案不必改代码」，
  而是往仓库里塞一门模板语言。接线：`build_runtime` 把 `prompt_library` 传给 `PlanGate`，
  `plan/prompt.py` 只备数据。
* **子 Agent 的 system 提示词同样落盘**（`prompts/subagent_system.md`，2026-10-02）。两处坑记下来：
  ① `SubAgentRunner.__init__` 的形参默认值原先直接写 `CHILD_SYSTEM_PROMPT`——**默认参数在 import
  时定格**，启动期再怎么刷新模块全局都进不了构造，现在形参是 `None`、构造时现取全局；
  ② `build_runtime` 里 `make_spawn_tool(...)` 很早就把 runner 连提示词一起构造好，所以提示词库
  与两条 `refresh` 必须排在它之前（库的构造因此挪到了 `build_runtime` 开头）。两条都由用例钉住。
* **漂移守卫**：`tests/test_prompt_library.py`（49 项）钉 `system_prompt.md` 与
  `DEFAULT_SYSTEM_PROMPT`、`planning_round.md` 与内联回落模板必须一致（模板那份只允许行尾空白差）。
  两处各写一份文案，靠人眼比对一定会漂——实测就漂过一次。
* **逐字金样**：`tests/test_planning_prompt_golden.py`（29 项）拿**迁移之前**从旧代码逐字捕获的
  9 条渲染结果（`tests/data/planning_round_golden.json`）比对现在的输出，内联回落与磁盘模板各跑
  一遍、要求字符串相等——差一个空格模型行为就可能漂，这里不比「意思差不多」。另钉一条接线守卫：
  `run_server.py` 构造 `PlanGate` 时必须真的带 `prompt_library`，防的正是「模板写了没人接」
  这个本项目死过一次的模式。

### 3.19 装配期一致性检查：点名的工具必须真的存在

`agent_framework/consistency.py`，启动时跑一次（§3 启动日志的第 (f) 步），非空只**显式告警**、
不拦启动。防的是四份互不校验的清单漂移：手写进提示词的工具名 / 真实注册的工具名 / 计划门白名单 /
技能正文里让模型调用的工具名。真机事故：提示词写 `read_node_artifact`、真实工具叫
`read_node_history` → 模型照提示词调 → `UnknownToolError` → 调用从未发生 → 白烧一轮迭代预算，
表现成「流程走到一半莫名其妙断了」。

* **方向 A（精确、零假阳性）**：`WATCHED` 清单——只收「曾经写错过 / 极易写错」的名字，
  每个名字要么在真实工具集合里，要么就是漂移。扫到就是真错。
* **方向 B（跨来源）**：`SKILL.md` 正文里 `xxx_yyy` 形态的标识符与真实工具集合求差集。
  技能正文是给人看的自然语言，那里的下划线名基本就是工具名，判据可以激进些；
  再排掉参数键、技能名、模板 id、样例 id 这些确定不是工具的东西（形态规则 + 词表，宁漏不误报）。
* **字段名来源收在一处**：`skill_field_names(node_param_keys=, tools=[…])` = 剪辑节点参数键
  ∪ 各批工具 schema 里出现过的**全部层级**字段名（`schema_property_names` 递归进
  `items / additionalProperties / oneOf / anyOf`）。为什么必须收全：计划门的 `param_keys()` 只覆盖
  剪辑节点参数，而 `submit_plan` 的 `plans[].steps[].param_options` 只在**规划注册表**里——
  装配处第一版只扫主注册表，于是启动日志挂着一条假告警「引用了不存在的工具：param_options」，
  而离线测试因为喂的是手写替身 schema 依然全绿。假告警的代价是教人忽略这个检查本身。
* **离线 37 项**：`tests/test_consistency_check.py`，含用例⑤专钉「漏传规划那批来源 → 正好复现
  那条假告警」。**真机精度校准**（拿真注册表 + 真技能正文跑零误报）在
  `.runtime/audit/calibrate_consistency.py`——离线跑不了，因为 `ToolCatalog` 只在装配时填充，
  离线拿到的 `catalog.names` 是近空的替身。

### 3.20 配额闸（用量落库 + 超限拦截）

每次 LLM 调用的 token 用量落 `token_usage` 表，按用户 + 时间窗聚合；超限不再调 LLM。

* **用量接出**：`LLMResponse.usage` / `StreamChunk.usage`（`{prompt_tokens, completion_tokens, total_tokens}`）。
  `llm_openai` 非流式从 `resp.usage` 取，流式靠 `stream_options={"include_usage": True}` 从末块取。
* **落库**：`UsageHook` 写 `token_usage`（`user_id, session_id, run_id, model, *_tokens, created_at`）。
* **拦截**：`QuotaHook` 在 `before_iteration` 查近 `window` 秒合计，超 `limit` 抛 `QuotaExceeded`。
  `--quota-limit` 默认 **0**、`--quota-window` 默认 86400。**`limit=0` 是「不拦截但仍记录用量」的
  刻意默认，不是缺陷**——上线先观察一周用量再定阈值。
* **离线 9 项**：`tests/test_quota_gate.py`（落库 / 未超限不拦截 / 超限抛 / limit=0 不拦截）。

### 3.21 eval 回归通道：钉 prompt 路径的形状

* **prompt 指纹**：`context.prompt_fingerprint(text)` = `"{version}:{sha256[:8]}"`
  （当前 `2026-10-01.v2:cbf36a96`）。改了提示词文本，指纹自动变；每轮 `qa.parts` 首条记
  `{"type": "prompt_fingerprint", "fingerprint": "…"}`，于是「换过文案之后的结果」能归到具体版本。
* **golden 基线**：`tests/golden/baseline.json`（4 条用例：纯答复 / 一轮工具 / 两轮工具 / 中文断言）。
  每条声明 `input` + `llm_steps`（ScriptedLLM 脚本）+ `assertions`（equals / contains / regex / tool_calls）。
* **入口**：`PYTHONPATH=. python scripts/run_eval.py`，不联网不接真 LLM——用 ScriptedLLM 按
  golden 脚本逐轮返回。**它钉的是路径形状，不是模型质量**；真机评测走 `--live` 另接真 LLM + 真素材。

### 3.22 多副本执行语义

多个 `run_server.py` 实例共用同一套 PG，崩溃恢复按 `(status, lease)` 原子认领，
跨实例同一条 run 只被一个实例接手。

* **认领**：`CheckpointManager.claim_recoverable(instance_id)` 两步纯 AND claim——先认领无主的
  （`owner_instance_id IS NULL`），再认领租约过期的（`lease_expires_at < now()`）。
  PG 用 `FOR UPDATE SKIP LOCKED`，内存引擎在锁内比较-改写。
* **租约续期**：`save_progress` 每次落盘顺带续租（迭代边界 = 心跳点）；`complete` / `mark_failed`
  归还认领。
* **装配**：`--instance-id=web-1`（默认 `hostname-pid`）。单副本下 `owner/lease` 恒为 NULL，
  既有行为不变。§3.11 的 Redis 广播解决的是**回投**跨实例，这一节解决的是**执行权**跨实例，两件事。
* **离线 17 项**：`tests/test_replica_claim.py`（认领无主 / 租约过期 / 续租 / 归还 / 跨实例互斥）。

### 3.23 一次 run 的可恢复状态：挂起之后对账不丢

`agent_framework/run_state.py` 把「跨挂起仍然要成立」的事实收敛成一个**有字段清单的对象**
（`RunState`），随指针行落盘（`cp.plan["state"]`，不新增列、不需要迁移）。

改之前这些事实挂在 `ctx.extras` 的约定键上，而 `extras` 是纯内存字典、不进 checkpoint，
于是任何一次挂起→续跑（渲染确认门、`ask_user` 弹窗、兜底方案选择）都把它们清零。四种表现：

| 症状 | 清零的键 |
| --- | --- |
| 执行轮在渲染门前停过一次，续跑那半截再没有一条 `plan reconciliation` 帧 | `approved_plan` |
| 挂起前真跑过的步骤，续跑后被「本轮一次步骤都没调用」的硬保证当成假称 | 调用清单 |
| 规划轮弹过问题再续跑，界面上确实在着的卡被判成「没有出卡」 | `plan_candidates` |
| 最后那条 assistant 行只剩续跑之后的半截，挂起前的思考与调用气泡全丢 | `qa_parts` |

字段清单：`approved_plan`、`calls_attempted`（含失败的调用，供「未调用步骤不许声称已执行」
那道保证）、`calls_executed`（其中真打到注册表的那些，供对账）、`plan_candidates` /
`plan_warnings` / `plan_card_pushed`、`audit_pushed` / `plan_audit`、`qa_parts`。
写回指针行只有四处：`_drive` 开头一次、每个一致点（`save_progress` 之前）、每个挂起点
（`await_approval` 之前）、异常路径（`mark_failed` 之前）——**与消息同批**，
崩溃恢复取到的调用清单才不会对不上当时的那条一致点。

两处刻意的设计：

* **候选计划不在 state 里重复存一份**。`submit_plan` 已经把它写进 `cp.plan["candidates"]`
  （确认接口与 `/plans/{id}` 的数据源），`restore()` 从那一列 hydrate 回来——库里只有一份真相。
* **收尾后只留「认过的承诺」**（`retain_residue`）。完成的 run 不再被恢复，transcript 撤下
  （已在 assistant 行的 `qa.parts` 里）；但 `approved_plan` 必须留着：它是**编译后的那一份
  计划**在库里唯一的落点，从这条 run 的某个一致点分叉时子 run 要继承同一个承诺，否则
  执行轮的对账与那句硬保证会一起失效。`CheckpointManager.fork` 就按这一条把承诺带给子 run，
  而调用清单按子 run 上下文里**真有的**回执重建（`tool_names_in`）——分叉点之后那些步骤
  这次要重跑，照父 run 的清单对账会恰好把「用户要求重做的几步」判成已履行。

仍然留在 `extras` 的是**本轮调用私有**的注入，落盘反而失真：`run_id`、`checkpoint`、
`checkpoint_manager`（每次 `_drive` 由调用方重新给）、`handover`（分叉工具申请、同一轮里
就被消费掉）、`confirm_plan_requested`（同上）、以及两处本轮去重计数
（`_fail_counts`、`_media_urls_pushed`）。原先那个写了四次、从来没人读的
`approval_paused` 顺手删掉了。

顺带一条对账口径的修正：`ask_user` 进了 `NON_STEP_TOOLS`（与 `render_status`、
`read_node_history` 同一列）。提问是**交互动作**，不是模型偷偷多做的一步——执行轮只要问过
一个问题，角标就会挂一条「计划外 · 询问用户」，那是噪声；它仍然记在 `calls_executed` 里，
只是不作为创作步骤出现在偏差里。

* **离线 34 项**：`tests/test_run_state.py`（序列化往返与脏数据 / 指针行往返 /
  **端到端挂起→续跑后对账仍然算得出来**、去重帧不重复推、qa 片段不断档、收尾残留、分叉继承）。

## 4. 测试

```bash
python -m pytest -q                              # 全量：一条命令收完 tests/ 下所有脚本
python tests/test_approval_gate.py               # 单文件直跑（exit code 判定，便于反复调一个用例）
```

当前规模：`tests/test_*.py` **59 份脚本**，`python -m pytest` 收出 59 个用例
（2026-10-02 全量复跑 59/59 绿；注意 `pytest.ini` 的 `addopts` 里已经有一个 `-q`，
命令行再带 `-q` 会变成 `-qq`，末尾那行 `59 passed in …` 就不打印了，判据看 exit code 与点数）。

**`tests/` 里每份文件都是自带 `asyncio.run(main())` 的独立脚本，一个真 pytest 用例也没有**。
`pytest.ini` 写着 `testpaths = tests`，直接收集会把脚本里的 `async def` 判成「缺异步插件」、
把 `tmp` 参数判成「fixture 不存在」——**命令能跑、结论全是假故障**，既掩盖真实回归也让人不敢用标准入口。
`tests/conftest.py` 因此把每份脚本包成一个用子进程执行的用例（同样按 exit code 判定），
退出码非 0 时在报告里带上该脚本的输出尾部，全量输出落 `.tmp/reg_pytest/`。
实现上有个坑别改回去：`pytest_collect_file` **不是** firstresult hook，内置收集器同时会收出
`Module`，同一文件于是既出现 5 个 async 用例、又出现 1 个脚本用例；在
`pytest_pycollect_makemodule` 里返回 `None` 也挡不掉（那个 hook 经 `hookproxy(file_path)` 调用，
子目录 conftest 不参与）。所以做法是收集完成之后**筛掉**非 `ScriptItem` 的那些。

离线套件不联网、不起容器（存储层用内存替身，外部能力用注入的假实现）。
`test_fork_time_travel.py`（49 项）钉的是增量链形态（指针行里没有 `messages`、`head_seq` 等于链长减一）、
`load(at_seq)` 截断、`fork` 的产物集复制与下游作废、`superseded` 退出恢复候选、
`rerun_from` 的三处如实回绝，以及分叉带新诉求时**两处留痕**（子 run 指针行的 `message` +
回退后上下文尾部那条 user 消息）与复用区间不变；`history()` 每点带 `tools`——前端预勾哪些节点就是读它。
跨轮那半（同文件的 part4）钉的是 `run_id` 前缀解析、命中多条时回绝并列出候选、别人会话的 run 一律回绝、
本轮链上没这个节点时把历史执行当路标交出来，以及**跨轮回溯时本轮原来那条也置 superseded**。
`test_outbound_broadcast.py`（37 项）在假 Redis pub/sub 上起**两个副本**：帧产生在 A、WS 挂在 B
才收得到、且只收到一次；反向对照——同样拓扑不开广播时 B 一条也收不到（差距是真的）；开广播时
OutBound 的订阅者只有广播器一个；**没挂这条 session 连接的副本不投递、但攒进有界影子预算**，
于是浏览器被它接管后重连补得到接管之前的那一帧（这一条以前钉的是反方向：「不投递也不攒缓冲」）；
预算的两条上界（会话条数 / 合计帧数）按**整条**淘汰、本地挂上连接即退出预算、最后一条连接断开
即并入预算，两个观测计数同样有界；broker 不可达（PUBLISH 直接抛）时消费那一帧的实例退回本地投递、
环形缓冲照写，而跨实例那半程**确实**送不到，恢复后重新扇出。
`test_storage_contract.py` 是**同一套用例在内存替身与真 PG+MinIO 上各跑一遍**——
内存全绿不等于真引擎全绿，这一层的 SQL 语义只能靠它钉住。**结尾必须看到
`未验证清单：无——两引擎都跑过了`**：曾经这里的 SKIP 掩盖过一个真 bug
（`ensure_schema` 把引用新列的 `CREATE INDEX` 排在 `ALTER ADD COLUMN` 之前，
已建过的库直接起不来）。
`test_payload_refs.py` 钉 §3.12 的引用契约四段：引用原语（`obj:` 前缀一眼和盘符区分开）、
`localize_ref`（同名不互盖、命中直返不再取一次字节、8 路并发取回各自的字节、遗留绝对路径
仍可读时放行、指向空气时如实说「请重跑产出它的节点」）、`publish_derived` 的键面与同作用域覆盖、
以及**整链搬迁**：真跑一遍出片后把本机工作区与内容缓存**当场删空**，再从 `artifacts` 表重建
Store 只调 `render_video`——`order_trace` 恰好多一个节点、时长对得上、成片键不变、库里依旧
一处本机路径都没有。同一段还钉 `media_url` 的归属：落库那行有 `video` 对象键、没有会过期的直链。
`test_upload_resume.py`（88 项，§3.13 的离线面）钉的是：切法由服务端定且末片余数可推算、
**续传进度以桶为准**（重发 `init` 接回同一条会话，换一个 `Storage` 实例=换副本也问出同一份进度）、
乱序传片与缺片必拒（409 列出还差哪几片）、短收/超长/指纹不符当场回绝且**不留脏对象**、
三道内容校验各自的失败形状（逐片 422 重传；`complete` 带 `parts_sha256` 时先 400 拒畸形清单
——条数不足、非十六进制、非列表、非 JSON 体——再 422 拒内容不符，**且只删对不上的那几片、
好片一块不动、账本仍 `uploading`、`missing_parts` 恰好是那几片**，重传后带大写指纹再 complete
得 `parts_verified == 片数`；整文件 422 且不登记素材）、别人的 `upload_id` 一律 404、
收齐后拼装的字节与源文件全等且与 `/upload` 落在同一个 `object_key` 布局上、重复 `complete` 幂等、
`abort` 与过期清扫删净分片、后台清扫真的在跑（`app.state.upload_sweep`）。
`test_render_follow.py`（35 项，§3.10 的代查面）用真 `MediaCardHook` + 假 `render_status` 钉住：
模型拿到 `queued` 就收尾时循环自己轮到 `done`（3 次查询、卡片回投一次、持久链接登记一条）、
**中间态不进上下文**（一次长渲染不该往 messages 里拼 600 条「还在跑」）、预算用尽只追加一条
如实说明的 system 且**不发卡片不登记链接**、Registry 里没有 `render_status` 时行为与改动前一致、
`rerun_from` 交接那一轮不追旧作用域的渲染、`failed` 也算终态不空转，外加
`_inflight_render_id` 的判据边界 8 项（只认 queued/running，非 JSON / None / done / failed 一律不轮）。
块 A/B/C 那几套：`test_tool_catalog.py`（55 项，词表的三种出口形状——文本 / 对象 / 流——与
`KEEP_KEYS` / `OPAQUE_KEYS` 两条旁路，含「残片跨帧续判代码段」与「4096 字兜底」）、
`test_display_name_sources.py`（28 项，三个声明源汇入同一张表的每一条：框架 `display_name`、
MCP `title`、技能 frontmatter `display`，以及规划轮那张独立注册表也得进表）、
`test_display_outlets.py`（29 项，六个 HTTP 口 + WS 序列化 + 工具文本的出口全覆盖，
**结尾一条判据是 `frontend/dist` 已按当前源码重建**——源码改了没重新 build，前端那半就等于没做）、
`test_plan_gate.py`（145 项，§3.15 的四重校验、两种 action 帧、跳过降级与 ⑮ 节两条假称判据的词面）、
`test_run_state.py`（34 项，§3.23 的可恢复状态：字段往返与脏数据兜住、指针行读写、
**执行轮挂起一次之后对账仍然算得出来**（改之前那里整条链再无 `plan reconciliation` 帧）、
去重帧不重复推、qa 片段跨挂起不断档、收尾只留承诺、分叉继承承诺而清单重建），
`test_plan_flow.py`（68 项，真装配链路：规划轮物理不含剪辑节点、两段注入与中和、谱系、
对账三处同形、consumer 分派、guard 退一次与预算用尽补事实）、
`test_plan_layout.py`（31 项，**形状**而非剪辑逻辑：计划门包一文件一职责且没有一模块长回
god file、依赖方向（底层不引上层、装配层不被任何运行期引用，只为形参标注引 PlanGate 不算环）、
对外名字一个都不缺且仍只从 `agent_framework.plan` 取、`_drive` 保持骨架且六块具名助手在位、
跨挂起的事实不再退回 `ctx.extras` 的约定键）、
`test_prompt_library.py`（49 项，提示词库的读盘 / 占位符 / 块守卫 / 回落 / 热替换、
「内联回落与磁盘文件必须一致」的漂移守卫（系统提示 + 规划轮模板 + 子 Agent），
外加「库与 refresh 必须排在 `make_spawn_tool` 之前」那条接线守卫——两份文案各写一处迟早会漂）、
`test_planning_prompt_golden.py`（29 项，规划轮段搬进模板**之前**逐字捕获的 9 条渲染金样，
内联回落与磁盘模板各比对一遍并要求字符串相等，另钉「`run_server` 真的把 `prompt_library`
传进了 `PlanGate`」——金样全绿也可能意味着磁盘那份从来没被读过）、
`test_render_watchdog.py`（§3.10 的停滞看门狗：无进展收口、慢渲染不误杀、`stall_sec=0` 不建循环）、
`test_hooks.py`（21 项，除生命周期接线外钉住「`result` 截到 600 字后帧上的 `render` 视图仍完整」）。

真机冒烟在 `.smoke/`（需容器起来、`.env` 填好、模型密钥就位）：
`b3_render_smoke.py`（一句话驱动出片）、`b4_runtime_smoke.py`（kill -9 后重启续跑）、
`b5_auth_smoke.py`（换进程只带 token 接回全部历史）、`b56_identity_smoke.py`（内部身份
与外键拒绝）、`b5_migrate_smoke.py`（迁移跑两遍一致）、`settings_key_smoke.py`
（页面填的 key 落进真 PG、启动在先的服务不重启就读到、文本与带图两路打真接口、
删用户即 CASCADE 清掉明文）、`thinking_off_smoke.py`（真 SDK 出网 body 里确实带
`thinking=disabled`、真 DeepSeek 文本与带图两条路各测开/关的中位耗时）、`b6_fork_smoke.py`
（执行面在真 PG 上走通：一轮对话落「指针行 + 增量链」→ `/convs/{id}/runs` 清单 →
`/runs/{id}/history` → HTTP `fork` 出子 run 跑完 → 父 run 置 superseded → 新消息不被
「继续」劫持 → 别人的 run 404；并真取一次 `dag_contract` 验 19 节点契约与下游闭包），
`b7_bgm_rerun_smoke.py`（整链「换 BGM → 分叉 → 在真 Storyline 上重渲染出片」36 项，三段全打真：
① 一句话出第一版（选到 A、出片可播）② HTTP 分叉回选乐之前换 B——作用域换新、上游 `split_shots`
逐字节复用、父那一版原样不动、新片 ffprobe 量得出时长 ③ 模型自己调 `rerun_from` 把 B 换回 A：
新诉求带进子 run、父 run 让位、交接后继续往下重跑选乐/时间线/渲染），
`b8_tts_smoke.py`（edge-tts 真网络三段：provider 层合成 9.29s 可播 mp3、节点层 `generate_voiceover`
每条标 `edge-tts` 且 payload 真进 Store、反向对照换坏 voice 必落 `silent_fallback` 而不抛穿），
`b9_render_poll_smoke.py`（渲染轮询的**模型侧**16 项：临时 Storyline 把 `render_grace_sec` 关成 0
逼出「提交即回句柄」，看模型收到 `queued`+`hint` 后会不会自己调 `render_status` 追到 `done`——
连查三次 `running/running/done`、done 那一轮才回投成片卡、`GET /render_status` 同形状、别人 404；
判据取 checkpoint 链上的 tool 全文，WS 帧里的 `result` 只留 600 字符给前端展示，长返回值读不得），
`b15_render_follow_smoke.py`（渲染轮询的**循环侧**真机 16 项：同样把 `render_grace_sec` 关成 0，
但提示词**明令模型不许**调 `render_status`、拿到 queued 就一句话收尾——于是判据反过来：
链上那些 `render_status` 的 `tool_call_id` 全是 `render_follow_*`（循环发起，模型自己发起的 0 条）、
轮到的终态是 `done` 且带回成片对象键、进上下文的只有终态、WS 里仍出现 `media` 帧、
历史口的 `media` 视图指向同一产物作用域、`render_jobs` 终态 done；
跑完复核自造数据零残留，**成片对象也一并删掉**（桶里的键把作用域 `:` 换成 `_`，按会话 id 子串认）），
`b10_cross_run_rerun_smoke.py`（**跨轮**回溯 20 项：同一个会话连跑两轮、第一轮换乐出一版，
第二轮的提示词**不含任何 run_id**也不许重头剪——模型先在本轮调 `rerun_from` 被回绝，再照回绝文本列出的
历史执行把上一轮那条 id 填回来重试；判据是子 run 的 `forked_from` 等于**上一轮**那条而不是本轮发起那条、
换新产物作用域、`select_BGM` 换到另一首、时间线带着重选的那首、上游 `split_shots` 逐字节复用、
父那一版原样不动、又出一版可播新片，以及本轮原来那条被就地置 superseded），
`b11_broadcast_smoke.py`（**多副本回投广播**真机 23 项：起两个真 `run_server.py` 进程共用同一套
PG+MinIO、再加一个真 Redis，浏览器 WS 挂副本 B、那一轮打在副本 A——delta/stream_end/answer 三路
跨实例到达、answer 正文就是 A 真跑出来的、每帧仍带同一条 session_id、A 上终态 completed、
同一份答案从 B 的 HTTP 口也读得到、B 攒下环形缓冲重连补看 5 帧；对照段关掉广播 flag 后 B 只拿到
connected 而**那轮照样在 A 上跑完并进共享存储**（先钉 /chat 200 带回 run_id，否则「收不到」可能
只是没跑——上一版两段共用一个 conv，会话归一个用户所有，对照段直接吃了 409 变成假阳性）；
另钉两副本并发冷启动 `ensure_schema` 不抛穿、起来后 `scheduled_jobs` 里 heartbeat 行**恰好**一条，
跑完复核身份/会话零残留），
`b12_cli_smoke.py`（**独立 CLI 客户端** 24 项：`scripts/runs_cli.py` 打临时实例
（`--no-resume` + 随机端口，不动常驻 :8000），只用公开 HTTP/WS 端点——`register` 把 token
只写进凭证文件、屏幕上连长度都不外泄，`chat --wait --frames` 先连 WS 再投话并当场逐帧看到
delta 直到 answer，`runs` / `history` 读回一致点链与每点的 `tools`，`fork --at-seq 0` 带新诉求
出子 run 并追到终态、父 run 就地 `superseded`、`runs` 清单两条齐全，`resume --at-seq 0` 同一条
run 续到终态，两处如实回绝（不给分叉点、「链上没这个节点」）都原样把服务端那句话传出来，
跑完自造身份与会话零残留），
`b13_payload_refs_smoke.py`（**产物引用契约**真机 11 项：模型能力注入 fake（钉的是存储层不是模型），
素材字节真进 MinIO → 整链出片 → 扫真 PG 的 `artifacts.payload` 一处本机绝对路径都没有、
时间线每条引用逐条能在 MinIO `head` 到字节 → **当场把第一份工作区与内容缓存删净**
（漏关的 ffmpeg 句柄会把它锁到进程结束，这条同时钉住了句柄回收）→ 换一套空工作区空缓存的第二个
实例，`order_trace` 只有 `render_video` 也照样出同一条时长、同一个成片键，`render_jobs` 终态由
第二个实例写回同一行 → 重出片之后库里依旧干净；跑完产物/渲染任务/素材/身份/对象零残留），
`b14_upload_resume_smoke.py`（**分片续传**真机 39 项：本机 ffmpeg 造一条 7MB 真容器素材、按 1MB 切 8 片
（第一版用 `-b:v` 目标码率只造出 0.82MB=一片就走完，续传场景展不开，故改全关键帧）→ 传两片后把服务进程
**kill -9**（掉电式，不走收尾钩子）→ 重起一个新进程、新事件循环、新连接池、**新工作区与新缓存目录**，
同身份同会话重发 `init` 接回崩溃前那条 `upload_id` 且进度显示那两片已在 → 补齐后 `complete` 出的素材
与源文件 `sha256` 全等、ffprobe 在新工作区读得出 6.0s/1280x720/音轨 → `uploads/` 前缀在真桶里已空、
账本行标 `completed`。同时把账本每一条 SQL 都在真 PG 走过（`open/find_live/touch/finalize/drop/expired`，
离线只见过内存替身），并钉 MinIO 一次 `list_prefix` 就带回 `x-amz-meta-sha256` 且与本机切片逐片相符；
**真桶上的逐片摘要核对**也走了一遍：清单少一条回 400 且桶里一片不少，把第 1 片的摘要算错回 422 且
**只有那一片**从桶里消失、其余 7 片完好无损，照 `missing_parts` 重传那一片再交正确清单正常出片，
`parts_verified` 为 8；
`abort` 与过期清扫各自删净、别人的 token 得 404，跑完身份/会话/素材/分片对象零残留），
`b16_plan_gate_smoke.py`（**计划门**真机六段：剪辑诉求先出卡、伪造的 `selected_plan` 与越界枚举
**在跑模型之前**被拒收（`/runs` 查不到那一行）、`<approved_plan>` 探针确实注入执行轮上下文、
不可跳的步降级并出 warning、`revise` 换一版出新规划 run 与新卡，跑完自建身份/会话/run/计划行零残留），
冒烟自己起自己的端口，
不动开发者常驻的 :8000/:8001，跑完把自建的行与对象收回、报告零残留。

另有一对夹具脚本 `ui_seed_demo_runs.py` / `ui_clean_demo_runs.py`：给 `执行记录` 面板按真实形状
造三条演示 run（不占模型密钥、不等渲染），浏览器点完分叉与续跑再由后者定点收回。

块 B/C 的那几张卡最后是在**常驻 :8000 的浏览器里**点完的（冒烟脚本能钉形状，点不出来观感）：
一条剪辑诉求出计划卡 → 勾「这一步跳过」后确认，执行 run 只调 `render_video`、audit 里
`extra=[] unfulfilled=[]`；「换一版」出一条新规划 run 与两张词面不同的新卡；不可跳的那一步在卡上
显示降级理由且不给勾选框；确认后的那一轮从 `render_video` 提交气泡起步，聊天窗里的进度条走
`0% → 编码输出 73% → 81% → 86% → ✓ 渲染完成 100%`（1s 采样），终态落成片卡。
被服务端打回的臆造节点（把 `read_node_history` 放上卡）也在浏览器里看到了完整形状：打回两次后
服务端不再打回，模型如实向用户说明而不是硬造一版。

## 5. 有意留在本地磁盘的九项

不是遗漏，是判断（spec §8）：ffmpeg/ffprobe 二进制与 PATH；部署期配置
（`config.toml`、`mcp.json`）；渲染临时工作区与内容缓存（可丢弃、可重算，LRU+TTL 回收——
「可丢弃」不是声称，§3.12 的冒烟就是当场删空本机两份目录后重出同一部片）；
faster-whisper 权重（几百 MB～GB，镜像预置）；系统字体（`font_dirs` 配置项）；
`frontend/dist`（构建产物）；运行日志（交平台采集）；`storyline_server/data/templates.json`
（随代码发布的静态数据）；Agent 的文件工具（是能力不是状态，root 已收窄到本会话工作区）。

## 6. 已知边界（不假装完成）

- **多副本回投已接通**（§3.11）：OutBound 帧经 Redis pub/sub 扇出到每个实例，挂着连接的那个
  实例照原路径推送，没挂连接的那个**只攒不投**（有界影子预算），所以浏览器被另一个副本接走时
  重连补得回接管之前的进度。剩下的边界不是「要不要广播」而是两处：① 补的是「这个副本订阅频道
  之后见过的那段」——它启动前的帧没有，超出预算被整条淘汰的也没有，跨**重启**补全仍要把缓冲
  本身搬到共享存储（未做）；
  ② 入站侧的 sticky 会话（同一用户固定打到同一副本）是网关层配置，不影响正确性，只决定 ①
  出现得多频繁。单副本不配 Redis 时行为与改动前完全一致。
- **大文件分片续传已接通并已上线**（§3.13，`tests/test_upload_resume.py` 88 项 + `.smoke/b14_upload_resume_smoke.py`
  真机 39 项 + 常驻 :8000 浏览器实点：64 片正传、25% 刷新后续传、30% 点「取消上传」）。
  以前列的两条缺口已销：整体内容校验对浏览器不再是"没开"（`complete` 交 `parts_sha256` 逐片核，
  见 §3.13），放弃的会话也不再只能等过期清扫（前端有「取消上传」→ `POST /upload/abort`）。
  现在真正剩下的边界只有一处：**拼装期间桶占用峰值约 2× 文件大小**（成品与分片短暂共存）——
  这是有意的取舍（磁盘便宜、路径简单），真被 GB 级素材撑到再优化。
  另两点如实：① 小文件（<2MB）走单请求路径，没有分片会话可作废，因此不显示「取消上传」，
  要中止只能等那一个请求自己结束；② init 声明整文件 `sha256` 的强校验只有脚本/CLI 客户端会发，
  浏览器换成交逐片摘要列（形态差异，非缺口）。
- **两台常驻服务的当前状态（2026-10-02 15:31 起重启，今天所有改动已上线）**：:8001
  `python -u run_storyline.py`（15:31:06 起），:8000 `python -u run_server.py`（随后起），
  两行命令行**都不带 flag**，走内置默认值（`--port 8000`、`--storage pg_minio`、
  `--config examples/storyline/config.toml`）。`GET :8000/health` 200、`GET :8001` 在听，
  两者的 stdout 这次都接进了 `.storyline_server.log` / `.main_server.log`。
  * 启动日志**这次逐行核到**（此前两轮重启都没落文件，那几条只是"应该生效"）：
    `storage=pg_minio 连通性与落盘校验` / `内部身份已登记 ['default','cron']` /
    `技能库已按 examples\skills 刷新 4 份` / `启动对账：本次 0 条` /
    `Storyline 已接入 21 个剪辑节点（DAG 契约 19 节点，rerun_from 可用）` /
    `中文名词表已装载：49 条工具名 / 剪辑节点 / 技能，参数标签 79 个` /
    `计划门已就绪：19 个可规划节点` /
    **`提示词库已接线：8 份来自 prompts（系统提示 + 规划轮段 + 子 Agent + 纠错说明）`** /
    `一致性检查通过：提示词与 4 份技能里点名的工具都在注册表实现 47 个工具里`。
  * 这一次重启同时把 §3.19 一致性装配修复、结构性改造 3/4/6/7 的代码带上线
    （旧 :8000 进程起于 11:10，早于这些改动）。§3.11 的影子缓冲**代码在跑、路径没走**：
    当前命令行不带 `--broadcast-redis-url`，启动日志明写「回投广播：关闭（单副本直投）」，
    所以跨副本接管仍只有离线拓扑证据。
  * **旧 run 清账**（用户批准「旧的可以清掉」后执行）：`running/failed` 4 条 +
    `awaiting_approval` 13 条（会话名全是 `gate-*`/`ask-*`/`dfx-*`/`dyn-*`/`real-*` 这类探针）
    置为 `superseded`，改前转储在 `.runtime/audit/stale_runs_2026-10-02.json`（17 行，可回滚）。
    不清的后果已实测到：**每一次启动都会自动重试这 4 条 failed**——3 条报「未配置模型密钥」、
    `c0b7caa0d526` 报 400（`Messages with role 'tool' must be a response to a preceding
    message with 'tool_calls'`）。要分清两件事：置 `superseded` 只是让它不再每次启动重跑，
    **那条 400 本身没修**——它是链上数据形状问题，与代码版本无关（预检时在新代码实例上原样复现）。
  * 真机复验（就在这台新进程上，浏览器实测）：新会话发「只给一版两步的剪辑计划」→ 模型调
    `提交候选计划`（`submit_plan` 的中文出口名）→ `accepted: ["p1"]` → 右侧「剪辑链路」面板
    两步 + 计划卡「按此执行 / 换一版 / ✕」→ 随后 `向用户提问` 弹窗（§3.17 的形状）。
    全程界面未露机器名（只有 `⧉` 复制钮旁路给排障用）。
  * `--max-iterations` 的内置默认值现在是 **40**（旧文档写的 20 已改：整链剪辑十几步工具调用、
    每步一轮 LLM，20 轮必然半路耗尽，用户看到「已达最大迭代次数 20，提前结束」）。
  * 仓库根有 `start.bat`（先 :8001 后 :8000 的一键启动）。它起的进程**不带 `-u`、stdout 只在自己
    的控制台窗口里**，而且会和脚本化重启抢端口（本次真撞上一回 `[Errno 10048]` 后自我退出）。
    要用它启动就别再叠一层脚本重启；要复核启动日志则用 `python -u … >> .main_server.log 2>&1`。
  * 老提醒仍然成立：uvicorn 的访问日志会把 WS 查询串里的 token 原样写进 `.main_server.log`——
    该文件已在 `.gitignore` 里，但别贴进 issue。
  主服务只在启动时建 MCP 连接，所以**改 Storyline 侧或 `config.toml` 白名单后必须重启 :8000**，
  否则它拿的还是旧工具表；只改主服务侧（如本轮的 `agent.py`）时 :8001 可以不动。
- **块 A/B/C（§3.14、§3.15、§3.10）落地后仍存在的边界**，逐条如实：
  ① 绕开计划门的只有两处投递：**定时任务到点那一投**（`run_server` 发 `action.op="execute"`——
  那条消息没有坐在屏幕前的人点确认，拦在规划轮里等于任务永不落地）与 **`POST /chat/sync`**
  （直调 `agent.handle`，不经 consumer 分派，是最小闭环/回归用的直连口）。
  `scripts/runs_cli.py` 发的是 `POST /chat`，照常先出计划卡。
  ② 规划轮**出不出卡**仍是判断而非硬保证：提示段明写「咨询/闲聊直接回答，不要为了走流程编一份
  计划」，所以模型有权判定这条消息不是剪辑任务。「换一版」那一轮已经把出口写死（带旧卡摘要 +
  只许 submit_plan），普通新诉求这一格仍留给模型。但**「没出卡却声称已出卡」这一半已经收口**
  （§3.15 的 `claims_plan_card` 守卫：退一次、预算用尽补事实）；判据是词面匹配，所以它认得
  完成态说法、认不出一句绕开措辞的自述——真机漏过一次「这次提交成功了，计划卡已回投给你，请确认」，
  正是因为第一版判据只认「已提交 / 等你确认」两种字面。
  ③ 卡面的参数位取决于节点有没有可反查的枚举/布尔/带界数值：BGM 曲库那侧没有标签列时 `select_BGM`
  就只能给 `query`（自由文本，不上卡），于是真机常出现「整张卡没有参数位」的形态——枚举反查与越界拒收
  那几条判据在离线套件里钉着（`tests/test_plan_gate.py` ④⑤），真机侧遇到有参数位的卡才会计入通过。
  ④ 中文名换轨的前提是**声明过**：框架工具没写 `display_name`、Storyline 节点没给 MCP `title`、
  技能 frontmatter 没写 `display`，出口就只能原样退回机器名（宁可露真名也不编一个）。
  ⑤ 计划是软约束：`PlanReconcileHook` 事后算偏差、只观测不拦截，不做逐步放行，也不因偏差中断这一轮。
  ⑥ 聊天窗的渲染进度条**已接通并已真机验过**（§3.10：tool_call 帧起步、帧上独立 `render` 字段
  推进、`"_default"` 也轮询 `/render_status`、`done/failed/error` 收口；浏览器 1s 采样看到
  `0% → 编码输出 73% → 81% → 86% → ✓ 100%`）。它仍只吃渲染视图，不代替模型决定要不要继续跑
  下一步——那仍然是 §3.10 的代查循环在做的事。
  ⑦ 两条如实观感：① 被 guard 退回重写时，**那一句已经流出去的字**在实时 DOM 里仍留在独立气泡
  上（入库的只有终答，刷新后它就消失了）——不影响事实，但屏幕上会短暂多看一眼被否掉的回答；
  ② 执行轮的假称判据本轮加宽到「只看完成态措辞」（§3.15），离线 141 项钉住了词面，真机侧
  加宽之后尚未再复现同类假称，所以那条反向证据目前只有离线那一半。
- 目标时长：`target_duration_sec` 是三个 `plan_timeline*` 与 `render_video` 的公共入参，
  **落地在时间线出口**（`_plan_output` → `_trim_timeline`）：超过目标就裁事件本体而不只改
  `duration` 字段——ffmpeg 兜底路径不看 `duration`，只看各事件 `src` 窗口之和，只改字段等于没改。
  裁过的会在节点输出里带一句 `已按目标时长裁到 5.0s（原 8.0s）`，界面上看得到这条片是被截停的。
  剩下的边界如实：① 超出目标的部分是**从尾部裁掉**，不是重新排一版更紧凑的时间线，
  所以「剪一条 30 秒」得到的是前 30 秒而不是 30 秒的高光（要高光仍走 `speech_rough_cut` 的精选模式）；
  ② 没声明目标、或本来就不超目标时行为与改动前一致（原样返回、不追加说明）。
- `agent_framework/video_editing.py` 已从**生产装配里摘出**，只剩离线测试夹具身份：
  真节点、真 DAG、真 Interceptor 都在 Storyline 服务端，主服务里那套 mock 节点既不注册也不兜底
  （两套图并存时依赖补齐与 `require_prior_kind` 只在 mock 侧生效，那是第二个真相）。
  夹具的产物键仍与真节点对齐（`video/media_url/duration/width/height/title`），
  所以离线用例能验到成片卡的形状；但它的 `media_url` 是 `memory://` 占位，
  不落盘、不写 MinIO——可播链路只在真 Storyline 上完整成立。
- **产物引用已收口成 `obj:` 对象键**（§3.12）：新写的 payload 里没有工作区绝对路径，离线
  `tests/test_payload_refs.py` 与真机 `.smoke/b13_payload_refs_smoke.py`（删空本机工作区与内容
  缓存、换一套目录只重跑 `render_video` 重出同一部片）各钉一遍。剩下的边界是**遗留数据**：
  `backfill_payload_refs.py --apply` 只改写字节能证明还在的引用，2026-09-29 真库跑完仍有
  **113 行、14803 处**指向既不在盘上也不在 MinIO 的旧路径（迁移前就被删掉的中间产物），
  这些行原样保留、不做假引用；时间旅行回到它们上重跑会当场得到「请重跑产出它的节点」。
- edge-tts 已真机验过（`.smoke/b8_tts_smoke.py`：真网络合成出可播 mp3、节点层每条标 `edge-tts`、
  坏 voice 必落 `silent_fallback` 的反向对照）；画面理解已改为与主 LLM 共用同一把
  key 打 `deepseek-flash`，`.smoke/vl_shared_key_smoke.py` 与 `settings_key_smoke.py`
  各真机验过一遍；ASR 已真机验过，且本机 CUDA 不可用，实走 CPU+int8，别按 GPU 预算估时。
- **YouTube 取料的 JS 运行时这一层已接通**（yt-dlp 的 `js_runtimes` 自动探测，本机
  `E:\node\node.EXE` v24.16.0 被真 yt-dlp 判 `supported=True`；`tests/test_fetch_media.py`
  里 11 项钉住探测、传参、缺位时的可操作报错）。**这台机器上仍取不到 YouTube**：实测
  `https://www.youtube.com:443` TCP 连接超时（没有出口/未配代理），门槛在部署的网络层，
  不在代码里——有出口的机器或配好代理后即可直接受益。B 站等直连站点不受这两项影响。
- **成片播放卡刷新重放（本次修复）**：渲染完成当轮，`MediaCardHook` 除了把临时 `media_url`
  经 OutBound 回投，还把**持久链接**（渲染对象键 + `artifact_id`）落进本轮 assistant 行的
  `qa.parts`；`GET /convs/{id}/messages` 读历史时据此现签一条 `media_url`，回投成与实时 WS
  `media` 帧同形的卡片，前端复用同一组件即可渲染，刷新后成片不再丢。**注意**：这条链接是
  本次才加的，早于它的历史行里没有 `media` 片段，刷新后不回放进片卡（历史附件仍照常回填）。
- **渲染改成提交 + 轮询的代价已收口成硬保证（见 §3.10）**：`render_video` 不再一次等到终态，
  成片卡片因此变成「轮询到 done 才出现」。以前这条链的成败点是模型行为——模型收到 queued/running
  之后就收尾，本轮既不会有 `media` 帧，也不会落 `qa.parts` 里的 media 片段（`MediaCardHook` 与
  `record_rendered_media` 都只认工具结果里出现的 `media_url`），刷新历史同样补不回来。
  现在由 `AgentOnceRun._follow_inflight_renders` 兜住：一轮工具跑完，只要结果里还有未达终态的
  渲染视图，循环自己轮 `render_status` 到 done/failed 才把结果拼进上下文，代查的那一次照原路径
  触发卡片与持久链接。剩下的边界是三处，都如实留着：① 预算上限 `render_follow_max_sec`（默认
  1800s）用尽后**不假称成功**——追加一条 system 说明渲染仍在进行、给出 `artifact_id` 让用户稍后
  自查，卡片当轮不出现；② 代查跟着这一轮 run 活在同一个进程里，进程被 kill 时它随run一起停，
  崩溃恢复取的是上一致点、不会自动接着轮（成片仍可凭 `render_jobs` 行用 `/render_status` 查到）；
  ③ 聊天窗口里成片卡片仍是**轮到 done 才到**（工具帧一跳一跳），不是当轮立刻出卡——进度那一半
  本轮已经补上：聊天窗里有一条真百分比进度条（§3.10），它吃的是同一份渲染视图，不再只有
  「直接渲染」那条 UI 路径有进度。离线 `tests/test_render_follow.py`（35 项）、
  `tests/test_hooks.py`（21 项，含「result 截断后帧上的 `render` 视图仍完整」）与真机
  `.smoke/b15_render_follow_smoke.py`（明令模型不许自己查，卡片仍落地）各钉一遍。
- **执行记录 UI（本轮新增，2026-09-23）**：`执行记录` 抽屉把 `/convs/{id}/runs`、`/runs/{id}/history`、
  `fork`、`resume` 全部点得出来了，浏览器验证过一遍。但**验的是种子数据**：造三条形状与真执行一致的
  run 行（`checkpoints` + `checkpoint_entries`）后点链条、点分叉、点续跑——分叉确实 POST 到
  `/runs/{id}/fork` 并 200、子 run 真的出现在清单上（换新产物集、`← 分自 父 第 N 步`、
  带上新诉求当标题、父 run 变「已让位」），续跑也 200；**子 run 本身跑不完**，因为那个临时身份没配
  模型密钥，页面上如实回显「未配置模型密钥」。也就是说 UI 通路验过、UI 到真出片的那一段是靠
  b7 冒烟在后端侧覆盖的，不是在同一次点击里覆盖的。修掉一处真 bug：`resume` 先前发裸 POST，
  被请求体校验判 422，现在带 `{}`。
- `rerun_from` 的同轮回溯与跨轮回溯都已接通（跨轮靠 `run_id` 参数，回绝文本自带本会话的历史执行当索引，
  见 §3.9）。剩余边界是**可达范围**：候选只来自本会话清单里还留着 checkpoint 链的那些执行——
  `prune` 收掉的旧 run、别的会话的 run 都不在索引里，那时只能走前端 `执行记录` / HTTP `fork`。
- **模型侧整链已真机跑过**（`.smoke/b7_bgm_rerun_smoke.py` 36 项）：换 BGM → `rerun_from` 分叉 →
  真 Storyline 上重渲染出片，上游逐字节复用、父 run 让位。**顺带发现并修掉一个设计缺口**：
  回退后的上下文尾部正是父那条旧指令，模型不接新诉求就会连着分九条叉反复选中同一首歌——
  现在 `fork(message=)` / `rerun_from(instruction=)` 把新要求带进去。
- DeepSeek 有时只叙述计划而不发工具调用（同一句提示词重跑结果不稳）；冒烟里靠把工具名与参数
  显式点名在提示词里才稳定。真实产品里由用户消息驱动，影响面比冒烟小，但仍不是确定性的。
- `generate_script` 会把标题截断（观察过：「换乐冒烟」→「换乐冒」），只影响文案标题字段，不影响产物。
- 本机 `.runtime/checkpoints/` 的 11 个遗留 JSON **已实迁进库**（见 §3.4）；盘上文件按迁移器口径
  保留不删，要清盘得人工确认。
