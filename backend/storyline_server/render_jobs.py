"""长耗时节点（目前只有 render_video）的后端执行位：提交即回句柄，进度查 render_jobs 行。

一次 1080p 渲染要跑几分钟，而 MCP 是一次阻塞的 JSON-RPC 往返：渲染留在请求线程里，
客户端超时就是「一句话出片」稳定失败的那一面（历史上 tool_timeout 从 600s 一路抬到
1800s，抬预算并不改变「一次调用等一部片子」这个形状）。这里把执行挪出请求生命周期：

* handler 只做「登记 + 起跑」，立刻回一张任务视图；
* 渲染体仍在原节点里跑（MoviePy→ffmpeg 兜底、终态写 render_jobs 的语义都不变），
  本模块只保证它活在一个不被请求取消的后台任务里；
* 进度/结果的唯一真相是 ``render_jobs`` 行，所以任何实例、进程重启之后都能查到 ——
  这也是 ``render_status`` 不依赖内存状态的原因。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

TERMINAL = ("done", "failed")

# 渲染是 ffmpeg/MoviePy 级 CPU 活，同时开太多只会互相拖慢；上限由配置给。
DEFAULT_MAX_CONCURRENT = 2
# 看门狗的检查节奏：停滞阈值是分钟级，30 秒一跳足够快也不至于一直在读库。
STALL_CHECK_SEC = 30.0


class RenderDispatcher:
    """把「跑一部片子的协程」放到后台，并给出可轮询的视图。"""

    def __init__(self, storage: Any, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                 poll_interval_sec: float = 0.5, stall_sec: float = 0.0) -> None:
        self.storage = storage
        self._limit = max(1, int(max_concurrent))
        # 延迟到首个后台任务里再建：装配体（StorylineServer）是在事件循环外构造的
        self._slots: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        # 已拿到槽、正在跑渲染体的 key：排队等槽的行「没有进展」是正常状态，
        # 不能和活儿死了混为一谈，看门狗要区分这两者。
        self._active: set[str] = set()
        self._poll = poll_interval_sec
        self._stall_sec = float(stall_sec)
        self._watchdog: asyncio.Task | None = None
        # 判死后仍未真正结束的渲染（MoviePy 跑在 to_thread 里，Python 杀不掉那个线程）。
        # 这些 key 必须继续占着，否则重试会**并发开第二份渲染写同一个产物**：
        # 真机现场留下过 69 个分段时间戳分两波、互相覆盖的痕迹。
        self._reaped: set[str] = set()

    def _semaphore(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self._limit)
        return self._slots

    @staticmethod
    def key(session_id: str, artifact_id: str) -> str:
        return f"{session_id}|{artifact_id or '_default'}"

    @property
    def inflight(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def submit(self, session_id: str, artifact_id: str,
                     work: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
        """登记一次渲染并起跑（已在跑/在排队的同一产物不重开）。"""
        art = artifact_id or "_default"
        await self.storage.render_jobs.enqueue(session_id, art)
        key = self.key(session_id, art)
        running = self._tasks.get(key)
        # ``_reaped`` 里的 key 是「已被判死、但旧渲染其实还在跑」：这时**不能**再起一份，
        # 否则两份同时写同一个 dst。仍然回一张视图让调用方继续轮询——旧渲染真跑完时
        # 会把行改写成 done（那是「片子的确存在」），到那时重试自然就能接手了。
        if running is None or running.done():
            if key in self._reaped:
                snap = await self.snapshot(session_id, art)
                if snap and snap["status"] not in TERMINAL:
                    return snap
                self._reaped.discard(key)      # 旧渲染已收口，可以重来
            self._tasks[key] = asyncio.create_task(
                self._guarded(key, session_id, art, work))
        snap = await self.snapshot(session_id, art)
        return snap or {"status": "queued", "session_id": session_id,
                        "artifact_id": art, "percent": 0, "stage": "queued"}

    async def _guarded(self, key: str, session_id: str, artifact_id: str,
                       work: Callable[[], Awaitable[Any]]) -> Any:
        try:
            async with self._semaphore():
                self._active.add(key)
                try:
                    return await work()
                finally:
                    self._active.discard(key)
        except Exception as exc:  # noqa: BLE001 - 后台任务里的异常不能只留给日志
            # 节点自己会在失败路径写 failed；没写到（上游补齐阶段就炸、进程内崩溃）
            # 才由这里补一行，否则轮询端会永远停在 queued/running。
            # 带令牌写：如果这一行已经属于另一次尝试（判死重开、或已收口），
            # 这段迟到异常不该去改别人的状态。
            row = await self.storage.render_jobs.get(session_id, artifact_id)
            if not row or row["status"] not in TERMINAL:
                await self.storage.render_jobs.fail(
                    session_id, artifact_id, f"渲染任务异常：{exc}"[:500],
                    (row or {}).get("attempt") or None)
            return None
        finally:
            self._tasks.pop(key, None)
            # 任务终于结束了，解除「被判死但仍占位」的封锁。
            self._reaped.discard(key)

    async def snapshot(self, session_id: str, artifact_id: str) -> dict[str, Any] | None:
        return await self.storage.render_view(session_id, artifact_id)

    async def wait(self, session_id: str, artifact_id: str,
                   grace_sec: float) -> dict[str, Any] | None:
        """内联等一小段时间：短片一次调用就拿到 done，长片超时后回一张进度视图。

        等的是库里的行而不是本地任务，所以渲染跑在别的实例上时这段等待只是白等一会儿，
        语义不受影响（回句柄、由调用方继续轮询）。
        """
        snap = await self.snapshot(session_id, artifact_id)
        if grace_sec <= 0 or not snap or snap["status"] in TERMINAL:
            return snap
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_sec
        while loop.time() < deadline:
            await asyncio.sleep(self._poll)
            snap = await self.snapshot(session_id, artifact_id)
            if not snap or snap["status"] in TERMINAL:
                return snap
        return snap

    def start_watchdog(self) -> None:
        """进程存活期间的停滞看门狗（lifespan 启动）。

        启动对账只收得到「崩溃遗留的行」；进程活得好好的、渲染体却再也不推进的那种行，
        没有这一步就永远停在 running——而「未达终态不许收尾」于是等价于「这条 run
        永不收尾」，前端进度条同样钉在最后一个数字上。开了这一条，等待方最迟 stall_sec
        之后拿到的是 failed，不是空等。
        """
        if self._stall_sec <= 0 or self._watchdog is not None:
            return
        self._watchdog = asyncio.create_task(self._watch_loop())

    async def stop_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    async def _watch_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(STALL_CHECK_SEC)
                await self.reap_stalled()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 看门狗不能因为一次读库失败就静默退场
                await asyncio.sleep(STALL_CHECK_SEC)

    async def reap_stalled(self) -> int:
        """收口一批停滞行，回收口条数（本地任务一并取消，槽位要还回来）。"""
        # 排队中的行先续期：它的 updated_at 冻在提交那一刻，是「活儿还没开始」而不是
        # 「活儿死了」——不区分就会把排在并发槽外的长队整条打死。
        for key in list(self._tasks):
            if key in self._active:
                continue
            sid, _, art = key.partition("|")
            await self.storage.render_jobs.touch(sid, art)
        rows = await self.storage.render_jobs.reap_stalled(
            self._stall_sec,
            reason=f"渲染停滞超过 {self._stall_sec:g} 秒无进度，已按失败收口"[:500])
        for r in rows:
            key = self.key(r["session_id"], r.get("artifact_id") or "_default")
            task = self._tasks.pop(key, None)
            if task is not None:
                # 取消只回收程等待与并发槽：MoviePy 那个线程 Python 杀不掉，
                # 它要是真跑完了会把行改写回 done（那是「片子的确存在」，不改判）。
                task.cancel()
                # 线程还活着，这个产物**仍然被占用**：不记下来的话，调用方一重试就会
                # 起第二份渲染写同一个 dst，两份互相覆盖。等任务真正结束再释放。
                self._reaped.add(key)
        return len(rows)

    async def cancel_all(self) -> None:
        """进程退出时收掉未跑完的后台任务（状态交给启动对账，不改库）。"""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()


def tool_view(snap: dict[str, Any] | None, *, node: str = "render_video") -> dict[str, Any]:
    """渲染视图 → 工具返回值，形状对齐 ``BaseNode.pack_outputs_to_client``。

    保持 ``node`` / ``artifact_id`` / ``output`` 三键不是审美问题：
    ``MediaCardHook`` 在工具结果里递归找 ``media_url``，``record_rendered_media`` 又从
    **顶层**取 ``artifact_id`` 与 ``output.video`` 对象键——换形状等于让成片卡片静默失效。
    """
    if snap is None:
        return {"node": node, "artifact_id": "", "output": None,
                "render": {"status": "none"},
                "hint": f"这个作用域还没有 {node} 任务：先调用 {node} 提交渲染"}
    render = {k: snap.get(k) for k in
              ("status", "stage", "percent", "seconds_since_update", "error")
              if snap.get(k) is not None}
    terminal = snap["status"] in TERMINAL
    out = {k: v for k, v in snap.items()
           if k not in ("status", "stage", "percent", "seconds_since_update",
                        "error", "session_id", "artifact_id")} or None
    view = {"node": node, "artifact_id": snap.get("artifact_id") or "",
            "output": out, "render": render}
    if not terminal:
        view["hint"] = (f"渲染仍在进行（stage={snap.get('stage') or 'queued'}，"
                        f"{snap.get('percent', 0)}%）。请调用 render_status"
                        f"（artifact_id={view['artifact_id']!r}）继续查询，"
                        f"直到 status=done 或 failed 再收尾")
    return view
