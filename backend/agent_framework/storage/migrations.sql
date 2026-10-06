-- 已建库的约束放宽（真引擎专用，由 PgDatastore.ensure_schema() 在 schema.sql 之后逐句执行）。
-- schema.sql 必须保持纯幂等 DDL（不含 DROP），所以任何「改已存在对象」的语句都进这里，
-- 每条都用 DROP IF EXISTS + 重建，跑两遍结果一致。B5 的 run_migrate.py 接管后本文件并入迁移。

-- materials.origin 增 'url'：按链接取料（/fetch_media、fetch_media 工具）新增的入料来源。
ALTER TABLE materials DROP CONSTRAINT IF EXISTS materials_origin_check;
ALTER TABLE materials ADD CONSTRAINT materials_origin_check
  CHECK (origin IN ('upload','library','bgm','url'));

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
  CHECK (status IN ('running','completed','failed','superseded','awaiting_approval'));

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
ALTER TABLE checkpoints DROP CONSTRAINT IF EXISTS checkpoints_status_check;
ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_status_check
  CHECK (status IN ('running','completed','failed','superseded','awaiting_approval'));
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
