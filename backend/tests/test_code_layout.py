# -*- coding: utf-8 -*-
"""闸⑧ 版面回读的离线用例：三态分清、只记账不拦截、措辞单源、缓存还原。

真机那一半在 `remotion/tests/layout-probe.mjs`（六支夹具各真渲一镜，钉住 0.4 那条重叠
线、3px 那条切字线、以及 `<g transform>` 的 `getScreenCTM` 映射）。Python 侧用假读数跑，
所以能单面造出真机上要改母版才造得出的东西——**并且先按状态打出红印在屏幕上**：

1. **三态分开**。「没有告警」有两种完全不同的意思：扫过且干净 / 压根没扫。把后者写成
   前者，回执就是在谎报「这一镜的版面验过」，而它是这条链唯一看不见的东西（判据是几何
   猜的，抽帧也不一定能看得出）。所以 missing / unscanned / scanned 三档的产出必须两两
   不同，且前两档**必须**出一条话。
2. **只记账，永不拦截**。这一道是八道里唯一不设红的，于是它有另一种失败方式：容器随手
   回一条形状不对的读数，Python 侧把它读成异常——那已经是像素烧完之后了，一镜白扔。
   所以这里喂的全是畸形输入，断言只有一条：不许抛。同时用源码钉把「闸⑧ 参与 raise」
   这条路堵死（将来有人想把它升级成硬闸，得先改这条断言并说明代价）。
3. **措辞单源**。逐条人话只在 `describe_layout` 写一遍（节点、回执、缓存还原共用），
   阈值随读数一起回传，本层不抄一份常量表——抄了就会和容器里的母版漂移。

外加两条口径钉子：`"layout"` 必须在 `META_FIELDS` 里（缓存镜不还原就会被读成「没报问题」，
而它其实是「这一次压根没扫」），以及模型侧白名单收得下这两个键、client 契约里点名了
`layout_used`（不收就是假告警：账上有、模型看不见）。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.motion import code_render as cr   # noqa: E402

_fails = 0
_checks = 0

BACKEND = Path(__file__).resolve().parents[1]

#: 与 remotion/src/tokens.ts 的 LAYOUT_PROBE 同一组值；这里当**读数**用（容器回传什么
#: 就记什么），不是判据表——判据表只有 TS 那一份（`tokens.ts` 的 `LAYOUT_PROBE`，
#: 探针文件 `src/lib/LayoutProbe.tsx` 读它）。**这里面一个判据数值都不许出现**：
#: 这一份全是「容器这么报、我就这么记」的夹具，抄进来一个 0.3 就多了一把尺。
TH = {"overlap_ratio": 0.4, "min_box_px": 6, "clip_px": 3, "anchor_ratio": 0.02,
      "anchor_min_px": 20, "leader_min_px": 40, "max_findings": 8,
      "subject_overlap_ratio": 0.3, "subject_max_share": 0.85}


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _scanned(**over):
    base = {"frame": 43, "findings": [],
            "scanned": {"texts": 2, "leaders": 1, "drawn": 5, "dropped": 0,
                        "band": {"w": 936, "h": 1524}},
            "thresholds": dict(TH)}
    base.update(over)
    return base


def main() -> int:
    # ── ① 三态各自的形状 ────────────────────────────────────────────────
    print("\n[① missing：容器压根没回传版面行]")
    miss = cr.summarize_layout("s01", None)
    check(miss["status"] == "missing", f"None → missing（实得 {miss['status']}）")
    junk = cr.summarize_layout("s01", ["不是对象"])
    check(junk["status"] == "missing" and "形状不认" in junk["reason"],
          f"列表形状的版面行也归 missing，并把原因写在 reason 里（实得 {junk['reason']}）")
    junk2 = cr.summarize_layout("s01", "字符串")
    check(junk2["status"] == "missing" and "str" in junk2["reason"],
          f"字符串版面行归 missing 且 reason 带类型名（实得 {junk2['reason']}）")

    print("\n[② unscanned：收到了行、这一帧没扫]")
    uns = cr.summarize_layout("s02", {"scanned": None, "thresholds": dict(TH),
                                      "reason": "画面带 ref 未挂载，这一帧没扫版面"})
    check(uns["status"] == "unscanned", f"scanned=null → unscanned（实得 {uns['status']}）")
    check(uns["reason"].startswith("画面带"), "母版给的 reason 原样留着（不是泛化成「失败」）")
    check(uns["thresholds"] == TH, "没扫也把阈值一起记账：这条链的判据取值要能对着读数核")
    check(cr.summarize_layout("s02", {"scanned": "不是表"})["status"] == "unscanned",
          "scanned 是个字符串也不算扫过（只认 Mapping）")

    print("\n[③ scanned：真扫了]")
    clean = cr.summarize_layout("s03", _scanned())
    check(clean["status"] == "scanned" and clean["findings"] == [],
          "findings 空 + scanned 有 → scanned 且零告警")
    # R8-T30 加了三个读数（闸⑲-a 的两个占比与主体件个数），R8-T32 又加了三个
    # （闸⑳(c) 的画面指纹）。**白名单**这一条因此从「等于那四个」长成「等于那四个 +
    # 明确列出来的那几个」——落进 scanned 的每一栏都必须是判据真读的，顺手带进来的
    # 字段（比如母版新加的调试值）不许悄悄流到回执上。
    check(set(clean["scanned"]) == {"texts", "leaders", "drawn", "dropped",
                                    "subjects", "subject_share", "text_share",
                                    "grid_text", "grid_drawn", "colors"},
          f"scanned 只留判据真读的那几栏（实得 {sorted(clean['scanned'])}）")
    check(clean["band"] == {"w": 936, "h": 1524},
          "带内几何随读数回传：finding 的坐标只有配上带子尺寸才读得懂")
    check(clean["frame"] == 43, "探针帧号入账——它必须与代表帧、闸⑥ 的盒是同一帧")

    print("\n[④ findings 里的杂色]")
    mixed = cr.summarize_layout("s04", _scanned(findings=[
        None, "不是表", {"kind": "overlap", "texts": ["甲", "乙"],
                         "box": {"x": 1, "y": 2, "w": 3, "h": 4}, "ratio": 0.62}]))
    check(len(mixed["findings"]) == 1,
          f"非 Mapping 的 finding 丢掉，合法那条留着（实得 {len(mixed['findings'])} 条）")

    # ── ⑤ 措辞：三档必须两两不同，前两档必须出声 ─────────────────────────
    print("\n[⑤ 措辞单源 + 三档都出声]")
    d_miss = cr.describe_layout("s01", miss)
    d_uns = cr.describe_layout("s02", uns)
    d_clean = cr.describe_layout("s03", clean)
    check(len(d_miss) == 1 and "没扫过" in d_miss[0] and "不许当成" in d_miss[0],
          f"missing 必出一条且明说不许当干净（实得 {d_miss}）")
    check(len(d_uns) == 1 and "没扫版面" in d_uns[0] and "ref 未挂载" in d_uns[0],
          f"unscanned 必出一条且带上母版的原因（实得 {d_uns}）")
    check(d_clean == [], "scanned 且零 finding → 一条都不出（这条链不许把干净也写成话）")
    check(d_miss != d_uns, "「落空」与「没扫」两句话不是一句——它们是两种病")
    none_lay = cr.describe_layout("s01", None)
    check(len(none_lay) == 1 and "版面回读落空" in none_lay[0]
          and "不许当成" in none_lay[0],
          f"layout 整个缺失时按 missing 那档说话（实得 {none_lay}）")
    check(cr.describe_layout("s01", ["不是表"]) == none_lay,
          "layout 是杂色时也归到 missing 那句话，不在 .get 上抛")

    print("\n[⑥ 逐条人话：三类 finding + 丢条数，阈值引的是读数里的值]")
    lay = cr.summarize_layout("s05", _scanned(findings=[
        {"kind": "overlap", "texts": ["标题很长很长的一个标签", "另一个标签"], "ratio": 0.52,
         "box": {"x": 250, "y": 200, "w": 150, "h": 120}},
        {"kind": "clipped", "texts": ["被切掉的长标签文字"], "side": "right", "px": 124,
         "box": {"x": 760, "y": 300, "w": 300, "h": 90}},
        {"kind": "dangling", "texts": ["引线标签"], "at": {"x": 800, "y": 1300}, "box": None},
        {"kind": "以后再说", "texts": ["没见过的判据"]},
    ], scanned={"texts": 9, "leaders": 3, "drawn": 20, "dropped": 2,
                "band": {"w": 936, "h": 1524}}))
    lines = cr.describe_layout("s05", lay)
    check(len(lines) == 4 + 1, f"三类 + 未知类 + 丢条数 = 5 条（实得 {len(lines)}）")
    check("重叠 0.52" in lines[0] and "判据 0.4" in lines[0],
          f"overlap 报实测比例并引读数里的判据（实得 {lines[0]}）")
    check("右边缘切掉约 124px" in lines[1] and "判据 3px" in lines[1],
          f"clipped 把 side 翻成人话并带上越界像素（实得 {lines[1]}）")
    check("带内坐标" in lines[2] and "800" in lines[2],
          f"dangling 报的是带内坐标空端（实得 {lines[2]}）")
    check(lines[3].startswith("闸⑧ s05：以后再说"),
          f"没认得的 kind 不静默吞掉，照原文落一条（实得 {lines[3]}）")
    check("丢了 2 条" in lines[4] and "上限 8" in lines[4],
          f"母版那边丢的 finding 也要上账（实得 {lines[4]}）")
    check("标题很长很长的一个标签" in lines[0],
          "原文进句子（截到 16 字）——不给镜号加一句没法核对的告警")

    # ── ⑥-b 字压图（2026-10-16 新增的第四类 kind）──────────────────────
    # 单独一小节而不是塞进 ⑥ 那支夹具：那支的断言全按**下标**取行（lines[0]/[1]/…），
    # 往中间插一条会把后面每一条断言的指向都挪一格，而挪错一格仍然是绿的。
    print("\n[⑥-b 字压图：这一条必须给出改法，光说「压住了」没法改成代码]")
    lay_t = cr.summarize_layout("s06", _scanned(findings=[
        {"kind": "overlap_subject", "texts": ["工业革命之后的富态"],
         "box": {"x": 120, "y": 400, "w": 300, "h": 90}, "ratio": 0.62, "share": 0.31}],
        scanned={"texts": 3, "leaders": 0, "drawn": 8, "dropped": 0,
                 "band": {"w": 936, "h": 1524}}))
    lt = cr.describe_layout("s06", lay_t)
    check(len(lt) == 1, f"一条 finding 出一句（实得 {len(lt)} 条）")
    check("压在画出来的图形上" in lt[0] and "工业革命之后的富态" in lt[0],
          f"点名是哪一块字压住了（实得 {lt[0]}）")
    check("0.62" in lt[0] and "0.3" in lt[0],
          f"被盖住多少与判据都印出来，且**与上面 overlap 那一句同一个写法**（实得 {lt[0]}）")
    check("移开" in lt[0] or "衬底" in lt[0],
          f"给出可执行的改法，不只是把事实念一遍（实得 {lt[0]}）")
    check("x=120" in lt[0] and "w=300" in lt[0],
          f"四栏都在时把带内坐标印出来（挪字的人得知道从哪儿挪）（实得 {lt[0]}）")
    lt_no_th = cr.describe_layout("s06", cr.summarize_layout(
        "s06", _scanned(findings=[{"kind": "overlap_subject", "texts": ["甲"], "ratio": 0.5}],
                        thresholds=None)))
    lt_no_box = cr.describe_layout("s06", cr.summarize_layout(
        "s06", _scanned(findings=[{"kind": "overlap_subject", "texts": ["甲"], "ratio": 0.5,
                                   "box": {"x": 1, "y": 2}}])))
    check("x=1" not in lt_no_box[0] and "None" not in lt_no_box[0],
          f"盒缺栏时不印半张坐标表（半张表会让人照着错的数去挪）（实得 {lt_no_box[0]}）")
    check(len(lt_no_th) == 1 and "判据 未知" in lt_no_th[0],
          f"读数没带阈值时明写「未知」，不填一个默认数字冒充判据（实得 {lt_no_th}）")

    print("\n[⑦ 措辞不许再抄一份常量表]")
    no_th = cr.describe_layout("s06", cr.summarize_layout(
        "s06", _scanned(findings=[{"kind": "overlap", "texts": ["甲", "乙"], "ratio": 0.9}],
                        thresholds=None)))
    check("判据 未知" in no_th[0],
          f"读数没带阈值时明写「未知」，不填一个默认数字冒充判据（实得 {no_th[0]}）")
    src = inspect.getsource(cr.describe_layout)
    check("LAYOUT_PROBE" not in src and "0.4" not in src.replace("0.42", ""),
          "describe_layout 里没有第二个判据数值来源（只有读数里的）")

    # ── ⑧ 畸形读数：不许抛 ──────────────────────────────────────────────
    print("\n[⑧ 畸形读数只记账，不许把烧好的像素作废]")
    for label, payload in [
        ("版面行是数字", 42),
        ("版面行是空串", ""),
        ("findings 是数字", {"scanned": {"texts": 1}, "findings": 5}),
        ("scanned 是布尔", {"scanned": True}),
        ("阈值是字符串", _scanned(thresholds="不是表")),
        ("band 缺位", {"findings": [], "scanned": {"texts": 1, "leaders": 0, "drawn": 1,
                                                   "dropped": 0}}),
        ("findings 里套列表", {"findings": [[1, 2]], "scanned": {"texts": 1}}),
    ]:
        try:
            out = cr.summarize_layout("s07", payload)
            words = cr.describe_layout("s07", out)
        except Exception as e:                                   # noqa: BLE001
            check(False, f"{label}：抛了 {type(e).__name__}: {e}")
            continue
        check(isinstance(out, dict) and isinstance(words, list),
              f"{label} → 收成 {out['status']}，逐条话 {len(words)} 句，没抛")

    for label, raw in [
        ("texts 是数字", [{"kind": "overlap", "texts": 7, "ratio": 0.5}]),
        ("findings 是数字", 7),
        ("dropped 是字符串", []),
    ]:
        sc = {"dropped": "不是数"} if label == "dropped 是字符串" else {"dropped": 0}
        try:
            words = cr.describe_layout("s07b", {"status": "scanned", "findings": raw,
                                                "scanned": sc, "thresholds": {}})
        except Exception as e:                                   # noqa: BLE001
            check(False, f"describe 直喂 {label}：抛了 {type(e).__name__}: {e}")
            continue
        check(isinstance(words, list),
              f"describe 直喂 {label} → {len(words)} 句，没抛")

    bad_ratio = cr.describe_layout("s08", {"status": "scanned", "scanned": {"dropped": 0},
                                           "thresholds": {}, "findings": [
                                               {"kind": "overlap", "texts": ["甲"],
                                                "ratio": "不是数字"}]})
    check(len(bad_ratio) == 1 and "拿不到读数" in bad_ratio[0],
          f"ratio 是杂字也只写「拿不到读数」，不抛（实得 {bad_ratio}）")
    check(cr.describe_layout("s09", {"status": "scanned", "scanned": "不是表",
                                     "findings": []}) == [],
          "scanned 段被手改坏时不抛，且不硬编一条丢条数告警")

    print("\n[⑨ 记账不拦截：源码钉]")
    for fn in (cr.render_code_shot, cr.summarize_layout, cr.describe_layout):
        body = inspect.getsource(fn)
        bad = [ln.strip() for ln in body.splitlines()
               if "闸⑧" in ln and ("raise" in ln or "MediaError" in ln)]
        check(not bad, f"{fn.__name__} 里没有「闸⑧ → raise」（版面这一路不设红）：{bad}")
    check("闸⑧" in inspect.getsource(cr.render_code_shot),
          "渲染路径确实经过闸⑧ 的记账（不是只在回执层才现算）")

    # ── ⑩ 缓存还原与白名单：两处口径钉 ──────────────────────────────────
    print("\n[⑩ 缓存还原 / 模型侧白名单 / client 契约]")
    check("layout" in cr.META_FIELDS,
          "layout 在 META_FIELDS 里——命中缓存的镜不还原它就会被读成「没报问题」，"
          "而它其实是「这一次压根没扫」")
    full = {k: None for k in cr.META_FIELDS}
    check(all(k in full for k in cr.META_FIELDS), "还原判定：字段齐全 → 能命中")
    check(not all(k in {k: v for k, v in full.items() if k != "layout"}
                  for k in cr.META_FIELDS),
          "老格式缓存（没有 layout）→ 判成 miss，照实重烧一遍，不拿旧口径冒充新读数")
    check('"layout": summarize_layout(' in inspect.getsource(cr.render_code_shot),
          "每镜的账里存的就是 summarize_layout 的产出（措辞与状态在写入时就定口径）")

    # 按**集合**判，不按文本判：support.py 里那段注释也写着这两个键名，而文本判
    # （早先那版的 `or "layout_warnings" in support`）在键真被删掉时照样绿——假绿
    # 比不报更坏，那正是这条口径钉要防的。
    from agent_framework.plan import support as plan_support          # noqa: E402
    miss = {"layout_warnings", "layout_unscanned"} - set(plan_support.OUTPUT_FIELD_NAMES)
    check(not miss,
          f"模型侧白名单收得下这两个键（缺 {sorted(miss)} 就是假告警：账上有、模型看不见）")
    client = (BACKEND / "open_storyline" / "motion" / "remotion_client.py").read_text(encoding="utf-8")
    check("layout_used" in client, "client 的响应契约里点名 layout_used（漏字段=静默 missing）")
    # 2026-10-25：原「节点层用同一份措辞」那条删了——它读 `nodes/motion_code_nodes.py`，
    # 而那个节点随图形科普链一起退役了。措辞的单源性仍由 `remotion_client` 那一侧守着。

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
