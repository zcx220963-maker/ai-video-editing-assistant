# -*- coding: utf-8 -*-
"""R7：异步 Spawn——默认立刻返回 task_id，wait=True 才同步等结论。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import agent_framework.subagent as sub  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class FakeCenter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, sender, to, content, *a, **kw):
        self.sent.append((sender, to, content))
        return "ok"


class SlowRunner:
    """跑 0.3s 才给结论；记录调用次数。"""

    def __init__(self) -> None:
        self.calls = 0
        self.message_center = FakeCenter()

    async def run(self, prompt, parent_session_id=""):
        self.calls += 1
        await asyncio.sleep(0.3)
        return f"结论：{prompt[:10]}"


async def main() -> None:
    cls = getattr(sub, "SpawnTool", None) or getattr(sub, "SpawnSubagentTool", None)
    check(cls is not None, "找到 SpawnTool 类")
    if cls is None:
        raise SystemExit(1)

    # 构造：只需 _runner
    tool = cls(SlowRunner())
    check("wait" in tool.parameters["properties"], "schema 暴露了 wait")
    check("默认" in tool.parameters["properties"]["wait"]["description"]
          and "false=立刻返回" in tool.parameters["properties"]["wait"]["description"],
          "schema 说明默认异步（省略 wait 即立刻返回）")
    check("默认异步" in tool.description, "工具描述与默认值一致（不再自称同步等待）")

    # ---- 默认（不传 wait）：立刻返回 ----
    t0 = time.monotonic()
    out = await tool.execute(task="写一段分镜")
    cost = time.monotonic() - t0
    check(cost < 0.25, f"默认异步：立刻返回（{cost:.3f}s）")
    check("task_id=" in out, f"回执带 task_id（{out[:40]}）")
    check("已派发" in out, "回执说明已派发")

    # ---- 后台真的跑了，结论回投收件箱 ----
    await asyncio.sleep(0.45)
    check(tool._runner.calls == 1, "后台执行了一次")
    sent = tool._runner.message_center.sent
    check(len(sent) == 1 and sent[0][1] == "main_agent"
          and "[子 Agent 完成]" in sent[0][2] and "结论：" in sent[0][2],
          f"异步结论回投 main_agent 收件箱：{sent}")

    # ---- wait=True：同步等结论 ----
    t0 = time.monotonic()
    out2 = await tool.execute(task="写第二段", wait=True)
    check(time.monotonic() - t0 >= 0.25, "wait=True 会等")
    check("[子 Agent 结论]" in out2, f"同步分支回结论（{out2[:30]}）")
    check(tool._runner.calls == 2, "同步分支也调了 runner")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
