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

        # 注入到 ContextBuilder（作为第一个真实 ContextSource）
        b = ContextBuilder("BASE", context_sources=[MemoryContextSource(store)],
                           runtime_context=None)
        msgs = await b.build(Session(user_id="default", conversation_id="c"), "帮我写文案")
        system = msgs[0]["content"]
        check("<memory>" in system, "System 含 <memory> 段")
        check("用户偏好记忆" in system and "极简" in system, "注入了 user 记忆内容")
        check("工具使用记忆" in system and "MCP Tool" in system, "注入了 tool 记忆内容")

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
    finally:
        await storage.close()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
