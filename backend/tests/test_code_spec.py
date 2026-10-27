# -*- coding: utf-8 -*-
"""代码镜契约与缓存键（新链 P2-b）。

这条文件只管两件事，各自钉一组「漂了看不出来」的错：

① `validate_code_spec` 拦的是**必然写坏画面**的四类：没代码、文案超字幕容量、
   高亮词不是文案子串、枚举不在白名单。代码**内容**对不对不在这里（闸① 容器 lint
   判像素前的语法，闸④ 判画面上有没有东西）。

② `shot_cache_key` 按字节算，所以断言也按字节算。缓存键漏一项的后果不是报错而是
   **静默命中旧切片**：改母版 token 全片换像素、换依赖版本全片换像素、改出处**不**换
   像素、加质感层**不**换镜切片。四类必须各一条用例把方向钉死——和 `44` / `44.0`
   那次键漂移同族，所以这里先证红再证绿，红绿都打出来。

③ 字段参数通道（spec §12）钉的是同一枚硬币的两面：**值变必须换键**（不改像素的通道
   就没有存在的意义），**话术变必须不换键**（`label`/`min`/`step` 改个写法就重烧一镜，
   等于把「改一个数字」的代价抬到「重烧一镜」）。所以这里按字节断言方向，闸⑥ 那条
   动态对账在 tests/test_code_fields.py。
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.motion import code_render as cr  # noqa: E402
from open_storyline.motion import code_spec as cs  # noqa: E402

_fails = 0
_checks = 0


def remotion_root() -> Path:
    """母版（TSX）源码树的位置：本机在仓库根 `remotion/`，app 镜像里是 Dockerfile
    单独 COPY 进来的 `/app/remotion`（只带 ⑤ 要比的那几个文件）。

    两处都没有就抛——「读不到就算过」等于把这条漂移闸变成空挂，正是本文件要消灭的那类红。
    """
    for cand in (Path("/app/remotion"), Path(__file__).resolve().parents[2] / "remotion"):
        if (cand / "src" / "tokens.ts").is_file() and (cand / "lint-rules.mjs").is_file():
            return cand
    raise FileNotFoundError(
        "母版源码树没找到（要 remotion/src/tokens.ts + remotion/lint-rules.mjs）："
        "本机在仓库根，容器里由 Dockerfile COPY 到 /app/remotion")


def skill_md() -> Path:
    """图形链教程的唯一载体：``motion_explainer`` 技能正文。

    写法纪律与整片开关的「怎么选」都只写在这一份里（schema 的散文送不到模型那里，
    见 ⑦ 节），所以它漏一条就等于那条纪律不存在。本机在
    ``backend/examples/retired/motion_explainer/SKILL.md``，app 镜像里是
    ``/app/examples/skills/…``。两处都没有就抛——「读不到就算过」会把这条闸变成空挂。
    """
    rel = Path("examples") / "retired" / "motion_explainer" / "SKILL.md"
    for cand in (Path("/app") / rel, Path(__file__).resolve().parents[1] / rel):
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"技能正文没找到（试过 /app 与 backend 两处）：{rel}")


def _pkg_root(pkg: str) -> str:
    """`katex/dist/katex.min.css` → `katex`；`@remotion/paths` 保持原样。"""
    parts = pkg.split("/")
    return "/".join(parts[:2]) if pkg.startswith("@") else parts[0]


def _exports_target(val: Any) -> str | None:
    """`exports` 的取值可能是字符串，也可能是 `{import/require/default: …}` 条件表。"""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for cond in ("import", "module", "default", "require"):
            got = _exports_target(val.get(cond))
            if got:
                return got
    return None


def _exports_file(node_modules: Path, pkg: str, sub: str) -> Path | None:
    """`pkg` 的 exports map 把 `./sub` 映到哪个文件（含 `./dist/*` 这类通配）。

    返回 None 表示 exports map **没声明**这条子路径。
    """
    pj = node_modules / pkg / "package.json"
    if not pj.is_file():
        return None
    try:
        ex = json.loads(pj.read_text(encoding="utf-8")).get("exports") or {}
    except (ValueError, OSError):
        return None
    if not isinstance(ex, dict):
        return None
    key = f"./{sub}"
    star = ""
    val = ex.get(key)
    if val is None:
        for k, v in ex.items():
            if isinstance(k, str) and k.endswith("/*") and key.startswith(k[:-1]):
                star = key[len(k) - 1:]
                val = v
                break
        else:
            return None
    tgt = _exports_target(val)
    if not tgt:
        return None
    # 通配映射的真实语义：`"./dist/*": "./dist/*"` 只是**允许**这个前缀上做映射，
    # 映到的那个文件还得真在。少了这一步，`katex/dist/nope.css` 会被判成「解析得到」。
    return node_modules / pkg / tgt.replace("*", star)


def _subpath_resolves(node_modules: Path, entry: str) -> bool:
    """白名单里的一条子路径能不能真的 import 到。

    **两种子路径，判据不是同一把尺**（§9-Q89）：
      · **包里的真文件**（`katex/dist/katex.min.css`）——`node_modules` 下那个路径是文件；
      · **exports map 的入口**（`@remotion/effects/paper`）——磁盘上**没有**这个路径，
        真身是 `dist/paper.mjs`，能不能 import 由包的 `exports` 说了算。
    只用第一把尺，好条目会被报成缺件；只用第二把尺，写错目录的真文件会漏过去。
    """
    item = node_modules / entry
    if item.is_file():
        return True
    root = _pkg_root(entry)
    tgt = _exports_file(node_modules, root, entry[len(root) + 1:])
    return bool(tgt) and (tgt.is_file() or (tgt / "index.mjs").is_file())


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _shot(**over) -> dict:
    # 夹具的代码体**必须真画东西**：闸⑦「每镜必画」逐镜判，一镜只有文字与色块就红。
    # 这里用最短的那条画法通道（裸 `<svg/>`，22 个骨架 token）——它同时停在闸⑫ 的
    # `_SHAPE_MIN_TOKENS=40` 豁免线以下，不会给无关的用例添一条相似度判据。
    # P1（2026-10-17）起还要过注入契约：读 `getInputProps`、接住 `text`（或摆
    # `<CaptionLayer/>`）。import 不计骨架 token（`canonical_shape` 先剥 import），
    # 所以这段代码仍远在 40 以下。
    base = {"id": "s01",
            "code": ("import {getInputProps} from \"remotion\"; "
                     "import {CaptionLayer} from \"@dg/film\"; "
                     "export const Shot = () => { const {text} = getInputProps(); "
                     "return (<AbsoluteFill><svg /><CaptionLayer />{text}</AbsoluteFill>); };"),
            "text": "中子星一茶匙物质重约十亿吨",
            "highlight": ["中子星"],
            "min_duration_sec": 6.0,
            "source": {"title": "维基：中子星", "url": "https://zh.wikipedia.org/wiki/中子星"},
            "card": "typography/paper-title-card",
            # 闸⑯：每一镜要说清画面主体是什么（具名名词短语，不是主题词），这一格缺了
            # 就是打回。`subject_channel` 与代码对齐——代码里用的是 `vector`，就报 `vector`。
            "subject": "中子星",
            "subject_channel": "vector",
            "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}
    base.update(over)
    return base


def _spec(**over) -> dict:
    base = {"title": "一茶匙中子星", "aspect": "portrait", "fps": 30,
            "narration": True, "voice": "zh-CN-YunjianNeural", "rate": "+0%",
            "caption_style": "on_picture", "highlight_style": "color",
            "texture": "none",
            "shots": [_shot(), _shot(id="s02", text="密度相当于把一座山压进方糖",
                                     highlight=["方糖"])]}
    base.update(over)
    return base


def _field(**over) -> dict:
    """一个合规的数字旋钮。"""
    base = {"name": "radius", "type": "number", "label": "半径", "value": 40,
            "min": 10, "max": 120, "step": 1, "unit": "px"}
    base.update(over)
    return base


def _shot_with_fields(**over) -> dict:
    """三旋钮齐活的镜：数字 / 颜色（故意写大写，验归一）/ 下拉。"""
    base = _shot(
        fields=[
            _field(),
            {"name": "accent", "type": "color", "label": "强调色", "value": "#F6F1E4"},
            {"name": "kind", "type": "select", "label": "手绘族", "value": "hachure",
             "options": ["hachure", "solid"]},
        ])
    base.update(over)
    return base


def main() -> int:
    print("① 契约：绿样例")
    spec, errors = cs.validate_code_spec(_spec())
    check(errors == [], f"合规分镜零错误（实际 {errors}）")
    check(spec.get("chain") == "remotion", "spec 打上 chain=remotion（逐镜账要认得出哪条链）")
    check(len(spec["shots"]) == 2, "两镜都收下")
    check(spec["width"] == 1080 and spec["height"] == 1920, "portrait → 1080×1920")
    check(spec["shots"][0]["min_duration_sec"] == 6.0, "min_duration_sec 原样收下")

    print("①-b 实体清单：归一、去重、封顶，且只是个提示词字段")
    spec_e, err_e = cs.validate_code_spec(_spec(shots=[_shot(entities=[
        "鲣鸟", " 巨型蛋 ", "鲣鸟", "", "三枚蛋", "草丛", "落日", "海面"])]))
    check(err_e == [], f"实体写得随意不报错：{err_e}")
    check(spec_e["shots"][0]["entities"] == ["鲣鸟", "巨型蛋", "三枚蛋", "草丛", "落日"],
          f"去空白、去重、只留前 {cs.MAX_ENTITIES_PER_SHOT} 个："
          f"{spec_e['shots'][0].get('entities')}")
    spec_s, err_s = cs.validate_code_spec(_spec(shots=[_shot(entities="鲣鸟、巨型蛋，草丛")]))
    check(err_s == [] and spec_s["shots"][0]["entities"] == ["鲣鸟", "巨型蛋", "草丛"],
          "一个字符串按顿号/逗号拆开也收")
    check("entities" not in cs.SHOT_KNOBS,
          "实体不进缓存键——改它不该让已经写好的代码重烧")
    props = cs.spec_schema()["properties"]["shots"]["items"]["properties"]
    check("entities" in props and props["entities"]["maxItems"] == cs.MAX_ENTITIES_PER_SHOT,
          "schema 里有 entities，maxItems 与运行时同一个数")
    import json as _json
    check("entities" not in _json.dumps(cs.knob_props(), ensure_ascii=False),
          "不进计划卡旋钮（自由值不上卡）")

    print("① 契约：四类红")
    _, e1 = cs.validate_code_spec(_spec(shots=[_shot(code="   ")]))
    check(bool(e1) and "既没有 brief 也没有 code" in e1[0],
          f"空 code 报错（2026-10-18 起 brief 也能代替代码，话术跟着改）：{e1}")

    long_text = "这句话故意长到超过竖屏字幕条两行的容量" * 5
    _, e2 = cs.validate_code_spec(_spec(shots=[_shot(text=long_text)]))
    check(bool(e2) and "字幕条放不下" in " ".join(e2), f"文案超限报错：{e2}")

    # 2026-10-19 真机：一个高亮词不是文案子串，整份分镜被打回重交。
    # 它只决定「这个词亮不亮」，丢掉即可，不值得让模型重写 24 镜。
    spec_h, e3 = cs.validate_code_spec(_spec(shots=[_shot(highlight=["夸克"])]))
    check(not e3 and spec_h["shots"][0]["highlight"] == [],
          f"高亮词不是子串就丢掉、不再报错：{e3}")

    _, e4 = cs.validate_code_spec(_spec(caption_style="bottom"))
    check(bool(e4) and "caption_style" in " ".join(e4),
          f"旧链那档 bottom 在新链不是合法值：{e4}")
    check("bottom" not in cs.CAPTION_STYLES, "CAPTION_STYLES 里没有母版没实现的形态")
    _, e4h = cs.validate_code_spec(_spec(highlight_style="torn_highlight"))
    check(bool(e4h) and "highlight_style" in " ".join(e4h),
          f"两维各按自己的白名单判——旧合体的档名不是高亮那一维的值：{e4h}")
    check(not {"torn_highlight", "inline", "full"} & set(cs.HIGHLIGHT_STYLES),
          "高亮那一维里没有旧链合体的档名：纸条底与逐词点亮拆开之后各归各的维")

    _, e5 = cs.validate_code_spec(_spec(shots=[_shot(code="//" + "x" * cs.MAX_CODE_BYTES)]))
    check(bool(e5) and "字节" in " ".join(e5), f"代码超体积上限报错：{e5}")

    _, e6 = cs.validate_code_spec(_spec(aspect="banana"))
    check(bool(e6) and "aspect" in " ".join(e6), f"认不出的画幅照样报错：{e6}")

    print("①-a 画幅写法归一：常见写法不白烧一份分镜")
    # 2026-10-18 真机：模型第一次就把横屏写成 `16:9`，一句校验打回，整份 24 镜
    # 分镜从头重生成了一遍。常见写法服务端认，认不出的才打回。
    for alias, want in (("16:9", "landscape"), ("16x9", "landscape"),
                        ("1920x1080", "landscape"), ("1920×1080", "landscape"),
                        ("4:3", "landscape"), ("2.39:1", "landscape"),
                        ("横屏", "landscape"), ("横向", "landscape"),
                        ("9:16", "portrait"), ("1080x1920", "portrait"),
                        ("竖屏", "portrait"), ("vertical", "portrait"),
                        ("1:1", "square"), ("方屏", "square"),
                        ("LANDSCAPE", "landscape"), (" landscape ", "landscape")):
        out, errs = cs.validate_code_spec(_spec(aspect=alias))
        check(not errs and out.get("aspect") == want,
              f"aspect={alias!r} 归一成 {want}（实得 {out.get('aspect')}，错误 {errs[:1]}）")
    out_l, _ = cs.validate_code_spec(_spec(aspect="16:9"))
    check((out_l["width"], out_l["height"]) == (1920, 1080),
          "16:9 归一之后像素尺寸也跟着是 1920×1080")
    check(cs.norm_aspect("") == "" and cs.norm_aspect("banana") == "",
          "认不出的写法返回空串，不拿默认值蒙过去")

    _, e7 = cs.validate_code_spec(_spec(rate="fast"))
    check(bool(e7) and "rate" in " ".join(e7), f"语速写法不对报错：{e7}")

    # 正常语速是下限：负档位从可选清单拿掉，历史分镜里写过的负值静默归零。
    check(not any(str(x).startswith("-") for x in cs.RATE_CHOICES),
          f"语速可选档位里不再有负档：{cs.RATE_CHOICES}")
    slow, e_slow = cs.validate_code_spec(_spec(rate="-8%"))
    check(not e_slow and slow.get("rate") == "+0%",
          f"-8% 归到正常语速（实得 {slow.get('rate')}，错误 {e_slow[:1]}）")

    _, e8 = cs.validate_code_spec("不是对象")
    check(e8 == ["spec 必须是对象"], f"非对象输入返回二元组的第二项：{e8}")
    check(isinstance(e8, list), "validate 的错误项始终是 list（不是字符串）")

    print("①-b 字段声明：绿样例")
    spec_f, errors_f = cs.validate_code_spec(
        _spec(shots=[_shot_with_fields(),
                     _shot(id="s02", text="密度相当于把一座山压进方糖", highlight=["方糖"])]))
    check(errors_f == [], f"三旋钮齐活的镜零错误（实际 {errors_f}）")
    fs = spec_f["shots"][0]["fields"]
    check([f["name"] for f in fs] == ["radius", "accent", "kind"],
          f"声明顺序原样保留（界面按这个顺序出表单）：{[f['name'] for f in fs]}")
    check(fs[1]["value"] == "#f6f1e4",
          f"颜色归一成小写六位（大写与缩写都不该重烧一镜）：{fs[1]['value']}")
    check(cs.field_values(spec_f["shots"][0]) == {"radius": 40, "accent": "#f6f1e4",
                                                  "kind": "hachure"},
          f"field_values 就是那张 name→value 扁表：{cs.field_values(spec_f['shots'][0])}")
    check(fs[0]["min"] == 10.0 and fs[0]["unit"] == "px",
          "数字旋钮的界与单位随声明一起交给界面（控件的单一来源是 type）")
    check(spec_f["shots"][1]["fields"] == [],
          "没交旋钮的镜归一后是空的 list（不是缺键）——界面据此显示"
          "「这一镜没交出可改的旋钮」，而不是一个空白页")

    print("①-b 字段声明：红")
    def errs_for(*fields: dict) -> list[str]:
        _, e = cs.validate_code_spec(_spec(shots=[_shot_with_fields(fields=list(fields))]))
        return e

    e = errs_for(_field(name="半径"))
    check("不是合法 JS 标识符" in " ".join(e), f"中文 name 写不出 fields.半径：{e}")

    e = errs_for(_field(name="text"))
    check("保留键" in " ".join(e), f"撞母版注入的保留键会在 props 里覆盖掉字幕带的 text：{e}")

    e = errs_for(_field(label=""))
    check("没有 label" in " ".join(e), f"没 label 的表单只能拿机器名当标题：{e}")

    e = errs_for(_field(type="slider"))
    check("不在白名单" in " ".join(e), f"第六种类型会落成一个没人认识的输入框：{e}")

    e = errs_for(_field(value="40"))
    check("不是数字" in " ".join(e), f"数字旋钮收到字符串：{e}")

    e = errs_for(_field(value=200))
    check("高于 max" in " ".join(e), f"声明期越界当场打回：{e}")
    spec_drop, _ = cs.validate_code_spec(
        _spec(shots=[_shot_with_fields(fields=[_field(value=200)])]))
    check(spec_drop["shots"][0]["fields"] == [],
          "越界的值**没有**被钳成 120 收下——钳了就是账上写 200、画上是 120，"
          "而这条通道存在的理由恰恰是消灭这种不一致")

    e = errs_for(_field(min=120, max=10))
    check("min=" in " ".join(e) and "这样的区间一个值都收不下" in " ".join(e),
          f"min>max 收不下任何值：{e}")

    e = errs_for(_field(step=0))
    check("step=0" in " ".join(e), f"步长 0 的滑杆拖不动：{e}")

    e = errs_for({"name": "accent", "type": "color", "label": "强调色", "value": "红色"})
    check("#rgb" in " ".join(e), f"颜色名在这里没有对应字节：{e}")

    e = errs_for({"name": "cap", "type": "text", "label": "标题", "value": "字" * 121})
    check("121 字" in " ".join(e), f"超长文案塞不进单行控件，应拆回代码：{e}")

    e = errs_for({"name": "glow", "type": "bool", "label": "发光", "value": 1})
    check("true/false" in " ".join(e), f"bool 收到 1（bool 与 int 在 Python 里同族，必须严判）：{e}")

    e = errs_for({"name": "kind", "type": "select", "label": "手绘族", "value": "a"})
    check("没有 options" in " ".join(e), f"select 没有候选，下拉框是空的：{e}")

    e = errs_for({"name": "kind", "type": "select", "label": "手绘族", "value": "zigzag",
                  "options": ["hachure", "solid"]})
    check("不在 options" in " ".join(e), f"初值不在候选里：{e}")

    many = [_field(name=f"k{i}") for i in range(cs.MAX_FIELDS_PER_SHOT + 1)]
    e = errs_for(*many)
    check(f"{len(many)} 个 >" in " ".join(e), f"一屏放不下的表单没人改得动：{e}")
    spec_many, _ = cs.validate_code_spec(_spec(shots=[_shot_with_fields(fields=many)]))
    check(len(spec_many["shots"][0]["fields"]) == cs.MAX_FIELDS_PER_SHOT,
          f"超上限截断到 {cs.MAX_FIELDS_PER_SHOT}（同时报错，不静默丢）")

    e = errs_for(_field(), _field(label="换个说法"))
    check("出现两次" in " ".join(e), f"同名后一个会覆盖前一个，表单上只剩一行：{e}")
    spec_dup, _ = cs.validate_code_spec(
        _spec(shots=[_shot_with_fields(fields=[_field(), _field(label="换个说法")])]))
    check(len(spec_dup["shots"][0]["fields"]) == 1, "同名只留第一个")

    _, e = cs.validate_code_spec(_spec(shots=[_shot(fields={"radius": _field()})]))
    check("fields 必须是数组" in " ".join(e),
          f"写成对象顺序就没了，表单顺序不能靠 JSON 的键序：{e}")

    print("①-b 声明期与补丁口同源：两边规则漂一次，就会出现「新建被拒的值改回来却能写进去」")
    num = fs[0]
    check(cs.check_field_value(num, 200) is not None and "不钳值" in cs.check_field_value(num, 200),
          f"补丁口同样打回越界值：{cs.check_field_value(num, 200)}")
    check(cs.check_field_value(num, 41) is None, "界内的值放行")
    check(cs.check_field_value(num, "41") is not None, "字符串数字在补丁口也拒")
    check(cs.check_field_value(fs[1], "#abc") is None, "三位缩写 hex 收（归一在声明期做过）")
    check(cs.check_field_value(fs[1], "红色") is not None, "颜色名在补丁口也拒")
    check(cs.check_field_value(fs[2], "solid") is None, "候选内的 select 值放行")
    check(cs.check_field_value(fs[2], "zigzag") is not None, "候选外打回")
    check(cs.check_field_value({"name": "x", "type": "meter", "label": "x", "value": 1}, 1)
          is not None, "不认识的类型也拒（不让补丁口成一个没人校验的口子）")
    for proto, bad_value, why in (
        (_field(), 200, "越界数字"),
        ({"name": "c", "type": "color", "label": "色", "value": "#ffffff"}, "红色", "颜色名"),
        ({"name": "k", "type": "select", "label": "族", "value": "a",
          "options": ["a"]}, "zigzag", "候选外"),
    ):
        decl_errs = errs_for({**proto, "value": bad_value})
        patch_err = cs.check_field_value(proto, bad_value)
        check(bool(decl_errs) and patch_err is not None,
              f"同一份规则判 {why}：声明期拒（{decl_errs[:1]}）↔ 补丁口拒（{patch_err}）")

    print("② 缓存键：同输入同键")
    s, _ = cs.validate_code_spec(_spec())
    shot = s["shots"][0]

    def film(**over) -> dict:
        # `art` 是可省的一栏（没写就不在 spec 里）——用 .get，缺了就是 None。
        f = {k: s.get(k) for k in cs.FILM_KNOBS}
        f.update(over)
        return f

    FILM = film()
    key = cs.shot_cache_key(shot, film=FILM, master_hash="a" * 16, lockfile_hash="b" * 16)
    key2 = cs.shot_cache_key(shot, film=FILM, master_hash="a" * 16, lockfile_hash="b" * 16)
    check(key == key2, "字节级幂等：同镜同参数两次算出同一个键")
    check(len(key) == 16, f"键长 16（当前 {len(key)}）")

    print("② 缓存键：换了像素必须换键")
    k_code = cs.shot_cache_key({**shot, "code": shot["code"] + " "},
                               film=FILM, master_hash="a" * 16, lockfile_hash="b" * 16)
    check(k_code != key, "代码多一个空格也换键（键按字节，不看语法树）")

    k_text = cs.shot_cache_key({**shot, "text": shot["text"] + "。"},
                               film=FILM, master_hash="a" * 16, lockfile_hash="b" * 16)
    check(k_text != key, "文案变 → 字幕带像素变 → 换键")

    k_m = cs.shot_cache_key(shot, film=FILM, master_hash="c" * 16, lockfile_hash="b" * 16)
    check(k_m != key, "母版哈希变 → 全片换像素 → 换键（漏了这项会命中旧母版切片）")

    k_l = cs.shot_cache_key(shot, film=FILM, master_hash="a" * 16, lockfile_hash="d" * 16)
    check(k_l != key, "依赖锁变（如 three.js 换版）→ 换键")

    for knob, value in (("fps", 24), ("aspect", "landscape"), ("narration", False),
                        ("voice", "zh-CN-XiaoxiaoNeural"), ("rate", "+20%"),
                        ("caption_style", "none"),
                        # R7-T22：进出场是母版逐帧画进切片的，与字幕同族。
                        ("entrance", "rise"), ("exit", "fade"),
                        # 高亮那一维也画进切片：换一个字色就是换像素。
                        ("highlight_style", "plain"),
                        # 用户钉的强调色同理，而且更直白——它改的就是那几个词的颜色。
                        # 不进键的后果是「换了颜色整片还是上一版的颜色」，看起来像没保存成功。
                        ("highlight_color", "#ff8a3d"),
                        # 2026-10-17：整片色板。这一项与上面 `highlight_color` 是同一族
                        # （改的是每一镜的底色与字幕三色），只是改的面更大——它换的是
                        # 「这一镜认不出自己的色板时用哪一套」。
                        ("palette", "deep-ocean"),
                        # 2026-10-18：整片美术方向。它改两处像素——写手按它重写每一镜
                        # 的代码，以及切片末段那趟统一调色（`render.art_grade`）。
                        ("art", {"brush": "watercolor", "light": "top-left"}),
                        # 2026-10-19：整片主角。身高/脚底线/朝向/步频决定每一镜画出来
                        # 的像素；不进键的话改主角只会重写新镜，旧切片还是上一版的身材。
                        ("protagonist", {"look": "瘦高圆头的剪影", "height": 0.52})):
        k = cs.shot_cache_key(shot, film=film(**{knob: value}),
                              master_hash="a" * 16, lockfile_hash="b" * 16)
        check(k != key, f"整片开关 {knob} 变 → 换键（切片自带音轨、字幕带是像素）")

    check(set(cs.FILM_KNOBS) == {"fps", "aspect", "narration", "voice", "rate",
                                 "caption_style", "highlight_style", "highlight_color",
                                 "palette", "entrance", "exit", "art", "protagonist"},
          f"FILM_KNOBS 就是上面逐条验过的那十三项：{cs.FILM_KNOBS}")
    for knob in ("texture", "transition", "transition_sec"):
        k = cs.shot_cache_key(shot, film=film(**{knob: "film" if knob == "texture"
                                                 else "0.9"}),
                              master_hash="a" * 16, lockfile_hash="b" * 16)
        check(k == key,
              f"整片开关 {knob} 变 → **不**换键（质感和转场跑在缓存之后的那一趟，"
              "换档不重烧任何一镜）")

    print("② 缓存键：不换像素的改动不许换键")
    k_src = cs.shot_cache_key({**shot, "source": {"title": "换了个引用"}},
                              film=FILM, master_hash="a" * 16, lockfile_hash="b" * 16)
    check(k_src == key, "改出处不改像素 → 同键（进了键＝换一条引用重烧一镜）")
    check("source" not in cs.SHOT_KNOBS and "min_duration_sec" in cs.SHOT_KNOBS,
          "SHOT_KNOBS 白名单与上面那条断言同源")

    k_tex = cs.shot_cache_key(shot, film={**FILM, "texture": "tv"},
                              master_hash="a" * 16, lockfile_hash="b" * 16)
    check(k_tex == key, "换质感 → 0 镜重烧（texture 跑在取到切片之后的那一趟逐镜滤镜上，"
                        "故意不进键；逐镜覆盖同理）")
    check("texture" not in cs.FILM_KNOBS, "texture 不在 FILM_KNOBS 里（上面那条才成立）")

    k_tr = cs.shot_cache_key(shot, film={**FILM, "transition": "wipeleft",
                                         "transition_sec": "0.9"},
                             master_hash="a" * 16, lockfile_hash="b" * 16)
    check(k_tr == key, "换转场 → 0 镜重烧（T22：转场跑在拼接那一趟的 ffmpeg xfade 上，"
                       "逐镜切片一个字节不动——与 texture 同一个理由，只是换了一层）")
    check("transition" not in cs.FILM_KNOBS and "transition_sec" not in cs.FILM_KNOBS,
          "转场那两栏不在 FILM_KNOBS 里（上面那条才成立）")

    print("② 缓存键：字段通道（值变换键、话术变换不了键）")
    shot_f = spec_f["shots"][0]

    def _key_of(shot: dict) -> str:
        return cs.shot_cache_key(shot, film=FILM, master_hash="a" * 16,
                                 lockfile_hash="b" * 16)

    def _patched(name: str, **over) -> dict:
        fs2 = [{**f, **over} if f["name"] == name else f for f in shot_f["fields"]]
        return {**shot_f, "fields": fs2}

    base_f = _key_of(shot_f)
    check("fields" in cs.SHOT_KNOBS, f"SHOT_KNOBS 里确有 fields：{cs.SHOT_KNOBS}")
    for name, over, why in (("radius", {"value": 41}, "数字 40→41"),
                            ("accent", {"value": "#111111"}, "强调色换值"),
                            ("kind", {"value": "solid"}, "手绘族换候选")):
        check(_key_of(_patched(name, **over)) != base_f,
              f"{why} → 换键（这一镜的画面本就该重烧，只有它自己）")

    for name, over, why in (("radius", {"label": "半径像素"}, "改标题话术"),
                            ("radius", {"min": 1}, "改下限"),
                            ("radius", {"max": 999}, "改上限"),
                            ("radius", {"step": 5}, "改步长"),
                            ("radius", {"unit": "px→"}, "改单位后缀")):
        check(_key_of(_patched(name, **over)) == base_f,
              f"{why}不改像素 → 同键（整份声明进键就会为一句话术重烧一镜）")

    reordered = {**shot_f, "fields": list(reversed(shot_f["fields"]))}
    check(_key_of(reordered) == base_f,
          "字段顺序只决定表单排布，不进键（扁表按 name 排序）")

    check(_key_of(_patched("radius", value=cs.normalize_field_value("number", 41)))
          == _key_of(_patched("radius", value=cs.normalize_field_value("number", 41.0))),
          "数字 41 与 41.0 归一后同键（旧链 44/44.0 那次键漂移的同族）")
    check(_key_of(_patched("accent", value=cs.normalize_field_value("color", "#F6F1E4")))
          == base_f, "颜色大小写归一后同键（同一个颜色不该烧两次）")

    no_fields = {k: v for k, v in shot_f.items() if k != "fields"}
    check(_key_of({**no_fields, "fields": []}) == _key_of(no_fields),
          "fields 缺省与空表同键（没交旋钮的镜不因归一方式不同而换键）")

    print("②-b 风格档：色板先认代码里那句绑定，认不出才拿底色反查")
    # 这一节的病：`bg` 是可省略的（只有 bleed 镜必须给，闸⑲ 的 VARIETY_BG_SHARE
    # 也只要求 ≥25%），而派生三色原先只挂在「申报了 bg」这条路上——于是多数镜的字幕条
    # 根本不跟着色板走，一部片子里一半新一半旧。修法是把来源换成代码里那句绑定
    # （`shot_colors()["palette"]`，闸⑩/⑲ 本来就走它），bg 反查只作兜底。
    BOUND = ('import palettes from "@dg/themes/palettes.json";\n'
             'const palette = palettes["coral-burst"];\n'
             'export const Shot = () => (<FilmLayout><div/></FilmLayout>);')
    PLAIN = 'export const Shot = () => (<FilmLayout><div/></FilmLayout>);'
    CORAL = {"paper": "#ffffff", "ink": "#1f1235", "accent": "#d94e59"}
    DEEP = {"paper": "#142735", "ink": "#e8f1f5", "accent": "#7cc8b4"}

    fs_bound = cs.film_style_of({"code": BOUND})
    check(fs_bound == {"bg": "#fffffe", **CORAL},
          f"绑了 coral-burst 的镜连**底色**一起给出（先说这一条要修的病：绑了色板而底色"
          f"还停在 TOKENS 的米白，一整片里就它一个色）：{fs_bound}")
    fs_both = cs.film_style_of({"code": BOUND, "bg": "#dddddd"})
    check(fs_both == {"bg": "#dddddd", **CORAL},
          f"但**申报过的底色赢**：它改的只是 bg 那一栏，三件套一字不变"
          f"（说的是「这一镜明写的底色」，与色板本身无关）：{fs_both}")
    check(cs.film_style_of({"code": PLAIN}) == {},
          "没绑色板、没申报底色 → 空档（既有镜的键一个字节不变，不白烧一帧）")
    fs_bg = cs.film_style_of({"code": PLAIN, "bg": "#0b1620"})
    check(fs_bg == {"bg": "#0b1620", **DEEP},
          f"没绑色板时按底色反查到 deep-ocean（兜底那条路仍通）：{fs_bg}")
    fs_white = cs.film_style_of({"code": PLAIN, "bg": "#ffffff"})
    check(fs_white == {"bg": "#ffffff"},
          f"#ffffff 是四套色板的 surface——撞色不猜，退回按亮度二档：{fs_white}")
    fs_ghost = cs.film_style_of({"code": BOUND.replace("coral-burst", "warm-cream")})
    check(fs_ghost == {},
          f"绑了个 palettes.json 里没有的名字——认不出就不猜，落回兜底也没命中：{fs_ghost}")
    check(cs.shot_cache_key({**shot, "bg": "#0b1620"}, film=FILM,
                            master_hash="a" * 16, lockfile_hash="b" * 16) != key,
          "申报底色 → 风格档进键 → 换键（底色与字幕条都是像素，命中旧切片就是拿旧片冒充新片）")
    check({"paper", "captionInk", "accent", "hl"} <= cr.TOKEN_PER_SHOT_KEYS,
          f"母版回读的那几栏逐镜色都在闸⑤ 白名单里：{sorted(cr.TOKEN_PER_SHOT_KEYS)}")
    check("ink" not in cr.TOKEN_PER_SHOT_KEYS,
          "母版常量 ink 不在白名单里——闸⑤ 正是拿它比全片一致性")

    print("③ 枚举单源：旧链已删，这一份就是唯一出处；另一半在渲染侧的表上")
    from open_storyline.motion import render as rd  # noqa: E402
    legacy_spec = (Path(__file__).resolve().parents[1]
                   / "open_storyline" / "motion" / "spec.py")
    check(not legacy_spec.exists(),
          "声明式那份契约文件不在树上了——同一批整片开关没有第二处可漂")
    check(cs.FPS_CHOICES == (10, 15, 24, 30),
          f"fps 白名单就是这一份死清单：{cs.FPS_CHOICES}")
    check(cs.MAX_TEXT_CHARS == 60 and cs.MAX_TEXT_CHARS_WIDE == 110,
          f"字幕容量（竖屏两行 / 横屏一行）：{cs.MAX_TEXT_CHARS} / {cs.MAX_TEXT_CHARS_WIDE}")
    check(not {"torn", "bottom"} & set(cs.CAPTION_STYLES),
          "旧链的 torn / bottom 两档已删——母版 <FilmLayout> 没实现就不该在选项里")

    # 质感档名在这里列一遍、能渲出来的滤镜串在 render 那边列一遍。漂了不报错：
    # 契约有这一档而表里没有 → apply_texture 原样拷贝，用户挑了「老胶片」看的是原片。
    check(set(cs.TEXTURES) - {"none"} == set(rd.TEXTURE_FILTERS),
          f"每一档都真渲得出来（契约 {cs.TEXTURES} / 滤镜表 {sorted(rd.TEXTURE_FILTERS)}）")
    check(cs.TEXTURES[0] == "none" and list(cs.TEXTURES[1:]) == list(rd.TEXTURE_FILTERS),
          f"顺序也钉住，卡面按契约顺序列档：{cs.TEXTURES}")

    print("④ 选区改指针：五条，其中字段值与质感两条不重过闸①")
    check(set(cs.EDITABLE_POINTERS) == {"/shots/{i}/code", "/shots/{i}/text",
                                        "/shots/{i}/source",
                                        "/shots/{i}/fields/{name}/value",
                                        "/shots/{i}/texture"},
          f"代码镜可编辑面 = 代码/文案/出处/字段值/质感档：{cs.EDITABLE_POINTERS}")
    check(any("{name}" in p for p in cs.EDITABLE_POINTERS),
          "字段值指针按 name 定位（补丁口据此拿声明判类型与界；代码字节没动，"
          "所以这一条不重跑 lint，只有 /code 那条必须重过闸①）")

    print("⑤ 母版与契约同源：两处各列一遍的枚举，漂了不会报错")
    # 离线读母版源码，不发 HTTP。漂移的后果是「界面上有这一档、渲出来没有」——
    # 结构上不可能抛错，只能在这里对一次。
    # 容器里读的是 Dockerfile COPY 进来的那份：它只可能因「改了 TSX 却没重烤 app 镜像」
    # 而变旧，变旧的红法是拿新契约比旧拷贝→报红，不是静默过。
    tokens_ts = (remotion_root() / "src" / "tokens.ts").read_text(encoding="utf-8")

    # 字幕两维（2026-10-16 拆开）：两处各列一遍，漂了不抛错，只能在这里对一次。
    def _enum_in_ts(name: str, want: tuple[str, ...]) -> tuple[str, ...]:
        m = re.search(r"const " + name + r" = \[([^\]]*)\] as const", tokens_ts)
        got = tuple(re.findall(r"'([^']+)'", m.group(1))) if m else ()
        check(got == tuple(want),
              f"{name} 与母版逐字相等：容器 {list(got)} vs 契约 {list(want)}")
        return got

    _enum_in_ts("CAPTION_STYLES", cs.CAPTION_STYLES)
    _enum_in_ts("HIGHLIGHT_STYLES", cs.HIGHLIGHT_STYLES)

    master_aspects = {k: (int(w), int(h)) for k, w, h in re.findall(
        r"(\w+): \{ width: (\d+), height: (\d+) \}", tokens_ts)}
    check(master_aspects == dict(cs.ASPECTS),
          f"画幅像素与母版相等：{master_aspects} vs {dict(cs.ASPECTS)}")

    # import 白名单在两个语言里各写一遍：闸① 拿 mjs 那份判退，模型拿 Python 那份写。
    # 漂了的两种红法都不会自己跳出来：名单少了模型写了被退回（看得懂），
    # 名单多了**闸放行、打包期才炸**（看不懂）。这次的真缺陷是后者。
    rr = remotion_root()
    lint_mjs = (rr / "lint-rules.mjs").read_text(encoding="utf-8")
    wl_m = re.search(r"export const ALLOWED_IMPORTS = new Set\(\[(.*?)\]\)",
                     lint_mjs, re.S)
    # 先剥 `//` 注释再认条目：名单旁边那段注释里会**引到**某些名字（比如解释
    # 「为什么不放行包名 `@remotion/effects`」时写了一句 `import ... from '@remotion/effects'`），
    # 不剥就把注释当条目收进来，报出一堆名单里根本没有的名字。
    wl_block = re.sub(r"//[^\n]*", "", wl_m.group(1)) if wl_m else ""
    mjs_wl = set(re.findall(r"'([^']+)'", wl_block))
    # D-红先：剥注释这一步自己先抓住一次「该拒的」——一段注释里引到的包名**不许**
    # 被算成名单条目。（真事故：加了一行解释「为什么不放行包名 `@remotion/effects`」
    # 的注释之后，43 项被读成 45 项，报出一堆名单里根本没有的名字。）
    _probe_block = re.sub(r"//[^\n]*", "",
                          "// 见 '@remotion/effects' 与 'three' 那段\n'alpha', 'beta',")
    check(set(re.findall(r"'([^']+)'", _probe_block)) == {"alpha", "beta"},
          "名单旁边的注释里引到的包名不会被当成名单条目（构造样本先证一次红）")
    check(mjs_wl == set(cs.IMPORT_WHITELIST),
          f"import 白名单两侧逐项相等：闸① {sorted(mjs_wl)} vs 契约 "
          f"{sorted(cs.IMPORT_WHITELIST)}")
    check(", ".join(cs.IMPORT_WHITELIST) in cs._CODE_DOC,
          "契约文档里回显的就是这份名单（拼两遍必然漂，所以从常量拼）")

    # 白名单的唯一判据是「镜像里真装着」——npm 上不存在的名字进了名单，
    # 闸① 就等于放行一条注定解析失败的 import。
    deps = set(json.loads((rr / "package.json")
                          .read_text(encoding="utf-8"))["dependencies"])

    not_installed = {_pkg_root(p) for p in cs.IMPORT_WHITELIST
                     if not p.startswith("@dg/")} - deps
    check(not_installed == set(),
          f"白名单里每一项的包都真装进镜像了（没装的：{sorted(not_installed)}）")
    check(deps >= {"@remotion/bundler", "@remotion/renderer"},
          f"服务端自己用的打包/渲染包在依赖里而**不在**白名单：{sorted(deps)}")

    # `@remotion/preload` 三个地方都该没有它（Q12：不装）。这条原本只是三处注释，
    # 而注释拦不住「下次顺手补全」——真装进来的那天，代价是构建期多一个浏览器侧包
    # 和一条**没人需要**的预热路径，而且它会顶在这张表里，让人以为它是必需件。
    for where, got in (("package.json 依赖", deps),
                       ("契约白名单", set(cs.IMPORT_WHITELIST)),
                       ("闸① 白名单", mjs_wl)):
        check(not [p for p in got if p == "@remotion/preload"
                   or p.startswith("@remotion/preload/")],
              f"`@remotion/preload` 不在{where}里（Q12：渲染不需要它，装了也没人调）")

    # 子路径那一条例外（katex 的样式表）：包名对了不代表文件在——写错一层目录，
    # lint 与 dependencies 两边都不报错，要到打包期才炸，而画面上只是「公式没样式」。
    #
    # **子路径有两种，判据不能用同一把尺**（§9-Q89）：
    #   · **包里的真文件**（`katex/dist/katex.min.css`）——判「node_modules 下这个路径
    #     是个真文件」；
    #   · **exports map 的入口**（`@remotion/effects/paper`）——磁盘上**没有**这个路径，
    #     真身是 `dist/paper.js`，能不能 import 由包的 `package.json` 的 `exports` 说了算。
    #     拿「是不是真文件」去判它，会把一条**好**条目报成缺。
    # 两种都得判：第一把尺漏掉 exports 入口（比如 `@remotion/effects/nonexistent` 是
    # 好名字却解析不了），第二把尺漏掉真文件。
    subpaths = [p for p in cs.IMPORT_WHITELIST
                if not p.startswith("@dg/") and _pkg_root(p) != p]
    nm = rr / "node_modules"
    if nm.is_dir():
        missing = [p for p in subpaths if not _subpath_resolves(nm, p)]
        check(not missing,
              f"子路径那几条都能解析（真文件在 node_modules 下，或包的 exports map "
              f"里声明了它；缺：{missing}）")
        # D-红先：这条判据自己得先抓住一次「该拒的」——上面那条只有在名单写错时才红，
        # 而那要等真出事。拿三个**构造样本**当场把两把尺都量一遍：
        # 真文件（通过）/ exports 入口（通过）/ exports 里没有的名字（**必须报缺**）。
        check(_subpath_resolves(nm, "katex/dist/katex.min.css")
              and _subpath_resolves(nm, "@remotion/effects/paper")
              and not _subpath_resolves(nm, "@remotion/effects/not-an-effect")
              and not _subpath_resolves(nm, "katex/dist/nope.css"),
              "两把尺都真在量：katex 样式表（真文件）与 effects/paper（exports 入口）"
              "都算解析得到，而 `@remotion/effects/not-an-effect` 与 "
              "`katex/dist/nope.css` 都算解析不到——前者防「写了个好名字却拿不到」，"
              "后者防「写错目录却不报错」")
    else:
        print(f"  ⚠ 子路径文件没点验（{rr} 下没有 node_modules；remotion 容器里有，"
              f"这一项由真渲 fixture-katex 取证，未取证前不算绿）：{sorted(subpaths)}")

    print("⑤-b 整片美术方向：枚举认错就丢掉，自由画风原样保留、幂等")
    art, e_art = cs.norm_art({"brush": "watercolor", "light": "top-left",
                              "look": " 木刻版画 + 淡彩，纸边毛糙，一束斜光  ",
                              "palette_note": "牛皮纸、墨黑、一点朱砂"})
    check(e_art == [] and art["brush"] == "watercolor" and art["light"] == "top-left",
          f"认得出的枚举归一成小写（{art}）")
    check(art["look"] == "木刻版画 + 淡彩，纸边毛糙，一束斜光",
          "look 是自由一句：只去首尾空白，不挑菜单")
    check(cs.norm_art(art) == (art, []), "幂等：过过校验的 art 再归一一次一个字节都不变")
    art_bad, e_bad = cs.norm_art({"brush": "crayon", "look": "x" * 300})
    check(e_bad == [] and "brush" not in art_bad
          and art_bad.get("look") == "x" * cs.ART_LOOK_MAX_CHARS,
          f"认不出的枚举只丢掉这一栏、不打回整份计划；自由句只截断（{e_bad}）")
    # 2026-10-20 第 001 期真机：`brush="flat"`、`motion="standard"` 让整份计划被打回一次。
    art_flat, e_flat = cs.norm_art({"brush": "flat", "motion": "standard"})
    check(e_flat == [] and art_flat == {"brush": "ink", "motion": "linear"},
          f"扁平矢量/标准动效这类自然叫法归一，不再整份重交（{art_flat}）")
    check(cs.norm_art(None) == ({}, []) and cs.norm_art("") == ({}, []),
          "没写 art（None/空串）＝ {}，老片子字节不变")
    # 真机 run 7dcaa7246e92：整份 13 镜只因 `texture="film"`、`type="sans-caption"`
    # 两条一词之差被打回。同一档的另一种叫法先归一，不再烧一整轮重交。
    art_alias, e_alias = cs.norm_art({"texture": "film", "type": "sans-caption",
                                      "look": "纯黑剪影"})
    check(e_alias == [] and art_alias.get("texture") == "grain"
          and art_alias.get("type") == "sans-editorial",
          f"近义写法归一到契约档（film→grain, sans-caption→sans-editorial）：{art_alias}")
    check(cs.norm_art(art_alias) == (art_alias, []),
          "归一后的值仍是幂等的（补丁链会再校验一遍）")
    allowed_by_key = dict(cs.ART_FIELDS)
    for key, aliases in cs.ART_ALIASES.items():
        for alias, target in aliases.items():
            check(target in allowed_by_key.get(key, ()),
                  f"别名 {key}: {alias}→{target!r} 落在该栏的枚举里")

    print("⑤-c 贯穿装置：自由一句、原样穿过校验、超长只截断")
    p_ok, e_p_ok = cs.validate_code_spec(_spec(recurring_prop="  一枚钉在墙上的读数表  "))
    check(e_p_ok == [] and p_ok.get("recurring_prop") == "一枚钉在墙上的读数表",
          "recurring_prop 去首尾空白后原样留着（自由文本，不挑菜单）")
    p_long, e_p_long = cs.validate_code_spec(_spec(recurring_prop="镜" * 100))
    check(e_p_long == [] and len(p_long["recurring_prop"]) == cs.RECURRING_PROP_MAX_CHARS,
          f"超长只截到 {cs.RECURRING_PROP_MAX_CHARS} 字，不为一句话打回整份分镜")
    p_none, _ = cs.validate_code_spec(_spec())
    check("recurring_prop" not in p_none, "没写就不出现——老片子逐字节不变")
    check(cs.spec_schema()["properties"]["recurring_prop"]["maxLength"]
          == cs.RECURRING_PROP_MAX_CHARS,
          "schema 里的上限就是校验器截断用的那个")

    print("⑤-d 风格落地：自由一句、原样穿过校验、超长只截断")
    p_ok, e_ok = cs.validate_code_spec(
        _spec(style_grammar="  像素网格 8px；色数 ≤6；硬边  "))
    check(e_ok == [] and p_ok.get("style_grammar") == "像素网格 8px；色数 ≤6；硬边",
          "style_grammar 去首尾空白后原样留着（美术导演补的自由文本）")
    p_long, e_long = cs.validate_code_spec(_spec(style_grammar="格" * 400))
    check(e_long == [] and len(p_long["style_grammar"]) == cs.STYLE_GRAMMAR_MAX_CHARS,
          f"超长只截到 {cs.STYLE_GRAMMAR_MAX_CHARS} 字，不为一句话打回整份分镜")
    p_none, _ = cs.validate_code_spec(_spec())
    check("style_grammar" not in p_none, "没写就不出现——老片子逐字节不变")
    check(cs.spec_schema()["properties"]["style_grammar"]["maxLength"]
          == cs.STYLE_GRAMMAR_MAX_CHARS,
          "schema 里的上限就是校验器截断用的那个")

    print("⑥ 给模型的 schema 与校验器同源（两处各列一遍必然漂）")
    aprops = cs.spec_schema()["properties"]["art"]["properties"]
    check(aprops["look"]["maxLength"] == cs.ART_LOOK_MAX_CHARS,
          "schema 里 look 的上限就是 norm_art 截断用的那个")
    fprops = cs.spec_schema()["properties"]["shots"]["items"]["properties"]["fields"]
    check(fprops["maxItems"] == cs.MAX_FIELDS_PER_SHOT,
          f"schema 的上限就是校验器用的那个：{fprops['maxItems']} vs "
          f"{cs.MAX_FIELDS_PER_SHOT}")
    item_props = fprops["items"]["properties"]
    check(item_props["type"]["enum"] == list(cs.FIELD_TYPES),
          "type 的 enum 从 FIELD_TYPES 拼（前端控件单源于此）")
    check(set(fprops["items"]["required"]) == {"name", "type", "label", "value"},
          f"必填四栏与校验器判的那些一致：{fprops['items']['required']}")
    check({"min", "max", "step", "unit", "options"} <= set(item_props),
          f"类型专属项都列给了模型：{sorted(item_props)}")
    check(item_props["options"]["maxItems"] == cs.MAX_OPTIONS,
          "select 候选上限与校验器同一个数")
    check(item_props["label"]["maxLength"] == 24 and item_props["unit"]["maxLength"] == 12,
          "校验器**截断**而不报错的那两处长度，schema 得如实写着，否则模型以为写了就作数")
    spec_trunc, e_trunc = cs.validate_code_spec(
        _spec(shots=[_shot_with_fields(fields=[_field(label="半" * 30, unit="px" * 20)])]))
    check(e_trunc == [] and len(spec_trunc["shots"][0]["fields"][0]["label"]) == 24
          and len(spec_trunc["shots"][0]["fields"][0]["unit"]) == 12,
          f"超长 label/unit 截断不报错（实际 {e_trunc}）")

    doc = cs._CODE_DOC
    check("fields.<name>" in doc, "文档教了从 `fields.<name>` 读值（而不是写死字面量）")
    check("<F name=" in doc and "@dg/film" in doc,
          "文档教了 `<F name=… value=…>` 标注与它的来源包（`F` 不导进来就是 undefined）")
    for phrase in ("声明了却没人读", "读了却没声明", "不是这个字段自己的值"):
        check(phrase in doc, f"文档写明了这条 lint 规则的后果：{phrase}")

    print("⑥-b 整片贯穿主体：结构化归一、越界夹回、没 look 就不成立")
    p, e_p = cs.validate_code_spec(_spec(protagonist={
        "look": "瘦高、圆头、宽肩，只画剪影", "kind": "人", "render": "lit",
        "wardrobe": "兽皮斗篷 → 连帽衫",
        "height": 0.48, "foot_y": 0.86, "facing": "右", "stride_sec": 1.0,
        "light": "火光橙 → 手机屏冷蓝", "bogus": "x"}))
    check(e_p == [], f"主体是增益不是契约，认不出的栏不报错：{e_p}")
    got = p.get("protagonist", {})
    check(got.get("look") == "瘦高、圆头、宽肩，只画剪影"
          and got.get("wardrobe") == "兽皮斗篷 → 连帽衫"
          and got.get("light") == "火光橙 → 手机屏冷蓝",
          f"三句自由文本原样进 spec：{got}")
    check(got.get("kind") == "人" and got.get("render") == "lit",
          f"主体类型与画法方向原样进 spec：{got}")
    check(cs.norm_protagonist({"look": "x", "render": "写实"})[0].get("render") == "lit"
          and cs.norm_protagonist({"look": "x", "render": "剪影"})[0].get("render")
          == "silhouette",
          "画法方向的常见说法归一到枚举（写实→lit、剪影→silhouette）")
    check(cs.norm_protagonist({"look": "一块发光的矩形", "kind": "发光的矩形"})[0]
          .get("kind") == "发光的矩形",
          "非人主体不被硬说成人：kind 是自由文本")
    check(got.get("height") == 0.48 and got.get("foot_y") == 0.86
          and got.get("stride_sec") == 1.0 and got.get("facing") == "right",
          f"数值与朝向归一：{got}")
    check("bogus" not in got, "不认识的栏不落进 spec")
    p_clamp, e_clamp = cs.validate_code_spec(_spec(protagonist={
        "look": "一个人", "height": 0.99, "foot_y": 0.1, "stride_sec": 9,
        "facing": "斜着走"}))
    got_c = p_clamp["protagonist"]
    check(e_clamp == [] and got_c["height"] == cs.PROTAGONIST_HEIGHT_RANGE[1]
          and got_c["foot_y"] == cs.PROTAGONIST_FOOT_RANGE[0]
          and got_c["stride_sec"] == cs.PROTAGONIST_STRIDE_RANGE[1],
          f"越界数字夹回范围、不报错：{got_c}")
    check("facing" not in got_c, "认不出的朝向丢掉，不猜一个方向")
    # 2026-10-20：主体怎么画由写手按 brief 自己决定，spec 里不再有「用哪个组件」
    # 这一栏；旧 spec 里残留的 component 一律丢掉，不影响分镜成立。
    check("component" not in got, f"主体画法不进 spec：{got}")
    check(cs.norm_protagonist({"look": "一个人", "kind": "人",
                               "component": "WalkingFigure"})[0].get("component")
          is None,
          "旧 spec 的 component 栏被丢掉")
    p_nolook, _ = cs.validate_code_spec(_spec(protagonist={"height": 0.5}))
    check("protagonist" not in p_nolook,
          "没有 look 的主角不成立（只有数字时写手仍会每镜发明一个人）")
    p_idem, _ = cs.validate_code_spec(p)
    check(p_idem.get("protagonist") == got, "归一幂等：过过校验的 spec 再验一遍不变")
    pprops = cs.spec_schema()["properties"]["protagonist"]["properties"]
    check(set(pprops) == {"look", "kind", "render", "wardrobe",
                          "height", "foot_y", "facing", "stride_sec", "light"},
          f"schema 里的主体栏与归一器同一份：{sorted(pprops)}")
    check(pprops["facing"]["enum"] == list(cs.PROTAGONIST_FACINGS)
          and pprops["render"]["enum"] == list(cs.PROTAGONIST_RENDERS)
          and pprops["look"]["maxLength"] == cs.PROTAGONIST_LOOK_MAX_CHARS,
          "枚举与上限从常量拼，不在 schema 里另抄一遍")
    check("protagonist" not in cs.knob_props(),
          "主角不是计划卡旋钮（自由/演出值不进卡面参数）")

    print("⑦ 教程分层：写法纪律的唯一读者通道是技能正文，那里必须逐条写着")
    # 为什么需要这一节：schema 里那 2 KB 散文**送不到模型那里**（inputSchema 由
    # ``server.py::_signature_for`` 从 Python 签名生成，只取 type），所以「这一镜该怎么写」
    # 全靠 motion_explainer 技能正文（三个图形节点都挂在 ``plan/vocab.py`` 的
    # ``_IMPLICIT_SKILLS`` 上，执行轮起步预注入）。技能漏了一条，契约文档就成了空头判据：
    # 闸会退回，模型不知道为什么、也不知道正确写法——那是一次分钟级的盲重烧。
    skill = skill_md().read_text(encoding="utf-8")
    missing_pkgs = [p for p in cs.IMPORT_WHITELIST if f"`{p}`" not in skill]
    check(not missing_pkgs,
          f"技能正文逐个列出了 import 白名单（缺：{missing_pkgs}）")
    # 逐项列出还不够：正文那句「lint 白名单 **N** 项」是个**人会把 N 写错**的数——加一件
    # 忘了改数，模型照着 N 去数会以为自己漏抄了。所以这个数也从常量算出来比一遍。
    _cnt = re.search(r"lint 白名单\s*(\d+)\s*项", skill)
    check(_cnt and int(_cnt.group(1)) == len(cs.IMPORT_WHITELIST),
          f"技能正文里那个「白名单几项」的数与常量相等（正文写 "
          f"{_cnt.group(1) if _cnt else '（没写）'}，常量 {len(cs.IMPORT_WHITELIST)}）")
    # D-红先：这条判据先对**构造样本**红一次（正文少写一件的那种样本）。
    _probe = "lint 白名单 43 项（`ALLOWED_IMPORTS`），镜头代码只能引这些："
    _p = re.search(r"lint 白名单\s*(\d+)\s*项", _probe)
    check(_p and int(_p.group(1)) != len(cs.IMPORT_WHITELIST),
          "构造样本「正文 43 / 常量 44」真被判红——加一件忘了改数的红法不漏")
    for token in ("<AbsoluteFill>", "useVideoConfig", "CaptionText", 'emphasis="color"',
                  "fields.<name>", '<F name="',
                  "rough.generator", "seed 必须给", "katex", "@react-three/fiber",
                  "durationInFrames", "Math.random", "声明了却没人读", "读了却没声明",
                  "不是这个字段自己的值"):
        check(token in skill, f"技能正文写了这条写法纪律：{token}")
    for m in cs.CAPTION_STYLES:
        check(m in skill, f"字幕这一档的选法在技能正文里：{m}")
    for m in cs.HIGHLIGHT_STYLES:
        check(m in skill, f"高亮这一档的选法在技能正文里：{m}")
    for t in cs.TEXTURES:
        check(t in skill, f"质感这一档的贵在哪在技能正文里：{t}")

    # 画面带尺寸：技能正文给的是**数字**（模型照它写实数坐标），而数字的出处是母版源码。
    # 这条对账消灭的正是旧链那次——没人告诉镜头代码带子多大，12 镜全照抄 viewBox
    # 0 0 1080 620，横屏那条带子其实是 1800×732，每个母题都被压成画面约 9%。
    geo = (remotion_root() / "src" / "tokens.ts").read_text(encoding="utf-8")
    for aspect, (w, h) in cs.ASPECTS.items():
        gm = re.search(rf"{aspect}: \{{\s*topSafe: (\d+), bottomSafe: (\d+),\s*"
                       rf"gutter: (\d+),", geo)
        if not gm:
            check(False, f"母版 FILM_GEOMETRY 里读得到 {aspect} 的几何")
            continue
        top, bottom, gutter = (int(x) for x in gm.groups())
        box = f"{w - 2 * gutter}×{h - top - bottom}"
        check(box in skill,
              f"技能正文写的画面带尺寸与母版现算的一致：{aspect} {box}")

    # 自包含镜的底色回退：`code_render.DEFAULT_BG` 是「这一镜既没回读 token、也没申报
    # `bg`」时闸④ 判空的基准色，而它的出处应当是 `TOKENS.bg`。两处各写一个字面量的那一刻，
    # 基准色就与真底色分家了——判空会拿片子从没用过的一个颜色去比（读数整体歪掉，而闸
    # 还是绿的），与上面那条带子尺寸是同一类「两个出处」的错。所以把这两个字节钉在一起。
    _bg = re.search(r"\bbg:\s*'(#[0-9a-fA-F]{3,6})'", geo)
    check(_bg is not None, "tokens.ts 里读得到 TOKENS.bg 的字面量")
    if _bg:
        check(cr.DEFAULT_BG.lower() == _bg.group(1).lower(),
              f"code_render.DEFAULT_BG 与 tokens.ts 的 TOKENS.bg 同值"
              f"（{cr.DEFAULT_BG} vs {_bg.group(1)}）")

    print("⑧ 闸⑦：每镜必画判在契约层（这里红一次 = 一次分镜重做，一帧都不烧）")
    # 判据读的是**代码字节里的 import**，不是模型自报的一栏：自报会漂（写 `style: rough`
    # 而代码里一个 roughjs 都没有），而这一道要拦的恰恰就是「说用了但其实没有」。
    # 所以夹具一律按通道造代码，不设任何自报字段。
    WRAP = ("export const Shot = () => <FilmLayout>a{getInputProps().text}"
            "<CaptionLayer /></FilmLayout>;")
    # 每一行 import 都必须在代码体里**用得上**（闸⑦ 认的是那个局部名，不认包名单）：
    # 夹具写成「只 import 一行」就会全部退化 vector/text，正好验证这条不收空 import。
    # P1（2026-10-17）注入契约也罩着这批夹具：每支都让 `getInputProps` 真的出现在代码体里
    # （E1），并摆 `<CaptionLayer/>`（它自己读注入，E2 的「接文案」与 E3 的「整片开着
    # 字幕」一并满足——不写属性的空标签 token 成本最低）。加了这些之后几支残件会越过
    # `_SHAPE_MIN_TOKENS=40` 进闸⑫/⑨ 的比对——这是接受的：本文件里没有「整片闸必须
    # 空账」的断言，谁日后想加，先把夹具收短再谈。
    CH_CODE = {
        "rough": ('import { rough } from "roughjs";\nexport const S = () => '
                  '<FilmLayout>{rough.circle(0,0,9)}{getInputProps().text}'
                  '<CaptionLayer /></FilmLayout>;'),
        "three": ('import { Canvas } from "@react-three/fiber";\nexport const Shot = () => '
                  '(<FilmLayout><Canvas />{getInputProps().text}'
                  '<CaptionLayer /></FilmLayout>);'),
        "katex": ('import katex from "katex";\nexport const Shot = () => '
                  '(<FilmLayout>{katex.renderToString("E=mc^2")}{getInputProps().text}'
                  '<CaptionLayer /></FilmLayout>);'),
        # 闸⑫ 对**短于 40 个骨架 token** 的镜不判（`_SHAPE_MIN_TOKENS`：「三五行代码
        # 怎么比都是噪声，宁可不判」；残件与任何真 demo 的相似度都只有 0.01 档， Negative
        # 样本 max 0.736）。P1 之前这几支残件停在豁免线以下；加注入之后越线，见上面那段。
        "vector": ('export const Shot = () => <FilmLayout><svg />{getInputProps().text}'
                   '<CaptionLayer /></FilmLayout>;'),
        "text": WRAP,
    }
    for name, code in CH_CODE.items():
        check(cs.shot_style_channel(code) == name, f"通道归类读代码字节：{name}")
    check(cs.shot_style_channel('import { rough } from "roughjs";\n' + WRAP) == "text"
          and cs.shot_used_channels('import { Canvas } from "@react-three/fiber";\n' + WRAP)
          == set(),
          "只 import 一行、代码体里没用上 → 不算通道（凑配额最省事的写法）")
    check(cs.shot_style_channel('import { Canvas } from "three";\nexport const Shot = () => '
                                '(<FilmLayout><Canvas /><svg /></FilmLayout>);') == "three",
          "既上 three 又写裸 SVG → 专门通道优先（否则「一条 three + 十一条 SVG」会被蒙成两档各半）")
    # 三维写实模板（2026-10-21）：镜头代码只 import `StudioStage`，三维那一层在模板里。
    # 不认它，整片十三镜都会被闸⑦ 判成「只有文字与色块」——这是模板路线的主路。
    _STUDIO = ("import { StudioStage, CeramicMaterial } from "
               "'@dg/lib/style/cinema-studio';\n"
               "export default () => <StudioStage><mesh>"
               "<sphereGeometry /><CeramicMaterial /></mesh></StudioStage>;")
    check(cs.shot_drawn(_STUDIO) and cs.shot_style_channel(_STUDIO) == "three",
          "只 import 三维写实模板 → 算 three 通道（舞台在模板里，实物在镜里）")
    check("@dg/lib/style/cinema-studio" in cs.IMPORT_WHITELIST
          and "@dg/lib/style/cinema-studio" in cs.PICTORIAL_CHANNELS["three"]
          and "@dg/lib/style/cinema-studio" in cs._VECTOR_COMPONENT_PKGS,
          "三维写实模板在契约白名单、three 通道与矢量组件包里各有一份")

    # 闸⑮ 的两条（卡名种类 ≥ ceil(镜数/4)、同一族 ≤2 镜）意味着**六镜片不能六镜都用
    # 同一张卡**。所以这里的夹具按族循环取真卡（族 = 卡库类别目录名，见 code_spec.card_family）。
    CARD_CYCLE = ("opening/brand-ink-open", "data/before-after-slider-scrub",
                  "effects/assemble-then-type-flyin", "typography/blur-slide",
                  "ui-entrance/avatar-bracket-carousel", "camera/basic-3d-scene")
    # 闸⑯ 的主体名按镜号错开只是为了让夹具读起来像真的分镜；判据不要求各不相同。
    SUBJECTS = ("活字盘", "铁板", "胶泥字", "墨斗", "排字架", "拓本")

    # 闸⑲ 读的是代码里那句 `const palette = palettes["…"]`（`_PALETTE_BIND_RE`）：
    # 认不出色板的镜**不进分子也不进分母**，所以夹具也得按技能正文给的正规模板逐镜
    # 绑一套色板。四套轮着用——六镜片因此摊到 4 套 ≥ ceil(6/4)=2；把 `palettes` 收成
    # 一套（`palettes=("warm-cream",)`）造的就是「二十镜一个调子」的负数样本。
    # 满幅同理由夹具承担：默认在 s02/s05 写 `bleed: true`，六镜起 ceil(n/5)=2 镜打平，
    # 十镜那一档也是 2 镜。`bleed=()` 就是「全被裁进同一个居中框里」的负数样本。
    PAL_CYCLE = ("warm-cream", "deep-ocean", "obsidian-violet", "solar-pop")

    def _shots(channels: list[str], *, text: str = "胶泥刻字排成一行",
               hl: str = "刻字", bleed: tuple[int, ...] = (1, 4),
               bg: bool = True, palettes: tuple[str, ...] = PAL_CYCLE) -> list[dict]:
        """按通道造一串镜；文案默认那条**词法认不出具体项**的（纯中文、无数字无拉丁）。

        主体通道按通道给：纯文字镜**不填** `subject_channel`（没有图形通道可指，
        填了 ⑯ 会判「自报无证据」；而它更早在 ⑦ 上就已经被打回了），
        其余四条画法通道都填上代码里真认得出来的那一条。
        `palettes=()` 造「一镜都没绑色板」的样本——闸⑲ 那一档要落**未判**，不是通过。
        """
        out = []
        for i, c in enumerate(channels):
            code = CH_CODE[c]
            if palettes:
                code = (f'const palette = palettes["{palettes[i % len(palettes)]}"];\n'
                        + code)
            over = {"id": f"s{i + 1:02d}", "code": code, "text": text, "highlight": [hl],
                    "card": CARD_CYCLE[i % len(CARD_CYCLE)],
                    "subject": SUBJECTS[i % len(SUBJECTS)]}
            if c != "text":
                over["subject_channel"] = c
            else:
                # **必须显式置空**：`_shot()` 的缺省申报是 `vector`（它那份代码带
                # `<svg />`），这里「不覆盖」＝继承那个缺省，纯文字镜就成了
                # 「申报 vector 而代码里一条通道都没用上」——闸⑯ 会按自报无证据打回。
                # 2026-10-23 晚闸⑯ 加回整片判据时暴露出来的：注释一直写着「不填」，
                # 而实际填的是 `_shot()` 的缺省值。
                over["subject_channel"] = ""
            if i in bleed:
                # 满幅与底色**成对**写：闸⑲ 的两条判据一条读 `bleed`、一条读 `bg`
                over["bleed"] = True
                if bg:
                    over["bg"] = "#0b1620"
            out.append(_shot(**over))
        return out

    # —— 每镜必画：先证它逐镜拦，再证「全片一种通道」已经不再是错 ——
    errs, _ = cs.check_draw_floor(_shots(["text"] * 6))
    check(any("闸⑦ 每镜必画" in e and "6/6 镜" in e for e in errs),
          f"6 镜全文字 → 逐镜打回，缺口数报成 6/6：{errs[:1]}")
    check(all(f"s{i + 1:02d}" in errs[0] for i in range(6)),
          "打回文本把每一镜都点名到镜号——「有几镜没画」之外还得知道是哪几镜")
    errs, _ = cs.check_draw_floor(
        _shots(["rough", "vector", "text", "rough", "vector", "katex"]))
    check(len(errs) == 1 and "1/6 镜" in errs[0] and "s03" in errs[0] and "s01" not in errs[0],
          f"混合片里只有 s03 纯文字 → 只点它一个：{errs[:1]}")
    errs, _ = cs.check_draw_floor(_shots(["text"] * 5))
    check(any("5/5 镜" in e for e in errs),
          f"5 镜全文字照样打回：旧版「镜数不到 {cs.MIN_SHOTS_FOR_QUOTA} 就不判配额」"
          f"那个口子已经关掉，留着它就是「短片是这一道闸的盲区」（{errs[:1]}）")
    errs, _ = cs.check_draw_floor(_shots(["vector", "text", "rough"]))
    check(any("s02" in e for e in errs),
          f"三镜片也逐镜判（这条没有最低镜数这一档）：{errs[:1]}")
    # 全片一种通道**不再**是错：画法由模型按题材定，重不重样是它自己的取舍（用户拍板）。
    # ⑦ 只问「画了没有」，不问「重不重样」——后者从来就不是这条闸的职责。
    check(cs.check_draw_floor(_shots(["vector"] * 6))[0] == [],
          "6 镜全裸 SVG → 绿：四条画法通道命中任一条都算画了，通道种数不再是闸")
    check(cs.check_draw_floor(_shots(["rough", "vector", "katex"] * 3 + ["rough"]))[0] == [],
          "10 镜三种通道 → 绿：旧版这一档要四种，配额连同它那一档一起取消了")
    check(cs.check_draw_floor(_shots(["rough", "vector", "katex", "three"] + ["vector"] * 6))[0] == [],
          "10 镜四种通道 → 绿")
    # 静态图**不是**画法通道（`image` 已从通道表摘掉）：一镜只摆一张图 = 没画。
    _IMG_ONLY = ('import { staticFile } from "remotion";\n'
                 'export const Shot = () => (<FilmLayout>'
                 '<img src={staticFile("a.png")} /></FilmLayout>);')
    check(cs.shot_drawn(_IMG_ONLY) is False and cs.shot_style_channel(_IMG_ONLY) == "text",
          "只 staticFile 摆一张图 → 不算画了：落在 text 那一档，逐镜打回")
    check(set(cs.DRAWN_CHANNELS) == {"rough", "three", "katex", "vector", "canvas"}
          and "image" not in cs.PICTORIAL_CHANNELS and "image" not in cs.SUBJECT_CHANNELS,
          "画法通道五选一（2026-10-18 加 canvas）：`image` 不在通道表、也不在 ⑯ 的白名单里")
    # 手绘件与场景件落在 `vector`：标记在组件里，不在这段代码的字节里——不认它们，
    # 一镜纯 `<BrushStroke/>` 会被闸⑦ 判成「只有文字与色块」（手绘路线的主路被误杀）。
    _BRUSH_ONLY = ("import { BrushStroke } from '@dg/lib/brush';\n"
                   "export default () => <AbsoluteFill>"
                   "<BrushStroke points={[[1, 2], [3, 4]]} /></AbsoluteFill>;")
    _SCENE_ONLY = ("import { Scene } from '@dg/lib/scene';\n"
                   "export default () => <AbsoluteFill><Scene /></AbsoluteFill>;")
    check(cs.shot_drawn(_BRUSH_ONLY) and cs.shot_style_channel(_BRUSH_ONLY) == "vector",
          "纯 <BrushStroke/> 的镜算画了（落在 vector，不是 text）")
    check(cs.shot_drawn(_SCENE_ONLY) and cs.shot_style_channel(_SCENE_ONLY) == "vector",
          "纯 <Scene/> 的镜算画了（落在 vector，不是 text）")
    check(not cs.shot_drawn("import { BrushStroke } from '@dg/lib/brush';\n"
                            "export default () => <AbsoluteFill><div>hi</div></AbsoluteFill>;"),
          "import 了笔刷却没用上 → 不算画（与其余几条通道同一口径）")
    _img_shots = _shots(["rough", "vector", "katex"] * 2)
    _img_shots[2]["code"] = _IMG_ONLY
    errs, _ = cs.check_draw_floor(_img_shots)
    check(len(errs) == 1 and "s03" in errs[0],
          f"摆图的那一镜与纯文字镜同等对待（都不是画）：{errs[:1]}")

    # —— 文案具体项：只提醒，绝不打回（与「画不画」正交，故原样留着）——
    errs, warns = cs.check_draw_floor(_shots(["rough", "vector", "katex"] * 2))
    check(errs == [] and len(warns) == 1 and "文案提醒" in warns[0],
          f"每镜都画了、文案全空 → 只有提醒：{errs} / {warns[:1]}")
    check("地名人名机器认不出" in warns[0], "提醒文本自己就写清为什么这条不拦")
    check(all(sid in warns[0] for sid in ("s01", "s06")), "提醒逐镜点名到镜号")
    errs, warns = cs.check_draw_floor(
        _shots(["rough", "vector", "katex"] * 2, text="1040 年代的活字版", hl="活字"))
    check(errs == [] and warns == [], f"每镜都带数字 → 提醒也消失：{errs} / {warns}")
    check(cs.has_concrete_item("毕昇用胶泥刻字，铁板蒙气") is False
          and cs.has_concrete_item("元丰年间的事") is False
          and cs.has_concrete_item("直径约 20 公里") is True,
          "词法只认数字/引号术语/拉丁缩写——这条判据覆盖面窄，所以才只做提醒")

    # —— 这道闸真的挂在整片判据那一个出口上（不是只活在 check_style_variety 里）——
    #    2026-10-16 并档：判据**不再随 validate_code_spec 的 errors 抛出去**，
    #    全部走 whole_film_gate_results 那一个出口，由三个调用点附进正常返回。
    #    这一对断言守的就是这条搬家：读数还在（前一条），但不再拦人（后一条）。
    _spec7, v_errs = cs.validate_code_spec(_spec(shots=_shots(["text"] * 6)))
    _f7 = cs.whole_film_gate_results(_spec7["shots"])
    check(not any("闸⑦" in e for e in _f7[0]),
          f"2026-10-23：闸⑦ 已撤出整片判据——直接调仍有读数（上一条），出口里没有它"
          f"（{_f7[0][:1]}）")
    check(not any("闸⑦" in e for e in v_errs),
          f"契约入口不再装判据：闸⑦ 的读数不随 errors 抛出去，"
          f"改走整片判据那一个出口（{v_errs[:1]}）")
    # —— 自检工具与老路**同一份读数**（计划 §8 第 2 条守卫）——
    #    两份对不上的表现是「工具说绿、闸说红」，而那种漂在离线用例里看不出来。
    from open_storyline.motion import selfcheck as _sc
    _cf = _sc.check_film(_spec7["shots"])
    check(_cf["errors"] == _f7[0] and _cf["warnings"] == _f7[1]
          and _cf["undetermined"] == _f7[2],
          f"check_film 与整片判据逐字相同（工具 {_cf['errors'][:1]} vs 出口 {_f7[0][:1]}）")
    check(_cf["verdict"] in ("green", "undetermined"),
          f"闸⑦ 撤出后，只犯这一条的片子不再被判红（实际 {_cf['verdict']}）")
    # 2026-10-24：这一条原来断言 `verdict == "green"`，而它成立的原因是个 **bug**——
    # `check_ink_contrast` 的未判档跟着 `WHOLE_FILM_GATES` 一起被裁掉，`style_unjudged`
    # 恒为空，于是「没人量过」被折算成了「量过且通过」。修好之后这份夹具露出真话：
    # 它的代码里一个**字面颜色**都没有（只有 `<svg />` 与 `<CaptionLayer />`），
    # 闸⑩ 因此量不出底色与字色 → 未判档恰一条。
    # 未判不是红，但也不许读成绿——T28 的三态就落在这一句上。
    check(len(_f7[2]) == 1 and "未判" in _f7[2][0] and "对比度" in _f7[2][0],
          f"这份夹具唯一量不出的是字色对比度，且它单列在未判档、没被折算成通过：{_f7[2][:1]}")
    # —— 工具返回值**必须真的能进 JSON**（2026-10-16 真机事故的回归守卫）——
    #    上面那三条断言是**绿的**，而工具在真机上每次调用都回
    #    `Error: Object of type set is not JSON serializable`（run 9272c7f59501，
    #    seq 41/50/53/59，连试四次才放弃）：它们只比三个字符串列表，**从来没有把这个
    #    返回值交出去序列化过一次**。风格账 `all_channels[sid]` 是个集合，内部算得没错，
    #    错在这份返回值要过 MCP 的 JSON 那一关。所以这条断言判的不是判据，是**边界**：
    #    它跟 `_jsonable` 一起守「工具能不能把话说完」。少了它，同一个 bug 会再回来一次
    #    ——而这次的表现和上次一模一样：离线全绿、真机全红。
    import json as _json
    try:
        _blob = _json.dumps(_cf, ensure_ascii=False)
        _json_ok, _why = True, f"{len(_blob)} 字节"
    except TypeError as exc:
        _json_ok, _why = False, f"{type(exc).__name__}: {exc}"
    check(_json_ok, f"check_film 的返回值过得了 json.dumps（MCP 交出去的就是它）：{_why}")
    check(_json_ok and _cf["facts"]["all_channels"] ==
          {k: sorted(v) for k, v in cs.style_facts(_spec7["shots"])["all_channels"].items()},
          "风格账里的集合经边界那一层后变成**排序后**的列表（顺序稳定、两次调用可比）")
    _, v_ok = cs.validate_code_spec(_spec(shots=_shots(["rough", "vector", "katex"] * 2)))
    check(v_ok == [], f"混合通道的六镜片过整份契约（实际 {v_ok}）")

    # —— ⑲ 闸⑲ 风格配额：**2026-10-24 整道删除**（先 10-23 撤出整片判据，
    #    再连函数与四个常数一起删）。这里只守一件事：钩子真的走了，别留个没人读的常数
    #    让下一个人以为它还有用。判据原文归档 docs/已删的闸-2026-10-24.md。
    check(not hasattr(cs, "check_style_variety")
          and "VARIETY_PALETTE_DIV" not in dir(cs)
          and "VARIETY_MIN_SHOTS" not in dir(cs)
          and "VARIETY_MIN_TEXTURES" not in dir(cs)
          and "VARIETY_BLEED_DIV" not in dir(cs)
          and "VARIETY_BG_SHARE" not in dir(cs),
          "闸⑲ 的函数与它的五个常数一起删了——留一个没人读的常数会误导下一个人")
    # —— 整片色板（2026-10-17）：一个字段作**默认**，个别镜可覆盖 ——
    #    这一节判的是「整片一个字段能不能真的把底色统一起来」，三件事各一支：
    #    契约收下它、它进缓存键、以及认不出自己色板的镜真的落到它上面。
    pal_spec, pal_errs = cs.validate_code_spec(_spec(palette="vintage-kraft"))
    check(pal_errs == [] and pal_spec["palette"] == "vintage-kraft",
          f"整片色板过契约并原样收下（实际 {pal_errs} / {pal_spec.get('palette')}）")
    check(cs.validate_code_spec(_spec())[0]["palette"] == "none",
          "没写这一栏时落 `none`（缺省那一档的字面量，卡面/契约/回执三处同一个字）")
    check(bool(cs.validate_code_spec(_spec(palette=""))[1]) is False
          and cs.validate_code_spec(_spec(palette=""))[0]["palette"] == "none",
          "空串也是「不指定」——文字库面板那条老路发的就是空串，两个写法同义")
    # 卡面那一格的默认值就是 `none`，而它归一之后是空串——「认不出」那条判据必须**先**
    # 把这一个值排掉，否则计划卡下来的每一份 spec 都会被自己打回一句「`none` 不是一套
    # 色板」（可用清单里第一个写着的就是它）。这条守卫是 2026-10-17 补的。
    _none_spec, _none_errs = cs.validate_code_spec(_spec(palette="none"))
    check(_none_errs == [] and _none_spec["palette"] == "none",
          f"`none` 是合法取值（卡面默认那一档）：{_none_errs[:1]}")
    check(cs.validate_code_spec(cs.validate_code_spec(_spec(palette="none"))[0])[1] == [],
          "校验对自己写下的 `none` 幂等——补丁链会把同一份 spec 再校验一遍")
    _bad_pal, _bad_errs = cs.validate_code_spec(_spec(palette="warm-cream"))
    check(any("palette" in e and "不是一套色板" in e for e in _bad_errs)
          and _bad_pal["palette"] == "none",
          f"认不出的色板名**报错并清成 none**（与 texture 那一支同规："
          f"留着它下游只会静默不生效）：{_bad_errs[:1]}")

    # 认不出自己色板的镜落到整片那一档；自己绑了色板的镜**不受影响**（个别镜可覆盖）。
    plain = _shot(id="s01", code=CH_CODE["vector"])
    bound = _shot(id="s02", code=('const palette = palettes["deep-ocean"];'
                                  + CH_CODE["vector"]))
    film_pal = {"palette": "vintage-kraft"}
    fs_film = cs.film_style_of(plain, film=film_pal)
    check(fs_film == {"bg": "#c8aa7d", "paper": "#dfc69e", "ink": "#382a20",
                      "accent": "#8b4937"},
          f"没绑色板的镜落到整片那一档（底色 + 字幕三色四件一起给）：{fs_film}")
    check(cs.film_style_of(plain) == {},
          "**不给 film 时与从前逐字一致**（既有调用点不传也不会悄悄换像素）")
    fs_own = cs.film_style_of(bound, film=film_pal)
    check(fs_own["bg"] == "#0b1620" and fs_own["accent"] == "#7cc8b4",
          f"自己绑了色板的镜仍用自己那一套——「个别镜可覆盖」是结构上的：{fs_own}")
    check(cs.style_facts([plain, bound], film=film_pal)["palettes"] ==
          {"s01": "vintage-kraft", "s02": "deep-ocean"},
          "账上逐镜记的也是**实际用上的**那一套（人一眼看得出整片是不是一个调子）")
    check(cs.style_facts([plain])["palettes"] == {"s01": None},
          "不传 film 时账上仍是「这一镜自己绑了没有」，不是编一个名字出来")

    # —— 字幕字号 / 中英次序（2026-10-21）：风格包 manifest 的 `caption` 块 ——
    #    用户问「为什么不能在 skill 或提示词里改，为什么要写死」：这两件现在就是
    #    数据（`caption.size` / `caption.order`），母版只留范围兜底。这一节盯三件事：
    #    声明了就带出去、没声明一个键都不多、写坏了退回缺省而不是抛。
    def _cap_film(caption, *, as_json: bool = False) -> dict:
        manifest = {"caption": caption}
        if as_json:
            return {"skill_contract": {"name": "style_cap",
                                   "manifest": json.dumps(manifest, ensure_ascii=False)}}
        return {"skill_contract": {"name": "style_cap", "manifest": manifest}}

    fs_cap = cs.film_style_of(plain, film=_cap_film({"size": 0.36, "order": "zh-en"}))
    check(cs.film_style_of(plain, film={"skill_contract": {"name": "style_cap",
                                                       "manifest": {}}}) == {},
          "没有 caption 块 → **一个键都不加**（没声明过的片子逐字节不变）")
    for _bad in (0.05, 2.0, "big", None, True, [], {}):
        check("captionSize" not in cs.film_style_of(
            plain, film=_cap_film({"size": _bad})),
            f"size={_bad!r} 不认（区间 {cs.CAPTION_SIZE_MIN}–{cs.CAPTION_SIZE_MAX}，"
            "写坏退回缺省而不是抛）")
    check("captionOrder" not in cs.film_style_of(
        plain, film=_cap_film({"order": "top-bottom"})),
        "order 只收 en-zh / zh-en（不认的说法不进风格档）")
    check(cs.film_style_of(
        plain, film={"skill_contract": {"name": "x", "manifest": "{oops"}}) == {},
        "manifest 是坏 JSON → 当没有，不抛")

    # —— 字幕离底距离与字号上限（2026-10-21，风格工作室）——
    #    用户：「字幕位置下调，大概离底部一行字的距离」。位置与上限同样是数据：
    #    `caption.bottom`（占画面高）/ `caption.cap`。缺席时母版落几何表与 0.036H。
    fs_geo = cs.film_style_of(plain, film=_cap_film({"bottom": 0.045, "cap": 0.032}))
    for _bad in (0.0, 0.9, -0.1, "low", True, None, [], {}):
        check("captionBottom" not in cs.film_style_of(
            plain, film=_cap_film({"bottom": _bad})),
            f"bottom={_bad!r} 不认（区间 {cs.CAPTION_BOTTOM_MIN}–{cs.CAPTION_BOTTOM_MAX}）")
    for _bad in (0.005, 0.2, "big", True, None):
        check("captionCap" not in cs.film_style_of(
            plain, film=_cap_film({"cap": _bad})),
            f"cap={_bad!r} 不认（区间 {cs.CAPTION_CAP_MIN}–{cs.CAPTION_CAP_MAX}）")
    check("captionBottom" not in cs.film_style_of(plain, film=_cap_film({})),
          "没有 caption 块 → 位置键一个都不加")

    def _cap_key(caption) -> str:
        return cs.shot_cache_key(plain, film=_cap_film(caption),
                                 master_hash="a" * 16, lockfile_hash="b" * 16)


    # —— 花钱的那几个整片开关（2026-10-17）：`review`/`still_review`/`identity` ——
    #    这一节判的是**它们能不能活着走到渲染链上**。`validate_code_spec` 返回的是一个
    #    逐栏抄的新字典，所以「开关能开」这件事**不是自动成立的**：在这之前三栏都掉在
    #    半路上，回执上永远写着「这趟没开」。这一节就是那一天的真身守卫。
    check(set(cs.SWITCH_KNOBS) == {"review", "still_review", "identity"},
          f"开关清单就是这三个（实际 {cs.SWITCH_KNOBS}）")
    check(not (set(cs.SWITCH_KNOBS) & set(cs.KNOB_FIELDS)),
          "它们**不在卡面参数里**：卡面那排 radio 回答「片子长什么样」，"
          "这三档回答「愿不愿意按镜付钱」，混在一起等于替人按下付款键")
    check(not (set(cs.SWITCH_KNOBS) & set(cs.FILM_KNOBS)),
          "也不进缓存键：它们不改像素，改的是「烧完像素之后再看一遍」那条腿")
    _sw_on, _sw_errs = cs.validate_code_spec(
        _spec(review=True, still_review=True, identity=True))
    check(_sw_errs == [] and all(_sw_on.get(k) is True for k in cs.SWITCH_KNOBS),
          f"三个都写 true → 原样穿过校验（实际 {[_sw_on.get(k) for k in cs.SWITCH_KNOBS]}）")
    _sw_off = cs.validate_code_spec(_spec())[0]
    check(all(k not in _sw_off for k in cs.SWITCH_KNOBS),
          "没写时**不留这一栏**（`spec.get` 得到 None，与「没写」逐字同义——"
          "回执里那句「这趟没开」因此仍然成立）")
    for _v in ("false", "False", "0", 0, "", None, []):
        check("identity" not in cs.validate_code_spec(_spec(identity=_v))[0],
              f"identity={_v!r} 按**没开**处理（`bool(\"false\") is True` 那一族坑："
              "用户关掉了回看、回执却说回看跑过）")
    check(cs.validate_code_spec(_spec(identity="true"))[0].get("identity") is True,
          "字符串 \"true\" 也认——卡面与面板发的值就是字符串（`narration` 同规）")

    # —— ⑳ 闸⑳ 原创配额：**2026-10-24 删除**（`DEMOS_ENABLED` 关着，它本来就不判）。
    #    夹具留着：下面 ⑫ 的两条哨兵用例还要用它。
    base = ["rough", "vector", "katex"] * 2

    def _with_originals(k: int) -> list[dict]:
        """六镜片，前 k 镜走原创哨兵，其余照旧派生自 demo。"""
        rows = _shots(base)
        for r in rows[:k]:
            r["derived_from"] = [cs.ORIGINAL_SENTINEL]
        return rows
    # 同一段长代码，先证红（声明派生自 ListStackPress → 骨架不像 → 打回），
    # 再证绿（只把声明换成哨兵 → 不再响）。这一对才验到「哨兵镜跳过骨架比对」本身。
    _long = CH_CODE["rough"] + "\n" + ("const a = 1;\n" * 40)
    _row = _shot(id="s01", code=_long)
    check(bool(cs.check_derived_similarity([_row])[0]),
          f"长代码声明派生自 ListStackPress → 闸⑿ 先证红："
          f"{cs.check_derived_similarity([_row])[0][:1]}")
    check(cs.check_derived_similarity([dict(_row, derived_from=[cs.ORIGINAL_SENTINEL])])[0] == [],
          "只把声明换成哨兵 → 同一道闸不再响：声明里没有 demo，无从比起")
    _, v_orig_ok = cs.validate_code_spec(_spec(shots=_with_originals(1)))
    check(v_orig_ok == [], f"1 镜原创的六镜片过整份契约（实际 {v_orig_ok}）")
    _spec_orig, v_orig_bad = cs.validate_code_spec(_spec(shots=_with_originals(2)))
    check(not any("闸⑳ 原创配额" in e for e in v_orig_bad)
          and not any("闸⑳ 原创配额" in w
                      for w in cs.whole_film_gate_results(_spec_orig["shots"])[1])
          and cs.DEMOS_ENABLED is False,
          f"⑳ 随 demo 一起关掉（DEMOS_ENABLED=False）：契约不打回、回执也不点名"
          f"（{v_orig_bad[:1]}）")
    # —— 哨兵与真 demo 混写：**不再报错**（2026-10-16 拆「必须照卡派生」）——
    #    必填时代那条「哨兵必须单独出现」是在给闸⑳ 数数用的；现在 `derived_from` 是
    #    选填、"不派生"的最省事写法就是留空，哨兵成了留空的一种显式写法。真 demo 在，
    #    这一镜就是派生的；哨兵那一项按没写处理。
    _mix = _shots(base)
    _mix[0]["derived_from"] = [cs.ORIGINAL_SENTINEL,
                               "ui-entrance/list-stack-press/ListStackPress.tsx"]
    _mix_spec, v_mix = cs.validate_code_spec(_spec(shots=_mix))
    check(not any("哨兵" in e for e in v_mix),
          f"哨兵与真 demo 混写不再打回（真 demo 数数，哨兵按没写算）：{v_mix[:1]}")
    check(_mix_spec["shots"][0]["derived_from"] == [],
          f"demo 关着：derived_from 写什么都当没写（"
          f"{_mix_spec['shots'][0]['derived_from']}）")
    check("s01" not in cs.original_shot_ids(_mix_spec["shots"]),
          "混写的那一镜不算原创（它真地派生自一份 demo）")
    # —— 拆掉必填之后，**留空**是合法的一份分镜（这是 §4 那一拆的全部意思）——
    _bare = _shots(base)
    for _r in _bare:
        _r["card"] = _r["derived_from"] = ""
    _bare_spec, v_bare = cs.validate_code_spec(_spec(shots=_bare))
    check(not any("card" in e or "derived_from" in e for e in v_bare),
          f"既不选卡也不声明出处：过契约，不再被逼着照卡派生（{v_bare[:1]}）")
    check(all(s["card"] == "" and s["derived_from"] == [] for s in _bare_spec["shots"]),
          "留空归一成空值（不是 None、也不是被塞一个默认卡名）")
    _acct, _ferr = cs.collect_demos(
        [{"id": "s01", "card": CARD_CYCLE[0], "derived_from": [cs.ORIGINAL_SENTINEL]}])
    check(_ferr == [] and _acct["forage_demos"] == [],
          f"取材轮遇到纯哨兵行：不报「读不到」、也不回字节（{_ferr}）")
    check(cs.style_facts(_with_originals(2))["original_shots"] == ["s01", "s02"],
          "计划卡与出片回执读得到哪几镜是原创（style_facts.original_shots）")

    # —— 层E2：字幕两维**逐镜覆盖**。整片那一档写 spec 顶层，覆盖写在那一镜上。
    #    为什么要这一层：一条片子里总有几镜要「另一档字幕」（这一镜是大字压屏、
    #    其余是下沿贴画面），而「整片一档」把它变成全片或全无。
    #    三条判据：① 收下并归一（没写 = None，不是「覆盖成整片那一档」）；② 注入时逐镜
    #    优先；③ 键按**这一镜**算（别的镜改了不许动这一镜的键）。
    _sub_pair, _sub_err = cs.validate_code_spec(_spec(caption_style="paper", shots=[
        dict(_shot(), caption_style="big_center", highlight_style="karaoke"),
        _shot(id="s02", text="密度相当于把一座山压进方糖", highlight=["方糖"])]))
    check(_sub_err == [], f"逐镜字幕两维原样收下（实际 {_sub_err}）")
    check(_sub_pair["shots"][0]["caption_style"] == "big_center"
          and _sub_pair["shots"][0]["highlight_style"] == "karaoke"
          and _sub_pair["shots"][1]["caption_style"] is None
          and _sub_pair["shots"][1]["highlight_style"] is None,
          "写了的存它那一档、没写的存 None——None 是「没覆盖」；"
          "两维各存各的，一维覆盖不许把另一维也标成覆盖过")
    _, _sub_bad = cs.validate_code_spec(_spec(shots=[dict(_shot(), caption_style="bottom")]))
    check(any("caption_style" in e and "bottom" in e for e in _sub_bad),
          f"逐镜那一栏按同一张白名单校验，报错点名是哪一栏（{_sub_bad[:1]}）")
    check("bottom" not in cs.CAPTION_STYLES and "big_center" in cs.CAPTION_STYLES,
          f"白名单就是母版实现的那七档：{sorted(cs.CAPTION_STYLES)}")

    _pa = cr.shot_input_props(_sub_pair["shots"][0], _sub_pair, frames=60, frame_words=[])
    _pb = cr.shot_input_props(_sub_pair["shots"][1], _sub_pair, frames=60, frame_words=[])
    check(_pa["captionStyle"] == "big_center" and _pb["captionStyle"] == "paper",
          f"注入时逐镜优先、没覆盖的回落整片那一档"
          f"（{_pa['captionStyle']!r} / {_pb['captionStyle']!r}）")
    check(_pa["highlightStyle"] == "karaoke" and _pb["highlightStyle"] == "color",
          f"两维各自回落，互不牵连（{_pa['highlightStyle']!r} / {_pb['highlightStyle']!r}）")
    _pc = cr.shot_input_props(dict(_sub_pair["shots"][1]),
                              {**_sub_pair, "caption_style": "", "highlight_style": ""},
                              frames=60, frame_words=[])
    check(_pc["captionStyle"] == "outlined" and _pc["highlightStyle"] == "plain",
          "两处都没有时的落点是「白字黑边 · 整条同色」（2026-10-20 用户点名："
          "字幕要统一；深底浅底都读得出来，且没有纸条底）")
    for _k in ("captionStyle", "highlightStyle"):
        check(_k in cr.TOKEN_PER_SHOT_KEYS,
              f"闸⑤ 的跨镜白名单里有 {_k}（{sorted(cr.TOKEN_PER_SHOT_KEYS)}）——"
              "不收的话「这镜大字压屏、那镜下沿」会被判成「母版常量漂移」而当场判死")

    def _sub_key(shots: list[dict], i: int, film_mode: str = "paper") -> str:
        return cs.shot_cache_key(shots[i],
                                 film={**{k: _sub_pair.get(k) for k in cs.FILM_KNOBS},
                                       "caption_style": film_mode},
                                 master_hash="a" * 16, lockfile_hash="b" * 16)

    _k_a, _k_b = _sub_key(_sub_pair["shots"], 0), _sub_key(_sub_pair["shots"], 1)
    _flip = copy.deepcopy(_sub_pair["shots"])
    _flip[0]["caption_style"] = "none"
    check(_sub_key(_flip, 0) != _k_a,
          "改**这一镜**的字幕样式 → 换键：那是母版画进切片的像素，不进键就是拿旧切片冒充新片")
    check(_sub_key(_flip, 1) == _k_b,
          "对照：**别的镜**改了 → 这一镜的键一个字不变。逐镜就是逐镜——"
          "不然「第 3 镜要大字压屏」会把整片 20 镜全重烧")
    _flip[0]["caption_style"] = "big_center"
    _flip[0]["highlight_style"] = "plain"
    check(_sub_key(_flip, 0) != _k_a,
          "高亮那一维单独改也换键（两维都进这一镜的键，不是只进第一维）")
    check(_sub_key(_sub_pair["shots"], 0, film_mode="none") != _k_a,
          "覆盖之后整片那一档仍留在键里：这是**偏保守**的那一侧——"
          "多烧一镜只是浪费，少算一个键是拿旧切片冒充新片")

    # —— 技能正文 ↔ 常量：模型读的是正文，机器判的是常量，漂一次就是「闸拦了人不知道为什么」
    check("每镜必画" in skill and "五条画法通道" in skill,
          "正文第一句就写着这条闸的名字与判据面")
    for ch in cs.DRAWN_CHANNELS:
        check(f"`{ch}`" in skill, f"五条画法通道 `{ch}` 都在正文的通道表里")
    check("`text`" in skill and "只有文字与色块" in skill,
          "fallback 那一档（`text`）与它的中文说法都在正文里")
    check("没有配额可摊" in skill and "打回" in skill,
          "正文写清了这条**没有配额**：任何一镜落在 text 那一档就是打回")
    check("CHANNEL_QUOTA" not in skill and "MAX_TEXT_ONLY_SHARE" not in skill
          and "三分之一" not in skill and "1/3" not in skill,
          "旧配额的两个常量名与那个三分之一都不许再出现在正文里——"
          "留着它，模型就会按一条已经不存在的规矩办事")
    check(f"{cs.CONCRETE_SHARE_TARGET:.0%}" in skill,
          f"文案那条线本身也写在正文里：{cs.CONCRETE_SHARE_TARGET:.0%}")
    for phrase in ("只出提醒", "代码字节里的 import"):
        check(phrase in skill, f"正文写清了这条判据的强度与出处：{phrase}")
    check("⑮" not in skill and "版式族" not in skill,
          "闸⑮ 版式族配额随 demo 一起下架，正文里不再出现（模型读不到已不存在的规矩）")
    # `code_render as cr` 已经在文件顶上 import 过（层E2 那一节用它算注入），
    # 这里再 import 一次会让 `cr` 变成 `main()` 的**局部名**，于是它上面那一段
    # 就成了 UnboundLocalError——同一条通道不写两个出处。
    for num, const, label in ((f"{int(cr.FILL_TARGET * 100)}%", cr.FILL_TARGET, "铺满度目标"),
                              (f"{cr.SPAN_MIN * 100:.0f}%", cr.SPAN_MIN, "空壳那半的铺满闸值"),
                              (f"{cr.TIGHT_MIN * 100:.0f}%", cr.TIGHT_MIN, "小气闸值"),
                              (f"{cr.EMPTY_MIN_RATIO * 100:.0f}%", cr.EMPTY_MIN_RATIO, "占比闸值")):
        check(num in skill, f"闸④ 的{label}在正文里是这个数：{num}（常量 {const}）")

    print("⑨ 文字库：前端零硬编码，取值与中文标签都从这里现取")
    lib = cs.library_props()
    knobs_props = cs.knob_props()

    # —— 枚举与标签与 knob_props 逐字一致 ——
    for dim in cs.LIBRARY_DIMS:
        got = lib["dims"][dim]
        src = knobs_props.get(dim)
        if dim in cs.COLOR_KNOBS:
            # 自由色那一维**故意没有** knob_props 那一份：卡面的每一项都要能反查到枚举源
            # （`plan/vocab.py::enum_source`），自由色四样都不是，上了卡就会让整份计划
            # 被打回。所以它的形状在这里直接断言——空枚举（没有档可列）、空缺省
            # （空串＝没钉，跟色板走）、`kind` 是 color（面板据此渲取色控件而不是一排空档）。
            check(src is None, f"{dim} 不在 knob_props 里（它上不了计划卡）：{src}")
            check(got["kind"] == "color" and got["enum"] == [] and got["default"] == ""
                  and got["value_labels"] == {},
                  f"{dim} 是自由色：面板拿到的形状是「空枚举 + 空缺省 + 取色控件」——{got}")
            continue
        check(src is not None and got["kind"] == "enum",
              f"{dim} 是枚举档（kind=enum），有 knob_props 那一份可对账")
        check(got["enum"] == list(src["enum"]),
              f"{dim} 的 enum 就是 knob_props 那一份：{got['enum']} vs {src['enum']}")
        check(got["value_labels"] == dict(src["value_labels"]),
              f"{dim} 的标签就是 KNOB_VALUE_LABELS 那一份（前端列第二遍必漂）：{dim}")
        check(got["default"] == src["default"], f"{dim} 的缺省也照抄声明，不另设一个")
    check(set(lib["dims"]) == set(cs.LIBRARY_DIMS),
          f"面板管的维度就是 LIBRARY_DIMS 那几个：{sorted(lib['dims'])}")
    # 栏目名也在这一份里：面板上出现的每一个中文词都只有一处出处，前端连「这一栏叫什么」
    # 都不自己写。漏一个就会在界面上露出 caption_position 这种机器名。
    for dim in cs.LIBRARY_DIMS:
        title = lib["dims"][dim].get("title") or ""
        check(title and title != dim, f"{dim} 有中文栏目名（不是把机器名顶上去）")
    check(len({lib["dims"][d]["title"] for d in cs.LIBRARY_DIMS}) == len(cs.LIBRARY_DIMS),
          "栏目名互不相同（两栏同名＝分不出改的是哪一维）")
    check(len(cs.LIBRARY_DIMS) < len(cs.KNOB_FIELDS),
          "文字库只挑声音与字那几维，不是整片开关的全集（画幅帧率不该出现在这里）")
    check("aspect" not in cs.LIBRARY_DIMS and "voice" in cs.LIBRARY_DIMS,
          "画幅在计划卡上改，音色在文字库里挑——两处的分工写死在清单里")

    # —— 预设档：两排卡，每条**只表态自己那一维** ——
    # 2026-10-16 之前一档把形态/位置/动画三件事一起表态，于是「要关键词换色但不要纸条」
    # 说不出口；拆成两维之后，每条卡的 knobs **恰好一个键**——那是「两维正交」在数据上的
    # 样子，多一个键就意味着它偷偷替另一维做了决定。
    check(len(lib["presets"]) == len(cs.CAPTION_PRESETS) + len(cs.HIGHLIGHT_PRESETS),
          f"预设档一档不漏地带出来：{len(lib['presets'])}")
    seen_keys: set[str] = set()
    for pre in lib["presets"]:
        check(bool(pre["label"] and pre["note"]), f"档 {pre['key']} 有档名也有一句什么时候该选它")
        check(pre["key"] not in seen_keys, f"档名 {pre['key']} 不重复（重复了前端认不出点了哪个）")
        seen_keys.add(pre["key"])
        check(set(pre["knobs"]) == {pre["group"]},
              f"档 {pre['key']} 只决定它自己那一维（{pre['group']}）：{sorted(pre['knobs'])}")
        for name, value in pre["knobs"].items():
            check(value in lib["dims"][name]["enum"],
                  f"档 {pre['key']} 的 {name}={value!r} 在枚举里（写错了不会报错，只会渲成别的）")
    check(sorted({p["group"] for p in lib["presets"]}) == ["caption_style", "highlight_style"],
          "两排卡就是字幕与高亮两维——面板按 group 现取栏名，前端不列名单")
    check([p["group"] for p in lib["presets"]]
          == ["caption_style"] * len(cs.CAPTION_PRESETS)
             + ["highlight_style"] * len(cs.HIGHLIGHT_PRESETS),
          "顺序也钉住（前端按出现顺序取 unique 组名，排反了面板上两排就换个先后）")
    for group, table in (("caption_style", cs.CAPTION_PRESETS),
                         ("highlight_style", cs.HIGHLIGHT_PRESETS)):
        got = [p["key"] for p in lib["presets"] if p["group"] == group]
        want = [p["key"] for p in table]
        check(got == want, f"{group} 那一排的档序就是契约里的表：{got} vs {want}")
        check(set(got) == set(lib["dims"][group]["enum"]),
              f"{group} 那一排**铺满**这一维的取值域（漏一个＝有一个值挑不出来）：{got}")
    # 每一维的每个取值都得有卡：两排合起来是「两维取值域的笛卡尔积」的入口，
    # 少一张卡就有组合说不出口——那正是这一轮要治的病。
    check(len(set(p["knobs"][p["group"]] for p in lib["presets"]))
          == len(cs.CAPTION_STYLES) + len(cs.HIGHLIGHT_STYLES),
          "每一档取值都有自己的卡，取值域里没有「只能靠默认」的值")

    # —— 音色分组：声明的音色一个都不许掉 ——
    # 这里数的是 `VOICES`（可选列表）本身，不是「十四个」那个历史数字：方言那一批
    # 2026-10-16 从可选列表里撤了（见 code_spec.VOICES 上方那条注释），但它们仍在
    # `KNOB_VALUE_LABELS["voice"]` 里——两件事，别把标签表也一起瘦身，
    # 否则老片子读回来会露出机器名。下面那条「标签逐字取自 KNOB_VALUE_LABELS」
    # 只单向要求：可选列表里的每个都必须有标签，不是反过来。
    flat = [i["value"] for g in lib["voice_groups"] for i in g["items"]]
    check(sorted(flat) == sorted(cs.VOICES),
          f"可选音色一个不漏、也没有多出来的：{len(flat)} vs {len(cs.VOICES)}")
    check(all(not v.startswith(("zh-HK", "zh-TW")) and "liaoning" not in v
              and "shaanxi" not in v for v in cs.VOICES),
          "可选列表里只剩普通话音色：方言那一批已撤（撤的是清单，不是校验）")
    group_keys = [g["key"] for g in lib["voice_groups"]]
    declared = [p for p, _ in cs.VOICE_GROUPS]
    check(group_keys[:len(declared)] == declared
          and set(group_keys) - set(declared) <= {"other"},
          f"分组次序就是 VOICE_GROUPS 声明的次序，多出来的只可能是末尾的 other：{group_keys}")
    check(all(g["items"] for g in lib["voice_groups"]),
          "没有空组（空组会在界面上留一个点进去什么都没有的标签）")
    check(not any(g["key"] == "other" for g in lib["voice_groups"]),
          "「其他」那一组今天是空的——服务商加了新地区没归组时它会先非空，"
          "界面看得见少了一档，而不是静默吞掉")
    check(all(i["label"] and i["label"] != i["value"] for g in lib["voice_groups"]
              for i in g["items"]),
          "每个音色都有中文标签（漏一个界面上就露 zh-CN-XiaoxiaoNeural 这种机器名）")
    check(all(i["label"] in cs.KNOB_VALUE_LABELS["voice"].values()
              for g in lib["voice_groups"] for i in g["items"]),
          "音色标签逐字取自 KNOB_VALUE_LABELS，不在这里另起一套措辞")

    # —— 色板可选项：样张那一栏要的「底色 × 高亮」两笔 ——
    # 色板名单本身归 test_theme_single_source.py（palettes.json 是唯一出处）；这里管的是
    # 「带出去给前端的那一份」有没有漏、有没有多、中文名是不是也只剩一处。
    pals = lib["palettes"]
    names = list(cs.palettes())
    check(pals and pals[0]["key"] == "" and pals[0]["label"] == cs.PALETTE_DEFAULT_LABEL,
          f"第一项是「默认（母版底色）」：{pals[0] if pals else None}")
    check([p["key"] for p in pals[1:]] == names,
          f"余下各项与 palettes() 逐个同序同名（两份色板名单必漂）："
          f"{[p['key'] for p in pals[1:]]} vs {names}")
    check(len(pals) == len(names) + 1, f"就是「默认 + 全部色板」，不多不少：{len(pals)}")
    check(set(cs.PALETTE_LABELS) == set(names),
          f"每套色板都有中文名（漏一个界面上就露 coral-burst 这种机器名）："
          f"缺 {sorted(set(names) - set(cs.PALETTE_LABELS))}；"
          f"多 {sorted(set(cs.PALETTE_LABELS) - set(names))}")
    check(all(p["label"] and p["label"] != p["key"] for p in pals[1:]),
          "色板选项的中文名不是把机器名顶上去")
    check(len({p["label"] for p in pals}) == len(pals),
          "选项名互不相同（两个同名＝界面上分不出点了哪个）")
    # 三笔画色块：底色、字色、高亮各一。少一笔前端就得自己猜一个颜色，
    # 而「猜」在这个功能里是致命的——色板这一栏存在的理由就是颜色本身。
    for p in pals:
        sw = p["swatch"]
        check(set(sw) == {"page", "text", "accent"} and all(sw.values()),
              f"色块 {p['key'] or '(默认)'} 三笔齐全：{sw}")
    master = cs.token_colors()
    check(pals[0]["swatch"] == {"page": master.get("bg"), "text": master.get("ink"),
                                "accent": master.get("accent")},
          f"「默认」那一项就是母版底色本身（不然它连自己是什么都说不清）：{pals[0]['swatch']}")

    # ── P1 注入契约（2026-10-17 加，2026-10-18 下架）────────────────────────
    # 那一版把「代码必须读 getInputProps() / 必须接住文案 / 必须摆字幕」做成了**契约
    # 错误**，一次打回说三条。图形片改成「逐镜自由画 + 字幕由这一镜代码排」之后，
    # 这三条不再是契约：画法不限，字幕的摆放由 brief、闸⑧ 与视觉评审管。这里只钉住
    # 「旧契约已下架」与仅存的静态事实，免得有人把那一串硬校验又加回来——真机片
    # （rj-e0d34b53）上它曾把整片 14 镜一起挡住，而画面本身没有问题。
    print("P1 旧注入契约已下架：不读注入不再打回，字幕静态事实仍读得出")
    plain = "export const Shot = () => (<AbsoluteFill><svg /></AbsoluteFill>);"
    _, p1a = cs.validate_code_spec(_spec(shots=[_shot(code=plain)]))
    check(not any("没读注入" in e for e in p1a),
          f"「没读注入」不再是契约错误（{p1a}）")
    check(cs.caption_any_by_code(
        "import {CaptionText} from '@dg/film'; <CaptionText fitWidth={600} />")
        and not cs.caption_any_by_code(plain),
        "代码里摆没摆字幕仍能从代码静态读出（captionFromCode 的唯一出处）")
    check(cs.caption_any_by_code("<CaptionLayer />")
          and not cs.caption_any_by_code("const s = '<CaptionTextish>';"),
          "只认真的元件：字符串里的名字不算")
    check(cs.caption_any_by_code("<CaptionFrame>{(art) => <svg />}</CaptionFrame>"),
          "分带外框 <CaptionFrame/> 也算「这一镜自己画了字幕」——它内部画的就是 CaptionText")

    # ── P2 风格承诺（2026-10-17 加，2026-10-18 下架）───────────────────────
    # `style_facts` / `style_promise_findings` / `check_shot_timeline` 那一族
    # 「量承诺、数拍点」的判据随「零件库 + 配额」一起下架：图形片改成逐镜自由画之后，
    # 「细规则线 ≥2 条」这类承诺量与画面质量无关，只会逼模型交假的读数；拍点数得出
    # 次数，数不出「动得对不对」。审美那一段现在由视觉评审（`motion/design_review.py`）
    # 看真渲出来的静帧（M2）。这里只钉住「下架」与还在的那条预览链。
    print("P2 旧风格承诺/时间轴已下架：预览链仍带图、空帧仍记未判")
    s_ok, s_errs = cs.validate_code_spec(_spec(style="编辑杂志感：大留白、细规则线、左对齐"))
    check(s_errs == [], f"style 那一句照收（{s_errs}）")
    check(s_ok["style"].startswith("编辑杂志感"), "style 随 spec 入库（展示态，不参与判据）")
    check(not hasattr(cs, "style_promise_findings")
          and not hasattr(cs, "check_shot_timeline"),
          "旧判据的两个入口都已下架（有人加回来时这一条会红）")
    s_long, _ = cs.validate_code_spec(_spec(style="长" * 200))
    check(len(s_long["style"]) == cs.STYLE_MAX_CHARS,
          f"style 截到 STYLE_MAX_CHARS={cs.STYLE_MAX_CHARS}——它是一句话，不是构图")

    # —— P2-b 自检工具另一头：`preview_shot` 带图看帧、空帧记未判 ——
    import asyncio

    class _FakeLLM:
        def __init__(self, text: str) -> None:
            self._text = text

        async def complete(self, messages):
            return type("R", (), {"content": self._text})()

    pv_row = _shot()

    async def _preview_case():
        ep_ok = {"stills": lambda src, props, frames:
                 [{"frame": frames[0], "jpeg_b64": "aGk="}]}
        ep_empty = {"stills": lambda src, props, frames: []}
        full = await _sc.preview_shot(code=pv_row["code"], shot=pv_row,
                                      llm=_FakeLLM("画面里有一座塔。"), client=ep_ok)
        empty = await _sc.preview_shot(code=pv_row["code"], shot=pv_row,
                                       llm=_FakeLLM("x"), client=ep_empty)
        return full, empty

    pv_full, pv_empty = asyncio.run(_preview_case())
    check(pv_full["ok"] and pv_full["review"] == "画面里有一座塔。",
          "看帧子调用的话术原样交回（工具返回值只带话）")
    check(not pv_empty["ok"] and "未判" in pv_empty["note"],
          f"一帧都没回 → 未判话术说清不是「没问题」：{pv_empty['note'][:50]}")

    # —— 字幕缺省收敛（2026-10-20：用户原话「我要默认黑边白字」）——
    print("P1-d 模型写了无描边 on_picture → 系统收回白字黑边")
    _cap_raw = {"caption_style": "on_picture",
                "shots": [{"id": "s01", "caption_style": "on_picture"},
                          {"id": "s02", "caption_style": "paper",
                           "code": "x"}]}
    _cap_fix, _cap_notes = cs.autofix_caption_default(_cap_raw)
    check(_cap_fix["caption_style"] == "outlined",
          "整片 on_picture → outlined（白字黑边）")
    check("caption_style" not in _cap_fix["shots"][0],
          "逐镜 on_picture 覆盖去掉、继承整片那档")
    check(_cap_fix["shots"][1]["caption_style"] == "paper",
          "有理由的逐镜版式（paper）保留，只收无描边那一档")
    check(any("on_picture" in n and "outlined" in n for n in _cap_notes),
          f"改动写进回执：{_cap_notes}")
    _cap_user, _ = cs.autofix_caption_default(
        {"caption_style": "on_picture"}, user_request="字幕不要黑边，贴画面就行")
    check(_cap_user["caption_style"] == "on_picture",
          "用户原话点名「不要黑边」时不改——那是用户的选择")
    _cap_empty, _ = cs.autofix_caption_default({"shots": []})
    check(_cap_empty["caption_style"] == "by_shot",
          "没写这一栏时落成 by_shot（2026-10-23 改的缺省：字幕交给镜头代码设计），"
          "卡面上看到的就是会渲出来的那档")
    _cap_hl, _ = cs.autofix_caption_default(
        {"caption_style": "outlined", "highlight_style": "color"})
    check(_cap_hl["highlight_style"] == "plain",
          "白字黑边 + 关键词换色 → 收回整条同色（2026-10-20 真机字幕不统一）")
    _cap_hl_user, _ = cs.autofix_caption_default(
        {"caption_style": "outlined", "highlight_style": "color"},
        user_request="字幕要白字黑边，关键词换色")
    check(_cap_hl_user["highlight_style"] == "color",
          "用户点名「关键词换色」时保留——那是用户的选择")

    # —— 2026-10-22 真机：《停》v3 文档写了「白色，无描边、无底色」「不需要关键词」，
    #   模型据此给了 on_picture + color。原先 `_CAPTION_REQUESTS` 里的裸「无底」命中了
    #   「无底色」，整条提前 return，连 highlight 也一起放过——用户看到的就是
    #   「贴画面 + 关键词换色」。字幕形态与关键词着色从此各判各的。——
    print("P1-d′ 用户原话含「无底色」不再误判成「用户要无底条」")
    _cap_v3, _cap_v3_notes = cs.autofix_caption_default(
        {"caption_style": "on_picture", "highlight_style": "color",
         "shots": [{"id": "s01", "caption_style": "on_picture"}]},
        user_request="白字、黑色描边、无底色，字号约画面高 3.5%；整条同色，不换色、不点亮关键词")
    check(_cap_v3["caption_style"] == "outlined",
          f"「无底色」不是「无底条」：字幕仍收回白字黑边（当前 {_cap_v3['caption_style']!r}）")
    check(_cap_v3["highlight_style"] == "plain",
          f"同一句里的「不点亮关键词」是否定，关键词着色收回 plain（当前 {_cap_v3['highlight_style']!r}）")
    check("caption_style" not in _cap_v3["shots"][0],
          "整片收回白字黑边后，逐镜 on_picture 覆盖也一并去掉")
    check(any("highlight" in n or "关键词" in n for n in _cap_v3_notes),
          f"取消强调写进回执：{_cap_v3_notes}")
    # 「不要关键词」「不加高亮」是否定式点名，不算用户要关键词。
    _cap_neg, _ = cs.autofix_caption_default(
        {"caption_style": "outlined", "highlight_style": "karaoke"},
        user_request="不需要关键词，字幕整条一个颜色")
    check(_cap_neg["highlight_style"] == "plain",
          f"「不需要关键词」是否定，仍收回 plain（当前 {_cap_neg['highlight_style']!r}）")

    # —— 叠化 × 逐镜烧进画面的字幕 = 两条字幕同屏（2026-10-22 真机「有的重叠」）——
    print("P1-d″ 两侧都有字幕的叠化接缝改硬接，其余接缝照原档")
    _tr_raw = {"transition": "dissolve", "transition_sec": 0.6,
               "shots": [{"id": "s01", "text": "第一句"},
                         {"id": "s02", "text": "第二句"},
                         {"id": "s03", "text": ""},
                         {"id": "s04", "text": "第四句"}]}
    _tr, _tr_notes = cs.autofix_caption_transition(_tr_raw)
    _tr_trans = [s.get("transition") for s in _tr["shots"]]
    check(_tr_trans == ["none", None, None, None],
          f"只有 s01→s02 两侧都有字 → 只这一处硬接（当前 {_tr_trans}）")
    check(_tr["transition"] == "dissolve", "整片转场那一档不改，只写逐镜覆盖")
    check(_tr_notes and "s01" in _tr_notes[0] and "叠" in _tr_notes[0],
          f"回执点名改了哪一处、为什么：{_tr_notes}")
    _tr_again, _tr_again_notes = cs.autofix_caption_transition(_tr)
    check(_tr_again["shots"][:2] == _tr["shots"][:2] and not _tr_again_notes,
          "幂等：再跑一遍不再改、不出说明")
    # 没字幕的整片：一个接缝都不动。
    _tr_none, _tr_none_notes = cs.autofix_caption_transition(
        {"transition": "dissolve",
         "shots": [{"id": "s01", "text": ""}, {"id": "s02", "text": ""}]})
    check(_tr_none_notes == [], "两侧都没字幕：叠化照旧，不瞎改转场")

    # —— 中英双字幕（2026-10-21：用户原话「字幕就用中英双字幕，黑边白字就行了，
    #   字体适中，且不需要 ⌈⌋ 这种符号」）——
    print("P1-e 中英双字幕：英文单独一栏，装饰括号不画进画面")
    _en_spec, _en_err = cs.validate_code_spec(
        _spec(shots=[_shot(text="「中子星」一茶匙物质重约十亿吨",
                           text_en="⌈A teaspoon of neutron star⌋ weighs about a billion tons",
                           highlight=["中子星"])]))
    check(_en_err == [], f"带英文字幕的合规镜零错误（实际 {_en_err}）")
    _en_shot = _en_spec["shots"][0]
    check(_en_shot.get("text_en") ==
          "A teaspoon of neutron star weighs about a billion tons",
          f"英文原样收下、装饰括号剥掉（当前 {_en_shot.get('text_en')!r}）")
    check(_en_shot.get("text") == "中子星一茶匙物质重约十亿吨",
          f"中文那一栏也剥掉「」（当前 {_en_shot.get('text')!r}）")
    check(_en_shot.get("highlight") == ["中子星"],
          "高亮词按**剥完**的文案判子串——先判后剥会让这个词落空")
    # 没写英文照旧：老片子的 spec 一个字节都不变，母版也不会多排一行。
    _no_en, _ = cs.validate_code_spec(_spec())
    check(_no_en["shots"][0].get("text_en") == "", "没写英文字幕 → 空串，不报错")
    # 长英文只报不改句（截断会留下半句话，比报错更难查）。
    _long_en, _long_err = cs.validate_code_spec(
        _spec(shots=[_shot(text_en="x" * (cs.MAX_TEXT_EN_CHARS + 1))]))
    check(any("英文字幕" in e for e in _long_err), f"超长英文有回执：{_long_err[:1]}")
    check(len(_long_en["shots"][0].get("text_en") or "") == cs.MAX_TEXT_EN_CHARS + 1,
          "超长英文不截断——留给模型自己压短")
    # 句尾句号不画（2026-10-21 用户点名：句尾不要句号，用两个空格替代）。
    _dot, _dot_err = cs.validate_code_spec(_spec(shots=[_shot(
        text="第一句。第二句。", text_en="One sentence. Another sentence.")]))
    check(_dot_err == [], f"带句号的合规镜零错误（实际 {_dot_err}）")
    check(_dot["shots"][0]["text"] == "第一句  第二句",
          f"句中句号换成两个空格、句尾去掉（当前 {_dot['shots'][0]['text']!r}）")
    check(_dot["shots"][0]["text_en"] == "One sentence. Another sentence",
          "英文只去句尾那个点，词中间的保留")
    _num, _ = cs.validate_code_spec(_spec(shots=[_shot(text="有高血压的占 44.7%")]))
    check(_num["shots"][0]["text"] == "有高血压的占 44.7%",
          "数字里的点一个都不动（44.7% 不能变成 447%）")
    check(cs.strip_caption_periods("3.6 倍。") == "3.6 倍", "句尾句号去掉，数字不动")

    # 缓存键：英文是画进切片的第二行，改一个词就换像素。
    _k_base = cs.shot_cache_key(_no_en["shots"][0], film=FILM,
                                master_hash="a" * 16, lockfile_hash="b" * 16)
    _k_empty = cs.shot_cache_key({**_no_en["shots"][0], "text_en": ""}, film=FILM,
                                 master_hash="a" * 16, lockfile_hash="b" * 16)
    check(_k_base == _k_empty, "英文为空时不进键——老片子的键不整体漂移")
    _k_en = cs.shot_cache_key({**_no_en["shots"][0], "text_en": "one teaspoon"},
                              film=FILM, master_hash="a" * 16, lockfile_hash="b" * 16)
    check(_k_en != _k_base, "英文字幕变 → 第二行像素变 → 换键")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
