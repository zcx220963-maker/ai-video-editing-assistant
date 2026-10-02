"""Context 构建增强验证（不联网）：装配顺序 / Bootstrap / ContextSource / compressor 接缝。

运行：  python tests/test_context.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentOnceRun, AgentConfig
from agent_framework.context import ContextBuilder
from agent_framework.llm import ScriptedLLM
from agent_framework.messages import user
from agent_framework.session import Session, SessionManager

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeMemorySource:
    name = "memory"

    def __init__(self, text):
        self._text = text
        self.seen_query = None

    async def render(self, query):
        self.seen_query = query
        return self._text


class EmptySource:
    name = "skill"

    async def render(self, query):
        return None  # 本轮无技能命中 → 不应出现该段


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "role.md").write_text("你说话简洁。", encoding="utf-8")
        (Path(tmp) / "conventions.md").write_text("中文回复。", encoding="utf-8")

        mem = FakeMemorySource("用户喜欢文艺风格")
        b = ContextBuilder(
            "BASE_PROMPT",
            bootstrap_dir=tmp,
            context_sources=[mem, EmptySource()],
            runtime_context=lambda: "当前时间：2026-09-19T00:00:00",
        )
        session = Session(user_id="u", conversation_id="c")
        session.add(user("上一轮问题"))  # 预置一条历史
        msgs = await b.build(session, "帮我写文案")

        system = msgs[0]["content"]
        # 装配顺序：BASE → bootstrap → runtime → memory
        check(system.startswith("BASE_PROMPT"), "以基础提示开头")
        check("<role.md>" in system and "你说话简洁。" in system, "注入 role.md")
        check("<conventions.md>" in system and "中文回复。" in system, "注入 conventions.md")
        check(system.index("conventions.md") < system.index("role.md"), "bootstrap 按文件名排序")
        check("<runtime>" in system and "2026-09-19" in system, "注入 runtime context")
        check("<memory>" in system and "文艺" in system, "注入 ContextSource(记忆)")
        check("skill" not in system, "render 返回 None 的来源不注入")
        check(mem.seen_query == "帮我写文案", "ContextSource 收到当前输入做相关性判断")

        # 消息结构：system → history → 当前 user
        check(msgs[1]["role"] == "user" and msgs[1]["content"] == "上一轮问题", "历史消息保留")
        check(msgs[-1]["role"] == "user" and msgs[-1]["content"] == "帮我写文案", "当前输入在末尾")

        # compressor 接缝：截断到最后 2 条消息
        async def compressor(messages, query):
            return messages[-2:]

        b2 = ContextBuilder("X", runtime_context=None, compressor=compressor)
        s2 = Session(user_id="u", conversation_id="c")
        s2.add(user("h1"))
        s2.add({"role": "assistant", "content": "a1"})
        msgs2 = await b2.build(s2, "h2")
        check(len(msgs2) == 2 and msgs2[-1]["content"] == "h2", "compressor 生效并裁剪消息")
        # runtime_context=None → 不注入 <runtime>
        check("<runtime>" not in msgs2[0]["content"], "runtime_context=None 时不注入")

        # ---- 附件结构化事实（spec §5 步骤 8）：material_id 进上下文，路径不进 ----
        video_row = {"id": "mat-7f3a9c", "filename": "海边日落.mp4", "kind": "video",
                     "duration_sec": 18.4, "width": 1920, "height": 1080, "has_audio": True}
        bgm_row = {"id": "mat-2b41de", "filename": "轻快_日常.wav", "kind": "audio",
                   "duration_sec": 62.0}
        msgs3 = await b.build(session, "把这段剪成 vlog", attachments=[video_row, bgm_row],
                              rejected_attachments=["mat-nope"])
        sys3 = msgs3[0]["content"]
        check("<attachments>" in sys3, "注入 <attachments> 段")
        check("mat-7f3a9c" in sys3 and "海边日落.mp4" in sys3 and "18.4s" in sys3
              and "1920x1080" in sys3 and "含音轨" in sys3, "素材行渲染成 id/文件名/时长/分辨率/音轨事实")
        check("mat-2b41de" in sys3 and "62.0s" in sys3, "音频素材同样列出")
        check("load_media(material_ids=" in sys3, "指示模型把 id 传给 load_media")
        check("mat-nope" in sys3 and "无法使用" in sys3, "越权/不存在的附件如实报告")
        check(sys3.index("<attachments>") > sys3.index("<memory>"), "附件段排在来源之后，贴近当前输入")
        check("<attachments>" not in msgs[0]["content"], "无附件时不注入空段")
        check("海边日落" not in msgs[-1]["content"], "当前输入仍是用户原话，未被附件污染")

        # 端到端：AgentOnceRun 使用 async build 正常跑通
        from agent_framework.tool import ToolRegistry

        llm = ScriptedLLM(steps=[("answer", "好")])
        agent = AgentOnceRun(llm=llm, registry=ToolRegistry(), context_builder=b, config=AgentConfig(max_iterations=3))
        sess = await SessionManager().get_or_create("u", "c")
        out = await agent.run(sess, "开始")
        check(out == "好" and len(llm.calls) == 1, f"经增强 ContextBuilder 的循环可跑通: {out}")
        # 该轮请求的 system 含 memory
        check("<memory>" in llm.calls[0][0]["content"], "LLM 实际收到拼装后的 system")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
