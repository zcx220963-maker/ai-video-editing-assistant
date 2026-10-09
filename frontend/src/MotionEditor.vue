<script setup>
// 图形科普片的「选区改」编辑器（S3 轨道 + 命中层 + 框选，S5 局部改）。
//
// 为什么自己画这一层而不是嵌第三方剪辑器：要的是「鼠标框住屏幕上这一块，改的就是这一块」，
// 而这件事只有我们的命中表（出片时量好的 元素框 ↔ 分镜字段 凭据）说得出对应关系。
// 第三方剪辑器只认时间线与素材，框不出「这一格字属于 /shots/0/panel/编号」。
//
// 全部改动拼成一次 POST /motion/patch（不重做整片：后端只重烧受影响的那几镜），
// 提交后轮询 GET /render_status——拒绝也落在轮询端的 error 正文里，HTTP 状态码不作判据。
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";

const props = defineProps({
  artifactId: { type: String, required: true },
  convId: { type: String, default: "" },
  mediaUrl: { type: String, default: "" },
  title: { type: String, default: "" },
});
const emit = defineEmits(["close", "patched"]);

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

async function loadHitmap(id) {
  loading.value = true; loadErr.value = ""; hm.value = null;
  try {
    const r = await fetch(`/motion/hitmap?${qs({ artifact_id: id, conv_id: props.convId })}`,
                          { headers: hdr() });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    hm.value = await r.json();
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
    const r = await fetch(`/motion/frame?${qs({ artifact_id: aid, shot: sid,
                                               conv_id: props.convId })}`, { headers: hdr() });
    if (!r.ok) { frameMiss[key] = true; return ""; }
    frameUrls[key] = URL.createObjectURL(await r.blob());
    return frameUrls[key];
  } catch {
    frameMiss[key] = true;
    return "";
  }
}
async function loadFrames(aid, table) {
  await Promise.all((table.timeline || []).map((t) => frameUrl(aid, t.id)));
}

// ---- 指针：与后端 hitmap._segs 同口径（先解 ~1 再解 ~0）----
const segs = (p) => p.split("/").slice(1).map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
function resolvePath(doc, ss) {
  let cur = doc;
  for (const s of ss) {
    if (cur == null) return undefined;
    cur = Array.isArray(cur) ? cur[Number(s)] : cur[s];
  }
  return cur;
}
const timeline = computed(() => hm.value?.timeline || []);
const shots = computed(() => hm.value?.shots || {});
const baseIds = computed(() => timeline.value.map((t) => t.id));
const idxOf = (sid) => baseIds.value.indexOf(sid);
const shotBase = (sid) => (hm.value?.spec?.shots || [])[idxOf(sid)] || {};
const baseValue = (p) => resolvePath(hm.value?.spec, segs(p));
// 类型按原值收：图示的 value/x/y 契约上必须是数字，别等闸门把中文数字打回来。
function coerce(p, raw) {
  const t = typeof baseValue(p);
  if (t === "number") return Number(raw);
  if (t === "boolean") return raw === "true";
  return raw;
}

// ---- 播放与时间线 ----
const stageEl = ref(null), videoEl = ref(null), trackEl = ref(null);
const playUrl = ref(props.mediaUrl);
const cur = ref(0), playing = ref(false), muted = ref(true);
const aspect = computed(() => (hm.value ? `${hm.value.width} / ${hm.value.height}` : "9 / 16"));
const curIdx = computed(() => {
  const tl = timeline.value;
  if (!tl.length) return 0;
  let i = 0;
  tl.forEach((t, k) => { if (cur.value + 0.001 >= (t.start_sec || 0)) i = k; });
  return i;
});
const curSid = computed(() => timeline.value[curIdx.value]?.id || "");
const entries = computed(() => (shots.value[curSid.value] || {}).entries || []);
const headPct = computed(() => {
  const d = hm.value?.duration || 0;
  return d ? `${Math.min(100, (cur.value / d) * 100)}%` : "0%";
});
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
const selected = ref([]);
const marquee = ref(null);
const tab = ref("cells");
let dragStart = null;
const norm = (ev) => {
  const r = stageEl.value.getBoundingClientRect();
  return { x: (ev.clientX - r.left) / r.width, y: (ev.clientY - r.top) / r.height,
           px: ev.clientX - r.left, py: ev.clientY - r.top };
};
const boxStyle = (b) => ({ left: `${b.x * 100}%`, top: `${b.y * 100}%`,
                          width: `${b.w * 100}%`, height: `${b.h * 100}%` });
