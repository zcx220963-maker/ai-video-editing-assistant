<script setup>
// 两条剪辑链共用的「选区改」编辑器（S3 轨道 + 命中层 + 框选，S5 局部改）。
//
// 为什么自己画这一层而不是嵌第三方剪辑器：要的是「鼠标框住屏幕上这一块，改的就是这一块」，
// 而这件事只有我们的命中表（出片时量好的 元素框 ↔ 分镜字段 凭据）说得出对应关系。
// 第三方剪辑器只认时间线与素材，框不出「屏幕上这一块属于 /shots/0/fields/半径/value」。
//
// 两种形态由**表自己声明**，不靠前端猜：
// * 图形科普片·代码镜（motion-code-hitmap/1 + chain=remotion）：盒是母版在探针帧量回来的
//   **成片像素**（表里写着 coord=output_px），条目只有 `pointer` 与字段声明，控件的类型/范围/
//   档位由条目自带的 fields schema 说（闸⑥ 那条通道）；
// * 口播/素材片（segment-hitmap/1 + hit_mode=track）没有版式可量，每一段本来就是
//   「第 s 秒到第 e 秒」→ 没有空间命中层，选段只能在时间线上点，表单是那一段的字段。
// 代码镜那一份的**形状差异只在下面那个适配器里处理**（盒归一、补 id 与 field 两栏），
// 框选 / 表单 / 提交三条码路吃的都是适配后的条目 —— 在三个位置各写一个 if 的话，
// 漏掉的那一处就是「按旧框改错字段」，而那正是命中表要消灭的失败模式。
// 两条链的口实现同一份（/motion/* 与 /timeline/* 落到同一个读表/取帧/提交函数），
// 差别只在提交时的键：那条链收 edits/shot_sets/remove_shots/reorder，这条只收 edits
// ——它的合同不改总长也不换序（画面与口播句子的对应是基准）。
// 交给哪条链的局部改节点由**服务端读 base 那一版的表里的 chain** 决定，前端不猜。
//
// 全部改动拼成一次 POST，提交后轮询 GET /render_status——拒绝也落在轮询端的 error
// 正文里，HTTP 状态码不作判据。
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";

const props = defineProps({
  artifactId: { type: String, required: true },
  convId: { type: String, default: "" },
  mediaUrl: { type: String, default: "" },
  title: { type: String, default: "" },
});
const emit = defineEmits(["close", "patched", "ask"]);

