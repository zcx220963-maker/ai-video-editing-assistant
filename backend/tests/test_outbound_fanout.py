# -*- coding: utf-8 -*-
"""R2：OutBound 每实例唯一消费组（固定组在多副本下只会到一个实例）。"""

from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.connection_manager import ConnectionManager, instance_group  # noqa: E402
from agent_framework.mq import InMemoryMessageQueue  # noqa: E402

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
    # ---- 两个实例拿到不同的组 ----
    a, b = instance_group("a"), instance_group("b")
    check(a != b, f"两个实例组名不同（{a} vs {b}）")
    check(a.startswith("conn-") and b.startswith("conn-"), "组名带 conn- 前缀")
    check(a == instance_group("a"), "同名幂等")

    # ---- 不给实例名时按 hostname:pid 兜底（同机多进程也是两个实例）----
    auto = instance_group(None)
    check(auto.startswith("conn-") and auto != instance_group("a"), "缺省组名唯一")

    # ---- ConnectionManager 默认就用实例组 ----
    mq = InMemoryMessageQueue()
    cm1 = ConnectionManager(mq, instance_id="i1")
    cm2 = ConnectionManager(mq, instance_id="i2")
    check(cm1.group != cm2.group, f"两个 manager 组不同（{cm1.group} / {cm2.group}）")
    check(cm1.group == "conn-i1", "组名 = conn-<instance_id>")
    # 都不给 instance_id 时同进程会拿到同一个 hostname:pid——这是对的：
    # 同一个进程就是同一个实例，多开进程才是多实例。
    check(ConnectionManager(mq).group == ConnectionManager(mq).group,
          "同进程缺省组名一致（进程才是实例）")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
