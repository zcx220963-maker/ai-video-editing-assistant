# 上线存储层设计：PostgreSQL 元数据 + MinIO 对象存储

日期：2026-09-20 · 状态：**已实现并完成联机验证**（B1–B5 全部落地，README §3/§4 是运行口径）
目标：把「智能创作助手」全部本地文件持久化（25 处落盘点中的 13 类）搬到 PG + MinIO，
使项目具备多实例部署条件；严格沿用文档既有的可插拔后端范式（`build_message_queue`），
不另起炉灶。

## 0. 决策记录（评审时请确认这五条）

| # | 决策 | 取舍 |
|---|---|---|
| D1 | **全量替换**：本地磁盘不再是任何一类数据的运行路径 | 换来形态统一；代价是没容器就起不来，且中间态不可半跑 |
| D2 | **MinIO 为媒体唯一源 + 本地临时工作区**（`localize`/`publish`） | ffmpeg/MoviePy/whisper 只吃真实本地路径，这是硬约束；本地目录降级为可丢弃缓存 |
| D3 | **真实现 + 内存 test double**：生产配置只认 `pg_minio`；`memory` 后端仅供测试注入 | 不联网测试的规矩不破；但内存 double 验不了 SQL 语义，SQL 由联机契约测试覆盖 |
| D4 | **全异步 + 真抽象**（SQLAlchemy 2.0 async + asyncpg；`minio` SDK 经 `to_thread` 包装） | 相关同步接口（Checkpoint/TaskManager/Interceptor 落盘钩子等）提为 async；不把同步连接池债带进新层 |
| D5 | **范围不裁剪**，一次做完 13 处迁移，按依赖分 B1–B5 五批落地，每批全量回归 | 交付周期长；用"每批全绿再进下一批"控制中间态风险 |

附带的既有缺陷修复（不修就是新层漏洞）：Checkpoint 永不删除、`temp_audio.m4a`/`_fb_*`
污染公开媒体面、`cache_dir` 中间产物无清理、cron 整文件覆写多实例互踩、记忆 append 丢更新、
子 Agent 重启后谎报 idle、任务板双向边无事务、历史目录无鉴权。

## 1. 总体架构

```
                         ┌──────────────────────────────────────┐
  前端 (Vue3 dist)       │  agent_framework/storage/            │
   │ 附件卡片 / WS 流     │   object_store.py  ObjectStore ABC    │
   ▼                     │     ├─ MinioObjectStore （生产）      │
 :8000 主 Agent ──────────┤     └─ MemoryObjectStore （测试）     │
   ├─ repositories.py ────┤   db.py  engine/session（DSN 读 env） │
   ├─ 身份/会话/运行时表   │   schema.sql  幂等建表                │
   └─ MQ (kafka\|memory)  │   __init__  build_storage(backend)   │
                          └──────────────────────────────────────┘
                                    ▲                ▲
                                    │ 同一个库/同一个桶 │
 :8001 StorylineServer ──────────────┘                │
   ├─ artifacts 表（取代 FileStore 目录树）──────────────┘
   ├─ materials 表（取代 rglob 扫盘检索）
   ├─ render_jobs 表（取代 render_progress.json 探针）
   └─ Workspace：localize(object_key) → 本地临时文件 → ffmpeg/MoviePy → publish → presigned URL
```

PostgreSQL 存**元数据与全部结构化状态**，MinIO 存**媒体字节与技能附件**，两者以
`object_key` / `material_id` 相连。`build_storage(backend)` 是唯一分派点，形状照
`agent_framework/mq.py:194 build_message_queue("memory"|"kafka")`：重依赖在方法内延迟
import（未装 `minio`/`asyncpg` 时 import 模块不报错），后端名未知即抛 `ValueError`。

两台服务连同一份配置：`run_server.py --storage pg_minio`（主 Agent 侧用
`users`/`conversations`/`messages`/`checkpoints`+`checkpoint_entries`/`inbox_messages`/
`tasks`/`subagents`/`scheduled_jobs`/`memories`/`skills`），`examples/storyline/config.toml` 新增 `[storage]` 段
（Storyline 侧用 `artifacts`/`materials`/`render_jobs` + 同一桶）。`task_edges`、`skill_files`
两张关联表由各自的父表模块读写。

部署级凭证：`PG_DSN`、`MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、
`MINIO_BUCKET` 五项**只从环境变量读**，`.env.example` 给模板；代码不落盘、不打印、不写日志。
模型 API Key 不属于这一类——它是**按用户**的密钥，落在 PG `app_secrets`（§3.16），
由页面「设置」自配置、两台服务热读（§7），环境变量只是它的回落层。

## 2. `ObjectStore` 接口面

```python
class ObjectStore(ABC):
    async def put(self, key: str, chunks: AsyncIterator[bytes], *,
                  content_type: str = "", size: int | None = None) -> ObjectInfo
    async def get_stream(self, key: str, *, start: int = 0,
                         end: int | None = None) -> AsyncIterator[bytes]
    async def head(self, key: str) -> ObjectInfo | None          # 存在性 + bytes + etag
    async def delete(self, key: str) -> None
    async def list_prefix(self, prefix: str) -> list[ObjectInfo]   # 按前缀列出（键升序）
    async def presign_get(self, key: str, ttl_sec: int = 3600) -> str
    async def localize(self, key: str, dst_dir: Path) -> Path    # 拉到本地；内容寻址缓存
