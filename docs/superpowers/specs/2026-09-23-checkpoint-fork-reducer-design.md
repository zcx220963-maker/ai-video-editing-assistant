# 2026-09-23 · checkpoint 增量链 + fork 原语 + reducer 契约（优化轮）

> 定位：本轮不是加功能，是把框架侧三处「比 LangGraph 弱」的地方补齐成完整链路，
> 并顺手删掉一处会造成第二套真相的装配。前两份设计文档（09-19 真节点、09-20 存储层）
> 里与本文冲突的段落已就地改写并指向本文。

## 0. 三条原始弱点与决策

| 弱点 | 现象 | 决策 |
|---|---|---|
| checkpoint 写放大 + 无 fork | 每轮把整条 `messages` 全量 upsert 回一行 jsonb；恢复只能"从最新一致点重跑一次 LLM"，回不到第 3 轮换分支 | 指针行 `checkpoints` + 一致点增量链 `checkpoint_entries`（`parent_seq` 串起来），`fork` 成一等操作 |
| 并行没有 reducer 契约 | `concurrency_safe` 靠"同批次里出现了被依赖的节点就退化串行"的一次性推理；多工具写同一 store key 的竞态没有定义 | 节点侧声明 `reads`/`writes`，调度侧 `conflicts()` + `plan_batches()` 显式分批 |
| DAG 造了两遍、只装一遍 | 连上真 Storyline 就 `unregister` 本地 NodeTool，而远程 MCP 工具不经过本地两阶段 Interceptor —— 依赖补齐与 `require_prior_kind` 只活在 mock 模式 | 服务端为唯一权威：mock 退出生产装配，只留离线测试夹具；未连通即**无剪辑能力**并明确告警 |

配套范围（同轮拍板）：fork 同时开放**执行面（HTTP/CLI）**与**模型工具 `rerun_from`**；
一并清掉存储层遗留三项、成片卡刷新重放、`handle()` 劫持新消息、`ToolError` 类型化。

## 1. 指针行 + 一致点增量链

`checkpoints` 只答"到哪了"（`iteration` / `head_seq` / `status` / `scope` / 分叉来源），
`checkpoint_entries` 答"走过哪些一致点"。DDL 见存储 spec §3.7。

- **增量优先**（`checkpoint.py:138 _append_entry`）：新消息是已落链的后缀 → 只写新增段
  （`kind='delta'`）；上下文压缩改写了旧消息 → 那一条退化成 `kind='full'` 基准，
  `rebuild()` 遇到 full 就重新起算。所以链不是纯 append 日志，而是"基准 + 段"。
- **写放大实测口径**：`test_fork_time_travel.py` 钉住"单条 payload 最长只有 2 条消息"，
  且 `head_seq == len(entries) - 1`、指针行里**没有** `messages` 键。
- **时间旅行**：`load(run_id, at_seq)` 用 `load_entries(upto_seq=at_seq)` 截断重建，
  崩溃恢复与"回到第 N 步"共用同一条读路径。
- 老库的 `.runtime/checkpoints/*.json` 由 `scripts/run_migrate.py` 迁成"一条 full + 指针行"
  （自然键仍是 `run_id`，幂等）。

## 2. fork：三件事必须同时发生

`CheckpointManager.fork(run_id, at_seq, *, run_id_new, invalidate)`（`checkpoint.py:266`）：

1. 取 `at_seq` 那个一致点重建状态，开子 run（`forked_from` / `forked_at_seq` 记账）；
2. **换新 `artifact_id`** 并把父 run 的产物集整份复制过来（`_clone_artifacts`），
   再删掉 `invalidate` 里的节点产物 —— 不删，服务端拦截器会认为它们已完成，
   恰好跳过用户要求重跑的那一步；不换新作用域，试错就把父那一版改花了；
   父没显式 `artifact_id`（产物落在 `_default`）时同样复制。
   这一步复制的是 **payload 原文**，所以"分叉出去只重跑分叉点之后"能成立的前提，
   是 payload 里的文件句柄只有 `obj:` 对象键（引用契约见存储 spec §13 与 README §3.12）：
   留的是本机绝对路径，子作用域就拿着一段属于别人机器的地址，换实例或清过盘之后
   拦截器判定「上游已完成」而终点节点取不到字节。
3. 父 run 就地置 `superseded`：分叉出去后它已让位，留在 `running` 里会被崩溃恢复
   重播一遍用户不要的分支。