// 与 App.vue 同一份凭证：浏览器只留 token，身份由服务端反查。
const TOKEN_KEY = "ca.token";
const hdr = (extra = {}) => ({
  Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY) || ""}`, ...extra,
});
const qs = (o) => Object.entries(o)
  .filter(([, v]) => v !== "" && v !== null && v !== undefined)
  .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

// ---- 编辑包：这一版的命中表 ----
const artifact = ref(props.artifactId);
const chain = reactive([{ id: props.artifactId, url: props.mediaUrl }]);
const hm = ref(null);
const loadErr = ref("");
const loading = ref(true);
// 两个读表口在服务器端是同一份 bundle，先打哪个都拿得到；表一到手就以 hit_mode 定
// 形态与写口——真正分岔的是提交补丁（那条链收 edits/shot_sets/remove_shots/reorder，
// 这条只收 edits）与取帧的段号口径。
const api = ref("/motion");
const isTrack = computed(() => hm.value?.hit_mode === "track");

async function loadHitmap(id) {
  loading.value = true; loadErr.value = ""; hm.value = null;
  try {
    const r = await fetch(`${api.value}/hitmap?${qs({ artifact_id: id,
                                                      conv_id: props.convId })}`,
                          { headers: hdr() });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    hm.value = await r.json();
    api.value = isTrack.value ? "/timeline" : "/motion";
    resetEdits();
    await loadFrames(id, hm.value);
  } catch (e) {
    loadErr.value = `读不到这一版的命中表：${e.message}`;
  } finally {
    loading.value = false;
  }
}

// ---- 代表帧：取字节转 blob（<img> 直链带不上 Bearer）----
const frameUrls = reactive({});        // `${artifact}|${shot}` → objectURL
const frameMiss = reactive({});
async function frameUrl(aid, sid) {
  const key = `${aid}|${sid}`;
  if (key in frameUrls || key in frameMiss) return frameUrls[key] || "";
  try {
    const r = await fetch(`${api.value}/frame?${qs({ artifact_id: aid, shot: sid,
                                                    conv_id: props.convId })}`,
                          { headers: hdr() });
    if (!r.ok) { frameMiss[key] = true; return ""; }
    frameUrls[key] = URL.createObjectURL(await r.blob());
    return frameUrls[key];
  } catch {
    frameMiss[key] = true;
    return "";
  }
}
async function loadFrames(aid, table) {
  // 口播链只有画面段有代表帧（表里那行的 frame 键为空就是没有）；图形科普片每镜都有。
  const rows = table.timeline || [];
  const want = table.hit_mode === "track" ? rows.filter((t) => t.frame) : rows;
  await Promise.all(want.map((t) => frameUrl(aid, t.id)));
}

// ---- 指针：与后端 hitmap._segs 同口径（先解 ~1 再解 ~0）----
const segs = (p) => p.split("/").slice(1).map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
function resolvePath(doc, ss) {
  let cur = doc;
  for (const s of ss) {
    if (cur == null) return undefined;
    if (Array.isArray(cur)) {
      // 代码镜的字段指针中段是**按 name 寻址的数组**（`/shots/i/fields/<name>/value`），
      // 与后端 code_patch 同口径。只认下标的话这一格永远查不到值，〈字段〉页就会把
      // 「原值 240」说成「未设置 · 渲染按默认 null」、滑杆停在量程左端。
      const i = Number(s);
      cur = (Number.isInteger(i) && String(i) === s)
        ? cur[i]
        : cur.find((el) => String(el?.name ?? "") === s);
    } else {
      cur = cur[s];
    }
  }
  return cur;
}
const timeline = computed(() => hm.value?.timeline || []);
const shots = computed(() => hm.value?.shots || {});
const baseIds = computed(() => timeline.value.map((t) => t.id));
const idxOf = (sid) => baseIds.value.indexOf(sid);
// 指针的根：那条链指向分镜 spec，这条指向表里嵌的那一版时间线（doc）。
const docRoot = computed(() => (isTrack.value ? hm.value?.doc : hm.value?.spec) || {});
const rowOf = (sid) => timeline.value.find((t) => t.id === sid) || null;
const shotBase = (sid) => {
  if (!isTrack.value) return (hm.value?.spec?.shots || [])[idxOf(sid)] || {};
  const r = rowOf(sid);
  return r ? ((docRoot.value[r.track] || [])[r.index] || {}) : {};
};
const baseValue = (p) => resolvePath(docRoot.value, segs(p));

// ---- 播放与时间线 ----
const stageEl = ref(null), videoEl = ref(null), trackEl = ref(null);
const playUrl = ref(props.mediaUrl);
const cur = ref(0), playing = ref(false), muted = ref(true);
const aspect = computed(() => (hm.value ? `${hm.value.width} / ${hm.value.height}` : "9 / 16"));
// 「第几镜」在两种形态下指的是不同东西：轨道形态整条表就是镜序，但同一秒上画面/声音/
// 字幕各有一行，跟着整条表跳会让标题来回换段，所以只看画面轨；空间形态改用投影后的
// 镜行——用户拖过时长或换了序，播放头归属要跟着新的秒数走，而不是停在出片那一刻。
const startOf = (r) => Number(r.start_sec ?? r.start) || 0;
const laneRows = computed(() => (isTrack.value
  ? timeline.value.filter((r) => r.track === "events") : projected.value.rows));
const curIdx = computed(() => {
  const tl = laneRows.value;
  if (!tl.length) return 0;
  let i = 0;
  tl.forEach((t, k) => { if (cur.value + 0.001 >= startOf(t)) i = k; });
  return i;
});
const curSid = computed(() => laneRows.value[curIdx.value]?.id || "");
// 右侧面板对谁说话：轨道形态是用户点上的那一段，框选形态是播放头那一镜。
const focusSid = computed(() => (isTrack.value ? selectedRow.value : curSid.value));
// ---- 代码镜那份表的形状差异**只在这里抹平** ----
// 表里的盒是母版在探针帧量回来的**成片像素**（coord=output_px），条目也只有 pointer
// 没有 id/field 两栏；口播轨那份压根没有盒（一段就是一整幅画面）。
// 不在这里补齐，框选、表单、提交三条码路就要各写一个 if；漏掉的那一处就是「按旧框
// 改错字段」，而那正是命中表要消灭的失败模式。
const isCode = computed(() => hm.value?.chain === "remotion");
function adaptCodeEntry(e, sid, tbl) {
  const w = Number(tbl?.width) || 1, h = Number(tbl?.height) || 1;
  const px = Array.isArray(e.box) ? e.box : null;
  const isField = e.kind === "field";
  return {
    ...e,
    id: `${sid}/${isField ? e.name : "@shot"}`,
    // 整镜那条对应的是 code/text/source **三条**指针，不是一格可写的字段，所以 field
    // 留空、不进框选层；它的改写入口在〈代码〉与〈文案·出处〉两页。
    field: isField ? e.pointer : "",
    box: px ? { x: px[0] / w, y: px[1] / h, w: px[2] / w, h: px[3] / h } : null,
    text: typeof e.value === "string" ? e.value : "",
    // pointer 来自分镜契约，不是按文字近似对上的，所以没有「请核对」那一档。
    confidence: "exact",
    title: `${isField ? e.label || e.name : e.label} · ${e.pointer}`,
  };
}
const tableEntries = computed(() => {
  const out = {};
  Object.entries(shots.value || {}).forEach(([sid, tbl]) => {
    out[sid] = ((tbl || {}).entries || [])
      .map((e) => (isCode.value ? adaptCodeEntry(e, sid, tbl) : e));
  });
  return out;
});
const entries = computed(() => tableEntries.value[focusSid.value] || []);
// 代码镜铺满全幅的整镜框不进框选层：留着它，每次框选都多命中一个没有字段的空行。
const spaceEntries = computed(() => (isCode.value
  ? entries.value.filter((e) => e.kind === "field" && e.box) : entries.value));
const codeFields = computed(() => (isCode.value
  ? entries.value.filter((e) => e.kind === "field") : []));
// 播放头用像素而不是百分比：整条时间线在一个横向滚动容器里，缩放后两者会漂。
const headX = computed(() => cur.value * pps.value);
// ---- 统一时间线：一条秒尺 + 若干条 lane，两条链共用同一套坐标 ----
// 为什么不用百分比铺块：百分比放不进缩放，也滚不起来，而「拖到第几帧」要的是把
// 1/fps 秒放大到人眼看得见的宽度。两条链的段都只给 start_sec/sec，坐标口径因此一致。
const viewW = ref(880);
const zoomPx = ref(0);                 // 0 = 按容器宽度铺满
const scrollEl = ref(null);
// 秒尺的「铺满」按容器**实宽**算：这块面板会被左侧会话栏的收放挤压，写死 880 的话
// 缩放比例与滚动的边界都会跟画面对不上。
let ro = null;
// 不用 onMounted：这一整块在 v-else-if="hm" 里，表没到就挂不上，onMounted 量到的是 null。
const setScrollEl = (el) => {
  scrollEl.value = el;
  ro?.disconnect(); ro = null;
  if (!el || typeof ResizeObserver === "undefined") return;
  viewW.value = el.clientWidth || viewW.value;
  ro = new ResizeObserver(() => { viewW.value = el.clientWidth || viewW.value; });
  ro.observe(el);
};
const setTrackEl = (el) => { trackEl.value = el; };
// 改动账的四个容器声明在这里而不是下面：``watch(curSid)`` 在 setup 期间就取一次值，
// 而 curSid→laneRows→projected 要读镜序与删镜清单——晚一行就是渲染期 TDZ 报错，
// 整个剪辑侧栏空挂（真机踩过：侧栏宽度在、内容一片白）。
const overrides = reactive({});
const deepEdits = reactive({});
const removals = ref([]);
const order = ref([]);
// 图形科普片那条链没有「时长表」：每镜一段配音、整片一条背景乐，长度是算出来的。
// 投影跟着当前编辑走（镜序、时长、删镜），所以拖完右边缘立刻在秒尺上看得到结果。
const projected = computed(() => {
  if (isTrack.value) {
    // 口播/素材链只许改字段、不许改总长，时长表里画面/声音/字幕/配乐是**同一秒上的四行**，
    // 把它们各段的秒数再累一遍会把 6 秒的片子画成 21 秒（真机踩过：刻度走到 20s、播放头拖不满轴）。
    // 秒尺因此只看画面轨，其余轨贴在同一个坐标上，不参与轴长。
    const rows = timeline.value.filter((r) => r.track === "events").map((r) => ({
      sid: r.id, id: r.id, i: Number(r.index) || 0, start: startOf(r),
      sec: Number(r.sec) || 0, text: String((shotBase(r.id) || {}).text || ""),
      speech: Number(r.speech_sec) || 0,
    }));
    const t = rows.reduce((a, r) => a + r.sec, 0);
    return { rows, total: Math.round(t * 1000) / 1000 };
  }
  const rows = [];
  let t = 0;
  order.value.forEach((sid) => {
    if (removals.value.includes(sid)) return;
    const sec = effSec(sid).sec;
    const base = shotBase(sid) || {};
    const r = rowOf(sid) || {};
    rows.push({ sid, id: sid, i: idxOf(sid), start: Math.round(t * 1000) / 1000, sec,
                text: String(base.text || ""), speech: Number(r.speech_sec) || 0 });
    t += sec;
  });
  return { rows, total: Math.round(t * 1000) / 1000 };
});
const timelineDur = computed(() => Math.max(
  0.1, Number(hm.value?.duration) || 0, projected.value.total));
const pps = computed(() => zoomPx.value || Math.max(
  3, (viewW.value - 78) / timelineDur.value));
const laneW = computed(() => Math.max(viewW.value,
                                      timelineDur.value * pps.value + 10));
const xOf = (sec) => (Number(sec) || 0) * pps.value;
// 刻度档位：挑「相邻两条至少隔开 56px」里最小的那一档，缩放时自动从 1/4 秒走到 5 分钟
const TICK_STEPS = [0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
const ticks = computed(() => {
  const st = TICK_STEPS.find((s) => s * pps.value >= 56) || 900;
  const out = [];
  for (let t = 0; t <= timelineDur.value + 1e-6; t += st) {
    out.push({ sec: Math.round(t * 1000) / 1000, x: xOf(t) });
  }
  return out;
});
const fmtSec = (v) => (v >= 60
  ? `${Math.floor(v / 60)}:${String(Math.round(v % 60)).padStart(2, "0")}`
  : `${v.toFixed(v % 1 ? 2 : 0)}s`);
function zoomBy(k) {
  zoomPx.value = Math.max(2, Math.min(1200, pps.value * k));
}
const fps = computed(() => Number(hm.value?.fps) || 25);
// 逐帧步进按「第几帧」取整再回秒：直接 ±0.04 会累积成小数漂移，第 25 帧落不到整码上。
function stepFrames(n) {
  seek((Math.round(cur.value * fps.value) + n) / fps.value);
}
function onScrubDown(ev) {
  if (ev.button !== 0 || !ev.currentTarget) return;
  const move = (e) => {
    const r = ev.currentTarget.getBoundingClientRect();
    seek(Math.max(0, Math.min(timelineDur.value, (e.clientX - r.left) / pps.value)));
  };
  move(ev);
  const up = () => window.removeEventListener("pointermove", move);
  window.addEventListener("pointermove", move, { once: false });
  window.addEventListener("pointerup", up, { once: true });
  ev.currentTarget.setPointerCapture?.(ev.pointerId);
}
// 三条 lane 的来历不同，但形状一致：label + 若干按秒定位的块。
// 图形片的「旁白」与「配乐」是从分镜合成出来的——界面上要的仍是「画面在上、
// 声音在下、字幕贴着同一条时间轴」，两条链共用同一个编辑动作。
const lanes = computed(() => {
  if (isTrack.value) {
    return (hm.value?.tracks || []).map((t) => ({
      key: t.key, label: t.label,
      bars: timeline.value.filter((r) => r.track === t.key).map((r) => ({
        sid: r.id, start: r.start_sec, sec: r.sec, label: r.label, kind: "row",
        frame: frameUrls[`${artifact.value}|${r.id}`] || "",
      })),
    })).filter((l) => l.bars.length);
  }
  const rows = projected.value.rows;
  const out = [{
    key: "shots", label: "画面",
    bars: rows.map((r) => ({
      sid: r.sid, start: r.start, sec: r.sec, kind: "shot",
      label: `镜 ${r.i + 1} · ${r.sid}`,
      frame: frameUrls[`${artifact.value}|${r.sid}`] || "",
    })),
  }];
  const voice = rows.filter((r) => r.text).map((r) => ({
    sid: r.sid, start: r.start, sec: Math.max(0.1, r.speech || r.sec), kind: "voice",
    label: r.text, frame: "",
  }));
  if (voice.length) out.push({ key: "voice", label: "旁白", bars: voice });
  const bg = (hm.value?.spec?.bgm) || {};
  if (bg.ref) {
    out.push({ key: "bgm", label: "配乐", bars: [{
      sid: "bgm", start: 0, sec: timelineDur.value, kind: "bgm",
      label: `音量 ${bg.volume}${bg.duck ? " · 人声起时压低" : ""}`, frame: "",
    }] });
  }
  return out;
});
const barStyle = (b) => ({ left: `${xOf(b.start)}px`,
                          width: `${Math.max(b.kind === "shot" ? 54 : 22, xOf(b.sec))}px` });
const barIsNow = (b) => cur.value + 0.001 >= (b.start || 0)
  && cur.value < (b.start || 0) + (b.sec || 0);
function onTime() { if (videoEl.value) cur.value = videoEl.value.currentTime; }
function toggle() {
  const v = videoEl.value; if (!v) return;
  if (v.paused) { v.play(); playing.value = true; } else { v.pause(); playing.value = false; }
}
function seek(sec) {
  const v = videoEl.value; if (!v) return;
  v.currentTime = Math.max(0, Math.min(sec, (hm.value?.duration || 0) - 0.05));
  cur.value = v.currentTime;
}

// ---- 框选 / 点选：命中判定走 JS 盒，与框选同一条码路（不靠 DOM 事件）----
// 轨道形态（口播/素材）没有这一层：那一版的段本来就是「第 s 秒到第 e 秒的一整幅画面」
// 或「底部一条字幕带」，画面上没有任何一块可以框——选段改成在时间线上点。
const selected = ref([]);
const selectedRow = ref("");           // 轨道形态：点中的是哪一段
const marquee = ref(null);
const tab = ref("cells");
const bgmEdit = ref({ volume: null, duck: null }); // 配乐改：null=未改
let dragStart = null;
function pickRow(sid) {
  selectedRow.value = sid;
  // 一段能改的字段全在那一行的条目里（timeline_edit 每条可改字段一条），一次选上
  selected.value = (tableEntries.value[sid] || []).map((e) => e.id);
  tab.value = "cells";
}
const norm = (ev) => {
  const r = stageEl.value.getBoundingClientRect();
  return { x: (ev.clientX - r.left) / r.width, y: (ev.clientY - r.top) / r.height,
           px: ev.clientX - r.left, py: ev.clientY - r.top };
};
const boxStyle = (b) => ({ left: `${b.x * 100}%`, top: `${b.y * 100}%`,
                          width: `${b.w * 100}%`, height: `${b.h * 100}%` });
const visibleBoxes = computed(() => spaceEntries.value.map((e) => ({
  ...e, style: boxStyle(e.box || {}), on: selected.value.includes(e.id),
  dead: !e.field || e.confidence === "none",
})));
const overlap = (a, b) => !(a.x + a.w < b.x || b.x + b.w < a.x
                            || a.y + a.h < b.y || b.y + b.h < a.y);

function onStageDown(ev) {
  if (ev.button !== 0 || !stageEl.value || isTrack.value) return;
  dragStart = { ...norm(ev), moved: false };
  window.addEventListener("pointermove", onStageMove);
  window.addEventListener("pointerup", onStageUp, { once: true });
}
function onStageMove(ev) {
  if (!dragStart) return;
  const p = norm(ev);
  if (!dragStart.moved && Math.abs(p.px - dragStart.px) < 4 && Math.abs(p.py - dragStart.py) < 4) return;
  dragStart.moved = true;
  marquee.value = { x: Math.min(dragStart.x, p.x), y: Math.min(dragStart.y, p.y),
                    w: Math.abs(p.x - dragStart.x), h: Math.abs(p.y - dragStart.y) };
}
function onStageUp(ev) {
  window.removeEventListener("pointermove", onStageMove);
  const start = dragStart;
  dragStart = null;
  const mq = marquee.value;
  marquee.value = null;
  if (!start) return;
  if (mq && (mq.w > 0.01 || mq.h > 0.01)) {
    selected.value = spaceEntries.value.filter((e) => overlap(mq, e.box || {})).map((e) => e.id);
    tab.value = "fields";
    return;
  }
  // 没拖动 = 单击：多个框叠在一起时取最小的（父块会把子文本再回显一遍）
  const p = norm(ev);
  const hit = spaceEntries.value.map((e) => ({ e, b: e.box || {} }))
    .filter(({ b }) => b.x <= p.x && p.x <= b.x + b.w && b.y <= p.y && p.y <= b.y + b.h)
    .sort((a, c) => (a.b.w * a.b.h) - (c.b.w * c.b.h));
  selected.value = hit.length ? [hit[0].e.id] : [];
  tab.value = "fields";
}
// 换镜即清选区：选中的是「这一帧上的这一块」，跳到别的镜还留着就是拿旧框改新字。
// 轨道形态不清：那里的选区是点段点出来的，播放头路过别的时间不改变「在改哪一段」。
watch(curSid, () => { if (!isTrack.value) selected.value = []; });

// 选中条目 → 按字段指针归组（一行的 key 与 value 常常同指一个 panel 键）
const groups = computed(() => {
  const want = new Set(selected.value);
  const by = new Map();
  for (const e of spaceEntries.value) {
    if (!want.has(e.id)) continue;
    const k = e.field || `@none:${e.id}`;
    if (!by.has(k)) by.set(k, { pointer: e.field || "", key: k, hits: [] });
    by.get(k).hits.push(e);
  }
  return [...by.values()];
});
// 〈字段〉页那一栏吃的是**这一镜全部旋钮**，不是选区：框住某个元素只是把那一行点亮，
// 没点到的旋钮一样要能改（spec §6：表吐出 fields schema，界面自动出控件）。
const cellGroups = computed(() => (isCode.value
  ? codeFields.value.map((f) => ({ pointer: f.pointer, key: f.id, hits: [f] }))
  : groups.value));

// ---- 改动账 ----
// overrides：〈文案·出处〉表单覆盖的顶层栏（落在这些栏上的指针改动也写进同一份，两处不会口径不一）
// deepEdits：表单没覆盖的指针（镜头代码、字段注入值、出处整份）——选区改的主场
const FORM_FIELDS = ["text", "highlight", "min_duration_sec"];
function resetEdits() {
  Object.keys(overrides).forEach((k) => delete overrides[k]);
  Object.keys(deepEdits).forEach((k) => delete deepEdits[k]);
  removals.value = [];
  order.value = [...baseIds.value];
  selected.value = [];
  selectedRow.value = "";
  fieldScope.value = "";
  syncAsk.value = false;
  bgmEdit.value = { volume: null, duck: null };
  // 〈字段〉〈代码〉〈文案·出处〉是代码镜那三页，口播版只有「点一段改它的字段」那一页。
  tab.value = isTrack.value ? "cells" : "fields";
}
// 指针 → 表里那条条目：轨道形态提交时要照它的 segment_id 一起交（闸门拿段号核对
// 这张表量的是不是当前这一版），字段标签也从这里取。
const entryByPointer = computed(() => {
  const m = {};
  Object.values(tableEntries.value).forEach((list) =>
    list.forEach((e) => { if (e.field) m[e.field] = e; }));
  return m;
});
function overrideFor(sid) {
  if (!overrides[sid]) {
    const s = shotBase(sid) || {};
    const pick = {};
    FORM_FIELDS.forEach((f) => { if (f in s) pick[f] = s[f]; });
    overrides[sid] = JSON.parse(JSON.stringify(pick));
  }
  return overrides[sid];
}
function setEdit(p, value) {
  if (isTrack.value) { deepEdits[p] = value; return; }
  const ss = segs(p);
  const sid = baseIds.value[Number(ss[1])];
  const field = ss[2];
  if (sid && FORM_FIELDS.includes(field)) {
    const o = overrideFor(sid);
    if (ss.length === 3) o[field] = value;
    else if (field === "highlight") (o.highlight ||= [])[Number(ss[3])] = value;
    else (o[field] ||= {})[ss[3]] = value;
    delete deepEdits[p];
  } else {
    deepEdits[p] = value;
  }
}
function editValue(p) {
  if (p in deepEdits) return deepEdits[p];
  if (isTrack.value) return baseValue(p);
  const ss = segs(p);
  const sid = baseIds.value[Number(ss[1])];
  const o = sid ? overrides[sid] : null;
  if (!o || !FORM_FIELDS.includes(ss[2])) return baseValue(p);
  if (ss.length === 3) return o[ss[2]];
  if (ss[2] === "highlight") return (o.highlight || [])[Number(ss[3])];
  return (o[ss[2]] || {})[ss[3]];
}
/** 这一镜表单里真正变了的那几栏（与原值逐栏比，没变的不进补丁）。 */
function setFor(sid) {
  const o = overrides[sid];
  if (!o) return {};
  const s = shotBase(sid) || {};
  const set = {};
  FORM_FIELDS.forEach((f) => {
    if (!(f in o)) return;
    if (JSON.stringify(o[f]) !== JSON.stringify(s[f] === undefined ? null : s[f])) set[f] = o[f];
  });
  return set;
}
const changedShots = computed(() => {
  const touched = new Set();
  if (isTrack.value) {
    // 轨道形态的指针中段是「该轨内的下标」，不能拿整条表的镜序去查——那是别的段
    Object.keys(editAccount.value).forEach((p) => {
      const sid = String(entryByPointer.value[p]?.segment_id || "");
      if (sid) touched.add(sid);
    });
    return touched;
  }
  Object.keys(editAccount.value).forEach((p) => {
    const sid = baseIds.value[Number(segs(p)[1])];
    if (sid) touched.add(sid);
  });
  order.value.forEach((sid) => { if (Object.keys(setFor(sid)).length) touched.add(sid); });
  return touched;
});
const dirty = computed(() => Object.keys(deepEdits).length > 0
  || removals.value.length > 0
  || order.value.join(" ") !== baseIds.value.join(" ")
  || changedShots.value.size > 0
  || bgmEdit.value.volume !== null
  || bgmEdit.value.duck !== null);

// 时长：拖右边缘改的就是 min_duration_sec。有旁白时镜头长度归真实语音时长，
// 这一栏只剩「下限」的意思——所以拖到配音秒数以下不会有任何效果，这里如实拦住。
function effSec(sid) {
  const tl = timeline.value[idxOf(sid)] || {};
  const floor = Math.max(Number(tl.speech_sec) || 0, 0.5);
  const o = overrides[sid];
  // overrides 是表单一渲染就预填的（overrideFor 把当前值抄进去好做对比），所以
  // 「这一栏在 overrides 里」不等于「用户拖过」——照 presence 判的话，只是打开
  // 〈文案·出处〉看一眼，秒尺就会把实测 4.36s 换成下限 2.0s 并报「合计约 4.0s」。
  const base = Number(shotBase(sid).min_duration_sec);
  const dragged = o && "min_duration_sec" in o && Number(o.min_duration_sec) !== base;
  const want = dragged ? Number(o.min_duration_sec) : (tl.sec || 0);
  return { want: want || 0, floor, sec: Math.max(want || 0, floor) };
}
const estTotal = computed(() => projected.value.total);
const removedShots = computed(() => order.value.filter((s) => removals.value.includes(s)));

// 时间线上的拖拽（只有图形片那条链吃这三样——那条链的合同允许换序与删镜，
// 口播链不许：画面与口播句子的对应是基准，挪一次就得回计划卡重新规划）：
// 块身拖 = 换序，右边缘拖 = 改时长，纯点击 = 定位到那一镜。
let trackDrag = null;
function blockRects() {
  return [...(trackEl.value?.querySelectorAll(".me-block") || [])]
    .map((el) => el.getBoundingClientRect());
}
function onBarDown(ev, b, laneKey) {
  if (ev.button !== 0) return;
  if (isTrack.value) {                    // 轨道形态的块就是那一段：点它=选它
    pickRow(b.sid);
    seek(b.start);
    return;
  }
  if (laneKey !== "shots") {              // 旁白/配乐是从镜合成出来的，不是可拖的旋钮
    if (laneKey === "bgm") tab.value = "bgm"; // 配乐轨可点开配乐编辑面板
    seek(b.start);
    return;
  }
  trackDrag = { kind: "move", sid: b.sid, x0: ev.clientX, moved: false };
  window.addEventListener("pointermove", onTrackMove);
  window.addEventListener("pointerup", onTrackUp, { once: true });
}
function onResizeDown(ev, sid) {
  ev.stopPropagation();
  if (ev.button !== 0) return;
  const e = effSec(sid);
  trackDrag = { kind: "resize", sid, x0: ev.clientX, sec0: e.want, moved: false,
                floor: e.floor, pxPerSec: pps.value };
  window.addEventListener("pointermove", onTrackMove);
  window.addEventListener("pointerup", onTrackUp, { once: true });
}
function onTrackMove(ev) {
  if (!trackDrag) return;
  const dx = ev.clientX - trackDrag.x0;
  if (!trackDrag.moved && Math.abs(dx) < 4) return;
  trackDrag.moved = true;
  if (trackDrag.kind === "resize") {
    const v = Math.max(trackDrag.floor,
                       Math.round((trackDrag.sec0 + dx / trackDrag.pxPerSec) * 10) / 10);
    overrideFor(trackDrag.sid).min_duration_sec = v;
    return;
  }
  let target = order.value.indexOf(trackDrag.sid);
  blockRects().forEach((r, k) => {
    if (ev.clientX >= r.left && ev.clientX <= r.right) target = k;
  });
  if (target !== order.value.indexOf(trackDrag.sid)) {
    const next = order.value.filter((s) => s !== trackDrag.sid);
    next.splice(target, 0, trackDrag.sid);
    order.value = next;
  }
}
function onTrackUp() {
  window.removeEventListener("pointermove", onTrackMove);
  const d = trackDrag;
  trackDrag = null;
  if (d && !d.moved && d.kind === "move") {
    const r = projected.value.rows.find((x) => x.sid === d.sid);
    seek(r ? r.start : (rowOf(d.sid) || {}).start_sec || 0);
  }
}
const toggleRemove = (sid) => {
  removals.value = removals.value.includes(sid)
    ? removals.value.filter((x) => x !== sid) : [...removals.value, sid];
};

// ---- 表单小工具：高亮词是数组，增删只能整镜写回（指针只认已存在的格）----
function addHighlight(sid) { const o = overrideFor(sid); (o.highlight ||= []).push(""); }
function delHighlight(sid, k) { overrideFor(sid).highlight.splice(k, 1); }

// ---- 控件清单与取值范围都读表里的 controls ----
// 为什么不在前端写一份：写死的那份与入口校验迟早对不上，代价是「界面能拖到 3、
// 提交被拒收」，而用户读到的只是「改不动」。表里发了范围，滑杆就只可能给出合法值。
// 代码镜的旋钮不读这里——那份声明（type/min/max/options/step）随条目一起发，见 fieldEntry。
const ctl = computed(() => hm.value?.controls || {});
const ranges = computed(() => {
  const c = ctl.value, out = {};
  if (c.volume_max != null) out.volume = [0, c.volume_max];
  if (c.font_size_range) out.font_size = c.font_size_range;
  return out;
});
const enums = computed(() => ({ position: ctl.value.subtitle_positions || [] }));
/** 指针 → 这一格的**声明**（代码镜的字段条目）。控件的类型、范围、档位、候选
 *  都从这一份读：表里没给 min/max 就把滑杆退成纯数字框，而不是前端编一个上界——
 *  编出来的上界与契约对不上，用户拖到的值迟早被闸门打回。 */
const fieldEntry = (p) => {
  const e = entryByPointer.value[p];
  return e && e.kind === "field" ? e : null;
};
/** 这一格该用什么控件：类型问表（口播链的 editable_fields 连类型一起发），
 *  范围与档位也问表；两处都不在前端猜类型。 */
function ctlKind(p) {
  const f = fieldEntry(p);
  if (f) {
    if (f.type === "number") return f.min != null && f.max != null ? "range" : "number";
    return f.type === "color" ? "color" : f.type === "bool" ? "bool"
      : f.type === "select" ? "enum" : "text";
  }
  const name = leafField(p);
  const t = (hm.value?.editable_fields?.[segs(p)[0]] || {})[name] || "";
  if (t === "color") return "color";
  if (t === "enum" || (enums.value[name] || []).length) return "enum";
  if (ranges.value[name]) return "range";
  if (t === "number") return "number";
  return typeof baseValue(p) === "number" ? "number" : "text";
}
const rangeOf = (p) => {
  const f = fieldEntry(p);
  if (f && f.min != null && f.max != null) return [f.min, f.max];
  return ranges.value[leafField(p)] || [0, 1];
};
/** 步长按范围跨度给：0~2 的音量拖不出 0.05 的差别就等于没改，400 档字号拖 0.01 又点不准。
 *  代码镜的旋钮例外：声明里写了 step 就用它，那是契约给的档位，不是前端猜的跨度。 */
function stepOf(p) {
  const f = fieldEntry(p);
  if (f && f.step != null) return f.step;
  const [lo, hi] = rangeOf(p);
  const s = hi - lo;
  return s <= 1 ? 0.01 : s <= 4 ? 0.05 : s <= 150 ? 1 : 2;
}
/** 取色器只认 #rrggbb；颜色名与空串（= 用模板默认）照原样由文本框写回。 */
const HEX6 = /^#[0-9a-fA-F]{6}$/;
// 命中表把「这一栏没写值时渲染真正吃进去的那个数」一起发过来（表里的 default）。
// 没有它，滑杆和色块只能拿最小值去画：音量标 0、字幕色标黑，而片子里其实是 1.0× 白字。
const defaultOf = (p) => entryByPointer.value[p]?.default ?? null;
const shownValue = (p) => editValue(p) ?? defaultOf(p);
const colorHex = (p) => hexOf(shownValue(p));
const NAMED_COLOR = Object.fromEntries([
  ["white", "#ffffff"], ["black", "#000000"], ["yellow", "#ffd400"], ["red", "#e03a26"],
  ["green", "#1f9d55"], ["blue", "#1c6ee8"], ["orange", "#f2811d"], ["pink", "#f0b7bd"],
  ["purple", "#7b3fbf"], ["cyan", "#00b3b8"], ["magenta", "#d13fb0"], ["gray", "#8a8a8a"],
  ["grey", "#8a8a8a"], ["silver", "#c4c4c4"], ["gold", "#b98a2f"], ["beige", "#f6f1e4"],
  ["brown", "#7a4a21"], ["lime", "#8fd40a"], ["teal", "#18796f"], ["navy", "#1b2a5c"],
  ["violet", "#8f5fd6"], ["ivory", "#fffdf2"], ["khaki", "#c8b784"], ["salmon", "#f08a7a"],
  ["turquoise", "#3fc0b0"], ["wheat", "#e6c88a"],
]);
const namedHex = (v) => NAMED_COLOR[String(v || "").toLowerCase()] || "";
// 只覆盖口播轨那几条字段：代码镜的标题来自条目声明里的 label，不查这张表。
const FIELD_LABEL = {
  color: "字色", text: "文字", volume: "音量倍率", font_size: "字号", position: "位置",
  src_start: "取源起点 (s)", src_end: "取源终点 (s)", path: "素材路径",
  offset_sec: "起播偏移 (s)", style: "样式名", start: "起点 (s)", end: "终点 (s)",
};
const leafField = (p) => String(segs(p).slice(-1)[0] || "");
/** 任意写法 → 取色器要的 #rrggbb（空串 = 用模板默认，色板上只能显示成黑，原样仍由文本框写）。 */
function hexOf(v) {
  const s = String(v ?? "");
  if (HEX6.test(s)) return s;
  const m = /^#([0-9a-fA-F]{3})$/.exec(s);
  if (m) return `#${[...m[1]].map((c) => c + c).join("")}`;
  return namedHex(s) || "#000000";
}
/** 下拉里连当前值一起列：原值不在档位表里时，打开下拉不该顺手把它改成第一项。 */
const withCur = (list, cur) => (cur == null || cur === "" || list.includes(cur)
  ? list : [...list, cur]);
