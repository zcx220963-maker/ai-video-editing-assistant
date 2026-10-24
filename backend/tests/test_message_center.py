"""Message Center 验证（不联网）：PG 收件箱、标记已读、作用域与 A2A 工具接线。

对应 Context 构建流程图右侧的协作通道：消息写在 ``inbox_messages`` 表，
作用域 = ``(user_id, conv_id, agent)``（spec §3.8），取代原来的
``{user}/{conv}/{agent}.jsonl`` 三级目录。文档的「消费即删」落成「打 consumed_at」：
一条消息仍只被消费一次，但行留在表里可重放、可审计。

运行：  python tests/test_message_center.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.identity import use_identity
from agent_framework.message_center import MessageCenter, register_message_tools
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
    with tempfile.TemporaryDirectory() as tmp:
        storage = build_storage("memory")
        await storage.start()
        mc = MessageCenter(storage, user_id="u1", conversation_id="c1")

        # ---- send：一行消息进收件人 Box，路由列 + jsonb 正文 ----
        ack = await mc.send("main_agent", "sub_agent_a", "请完成 id=3 的 Task")
        check(ack == "Sent message to sub_agent_a", f"send 返回确认串：{ack}")
        rows = await storage.db.select("inbox_messages", order_by=["id"])
        check(len(rows) == 1, "一条消息对应表里一行")
        row = rows[0]
        check((row["user_id"], row["conv_id"], row["agent"]) == ("u1", "c1", "sub_agent_a"),
              f"作用域三元组钉在行上：{(row['user_id'], row['conv_id'], row['agent'])}")
        check(row["type"] == "message" and row["sender"] == "main_agent",
              "type/sender 是可直接过滤的路由列")
        body = row["content"]
        check(body["from"] == "main_agent" and body["content"] == "请完成 id=3 的 Task",
              "正文含 from/content")
        check(isinstance(body["timestamp"], float) and body["timestamp"] > 0, "含浮点时间戳")
        check(row["consumed_at"] is None, "未消费的行 consumed_at 为空")

        # ---- 跨作用域隔离：另一用户/会话的同名 Box 看不到消息 ----
        mc_other = MessageCenter(storage, user_id="u2", conversation_id="c9")
        check(await mc_other.read_inbox("sub_agent_a") == [], "不同用户/会话的消息空间相互隔离")
        # 身份 contextvar 优先于构造缺省：同一次 run 内的工具不必自带会话参数
        with use_identity("u3", "c3"):
            check(await mc.read_inbox("sub_agent_a") == [],
                  "run 内作用域由执行身份决定，不受实例缺省值影响")
            check(await mc.agents() == [], "新作用域里没有别人的收件箱")

        # ---- 多条追加 + 自定义类型 ----
        await mc.send("main_agent", "sub_agent_a", "补充：注意时长", msg_type="note")
        rows = await storage.db.select("inbox_messages", order_by=["id"])
        check(len(rows) == 2 and rows[1]["type"] == "note", "第二条消息是新行，自定义 msg_type 生效")

        # ---- read_inbox：读出全部未消费 + 标记已读（不再物理删除）----
        msgs = await mc.read_inbox("sub_agent_a")
        check(len(msgs) == 2 and msgs[0]["content"].startswith("请完成"), "read 返回整批消息")
        after = await storage.db.select("inbox_messages", order_by=["id"])
        check(len(after) == 2 and all(r["consumed_at"] is not None for r in after),
              "读取后行仍在表里，只是打上了 consumed_at（可重放/审计）")
        check(await mc.read_inbox("sub_agent_a") == [], "再次读取为空（不会重复消费）")

        # ---- 未知收件箱：空列表不报错 ----
        check(await mc.read_inbox("nobody") == [], "无收件箱返回空列表")
        check(await mc.peek("nobody") == [], "peek 无收件箱返回空列表")

        # ---- peek 不标记 ----
        await mc.send("x", "watcher", "hi")
        check(len(await mc.peek("watcher")) == 1 and len(await mc.peek("watcher")) == 1,
              "peek 读取但不标记已读")
        check("watcher" in await mc.agents(), "agents() 列出有收件箱的成员")

        # ---- purge：只清已读 ----
        check(await storage.inbox.purge_consumed("u1", "c1") == 2, "purge_consumed 删掉已读的 2 条")
        check(len(await storage.db.select("inbox_messages")) == 1, "未消费的 watcher 消息留着")

        # ---- 工具接线：LLM 经 registry 收发消息 ----
        reg = ToolRegistry()
        register_message_tools(reg, mc, "sub_agent_b")
        check({"send_message", "read_inbox"} <= set(reg.tool_names), "注册了 send/read 两个工具")
        r = await reg.execute("send_message", {"to": "sub_agent_b", "content": "任务已就绪"})
        check(r == "Sent message to sub_agent_b", f"工具 send 成功：{r}")
        # 以 sub_agent_b 身份读，应拿到刚才那条，且发件人是 sub_agent_b（注册身份）。
        got = json.loads(await reg.execute("read_inbox", {}))
        check(len(got) == 1 and got[0]["content"] == "任务已就绪", "工具 read_inbox 拿到新消息")
        check(json.loads(await reg.execute("read_inbox", {})) == [], "工具 read 后再次为空")
        check(not reg.get("read_inbox").concurrency_safe, "read_inbox 有副作用不并发")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