`invalidate` 的来源是 `EditingContract.downstream(node)`（重跑点本身 + 全部下游）。
两条入口都收敛到 `fork`，所以"父 run 让位"不放给调用方各自记得做。

- **模型侧** `rerun_from(node, reason, instruction)`（`tools/runs.py`）：只给节点名，`seq_before_tool(run_id, node)`
  在链上找"该工具结果出现之前"的一致点当分叉点。两处如实回绝：找不到该工具的执行记录
  （列出已有工具名），结果落在链首（前面没东西可复用，那等于重开一轮）。
  工具把子 run 放进 `ctx.extras["handover"]`，循环调 `_adopt_fork` 就地接手：
  回退消息、按 contextvar 换绑产物作用域、重播种迭代计数，并留一条"已回到一致点"的系统注记。
- **分叉带一条新诉求**（`fork(message=)` / 工具的 `instruction`，落 `checkpoint.py:fork`）：
  它既是子 run 指针行的 `message`，也作为一条 user 消息接在回退后的上下文尾部。
  这条通道由 b7 真机冒烟逼出来：一致点写在每次 LLM 调用之前，所以回到「select_BGM 之前」
  后链尾正是父那条旧指令 —— 不带新诉求时模型连着分了九条叉，每条都原样重选出同一首歌，
  看起来像分叉坏了，其实是"回溯语义把用户的新想法擦掉了"。注记尾部同时告诉模型
  「上一条用户指令就是这次重跑要照办的新要求」。
- **执行面**：`POST /runs/{id}/fork`（`server.py:434`）与 `/resume` 都**不直接执行**，
  而是投一帧带 `action` 的 MQ 消息 —— 流式回投、同会话串行、错误回执全部复用 `/chat`
  那一条路；子 `run_id` 由服务端先起好再回给客户端。

## 3. reducer 契约与并发分批

`editing_contract.py`：节点定义只活在服务端，以前靠正则从工具描述里抠"（需先完成：a, b）"
—— 把契约写成散文再解析回来，改文案就断。现在 `load_contract()` 从 Storyline 取结构化契约：

```
NodeContract(name, requires, reducer)  →  reads  = {store:<dep>…}
                                           writes = {store:<self>}   # store_key() 与 BaseNode.write_keys 同形状
```

`Tool.reads/writes` 由此填上，调度侧只剩一条确定性规则（`tool.py:233`）：

- `conflicts(a, b)` = 写集相交（谁最后写没定义）**或**一方写集撞上另一方读集（会读到半程结果）；
- `plan_batches(calls, resolve)` 贪心且**保持模型给出的原始顺序**：每个调用尽量塞进已有的
  靠前排次，塞不进就开下一批；`concurrency_safe=False` 者独占一批并封死该排；
- 未声明读写集的工具不参与冲突判定，但仍受 `concurrency_safe` 这道粗门禁约束 ——
  **契约取不到时保守串行，正确性不依赖契约存在**。

判据钉在 `test_video_editing.py`：`render_video` 声明 `store:plan_timeline → store:render_video`
后即可并发；`plan_timeline` 撞 `select_BGM` 的写集所以退到下一批，
`[select_BGM, generate_voiceover] → [plan_timeline]`。

`ToolError` 同步类型化：失败不再是"以 Error 开头的字符串"这种约定，而是带工具名与详情的
异常；`str(e)` 才拼出回喂模型的文本，`is_tool_error()` 是唯一判定口。

## 4. DAG 只装一遍

`run_server.py:187-193` 不再注册任何本地剪辑节点。Storyline 未连通时工具表里没有任何剪辑
节点，`rerun_from` 因契约空而如实回绝，`plan_editing_team` 回绝而非交一张空板
（`test_team_wiring.py` 双向钉住）。`agent_framework/video_editing.py` 保留为**离线夹具**：
节点定义、DAG 联机用例、以及"服务端不可用时怎么测"靠它，服务不接入。

## 5. 同轮修掉的两个真 bug

- **`ensure_schema` 语句顺序**（`storage/db.py`）：已建过的库里 `checkpoints` 早就存在，
  `CREATE TABLE IF NOT EXISTS` 跳过它，于是 schema.sql 尾部引用新列的
  `CREATE INDEX … ON checkpoints(forked_from)` 会排在 migrations.sql 的 `ALTER ADD COLUMN`
  **之前** → 真机启动直接失败，而 `test_storage_contract.py` 在连不上 PG 时静默 SKIP，
  所以离线全绿把它藏住了。现在三阶段执行：建表/序列 → migrations → 索引。
