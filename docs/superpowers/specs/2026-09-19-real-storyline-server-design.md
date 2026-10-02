# 真实 Storyline MCP Server 设计（方案 A）

日期：2026-09-19 · 状态：已实现并完成联机验证
目标：把「智能创作助手」的 19 个 mock 剪辑节点替换为可真实出片的 Storyline MCP Server，
达到上线质量的本地单体前后端项目（暂不部署），严格复用文档既有编排机制，不另起炉灶。

> **2026-09-21 口径更新（读本文前先换掉这几处机制，节点契约不变）**
> 本文写于「本地单体、暂不部署」阶段。存储层已被
> `2026-09-20-pg-minio-storage-design.md` 全量替换，下列字样在今天的代码里**已不存在**：
> `FileStore` → PG `artifacts` 表；`.storyline/out/{会话}` → MinIO `renders/{会话}/`；
> `media_url=/media/…` 与 FastAPI 的 `/media` 静态挂载 → presigned 直链（支持 Range 206）；
> `[media]` 的 `library_dirs / bgm_dirs / out_dir / cache_dir` 四目录 → `[storage]`
> （PG DSN + bucket + 可丢弃工作区/内容缓存根）；`POST /upload` 落盘并回填绝对路径 →
> 直写 MinIO + 登记 `materials`，回 `material_id`，MCP 入参是 `load_media(material_ids=[…])`；
> `search_media` / `select_BGM` 按扩展名扫目录 → 查 `materials` 表。
> 节点名、DAG 边、payload 契约键、降级策略、`MediaCardHook` → WS `type=media` 链路照旧。
> 部署与运行方式以仓库根 `README.md` 为准。

## 1. 范围与约束

- **复用而非重写**：服务端直接使用 `agent_framework/orchestration.py` 的
  `BaseNode` / `Interceptor` 与产物存储层（原 `FileStore`，现为 PG `artifacts` 表），
  节点名、DAG 边、payload 契约键与 mock 完全一致，
  主 Agent 侧（run_server、工具白名单、Skill、测试）**零改动**。
  这张 DAG 引擎如今只被 `storyline_server/` 装配（`server.py:23`、`nodes/core_nodes.py:19`）；
  客户端那套 mock 节点（`agent_framework/video_editing.py`）只剩离线夹具身份，见 §2。
- **本地模型 + 一个服务商**：ASR 用 faster-whisper（本地，GPU 可用则 CUDA）；TTS 用 edge-tts（免费）；
  画面理解与文案生成走同一个服务商的同一把 key——`deepseek-flash` 实测支持 `image_url`，
  所以不再需要第二家服务商（原设计的 SiliconFlow Qwen2.5-VL 已换掉）。
- **API 密钥**：日常在页面「设置」里填，值落 PG `app_secrets`（按用户），两台服务每次请求前
  热读、不用重启；`OPENAI_API_KEY` 是回落层。任何情况下都不打印、不进日志、对外只出掩码。
- **思考模式默认关**：`deepseek-flash` 服务端默认先写一大段 `reasoning_content`，而本项目
  的时间花在成百次小请求上（画面理解逐镜一次请求）。两条通道都默认发
  `{"thinking": {"type": "disabled"}}`（常量 `llm_openai.THINKING_OFF` 唯一定义），
  开关分别是 `thinking=` / `OPENAI_THINKING` 与 `[capabilities] vl_thinking`；
  真机中位对比：文本 2.1×、带图 1.7×，答复与描述要素无退化（口径见 `README.md` §3.8）。
- **search_media = 素材库检索**：查 `materials` 表（上传与按链接取料都登记在这张表），不联网。
- **前端 = 聊天 + 产物播放卡片**：渲染完成后成片以 `<video>` 卡片出现在会话流里。

## 2. 总体架构

