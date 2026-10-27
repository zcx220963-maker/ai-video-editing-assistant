# -*- coding: utf-8 -*-
"""R10：逐调用 checkpoint——payload 是对象时 rebuild 跳过；completed_calls 按 call_id 汇总。"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.checkpoint import CheckpointManager, rebuild  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class StubRepo:
    """只实现 load_entries / append_entry。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def load_entries(self, run_id, upto_seq=None):
        return [e for e in self.entries if e.get("run_id") == run_id]

    async def append_entry(self, run_id, *, kind, payload, iteration, parent_seq):
        seq = len(self.entries) + 1
        self.entries.append({"run_id": run_id, "seq": seq, "kind": kind, "payload": payload})
        return {"seq": seq}


def _manager(repo: StubRepo) -> CheckpointManager:
    mgr = CheckpointManager.__new__(CheckpointManager)
    mgr._repo = repo
    return mgr


async def main() -> None:
    repo = StubRepo()
    mgr = _manager(repo)

    # ---- 写 tool_call / tool_result ----
    await mgr.record_tool_call("r1", call_id="c1", name="load_media", arguments={"a": 1})
    await mgr.record_tool_result("r1", call_id="c1", name="load_media", status="ok")
    kinds = [e["kind"] for e in repo.entries]
    check(kinds == ["tool_call", "tool_result"], f"两类条目都写了（{kinds}）")

    # ---- completed_calls 只看 tool_result ----
    await mgr.record_tool_call("r1", call_id="c2", name="split_shots")
    done = await mgr.completed_calls("r1")
    check(done == {"c1"}, f"只有带结果的算完成（{done}）")
    await mgr.record_tool_result("r1", call_id="c2", name="split_shots", status="error")
    check(await mgr.completed_calls("r1") == {"c1", "c2"}, "失败的也算「跑过了」，不再重放")

    # ---- rebuild 不会被对象 payload 带坏 ----
    entries = [
        {"kind": "full", "payload": [{"role": "user", "content": "hi"}]},
        {"kind": "tool_call", "payload": {"call_id": "c1"}},
        {"kind": "tool_result", "payload": {"call_id": "c1"}},
        {"kind": "delta", "payload": [{"role": "assistant", "content": "ok"}]},
    ]
    msgs = rebuild(entries)
    check(len(msgs) == 2, f"rebuild 跳过对象 payload（得到 {len(msgs)} 条）")
    check(msgs[-1]["content"] == "ok", "delta 正常追加")

    # ---- 空 run ----
    check(await mgr.completed_calls("nope") == set(), "没有条目时返回空集合")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