/** 一格的候选：代码镜的旋钮读**条目自己带的 options**（声明期就校验过值在里面），
 *  其余仍读整片那一份枚举表。两处各写一遍枚举，迟早出现「界面有这档、渲出来没有」。 */
const ctlOpts = (p) => {
  const f = fieldEntry(p);
  return withCur(f ? (f.options || []) : (enums.value[leafField(p)] || []), shownValue(p));
};
/** 一格的标题：代码镜用声明里的 label（那是给人看的），其余按字段名查中文表。 */
const fieldLabel = (p) => {
  const f = fieldEntry(p);
  return f ? (f.label || f.name) : (FIELD_LABEL[leafField(p)] || leafField(p));
};
// 闸⑥ 的四种回执（执行层那侧的同一批字符串）。mismatch 是唯一的红，跑到这里就已经
// 整镜拦下了；剩下三态都只影响「点不点得中」，所以〈字段〉页把它写成原因而不是错误。
const FIELD_STATUS = {
  ok: "画面对上了",
  unreported: "这一镜没把它标在元素上（代码里缺 <F>）",
  no_geometry: "标了但量不到盒（零宽/被裁掉/不可见）",
  mismatch: "注入值与画上自报不一致",
};
const codeNoBox = computed(() => codeFields.value.filter((f) => !f.box));


