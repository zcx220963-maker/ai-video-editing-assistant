"""多 Agent 协作一致性（任务幂等 + 锁 + 消息恰好一次消费）。

对应「Agent Team 生产语义」四条硬保证：
  ① 原子认领：两个协程抢同一条任务，只有一个成功；
  ② 交付纪律：pending 不能跳过认领；只有认领者本人能 complete；
  ③ 交付幂等：同 owner 重复 complete 原样返回，下游不二次解锁；
  ④ 消息恰好一次：收件箱消费是原子领取（并发读者各拿各的），投递支持 dedup_key 幂等。

运行：  python tests/test_team_consistency.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.message_center import MessageCenter
from agent_framework.storage import build_storage
from agent_framework.task_manager import CLAIMED, COMPLETED, PENDING, TaskError, TaskManager

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def expect_task_error(coro, label: str, needle: str = "") -> None:
    try:
        await coro
        check(False, f"{label}（竟未抛错）")
    except TaskError as e:
        check(needle in str(e) if needle else True, f"{label}（{e}）")
    except Exception as e:  # noqa: BLE001
        check(False, f"{label}（抛了非 TaskError：{type(e).__name__}: {e}）")


async def part1_claim_lock() -> None:
    print("\n[1] 原子认领：并发抢同一条任务只有一个成功")
    tm = TaskManager(build_storage("memory"))
    t = await tm.create("切镜头", "split_shots")

    results = await asyncio.gather(
        tm.claim(t["id"], "sub_a"),
        tm.claim(t["id"], "sub_b"),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    check(len(winners) == 1 and len(losers) == 1,
          f"恰好一个认领成功（赢家 {len(winners)} / 输家 {len(losers)}）")
    check(all("已被" in str(l) for l in losers),
          f"输家拿到的是「已被抢先认领」：{losers}")
    after = await tm.get(t["id"])
    check(after["status"] == CLAIMED and after["owner"] == winners[0]["owner"],
          f"板上归属与赢家一致（{after['owner']}）")


async def part2_complete_discipline() -> None:
    print("\n[2] 交付纪律与幂等：只有认领者能 complete，重复交付原样返回")
    tm = TaskManager(build_storage("memory"))
    upstream = await tm.create("粗剪", "speech_rough_cut")
    downstream = await tm.create("配音", "generate_voiceover",
                                 blocked_by=[upstream["id"]])

    await expect_task_error(tm.complete(upstream["id"], "sub_a"),
                            "pending 直接交付被打回", "尚未认领")

    await tm.claim(upstream["id"], "sub_a")
    await expect_task_error(tm.complete(upstream["id"], "sub_b"),
                            "他人交付被打回", "只有认领者可交付")

    done = await tm.complete(upstream["id"], "sub_a")
    check(done["status"] == COMPLETED, "认领者本人交付成功")
    check(done["_unlocked"] == [downstream["id"]],
          f"交付即解锁下游：{done['_unlocked']}")

    again = await tm.complete(upstream["id"], "sub_a")
    check(again["status"] == COMPLETED and again["_unlocked"] == [],
          "重复交付幂等：原样返回且不再二次解锁下游")

    await expect_task_error(tm.complete(upstream["id"], "sub_b"),
                            "他人对已完成任务的交付仍被打回", "无法由 sub_b 交付")

    ready = await tm.ready()
    check([t["id"] for t in ready] == [downstream["id"]],
          "下游进入可认领清单")


async def part3_inbox_dedup() -> None:
    print("\n[3] 投递幂等：同 dedup_key 的未消费消息只投一次")
    mc = MessageCenter(build_storage("memory"))
    r1 = await mc.send("main_agent", "sub_a", "做任务 #3", dedup_key="assign-3")
    r2 = await mc.send("main_agent", "sub_a", "做任务 #3", dedup_key="assign-3")
    check("未重复投递" in r2, f"第二次发送返回幂等说明：{r2}")

    msgs = await mc.read_inbox("sub_a")
    check(len(msgs) == 1, f"收件箱里只有一条（{len(msgs)}）")
    check(msgs[0].get("_dedup") == "assign-3" and isinstance(msgs[0].get("_id"), int),
          f"消息带 _dedup 与 _id：{msgs[0].get('_dedup')}, id={msgs[0].get('_id')}")
    check(await mc.read_inbox("sub_a") == [], "消费后清空")

    r3 = await mc.send("main_agent", "sub_a", "做任务 #3", dedup_key="assign-3")
    check("未重复投递" not in r3, "消费过后同键消息可以再投（幂等只挡未消费的）")


async def part4_inbox_exactly_once() -> None:
    print("\n[4] 消费恰好一次：并发读者各拿各的，不重不漏")
    mc = MessageCenter(build_storage("memory"))
    for i in range(6):
        await mc.send("main_agent", "worker", f"任务-{i}")

    got_a, got_b = await asyncio.gather(
        mc.read_inbox("worker"), mc.read_inbox("worker"))
    ids_a = [m["_id"] for m in got_a]
    ids_b = [m["_id"] for m in got_b]
    overlap = set(ids_a) & set(ids_b)
    check(not overlap, f"两个并发读者零重叠（重叠 {overlap}）")
    check(len(got_a) + len(got_b) == 6,
          f"六条消息恰好各被消费一次（{len(got_a)}+{len(got_b)}）")
    check(await mc.peek("worker") == [], "板上不再有未读")


async def part5_tool_default_owner() -> None:
    print("\n[5] 工具层回归：complete_task 缺省 owner=main_agent，认领者交付才生效")
    from agent_framework.tool import ToolRegistry
    from agent_framework.team_tools import register_team_tools

    storage = build_storage("memory")
    reg = ToolRegistry()
    register_team_tools(reg, llm=None, storage=storage)
    tm = TaskManager(storage)

    t = await tm.create("切片段")
    complete = reg.get("complete_task")
    check(complete is not None, "complete_task 工具已注册")

    # FunctionTool 不吞异常（由 Registry 层统一转 ToolError），这里直接验异常语义
    await expect_task_error(complete.execute(task_id=t["id"]),
                            "未认领先交付被打回", "尚未认领")

    await tm.claim(t["id"], "sub_x")
    await expect_task_error(complete.execute(task_id=t["id"]),
                            "缺省 main_agent 交他人的任务被打回", "只有认领者可交付")

    out = await complete.execute(task_id=t["id"], owner="sub_x")
    check('"status": "completed"' in out or '"status":"completed"' in out
          or '"status": "completed"' in out.replace(" ", "") or '"completed"' in out,
          f"认领者带 owner 交付成功：{out.strip()[:60]}")


async def main() -> None:
    await part1_claim_lock()
    await part2_complete_discipline()
    await part3_inbox_dedup()
    await part4_inbox_exactly_once()
    await part5_tool_default_owner()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