- **`handle()` 不再劫持新消息**（`agent.py:353`）：续跑改为显式 `resume=True`，由入口处
  （consumer/server）判断"这条是不是『继续』"；带新消息进来就按新消息开新 run。

## 6. 执行面（本轮新增端点）

| 端点 | 用途 |
|---|---|
| `GET /convs/{cid}/runs` | 本会话全部执行（含已完成与分叉出来的），时间旅行面板数据源 |
| `GET /convs/{cid}/runs/active` | 有无在途 run（前端刷新后接续进度） |
| `GET /runs/{run_id}` | 指针行视图（`head_seq`/`scope.artifact_id`/分叉来源） |
| `GET /runs/{run_id}/history` | 一致点链：`seq`/`iteration`/`kind`/增量条数/`tools`（该步跑过的节点） |
| `POST /runs/{run_id}/fork` | `{at_seq, rerun_nodes[], message}` → 投 MQ，返回子 `run_id` |
| `POST /runs/{run_id}/resume` | 崩溃恢复与时间旅行同一入口（`at_seq` 可选） |

跨 owner 的 run 一律按 404 处理（归属校验不泄露存在性）；未配置 checkpoint 时 503。

**这张面已经接到前端**（`frontend/src/App.vue` 的 `执行记录` 抽屉）：清单列本会话所有 run
（状态/轮次/一致点范围/产物集/`← 分自 父 第 N 步`），点开看一致点链并按 `tools` 标出每步跑了哪些节点；
点一个点即选它当分叉点 —— `at_seq` 自动取「这一步之前」，该步节点预勾，可改可另写新诉求，
两个按钮分别打 `fork` 与 `resume`。同轮修掉一处真 bug：`resume` 先前发裸 POST，
被 `RunResumeRequest` 的请求体校验判 422，现在带 `{}`。

## 7. 验证与未验证

**已验证（离线）**：全量回归 39 个 `test_*.py` 逐个 `python tests/test_x.py` 跑完全绿（这些是自带
`asyncio.main` 的脚本，**不能用 pytest 收集** —— 会把每个 `async def` 判成"缺异步插件"而全线 FAILED），
其中 `test_fork_time_travel.py` 35 项为本轮新增
（增量链形态、`load(at_seq)` 截断、fork 的作用域换绑/产物复制与作废、`superseded` 退出候选、
`rerun_from` 的三处回绝、接手后身份切换、**分叉带新诉求时指针行与上下文尾部两处留痕且复用区间不变**）；
`test_storage_contract.py` 184/184 在
**内存替身与真 PG+MinIO 各跑一遍**（新增的分叉/清单查询逐条过真引擎）；
迁移语句在本地真 PG（容器 `creation-pg`）上过：`checkpoints.messages` 列已消失、
`head_seq`/`scope`/`forked_from`/`forked_at_seq` 与 `ck_fork_idx`/`ce_run_idx` 在位，
第二次 `Storage.start()` 成功（DDL+migrations 可重跑）。本机遗留数据也已**真迁完**：
`scripts/run_migrate.py --dry-run` 八类全部报「迁入 0」（users / messages / scheduled_jobs /
artifacts 逐条跳过：已存在，不覆盖库里较新的运行态），`.runtime/checkpoints/` 那 11 个 JSON
按 `run_id` 复查 **11/11 都在**、各成链首一条 `full` 基准（`head_seq=0`）；
盘上源文件按迁移器「迁完不删」的口径留在原地。

**已验证（真机）**：`.smoke/b6_fork_smoke.py` 一次跑完 33 项全绿并自清零残留，
两段各自起当前代码的进程、真端口、真 DeepSeek、真 PG+MinIO（密钥只走页面同款写入口，
值不打印）：

- **① 契约面 6/6**：真 Storyline 的 `tools/list` 里确有 `dag_contract`（21 工具），
  `load_contract()` 解析出 **19 节点**且每个都带写集与依赖元组（`Tool.reads/writes` 原料齐了），
  `downstream("select_BGM")` = `{select_BGM, plan_timeline, plan_timeline_ai_transition,
  plan_timeline_pro, render_video}`；契约取不到时 `downstream()` 退化成只作废自己。
