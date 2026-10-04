<script setup>
// 智能创作助手 · 前端主界面
// 数据流严格对应后端文档链路：
//   发送 → POST /chat（MQ InBound）；结果 → WS /ws/{conv}?token= 收
//   delta / stream_end / answer（MQ OutBound → Connection Manager 回投）。
// 身份（spec §7）：首次访问 POST /register 换一份 token，之后 HTTP 带 Bearer、
//   WS 带 ?token=；请求里不再出现 user_id——它由服务端从 token 反查。
import { reactive, ref, nextTick, onMounted, onBeforeUnmount } from "vue";

const TOKEN_KEY = "ca.token";
// 浏览器只留凭证：身份由 token 反查，会话列表与历史都从服务端读。
localStorage.removeItem("ca.user");
localStorage.removeItem("ca.convs");

function rid(n = 8) {
  return Math.random().toString(36).slice(2, 2 + n);
}

// 轻量 markdown：只解析 **加粗**，逐段渲染（不用 v-html，避免注入）。
function segs(text) {
  const parts = [];
  const re = /\*\*(.+?)\*\*/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push({ t: text.slice(last, m.index) });
    parts.push({ t: m[1], b: true });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ t: text.slice(last) });
  return parts;
}

const userId = ref("");                     // 只由服务端反查得到，本地不存身份
const token = ref(localStorage.getItem(TOKEN_KEY) || "");
const hadToken = ref(!!token.value);

function authHeaders(extra = {}) {
  return { Authorization: `Bearer ${token.value}`, ...extra };
}

// 凭证是身份的唯一来源：token 丢了就是新身份（旧历史仍在服务端那个用户名下，只有原 token 能找回）。
let identityPromise = null;
function ensureIdentity() {
  if (userId.value) return Promise.resolve(userId.value);
  if (!identityPromise) {
    identityPromise = (token.value ? whoami() : register()).catch((e) => {
      identityPromise = null;             // 失败不留半成品 promise，下次操作会重试
      throw e;
    });
  }
  return identityPromise;
}

async function whoami() {
  const r = await fetch("/whoami", { headers: authHeaders() });
  if (r.status === 401) {                 // 服务端认不得这张 token（库被清过）：换新的
    forgetToken();
    return register();
  }
  if (!r.ok) throw new Error("无法确认身份（HTTP " + r.status + "）");
  userId.value = (await r.json()).user_id;
  return userId.value;
}

async function register() {
  const r = await fetch("/register", { method: "POST" });
  if (!r.ok) throw new Error("无法取得身份（HTTP " + r.status + "）");
  const j = await r.json();
  token.value = j.token;                  // 明文 token 只在这一次响应里出现
  userId.value = j.user_id;
  hadToken.value = true;
  localStorage.setItem(TOKEN_KEY, j.token);
  identityPromise = null;
  return j.user_id;
}

function freshConv() {
  const id = (crypto.randomUUID && crypto.randomUUID()) || ("c-" + rid(10));
  return { id, title: "新对话", ts: Date.now() };
}

const convs = ref([]);
const active = ref("");

// 会话列表与历史都从服务端读：换浏览器 / 清缓存，只要 token 还在就接得上。
async function loadConvs() {
  let list = [];
  let failed = "";
  try {
    const r = await fetch("/convs", { headers: authHeaders() });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    list = (j.conversations || []).map((c) => ({
      id: c.conversation_id, title: c.title || "新对话",
      ts: Date.parse(c.updated_at) || Date.now(),
    }));
  } catch (e) {
    failed = e.message;
  }
  convs.value = list.length ? list : [freshConv()];
  await openConv(convs.value[0].id);
  if (failed) {
    msgs(active.value).push({ role: "assistant", text: "会话列表没读出来：" + failed, state: "error" });
  }
}

async function loadHistory(cid, force) {
  // force=true 用于**重连**：那时必须重拉一次。
  // 非 force 时按 histories 占位去重（首次打开会话的常规路径，避免重复拉）。
  if (!cid) return;
  if (histories[cid] && !force) return;
  histories[cid] = [];                       // 先占位，避免重复拉
  try {
    const r = await fetch(`/convs/${encodeURIComponent(cid)}/messages`, { headers: authHeaders() });
    if (!r.ok) return;
    const j = await r.json();
    const list = [];
    for (const m of (j.messages || [])) {
      list.push({
        role: m.role, text: m.text, state: "done", attachments: m.attachments || [],
      });
      // 刷新后重放的成片播放卡：/convs/{id}/messages 把当轮渲染的持久链接回投成 media 数组，
      // 用与实时 WS「media」帧同形的 {media_url,title,duration}，复用下面 kind==='media' 的卡片。
      for (const card of (m.media || [])) {
        list.push({
          role: "assistant", kind: "media", state: "done",
          text: card.title || "成片已渲染", url: card.media_url, duration: card.duration,
          evidence: card.evidence || [],
        });
      }
      // 刷新后重放的计划卡：待确认的那一份存在 assistant 行的 qa.parts 里，
      // 带 plan_run_id 才知道确认帧该发往哪条规划 run。
      for (const view of (m.plan || [])) {
        const card = planCardOf(view);
        if (card) { card.replayed = true; list.push(card); }
      }
      // 刷新后重放的对账角标：与实时 answer 之后补的那一条同形。
      if (m.plan_audit && ((m.plan_audit.extra || []).length + (m.plan_audit.unfulfilled || []).length)) {
        list.push({
          role: "assistant", kind: "audit", state: "done", run_id: "",
          plan_id: m.plan_audit.plan_id || "", extra: m.plan_audit.extra || [],
          unfulfilled: m.plan_audit.unfulfilled || [], reason: m.plan_audit.reason || "",
        });
      }
    }
    histories[cid] = list;
    scrollDown();
  } catch (_) {
    /* 回填失败不打断使用：新会话本来就还没有历史 */
  }
}

async function openConv(cid) {
  // 让位排队只在会话内有效：切走就清（挂起态在服务端，`checkActiveRun` 进会话时补弹）
  parkedCards.value = [];
  active.value = cid;
  await loadHistory(cid);
  await checkActiveRun(cid);
  openRun.value = null;
  preview.value = null;              // 换会话先清掉，避免显示上一个会话的编排
  if (activePanel.value === 'runs') await loadRuns();
  if (activePanel.value === 'plan') await loadPreview(cid);
  ensureSocket(cid);
}

async function checkActiveRun(cid) {
  try {
    const r = await fetch(`/convs/${cid}/runs/active`, { headers: authHeaders() });
    if (!r.ok) return;
    const j = await r.json();
    if (!j.run) return;
    if (j.run.status === "running") {
      busy[cid] = j.run.run_id;
      msgs(cid).push({
        role: "assistant", text: "⏳ 任务正在后台运行中，正在接续进度…", state: "streaming",
      });
      scrollDown();
      return;
    }
    // 挂起态：刷新/重连后要把选项卡**重新弹出来**。
    //
    // 服务端 /convs/{id}/runs/active 一直在回 approval（含结构化 ask），
    // 但前端原先只判 status === "running"，approval 一个字都没读——
    // 于是用户在等确认时一刷新，后端永久挂起、界面上没有任何可点的东西，
    // 只能整页重开。这里把服务端那份 approval 还原成 WS 帧的同一形状
    // （approvalBubble 就吃这个形状），复用同一条渲染路径。
    //
    // **必须去重**：questionCard 是单例，而 openQuestionCard 每次都整体重建
    // （answers/customAnswers 全清空）。这条路径在 **WS 每次重连**时都会跑
    // （reloadAfterReconnect → checkActiveRun），于是用户选到一半、
    // 连接一断一重，卡片就被重建、已选全丢——真机反馈正是
    // 「用户还没选完就跳另一个卡片，还要用户去找」。
    // 所以这里只认「这个 run + 这道题」还没弹过才弹：
    // 同一 run 的同一个 ask 再进来一律跳过，把选择留在用户手上。
    if (j.run.status === "awaiting_approval" && j.run.approval) {
      const ap = j.run.approval;
      const rid = String(ap.run_id || j.run.run_id || "");
      const title = String((ap.ask && ap.ask.title) || ap.reason || "");
      const sig = `${rid}\u0000${title}`;
      busy[cid] = j.run.run_id;

      // 这条路径**只负责"冷启动时把卡补回来"**（首次进会话 / 整页刷新）。
      // 去重规则两条：
      //  · 这道题已经补弹过 → 不再补（否则 WS 每次重连都会重建一次卡）；
      //  · 或者正开着一张同签名的卡（用户还没提交）→ 更不能重建。
      //
      // 为什么不去重会出事：questionCard 是单例，openQuestionCard 每次都整体重建
      // （answers/customAnswers 清空）。这条路径在 **WS 每次重连**时都跑
      // （reloadAfterReconnect → checkActiveRun），于是用户选到一半、连接一断一重，
      // 卡片就被重建、已选全丢——真机反馈正是
      // 「用户还没选完就跳另一个卡片，还要用户去找」。
      //
      // 新题不会因此漏弹：新题是一条**新的**签名，WS 与这条路径都会放行；
      // 两条路径共用同一套签名（run_id + 标题），所以谁先弹都不会互相打架。
      // 用户答完时 `decideApproval` 会清掉这个集合，换一版后被重新问到仍弹得出。
      const seen = askedCards[cid] || new Set();
      const same = questionCard.value
        && String(questionCard.value.run_id || "") === rid
        && String(questionCard.value.pages?.[0]?.title || "") === title;
      if (same || seen.has(sig)) return;
      seen.add(sig);
      askedCards[cid] = seen;
      approvalBubble(cid, {
        run_id: ap.run_id || j.run.run_id,
        reason: ap.reason || "",
        calls: ap.calls || [],
        fallback_options: ap.fallback_options || [],
        ask: ap.ask || undefined,
      });
    }
  } catch (_) { /* 查不到不影响正常使用 */ }
}

// 列表是异步拉的：手快的用户可能在它回来前就按了发送，先补上当前会话再说。
async function ensureActive() {
  if (!active.value) await loadConvs();
  return active.value;
}

function renameConv(cid, title) {
  fetch(`/convs/rename`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ conversation_id: cid, title }),
  }).catch(() => {});
}

// 每个会话的消息流：{role, text, state: done|streaming|error}
const histories = reactive({});
// 每个会话的 WS 与在途状态
const socks = {};
const busy = reactive({});   // convId -> 等待中的 run_id
const streamCur = {};        // convId -> 当前流式气泡
// convId -> 已经弹过的那道题签名。见 checkActiveRun 的说明：
// 它是「不要因为一次重连就把用户选到一半的卡片重建掉」的那道闸。
const askedCards = reactive({});
const connected = reactive({});
const draft = ref("");
const scroller = ref(null);

const toolLib = ref(null);
const loadingTools = ref(false);
// —— 工具库管理态：MCP 服务（动态注册）与技能库的增删改查 ——
const mcpServers = ref([]);
const loadingMcp = ref(false);
const mcpForm = ref(null);        // null=表单收起;{name,type,url,command,args,tool_timeout}
const mcpBusy = ref(false);
const mcpMsg = ref("");
const skillsAdmin = ref([]);
const loadingSkillsAdmin = ref(false);
const skillsMsg = ref("");
// 服务端词表（/tools 的 name_display）灌进来的 {机器名: 中文名}：界面标签的第一来源。
// 拉不到（离线装配 / Storyline 未连通）时退回 TOOL_LABELS，再退机器名本身。
const catalogLabels = ref({});
// /tools 的 params_display：参数名中文标签的**单源表**（帧里的 arg_labels 才是第一来源）。
const paramLabels = ref({});
const activePanel = ref(null);
const libraryItems = ref([]);
const loadingLibrary = ref(false);
const libSelected = reactive({});   // material_id -> true
const deletingLib = ref(false);

const timelineList = ref([]);
const editingTimeline = ref(null);
const loadingTimeline = ref(false);
const renderingTimeline = ref(false);
const renderProgress = ref(null);
const timelineError = ref("");

function libSelCount() {
  return Object.keys(libSelected).filter((k) => libSelected[k]).length;
}

function toggleLibSel(material_id) {
  libSelected[material_id] = !libSelected[material_id];
}

function clearLibSel() {
  for (const k of Object.keys(libSelected)) delete libSelected[k];
}

