# REMOVIDE 开发文档 —— 图像驱动高质量视频链路完整闭环

版本：v1.0（2026-10-13）
适用基线：git `0a2acdb`；**工作树不干净**（实测 `git status --porcelain` 有 45 项：31 个已跟踪文件被改、若干新文件未跟踪，且尚无 commit）。这不是一句免责声明——「把盘上改动变成镜像里生效的改动，再变成一次 commit」正是 R0/§7 与 §11 收口条要干的活，本文档的前提是**基线提交**，不是**干净工作树**。
交付性质：**终版开发合同**。本文档把截至今天所有开放问题都落成编号决定（§9，**Q1–Q77**；其中 Q71–Q77 是执行期对实测偏差的追加登记，不是新增待办），按 §6 任务表 R0–R10 逐条执行完即可闭环，不需要下一轮开发、不需要再向需求方提任何问题。
两条本轮新增的体检也一并落成编号，不留形容词：①**与通用 Agent 架构骨架逐项对照**（§1.10，17 项 → 12 项已具备（多为超集）、5 项有差；差的其中 3 项归 R10＝P29/P30/P31/P34/P35，「压缩三级」那一格虽判已具备也查出两条真差 P32/P33 同样归 R10，剩下 2 项**早已在 R9 记账**＝P24/Q54 与 §1.8 第五格/Q57，不重复列为新增工作）；②**「能复用外部开源就别造轮子」这本账在 Agent 侧结清**（§3.6，14 行逐项定价，结论用 `tests/test_requirements_hygiene.py` 变成可失败断言，而不是一句自我表扬）。
本文档**没有任何「无法解决」栏**：以前写成「买不到机器判据」的三条（画面语义 / 版式美丑 / 动效意义）本轮落成 ⑲/⑳/㉑，「上下文只能人肉把控」落成 ㉓，并由元闸 ㉒ 把「判断型闸自己会不会错」量化成四个数字。§10 留下的只有三样东西——一个**有阈值的错误率**、一份**对策已排布的绕法清单**、以及一条**无法量化但被规则化的取舍**（每块常驻内容留一行理由 + 每轮 3 次人工复核，见 §10.6）——三者都写成了可验收的口径，不是待办。

---

## 0. 这份文档怎么用

阅读顺序与角色：

| 角色 | 必读 |
|---|---|
| **任何人，第一遍** | **§0.5 待解决清单总序（35 条，每条后面就跟着它的解决方法）** |
| 执行开发（人或 agent） | §0.5 → §5 契约冻结 → §6 任务表 R0–R10 → §7 部署收口 → §8 端到端验收 |
| 提示词 / 工具面维护者 | §5.4 字节账 → §5.7 上下文面契约 → §6 的 R9（T37–T42）+ R10（T43–T49）→ §10.6 取舍条款 |
| 验收/交差 | §2 判据总表 → §8 验收矩阵 → §10 残余风险 → §11 完成定义 |
| 后来接手的人 | §1 基线快照（含取证方法）→ **§1.10 骨架对照（本仓 vs 通用 Agent 架构骨架）** → §3 复用决策 → §9 决定一览 |

三条纪律贯穿全文，违反任何一条即视为该任务未完成：

- **D-红先**：任何新判据必须先在测试里**证红**（构造一个应当被拒的样本、断言它被拒），再证绿。只写阈值不写红样本，等于没写判据。
- **D-三级取证**：任何「已经生效」的结论必须同时给出三层证据——盘（工作树文件 md5）→ 镜像/容器（容器内 md5 + 镜像 CreatedAt + 文件 mtime 对齐）→ 活库/MinIO。`docker cp` 进活容器**不等于**生效（recreate 会清掉）。
- **D-未判不绿**：任何判定态为 `undetermined` 的结果，在报告里必须写「未判」，不许折算成通过，也不许因为「大部分镜头过了」而整体报绿。

---

## 0.5 待解决清单总序（35 条，按执行顺序；每条后面就是它的解决方法）

这一节是全文的**唯一入口**。每条都是「问题 → 解决方法 → 落点」三段，解决方法写到这里就能照着做，不需要再翻别的章节找答案；落点列给出细化的任务号（§6）、判据号（§2.2）、决定号（§9）。**没有任何一条只写了问题。**

### A组 · R0 诚实与契约（不做完，后面全是假绿）

| # | 待解决的事情 | 解决方法 | 落点 |
|---|---|---|---|
| P1 | **活后端不具备素材投递能力**：容器里 `code_render.py`/`remotion_client.py` 仍是 10-11 那份，`grep -c stage_asset` = 0，而工作树是 2/4 | 重烤两镜像（`docker build -t creation-app:latest .`；`docker build -t creation-remotion:latest ./remotion`），**不用 compose build**；烤完立刻按 D-三级取证比 md5（**逐件**那一趟已落成 `scripts/image_parity.py`：清单现读 Dockerfile，比 `docker exec md5sum` 与盘上，退出码 0/1/2——点名两条 md5 只是它的子集，§9-Q86）。同时把「缺腿」从运行期事故前移成启动自检 | §7 D-1/D-2、T1、T3、T5、**T21**、闸⑱ |
| P2 | **活库 `materials_origin_check` 不收 `'art'`**，因为唯一带 `'art'` 的 SQL 放在 `storage/migrations/` 目录里，而执行口只读 `migrations.sql` | 把 `20261012_asset_origin.sql` 内容并入 `storage/migrations.sql`、废弃该目录；对活库执行 `DROP CONSTRAINT` + `ADD CONSTRAINT ... ('upload','library','bgm','url','render','art')`（SQL 用 base64 传进容器）；补一条常驻断言令 `schema.sql:54` 与 `migrations.sql` 逐项相等 | T2、Q4、§5.2 |
| P3 | **SKILL.md 向模型许诺链上不存在的东西**（:378 断言 `origin` 支持 `'art'`，另有五个点名字段库里没有）——这是提示词污染 | 以**真实来源为准**逐词对账：能对上语义的改名，对不上的**从正文删除**，不许反过来改库迁就正文；把对账固化成常驻测试而不是注释 | T0、T36、§5.2 命名漂移行 |
| P4 | **五处「写了但没人接」的孤儿**：`styles.json`（0 消费者）、`migrations/`（0 读取）、`remotion/public`（不存在）、`OBJECT_PREFIXES` 不含 `assets/`、`_bootstrap_sections` 生产恒空且无 flag 可开 | 新建 `tests/test_delivery_hygiene.py` 五断言（**现状全红**）逐条转绿：删 `styles.json` 并把三档预设并入 `palettes.json` 的 `preset`；建 `public/assets/.gitkeep` 并进 Dockerfile COPY；`assets/` 前缀**保持不 GC** 且用断言钉住；bootstrap 按 Q57 **二选一**（接配置口或删函数），不许留着「看起来能配」的形状 | T13、T8-1、T4、T14、T37 |
| P5 | **§1.5 那四条测试红**是四种病各一例，不修就会在后续每轮被当成背景噪声 | 分别收口：`undetermined` 从提醒升级成**阻断档**（T28）；正文↔来源双向对账（T0）；工具总数守卫从「= 常数」改成「= 注册表实际大小且每个节点可被 `dag_contract` 列出」（T15b）；压缩阈值**以代码值 65536 为准改测试**，不为凑绿回退阈值（T15c） | T28、T0、T15b、T15c、Q25、Q26 |

### B组 · R1–R4 图像通道（第五根因：链路上没有图像）

| # | 待解决的事情 | 解决方法 | 落点 |
|---|---|---|---|
| P6 | **`image` 通道今天一旦真被使用就取不到字节**（48 张 PNG 在 `src/demos` 下，不在 `staticFile` 可达的 `public/`） | 建 `remotion/public/assets/`（进 COPY）；投递方向唯一化：Python → `POST /stage-assets` → `public/assets/<sid>/` → 镜头 `staticFile(name)`；lint 期读 `_manifest.json` 判存在性，并对「名字不存在」与「manifest 有但未投递」给**两句不同文案** | T4、T3、T6、§5.1、§4-5 |
| P7 | **「用了图但画面没图」**——引用了素材、产物上却是纯色/1×1 占位 | 闸⑬ 三张红样各一支（1×1 PNG / 全白图 / manifest 有记录但文件缺），判据 = 落盘字节 sha16 对得上 + 非 1×1 + **至少两档主色** | T8、闸⑬ |
| P8 | **策展库 132 行对链上不可见**（`materials.origin='library'` 为 0 行，两批数据之间没有投递关系） | 补投递口：库条目被会话取用时插入 `materials(origin='library', ...)` 并带来源指回，`owner_user_id` 取当前身份。这一步**不需要任何新密钥**，可直接用 xuzc 账号那 132 行验收 | T11、T45、Q18、§5.2 |
| P9 | **模型「读得到卡、读不到码」→ 被迫手搓实现**（丑的机制性根因） | 生成轮按该镜 `derived_from` 集合注入 **demo 全文**（只注引用到的那几份，不全量 158 个），`load_skill(path=)` 不截字节；再用闸⑫ 按字节比对申报与实现（阈值 0.61，由标定脚本产出） | T20、闸⑫、Q37、§5.3 |

### C组 · R5 + R8 画面观感（把「卡片+文字」这类驳回过的形态变成判据）

| # | 待解决的事情 | 解决方法 | 落点 |
|---|---|---|---|
| P10 | **「卡片+文字」镜今天判不住**：闸④ 只验「非空 + 带内有东西」，这种镜两条都满足 | ⑲-a 无模型那条腿：AST 数主体件图元（`@remotion/paths`/`shapes`/`rough`/`three`/`@dg/`）≥1，且复用闸⑧ 已有的 DOM 盒算**包围盒占比 ≥ 0.18**；再与 ⑳(a) 的文本面积占比 >0.45 **联合**判 | T30、T32、闸⑲-a/⑳(a) |
| P11 | **母题被挤**（1664×340 横屏正文带那档，母题只占 9%）会复现 | 直接拿 2026-10-11 那支产物的**实测几何当锚定负样本**做红样；母题占比阈值**沿用闸④ 第二半现值**（`fill_span` 的 `SPAN_MIN`/`TIGHT_MIN`/`FILL_TARGET`）不新造数——合同原写「闸⑧ 现值」，口径按 §9-Q88 更正（闸⑧ 里没有这个数）。**T18b 已落地**（`tests/test_motif_share.py`，横屏带子 1800×732）；Q88 同时记下「实心块 31%~44% 是两条硬闸之间的已知边界」，那一段留 T32 的 ⑳(b) 拿锚定几何整段判 | T32、T18b、§9-Q88、§8.3 |
| P12 | **代码写法不同、画面上却是同一张卡**，闸⑨ 看不见它（闸⑨ 比代码骨架且要 ≥3 镜才定罪） | ⑳(c) 判**画面**：布局盒哈希 + 主色直方图，两镜即算；红样必须同时断言「⑳(c) 拒而闸⑨ 不报」，否则算重复实现 | T32、Q51、闸⑨ |
| P13 | **动效同帧齐发 / 一帧到位**看不出「有动效」 | 闸⑭ 帧采样：每镜 ≥2 次可测的先后变化事件（几何/alpha/描边进度），相邻事件 ≥0.4 s，全片不得同刻齐发；两支红样（静帧镜 + 齐发镜） | T18、闸⑭ |
| P14 | **动效有先后但不为任何数据服务**（「有没有意义」这句形容词没法判） | 把问题替换成可测的「为哪个数据服务」：`card.motion[i].anchors` 必填（`fields.<key>` / `subtitle:<i>` / `beat:<n>`），闸前查空锚即红，闸后查**触发帧是否落在所锚字段首次可读窗口 ±k 帧内**（`k=fps//3`，改 fps 须重算） | T33、闸㉑、附录 B |
| P15 | **版式在同一片内重复** | 闸⑮ 族配额：`layout` 取值种类 ≥ `ceil(镜数/4)` 且同族 ≤2 镜；纯文字族受 ⑦ 的 1/3 约束。红样用 8 镜同族 | T15、闸⑮/⑦ |
| P16 | **镜像只有 Noto CJK，而 demo 硬写 SF Pro/Avenir → 全静默回落**（第六根因） | 采纳 `@remotion/fonts`：woff2 落 `public/fonts/`，`loadFont({url: staticFile(...), format:'woff2'})`；保留 `font-family-whitelist` 并新增「加载即断言」。官方推荐的 Google Fonts 写法**禁用**（离线镜像必炸） | T16、§3.1、Q15 |
| P17 | **判断型闸自己会错，而「模型说了算」不能当验收** | 元闸 ㉒：同帧重复 N=3 的一致率 ≥0.90、与人标注符合率 ≥0.85、`bad:*` 召回 ≥0.95、`good` 误拒 ≤0.15；任一不达 → 该闸自降 `undetermined` 并阻断，外加每轮 10% 抽检出人工复核清单。golden 集不另起标注任务：§8.1 首轮 12 支抽帧 + §8.3 那 4 支 `bad` 反例帧就是初始内容 | T31、T34、T35、闸㉒、Q46/Q47/Q48 |

### D组 · R6–R7 出图与声画

| # | 待解决的事情 | 解决方法 | 落点 |
|---|---|---|---|
| P18 | **没有任何 agent 节点/工具会出图**，唯一的生图调用挂在 `/settings/image/test` 测试口上，且 model 名硬编码 `doubao-seedream-5-0-pro` | 新增 `imagegen` 节点并注册进 `build_real_registry`（照 `asset_nodes` 的接法）；body 改成可配 `model`+`size` 的通用 OpenAI 兼容格式；验收只看「配置口存在 + 自检 `ok:true` + 一张落盘 PNG」，**密钥只验存在、绝不取值** | T24、§3.4、§1.8 |
| P19 | **出图后端没配时今天会静默出一支「卡片+文字」片** | 写死供给优先级：检索库 → 未命中 → 明确回执「图像通道降级为检索，本片 N 镜无图」并让 ⑦/⑯ **报红**。降级可见 ≠ 失败，静默出片才是失败 | T25、Q9、Q27 |
| P20 | **声画只有形容词**（BGM 短于成片、`narration` 关着却有旁白轨，都没人查） | 闸⑰ 合成之前对账：BGM 时长 ≥ 成片 × 0.98；字幕条数 × 平均显示时长落在帧账内；`narration` 关闭时不得存在任何旁白轨。字幕 token 化不自研，用 `@remotion/captions`（MIT 零依赖） | T23、闸⑰、Q14 |
| P21 | **想用官方 `@remotion/transitions` 做转场，会连带放弃逐镜缓存**（它要求相邻片段在同一 React 树内） | **不引入**树内转场；转场落**拼接层**（ffmpeg `xfade`），母版只发由 token 驱动的入场/出场。红法：断言逐镜独立渲染仍命中 `shot_cache_key`——引入树内转场即破 | T22、Q11、§4-1 |

### E组 · R9 上下文面（提示词 / skill 正文 / tools / MCP / 记忆不许堆砌）

| # | 待解决的事情 | 解决方法 | 落点 |
|---|---|---|---|
| P22 | **§5.4 那张字节账表目前没有代码支撑**（`prompt_bytes`/`字节账`/`count_tokens` 四目录全零命中，靠人跑一次性脚本） | 装配出口 `context.py:226` 返回前产出 `context_ledger`（每块 name/bytes/kind/condition_key/referenced_by + tools 面 + over_budget + orphan_sections）；取数**复用** `compress.py:31-62` 已有的计数器，不新造；落进 `hooks.py` 既有记录通道。§5.4 表改由账自动生成 | T37、§5.7 第①条 |
| P23 | **常驻段没有任何上限约束**（`compress.py:163-166` 令首条 system 永不压缩，这是**骨架要求的正确行为**，但因此 `65536` 管不到它 → 堆多少发多少，没人拦） | 闸㉓：逐块预算表（§5.4 末列），`over_budget` 非空即抛 `ContextBudgetError`、请求发不出去；**明令禁止用截断代替报红**。预算数不照抄本文档：T37 上线后拿 12 支夹具跑每块 P95，预算 = P95 × 1.15，同时写回 §5.4 与测试常量，两处不同步即红 | T38、闸㉓、Q55、附录 B |
| P24 | **`<memory>` 每次全量 dump，超限「保尾部」= 悄悄丢头部**；`query` 形参为 RAG 预留但从未使用 | 用起 `query`：本轮用户消息 + 计划步骤名做**关键词召回** top-k（k=6 起），重合为零即不带；`USAGE_NOTE` 344→≤120 字；超限行为改成出 `over_budget` 回执「本轮因预算未注入 N 条，最早一条 <时间戳>」。**只动读侧**（写回侧 `plan_retro.py` 已实现，别重复造） | T39、Q54、§5.7 第④条 |
| P25 | **`knobs` 参数教程只要白名单非空就每个规划轮全量给**（「参数教程不许常驻」这条要求的最后一条残留） | 与 `_IMPLICIT_SKILLS` 同构成条件注入：仅当本轮计划步骤命中「暴露这些旋钮的节点」时才发；判据沿用 `plan/support.py:71` 的去前缀节点名匹配，**不新增第二套判据**。双向红样各一支（该关没关 / 该开没开） | T40、§5.7 第③条 |
| P26 | **执行轮把 53–57 份工具 schema 整批发**（规划轮已有物理过滤，执行轮无过滤） | 把 `planning_registry` 泛化成 `registry_for_round(plan_steps)`：本计划用到的节点 + 通用件；`agent.py:1899-1911` 的缓存签名**必须含工具子集身份**（否则「同一签名不同工具面」假命中）。裁的是 schema 字节，**不是可发现性**：被裁节点名必须仍出现在「如何请求扩面」小节，`load_skill`/`ask_user` 永不裁 | T41、Q56、§5.7 第⑥条 |
| P27 | **两条链两份提示词真相源**：`editing_agent.py:38` 写死常量且不接 `prompt_library` | 落 `prompts/editing_round.md`，副链改调库，内联常量退化为回落副本并纳入 `test_prompt_library.py:88` **既有**一致性守卫（不新写测试骨架）。并的是文件与对账，**不是段落**：剪辑链注入集不得含 motion 闸说明（写成断言） | T42、Q58、§5.7 第⑤条 |
| P28 | **技能正文整段全量注入**（13,580 字符），且本批要新增的三节若追加进正文会自己顶穿预算 | `SKILL.md` frontmatter 增 `sections:`，正文用 `<!-- section: 名称 -->` 切块，`preload_skills(sections=)` 按本片实际通道取块。红样：把三节之一误挂进默认集 → 该块字节超预算 → ㉓ 必须报红 | T36、T42、§5.7 第②条 |

### F组 · R10 骨架对照查出的差（详见 §1.10 那张表）

| # | 待解决的事情 | 解决方法 | 落点 |
|---|---|---|---|
| P29 | **`<skills>` 标签被包了两层**：`skill.py:290` 自己拼了 `<skills>…</skills>`，`context.py:253` 又按 `source.name` 包一次 → 外层 `<skills>` 把整段（含本应同级的 `<skill_instructions>` 正文块）吞成子节点，标签还重复一遍 | 装配出口改成**单一包装规则**：`context.py:250-253` 只对**不以 `<` 开头**的段按名包一层，源侧不动（理由见 Q70：那个源返回的是多标签块）。断言两支：`<skills>` 开闭标签**各恰好 1 次**、`<skill_instructions>` 与 `<skills>` **同级**；§5.4 的 `<skills>` 清单字节按**外层去重后**计 | T43、Q70、§5.4 `<skills>` 行 |
| P30 | **MCP 客户端是手写 JSON-RPC**（`tools/mcp.py:410-477` 自己发 initialize/`tools.list`/`tools.call`），而 `requirements.txt:6` 的官方 `mcp` 包**只被服务端用了**；且 `type: sse` 在 `:41` 声明了却在 `:612-615` 一律落到 StreamableHttp——**声明了但不实现** | 优先**复用官方 SDK 客户端**（`mcp` 包自带 stdio/streamableHttp/sse 三传输与会话握手），把 `MCPClient` 换成 SDK 的薄适配层，保留本项目已有的三件东西：`register_mcp_tools` 白名单、`tools/mcp.py:583-590` 的身份覆写（模型不许自报 `user_id`/`conversation_id`）、`:544-560` 的「必须显式调用」附注。若迁移风险大于收益，退路是**删掉 `sse` 声明并对 `type=="sse"` 明确报错**——留着「配了但走的是另一条」比不支持更坏 | T44、Q62 |
| P31 | **MCP 有两个互不相认的真相**：①默认文件 `backend/mcp.json`（`run_server.py:115`）**在仓库里不存在**，`:540-552` 整段静默跳过 → 首次部署的 MCP 面是空的，且没有任何回执；②即使该文件存在，`run_server.py:540-552` 走的是**另一条腿**——直接 `connect_server` + `register_mcp_tools` 挂进本地 `mcp_clients`，**绕开 `McpManager`**，于是文件里的 server 在设置页的 `live_servers()` 里看不见、也关不掉，而 `mcp_manager.py:7-12` 那三条纪律（撞名不上线 / 连接幂等 / 失败即回滚）只约束库里那条热连通道 | **连接通道只留一条**：把 `mcp.json` 降级为**启动种子**——读出来后 upsert 进既有表 `mcp_servers`（`repositories.py:1176-1190` 的 `McpServersRepo`，带 `owner_user_id`/`enabled`），连接一律由 `McpManager` 发起；页面/DB 那份成为唯一运行期真相。配置缺失时启动日志出**一行可读回执**「MCP 静态配置不存在，本次仅 Storyline 一路」，不许静默。设置页补一个 MCP 区块（与既有的「配额」「检索后端」两区块同形同口径） | T46、Q61、Q63 |
| P32 | **压缩摘要每轮重算**：L3 产出的摘要只活在返回的 list 里，下一轮 `context.py:263-267` 又全量重载历史 → 同一前缀每轮重新总结一次（多烧一次 summarizer 调用） | 摘要**持久化**：随 checkpoint 的 `head_seq` 存一条摘要行（复用 `checkpoints`/`checkpoint_entries` 既有通道，不新建表），下一轮按 `head_seq` 命中即直接当前缀；**首条 system 永不进摘要输入**这条不变（`compress.py:163-166` 是正确行为，别顺手"优化"掉） | T47、Q64、§5.4 压缩行 |
| P33 | **每轮全量拉会话历史**（`session.py:62-64` 无 LIMIT），长会话下这段字节既进 `context_ledger` 也进 token 账，却没进过任何预算表 | 读侧改**窗口拉取**（近 N 条 + 一条持久化摘要前缀，与 P32 同一批做）；`context_ledger` 新增 `<history>` 块并给它一条预算（附录 B 的 P95×1.15 口径同样适用） | T48、Q64 |
| P34 | **`SpawnTool` 是同步 await 子 Agent**（`subagent.py:148`），与骨架写的「异步派生」不符 | **保持 await**，不改成 fire-and-forget。理由（按「断掉了哪条既有保证」标价）：改成异步就断掉「子结论作为工具回执直接进入本轮消息序列、父计划在它回来前不许收尾」这条既有保证，而骨架要的异步拆分能力**本仓已有**——`subagent_manager.py:134-160` 的常驻 WORK/IDLE 型就是异步腿。要做的只有两件事：①工具描述如实写「同步等待」；②给「子注册表里没有 spawn」补一支**测试**而不是注释（今天只有 `subagent.py:81-86` 的一句注释在守递归派生） | T49、Q65 |
| P35 | **四处手写解析器/加密件**（JWT HS256 + PBKDF2 `auth.py:34-71`、YAML 子集 `mcp.py:114`、frontmatter `skill.py:83`、CJK 计数 `compress.py:31-62`）——「能复用就别造轮子」的账要结 | **决定不换库**（每条都给价格）：换 pyjwt/`auth.py` 注释自陈「避免为这一个用途引第三方」，且现有实现只暴露 HS256，引库会把算法协商面打开；换 PyYAML 会**扩大解析面**（现只认子集 + 自实现 `${ENV}` 展开），全量 YAML 是新的注入面。取而代之的判据是：**每份手写解析器必须有一条金样回归测试**（不改行为，只锁行为），并在 §3.6 的复用底账里登记「本仓自持 + 有金样」而不是「未复用」 | T49、Q66 |

**P1–P35 之外没有别的「待解决」来源。** 骨架 17 项对照里 12 项本仓已具备（且多数是超集：钩子 5 点 → 11 点、CronJob 文件 → PG + CAS、Store 缓存 → `artifacts` 表持久化），逐项证据与差异见 §1.10；那三项差异已全部落成上面的 P29–P35。

---

## 1. 基线快照（2026-10-13 实测）

写文档前先把当前真实状态钉死。下面每一项都是实测值，不是描述。

### 1.1 盘 vs 活容器：后端是「半新」状态

| 文件 | 工作树 mtime/字节/md5 | 容器内 | 一致 |
|---|---|---|---|
| `backend/storyline_server/motion/code_spec.py` | 2026-10-13 10:45 / 95656 / `850f803b…` | 02:05Z / 同 md5 | ✅（注入，非烤入） |
| `backend/storyline_server/motion/code_render.py` | 10:45 / **59097** / `a159ed0b…` | **2026-10-11 14:31Z / 56499** / `8ce47301…` | ❌ 差 2598 B |
| `backend/storyline_server/motion/remotion_client.py` | 10:45 / **9622** / `7c552aa6…` | **2026-10-11 01:41Z / 8120** / `be3f4f82…` | ❌ 差 1502 B |
| `backend/examples/skills/motion_explainer/SKILL.md` | 10:45 / 22996 / `6ba7238b…` | 02:09Z / 同 | ✅（注入） |
| `remotion/src/FilmLayout.tsx` | 10:45 / 30679 / `46255d07…` | 02:13Z / 同 | ✅（注入） |
| `remotion/src/themes/styles.json` | 10:45 / 1355 / `60d4d8e7…` | 02:09Z / 同 | ✅（注入） |
| `remotion/lint-rules.mjs` / `server.mjs` / `package.json` | 00:37 | 10-12 13:22–14:56Z / 同 md5 | ✅（烤入） |

镜像：`creation-app:latest` = `87cc24abeb0f`，建于 2026-10-12 21:53 +08；`creation-remotion:latest` = `a36e599a809f`，建于 2026-10-12 22:57 +08；两容器均 2026-10-13 02:21Z 起。`creation-editor` 复用 `creation-app:latest`。

**结论（这条决定 §7 的顺序）**：`code_spec.py`/`SKILL.md`/`FilmLayout.tsx`/`styles.json`/`tests/test_code_spec.py` 五处容器 mtime **晚于镜像构建时间**，说明是事后 `cp` 进去的，recreate 一次就没了；而真正承重的 `code_render.py`/`remotion_client.py` **没有被 cp**，仍是 10-11 的版本。于是：

```
docker exec creation-app sh -c 'grep -c stage_asset /app/storyline_server/motion/code_render.py /app/storyline_server/motion/remotion_client.py'
→ code_render.py:0   remotion_client.py:0
工作树同查 → 2 / 4
```

**活后端不具备素材投递能力**，闸⑪（`asset_motion`）在活后端也完全不存在（`grep -c asset_motion` → 0），而活渲染器已经在回报这个字段。也就是说：镜头代码里声明 `assets` 之后，链路上没有任何一条腿把字节送到 `/stage-assets`。这是当前第一条真实阻塞。

### 1.2 活库真相

```
materials_origin_check:
CHECK ((origin = ANY (ARRAY['upload'::text,'library'::text,'bgm'::text,'url'::text,'render'::text])))
```

不含 `'art'`。`asset_library` 表**存在**（由 `schema.sql:64` 建，不是由那个迁移文件建的）。行数：

| 表 | 数量 |
|---|---|
| `materials` 合计 | 116（upload 54 / render 33 / url 25 / bgm 4 / **library 0** / **art 0**） |
| `asset_library` | **132** |
| `skill_files` | 290 行，distinct `skill` = 1（只有 `motion_explainer_skill`），SUM(bytes) = 2,895,305 |
| `skills` | 6 |
| `artifacts` / `render_jobs` / `retrieval_hits` | 139 / 16 / 673 |
| `app_secrets` distinct key_name | 6：`base_url, judge_api_key, judge_base_url, judge_model, model_api_key, model_name` —— **无任何 `image_*` / `search_*` 行** |
| 缓存表 | **不存在**（24 张 public 表里没有 shot_cache / render_cache；见 §9-Q24） |

`asset_library` 有 132 行但 `materials.origin='library'` 为 0 行——两批数据之间**还没有投递关系**（R3 要收的就是这个口）。

### 1.3 迁移为什么没落地（根因，一行）

执行口只认一个文件：`ddl.py:19 MIGRATIONS_FILE = "migrations.sql"` → `ddl.py:299 _schema_path().with_name(MIGRATIONS_FILE)` → `db.py:647 split_statements(read_migrations_sql())`。全仓对 `migrations/`（目录）**零命中**。因此 `backend/agent_framework/storage/migrations/20261012_asset_origin.sql`（唯一带 `'art'` 的 SQL）由没有任何代码读取，是一段**写而不执行**的死文。`schema.sql:54` 里确实列了 `'art'`，但它是建表期 CHECK，对已存在的表不生效。

### 1.4 前端与静态服务

`frontend/dist` 存在但只有 3 个文件（`index-*.js` 244348 / `index-*.css` 67095 / `index.html` 661），构建于 **2026-10-11 12:25 +08**；`frontend/src/App.vue` mtime 2026-10-13 01:46 → **dist 比源码旧约 2 天**。无 vite dev server；`deploy/Caddyfile` 只有一条活动路由 `:80 { reverse_proxy app:8000 }`；静态由 FastAPI 挂（`server.py:2649 StaticFiles(directory=static_dir, html=True)`，`run_server.py:832` 默认指向 `REPO_ROOT/frontend/dist`）。编辑器是同一个 bundle 里的 `frontend/src/MotionEditor.vue`，没有第二份构建产物。

### 1.5 测试基线（容器内 `PYTHONPATH=/app`，逐文件 `python <file>`，rc==0 记通过）

**TOTAL=96 PASS=92 FAIL=4**。（`python -m pytest` 仍 INTERNALERROR：`backend/tests/test_segment_ids.py:152` 有模块级 `sys.exit`，被 pytest 收集期抛出；见 T15。）

| 红文件 | 首个失败断言（原样） |
|---|---|
| `test_code_gates.py` | `✗ 未判档：提醒清单为空但通道账照样逐镜摊着（不谎报）：{'s01':'text','s02':'text','s03':'text'}` — 108/109 |
| `test_motion_channel.py` | `✗ 技能正文点名的工具/取值/字段都在真实来源里（误报：['asset_library','asset_sources','object_key','read_file','source_title','source_url']）` — 1/217 |
| `test_storyline_server.py` | `FAIL 工具总数 = 25 节点 + read_node_history + render_status + dag_contract（实际 30）` |
| `test_team_wiring.py` | `✗ 默认压缩阈值 8000（实际 65536）` — 49/50 |

这四条的红法正好是四类：①「未判」被当成提醒而不是判据；②**技能正文与真实来源漂移**（SKILL.md 承诺了库里不存在/口径不同的字段）；③注册表长过守卫；④默认值与文档不同。R 组任务按这四类各自收口。

### 1.6 闸的现有清单（这是 §2 的起点）

- Python 侧（`code_spec.py:566-568` 注册表，按序 4 个）：`check_style_quota` → `check_derived_similarity` → `check_shot_structure` → `check_ink_contrast`。另有散点判据 `check_field_value`(:318)、`check_target`(:1703)、`validate_code_spec`(:495)、`_norm_assets`(:378)；`shot_cache_key`(:597) 是缓存键不是闸。
- Node 侧（`remotion/lint-rules.mjs` 的 `dgPlugin.rules`，11 条）：`only-allowed-imports`、`must-wrap-film-layout`、`needs-shot-export`、`no-external-urls`、`must-use-motion`、`no-raw-styled-div`、`staticfile-asset-check`、`font-family-whitelist`，加 3 个 `fieldRule` 实例（`fields-declared-unused`、`fields-undeclared-read`、`field-mark-not-reading`）。全部在 `remotion/lint.mjs:34-44` 设为 `error`，外加核心 `no-restricted-globals`（7 个标识符）与 `no-restricted-syntax`（7 个选择器）。唯一消费者链：`lint.mjs:3` → `server.mjs:22/133/457`。
- **两份 import 名单当前逐项相等且顺序相同**（各 36 项，`ONLY_IN_PY=[]`、`ONLY_IN_JS=[]`）。历史上漂过两次（`@remotion/sequence` 这个包根本不存在、`@remotion/preader` 未装），所以 §5.5 把这条对账固化成常驻测试而不是注释。
- 阈值现值：`DERIVED_MIN_SIM=0.61`（`code_spec.py:80`，由 `scripts/calibrate_derived_sim.py:84` 标定）、`CHANNEL_QUOTA=((6,3),(10,4))`、`MIN_SHOTS_FOR_QUOTA=6`、`MAX_TEXT_ONLY_SHARE=1/3`、`CONCRETE_SHARE_TARGET=0.40`、跨镜同构 `0.88`、字色对比 `3.0`/`4.5`、`PICTORIAL_CHANNELS={rough,three,katex,vector,image}`（`image → ("staticFile",)`）、`CHANNEL_ORDER=("rough","three","katex","image")`。

### 1.7 demo 语料与三个「不存在」

- `remotion/src/demos`：280 个文件（`_fixtures` 6），扩展名直方 **226 `.tsx` / 48 `.png` / 3 `.json` / 2 `.md` / 1 `.jpg`**。
- **`remotion/src/demos` 下没有 `MANIFEST.json`**。唯一的清单在代码真正读取的位置：`backend/examples/skills/motion_explainer/demos/MANIFEST.json`，15721 B，**list of 158**，键 `relpath/bytes`；该目录 159 个文件 = **158 `.tsx` + 1 `.json`，0 个二进制**，合计 2,247,087 B。容器内 `len(_known_demo_relpaths()) == 158`。
- **`remotion/public` 在工作树和容器里都不存在**（0 目录 0 文件）。`publicDir` 指向 `path.join(ROOT,'public')`（`server.mjs:172`，另有 `:471` 的越界防护读口）。`remotion/out` 0 文件。没有 `remotion.config.*`。→ 这解释了「镜像里 34 张位图」那一档：位图在 `src/demos` 下（48 个 PNG），但**不在 staticFile 可达的 public 目录**，所以 `image` 通道今天一旦真被使用就取不到字节。（R4 收这个口。）
- `_legacy/manifest.json`（16890 B，`dict{sourceRevision, cards[66]}`）与上面那份 list 形态**结构不同**，别混用。

### 1.8 五处「写了但没人接」的孤儿（每条都对应一个任务）

| 孤儿 | 实测 | 任务 |
|---|---|---|
| `remotion/src/themes/styles.json`（1355 B，三档 `ink-archive`/`tech-data`/`night-concept`） | 全仓消费者 **0**（Python 合同、`server.mjs`、`lint-rules.mjs`、`FilmLayout.tsx` 四处 grep 皆空）；真正的单一来源是同目录 `palettes.json`（1621 B，`code_spec.py:1143` + `lint-rules.mjs:95` 都在读，容器内 `len(palettes())==8`）；Dockerfile 只 COPY `lint-rules.mjs/package.json/src/tokens.ts/src/themes/palettes.json`（:47,50,51），**`styles.json`、`FilmLayout.tsx`、`server.mjs`、`src/demos` 都不在 app 镜像的 `/app/remotion` 里** | T8-1 |
| `backend/agent_framework/storage/migrations/` 目录 | 零读取（§1.3） | T2 |
| 出图能力 | `secrets.py:36,37,` `IMAGE_PROVIDER_KEY/IMAGE_API_KEY/IMAGE_BASE_KEY` 已定义并在 `:150` 导出、`:213` 解析；`server.py:1997` 读、`:2278` 写、`:2299` 只有测试口。**唯一的出图调用**在 `server.py:2231` 起的 `/settings/image/test` 里，`:2321` `f"{api_base}/images/generations"`，model 名**硬编码** `doubao-seedream-5-0-pro`。**没有任何 agent 节点/工具调用生图**；图像供给只有 `asset_nodes.py:27,65` 的 `search_illustration`/`select_illustration` 读 `asset_library`。`app_secrets` 无 `image_*` 行 → 该测试口自报 `ok:false / 未配出图后端` | R6 |
| `session_gc.py:34 OBJECT_PREFIXES` = `("renders/","derived/","motion-shots/","render-windows/")` | 不含 `assets/` → 库素材对象**永不回收**（安全靠遗漏，无测试保护） | T14 |
| `context.py:204-219 _bootstrap_sections`（提示词引导块通道） | 生产**恒空**：`run_server.py:268/395` 传 `bootstrap_dir=None`，且**没有任何 CLI flag 或 env 能打开它**——形状像「可配置的提示词层」，实际谁也配不了。R9 装配面体检时才发现，此前没进过任何清单 | T37（并按 Q57 二选一：接上配置口或删掉） |

### 1.9 SKILL.md 与合同的一处口径漂移（必须先修的提示词污染）

`SKILL.md:378` 向模型断言 `materials.origin` **支持 `'art'`**——被 §1.2 的活约束直接否证。这类「正文点名了真实来源里没有的东西」正是 `test_motion_channel.py` 那条红在抓的东西。修的顺序在 R0：要么把库口径放宽到收 `art`（T2），要么把正文改成 `'library'`；本文档选前者并连带统一命名（§5.2）。

### 1.10 与外部架构骨架的逐条对照（**17 项：12 项已具备（多为超集），5 项有差**——其中 **3 项的差归 R10**（`<skills>` 嵌套 P29、MCP 的 P30/P31、spawn 语义的 P34/P35）；另外「压缩三级」那一格虽判 **已具备**，对照时查出两条真差 P32/P33（摘要每轮重算、历史全量重载），同样归 R10，另外 **2 项早已在 R9 记账**（记忆读侧召回＝P24/T39/Q54；装配四层的 bootstrap 孤儿＝§1.8 第五格、Q57/T37），**不属于 R10 新增工作**）

需求方给了一份通用 Agent 架构骨架（Session/MQ 路由、双层循环、Tool+Registry+并发、Hook、Checkpoint、SubAgent、Cron、MCP、三级压缩、记忆、SKILL、剪辑 DAG、Agent Team、装配四层），问「有没有照这个搭」。下面是逐项取证——**这张表的用途是防止实现者照骨架去重写本仓已有的东西**（那属于 §5.7 明令禁止的「把已验证的行为断掉、换零收益」）。

| 骨架条目 | 本仓实现位置 | 结论 | 差异与处置 |
|---|---|---|---|
| **Session = user_id + conversation_id → MQ 分区路由** | `session.py:27-29` `session_id=f"{user_id}:{conversation_id}"`；`mq.py:47-111` `InMemoryMessageQueue` 4 分区 + `partition_of()` 用 `zlib.crc32(key)%N`，**每分区一条 asyncio.Queue + 独立 `_partition_worker`**；`mq.py:129-197` `KafkaMessageQueue`（aiokafka，`send_and_wait(key=session_id)`）；`run_server.py:760` 默认 `--mq memory`；`server.py:1202-1223` `POST /chat` 只 publish | **已具备** | 骨架的「同 Session 串行、不同 Session 并行」在进程内分区下**真实成立**（同 key 严格 FIFO），另有 `agent.py:2264-2268` per-session `asyncio.Lock` 兜底。默认档不是 Kafka 属**部署选择**不是缺口，Kafka 代码在链上可选（`--bootstrap-servers`） |
| **OutBound → Connection Manager 按 session_id 找回连接** | `connection_manager.py:51-102` 订阅 `outbound`，`_sockets: dict[session_id, set[ws]]`；`:274-289` `route()`；`:255-272` `replay_to` 环形缓冲；`broadcast.py:44-124` Redis `PUBLISH outbound:broadcast` 跨副本，broker 不可达退本地 `:95-96`；`server.py:2341-2365` WS 挂载 | **已具备（超集）** | 骨架只有一个连接映射，本仓多了**刷新重放 + 跨副本影子预算**（`buffer_only :131-147`）。不动 |
| **外层 Main Loop + 内层有界 ReAct** | `agent.py:2257-2262` `run_forever(): while self._running: 取队列 → create_task(_process)`；`agent.py:697` `while r.iteration < limit`，上限 `AgentConfig.max_iterations=10`（`:403`），续跑起点已撞上限由 `_iteration_limit():226-242` 处理；无工具时 `_nudge_back:729` 打回 | **已具备** | 与骨架逐字对应，且多了无进展熔断（`:1598-1628` 同名连续失败 + `_MAX_CONSECUTIVE_FAILURES`） |
| **`Tool` 抽象基类（name/description/parameters/execute + `_TYPE_MAP` + read_only/concurrency_safe/exclusive）** | `tool.py:80-109`；`_TYPE_MAP:22-29`、`_resolve_type:175-183`（联合类型取首个非 null）；`read_only/exclusive/concurrency_safe:114-145` | **已具备（超集）** | 本仓另加 `reads/writes` 状态键（`:126-133`）、`requires_approval:149-156`、`owner_user_id:160`。**不重写** |
| **`ToolRegistry`（register/get/has/get_definitions/execute + 错误提示后缀）** | `tool.py:186-319`；`_HINT:32` 与骨架同句（`[Analyze the error above and try a different approach.]`），经 `ToolError.__str__:57-59` 与 `execute:298-299,312` 追加；`UnknownToolError:62` | **已具备** | — |
| **被选中工具二次判断并发资格 → batch gather** | `tool.py:327-337 conflicts()` + `340-366 plan_batches()`（不可并发者**独占一批** `:351-353`）；执行点 `agent.py:1587-1589` `for batch in plan_batches(...): await asyncio.gather(...)` | **已具备** | 骨架的「或者丢线程池，每 batch 一线程」本仓**不采**：全链 asyncio 且工具多为 IO+DB，线程池会断掉 `current_identity()` 的上下文传播 |
| **AgentHook 5 点 + CompositeHook 逐 hook 异常隔离 + finalize_content 链式改写** | `hooks.py:59-124`（骨架 5 点全在：`before_iteration:62`/`on_stream:65`/`on_stream_end(resuming=):68`/`after_execute_tools:71`/`finalize_content:120`）；`CompositeHook:127-229` 每个 async 节点各一份 `try/except logger.exception`，`finalize_content:223-229` 串接 | **已具备（11 点）** | 本仓另有 6 个骨架没有的点（`before_tool_call:97`/`after_tool_call:107`/`on_llm_usage:92`/`on_approval_required:74`/`on_subagent_spawn:114`/`on_subagent_end:117`）与配额/用量/追踪/审批/成片卡五类 hook。**两处有意为之要如实记**：`QuotaExceeded` 被**故意放行**（`:140-141`）；`finalize_content` 同步且**不隔离异常**——都不是 bug，别「顺手修好」 |
| **Checkpoint 持久化中间状态（tool call / tool result / 进度）** | `checkpoint.py:57-135`（`run_id/session_id/message/iteration/status/head_seq/scope/forked_from/plan/approval/lease`）；`:161-205` delta + `parent_seq` 成链；`:222-266` `mark_failed`/`await_approval`（存 `pending_calls`）；`:447-515` **fork 时间旅行**；`agent.py:706-712` **一致点落在调 LLM 之前**（messages 已含 tool_result → 恢复不重放工具）；`run_server.py:521-537` 启动 `claim_recoverable` + 租约 | **已具备（超集）** | 骨架只说"保存中间态"，本仓多了 delta 压缩链、fork、跨实例租约。**已销账**：failed run 不再自动重放（历史任务 #201）。会丢的只有「最后一个一致点之后的当前迭代」，已如实记录 |
| **SubAgent：派生后不可再派生，结果回传父** | `subagent.py:105-151 SpawnTool`；防递归 = `:81-86` 子注册表**不注册 spawn** + `subagent_manager.py:202-214 build_subagent_registry(parent, allow)` 白名单；子上限 `max_iterations=6`；提示词 `:36`「不要再派生子任务」 | **部分具备** | 两条差：①`:148` 是**同步 await**，不是骨架的异步派生 → **P34/Q65：保持 await**，异步腿由 `subagent_manager.py:134-160` 常驻 WORK/IDLE 承担；②防递归只有注释没有测试 → **T49** 补一支「子注册表内 `has("spawn_subagent") is False`」断言 |
| **CronTool：文件系统持久化 CronJob + 定时器装填 + 一次性/周期分档** | `tools/cron.py:39-129`（`kind("at"\|"every")`、`next_run_at_ms:55`、`delete_after_run:72-73`）；`:220-223 _next_wake_ms`、`:232-247 _loop`（`asyncio.wait_for(self._wake.wait(), timeout)`，CRUD 打断即重算）、`:249-265 start + _recover_overdue`；`:274-297 _fire_due`：`every` → 续排、`at` → `enabled=False` + `delete_after_run` 决定删或禁；`run_server.py:447-466` 到点把 `job.task` 投回 MQ；LLM 侧三工具 `:311-425` | **已具备（存储介质不同）** | 骨架落文件系统，本仓落 **PG `scheduled_jobs` + `claim_run` 比较-交换**（`repositories.py:1005-1058`）。**这是升级不是偏差**：文件档在多副本下会双触发，CAS 不会。→ §3.6 登记，不改 |
| **MCP 三传输（stdio/streamableHttp/sse）+ `tools/list`→inputSchema + 注册进 Registry + FastMCP 服务端** | `tools/mcp.py:243` Stdio、`:304` StreamableHttp、`:410-477` **手写 JSON-RPC**（`tools/list:448-451`、`tools/call:453-474`）、`:521` inputSchema 原样进 `parameters`、`:527-528 readOnlyHint→read_only`、`:618-647 register_mcp_tools`（白名单 `:599-602`）、`mcp_manager.py:57-101` 热插拔；服务端 `storyline_server/server.py:21 from mcp.server.fastmcp import FastMCP` + `:366-387 streamable_http_app`；`requirements.txt:6 mcp>=1.28,<2` | **部分具备** | 四条差，全部编号：①`sse` 在 `:41` 声明、`:612-615` 一律落 StreamableHttp＝**声明不实现** → P30/T44；②客户端手写而官方 SDK 只用在服务端 → P30/T44；③配置是 JSON 或手写 YAML 子集（`:103-204`）**不是 TOML**，且默认文件 `backend/mcp.json`（`run_server.py:115`）**不存在** → P31/T46；④**连接腿有两条**：`run_server.py:540-552` 对文件那条直接 `connect_server`+`register_mcp_tools`，绕开 `McpManager`（库里那条才走 `mcp_manager.py:57/89/102` 的热连热断与 `:7-12` 三条纪律）→ P31/T46 |
| **上下文压缩三级（折叠工具结果 → 淘汰旧消息 → 全量摘要），首条 system 与最新输入不动** | `compress.py:3-6` 三级俱在：L1 `_fold_tool_info:119-138`（`keep_recent_tool_results=2`、预览 60 字、`FOLD_MARKER` 幂等）、L2 `_split_to_fit:142-155`（最后一条无条件保）、L3 `:66-93` + 组装 `:159-185`；`:163-166` 首条 system 单独摘出、**永不进摘要输入**；`:188-258` 孤儿 `tool_calls` 修复；阈值 `DEFAULT_MAX_CONTEXT_TOKENS=65536`（`run_server.py:117,389`） | **已具备** | 与骨架完全对应。`:163-166` 是**正确行为**（§0.5 P23 措辞已按此订正：它本身不是缺陷，缺陷是「因此常驻段不受任何上限约束」）。唯一真差：**摘要只活在返回 list 里、每轮重算**（`:180-184`；`context.py:263-267` + `session.py:62-64` 全量重载）→ P32/T47 |
| **记忆系统（骨架：User.md / Tool.md 两档 + 注入 + RAG 召回列为下一步）** | `memory.py:22-25 CATEGORIES={user,tool}`（PG `memories` `schema.sql:255-261` + CHECK；`:3-4` 自陈取代 User.md/Tool.md）；CAS 追加 `repositories.py:1085-1123`；渲染 `memory.py:123-141`（`per_category_chars=2000`/`total_chars=4000`，`:116-121` 保尾部）；写回 `plan_retro.py:69/137/145/172-198` → `agent.py:1978,2144-2150` | **部分具备** | 分档**已具备且更强**（PG + CAS + 写回闭环，骨架只有文件）。缺的只有**读侧召回**：`query` 形参 `:123` 预留未用，全 `AF` 目录 `embedding/recall/similarity/ILIKE` 零命中 → 正是 R9-T39/P24，且**不上向量库**（Q54） |
| **SKILL 系统（frontmatter `name/description/always` + `<skills>` 清单带 `available/location/requires` + 渐进加载）** | `skill.py:83-98` 解析（扁平 `key: value`），消费字段 `name:189/description:203/always:137/requires:132/display:144`；`:65-80 manifest_lines`（`available:68/location:75/requires:78`）；`:290` 拼 `<skills>`、`:294-298` always 附正文、`:340-373` 按需与附件；`context.py:250-253` 注入；6 份 SKILL.md | **部分具备** | 结构与骨架**同名同形**（本仓这套约定本来就照 Claude-Code 风格建，不是照这份文档补的）。三条差：①`<skills>` **双标签嵌套** → P29/T43；②无 `sections` 分段、正文整段注入 → P28/T36；③`requires` 语义是**环境探测**（CLI/ENV 在不在，`:38-49,270-275`），**不是 skill→skill 依赖** → **Q67：明确不做 skill 间依赖**（6 份正文 `always:`/`requires:` 均 0 命中，加了没消费者） |
| **剪辑 DAG：`require_node` + 拦截器递归回填 + BaseNode 五段生命周期 + Store ≠ Agent Memory** | `orchestration.py:213-302 BaseNode`（`__call__:288 → load_inputs_from_client:304 → _parse_input:313 → process → pack_outputs_to_client:322 → store.put:300`），契约属性 `required_nodes:230/require_prior_kind:231/require_explicit_call:232/reducer:235/ephemeral:238`；`:450-466 Interceptor._ensure_deps` **真递归回填**（非只做拓扑序），成环即报 `依赖成环: a -> b -> a`（`:451-452`），另有静态 Kahn `detect_cycle:343-361`；`:57-101 Store`（键=节点名，作用域 `(session_id, artifact_id)`）+ `:112-137 ArtifactStore` 以 PG `artifacts` 为真相、`open()` 重水化；`:11` docstring 明写 Store 与 Agent Memory 分离；注册表 `core_nodes.py:3981-4001 build_real_registry` | **已具备（超集）** | 骨架的 Store 是内存档，本仓**持久化**（重启/换实例可续）。跨边界也是复用而非自研：Agent 侧走 **MCP streamableHttp**（`run_server.py:568-583`，5 次重试），身份从服务端 `X-Storyline-Session-Id` 取（`server.py:37,158`）、`user_id/conversation_id` 由 `mcp.py:583-590` **覆写**，模型无法自报；`require_explicit_call` 依赖**拒绝自动回填**并给指令式报错（`:458-462`） |
| **Agent Team：Task Manager（blockedBy/blocks + BFS）/ Message Center（{name}.jsonl 已读即删）/ SubAgent Manager（WORK-IDLE-SHUTDOWN）** | `task_manager.py:62-201`（字段名就是 `blockedBy`/`blocks`：`:104,153,167-171`，`:173-192` deque BFS，`ready():159-163`，`claim:112`/`complete:126` 原子，`topo_order:37-55`）；`message_center.py:25-80`（表 `inbox_messages`，`:5` 自陈取代 `.jsonl`，`send:38-55` 带 `dedup_key`，`read_inbox:57-70`，`peek:72-75`）；`subagent_manager.py:35-37` 三态 + `:134-182` WORK/IDLE 轮询（默认 idle 120 s / poll 1 s）+ `:96 reset_expired` 租约；装配 `team_tools.py:250-287` → `run_server.py:435` | **已具备（两处语义不同）** | ①介质 JSON/JSONL 文件 → **PG 表**（同 Cron 的理由：多副本）；②`read_inbox` 是**标记消费（`consumed_at`）而非删除**，`:5` 明写为保留重放/审计。**两者都保留**（Q68）——把「已读即删」搬回来会断掉刷新重放这条既有保证。工具名是 `list_tasks/claim_task/complete_task`，不是骨架示例的 TaskGet 系 |
| **装配四层（System Prompt / Bootstrap 文件 / Memory / 当前输入 / Runtime / Tool Schema → Current Context）** | 单一出口 `context.py:226 build`，`parts` 以 `\n\n` 拼接（`:241-261`）：`system_prompt → _bootstrap_sections:204-219 → <runtime>:245-248 → 各 context_source(<skills>/<memory>) → <attachments> → extra_sections` | **部分具备** | 形状完全对应，但 **bootstrap 那一层生产恒空**：`run_server.py:393-398` 传的参数在 `:268` 默认 `None`，argparse（`:761,845-860`）**没有 `--bootstrap-dir`**，`server.py` 零命中 `bootstrap`，另三处构造（`team_tools.py:170`/`subagent.py:96`/`editing_agent.py:117`）都不传 → `run_server.py:300-301` 自陈「当年就死在写了没人接上」。§1.8 第五格 + T37/Q57。活着的等价通道：`prompts/` 库（`prompts.py:37,111,167`，可热重载 `context.py:187-196`）与 memory/skill 两个 source |

**这张表怎么读（三条，写给实现者）**：

1. **12 项已具备 = 一行代码都不用动。** 谁以「照骨架统一」为名重写 `Tool`/`ToolRegistry`/hooks/compress/checkpoint 的层级结构，就是 §5.7 禁止的那类改动：收益为零，代价是断掉已有测试守着的行为（`test_skill.py:125-128`、`test_planning_prompt_golden.py:83-110`、`test_code_gates.py` 都在守）。
2. **5 项有差，全部已编号，没有第四条待办的来源**：归 R10 的三项是 P29（`<skills>` 嵌套）、P30/P31（MCP 的传输、配置口与**两条连接腿**）、P34/P35（spawn 语义如实化、手写件金样）＋ P32/P33（压缩与历史两条真差，也在 R10）；归 R9 的两项是 P24（记忆读侧召回，Q54）与 §1.8 的 bootstrap 孤儿（Q57）。**注意**：P32/P33 之所以记在 R10 而不是 R9，是因为它们由骨架的「压缩三级 + 会话状态」这一格对照查出来，判据与预算仍复用 ㉓，不另立闸。细化和红法见 §6 的 **R10**。
3. **两处「与骨架不同但更强」不许退回**：Cron/Team 用 PG + CAS 而不是文件（退回＝多副本双触发）；Store 持久化而不是内存（退回＝重启丢产物）。这两条在 §3.5 登记为「自研有理由」，不是欠账。

---

## 2. 「高质量」的定义与判据总表

### 2.1 把「高质量」拆成四个可验收面

| 面 | 说的是什么 | 现在的判据 |
|---|---|---|
| **A 画面** | 每镜有可辨认的画面主体（不是「卡片+文字」），版式在片内有变化，多处先后动效 | **有**：④ 非空+带内填充、**⑲ 主体在场（⑲-a 无模型 / ⑲-b 盲测）**、⑦⑮ 版式族配额、⑳ 退化形态锚定、⑭㉑ 动效密度与锚定 |
| **B 图** | 位图/矢量真的进画面，且动起来了 | **R4 之后有**：⑪ + ⑬ |
| **C 声画** | BGM 时长对得上、字幕带不压主体、音画节拍可对齐 | **有**：⑩ 字色、⑰ 音画数据对账、㉑ 的节拍窗锚定 |
| **D 稳** | 同一次投放可复现、改一镜不重烧全片、缓存键含资产身份 | **有**：`shot_cache_key` + `assets_hash`，但从未在活链上验过（R1 收） |

### 2.2 判据总表（28 道：本表 ①–㉓ 共 23 道，另 ㉔–㉘ 五道记在增补卷）

> **㉔–㉘ 五道为什么不并进这张表**：它们是 R11/R12 落的（㉔ 供给面 / ㉕ 徽章构图 / ㉖ 回看环 / ㉗ 分区揭示 / ㉘ `reveal` 申报与画布对账），属于**增补卷**的地界（`REMOVIDE_ADDENDUM_R11_PARTS_AND_REVIEW.md` §12–§13）。并进 ①–㉓ 这张表就要连带改「谁读它 / 落点任务 / 红法在哪」三列与 §2.2 这一段的行文，而增补卷已经有一份更细的账。**这不等于它们可以少记**：每道的落点任务（T53–T62）、红样与现状（离线绿 / 真机未跑）都在增补卷 §14.3 那张表里，**未跑的照旧记未跑**（D-未判不绿）。本表的 23 道仍是 23 道，谁也不许把「28 道」当成 ①–㉘ 全在这张表里。**

编号沿用既有说法（①–⑪ 已在链上，⑫ 已实现），新增 ⑬–⑱；**⑲–㉑ 是本轮新落地的三道**——它们专门吃掉以前被写成「永远没有机器判据」的那三条；㉒ 是一条**元闸**，管「判断型闸自己会不会错」；㉓ 是另一条**元闸**，管的不是画面而是**上下文面**（系统提示词 / skills 正文 / 工具 schema / MCP 描述 / 记忆），它把「不许堆砌、要动态灵活」这句形容词变成能报红的账面。`位置` 列指判据发生在哪一侧；`阶段` 列很关键——**㉓ 判在拼 prompt 之前，比所有「烧像素之前」的闸还早一档**；再往后是 ②⑦⑨⑩⑫⑮⑯⑱⑲-a 和 ㉑ 的前半段判在烧像素之前，这是省钱的地方；④⑧⑪⑬⑭⑳ 与 ㉑ 的后半段判在产物上。

| # | 名称 | 位置 | 阶段 | 状态 | 判据一句话 |
|---|---|---|---|---|---|
| ① | import/idiom lint | Node `lint.mjs` | 烧像素前 | 已有 | 36 项名单外的一律拒；禁 `Math.random/Date.now/new Date` 等 7 全局 + 7 语法选择器 |
| ② | 帧预算注入 | Python | 生成前 | 已有 | 每镜拿到自己的 `fps/durationInFrames/安全区`，不许自算 |
| ③ | 帧账 | Python | 烧之后 | 已有 | 逐镜申报帧数与实际渲染帧数对齐 |
| ④ | 画面非空 + 带内填充 | Node（像素回读） | 烧之后 | 已有 | 非背景像素占比 + 母版带内有内容 |
| ⑤ | token drift | Node | 烧之后 | 已有 | 字阶/配色只能来自 `@dg/tokens`，不许镜内自定 |
| ⑥ | 字段对账 | Node 3×`fieldRule` | 烧之前 | 已有 | 声明未用 / 未声明而读 / `card` 标记了却没读 |
| ⑦ | 风格配额 | Python | 烧之前 | 已有 | `CHANNEL_QUOTA=((6,3),(10,4))`、纯文字占比 ≤ 1/3、具象占比目标 0.40 |
| ⑧ | 版面回读 | Node | 烧之后 | 已有 | 从产物回读几何，与申报版面比对 |
| ⑨ | 跨镜同构 | Python | 烧之前 | 已有 | 同片结构相似度 ≥ **0.88** |
| ⑩ | 字色对比 | Python | 烧之前 | 已有 | ≥ 3.0（大字）/ ≥ 4.5（正文） |
| ⑪ | 资产动效三态 | Node `server.mjs:346-389` | 烧之后 | **代码有，活链无** | `static` 打回 / `animated` 通过 / `undetermined` 报未判 |
| ⑫ | 派生相似度 | Python `check_derived_similarity` | 烧之前 | 已有 | 与 `derived_from` 指向的 demo ≥ **0.61**（按字节） |
| ⑬ | **资产存在且非纯色** | Node（读落盘字节） | 烧之后 | **新增，R4** | `staticFile(name)` 的字节必须真在 `public/assets/<sid>/` 下、sha16 对得上、非 1×1、且**至少两档主色** |
| ⑭ | **动效事件密度** | Node（帧采样） | 烧之后 | **新增，R5** | 每镜在时长内出现 ≥ 2 次可测的「先后变化事件」（几何/alpha/描边进度），且相邻事件间隔 ≥ 0.4 s；全片不得出现「所有事件同时刻齐发」 |
| ⑮ | **版式族配额** | Python | 烧之前 | **新增，R5** | 一条片内 `layout` 取值种类 ≥ `ceil(镜头数/4)`，且不允许多于 2 个镜头共用同一族；纯文字族受 ⑦ 的 1/3 约束 |
| ⑯ | **画面主体具名** | Python（对账 `card`） | 烧之前 | **新增，R5** | 每镜 `card` 必须给出 `subject`（可被认出的东西的名词短语，非空、非抽象名词白名单外）+ `subject_channel`（`rough/three/katex/vector/image` 之一），⑦ 的通道账按它摊 |
| ⑰ | **音画数据对账** | Python | 合成之前 | **新增，R7** | BGM 素材时长 ≥ 成片时长 × 0.98；字幕条数 × 平均显示时长落在帧账内；`narration` 关闭时不得存在任何旁白轨 |
| ⑱ | **投递腿存在性** | Python（启动自检） | 全链之前 | **新增，R0** | 渲染客户端必须暴露 `stage_asset`；缺失则**在生成开始前**就报「投递腿缺失」，而不是让镜头代码在打包期撞死 |
| ⑲ | **画面主体在场**（两条腿） | ⑲-a Python AST+几何 / ⑲-b Node 抽帧→judge | ⑲-a 烧之前，⑲-b 烧之后 | **新增，R8** | ⑲-a：每镜必须真画出可归属的**主体件**（`@remotion/paths`/`shapes`/`rough`/`three`/`@dg/` 主体件生成的图元 ≥ 1，且其包围盒 ≥ 母版带面积 **0.18**）——不依赖任何模型。⑲-b：抽该镜 2 个事件峰值帧，**先让判断模型盲描述画面里是什么（不给申报 subject）**，再与该镜 `card.subject` 双向比对：盲描述命中申报词族=过；盲描述答「纯文字/无主体」而 ⑲-a 过了=打回并记「主体是空壳」 |
| ⑳ | **退化形态锚定** | Node 几何 + Python 判据 | 烧之后 | **新增，R8**（(c) 已落地） | 不判「好不好看」，只判**是不是这三种已被人眼驳回过的形态**：(a) 卡片+文字（文本面积占比 > 0.45 且主体件包围盒 < 0.18）——判据只落在 ⑲-a 一处；(b) 母题被挤（1664×340 横屏正文带那档的复现，锚 2026-10-11 的实测几何）——归闸④ 的 `fill_span` 与 T18b；(c) **画面同构**——两镜的**粗网格布局指纹**（文字/图元各一份）+ 主色集合几乎一致，而 `check_shot_structure`（闸⑨）看不见它，因为闸⑨ 比的是**代码骨架**且要 ≥3 镜才定罪。⑳(c) 与闸⑨ 的分工就是「画面一样」vs「代码一样」，不许合并、不许重复实现 |
| ㉑ | **动效锚定**（为哪个数据服务） | Python 契约 + Node 帧采样 | 烧之前 + 烧之后 | **已落地（T33）** | 镜级 `motion: [{kind, anchors}]`，`anchors` 必填（`fields.<本镜声明过的字段>` / `subtitle:<本镜第几个词>` / `beat:<第几个节拍>`，一首一尾都从 1 起、写法是白名单、一个事件最多 3 个）；闸前查「有事件无锚 / 锚认不出 / 锚到不存在的字段」即红；闸后把 ⑭ 从**像素**上数出来的触发帧与所锚数据的「首次可读帧」按顺序一一对齐，**每一个锚**都要落在 ±k 内（`k=fps//3`）。今天算得出窗口的只有 `subtitle:` 一条，`fields.` 与 `beat:` 落**未判**（未判不是通过），绕法与残差见 §9-Q98。「有没有意义」在这条被替换成「为哪个数据服务」——后者可测 |
| ㉒ | **判据自洽元闸** | Python + 离线脚本 | 上线前 + 每轮验收 | **新增，R8** | ⑲-b/⑳/㉑ 都是判断型闸，自身会错，所以给它们一条可测元性质：同一帧重复 N 次的**一致率 ≥ 0.90**；对 golden 集与人标注的**符合率 ≥ 0.85**，且对负样本（已知坏形态）的召回 **≥ 0.95**（宁可错杀）。不达 → 该闸降为 `undetermined` **不许折算成通过**，并按 ㉒ 的抽检率 10% 出人工复核清单 |
| ㉓ | **上下文配额闸（「不堆砌」的机器判据）** | Python（装配出口） | 每次拼 prompt 之前 | **新增，R9** | 每一个**常驻**注入块（系统提示词、`<memory>`、`<skills>` 清单、`knobs` 段、技能正文、工具 schema 整批）必须带一张**账面**：字节数 + 本请求内**引用它的对象名**（哪道闸、哪个工具、哪段任务）。三条硬规则：(a) 块级预算表（§5.7）超线 → **装配失败并报红**，绝不允许「静默截断尾部」；(b) 常驻块引用数为 0 → 记 `orphan_section`，连续两轮即视为该删；(c) 条件块（`prompts.py:83-108` 的 `{?key}`、`plan/vocab.py:27-38` 的 `_IMPLICIT_SKILLS`）必须**双向红样**——「该关时没关」和「该开时没开」各一支。今天**没有任何代码能测这件事**（`prompt_bytes`/`prompt_size`/`字节账` 在四个目录全零命中；而 `compress.py:163-166` 令首条 system 永不进压缩——这是骨架要求的正确行为，但它的推论是**常驻段没有任何上限约束**），所以 §5.4 那张表的「现值」目前是人工一次性量出来的，㉓ 就是把这句话变成代码 |

> ⑱ 是这份文档里最便宜的一条判据，也是当前唯一能让「假绿」变「真红」的一条：今天活后端缺这条腿，任何带 `assets` 的镜头都会走到 §5.1 的兜底分支或被静默忽略。

> **⑲–㉓ 共同的设计原则（照抄即可，别自由发挥）**：
> 1. **不要求证明「好」，只要求可审计地拒掉「已知坏」。** 每一条判据都锚在一个**历史上真被驳回过的具体形态**上（卡片+文字、母题占 9%、只换色相的复制卡、无锚动效），而不是锚在形容词上。这样判据才有反例可写，也就才有「先证红」这一说。
> 2. **判断型闸必须留两栏原文回执**：模型/几何「看到了什么」 vs 合同「申报了什么」。只有一句 `pass/fail` 的回执等于没判据——它不可复核。
> 3. **不许吊在一条腿上。** ⑲ 有 ⑲-a（无模型，永远生效）+ ⑲-b（要 judge 通道）；㉒ 再给这两条本身打分。任何一条腿断了，另一条仍能红，且断腿本身要出可读回执。
> 4. **㉓ 把同一条思路搬到上下文面。** 它同样不证明「提示词写得好」，只可审计地拒掉四种**历史上真实发生过的堆积形态**：`<memory>` 每次全量 dump（`memory.py:123-141`，`query` 形参为 RAG 预留但从未使用）、工具 schema 每迭代整批发（`agent.py:716-718`，53–57 个）、参数教程常驻（`vocab.py:210-252` 的 `knobs` 段只要白名单非空就每规划轮全量给）、以及「改了正文但没人能量出改了多少字节」（四目录 `字节账` 零命中）。判据形式=账面 + 超预算即红，**永远不许用截断代替报红**（截断会把「堆多了」变成「尾部悄悄没了」，那是更难查的一种坏）。

### 2.3 覆盖矩阵（每一面用什么判，以及那个判据**会怎么错**）

| 需求面 | 覆盖它的判据 | 判据的已知错误方式（由 ㉒ 量化，不是「没有判据」） |
|---|---|---|
| 画面可辨认 | ④ + ⑯ + **⑲-a（无模型：主体件图元 ≥1 且包围盒 ≥0.18）** + **⑲-b（盲描述 vs 申报 subject）** + ⑬ | ⑲-a 会放过「画了个东西但不像申报的那个」；⑲-b 会受 judge 模型自身识别力限制。两者的漏判率由 ㉒ 的符合率 + 10% 抽检给出**数字**，不再是形容词 |
| 版式不重复 / 不丑 | ⑦ ⑮ ⑧ + **⑳（三种已知坏形态锚定）** | ⑳ 只覆盖**已标注过的**坏形态；没见过的坏形态要等它被标进 golden 集才判得住。这是一条**会长大的判据**，不是空判据（T34 给采集协议） |
| 动效密度 / 有没有意义 | ⑭ + ⑪ + **㉑（每个事件必须锚在一个字段/字幕窗/节拍窗上）** | ㉑ 判得住「为哪个数据服务」，判不住「这个数据值不值得动」。后半句永久归人眼，但只以 10% 抽检的**成本**存在，不再是验收的唯一手段 |
| 图像进画面 | ⑬ ⑪ ① + ⑲-a | — |
| 声画 | ⑰ ⑩ ㉑（节拍窗锚） | — |
| 稳定可复现 | ③ ⑫ ⑱ + `shot_cache_key` | — |
| **上下文面不堆砌**（提示词 / skills 正文 / 工具 schema / MCP 描述 / 记忆） | **㉓（账面 + 块级预算 + 孤儿段记账 + 条件块双向红样）** | ㉓ 能量出「哪块涨了多少字节」「哪块本请求无人引用」，**量不出「这段内容值不值得占常驻」**——后者永远是人的取舍，且预算表本身是人写的。这一条不进 ㉒（㉒ 只管画面形态判断），因此它是 §10.2 之外**唯一没有元闸兜底**的错误率，处理方式见 §10.6：把它的取舍写成一条明码的「删块需附理由行」规则，而不是假装可测 |

---

## 3. 复用决策表 —— 官方有什么、拿什么、不拿什么

原则：**合同层（SKILL.md 的契约字段 + 23 道闸）不外包给任何外部说明书；写法层与零件层尽量用官方，减少自研。** 下面每一条都是拍板结果，不是待议项。

**关于「判断型闸」的复用红线**：⑲-b 与 ㉒ 需要「看图说话」的能力，但**不引入新模型、不新增密钥、不自建推理服务**——直接复用已配好的判断模型三件套（`secrets.py:22-24`：`JUDGE_BASE_KEY="judge_base_url"` / `JUDGE_MODEL_KEY="judge_model"` / `JUDGE_API_KEY_NAME="judge_api_key"`，活库 `app_secrets` 里这三行今天**已存在**）。三件套只验存在与是否 vision-capable（T31 的探针），值永不读取。

### 3.1 官方 Remotion 代码渲染包（版本一律 4.0.534，与现有 `package.json` 对齐）

| 包 | 许可 | 服务端可渲 | 离线 | 决定 | 理由（用「断掉了哪条既有保证」标价） |
|---|---|---|---|---|---|
| `@remotion/fonts` | MIT | ✅ | ✅ | **采纳（R5）** | 治第六根因：镜像只有 Noto CJK，demo 硬写 SF Pro/Avenir 等**全静默回落**。官方写法 `loadFont({url: staticFile(...), format:'woff2'})` 正是我们要的，自研只会写出一个更弱的版本 |
| `@remotion/captions` | MIT（**零依赖**） | ✅ | ✅ | **采纳（R7）** | `createTikTokStyleCaptions()` 做逐词分页/高亮，`parseSrt()` 导入。我们已有 3 档字幕模式，缺的是 token 切分这件轮子 |
| `@remotion/paths` `@remotion/shapes` | MIT | ✅ | ✅ | 已在用 | SVG 描画动效的数据源（⑭ 的一类事件） |
| `@remotion/layout-utils` | MIT（零依赖） | 仅浏览器页内 | ✅ | 已在用 | `measureText/fitText/fillTextBox`；母版 `fitText` 已落，R5 把正文换行改用 `fillTextBox` |
| `@remotion/rough-notation` | MIT（依赖 `roughjs`+`paths`，**我们都有**） | ✅ | ✅ | **采纳（R5）** | 手绘标注族（下划线/划圈/高光）——直接抬 A 面观感，零新依赖 |
| `@remotion/noise` | MIT（依赖 `simplex-noise`） | ✅ | ✅ | **采纳（R5）** | `noise2D/3D/4D` **种子确定性**，与「禁 `Math.random`」同向；用来替掉一部分 `_textures` PNG，减 staticFile 体积 |
| `@remotion/media-utils` | MIT | 仅浏览器页内 | ✅ | **有条件采纳（R7）** | `visualizeAudio/Waveform` 给 ⑰ 提供音频侧数据。⚠️ 要 `AudioContext`，只能在渲染页里调，不能在纯 Node 预步调 |
| `@remotion/animation-utils` | MIT | ✅ | ✅ | 可采纳（不强制） | 只有 `getSpringFrame()`；核心 `remotion` 已有 `spring/Easing.spring`，价值有限 |
| `@remotion/transitions` | Remotion License | ✅ | ✅ | **不采纳** | 架构不符：`<TransitionSeries>` 要求相邻片段在**同一 React 树**内，而我们逐镜独立渲 → 拼接。硬套等于放弃逐镜缓存（D 面的既有保证没了）。转场改在拼接层做（R7-T22） |
| `@remotion/effects` | Remotion License | ✅（需 WebGL2） | ✅ | **采纳，但按子路径逐件**（`@remotion/effects/paper`、`@remotion/effects/halftone`；R5-T16b 已过） | 先验夹具 `remotion/tests/effects-probe.mjs` 三条判据全过（2026-10-13）：`paper()` 与 `halftone()` 在 swangle 下不崩、画面里没有参数外的颜色、挂错宿主是逐字节相同的静默空操作；判据自己另有 D-红先自检（人工染品红 → 品红探测器红；不挂 effects → 「点真画出来了」红）。**不放行包名 `@remotion/effects`**：4.0.534 的桶文件只重导 11 件，`paper`/`halftone` **不在其中**，写包名拿到的是 `undefined` 且**打包期不报错**（§9-Q89）。只允许作用于 `<Img>/<Solid>/<HtmlInCanvas>`，普通 `<AbsoluteFill>/<Sequence>` **不接受** `effects` |
| `@remotion/media`（`<Video>/<Audio>`） | Remotion License | ✅ | ✅ | **不采纳** | 音轨不在镜头渲染期混合（原声混剪 + BGM 在合成层做）；引入只为多一个 `mediabunny` 到 2.29 GB 镜像里 |
| `@remotion/google-fonts` | Remotion License | 名义上 | ❌ **需联网** | **禁用** | 渲染容器无外网；且 `lint-rules.mjs` 的 `ALLOWED_IMPORTS` 里没有它 → 闸① 直接拒。**这是「整片换成官方 skill」会死的第一处** |
| `@remotion/player` `@remotion/studio` `@remotion/preload` `@remotion/web-renderer` | 混合 | ❌ 客户端/开发期 | — | **不采纳** | `preload` 文档自己写着「渲染不需要 preload」；Studio/player 是交互式预览，与 headless 链无关 |
| `@remotion/sfx` | MIT | — | ❌ 返回远端 URL | **不采纳（仅采约定）** | 它文档化的「峰值归一到 −3 dB」这条约定抄进 R7，素材本身不许进画面 |
| `@remotion/whisper-webgpu` / `install-whisper-cpp` / `openai-whisper` | MIT / SEE LICENSE | 预处理步 | ❌/需 API | **不采纳** | 前者要兼容 GPU（文档明写 Linux arm64 的 ONNX Runtime 不可用）；我们的字幕来自既有 ASR 链 |
| `@remotion/light-leaks` | Remotion License | ✅（WebGL2） | ✅ | **不采纳** | 官方文档已把它指到 `@remotion/effects`，即已被取代 |
| `@remotion/cloudrun` | — | — | — | **不采纳** | 官方自标 alpha 且不再积极开发 |
| `@remotion/gif` `@remotion/three` | SEE LICENSE / MIT | ✅ | ✅ | `three` 在用；`gif` **不采纳**（⑬ 只认静态位图身份，GIF 的帧由浏览器解码，sha 稳定但逐帧内容不可判） |

新增进 import 白名单的集合（T17 同步两侧，顺序一致，由 `code_spec.py` 第 ⑤ 节常驻测试逐项比）：
`@remotion/fonts`、`@remotion/captions`、`@remotion/rough-notation`、`@remotion/noise`、`@remotion/animation-utils`；**`@remotion/effects/paper` 与 `@remotion/effects/halftone`**（T16b 已验过，按子路径逐件进名单——桶名不进，理由见 §9-Q89）；`@remotion/media-utils` **未过，不进名单**。

### 3.2 官方 Agent Skills：当语料，不当合同

一手来源核实：`npx skills add remotion-dev/skills`；该仓 id 1137388347，`license: null`，`package.json` 自称 `@remotion/skills` 且 `repository` 指向 `remotion-dev/remotion/tree/main/packages/skills`。424 个 git 路径，12 个 skill：`best-practices(路由器)、create、markup、studio、render、maps、captions、saas、interactivity、docs、upgrade、multimedia`。

- **许可（关键，先定再用）**：`remotion-dev/skills` 根目录**没有 LICENSE 文件**；`packages/skills/LICENSE`、`LICENSE.md` 均 404。同仓 `packages/agent-plugin/LICENSE` 是 MIT、`packages/core/LICENSE.md` 是 Remotion License——可见「逐包/逐目录选许可」是刻意为之。因此 skills 受 **Remotion License** 管辖，其禁止条款含「不得为销售/出租/再许可目的复制或修改 Remotion 代码」。→ **决定 D-抄**：**抄写法，不抄文件**。任何官方 skill 里的 `.tsx` 源码（尤其它明确要求「原样拷进项目」的 `basic-captions.tsx`）**不得进本仓**；需要该能力时自己实现一份，用官方文档做行为参照。这条不是保守姿态：把第三方非 MIT 文本原样入库，和 `npm install` 一个包，是两种完全不同的暴露面。
- **为什么不能整片替换我们的 SKILL.md**：官方 `best-practices` 本身是纯路由器，不含任何生成规则；`markup` 教的写法里大量带外链与联网（实测原句：`Use staticFile() for files in public/ or pass a remote URL directly`、`<Video src="https://remotion.media/video.mp4">`、`Google Fonts is the recommended way to load fonts`、`sfx.md` 整页 34 个 `https://remotion.media/*.wav`、「For more sound effects, search the internet」、多处 `open the Remotion Studio`）。`render` skill 全是 CLI（`npx remotion render/still`）；`multimedia` 是浏览器 Mediabunny；`captions` 转写走 GPU Whisper 并要求原样拷文件。我们的闸① `no-external-urls` + `staticfile-asset-check` + `font-family-whitelist` 会逐条打死这些写法。替换的结果不是「变好」，是**每一镜第一轮必红**。
- **可采的写法（只此 6 条，来自 `markup`）**：
  1. `useCurrentFrame()` + `interpolate()` 承载所有动效；CSS `transition/animation` 渲染不正确 —— 与我们 `must-use-motion` 同向。
  2. `Easing.bezier(0.16, 1, 0.3, 1)` / `Easing.spring({damping: 200})` 作为默认缓动曲线 → 进 `@dg/motion` 作为共享件，而不是让每镜自选（受 ⑤ 约束）。
  3. `interpolate(..., output:'perceptual-scale')`、`posterize: 3`。
  4. 优先 `scale/translate/rotate` 独立属性，而不是 `transform` 字符串。
  5. `interpolate()` 留在 `style` 内联（便于闸⑥ 读 `card` 标记）。
  6. 秒→帧用 `useVideoConfig().fps` 换算，不许写死数字（受 ② 约束）。
- **不可采**：一切 `npx remotion studio` / `<Player>` / SaaS 架构 / `--no-open` 浏览器验证 / `npx remotion add`（会写 registry）/ GPU Whisper / 任何远端 URL。
- **注入方式（遵守提示词分层纪律）**：上面 6 条压缩成约 400 字的「写法块」，**只在视频链执行轮**随 SKILL.md 一起注入；文本/剪辑链不带。字节账见 §5.4。

### 3.3 图像/抠图/矢量三件套（许可短名单，已定）

| 用途 | 选型 | 许可 | 实测代价 | 决定 |
|---|---|---|---|---|
| 深度/显著性 | **DA3-Small ONNX** | Apache-2.0 | 20 MB，CPU 实测 1.4 s | **采纳**（R6-T24） |
| 抠图 | `rembg` | MIT | ⚠️ 新默认模型 `bria-rmbg` **要避开**，锁旧模型 | 采纳但锁模型 |
| 位图转矢量 | `vtracer` | MIT | — | 采纳（`vector` 通道已有） |
| 3D Ken Burns | — | 许可 + CUDA 双杀 | — | **禁用** |
| Depth Pro | — | 仅学术 | — | **禁用** |
| FLUX dev/klein | — | 非商用 | — | **禁用** |

### 3.4 出图 API（R6 的边界，不写成待议）

各家单价与两个坑是已核实事实：**出图 API 没有「透明背景」开关**（要透明必须走 §3.3 抠图）；**DeepSeek / Kimi 没有出图端点**（模型主对话用它们，配图必须另配后端）。`server.py:2321` 现在硬编码 `doubao-seedream-5-0-pro`，R6-T24 要改成可配 model 名 + 通用 OpenAI 兼容 body，并把 size 做成参数。**密钥值不在本文档任何清单里出现，也不需要出现**：配置口是页面（`app_secrets` 的 `image_*` 三键），验收只看「`/settings/image/test` 返回 `ok:true` + 一张落盘 PNG」。

### 3.5 许可与账号身份的边界（事实 + 决定，不构成法律意见）

Remotion 不是 OSI 意义上的开源，用的是自订 Remotion License。现行免费资格**只按人头**，文本里没有营收门槛：个人、**员工数 ≤ 3 的营利组织**、非营利组织、以及「评估阶段」。`@remotion/*` 各子包许可**并不统一**：`layout-utils/captions/paths/shapes/three/noise/fonts/rough-notation/animation-utils/rounded-text-box/media-utils` 为 MIT；`remotion/player/bundler/renderer/cli/transitions/effects/media/light-leaks/google-fonts/gif/lottie/web-renderer/lambda/vercel/media-parser/webcodecs` 为 Remotion License。付费两档：**Automators $0.01/render，按 1000 次进位（$10），最低消费 $100/月**（我们这种服务端渲染/prompt-to-video 属这一档）；**Creators $25/席/月，无最低**（席 = 写 Remotion 代码或用 coding agent 生成它的人）。企业同价但最低 $500/月。免费档**没有功能差异**。从 5.0 起遥测强制（`@remotion/licensing`）。

**决定 D-许可**：本文档**不以「许可未定」为由推迟任何 R1–R6 任务**（这是既有硬约束）。同时把一条可测规则写死：一旦贡献者人数 > 3，必须切 Automators 档并持 `licenseKey`，`remotion/package.json` 所在镜像必须能读到该 key 的存在（只验存在，不打印值）。这条规则的可执行落点在 T13-5。

### 3.6 Agent 侧复用底账（skill / tool / MCP / 框架件）——「别造轮子」这本账在 Agent 侧的一半场

§3.1–§3.3 结的是渲染侧的账，这一节结的是 `backend/` 侧的账。**这张表不是自述，由 T49 的 `tests/test_requirements_hygiene.py` 守着**：逐行断言「声明了的直接依赖确实被 import」「adopted 件确实在代码里被调用」「本仓自持件确实有金样测试」。

| 能力 | 外部可复用件 | 本仓现状（实测） | 决定 | 理由（按「断掉了哪条既有保证」标价） |
|---|---|---|---|---|
| MCP **服务端** | 官方 `mcp` SDK 的 FastMCP | **已在用**：`storyline_server/server.py:21,71,366-387`；`requirements.txt:6 mcp>=1.28,<2` | **复用（保持）** | — |
| MCP **客户端**（三传输 + 握手 + 协议协商） | 同一个 `mcp` 包（自带 stdio/streamableHttp/sse） | **手写**：`tools/mcp.py:410-477` 自拼 JSON-RPC，`agent_framework/**` 里 `from mcp import` **零命中**；`sse` 声明不实现（`:612-615`） | **改为复用（T44）**，退路见 T44 | 手写件的代价今天已经付过一次：`sse` 配了却静默走另一条＝**声明与行为不符**。迁移的硬风险只有身份覆写（`mcp.py:583-590`）被绕过，那是越权口子——所以 T44 的红法第②支专门断言它 |
| 定时任务调度 | APScheduler / celery beat / croniter | **手写**：`tools/cron.py:232-247` asyncio 循环 + PG `scheduled_jobs` + `claim_run` CAS（`repositories.py:1058`）+ lease | **不替换** | 换库不会带来任何现有保证的加强，只会新增一个部署依赖与一套**第二真相的**作业存储；CAS+lease 已经解决多副本双触发。**登记为「自研有理由」**，不是欠账 |
| Agent 循环 / 工具注册 / 并发分批 | langchain / langgraph / openai-agents / pydantic-ai / autogen | **全部自持**：`agent.py:697/1587-1589`、`tool.py:186-366`（§1.10 逐项） | **不引入** | 引框架的代价是**同时存在两套循环真相**：本仓的循环里挂着缓存签名（`agent.py:1899-1911`）、审批（`requires_approval`）、计划门对账（`plan/reconcile.py:41,93`）、额度闸（`QuotaHook:541`）四条既有保证，框架层各有一套且**语义不同**。收益是少写几百行，代价是这四条全部要重新证明 |
| LLM 客户端 | 官方 `openai` SDK | **已在用**：`llm_openai.py:234 AsyncOpenAI` | **复用（保持）** | 模型主对话走 DeepSeek/Kimi 的 OpenAI 兼容端点，不额外引 provider 框架 |
| 会话历史摘要 | langchain 的 summarization chain | **手写**：`compress.py:66-93`（三级见 §1.10） | **不替换，但修一处低效（T47）** | 真问题不是"没复用"，是**摘要每轮重算**（`compress.py:180-184` + `context.py:263-267`）。这是烧钱，不是优雅度 |
| Token 计数 | `tiktoken` | **手写**：`compress.py:31-62` CJK 感知启发式 | **不换** | 换 tiktoken 会让 `65536` 这条**跨模型**预算变成按模型分词器各算一套，现有断言（`test_team_wiring.py` 的阈值用例）全部要按模型分支；而计数器只用于"超没超"的判定，不参与缓存键 |
| JWT / 口令散列 | `pyjwt` / `passlib[bcrypt]` | **手写**：`auth.py:34-51` HS256、`:59-71` PBKDF2（文件内注释自陈「避免为这一个用途引第三方」） | **不换，但补金样（T49）** | 换 pyjwt 会把**算法协商面**打开（现实现只认 HS256，`aud`/`iss` 校验形状与本项目一致）；换 bcrypt 要重做历史口令兼容。风险 > 收益，改用"锁行为"的金样兜住 |
| MCP 配置解析 | `PyYAML` / `toml` | **手写**：JSON + YAML 子集 `mcp.py:103-204`（`${ENV}` 自实现 `:96-100`）；TOML 只在 Storyline 侧（`video_editing.py:21`、`settings.py:11`） | **不换（Q61）** | 全量 YAML 是**新的解析面**（锚点、多文档、任意对象构造），当前子集故意不支持这些。**保持"配不了危险东西"这条既有性质** |
| SKILL.md 约定 | Claude Code / Anthropic Agent Skills 的公开约定（frontmatter `name/description/always` + 渐进加载） | **已照该约定建**：`skill.py:83-98,65-80`（§1.10 SKILL 行逐项同名同形） | **复用（保持）** | 这是"约定复用"而不是"代码复用"——不引入第三方 skill 运行库，避免把别人家的 `requires` 语义（skill→skill 依赖）也搬进来（Q67） |
| 官方 Remotion Agent Skills 文本 | `remotion-dev/skills` 12 个 skill | **当语料，不当合同**（§3.2） | **只抄写法，禁拷文件** | 该仓无 LICENSE → 受 Remotion License 管辖，其条款禁止为商业目的复制/修改；原样入库与 `npm install` 是两种暴露面（D-抄） |
| 素材抓取 | `yt-dlp` | **已在用**：`requirements.txt:34 yt-dlp>=2026.8.19` | **复用（保持）** | 限制只剩两条且是事实边界：`YTDLP_COOKIES_FILE`、YouTube 缺 JS runtime（§10.4-2） |
| 视频/音频处理 | `moviepy` + 系统 ffmpeg | **已在用**：`requirements-storyline.txt`（moviepy>=2.0、faster-whisper>=1.0、edge-tts>=6.1）；ffprobe 走 subprocess（`storage/media.py:87,118`） | **复用（保持）** | — |
| 对象存储 / DB / Redis | `minio` / `asyncpg`+SQLAlchemy / `redis` | **已在用**：`requirements.txt:17-20,28` | **复用（保持）** | T47 复用 `checkpoints` 表、T46 复用 per-user 配置表——**复用也适用于自己的表**，别为新需求先建新表 |

**读这张表的一句话**：本仓「造轮子」的地方一共 **4 处**（MCP 客户端、调度循环、摘要与计数、auth 与解析器）。前一处本轮改成复用（T44）；后三处经逐条定价后**保留自研并补金样**（T47/T49），理由是"换库会打开新的协商/解析面、且对现有保证零加强"。**这个结论是可反驳的**：如果实现过程中发现 `mcp` SDK 的客户端能原样保住那三件（白名单 / 身份覆写 / 显式调用附注），那 cron 与 auth 两处也应一并重新评估——但必须**先写出一支断言证明既有保证没断**，再换库，不许反过来。

---

## 4. 总体架构不变量（改动不得触碰）

1. **逐镜独立渲染 + 拼接合成**。缓存身份 `shot_cache_key` 含 `assets_hash`（由 `f"{sha16}/{name}"` 串起来取 sha256 前 16 位，`code_spec.py:577-596`），**故意不含** `master_hash` 的资产部分——母版改动不应使所有素材作废。
2. **母版 `<FilmLayout>` 独占排版权**（字幕带、安全区、字阶、配色）。闸 `must-wrap-film-layout` 的原文理由就是「各镜自己排版必然逐镜漂」。
3. **单一来源**：配色/质感只认 `src/themes/palettes.json`（8 档，容器实测），**不认 `styles.json`**。
4. **素材纪律**：出图/抠图/深度一律在**创作期 Python 侧**完成，渲染容器只接收已落盘字节。渲染期不联网、不生成。
5. **投递方向唯一**：Python → `POST /stage-assets`（`server.mjs:464`，响应 `{ok, relpath, sha16, bytes}`）→ `public/assets/<sid>/`（`publicDir`，遍历防护 `:471`）→ 镜头代码 `staticFile(name)`。
6. **两份 import 名单逐项相等**，且都有常驻测试兜着（T17）。
7. **BGM 只认附件 `material_id`**；`narration` 默认关闭、不生成旁白。任何任务不得改成「自动选曲」或「预填 bgm.ref」。
8. **配额上限按身份热读**（页面压过 env）——既有行为，不得回退。

---

## 5. 契约冻结（字段级，实现照抄即可）

### 5.1 素材 `assets` 字段

```
assets: [ { name, sha16, ext } ]        # 三键必填，sha16 = sha256 的前 16 个十六进制字符
```

- `0a2acdb` 之前合同要求 `name/sha16/ext`，一度漂成 `{relpath, sha256, bytes}`（SKILL.md:200-213 的旧正文）。**冻结为三键版**，正文与 schema 与 `_norm_assets`(:378) 三者一致，由 §6 的 drift 测试断言。
- 后端投递（`code_render.py:656-679`）：`client.get("stage_asset")` 不存在时**现在**抛 `MediaError("声明了 N 个素材但渲染客户端没有 stage_asset 口")`（:658）。这条错误信息是好的，但它是**运行时**才响——所以新增 ⑱ 把它前移到生成前自检。
- 渲染侧 `staticfile-asset-check`（`lint-rules.mjs:614`）必须能在 lint 时读到 manifest；manifest 由 `code_render.py` 在投递完成后写，文件名/字段冻结为：

```
public/assets/<sid>/_manifest.json → { "sid": str, "items": [ {"name","sha16","bytes","ext","motion":"animated|static|undetermined"} ] }
```

- 闸⑪ 三态（`server.mjs:346-389`）比对的是**同一素材在两个采样帧上的像素差**；`undetermined` 时 `resp.asset_motion.status = "undetermined"` 并附 `reason`；Python 侧记录键 `asset_motion`（`code_render.py:103,797`）。**报告里 `undetermined` 一律写「未判」。**

### 5.2 库/表口径

| 项 | 冻结值 |
|---|---|
| `materials.origin` 取值 | `('upload','library','bgm','url','render','art')` —— `'art'` 为**出图产物**专用档 |
| `asset_library` | 全局策展库（132 行已存在），**无 `owner_user_id`**，因为 `materials.owner_user_id NOT NULL REFERENCES users(id)` 装不下全局资产——这是当初另立一表的唯一理由，不得合并回 `materials` |
| 投递关系 | `asset_library` 条目被某会话取用时，**插入** `materials(origin='library', ...)` 并带来源指回；`library` 档今天 0 行，R3-T11 收这个口 |
| sha16 | 全链统一「已有 sha256 的前 16 字符」，**不新增哈希函数** |
| 命名漂移 | SKILL.md 里的 `asset_sources / source_title / source_url / read_file / object_key` 五个名字，必须在 R0-T0 与真实来源逐一比对：能对上语义的改名，对不上的从正文删除。**不许反过来改库来迁就正文** |

### 5.3 `card` / `derived_from`（防手搓的正面机制）

```
derived_from: str        # 必填，必须是 demos/MANIFEST.json 里某个 relpath（白名单校验 _known_demo_relpaths()，code_spec.py:465-476）
card: {
  subject: str,          # 新增，⑯：能被认出的东西的名词短语；抽象词在拒绝名单里
  subject_terms: [str],  # 新增，⑲-b 用：主体的词族（含同义词/上下位词），盲描述须命中其中之一
  subject_channel: enum, # 新增，⑯：rough|three|katex|vector|image
  layout: str,           # 已有，⑮ 按它做族配额，⑳(c) 按它判「只换气相」
  motion: [ {            # 已有，⑭ 按它数事件
      kind: str,
      anchors: [ str ],  # 新增，㉑ 必填：`fields.<key>` | `subtitle:<i>` | `beat:<n>`；空数组＝闸前红
  } ],
  fields: { ... }        # 已有，⑥ 逐字段对账，㉑ 用它算「首次可读窗口」
}
```

⑲-a **不新增字段**：它从镜头代码的 AST 数主体件图元（`@remotion/paths`/`shapes`/`rough`/`three`/`@dg/` 主体件），再从闸⑧ 已有的 DOM 盒回读拿包围盒面积。**同一份几何被 ④⑧⑳⑲-a 四个闸复用，不新造通道。**

`code_spec.py:1460-1478` 是这三段在工具 schema 里的落点。闸⑫ 用 `DERIVED_MIN_SIM=0.61` 比对镜头代码与它声称的 demo（**按字节算，就按字节断言**）。

**「不能靠 LLM 手搓实现」的落点**：653 KB 卡与 158 个 demo 都在库里，`load_skill(name, path=)` **不截字节**；模型「读得到卡、读不到码」是被机制逼出丑的根因。R4/R5 把 `derived_from` 指向的 demo 全文随卡一并注入，于是「手搓」失去唯一的技术理由，而闸⑫ 给出机器判据。lint 只是下限，不是这条要求的满足。

### 5.4 提示词分层字节账（每次改动后重测并更新此表）

**这张表现在是人工量出来的，而且缺一个上限约束。** 2026-10-13 实测：`prompt_bytes` / `prompt_size` / `字节账` / `count_tokens` 在 `backend/`、`tests/`、`remotion/`、`frontend/` 四个目录**全零命中**——也就是说「每次改动后重测并更新此表」这句话**目前没有代码支撑**，靠人跑一次性脚本。同时 `compress.py:163-166` 把首条 system 单独摘出、**永不送进摘要输入**——**这条本身是通用 Agent 骨架要求的正确行为，不是缺陷**（缺陷是它的**推论**：常驻段因此完全不受 `DEFAULT_MAX_CONTEXT_TOKENS=65536`（`run_server.py:117,389`）约束，堆多少发多少，没人拦）。R9-T37 把前者变成「一台自动账」，T38 把后者变成「一条会报红的预算闸（㉓）」；**两件都不许用「干脆让 system 也进压缩」来糊**（那是 §1.10 明令不许动的正确行为）。

装配出口：`context.py:226 ContextBuilder.build`，`parts` 以 `\n\n` 拼接（`:241-261`）。下表「常发」列 = 今天的事实，「预算」列 = **目标**（R9 要把这一层瘦到哪；㉓ 实断的那条线在 §5.4.1，两者不是一回事，2026-10-13 T37 落地时分开）。

| 层 | 内容 / 装配点 | 现值 | 常发？ | 预算（㉓） | 规则 |
|---|---|---|---|---|---|
| 系统提示词 | `prompts/system_prompt.md` 2,833 B；磁盘缺失时回落 `context.py:40-85` 的 `DEFAULT_SYSTEM_PROMPT` 2,828 B | 2.8 KB | **无条件常发** | ≤ 3.0 KB | 与内联副本的一致性由 `test_prompt_library.py:88` 守着；**不许再往里塞参数教程** |
| `<runtime>` 当前时间 | `context.py:108-109,245-248` | <100 B | 无条件 | 不限量 | 无害，保留 |
| bootstrap *.md | `context.py:204-219 _bootstrap_sections` | **0**（`run_server.py:268/395` 传 `bootstrap_dir=None` 且**无 CLI flag/env 可打开**） | 生产恒空 | — | 这条是死通道：要么接上配置口，要么删掉，不许留「看起来能配」的形状 |
| `<skills>` 清单 | `skill.py:286-299`（**实测缺陷**：`:290` 自拼 `<skills>…</skills>`，`context.py:253` 又按 `source.name` 包一次 → 实际是 `<skills><skills>…</skills></skills>`，字节虚高，T43 修） | 6 份清单，**实测 3,156 B**（2026-10-13，含内层重复标签；去重后 3,118 B，所以「省字节」不是 T43 的目标——它是**形状**缺陷） | 每轮无条件（无技能才返 None） | ≤ 1.5 KB（**按外层去重后计**） | 只发清单，正文走按需；`always:` 今天 6 份 frontmatter **均未设置 → 常驻正文 0**，这是当前唯一真正生效的「不堆砌」机制。**目标今天没达标**：3,118 B 要去掉一半只能靠**描述瘦身**（6 份 description 合计 1,807 B），这件事**没有任务号**，挂在 §5.4.1 的欠账表里 |
| `<memory>` | **T39 已改读侧**：`memory.py` 的 `MemoryContextSource` 按关键词（`query` = 本轮用户消息 + 计划步骤名，由 `ContextBuilder.build(recall_terms=)` 拼好）召回 top-6，零命中则退回**每类最近几条**并写明「改注入最近」；一条都装不下时留回执行「未注入 N 条 + 最早一条起于哪个分类（含该分类 `updated_at`）」；上限改成**字节制**（`total_bytes=1400` / `per_category_bytes=700`，与账面同一把尺）；`USAGE_NOTE` 344 字 → **105 B** | 注入上界 ≈ 1,800 B（结构上限折算，最坏情况全 CJK） | **按相关度召回 + 每类最近兜底**（不再无条件全量 dump，也不再「保尾部」静默截断） | ≤ 2.0 KB（达成：上界 1.8 KB） | **已还**：`query` 形参真正用起来了；「保尾部」这个静默截断形状被换成回执行——少带可以，但必须说出来（§9-Q102 记了两处如实偏差：逐条时间戳不存在、零命中为何要兜底） |
| `<attachments>` | `context.py:128-158,255-257` | 条件（空返 None） | 条件 | — | 已达标，不动 |
| 规划轮段 | `plan/prompt.py:260-309 planning_section` + `prompts/planning_round.md` 4,134 B；条件由 `{?key}` 块守卫表达（`prompts.py:83-108`：`has_pending_plan`/`must_include`/`prior_cards`/`feedback_quote`/`has_pending`/`session_history_rows`） | 真机（2026-10-13 live，真白名单 28 节点）：冷启动 **2,275 B**、口播链 4,384 B、视频链 8,982 B、全命中 13,022 B（旧行写的 4.1 KB 是照**桩门** 3 节点量出来的，§9-Q104） | **条件**（已达标） | ≤ 5.0 KB（视频链/全命中今天超，欠账见表） | 守卫要有双向红样（T40 已落：7 个守卫 × 双向）；段内 6～9.9 KB 全是 `knobs` 那一段，见下一行 |
| `knobs` 段 | `plan/knobs.py`（T40 新建的判据层）→ `prompt.py` 的 `{?knobs}` 守卫块 | 真机：非命中轮 **0 B**；单节点命中 277～3,113 B（按节点，`plan_motion`/`render_motion_code` 最重）；视频链 6,121 B；全命中 9,870 B | **条件（T40 已改）**：只发本轮**计划上下文命中**的节点，一条没命中就整块不发（连「本轮没有开关」那行都不发） | ≤ 1.2 KB（**今天只在轻链上达标**） | 判据与语料边界见 §9-Q103；**参数教程还没瘦身**，只改了「什么时候发」（§5.4.1 欠账表挂着，无任务号） |
| 技能正文（视频链） | `plan/skills.py preload_skills` ← `agent.py execute_plan`；开关 `vocab.py _IMPLICIT_SKILLS`，判据=计划 steps 出现 `plan_motion`/`render_motion_code`/`patch_motion_code` 三节点名（去 `storyline_` 前缀，`knobs.bare_node`；`patch_video` **T41 起挂** `default_editing_workflow_skill`，理由是描述截断要有载体）；**取哪几块**由 `plan/skills.py sections_for_plan` 判（T42，见 §5.7 第②条） | `SKILL.md` 全文 **41,333 B**（2026-10-13 T36/T42/T57/T62 实测，含开头 frontmatter 752 B：默认集 **24,223 B** + 十一个可选块：`film-knobs` 2,777 / `patch-motion` 365 / `image-channel` 2,944 / `asset-motion` 874 / `asset-parts` 2,260 / `scale-safe` 1,622 / `whiteboard-imagegen` 1,256 / `subject-naming` 1,263 / `motion-anchors` 1,460 / `text-only-quota` 819 / `sound-design` 933，块尺寸含块间的 `\n\n` 连接符）。**一支最常见的代码链**（`plan_motion`+`render_motion_code`，有旁白、没动整片开关）实发 **28,698 B**（默认集 + 三节 T36 + `sound-design`；注入时另有 `<skill_instructions>` 标签 73 B）；**十一块全给上 = 40,796 B**，与断值 41,007 之间**余量只剩 211 B**——三块新块（T57 的 `asset-parts`/`scale-safe`、T62 的 `whiteboard-imagegen`）的通道条件与「先砍别抬」那条规矩见 `context.py` 的 `SECTION_BUDGET_BYTES` 注释（Q55） | **按链 + 按块**（✅ 两条都达标） | ≤ 16 KB（**未达标，欠账**） | T42 落地后「整段全量」这一半没了；**16 KB 目标今天仍未达**（实测默认集 24,223 B）。为什么不再往下切、以及还差的那 12 KB 由什么构成，见 §5.4.1 欠账表与 §9-Q106 ③——这一段里每一块都真有人读（写代码要的合同、要过的闸、要选卡的方法），再切就是砍合同不砍脂肪 |
| demo 语料 | `demos/` 159 文件 2,247,087 B（容器内 `skill_files` 实测 290 行 / 2,895,305 B）；`LoadSkillTool skill.py:302-373` 按 `path=` 读附件，**不截字节** | 按需 | 按需 | 单支 ≤ 12 KB | **只注入本片实际引用到的 demo 全文**，按 `derived_from` 集合取，不全量 |
| 工具 schema（整批） | `tool.py:164-173 to_schema` + `:226-228 get_definitions`，每迭代整批发：`agent.py:716-718`。本地 23 个（`tools/file.py:296-299`、`tools/web.py:940-945`、`fetch_media`、`tools/runs.py:173`、`run_server.py:336/341/366/371/466`、`team_tools.py:250-261`）+ Storyline MCP ≈30 ≈ **53–57 个** | 真机探针（2026-10-13，接真 Storyline 契约 + 真注册表装配）：整批 **54,140 B / 37 件**；按一支只含 `plan_motion`+`render_motion_code` 的计划裁后 **14,561 B（降 73%）**。旧记的「执行轮约 32 KB」是 2026-10-11 的读数，已过期 | 规划轮**有物理过滤**（`plan/vocab.py:163-189 planning_registry` 剔 `_MUTATING_TOOLS` 与所有 `contract is not None` 的执行节点）；**执行轮自 T41 起也有**（`plan/round.py registry_for_round`：计划命中的剪辑节点 + 全部通用件，被裁的节点名进「如何请求扩面」小节） | ≤ 40 KB（**T41 起由 `tools` 断值真判，且只判「裁过的那一档」**——未裁的轮次是全量能力面，见 §9-Q105） | 最长五份 description：`nodes/motion_code_nodes.py` 2,004 B、`plan/tools.py SubmitPlanTool` 1,502 B、`nodes/motion_plan.py` 1,392 B、`nodes/core_nodes.py` 1,163 B、`ask_user.py` 798 B。**注意 `catalog.py:116-276 ToolCatalog` 是出口/界面替换器，不进 prompt**，别把它当成工具面瘦身 |
| MCP 工具描述 | `tools/mcp.py`（685 行：`MCPServerConfig:48`、`MCPTool:502`、`register_mcp_tools:618`）+ `mcp_manager.py:30,102-122 connect_enabled`；已接入 `storyline`（`run_server.py:561-575`，streamableHttp，前缀 `storyline_`），描述随 `tools/list` 进**每次请求的 function 列表**；`tools/mcp.py:544-560` 另附一行「必须显式调用」 | 计入上一行；**每份描述自 T41 起截到 ≤600 B**（`tools/mcp.py compose_description`，账面上每份最大工具的 `desc_bytes` 可核） | 全量常发（剪辑节点描述由 Storyline 侧写，最长四份合计 6.1 KB，其中每一句在随身技能正文里都有更完整的一份 → 描述是**另一次抄写**） | 计入上一行；截掉的正文去哪了写在截断处（指向 `load_skill`），**移出即须有闸读到**：`plan/round.py unbacked_truncations` 问「这段溢出真有载体吗」 | 静态 `backend/mcp.json`（`run_server.py:115`）**文件不存在**，`:540-556` 整段跳过；OpenChatCut 在 `backend/`（除 `.smoke`）、`frontend/`、`remotion/`、`deploy/`、`docker-compose.yml` **搜不到**，残留仅两个 `.smoke/*.sql` 与一份历史设计文档 |
| 剪辑副链 | `editing_agent.py` 的 `EDITING_SYSTEM_PROMPT`（内联回落副本）→ **T42 起真相源是磁盘**：`prompts/editing_round.md`，经 `build_prompt_library` 读出并由 `test_prompt_library.py` 的对账清单守 | 1,331 B（`prompts/editing_round.md`，2026-10-13 实测） | 无条件（副链的系统提示，不是可选块） | ≤ 3.0 KB（✅） | 与主链**共用同一个 `PromptLibrary`**（`build_editing_agent(prompt_library=…)`），「改一句文案要动两处」这条到此为止（见 §5.7 第⑤条） |
| 官方写法块 | §3.2 的 6 条，落在 `SKILL.md` 的「官方写法（六条，只此六条）」一节 | **631 B / 421 字**（2026-10-13 实测，首版 762 B 已压） | 仅视频链（R5-T16）——它长在 `SKILL.md` 正文里，而正文由 `plan/skills.py` 按链预注入，所以「只在视频链」是结构保证的，不是靠人记得 | ≤ 500 B（**与「约 400 字」自相矛盾，见 §9-Q76**） | 新增，不进常驻 |
| **会话历史 `<history>`** | `session.py:62-64`（T48 前**无 LIMIT，全量拉**）→ `context.py:263-267` 每轮重载；历史消息进的是 `messages` 而非 `parts`，**所以它从来没出现在这张表里** | 每轮现算（随会话长度线性涨，到窗口即封顶） | 每轮无条件 | **不设预算**（T48 定；理由见 §5.4.1 的 `history` 行——它该被压缩管，不该被 ㉓ 判死） | **本批补的两行之一**：`context_ledger.sections` 今天**没有** `<history>` 这个块名（T48 第①支红样）。改窗口拉取 + 摘要前缀后它才既可见又可控 |
| 压缩层（三级） | `compress.py:119-138` L1 折叠工具结果（`keep_recent_tool_results=2`）/ `:142-155` L2 淘汰旧消息 / `:66-93` L3 全量摘要；触发 `estimate > 65536`（`:159-161`）；`:163-166` 首条 system **永不进摘要输入** | 每次触发约 1 次模型调用 | 条件 | 不限量（但**调用次数**限量） | **本批补的两行之二**：三级俱在且行为与骨架一致，唯一的问题是**摘要不持久化 → 同一前缀每轮重算一次**（`:180-184`）。T47 修这条；**`:163-166` 不许动**（正确行为） |

> 表末两行是本轮**骨架对照（§1.10）**新加进来的——此前这张表只覆盖「拼进 system 的那一段」，而骨架要求的装配面还包括 `messages` 侧的历史与压缩。这两行的红法与落点在 §6 的 **R10（T47/T48）**。

### 5.4.1 ㉓ 的断值表（可执行副本：`context.py` 的 `SECTION_BUDGET_BYTES`）

**「目标」与「断值」是两件事**，这是 T37 落地时定下的口径：目标说「R9 要把这块瘦到哪」，断值说「今天超过它就**不发出请求**」。分开的理由是实测——表里三块**今天就已经超目标**（`<skills>` 3,118 B vs 1,536 B、技能正文 41,333 B vs 16,384 B、`<memory>` 上限折算 13,417 B vs 2,048 B——最后这条 T39 已还清），拿目标当断值等于给每一条真机 run 判死刑。

断值的来源只有两种：**固定形状的块用「实测现值 × 1.15」**（口径同附录 B 的 P95 × 1.15，只是 §8.1 十二支夹具还没跑，先用现值当 P95 的代理）；**内容由上限决定的块用「结构上限的字节折算」**（拦的是「有人把上限删了」）。

| 块 | 断值 | 来源 | 目标 | 谁读它 |
|---|---|---|---|---|
| `system_prompt` | 3,257 B | 实测 2,832（`prompts/system_prompt.md`）× 1.15 | 3,072 B | `test_prompt_library.py:88` 的对账 |
| `skills` | 3,630 B | 实测 3,156 × 1.15 | 1,536 B | 同上 |
| `skill_instructions` | **41,007 B**（T42 换口径，**T57/T62 之后刻意不重算**） | 「能拼出来的最大那一份」实测 **35,658 B**（`motion_explainer` 默认集 + 八个块全给上）× 1.15。旧值 35,864 = 31,186（那时是**整段**注入）× 1.15，今天只剩 500 B 余量，再写一节说明就会把真机 run 判死（§9-Q106 ③）。**这不是放水**：口径一个字没改，只是「现值」从「整份正文」换成「装配真能拼出的最大一份」。**T57 加两块**（`asset-parts`/`scale-safe`，最大装配回算 36,914 B）、**T62 再加一块**（`whiteboard-imagegen`，最大装配实测 **40,796 B**）之后，按同一口径本应写 46,915——**没写**，断值仍是 41,007：每加一节就抬一次天花板，抬两次这一行就不再是一把尺（Q55 / addendum Q126）。代价是 T62 起余量只剩 **211 B**，下一节必须先砍（`context.py` 该行注释里记着这笔账，连 T57 那次手算装配 39,540 的笔误一起纠正了） | 16,384 B | T42 分段后按本片计划取块（`plan/skills.py sections_for_plan`） |
| `memory` | **2,048 B**（T39 后） | 结构上限的字节折算：召回后注入量由自己的上限卡死（`total_bytes=1400` + `per_category_bytes=700`），最坏情况（全 CJK、两段标题、未注入回执带 ISO 时间戳、`USAGE_NOTE` ≤120 B）≈ 1,800 B；断值取 2,048 与目标同值，**严于目标就等于把目标当闸**（§9-Q100 ①） | 2,048 B | T39 已还（不再是欠账） |
| `planning_round` | **14,976 B**（T40 就地修正） | 真白名单全命中 **13,022 B** × 1.15（2026-10-13 在 `creation-app` 里接真 Storyline 实测；旧值 8,192 = 金样 4,834 × 2，而那份金样是**桩门 3 节点**，真机上连最常见的视频链 8,982 B 都被它判死 → §9-Q104） | 5,120 B | 段内有用户可控文本（`feedback_quote`/`pending_asked`），紧值要等子块各自设限 |
| `knobs` | **不设断值** | 现值已标定（真机：非命中轮 0 B / 全开 9,870 B），但它住在 `planning_round` 那条线里，单列断值等于把同一段判两遍 | 1,228 B | §5.7 第③条 |
| `tools` | **40,960 B（T41 起）** | 取 §5.4 的目标数，与别块相反：这一栏的目标就是「执行轮工具面 ≤40 KB」这条验收口径本身，今天必须过（真机探针：裁后 14,561 B / 裁前 54,140 B）。**只判裁过的那一档**（`planning_registry` / `registry_for_round`）：未裁的轮次发的是全量能力面，53 KB 上下是它的正常尺寸，拿这条线判它们等于让闲聊轮全死 | 40,960 B | §5.7 第⑥条；判法见 `context.py: tools_box` / `check_tools_budget` |
| `history` | **不设断值**（T48 记账当场的决定） | 它进过一次断值表（值取 `session.HISTORY_WINDOW_BYTES=65,536`），随即撤掉：`_ledger` 把每行与 `section_budget(name)` 比，超了就 `ContextBudgetError`、**请求不发出**；而 `<history>` 那一行是按 `session.messages` 在**压缩链上游**记的（`context.py` 第 629 行），压缩要到那之后才跑。挂上断值等于让长对话**在压缩来得及之前**就硬失败——把「该压缩」写成了「不许发」，正是同一张表里 `tools` 那一行警告过的病 | 无 | 管历史大小的是压缩链自己（`DEFAULT_MAX_CONTEXT_TOKENS=65,536`，同一把尺 1 B ≤ 1 token）。本行只**记账**：`sections` 里有一行 `history`（`kind=conditional`），另有一条独立回执 `ledger["history"] = {window, dropped, prefix_bytes, bytes, compress_at}`。**`compress_at` 是参考线不是断值**，字段名刻意避开 `budget`，免得读回执的人把它当闸 |

**欠账表**（`SECTION_BUDGET_DEBT`，每个「今天超目标」的块必须挂着欠哪个任务）：`skills` → 描述瘦身（**无任务号**）；`knobs` → T40 只还了一半（条件注入落地、文案未瘦身，**无任务号**）；`planning_round` → 与 `knobs` 同欠（T40 后冷启动 2,275 B、口播链 4,384 B 已进目标，剩下的全是 `knobs`）；`skill_instructions` → **T42 只还了一半**：分段取块已落地（默认集 31,762 → 24,223 B），但 **16,384 B 目标今天没到**——最常见的代码链一轮实发 28,698 B，差的 12.3 KB 是「写代码必须随身的合同」（选卡方法 / 适配 demo / 可用 import / 代码模板 / 七镜七栏 / 十一道闸 / 通道表 / ⑮⑯ / 字体白名单 / TOKENS 合计 21 KB）。**再切就是砍合同不是砍脂肪**——每一块都有闸在真读它，砍掉只会把闸前拦截换成事后打回。**没有任务号**，要还它得先回答「哪一块不给了」。**T57/T62 之后这里另多一笔账**：断值仍是 41,007（刻意不重算），而「能拼出来的最大那一份」实测已到 **40,796 B**（默认集 24,223 + 十一块），**余量只剩 211 B**——第十二块要么先砍出一块地，要么这一行就得改数，而改数这条路 Q55 已经封了。**`memory` 于 T39 还清**（读侧召回 + `USAGE_NOTE` 105 B，注入上界 ≈1.8 KB ≤ 目标 2,048 B，故从欠账表移出——移出时同批改了本节、常量表与 `test_memory.py` 的断言，免得「还清了」只体现在一句注释里）。**现值口径在 T40 纠过一次**：`knobs`（旧记 1,829）与 `planning_round`（旧记「金样 4,834」）原先都是照桩门量的，现值一律改成真白名单的实测（§9-Q104）。断值表、目标表、欠账表**同批改两处**（`context.py` 的常量与本节），单改即红（`tests/test_context_hygiene.py` ⑥ 逐行对账）。

**`tools` 这一栏为什么进断值表却不进欠账表**：它在**裁过的那一档**上已达标（14,561 B ≤ 40,960 B），超目标的是**未裁的那一档**（闲聊 / 定时任务 / 直接调用仍整批发，探针下限 54,140 B）。那一档没有进欠账表，是因为它不是一个「该瘦但还没瘦」的块——它是**全量能力面本身**，要裁它得先回答「闲聊轮该给哪些节点」，那是产品决策不是预算决策（§9-Q105 记了这条与另外三条取舍）。今天如实记为**未做、无任务号**，不许拿它当「已达标」。§8.1 十二支夹具跑完后，用**真 P95 × 1.15** 替换上面那四个「实测 × 1.15」的数，并同批改本节与常量表。

### 5.5 常驻名单对账测试（不许降级成注释）

`tests/test_code_spec.py` 第 ⑤ 节用正则从 `lint-rules.mjs` 抠出 `ALLOWED_IMPORTS` 与 Python 侧 `IMPORT_WHITELIST` 逐项比；第 ⑦ 节比技能正文是否把名单列全。**任一侧新增包而另一侧未同步，测试必须先红**，理由写在 `code_spec.py:1421-1427` 的历史里：漂一次的成本是「打包期一句解析失败」，模型拿不到人话。

### 5.6 golden 集与判断型闸的回执格式（⑲-b/⑳/㉑/㉒ 共用）

**golden 集**：`remotion/golden/`（新增，进 Dockerfile COPY）。它不是「另做一次标注工作」——**§8.1 首轮 12 支的抽帧就是它的初始内容**（T34）：每支片每镜留 1 帧，标三档 `good` / `bad:<形态码>` / `borderline`，形态码固定四个：`card-only`（卡片+文字）、`motif-crushed`（母题被挤）、`picture-copy`（代码不同而画面相同）、`unanchored-motion`（动效不锚任何数据）。

```
remotion/golden/index.json → [
  { "frame": "frames/<run>/<sid>.png", "verdict": "good"|"bad"|"borderline",
    "form": null|"card-only"|"motif-crushed"|"picture-copy"|"unanchored-motion",
    "labeled_by": "human", "labeled_at": "<iso>", "sid": str, "shot_no": int }
]
```

`borderline` **不参与** ㉒ 的符合率计算（它只用来暴露判据边界，T35 会把它单独列成一张表）。初始规模预估：12 支 × 每支镜数，外加 §8.3 七支反例里那 **4 支 `bad` 样本**（第 1、5、6、7 支，它们是 ㉒ 负样本召回率的来源；`motif-crushed` 那一档另用 2026-10-11 的历史产物补）。

**判断型闸的回执格式**（`code_render.py` 逐镜记录，字段名冻结；闸⑪ 的 `asset_motion` 记录键沿用同一条链）：

```
"judgement": {
  "gate": "19b"|"20"|"21",
  "observed": "<模型/几何实际说了什么，原文>",
  "declared": "<合同申报了什么，原文>",
  "verdict": "pass"|"fail"|"undetermined",
  "reason": "<一句人话；undetermined 必填>",
  "repeats": [ "<同一帧重复 N 次的各自结论>" ]   # ㉒ 用它算一致率
}
```

三条硬规则：`observed` 必须是**原文**而不是转述；`undetermined` **不许折算成通过**（D-未判不绿）；`repeats` 少于 N（默认 3）次时 ㉒ 直接判 `undetermined`——没有重复就没有一致率可谈。

### 5.7 上下文面契约（冻结：装配出口、账面格式、六条瘦身改法）

这一节是 ㉓ 的合同部分。**先说清哪些今天已经达标**，否则「动态灵活」会被写成一句对既有成果的否定：按链预加载（`vocab.py:27-38` + `plan/skills.py:13-37`）、规划轮工具物理过滤（`plan/vocab.py:163-189`）、条件块守卫（`prompts.py:83-108`）、`always:` 缺席导致的常驻技能正文 0（`skill.py:294-298`）、纠错 nudge 只在失败时回灌（`agent.py:171-189,977/994/1011`）——这五条**已经是动态注入**，本批不动它们的机制，只补账面和残留。下面六条是要动的。

**① 装配出口与账面格式**（唯一插入点，别在别处记账）

```
context.py:226 ContextBuilder.build  →  返回前产出（值挂在 self.last_ledger；agent 侧取走，
                                         以 {"type": "context_ledger", …} 进 RunState.qa_parts）：
"context_ledger": {
  "sections": [ { "name": "<块名>", "bytes": <int>,
                  "kind": "always"|"conditional"|"on_demand",
                  "condition_key": "<{?key} 或 implicit_skills 判据名；always 填 null>",
                  "referenced_by": [ "<本请求内引用它的工具名/闸号/任务号>" ] } ],
  "tools": { "sent": <int>|null, "total_registered": <int>|null,
             "filter": "planning_registry"|null,
             "largest": [ {"name":..., "bytes":...} ] },   // None = 未判，不是 0
  "over_budget": [ "<块名>" ],        // 非空 → 装配抛 ContextBudgetError，请求不发出
  "over_target": [ "<块名>" ],        // 超 §5.4 的**目标**（R9 的欠账，不拦）——T37 新增
  "orphan_sections": [ "<块名>" ]     // always 且 referenced_by 为空
}
```

**`over_target` 是 T37 落地时加的第五栏**（原格式四栏之外）：实测下来有三块**今天就已超目标**（§5.4.1 的欠账表），如果只留 `over_budget`（断值），这三块会被「现值 × 1.15」的断值洗成绿色，R9 剩下要还的账就没人记得了。两栏的关系与三个常量表（断值/目标/欠账）在 §5.4.1；口径是「账要能区分**合规**与**还没还完的账**」。

几条落地约束（2026-10-13 T37 实现时定的，实测坑）：①子块（`knobs`/`session_history_rows`）单独成行、字节已含在父段里，所以**账面是逐块看、不是加起来看总账**；②`Section` 是可选的——裸串照样能用（按首个标签自动取名，`<skill_instructions name="X">` → `skill_instructions:X`），但那样只有名字，`kind` 一律 `on_demand`；③字节口径是 **UTF-8 字节**（不是 token）——账面要能被人对着表逐字复核，token 是估算量、会随计数器口径漂（两者分工：字节进账，token 进 `compress.py` 的压缩预算）；④账面**就地取走**（`run()` 里 `build()` 之后立刻读 `last_ledger`）——`ContextBuilder` 是跨 run 共享的，留到后面读会读到别人的账。

- 现状：`prompt_bytes`/`prompt_size`/`字节账` 四目录全零命中；逐调用只记 usage（`hooks.py:277-321,453-461`）。
- 改成：账面的**取数**走 `compress.py:31-62` 已有的 `default_token_counter`/`estimate_tokens`（不新造计数器，字节用 `len(s.encode())`）；账面落进 `hooks.py` 那条既有记录通道，随渲染/规划任务行留档。
- 机器判据：`over_budget` 非空时**必须抛错**而不是继续发；单测里注入一段超长 `USAGE_NOTE`，断言请求发不出去（红）+ 删回预算内即绿。

**② 技能正文从「整段全量」改成「分段键 + 按片装配」**

- 现状：`plan/skills.py:35-36` 注入 `{skill.body}` **整段**；`SKILL.md` 入库正文 13,580 字符。附件侧已有按需通道（`LoadSkillTool skill.py:302-373`，`path=` 读附件、**不截字节**；`motion_explainer` 附件里 `pipeline.md` 24,720 B / `sound-design.md` 27,884 B / `aesthetic-rules.md` 27,656 B / `shots/*.md` 126 个 / `demos` 159 文件，全目录 .md 合计 671,214 B / 291 文件）。**缺的只有 SKILL.md 内部的分段键**（现在没有）。
- 改成：`SKILL.md` frontmatter 增 `sections:` 声明，正文用 `<!-- section: <name> -->` 切块；`preload_skills` 收 `sections=` 参数，按**本片计划实际用到的通道**挑块（`rough`/`three`/`katex`/`image` 四通道 → 四块，字幕/BGM 两块按需）。**T36 要新增的三节一律写成新 section 块，不追加进默认注入集**——否则 ㉓ 的 16 KB 预算会被这批改动自己顶穿。
- 机器判据：`test_skill.py` 现有 `:125-128`（always 常驻 vs 渐进）之外，新增双向断言：①不含 motion 节点的请求里，`sound-design` 块字节必须为 0；②含 `image` 通道的请求里，`image-channel` 块必须出现。任一条不满足即红。
- **落地记录（2026-10-13，T42/T36 同批）**：分段机制住在 `skill.py`（`_SECTION_MARK` / `_SECTION_END` / `_section_pieces` / `split_sections` / `assemble_sections` / `_parse_sections` + `Skill.declared_sections` / `section_names` / `assemble`），判据住在 `plan/skills.py`（`sections_for_plan(compiled, card)` / `changed_film_knobs` / `wants_sound_design`），装配侧 `agent.py execute_plan` 把两者接上并把**卡面**一并传进去（只为读 `param_options[].default`）。四条判据：①计划里有 `MOTION_NODES` 之一 → `subject-naming`/`motion-anchors`/`text-only-quota`（T36 那三节，写代码就得随身的合同）；②`changed_film_knobs` 非空 → `film-knobs`；③`wants_sound_design` → `sound-design`；④计划里有 `IMAGE_NODES` 之一 → `image-channel` + `asset-motion`。**十四处口径决定如实登记**（四条偏离 + 编号）见 **§9-Q106**；其中三条最要记住：`sound-design` 不是从附件搬来的（正文里原本没有声音节，新写的这一块只有 933 B 且**只做指针**，附件 27,884 B 仍走 `load_skill(path=)`）；**收尾标记 `<!-- /section -->` 是这次新增的语法**（没有它，「中间抽掉一段、后面接回默认集」写不出来）；`motion_explainer` 的默认集从 31,762 B 降到 **24,223 B**（最常见代码链一轮实发 28,698 B），但 16,384 B 的目标**仍未达**。

**③ `knobs` 段改条件注入（参数教程常发的最后一条残留）**

- 现状：`vocab.py:210-252 knob_facts` → `prompt.py:327-339 _knob_lines`，**只要白名单非空就在每个规划轮全量给**。
- 改成：与 `_IMPLICIT_SKILLS` 同构——`knobs` 只在该请求的计划步骤命中「暴露这些旋钮的节点」时才发；无命中即不发。判据沿用 `plan/support.py:71` 的去前缀节点名匹配，**不新增第二套判据**。
- 机器判据：双向红样各一支（该关没关 / 该开没开），落 `tests/test_planning_prompt_golden.py`（`:83-110` 已是逐字相等断言，直接加两支 golden）。
- **落地记录（2026-10-13，T40）**：判据拆成独立模块 `plan/knobs.py`（只依赖 `support`，`prompt.py` 只管排版）；`{knobs}` 单行块改成 `{?knobs}` 守卫块（两份模板逐字同改）；命中语料与两条刻意的排除见 **§9-Q103**；冷启动的代价由打回消息带回真键名补偿（`validate._check_params`）。**顺带就地修正了 ㉓ 的一个真机死结**：live 量出 `planning_round` 断值 8,192 B 会把最普通的视频链规划轮判死（8,982 B → `ContextBudgetError`）——按同一口径重算为 14,976 B，取证与「为什么不是抬预算」见 **§9-Q104**。

**④ 记忆从全量 dump 改成召回，并且禁止静默截断**

- 现状：`memory.py:123-141` 每次把 `user`/`tool` 两类**全量**塞进 `<memory>`（上限 `per_category_chars=2000`、`total_chars=4000`，`:91-92`），超限**保尾部**（`:116-121`）——即悄悄丢头部；另固定追加 `USAGE_NOTE` 344 字（`:94-104,141`）。`query` 形参（`:123`，`# noqa ARG002`）是为召回预留的，**文件头 `:9` 自己写着「当前全量携带」**。写回侧已实现（`plan_retro.py:69/137/145/172-198`，调用 `agent.py:1978,2144-2150`），所以缺的只是读侧召回。
- 改成：把 `query` 真正用起来——按本轮用户消息 + 计划步骤名做关键词召回，取 top-k（k 默认 6）条；`USAGE_NOTE` 从 344 字压到 ≤120 字（它每次常发，且讲的是「怎么用记忆」的教程，属参数教程同类）。超限行为从「保尾部」改成**报 `over_budget`**：少带几条可以，但必须留一行「本轮因预算未注入 N 条，最早一条 <时间戳>」。
- 机器判据：`tests/test_memory.py` **今天没有 `:91-92` 上限的用例**（实测零命中），补三支：①写入 40 条、本轮话题只关 3 条 → 断言注入集恰好含那 3 条且不含无关条；②超限 → 断言出 `over_budget` 与那句「未注入 N 条」回执，而不是静默截断；③`USAGE_NOTE` 字节数断言 ≤120（改长了要同批改断言，同 ⑫ 的纪律句式）。

**⑤ 两条链的提示词真相源合一**

- 现状：主链走 `prompt_library`（`prompts/*.md` + 内联回落，一致性由 `test_prompt_library.py:88` 守），剪辑副链 `editing_agent.py:38` **写死** `EDITING_SYSTEM_PROMPT`、`:105-117` 自建两段且不接库。于是同一句纪律改一处不改另一处，属 §1.9 那类「两份真相」污染的同一个形状。
- 改成：`EDITING_SYSTEM_PROMPT` 落 `prompts/editing_round.md`，副链改调 `prompt_library`；内联常量退化为回落副本，并被 `test_prompt_library.py:88` 那条**既有一致性守卫**纳入（不新写测试骨架）。
- 机器判据：把 `editing_round.md` 加进 `:88` 的文件↔内联对账清单，任一侧改动未同步即红。
- **落地记录（2026-10-13，T42）**：`prompts/editing_round.md`（1,331 B）是真相源；`editing_agent.EDITING_SYSTEM_PROMPT` 退化为**逐字回落副本**，`build_editing_agent(..., prompt_library=…)` 让副链与主链**共用同一个 `PromptLibrary`**（含热替换）。对账加在 `tests/test_prompt_library.py` 的 ② 那一节（② 原本只对 `system_prompt.md` 与 `DEFAULT_SYSTEM_PROMPT` 逐字对账，这次把副链那份一并纳入，**不新写测试骨架**）。**落地时真红过一次**：内联副本原先把五段拼成一整行（无段间换行），文件是分段的，`_norm`（空白归一）把「文件里的空行」变成空格 → 两边差一个空格即判漂移；修法是把内联那五段按 `\n\n` 分段，与文件逐字对齐（指纹 `EDITING_PROMPT_FINGERPRINT` 随之变化，这是对的：它标的就是这份内联文本的版本）。

**⑥ 执行轮工具面按链裁剪 + MCP 描述瘦身**

- 现状：执行轮**无过滤**，53–57 份 schema 整批发（`agent.py:716-718`）；MCP 侧描述随 `tools/list` 全量进入（`tools/mcp.py:502,618`），`tools/mcp.py:544-560` 还额外附一行「必须显式调用」。规划轮已有可复用的裁剪件：`plan/vocab.py:163-189 planning_registry`。
- 改成：把 `planning_registry` 的思路泛化成 `registry_for_round(plan_steps)`——执行轮按计划的节点集合取「本计划用到的节点 + 通用件（file/web/memory/ask_user/load_skill/cron 除外，team 7 件按是否多 Agent 计划决定）」，其余不批发schema，但**保留 `load_skill` 式的可达性说明**（模型要知道能请求扩面，否则会把「没给 schema」理解成「功能不存在」，这是 §5.5 名单漂移的同一种失败）。MCP 每份描述截到 ≤600 B，超出部分移进 `load_skill` 附件（**移出即须有闸读到**，同 ⑫ 的双向断言）。
- 机器判据：①一支计划只含 `plan_motion`+`render_motion_code` 的请求，断言 `context_ledger.tools.sent` < `total_registered` 且差值 ≥ 20；②断言被裁掉的节点名仍出现在「如何请求扩面」那一小节里（防止把能力裁成不可发现）；③`largest[0].bytes ≤ 600` 对 MCP 侧成立。
- **落地记录（2026-10-13，T41）**：判据与装配拆成独立模块 `plan/round.py`（只依赖 `support` + `knobs` 的 `bare_node`，**不新增第二套节点名口径**）——`registry_for_round`（重建一张表，不就地裁主注册表）、`trimmed_nodes`、`scope_lines`（**空表即整节不发**）、`truncated_nodes`/`unbacked_truncations`（承载闸）。判据只有一条：`tool.contract is not None` 才是剪辑执行节点，其余一律算通用件、一律不裁——`load_skill`/`ask_user` 因此**物理上**裁不到（Q56 的硬要求由结构保证，不靠一张「永不裁」名单）。装配侧：`agent.py _round_registry`（缓存键 = 主注册表清单 + **计划节点集合**，两样一起）、`_scope_section`（`tool_scope` 段，走 `extra_sections`）、`execute_plan` 传表、`_resume_registry` 按 `approved_plan` 取回**同一条口径**的表（续跑前后两半的工具面必须同一张）。MCP 侧：`tools/mcp.py compose_description` 截到 `DESC_MAX_BYTES=600`（UTF-8 字节、不切碎字符；「必须显式调用」与「去哪找」两行**永远留下**，先丢正文尾部）。**判据③的口径改了，如实记**：原文照字面读是「整份 schema ≤600 B」，那会把入参形状一起砍掉（分镜类节点天然近 2 KB），改成「描述 ≤600 B」（账面新增 `desc_bytes` 一栏可逐份核），理由与另外三条取舍见 **§9-Q105**。**未跑的一腿**：执行轮真机 run 读 `context_ledger.tools` 这一腿**未跑**（本轮停在这里），如实记为未判——探针量的是真契约 + 真注册表装配，不是真 run 的那本账。
- 不做的事，以及代价（按「断掉了哪条既有保证」标价）：**不裁规划轮的白名单语义**（`_MUTATING_TOOLS` 剔除是安全保证，不是体积优化）；**不动 `ToolCatalog`**（`catalog.py:116-276` 是出口/界面名称单源，不进 prompt，把它当瘦身手段是误解）；执行轮裁剪换来的省是 **token，不是部署耦合**——`get_definitions` 仍在，只是按轮取子集，`agent.py:1899-1911` 的缓存签名要一并把子集身份算进去，否则会出现「同一签名不同工具面」的假命中（已落进 `_round_registry`，判据与红样见 §9-Q105 ④）。

---

## 6. 任务分解 R0–R10（每条都有红→绿→验收→退出四态）

粒度约定：**一个任务 = 一次可独立验证的改动**。`红法` 列是「先构造出应当失败的那个东西」，这是判据存在性的唯一证明。

编号约定（免得被当成漏项）：任务号分配到 **T0–T49**，共 **49 条**，其中 **T12 是空位**——它没有对应的独立判据，因此不指派任务，任何交叉引用若指向 T12 均按引用错误处理（这样「空号」不会在后续轮次里被随手补成一条无红法的任务）；**T45 属 R3**（§6 R3 表内，xuzc 账号那 132 行的检索命中档），所以 R10 从 T43 起但跳过 T45。

### R0 —— 让诚实回到链路上（先做，不做完其余都是假绿）

| ID | 任务 | 改动文件 | 红法（先证红） | 绿（验收） | 退出条件 |
|---|---|---|---|---|---|
| T0 | SKILL.md ↔ 真实来源逐词对账 | `backend/examples/skills/motion_explainer/SKILL.md`（:200-213、:378 及全部点名字段） | 现状即红：`test_motion_channel.py` 报出 `['asset_library','asset_sources','object_key','read_file','source_title','source_url']` | 该测试 217 条断言全绿，且新增断言：**正文里出现的每个 `origin` 取值都能在 `pg_constraint` 里找到** | 正文不再对模型许诺链上不存在的东西 |
| T1 | 闸⑱ 投递腿存在性自检 | `code_spec.py`（新增 `check_stage_leg_present`）、`code_render.py:656` 附近前移 | 在**没有** `stage_asset` 的客户端桩上断言：声明 `assets` 的 spec 必须在**生成前**返回「投递腿缺失」，而不是抛 `MediaError` | 桩测红→绿；活容器（今天正好缺这条腿）跑一次必须**报红**——这是本任务唯一正确的首轮结果 | 「缺腿」从运行期事故变成开始前的可报告状态 |
| T2 | 迁移执行口修复 + `'art'` 落地 | **废弃** `storage/migrations/` 目录（把 `20261012_asset_origin.sql` 内容并入 `storage/migrations.sql:7-9`，并把 `asset_library` DDL 留在 `schema.sql`）；活库执行 `ALTER TABLE materials DROP CONSTRAINT materials_origin_check; ALTER TABLE materials ADD CONSTRAINT ... WITH ('upload','library','bgm','url','render','art');` | 红法：在活库插入 `origin='art'` 的一行，必须失败 → 改完必须成功 | §1.2 的 constraint 定义打印含 `'art'`；`library` 与 `art` 各能插入并被读回 | 新库/老库两条路径口径一致（`schema.sql:54` 与 `migrations.sql` 逐项相等，新增常驻断言） |
| T13 | 把「无消费者/孤儿」变成红测 | 新建 `tests/test_delivery_hygiene.py` | 逐条构造（现状**全部应当红**）：①`styles.json` 存在但无人读；②`migrations/` 存在但无人读；③`remotion/public` 不存在；④`OBJECT_PREFIXES` 不含 `assets/`；⑤存在 `@remotion/*` 依赖不在 `package.json`（许可/T21 的镜像可达性检查：读 `node_modules/<pkg>/package.json` 的 `license` 字段，MIT/Remotion License 之外的拒绝安装） | 实现完成后五条全绿，且每条给出具体修复物 | 孤儿类缺陷从此不可能静默存活 |

> T13 的顺序很重要：先让它红，后面每个 R 的交付物才有一个「不许留孤儿」的收口闸。

### R1 —— 投递腿端到端打通（图像通道的第一根管道）

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T3 | Python 侧素材投递 | `code_render.py:648-679`（已在新代码里，**待生效**）；`remotion_client.py:147,185` `stage_asset` | 投一个 sha16 与申报不符的字节 → 必须拒；投成功 → `public/assets/<sid>/` 出现该文件且响应 `sha16` 对得上 | 一镜带 2 张图，真机 `POST /stage-assets` → 容器 `ls` → 产物里能看到图 | 三处 sha16 一致（申报 / 落盘 / 闸⑬ 回读） |
| T4 | 建 `remotion/public` | 新增 `remotion/public/assets/.gitkeep`；`remotion/Dockerfile` COPY `public/`；`server.mjs:172` 的 `publicDir` 保持 | `test_delivery_hygiene.py` 第③条红→绿 | 容器内 `/srv/public/assets/` 存在且 lint 读得到 manifest | `staticFile` 有可落之地 |
| T5 | 母版与主题进镜像 | `remotion/Dockerfile` 补 COPY `src/FilmLayout.tsx`、`src/themes/palettes.json`（app 镜像侧同样补齐）；`creation-remotion` 重建 | §1.8 最后一格：今天 `/app/remotion` 只有 4 个文件，缺 FilmLayout/server.mjs/demos | `docker exec creation-remotion md5sum /srv/src/FilmLayout.tsx` == 盘上 md5 | **镜像里跑的就是工作树那份** |

### R2 —— 让 `image` 通道在闸① 下真的可用

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T6 | `staticfile-asset-check` 与 manifest 对齐 | `remotion/lint-rules.mjs:614,737`、新增 `readAssetManifest()` | 引用一个 manifest 里没有的名字 → 必须 lint 红；引用存在但未投递 → 也红（区分两句错误文案） | 正反各一支；闸① 的报错是**人话**而不是解析失败 | lint 期能判定资产存在性 |
| T7 | `PICTORIAL_CHANNELS`/`CHANNEL_ORDER` 纳入 image | `code_spec.py:617-625` | 6 镜以上全 `text` → 闸⑦ 必须红（`MAX_TEXT_ONLY_SHARE=1/3`） | 全绿但配额账逐镜摊得出 | `image` 进配额账 |

### R3 —— 素材供给闭环（库 → 检索 → 镜头）

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T11 | `asset_library` → `materials(origin='library')` 投递口 | `backend/agent_framework/server.py`、`session_service.py`、`nodes/asset_nodes.py:27,65` | 今天 `library` 0 行而库 132 行——**断言这条转换存在**，无转换即红 | 前端选一条库素材 → `materials` 出现 1 行 `origin='library'`，`owner_user_id` 正确 | 132 行策展库对链上可见 |
| T10 | 逐源探测与入库 | 沿用 `833c2da` 的探测链，补断言：探测失败必须落「失败原因」而非静默 0 条 | 给一个已知不可达源（知乎两档 UA 全 403）→ 必须给出「403，代码修不了」这种可解释回执 | 可达源入库、不可达源出原因 | 与「外部来源可达性」事实表一致：B 站视频页可读；知乎 403；YouTube `-watch` 是 JS 壳 |
| T14 | `assets/` 前缀生命周期 | `session_gc.py:34`（**保持不含** `assets/`）+ 新增 `scripts/asset_gc_report.py`（只读巡检） | 断言：删会话后 `assets/` 对象仍在（防未来误加前缀） | 巡检报出「引用计数为 0 且超 30 天」清单 | 见 §9-Q26：**不做自动删除** |
| T45 | **当前唯一能测的档**：xuzc 账号下库有 132 行 | — | 检索节点必须能从这 132 行命中 ≥ 1 条相关（无密钥需求） | `search_illustration` 命中并 `select_illustration` 落 `materials` | R3 不依赖任何新密钥即可验收 |

### R4 —— 图像进画面 + 闸⑬

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T8 | 闸⑬ 资产存在且非纯色 | `remotion/server.mjs`（新增像素校验）、`code_spec.py` 记录键 | 三张红样：1×1 PNG / 全白图 / manifest 有但文件缺 | 三类各拒并各出一句人话；正常图过 | 「用了图但画面没图」不再可能通过 |
| T9 | 版式母题随图扩展 | `remotion/src/themes/palettes.json`（**唯一来源**）、`FilmLayout.tsx` | 断言 `len(palettes())==8`，且新增档必须同时被 Python `code_spec.py:1143` 与 `lint-rules.mjs:95` 读到 | 新增档两侧同时可见 | 单一来源不被破坏 |
| T8-1 | `styles.json` 二选一 | **决定：删除 `remotion/src/themes/styles.json`**，其语义（三档风格预设 `ink-archive`/`tech-data`/`night-concept`）落进 `palettes.json` 的 `preset` 字段，SKILL.md:133-142 的三档表引用它 | `test_delivery_hygiene.py` 第①条 | 文件消失或被读；三档表与 `palettes.json` 逐项一致 | 不留「写了但没人接」的预设 |
| T20 | 模型「读得到码」 | 生成轮按 `derived_from` 集合注入 demo 全文（§5.4） | 断言：注入内容里含被引用 demo 的字节，且 `load_skill(path=)` 不截断 | 一镜引用 2 个 demo → 上下文里真有那 2 份全文 | 手搓失去技术理由 |
| 验收 | **图必须上屏** | — | — | 真机 ≥3 镜带图成片，抽帧肉眼见图 + 闸⑬⑪ 回执 | **闸⑪ 未判不算过** |

### R5 —— 观感：把 A 面三条尽量往判据推

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T16 | 采纳官方写法块（§3.2 的 6 条）+ `@remotion/fonts`/`rough-notation`/`noise`/`captions` 进白名单 | 两份名单 + `package.json`/lockfile | 名单漂一格即红（T17） | 一镜用 `loadFont` 加载 `NotoSansSC-*.woff2`，画面字体与 token 声明一致 | 第六根因（静默回落）有判据：`font-family-whitelist` + 新增「加载即断言」 |
| T16b | `@remotion/effects` 真渲夹具验证 | **新探针 `remotion/tests/effects-probe.mjs`**（4 支真渲 + 2 支自检；判在真渲帧的像素上，底色从母版回读的 `tokens.bg` 取，不写死） | 若出现 `glow 染品红` 那类色彩异常 → 判红，**不进名单**；**另有一层 D-红先**：判据自己必须对构造样本真红（把真帧绿通道压一半 → 品红探测器红；不挂 effects 的同一张图 → 「点真画出来了」红），否则「全绿」可能只是判据从不说话 | 出片色彩正确、swangle 下不崩；`paper()` 红通道跨度 193（纯灰源本该 0）、品红签名 0.000%；`halftone()` 点色占 11.8%、第三色 3.58%（<5%）；挂错宿主逐字节相同（6220800 字节差 0） | **过了——但按子路径逐件进名单**（`@remotion/effects/paper`、`@remotion/effects/halftone`），桶名不进：4.0.534 的桶文件只重导 11 件，这两件不在里面，写包名拿到 `undefined` 而**打包期不报错**（§9-Q89）。四处名单已同步（`lint-rules.mjs` / `code_spec.py` / `package.json` / `SKILL.md` 44 项），`test_code_spec` ⑤ 的逐项对账同步放宽成「真文件或 exports 入口」两把尺 |
| T18 | 闸⑭ 动效事件密度 | `server.mjs` 帧采样 + `code_spec.py` 记录键 | 红样：一支「所有元素 0 帧到位」的静帧镜；一支「三事件同帧齐发」的镜 | 每镜 ≥2 次先后事件、相邻 ≥0.4 s；全片不得齐发 | 「有动效但看不出动」被机器拒 |
| T15 | 闸⑮ 版式族配额 + 闸⑯ 主体具名 | `code_spec.py`（新增两 checker `check_layout_family`/`check_subject_named`，进 `validate_code_spec` 的 checker 表 `:662`；`_norm_shot` 归一 `subject`/`subject_channel`；`style_facts` 加 `card_families`/`subject_channels` 两本账；`spec_schema()` 加两栏，`subject` 进 `required`）；`nodes/motion_plan.py`（提醒合流 + 逐镜账三栏）、`nodes/motion_code_nodes.py`（提醒合流）；`agent_framework/plan/support.py`（文案词表收三键）；`examples/skills/motion_explainer/SKILL.md`（闸表 ⑮⑯ 两行 + 教程段）；**新用例 `tests/test_layout_subject.py`** | 红样（已按 D-红先落成 109 条里的 27 条）：8 镜同卡 / **六张不同卡全挤在一个族** / `subject=""` / `subject="理念"` / `subject="字"` / 有图形主体不申报通道 / 申报 rough 而代码是 vector / 纯文字镜却申报 | 全绿；⑦ 的通道账**不**改读申报栏，改为逐镜并排 `declared`/`detected` 并由 ⑯ 强制一致（§9-Q85） | ⑦⑮⑯ 全部判在**烧像素之前**（三处调用点共用 `validate_code_spec`；口径争议见 §9-Q84） |
| T17 | 名单同步与 `@remotion/preload` 处置 | 两侧名单 + `tests/test_code_spec.py` ⑤/⑦ 节；**决定：不装 `@remotion/preload`**（官方文档自陈「渲染不需要 preload」，且 10-11 那次漂单的元凶之一） | 现状一项不红（**44 项**逐项相等，含 T16b 进名单的两件）；抽掉一项必须红 | 抽一项→红→补回→绿 | 对账常驻化：两侧逐项相等、`package.json` 依赖覆盖、技能正文逐项列出**且正文那句「几项」的数**与常量相等（§9-Q89） |
| T21 | Dockerfile COPY 完整性 + 许可检查 | **新脚本 `scripts/image_parity.py`**（清单**现读 Dockerfile**：`FROM`/`COPY`/`ADD`/`WORKDIR`/`CMD`/`RUN`、续行、`--from=` 阶段引用、`.dockerignore` 六种形态、目录源展开、WORKDIR 相对 dest；对账腿 = `docker exec md5sum --` 逐件与盘上比）；**新用例 `backend/tests/test_image_parity.py`**（54 条 / 两个布局分别 54 与 46 条——⑨⑪-e⑫-c 要真 Dockerfile 或真 remotion 源码树，缺了就如实记未判）；`remotion/Dockerfile`、`app` Dockerfile 的 COPY 段；许可检查仍在 `tests/test_delivery_hygiene.py` ⑤（§9-Q77） | 红样（D-红先，全部落在用例里）：源文件不在盘上 / 空 glob / `.dockerignore` 四种形态各排一件 / `--from=` 只查存在 / **把 `COPY scripts/ /scripts/` 一行抽掉 → `/scripts/*` 立刻从清单消失** / 带实参的 `CMD node a.mjs b.mjs` 取错脚本名（这条是写完就被用例抓住的真缺陷）；读不到 Dockerfile → 退出码 **2**（未判，不是绿） | 两腿 `docker exec md5sum` **逐件**与盘上相同：`creation-app` **575/575**、`creation-remotion` **430/430**（0 缺件、0 内容不一致）；两栏不进对账但逐条点名——「构建期重写」remotion 侧一件 `font-whitelist.json`（§9-Q87）、「运行期改写」一件 `src/user-shots/shot.tsx`（服务每次渲染前覆写；不比字节但**仍判它在**，§9-Q89） | 见 §4-2、§9-Q23、§9-Q86（清单来源与两腿口径）、§9-Q87（构建期重写这一类怎么算）、§9-Q89（运行期改写 + 子路径两把尺） |
| T18b | 母题占比回归 | **新用例 `backend/tests/test_motif_share.py`**（34 条，横屏 1920×1080 / 带子 1800×732）；判据比的是闸④ 第二半 `code_render._empty_check`/`_frame_diff_stats`（`fill_span` + `SPAN_MIN`/`TIGHT_MIN`/`FILL_TARGET`），**不新造任何阈值**（§9-Q88） | 红样（D-红先，喂真实字节）：记录在案的那批 `fill_span≈9%~36%` 里 9% 的细线条簇 → 被「几乎空的」接住；25% 的实心块（占比 6.3% 远过 2% 线）→ 被「挤成一团」接住；外加**边界支**：记录的上限 36% 实心块**今天过闸**（如实钉住，见 §9-Q88） | 铺满带子那支过闸（带内 100%；**同一份字节按整帧量只有 68%**，即 `code_render.py:249` 那句注释里的数）；50% 那档过闸但进提醒（`FILL_TARGET` 只提醒不打回）；`_band_of` 拿母版回读的四个数、Python 侧不另算几何 | 合同 T18b 那句「横屏同理」从此有夹具：横屏带子矮（732/1080），换个分母读数就翻倍——这是竖屏那套（`test_code_gates.py` :450）覆盖不到的一档 |

### R6 —— 自动出图（唯一有外部依赖的一档）

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T24 | 出图节点 | `backend/storyline_server/nodes/`（新增 `imagegen` 节点并注册进 `build_real_registry`，同 `asset_nodes` 的接法）；`server.py:2299-2330` 的 body 改为可配 `model`+`size`，去掉硬编码 `doubao-seedream-5-0-pro` | 今天**红**：没有任何节点调用生图（`grep` 唯一调用在测试口） | 配置口存在 + `/settings/image/test` 返回 `ok:true` + 一张落盘 PNG（**只验存在，绝不打印密钥**） | 「没有图像通道」第五根因彻底闭合 |
| T24b | 抠图后处理 | `rembg`（锁旧模型，避开 `bria-rmbg`）；DA3-Small 用于深度/显著性；`vtracer` 供 `vector` 通道 | 出图无透明通道 → 断言走抠图分支而非直接贴 | 透明底素材入库带 `sha16` | 全部在创作期 Python 侧（§4-4） |
| T25 | 供给优先级策略（写死，不询问） | 检索节点 + `imagegen` 编排 | 无出图后端时：检索库 → 未命中 → 明确报「图像通道降级为检索，本片 N 镜无图」并让 ⑦/⑯ 红 | 库有 132 行可测；配 key 后自动升级 | 见 §9-Q27：无 key 也必须有**可见的降级回执**，不许静默出「卡片+文字」片。**已落地**（`tests/test_imagegen.py` ⑤ 16 条 + `tests/test_library_pick.py` 检索腿）：降级回执实测 `未配出图后端，图像通道降级为素材库检索（请在「设置 → 出图后端」配 provider 与密钥），本片 6 镜无图`，那一趟 0 行落库、无 `asset` 三键；全文字/色块六镜片被 ⑦ 打回两条、不填 `subject` 被 ⑯ 逐镜点名六条；**配好后同一次调用**即改走真出图（桩后端收到请求、这趟才落库）——「降级」因此是一条**有回程**的路。三个面（工具描述 / 技能正文新增一节 / 节点模块头）逐字同一份措辞，各自带该被拒样本先证红。口径与另两处清单漂移（技能正文的工具来源清单、启动期一致性检查的字段来源）见 §9-Q91 |

### R7 —— 合成、声音与整片收口

| ID | 任务 | 改动文件 | 红法 | 绿 | 退出 |
|---|---|---|---|---|---|
| T22 | 转场落点 | 拼接层（ffmpeg `xfade`）——**不在镜头层引入 `TransitionSeries`**（§3.1）；母版提供由**注入的档名 + 帧数**驱动的入场/出场（`shot_input_props` 的 `entrance`/`exit`/`entranceFrames`/`exitFrames`） | **已落地**。`code_spec.py`：`TRANSITION_STYLES`/`TRANSITION_SEC_CHOICES`/`SHOT_MOTION_STYLES` + `transition_plan()`/`shot_motion_window()`/`check_transition()`/`check_transition_effect()`；`mediaops.concat(transition={style,duration})` → `_concat_xfade`（逐接缝 `xfade` + 同 d 的 `acrossfade`）；`code_render` 在**拼接之前**判时长对账、**拼接之后**按实测秒数判生效；`FilmLayout.tsx` 的 `shotMotion()` 只演**这一镜自己**的进/出，四条取值走 `resolveShotMotion`：**显式 props 优先、没给就取 `getInputProps()` 注入的那一份**——模板里的 `<FilmLayout text=…>` 并不转交这四条，只认 props 就等于整片进出场在生产上**一帧都不演**（见 §9-Q94）。红法（`tests/test_code_transition.py` **61 条**）：档名/档位/动作三类坏值各先证红、拿不到实测时长**不判**、把硬接那一次的实测时长喂给生效判据 → 红、`entrance` 变 → 0 镜命中缓存（对照：`transition` 变 → 键一字不变） | 真 ffmpeg 实测（240×320@10fps 两镜）：选 fade 0.4s 时成片比逐镜之和**短 0.36s**（该短 0.40s，容差 (接缝+2)/fps=0.30s）、回执 `overlap_sec=0.358`；不选转场时差 −0.04s（两条路只差接缝）；同一批镜跑第二次**2 镜全命中缓存且成片照样带转场**。另加结构闸：`remotion/src` 的**代码行**与依赖表里都没有 `TransitionSeries`/`@remotion/transitions`（注释里那段「为什么不引」不算违规）。**真机版面取证**（`remotion/tests/layout-probe.mjs`，先修了它自己过不了闸① 那件事——见 Q94）：同一段代码渲两次（静止 / `exit=zoom`、探针帧落在动作里），findings 种类一致、每个盒的坐标**差 0px**（容差 0.5px）、带子尺寸与阈值不变；把 `toCodeSpace` 的折回掐掉立刻红（差 **7.9px**）——判据不是空转 | D 面保证不被「用官方包」这件事弄断。口径与两条新决定见 §9-Q92/Q93 |
| T23 | 闸⑰ 音画数据对账 | **已落地**。`code_spec.py`：`check_av_accounting()` + 三个常量（`AV_BGM_MIN_SHARE=0.98`／`AV_SILENT_DB=-50`／`AV_TAIL_SLACK_SEC=1/30`）；`mediaops.py`：`audio_levels()`（`ffmpeg -af volumedetect`，抠不到数就 `raise MediaError`，**绝不返回 0 冒充安静**）；`code_render.py` 在 `misfit` 之后、`mediaops.concat` **之前**调用并把 `av_accounting` 带回来；`motion_code_nodes._result` 摊成一行证据。判三件事：① BGM 素材 ≥ 成片 × 0.98（混音走 `-stream_loop -1`，短了不报错而是从头反复——报「会循环约 N 遍」）；② 旁白**两个方向**判**逐镜切片的电平**（关着必须真静、开着必须真响；静音垫片 −91dB 与真说话 −20~−26dB 分得开，而两者**时长一模一样**，所以判的是电平不是「音轨在不在」）；③ 接缝的字尾账：`overlap_sec ≤ min(两侧语音尾余量) + 1 帧`，超了报「接缝 i（sA→sB）的转场会切到字」——T22 的 `acrossfade` 那笔账归这一条。红法（`tests/test_code_av.py` **37 条**）：每条判据两侧都取边界样本（9.8 绿／9.79 红）、量不到电平的那一档**记未判不当通过**、真 ffmpeg 量出「静音与真话时长几乎相等」把②的理由钉在字节上、整链红样（1s BGM）证明**判在拼接之前**——红了连 `master.mp4` 都不出现，而逐镜 `clip.mp4` 照样留着 | 全链绿 + 报告层同源（回执那行摊开 BGM 档、旁白档与最响电平，数与 `av_accounting` 是同一个数、报告层不重算）；真 ffmpeg 实测（隐藏 `-hide_banner`、`volumedetect` 只此一处、`stream_loop` 只此一处） | 「声画」从主观变数据。P20 那句话里「字幕条数 × 平均显示时长落在帧账内」在本链是**退化情形**（一镜一句：条数恒 = 镜数、时长恒 = 切片时长，已含在闸③ 帧账里），`@remotion/captions` 的逐词 token 化**未落地**——记 §10 残余。口径见 §9-Q95 |
| T26 | 前端重建 | `frontend/`（`dist` 落后 src 2 天） | 断言：`dist` mtime ≥ `src/App.vue` mtime | 页面出图配置口 + 附件 `material_id` 路径可用 | §7-D3 |
| T27 | 配额/上限热读不回退 | 沿用 `385d957` | 断言：页面值压过 env | — | §4-8 |
| T15b | 工具总数守卫改法 | `tests/test_storyline_server.py`（今天红：25+3 vs 实际 30） | **决定**：断言从「总数 = 常数」改成「总数 = 注册表实际大小，且每个注册节点都能被 `dag_contract` 列出」 | 绿，且新增节点不再误红 | 守卫的意义是抓「注册了但不可达」，不是抓数字变化 |
| T15c | 压缩阈值 | `tests/test_team_wiring.py`（今天红：期望 8000，实际 65536） | **决定：以代码实际值 65536 为准，改测试 + 在文档/注释里说明为什么是 64 K**（不做「为了让测试绿而回退阈值」） | 绿 | 见 §9-Q29 |

### R8 —— 把「语义 / 丑 / 无意义」这三条转成判据（本轮新增，回应「这三条不能想办法解决吗」）

> 这一组的存在前提：**以前把它们写成「永远没有机器判据」，是因为试图让闸去证明「好」。改成让闸去证明「不是那几种已知坏」，判据立刻可写。** 每个红样都是历史上真被驳回过的那个形态，不是编出来的。

| ID | 任务 | 改动文件 | 红法（先证红） | 绿（验收） | 退出条件 |
|---|---|---|---|---|---|
| T30 | **⑲-a 主体件在场率**（无模型那条腿） | **已落地**，但落点与计划不同：判据读的是**母版量的两个原始读数**，不是 Python 自己数图元——见「为什么」那一栏。`code_spec.check_subject_present()`（三出口）+ 两个阈值 `SUBJECT_MIN_BBOX=0.18` / `TEXT_DOMINANT_SHARE=0.45`；`code_render` 在 `mediaops.concat` **之前**调用（红了连母带都不出现），读数随 `subject_presence` 带回；`FilmLayout.tsx` 新增 `SUBJECT_TAGS`（SVG 形状 / `img` / `video` / `canvas`——**`div` 不在里面**）、`subjectBoxes()`、`shareOfBoxes()`（并集**外接盒** ∩ 画面带 ÷ 带面积），`scanned` 里多三栏 `subjects`/`subject_share`/`text_share`；`summarize_layout` 的白名单同步加三栏。两条硬判据**都是联合判**：① 主体 <0.18 **且** 文字 >0.45 → 「卡片+文字」（这就是 ⑳(a) 那条退化形态，判据只此一处）；② 申报了 `subject_channel` 而屏幕上 `subjects == 0` → 「主体是空壳」（⑦⑯ 与 ⑲-b 之间的缝：代码写了、画面上没有）。只踩①一半的落**提醒**（「构图小气」归闸④ 的 `fill_span` 与正文目标值）。**为什么不在 Python 里重数图元**：`shot_used_channels` 已经要求「import 了还得真用得上」，⑦+⑯ 早就把「代码里一个 roughjs 都没有」拦在烧像素之前——再数一遍就是同一个判据的第二个实现（本仓踩过三次的清单漂移）。⑲-a 只补**没人管的那一格**：屏幕上那个东西到底铺开了多大一片 | 真机探针（`remotion/tests/layout-probe.mjs` 两支新夹具，`ALL PASSED`）：**一个半径 300 的圆** → `subject_share=0.2524`、`subjects=1`（过线，证明 SVG 形状真被量进去）；**一整块渐变底板 + 四处文字、一个形状都没有** → `subject_share=0`、`subjects=0`、`text_share=0.5788`——正是被判据拒掉的那一格（`div` 算进主体的话这一支会报出接近 1 的占比，「卡片+文字」从此绿得非常稳）。离线 25 条（`tests/test_semantic_gates.py`）：① 两侧夹（0.18 放行 / 0.179+文字主导 打回）、联合的另一侧（主体 30%+文字 80% → 绿，单条判会误伤封面镜）、文字正好 0.45 不算主导；② 空壳那一格两向（申报了通道且图元数 0 → 打回；纯文字镜图元数 0 不打回）；③ 出口三态（没有回读 / 这一镜没扫成 / 母版比这条链旧 → **未判**，与「量到 0」分开——`0` 是读数、`None` 才是没量到）；④ `_as_float` 认得出 `True` 是 `int` 子类那个坑 | 判据本体 + 三态 + 单源 + **结构闸**（`div` 不在 `SUBJECT_TAGS` 那 400 字里——这是这条闸的命门，钉住它才不会被后来的人顺手加回去） | 这是三条里**最硬**的一块：不需要任何模型。真像素那一格的两侧读数见左栏 |
| T31 | **⑲-b 盲测通道** | 新 `motion/judge_channel.py`（复用 `secrets.py:22-24` 三件套）+ `server.mjs` 抽 2 个事件峰值帧 + `code_spec.py` 词族比对 | 三步红法：①给 judge 发两张已知帧（一张有鲸鱼图形、一张纯文字），断言它**能区分**——区分不了即判「该模型 vision 不可用」，⑲-b 报 `undetermined`；②构造「⑲-a 过了但盲描述答『只有文字』」的合成回执，断言 ⑲-b 打回并记「主体是空壳」；③盲描述与申报词族无交集时必须红 | 真机 ≥1 支片每镜有 `judgement{gate:"19b"}` 的两栏原文回执；`repeats` ≥3 | 未配/不可用 judge 时**阻断出片并出可读回执**（§9-Q44），绝不静默放行 |
| T32 | **⑳ 退化形态锚定** | **⑳(c) 已落地**（⑳(a)/(b) 不在这里重写，见「为什么」那一栏）：`code_spec.check_degenerate_forms()`（三出口，只实现 ⑳(c)）+ 常数 `DEGEN_MIN_PAIRS` 那一档**没有**——两镜即算，所以不留那个常量；读数由 `FilmLayout.tsx` 的 `gridSignature()`（`DEGEN_GRID=6` 的粗网格位掩码，文字/图元各一份）与 `colorHistogram()`（**DOM 里声明的**颜色，含 `color`；`fill`/`stroke` **只对 SVG 元素收**）报出，`scanned` 增三栏 `grid_text`/`grid_drawn`/`colors`；`summarize_layout` 白名单同步加三栏；`code_render` 在 `mediaops.concat` **之前**调用（与 ⑲-a 同一趟回读、同一个位置），读数随 `degenerate_forms` 带回；`motion_code_nodes` 出一条证据行 + 一个返回键 | **⑳(a)** 不实现——它就是 ⑲-a 那条**联合**判（主体 <0.18 **且** 文字 >0.45），判据只此一处，⑳ 的账读它的结果；**⑳(b)** 不实现——归闸④ 的 `fill_span` 与 T18b/Q88（判在**代码**上，「模板套上去等于没画」）。⑳(c) 的红样：三镜里 s01/s02 画面同构（代码各不相同）**恰好一条**打回，并**并列断言 `check_shot_structure`（闸⑨）对它不报**——两条闸判的不是一件事（⑨ 比代码骨架且要 ≥3 镜定罪，⑳(c) 比画面且两镜即算） | 离线 47 条（`tests/test_semantic_gates.py`，⑲-a 与 ⑳(c) 同住一支）；真机三支夹具在 `remotion/tests/layout-probe.mjs`：**甲/甲′（同一张卡、另一份代码、坐标各偏 4px、元素次序不同）指纹完全相同**（`0xffffffff/0x0`）、**甲/乙（另一个画面）指纹分得开**；golden 集的召回/误拒两数由 T34/T35 接 | 「不好看」被替换成「是不是这三种已记录的坏」；坏形态清单可长大（T34） |
| T33 | **㉑ 动效锚定**（**已落地**） | `code_spec.py`：`MOTION_MAX_ANCHORS=3` / `MOTION_ANCHOR_FORMS` / `MOTION_ANCHOR_TOLERANCE_DIV=3`、`parse_motion_anchor`、`check_motion_anchors`（闸前，挂在 `validate_code_spec` 的整片闸那一段上）、`check_motion_windows`（闸后）、`_norm_shot` 透传 `motion`（**刻意不归一**——归一就会把「写了但认不出」修成「没写」）、schema 加 `motion`（**不进 required**）；`code_render.py`：⑭ 之后、切片进缓存之前调用 ㉑ 并 `raise`，词表提到 `props` 之前一次算两处用，`META_FIELDS` 加 `motion_anchors`/`motion_anchor_frames`，缓存命中那一路按**当前申报**重判；`motion_code_nodes.py`：一条证据行 + `motion_anchor_warnings`/`motion_anchor_unjudged` 两栏；`support.py` 收五个键 | 已落：`tests/test_semantic_gates.py` ㉑ 一节（本文件 47→96 条中的新增部分），含**两个红样**——①`anchors: []`（以及 `kind` 空、写法认不出、锚到本镜没声明的字段、超上限）→ 闸前红；②触发帧离所锚词首帧 18 帧 >k=10 → 闸后红（连同「每一个锚都要在窗口内」那一条）。**真机那一半**（触发帧来自真像素采样、词刻度来自真 TTS，差分布标定 `k=fps//3`）随 §8.1 首轮走，见 §9-Q98 | 每镜每个动效事件都能报「为哪个字段/哪条字幕/哪个节拍动」；无锚即拒。**今天真正判完的只有 `subtitle:` 一条**：`beat:` 这条链没有节拍表、`fields.` 的「画面上首次可读」反推不出来 → 必然落**未判**（未判不是通过） | 「动效有没有意义」→「动效为哪个数据服务」，**后半句可测**；绕法清单见 §9-Q98 |
| T34 | **golden 集采集协议**（不是额外工作） | 新 `remotion/golden/`（`index.json` + `frames/`）；`scripts/harvest_golden.py` 从 §8.1 首轮 12 支的抽帧直接生成待标表；Dockerfile COPY | 断言：`index.json` 非空且四个形态码各至少 1 个 `bad` 样本（首轮由 §8.3 七支反例的第 1/5/6/7 支天然提供） | 标注完成后 ㉒ 能读出符合率/召回率两个数 | 判据基线**来自真实产物**，不是凭空阈值 |
| T35 | **㉒ 判据自洽元闸** | 新 `scripts/check_gate_selfconsistency.py`（同帧重复 N=3、一致率、与人标注符合率、负样本召回）；`code_render.py` 记录 `repeats` | 红法：把 judge 的采样温度改高（或在桩里注入 30% 随机翻转），断言 ㉒ **把该闸降为 `undetermined` 并阻断**，而不是继续报 pass | 连续两轮达阈（§9-Q47）即转常态；每轮验收出 10% 抽检的人工复核清单 | 判断型闸的**错误率本身有数字**，不再有「模型说了算」这一格 |
| T36 | SKILL.md 增三节（**以分段块形式**，不胀默认注入集）+ 字节账改由机器产出。 **落地记录（2026-10-13，T36/T42 同批）**：三节都写成了 `<!-- section: … -->` 块并登记进 frontmatter `sections:`（T36 落地时共八块：`film-knobs` 2,777 / `patch-motion` 365 / `sound-design` 933 / `image-channel` 2,944 / `asset-motion` 874 / `subject-naming` 1,263 / `motion-anchors` 1,460 / `text-only-quota` 819 B；**T57 增两块、T62 再增一块，今天十一块、全文 41,333 B**，见 §5.4 表与增补卷 §12–§13）。**默认注入集 31,762 → 24,223 B（降 23.7%），三节一律不在默认集里**（机器证明：`skill.split_sections` 出的默认集文本里搜不到这三节的标题）。三节正文逐条对着 `code_spec.py` 真有的字段写；三处「合同里有、schema 里没有」的地方**在正文里明写成「今天没有这一栏 / 没有这条判据」**（`subject_terms`、`beat:` 锚的节拍表、纯文字镜「不得连续 3 镜」）——不许把没实现的规矩教成会被拦的规矩（§9-Q106）。**未做的一半**：「§5.4 表由 ㉓ 的 `context_ledger` 自动生成」没做，那张表今天仍是手改 + 两处逐行对账（`test_context_hygiene.py` ⑥）；自动生成要等 §8.1 首轮的真机账，**如实记为未做**。**16 KB 目标仍未达**（实测默认集 24,223 B），欠账与理由见 §5.4.1。 | `SKILL.md` 三节内容（① `subject`/`subject_terms` 怎么写才算「能认出」；② `motion[].anchors` 语法与三种锚；③「纯文字镜在全片 ≤1/3 且不得连续 3 镜」）**必须按 §5.7 第②条写成 `<!-- section: … -->` 块，并登记进 frontmatter `sections:`**；默认注入集（无 motion/无 image 时发的那部分）字节数**不得因这三节增加**。§5.4 表的「现值」列改为从 ㉓ 的 `context_ledger` 自动生成，不再人工填 | 断言（`test_motion_channel.py` 那条常驻 drift 测试）：正文点名的每个字段都在 `code_spec.py` schema 里存在，反向亦然。**再加一条**：把三节之一误挂进默认集 → `context_ledger` 的 skill 块字节超出 §5.4 预算 → ㉓ 必须报红（这条就是「本批不许顶穿自己的预算」的机器证明） | 正文 ↔ schema 双向对账绿；§5.4 表由账自动生成且与手改一致；三节都能被 `preload_skills(sections=…)` 单独取到 | 模型拿到的合同与闸读的合同**同一份**（§1.9 那类污染不再发生），且「加说明」不再天然等于「加常驻」 |

**R8 的验收口径**：不是「闸变多了」，而是 §8.3 的七支反例**全部由闸自己拒掉**，且 ⑲-b/⑳/㉑ 每条都能打印两栏原文回执。这七支里第 5/6/7 三支已分别由 T30/T32/T33 接住（「主体件占比不足但画面非空」→ ⑲-a 的联合判；「代码不同而画面相同」→ ⑳(c)；「动效有先后但不锚数据」→ ㉑ 的前后两半）；⑦ 支的**真机那一半**（真像素触发帧 + 真 TTS 词刻度，以及 `k=fps//3` 的标定）与 ⑲-b（盲测通道，T31）、⑳ 的 golden 召回/误拒两数（T34/T35）一起欠在 §8.1 首轮与 §9-Q98。

### R9 —— 上下文面去堆砌（把「提示词 / skill / 记忆 / tools / MCP 要动态不要堆砌」变成 ㉓）

> 存在前提，如实写：**这六条里有五条今天已经是动态注入**（按链预加载、规划轮物理过滤、条件块守卫、`always:` 缺席、nudge 条件回灌）。R9 不是推翻它们，而是①给它们装上账面与预算（否则下一次改动就能悄悄把它们退化掉），②治剩下四条真的堆着的东西：`<memory>` 全量 dump、`knobs` 段常发、执行轮 53–57 份 schema 整批发、剪辑副链的第二份提示词真相源。每条的落点与改法见 §5.7，这里只给红法与验收。

| ID | 任务 | 落点与判据 | 红法（先证红） | 验收口径 | 备注 |
|---|---|---|---|---|---|
| T37 | **自动字节账**（把 §5.4 从人工表变成产物）**已落地** | `context.py` 出口产 `context_ledger`（§5.7 第①条格式 + 新增 `over_target` 第五栏）：`Section` dataclass（name/text/kind/condition_key/referenced_by/sub_blocks）、`section_bytes`（UTF-8 字节，**不新造计数器**——token 那一把尺仍归 `compress.py`）、`section_name`（裸串按首个标签自动取名）、`tools_box`（工具面）；`planning_section_blocks` 上报 `knobs`/`session_history_rows` 子块；账随 `RunState.qa_parts` 既有通道留档（`{"type": LEDGER_ENTRY_TYPE, …}`，与 `prompt_fingerprint` 同形），`tools` 那一栏由 agent 侧在 `_open_run` 补（**只有那里知道注册了多少**） | 「实现前四目录零命中」已取证（**代码**零命中；`context_ledger` 此前只出现在本指南与 `run_server.py:123` 的一句前瞻注释里），`tests/test_context_hygiene.py` ① 是现代码存在性断言；超长 `USAGE_NOTE` → 请求发不出去、删回预算内即绿（⑤，双向）；⑥ 断值/目标/欠账三表逐行自洽；⑧ 两个规划段出口逐字同源；⑨ **§5.4.1 表与常量表逐行对账**（单改即红） | 每支 §8.1 夹具的逐镜记录里带 `context_ledger`（`test_session_history` 已把 `qa.parts` 的分段期望改成 `指纹/账面/think/tool call/answer` 并抽查账面四栏）；§5.4 表由账自动生成（§5.4.1 落地，真 P95 待 §8.1 首轮） | 账先于闸：没有账，㉓ 只能对常量断言，改一次就失真。**实测三块今天已超「目标」**（§5.4.1 欠账表）——断值因此与目标分家 |
| T38 | **㉓ 预算闸上线 · 已落地** | 断值表 §5.4.1（可执行副本 `SECTION_BUDGET_BYTES`）；`over_budget` 非空 → 抛 `ContextBudgetError`（`context.py` 已实现）。**明令禁止用截断代替报红**（今天 `memory.py:116-121` 的「保尾部」就是这个形状） | 逐块红：把系统提示词、`<skills>` 清单、技能正文各顶过断值一次，断言**三次都红**——`tests/test_context_hygiene.py` ⑩ 已落：三块各走**真产出方**（装配自己的 `system_prompt` / `SkillManifestContextSource` / `plan/skills.py: preload_skills` 的原样返回），样本大小照着 `section_budget(name)` 造（**不写字面字节数**，写死就测不出「表被逐块读」），红的必须是**那一块且只有那一块**（`over_budget == [那一块]`），并双向跑一遍「缩回预算内即绿」；⑤ 另有超长 `USAGE_NOTE`（`memory`）那一块的双向红样。「只红一块说明断值表没被真读」这条已被反证：把 `section_budget` 的前缀规则临时抹掉（`skill_instructions:big` → None），⑩ 当场红（1 条 ✗） | 12 支夹具的 `over_budget` 全为空（**待 §8.1 首轮**）；且有一支被人为顶红的样本留在测试里（已有）。**T40 收尾时先被这条验收标准反咬一口**：拿真白名单一量，`planning_round` 的旧断值（8,192 B，照桩门金样算的）会把正常的视频链规划轮判死 → 已按同一口径重算为 14,976 B，取证与「为什么这不是抬预算」见 §9-Q104 | 阈值来源与调数规则见附录 B；**不许因为「真机报红」而临时抬预算**，只能按 Q55 走（Q104 走的是第②种出路：证明表本身写错了）；**红样必须照表造、且只准红一块**（见 §9-Q101） |
| T39 | **记忆读侧召回 + 去教程 · 已落地** | §5.7 第④条：用起 `memory.py` 那个从未使用的 `query` 形参；`USAGE_NOTE` 344 字 → ≤120 字 | `tests/test_memory.py` 补四支（原表列三支，落地时多出一支）：①40 条写入、本轮话题只关 3 条 → 注入集**恰好**那 3 条、37 条无关的一条不搭车；①b 零命中 → 退回**每类最近几条**且回执写明「改注入最近」（服务端自动生成的「计划卡：跳过…/成片退回重做…」与新一轮诉求常常不同词，纯相关度会把这种**状态**丢掉）；②超限两层分开测：装不下 → 回执行「未注入 N 条 + 最早一条起于哪个分类」，**把上限放开到顶穿 §5.4.1 断值 → ㉓ 抛错、请求不发出**（`over_budget == ["memory"]`）；③`USAGE_NOTE` 字节 ≤120（实测 105 B） | `<memory>` 现值 ≤2.0 KB（**上界 1.8 KB，已达标**，`SECTION_BUDGET_DEBT` 里的 memory 行已还清）；召回命中集与人判断一致——§8.1 首轮抽 10 条人工核（**待 §8.1**） | 写回侧（`plan_retro.py`）未动，本条只动读侧；两处如实偏差登记在 §9-Q102 |
| T40 | **`knobs` 段改条件 + 条件块双向红样 · 已落地** | §5.7 第③条；判据沿用 `plan/support.py:71` 的去前缀节点名匹配，**不新增第二套判据**（判据层拆成 `plan/knobs.py`，只依赖 `support`；`prompt.py` 只负责排版） | ①`tests/test_planning_prompt_golden.py` ⑦ 11 条（冷启动 0 字节 / 命中无旋钮节点整块不发 / 命中 `select_BGM` 只发它且 `subtitle` 不搭车 / `pending` 与 `feedback` 两条通道 / **已产出节点不算命中** / 磁盘模板同判据）+ ⑧ 7 个守卫 × 双向 16 条 + 9 条 golden 逐字重烤（`everything` 2130→2130，说明有 knobs 时输出逐字不变）；② 两支反证：命中过滤换成 `if False:` → **4 ✗**（该关没关）、整段改为永远不发 → **6 ✗**（该开没开），复原后 60 条全绿；③ 同级：`test_plan_gate.py` 158/158（含「打回消息带真键名且不含 `clip`/`session_id`」）、`test_context_hygiene.py` 122/122、`test_plan_layout.py`（包 14 模块、`knobs → support`、`prompt → support+knobs`、无模块超 400 行） | 非 motion 规划轮的 `knobs` 字节=0（**已达标**：真机冷启动 0 B、命中真白名单无开关节点 `asr` 也 0 B）；7 个守卫双向绿（**已达标**） | 这是「参数教程常发」的**最后一条**残留，不是重启 B3。三级取证：宿主 115 支 / 断言 5,235（1 支已知红，Q99）→ 容器 115 支 `rc==0` / 断言 5,174 + `image_parity` **584/584** 与 **430/430** → live 接真 Storyline（白名单 28 节点 / 10 个带开关）：冷启动 0 B、命中 `generate_voiceover` 277 B 且只有它、全命中 9,870 B。**收尾时就地修正了 ㉓**：视频链规划轮 8,982 B 被旧断值 8,192 B 判死 → 重算 14,976 B，见 §9-Q104；语料边界见 §9-Q103 |
| T41 | **执行轮工具面按链裁剪 + MCP 描述瘦身 · 已落地** | §5.7 第⑥条 `registry_for_round(plan_steps)`（新建 `plan/round.py`：`registry_for_round`/`trimmed_nodes`/`scope_lines`/`unbacked_truncations`）；`_round_registry` 的缓存签名 = 主注册表清单 + **计划节点集合**（缺一样就是假命中）；MCP 描述截 `tools/mcp.py compose_description`（`DESC_MAX_BYTES=600`）；`tools` 断值 40,960 随裁剪上线（`context.check_tools_budget`，**只判裁过的那一档**） | ①一支只含 `plan_motion`+`render_motion_code` 的计划：`tools.sent < total_registered` 且差 ≥20（冒烟：33 件里发 4 件、差 29）；②**假命中红样**：同一签名先后两次装配出不同工具面 → 必须被签名区分（签名里去掉节点集合即红）；③MCP 侧 `largest[0].desc_bytes ≤ 600`（口径改动见 §9-Q105）；④承载闸 `unbacked_truncations` 双向：无载体的截断 → 红、有载体 → 空表 | 执行轮 schema 总量 ≤40 KB 且比裁剪前降 ≥25%（**探针已达标**：54,140 B → 14,561 B，降 73%）；被裁节点名仍在「如何请求扩面」小节里可见（`scope_lines` 逐名列出 + 段进账面） | 省的是 token；`ToolCatalog`（`catalog.py:116-276`）**不在本条射程**——它是界面名称单源，不进 prompt。**未跑的一腿**：真机 run 读 `context_ledger.tools`（探针量的是真契约 + 真注册表装配，不是某一条 run 的账），如实记为未判。四条取舍（判据③口径、未裁轮次不判断值、team 不按多 Agent 裁、`patch_video` 反向挂技能）见 §9-Q105 |
| T42 | **两条链提示词真相源合一**（含技能正文分段） | §5.7 第⑤条（`EDITING_SYSTEM_PROMPT` → `prompts/editing_round.md`，纳入 `test_prompt_library.py:88` 既有对账）+ 第②条（`SKILL.md` 分段键 + `preload_skills(sections=)`） **落地记录（2026-10-13，与 T36 同批一次提交）**：①副链真相源合一—— `prompts/editing_round.md`（1,331 B）是磁盘那份，`editing_agent.EDITING_SYSTEM_PROMPT` 退化为**逐字回落副本**，`build_editing_agent(..., prompt_library=…)` 让两条链共用同一个 `PromptLibrary`（含热替换）；对账加在 `tests/test_prompt_library.py` ② 那一节（**没新写测试骨架**，就挂在既有的「内联 ↔ 文件」那条上）。**落地时真红过一次**：内联副本原先把五段拼成一整行，文件是分段的，空白归一后差一个空格即判漂移 → 把内联按 `

` 分段与文件逐字对齐才绿（指纹随之变化，这是对的：它标的就是这份内联文本）。②技能正文按块取——判据在 `plan/skills.py`（`sections_for_plan` / `changed_film_knobs` / `wants_sound_design`），装配在 `skill.py`（`split_sections` / `assemble_sections` / `Skill.assemble`），`execute_plan` 把**卡面**也传进去（只读 `param_options[].default`，因为编译结果看不出用户勾没勾）。③**判据①按字面成立且可反证**：不含 motion 节点的轮次 `sections_for_plan` 返回空表，而那一轮 `skills_hint` 本来也不含这份技能 → `sound-design` 块字节 **0**（`sound-design` 是 T36/T42 新写的正文块 933 B，不是附件；附件 27,884 B 仍走 `load_skill(path=)`，正文里只留一行指针）；判据②：计划里出现 `search_illustration` / `select_illustration` / `imagegen` 之一 → `image-channel` 块必现（实测 2,944 B），且默认集里搜不到它。④**方向性取舍**：`changed_film_knobs` 拿不到卡面（续跑路径）时一律按「动过」算，宁可多发 2.8 KB 也不吞掉「这几件事不用你写代码」的约束。⑤**未跑的一腿**：真机 run 读 `context_ledger` 里技能块的字节这一腿**未跑**，如实记为未判——上面那些数都是离线装配量出来的，不是真 run 的账。 | 改一处文案而另一处不同步 → `:88` 必须红（今天副链不接库，所以这支红不出来——这本身就是现状证据）；分段：不含 motion 的请求里 `sound-design` 块字节必须为 0，含 `image` 通道时 `image-channel` 块必须出现 | 主链/副链共用 `prompt_library`；技能块可按通道单独取；T36 的三节走分段块、默认集字节未涨 | 本条与 T36 同一批改文件，**别分两次提交**（名单/字节账会中途漂） |

**R9 的验收口径**：不是「上下文变小了」，而是——①`context_ledger` 在 12 支夹具里都有产物且 `over_budget` 为空；②T38 的逐块顶预算**三次全红**（证明预算表是被真读的，不是装饰）；③T40/T41 的双向红样都红过（证明「动态」不是只在演示路径上动态）；④今天四条真堆着的东西各降到 §5.4 预算内：`<memory>` ≤2.0 KB、`knobs` 条件化、执行轮 schema ≤40 KB、副链并库。**这四条今天全部处于「没有代码能测」的状态**，这就是 R9 存在的全部理由。

### R10 —— 骨架对照查出的差 + 「skill/tool 面复用而非造轮子」结清（本轮新增，回应「有没有照骨架搭 / 有没有复用」）

> 存在前提，如实写：**骨架 17 项里 12 项本仓已具备**（§1.10 有逐项 file:line），所以 R10 **不是**「照骨架重建」，而是①把查出的 3 处差补掉，②把「复用/自研」这本账在 Agent 侧（不只是 Remotion 侧）结清。编号说明：**T45 已被 R3 占用**（§6 R3 表内），所以本组从 T43 起、跳过 T45。

| ID | 任务 | 落点与解决方法 | 红法（先证红） | 验收口径 | 备注 |
|---|---|---|---|---|---|
| T43 | **`<skills>` 双标签嵌套修掉** | 现状（已实测确认）：`skill.py:290` 自拼 `"<skills>\n…\n</skills>"`，`context.py:253` 又按 `f"<{source.name}>"` 包一次（`source.name="skills"`，`skill.py:280`）→ 外层 `<skills>` 套住的是 `"\n\n".join` 的**整段**结果，即 `<skills><skills>…</skills>＋<skill_instructions name="…">…</skill_instructions></skills>`——**两个毛病一起**：标签重复，且本应与 `<skills>` **同级**的 `always` 正文块被吞成它的子节点。**解决（Q70 已拍，不再二选一）：装配出口改成单一包装规则**——`context.py:250-253` 只对**不以标签开头**的段按名包一层（判据用 `section.lstrip()` 是否以 `<` 起头，不必解析结构）；源侧 `skill.py:287-300` **一行不动**。不选「让源不拼标签」，因为那个源返回的是多标签块，拆它等于把一个 context source 拆成 N 个 | 断言今天必红两支：①渲染串里 `<skills>` **开标签出现 2 次**；②`<skill_instructions>` 的父节点是 `<skills>` 而不是片段根。修完两支都改成「恰好 1 次 / 同级」 | ①`<skills>` 开/闭标签各 1、`always` 正文块层级正确；②§5.4 的 `<skills>` 清单字节按**外层去重后**计（嵌套会让字节数虚高，账面失真）；③`test_skill.py` 现有 `:125-128` 语义不破 | 这条是**体检查出来的真缺陷**，不是风格问题：多套的那层标签会让模型把「清单」和「正文」读成父子关系，也是 §5.4 字节的虚增项 |
| T44 | **MCP 客户端复用官方 SDK + `sse` 声明兑现或删除** | 见 §0.5 P30。**首选：把 `MCPClient` 换成官方 `mcp` 包的薄适配层**（`mcp` 已在 `requirements.txt:6`，自带 stdio/streamableHttp/sse 三传输与握手/协议协商），**必须原样保留本项目已有的三件**：`register_mcp_tools` 的 `enabled_tools` 白名单（`mcp.py:599-602`）、`:583-590` 的身份覆写（模型不许自报 `user_id`/`conversation_id`）、`:544-560` 的「必须显式调用」附注与 `:521 inputSchema→parameters`、`:527-528 readOnlyHint→read_only` 的映射。**退路（仅当迁移把上面三件任一件弄断）**：删掉 `:41` 的 `sse` 取值，对 `type=="sse"` 明确报错「本客户端不支持 sse，请改 streamableHttp」 | ①今天必红：配 `type:"sse"` 后实际发出的仍是 StreamableHttp 的 POST——断言「声明与行为不符」；②迁移后一支 stdio 桩服务跑 `tools/list`→`tools/call` 全链路，断言三件保留项都在（白名单外工具**不可见**、身份由覆写值而非模型入参决定） | 三传输真实可选（或 sse 被明确移除并有报错文案）；用一支临时写入的 `mcp.json`（含一项 stdio 桩）验迁移前后**同样能连上**（注意仓库里今天没有这份文件，见 T46）；离线套件相关用例绿 | **价格按「断掉了哪条既有保证」标**：迁移的唯一硬风险是身份覆写被绕过——那是越权读取他人会话产物的口子。这一条断言不过就不许合（Q62 的退路正是为此留的） |
| T46 | **MCP 配置口结清（两条真相并成一条 + 缺失不再静默）** | 现状两条（`run_server.py:115` 默认 `backend/mcp.json`，该文件**在仓库里不存在**）：①`:540-552` 整段跳过、无回执；②就算文件存在，这段也是**独立一条腿**——直接 `connect_server`+`register_mcp_tools` 挂进本地 `mcp_clients`，**绕开 `McpManager`**，因此该 server 不出现在 `live_servers()`（`mcp_manager.py:52 live_servers()`）里、设置页看不见也关不掉，`mcp_manager.py:7-12` 的三条纪律只保护库里那条。**解决（Q61/Q63）**：`mcp.json` 改为**启动种子**——`load_mcp_config` 后逐项 `McpServersRepo.upsert(name, config, enabled=True, owner_user_id=None)`（`repositories.py:1176-1190`），连接统一交给 `mcp_manager.connect_enabled()`（`:102`）；关断走 `disconnect`（`:89`）；文件缺失时出一行回执「MCP 静态配置不存在（<path>），本次仅 Storyline 一路」；设置页加一个 MCP 区块，与既有的「配额」「检索后端」两区块同一形状、同一读写口径 | 断言今天必红两支：①默认配置下连接数=0 **且日志零命中原因**；②给一个临时 `mcp.json` 起服务，断言 `live_servers()` **不含**该 server（今天必然不含——这正是绕开管理器的那条腿） | ①缺失态有回执一行；②种子项 upsert 后出现在设置页且可关（关断走 `McpManager.disconnect`，工具从主注册表移除）；③页面新增一路 MCP 后不重启即出现在 `context_ledger.tools`（含同名冲突回滚）；④TOML 只留在 Storyline 侧（`examples/storyline/config.toml`，节点白名单），**不把 Agent 侧配置也改成 TOML**（Q61） | 「不造轮子」在这条上的意思是：**复用已有表与已有管理器**，不是新造配置文件格式、也不是维持两条连接腿 |
| T47 | **压缩摘要持久化（不再每轮重算）** | 现状：L3 摘要只活在返回 list（`compress.py:180-184`），下一轮 `context.py:263-267` + `session.py:62-64` 全量重载 → 同一前缀每轮重新总结一次。**解决：摘要随 `head_seq` 落一条**，**复用 `checkpoints`/`checkpoint_entries` 既有通道**（`checkpoint.py:161-205`、`repositories.py:632-640`），不新建表；下一轮 `head_seq` 命中即直接把存好的摘要当前缀，未命中才重算。**首条 system 永不进摘要输入这条不许动**（`compress.py:163-166` 是骨架要求的正确行为） | 断言今天必红：连续两轮同 `head_seq` 调用，`summarizer` 被调用 **2 次**；修完必须 **1 次**（用桩计数，不依赖真模型） | ①同前缀第二轮零次 summarizer 调用；②压缩结果逐字等价于今天（金样：拿 12 支夹具里最长那通会话的一轮，比对改动前后的压缩产物）；③崩溃恢复路径（`run_server.py:521-537`）不因新条目报错 | 省的是**真钱**（每轮一次模型调用），不是优雅度 |
| T48 | **会话历史改窗口拉取 + `<history>` 进账** | 现状：`session.py:62-64` 无 LIMIT 全量拉。长会话下这段既进 token 账也进 ㉓ 的字节账，却**从未在任何预算表里出现过**。**解决：与 T47 同批改成「近 N 条 + 一条持久化摘要前缀」**（N 起步取 §8.1 夹具里最长会话的 P95，附录 B 口径）；`context_ledger` 新增 `name="<history>"` 的块并给它一条预算 | 断言今天必红：构造 400 条历史，`context_ledger.sections` 里**没有** `<history>` 这个块名（现状根本没记账）；且 `count(messages)` 超 `65536` 触发压缩的调用次数 >0 | ①`<history>` 出现在账面且有预算；②同一夹具的 `estimate` 比改前降；③保尾部/丢头部都必须走 `over_budget` 回执而不是静默（同 P24 的纪律） | 这条与 P23/P24 共用「不许用截断代替报红」那一条硬规则，别做成第二个静默截断点 |
| T49 | **「复用 vs 自研」这本账落成测试与清单，不是落成形容词** | 三件各有唯一落点：①**spawn 防递归由注释变断言**——`tests/test_team_wiring.py` 加一支：子注册表 `has("spawn_subagent") is False`（今天只有 `subagent.py:81-86` 的注释在守）；②**四处手写解析器各配一条金样**（`auth.py:34-71` JWT+PBKDF2、`mcp.py:114` YAML 子集、`skill.py:83` frontmatter、`compress.py:31-62` CJK 计数）——**不改行为只锁行为**，§3.6 登记「本仓自持 + 有金样」；③**§3.6 复用底账进测试**：断言 `requirements.txt` 里声明的每个直接依赖都被 import（防「装了不用」），且 `mcp` 包在客户端侧被用到（T44 完成后才绿） | ①今天必红：金样不存在；②`test_requirements_hygiene` 今天必红：`mcp` 声明了但 `agent_framework/**` 里 `from mcp import` **零命中**（只在 `storyline_server` 用）——这正是 P30 的证据 | 三条都绿；§3.6 表逐行有「复用来源 / 自研理由 / 金样测试」三列且无空格 | **「最好复用外部开源而非造轮子」这句要求，落在这三条上，不落在形容词上** |

**R10 的验收口径**：不是「照骨架补齐了」，而是——①§1.10 那 5 项「部分具备」里**归 R10 的三项**全部转成「已具备」或有**明确保留理由的偏差**（P30 的退路、P34 的 await）；②`<skills>` 嵌套这个真缺陷被断言钉住；③MCP 客户端与官方 SDK 的关系作出**二选一决定**并落进 Q62（配置来源与格式落 Q61、缺失回执落 Q63），不留「配了 sse 走的却是别的」这种最坏形状；④压缩与历史两条**每轮重复烧钱**的腿各改一次（T47/T48 同批）；⑤「复用底账」有测试兜着，装了不用的依赖会被报出来（Q69）。

**R10 故意不做的三件事（写清楚，免得下一轮被当成漏）**：不换 APScheduler/celery（PG CAS + lease 已解决多副本双触发，换库只是换个依赖）；不引入 langchain/langgraph/openai-agents/pydantic-ai/autogen（本仓的循环、注册表、并发分批、钩子、checkpoint 都已具备且比框架层更贴这条链的缓存与审批语义；引框架的代价是**同时存在两套循环真相**）；不做 skill→skill 依赖（Q67，`requires` 的现有语义是环境探测，且 6 份正文 `always:`/`requires:` 零命中，加了没有消费者）。

### R-cross —— 跨 R 的常驻化

| ID | 任务 | 说明 |
|---|---|---|
| T19 | 把 §1.5 四条红全转成常驻断言 | 逐文件 rc==0 口径固化进 `scripts/run_tests.py`，并在 README 记录「pytest 收集会 INTERNALERROR，勿单独依赖」；同时**决定**：`tests/test_segment_ids.py:152` 的模块级 `sys.exit` 改为 `if __name__=="__main__"` 守卫（一处小改，让未来 pytest 可用，但不作为本轮阻塞项） |
| T28 | 「未判」不许算过 | **已落地**。病根不是措辞而是**出口数**：闸⑩ 那句「本片这一档未判」原先走的是 `warns`（第二个出口），而提醒在回执与卡面上的读法是「不打回、只是顺口一提」——于是「压根没量过」与「量过、只是不达标」在**每一处读它的人眼里**长得一模一样（D-未判不绿 说的正是这件事）。修法是把三态**分家**：① `check_ink_contrast` 增第三个出口 `undetermined`（返回三元组；`[0]`/`[1]` 的取法不变，三处调用点里只有报账那两处要改）；② `code_spec` 新增**唯一的**整片闸清单 `WHOLE_FILM_GATES`（六道）+ 三态合成 `whole_film_gate_results()`——六道闸原先在**三处各抄一遍**（validate 一份、计划卡一份、出片回执一份，回执那份还漏了 ⑫），这类清单漂移在本仓踩过三次（§9-Q90/Q91）；③ 计划卡与出片回执各出两栏：`style_warnings`（提醒）与 `style_unjudged`（未判），互不混装；④ 回执新增一条**阻断档**证据行 `未判档（阻断：不许折算成通过）`，`status` 恒为 `UNVERIFIED`。红法（`test_code_gates.py` 那条**故意红**改成新语义 + 两支两侧夹：字面颜色的一镜 → 未判栏空；认不出颜色的一镜 → 恰好一条——这一栏不是常量） | `test_code_gates.py` **120/120**、`test_layout_subject.py` **110/110**（那条源码钉改成钉**唯一清单**），**全套件 113 支 0 红**（首次全绿）、**4917 条断言** | P5 的第一条销账：未判与通过不再同格；「清单只有一个」那条纪律顺带落到这一族判据上 |
| T29 | 离线套件基线更新 | 基线 96 文件 → **R0–R9 后 99+**（新增 `test_delivery_hygiene.py`、`test_semantic_gates.py`、`test_context_hygiene.py`〔㉓/账面/T40-T41 双向红样〕）→ **R10 后 100+**（另加 `test_requirements_hygiene.py`〔Q69〕、`test_handrolled_goldens.py`〔四份金样，Q66〕）；断言总数须**只增不减**（历史基线 1176）；跑法固定：容器内 `PYTHONPATH=/app`、逐文件 rc==0 口径 |

---

## 7. 部署收口三件事（顺序即依赖，命令照抄）

> 硬约束：**重烤用 `docker build`，不要用 `compose build`**（compose 注入的 `127.0.0.1:3067` 代理对个别 `.deb` 稳定 502）。运行期容器**无 proxy**，npm 源直连 200。

```bash
# D-1 重建 app 镜像（含 code_render.py / remotion_client.py 的新代码）
docker build -t creation-app:latest .

# D-2 重建 remotion 镜像（含 public/ 与补齐的 COPY）
docker build -t creation-remotion:latest ./remotion

# 重建后立刻做 §0 D-三级取证：容器内 md5 必须等于盘上 md5
docker exec creation-app sh -c 'md5sum /app/storyline_server/motion/code_render.py /app/storyline_server/motion/remotion_client.py'
docker exec creation-remotion sh -c 'md5sum /srv/src/FilmLayout.tsx /srv/server.mjs'

# 上面两条是**点名**的那几件；要一次看全（T21）就用现读 Dockerfile 的逐件对账：
#   清单来自 Dockerfile 本身，不维护表；退出码 0=全对上 / 1=有红 / 2=只未判（未判≠绿）
PYTHONIOENCODING=utf-8 python scripts/image_parity.py \
    --dockerfile Dockerfile --context . --container creation-app
PYTHONIOENCODING=utf-8 python scripts/image_parity.py \
    --dockerfile remotion/Dockerfile --context remotion \
    --container creation-remotion --ignore remotion/.dockerignore
# 2026-10-13 实测：app 575/575；remotion 430/430（外加两栏「不比字节但逐条点名」：
# 构建期重写 1 件 `font-whitelist.json`、运行期改写 1 件 `src/user-shots/shot.tsx`）。
# 注意 D-1/D-2 用的是 `docker build`（见本节的 proxy 硬约束），而 `compose build` 烤出来
# 的镜像 tag 会移动、**已在跑的容器不会跟着重建**——所以「重建了」之后仍要跑这两条，
# 它们判的是**容器里的字节**，不是「我构建过」（§9-Q81/Q86/Q89）。

# 换 Chromium / 改 GL 档 / 进新效果件之前，先跑那支探针（它要容器**活着**才跑得动，
# 所以不在构建期）：
#   · `tests/gl-probe.mjs`     —— 四档 GL 谁能拿到 WebGL2（现档 swangle）
#   · `tests/effects-probe.mjs`—— `@remotion/effects` 那两件的入场券（§9-Q89）
docker exec -e DG_BASE=http://127.0.0.1:8010 creation-remotion \
    sh -c 'cd /srv && node tests/effects-probe.mjs'

# D-3 前端构建（今天 dist 落后 src 约 2 天）
cd frontend && npm run build

# D-4 活库约束放宽（T2 的另一半；SQL 用 base64 传，避免 MSYS 改写 %/$$/'）
docker exec creation-pg sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -c "..."'   # 凭据走容器 env，绝不打印
```

recreate 会清掉容器 `/tmp` 与所有 `docker cp` 的痕迹——**这正是取证必须比 md5 而不是比「我 cp 过」的原因**。

---

## 8. 端到端验收（真机矩阵，跑完才算闭环）

### 8.1 矩阵

| 维度 | 取值 |
|---|---|
| 账号 | 仅 `xuzc`（其他测试身份按 §9-Q31 先查表结构再清） |
| 链 | 「整片改」+「局部改（选区改）」两条 |
| 风格档 | `ink-archive` / `tech-data` / `night-concept` |
| 画幅 | 横屏 + 竖屏 |
| 声音 | 带配音一次；BGM 仅由附件 `material_id` 提供 |

最小集：**2 链 × 3 档 × 2 画幅 = 12 支**，另加「局部改只重烧 1 镜」的缓存断言支。

### 8.2 每支必须留下的证据

1. 闸①–⑱ 逐闸回执（JSON 原文，含 `asset_motion.status`）。
2. `shot_cache_key` 逐镜值 + 改图前后对比（证明「局部改其实整片重烧」那个已修的坑没复现）。
3. **抽帧**：用 ffmpeg 每 12 s 一帧（沿用既有做法），帧文件入库留档，不许凭「渲染返回 200」判定。
4. `materials` / `artifacts` / `render_jobs` 三表该会话的行数与对象前缀清单。
5. 未判/失败镜头清单逐条列出，**不许折叠进「其他通过」**。

### 8.3 「先证红」的固定七支反例

| 反例 | 必须被哪道闸拒 | 今天能不能拒 |
|---|---|---|
| 纯文字 6 镜片 | ⑦ + ⑮ + ⑯ | ✅ 能（配额账） |
| 引用不存在的 demo relpath | ⑫（白名单校验 `code_spec.py:465-476`） | ✅ 能 |
| `assets` 申报 sha16 与字节不符 | ⑬ | ❌ 投递腿还没上链（R1） |
| 全镜动效同帧齐发 | ⑭ | ❌ 静默通过（R5） |
| **画面非空但主体件包围盒 < 0.18**（「卡片+文字」的真实漏法） | **⑲-a** + **⑳(a)** | ✅ **能**（T30 起；闸④ 判它合格——非空且有带内内容，这两条它都满足，所以这一支只能由 ⑲-a 的联合判接住） |
| **代码骨架不同、但画面上是同一张卡**（换写法不换画面） | **⑳(c)** 画面同构 | ✅ **能**（T32 起；真机两支夹具钉住：同一张卡换写法+坐标各偏 4px → 指纹完全相同，另一个画面 → 分得开）。**注意别和闸⑨ 混**：`check_shot_structure`(:1039) 判的是**代码骨架**逐字重合（剥掉文案与颜色后），且 `STRUCT_MIN_GROUP` 要 ≥3 镜才定罪、2 镜只出提醒；它看不见「代码不同而画面相同」——红样里因此并列断言了「⑳(c) 拒而闸⑨ 一条都不报」 |
| **动效确有先后、但不锚任何字段/字幕/节拍** | **㉑** | ✅ **能**（T33 起；⑭ 只数事件、不查锚，所以这一支原先静默通过——这正是 ㉑ 存在的全部理由。红样两条：`anchors: []` 在**闸前**就打回（一次请求都不该发出去），以及像素上数得出 ≥2 段而这一镜**一条都没申报**）。**真机那一半随 §8.1 首轮**（触发帧要真像素、词刻度要真 TTS），见 §9-Q98 |

七支全部**报红即判据成立**；任何一支报了绿，对应的 R 视为未完成。第 5/6/7 支**同时是 golden 集的三个 `bad` 初始样本**（T34）——它们的存在就是这三条判据的理由。

---

## 9. 决定一览（历史上所有开放问题，此处一次性拍板）

| # | 开放问题 | 决定 |
|---|---|---|
| Q1 | 用官方 skill 替换我们的 SKILL.md？ | **否**。官方 skills 无 LICENSE、教联网写法（google-fonts / 远端 URL / Studio），闸① 逐条打死；只采 §3.2 的 6 条写法，做成 ≤500 B 的仅视频链注入块 |
| Q2 | `styles.json` 留不留？ | **删**（T8-1）；三档预设语义并入唯一来源 `palettes.json` 的 `preset` 字段 |
| Q3 | `origin='art'` 与 `origin='library'` 怎么分？ | 两者都要：`library`=策展库进入会话；`art`=出图产物。约束一并放宽（T2） |
| Q4 | 迁移放目录还是文件？ | **文件**（`migrations.sql` 是唯一执行口）；目录废弃，内容并入 |
| Q5 | 全局库为什么不并进 `materials`？ | `materials.owner_user_id NOT NULL REFERENCES users(id)` 装不下全局资产。**不合并** |
| Q6 | `master_hash` 为什么不含资产身份？ | 保持现状：`assets_hash` 在 `shot_cache_key` 内、不进 `master_hash`。母版改动不应作废全部素材缓存 |
| Q7 | 闸⑫ `0.61` 这个数可信吗？ | 已由 `scripts/calibrate_derived_sim.py` 标定（非占位）。**若标定脚本无法在容器内复现该数，闸⑫ 视为未完成**（沿用 §6.1 的纪律句式） |
| Q8 | 「画面语义 / 好不好看 / 有没有意义」这三条要不要留成「无判据」？ | **不留，已转成判据**（R8：⑲-a/⑲-b/⑳/㉑，㉒ 量化其错误率）。措辞规则改成：写「**判据存在，其漏判率是 X**」，不写「没有判据」，也不写「审美已解决」——见 §10。**第四条同类问题（「上下文只能人肉把控」）同样不留**，由 R9 的 ㉓ 接住；措辞规则一致：写「有账、有预算、有双向红样」，不写「上下文已治理完成」 |
| Q9 | 出图后端没配怎么办？ | 明确降级 + 明确回执 + 让 ⑦⑯ 红（T25）。**不许静默出卡片文字片** |
| Q10 | 密钥归谁？ | 归用户，页面/环境配置。文档与代码只验存在与长度，**永不取值** |
| Q11 | 转场用官方包？ | **不用** `@remotion/transitions`（§3.1），转场在拼接层（T22） |
| Q12 | `@remotion/preload` 装不装？ | **不装**（官方自陈渲染不需要；且是历史漂单元凶之一，T17） |
| Q13 | `@remotion/effects` 能不能马上用？ | **先 T16b 一支真渲夹具验色彩 + swangle，过了才进名单**——2026-10-13 过了，但**按子路径逐件进**（`@remotion/effects/paper`、`@remotion/effects/halftone`），桶名 `@remotion/effects` 不进，理由见 §9-Q89 |
| Q14 | 字幕轮子自己写？ | **不**，用 `@remotion/captions`（MIT 零依赖，T23）；显示仍走母版字幕带（不破坏 §4-2） |
| Q84 | 合同 §5.3 把 `subject`/`subject_channel` 画在 `card` **里面**（`card` 是个带 `subject`/`layout`/`motion` 的对象），而本仓 `card` 早就是**字符串**卡 id、表里也没有 `layout` 栏——闸⑮ 的「族」与「取值」到底按什么算？ | **族 = 卡库类别目录名（`card` 写成 `<类别>/<卡名>` 的前半截），取值 = 卡名整串；`subject`/`subject_channel` 落成镜头上的两个平级键**（不改 `card` 的形态，不新造 `layout` 字段）。**为什么只有这一个读法自洽**：⑮ 的两条判据（卡名种类 ≥ `ceil(镜数/4)`、同一族 ≤2 镜）若把「族」读成模型自报的 `layout`，模型换个名字就能凑够数量——而这条闸要拦的恰恰是「每镜一个名字、画面上还是同一张卡」；取成卡库的目录结构，两条判据就各自有白名单撑腰（`_known_card_ids()` 早已把 124 张卡 / 10 个类别当白名单使），且第二条**蕴含**第一条（同族 ≤2 ⇒ n 镜至少 n/2 个族 ≥ `ceil(n/4)`），两条都留不是啰嗦：第一条是族口径万一被放松时还站着的地板。**为什么不新造 `layout`**：新字段没有任何白名单来源，等于给模型开一个纯自报的口（与 ⑦「自报会漂」同一句话）；而且合同原话里那个 `layout` 在实现里**根本不存在**，照抄它等于让判据去读一个永远为空的键——那种闸全绿，因为没人能填错一个不存在的字段。**为什么 `subject` 挂镜头而不是挂 card**：卡是「版式配方」，同一张卡用在两镜上，画的东西与画法都可能不同（`ui-entrance/card-stack` 既可以 rough 画也可以 vector 画）；挂在 card 上会逼出「同一张卡的两次使用写同一个 subject」这种谎。**判据**：`tests/test_layout_subject.py` 的四条单源钉（常量↔技能正文逐字、schema 的 `required`/`enum`/`maxLength`、`style_facts` 的两本账、`plan/support.OUTPUT_FIELD_NAMES`），外加 ⑮ 第二条判据**独立证红**（六张**不同**的卡全挤在 `typography` 族）。**代价（如实记账）**：读合同的人会去找 `card.subject`；已在 `SKILL.md` 的教程段写明两栏与 `card` 平级，并在 `check_subject_named` 的 docstring 里指回本行 |
| Q85 | T15 的验收列写着「⑦ 的通道账按 `subject_channel` 摊」——那 ⑦ 要不要改成读申报的那一栏？ | **不要：⑦ 的账仍从代码字节里算（`shot_all_channels`），`subject_channel` 是**申报侧**的那一栏，逐镜账把两栏并排摆，并由 ⑯ 强制两者一致**（`style_facts["subject_channels"][sid] = {"declared":…, "detected":[…]}`；`declared ∉ detected` 即打回）。**为什么不让 ⑦ 改读申报栏**：⑦ 存在的全部理由是「写 `style: rough` 而代码里一个 `roughjs` 都没有」——把它的输入换成模型自报，就把一条有证据的判据降级成自报，那是**用一个更弱的判据换一句话的顺口**。**「按它摊」的实质要求是「通道账上看得见每一镜的申报与实据」**，而并排摆两栏 + ⑯ 的逐镜对账，让这两栏在**入库的产物里不可能不一致**：要么对上，要么这版分镜压根没入库。**两向都判**也是这一条的一部分：有图形主体的镜**必填**（缺了就是「画了东西却没说用什么画的」），纯文字/纯色块镜**不许填**（填了就是自报无证据，⑦ 给出的那 ≤1/3 额度不是免责条款）。**未判的边界**：⑯ 判不了「画出来的那个东西像不像 `subject` 说的那个」——那是 ⑲-b（盲描述）的活，本行不做承诺；⑯ 判的是「有没有具名、名字有没有证据」，这两件事机器判得动 |
| Q86 | T21 的判据要判「镜像里的字节 == 盘上的字节」——那份**清单**从哪来，人手维护一张表还是现读 `Dockerfile`？remotion 那一侧要不要一起管？ | **现读 `Dockerfile`，绝不维护表；两腿都管**（`scripts/image_parity.py`：`--dockerfile`/`--context`/`--container` 三个参数，同一份脚本跑 `editor` 与 `remotion` 两条容器）。**为什么不能是人手维护的表**：这条判据要拦的正是「盘上改了、镜像没重烤」，而表本身是盘上的一件——它漂了这条判据就退化成自证（与自己比，永远绿）。现读的代价是解析 Dockerfile 的 `FROM`/`COPY`/`ADD`/`WORKDIR`/`RUN`（含续行、`--from=` 阶段引用、`.dockerignore`），换来的是一句话成立：**清单永远等于这一份 Dockerfile 说它该拷的东西**；`tests/test_image_parity.py` ⑨ 里那条「把 `COPY scripts/ /scripts/` 一行抽掉 → `/scripts/*` 立刻从清单里消失」钉的就是这件事。**实测（2026-10-13，这条判据第一次跑就同时抓到两处）**：① `editor` 那份镜像里缺新写的 `/scripts/image_parity.py` 与 `/app/tests/test_image_parity.py`（574 件里缺 2，其余 572 件**逐件 md5 相同**——「其余都没漂」这句话只有逐件比过才说得出口）；② **`creation-remotion` 上重演了一遍 Q81**：`remotion/Dockerfile` 改过之后没重烤，容器里 `font-whitelist.json`、`src/user-shots/shot.tsx`、`tests/motion-probe.mjs` 三件与盘上不一致，而那只容器已经跑了 52 分钟——**症状同样是「长得像对的」**（跑得动、出得来画面，只是出的不是这一版代码的画面）。重烤后两腿分别 **574/574**、**430/430** 对上。**为什么它比 Q81 那句「比一个 ID」强**：ID 相同也可能是**在容器里手改过文件**（`docker cp`/`exec` 写进去的），逐件 md5 把这种也照出来；ID 那句仍留着，因为它在没有 docker 客户端的地方也能念。**退出码三态**：0 全对上 / 1 有红（缺件、内容不一致、该存在的目录不在）/ 2 只未判（Dockerfile 读不到、容器不在）——**未判不许算绿**，与套件口径一致 |
| Q87 | 构建期由 `RUN` **写**出来的文件（如 `remotion/Dockerfile` 第 61 行的 `font-whitelist.json`）本来就与宿主不同——算红、还是直接跳过？ | **都不：落成第三类，逐条点名给人看**（带 `RUN` 行号），不进 md5 对账但**必须出现在输出里**。**为什么不许算红**：那是**假红**，而假红会训练人忽略这条判据（下一次真红就没人看了）。**为什么更不许静默跳过**：静默跳过是**静默少查一件**，正是这条判据最不该出的错——`font-whitelist.json` 这件恰好是闸① 那份白名单的运行时形态，将来谁把它从 RUN 里挪走了，跳过就会让「镜像里那份是哪一版」永远没人问。**所以取向定成「偏松的检测 + 逐条点名」**：多查一件最坏是输出里多一行，漏认一件是少查一件；`--generated NAME` 手工补名同样要在 `RUN` 原文里查得到那个名字，否则报红——**这个口子不能变成「把碍事的文件都写进来」的静默豁免**。**检测形态踩过两个坑（都写进了代码注释，免得有人再放宽一次）**：① 把 `>` 当子串找会命中 JS 箭头函数 `(m) => {`，240 字符的窗口一滑就捞到下一个 `require('./package.json')`——重定向必须是**正则要求 `>` 前有空白**；② 只看「带引号的点号文件名」会把 `require('./package.json')`、`import('./lint.mjs')` 这类**读**误判成写，而 `lint-rules.mjs` 正是好几条用例的被测件——**误判成「构建期重写」的后果是这件静默退出对账**，所以引号那一支必须挂在写调用（`writeFileSync`/`tee`/`cp`…）之后。修掉这两处之后 remotion 那腿的名单正好收敛成一件（`font-whitelist.json`），与「只有这一件是构建期生成的」这个事实相符。**判据**（`tests/test_image_parity.py` ⑪）：① 真被 `writeFileSync` 写的件进名单且带 RUN 行号、不进对账，同一份 Dockerfile 里没被写的那件照旧对账；② 两个假阳样本（箭头函数 `=>`、`require('./x')`）**必须不被挪走**；③ 真重定向 `> out.json` 要认出来；④ `--generated` 补名查不到依据 → 退出码 1；⑤ 真 `remotion/Dockerfile` 的名单**正好一件**（多一件就说明检测又放宽了） |
| Q88 | T18b 的绿写着「母题占比 ≥ 阈值（**沿用闸⑧ 现值**，不新造数）」——而闸⑧ 是版面回读（叠字/悬空/越界三个量），它里面**根本没有**「母题占比」这个数；这个数长在闸④ 的第二半（`fill_span`：`SPAN_MIN`/`TIGHT_MIN`/`FILL_TARGET`）。到底按哪个算？ | **按闸④ 第二半的三个现值算**；合同里那个「闸⑧」是写作时的口径笔误。**为什么不可能是闸⑧**：它那三个量都是**版式**缺陷（两块文字压在一起、引线没落点、文字被切），跟「母题画了多大」没有函数关系；而 `fill_span` 的字面定义就是「内容在带子里铺开了多少」，`SKILL.md` 那张闸表也把它印在 ④ 那一行。**换到 ④ 不损失时机**：两闸都在**烧完之后**（都读产物），T18b 要的「横屏前例不复现」在这一步判得动。**「不新造数」落成**：`tests/test_motif_share.py` 引用的就是这三个常量本身，用例里**一个阈值都没有自己赋值**（§4 用 AST 查模块级赋值语句钉住，不是拿字符串去找——那种自检有自指陷阱，找的字面量就在被找的源码里，恒假）。**本行真正要记的是这次翻出来的边界**：记录在案的那批是 `fill_span` **≈9%~36%**，而 `TIGHT_MIN=0.30` —— 于是**实心块占带短边 31%~44% 这一小段，今天两条硬闸都够不着**（空壳那半要先满足「占比 < 2%」，实心块占比 13% 满足不了）。**为什么 T18b 不顺手补一条线**：①「不新造数」是这一条的明文约束；②补线的正确形状不是再定一个比值，而是 T32 的 ⑳(b)「拿 2026-10-11 那支产物的**实测几何**当锚定负样本」——⑳ 整节的本意就是**锚形态而不是锚形容词**，再划一条比值线恰恰是它要替代的那种做法。**这个边界必须在明面上**：用例 §3(c) 拿记录在案的上限 36% 造了一支实心块，断言它**今天过闸**，标签里直写「已知边界（§9-Q88）」——写成绿而嘴上一句不提，就成了「这个问题已经治好了」的假象。**判据**：横屏 1920×1080 / 带子 1800×732 上，9% 细线条被「几乎空的」接住、25% 实心块被「挤成一团」接住、铺满带子那支过闸，且**同一份字节按整帧量只有 68%**（`code_render.py:249` 那句注释里的数被逐字量出来）——「横屏同理」那句话从此有夹具 |
| Q89 | ① `@remotion/effects` 过了 T16b，是**放行包名**还是**逐件列子路径**？② 镜像逐件对账里`src/user-shots/shot.tsx` 总与盘上不一致——算红、跳过、还是第三种？ |**① 逐件列子路径**（`@remotion/effects/paper`、`@remotion/effects/halftone`），桶名 `@remotion/effects` **不进名单**。这一条是**先验第一步就撞出来的**：夹具原样写 `import { paper } from '@remotion/effects'`，渲染器报「`paper` is not a function」——4.0.534 的桶文件 `dist/index.d.ts` 只重导 11 件（`checkerboard`/`pattern`/`tile`/`rings`/`starburst`/`lightLeak`/`gridlines`/`zigzag`/`linearGradient`/`linearGradientTint`/`cornerPin`），`paper`/`halftone` 与其余约 60 件**只能走子路径**。**为什么不能放行桶名**：那等于同时放行两种坏——那 11 件没经过先验就先得了票；更要命的是**看着像放行了 `paper`、实际拿到 `undefined`**，而这一路上**打包期不报错**，只在渲染时炸一句跟模型写的 import 长得不一样的错（同族：`@remotion/light-leaks` 已被官方指到 `effects`）。白名单只列**先验过的两件**，与「过了才允许进名单」同一句纪律。**② 落成第三栏「运行期改写」**：不比 md5、**逐条点名**、且**仍然判它在**。这一件是服务每次渲染前用 `fs.writeFile` 覆写的镜头代码落点——跑过任何一次渲染的容器里它必然与盘上不同，比出来是**假红**（假红会训练人忽略这条判据）；而静默跳过又是**静默少查一件**（`src/entry.tsx` 静态 import 着 `./user-shots/shot`，这一件缺了连 bundle 都起不来）。名单**不手写**：从最终阶段 `CMD` 点名的那个服务入口（`CMD ["node", "server.mjs"]` → `server.mjs`）源码里现读，只认「`const X = path.join(ROOT, 'a', 'b')` **且** `X` 出现在写调用里」——与 Q87 挂在**写**上是同一条纪律（`ROOT` 家族里 `src/FilmLayout.tsx`、`package-lock.json` 都是只读件，照旧逐件对账）。**顺手翻出来的一件事**：`test_code_spec` ⑤ 那条「子路径指得到 `node_modules` 里的真文件」的判据，只量得动 `katex/dist/katex.min.css` 这种**真文件**子路径；`@remotion/effects/paper` 磁盘上**没有**这个路径（真身是 `dist/paper.mjs`），能不能 import 由包的 `exports` 说了算——拿「是不是真文件」去判它，会把一条**好**条目报成缺。于是判据扩成**两把尺**（真文件 / exports 入口），并且**两把尺都要先红一次**：构造样本 `@remotion/effects/not-an-effect` 与 `katex/dist/nope.css` 必须都算解析不到——**这条自检当场抓到判据偏松**：katex 的 exports 是`"./dist/*": "./dist/*"` 通配，只查「声明了没有」的话 `katex/dist/nope.css` 会被判成「解析得到」，而通配映射的真实语义是**允许在这个前缀上映射**、映到的那个文件还得真在。**判据**（`tests/test_image_parity.py` ⑫、`tests/test_code_spec.py` ⑤）：真被服务写过的件进「运行期改写」并说明依据、同一 `path.join(ROOT, …)` 家族里只被读的那件照旧对账、只出现没被写的常量不算、`unlink` 也算写、真 `remotion/server.mjs` 收敛成**正好一件**、三处名单（`lint-rules.mjs`/`code_spec.py`/`SKILL.md` 44 项）逐项相等 |
| Q90 | ① R3/R6 的 `imagegen` / `search_illustration` / `select_illustration` 类写好了却从没进过 `available_nodes`——算文档问题还是实现缺陷？② 出图产物的「按哈希找字节」行怎么存，`enabled` 取真还是假？③ 真实 provider 那半条腿没有密钥，怎么记？④ 节点文件叫 `imagegen_node.py`（附录 C）还是实现里的 `imagegen_nodes.py`？ | **① 实现缺陷，按缺陷修**。白名单是**唯一的**开关，而且是**两层**同一份：服务端 `build_real_registry(allowed=…)` 过滤节点、客户端 `run_server.py:581` 的 `enabled_tools` 过滤 `tools/list`。缺一项 = 模型手里没有这个工具，而离线用例照样全绿（假绿的典型形态）。修法：三件补进**两份** `available_nodes`——`config.toml` 与 `config.docker.toml`，**真机上 editor 读的是后者**（compose 的 `run_storyline.py --config examples/storyline/config.docker.toml`），只改前者等于没改；并新增两条判据：`tests/test_storyline_server.py`「白名单 − 控制面三件 == 真注册表实际注册的节点」（双向等式，附一条「任意抠掉一个名字都必须红」的敏感性样本——先证红过：把白名单退回旧状态，它精确点名那三件）、`tests/test_video_editing.py`「两份白名单逐项相同 + 各自无重复项」（同样先证红）。修后真机实测：白名单 30 → 注册 28，启动日志 `Storyline 已接入 30 个剪辑节点（DAG 契约 28 节点）`。② **两行都写**：`materials(origin='art')` 回答「这是谁的什么图」（带 owner、用户可见可删），`asset_library(source='imagegen', enabled=False)` 是**全仓唯一**「按内容哈希找字节」的索引、且无归属（§5.2 明写它装不下 `owner_user_id`），镜头侧 `assets:[{name,sha256,ext}]` 的投递腿只认它。`enabled=False` 是**故意的**：`AssetLibraryRepo.search` 只回 `enabled=True`，若为真则**别人**在自己会话里检索时会搜到用户 A 刚生成的图；`get_by_sha` 不看这一列，所以投递照旧能取到字节。③ **记未判，不记通过**（D-未判不绿）。仓里没有任何 `image_*` 密钥；本轮能判的只到「配置口 → 请求体（`model`/`size` 可配，不再写死 `doubao-seedream-5-0-pro`）→ 真取字节 → 落盘 PNG + sha16」，由 `.smoke/t24_imagegen_smoke.py` 拿本地桩后端在真 PG + 真服务上跑通（21 条全过）。真实 provider 那半条腿要用户配 key 后跑一次 `/settings/image/test` 才成立。④ **保留复数的 `imagegen_nodes.py`**：与仓里既有的 `asset_nodes.py` / `core_nodes.py` / `web_nodes.py` 同一命名法（模块导出 `IMAGE_NODE_CLASSES` 列表），附录 C 的单数写法按笔误处理。 |
| Q91 | T25 的「供给优先级策略（写死，不询问）」到底**写死在哪**——编排层强制调用顺序，还是写在模型读得到的地方由它自己走？ | **后者，且是三个面 + 两条闸**。编排层没有（也不该有）图像感知：这一链的分镜轮是模型自己调工具的循环，`plan/*.py` 里今天一行配图相关都没有；「写死」的落点因此是**模型真读得到的那些面**——① `imagegen` 的工具描述（取工具那一刻进上下文）、② `motion_explainer` 技能正文新增的「素材从哪来（供给优先级，写死——**不要问用户**）」一节（分镜轮读的那一份）、③ `imagegen_nodes.py` 模块头（改行为的人先读到它）。**只在①写是不够的**：模型规划到「这一镜要不要配图」时手里没有那句话。两条闸是这条策略的下半句——降级允许、**静默出片不允许**：全文字/色块的六镜片被 ⑦ 打回（「6 镜没有一镜画出图形」+「纯文字镜超标」两条）、每镜不填 `subject` 被 ⑯ 逐镜点名。**判据**（`tests/test_imagegen.py` ⑤，16 条）：判据本体各写成一个具名谓词（`_degrade_is_visible` / `_priority_is_stated`），并各配一支**该被拒样本**先证红——Q27 之前那版「静默回 `ok:True`」的回执、以及只写功能不写优先级的描述，两支都必须被判据拒掉；三个面逐字对上（工具名与「降级为素材库检索」措辞不许只出现在一处）。**顺手翻出来的第二处漂移**：`tests/test_motion_channel.py` ⑦ 的「技能正文点名的工具都在真实来源里」那份来源清单**漏了素材供给三件**（它只拼 `REAL_NODE_CLASSES` + web/motion 那几个），于是正文里**正确**写下的 `search_illustration`/`select_illustration` 被报成「臆造工具」——假告警比不报更坏（它教人忽略这台检查）。修法与名字同构：来源清单按 `build_real_registry` 的组装补全（`*ASSET_NODE_CLASSES, *IMAGE_NODE_CLASSES`），并当场用一支构造样本证红（把正文里一个工具名换成 `search_pictures_v2` → 精确点名它）。**第三处，同一形状、但在装配处**：`run_server.py` 启动期那台一致性检查真机挂着一条假告警「[技能正文] 点名了不存在的工具：`derived_from`」——技能正文讲的是「`shots[]` 里的 `derived_from` 怎么填」，这是**嵌套**键，而装配处收的两份来源都只有顶层（`plan_gate.param_keys()` 按自己的契约只收顶层键名；MCP 工具声明的 `.parameters` 只剩类型，`_signature_for` 翻签名时别的带不过来）；离线用例 ⑦ 把节点 schema **整批**塞进了 `tools`，所以**测试全绿而真机挂告警**。修法：`consistency.skill_field_names` 新增 `node_props` 一路（裸的 `{键: 声明}` 表，与工具对象那一路共用同一把 `walk`，抽成 `props_schema_names`/`_walk_schema`），装配处传 `plan_gate.vocab.props_for(n)`（契约参数那一份，含嵌套字段），用例改传**MCP 形状**的节点声明（`_McpDecl`，只剩顶层类型——拿 `cls.input_schema` 整个塞进去是替被测链路作弊）+ 同一批 `node_props`；并留一条 D-红先：抽掉 `node_props` 必须精确复现 `['derived_from']`。修后真机启动日志：`[startup] 一致性检查通过：提示面与 6 份技能正文点名的工具都在真实的 56 个工具里`。教训与 Q90 ① 同族，且这已是**第三次**：**「实现/字段明明存在，只是某个清单不知道它」——三处都是「两处清单各算各的」**。 |
| Q92 | T22 的两侧在缓存键上口径**相反**：`entrance`/`exit` 进 `FILM_KNOBS`、`transition`/`transition_sec` 不进——凭什么？另外转场时长为什么做成**字符串档位枚举**而不是自由数字？ | **按「谁画进切片」分，不按「是不是整片的开关」分**。进键的判据只有一条：这个开关会不会改动**逐镜切片里的像素或声音**。`entrance`/`exit` 是母版逐帧画进切片的（`FilmLayout.shotMotion()` 按注入的帧数插值），不进键的后果与 `subtitle_mode` 同族——**拿旧切片冒充新片**；`transition`/`transition_sec` 跑在**拼接那一趟**（`mediaops.concat` → `_concat_xfade`），逐镜切片一个字节不动，进键的代价是「整片把淡出换成擦除」重烧 20 镜的 Remotion 帧，而它本来只是一趟拼接。两组各带一条**对照**（改文案必须换键），否则「键没变」可能只是夹具压根没进键；真链上还各有一条落地证据：`entrance` 变了 **0 镜命中缓存**、转场变了**键一字不变**。**档位做成枚举**是被计划卡的开关模型逼出来的、不是审美：卡面每一项都要能反查到取值来源、都要有一句中文标签（`plan/vocab.py::knob_facts`，由 `test_motion_channel.py` ① 断言），自由数字没有候选可列——而这条通道真正的合法性判据要等 TTS 出来才知道（转场不得长过**相邻较短那一镜的一半**），所以合法值本来就只能是**少数几档**，写死四个反而把「0.37 秒配不上短镜」这类错挡在了渲染之前。 |
| Q93 | 母版给每一镜套的整片进场/出场是一层 `transform`，而闸⑧（版面回读）与闸⑪（资产动效）读的都是 `getBoundingClientRect()`——一个「整片轻拉远」的片子会让 ⑧ 把好版式报成「文字被画面带裁掉」、让 ⑪ 把**静态**图判成「参与动效」（它逐字节比两帧的盒）。是让这两条闸「对有进出场的片子不判」，还是把读数折回去？ | **折回去，且只折一次**（`FilmLayout.toCodeSpace`）。母版是这一镜唯一既有 DOM 又有真像素的地方，两条判据都长在这里；让它们对有动作的片子闭眼，等于**最需要被看的那批片子（做了效果的）恰恰没人看**——而它们闭眼的理由是「读数是假的」，假是量法造成的，不是片子造成的。折法：同一帧量一次画面带自己的盒，按它的缩放把每个盒还原成「代码写下的那个位置」（对 translate+scale 精确，与 transform-origin 无关）。**`entrance`/`exit` 四栏的默认值是 `none` 时这一步是恒等变换**——老片子的读数逐字节不变，回归就永远有那一侧可比。**为什么不做成「只比相对几何」**：⑧ 的 `clipped` 判据（越界像素数）与 ⑪ 的两帧逐字节比对都带绝对坐标，相对化会把它们一起改弱。**证红在真 DOM 上**：`remotion/tests/layout-probe.mjs` 末段同一段代码渲两次（静止 / `exit=zoom` 6 帧窗口、探针帧落在动作里），findings 的种类、每个盒的坐标（容差 0.5px）、报出去的带子尺寸与阈值必须**落在同一处**——不折回去，缩放 1.2% 会让每个盒偏出 5px 上下，那条相等立刻红。⑪ 走的是同一个换算，但它要一件真资产才量得出盒，那一半留给 §8.1 真机矩阵。 |
| Q94 | T22 的母版进出场，四条取值该由**镜头代码转交给 `<FilmLayout>`**（像 `text`/`words` 那样），还是母版**自己从渲染入参里取**？ | **自己取**（`getInputProps()`，显式 props 优先只做覆盖口）。这四条不是「镜头代码自己的数据」，而是整片开关决定的母版输入；让镜头代码转交，等于把「整片勾了进出场」这件事交给 20 份现写代码各自抄一行——**漏抄的那一镜只是静默地不演**，而「没演」与「这一镜没选进出场」在成片、在闸④/⑤/⑥ 的读数上**完全一样**（token、版面读数、空壳率一个都不动），没有任何闸能判出来。这条不是推演出来的：探针那一段一开始是**绿的**，把 `toCodeSpace` 的折回整个掐掉**还是绿的**（盒子差 0px）——因为夹具没转交，`exit=zoom` 从没进过母版，判据一直在空转；补上回退后同一支夹具掐掉折回立刻红（**7.9px**）。**顺带翻出来的第二处**：`remotion/tests/layout-probe.mjs` 的夹具从闸① 的两条规则（`dg/must-use-motion`、`dg/no-raw-styled-div`）上线那天起就全数打回（实测 8 支全红、每支都是 `HTTP 400 闸① lint 未通过`）——**整份探针早就死了**，而它的红看着像「版面判据坏了」。修法与 `effects-probe.mjs` 同规（`@dg/motion` 的工具 import 且真用上；纯色底板写成渐变），并另加一条结构判据钉住回退不许被删（`test_code_transition.py` ⑦：`getInputProps` + `resolveShotMotion` 必须在母版里，且组件签名里那四条**不许给默认值**——给了就分不出「没转交」与「转交了 none」）。 |
| Q95 | 闸⑰「旁白在不在」该怎么判——判**音轨存在性**，还是判**电平**？另外这一道该判在拼接之前还是之后？ | **判电平，而且判在拼接之前**。两个都不是推演：**① 判存在性会误报**——旁白**关着**时这条链照样给每一镜垫一条**静音** wav（`narration_for` → `silent_wav`），切片上 `has_audio=True`、实测 **−91 dB**（真机同一个数）。于是「关着旁白却又存在旁白轨」这条判据在真链上**恒真**，一条永远不会响的闸；反过来若按存在性判「开着就必须有轨」，它又会把静音垫片判成合格旁白。两种写法都在**同一件事实上**栽跟头，而静音垫片与真说话（−20~−26 dB）在**时长上一模一样**（真 ffmpeg 量过：2.00s vs 2.00s）——所以只有电平分得开。量不到电平（没有音轨 / `volumedetect` 不吐数）的那一档按 D-未判不绿**记提醒**，不当通过、也不当红。**② 判在拼接之前**：BGM 一短就是整片的错，拼接之后再判等于每一镜的 Remotion 帧白烧一遍、`master.mp4` 白落一次盘；落成「先把三条账算完，账不平就在这里 `raise MediaError`」，红了连一帧都不拼（逐镜 `clip.mp4` 留着，改完 BGM 直接复用缓存）。**③ 回执只摊开不重算**：读数由渲染层随 `av_accounting` 带回来，报告层照抄——重算就会出现「判的时候 0.97、报的时候 0.99」两把尺。**顺带钉住的第三件事**：接缝的字尾账（`d ≤ min(两侧语音尾余量) + 1 帧`），它把 T22 的 `acrossfade` 与旁白接上了——转场把两侧各 d 秒的尾巴交叠淡出，而语音就住在尾巴前面，d 大了就是**话被淡掉半句**，而这件事在画面上一个像素都不动。 |
| Q96 | ⑲-a 该「用 Python 数代码里的主体件图元」还是「读母版量出来的画面占比」？判据该按 `≥0.18` 单条硬判，还是与文字占比**联合**判？`div` 算不算主体件？ | **① 读母版的读数，不在 Python 重数一遍**。计划里写的是「AST 数图元 + 复用闸⑧ 的 DOM 盒」，落地的过程中发现前半句是**重复实现**：`shot_used_channels` 早已要求「import 了还得在代码体里用得上」，⑦ 与 ⑯ 因此早就把「写了 `roughjs` 而一个图元都没画」拦在烧像素之前；在 ⑲-a 里再数一遍，得到的是同一个判断的第二个实现——本仓因为「两处清单各算各的」已经踩过三次（§9-Q90/Q91）。⑲-a 唯一没人管的那一格是**屏幕上那个东西铺开了多大一片**，而那只有 DOM 量得出来，所以判据读母版回传的 `subject_share`/`text_share`，阈值留在 Python（母版只报原始读数——这条链的规矩，两边各写一个 0.18 就会出现两把尺）。**② 联合判，不单条硬判**。单条 `<0.18` 会误伤两类完全合法的镜头：满屏大字配一只小图标的封面镜（文字主导、主体小，但那是设计）、一行字的过渡镜。所以红的是**交集**：主体 <0.18 **且** 文字 >0.45——这就是 P10 与 §2.2-⑳(a) 同写的那条退化形态，判据只落在 ⑲-a 一处，⑳(a) 读它的结果，不许再实现一遍。只踩半边落**提醒**（「构图小气」归闸④ 的 `fill_span` 与正文那句「短边的六成」）。**③ `div` 不算主体件**——这一条是整道闸的**命门**：卡片就是一张涂了底色的 div，把它算进主体，「卡片+文字」会绿得非常稳（真机夹具实测：一整块渐变底板 + 四行字 → `div` 不计入时 `subject_share=0`、`text_share=0.5788`，正是被判据拒掉的那一格）。所以 `SUBJECT_TAGS` 只收 SVG 形状 / `img` / `video` / `canvas`，并由一条**结构闸**钉住 `div` 不在那 400 字里（后人顺手加回去就红）。**④ 顺带补上的一格**：申报了 `subject_channel` 而屏幕上图元数 = 0 → 「主体是空壳」——代码写了不等于画出来了，这是 ⑦⑯（判代码）与 ⑲-b（判认不认得出）之间的一条缝。**⑤ 三态**：回读取不到（没扫成 / 镜像里的母版比这条链旧）记**未判**，与「量到了 0」严格分开——`0` 是读数、`None` 才是没量到，`True` 还要单独挡住（`bool` 是 `int` 子类，顺手转成 1.0 就是「没量到写成量到了」）。 |
| Q97 | ⑳(c) 的「布局盒哈希 + 主色直方图」在实现时该按哪种量？为什么 T32 的三条只落了 (c)？红样怎么保证它没和闸⑨ 撞车？ | **① 用粗网格位掩码，不用精确坐标、也不用像素哈希**。计划里写「像素布局盒哈希 + 主色直方图」，两处都要改口径：**「像素」改成「DOM 里声明的」**——⑳(c) 要回答的是「谁画的、画在哪」，不是「看起来像不像」，按像素算要多跑一趟解码/编码，还会在缩放下漂。**「精确坐标」改成 6×6 粗网格**（`DEGEN_GRID`）：同一张卡换个写法坐标会差几像素，精确坐标会把「同一张卡」判成不同，那条闸就成了闸⑨ 的第二个马甲——这正是 Q51 明令禁止的。真机取证钉住了这件事：甲/甲′同一张卡、另一份代码、**坐标各偏 4px**、元素次序不同 → `grid_text`/`grid_drawn`/`colors` **三者完全相同**；甲/乙另一个画面 → 三者分得开（少了这一句，一个恒定的指纹也能让上一条全绿）。**② 主色必须收 `color`，`fill`/`stroke` 只对 SVG 收**。这张丑卡恰好是「一块底板 + 几行字、一个 SVG 形状都没有」，只收 `background-color`/`fill`/`stroke` 的话这一栏就是**空的**，而空栏在 Python 侧落**未判**——这条闸就恰好在它该响的地方闭嘴。真机实测印证了两件事：不收 `color` → 甲的主色是 `[]`；无差别收 `fill`/`stroke` → 每个普通 `div` 都贡献一次 `rgb(0, 0, 0)`（CSS 里这两个属性的**初值就是黑**），把真颜色挤出前四名。修完甲的主色是 `["rgb(35, 33, 28)", "rgb(0, 0, 0)"]`。**③ 配色比集合不比次序**：频次相近的两个色在两条片子上会互换名次，按位比会把同一张卡判成不同（用例里有一支换次序仍定罪的断言）。集合不同 = 配色不同 → **放行**：「同版式不同配色」的合法系列镜头不该被这条拦住。**④ 有一边认不出颜色 → 这一对落未判，不当「相同」也不当「不同」**：只凭几何撞上就定罪会误伤合法系列镜头，而把「没认出来」写成「不同」又会让闸闭嘴（D-未判不绿）。**⑤ T32 只落 (c) 不是漏做**：⑳(a) 就是 ⑲-a 的联合判（Q96②），⑳(b) 归闸④ 的 `fill_span` 与 T18b/Q88——三条各有一处唯一实现，⑳ 这一节是**账**（回执里 ⑳ 的那一栏点名另两条的读数），不是第二份实现。**⑥ 红样必须成对断言**：三镜里 s01/s02 画面同构（三镜的**代码各不相同**，所以闸⑨ 有话可说的那一档根本不成立），断言 ⑳(c) **恰好一条**打回，同时断言 `check_shot_structure`（闸⑨）对它**一条都不报**——这才叫「两条闸独立」，否则就是 Q51 说的重复实现。 |
| Q98 | ㉑ 动效锚定落地时，判据「触发帧落在所锚数据首次可读窗口 ±k 帧内」在今天的链上真正判得完吗？合同里那条 `card.motion[i].anchors` 在真 schema 里有对应字段吗？申报改了要不要重烧？ | **① 三种锚今天只有一种判得完，另两种如实落未判（D-未判不绿），不许在交付语里被抹成「㉑ 完全挡住了」。** `subtitle:<i>` 的窗口 = 第 i 个词的 `startFrame`（TTS 派生的逐词帧刻度，`_to_frame_words`），**这一条真判**；`fields.<key>` 的「画面上首次可读」**这一层反推不出来**——字段的值从第 0 帧就注进 props 了，它什么时候在屏幕上变得可读是**母版/像素**的事（反推它就是在替 ⑲-b 做模型该做的判断）；`beat:<n>` 这条链**没有节拍检测**，连节拍表都不存在（⑰ 只做时长与电平，不做拍点）。两条都落**未判**并写明理由，不是「通过」。**② 于是「把 motion 锚到必然存在的字段上」这条绕法（Q50 第 2 条）今天只被闸前那一半挡着**：闸前要求该字段必须是**这一镜自己声明过的**（锚到不存在的字段与没锚同罪），窗口那一半挡不住。要判死它，需要「字段值在画面上变可读的那一帧」这个读数，那是 ⑲-b 那一侧（T31）的事——**登记在此，未做**。**③ 两处已知绕法/残差，一并记账**：`a` 申报条数与像素段数不等 → **未判**（段数少一条完全可能是一次动画被像素切成两段，判红会误伤；而把条数写错就永远不进窗口判——这条本身就是绕法）；`b` 容差 `k=fps//3`（@30fps = 10 帧 ≈ 0.33 s）**没有标定过**：附录 B 要求用真渲夹具测「字段可读首帧 vs 事件触发帧」的实际差分布取 95 分位，**标定脚本尚不存在**，所以「3」是一个**有理由的初值**（人的对齐精度按时间走，写死帧数会让 15fps 容差翻倍）而不是标定结果；改 `fps` 时 k 跟着变（它是 fps 的函数，不是写死的帧数）。**④ 合同里的 `card.motion[i].anchors` 在真 schema 里并不存在**（`card` 是一个卡名串），所以落地的是**镜级平铺的 `motion` 栏**——与 Q84 记的 `layout`/`subject_terms` 同族（合同原话里的栏位有三处没有对应实现，这里是第三处）。**它刻意不进 `required`**：缺了 = 「这一镜压根没有动效」（归闸⑭ 按像素判），空锚 = 「有动效却不说为谁动」（归 ㉑ 闸前红）——两句话，压成一句就会把「没动效」变成不合法请求。**⑤ 申报**不进缓存键（`SHOT_KNOBS` 没有它，刻意）：它不改变任何像素，改它不该重烧一帧。代价是命中缓存的那几镜**必须拿当前申报重判一次**——`motion_anchors`/`motion_anchor_frames` 因此进了 `META_FIELDS`（`motion_anchor_frames` 是判据**算不出来的那个输入**：命中缓存的镜不重跑 TTS，没有它就只能把字幕锚全记成「未判」，而那是拿一个假未判冒充真判过）。**⑥ 闸前那一半挂在 `validate_code_spec` 上**（一次请求都不发出去），闸后那一半与 ⑭ 紧挨着、在**切片进缓存之前** `raise`（同 ⑭ 的理由：退回去的一镜不许在缓存里留一份「锚不上任何数据」的切片）。**⑦ 提醒栏今天只有一条**：同一事件挂了重复的锚（`["subtitle:3","subtitle:3"]`）——判据是「**每一个**锚都在窗口内」，重复项在这句话上是零信息，出声但不拦（它没有错，只是这份申报比看上去薄）。**⑧ 真机那一半随 §8.1 首轮**：两个红样已在离线用例里先红过，但「触发帧来自真像素采样、词刻度来自真 TTS」这一层要真渲一镜（连同 ⑳(c)/⑲-a 的几何读数一起，由矩阵那一轮取证）。**⑨ 落地即生效的代价，写在明处**：今天**没有任何一份真机夹具或历史 spec 申报过 `motion`**，而闸⑭ 保证走到 ㉑ 的每一镜都数得出 ≥2 段先后变化——所以 ㉑ 上线后，**旧的 spec 与 `.smoke/` 里那几支代码链夹具会在闸后逐镜报红**（「像素上数得出 N 段先后变化，而这一镜一条动效都没申报」）。这不是误伤，正是这一道要的：这一支原先静默通过，现在被拒。代价是 §8.1 的首轮夹具**必须申报**（每镜 `motion: [{kind, anchors}]`，锚优先打在 `subtitle:<i>` 上——它是今天唯一真判得完的一种），也说明为什么闸前那一半的措辞要把三种写法与「空数组等于这条动效不为任何数据服务」写全：模型改一次就得改对。 |
| Q99 | T33 收尾在**宿主机**跑全套时 `test_delivery_hygiene.py` ⑤ 红了：「声明的 `@remotion` 包许可越界：`@remotion/effects='UNLICENSED'`」。容器里同一份用例是 15/15 加一条**未判**。这是回归吗？该怎么记？ | **不是回归，是宿主与镜像的可见性差**，而且它顺手翻出了一件**真该有人看一眼的事**。① 判据本身没变：它读 `node_modules/<pkg>/package.json` 的 `license` 字段、缺字段就翻随包的许可正文（Q77）。`creation-app` 镜像里 `remotion/node_modules` **压根不在**（remotion 的依赖装在 `creation-remotion` 那份镜像里），所以那一栏在容器内是**未判**并显式打出一行「node_modules 不可达——「已装载 + 许可」两项未验证」——**未判不是绿**，这一格从来没人验过。宿主机上那份 `node_modules` 是可达的，于是判据真的读到了值。② 真正的新信息是**读到的那个值**：宿主机 `@remotion/effects@4.0.534` 的 `license` 字段是 `UNLICENSED`，且包内**没有** `LICENSE.md`/`LICENSE` 正文（`animation-utils` 那种兜底这次兜不住，Q77 的第二层在这里没有输入）。`@remotion/effects/paper` 与 `/halftone` 在两处白名单里（`package.json` 声明 + `lint-rules.mjs` 的静态文件白名单），所以这不是「装了没人用的包」。**记成一条待核实的许可疑点**：要判它得在 `creation-remotion` 容器里把那条判据跑一遍（那份镜像才有 node_modules），而今轮没有做——**不写成「许可没问题」，也不写成「许可有问题」**，就记「这一栏今天在容器侧是未判、在宿主侧读到的字段是 `UNLICENSED`」。③ 宿主那趟的红因此**照实报**（README 的离线回归行里点了名），它不改容器内 5013/0 红那一趟的结论——入口仍是容器内逐文件 `rc==0`。 |
| Q100 | R9-T37 落地时，「§5.4 表末的预算列」能不能直接当 ㉓ 的断值？三块今天已超目标怎么办？`tools`/`knobs` 为什么没有断值？ | **① 不能直接当，断值与目标必须分家，这是实测逼出来的一条。** 照抄那列数，`<skills>` 清单（实测 3,156 B vs 目标 1,536 B）、技能正文（`motion_explainer` 正文 31,186 B vs 16,384 B）、`<memory>`（结构上限折算 13,417 B vs 2,048 B）三块**今天就已经超**——断值照抄等于给每一条真机 run 判死刑（装配期 `raise`，请求一条都发不出去）。所以断值按附录 B 的口径自己算：**固定形状的块用「实测现值 × 1.15」**（§8.1 十二支夹具还没跑，先用现值当 P95 的代理，跑完按真 P95 替换、同批改 §5.4.1 与常量表），**内容由上限决定的块用「结构上限的字节折算」**（`memory`：4000 字正文 + 344 字教程，按全 CJK 3 B/字 ≈ 13.4 KB，拦的是「有人把上限删了」）。三个数（实测现值、断值、目标）在 §5.4.1 逐行列清，口径都写在 `context.py` 的常量旁边。**② 「超目标」必须一直看得见，否则这批账等于没记。** 账面上 `over_budget`（超断值 → 抛错）之外另开 `over_target`（超目标 → 只登记、每次都出现）——没有它，那三块会被 ×1.15 的断值洗成绿色，R9 剩下要还的账就没人记得了。`SECTION_BUDGET_DEBT` 把每个超目标的块挂上「欠哪个任务」：`memory`→T39、`knobs`→T40、`skill_instructions`→T42、`skills`→**描述瘦身（没有任务号，如实记为无主）**（T43 只去重内层 `<skills>` 标签，实测只省 38 B，不是「省字节」那件事）。**③ `tools` 与 `knobs` 今天刻意不设断值，理由各不同。** `tools`：执行轮现在 53~57 份 schema 全量批发（§5.7 第⑥条），先设 40 KB 的断值有可能让真机 run 当场死在 MCP 侧不受我们控制的描述上——闸随 **T41 的裁剪**一起上，今天只记账（`tools` 那一栏由 agent 侧补，装配出口不认识注册表）。`knobs`：现值随白名单长度、**未标定**，标定要等真机首轮的账；它在欠账表里挂着 T40。**④ `planning_round` 给的是 2× 的失控线，不是紧预算。** 全开金样实测 4,834 B，而段内有**用户可控文本**（`feedback_quote`/`pending_asked` 是用户原话，`neutralize` 只防注入不截长），紧值会让「用户写了段长反馈」变成「出不了计划」；真正的紧值要等这一段按子块各自设限（登记在欠账行的理由里）。**⑤ 一笔顺手发现的老账**：`<skills>` 现值 3,156 B 里只有 38 B 是那个嵌套标签缺陷（T43 的射程），剩下的是 6 份 description（合计 1,807 B）——「预算 ≤1.5 KB」这个目标**今天没有任何任务在推**，如实记成无主欠账，不许拿 T43 冒充。**⑥ 两栏的字节口径是 UTF-8 字节，不是 token**：账面要能被人对着表逐字复核（`test_context_hygiene` ② 就是把发出去的 `system_content` 按 `</p>` 切口逐行比 `section_bytes`），token 那一把尺仍归 `compress.py` 的 `estimate_tokens`/`default_token_counter`，**两边分工不重叠**（想省 token 的去看压缩比，想复核账的拿字节数）。**⑦ 三级取证这次真走到了第三腿**：盘上源码 → 重烤镜像后逐件对字节（`image_parity` 583/583）→ **真 PG 往返**——在 `creation-app` 里用一次性身份（`users.register()` 现签，明文 token 不打印、读完即删）跑一轮 `ScriptedLLM`，从 `messages.qa->'parts'` 读回那条 `context_ledger`：五栏键齐、每段字节都是真数字、`over_budget == []`、`tools.sent` 是 agent 侧补的 1，删会话（外键 `ON DELETE CASCADE` 连带 messages）后残留 0 行。**「JSONB 没吃掉字段」这个结论只有真库给得了**，内存替身（`build_storage("memory")`）验不了它。 **⑧ 号段**：本行占的是主合同 §9 的第一个空号 Q100。工作树里那份**未跟踪**的增补卷 `REMOVIDE_ADDENDUM_R11_PARTS_AND_REVIEW.md` 起草时把 Q100–Q108 划给了自己（R11）——两份合同的号按**落地先后**取，增补卷落地时从**当时主合同 §9 的下一个空号**起，**不许同时存在两个同号**；本行内容不动（增补卷首页写的是「只新增、不改动」，改号是顺延不是改动）。 |
| Q101 | R9-T38 的「逐块红样」怎么写才不是自欺？ | **① 样本大小照表造，不写字面字节数。** `plan(budget + 3)`——`budget` 取自 `section_budget(name)`。写死「造 40 KB 的正文」也能红，但那条红在**改断值之后仍然红**，它测的是「有闸」不是「表被读」。**② 只准红一块**：断言 `over_budget == [那一块]`。三块里有两块同时红，说明样本互相污染（多半是填充把别块也顶了），那样的绿/红都归因不了。**③ 走真产出方，不手搓字符串**：`system_prompt` 用装配自己的那一份、`<skills>` 用 `SkillManifestContextSource`、技能正文用 `plan/skills.py: preload_skills` 的原样返回（它自动命名成 `skill_instructions:big`，顺带钉住前缀取预算那条规则真接上了）。手搓一份`<skill_instructions …>` 也能过，但那份样本与执行轮真发的东西没有关系。**④ 必须双向**：红样之外再跑一遍「缩回预算内即绿」——只验红会漏掉「什么都在红」这种失效（真到那一步，每条真机 run 都会死，而测试全绿）。**⑤ 反证留档**：把 `section_budget` 的前缀规则临时抹掉（`skill_instructions:big` 返 None），⑩ 当场 1 条 ✗——这就是「表没被逐块读」的样子，记在这里省得下次有人把前缀规则当冗余删掉。**⑥ 「刚好等于断值」不算超**：判据是 `bytes > budget`，差 1 B 不红是**应有**行为（拦的是越线不是踩线）——造红样时把这一点写进注释，否则下一个人会把「等于也红」当成更严的闸去改。 |
| Q102 | R9-T39 的「未注入 N 条 + 最早一条 <时间戳>」在现有 schema 下怎么写才算真话？零命中的召回该不该什么都不给？ | **① 逐条时间戳不存在，回执只能给到分类粒度。** `memories` 表是 `(user_id, category)` 一行一块文本，`append` 就是把整块拼上换行；没有任何一列记「这条什么时候写的」。T39 明令**只动读侧**（写回侧 `plan_retro.py` 已实现），所以给写侧加时间戳列/给每行加前缀都被排除了。回执因此写的是**表里读得到的真值**：「最早一条在该分类正文开头，该分类最后更新于 <ISO>」——时间来自 `memories.updated_at`（`repositories.py: stamps()`，只读），不是编的，也不假装是「最早那一条的写入时间」。**② 零命中必须兜底，而且要说清是兜底。** 相关度召回最危险的失效不是「多带了」而是「该带的没带」：服务端自动写进记忆的那些行（`计划卡：跳过第 N 步…`、`计划卡：改动…`、`成片退回重做…`）记的是用户在上一版计划上的**动作**，而下一轮用户提的是全新诉求，两者常常没有一个字重合——纯相关度会把这类**状态**判成无关丢掉，于是 `USAGE_NOTE` 里「被跳过的那步默认不再排」变成空话（真机后果：用户跳过的步骤被重新排上）。故零命中时退回**每类最近 top_k 条**（按类取、不是全局取，否则工具教训会被用户偏好挤没），回执写明「关键词未命中，改注入最近几条」——**不许把兜底说成命中**。**③ 判据留三条边界**：同分时短条目优先（同样切题，短的更省预算，能让更多切题条目进来）；注入顺序按时间而不是按分数（记忆读起来该像流水账）；命中计数**去重**（一条把「文案」写十遍不比只写一遍更相关）。**④ 「超限」是两层、不是一层**：召回装不下 → 少带 + 回执行；有人把上限放开到顶穿 §5.4.1 断值 → ㉓ 拦下不发出。两层各有一支红样，只测前一层会漏掉「限额被删」这种失效。 |
| Q103 | R9-T40 的 `knobs` 条件注入，「命中」的语料边界怎么划？为什么「本会话已产出的节点」与「本轮用户消息整段」都不算？冷启动那一轮没有清单怎么办？ | **① 判据只有一套，不新增第二套。** `knobs.bare_node` = 去 `storyline_` 前缀的裸名，与 `vocab.resolve_tool` / `implicit_skills` 同一口径：白名单、卡面、旧卡里都可能带前缀，去前缀再逐字比。**② 语料只认「计划形状」的东西**：`prior[].steps[].node`、`pending.unfulfilled`、`session_history` 的 `candidates[].steps` 与 `audit_unfulfilled`，外加两段**锚在计划上的**用户原话（`feedback`、`pending.asked`）里的节点名 token（`[A-Za-z_][A-Za-z0-9_]*`，去前缀后必须在该轮白名单里）——用户会说「`render_video` 那步别要了」，这里只做词面捞取，不猜意图。**③ 两条刻意的排除（本行就是来记这两条边界的）**：*本会话已产出的节点*不算——那是**产物**不是计划；一个会话跑过一次 `plan_motion`，之后每一轮（哪怕只是问一句时长）都会因此常发整份开关教程，那正是 T40 要治的形态换了个入口（`test_planning_prompt_golden.py` ⑦ 有一支专测它「不算命中」）；*本轮用户消息整段*不算——模型在消息里看到 `plan_motion` 四个字多半是在**问它是什么**，不是要排它，把整段算进来等于第一句就点亮（而第一句通常正点名节点，等于回到常发）。**④ 代价与补偿（冷启动那一轮）**：没有计划上下文时一条都不发，模型若凭印象造 `param_options` 会被校验器 ④ 打回——而**打回消息里此时带上该节点真实的开关键**（`validate._check_params` + `_knobs_keys_hint`），即「谁真要用，谁才拿到清单」。这是 T40 与「把教程删掉」的分界：删了教程又没补偿，就是把「少堆砌」变成「多烧迭代」。**⑤ 真机取证**（`creation-app` 接真 Storyline，白名单 28 节点 / 10 个带开关）：冷启动 0 B；prior 命中 `generate_voiceover` → 277 B **且只有它**（不高频节点不搭车）；命中 `asr`（真白名单、无开关）→ 仍 0 B；全命中 9,870 B。**⑥ 反向（该开没开）也有据**：把命中过滤换成 `if False:` → 4 条 ✗；把整段改成永远不发 → 6 条 ✗；两次都复原后 60 条全绿。 |
| Q104 | R9-T40 收尾时 live 测出 ㉓ 会把**正常**的规划轮判死（视频链 8,982 B > 断值 8,192 B，`ContextBudgetError` → 请求不发出）。改断值算不算「为跑通而抬预算」（Q55 禁止的那件事）？ | **① 现象与取证**（在 `creation-app` 里按 `run_server.py` 的装配方式接真 Storyline：30 工具 / 契约 28 节点 / 白名单 28 个，走真 `ContextBuilder.build` + 真 `Section(name="planning_round", …)`）：冷启动 **2,275 B**（过）、口播链 4,384 B（过）、**视频链 8,982 B ✗**、全命中 13,022 B ✗——后两档当场抛错、请求不发出。视频链 = prior 或待续跑命中 `plan_motion + render_motion_code`（`knobs` 6,121 B），这是本服务的**主打路径**（零素材解说视频）。触发面比「换一版」更宽：`_session_checkpoint_brief` 的 `candidates[].steps` 也算命中，所以**同一会话里只要出过一次这类计划卡，之后每一轮规划都是 8,982 B**。**② 为什么这不是抬预算，而是 Q55 第②种出路（预算表本身写错了）**：断值表自己的口径是「固定形状的块 = 实测现值 × 1.15」（§5.4.1 开头、Q100 ①）。`planning_round` 那格写的「全开金样 4,834 B × 2」里的 4,834 是**桩门**（3 个节点、每节点一个开关）量出来的，真白名单 28 个节点——**错的是输入量，不是「值算对了但今天嫌小」**。反过来说：T40 之前「只要白名单非空就整份发」意味着这一段每轮 ≈12 KB，也就是**每一条真机规划轮都发不出去**，正是 Q100 ① 明令要避免的那种写法，只是当时没人拿真白名单量过。**③ 改法与口径**：现值换成真机实测，并且取**内容上限**——T40 之后这一段是**内容有界**的块（`knobs` 只发命中集，命中集 ⊆ 白名单，无命中整块不发），内容上限是唯一不会把合法轮次判死的值：13,022 × 1.15 ≈ 14,975 → **14,976 B**，同批改两处（`context.py:SECTION_BUDGET_BYTES` 与指南 §5.4.1）。现值口径一并纠正：§5.4.1 与 `SECTION_BUDGET_DEBT` 里 `knobs` 由 **1,829 → 9,870**、`planning_round` 由「金样 4,834」→ **13,022**，两处都是「照桩门量的数被真白名单推翻」。§8.1 十二支夹具跑完后按**真 P95 × 1.15** 收紧（同批改两处）。**④ 代价如实记**：断值放宽到 ~15 KB 之后，㉓ **不再能靠预算闸抓住「`knobs` 又变回整份发」这种退化**（那是 12 KB，落在新线之内）——T40 的回归保护因此在**测试**里（`test_planning_prompt_golden.py` ⑦⑧ 的 60 条 + 两支反证），不在闸里。这条不对称是口径本身带来的：内容有界的块，闸只能拦「内容上限被放开」。**⑤ 新欠账**：`planning_round` 首次进欠账表（目标 5,120 B vs 真机现值 13,022 B）——T40 已让冷启动与口播链进目标，视频链那 6.1 KB 与全命中那 9.9 KB **全是 `knobs` 文案本身**，瘦身**没有任务号**，与 `knobs` 那行同欠；不许拿「条件化」冒充「已瘦身」。**⑥ 号段**：Q103/Q104 占的是主合同 §9 紧接着的两个空号（R9-T40 同日落地）；那份**未跟踪**的增补卷按 Q100 ⑧ 的规则，落地时从**当时**的下一个空号起顺延。 |
| Q105 | R9-T41 落地时有四处不能照字面办：判据③写的是「MCP `largest[0].bytes ≤ 600`」；`tools` 断值上不上、上在哪一档；§5.7 第⑥条括号里的「team 7 件按是否多 Agent 计划决定」要不要做；`patch_video` 原先**明确不挂**技能，现在挂不挂？ | **① 判据③的口径改了，理由是照字面读会把工具砍坏。** `largest[0].bytes` 是**整份 schema** 的字节（T37 定的口径），而分镜类节点的 schema 天然近 2 KB——它的字节由**入参形状**决定，把整份压到 600 B 就等于删参数，模型再也构造不出调用。§5.7 第⑥条要治的是**描述**（那是 Storyline 侧另写的一份抄写），所以判据落成「每份描述的 UTF-8 字节 ≤600」，并给账面每份最大工具补一栏 `desc_bytes`——判据要能**逐份复核**，不能靠「谁敢说它没超」。真机探针（2026-10-13，真契约）：超 600 B 的四件全是剪辑节点（`plan_motion` 1,966 / `patch_motion_code` 2,004 / `render_motion_code` 981 / `patch_video` 1,163）。**② `tools` 断值上，但只判「裁过的那一档」。** 数取 §5.4 的目标 40,960 B（这一栏的目标就是验收口径本身），判法是 `filter_name` 非空才判——规划轮（`planning_registry`）与执行轮（`registry_for_round`）都裁过，都判；**未裁的轮次**（闲聊 / 定时任务 / 没接门的直接调用）仍整批发（探针下限 54,140 B，真机还挂着 memory/skill/cron/team 等十余件，只会更大）。给它们也判等于让闲聊轮全死——那正是 Q100 ③ 当初不敢设这个数的原样。**未裁那一档如实记为未做、无任务号**（要裁它得先回答「闲聊轮该给哪些节点」，那是产品决策，不是预算决策），不许拿「执行轮达标」冒充「工具面达标」。**③ team 那几件不做条件裁剪。** §5.7 第⑥条括号里提到「按是否多 Agent 计划决定」，但**计划里没有这个信号**——会不会派子代理是执行轮跑到中途才知道的事。按计划裁它只会让本来能派的计划先撞一次「本轮不提供」、再走一遍扩面，多一轮的代价换不到可预期的字节数（团队工具合计不到 2 KB）。判据因此只有一条：`tool.contract is not None` 才是剪辑执行节点，其余全是通用件——`load_skill`/`ask_user` 的「永不裁」（Q56）由此变成**结构保证**，不需要第二张名单。**④ `patch_video` 反向挂技能，理由变了。** `vocab._IMPLICIT_SKILLS` 原先写着「口播链的 `patch_video` 不挂技能：指针形状与可改字段白名单全写在 input_schema 里」——那个理由只覆盖**入参形状**，而描述里另有四条**约束散文**（必须带 `base_artifact_id`、指纹不一致就重出片、本工具不改总长也不改顺序、分钟级任务要轮询 `render_status`），它们在 `default_editing_workflow/SKILL.md` 的「# 出片之后要改哪里：局部改（patch_video）」一节里逐条都有、且更细（连 hitmap 对象键与 GET 端点都写了）。描述既然要截到 ≤600 B（第①条），截掉的那几句就**必须有载体**——挂上这份技能同时满足「预注入」与「移出即须有闸读到」（`round.unbacked_truncations` 对无载体的截断判红）。四个被截节点因此**个个有载体**：motion 三件 → `motion_explainer_skill`，`patch_video` → `default_editing_workflow_skill`。**⑤ 本地两件超 600 B，如实不做**：`ask_user` 798 B、`web_search` 643 B 也超线，但 §5.7 第⑥条的射程写的是 MCP（判据③也写「对 MCP 侧成立」），本地那两份是**我们自己**写的文案，砍哪一句是内容决策（`ask_user` 那份讲的是「向用户提问必须走弹窗」的硬纪律）。记为未做、无任务号。**⑥ 未跑的一腿与号段**：执行轮**真机 run** 读 `context_ledger.tools` 这一腿未跑（本轮停在写码），探针量的是真契约 + 真注册表装配、不是某条 run 的账——如实记为未判；`over_budget` 是否为空、真 run 的 `sent/total` 由 §8.1 首轮补。本行占的是主合同 §9 在 Q104 之后的下一个空号（未跟踪的增补卷按 Q100 ⑧ 顺延）。 |
| Q106 | R9-T42/T36 落地时有六处不能照字面办（分段块 + 两条链提示词真相源合一）：①判据里的 `sound-design` **块**在正文里根本不存在（它是**附件** `sound-design.md` 27,884 B，走 `load_skill(path=)`）；②「把 SKILL.md 切成块」需要一个原文里没有的语法：**收尾**；③16,384 B 的目标今天到不了；④`subject_terms` 今天不在 schema 里；⑤「纯文字镜不得连续 3 镜」今天没有判据；⑥§5.4 表「由 ㉓ 自动生成」这一半没做。 | **① `sound-design` 补成真的正文块（933 B），且**只做指针不做副本**：正文写「配乐只从曲库选 / 有旁白不写每镜时长 / 没旁白必给 `min_duration_sec` / 细节读附件 `sound-design.md`」——那一节的判据因此**双向可判**：不含 motion 的轮次 `sections_for_plan` 返回空表、那一轮 `skills_hint` 本来也不含这份技能 → 该块字节 **0**；含 `image` 通道（计划里出现 `search_illustration`/`select_illustration`/`imagegen` 之一）→  `image-channel` 块必现（2,944 B）。**为什么不把附件那 27,884 B 搬进正文**：那就是两份真相（附件改一处、正文不改＝§1.9 那类污染），也会把 16 KB 目标彻底变成一句空话。**② 新增收尾标记 `<!-- /section -->`**：只有起块标记的话块只能一路排到文末——「中间抽掉一段、后面接回默认集」写不出来，而要抽的三段（整片开关 / 资产与图像通道 / 闸⑪）正散在正文中段。收尾之后的行重新算默认集；**不写收尾就等于「一直到下一个标记」**，末尾的块因此不必多写一行。用注释形标记而不用 `# 标题`：拿标题当键会把「改标题措辞」变成「改注入行为」。**③ 16 KB 目标未达，如实记账**：默认集 31,762 → **24,223 B**（降 23.7%），最常见的代码链一轮实发 **28,698 B**（默认集 + T36 三节 + `sound-design`）。为什么不在这一步继续往下切：剩下的 21 KB 是「写代码必须随身的合同」（选卡方法 / 适配 demo / 可用 import / 代码模板 / 一镜七栏 / 十一道闸 / 通道表 / ⑮⑯ / 字体白名单 / TOKENS），每一块都有闸**在真读它**——砍掉不是省字节，是把闸前拦截换成事后打回。目标**仍挂在欠账表里、无任务号**（同 `skills` / `knobs` 那两行），不许拿「分段已落地」冒充「目标已达成」。**顺带换掉了断值的取数口径**：旧值 35,864 = 31,186（那时是整段注入）× 1.15，今天只剩约 500 B 余量，T36 三节一写就会把真机 run 判死；新值 **41,007 = 35,658（默认集 + 八块全给上，即装配真能拼出的最大一份）× 1.15**——口径一个字没改（实测现值 × 1.15），换的是「现值」指哪一份。这不是 Q55 禁止的抬预算：那一条禁的是「值算对了但今天嫌小」。**④ `subject_terms` 不在 schema 里**（⑲-b 的合同栏位，与 Q84/Q98④ 同族「合同里有、schema 里没有」）：T36① 的正文**只教 `subject`/`subject_channel`**，`subject_terms` 那句如实写成「今天没有这一栏、写了会被 schema 退」——把没实现的栏位教成必填，比不教更坏（模型会照着不存在的合同自报）。**⑤「纯文字镜不得连续 3 镜」没有判据**：`check_style_quota` 读的是**配额**（中间镜 ≤1/3，首尾不计入分子也不计入分母），连续与否没有任何闸在读；正文里**明写这一句今天没有判据**，不许教成会被拦的规矩（要收成闸得先回答「连续两镜算不算」「首尾算不算间隔」，那是新闸不是措辞）。**⑥ §5.4 表自动生成未做**：要等 §8.1 首轮的真机账；今天仍是手改 + `test_context_hygiene.py` ⑥ 两处逐行对账，**如实记为未做**。**⑦ 一条落地时真踩到的坑**：内联副本原先五段拼成一整行、文件是分段的，空白归一后差一个空格 → 对账判漂移；修法是把内联按 `

` 分段与文件逐字对齐（`EDITING_PROMPT_FINGERPRINT` 随之变化是对的：它标的就是这份内联文本）。**⑧ 未跑的一腿**：真机 run 读 `context_ledger` 里技能块字节这一腿**未跑**，本文所有字节数都是**离线装配**量出来的（真 `SkillLoader` + 真 `SKILL.md` + 真 `preload_skills`），不是某条 run 的账——如实记为未判。本行占的是主合同 §9 在 Q105 之后的下一个空号。 |

` 切开逐段对字节），而 token 是估算量、会随计数器口径漂；token 那一把尺仍归 `compress.py` 的压缩预算，两边分工不重叠。 |
| Q134 | R10-T43 落地时红法②写的是「`<skill_instructions>` 的父节点是 `<skills>` 而不是片段根」——这条怎么判？为什么不按源拆？ | **① 病根不是「标签重复」，所以判据不能只数标签。** 出口原先按 `source.name` 无条件包一层，而 `SkillManifestContextSource.render` 返回的是**多标签块**（一个 `<skills>` 清单 + N 个 `<skill_instructions name="…">` 正文），于是得到 `<skills><skills>…</skills>＋<skill_instructions>…</skill_instructions></skills>`——重复只是表象，真正的错是**本应与 `<skills>` 同级的正文块被第二个 `<skills>` 吞成了它的子节点**（模型会把「有哪些技能」与「这个技能怎么写」读成父子关系，§5.4 那一栏的字节也因此把同一个东西数了两遍）。所以判据落成两支：`test_skill.py:182` 数**开/闭标签各恰好 1 次**，外加「正文块与清单同级」。**② 改法与 Q70 一致：出口单一包装规则** `context.wrap_source`——**只看首字符是不是 `<`**，不解析结构（认不出标签的段照样按名包，所以这条规则对别的源零影响）；**源侧 `skill.py` 一行不动**，因为把一个 context source 拆成 N 个来换包装规则，是拿一处清楚换两处。**③ 这条只省 38 B**：§5.4 的 `<skills>` 现值 3,156 B 里 1,807 B 是 6 份 description——「≤1.5 KB」那个目标**今天没有任务在推**，如实记成无主欠账（Q100 ⑤），不许拿 T43 冒充。 |
| Q135 | R10-T44/T46 落地：`mcp` SDK 的会话上下文能不能跨任务进出？T46 的「一行回执」与「设置页看得见」分别判在哪一层？ | **① T44 的硬约束是探针量出来的，不是读文档得到的：SDK 的客户端上下文是 anyio 的 cancel scope，必须在**同一个任务里进入并退出**。** 本仓在**装配任务**里连、在 **agent-run 任务**里调 `MCPTool.execute`；跨任务**调用** `session.*` 是安全的，跨任务**退出**会 `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`。所以唯一可行的形状是「**一个长活的所有者任务握着 SDK 上下文（自己进自己出），别的任务只调 `session.*`**」，落成 `_SDKTransport._owner()`：进 `AsyncExitStack` → 连接 → `ClientSession(...)` → `initialize()` → 置 `_ready` → 停在 `await self._stop.wait()`。三传输（stdio / streamableHttp / sse）共用同一条所有者任务，所以 Q62 的**退路乙没有启用**（甲把三件保留项都保住了）。**② 取证在真协议上，不在我们自己的接缝上**：`tests/mcp_stub_server.py` 是一支用官方 `FastMCP` 写的**真** stdio 服务端，`test_mcp.py` 第 7 节在真往返里断言三件保留项（白名单外的工具不可见、身份由覆写值而非模型入参决定、「必须显式调用」附注在）。**③ T46 的「一行回执」判在启动日志，「设置页看得见」判在 `live_servers()`。** 前者落 `run_server.py` 的 `elif mcp_path is not None:`（文件不存在时出一行，且**只有**这一行）；后者落 `test_mcp_dynamic.py` ⑤——它逐字复刻启动期的三步（`load_mcp_config` → 逐项 `upsert(sc.name, sc.to_dict(), enabled=True, owner_user_id=None)` → `connect_enabled()`）后断言种子项**出现在 `live_servers()`**，这正是 P31 的正身：旧代码里 json 那条腿绕开 `McpManager` 直接 connect，于是它的 server 在设置页**看不见也关不掉**。**④ 一条今天仍记未判的腿**：R10-3 的后半句「设置页新增一路 MCP 后**不重启即出现在 `context_ledger.tools`**」要一条真机 run 才读得到，本批停在写码——`tools` 那一栏本来就由 agent 侧补（§5.7 第①条），离线套件只能验到「工具进了主注册表」。按 D-未判不绿记未跑。 |
| Q136 | **偏离项**：R10-T48 的验收（§6 R10 表与 §11 R10-5）写的是「`<history>` … **且有预算上限**」，落地为什么**没有**给它断值？ | **① 挂上去会让长对话在压缩来得及之前当场死，而且是不可恢复的那种。** `_ledger` 对每一行做 `section_budget(name)` 比较，越线就 `ContextBudgetError`、**这次请求不发出**；而 `<history>` 那一行是按 `session.messages` 在**压缩链上游**记的账（`context.py: build()` 的顺序是：先造 `history_row` → `_ledger` → 越线就 `raise` → 之后才轮到 `self.compressor(...)`）。于是「这一轮历史太大」的后果不是「该压缩了」，而是「请求发不出去」——把压缩该管的事写成了㉓该管的事。这**不是**一次性的失败：那条超长的历史留在会话里，之后**每一轮**都同样死在闸上，而错误文案给的出路（「压回预算内，或按 Q55 走流程调预算」）用户一条都走不了——那不是他能改的东西。**② 判据是这条线的**语义**不属于断值表，不是「今天算出来的数偏小」。** §5.4.1 开头写死断值的来源只有两种，都是**代码侧**的量（实测现值 ×1.15 / 结构上限折算），拦的是「有人把上限删了」这类**回归**；而 `<history>` 的体积由**用户自己的对话**决定，是这套系统里最不可控的一个变量。同一张表里已经有过一次同样的判断：`tools` 那一行写着「拿这条线去判未裁的那一档等于让闲聊轮全死」——那里是「能力面本身」，这里是「对话本身」，同一类东西。**③ 落地形状**：`history` **不进** `SECTION_BUDGET_BYTES`（`context.py` 那张表上方留了一段注释写明为什么，免得下一个人看它「没预算」又补上去）；§5.4.1 按 `knobs` 那一行的先例记「**不设断值**」（`test_context_hygiene` ⑨ 对这种行是**反着判**的：文档说不设断值，代码里就**不许**有）；记账照做——`sections` 里有一行 `history`，另有独立回执 `ledger["history"] = {window, dropped, prefix_bytes, bytes, compress_at}`，`compress_at` 是**参考**线，字段名刻意避开 `budget`，免得读回执的人把它当闸。**④ 代价如实记**：这一栏因此**没有**「历史被撑爆时拒发」这道保护，今天只有压缩链自己的 `DEFAULT_MAX_CONTEXT_TOKENS=65536`（同一把尺 1 B ≤ 1 token）在拦，而它拦的方式是**压缩**不是**拒发**。如果哪天要真正的断值，正确的位置在压缩链**下游**（压缩完还是太大才拒发），不是装配出口——那是一次结构改动，不是加一个数。**⑤ 与 R10-5 的关系**：那一行还剩两句今天同样**未判**——「400 条历史的夹具跑出来的 `estimate` 低于改前」（夹具要真模型 / 真 run，离线只验到窗口本身把 `<history>` 的渲染字节从 1,718 降到 410）与「超限走 `over_budget` 回执而非静默截断」（既然不设断值，`over_budget` 这条路径对它**不存在**；替代它的是 `ledger["history"]["dropped"]`——截了多少条、换成哪份摘要，逐轮可读，**不是静默**）。 |
| Q137 | R10-T47/T48/T49 落地时查出的三处实际问题：截掉的条数怎么算才对？依赖清单里那几条「装了没用」的怎么办？T48 的 N 与字节线今天算标定了吗？ | **① 截掉多少条不能写 `最旧那条的 seq − 1`——那会少报一条，且是测试当场抓出来的。** `seq` 在会话里从 1 起连续自增（`append` 取 `max+1`），所以最旧那一条的 seq **就是它前面有多少条 + 1**；而保下来的是「多拉的那 `window+1` 条里最后 `window` 条」，**最旧那条自己也在丢掉之列**，两头一抵消，丢掉的条数正好等于它的 seq（`session.py: _load_history`）。写成 `seq − 1` 时 60 条历史 / 窗口 10 会报 49 而不是 50——回执少报一条与静默丢内容只差一步，所以 `test_session_history.py` 那一支钉的就是这个数（**先写错、再被断言照出来**，不是事后补的注释）。**② 依赖卫生照出 `aiohttp` 是「声明了但零 import」**：出站 HTTP 一律走标准库 urllib（`media_fetch` / `image_backend` / `tools/mcp.py`），`requirements.txt` 里那一条是纯粹占位。删掉它而不是留着「以后可能用」——T49 要的正是这个：把「别造轮子」从一个主张变成**可失败的断言**，装了不用的依赖在闸上红，不是留到下一轮人工体检才有人想起来。**③ 顺手修掉一处 HEAD 里就在的 bug**：`storage/repositories.py` 在 `json.dumps(scale_facts …)`（T54 的代码）处调用 `json` 却没 import，`test_session_gc.py` 一走到那条链就 `NameError`。`git show HEAD:` 确认**不是本批引入**——上一轮落的代码少了一行 import，这次补上，并在提交信息里单独点名（不许混在别的话里悄悄带过）。**④ T48 的 N 与字节线**今天**都是起步值、不是标定值**：`HISTORY_WINDOW=40` 按附录 B 该取「§8.1 十二支夹具里最长会话的历史条数 P95 向上取整」，而**十二支夹具还没跑**；`HISTORY_WINDOW_BYTES=65536` 同理。两个常量旁边都如实写着这一点，跑完按真 P95 替换并**同批**改 §5.4 表末那行与代码常量（Q55 第②种出路）。**不许拿 40 冒充标定过的数**。 |
| Q15 | 字体怎么治？ | `@remotion/fonts` + `staticFile` + Noto CJK woff2 落 `public/fonts/`；`font-family-whitelist` 已有 |
| Q16 | 音频响度有没有官方包？ | **没有**。用官方文档化的 ffmpeg `loudnorm=print_format=json` + `silencedetect` 约定，和 `@remotion/sfx` 的「峰值 −3 dB」约定（仅约定，不取素材） |
| Q17 | `remotion/public` 谁建？ | T4 建，且进 Dockerfile COPY；lint 与闸⑬ 都读它 |
| Q18 | `asset_library` 的 132 行怎么变可用？ | T11 投递口 + T45 检索命中 |
| Q19 | 会话删除会不会误删全局库？ | `OBJECT_PREFIXES` **保持不含** `assets/`，并用 T14 的断言钉住 |
| Q20 | 库素材要不要自动 GC？ | **不做自动删**，只做只读巡检报表（T14）。全局资产一旦被引用计数误判就是不可逆损失 |
| Q21 | 前端全屏/停靠栏等 UI 之争？ | 沿用既有 F 组拍板：右侧停靠栏不做全屏、样式同步要问、音画不许分离只分 BGM/字幕、两条链一次做完。**本文档不重开这些决定** |
| Q22 | `pytest` 修不修？ | 一处小改（`test_segment_ids.py` 加 `__main__` 守卫）；但**验收口径仍是容器内逐文件 rc==0**（T19） |
| Q23 | 未跟踪的 tests/docs/scripts/.smoke 进不进 git？ | **保持现状**：`docs/scripts/tests/.smoke` 是未跟踪工作树目录。**风险记录**：`git checkout` 曾抹掉只存在于工作树的改动；本文档也属未跟踪类，落地前先备份 |
| Q24 | 要不要建 shot_cache 表？ | **不建**。今天缓存是键值函数而非表，先证明需要持久化再建，别为想象中的运维建表 |
| Q25 | 工具数量守卫怎么留？ | 改成「注册表实际大小 + 每个节点可达」（T15b） |
| Q26 | 压缩阈值取 8000 还是 65536？ | **65536**（以代码为准，T15c） |
| Q27 | 无出图 key 时这条链算不算完整？ | 算「检索供给完整、生图供给未启用」；验收 §8 在两种状态下分别跑一次并明写哪一半没验 |
| Q28 | 图像驱动视频算不算「完整功能」？ | 只有 §11 DoD 全绿才写「完整」；在此之前任何交付物措辞只能是「R# 已落地，§8 待跑」 |
| Q29 | 匿名测试身份/悬空素材行/宿主缓存三档脏数据？ | 沿用已记录的 a/b/c 三档：删身份前**先查表结构**（artifacts/render_jobs/checkpoints 与按会话前缀的切片都不认身份），本文档不新增删除动作 |
| Q30 | 知乎/YouTube 取不到素材是缺陷吗？ | 不是。可达性边界是事实表（Q 见 §10-3），代码修不了 403 |
| Q31 | 测试身份保留哪些？ | 只留 `xuzc` |
| Q32 | narration 默认？ | **关**，不生成旁白（§4-7） |
| Q33 | BGM 来源？ | 只认输入框附件 `material_id`，不自动选曲、不预填 `bgm.ref` |
| Q34 | 「局部改」到底改几镜？ | 必须有 §8.2-2 的缓存键前后对比作为证据，否则视为未验 |
| Q35 | `vector` 通道要不要做？ | 已在 `PICTORIAL_CHANNELS`；供给走 `vtracer`（MIT） |
| Q36 | 3D Ken Burns / Depth Pro / FLUX？ | **禁用**（许可或 CUDA 或学术条款） |
| Q37 | demo 语料要不要全量注入？ | 不（§5.4）：只注入 `derived_from` 引用的那几份 |
| Q38 | 提交策略？ | **完整实现最后一次性提交，不 push**（§11） |
| Q39 | 谁先做？ | R0 → R1 → R2/R3 → R4 → R5 → R6 → R7 → **R8** → **R9**（R6 的 T24 可与 R5 并行；R8 的 **T30/T32/T33/T36 可提前与 R4/R5 并行**，因为它们的红样不依赖新产物；**T31/T34/T35 必须等 §8.1 首轮跑完**，golden 集与 judge 回执要真实帧。R9 单独排：**T37 账面必须最先落**——㉓ 的预算断言、§5.4 的自动刷新、T36/T38 的红法都读它；T38-T41 可与 R1/R2 并行（不依赖新产物）；T42 与 T36 同一批，别分两次提交。**R10 排在 R9 之后但可与它并行**：T43（`<skills>` 去重）、T47/T48（摘要持久化 + 历史窗口）、T49（金样与依赖卫生）都**不依赖任何新产物**，红样今天就该红，只等 T37 的账面把 `<history>` 这一块算进去；只有 **T44 需要先备一支 stdio 桩服务**、**T46 需要设置页改动 + 前端重建**（附录 A 的 dist 那条坑同样适用）。T45 属 R3，不在这个排序里另立一档） |
| Q40 | 需不需要再问需求方任何问题？ | **不需要**。本文档即终版；任何后续分歧按 §9 追加行，不重开旧决定 |

### R8 派生的决定（⑲–㉒）

| # | 开放问题 | 决定 |
|---|---|---|
| Q41 | 判断型闸用什么模型通道？ | **复用已配的 judge 三件套**（`secrets.py:22-24`，活库 `app_secrets` 已有 `judge_*` 三行）。不引入新模型、不新增密钥种类、不自建推理服务。密钥值永不读取，只验存在 |
| Q42 | judge 模型不认图怎么办？ | T31 第①步探针：发两张已知帧（有主体图形 / 纯文字），**能区分才算 vision 可用**。不可用 → ⑲-b 报 `undetermined`，**并出「当前 judge 模型不具备视觉能力」这句可读回执**；⑲-a 不受影响，仍照常拒卡片+文字 |
| Q43 | 语义闸能不能只靠模型？ | **不能**。⑲ 必须两条腿：⑲-a（AST+几何，无模型、永远生效、烧像素前判）+ ⑲-b（盲测，烧像素后判）。单腿闸一律视为未完成 |
| Q44 | judge 没配/不可用时还出片吗？ | **不出**。⑲-b 的 `undetermined` 是**阻断档**（同 D-未判不绿）。唯一出路：用户在设置页显式关闭 ⑲-b，页面必须留下「本次以 10% 人工抽检替代」的回执记录。**不许默认关、不许静默放行** |
| Q45 | ⑳ 到底判不判「好看」？ | **不判**。判「是不是这四种已被记录的坏形态」。这条措辞是永久边界：任何文档/汇报把 ⑳ 写成「美观已保障」即为不实。**坏形态清单会长大**（新坏形态经 T34 标注后进入），所以它是**可累积的判据**，不是固定阈值 |
| Q46 | golden 集谁标、什么时候标？ | **不安排单独的标注任务**：§8.1 首轮 12 支的抽帧 + §8.3 的 4 支 `bad` 反例帧就是初始集（T34 的 `harvest_golden.py` 自动出待标表）。此后每轮只按 ㉒ 抽 10% 复核，人工成本随轮次下降 |
| Q47 | ㉒ 的数值门槛是多少？ | 上线门槛：同帧重复 N=3 的**一致率 ≥ 0.90**；与人标注**符合率 ≥ 0.85**；对 `bad:*` 的**召回 ≥ 0.95**（宁可错杀不可放过坏形态）；`good` 帧**误拒 ≤ 0.15**。四个数同时达阈才算该闸可用 |
| Q48 | 闸㉒ 会不会变成「永远红、谁也出不了片」？ | 有退出机制：**连续两轮达阈 → 该闸转常态**（不再每镜重复 3 次，降为抽 1 次 + 每 10 镜复核一次）；不达 → 该闸自身降 `undetermined` 并阻断（不是"当它不存在"） |
| Q49 | 这三条现在算不算「解决」？ | 算「**有机器判据 + 判据错误率是可测数字**」。**不算**「审美/语义已被机器掌握」。§10 按这个口径写，一字不放宽 |
| Q50 | 手搓实现还剩几条绕法？ | 三条已知绕法（沿用既有记录）：①用**结构极简**的图形满足 ⑲-a 的 0.18 占比；②把 motion 锚到**必然存在**的字段上骗过 ㉑；③用**大量小图元**堆出主体占比。对策分别落在：⑳(a) 的文本占比联合判、㉑ 的「锚必须在该镜 fields 里真实可读且首次出现窗内」、⑳(c) 的像素同构。**这仍是绕法清单不是完备证明**——所以 ㉒ 的抽检率不许降到 0 |
| Q51 | ⑳(c) 与闸⑨ 是不是重复实现？ | **不是，也不许合并**：闸⑨ `check_shot_structure`(:1039) 比**代码骨架**（剥字剥色后的标识符/数字/符号序列），≥3 镜才定罪、2 镜只提醒；⑳(c) 比**画面**（布局盒哈希 + 主色直方图），2 镜即算。T32 的红样必须同时断言「⑳(c) 拒而闸⑨ 不报」，否则算重复 |
| Q52 | 新增的这四道闸要不要进 cache_key？ | **不进**。它们判在产物之后/之前，不改变出片内容本身；进键会让「改阈值」变成「全片重烧」，与 §4-1 的缓存身份设计相反。阈值变更只影响后续判定，历史产物按当时回执归档 |

### R9 派生的决定（㉓ 与上下文面）

| # | 开放问题 | 决定 |
|---|---|---|
| Q53 | 「动态注入」这几条机制（按链预加载 / 规划轮物理过滤 / 条件块守卫 / `always:` 缺席 / nudge 条件回灌）要不要重写？ | **不动，一条都不重写**。R9 只给它们装账面。把它们重写成新机制的代价是「断掉已验证的行为」，而收益为零——`test_skill.py:125-128`、`test_planning_prompt_golden.py:83-110`、`test_plan_gate.py:678-687` 已经在守这套语义 |
| Q54 | 记忆召回用什么召回？ | **关键词召回，不上向量库**：查询串 = 本轮用户消息 + 计划步骤名（`plan/support.py:71` 那份节点名，已在账里），对 `memory` 两类的正文做词族重合打分，取 top-k（**k=6 起**，附录 B 给标定法）。理由：新增向量依赖=新增部署耦合，而记忆条目是短句，关键词重合已够判「相关/无关」；重合为零即**不带**（宁缺毋滥，因为带错的记忆会盖过用户当前指令） |
| Q55 | 真机一跑 ㉓ 就红，能不能临时抬预算？ | **不能**。超线只有两种出路：①把该块内容真的删瘦（附一行「删了什么」），②证明预算表本身写错了（须同批改 `test_context_hygiene.py` 的常量和 §5.4 表，两处一起改，单改即红）。**「为了跑通而调大阈值」在这条链上等同于 §1.5 那四条红的老病**——用放宽判据换取绿色 |
| Q56 | 执行轮裁工具会不会让模型「以为功能不存在」？ | 会，如果只裁不说明。**判据固定两条**：被裁掉的节点名必须出现在「如何请求扩面」那一小节（T41-②），且 `load_skill`/`ask_user` 这类发现性工具永不被裁。裁的是 schema 字节，不是可发现性 |
| Q57 | `_bootstrap_sections` 这条没人能打开的通道（`run_server.py:268/395` 传 `None` 且无 flag/env）留不留？ | **二选一，不许留着**：要么给它一个真的配置口，要么删掉函数与调用点。留着「看起来能配但配不了」的形状，正是 §1.8 新列的**第五条**孤儿——它此前没进过任何清单，是 R9 装配面体检时才发现的 |
| Q58 | 两条链的提示词并成一份，会不会互相污染（剪辑链被视频链的闸说明干扰）？ | **并的是文件与对账，不是段落**：`prompts/editing_round.md` 独立成篇，只共享 `test_prompt_library.py:88` 那条「文件↔内联一致」守卫。合并后剪辑链的注入集**不得**含 motion 的闸说明（这条写成断言，不是写成说明） |
| Q59 | ㉓ 的账要不要进 `shot_cache_key`？ | **不进**（同 Q52 的理由）。但**进渲染任务行的留档**：账面是取证材料，不是身份材料。进了键会让「删 200 字教程」变成「全片重烧」 |
| Q60 | ㉓ 有没有把「上下文面」这条残余销掉？ | **没有，也不许销**。㉓ 判得住「哪块涨了多少字节、哪块本请求无人引用、哪个条件块双向失守」，**判不住「这段内容值不值得占常驻」**——后者是人的取舍，且预算表本身是人写的。这条挂在新 §10.6，是本批**唯一没有元闸兜底**的残余；它的处理方式是「删/留每块都要留一行理由」，而不是假装可测 |

### R10 派生的决定（骨架差与复用面）

这一组把 §1.10 的三项「部分具备」和 §3.6 的「别造轮子」这本账一次拍完。每条同样按「断掉了哪条既有保证」定价，不按工程量定价。

| # | 开放问题 | 决定 |
|---|---|---|
| Q61 | MCP / Agent 侧配置用哪种格式、由谁解析？要不要统一成 TOML 或换 PyYAML？ | **两层都不换格式，且连接通道只留一条**：①配置**来源**以 `mcp_servers` 表（`repositories.py:1176-1190`，`McpServersRepo`，带 `owner_user_id` 与 `enabled`）为唯一运行期真相——注意今天实测**并非如此**：`run_server.py:540-552` 对 `mcp.json` 走的是**另一条腿**（直接 `connect_server` + `register_mcp_tools`，挂进本地 `mcp_clients`，**绕开 `McpManager`**），于是文件里那个 server 在设置页 `live_servers()` 里看不见、也关不掉，而库里那条走的是热连热断（`mcp_manager.py:7-12` 的三条纪律只约束后者）。**决定：把 `mcp.json` 降级为「启动种子」**——读出来后 upsert 进 `mcp_servers`（`enabled=true`），连接一律由 `McpManager` 发起；这样同名冲突、失败回滚、热断这三条既有纪律对两种来源**同时生效**，而不是只有一半人受保护。②配置**语法**保持现在的「JSON + YAML 子集」手写解析器（`tools/mcp.py:103-204`，`${ENV}` 自实现 `:96-100`），**不引入 PyYAML**——全量 YAML 会带进锚点、多文档与任意对象构造，那是**新的解析面**，而现在「配不了危险东西」这条既有性质正是由子集本身保证的；③**不把 Agent 侧也改成 TOML**：TOML 归 Storyline 侧的节点白名单（`examples/storyline/config.toml`、`video_editing.py:21`、`settings.py:11`），两侧各用一套格式**不是不一致，是两套语义**——合并只会造出第三套真相 |
| Q62 | 手写 MCP 客户端（`tools/mcp.py:410-477` 自拼 JSON-RPC）要不要换成官方 `mcp` SDK？`sse` 那条声明怎么办？ | **二选一，且必须按次序选**：**先写断言，再动代码**。首选（甲）＝把 `MCPClient` 换成官方 `mcp` 包（`requirements.txt:6`，服务端已在用，无新依赖）的薄适配层，前提是本项目已有的三件**原样保住**：`register_mcp_tools` 的 `enabled_tools` 白名单（`:599-602`）、身份覆写（`:583-590`，模型不许自报 `user_id`/`conversation_id`）、「必须显式调用」附注（`:544-560`）以及 `:521 inputSchema→parameters`、`:527-528 readOnlyHint→read_only` 两个映射。**任一件断不掉才许合**——身份覆写被绕过就是越权读他人会话产物的口子。次选（乙，仅当甲确实弄断三件之一）＝**删掉 `:41` 的 `sse` 取值**并对 `type=="sse"` 明确报错「本客户端不支持 sse，请改 streamableHttp」。不许留的形状是「声明了 sse、实际发的是 StreamableHttp 的 POST」：**声明与行为不符，比缺一个传输更坏**，因为它把配置错误变成静默的运行时行为差异 |
| Q63 | `backend/mcp.json` 根本不存在（`run_server.py:115` 默认指向它、`:540-556` 整段跳过），要不要补一个文件进仓库？ | **不补文件，补回执**。首次部署的 MCP 面为空是**事实**，把它藏进一个仓库里的样例文件只会让「谁生效」变成第二个真相。判据固定：默认配置下 `mcp_manager` 连接数=0 时，启动日志**必须有且只有一行**写明原因（「MCP 静态配置不存在（<path>），本次仅 Storyline 一路」）。红样按今天必红的形态写：连接数=0 **且日志零命中**。静默跳过是缺陷，不是"未启用" |
| Q64 | 压缩摘要每轮重算、会话历史每轮全量重载——这两条要不要一起做成新机制？ | **做成两处局部收口，不引入新机制**：①摘要**持久化**（T47）——按 `head_seq` 存一条摘要行，**复用 `checkpoints`/`checkpoint_entries` 既有通道，不新建表**（「复用」也适用于自己的表），下一轮命中即直接当前缀；判据＝同前缀的第二轮，summarizer 调用次数为 **0**。②历史**窗口拉取**（T48）——`session.py:62-64` 加 LIMIT，近 N 条 + 一条持久化前缀，并把 `<history>` 作为一块进 `context_ledger`、给一条预算（附录 B 的 P95×1.15 口径）。**硬约束一条：`compress.py:163-166`「首条 system 永不进摘要输入」是骨架要求的正确行为，不许顺手"优化"**；真正的病是它的推论（常驻段因此不受 `DEFAULT_MAX_CONTEXT_TOKENS=65536` 约束），那一条由 ㉓ 的账面与预算治，不由压缩治 |
| Q65 | `SpawnTool` 同步 await 子 Agent（`subagent.py:148`），与骨架的「异步派生」不符，要不要改成 fire-and-forget？ | **保持 await**，并把差异如实写进工具描述。改成异步的代价是**断掉「子结论作为工具回执直接进入本轮消息序列、父计划在它回来前不许收尾」**这条既有保证；而骨架真正想要的「异步拆活」本仓**已经有了**——`subagent_manager.py:134-160` 的常驻 WORK/IDLE 型就是异步腿。R10 只做两件事：①描述与行为一致；②给「子注册表里没有 spawn」补一支**断言**（今天只有 `subagent.py:81-86` 的一句注释在守递归派生，注释不构成判据）。**结论：这是「与骨架不同但更强」的两处之一，不许退回** |
| Q66 | 四处手写解析器/加密件（JWT HS256 + PBKDF2 `auth.py:34-71`、YAML 子集 `mcp.py:114`、frontmatter `skill.py:83`、CJK 计数 `compress.py:31-62`）算不算「该复用没复用」的欠账？ | **不算欠账，算自持件，但必须锁行为**：逐条定价后全部不换库——换 pyjwt 会打开**算法协商面**（现实现只认 HS256）；换 bcrypt 要重做历史口令兼容；换 PyYAML 见 Q61；换 tiktoken 会让 `65536` 这条**跨模型**预算变成按模型分词器各算一套（现有阈值断言全部分支化）。取而代之的判据是**每份手写件一条金样回归测试**（不改行为，只锁行为，输入集含边界：无 `Authorization` 头 / 过期 / 错签名、`${ENV}` 未定义、多行 `description`、纯 CJK 与混合串）。**次序纪律**：将来若要换库，必须**先写一支断言证明既有保证没断，再换**，不许反过来用「换了顺便测」 |
| Q67 | `requires` 要不要做成 skill 之间的依赖（骨架示例那种 `requires: [other-skill]`）？ | **不做**。现有 `requires` 语义是**环境探测**（CLI/ENV 在不在，`skill.py:38-49,270-275`），与"skill 依赖 skill"是两种东西，混用会让清单里 `available:` 的含义变成两个。更直接的否证是**没有消费者**：6 份 SKILL.md 的 `always:` 与 `requires:` 均 0 命中。引入第三方 skill 运行库同理——那会把别人家的 `requires` 语义一并搬进来 |
| Q68 | Agent Team 三件套（Task/Message/SubAgent Manager）介质是 PG 表而非骨架示例的 JSON/JSONL、`read_inbox` 标记消费而非删除，要不要改回去？ | **两处都保留，不改回**：①PG 取代文件，理由与 Cron 同源——多副本下文件不是共享真相；②`message_center.py:25-80` 的 `consumed_at` 标记消费（`:5` 自陈取代 `.jsonl`）为的是**刷新可重放 + 可审计**，改回「已读即删」会**断掉这条既有保证**。工具名保持 `list_tasks/claim_task/complete_task`，不追骨架示例的命名。**这是第二处「与骨架不同但更强」** |
| Q69 | 「复用底账」怎么防止变成一句自我表扬？也就是：装了不用的依赖、或者写了不用的自研件，谁能报出来？ | **靠一支常驻测试，不靠表格**：新增 `tests/test_requirements_hygiene.py`（T49），判据两条——①`requirements*.txt` 里每个包必须在 `backend/` 或 `remotion/` 有 import/调用命中，零命中即红（白名单需一行理由，如纯运行期 CLI）；②§3.6 登记的 4 处自持件（MCP 客户端除外，它按 Q62 换成复用）必须各自有金样，缺金样即红。这条把「别造轮子」从一句主张变成一个**可失败的断言**：换库换了一半、旧手写件没删、或者装了个用不上的库，都会在这里红，而不是在下一轮人工体检时才被想起来 |
| Q70 | `<skills>` 被包了两层（`skill.py:290` 自拼标签 + `context.py:253` 按 `source.name` 再包）——改哪一侧？ | **改装配出口的规则，不改源的正文**：定「**单一包装规则**」＝`context.py:250-253` 只对**不带标签**的段按名包一层，自带标签的源不再包。**必须选这一侧**，因为 `SkillManifestContextSource.render`（`skill.py:287-300`）返回的**不是单标签**，而是「一个 `<skills>` 清单 + N 个 `<skill_instructions name="…">` 正文」拼起来的多标签块（`:291` `sections=[manifest]`，`:294-298` 逐条 append，`:300` `"\n\n".join`）——出口按 `source.name="skills"` 再包一层，结果不只是标签重复，还把**本应与 `<skills>` 同级的 `<skill_instructions>` 变成了它的子节点**，语义也一起错。要「让源别拼标签」就得把这个源拆成 N 个 context source，是把一处清楚变成两处。**判据**：成品 prompt 里 `<skills>` 开标签与闭标签**各恰好 1 次**，且 `<skill_instructions>` 与 `<skills>` **同级**（用 DOM 层级断言，不只数字数）；§5.4 的 `<skills>` 字节按外层去重后计。这条今天必红，红法见 T43 |
| Q71 | 闸⑬（资产存在且非纯色）的契约把判据记在「烧之后」，实现却跑在**熔合之前一步**——算偏离吗，改回去吗？ | **算偏离，如实登记，但保留前移**（`remotion/assetgate.mjs:84-85` 就在这处写着「契约把⑬ 记在『烧之后』，判据本身不依赖烧，故前移；这一处偏差登记在 §9」）。**不改回去**：闸⑬ 判的是**投递腿落盘的字节**——字节在 `/stage-assets` 那一刻就落在 `public/assets/<sid>/` 上，此后渲染不再动它，所以熔合前读到的与烧完读到的**是同一批字节**（D-三级取证 第②级「盘上 md5 ≡ 容器里 md5」这条断言仍成立），而一次拒掉能省下**一整镜**的烧渲染。契约写的「烧之后」是**时序描述**，不是判据的构成部分；把判据挂在它并不依赖的时序上只会白烧一镜。**验收口径**：⑬ 的三态（`rejected`/`undetermined`/`ok`）与闸⑪ 同形不变、`D-未判不绿` 不变、空 manifest＋闸① 已过 = `ok`（不是 `undetermined`）不变 |
| Q72 | Q7 的闸⑫ 阈值标定（`scripts/calibrate_derived_sim.py`）拿 **demo↔demo** 当负样本，而不是「真手搓过的镜头代码」——这个数还信得过吗？ | **信得过，且偏差方向是单侧的、对我们有利**（`scripts/calibrate_derived_sim.py:31-35` 写着「一处如实登记的偏差（进 §9）」）。离线脚本**复现不了**「真生成过的镜头代码」——那种东西只存在于跑过的会话里、随会话状态而变。用 demo↔demo 顶替它，偏差方向是**单一**的：真手搓的代码与任何 demo 的骨架相似度**通常低于**两份 demo 之间，因此真实场景下负样本通过率只会**更低**、不会更高——也就是说这份标定把「负样本漏过」这件事**高估**了，0.61 在真机上不会比标定的 2% 更松。**这条偏差只会让闸⑫ 更保守**（宁可把声明造假的镜打回），与「阈值越高越误伤真派生镜、越低越放过造假」这个取舍方向一致地压在安全侧。**已满足的判据（Q7）**：容器内可复现——`python scripts/calibrate_derived_sim.py` 退出 0、选点 0.61、`agree`；容器实测 1.690%@0.61、2.400%@0.60，分位点 median 0.400 / p98 0.605 / max 0.736；Windows（`core.autocrlf=true`，检出 CRLF）跑同一脚本为 1.685%，**分位点一字不差**——发布口径认容器那一组数 |
| Q73 | T20「取材轮」在「`plan_motion` 一次调用同时收 `code` 与 `derived_from`」这个约束下怎么落地？ | **declare-first 两轮调用，不改工具签名**（`nodes/motion_plan.py`＋`code_spec.py::forage_scan`/`collect_demos`）：①**取材轮**——骨架 spec（每镜带 `card` 与 `derived_from`，**一镜都不带 `code`**），节点把这些 relpath 对着 demo 白名单核一遍、把每份被引 demo 的**全文**回给模型，**不入库**（`__no_store__`，先例见 `nodes/core_nodes.py:2651-2655` 的 render dry_run）；②**分镜轮**——每镜都带 `code`，此时模型手上已有 demo 全文，闸⑫ 再比骨架形状。**为什么不做成「一次注入」**：`plan_motion` 的入参把 `code` 与 `derived_from` 放在同一次调用里，工具**没有**「先给 demo 再写码」的一次机会；硬捏一个「边写边取」的中间态，只会让「未判」出现在一个本来就判不了的时刻。**混形（有的镜带 code、有的不带）一律报错**，并明说「这两种意图要各调一次」——服务端分不出「漏填」与「想先取材」。**两处措辞修正一并登记**：①`derived_from` 的语料 relpath 是**相对 `demos/`** 的（如 `opening/fracture/Fracture.tsx`），而 schema 例子与 SKILL.md 一度写成 `demos/…`——现由 `_norm_demo_relpath`（`code_spec.py:467-472`）两种拼法都收、统一存一种，且 SKILL.md 例子已改正；②SKILL.md 的 `assets` 键说明一度写成 `{relpath, sha256, bytes}`，与机器实际吐出的 `{name, sha256, ext}` 不符，已改。**超限不截断**：`FORAGE_MAX_BYTES=120000`，超限文件**既不进本轮、也绝不截断**，而是点名（`forage_oversize`）让模型走 `load_skill(name="motion_explainer_skill", path="<relpath>")` 单取（该路径经真 `SkillLoader` 在最大一份 demo 上和 SKILL.md 正文末行上验证过**无截断**）；第一份文件**无论多大都先给**。**验收**：13 支扰动红样全红后又全绿（D-红先），`test_code_forage.py` 54/54、D-三级取证：盘上 md5 ≡ 镜像内 md5 |
| Q74 | `demos/MANIFEST.json` 里那个 `bytes` 数在工作区上对不上（158/158 全差一个行数）——是清单错了还是语料错了，怎么处置？ | **两边都没错，错在「行尾没有被钉死」**。实测定性：158 份 entry 的 `bytes` **逐份**等于该文件**把 CRLF 归一成 LF 之后**的长度，也**逐份**不等于盘上（CRLF）的长度；`scripts/build_skill_demos.py` 是 `read_text→encode("utf-8")` 记数（文本模式天然归一），而 `dst_file.write_text` 在 Windows 上又把 `\n` 翻回 `\r\n`——于是「写出的字节」与「记下的字节」差了正好一个行数，`docker build` 再从工作区原样取字节，把 CRLF 一并打进了镜像（已实测：容器内 `_fixtures/Brand.tsx` 也是 6293、与宿主 md5 相同）。**处置＝钉死 LF，不是重算清单**：新增仓根 `.gitattributes`，作用域只落在有字节被记录的这份语料（`backend/examples/skills/motion_explainer/demos/** text eol=lf`），并把 `build_skill_demos.py` 的写出改成 `newline="\n"`；此后盘上、镜像里、清单里三处的字节相同（重跑校验：`mismatch 0`，`git ls-files --eol` 从 `i/lf w/crlf` 变 `w/lf`，且 `git diff HEAD` 为空——**只是行尾被立了规矩，没有一个字节的内容被改**）。**为什么不改成「记盘上的原始字节」**：那会让清单随检出的平台漂移（Windows 一套数、Linux 另一套），把一份本该是常量的元数据做成环境相关量。**判据**：`for m in MANIFEST: len(磁盘字节) == m["bytes"]` 恒真；`collect_demos` 报给模型的 `bytes` 是**调用时实测**的（不是抄清单），所以即便将来行尾再被人弄乱，回执也仍然是真话 |
| Q75 | 治第六根因要「woff2 落 `public/fonts/`」，可 CJK 网络字体**没有单文件形态**——上游按 unicode-range 切了 101 片，收还是不收？ | **照上游的切法全收**（`scripts/fetch_fonts.mjs` 生成，101 片共 **4.50 MB**，`NotoSansSC-000.woff2`…`NotoSansSC-100.woff2`）。上游事实：`fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;700` 会回 **202 个 `@font-face` / 101 个不同的 woff2 URL**（一个 wght 可变字体按 unicode-range 切片；400 与 700 共用同一份字节，所以是 101 不是 202）。**为什么不自己子集化**：`text=` 子集只含点名的那几个字（别的字**照样静默回落**，等于把要治的病再造一遍），而本机没有 fonttools/brotli，转出来的字节与上游不再可比对。**为什么 4.5 MB 可以接受**：浏览器的用法本来就是只下它真用到的那几片，`loadFont` 的 101 次注册在本地 `staticFile` 上是毫秒级；镜像 2.29 GB 面前这 4.5 MB 不是问题，而「字形不全」是**不可见**的病。**判据（三条，缺一条就不算闭环）**：①`node scripts/fetch_fonts.mjs --check` 校验「切片表 ↔ 盘上字节 ↔ wOF2 魔数」逐项对上（离线、可重复）；②`remotion/tests/gate1-lint.mjs` 的 `dg/font-load-check` 判 `loadFont` 指的字节在 `public/fonts/` 里真存在、format 与后缀一致、且**不许指远端 URL**；③字族的可写名单从 `src/fonts.ts` 的 `WEB_FONT_FAMILIES` 正则读出（`dg/font-family-whitelist` 把它并进白名单），所以「随镜像发的字族」与「能写进 `fontFamily` 的名字」是同一处真相。**顺带修掉的两处**：`public/fonts/` 不在 `.dockerignore` 里（只有 `public/assets/*` 被挡）——字体是构建产物，必须进镜像；而 `dg/staticfile-asset-check` 原先会把 `staticFile('fonts/…')` 判成「未声明的资产」（每一镜都误红），现按 `fonts/` 前缀豁免：**随镜像发的字节与按镜投递的素材是两件事** |
| Q76 | §5.4 给「官方写法块」同时写了两个上限：正文说「约 **400 字**」，预算列写「≤ **500 B**」——这两个数在中文上不可能同时成立（1 个汉字 3 字节），以哪个为准？ | **以「400 字」为准，并如实登记实测值：631 B / 421 字**（首版 762 B，已压掉 131 B）。**为什么不按 500 B 砍**：把六条砍到 500 B 以内（≈166 字）就得删掉整条规则或把它压成看不懂的缩写——这条块的读者是写镜头代码的模型，压缩到那个程度的文本会**失去可操作性**，而它换来的是 131 字节的预算。**剂量事实**：这一块只在视频链的执行轮随 `SKILL.md` 注入（不是常驻），而 `SKILL.md` 正文本身是 25.3 KB——631 B 是其中 2.5%。§5.4 的两个数字里，「400 字」才是量出来的那个口径（字节数随中英混排浮动），所以按它判、把预算列改记实测值。**判据**：这一节的行数固定为 6 条（一个编号列表），且 `SKILL.md` 的字节数与 §5.4 记的那个数**逐字节相等**（`test_skill.py` 侧的正文回归） |
| Q77 | T21 的许可合规判据读 `node_modules/@remotion/*/package.json` 的 `license` 字段，可 `@remotion/animation-utils@4.0.534` 的这个字段**整个不存在**（上游漏填，`licenses` 也没有）——算越界、还是算合规？ | **两个都不算：按它随包发的 `LICENSE.md` 正文认**（该包发的是标准 MIT 正文，Copyright 2023 Chetan Karwa，`animation-utils` 把自己那批插值工具连许可一起并了进来）。判据落成「字段优先、正文兜底」两层：字段在就信字段；字段缺就翻 `LICENSE.md`/`LICENSE`/`LICENSE.txt`，正文里含 MIT 的授权语（`Permission is hereby granted, free of charge`）就认成 MIT，否则把**正文首行原样带进越界名单**。**为什么按正文认而不是按关键词认**：MIT 正文里根本不出现「MIT」三个字母——按关键词写会永远认不出来，而「永远认不出来」在一个只看「越界名单空不空」的口上，和「全都合规」打印出来一模一样。**为什么两句话分开**：「字段缺 + 正文是 MIT」是上游打包疏漏，「字段缺 + 正文不是 MIT」才是真的许可不明；合成一句「许可未知」，下次真进来一个 GPL 包也要靠人去翻它。**顺带补的一个静默放行**：原先 `package.json` 读不着时是 `continue`（既不报越界也不记未判），现改为记进越界名单；`tests/test_delivery_hygiene.py` ⑤b 用五个假包把五种形态各钉一句人话，其中两种**必须认不出来**——要不这一层兜底只是个「让那条红变绿」的补丁 |
| Q78 | 治第六根因要「随镜像发的 `public/fonts/`」，而按镜渲染的 `publicDir` 是 `public/assets/<镜号>/`（素材必须落在根上，模型才写得成 `staticFile("<name>")`）——两个根，`bundle({publicDir})` 只认一个，怎么处置？ | **软链进这一镜的目录，而不改任何一方的落盘约定**（`assetgate.linkShippedNamespaces`，`server.mjs` 在 `bundle()` 之前调一次）。**真机取证**（这一条不是推断出来的）：⑭ 的真渲探针两支夹具同时 500，原文 `Failed to load font "/public/fonts/NotoSansSC-025.woff2": 404 Not Found`，而同一时刻容器里 `ls /srv/public/fonts/` 是 101 片、逐片 md5 与宿主一致——**盘上有、服务端读不到**，所以「三级取证」的前两级在这里全绿也证明不了字体会加载。**根因**：Remotion 把 publicDir 整份拷进打包目录的 `public/`（`@remotion/bundler/dist/copy-dir.js`），`staticFile()` 的根就是它；而母版 `FilmLayout` 侧效 import `@dg/fonts`，于是**每一镜**都加载字体、**每一镜**都 404——不是「用了图的镜」才中的招。**为什么是软链不是拷贝**：字体是 101 片 4.5 MB 的构建产物，每渲一镜复制一遍既要复制又要随 `session_gc` 清，而 `copyDir` 会把软链按 **realpath** 转投（`symlink(await realpath(srcPath), dest)`），realpath 在容器里指得到，静态文件服务照读不误。**为什么这条要单独登记**：§9-Q75 里那句「随镜像发的字节与按镜投递的素材是两件事」当时只是 lint 的一句话（`fonts/` 前缀豁免），**运行时并没有兑现**——名字被豁免了，可渲染期那个根里压根没有它。这次把它做实，并且**用像素验**：`remotion/tests/shipped-namespace.mjs`（离线 12 项：接上之后`staticFile('fonts/…')` 的落脚点读得到同一份字节、嵌套子树也通、不接 `assets/` 自己（会成死环）、重复调用不动手、**源头缺了同一断言必须塌**（那一支是红样）、真根上 101 片逐字一致）。**判据三条**：①构建期 `test -d public/fonts` + `node tests/shipped-namespace.mjs`（进 Dockerfile 层末与 `npm run verify`）；②真渲 B 半三支夹具都拿到 `motion` 读数（那才是「字体真加载了」的证据）；③接不上时**抛**（带原因）——Windows 宿主建目录软链要开发模式，故用 `'junction'`；接不上等于字体没加载，宁可这一行失败，也不许渲出一张字体不对的片子。**顺带正过一处口径**：⑭ 的探针此前把「渲不成」写成 `check(st === null \|\| st.status === 'reject')`——那是拿未判当通过，两行绿勾勾在一支 404 的镜上；现在未判的镜不进 check 计数、只进未判清单，且没有未判项时才允许打印 `PASS` |
| Q79 | 闸⑭ 那几个数（8 Hz 采样、地板线 1.5、回落线 0.6、≥2 段、相邻 ≥0.4 s）是量出来的还是选出来的？ | **选出来的（判据选型），如实登记，待 §8.1 十二支真片标定**——与闸⑫ 的 0.61、闸⑬ 的阈值同规：先把判据钉在构造样本上，真片标定是之后的事。**采样率是反推的**：要判的最小相邻间隔就是 0.4 s，4 Hz 一格 0.5 s 比这条判据本身还宽（那一条就成了摆设），取 8 Hz 后一格 0.125 s、0.4 s 还剩 3 格余量。**地板线 1.5 的两侧依据**（0–255 灰度单格平均绝对差）：一支元素横穿画面的镜单格差在几十；一次 0.5 s 的淡入（元素占画面一成、亮度变化 20 级）折算到整幅约 2——取 1.5 是「宁可漏报小的、不许把静止判成在动」那一侧的落点。它的**替换判据**不是真片而是构造样本：`remotion/tests/motion-probe.mjs` 的 A 半（静帧 0 段、齐发 1 段、先后三件 3 段、1.0 的抖动 0 段、滞回中段 1 段），改这个数就得同时改那几支。**更值钱的一条是删掉的规则**：初版还有「相隔 ≤1 格的两段合并成一段」（防亚像素抖动把一段切碎），真渲一支三件先后滑入（每件 0.35 s、彼此隔 0.6 s）的镜把它证伪了——8 Hz 采出来是 `[0,0,4.9,3.8,4.4,0,1.7,5.0,6.8,4.3,0,3.8,16.1,22.8,8.7]`，三次事件之间**各只有一格真安静**（值就是 0），合并规则把它们并成一段、一支合法片被判红。算一下就明白这不是「阈值调不好」：8 Hz 下一格 0.125 s，判据要的「相邻 ≥0.4 s」在画面上本来就只剩 1~2 格安静可言，**合并规则吃掉的正是判据要量的那个间隔，两支事件挨得越近（越是判据要拦的那种）越容易被它并掉**。所以删掉而不是调小；防「切碎」改由回落线负责（滞回下沿 = 地板线 ×0.4，A5 那支钉着「中段差掉到地板线下但没停，仍算一段」）。**未判的处置**：抽帧拿不到时记 `undetermined`，`describe_motion` 单独出一条「没判 ≠ 看得出动」，**不按内容缺陷拒镜**——工具故障冒充内容缺陷是另一种错；这一档要不要升级成硬拦按 T28 统一办（`tests/test_code_gates.py` 那条待红的断言就是它） |
| Q80 | P13/T18 写「全片不得同刻齐发」——按每镜判还是按全片判？判在像素上，还是判在 P13 列的那三个通道（几何 / alpha / 描边进度）上？ | **按每镜判、判在像素上**。**为什么不是「全片」**：镜与镜在成片里是**串行**的，不存在「同刻」，所以那句话唯一能落成的意思是「任何一镜都不许齐发」，而 ⑭ 的两条（每镜 ≥2 段、相邻 ≥0.4 s）已经蕴含它；真按全片同一条时间轴判，就成了一个没有物理对应的判据。**为什么不是那三个通道**：它们是**代码里的**量，要么读自报（`card.motion`）、要么解析 AST——前者是自报不是证据，后者在 `interpolate`/`transform` 写成变量或算式时认不出来（与闸⑥「少了 `value` 就成了假指针」同族）。像素差是这三个通道的**共同下游**：任一通道真动了画面上就变，三条都没动画面上就不变，而「有动效但看不出动」正是「代码里有动、像素上没动」这件事的名字。**真机两侧都钉住了**（`remotion/tests/fixtures-motion.mjs` 三支夹具、同一个探针的真渲 B 半）：静帧镜 → 0 段拒、三件齐发镜 → 1 段拒（理由指向「一次跳变」）、先后三件 → 3 段放行。**闭环**：容器判（`motionsample.mjs` → `server.mjs` 回传 `motion`）→ Python 收账（`code_render.summarize_motion`）+ 在**切片进缓存之前**退镜（`motion_error`）+ 出一条带镜号的人话（`describe_motion`）→ 回执（`motion_warnings`/`motion_unjudged` 两键已进 `plan/support.OUTPUT_FIELD_NAMES`，不收就是假告警：账上有、模型看不见） |
| Q81 | §7 与 README 都写「离线回归以 `creation-app` 容器内那一趟为准」——这句话要不要配一条前置动作？ | **要：先 `--force-recreate`，否则那一趟跑的可能是几天前那份镜像**。**实测（2026-10-13）**：`docker compose --profile app build editor` 已把 `creation-app:latest` 烤到新 ID（`081da91f`），而 `docker exec creation-app`（compose 服务 `app`，`image: creation-app:latest`、**没有 `build:`**）里那只容器是 2026-10-12 21:53 建的、跑着已被摘标签的旧镜像 `87cc24abeb0f`：`/app/tests/*.py` **101 支**，新写的 `test_motion_density.py` 压根没进镜像，于是 `md5sum` 那一组在它里面直接 `No such file or directory`。**危险在它长得像对的**：容器里跑、逐文件 `rc`、中文正常——只是考的**是另一份代码**，而且那趟会以假红/假未判的形式记账（红得还特别像环境问题，最容易被判成「容器里跑不了，跳过」）。**判据**：跑套件前比 `docker inspect -f '{{.Image}}' creation-app` 与 `docker images --format '{{.ID}}' creation-app:latest` 是不是同一个 ID，或干脆 `compose up -d --force-recreate app`（镜像 tag 移动**不会**重建容器）。**为什么不给 `app` 也加 `build:`**：`editor` 与 `app` 共用同一份 `creation-app:latest`，两个 build 段会各烤一遍——两倍构建时间，外加**两份可能不同的镜像**，正是这个仓一路在治的「两处各算一遍迟早不一致」；build 只留在 `editor` 上，改判据是多跑一条 recreate |
| Q82 | 仓库根有 `scripts/`、`backend/scripts/` 各一份，**同名不同物**（根那份是 `asset_ingest.py`/`asset_gc_report.py`/`probe_asset_sources.py`，`backend/scripts/` 那份是 `run_tests.py` 一族）——镜像里怎么摆？ | **只把 backend 那份当 `/app/scripts`，根那份另拷到 `/scripts`**（`COPY scripts/ /scripts/`），同时让那两份用例的路径锚点**两个布局都认**。**起因**：`tests/test_asset_sources.py` 与 `tests/test_asset_gc_report.py` 的被测对象就是根 `scripts/` 那几件，而镜像里 `WORKDIR /app` **就等于 backend**——宿主布局「`<仓库根>/backend` + `<仓库根>/scripts`」在容器里只剩了前半截，两份用例于是以 `ModuleNotFoundError: asset_common` 的样子红。**为什么这条必须治而不是登记成未判**：那个红**看起来是环境的锅**（像「容器里没这棵树」），而在 §7 的记账里它就成了一条永远没人回答的未判；`Dockerfile` 里已有先例（`remotion/` 那几个文件就是为了「sanctioned 环境本该能判却判不了」才 COPY 的）。**`/scripts` 这个落点是刻意的**：镜像里仓库根就是 `/`，于是 `Path(__file__).resolve().parents[2]`（宿主指仓库根、容器指 `/`）**两个布局里都指得对**；原地塞进 `/app/scripts` 会把两份同名目录合并，将来谁盖谁全看构建顺序。用例侧另补一条 `parents[1]`（镜像里就是 backend 本尊），所以连不设 `PYTHONPATH` 也稳。**判据**：那两份用例宿主与容器**都绿**，且 `md5sum` 四份逐字相同（已实测 `asset_common.py bf500e465d8dfbbf8b3c201d9578b173`、`asset_gc_report.py f95d336e18c92f84366c518152137ce5`、`asset_ingest.py b3975f087fc04c7c130932b3fa688c85`、`probe_asset_sources.py bfbb9fc784aa4e3c77cb3976ad963027`）。**副作用一个**：改了根 `scripts/` 没重烤镜像，这一条会变旧——变旧报红，不静默过 |
| Q83 | 「投递腿」活服务那两支（`test_asset_delivery.py` ②）用**定名** `assets/_probe/ok.png` 探针——为什么第二趟必红？红的是代码还是用例？ | **红的是用例，改的也是用例**（判据本身没错）。**机理**：第二趟服务端幂等短路，回执里**没有 `bytes`**，而那条断言要的正是「`sha16`/`bytes` 逐项对得上」——`bytes` 只可能来自真写盘那一路，所以这个判据是对的；错的是在**共享服务**上用一个下一趟一定会踩到的定名，于是这份用例的绿带着一个没写出来的前置条件「渲染容器的 `public/` 里没有这只文件」。**它是「环境干净才绿」，比红更坏**：新容器上第一趟全绿、第二趟红，人第一反应是去查幂等是不是坏了——而幂等好得很。**实测**（同一容器连跑）：改前第二趟 `✗ 投成功且响应逐项对得上：{… 'idempotent': True}`、16/16 掉到 15/16；改后**连跑两趟都 16/16**。**改法**：探针名带随机后缀（`assets/_probe/ok-<uuid8>.png`），写盘那一支与幂等那一支验的仍是同一件事，只是不再互相踩。**实话记账**：每趟会往 `_probe/` 里留 43 字节且无处可删（服务端没有删除口，`§9-Q26` 也不做自动删）——而真渲染的 `publicDir` 是 `assets/<镜号>/`（`assetgate.shotPublicDir`），够不着它，所以这些字节不会进任何一帧 |

---

## 10. 残余：四条「买不到判据」的都落成了判据，剩下两条——一条是数字，一条是取舍

### 10.1 那四条的去向（不再挂在「无判据」栏）

| 原表述 | 现在的判据 | 反例证据（必须先证红） |
|---|---|---|
| 画面语义认不出 | **⑲-a**（AST 主体件图元 + 包围盒 ≥0.18，无模型）+ **⑲-b**（judge 盲描述 vs `subject_terms` 词族，两栏原文回执） | §8.3 第 5 支 |
| 版式好不好看 | **⑳(a)(b)(c)**：三种**已记录在案**的坏形态锚定（卡片+文字 / 母题被挤 / 画面同构） | §8.3 第 5、6 支 + 2026-10-11 历史产物 |
| 动效有没有意义 | **㉑**：每个事件必须锚在 `fields.<key>` / `subtitle:<i>` / `beat:<n>` 上，且触发帧落在该锚首次可读窗口 ±k 帧内（今天算得出窗口的只有 `subtitle:` 一条，另两条落未判） | §8.3 第 7 支 |
| 上下文只能人肉把控（提示词/skill 正文/记忆/tools/MCP 会不会堆） | **㉓**：`context_ledger` 账面 + 块级预算（超线抛错）+ `orphan_sections` + 条件块双向红样 | T37 第①支（今天 `prompt_bytes` 四目录零命中 → 实现前必红）+ T38 的**逐块顶预算三次全红** + T39 的「超限必须回执而不是截尾」 |

### 10.2 剩下的一条画面侧残余：**判据自身的错误率**

这条不可消除，但它**已经从形容词变成四个数**（㉒，Q47）：一致率 ≥0.90、与人标注符合率 ≥0.85、`bad:*` 召回 ≥0.95、`good` 误拒 ≤0.15，外加每轮 10% 抽检。

所以这条链**不承诺**「机器能判断美」；它承诺的是「**机器以可测的错误率拒掉已知坏形态，并把当轮的漏判率报给你**」。二者是不同的主张，交付时必须按后者说。

### 10.3 三条已知绕法（对策已排布，但没有一条能宣告堵死）

1. 用**结构极简**的大图形凑满 ⑲-a 的 0.18 占比 → 靠 ⑳(a) 的文本/主体面积**联合**判压低，靠 10% 抽检发现。
2. 把 motion 锚到**必然存在**的字段（如标题）上骗过 ㉑ → **今天只挡住一半，如实记**：闸前要求该字段必须是**这一镜自己声明过的**（锚到不存在的字段与没锚同罪），所以「锚一个不存在的名字」这条路走不通；但窗口那一半今天**算不出 `fields.` 的「画面上首次可读」**，于是这类申报落在**未判**（未判不是通过），不是被打回。要把它判死得有「字段值在画面上变可读的那一帧」这个读数，那是 ⑲-b 那一侧的事——见 §9-Q98。
3. 用**大量小图元**堆主体占比 → ⑳(c) 的像素同构 + ⑭ 的事件密度共同压。

**这三条是绕法清单，不是完备证明**。因此 ㉒ 的 10% 抽检率**永远不许降到 0**，也不许因为「上一轮全绿」而豁免。

### 10.4 两条结构性事实（不是待办，代码修不了）

1. **登录态只在用户侧**。出图（R6-T24）与 judge 盲测（R8-T31）这两半的「真的返回了东西」只能由用户实际跑；本文档能承诺的是配置口存在、代码路径通、自检返回可解释状态，并**明写哪一半没验**。judge 模型是否 vision-capable 取决于用户在三件套里配了哪个模型（Q42 的探针就是用来把这件事变成一个明确回执，而不是一个假设）。
2. **外部来源可达性边界**：知乎两档 UA 全 403、YouTube `-watch` 是 JS 壳、B 站视频页可读。写任何「出处真实」类提示词前先对这张表；`YTDLP_COOKIES_FILE` 与 YouTube 的 JS runtime 是仅有的两个遗留限制。

### 10.5 交付措辞对照（禁止把左栏写成右栏）

| 允许写 | 禁止写 |
|---|---|
| 「⑳ 拒掉三种已记录的坏形态，当轮召回 X」 | 「画面美观已保障」 |
| 「⑲ 两条腿，judge 未配时 ⑲-b 阻断并报未判」 | 「语义识别已解决」 |
| 「㉑ 报出每个动效锚在哪条数据上、触发帧差几帧，以及哪几条锚的窗口今天算不出来（未判）」 | 「动效有叙事意义」；也禁止写「㉑ 保证每个动效都绑定了一条可读的数据」（`fields.`/`beat:` 两条今天必然未判） |
| 「92/96 通过，4 红（清单见 §1.5）」 | 「测试全绿」 |
| 「闸⑪ 未判的镜头单列」 | 「未判按通过计」 |
| 「㉓ 报出本轮各块字节与超线清单，`<memory>` 由 4.3 KB 降到 1.8 KB」 | 「提示词已经精简到位」 |
| 「执行轮工具面按链裁剪，被裁节点仍可请求扩面（T41-② 断言在）」 | 「上下文已经完全动态化」 |

### 10.6 第二条残余：**某块内容值不值得占常驻**（本批唯一没有元闸兜底的一条）

㉓ 是一台**秤**，不是一张**判决书**。它能并且将会报出：这块涨了多少字节、这块本请求无人引用（`orphan_sections`）、这个条件块双向失守。它**不能**报出「这段说明该不该每次都给」。这一格今天没有任何判据可以替换，原因和画面侧「好不好看」不同——画面侧是感知问题，这侧是**取舍问题**：预算表里的每个数都是人写的，把数写大的那一刻 ㉓ 就哑了（Q55 因此禁止「为跑通而抬预算」，但禁止本身也是纪律不是判据）。

处理方式（不是解决方案，是把残余摆到台面上）：

1. **每块留一行理由**：`context_ledger` 里 `kind=always` 的块必须登记 `why_always` 一行（≤80 字），说明它被谁引用、为什么每次都要发。没有这行的块，㉓ 直接按 `orphan_section` 处理。
2. **`orphan_sections` 连续两轮非空即须处置**（删、或补上引用它的那段任务/闸），处置记录进交付；不许第三轮还挂着。
3. **抽检兜底搬到上下文侧**：每轮验收随机取 3 个请求，人工核「有没有哪块本可以不发」，结论写进 §11 的清单。频次**不许降到 0**（同 ㉒ 的 10% 纪律）。

因此，任何交付文档都不许把 R9 写成「上下文治理完成」；正确写法是「**上下文有了账、有了预算、有了双向红样，膨胀会被拦下；哪块该常驻仍需每轮 3 次人工复核**」。这一条是本批**明知治不好而仍在文档里留位**的残余——它不进 ㉒，因为 ㉒ 的四个数都需要画面真值集做分母，而这里没有分母。

### 10.7 第三条残余：字幕**逐词 token 化**未落地（T23 的半句）

P20 那句「字幕条数 × 平均显示时长落在帧账内」在本链是**退化情形**：母版的字幕带是**一镜一句**（`<FilmLayout text=… highlight=…>` 一次画整句，`highlight` 只决定哪几个词被点亮），于是条数恒 = 镜数、显示时长恒 = 该镜切片时长，这笔账已经被闸③ 的帧账含住，另立一条判据只会写出一个恒真的断言。

`@remotion/captions` 的 `createTikTokStyleCaptions` / `parseSrt`（Q14 决定的轮子）**仍未接进这条链**：依赖在 `remotion/package.json` 里、也在 `code_spec` 的 sanctioned 白名单里，但**没有一处 import**。因此今天「字幕什么时候点亮哪个词」是母版按 `highlight` + 帧数均分的，**与 TTS 的真实音素时间对不上**——旁白开着时会出现「字已经亮了但还没念到」。这不是缺陷（没有断言声称它对），但它是一件**已知的不精确**，且要等一条真带配音的真机片才量得出来。

处理方式：任何交付文档不许把 T23 写成「字幕 token 化完成」；正确写法是「**声画三条账（BGM 时长、旁白电平、接缝字尾）已落到字节；字幕点亮仍是均分近似**」。真机矩阵（§8.1）里带配音的那一格测的是三条账，不是逐词对时。

---

## 11. 完成定义（DoD）

逐条打勾，全绿才可对外说「完整闭环」：

- [ ] §1.5 的四条红全部转绿（`test_code_gates` / `test_motion_channel` / `test_storyline_server` / `test_team_wiring`）。
- [ ] `test_delivery_hygiene.py` 存在且五条孤儿断言全绿（T13）。
- [ ] 活容器 md5 == 盘上 md5，覆盖 `code_render.py`/`remotion_client.py`/`FilmLayout.tsx`/`server.mjs`/`palettes.json`/`public/`（§7）。
- [ ] 活库 `materials_origin_check` 定义含 `'art'`；`library` 与 `art` 各能插入读回（T2）。
- [ ] 闸⑱ 在**缺腿**状态下报红、在**有腿**状态下放行；`stage_asset` 在活容器 `grep -c > 0`（T1/T3）。
- [ ] §8.3 **七支**反例全部报红（判据存在的证据），其中第 5/6/7 支必须分别由 ⑲-a/⑳(c)/㉑ 拒。
- [ ] §8.1 12 支 + 缓存断言支跑完，每支附 5 类证据；`undetermined` 一律写「未判」，不计入通过。
- [ ] §2.2 判据表 **23/23** 有实现 + 有红样；两份 import 名单逐项相等（T17）。——**另加 R11/R12 的 ㉔–㉘ 五道**（记在增补卷 §13–§14，不并进 §2.2 那 23 行；红样与现状见 R11-1 条与增补卷 §14.3）。
- [ ] `context_ledger` 在 §8.1 全部 12 支的逐镜记录里都有产物，且 `over_budget` 为空；`kind=always` 的块**每块都有 `why_always` 一行**（缺行即按 `orphan_section` 计）。
- [ ] T38 的**逐块顶预算三次全红**（系统提示词 / `<skills>` 清单 / 技能正文各一次）——只红一次算未完成，因为那说明预算表没被逐块读。
- [ ] T40 / T41 的**双向红样都红过**（该关没关、该开没开各留证据）；执行轮 `tools.sent < total_registered` 且差 ≥20，同时被裁节点名在「如何请求扩面」小节里可见。——**T40 已红过**（命中过滤 `if False:` → 4 ✗；整段永远不发 → 6 ✗；两次都复原，见 §6 T40 行）；**T41 的代码与判据已落地**（`plan/round.py` + 装配 + 断值 + 承载闸，冒烟：33 件发 4 件、差 29），**红样与真机那两腿未跑**（本轮停在写码，如实记为未判，见 §6 T41 行）。
- [ ] §5.4 四个现值全部进预算：`<memory>` ≤2.0 KB、非 motion 规划轮 `knobs` 字节=0、执行轮 schema ≤40 KB、`editing_agent` 与主链共用 `prompt_library`（并库后 `test_prompt_library.py:88` 纳入 `editing_round.md`）。——**`<memory>`（T39）与 `knobs` 条件化（T40）已达标**（后者真机冷启动 0 B）；**执行轮 schema（T41）探针已达标**（54,140 B → 14,561 B ≤ 40 KB），真 run 那一腿待 §8.1。**T42 起副链已并库**（`prompts/editing_round.md` + 共用 `PromptLibrary`，`test_prompt_library.py` ② 那节双向对账，落地时真红过一次，见 §9-Q106 ⑦）；**技能正文按本片计划取块**也已落地，但 §5.4 那一栏的 **16 KB 目标未达**（默认集 24,223 B、代码链一轮 28,698 B），欠账仍挂在表里、**无任务号**（§9-Q106 ③）`knobs` 的文案本身仍超标（欠账表，无任务号）；工具面**未裁的那一档**（闲聊/定时任务）如实记为未做、无任务号（§9-Q105 ②）。
- [ ] ㉓ 的断值表里没有一个数会把**正常轮次**判死：视频链规划轮 8,982 B 曾被旧 `planning_round` 断值（8,192 B，照桩门量的）拦死，已于 T40 按真白名单现值重算为 14,976 B（§9-Q104）。**这条要在 §8.1 首轮再复核一次**（真 P95 × 1.15 收紧后不许出现「正常轮次被拦」）。
- [ ] **T36/T42 的双向红样都红过**（T36/T42 同批）——①不含 motion 节点的轮次里 `sound-design` 块字节=**0**、②计划里有 `search_illustration`/`select_illustration`/`imagegen` 之一时 `image-channel` 块**必现**、③`motion_explainer` 默认集里搜不到那三个 T36 节的标题、④`editing_round.md` 与内联副本逐字相同（改一处不同步即红，`test_prompt_library.py` ② 那一节）。——**代码与判据已落地**（`plan/skills.py sections_for_plan` + `skill.py split_sections/assemble_sections` + `prompts/editing_round.md`），**离线装配全绿**（默认集 24,223 B、代码链一轮 28,698 B、image 轮含 `image-channel`、非 motion 轮空表、内联 ↔ 文件逐字相同——④的红在落地时真出现过一次，见 §9-Q106 ⑦）；**真机 run 那一腿未跑**，如实记为未判（§6 T36/T42 行）。
- [x] **R10-1**：`<skills>` 的开标签与闭标签在一次请求的成品 prompt 里**各只出现 1 次**（T43 的去重断言绿）；§5.4 的 `<skills>` 字节按外层去重后计。——**绿**（`test_skill.py` 数开/闭各 1 次 + 正文块与清单同级；§5.4 那一行按外层去重后计）。病根不是标签重复而是**正文块被吞成子节点**，见 §9-Q134。
- [x] **R10-2**：MCP 客户端与官方 SDK 的关系**已按 Q62 二选一并落地**——要么三件保留项（白名单 / 身份覆写 / 显式调用附注）在 SDK 适配层上全部有通过断言，要么 `sse` 取值已从声明里删除且 `type=="sse"` 有报错文案。**不许出现第三种状态**（声明着 sse、实际走 streamableHttp）。——**绿，走的是甲**：三传输全真，三件保留项在**真 stdio 协议往返**上被断言（`tests/mcp_stub_server.py` 是官方 `FastMCP` 写的真服务端）。退路乙未启用，理由与 SDK 的 cancel-scope 约束见 §9-Q135。
- [~] **R10-3**：默认配置（无 `mcp.json`）启动日志里**有且只有一行**缺失回执；设置页新增一路 MCP 后不重启即出现在 `context_ledger.tools`（T46）。——**前半已在活容器上实测**：2026-10-14 重烤镜像后重启 `creation-app`，`docker logs` 里 `grep MCP` **只命中一行**——「`MCP 静态配置不存在（/app/mcp.json），本次仅 Storyline 一路`」（旧代码这一段是**整段跳过、零回执**）；后半**未判**：要一条真机 run 才读得到 `context_ledger.tools`，本批停在写码。离线能验到的是「种子项出现在 `live_servers()` 且关得掉」（`test_mcp_dynamic.py` ⑤），**不是**真 run 的那一栏。
- [x] **R10-4**：同一 `head_seq` 前缀的第二轮，summarizer 调用次数为 **0**（T47）；且 `compress.py:163-166`（首条 system 不进摘要输入）**未被改动**——这两条要同时绿，只绿后者等于什么都没修。——**两条同时绿**：`test_compress.py` 新增一段同时判「第二轮零调用」与「第二轮用的就是存好的那一份」（只验前者的话，「干脆不插摘要了」也能让计数器不动），并补了「前缀变了要重算」与「换会话作用域不许串话」两支边界；`compress.py` 的 163-166 一行未动。
- [~] **R10-5**：`<history>` 出现在 `context_ledger.sections` 里且有预算上限（T48）；400 条历史的夹具跑出来的 `estimate` 低于改前，且超限时走 `over_budget` 回执**而非静默截断**。——**前半绿，后半是偏离项**：`<history>` 进账了（`test_session_history.py` 断言那一行在、字节是真的、回执键齐）；**「预算上限」不设**，理由见 §9-Q136（挂上去会让长对话在压缩来得及之前当场死，且不可恢复）。「estimate 低于改前」离线只验到窗口把渲染字节从 1,718 降到 410，**真夹具未跑**；「`over_budget` 回执」这条路径对**不存在断值**的块无从走起，替代它的是 `ledger["history"]["dropped"]`（截了多少条逐轮可读，不是静默）。
- [x] **R10-6**：`test_team_wiring.py` 里「子注册表 `has("spawn_subagent") is False`」断言在（T49-①，防递归从注释变判据）；四处手写件金样全绿；`tests/test_requirements_hygiene.py` 存在且绿（含 `mcp` 在客户端侧被用到这一条，Q69）。——**绿**：`test_team_wiring.py` 55/55、`test_handrolled_goldens.py` 62/62、`test_requirements_hygiene.py` 24/24（依赖卫生顺带照出 `aiohttp` 是「装了没用」，已删；见 §9-Q137）。
- [ ] **R11-1**：㉔–㉘ 五道**每道都有红样**（不是「写了代码」）——㉔ 供给面（schema / 声明 / 注册表 / 活库四者对账，库连不上**记未判不是记过**）、㉕ 一枚徽章（四条读数同时成立才打回）、㉖ 回看环（三出口 + 缺席四栏不许混装）、㉗ 分区揭示（算术 + 像素两半）、㉘ `reveal` 申报与画布／库行尺寸对账。入口：`scripts/asset_gate.py`（㉔/T53/T54 一张回执）、`code_spec.check_badge_composition`（㉕）、`scripts/review_gate.py`（㉖）、`scripts/reveal_gate.py`（㉗）、`code_spec.check_reveal_regions`（㉘）。**离线部分今天全绿**（`review_gate` 15 条 / `reveal_gate` 32 条 / `skill_contract_gate` 32 条），**要活库或要真渲的那几腿未跑**，按 D-未判不绿记未判。
- [ ] **R11-2**：活库里 `materials(origin='library') > 0` **且** `materials(origin='art') > 0`（㉔ 的「腿通不通」那一半）。**今天未跑**。
- [ ] **R11-3**：T64 的三分区真机中帧——人眼与 ㉗ 说同一件事（`want="paper"`（`start_ms > t`）采样必须全纸、`want="drawn"`（`start+duration ≤ t`）不许全纸；空 mask 不对称：纸→过、画→硬错；四角基线不一致超 `CORNER_AGREE_DELTA_E_MAX` → 未判）。**今天未跑**。
- [ ] **R11-4**：T60 的真渲探针绿（`remotion/tests/layout-probe.mjs` 那 9 条：allowed mask 的减法在三处时刻各对一次、第二次渲染的 `reveal_used` 逐字节相同、无 `reveal` 的镜 `reveal_used is null`）。**今天未跑**。
- [ ] **R11-5**：增补卷 §14.2 三态账里**「未跑」那些格要有跑过的证据**（T51 真探针 / T52–T54 真 ingest 与回填 / T55–T56 真回看环 / T58 A/B / T60、T64 真渲）；同时 **T63 必须读成「明确不做」而不是「未跑」**——它被决定否掉了，混进未跑栏就是拿决定当欠账，两种状态不许并栏。
- [ ] **R11-6**：`scripts/skill_contract_gate.py` 的**双向对账**（正文点名的名字 ↔ schema/库列/常量；枚举多一档而正文没教也红）与「新加的块不进默认注入集」两条今天绿，且**以后每加一块必须先红**——T57/T62 之后余量只剩 **211 B**，它是防「悄悄加一节把余量吃穿」的唯一一道闸（Q55：先砍，别抬）。
- [ ] §10.6 的每轮 3 次「哪块本可以不发」人工复核**有名单**（哪 3 个请求、结论各一句）；频次不许记 0。
- [ ] `remotion/golden/index.json` 存在，四个形态码各有 ≥1 个 `bad` 样本，㉒ 能读出**四个数**（一致率/符合率/召回/误拒）并附在交付里。
- [ ] 真机 ≥1 支片的逐镜记录里存在 `judgement{gate:"19b"}` 的**两栏原文**（`observed` 原文 + `declared` 原文），`repeats` ≥3。
- [ ] judge 三件套不可用时，⑲-b 报 `undetermined` **并阻断**，回执里写明原因；⑲-a 仍独立生效（证明没吊在一条腿上）。
- [ ] 离线套件 100+ 文件、断言总数 ≥ 1176，容器内 `PYTHONPATH=/app` 口径。
- [ ] §3 复用表的「采纳」项全部在镜像里真装且被闸读到；「不采纳」项全部有理由行，无一悬置。
- [ ] §10 按**新口径**保留：两条残余都在——10.2 是**带阈值的判据错误率**（四个数 + 10% 抽检），10.6 是**「哪块该常驻」这条无法量化的取舍**（每块一行理由 + 每轮 3 次人工复核）。措辞对照表（§10.5）逐条核过，没有把左栏写成右栏。
- [x] 提交：一次性 commit（不 push），工作树不留未记录的半成品；未跟踪目录按 Q23 处理。——**R10 收尾时复查过**：`git status --porcelain` 为空；R10 落成四个提交（`07a2faf` T43/T47/T48、`8fc183d` T46、`b5af691` T49、`bf79c11` 落地账；T44 是更早的 `799967b`），未 push；`docs/scripts/tests/.smoke` 仍是未跟踪工作树目录，`.scratch/` 那一批探针脚本已删。

---

## 附录 A. 环境与命令速查（都是踩过坑的）

| 事项 | 正确做法 |
|---|---|
| 重建镜像 | `docker build -t creation-app:latest .`；`docker build -t creation-remotion:latest ./remotion`。**不用 `compose build`** |
| 路径 WORKDIR | app：`/app` == 仓库 `backend/`；remotion：`/srv` == 仓库 `remotion/` |
| 传脚本/SQL 进容器 | base64 传输（`printf` 会把 `%`/`'`/`$$` 吞掉；`docker exec -w` 的路径会被 MSYS 改写，需套 `sh -c`） |
| 容器凭据 | `docker exec creation-pg sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" ...'`，绝不打印值 |
| 容器内网络 | 无 `curl`，运行期无 proxy |
| 跑离线套件 | 容器内、`PYTHONPATH=/app`、逐文件 `python /app/tests/<f>`、以 rc==0 记通过 |
| 抽帧 | ffmpeg 每 12 s 一帧 |
| `docker cp` | **不算生效**（recreate 清空 `/tmp` 与 cp 痕迹）；只用来临时验证，结论一律以镜像 md5 为准 |
| 检索工具 | 必须显式限定绝对目录，别从仓库根裸递归（会误捞 `.runtime`）；**不 grep `.runtime`、不 commit `.env`/`legacy-credentials.txt`** |
| 密钥 | 只读 env/页面配置的**存在与长度**，不取值、不落盘、不打印 JWT/`ca.token` |
| 浏览器验证 | UI/几何结论只能来自渲染产物或 CDP `evaluate_script` 测量；`mousePressed` 前必须有一次 `mouseMoved`，否则舞台静默不认 |

## 附录 B. 阈值怎么定（避免「写了个数」被当成「写了判据」）

- 派生相似度 `DERIVED_MIN_SIM=0.61`：由 `scripts/calibrate_derived_sim.py:84` 标定，输入是 demo↔镜头代码的字节级比对集。**复现方式**：容器内跑该脚本，输出中位数/分位即阈值来源。改阈值必须同批改脚本，否则 T13/⑫ 断言红。
- 跨镜同构 `0.88`、字色 `3.0`/`4.5`、`CHANNEL_QUOTA=((6,3),(10,4))`、`MAX_TEXT_ONLY_SHARE=1/3`、`CONCRETE_SHARE_TARGET=0.40`、`MIN_SHOTS_FOR_QUOTA=6`：均为**既有实现值**，本文档不改数；任何一项要改，先补一支「改小会误放 / 改大会误拒」的双向夹具。
- 新增的 ⑭（≥2 事件、相邻 ≥0.4 s）、⑮（族数 ≥ `ceil(镜数/4)`、同族 ≤ 2 镜）、⑰（BGM ≥ 成片×0.98）：首轮以 §8.1 十二支实测分布定标，标定脚本沿用 `scripts/calibrate_*.py` 命名法，且**先证红再上线**（D-红先）。
- **R8 四道新闸的数怎么来（不许拍脑袋，也不许照抄本文档的值当结论）**：
  - `SUBJECT_MIN_BBOX=0.18`（⑲-a/⑳(a) 共用）：拿 §8.1 十二支 + 2026-10-11 那六支真渲夹具的**闸⑧ 回读几何**跑一遍，取「有主体件」那批的下分位与「卡片+文字」那批的上分位之间的分隔点。若两批分布重叠 → ⑲-a 单独判不住，必须与 ⑳(a) 的文本占比**联合**判，这条要写进实现而不是硬调阈值。
  - ㉑ 的 `k=fps//3`：窗口容差。用真渲夹具测「字段可读首帧」与「事件触发帧」的实际差分布，取 95 分位；改 `fps` 时 k 必须重算，不许写死帧数。
  - ⑳(c) 的「布局盒哈希 + 主色直方图一致」：相似度阈值同样从 §8.1 的**跨镜两两配对**分布定，且必须与闸⑨ 的 `STRUCT_SAME/STRUCT_NEAR` 对照画出「代码像 vs 像」两条曲线——两条曲线不得互为替代（Q51）。
  - **㉒ 的四个数不是标定值而是每轮重算值**：`scripts/check_gate_selfconsistency.py` 在每轮 §8 后输出当轮一致率/符合率/召回/误拒，写进交付。任何一轮不达 → 该闸自动 `undetermined`（Q48 的退出机制反向也成立）。
- **R9 的预算数与召回 k 怎么来（同样不许照抄本文档的值当结论）**：
  - §5.4「预算」列里那些数（3.0/1.5/2.0/5.0/1.2/16/12/40/0.5 KB）**是本轮写文档时按现值上浮给的目标值，不是标定值**。真正的落地口径：**T37 的账先上线 → 拿 §8.1 十二支夹具跑出每块 P95 → 预算 = P95 × 1.15 → 把这两个数（P95、预算）同时写回 §5.4 表和 `test_context_hygiene.py` 的常量表**。两处不同步即红（这正是 Q55 第②种出路要求的「同批改两处」）。
  - `<memory>` 的召回 top-k 起始值 **k=6**：用现有记忆条目（`plan_retro.py` 写回的那批）跑「本轮话题相关/无关」的人工标注 10 条，取「相关条数的最大值」作为 k；若相关集常 >6，说明记忆条目粒度写得太细，**改写回粒度而不是调大 k**（调大 k 等于把堆砌合法化）。
  - `USAGE_NOTE ≤ 120` 字与 MCP 描述 `≤600 B` 这两个数是**内容长度约束**，不需要标定，但**改一个数必须同批改对应断言**（同 `DERIVED_MIN_SIM` 那条纪律的句式）。
  - **明令禁止**：任何一次验收若出现「㉓ 红了所以把预算调大」，该轮记录为**未完成**，并按 Q55 走两种正当出路之一。
- **R10 的两条窗口/预算数怎么来**：
  - T48 的历史窗口 **N 与 `<history>` 预算同批标定**，口径与 R9 完全一致：先上线 T37 账面 → 拿 §8.1 十二支夹具跑出「每轮实际送出的历史条数」与「`<history>` 块字节」两条分布 → **N 取 P95 向上取整、预算取 P95 × 1.15** → 两个数同时写回 §5.4 表末那行（会话历史）与 `test_context_hygiene.py` 常量表，单改即红（Q55 第②种出路）。**N 不许先拍一个整数**：拍小了会把用户十轮前说过的约束挤出窗口，那是把「省字节」变成「丢上下文」，属于 §10.2 之外的第二种坏。
  - T47 的 `head_seq` 命中判据是**整数相等**（同一压缩前缀），不涉及阈值标定；但它的**产物必须逐字等于**改前——所以它的"数"是一条**金样**（拿最长会话的一轮比对压缩产物），不是分位数。
  - T44/T46/T49 都不产生新阈值：Q62 是二选一、Q63 是「有无一行回执」、T49 是金样与断言存在性。**如果实现过程中有人给这三条编了个数字（比如"MCP 描述截断到 X 字"另立一套），那是把决定改成了参数，必须回 §9 追加行而不是就地取值。**
- 启发式阈值一律要跑全量套件（历史基线 96 文件 / 1176 断言，R0–R10 收口后 100+ 文件），局部绿不算绿。

## 附录 C. 本轮涉及的文件清单

新增：`remotion/public/assets/.gitkeep`、`remotion/public/fonts/*.woff2`、`remotion/golden/index.json` + `remotion/golden/frames/`、`tests/test_delivery_hygiene.py`、`tests/test_semantic_gates.py`（⑲⑳㉑ 各一支红样）、`tests/test_context_hygiene.py`（㉓：账面、逐块顶预算三次红、T40/T41 双向红样、预算常量表与 §5.4 同步断言）、`backend/agent_framework/prompts/editing_round.md`（副链提示词入册）、`backend/storyline_server/motion/judge_channel.py`、`backend/storyline_server/nodes/imagegen_node.py`、`scripts/asset_gc_report.py`、`scripts/harvest_golden.py`、`scripts/check_gate_selfconsistency.py`、`scripts/calibrate_subject_bbox.py`、`scripts/calibrate_motion_density.py`；**R10 另新增**：`tests/test_requirements_hygiene.py`（Q69：`requirements*.txt` 每个声明包必须被 import/调用命中，零命中即红，白名单需一行理由）、`tests/test_handrolled_goldens.py`（四处自持件各一条金样：`auth.py` JWT 三态〔无头/过期/错签名〕+ PBKDF2、`mcp.py` YAML 子集含 `${ENV}` 未定义、`skill.py` frontmatter 多行 `description`、`compress.py` 纯 CJK/混合串计数）、`tests/mcp_stub_server.py`（T44 的 stdio 桩，跑 `tools/list`→`tools/call` 全链路）。
修改：`backend/agent_framework/storage/migrations.sql`（并入 `'art'`，废弃 `migrations/` 目录）、`backend/agent_framework/session_service.py`（T3 投递腿）、`backend/agent_framework/server.py`（`:2299-2330` 去硬编码 model；T11 投递口）、`backend/storyline_server/motion/code_spec.py`（⑯⑮⑱ 外加 **⑲-a `check_subject_present` / ⑳ `check_degenerate_forms` / ㉑ 锚校验** 三个 checker 进 `:566` 注册表 + schema `:1460-1478` 加 `subject_terms`/`motion[].anchors` + 名单 `:1433`）、`backend/storyline_server/motion/code_render.py`（闸⑬⑭记录键 + **`judgement` 回执与 `repeats`**）、`remotion/lint-rules.mjs`（名单 `:55`、`staticfile-asset-check`、新增字体加载断言）、`remotion/server.mjs`（`publicDir`/`/stage-assets`/闸⑬⑭ + **⑲-b/⑳(c)/㉑ 要的 2 帧采样、布局盒哈希、事件触发帧**）、`remotion/src/FilmLayout.tsx`（入场出场 token 化）、`remotion/src/themes/palettes.json`（preset 三档）、`remotion/package.json` + lockfile（新增 4 个 MIT 包 → `assets_hash`/`cache_key` 会随 lockfile 哈希变化，属预期）、`remotion/Dockerfile` + app Dockerfile（COPY 补齐 `public/`、`FilmLayout.tsx`、`server.mjs`、`src/demos`、**`remotion/golden/`**）、`frontend/`（重建 dist）、`backend/examples/skills/motion_explainer/SKILL.md`（逐词对账 + **R8 三节以 `<!-- section: … -->` 块形式加入并登记 `sections:`，默认注入集字节不涨**）、`tests/test_storyline_server.py`、`tests/test_team_wiring.py`、`tests/test_code_gates.py`、`tests/test_motion_channel.py`；**R9 上下文面另改**：`backend/agent_framework/context.py`（`:226` 出口产 `context_ledger`；`:204-219 _bootstrap_sections` 按 Q57 接配置口或删除）、`memory.py`（`:123` 用起 `query` 做关键词召回、`:116-121` 的「保尾部」改成 `over_budget` 回执、`USAGE_NOTE` 344→≤120 字）、`plan/vocab.py`（`:210-252` `knobs` 条件化 + 新增 `knob_facts_for(node)`）+ **新增** `plan/knobs.py`（T40 的命中判据层，只依赖 `support`）+ `plan/prompt.py`（`:327-339` → `{?knobs}` 守卫块）+ `plan/gate.py`（转发 `knob_facts_for`）+ `plan/validate.py`（打回消息带真键名，`_knobs_keys_hint`）+ `prompts/planning_round.md`（同一处守卫，与内联模板逐字同改）+ `context.py`（`planning_round` 断值 8,192→14,976，§9-Q104）、`plan/skills.py`（`:13-37` 收 `sections=`）+ `skill.py`（分段解析）、`agent.py`（T41 已落：`execute_plan` 走 `registry_for_round(plan_steps)` 并加 `_scope_section`、`_resume_registry` 按 `approved_plan` 取回同口径的表、`_record_context_ledger` 记 `filter` 并在发请求前判 `tools` 断值；`_round_registry` 的缓存签名含工具子集身份）、**新增** `plan/round.py`（T41 的装配/判据层：`registry_for_round`/`trimmed_nodes`/`scope_lines`/`unbacked_truncations`）、`tools/mcp.py`（T41 已落：`compose_description` 把每份描述截到 `DESC_MAX_BYTES=600`，溢出指向随身技能正文）、`context.py`（T41 已落：`SECTION_BUDGET_BYTES["tools"]=40960` + `tools_box` 补 `bytes`/`desc_bytes` + `check_tools_budget`）、`editing_agent.py`（`:38` 常量改读 `prompts/editing_round.md`）、`tests/test_prompt_library.py`（`:88` 对账清单纳入新文件）、`tests/test_memory.py`（补三条：召回命中、超限回执、`USAGE_NOTE` 字节）、`tests/test_planning_prompt_golden.py`（`:83-110` 加 `knobs`/7 守卫的双向 golden：9 条重烤 + 2 条新增 + 27 条断言）、`tests/test_plan_gate.py`、`tests/test_plan_layout.py`、`tests/test_context_hygiene.py`、`tests/data/planning_round_golden.json`、`tests/test_skill.py`（`:125-128` 旁加分段块按通道取用的两支断言）；**R10 骨架面另改**：`skill.py`（`:286-299` 的 `<skills>` 自拼标签与 `context.py:253` 的按名包裹**二选一**，T43/Q70）、`context.py`（装配出口把 `<history>` 作为一块记账，T48）、`session.py`（`:62-64` 加 LIMIT 改窗口拉取，T48）、`compress.py`（`:180-184` 摘要按 `head_seq` 存取；**`:163-166` 不许动**，T47/Q64）、`checkpoint.py` + `repositories.py`（复用 `checkpoints`/`checkpoint_entries` 存摘要行，**不新建表**，T47）、`tools/mcp.py`（客户端换官方 SDK 薄适配层或删 `:41` 的 `sse` 取值，T44/Q62；`:612-615` 的兜底分支必须与声明一致）、`run_server.py`（`:115` 默认路径改为可选种子、`:540-556` 缺失态出一行回执，T46/Q63）、`subagent.py`（`:148` 的工具描述如实写「同步等待」，T49/Q65）、`settings` 前端 + `frontend/dist`（新增 MCP 区块后**必须重建 dist**，否则 FastAPI StaticFiles 挂的还是旧包，见 §1.4）、`tests/test_team_wiring.py`（追加 spawn 防递归断言）。
删除：`remotion/src/themes/styles.json`、`backend/agent_framework/storage/migrations/20261012_asset_origin.sql`（内容并入 `migrations.sql` 后）、`editing_agent.py:38` 的 `EDITING_SYSTEM_PROMPT` 硬编码正文（并库后只留 `prompt_library` 回落副本）。

**执行期实际新增、本清单未预告的文件**（2026-10-13 实测盘上）：`remotion/motionsample.mjs`（闸⑭ 的像素侧实现：ffmpeg 抽 8 Hz 灰度小图 → 相邻帧平均绝对差 → 按地板线切「先后变化段」）、`remotion/tests/motion-probe.mjs`（闸⑭ 的 A 半离线纯函数红/绿对 + B 半真渲，跑不了记未判）、`backend/agent_framework/library_pick.py`（T11 投递口的实现形态：独立模块，被 `server.py:67/1764` 与 `asset_nodes.py:17` 两处消费——本清单原写「改 `session_service.py`」，实际落在新模块，**结论等价、路径以盘上为准**）、`scripts/asset_common.py`、`.gitattributes`（Q74）、`remotion/src/fonts.notosanssc.generated.ts` + `remotion/public/fonts/*.woff2`（Q75）、`backend/tests/test_library_pick.py`／`test_asset_delivery.py`／`test_code_assets.py`／`test_asset_gc_report.py`／`test_upload_resume.py`／`test_code_forage.py`／`test_derived_calibration.py`／`test_theme_single_source.py`。**T18b/T21/T16b 追加**（2026-10-13）：`backend/tests/test_motif_share.py`（横屏母题占比回归，§9-Q88）、`scripts/image_parity.py` + `backend/tests/test_image_parity.py`（镜像逐件对账与它的清单，§9-Q86/Q87/Q89）、`remotion/tests/effects-probe.mjs`（`@remotion/effects` 那两件的入场券，§9-Q89）；`remotion/package.json` + lockfile 追加 `@remotion/effects@4.0.534`，四处 import 名单同步成 **44 项**（`lint-rules.mjs`／`code_spec.py`／`SKILL.md` 正文那句数也从常量比）。**一处口径要追认**：闸⑭ 的数（`MOTION_SAMPLE_HZ=8`、`MOTION_DIFF_MIN=1.5`、段数 ≥2、相邻 ≥0.4 s）附录 B 原本要求由 `scripts/calibrate_motion_density.py` 从 §8.1 十二支实测分布标定，实现改成了「采样率由判据反推 + 阈值由 `motion-probe.mjs` 双向钉住」——**标定脚本仍不存在**，这条偏差不并入 Q71–Q77（那七条各是别的事），须按 §11 的「先证红再上线」口径单独认一次：要么补脚本、要么把 §8.1 首轮真渲分布写回来替它作数。

## 附录 D. 官方 skill 可抄 / 禁抄对照（实现者的逐条检查表）

| 官方出处 | 原句（节选） | 处置 |
|---|---|---|
| `markup/SKILL.md` | 「Use `staticFile()` for files in `public/` **or pass a remote URL directly**」 | **禁抄后半句**；我们的 `no-external-urls` + `staticfile-asset-check` 只允许前者 |
| `markup/SKILL.md` | 「Google Fonts is the recommended way to load fonts」 | **禁抄**；改 `@remotion/fonts` + 本地 woff2（T16） |
| `markup/sfx.md` | 34 条 `https://remotion.media/*.wav` | **禁抄 URL**；只采「峰值归一 −3 dB」约定 |
| `best-practices/SKILL.md` | 「start Studio and open the preview in the browser」/「Only render if the user is very explicit」 | **禁抄**（无 Studio）；后半句的「不轻易起渲染」与我们 §7 的按需渲染精神一致，可借语义 |
| `render/SKILL.md` | `npx remotion render` / `still --frames=0,30,90` | **CLI 不采纳**；`--frames` 多帧出图行为已在 §8.2-3 抽帧里以 ffmpeg 实现 |
| `captions/SKILL.md` | 「copy the `basic-captions.tsx` file into the project **unchanged**」 | **禁抄**（§3.2 D-抄）；能力用 `@remotion/captions` 自研显示 |
| `captions/transcribe-captions.md` | 「Whisper locally on the GPU…**A compatible GPU is required**」 | **不采纳**；字幕来自既有 ASR 链 |
| `multimedia/SKILL.md` | 「Mediabunny … in the browser」+ `UrlSource` | **不采纳**（时长口径在 Python 侧算，§4-4） |
| `markup`（timing/sequencing/transitions/measuring-text/effects） | `interpolate`/`Easing.bezier/spring`/`output:'perceptual-scale'`/`posterize`/`premountFor`/`trimBefore`/`fillTextBox`/effects 目标表 | **可抄写法**（§3.2 的 6 条 + T16） |

---

## 结语

- **现在的阻塞全是部署与契约收口，不是知识层。** 因此「换一份官方说明书」不能推进它；R0 与 §7 能。
- **能复用的都复用了**：`@remotion/fonts`（治静默字体回落）、`@remotion/captions`（字幕 token 轮子）、`@remotion/rough-notation`、`@remotion/noise`、`@remotion/layout-utils` 的 `fillTextBox`、`@remotion/media-utils` 的音频可视化、以及官方 6 条动效写法。
- **不能复用的都给了理由，并且理由是用「断掉了哪条既有保证」标的价**：树内转场会破逐镜缓存，联网字体在离线镜像里必炸，原样拷贝 skill 源码会把非 MIT 文本弄进仓库。
- **上下文面（提示词 / skills / tools / MCP / 记忆）这一批的处理是「保留已动态的，补齐真堆着的」**。已经动态的五条（按链预加载、规划轮物理过滤、条件块守卫、`always:` 缺席、nudge 条件回灌）一条不重写；真堆着的四条——`<memory>` 每次全量 dump（还带静默截尾）、`knobs` 参数教程每规划轮常发、执行轮 53–57 份 schema 整批发、剪辑副链第二份提示词真相源——各自有 §5.7 的改法与 ㉓ 的判据，且 §5.4 的账从「人工量一次」变成「每请求产出」。**这一批治不好的那一条写在 §10.6**：㉓ 是一台秤，称得出膨胀与孤儿段，称不出「这段内容值不值得每次都发」——那是人的取舍，只能以「每块留一行理由 + 每轮 3 次人工复核」的规则化形式存在。
- **本轮新增的两项体检也已闭环，且没有留下形容词**：①**与通用 Agent 架构骨架逐项对照**（§1.10，17 项 → 12 项本仓已具备（多为超集）、5 项有差）：归 R10 的那几项编号成 P29/P30/P31/P32/P33/P34/P35 并落成 T43–T49 与 Q61–Q70，另 2 项早在 R9 记账（P24/Q54、§1.8 第五格/Q57）故不重复开工；其中两条是「与骨架不同但更强」（SpawnTool 的 await 语义、Agent Team 用 PG 表 + `consumed_at` 而不是 JSONL 已读即删），不折算成债；②**「能复用外部开源就别造轮子」这本账在 Agent 侧结清**（§3.6，14 行逐项定价），四个自持件（JWT/PBKDF2/YAML 子集/中文字数）逐一写明「换库会断掉哪条既有保证」，并用 Q69 的 `tests/test_requirements_hygiene.py` 把这句话变成**会红的断言**——声明了却没 import 的包、该有 golden 却没有 golden 的自持件，都算失败。判据的语义是「账目对得上」，不是「我们很省」。**这一批治不好的仍是同一条**：骨架对照能查出「哪一层没接上」，查不出「这段常驻内容值不值得每次都发」（§10.6）；⑳ 的判据集合只会随人工标注增长，**没见过的退化形态它判不出来**——这两句不是遗留问题清单，是判据的边界，写在 §10 就不许在交付语里被抹成「已全部自动化」。
- **以前说「买不到判据」的三条，现在各有判据了**：⑲-a（AST+包围盒，无模型，烧像素前就拒「卡片+文字」）、⑲-b（judge 盲描述对 `subject_terms`）、⑳（三种历史退化形态锚定）、㉑（动效必须锚在真实可读的数据上）；㉒ 把「这四道自己会不会错」压成一致率/符合率/召回率/误拒率四个带阈值的数字。**残余不是「无法判定」，而是「判定有错误率、且有 10% 抽检兜底」**——这就是 §10 的全部内容，也是「无需下一次开发」的确切含义：没有下一轮**待解决**的问题，只有下一轮**要复测**的数字。交付文档里这句话的措辞按 §10.5 的对照表写，一字不放宽，也不许升格成「审美已被机器掌握」。