```
主 Agent (run_server :8000)
  └─ StreamableHttpTransport ──MCP JSON-RPC──▶ StorylineServer (:8001, FastMCP streamable-http)
                                                 ├─ Interceptor（递归补依赖 + order_trace）
                                                 ├─ 19 × StoryNode(BaseNode)（真实实现）
                                                 ├─ 产物状态：PG artifacts 表（按 会话/产物/节点 寻址）
                                                 ├─ 渲染工作区：本地临时目录（可丢弃，LRU+TTL 回收）
                                                 └─ Providers（whisper / edge-tts / SiliconFlow VL / DeepSeek）
成片字节进 MinIO renders/{会话}/{产物}.mp4  →  render_video 返回 media_url=presigned 直链
  →  MediaCardHook(after_execute_tools) → MQ OutBound → ConnectionManager → WS type=media
  →  前端播放卡片（<video> 直接播 presigned URL，支持 Range 拖动）
```

主服务启动时 `_startup()` 连上 :8001 即注册 `storyline_*` 远程工具。**剪辑节点只有这一个来源**：
本地 mock 不再进生产装配（2026-09-23 起），连不上 Storyline 就是**本服务没有剪辑能力**，
启动日志明确告警，不静默降级成一套假节点给出假成功。`agent_framework/video_editing.py`
保留，但定位已收窄成**离线测试夹具**（节点定义、DAG 联机用例靠它，服务不接入）。
理由见 `2026-09-23-checkpoint-fork-reducer-design.md` §4：两套图并存时依赖补齐与
`require_prior_kind` 校验只活在 mock 侧，远程 MCP 工具绕过本地 Interceptor，
DAG 概念等于造了两遍、只装上一遍。

## 3. 组件

### 3.1 `storyline_server/settings.py`
TOML 驱动（`Settings.load(path)`，tomllib）：
`[local_mcp_server]`（host/port/path/timeout/available_nodes 白名单，沿用文档字段）、
`[storage]`（backend=pg_minio|memory + 工作区/内容缓存根 + LRU 上限；**取代原 `[media]`
的 library_dirs / bgm_dirs / out_dir / cache_dir 四目录**，DSN 与 bucket 只从环境变量读）、
`[capabilities]`（vl_base / vl_model=deepseek-flash（与主 LLM 同源，实测吃 image_url）/
vl_key_env / vl_max_tokens=1024 / vl_thinking=false（逐镜一次请求的热路径，默认关思考）/
asr_model=small /
asr_device=auto / tts_voice / scene_threshold / min·max_shot_sec / max_pause_sec /
bgm_volume / subtitle_font / transition_sec / proxy_max_side；后加 `provider_timeout_sec`
——外部能力卡死按超时降级，`font_dirs`——字幕字体跨平台目录，不再是硬编码 Windows 路径）。

### 3.2 `storyline_server/mediaops.py`
ffmpeg/ffprobe 阻塞原语（调用方一律 `asyncio.to_thread`）：`probe`（宽高/时长/fps/有无音轨，
坏帧率回退 25.0）、`cut`（重编码 veryfast）、`extract_audio`（16k 单声道 wav）、
`extract_frame`（缩放代理帧）、`scene_ranges`（scdet 场景边界 + 合并过短/拆分过长）、
`concat`、`xfade_clip`（AI 转场片段）、`silent_wav`/`tone_wav`（降级介质）。
（原 `write_progress` 原子写 `render_progress.json` 已随 FileStore 一起退役，
渲染进度与结果现在写 PG `render_jobs` 表。）

### 3.3 `storyline_server/providers.py`
四个可注入的能力出口，`build_providers(caps, *, vision=, transcribe=, llm=, tts=)` 支持测试替身：
- `vision(images, prompt, *, api_key=, key_source=)`：urllib POST base64 data-URL 到
  `vl_base`/chat/completions（默认与主 LLM 同一个服务商同一个模型）；key 由节点在异步侧
  `resolve_key()` 每轮解析一次后显式传入（前端「设置」→ `vl_key_env` → 主 LLM 回落位），
  解析不到直接 `ProviderError`；请求体默认并 `agent_framework.llm_openai.THINKING_OFF`
  （`{"thinking":{"type":"disabled"}}`，`vl_thinking=true` 才不发）——逐镜一次请求的上百次
  带图往返里，思考内容是唯一能被直接省掉的开销（真机中位 747ms vs 1293ms）；