// ---- 样式同步范围：改完这一镜的旋钮取值，保存前必须说清同步到哪 ----
// 为什么问而不是默认全片：全片同型是常见诉求，但「这一镜想例外」同样是——
// 默认任何一个方向，另一批用户都会觉得自己被替决定了。
//
// 代码镜没有 card 也没有 theme，能同步的只有**这一镜自己交出来的旋钮**：
// 同名字段（且同 type）才叫同一个样式。别的镜没声明这个 name 就没有这一栏，
// 硬塞进去等于替它改代码；声明了但取值不合**它自己的** min/max/options，
// 也不许偷偷改——那是拿这一镜的刻度去量另一镜，所以它进「没改」那一栏并写明数字。
const fieldScope = ref("");          // "" 未决 · all 全片同名字段 · single 只这一镜
const syncAsk = ref(false);
const FIELD_POINTER_RE = /^\/shots\/(\d+)\/fields\/([^/]+)\/value$/;
const fieldEdits = computed(() => Object.keys(deepEdits).filter((p) => FIELD_POINTER_RE.test(p)));
const codeFieldOf = (sid, name) =>
  (tableEntries.value[sid] || []).find((e) => e.kind === "field" && e.name === name) || null;
/** 这一镜自己声明的取值界接不接得住这个值（与补丁口那份 check_field_value 同一口径）。 */
function fieldValueFits(f, v) {
  if (f.type === "number") {
    const n = Number(v);
    if (!Number.isFinite(n)) return false;
    if (f.min != null && n < Number(f.min)) return false;
    if (f.max != null && n > Number(f.max)) return false;
    return true;
  }
  if (f.type === "select") return (f.options || []).map(String).includes(String(v));
  if (f.type === "bool") return typeof v === "boolean";
  if (f.type === "color") return /^#[0-9a-fA-F]{3,8}$/.test(String(v));
  return true;
}
/** 每个被改到的字段指针 → 可以接同一个值的其他镜（take）与不接纳的（skip）。 */
const fieldSiblings = computed(() => {
  const out = [];
  fieldEdits.value.forEach((p) => {
    const m = FIELD_POINTER_RE.exec(p);
    const sid = m ? baseIds.value[Number(m[1])] : "";
    const src = sid && codeFieldOf(sid, m[2]);
    if (!src) return;
    const value = deepEdits[p];
    const take = [];
    const skip = [];
    order.value.forEach((t) => {
      if (t === sid || removals.value.includes(t)) return;
      const f = codeFieldOf(t, m[2]);
      if (!f || f.type !== src.type) return;
      if (Object.prototype.hasOwnProperty.call(deepEdits, f.pointer)) return;
      (fieldValueFits(f, value) ? take : skip).push(f.pointer);
    });
    out.push({ pointer: p, name: m[2], value, take, skip });
  });
  return out;
});
const syncTake = computed(() =>
  [...new Set(fieldSiblings.value.flatMap((g) => g.take))].length);
const syncSkip = computed(() =>
  [...new Set(fieldSiblings.value.flatMap((g) => g.skip))].length);
// 别的镜一个同名同类字段都没交出来时，「同步到哪儿」这个问题没有对象，不问。
const syncTargets = computed(() => syncTake.value + syncSkip.value);
/** 「全片同名字段」这一档在**提交时**才代写：不提前塞进改动账，用户后来在别的镜
 *  上亲手改回原值才算真撤销，否则下一次联动又会把它推回去。 */
const codeFanOut = computed(() => {
  const out = {};
  if (fieldScope.value !== "all") return out;
  fieldSiblings.value.forEach((g) =>
    g.take.forEach((p) => { out[p] = g.value; }));
  return out;
});
/** 提交用的改动账 = 代写那份打底，用户自己改的指针覆盖在上面（手改优先）。 */
const editAccount = computed(() => ({ ...codeFanOut.value, ...deepEdits }));
function applyFieldSyncAll() {
  fieldScope.value = "all";
  syncAsk.value = false;
}
function setFieldScopeSingle() {
  fieldScope.value = "single";
  syncAsk.value = false;
}
/** 这一趟改了旋钮取值、还没定同步范围（口播轨没有这个问题：一段的字段不跨段共用）。 */
const styleScopePending = computed(() => (!isTrack.value
  && syncTargets.value > 0 && !fieldScope.value));
const scopeTag = computed(() => {
  if (isTrack.value) return "";
  if (!syncTargets.value) return "";
  if (fieldScope.value === "all") {
    return `样式同步=全片同名字段（${syncTake.value} 处`
      + (syncSkip.value ? ` · ${syncSkip.value} 处取值不合它自己声明的界，没改` : "") + "）";
  }
  if (fieldScope.value === "single") return "样式同步=只这一镜";
  return "样式同步=还没定（提交时会问）";
});

// ---- 代码镜的两页逃生口：〈代码〉与〈文案·出处〉----
// 〈代码〉改了走 `/shots/i/code` 指针，服务端**必须重过闸①**才允许落盘
// （`code_patch.relint_codes`：验不过一寸不落盘）；〈字段〉那页改的是注入值，代码字节
// 没动，lint 结论不可能变，所以不重跑。两条路的复闸口径不同，界面上要写明——
// 否则用户会以为改代码更便宜，而它恰恰是贵的那一条。
const pointerTo = (sid, tail) => {
  const i = idxOf(sid);
  return i < 0 ? "" : `/shots/${i}/${tail}`;
};
const codePointer = computed(() => pointerTo(focusSid.value, "code"));
const codeValue = computed(() => (codePointer.value
  ? String(editValue(codePointer.value) ?? "") : ""));
const setCode = (v) => { if (codePointer.value) setEdit(codePointer.value, v); };
const resetCode = () => { if (codePointer.value) delete deepEdits[codePointer.value]; };
const codeBytes = computed(() => new TextEncoder().encode(codeValue.value).length);

// 出处（source）整镜三条指针之一，值必须是对象（写成字符串的话出处闸读不到键），
// 所以这里不按 key 逐条发指针，而是一次写回整个 source——它不进缓存键，改它不动像素。
const sourcePointer = computed(() => pointerTo(focusSid.value, "source"));
const sourceRows = computed(() => {
  const v = sourcePointer.value ? editValue(sourcePointer.value) : null;
  return v && typeof v === "object" ? v : {};
});
const sourceSet = (next) => { if (sourcePointer.value) setEdit(sourcePointer.value, next); };
function sourceRename(oldKey, ev) {
  const nk = String(ev.target.value || "").trim();
  if (!nk || nk === oldKey || nk in sourceRows.value) return;
  const out = {};
  Object.entries(sourceRows.value).forEach(([k, v]) => { out[k === oldKey ? nk : k] = v; });
  sourceSet(out);
}
const sourceEdit = (k, v) => sourceSet({ ...sourceRows.value, [k]: v });
function sourceDel(k) { const s = { ...sourceRows.value }; delete s[k]; sourceSet(s); }
function sourceAdd() {
  const s = sourceRows.value;
  let n = 1;
  while (`新出处 ${n}` in s) n += 1;
  sourceSet({ ...s, [`新出处 ${n}`]: "" });
}

// ---- 逐镜质感（第五口指针：/shots/i/texture）----
// 档位与标签都读命中表里的 `controls.texture`（enum + value_labels），不在前端抄一份：
// 抄的那份与契约迟早对不上，代价是「界面能选、闸门打回」。
// **「跟整片默认」(空串) 与 「不加」("none") 是两个选项**，前者把决定权交回顶层，
// 后者是这一镜点名不要——后端把这两种形状判给不同的滤镜趟，界面上也不许合成一个。
const texCtl = computed(() => ctl.value.texture || {});
const texLabels = computed(() => texCtl.value.value_labels || {});
const texEnum = computed(() => texCtl.value.enum || []);
const texLabel = (k) => (k === "" ? "跟整片默认"
  : (texLabels.value[k] || k));
const filmTexture = computed(() => String(hm.value?.spec?.texture || "none"));
const texturePointer = computed(() => pointerTo(focusSid.value, "texture"));
/** 这一镜**声明**的档（空串＝没表态）。 */
const textureDeclared = computed(() => String((texturePointer.value
  ? editValue(texturePointer.value) : "") ?? ""));
/** 画面上**实际**的档：与逐镜账同源（后端 `out["shots"][].texture`），前端不重算那条
 *  「有就用、没有问整片」的规则——自己算一遍就是第二个出处。 */
const textureEffective = computed(() => String(rowOf(curSid.value)?.texture || "none"));
const setTexture = (v) => { if (texturePointer.value) setEdit(texturePointer.value, v); };
const resetTexture = () => { if (texturePointer.value) delete deepEdits[texturePointer.value]; };

// ---- 带着这个选区去问（F4）----
// 选中的是「这一版的这一段/这一块」，光说「把那句字幕改一下」模型得回头猜；
// 把锚点原样贴进聊天框，AI 改与手工改用的就是同一个选中态。
function askAbout() {
  const sid = focusSid.value || "(没选)";
  const kind = isTrack.value ? "口播/素材片" : "图形科普片 · 代码镜";
  let what;
  if (isCode.value) {
    // 代码镜的选区是**旋钮**，不是画面上的一块字：把指针与当前值一起带上，模型才不必
    // 把「把那个圆改大一点」再翻成一次整镜改写（那会白重过一趟闸①）。
    const list = selected.value.length
      ? codeFields.value.filter((f) => selected.value.includes(f.id)) : codeFields.value;
    what = list.length
      ? list.map((f) => `${f.label || f.name}＝${JSON.stringify(editValue(f.pointer) ?? f.value)}`
          + `（${f.pointer}）`).join("；")
      : "这一镜没交出旋钮（fields 为空），只能改代码、整镜文案或质感档";
    if (texturePointer.value) {
      what += `${what ? "；" : ""}质感＝${texLabel(textureDeclared.value)}`
        + (textureDeclared.value !== textureEffective.value
            ? `（画面上是 ${texLabel(textureEffective.value)}）` : "")
        + `（${texturePointer.value}）`;
    }
  } else if (groups.value.length) {
    what = groups.value.map((g) => `${g.pointer || "(回指不到字段)"}＝${g.hits[0].label}`)
      .join("；");
  } else {
    what = (tableEntries.value[sid] || [])[0]?.label || "";
  }
  emit("ask", `【选区改】在成片 ${artifact.value}（${kind}）`
    + `的 ${isTrack.value ? "段" : "镜"} ${sid}${what ? ` · ${what}` : ""} 上，我想改：`);
}

