"""Connection Manager：文档数据流的下半程「Agent → MQ OutBound → MQ Consumer → Connection Manager → 对应 Web 连接」。

Agent 侧把流式 delta 与最终结果发布到 OutBound topic（key=session_id），
本模块作为该 topic 的消费端维护 session_id → set[WebSocket] 注册表，
按消息里的 session_id 找到对应会话窗口的连接逐条推送；结果不会串到
别人的窗口——这正是文档第 6 节「结果发给谁」的落点。

同一 session 允许多个连接（多标签页），全部收到；单个连接发送失败
（浏览器已断开）只摘除该连接，不影响其余。

事件环形缓冲：每个 session 维护最近 N 帧事件，新连接注册时重放——
用户刷新页面后 WebSocket 重连，能补看到断连期间错过的进度。落定（拿到 answer 帧）的
那一轮**只留终态、清掉中间帧**：进度条与逐字 delta 不再重复占位，而答复本身一定补得到
（前端重连只重开 socket、不重拉历史，早期「answer 直接不进缓冲」的口径会让恰好断在
收尾那几秒的答复哪儿都拿不到）。重放帧统一带 `replayed: true`，客户端可自行与
``/convs/{id}/messages`` 的历史去重。

**跨副本影子预算**：本实例没挂某条 session 的连接时，这一帧仍然写缓冲、只是不投递
（`buffer_only`，见 `broadcast.py`）——不然浏览器被别的副本接走后，重连只能补到接管之后
的进度。这块缓冲有两条常数上界（`shadow_sessions` 条会话、合计 `shadow_frames` 帧），
超了整条丢掉最久没人碰的会话，因此内存不随「会话数 × 副本数」涨。会话最后一次本地连接
断开时也并入同一预算：曾经「每来过一个会话就永久留一条 deque」是更早存在的漏。
观测计数 `sent` 同理只留最近 `obs_limit` 条。

**这一层同时是中文名的出口**（见 ``catalog.py``）：帧的生产方（Hook、别的副本）只认
机器名，写进 WS 之前才换成给人看的那一版。环形缓冲里存的仍是原帧，重放走同一条出口，
所以「刷新后补看」与「当轮直播」文案一致。delta 帧按连接各持一个挂起缓冲：模型的字是
一个 token 一个 token 吐的，``split_sho`` + ``ts`` 之间不许发残片。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

from .catalog import StreamRewriter, get_catalog
from .mq import MessageQueue

OUTBOUND_TOPIC = "outbound"
CONNECTION_GROUP = "connection-manager"
_DEFAULT_BUFFER_SIZE = 200
# 观测计数：测试与「这一路到底投了什么」的现场勘查用。以前是 list、每帧永久追加一份
# （帧里带整段工具结果与媒体载荷），长跑必漏；现在只留最近这些条。
_DEFAULT_OBS_LIMIT = 500
# 跨副本影子预算：本实例没挂连接的会话也攒帧（接管后重连才补得回接管之前的进度），
# 但上界由这两条常数说了算，而不是「会话数 × 副本数」。
_DEFAULT_SHADOW_SESSIONS = 64
_DEFAULT_SHADOW_FRAMES = 2000


class ConnectionManager:
    def __init__(
        self,
        mq: MessageQueue,
        *,
        topic: str = OUTBOUND_TOPIC,
        group: str = CONNECTION_GROUP,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
        shadow_sessions: int = _DEFAULT_SHADOW_SESSIONS,
        shadow_frames: int = _DEFAULT_SHADOW_FRAMES,
        obs_limit: int = _DEFAULT_OBS_LIMIT,
    ) -> None:
        self.mq = mq
        self.topic = topic
        self.group = group
        self._sockets: dict[str, set[Any]] = {}
        self.sent: deque[dict[str, Any]] = deque(maxlen=max(0, obs_limit))  # 观测 / 测试
        self._subscribed = False
        self._buffer_size = buffer_size
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        # 每个 session 里「已经落定（拿到 answer）」的 run_id。用它精确判断该清谁的中间帧。
        self._finished: dict[str, deque[str]] = {}
        # 每条连接自己的流式挂起缓冲（词表共享、状态不共享）
        self._streams: dict[Any, StreamRewriter] = {}
        # 「本实例此刻没挂它的连接」的会话，按最近碰过排序（先进=最先被淘汰）。
        self._shadow: OrderedDict[str, None] = OrderedDict()
        self._shadow_cap = max(0, shadow_sessions)
        self._shadow_frame_cap = max(0, shadow_frames)

    def start(self) -> None:
        """订阅 OutBound（需在 mq.start() 之前调用）。"""
        if not self._subscribed:
            self.mq.subscribe(self.topic, self.group, self.route)
            self._subscribed = True

    def connect(self, session_id: str, websocket: Any) -> None:
        self._sockets.setdefault(session_id, set()).add(websocket)
        # 本地有人看了：这条会话的缓冲不再占跨副本预算（上界回到每会话那条 deque）
        self._shadow.pop(session_id, None)
        self._streams[websocket] = StreamRewriter(get_catalog())

    def disconnect(self, session_id: str, websocket: Any) -> None:
        self._streams.pop(websocket, None)
        socks = self._sockets.get(session_id)
        if socks is None:
            return
        socks.discard(websocket)
        if not socks:
            del self._sockets[session_id]
            # 本地最后一条连接走了：这条会话从此只可能被别的副本接管，并入同一预算，
            # 否则「每来过一个会话就永久留一条 deque」是比跨副本更早就存在的漏。
            self._touch_shadow(session_id)

    def sockets_of(self, session_id: str) -> int:
        return len(self._sockets.get(session_id, ()))

    # ---- 跨副本影子预算：本实例没挂连接的会话「只攒不投」 -----------------

    def _touch_shadow(self, session_id: str) -> None:
        self._shadow.pop(session_id, None)
        self._shadow[session_id] = None      # 排到最近

    def _drop_shadow(self, session_id: str) -> None:
        self._shadow.pop(session_id, None)
        if not self.sockets_of(session_id):
            self.clear_buffer(session_id)

    def _trim_shadow(self) -> None:
        """两条上界：会话条数、这些会话合计帧数。超了先丢最久没人碰的**整条**会话。

        按整条丢而不是按帧丢：留下半截进度比什么都不留更容易让人误判「我看到的就是全部」。
        """
        while len(self._shadow) > self._shadow_cap:
            self._drop_shadow(next(iter(self._shadow)))
        total = sum(len(self._buffers.get(s, ())) for s in self._shadow)
        while self._shadow and total > self._shadow_frame_cap:
            victim = next(iter(self._shadow))
            total -= len(self._buffers.get(victim, ()))
            self._drop_shadow(victim)

    def buffer_only(self, payload: dict[str, Any]) -> bool:
        """攒下别的副本消费的帧，不投递。返回是否真的留下了。

        广播原先对「本地查无连接」的帧整条跳过，理由是不想按「会话数 × 副本数」吃内存；
        代价是浏览器被别的副本接走时，补不回接管之前的进度。现在改成**有界地攒**：
        只占 ``shadow_sessions`` 条会话、合计 ``shadow_frames`` 帧这一块 LRU 名额，
        真超了就整条丢掉最久没人碰的——上界是常数，不随会话数与副本数乘起来。
        """
        session_id = payload.get("session_id")
        if not session_id or self.sockets_of(session_id):
            return False          # 有连接的该走 route()：那条既投也攒
        if self._shadow_cap == 0 or self._shadow_frame_cap == 0:
            return False          # 显式关掉预算 = 回到「整条跳过」的老口径
        self._touch_shadow(session_id)
        self._buffer_append(session_id, payload)
        self._trim_shadow()
        return session_id in self._shadow

    def shadow_of(self) -> list[str]:
        """当前占着跨副本预算的会话（最久没人碰的在前）——测试与现场勘查用。"""
        return list(self._shadow)

    def _buffer_append(self, session_id: str, payload: dict[str, Any]) -> None:
        """进环形缓冲——**包括 answer 帧**。

        原先对 ``answer`` 直接 return，理由是「答复落定后这一轮由 REST
        ``/convs/{id}/messages`` 给全，缓冲再留一份就是界面上出现两次」。
        但那条理由只在前端**真的会去读历史**时成立：前端重连路径只重开 socket，
        不重拉历史，而 ``loadHistory`` 又被 ``if (histories[cid]) return`` 挡住。
        于是断连恰好落在收尾那几秒时，这条答复哪儿都拿不到——要整页刷新才看得到。

        现在缓冲保留完整一轮（含终答），重放时统一打 ``replayed: true``：
        客户端可以据此去重（历史已经给过的就不用再画一遍），但**至少拿得到**。
        「宁可让客户端有机会去重，也不要让答复永久消失」是这里的取舍。
        """
        buf = self._buffers.get(session_id)
        if buf is None:
            buf = deque(maxlen=self._buffer_size)
            self._buffers[session_id] = buf
        buf.append(payload)
        if payload.get("type") == "answer":
            run = str(payload.get("run_id") or "")
            if run:
                done = self._finished.setdefault(
                    session_id, deque(maxlen=self._buffer_size))
                if run not in done:
                    done.append(run)
        self._prune_finished(session_id)

    def _prune_finished(self, session_id: str) -> None:
        """把**已落定**那几轮的中间帧清掉，但保留它们各自的终态帧（末尾那条）。

        判据是「run 拿到过 answer」而不是「这一段的第一帧」：原先按「这条 run 的第一帧
        位置往后清」实现，只要缓冲区里还有别条 run 的在途进度夹在中间，就会把它们
        一起清掉——实测 A:2 那段序列里 r-3 的在途 delta 就这么被 r-2b 的答复误删了。
        「已落定」用独立集合记，不依赖帧还在不在缓冲里，因此判断是确定的。

        末尾那条不参与清理：它必然是本轮刚落下的终态帧（或更晚的一条），
        清它是「答复又消失」，正是这次要修的东西。
        """
        buf = self._buffers.get(session_id)
        done = self._finished.get(session_id)
        if not buf or not done:
            return
        done_set = set(done)
        frames = list(buf)
        kept = [p for p in frames[:-1] if str(p.get("run_id") or "") not in done_set]
        kept.append(frames[-1])
        if len(kept) != len(frames):
            self._buffers[session_id] = deque(kept, maxlen=self._buffer_size)

    def _prune_finished_run(self, session_id: str, run_id: Any,
                            keep: dict[str, Any] | None = None) -> None:
        """（已由 ``_prune_finished`` 取代，保留仅为兼容旧调用点。）

        老实现按「这条 run 的第一帧位置往后清」判据，会把夹在中间的其他 run 在途进度
        一起清掉。新判据见 ``_prune_finished``：用独立的「已落定 run」集合来判。
        """
        self._prune_finished(session_id)

    def get_buffer(self, session_id: str) -> list[dict[str, Any]]:
        """返回某 session 的缓冲事件列表（供 REST 端点或重放使用）。"""
        return list(self._buffers.get(session_id, ()))

    def clear_buffer(self, session_id: str) -> None:
        self._buffers.pop(session_id, None)
        self._finished.pop(session_id, None)
        self._shadow.pop(session_id, None)

    async def _emit(self, websocket: Any, payload: dict[str, Any]) -> None:
        """写一条连接的唯一出口：出去之前过一遍中文名词表。"""
        catalog = get_catalog()
        kind = payload.get("type")
        if kind == "tool_call":
            # 参数名是**键位**，进不了文本替换（普通词会咬坏正文），所以在出帧这一站
            # 整帧挂一条 arg_labels。单源在服务端的 PARAM_LABELS，因此空词表也要挂——
            # 那半张表不靠 Storyline 连不连上。
            labels = catalog.arg_labels(payload.get("arguments"))
            if labels:
                payload = {**payload, "arg_labels": labels}
        if not catalog:                       # 词表还没装配（Storyline 没连上）：全直通
            await websocket.send_json(payload)
            return
        if kind == "delta":
            rewriter = self._streams.get(websocket)
            if rewriter is None:
                rewriter = self._streams[websocket] = StreamRewriter(catalog)
            text = rewriter.feed(str(payload.get("text") or ""))
            if not text:
                return                       # 整帧还扣着：宁可不发，也不发残片
            frame = dict(payload)
            frame["text"] = text
            await websocket.send_json(frame)
            return
        if kind == "stream_end":
            rewriter = self._streams.get(websocket)
            tail = rewriter.flush() if rewriter is not None else ""
            if tail:
                frame = dict(payload)
                frame["type"] = "delta"
                frame["text"] = tail
                await websocket.send_json(frame)
        await websocket.send_json(catalog.rewrite_obj(payload))

    async def replay_to(self, session_id: str, websocket: Any) -> int:
        """把缓冲事件重放给指定连接（新连接注册后调用）。返回重放帧数。

        重放的帧统一打 ``replayed: true``：客户端据此知道「这条是补看的历史，
        可能 REST 历史里也有」，可以按 run_id 去重。缓冲里存的仍是原帧，
        中文名出口与直播同一条，所以文案一致。
        """
        buf = self._buffers.get(session_id)
        if not buf:
            return 0
        count = 0
        for payload in list(buf):
            try:
                await self._emit(websocket, {**payload, "replayed": True})
                count += 1
            except Exception:  # noqa: BLE001
                break
        return count

    async def route(self, payload: dict[str, Any]) -> None:
        """OutBound 消费入口：按 session_id 找回对应 Web 连接并推送。"""
        self.sent.append(payload)
        session_id = payload.get("session_id")
        if session_id:
            self._buffer_append(session_id, payload)
            if not self.sockets_of(session_id):
                # 本地没人看它（broker 不可达时退回本地投递会走到这里）：并入同一预算，
                # 不然这份缓冲没有任何淘汰机会。
                self._touch_shadow(session_id)
                self._trim_shadow()
        for ws in list(self._sockets.get(session_id, ())):
            try:
                await self._emit(ws, payload)
            except Exception:  # noqa: BLE001  连接已断开：摘除坏连接，不影响其他
                self.disconnect(session_id, ws)