- `transcribe(wav)`：faster-whisper 懒加载缓存，CUDA 失败回退 CPU+int8，带 vad_filter；
- `llm(messages)`：复用 `agent_framework.llm_openai.get_default_llm()`（同一密钥源）；
- `tts(text, dst)`：edge-tts，产物 <512B 视为失败。
**所有网络/模型失败抛 ProviderError，由节点捕获降级，绝不炸整条流水线。**

### 3.4 `storyline_server/nodes/core_nodes.py` — 19 个真实节点
与 mock 同名同依赖（DAG 联机实测输出）：

| 节点 | 依赖 | 真实实现 |
|---|---|---|
| search_media | – | 查 `materials` 表（本人/本会话作用域内按关键词匹配文件名） |
| load_media | – | `material_ids=[…]` → localize 到工作区 → ffprobe → `{media, clips}` |
| split_shots | load_media | 场景检测切镜 → `{clips(shots), shot_count}` |
| understand_clips | split_shots | 代表帧 + VL 逐镜描述 → `{clip_captions}` |
| filter_clips | understand_clips | 按主题关键词保留/丢弃 → `{clips, dropped}` |
| group_clips | filter_clips | 按源素材聚合、合并 ≤4 组 → `{groups}` |
| asr | load_media | whisper 分段转写 → `{asr_segments, warnings}` |
| speech_rough_cut | asr | 按 max_pause_sec 保留口播段 → `{rough_clips, kept_sec}` |
| script_template_rec | – | 模板库匹配 → `{templates}`（data/templates.json：vlog 三幕/知识口播/好物测评/自由） |
| generate_script | group_clips, script_template_rec, text_rec | LLM 逐组产文案（支持 custom_script 直书），失败按字幕拼接兜底 → `{group_scripts, title}` |
| generate_ai_transition | group_clips | VL 生成转场文案 → `{transitions}` |
| transition_rec | group_clips | 规则推荐转场样式 → `{transition_plan}` |
| text_rec | asr, understand_clips | 抽取主题词 → `{keywords}` |
| generate_voiceover | group_scripts | edge-tts 逐组配音 → `{voiceover}`；原声混剪模式自动跳过（`{voiceover: [], mode: "original_audio"}`） |
| select_BGM | generate_script | 查 `materials` 里归属本人的音频行当曲库（前端上传/取料的音频即曲库），按请求 2-gram 关键词排序取首 → `{bgm, source}`；无曲库时生成占位音轨 |
| plan_timeline | speech_rough_cut, 4 项 | 基础时间线（画面+音轨+字幕）；满足原声条件时改出原声混剪时间线 |
| plan_timeline_pro | speech_rough_cut, 6 项 | 叠加转场/花字推荐；同样支持原声混剪 |
| plan_timeline_ai_transition | speech_rough_cut, 6 项 | 插入 xfade 转场事件（kind:"transition"）；同样支持原声混剪 |
| render_video | 三个 timeline | 优先级 ai>pro>base 取最新；MoviePy 合成，异常回退 ffmpeg concat，**保证产出 mp4**；字节 publish 进 MinIO `renders/`，进度与结果写 `render_jobs`（status/stage/percent/error + 终态 `result`）。执行位已挪出请求生命周期（README §3.10）：handler 登记 + 后台起跑，内联只等 `render_grace_sec`，未完成即回 `{node, artifact_id, output:null, render:{status,stage,percent}, hint}` 并指向 `render_status`；done 后 `render_status` 从 `result` 重建 `{video, duration, width, height, title}` 并现签 `media_url`。返回值顶层保持 `node/artifact_id/output` 三键——`MediaCardHook` 与 `record_rendered_media` 就认这个形状 |

**原声保真混剪（口播原声 + 空镜画面）**：三个 plan 节点的依赖把 `speech_rough_cut`
排在 `generate_voiceover` **之前**，因此口播区间先落盘。当 ①`rough_clips` 非空且
②用户要原声（`keep_original_audio=True` 显式参数，或 `user_request` 命中
「原声/别配音/演讲声…」关键词）时，`_build_original_timeline` 取代常规时间线：

