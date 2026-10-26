<script setup>
// 智能创作助手 · 前端主界面
// 数据流严格对应后端文档链路：
//   发送 → POST /chat（MQ InBound）；结果 → WS /ws/{conv}?token= 收
//   delta / stream_end / answer（MQ OutBound → Connection Manager 回投）。
// 身份（spec §7）：首次访问 POST /register 换一份 token，之后 HTTP 带 Bearer、
//   WS 带 ?token=；请求里不再出现 user_id——它由服务端从 token 反查。
import { computed, reactive, ref, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import MotionEditor from "./MotionEditor.vue";

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
// —— 登录门禁：未登录（或凭证失效）只显示登录/注册页，登录成功才进主界面 ——
const loginView = ref(!token.value);    // 有 token 先不显示登录页（避免刷新闪现），whoami 401 再翻回 true
const loginMode = ref("login");            // login | register
const loginForm = reactive({ username: "", password: "" });
const loginErr = ref("");
const loginBusy = ref(false);
const username = ref("");                 // 账号名：登录/whoami 时由服务端回，左下角展示

function ensureIdentity() {
  if (userId.value) return Promise.resolve(userId.value);
  if (!identityPromise) {
    identityPromise = (token.value ? whoami() : Promise.reject(new Error("未登录"))).catch((e) => {
      identityPromise = null;             // 失败不留半成品 promise，下次操作会重试
      loginView.value = true;             // 凭证缺失/失效 → 回到登录页
      throw e;
    });
  }
  return identityPromise;
}

async function submitAuth() {
  if (loginBusy.value) return;
  loginBusy.value = true; loginErr.value = "";
  try {
    const path = loginMode.value === "login" ? "/auth/login" : "/auth/register";
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: loginForm.username.trim(),
                             password: loginForm.password }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    token.value = j.token;                 // JWT
    userId.value = j.user_id;
    username.value = j.username || "";
    hadToken.value = true;
    localStorage.setItem(TOKEN_KEY, j.token);
    loginView.value = false;
    identityPromise = null;
    await ensureIdentity();
    loadConvs();
    ensureCatalog();
  } catch (e) {
    loginErr.value = e.message;
  } finally {
    loginBusy.value = false;
  }
}

function switchLoginMode() {
  loginMode.value = loginMode.value === "login" ? "register" : "login";
  loginErr.value = "";
}

async function whoami() {
  const r = await fetch("/whoami", { headers: authHeaders() });
  if (r.status === 401) {                 // 服务端认不得这张凭证：清掉,回登录页
    forgetToken();
    return null;
  }
  if (!r.ok) throw new Error("无法确认身份（HTTP " + r.status + "）");
  const j = await r.json();
  userId.value = j.user_id;
  username.value = j.username || "";
  loginView.value = false;               // 凭证有效 → 进主界面（刷新后恢复登录态）
  return userId.value;
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
    let hasMedia = false;
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
          artifactId: card.artifact_id, hitmap: !!card.hitmap,
        });
        hasMedia = true;
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
    if (hasMedia && cid === active.value) {
      activePanel.value = 'timeline';
    }
    scrollDown();
  } catch (_) {
    /* 回填失败不打断使用：新会话本来就还没有历史 */
  }
}

async function openConv(cid) {
  // 让位排队只在会话内有效：切走就清（挂起态在服务端，`checkActiveRun` 进会话时补弹）
  parkedCards.value = [];
  questionCard.value = null;
  active.value = cid;
  await loadHistory(cid);
  await checkActiveRun(cid);
  openRun.value = null;
  preview.value = null;              // 换会话先清掉，避免显示上一个会话的编排
  if (activePanel.value === 'runs') await loadRuns();
  if (activePanel.value === 'plan') await loadPreview(cid);
  if (activePanel.value === 'textlib') await loadConvPrefs();
  ensureSocket(cid);
}

async function checkActiveRun(cid) {
  try {
    const r = await fetch(`/convs/${cid}/runs/active`, { headers: authHeaders() });
    if (!r.ok) return;
    const j = await r.json();
    // 没有「在途」也没有「等审批」的 run ≠ 这个会话没出过事：**死在半路**也是没有
    // 在途 run，而服务端 /runs/active 对 failed 一个字都不回。见下面的兜底。
    if (!j.run) { await offerResumeIfNewestFailed(cid); return; }
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
      if (cid !== active.value) return;
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

// 冷启动兜底之二：上一个任务**死在半路**时，界面上得有一个能点的东西。
//
// 服务端 /runs/active 只认「在途」和「等审批」两种，一条 failed 的 run 它一个字都不回，
// 前端原先就跟着 `if (!j.run) return` 走了。于是真机上出现过这样一幕
// （2026-10-16，run 9272c7f59501 那 20 镜）：编辑器容器被重建、MCP 会话失效，
// 第 89 轮整条链停死；用户刷新前端——**界面看起来只是"停了"**，既没有报错也没有按钮，
// 只能自己翻执行记录才找得到那条「从最新一致点续跑」。
//
// 只认**最新那条**是 failed：会话里后面若又跑过（completed / 等审批 / 在途），
// 说明用户已经往下走了，不再翻旧账。
async function offerResumeIfNewestFailed(cid) {
  if (cid !== active.value) return;
  try {
    await ensureIdentity();
    const r = await fetch(`/convs/${encodeURIComponent(cid)}/runs`, { headers: authHeaders() });
    if (!r.ok) return;
    const j = await r.json();
    const run = (j.runs || [])[0];               // 服务端按「最近的在前」排
    if (!run || run.status !== "failed") return;
    // **去重靠扫列表，不靠一张旁挂的"提过没有"表**：这条路径的每一个调用方
    // （openConv / reloadAfterReconnect）都在它之前刚跑过 loadHistory，
    // 而 loadHistory 是整体重建 `histories[cid]`。用旁挂表就会出现
    // 「历史一重建、气泡没了，可表里记着提过 → 再也不补」。气泡自己在列表里，
    // 就以列表为准。
    if (msgs(cid).some((m) => m.resume && m.resume.run_id === run.run_id)) return;
    msgs(cid).push({
      role: "assistant", state: "error", resume: run,
      text: `⚠️ 上一个任务在第 ${run.iteration} 轮**失败**了，整条链停在半路。`
          + `已经跑出来的产物都还在工作区里，可以从最新一致点接着跑——不会从头重来。`,
    });
    scrollDown();
  } catch (_) { /* 拉不到就不提，不影响正常使用 */ }
}

// 气泡上那颗按钮。请求本身照旧走 `doResume`（与执行记录面板里那颗是同一段代码），
// 这里只多管两件事：**防连点**、以及**失败要看得见**。
async function resumeFromBubble(m) {
  const cid = active.value;
  const run = m.resume;
  m.resume = null;                 // 先收掉按钮
  await doResume(run);
  if (!runsError.value) return;    // 成功：doResume 已关抽屉并接上流式进度，收掉就对了
  // 失败时 `doResume` 把话说进 `runsError`——那是**执行记录抽屉**的位置，对话里根本看不见。
  // 所以把按钮还回去，并在对话里补一句；否则用户点了「续跑」像什么都没发生。
  m.resume = run;
  msgs(cid).push({ role: "assistant", state: "error", text: "续跑没能提交：" + runsError.value });
  scrollDown();
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
// 输入框跟着内容长高：换行的长句不用挤在一条横线里，到 240px 封顶后再内部滚动。
// 高度由 scrollHeight 反推（先归零再量），回车/删除/粘贴都会走同一条 watch。
const draftEl = ref(null);
function fitDraft() {
  const el = draftEl.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 240) + "px";
}
watch(draft, () => nextTick(fitDraft));
const scroller = ref(null);

const toolLib = ref(null);
const loadingTools = ref(false);
// —— 工具库管理态：MCP 服务（动态注册）与技能库的增删改查 ——
const mcpServers = ref([]);
// 设置页里那个 MCP 区块用的草稿与回话。刻意**不复用** mcpForm/mcpMsg：那两个是工具库
// 弹窗的东西（弹窗开着/收起是另一套状态），共用会让「我在设置页填了一半，切去工具库
// 弹窗，回来字没了」。写库那一支仍然共用（见 mcpUpsert）——状态各管各的，口径只有一份。
const mcpDraft = ref({ name: "", type: "streamableHttp", url: "", command: "", args: "" });
const mcpSetMsg = ref("");
const loadingMcp = ref(false);
const mcpForm = ref(null);        // null=表单收起;{name,type,url,command,args,tool_timeout}
const mcpBusy = ref(false);
const mcpMsg = ref("");
const skillsAdmin = ref([]);
const loadingSkillsAdmin = ref(false);
const skillsMsg = ref("");
// 技能面板的拖放态：拖到面板上高亮，松手即读取整个文件夹。
const skillsDragOver = ref(false);
// —— 条目弹窗：工具 / 技能 / MCP 服务 的详情与增删改 ——
const itemModal = ref(null);      // {kind:'tool'|'skill'|'mcp', ...}
const skillForm = ref(null);      // null=查看态;非空=编辑态
const skillBusy = ref(false);
// —— 布局:左栏收起/展开 + 右侧抽屉拖宽 ——
const railOpen = ref(localStorage.getItem("ca.railOpen") !== "0");
// 「一句话出片」的模式：`preview`（默认，先出分镜给你看）或 `auto`（跳过预览直接烧渲染）。
// 一次成片十几到几十分钟，跳过预览是"我知道我要什么"。存 localStorage，跟这个浏览器走。
const filmMode = ref(localStorage.getItem("ca.filmMode") || "preview");
const filmOpen = ref(false);
const fmEl = ref(null);
function pickFilm(v) {
  setFilmMode(v);
  filmOpen.value = false;
}
function setFilmMode(v) {
  filmMode.value = v === "auto" ? "auto" : "preview";
  localStorage.setItem("ca.filmMode", filmMode.value);
}
// 点别处收起下拉。`@click.stop` 在触发元素上，所以这里只管"外面"。
onMounted(() => {
  document.addEventListener("click", (e) => {
    if (fmEl.value && !fmEl.value.contains(e.target)) filmOpen.value = false;
  });
});
function toggleRail() {
  railOpen.value = !railOpen.value;
  localStorage.setItem("ca.railOpen", railOpen.value ? "1" : "0");
}
// 预览缩略图点开看大图（新标签页，不打断确认卡）
function openThumb(url) {
  if (url) window.open(url, "_blank", "noopener");
}
// —— 入口只有一个：全自动 ——
// 用户只写一句话，技能与节点由助手在规划轮自己找（`find_skill` 搜画面契约、
// 节点从白名单里挑）。2026-10-22 用户明确要求「前端只有自动入口」，所以入口按钮、
// 「直接出片」开关、风格下拉都撤了：之前那三样都是让人替模型做决定。
// 服务端仍保留 `mode` / `style_skill` 两个入参（对话里换风格、脚本调用要用），
// 只是这一端不再提供「选」的动作。
function styleLabel(name) {
  const hit = (skillsAdmin.value || []).find((s) => s && s.name === name);
  return (hit && (hit.display || hit.name)) || name;
}
// —— 风格模板库：从参考片沉淀出来的**可执行模板**（TSX） ——
// 与素材库分开列：素材是原料，模板是已经过闸、能直接复用的成品。系统模板随镜像
// 发布；用户模板只对本人可见，删它的时候连渲染容器里的那份一起删。
const styleTemplates = ref([]);
const loadingTemplates = ref(false);
const templateBusy = ref("");          // 正在沉淀的 material_id（按钮禁用 + 文案）
// {material_ids: [], filenames: [], slug, note}——可一次沉淀多条同风格参考片
const templateForm = ref(null);
const templateMsg = ref("");
const templateErr = ref("");

async function loadStyleTemplates() {
  // 2026-10-24：风格包通道已从项目里删除（后端路由、生成工具、种子全部移除），
  // 视觉风格一律由 skill 提供。这里不再发请求——留着会拿到 405 并在界面上挂一条
  // 「模板库加载失败」，而那个入口本来就已经不该存在了。
  styleTemplates.value = [];
  templateErr.value = "";
  loadingTemplates.value = false;
}

// 文件名 → 合法短名：只留小写字母数字与 `-`，截 20 位。服务端还会再校验一次。
function slugFromFilename(name) {
  const slug = String(name || "").replace(/\.[^.]+$/, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 20);
  return slug || "style";
}

function openTemplateDeposit(items) {
  const picked = (Array.isArray(items) ? items : [items])
    .filter((x) => x && x.kind === "video" && x.material_id);
  if (!picked.length) {
    templateErr.value = "只能拿视频沉淀模板（音频在「音乐库」，图片不能当参考片）";
    return;
  }
  templateForm.value = {
    material_ids: picked.map((x) => x.material_id),
    filenames: picked.map((x) => x.filename || x.material_id),
    // 多条时用第一条的文件名起短名，用户仍可改。
    slug: slugFromFilename(picked[0].filename),
    note: "",
  };
  templateMsg.value = "";
  templateErr.value = "";
}

// 素材库勾选里挑出视频，供工具栏「沉淀选中的 N 个视频」用。
function selectedLibraryVideos() {
  return (libraryItems.value || []).filter(
    (x) => x.kind === "video" && libSelected[x.material_id]);
}

function cancelTemplateDeposit() {
  if (templateBusy.value) return;   // 读片/过闸还在跑，关掉只会让人以为没开始
  templateForm.value = null;
}

async function depositTemplate() {
  const f = templateForm.value;
  if (!f || templateBusy.value) return;
  templateBusy.value = f.material_ids[0];
  templateMsg.value = f.material_ids.length > 1
    ? `正在读 ${f.material_ids.length} 条参考片、提炼共同语法、写模板、过闸——通常 2~5 分钟…`
    : "读片、写模板、过闸中——通常 1~3 分钟…";
  templateErr.value = "";
  try {
    await ensureIdentity();
    // 先提交任务、再轮询：沉淀是分钟级任务，长连接很容易被浏览器/代理提前掐断
    // （表现为 HTTP 499，而后端连请求都没看到）。提交只返回 job_id，连接很短。
    const r = await fetch("/style/templates/deposit/jobs", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ material_ids: f.material_ids, slug: f.slug.trim(),
                             name_hint: f.slug.trim(), note: f.note.trim() }),
    });
    const submitted = await r.json().catch(() => ({}));
    if (!r.ok || submitted.ok === false) {
      throw new Error(submitted.error || submitted.detail || ("HTTP " + r.status));
    }
    const jobId = submitted.job_id;
    let j = null;
    for (let i = 0; i < 600; i += 1) {          // 每 3 秒一次，最多等 30 分钟
      await new Promise((done) => setTimeout(done, 3000));
      const pr = await fetch(`/style/templates/deposit/jobs/${jobId}`,
                             { headers: authHeaders() });
      if (pr.status === 404) {
        throw new Error("任务不存在了（服务可能重启过），请重新点一次沉淀");
      }
      const pj = await pr.json().catch(() => ({}));
      if (!pr.ok) throw new Error(pj.detail || ("HTTP " + pr.status));
      const secs = Math.round(Number(pj.elapsed) || 0);
      templateMsg.value = `正在读片、写模板、过闸……已 ${Math.floor(secs / 60)} 分 `
        + `${secs % 60} 秒（通常 2~5 分钟）`;
      if (pj.status === "done") { j = pj.result || {}; break; }
      if (pj.status === "error") {
        const res = pj.result || {};
        // `detail` 可能是字符串（FastAPI 的 HTTPException），也可能是字符串数组；
        // 直接展开字符串会把它拆成一个个字，所以先判形状再拼。
        const details = Array.isArray(res.detail) ? res.detail : [];
        const head = res.error || pj.error
          || (typeof res.detail === "string" ? res.detail : "")
          || ("HTTP " + (pj.status_code || ""));
        throw new Error([head, ...details]
          .filter((x) => typeof x === "string" && x).join("；"));
      }
    }
    if (!j) {
      throw new Error("等待超时，任务可能仍在后台运行；稍后刷新模板库查看");
    }
    templateForm.value = null;
    const refs = Number(j.references || 0);
    templateMsg.value = `已沉淀模板《${j.display || j.name}》`
      + (refs > 1 ? `（${refs} 条参考片）` : "")
      + "，已进技能库——下一句话里提到它（或者说「用刚沉淀的那套」）就会自己用上";
    await Promise.all([loadStyleTemplates(), loadSkillsAdmin()]);
  } catch (e) {
    templateMsg.value = "";     // 失败时别把「正在读片」和错误一起挂着
    templateErr.value = "沉淀失败：" + e.message;
  } finally {
    templateBusy.value = "";
  }
}

async function deleteStyleTemplate(t) {
  if (!t || t.system) return;
  if (!confirm(`删除模板「${t.display || t.name}」？对应的风格包也会一起删除，且不可撤销。`)) return;
  try {
    const r = await fetch(`/style/templates/${encodeURIComponent(t.name)}`,
                          { method: "DELETE", headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    templateMsg.value = "已删除：" + (t.display || t.name);
    await Promise.all([loadStyleTemplates(), loadSkillsAdmin()]);
  } catch (e) {
    templateErr.value = "删除失败：" + e.message;
  }
}
function styleTemplateLabel(name) {
  const hit = (styleTemplates.value || []).find((t) => t && t.name === name);
  return (hit && (hit.display || hit.name)) || name;
}

// —— 换件：模板的每个导出组件都是一个可替换零件 ——
// 只换零件、不换外壳：替换件必须导出同一个组件名、收同一套 props。新零件单独过闸，
// 登记进基模板后，聊天里说「用XX风格，人物换成一鹿」时规划层就能把它绑进本片。
//
// 用户不该被要求懂 props。「换成别的 / 改画法 / 跟着每一镜变」三种改法各自带
// 例子，服务端再把一句话解读成按哪个参数分档；界面只负责把零件画出来、让用户点。
const variantForm = ref(null);         // {template, display, slots, preview, component, mode, follow, target, quick}
const variantBusy = ref(false);
const variantMsg = ref("");
const variantErr = ref("");
//: 换完的预览：{display, request, items: [{label, component, spec, preview}], failures}
//: 一句话可以同时改好几个零件，所以结果是一组，不是一个。
const variantResult = ref(null);
//: 按当前模板/零件生成的建议（服务端调用一次模型）。缓存按「模板::零件」，
//: 同一次打开面板里来回点零件不重复烧模型。
const variantOptions = ref([]);
const variantOptionAxes = ref([]);
const variantOptionsBusy = ref(false);
const variantOptionCache = new Map();

//: 零件类型 → 图标。可视化用，不参与任何提示词。沉淀出来的模板 kind 可能是
//: 中英文任意一种叫法，这里多收几种别名；认不出就退回中性图标，不挡事。
const VARIANT_PART_ICONS = {
  subject: "🚶", character: "🚶", person: "🚶", figure: "🚶",
  object: "🧩", prop: "🧩", item: "🧩",
  background: "🏞️", scene: "🏞️", sky: "🌅", landscape: "🏞️",
  light: "✨", glow: "✨", lamp: "💡", fire: "🔥",
  text: "🔤", caption: "🔤", subtitle: "🔤", title: "🔤", label: "🔤",
  inscription: "🔖", date: "📅",
  decor: "▦", particle: "✦", texture: "▦", overlay: "▦", grain: "▦",
  other: "◻️",
};
//: 参数名 → 人话。界面上让用户选「跟着什么变」，不要求他知道 `wardrobe` 是什么。
const VARIANT_AXIS_LABELS = {
  wardrobe: "服饰", carried: "持物", sceneType: "场景", facing: "朝向",
  walking: "行走", phase: "步态", strideSec: "步频",
  skyColors: "天色", farColor: "远景颜色", midColor: "中景颜色",
  groundColor: "地面颜色", windowLights: "窗光", horizon: "地平线",
  groundY: "地平线高度", accent: "强调色", accentColor: "光色",
  intensity: "亮度", radius: "光晕大小", text: "文案", position: "位置",
  x: "横向位置", y: "纵向位置", heightRatio: "身高比例",
};
//: 不限定某一种模板：这里只是「改法」的四种意图，具体换成什么、按什么变，
//: 由上面点中的零件和用户那句话决定。key 为空＝让服务端按原话判断。
const VARIANT_MODES = [
  { key: "", title: "让系统判断",
    hint: "按你写的那句话来，不预设" },
  { key: "replace", title: "换成别的东西",
    hint: "把这个对象本身换掉" },
  { key: "restyle", title: "改画法 / 轮廓",
    hint: "对象不变，改形状与细节" },
  { key: "by_shot", title: "跟着每一镜变",
    hint: "每一镜按镜头内容分别长" },
];

function variantPartIcon(kind) {
  return VARIANT_PART_ICONS[kind] || VARIANT_PART_ICONS.other;
}
function variantAxisLabel(prop) {
  const hit = (variantOptionAxes.value || []).find((a) => a.prop === prop);
  return (hit && hit.label) || VARIANT_AXIS_LABELS[prop] || prop;
}
// 零件现在能画什么：`values` 是能原样传给组件的取值；`reference` 只是参考片里
// 见过的中文叫法（「山峦」「军装」），不能当参数值传，所以分开显示。
function variantContentLabel(c) {
  const values = (c && c.values ? c.values : []).filter((x) => String(x || "").trim());
  const ref = (c && c.reference ? c.reference : []).filter((x) => String(x || "").trim());
  if (values.length) {
    return ref.length && ref.join("|") !== values.join("|")
      ? `${values.join(" / ")}（参考片：${ref.join(" / ")}）`
      : values.join(" / ");
  }
  return ref.length ? `参考片用过 ${ref.join(" / ")}` : "";
}
function variantContentText(slot) {
  return ((slot && slot.content) || [])
    .map((c) => {
      const text = variantContentLabel(c);
      return text ? `${variantAxisLabel(c.prop)}：${text}` : "";
    })
    .filter(Boolean)
    .join("；");
}
function variantAxes() {
  const f = variantForm.value;
  if (!f || !f.component) return [];
  const slot = (f.slots || []).find((s) => s.component === f.component);
  const out = [];
  const add = (p) => {
    const name = String(p || "");
    if (name && !out.includes(name)) out.push(name);
  };
  // 模板自己声明的轴优先；建议器从合约里另外挑出来的轴（比如背景的 sceneType）
  // 也放出来——否则「跟着每一镜变」只能改颜色，改不了形状。
  ((slot && slot.varies) || []).forEach(add);
  (variantOptionAxes.value || []).forEach((a) => add(a && a.prop));
  add(f.follow);
  return out;
}
//: 当前选中的零件（自动判断时为 null）。卡面要显示它的默认画法和可变的轴。
const variantSlot = computed(() => {
  const f = variantForm.value;
  if (!f || !f.component) return null;
  return (f.slots || []).find((s) => s.component === f.component) || null;
});
//: 建议是「按这个模板、这个零件」现生成的，不是写死的风格例子。
function applyVariantOptionsPayload(f, component, j) {
  variantOptions.value = (j && j.options) || [];
  variantOptionAxes.value = (j && j.axes) || [];
  // 自动判断：把模型挑中的那个零件也点亮，用户看得见建议是给谁的。
  if (!component && j && j.component && !f.component
      && (f.slots || []).some((s) => s.component === j.component)) {
    f.component = j.component;
  }
}
let variantOptionsSeq = 0;
async function loadVariantOptions(component) {
  const f = variantForm.value;
  if (!f) return;
  const key = `${f.template}::${component || ""}`;
  const seq = ++variantOptionsSeq;
  variantOptions.value = [];
  variantOptionAxes.value = [];
  if (variantOptionCache.has(key)) {
    applyVariantOptionsPayload(f, component, variantOptionCache.get(key));
    return;
  }
  variantOptionsBusy.value = true;
  try {
    await ensureIdentity();
    const r = await fetch(
      `/style/templates/${encodeURIComponent(f.template)}/variant-options`,
      {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ component: component || "" }),
      });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) return;
    variantOptionCache.set(key, j);
    if (seq !== variantOptionsSeq || variantForm.value !== f) return;
    applyVariantOptionsPayload(f, component, j);
  } catch (e) {
    // 建议失败不挡自己写：零件卡片、输入框与「开始换件」照常可用。
  } finally {
    if (seq === variantOptionsSeq) variantOptionsBusy.value = false;
  }
}

function applyVariantOption(o) {
  const f = variantForm.value;
  if (!f || !o) return;
  f.target = o.text || "";
  f.mode = o.mode || "";
  f.follow = o.follow || "";
  f.suggested = "";
}

//: 点零件时先给一句能直接改的话——用户不用从空白框开始想怎么描述。
function variantSuggestion(slot) {
  const f = variantForm.value;
  if (!f || !slot) return "";
  const name = slot.label || slot.component || "这个零件";
  if (f.mode === "by_shot") {
    return `每一镜按镜头内容改变${name}的轮廓与形态（例如按时代换服饰、持物）`;
  }
  if (f.mode === "restyle") {
    return `改${name}的画法与轮廓：`;
  }
  if (f.mode === "replace") return `把${name}换成`;
  return `改${name}：`;
}
function applyVariantSuggestion() {
  const f = variantForm.value;
  if (!f) return;
  const slot = (f.slots || []).find((s) => s.component === f.component) || null;
  const tip = variantSuggestion(slot);
  // 只在空白、或还是上一次自动填的那句时覆盖；用户自己写的话绝不吞掉。
  if (tip && (!f.target.trim() || f.target === f.suggested)) f.target = tip;
  f.suggested = tip;
}
function pickVariantPart(component) {
  const f = variantForm.value;
  if (!f) return;
  f.component = component || "";
  // 换零件后，变化轴跟着这个零件重选，避免拿着上一个零件的参数去分档。
  if (f.mode === "by_shot") f.follow = variantAxes()[0] || "";
  applyVariantSuggestion();
  loadVariantOptions(component || "");
}
function setVariantMode(mode) {
  const f = variantForm.value;
  if (!f) return;
  f.mode = mode;
  if (mode === "by_shot") f.follow = variantAxes()[0] || "";
  else f.follow = "";
  applyVariantSuggestion();
}
function variantPlaceholder() {
  const f = variantForm.value;
  if (!f) return "";
  if (f.mode === "by_shot") {
    return "可选补充：例如第一镜和最后一镜要明显不同；其余交给系统按镜头内容画";
  }
  if (f.mode === "restyle") {
    return "想怎么改？例如：轮廓更简洁、材质更粗糙、颜色更冷";
  }
  if (f.mode === "replace") {
    return "换成什么？例如：一头鹿 / 一朵菊花 / 一个骑自行车的人";
  }
  return "想怎么改？例如：把主角换成一头鹿 / 不要头盔 / 每一镜跟着时代变";
}

function openVariantForm(t) {
  if (!t || t.system) return;
  const slots = (t.slots || []).filter((s) => s && s.component);
  variantForm.value = {
    template: t.name,
    display: t.display || t.name,
    slots,
    preview: t.preview_url || "",
    // 默认让系统判断：用户只说一句人话，服务端解读出换哪个零件、按什么分档。
    // 不预设「换成别的」——那会把「每一镜变」的诉求悄悄压成整片一种。
    component: "",
    mode: "",
    follow: "",
    target: "",
    suggested: "",
    //: 一句话改整片：可以同时说人物和背景，服务端拆成多个零件。
    quick: "",
  };
  variantResult.value = null;
  variantMsg.value = "";
  variantErr.value = "";
  loadVariantOptions("");
}

function cancelVariantForm() {
  if (variantBusy.value) return;   // 写替换件+过闸在跑，关掉只会让人以为没开始
  variantForm.value = null;
}