async function loadMaterials() {
  loadingLibrary.value = true;
  try {
    await ensureIdentity();
    const r = await fetch("/materials", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    libraryItems.value = j.materials || [];
    clearLibSel();
  } catch (e) {
    libraryItems.value = [];
  } finally {
    loadingLibrary.value = false;
  }
}

async function toggleLibrary() {
  activePanel.value = activePanel.value === 'library' ? null : 'library';
  if (activePanel.value === 'library') await loadMaterials();
}

// ---- 音乐库：GD Studio 在线搜索 + 试听 + 指定配乐 ----
const bgmItems = ref([]);
const bgmQuery = ref("");
const loadingBgm = ref(false);
const bgmHint = ref("");
const bgmError = ref("");
const importingBgm = ref("");
const bgmAudioUrl = ref({});
const bgmImported = ref([]);
const loadingBgmImported = ref(false);
const deletingBgm = ref("");

async function loadBgm() {
  loadingBgm.value = true;
  bgmError.value = "";
  bgmHint.value = "";
  bgmAudioUrl.value = {};
  try {
    await ensureIdentity();
    const q = bgmQuery.value.trim();
    const r = await fetch("/bgm/search" + (q ? "?q=" + encodeURIComponent(q) : ""), { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    bgmItems.value = j.tracks || [];
    bgmHint.value = j.hint || (q ? `搜索「${q}」` : "");
  } catch (e) {
    bgmItems.value = [];
    bgmError.value = e.message;
  } finally {
    loadingBgm.value = false;
  }
}

async function playBgm(item) {
  const key = item.track_id + "|" + item.source;
  if (bgmAudioUrl.value[key]) return;
  try {
    await ensureIdentity();
    const r = await fetch(`/bgm/url?track_id=${encodeURIComponent(item.track_id)}&source=${encodeURIComponent(item.source)}&name=${encodeURIComponent(item.name || "")}&artist=${encodeURIComponent(item.artist || "")}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    bgmAudioUrl.value = { ...bgmAudioUrl.value, [key]: j.url };
  } catch (e) {
    bgmError.value = "获取播放链接失败：" + e.message;
  }
}

async function toggleBgm() {
  activePanel.value = activePanel.value === 'bgm' ? null : 'bgm';
  if (activePanel.value === 'bgm') {
    await loadBgm();
    await loadBgmImported();
  }
}

// ---- 执行记录 / 时间旅行 / 分叉：后端那一整套 run 端点的前端口 ----
const runList = ref([]);
const loadingRuns = ref(false);
const runsError = ref("");
const openRun = ref(null);        // 展开了一致点链的那条 run
const runPoints = reactive({});    // run_id -> /runs/{id}/history 的 points
const forkDraft = reactive({ run_id: null, at_seq: null, point: null, nodes: [], message: "" });
const forking = ref(false);

const RUN_STATUS_TEXT = {
  running: "进行中", completed: "已完成", failed: "失败", superseded: "已让位",
};
function runStatusText(s) {
  return RUN_STATUS_TEXT[s] || s || "—";
}

function runTime(run) {
  const ms = Number(run.updated_at_ms || run.created_at_ms || 0);
  if (!ms) return "";
  return new Date(ms).toLocaleString("zh-CN", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function forkNodeOptions() {
  // PIPE_ORDER 在本文件后面才求值，所以只能在渲染期取，不能在模块顶层 map。
  return PIPE_ORDER.map((n) => ({ name: n, label: toolLabel(n) }));
}

async function toggleRuns() {
  activePanel.value = activePanel.value === 'runs' ? null : 'runs';
  if (activePanel.value === 'runs') await loadRuns();
}

async function loadRuns() {
  const cid = active.value;
  if (!cid) return;
  loadingRuns.value = true;
  runsError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/convs/${encodeURIComponent(cid)}/runs`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    runList.value = j.runs || [];
  } catch (e) {
    runList.value = [];
    runsError.value = e.message;
  } finally {
    loadingRuns.value = false;
  }
}

async function toggleRunHistory(run) {
  const cid = active.value;
  if (openRun.value === run.run_id) { openRun.value = null; return; }
  openRun.value = run.run_id;
  if (runPoints[run.run_id]) return;
  try {
    await ensureIdentity();
    const r = await fetch(`/runs/${encodeURIComponent(run.run_id)}/history`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    runPoints[run.run_id] = j.points || [];
  } catch (e) {
    runPoints[run.run_id] = { error: e.message };
  }
  if (active.value === cid) scrollDown();
}

function pointsOf(runId) {
  const v = runPoints[runId];
  return Array.isArray(v) ? v : [];
}

function pickForkPoint(run, pt) {
  // 点一个一致点 = 回到「这一步还没做」的时刻：这一步落下的节点正是要重做的那些。
  // （#0 是基准点，它之前没有东西可留，只能从它本身起。）
  const redo = (pt.tools || [])
    .map((n) => String(n).replace(/^storyline_/, ""))
    .filter((n) => PIPE_ORDER.includes(n));
  forkDraft.run_id = run.run_id;
  forkDraft.at_seq = pt.seq === 0 ? 0 : pt.seq - 1;
  forkDraft.point = pt.seq;
  forkDraft.nodes = redo;
  forkDraft.message = "";
}

function toggleForkNode(name) {
  const i = forkDraft.nodes.indexOf(name);
  if (i >= 0) forkDraft.nodes.splice(i, 1);
  else forkDraft.nodes.push(name);
}

function dropRunDraft(runId) {
  if (forkDraft.run_id === runId) {
    forkDraft.run_id = null; forkDraft.at_seq = null; forkDraft.point = null;
    forkDraft.nodes = []; forkDraft.message = "";
  }
}

function dropRunHistoryOf(cid) {
  // runPoints 按 run_id 存：会话没了，它名下的一致点链与分叉草稿不能悬着。
  for (const run of runList.value) {
    if (!String(run.session_id || "").endsWith(":" + cid)) continue;
    delete runPoints[run.run_id];
    dropRunDraft(run.run_id);
  }
}

// 分叉/续跑都只投一帧 MQ：真正执行的是消费侧，流式回投按 run_id 归并，
// 所以这里必须把服务端预先起好的子 run_id 认成当前在途的那条。
async function followQueuedRun(cid, j, note) {
  if (!j || !j.run_id) {
    msgs(cid).push({ role: "assistant", text: note + "（服务端没回 run_id）", state: "error" });
  } else {
    busy[cid] = j.run_id;
    msgs(cid).push({ role: "assistant", text: note, state: "done" });
    ensureSocket(cid);
  }
  scrollDown();
}

async function doFork(run) {
  const cid = active.value;
  if (forkDraft.point === null || forking.value) return;
  forking.value = true;
  runsError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/runs/${encodeURIComponent(run.run_id)}/fork`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        at_seq: forkDraft.at_seq,
        rerun_nodes: forkDraft.nodes.slice(),
        message: forkDraft.message,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    const n = forkDraft.nodes.length;
    await followQueuedRun(cid, j,
      `✂ 已回到 #${forkDraft.point} 之前分叉重跑` +
      (n ? `，重做 ${n} 个节点（连同其下游产物一起作废）` : "，上游产物全部复用") +
      `；新执行 ${j.run_id}`);
    dropRunDraft(run.run_id);
    openRun.value = null;
    activePanel.value = null;   // 关掉抽屉，让用户看得见接着往下流的进度
    loadRuns();
  } catch (e) {
    runsError.value = "分叉失败：" + e.message;
  } finally {
    forking.value = false;
  }
}

async function doResume(run) {
  const cid = active.value;
  if (forking.value) return;
  forking.value = true;
  runsError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/runs/${encodeURIComponent(run.run_id)}/resume`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      // 裸 POST 会被请求体校验判 422：这条端点要一个 RunResumeRequest，at_seq 留空即「从最新一致点」。
      body: "{}",
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    await followQueuedRun(cid, j, `↻ 从「${run.message || run.run_id}」的最新一致点续跑`);
    activePanel.value = null;
  } catch (e) {
    runsError.value = "续跑失败：" + e.message;
  } finally {
    forking.value = false;
  }
}

async function toggleTimeline() {
  activePanel.value = activePanel.value === 'timeline' ? null : 'timeline';
  if (activePanel.value === 'timeline') {
    await loadTimelineList();
  }
}

async function loadTimelineList() {
  loadingTimeline.value = true;
  timelineError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/timelines", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    timelineList.value = j.timelines || [];
  } catch (e) {
    timelineError.value = e.message;
  } finally {
    loadingTimeline.value = false;
  }
}

async function importLatestTimeline() {
  if (!active.value) return;
  timelineError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/latest_timeline?conv_id=${active.value}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    editingTimeline.value = {
      id: "",
      name: "从当前会话导入",
      payload: j.timeline,
      video_url: "",
      duration_sec: null,
      conv_id: active.value,
    };
  } catch (e) {
    timelineError.value = e.message;
  }
}

async function openTimeline(tl) {
  timelineError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/timelines/${tl.id}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    editingTimeline.value = j;
  } catch (e) {
    timelineError.value = e.message;
  }
}

async function saveTimeline() {
  if (!editingTimeline.value) return;
  timelineError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/timelines", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        id: editingTimeline.value.id || undefined,
        name: editingTimeline.value.name,
        payload: editingTimeline.value.payload,
        conv_id: editingTimeline.value.conv_id || active.value || undefined,
        video_url: editingTimeline.value.video_url || undefined,
        duration_sec: editingTimeline.value.duration_sec || undefined,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    editingTimeline.value.id = j.id;
    await loadTimelineList();
  } catch (e) {
    timelineError.value = e.message;
  }
}

async function pollRender(artifactId, convId) {
  // 渲染是分钟级的：一次请求不再等整部片子，改成按 (产物, 会话) 轮询任务视图。
  const deadline = Date.now() + 30 * 60 * 1000;
  for (;;) {
    if (Date.now() > deadline) throw new Error("渲染超过 30 分钟仍未结束，稍后在执行记录里看结果");
    await new Promise(res => setTimeout(res, 2000));
    const q = new URLSearchParams({ artifact_id: artifactId, conv_id: convId || "" });
    const r = await fetch(`/render_status?${q}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    renderProgress.value = j;
    if (j.status === "done" || j.status === "failed") return j;
  }
}

async function renderDirect() {
  if (!editingTimeline.value) return;
  renderingTimeline.value = true;
  timelineError.value = "";
  renderProgress.value = { status: "queued", percent: 0, stage: "queued" };
  try {
    await ensureIdentity();
    const convId = editingTimeline.value.conv_id || active.value || "";
    const r = await fetch("/render_direct", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        timeline: editingTimeline.value.payload,
        conv_id: convId,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    let view = j.output && j.render && j.render.status === "done" ? j.output : null;
    if (!view && j.video && j.media_url) view = j;          // 已跑完：/render_status 的扁平形状
    if (!view) {
      const art = (j.render && j.render.artifact_id) || j.artifact_id || "";
      if (!art) throw new Error("渲染提交未返回任务标识");
      const done = await pollRender(art, convId);
      if (done.status === "failed") throw new Error(done.error || "渲染失败");
      view = done;
    }
    editingTimeline.value.video_url = view.media_url;
    editingTimeline.value.duration_sec = view.duration;
    await saveTimeline();
    renderProgress.value = view;
  } catch (e) {
    timelineError.value = e.message;
  } finally {
    renderingTimeline.value = false;
  }
}

async function deleteTimeline(tl) {
  try {
    await ensureIdentity();
    const r = await fetch(`/timelines/${tl.id}`, { method: "DELETE", headers: authHeaders() });
    if (!r.ok) throw new Error("HTTP " + r.status);
    await loadTimelineList();
    if (editingTimeline.value && editingTimeline.value.id === tl.id) {
      editingTimeline.value = null;
    }
  } catch (e) {
    timelineError.value = e.message;
  }
}

async function loadBgmImported() {
  loadingBgmImported.value = true;
  try {
    await ensureIdentity();
    const r = await fetch("/bgm", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    bgmImported.value = j.bgm || [];
  } catch (e) {
    bgmImported.value = [];
  } finally {
    loadingBgmImported.value = false;
  }
}

function addBgmImported(item) {
  const cid = active.value;
  atts(cid).push({
    material_id: item.material_id, filename: item.filename, kind: "audio",
    url: item.url, bytes: item.bytes, duration: item.duration,
  });
  activePanel.value = null;
  scrollDown();
}

async function deleteBgmImported(item) {
  if (!confirm(`确定从曲库删除「${item.filename}」？`)) return;
  deletingBgm.value = item.material_id;
  bgmError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/bgm/${encodeURIComponent(item.material_id)}`, {
      method: "DELETE", headers: authHeaders(),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || ("HTTP " + r.status));
    }
    bgmImported.value = bgmImported.value.filter(x => x.material_id !== item.material_id);
  } catch (e) {
    bgmError.value = "删除失败：" + e.message;
  } finally {
    deletingBgm.value = "";
  }
}

async function useBgm(item) {
  importingBgm.value = item.track_id;
  bgmError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/bgm/import", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        track_id: item.track_id,
        source: item.source,
        name: item.name,
        artist: item.artist,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    const cid = active.value;
    atts(cid).push({
      material_id: j.material_id, filename: j.filename, kind: j.kind || "audio",
      url: j.url, bytes: j.bytes, duration: j.duration,
    });
    activePanel.value = null;
    scrollDown();
  } catch (e) {
    bgmError.value = "导入失败：" + e.message;
  } finally {
    importingBgm.value = "";
  }
}

function addFromLibrary(item) {
  const cid = active.value;
  atts(cid).push({
    material_id: item.material_id, filename: item.filename, kind: item.kind,
    url: item.url, bytes: item.bytes, duration: item.duration,
  });
  activePanel.value = null;
  scrollDown();
}

function addSelectedFromLibrary() {
  const ids = Object.keys(libSelected).filter((k) => libSelected[k]);
  if (!ids.length) return;
  const cid = active.value;
  for (const item of libraryItems.value) {
    if (ids.includes(item.material_id)) {
      atts(cid).push({
        material_id: item.material_id, filename: item.filename, kind: item.kind,
        url: item.url, bytes: item.bytes, duration: item.duration,
      });
    }
  }
  clearLibSel();
  activePanel.value = null;
  scrollDown();
}

async function deleteSelected() {
  const ids = Object.keys(libSelected).filter((k) => libSelected[k]);
  if (!ids.length || deletingLib.value) return;
  if (!confirm(`确定删除选中的 ${ids.length} 个素材？删除后不可恢复。`)) return;
  deletingLib.value = true;
  const failed = [];
  for (const id of ids) {
    try {
      await ensureIdentity();
      const r = await fetch(`/materials/${encodeURIComponent(id)}`, {
        method: "DELETE", headers: authHeaders(),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
    } catch (_) {
      failed.push(id);
    }
  }
  clearLibSel();
  await loadMaterials();
  deletingLib.value = false;
  if (failed.length) {
    msgs(active.value).push({
      role: "assistant", text: `${failed.length} 个素材删除失败，可能正在被使用。`,
      state: "error",
    });
    scrollDown();
  }
}

// 工具库：从后端拉取全部已注册工具、技能和 MCP 节点，中文展示作用说明。
async function loadTools() {
  if (toolLib.value) return;
  loadingTools.value = true;
  try {
    await ensureIdentity();
    const r = await fetch("/tools", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    toolLib.value = j;
    absorbCatalog(j);
  } catch (e) {
    toolLib.value = { error: e.message };
  } finally {
    loadingTools.value = false;
  }
}

// /tools 是词表在界面上的唯一入口：拿到一次就够，各处标签都查这一张表。
function absorbCatalog(j) {
  const next = { ...catalogLabels.value };
  for (const items of Object.values(j.groups || {})) {
    for (const t of items || []) {
      if (t && t.name && t.name_display) next[t.name] = t.name_display;
    }
  }
  for (const s of j.skills || []) {
    if (s && s.name && s.name_display) next[s.name] = s.name_display;
  }
  catalogLabels.value = next;
  if (j && j.params_display) paramLabels.value = j.params_display;
}

// 预热：进度卡与剪辑流水线不等用户打开工具库抽屉就能拿到中文名。
// 拉失败只是退回兜底表（工具卡片帧自带 tool_display，仍优先），不打断使用。
async function ensureCatalog() {
  try {
    await ensureIdentity();
    const r = await fetch("/tools", { headers: authHeaders() });
    if (r.ok) absorbCatalog(await r.json());
  } catch (_) {
    /* 词表预热失败：界面退回 TOOL_LABELS 兜底 */
  }
}

async function toggleHelp() {
  activePanel.value = activePanel.value === 'tools' ? null : 'tools';
  if (activePanel.value === 'tools') {
    await loadTools();
    loadMcpAdmin();
    loadSkillsAdmin();
  }
}

// ---- MCP 服务管理：配置落库，enable 即连 / disable 即断 ----
// 撞名与连不上服务端都如实报错（400/502），这里把 detail 原样展示。

async function loadMcpAdmin() {
  loadingMcp.value = true;
  try {
    await ensureIdentity();
    const r = await fetch("/mcp/servers", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    mcpServers.value = j.servers || [];
  } catch (e) {
    mcpMsg.value = "加载失败：" + e.message;
  } finally {
    loadingMcp.value = false;
  }
}

function editMcp(s) {
  mcpForm.value = s ? {
    name: s.name,
    type: (s.config && s.config.type) || "streamableHttp",
    url: (s.config && s.config.url) || "",
    command: (s.config && s.config.command) || "",
    args: ((s.config && s.config.args) || []).join(", "),
    tool_timeout: (s.config && s.config.tool_timeout) || 60,
  } : { name: "", type: "streamableHttp", url: "", command: "", args: "", tool_timeout: 60 };
  mcpMsg.value = "";
}

async function saveMcp() {
  const f = mcpForm.value;
  if (!f || !f.name.trim()) { mcpMsg.value = "名称必填"; return; }
  const config = { type: f.type, tool_timeout: Number(f.tool_timeout) || 60,
                   enabled_tools: ["*"] };
  if (f.type === "stdio") {
    config.command = f.command.trim();
    config.args = f.args.split(",").map((x) => x.trim()).filter(Boolean);
  } else {
    config.url = f.url.trim();
  }
  mcpBusy.value = true;
  try {
    const r = await fetch("/mcp/servers", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ name: f.name.trim(), config }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    mcpForm.value = null;
    mcpMsg.value = "已保存：" + j.saved + "（点「启用」才会连接）";
    await loadMcpAdmin();
  } catch (e) {
    mcpMsg.value = "保存失败：" + e.message;
  } finally {
    mcpBusy.value = false;
  }
}

async function toggleMcp(s) {
  mcpBusy.value = true;
  try {
    const op = s.live ? "disable" : "enable";
    const r = await fetch(`/mcp/servers/${encodeURIComponent(s.name)}/${op}`,
                          { method: "POST", headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    mcpMsg.value = op === "enable"
      ? `已连接，注册 ${(j.tools || []).length} 个工具`
      : "已断开";
    await loadMcpAdmin();
  } catch (e) {
    // detail 本身已是人话(如「连接失败:<urlopen error ...>」),不再叠加前缀
    mcpMsg.value = e.message;
  } finally {
    mcpBusy.value = false;
  }
}

async function delMcp(s) {
  if (!confirm(`删除 MCP 服务 ${s.name}？（在线会先断开）`)) return;
  const r = await fetch(`/mcp/servers/${encodeURIComponent(s.name)}`,
                        { method: "DELETE", headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { mcpMsg.value = "删除失败：" + (j.detail || r.status); return; }
  mcpMsg.value = "已删除：" + j.deleted;
  await loadMcpAdmin();
}

// ---- 技能库管理：上传 zip / 重扫导入目录 / 删除 ----

async function loadSkillsAdmin() {
  loadingSkillsAdmin.value = true;
  try {
    await ensureIdentity();
    const r = await fetch("/skills", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    skillsAdmin.value = j.skills || [];
  } catch (e) {
    skillsMsg.value = "加载失败：" + e.message;
  } finally {
    loadingSkillsAdmin.value = false;
  }
}

function pickSkillZip() {
  const el = document.getElementById("skill-zip");
  if (el) el.click();
}

async function uploadSkillZip(ev) {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (!f) return;
  skillsMsg.value = "上传中…";
  const r = await fetch(`/skills/upload?filename=${encodeURIComponent(f.name)}`,
                        { method: "POST", headers: authHeaders(),
                          body: await f.arrayBuffer() });
  const j = await r.json().catch(() => ({}));
  skillsMsg.value = r.ok
    ? "已导入：" + (j.imported || []).join(", ")
    : "失败：" + (j.detail || r.status);
  await loadSkillsAdmin();
}

async function reloadSkillsDir() {
  skillsMsg.value = "重扫中…";
  const r = await fetch("/skills/reload", { method: "POST", headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  skillsMsg.value = r.ok
    ? "已重扫：" + (j.imported || []).join(", ")
    : "失败：" + (j.detail || r.status);
  await loadSkillsAdmin();
}

async function delSkill(s) {
  if (!confirm(`删除技能 ${s.name}？附件一并删除，不可撤销。`)) return;
  const r = await fetch(`/skills/${encodeURIComponent(s.name)}`,
                        { method: "DELETE", headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  skillsMsg.value = r.ok ? "已删除：" + j.deleted : "失败：" + (j.detail || r.status);
  await loadSkillsAdmin();
}

function msgs(cid) {
  if (!histories[cid]) histories[cid] = [];
  return histories[cid];
}

function wsUrl(cid) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // 浏览器 WebSocket 带不了自定义头，所以凭证走查询串；user_id 由服务端反查
  return `${proto}://${location.host}/ws/${encodeURIComponent(cid)}?token=${encodeURIComponent(token.value)}`;
}

function forgetToken() {
  if (!hadToken.value) return;         // 本来就没凭证：不是「失效」，是还没注册过
  hadToken.value = false;
  localStorage.removeItem(TOKEN_KEY);
  token.value = "";
  userId.value = "";
}

const _reconnectAttempts = {};
const _MAX_RECONNECT = 5;

function ensureSocket(cid) {
  if (!token.value) {
    // 先拿到凭证再连，否则握手必被拒（服务端不 accept 无凭证的连接）
    ensureIdentity().then(() => ensureSocket(cid)).catch(() => {});
    return null;
  }
  const s = socks[cid];
  if (s && (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING)) return s;
  const ws = new WebSocket(wsUrl(cid));
  let opened = false;
  ws.onopen = () => {
    const wasReconnect = opened === false && _reconnectAttempts[cid] > 0;
    opened = true;
    connected[cid] = true;
    _reconnectAttempts[cid] = 0;
    if (wasReconnect) {
      // 重连后必须**重拉一次历史**：服务端的环形缓冲只保证「还在跑的那一轮」的增量帧，
      // 终答落定那几秒里断连的话，只靠重放拿不全。原先这里只重开 socket，
      // 而 loadHistory 又被 histories 占位挡住 → 答复永久不显示，必须整页刷新。
      // 服务端重放的帧都带 replayed:true，历史与缓冲重叠时由 onFrame 去重。
      reloadAfterReconnect(cid);
    }
  };
  ws.onclose = () => {
    connected[cid] = false;
    delete socks[cid];
    // 不在这里 forgetToken：连不上可能是服务重启中，不是 token 失效。
    // token 真无效时 whoami 返回 401 会处理。
    if (busy[cid]) {
      // 任务在途：尝试自动重连，不急着报错
      const attempt = (_reconnectAttempts[cid] || 0) + 1;
      _reconnectAttempts[cid] = attempt;
      if (attempt <= _MAX_RECONNECT) {
        const delay = Math.min(1000 * 2 ** (attempt - 1), 16000);
        setTimeout(() => ensureSocket(cid), delay);
      } else {
        msgs(cid).push({ role: "assistant", text: "连接已断开，重连失败。任务仍在后台运行，刷新页面可再次尝试接续。", state: "error" });
        streamCur[cid] = null;
      }
    }
  };
  ws.onmessage = (e) => onFrame(cid, JSON.parse(e.data));
  socks[cid] = ws;
  return ws;
}

// 重连之后把这一轮的真相拉齐：历史（终答/成片卡/计划卡）+ 在途 run。
//
// 为什么必须做：服务端的环形缓冲只保「还没落定那几秒」的增量，而且它存的是原帧；
// 断连恰好落在**收尾那一刻**时，答复可能一帧都没推到浏览器。原先重连只重开 socket，
// 而 loadHistory 被 histories 占位挡住不重拉 → 界面上永远看不到那条答复，
// 只能整页刷新。这里重连后强制重拉一次，并把在途 run 的状态也校准回来。
async function reloadAfterReconnect(cid) {
  try {
    await loadHistory(cid, true);
    await checkActiveRun(cid);
    // 重拉历史会重建消息列表：正在流式的那条气泡（如果有）要重新挂回列表尾部，
    // 否则后续 delta 会往一个已经不在列表里的对象上追加，界面上看不到字。
    if (streamCur[cid]) {
      const sc = streamCur[cid];
      const list = msgs(cid);
      if (!list.includes(sc)) list.push(sc);
    }
    scrollDown();
  } catch (_) { /* 重拉失败不影响继续用：后续帧照旧 */ }
}

// 模型在一轮对话里调 rerun_from 分叉时，服务端会把在途流的 run_id 就地换成子 run 的// （agent._adopt_fork）。这条 tool_result 是换绑前最后能收到的一帧：认下新 run_id，
// 否则后续 delta/tool/media/answer 全被下面的 run_id 守卫丢掉，界面停在流式状态不动。
function adoptForkedRun(cid, p) {
  if (!p || !p.tool) return;
  const bare = String(p.tool || "").replace(/^storyline_/, "");
  if (bare !== "rerun_from" || p.error) return;
  // result 是被截断过的字符串化结果（不是 JSON），两种引号都容一下。
  const m = /["']run_id["']\s*:\s*["']([0-9a-zA-Z_-]{6,})["']/.exec(String(p.result || ""));
  const child = m && m[1];
  if (!child || busy[cid] === child) return;
  busy[cid] = child;
  if (streamCur[cid]) { streamCur[cid].state = "done"; streamCur[cid] = null; }
  msgs(cid).push({
    role: "assistant", state: "done",
    text: `✂ 助手已回到某一步重跑，后续进度接着往这条新执行（${child}）里流。`,
  });
  if (activePanel.value === 'runs') loadRuns();
}

// ---- 计划卡（块 B-4）：候选计划只读展示 + 点击编译成确认帧 --------------------
//
// 实时那一帧（WS type=plan）与刷新后那一份（/convs/{id}/messages 的 plan 数组）形状
// 相同，都是服务端通过四重校验的那一份。前端从不回传计划本体：确认帧只回答
// 「选了哪张卡、动了哪些开关、另填了什么」，服务端按 plan_run_id 取回承诺。

const PLAN_VALUE_TEXT = { true: "开", false: "关" };

// 计划确认弹窗：待确认的计划卡自动弹出，用户操作后收起；消息流里留一条轻量状态行。
const planModal = ref(null);

function openPlanModal(card) {
  if (!card || card.state !== "waiting") return;
  planModal.value = card;
  activePanel.value = 'plan';
  planAskCard(card);
  // 打开计划面板顺手拉一次编排预览：用户一边看链路一边就能核对待渲的编排
  loadPreview(active.value);
}

// 计划卡 → 统一提问卡：第一页选版本，后面每页选一步的参数。
//
// 这样计划确认也走截图那种弹窗形态，而不是只能打字。参数页会把
// 既有的 param_options 枚举接回来——原先那段 UI 被删掉后，
// confirmFrameOf 的 overrides 永远是空的，用户没法表达「我要别的值」。
function planAskCard(card) {
  const pages = [];
  if (card.plans.length > 1) {
    pages.push(questionPage("用哪一版方案？", card.plans.map((p) => ({
      key: p.plan_id, label: p.label || p.plan_id,
      description: p.goal || "", recommended: p.plan_id === card.plans[0].plan_id,
    })), { allowCustom: false }));
  } else if (card.plans.length === 1) {
    // 只有一版就不必问「选哪版」，直接确认即可
  }
  for (const s of planChosen(card).steps || []) {
    for (const o of s.params) {
      if (!o.options || !o.options.length) continue;
      const k = `${card.chosen}:${s.seq}:${o.key}`;
      pages.push(questionPage(
        `第 ${s.seq} 步「${s.node_display}」的 ${o.display}？`,
        o.options.map((v) => ({
          key: String(v.value), label: v.label,
          recommended: v.value === o.default,
        })),
        { paramKey: k, allowCustom: true, stepSeq: s.seq }));
    }
  }
  if (!pages.length) {
    pages.push(questionPage(card.plans[0].label || "确认这版方案？", [
      { key: "go", label: "按此执行", description: "按上面的链路开始执行", recommended: true },
      { key: "revise", label: "换一版", description: "先不开跑，我想调整方案" },
    ], { allowCustom: false }));
  }
  return openQuestionCard(pages, {
    origin: "plan_confirm", card, submitLabel: "按此执行",
    // 签名用**规划轮的 run**（帧上的那个）：冷启动补弹 / 重连补弹拿它跟服务端比，
    // 用 plan_run_id 只有服务端跑同一条 run 时才碰得上。
    run_id: card.frame_run_id || card.plan_run_id,
  });
}

function closePlanModal() {
  planModal.value = null;
}

// ---- 编排预览（侧边栏）----
// 渲染前的编排结果：分镜/分组/画面描述/人声/字幕/配乐/出镜比例 + 偏差提醒。
// 只读端点，随时可刷；不触发任何剪辑动作。
const preview = ref(null);
const pvLoading = ref(false);

async function loadPreview(cid) {
  const conv = cid || active.value;
  if (!conv) { preview.value = null; return; }
  pvLoading.value = true;
  try {
    await ensureIdentity();
    const r = await fetch(`/preview/${encodeURIComponent(conv)}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    // 只认当前会话的结果：慢响应回来时用户可能已经切走了
    if (conv === active.value) preview.value = r.ok ? j : null;
  } catch {
    if (conv === active.value) preview.value = null;
  } finally {
    pvLoading.value = false;
  }
}

// 轨道条宽度：占成片时长的百分比（成片时长未知时给个 0，不猜）
function pctOf(seconds) {
  const total = Number(preview.value?.timeline?.duration || 0);
  if (!total) return "0%";
  const p = Math.max(0, Math.min(100, (Number(seconds || 0) / total) * 100));
  return `${p}%`;
}

// HITL 审批弹窗：撞上需人工确认的工具时弹出，用户批准/拒绝后从断点续跑。
const approvalModal = ref(null);

// ---- 统一提问卡（截图那种形态）：编号选项 + 推荐徽标 + 铅笔自定义 + 分页 ----
//
// 一处组件服务四种来源，避免四套长得不一样的问答 UI：
//   ① ask_user     模型主动提问      → page 由 ask.options 组成
//   ② plan_confirm 计划卡确认        → page 由各版本 + 每步参数组成
//   ③ preview_gate 渲染前确认        → page 由编排预览的决策组成
//   ④ fallback     渲染兜底选项      → page 由 fallback_options 组成
//
// 设计取舍：弹窗只装「要用户拍板的那几题」，完整编排细节一律放右侧侧边栏
// （用户明确要求「弹窗问决策，侧边栏看详情」）。
const questionCard = ref(null);

// 让位排队的卡：同一时刻只渲染一张提问卡，但服务端可能**已经**在问另一件事。
//
// 真机事故（用户原话「怎么我第一个弹窗的问题还没选完，就继续了然后跳第二次弹窗，
// 第一次都没提交啊」）：规划轮交卡后自动弹的是计划确认卡（origin=plan_confirm），
// 模型那边又被 ask_gate 误判、追加了一条 ask_user 提问帧；两条消息各弹一次，
// 后一张卡把前一张整体覆盖（answers/customAnswers 全没了），用户选到一半的计划卡
// 就这么消失，接着点「确认」续跑的是那条提问，流程整个对不上。
//
// 所以：新卡不让它白白吃掉旧卡——旧卡整份（含已选答案）进队列，等当前这张提交或关掉
// 之后再弹回来。队列只在会话内有效，切会话时清空（挂起态在服务端，`checkActiveRun`
// 在进会话时会把该弹的卡补回来，不会漏）。
const parkedCards = ref([]);

// 当前卡走完了（提交成功 / 用户主动关掉）→ 把让位的那张还回来。
// 只在前一张真的提交了之后才还：`decideApproval` / `confirmPlan` 里都是先确认
// 「这张就是我刚提交的那张」再清空，所以不会把用户还没提交的卡挤走。
function restoreParkedCard() {
  if (questionCard.value) return;
  const next = parkedCards.value.shift();
  if (next) questionCard.value = next;
}

const RECOMMEND_LABEL = "推荐";

// 把一道题整理成一页：选项统一成 {index, key, label, description, recommended}
function questionPage(title, options, extra = {}) {
  const opts = (options || []).map((o, i) => ({
    index: i + 1,
    key: String(o.key ?? i + 1),
    label: o.label || `选项 ${i + 1}`,
    description: o.description || "",
    recommended: !!o.recommended,
    badge: o.badge || "",
  }));
  return {
    title, options: opts, custom: "", customOpen: false,
    customHint: extra.customHint || "其他（自己写一句）",
    allowCustom: extra.allowCustom !== false,
    // 多选时暂存勾选；单选时点一下即为选择
    multi: !!extra.multi, picked: [],
    ...extra,
  };
}

// 一次提问最多几页：与后端 ask_user 的选项上限对齐，避免弹窗变成问卷
function openQuestionCard(pages, meta = {}) {
  const list = (pages || []).filter(Boolean);
  if (!list.length) return null;
  const cur = questionCard.value;
  const origin = meta.origin || "ask";
  // 同一道题被再问一次（重连补弹、同一条帧重放）不重开：重建会把已选的答案洗掉。
  // 签名与 WS 帧 / checkActiveRun 那两处一致：同一个 run + 同一道题。
  // 计划卡例外——同一张卡重开是用户主动要刷新题面（侧边栏改了版本再点「查看并确认」），
  // 题面必须跟着 card.chosen 重算，所以照旧重建。
  const sig = (c) => c ? `${String(c.run_id || "")}\u0000${
    String(c.pages?.[0]?.title || "")}` : "";
  const mine = `${String(meta.run_id || "")}\u0000${String(list[0]?.title || "")}`;
  if (cur && !(meta.card && cur.card === meta.card)
      && cur.origin === origin && sig(cur) === mine) {
    return cur;
  }
  // 还有别的卡开着 → 让位排队（整份留着，含用户已经选好的答案）。
  // `submitting` 的那张不能动：它的提交回调认的是自己那张对象的身份，
  // 换掉之后回调就清不掉了（卡会一直挂着）。
  if (cur && !cur.submitting) parkedCards.value.push(cur);
  questionCard.value = {
    pages: list, page: 0,
    origin: meta.origin || "ask",      // ask_user / plan_confirm / preview_gate / fallback
    run_id: meta.run_id || "",
    reason: meta.reason || "",
    submitLabel: meta.submitLabel || "下一题",
    submitting: false, error: "",
    answers: {},                        // pageIndex -> 选中的 key
    customAnswers: {},                  // pageIndex -> 自定义文本
    previewSummary: meta.previewSummary || null,
    // 审批类弹窗（ask_user / 渲染确认 / 兜底选项）要把服务端那份 approval 带在身上：
    // qSubmit 提交时取的就是它，缺了这个字段，点「确认」会抛
    // 「Cannot set properties of undefined」，审批请求根本发不出去。
    approval: meta.approval || null,
    card: meta.card || null,            // plan_confirm 时挂回原计划卡
  };
  return questionCard.value;
}

function qCurrentPage() {
  const c = questionCard.value;
  return c ? c.pages[c.page] : null;
}

function qPick(pageIndex, optionKey) {
  const c = questionCard.value;
  if (!c) return;
  const page = c.pages[pageIndex];
  if (!page) return;
  if (page.multi) {
    const i = page.picked.indexOf(optionKey);
    if (i >= 0) page.picked.splice(i, 1); else page.picked.push(optionKey);
    return;
  }
  c.answers[pageIndex] = optionKey;
  page.customOpen = false;              // 选了枚举项就收起自定义输入
  page.custom = "";
}

function qToggleCustom(pageIndex) {
  const c = questionCard.value;
  const page = c?.pages[pageIndex];
  if (!page) return;
  page.customOpen = !page.customOpen;
  if (page.customOpen) c.answers[pageIndex] = "__custom__";
}

function qAnswerOf(pageIndex) {
  const c = questionCard.value;
  const page = c?.pages[pageIndex];
  if (!c || !page) return "";
  const free = String(page.custom || "").trim();
  if (page.customOpen && free) return free;
  if (page.multi) return page.picked.join(",");
  return c.answers[pageIndex] || "";
}

function qAnswered(pageIndex) {
  return !!qAnswerOf(pageIndex);
}

function qLastPage() {
  const c = questionCard.value;
  return !c || c.page >= c.pages.length - 1;
}

function qNext() {
  const c = questionCard.value;
  if (!c || qLastPage()) return;
  c.page += 1;
}

function qPrev() {
  const c = questionCard.value;
  if (!c || c.page <= 0) return;
  c.page -= 1;
}

function qClose() {
  // 收起弹窗不等于放弃：挂起状态在服务端，刷新或重连后会重新弹出来
  questionCard.value = null;
  // 这张是被别的卡顶掉后一直等着的？用户主动关掉当前这张，就把它还回来。
  restoreParkedCard();
}

function qFullscreen() {
  const c = questionCard.value;
  if (c) c.fullscreen = !c.fullscreen;
}

async function qSubmit() {
  const c = questionCard.value;
  if (!c) return;
  if (!qLastPage()) { qNext(); return; }
  c.submitting = true;
  c.error = "";
  try {
    if (c.origin === "plan_confirm" && c.card) {
      // 计划确认：把每页答案回填到计划卡的自定义项，再走既有确认帧
      const card = c.card;
      c.pages.forEach((p, i) => {
        const ans = qAnswerOf(i);
        if (!p.paramKey) return;
        const k = p.paramKey;
        const opt = p.options.find((o) => o.key === ans);
        if (opt) { card.final[k] = opt.rawValue ?? opt.key; card.custom[k] = ""; }
        else if (String(ans).trim()) { card.custom[k] = String(ans).trim(); }
      });
      await confirmPlan(card);
    } else {
      // ask / preview_gate / fallback：答案经审批续跑回喂给服务端
      //
      // 关键守口：只提交**属于当前这道题**的 key。
      // 真机事故：客户端拿上一题的 key 去答下一题（问时长、答的是风格），
      // 服务端照单全收，于是同一道题被反复"答"、run 原地打转。这里逐页核对
      // 选项清单，不属于本页的一律不提交（宁可让用户重新点，也不送脏答案）。
      const answers = c.pages.map((p, i) => {
        const raw = String(c.answers[i] || "");
        const custom = String(p.custom || "").trim();
        const belongs = (p.options || []).some((o) => String(o.key) === raw);
        const key = belongs ? raw : "";
        const ans = custom || (belongs ? qAnswerOf(i) : "");
        return { page: i, title: p.title, answer: ans, key, custom };
      });
      const unanswered = answers.findIndex((a) => !a.answer);
      if (unanswered >= 0) {
        c.submitting = false;
        c.error = `第 ${unanswered + 1} 题还没选（这题可能是刚弹出来的）——请点一个选项再提交。`;
        return;
      }
      const first = answers[0] || {};
      await decideApproval(c.approval, first.answer || "approve", answers);
    }
    c.submitting = false;
  } catch (e) {
    c.submitting = false;
    c.error = String(e && e.message ? e.message : e);
  }
}

// ask 帧 → 统一提问卡。服务端可以一帧带多道题（questions[]），也可以只带一道。
// `frameRunId` 是帧上那条 run（挂起的就是它）；`runId` 是审批卡自己的记录，缺了才回落。
function askQuestionCard(src, runId, frameRunId) {
  const pages = [];
  const a = src.ask || {};
  const rawQuestions = Array.isArray(a.questions) && a.questions.length
    ? a.questions : [a];
  for (const q of rawQuestions) {
    if (!q || !(q.title || q.question)) continue;
    pages.push(questionPage(q.title || q.question, q.options || [], {
      allowCustom: q.allow_custom !== false && a.allow_custom !== false,
      customHint: q.custom_hint || a.custom_hint || "其他（自己写一句）",
      multi: !!(q.multi || a.multi),
    }));
  }
  // 没有结构化 ask 时，退回「批准 / 拒绝」，仍是同一个弹窗组件
  if (!pages.length) {
    const fb = (src.fallback_options || []).map((o) => ({
      key: o.key, label: o.label, description: o.description,
      recommended: !!o.recommended,
    }));
    if (fb.length) {
      pages.push(questionPage(src.reason || "渲染需要你选择一个方案", fb));
    } else {
      pages.push(questionPage(src.reason || "以下操作需要你确认后才会执行", [
        { key: "approve", label: "批准执行", description: "按上面列出的内容继续", recommended: true },
        { key: "reject", label: "拒绝并跳过", description: "不执行这一步，继续往下走" },
      ]));
    }
  }
  return openQuestionCard(pages, {
    origin: src.origin || "ask",
    // 优先用审批卡自己那份 run_id：`plan_run_id` 是计划卡上的指针，
    // 提问卡要跟服务端挂起的那条 run 对齐才能配上签名（否则重连时被当新题重弹）。
    run_id: runId || frameRunId || src.plan_run_id || src.run_id || "",
    reason: src.reason || "", approval: src,
    submitLabel: pages.length > 1 ? "全部确认" : "确认",
  });
}

function approvalBubble(cid, src) {
  const fbOpts = (src.fallback_options || []).map((o) => ({
    key: o.key || "", label: o.label || o.key || "",
    description: o.description || "",
  }));
  const card = {
    run_id: src.run_id || "",
    calls: (src.calls || []).map((c) => ({
      id: c.id || "", name: c.name || "", arguments: c.arguments || {},
    })),
    reason: src.reason || "以下操作需要你确认后才会执行",
    fallback_options: fbOpts,
    state: "waiting",       // waiting → decided
    decision: "",           // approve / reject / option key
    decision_label: "",     // 显示用：用户选的按钮文案
    submitting: false,
    error: "",
  };
  const list = msgs(cid);
  // text 不能省：消息列表的模板没有 kind==='approval' 的专门分支，这条会落到默认
  // 的富文本分支，而那个解析器读 undefined.length 会抛 TypeError，
  // 一抛就把整条消息流的这次更新中断掉（表现：挂起气泡根本不出现）。
  list.push({ role: "assistant", kind: "approval", text: card.reason, card, state: "waiting" });
  scrollDown();
  // 走统一提问卡：结构化 ask 优先，没有就退化成「批准 / 拒绝」，同一个弹窗组件。
  // 第二参用**帧上的 run_id**，跟 WS 去重、冷启动补弹那两处的签名对齐。
  askQuestionCard(src, card.run_id, src.run_id);
  card.state = "asking";
}

async function decideApproval(card, decision, answers) {
  const cid = active.value;
  card.submitting = true;
  card.error = "";
  try {
    await ensureIdentity();
    let label;
    if (card.fallback_options && card.fallback_options.length) {
      const opt = card.fallback_options.find((o) => o.key === decision);
      label = opt ? opt.label : decision;
    } else if (decision === "approve" || decision === "reject") {
      label = decision === "approve" ? "批准执行" : "拒绝并跳过";
    } else {
      label = decision;      // 结构化提问：用户选的就是那句话本身
    }
    // 多题时把「第几题 = 选了什么」一并回给服务端，续跑时模型能看到完整选择
    const detail = (answers || []).length > 1
      ? (answers || []).map((a) => `「${a.title}」→ ${a.answer}`).join("；")
      : "";
    const r = await fetch(`/runs/${encodeURIComponent(card.run_id)}/approve`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ decision, message: detail || label, answers: answers || [] }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    card.state = "decided";
    card.decision = decision;
    card.decision_label = label;
    if (approvalModal.value === card) approvalModal.value = null;
    if (questionCard.value && questionCard.value.approval === card) {
      questionCard.value = null;
      restoreParkedCard();     // 这张交掉了，把先前让位排队的那张还回来
    }
    // 清掉这个会话的「已补弹」标记：答完之后同一道题若被重新问到（换一版/重试），
    // 冷启动兜底还要能把它补出来，不能被上面的去重挡住。
    delete askedCards[cid];
    msgs(cid).push({ role: "user", text: detail || label, state: "done", attachments: [] });
    busy[cid] = j.run_id || card.run_id || null;
    scrollDown();
  } catch (e) {
    card.error = e.message;
    throw e;
  } finally {
    card.submitting = false;
  }
}

function planCardOf(src) {
  const plans = (src.plans || []).map((p) => ({
    plan_id: String(p.plan_id || ""),
    label: p.label || "",
    goal: p.goal || "",
    steps: (p.steps || []).map((s) => ({
      seq: s.seq,
      node_display: s.node_display || toolLabel(s.node),
      why: s.why || "",
      expectation: s.expectation || "",
      skippable: !!s.skippable,
      skills: (s.skills_hint_display || [])
        .concat((s.skills_hint || []).map((n) => toolLabel(n))),
      params: (s.param_options || []).map((o) => ({
        key: o.key,
        display: o.display || toolLabel(o.key),
        default: o.default,
        unit: o.unit || "",
        options: (o.options || []).map((v) => ({
          value: v.value,
          label: v.display || PLAN_VALUE_TEXT[v.value]
            || `${v.value}${o.unit ? " " + o.unit : ""}`,
        })),
      })),
    })),
  }));
  if (!plans.length) return null;
  const card = {
    role: "assistant", kind: "plan", state: "waiting",
    plan_run_id: src.plan_run_id || src.run_id || "",
    // 帧上的 run_id（= 规划轮的 run）：提问卡的签名、冷启动补弹都按它对齐
    frame_run_id: src.run_id || "",
    plans, warnings: src.warnings || [],
    chosen: plans[0].plan_id,
    final: {}, custom: {}, skip: {}, general: "",
    feedback: "", askFeedback: false, submitting: false, error: "", exec_run: "",
  };
  for (const p of plans) {
    for (const s of p.steps) {
      card.skip[`${p.plan_id}:${s.seq}`] = false;
      for (const o of s.params) card.final[`${p.plan_id}:${s.seq}:${o.key}`] = o.default;
    }
  }
  return card;
}

// 推进消息流并返回**响应式那一份**：直接改 push 进去的原对象不会触发重渲染。
function planBubble(cid, src) {
  const card = planCardOf(src);
  if (!card) return null;
  const list = msgs(cid);
  list.push(card);
  scrollDown();
  const rc = list[list.length - 1];
  openPlanModal(rc);   // 待确认的计划自动弹窗
  return rc;
}

function planChosen(card) {
  return card.plans.find((p) => p.plan_id === card.chosen) || card.plans[0] || { steps: [] };
}

// 模板里的键与状态：与 PlanGate 的上限对齐（自定义 ≤5 条，每条 ≤200 字）。
const CUSTOM_MAX_ITEMS = 5;
const CUSTOM_MAX_CHARS = 200;
const pkey = (card, seq, key) => `${card.chosen}:${seq}:${key}`;
const skey = (card, seq) => `${card.chosen}:${seq}`;
const hasFree = (card, seq, key) => !!String(card.custom[pkey(card, seq, key)] || "").trim();
const planLocked = (card) => card.state !== "waiting";
function customCount(card) {
  let n = Object.values(card.custom).filter((v) => String(v || "").trim()).length;
  if (String(card.general || "").trim()) n += 1;
  return n;
}

// 卡面选择 → 确认帧。「其他…」与同组枚举同时给了值时以「其他」为准（doc §8）：
// 那是用户刚刚放弃的承诺，也不回落默认值。
function confirmFrameOf(card) {
  const pid = card.chosen;
  const param_finals = [], skips = [], overrides = [];
  for (const s of planChosen(card).steps || []) {
    for (const o of s.params) {
      const k = `${pid}:${s.seq}:${o.key}`;
      const free = String(card.custom[k] || "").trim();
      if (free) {
        overrides.push({ step_seq: s.seq, key: o.key, kind: "custom_text", value: free });
      } else {
        param_finals.push({ step_seq: s.seq, key: o.key, value: card.final[k] });
      }
    }
    if (card.skip[`${pid}:${s.seq}`]) skips.push({ step_seq: s.seq });
  }
  const g = String(card.general || "").trim();
  if (g) overrides.push({ step_seq: null, key: "_general", kind: "custom_text", value: g });
  return { selected_plan: pid, param_finals, skips, overrides };
}

async function confirmPlan(card) {
  const cid = active.value;
  const label = planChosen(card).label || card.chosen;
  card.submitting = true;
  card.error = "";
  try {
    await ensureIdentity();
    const message = `就按这版执行：${label}`;
    const r = await fetch(`/plans/${encodeURIComponent(card.plan_run_id)}/confirm`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(Object.assign(confirmFrameOf(card), { message })),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    card.state = "confirmed";
    card.exec_run = j.run_id || "";
    if (planModal.value === card) planModal.value = null;
    // 用户看到的那张「按此执行」卡其实是**统一提问卡**（planCardModal 把
    // origin='plan_confirm' 交给 openQuestionCard 渲染，见 questionPage 那段），
    // 而不是老的 planModal。这里原先只清 planModal，于是点「执行」之后
    // 弹窗一直挂着不关——真机反馈的原话就是「我选完点执行后弹窗不自动关闭」。
    // 两个都清：哪条路径渲染的就关哪一个。
    if (questionCard.value && questionCard.value.card === card) {
      questionCard.value = null;
      restoreParkedCard();     // 同上：交掉了就把让位的卡还回来
    }
    msgs(cid).push({ role: "user", text: message, state: "done", attachments: [] });
    busy[cid] = j.run_id || null;
    if (activePanel.value === 'runs') loadRuns();
    scrollDown();
  } catch (e) {
    card.error = e.message;
  } finally {
    card.submitting = false;
  }
}

async function revisePlan(card) {
  const cid = active.value;
  card.submitting = true;
  card.error = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/plans/${encodeURIComponent(card.plan_run_id)}/revise`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ feedback: String(card.feedback || "").trim() }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    card.state = "revised";
    busy[cid] = j.run_id || null;      // 新的一轮规划：它的卡会作为新一条消息出现
    if (planModal.value === card) planModal.value = null;
    scrollDown();
  } catch (e) {
    card.error = e.message;
  } finally {
    card.submitting = false;
  }
}

function dismissPlan(card) {
  card.state = "dismissed";
  card.error = "";
  if (planModal.value === card) planModal.value = null;
}

// ---- 对账角标（块 B-3）：只观测，不改任何一次调用 ----------------------------

const runAudit = reactive({});   // run_id -> {plan_id, extra, unfulfilled, reason}

function auditCount(a) {
  return (a.extra || []).length + (a.unfulfilled || []).length;
}

function auditBubble(cid, a, runId) {
  msgs(cid).push({
    role: "assistant", kind: "audit", state: "done", run_id: runId || "",
    plan_id: a.plan_id || "", extra: a.extra || [], unfulfilled: a.unfulfilled || [],
    reason: a.reason || "",
  });
  scrollDown();
}

function currentAudit(cid) {
  const rid = busy[cid];
  if (rid && runAudit[rid]) return runAudit[rid];
  return null;
}

// ---- 聊天窗渲染进度条（块 C 后半）------------------------------------------
//
// 两个数据源同形：轮询那对 tool_call/tool_result 帧（服务端每 render_poll_sec 一帧），
// 以及 /render_status 读到的 render_jobs 进度行。帧只负责「起」与「推」，
// 数字拿不到时按秒级 HTTP 兜着，成片帧到达即收尾。

const RENDER_STAGE_TEXT = {
  queued: "排队中", preparing: "准备素材", compositing: "合成画面",
  rendering: "渲染画面", encoding: "编码输出", mixing: "混音",
  subtitle: "上字幕", done: "已完成", failed: "失败",
};

function renderStageText(v) {
  return RENDER_STAGE_TEXT[v.stage] || RENDER_STAGE_TEXT[v.status] || "渲染中";
}

function isRenderPoll(name) {
  return String(name || "").replace(/^storyline_/, "") === "render_status";
}

function isRenderSubmit(name) {
  return String(name || "").replace(/^storyline_/, "") === "render_video";
}

function grabArtifactId(text) {
  const m = /"artifact_id"\s*:\s*"([^"]+)"/.exec(String(text || ""));
  return m ? m[1] : "";
}

function parseRenderView(text) {
  const s = String(text || "");
  const grab = (rx) => { const m = rx.exec(s); return m ? m[1] : ""; };
  const status = grab(/"status"\s*:\s*"([a-z_]+)"/);
  if (!status) return null;
  return {
    artifact_id: grab(/"artifact_id"\s*:\s*"([^"]+)"/),
    status,
    percent: Number(grab(/"percent"\s*:\s*(\d+)/) || 0),
    stage: grab(/"stage"\s*:\s*"([a-z_]+)"/),
    error: grab(/"error"\s*:\s*"([^"]*)"/),
  };
}

function liveRenderBar(cid) {
  const list = msgs(cid);
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].kind === "render" && list[i].state === "running") return list[i];
  }
  return null;
}

