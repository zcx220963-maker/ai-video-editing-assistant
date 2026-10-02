"""FileTool 家族验证脚本（无需第三方依赖）。

运行：  python tests/test_file_tools.py
在临时目录内验证 Write/Read/Edit/Grep 的正常路径、错误路径，以及 spec §8 第八项修正：
根目录每次调用现取、且只能是**本会话**的工作区沙箱——越界、绝对路径、换会话都碰不到别人的文件。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.identity import use_identity
from agent_framework.tool import ToolRegistry, is_tool_error
from agent_framework.tools.file import register_file_tools, session_workspace_root

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        _fails += 1
        print(f"  ✗ {label}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry()
        register_file_tools(reg, lambda: Path(tmp))
        print("已注册:", reg.tool_names)

        # 并发标记
        check(reg.get("read_file").concurrency_safe, "read_file 可并发 (read_only)")
        check(reg.get("grep").concurrency_safe, "grep 可并发 (read_only)")
        check(not reg.get("write_file").concurrency_safe, "write_file 不可并发")
        check(not reg.get("edit_file").concurrency_safe, "edit_file 不可并发")

        # Write
        r = await reg.execute("write_file", {"path": "a/hello.txt", "content": "line1\nline2\nline3"})
        check("创建" in r, f"write 创建文件: {r}")
        check(Path(tmp, "a/hello.txt").read_text(encoding="utf-8") == "line1\nline2\nline3", "内容落盘正确")

        # Read
        r = await reg.execute("read_file", {"path": "a/hello.txt"})
        check("1\tline1" in r and "3\tline3" in r, "read 返回带行号内容")
        r = await reg.execute("read_file", {"path": "a/hello.txt", "offset": 2, "limit": 1})
        check("line2" in r and "line1" not in r, "read 分页 offset/limit 生效")
        r = await reg.execute("read_file", {"path": "missing.txt"})
        check(is_tool_error(r), f"read 缺失文件报错: {str(r)[:20]}")

        # Edit
        r = await reg.execute("edit_file", {"path": "a/hello.txt", "old_string": "line2", "new_string": "LINE_TWO"})
        check("已替换 1 处" in r, f"edit 唯一替换: {r}")
        check("LINE_TWO" in Path(tmp, "a/hello.txt").read_text(encoding="utf-8"), "edit 落盘正确")
        r = await reg.execute("edit_file", {"path": "a/hello.txt", "old_string": "nope", "new_string": "x"})
        check(is_tool_error(r), "edit 未命中报错")
        # 制造多次匹配，测试不唯一 + replace_all
        await reg.execute("write_file", {"path": "dup.txt", "content": "x x x"})
        r = await reg.execute("edit_file", {"path": "dup.txt", "old_string": "x", "new_string": "y"})
        check("不唯一" in str(r), f"edit 多重匹配报错: {str(r)[:24]}")
        r = await reg.execute("edit_file", {"path": "dup.txt", "old_string": "x", "new_string": "y", "replace_all": True})
        check("已替换 3 处" in r and Path(tmp, "dup.txt").read_text(encoding="utf-8") == "y y y", "edit replace_all 全替换")

        # Grep
        await reg.execute("write_file", {"path": "src/mod.py", "content": "def foo():\n    return 42\n"})
        r = await reg.execute("grep", {"pattern": "def ", "glob": "*.py"})
        check("src/mod.py:1" in r, f"grep 命中带 file:line: {r.splitlines()[0]}")
        r = await reg.execute("grep", {"pattern": "RETURN", "case_insensitive": True})
        check("return 42" in r, "grep case_insensitive 生效")
        r = await reg.execute("grep", {"pattern": "zzz_none"})
        check("未找到匹配" in r, "grep 无命中提示")
        r = await reg.execute("grep", {"pattern": "("})
        check(is_tool_error(r), "grep 非法正则报错")

        # 路径越界防护
        r = await reg.execute("read_file", {"path": "../../../../etc/passwd"})
        check(is_tool_error(r) and "越出" in str(r), f"越界路径被拦截: {str(r)[:30]}")
        r = await reg.execute("write_file", {"path": "/tmp/pwned", "content": "x"})
        check(is_tool_error(r), "绝对路径越界被拦截")

    # ---- 第八项修正：根目录 = 本会话工作区沙箱，且每次调用现取 ----
    with tempfile.TemporaryDirectory() as tmp:
        # 模拟「仓库根」：里面有密钥文件与源码，工作区只是它下面的一个子目录
        repo = Path(tmp)
        (repo / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
        (repo / "agent_framework").mkdir()
        (repo / "agent_framework" / "llm_openai.py").write_text("KEY = 'sk-secret'\n", encoding="utf-8")
        ws_root = repo / "workspace"

        reg = ToolRegistry()
        register_file_tools(reg, session_workspace_root(ws_root))

        # 未进入任何 run（离线直调）→ 缺省作用域，也仍在 workspace 里
        r = await reg.execute("write_file", {"path": "scratch.txt", "content": "draft"})
        sandbox_default = ws_root / "default_default" / "_files"
        check((sandbox_default / "scratch.txt").is_file(), f"无身份时落缺省沙箱: {r}")

        # alice 的一次 run：写到自己名下
        with use_identity("alice", "conv-a"):
            r = await reg.execute("write_file", {"path": "notes/plan.md", "content": "alice 的计划"})
            alice_box = ws_root / "alice_conv-a" / "_files"
            check((alice_box / "notes/plan.md").is_file(), f"按身份现取根目录: {r}")
            r = await reg.execute("read_file", {"path": "notes/plan.md"})
            check("alice 的计划" in r, "同会话读回自己的文件")
            # 越界读密钥 / 越界写仓库
            r = await reg.execute("read_file", {"path": "../../.env"})
            check(is_tool_error(r) and "越出" in str(r), f"读不到仓库根的 .env: {str(r)[:30]}")
            r = await reg.execute("read_file", {"path": str(repo / ".env")})
            check(is_tool_error(r), "绝对路径指向 .env 也被拒")
            r = await reg.execute("edit_file", {
                "path": "../../agent_framework/llm_openai.py",
                "old_string": "sk-secret", "new_string": "x"})
            check(is_tool_error(r), "改不了仓库源码")
            r = await reg.execute("grep", {"pattern": "sk-secret", "path": "../.."})
            check(is_tool_error(r) and "越出" in str(r), f"grep 起点也越不了界: {str(r)[:30]}")
            r = await reg.execute("grep", {"pattern": "sk-secret"})
            check("未找到匹配" in r, f"grep 扫不到沙箱外的内容: {r[:24]}")
            check((repo / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-secret", ".env 原样未动")
            check("KEY = 'sk-secret'" in (repo / "agent_framework" / "llm_openai.py").read_text(encoding="utf-8"),
                  "仓库源码原样未动")
            check(not (repo / "inject.py").exists() and not (ws_root / "inject.py").exists(),
                  "越界写没有留下任何文件")

        # bob 的另一次 run：看不见 alice 的文件（同一份进程级工具实例）
        with use_identity("bob", "conv-b"):
            r = await reg.execute("read_file", {"path": "notes/plan.md"})
            check(is_tool_error(r), f"两会话工作区互不可见: {str(r)[:24]}")
            r = await reg.execute("grep", {"pattern": "alice 的计划"})
            check("未找到匹配" in r, "grep 搜不到别人的会话内容")
            r = await reg.execute("write_file", {"path": "notes/plan.md", "content": "bob 的计划"})
            check((ws_root / "bob_conv-b" / "_files" / "notes/plan.md").is_file(), f"各写各的沙箱: {r}")
        check((alice_box / "notes/plan.md").read_text(encoding="utf-8") == "alice 的计划",
              "bob 的写入没有覆盖 alice 的同名文件")

        # 同一身份换会话 → 另一个沙箱
        with use_identity("alice", "conv-c"):
            r = await reg.execute("read_file", {"path": "notes/plan.md"})
            check(is_tool_error(r), "同一用户不同会话也不共享工作区")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