- 音轨 = 口播原声区间串联（`audio_events` 带 `src_start/src_end`，渲染侧
  `AudioFileClip(源视频).subclipped(...)` 直接从 mp4 取声）；
- 时间线总长 = 口播净时长；画面轨用**非口播源素材**（空镜）循环铺满、裁到等长；
- 字幕逐字取 ASR 原文；TTS 配音环节整体跳过。

跨节点开关传递：Interceptor 补齐依赖时以默认入参执行依赖节点，故 `invoke()` 先把
客户端参数留痕到 `NodeState.flags`，`_wants_original_audio` 依次查 inputs → flags →
关键词。渲染侧画面层统一 `with_audio(None)`（音轨全部来自 audio_events/BGM），
无音轨的空镜素材也能安全 subclip，ffmpeg 兜底路径不受影响。

共享构建器 `_build_timeline` 与渲染器 `_render_with_moviepy`（v2 API：subclipped/with_start/
with_effects(FadeIn·FadeOut)/AudioLoop/with_volume_scaled；TextClip 逐条 try/except——字幕失败
不算渲染失败）与 `_render_via_ffmpeg`（concat 兜底）。

### 3.5 `storyline_server/server.py` — FastMCP 动态注册
- `StorylineServer(settings)`：build_real_registry + Interceptor + FastMCP(streamable-http)。
- 每个节点 → 一个 MCP tool：动态伪造 `__signature__`（ctx 位参 + 节点 inputSchema 属性 +
  5 个公共参数 session_id/artifact_id/user_request/lang/mode，全部 KEYWORD_ONLY Optional，
  **不用 VAR_KEYWORD**——实测会把 kwargs 变成必填 schema 字段）。
- 会话路由：`X-Storyline-Session-Id` 头 → 参数 session_id → `storyline:default`；
  匿名但带 user_request 时用 `storyline:{request前16字}`。
- 跨调用状态一律走 PG（`artifacts` / `render_jobs` / `materials`），进程内不缓存——
  重启不丢产物，多实例看到的是同一份真相。
- 额外注册白名单外的 `read_node_history(key, …)` 供 CAPABILITY 技能读 Store，
  以及 `render_status(artifact_id, …)`——渲染改成提交+轮询后它是唯一的进度口，
  因此必须出现在 `[capabilities].available_nodes` 里：模型收到 hint 却无工具可点是一层后果，
  主服务侧的**代查**（§4 步骤 4）也按名字找它，找不到就直接放弃保证、退回「模型自己查」。
- 入口 `python run_storyline.py --config examples/storyline/config.toml`。

### 3.6 主 Agent 侧改动（最小）
- `agent_framework/tools/mcp.py`：StreamableHttpTransport 补齐规范——
  `accept: application/json, text/event-stream`；initialize 响应头捕获 `mcp-session-id`
  并在后续请求回带；`notifications/initialized` 真正 POST（202 空响应）；SSE `data:` 行解析。
  （修复联机 406 的根因。）
- `agent_framework/hooks.py`：新增 `MediaCardHook`，在文档 `after_execute_tools` 生命周期节点
  扫描 role="tool" 消息中的 `media_url`，去重后向 OutBound 投 `type=media` 帧。
  （`media_url` 现在是 presigned `http(s)` 直链，所以判据从 `startswith("/media/")` 放开为
  「绝对 URL」；FastAPI 的 `/media/{name}` 静态挂载与 `create_app(media_dirs=…)` 已退役。）
- `agent_framework/server.py`：`POST /upload`（原始字节流、分块读、`max_upload_mb` 上限、
  文件名净化+防撞名）**直写 MinIO + 登记 `materials`**，返回 `material_id` 与 presigned 回放
  URL，本地不再落一份素材字节；后加 `POST /fetch_media`（按链接取料）走同一条入库路径。
- `run_server.py`：storyline `tool_timeout` 改读 config（600s）；渲染已改成提交+轮询，
  这个预算不再用来兜「一次调用等一部片子」，只兜单次往返；接 MediaCardHook；
  `--max-upload-mb` / `--max-fetch-mb` 可调；`--storage pg_minio|memory`。