// ---- 提交：拼成一次局部改 ----
const busy = ref("");
const failMsg = ref("");
const done = ref(null);
const progress = reactive({ status: "", stage: "", percent: 0 });
let pollAbort = false;

function payload() {
  if (isTrack.value) {
    // 这条链只收指针补丁：段号逐字取自表，闸门拿它对「这张表量的是不是当前这一版」，
    // 对不上就整批拒收。没有 shot_sets/remove_shots/reorder——那条合同不改总长也不换序。
    const edits = Object.entries(editAccount.value).map(([p, value]) => ({
      pointer: p, value,
      segment_id: String(entryByPointer.value[p]?.segment_id ?? ""),
    }));
    const body = { base_artifact_id: artifact.value, conv_id: props.convId, wait_sec: 0 };
    if (edits.length) body.edits = edits;
    return body;
  }
  const edits = Object.entries(editAccount.value).map(([p, value]) => ({
    pointer: p, value, shot: baseIds.value[Number(segs(p)[1])] || "",
  }));
  const shotSets = order.value
    .filter((sid) => !removals.value.includes(sid))
    .map((sid) => ({ shot: sid, set: setFor(sid) }))
    .filter((x) => Object.keys(x.set).length);
  const removed = removals.value.filter((s) => baseIds.value.includes(s));
  const kept = baseIds.value.filter((s) => !removed.includes(s));
  const now = order.value.filter((s) => !removed.includes(s));
  const body = { base_artifact_id: artifact.value, conv_id: props.convId, wait_sec: 0 };
  if (edits.length) body.edits = edits;
  if (shotSets.length) body.shot_sets = shotSets;
  if (removed.length) body.remove_shots = removed;
  if (now.join(" ") !== kept.join(" ")) body.reorder = now;
  if (bgmEdit.value.volume !== null) body.bgm_volume = bgmEdit.value.volume;
  if (bgmEdit.value.duck !== null) body.bgm_duck = bgmEdit.value.duck;
  return body;
}
const planBits = computed(() => {
  const p = payload();
  const bits = [];
  if (isTrack.value) return p.edits ? [`逐段 ${p.edits.length} 处`] : bits;
  if (p.edits) bits.push(`逐格 ${p.edits.length} 处`);
  if (p.shot_sets) bits.push(`整镜 ${p.shot_sets.length} 镜`);
  if (p.remove_shots) bits.push(`删 ${p.remove_shots.length} 镜`);
  if (p.reorder) bits.push("换序");
  if (p.bgm_volume !== undefined) bits.push(`音量→${p.bgm_volume}`);
  if (p.bgm_duck !== undefined) bits.push(p.bgm_duck ? "人声压低" : "不压低");
  if (scopeTag.value) bits.push(scopeTag.value);
  return bits;
});
const plan = computed(() => (planBits.value.join(" · ") || "还没有改动"));
/** 代码链的**重烧**口径：改动账按镜内容算（含只改出处的），但 `source` 与 `texture`
 *  都不在缓存键里——改出处不改像素，改质感只在切片之后多过一趟滤镜，一帧 Remotion 都不重烧。
 *  预计数里不摘掉它们，界面就会报「重烧 1 镜」而后端回「0 镜」——
 *  这类虚报与旧链那次「局部改其实整片重烧」是同一族，只是方向相反。 */
const NO_BAKE_FIELDS = ["source", "texture"];
const pixelTouched = computed(() => {
  const touched = new Set();
  Object.keys(editAccount.value).forEach((p) => {
    if (NO_BAKE_FIELDS.includes(segs(p)[2])) return;
    const sid = baseIds.value[Number(segs(p)[1])];
    if (sid) touched.add(sid);
  });
  order.value.forEach((sid) => { if (Object.keys(setFor(sid)).length) touched.add(sid); });
  return touched;
});
const noBake = computed(() =>
  [...changedShots.value].filter((sid) => !pixelTouched.value.has(sid)));
const rebakeNote = computed(() => {
  if (isTrack.value) {
    // 具体几窗由后端按「这一窗的输入清单变没变」点名（一句跨两窗的字幕要重烧两窗），
    // 前端不替它猜数字，只说改动的段落在哪几段画面上。
    return `改到 ${changedShots.value.size} 段 · 只重烧这些段压着的窗口`;
  }
  const p = payload();
  const n = pixelTouched.value.size;
  const tail = noBake.value.length
    ? ` · 另 ${noBake.value.length} 镜只改出处或质感（都不进缓存键，一帧不重烧）` : "";
  if (!p.reorder || n) return `预计重烧 ${n} 镜${tail}`;
  return "重排不烧像素（拼而已）";
});

