"""主通道「不碰壁」的几处离线验证（真机 run d27b37bf1419 暴露的那几处）。

① 附件寻址：照抄取材回执（不带 `demos/`）、带前缀、大小写不同、只给文件名，都能取到；
② 确认过的渲染**失败**之后，重交不再被当成「已经在渲」吞掉；
③ 读不出的字色由系统修，底色与图形填色不动。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework.agent import _last_render_failed                 # noqa: E402
from agent_framework.skill import Skill, SkillLoader                  # noqa: E402
from open_storyline.motion import code_spec as cs                   # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


def main() -> int:
    print("\n〔①〕附件寻址")
    files = [{"relpath": "demos/typography/paper-title-card/PaperTitleCard.tsx"},
             {"relpath": "demos/typography/brace-expand/BraceExpand.tsx"},
             {"relpath": "shots/INDEX.md"}]
    skill = Skill(name="m", description="", always=False, location="skills/m", body="",
                  files=files)
    find = SkillLoader._find_file
    want = "demos/typography/paper-title-card/PaperTitleCard.tsx"
    for path, label in (("typography/paper-title-card/PaperTitleCard.tsx", "照抄回执（无前缀）"),
                        (want, "带 demos/ 前缀"),
                        ("demos/Typography/Paper-Title-Card/PaperTitleCard.tsx", "大小写不同"),
                        ("PaperTitleCard.tsx", "只给文件名"),
                        ("typography/paper-title-card/Wrong.tsx", "卡目录对、文件名拼错")):
        got = find(None, skill, path)
        check(bool(got) and got["relpath"] == want, f"{label} → {got and got['relpath']}")
    check(find(None, skill, "nope/Nothing.tsx") is None, "真没有的不瞎猜")

    print("\n〔②〕失败过的渲染不算「已经在渲」")
    ok = [{"role": "tool", "name": "render_web", "content": '{"video": "x.mp4"}'}]
    bad = [{"role": "tool", "name": "render_web", "content": "Error: headless 截图失败"}]
    async_bad = [{"role": "tool", "name": "render_motion_code", "content": '{"status": "queued"}'},
                 {"role": "tool", "name": "storyline_render_status",
                  "content": '{"render": {"status": "failed", "error": "..."}}'}]
    check(not _last_render_failed(ok, "render_web"), "上一次成功 → 照旧拦重复提交")
    check(_last_render_failed(bad, "render_web"), "上一次同步失败 → 放行重交")
    check(_last_render_failed(async_bad, "render_motion_code"),
          "上一次排队后在 render_status 里失败 → 放行重交")

    print("\n〔③〕字色自动修")
    code = ("<div style={{background:'#fffffe'}}><h1 style={{color:'#ffffff'}}>标题</h1>"
            "<svg><text fill=\"#fff\">x</text><path fill=\"#ffffff\"/></svg></div>")
    fixed, notes = cs.autofix_ink_contrast({"shots": [{"id": "s09", "code": code}]})
    new = fixed["shots"][0]["code"]
    check(cs.check_ink_contrast(fixed["shots"])[0] == [], "修完闸⑩ 不再打回")
    check("background:'#fffffe'" in new and '<path fill="#ffffff"/>' in new,
          "底色与图形填色原样")
    check(bool(notes) and "s09" in notes[0], f"回执交代改了哪镜：{notes}")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
