"""SubAgent Manager：对常驻子 Agent 的状态机与 WORK/IDLE 生命周期管理。

对应设计文档「Agent Team —— SubAgent Manager」，四个关键设计：
1. 子 Agent 有【独立且收敛为父集子集】的工具集（权限收敛）——由外部传入裁剪后的 registry。
2. 除 working 外还有 idle / shutdown 状态；idle 常驻内存（线程池式），避免反复创建进程。
3. 子 Agent 的 prompt 与工作状态持久化 —— 落在 PG ``subagents`` 表
   （spec §3.10，取代 ``subagents.json`` 整文件覆写），主键 ``(scope, name)``，
   scope = ``{user}:{conv}``；working 行带 ``lease_expires_at`` 租约，进程被 kill 后
   租约到期即由 ``reset_expired`` 归位 idle，不再谎报「还在干」。
4. idle 有两种唤醒方式：
   - 主动：经 Message Center 得知自己被分配了任务（读到收件箱消息）；
   - 被动：自驱扫描 Task Manager，发现有 pending、无 owner、无阻塞的可认领任务。

生命周期（把文档的 while True 落成可测的异步实现）：
    WORK：跑一次 AgentOnceRun（内层 ReAct，最多 max_iterations 轮）；LLM 异常 → shutdown。
    IDLE：置 idle；轮询 loop_times 次（每次 sleep poll_interval）：
          读到收件箱消息 或 有可认领任务 → 唤醒回 WORK；
          轮询耗尽仍无任务 → shutdown 并退出（释放资源）。

设计取舍：sleep / loop_times / poll_interval 可注入，测试里用零延时即可跑通整条状态机，
无需真实等待。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .agent import AgentOnceRun
from .identity import current_identity_or
from .message_center import MessageCenter
from .task_manager import TaskError, TaskManager
from .tool import ToolRegistry

WORKING = "working"
IDLE = "idle"
SHUTDOWN = "shutdown"

# 可注入的睡眠函数：默认 asyncio.sleep；测试传一个立即返回的协程函数。
Sleeper = Callable[[float], Awaitable[None]]

# IDLE 阶段默认等多久才认输（秒）。这是「子 Agent 是常驻线程池」这条语义的体现：
# 干完一轮活之后它留在内存等下一个任务，而不是马上销毁。
#
# 原先默认 loop_times=3 × poll_interval=0.05 = **总共 0.15 秒**：一轮 WORK 刚结束，
# 0.15 秒内没有新任务就转 SHUTDOWN，于是「常驻复用」名存实亡——每个任务都要重新登记
# 一个子 Agent，`subagents` 表里也看不到真正 idle 的实例。真机上表现为
# 「团队任务只跑得起第一条，后面的都得等重建」。
# 现在默认给 2 分钟窗口，并按 1 秒节奏轮询；要短窗口（测试/退场）显式传即可。
IDLE_WINDOW_SEC = 120.0
IDLE_POLL_SEC = 1.0


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - 真实运行才用到
    await asyncio.sleep(seconds)


class SubAgentManager:
    """管理一组常驻子 Agent 的状态与生命周期；状态与租约落在 ``subagents`` 表。"""

    def __init__(self, storage: Any, *, user_id: str = "default",
                 conversation_id: str = "default", lease_sec: float = 600) -> None:
        self._repo = storage.subagents
        self.default_user_id = user_id
        self.default_conversation_id = conversation_id
        self.lease_sec = lease_sec

    @property
    def scope(self) -> str:
        ident = current_identity_or(self.default_user_id, self.default_conversation_id)
        return f"{ident.user_id}:{ident.conversation_id}"

    async def register(self, name: str, prompt: str, status: str = IDLE) -> dict[str, Any]:
        """登记一个子 Agent（写入 prompt 与初始状态），供跨进程/重启恢复。"""
        return await self._repo.register(self.scope, name, prompt,
                                         lease_sec=self.lease_sec, status=status)

    async def set_status(self, name: str, status: str) -> None:
        await self._repo.set_status(self.scope, name, status, lease_sec=self.lease_sec)

    async def get_status(self, name: str) -> str | None:
        for r in await self._repo.list(self.scope):
            if r["name"] == name:
                return r["status"]
        return None

    async def list_agents(self) -> list[dict[str, Any]]:
        return await self._repo.list(self.scope)

    async def working(self) -> list[str]:
        return [r["name"] for r in await self.list_agents() if r["status"] == WORKING]

    async def idle(self) -> list[str]:
        return [r["name"] for r in await self.list_agents() if r["status"] == IDLE]

    async def reset_expired(self, scope: str | None = None) -> int:
        """启动对账：租约到期的 working 一律归位 idle（原来重启后状态永久失真）。

        scope 缺省=本会话；服务启动时传 None 扫全部作用域。
        """
        return await self._repo.reset_expired(scope)


class ManagedSubAgent:
    """一个常驻子 Agent：持有裁剪后的工具集与一次运行的 AgentOnceRun，跑 WORK/IDLE 状态机。"""

    def __init__(
        self,
        name: str,
        agent: AgentOnceRun,
        manager: SubAgentManager,
        *,
        message_center: MessageCenter | None = None,
        task_manager: TaskManager | None = None,
        session: Any | None = None,
        loop_times: int | None = None,
        poll_interval: float = IDLE_POLL_SEC,
        sleep: Sleeper = _default_sleep,
    ) -> None:
        self.name = name
        self.agent = agent
        self.manager = manager
        self.message_center = message_center
        self.task_manager = task_manager
        self.session = session
        # 轮询窗口：显式给了 loop_times 就用它（测试/短窗口），
        # 否则按 IDLE_WINDOW_SEC / poll_interval 推出「常驻等待」的轮数。
        self.poll_interval = poll_interval
        self.loop_times = (int(loop_times) if loop_times is not None
                           else max(1, int(IDLE_WINDOW_SEC / max(poll_interval, 1e-6))))
        self._sleep = sleep
        self.replies: list[str] = []  # 记录每轮 WORK 的最终答复，便于观测/测试

    async def run(self, first_prompt: str) -> dict[str, Any]:
        """驱动完整生命周期直到 shutdown；返回 {final_status, worked_rounds, replies}。

        WORK 一轮 = 一次 AgentOnceRun.run；答复文本作为下一轮唤醒的上下文来源。
        """
        prompt = first_prompt
        rounds = 0
        while True:
            # ---------- WORK ----------
            await self.manager.set_status(self.name, WORKING)
            rounds += 1
            try:
                reply = await self.agent.run(self.session, prompt)
            except Exception:
                # LLM 调用失败：按文档置 shutdown 并退出。
                await self.manager.set_status(self.name, SHUTDOWN)
                return {"final_status": SHUTDOWN, "worked_rounds": rounds, "replies": self.replies}
            self.replies.append(reply or "")

            # ---------- IDLE ----------
            await self.manager.set_status(self.name, IDLE)
            wake = await self._wait_for_work()
            if wake is None:
                # 轮询超时且无任务：释放资源。
                await self.manager.set_status(self.name, SHUTDOWN)
                return {"final_status": SHUTDOWN, "worked_rounds": rounds, "replies": self.replies}
            prompt = wake  # 唤醒后带着新指令回到 WORK

    async def _wait_for_work(self) -> str | None:
        """IDLE 轮询：读到收件箱消息（主动）或有可认领任务（被动）→ 返回唤醒 prompt；否则 None。

        **抢任务失败不能把协程带走。** 原先这里直接 ``await claim(...)``：
        ``claim`` 在「已被他人抢先认领」时抛 ``TaskError``，本函数不接，
        异常一路冒到 ``run()``，协程死掉——而 ``subagents`` 表里那一行还停在 idle，
        看起来「这个子 Agent 就绪」其实已经没了。多子 Agent 并发抢同一批任务时命中率很高。
        现在抢不到就换下一个候选，全抢不到只是这一轮没醒，不是死。
        """
        for _ in range(self.loop_times):
            await self._sleep(self.poll_interval)

            # 主动：Message Center 得知自己被分配了任务。
            if self.message_center is not None:
                inbox = await self.message_center.read_inbox(self.name)
                if inbox:
                    return "\n".join(str(m.get("content", "")) for m in inbox)

            # 被动：自驱扫描待认领任务（pending、无 owner、无 blockedBy）。
            #
            # 每次 IDLE 轮询最多认领**一个**任务（与「干完一轮活再回来等」的语义一致）：
            # 抢到就走、抢不到就继续等下一拍。原先这里不接 ``claim`` 抛的
            # ``TaskError``（「已被他人抢先认领」）——异常一路冒到 ``run()``，
            # 协程直接死掉，而 ``subagents`` 表里那行还停在 idle，看起来「就绪」其实没了。
            # 多个子 Agent 并发抢同一批任务时命中率很高，所以这一条必须接住。
            if self.task_manager is not None:
                ready = await self.task_manager.ready()
                if ready:
                    task = ready[0]
                    try:
                        await self.task_manager.claim(task["id"], self.name)
                    except TaskError:
                        continue          # 被别人抢先：这一拍不算醒，接着轮询
                    except Exception:  # noqa: BLE001 - 存储抖动同样不该杀死子 Agent
                        continue
                    return (f"认领任务 #{task['id']}：{task['name']}。"
                            f"{task.get('description', '')}")
        return None


def build_subagent_registry(
    parent_registry: ToolRegistry, allow: list[str]
) -> ToolRegistry:
    """权限收敛：从父工具集里【只挑 allow 白名单】构造子 Agent 的独立工具集。

    对应文档“子 Agent 工具集是主 Agent 的子集”。未在父集中出现的名字会被忽略。
    """
    sub = ToolRegistry()
    for tool_name in allow:
        tool = parent_registry.get(tool_name)
        if tool is not None:
            sub.register(tool)
    return sub