async function submit() {
  if (!dirty.value || busy.value) return;
  if (styleScopePending.value) {
    syncAsk.value = true;                 // 样式改动的同步范围要先定，这一趟不替用户决定
    return;
  }
  syncAsk.value = false;
  busy.value = "submitting"; failMsg.value = ""; done.value = null;
  progress.status = "queued"; progress.percent = 0; progress.stage = "提交中";
  try {
    const r = await fetch(`${api.value}/patch`, {
      method: "POST", headers: hdr({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload()),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    const newId = String(j.artifact_id || "");
    if (!newId) throw new Error("局部改没有返回新版本的产物号");
    if ((j.status || "") === "done") { finish(j, newId); return; }
    progress.status = j.status || "queued";
    busy.value = "rendering";
    await poll(newId);
  } catch (e) {
    busy.value = "";
    failMsg.value = String(e?.message || e);
    progress.status = "failed";
  }
}
async function poll(id) {
  // 长任务的判据在轮询端的正文里：done/failed 都可能出现在一路 200 之后
  for (let n = 0; n < 360 && !pollAbort; n++) {
    await new Promise((s) => setTimeout(s, 2000));
    let v;
    try {
      const r = await fetch(`/render_status?${qs({ artifact_id: id, conv_id: props.convId })}`,
                            { headers: hdr() });
      v = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(v.detail || `HTTP ${r.status}`);
    } catch (e) {
      busy.value = ""; failMsg.value = String(e?.message || e); return;
    }
    progress.status = v.status || ""; progress.stage = v.stage || "";
    progress.percent = Number(v.percent || 0);
    if (v.status === "done") { finish(v, id); return; }
    if (v.status === "failed") {
      busy.value = "";
      failMsg.value = v.error || "局部改失败（闸门没有留下正文）";
      const fb = v.fallbacks || v.options || [];
      if (fb.length) failMsg.value += `\n\n可回退的路子：${fb.map((x) => x?.label || x).join("；")}`;
      return;
    }
  }
  if (!pollAbort) {
    busy.value = ""; progress.status = "running";
    failMsg.value = "这次局部改还在跑（已等过 12 分钟）。关掉这里不会取消它——"
      + "稍后在对话里报这个产物号查一次进度即可。";
  }
}
function finish(view, id) {
  busy.value = ""; progress.status = "done"; progress.percent = 100;
  const patch = view.patch || {};
  const base = String(patch.base_artifact_id || artifact.value);
  const before = patch.before_frames || {};
  const cache = view.segment_cache || {};
  done.value = {
    id, base, url: view.media_url || "", duration: view.duration ?? null,
    // 对比帧摆的是**这次真重烧的那些窗**：轨道形态下改一句字幕，画面跟着变的
    // 是它压着的那一到两窗，被点名的段 id 自己根本没有帧。
    changed: isTrack.value ? (cache.rebuilt || []) : (patch.changed_shots || []),
    segs: isTrack.value ? (patch.changed_ids || []) : (patch.changed_shots || []),
    reused: cache.reused || [],
    removed: patch.removed_ids || patch.removed_shots || [],
    reordered: !!patch.reordered, hadBefore: Object.keys(before).length,
    // 改过取景的窗在新版里换了段名，而 base 的帧按旧名存：这份映射由后端算给出。
    beforeShots: patch.before_frame_shots || {},
    evidence: view.evidence || [],
  };
  if (!chain.some((c) => c.id === id)) chain.unshift({ id, url: view.media_url || "" });
  const summary = { artifactId: id, mediaUrl: view.media_url || "",
                    duration: view.duration ?? null, evidence: view.evidence || [],
                    changed: done.value.changed,
                    title: props.title ? `${props.title}（局部改）` : "局部改后的成片" };
  resetEdits();
  emit("patched", summary);
  prepareCompare();
}
const baseShotOf = (d, sid) => d.beforeShots[sid] || sid;
async function prepareCompare() {
  const d = done.value;
  if (!d) return;
  await Promise.all(d.changed.flatMap((sid) => [frameUrl(d.base, baseShotOf(d, sid)),
                                                frameUrl(d.id, sid)]));
}
const compare = computed(() => {
  const d = done.value;
  if (!d) return [];
  return d.changed.map((sid) => ({
    sid, base: frameUrls[`${d.base}|${baseShotOf(d, sid)}`] || "",
    after: frameUrls[`${d.id}|${sid}`] || "",
    removed: d.removed.includes(sid),
  }));
});
async function adopt(id) {
  artifact.value = id;
  playUrl.value = chain.find((c) => c.id === id)?.url || playUrl.value;
  done.value = null; failMsg.value = ""; cur.value = 0; playing.value = false;
  await loadHitmap(id);
}
// 侧栏开着时点另一张成片卡：只有 props 在变，表不会自己重读——不接这一句，界面上挂着
// 的仍是上一版的命中表，指针也就改到上一版的镜号上（真机踩过：新出的版本点不开）。
watch(() => props.artifactId, (id) => { if (id && id !== artifact.value) adopt(id); });
onBeforeUnmount(() => {
  pollAbort = true;
  ro?.disconnect();
  Object.values(frameUrls).forEach((u) => URL.revokeObjectURL(u));
});

loadHitmap(props.artifactId);
</script>

<template>
  <!-- 根节点就是面板本体：外层 <aside class="editor-dock"> 给宽度与左边框，
       这里只负责把自己填满那一列并整体滚动。 -->
  <div class="me-panel">
      <header class="me-head">
        <b>✂ 选区改 · {{ title || (isTrack ? "口播 / 素材片" : "图形科普片") }}</b>
        <span class="me-art">在改版本 {{ artifact }}</span>
        <span v-if="dirty" class="me-dirty">未提交：{{ plan }} · {{ rebakeNote }}</span>
        <button class="me-x" @click="emit('close')">✕ 关闭</button>
      </header>

      <div v-if="loading" class="me-note pad">正在读这一版的命中表…</div>
      <div v-else-if="loadErr" class="me-err">
        {{ loadErr }}
        <div class="me-sub">没有命中表就只能整片重做——选中的这一块/这一段对不回时间线的哪一条。
          要让这一版可改，请重新出片一次（出片时顺手量表）。</div>
      </div>

      <template v-else-if="hm">
        <div class="me-body">
          <section class="me-left">
            <div ref="stageEl" class="me-stage" :class="{ pick: isTrack }"
                 :style="{ aspectRatio: aspect }" @pointerdown="onStageDown">
              <video ref="videoEl" :src="playUrl" :muted="muted" preload="metadata"
                     @timeupdate="onTime" @ended="playing = false"></video>
              <div v-if="!isTrack" class="me-layer">
                <span v-for="b in visibleBoxes" :key="b.id" class="me-hit"
                      :class="{ on: b.on, dead: b.dead }" :style="b.style"
                      :title="b.title || b.field || '量到了字，但回指不到分镜字段'"><i v-if="b.on">✎</i></span>
              </div>
              <div v-if="marquee" class="me-marquee" :style="boxStyle(marquee)"></div>
              <div v-if="isCode" class="me-hint">
                画面上这些框是<b>这一镜交出的旋钮</b>（母版在探针帧量出来的盒子）：
                框住或点一下，右边〈字段〉页就把那一行点亮 ·
                量不到盒的旋钮仍列在表里，只是点不中
              </div>
              <div v-else class="me-hint">
                这一版的段就是「第 s 秒到第 e 秒的一整幅画面」，画面上没有可框的格子：
                <b>在下面按轨道点一段</b>
              </div>
            </div>

            <div class="me-ctrl">
              <button class="me-btn" @click="toggle">{{ playing ? "⏸ 暂停" : "▶ 播放" }}</button>
              <button class="me-btn"
                      @click="muted = !muted; videoEl && (videoEl.muted = muted)">
                {{ muted ? "🔇 静音" : "🔊 有声" }}</button>
              <span class="me-time">{{ cur.toFixed(1) }}s / {{ (hm.duration || 0).toFixed(1) }}s
                · {{ isTrack ? "画面段" : "镜" }} {{ curIdx + 1 }}/{{ laneRows.length }}
                （{{ curSid }}）</span>
              <span class="me-old">放着的是<b>这一版的原片</b>，改动要提交之后才看得见</span>
            </div>

            <div class="me-zoom">
              <button class="me-btn" @click="zoomBy(1 / 1.4)">－</button>
              <button class="me-btn" @click="zoomBy(1.4)">＋</button>
              <button class="me-btn" :class="{ on: !zoomPx }" @click="zoomPx = 0">铺满</button>
              <span class="me-zoomtag">{{ pps.toFixed(0) }} px/秒</span>
              <button class="me-btn" @click="stepFrames(-1)">⏯ 上一帧</button>
              <button class="me-btn" @click="stepFrames(1)">下一帧 ⏯</button>
              <span class="me-zoomtag">第 {{ Math.round(cur * fps) }} 帧 ·
                {{ cur.toFixed(2) }}s / {{ timelineDur.toFixed(2) }}s
                · 1 帧 = {{ (1000 / fps).toFixed(1) }}ms</span>
            </div>

            <div :ref="setScrollEl" class="me-scroll">
              <div class="me-wide" :style="{ width: `${laneW}px` }">
                <div class="me-ruler" @pointerdown="onScrubDown">
                  <span v-for="t in ticks" :key="t.sec" class="me-tick"
                        :style="{ left: `${t.x}px` }"><i></i><b>{{ fmtSec(t.sec) }}</b></span>
                  <div class="me-playline" :style="{ left: `${headX}px` }"></div>
                </div>
                <div v-for="l in lanes" :key="l.key" class="me-lane">
                  <div class="me-lanebody" :ref="l.key === 'shots' ? setTrackEl : null">
                    <div class="me-lanegd">{{ l.label }}<em>{{ l.bars.length }}</em></div>
                    <button v-for="b in l.bars" :key="`${l.key}-${b.sid}`"
                            :class="['me-bar', l.key, { hot: b.sid === curSid, now: barIsNow(b),
                                                       on: isTrack && b.sid === selectedRow,
                                                       'me-block': l.key === 'shots' }]"
                            :style="barStyle(b)"
                            :title="`${b.label} · ${fmtSec(b.start)} 起 ${b.sec.toFixed(2)}s`"
                            @pointerdown="onBarDown($event, b, l.key)">
                      <img v-if="b.frame" :src="b.frame" alt="" />
                      <b>{{ b.label }}</b>
                      <span>{{ fmtSec(b.start) }}–{{ fmtSec(b.start + b.sec) }}</span>
                      <em v-if="l.key === 'shots' && effSec(b.sid).want < effSec(b.sid).floor"
                          class="me-floor">拖到 {{ effSec(b.sid).floor.toFixed(1) }}s 以下无效：
                        这一镜的时钟归配音</em>
                      <span v-if="l.key === 'shots'" class="me-del"
                            @click.stop="toggleRemove(b.sid)">✕</span>
                      <span v-if="l.key === 'shots'" class="me-edge"
                            @pointerdown="onResizeDown($event, b.sid)"></span>
                    </button>
                    <div class="me-playline" :style="{ left: `${headX}px` }"></div>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="removedShots.length" class="me-removed">
              已删待恢复：<button v-for="s in removedShots" :key="s" class="me-ver"
                                 @click="toggleRemove(s)">↺ {{ s }}</button>
            </div>
            <div class="me-track-note">
              一条秒尺上按真实起止秒铺块，三条 lane 同轴对齐 ·
              <template v-if="isTrack">
                点一段就在右侧改它的字段，改字与换素材只重烧它压着的窗口。<b>改时长、删段、
                换序不在这张表里</b>：那会打乱画面与口播句子的对应，请回计划卡重新出片。
              </template>
              <template v-else>
                拖块身换序 · 拖右边缘改时长 · 点块定位到那一镜 ·
                改完合计约 {{ estTotal.toFixed(1) }}s（原来 {{ (hm.duration || 0).toFixed(1) }}s）
              </template>
            </div>

            <div v-if="done" class="me-done">
              <b>✓ 新的一版：{{ done.id }}</b>
              <span v-if="isTrack">改了 {{ done.segs.length }} 段{{ done.segs.length
                     ? `（${done.segs.join("、")}）` : "" }}
                · 重烧 {{ done.changed.length }} 窗（{{ done.changed.join("、") }}）
                · 复用 {{ done.reused.length }} 窗没动过的字节
                · 基于 {{ done.base }}，旧版字节原位还在</span>
              <span v-else>改了 {{ done.changed.length }} 镜{{ done.changed.length
                     ? `（${done.changed.join("、")}）` : "" }}
                · 删 {{ done.removed.length }} 镜{{ done.reordered ? " · 换过序" : "" }}
                · 基于 {{ done.base }}，旧版字节原位还在</span>
              <div class="me-done-act">
                <button class="me-btn primary" @click="adopt(done.id)">接着改这一版</button>
                <button class="me-btn" @click="playUrl = done.url">播新版</button>
                <button class="me-btn" @click="playUrl = chain[chain.length - 1].url">播旧版</button>
              </div>
              <video class="me-new" :src="done.url" controls preload="metadata"></video>
              <div v-if="compare.length" class="me-cmp">
                <div v-for="c in compare" :key="c.sid" class="me-cmpcol">
                  <div class="me-cmphd">{{ c.sid }} 改前{{ isTrack ? "（那一窗）" : "（这一镜）" }}</div>
                  <img v-if="c.base" :src="c.base" alt="" />
                  <div v-else class="me-noimg">旧版没留这一{{ isTrack ? "窗" : "镜" }}的代表帧</div>
                  <div class="me-cmphd">{{ c.removed ? "（已删，没有改后）" : "改后" }}</div>
                  <img v-if="c.after" :src="c.after" alt="" />
                  <div v-else-if="!c.removed" class="me-noimg">取帧失败</div>
                </div>
              </div>
            </div>
            <div v-else-if="failMsg" class="me-fail">
              <b>✗ 这一版补丁没有落下去</b>
              <pre>{{ failMsg }}</pre>
              <span class="me-sub">上面是闸门原话，一字未改。照着改完可以直接再提交一次。</span>
            </div>
          </section>

          <aside class="me-right">
            <div class="me-tabs">
              <template v-if="isCode">
                <button :class="{ on: tab === 'fields' }" @click="tab = 'fields'">
                  字段 · {{ curSid }}<em>{{ codeFields.length }}</em></button>
                <button :class="{ on: tab === 'code' }" @click="tab = 'code'">
                  代码 · {{ curSid }}</button>
                <button :class="{ on: tab === 'doc' }" @click="tab = 'doc'">
                  文案 · 出处 · {{ curSid }}</button>
                <button :class="{ on: tab === 'texture' }" @click="tab = 'texture'">
                  质感 · {{ curSid }}</button>
                <button :class="{ on: tab === 'bgm' }" @click="tab = 'bgm'">
                  配乐</button>
              </template>
              <template v-else>
                <button :class="{ on: tab === 'cells' }" @click="tab = 'cells'">
                  选中段的字段</button>
              </template>
            </div>

            <div v-if="syncAsk && isCode" class="me-askbar">
              <b>这次改了画面旋钮的取值：同步到哪儿？</b>
              <span class="me-sub">改动没有落下去。代码镜没有「同一版式共用一份样式」
                这回事，能同步的只有<b>别的镜也交出来的同名同类字段</b>（name 与 type
                都对上才算同一个旋钮）。这一趟有 {{ syncTargets }} 处这样的字段：
                {{ syncTake }} 处接得住这个值{{ syncSkip ? `；另 ${syncSkip} 处它自己声明的上下界或选项容不下，
                不拿这一镜的刻度去量另一镜，保持原样` : "" }}。
                没交这个字段的镜不受影响，也不会替它改代码。</span>
              <div class="me-line">
                <button class="me-btn primary" :disabled="!syncTake"
                        @click="applyFieldSyncAll">
                  全片同名字段都改（{{ syncTake }} 处）</button>
                <button class="me-btn" @click="setFieldScopeSingle">
                  只改这一镜（其余留原样）</button>
              </div>
            </div>

            <div class="me-vers">
              <button v-for="c in chain" :key="c.id" class="me-ver"
                      :class="{ on: c.id === artifact }" @click="adopt(c.id)">
                {{ c.id }}<em v-if="c.id === artifact">在改</em>
              </button>
              <span class="me-sub">这里只列本次编辑器里走过的版本——服务端还没有
                「列出某会话全部版本」的口，跨刷新找旧版要在对话里报产物号。</span>
            </div>

            <div v-if="tab === 'cells' || tab === 'fields'" class="me-cells">
              <div v-if="isCode && syncTargets" class="me-sub">
                {{ scopeTag }}
                <button class="me-mini" @click="fieldScope = ''; syncAsk = true">重新问过</button>
              </div>
              <div v-if="isCode && !codeFields.length" class="me-note">
                <b>这一镜没交出可改的旋钮。</b>
                要调的值都写死在代码里了。三条路：切〈代码〉页直接改那一段（这一条要
                <b>重过闸①</b>）、切〈文案 · 出处〉改文案与引用，或回计划卡让模型重新出
                这一镜并在 <code>fields</code> 里交旋钮（name / type / label / value，
                界面就照那份自动出控件，改值不必动代码）。
              </div>
              <div v-else-if="isCode" class="me-sub">
                这一镜 {{ codeFields.length }} 个旋钮{{ codeNoBox.length
                  ? `，其中 ${codeNoBox.length} 个量不到盒（点不中，仍可改）` : "" }} ·
                左边框住一块会把对应那一行点亮，清单本身不收窄
              </div>
              <div v-else-if="!selected.length" class="me-note">
                还没点段。在下面按轨道点一段——每一段能改的就是它的素材路径、进出点或文字。
                改完只重烧那一段压着的窗口，其余窗复用上一版字节。
                <div v-if="(shots[curSid] || {}).error" class="me-dead">
                  这一镜的表没量成：{{ (shots[curSid] || {}).error }}
                </div>
              </div>
              <div v-for="g in cellGroups" :key="g.key" class="me-cell"
                   :class="{ hot: isCode && selected.includes(g.hits[0].id) }">
                <div class="me-cellhd">
                  <code>{{ g.pointer || "（回指不到字段）" }}</code>
                  <em v-if="!g.pointer">这一格回指不到字段指针</em>
                  <em v-else-if="isCode && !g.hits[0].box" class="me-maybe"
                      :title="`闸⑥ 回执：${g.hits[0].field_status}`"
                  >无空间命中 · {{ FIELD_STATUS[g.hits[0].field_status] || "量不到盒" }}</em>
                </div>
                <div class="me-cellt">
                  <template v-if="isCode">
                    <b>{{ g.hits[0].label || g.hits[0].name }}</b>
                    · 机器名 <code>{{ g.hits[0].name }}</code>
                    · 类型 {{ g.hits[0].type }}
                    <template v-if="g.hits[0].unit"> · 单位 {{ g.hits[0].unit }}</template>
                  </template>
                  <template v-else>{{ g.hits[0].label }}</template>
                </div>
                <template v-if="g.pointer">
                  <div class="me-ctl">
                    <template v-if="ctlKind(g.pointer) === 'color'">
                      <input class="me-color" type="color" :value="colorHex(g.pointer)"
                             @input="setEdit(g.pointer, $event.target.value)" />
                      <input class="me-in" :value="editValue(g.pointer) ?? ''"
                             @input="setEdit(g.pointer, $event.target.value.trim())" />
                    </template>
                    <template v-else-if="ctlKind(g.pointer) === 'range'">
                      <input class="me-range" type="range"
                             :min="rangeOf(g.pointer)[0]" :max="rangeOf(g.pointer)[1]"
                             :step="stepOf(g.pointer)"
                             :value="Number(shownValue(g.pointer) ?? rangeOf(g.pointer)[0])"
                             @input="setEdit(g.pointer, Number($event.target.value))" />
                      <input class="me-in num" type="number" :step="stepOf(g.pointer)"
                             :value="editValue(g.pointer) ?? ''"
                             @input="setEdit(g.pointer, Number($event.target.value))" />
                    </template>
                    <template v-else-if="ctlKind(g.pointer) === 'enum'">
                      <select class="me-in" :value="shownValue(g.pointer) ?? ''"
                              @change="setEdit(g.pointer, $event.target.value)">
                        <option v-for="o in ctlOpts(g.pointer)" :key="o" :value="o">{{ o }}</option>
                      </select>
                    </template>
                    <label v-else-if="ctlKind(g.pointer) === 'bool'" class="me-bool">
                      <input type="checkbox" :checked="!!editValue(g.pointer)"
                             @change="setEdit(g.pointer, $event.target.checked)" />
                      {{ editValue(g.pointer) ? "开" : "关" }}</label>
                    <input v-else-if="ctlKind(g.pointer) === 'number'"
                           class="me-in" type="number" :step="stepOf(g.pointer)"
                           :min="fieldEntry(g.pointer)?.min"
                           :max="fieldEntry(g.pointer)?.max"
                           :value="editValue(g.pointer) ?? ''"
                           @input="setEdit(g.pointer, Number($event.target.value))" />
                    <input v-else class="me-in" :value="editValue(g.pointer) ?? ''"
                           @input="setEdit(g.pointer, $event.target.value)" />
                  </div>
                  <div class="me-sub">
                    <template v-if="!isCode">{{ fieldLabel(g.pointer) }} · </template>
                    <template v-if="baseValue(g.pointer) == null">
                      未设置 · 渲染按默认 {{ JSON.stringify(defaultOf(g.pointer)) }}
                    </template>
                    <template v-else>原值：{{ JSON.stringify(baseValue(g.pointer)) }}</template>
                    <template v-if="ctlKind(g.pointer) === 'range'">
                      · 可取 {{ rangeOf(g.pointer)[0] }}~{{ rangeOf(g.pointer)[1] }}
                      <template v-if="isCode && g.hits[0].unit">{{ g.hits[0].unit }}</template>
                      · 步进 {{ stepOf(g.pointer) }}
                    </template>
                    <template v-else-if="isCode && g.hits[0].type === 'number'">
                      · 这条声明没给范围，数字框照原样收值
                    </template>
                    <template v-if="isCode">· 画面上自报
                      {{ JSON.stringify(g.hits[0].reported) }}（闸⑥
                      {{ FIELD_STATUS[g.hits[0].field_status] || g.hits[0].field_status }}）
                      · 改注入值<b>不重过闸①</b>，只重烧这一镜
                    </template>
                  </div>
                </template>
                <div v-else class="me-dead">
                  这一格没有可写的字段指针：代码镜请切〈代码〉页改那一段，口播轨请回计划卡重新出片。
                </div>
              </div>
            </div>

            <div v-else-if="tab === 'code'" class="me-form">
              <div class="me-frow">
                <label>镜头代码 code · {{ curSid }}（React/TSX，一镜一次浏览器）</label>
                <textarea class="me-in me-code" rows="16" spellcheck="false"
                          :value="codeValue" @input="setCode($event.target.value)"></textarea>
                <div class="me-sub">{{ codePointer || "（这一镜不在表里）" }} ·
                  {{ codeBytes }} 字节
                  <button v-if="codePointer in deepEdits" class="me-mini"
                          @click="resetCode">↺ 回到这一版的代码</button>
                </div>
                <div class="me-note">
                  改代码<b>必须重过闸①</b>：容器里的 lint 带行号退回「声明了没读、读了没声明、
                  &lt;F&gt; 没引用 fields.x」这几类，验不过一寸不落盘，旧版字节原位还在。
                  要调的数、色、开关只要在〈字段〉页里有旋钮就别动代码——那一页不重跑 lint。
                  版式与字幕带挂在母版上，镜头代码碰不到它们；这一镜代码为空会被直接拒收
                  （这条链没有默认版式可退，空代码渲出来就是一张空画面）。
                </div>
              </div>
            </div>

            <div v-else-if="tab === 'doc'" class="me-form">
              <div class="me-frow">
                <label>文案 text（字幕与配音同源，改这里等于换这一镜说的话）</label>
                <textarea class="me-in" rows="2" :value="overrideFor(curSid).text ?? ''"
                          @input="overrideFor(curSid).text = $event.target.value"></textarea>
                <div class="me-sub">字数上限按竖屏/横屏给，超了带镜号退回；
                  换文案会重发这一镜的配音并重烧这一镜</div>
              </div>
              <div class="me-frow">
                <label>高亮词 highlight（必须是文案里的子串，否则闸门会带镜号退回）</label>
                <div v-for="(h, k) in (overrideFor(curSid).highlight || [])" :key="k" class="me-line">
                  <input class="me-in" :value="h"
                         @input="overrideFor(curSid).highlight[k] = $event.target.value" />
                  <button class="me-mini" @click="delHighlight(curSid, k)">✕</button>
                </div>
                <button class="me-mini" @click="addHighlight(curSid)">＋ 加一个高亮词</button>
              </div>
              <div class="me-frow">
                <label>时长下限 min_duration_sec（秒）</label>
                <input class="me-in" type="number" step="0.1" min="0.5"
                       :value="overrideFor(curSid).min_duration_sec ?? ''"
                       @input="overrideFor(curSid).min_duration_sec = Number($event.target.value)" />
                <div class="me-sub">这一镜配音
                  {{ (timeline[curIdx]?.speech_sec || 0).toFixed(1) }}s ·
                  实际长度取两者的大值（也可以直接在轨道上拖右边缘）</div>
              </div>
              <div class="me-frow">
                <label>出处 source（写在分镜卡与逐镜账上，不进画面）</label>
                <div v-for="(v, k) in sourceRows" :key="k" class="me-line">
                  <input class="me-in key" :value="k" @blur="sourceRename(k, $event)" />
                  <input class="me-in val" :value="v"
                         @input="sourceEdit(k, $event.target.value)" />
                  <button class="me-mini" @click="sourceDel(k)">✕</button>
                </div>
                <div v-if="!Object.keys(sourceRows).length" class="me-sub">这一镜没写出处。</div>
                <button class="me-mini" @click="sourceAdd()">＋ 加一条出处</button>
                <div class="me-sub">凭记忆写的请标「未核实：…」；写「查证过」的要对得上本会话的
                  检索账（出片那一步的出处闸判）。改这一栏不进缓存键，所以不动像素。</div>
              </div>
              <div class="me-note">这条链没有 panel/label/stamp 与卡面样式那几栏：版式来自母版，
                画面来自代码。要改画面上看得见的东西，看〈字段〉与〈代码〉两页。</div>
            </div>

            <div v-else-if="tab === 'texture'" class="me-form">
              <div class="me-frow">
                <label>这一镜的质感 · {{ curSid }}</label>
                <div class="me-line me-chips">
                  <button v-for="k in ['', ...texEnum]" :key="k || '@default'"
                          class="me-mini pick" :class="{ on: textureDeclared === k }"
                          @click="setTexture(k)">{{ texLabel(k) }}</button>
                </div>
                <div class="me-sub">
                  <template v-if="!texturePointer">（这一镜不在表里）</template>
                  <template v-else>
                    画面上实际是 <b>{{ texLabel(textureEffective) }}</b>
                    <span v-if="textureDeclared === ''">（跟整片默认
                      {{ texLabel(filmTexture) }}）</span>
                    <button v-if="texturePointer in deepEdits" class="me-mini"
                            @click="resetTexture">↺ 回到这一版的档</button>
                  </template>
                </div>
                <div class="me-note">「跟整片默认」与「不加（原片直出）」<b>是两件事</b>：
                  前者把决定权交回顶层那一档（整片改了质感，这一镜跟着变），后者是这一镜
                  点名不要，即使整片加了。其余四档是这一镜自己要的观感，别的镜不受影响。<br />
                  质感<b>不在按镜缓存键里</b>——切片缓存存的是加质感之前的像素，滤镜在缓存之后
                  按镜各过一趟。所以换档（这一镜或整片默认）<b>一帧 Remotion 都不重烧</b>，
                  代价只有加了档的这一镜多过一遍编码。</div>
              </div>
            </div>

            <div v-else-if="tab === 'bgm'" class="me-form">
              <div class="me-frow">
                <label>配乐 ref</label>
                <div class="me-line">
                  <input class="me-in" :value="(hm?.spec?.bgm?.ref || '')"
                         disabled placeholder="未设配乐" />
                </div>
                <div class="me-sub">换歌在左侧音乐库里搜→导入→加入会话，提交时带新 bgm 参数。
                  这里只读当前版本用的哪首。</div>
              </div>
              <div class="me-frow">
                <label>音量 volume（0~0.6）</label>
                <div class="me-line">
                  <input class="me-range" type="range" min="0" max="0.6" step="0.02"
                         :value="bgmEdit.volume !== null ? bgmEdit.volume : (hm?.spec?.bgm?.volume ?? 0.18)"
                         @input="bgmEdit.volume = Number($event.target.value)" />
                  <input class="me-in num" type="number" min="0" max="0.6" step="0.02"
                         :value="bgmEdit.volume !== null ? bgmEdit.volume : (hm?.spec?.bgm?.volume ?? 0.18)"
                         @input="bgmEdit.volume = Number($event.target.value)" />
                </div>
                <div class="me-sub">配乐不进按镜缓存键，改音量只重走混音，一镜都不重烧。</div>
              </div>
              <div class="me-frow">
                <label>人声起时压低 duck</label>
                <div class="me-line">
                  <button class="me-mini"
                          @click="bgmEdit.duck = !(bgmEdit.duck !== null ? bgmEdit.duck : (hm?.spec?.bgm?.duck ?? true))">
                    {{ (bgmEdit.duck !== null ? bgmEdit.duck : (hm?.spec?.bgm?.duck ?? true)) ? '✓ 开' : '✗ 关' }}
                  </button>
                  <span class="me-sub">开：人声出现时自动压低配乐，人声停时恢复</span>
                </div>
              </div>
              <div class="me-note">配乐参数挂在母带层，与单镜无关——改它们整片只重走一遍混音，
                不烧任何一镜的像素。在时间线上点配乐轨也能跳到这里。</div>
            </div>

            <div class="me-submit">
              <button class="me-btn" :disabled="!focusSid" @click="askAbout">
                💬 带着这个选区去问</button>
              <div v-if="busy" class="me-prog">
                <div class="me-progressbar"><i :style="{ width: `${progress.percent}%` }"></i></div>
                <span>{{ progress.status }} · {{ progress.stage || "—" }} · {{ progress.percent }}%</span>
              </div>
              <button class="me-btn primary big" :disabled="!dirty || !!busy" @click="submit">
                {{ busy ? "局部改进行中…" : `提交局部改（${plan}）` }}
              </button>
              <button class="me-btn" :disabled="!dirty || !!busy" @click="resetEdits">清空改动</button>
              <div v-if="!dirty" class="me-note">
                改动清单为空——闸门会拒收「值与原来一样」的补丁：
                出一版一模一样的片子等于白烧一次渲染。
              </div>
            </div>
          </aside>
        </div>
      </template>
  </div>
</template>

<style scoped>
/* 右侧停靠栏：宽度和左边框归父级 aside，这里只填满那一列并自己滚动。
   container-type 让下面的表单栏按面板**实宽**换列——停靠栏比原来的全屏弹窗窄，
   塞不下「画面 + 400px 表单」两栏时把表单挪到画面下面，而不是把画面挤没。 */
.me-panel { background: var(--paper); width: 100%; flex: 1; min-height: 0;
  overflow: auto; container-type: inline-size; }
.me-head { display: flex; align-items: center; gap: 12px; padding: 12px 16px; flex-wrap: wrap;
  border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--paper); z-index: 2; }
