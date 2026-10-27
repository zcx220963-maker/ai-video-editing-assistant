# -*- coding: utf-8 -*-
"""R8：Bootstrap Files 每轮进 system；目录不存在等于不启用；超预算截断。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.bootstrap import MAX_BYTES, load_bootstrap  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.session import SessionManager  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def main() -> None:
    # ---- 目录不存在 = 不启用（不用开关）----
    check(load_bootstrap(None) == "", "None 返回空串")
    check(load_bootstrap("/no/such/dir") == "", "不存在的目录返回空串")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "b_second.md").write_text("第二段", encoding="utf-8")
        (root / "a_first.md").write_text("第一段", encoding="utf-8")
        (root / "empty.md").write_text("   ", encoding="utf-8")
        (root / "note.txt").write_text("不是 md", encoding="utf-8")

        text = load_bootstrap(root)
        check("<bootstrap file=\"a_first.md\">" in text, "按文件名排序，a 在前")
        check(text.index("第一段") < text.index("第二段"), "顺序正确")
        check("不是 md" not in text, "只读 *.md")
        check("empty.md" not in text, "空文件跳过")

        # ---- 超预算截断而不是全塞 ----
        (root / "big.md").write_text("x" * (MAX_BYTES + 100), encoding="utf-8")
        capped = load_bootstrap(root)
        check(len(capped.encode("utf-8")) <= MAX_BYTES + 200, "超预算被截断")

        # ---- ContextBuilder 接得上：bootstrap_dir 非空就多一段 ----
        b = ContextBuilder("SYS")
        plain = asyncio.run(b.build(_session(), "hi"))
        b.bootstrap_dir = str(root)
        with_boot = asyncio.run(b.build(_session(), "hi"))
        check(len(with_boot) == len(plain), "消息条数不变（bootstrap 并入 system）")
        check(any("<bootstrap" in str(m.get("content")) for m in with_boot),
              "bootstrap 段进了上下文")

    runtime_once()
    asyncio.run(history_window())

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


def runtime_once() -> None:
    """R8-b：runtime 段每轮只注入一次（不是每段/每条消息各来一份）。"""
    calls: list[int] = []

    def rt() -> str:
        calls.append(1)
        return "机器：8 核 / 16 GB"

    b = ContextBuilder("SYS", runtime_context=rt)
    first = asyncio.run(b.build(_session(), "hi"))
    second = asyncio.run(b.build(_session(), "再说一句"))
    check(len(calls) == 2, f"每轮装配调用一次 runtime_context（实际 {len(calls)}）")
    check(first[0]["content"].count("<runtime>") == 1, "system 里只有一段 <runtime>")
    check(first[0]["content"].count("机器：8 核 / 16 GB") == 1, "runtime 内容只出现一次")
    check(sum(1 for m in first if m.get("role") == "system") == 1, "只有一条 system 消息")
    check(second[0]["content"].count("<runtime>") == 1, "第二轮同样只注入一次")

    empty = ContextBuilder("SYS", runtime_context=lambda: "")
    no_rt = asyncio.run(empty.build(_session(), "hi"))
    check("<runtime>" not in no_rt[0]["content"], "空 runtime 不落空段")


async def history_window() -> None:
    """R8-c：默认只装尾部 40 条，并如实回报丢了多少条（不许静默）。"""
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=pathlib.Path(td) / "cache",
                                workspace_root=pathlib.Path(td) / "ws")
        await storage.start()
        await storage.conversations.ensure("u1", "c-long")
        for i in range(45):
            role = "user" if i % 2 == 0 else "assistant"
            await storage.messages.append("u1", "c-long", role, f"m{i + 1}")

        sess = await SessionManager(storage).get_or_create("u1", "c-long")
        check(len(sess.messages) == 40, f"只装尾部 40 条（实际 {len(sess.messages)}）")
        check(sess.history_window == 40, f"窗口回执 40（实际 {sess.history_window}）")
        check(sess.history_dropped == 5, f"丢掉 5 条（实际 {sess.history_dropped}）")
        check(sess.messages and sess.messages[0].get("content") == "m6",
              f"保下来的是 seq 6 起的尾部（实际首条 {sess.messages[0].get('content')!r}）")

        b = ContextBuilder("SYS", runtime_context=lambda: "")
        msgs = await b.build(sess, "继续")
        check(len(msgs) == 42, f"system + 40 条历史 + 当前输入（实际 {len(msgs)}）")
        led = (b.last_ledger or {}).get("history") or {}
        check(led.get("window") == 40 and led.get("dropped") == 5,
              f"账面记了窗口与丢弃数（实际 {led.get('window')} / {led.get('dropped')}）")

        # window=0 = 不设窗口：整段装回（旧口径的退路还在）
        full = await SessionManager(storage, history_window=0).get_or_create("u1", "c-long")
        check(len(full.messages) == 45 and full.history_dropped == 0,
              f"window=0 全量装回 45 条（实际 {len(full.messages)}）")


def _session():
    from agent_framework.session import Session
    return Session(user_id="u1", conversation_id="c1")


if __name__ == "__main__":
    main()