async function submitVariant(quick = false) {
  const f = variantForm.value;
  if (!f || variantBusy.value) return;
  const typed = quick ? (f.quick || "").trim() : f.target.trim();
  // 「跟着每一镜变」可以一句话不说：默认要求就是按每镜内容变。
  const target = typed || (!quick && f.mode === "by_shot"
    ? "按每一镜的时代背景与内容改变这个零件的轮廓、形态与服饰" : "");
  if (!target) return;
  // 一句话改整片：不预设零件，让服务端把原话拆成几个零件；
  // 精细改：只改点中的那一个（没点＝也让系统判断）。
  const components = quick ? [] : (f.component ? [f.component] : []);
  const mode = quick ? "" : (f.mode || "");
  const follow = quick ? "" : (f.follow || "");
  variantBusy.value = true;
  variantErr.value = "";
  variantMsg.value = mode === "by_shot"
    ? "正在理解「每一镜怎么变」、写替换件与过闸……通常 1~3 分钟，请不要关闭页面…"
    : "正在理解这句话、逐个零件写替换件与过闸……通常 1~3 分钟/件，请不要关闭页面…";
  try {
    await ensureIdentity();
    // 与沉淀同一套「提交任务 → 轮询」：写替换件也是分钟级，别让长连接被掐断。
    // 统一走 edits：一句话可以同时改人物和背景；只点一个零件时它就是单件。
    const base = `/style/templates/${encodeURIComponent(f.template)}`;
    const r = await fetch(`${base}/edits/jobs`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ components, target, mode, follow }),
    });
    const submitted = await r.json().catch(() => ({}));
    if (!r.ok || submitted.ok === false) {
      throw new Error(submitted.error || submitted.detail || ("HTTP " + r.status));
    }
    const jobId = submitted.job_id;
    let j = null;
    for (let i = 0; i < 600; i += 1) {
      await new Promise((done) => setTimeout(done, 3000));
      const pr = await fetch(`${base}/edits/jobs/${jobId}`,
                             { headers: authHeaders() });
      if (pr.status === 404) {
        throw new Error("任务不存在了（服务可能重启过），请重新点一次换件");
      }
      const pj = await pr.json().catch(() => ({}));
      if (!pr.ok) throw new Error(pj.detail || ("HTTP " + pr.status));
      const secs = Math.round(Number(pj.elapsed) || 0);
      variantMsg.value = `正在逐个零件写替换件、过闸……已 ${Math.floor(secs / 60)} 分 `
        + `${secs % 60} 秒（通常 1~3 分钟/件）`;
      if (pj.status === "done") { j = pj.result || {}; break; }
      if (pj.status === "error") {
        const res = pj.result || {};
        const details = Array.isArray(res.detail) ? res.detail : [];
        const head = res.error || (typeof res.detail === "string" ? res.detail : "")
          || ("HTTP " + (pj.status_code || ""));
        throw new Error([head, ...details]
          .filter((x) => typeof x === "string" && x).join("；"));
      }
    }
    if (!j) throw new Error("等待超时，任务可能仍在后台运行；稍后刷新模板库查看");
    const items = (j.edits || []).map((e) => ({
      label: e.label || target,
      component: e.component || "",
      spec: ((e.intent || {}).spec) || "",
      preview: e.preview_url || "",
    }));
    if (!items.length) {
      throw new Error(j.error || "这次没有换成任何零件，请把要求写得更具体一点");
    }
    variantForm.value = null;
    variantResult.value = {
      display: f.display,
      request: target,
      items,
      failures: Array.isArray(j.failures) ? j.failures : [],
    };
    variantMsg.value = "";
    await loadStyleTemplates();
  } catch (e) {
    variantMsg.value = "";
    variantErr.value = "换件失败：" + e.message;
  } finally {
    variantBusy.value = false;
  }
}

// —— 工具库两级导航:父列表(分类) → 二级清单 ——
const toolView = ref(null);       // null=父列表;'tools'|'skills'|'mcp'=已进入的分类
const toolCount = computed(() => {
  const g = toolLib.value && toolLib.value.groups;
  return g ? Object.values(g).reduce((n, items) => n + items.length, 0) : 0;
});

// —— 工具库的「文件夹」形态 ——
//
// 一个技能就是磁盘上的一个目录：`SKILL.md` 是入口，其余（demos/、scripts/、示例）
// 都是它自己的文件。原来界面只给一串平铺的名字 + 一份手写表单，用户看不出包里
// 有什么、也看不出自己拖进来的东西被装成了什么。这里把三类内容都折成树：
// 技能 → 类型文件夹 / 包 / 包内文件（一层一屏）；工具 → 分类文件夹；MCP → 服务 → 工具。
// 展开状态按名字记在几张小表里（缺省：工具/服务展开），刷新清单不会丢。
const skillDirOpen = ref({});   // "技能名/目录路径" → 是否展开
const toolCat = ref("");        // 工具：已点进去的分类；空＝停在分类这一层
const mcpPicked = ref("");      // MCP：已点进去的服务名；空＝停在服务这一层
const fileView = ref(null);     // 文件预览弹窗 {skill,path,loading,text,binary,url,...}

// 注意：这几个函数是从模板里调的，而模板里的 ref 会被**自动解包**——传进来的是
// 那张普通对象，不是 ref。所以这里一律直接用 `bag[key]`，不要再取 `.value`
// （2026-10-22：写成 `.value` 时整个工具库面板渲染抛错，点开是一片空白）。
function treeToggle(bag, key) { bag[key] = !bag[key]; }
// dragleave 在子元素之间移动也会触发：光标还在面板里就别收起提示，否则一闪一闪。
function onToolDrawerDragLeave(ev) {
  const to = ev && ev.relatedTarget;
  if (to && ev.currentTarget && ev.currentTarget.contains(to)) return;
  skillsDragOver.value = false;
}

// relpath 清单 → 目录树（目录在前、同层按名字排）。
function fileTreeOf(paths) {
  const root = { dirs: {}, files: [] };
  for (const raw of paths || []) {
    const parts = String(raw || "").split("/").filter(Boolean);
    if (!parts.length) continue;
    let node = root;
    for (const seg of parts.slice(0, -1)) {
      if (!node.dirs[seg]) node.dirs[seg] = { dirs: {}, files: [] };
      node = node.dirs[seg];
    }
    node.files.push(parts[parts.length - 1]);
  }
  return root;
}

// 目录树 → 行清单（模板只要一层 v-for，不必写递归组件）。
function flattenSkillFiles(skillName, paths) {
  const rows = [];
  const walk = (node, prefix, depth) => {
    for (const dir of Object.keys(node.dirs).sort()) {
      const path = prefix ? `${prefix}/${dir}` : dir;
      const key = `${skillName}/${path}`;
      const open = !!skillDirOpen.value[key];
      rows.push({ kind: "dir", depth, name: dir, path, key, open });
      if (open) walk(node.dirs[dir], path, depth + 1);
    }
    for (const name of node.files.slice().sort()) {
      rows.push({ kind: "file", depth, name, path: prefix ? `${prefix}/${name}` : name });
    }
  };
  walk(fileTreeOf(paths), "", 0);
  return rows;
}

// 技能按「类型文件夹 → 技能包 → 包内文件」三层收，一次只显示当前一层：父级与
// 子级不同屏，跟资源管理器一样点进去才换下一层。类型用契约里的 kind（缺省当流程）。
const SKILL_KIND_ORDER = [
  { key: "workflow", label: "流程" },
  { key: "capability", label: "能力" },
  // 2026-10-24：「风格包」这一档已删除（通道整条撤掉，视觉风格由 skill 承载）。
];
const skillKind = ref("");        // 空＝根目录（类型文件夹）
const skillOpened = ref("");      // 已点进去的技能名；空＝还停在技能清单
const skillFolders = computed(() => {
  const list = skillsAdmin.value || [];
  return SKILL_KIND_ORDER
    .map((k) => ({ ...k, count: list.filter((s) => (s.kind || "workflow") === k.key).length }))
    .filter((f) => f.count);
});
const skillKindLabel = (k) => (SKILL_KIND_ORDER.find((x) => x.key === k) || {}).label || "流程";
const skillListOfKind = computed(() => (skillsAdmin.value || [])
  .filter((s) => (s.kind || "workflow") === skillKind.value));
const openedSkill = computed(() => (skillsAdmin.value || [])
  .find((s) => s.name === skillOpened.value) || null);
const openedSkillRows = computed(() => (openedSkill.value
  ? flattenSkillFiles(openedSkill.value.name, openedSkill.value.files || []) : []));

const toolFolders = computed(() => Object.entries(
  (toolLib.value && toolLib.value.groups) || {}
).map(([cat, items]) => ({ cat, items: items || [] })));
// 当前点进去的那个工具分类（空＝分类这一层）。
const toolCatFolder = computed(() => (toolCat.value
  ? toolFolders.value.find((f) => f.cat === toolCat.value) || null : null));
// 当前点进去的那台 MCP 服务。
const mcpPickedServer = computed(() => (mcpPicked.value
  ? mcpServers.value.find((s) => s.name === mcpPicked.value) || null : null));

