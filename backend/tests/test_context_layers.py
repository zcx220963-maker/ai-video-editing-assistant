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

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


def _session():
    from agent_framework.session import Session
    return Session(user_id="u1", conversation_id="c1")


if __name__ == "__main__":
    main()
