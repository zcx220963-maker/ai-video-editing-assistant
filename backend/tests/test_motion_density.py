# -*- coding: utf-8 -*-
"""闸⑭ 动效事件密度的离线用例：三态分清、拒的那一档真退镜、措辞单源、阈值只有一个出处。

真机那一半在 `remotion/tests/motion-probe.mjs`（三支夹具各真渲一镜：静帧必须拒、
三件齐发必须拒、先后三件必须放）。那边的判据本体是 `remotion/motionsample.mjs`——
**判据住在容器里**，Python 侧只做两件事：把读数收成账，以及把 `rejected` 翻成退镜。
所以这个文件用假读数跑，钉的是这四件事：

1. **三态分开**。「数不出先后动」与「压根没判过」在回执上必须是两句话：前者是内容缺陷
   （这一镜真退），后者是工具故障（记未判、单独点名）。把后者折成通过，这一闸就白设了；
   把后者折成拒，则是让工具故障冒充内容缺陷——两种都钉在这里。
2. **两份证据打架时以细的那份为准**。容器报 `ok`、账上逐段却数不出它自己那条判据要的
   段数时，降成未判而不是跟着 `ok` 走。这是这一闸最容易出的假绿：判据自己说不圆，
   而账面看着是绿的。
3. **拒的那一档真的退镜**，且退在**切片进缓存之前**（退回去的镜不该在缓存里留一份
   不合格的切片，否则下一次命中缓存就从另一条路进来了）。
4. **阈值只有一个出处**。判据数字（≥2 段、相邻 ≥0.4 s）住在容器的 `motionsample.mjs` 里，
   随读数回传；Python 侧**不许**另抄一份常量表——抄了就会「容器改了阈值、回执还按旧值
   解释读数」。这条用行为钉：喂一组**非默认**阈值，模板里报出来的必须是那一组。

真机侧的红样另见 `remotion/tests/fixtures-motion.mjs` 与那份探针的输出。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_render as cr   # noqa: E402

_fails = 0
_checks = 0

BACKEND = Path(__file__).resolve().parents[1]

#: 与 `remotion/motionsample.mjs` 的导出同形；这里当**读数**用（容器回传什么就记什么），
#: 不是判据表——判据表只有容器那一份（见文件头第 4 条）。
TH = {"diff_min": 1.5, "min_events": 2, "min_gap_sec": 0.4, "sample_hz": 8}


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _ok_row(**kw):
    """一份「判过且过」的读数：两段分开的先后变化（帧 23 / 44），最紧一对隔 21 帧。"""
    row = {"status": "ok", "samples": 16, "sample_hz": 8, "thresholds": dict(TH),
           "events": [{"start_frame": 23, "end_frame": 34, "start_sec": 2.875, "peak_diff": 12.5},
                      {"start_frame": 44, "end_frame": 52, "start_sec": 5.5, "peak_diff": 9.25}]}
    row.update(kw)
    return row


def _reject_row(**kw):
    row = _ok_row(status="reject", events=[{"start_frame": 8, "end_frame": 56,
                                            "start_sec": 1.0, "peak_diff": 22.79}],
                  reason="全片只数到 1 个先后变化段（要 ≥2 个）——所有元素挤在同一段时间里"
                         "一起变，看起来是一次跳变而不是「先后动效」。")
    row.update(kw)
    return row


def main() -> int:
    print("=== 一、三态分开（红样就是那两支「看着绿」的形状）===")
    # 未判三支：压根没回传 / 形状不认 / 状态不认识。三支都必须落在 undetermined，
    # 而且都**不许**等于 ok——「拿不到读数」折成通过正是这一族最坏的写法。
    for label, raw in (("容器没回传这一行", None),
                       ("回传了个字符串", "ok"),
                       ("状态不认识", {"status": "maybe", "events": [], "thresholds": dict(TH)})):
        got = cr.summarize_motion("s01", raw)
        check(got["status"] == "undetermined" and got["status"] != "ok",
              f"{label} → undetermined（实得 {got['status']}：{str(got['reason'])[:40]}）")
    got = cr.summarize_motion("s01", None)
    check("没回传" in str(got["reason"]),
          f"未判的理由说得出是「没回传」（实得 {str(got['reason'])[:50]}）")

    ok = cr.summarize_motion("s01", _ok_row())
    check(ok["status"] == "ok" and len(ok["events"]) == 2 and ok["min_gap_frames"] == 21,
          f"判过且过的读数照实记 ok，并算出最紧一对隔 21 帧（实得 {ok['status']}、"
          f"{len(ok['events'])} 段、{ok['min_gap_frames']}）")
    check(ok["thresholds"] == TH,
          "阈值原样搬运（Python 侧不另写一份判据数字）")

    rej = cr.summarize_motion("s01", _reject_row())
    check(rej["status"] == "rejected",
          f"容器的 reject 在账上记 rejected（实得 {rej['status']}）——"
          "与闸⑬ 的用词同规，下游不用同时认两个拼法")

    print("\n=== 二、两份证据打架：以细的那份为准，降成未判 ===")
    # 这一支就是「假绿」的样本：整份状态写 ok，逐段账却只有 1 段，而它自己回传的判据
    # 要 ≥2 段。跟着 ok 走的话，这一闸对着内嵌自相矛盾的读数永远绿。
    clash = cr.summarize_motion("s01", _ok_row(events=[{"start_frame": 23, "end_frame": 34,
                                                       "peak_diff": 12.5}]))
    check(clash["status"] == "undetermined",
          f"报 ok 但只有 1 段（判据要 ≥2）→ 降成未判（实得 {clash['status']}）")
    check("打架" in str(clash["reason"]),
          f"降级的理由明写「两份证据打架」（实得 {str(clash['reason'])[:60]}）")
    # 拿不到阈值时不做这项核对（本层不持有第二个拷贝，也就不可能自己造一个阈值出来）。
    noth = cr.summarize_motion("s01", _ok_row(thresholds=None,
                                              events=[{"start_frame": 23, "end_frame": 34}]))
    check(noth["status"] == "ok" and noth["thresholds"] is None,
          f"阈值缺失时不硬判（核对要用的那个数字只有容器有，本层不猜；实得 {noth['status']}）")

    print("\n=== 三、拒的那一档真的退镜，且理由里带得出「差在哪」 ===")
    check(cr.motion_error("s01", cr.summarize_motion("s01", _ok_row())) is None,
          "ok 的镜不退")
    check(cr.motion_error("s01", cr.summarize_motion("s01", None)) is None,
          "未判的镜不走「退镜」这一刀（工具故障不当内容缺陷判）")
    err = cr.motion_error("s01", cr.summarize_motion("s01", _reject_row()))
    check(bool(err) and "跳变" in err,
          f"rejected → 退镜，且把容器那句理由原文搬回来（实得 {str(err)[:80]}）")
    # 「数到 1 段」这种拒法必须报出**它把那一段量在哪几帧**：只报段数的话，
    # 「三件齐发」与「两件挨太近」在回执上长得一样，而两者的改法完全不同。
    check(bool(err) and "s01" in err and "数到 1 段" in err and "跨帧 8–56" in err,
          f"退镜理由带镜号、「数到几段」与那段跨了哪些帧（实得 {str(err).splitlines()[-1][:80]}）")
    bare = cr.motion_error("s01", cr.summarize_motion(
        "s01", {"status": "reject", "thresholds": dict(TH), "events": []}))
    check(bool(bare) and "没给出理由" in bare,
          f"容器没给理由时也要有一条人话（实得 {str(bare)[:70]}）")

    print("\n=== 四、阈值只有一个出处：喂非默认阈值，报出来的必须是那一组 ===")
    # 这条是**行为**钉，不是文本钉：Python 侧若抄了一份 2 / 0.4，这里就会把「3 / 0.75」
    # 报成「2 / 0.4」，断言当场红。
    other = dict(TH, min_events=3, min_gap_sec=0.75)
    err2 = cr.motion_error("s07", cr.summarize_motion(
        "s07", _reject_row(thresholds=other, reason="只数到 2 段")))
    check("3" in err2 and "0.75" in err2 and "0.4" not in err2,
          f"退镜理由里的判据跟着容器的读数走（实得 {err2.splitlines()[-1][:90]}）")

    print("\n=== 五、措辞：ok 不占行，另两态各一条且说法不同 ===")
    check(cr.describe_motion("s01", cr.summarize_motion("s01", _ok_row())) == [],
          "ok 的镜不在回执里占行")
    und = cr.describe_motion("s01", cr.summarize_motion("s01", None))
    check(len(und) == 1 and "没判" in und[0] and "退回" not in und[0],
          f"未判出一条，且明写「没判」（实得 {und[0][:70] if und else '空'}）")
    bad = cr.describe_motion("s01", cr.summarize_motion("s01", _reject_row()))
    check(len(bad) == 1 and "退回" in bad[0] and "没判" not in bad[0],
          f"被判据拒的那条与未判那条是两句话（实得 {bad[0][:70] if bad else '空'}）")
    # P6（2026-10-17 真机片）：同一条读数，档位不同说法不同——严格档是「没有进成片」，
    # 预览档这一镜**仍在成片里**。把预览档也写成「退回」，用户会以为片里没它。
    strict_bad = cr.describe_motion("s01", cr.summarize_motion("s01", _reject_row()),
                                    strict=True)
    prev_bad = cr.describe_motion("s01", cr.summarize_motion("s01", _reject_row()),
                                  strict=False)
    check("没有进成片" in strict_bad[0] and "仍在成片里" not in strict_bad[0],
          f"严格档收尾是「没有进成片」（实得 {strict_bad[0][-40:]}）")
    check("仍在成片里" in prev_bad[0] and "切 strict" in prev_bad[0],
          f"预览档明说「仍在成片里；要它退镜请切 strict」（实得 {prev_bad[0][-52:]}）")
    err_prev = cr.motion_error("s01", cr.summarize_motion("s01", _reject_row()),
                               strict=False)
    check(bool(err_prev) and "仍在成片里" in err_prev and "退回" not in err_prev,
          f"motion_error 预览档不写「退回」（实得 {str(err_prev)[:60]}）")

    print("\n=== 六、账与回执的口径钉子 ===")
    check("motion" in cr.META_FIELDS,
          "motion 在 META_FIELDS 里（缓存镜不还原这一栏，动效读数就整片消失）")
    full = {k: None for k in cr.META_FIELDS}
    check(not all(k in {k: v for k, v in full.items() if k != "motion"} for k in cr.META_FIELDS),
          "老格式缓存（没有 motion）→ 判成 miss，照实重烧一遍，不拿旧口径冒充新读数")
    src = inspect.getsource(cr.render_code_shot)
    check('"motion": motion' in src and "summarize_motion(" in src,
          "每镜的账里存的就是 summarize_motion 的产出（状态在写入时就定口径）")
    # 拒的那一刀必须落在**切片进缓存之前**：`cache.store(` 出现在 motion_error 之后。
    check(src.index("motion_error(sid, motion,") < src.index("await cache.store("),
          "闸⑭ 的退镜排在切片进缓存之前——退回去的镜不在缓存里留一份不合格的切片")
    check(src.index("motion_error(sid, rec.get(\"motion\"),") < src.index("await cache.store("),
          "命中缓存的那一路也过同一刀（阈值不在缓存键里，缓存只证明「按当时的判据它是绿的」）")
    check("strict=qc_strict(spec)" in src,
          "退镜文案跟着质检档走（P6：预览档那镜仍在成片里，不能写成「退回」）")

    from agent_framework.plan import support as plan_support          # noqa: E402
    miss = {"motion_warnings", "motion_unjudged"} - set(plan_support.OUTPUT_FIELD_NAMES)
    check(not miss,
          f"模型侧白名单收得下这两个键（缺 {sorted(miss)} 就是假告警：账上有、模型看不见）")
    nodes = (BACKEND / "storyline_server" / "nodes" / "motion_code_nodes.py").read_text(encoding="utf-8")
    check("describe_motion" in nodes and "motion_unjudged" in nodes,
          "节点层用同一份措辞，且把「几镜判过」写进证据")
    check("motion" in (BACKEND / "storyline_server" / "motion" / "remotion_client.py").read_text(encoding="utf-8"),
          "client 的响应契约里点名 motion（漏字段=静默未判）")

    print("\n=== 七、「差一点」的那几处要留进账里 ===")
    # 2026-10-18 真机曲线（s06，338 帧）：第 0~0.25 s 的满宽色带 14.05 过了线，
    # 4.4 s 那处第二块面其实画了，峰值 1.23——只差地板线 1.5 一点点，却被
    # 「数到 1 段」这句话吞掉。写手看到「只有一段」只会重排时间轴，而它真正该做的
    # 是把那一处做大。所以账里要留最响的几处亚门槛峰值。
    curve = [14.05, 10.24, 1.4, 0.5, 0.33, 0.15, 0.07, 0.02, 0.01, 0.0, 0.0, 0.0,
             0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
             0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.19, 1.15, 1.23,
             0.87, 0.39, 0.58, 0.22, 0.2, 0.28, 0.03, 0.02]
    near = cr.summarize_motion("s06", _reject_row(diffs=curve, sample_hz=8))["near"]
    check(len(near) == 1, f"只报一处（4.4 s 那块），实得 {near}")
    check(near and abs(near[0]["start_sec"] - 4.56) < 0.2, f"起点落在 4.5 s 上下：{near}")
    check(near and near[0]["peak_diff"] == 1.23, f"峰值照实抄 1.23：{near}")
    # 进了 events 的那段不许在 near 里再报一遍；地板线以下的噪声也不许报。
    done = cr.summarize_motion("s06", _reject_row(
        diffs=[14.05, 10.24, 1.4, 0.5, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
        events=[{"start_frame": 0, "end_frame": 11, "start_sec": 0.0,
                 "end_sec": 0.375, "peak_diff": 14.05}]))["near"]
    check(done == [], f"过线那段与噪声都不进 near（实得 {done}）")
    nobump = cr.summarize_motion("s06", _reject_row())["near"]
    check(nobump == [], "没给曲线就没有 near（不凭空造一处）")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