async function openSkillFile(skillName, path) {
  if (path === "SKILL.md") { openSkillModal(skillName); return; }
  fileView.value = { skill: skillName, path, loading: true, text: "", binary: false,
                     url: "", bytes: 0, err: "" };
  try {
    await ensureIdentity();
    const r = await fetch(
      `/skills/${encodeURIComponent(skillName)}/file?path=${encodeURIComponent(path)}`,
      { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    fileView.value = { skill: skillName, path: j.path || path, loading: false,
                       text: j.text || "", binary: !!j.binary, url: j.url || "",
                       bytes: j.bytes || 0, mime: j.mime || "", err: "" };
  } catch (e) {
    fileView.value = { skill: skillName, path, loading: false, text: "", binary: false,
                       url: "", bytes: 0, err: e.message };
  }
}
// 工具库一次只显示一层：根目录（工具/技能/MCP）→ 分类 / 技能类型 / 服务 → 里面。
function openToolLibRoot() {
  toolView.value = null; toolCat.value = ""; mcpPicked.value = "";
  skillKind.value = ""; skillOpened.value = "";
}
// 服务端词表（/tools 的 name_display）灌进来的 {机器名: 中文名}：界面标签的第一来源。
// 拉不到（离线装配 / Storyline 未连通）时退回 TOOL_LABELS，再退机器名本身。
const catalogLabels = ref({});
// /tools 的 params_display：参数名中文标签的**单源表**（帧里的 arg_labels 才是第一来源）。
const paramLabels = ref({});
const activePanel = ref(null);

// 抽屉宽度**按面板各记一份**：文字库那一屏是左右两栏（样张 248px 钉在左边 + 右边两列卡），
// 拢共要 ~750px；380px 底下那两栏会被挤成一条，还不如原来的竖排。
// 别的面板沿用老宽度、老键 `ca.drawerW`——用户之前拖出来的宽度不会丢。
// 四个库改成资源管理器那种两栏（左文件夹、右内容）之后，380px 连一行表格都排不下；
// 这几屏各给一个够宽的初值。用户拖过的宽度仍按 `ca.drawerW.<panel>` 记着，不会被覆盖。
const DRAWER_W_DEFAULT = { textlib: 960, library: 760, bgm: 760, tools: 620 };
const DRAWER_W_MIN = 300;
const DRAWER_W_MAX = 1000;      // 上限跟着抬高：样张那一屏宽一点才排得开两列卡
const DRAWER_W_FALLBACK = 380;
function drawerWOf(panel) {
  const own = Number(localStorage.getItem("ca.drawerW." + panel));
  if (own) return own;
  if (DRAWER_W_DEFAULT[panel]) return DRAWER_W_DEFAULT[panel];
  return Number(localStorage.getItem("ca.drawerW")) || DRAWER_W_FALLBACK;
}
const drawerW = ref(drawerWOf(activePanel.value));
const resizing = ref(false);
function startResize(ev) {
  if (ev.button !== 0) return;
  resizing.value = true;
  const startX = ev.clientX, startW = drawerW.value;
  const move = (e) => {
    // 向左拖 = 变宽;夹在 300~1000
    drawerW.value = Math.min(DRAWER_W_MAX,
      Math.max(DRAWER_W_MIN, startW + (startX - e.clientX)));
  };
  const up = () => {
    resizing.value = false;
    localStorage.setItem("ca.drawerW." + (activePanel.value || ""), String(drawerW.value));
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
  ev.preventDefault();
}
// 换面板＝换成那一屏自己记着的宽度（文字库第一次打开是 560，其余是 380）。
watch(activePanel, (p) => { drawerW.value = drawerWOf(p); });
const libraryItems = ref([]);
const loadingLibrary = ref(false);
const libSelected = reactive({});   // material_id -> true
const deletingLib = ref(false);
// 四个库同一种形状：左边一列文件夹（类型 / 来源 / 维度），右边是这个文件夹里的东西。
const MATERIAL_KINDS = [
  { key: "video", label: "视频" },
  { key: "image", label: "图片" },
  { key: "audio", label: "音频" },
];
const materialFolders = computed(() => {
  const groups = MATERIAL_KINDS.map((k) => ({
    ...k, items: libraryItems.value.filter((it) => (it.kind || "") === k.key),
  }));
  // 认不出的类型不丢：归到「其他」，否则素材传上来了却在库里看不见。
  const rest = libraryItems.value.filter(
    (it) => !MATERIAL_KINDS.some((k) => k.key === it.kind));
  if (rest.length) groups.push({ key: "other", label: "其他", items: rest });
  return groups.filter((g) => g.items.length);
});

// ---- 四个库共用的浏览骨架：一次只显示一层 ----
// 空串＝文件夹这一层（只列文件夹）；点进去以后才是里面那一层，靠顶部「返回」回上一级。
// 不再常驻左栏：父级和子级不同屏，跟资源管理器点进文件夹一样。
// 视图模式按库各记一份；素材/音乐缺省看图标（缩略图本来就是挑素材的主要依据），
// 文字库缺省看详细。
const libPicked = ref({ library: "", bgm: "", textlib: "" });
const libMode = ref({ library: "icons", bgm: "icons", textlib: "details" });
const materialFolder = computed(() => (materialFolders.value
  .find((f) => f.key === libPicked.value.library) || null));
const bgmFolder = computed(() => {
  const k = libPicked.value.bgm;
  return k === "imported" || k === "online" ? k : "";
});
// 详细视图里正在试听的那一行（键：m:<素材> / b:<已导入> / o:<曲目>）；同一时刻只留一个。
const libPlaying = ref("");
function toggleLibPlaying(key) {
  libPlaying.value = libPlaying.value === key ? "" : key;
}
// 在线曲目要先取直链，再铺到详情视图里那一行。
async function playBgmRow(item) {
  const key = "o:" + item.track_id + "|" + item.source;
  if (!bgmAudioUrl.value[key]) await playBgm(item);
  if (bgmAudioUrl.value[key]) libPlaying.value = key;
}
const mediaLabel = (kind) => ({ video: "视频", image: "图片", audio: "音频" }[kind] || "文件");

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
  // 模板库与素材库同屏：一个列原料，一个列「已经沉淀好的可复用成品」。
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

// ---- 文字库：把「字幕长什么样」与「谁来读」从模型手里接过来（只对本对话生效） ----
// 面板上出现的**每一个取值与中文词都来自 /motion/text-library**（剪辑服务端契约里的
// `library_props()`）：档名、取值、标签、连栏目名都在那一份里。前端自己列一遍的后果
// 不是报错，是界面上写着「撕纸条·逐词点亮」而渲染认的是另一个字符串——这种不一致不会红，
// 只会渲成别的。下面只有几个**协议键名**（不是给人看的词）：样张那两个查询参数，
// 以及归右栏的声音两维。字样两维的**栏名、取值、顺序**一律按契约里出现的顺序现取。
const CAPTION_DIM = "caption_style";     // 样张端点的查询参数名 · 第一维
const HIGHLIGHT_DIM = "highlight_style"; // 样张端点的查询参数名 · 第二维
const SOUND_DIMS = ["voice", "rate"];
const textLib = ref({ presets: [], dims: {}, voice_groups: [] });
const loadingTextLib = ref(false);
const textLibError = ref("");
const convPrefs = ref({});          // 本对话挑过的维度：{维度: 原始取值}，未挑过的键不存在
const savingPrefs = ref("");
const shownPick = ref({});          // 样张区正看哪几档：{组: 档名}，点说明换它、**不改偏好**
const voicePreviewUrls = ref({});   // 试听直链缓存（试听文案固定，同一音色同一语速只合成一次）
const previewKey = ref("");         // 正在响的那个音色+语速；同一时刻只留一个播放器
const loadingVoice = ref("");

// 两排卡：按契约里 `presets[].group` 的出现顺序取 unique，前端不列名单。
const presetGroups = computed(() => {
  const seen = [];
  for (const p of textLib.value.presets || []) {
    if (p.group && !seen.includes(p.group)) seen.push(p.group);
  }
  return seen;
});
function presetsOf(group) {
  return (textLib.value.presets || []).filter((p) => p.group === group);
}
// 预设档管得着哪几维 —— 从契约的档位里现取并集，不另写一份名单。
const presetDims = computed(() => {
  const seen = [];
  for (const p of textLib.value.presets || []) {
    for (const k of Object.keys(p.knobs || {})) if (!seen.includes(k)) seen.push(k);
  }
  return seen;
});
// 顶栏那句「本对话：…」分两段说：字样那两维（预设档管得着的）与声音那两维。
const subtitleDims = computed(() => presetDims.value.filter((k) => !SOUND_DIMS.includes(k)));
const libReady = computed(() => !!(textLib.value.presets || []).length
  || !!(textLib.value.voice_groups || []).length);
// 音色总数：文件夹标题上要报个数，和工具库那边的「N 个」同一种读法。
const voiceTotal = computed(() => (textLib.value.voice_groups || [])
  .reduce((n, g) => n + (g.items || []).length, 0));

// 文字库的左侧文件夹：每组预设一档、每个自由取色维度一档、音色一档。
// 名单全部从契约现取（`presetGroups` / `colorDims` / `voice_groups`），不另写一份。
const textFolders = computed(() => {
  const out = [];
  for (const g of presetGroups.value) {
    out.push({ key: "g:" + g, label: dimTitle(g), count: presetsOf(g).length,
      kind: "preset", group: g });
  }
  for (const dim of colorDims.value) {
    out.push({ key: "c:" + dim, label: dimTitle(dim), count: 2, kind: "color", dim });
  }
  out.push({ key: "voice", label: dimTitle("voice"), count: voiceTotal.value, kind: "voice" });
  return out;
});
const textFolder = computed(() => (textFolders.value
  .find((f) => f.key === libPicked.value.textlib) || null));
// 音色不再逐层点开文件夹：一屏列出全部，组名进「类型」一列，找起来更快。
const voiceRows = computed(() => {
  const out = [];
  for (const g of textLib.value.voice_groups || []) {
    for (const v of g.items || []) out.push({ ...v, group: g.label });
  }
  return out;
});

function labelOf(dim, value) {
  return ((textLib.value.dims || {})[dim]?.value_labels || {})[value] || value;
}
function dimTitle(dim) {
  return (textLib.value.dims || {})[dim]?.title || dim;
}
function presetOn(p) {
  const knobs = p.knobs || {};
  const keys = Object.keys(knobs);
  return keys.length > 0 && keys.every((k) => convPrefs.value[k] === knobs[k]);
}
// 这一组正看哪一档：点过的（只看不挑）> 他挑中的 > 清单第一档。**每组各取一档**，
// 于是「字幕样式」与「高亮样式」两排各自记得自己那一格，拼起来就是样张那一对。
function shownOf(group) {
  const list = presetsOf(group);
  return list.find((p) => p.key === shownPick.value[group])
    || list.find(presetOn) || list[0] || null;
}
function showPreset(p) {
  const cur = shownPick.value[p.group];
  shownPick.value = { ...shownPick.value, [p.group]: cur === p.key ? "" : p.key };
}
function summaryOf(dims) {
  return dims.map((k) => (convPrefs.value[k] ? labelOf(k, convPrefs.value[k]) : ""))
    .filter(Boolean).join(" · ");
}
const hasPrefs = computed(() => Object.keys(convPrefs.value || {}).length > 0);
const subtitleSummary = computed(() => summaryOf(subtitleDims.value));
const soundSummary = computed(() => summaryOf(SOUND_DIMS));
const vkey = (v) => v + "|" + (convPrefs.value.rate || "");

async function loadTextLib() {
  loadingTextLib.value = true;
  textLibError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/motion/text-library", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    textLib.value = (j && typeof j === "object") ? j : {};
  } catch (e) {
    textLib.value = { presets: [], dims: {}, voice_groups: [] };
    textLibError.value = "拿不到字幕样式与音色清单：" + e.message;
  } finally {
    loadingTextLib.value = false;
  }
}

async function loadConvPrefs() {
  if (!active.value) { convPrefs.value = {}; return; }
  try {
    await ensureIdentity();
    const r = await fetch(`/convs/${encodeURIComponent(active.value)}/prefs`,
      { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    convPrefs.value = j.prefs || {};
  } catch (e) {
    convPrefs.value = {};
    textLibError.value = "读不到本对话已挑的样式：" + e.message;
  }
}

// 写入一律走服务端的校验口（值不在枚举里会被 422 打回，契约拿不到时它宁可拒收），
// 所以这里不做本地白名单——本地再判一遍就等于第二份名单。
async function saveConvPrefs(patch) {
  if (!active.value) return;
  savingPrefs.value = Object.keys(patch).join("、");
  textLibError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/convs/${encodeURIComponent(active.value)}/prefs`, {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ prefs: patch }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    convPrefs.value = j.prefs || {};
  } catch (e) {
    textLibError.value = "这次没存上：" + e.message;
  } finally {
    savingPrefs.value = "";
  }
}

async function pickPreset(p) {
  // 选中一档＝只写**它自己那一组的那一维**。两排卡并存，写别的组就把另一排清掉了
  // ——「字幕样式」与「高亮样式」是两条正交的选择，谁都不该动对方。
  // （档位自带的 `knobs` 本来就只含本组那一维，所以照原样写出去即可。）
  await saveConvPrefs({ ...(p.knobs || {}) });
}

async function pickDim(dim, value) {
  await saveConvPrefs({ [dim]: value || null });
}

async function clearPrefs() {
  const patch = {};
  for (const k of Object.keys(convPrefs.value)) patch[k] = null;
  shownPick.value = {};
  await saveConvPrefs(patch);
}

async function auditionVoice(voice) {
  const rate = convPrefs.value.rate || "";
  const key = voice + "|" + rate;
  loadingVoice.value = voice;
  textLibError.value = "";
  try {
    await ensureIdentity();
    let url = voicePreviewUrls.value[key];
    if (!url) {
      const r = await fetch(`/tts/preview?voice=${encodeURIComponent(voice)}`
        + `&rate=${encodeURIComponent(rate)}`, { headers: authHeaders() });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
      url = j.url;
      voicePreviewUrls.value = { ...voicePreviewUrls.value, [key]: url };
    }
    // 换一个音色＝换掉那个 <audio> 的 src：只有重新挂载才会触发 autoplay，
    // 所以先清空、等一帧再置上（同一档再点一次也重放）。
    previewKey.value = "";
    await nextTick();
    previewKey.value = key;
  } catch (e) {
    textLibError.value = "试听失败：" + e.message;
  } finally {
    loadingVoice.value = "";
  }
}

async function toggleTextLib() {
  activePanel.value = activePanel.value === 'textlib' ? null : 'textlib';
  if (activePanel.value === 'textlib') {
    // 样张跟着清单走：`previewPair` 要等清单回来才知道是哪一对，那个 watch 会接上。
    // 不 await 清单——它是本地读契约，快；而样张那一路要打包几秒（渲染容器那一侧）。
    await loadTextLib();
    await loadConvPrefs();
    void loadCaptionPreview();
  }
}

// ---- 字幕样张：把「这一对渲出来长什么样」在**渲染之前**看到 ----------------------
// 探的是 /motion/caption-preview，出来的是渲染链渲的真图（同一张样片镜、同一件字幕元件，
// 只换注入的两个样式名与强调色）——不是前端照着档名画一张示意图。示意图是第二份实现，
// 它会与成片漂，而漂的方式恰恰是「看着对、渲出来不是」。
//
// 缓存按**那一对**存：一次只渲一张，点过的再来就走对象存储里的缓存。
//
// 这里曾经还有一行色板筹码（换样张的底色/纸条底/字色看同一对的样子）。用户 2026-10-16
// 看过之后否掉了：「可以自定义了，为什么还要你提供那些固定的」。成片里每一镜各按自己绑的
// 那套色板走、色板本来就是模型按镜挑的，面板上再摆一份**只作用于这张图**的固定九档，
// 就是第二份名单——而且其中「强调色」那一色与下面那一行是同一个东西，钉住之后筹码上
// 那个圆点还会显示错的色。取色那件事只归「强调色」那一行。
const capPreview = ref({});      // { "字幕+高亮[@色值]": 直链 | null }；null＝要过、没回来
const loadingPreview = ref(false);
const previewErr = ref("");

// 自由取色的那几维（契约里 `kind === "color"`）：面板据此渲取色器，而不是一排卡。
// 键名一个都不写死——今天只有「强调色」一维，但这一行不知道这件事。
const colorDims = computed(() => Object.entries(textLib.value.dims || {})
  .filter(([, d]) => d && d.kind === "color").map(([k]) => k));
const colorDim = computed(() => colorDims.value[0] || "");
// 用户钉住的色值；空串＝没钉＝跟色板走。只认归一过的六位小写，别的一律当没钉
// （收下的脏值只会让样张与成片悄悄差一个颜色，且两边的缓存键还不一致）。
const previewColor = computed(() => {
  const v = String(convPrefs.value[colorDim.value] || "").trim().toLowerCase();
  return /^#[0-9a-f]{6}$/.test(v) ? v : "";
});
// 取色器的起手色＝契约里「默认」那一档的强调色（母版底色的那一套）。前端**不新长出一个
// 品牌色常量**——那会与片子里真正用的色板漂开。
const colorSeed = computed(() => {
  const list = textLib.value.palettes || [];
  const pal = list.find((p) => p.is_default) || list.find((p) => !p.key) || list[0] || {};
  const sw = pal.swatch || {};
  return sw.accent || sw.text || "#ff8a3d";
});

// 每组各取一档，拼成正看的那一对。空串＝这一组还没得挑（清单没回来）。
const previewCaption = computed(() => (shownOf(CAPTION_DIM) || {}).key || "");
const previewHighlight = computed(() => (shownOf(HIGHLIGHT_DIM) || {}).key || "");
// 与后端 `caption_preview.pair_key` 同一拼法（`caption+highlight[@色值]`）——回执键、
// 对象键、缓存键都是它，前端只是照契约拼一次。色值进这个键，于是「换个颜色＝换张图」
// 在这三层上自动成立，不用另加一段清理逻辑。
const previewPair = computed(() => (previewCaption.value && previewHighlight.value)
  ? `${previewCaption.value}+${previewHighlight.value}`
    + (previewColor.value ? `@${previewColor.value.slice(1)}` : "")
  : "");
const previewPairLabel = computed(() => {
  const a = shownOf(CAPTION_DIM); const b = shownOf(HIGHLIGHT_DIM);
  return (a && b) ? `${a.label} · ${b.label}` : "";
});
const previewUrl = computed(() => previewPair.value
  ? (capPreview.value[previewPair.value] || "") : "");
// 这一对**要回来过**没有：要回来过却没有直链（渲染中途出错时那条不回键），与「还没去要」
// 是两句不同的话——前者说「这一对这次没渲出来」，后者说「点一下就有」。
const previewAsked = computed(() => previewPair.value
  && previewPair.value in capPreview.value);

async function loadCaptionPreview() {
  const pair = previewPair.value;
  if (!pair) return;                                     // 清单还没回来，没得可渲
  if (pair in capPreview.value) return;                   // 这一对已经要过了
  loadingPreview.value = true;
  previewErr.value = "";
  try {
    await ensureIdentity();
    // `palette` 留空＝「默认（片子的底色）」那一档：成片里每一镜各按自己绑的色板走，
    // 这张图不该假装站在某一套色板上。
    const r = await fetch(`/motion/caption-preview?palette=`
      + `&caption=${encodeURIComponent(previewCaption.value)}`
      + `&highlight=${encodeURIComponent(previewHighlight.value)}`
      + `&color=${encodeURIComponent(previewColor.value)}`,
      { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    let url = null;
    for (const s of j.stills || []) if (s.key === pair) url = s.url;
    // 记的是**这一对**（而不是整批回执）：换一对样式或换一个强调色就是另一个键。
    capPreview.value = { ...capPreview.value, [pair]: url };
  } catch (e) {
    previewErr.value = "字幕样张没渲出来：" + e.message;
  } finally {
    loadingPreview.value = false;
  }
}

// 换正看的那一对（点卡片说明）或换强调色都要重取。清单回来之前 `previewPair` 是空串，
// 那一次进函数就返回——清单回来时它变了，这里会自己再跑一次。
watch(previewPair, () => { void loadCaptionPreview(); });

// 刷新＝连样张一起重取：清单与样张是两件事，但「刷新」这个动作在人心里是
// 「这一屏重新来一遍」，只换清单会留下半旧的图。
async function refreshTextLib() {
  capPreview.value = {};
  await loadTextLib();
  await loadCaptionPreview();
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
// `force`：技能保存后必须重拉——`/tools` 里带着技能清单与词表，缓存一次就永远不刷新，
// 新建的风格包在入口下拉里要等到刷新页面才出现。
async function loadTools(force = false) {
  if (toolLib.value && !force) return;
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
    openToolLibRoot();                // 从最外层文件夹开始，一次一层
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
    headers: (s.config && s.config.headers) ? JSON.stringify(s.config.headers, null, 2) : "",
    command: (s.config && s.config.command) || "",
    args: ((s.config && s.config.args) || []).join(", "),
    tool_timeout: (s.config && s.config.tool_timeout) || 60,
  } : { name: "", type: "streamableHttp", url: "", headers: "", command: "", args: "", tool_timeout: 60 };
  mcpMsg.value = "";
}

// 登记/更新一台 MCP Server（**只写配置，不连线**——connect 走 enable）。
// 工具库弹窗与设置页区块共用这一支：两个入口写的是同一张表、同一份字段口径。
async function mcpUpsert(name, config) {
  const r = await fetch("/mcp/servers", {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ name, config }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
  return j;
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
  if (f.headers && f.headers.trim()) {
    try {
      config.headers = JSON.parse(f.headers);
    } catch (e) {
      mcpMsg.value = "headers 不是合法 JSON：" + e.message;
      return;
    }
  }
  mcpBusy.value = true;
  try {
    const j = await mcpUpsert(f.name.trim(), config);
    mcpForm.value = null;
    itemModal.value = null;
    mcpMsg.value = "已保存：" + j.saved + "（点「启用」才会连接）";
    await loadMcpAdmin();
  } catch (e) {
    mcpMsg.value = "保存失败：" + e.message;
  } finally {
    mcpBusy.value = false;
  }
}

async function toggleMcp(s, sink = mcpMsg) {
  mcpBusy.value = true;
  try {
    const op = s.live ? "disable" : "enable";
    const r = await fetch(`/mcp/servers/${encodeURIComponent(s.name)}/${op}`,
                          { method: "POST", headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    sink.value = op === "enable"
      ? `已连接，注册 ${(j.tools || []).length} 个工具`
      : "已断开";
    await loadMcpAdmin();
  } catch (e) {
    // detail 本身已是人话(如「连接失败:<urlopen error ...>」),不再叠加前缀
    sink.value = e.message;
  } finally {
    mcpBusy.value = false;
  }
}

async function delMcp(s, sink = mcpMsg) {
  if (!confirm(`删除 MCP 服务 ${s.name}？（在线会先断开）`)) return;
  const r = await fetch(`/mcp/servers/${encodeURIComponent(s.name)}`,
                        { method: "DELETE", headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { sink.value = "删除失败：" + (j.detail || r.status); return; }
  sink.value = "已删除：" + j.deleted;
  // 正停在这台服务的详情上：删完退回服务清单，别留一屏空白。
  if (mcpPicked.value === s.name) mcpPicked.value = "";
  await loadMcpAdmin();
}

// ---- 设置页的 MCP 区块：与工具库那边共用读写，只换个草稿与回话出口 ----

//: 已连线的 MCP server 数（设置页抬头那一行读它）
const liveMcpCount = computed(() => mcpServers.value.filter((s) => s.live).length);
//: 已连线 server 贡献的工具总数——这个数就是本轮 `context_ledger.tools` 里来自 MCP 的那部分
const mcpToolCount = computed(
  () => mcpServers.value.reduce((n, s) => n + ((s.tools || []).length), 0));

async function addMcpFromSettings() {
  const d = mcpDraft.value;
  if (!d.name.trim()) { mcpSetMsg.value = "名称必填"; return; }
  const config = { type: d.type, tool_timeout: 60, enabled_tools: ["*"] };
  if (d.type === "stdio") {
    config.command = d.command.trim();
    config.args = d.args.split(",").map((x) => x.trim()).filter(Boolean);
  } else {
    config.url = d.url.trim();
  }
  mcpBusy.value = true;
  try {
    const j = await mcpUpsert(d.name.trim(), config);
    mcpSetMsg.value = `已登记 ${j.saved}（点「启用」才连线）`;
    // 只清地址不清 name/type：连着登记两三台同一类 server 时少敲两下
    mcpDraft.value = { ...d, name: "", url: "", command: "", args: "" };
    await loadMcpAdmin();
  } catch (e) {
    mcpSetMsg.value = "登记失败：" + e.message;
  } finally {
    mcpBusy.value = false;
  }
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

// ---- 技能文件夹导入：拖一个文件夹进来就能读能用的那条路 ------------------
//
// 浏览器不让页面读本地目录的绝对路径，只给「文件 + 相对路径」。所以这里做三件事：
// 递归把一个目录展开成文件清单、按扩展名决定走文本还是 base64，再整包发给
// `/skills/import_folder`（服务端拼回 zip，走与 zip 上传同一条安装路径）。
// 大目录（node_modules / .git / 图片素材库）在展开时就丢掉——技能包只需要正文与
// 它点名的附属文件，把整个仓库塞上去只会撞条目数上限。

const SKILL_TEXT_EXT = new Set([
  ".md", ".txt", ".json", ".yaml", ".yml", ".tsx", ".ts", ".js", ".jsx",
  ".py", ".sh", ".css", ".html", ".csv", ".toml", ".mjs", ".cjs",
]);
const SKILL_SKIP_DIR = new Set([
  "node_modules", ".git", ".github", "dist", "build", "__pycache__",
  ".venv", "venv", "target", ".idea", ".vscode",
]);

function skillRelPath(raw) {
  let rel = String(raw || "").replace(/\\/g, "/");
  while (rel.startsWith("./")) rel = rel.slice(2);
  rel = rel.replace(/^\/+/, "");
  const parts = rel.split("/").filter((p) => p && p !== ".");
  if (parts.some((p) => p === "..")) return "";
  return parts.join("/");
}

function skillFileSkipped(path) {
  const parts = path.split("/");
  if (parts.some((p) => SKILL_SKIP_DIR.has(p))) return true;
  return parts.some((p) => p.startsWith(".") && p !== ".well-known");
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => {
      const s = String(fr.result || "");
      resolve(s.slice(s.indexOf(",") + 1));
    };
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

function readAsText(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || ""));
    fr.onerror = () => reject(fr.error);
    fr.readAsText(file);
  });
}

async function skillPayloadOf(file, path) {
  const ext = ("." + (path.split(".").pop() || "")).toLowerCase();
  // 文本按 UTF-8 读；其余（字体、图片）走 base64。判据是扩展名而不是「读失败再试」：
  // 一个二进制文件被当文本读不会报错，只会变成一堆替换字符——装上去了、内容却是坏的。
  if (SKILL_TEXT_EXT.has(ext) && file.size < 512 * 1024) {
    return { path, text: await readAsText(file) };
  }
  if (file.size > 4 * 1024 * 1024) return null;  // 单个附属文件上限
  return { path, b64: await readAsBase64(file) };
}

function walkDirEntry(entry, prefix, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((f) => {
        out.push({ file: f, path: prefix + entry.name });
        resolve();
      }, () => resolve());
      return;
    }
    if (!entry.isDirectory) return resolve();
    if (SKILL_SKIP_DIR.has(entry.name)) return resolve();
    const reader = entry.createReader();
    const all = [];
    // readEntries 一次最多回 100 条，要一直读到空批——不循环的话，大目录会静默
    // 只装前 100 个文件（技能包通常没这么多，但参考片/脚本多一点的包就会中招）。
    const readBatch = () => reader.readEntries((batch) => {
      if (!batch.length) {
        (async () => {
          for (const e of all) await walkDirEntry(e, prefix + entry.name + "/", out);
          resolve();
        })();
        return;
      }
      all.push(...batch);
      readBatch();
    }, () => resolve());
    readBatch();
  });
}

async function importSkillFolderPayload(files, root) {
  if (!files.length) {
    skillsMsg.value = "这个文件夹是空的（或只有被忽略的目录）";
    return;
  }
  // 只挡「一个入口都没有」：一个技能文件夹、或一个装着若干技能文件夹的父文件夹，
  // 里面都至少有一份 SKILL.md。具体的层级由服务端剥壳判定（多套一层也能装）。
  if (!files.some((f) => f.path.split("/").pop() === "SKILL.md")) {
    skillsMsg.value = "没有找到 SKILL.md：拖一个技能文件夹，或一个装着若干技能文件夹的父文件夹";
    return;
  }
  skillsMsg.value = `读取 ${files.length} 个文件…`;
  const payload = [];
  for (const f of files) {
    const item = await skillPayloadOf(f.file, f.path);
    if (item) payload.push(item);
  }
  skillsMsg.value = `安装中（${payload.length} 个文件）…`;
  const r = await fetch("/skills/import_folder", {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ files: payload, root: root || "" }),
  });
  const j = await r.json().catch(() => ({}));
  skillsMsg.value = r.ok
    ? "已装好：" + (j.imported || []).join("、")
    : "失败：" + (j.detail || r.status);
  await loadSkillsAdmin();
  // 装完就停在技能库，并落到刚装的那个包的类型里——装了一个就直接进包内，
  // 装了多个就停在那类技能清单上，免得用户自己一层层找。
  toolView.value = "skills";
  const names = j.imported || [];
  const first = (skillsAdmin.value || []).find((s) => s.name === names[0]);
  if (first) {
    skillKind.value = first.kind || "workflow";
    skillOpened.value = names.length === 1 ? first.name : "";
  }
}

async function importSkillFolderFromInput(ev) {
  const list = Array.from(ev.target.files || []);
  ev.target.value = "";
  if (!list.length) return;
  const root = String(list[0].webkitRelativePath || "").split("/")[0] || "";
  const files = [];
  for (const f of list) {
    const rel = skillRelPath(f.webkitRelativePath || f.name);
    if (!rel || skillFileSkipped(rel)) continue;
    files.push({ file: f, path: rel });
  }
  await importSkillFolderPayload(files, root);
}

async function dropSkillFolder(ev) {
  skillsDragOver.value = false;
  const dt = ev.dataTransfer;
  if (!dt) return;
  const collected = [];
  const items = Array.from(dt.items || []);
  const entries = items
    .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (entries.length) {
    for (const e of entries) await walkDirEntry(e, "", collected);
  } else {
    for (const f of Array.from(dt.files || [])) collected.push({ file: f, path: f.name });
  }
  const files = [];
  for (const f of collected) {
    const rel = skillRelPath(f.path);
    if (!rel || skillFileSkipped(rel)) continue;
    files.push({ file: f.file, path: rel });
  }
  const root = files[0] ? files[0].path.split("/")[0] : "";
  await importSkillFolderPayload(files, root);
}

function pickSkillFolder() {
  const el = document.getElementById("skill-folder");
  if (el) el.click();
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
  if (r.ok && skillOpened.value === s.name) skillOpened.value = "";
  await loadSkillsAdmin();
}

// ---- 条目弹窗 ----

function openToolModal(t, cat) {
  // 动态 MCP 工具的名字带 "{服务名}_" 前缀：弹窗里给出来源与跳转
  const live = mcpServers.value.find(
    (sv) => sv.live && t.name.startsWith(sv.name + "_"));
  itemModal.value = {
    kind: "tool", name: t.name, desc: t.desc, cat,
    display: toolLabel(t.name, t.name_display), params: t.params || null,
    source: live ? "mcp" : "code", serverName: live ? live.name : "",
  };
}

function openMcpModal(s) {
  editMcp(s);                       // 复用既有的表单填充逻辑
  itemModal.value = { kind: "mcp", create: !s, server: s || null };
  mcpMsg.value = "";
}

async function openSkillModal(name) {
  itemModal.value = { kind: "skill", name, detail: null, loading: true };
  skillForm.value = null;
  try {
    await ensureIdentity();
    const r = await fetch(`/skills/${encodeURIComponent(name)}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    itemModal.value.detail = j;
  } catch (e) {
    itemModal.value.detail = null;
    itemModal.value.error = e.message;
  } finally {
    itemModal.value.loading = false;
  }
}

function editSkillForm() {
  const d = itemModal.value.detail;
  if (!d) return;
  skillForm.value = {
    name: d.name, display: d.display || "", always: !!d.always,
    description: d.description || "", requires: (d.requires || []).join(", "),
    body: d.body || "", kind: d.kind || "workflow",
    // 这两栏保存时必须带回去——从前 PUT 不带它们，保存一次就把技能的分段声明
    // 与排除规则静默清空了。表单里给一行逗号清单，解析仍在服务端做。
    sectionsText: (d.sections || []).join(", "),
    excludeText: (d.exclude || []).join(", "),
    manifestText: JSON.stringify(d.style_manifest || {}, null, 2),
  };
  skillsMsg.value = "";
}

// 「填入剪影模板」不在这里抄一份默认值：从已入库的系统风格包读真 manifest，
// 前端就不必维护第四份副本，库里改了这里跟着变。
async function fillStyleTemplate() {
  const f = skillForm.value;
  if (!f) return;
  try {
    await ensureIdentity();
    const r = await fetch("/skills/style_silhouette_history", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    const m = j.style_manifest || {};
    if (!Object.keys(m).length) throw new Error("系统风格包没有 manifest（镜像里的种子没同步？）");
    f.manifestText = JSON.stringify(m, null, 2);
    if (f.kind !== "style") f.kind = "style";
    skillsMsg.value = "已填入「剪影年轮」的 manifest";
  } catch (e) {
    skillsMsg.value = "填模板失败：" + e.message;
  }
}

// 逗号清单 → 数组（中英文逗号、顿号都认）。空串就是空数组，不是 [""]。
function splitList(text) {
  return String(text || "").split(/[,，、]/).map((s) => s.trim()).filter(Boolean);
}

const SKILL_KINDS = { workflow: "工作流", capability: "能力", style: "风格包" };
function kindLabel(kind) {
  return SKILL_KINDS[kind] || SKILL_KINDS.workflow;
}

function manifestPretty(m) {
  try {
    return JSON.stringify(m || {}, null, 2);
  } catch (e) {
    return String(m || "");
  }
}

// 教程里那条「Studio 改动会写回代码，调参前先留版本」的最小实现：
// 服务端在 manifest 变化时把旧值存进 `style_manifest_prev`，这里把它填回表单。
function restorePrevManifest() {
  const d = itemModal.value.detail;
  if (!d || !d.style_manifest_prev) return;
  editSkillForm();
  skillForm.value.manifestText = JSON.stringify(d.style_manifest_prev, null, 2);
  if (skillForm.value.kind !== "style") skillForm.value.kind = "style";
  skillsMsg.value = "已把上一版 manifest 填回表单；点保存才生效。";
}

// ——— 旧风格工作室的遗留状态（界面已撤）———
// 风格现在只在对话里改（`style_look`）。这些 ref 与函数已不被模板引用，构建时会被
// 摇掉；留在这里只是因为撤销时没动它们，后续清理可整段删除。
const studioTab = ref("style");
const studioName = ref("");
const studioDetail = ref(null);
const studioBody = ref("");
const studioManifest = ref(null);
const studioMsg = ref("");
const studioErr = ref("");
const studioBusy = ref("");
const studioStills = ref([]);
const studioOpenTpl = ref("");
// 「一句话生成」：用户写需求，模型写数据（名字/正文/manifest），落成自己的一份风格。
// 不生成模板代码——模板仍走沉淀/换件那条链；这里只决定挂哪份现成模板或不挂。
const studioIdea = ref("");
const studioGen = ref(false);
// 「改这一版」：一句话改数据（manifest/正文），改完仍是草稿；不碰模板代码。
const studioSay = ref("");
const studioSayBusy = ref(false);

const STUDIO_COLORS = [
  ["page", "底色"], ["surface", "台面"], ["field", "场景深处"],
  ["text", "文字"], ["muted", "次要字"], ["accent", "强调"],
  ["border", "边框"], ["light", "主光"], ["rim", "边光"],
];

function studioStyles() {
  return (skillsAdmin.value || []).filter((s) => s && s.kind === "style");
}

/** 工作副本：四个块先补成对象，模板里的 `v-model` 才不会因为缺键而写不进去。 */
function normManifest(m) {
  const out = (m && typeof m === "object" && !Array.isArray(m))
    ? JSON.parse(JSON.stringify(m)) : {};
  for (const k of ["palette", "composition", "caption", "motion"]) {
    if (!out[k] || typeof out[k] !== "object" || Array.isArray(out[k])) out[k] = {};
  }
  return out;
}

function studioPx() {
  // 1080p 上「这个比例是多少像素」——滑杆旁边那行小字，比 0.045 好读。
  const c = studioManifest.value && studioManifest.value.caption;
  return c ? Math.round(1080 * Number(c.bottom || 0)) : 0;
}

/** 配色只列 `#rrggbb` 的项：`sky` 这种三段色数组另走一行文本，色块控件吃不了数组。 */
function studioPaletteEntries() {
  const p = (studioManifest.value && studioManifest.value.palette) || {};
  return Object.entries(p).filter(([, v]) => typeof v === "string"
    && /^#[0-9a-fA-F]{6}$/.test(v)).map(([k, v]) => ({ key: k, value: v }));
}

function setStudioList(key, text) {
  if (!studioManifest.value) return;
  studioManifest.value[key] = String(text || "")
    .split("\n").map((s) => s.trim()).filter(Boolean);
}

async function loadStudio(name = studioName.value) {
  studioErr.value = "";
  studioMsg.value = "";
  studioStills.value = [];
  if (!name) { studioDetail.value = null; studioManifest.value = null; return; }
  studioBusy.value = "load";
  try {
    await ensureIdentity();
    const r = await fetch(`/skills/${encodeURIComponent(name)}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    studioName.value = name;
    studioDetail.value = j;
    studioBody.value = j.body || "";
    studioManifest.value = normManifest(j.style_manifest);
  } catch (e) {
    studioDetail.value = null;
    studioErr.value = "读不到这份风格包：" + e.message;
  } finally {
    studioBusy.value = "";
  }
}

function studioPayload(name, display, body, manifest) {
  const d = studioDetail.value || {};
  // 三处补充要求：空着就**不留键**。留着空串会让「这一项到底写没写过」看不出来，
  // 也会让每存一次 manifest 都多三行噪声；保存端只收有字的那几条。
  const clean = { ...(manifest || {}) };
  for (const k of ["writer_note", "art_note", "review_note"]) {
    const text = String(clean[k] || "").trim();
    if (text) clean[k] = text; else delete clean[k];
  }
  return {
    display: display || d.display || name,
    description: d.description || "",
    always: !!d.always,
    requires: d.requires || "",
    body,
    kind: "style",
    style_manifest: clean,
    sections: d.sections || [],
    exclude: d.exclude || [],
  };
}

async function generateStudio() {
  const idea = studioIdea.value.trim();
  if (idea.length < 4) {
    studioErr.value = "先用一句话说清想要的视觉（至少四个字）。";
    return;
  }
  studioGen.value = true;
  studioErr.value = "";
  studioMsg.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/style/packs/generate", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ description: idea }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    await loadSkillsAdmin();
    await loadStudio(j.name);
    studioMsg.value = `已生成《${j.display}》：可以直接出三帧，或改一改；`
      + "不满意就再说一句重生成一份。";
    studioIdea.value = "";
  } catch (e) {
    studioErr.value = "生成失败：" + e.message;
  } finally {
    studioGen.value = false;
  }
}

async function reviseStudio() {
  const say = studioSay.value.trim();
  if (!studioManifest.value) return;
  if (say.length < 2) {
    studioErr.value = "先说一句想改哪里。";
    return;
  }
  studioSayBusy.value = true;
  studioErr.value = "";
  studioMsg.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(
      `/style/packs/${encodeURIComponent(studioName.value)}/revise`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          instruction: say,
          manifest: studioManifest.value,
          body: studioBody.value,
        }),
      });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    studioManifest.value = normManifest(j.manifest);
    if (j.body) studioBody.value = j.body;
    studioMsg.value = j.note || "已改，点「出三帧预览」看看。";
    if (j.needs_template) {
      studioMsg.value += " 这一步要在「模板」页做：从参考片沉淀一套，或给现有模板换件。";
      studioTab.value = "templates";
    }
    studioSay.value = "";
  } catch (e) {
    studioErr.value = "没改成：" + e.message;
  } finally {
    studioSayBusy.value = false;
  }
}

function restoreStudioPrev() {
  const d = studioDetail.value;
  if (!d || !d.style_manifest_prev) return;
  // 只载入、不自动保存：先看一眼上一版长什么样，再决定要不要写回去。
  studioManifest.value = normManifest(d.style_manifest_prev);
  studioMsg.value = "已载入上一版；要写回就点「保存」，只是看看就不用管。";
}

async function saveStudio() {
  const d = studioDetail.value;
  if (!d) return;
  if (d.system) {
    studioErr.value = "系统内置风格只读：点「另存为我的风格」改一份自己的。";
    return;
  }
  studioBusy.value = "save";
  studioErr.value = "";
  try {
    const r = await fetch(`/skills/${encodeURIComponent(studioName.value)}`, {
      method: "PUT",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(studioPayload(studioName.value, d.display,
                                         studioBody.value, studioManifest.value)),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    studioMsg.value = "已保存；下一版分镜会按这份契约走。";
    await loadSkillsAdmin();
    await loadStudio(studioName.value);
  } catch (e) {
    studioErr.value = "保存失败：" + e.message;
  } finally {
    studioBusy.value = "";
  }
}

async function saveStudioAsMine() {
  const d = studioDetail.value;
  if (!d) return;
  const base = studioName.value.replace(/_mine\d*$/, "");
  let name = `${base}_mine`;
  for (let i = 2; i < 20 && (skillsAdmin.value || []).some((s) => s && s.name === name); i += 1) {
    name = `${base}_mine${i}`;
  }
  studioBusy.value = "copy";
  studioErr.value = "";
  try {
    const r = await fetch(`/skills/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(studioPayload(name, `${d.display || name}（我的）`,
                                         studioBody.value, studioManifest.value)),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    studioMsg.value = `已另存为「${name}」，接下来改的是它。`;
    await loadSkillsAdmin();
    await loadStudio(name);
  } catch (e) {
    studioErr.value = "另存失败：" + e.message;
  } finally {
    studioBusy.value = "";
  }
}

async function previewStudio() {
  if (!studioDetail.value) return;
  studioBusy.value = "preview";
  studioErr.value = "";
  studioStills.value = [];
  try {
    const r = await fetch(`/style/packs/${encodeURIComponent(studioName.value)}/preview`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ manifest: studioManifest.value }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    studioStills.value = (j.stills || []).map(
      (s) => `data:image/jpeg;base64,${s.jpeg_b64}`);
    studioMsg.value = `已按「${j.template}」渲了 ${studioStills.value.length} 帧（还没保存）。`;
  } catch (e) {
    studioErr.value = "预览没渲出来：" + e.message;
  } finally {
    studioBusy.value = "";
  }
}

async function openStudio(name) {
  activePanel.value = "studio";
  await loadSkillsAdmin();
  await loadStyleTemplates();
  await loadPrompts();
  await loadStudio(name || studioName.value || ((studioStyles()[0] || {}).name || ""));
}

async function saveSkillForm() {
  const f = skillForm.value;
  if (!f || !f.name.trim()) { skillsMsg.value = "技能名必填"; return; }
  const kind = f.kind || "workflow";
  let manifest = {};
  if (kind === "style") {
    // 风格包 manifest 是**数据**：解析失败就地提示，不发出去——服务端只收 dict。
    try {
      manifest = JSON.parse(f.manifestText || "{}");
    } catch (e) {
      skillsMsg.value = "manifest 不是合法 JSON：" + e.message;
      return;
    }
    if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
      skillsMsg.value = "manifest 必须是一个 JSON 对象（{…}）";
      return;
    }
  }
  skillBusy.value = true;
  try {
    const r = await fetch(`/skills/${encodeURIComponent(f.name.trim())}`, {
      method: "PUT",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ display: f.display, always: !!f.always,
                             description: f.description, requires: f.requires,
                             body: f.body, kind,
                             style_manifest: manifest,
                             sections: splitList(f.sectionsText),
                             exclude: splitList(f.excludeText) }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    skillsMsg.value = "已保存：" + j.saved;
    await loadSkillsAdmin();
    await loadTools(true);                  // /tools 技能清单与词表同步刷新
    await openSkillModal(f.name.trim());    // 回到查看态,展示刚保存的内容
  } catch (e) {
    skillsMsg.value = "保存失败：" + e.message;
  } finally {
    skillBusy.value = false;
  }
}

async function delSkillFromModal() {
  const name = itemModal.value.name;
  await delSkill({ name });
  if (!skillsMsg.value.startsWith("失败")) itemModal.value = null;
}

// —— 执行流水沉淀为技能：后端把本会话全部已完成执行合并净化，LLM 润色（无密钥退回模板）——
async function fromConversationAsSkill() {
  if (!active.value) { alert("先选一个会话"); return; }
  try {
    await ensureIdentity();
    const r = await fetch("/skills/from_run", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: active.value }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    await loadSkillsAdmin();
    skillsMsg.value = "已沉淀：" + j.name + "（净 " + j.steps + " 步，剔 " +
      (j.dropped || 0) + " 次重复/失败" + (j.llm_polished ? "，模型润色" : "，模板") + "）";
    await openSkillModal(j.name);
    editSkillForm();
  } catch (e) {
    alert("沉淀失败：" + e.message);
  }
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
  hadToken.value = false;
  localStorage.removeItem(TOKEN_KEY);
  token.value = "";
  userId.value = "";
  username.value = "";
  identityPromise = null;
  loginView.value = true;              // 凭证没了就回登录页
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

// 「选区改」编辑器：只在成片卡带命中表时才开（图形科普片那条路的产物才有这张表）。
const motionEditor = ref(null);
function openMotionEditor(card) {
  if (!card || !card.hitmap || !card.artifactId) return;
  motionEditor.value = card;
}
function closeMotionEditor() { motionEditor.value = null; }
// 局部改出来的新版本也是一张成片卡：推进消息流，刷新后由 /convs/{id}/messages 重放接手续。
function onMotionPatched(p) {
  const cid = active.value;
  if (!cid || !p?.mediaUrl) return;
  msgs(cid).push({
    role: "assistant", kind: "media", state: "done",
    text: p.title || "局部改后的成片", url: p.mediaUrl, duration: p.duration,
    evidence: p.evidence || [], artifactId: p.artifactId, hitmap: true,
  });
  scrollDown();
}

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
      // 只有一个答案的问题不是问题：曲库标签（用户指名了那一首）与「用户已定」的取值
      // 都只有一项，再弹一页问一遍就是拿他说过的话问他。值仍然进确认帧——planCardOf
      // 已经把每一项的 default 填进 card.final，用户在链路面板上照样看得到、改得了。
      if (o.options.length === 1) continue;
      const k = `${card.chosen}:${s.seq}:${o.key}`;
      // 有多个候选但用户已经定过：预选他那个值，并把他的原话摆在标题上（免得他
      // 以为我们又拿这事问他一遍）。
      const note = o.user_says ? `（你说过「${o.user_says}」，已按此预选）` : "";
      pages.push(questionPage(
        `第 ${s.seq} 步「${s.node_display}」的 ${o.display}${note}？`,
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
    warnings: meta.warnings || [],
    storyboard: meta.storyboard || [],
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
    // 渲染确认门就是「渲染前的计划卡」：摘要、提醒与逐镜分镜都要摆在题面上，
    // 否则用户只看到一句「确认开始渲染吗」，等于闭眼批准。
    previewSummary: a.preview || null,
    warnings: Array.isArray(a.warnings) ? a.warnings : [],
    storyboard: Array.isArray(a.storyboard) ? a.storyboard : [],
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
    // 服务端把本轮风格包挂在每份候选上（见 `SubmitPlanTool`）：刷新、换设备、
    // 从执行记录重放同一张卡，都能显示它是按哪套视觉排的。
    style_skill: p.style_skill || "",
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
        // 「这一项是用户在消息里指名要的」——服务端把用户那句原话一路带到这里
        user_says: o.user_says || "",
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
    // 卡级的 style 只记「用户这一次明确选的那套」；模型在候选上自己挑的风格挂在
    // 每份候选上（三份可以各选一套），显示时按当前选中的那份取（见 planStyleOf）。
    style: src.style_skill || "",
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

// 这一版按哪套视觉排：用户明确选的那套压过候选上自带的那套。
// 候选上的风格是助手自己搜出来的（用户只说了「剪影风」这种自然语言）——
// 不显示出来，用户就无从发现它挑错了。
function planStyleOf(card) {
  if (!card) return "";
  return card.style || planChosen(card).style_skill || "";
}

// 参数在链路面板上显示的取值：就是它的默认值（= 用户确认时不用改就按这个跑）。
function planParamText(o) {
  const hit = (o.options || []).find((v) => v.value === o.default);
  return hit ? hit.label : String(o.default);
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
      // 「选区改」的开关：判据从产物里带出来（只有图形科普片那条路的终态带命中表），
      // 不由前端猜——按钮摆上去点了却 404，等于骗用户这一版能选着改。
      artifactId: p.artifact_id, hitmap: !!p.hitmap,
    });
    scrollDown();
    if (cid === active.value) {
      activePanel.value = 'timeline';
    }
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
    if (cid !== active.value) return;
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
  } else if (p.type === "style") {
    // 对话里把风格换了：服务端同时改了技能库，这里只把新名字补进本地清单（卡面上
    // 显示中文名要用它）。当前风格记在服务端会话表上，这一端不需要再存一份。
    if (p.style_skill && p.display
        && !(skillsAdmin.value || []).some((s) => s && s.name === p.style_skill)) {
      skillsAdmin.value = [...(skillsAdmin.value || []),
        { name: p.style_skill, display: p.display, kind: "style" }];
    }
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
  let text = draft.value.trim();
  const shown = text;            // 界面上显示原文；前缀只发给模型，不污染对话
  const cid = await ensureActive();
  const atts0 = atts(cid).slice();
  if (!text && !atts0.length) return;
  // 全自动：告诉模型别先给分镜。默认（preview）什么都不加——SKILL.md 里
  // 已经规定"默认先出分镜、用户确认后再出片"，所以默认这条路不需要额外指令。
  if (filmMode.value === "auto" && text) {
    text = "【全自动：不用给我看分镜，直接出片】\n" + text;
  }

  ensureSocket(cid);
  msgs(cid).push({ role: "user", text: shown, state: "done", attachments: atts0 });
  const conv = convs.value.find((c) => c.id === cid);
  if (conv && conv.title === "新对话") {
    conv.title = (shown || atts0[0].filename).slice(0, 18);
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
        // 只有一个入口：一句话进来，技能和节点由助手自己找。不传 mode 以外的
        // 任何「选择」——风格、是否直出都由服务端按这句话自己判断。
        mode: "auto",
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
  // 出图链路拿掉后，「检索配图 / 选用配图」这两个工具在后端词表里也不再存在，
  // 标签留在兜底映射里，只会让不存在的名字看着像还在。
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

// ---- 一轮里的工具卡片折成一行 ----
// 模型一「用起工具」就是连着好几张卡片、每张一个「助」印记，把对话压得看不见——
// 用户问的「调了一堆工具还占满会话窗口」在界面上就是这件事。这里把**连续**的工具
// 卡片折成一行摘要（默认收起，点开还是原来那些卡片）。
// 只在展示层做：msgs() 那份数据一个字段都不动（回投帧的配对是按 call_id 在原始数组上
// 找 running 那张卡片，改写数组会让配对错位）。
// 哪怕只有 1 张也折。原先卡在「少于 3 张不折（折了比展开还长）」，那是照「一轮十来次
// 调用」的形状定的；真机那条 14 镜的 run 一轮的实际调用次数是
// 1,2,1,3,3,3,3,2,2,2,1,2,2,2,2,2,1,1,2,2,1,1,1,…——39 轮里 22 轮只有 1 次、13 轮 2 次，
// 够得上 3 张的只有 4 轮：整场几乎一张都没折上，用户看到的还是满屏卡片（他问的就是
// 这个）。一张卡片是「标题＋参数＋结果」三五行，折起来就是一行，折了确实更短。
const TOOL_GROUP_MIN = 1;
const toolGroupsOpen = ref({});     // { 组键: true }

function toolGroupKey(list, first) {
  return `${first.call_id || first.tool}#${list.indexOf(first)}`;
}

// v-for 的 :key 必须落在**原始**下标上，不能落在视图下标上：展开一组会在它后面
// 插进若干项，视图下标全体后移 → 后面每条消息都被当成新节点重建，而其中可能有
// `<video>`（重建 = 播放从头开始）。原始数组不随展开变化，所以它才是稳的那个。
const _rawIdx = new WeakMap();

function rawKey(m, i) {
  const ri = _rawIdx.get(m);
  return ri === undefined ? "v" + i : "r" + ri;
}

function toggleToolGroup(key) {
  toolGroupsOpen.value = { ...toolGroupsOpen.value, [key]: !toolGroupsOpen.value[key] };
}

function toolGroupSummary(items) {
  const counts = new Map();
  for (const m of items) {
    const label = toolLabel(m.tool, m.tool_display);
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  return [...counts.entries()].map(([k, n]) => (n > 1 ? `${k} ×${n}` : k)).join("、");
}

function viewMsgs(cid) {
  const list = msgs(cid);
  const out = [];
  let run = [];
  const flush = () => {
    if (!run.length) return;
    if (run.length >= TOOL_GROUP_MIN) {
      const key = toolGroupKey(list, run[0]);
      const open = !!toolGroupsOpen.value[key];
      out.push({
        role: "assistant", kind: "toolgroup", key, items: run.slice(), open,
        running: run.some((m) => m.state === "running"),
        failed: run.filter((m) => m.error).length,
      });
      // 展开时把原来的卡片原样排在后面——不复制一份卡片样式，展开的与单张的是同一段
      // 模版（复制一份迟早两边长得不一样）。
      if (open) out.push(...run);
    } else {
      out.push(...run);
    }
    run = [];
  };
  for (let i = 0; i < list.length; i++) {
    const m = list[i];
    _rawIdx.set(m, i);
    if (m.kind === "tool" && m.role === "assistant") run.push(m);
    else { flush(); out.push(m); }
  }
  flush();
  return out;
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

// ---- 模型设置：双模型(主模型/判断模型)都由用户在页面上配，存进 PG 热读，不重启 ----

const settings = ref(null);
const keyDraft = ref("");

const testingKey = ref(false);
const testResult = ref(null);
const keyError = ref("");
// 主模型覆盖(model/base_url,空 = 恢复默认 DeepSeek)
const modelDraft = ref({ model: "", base_url: "" });

// 判断模型(Jev 类)三件套
const judgeDraft = ref({ base_url: "", model: "", api_key: "" });
const savingJudge = ref(false);
const savingLLM = ref(false);
const testingJudge = ref(false);
const testJudgeResult = ref(null);

// 配额(按身份覆盖 env 与启动参数；limit 填 "0" = 明确关拦截，留空 = 交回下层)
const quotaDraft = ref({ limit: "", window: "" });
const savingQuota = ref(false);
// 联网检索后端：provider + 密钥/自建地址，全空 = 免 key 回落链
const searchDraft = ref({ provider: "", base_url: "", api_key: "" });
const savingSearch = ref(false);
const testingSearch = ref(false);
const testSearchResult = ref(null);
// 图像出图后端的那组草稿与自检状态（provider / 密钥 / 模型 / 尺寸、保存中 / 自检中 / 自检结果）
// 随面板一并删除：成片每一镜由 LLM 写 TSX 现画，界面上已没有任何图源可配。

// 记忆（memories 表按身份摊开）：只由 Agent 的工具读写的话，用户看不见也改不掉。
// 这三态对应「读到了什么 / 编辑中的草稿（按 category 存）/ 正在保存哪个」。
// 草稿按类存：两个文本域各自编辑，保存一个不该动另一个。
const memories = ref(null);
const memoryDraft = ref({});
const memorySaving = ref("");
const memoryMsg = ref("");
const memoryCompacting = ref("");   // 正在整理哪个分类（"*" = 全部）
const memoryReport = ref(null);     // 最近一次整理的判决（null = 还没整理过）

async function loadSettings() {
  await ensureIdentity();
  const r = await fetch("/settings", { headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
  settings.value = j;
  modelDraft.value = {
    model: j.main_override?.model || j.model || "",
    base_url: j.main_override?.base_url || j.base_url || "",
  };
  judgeDraft.value = {
    base_url: j.judge?.base_url || "",
    model: j.judge?.model || "",
    api_key: "",
  };
  // 配额：留空 = 这一项不动（不是清除），所以每次打开都从空白开始写
  quotaDraft.value = { limit: "", window: "" };
  searchDraft.value = {
    provider: j.search?.provider || j.search?.effective_provider || "",
    base_url: j.search?.base_url || "",
    api_key: "",
  };
  testSearchResult.value = null;
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
  // 记忆与 MCP 各有自己的端点，都单独拉一次；失败只写进各自区块的回话，
  // 不占 keyError——记忆读不到不该让整页设置看起来是坏的。
  memoryMsg.value = "";
  await loadMemories();
  // 提示词清单一同拉一次：它是「哪句话在生效」的答案，与记忆一样属于
  // 「此前只有代码里能看到」的东西；失败同样只写进自己区块的回话。
  promptOpen.value = "";
  await loadPrompts();
  mcpSetMsg.value = "";
  await loadMcpAdmin();
}

// ---- 记忆：查看 / 编辑 / 删除（后端 /memories，与 Agent 的 update_memory 同一张表）----

// 视图回写：正文与草稿一起换。草稿初值 = 库里的现值——用户改了草稿、没保存就刷新时，
// 以库里为准，这正是「保存」这个动作的意义。读/存/删/整理四处都走它，
// 少走一处就会出现「库变了、输入框还是旧的」那种最难发现的假象。
function applyMemoryView(j) {
  memories.value = j;
  const draft = {};
  for (const row of j.categories || []) draft[row.category] = row.content || "";
  memoryDraft.value = draft;
}

async function loadMemories() {
  try {
    await ensureIdentity();
    const r = await fetch("/memories", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    applyMemoryView(j);
  } catch (e) {
    memoryMsg.value = "读不到记忆：" + e.message;
  }
}

async function saveMemory(category) {
  if (memorySaving.value) return;
  memorySaving.value = category;
  memoryMsg.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/memories/${encodeURIComponent(category)}`, {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ content: memoryDraft.value[category] || "" }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    applyMemoryView(j);
    memoryMsg.value = j.status === "cleared" ? "已清空这一类。" : "已保存。";
  } catch (e) {
    memoryMsg.value = "保存失败：" + e.message;
  } finally {
    memorySaving.value = "";
  }
}

async function deleteMemory(category) {
  if (memorySaving.value) return;
  memorySaving.value = category;
  memoryMsg.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/memories/${encodeURIComponent(category)}`, {
      method: "DELETE", headers: authHeaders(),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    applyMemoryView(j);
    memoryMsg.value = "已删除这一类记忆。";
  } catch (e) {
    memoryMsg.value = "删除失败：" + e.message;
  } finally {
    memorySaving.value = "";
  }
}

// 整理记忆：去重 / 合并被包含的条目 / 清掉低质行 / 超上限丢最早。
// 为什么要有这个按钮：写侧过线时会**自动**整理（追加时触发），但设置页上这块是人手
// 编辑的，人手编辑不走自动整理——用户存到很大时得有一个显式入口，而不是靠制造一次
// 追加去触发放线。后端把「丢了哪几条、原文是什么」逐条回给这里，页面照原样念出来。
async function compactMemory(category) {
  if (memoryCompacting.value) return;
  memoryCompacting.value = category || "*";
  memoryMsg.value = "";
  memoryReport.value = null;
  try {
    await ensureIdentity();
    const q = category ? "?category=" + encodeURIComponent(category) : "";
    const r = await fetch("/memories/compact" + q, {
      method: "POST", headers: authHeaders(),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    applyMemoryView(j);
    memoryReport.value = j.reports || null;
    const touched = (j.reports || []).filter((x) => x.changed);
    memoryMsg.value = touched.length
      ? "已整理 " + touched.length + " 类记忆。"
      : "查过一遍，没有可整理的内容。";
  } catch (e) {
    memoryMsg.value = "整理失败：" + e.message;
  } finally {
    memoryCompacting.value = "";
  }
}

// ---- 提示词一览（后端 /prompts，读 prompts/manifest.json + 磁盘）----
// 只读，**不给编辑**：这些文件随代码分发（Dockerfile COPY 进镜像），在页面上改完
// 落不进镜像、下次部署就被覆盖——「改了不生效」比「不让改」更难查。要看正文是因为
// 「哪句话在生效」此前只散在 8 个 .py 与 prompts/ 目录里，用户只能去翻仓库。
const prompts = ref(null);
const promptsMsg = ref("");
const promptOpen = ref("");       // 正在展开正文的那一份（空 = 都收起）
const promptOpenText = ref("");   // 该份的正文
const promptOpenErr = ref("");

async function loadPrompts() {
  promptsMsg.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/prompts", { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    prompts.value = j;
  } catch (e) {
    promptsMsg.value = "读不到提示词清单：" + e.message;
  }
}

async function togglePrompt(name) {
  if (promptOpen.value === name) {
    promptOpen.value = "";
    return;
  }
  promptOpen.value = name;
  promptOpenText.value = "";
  promptOpenErr.value = "";
  try {
    await ensureIdentity();
    const r = await fetch(`/prompts/${encodeURIComponent(name)}`, { headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    if (promptOpen.value !== name) return;  // 展开期间又点了别的，这份结果作废
    promptOpenText.value = j.text || "";
  } catch (e) {
    promptOpenErr.value = "读不到正文：" + e.message;
  }
}

async function saveLLM() {
  if (savingLLM.value) return;
  savingLLM.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    if (keyDraft.value.trim()) {
      const r1 = await fetch("/settings/api-key", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ api_key: keyDraft.value.trim() }),
      });
      const j1 = await r1.json().catch(() => ({}));
      if (!r1.ok) throw new Error(j1.detail || ("HTTP " + r1.status));
      settings.value = j1;
      keyDraft.value = "";
    }
    const r2 = await fetch("/settings/model", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ model: modelDraft.value.model.trim(),
                             base_url: modelDraft.value.base_url.trim() }),
    });
    const j2 = await r2.json().catch(() => ({}));
    if (!r2.ok) throw new Error(j2.detail || ("HTTP " + r2.status));
    settings.value = j2;
    testResult.value = null;
  } catch (e) {
    keyError.value = "保存失败：" + e.message;
  } finally {
    savingLLM.value = false;
  }
}

async function clearKey() {
  if (savingLLM.value) return;
  savingLLM.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/settings/api-key", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ api_key: "" }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    settings.value = j;
    keyDraft.value = "";
    testResult.value = null;
  } catch (e) {
    keyError.value = "清除失败：" + e.message;
  } finally {
    savingLLM.value = false;
  }
}

async function saveJudge() {
  if (savingJudge.value) return;
  keyError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/settings/judge", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ base_url: judgeDraft.value.base_url.trim(),
                             model: judgeDraft.value.model.trim(),
                             api_key: judgeDraft.value.api_key }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    judgeDraft.value.api_key = "";
    testResult.value = null;
    await loadSettings();
  } catch (e) {
    keyError.value = "判断模型保存失败：" + e.message;
  }
}

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

async function testJudgeConn() {
  if (testingJudge.value) return;
  testingJudge.value = true;
  keyError.value = "";
  testJudgeResult.value = null;
  try {
    await ensureIdentity();
    const r = await fetch("/settings/judge/test", { method: "POST", headers: authHeaders() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    testJudgeResult.value = j;
  } catch (e) {
    keyError.value = "Jev 自检没跑成：" + e.message;
  } finally {
    testingJudge.value = false;
  }
}

async function saveQuota() {
  if (savingQuota.value) return;
  savingQuota.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    const limit = quotaDraft.value.limit.trim();
    const window = quotaDraft.value.window.trim();
    // 没写的字段发 null = 不动；写成空串发过去 = 清掉页面覆盖交回下层。两者是两件事。
    const r = await fetch("/settings/quota", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        limit: limit === "" ? null : limit,
        window: window === "" ? null : window,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    settings.value = j;
    quotaDraft.value = { limit: "", window: "" };
  } catch (e) {
    keyError.value = "配额保存失败：" + e.message;
  } finally {
    savingQuota.value = false;
  }
}

async function refreshQuota() {
  if (savingQuota.value) return;
  keyError.value = "";
  try {
    await loadSettings();
  } catch (e) {
    keyError.value = "读不到配额账：" + e.message;
  }
}

async function clearQuotaOverride() {
  if (savingQuota.value) return;
  savingQuota.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    // 全空串 = 后端 drop 页面层，回落到环境变量与启动参数（发 null 是「不动」，两回事）
    const r = await fetch("/settings/quota", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ limit: "", window: "" }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    settings.value = j;
    quotaDraft.value = { limit: "", window: "" };
  } catch (e) {
    keyError.value = "清除页面覆盖失败：" + e.message;
  } finally {
    savingQuota.value = false;
  }
}

async function clearSearchOverride() {
  if (savingSearch.value) return;
  savingSearch.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/settings/search", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ provider: "", base_url: "", api_key: "" }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    testSearchResult.value = null;
    await loadSettings();
  } catch (e) {
    keyError.value = "清除检索后端失败：" + e.message;
  } finally {
    savingSearch.value = false;
  }
}

async function saveSearch() {
  if (savingSearch.value) return;
  savingSearch.value = true;
  keyError.value = "";
  try {
    await ensureIdentity();
    const r = await fetch("/settings/search", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        provider: searchDraft.value.provider,
        base_url: searchDraft.value.base_url.trim(),
        api_key: searchDraft.value.api_key.trim(),
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    testSearchResult.value = null;
    await loadSettings();
  } catch (e) {
    keyError.value = "检索后端保存失败：" + e.message;
  } finally {
    savingSearch.value = false;
  }
}

async function testSearchConn() {
  if (testingSearch.value) return;
  testingSearch.value = true;
  keyError.value = "";
  testSearchResult.value = null;
  try {
    await ensureIdentity();
    const r = await fetch("/settings/search/test", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ query: "" }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    testSearchResult.value = j;
  } catch (e) {
    keyError.value = "检索自检没跑成：" + e.message;
  } finally {
    testingSearch.value = false;
  }
}

// 出图后端那三个处理器（保存 / 清除页面覆盖 / 自检）随面板删除：
// /settings/image 与 /settings/image/test 两条路由后端已不再提供，前端留着只会打空。

function newConv() {
  const c = freshConv();
  convs.value.unshift(c);
  histories[c.id] = [];
  parkedCards.value = [];
  questionCard.value = null;
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
  // 风格包要**一进来就拉**：入口那个下拉只列 `kind=style`，等用户打开工具库
  // 才加载的话，第一次进页面根本看不到「剪影年轮」。
  ensureIdentity().catch(() => {}).then(loadSkillsAdmin);
});
onBeforeUnmount(() => Object.values(socks).forEach((s) => s.close && s.close()));
</script>

<template>
  <!-- 登录/注册门禁:登录成功才进入主界面 -->
  <div v-if="loginView" class="login-mask">
    <div class="login-card">
      <div class="seal login-seal"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.24 12.24a6 6 0 0 0-8.49-8.49L5 10.5V19h8.5z"/><line x1="16" y1="8" x2="2" y2="22"/><line x1="17.5" y1="15" x2="9" y2="15"/></svg></div>
      <h1>智能创作助手</h1>
      <p class="login-sub">一句话驱动「查资料 → 写文案 → 剪视频 → 出片」</p>
      <input v-model.trim="loginForm.username" placeholder="用户名（中英文/数字/_ -）"
             autocomplete="username" @keyup.enter="submitAuth" />
      <input v-model="loginForm.password" type="password" placeholder="密码（至少 6 位）"
             autocomplete="current-password" @keyup.enter="submitAuth" />
      <div v-if="loginErr" class="login-err">{{ loginErr }}</div>
      <button class="login-btn" :disabled="loginBusy || !loginForm.username
              || loginForm.password.length < 6" @click="submitAuth">
        {{ loginBusy ? "…" : (loginMode === "login" ? "登 录" : "注 册 并 登 录") }}
      </button>
      <div class="login-switch">
        {{ loginMode === "login" ? "还没有账号？" : "已有账号？" }}
        <a href="#" @click.prevent="switchLoginMode">
          {{ loginMode === "login" ? "注册新账号" : "去登录" }}
        </a>
      </div>
    </div>
  </div>

  <div class="shell" v-else>
    <!-- 左侧：卷宗栏（可收起） -->
    <aside class="rail" :class="{ closed: !railOpen }">
      <button v-if="!railOpen" class="rail-open-btn" title="展开卷宗栏"
              @click="toggleRail">»</button>
      <div class="brand">
        <div class="seal"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.24 12.24a6 6 0 0 0-8.49-8.49L5 10.5V19h8.5z"/><line x1="16" y1="8" x2="2" y2="22"/><line x1="17.5" y1="15" x2="9" y2="15"/></svg></div>
        <div class="brand-text">
          <b>智能创作助手</b>
          <span>纸上创作台</span>
        </div>
        <button class="rail-toggle" title="收起卷宗栏" @click="toggleRail">«</button>
      </div>

      <button class="new" @click="newConv"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span class="lbl"> 开一册新卷</span></button>

      <div class="rail-btns">
        <button class="help" :class="{ on: activePanel === 'library' }" @click="toggleLibrary"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg> 素材库</button>
        <button class="help" :class="{ on: activePanel === 'bgm' }" @click="toggleBgm"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg> 音乐库</button>
        <button class="help" :class="{ on: activePanel === 'textlib' }" @click="toggleTextLib"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><path d="M4 7V4h16v3"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="7" x2="12" y2="20"/></svg> 文字库</button>
        <button class="help" :class="{ on: activePanel === 'tools' }" @click="toggleHelp"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg> 工具库</button>
      </div>

      <div class="conv-section-label">会话历史</div>
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

      <div class="rail-user-bar">
        <span class="rail-username">{{ username || userId || "身份签发中…" }}</span>
        <button class="rail-icon-btn" :class="{ on: activePanel === 'settings' }" @click="toggleSettings" title="设置"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
        <button class="rail-icon-btn" @click="forgetToken" title="登出"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg></button>
      </div>
    </aside>

    <!-- 右侧：稿纸区 -->
    <main class="desk">
      <header class="head">
        <span class="rule"></span>
        <h2>{{ convs.find((c) => c.id === active)?.title || "—" }}</h2>
        <button class="help" :class="{ on: activePanel === 'timeline' }" @click="toggleTimeline">时间线</button>
        <button class="help" :class="{ on: activePanel === 'runs' }" @click="toggleRuns">执行记录</button>
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

        <div v-for="(m, i) in viewMsgs(active)"
             :key="m.kind === 'toolgroup' ? 'g' + m.key : rawKey(m, i)"
             class="row" :class="m.role">
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
                  <!-- 开关来自产物自己带的命中表：没有表就只能整片重做，按钮摆上去点了也是 404 -->
                  <button v-if="m.hitmap && m.artifactId" class="clip mc-patch"
                          title="在画面上框住要改的那块，只重烧受影响的那几镜"
                          @click="openMotionEditor(m)">✂ 选区改</button>
                  <span v-else class="mc-nopatch">这一版不能选着改（出片时没量到命中表）</span>
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
            <template v-else-if="m.kind === 'toolgroup'">
              <!-- 一轮里连着好几张工具卡片，折成这一行（默认收起）。点开把原卡片
                   原样接在后面（见 viewMsgs），所以展开的与单张的用的是同一段模版。 -->
              <div class="tool-group" :class="{ open: m.open }">
                <button class="tg-head" @click="toggleToolGroup(m.key)">
                  <span class="tt-ico">🔧</span>
                  <b>本轮 {{ m.items.length }} 次工具调用</b>
                  <span class="tg-list">{{ toolGroupSummary(m.items) }}</span>
                  <span v-if="m.running" class="tt-spin">执行中…</span>
                  <span v-else-if="m.failed" class="tg-fail">✗ {{ m.failed }} 次失败</span>
                  <span class="tg-toggle">{{ m.open ? "收起 ▴" : "展开 ▾" }}</span>
                </button>
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
                <span v-if="planStyleOf(m)" class="pi-tag style">风格 · {{ styleLabel(planStyleOf(m)) }}</span>
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
              <!-- 死在半路的那条 run（见 offerResumeIfNewestFailed）：与执行记录面板里
                   那颗按钮同一段 `doResume`，只是把入口摆到用户真正会看的地方。 -->
              <div v-if="m.resume" class="ru-actions">
                <button class="clip ghost" :disabled="forking" @click="resumeFromBubble(m)">
                  {{ forking ? "提交中…" : "从最新一致点续跑" }}
                </button>
              </div>
            </template>
          </div>
        </div>

        <div v-if="questionCard" class="row assistant">
          <div class="mark" aria-hidden="true"><span class="seal-mini">助</span></div>
          <div class="bubble qc-inline">
            <div class="pm-head qc-head">
              <b class="qc-step">第 {{ questionCard.page + 1 }} / {{ questionCard.pages.length }} 题</b>
              <div class="qc-pager">
                <button class="qc-pg" :disabled="questionCard.page <= 0"
                        title="上一题" @click="qPrev">‹</button>
                <span class="qc-pgn">{{ questionCard.page + 1 }}/{{ questionCard.pages.length }}</span>
                <button class="qc-pg" :disabled="qLastPage()"
                        title="下一题" @click="qNext">›</button>
              </div>
              <button class="pm-x" :title="'收起（选择仍会保留，随时可再打开）'"
                      @click="qClose">×</button>
            </div>
            <p class="qc-title">{{ qCurrentPage().title }}</p>
            <p v-if="questionCard.reason && questionCard.reason !== qCurrentPage().title"
               class="ap-reason">{{ questionCard.reason }}</p>
            <div v-if="questionCard.previewSummary" class="qc-preview">
              <span v-for="(v, k) in questionCard.previewSummary" :key="k" class="qc-pv">
                <em>{{ k }}</em>{{ v }}
              </span>
            </div>
            <div v-if="(questionCard.warnings || []).length" class="pc-warn">
              <div v-for="(w, i) in questionCard.warnings" :key="i">⚠ {{ w }}</div>
            </div>
            <div v-if="(questionCard.storyboard || []).some((sb) => sb.thumb)" class="qc-thumbs">
              <figure v-for="sb in questionCard.storyboard" :key="sb.id" class="qc-thumb">
                <img v-if="sb.thumb" :src="sb.thumb" :alt="sb.id" loading="lazy"
                     @click="openThumb(sb.thumb)" />
                <div v-else class="qc-thumb-none">无预览</div>
                <figcaption><b>{{ sb.id }}</b> {{ sb.text || "（无字幕）" }}</figcaption>
              </figure>
            </div>
            <ol v-else-if="(questionCard.storyboard || []).length" class="qc-board">
              <li v-for="sb in questionCard.storyboard" :key="sb.id">
                <b>{{ sb.id }}</b>
                <span class="qc-board-text">{{ sb.text || "（无字幕）" }}</span>
                <em v-if="sb.note">{{ sb.note }}</em>
              </li>
            </ol>
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
        <span v-if="planStyleOf(planModal)" class="pb-style">风格 · {{ styleLabel(planStyleOf(planModal)) }}</span>
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
        <!-- 按钮单独占一行，放在输入框上方：输入框可以整行铺开，长句不用在按钮中间挤着换行。 -->
        <div class="composer-bar">
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
        </div>

        <div class="composer-row">
          <!-- 出片模式：贴在输入框**左侧**、与它底部对齐（`.composer-row` 是 flex-end）。
               默认「先看分镜」——一次成片十几分钟起烧，先看一眼讲法；选「全自动」才跳过预览。
               **不用原生 `<select>`**：它的选项列表由系统画，那一片蓝色高亮没法用 CSS 改。 -->
          <div class="film-mode" ref="fmEl" :class="{ open: filmOpen }"
               :title="filmMode === 'auto'
                 ? '跳过预览，直接烧渲染（十几分钟）'
                 : '先出分镜给你看，确认后再出片'"
               @click.stop="filmOpen = !filmOpen">
            <span>{{ filmMode === "auto" ? "全自动" : "先看分镜" }}</span>
            <i class="fm-caret"></i>
            <ul v-if="filmOpen" class="film-menu">
              <li :class="{ on: filmMode === 'preview' }"
                  @click.stop="pickFilm('preview')">先看分镜</li>
              <li :class="{ on: filmMode === 'auto' }"
                  @click.stop="pickFilm('auto')">全自动</li>
            </ul>
          </div>
          <textarea
            ref="draftEl"
            v-model="draft"
            rows="1"
            placeholder="把想法落在此处，Enter 落笔，Shift+Enter 换行；素材点「＋素材」/拖入或「＋链接」贴网址，会挂在输入框上方随消息发出…"
            @keydown="onKey"
          ></textarea>
          <button class="send" :class="{ off: !draft.trim() && !atts(active).length }" @click="send">落笔 ➤</button>
        </div>
      </footer>
    </main>

    <!-- 右侧抽屉：工具库 / 素材库 / 音乐库 / 文字库 / 时间线 / 执行记录 / 设置（互斥，同时只开一个） -->
    <aside v-if="activePanel" class="drawer" :style="{ width: drawerW + 'px' }">
      <div class="drawer-grip" title="拖拽调宽" @mousedown="startResize"></div>
      <header class="drawer-head">
        <b>{{ activePanel === 'plan' ? '剪辑链路' : activePanel === 'tools' ? '工具库' : activePanel === 'library' ? '素材库' : activePanel === 'bgm' ? '音乐库' : activePanel === 'textlib' ? '文字库' : activePanel === 'timeline' ? '时间线编辑器' : activePanel === 'runs' ? '执行记录 · 时间旅行' : '设置' }}</b>
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
              <!-- 每版各用哪套视觉都要看得见：助手自己挑的也要亮出来，
                   否则用户只能等出片才发现挑错了。 -->
              <i v-if="p.style_skill" class="pp-ver-style">· {{ styleLabel(p.style_skill) }}</i>
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
              <!-- 这一步按哪些取值跑，摆在这里：只有一个候选值的参数不再单独弹一页问，
                   所以这里是用户唯一能看见它们的地方（凡是会影响成片的取值都得露出来）。 -->
              <div v-if="(s.params || []).length" class="pp-params">
                <span v-for="o in s.params" :key="o.key"
                      class="pp-param" :class="{ pinned: !!o.user_says }">
                  <em>{{ o.user_says ? "已定" : "可选" }}</em>
                  {{ o.display }}：{{ planParamText(o) }}
                  <i v-if="o.user_says">按你说的「{{ o.user_says }}」</i>
                </span>
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

      <!-- 整个工具库面板都是拖放区：技能以文件夹的形式存在，拖进来就该装上。 -->
      <div v-if="activePanel === 'tools'" class="tool-drawer" :class="{ 'drop-on': skillsDragOver }"
           @dragenter.prevent="skillsDragOver = true"
           @dragover.prevent="skillsDragOver = true"
           @dragleave.prevent="onToolDrawerDragLeave($event)"
           @drop.prevent="dropSkillFolder($event)">
        <input type="file" id="skill-zip" accept=".zip" style="display:none"
               @change="uploadSkillZip" />
        <input type="file" id="skill-folder" webkitdirectory directory multiple
               style="display:none" @change="importSkillFolderFromInput" />
        <div v-if="loadingTools" class="lib-empty">正在加载工具库…</div>
        <div v-else-if="toolLib && toolLib.error" class="lib-empty bad">工具库加载失败：{{ toolLib.error }}</div>
        <div v-else-if="toolLib" class="td-detail">
            <template v-if="!toolView">
              <!-- 根目录：三类各是一个文件夹，点进去才是它里面的东西。 -->
              <div class="tree-note">
                工具库按文件夹组织：一个技能就是一个文件夹（<code>SKILL.md</code> 是入口），
                工具按能力分类，MCP 服务下挂它提供的工具。把技能文件夹拖进这块面板即可安装；
                拖一个装着多个技能文件夹的父文件夹，就一次装好几个。
              </div>
              <div class="tree-row depth-0" @click="toolView = 'tools'">
                <span class="tree-caret">▸</span>
                <b class="tree-name">工具</b>
                <span class="tree-count">{{ toolCount }} 个 · {{ toolFolders.length }} 个分类</span>
              </div>
              <div class="tree-row depth-0" @click="toolView = 'skills'">
                <span class="tree-caret">▸</span>
                <b class="tree-name">技能</b>
                <span class="tree-count">{{ skillsAdmin.length }} 个包</span>
              </div>
              <div class="tree-row depth-0" @click="toolView = 'mcp'">
                <span class="tree-caret">▸</span>
                <b class="tree-name">MCP 服务</b>
                <span class="tree-count">{{ mcpServers.length }} 个</span>
              </div>
              <div class="skill-drop" :class="{ over: skillsDragOver }">
                {{ skillsDragOver ? "松开即安装这个文件夹" : "把技能文件夹拖到这里（或拖进本面板任意位置）" }}
              </div>
              <div v-if="skillsMsg" class="lib-empty">{{ skillsMsg }}</div>
            </template>
            <template v-else-if="toolView === 'tools'">
              <!-- 分类这一层：只列分类，不把里面的工具一起摊开。 -->
              <template v-if="!toolCat">
                <div class="tree-row" @click="toolView = null">
                  <span class="tree-caret">←</span>
                  <span class="tree-name">返回</span>
                  <span class="tree-count">工具 · {{ toolFolders.length }} 个分类</span>
                </div>
                <div v-for="f in toolFolders" :key="'tf-' + f.cat" class="tree-row"
                     @click="toolCat = f.cat">
                  <span class="tree-caret">▸</span>
                  <b class="tree-name">{{ f.cat }}</b>
                  <span class="tree-count">{{ f.items.length }} 个</span>
                </div>
                <div v-if="!toolFolders.length" class="lib-empty">还没有注册工具。</div>
              </template>
              <!-- 工具这一层：只有这个分类里的工具。 -->
              <template v-else-if="toolCatFolder">
                <div class="tree-row" @click="toolCat = ''">
                  <span class="tree-caret">←</span>
                  <span class="tree-name">返回</span>
                  <span class="tree-count">{{ toolCatFolder.cat }} · {{ toolCatFolder.items.length }} 个</span>
                </div>
                <div v-for="t in toolCatFolder.items" :key="'tf-' + toolCatFolder.cat + t.name"
                     class="tree-row file" @click="openToolModal(t, toolCatFolder.cat)">
                  <span class="tree-caret">·</span>
                  <b class="tree-name">{{ toolLabel(t.name, t.name_display) }}</b>
                  <span class="tree-sub">{{ t.desc }}</span>
                </div>
              </template>
            </template>
            <template v-else-if="toolView === 'skills'">
              <div class="td-row-btns" style="margin-bottom:6px">
                <button class="help" @click="pickSkillFolder">导入文件夹</button>
                <button class="help" @click="pickSkillZip">上传 .zip</button>
                <button class="help" @click="reloadSkillsDir">重扫导入目录</button>
              </div>
              <div v-if="skillsMsg" class="lib-empty">{{ skillsMsg }}</div>

              <!-- 第一层：类型文件夹（流程 / 能力 / 风格包）。 -->
              <template v-if="!skillKind">
                <div class="tree-row" @click="toolView = null">
                  <span class="tree-caret">←</span>
                  <span class="tree-name">返回</span>
                  <span class="tree-count">技能 · {{ skillsAdmin.length }} 个包</span>
                </div>
                <div v-for="f in skillFolders" :key="'skf-' + f.key" class="tree-row"
                     @click="skillKind = f.key">
                  <span class="tree-caret">▸</span>
                  <b class="tree-name">{{ f.label }}</b>
                  <span class="tree-count">{{ f.count }} 个</span>
                </div>
                <div v-if="!skillFolders.length && !loadingSkillsAdmin" class="lib-empty">
                  还没有技能。把技能文件夹拖进这块面板即可安装。
                </div>
              </template>

              <!-- 第二层：这一类的技能包。能删的只留一个删除按钮，内置的只标「内置」。 -->
              <template v-else-if="!skillOpened">
                <div class="tree-row" @click="skillKind = ''">
                  <span class="tree-caret">←</span>
                  <span class="tree-name">返回</span>
                  <span class="tree-count">{{ skillKindLabel(skillKind) }} · {{ skillListOfKind.length }} 个</span>
                </div>
                <div v-for="s in skillListOfKind" :key="'sk-' + s.name" class="tree-row"
                     @click="skillOpened = s.name">
                  <span class="tree-caret">▸</span>
                  <b class="tree-name">{{ s.display || s.name }}</b>
                  <span class="tree-sub">{{ s.name }}</span>
                  <span v-if="s.available === false" class="st-bad"
                        :title="s.unavailable_reason || '依赖未满足'">⚠</span>
                  <span v-if="s.system" class="tree-count">内置</span>
                  <span class="tree-actions" @click.stop>
                    <button v-if="!s.system" class="help danger" @click="delSkill(s)">删除</button>
                  </span>
                </div>
                <div v-if="!skillListOfKind.length" class="lib-empty">这一类还没有技能。</div>
              </template>

              <!-- 第三层：这个包里的文件。 -->
              <template v-else-if="openedSkill">
                <div class="tree-row" @click="skillOpened = ''">
                  <span class="tree-caret">←</span>
                  <span class="tree-name">返回</span>
                  <span class="tree-count">{{ openedSkill.display || openedSkill.name }}</span>
                </div>
                <div class="tree-row file" @click="openSkillFile(openedSkill.name, 'SKILL.md')">
                  <span class="tree-caret">·</span>
                  <b class="tree-name">SKILL.md</b>
                  <span class="tree-sub">入口 · 点开看正文 / 编辑</span>
                </div>
                <div v-for="r in openedSkillRows" :key="openedSkill.name + ':' + r.path"
                     class="tree-row" :class="{ file: r.kind !== 'dir' }"
                     :style="{ paddingLeft: (8 + r.depth * 16) + 'px' }"
                     @click="r.kind === 'dir' ? treeToggle(skillDirOpen, r.key) : openSkillFile(openedSkill.name, r.path)">
                  <span class="tree-caret">{{ r.kind === "dir" ? (r.open ? "▾" : "▸") : "·" }}</span>
                  <b v-if="r.kind === 'dir'" class="tree-name">{{ r.name }}</b>
                  <span v-else class="tree-name">{{ r.name }}</span>
                </div>
                <div v-if="!openedSkillRows.length" class="tree-row file" style="opacity:.55">
                  <span class="tree-caret">·</span>
                  <span class="tree-name">（这个包只有正文，没有其他文件）</span>
                </div>
              </template>
            </template>
            <template v-else-if="toolView === 'mcp'">
              <!-- 服务这一层：只列服务；点进去才是这一台服务的配置与工具。 -->
              <template v-if="!mcpPicked">
                <div class="td-row-btns" style="margin-bottom:6px">
                  <button class="help" @click="toolView = null">← 返回</button>
                  <button class="help" @click="openMcpModal(null)">＋ 新增</button>
                </div>
                <div v-if="mcpMsg" class="lib-empty">{{ mcpMsg }}</div>
                <div v-if="loadingMcp" class="lib-empty">加载中…</div>
                <div v-for="s in mcpServers" :key="'m-' + s.name" class="tree-row"
                     @click="mcpPicked = s.name">
                  <span class="tree-caret">▸</span>
                  <b class="tree-name">{{ s.name }}</b>
                  <span class="tree-sub" :class="s.live ? 'st-ok' : 'st-bad'">{{ s.live ? "● 在线" : (s.enabled ? "○ 已启用未连" : "○ 停用") }}</span>
                  <span class="tree-count">{{ (s.tools || []).length }} 个工具</span>
                </div>
                <div v-if="!loadingMcp && !mcpServers.length" class="lib-empty">
                  还没有动态 MCP 服务（mcp.json 里的静态服务不在此列）。
                </div>
              </template>
              <!-- 服务里面：配置 + 它提供的工具，动作只在这里出现。 -->
              <template v-else-if="mcpPickedServer">
                <div class="tree-row" @click="mcpPicked = ''">
                  <span class="tree-caret">←</span>
                  <span class="tree-name">返回</span>
                  <span class="tree-count">{{ mcpPickedServer.name }}</span>
                </div>
                <div class="td-row-btns" style="margin-bottom:6px">
                  <button class="help" :disabled="mcpBusy" @click="toggleMcp(mcpPickedServer)">
                    {{ mcpPickedServer.live ? "断开" : "启用" }}
                  </button>
                  <button class="help" @click="openMcpModal(mcpPickedServer)">编辑</button>
                  <button class="help danger" @click="delMcp(mcpPickedServer)">删除</button>
                </div>
                <div class="tree-row file">
                  <span class="tree-caret">·</span>
                  <span class="tree-name">配置：{{ (mcpPickedServer.config && (mcpPickedServer.config.url || mcpPickedServer.config.command)) || "—" }}</span>
                </div>
                <div v-for="t in (mcpPickedServer.tools || [])" :key="'mt-' + mcpPickedServer.name + t"
                     class="tree-row file">
                  <span class="tree-caret">·</span>
                  <span class="tree-name">{{ t }}</span>
                </div>
                <div v-if="!(mcpPickedServer.tools || []).length" class="lib-empty">
                  这台服务在线后没有报回任何工具。
                </div>
              </template>
            </template>
        </div>
      </div>

      <!-- —— 技能包里的一个文件：点开就能看（文本给正文，图片给直链） —— -->
      <div v-if="fileView" class="modal-mask" @click.self="fileView = null">
        <div class="modal-card">
          <div class="modal-head">
            <b>{{ fileView.skill }} / {{ fileView.path }}</b>
            <button class="help" @click="fileView = null">×</button>
          </div>
          <div class="modal-body">
            <div v-if="fileView.loading" class="lib-empty">读取中…</div>
            <div v-else-if="fileView.err" class="lib-empty bad">{{ fileView.err }}</div>
            <template v-else-if="fileView.binary">
              <img v-if="(fileView.mime || '').startsWith('image/') && fileView.url"
                   :src="fileView.url" class="file-img" alt="" />
              <p class="modal-note">{{ fileView.bytes }} 字节 · {{ fileView.mime || "二进制文件" }}</p>
              <a v-if="fileView.url" class="help" :href="fileView.url"
                 target="_blank" rel="noreferrer">打开 / 下载</a>
            </template>
            <template v-else>
              <p class="modal-note">{{ fileView.bytes }} 字节 · 只读预览</p>
              <pre class="file-pre">{{ fileView.text }}</pre>
            </template>
          </div>
        </div>
      </div>

      <!-- —— 条目弹窗：工具 / 技能 / MCP 服务 的详情与增删改 —— -->
      <div v-if="itemModal" class="modal-mask" @click.self="itemModal = null">
        <div class="modal-card">
          <div class="modal-head">
            <b>{{ itemModal.kind === "tool" ? "工具详情"
                  : itemModal.kind === "mcp" ? (itemModal.create ? "新增 MCP 服务" : "编辑 MCP 服务")
                  : "技能详情" }}</b>
            <button class="help" @click="itemModal = null">×</button>
          </div>

          <!-- 工具：详情只读（代码型/远程节点不可在线改写） -->
          <div v-if="itemModal.kind === 'tool'" class="modal-body">
            <div class="kv"><span>机器名</span><b>{{ itemModal.name }}</b></div>
            <div class="kv"><span>界面名</span><b>{{ itemModal.display }}</b></div>
            <div class="kv"><span>分类</span><b>{{ itemModal.cat }}</b></div>
            <div class="kv top"><span>说明</span><p>{{ itemModal.desc }}</p></div>
            <div v-if="itemModal.params && itemModal.params.properties"
                 class="kv top"><span>参数</span>
              <ul class="param-list">
                <li v-for="(pv, pk) in itemModal.params.properties" :key="pk">
                  <b>{{ pk }}</b>（{{ pv.type || "any" }}<template
                    v-if="(itemModal.params.required || []).includes(pk)">，必填</template>）{{ pv.description || "" }}
                </li>
              </ul>
            </div>
            <p class="modal-note">{{ itemModal.source === "mcp"
              ? "该工具来自动态 MCP 服务「" + itemModal.serverName + "」——修改/下线请编辑那个服务。"
              : "内置代码工具或 Storyline 远程节点：定义在代码/剪辑服务端，不可在线改写；扩展能力请用 MCP 动态注册或技能。" }}</p>
            <div v-if="itemModal.source === 'mcp'" class="td-row-btns">
              <button class="help"
                      @click="openMcpModal(mcpServers.find(x => x.name === itemModal.serverName))">
                编辑来源服务「{{ itemModal.serverName }}」
              </button>
            </div>
          </div>

          <!-- MCP 服务：编辑表单 -->
          <div v-else-if="itemModal.kind === 'mcp'" class="modal-body">
            <input v-model="mcpForm.name" placeholder="名称（字母数字与 _ -）" />
            <select v-model="mcpForm.type">
              <option value="streamableHttp">HTTP（streamableHttp）</option>
              <option value="stdio">本地进程（stdio）</option>
            </select>
            <input v-if="mcpForm.type === 'streamableHttp'"
                   v-model="mcpForm.url" placeholder="http://host:port/mcp" />
            <textarea v-if="mcpForm.type === 'streamableHttp'" v-model="mcpForm.headers" rows="3"
                      placeholder='请求头（JSON，选填），如 {"Authorization": "Bearer token"}'></textarea>
            <template v-else>
              <input v-model="mcpForm.command" placeholder="命令，如 npx" />
              <input v-model="mcpForm.args" placeholder="命令参数（逗号分隔）" />
            </template>
            <input v-model.number="mcpForm.tool_timeout" type="number" placeholder="工具超时（秒）" />
            <div v-if="mcpMsg" class="lib-empty">{{ mcpMsg }}</div>
            <div class="td-row-btns">
              <button class="help" :disabled="mcpBusy || !mcpForm.name.trim()" @click="saveMcp">保存</button>
              <template v-if="!itemModal.create">
                <button class="help" :disabled="mcpBusy" @click="toggleMcp(itemModal.server)">{{ itemModal.server.live ? "断开" : "启用" }}</button>
                <button class="help danger" @click="delMcp(itemModal.server); itemModal = null">删除</button>
              </template>
              <button class="help" @click="itemModal = null">取消</button>
            </div>
          </div>

          <!-- 技能：详情查看 + 表单编辑 + 删除 -->
          <div v-else class="modal-body">
            <template v-if="!skillForm">
              <div v-if="itemModal.loading" class="lib-empty">加载中…</div>
              <div v-else-if="itemModal.error" class="lib-empty bad">{{ itemModal.error }}</div>
              <template v-else-if="itemModal.detail">
                <div class="kv"><span>机器名</span><b>{{ itemModal.detail.name }}</b></div>
                <div class="kv"><span>中文名</span><b>{{ itemModal.detail.display || "—" }}</b></div>
                <div class="kv"><span>类型</span><b>{{ kindLabel(itemModal.detail.kind) }}
                  <span v-if="itemModal.detail.system">· 系统内置（只读）</span></b></div>
                <div v-if="itemModal.detail.kind === 'style'" class="kv top"><span>manifest</span>
                  <pre class="skill-pre">{{ manifestPretty(itemModal.detail.style_manifest) }}</pre></div>
                <div class="kv"><span>状态</span><b>
                  <span :class="itemModal.detail.available ? 'st-ok' : 'st-bad'">
                    {{ itemModal.detail.available ? "可用" : (itemModal.detail.unavailable_reason || "不可用") }}</span>
                  · 常驻：{{ itemModal.detail.always ? "是" : "否" }}</b></div>
                <div class="kv top"><span>描述</span><p>{{ itemModal.detail.description }}</p></div>
                <div class="kv"><span>依赖</span><b>{{ (itemModal.detail.requires || []).join("，") || "—" }}</b></div>
                <div class="kv"><span>附件</span><b>{{ (itemModal.detail.files || []).join("，") || "—" }}</b></div>
                <div class="kv top"><span>正文（SKILL.md）</span>
                  <pre class="skill-pre">{{ itemModal.detail.body }}</pre></div>
                <div class="td-row-btns">
                  <button v-if="!itemModal.detail.system" class="help"
                          @click="editSkillForm()">✎ 编辑</button>
                  <button v-if="itemModal.detail.style_manifest_prev" class="help"
                          @click="restorePrevManifest">↶ 恢复上一版 manifest</button>
                  <button v-if="!itemModal.detail.system" class="help danger"
                          @click="delSkillFromModal()">删除</button>
                </div>
              </template>
            </template>
            <template v-else>
              <input v-model.trim="skillForm.name" disabled
                     title="技能名来自包内的 SKILL.md，导入后不可改"
                     placeholder="技能名（来自包内 SKILL.md）" />
              <input v-model="skillForm.display" placeholder="中文名（界面显示）" />
              <input v-model="skillForm.description" placeholder="description（触发关键，写清何时用）" />
              <input v-model="skillForm.requires" placeholder="依赖（可选），如 CLI: curl, ENV: KEY" />
              <label class="kv chk">
                类型
                <select v-model="skillForm.kind" class="style-pick">
                  <option value="workflow">工作流（一条出片流程）</option>
                  <option value="capability">能力</option>
                  <!-- 2026-10-24：「风格包」这一档已撤（用户要求：风格包这条路删掉、
                       以后不允许造，视觉风格一律由 skill 提供）。这里不再给入口；
                       存量数据已清空，`kind=style` 的读取分支保留给老 spec 兜底。 -->
                </select>
              </label>
              <label class="kv chk"><input type="checkbox" v-model="skillForm.always" /> 常驻注入（每次对话都带上正文）</label>
              <input v-model="skillForm.sectionsText" placeholder="分段声明（可选，逗号分隔，如 image-channel, sound-design）" />
              <input v-model="skillForm.excludeText" placeholder="排除的附件前缀（可选，逗号分隔，如 demos/, shots/）" />
              <template v-if="skillForm.kind === 'style'">
                <div class="kv chk">
                  <button class="help" type="button" @click="fillStyleTemplate">填入剪影模板</button>
                  <span class="lib-empty" style="display:inline">manifest 是给镜头写手的视觉契约：同一构图、同一光位、字幕位置。</span>
                </div>
                <textarea v-model="skillForm.manifestText" rows="12"
                          placeholder='{"template": "silhouette-history", "repeat_composition": true, …}'></textarea>
              </template>
              <textarea v-model="skillForm.body" rows="14"
                        placeholder="SKILL.md 正文（markdown，不含 frontmatter）"></textarea>
              <div v-if="skillsMsg" class="lib-empty">{{ skillsMsg }}</div>
              <div class="td-row-btns">
                <button class="help" :disabled="skillBusy || !skillForm.name.trim()" @click="saveSkillForm">保存</button>
                <button class="help" @click="skillForm = null">取消</button>
              </div>
            </template>
          </div>
        </div>
      </div>

      <div v-if="activePanel === 'library'" class="library ex-lib">
        <!-- 资源管理器那种排法：上面一条工具条，左边文件夹，右边内容。 -->
        <div class="ex-bar">
          <span class="lib-count">{{ libraryItems.length }} 个素材</span>
          <button class="help" @click="loadMaterials" :disabled="loadingLibrary">
            {{ loadingLibrary ? "刷新中…" : "刷新" }}
          </button>
          <button
            v-if="libSelCount()"
            class="lib-add-btn"
            @click="addSelectedFromLibrary"
          >加入选中 {{ libSelCount() }} 项</button>
          <button
            v-if="libSelCount()"
            class="lib-del-btn"
            :disabled="deletingLib"
            @click="deleteSelected"
          >{{ deletingLib ? "删除中…" : `删除 ${libSelCount()} 项` }}</button>
          <span class="ex-view">
            <button class="ex-vbtn" :class="{ on: libMode.library === 'icons' }"
                    title="图标" @click="libMode.library = 'icons'">▦</button>
            <button class="ex-vbtn" :class="{ on: libMode.library === 'details' }"
                    title="详细信息" @click="libMode.library = 'details'">☰</button>
          </span>
        </div>
        <div v-if="!libraryItems.length && !loadingLibrary" class="lib-empty">
          还没有画面素材。上传视频或图片后会出现在这里（音频在「音乐库」）。
        </div>
        <!-- 文件夹这一层：只列类型，不把里面的素材一起摊开。 -->
        <div v-else-if="!materialFolder" class="ex-flist">
          <div v-for="f in materialFolders" :key="'mf-' + f.key" class="ex-frow"
               @click="libPicked.library = f.key">
            <span class="ex-fname">{{ f.label }}</span>
            <span class="ex-fcount">{{ f.items.length }} 项</span>
          </div>
        </div>
        <!-- 文件夹里面：只有这一层的东西，回上一级靠顶部那一行。 -->
        <section v-else class="ex-main">
            <div class="ex-back" @click="libPicked.library = ''">
              <span class="ex-caret">←</span>
              <span class="ex-fname">返回</span>
              <span class="ex-fcount">{{ materialFolder.label }} · {{ materialFolder.items.length }} 项</span>
            </div>
            <!-- 图标：缩略图网格（挑素材主要看画面）。 -->
            <div v-if="libMode.library === 'icons'" class="ex-grid">
              <div v-for="item in materialFolder.items" :key="item.material_id"
                   class="lib-item" :class="{ sel: libSelected[item.material_id] }">
                <label class="lib-check" :title="libSelected[item.material_id] ? '已选中' : '点选删除'">
                  <input type="checkbox" :checked="!!libSelected[item.material_id]"
                         @change="toggleLibSel(item.material_id)" />
                  <span></span>
                </label>
                <video v-if="item.kind === 'video'" :src="item.url" class="lib-thumb" preload="metadata"></video>
                <img v-else-if="item.kind === 'image'" :src="item.url" class="lib-thumb" :alt="item.filename" />
                <div v-else class="lib-thumb ex-thumb-ph">{{ mediaLabel(item.kind) }}</div>
                <div class="lib-meta">
                  <b :title="item.filename">{{ item.filename }}</b>
                  <span>{{ mediaLabel(item.kind) }} · {{ fmtSize(item.bytes) }}<template
                    v-if="item.duration"> · {{ Math.round(item.duration) }}s</template></span>
                </div>
                <audio v-if="item.kind === 'audio'" :src="item.url" class="lib-aud" controls preload="none"></audio>
                <button class="help" @click="addFromLibrary(item)">加入本会话</button>
              </div>
            </div>

            <!-- 详细信息：一张表，列跟资源管理器一样按「名称 / 类型 / 大小」。 -->
            <table v-else class="ex-table">
              <colgroup>
                <col style="width:24px" /><col /><col style="width:56px" />
                <col style="width:70px" /><col style="width:52px" /><col style="width:104px" />
              </colgroup>
              <thead>
                <tr><th class="ex-thchk"></th><th>名称</th><th>类型</th><th>大小</th><th>时长</th><th></th></tr>
              </thead>
              <tbody>
                <template v-for="item in materialFolder.items" :key="item.material_id">
                <tr :class="{ sel: libSelected[item.material_id] }">
                  <td class="ex-tdchk">
                    <label class="tree-check" :title="libSelected[item.material_id] ? '已选中' : '点选删除'">
                      <input type="checkbox" :checked="!!libSelected[item.material_id]"
                             @change="toggleLibSel(item.material_id)" />
                      <span></span>
                    </label>
                  </td>
                  <td class="ex-name" :title="item.filename">
                    <video v-if="item.kind === 'video'" :src="item.url" class="lib-thumb-xs" preload="metadata"></video>
                    <img v-else-if="item.kind === 'image'" :src="item.url" class="lib-thumb-xs" :alt="item.filename" />
                    <span class="ex-fname">{{ item.filename }}</span>
                  </td>
                  <td>{{ mediaLabel(item.kind) }}</td>
                  <td>{{ fmtSize(item.bytes) }}</td>
                  <td>{{ item.duration ? Math.round(item.duration) + "s" : "—" }}</td>
                  <td class="ex-op">
                    <button v-if="item.kind === 'audio' && item.url" class="help"
                            @click="toggleLibPlaying('m:' + item.material_id)">
                      {{ libPlaying === 'm:' + item.material_id ? "收起" : "试听" }}
                    </button>
                    <button class="help" @click="addFromLibrary(item)">加入本会话</button>
                  </td>
                </tr>
                <!-- 播放器单占一行：详细视图的列都很窄，塞进任何一格都会把别的格子挤掉。 -->
                <tr v-if="item.kind === 'audio' && libPlaying === 'm:' + item.material_id"
                    class="ex-play-row">
                  <td colspan="6"><audio :src="item.url" controls autoplay></audio></td>
                </tr>
                </template>
              </tbody>
            </table>
        </section>
      </div>

      <div v-if="activePanel === 'bgm'" class="library ex-lib">
        <div class="ex-bar">
          <span class="lib-count">音乐库 · {{ bgmImported.length }} 首</span>
          <button class="help" @click="loadBgmImported" :disabled="loadingBgmImported">
            {{ loadingBgmImported ? "刷新中…" : "刷新" }}
          </button>
          <template v-if="bgmFolder === 'online'">
            <input
              v-model="bgmQuery"
              class="bgm-search"
              placeholder="搜歌名或歌手…"
              @keydown.enter.prevent="loadBgm"
            />
            <button class="help" @click="loadBgm" :disabled="loadingBgm">
              {{ loadingBgm ? "搜索中…" : "搜索" }}
            </button>
          </template>
          <span class="ex-view">
            <button class="ex-vbtn" :class="{ on: libMode.bgm === 'icons' }"
                    title="图标" @click="libMode.bgm = 'icons'">▦</button>
            <button class="ex-vbtn" :class="{ on: libMode.bgm === 'details' }"
                    title="详细信息" @click="libMode.bgm = 'details'">☰</button>
          </span>
        </div>
        <!-- 文件夹这一层：已导入 / 在线搜索。 -->
        <div v-if="!bgmFolder" class="ex-flist">
          <div class="ex-frow" @click="libPicked.bgm = 'imported'">
            <span class="ex-fname">已导入</span>
            <span class="ex-fcount">{{ bgmImported.length }} 首</span>
          </div>
          <div class="ex-frow" @click="libPicked.bgm = 'online'">
            <span class="ex-fname">在线搜索</span>
            <span class="ex-fcount">{{ bgmItems.length }} 首</span>
          </div>
        </div>
        <section v-else class="ex-main">
            <div class="ex-back" @click="libPicked.bgm = ''">
              <span class="ex-caret">←</span>
              <span class="ex-fname">返回</span>
              <span class="ex-fcount">{{ bgmFolder === 'online' ? '在线搜索' : '已导入' }} ·
                {{ bgmFolder === 'online' ? bgmItems.length : bgmImported.length }} 首</span>
            </div>
            <!-- 已导入：音乐库里现有的音频。 -->
            <template v-if="bgmFolder === 'imported'">
              <div v-if="!bgmImported.length && !loadingBgmImported" class="lib-empty">
                音乐库还没有音频。可以在「在线搜索」里导入，或直接在对话里上传音频文件——它们都会出现在这里。
              </div>
              <div v-else-if="libMode.bgm === 'icons'" class="ex-grid">
                <div v-for="item in bgmImported" :key="item.material_id" class="lib-item ex-audio-card">
                  <div class="lib-meta">
                    <b :title="item.filename">{{ item.filename }}</b>
                    <span>{{ item.origin === "bgm" ? "曲库导入" : "本地上传" }} ·
                      {{ fmtSize(item.bytes) }}<template v-if="item.duration"> · {{ Math.round(item.duration) }}s</template></span>
                  </div>
                  <audio v-if="item.url" :src="item.url" class="lib-aud" controls preload="none"></audio>
                  <div class="ex-card-btns">
                    <button class="help" @click="addBgmImported(item)">加入本会话</button>
                    <button class="help" :disabled="deletingBgm === item.material_id"
                            @click="deleteBgmImported(item)">
                      {{ deletingBgm === item.material_id ? "删除中…" : "删除" }}
                    </button>
                  </div>
                </div>
              </div>
              <table v-else class="ex-table">
                <colgroup>
                  <col /><col style="width:66px" /><col style="width:66px" />
                  <col style="width:46px" /><col style="width:132px" />
                </colgroup>
                <thead>
                  <tr><th>名称</th><th>来源</th><th>大小</th><th>时长</th><th></th></tr>
                </thead>
                <tbody>
                  <template v-for="item in bgmImported" :key="item.material_id">
                  <tr>
                    <td class="ex-name" :title="item.filename">
                      <span class="ex-fname">{{ item.filename }}</span>
                    </td>
                    <td>{{ item.origin === "bgm" ? "曲库导入" : "本地上传" }}</td>
                    <td>{{ fmtSize(item.bytes) }}</td>
                    <td>{{ item.duration ? Math.round(item.duration) + "s" : "—" }}</td>
                    <td class="ex-op">
                      <button v-if="item.url" class="help"
                              @click="toggleLibPlaying('b:' + item.material_id)">
                        {{ libPlaying === 'b:' + item.material_id ? "收起" : "试听" }}
                      </button>
                      <button class="help" @click="addBgmImported(item)">加入本会话</button>
                      <button class="help" :disabled="deletingBgm === item.material_id"
                              @click="deleteBgmImported(item)">
                        {{ deletingBgm === item.material_id ? "删除中…" : "删除" }}
                      </button>
                    </td>
                  </tr>
                  <tr v-if="item.url && libPlaying === 'b:' + item.material_id" class="ex-play-row">
                    <td colspan="5"><audio :src="item.url" controls autoplay></audio></td>
                  </tr>
                  </template>
                </tbody>
              </table>
            </template>

            <!-- 在线搜索：搜到的歌也按文件列。 -->
            <template v-else>
              <div v-if="bgmError" class="lib-empty bad">{{ bgmError }}</div>
              <div v-else-if="!bgmItems.length && !loadingBgm" class="lib-empty">
                <template v-if="bgmHint">{{ bgmHint }}</template>
                <template v-else>输入歌名或歌手名搜索，搜到的歌可以试听、导入做配乐。</template>
              </div>
              <div v-else-if="libMode.bgm === 'icons'" class="ex-grid">
                <div v-for="item in bgmItems" :key="item.track_id + '|' + item.source"
                     class="lib-item ex-audio-card">
                  <div class="lib-meta">
                    <b :title="item.name">{{ item.name }}</b>
                    <span>{{ item.artist }} · {{ item.source }}</span>
                  </div>
                  <audio
                    v-if="bgmAudioUrl[item.track_id + '|' + item.source]"
                    :src="bgmAudioUrl[item.track_id + '|' + item.source]"
                    class="lib-aud" controls preload="none"
                  ></audio>
                  <div class="ex-card-btns">
                    <button v-if="!bgmAudioUrl[item.track_id + '|' + item.source]" class="help"
                            @click="playBgm(item)">试听</button>
                    <button class="help" :disabled="importingBgm === item.track_id"
                            @click="useBgm(item)">
                      {{ importingBgm === item.track_id ? "导入中…" : "导入并做配乐" }}
                    </button>
                  </div>
                </div>
              </div>
              <table v-else class="ex-table">
                <colgroup>
                  <col /><col style="width:86px" /><col style="width:86px" /><col style="width:124px" />
                </colgroup>
                <thead>
                  <tr><th>名称</th><th>歌手</th><th>来源</th><th></th></tr>
                </thead>
                <tbody>
                  <template v-for="item in bgmItems" :key="item.track_id + '|' + item.source">
                  <tr>
                    <td class="ex-name">
                      <span class="ex-fname" :title="item.name">{{ item.name }}</span>
                    </td>
                    <td>{{ item.artist }}</td>
                    <td>{{ item.source }}</td>
                    <td class="ex-op">
                      <button class="help" @click="playBgmRow(item)">试听</button>
                      <button class="help" :disabled="importingBgm === item.track_id"
                              @click="useBgm(item)">
                        {{ importingBgm === item.track_id ? "导入中…" : "做配乐" }}
                      </button>
                    </td>
                  </tr>
                  <tr v-if="bgmAudioUrl[item.track_id + '|' + item.source]
                            && libPlaying === 'o:' + item.track_id + '|' + item.source"
                      class="ex-play-row">
                    <td colspan="4">
                      <audio :src="bgmAudioUrl[item.track_id + '|' + item.source]"
                             controls autoplay></audio>
                    </td>
                  </tr>
                  </template>
                </tbody>
              </table>
            </template>
        </section>
      </div>

      <!-- 文字库：字幕样式与朗读音色。挑过的维度**只对这条对话**生效，其余仍归模型；
           开一册新卷就回到「交给模型自己定」。 -->
      <div v-if="activePanel === 'textlib'" class="library ex-lib">
        <div class="ex-bar">
          <span class="lib-count">
            <template v-if="!active">先开一册卷（或点进一个会话）再挑</template>
            <template v-else-if="!hasPrefs">本对话还没挑过——字幕与声音都由模型定</template>
            <template v-else>
              本对话：<template v-if="subtitleSummary">字幕 {{ subtitleSummary }}</template>
              <template v-if="subtitleSummary && soundSummary"> · </template>
              <template v-if="soundSummary">音色 {{ soundSummary }}</template>
            </template>
          </span>
          <button v-if="hasPrefs" class="help" :disabled="!!savingPrefs" @click="clearPrefs">
            {{ savingPrefs ? "恢复中…" : "恢复默认" }}
          </button>
          <button class="help" @click="refreshTextLib" :disabled="loadingTextLib || loadingPreview">
            {{ (loadingTextLib || loadingPreview) ? "刷新中…" : "刷新" }}
          </button>
          <!-- 语速只跟音色有关，所以只在音色那格里出现，别在挑字幕样式时占地方。 -->
          <label class="tl-rate" v-if="textFolder && textFolder.kind === 'voice'" @click.stop>
            <span>{{ dimTitle("rate") }}</span>
            <select :value="convPrefs.rate ?? (textLib.dims || {}).rate?.default ?? ''"
                    :disabled="!!savingPrefs"
                    @change="pickDim('rate', $event.target.value)">
              <option v-for="o in ((textLib.dims || {}).rate?.enum || [])" :key="o" :value="o">
                {{ labelOf("rate", o) }}
              </option>
            </select>
          </label>
          <span class="ex-view">
            <button class="ex-vbtn" :class="{ on: libMode.textlib === 'icons' }"
                    title="图标" @click="libMode.textlib = 'icons'">▦</button>
            <button class="ex-vbtn" :class="{ on: libMode.textlib === 'details' }"
                    title="详细信息" @click="libMode.textlib = 'details'">☰</button>
          </span>
        </div>
        <div v-if="textLibError" class="lib-empty bad">{{ textLibError }}</div>
        <div v-if="!libReady && loadingTextLib" class="lib-empty">正在取字幕样式与音色清单…</div>
        <div v-else-if="!libReady" class="lib-empty">
          拿不到清单——剪辑服务还没连上时会这样。稍后点「刷新」重试。
        </div>

        <!-- 文件夹这一层：每组预设（字幕样式 / 高亮样式…）、强调色、音色。 -->
        <div v-else-if="!textFolder" class="ex-flist">
          <div v-for="f in textFolders" :key="'tf-' + f.key" class="ex-frow"
               @click="libPicked.textlib = f.key">
            <span class="ex-fname">{{ f.label }}</span>
            <span class="ex-fcount">{{ f.count }} 项</span>
          </div>
        </div>
        <div v-else class="ex-body">
          <section class="ex-main ex-main-text">
            <div class="ex-back" @click="libPicked.textlib = ''">
              <span class="ex-caret">←</span>
              <span class="ex-fname">返回</span>
              <span class="ex-fcount">{{ textFolder.label }} · {{ textFolder.count }} 项</span>
            </div>
            <!-- 预设档：点「看样张」只换右边那张图，点「用这一档」才写进本对话。 -->
            <template v-if="textFolder.kind === 'preset'">
              <div v-if="libMode.textlib === 'icons'" class="ex-grid">
                <div v-for="p in presetsOf(textFolder.group)" :key="p.key"
                     class="lib-item ex-card" :class="{ sel: presetOn(p) }">
                  <span class="ex-cardmark">{{ presetOn(p) ? "✓"
                    : ((shownOf(textFolder.group) || {}).key === p.key ? "◉" : "") }}</span>
                  <div class="lib-meta">
                    <b>{{ p.label }}</b>
                    <span>{{ p.note }}</span>
                  </div>
                  <div class="ex-card-btns">
                    <button class="help" @click="showPreset(p)">看样张</button>
                    <button class="help" :disabled="!!savingPrefs" @click="pickPreset(p)">
                      {{ presetOn(p) ? "已选（点击重设）" : "用这一档" }}
                    </button>
                  </div>
                </div>
              </div>
              <table v-else class="ex-table">
                <colgroup>
                  <col style="width:30%" /><col /><col style="width:58px" /><col style="width:128px" />
                </colgroup>
                <thead>
                  <tr><th>名称</th><th>说明</th><th>状态</th><th></th></tr>
                </thead>
                <tbody>
                  <tr v-for="p in presetsOf(textFolder.group)" :key="p.key"
                      :class="{ sel: presetOn(p) }">
                    <td class="ex-name"><span class="ex-fname">{{ p.label }}</span></td>
                    <td class="ex-sub">{{ p.note }}</td>
                    <td>{{ presetOn(p) ? "已选"
                      : ((shownOf(textFolder.group) || {}).key === p.key ? "正在看" : "—") }}</td>
                    <td class="ex-op">
                      <button class="help" @click="showPreset(p)">看样张</button>
                      <button class="help" :disabled="!!savingPrefs" @click="pickPreset(p)">
                        {{ presetOn(p) ? "已选" : "用这一档" }}
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </template>

            <!-- 强调色：不钉＝跟色板走，钉住＝整条片子按这个色。 -->
            <template v-else-if="textFolder.kind === 'color'">
              <table class="ex-table">
                <colgroup>
                  <col style="width:92px" /><col /><col style="width:76px" /><col style="width:78px" />
                </colgroup>
                <thead>
                  <tr><th>名称</th><th>说明</th><th>当前</th><th></th></tr>
                </thead>
                <tbody>
                  <tr :class="{ sel: !previewColor }">
                    <td class="ex-name"><span class="ex-fname">跟随色板</span></td>
                    <td class="ex-sub">各镜用自己的那套色板</td>
                    <td>{{ !previewColor ? "已选" : "—" }}</td>
                    <td class="ex-op">
                      <button class="help" :disabled="!!savingPrefs"
                              @click="pickDim(textFolder.dim, null)">
                        {{ !previewColor ? "已选" : "用这一档" }}
                      </button>
                    </td>
                  </tr>
                  <tr :class="{ sel: !!previewColor }">
                    <td class="ex-name"><span class="ex-fname">钉一个颜色</span></td>
                    <td class="ex-sub">整条片子的高亮词都用它（镜头代码里自己写的那一色也盖过）</td>
                    <td>{{ previewColor || "—" }}</td>
                    <td class="ex-op">
                      <input type="color" class="tl-color-input" :value="previewColor || colorSeed"
                             :disabled="!!savingPrefs"
                             @change="pickDim(textFolder.dim, $event.target.value)" />
                    </td>
                  </tr>
                </tbody>
              </table>
            </template>

            <!-- 音色：一屏列出全部，组名进「类型」一列；语速在工具条上。 -->
            <template v-else-if="textFolder.kind === 'voice'">
              <div v-if="libMode.textlib === 'icons'" class="ex-grid">
                <div v-for="v in voiceRows" :key="v.value" class="lib-item ex-card ex-audio-card"
                     :class="{ sel: convPrefs.voice === v.value }">
                  <span class="ex-cardmark">{{ convPrefs.voice === v.value ? "✓" : "" }}</span>
                  <div class="lib-meta">
                    <b>{{ v.label }}</b>
                    <span>{{ v.group }} · {{ v.value }}</span>
                  </div>
                  <audio v-if="previewKey === vkey(v.value)" :src="voicePreviewUrls[vkey(v.value)]"
                         class="lib-aud" controls autoplay></audio>
                  <div class="ex-card-btns">
                    <button class="help" :disabled="loadingVoice === v.value"
                            @click="auditionVoice(v.value)">
                      {{ loadingVoice === v.value ? "合成中…"
                         : (previewKey === vkey(v.value) ? "重听" : "试听") }}
                    </button>
                    <button class="help" :disabled="!!savingPrefs" @click="pickDim('voice', v.value)">
                      {{ convPrefs.voice === v.value ? "已选（点击重设）" : "用这个音色" }}
                    </button>
                  </div>
                </div>
              </div>
              <table v-else class="ex-table">
                <colgroup>
                  <col style="width:32%" /><col style="width:82px" /><col /><col style="width:146px" />
                </colgroup>
                <thead>
                  <tr><th>名称</th><th>类型</th><th>音色 ID</th><th></th></tr>
                </thead>
                <tbody>
                  <template v-for="v in voiceRows" :key="v.value">
                  <tr :class="{ sel: convPrefs.voice === v.value }">
                    <td class="ex-name"><span class="ex-fname">{{ v.label }}</span></td>
                    <td>{{ v.group }}</td>
                    <td class="ex-mono">{{ v.value }}</td>
                    <td class="ex-op">
                      <button class="help" :disabled="loadingVoice === v.value"
                              @click="auditionVoice(v.value)">
                        {{ loadingVoice === v.value ? "合成中…"
                           : (previewKey === vkey(v.value) ? "重听" : "试听") }}
                      </button>
                      <button class="help" :disabled="!!savingPrefs" @click="pickDim('voice', v.value)">
                        {{ convPrefs.voice === v.value ? "已选" : "用这个音色" }}
                      </button>
                    </td>
                  </tr>
                  <tr v-if="previewKey === vkey(v.value) && voicePreviewUrls[vkey(v.value)]"
                      class="ex-play-row">
                    <td colspan="4"><audio :src="voicePreviewUrls[vkey(v.value)]" controls autoplay></audio></td>
                  </tr>
                  </template>
                </tbody>
              </table>
            </template>
          </section>

          <!-- 预览窗格（资源管理器右边那一栏）：图是渲染容器渲的真帧，只换注入的那几个
               样式名与强调色；不是前端照着档名画的示意图——示意图会和成片漂。 -->
          <aside v-if="drawerW >= 760" class="ex-preview">
            <div class="ex-prev-head">
              <b>字幕样张</b>
              <span v-if="previewPairLabel">正看：{{ previewPairLabel }}</span>
            </div>
            <div class="tl-preview">
              <img v-if="previewUrl" class="tl-shot" :src="previewUrl"
                   :alt="previewPairLabel || '字幕样张'" />
              <div v-else class="tl-shot tl-shot-empty">
                <template v-if="loadingPreview">正在渲样张…（同一对只渲这一次，之后走缓存）</template>
                <template v-else-if="previewErr">{{ previewErr }}</template>
                <template v-else-if="previewAsked">这一对这次没渲出来——换一档看，或点「刷新」重来</template>
                <template v-else>点任一档的「看样张」即可看到它在真实渲染下的样子</template>
              </div>
            </div>
            <p class="ex-prev-note">
              点档名或「看样张」只换这张图；要生效仍按「用这一档」。
              底是一块空白（只留一层明暗，好看对比度）——画面由镜头代码自由画，不在这里挑。
            </p>
          </aside>
        </div>
      </div>

      <!-- 设置：模型密钥与配额。画面风格不在这里——对话里说一句就行，
           入口只留一枚「当前风格」的只读标签。 -->
      <div v-if="activePanel === 'settings'" class="settings">
        <!-- LLM 主模型:密钥 + 模型名/地址(可选),一个保存按钮 -->
        <div class="set-line set-head">
          LLM 主模型
          <span v-if="settings && settings.api_key.configured" class="set-v">
            已配置 · {{ settings.model }} @ {{ settings.base_url }}
          </span>
          <span v-else class="set-v bad">未配置 —— 视觉理解与文案会整链降级</span>
        </div>
        <div class="set-line">
          <input v-model="keyDraft" type="password" autocomplete="off" spellcheck="false"
            :placeholder="settings && settings.api_key.configured
              ? '新密钥(不改则留空)' : '粘贴 API Key（sk-…），只存进本机 PG'"
            :disabled="savingLLM" @keydown.enter.prevent="saveLLM" />
        </div>
        <div class="set-line">
          <input v-model.trim="modelDraft.model" placeholder="模型名,留空 = 默认 deepseek-flash"
            :disabled="savingLLM" @keydown.enter.prevent="saveLLM" />
          <input v-model.trim="modelDraft.base_url" placeholder="API 地址,留空 = 默认"
            :disabled="savingLLM" @keydown.enter.prevent="saveLLM" />
        </div>
        <div class="set-line">
          <button class="clip" :disabled="savingLLM" @click="saveLLM">
            {{ savingLLM ? "保存中…" : "保存" }}
          </button>
          <button v-if="settings && settings.api_key.configured" class="clip ghost"
            :disabled="savingLLM" @click="clearKey">清除密钥</button>
          <button class="clip" :disabled="testingKey" @click="testConn">
            {{ testingKey ? "自检中…" : "测试连接" }}
          </button>
        </div>
        <p v-if="settings" class="set-note">
          保存即生效，剪辑服务最多 {{ settings.hot_reload_sec }} 秒后跟上，两个服务都不用重启。
          模型思考模式当前为<b>{{ settings.thinking ? "开" : "关" }}</b
          >（关掉时答复更快，长推理任务才需要开：环境变量 OPENAI_THINKING=on）。
          <template v-if="settings.api_key.updated_at">上次修改 {{ settings.api_key.updated_at.slice(0, 19).replace("T", " ") }}。</template>
        </p>

        <!-- 判断模型(Jev 类):三件套 -->
        <div class="set-line set-head">
          判断模型(Jev 类·守卫复核)
          <span
            v-if="settings && settings.judge"
            :class="settings.judge.configured ? 'set-v' : 'set-note'"
          >{{ settings.judge.configured
             ? `已配置 · ${settings.judge.model} @ ${settings.judge.base_url}`
             : "未配置——守卫只用词面判据" }}</span>
        </div>
        <div class="set-line">
          <input v-model.trim="judgeDraft.base_url" placeholder="判断模型 API 地址"
                 :disabled="savingJudge" />
        </div>
        <div class="set-line">
          <input v-model.trim="judgeDraft.model" placeholder="模型名,如 jev-latest"
                 :disabled="savingJudge" />
          <input v-model="judgeDraft.api_key" type="password" autocomplete="off"
                 placeholder="API Key(不改则留空)" :disabled="savingJudge" />
          <button class="clip" :disabled="savingJudge
                  || !judgeDraft.base_url || !judgeDraft.model" @click="saveJudge">
            保存
          </button>
          <button class="clip" :disabled="testingJudge" @click="testJudgeConn">
            {{ testingJudge ? "自检中…" : "测试连接" }}
          </button>
        </div>
        <p class="set-note">
          判断模型用于收尾守卫的第二意见(假称完成/散文式提问的复核)——
          只下判断不生成,成本约为生成模型的百分之一。不配置则守卫只用词面判据。
        </p>
        <div v-if="testJudgeResult" class="probe">
          <div>
            <b :class="testJudgeResult.ok ? 'ok' : 'bad'">{{ testJudgeResult.via || "Jev" }}</b>
            {{ testJudgeResult.ok
               ? "连通 · " + (testJudgeResult.latency_ms || "?") + "ms · 置信度 " + (testJudgeResult.confidence ?? "—")
               : "不通 · " + (testJudgeResult.error || "未知错误") }}
          </div>
        </div>
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
        <!-- 配额：按身份的滑动窗口，页面值压过环境变量与启动参数，保存即生效 -->
        <div class="set-line set-head">
          配额（按身份 · 滑动窗口）
          <span v-if="settings && settings.quota"
                :class="settings.quota.blocking ? 'set-v' : 'set-note'">
            {{ settings.quota.blocking
               ? `拦截中 · 窗口内已用 ${settings.quota.used.toLocaleString()} / ${settings.quota.limit.toLocaleString()} token`
               : `只记账不拦截 · 窗口内已用 ${settings.quota.used.toLocaleString()} token` }}
            （{{ settings.quota.window_label }} · 来源 {{ settings.quota.source }}）
          </span>
        </div>
        <div class="set-line">
          <input v-model.trim="quotaDraft.limit" spellcheck="false"
                 :placeholder="settings && settings.quota && settings.quota.pg_limit
                   ? `页面已填 ${settings.quota.pg_limit}（不改则留空）`
                   : '上限 token，如 20000000；填 0 = 只记账不拦'"
                 :disabled="savingQuota" @keydown.enter.prevent="saveQuota" />
          <input v-model.trim="quotaDraft.window" spellcheck="false"
                 :placeholder="settings && settings.quota && settings.quota.pg_window
                   ? `窗口已填 ${settings.quota.pg_window} 秒（不改则留空）`
                   : '窗口秒数,留空 = 默认 86400'"
                 :disabled="savingQuota" @keydown.enter.prevent="saveQuota" />
          <button class="clip" :disabled="savingQuota
                  || (!quotaDraft.limit && !quotaDraft.window)" @click="saveQuota">
            {{ savingQuota ? "保存中…" : "保存" }}
          </button>
          <button v-if="settings && settings.quota && settings.quota.overridden"
                  class="clip ghost" :disabled="savingQuota" @click="clearQuotaOverride">清除页面覆盖</button>
          <button class="clip ghost" :disabled="savingQuota" @click="refreshQuota">刷新账</button>
        </div>
        <p v-if="settings && settings.quota" class="set-note">
          <template v-if="settings.quota.frees_at">
            <b class="set-v bad">当前已被拦住</b>：最早那笔在
            {{ settings.quota.frees_at.slice(0, 16).replace("T", " ") }} UTC
            （约 {{ settings.quota.frees_in_minutes }} 分钟后）滚出窗口，届时额度自动回落。
            刷新页面、换会话、重开标签页都退不回已花掉的账——要立刻继续，把上面的上限改大或填 0。
          </template>
          <template v-else>
            被拦的是「下一次问模型」：用量按身份记在服务端，与浏览器状态无关。
            上限填 <b>0</b> = 照常记账但不拦截；留空则交回环境变量与启动参数那两层。
            {{ settings.quota.calls }} 笔调用 · 剩余 {{ settings.quota.remaining === null
               ? "不限" : settings.quota.remaining.toLocaleString() }}。
          </template>
        </p>

        <!-- 联网检索后端：哪一路能出结果由这台机器说了算，配置也按身份热读 -->
        <div class="set-line set-head">
          联网检索后端
          <span v-if="settings && settings.search" :class="settings.search.provider
                 || settings.search.effective_provider ? 'set-v' : 'set-note'">
            {{ settings.search.effective_provider
               ? `走 ${settings.search.effective_provider}（来源 ${settings.search.source}）`
               : "未配置后端 —— 走免 key 回落链，命中率看这台机器的 IP 像不像爬虫" }}
            <template v-if="settings.search.masked"> · {{ settings.search.masked }}</template>
          </span>
        </div>
        <div class="set-line">
          <select v-model="searchDraft.provider" :disabled="savingSearch">
            <option value="">免 key 回落链（不指定后端）</option>
            <option v-for="p in (settings && settings.search && settings.search.providers) || []"
                    :key="p" :value="p">{{ p }}</option>
          </select>
          <input v-if="searchDraft.provider === 'searxng'" v-model.trim="searchDraft.base_url"
                 spellcheck="false" placeholder="自建实例地址 http(s)://…"
                 :disabled="savingSearch" />
          <input v-else v-model="searchDraft.api_key" type="password" autocomplete="off"
                 spellcheck="false" placeholder="搜索 API Key（不改则留空）"
                 :disabled="savingSearch" />
        </div>
        <div class="set-line">
          <button class="clip" :disabled="savingSearch" @click="saveSearch">
            {{ savingSearch ? "保存中…" : "保存" }}
          </button>
          <button class="clip" :disabled="testingSearch" @click="testSearchConn">
            {{ testingSearch ? "自检中…" : "测试连接" }}
          </button>
          <button v-if="settings && settings.search && settings.search.overridden"
                  class="clip ghost" :disabled="savingSearch" @click="clearSearchOverride">清除页面覆盖</button>
        </div>
        <p class="set-note">
          模型用的是 DeepSeek 时，留空就自动走 DeepSeek 官方检索与抓取（沿用模型密钥，
          每千次 5 元）；deepseek 也可以单独填一把 Key。brave / tavily / serpapi 要 API Key；
          searxng 是自建实例、只要地址（它的 settings.yml 得打开 json_format）。保存即生效不重启。
        </p>
        <div v-if="testSearchResult" class="probe">
          <div>
            <b :class="testSearchResult.ok ? 'ok' : 'bad'">{{ testSearchResult.backend
                       || testSearchResult.provider || "免 key 回落链" }}</b>
            <template v-if="testSearchResult.ok">
              {{ `连通 · ${testSearchResult.results} 条 · ${testSearchResult.latency_ms}ms` }}
            </template>
            <!-- 「后端答了话」与「这条查询有可用结果」是两件事：免 key 链把与查询词
                 零重合的条目全丢掉时，说「不通」会让人去查网络，真要查的是关键词。 -->
            <template v-else>
              {{ (testSearchResult.refused ? "后端有回应，但这条查询没留下可用结果 · "
                                          : "不通 · ")
                 + (testSearchResult.message || "未知错误") }}
            </template>
          </div>
          <div v-if="testSearchResult.sample && testSearchResult.sample.length"
               class="set-note">{{ testSearchResult.sample.join(" ／ ") }}</div>
          <div v-if="testSearchResult.ok" class="set-note">{{ testSearchResult.message }}</div>
        </div>

        <!-- 图像出图后端那块设置（provider / 密钥 / 模型 / 尺寸 + 保存 / 自检 / 清除覆盖）已随出图链路一起拿掉：
             成片不再从任何图源取图（素材库检索、出图 API、上传位图都没有了），每一镜由 LLM 写 TSX 现画，
             这块面板留着就是让人去配一条不存在的路。 -->
        <!-- 记忆：Agent 自己攒的长期记忆，此前只能由 update_memory/read_memory 读写，
             用户在界面上看不见也改不掉。这一块把它摊开——查、改、删都在这里。 -->
        <div class="set-line set-head">
          记忆（Agent 长期记忆）
          <span v-if="memories" class="set-note">
            共 {{ memories.total_lines }} 条 · {{ memories.total_bytes }} 字节
          </span>
          <button class="clip ghost" :disabled="!memories || !!memoryCompacting"
                  @click="compactMemory('')">
            {{ memoryCompacting === "*" ? "整理中…" : "整理全部" }}
          </button>
        </div>
        <template v-if="memories && memories.categories">
          <div v-for="row in memories.categories" :key="row.category" class="mem-block">
            <div class="set-note">
              <b class="set-v">{{ row.label }}</b>
              （category='{{ row.category }}'）·
              {{ row.lines }} 条 / {{ row.bytes }} 字节
              <template v-if="row.updated_at"> · 最后更新
                {{ String(row.updated_at).slice(0, 16).replace("T", " ") }}</template>
              <!-- 超预算这一类当轮不会整条注入（注入侧按条取舍、不截字），
                   所以这里当场标出来——不然用户只会觉得「我明明写了它却不照做」。 -->
              <b v-if="memories && row.bytes > memories.inject_per_category_bytes" class="bad">
                · 已超单类预算 {{ memories.inject_per_category_bytes }} 字节，本轮可能不被注入
              </b>
              <!-- 触发线：写侧追加时过这条线就自动整理一次。不显示它，用户只会看到
                   「记忆自己变短了」而不知道是谁动的、什么时候动的。 -->
              <span v-if="memories && row.over_limit" class="mem-over">
                · 已过整理线（{{ row.over_limit }}），下次追加会自动整理
              </span>
            </div>
            <textarea v-model="memoryDraft[row.category]" rows="4" spellcheck="false"
                      class="mem-area"
                      :placeholder="row.category === 'user'
                        ? '用户偏好：如「旁白用女声」「成片默认竖屏」——每行一条'
                        : '工具使用教训：如「渲染失败先查字体软链」——每行一条'"
                      :disabled="memorySaving === row.category"></textarea>
            <div class="set-line">
              <button class="clip" :disabled="memorySaving === row.category"
                      @click="saveMemory(row.category)">
                {{ memorySaving === row.category ? "保存中…" : "保存" }}
              </button>
              <button class="clip ghost" :disabled="memorySaving === row.category || !row.lines"
                      @click="deleteMemory(row.category)">删除这一类</button>
              <button class="clip ghost"
                      :disabled="!!memoryCompacting || !row.lines"
                      @click="compactMemory(row.category)">
                {{ memoryCompacting === row.category ? "整理中…" : "整理这一类" }}
              </button>
            </div>
          </div>
        </template>
        <!-- 整理判决：改了哪几条、丢的原文是什么，逐条念出来。
             只给一个「已整理」的按钮会让用户只能选择相信或不信——而这是**删了东西**
             的动作，必须能核对。 -->
        <div v-if="memoryReport" class="mem-report">
          <div v-for="rep in memoryReport" :key="rep.category" class="mem-report-row">
            <b class="set-v">{{ rep.category }}</b>
            <template v-if="rep.changed">
              ：{{ rep.before_lines }} 条 / {{ rep.before_bytes }} 字节 →
              {{ rep.after_lines }} 条 / {{ rep.after_bytes }} 字节
              <div v-if="rep.dropped && rep.dropped.length" class="set-note">
                丢掉 {{ rep.dropped.length }} 条：
                <span v-for="(d, i) in rep.dropped.slice(0, 8)" :key="i">
                  {{ i ? "；" : "" }}{{ d.reason }}「{{ d.text }}」</span>
                <span v-if="rep.dropped.length > 8">；…另 {{ rep.dropped.length - 8 }} 条</span>
              </div>
              <div v-if="rep.merged && rep.merged.length" class="set-note">
                合并 {{ rep.merged.length }} 处：{{ rep.merged.slice(0, 4).join("；") }}
              </div>
            </template>
            <template v-else>：{{ rep.skipped || "无需整理" }}</template>
          </div>
        </div>
        <p class="set-note">
          每行一条。Agent 每轮按相关度召回前几条注入，
          单类约 {{ memories ? memories.inject_per_category_bytes : 700 }} 字节、
          合计约 {{ memories ? memories.inject_budget_bytes : 1400 }} 字节——
          超出的条目本轮不会注入（模型看不到），所以这里存得越精炼越有用。
          清空文本框再保存＝删除这一类。
        </p>
        <!-- 阈值全部取自后端（memory_compact.MemoryCompactor），前端一个数都不自己写：
             两处各写一份，改一处漏一处时这里会给出「说没过线、其实整理了」的错判。 -->
        <p class="set-note">
          记忆也会**自己收口**：Agent 每次追加时，这一类过了
          {{ memories ? memories.compact_at_entries : 24 }} 条 /
          {{ memories ? memories.compact_at_bytes : 6000 }} 字节就顺手整理一次——
          完全相同或已被更长那条包含的条目并掉、占位与问句这类空话清掉、
          仍超过 {{ memories ? memories.max_entries : 40 }} 条时从最早的一条开始丢。
          这里手动保存的内容**不会**被自动整理（你写的每个字都算数）；
          要收一次就点「整理这一类」，丢掉了什么上面会逐条列出来。
        </p>
        <!-- 阈值取自后端（memory.MemoryContextSource 的两个类属性），前端不自己写一份：
             两处各写一个数，改一处漏一处时这里会给出「说没超、其实超了」的错判。 -->
        <p class="set-note">
          这里的读写与 Agent 的 update_memory / read_memory 是**同一张表**；
          「删除这一类」是不可撤销的整类清空。
        </p>
        <p v-if="memoryMsg" class="set-note"
           :class="{ bad: memoryMsg.includes('失败') || memoryMsg.includes('读不到') }">{{ memoryMsg }}</p>

        <!-- 提示词一览：此前「哪句话在生效」只散在 8 个 .py 与 prompts/ 目录里，只能翻仓库。
             这一块把清单摊开——按相分组、每份的字节数/调用点/占位符，点开看全文。
             **只读**：文件随代码分发进镜像，页面上改完落不进下次部署（见 loadPrompts）。 -->
        <div class="set-line set-head">
          提示词（发给模型的话）
          <span v-if="prompts && prompts.entries" class="set-note">
            共 {{ prompts.entries.length }} 份 · 每轮常发合计 {{ prompts.always_bytes }} 字节
          </span>
        </div>
        <p v-if="prompts && prompts.note" class="set-note">{{ prompts.note }}</p>
        <template v-if="prompts && prompts.dir">
          <p class="set-note">
            目录 {{ prompts.dir }}（清单 version {{ prompts.version }}）。
            每轮必发的那几份是固定段，其余按链（规划 / 剪辑 / 子 Agent）与按需加载——
            带 <b class="pmt-fixed">固定</b> 标记的就是唯一还带内联兜底的那份。
          </p>
          <!-- 清单与磁盘对不上（清单说有、盘上没有 / 加了文件没记账 / 某份超上限）时
               当场标红，不等下一次重启在日志里发现——启动期那条同样会拦服务。 -->
          <div v-for="p in (prompts.problems || [])" :key="p" class="set-note bad">自检：{{ p }}</div>
          <div v-for="ph in prompts.phases" :key="ph.key" class="pmt-phase">
            <div class="set-note">{{ ph.label }}</div>
            <div v-for="e in prompts.entries.filter(x => x.phase === ph.key)"
                 :key="e.name" class="pmt-row">
              <button class="pmt-head" @click="togglePrompt(e.name)">
                <span class="pmt-name">{{ e.name }}</span>
                <span class="set-note">
                  {{ e.kind_label }} · {{ e.bytes }} 字节<template
                    v-if="e.max_bytes">／上限 {{ e.max_bytes }}</template>
                  · {{ e.file }}
                  <template v-if="e.fixed"> · <b class="pmt-fixed">固定</b></template>
                  <template v-if="e.over_cap"> · <b class="bad">已超上限</b></template>
                  <template v-if="e.missing"> · <b class="bad">文件读不到</b></template>
                </span>
                <span class="set-note">{{ promptOpen === e.name ? "收起" : "看正文" }}</span>
              </button>
              <div class="set-note">{{ e.desc }}</div>
              <div v-if="e.referenced_by && e.referenced_by.length" class="set-note">
                调用点：{{ e.referenced_by.join(" · ") }}
              </div>
              <!-- 占位符要列出来：漏喂一个键不会抛错，只会让那段文案静默消失。 -->
              <div v-if="e.placeholders && e.placeholders.length" class="set-note">
                模板占位符：{{ e.placeholders.map(k => "{" + k + "}").join(" ") }}
              </div>
              <template v-if="promptOpen === e.name">
                <div v-if="promptOpenErr" class="set-note bad">{{ promptOpenErr }}</div>
                <pre v-else-if="promptOpenText" class="pmt-body">{{ promptOpenText }}</pre>
                <div v-else class="set-note">读取中…</div>
              </template>
            </div>
          </div>
          <p class="set-note">
            正文里的机器名会被换成中文名（如 skill id → 技能显示名），所以这里看到的字节数
            与正文字符数可能差几十字节，不是数据坏了。
            要改提示词就改仓库里的 <code>prompts/</code>（新增或改动要同步 manifest.json 的记账），改完重建。
          </p>
        </template>
        <p v-if="promptsMsg" class="set-note bad">{{ promptsMsg }}</p>

        <!-- MCP 外部工具：发版种子（backend/mcp.json，启动时入库）与这里登记的运行期
             那一路写的是**同一张表**，连接一律由 McpManager 发起——所以「哪台在线、
             贡献了几个工具」在这一个地方就能看全，不用去翻启动日志。 -->
        <div class="set-line set-head">
          MCP 外部工具
          <span :class="liveMcpCount ? 'set-v' : 'set-note'">
            {{ liveMcpCount
               ? `已连 ${liveMcpCount} 台 · 共 ${mcpToolCount} 个工具`
               : "未连任何外部 MCP —— 工具面只有本机自带的那些" }}
          </span>
        </div>
        <p v-if="!mcpServers.length" class="set-note">
          还没有登记任何 MCP Server。部署期可以放一份 <code>backend/mcp.json</code> 当种子
          （启动时写进库，不带这份文件就是现在这样）；也可以在下面现登记一台。
        </p>
        <div v-for="s in mcpServers" :key="'set-' + s.name" class="set-line">
          <span :class="s.live ? 'set-v' : 'set-note'" style="min-width: 11rem">
            {{ s.name }}
            <!-- 「这台是发版种子带进来的，删了重启还会回来」要在这一行说清，
                 否则有人删完发现它又冒出来，会当成 bug 查 -->
            <template v-if="s.owner_user_id === null">（种子·系统）</template>
          </span>
          <span class="set-note">
            {{ s.live
               ? `${s.tools.length} 个工具：${s.tools.slice(0, 4).join("、")}`
                  + (s.tools.length > 4 ? "…" : "")
               : (s.enabled ? "已启用但未连上（看上一条回话）" : "未启用") }}
          </span>
          <button class="clip" :disabled="mcpBusy" @click="toggleMcp(s, mcpSetMsg)">
            {{ s.live ? "关断" : "启用" }}
          </button>
          <button class="clip ghost" :disabled="mcpBusy" @click="delMcp(s, mcpSetMsg)">删除</button>
        </div>
        <div class="set-line">
          <input v-model.trim="mcpDraft.name" spellcheck="false"
                 placeholder="名称（字母数字与 _ -）" :disabled="mcpBusy" />
          <select v-model="mcpDraft.type" :disabled="mcpBusy">
            <option value="streamableHttp">streamableHttp</option>
            <option value="sse">sse</option>
            <option value="stdio">stdio</option>
          </select>
          <template v-if="mcpDraft.type === 'stdio'">
            <input v-model.trim="mcpDraft.command" spellcheck="false"
                   placeholder="命令，如 npx" :disabled="mcpBusy" />
            <input v-model.trim="mcpDraft.args" spellcheck="false"
                   placeholder="参数（逗号分隔）" :disabled="mcpBusy" />
          </template>
          <input v-else v-model.trim="mcpDraft.url" spellcheck="false"
                 placeholder="http://host:port/mcp" :disabled="mcpBusy" />
          <button class="clip" :disabled="mcpBusy || !mcpDraft.name.trim()"
                  @click="addMcpFromSettings">{{ mcpBusy ? "登记中…" : "登记" }}</button>
        </div>
        <p class="set-note">
          「登记」只写配置不连线；点「启用」才连，连上后它的工具**当轮**就进模型工具面，
          不用重启。与既有工具撞名会整体回绝（宁可不上线也不静默覆盖）。
        </p>
        <div v-if="mcpSetMsg" class="set-note">{{ mcpSetMsg }}</div>
        <div v-if="keyError" class="set-v bad">{{ keyError }}</div>
      </div>

      <div v-if="activePanel === 'timeline'" class="timeline-panel">
        <div v-if="timelineError" class="set-v bad">{{ timelineError }}</div>

        <template v-if="!editingTimeline">
          <div class="tl-toolbar">
            <button class="clip ghost" @click="importLatestTimeline">从当前会话导入</button>
            <button class="clip ghost" @click="loadTimelineList" :disabled="loadingTimeline">
              {{ loadingTimeline ? "刷新中…" : "刷新" }}
            </button>
          </div>
          <div v-if="!timelineList.length && !loadingTimeline" class="lib-empty">
            还没有保存的时间线。点击「从当前会话导入」获取 AI 生成的最新时间线；要按画面框选局部改，用成片卡片上的「✂ 选区改」。
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
          <span class="td-row-btns">
            <button class="help" title="把本会话所有已完成的执行合并净化成一篇技能——
失败重试与问询不计入,每个动作只保留你修正后的最终做法"
                    @click="fromConversationAsSkill">✦ 沉淀为技能</button>
            <button class="help" @click="loadRuns" :disabled="loadingRuns">
              {{ loadingRuns ? "刷新中…" : "刷新" }}
            </button>
          </span>
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

    <!-- 统一提问卡已改为内嵌在对话流里（见上方消息列表末尾），不再用弹窗挡住上下文 -->

    <!-- 成片卡上的「✂ 选区改」：在画面上框住要改的那块，只重烧受影响的那几镜 -->
    <MotionEditor v-if="motionEditor" :artifact-id="motionEditor.artifactId"
                  :conv-id="active" :media-url="motionEditor.url" :title="motionEditor.text"
                  @close="closeMotionEditor" @patched="onMotionPatched" />
  </div>
</template>

<style scoped>
.shell { display: flex; height: 100%; position: relative; z-index: 1; }

/* ---------- 卷宗栏 ---------- */
.rail {
  width: 264px; flex: none; display: flex; flex-direction: column;
  background: linear-gradient(180deg, #2a2a2a, #1f1f1f);
  color: #f5f5f5; padding: 22px 16px 16px;
  border-right: 3px solid var(--vermilion);
  transition: margin-left .22s ease;
  overflow: visible;
}
.rail.closed { margin-left: -267px; }   /* 宽度+边框,整体滑出 */
.rail-toggle {
  margin-left: auto; border: none; background: transparent; color: #e9e2d2;
  opacity: .5; cursor: pointer; font-size: 16px; padding: 4px 6px; border-radius: 6px;
}
.rail-toggle:hover { opacity: 1; background: rgba(255, 253, 247, .12); }
.rail-open-btn {
  position: fixed; left: 0; top: 22px; z-index: 50;
  border: none; border-radius: 0 6px 6px 0; cursor: pointer;
  background: #2a2a2a; color: #f5f5f5;
  border-right: 3px solid var(--vermilion); border-left: none;
  padding: 4px 8px; font-size: 16px; opacity: .7;
  box-shadow: 2px 0 8px rgba(0,0,0,.25);
}
.rail-open-btn:hover { opacity: 1; background: #333; }
.rail.closed .brand, .rail.closed .new, .rail.closed .convs,
.rail.closed .conv-section-label, .rail.closed .rail-user-bar { visibility: hidden; }
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

.rail-btns { display: flex; flex-direction: column; gap: 2px; padding: 4px 0; }
.rail-btns .help { font-size: 13px; padding: 8px 12px; border-radius: 6px; text-align: left; width: 100%; box-sizing: border-box; color: #f5f5f5; border-color: transparent; }
.rail-btns .help:hover { background: rgba(255, 255, 255, .06); border-color: transparent; color: #fff; }
.rail-btns .help.on { color: var(--vermilion); border-color: transparent; background: rgba(200, 64, 31, 0.18); }
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
.conv-section-label {
  font-size: 11px; color: rgba(245, 245, 245, 0.45);
  text-transform: uppercase; letter-spacing: 2px;
  padding: 14px 12px 6px; font-family: var(--serif);
}
.rail-user-bar {
  display: flex; align-items: center; gap: 4px; padding: 12px 8px;
  border-top: 1px solid rgba(245, 245, 245, 0.12);
}
.rail-username {
  flex: 1; color: #fff; font-size: 12px; font-family: ui-monospace, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rail-icon-btn {
  flex: none; border: none; background: transparent; color: #f5f5f5;
  cursor: pointer; padding: 6px; border-radius: 6px; display: flex;
  align-items: center; justify-content: center; opacity: .7;
  transition: all 0.18s;
}
.rail-icon-btn:hover { opacity: 1; background: rgba(255, 255, 255, .08); }
.rail-icon-btn.on { opacity: 1; color: var(--vermilion); }

/* ---------- 稿纸区 ---------- */
.desk { flex: 1; display: flex; flex-direction: column; min-width: 0; }

/* ---------- 右侧抽屉 ---------- */
.drawer {
  width: 380px; flex: none; display: flex; flex-direction: column;
  background: rgba(255, 253, 247, 0.72);
  border-left: 1px solid var(--line);
  overflow: hidden; position: relative;
}
.drawer-grip {
  position: absolute; left: 0; top: 0; bottom: 0; width: 7px;
  cursor: col-resize; z-index: 20;
}
.drawer-grip::after {
  content: ""; position: absolute; left: 3px; top: 50%; height: 46px; width: 2px;
  transform: translateY(-50%); background: var(--line); border-radius: 2px; opacity: .8;
}
.drawer-grip:hover::after { background: var(--vermilion); opacity: 1; }
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
  display: flex; flex-direction: column; gap: 2px; padding: 6px 0;
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

/* 「选区改」入口：只有产物带了命中表才出现；带不出的那条路给出解释而不是死按钮 */
.media-card .meta .mc-patch {
  margin-left: auto; padding: 2px 8px; font-size: 12px; letter-spacing: 1px;
  color: var(--vermilion-deep); border-color: var(--vermilion-deep);
}
.media-card .meta:has(.mc-patch) a { margin-left: 0; }
.mc-nopatch { font-size: 11.5px; opacity: .72; }

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
/* 一轮里的工具卡片折成的一行：整行可点（点开/收起），比逐张卡片矮一大截。 */
.tool-group { font-family: ui-monospace, monospace; font-size: 12.5px; }
.tg-head {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  width: 100%; text-align: left; cursor: pointer;
  background: none; border: none; padding: 0; font: inherit; color: var(--ink);
}
.tg-head b { font-family: var(--serif); font-size: 13px; color: var(--ink); }
.tg-head:hover b { color: var(--vermilion); }
.tg-list { color: var(--ink-soft); font-size: 11.5px; }
.tg-fail { color: var(--vermilion-deep); font-size: 11px; }
.tg-toggle { margin-left: auto; color: var(--ink-soft); font-size: 11px; opacity: 0.75; }
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

/* 落笔栏：按钮一整行在上，输入框整行在下。
   按钮挤在输入框左边时，长句的换行宽度只剩一半多；分开以后输入框独占一行。 */
.composer {
  display: flex; flex-direction: column; gap: 10px; align-items: stretch;
  padding: 14px 34px 24px;
  border-top: 1px solid var(--line); background: linear-gradient(180deg, transparent, rgba(239, 231, 214, 0.5));
}
.composer-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.composer-row { display: flex; gap: 12px; align-items: flex-end; }
.composer textarea {
  flex: 1; min-width: 0; resize: none; max-height: 240px; overflow-y: auto;
  padding: 12px 16px;
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
/* 出片模式：贴在输入框左侧。实线细框 + 与输入框同底，跟旁边的 .send 齐底。
   自己画下拉而不用 <select>：原生选项列表的高亮是系统画的（一片蓝），改不动。 */
.film-mode {
  flex: none; position: relative;
  display: inline-flex; align-items: center; gap: 7px;
  padding: 13px 11px; cursor: pointer; user-select: none;
  font-family: var(--sans); font-size: 13px; line-height: 1.4;
  color: var(--ink); background: rgba(255, 253, 247, 0.9);
  border: 1px solid var(--line); border-radius: 4px; transition: all 0.18s;
}
.film-mode:hover, .film-mode.open {
  color: var(--vermilion); border-color: var(--vermilion);
}
.fm-caret {
  width: 0; height: 0; flex: none; margin-top: 1px;
  border-left: 4px solid transparent; border-right: 4px solid transparent;
  border-top: 5px solid currentColor; transition: transform 0.18s;
}
.film-mode.open .fm-caret { transform: rotate(180deg); }
.film-menu {
  position: absolute; left: -1px; bottom: calc(100% + 6px);
  min-width: calc(100% + 2px); margin: 0; padding: 4px;
  list-style: none; z-index: 40;
  background: #fffdf7; border: 1px solid var(--line); border-radius: 4px;
  box-shadow: 0 8px 24px rgba(60, 40, 20, 0.12);
}
.film-menu li {
  padding: 7px 12px; border-radius: 3px; cursor: pointer;
  font-size: 13px; color: var(--ink); white-space: nowrap; transition: all 0.12s;
}
.film-menu li:hover { background: var(--vermilion); color: #fff8ec; }
.film-menu li.on { color: var(--vermilion); font-weight: 600; }
.film-menu li.on:hover { color: #fff8ec; }
/* 风格下拉：外形与旁边的 .clip 一致，但它是 <select>——原生小三角收进内边距里。 */
.style-pick {
  appearance: none; -webkit-appearance: none;
  max-width: 190px; padding-right: 26px;
  background-image: linear-gradient(45deg, transparent 50%, currentColor 50%),
                    linear-gradient(135deg, currentColor 50%, transparent 50%);
  background-position: calc(100% - 15px) 52%, calc(100% - 10px) 52%;
  background-size: 5px 5px, 5px 5px;
  background-repeat: no-repeat;
}
.style-pick option { color: var(--ink); background: var(--paper); }
/* 选过风格时用实线朱红（与旁边「全自动」等开关的选中态一致）；「自动」是虚线。 */
.style-pick.on { border-style: solid; }
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
.set-line input, .set-line select {
  flex: 1; min-width: 240px; padding: 9px 12px;
  font-family: ui-monospace, monospace; font-size: 13px; color: var(--ink);
  background: rgba(255, 253, 247, 0.9); border: 1px dashed var(--line); border-radius: 4px;
  outline: none;
}
.set-line input:focus, .set-line select:focus { border-color: var(--vermilion); border-style: solid; }
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
/* 记忆区块：一个分类一块——说明行 + 多行文本域 + 保存/删除。 */
.mem-block { display: flex; flex-direction: column; gap: 6px; padding: 8px 0;
  border-top: 1px dotted var(--line); }
.mem-area {
  width: 100%; padding: 9px 12px; box-sizing: border-box;
  font-family: ui-monospace, monospace; font-size: 12.5px; line-height: 1.6;
  color: var(--ink); background: rgba(255, 253, 247, 0.9);
  border: 1px dashed var(--line); border-radius: 4px; outline: none; resize: vertical;
}
.mem-area:focus { border-color: var(--vermilion); border-style: solid; }
/* 整理判决：丢/并了什么的逐条明细。用等宽字体+浅底，读起来像一张回执而不是提示语。 */
.mem-over { color: var(--ink-soft); }
.mem-report {
  display: flex; flex-direction: column; gap: 4px; padding: 8px 10px; margin-top: 4px;
  font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.6;
  background: rgba(200, 64, 31, 0.05); border: 1px dotted var(--vermilion); border-radius: 4px;
}
.mem-report-row { color: var(--ink); }
/* 提示词区块：按相（phase）分组，每份一行、点开看正文。只读——不给输入框，
   因为改这里落不进镜像（见 loadPrompts 上方那段注释）。 */
.pmt-phase { display: flex; flex-direction: column; gap: 2px; padding: 6px 0 2px; }
.pmt-row { display: flex; flex-direction: column; gap: 4px; padding: 6px 0;
  border-top: 1px dotted var(--line); }
.pmt-head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
  background: none; border: none; padding: 0; text-align: left; cursor: pointer;
  font: inherit; color: var(--ink); }
.pmt-head:hover .pmt-name { color: var(--vermilion); }
.pmt-name { font-family: ui-monospace, monospace; font-size: 12.5px; font-weight: 600; }
.pmt-fixed { color: var(--vermilion); font-weight: 600; }
.pmt-body {
  margin: 0; padding: 9px 11px; max-height: 22rem; overflow: auto;
  white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.65;
  color: var(--ink); background: rgba(255, 253, 247, 0.9);
  border: 1px dashed var(--line); border-radius: 4px;
}

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
/* 技能文件夹拖放区：虚线框 + 拖到上面才变实。它同时是「能拖」的唯一提示——
   没有这块，用户不会想到技能可以整包拖进来。 */
.skill-drop {
  margin: 6px 0 10px; padding: 14px 10px; border: 1px dashed var(--line);
  border-radius: 6px; text-align: center; color: var(--ink-soft);
  font-family: var(--serif); font-size: 12px; letter-spacing: 1px;
  transition: border-color .15s, color .15s, background .15s;
}
.skill-drop.over {
  border-color: var(--accent, #8a6a3a); color: var(--ink);
  background: rgba(138, 106, 58, .08);
}
.skill-kind {
  padding: 1px 6px; border: 1px solid var(--line); border-radius: 3px;
  font-size: 11px; color: var(--ink-soft);
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
/* 模板沉淀：参考片 → 可执行模板（与素材库同屏，但它是成品、不是原料） */
.tpl-deposit-btn { border-style: solid; opacity: 0.85; }
.tpl-deposit-btn:disabled { opacity: 0.45; cursor: wait; }
.tpl-card { width: min(460px, calc(100vw - 32px)); }
.tpl-card .modal-body label { font-size: 12px; color: var(--ink-soft); }
.tpl-thumb-empty {
  display: grid; place-items: center; min-height: 96px;
  font-family: var(--serif); font-size: 12px; letter-spacing: 2px; color: var(--ink-soft);
}
.tpl-desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 换件：替换件不是一套新风格，只是基模板里的一个零件——行内小标签说清归属。 */
.tpl-variant-tag { color: var(--ink-soft); font-size: 11px; line-height: 1.4; }
/* 换件面板：先看外壳，再点零件，再选改法——不让用户面对一个空白输入框。 */
.variant-card { width: min(600px, calc(100vw - 32px)); }
.variant-hero { position: relative; margin-bottom: 10px; border-radius: 6px; overflow: hidden; }
.variant-hero img { display: block; width: 100%; max-height: 190px; object-fit: cover; }
.variant-hero span {
  position: absolute; left: 0; right: 0; bottom: 0; padding: 6px 10px;
  background: linear-gradient(transparent, rgba(0, 0, 0, 0.72));
  color: #fff8ec; font-size: 11px;
}
.variant-parts {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(124px, 1fr));
  gap: 8px; margin: 6px 0 4px;
}
.variant-part {
  display: grid; grid-template-columns: auto 1fr; grid-template-rows: auto auto;
  column-gap: 8px; align-items: center; text-align: left; min-width: 0;
  padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: transparent; color: var(--ink); cursor: pointer; transition: all 0.18s;
}
.variant-part:hover:not(:disabled) { border-color: var(--vermilion); }
.variant-part.on {
  border-color: var(--vermilion); background: rgba(196, 60, 40, 0.06);
  color: var(--vermilion-deep);
}
.variant-part:disabled { opacity: 0.5; cursor: wait; }
.variant-part .vp-icon { grid-row: 1 / span 2; font-size: 20px; line-height: 1; }
.variant-part b { font-family: var(--serif); font-size: 13px; font-weight: normal; }
.variant-part small {
  font-size: 10px; color: var(--ink-soft);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.variant-part em {
  grid-column: 1 / -1; margin-top: 4px; font-style: normal;
  font-size: 10px; color: var(--ink-soft);
}
.variant-part-note {
  margin: 2px 0 8px; padding: 6px 8px; border-left: 2px solid var(--vermilion);
  background: rgba(196, 60, 40, 0.04); line-height: 1.5;
}
.variant-modes {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 6px 0 8px;
}
/* 建议：按当前模板/零件现生成，点一句就填进输入框——不是写死的风格例子。 */
.variant-suggest { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 6px 0 4px; }
.variant-suggest .vs-head { font-size: 12px; color: var(--ink-soft); }
.variant-suggest-chip {
  padding: 5px 10px; border: 1px solid var(--line); border-radius: 999px;
  background: transparent; color: var(--ink); font-size: 12px; cursor: pointer;
  max-width: 100%; text-align: left; transition: all 0.18s;
}
.variant-suggest-chip:hover:not(:disabled) { border-color: var(--vermilion); }
.variant-suggest-chip.on { border-color: var(--vermilion); color: var(--vermilion-deep); }
.variant-suggest-chip:disabled { opacity: 0.5; cursor: wait; }
.variant-mode {
  display: block; text-align: left; padding: 8px 9px;
  border: 1px solid var(--line); border-radius: 6px; background: transparent;
  cursor: pointer; transition: all 0.18s;
}
.variant-mode:hover:not(:disabled) { border-color: var(--vermilion); }
.variant-mode.on { border-color: var(--vermilion); background: rgba(196, 60, 40, 0.06); }
.variant-mode:disabled { opacity: 0.5; cursor: wait; }
.variant-mode b {
  display: block; font-family: var(--serif); font-size: 12px; font-weight: normal;
}
.variant-mode small {
  display: block; margin-top: 3px; font-size: 10px;
  color: var(--ink-soft); line-height: 1.4;
}
.variant-axes { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
.variant-axis {
  padding: 3px 9px; border: 1px solid var(--line); border-radius: 999px;
  background: transparent; color: var(--ink-soft); font-size: 12px; cursor: pointer;
}
.variant-axis.on { border-color: var(--vermilion); color: var(--vermilion); }
.variant-axis small { opacity: 0.65; margin-left: 4px; font-size: 10px; }
.variant-result {
  display: flex; gap: 10px; align-items: flex-start; margin: 8px 0;
  padding: 8px; border: 1px solid var(--line); border-radius: 6px;
}
.variant-result-img { width: 132px; border-radius: 4px; flex: 0 0 auto; }
.variant-result-body {
  display: flex; flex-direction: column; gap: 4px;
  font-size: 11px; color: var(--ink-soft);
}
.variant-result-body b {
  font-family: var(--serif); font-size: 13px; font-weight: normal; color: var(--ink);
}
.variant-result-body button { align-self: flex-start; }
.variant-result-body > .bad { color: var(--vermilion); }
.variant-result-item {
  display: flex; gap: 8px; align-items: flex-start; padding-top: 6px;
  border-top: 1px dashed var(--line);
}
.variant-result-text { display: flex; flex-direction: column; gap: 2px; }

/* 文字库：语速那一格 + 音色分组 */
.tl-rate {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; color: var(--ink-soft);
}
.tl-rate span { flex: 0 0 auto; }
.tl-rate select {
  flex: 1 1 auto; min-width: 0; padding: 4px 8px;
  font-size: 12.5px; color: var(--ink);
  background: rgba(255, 253, 247, 0.9); border: 1px dashed var(--line); border-radius: 4px;
  outline: none;
}
.tl-rate select:focus { border-color: var(--vermilion); border-style: solid; }
.tl-rate { margin-left: auto; }
.tl-group { margin-bottom: 10px; }
.tl-group-label {
  font-family: var(--serif); font-size: 12px; letter-spacing: 2px;
  color: var(--ink-soft); margin: 8px 0 6px;
}

/* 文字库：左右两栏（左边样张，右边要挑的卡）。样张 440px 高、右栏的卡更长，
   不把它钉在左边的话，滚到音色那一段去挑就再也看不见自己正在配的那张图了。
   抽屉拖窄到放不下两栏时它自己折回上下的老样子（`flex-wrap` 兜的），不必另写一套。 */
.tl-body {
  display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start;
  /* 折行是**容器宽度**决定的，不是视口宽度：抽屉可以拖宽拖窄。用容器查询让
     「钉住样张」只发生在两栏真的并排时——否则折行后样张独占一行，sticky 仍把它
     钉在滚动容器顶部，后面的卡片会从它底下滚过去，看起来就是样张叠在卡片上
     （真机截图里那次重叠）。 */
  container-type: inline-size;
}
/* 宽度**写死**成样张那一栏的宽（248px ＝ 竖屏图限高 440 时的宽）。不写死会出事：
   `flex: 0 0 auto` 的基准是 max-content，而这一栏里那句说明是一整段中文——按「一行放得下
   整段」算出来是一千多像素，整栏会把右边挤没。写死这一栏，段子在栏内自己折行。 */
.tl-rail { flex: 0 0 248px; width: 248px; }
/* 248（样张）+16（间距）+300（右栏最小宽）＝564；留一点余量取 600。
   低于这个宽度时两栏折成上下两行，这里**不能** sticky。 */
@container (min-width: 600px) {
  .tl-rail { position: sticky; top: 0; }
}
/* 栏名与那句说明在这个宽度里竖着排（横排时标题要占掉一半，说明被挤成一条条窄的） */
.tl-rail .lib-head { display: block; }
.tl-rail .lib-head b { display: block; margin-bottom: 6px; }
.tl-cols { flex: 1 1 420px; min-width: 300px; }
/* 右栏的卡比别处窄一档（200 而不是 220）。样张那一栏固定吃掉 264px，沿用 220 的话
   常见的抽屉宽度（~950px）下右栏只剩两列——卡片总高反而比原来竖排时还高，
   分栏就白分了。200px 底下同样宽度能排三列，而行数才是这一屏要省的东西。
   代价说清楚：卡片窄一点、说明多折一行；档名（如 zh-CN-XiaoxiaoNeural）仍放得下。 */
.tl-cols .lib-grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }

/* 文字库：字幕样张。图来自渲染容器。
   尺寸**按图自己的比例**收（只压高度），不写死宽高比也不裁：样张是竖屏的，
   若按 16:9 装进去会在两侧留一片黑，看着像字形被压扁了；写死比例以后样张换成
   横屏又会再错一次——所以只限高，比例交给图片自己说。 */
.tl-preview { margin-bottom: 12px; }
.tl-shot {
  flex: 0 0 auto; max-height: 440px; max-width: 100%; width: auto; height: auto;
  border-radius: 4px; background: #16130f; display: block;
  border: 1px solid var(--line); box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.08);
}
.tl-shot-empty {
  width: 248px; height: 440px; display: flex; align-items: center; justify-content: center;
  text-align: center; padding: 12px; box-sizing: border-box;
  font-family: var(--serif); font-size: 12.5px; letter-spacing: 1px; color: #b9b1a3;
}
/* 卡片说明兼作「换样张」的开关：点它只看图，不动本对话的设定 */
.tl-meta-pick { cursor: pointer; }
.tl-meta-pick:hover b { color: var(--vermilion-deep); }
.tl-meta-pick.on b { color: var(--vermilion-deep); }

/* 文字库：自由取色那一维（高亮词的颜色）。两枚控件＝「跟色板走」与「钉一个色」，
   选中态与卡片同一套笔法（实底朱红／描边高亮），一眼看得出这一维钉没钉。 */
.tl-color { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.tl-color-btn {
  cursor: pointer; padding: 6px 14px; font-family: var(--serif); font-size: 12px;
  letter-spacing: 2px; color: var(--vermilion-deep);
  background: transparent; border: 1px dashed var(--vermilion); border-radius: 4px;
  transition: all 0.18s;
}
.tl-color-btn:hover, .tl-color-btn.sel {
  color: #fff8ec; background: var(--vermilion); border-style: solid;
}
.tl-color-pick {
  display: inline-flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 3px 12px 3px 4px; border-radius: 4px;
  border: 1px solid var(--line); background: rgba(255, 253, 247, 0.9);
  box-shadow: 2px 3px 0 rgba(38, 34, 28, 0.05);
}
.tl-color-pick.sel { border-color: var(--vermilion); box-shadow: 0 0 0 2px rgba(200, 64, 31, 0.18); }
/* 原生取色器的色块自带一圈内边距与白边框，抠掉才和旁边的字底一样齐 */
.tl-color-pick input[type="color"] {
  width: 36px; height: 28px; padding: 0; border: 0;
  background: transparent; cursor: pointer;
}
.tl-color-pick input[type="color"]::-webkit-color-swatch-wrapper { padding: 0; }
.tl-color-pick input[type="color"]::-webkit-color-swatch { border: 0; border-radius: 3px; }
.tl-color-val {
  font-family: var(--serif); font-size: 12px; letter-spacing: 1px; color: var(--ink-soft);
}

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
/* 风格徽章排在标题后面；它自己不要再吃 `margin-left:auto`，否则会把确认按钮挤到它前面。 */
.pi-tag.style { margin-left: 8px; color: var(--vermilion); border-color: var(--vermilion); }

/* 计划确认窄横条（输入框上方） */
.plan-banner {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 14px; margin: 0 0 6px;
  background: var(--paper); border: 1px solid var(--gold); border-radius: 8px;
  box-shadow: 0 2px 8px rgba(200, 64, 31, 0.06);
}
.pb-icon { font-size: 16px; }
.pb-label { font-family: var(--serif); font-weight: 600; font-size: 14px; }
.pb-style { font-size: 11.5px; color: var(--vermilion); padding: 1px 7px;
            border: 1px solid var(--vermilion); border-radius: 10px; }
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
.pp-ver-style { font-style: normal; opacity: 0.7; font-size: 11px; }
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
.pp-params { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }
.pp-param { font-size: 11px; color: var(--ink-light); }
.pp-param em { font-style: normal; border: 1px solid currentColor; border-radius: 3px;
              padding: 0 3px; margin-right: 4px; font-size: 10px; }
.pp-param.pinned { color: var(--gold); }
.pp-param i { font-style: normal; color: var(--ink-soft); }

/* 计划确认弹窗：遮罩 + 居中卡片 */
.plan-modal-mask {
  position: fixed; inset: 0; z-index: 30;
  background: rgba(38, 34, 28, 0.45);
  display: flex; align-items: center; justify-content: center;
  padding: 24px; backdrop-filter: blur(2px);
}
/* 内嵌提问卡：不遮罩、不挡上下文，作为对话流里一条消息渲染 */
.qc-inline {
  display: flex; flex-direction: column; gap: 10px;
  border: 1px solid var(--line); border-left: 4px solid var(--gold);
  border-radius: 12px; background: var(--paper);
  padding: 14px 16px; margin-top: 4px;
  box-shadow: 0 4px 16px rgba(38, 34, 28, 0.12);
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
.qc-board { margin: 8px 0; padding-left: 0; list-style: none; max-height: 220px; overflow-y: auto;
  font-size: 13px; border: 1px solid var(--line, #e5e1d8); border-radius: 8px; }
.qc-board li { display: flex; gap: 8px; align-items: baseline; padding: 5px 10px;
  border-bottom: 1px solid var(--line, #eee); }
.qc-board li:last-child { border-bottom: none; }
.qc-board b { flex: none; color: var(--ink-soft); font-weight: 600; min-width: 2.4em; }
.qc-board-text { flex: 1; }
.qc-thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px;
  margin: 8px 0; max-height: 340px; overflow-y: auto; }
.qc-thumb { margin: 0; font-size: 11px; line-height: 1.35; }
.qc-thumb img, .qc-thumb-none { width: 100%; height: 150px; object-fit: contain; border-radius: 6px;
  border: 1px solid var(--line, #e5e1d8); cursor: zoom-in; background: #f3efe6; }
.qc-thumb-none { display: flex; align-items: center; justify-content: center; color: var(--ink-soft); cursor: default; }
.qc-thumb figcaption { margin-top: 3px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; }
.qc-board em { flex: none; font-style: normal; color: var(--ink-soft); font-size: 12px; }
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
  .brand-text, .conv .t, .conv .x, .rail-username, .conv-section-label, .new .lbl { display: none; }
  .new { padding: 10px 0; font-size: 18px; }
  .paper, .head, .composer, .attach-bar, .link-bar { padding-left: 18px; padding-right: 18px; }
  .drawer { width: 100%; }
}

/* —— 设置:双模型 —— */
.set-head {
  font-family: var(--serif); font-size: 13px; letter-spacing: 3px; color: var(--vermilion-deep);
  margin: 14px 0 6px; padding-bottom: 4px; border-bottom: 1px solid var(--line);
  width: 100%;
}
.set-head .set-v, .set-head .set-note, .set-head .bad {
  font-family: var(--sans); letter-spacing: 0; font-size: 12px;
}

/* —— 登录门禁 —— */
.login-mask {
  position: fixed; inset: 0; z-index: 200;
  background: linear-gradient(160deg, #211d18, #2b2620 55%, #35302a);
  display: flex; align-items: center; justify-content: center;
}
.login-card {
  width: min(360px, calc(100vw - 40px));
  background: rgba(255, 253, 247, 0.97); border-radius: 14px;
  padding: 34px 30px 26px; text-align: center;
  box-shadow: 0 18px 60px rgba(0, 0, 0, .45);
  display: flex; flex-direction: column; gap: 10px;
}
.login-seal { margin: 0 auto 4px; }
.login-card h1 { font-family: var(--serif); font-size: 20px; color: var(--ink); margin: 0; }
.login-sub { font-size: 12px; color: #8b949e; margin: 0 0 8px; }
.login-card input {
  padding: 9px 12px; border: 1px solid #d0d7de; border-radius: 8px;
  font-size: 14px; width: 100%; box-sizing: border-box;
}
.login-btn {
  padding: 10px; border: none; border-radius: 8px; cursor: pointer;
  background: var(--vermilion); color: #fff9ef; font-size: 15px; letter-spacing: 4px;
}
.login-btn:disabled { opacity: .45; cursor: default; }
.login-err { color: #cf222e; font-size: 12.5px; }
.login-switch { font-size: 12.5px; color: #656d76; }
.login-switch a { color: #0969da; cursor: pointer; text-decoration: none; }

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
.tt-copy.pencil { color: #0969da; font-weight: 600; opacity: 0.85;
  font-size: 13px; line-height: 1; }
.td-clickable { cursor: pointer; transition: color .15s, text-decoration-color .15s; }
.td-clickable:hover { color: var(--vermilion-deep, #c8401f); text-decoration: underline; }
.cat-manage {
  font-size: 11px; color: var(--ink-soft, #6e7781); background: none;
  border: none; cursor: pointer; padding: 2px 6px; border-radius: 4px;
  transition: color .15s, background .15s;
}
.cat-manage:hover { color: var(--vermilion-deep, #c8401f); background: rgba(200,64,31,.08); }
.td-intro {
  font-size: 11.5px; color: var(--ink-soft); opacity: 0.72; line-height: 1.5;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.td-title { display: flex; align-items: center; gap: 6px; min-width: 0; }
.td-title b { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.td-title .tt-copy { flex: 0 0 auto; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(31, 35, 40, .45);
  display: flex; align-items: center; justify-content: center; z-index: 90;
}
.modal-card {
  width: min(680px, calc(100vw - 32px)); max-height: min(80vh, 720px);
  overflow: auto; background: #fff; border-radius: 12px; padding: 16px 18px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, .22);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; font-size: 15px;
}
.modal-body { display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
.modal-body input, .modal-body select, .modal-body textarea {
  padding: 6px 9px; border: 1px solid #d0d7de; border-radius: 6px;
  font-size: 13px; width: 100%; box-sizing: border-box;
}
.modal-body textarea { font-family: Consolas, "Courier New", monospace; line-height: 1.5; }
.kv { display: flex; gap: 8px; align-items: baseline; }
.kv > span:first-child { color: #656d76; flex: 0 0 5.5em; }
.kv.top { flex-direction: column; gap: 3px; }
.kv.chk { gap: 6px; align-items: center; }
.kv.chk input { width: auto; }
.param-list { margin: 0; padding-left: 16px; }
.skill-pre {
  margin: 0; padding: 8px; background: #f6f8fa; border: 1px solid #e1e4e8;
  border-radius: 6px; white-space: pre-wrap; word-break: break-word;
  max-height: 260px; overflow: auto; font-size: 12px; line-height: 1.5;
}
.modal-note { margin: 0; color: #656d76; font-size: 12px; }
.lib-empty.bad { color: #cf222e; }
.tool-drawer {
  display: flex; flex-direction: column; flex: 1; min-height: 0;
}
/* 工具库一次只显示一层：根目录 → 分类 / 技能类型 / 服务 → 里面。
   没有常驻左栏，`.td-detail` 就是整块内容区。 */
.td-detail { flex: 1; min-width: 0; overflow-y: auto; padding: 10px 12px; }

/* 文件夹树：技能包 → 包内文件；工具 → 分类；MCP → 服务 → 工具。
   缩进用行内 padding-left（层级深度在脚本里算好），这里只管一行长什么样。 */
.tree-note {
  font-size: 12px; color: #656d76; line-height: 1.75; margin: 2px 0 10px;
}
.tree-note code {
  font-family: ui-monospace, monospace; font-size: 11.5px;
  background: rgba(246, 248, 250, .9); padding: 0 3px; border-radius: 3px;
}
.tree-node { margin-bottom: 2px; }
.tree-row {
  display: flex; align-items: center; gap: 6px; min-height: 27px;
  padding: 3px 5px; border-radius: 4px; cursor: pointer; overflow: hidden;
}
.tree-row:hover { background: rgba(200, 64, 31, .07); }
.tree-row.file { color: var(--ink-soft); }
.tree-row.file:hover { background: rgba(200, 64, 31, .05); }
.tree-caret {
  width: 12px; flex: none; text-align: center; font-size: 11px;
  color: var(--vermilion-deep, #c8401f); opacity: .8;
}
.tree-name {
  font-family: var(--serif); font-size: 13px; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: none; max-width: 46%;
}
.tree-row.file .tree-name { font-weight: 400; }
.tree-sub {
  font-size: 11.5px; color: #8a9099; min-width: 0; flex: 1;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tree-count { margin-left: auto; flex: none; font-size: 11px; color: #8a9099; white-space: nowrap; }
.tree-actions { display: flex; gap: 4px; flex: none; }
.tree-actions .help { padding: 0 5px; font-size: 11px; }
/* 四个库共用同一副骨架：素材 / 音乐 / 文字库的列表也是「文件夹 → 叶子」的行。 */
.lib-tree { display: flex; flex-direction: column; gap: 1px; }
.tree-row.d1 { padding-left: 18px; }
.tree-row.d2 { padding-left: 32px; }
.tree-row.sel { background: rgba(200, 64, 31, .08); }
.tree-row.sel .tree-name { color: var(--vermilion-deep, #c8401f); }
/* 行里塞得下播放器与按钮时排一行；塞不下（音频行）就换到第二行，别把按钮挤没。 */
.tree-row.wrap { flex-wrap: wrap; }
.tree-row.wrap .lib-aud-xs {
  flex: 1 0 100%; width: auto; margin: 2px 0 4px 24px;
}
.tree-check {
  position: relative; width: 14px; height: 14px; flex: none;
  cursor: pointer; display: block;
}
.tree-check input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; }
.tree-check span {
  display: block; width: 14px; height: 14px; border-radius: 3px;
  border: 1px solid var(--line); background: #fffdf7;
}
.tree-check input:checked + span { background: var(--vermilion); border-color: var(--vermilion); }
.tree-check input:checked + span::after {
  content: "✓"; display: grid; place-items: center; height: 100%;
  color: #fff8ec; font-size: 10px;
}
.lib-thumb-xs {
  width: 44px; height: 28px; flex: none; object-fit: cover;
  border-radius: 3px; background: #16130f;
}
.lib-aud-xs { width: 168px; height: 28px; flex: none; }
/* 文件夹标题上的搜索框：标题/按钮都不缩，只让输入框吃剩下的宽度。 */
.tree-row .bgm-search { flex: 1 1 70px; min-width: 60px; height: 22px; }
.tree-row .tree-count { flex: none; }
.tl-color-input {
  width: 26px; height: 20px; padding: 0; border: 1px solid var(--line);
  border-radius: 3px; background: transparent; cursor: pointer;
}

/* —— 四个库共用的「资源管理器」排法：顶部工具条 / 左文件夹 / 右内容。
   文字库多一栏预览（资源管理器右边那块「预览窗格」），窄抽屉里自己收起来。 —— */
.drawer .library.ex-lib { display: flex; flex-direction: column; overflow: hidden; }
.ex-bar {
  display: flex; align-items: center; gap: 8px; flex: none; min-width: 0;
  padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid var(--line);
}
.ex-bar .lib-count { flex: none; }
.ex-bar > .help { flex: none; }
.ex-bar .bgm-search { flex: 1 1 140px; min-width: 90px; height: 24px; }
.ex-view { margin-left: auto; display: flex; gap: 2px; flex: none; }
.ex-vbtn {
  width: 24px; height: 22px; border: 1px solid var(--line); border-radius: 3px;
  background: transparent; color: var(--ink-soft); font-size: 12px; line-height: 1;
  cursor: pointer; display: grid; place-items: center;
}
.ex-vbtn.on {
  border-color: var(--vermilion); color: var(--vermilion-deep, #c8401f);
  background: rgba(200, 64, 31, .07);
}
.ex-body { display: flex; flex: 1; min-height: 0; }
/* 文件夹这一层：整块宽度都给文件夹行，不跟子级同屏。 */
.ex-flist { flex: 1; min-height: 0; overflow-y: auto; padding: 4px 0; }
.ex-frow {
  display: flex; align-items: center; gap: 8px; min-height: 30px;
  padding: 4px 8px; border-radius: 4px; cursor: pointer;
  font-family: var(--serif); font-size: 13px; color: var(--ink);
}
.ex-frow:hover { background: rgba(200, 64, 31, .07); }
.ex-frow .ex-fname { flex: 1; font-weight: 600; }
/* 进到文件夹里面：顶上只有一行「返回」，父级不再出现在旁边。 */
.ex-back {
  display: flex; align-items: center; gap: 6px; min-height: 27px;
  padding: 3px 6px; margin-bottom: 4px; border-radius: 4px; cursor: pointer;
  border-bottom: 1px solid var(--line); font-family: var(--serif); font-size: 12.5px;
  color: var(--ink);
}
.ex-back:hover { background: rgba(200, 64, 31, .06); }
.ex-back .ex-fname { flex: none; }
.ex-back .ex-fcount { margin-left: auto; }
.ex-caret { flex: none; font-size: 11px; color: var(--vermilion-deep, #c8401f); }
.ex-fname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ex-fcount { flex: none; font-size: 11px; color: #8a9099; }
.ex-main { flex: 1; min-width: 0; overflow: auto; padding-bottom: 4px; }
/* 文字库那一屏右边还有预览窗格：左边内容区自己再成一列。 */
.ex-main-text { display: flex; flex-direction: column; }
.ex-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
/* 详细视图：列跟资源管理器一样（名称 / 类型 / 大小 / 时长）。表头钉在滚动区顶上。 */
/* `table-layout: fixed` + 每张表的 colgroup：抽屉就那么宽，让浏览器按列宽排，
   否则「说明」那种长句会把操作列顶出可视区（第一版就是这样，按钮被裁在右边界外）。 */
.ex-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; color: var(--ink-soft); }
.ex-table th {
  position: sticky; top: 0; z-index: 1; text-align: left; white-space: nowrap;
  font-weight: 500; font-size: 11px; letter-spacing: 1px; color: #8a9099;
  padding: 5px 6px; border-bottom: 1px solid var(--line); background: #fdfaf2;
}
.ex-table td { padding: 5px 6px; border-bottom: 1px solid rgba(233, 226, 210, .55); vertical-align: middle; }
.ex-table tr:hover td { background: rgba(200, 64, 31, .04); }
.ex-table tr.sel td { background: rgba(200, 64, 31, .07); }
/* 注意：td 上**不能**改 display（flex/block 会让它脱离表格格子，整行错位——
   第一版就是这样：说明掉到下一行、状态另起一行）。缩略图与名字用 inline-block 对齐。 */
.ex-table .ex-name { white-space: nowrap; }
.ex-table .ex-name > * { vertical-align: middle; }
.ex-table .ex-name .ex-fname {
  display: inline-block; max-width: calc(100% - 28px); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
  color: var(--ink); font-family: var(--serif); font-size: 12.5px;
}
.ex-table .ex-name .lib-thumb-xs { margin-right: 4px; }
.ex-table .ex-name .lib-thumb-xs + .ex-fname { max-width: calc(100% - 52px); }
.ex-sub { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ex-mono { font-family: ui-monospace, monospace; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ex-op { text-align: right; white-space: nowrap; }
.ex-op .help { margin-left: 3px; padding: 0 5px; font-size: 11px; }
.ex-thchk, .ex-tdchk { width: 22px; }
/* 试听播放器单占一行，横跨整张表：详细视图的列都窄，塞进哪一格都会挤掉别的。 */
.ex-play-row td { padding: 4px 6px 8px; background: rgba(200, 64, 31, .03); }
.ex-play-row audio { width: 100%; height: 30px; display: block; }
/* 非图片素材在网格里没有缩略图：用一块同高的深色底写类型，别用图标凑。 */
.ex-thumb-ph {
  display: flex; align-items: center; justify-content: center;
  color: #b9b1a3; font-family: var(--serif); font-size: 12px; letter-spacing: 3px;
}
.ex-card { position: relative; }
.ex-cardmark {
  position: absolute; top: 6px; right: 8px; font-size: 11px;
  color: var(--vermilion-deep, #c8401f);
}
.ex-card .lib-meta span { line-height: 1.5; }
.ex-card-btns { display: flex; gap: 4px; flex-wrap: wrap; }
.ex-card-btns .help { flex: 1 1 auto; padding: 3px 6px; font-size: 11px; }
.ex-audio-card .lib-aud { height: 30px; }
/* 预览窗格：固定右栏，不参与内容滚动。 */
.ex-preview {
  flex: 0 0 240px; width: 240px; overflow-y: auto;
  border-left: 1px solid var(--line); margin-left: 10px; padding-left: 10px;
}
.ex-prev-head { display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
.ex-prev-head b { font-family: var(--serif); font-size: 13px; letter-spacing: 1px; }
.ex-prev-head span { font-size: 11px; color: var(--ink-soft); }
.ex-preview .tl-shot { max-height: 380px; width: 100%; object-fit: contain; }
.ex-preview .tl-shot-empty { width: 100%; height: 300px; }
.ex-prev-note { margin: 8px 0 0; font-size: 11px; line-height: 1.7; color: var(--ink-soft); }
.tool-drawer.drop-on { outline: 2px dashed var(--accent, #8a6a3a); outline-offset: -6px; }
.file-pre {
  max-height: 56vh; overflow: auto; margin: 6px 0 0; padding: 10px;
  background: rgba(246, 248, 250, .85); border: 1px solid var(--line); border-radius: 4px;
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
}
.file-img { max-width: 100%; max-height: 52vh; display: block; margin: 0 auto 8px; border-radius: 4px; }
</style>