- `examples/storyline/config.toml`：原 `[media]` 四目录退役，改 `[storage]`；
  素材与 BGM 一律查 `materials` 表，不再扫任何目录。
- 前端 `App.vue`：`type=media` → 播放卡片（`<video controls>` + 时长 + 下载链接）；
  落笔栏「＋素材」/「＋链接」与整页拖拽上传，成功后素材以**附件卡片**挂在输入框上方
  （输入框里只留用户的话），随消息以 `attachments=["mat-…"]` 发出。dist 已重建。

## 4. 数据流（一次真实出片）

1. 用户「把这几条素材剪成 vlog」（附件以 `material_id` 挂在消息上）→ MQ InBound → AgentLoop。
2. LLM 依次调 `storyline_load_media → split_shots → … → render_video`（Interceptor 自动补依赖）。
3. 每节点产物写 PG `artifacts` 表；跨调用只靠这张表衔接，进程内存不缓存。
4. render_video 把 mp4 字节 publish 进 MinIO `renders/{会话}/`，进度与终态产物写 `render_jobs`；
   调用本身提交即回句柄，未完成时由 `render_status` 轮询（跨实例、跨重启都查得到），
   done 时现签 presigned `media_url` 回给调用方。
   **轮询的发起方是 Agent 循环而不是模型**：`AgentOnceRun._follow_inflight_renders` 在一轮工具跑完
   后检查返回值里有没有 `render.status ∈ {queued, running}`，有就自己按 `render_poll_sec` 调
   `render_status` 直到终态（预算 `render_follow_max_sec`），中间态只发进度帧不拼进上下文，
   终态那一条才进 messages；预算用尽时如实追加一条 system 说明「不要声称成片已完成」，
   绝不假称成功（README §3.10/§6，`tests/test_render_follow.py` + `.smoke/b15_render_follow_smoke.py`）。
5. 工具结果里出现 `media_url` 那一轮，MediaCardHook → MQ OutBound → ConnectionManager →
   该会话 WS → `<video>` 卡片可直接播放；同时持久链接落进本轮 assistant 行的 `qa.parts`，
   刷新历史时重放同一张卡（见 pg-minio spec §6）。
6. LLM 叙述走既有 delta/stream_end 流式链路，不受影响。

## 5. 错误处理与降级（degrade-not-fail）

- 任一 Provider 失败 → 该环节换确定性兜底（静音轨/蜂鸣 BGM 占位/字幕拼文案/模板兜底脚本），
  payload 里以 `source: "fallback"`、`warnings` 显式标注，不断链。
- MoviePy 渲染抛错 → ffmpeg concat 重编码兜底，仍保证 mp4 产出。
- 字幕单条渲染失败只丢该字幕。
- Storyline 连不上 → **不降级**：本服务没有剪辑能力（启动告警 + 工具表里没有任何剪辑节点），
  其余链路照常可用。客户端假节点曾作为兜底，2026-09-23 起退出生产装配，见 §2。
- 所有 ffmpeg 失败抛 `CalledProcessError`，stderr 尾 800 字符并入 MCPError 文本给 LLM 看。
- **降级的边界**（2026-09-20 spec §9）：只有外部能力（VL/TTS/LLM/ASR）失败才换兜底介质出片；
  **存储层故障不降级**——没有可信素材字节就不出片，绝不产出引用死链的"成功"。
- 外部能力还要过 `provider_timeout_sec`（默认 180s）：真机上 ASR 曾挂住 13 分钟且零 CPU，
  **卡死与报错同等对待**，否则一条链冻住整台服务。

## 6. 测试与验证证据（2026-09-19 全部通过）

> 本节是当时的**验收快照**，用例数此后只增不减；其中「FileStore 落盘」「`/media` 回放」
> 「上传落盘」三类断言已在 B2/B3 换成了 `artifacts` 表 / presigned URL / `materials` 行。
> 当前判据看 `README.md` 的「测试」一节与 `.smoke/` 里的真机冒烟。

