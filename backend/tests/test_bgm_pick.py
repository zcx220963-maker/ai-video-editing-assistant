# -*- coding: utf-8 -*-
"""曲库配乐：选哪一首、怎么降级、混音有没有真的闪避。

为什么要单独一个测试文件：这条线**曾经整段是死的**——`voice.pick_bgm()` 只读
`SIMPLE_BGM_DIR`，而容器里没有那个目录，于是"配了曲库"在成片里等于没配，
而所有测试都是绿的（没人测"配乐到底加进去了没有"）。

四条：
① `choose()` 是纯函数，规则能单测（时长够不够盖住整片）
② `pick()` 三级降级：曲库匹配 → 曲库全部 → 本人任意音频 → None
③ 取不到 / 存储不可用 / 回源失败，一律**返回 None 不抛**（配乐是加分项）
④ 混音滤镜图里必须有 `normalize=0` 与 `sidechaincompress`

运行：  python tests/test_bgm_pick.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework import bgm  # noqa: E402
from storyline_server.simple import mix  # noqa: E402

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


class _Materials:
    """假素材仓库：按 (origin, query) 决定返回什么，并记下被问了哪几级。"""

    def __init__(self, plan: dict[tuple[str | None, str], list[dict[str, Any]]],
                 boom: bool = False) -> None:
        self.plan = plan
        self.boom = boom
        self.asked: list[tuple[str | None, str]] = []

    async def search(self, user_id: str, query: str, *, conv_id: str | None = None,
                     kinds: tuple[str, ...] = (), origin: str | None = None,
                     limit: int = 20) -> list[dict[str, Any]]:
        self.asked.append((origin, query))
        if self.boom:
            raise RuntimeError("PG 挂了")
        return self.plan.get((origin, query), [])


class _Objects:
    def __init__(self, boom: bool = False) -> None:
        self.boom = boom
        self.got: list[str] = []

    async def localize(self, key: str, dst_dir: Path) -> Path:
        self.got.append(key)
        if self.boom:
            raise RuntimeError("对象存储不可用")
        dst = Path(dst_dir) / Path(key).name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"\0" * 2048)
        return dst


class _Storage:
    def __init__(self, plan=None, *, search_boom: bool = False,
                 localize_boom: bool = False) -> None:
        self.materials = _Materials(plan or {}, boom=search_boom)
        self.objects = _Objects(boom=localize_boom)


def row(name: str, dur: float, origin: str = "bgm") -> dict[str, Any]:
    return {"id": "mat" + name[:2], "object_key": f"bgm/{name}", "filename": name,
            "duration_sec": dur, "origin": origin, "kind": "audio"}


def main() -> int:
    print("\n① choose()：纯函数，时长够不够盖住整片")
    check(bgm.choose([]) is None, "没有候选 → None")
    a, b = row("a.mp3", 40), row("b.mp3", 200)
    check(bgm.choose([a, b], 0) is a, "没给 need_sec → 取第一条（最相关）")
    check(bgm.choose([a, b], 90) is b, "90 秒的片子：挑盖得住的 b（不是更相关的 a）")
    check(bgm.choose([a], 90) is a, "一首都盖不住 → 取最长的（循环次数最少）")
    check(bgm.choose([row("c.mp3", 0), a], 90) is a, "读不出时长的排最后")
    d = row("d.mp3", 95)
    check(bgm.choose([d, row("e.mp3", 100)], 100) is d,
          "95 秒也算「盖得住」100 秒（留 5% 给 probe 与容器时长误差）")

    print("\n② pick()：三级降级")
    plan = {
        ("bgm", "三体 信号"): [row("signal.mp3", 120)],
        ("bgm", ""): [row("any.mp3", 60)],
        (None, ""): [row("mine.mp3", 30, origin="upload")],
    }
    with tempfile.TemporaryDirectory() as td:
        st = _Storage(plan)
        got = asyncio.run(bgm.pick(st, "u1", "三体 信号", work_dir=Path(td), need_sec=60))
        check(got is not None and got["filename"] == "signal.mp3",
              f"第一级命中曲库匹配（{got and got['filename']}）")
        check(st.materials.asked == [("bgm", "三体 信号")],
              f"命中就不再往下问（{st.materials.asked}）")
        check(got["origin"] == "bgm", "来源标成曲库")
        check(Path(got["path"]).is_file(), "回源到本地了（ffmpeg 要的是路径不是 key）")

    with tempfile.TemporaryDirectory() as td:
        st = _Storage({("bgm", ""): [row("any.mp3", 60)],
                       (None, ""): [row("mine.mp3", 30, origin="upload")]})
        got = asyncio.run(bgm.pick(st, "u1", "对不上的主题", work_dir=Path(td)))
        check(got is not None and got["filename"] == "any.mp3",
              "名字对不上 → 退到「曲库里所有的」")
        check([o for o, _ in st.materials.asked] == ["bgm", "bgm"],
              f"只问了曲库两级（{[o for o, _ in st.materials.asked]}）")

    with tempfile.TemporaryDirectory() as td:
        st = _Storage({(None, ""): [row("mine.mp3", 30, origin="upload")]})
        got = asyncio.run(bgm.pick(st, "u1", "对不上的主题", work_dir=Path(td)))
        check(got is not None and got["origin"] == "upload",
              "曲库空 → 退到「本人任意音频」（对话里传的 mp3）")

    print("\n③ 取不到 / 出错：一律 None，不抛")
    with tempfile.TemporaryDirectory() as td:
        st = _Storage({})
        check(asyncio.run(bgm.pick(st, "u1", "x", work_dir=Path(td))) is None,
              "三级都空 → None")
        st = _Storage({("bgm", "x"): [row("a.mp3", 60)]}, search_boom=True)
        check(asyncio.run(bgm.pick(st, "u1", "x", work_dir=Path(td))) is None,
              "素材表查不了 → None（不抛）")
        st = _Storage({("bgm", "x"): [row("a.mp3", 60)]}, localize_boom=True)
        check(asyncio.run(bgm.pick(st, "u1", "x", work_dir=Path(td))) is None,
              "回源失败 → None（不抛）")

    print("\n④ 混音滤镜图：旁白不能被 amix 除以 2，音乐要让路")
    seen: list[str] = []
    orig = mix.run

    def spy(args: list[str], **kw: Any) -> None:
        seen.append(" ".join(args))
        raise RuntimeError("只看参数，不真跑")

    mix.run = spy
    try:
        mix.add_bgm(Path("/tmp/v.m4a"), Path("/tmp/o.m4a"), total_sec=90.0,
                    bgm=Path("/tmp/b.mp3"))
    except RuntimeError:
        pass
    finally:
        mix.run = orig
    graph = seen[0] if seen else ""
    check("normalize=0" in graph,
          "`amix` 显式 normalize=0（缺省 1 会把旁白除 2，听起来就是「声音小」）")
    check("sidechaincompress" in graph, "有侧链闪避：旁白一响音乐自动让路")
    check("afade=t=in" in graph and "afade=t=out" in graph,
          "首尾淡入淡出（否则循环接缝会有一声「咔」）")
    check("atrim=0:90.000" in graph, "铺满到整片长度")
    check("stream_loop" in graph, "短曲循环铺满")

    print("\n⑤ 本地兜底目录：没曲库时扔几个 mp3 也能出片")
    if shutil.which("ffmpeg") is None:
        print("  --   跳过（本机没有 ffmpeg）")
    else:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "bgm"
            d.mkdir()
            (d / "a.mp3").write_bytes(b"\0" * 1024)
            old = os.environ.get(mix.BGM_ENV)
            os.environ[mix.BGM_ENV] = str(d)
            try:
                got = mix.pick_local("任意种子")
                check(got is not None and got.name == "a.mp3", "从目录里挑到一首")
                check(mix.pick_local("任意种子") == got, "同一 seed 结果稳定（重渲不变）")
            finally:
                if old is None:
                    os.environ.pop(mix.BGM_ENV, None)
                else:
                    os.environ[mix.BGM_ENV] = old

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