function closeRenderBar(cid, patch) {
  const bar = liveRenderBar(cid);
  if (bar) Object.assign(bar, patch || {});
}

function touchRender(cid, v) {
  if (!v) return null;
  let bar = liveRenderBar(cid);
  if (!bar) {
    msgs(cid).push({
      role: "assistant", kind: "render", state: "running",
      artifact_id: v.artifact_id || "", percent: 0, stage: "", error: "", polling: false,
    });
    const list = msgs(cid);
    bar = list[list.length - 1];
    scrollDown();
  }
  if (v.artifact_id && !bar.artifact_id) bar.artifact_id = v.artifact_id;
  if (v.status === "done") {
    bar.state = "done"; bar.percent = 100; bar.stage = "done";
    return bar;
  }
  if (v.status === "failed") {
    bar.state = "error"; bar.stage = "failed"; bar.error = v.error || "渲染失败";
    return bar;
  }
  if (v.percent) bar.percent = v.percent;
  if (v.stage) bar.stage = v.stage;
  followChatRender(cid, bar);
  return bar;
}

async function followChatRender(cid, bar) {
  // "_default" 也是合法键：/render_status 按 (会话作用域, artifact_id) 找行，
  // 剪辑链路里渲染任务就用这个默认作用域；查不到会 404，循环自己停下。
  if (bar.polling || !bar.artifact_id) return;
  bar.polling = true;
  const deadline = Date.now() + 30 * 60 * 1000;
  try {
    while (Date.now() > 0 && bar.state === "running" && Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 2000));
      if (bar.state !== "running") break;
      const q = new URLSearchParams({ artifact_id: bar.artifact_id, conv_id: cid || "" });
      const r = await fetch(`/render_status?${q}`, { headers: authHeaders() }).catch(() => null);
      if (!r) break;
      const j = await r.json().catch(() => ({}));
      if (!r.ok) break;                 // 任务查不到就不纠缠：成片帧才是终态凭据
      if (typeof j.percent === "number") bar.percent = j.percent;
      if (j.stage) bar.stage = j.stage;
      if (j.status === "done") { bar.state = "done"; bar.percent = 100; break; }
      if (j.status === "failed") { bar.state = "error"; bar.error = j.error || "渲染失败"; break; }
    }
  } finally {
    bar.polling = false;
  }
}

function onFrame(cid, p) {
  if (p.type === "connected") return;
  // 服务端重放的帧带 replayed:true。重放与「重连后重拉历史」可能覆盖同一轮：
  // 历史里的 assistant 行与重放的 answer/delta 是同一件事，重复画会出现两条一样的气泡。
  // 这里按内容做一次轻量去重（历史已经给过同样的助手文本就跳过），
  // 只对 replayed 帧生效——直播帧的行为一个字不变。
  if (p.replayed && p.type === "answer") {
    const text = String(p.answer || "");
    const dup = msgs(cid).some((m) => m.role === "assistant"
      && m.state === "done" && String(m.text || "") === text);
    if (dup) { busy[cid] = null; return; }
  }
  if (p.type === "run_adopted") {
    // 后端决定「接着跑被打断的那一轮」：真正在跑的 run_id 与 /chat 回的那个不同。
    // 必须在这里认下它，否则后面每一帧都过不了 run_id 守卫，整轮静默消失。
    if (p.run_id) busy[cid] = p.run_id;
    return;
  }
  if (p.type === "delta") {    // 工具轮之间会有 stream_end：下一段 delta 开新气泡，保留中间叙述。
    if (p.run_id && busy[cid] && p.run_id !== busy[cid]) return;
    let cur = streamCur[cid];
    if (!cur) {
      cur = { role: "assistant", text: "", state: "streaming" };
      msgs(cid).push(cur);
      streamCur[cid] = cur;
    }
    cur.text += p.text || "";
    scrollDown();
  } else if (p.type === "stream_end") {
    // 一轮 LLM 输出结束：封当前气泡为 done，下一轮 delta 另起新气泡。
    if (streamCur[cid]) streamCur[cid].state = "done";
    streamCur[cid] = null;
  } else if (p.type === "answer") {
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    const cur = streamCur[cid];
    const list = msgs(cid);
    const last = list[list.length - 1];
    if (cur) {
      cur.text = p.answer ?? cur.text;
      cur.state = "done";
    } else if (last && last.role === "assistant" && last.state !== "error") {
      // 最后一条助手气泡就是本轮流式输出：用 answer 定稿，绝不另起一条重复的。
      last.text = p.answer ?? last.text;
      last.state = "done";
    } else {
      list.push({ role: "assistant", text: p.answer ?? "", state: "done" });
    }
    streamCur[cid] = null;
    const pendingAudit = p.run_id ? runAudit[p.run_id] : currentAudit(cid);
    busy[cid] = null;
    // 对账只观测：答复定稿后把「计划外 / 未履行 + 偏离理由」作为一条角标补进流里，
    // 与刷新后从 qa.parts 重放出来的那一条同形（历史与实时看到同一份结论）。
    if (pendingAudit && auditCount(pendingAudit)) {
      auditBubble(cid, {
        plan_id: pendingAudit.plan_id,
        extra: pendingAudit.extra, unfulfilled: pendingAudit.unfulfilled,
        reason: String(p.answer || "").slice(0, 400),
      }, p.run_id);
    }
    scrollDown();
  } else if (p.type === "media") {
    // 成片回投：MediaCardHook 从 render_video 工具结果捕获 media_url → 播放卡片
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    closeRenderBar(cid, { state: "done", percent: 100, stage: "done" });
    msgs(cid).push({
      role: "assistant", kind: "media", state: "done",
      text: p.title || "成片已渲染",
      url: p.media_url, duration: p.duration, evidence: p.evidence || [],
    });
    scrollDown();
  } else if (p.type === "tool_call") {
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    if (isRenderPoll(p.tool)) {         // 轮询进度不刷屏：喂进度条，不各推一条气泡
      const art = grabArtifactId(p.arguments);
      if (art) touchRender(cid, { artifact_id: art, status: "running" });
      return;
    }
    if (isRenderSubmit(p.tool)) {
      // render_video 内部 submit + poll 会一路堵到终态，这之前前端只有这一帧：
      // 进度条从这里起步，数字交给 followChatRender 的 /render_status。
      touchRender(cid, {
        artifact_id: grabArtifactId(p.arguments) || "_default", status: "running",
      });
    }
    pipeTouch(cid, p.tool, { state: "running" }, p.tool_display);
    msgs(cid).push({
      role: "assistant", kind: "tool", state: "running",
      tool: p.tool, tool_display: p.tool_display || "", call_id: p.call_id || "",
      args: p.arguments, arg_labels: p.arg_labels || null,
      result: null, elapsed: null, error: null,
    });
    scrollDown();
  } else if (p.type === "tool_result") {
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    // 进度字段优先看帧上单独挂的 render：result 出帧要截 600 字，长 presigned 链接
    // 会把排在末尾的 render 块整段削掉（真机：渲染跑完一根进度条都没有）。
    const view = p.render || parseRenderView(p.result);
    if (isRenderSubmit(p.tool) && !view && p.error) {
      // 提交就没成功（结果里没有渲染视图）：进度条收成失败，不能一直挂着「渲染中」。
      closeRenderBar(cid, { state: "error", stage: "failed", error: String(p.error).slice(0, 200) });
    }
    if (isRenderPoll(p.tool)) {         // 轮询那一趟的结果只喂进度条，不进工具流水
      if (view) touchRender(cid, view);
      return;
    }
    if (view) touchRender(cid, view);   // render_video 提交回来的在途视图：进度条从这里起步
    // 注册表里没有这个工具 = 调用从未打到后端（规划轮挡剪辑节点走的就是这条路）：
    // 记进进度卡就是「✂ 剪辑受阻 · 有一步失败」，而那一步压根没跑过。
    if (p.invoked !== false) {
      pipeTouch(cid, p.tool, {
        state: p.error ? "error" : "done", elapsed: p.elapsed, error: p.error,
      }, p.tool_display);
    } else {
      pipeUnmark(cid, p.tool);
    }
    const list = msgs(cid);
    // 同批并发的同名调用按 call_id 各回各家：按「第一个同名 running 气泡」配对会让
    // 两次加载技能的结果互换（结果按完成顺序到达，声明顺序只是另一套）。
    let idx = p.call_id ? list.findIndex(
      (m) => m.kind === "tool" && m.state === "running" && m.call_id === p.call_id
    ) : -1;
    if (idx < 0) {
      idx = list.findIndex(
        (m) => m.kind === "tool" && m.state === "running" && m.tool === p.tool
      );
    }
    if (idx >= 0) {
      list[idx].state = "done";
      if (p.tool_display) list[idx].tool_display = p.tool_display;
      list[idx].result = p.result;
      list[idx].elapsed = p.elapsed;
      list[idx].error = p.error;
    }
    adoptForkedRun(cid, p);
    scrollDown();
  } else if (p.type === "subagent_spawn") {
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    msgs(cid).push({
      role: "assistant", kind: "subagent", state: "running",
      task: p.task, result: null,
    });
    scrollDown();
  } else if (p.type === "subagent_end") {
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    const list = msgs(cid);
    const idx = list.findIndex(
      (m) => m.kind === "subagent" && m.state === "running"
    );
    if (idx >= 0) {
      list[idx].state = "done";
      list[idx].result = p.result;
    }
    scrollDown();
  } else if (p.type === "approval") {
    // HITL 审批：撞上需人工确认的工具，弹审批卡等用户批准/拒绝
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    // **按题去重**——这就是「同一道题弹好几次」的成因。
    //
    // 服务端每次挂起只发一帧审批（已核对库里的链：每道题只问一次），
    // 但这条帧会进会话缓冲，**每次重连都带 ``replayed: true`` 重放一次**
    // （见 connection_manager 的缓冲说明）。上面只对 ``answer`` 帧做了去重，
    // 审批帧漏了，于是重连一次就再弹一次同一道题。
    //
    // 判据是「这个 run + 这道题」的签名。**不看 replayed 也去重**：
    // 后端每道题只问一次，所以同一签名再进来必然是重放——统一拦掉更稳，
    // 也顺带挡住任何重复投递。用户答完时 `decideApproval` 会清这个集合，
    // 所以「换一版后同一道题被重新问到」仍然弹得出来。
    const apSig = `${String(p.run_id || "")}\u0000${
      String((p.ask && p.ask.title) || p.reason || "")}`;
    const seenAp = askedCards[cid] || new Set();
    if (seenAp.has(apSig)) return;
    seenAp.add(apSig);
    askedCards[cid] = seenAp;
    approvalBubble(cid, p);
  } else if (p.type === "plan") {
    // 候选计划：一张卡一条消息，等待用户在卡上点确认（服务端把这一份也存进了指针行）
    if (busy[cid] && p.run_id && p.run_id !== busy[cid]) return;
    planBubble(cid, p);
  } else if (p.type === "plan reconciliation") {
    // 对账只观测：先记在 run 上，进度卡挂角标；偏离理由等 answer 定稿再补一条气泡
    if (p.run_id) runAudit[p.run_id] = {
      plan_id: p.plan_id || "", extra: p.extra || [], unfulfilled: p.unfulfilled || [],
    };
  } else if (p.type === "error") {
    msgs(cid).push({ role: "assistant", text: p.error || "执行出错", state: "error" });
    streamCur[cid] = null;
    busy[cid] = null;
    scrollDown();
  }
}