const visibleBoxes = computed(() => entries.value.map((e) => ({
  ...e, style: boxStyle(e.box || {}), on: selected.value.includes(e.id),
  dead: !e.field || e.confidence === "none",
})));
const overlap = (a, b) => !(a.x + a.w < b.x || b.x + b.w < a.x
                            || a.y + a.h < b.y || b.y + b.h < a.y);

function onStageDown(ev) {
  if (ev.button !== 0 || !stageEl.value) return;
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
    selected.value = entries.value.filter((e) => overlap(mq, e.box || {})).map((e) => e.id);
    tab.value = "cells";
    return;
  }
  // 没拖动 = 单击：多个框叠在一起时取最小的（父块会把子文本再回显一遍）
  const p = norm(ev);
  const hit = entries.value.map((e) => ({ e, b: e.box || {} }))
    .filter(({ b }) => b.x <= p.x && p.x <= b.x + b.w && b.y <= p.y && p.y <= b.y + b.h)
    .sort((a, c) => (a.b.w * a.b.h) - (c.b.w * c.b.h));
  selected.value = hit.length ? [hit[0].e.id] : [];
  tab.value = "cells";
}
// 换镜即清选区：选中的是「这一帧上的这一块」，跳到别的镜还留着就是拿旧框改新字
watch(curSid, () => { selected.value = []; });

// 选中条目 → 按字段指针归组（一行的 key 与 value 常常同指一个 panel 键）
const groups = computed(() => {
  const want = new Set(selected.value);
  const by = new Map();
  for (const e of entries.value) {
    if (!want.has(e.id)) continue;
    const k = e.field || `@none:${e.id}`;
    if (!by.has(k)) by.set(k, { pointer: e.field || "", key: k, hits: [] });
    by.get(k).hits.push(e);
  }
  return [...by.values()];
});

// ---- 改动账 ----
// overrides：整镜表单覆盖的顶层栏（落在这些栏上的指针改动也写进同一份，两处不会口径不一）
// deepEdits：表单没覆盖的深层指针（图示数值、画面里的文字坐标）——选区改的主场
const FORM_FIELDS = ["text", "highlight", "panel", "label", "stamp", "min_duration_sec"];
const overrides = reactive({});
const deepEdits = reactive({});
const removals = ref([]);
const order = ref([]);
function resetEdits() {
  Object.keys(overrides).forEach((k) => delete overrides[k]);
  Object.keys(deepEdits).forEach((k) => delete deepEdits[k]);
  removals.value = [];
  order.value = [...baseIds.value];
  selected.value = [];
}
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
  Object.keys(deepEdits).forEach((p) => {
    const sid = baseIds.value[Number(segs(p)[1])];
    if (sid) touched.add(sid);
  });
  order.value.forEach((sid) => { if (Object.keys(setFor(sid)).length) touched.add(sid); });
  return touched;
});
const dirty = computed(() => Object.keys(deepEdits).length > 0
  || removals.value.length > 0
  || order.value.join(" ") !== baseIds.value.join(" ")
  || changedShots.value.size > 0);

// 时长：拖右边缘改的就是 min_duration_sec。有旁白时镜头长度归真实语音时长，
// 这一栏只剩「下限」的意思——所以拖到配音秒数以下不会有任何效果，这里如实拦住。
function effSec(sid) {
  const tl = timeline.value[idxOf(sid)] || {};
  const floor = Math.max(Number(tl.speech_sec) || 0, 0.5);
  const o = overrides[sid];
  const want = o && "min_duration_sec" in o ? Number(o.min_duration_sec) : (tl.sec || 0);
  return { want: want || 0, floor, sec: Math.max(want || 0, floor) };
}
const blocks = computed(() => {
  const items = order.value.map((sid) => {
    const e = effSec(sid);
    const removed = removals.value.includes(sid);
    return { sid, removed, want: e.want, floor: e.floor, sec: e.sec,
             grow: removed ? 0.6 : e.sec,
             thumb: frameUrls[`${artifact.value}|${sid}`] || "" };
  });
  const total = items.reduce((a, b) => a + b.grow, 0) || 1;
  items.forEach((b) => { b.pct = (b.grow / total) * 100; });
  return items;
});
const estTotal = computed(() => blocks.value
  .filter((b) => !b.removed).reduce((a, b) => a + b.sec, 0));

