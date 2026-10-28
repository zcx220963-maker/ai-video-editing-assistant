-- 已建库的约束放宽（真引擎专用，由 PgDatastore.ensure_schema() 在 schema.sql 之后逐句执行）。
-- schema.sql 必须保持纯幂等 DDL（不含 DROP），所以任何「改已存在对象」的语句都进这里，
-- 每条都用 DROP IF EXISTS + 重建，跑两遍结果一致。B5 的 run_migrate.py 接管后本文件并入迁移。

-- materials.origin 增 'url'：按链接取料（/fetch_media、fetch_media 工具）新增的入料来源。
-- 增 'render'：渲染终态的成片登记回素材库（归用户，不随对话删除）。
-- 增 'art'：出图/抠图产物（代码生成的素材）——R2 由 assets 栏与 image 通道引入。
-- 这三档必须与 schema.sql 的建表期 CHECK **逐项相等**（多/少一档就是新库老库两条口径），
-- 由 tests/test_migration_origin.py 常驻断言守着；本文件是唯一执行口，别再另开迁移目录。
ALTER TABLE materials DROP CONSTRAINT IF EXISTS materials_origin_check;
ALTER TABLE materials ADD CONSTRAINT materials_origin_check
  CHECK (origin IN ('upload','library','bgm','url','render','art'));

-- checkpoint_entries 增 'tool_call' / 'tool_result'（R10：逐调用恢复点）。
-- 老库的 CHECK 只允许 delta/full/summary，不放宽的话写逐调用条目会直接约束违例。
-- 必须与 schema.sql 的建表期 CHECK 逐项相等（新库/老库两条口径）。
ALTER TABLE checkpoint_entries DROP CONSTRAINT IF EXISTS checkpoint_entries_kind_check;
ALTER TABLE checkpoint_entries ADD CONSTRAINT checkpoint_entries_kind_check
  CHECK (kind IN ('delta','full','summary','tool_call','tool_result'));

-- render_jobs 增 result jsonb：渲染改成「提交 + 轮询」后，终态产物要能在任意实例、
-- 进程重启之后照样重建出来（此前它只活在一次阻塞调用的返回值里）。
ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS result jsonb;

-- render_jobs 增 attempt：渲染跑在杀不掉的线程里，判死/重开后旧线程可能姗姗来迟。
-- 没有令牌围栏时，旧线程跑完会把行从 failed 改回 done（或覆盖新一次尝试的结果）。
ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS attempt text;

-- checkpoints 由「一行装整条 messages 的全量快照」改成「指针行 + checkpoint_entries 增量链」。
-- 本文件先补指针行需要的新列（无条件可重跑），末尾那个 DO 块再做完剩的一半：老库列里
-- 存着的整段上下文迁成链首 full 一致点并把列删掉。
-- （**磁盘**上 `.runtime/checkpoints/*.json` 的遗留 run 不在此列，由 scripts/run_migrate.py 迁。）
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS head_seq integer NOT NULL DEFAULT 0;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS scope jsonb NOT NULL DEFAULT '{}';
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS forked_from text;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS forked_at_seq integer;
CREATE INDEX IF NOT EXISTS ck_fork_idx ON checkpoints(forked_from);
CREATE INDEX IF NOT EXISTS ce_run_idx ON checkpoint_entries(run_id, seq);

-- checkpoints.status 的取值约束。
--
-- 这里**必须**与 schema.sql 里 CREATE TABLE 的那份保持一致（含 awaiting_approval）。
-- 曾经这里是只到 'superseded' 的窄定义：对全新库没有影响（schema.sql 先跑、建的是全量
-- 约束，这条随后把它换回窄的），但只要库里存在一条 awaiting_approval 的挂起 run，
-- 这次加约束就会因「已有行违反」直接失败，而它跑在启动流程里 —— **整个服务起不来**，
-- 且只在「真的挂起过一次审批」的库上复现（真机踩到过：checkpoints_status_check）。
ALTER TABLE checkpoints DROP CONSTRAINT IF EXISTS checkpoints_status_check;
ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_status_check
  CHECK (status IN ('running','completed','failed','superseded','awaiting_approval','cancelled'));

-- 一次性变换：老库里 checkpoints.messages 那一列装着整条工作上下文，把它迁成链首的
-- full 一致点，然后删列——此后正文只有一条去处（checkpoint_entries），不会两套并存。
-- 有状态的变换写成 DO 块 + information_schema 判定，就仍可无条件重跑：列已删过，
-- 条件不成立，块内语句一次都不执行（PL/pgSQL 逐语句懒解析，不存在「引用不存在的列」）。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema = current_schema()
                AND table_name = 'checkpoints' AND column_name = 'messages') THEN
    INSERT INTO checkpoint_entries (run_id, seq, parent_seq, kind, payload,
                                    iteration, created_at_ms)
    SELECT run_id, 0, NULL, 'full', messages, iteration, updated_at_ms
      FROM checkpoints
     WHERE jsonb_typeof(messages) = 'array' AND jsonb_array_length(messages) > 0
    ON CONFLICT (run_id, seq) DO NOTHING;
    ALTER TABLE checkpoints DROP COLUMN messages;
  END IF;
END
$$;

