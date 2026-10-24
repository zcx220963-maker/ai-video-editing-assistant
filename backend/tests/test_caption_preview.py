"""字幕样张（`storyline_server/motion/caption_preview.py`）——预览就是成片那一条链。

用户的诉求原话是「字幕类型也要支持预览啊，不然渲染完才知道什么样」。这一节守的正是
那句「预览＝成片」，而不是「画得像」：

1. **一对一对地渲，不预渲整张网**——2026-10-16 起字幕成了两个正交维度
   （字幕样式 7 × 高亮样式 5），35 种组合预渲一遍是 35 张静帧；用户看的是哪一对就渲
   哪一对。判据落在 `preview_job` 上：**两个维度都真的进了注入**，只带一个就是
   「选高亮样式没反应」——而那正是面板重做要治的毛病。
2. **词时间轴必须在**——母版那侧的 `charStartFrames` 没有 `words` 就返回 null，
   逐词点亮会退化成「整句一直亮」，于是 `karaoke` 那一档与前几档长得一样。样张少这一栏
   不会报任何错，只会骗人。
3. **色板是入参、不猜**——认不出的色板名回空表按默认渲，而不是拿一套不相干的配色冒充。
4. **缓存按内容寻址**——母版变了、色板换了、换了一对样式都换键；同一对第二次要样张
   不再打渲染容器（打包一次好几秒，每开一次面板都重渲是无法接受的）。

运行：python tests/test_caption_preview.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import build_storage
from storyline_server.motion import caption_preview as cp
from storyline_server.motion import code_spec as cs

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


#: 一张 1×1 的 JPEG（样张那条链只关心「字节有没有落进库里」，不解码）。
_JPEG = base64.b64encode(bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffc2"
    "001108000100010103011100021101031101ffc4001f00000105"
    "01010101010100000000000000000102030405060708090a0bffc4"
    "00b5100002010303020403050504040000017d01020300041105"
    "122131410613516107227114328191a1082342b1c11552d1f024"
    "33627282090a161718191a25262728292a3435363738393a4344"
    "45464748494a535455565758595a636465666768696a73747576"
    "7778797a838485868788898a92939495969798999aa2a3a4a5a6"
    "a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4"
    "d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9"
    "faffda0008010100003f00fbfeffd9")).decode()


class FakeClient:
    """渲染容器的替身：记下被问了几次、被要求渲了哪一帧。"""

    def __init__(self, *, ok: bool = True, master: str = "deadbeefcafe1234",
                 empty: bool = False) -> None:
        self.ok, self.master, self.empty = ok, master, empty
        self.calls: list[list[int]] = []
        # 每一次真发出去的注入：判「强调色那一栏真的发到了渲染容器」要比判键更难伪造——
        # 键只说明它被算进了地址，props 才说明它进了那一帧。
        self.props: list[dict] = []

    def health(self):
        if not self.ok:
            raise ConnectionError("渲染服务没起来")
        return {"ok": True, "master_hash": self.master}

    def stills(self, code, input_props, frames, scale=0.4):
        self.calls.append(list(frames))
        self.props.append(dict(input_props))
        if self.empty:
            return []
        return [{"frame": f, "jpeg_b64": _JPEG} for f in frames]


def _client(fake: FakeClient) -> dict:
    """`render_one` 收的就是 `remotion_client.endpoints()` 那张表：两个可调用。"""
    return {"health": fake.health, "stills": fake.stills}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="caption_preview_"))
    storage = build_storage("memory", cache_root=tmp / "cache",
                            workspace_root=tmp / "ws")

    # ---------- ① 词时间轴：没有它，逐词点亮看不出与整句亮着的区别 ----------
    print("① 词时间轴覆盖全文且铺在时长里（逐词点亮靠它）")
    words = cp.sample_words()
    check("".join(w["text"] for w in words) == cp.SAMPLE_TEXT,
          "分段拼回来就是那句示范文案（漏字＝母版按顺序找不到词，高亮会错位）")
    starts = [w["startFrame"] for w in words]
    check(starts[0] == 0 and starts == sorted(starts) and len(set(starts)) == len(starts),
          f"起点从 0 起、严格递增：{starts[:4]}…")
    check(starts[-1] < cp.PREVIEW_FRAMES,
          f"最后一个词在样张取的那一帧之前就开了（否则逐词点亮还没轮到它）："
          f"{starts[-1]} < {cp.PREVIEW_FRAMES}")
    check(0 < int(round(cp.PREVIEW_FRAMES * cp.PREVIEW_AT)) < cp.PREVIEW_FRAMES,
          "取中间那一帧：太早字没出来，太晚逐词点亮与整句亮着没有区别")

    # ---------- ② 色板是入参，不猜 ----------
    print("② 色板四色：给名字就照给，认不出就按默认")
    check(cp.film_style("") == {}, "空串＝「默认（片子的底色）」→ 空表（母版走自己的缺省）")
    check(cp.film_style("no-such-palette") == {},
          "认不出的色板名→空表：宁可按默认渲一张，不拿一套不相干的配色冒充")
    names = list(cs.palettes())
    for name in names:
        style = cp.film_style(name)
        pal = cs.palettes()[name]
        check(set(style) == {"bg", "paper", "ink", "accent"}
              and style["bg"] == str(pal["page"]).lower()
              and style["paper"] == str(pal["surface"]).lower()
              and style["ink"] == str(pal["text"]).lower()
              and style["accent"] == str(pal["accent"]).lower(),
              f"{name} 的四色就是色板那四栏：{style}")
    fields = cp.sample_fields("deep-ocean")
    check([f["name"] for f in fields] == ["page", "surface"]
          and all(f["type"] == "color" for f in fields),
          "底稿那侧声明的是它真读的那两色（少一栏＝闸① 判「读了却没声明」，"
          "多一栏＝判「声明了没读」）")
    check(fields[0]["value"] == cp.film_style("deep-ocean")["bg"],
          "底稿与字幕条取的是同一份色（两处各取一次必漂）")
    check(all(f["value"] == "" for f in cp.sample_fields("")),
          "默认色板下两栏给空串，底稿落回 TOKENS（不是前端猜一个颜色）")

    # ---------- ③ 一对样式：两维都进注入，且真的各管各的 ----------
    print("③ 一对样式：字幕样式与高亮样式都落进注入，互不干扰")
    check(cp.pair_key("on_picture", "color") == "on_picture+color",
          f"一对样式的拼法就是这个键：{cp.pair_key('on_picture', 'color')}")
    check(cp.pair_key("paper", "ink") != cp.pair_key("ink", "paper"),
          "两维次序不混：纸条底+撕纸 与 撕纸+纸条底 不是一个键（一个是字幕样式一个是高亮）")
    combos = [(c, h) for c in cs.CAPTION_STYLES for h in cs.HIGHLIGHT_STYLES]
    check(len({cp.pair_key(c, h) for c, h in combos}) == len(combos),
          f"35 对组合的键两两不同（撞了＝两对样式共用一张样张，界面看着像点了没反应）："
          f"{len(combos)} 对")

    for cap, hl in [("on_picture", "color"), ("paper", "ink"), ("big_center", "karaoke")]:
        job = cp.preview_job(cap, hl)
        check(job is not None, f"（{cap} × {hl}）拿得到一组载荷")
        if job is None:
            continue
        props = job["input_props"]
        check(props["captionStyle"] == cap and props["highlightStyle"] == hl,
              f"（{cap} × {hl}）两维原样落进注入："
              f"{props['captionStyle']} / {props['highlightStyle']}")
        check(bool(props.get("words")) and props["text"] == cp.SAMPLE_TEXT,
              f"（{cap} × {hl}）带着词轴与那句示范文案")
        check(props["durationInFrames"] == cp.PREVIEW_FRAMES
              and (props["width"], props["height"]) == (1080, 1920),
              f"（{cap} × {hl}）的时长画幅是样张自己那份（与成片无关）")
        check(props.get("entrance") == "none" and props.get("exit") == "none",
              f"（{cap} × {hl}）关掉整片进出场：样张要站在原地给人量位置")
        check("film_style" not in props,
              f"（{cap} × {hl}）默认色板时不塞 film_style"
              f"（空表会让母版走一条不同的分支）")
        check(job["frame"] == int(round(cp.PREVIEW_FRAMES * cp.PREVIEW_AT))
              and job["key"] == cp.pair_key(cap, hl),
              f"（{cap} × {hl}）取同一帧、回执键就是那一对")
    # 只动一维：另一维必须原样不动——这正是旧三维模型做不到的那件事。
    a = cp.preview_job("paper", "color")["input_props"]
    b = cp.preview_job("paper", "karaoke")["input_props"]
    check(a["captionStyle"] == b["captionStyle"] == "paper"
          and a["highlightStyle"] != b["highlightStyle"],
          "换高亮样式不动字幕样式（旧模型里这两件事被揉在一起，说不出口）")

    # ---------- ③-b 强调色：用户在面板上钉的那一枚颜色 ----------
    print("③-b 强调色：进同一份注入、进同一个键；没钉就一栏都不发")
    plain = cp.preview_job("on_picture", "color")
    colored = cp.preview_job("on_picture", "color", highlight_color="#FF8A3D")
    check("highlightColor" not in plain["input_props"],
          "没钉强调色时不发这一栏（母版照老路取色板的 accent，老行为一个字节不变）")
    check(colored["input_props"].get("highlightColor") == "#ff8a3d",
          f"钉了就是母版读的那一栏（与成片同一个注入键）：{colored['input_props'].get('highlightColor')}")
    check(colored["key"] != plain["key"] and colored["key"] == "on_picture+color@ff8a3d",
          f"换一枚颜色＝另一个键（回执键与对象键末段是同一个字符串）：{colored['key']}")
    check(cp.preview_job("on_picture", "color", highlight_color="#ff8a3d")["key"]
          == colored["key"],
          "同一个颜色两次算出同一个键（第二次才可能命中缓存）")
    # 认不出的颜色按「没钉」处理，而不是渲一张染了别的颜色的图：样张不是校验口，
    # 但它也不该拿一个用户没按过的颜色冒充他按过的那一枚。
    weird = cp.preview_job("on_picture", "color", highlight_color="红色")
    check(weird["key"] == plain["key"]
          and "highlightColor" not in weird["input_props"],
          "认不出的颜色＝没钉（键与原样一样）")
    check(cp.preview_job("on_picture", "color", highlight_color="#fff")["input_props"]
          .get("highlightColor") == "#ffffff",
          "三位缩写归一成六位（同一个颜色写成两种写法＝两张图）")
    # 认不出的取值不抛：样张不是校验口，落缺省即可。
    bad = cp.preview_job("no-such-style", "no-such-highlight")
    check(bad is not None and bad["input_props"]["captionStyle"] == "outlined"
          and bad["input_props"]["highlightStyle"] == "color",
          "认不出的取值落缺省（样张照渲一张默认的，不因为点错一次就整条报错）")

    styled = cp.preview_job("on_picture", "color", "coral-burst")
    pal = cs.palettes()["coral-burst"]
    flat_fields = {f["name"]: f["value"] for f in cp.sample_fields("coral-burst")}
    check(flat_fields == {"page": str(pal["page"]).lower(),
                          "surface": str(pal["surface"]).lower()},
          f"底稿那侧只拿它用得着的两色（底色 + 那一层柔光）：{flat_fields}")
    check(styled["input_props"].get("fields") == flat_fields,
          "底稿那份注入就是这两色")
    # 注入给底稿代码的那一栏是**拍平的** {底色/柔光: 色}（`shot_input_props` 干的），
    # 而字幕条那条走的是 film_style 四键。两条必须取自同一套色板——「字幕跟着这一镜的
    # 色板走」这句话在样张里若为假，预览就又在骗人，而且骗得比不预览更理直气壮。
    check(styled["input_props"].get("film_style") == {
        "bg": flat_fields["page"], "paper": flat_fields["surface"],
        "ink": str(pal["text"]).lower(), "accent": str(pal["accent"]).lower()},
          f"字幕条与底稿同源同值（两处各取一次必漂）："
          f"{styled['input_props'].get('film_style')}")

    # ---------- ④ 对象键：内容寻址 ----------
    print("④ 样张地址：按 (母版, 底稿, 色板, 那一对) 内容寻址")
    k1 = cp.object_key("on_picture+color", palette_key="deep-ocean",
                       master_hash="abc123def456ghi", sample_hash="samp0001")
    check(k1 == cp.object_key("on_picture+color", palette_key="deep-ocean",
                              master_hash="abc123def456ghi", sample_hash="samp0001"),
          "同样的五件事→同样的键（第二次要样张才可能命中缓存）")
    check(k1 != cp.object_key("paper+color", palette_key="deep-ocean",
                              master_hash="abc123def456ghi", sample_hash="samp0001"),
          "换字幕样式换键")
    check(k1 != cp.object_key("on_picture+ink", palette_key="deep-ocean",
                              master_hash="abc123def456ghi", sample_hash="samp0001"),
          "换高亮样式也换键（两维各存各的——只按一维存会让另一维取到错图）")
    check(k1 != cp.object_key("on_picture+color", palette_key="midnight",
                              master_hash="abc123def456ghi", sample_hash="samp0001"),
          "换色板换键（同一对在不同底色下是两张图）")
    check(k1 != cp.object_key("on_picture+color", palette_key="deep-ocean",
                              master_hash="999999999999", sample_hash="samp0001"),
          "母版改了换键——不会把旧母版渲出来的样张当新的发出去")
    check(k1 != cp.object_key("on_picture+color", palette_key="deep-ocean",
                              master_hash="abc123def456ghi", sample_hash="samp0002"),
          "底稿改了也换键——挪过样张的版面之后，不该还发上一次那张旧图")
    check(k1 != cp.object_key(cp.pair_key("on_picture", "color", "#ff8a3d"),
                              palette_key="deep-ocean",
                              master_hash="abc123def456ghi", sample_hash="samp0001"),
          "换一枚强调色＝另一个地址（同一对样式在不同强调色下是两张图）")
    check("default" in cp.object_key("on_picture+color", palette_key="",
                                     master_hash="x", sample_hash="y"),
          "默认那一档的地址里也有个确定的名字（不是空段拼出 //）")
    check(cp.object_key("a", palette_key="", master_hash="", sample_hash="").startswith(
              "captionpreview/nomaster.nosample/"),
          "拿不到母版/底稿哈希时也不落成空段")

    # ---------- ⑤ render_one：缓存、失败、没渲出来 ----------
    print("⑤ 渲样张：连不上不报错、第二次不再渲、没渲出来不回键")

    async def run() -> None:
        dead = FakeClient(ok=False)
        check(await cp.render_one(client=_client(dead), storage=storage,
                                  caption_style="on_picture",
                                  highlight_style="color") == {},
              "渲染服务连不上→空表：样张不是出片链路的一环，"
              "不把「看一眼样式」变成一条报错")

        live = FakeClient()
        keys = await cp.render_one(client=_client(live), storage=storage,
                                   caption_style="on_picture", highlight_style="color")
        check(list(keys) == ["on_picture+color"] and len(live.calls) == 1,
              f"就渲这一对、只发一次请求（实得 {live.calls}）")
        check(live.calls and len(live.calls[0]) == 1,
              f"那一次请求里只要一帧（实得 {live.calls[0] if live.calls else []} 帧）")
        head = await storage.objects.head(keys["on_picture+color"])
        check(head is not None, "这张样张的字节真落进了对象库")

        again = FakeClient()
        keys2 = await cp.render_one(client=_client(again), storage=storage,
                                    caption_style="on_picture", highlight_style="color")
        check(keys2 == keys and again.calls == [],
              "同一对再要一次：键一模一样、一次都不重渲（缓存判据是对象在不在）")

        other = FakeClient()
        keys3 = await cp.render_one(client=_client(other), storage=storage,
                                    caption_style="on_picture", highlight_style="ink")
        check(len(other.calls) == 1 and set(keys3.values()) - set(keys.values()),
              "换高亮样式＝另一张（不覆盖上一张的键，也不吃它的缓存）")

        pal = FakeClient()
        keys4 = await cp.render_one(client=_client(pal), storage=storage,
                                    caption_style="on_picture", highlight_style="color",
                                    palette_key="midnight")
        check(len(pal.calls) == 1 and set(keys4.values()) - set(keys.values()),
              "换色板同样是另一张（不覆盖上一套色板的样张）")

        tinted = FakeClient()
        keys6 = await cp.render_one(client=_client(tinted), storage=storage,
                                    caption_style="on_picture", highlight_style="color",
                                    highlight_color="#ff8a3d")
        check(len(tinted.calls) == 1 and set(keys6.values()) - set(keys.values())
              and tinted.props[0].get("highlightColor") == "#ff8a3d",
              "钉了强调色＝另一张，且**真发出去了那一栏**（不覆盖没钉那张）："
              f"{tinted.props[0].get('highlightColor')}")
        back = FakeClient()
        keys7 = await cp.render_one(client=_client(back), storage=storage,
                                    caption_style="on_picture", highlight_style="color",
                                    highlight_color="#FF8A3D")
        check(keys7 == keys6 and back.calls == [],
              "同一个颜色（大小写不同也算同一个）再要一次：键一样、一次都不重渲")

        # 换底稿：同一对样式必须落到另一个键上、而且真的重渲一次。改样张的版面之后
        # 还发上一次那张旧图，是这条链上最看不出来的一种错——两张图本身都没问题，
        # 只是不是你刚改过的那张。判据不能只看「键变了」，还得看「没吃旧缓存」。
        orig = cp.cspec.sample_shot_code
        cp.cspec.sample_shot_code = lambda: orig() + "\n// 探针：底稿改了\n"
        try:
            edited = FakeClient()
            keys5 = await cp.render_one(client=_client(edited), storage=storage,
                                        caption_style="on_picture", highlight_style="color")
        finally:
            cp.cspec.sample_shot_code = orig
        check(len(edited.calls) == 1 and set(keys5.values()) - set(keys.values()),
              "底稿改了＝另一张、也重渲（不吃旧底稿渲出来的缓存）")

        blank = FakeClient(empty=True)
        got = await cp.render_one(client=_client(blank), storage=storage,
                                  caption_style="paper", highlight_style="block")
        check(got == {},
              "这一轮没渲出来就**不回键**（破图与「这档就是这么素」在界面上一样）："
              f"{got}")

    asyncio.run(run())

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
