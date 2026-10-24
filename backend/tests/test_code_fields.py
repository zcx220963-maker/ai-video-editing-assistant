# -*- coding: utf-8 -*-
"""字段参数通道（spec §12）的离线用例：闸⑥ 的四态、缓存键口径、命中表的字段条目、
补丁口的指针与重烧口径。

这份文件管的是**执行层那一半**（`code_render` + `code_spec`）与**装配层那一半**
（`code_patch`）。闸① 那三条规则的红在 `remotion/tests/gate1-lint.mjs`（那侧是 AST，
Python 造不出红），回读通道的字节在 `remotion/tests/field-probe.mjs`（那侧要真浏览器）。
这里用假服务把 `fields_used` 塞进 `/render-shot` 的响应，于是能单面造出「注入 90、
自报 80」这种真机上要改代码才造得出的不一致——**并且先把它打成红印在屏幕上**，
再证明同一份夹具改一个字节就绿。补丁那半同理：`relint_codes` 收的是**注入进来的**
lint 可调用对象，所以「改代码必复跑、复跑炸了不许放行」这条口径能在离线拿假服务跑。

十五组断言，前八组是执行层（§12.5/§12.6），后七组是补丁口（§12.7）：

    ① 闸⑥ 值不符 → 红，而且红得早（闸④ 不开、切片不进缓存、母带不拼）
    ② 闸⑥ 值相符 → 绿，逐字段结论进账
    ③ 量不到盒 → no_geometry，**不判红**但照实记账（spec 定死的口径）
    ④ 压根没标注 / 整行回读落空 → unreported，同样不判红
    ⑤ 比对口径按类型分：颜色写法归一、数字容差 0、bool 用 is
    ⑥ 改一个字段的值 → 只有那一镜的缓存键变（按字节断言），别的镜命中
    ⑦ 命中表的字段条目：pointer、box（成片像素）、控件范围（min/max/step/options，
      前端只问表）、缺盒仍出一条、没交旋钮的镜只有一条
    ⑧ props 里注入的是 name→value 扁表（label 换写法不许重烧）
    ⑨ 指针白名单四条认得，第五样与旧链那形状各自拒在原话上
    ⑩ 值校验与声明期同源：越界**打回不钳值**、写法归一后才认改动、同值重交直接拒
    ⑪ 表给的指针 → 装配 → 再过一遍真契约 → 只重烧那一镜（整条串起来跑）
    ⑫ 闸① 复跑口径：改值不复跑、改代码必复跑、复跑不通不许静默放行
    ⑬ 整镜改写的栏白名单（旧链那几栏在这里没有对应物）
    ⑭ 删镜与重排：先落笔再重排；重排只换顺序，不改集合也不记成改动
    ⑮ 节点级：只有代码字节变了才去敲闸①，且敲在**分叉之前**——验不过一寸不落盘

便宜档同 `test_code_gates.py`：`narration=False`、240×320@10fps。假服务、假缓存与夹具
直接 import 那一份 —— 两处各造一套的话，「两个夹具对同一条闸给出不同结论」这种红就
再也分不清是闸的问题还是夹具的问题。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import shutil
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.mediaops import MediaError                 # noqa: E402
from storyline_server.motion import code_patch as cp             # noqa: E402
from storyline_server.motion import code_render as cr            # noqa: E402
from storyline_server.motion import code_spec as cs              # noqa: E402
from storyline_server.nodes.motion_code_nodes import (           # noqa: E402
    PatchCodeMotionVideoNode)
from storyline_server.nodes.motion_nodes import (                # noqa: E402
    MotionPatchBase)
from test_code_gates import (FakeCache, FakeService, W, H, _shot,  # noqa: E402
                             _spec)

_fails = 0
_checks = 0
WORK = Path("/tmp") / "code_fields_work"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


#: 夹具用的两份字段声明。都写成契约认得的样子：name 是标识符、带 label、值在范围内
#: （`_spec` 会过一遍真校验，越界的夹具在红之前就会被拒）。
RADIUS = {"name": "radius", "type": "number", "label": "圆半径",
          "value": 90, "min": 20, "max": 300, "step": 5, "unit": "px"}
ACCENT = {"name": "accent", "type": "color", "label": "强调色", "value": "#123456"}

#: 补丁口那两组另需两类型的声明：bool 要验「前端传来的 1 不算 true」，
#: select 要验「档位必须在这份清单里」——两条都是 `check_field_value` 的分型口径。
GRID = {"name": "showGrid", "type": "bool", "label": "显示网格", "value": False}
STYLE = {"name": "style", "type": "select", "label": "填充", "value": "solid",
         "options": ["solid", "hachure"]}

#: 真机 field-probe 量回来的那两个盒（同一份版式：x = 200 + 母版 gutter 72）
BOX_R = {"x": 272, "y": 396, "w": 180, "h": 180}
BOX_A = {"x": 772, "y": 996, "w": 320, "h": 160}


def _mark(name: str, value, box: dict | None = None) -> dict:
    m = {"name": name, "value": value}
    if box:
        m["box"] = box
    return m


#: 夹具里每个字段名的盒（echo 模式用）。名字对不上时给一个通用盒——量到量不到盒
#: 这件事不是这份用例要判的，`marks=` 那一路才是。
BOXES = {"radius": BOX_R, "accent": BOX_A}
GENERIC_BOX = {"x": 40.0, "y": 40.0, "w": 120.0, "h": 60.0}

_UNSET = object()


class FieldService(FakeService):
    """假服务：把 `fields_used` 附在 /render-shot 的响应上。

    两种造法各有用处，缺一种这份用例就只验得了一半：

    * `marks=[…]` —— 逐镜发一份**固定**清单，用来单面造不一致（注入 90、自报 80）。
      `marks=[]` 是「母版跑了但一处 `<F>` 都没标」，`marks=None` 是「整行回读落空」，
      两种塌法都必须走 unreported 而不是抛异常，所以两个都要能造出来。
    * `echo=True` —— 把 props 里注入的值**原样自报**。改字段值那一组必须走这条：
      第二版期望值是 120，固定清单还停在 90，红的就不是闸而是夹具。
    """

    def __init__(self, *, marks: list[dict] | None = _UNSET, echo: bool = False,
                 frame: int = 43, **kw) -> None:
        super().__init__(**kw)
        self.marks = marks
        self.echo = echo
        self.frame = frame
        if not echo and marks is _UNSET:
            raise AssertionError("夹具得明确表态：echo、固定 marks，还是显式 None"
                                 "（整行回读落空）——不给是第四种塌法")

    def render(self, code, cache_key, input_props, declared_fields=None, assets=None,
               render=None):
        r = FakeService.render(self, code, cache_key, input_props,
                               declared_fields, assets)
        marks = self.marks
        if self.echo:
            marks = [_mark(str(n), v, BOXES.get(str(n), GENERIC_BOX))
                     for n, v in (input_props.get("fields") or {}).items()]
        r["fields_used"] = None if marks is None else {"frame": self.frame,
                                                       "marks": marks}
        return r


def _fshot(i: int = 1, **over) -> dict:
    """带字段清单的镜夹具。**code/text 按镜号区分**：缓存键里没有镜号（同输入同像素
    本就该共用一份切片，这是对的），照 `test_code_gates._shot` 那样两镜交同一段代码，
    「改一镜、另一镜命中」就会退化成「两镜共用一键」——那条断言就白写了。
    """
    base = {"fields": [dict(RADIUS), dict(ACCENT)],
            # 闸⑦「每镜必画」逐镜判：一镜只有文字过不了契约，所以夹具里必须真画出
            # 一件东西（一支裸 `<svg/>` 落 `vector` 那条通道，与 `_shot` 里默认的
            # `subject_channel="vector"` 对齐）。文字留着——闸⑥ 的字段对账与那个
            # 「字段位置」的回读都要它。
            # P1（2026-10-17）注入契约：读 `getInputProps`、用到 `text`、摆
            # `<CaptionLayer/>`——夹具先过契约，用例的红才只来自字段对账那一件事。
            # 加了注入头之后骨架不再停在 `_SHAPE_MIN_TOKENS`(40) 以下，闸⑫ 会拿它去
            # 与声明里那份 demo 比——本文件没有「整片闸必须空账」的断言，见同族说明
            # 在 `test_code_gates._shot`。
            "code": f"export const Shot = () => (<FilmLayout>第 {i} 镜要说的那一句"
                    "<svg />{getInputProps().text}<CaptionLayer /></FilmLayout>);",
            "text": f"第 {i} 镜要说的那一句"}
    base.update(over)
    return _shot(i, **base)


def _reconcile(shot: dict, marks) -> tuple[list[str], dict]:
    """单面跑闸⑥：跳开渲染，只比对这一份账。"""
    return cr.check_field_reconcile(
        "s01", shot=shot,
        fields_used=None if marks is None else {"frame": 43, "marks": marks})


async def _run(spec: dict, *, work: Path, client, cache=None):
    try:
        return await cr.render_code_motion(spec, providers=None, work=work,
                                           client=client, cache=cache), None
    except Exception as exc:                            # noqa: BLE001
        return None, exc


def _plan(base: dict, **kw):
    """跑装配器 → ((改后的 spec, 改动账) 或 None, 错误原文)。

    这里不直接让 `ValueError` 冒出去：那份清单是给人看的原话，断言就得按原话断，
    而一抛整份用例便停在第一条红上——后面的指针形状一条都验不着。
    """
    try:
        return cp.plan_code_patch(base, **kw), ""
    except ValueError as exc:
        return None, str(exc)


#: 改代码那两组用的两段新代码。**必须与夹具原码字节不同**（不然改动账是空的，
#: 「复跑闸①」这一路压根不会被走到），也必须仍带 `<FilmLayout>`（闸① 的内容红
#: 归容器侧那份用例，这里判的是「有没有去敲那扇门」）。
CODE_A = "export const Shot = () => (<FilmLayout>换了一版画面 A</FilmLayout>);"
CODE_B = "export const Shot = () => (<FilmLayout>换了一版画面 B</FilmLayout>);"


class FakeLint:
    """闸① 的替身：`relint_codes` 收的那颗**注入进来的**可调用对象。

    真机上这一路是一次出容器的 HTTP 外呼，两种塌法都得造得出来才谈得上验口径：

    * `results=[…]` —— 服务端按队列回 `ok=false`（代码真有问题），甚至回一个
      没有条目的 `{"ok": False}`（拒绝却什么都不说）。
    * `boom=True` —— **外呼本身炸了**（容器没起、超时）。这一种要是放过去，
      就等于「渲染服务暂时不通」变成了「这次改的代码不用验」。
    """

    def __init__(self, results: list[dict] | None = None, *, boom: bool = False) -> None:
        self.results = list(results or [])
        self.boom = boom
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, code: str, names: list[str]) -> dict:
        self.calls.append((code, list(names)))
        if self.boom:
            raise ConnectionError("creation-remotion 没起来（connect 被拒）")
        if self.results:
            return self.results.pop(0)
        return {"ok": True, "errors": []}


class _SyncLint:
    """节点那一层要的**同步** lint 口。

    `_pre_gate` 把外呼包在 `asyncio.to_thread(client["lint"], …)` 里，收的是同步函数；
    拿上面那颗协程版假服务去敲会把「 awaited 一个协程的协程」这种夹具错误伪装成红，
    所以节点级那组单开一份同步形状。
    """

    def __init__(self, *, result: dict | None = None, boom: bool = False) -> None:
        self.result = {"ok": True, "errors": []} if result is None else result
        self.boom = boom
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, code: str, names: list[str]) -> dict:
        self.calls.append((code, list(names)))
        if self.boom:
            raise ConnectionError("creation-remotion 没起来（connect 被拒）")
        return self.result


def main() -> int:
    async def run() -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)

        # =========================================================
        print("\n〔闸⑥·红〕注入 90、画面上自报 80")
        shot = _spec([_fshot(1)])["shots"][0]
        errs, states = _reconcile(shot, [_mark("radius", 80, BOX_R),
                                        _mark("accent", "#123456", BOX_A)])
        check(len(errs) == 1 and "radius" in errs[0],
              f"红只点名对不上的那一个：{errs}")
        check("表单改了、画面没跟着改" in errs[0],
              f"错误文本直说这条通道要消灭的那种不一致：{errs[0][:52]}")
        check("注入的是 90.0" in errs[0] and "80" in errs[0],
              f"两个读数都打出来给人看：{errs[0][:88]}")
        check(states["radius"]["status"] == "mismatch"
              and states["accent"]["status"] == "ok",
              f"逐字段结论各记各的：{ {k: v['status'] for k, v in states.items()} }")

        svc = FieldService(marks=[_mark("radius", 80, BOX_R),
                                  _mark("accent", "#123456", BOX_A)])
        out, err = await _run(_spec([_fshot(1)]), work=WORK / "g6red", client=svc)
        check(isinstance(err, MediaError) and "闸⑥" in str(err),
              f"整链把这镜退回：{str(err)[:78]}")
        check("闸④" not in str(err),
              "闸⑥ 排在闸④ 之前：值不对时不必再开 ffmpeg 判画面空不空")
        check(not (WORK / "g6red" / "shot00" / "clip.mp4").exists(),
              "红了 → 带声切片没落盘，也就没进缓存")
        check(not (WORK / "g6red" / "final.mp4").exists(), "红了 → 母带没开始")

        # =========================================================
        print("\n〔闸⑥·绿〕同一个名字、值对上")
        svc = FieldService(marks=[_mark("radius", 90, BOX_R),
                                  _mark("accent", "#123456", BOX_A)])
        out, err = await _run(_spec([_fshot(1)]), work=WORK / "g6ok", client=svc)
        check(err is None, f"值全对 → 一路到底：{err}")
        fc = out["shots"][0]["field_checks"]
        check(fc["radius"]["status"] == "ok" and fc["accent"]["status"] == "ok",
              f"逐字段结论进账：{ {k: v['status'] for k, v in fc.items()} }")
        check(fc["radius"]["box"] == BOX_R, f"盒也记着：{fc['radius']['box']}")

        # =========================================================
        print("\n〔闸⑥·四态〕缺盒与缺标注都不判红，但必须照实记账")
        errs, states = _reconcile(shot, [_mark("radius", 90, None),
                                        _mark("accent", "#123456", BOX_A)])
        check(errs == [], f"缺盒不拦（拦了就是逼模型给每个字段套一层壳去过分）：{errs}")
        check(states["radius"]["status"] == "no_geometry",
              f"结论仍是 no_geometry：{states['radius']['status']}")

        errs, states = _reconcile(shot, [])
        check(errs == [], "一处 <F> 都没标 → 不判红（判红的是闸① 那条静态规则）")
        check(set(states) == {"radius", "accent"}
              and all(v["status"] == "unreported" for v in states.values()),
              f"每个声明都留一行账，前端据此落进「无空间命中」："
              f"{ {k: v['status'] for k, v in states.items()} }")
        errs, states = _reconcile(shot, None)
        check(not errs and all(v["status"] == "unreported" for v in states.values()),
              "整行回读落空（fields_used=None）走同一条，不 KeyError")

        # =========================================================
        print("\n〔闸⑥·口径〕比对按类型分，归一不等于放宽")
        color_shot = _spec([_fshot(1, fields=[{**ACCENT, "value": "#f60"}])])["shots"][0]
        errs, states = _reconcile(color_shot, [_mark("accent", "#FF6600", BOX_A)])
        check(not errs and states["accent"]["status"] == "ok",
              f"#f60 与 #FF6600 是同一个颜色（写法差异不许判出一片假红）：{errs}")
        errs, _ = _reconcile(color_shot, [_mark("accent", "#f60", BOX_A)])
        check(not errs, "注入短写、自报短写 → 同样绿（归一走同一份 normalize_field_value）")
        errs, _ = _reconcile(_spec([_fshot(1)])["shots"][0],
                             [_mark("accent", "#123457", BOX_A)])
        check(len(errs) == 1, f"颜色差一位仍判红：{errs}")

        num_shot = _spec([_fshot(1, fields=[{**RADIUS, "value": 41}])])["shots"][0]
        errs, _ = _reconcile(num_shot, [_mark("radius", 41.0, BOX_R)])
        check(not errs, "数字 41 与 41.0 是同一个值（容差仍是 0，归一的只是写法）")
        errs, _ = _reconcile(num_shot, [_mark("radius", "41px", BOX_R)])
        check(len(errs) == 1, f"自报的压根不是数（NaN 尺寸那一类的现场）→ 红：{errs}")

        bool_shot = _spec([_fshot(1, fields=[{"name": "showGrid", "type": "bool",
                                             "label": "显示网格", "value": True}])])["shots"][0]
        errs, _ = _reconcile(bool_shot, [_mark("showGrid", 1, BOX_R)])
        check(len(errs) == 1, "bool 用 is 比：自报 1 不算 true（Python 里 True == 1 是坑）")
        errs, _ = _reconcile(bool_shot, [_mark("showGrid", True, BOX_R)])
        check(not errs, "bool 自报 True → 绿")

        sel_shot = _spec([_fshot(1, fields=[{"name": "style", "type": "select",
                                            "label": "填充", "value": "hachure",
                                            "options": ["solid", "hachure"]}])])["shots"][0]
        errs, _ = _reconcile(sel_shot, [_mark("style", "solid", BOX_R)])
        check(len(errs) == 1, f"select 换了档位 → 红（画面上换的是画法，不是写法）：{errs}")

        # =========================================================
        print("\n〔缓存键〕改一个字段的值 → 只有那一镜未命中")
        def key_of(spec: dict, i: int) -> str:
            return cs.shot_cache_key(
                spec["shots"][i], film=spec,
                master_hash="master0000000000", lockfile_hash="lockfile000000")

        spec_f = _spec([_fshot(1), _fshot(2)])
        before = [key_of(spec_f, 0), key_of(spec_f, 1)]
        moved = copy.deepcopy(spec_f)
        moved["shots"][1]["fields"][0]["value"] = 120
        after = [key_of(moved, 0), key_of(moved, 1)]
        check(before[0] == after[0], f"第 1 镜的键一字节没动：{before[0]}")
        check(before[1] != after[1],
              f"第 2 镜的键变了：{before[1]} → {after[1]}")

        label_only = copy.deepcopy(spec_f)
        label_only["shots"][0]["fields"][0]["label"] = "半径（像素）"
        label_only["shots"][0]["fields"][0]["step"] = 10
        check(key_of(label_only, 0) == before[0],
              "label/step 换写法不改像素 → 键不变（进键的是值扁表，不是整份声明）")

        cache = FakeCache()
        svc = FieldService(echo=True)
        first, err = await _run(spec_f, work=WORK / "k1", client=svc, cache=cache)
        check(err is None and len(svc.calls) == 2, f"首跑两镜都真烧：{err}")
        second, err = await _run(moved, work=WORK / "k2", client=svc, cache=cache)
        check(err is None, f"改值那一版跑通：{err}")
        check(second["cached_shots"] == 1,
              f"命中 1 镜、另一镜重烧：cached={second['cached_shots']}")
        check(len(svc.calls) == 3,
              f"整片只多提交 1 镜（3 = 首跑 2 + 改版 1）：{len(svc.calls)}")
        ids = [(r["id"], bool(r.get("cached"))) for r in second["shots"]]
        check(ids == [("s01", True), ("s02", False)],
              f"没变的那一镜命中、变了的那一镜重烧：{ids}")
        check(second["shots"][1]["field_checks"]["radius"]["expected"] == 120,
              f"重烧那一镜的账上用的是新值：{second['shots'][1]['field_checks']['radius']}")

        # =========================================================
        print("\n〔命中表〕字段条目带 pointer 与盒；没交旋钮的镜只有一条")
        svc = FieldService(marks=[_mark("radius", 90, BOX_R),
                                  _mark("accent", "#123456", BOX_A)])
        out, err = await _run(_spec([_fshot(1)]), work=WORK / "t1", client=svc)
        table = out["hitmap"]["s01"]
        check(table["schema"] == "code-shot-table/2" and table["coord"] == "output_px",
              f"表的身份与坐标系写死在表里：{table['schema']} / {table['coord']}")
        entries = [e for e in table["entries"] if e["kind"] == "field"]
        check(len(entries) == 2, f"每个声明一条：{len(entries)} 条")
        r_entry = next(e for e in entries if e["name"] == "radius")
        check(r_entry["pointer"] == "/shots/0/fields/radius/value",
              f"指针是表里的数据，不是前端拼出来的：{r_entry['pointer']}")
        check(r_entry["box"] == [272.0, 396.0, 180.0, 180.0],
              f"盒按成片像素原样：{r_entry['box']}")
        check(r_entry["type"] == "number" and r_entry["unit"] == "px"
              and r_entry["label"] == "圆半径",
              f"控件单源（type 决定用哪个控件，#127 那条纪律）：{ {k: r_entry[k] for k in ('type', 'unit', 'label')} }")
        check(r_entry["value"] == 90 and r_entry["reported"] == 90,
              f"注入值与自报值两边都进表：{r_entry['value']} / {r_entry['reported']}")
        # G9 的〈字段〉页签**只问这张表**出控件：范围若要从 bundle 里嵌的那份 spec 啃，
        # 就是给前端设了第二个出处（#127 那条纪律的另一半）。
        check({k: r_entry.get(k) for k in ("min", "max", "step", "unit")}
              == {"min": 20.0, "max": 300.0, "step": 5.0, "unit": "px"},
              f"number 条目把滑杆要的三段一起发出去（浮点，与声明期同归一）："
              f"{ {k: r_entry.get(k) for k in ('min', 'max', 'step', 'unit')} }")
        a_entry = next(e for e in entries if e["name"] == "accent")
        check([a_entry.get(k) for k in ("min", "max", "step", "options")]
              == [None, None, None, None],
              f"没声明范围就发 null，前端据此退成色块＋文本框而不是自己编一个上界："
              f"{ {k: a_entry.get(k) for k in ('min', 'max', 'step', 'options')} }")
        spec9 = _spec([_fshot(9, fields=[dict(RADIUS), dict(STYLE), dict(GRID)])])
        mixed = cr._shot_table("s09", 8, (W, H), shot=spec9["shots"][0], checks={})
        m_entry = {e["name"]: e for e in mixed["entries"] if e["kind"] == "field"}
        check(m_entry["style"]["options"] == ["solid", "hachure"],
              f"select 的档位清单进表（下拉里只能出这几项）：{m_entry['style']['options']}")
        check(m_entry["showGrid"]["type"] == "bool"
              and m_entry["showGrid"].get("options") is None,
              f"bool 靠 type 认控件，不靠字段名猜：{m_entry['showGrid']['type']}")
        check(table["entries"][0]["kind"] == "shot",
              "整镜那条仍在（这条链的底色粒度是镜，但要作为数据出现而不是代码里的假设）")
        check(table["width"] == W and table["height"] == H,
              f"表带着 width/height（要归一化坐标的自己除，这里不预先换算）")

        plain = cr._shot_table("s02", 1, (W, H), shot=_shot(2), checks={})
        check(len(plain["entries"]) == 1,
              f"没交旋钮的镜只有一条：{len(plain['entries'])}")

        no_box = FieldService(marks=[_mark("radius", 90, None),
                                     _mark("accent", "#123456", BOX_A)])
        out2, _ = await _run(_spec([_fshot(1)]), work=WORK / "t2", client=no_box)
        t2 = [e for e in out2["hitmap"]["s01"]["entries"] if e["kind"] == "field"]
        r2 = next(e for e in t2 if e["name"] == "radius")
        check(r2["box"] is None and r2["field_status"] == "no_geometry",
              f"缺盒的字段**照旧出一条**（box=null + field_status 说明为什么）：{r2['box']} / {r2['field_status']}")

        # =========================================================
        print("\n〔props〕注入的是 name→value 扁表")
        svc = FieldService(marks=[_mark("radius", 90, BOX_R),
                                  _mark("accent", "#123456", BOX_A)])
        await _run(_spec([_fshot(1)]), work=WORK / "p1", client=svc)
        props = svc.calls[0]["props"]
        check(props["fields"] == {"radius": 90, "accent": "#123456"},
              f"props.fields = 值扁表：{props.get('fields')}")
        check("label" not in str(props["fields"]),
              "整份声明没跟着注入（注入的只是值；label 上不了画面）")

        # =========================================================
        print("\n〔补丁⑨·指针白名单〕四条认得，第五样与旧链那形状各自拒在原话上")
        for ptr, want_kind in (("/shots/0/code", "code"), ("/shots/0/text", "text"),
                               ("/shots/0/source", "source"),
                               ("/shots/0/fields/radius/value", "field")):
            got = cp._parse_pointer(ptr)
            check(isinstance(got, tuple) and got[1] == want_kind,
                  f"{ptr} → 形态 {want_kind}：{got}")
            check(isinstance(got, tuple) and got[0] == 0,
                  f"{ptr} → 镜号下标 0：{got}")
        bad, why = "/shots/0/fields/radius/min", cp._parse_pointer("/shots/0/fields/radius/min")
        check(isinstance(why, str) and "不在白名单里" in why, f"{bad} 被拒：{str(why)[:52]}")
        check("min/max/step/label" in why,
              f"拒的时候顺带说清那几栏为什么不该改：{why[-58:]}")
        old = cp._parse_pointer("/shots/0/visual/levels/2/value")
        check(isinstance(old, str) and "不在白名单里" in old,
              f"旧链那形状（visual 里的下标值）在这里没有对应物：{str(old)[:48]}")
        check("不在镜头子树上" in str(cp._parse_pointer("/title")),
              "/title 拒在「不是镜头」上，而不是「不是那四条」")
        check("不在镜头子树上" in str(cp._parse_pointer("/shots/first/code")),
              "镜号写不成数字 → 同一条原话（含糊的「指针错了」教不了人）")
        check(tuple(p.replace("<i>", "{i}").replace("<name>", "{name}")
                    for p in cp.POINTER_GRAMMAR) == cs.EDITABLE_POINTERS,
              f"给人的语法与契约里那四条是同一份清单：{cp.POINTER_GRAMMAR}")
        tbl = cr._shot_table("s01", 0, (W, H), shot=_fshot(1), checks={})
        check(tbl["editable_pointers"] == list(cs.EDITABLE_POINTERS),
              f"命中表把四条指针原样发出去（前端不必自己拼）：{tbl['editable_pointers']}")

        # =========================================================
        print("\n〔补丁⑩·值校验〕与声明期同源：越界打回不钳值，同值重交直接拒")
        base10 = _spec([_fshot(1), _fshot(2)])
        got, perr = _plan(base10, edits=[{"pointer": "/shots/1/fields/radius/value",
                                          "value": 500}])
        check(got is None and "高于 max=300" in perr and "不钳值" in perr,
              f"越界打回，并把区间报回给人：{perr[:92]}")
        check("radius" in perr and "/shots/1/fields/radius/value" in perr,
              f"错误文本点名到那一格：{perr[:78]}")
        check(base10["shots"][1]["fields"][0]["value"] == 90.0,
              f"红的时候一个字节都没落（钳值就是把「表单 500、画上一个别的数」造出来）："
              f"{base10['shots'][1]['fields'][0]['value']}")
        _low = _plan(base10, edits=[{"pointer": "/shots/0/fields/radius/value",
                                     "value": 5}])[1]
        check("低于 min=20" in _low and "不钳值" in _low,
              f"下界同样打回（只防上界等于没防）：{_low[:70]}")
        check("要数字" in _plan(base10, edits=[{
            "pointer": "/shots/0/fields/radius/value", "value": "大一点"}])[1],
            "number 收到字符串 → 拒，不归一成 0")
        check("要写成 #rgb 或 #rrggbb" in _plan(base10, edits=[{
            "pointer": "/shots/0/fields/accent/value", "value": "red"}])[1],
            "color 只认十六进制（颜色名在这里没有对应字节）")
        check("要 true/false" in _plan(
            _spec([_fshot(1, fields=[dict(RADIUS), dict(GRID)])]),
            edits=[{"pointer": "/shots/0/fields/showGrid/value", "value": 1}])[1],
            "bool 收到 1 → 拒（Python 里 True == 1 是坑，闸⑥ 也按 is 比）")
        check("不在 options" in _plan(
            _spec([_fshot(1, fields=[dict(STYLE)])]),
            edits=[{"pointer": "/shots/0/fields/style/value", "value": "dotted"}])[1],
            "select 交了清单外的档位 → 拒（画面上根本没有这种画法）")
        unnamed = _plan(base10, edits=[{"pointer": "/shots/0/fields/glow/value",
                                       "value": 3}])[1]
        check("不在这一镜的清单里" in unnamed and "radius、accent" in unnamed,
              f"没声明的旋钮凭空改不了，并把现有清单摊开：{unnamed[:92]}")
        cross = _plan(base10, edits=[{"pointer": "/shots/0/fields/radius/value",
                                      "value": 120, "shot": "s02"}])[1]
        check("命中表与这一版分镜对不上" in cross,
              f"条目自报的镜号与指针的下标对不上 → 拒（拿旧表改新片会改错镜）：{cross[:84]}")

        landed, perr = _plan(base10, edits=[{"pointer": "/shots/0/fields/accent/value",
                                             "value": "#F60"}])
        check(perr == "", f"合法的写法差异照样落得下去：{perr[:80]}")
        check(landed[0]["shots"][0]["fields"][1]["value"] == "#ff6600",
              f"落盘前先归一（不然缓存键为同一个像素换写法）："
              f"{landed[0]['shots'][0]['fields'][1]['value']}")
        same = _plan(base10, edits=[{"pointer": "/shots/0/fields/radius/value",
                                     "value": 90}])[1]
        check("没有改动任何一格" in same,
              f"归一后与原题一样的值 → 整批拒收，不烧一次一模一样的片子：{same[:78]}")

        # =========================================================
        print("\n〔补丁⑪·串成一条〕表给的指针 → 装配 → 真契约 → 只重烧那一镜")
        base11 = _spec([_fshot(1), _fshot(2)])
        cache11, svc11 = FakeCache(), FieldService(echo=True)
        first11, err = await _run(base11, work=WORK / "pp1", client=svc11, cache=cache11)
        check(err is None and len(svc11.calls) == 2, f"首跑两镜都真烧：{err}")
        fentries = [e for e in first11["hitmap"]["s02"]["entries"]
                    if e["kind"] == "field"]
        check(len(fentries) == 2, f"表里两镜各两条字段：{len(fentries)}")
        check(all(not isinstance(cp._parse_pointer(e["pointer"]), str) for e in fentries),
              f"表里每一条字段指针装配器都认：{[e['pointer'] for e in fentries]}")
        check([cp._parse_pointer(e["pointer"])[2] for e in fentries]
              == [e["name"] for e in fentries],
              "指针里的字段名就是那一条的名字（按 name 定位，不靠数组下标）")
        rptr = next(e["pointer"] for e in fentries if e["name"] == "radius")
        landed, perr = _plan(base11, edits=[{"pointer": rptr, "value": 120, "shot": "s02"}])
        check(perr == "", f"照表取来的指针落得下去：{perr[:88]}")
        doc11, acct11 = landed
        check(acct11["changed_shots"] == ["s02"] and acct11["edits_applied"] == 1,
              f"改动账只记那一镜：{acct11}")
        _, verrors = cs.validate_code_spec(copy.deepcopy(doc11))
        check(verrors == [], f"装配结果再过一遍真契约（两层口径不能打架）：{verrors}")
        check(key_of(doc11, 0) == key_of(base11, 0),
              "没改的那一镜键一字节没动（按字节比）")
        check(key_of(doc11, 1) != key_of(base11, 1), "改过的那一镜键变了")

        second11, err = await _run(doc11, work=WORK / "pp2", client=svc11, cache=cache11)
        check(err is None, f"改版整链跑通：{err}")
        check(second11["cached_shots"] == 1 and len(svc11.calls) == 3,
              f"命中 1 镜、只多提交 1 镜：cached={second11['cached_shots']} / "
              f"calls={len(svc11.calls)}")
        pair = [(r["id"], bool(r.get("cached"))) for r in second11["shots"]]
        check(pair == [("s01", True), ("s02", False)], f"逐镜命中账：{pair}")
        check(second11["shots"][1]["field_checks"]["radius"]["expected"] == 120,
              f"重烧那一镜的闸⑥ 用的是新期望值：{second11['shots'][1]['field_checks']['radius']}")

        # =========================================================
        print("\n〔补丁⑫·闸① 复跑口径〕改值不敲那扇门，改代码必须敲")
        base12 = _spec([_fshot(1)])
        doc12 = _plan(base12, edits=[{"pointer": "/shots/0/fields/radius/value",
                                      "value": 120}])[0][0]
        check(cp.changed_codes(base12, doc12) == {},
              "改字段值不动代码字节 → 变更集是空的")
        lint = FakeLint()
        check(await cp.relint_codes(base12, doc12, lint=lint) == []
              and lint.calls == [],
              f"改值不重过闸①（代码没变，lint 结论不可能变，重跑只是白等十秒）：{lint.calls}")

        doc12b = _plan(base12, edits=[{"pointer": "/shots/0/code", "value": CODE_A}])[0][0]
        check(cp.changed_codes(base12, doc12b) == {"s01": CODE_A},
              "改代码 → 变更集带上那一镜的新字节")
        lint = FakeLint()
        errs = await cp.relint_codes(base12, doc12b, lint=lint)
        check(errs == [], f"lint 放行 → 无错：{errs}")
        check(lint.calls == [(CODE_A, ["radius", "accent"])],
              f"只敲一次，交的是那一镜的新代码 + 该镜的声明名单：{lint.calls}")

        lint = FakeLint(results=[{"ok": False, "errors": [
            {"rule": "field-declared-but-unread", "line": 7,
             "msg": "声明了 accent 但代码里没读"}]}])
        errs = await cp.relint_codes(base12, doc12b, lint=lint)
        check(len(errs) == 1 and errs[0].startswith("s01：闸① "),
              f"拒绝要带上镜号与闸号：{errs}")
        check("field-declared-but-unread" in errs[0] and "第 7 行" in errs[0]
              and "代码里没读" in errs[0], f"规则、行号、原话一条不落：{errs[0][:96]}")

        errs = await cp.relint_codes(base12, doc12b,
                                     lint=FakeLint(results=[{"ok": False}]))
        check(len(errs) == 1 and "没给出条目" in errs[0],
              f"拒绝却不说为什么 → 仍算红，并把原始回执摊出来：{errs}")
        errs = await cp.relint_codes(base12, doc12b, lint=FakeLint(boom=True))
        check(len(errs) == 1 and "闸① 复跑没跑成" in errs[0]
              and "旧版片子原样还在" in errs[0],
              f"外呼本身炸了不许当成验过了：{errs}")

        base12c = _spec([_fshot(1), _fshot(2)])
        doc12c = _plan(base12c, edits=[{"pointer": "/shots/0/code", "value": CODE_A},
                                       {"pointer": "/shots/1/fields/radius/value",
                                        "value": 120}])[0][0]
        check(cp.changed_codes(base12c, doc12c) == {"s01": CODE_A},
              "一批里既有改代码又有改值 → 只为前者敲闸①")
        lint = FakeLint()
        errs = await cp.relint_codes(base12c, doc12c, lint=lint)
        check(errs == [] and [c[0] for c in lint.calls] == [CODE_A],
              f"逐镜各敲各的，代码字节不许串行：{lint.calls}")
        two = _plan(base12c, edits=[{"pointer": "/shots/0/code", "value": CODE_A},
                                    {"pointer": "/shots/1/code", "value": CODE_B}])[0][0]
        lint = FakeLint()
        await cp.relint_codes(base12c, two, lint=lint)
        check([c[0] for c in lint.calls] == [CODE_A, CODE_B],
              f"两镜都改代码 → 两次外呼，一次都不许省：{[c[0][-14:] for c in lint.calls]}")

        # =========================================================
        print("\n〔补丁⑬·整镜改写〕能改的就是那六栏，旧链那几栏在这里没有对应物")
        base13 = _spec([_fshot(1)])
        perr = _plan(base13, shot_sets=[{"shot": "s01",
                                        "set": {"card": "dict_entry",
                                                "theme": {"accent": "#fff"}}}])[1]
        check("card、theme 不是这一镜有的栏" in perr,
              f"旧链那两栏（卡型 / 主题）一次拒干净，不是一栏一轮：{perr[:78]}")
        check("可改" in perr and "min_duration_sec" in perr,
              f"拒的时候把能改的清单摊开：{perr[-88:]}")
        check("不许带 id" in _plan(base13, shot_sets=[{"shot": "s01",
                                                      "set": {"id": "s09",
                                                              "text": "换一句"}}])[1],
              "镜号是命中表与缓存的锚点，改写不许顺手换号")
        check("非空对象" in _plan(base13, shot_sets=[{"shot": "s01", "set": {}}])[1],
              "空 set 是一次没有内容的改写 → 拒，不记成成功")
        check("不在这一版分镜里" in _plan(base13, shot_sets=[{"shot": "s07",
                                                             "set": {"text": "x"}}])[1],
              "改写一个不存在的镜 → 拒并列出这一版有哪些镜")

        got, perr = _plan(base13, shot_sets=[{"shot": "s01", "set": {"fields": [
            dict(RADIUS), dict(ACCENT),
            {"name": "halo", "type": "number", "label": "光晕", "value": 12}]}}])
        check(perr == "", f"给一镜补上原本没交的旋钮是合法入口：{perr[:80]}")
        doc13 = got[0]
        check([f["name"] for f in doc13["shots"][0]["fields"]] == ["radius", "accent", "halo"],
              f"新声明落在清单末尾：{[f['name'] for f in doc13['shots'][0]['fields']]}")
        check(got[1]["changed_shots"] == ["s01"] and got[1]["shot_sets_applied"] == 1,
              f"整镜改写进的是改动账而不是指针账：{got[1]}")
        _, verrors = cs.validate_code_spec(copy.deepcopy(doc13))
        check(verrors == [], f"补上的旋钮过得了真契约（含值域校验）：{verrors}")
        check(key_of(doc13, 0) != key_of(base13, 0),
              "多了一个注入值 → 缓存键变（这一镜的像素确实会不一样）")

        got, _ = _plan(base13, shot_sets=[{"shot": "s01",
                                          "set": {"source": {"标题": "换一条引用"}}}])
        doc13s = got[0]
        check(got[1]["changed_shots"] == ["s01"],
              f"改出处算内容变了（账要认）：{got[1]['changed_shots']}")
        check(key_of(doc13s, 0) == key_of(base13, 0),
              "但出处不进缓存键（改它不改像素 → 0 镜重烧）——账与键认的是两件事")

        # =========================================================
        print("\n〔补丁⑭·删镜与重排〕先落笔再重排；重排只换顺序、不改集合")
        base14 = _spec([_fshot(1), _fshot(2), _fshot(3)])
        got, perr = _plan(base14, remove_shots=["s02"])
        check(perr == "" and cp.shot_ids(got[0]["shots"]) == ["s01", "s03"],
              f"按镜号删，不按下标：{cp.shot_ids(got[0]['shots'])}")
        check(got[1]["removed_shots"] == ["s02"] and got[1]["changed_shots"] == [],
              f"删镜记在删的账上，不冒充内容改动：{got[1]}")
        check("全删了就没有片子了" in _plan(base14, remove_shots=["s01", "s02", "s03"])[1],
              "三镜全删 → 拒（要重做整片走 render_motion_code）")
        check("不在这一版里" in _plan(base14, remove_shots=["s09"])[1],
              "删一个不存在的镜号 → 拒，不静默少删一镜")

        got, perr = _plan(base14, reorder=["s03", "s01", "s02"])
        check(perr == "" and cp.shot_ids(got[0]["shots"]) == ["s03", "s01", "s02"],
              f"整套镜号 → 换序：{cp.shot_ids(got[0]['shots'])}")
        check(got[1]["reordered"] and got[1]["changed_shots"] == [],
              f"重排不该被记成内容改动（否则一镜像素白重烧）：{got[1]}")
        miss = _plan(base14, reorder=["s02", "s01"])[1]
        check("重排清单不成一套" in miss and "漏了 s03" in miss,
              f"少一个镜号就是「顺便删了」→ 拒并点名漏了谁：{miss[:88]}")
        check("重复 s01" in _plan(base14, reorder=["s01", "s01", "s02"])[1],
              "重复的号 → 拒（两个 s01 会撑出三份切片账）")
        check("这一版里没有 s09" in _plan(base14, reorder=["s01", "s09", "s03"])[1],
              "清单里多出一个不存在的号 → 拒")
        check("没有改动任何一格" in _plan(base14, reorder=cp.shot_ids(base14["shots"]))[1],
              "交一份与现状同序的清单 = 没改 → 拒，不出一个字节相同的版本")

        # 先落笔再重排：指针按**改前**的下标解析，落点不随最终顺序漂移。
        # （这里另起一份两镜夹具——三镜的片子只交两个号的重排清单会被「不成一套」
        # 那条拒掉，那是另一件事，本条要判的是先后次序。）
        base14b = _spec([_fshot(1), _fshot(2)])
        got, perr = _plan(base14b, edits=[{"pointer": "/shots/0/fields/radius/value",
                                           "value": 120}],
                          reorder=["s02", "s01"])
        check(perr == "", f"同批给指针与重排 → 装配器自己定先后：{perr[:80]}")
        doc14, acct14 = got
        check(cp.shot_ids(doc14["shots"]) == ["s02", "s01"],
              f"落笔之后才重排：{cp.shot_ids(doc14['shots'])}")
        check(doc14["shots"][1]["fields"][0]["value"] == 120.0
              and doc14["shots"][0]["fields"][0]["value"] == 90.0,
              "指针按**改前**的下标解析：落在原来那一镜（s01），不落在换序后的第 2 格")
        check(acct14["changed_shots"] == ["s01"] and acct14["reordered"],
              f"改动账按镜号记，与最终顺序无关：{acct14}")

        # =========================================================
        print("\n〔补丁⑮·节点级〕谁去敲那扇门，以及它在流水线的哪一步敲")
        # 上面那组判的是 `relint_codes` 本体；这一组判**节点**是否真的按口径去敲它——
        # 「模块自己做对了，节点没叫它做」是这类链最常见的假绿。
        base15 = _spec([_fshot(1)])
        doc15_val = _plan(base15, edits=[{"pointer": "/shots/0/fields/radius/value",
                                          "value": 120}])[0][0]
        doc15_code = _plan(base15, edits=[{"pointer": "/shots/0/code",
                                           "value": CODE_A}])[0][0]

        async def _node_gate(spec: dict, *, errors=(), lint=None) -> _SyncLint:
            """只跑 `_pre_gate` 那一步（整条 process 的编排由 ⑮ 末尾那条顺序断言盯）。"""
            l = lint or _SyncLint()
            await PatchCodeMotionVideoNode._pre_gate(
                types.SimpleNamespace(_client=lambda: {"lint": l}), None,
                base=base15, spec=spec, report={}, errors=list(errors))
            return l

        l = await _node_gate(doc15_val)
        check(l.calls == [], f"改值那一版：节点压根不出容器（省的那几秒是本钱）：{l.calls}")
        l = await _node_gate(doc15_code)
        check(l.calls == [(CODE_A, ["radius", "accent"])],
              f"改代码那一版：敲一次，交新字节 + 该镜声明名单：{l.calls}")
        l = await _node_gate(doc15_code, errors=["s01：这一镜不合法"])
        check(l.calls == [],
              f"分镜自己还红着就不去外呼（别让 lint 掩盖「哪一格改错了」）：{l.calls}")

        for label, linter in (
                ("lint 拒绝", _SyncLint(result={"ok": False, "errors": [
                    {"rule": "no-raw-fetch", "line": 3, "msg": "别自己 fetch"}]})),
                ("外呼炸了", _SyncLint(boom=True))):
            try:
                await _node_gate(doc15_code, lint=linter)
                raised = ""
            except ValueError as exc:
                raised = str(exc)
            check("闸① 复跑未通过" in raised and "没有落盘" in raised,
                  f"{label} → 节点抛错，这一版进不了产物作用域：{raised[:64]}")
            check("只想改画面上的数字请改对应的字段值指针" in raised,
                  f"退回原话把那条不改代码的通道指给人看：{raised[-58:]}")

        # 基类那一半什么都不做：闸① 是代码镜链带进来的东西，不该被拉去敲第二次。
        # 这个顺序位保留的理由是**下一族链**——钩子（数据）而不重写 process，
        # 才使装配 → 校验 → 复闸 → 分叉这一步序只有一份实现。
        check(await MotionPatchBase._pre_gate(
            types.SimpleNamespace(), None, base=base15, spec=doc15_code,
            report={}, errors=[]) is None,
            "基类那半不敲闸①（空转的钩子留给下一族链）")
        check(PatchCodeMotionVideoNode.process is MotionPatchBase.process,
              "子类没重写 process：局部改的步序只存在一份，漏一步的机会不在子类手里")

        src = inspect.getsource(MotionPatchBase.process)
        i_plan = src.index("self.patch_validator(")
        i_gate = src.index("self._pre_gate(")
        i_fork = src.index("await self._fork(")
        check(i_plan < i_gate < i_fork,
              f"顺序钉在共用编排里：装配 → 校验 → 复跑闸① → **才**分叉"
              f"（子类不许重写 process，重写就等于漏一步）：{i_plan} < {i_gate} < {i_fork}")

    asyncio.run(run())
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