-- 计划门（块 B）：规划 run 与执行 run 是两条普通 run，靠这两列连起来——
-- plan_run_id 是 run B 指回 run A 的谱系指针，plan 装 run A 的候选卡（服务端信任锚）
-- 与 run B 的对账结论。不新增 status 取值、不引入暂停态。
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS plan_run_id text;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS plan jsonb NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS ck_plan_idx ON checkpoints(plan_run_id);
-- HITL 审批断点：status 增 'awaiting_approval'——执行到标注需审批的工具时挂起，
-- 待批动作落在 approval 列；批准后从同一一致点继续，拒绝则回喂拒绝结果。
-- 挂起态不在 running/failed 里，崩溃恢复候选天然看不到它（人工挂了就是等人工，不是等重启）。
-- 用户「停止」（R22，2026-10-28）再加 'cancelled'：停在最近一致点、等人点「继续」。
-- 它同样不在 running/failed 里——重启替用户重按「继续」是事故；`resume()` 只拒 completed，
-- 所以「继续」这条路照样接得起来。这条 ADD 与 schema.sql 建表期那份**逐项相等**。
ALTER TABLE checkpoints DROP CONSTRAINT IF EXISTS checkpoints_status_check;
ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_status_check
  CHECK (status IN ('running','completed','failed','superseded','awaiting_approval','cancelled'));
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS approval jsonb NOT NULL DEFAULT '{}';

-- 多副本执行语义：认领该 run 的实例 id 与租约到期时刻。恢复时按 (status, lease) 原子领取，
-- 跨实例同一 run 只被一个实例接手；单副本下这两列恒为 NULL，既有行为不变。
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS owner_instance_id text;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
CREATE INDEX IF NOT EXISTS ck_lease_idx ON checkpoints(status, lease_expires_at);

-- 账号密码登录:username 与 password_hash 补列。历史匿名身份两列为空,不可密码登录,
-- 但仍可用旧 token。username 唯一索引只约束非空行。
ALTER TABLE users ADD COLUMN IF NOT EXISTS username text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_uidx ON users (username);
-- skills/mcp_servers 加 owner_user_id:技能与动态 MCP 注册按用户隔离
-- (系统预置为 NULL 对所有人可见,用户添加的仅本人可见,删账号级联清理)。
ALTER TABLE skills ADD COLUMN IF NOT EXISTS owner_user_id text REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS owner_user_id text REFERENCES users(id) ON DELETE CASCADE;

-- R11-T53：asset_library 增 asset_role（素材在片子里的角色）。
-- 老库的行一律先是空串（= 未判），补值只走机器那条路（`asset_ingest.py --backfill`），
-- 不给人工 UPDATE 留入口——Q114 否决的就是「先人工标一遍再上线」那种作业。
-- 约束在这里 DROP+ADD 的写法与 schema.sql 的建表期 CHECK **逐项相等**（与 materials.origin
-- 那条同规）：本文件是唯一执行口，新建库走 schema.sql、老库走这里，两边必须落成同一形状。
ALTER TABLE asset_library ADD COLUMN IF NOT EXISTS asset_role text NOT NULL DEFAULT '';
ALTER TABLE asset_library DROP CONSTRAINT IF EXISTS asset_library_asset_role_check;
ALTER TABLE asset_library ADD CONSTRAINT asset_library_asset_role_check
  CHECK (asset_role IN ('','icon','part','scene','texture','bg'));
CREATE INDEX IF NOT EXISTS asset_role_idx ON asset_library(asset_role, enabled);

-- R11-T54：scale_safe（最细笔画实测 ≥2 px）与它的量法回执 scale_facts。
-- **不加 NOT NULL、不设默认值**：NULL = 未判（栅格件、纯填充路径量不出最细笔画），
-- false = 量出来不够细。两者并成一格，「今天没量」就会被读成「不合格」，
-- 而 T54 的清单（哪些件要补）正是从这一格里数出来的。
ALTER TABLE asset_library ADD COLUMN IF NOT EXISTS scale_safe boolean;
ALTER TABLE asset_library ADD COLUMN IF NOT EXISTS scale_facts jsonb NOT NULL DEFAULT '{}';

-- R10-T47：压缩摘要复用 checkpoint_entries 落行（合成 run_id="summary:<会话指纹>"，
-- kind='summary'）。约束在这里 DROP+ADD 的写法与 schema.sql 的建表期 CHECK **逐项相等**
-- （与 asset_library.asset_role 那条同规）：本文件是唯一执行口，新建库走 schema.sql、
-- 老库走这里，两边必须落成同一形状，否则「新建库能写摘要、老库写不进去」会变成
-- 一个只在老部署上复现的 bug。
-- 约束名按 PG 对列级 CHECK 的自动命名规则写（<表>_<列>_check）。
ALTER TABLE checkpoint_entries DROP CONSTRAINT IF EXISTS checkpoint_entries_kind_check;
ALTER TABLE checkpoint_entries ADD CONSTRAINT checkpoint_entries_kind_check
  CHECK (kind IN ('delta','full','summary'));

-- 文字库：会话级偏好（用户在「文字库」里挑过的字幕样式档与音色）。
-- 只存用户真挑过的原始取值，不存派生的档名——理由见 schema.sql 的 conversations 那段。
-- 形状与 schema.sql 的建表期定义一致（本文件是唯一执行口：新库走 schema.sql、老库走这里）。
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS prefs jsonb NOT NULL DEFAULT '{}';

-- token_usage 增缓存命中 / 未命中与花费（元）。形状与 schema.sql 的建表期定义一致
-- （本文件是唯一执行口：新库走 schema.sql、老库走这里）。不设 NOT NULL：老行没有这三项，
-- NULL 的意思是「当时没记」，不是 0。
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_hit_tokens integer;
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cache_miss_tokens integer;
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS cost_yuan double precision;

-- R19：任务状态改设计原文口径（pending → on_progress → completed）。
-- 老库的行是 claimed；约束先删再改再加，与本文件其他 DROP+ADD 同规，可反复执行。
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
UPDATE tasks SET status = 'on_progress' WHERE status = 'claimed';
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
  CHECK (status IN ('pending','on_progress','completed'));