async function send() {
  const text = draft.value.trim();
  const cid = await ensureActive();
  const atts0 = atts(cid).slice();
  if (!text && !atts0.length) return;

  ensureSocket(cid);
  msgs(cid).push({ role: "user", text, state: "done", attachments: atts0 });
  const conv = convs.value.find((c) => c.id === cid);
  if (conv && conv.title === "新对话") {
    conv.title = (text || atts0[0].filename).slice(0, 18);
    renameConv(cid, conv.title);
  }
  draft.value = "";
  pending[cid] = [];
  scrollDown();
  try {
    await ensureIdentity();
    const r = await fetch("/chat", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        conversation_id: cid, message: text,
        attachments: atts0.map((a) => a.material_id),
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    if (j.run_id) busy[cid] = j.run_id;
  } catch (e) {
    msgs(cid).push({ role: "assistant", text: "投递失败：" + e.message, state: "error" });
  }
}

function onKey(e) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
}

// ---- 素材上传：POST /upload → material_id 挂到「待发附件条」，输入框只留用户的话 ----
const fileInput = ref(null);
const uploading = ref("");
const uploadPercent = ref(0);   // 分片路径的进度；单次上传的小文件不写它
const cancelling = ref(false);  // 按了「取消」但正在飞的那一片还没落地
const uploadChunkedPath = ref(false);  // 只有分片路径有会话可作废，小文件单请求没有
const dragOver = ref(false);
const pending = reactive({});   // convId -> [{material_id, filename, kind, url, bytes, duration}]

function atts(cid) {
  if (!pending[cid]) pending[cid] = [];
  return pending[cid];
}

function removeAtt(cid, i) {
  atts(cid).splice(i, 1);
}

const KIND_ICON = { video: "🎬", audio: "🎵", image: "🖼" };
function kindIcon(kind) {
  return KIND_ICON[kind] || "📎";
}

// 工具名 → 用户能看懂的中文短语（工具卡片展示用，非程序员友好）
// 自块 A 起这是**兜底表**：第一来源是服务端词表（帧里的 *_display 与 /tools 的 name_display），
// 这张表只在词表没装上（离线装配 / Storyline 未连通）时兜住界面。
const TOOL_LABELS = {
  load_skill: "加载剪辑技能",
  load_media: "读取素材",
  search_media: "检索素材库",
  split_shots: "切分镜头",
  understand_clips: "理解画面内容",
  filter_clips: "筛选镜头",
  group_clips: "编排镜头分组",
  asr: "识别语音文字",
  correct_transcript: "修正转写错字",
  speech_rough_cut: "去除口播停顿",
  script_template_rec: "匹配文案模板",
  generate_script: "撰写文案",
  generate_ai_transition: "生成 AI 转场",
  transition_rec: "挑选转场效果",
  text_rec: "文案风格仿写",
  generate_voiceover: "生成配音",
  select_BGM: "挑选背景音乐",
  plan_timeline: "规划时间线",
  plan_timeline_pro: "规划专业版时间线",
  plan_timeline_ai_transition: "规划 AI 转场时间线",
  render_video: "渲染成片",
  read_node_history: "查看历史步骤",
  render_status: "查询渲染进度",
  fetch_media: "按链接取素材",
  fetch_url: "抓取网页内容",
  web_search: "联网搜索",
  write_file: "写入文件",
  read_file: "读取文件",
  edit_file: "编辑文件",
  grep: "搜索文件内容",
  spawn_subagent: "派出子助手",
  update_memory: "记录备忘",
  read_memory: "回忆备忘",
  send_message: "给同伴发消息",
  read_inbox: "查看收件箱",
  plan_editing_team: "组建剪辑团队",
  rerun_from: "回到某一步重跑",
  list_tasks: "查看任务板",
  claim_task: "认领任务",
  complete_task: "完成任务",
  team_status: "查看团队状态",
  create_cron_job: "创建定时任务",
  list_cron_jobs: "查看定时任务",
  delete_cron_job: "删除定时任务",
};
function toolLabel(name, display) {
  if (display) return display;                       // 帧里自带的 *_display：第一来源
  if (!name) return "";
  const bare = String(name).replace(/^storyline_/, "");
  return catalogLabels.value[name] || catalogLabels.value[bare]   // 服务端词表
      || TOOL_LABELS[name] || TOOL_LABELS[bare] || name;          // 兜底 → 机器名
}