```

`ObjectInfo` 是 `@dataclass`：`key: str`、`bytes: int`、`sha256: str`、`content_type: str`、
`last_modified: float`（epoch 秒）。

`list_prefix` 的约定两个引擎一致（`tests/test_storage_contract.py` 的「对象前缀列举」在内存替身
与真 MinIO 上各跑一遍）：**按键升序**返回、`bytes` 必填、`sha256` 引擎拿得到就填——MinIO 侧用
`list_objects(..., include_user_meta=True)`，一次列举就把 `x-amz-meta-sha256` 带回来，不必逐片
`head`。片号定宽补零（`part_00007`）让字典序等于数值序，所以升序列表本身就是拼装顺序。
它是分片续传问「已有哪几片」的唯一问法（见 §3.17）。对象键**一律由调用方按 §4 布局拼好后传入**，
`ObjectStore` 不参与业务命名，也不校验前缀——它只认 key，这样测试 double 与真实现的
差异被压到最小。

`MinioObjectStore` 内部用官方 `minio` 同步 SDK，每个调用点包 `asyncio.to_thread`——
与 `storyline_server` 现有约定一致（`core_nodes.py` 顶部注释：阻塞工作一律下线程）。
`localize` 的缓存层按 sha256 寻址：`{cache_dir}/objects/{hex[:2]}/{hex}`，命中即
`shutil.copyfile` 到 `dst_dir`，未命中才走网络；内容指纹统一用 `ObjectInfo.sha256`
（上传时服务端边收流边算，MinIO 侧同时写入 `x-amz-meta-sha256`）。**不拿 MinIO 的 etag
做比对**——分片上传下 etag 不是内容 MD5，用它寻址会撞 cache miss。

## 3. 数据模型（`storage/schema.sql`，幂等；19 张表 + 1 个序列 `task_seq`）

表名集合与数量由 `tests/test_storage.py::test_schema_static` 钉住，加表要同时改那里。

全部 `CREATE TABLE IF NOT EXISTS`；服务启动时自动执行，跑两遍结果一致。
时间列统一 `timestamptz`，jsonb 列统一 `NOT NULL` + 默认 `'{}'`（除注明可空者）。

```sql
-- 3.1 身份：取代前端 localStorage 的 ca.user（客户端自造、可伪造）
CREATE TABLE IF NOT EXISTS users (
  id            text PRIMARY KEY,                 -- 'u-' + 12 hex，服务端生成
  token_hash    text NOT NULL UNIQUE,             -- sha256(明文 token)，明文只返回一次
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- 3.2 会话：取代 localStorage 的 ca.convs（含 50 条截断）
CREATE TABLE IF NOT EXISTS conversations (
  id            text PRIMARY KEY,                 -- 'c-' + 10 hex
  user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title         text NOT NULL DEFAULT '新对话',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conv_user_idx ON conversations(user_id, updated_at DESC);

-- 3.3 消息：取代 .runtime/sessions/{user}/{conv}.jsonl
--     qa 列原样存文档的 QA 结构 {"question": str, "answer": [{"type":"think"|"tool call"|"answer",...}]}
--     seq 由 repo 在同一事务内取号：SELECT COALESCE(MAX(seq),0)+1 ...，
--     并发下靠 UNIQUE(conv_id,seq) 冲突重试一次；读取一律 ORDER BY seq。
CREATE TABLE IF NOT EXISTS messages (
  id            bigserial PRIMARY KEY,
  conv_id       text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq           integer NOT NULL,                 -- 会话内单调序号，取代 jsonl 行序
  role          text NOT NULL CHECK (role IN ('user','assistant')),
  content       text NOT NULL DEFAULT '',
  attachments   jsonb NOT NULL DEFAULT '[]',      -- ["mat-xxx", ...]
  qa            jsonb,                            -- 仅 assistant 行；可空
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conv_id, seq)
);

-- 3.4 素材登记：取代「本地绝对路径当主键」整条链路（本次迁移的骨）
CREATE TABLE IF NOT EXISTS materials (
  id            text PRIMARY KEY,                 -- 'mat-' + 6 hex
  owner_user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conv_id       text REFERENCES conversations(id) ON DELETE SET NULL,
  object_key    text NOT NULL UNIQUE,
  filename      text NOT NULL,                    -- 净化后的原名，含扩展名
  kind          text NOT NULL CHECK (kind IN ('video','audio','image')),
  mime          text NOT NULL DEFAULT '',
  bytes         bigint NOT NULL,
  sha256        text NOT NULL,
  duration_sec  double precision,                 -- ffprobe 结果，图片为 NULL
  width         integer, height integer,
  has_audio     boolean NOT NULL DEFAULT false,
  origin        text NOT NULL DEFAULT 'upload'
                CHECK (origin IN ('upload','library','bgm')),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mat_owner_idx  ON materials(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS mat_conv_idx   ON materials(conv_id);
CREATE INDEX IF NOT EXISTS mat_origin_idx ON materials(origin);   -- search_media / select_BGM

-- 3.5 节点产物：取代 FileStore 的 {会话}/{节点}/{产物}.json 目录树
--     record = {node, artifact_id, session_id, payload}，payload 内嵌的对象键不再是文件路径
CREATE TABLE IF NOT EXISTS artifacts (
  session_id    text NOT NULL,
  node          text NOT NULL,
  artifact_id   text NOT NULL,                    -- 空串归一为 '_default'
  payload       jsonb NOT NULL,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, node, artifact_id)
);

-- 3.6 渲染任务：取代 out_dir 里的 render_progress.json 探针（跨实例可见）
CREATE TABLE IF NOT EXISTS render_jobs (
  id               text PRIMARY KEY,              -- 'rj-' + 8 hex
  session_id       text NOT NULL,
  artifact_id      text NOT NULL,
  status           text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','running','done','failed')),
  stage            text NOT NULL DEFAULT '',
  percent          integer NOT NULL DEFAULT 0,
  video_object_key text,
  duration_sec     double precision,
  error            text,
  -- 终态产物（video/duration/width/height/title…）：渲染改成「提交 + 轮询」后，
  -- 结果必须能在任意实例、进程重启之后照样重建（此前它只活在一次阻塞调用的返回值里）。
  -- 不含 media_url——presigned 直链会过期，读时现签。
  result           jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, artifact_id)
);

-- 3.7 Checkpoint：run 指针行。正文不在这里——一致点增量链见 checkpoint_entries。
-- （本节按 2026-09-23 优化轮改写：原本每轮把整条 messages 全量 upsert 回一行 jsonb，
--   几十 KB 写放大且只能"从最新点重跑"。理由与新语义见
--   `2026-09-23-checkpoint-fork-reducer-design.md`。）
CREATE TABLE IF NOT EXISTS checkpoints (
  run_id         text PRIMARY KEY,
  session_id     text NOT NULL,
  message        text NOT NULL DEFAULT '',
  iteration      integer NOT NULL DEFAULT 0,
  head_seq       integer NOT NULL DEFAULT 0,                 -- 链尾 seq（-1 = 还没有一致点）
  status         text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running','completed','failed','superseded')),
  scope          jsonb NOT NULL DEFAULT '{}',                -- {storyline_session, artifact_id}
  forked_from    text,
  forked_at_seq  integer,
  created_at_ms  bigint NOT NULL,
  updated_at_ms  bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS ck_status_idx ON checkpoints(status);
CREATE INDEX IF NOT EXISTS ck_time_idx   ON checkpoints(updated_at_ms);
CREATE INDEX IF NOT EXISTS ck_fork_idx   ON checkpoints(forked_from);

-- 一致点增量链：每行只存自上一致点新增的消息；上下文压缩改写了旧消息时那一条退化成 full 基准。
CREATE TABLE IF NOT EXISTS checkpoint_entries (
  run_id        text NOT NULL,
  seq           integer NOT NULL,
  parent_seq    integer,
  kind          text NOT NULL DEFAULT 'delta' CHECK (kind IN ('delta','full')),
  payload       jsonb NOT NULL,
  iteration     integer NOT NULL DEFAULT 0,
  created_at_ms bigint NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS ce_run_idx ON checkpoint_entries(run_id, seq);

-- 3.8 A2A 收件箱：取代 Message Center 的 {user}/{conv}/{agent}.jsonl
CREATE TABLE IF NOT EXISTS inbox_messages (
  id          bigserial PRIMARY KEY,
  user_id     text NOT NULL,
  conv_id     text NOT NULL,
  agent       text NOT NULL,
  type        text NOT NULL DEFAULT 'message',
  sender      text NOT NULL DEFAULT '',
  content     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  consumed_at timestamptz                        -- 消费即删语义改为标记
);
CREATE INDEX IF NOT EXISTS inbox_pending_idx ON inbox_messages(user_id, conv_id, agent)
  WHERE consumed_at IS NULL;

-- 3.9 任务板：取代 task_{id}.json + 读-改-写维护的双向边
CREATE TABLE IF NOT EXISTS tasks (
  id            integer PRIMARY KEY,              -- 取自 task_seq 序列，替代文件名反推
  scope         text NOT NULL,                    -- '{user}:{conv}'
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','claimed','completed')),
  owner         text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE SEQUENCE IF NOT EXISTS task_seq;
CREATE TABLE IF NOT EXISTS task_edges (
  task_id     integer NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on  integer NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on)
);

-- 3.10 子 Agent 登记：租约取代重启后残留的 working/idle 谎报
CREATE TABLE IF NOT EXISTS subagents (
  scope             text NOT NULL,
  name              text NOT NULL,
  prompt            text NOT NULL DEFAULT '',
  status            text NOT NULL DEFAULT 'idle'
                    CHECK (status IN ('idle','working','shutdown')),
  lease_expires_at  timestamptz,
  PRIMARY KEY (scope, name)
);

-- 3.11 定时任务：cron 与 heartbeat 合并（同一份 schema，心跳只是其中一个 job）
CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id               text PRIMARY KEY,
  name             text NOT NULL UNIQUE,
  task             text NOT NULL DEFAULT '',
  enabled          boolean NOT NULL DEFAULT true,
  delete_after_run boolean NOT NULL DEFAULT false,
  schedule         jsonb NOT NULL DEFAULT '{}',   -- {kind, at_ms, every_ms}
  state            jsonb NOT NULL DEFAULT '{}',   -- {next_run_at_ms, last_run_at_ms}
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- 3.12 长期记忆：取代 User.md / Tool.md 全文覆盖（消除 read-modify-write 丢更新）
CREATE TABLE IF NOT EXISTS memories (
  user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category   text NOT NULL CHECK (category IN ('user','tool')),
  content    text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, category)
);

-- 3.13 技能：正文进 PG（多实例一致分发），scripts/references/assets 进 MinIO
CREATE TABLE IF NOT EXISTS skills (
  name         text PRIMARY KEY,
  description  text NOT NULL DEFAULT '',
  frontmatter  jsonb NOT NULL DEFAULT '{}',
  body         text NOT NULL DEFAULT '',
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS skill_files (
  skill       text NOT NULL REFERENCES skills(name) ON DELETE CASCADE,
  relpath     text NOT NULL,
  object_key  text NOT NULL,
  bytes       integer NOT NULL DEFAULT 0,
  PRIMARY KEY (skill, relpath)
);

-- 3.16 密钥表：模型 API Key 由前端「设置」自配置，取代「改环境变量 + 重启两个进程」
--     value 明文存是刻意取舍：:8000 与 :8001 都要拿它真签发出网请求，任何一端拿不到
--     密文就配不成。对外可见的一切出口（HTTP 响应、日志、报错、dump）一律先过
--     SecretsRepo.mask()；按 user_id 归属，删用户即 CASCADE 清掉明文。
--     部署级凭证（PG_DSN / MINIO_*）不在此列，仍然只从环境变量读。
CREATE TABLE IF NOT EXISTS app_secrets (
  user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key_name   text NOT NULL,                       -- 目前只有 'model_api_key'（一把 key 用到底）
  value      text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key_name)
);

-- 3.17 分片上传会话：只记「计划」，不记进度。
--      进度以桶为准——已有哪几片问 list_prefix(uploads/{u}/convs/{c}/{sid}/)，
--      片长由账本推算（除末片外都等于 part_size，末片是余数），于是没有任何需要
--      谁来维护、也没有机会说谎的中间状态；换实例、换进程、kill -9 都看得见同一份进度。
--      故意**不建 UNIQUE 约束**：一条已 completed 的行不该挡住日后同内容的再传，
--      去重在应用层按 (owner, conv, filename, total_bytes, part_size, sha256) 且只认
--      status='uploading' 做。完成态的行也不删——它是「成品已写、分片没删净」的唯一兜底，
--      由过期清扫按 updated_at 收（该查询不看 status）。
--      complete 时的**逐片摘要清单**（请求体 parts_sha256）**不需要任何 schema 变更**：
--      比对基准就是写入时已经存在对象上的 x-amz-meta-sha256，一次 list_prefix 全带回；
--      对不上的片当场删掉、其余留着，missing_parts 把它们还给续传逻辑——账本无需为此加列。
CREATE TABLE IF NOT EXISTS upload_sessions (
  id            text PRIMARY KEY,                 -- 'up-' + 8 hex
  owner_user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conv_id       text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  filename      text NOT NULL,
  total_bytes   bigint NOT NULL,
  part_size     integer NOT NULL,
  part_count    integer NOT NULL,
  sha256        text NOT NULL DEFAULT '',         -- 整文件指纹，客户端不给就是空
  status        text NOT NULL DEFAULT 'uploading'
                CHECK (status IN ('uploading','completed')),
  material_id   text,                              -- complete 后回填
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now() -- 每收一片 touch 一次（过期依据）
);
```

## 4. MinIO 桶与对象键布局

单桶 `creation-assets`（`MINIO_BUCKET` 可覆盖），四个前缀：

| 前缀 | 内容 | 生命周期 |
|---|---|---|
| `users/{user_id}/convs/{conv_id}/{material_id}{ext}` | 上传素材原样字节，**永不转码** | 随 `materials` 行 ON DELETE CASCADE（应用层删） |
| `uploads/{user_id}/convs/{conv_id}/{upload_id}/part_{NNNNN}` | 分片上传的在途字节（§3.17），定宽补零使字典序==拼装序 | 三处收尸：`complete` 当场删、`abort` 取消即删、过期清扫兜底；**拼装期间与成品短暂共存，桶占用峰值约 2× 文件大小** |
| `renders/{session_id}/{artifact_id}.mp4` | 成片 | 长期，随 `render_jobs.video_object_key` |
| `skills/{skill_name}/{relpath}` | 技能附件（脚本/参考/资产） | 随 `skill_files` |

`ext` 取净化后文件名的扩展名，白名单由 `kind` 判定：视频沿用 `mediaops.py:17 MEDIA_EXTS`、
音频沿用 `mediaops.py:18 AUDIO_EXTS`，**图片需新增 `IMAGE_EXTS`**（前端 `accept` 早已允许选图，
但后端至今没有图片扩展名常量，图片只被当作不可渲染的普通素材——这是本次要补的一处）。
对象键里不出现用户原始文件名，中文名与路径穿越在键面上彻底消失，`filename` 只作为列存展示用。

## 5. 素材主键改造与附件数据流（端到端）

现状链路（全部以本地绝对路径为身份证）：`/upload` 落盘 → 返回 `str(dst.resolve())` →
前端把它贴进输入框 → LLM 从自由文本读路径 → `load_media(paths=[...])` → ffprobe →
这个 path 被写进 FileStore 记录。换层后这五步全断，故整链改为 `material_id`：

```
1  前端 拖拽/选文件 ──▶ POST /upload?filename=…（原始字节流, Bearer token）
2  服务端 边收流边算 sha256 ──▶ ObjectStore.put(...)      ← 不再经本地磁盘中转
3         ffprobe（经 localize 拉的一次临时文件）→ kind/duration/width/height
4         INSERT materials(...) ──▶ 返回 {material_id, filename, kind, duration, url}
5  前端 附件挂到输入框上方的「待发附件条」（chip，可单个 ✕ 移除）
     输入框始终只有用户自己写的话
6  POST /chat {conversation_id, message, attachments:["mat-7f3a9c","mat-2b41de"]}
7  InBound(MQ) ──▶ SessionConsumer ──▶ Agent：user 行落 messages(attachments=…)
8  ContextBuilder 渲染成结构化事实注入本轮上下文：
     「本条消息附带素材：mat-7f3a9c（海边日落.mp4, video, 18.4s）」
9  LLM 调工具：storyline_load_media(material_ids=["mat-7f3a9c"])
10 节点：SELECT materials WHERE id = ANY($1) AND (owner = $2 OR conv_id = $3)
        越权 id 过滤掉并在 payload 记 skipped_unauthorized 一笔
11 Workspace.localize(object_key) → 真实文件 → ffmpeg/MoviePy 渲染
12 成片 ObjectStore.put(renders/…) → render_jobs.succeed(status='done', video_object_key=…,
   duration_sec=…, result={video,duration,width,height,title})   -- 提交即回句柄，此处是后台任务收尾
13 轮询口（MCP render_status / HTTP GET /render_status）在 done 时把 result 平铺回 output，
   media_url = presign_get(key, ttl=3600) 现签 → MediaCardHook → WS type=media
14 前端播放卡片（presigned URL 直接播放，不依赖 /media 静态挂载）
```

MCP 契约随之变：`load_media` 入参 `paths: list[str]` → **`material_ids: list[str]`**，
不留兼容位（D1 全量替换）。`search_media` / `select_BGM` 由 `rglob` 扫盘改为
`materials` 表查询（`origin` 过滤 + 文件名 2-gram 相似度排序，`core_nodes.py` 现有
中文匹配逻辑原样搬到 SQL 侧的参数处理里）。

## 6. 渲染工作区与回收

`cache_dir` 下的本地目录职责收窄为**两种、且都可随时删光**：

- 内容缓存 `objects/{hex[:2]}/{hex}`：`localize` 的落点，LRU 有容量上限
  （`--workspace-max-gb`，默认 20），超限按最后访问时间逐出。
- 会话工作区 `ws/{session_id}/{artifact_id}/{frames,asr,voiceover,bgm,transitions}/`：
  抽帧 jpg、抽音 wav、配音 mp3、转场片段、`progress.json`——写完即用，节点结束后
  `finally` 删除整个 artifact 目录。

三处修改：
- `temp_audio.m4a`（MoviePy 副产物）与 `_fb_{stem}/{i:03d}.mp4`（兜底分片）现在落在
  `out_dir`，等于把临时碎片暴露成可下载媒体 → 改写进会话工作区。
- `StoryNode._work()`（`core_nodes.py:53`）保持"唯一路径出口"的地位，但语义从"数据目录"
  变为"临时工作区"；`render_video` 的产物判定从"文件在不在 out_dir"改为
  `render_jobs.status='done' AND video_object_key IS NOT NULL`。
- `media_url` 不再由 `relative_to(out_dir)` 拼接（对象键与物理路径已无前缀关系）；
  `MediaCardHook._find_media`（`hooks.py:232`）的 `startswith("/media/")` 判断放开为
  scheme 无关（`http(s)://` presigned 亦认）。`/media/*` StaticFiles 挂载随之删除。

## 7. 身份、鉴权与多实例边界

- `POST /register {device_name?}` → 生成 `user_id` + 32 字节随机 token；PG 只存
  `sha256(token)`，明文一次性返回。前端 localStorage 只存 token（`ca.token`），
  原 `ca.user` 废弃。
- 全部 HTTP 与 WS 端点要求凭证（HTTP `Authorization: Bearer`；WS 用 `?token=` 查询参数，
  浏览器 WebSocket 不能带自定义头）。**`user_id` 一律由 token 反查**，不再信客户端传值。
- 每条 repository 查询带 owner 条件；跨 owner 读取返回未找到而非 403，避免会话存在性探测。
- `scheduled_jobs` 取任务用 `SELECT ... FOR UPDATE SKIP LOCKED`，取代整文件覆写 →
  多副本不重复触发同一定时任务。
- 模型密钥按**每次请求**解析（`secrets.resolve_api_key`）：`app_secrets`（当前身份）→
  `OPENAI_API_KEY` → 进程内回落位。`Storage.start()` 把存储句柄绑进 `secrets`，两台服务
  共用这条启动路径，所以谁都不用重启：写入方进程 `invalidate()` 立刻生效，另一进程最多
  晚 `CACHE_TTL_SEC`（5 秒）跟上。读库异常不阻断模型调用，只退回后面的回落层。
  同步阻塞的 VL 调用在异步侧解析一次后显式传值（`vision(api_key=…)`），
  不为读一行库再引入第二个（同步）PG 驱动。

**明确不在本次范围（写明以免被当成已完成）**：WebSocket 回投仍是单实例内存路由——
Connection Manager 的 `{user}/{conv} → ws` 映射在进程内，多副本部署时若消息被 A 实例
消费而用户挂在 B 实例，流式输出收不到。解法是 Redis pub/sub 广播，属独立子项目，
本次不做半套。因此本次交付后**渲染与状态可多实例，实时回投仍需会话亲和（sticky）**。

## 8. 25 处落盘点 → 去向对照

| 落盘机制 | 位置（写/读） | 新去向 | 批次 |
|---|---|---|---|
| FileStore 节点产物 | `orchestration.py:99-112` / `:85-97,:114-120` | PG `artifacts` | B3 |
| 会话历史 JSONL | `session.py:75-78` / `:84-99` | PG `messages` | B4 |
| Message Center 收件箱 | `message_center.py:50-62,:71` / `:64-84` | PG `inbox_messages` | B4 |
| Checkpoint | `checkpoint.py`（写入走 `CheckpointManager`，读取走 `storage.repos` 两张表） | PG `checkpoints`+`checkpoint_entries`（2026-09-23 改为一整链，见该日设计） | B4 |
| Cron 定时任务 | `tools/cron.py:170-176` / `:160-168` | PG `scheduled_jobs` | B4 |
| Heartbeat | `heartbeat.py:30`（同上） | 同表，`name='heartbeat'` | B4 |
| 任务板 + 双向边 | `task_manager.py:90-127` / `:52-62` | PG `tasks`+`task_edges` | B4 |
| 子 Agent 状态 | `subagent_manager.py:68-69` / `:63-66` | PG `subagents`(+租约) | B4 |
| 长期记忆 MD | `memory.py:56-64` / `:52-54,:66-74` | PG `memories` | B4 |
| SKILL 技能库 | `skill.py:95-125`（只读） | PG `skills` + MinIO `skill_files` | B4 |
| `/upload` 素材 | `server.py:156,162` | MinIO + PG `materials` | B2 |
| `/media` 静态挂载 | `server.py:188-191`；URL `core_nodes.py:720-723` | presigned URL | B2 |
| 渲染中间产物 | `core_nodes.py:168,218,467,498,559,62` | 本地工作区（保留）+ LRU/TTL | B3 |
| 成片 mp4 | `core_nodes.py:705-707` | MinIO `renders/` | B3 |
| MediaSettings 四目录 | `settings.py:31-37,:93-98` | 改为 `[storage]` DSN/bucket + 工作区根 | B1 |
| run_server 路径装配 | `run_server.py:93-97,114-137,346-397` | `--storage` + env；补 4 个缺失开关 | B1 |
| 前端 localStorage | `App.vue:30-34,88-90` | PG `users`/`conversations` + `/convs` API | B5 |

**留在本地磁盘、有意不进存储层的九项**：ffmpeg/ffprobe 二进制与 PATH；部署期配置
（`config.toml`、`mcp.json`）；渲染临时工作区与内容缓存（§6，可丢弃、可重算）；
faster-whisper 权重（几百 MB～GB，镜像预置，不该每次冷启动联网拉）；系统字体
（但 `_find_font` 硬编码 `C:/Windows/Fonts` 改成配置项，Linux 上线阻塞）；
`frontend/dist`（构建产物，或 CDN）；运行日志（代码本无写日志文件逻辑，交平台采集）；
`storyline_server/data/templates.json`（随代码发布的静态数据，不是用户数据）；
Agent 的 `write_file/read_file/edit_file/grep`（是能力不是状态）。
第八项修正一处既有风险：文件工具的 root 从 `os.getcwd()`（LLM 可改写整个仓库、
读到 `.env`）收窄到本会话工作区。

## 9. 错误处理

- **启动即校验**：`build_storage("pg_minio")` 在装配阶段跑一次 `SELECT 1` +
  `bucket_exists`（不存在则 `make_bucket`），失败直接抛并退出——不带病服务。
- `schema.sql` 逐句执行，失败时报告具体语句与行，不回滚已建表（幂等，重启再跑）。
- 对象存储瞬时失败：`put` 不重试（流已消费，重试需重读上传流）；`localize` 重试 2 次
  指数退避；`presign_get` 纯本地签名，不会失败。
- DB 不可达：读操作抛 `StorageUnavailable`；写操作**不降级到本地文件**（D1），
  渲染节点捕获后 `render_jobs.status='failed'` + `error`，经现有 WS `type=error` 回投。
- 与 Storyline 既有降级策略的边界：`ProviderError → 降级出片`（VL/TTS/LLM）保持不变；
  **存储层故障不降级**——没有可信素材字节就不出片，绝不产出引用死链的"成功"。
- 孤儿对账：启动后台任务扫 `render_jobs.status IN ('queued','running')` 且 `updated_at` 超 30 分钟者
  置 `failed`（进程崩溃留下的悬挂态）。`queued` 也要扫：提交后还没起跑就崩了，那一行同样在谎报
  「有活儿在干」，不收就等于让轮询端空等。

## 10. 测试与验证

**离线（必跑，不联网、不起容器）**
- 新增 `test_storage.py`：`build_storage` 后端分派与未知后端报错；`minio`/`asyncpg`
  延迟 import（未安装也能 import 本模块）；`schema.sql` 静态检查（每表都有 owner 或
  作用域列、无文件路径列、幂等语句计数）；`MemoryObjectStore`/内存 repos 的契约行为
  （put/get/head/delete、`localize` 内容寻址命中、`SKIP LOCKED` 语义在 double 侧的等价断言）。
- 新增 `test_storage_contract.py`：**同一套用例跑两遍**，一遍 `MemoryStorage`、
  一遍 `PgMinioStorage`（后者容器不可达则整组 SKIP 并在末尾打印未验证清单）。
- 改造 22 个现有 `test_*.py`：构造点注入 `build_storage("memory")`，去掉 `tmp_path`
  式目录断言；`test_server.py` 的上传用例改断言 `materials` 行 + MinIO key +
  presigned URL 三段，不再有 `upload_dir` 落盘检查。
- 全量回归：所有 `test_*.py` exit 0。

**联机（Docker 起 pg16 + minio 后）**
- `test_pg_minio.py`：真实建表幂等跑两遍；`/upload` 真流 → MinIO key 存在 +
  `head.sha256` 与本地算的一致；`presign_get` 回 200 且 `Content-Range` 可用（视频拖动）；
  `localize` 拉下的文件真能被 ffprobe 读出宽高时长；一句话驱动完整渲染出片且
  `renders/…mp4` 存在；两个 worker 并发领 `scheduled_jobs` 不重复执行；
  跨 owner 读他人 `material_id` 被过滤。
- 迁移脚本 `run_migrate.py`：把现有 `.storyline/uploads/*` 与 `.runtime/*.json` 灌进新层，
  **连跑两遍结果一致**（幂等），并输出对账表（迁入 N 条、跳过 M 条及原因）。

## 11. 落地批次（每批全量回归通过再进下一批）

| 批 | 内容 | 出口判据 |
|---|---|---|
| B1 | `storage/` 包骨架（ObjectStore/ABC + Minio/Memory 两实现 + db.py + schema.sql + 全部 repos + `build_storage`）；`docker-compose.yml` + `.env.example`；补 `requirements.txt`（项目当前无主清单）；`Settings`/`run_server` 加 `[storage]`/`--storage` | 离线绿；容器起来后建表成功、重启不报错 |
| B2 | 素材与附件端到端：`/upload` 直写 MinIO、`materials` 登记、presigned 回放、前端待发附件条＋气泡内附件卡片、`load_media(material_ids)` MCP 契约改造 | 上传→卡片播放→`load_media` 命中，全程无本地路径出现在数据里 |
| B3 | 渲染管线：`artifacts` 表取代 FileStore 目录树、`Workspace` localize/publish、LRU+TTL 回收、`search_media`/`select_BGM` 查库、三处媒体面污染修复、`/media` 挂载删除 | 52/52 全绿；真机一句话出片可播放 |
| B4 | 运行时状态迁入（`checkpoints`/`messages`/`inbox_messages`/`tasks`+`task_edges`/`subagents`/`scheduled_jobs`/`memories`/`skills`+`skill_files`）；相关同步接口提 async；文件工具 root 收窄 | 全量回归 exit 0；重启后历史/定时任务/未完成任务都在 |
| B5 | 身份与鉴权（`/register`、token 校验、owner 过滤）、前端会话列表 API（废弃 `ca.convs`）、`run_migrate.py`、设计文档与 `README` 部署章节更新 | 迁移跑两遍一致；换浏览器不丢历史 |

## 12. 运行方式（B1 后可用）

```bash
# 1. 起存储层（本机 Docker Desktop 已就绪；也可换原生 PG/MinIO 或云实例，只改 .env）
docker compose up -d                  # pg16:5432 + minio:9000(console 9001)

# 2. 填密钥（自己填，代码只读环境变量）
copy .env.example .env                # PG_DSN / MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY

# 3. 起两台服务
python -u run_server.py --port 8000 --storage pg_minio
python -u run_storyline.py --config examples/storyline/config.toml   # :8001

# 首次上线迁数据
python -u run_migrate.py              # 幂等，可重跑
```

## 13. 已知边界（不假装完成）

- 实时回投的会话亲和：本批次交付时仍是单实例内存路由（§7 写明「本次不做半套」）。
  该边界已由后续子项目补上——OutBound 帧经 Redis pub/sub 扇出到每个实例
  （`agent_framework/broadcast.py`，用法与剩余边界见 README §3.11）：没挂 WS 的副本如今也攒一份
  **有界**影子缓冲（2026-10-02），接管后重连补得回接管之前的进度；仍补不回的是这个副本订阅频道
  之前的帧、被预算整条淘汰的会话，以及跨重启（缓冲本身还在进程内）。
- `minio` SDK 与 `asyncpg` 需新装；未装时 `--storage memory` 仍可跑全部离线测试，
  但生产路径不可用。
- faster-whisper / edge-tts / 画面理解**已各自真机冒烟过**（`.smoke/b8_tts_smoke.py` 真网络合成、
  `.smoke/vl_shared_key_smoke.py` 与 `settings_key_smoke.py` 打真接口、ASR 走真模型）；
  本机 CUDA 不可用，ASR 实走 CPU+int8，别按 GPU 预算估时。
- 大文件分片续传**已落地并已上线**（§3.17 + §4 的 `uploads/` 前缀，README §3.13）：应用层切片、
  每片是桶里的普通对象，**没有用 MinIO 原生 multipart**——进度只靠一次 `list_prefix`，不需要
  uploadId 与分片 ETag 那套协议，也就不受桶实现约束。内容校验三道全开：逐片指纹（PUT 时核）、
  complete 时的按序逐片摘要清单（`parts_sha256`，只作废对不上的那几片）、init 声明的整文件 `sha256`。
  真正剩下的边界只有一处：**拼装期间桶占用峰值约 2× 文件大小**（有意取舍，磁盘便宜、路径简单）。
  前端已挂「取消上传」（→ `/upload/abort`），放弃的会话不再只能等过期清扫；小文件走单请求路径，
  没有分片会话可作废，故不显示该按钮。
- **D2 那句「本地目录降级为可丢弃缓存」现在字面成立**（2026-09-29 收口，README §3.12）：
  持久化 payload 里只允许 `obj:{object_key}` 一种文件句柄，本机路径只作为渲染期副本存在于
  `render/src/`；会过期的一小时 presigned 直链走 `BaseNode.ephemeral`，只回调用方、不落共享库。
  验证口径不是"看起来没路径"，而是**把本机工作区与内容缓存当场删空后仍能重出同一部片**
  （`tests/test_payload_refs.py` + `.smoke/b13_payload_refs_smoke.py`，后者跑在真 PG+MinIO 上）。
  遗留数据里仍有 **113 行产物、14803 处**指向既不在盘上也不在 MinIO 的旧绝对路径——那是迁移前
  就被删掉的中间字节，`scripts/backfill_payload_refs.py` 只改写字节能证明还在的引用（真库改写
  1039 处后第二遍复核命中 0），不为救不回来的行编一条指向空气的引用；时间旅行回到这些行上重跑，
  当场得到「产物引用的字节不在本机……请重跑产出它的节点」。