- `test_storyline_nodes.py` 50/50：合成素材全 DAG 真渲染（真实 mp4 >10KB、640x360、字幕、
  BGM/配音文件、order_trace 11 节点、三时间线、真 xfade 转场、二次渲染选中 ai 版、
  FileStore 持久化、render_progress done/100、离线降级渲染仍出片）。
  2026-09-20 新增 9 项**原声混剪**检查：口播+空镜双素材场景下关键词自动启用与
  `keep_original_audio=True` 显式参数两条路径；断言音轨 src 区间指向源视频、字幕=ASR
  原文、画面轨排除口播素材、TTS 跳过、时间线/成片时长≈口播净时长 4.1s、成片含音轨。
- `test_storyline_server.py` 18/18：19+1 工具注册、schema 无 kwargs、公共参数、白名单、
  call_tool 打包契约、FileStore 跨调用读写、MediaCardHook 投帧与去重。
- `test_mcp.py` 26/26（新增 5 项 transport 规范检查：双 Accept、通知真投递、
  会话头捕获/回带、SSE 解析）。
- `test_server.py` 19/19（新增 9 项上传检查：落盘/字节完整/分目录/文件名净化/防撞名/
  413 超限/400 空文件//media/uploads 回放/未启用 404）。
- **联机**：:8001 握手成功列出 20 工具、注册 19；远程 `load_media` 真实 ffprobe 返回契约；
  `read_node_history` 跨调用读到落盘 payload。
- **整机**：重启 :8000 启动日志「Storyline 已接入 19 个远程剪辑节点，本地 mock 已移除」；
  （2026-09-23 起该行为「Storyline 已接入 N 个剪辑节点（DAG 契约 M 节点，rerun_from 可用）」，
  mock 已不在装配里，无需再报移除。）
  `GET /media/out/smoke.mp4` → 200 video/mp4；真实上传冒烟：`POST /upload` 落盘
  `.storyline/uploads/u-demo/c-demo/海边日落_test.mp4` → `/media/uploads/…` 200 回放 →
  远程 `search_media(query=海边)` 命中该文件（临时素材已清理）。
- 全量回归 exit 0（那一批时 27 个测试文件；2026-09-21 已增至 36 个）。
- 2026-09-20 **BGM 联机冒烟**：上传目录临时放入 `夏日_轻快.mp3` + 2s 测试视频 → 远程
  `select_BGM`（含真实 DeepSeek 文案依赖链，125s）返回 `{bgm: …夏日_轻快.mp3,
  source: "library"}`；离线新增 2 项曲库断言（test_storyline_nodes 52/52，含关键词排序）。
  临时文件已清理。

## 7. 已知边界与后续路线（不在本次范围）

- 转场仅 fade 系（xfade 样式表可扩展）；GPU 不可用时 whisper 自动 CPU+int8（慢但可用）
  ——真机实测本机 CUDA 缺 `cublas64_12.dll`，**实走 CPU+int8**，别按 GPU 预算估时。
- 本次（2026-09-19）说的「暂不部署」已被 2026-09-20 的上线存储层子项目推翻：
  PG + MinIO、`docker-compose.yml`、`.env`、`README.md` 部署章节均已落地。
- 后续：素材库索引缓存、渲染进度 WS 推送、批量导出、真实 Kafka 后端上线。

```
运行方式（两份终端，先起 Storyline）：
  docker compose up -d                                                # PG + MinIO
  copy .env.example .env                                              # 五项自己填
  python -u run_storyline.py --config examples/storyline/config.toml  # :8001
  python -u run_server.py --port 8000 --max-iterations 24             # :8000，自动接入
  模型密钥：打开页面右上「设置」填一次即可（写进 PG，两个服务热读，不用重启）；
  无头部署可退回 set OPENAI_API_KEY=…；两处都没有则 VL 与文案环节自动降级/报错。
  首次上线迁数据：python -u run_migrate.py（幂等，先 --dry-run 看对账表）
完整部署口径见仓库根 `README.md`。
```
