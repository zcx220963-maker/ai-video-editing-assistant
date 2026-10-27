# -*- coding: utf-8 -*-
"""T73 失败前置：review 开着而腿做不了 → 烧像素**之前**打回（R13-Q163）。

为什么单独建这份：这条闸的整个价值就在**位置**——旧形状是「烧到一半才发现回看环
跑不了，整条 run 白烧」，而判据不可用这件事其实在**第一帧之前**就查得出来。
所以下面量的是两样东西：打回文案点名到原因，以及**桩 render 一次都没被走到**。

2026-10-16：这份原来叫 `test_supply_gates.py`，装着三件事——闸㉙ 取材次序、
闸㉛ 指名权、以及这条 T73。前面那两条整条死了（取材只剩一路：模型逐镜写代码画，
没有素材库检索、没有出图、上传件不上屏），文件名与内容一起收成现在这样。

2026-10-17 加了第二支 ：⑲-b 的开关从出片链一路走到回看环（`case_identity_knob`）。
放在这份里是因为它与 T73 量的是同一条腿的一头一尾：那一支量「开不了就不许开烧」，
这一支量「开了能真的开起来」——少了后者，前者守的是一个永远打不开的开关。

运行时不需要真机服务、不需要存储：`capability` 与 `_render_shots` 都是桩。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.motion import code_render as cr_render                # noqa: E402
from open_storyline.motion import review_loop as rl_loop                  # noqa: E402
from open_storyline.mediaops import MediaError                            # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    # 失败行带 ASCII 前缀：docker exec 的输出管道会把 ✓/✗ 搅成同一个乱码，
    # 而这行前缀在错码下仍然读得出来（定位失败用例不靠符号，靠这一行）。
    print(("  [ok] " if cond else "  [FAIL] ") + label)
    if not cond:
        _fails += 1

# ⑦ T73 失败前置：review 开了而腿做不了 → 烧像素**之前**打回（R13-Q163）
# ---------------------------------------------------------------------------

async def case_t73() -> None:
    print("\n[T73 失败前置：判据不可用时一帧都不烧]")
    import shutil
    import tempfile

    real_cap, real_render = rl_loop.capability, cr_render._render_shots
    burn = {"n": 0}

    class Burned(Exception):
        """哨兵：桩 _render_shots 被走到（= 像素那一路开工了）。"""

    async def _stub_render_shots(*a, **kw):
        burn["n"] += 1
        raise Burned("像素烧起来了（桩）")

    async def _cap_off():
        return {"gate": rl_loop.GATE, "available": False, "judge": False,
                "judge_why": "判断模型没配：三件套是空的", "frame": True,
                "frame_why": "", "subject_terms_in_schema": True,
                "why": "裁决通道没配：judge 三件套是空的（桩）"}

    async def _cap_on():
        return {"gate": rl_loop.GATE, "available": True, "judge": True,
                "judge_why": "", "frame": True, "frame_why": "",
                "subject_terms_in_schema": True, "why": ""}

    client = {"health": lambda: {"ok": True, "master_hash": "a" * 16,
                                 "lockfile_hash": "b" * 16}}
    spec = {"shots": [{"id": "s01", "text": "一镜", "subject": "小球",
                       "subject_channel": "vector",
                       "code": "export default function Shot(){return null;}"}]}
    work = Path(tempfile.mkdtemp(prefix="t73-"))

    async def _run(**kw):
        burn["n"] = 0
        return await cr_render.render_code_motion(
            spec, providers=None, work=work, client=client, **kw)

    try:
        rl_loop.capability = _cap_off
        cr_render._render_shots = _stub_render_shots

        # 出口一：review 开着而腿做不了 → 烧像素之前打回并点名
        try:
            await _run(review=True)
            check(False, "腿做不了还继续往下走（T73 没打回）")
        except MediaError as exc:
            msg = str(exc)
            check("T73" in msg and "烧像素之前" in msg,
                  f"打回文案点名 T73 与「烧像素之前」：{msg[:60]}…")
            check("judge 三件套是空的（桩）" in msg,
                  "打回文案带上 capability 给的原因（点名，不是一句空话）")
            check(burn["n"] == 0,
                  f"打回那一趟一帧都没烧（桩 render 被走到 {burn['n']} 次）")

        # 出口二：review 关着 → 这道闸不拦（默认关的理由见函数 docstring）
        try:
            await _run(review=False)
            check(False, "review 关着也被 T73 拦了（闸的射程超了）")
        except Burned:
            check(burn["n"] == 1, "review 关着时过了这道闸，正常走到烧像素那一趟")

        # 出口三：腿配齐了 → 闸放行（闸只拦「开不了」这一种）
        rl_loop.capability = _cap_on
        try:
            await _run(review=True)
            check(False, "腿配齐了仍被拦（闸拦了不该拦的）")
        except Burned:
            check(burn["n"] == 1, "腿配齐时闸放行，正常走到烧像素那一趟")

        # 开关的两处读法同源：spec 里写 review=true（不传参数那条路）也生效
        spec["review"] = True
        rl_loop.capability = _cap_off
        try:
            await _run()
            check(False, "spec.review=true 没触发 T73（两条读法漂了）")
        except MediaError:
            check(burn["n"] == 0,
                  "spec 里写 review=true 也走烧像素前置（不传参数那条路同源）")
        finally:
            spec.pop("review", None)

        # D-红先：旧形状（先烧像素，ReviewBlocked 在半路抛）——同一把尺量它必红
        async def _old_shape(*a, **kw):
            try:
                await _stub_render_shots()   # 像素先烧了（桩记账 burn）
            except Burned:
                pass
            raise rl_loop.ReviewBlocked("回看环这趟开不了（裁决通道没配）")

        burn["n"] = 0
        try:
            await _old_shape()
        except rl_loop.ReviewBlocked:
            pass
        check(burn["n"] == 1,
              "D-红先：旧形状下打回时像素**已经**烧了（同一把尺对旧形状真红"
              "——这就是 T73 要换掉的形状）")
    finally:
        rl_loop.capability = real_cap
        cr_render._render_shots = real_render
        shutil.rmtree(work, ignore_errors=True)

async def case_identity_knob() -> None:
    """⑲-b 的开关真的走到了回看环上（2026-10-17）。

    为什么单独立一支：「开关建好了但没人能开」与「开关没建」在回执上是**同一句话**
    （「这趟没开」）。这一支量的是那三步：出片链把参数/`spec` 读进来、原样传给回看环、
    回看环按**两层**解析它（外层 ``review`` 关着时里面那层必须为假）。
    """
    print("\n[⑲-b 开关：从出片链一路走到回看环，且是两层的]")
    import shutil
    import tempfile

    real_cap, real_render, real_run, real_self = (
        rl_loop.capability, cr_render._render_shots, rl_loop.run, rl_loop.self_test)
    client = {"health": lambda: {"ok": True, "master_hash": "a" * 16,
                                 "lockfile_hash": "b" * 16}}
    work = Path(tempfile.mkdtemp(prefix="idknob-"))

    class Got(Exception):
        """哨兵：回看环被调用（把 kwargs 带出来，不真跑那趟环）。"""

        def __init__(self, kw):
            super().__init__("captured")
            self.kw = kw

    async def _cap_ok():
        return {"gate": rl_loop.GATE, "available": True, "judge": True,
                "judge_why": "", "frame": True, "frame_why": "",
                "subject_terms_in_schema": False, "why": ""}

    async def _self_ok():
        return {"ok": True, "why": "", "detail": None}

    async def _ledger(*a, **kw):
        # 逐镜那一趟的返回值形状：clips / ledger / files / hitmaps / frames_used
        return [], [], {}, {}, 0

    async def _run(**kw) -> dict:
        return await cr_render.render_code_motion(
            kw.pop("spec"), providers=None, work=work, client=client, **kw)

    try:
        rl_loop.capability = _cap_ok
        cr_render._render_shots = _ledger
        # 自证是真调帧通道的（`judge_channel.self_test`）——离线跑这份用例不能让它出去，
        # 而这一支要量的东西与「判官配没配」无关。
        rl_loop.self_test = lambda: _self_ok()

        async def _capture(**kw):
            raise Got(kw)

        rl_loop.run = _capture

        def _spec(**over) -> dict:
            base = {"fps": 30, "shots": [
                {"id": "s01", "text": "一镜", "subject": "蒸汽机",
                 "subject_channel": "rough",
                 "code": "export default function Shot(){return null;}"}]}
            base.update(over)
            return base

        # ① `spec` 里那一栏（不传参数那条路）
        try:
            await _run(spec=_spec(identity=True) | {"review": True})
            check(False, "回看环压根没被走到（这一支量不到东西）")
        except Got as got:
            check(got.kw.get("enabled") is True and got.kw.get("identity") is True,
                  f"spec 写着两个开关 → 回看环收到 enabled/identity 都是 True："
                  f"{ {k: got.kw.get(k) for k in ('enabled', 'identity')} }")

        # ② 顶层参数那条路同样生效（与 `review` 逐字同规）
        try:
            await _run(spec=_spec(), review=True, identity=True)
            check(False, "第二次也没走到回看环")
        except Got as got:
            check(got.kw.get("identity") is True,
                  f"不写 spec、只传参数也生效：{got.kw.get('identity')}")

        # ③ 显式参数压过 spec 里那一栏：这是「花钱的能力必须能被人关掉」的守卫
        try:
            await _run(spec=_spec(identity=True) | {"review": True}, identity=False)
            check(False, "第三次也没走到回看环")
        except Got as got:
            check(got.kw.get("identity") is False,
                  f"identity=False 压过 spec 里的 true：{got.kw.get('identity')}")

        # ④ 外层关着时里面那层必须为假——这一条打在回看环自己身上（走的是真函数）。
        #    为什么它值得一量：外层没开时算出来的红镜**无从处置**，那笔钱是白花的。
        async def _render_fn(new_spec):
            return {}

        got_receipt = await real_run(
            spec=_spec(identity=True), first={"shots": [], "shot_files": {}},
            render_fn=_render_fn, cache=None, work=work, enabled=False, identity=True, fps=30)
        receipt = got_receipt["receipt"]
        check(receipt["identity"]["enabled"] is False
              and receipt["identity"]["counts"]["not_run"] == 1,
              f"外层 review 关着 → ⑲-b 一格调用都不发，逐镜记 not_run"
              f"（{receipt['identity']['counts']}）")
        check("没开" in receipt["identity"]["why"],
              f"回执明写「没开」而不是留白：{receipt['identity']['why'][:40]}…")
        check(receipt["identity"]["knob"] == rl_loop.IDENTITY_KNOB,
              "回执那一栏写着开关叫什么，读的人不用去猜是哪一档")
    finally:
        rl_loop.capability, cr_render._render_shots, rl_loop.run, rl_loop.self_test = (
            real_cap, real_render, real_run, real_self)
        shutil.rmtree(work, ignore_errors=True)


async def main() -> int:
    await case_t73()
    await case_identity_knob()
    print(f"\n{_checks} 项断言，{_fails} 项失败")
    # ASCII 汇总行：docker exec 的输出管道会把中文与符号搅成乱码，而这行是纯 ASCII，
    # 回执里读它就能知道红绿（✓/✗ 在错码下长得一样，数字位会被换成 ?）。
    print(f"RESULT checks={_checks} fails={_fails}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
