"""冒烟：简洁视频线四个工具能装进注册表、schema 合法、只读工具能跑。

不调模型、不渲染，只验装配。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from agent_framework.tool import ToolRegistry
from agent_framework.tools.short_video import register_short_video_tools
from storyline_server.simple import film, kernel, project


async def main() -> int:
    reg = ToolRegistry()
    register_short_video_tools(reg, storage=None)
    names = reg.tool_names
    print("工具:", names)
    assert names == ["make_short_video", "reshoot_shot",
                     "render_short_film", "list_short_films"], names

    defs = reg.get_definitions()
    for d in defs:
        f = d["function"]
        assert f["name"] and f["description"] and f["parameters"]["type"] == "object", f
        print(f"  {f['name']:<18} 参数 {list(f['parameters'].get('properties') or {})}")

    # 只读工具直接跑（工程目录可能为空）
    out = json.loads(await reg.execute("list_short_films", {}))
    print("list_short_films:", out["ok"], "条数", len(out["films"]))

    # 时间表 + 工程落盘的往返（用假的 bodies，不渲染）
    body = film.safe_scene("测试字幕", "#22D3EE", index=0)
    code, tl = film.assemble([body, body], [3.0, 3.0], ["slide"])
    fid = "smoke-" + project.new_id()
    project.save(fid, code=code, prompt="冒烟", seconds=6.0, aspect="portrait",
                 fps=30, plan={"title": "冒烟", "scenes": [{}, {}]},
                 secs=[3.0, 3.0], kinds=["slide"], bodies=[body, body],
                 audit={}, coverage={}, calls=1)
    doc = project.load(fid)
    assert doc["bodies"] == [body, body] and doc["kinds"] == ["slide"]
    assert (project.dir_of(fid) / "film.tsx").read_text(encoding="utf-8") == code
    assert (project.dir_of(fid) / "s02.tsx").exists()
    print("工程往返 OK:", fid, "镜数", len(doc["bodies"]))

    recent = project.list_recent()
    print("最近工程:", [(r["id"], r["shots"]) for r in recent[:3]])

    # reshoot 的入参校验（不调模型：镜号越界要在调模型之前就拦下）
    try:
        await film.reshoot(doc, 9, note="x")
        print("XX 镜号越界没有拦下")
        return 1
    except kernel.KernelError as exc:
        print("镜号越界被拦:", str(exc)[:40])

    import shutil
    shutil.rmtree(project.dir_of(fid), ignore_errors=True)
    print("\nOK")
    return 0


raise SystemExit(asyncio.run(main()))