- **② 执行面 27/27**：`run_server.py --no-storyline` 启动日志明确宣告无剪辑能力，
  工具表 23 个里**没有任何剪辑节点**，而 `rerun_from` 仍常驻（契约空时它自己回绝）；
  真对话跑完后指针行**无** `messages` 键、链连续且 `len == head_seq+1`、链首 full 其后按增量、
  最长 payload 2 条 < 全链 3 条（写放大在真库上确实没了）；`/history` 与库里的链一致、
  本会话清单列得出这条 run、别人的 run 一律 404；`POST /runs/{id}/fork` **只投一帧 MQ**
  （不就地执行），子 run 经真 MQ 跑完 → 父 `superseded` → 子记着 `forked_from`/`forked_at_seq`
  → 子另起一条链且链首是基准 → 分叉留在同一会话、清单按 owner 过滤 → `completed`/`superseded`
  都不算在途 → 不带 resume 的新消息开新 run → 三条执行的答复落回同一会话历史。

**已验证（真机 · 整链）**：`.smoke/b7_bgm_rerun_smoke.py` 36 项全绿、零残留，真 Storyline + 真主服务
（真 MQ/WS）+ 真 PG + 真 MinIO + 真 DeepSeek + 真 ffmpeg/MoviePy，三段同一条片子：

- **① 出第一版**：曲库导入两条测试音轨 → 一句话驱动 → 选到 A → 渲染出片可播。
- **② HTTP 分叉换 B**：回 `select_BGM` 之前那个一致点、只重做选乐（连带下游作废）→ 子 run
  换新作用域、上游 `split_shots` 产物**逐字节复用**、父那一版原样不动、新片 ffprobe 量得出时长与分辨率。
- **③ 模型侧 `rerun_from` 换回 A**：同一轮里模型先出一版再自己回到选乐那一步，
  `instruction` 落成子链尾部的 user 消息、子 run 真的重新选了一次乐且**换到了另一首**、
  时间线带着重选后的那一首、又出一版新片（新对象键、763856 B / 5.0 s）、
  父 run 让位、子链里留着"已回到一致点"的交接注记。

**已验证（真机 · 配音）**：`.smoke/b8_tts_smoke.py` 全部通过，edge-tts 走真网络：provider 层合成
55728 B、ffprobe 量出 9.29 s 的可播 mp3（不是占位空文件）；节点层真 `build_providers` 跑
`generate_voiceover`，两段口播每条都标 `edge-tts`、自报时长与真音频相符、payload 真进 Store；
**反向对照**换必然不存在的 voice 必须落 `silent_fallback` 且不抛穿 —— 没有这条对照，
"标了 edge-tts"说明不了什么（可能只是没判降级）。

**已验证（真机 · 渲染提交 + 轮询）**：b3 与 b7 都已在**新代码**上重跑过——b3 里 `wait_sec=0`
那段量到 37 ms 只回句柄与 hint、再由 `render_status` 追到 done 才交出片与现签直链（DIRECT SMOKE
PASSED，0 失败）；b7 36 项全绿、零残留，三处渲染证据都改成等终态再取。
另加一条 `.smoke/b9_render_poll_smoke.py` 专验**模型侧**会不会真的去轮询：临时 Storyline 把
`render_grace_sec` 关成 0（判据取 `tomllib` 解析结果而不是字符串包含——`[capabilities]` 这行
在注释里也出现过，无锚定替换会把开关插进注释），再一句话驱动整条剪辑链，16 项全绿、零残留：
提交那次只回 `render.status='queued'` 且 `hint` 指向 `render_status`，模型连查三次得到
`['running', 'running', 'done']`，done 那一版带回 `output.video`，成片卡是在**轮询到 done 的那一轮回投**
WS 的，`render_jobs` 落 `done/100`，`GET /render_status` 与工具同形状且别人的查不到（404）。
取证口径记一条：done 的返回值**不能从 WS 帧读**——`ToolTraceHook` 把 `result` 截到 600 字符给
前端展示，带对象键与现签直链的那一版必然被截断（第一次跑就被它误判成"没查到 done"），
b9 改从 checkpoint 链上 tool 消息的全文取判据，那才是模型读到的原文。

