-- 上线存储层 schema（PostgreSQL 16）。幂等：可反复执行，跑两遍结果一致。
-- 由 storage/db.py 的 ensure_schema() 在服务启动时逐句执行。
-- 表列的权威定义在此文件；内存引擎与仓储层复用本文件解析出的元数据（storage/ddl.py）。

-- 身份：取代前端 localStorage 的 ca.user（客户端自造、可伪造）
CREATE TABLE IF NOT EXISTS users (
  id            text PRIMARY KEY,
  token_hash    text NOT NULL UNIQUE,
  username      text UNIQUE,
  password_hash text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- 会话：取代 localStorage 的 ca.convs（含 50 条截断）
CREATE TABLE IF NOT EXISTS conversations (
  id            text PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title         text NOT NULL DEFAULT '新对话',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conv_user_idx ON conversations(user_id, updated_at DESC);

-- 消息：取代 .runtime/sessions/{user}/{conv}.jsonl
-- qa 列原样存文档的 QA 结构 {"question": str, "answer": [{"type":"think"|"tool call"|"answer",...}]}
CREATE TABLE IF NOT EXISTS messages (
  id            bigserial PRIMARY KEY,
  conv_id       text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq           integer NOT NULL,
  role          text NOT NULL CHECK (role IN ('user','assistant')),
  content       text NOT NULL DEFAULT '',
  attachments   jsonb NOT NULL DEFAULT '[]',
  qa            jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conv_id, seq)
);

-- 素材登记：取代「本地绝对路径当主键」整条链路
CREATE TABLE IF NOT EXISTS materials (
  id            text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conv_id       text REFERENCES conversations(id) ON DELETE SET NULL,
  object_key    text NOT NULL UNIQUE,
  filename      text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('video','audio','image')),
  mime          text NOT NULL DEFAULT '',
  bytes         bigint NOT NULL,
  sha256        text NOT NULL,
  duration_sec  double precision,
  width         integer,
  height        integer,
  has_audio     boolean NOT NULL DEFAULT false,
  origin        text NOT NULL DEFAULT 'upload'
                CHECK (origin IN ('upload','library','bgm','url')),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mat_owner_idx  ON materials(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS mat_conv_idx   ON materials(conv_id);
CREATE INDEX IF NOT EXISTS mat_origin_idx ON materials(origin);

-- 分片续传账本：一行 = 一次「谁、往哪个会话、传多大文件、切成几块」。
-- 只记计划不记进度：分片本身就放在对象存储的 uploads/ 前缀下，一块一个对象，
-- 「已有哪几片」问一次 list_prefix 就知道。所以进度不落在任何进程的内存里，
-- 刷新页面、断网重连、换副本接着传都成立；收齐后按序拼流交给 ingest_bytes。
CREATE TABLE IF NOT EXISTS upload_sessions (
  id            text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conv_id       text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  filename      text NOT NULL,
  total_bytes   bigint NOT NULL,
  -- part_size 由服务端定下并回给客户端：所有分片必须等长（末片可短），
  -- 于是「某一片该多大」是可推算的，不必逐片记账
  part_size     integer NOT NULL,
  part_count    integer NOT NULL,
  -- 整文件指纹：客户端给则在拼接完成时校验，空串表示未提供（只按字节数收口）
  sha256        text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'uploading'
                CHECK (status IN ('uploading','completed')),
  -- 拼装入库后的素材行 id；completed 之外为 NULL
  material_id   text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ups_owner_idx  ON upload_sessions(owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ups_status_idx ON upload_sessions(status, updated_at);

-- 节点产物：取代 FileStore 的 {会话}/{节点}/{产物}.json 目录树
CREATE TABLE IF NOT EXISTS artifacts (
  session_id    text NOT NULL,
  node          text NOT NULL,
  artifact_id   text NOT NULL,
  payload       jsonb NOT NULL,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, node, artifact_id)
);

-- 渲染任务：取代 out_dir 里的 render_progress.json 探针（跨实例可见）
CREATE TABLE IF NOT EXISTS render_jobs (
  id               text PRIMARY KEY,
  session_id       text NOT NULL,
  artifact_id      text NOT NULL,
  status           text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','running','done','failed')),
  stage            text NOT NULL DEFAULT '',
  percent          integer NOT NULL DEFAULT 0,
  video_object_key text,
  duration_sec     double precision,
  error            text,
  -- 终态产物（video/duration/width/height/title…）：轮询端据此重建结果，跨实例、跨重启
  -- 都能给出与阻塞渲染同形状的输出。不含 media_url——presigned 直链会过期，读时现签。
  result           jsonb,
  -- 尝试令牌（attempt fencing）：渲染跑在**杀不掉的线程**里，被判死或重开之后旧线程
  -- 仍可能姗姗来迟。每次 open / 重开 / 判死都换一个令牌，渲染体写进度与终态时带上它，
  -- 令牌不符的写一律丢弃——否则会出现 failed → done 的翻转、旧产物覆盖新一次尝试。
  attempt          text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, artifact_id)
);

-- Checkpoint：run 指针行。一致点正文在 checkpoint_entries，本行只记「链尾在哪 + 状态 + 产物作用域」。
CREATE TABLE IF NOT EXISTS checkpoints (
  run_id         text PRIMARY KEY,
  session_id     text NOT NULL,
  message        text NOT NULL DEFAULT '',
  iteration      integer NOT NULL DEFAULT 0,
  head_seq       integer NOT NULL DEFAULT 0,
  status         text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running','completed','failed','superseded','awaiting_approval')),
  -- 本次执行占用的剪辑产物作用域 {storyline_session, artifact_id}：fork 按它复制产物集
  scope          jsonb NOT NULL DEFAULT '{}',
  forked_from    text,
  forked_at_seq  integer,
  -- 计划门（块 B）：确认后的执行 run 指回规划 run 的谱系指针；普通 run 为 NULL。
  plan_run_id    text,
  -- 计划侧数据都装在这一个 jsonb 里（run A 的 candidates / run B 的 audit），
  -- 让规划与执行两条 run 都仍是普通 run——status 状态机一格都不额外增加。
  plan           jsonb NOT NULL DEFAULT '{}',
  -- HITL 审批断点：执行到标注「需人工审批」的工具时挂起，待批动作（工具调用、理由、
  -- 请求 id）落在这里；其余时刻为空。批准后从同一一致点继续，拒绝则回喂拒绝结果。
  approval       jsonb NOT NULL DEFAULT '{}',
  -- 多副本归属：认领该 run 的实例 id 与租约到期时刻。单副本下恒为 NULL，不改变既有行为。
  owner_instance_id text,
  lease_expires_at  timestamptz,
  created_at_ms  bigint NOT NULL,
  updated_at_ms  bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS ck_status_idx ON checkpoints(status);
CREATE INDEX IF NOT EXISTS ck_time_idx   ON checkpoints(updated_at_ms);
CREATE INDEX IF NOT EXISTS ck_fork_idx   ON checkpoints(forked_from);
-- 多副本：按 (status, lease) 扫可接手的 run；审批挂起的不在 running/failed 里，天然不参与恢复。
CREATE INDEX IF NOT EXISTS ck_lease_idx  ON checkpoints(status, lease_expires_at);

-- 一致点增量链：原来每轮把整条 messages 全量 upsert 回一行（几十 KB 写放大），
-- 现在每行只存自上一致点新增的消息；上下文压缩改写了旧消息时那一条退化成 full 基准。
-- 归属随 checkpoints 指针行（按 run_id 取链），本表不重复挂会话列。
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

-- A2A 收件箱：取代 Message Center 的 {user}/{conv}/{agent}.jsonl
CREATE TABLE IF NOT EXISTS inbox_messages (
  id          bigserial PRIMARY KEY,
  user_id     text NOT NULL,
  conv_id     text NOT NULL,
  agent       text NOT NULL,
  type        text NOT NULL DEFAULT 'message',
  sender      text NOT NULL DEFAULT '',
  content     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  consumed_at timestamptz
);
CREATE INDEX IF NOT EXISTS inbox_pending_idx ON inbox_messages(user_id, conv_id, agent)
  WHERE consumed_at IS NULL;

-- 任务板：取代 task_{id}.json + 读-改-写维护的双向边
CREATE SEQUENCE IF NOT EXISTS task_seq;
CREATE TABLE IF NOT EXISTS tasks (
  id            integer PRIMARY KEY,
  scope         text NOT NULL,
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','claimed','completed')),
  owner         text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS task_edges (
  task_id     integer NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on  integer NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on)
);

-- 子 Agent 登记：租约取代重启后残留的 working/idle 谎报
CREATE TABLE IF NOT EXISTS subagents (
  scope             text NOT NULL,
  name              text NOT NULL,
  prompt            text NOT NULL DEFAULT '',
  status            text NOT NULL DEFAULT 'idle'
                    CHECK (status IN ('idle','working','shutdown')),
  lease_expires_at  timestamptz,
  PRIMARY KEY (scope, name)
);

-- 定时任务：cron 与 heartbeat 合并同表（心跳只是其中一个 job）
CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id               text PRIMARY KEY,
  name             text NOT NULL UNIQUE,
  task             text NOT NULL DEFAULT '',
  enabled          boolean NOT NULL DEFAULT true,
  delete_after_run boolean NOT NULL DEFAULT false,
  schedule         jsonb NOT NULL DEFAULT '{}',
  state            jsonb NOT NULL DEFAULT '{}',
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- 长期记忆：取代 User.md / Tool.md 全文覆盖
CREATE TABLE IF NOT EXISTS memories (
  user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category   text NOT NULL CHECK (category IN ('user','tool')),
  content    text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, category)
);

-- 技能：正文进 PG（多实例一致分发），scripts/references/assets 进 MinIO
-- owner_user_id 为 NULL = 系统内置（所有用户共享）；填了 = 用户个人添加（仅本人可见）
CREATE TABLE IF NOT EXISTS skills (
  name           text PRIMARY KEY,
  owner_user_id  text REFERENCES users(id) ON DELETE CASCADE,
  description    text NOT NULL DEFAULT '',
  frontmatter    jsonb NOT NULL DEFAULT '{}',
  body           text NOT NULL DEFAULT '',
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS skill_files (
  skill       text NOT NULL,
  relpath     text NOT NULL,
  object_key  text NOT NULL,
  bytes       integer NOT NULL DEFAULT 0,
  PRIMARY KEY (skill, relpath)
);

-- 时间线编辑器持久化：用户手工保存的可编辑时间线 JSON
CREATE TABLE IF NOT EXISTS timelines (
  id            text PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conv_id       text REFERENCES conversations(id) ON DELETE SET NULL,
  name          text NOT NULL DEFAULT '未命名时间线',
  payload       jsonb NOT NULL DEFAULT '{}',
  video_url     text,
  duration_sec  double precision,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tl_user_idx ON timelines(user_id, updated_at DESC);

-- 模型密钥：前端「设置」自配置，取代「改环境变量 + 重启两个进程」
-- value 只能明文存——主服务与 Storyline 都要拿它真发请求；对外可见的一切出口
-- （HTTP 响应、日志、dump-all）一律经 SecretsRepo/secrets.mask 掩码后才出门。
CREATE TABLE IF NOT EXISTS app_secrets (
  user_id    text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key_name   text NOT NULL,
  value      text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key_name)
);
-- LLM token 用量流水（配额观测 + 成本核算的数据源）
CREATE TABLE IF NOT EXISTS token_usage (
  id                bigserial PRIMARY KEY,
  user_id           text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id        text,
  run_id            text,
  model             text,
  prompt_tokens     integer NOT NULL DEFAULT 0,
  completion_tokens integer NOT NULL DEFAULT 0,
  total_tokens      integer NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tk_user_time_idx ON token_usage (user_id, created_at);
CREATE INDEX IF NOT EXISTS tk_run_idx ON token_usage (run_id);

-- 动态 MCP Server 注册：配置与启停状态落库，运行时热连/热断（区别于部署期的 mcp.json）。
-- config 是 MCPServerConfig 的字面量（type/command/args/env/url/headers/tool_timeout/
-- enabled_tools）；headers 里可能带凭证，对外出口必须掩码后回显。
-- owner_user_id 为 NULL = 系统部署期配置（共享）；填了 = 用户自己添加（仅本人可见）
CREATE TABLE IF NOT EXISTS mcp_servers (
  name          text PRIMARY KEY,
  owner_user_id text REFERENCES users(id) ON DELETE CASCADE,
  config        jsonb NOT NULL DEFAULT '{}',
  enabled       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