.me-head b { font-family: var(--serif); font-size: 16px; }
.me-art { font-size: 12px; color: var(--ink-soft); }
.me-dirty { font-size: 12px; color: var(--vermilion); margin-left: auto; }
.me-x { border: 1px solid var(--line); background: transparent; border-radius: 6px;
  padding: 4px 10px; cursor: pointer; font-size: 12px; }
.me-body { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 16px; padding: 16px; }
@container (max-width: 900px) { .me-body { grid-template-columns: minmax(0, 1fr); } }
.me-left { min-width: 0; }
.me-stage { position: relative; background: #141210; border-radius: 8px; overflow: hidden;
  max-height: 54vh; margin: 0 auto; cursor: crosshair; touch-action: none; }
.me-stage.pick { cursor: default; }
.me-stage video { width: 100%; height: 100%; display: block; object-fit: fill; }
.me-layer { position: absolute; inset: 0; pointer-events: none; }
.me-hit { position: absolute; border: 1px dashed transparent; border-radius: 2px; }
.me-stage:hover .me-hit { border-color: rgba(185, 138, 47, .45); }
.me-hit.on { border: 1px solid var(--vermilion); background: rgba(200, 64, 31, .16); }
.me-hit.on.dead { border-color: #7a7a7a; background: rgba(120, 120, 120, .2); }
.me-hit i { position: absolute; left: 0; top: -13px; font-size: 10px; color: var(--vermilion);
  font-style: normal; }
.me-marquee { position: absolute; border: 1px solid var(--vermilion);
  background: rgba(200, 64, 31, .12); pointer-events: none; }
.me-hint { position: absolute; left: 8px; bottom: 8px; font-size: 11px; color: #f2e9d8;
  background: rgba(20, 18, 16, .62); padding: 3px 8px; border-radius: 5px; pointer-events: none; }
.me-hint b { color: var(--gold); }
.me-ctrl { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
.me-time { font-size: 12px; color: var(--ink-soft); }
.me-old { font-size: 11px; color: var(--ink-soft); margin-left: auto; }
.me-zoom { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 8px 0 4px; }
.me-zoomtag { font-size: 11px; color: var(--ink-soft); }
.me-btn.on { border-color: var(--vermilion); color: var(--vermilion); }
.me-scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;
  background: var(--paper-deep); user-select: none; }
.me-wide { position: relative; display: grid; gap: 3px; padding: 2px 0 6px; }
.me-ruler { position: relative; height: 20px; cursor: ew-resize; touch-action: none; }
.me-tick { position: absolute; top: 0; height: 20px; border-left: 1px solid var(--line); }
.me-tick b { position: absolute; left: 3px; top: 1px; font-size: 10px; font-weight: 400;
  color: var(--ink-soft); white-space: nowrap; }
.me-lane { position: relative; }
.me-lanebody { position: relative; height: 42px; border: 1px solid var(--line);
  border-radius: 6px; background: rgba(255, 255, 255, .5); overflow: hidden; touch-action: none; }
/* 导标浮在块上而不是占一列：占列会把整条 lane 相对秒尺右移，块就对不上刻度了。 */
.me-lanegd { position: absolute; left: 3px; top: 2px; z-index: 3; font-size: 10px;
  color: var(--ink-soft); background: rgba(255, 255, 255, .82); border-radius: 3px;
  padding: 0 4px; pointer-events: none; }
.me-lanegd em { font-style: normal; margin-left: 3px; opacity: .7; }
.me-bar { position: absolute; top: 3px; bottom: 3px; padding: 1px 5px; border: 1px solid var(--line);
  border-radius: 4px; background: #fff; overflow: hidden; cursor: pointer; text-align: left;
  font: inherit; line-height: 1.25; white-space: nowrap; }
.me-bar img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
  opacity: .35; }