**真机发现（已在 README §3.3.1 落到操作规程）**：崩溃恢复的口径是「接手库里**所有**
`running`/`failed` 的 run」，不带 `--no-resume` 起临时实例会重播别人没跑完的分支 ——
第一次冒烟就把一条外来 run 从 `running` 翻成了 `failed`（行数没丢，只是状态）。
`.smoke/` 下的脚本因此一律带 `--no-resume`。

**未验证 / 未做（不假装完成）**：

- **渲染改成「提交 + 轮询」（README §3.10）的证据链**：`test_storyline_server.py`（现共 41 项，
  本轮新增其中「渲染任务视图」一整段）用 stub 渲染体钉住了提交即回句柄、同作用域不重开、
  grace 超时回进度视图、
  done 视图带 `duration` 与 `://` 直链、`{node, artifact_id, output}` 三键不破、跨 dispatcher
  查得到别的实例跑完的任务、异常落 `failed`、`wait_sec` 被 `render_wait_max_sec` 夹住；
  真 PG 侧 `result jsonb` 走的是 `migrations.sql` 同款可重跑语句（`.tmp/verify_render_jobs_pg.py`）。
  真机那三段（b3 / b7 / b9）见上「已验证（真机 · 渲染提交 + 轮询）」。
- **前端 UI 验的是种子数据**：`执行记录` 抽屉的清单/链条/分叉/续跑都在浏览器里点过，
  但喂的是按真实形状造的 run 行（`.smoke/ui_seed_demo_runs.py`，跑完用
  `.smoke/ui_clean_demo_runs.py <user_id>` 定点收回）；那个临时身份没配模型密钥，
  点"从这里分叉重跑"后子 run 必然停在「未配置模型密钥」——**通路**（fork 200、子 run 上清单、
  换新作用域、父变已让位、续跑 200）验过，**UI 一路点到真出片**没有在同一次点击里覆盖，
  那一段只由 b7 冒烟在后端侧覆盖。种子行与临时身份跑完已从共享 PG 里清干净（含 CASCADE 的用户行）。
- **已验证（真机 · 跨轮回溯）**：`.smoke/b10_cross_run_rerun_smoke.py` 20 项全绿、零残留。第二轮的
  提示词**不含任何 run_id**，模型先在本轮调用 `rerun_from` 被回绝、照回绝文本列出的历史执行把上一轮
  那条 id 填回来重试；子 run 的 `forked_from` 是**上一轮**那条（≠ 本轮发起那条）、换新产物作用域、
  `select_BGM` 换成另一首、时间线带着重选后的那一首、上游 `split_shots` 产物逐字节复用、父版本原样
  不动、又出一版新片。离线侧 `test_fork_time_travel.py` 49 项补了前缀解析、歧义回绝、跨会话回绝、
  找不到节点时把历史执行当路标交出来，以及**本轮那条被弃用的 run 也得就地让位**（否则它停在
  `running`，崩溃恢复会重播一条已弃用的分支）。
- 剩余边界：跨轮回溯的候选只来自**本会话清单里还留着 checkpoint 链**的执行——被 `prune` 收掉的旧
  run、别的会话的 run 都不在索引里，那种情况仍只能走前端 / HTTP `fork`。
- **已验证（真机 · 独立 CLI 客户端）**：`scripts/runs_cli.py` 只用公开 HTTP/WS 端点
  （`register` / `chat` / `runs` / `history` / `fork` / `resume`），token 只从环境变量或凭证文件
  读、任何一条路径都不打印值；`--before-node` 与服务端 `Checkpoint.seq_before_tool` 同一条规则
  （第一次落该节点结果的那个一致点减一，链首即回绝），不是客户端自认一套。
  `.smoke/b12_cli_smoke.py` 在临时实例（`--no-resume` + 随机端口）上真跑 24 项：登记 → `chat --wait
  --frames` 当场等到 answer → 链与 `tools` → `fork --at-seq 0` 带新诉求出子 run 走完、父 run 置
  superseded → `resume --at-seq 0` 同一条 run 续到终态 → 两处如实回绝（不给分叉点、链上没那个
  节点），跑完自造身份零残留。
- DeepSeek 有时只叙述计划而不发工具调用，同一句提示词重跑结果不稳；b7 里靠把工具名与参数
  显式点名在提示词中才稳定。
- `generate_script` 观察过会截断标题（「换乐冒烟」→「换乐冒」），只影响文案标题字段。