// 轨道拖拽：块身拖 = 换序，右边缘拖 = 改时长，纯点击 = 定位到那一镜
let trackDrag = null;
function blockRects() {
  return [...(trackEl.value?.querySelectorAll(".me-block") || [])]
    .map((el) => el.getBoundingClientRect());
}
function onBlockDown(ev, sid, i) {
  if (ev.button !== 0) return;
  trackDrag = { kind: "move", sid, i, x0: ev.clientX, moved: false };
  window.addEventListener("pointermove", onTrackMove);
  window.addEventListener("pointerup", onTrackUp, { once: true });
}
function onResizeDown(ev, sid) {
  ev.stopPropagation();
  if (ev.button !== 0) return;
  const e = effSec(sid);
  const total = order.value.reduce((a, s) => a + effSec(s).sec, 0) || 1;
  trackDrag = { kind: "resize", sid, x0: ev.clientX, sec0: e.want, moved: false,
                floor: e.floor, pxPerSec: (trackEl.value?.clientWidth || 600) / total };
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
    const tl = timeline.value[idxOf(d.sid)] || {};
    seek(tl.start_sec || 0);
  }
}
const toggleRemove = (sid) => {
  removals.value = removals.value.includes(sid)
    ? removals.value.filter((x) => x !== sid) : [...removals.value, sid];
};

// ---- 表单小工具：panel/label 的键名也要能改（指针只认已存在的字段，改名只能整镜写回）----
function renameKey(sid, field, oldKey, ev) {
  const nk = ev.target.value.trim();
  const src = overrideFor(sid)[field] || {};
  if (!nk || nk === oldKey || nk in src) return;
  const out = {};
  Object.entries(src).forEach(([k, v]) => { out[k === oldKey ? nk : k] = v; });
  overrideFor(sid)[field] = out;
}
function addRow(sid, field) {
  const o = overrideFor(sid);
  const src = (o[field] ||= {});
  let n = 1;
  while (`新字段 ${n}` in src) n += 1;
  src[`新字段 ${n}`] = "";
}
function delRow(sid, field, key) {
  const src = { ...(overrideFor(sid)[field] || {}) };
  delete src[key];
  overrideFor(sid)[field] = src;
}
function addHighlight(sid) { const o = overrideFor(sid); (o.highlight ||= []).push(""); }
function delHighlight(sid, k) { overrideFor(sid).highlight.splice(k, 1); }

// ---- 提交：拼成一次局部改 ----
const busy = ref("");
const failMsg = ref("");
const done = ref(null);
const progress = reactive({ status: "", stage: "", percent: 0 });
let pollAbort = false;

