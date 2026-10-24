# -*- coding: utf-8 -*-
"""七道闸各一条「先证红再证绿」，外加报告层那几行账（新链 P2-e / B4）。

真机上的一次 run 已经跑通（`backend/.smoke/b22_code_chain_render_smoke.py`：两镜 →
8.27s 成片，闸①②③④⑤ 全过）。这份文件管的是**反过来**的那一半：每道闸都必须能被一
个故意的坏输入打到红，并且红的现场要长成这样——

* 红要**早**：闸①/② 拒绝时后面的镜一帧都不烧，闸⑤ 拒绝时母带一个字节都没拼；
* 红要**响**：错误文本里带着闸号、镜号和实际读数，不能只说「失败」；
* 红**不许变成静默**：不截帧、不补帧、不拿猜的底色判空、不因为某镜没回读到 token
  就把它悄悄排除在比对之外。

假服务用的是 `remotion_client.endpoints()` 预留的注入口（`{health, lint, render, fetch}`
四个键），切片由 ffmpeg 现场合成——所以这里验的是**执行层对读数的反应**，不是 Remotion
本身（那部分归 `remotion/tests/gate1-lint.mjs` 与真机冒烟）。

末尾多一节〔出处对账〕：它不在闸①~⑥ 之内，判的是**分镜入口**——代码镜那句凭据对不
对得上本会话的检索账（`plan_motion` 的入库前那道），以及绕过 `plan_motion` 直接塞 inline
分镜那条旁路有没有同样对账（`render_motion_code`）。判据只有一份（`motion_plan.gate_state` /
`settle_sources`），两个入口都指向它，第二条入口存在的意义是「手写 spec 也休想绕过这一道」。
2026-10-19 起对不上账**不再打回**：就地改写成「未核实：…」再入库/出片，同一句账照样落到
分镜卡与逐镜行上。

便宜档：`narration=False` 全程不碰 TTS；主链尺寸 240×320、fps 10，让每次 ffmpeg 落在几十
毫秒——闸的判据与像素数无关。**唯一例外是闸④ 的假阳性回归**：它按真机 s02 的尺寸
1080×1920 造细网格，否则 1×1 area 那条旧口径在小画布上抹不出 0.00%，红就打不出来了。
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server import mediaops                            # noqa: E402
from storyline_server.mediaops import MediaError                 # noqa: E402
from storyline_server.motion import code_render as cr            # noqa: E402
from storyline_server.motion import code_spec as cs              # noqa: E402
from storyline_server.motion import remotion_client as rc        # noqa: E402

_fails = 0
_checks = 0

BG = "#f6f1e4"                       # 与母版 tokens.bg 同一个值
BG0X = "0x" + BG[1:]                 # ffmpeg 的 color 源只认 0x 写法（# 会被解析成灰块）
W, H, FPS = 240, 320, 10
WORK = Path("/tmp") / "code_gates_work"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _shot(i: int = 1, **over) -> dict:
    # 默认代码走最短的那条画法通道（裸 `<svg/>`）：闸⑦「每镜必画」逐镜判，
    # 一镜只有文字与色块连 `_spec` 都过不去（它会先跑一遍真契约）。
    # P1（2026-10-17）注入契约也要过：读 `getInputProps`、用到 `text`、摆
    # `<CaptionLayer/>` 各堵一条岔路，让用例的红只来自它自己要判的那件事。
    base = {"id": f"s{i:02d}",
            "code": ('export const Shot = () => <FilmLayout><svg />'
                     "{getInputProps().text}<CaptionLayer /></FilmLayout>;"),
            "text": "斜纹填满的一片圆",
            "min_duration_sec": 2.0,
            "card": "typography/paper-title-card",
            # 闸⑯：画面主体要具名，`subject_channel` 要与代码里那条通道对齐——
            # 默认代码是 `vector` 就报 `vector`，换通道的那几支夹具自己覆盖（见 shots6）。
            "subject_channel": "vector",
            "subject": "斜纹圆",
            "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}
    base.update(over)
    return base


def _spec(shots: list[dict]) -> dict:
    """先过一遍真契约，再把尺寸/fps/旁白降成离线用例的便宜档（闸读的仍是 spec 里的值）。"""
    raw = {"title": "夹具", "aspect": "portrait", "fps": FPS, "narration": False,
           "caption_style": "on_picture", "texture": "none", "shots": shots}
    spec, errors = cs.validate_code_spec(cs.overlay_knobs(dict(raw), {}))
    assert errors == [], f"夹具本身没过契约：{errors}"
    spec = copy.deepcopy(spec)
    spec["width"], spec["height"] = W, H
    return spec


def _synthesize(props: dict, dest: Path, *, mode: str) -> None:
    frames = int(props["durationInFrames"])
    w, h, fps = int(props["width"]), int(props["height"]), int(props["fps"])
    if mode == "short":
        frames = max(1, frames - 3)                 # 闸③：渲完少给 3 帧
    if mode == "wrongsize":
        w += 20                                     # 闸③：尺寸不对
    draw = {"ok": f"drawbox=x={w // 4}:y={h // 4}:w={w // 2}:h={h // 2}:c=0xd62828:t=fill",
            "block": f"drawbox=x={w // 4}:y={h // 4}:w={w // 2}:h={w // 2}:c=0xd62828:t=fill",
            "short": f"drawbox=x={w // 4}:y={h // 4}:w={w // 2}:h={h // 2}:c=0xd62828:t=fill",
            "wrongsize": f"drawbox=x={w // 4}:y={h // 4}:w={w // 2}:h={h // 2}:c=0xd62828:t=fill",
            "grid": "drawgrid=w=96:h=96:t=1:c=0xd62828",
            # 细线条画法（真机那批手绘/公式的形状）：像素少**但铺满带子**。
            # 1px 网格、间距 400：占比在 1080×1920 上实测 **1.07%**（与真机那片 roughjs
            # 斜纹的 0.99% 同一档），在 240×320 的便宜档上 **1.45%**——两块画布都落在
            # 2% 线以下，外接盒却都还是 100%。间距 200 试过的读数是 2.14%：过线，
            # 证不出「单看占比会把合法画法打回」这一半，所以不用。便宜档上 400 间距只剩
            # 左上那一折——这条夹具要的是**像素算术**（占比低 + 外接盒大），不是形状好看。
            "hairline": "drawgrid=w=400:h=400:t=1:c=0xd62828",
            # 贴纸式构图：占比够（整块实心），但外接盒只够到短边的 25%。
            "tiny": (f"drawbox=x={3 * w // 8}:y={3 * h // 8}:w={w // 4}:h={h // 4}"
                     ":c=0xd62828:t=fill")}.get(mode, "")
    args = ["-v", "error", "-f", "lavfi", "-i", f"color=c={BG0X}:s={w}x{h}:r={fps}"]
    if draw:
        args += ["-vf", draw]
    args += ["-frames:v", str(frames), "-pix_fmt", "yuv420p", str(dest)]
    mediaops.ffmpeg(*args, timeout=120)


class FakeService:
    """creation-remotion 的替身：按 props 里注入的 durationInFrames 出货。

    执行层拿的是 `client["render"]` 这一组下标（`remotion_client.endpoints()` 返回四个
    键的 dict），所以替身也按下标交口；键名不在四口之内就抛 KeyError，不顺手 getattr
    到别的属性上把拼错掩盖成「跑通了」。

    `mode` 决定这一镜「渲出来的是什么」：ok/block 画了一大块红，flat 整帧就是底色，
    grid 只画细网格（闸④ 假阳性的回归形状），hairline 画更疏的细网格（像素少但铺满带子——
    闸④ 第一半单独判会打回的那种合法画风），tiny 在中间画一小块实心（占比够却铺不开——
    闸④ 第二半要拦的那种贴纸构图），short/wrongsize 交回对不上的读数，
    lint 让闸① 在 /render-shot 里拒绝。
    """

    #: 与 `remotion_client.endpoints()` 逐键相等（**四个**口）。
    #: （2026-10-16 摘了 `stage_asset`：投递腿随素材那三条路一起拆了。）
    ENDPOINTS = ("health", "lint", "render", "fetch")
    def __init__(self, *, mode: str = "ok", tokens: dict | None = None) -> None:
        self.mode = mode
        self.tokens = {"bg": BG, "fontSize": 64} if tokens is None else tokens
        self.calls: list[dict] = []

    def __getitem__(self, name: str):
        if name not in self.ENDPOINTS:
            raise KeyError(name)
        return getattr(self, name)

    def health(self) -> dict:
        return {"ok": True, "node": "v24", "remotion": "4.0.534", "gl": "swangle",
                "master_hash": "master0000000000", "lockfile_hash": "lockfile000000"}

    def lint(self, code: str) -> dict:              # 本层不经这里（闸① 在服务端），接口留着
        return {"ok": True, "errors": []}

    def render(self, code: str, cache_key: str, input_props: dict,
               declared_fields: list[str] | None = None,
               render: dict | None = None) -> dict:
        """位置参数口径跟 `remotion_client.endpoints()` 的 `render` 那一项逐位对齐。"""
        self.calls.append({"cache_key": cache_key, "props": input_props,
                           "declared_fields": declared_fields})
        if self.mode == "lint":
            raise rc.RemotionError(
                "闸① lint 未过：dg/must-wrap-film-layout", status=400,
                lint_errors=[{"rule": "dg/must-wrap-film-layout", "line": 1, "column": 0,
                              "msg": "顶层 JSX 必须被 <FilmLayout> 包住"}])
        return {"ok": True, "cache_key": cache_key, "artifact": f"/artifact/{cache_key}",
                "composition": {"id": "shot"}, "probed": {}, "bytes": 1234,
                "tokens": self.tokens, "warnings": ["夹具提醒"],
                "timings": {"bundle_ms": 10, "render_ms": 20, "per_frame_ms": 1.0}}

    def fetch(self, artifact: str, dest: Path) -> int:
        props = self._props_for(artifact)
        _synthesize(props, dest, mode=self.mode)
        return dest.stat().st_size

    def _props_for(self, artifact: str) -> dict:
        key = artifact.rsplit("/", 1)[-1]
        for c in self.calls:
            if c["cache_key"] == key:
                return c["props"]
        raise AssertionError(f"fetch 要了没渲过的产物 {artifact}（一次 run 里不该出现）")


class SeqTokens(FakeService):
    """按调用次序发不同的母版 token —— 造闸⑤ 的跨镜漂移。"""

    def __init__(self, seq: list[dict], **kw) -> None:
        super().__init__(**kw)
        self.seq = seq

    def render(self, code, cache_key, input_props, declared_fields=None, render=None):
        r = FakeService.render(self, code, cache_key, input_props,
                               declared_fields, render)
        r["tokens"] = self.seq[min(len(self.calls) - 1, len(self.seq) - 1)]
        return r


class FakeCache:
    """legacy.ShotCache 的替身：cache_key → 四个文件。

    `break_meta` 指名一个账字段，取回时把它抹掉 —— 用来验「账缺一项当未命中」这条
    纪律，而不是让下游 `rec["empty_ratio"]` 当场 KeyError。
    """

    def __init__(self, *, break_meta: str | None = None) -> None:
        self.blobs: dict[str, dict[str, bytes]] = {}
        self.break_meta = break_meta

    async def fetch(self, key: str, dst_dir: Path) -> dict[str, Path] | None:
        blob = self.blobs.get(key)
        if not blob:
            return None
        dst_dir.mkdir(parents=True, exist_ok=True)
        out: dict[str, Path] = {}
        for name, data in blob.items():
            p = dst_dir / f"cached_{name}"
            p.write_bytes(data)
            out[name] = p
        if self.break_meta and "meta.json" in out:
            meta = json.loads(out["meta.json"].read_text(encoding="utf-8"))
            meta.pop(self.break_meta, None)
            out["meta.json"].write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
        return out

    async def store(self, key: str, src: dict[str, Path]) -> None:
        self.blobs[key] = {name: p.read_bytes() for name, p in src.items()}


def _old_1x1_area_reading(clip: Path, at: float, *,
                          size: tuple[int, int] = (W, H)) -> float:
    """闸④ 用过的旧口径：二值图 `scale=1:1:flags=area` 落 1 字节，除以 255 当占比。

    留着只为把**红打在屏幕上**而不是写在注释里：真机 s02（roughjs 斜纹圆）逐字节数是
    4.26%，这条口径报 0.00%，一份画了东西的片子被判成空画面。

    `size` 跟新口径同一份：两侧必须在同一块画布上比，写成模块级 240×320 就成了
    「小块底色对大块画面」，两个数都不再是那条片子上的数。
    """
    w, h = int(size[0]), int(size[1])
    out = clip.with_suffix(".old.raw")
    mediaops.ffmpeg("-v", "error", "-ss", f"{at:.3f}", "-i", str(clip),
                    "-f", "lavfi", "-i", f"color=c={BG0X}:s={w}x{h}:r=1",
                    "-filter_complex",
                    "[0:v]format=rgb24,setsar=1[v];[1:v]format=rgb24,setsar=1[b];"
                    "[v][b]blend=all_mode=difference,format=gray,"
                    "geq=lum='255*gt(lum(X,Y),24)',scale=1:1:flags=area[out]",
                    "-map", "[out]", "-frames:v", "1", "-f", "rawvideo",
                    "-pix_fmt", "gray", "-y", str(out), timeout=120)
    return out.read_bytes()[0] / 255.0


async def _await_run(spec: dict, *, work: Path, client, cache=None):
    """跑一次整链，返回 (成片结果或 None, 异常或 None) —— 红的现场要能拿在手里断言。"""
    try:
        return await cr.render_code_motion(spec, providers=None, work=work,
                                           client=client, cache=cache), None
    except Exception as exc:                        # noqa: BLE001 - 用例就是要看异常
        return None, exc


def main() -> int:
    async def run() -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

        # =============================================================
        print("\n〔闸①〕lint 拒绝：后面的镜一帧都不烧")
        svc = FakeService(mode="lint")
        out, err = await _await_run(_spec([_shot(1), _shot(2)]),
                                    work=WORK / "g1", client=svc)
        check(isinstance(err, rc.RemotionError),
              f"lint 拒绝原样抛上来、不降级：{type(err).__name__}")
        check(bool(getattr(err, "lint_errors", [])) and
              err.lint_errors[0]["rule"] == "dg/must-wrap-film-layout",
              f"错误里带着规则名：{getattr(err, 'lint_errors', None)}")
        # 并发烧镜（RENDER_PARALLEL 路）时，红镜之前已在飞的那几镜照常烧完；
        # 还没开始的一镜都不再提交。
        check(len(svc.calls) <= cr.RENDER_PARALLEL,
              f"第一镜就被拒，没开始的镜不再提交（提交次数 {len(svc.calls)} ≤ "
              f"并发 {cr.RENDER_PARALLEL}）")
        check(not (WORK / "g1" / "final.mp4").exists(), "母带没开始拼")
        check(not (WORK / "g1" / "shot00" / "clip.mp4").exists(), "拒绝的那一镜没落带声切片")

        print("\n〔闸①〕绿：合规代码走到渲染，props 里带着闸② 注入的帧预算")
        svc = FakeService(mode="ok")
        out, err = await _await_run(_spec([_shot(1)]), work=WORK / "g1ok", client=svc)
        check(err is None, f"绿样例一路到底：{err}")
        p = svc.calls[0]["props"]
        planned = out["shots"][0]["planned_frames"]
        check(int(p["durationInFrames"]) == planned > 0,
              f"durationInFrames 由 Python 注入且 = 账上的计划帧：{p['durationInFrames']}")
        check(p["width"] == W and p["height"] == H and p["aspect"] == "portrait",
              "画幅与尺寸由 spec 注入，镜头代码没有这个权力")
        check(out["shots"][0]["chain"] == "remotion", "逐镜账认得出这是代码镜链")
        check(out["shots"][0]["lint_warnings"],
              f"服务端的 lint 提醒留在账上而不是丢掉：{out['shots'][0]['lint_warnings']}")

        # =============================================================
        print("\n〔闸②〕帧预算：到顶那一镜不提交，也不静默砍")
        svc = FakeService(mode="ok")
        budget = cr.rpipe.MAX_FRAMES_TOTAL
        cr.rpipe.MAX_FRAMES_TOTAL = 60      # 判据与预算大小无关，压小它用例才跑得起来
        try:
            out, err = await _await_run(_spec([_shot(i) for i in (1, 2, 3)]),
                                        work=WORK / "g2", client=svc)
        finally:
            cr.rpipe.MAX_FRAMES_TOTAL = budget
        check(isinstance(err, MediaError) and "闸②" in str(err),
              f"预算用尽抛闸②：{str(err)[:90]}")
        check("不静默砍镜" in str(err), "错误文本写明不砍镜、不截帧")
        check(len(svc.calls) == 1, f"超预算的那一镜没提交（提交次数 {len(svc.calls)}）")
        check(not (WORK / "g2" / "final.mp4").exists(), "预算红了 → 母带没开始")

        # =============================================================
        print("\n〔闸③〕渲完回读对账：少 3 帧 / 尺寸不对，都不截不补")
        for mode, needle in (("short", "帧"), ("wrongsize", "尺寸")):
            svc = FakeService(mode=mode)
            _, err = await _await_run(_spec([_shot(1)]), work=WORK / f"g3{mode}", client=svc)
            check(isinstance(err, MediaError) and "闸③" in str(err) and needle in str(err),
                  f"{mode} → 闸③ 报「{needle}」：{str(err)[:120]}")
            check(not (WORK / f"g3{mode}" / "final.mp4").exists(),
                  f"{mode} 红了 → 切片没进母带")
        info = mediaops.probe(WORK / "g1ok" / "shot00" / "silent.mp4")
        try:
            cr._check_frame_account("s01", planned_frames=info["frames"], fps=FPS,
                                    info=info, expected_size=(W, H))
            quiet = None
        except Exception as exc:                    # noqa: BLE001
            quiet = exc
        check(quiet is None, f"读数与计划一致 → 不抛（实测 {info['frames']} 帧）")

        # =============================================================
        print("\n〔闸④〕画面判两半：整帧底色要红，细线条不许误判，挤成一团也要拦")
        svc = FakeService(mode="flat")
        _, err = await _await_run(_spec([_shot(1)]), work=WORK / "g4flat", client=svc)
        check(isinstance(err, MediaError) and "闸④" in str(err),
              f"什么都没画 → 闸④ 拦住：{str(err)[:110]}")
        check("0.00%" in str(err), f"读数打出来给人看：{str(err)[:70]}")

        out_block, err_block = await _await_run(_spec([_shot(1)]), work=WORK / "g4block",
                                                client=FakeService(mode="block"))
        r_block = out_block["empty_ratios"]["s01"]
        check(err_block is None and r_block > cr.EMPTY_MIN_RATIO,
              f"画了一大块 → 过闸（非底色 {r_block:.1%}）")

        # 夹具按真机 s02 的形状造：1080×1920 上间距 96px 的 1px 细网格。
        # 逐字节数出来的非底色占比是 **4.26%** —— 与真机那片 roughjs 斜纹圆同一个数，
        # 因为那条片子的红就是在这里被判成 0.00% 的（`.smoke/lockfix/s02_frame.png`）。
        thin = WORK / "g4thin.mp4"
        _synthesize({"durationInFrames": 20, "width": 1080, "height": 1920, "fps": 10},
                    thin, mode="grid")
        r_new, span_new = cr._frame_diff_stats(thin, 1.0, bg_hex=BG, size=(1080, 1920),
                                               out=WORK / "g4thin.raw")
        r_old = _old_1x1_area_reading(thin, 1.0, size=(1080, 1920))
        check(abs(r_new - 0.0426) < 0.01 and r_new > cr.EMPTY_MIN_RATIO,
              f"细网格逐字节数出来 {r_new:.2%}（>2% 线）——画了东西")
        check(r_old < cr.EMPTY_MIN_RATIO,
              f"同一片细网格，旧口径（scale=1:1 area）读成 {r_old:.2%}："
              "假阳性在这一刻打出来，不是写在注释里")
        check(span_new >= cr.SPAN_MIN,
              f"同一份字节上的第二半读数：内容铺满了 {span_new:.0%} 带短边")

        out_grid, err_grid = await _await_run(_spec([_shot(1)]), work=WORK / "g4grid",
                                              client=FakeService(mode="grid"))
        r_grid = out_grid["empty_ratios"]["s01"]
        check(err_grid is None and cr.EMPTY_MIN_RATIO < r_grid < 0.20,
              f"这一片细网格过闸（非底色 {r_grid:.2%}）——旧口径在这里会把好片判空")
        check(out_grid["shots"][0]["empty_ratio"] == r_grid,
              "闸④ 的读数进账（P4 定 EMPTY_MIN_RATIO 终值要的是这些数字的分布）")
        check(out_grid["shots"][0]["fill_span"] == out_grid["fill_spans"]["s01"],
              f"铺满度同样进账、并进整片汇总：{out_grid['shots'][0]['fill_span']}")

        # **更细的一档**：400px 间距的 1px 网格，占比掉到 2% 线以下（这块画布上实测 1.07%），
        # 而它铺满了整条带子。只看像素占比的旧口径会把**手绘斜纹与公式字形**这一族
        # （实测整镜 0.99% / 1.54%）一律判成空画面——那正是这条链相对旧卡片链的价值。
        hair = WORK / "g4hair.mp4"
        _synthesize({"durationInFrames": 20, "width": 1080, "height": 1920, "fps": 10},
                    hair, mode="hairline")
        r_hair, span_hair = cr._frame_diff_stats(hair, 1.0, bg_hex=BG, size=(1080, 1920),
                                                 out=WORK / "g4hair.raw")
        check(r_hair < cr.EMPTY_MIN_RATIO,
              f"这一片的占比 {r_hair:.2%} 已经低于 {cr.EMPTY_MIN_RATIO:.0%} 线——"
              "单看占比必须打回（旧口径就是这么判死细线条的，把红留在这里）")
        check(span_hair >= cr.SPAN_MIN,
              f"但它铺满了 {span_hair:.0%} 带短边 → 两半合起来判的这一半救得回来")
        out_hair, err_hair = await _await_run(_spec([_shot(1)]), work=WORK / "g4hair",
                                              client=FakeService(mode="hairline"))
        check(err_hair is None, f"整链跑通：细线条画法不再是空画面（{str(err_hair)[:80]}）")
        check(out_hair and out_hair["shots"][0]["empty_ratio"] < cr.EMPTY_MIN_RATIO,
              f"账上留着那个「低于闸值但合法」的读数：{out_hair and out_hair['shots'][0]['empty_ratio']}")

        # **小气**：一大块实心颜色挤在中间，像素占比 6% 上下（远高于 2% 线），
        # 外接盒却只够到带短边的一小截。当年那批横屏镜就是这么漏过去的。
        _, err_tiny = await _await_run(_spec([_shot(1)]), work=WORK / "g4tiny",
                                       client=FakeService(mode="tiny"))
        check(isinstance(err_tiny, MediaError) and "挤成一团" in str(err_tiny),
              f"占比够但铺不开 → 闸④ 小气那半拦住：{str(err_tiny)[:120]}")
        check(f"{cr.TIGHT_MIN * 100:.0f}%" in str(err_tiny),
              "退回原因带着闸值与实测铺满度，不是只说「小」")
        check(not (WORK / "g4tiny" / "final.mp4").exists(), "小气红了母带一帧不拼")

        # 分母得是**带子**而不是整帧：同一块居中的红，裁到它自己那条带子上是铺满，
        # 按整帧量就成了小气。母版没回读几何（旧 film-layout@1）时退回整帧，
        # 判空仍在跑、只是分母偏大——这条也要能指认。
        band_tokens = {"bg": BG, "fontSize": 64, "bodyWidth": 936, "bodyHeight": 1524,
                       "gutter": 72, "topSafe": 96}
        check(cr._band_of(band_tokens, (1080, 1920)) == (72, 96, 936, 1524),
              "画面带取的是母版回读的那四个数，Python 侧不另算一套几何")
        check(cr._band_of({"bg": BG}, (1080, 1920)) is None,
              "母版没报几何 → 退回整帧口径（判空照跑，不误杀）")
        r_tiny_full, span_tiny_full = cr._frame_diff_stats(
            WORK / "g4tiny" / "shot00" / "silent.mp4", 0.5, bg_hex=BG, size=(W, H),
            out=WORK / "g4tiny_full.raw")
        r_tiny_band, span_tiny_band = cr._frame_diff_stats(
            WORK / "g4tiny" / "shot00" / "silent.mp4", 0.5, bg_hex=BG, size=(W, H),
            out=WORK / "g4tiny_band.raw",
            band=(3 * W // 8, 3 * H // 8, W // 4, H // 4))   # 与夹具那块红同一组坐标
        check(r_tiny_full > cr.EMPTY_MIN_RATIO and span_tiny_full < cr.TIGHT_MIN,
              f"整帧分母：占比 {r_tiny_full:.1%} 过线、铺满 {span_tiny_full:.0%} 不过线")
        check(abs(r_tiny_band - 1.0) < 0.02 and span_tiny_band > 0.9,
              f"同一帧裁到那块红自己的带子：占比 {r_tiny_band:.0%}、铺满 "
              f"{span_tiny_band:.0%}——分母换了，读数就换了（横屏同理）")

        # 底色三级退让（母版下线那一轮改的）：回读 → 这一镜申报的 bg → `DEFAULT_BG`
        # （= `TOKENS.bg`）。为什么不再抛「拿不到母版底色」：闸④ 判的是像素，基准色拿不到
        # 就把整镜判成「渲不出来」，落在自包含镜（**每一镜**都没有回读，而那是这一轮要放开
        # 的写法）身上就是整片渲不出来。基准色取错了的后果是读数歪——那是闸自己的红绿，
        # 不该和「链路断了」共用一个出口。
        check(cr._bg_of({"fontSize": 64}) == cr.DEFAULT_BG,
              f"母版没回读 bg、这一镜也没申报 → 退到 DEFAULT_BG"
              f"（{cr.DEFAULT_BG}），不拿猜色、也不打回")
        check(cr._bg_of({"fontSize": 64}, {"bg": "#0b1620"}) == "#0b1620",
              "回读不到时退到这一镜申报的 bg（深底镜的判空基准色才对得上）")
        check(cr._bg_of({"bg": "#123456"}, {"bg": "#0b1620"}) == "#123456",
              "有回读时以回读为准（申报值只是退路）")
        _, err = await _await_run(_spec([_shot(1)]), work=WORK / "g4nobg",
                                  client=FakeService(mode="ok", tokens={"fontSize": 64}))
        check(err is None or "拿不到母版底色" not in str(err),
              f"缺底色不是一条「渲不出来」：{str(err)[:90]}")

        # =============================================================
        print("\n〔闸⑤〕跨镜一致性：容差 0，红了就不拼母带")
        drift = cr.check_token_drift([{"id": "s01", "tokens": {"bg": BG, "fontSize": 64}},
                                      {"id": "s02", "tokens": {"bg": BG, "fontSize": 66}}])
        check(len(drift) == 1 and "fontSize" in drift[0] and "s02" in drift[0],
              f"字号漂 2 → 报键名和两镜号：{drift}")
        drift2 = cr.check_token_drift([{"id": "s01", "tokens": {"bg": BG}},
                                       {"id": "s02", "tokens": {}}])
        check(any("没读到母版 token" in e for e in drift2),
              f"某镜回读落空不静默跳过：{drift2}")
        check(cr.check_token_drift([{"id": "s01", "tokens": {"bg": BG}},
                                    {"id": "s02", "tokens": {"bg": BG}}]) == [],
              "逐键相同 → 绿")
        # 满幅镜的带子＝整帧：混排 bleed 是契约允许的逐镜形状，几何按 bleed 分桶比
        mixed = cr.check_token_drift([
            {"id": "s01", "tokens": {"bg": BG, "bleed": False,
                                     "bodyWidth": 936, "bodyHeight": 1524}},
            {"id": "s02", "tokens": {"bg": BG, "bleed": True,
                                     "bodyWidth": 1080, "bodyHeight": 1920}}])
        check(mixed == [], f"bleed 不同 → 几何不同不算母版漂移：{mixed}")
        same_bucket = cr.check_token_drift([
            {"id": "s01", "tokens": {"bg": BG, "bleed": False, "bodyWidth": 936}},
            {"id": "s02", "tokens": {"bg": BG, "bleed": False, "bodyWidth": 900}}])
        check(len(same_bucket) == 1 and "bodyWidth" in same_bucket[0],
              f"同为 bleed=false 而 bodyWidth 不同 → 真漂移，仍要红：{same_bucket}")
        svc = SeqTokens([{"bg": BG, "fontSize": 64}, {"bg": BG, "fontSize": 50}], mode="ok")
        _, err = await _await_run(_spec([_shot(1), _shot(2)]), work=WORK / "g5", client=svc)
        check(isinstance(err, MediaError) and "闸⑤" in str(err),
              f"跨镜漂 → 闸⑤：{str(err)[:120]}")
        check(not (WORK / "g5" / "final.mp4").exists(), "闸⑤ 红了母带一帧不拼")
        check(len(svc.calls) == 2, "两镜都渲完才比对（比对是跨镜的，拦不了单镜）")

        # =============================================================
        print("\n〔缓存〕命中还原 / 同输入同键；账缺一项当未命中，不许 KeyError")
        cache = FakeCache()
        svc = FakeService(mode="ok")
        spec_c = _spec([_shot(1)])
        first, _ = await _await_run(spec_c, work=WORK / "c1", client=svc, cache=cache)
        check(len(svc.calls) == 1 and first["cached_shots"] == 0, "首跑真烧了一镜")
        check(set(first["shot_files"]["s01"]) == {"clip", "frame"},
              f"新烧的那一镜交得出 clip 与 frame：{sorted(first['shot_files']['s01'])}")
        # 真机上这两条同时红过：代表帧那一步的 ffmpeg **退 0 且不写文件**（seek 落在最后
        # 一帧之后），frame.png 静默缺失 → 下面 cache.store 读它抛 FileNotFoundError →
        # 被 except 吞成账上一句 cache_stored → 缓存永不命中。红要能指认是哪一环。
        check("cache_stored" not in first["shots"][0],
              f"缓存确实存上了（没存上才会留一句：{first['shots'][0].get('cache_stored')}）")
        fp = Path(first["shot_files"]["s01"]["frame"])
        check(fp.exists() and fp.stat().st_size > 0,
              f"代表帧在盘上且非空（缓存与缩略图都读它）：{fp.name} "
              f"{fp.stat().st_size if fp.exists() else 0}B")
        second, _ = await _await_run(spec_c, work=WORK / "c2", client=svc, cache=cache)
        check(second["cached_shots"] == 1 and len(svc.calls) == 1,
              f"同输入第二次不提交渲染（提交次数 {len(svc.calls)}）")
        check(first["shots"][0]["cache_key"] == second["shots"][0]["cache_key"],
              "同输入同缓存键（按字节算的键按字节断言）")
        check(set(second["shot_files"]["s01"]) == {"clip", "frame"},
              f"命中分支也交得出 clip 与 frame：{sorted(second['shot_files']['s01'])}")
        check(second["shots"][0]["empty_ratio"] == first["shots"][0]["empty_ratio"],
              "命中时账上的闸④ 读数从 meta 还原，不是重渲")
        check(second["shots"][0]["fill_span"] == first["shots"][0]["fill_span"],
              f"铺满度同样还原：{first['shots'][0]['fill_span']}（不还原的话，"
              "复用切片的那几镜上的填充提醒会静默消失）")
        # 两个读数各验一遍「账里少一项」：META_FIELDS 每加一个字段都靠这条纪律兜住，
        # 少一项当未命中，而不是让下游 `rec["fill_span"]` 当场 KeyError。
        for missing in ("empty_ratio", "fill_span"):
            broken = FakeCache(break_meta=missing)
            await _await_run(spec_c, work=WORK / f"c3{missing}",
                             client=FakeService(mode="ok"), cache=broken)
            got, err = await _await_run(spec_c, work=WORK / f"c4{missing}",
                                        client=FakeService(mode="ok"), cache=broken)
            check(err is None and got["cached_shots"] == 0,
                  f"meta 里少 {missing} → 当未命中重渲，不 KeyError：{err}")

        # =============================================================
        print("\n〔成片〕三镜连排：拼接 / 混音 / 落盘一路到底")
        svc = FakeService(mode="ok")
        spec3 = _spec([_shot(i) for i in (1, 2, 3)])
        film, _ = await _await_run(spec3, work=WORK / "film", client=svc)
        info = mediaops.probe(film["video"])
        check(Path(film["video"]).exists() and info["duration"] > 6.0,
              f"成片落盘且时长对得上：{info['duration']:.2f}s / {len(svc.calls)} 镜")
        check({r["chain"] for r in film["shots"]} == {"remotion"}, "每镜的账都标了链")
        check(all(r["empty_ratio"] > cr.EMPTY_MIN_RATIO for r in film["shots"]),
              f"每镜非底色都过线：{[r['empty_ratio'] for r in film['shots']]}")
        check(len(film["shot_files"]) == 3 and
              all(Path(v["frame"]).exists() for v in film["shot_files"].values()),
              "每镜都留了代表帧（P3 的对比帧读它）")

        # =============================================================
        print("\n〔报告层〕闸④ 两半 + 闸⑦ 配额：账要长成给人看的文字")
        # 闸④/闸⑦ 的**判**在执行层与契约层，这一节判的是另一件事：出片回执上那几行
        # 有没有把读数、闸值、通道分布如实报出来。报告层错一次的形状是「闸明明红了、
        # 回执却说绿」，或者「回执报了个契约里根本没有的数」——两种都不在渲染链上红。
        from storyline_server.nodes.motion_code_nodes import RenderCodeMotionVideoNode
        from storyline_server.settings import Settings
        from storyline_server.providers import Providers

        # 闸⑲ 读代码里那句 `const palette = palettes["…"]`——四支模板各绑一套，六镜
        # 因此摊到 4 套色板（≥ ceil(6/4)=2）。满幅与底色成对写在 s02/s05 上（≥2 镜）。
        # P1 注入头（`{getInputProps().text}<CaptionLayer />`）现在挂在每一支上：
        # 骨架因此越过 `_SHAPE_MIN_TOKENS=40` 的豁免线，闸⑫ 会开始比相似度——本文件
        # 没有任何「整片闸必须空账」的断言（判闸用的是 `check_draw_floor` 这类直调），
        # 所以这是接受的代价；谁日后要在这儿加空账断言，先把夹具收短再谈。
        CODE_RUGH = ('const palette = palettes["deep-ocean"];\n'
                     'import { roughCanvas } from "roughjs";\n'
                     'export const Shot = () => (<FilmLayout>{roughCanvas()}'
                     '{getInputProps().text}<CaptionLayer /></FilmLayout>);')
        CODE_SVG = ('const palette = palettes["warm-cream"];\n'
                    'export const Shot = () => (<FilmLayout><svg />'
                    '{getInputProps().text}<CaptionLayer /></FilmLayout>);')
        CODE_KTX = ('const palette = palettes["solar-pop"];\n'
                    'import katex from "katex";\n'
                    'export const Shot = () => (<FilmLayout>{katex.renderToString("x")}'
                    '{getInputProps().text}<CaptionLayer /></FilmLayout>);')
        CODE_TEXT = ('const palette = palettes["obsidian-violet"];\n'
                     "export const Shot = () => (<FilmLayout>字</FilmLayout>);")
        PLAIN = "斜纹填满的一片圆"          # 纯中文、无数字无拉丁 → 词法认不出具体项
        SPECIFIC = "1934 年量到的那个数"     # 有数字 → 有具体项

        # 闸⑮ 要求六镜铺开至少两个版式族、且同一族不超过两镜——这里按族循环取真卡。
        # 闸⑯ 要求「自报的通道必须有证据」：六镜各自填**与它代码里那条通道相同**的值。
        # 2026-10-16：s05/s06 原来是纯文字镜（`CODE_TEXT`）——闸⑦ 翻成「每镜必画」之后
        # 那种镜连契约都过不去，所以这里换成真画的两支。用过的三条通道都还在，
        # `variety` 仍是三种（换掉的是通道的**次数**，不是种类）。
        CARD6 = ("opening/brand-ink-open", "data/before-after-slider-scrub",
                 "effects/assemble-then-type-flyin", "typography/blur-slide",
                 "ui-entrance/avatar-bracket-carousel", "camera/basic-3d-scene")
        shots6 = [_shot(1, code=CODE_RUGH, text=SPECIFIC, card=CARD6[0],
                        subject="斜纹圆", subject_channel="rough"),
                  _shot(2, code=CODE_SVG, text=PLAIN, card=CARD6[1],
                        subject="那条对角线", subject_channel="vector",
                        bleed=True, bg="#0b1620"),
                  _shot(3, code=CODE_KTX, text=PLAIN, card=CARD6[2],
                        subject="公式块", subject_channel="katex"),
                  _shot(4, code=CODE_RUGH, text=PLAIN, card=CARD6[3],
                        subject="斜纹圆", subject_channel="rough"),
                  _shot(5, code=CODE_RUGH, text=PLAIN, card=CARD6[4],
                        subject="斜纹圆", subject_channel="rough",
                        bleed=True, bg="#f6f1e4"),
                  _shot(6, code=CODE_SVG, text=SPECIFIC, card=CARD6[5],
                        subject="那条对角线", subject_channel="vector")]
        spec6 = _spec(shots6)
        # 夹具自己先要对得上闸⑦ 的判据，否则下面断言的是夹具而不是报告层
        errs6, warns6 = cs.check_draw_floor(spec6["shots"])
        facts6 = cs.style_facts(spec6["shots"])
        check(errs6 == [], f"六镜夹具过闸⑦（打回清单：{errs6}）")
        check(facts6["text_only"] == [],
              f"六镜都画了东西（⑦ 的账是空数组，那个空就是证据）：{facts6['text_only']}")
        check(len(facts6["variety"]) == 3,
              f"通道三种：rough + vector + katex（{facts6['channels']}）")

        film6, err6 = await _await_run(spec6, work=WORK / "report6",
                                       client=FakeService(mode="ok"))
        check(err6 is None, f"六镜片一路渲到底：{err6}")
        node = RenderCodeMotionVideoNode(Settings(), Providers(), None)  # type: ignore[arg-type]
        rep = node._result(spec6, film6, target=None, low_high=(0.0, 0.0),
                           warnings=[], bgm_ref="", object_key="k/offline.mp4",
                           frames=None, frame_reason="离线用例不抽帧")

        def row(needle: str) -> dict:
            hit = [e for e in rep["evidence"] if needle in e["claim"]]
            check(bool(hit), f"回执里有「{needle}」这一行")
            return hit[0] if hit else {"claim": "", "status": "MISSING", "proof": ""}

        ratios6 = [float(r["empty_ratio"]) for r in film6["shots"]]
        spans6 = {str(r["id"]): float(r["fill_span"]) for r in film6["shots"]}
        r4 = row("闸④ 画面非空（两半）")
        check(f"{min(ratios6) * 100:.1f}%" in r4["claim"] and
              f"{min(spans6.values()) * 100:.0f}%" in r4["claim"],
              f"闸④ 这一行把**两个**读数都打出来了：{r4['claim'][:120]}")
        check(all(v in r4["claim"] for v in
                  (f"{cr.EMPTY_MIN_RATIO * 100:.0f}%", f"{cr.SPAN_MIN * 100:.0f}%",
                   f"{cr.TIGHT_MIN * 100:.0f}%", f"{int(cr.FILL_TARGET * 100)}%")),
              "四道闸值（2%/45%/30% 与只提醒的 60%）全在同一行里，读的人不用去翻常量")
        check("两半的分工" in r4["proof"] and "空壳" in r4["proof"] and "小气" in r4["proof"],
              "proof 说清了哪两半各拦什么——不然「非底色 1.4%」看着像该红的数")
        check(r4["status"] == "verified", f"两半都有本次数值才叫验过：{r4['status']}")

        r7 = row("闸⑦ 每镜必画")
        check("6/6 镜用代码画了图形" in r7["claim"],
              f"闸⑦ 报的是**逐镜**的缺口，不是整片的配额分布：{r7['claim'][:110]}")
        check("rough×" in r7["claim"] and "katex×1" in r7["claim"],
              f"逐通道计数摊平了（读数，不是判据）：{r7['claim']}")
        check("一条画法通道都没用上" not in r7["claim"],
              "一镜不缺就不点名——这一句只在真有镜没画时出现")
        check(r7["status"] == "verified", f"6/6 都画了 → 已验：{r7['status']}")
        check("import" in r7["proof"], "proof 写明判据是代码字节里的 import，不是模型自报")
        check("逐镜" in r7["proof"] and "preview 档也打回" in r7["proof"],
              "proof 说清这条是**逐镜**判、且默认质检档也拦（不是「建议」）")

        rr = row("逐镜提醒（不打回）")
        check(rr["status"] == "verified", "提醒这一行是账，不是待验主张")
        check("具体项" in rr["claim"], f"文案那半的提醒上了回执：{rr['claim'][:130]}")
        check(len(rep["style_warnings"]) >= 1 and
              all(w in rr["claim"] for w in rep["style_warnings"]),
              f"style_warnings 里每一条都在这一行里看得见，不是只进返回值：{rep['style_warnings']}")
        check(f"低于 {cs.CONCRETE_SHARE_TARGET:.0%}" in " ".join(rep["style_warnings"]),
              "提醒带着那条线本身（40%），而不是只说「太空」")

        expect_tight = sorted(sid for sid, v in spans6.items() if v < cr.FILL_TARGET)
        check(rep["fill_warnings"] and
              sorted(w.split("：")[0] for w in rep["fill_warnings"]) == expect_tight,
              f"铺满度低于目标的镜逐条点名（本次 {len(expect_tight)} 镜）："
              f"{rep['fill_warnings'][:2]}")
        check(all(f"{spans6[s.split('：')[0]] * 100:.0f}%" in s for s in rep["fill_warnings"]),
              "提醒里的百分比就是账上那份读数，不是重算的另一个数")
        check("没铺满画面带" in rr["claim"], "同一份 tight 也摊在证据行里")

        check(rep["style_channels"] == facts6["channels"],
              f"通道账与契约层同一份 style_facts：{rep['style_channels']}")
        check(rep["style_text_only"] == [],
              "**每镜必画**之下这一栏恒为空——非空即「有镜没画面」，那种片子走不到渲染："
              f"{rep['style_text_only']}")
        check(rep["fill_spans"] == film6["fill_spans"], "整片汇总把逐镜铺满度带回去（前端读它）")

        # 2026-10-16 闸⑦ 翻成「每镜必画」之后，**短片子也判**——原来那句「镜数没到最低档
        # 6，这一档不判配额」连同 MIN_SHOTS_FOR_QUOTA 的提前返回一起没了。这条用例
        # 正是逼着那次改动的钉子：留着旧的「未判」口径，这一步就红。
        # （MIN_SHOTS_FOR_QUOTA 常量仍在——闸⑮ 版式族那一条还用它，只是不再管 ⑦。）
        rep3 = node._result(spec3, film, target=None, low_high=(0.0, 0.0),
                            warnings=[], bgm_ref="", object_key="k/offline3.mp4",
                            frames=None, frame_reason="离线用例不抽帧")
        r3 = [e for e in rep3["evidence"] if "闸⑦" in e["claim"]][0]
        check(f"3/3 镜用代码画了图形" in r3["claim"],
              f"三镜片同样逐镜判（不再看镜数档位）：{r3['claim'][:90]}")
        check(r3["status"] == "verified",
              f"3/3 都画了 → 已验，不是「未判」：{r3['status']}")
        # ★ T28 已落地（§0.5-P5 / §6-T28）：未判从**提醒**升级成**阻断档**。
        #   判据是「三态分家」——这一条曾经是故意红着的（未判混在提醒里，
        #   于是回执上「没有量过」与「量过、只是不达标」长得一模一样）。
        #   现在：提醒清单里不许出现未判的那句话，未判单列一栏、且回执上是一条
        #   status=UNVERIFIED 的阻断档。通道账照旧逐镜摊着（不谎报）。
        #   2026-10-16：这一段举的「未判」例子原本是闸⑦ 的镜数档位，那条已经不存在；
        #   下面几条断言的未判栏现在**只剩字色对比度那一条**（与闸⑦ 无关）。
        # 2026-10-16：这一条原来断言 `style_warnings == []`——那在旧口径下成立，
        # 因为闸⑦ 对三镜片**整条提前返回**，连「文案具体项」这条正交的**提醒**
        # 也一并被吞掉。现在 ⑦ 逐镜判、不再有镜数提前返回，于是那份只提醒不拦的
        # 文案读数（`CONCRETE_SHARE_TARGET`）对短片照样出声——这是**顺带的实话**，
        # 不是本用例要守的东西。本用例守的是 T28 的那一条：**未判不许混进提醒**。
        # 所以判据从「清单为空」改成「清单里没有未判那一句」。
        check(all("未判" not in w for w in rep3["style_warnings"])
              and len(rep3["style_channels"]) == 3,
              f"未判档不混进提醒（提醒里可以有别的读数，但不许把「没量过」说成提醒）；"
              f"通道账照样逐镜摊着（不谎报）：{rep3['style_channels']}")
        check(len(rep3["style_unjudged"]) == 1 and "未判" in rep3["style_unjudged"][0],
              f"未判单列一栏，且那句话里明写「未判」：{rep3['style_unjudged']}")
        unj = [e for e in rep3["evidence"] if "未判档（阻断" in e["claim"]]
        check(len(unj) == 1 and unj[0]["status"] == "UNVERIFIED"
              and "不许折算成通过" in unj[0]["claim"],
              f"回执上是一条阻断档（UNVERIFIED），不是一句提醒："
              f"{unj[0]['status'] if unj else 'MISSING'}")
        # 反向：真量得出来的那一支不许出现未判栏——**两侧夹住**，否则这个字段就是
        # 一句永远挂着的字（判据在两侧都会被证明是活的）。
        INKED = ('export const Shot = () => (<FilmLayout>'
                 '<div style={{background:"#101014"}}>'
                 '<span style={{color:"#ffffff"}}>字</span></div></FilmLayout>);')
        check(cs.check_ink_contrast([_shot(1, code=INKED)])[2] == [],
              "底与字都是字面颜色 → 量得出来，未判栏为空")
        check(len(cs.check_ink_contrast([_shot(1)])[2]) == 1,
              "认不出颜色的那一支 → 未判栏恰好一条（这一栏不是常量）")

        # =============================================================
        print("\n〔出处对账〕分镜入库与 inline 旁路走同一道：凭据只有本会话的检索账")
        # 闸①~⑥ 判的是「这一镜渲出来对不对」，出处对账判的是「这一镜那句凭据查过没有」。
        # 后者一度只挂在声明式的分镜节点上，而代码镜每镜照样带 source、照样把那几个字
        # 如实排进分镜卡与逐镜账——于是「凭模型记忆写的数字」在这条链上能一路出片。
        # 2026-10-19 起对不上账就地降级成「未核实：…」：同一句账仍在，但不再把整份分镜
        # 打回去重交（真机上模型为此连抓 23 页、重交 4 次，十分钟卡在分镜这一步）。
        from agent_framework import retrieval
        from agent_framework import secrets as af_secrets
        from agent_framework.identity import use_identity
        from agent_framework.orchestration import NodeState
        from agent_framework.storage import build_storage
        from storyline_server.nodes.motion_code_nodes import RenderCodeMotionVideoNode
        from storyline_server.nodes.motion_plan import PlanMotionNode
        from storyline_server.providers import Providers
        from storyline_server.settings import Settings

        def _code(shots: list[dict]) -> dict:
            return {"title": "夹具", "aspect": "portrait", "fps": FPS, "narration": False,
                    "caption_style": "on_picture", "texture": "none", "shots": shots}

        gate_state = NodeState(session_id="u:cg-u:c:cg-c")
        # 分镜与出处两道闸都不碰 storage、不烧像素，离线直调给 None
        plan_node = PlanMotionNode(Settings(), Providers(), None)      # type: ignore[arg-type]

        async def plan_err(inputs: dict) -> str:
            try:
                await plan_node.process(gate_state, inputs)
            except ValueError as e:
                return str(e)
            return ""

        async def plan_ok(inputs: dict) -> dict:
            return await plan_node.process(gate_state, inputs)

        cited = [_shot(1, source={"标题": "Neutron star",
                                  "链接": "https://en.wikipedia.org/wiki/Neutron_star"}),
                 _shot(2)]

        af_secrets.unbind_storage()      # 账本压根不存在（没装配存储的进程）
        check(await retrieval.ledger("u:cg-u:c:cg-c") is None,
              "没绑存储时 ledger 回 None：是「没有账本」，不是「一条都没查过」")
        check(await plan_err({"spec": _code(cited)}) == "",
              "没有账本时整体放行——拿「没记过」定罪会退化成所有出处一律标未核实")

        g7 = build_storage("memory")
        await g7.start()                 # Storage.start() 里 bind_storage，两边同一条路径
        try:
            check(await retrieval.ledger("u:cg-u:c:cg-c") == [],
                  "空账回 []（与 None 是两个意思：这里确实一次都没查到过）")
            matched = await plan_ok({"spec": _code(cited)})
            check([r["source_gate"] for r in matched["plan_table"]][0]
                  == "declared_unverified",
                  "冒充查过的出处**就地降级**成未核实，分镜照常入库："
                  f"{[r['source_gate'] for r in matched['plan_table']]}")
            check(str(matched["plan_table"][0].get("source") or "").startswith("未核实"),
                  "分镜卡上读到的就是「未核实：…」，不是原来那句像查过的标题")
            notes = " ".join(matched.get("autofix_notes") or [])
            check("s01" in notes and "未核实" in notes and "检索账" in notes,
                  f"降级这件事带镜号写进回执，模型看得到（{notes[:60]}）")

            honest = [_shot(1, source={"标题": "未核实：模型记忆里的数量级"}), _shot(2)]
            got = await plan_ok({"spec": _code(honest)})
            check(got["chain"] == "remotion" and "render_motion_code" in got["next"],
                  f"代码分镜的账指回代码链出片节点：{got['next'][:40]}")
            check([r["source_gate"] for r in got["plan_table"]]
                  == ["declared_unverified", "no_source"],
                  f"自标未核实与没填出处都入库，但如实分开标：{[r['source_gate'] for r in got['plan_table']]}")
            check(all("code_chars" in r and "card" not in r for r in got["plan_table"]),
                  "逐镜行是代码链的形状（报代码字节数，没有 card 那一栏）")
            check([r["texture"] for r in got["plan_table"]] == ["none", "none"],
                  f"分镜账逐镜带**实际生效**的质感档（整片没加、逐镜没点）："
                  f"{[r['texture'] for r in got['plan_table']]}")
            over = await plan_ok({"spec": _code([_shot(1, texture="tv"), _shot(2)])})
            check([r["texture"] for r in over["plan_table"]] == ["tv", "none"],
                  f"一镜点名换档、另一镜跟随整片——账上分开记：{[r['texture'] for r in over['plan_table']]}")

            with use_identity("cg-u", "cg-c"):
                await retrieval.note_hit("https://EN.wikipedia.org/wiki/Neutron_star/",
                                         "Neutron star", backend="wikipedia")
            got2 = await plan_ok({"spec": _code(cited)})
            check([r["source_gate"] for r in got2["plan_table"]][0] == "verified"
                  and got2["plan_table"][0]["source"] == "Neutron star",
                  "URL 归一后对得上账（大小写与尾斜杠不该成为降级的理由），出处照进逐镜账")
            pulsar = await plan_ok({"spec": _code(
                [_shot(1, source={"链接": "https://en.wikipedia.org/wiki/Pulsar"}),
                 _shot(2)])})
            check([r["source_gate"] for r in pulsar["plan_table"]][0]
                  == "declared_unverified",
                  "同站不同条目不算查到过——降级到未核实，而不是前缀匹配放行")

            other = NodeState(session_id="u:cg-u:c:other-conv")
            cross = await plan_node.process(other, {"spec": _code(cited)})
            check([r["source_gate"] for r in cross["plan_table"]][0]
                  == "declared_unverified",
                  "换个会话就查不到那一笔——账是按会话计的，不是全局白名单")

            check("spec 必须是对象且非空" in await plan_err({}),
                  "没交分镜就拒——不许悄悄回落到 Store 里那份旧账")
            check("spec 必须是对象且非空" in await plan_err({"spec": "一句话说清楚"}),
                  "形状不是对象也拒，不猜字符串背后想写什么")
            # 真校验错（画幅以外的档名）仍排在对账前：一条错误清单就够。
            bad = await plan_err({"spec": {**_code(
                [_shot(1, source={"标题": "某本书"}), _shot(2)]),
                "caption_style": "bottom"}})
            check("代码分镜校验未通过" in bad,
                  f"分镜形状不合法时照旧打回，不被出处那一步掩盖（{bad[:60]}）")

            # inline 那条旁路：出片节点读的第一层来路是**入参里的 spec**，它盖在 Store
            # 那份上面，从未过分镜闸。今天这里补走的是同一个 settle_sources，不是第二份判据；
            # 对不上账的出处同样就地降级，提醒记进本次 run 的摘要。
            render_node = RenderCodeMotionVideoNode(Settings(), Providers(), g7)
            render_node.remotion_client = FakeService(mode="ok")

            inline_state = NodeState(session_id="u:cg-u:c:cg-c")

            async def render_run(spec: dict) -> str:
                try:
                    await render_node.process(inline_state, {"spec": spec, "wait_sec": 0})
                except Exception as e:      # noqa: BLE001 - 这里判的是对账有没有拦住出片
                    return f"{type(e).__name__}: {e}"
                return ""

            bypass = await render_run(_code([
                _shot(1, source={"标题": "某个没查过的来源",
                                 "链接": "https://nope.example/x"}), _shot(2)]))
            check("出处闸" not in bypass,
                  f"绕过 plan_motion 直接塞 inline 分镜，不因出处被拦住（下游：{bypass[:70]}）")
            check(any("s01" in n and "未核实" in n for n in inline_state.summary.notes),
                  "inline 那一路也走了同一个对账：降级提醒进了本次 run 的摘要")
            passed = await render_run(_code(honest))
            check("出处" not in passed,
                  f"自标未核实照走，不被当成错误（下游：{passed[:50]}）")
        finally:
            af_secrets.unbind_storage()

    asyncio.run(run())
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
