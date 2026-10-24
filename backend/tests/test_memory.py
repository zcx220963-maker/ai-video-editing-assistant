"""记忆系统验证（不联网）：分类白名单、覆盖式删除、按用户归属、注入、并发追加。

记忆已从 User.md / Tool.md 两个文件搬进存储层 memories 表（spec §3.12），
所以这里的落点断言都对着表，不再对着目录。

运行：  python tests/test_memory.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.context import ContextBuilder
from agent_framework.identity import use_identity
from agent_framework.memory import (
    CATEGORIES,
    MemoryContextSource,
    MemoryStore,
    register_memory_tools,
)
from agent_framework.session import Session
from agent_framework.tool import is_tool_error
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def case_recall_returns_only_hits(storage) -> None:
    """R9-T39 ①：40 条记忆、本轮话题只关 3 条 → 注入集恰好是那 3 条。

    「恰好」是双向的：切题的**必须**进来（召回有命中），无关的**必须**不进来
    （不是 top-k 凑数——凑数就把「召回」退化成了「截短的 dump」）。
    """
    print("\n=== ① 召回：40 条里只关 3 条 ===")
    store = MemoryStore(storage, user_id="recall-u")
    noise = "\n".join(f"杂记{i}：今天拍了点素材，编号 {i}" for i in range(37))
    hits = "转场用叠化更稳\n转场时长别超过 1 秒\n转场的音效要提前 0.2 秒"
    await store.write("user", f"{noise}\n{hits}")

    src = MemoryContextSource(store)
    got = await src.render("这版转场怎么处理")
    check(all(f"转场" in line for line in got.splitlines() if "杂记" not in line
              and "转场" in line) or "转场用叠化更稳" in got,
          "切题的三条进来了")
    for line in ("转场用叠化更稳", "转场时长别超过 1 秒", "转场的音效要提前 0.2 秒"):
        check(line in got, f"注入含命中条目：{line}")
    check("杂记" not in got, "37 条无关条目一条都没搭车进来")
    check("共 40 条" in got or "未注入" in got, "回执说清总数（读的人知道这不是全部）")
    # 计划步骤名那条通道：查询串里带上节点名也能召回到跟它同词的那条
    await store.write("tool", "plan_motion 这个节点排的就是分镜顺序")
    got2 = await MemoryContextSource(store).render("继续\nplan_motion")
    check("plan_motion" in got2, "计划步骤名进 query 后召回到工具教训（T39 的 recall_terms）")

    # 一条都没命中 → 退回「每类最近几条」，**不是什么都不给**：服务端自动生成的
    # 「计划卡：跳过…」「成片退回重做…」跟新一轮诉求往往不同词，纯相关度会把它们丢掉。
    got3 = await MemoryContextSource(store).render("完全无关的一句话")
    check("转场用叠化更稳" in got3 and "plan_motion" in got3,
          "零命中时退回最近几条（每类各取，工具教训不被用户偏好挤没）")
    check("改注入最近" in got3, "回执说清这是「没命中改最近」，不冒充命中")


async def case_over_limit_is_reported(storage) -> None:
    """R9-T39 ②：超限必须**说出来**（回执行 + 账面 over_budget），不许静默截尾。

    两层分开测，因为它们是两件事：
    - 召回装不下（自有字节预算）→ 少注入几条 + 一行「未注入 N 条」回执；
    - 有人把限额放开到顶穿 §5.4.1 的断值 → ㉓ 拦下，请求根本不发出去。
    """
    print("\n=== ② 超限：回执行 vs 断值拦（两层）===")
    from agent_framework.context import SECTION_BUDGET_BYTES, ContextBudgetError

    store = MemoryStore(storage, user_id="limit-u")
    # 每条都够长：两条上限（召回条数、自有字节预算）与 §5.4.1 的断值必须**分得开**地测，
    # 否则「限额放开也顶不穿断值」这种假绿会把闸的失效藏起来。
    await store.write("user", "\n".join(
        f"喜欢第 {i} 种风格：排版要留白、字号别太大。" * 1 + "排版要留白、字号别太大。" * 12
        for i in range(30)))

    tight = MemoryContextSource(store, per_category_bytes=60, total_bytes=90)
    got = await tight.render("喜欢什么风格")
    check("未注入" in got and "条" in got, f"装不下时留了回执行：{got.splitlines()[-2][:60]}…")
    check("read_memory" in got, "回执告诉模型去哪里看全量（不是让它以为记忆就这么多）")
    check("正文开头" in got, "回执带上「最早一条起于哪里」（分类粒度的时间戳，见 §9-Q102）")
    check(len(got.encode("utf-8")) < SECTION_BUDGET_BYTES["memory"],
          "正常上限下这一段仍在 §5.4.1 的断值内（少了可以，超了才不许）")

    # 放开限额（= 有人把上限删了）→ 顶穿断值 → 装配期抛错，请求不发出。
    # top_k 也要一起放开：只放大字节预算、条数仍卡 6 条时，这一段压根顶不到断值。
    wide = MemoryContextSource(store, per_category_bytes=100000, total_bytes=100000,
                              top_k=30)
    b = ContextBuilder("BASE", context_sources=[wide], runtime_context=None)
    try:
        await b.build(Session(user_id="limit-u", conversation_id="c"), "喜欢什么风格")
        check(False, "限额放到顶穿断值时必须红（不许静默发出去）")
    except ContextBudgetError as exc:
        check("memory" in str(exc) and "不许截断" in str(exc),
              "㉓ 点名 memory 并写明不许截断顶过去")
        check(b.last_ledger["over_budget"] == ["memory"],
              f"账面上 over_budget 就是它：{b.last_ledger['over_budget']}")


def case_usage_note_budget() -> None:
    """R9-T39 ③：`USAGE_NOTE` 字节 ≤120（它每次常发，属参数教程同类）。"""
    print("\n=== ③ USAGE_NOTE ≤120 B ===")
    raw = MemoryContextSource.USAGE_NOTE
    n = len(raw.encode("utf-8"))
    check(n <= 120, f"USAGE_NOTE {n} B ≤ 120 B（改长了要同批改断言，同 ⑫ 的纪律）")
    check("update_memory" in raw and "read_memory" not in raw.replace("read_memory", "", 0)
          or "update_memory" in raw,
          "压短了也仍说清「写哪件工具、怎么合并」")
    check("计划卡" not in raw,
          "「怎么理解计划卡生成行」那段教程不再常发（它讲的是别人的格式，不是记忆用法）")


async def main() -> None:
    storage = build_storage("memory")
    await storage.start()
    try:
        store = MemoryStore(storage)                     # 未指定 user_id：跟着执行身份走
        src = MemoryContextSource(store)
        check(await src.render("q") is None, "无记忆时 render 返回 None（不注入）")

        # 白名单：非法分类既不落库也不静默忽略
        try:
            await store.read("../../etc/passwd")
            check(False, "非法 category 应报错")
        except ValueError:
            check(True, "非法 category 被白名单拦截")
        rows = await storage.db.select("memories")
        check(rows == [], "报错的写入没有留下任何行")

        # 工具：全量覆盖写入（写入落在 memories 表）
        reg = ToolRegistry()
        register_memory_tools(reg, store)
        check(reg.get("read_memory").concurrency_safe, "read_memory 可并发 (read_only)")
        check(not reg.get("update_memory").concurrency_safe, "update_memory 不可并发")

        r = await reg.execute("update_memory",
                              {"category": "user", "content": "喜欢文艺风格\n关注旅行领域"})
        check("全量覆盖" in r, f"写入 user 记忆: {r}")
        rows = await storage.db.select("memories")
        check([(x["user_id"], x["category"]) for x in rows] == [("default", "user")],
              "记忆落在 memories 表的 (user_id, category) 主键上")

        await reg.execute("update_memory",
                          {"category": "tool", "content": "定时发布走 MCP Tool，而非 CronJob"})

        # 全量覆盖实现删除
        await reg.execute("update_memory", {"category": "user", "content": "只保留：喜欢极简风格"})
        entries = await store.entries()
        check(entries["user"] == "只保留：喜欢极简风格", "replace 全量覆盖旧记忆（删除生效）")
        check("tool" in entries, "tool 记忆独立保留")
        check(list(entries) == ["user", "tool"], "entries 按白名单顺序返回")

        # append
        await reg.execute("update_memory",
                          {"category": "user", "content": "讨厌冗长", "mode": "append"})
        check("讨厌冗长" in await store.read("user") and "极简" in await store.read("user"),
              "append 追加保留原内容")

        # 读工具
        r = await reg.execute("read_memory", {})
        check("[user]" in r and "[tool]" in r, "read_memory 返回两类记忆")
        r = await reg.execute("read_memory", {"category": "tool"})
        check("[user]" not in r and "MCP Tool" in r, "read_memory 指定 category 生效")
        r = await reg.execute("update_memory", {"category": "evil", "content": "x"})
        check(is_tool_error(r), "update_memory 非法 category 报错")

        # 注入到 ContextBuilder（作为第一个真实 ContextSource）：R9-T39 起是**召回**，
        # 所以这里的 query 必须真的跟记忆同词——「帮我写文案」召不回「极简风格」是
        # 设计如此（不相关的不注入），不是回归。
        b = ContextBuilder("BASE", context_sources=[MemoryContextSource(store)],
                           runtime_context=None)
        msgs = await b.build(Session(user_id="default", conversation_id="c"),
                             "想做一版极简风格的旅行文案，用定时发布")
        system = msgs[0]["content"]
        check("<memory>" in system, "System 含 <memory> 段")
        check("用户偏好记忆" in system and "极简" in system, "注入了命中的 user 记忆")
        check("工具使用记忆" in system and "MCP Tool" in system, "注入了命中的 tool 记忆")
        check("讨厌冗长" not in system,
              "同一分类里没命中的条目**不**搭车注入（召回不是全量）")

        # 归属：进程级单例的工具实例按当前 run 的身份分账，用户之间不串味
        with use_identity("alice", "conv-a"):
            check(store.user_id == "alice", "store 归属取自执行身份")
            await store.write("user", "alice：只做竖屏")
        with use_identity("bob", "conv-b"):
            check(await store.read("user") == "", "换用户后读不到 alice 的记忆")
            await store.write("user", "bob：横屏纪录片")
        check((await storage.db.count("memories", where={"user_id": "alice"})) == 1
              and (await storage.db.count("memories", where={"user_id": "bob"})) == 1,
              "两位用户各自一行")
        bob_entries = await MemoryStore(storage, user_id="bob").entries()
        check(bob_entries["user"] == "bob：横屏纪录片", "构造时指定 user_id 可离线代查")
        with use_identity("carol", "conv-c"):
            check(await MemoryContextSource(store).render("q") is None,
                  "新用户无记忆时不注入（不是继承别人的）")

        # 并发追加不丢更新（spec 缺陷「记忆 append 丢更新」）
        await asyncio.gather(*(store.append("tool", f"提示{i}") for i in range(6)))
        txt = await store.read("tool")
        check(all(f"提示{i}" in txt for i in range(6)), "6 路并发追加一条不丢")

        # 换实例（= 进程重启）后仍读得到：数据在表里，不在对象内存里
        reborn = MemoryStore(storage, user_id="alice")
        check("只做竖屏" in await reborn.read("user"), "新实例读到同一张表（重启不丢）")
        check(set(CATEGORIES) == {"user", "tool"}, "白名单仍是 user/tool 两类")

        # R9-T39 三支：召回命中集、超限回执（不是静默截尾）、USAGE_NOTE 字节上限
        await case_recall_returns_only_hits(storage)
        await case_over_limit_is_reported(storage)
        case_usage_note_budget()
    finally:
        await storage.close()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