// 机器名在界面上不常驻，但必须取得回来：排障、rerun_from 的节点名都靠它。
const copiedName = ref("");
function copyMachine(name) {
  const text = String(name || "");
  if (!text) return;
  const done = () => {
    copiedName.value = text;
    setTimeout(() => { if (copiedName.value === text) copiedName.value = ""; }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (_) { /* 复制不了就算了 */ }
  document.body.removeChild(ta);
}

// 参数摘要：把生硬的 JSON 转成人话（material_ids 数组 → "N 个素材"，长文本截断）
// 标签三级：帧里的 arg_labels（服务端出口挂的，第一来源）→ /tools 的 params_display
// （同一张单源表）→ 本地 ARG_LABELS 兜底（词表没装上时才用），最后才露参数名。
function summarizeArgs(m) {
  const args = (m && m.args) || {};
  const frameLabels = (m && m.arg_labels) || {};
  if (!Object.keys(args).length) return [];
  const ARG_LABELS = {
    material_ids: "素材",
    name: "名称",
    query: "关键词",
    url: "链接",
    text: "文本",
    clips: "镜头",
    groups: "分组",
    script: "文案",
    bgm: "配乐",
    voiceover: "配音",
    timeline: "时间线",
    corrections: "修字表",
    conversation_id: "会话",
    session_id: "服务端会话",
    artifact_id: "产物",
    user_id: "用户",
    key: "键名",
    pattern: "检索式",
    max_results: "条数上限",
    path: "路径",
    task: "任务",
    result: "结论",
    to: "发送给",
    from: "来自",
  };
  const out = [];
  for (const [k, v] of Object.entries(args)) {
    if (k.endsWith("_display")) continue;   // 旁路位不单独成行，只把主键那一行换成中文
    const label = frameLabels[k] || paramLabels.value[k] || ARG_LABELS[k] || k;
    const shown = args[`${k}_display`] === undefined ? v : args[`${k}_display`];
    let val;
    if (Array.isArray(shown)) {
      val = shown.length <= 3 && shown.every((x) => typeof x === "string" && x.length <= 20)
        ? shown.join("、") : `${shown.length} 项`;
    } else if (typeof shown === "string") {
      val = shown.length > 40 ? shown.slice(0, 40) + "…" : shown;
    } else {
      val = JSON.stringify(shown);
      if (val.length > 40) val = val.slice(0, 40) + "…";
    }
    out.push({ k: label, v: val });
  }
  return out;
}

// 结果摘要：skill 文档全文等技术性长输出只取一行概要，不刷屏
function shortResult(result) {
  if (typeof result !== "string") return result;
  const skillMatch = result.match(/^\[skill:(\S+?) /);
  if (skillMatch) return `已加载技能「${skillMatch[1]}」`;
  return result.length > 200 ? result.slice(0, 200) + "…" : result;
}

// ---- 剪辑进度卡：从工具调用流里动态收集剪辑步骤，一眼看出进行中/失败/还剩哪些 ----
// 只是展示层排序参考：agent 实际走哪些步骤由它自己决定，没走的不会出现。
const PIPE_ORDER = [
  "search_media", "load_media", "fetch_media",
  "split_shots", "asr", "understand_clips", "correct_transcript", "speech_rough_cut",
  "filter_clips", "group_clips",
  "script_template_rec", "text_rec", "generate_script",
  "generate_voiceover", "select_BGM", "transition_rec", "generate_ai_transition",
  "plan_timeline", "plan_timeline_pro", "plan_timeline_ai_transition",
  "render_video",
];
function pipeRank(name) {
  const bare = (name || "").replace(/^storyline_/, "");
  const i = PIPE_ORDER.indexOf(bare);
  return i === -1 ? 999 : i;
}
const pipe = reactive({});   // convId -> { bareName: {state: running|done|error, elapsed, error} }

function pipeTouch(cid, name, patch, display) {
  const bare = (name || "").replace(/^storyline_/, "");
  if (pipeRank(bare) === 999) return;   // 非剪辑步骤不进进度卡
  if (!pipe[cid]) pipe[cid] = {};
  pipe[cid][bare] = {
    ...(pipe[cid][bare] || { state: "running", elapsed: null, label: "" }),
    ...(display ? { label: display } : {}), ...patch,
  };
}

function pipeUnmark(cid, name) {
  // 一次从未打到后端的试差：擦掉先前置的「进行中」，别留成永远跑不完的那一步。
  const bare = (name || "").replace(/^storyline_/, "");
  const m = pipe[cid];
  if (m && m[bare] && m[bare].state === "running") delete m[bare];
}

function pipeSteps(cid) {
  const m = pipe[cid];
  if (!m) return [];
  return Object.keys(m)
    .sort((a, b) => pipeRank(a) - pipeRank(b))
    .map((k) => ({ name: k, ...m[k] }));
}

function pipeStatus(cid) {
  const steps = pipeSteps(cid);
  if (!steps.length) return null;
  const done = steps.filter((s) => s.state === "done").length;
  const hasErr = steps.some((s) => s.state === "error");
  const running = steps.some((s) => s.state === "running");
  if (hasErr) return `✂ 剪辑受阻 · 已完成 ${done} 步，有一步失败`;
  if (running) return `✂ 剪辑进行中 · 已完成 ${done} 步`;
  if (steps.some((s) => s.name === "render_video")) return "✂ 成片完成";
  return `✂ 剪辑任务 · 已完成 ${done} 步`;
}

function pipeRunning(cid) {
  return pipeSteps(cid).find((s) => s.state === "running") || null;
}
const pipeExpanded = ref(false);

function fmtSize(bytes) {
  if (!bytes) return "";
  const mb = bytes / 1048576;
  return mb >= 1 ? mb.toFixed(1) + "MB" : Math.max(1, Math.round(bytes / 1024)) + "KB";
}

function pickFiles() {
  if (!uploading.value) fileInput.value && fileInput.value.click();
}

// 2MB 以内仍走 /upload 单请求：四次往返对短视频不值得。分片路径的价值在失败半径——
// 一次 fetch 断了要整片重传，分片只重传那一片，且「已有哪几片」以服务端桶里的字节为准，
// 所以刷新页面、换浏览器、服务重启都接得回原处。
const CHUNK_MIN_BYTES = 2 * 1024 * 1024;

function toHex(digest) {
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// crypto.subtle 只在安全上下文里有（https 或 localhost）。局域网 IP 打开页面时拿不到，
// 就如实降级：不声明任何指纹，服务端仍逐片核片长、核总长，只是少两道内容比对。
function subtleOk() { return !!(window.crypto && window.crypto.subtle); }

async function partSha(buf) {
  if (!subtleOk()) return "";
  return toHex(await window.crypto.subtle.digest("SHA-256", buf));
}

// 取消上传要能掐断「正在飞的那一片」，所以留句柄；fetch 报不出请求体写到哪了，
// 单片用 XHR 换来 xhr.upload.onprogress 的连续进度。
let inFlightXhr = null;
let cancelWanted = false;
let wholeUploadCtrl = null;   // 小文件单请求的 AbortController，取消时掐断它
const activeUploadId = ref("");

function xhrPut(url, buf, onLoaded) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    inFlightXhr = xhr;
    xhr.open("PUT", url);
    xhr.setRequestHeader("Authorization", `Bearer ${token.value}`);
    if (xhr.upload) xhr.upload.onprogress = (e) => onLoaded(e.loaded || 0);
    const done = (fn, arg) => { inFlightXhr = null; fn(arg); };
    xhr.onload = () => {
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch { /* 错误体不是 JSON 时按状态码说 */ }
      if (xhr.status >= 200 && xhr.status < 300) done(resolve, j);
      else done(reject, new Error(detailText(j.detail) || ("HTTP " + xhr.status)));
    };
    xhr.onerror = () => done(reject, new Error("网络中断：已传的分片留在服务端"));
    xhr.onabort = () => done(reject, new Error("已取消上传"));
    xhr.send(buf);
  });
}

// 服务端 detail 一般是字符串；万一是对象就别渲染成 [object Object]
function detailText(d) {
  if (!d) return "";
  return typeof d === "string" ? d : (d.message || JSON.stringify(d));
}

async function jsonOf(res) {
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(detailText(j.detail) || ("HTTP " + res.status));
  return j;
}

async function uploadWhole(f, cid) {
  const q = new URLSearchParams({ conversation_id: cid, filename: f.name });
  wholeUploadCtrl = new AbortController();
  try {
    return await jsonOf(await fetch("/upload?" + q.toString(),
      { method: "POST", headers: authHeaders(), body: f, signal: wholeUploadCtrl.signal }));
  } finally {
    wholeUploadCtrl = null;
  }
}

/** 把服务端还回来的缺口补齐（作废重传与断线重连共用这一条）。 */
async function putMissing(v, f, digests, startSent) {
  const ps = v.part_size;
  let sent = startSent;
  for (const i of (v.missing_parts || [])) {
    if (cancelWanted) throw new Error("已取消上传");
    const buf = await f.slice(i * ps, Math.min((i + 1) * ps, f.size)).arrayBuffer();
    const sha = await partSha(buf);
    if (sha) digests[i] = sha;
    const url = "/upload/part?" + new URLSearchParams(
      { upload_id: v.upload_id, part: String(i), sha256: sha });
    await xhrPut(url, buf, (loaded) => markProgress(f, sent + loaded));
    sent += buf.byteLength;
    markProgress(f, sent);
  }
  return sent;
}

function markProgress(f, bytes) {
  // 封顶 99：complete 还要拼流、探元数据、登记，那步没报回来之前不说「传完了」
  uploadPercent.value = f.size ? Math.min(99, Math.round(bytes * 100 / f.size)) : 0;
}

async function postComplete(uploadId, digests) {
  const headers = authHeaders();
  const init = { method: "POST", headers };
  if (digests) {
    headers["Content-Type"] = "application/json";
    // 整文件 sha256 浏览器算不动（那要把整个文件读进内存），但逐片摘要传的时候本来
    // 就算过——把按序整列交回去，服务端就拿着桶里真存下的字节逐片比，零额外内存。
    init.body = JSON.stringify({ parts_sha256: digests });
  }
  const url = "/upload/complete?" + new URLSearchParams({ upload_id: uploadId });
  return jsonOf(await fetch(url, init));
}

/** 分片上传一条素材；返回 {j, resumed, had_parts, verified}。 */
async function uploadChunked(f, cid) {
  const q = new URLSearchParams({ conversation_id: cid, filename: f.name, size: String(f.size) });
  let v = await jsonOf(await fetch("/upload/init?" + q.toString(),
    { method: "POST", headers: authHeaders() }));
  activeUploadId.value = v.upload_id;
  const resumed = !!v.resumed, had = (v.parts || []).length;
  const digests = new Array(v.part_count).fill(null);
  await putMissing(v, f, digests,
    (v.parts || []).reduce((a, p) => a + (p.bytes || 0), 0));

  // 上一次会话已落在服务端的那些片，手里没有本地摘要：从本地文件重读那一段算出来。
  // 宁可多读一次盘，也不拿服务端报的摘要当本地基准——那等于让被检者自己出证。
  let sendList = null;
  if (subtleOk()) {
    const ps = v.part_size;
    for (let i = 0; i < v.part_count; i++) {
      if (digests[i] || cancelWanted) continue;
      digests[i] = await partSha(await f.slice(i * ps, Math.min((i + 1) * ps, f.size)).arrayBuffer());
    }
    sendList = cancelWanted ? null : digests;
  }
  if (cancelWanted) throw new Error("已取消上传");

  let verified = 0;
  try {
    const j = await postComplete(v.upload_id, sendList);
    return { j, resumed, had, verified: j.parts_verified || 0 };
  } catch (e) {
    // 服务端作废了它对不上号的几片：缺口现在是真的缺了，补一轮再 complete。
    // 只兜一轮——再来一次还是不符就不是重传能解决的事，如实报错。
    if (!sendList || !/已作废这些片/.test(e.message)) throw e;
    v = await jsonOf(await fetch("/upload/status?"
      + new URLSearchParams({ upload_id: v.upload_id }), { headers: authHeaders() }));
    await putMissing(v, f, digests, 0);
    const j = await postComplete(v.upload_id, digests);
    return { j, resumed, had, verified: j.parts_verified || 0 };
  }
}

function cancelUpload() {
  if (!uploading.value) return;
  cancelWanted = true;
  cancelling.value = true;
  if (inFlightXhr) inFlightXhr.abort();   // 分片：正在飞的那一片当场停
  if (wholeUploadCtrl) wholeUploadCtrl.abort();   // 小文件单请求：掐断 fetch
}

/** 取消的收尾：把这条会话已传的分片从桶里删净（不等过期清扫来收尸）。 */
async function finishCancel() {
  const id = activeUploadId.value;
  let cleaned = 0;
  if (id) {
    try {
      const r = await jsonOf(await fetch("/upload/abort?" + new URLSearchParams({ upload_id: id }),
        { method: "POST", headers: authHeaders() }));
      cleaned = r.parts_cleaned || 0;
    } catch (e) { /* 删不净由 sweep_upload_sessions 兜底，这里只如实说 */ }
  }
  activeUploadId.value = "";
  return cleaned;
}

async function uploadOne(f, cid) {
  uploading.value = f.name;
  uploadPercent.value = 0;
  cancelWanted = false;
  cancelling.value = false;
  const chunked = f.size >= CHUNK_MIN_BYTES;
  uploadChunkedPath.value = chunked;
  try {
    await ensureIdentity();
    let j, resumed = false, had = 0, verified = 0;
    if (!chunked) {
      j = await uploadWhole(f, cid);
    } else {
      const r = await uploadChunked(f, cid);
      j = r.j; resumed = r.resumed; had = r.had; verified = r.verified;
    }
    uploadPercent.value = 100;
    atts(cid).push({
      material_id: j.material_id, filename: j.filename, kind: j.kind,
      url: j.url, bytes: j.bytes, duration: j.duration,
    });
    if (resumed && had) {
      msgs(cid).push({ role: "assistant", text: `续传上了：服务端已有 ${had} 片，只补了剩下的`, state: "done" });
    }
    const chk = verified ? `，${verified} 片内容与本地摘要逐片核对过` : "";
    msgs(cid).push({
      role: "assistant",
      text: `素材已就绪：${j.filename}（${fmtSize(j.bytes)}${chk}）`
        + (active.value !== cid ? "，回到本会话后随消息附上" : ""),
      state: "done",
    });
  } catch (e) {
    if (cancelWanted) {
      const n = await finishCancel();
      msgs(cid).push({
        role: "assistant",
        text: `已取消上传：${f.name} — `
          + (n ? `服务端已传的 ${n} 片分片已删除` : "还没有分片落到服务端，桶里无残留"),
        state: "done",
      });
    } else {
      msgs(cid).push({
        role: "assistant",
        text: "上传失败：" + f.name + " — " + e.message
          + (chunked ? `（已传 ${uploadPercent.value}%，重选同一文件可从中断处续传）` : ""),
        state: "error",
      });
    }
  } finally {
    uploading.value = "";
    uploadPercent.value = 0;
    uploadChunkedPath.value = false;
    cancelling.value = false;
    cancelWanted = false;
    activeUploadId.value = "";
    scrollDown();
  }
}

async function onFiles(e) {
  const files = [...(e.target.files || [])];
  e.target.value = "";
  const cid = await ensureActive();
  ensureSocket(cid);
  for (const f of files) await uploadOne(f, cid);   // 大文件顺序传，避免互抢带宽
}

async function onDrop(e) {
  const files = [...(e.dataTransfer?.files || [])];
  if (!files.length) return;
  const cid = await ensureActive();
  ensureSocket(cid);
  for (const f of files) await uploadOne(f, cid);
}

// ---- 链接取料：POST /fetch_media → 与 /upload 同一条待发附件通道，只多一个来源标记 ----
const linkUrl = ref("");
const linkOpen = ref(false);
const fetchingLink = ref(false);
const linkInput = ref(null);

function toggleLink() {
  linkOpen.value = !linkOpen.value;
  if (linkOpen.value) nextTick(() => linkInput.value && linkInput.value.focus());
}

async function fetchLink() {
  const url = linkUrl.value.trim();
  if (!url || fetchingLink.value) return;
  const cid = await ensureActive();
  ensureSocket(cid);
  fetchingLink.value = true;
  try {
    await ensureIdentity();
    const r = await fetch("/fetch_media", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ conversation_id: cid, url }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    atts(cid).push({
      material_id: j.material_id, filename: j.filename, kind: j.kind,
      url: j.url, bytes: j.bytes, duration: j.duration,
    });
    linkUrl.value = "";
    linkOpen.value = false;
    if (active.value !== cid) {
      msgs(cid).push({ role: "assistant", text: `素材已就绪：${j.filename}（回到本会话后随消息附上）`, state: "done" });
    }
  } catch (e) {
    msgs(cid).push({ role: "assistant", text: "链接取料失败：" + e.message, state: "error" });
  } finally {
    fetchingLink.value = false;
    scrollDown();
  }
}

// ---- 模型设置：密钥由用户自己在页面上填，服务端存进 PG 后两个进程热读，不重启 ----

const settings = ref(null);
const keyDraft = ref("");
const savingKey = ref(false);
const testingKey = ref(false);
const testResult = ref(null);
const keyError = ref("");

async function loadSettings() {
  await ensureIdentity();
  const r = await fetch("/settings", { headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
  settings.value = j;
  return j;
}

async function toggleSettings() {
  activePanel.value = activePanel.value === 'settings' ? null : 'settings';
  if (activePanel.value !== 'settings') return;
  keyError.value = "";
  testResult.value = null;
  try {
    await loadSettings();
  } catch (e) {
    keyError.value = "读不到当前设置：" + e.message;
  }
}

async function postApiKey(value) {
  if (savingKey.value) return;
  savingKey.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/settings/api-key", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ api_key: value }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    settings.value = j;
    keyDraft.value = "";
    testResult.value = null;          // 换 key 之后上一次自检结论就作废了
  } catch (e) {
    keyError.value = "保存失败：" + e.message;
  } finally {
    savingKey.value = false;
  }
}

const saveKey = () => postApiKey(keyDraft.value.trim());
const clearKey = () => postApiKey("");

async function testConn() {
  if (testingKey.value) return;
  testingKey.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/settings/test", { method: "POST", headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    testResult.value = j;
  } catch (e) {
    keyError.value = "自检没跑成：" + e.message;
  } finally {
    testingKey.value = false;
  }
}


function newConv() {
  const c = freshConv();
  convs.value.unshift(c);
  histories[c.id] = [];
  active.value = c.id;
  ensureSocket(c.id);
}

async function dropConv(cid) {
  if (convs.value.length === 1) return;
  if (socks[cid]) {
    try { socks[cid].close(); } catch (_) { /* noop */ }
    delete socks[cid];
  }
  convs.value = convs.value.filter((c) => c.id !== cid);
  delete histories[cid];
  delete pending[cid];
  if (active.value === cid) {
    runList.value = [];
    openRun.value = null;
  }
  dropRunHistoryOf(cid);
  if (active.value === cid) active.value = convs.value[0].id;
  try {
    const r = await fetch("/convs/" + encodeURIComponent(cid), {
      method: "DELETE", headers: authHeaders(),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
  } catch (e) {
    msgs(active.value).push({ role: "assistant", text: `这册没删掉（${cid}）：${e.message}`, state: "error" });
  }
}

async function scrollDown() {
  await nextTick();
  const el = scroller.value;
  if (el) el.scrollTop = el.scrollHeight;
}

onMounted(() => {
  ensureIdentity().catch(() => {}).then(loadConvs);   // 身份 → 会话列表，都是服务端的真相
  ensureCatalog();    // 词表预热：剪辑进度卡/流水线的中文名不靠用户先打开工具库
});
onBeforeUnmount(() => Object.values(socks).forEach((s) => s.close && s.close()));
</script>

<template>
  <div class="shell">
    <!-- 左侧：卷宗栏 -->
    <aside class="rail">
      <div class="brand">
        <div class="seal">创</div>
        <div class="brand-text">
          <b>智能创作助手</b>
          <span>纸上创作台</span>
        </div>
      </div>

      <button class="new" @click="newConv">✚<span class="lbl"> 开一册新卷</span></button>

      <nav class="convs">
        <div
          v-for="c in convs"
          :key="c.id"
          class="conv"
          :class="{ on: c.id === active }"
          @click="openConv(c.id)"
        >
          <span class="dot" :class="{ live: busy[c.id], idle: !busy[c.id] }"></span>
          <span class="t">{{ c.title }}</span>
          <button class="x" title="删除会话" @click.stop="dropConv(c.id)">×</button>
        </div>
      </nav>

      <footer class="who">
        <span class="k">执笔人</span>
        <span class="v">{{ userId || "身份签发中…" }}</span>
      </footer>
    </aside>

    <!-- 右侧：稿纸区 -->
    <main class="desk">
      <header class="head">
        <span class="rule"></span>
        <h2>{{ convs.find((c) => c.id === active)?.title || "—" }}</h2>
        <button class="help" :class="{ on: activePanel === 'tools' }" @click="toggleHelp">工具库</button>
        <button class="help" :class="{ on: activePanel === 'library' }" @click="toggleLibrary">素材库</button>
        <button class="help" :class="{ on: activePanel === 'bgm' }" @click="toggleBgm">音乐库</button>
        <button class="help" :class="{ on: activePanel === 'timeline' }" @click="toggleTimeline">时间线</button>
        <button class="help" :class="{ on: activePanel === 'runs' }" @click="toggleRuns">执行记录</button>
        <button class="help" :class="{ on: activePanel === 'settings' }" @click="toggleSettings">设置</button>
        <span class="ws" :class="{ on: connected[active] }">
          {{ connected[active] ? "回投已连接" : "回投未连接" }}
        </span>
      </header>

      <div
        ref="scroller"
        class="paper"
        :class="{ dropping: dragOver }"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="dragOver = false; onDrop($event)"
      >
        <div v-if="dragOver" class="drop-hint">松开即上传素材到本会话</div>
        <div v-if="!msgs(active).length" class="hello">
          <div class="vert">创·作</div>
          <h3>从一句<em>开始</em>。</h3>
          <p>写文案 · 剪视频 · 查资料 · 定日程 —— 视频素材可直接拖进本页。</p>
        </div>



        <div
          v-if="pipeSteps(active).length"
          class="pipe-card"
          :class="{ blocked: pipeStatus(active)?.includes('受阻'), open: pipeExpanded }"
          @click="pipeExpanded = !pipeExpanded"
        >
          <div class="pipe-head">
            <b>{{ pipeStatus(active) }}</b>
            <span v-if="currentAudit(active) && auditCount(currentAudit(active))" class="pipe-audit"
                  title="对照已确认的计划卡：计划外与未履行，只观测不拦截">
              ⚖ 计划外 {{ (currentAudit(active).extra || []).length }}
              · 未履行 {{ (currentAudit(active).unfulfilled || []).length }}
            </span>
            <span v-if="pipeRunning(active)" class="pipe-now">
              <i class="pipe-spin">◐</i>
              {{ toolLabel(pipeRunning(active).name, pipeRunning(active).label) }}中…
            </span>
            <span class="pipe-toggle">{{ pipeExpanded ? "收起 ▴" : "展开 ▾" }}</span>
          </div>
          <div v-if="pipeExpanded" class="pipe-flow">
            <template v-for="(s, i) in pipeSteps(active)" :key="s.name">
              <span v-if="i > 0" class="pipe-arrow">→</span>
              <span class="pipe-step" :class="s.state" :title="s.error || ''">
                <i v-if="s.state === 'running'" class="pipe-spin">◐</i>
                <i v-else-if="s.state === 'done'">✓</i>
                <i v-else-if="s.state === 'error'">✗</i>
                <i v-else>○</i>
                {{ toolLabel(s.name, s.label) }}
                <em v-if="s.state === 'done' && s.elapsed != null">{{ s.elapsed }}s</em>
                <em v-else-if="s.state === 'error'">失败</em>
              </span>
            </template>
            <span class="pipe-arrow">→ …</span>
          </div>
        </div>

        <div v-for="(m, i) in msgs(active)" :key="i" class="row" :class="m.role">
          <div class="mark" aria-hidden="true">
            <span v-if="m.role === 'user'">人</span>
            <span v-else class="seal-mini" :class="m.state">助</span>
          </div>
          <div class="bubble" :class="m.state">
            <template v-if="m.kind === 'media'">
              <div class="media-card">
                <video :src="m.url" controls preload="metadata"></video>
                <div class="meta">
                  <b>🎬 {{ m.text }}</b>
                  <span v-if="m.duration">时长 {{ Math.round(m.duration) }}s</span>
                  <a :href="m.url" download>下载成片</a>
                </div>
                <!-- 证据分级：渲染完成不等于每条都验过，这里把「有证据 / 没验」摊开 -->
                <details v-if="(m.evidence || []).length" class="mc-ev">
                  <summary>
                    这条片子验到什么程度：
                    <i class="ok">{{ m.evidence.filter((e) => e.verified).length }} 项有证据</i> ·
                    <i class="no">{{ m.evidence.filter((e) => !e.verified).length }} 项没验</i>
                  </summary>
                  <div v-for="(e, k) in m.evidence" :key="k"
                       class="mc-evrow" :class="{ un: !e.verified }">
                    <span class="mc-evlv">{{ e.verified ? "✓" : "?" }} {{ e.label }}</span>
                    <span class="mc-evcl">{{ e.claim }}</span>
                  </div>
                  <div class="mc-evnote">
                    「没验」不是漏做，是机器验不了的那几条（听感、字幕好不好看、画面对不对题）——
                    要你这一眼或这一耳朵。
                  </div>
                </details>
              </div>
            </template>
            <template v-else-if="m.kind === 'tool'">
              <div class="tool-trace" :class="{ err: m.error }">
                <div class="tt-head">
                  <span class="tt-ico">🔧</span>
                  <b>{{ toolLabel(m.tool, m.tool_display) }}</b>
                  <button class="tt-copy" title="复制机器名（排障 / 回到这一步重跑用）"
                          @click.stop="copyMachine(m.tool)">{{ copiedName === m.tool ? "已复制" : "⧉" }}</button>
                  <span v-if="m.state === 'running'" class="tt-spin">执行中…</span>
                  <span v-else class="tt-time">{{ m.elapsed }}s</span>
                </div>
                <div v-if="summarizeArgs(m).length" class="tt-args">
                  <template v-for="(p, i) in summarizeArgs(m)" :key="i">
                    <span class="tt-ak">{{ p.k }}:</span>
                    <span class="tt-av">{{ p.v }}</span>
                  </template>
                </div>
                <div v-if="m.result" class="tt-result">{{ shortResult(m.result) }}</div>
                <div v-if="m.error" class="tt-error">✗ {{ m.error }}</div>
              </div>
            </template>
            <template v-else-if="m.kind === 'subagent'">
              <div class="sub-trace">
                <div class="st-head">
                  <span class="st-ico">🤖</span>
                  <b>子 Agent</b>
                  <span v-if="m.state === 'running'" class="tt-spin">执行中…</span>
                  <span v-else class="st-ok">✓</span>
                </div>
                <div class="st-task">{{ m.task }}</div>
                <div v-if="m.result" class="tt-result">{{ m.result }}</div>
              </div>
            </template>
            <template v-else-if="m.kind === 'plan'">
              <div class="plan-inline" :class="m.state">
                <span class="pi-ico">🗂</span>
                <b>{{ planChosen(m).label || '剪辑方案' }}</b>
                <button
                  v-if="m.state === 'waiting'"
                  class="clip pi-open"
                  @click="openPlanModal(m)"
                >查看并确认</button>
                <span v-else-if="m.state === 'confirmed'" class="pi-tag ok">已确认 · 执行 {{ m.exec_run }}</span>
                <span v-else-if="m.state === 'revised'" class="pi-tag">已换一版</span>
                <span v-else-if="m.state === 'dismissed'" class="pi-tag">已取消</span>
              </div>
            </template>
            <template v-else-if="m.kind === 'audit'">
              <div class="audit-card">
                <div class="ac-head">
                  <b>⚖ 与已确认的计划有出入</b>
                  <span v-if="(m.extra || []).length" class="ac-badge extra">计划外 {{ m.extra.length }} 步</span>
                  <span v-if="(m.unfulfilled || []).length" class="ac-badge miss">未履行 {{ m.unfulfilled.length }} 步</span>
                  <span v-if="m.plan_id" class="ac-plan">对照 {{ m.plan_id }}</span>
                </div>
                <div v-if="(m.extra || []).length" class="ac-row">
                  <em>计划外</em><span>{{ m.extra.map((n) => toolLabel(n)).join('、') }}</span>
                </div>
                <div v-if="(m.unfulfilled || []).length" class="ac-row">
                  <em>未履行</em><span>{{ m.unfulfilled.map((n) => toolLabel(n)).join('、') }}</span>
                </div>
                <div v-if="m.reason" class="ac-reason">{{ m.reason }}</div>
                <div class="ac-note">只观测不拦截：偏差没有被打回，也没被强行纠正。</div>
              </div>
            </template>
            <template v-else-if="m.kind === 'render'">
              <div class="render-bar" :class="m.state">
                <div class="rb-head">
                  <b>{{ m.state === 'error' ? '✗ 渲染失败'
                        : m.state === 'done' ? '✓ 渲染完成'
                        : '⏳ ' + renderStageText(m) }}</b>
                  <span v-if="m.state !== 'error'" class="rb-pct">{{ m.percent || 0 }}%</span>
                </div>
                <div class="rb-track">
                  <i :style="{ width: Math.max(3, Math.min(100, m.percent || 0)) + '%' }"></i>
                </div>
                <div v-if="m.error" class="rb-error">{{ m.error }}</div>
                <div v-else-if="m.artifact_id" class="rb-art">渲染任务 {{ m.artifact_id }}</div>
              </div>
            </template>
            <template v-else>
              <div v-if="m.attachments && m.attachments.length" class="atts">
                <div v-for="a in m.attachments" :key="a.material_id" class="att" :title="'material_id: ' + a.material_id">
                  <video v-if="a.kind === 'video'" :src="a.url" class="thumb" controls preload="metadata"></video>
                  <img v-else-if="a.kind === 'image'" :src="a.url" class="thumb" :alt="a.filename" />
                  <audio v-else-if="a.kind === 'audio'" :src="a.url" class="aud" controls preload="none"></audio>
                  <div class="att-meta">
                    <b>{{ kindIcon(a.kind) }} {{ a.filename }}</b>
                    <span>{{ fmtSize(a.bytes) }}<template v-if="a.duration"> · {{ Math.round(a.duration) }}s</template> · 预览链接 1 小时内有效</span>
                  </div>
                </div>
              </div>
              <span class="txt"><template v-for="(s, j) in segs(m.text)" :key="j"><b v-if="s.b">{{ s.t }}</b><span v-else>{{ s.t }}</span></template></span>
              <span v-if="m.state === 'streaming'" class="caret">▍</span>
            </template>
          </div>
        </div>
      </div>

      <div v-if="atts(active).length" class="attach-bar">
        <span class="ab-label">随本条消息附上</span>
        <div v-for="(a, i) in atts(active)" :key="a.material_id" class="chip" :title="a.material_id">
          <span class="ico">{{ kindIcon(a.kind) }}</span>
          <span class="fn">{{ a.filename }}</span>
          <span class="mt">{{ fmtSize(a.bytes) }}<template v-if="a.duration"> · {{ Math.round(a.duration) }}s</template></span>
          <button class="rm" title="不随本条消息发送" @click="removeAtt(active, i)">×</button>
        </div>
      </div>

      <div v-if="linkOpen" class="link-bar">
        <input
          ref="linkInput"
          v-model="linkUrl"
          type="url"
          :disabled="fetchingLink"
          placeholder="粘贴视频/图片/音频链接：直链、含视频的网页、站点播放页…"
          @keydown.enter.prevent="fetchLink"
          @keydown.esc="linkOpen = false"
        />
        <button class="clip" :class="{ busy: fetchingLink }" :disabled="!linkUrl.trim() || fetchingLink" @click="fetchLink">
          <span v-if="fetchingLink" class="spin">取料中…</span><span v-else>取料 ➤</span>
        </button>
      </div>

      <div v-if="planModal && planModal.state === 'waiting'" class="plan-banner">
        <span class="pb-icon">🗂</span>
        <span class="pb-label">{{ planChosen(planModal).label || '剪辑方案' }}</span>
        <span class="pb-hint">已在右侧列出完整链路，确认后即执行</span>
        <button class="pb-btn primary" :disabled="planModal.submitting" @click="confirmPlan(planModal)">
          {{ planModal.submitting ? '提交中…' : '按此执行' }}
        </button>
        <button class="pb-btn" :disabled="planModal.submitting" @click="planModal.askFeedback = !planModal.askFeedback">🔄 换一版</button>
        <button class="pb-btn" @click="dismissPlan(planModal)">✕</button>
        <input v-if="planModal.askFeedback" v-model="planModal.feedback" :maxlength="200"
               class="pb-feedback" placeholder="哪里不合意？它会照这句重出一版"
               @keydown.enter.prevent="revisePlan(planModal)" />
      </div>

      <footer class="composer">
        <input ref="fileInput" type="file" multiple accept="video/*,audio/*,image/*" hidden @change="onFiles" />
        <button class="clip" :class="{ busy: uploading }"
                :title="uploading ? '上传中：' + uploading + ' ' + (uploadPercent ? uploadPercent + '%' : '') : '上传素材（也可直接拖入）'"
                @click="pickFiles">
          <span v-if="uploading" class="spin">…{{ uploadPercent ? uploadPercent + "%" : "" }}</span><span v-else>＋素材</span>
        </button>
        <button v-if="uploading" class="clip bad"
                :title="uploadChunkedPath ? '取消上传：已传分片从服务端删掉，不必等过期清扫' : '取消上传：掐断正在飞的请求'"
                :disabled="cancelling" @click="cancelUpload">
          {{ cancelling ? "取消中…" : "取消上传" }}
        </button>
        <button class="clip" :class="{ busy: fetchingLink, on: linkOpen }" title="按链接取素材" @click="toggleLink">＋链接</button>
        <textarea
          v-model="draft"
          rows="1"
          placeholder="把想法落在此处，Enter 落笔，Shift+Enter 换行；素材点「＋素材」/拖入或「＋链接」贴网址，会挂在输入框上方随消息发出…"
          @keydown="onKey"
        ></textarea>
        <button class="send" :class="{ off: !draft.trim() && !atts(active).length }" @click="send">落笔 ➤</button>
      </footer>
    </main>

    <!-- 右侧抽屉：工具库 / 素材库 / 音乐库 / 时间线 / 执行记录 / 设置（互斥，同时只开一个） -->
    <aside v-if="activePanel" class="drawer">
      <header class="drawer-head">
        <b>{{ activePanel === 'plan' ? '📋 剪辑链路' : activePanel === 'tools' ? '工具库' : activePanel === 'library' ? '素材库' : activePanel === 'bgm' ? '🎵 音乐库' : activePanel === 'timeline' ? '时间线编辑器' : activePanel === 'runs' ? '执行记录 · 时间旅行' : '设置' }}</b>
        <button class="drawer-x" @click="activePanel = null">×</button>
      </header>

      <div v-if="activePanel === 'plan'" class="plan-panel">
        <!-- 编排预览：渲染前的「成品长什么样」。用户在这里看细节，
             弹窗只问要拍板的那几题（弹窗问决策、侧边栏看详情）。 -->
        <section class="pv-sec">
          <div class="pv-head">
            <b>🎞 编排预览</b>
            <button class="pv-reload" :disabled="pvLoading" @click="loadPreview(active)">
              {{ pvLoading ? "刷新中…" : "刷新" }}
            </button>
          </div>
          <div v-if="pvLoading && !preview" class="lib-empty">正在读取编排结果…</div>
          <div v-else-if="!preview || !preview.ready" class="lib-empty">
            还没有可预览的编排（跑完切分/筛选/时间线之后这里会显示结果）
          </div>
          <template v-else>
            <div class="pv-chips">
              <span v-if="preview.summary.duration" class="pv-chip">
                <em>时长</em>{{ Math.round(preview.summary.duration) }}s</span>
              <span v-if="preview.summary.resolution" class="pv-chip">
                <em>画面</em>{{ preview.summary.resolution }}</span>
              <span v-if="preview.summary.groups" class="pv-chip">
                <em>段落</em>{{ preview.summary.groups }}</span>
              <span v-if="preview.summary.clips" class="pv-chip">
                <em>镜头</em>{{ preview.summary.clips }}</span>
              <span v-if="preview.audio.original_audio" class="pv-chip">
                <em>声音</em>保留原声</span>
              <span v-else-if="preview.audio.voiceover_count" class="pv-chip">
                <em>声音</em>配音 {{ preview.audio.voiceover_count }} 段</span>
              <span v-if="preview.audio.bgm" class="pv-chip">
                <em>配乐</em>{{ preview.audio.bgm.filename }}</span>
              <span v-if="preview.timeline.speaker_ratio" class="pv-chip">
                <em>出镜</em>{{ Math.round(preview.timeline.speaker_ratio * 100) }}%</span>
            </div>

            <div v-if="preview.warnings && preview.warnings.length" class="pv-warn">
              <div v-for="(w, i) in preview.warnings" :key="i">⚠ {{ w }}</div>
            </div>

            <!-- 轨道图：画面 / 人声 / 字幕 / 配乐 各自占多少、怎么排的 -->
            <div class="pv-tracks">
              <div class="pv-trk">
                <span class="pv-tl">画面</span>
                <div class="pv-bar">
                  <i class="pv-fill v"
                     :style="{ width: pctOf(preview.timeline.video_seconds) }"></i>
                </div>
                <span class="pv-tv">{{ Math.round(preview.timeline.video_seconds) }}s
                  / {{ preview.timeline.video_events }} 段</span>
              </div>
              <div v-if="preview.timeline.overlay_events" class="pv-trk">
                <span class="pv-tl">覆盖</span>
                <div class="pv-bar">
                  <i class="pv-fill o"
                     :style="{ width: pctOf(preview.timeline.duration) }"></i>
                </div>
                <span class="pv-tv">{{ preview.timeline.overlay_events }} 层</span>
              </div>
              <div class="pv-trk">
                <span class="pv-tl">人声</span>
                <div class="pv-bar">
                  <i class="pv-fill a"
                     :style="{ width: pctOf(preview.summary.speech_seconds) }"></i>
                </div>
                <span class="pv-tv">{{ Math.round(preview.summary.speech_seconds) }}s</span>
              </div>
              <div class="pv-trk">
                <span class="pv-tl">字幕</span>
                <div class="pv-bar">
                  <i class="pv-fill s"
                     :style="{ width: pctOf(preview.timeline.subtitles
                                            ? preview.timeline.duration : 0) }"></i>
                </div>
                <span class="pv-tv">{{ preview.timeline.subtitles }} 条</span>
              </div>
              <div v-if="preview.audio.bgm" class="pv-trk">
                <span class="pv-tl">配乐</span>
                <div class="pv-bar">
                  <i class="pv-fill b"
                     :style="{ width: pctOf(preview.timeline.duration) }"></i>
                </div>
                <span class="pv-tv">音量 {{ preview.audio.bgm.volume }}</span>
              </div>
            </div>

            <!-- 叙事分组：每组带画面描述与人声内容，这是用户最需要核对的 -->
            <div class="pv-sub">叙事分组（{{ preview.story.length }} 段）</div>
            <div v-for="g in preview.story" :key="g.group_id" class="pv-group">
              <div class="pv-gh">
                <b>{{ g.group_id }}</b>
                <span v-if="g.summary" class="pv-gs">{{ g.summary }}</span>
                <span class="pv-gd">{{ Math.round(g.duration) }}s · {{ g.clip_count }} 镜</span>
              </div>
              <div v-if="g.captions && g.captions.length" class="pv-gc">
                <span v-for="(c, i) in g.captions" :key="i" class="pv-cap">🎬 {{ c }}</span>
              </div>
              <div v-if="g.speech && g.speech.length" class="pv-gsp">
                <span v-for="(t, i) in g.speech" :key="i" class="pv-sp">💬 {{ t }}</span>
              </div>
            </div>

            <!-- 镜头清单：可折叠，避免侧边栏被 173 个镜头淹没 -->
            <details class="pv-details">
              <summary>镜头清单（保留 {{ preview.shots.kept }} / 共
                {{ preview.shots.total }}）</summary>
              <div v-for="s in preview.shots.items" :key="s.id"
                   class="pv-shot" :class="{ drop: !s.kept }">
                <span class="pv-sid">{{ s.id }}</span>
                <span class="pv-st">{{ s.start }}–{{ s.end }}s</span>
                <span class="pv-sc">{{ s.caption || "（无描述）" }}</span>
                <span v-if="!s.kept" class="pv-drop">未保留</span>
              </div>
            </details>

            <div v-if="preview.material.items.length" class="pv-sub">素材</div>
            <div v-for="m in preview.material.items" :key="m.id" class="pv-mat">
              <span class="pv-sid">{{ m.id }}</span>
              <span class="pv-st">{{ m.duration }}s</span>
              <span class="pv-st">{{ m.resolution }}</span>
              <span class="pv-sc">{{ m.has_audio ? "含音轨" : "无音轨" }}</span>
            </div>
          </template>
        </section>

        <div class="pv-sep"></div>

        <div v-if="!planModal" class="lib-empty">暂无待确认的剪辑计划</div>
        <template v-else>
          <div v-if="(planModal.warnings || []).length" class="pc-warn">
            <div v-for="(w, i) in planModal.warnings" :key="i">⚠ {{ w }}</div>
          </div>
          <div v-if="planModal.plans.length > 1" class="pp-versions">
            <button v-for="p in planModal.plans" :key="p.plan_id"
                    class="pp-ver" :class="{ on: planModal.chosen === p.plan_id }"
                    @click="planModal.chosen = p.plan_id">
              {{ p.label }}
            </button>
          </div>
          <div class="pp-chain">
            <div v-for="s in planChosen(planModal).steps" :key="s.seq"
                 class="pp-step" :class="{ skipped: planModal.skip[skey(planModal, s.seq)] }">
              <div class="pp-step-head">
                <span class="pp-num">{{ s.seq }}</span>
                <b>{{ s.node_display }}</b>
                <span class="pp-badge" :title="s.is_mcp ? 'MCP 节点' : '内置工具'">{{ s.is_mcp ? 'MCP' : 'tool' }}</span>
              </div>
              <div v-if="s.why" class="pp-why">{{ s.why }}</div>
              <div v-if="s.expectation" class="pp-exp">→ {{ s.expectation }}</div>
              <div v-if="s.skills && s.skills.length" class="pp-skills">
                <span v-for="k in s.skills" :key="k" class="pp-skill">✦ {{ k }}</span>
              </div>
              <label v-if="s.skippable" class="pp-skip">
                <input type="checkbox" v-model="planModal.skip[skey(planModal, s.seq)]"
                       :disabled="planLocked(planModal)" /> 跳过
              </label>
            </div>
          </div>
          <div v-if="planModal.error" class="pc-error">✗ {{ planModal.error }}</div>
        </template>
      </div>

      <div v-if="activePanel === 'tools'" class="tool-drawer">
        <div v-if="loadingTools" class="lib-empty">正在加载工具库…</div>
        <div v-else-if="toolLib && toolLib.error" class="lib-empty bad">工具库加载失败：{{ toolLib.error }}</div>
        <template v-else-if="toolLib">
          <template v-for="(items, cat) in toolLib.groups" :key="cat">
            <div class="td-group">{{ cat }}</div>
            <div v-for="t in items" :key="t.name" class="td-item">
              <b>{{ toolLabel(t.name, t.name_display) }}</b>
              <button class="tt-copy" title="复制机器名（排障用）"
                      @click="copyMachine(t.name)">{{ copiedName === t.name ? "已复制" : "⧉" }}</button>
              <span class="td-desc">{{ t.desc }}</span>
            </div>
          </template>
          <template v-if="toolLib.skills && toolLib.skills.length">
            <div class="td-group">技能（Skill）</div>
            <div v-for="sk in toolLib.skills" :key="sk.name" class="td-item">
              <b>{{ toolLabel(sk.name, sk.name_display) }}</b>
              <button class="tt-copy" title="复制技能标识（排障用）"
                      @click="copyMachine(sk.name)">{{ copiedName === sk.name ? "已复制" : "⧉" }}</button>
              <span class="td-name">{{ sk.available }} · 常驻：{{ sk.always }}</span>
              <span class="td-desc">{{ sk.desc }}</span>
            </div>
          </template>

          <!-- —— MCP 服务管理（动态注册）：查 / 增 / 改 / 启停 / 删 —— -->
          <div class="td-group mcp-head">
            <span>MCP 服务（第三方工具源）</span>
            <span class="td-row-btns">
              <button class="help" @click="editMcp(null)">＋ 新增</button>
            </span>
          </div>
          <div v-if="mcpMsg" class="lib-empty">{{ mcpMsg }}</div>
          <div v-if="loadingMcp" class="lib-empty">加载中…</div>
          <div v-for="s in mcpServers" :key="s.name" class="td-item">
            <b>{{ s.name }}</b>
            <span class="td-name">
              <span :class="s.live ? 'st-ok' : 'st-bad'">{{ s.live ? "● 在线" : (s.enabled ? "○ 已启用未连" : "○ 停用") }}</span>
              · {{ (s.tools || []).length }} 个工具
            </span>
            <span class="td-desc">{{ (s.config && (s.config.url || s.config.command)) || "—" }}</span>
            <span class="td-row-btns">
              <button class="help" :disabled="mcpBusy" @click="toggleMcp(s)">{{ s.live ? "断开" : "启用" }}</button>
              <button class="help" @click="editMcp(s)">编辑</button>
              <button class="help danger" @click="delMcp(s)">删除</button>
            </span>
          </div>
          <div v-if="!loadingMcp && !mcpServers.length" class="lib-empty">
            还没有动态 MCP 服务（mcp.json 里的静态服务不在此列）。
          </div>
          <div v-if="mcpForm" class="mcp-form">
            <input v-model="mcpForm.name" placeholder="名称（字母数字与 _ -）" />
            <select v-model="mcpForm.type">
              <option value="streamableHttp">HTTP（streamableHttp）</option>
              <option value="stdio">本地进程（stdio）</option>
            </select>
            <input v-if="mcpForm.type === 'streamableHttp'"
                   v-model="mcpForm.url" placeholder="http://host:port/mcp" />
            <template v-else>
              <input v-model="mcpForm.command" placeholder="命令，如 npx" />
              <input v-model="mcpForm.args" placeholder="命令参数（逗号分隔）" />
            </template>
            <input v-model.number="mcpForm.tool_timeout" type="number" placeholder="工具超时（秒）" />
            <span class="td-row-btns">
              <button class="help" :disabled="mcpBusy || !mcpForm.name.trim()" @click="saveMcp">保存</button>
              <button class="help" @click="mcpForm = null">取消</button>
            </span>
          </div>

          <!-- —— 技能库管理 —— -->
          <div class="td-group mcp-head">
            <span>技能库管理</span>
            <span class="td-row-btns">
              <input type="file" id="skill-zip" accept=".zip" style="display:none"
                     @change="uploadSkillZip" />
              <button class="help" @click="pickSkillZip">上传 .zip</button>
              <button class="help" @click="reloadSkillsDir">重扫导入目录</button>
            </span>
          </div>
          <div v-if="skillsMsg" class="lib-empty">{{ skillsMsg }}</div>
          <div v-if="loadingSkillsAdmin" class="lib-empty">加载中…</div>
          <div v-for="sk in skillsAdmin" :key="'adm-' + sk.name" class="td-item">
            <b>{{ sk.display || sk.name }}</b>
            <span class="td-name">
              <span :class="sk.available ? 'st-ok' : 'st-bad'">{{ sk.available ? "可用" : "不可用" }}</span>
              · 常驻：{{ sk.always ? "是" : "否" }} · {{ (sk.files || []).length }} 个附件
            </span>
            <span class="td-row-btns">
              <button class="help danger" @click="delSkill(sk)">删除</button>
            </span>
          </div>
        </template>
      </div>

      <div v-if="activePanel === 'library'" class="library">
        <div class="lib-head">
          <span class="lib-count">{{ libraryItems.length }} 个素材</span>
          <button class="help" @click="loadMaterials" :disabled="loadingLibrary">
            {{ loadingLibrary ? "刷新中…" : "刷新" }}
          </button>
          <button
            v-if="libSelCount()"
            class="lib-add-btn"
            @click="addSelectedFromLibrary"
          >加入选中 {{ libSelCount() }} 项到本会话</button>
          <button
            v-if="libSelCount()"
            class="lib-del-btn"
            :disabled="deletingLib"
            @click="deleteSelected"
          >{{ deletingLib ? "删除中…" : `删除选中 ${libSelCount()} 项` }}</button>
        </div>
        <div v-if="!libraryItems.length && !loadingLibrary" class="lib-empty">
          还没有素材。上传后会自动出现在这里。
        </div>
        <div class="lib-grid">
          <div
            v-for="item in libraryItems"
            :key="item.material_id"
            class="lib-item"
            :class="{ sel: libSelected[item.material_id] }"
          >
            <label class="lib-check" :title="libSelected[item.material_id] ? '已选中' : '点选删除'">
              <input
                type="checkbox"
                :checked="!!libSelected[item.material_id]"
                @change="toggleLibSel(item.material_id)"
              />
              <span></span>
            </label>
            <video v-if="item.kind === 'video'" :src="item.url" class="lib-thumb" preload="metadata" controls></video>
            <img v-else-if="item.kind === 'image'" :src="item.url" class="lib-thumb" :alt="item.filename" />
            <audio v-else-if="item.kind === 'audio'" :src="item.url" class="lib-aud" controls preload="none"></audio>
            <div class="lib-meta">
              <b>{{ kindIcon(item.kind) }} {{ item.filename }}</b>
              <span>{{ fmtSize(item.bytes) }}<template v-if="item.duration"> · {{ Math.round(item.duration) }}s</template></span>
            </div>
            <button class="lib-add" @click="addFromLibrary(item)">加入本会话</button>
          </div>
        </div>
      </div>

      <div v-if="activePanel === 'bgm'" class="library">
        <div class="lib-head">
          <span class="lib-count">已导入曲库 · {{ bgmImported.length }} 首</span>
          <button class="help" @click="loadBgmImported" :disabled="loadingBgmImported">
            {{ loadingBgmImported ? "刷新中…" : "刷新" }}
          </button>
        </div>
        <div v-if="bgmImported.length" class="lib-grid">
          <div v-for="item in bgmImported" :key="item.material_id" class="lib-item">
            <div class="lib-meta">
              <b>🎵 {{ item.filename }}</b>
              <span>{{ fmtSize(item.bytes) }}<template v-if="item.duration"> · {{ Math.round(item.duration) }}s</template></span>
            </div>
            <audio v-if="item.url" :src="item.url" class="lib-aud" controls preload="none"></audio>
            <button class="lib-add" @click="addBgmImported(item)">加入本会话</button>
            <button class="lib-del-btn" @click="deleteBgmImported(item)" :disabled="deletingBgm === item.material_id">
              {{ deletingBgm === item.material_id ? "删除中…" : "删除" }}
            </button>
          </div>
        </div>
        <div v-else-if="!loadingBgmImported" class="lib-empty">
          曲库还没有导入歌曲。在下方搜索框搜到喜欢的歌，点「导入并做配乐」即可加入曲库。
        </div>
        <div class="lib-divider"></div>
        <div class="lib-head">
          <span class="lib-count">{{ bgmHint || "在线搜索" }} · {{ bgmItems.length }} 首</span>
          <input
            v-model="bgmQuery"
            class="bgm-search"
            placeholder="搜歌名或歌手…"
            @keydown.enter.prevent="loadBgm"
          />
          <button class="help" @click="loadBgm" :disabled="loadingBgm">
            {{ loadingBgm ? "搜索中…" : "搜索" }}
          </button>
        </div>
        <div v-if="bgmError" class="lib-empty bad">{{ bgmError }}</div>
        <div v-if="!bgmItems.length && !loadingBgm && !bgmError" class="lib-empty">
          输入歌名或歌手名搜索，搜到的歌可以试听、导入做配乐。
        </div>
        <div class="lib-grid">
          <div v-for="item in bgmItems" :key="item.track_id + '|' + item.source" class="lib-item">
            <div class="lib-meta">
              <b>🎵 {{ item.name }}</b>
              <span>{{ item.artist }} · {{ item.source }}</span>
            </div>
            <audio
              v-if="bgmAudioUrl[item.track_id + '|' + item.source]"
              :src="bgmAudioUrl[item.track_id + '|' + item.source]"
              class="lib-aud" controls preload="none"
            ></audio>
            <button v-else class="lib-add" @click="playBgm(item)">试听</button>
            <button class="lib-add" @click="useBgm(item)" :disabled="importingBgm === item.track_id">
              {{ importingBgm === item.track_id ? "导入中…" : "导入并做配乐" }}
            </button>
          </div>
        </div>
      </div>

      <div v-if="activePanel === 'settings'" class="settings">
        <div class="set-line">
          <span class="set-k">模型密钥</span>
          <span v-if="settings && settings.api_key.configured" class="set-v">
            已配置 · {{ settings.api_key.masked }} · 生效来源：{{ settings.api_key.source }}
          </span>
          <span v-else-if="settings && settings.api_key.effective" class="set-v">
            本机未配置，正在用 {{ settings.api_key.source }}
          </span>
          <span v-else class="set-v bad">未配置 —— 视觉理解与文案会整链降级</span>
        </div>
        <div class="set-line">
          <input
            v-model="keyDraft"
            type="password"
            autocomplete="off"
            spellcheck="false"
            placeholder="粘贴 API Key（sk-…），只存进本机 PG"
            :disabled="savingKey"
            @keydown.enter.prevent="saveKey"
          />
          <button class="clip" :disabled="!keyDraft.trim() || savingKey" @click="saveKey">
            {{ savingKey ? "保存中…" : "保存" }}
          </button>
          <button
            v-if="settings && settings.api_key.configured"
            class="clip ghost"
            :disabled="savingKey"
            @click="clearKey"
          >清除</button>
          <button class="clip" :disabled="testingKey" @click="testConn">
            {{ testingKey ? "自检中…" : "测试连接" }}
          </button>
        </div>
        <p v-if="settings" class="set-note">
          当前通道 {{ settings.model }} @ {{ settings.base_url }}；保存即生效，剪辑服务最多
          {{ settings.hot_reload_sec }} 秒后跟上，两个服务都不用重启。
          模型思考模式当前为<b>{{ settings.thinking ? "开" : "关" }}</b
          >（关掉时答复更快，长推理任务才需要开：环境变量 OPENAI_THINKING=on）。
          <template v-if="settings.api_key.updated_at">上次修改 {{ settings.api_key.updated_at.slice(0, 19).replace("T", " ") }}。</template>
        </p>
        <div v-if="testResult" class="probe">
          <div>
            <b :class="testResult.text && testResult.text.ok ? 'ok' : 'bad'">文本</b>
            {{ testResult.text ? testResult.text.ms + "ms · " + testResult.text.detail : "—" }}
          </div>
          <div>
            <b :class="testResult.vision && testResult.vision.ok ? 'ok' : 'bad'">视觉</b>
            {{ testResult.vision ? testResult.vision.ms + "ms · " + testResult.vision.detail : "—" }}
          </div>
          <div class="set-note">密钥来源 {{ testResult.source }} · {{ testResult.masked || "无可用 key" }}</div>
        </div>
        <div v-if="keyError" class="set-v bad">{{ keyError }}</div>
      </div>

      <div v-if="activePanel === 'timeline'" class="timeline-panel">
        <div v-if="timelineError" class="set-v bad">{{ timelineError }}</div>

        <template v-if="!editingTimeline">
          <div class="tl-toolbar">
            <button class="clip" @click="importLatestTimeline">从当前会话导入</button>
            <button class="clip ghost" @click="loadTimelineList" :disabled="loadingTimeline">
              {{ loadingTimeline ? "刷新中…" : "刷新" }}
            </button>
          </div>
          <div v-if="!timelineList.length && !loadingTimeline" class="lib-empty">
            还没有保存的时间线。点击「从当前会话导入」获取 AI 生成的最新时间线。
          </div>
          <div v-for="tl in timelineList" :key="tl.id" class="tl-item" @click="openTimeline(tl)">
            <b>{{ tl.name }}</b>
            <span class="td-name">{{ tl.duration_sec ? Math.round(tl.duration_sec) + "s" : "未渲染" }}</span>
            <span class="td-desc">{{ new Date(tl.updated_at * 1000).toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) }}</span>
            <button class="tl-del" @click.stop="deleteTimeline(tl)">删除</button>
          </div>
        </template>

        <template v-if="editingTimeline">
          <div class="tl-edit-toolbar">
            <button class="clip ghost" @click="editingTimeline = null">← 返回列表</button>
            <button class="clip" @click="saveTimeline">保存</button>
            <button class="clip" :disabled="renderingTimeline" @click="renderDirect">
              {{ renderProgress && renderProgress.percent != null && renderProgress.status !== "done"
                 ? `渲染中 ${renderProgress.percent}%` : renderingTimeline ? "渲染中…" : "重新渲染" }}
            </button>
          </div>

          <input
            v-model="editingTimeline.name"
            class="tl-name-input"
            placeholder="时间线名称"
          />

          <div v-if="editingTimeline.video_url" class="tl-video">
            <video :src="editingTimeline.video_url" controls preload="metadata"></video>
            <span v-if="editingTimeline.duration_sec" class="td-name">
              时长 {{ Math.round(editingTimeline.duration_sec) }}s
            </span>
          </div>

          <div v-if="editingTimeline.payload && editingTimeline.payload.subtitles" class="tl-section">
            <div class="tl-section-head">字幕（可编辑）</div>
            <div
              v-for="(sub, i) in editingTimeline.payload.subtitles"
              :key="i"
              class="tl-sub-row"
            >
              <input
                v-model="sub.text"
                class="tl-sub-text"
                placeholder="字幕文字"
              />
              <input
                v-model.number="sub.start"
                type="number"
                step="0.1"
                class="tl-sub-time"
              />
              <input
                v-model.number="sub.end"
                type="number"
                step="0.1"
                class="tl-sub-time"
              />
            </div>
          </div>

          <div v-if="editingTimeline.payload && editingTimeline.payload.bgm" class="tl-section">
            <div class="tl-section-head">背景音乐</div>
            <div class="tl-bgm-row">
              <label>音量</label>
              <input
                v-model.number="editingTimeline.payload.bgm.volume"
                type="range"
                min="0"
                max="1"
                step="0.05"
                class="tl-vol-slider"
              />
              <span>{{ (editingTimeline.payload.bgm.volume || 0).toFixed(2) }}</span>
            </div>
          </div>

          <div v-if="editingTimeline.payload && editingTimeline.payload.events" class="tl-section">
            <div class="tl-section-head">画面轨（{{ editingTimeline.payload.events.length }} 段）</div>
            <div
              v-for="(ev, i) in editingTimeline.payload.events"
              :key="i"
              class="tl-event-row"
            >
              <span class="td-name">{{ i + 1 }}.</span>
              <span>{{ ev.start.toFixed(1) }}–{{ ev.end.toFixed(1) }}s</span>
              <span class="td-desc">{{ (ev.path || '').split('/').pop() }}</span>
            </div>
          </div>
        </template>
      </div>

      <div v-if="activePanel === 'runs'" class="runs">
        <div class="lib-head">
          <span class="lib-count">{{ runList.length }} 次执行</span>
          <button class="help" @click="loadRuns" :disabled="loadingRuns">
            {{ loadingRuns ? "刷新中…" : "刷新" }}
          </button>
        </div>
        <div v-if="runsError" class="lib-empty bad">{{ runsError }}</div>
        <div v-else-if="loadingRuns && !runList.length" class="lib-empty">正在取执行记录…</div>
        <div v-else-if="!runList.length" class="lib-empty">
          这个会话还没有执行记录。发一句话（或点一条剪辑卡片）之后再回来看。
        </div>

        <div
          v-for="run in runList"
          :key="run.run_id"
          class="ru-item"
          :class="{ sel: openRun === run.run_id }"
        >
          <div class="ru-row" @click="toggleRunHistory(run)">
            <b class="ru-status" :class="run.status">{{ runStatusText(run.status) }}</b>
            <span class="ru-msg">{{ run.message || "（没有指令文本）" }}</span>
            <span class="td-name">{{ run.run_id }}</span>
          </div>
          <div class="ru-meta">
            <span>第 {{ run.iteration }} 轮</span>
            <span>一致点 0–{{ run.head_seq }}</span>
            <span v-if="run.artifact_id && run.artifact_id !== '_default'">
              产物集 {{ run.artifact_id }}
            </span>
            <span v-if="run.forked_from">
              ← 分自 {{ run.forked_from }} 第 {{ run.forked_at_seq }} 步
            </span>
            <span v-if="run.plan_run_id" class="ru-line" title="这条执行是按哪次规划的计划卡跑的">
              ↩ 承计划 {{ run.plan_run_id }}
            </span>
            <span v-if="run.has_plan_cards" class="ru-plan" title="这条规划 run 产出了待确认的候选计划">
              🗂 有候选计划
            </span>
            <span v-if="auditCount(run.plan_audit || {})" class="ru-audit">
              ⚖ 计划外 {{ (run.plan_audit.extra || []).length }} · 未履行 {{ (run.plan_audit.unfulfilled || []).length }}
            </span>
            <span>{{ runTime(run) }}</span>
          </div>

          <div v-if="openRun === run.run_id" class="ru-body">
            <div v-if="auditCount(run.plan_audit || {})" class="ru-audit-body">
              <div class="tl-section-head">与已确认计划的出入 · 只观测不拦截</div>
              <div v-if="(run.plan_audit.extra || []).length" class="ac-row">
                <em>计划外</em>
                <span>{{ run.plan_audit.extra.map((n) => toolLabel(n)).join('、') }}</span>
              </div>
              <div v-if="(run.plan_audit.unfulfilled || []).length" class="ac-row">
                <em>未履行</em>
                <span>{{ run.plan_audit.unfulfilled.map((n) => toolLabel(n)).join('、') }}</span>
              </div>
              <div v-if="run.plan_audit.reason" class="ru-reason">{{ run.plan_audit.reason }}</div>
            </div>
            <div v-if="!runPoints[run.run_id]" class="lib-empty">正在取一致点链…</div>
            <div v-else-if="runPoints[run.run_id].error" class="lib-empty bad">
              {{ runPoints[run.run_id].error }}
            </div>
            <template v-else>
              <div class="tl-section-head">一致点链 · 点一个当分叉点（回到它之前，它跑的那步重做）</div>
              <div
                v-for="pt in pointsOf(run.run_id)"
                :key="pt.seq"
                class="ru-point"
                :class="{ on: forkDraft.run_id === run.run_id && forkDraft.point === pt.seq }"
                @click="pickForkPoint(run, pt)"
              >
                <span class="td-name">#{{ pt.seq }}</span>
                <span>{{ pt.kind === 'full' ? '基准' : '增量' }}</span>
                <span>{{ pt.messages }} 条</span>
                <span class="td-desc">第 {{ pt.iteration }} 轮</span>
                <span v-if="(pt.tools || []).length" class="ru-point-tools">
                  {{ (pt.tools || []).map((n, i) => toolLabel(n, (pt.tools_display || [])[i])).join('、') }}
                </span>
              </div>

              <div class="ru-actions">
                <button class="clip ghost" :disabled="forking" @click="doResume(run)">
                  {{ forking ? "提交中…" : "从最新一致点续跑" }}
                </button>
              </div>

              <div v-if="forkDraft.run_id === run.run_id" class="ru-fork">
                <div class="tl-section-head">
                  回到 #{{ forkDraft.point }} 之前分叉：要重做哪些节点
                </div>
                <div class="ru-nodes">
                  <button
                    v-for="n in forkNodeOptions()"
                    :key="n.name"
                    class="ru-node"
                    :class="{ on: forkDraft.nodes.includes(n.name) }"
                    @click="toggleForkNode(n.name)"
                  >{{ n.label }}</button>
                </div>
                <input
                  v-model="forkDraft.message"
                  class="tl-name-input"
                  placeholder="分叉后带一句新指令（留空则沿用原话）"
                />
                <div class="ru-hint">
                  勾上的节点会连同它的全部下游产物一起作废，未勾的上游产物直接复用；
                  新执行换一个产物作用域，父执行自动让位不再被重播。
                </div>
                <div class="ru-actions">
                  <button class="clip" :disabled="forking" @click="doFork(run)">
                    {{ forking ? "提交中…" : "从这里分叉重跑" }}
                  </button>
                  <button class="clip ghost" :disabled="forking" @click="dropRunDraft(run.run_id)">
                    取消
                  </button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
    </aside>

    <!-- 计划确认弹窗已改为：统一提问卡（见下方 questionCard），完整链路在右侧侧边栏 -->

    <!-- 统一提问卡：编号选项 + 推荐徽标 + 截断描述 + 铅笔自定义 + 分页 + 下一题。
         四种来源（模型提问 / 计划确认 / 渲染前确认 / 渲染兜底）共用这一个组件，
         避免出现几套形态不一的问答 UI。 -->
    <div v-if="questionCard" class="plan-modal-mask qc-mask">
      <div class="plan-modal qc" :class="{ full: questionCard.fullscreen }">
        <div class="pm-head qc-head">
          <b class="qc-step">第 {{ questionCard.page + 1 }} / {{ questionCard.pages.length }} 题</b>
          <div class="qc-pager">
            <button class="qc-pg" :disabled="questionCard.page <= 0"
                    title="上一题" @click="qPrev">‹</button>
            <span class="qc-pgn">{{ questionCard.page + 1 }}/{{ questionCard.pages.length }}</span>
            <button class="qc-pg" :disabled="qLastPage()"
                    title="下一题" @click="qNext">›</button>
            <button class="qc-pg" :title="questionCard.fullscreen ? '还原' : '放大'"
                    @click="qFullscreen">⤢</button>
          </div>
          <button class="pm-x" :title="'收起（选择仍会保留，随时可再打开）'"
                  @click="qClose">×</button>
        </div>

        <p class="qc-title">{{ qCurrentPage().title }}</p>
        <p v-if="questionCard.reason && questionCard.reason !== qCurrentPage().title"
           class="ap-reason">{{ questionCard.reason }}</p>

        <!-- 编排预览摘要：用户在弹窗里也能看到关键数字，细节去侧边栏 -->
        <div v-if="questionCard.previewSummary" class="qc-preview">
          <span v-for="(v, k) in questionCard.previewSummary" :key="k" class="qc-pv">
            <em>{{ k }}</em>{{ v }}
          </span>
        </div>

        <ul class="qc-options">
          <li v-for="o in qCurrentPage().options" :key="o.key"
              class="qc-opt"
              :class="{ on: qCurrentPage().multi
                          ? qCurrentPage().picked.includes(o.key)
                          : (questionCard.answers[questionCard.page] === o.key
                             && !qCurrentPage().customOpen) }"
              @click="qPick(questionCard.page, o.key)">
            <span class="qc-num">{{ o.index }}</span>
            <span class="qc-body">
              <span class="qc-label">
                {{ o.label }}
                <span v-if="o.recommended" class="qc-badge">{{ RECOMMEND_LABEL }}</span>
                <span v-if="o.badge" class="qc-badge soft">{{ o.badge }}</span>
              </span>
              <span v-if="o.description" class="qc-desc">{{ o.description }}</span>
            </span>
          </li>
          <li v-if="qCurrentPage().allowCustom" class="qc-opt qc-custom-row"
              :class="{ on: qCurrentPage().customOpen }"
              @click="qToggleCustom(questionCard.page)">
            <span class="qc-num qc-pencil">✎</span>
            <span class="qc-body">
              <span class="qc-label">{{ qCurrentPage().customHint }}</span>
              <input v-if="qCurrentPage().customOpen"
                     v-model="qCurrentPage().custom"
                     class="qc-input" :placeholder="'在这里写你想要的做法…'"
                     @click.stop />
            </span>
          </li>
        </ul>

        <!-- 工具调用明细：审批类才显示，折叠起来不干扰选择 -->
        <details v-if="questionCard.approval && (questionCard.approval.calls || []).length"
                 class="qc-details">
          <summary>这一步会执行什么（{{ questionCard.approval.calls.length }} 个动作）</summary>
          <ul class="ap-calls">
            <li v-for="c in questionCard.approval.calls" :key="c.id">
              <b>{{ c.name }}</b>
              <span class="ap-args" v-if="Object.keys(c.arguments || {}).length">
                {{ JSON.stringify(c.arguments) }}
              </span>
            </li>
          </ul>
        </details>

        <div class="qc-actions">
          <button class="clip" :disabled="questionCard.submitting"
                  @click="qSubmit">
            {{ questionCard.submitting ? "提交中…"
               : (qLastPage() ? questionCard.submitLabel : "下一题") }}
          </button>
          <button v-if="questionCard.pages.length > 1" class="clip ghost"
                  :disabled="questionCard.submitting" @click="qNext">
            跳过这题
          </button>
          <button class="clip ghost" :disabled="questionCard.submitting" @click="qClose">
            稍后再说
          </button>
          <span class="qc-hint">
            {{ qAnswered(questionCard.page) ? "" : "选一项，或用 ✎ 自己写" }}
          </span>
        </div>
        <div v-if="questionCard.error" class="pc-error">✗ {{ questionCard.error }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.shell { display: flex; height: 100%; position: relative; z-index: 1; }

/* ---------- 卷宗栏 ---------- */
.rail {
  width: 264px; flex: none; display: flex; flex-direction: column;
  background: linear-gradient(180deg, #2b2620, #211d18);
  color: #e9e2d2; padding: 22px 16px 16px;
  border-right: 3px solid var(--vermilion);
}
.brand { display: flex; gap: 12px; align-items: center; padding: 0 4px 18px; }
.seal {
  width: 46px; height: 46px; flex: none; display: grid; place-items: center;
  background: var(--vermilion); color: #fff9ef;
  font-family: var(--serif); font-weight: 900; font-size: 24px;
  border-radius: 8px; box-shadow: 0 0 0 2px #211d18, 0 0 0 3px var(--gold);
}
.brand-text { display: flex; flex-direction: column; line-height: 1.25; }
.brand-text b { font-family: var(--serif); font-size: 17px; letter-spacing: 2px; }
.brand-text span { font-size: 11px; opacity: 0.55; letter-spacing: 4px; }

.new {
  margin: 0 4px 14px; padding: 10px 0; cursor: pointer;
  background: transparent; color: #efe7d6; font-family: var(--serif);
  border: 1px dashed rgba(233, 226, 210, 0.4); border-radius: 6px;
  font-size: 14px; letter-spacing: 3px; transition: all 0.2s;
}
.new:hover { border-color: var(--vermilion); color: #ffd9c9; background: rgba(200, 64, 31, 0.12); }

.convs { flex: 1; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
.conv {
  display: flex; align-items: center; gap: 8px; padding: 9px 10px;
  border-radius: 6px; cursor: pointer; color: rgba(233, 226, 210, 0.75);
  font-size: 13px; position: relative;
}
.conv:hover { background: rgba(255, 255, 255, 0.05); }
.conv.on { background: rgba(200, 64, 31, 0.18); color: #fdf6e8; }
.conv.on::before {
  content: ""; position: absolute; left: -4px; top: 20%; bottom: 20%;
  width: 3px; background: var(--vermilion); border-radius: 2px;
}
.conv .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv .x {
  border: 0; background: none; color: rgba(233, 226, 210, 0.3); font-size: 15px;
  cursor: pointer; padding: 0 2px;
}
.conv .x:hover { color: var(--vermilion); }
.dot { width: 6px; height: 6px; flex: none; border-radius: 50%; background: rgba(233, 226, 210, 0.25); }
.dot.live { background: var(--gold); animation: pulse 1s infinite; }
@keyframes pulse { 50% { opacity: 0.25; } }

.who {
  display: flex; align-items: center; gap: 8px; padding: 12px 6px 2px;
  border-top: 1px solid rgba(233, 226, 210, 0.12); font-size: 11px;
}
.who .k { font-family: var(--serif); letter-spacing: 3px; opacity: 0.5; }
.who .v { opacity: 0.75; font-family: ui-monospace, monospace; }

/* ---------- 稿纸区 ---------- */
.desk { flex: 1; display: flex; flex-direction: column; min-width: 0; }

/* ---------- 右侧抽屉 ---------- */
.drawer {
  width: 380px; flex: none; display: flex; flex-direction: column;
  background: rgba(255, 253, 247, 0.72);
  border-left: 1px solid var(--line);
  overflow: hidden;
}
.drawer-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 18px 12px; border-bottom: 1px solid var(--line);
}
.drawer-head b { font-family: var(--serif); font-size: 16px; letter-spacing: 2px; color: var(--ink); }
.drawer-x {
  width: 28px; height: 28px; border: none; border-radius: 4px;
  background: transparent; font-size: 18px; color: var(--ink-soft);
  cursor: pointer; line-height: 1;
}
.drawer-x:hover { background: rgba(200, 64, 31, 0.08); color: var(--vermilion); }
.drawer .library, .drawer .settings {
  flex: 1; overflow-y: auto; padding: 14px 16px 16px;
}
.tool-drawer { flex: 1; overflow-y: auto; padding: 10px 16px 16px; }
.td-group {
  font-family: var(--serif); font-size: 12.5px; letter-spacing: 3px; color: var(--vermilion-deep);
  margin: 14px 0 6px; padding-bottom: 4px; border-bottom: 1px solid var(--line);
}
.td-item {
  display: flex; flex-direction: column; gap: 2px; padding: 8px 0;
  border-bottom: 1px dashed rgba(233, 226, 210, 0.3);
}
.td-item b { font-family: var(--serif); font-size: 13.5px; color: var(--ink); }
.td-name { font-size: 11px; color: var(--ink-soft); font-family: ui-monospace, monospace; opacity: 0.6; }
.td-desc { font-size: 12px; color: var(--ink-soft); line-height: 1.6; }

.head {
  display: flex; align-items: baseline; gap: 14px; padding: 18px 34px 14px;
  border-bottom: 1px solid var(--line);
}
.head .rule { width: 26px; height: 3px; background: var(--vermilion); align-self: center; }
.head h2 { font-family: var(--serif); font-size: 19px; font-weight: 600; letter-spacing: 1px; flex: 1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ws { font-size: 11px; letter-spacing: 2px; color: var(--ink-soft); }
.ws.on { color: var(--vermilion); }

.paper { flex: 1; overflow-y: auto; padding: 26px 34px 12px; scroll-behavior: smooth; }

/* 空态 */
.hello { max-width: 560px; margin: 5vh auto 0; text-align: center; position: relative; }
.hello .vert {
  position: absolute; right: -56px; top: 6px; writing-mode: vertical-rl;
  font-family: var(--serif); font-weight: 900; letter-spacing: 14px;
  color: rgba(200, 64, 31, 0.18); font-size: 30px; user-select: none;
}
.hello h3 { font-family: var(--serif); font-size: 34px; font-weight: 900; letter-spacing: 2px; }
.hello h3 em { font-style: normal; color: var(--vermilion); }
.hello p { margin-top: 10px; color: var(--ink-soft); letter-spacing: 2px; font-size: 14px; }
/* 功能快捷入口 */
.cats { max-width: 860px; margin: 26px auto 8px; }
.hello + .cats { margin-top: 30px; }
.cats.panel {
  border: 1px dashed var(--line); border-radius: 8px; padding: 14px 16px 6px;
  background: rgba(255, 253, 247, 0.72); margin-bottom: 22px;
}
.cat-g {
  font-family: var(--serif); font-size: 13px; letter-spacing: 4px; color: var(--vermilion-deep);
  margin: 10px 2px 10px; display: flex; align-items: center; gap: 10px;
}
.cat-g::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.cat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }
.cat {
  text-align: left; display: flex; flex-direction: column; gap: 6px;
  padding: 12px 14px; border-radius: 4px; font-family: var(--sans);
  background: rgba(255, 253, 247, 0.85); border: 1px solid var(--line);
  box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.05);
}
.cat b { font-family: var(--serif); font-size: 14.5px; letter-spacing: 1px; color: var(--ink); }
.cat .need { font-size: 12px; color: var(--vermilion-deep); letter-spacing: 0.5px; }
.cat .say { font-size: 12px; color: var(--ink-soft); line-height: 1.7;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }

.help {
  flex: none; cursor: pointer; font-family: var(--serif); font-size: 12.5px; letter-spacing: 2px;
  color: var(--ink-soft); background: transparent; border: 1px solid var(--line); border-radius: 999px;
  padding: 5px 14px; transition: all 0.18s;
}
.help:hover, .help.on { color: var(--vermilion); border-color: var(--vermilion); }

/* 消息 */
.row { display: flex; gap: 12px; margin-bottom: 20px; animation: rise 0.25s ease both; }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } }
.row.user { flex-direction: row-reverse; }
.mark { flex: none; width: 30px; display: grid; place-items: start; padding-top: 4px; }
.mark span {
  font-family: var(--serif); font-size: 13px; width: 28px; height: 28px;
  display: grid; place-items: center; border-radius: 5px;
}
.row.user .mark span { background: var(--ink); color: var(--paper); }
.seal-mini { background: var(--vermilion); color: #fff8ec; font-weight: 700; }
.seal-mini.streaming { animation: pulse 1.1s infinite; }
.seal-mini.error { background: #7a1f0e; }

.bubble {
  max-width: 72ch; padding: 12px 18px; border-radius: 3px; line-height: 1.85;
  font-size: 14.5px; white-space: pre-wrap; word-break: break-word;
  background: rgba(255, 253, 247, 0.88); border: 1px solid var(--line);
  box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.05);
}
.row.user .bubble { background: var(--bubble-user); color: #f2ecdd; border-color: var(--ink); }
.bubble.error { background: #fbeae4; border-color: #e0b39e; color: #7a1f0e; }
.caret { color: var(--vermilion); animation: blink 0.9s steps(1) infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* 成片播放卡片 */
.media-card { display: flex; flex-direction: column; gap: 8px; }
.media-card video {
  width: min(480px, 68vw); border-radius: 4px; display: block;
  border: 1px solid var(--line); box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.08);
  background: #16130f;
}
.media-card .meta { display: flex; align-items: baseline; gap: 12px; font-size: 13px; }
.media-card .meta b { font-family: var(--serif); letter-spacing: 1px; }
.media-card .meta span { color: var(--ink-soft); }
.media-card .meta a {
  margin-left: auto; color: var(--vermilion-deep); text-decoration: none;
  letter-spacing: 2px; border-bottom: 1px dashed var(--vermilion);
}
.media-card .meta a:hover { color: var(--vermilion); }

/* 证据分级：一条主张一行，验过的实、没验的淡 —— 用户要看的正是这个区分 */
.mc-ev { font-size: 12.5px; color: var(--ink-soft); }
.mc-ev summary { cursor: pointer; letter-spacing: 1px; }
.mc-ev summary i { font-style: normal; }
.mc-ev summary i.ok { color: var(--pine-deep, #2f6b46); }
.mc-ev summary i.no { color: var(--vermilion-deep); }
.mc-evrow {
  display: flex; gap: 8px; align-items: baseline;
  padding: 3px 0; border-bottom: 1px dashed var(--line);
}
.mc-evrow.un { opacity: .62; }
.mc-evlv {
  flex: none; font-family: ui-monospace, monospace; font-size: 11.5px;
  letter-spacing: 1px; color: var(--ink);
}
.mc-evrow.un .mc-evlv { color: var(--vermilion-deep); }
.mc-evcl { min-width: 0; }
.mc-evnote { padding-top: 6px; font-size: 11.5px; opacity: .8; }

/* 工具调用追踪卡片 */
.tool-trace {
  font-family: ui-monospace, monospace; font-size: 12.5px; line-height: 1.7;
  display: flex; flex-direction: column; gap: 4px;
}
.tool-trace.err .tt-head b { color: var(--vermilion-deep); }
.tt-head { display: flex; align-items: center; gap: 6px; }
.tt-head b { font-family: var(--serif); font-size: 13px; color: var(--ink); }
.tt-copy {
  border: none; background: transparent; cursor: pointer; padding: 0 3px;
  font-family: ui-monospace, monospace; font-size: 10px; color: var(--ink-soft);
  opacity: 0.45; line-height: 1.4;
}
.tt-copy:hover { opacity: 0.95; color: var(--vermilion-deep); }
.tt-ico { font-size: 14px; }
.tt-spin { color: var(--vermilion); animation: pulse 0.9s infinite; font-size: 11px; }
.tt-time { color: var(--ink-soft); font-size: 11px; }
.tt-args { display: flex; flex-wrap: wrap; gap: 4px 8px; padding-left: 22px; color: var(--ink-soft); }
.tt-ak { color: var(--vermilion-deep); }
.tt-av { color: var(--ink-soft); }
.tt-result {
  padding: 6px 10px; margin-left: 22px; max-height: 120px; overflow-y: auto;
  background: rgba(38, 34, 28, 0.06); border-radius: 3px;
  white-space: pre-wrap; word-break: break-word; color: var(--ink-soft);
}
.tt-error { padding-left: 22px; color: var(--vermilion-deep); }

/* 子 Agent 追踪卡片 */
.sub-trace {
  font-family: ui-monospace, monospace; font-size: 12.5px; line-height: 1.7;
  display: flex; flex-direction: column; gap: 4px;
  border-left: 2px solid var(--vermilion); padding-left: 10px;
}
.st-head { display: flex; align-items: center; gap: 6px; }
.st-head b { font-family: var(--serif); font-size: 13px; color: var(--vermilion); }
.st-ico { font-size: 14px; }
.st-ok { color: var(--vermilion); }
.st-task { color: var(--ink-soft); padding-left: 22px; }

/* 落笔栏 */
.composer {
  display: flex; gap: 12px; align-items: flex-end; padding: 14px 34px 24px;
  border-top: 1px solid var(--line); background: linear-gradient(180deg, transparent, rgba(239, 231, 214, 0.5));
}
.composer textarea {
  flex: 1; resize: none; max-height: 160px; padding: 12px 16px;
  font-family: var(--sans); font-size: 14.5px; line-height: 1.7; color: var(--ink);
  background: rgba(255, 253, 247, 0.9); border: 1px solid var(--line); border-radius: 4px;
  outline: none; transition: border 0.2s;
}
.composer textarea:focus { border-color: var(--vermilion); box-shadow: 0 0 0 3px rgba(200, 64, 31, 0.08); }
.send {
  flex: none; padding: 12px 26px; cursor: pointer; letter-spacing: 4px;
  font-family: var(--serif); font-size: 15px; font-weight: 600;
  background: var(--vermilion); color: #fff8ec; border: 0; border-radius: 4px;
  box-shadow: 0 4px 0 var(--vermilion-deep); transition: all 0.15s;
}
.send:active { transform: translateY(3px); box-shadow: 0 1px 0 var(--vermilion-deep); }
.send.off { background: var(--line); color: #fff8ec; box-shadow: 0 4px 0 #c4b493; cursor: default; }

/* 素材上传 */
.clip {
  flex: none; padding: 12px 14px; cursor: pointer; letter-spacing: 2px;
  font-family: var(--serif); font-size: 13px;
  background: transparent; color: var(--ink-soft);
  border: 1px dashed var(--line); border-radius: 4px; transition: all 0.18s;
}
.clip:hover { color: var(--vermilion); border-color: var(--vermilion); }
.clip.busy { color: var(--vermilion); border-style: solid; cursor: wait; }
.clip .spin { animation: pulse 0.9s infinite; }
.paper { position: relative; }
.paper.dropping { outline: 2px dashed var(--vermilion); outline-offset: -8px; background: rgba(200, 64, 31, 0.04); }
.drop-hint {
  position: sticky; top: 8px; z-index: 5; margin: 0 auto 6px; width: max-content;
  padding: 6px 18px; border-radius: 999px; font-size: 13px; letter-spacing: 2px;
  background: var(--vermilion); color: #fff8ec; box-shadow: 0 6px 18px rgba(200, 64, 31, 0.35);
}

/* 待发附件条：输入框上方，chip 可单个移除 */
.attach-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 0 34px 10px; margin-top: -4px;
}
.attach-bar .ab-label {
  font-family: var(--serif); font-size: 11.5px; letter-spacing: 3px; color: var(--ink-soft);
}
.chip {
  display: flex; align-items: center; gap: 7px; max-width: 320px;
  padding: 5px 6px 5px 10px; border-radius: 999px; font-size: 12.5px;
  background: rgba(255, 253, 247, 0.92); border: 1px solid var(--vermilion);
  box-shadow: 2px 2px 0 rgba(200, 64, 31, 0.14);
}
.chip .fn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip .mt { color: var(--ink-soft); flex: none; }
.chip .rm {
  flex: none; border: 0; background: none; cursor: pointer; color: var(--ink-soft);
  font-size: 15px; line-height: 1; padding: 0 3px;
}
.chip .rm:hover { color: var(--vermilion); }

/* 链接取料条：点「＋链接」后在落笔栏上方展开 */
.clip.on { color: var(--vermilion); border-color: var(--vermilion); border-style: solid; }
.clip:disabled { opacity: 0.45; cursor: default; }
.link-bar {
  display: flex; gap: 10px; align-items: center;
  padding: 0 34px 10px; margin-top: -4px;
}
.link-bar input {
  flex: 1; min-width: 0; padding: 10px 14px;
  font-family: ui-monospace, monospace; font-size: 13px; color: var(--ink);
  background: rgba(255, 253, 247, 0.9); border: 1px dashed var(--vermilion); border-radius: 4px;
  outline: none;
}
.link-bar input:focus { border-style: solid; box-shadow: 0 0 0 3px rgba(200, 64, 31, 0.08); }

/* 设置面板：密钥自配置 + 连通性自检（与功能卡片同一套纸面语言） */
.settings {

  display: flex; flex-direction: column; gap: 10px;
}
.set-line { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.set-line input {
  flex: 1; min-width: 240px; padding: 9px 12px;
  font-family: ui-monospace, monospace; font-size: 13px; color: var(--ink);
  background: rgba(255, 253, 247, 0.9); border: 1px dashed var(--line); border-radius: 4px;
  outline: none;
}
.set-line input:focus { border-color: var(--vermilion); border-style: solid; }
.set-k { font-family: var(--serif); font-size: 13px; color: var(--ink-soft); letter-spacing: 1px; }
.set-v { font-size: 12.5px; color: var(--ink-soft); }
.set-note { margin: 0; font-size: 12px; line-height: 1.7; color: var(--ink-soft); }
.ok { color: var(--vermilion); }
.bad { color: var(--vermilion-deep); }
.clip.ghost { border-style: dotted; color: var(--ink-soft); }
.probe {
  display: flex; flex-direction: column; gap: 4px; padding: 9px 11px;
  border-left: 2px solid var(--line); font-family: ui-monospace, monospace; font-size: 12px;
  color: var(--ink-soft);
}
.probe b { font-weight: 600; margin-right: 6px; }

/* 气泡内的附件卡片 */
.atts { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
.att {
  display: flex; align-items: center; gap: 10px; padding: 7px 9px;
  border-radius: 3px; background: rgba(38, 34, 28, 0.10);
  border: 1px solid rgba(255, 255, 255, 0.16);
}
.row.assistant .att { background: rgba(255, 253, 247, 0.6); border-color: var(--line); }
.att .thumb {
  width: 148px; max-height: 84px; flex: none; object-fit: cover;
  border-radius: 3px; background: #16130f; display: block;
}
.att .aud { width: 210px; flex: none; height: 26px; }
.att .att-meta { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.att .att-meta b {
  font-family: var(--serif); font-size: 13px; font-weight: 600; letter-spacing: 0.5px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.att .att-meta span { font-size: 11.5px; opacity: 0.72; }

/* 素材库面板 */
.library {

}
.lib-head {
  display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
}
.lib-head b { font-family: var(--serif); font-size: 15px; letter-spacing: 2px; color: var(--ink); }
.lib-count { font-size: 12px; color: var(--ink-soft); letter-spacing: 1px; }
.lib-empty {
  padding: 24px 0; text-align: center; color: var(--ink-soft);
  font-family: var(--serif); font-size: 13px; letter-spacing: 2px;
}
.lib-divider {
  height: 1px; background: var(--line); margin: 8px 0 12px;
}
.lib-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px;
}
.lib-item {
  display: flex; flex-direction: column; gap: 6px; padding: 10px;
  border-radius: 4px; background: rgba(255, 253, 247, 0.9);
  border: 1px solid var(--line); box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.05);
}
.lib-thumb {
  width: 100%; max-height: 130px; object-fit: cover; border-radius: 3px;
  background: #16130f; display: block;
}
.lib-aud { width: 100%; height: 32px; }
.lib-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.lib-meta b {
  font-family: var(--serif); font-size: 12.5px; font-weight: 600; letter-spacing: 0.5px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink);
}
.lib-meta span { font-size: 11px; color: var(--ink-soft); }
.lib-add {
  cursor: pointer; padding: 6px 0; font-family: var(--serif); font-size: 12px;
  letter-spacing: 2px; color: var(--vermilion-deep);
  background: transparent; border: 1px dashed var(--vermilion); border-radius: 4px;
  transition: all 0.18s;
}
.lib-add:hover { color: #fff8ec; background: var(--vermilion); border-style: solid; }

/* 音乐库搜索框 */
.bgm-search {
  margin-left: auto; width: 180px; padding: 5px 10px;
  font-size: 12.5px; color: var(--ink);
  background: rgba(255, 253, 247, 0.9); border: 1px dashed var(--line); border-radius: 4px;
  outline: none;
}
.bgm-search:focus { border-color: var(--vermilion); border-style: solid; }
.bgm-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 2px; }
.bgm-tag {
  padding: 3px 10px; font-size: 12px; cursor: pointer;
  color: var(--ink); background: rgba(255, 253, 247, 0.6);
  border: 1px solid var(--line); border-radius: 12px; transition: all 0.15s;
}
.bgm-tag:hover { background: var(--vermilion); color: #fff; border-color: var(--vermilion); }

/* 素材库：选择 + 批量删除 */
.lib-add-btn {
  margin-left: auto; cursor: pointer; padding: 5px 14px;
  font-family: var(--serif); font-size: 12px; letter-spacing: 1px;
  color: #fff8ec; background: var(--vermilion);
  border: 0; border-radius: 4px; transition: all 0.18s;
}
.lib-add-btn:hover { background: var(--vermilion-deep); }
.lib-del-btn {
  cursor: pointer; padding: 5px 14px;
  font-family: var(--serif); font-size: 12px; letter-spacing: 1px;
  color: #fff8ec; background: var(--vermilion-deep);
  border: 0; border-radius: 4px; transition: all 0.18s;
}
.lib-del-btn:hover { background: var(--vermilion); }
.lib-del-btn:disabled { opacity: 0.5; cursor: wait; }
.lib-item.sel { border-color: var(--vermilion); box-shadow: 0 0 0 2px rgba(200, 64, 31, 0.18); }
.lib-check {
  position: absolute; top: 6px; left: 6px; z-index: 2; cursor: pointer;
  width: 20px; height: 20px; display: block;
}
.lib-check input { position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
.lib-check span {
  display: block; width: 20px; height: 20px; border-radius: 4px;
  border: 2px solid rgba(255, 255, 255, 0.85); background: rgba(38, 34, 28, 0.45);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3); transition: all 0.15s;
}
.lib-check input:checked + span {
  background: var(--vermilion); border-color: #fff8ec;
}
.lib-check input:checked + span::after {
  content: "✓"; display: grid; place-items: center; color: #fff8ec;
  font-size: 13px; font-weight: 700; height: 100%;
}
.lib-item { position: relative; }

/* 剪辑进度卡：吸顶常驻——滚到哪都能看到进行中/失败，点击展开完整步骤流 */
.pipe-card {
  position: sticky; top: 6px; z-index: 6;
  max-width: 860px; margin: 0 auto 12px; padding: 9px 16px;
  border: 1px solid var(--line); border-left: 3px solid var(--gold); border-radius: 8px;
  background: rgba(255, 252, 244, 0.96); box-shadow: 0 4px 16px rgba(38, 34, 28, 0.12);
  cursor: pointer; backdrop-filter: blur(4px);
}
.pipe-card.blocked { border-left-color: var(--vermilion-deep); }
.pipe-head { display: flex; align-items: center; gap: 10px; }
.pipe-head b { font-family: var(--serif); font-size: 13.5px; letter-spacing: 1px; color: var(--ink); }
.pipe-card.blocked .pipe-head b { color: var(--vermilion-deep); }
.pipe-now {
  font-size: 12px; color: var(--vermilion-deep); display: inline-flex;
  align-items: center; gap: 4px; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pipe-toggle {
  margin-left: auto; flex: none; font-size: 11px; color: var(--ink-soft); opacity: 0.7;
}
.pipe-flow {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px 4px;
  font-size: 12.5px; margin-top: 10px;
}
.pipe-arrow { color: var(--ink-soft); opacity: 0.5; padding: 0 2px; }
.pipe-step {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 9px; border-radius: 999px;
  background: rgba(38, 34, 28, 0.04); color: var(--ink-soft);
  border: 1px solid transparent;
}
.pipe-step i { font-style: normal; font-size: 11px; }
.pipe-step.done { color: var(--ink); }
.pipe-step.done i { color: var(--vermilion); }
.pipe-step.done em { font-style: normal; font-size: 10.5px; opacity: 0.55; }
.pipe-step.running {
  color: var(--ink); border-color: var(--vermilion);
  background: rgba(200, 64, 31, 0.06);
}
.pipe-spin { display: inline-block; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.pipe-step.error {
  color: var(--vermilion-deep); border-color: var(--vermilion-deep);
  background: rgba(122, 31, 14, 0.07); font-weight: 600;
}
.pipe-step em { font-style: normal; font-size: 10.5px; }

.timeline-panel { padding: 12px 16px; overflow-y: auto; }
.tl-toolbar, .tl-edit-toolbar {
  display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap;
}
.tl-item {
  padding: 10px 12px; border-radius: 8px; background: rgba(38,34,28,0.04);
  margin-bottom: 8px; cursor: pointer; position: relative;
  transition: background 0.15s;
}
.tl-item:hover { background: rgba(38,34,28,0.08); }
.tl-item b { display: block; font-size: 13px; }
.tl-item .td-name { font-size: 11px; color: var(--ink-soft); margin-right: 8px; }
.tl-item .td-desc { font-size: 11px; color: var(--ink-soft); opacity: 0.6; }
.tl-del {
  position: absolute; right: 8px; top: 8px;
  font-size: 11px; color: var(--vermilion); background: none; border: none;
  cursor: pointer; opacity: 0.6;
}
.tl-del:hover { opacity: 1; }
.tl-name-input {
  width: 100%; padding: 6px 10px; margin-bottom: 12px;
  border: 1px solid rgba(38,34,28,0.15); border-radius: 6px;
  font-size: 13px; background: var(--paper); color: var(--ink);
}
.tl-video { margin-bottom: 16px; }
.tl-video video { width: 100%; border-radius: 8px; background: #000; }
.tl-video .td-name { font-size: 11px; color: var(--ink-soft); display: block; margin-top: 4px; }
.tl-section { margin-bottom: 16px; }
.tl-section-head {
  font-size: 12px; font-weight: 600; color: var(--ink-soft);
  margin-bottom: 8px; padding-bottom: 4px;
  border-bottom: 1px solid rgba(38,34,28,0.08);
}
.tl-sub-row {
  display: flex; gap: 6px; margin-bottom: 6px; align-items: center;
}
.tl-sub-text {
  flex: 1; padding: 4px 8px; font-size: 12px;
  border: 1px solid rgba(38,34,28,0.12); border-radius: 4px;
  background: var(--paper); color: var(--ink);
}
.tl-sub-time {
  width: 60px; padding: 4px 4px; font-size: 11px; text-align: center;
  border: 1px solid rgba(38,34,28,0.12); border-radius: 4px;
  background: var(--paper); color: var(--ink);
}
.tl-bgm-row {
  display: flex; align-items: center; gap: 10px; font-size: 12px;
}
.tl-vol-slider { flex: 1; }
.tl-event-row {
  display: flex; gap: 8px; align-items: center; font-size: 11px;
  padding: 4px 0; color: var(--ink-soft);
}
.tl-event-row .td-name { font-weight: 600; color: var(--ink); }
.tl-event-row .td-desc { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 执行记录抽屉：一次执行一张卡，展开后是它的一致点链与分叉草稿 */
.drawer .runs { padding: 12px 16px 16px; overflow-y: auto; flex: 1; }
.ru-item {
  border: 1px solid var(--line); border-radius: 4px;
  background: rgba(255, 253, 247, 0.9);
  box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.05);
  margin-bottom: 10px; padding: 9px 11px;
}
.ru-item.sel { border-color: var(--vermilion); box-shadow: 0 0 0 2px rgba(200, 64, 31, 0.18); }
.ru-row { display: flex; align-items: baseline; gap: 8px; cursor: pointer; }
.ru-msg {
  flex: 1; min-width: 0; font-family: var(--serif); font-size: 13.5px; color: var(--ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ru-status {
  flex: none; font-size: 11px; letter-spacing: 1px; padding: 1px 6px; border-radius: 3px;
  border: 1px solid var(--line); color: var(--ink-soft);
}
.ru-status.running { border-color: var(--gold); color: #7a5a12; }
.ru-status.completed { border-color: rgba(60, 120, 60, 0.5); color: #2f6b34; }
.ru-status.failed { border-color: var(--vermilion-deep); color: var(--vermilion-deep); }
.ru-status.superseded { opacity: 0.55; text-decoration: line-through; }
.ru-meta {
  display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px;
  font-family: ui-monospace, monospace; font-size: 11px; color: var(--ink-soft); opacity: 0.75;
}
.ru-body { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.ru-point {
  display: flex; align-items: center; gap: 10px; padding: 5px 8px; margin-bottom: 4px;
  font-size: 12px; color: var(--ink-soft);
  border-left: 2px solid var(--line); cursor: pointer;
}
.ru-point:hover { background: rgba(200, 64, 31, 0.05); }
.ru-point.on {
  border-left-color: var(--vermilion); color: var(--ink);
  background: rgba(200, 64, 31, 0.07);
}
.ru-point-tools { flex: 1 1 100%; color: var(--ink-soft); opacity: .78; }
.ru-fork { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--line); }
.ru-nodes { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.ru-node {
  font-size: 11.5px; padding: 3px 8px; cursor: pointer;
  border: 1px dashed var(--line); border-radius: 10px;
  background: transparent; color: var(--ink-soft); font-family: inherit;
}
.ru-node:hover { border-style: solid; border-color: var(--vermilion); }
.ru-node.on {
  border-style: solid; border-color: var(--vermilion); color: var(--vermilion-deep);
  background: rgba(200, 64, 31, 0.09);
}
.ru-hint { font-size: 11.5px; line-height: 1.7; color: var(--ink-soft); opacity: 0.85; margin: 8px 0; }
.ru-actions { display: flex; gap: 8px; margin-top: 8px; }

/* ---- 计划卡（块 B-4）---- */
/* 消息流里的轻量状态行：完整内容在弹窗里 */
.plan-inline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 9px 13px; border: 1px solid var(--line); border-left: 3px solid var(--gold);
  border-radius: 8px; background: rgba(255, 252, 244, 0.9);
}
.plan-inline b { font-family: var(--serif); font-size: 13px; letter-spacing: 0.5px; }
.plan-inline.dismissed, .plan-inline.confirmed, .plan-inline.revised { opacity: 0.78; }
.pi-ico { font-size: 15px; }
.pi-open { margin-left: auto; }
.pi-tag { margin-left: auto; font-size: 11.5px; color: var(--ink-soft); padding: 1px 7px; border: 1px solid var(--line); border-radius: 10px; }
.pi-tag.ok { border-color: var(--gold); color: var(--gold); }

/* 计划确认窄横条（输入框上方） */
.plan-banner {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 14px; margin: 0 0 6px;
  background: var(--paper); border: 1px solid var(--gold); border-radius: 8px;
  box-shadow: 0 2px 8px rgba(200, 64, 31, 0.06);
}
.pb-icon { font-size: 16px; }
.pb-label { font-family: var(--serif); font-weight: 600; font-size: 14px; }
.pb-hint { font-size: 12px; color: var(--ink-light); flex: 1; min-width: 120px; }
.pb-btn {
  padding: 4px 12px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--paper); font-size: 13px; cursor: pointer; white-space: nowrap;
}
.pb-btn:hover { border-color: var(--vermilion); }
.pb-btn.primary { background: var(--vermilion); color: #fff; border-color: var(--vermilion); }
.pb-btn.primary:disabled { opacity: 0.6; cursor: default; }
.pb-feedback { flex-basis: 100%; padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; font-size: 13px; }

/* 右侧侧边栏计划链路 */
.plan-panel { padding: 12px 16px; overflow-y: auto; }
.pp-versions { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
.pp-ver {
  padding: 4px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--paper); font-size: 12px; cursor: pointer;
}
.pp-ver.on { border-color: var(--vermilion); color: var(--vermilion); font-weight: 600; }
.pp-chain { display: flex; flex-direction: column; gap: 0; }
.pp-step {
  padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--paper); position: relative;
}
.pp-step:not(:last-child)::after {
  content: '↓'; position: absolute; left: 50%; bottom: -14px;
  transform: translateX(-50%); color: var(--ink-light); font-size: 14px;
}
.pp-step.skipped { opacity: 0.45; text-decoration: line-through; }
.pp-step-head { display: flex; align-items: center; gap: 8px; }
.pp-num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 50%;
  background: var(--vermilion); color: #fff; font-size: 12px; font-weight: 700;
}
.pp-step-head b { font-family: var(--serif); font-size: 14px; }
.pp-badge {
  font-size: 10px; padding: 1px 6px; border-radius: 4px;
  background: var(--line); color: var(--ink-light); text-transform: uppercase;
}
.pp-why { font-size: 12px; color: var(--ink); margin-top: 4px; }
.pp-exp { font-size: 12px; color: var(--ink-light); margin-top: 2px; }
.pp-skills { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.pp-skill { font-size: 11px; color: var(--gold); }
.pp-skip { font-size: 11px; color: var(--ink-light); margin-top: 4px; display: inline-flex; gap: 4px; }

/* 计划确认弹窗：遮罩 + 居中卡片 */
.plan-modal-mask {
  position: fixed; inset: 0; z-index: 30;
  background: rgba(38, 34, 28, 0.45);
  display: flex; align-items: center; justify-content: center;
  padding: 24px; backdrop-filter: blur(2px);
}
.plan-modal {
  display: flex; flex-direction: column; gap: 12px;
  width: min(680px, 100%); max-height: 86vh; overflow-y: auto;
  padding: 18px 20px; border: 1px solid var(--line); border-left: 4px solid var(--gold);
  border-radius: 12px; background: var(--paper);
  box-shadow: 0 18px 48px rgba(38, 34, 28, 0.32);
}
.pm-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.pm-head b { font-family: var(--serif); font-size: 15px; letter-spacing: 1px; }
.pm-x {
  margin-left: auto; font-size: 20px; line-height: 1; color: var(--ink-soft);
  background: none; border: none; cursor: pointer; padding: 0 4px;
}
.pm-x:hover { color: var(--vermilion); }
.pm-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; position: sticky; bottom: 0; background: var(--paper); padding-top: 6px; }

/* 审批弹窗复用 plan-modal 外壳，以下只补审批专属元素 */
.ap-reason { margin: 0; color: var(--ink-soft); font-size: 13px; }
.ap-calls { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.ap-calls li { padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); }
.ap-calls b { font-family: var(--mono); font-size: 13px; color: var(--vermilion); }
.ap-args { display: block; margin-top: 4px; font-family: var(--mono); font-size: 11.5px; color: var(--ink-soft); white-space: pre-wrap; word-break: break-all; }
.ap-done { padding: 8px 0; color: var(--gold); font-size: 13px; }

.plan-card {
  display: flex; flex-direction: column; gap: 10px; max-width: 620px;
  padding: 12px 14px; border: 1px solid var(--line); border-left: 3px solid var(--gold);
  border-radius: 8px; background: rgba(255, 252, 244, 0.9);
}
.plan-card.confirmed, .plan-card.revised, .plan-card.dismissed { opacity: 0.78; }
.pc-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.pc-head b { font-family: var(--serif); font-size: 14px; letter-spacing: 1px; }
.pc-run, .pc-tag, .pc-line { font-size: 11.5px; color: var(--ink-soft); }
.pc-tag { padding: 1px 7px; border: 1px solid var(--line); border-radius: 10px; }
.pc-tag.ok { border-color: var(--gold); color: var(--gold); }
.pc-warn {
  font-size: 11.5px; line-height: 1.7; color: var(--vermilion-deep);
  border-left: 2px solid rgba(200, 64, 31, 0.4); padding-left: 8px;
}
.pc-versions { display: flex; flex-direction: column; gap: 6px; }
.pc-ver {
  display: flex; gap: 8px; align-items: flex-start; cursor: pointer;
  padding: 7px 9px; border: 1px solid var(--line); border-radius: 6px; background: #fffdf7;
}
.pc-ver.on { border-color: var(--gold); box-shadow: inset 2px 0 0 var(--gold); }
.pc-ver input { margin-top: 3px; accent-color: var(--vermilion); }
.pc-vt { display: flex; flex-direction: column; gap: 2px; }
.pc-vt b { font-family: var(--serif); font-size: 13px; letter-spacing: 0.5px; }
.pc-vt em { font-style: normal; font-size: 11.5px; color: var(--ink-soft); }
.pc-steps { list-style: none; display: flex; flex-direction: column; gap: 8px; }
.pc-steps li { padding-left: 9px; border-left: 2px solid var(--line); }
.pc-steps li.skipped { opacity: 0.5; text-decoration: line-through; }
.pc-step { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pc-step b { font-family: var(--serif); font-size: 13px; letter-spacing: 0.5px; }
.pc-skip { font-size: 11.5px; color: var(--ink-soft); display: inline-flex; gap: 4px; align-items: center; }
.pc-skip input { accent-color: var(--vermilion); }
.pc-noskip { font-size: 11px; color: var(--ink-soft); opacity: 0.6; }
.pc-why, .pc-exp { font-size: 12px; line-height: 1.7; color: var(--ink-soft); }
.pc-skills { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
.pc-skill {
  font-size: 11px; color: var(--gold); border: 1px dashed var(--gold);
  border-radius: 9px; padding: 0 7px;
}
.pc-param { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 5px; }
.pc-pk { font-size: 11.5px; color: var(--ink); letter-spacing: 1px; }
.pc-opt { font-size: 12px; color: var(--ink-soft); display: inline-flex; gap: 3px; align-items: center; }
.pc-opt input { accent-color: var(--vermilion); }
.pc-other {
  flex: 1 1 150px; min-width: 120px; font-size: 12px; padding: 3px 7px;
  border: 1px solid var(--line); border-radius: 5px; background: #fffdf7; color: var(--ink);
}
.pc-general { display: flex; flex-direction: column; gap: 4px; }
.pc-general textarea {
  font-family: var(--sans); font-size: 12px; line-height: 1.7; padding: 6px 8px; resize: vertical;
  border: 1px solid var(--line); border-radius: 6px; background: #fffdf7; color: var(--ink);
}
.pc-count { font-size: 11px; color: var(--ink-soft); opacity: 0.75; }
.pc-actions, .pc-feedback { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.pc-feedback input {
  flex: 1 1 200px; font-size: 12px; padding: 5px 8px;
  border: 1px solid var(--line); border-radius: 6px; background: #fffdf7; color: var(--ink);
}
.fb-options { flex-direction: column; align-items: stretch; gap: 6px; }
.fb-opt {
  display: flex; flex-direction: column; gap: 2px; text-align: left;
  padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px;
  background: #fffdf7; color: var(--ink); cursor: pointer; transition: border-color .15s, background .15s;
}
.fb-opt:hover:not(:disabled) { border-color: var(--vermilion); background: #fff8ee; }
.fb-opt:disabled { opacity: 0.5; cursor: default; }
.fb-opt-label { font-weight: 600; font-size: 13px; }
.fb-opt-desc { font-size: 11px; color: var(--ink-soft); }
.pc-error { font-size: 12px; color: var(--vermilion-deep); }

/* ---- 统一提问卡（截图那种形态）----
   编号单选行 + 推荐徽标 + 截断描述 + 铅笔自定义项 + 分页 + 下一题。
   四种来源共用：模型提问 / 计划确认 / 渲染前确认 / 渲染兜底。 */
.qc { max-width: 640px; width: min(640px, 92vw); }
.qc.full { max-width: min(1080px, 94vw); }
.qc-head { align-items: center; gap: 8px; }
.qc-step { font-family: var(--serif); font-size: 13.5px; letter-spacing: 1px; }
.qc-pager { display: flex; align-items: center; gap: 2px; margin-left: auto; }
.qc-pg {
  min-width: 24px; height: 24px; padding: 0 5px; cursor: pointer;
  border: 1px solid var(--line); border-radius: 5px; background: #fffdf7;
  color: var(--ink); font-size: 13px; line-height: 1;
}
.qc-pg:hover:not(:disabled) { border-color: var(--vermilion); color: var(--vermilion); }
.qc-pg:disabled { opacity: 0.35; cursor: default; }
.qc-pgn { font-size: 11.5px; color: var(--ink-soft); padding: 0 3px; }
.qc-title {
  margin: 2px 0 0; font-family: var(--serif); font-size: 15px;
  letter-spacing: 0.5px; line-height: 1.6; color: var(--ink);
}
.qc-preview { display: flex; flex-wrap: wrap; gap: 6px; }
.qc-pv {
  font-size: 11.5px; color: var(--ink); padding: 2px 8px;
  border: 1px solid var(--line); border-radius: 10px; background: #fffdf7;
}
.qc-pv em { font-style: normal; color: var(--ink-soft); margin-right: 4px; }
.qc-options { list-style: none; display: flex; flex-direction: column; gap: 6px; }
.qc-opt {
  display: flex; gap: 10px; align-items: flex-start; cursor: pointer;
  padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px;
  background: #fffdf7; transition: border-color .12s, background .12s;
}
.qc-opt:hover { border-color: var(--vermilion); background: #fff8ee; }
.qc-opt.on { border-color: var(--gold); background: #fffaf0; box-shadow: inset 2px 0 0 var(--gold); }
.qc-num {
  flex: 0 0 22px; height: 22px; border-radius: 50%; text-align: center;
  font-size: 11.5px; line-height: 22px; color: var(--ink-soft);
  border: 1px solid var(--line); background: #fff;
}
.qc-opt.on .qc-num { border-color: var(--gold); color: var(--gold); }
.qc-pencil { font-size: 12px; }
.qc-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 auto; }
.qc-label { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.qc-badge {
  font-size: 10px; font-weight: 400; padding: 0 6px; border-radius: 9px;
  color: var(--vermilion-deep); border: 1px solid rgba(200, 64, 31, 0.35);
  background: rgba(200, 64, 31, 0.06);
}
.qc-badge.soft { color: var(--gold); border-color: var(--gold); background: transparent; }
/* 描述单行截断：选项行高度一致，扫一眼就能比较（要看全文悬停即可） */
.qc-desc {
  font-size: 11.5px; color: var(--ink-soft); line-height: 1.5;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.qc-custom-row .qc-input { margin-top: 3px; }
.qc-input {
  font-family: var(--sans); font-size: 12px; padding: 4px 8px; width: 100%;
  border: 1px solid var(--line); border-radius: 5px; background: #fffdf7; color: var(--ink);
}
.qc-details { font-size: 12px; color: var(--ink-soft); }
.qc-details summary { cursor: pointer; }
.qc-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.qc-hint { font-size: 11.5px; color: var(--ink-soft); opacity: 0.8; }

/* ---- 侧边栏：编排预览 ----
   渲染前的编排结果。弹窗只问要拍板的题，细节都在这里。 */
.pv-sec { display: flex; flex-direction: column; gap: 8px; }
.pv-head { display: flex; align-items: center; justify-content: space-between; }
.pv-head b { font-family: var(--serif); font-size: 13.5px; letter-spacing: 1px; }
.pv-reload {
  font-size: 11px; padding: 1px 8px; cursor: pointer;
  border: 1px solid var(--line); border-radius: 10px;
  background: #fffdf7; color: var(--ink-soft);
}
.pv-reload:hover:not(:disabled) { border-color: var(--vermilion); color: var(--vermilion); }
.pv-reload:disabled { opacity: 0.5; cursor: default; }
.pv-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.pv-chip {
  font-size: 11px; color: var(--ink); padding: 1px 7px;
  border: 1px solid var(--line); border-radius: 10px; background: #fffdf7;
}
.pv-chip em { font-style: normal; color: var(--ink-soft); margin-right: 4px; }
.pv-warn {
  font-size: 11.5px; line-height: 1.7; color: var(--vermilion-deep);
  border-left: 2px solid rgba(200, 64, 31, 0.4); padding-left: 8px;
}
/* 轨道图：一眼看出画面/人声/字幕/配乐各占多少 */
.pv-tracks { display: flex; flex-direction: column; gap: 5px; }
.pv-trk { display: flex; align-items: center; gap: 7px; }
.pv-tl { flex: 0 0 30px; font-size: 11px; color: var(--ink-soft); }
.pv-bar {
  flex: 1 1 auto; height: 9px; border-radius: 5px; overflow: hidden;
  background: rgba(0, 0, 0, 0.06);
}
.pv-fill { display: block; height: 100%; border-radius: 5px; }
.pv-fill.v { background: var(--ink-soft); }
.pv-fill.a { background: var(--vermilion); }
.pv-fill.s { background: var(--gold); }
.pv-fill.b { background: #8a7fbf; }
.pv-fill.o { background: #4aa564; }
.pv-tv { flex: 0 0 auto; font-size: 10.5px; color: var(--ink-soft); }
.pv-sub {
  font-family: var(--serif); font-size: 12px; letter-spacing: 1px;
  color: var(--ink-soft); margin-top: 3px;
}
.pv-group {
  display: flex; flex-direction: column; gap: 3px; padding: 6px 8px;
  border: 1px solid var(--line); border-left: 2px solid var(--gold); border-radius: 6px;
  background: #fffdf7;
}
.pv-gh { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; }
.pv-gh b { font-family: var(--mono); font-size: 11.5px; }
.pv-gs { font-size: 12px; color: var(--ink); }
.pv-gd { font-size: 10.5px; color: var(--ink-soft); margin-left: auto; }
.pv-gc, .pv-gsp { display: flex; flex-direction: column; gap: 1px; }
.pv-cap, .pv-sp { font-size: 11px; line-height: 1.6; color: var(--ink-soft); }
.pv-sp { color: var(--ink); }
.pv-details { font-size: 11.5px; color: var(--ink-soft); }
.pv-details summary { cursor: pointer; }
.pv-shot, .pv-mat {
  display: flex; gap: 6px; align-items: baseline; font-size: 11px;
  padding: 2px 0; border-bottom: 1px dashed rgba(0, 0, 0, 0.06);
}
.pv-shot.drop { opacity: 0.45; }
.pv-sid { font-family: var(--mono); font-size: 10.5px; flex: 0 0 auto; }
.pv-st { color: var(--ink-soft); flex: 0 0 auto; }
.pv-sc {
  flex: 1 1 auto; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; color: var(--ink-soft);
}
.pv-drop { flex: 0 0 auto; color: var(--vermilion-deep); }
.pv-sep { height: 1px; background: var(--line); margin: 4px 0; }

/* ---- 对账角标（块 B-3）---- */
.audit-card {
  display: flex; flex-direction: column; gap: 5px; max-width: 620px;
  padding: 9px 12px; border: 1px dashed var(--vermilion); border-radius: 8px;
  background: rgba(200, 64, 31, 0.05);
}
.ac-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.ac-head b { font-family: var(--serif); font-size: 12.5px; letter-spacing: 1px; color: var(--vermilion-deep); }
.ac-badge { font-size: 11px; padding: 0 7px; border-radius: 9px; border: 1px solid var(--vermilion); color: var(--vermilion-deep); }
.ac-plan, .ac-note { font-size: 11px; color: var(--ink-soft); opacity: 0.8; }
.ac-row { font-size: 12px; display: flex; gap: 8px; }
.ac-row em { font-style: normal; color: var(--ink-soft); flex: none; }
.ac-reason { font-size: 12.5px; line-height: 1.75; color: var(--ink); }
.pipe-audit {
  font-size: 11.5px; color: var(--vermilion-deep); padding: 0 7px;
  border: 1px solid rgba(200, 64, 31, 0.45); border-radius: 9px;
}
.ru-plan, .ru-line { font-size: 11px; color: var(--ink-soft); }
.ru-audit { font-size: 11px; color: var(--vermilion-deep); }
.ru-audit-body {
  margin-bottom: 8px; padding: 7px 9px; border: 1px dashed var(--vermilion);
  border-radius: 6px; background: rgba(200, 64, 31, 0.05);
}
.ru-reason { font-size: 12px; line-height: 1.7; color: var(--ink); margin-top: 4px; }

/* ---- 聊天窗渲染进度条（块 C 后半）---- */
.render-bar {
  max-width: 420px; padding: 9px 12px; border: 1px solid var(--line);
  border-left: 3px solid var(--vermilion); border-radius: 8px; background: #fffdf7;
}
.render-bar.done { border-left-color: var(--gold); }
.render-bar.error { border-left-color: var(--vermilion-deep); }
.rb-head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
.rb-head b { font-family: var(--serif); font-size: 12.5px; letter-spacing: 1px; }
.rb-pct { font-size: 12px; color: var(--vermilion-deep); font-variant-numeric: tabular-nums; }
.rb-track { height: 6px; margin: 7px 0 4px; border-radius: 999px; background: var(--paper-deep); overflow: hidden; }
.rb-track i { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--gold), var(--vermilion)); transition: width .6s ease; }
.render-bar.done .rb-track i { background: var(--gold); }
.rb-art, .rb-error { font-size: 11px; color: var(--ink-soft); }
.rb-error { color: var(--vermilion-deep); }

@media (max-width: 760px) {
  .rail { width: 72px; padding: 16px 8px; }
  .brand-text, .conv .t, .conv .x, .who .v, .new .lbl { display: none; }
  .new { padding: 10px 0; font-size: 18px; }
  .paper, .head, .composer, .attach-bar, .link-bar { padding-left: 18px; padding-right: 18px; }
  .drawer { width: 100%; }
}

/* —— 工具库管理区（MCP 服务 / 技能库）—— */
.mcp-head { display: flex; align-items: center; justify-content: space-between; }
.td-row-btns { display: inline-flex; gap: 6px; align-items: center; }
.st-ok { color: #1a7f37; }
.st-bad { color: #8b949e; }
.help.danger { color: #cf222e; }
.mcp-form {
  display: flex; flex-direction: column; gap: 6px;
  border: 1px solid #d0d7de; border-radius: 8px; padding: 8px; margin: 6px 0;
}
.mcp-form input, .mcp-form select {
  padding: 5px 8px; border: 1px solid #d0d7de; border-radius: 6px; font-size: 13px;
}
</style>