.me-bar b, .me-bar span { position: relative; display: block; font-size: 10px; }
.me-bar b { font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
.me-bar span { color: var(--ink-soft); }
.me-bar.now, .me-bar.hot { border-color: var(--gold); }
.me-bar.on { border-color: var(--vermilion); background: rgba(200, 64, 31, .12); }
.me-bar.voice, .me-bar.audio_events { background: rgba(28, 110, 232, .10); }
.me-bar.subtitles { background: rgba(31, 157, 85, .12); }
.me-bar.overlay_events { background: rgba(123, 63, 191, .10); }
.me-bar.bgm { background: rgba(185, 138, 47, .16); }
.me-block { cursor: grab; }
.me-floor { display: block; font-style: normal; font-size: 10px; color: var(--vermilion); }
.me-del { position: absolute; right: 2px; bottom: 2px; font-size: 10px; padding: 1px 4px;
  border: 1px solid var(--line); background: #fff; border-radius: 4px; cursor: pointer; }
.me-edge { position: absolute; right: 0; top: 0; width: 8px; height: 100%; cursor: ew-resize; }
.me-edge:hover { background: rgba(200, 64, 31, .3); }
.me-playline { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--vermilion);
  pointer-events: none; }
.me-removed { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; margin-top: 6px;
  font-size: 11px; color: var(--vermilion); }
.me-track-note { font-size: 11px; color: var(--ink-soft); margin: 6px 0 0; }
.me-track-note b { color: var(--vermilion); }
.me-done { margin-top: 12px; border: 1px solid var(--gold); border-radius: 8px; padding: 10px;
  background: rgba(185, 138, 47, .08); font-size: 12px; display: grid; gap: 6px; }
.me-done-act { display: flex; gap: 6px; flex-wrap: wrap; }
.me-new { width: 100%; max-height: 230px; border-radius: 6px; background: #141210; }
.me-cmp { display: flex; gap: 10px; flex-wrap: wrap; }
.me-cmpcol { width: 150px; }
.me-cmphd { font-size: 11px; color: var(--ink-soft); }
.me-cmpcol img { width: 100%; border-radius: 5px; border: 1px solid var(--line); display: block; }
.me-noimg { font-size: 11px; color: var(--ink-soft); padding: 8px 0; }
.me-fail { margin-top: 12px; border: 1px solid var(--vermilion); border-radius: 8px;
  padding: 10px; background: rgba(200, 64, 31, .07); font-size: 12px; }
.me-fail pre { white-space: pre-wrap; font-family: var(--sans); margin: 6px 0; }
.me-right { border-left: 1px solid var(--line); padding-left: 14px; }
.me-tabs { display: flex; gap: 6px; margin-bottom: 8px; }
.me-tabs button { border: 1px solid var(--line); background: #fff; border-radius: 6px;
  padding: 4px 10px; font-size: 12px; cursor: pointer; }
.me-tabs button.on { border-color: var(--vermilion); color: var(--vermilion); }
.me-tabs em { font-style: normal; color: var(--gold); margin-left: 4px; font-size: 10px; }
/* 框住的那个旋钮在〈字段〉页里点亮这一行：反查的凭据要看得见，不然用户不知道
   界面是按哪一格给的这行。 */
.me-cell.hot { border-color: var(--vermilion); box-shadow: 0 0 0 1px rgba(200, 64, 31, .25); }
.me-code { font-family: ui-monospace, Consolas, monospace; font-size: 11px; line-height: 1.35;
  white-space: pre; resize: vertical; }
.me-bool { display: flex; gap: 6px; align-items: center; font-size: 12px; cursor: pointer; }
.me-bool input { width: 16px; height: 16px; }
.me-vers { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
.me-ver { border: 1px solid var(--line); background: #fff; border-radius: 5px; font-size: 11px;
  padding: 2px 6px; cursor: pointer; }
.me-ver.on { border-color: var(--gold); }
.me-ver em { font-style: normal; color: var(--gold); margin-left: 4px; }
.me-sub { font-size: 10px; color: var(--ink-soft); word-break: break-all; display: block; }
.me-cells { display: grid; gap: 8px; }
.me-cell { border: 1px solid var(--line); border-radius: 6px; padding: 8px; background: #fff; }
.me-cellhd { display: flex; gap: 6px; font-size: 11px; align-items: center; flex-wrap: wrap; }
.me-cellhd code { color: var(--vermilion-deep); word-break: break-all; }
.me-maybe { color: var(--gold); font-style: normal; }
.me-cellt { font-size: 11px; color: var(--ink-soft); margin: 4px 0; }
.me-dead { font-size: 11px; color: var(--vermilion); }
.me-in { width: 100%; border: 1px solid var(--line); border-radius: 5px; padding: 5px 7px;
  font: inherit; font-size: 12px; background: #fff; }
.me-in.key { flex: 0 0 38%; }
.me-form { display: grid; gap: 10px; }
.me-frow label { display: block; font-size: 11px; color: var(--ink-soft); margin-bottom: 3px; }
.me-line { display: flex; gap: 5px; align-items: center; margin-bottom: 4px; }
.me-line .me-in { width: auto; flex: 1 1 auto; min-width: 0; }
.me-mini { border: 1px dashed var(--line); background: transparent; border-radius: 5px;
  font-size: 11px; padding: 2px 7px; cursor: pointer; color: var(--ink-soft); }
.me-chips { flex-wrap: wrap; }
.me-mini.on { border-style: solid; border-color: var(--vermilion); color: var(--vermilion); }
.me-submit { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px;
  display: grid; gap: 8px; }
.me-btn { border: 1px solid var(--line); background: #fff; border-radius: 6px; padding: 5px 10px;
  font-size: 12px; cursor: pointer; }
.me-btn.primary { border-color: var(--vermilion); background: var(--vermilion); color: #fff; }
.me-btn.big { padding: 9px 12px; font-size: 13px; }
.me-btn:disabled { opacity: .45; cursor: not-allowed; }
.me-prog { font-size: 11px; color: var(--ink-soft); }
.me-progressbar { height: 6px; border-radius: 4px; background: var(--paper-deep);
  overflow: hidden; margin-bottom: 4px; }
.me-progressbar i { display: block; height: 100%; background: var(--gold); transition: width .3s; }
.me-ctl { display: flex; gap: 6px; align-items: center; }
.me-ctl .me-in { width: auto; flex: 1 1 auto; min-width: 0; }
.me-ctl .me-in.num { flex: 0 0 78px; width: 78px; }
.me-color { width: 34px; height: 27px; padding: 0; border: 1px solid var(--line);
  border-radius: 5px; background: #fff; cursor: pointer; flex: 0 0 auto; }
.me-range { flex: 1 1 90px; min-width: 70px; }
.me-in.num { flex: 0 0 78px; width: 78px; }
.me-fl { font-size: 11px; color: var(--ink-soft); flex: 0 0 72px; }
.me-askbar { border: 1px solid var(--vermilion); border-radius: 8px; padding: 8px; margin-bottom: 8px;
  background: rgba(200, 64, 31, .07); display: grid; gap: 5px; font-size: 12px; }
.me-note { font-size: 11px; color: var(--ink-soft); }
.me-note.pad { padding: 14px 16px; }
.me-err { font-size: 12px; color: var(--vermilion); padding: 14px 16px; }
</style>