function payload() {
  const edits = Object.entries(deepEdits).map(([p, value]) => ({
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
  return body;
}
const planBits = computed(() => {
  const p = payload();
  const bits = [];
  if (p.edits) bits.push(`逐格 ${p.edits.length} 处`);
  if (p.shot_sets) bits.push(`整镜 ${p.shot_sets.length} 镜`);
  if (p.remove_shots) bits.push(`删 ${p.remove_shots.length} 镜`);
  if (p.reorder) bits.push("换序");
  return bits;
});
const plan = computed(() => (planBits.value.length ? planBits.value.join(" · ") : "还没有改动"));
const rebakeNote = computed(() => {
  const p = payload();
  const n = changedShots.value.size;
  if (!p.reorder || n) return `预计重烧 ${n} 镜`;
  return "重排不烧像素（拼而已）";
});

async function submit() {
  if (!dirty.value || busy.value) return;
  busy.value = "submitting"; failMsg.value = ""; done.value = null;
  progress.status = "queued"; progress.percent = 0; progress.stage = "提交中";
  try {
    const r = await fetch("/motion/patch", {
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
  done.value = {
    id, base, url: view.media_url || "", duration: view.duration ?? null,
    changed: patch.changed_shots || [], removed: patch.removed_shots || [],
    reordered: !!patch.reordered, hadBefore: Object.keys(before).length,
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
async function prepareCompare() {
  const d = done.value;
  if (!d) return;
  await Promise.all(d.changed.flatMap((sid) => [frameUrl(d.base, sid), frameUrl(d.id, sid)]));
}
const compare = computed(() => {
  const d = done.value;
  if (!d) return [];
  return d.changed.map((sid) => ({
    sid, base: frameUrls[`${d.base}|${sid}`] || "", after: frameUrls[`${d.id}|${sid}`] || "",
    removed: d.removed.includes(sid),
  }));
});
async function adopt(id) {
  artifact.value = id;
  playUrl.value = chain.find((c) => c.id === id)?.url || playUrl.value;
  done.value = null; failMsg.value = ""; cur.value = 0; playing.value = false;
  await loadHitmap(id);
}
onBeforeUnmount(() => {
  pollAbort = true;
  Object.values(frameUrls).forEach((u) => URL.revokeObjectURL(u));
});

loadHitmap(props.artifactId);
</script>

<template>
  <div class="me-mask" @click.self="emit('close')">
    <div class="me-panel">
      <header class="me-head">
        <b>✂ 选区改 · {{ title || "图形科普片" }}</b>
        <span class="me-art">在改版本 {{ artifact }}</span>
        <span v-if="dirty" class="me-dirty">未提交：{{ plan }} · {{ rebakeNote }}</span>
        <button class="me-x" @click="emit('close')">✕ 关闭</button>
      </header>

      <div v-if="loading" class="me-note pad">正在读这一版的命中表…</div>
      <div v-else-if="loadErr" class="me-err">
        {{ loadErr }}
        <div class="me-sub">没有命中表就只能整片重做——屏幕上这一块对不回分镜的哪一格。
          要让这一版可改，请重新出片一次（出片时顺手量表）。</div>
      </div>

      <template v-else-if="hm">
        <div class="me-body">
          <section class="me-left">
            <div ref="stageEl" class="me-stage" :style="{ aspectRatio: aspect }"
                 @pointerdown="onStageDown">
              <video ref="videoEl" :src="playUrl" :muted="muted" preload="metadata"
                     @timeupdate="onTime" @ended="playing = false"></video>
              <div class="me-layer">
                <span v-for="b in visibleBoxes" :key="b.id" class="me-hit"
                      :class="{ on: b.on, dead: b.dead }" :style="b.style"
                      :title="b.field || '量到了字，但回指不到分镜字段'"><i v-if="b.on">✎</i></span>
              </div>
              <div v-if="marquee" class="me-marquee" :style="boxStyle(marquee)"></div>
              <div class="me-hint">在画面上<b>按住拖</b>框住要改的那块字/图，点一下选单格</div>
            </div>

            <div class="me-ctrl">
              <button class="me-btn" @click="toggle">{{ playing ? "⏸ 暂停" : "▶ 播放" }}</button>
              <button class="me-btn"
                      @click="muted = !muted; videoEl && (videoEl.muted = muted)">
                {{ muted ? "🔇 静音" : "🔊 有声" }}</button>
              <span class="me-time">{{ cur.toFixed(1) }}s / {{ (hm.duration || 0).toFixed(1) }}s
                · 第 {{ curIdx + 1 }}/{{ timeline.length }} 镜（{{ curSid }}）</span>
              <span class="me-old">放着的是<b>这一版的原片</b>，改动要提交之后才看得见</span>
            </div>

            <div ref="trackEl" class="me-track">
              <div v-for="(b, i) in blocks" :key="b.sid" class="me-block"
                   :class="{ del: b.removed, hot: b.sid === curSid }"
                   :style="{ flexGrow: b.grow, flexBasis: 0 }"
                   @pointerdown="onBlockDown($event, b.sid, i)">
                <img v-if="b.thumb" :src="b.thumb" alt="" />
                <div class="me-bk">
                  <b>{{ i + 1 }} · {{ b.sid }}</b>
                  <span>{{ b.sec.toFixed(1) }}s</span>
                  <em v-if="b.want < b.floor" class="me-floor">拖到 {{ b.floor.toFixed(1) }}s
                    以下无效：这一镜的时钟归配音</em>
                </div>
                <button class="me-del" @click.stop="toggleRemove(b.sid)">
                  {{ b.removed ? "↺ 恢复" : "✕ 删镜" }}</button>
                <span class="me-edge" @pointerdown="onResizeDown($event, b.sid)"></span>
              </div>
              <div class="me-playline" :style="{ left: headPct }"></div>
            </div>
            <div class="me-track-note">
              块宽 ∝ 秒数 · 拖块身换序 · 拖右边缘改 min_duration_sec · 点块定位到那一镜 ·
              改完合计约 {{ estTotal.toFixed(1) }}s（原来 {{ (hm.duration || 0).toFixed(1) }}s）
            </div>

            <div v-if="done" class="me-done">
              <b>✓ 新的一版：{{ done.id }}</b>
              <span>改了 {{ done.changed.length }} 镜{{ done.changed.length
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
                  <div class="me-cmphd">{{ c.sid }} 改前</div>
                  <img v-if="c.base" :src="c.base" alt="" />
                  <div v-else class="me-noimg">旧版没留这一镜的代表帧</div>
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
              <button :class="{ on: tab === 'cells' }" @click="tab = 'cells'">选中格</button>
              <button :class="{ on: tab === 'shot' }" @click="tab = 'shot'">整镜 {{ curSid }}</button>
            </div>

            <div class="me-vers">
              <button v-for="c in chain" :key="c.id" class="me-ver"
                      :class="{ on: c.id === artifact }" @click="adopt(c.id)">
                {{ c.id }}<em v-if="c.id === artifact">在改</em>
              </button>
              <span class="me-sub">这里只列本次编辑器里走过的版本——服务端还没有
                「列出某会话全部版本」的口，跨刷新找旧版要在对话里报产物号。</span>
            </div>

            <div v-if="tab === 'cells'" class="me-cells">
              <div v-if="!selected.length" class="me-note">
                还没框到东西。当前这一镜（{{ curSid }}）量到 {{ entries.length }} 格，
                其中 {{ entries.filter((e) => e.field).length }} 格能回指到分镜字段。
                <div v-if="(shots[curSid] || {}).error" class="me-dead">
                  这一镜的表没量成：{{ (shots[curSid] || {}).error }}
                </div>
              </div>
              <div v-for="g in groups" :key="g.key" class="me-cell">
                <div class="me-cellhd">
                  <code>{{ g.pointer || "（回指不到字段）" }}</code>
                  <em v-if="!g.pointer">只能整镜改写</em>
                  <em v-else-if="g.hits.some((h) => h.confidence !== 'exact')"
                      class="me-maybe">按文字近似对上，请核对</em>
                  <em v-else-if="g.hits.some((h) => h.ambiguous)" class="me-maybe">多处同字</em>
                </div>
                <div class="me-cellt">画面上是：{{ g.hits[0].text }}</div>
                <template v-if="g.pointer">
                  <input class="me-in" :value="editValue(g.pointer) ?? ''"
                         @input="setEdit(g.pointer, coerce(g.pointer, $event.target.value))" />
                  <div class="me-sub">原值：{{ JSON.stringify(baseValue(g.pointer)) }}</div>
                </template>
                <div v-else class="me-dead">
                  这一格没有可写的字段。要改它请切到「整镜」改对应栏，或重新出片。
                </div>
              </div>
            </div>

            <div v-else class="me-form">
              <div class="me-frow">
                <label>文案 text（字幕与配音同源，改这里等于换这一镜说的话）</label>
                <textarea class="me-in" rows="2" :value="overrideFor(curSid).text ?? ''"
                          @input="overrideFor(curSid).text = $event.target.value"></textarea>
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
                <label>档案栏 panel（左列是屏幕上那行的小标，键名也能改）</label>
                <div v-for="(v, k) in (overrideFor(curSid).panel || {})" :key="k" class="me-line">
                  <input class="me-in key" :value="k" @blur="renameKey(curSid, 'panel', k, $event)" />
                  <input class="me-in val" :value="v"
                         @input="overrideFor(curSid).panel[k] = $event.target.value" />
                  <button class="me-mini" @click="delRow(curSid, 'panel', k)">✕</button>
                </div>
                <button class="me-mini" @click="addRow(curSid, 'panel')">＋ 加一行</button>
              </div>
              <div class="me-frow">
                <label>标签 label</label>
                <div v-for="(v, k) in (overrideFor(curSid).label || {})" :key="k" class="me-line">
                  <input class="me-in key" :value="k" @blur="renameKey(curSid, 'label', k, $event)" />
                  <input class="me-in val" :value="v"
                         @input="overrideFor(curSid).label[k] = $event.target.value" />
                  <button class="me-mini" @click="delRow(curSid, 'label', k)">✕</button>
                </div>
                <button class="me-mini" @click="addRow(curSid, 'label')">＋ 加一行</button>
              </div>
              <div class="me-frow">
                <label>印章 stamp</label>
                <div class="me-line">
                  <input class="me-in" placeholder="中文"
                         :value="(overrideFor(curSid).stamp || {}).zh ?? ''"
                         @input="overrideFor(curSid).stamp = { ...(overrideFor(curSid).stamp || {}), zh: $event.target.value }" />
                  <input class="me-in" placeholder="EN"
                         :value="(overrideFor(curSid).stamp || {}).en ?? ''"
                         @input="overrideFor(curSid).stamp = { ...(overrideFor(curSid).stamp || {}), en: $event.target.value }" />
                </div>
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
              <div class="me-note">版式（card）与画面（visual）不在表单里改：
                换版式等于每一镜都要重烧，那是重新出片；画面里的数字与文字，
                请在左边框住后用「选中格」改。</div>
            </div>

            <div class="me-submit">
              <div v-if="busy" class="me-prog">
                <div class="me-bar"><i :style="{ width: `${progress.percent}%` }"></i></div>
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
  </div>
</template>

<style scoped>
.me-mask { position: fixed; inset: 0; z-index: 60; background: rgba(38, 34, 28, .55);
  display: flex; align-items: center; justify-content: center; padding: 18px; }
.me-panel { background: var(--paper); border: 1px solid var(--line); border-radius: 10px;
  width: min(1240px, 100%); max-height: 96vh; overflow: auto;
  box-shadow: 0 18px 48px rgba(0, 0, 0, .28); }
.me-head { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--paper); z-index: 2; }
.me-head b { font-family: var(--serif); font-size: 16px; }
.me-art { font-size: 12px; color: var(--ink-soft); }
.me-dirty { font-size: 12px; color: var(--vermilion); margin-left: auto; }
.me-x { border: 1px solid var(--line); background: transparent; border-radius: 6px;
  padding: 4px 10px; cursor: pointer; font-size: 12px; }
.me-body { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 16px; padding: 16px; }
.me-left { min-width: 0; }
.me-stage { position: relative; background: #141210; border-radius: 8px; overflow: hidden;
  max-height: 54vh; margin: 0 auto; cursor: crosshair; touch-action: none; }
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
.me-track { position: relative; display: flex; gap: 4px; height: 78px; padding: 6px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--paper-deep);
  touch-action: none; user-select: none; }
.me-block { position: relative; min-width: 46px; border: 1px solid var(--line);
  border-radius: 6px; background: #fff; overflow: hidden; cursor: grab; }
.me-block.hot { border-color: var(--gold); }
.me-block.del { opacity: .4; filter: grayscale(1); }
.me-block img { position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; opacity: .55; }
.me-bk { position: relative; padding: 3px 6px; font-size: 11px; line-height: 1.3; }
.me-bk b { display: block; font-family: var(--serif); }
.me-bk span { color: var(--ink-soft); }
.me-floor { display: block; font-style: normal; font-size: 10px; color: var(--vermilion); }
.me-del { position: absolute; right: 2px; bottom: 2px; font-size: 10px; padding: 1px 4px;
  border: 1px solid var(--line); background: #fff; border-radius: 4px; cursor: pointer; }
.me-edge { position: absolute; right: -3px; top: 0; width: 8px; height: 100%;
  cursor: ew-resize; }
.me-edge:hover { background: rgba(200, 64, 31, .3); }
.me-playline { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--vermilion);
  pointer-events: none; }
.me-track-note { font-size: 11px; color: var(--ink-soft); margin: 6px 0 0; }
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
.me-mini { border: 1px dashed var(--line); background: transparent; border-radius: 5px;
  font-size: 11px; padding: 2px 7px; cursor: pointer; color: var(--ink-soft); }
.me-submit { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px;
  display: grid; gap: 8px; }
.me-btn { border: 1px solid var(--line); background: #fff; border-radius: 6px; padding: 5px 10px;
  font-size: 12px; cursor: pointer; }
.me-btn.primary { border-color: var(--vermilion); background: var(--vermilion); color: #fff; }
.me-btn.big { padding: 9px 12px; font-size: 13px; }
.me-btn:disabled { opacity: .45; cursor: not-allowed; }
.me-prog { font-size: 11px; color: var(--ink-soft); }
.me-bar { height: 6px; border-radius: 4px; background: var(--paper-deep);
  overflow: hidden; margin-bottom: 4px; }
.me-bar i { display: block; height: 100%; background: var(--gold); transition: width .3s; }
.me-note { font-size: 11px; color: var(--ink-soft); }
.me-note.pad { padding: 14px 16px; }
.me-err { font-size: 12px; color: var(--vermilion); padding: 14px 16px; }
</style>
