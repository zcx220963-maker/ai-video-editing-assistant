# -*- coding: utf-8 -*-
"""技能以「文件夹」安装与浏览：剥壳、多个包、包内文件预览。

真机里最常见的三件事，以前每一件都会静默走偏：

* 从下载目录拖进来，多套了一层（`outer/inner/mg-01/SKILL.md`）→ 旧实现只剥一层，
  入口检查判「没有 SKILL.md」，用户看着文件夹里明明有；
* 拖一个装着好几个技能文件夹的父文件夹 → 应该一次装完，而不是装出一个读不到
  入口的空记录；
* 装上之后界面要能点开看包里的文件——没有这条读路径，附件清单只是一串名字。

运行：  python tests/test_skill_folder_tree.py
"""

from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

from agent_framework.agent import Agent, AgentConfig  # noqa: E402
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.mq import InMemoryMessageQueue  # noqa: E402
from agent_framework.server import (  # noqa: E402
    _has_skill_entry, _strip_wrappers, create_app,
)
from agent_framework.session import SessionManager  # noqa: E402
from agent_framework.skill import SkillLoader  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import ToolRegistry  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _paths(items):
    return [rel for rel, _ in items]


def _items(*paths: str):
    return [(p, None) for p in paths]


def unit_part() -> None:
    print("① 剥壳：剥到认得出入口为止，认得出就不再剥")
    check(_paths(_strip_wrappers(_items("mg-01/SKILL.md", "mg-01/demos/a.tsx")))
          == ["mg-01/SKILL.md", "mg-01/demos/a.tsx"], "顶层就是技能文件夹：原样不动")
    check(_paths(_strip_wrappers(_items("outer/mg-01/SKILL.md"))) == ["mg-01/SKILL.md"],
          "多套一层：剥掉 outer")
    check(_paths(_strip_wrappers(_items("a/b/c/mg-01/SKILL.md", "a/b/c/mg-01/x.md")))
          == ["mg-01/SKILL.md", "mg-01/x.md"], "多套三层：一直剥到技能文件夹")
    check(_paths(_strip_wrappers(_items("parent/A/SKILL.md", "parent/B/SKILL.md")))
          == ["A/SKILL.md", "B/SKILL.md"], "父文件夹里两个技能：剥掉父层，两个都留")
    check(_paths(_strip_wrappers(_items("x/y/A/SKILL.md", "x/y/B/SKILL.md")))
          == ["A/SKILL.md", "B/SKILL.md"], "父文件夹本身也套了一层：先剥壳再拆包")
    check(_paths(_strip_wrappers(_items("p/SKILL.md", "p/notes.md")))
          == ["p/SKILL.md", "p/notes.md"], "根上就是 SKILL.md：一层都不剥")
    check(not _has_skill_entry(_items("notes.md", "pic.jpg")), "没有入口的文件堆不认")


def _skill_md(name: str, desc: str = "") -> str:
    return ("---\n"
            f"name: {name}\n"
            f"display: {name}\n"
            f"description: {desc or name}\n"
            "kind: workflow\n"
            "---\n\n"
            f"# {name}\n\n这是 {name} 的正文。\n")


def server_part() -> None:
    print("\n② 接口：拖进父文件夹 → 装成多个技能；包里的文件能点开看")
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        loader = SkillLoader(storage)
        agent = Agent(llm=ScriptedLLM(steps=[("answer", "ok")]),
                      registry=ToolRegistry(),
                      session_manager=SessionManager(storage),
                      context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
                      config=AgentConfig(max_iterations=2),
                      storage=storage)
        app = create_app(agent, InMemoryMessageQueue(), storage=storage,
                         skill_loader=loader)

        with TestClient(app) as client:
            j = client.post("/register", json={}).json()
            hdr = {"Authorization": f"Bearer {j['token']}"}

            def import_folder(files, root=""):
                return client.post("/skills/import_folder",
                                   json={"files": files, "root": root}, headers=hdr)

            # 多套一层：outer/inner/<技能>/SKILL.md
            r = import_folder([
                {"path": "outer/inner/nest_one/SKILL.md", "text": _skill_md("nest_one")},
                {"path": "outer/inner/nest_one/demos/a.tsx", "text": "export const A = 1;\n"},
            ], "outer")
            check(r.status_code == 200, f"多套一层能装上（{r.status_code} {r.text[:120]}）")
            check((r.json().get("imported") or []) == ["nest_one"], "装成 nest_one 这一个技能")

            # 父文件夹里两个技能
            r = import_folder([
                {"path": "pack/two_a/SKILL.md", "text": _skill_md("two_a")},
                {"path": "pack/two_a/notes.md", "text": "a 的笔记\n"},
                {"path": "pack/two_b/SKILL.md", "text": _skill_md("two_b")},
            ], "pack")
            check(r.status_code == 200, f"父文件夹一次装多个（{r.status_code} {r.text[:120]}）")
            check(sorted(r.json().get("imported") or []) == ["two_a", "two_b"],
                  f"两个都装上（{(r.json().get('imported') or [])}）")

            # 一个入口都没有：说清楚，不留空记录
            r = import_folder([{"path": "junk/readme.md", "text": "hi"}], "junk")
            check(r.status_code == 400 and "SKILL.md" in r.text, "没有入口就 400，并说清原因")

            # 包里文件能读
            r = client.get("/skills/nest_one/file", params={"path": "demos/a.tsx"}, headers=hdr)
            j2 = r.json()
            check(r.status_code == 200 and j2.get("text") == "export const A = 1;\n",
                  f"包内文本文件能读出来（{r.status_code} {str(j2)[:120]}）")
            check(j2.get("binary") is False and j2.get("bytes") == len("export const A = 1;\n"),
                  "文件大小与「是文本」都对得上")

            # 二进制：给直链，不给乱码
            blob = base64.b64encode(b"\xff\xfe\x00\x01").decode()
            r = import_folder([
                {"path": "bin_pack/SKILL.md", "text": _skill_md("bin_pack")},
                {"path": "bin_pack/media/blob.bin", "b64": blob},
            ], "bin_pack")
            check(r.status_code == 200, f"带二进制附件的包能装（{r.status_code} {r.text[:120]}）")
            r = client.get("/skills/bin_pack/file", params={"path": "media/blob.bin"}, headers=hdr)
            j3 = r.json()
            check(r.status_code == 200 and j3.get("binary") is True and j3.get("text") is None,
                  "二进制按二进制回（不把字节硬解成字符串）")

            # 表里没有的路径：不按用户给的路径去对象存储盲取
            r = client.get("/skills/nest_one/file", params={"path": "../../etc/passwd"},
                           headers=hdr)
            check(r.status_code == 404, f"未登记的路径 404（{r.status_code}）")


def main() -> None:
    unit_part()
    server_part()
    print(f"\n{'全过' if _fails == 0 else str(_fails) + ' 条红'}（{_checks} 项）")
    raise SystemExit(1 if _fails else 0)


if __name__ == "__main__":
    main()
