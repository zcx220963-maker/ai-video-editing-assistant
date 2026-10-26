# -*- coding: utf-8 -*-
"""短视频工具链的**装配**测试：四个工具装得上、参数对、工程能往返、B 链真的摘了。

不调模型、不渲染。功能由 `tools/smoke_tools_e2e.py` 真机跑（约 6 次调用）。

    python tests/test_short_video.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework.tool import ToolRegistry  # noqa: E402
from agent_framework.tools.short_video import (  # noqa: E402
    MIN_SEC, MAX_SEC, _clamp_seconds, register_short_video_tools)
from storyline_server.simple import edit, film, kernel, project  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _FAILS += 1
        print(f"  XX   {label}")


def main() -> int:
    print("\n① 四个工具：装得上、名字与参数对")
    reg = ToolRegistry()
    register_short_video_tools(reg, storage=None)
    check(reg.tool_names == ["make_short_video", "reshoot_shot",
                             "render_short_film", "export_film_project",
                             "list_short_films"],
          f"注册表里正好这五个（{reg.tool_names}）")
    defs = {d["function"]["name"]: d["function"] for d in reg.get_definitions()}
    check(set(defs["make_short_video"]["parameters"]["properties"])
          == {"prompt", "seconds", "aspect", "style", "render", "voice", "bgm"},
          "make_short_video 的参数是 prompt/seconds/aspect/style/render/voice/bgm")
    check(defs["make_short_video"]["parameters"]["required"] == ["prompt"],
          "只有 prompt 必填（其余都有缺省，模型不必猜）")
    check(set(defs["reshoot_shot"]["parameters"]["properties"])
          == {"film_id", "shot", "note"},
          "reshoot_shot 的参数是 film_id/shot/note")
    check(defs["reshoot_shot"]["parameters"]["required"] == ["film_id", "shot"],
          "note 不必填（用户没说改什么时也要能改）")
    check(defs["list_short_films"]["parameters"]["properties"] == {},
          "list_short_films 无参数")

    print("\n② 工具属性：只读的那件能并发，会写的那三件不能")
    check(reg.get("list_short_films").read_only, "list_short_films 声明只读")
    check(reg.get("list_short_films").concurrency_safe, "只读 → 可与别的调用同批")
    for name in ("make_short_video", "reshoot_shot", "render_short_film"):
        t = reg.get(name)
        check(not t.read_only and not t.concurrency_safe,
              f"{name} 会写状态且未声明读写集 → 独占一批")

    print("\n③ 描述里必须有的路由话术（这是模型不重做整片的唯一依据）")
    mk = defs["make_short_video"]["description"]
    rs = defs["reshoot_shot"]["description"]
    check("不需要用户上传任何素材" in mk, "make_short_video 说清「零素材」")
    check("reshoot_shot" in mk and "不要重新调这个工具" in mk,
          "make_short_video 主动把改镜指向 reshoot_shot")
    check("不要用 make_short_video 重做整条" in rs,
          "reshoot_shot 明确禁止「重做整条」")
    check("原样" in rs, "note 要求填用户原话（不翻译成术语）")
    check("不花钱" in defs["render_short_film"]["description"],
          "render_short_film 说清「不再调模型、不花钱」")

    print("\n④ 秒数夹紧：8~60，非法值退回 30")
    check(_clamp_seconds(12) == 12 and _clamp_seconds(30) == 30, "正常值原样")
    check(_clamp_seconds(2) == MIN_SEC, f"太短抬到 {MIN_SEC}")
    check(_clamp_seconds(999) == MAX_SEC, f"太长压到 {MAX_SEC}（内核物理上限）")
    check(_clamp_seconds("胡说") == 30 and _clamp_seconds(None) == 30, "非法值退回 30")

    print("\n⑤ 工程往返：存 → 读 → 改 → 列，且每镜单独落一份")
    tmp = Path(tempfile.mkdtemp(prefix="film-proj-"))
    real_out = kernel.out_dir
    kernel.out_dir = lambda: tmp          # type: ignore[assignment]
    try:
        body = film.safe_scene("测试字幕", "#22D3EE", index=0)
        code, tl = film.assemble([body, body], [3.0, 3.0], ["slide"])
        fid = project.new_id()
        project.save(fid, code=code, prompt="冒烟", seconds=6.0, aspect="portrait",
                     fps=30, plan={"title": "冒烟片", "scenes": [{}, {}]},
                     secs=[3.0, 3.0], kinds=["slide"], bodies=[body, body],
                     audit={}, coverage={}, calls=2)
        doc = project.load(fid)
        check(doc["bodies"] == [body, body], "bodies 原样读回（改一镜靠它）")
        check(doc["kinds"] == ["slide"] and doc["secs"] == [3.0, 3.0],
              "转场与每镜秒数也读得回")
        check((project.dir_of(fid) / "film.tsx").read_text(encoding="utf-8") == code,
              "film.tsx 与拼装结果逐字一致（重渲直接用这一份）")
        check((project.dir_of(fid) / "s01.tsx").exists()
              and (project.dir_of(fid) / "s02.tsx").exists(),
              "每镜单独一份 s01.tsx / s02.tsx（人能直接读改）")
        project.update(fid, mp4_url="http://x/y.mp4")
        check(project.load(fid)["mp4_url"] == "http://x/y.mp4", "update 只改指定字段")
        rows = project.list_recent()
        check(rows and rows[0]["id"] == fid and rows[0]["shots"] == 2,
              f"list_recent 能看到（{rows[0]['id'] if rows else '空'}）")
        check("bodies" not in (rows[0] if rows else {}),
              "list_recent 不读 bodies（列表要快）")

        print("\n⑥ 改一镜的入参校验在调模型之前")
        import asyncio
        try:
            asyncio.run(edit.reshoot(doc, 9, note="x"))
            check(False, "镜号越界没有拦下")
        except kernel.KernelError as exc:
            check("镜号要在 1~2" in str(exc), f"越界在调模型前被拦：{str(exc)[:30]}")
    finally:
        kernel.out_dir = real_out        # type: ignore[assignment]

    print("\n⑦ 超时表：三个长工具各有自己的档")
    from agent_framework.agent import _TOOL_CALL_TIMEOUTS as T
    check(T.get("make_short_video") == 3600.0,
          "make_short_video 一小时（180 秒片子：生成 10 分钟 + 渲染 30 分钟）")
    check(T.get("render_short_film") == 3600.0, "render_short_film 只重渲，3600s")
    check(T.get("reshoot_shot") == 900.0, "reshoot_shot 只改一镜，900s")

    print("\n⑧ B 链真的摘了：两份白名单都不再有那三个名字，且两份仍逐项相同")
    import tomllib
    ex = Path(__file__).resolve().parents[1] / "examples" / "storyline"
    lists = {}
    for name in ("config.toml", "config.docker.toml"):
        with (ex / name).open("rb") as fh:
            cfg = tomllib.load(fh)
        lists[name] = set(cfg["local_mcp_server"]["available_nodes"])
    gone = {"plan_motion", "render_motion_code", "patch_motion_code"}
    check(not (lists["config.toml"] & gone),
          f"config.toml 里已摘（残留 {sorted(lists['config.toml'] & gone)}）")
    check(not (lists["config.docker.toml"] & gone),
          f"config.docker.toml 里已摘（残留 {sorted(lists['config.docker.toml'] & gone)}）")
    check(lists["config.toml"] == lists["config.docker.toml"],
          "两份白名单仍逐项相同（只改一份等于真机上没改）")
    check("render_video" in lists["config.toml"],
          "口播那条链（render_video）仍在——摘的是图形科普那三个，不是全部")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
