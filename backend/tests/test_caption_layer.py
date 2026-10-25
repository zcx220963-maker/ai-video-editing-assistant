# -*- coding: utf-8 -*-
"""字幕元件化：母版那套字幕能力还给镜头代码之后，守四件事。

用户的疑问原话是「字幕为什么非要和视频分开？不能和视频交互在一起吗」。查下来是一处真缺陷：
`subtitle_mode: inline`（文字进画面）这一档，母版只把两个零件还给镜头代码
（`charStartFrames` / `splitByHighlights`），而逐词点亮、撕纸高亮下划线、按两行容量求字号
全在母版内部的局部组件里、没导出——于是「字幕进画面」与「逐词点亮」只能二选一。
这一轮把三件从 `@dg/film` 导出了（`CaptionLayer` / `CaptionText` / `useCaptionTiming`）。
本文件守的是这次交付**真的成立**，而不是「文件里有这几个词」：

① **一条渲染路径**：镜头代码摆出来的那条字幕与母版那条必须是**同一份实现**。这条一旦松了，
   就会长出第二份字（第二个字号、第二种断行），而两处不一致只在成片上看得见——预览与
   样张都看不出来。判据落在代码结构上：纸条里的字走 `CaptionText`、整件走纸条那个组件、
   「未读到的字淡显」只写一次。
② **母版一个字都不许变**：母版自己那处调用点仍直接调 `<Caption>`（十二项实参原样），
   而不是绕一手 `<CaptionLayer>`。老片子、样张、闸⑤ 的回读表全指着它。
③ **名字不撞车**：`@dg/film` 是模块级白名单（加导出不用改白名单），代价是新名字可能撞进
   既有的动效/打磨导出集合里——那种撞法不会报错，只会在模型 import 时静默取到另一个东西。
④ **闸㉜ 只判可判的那一条**，且 SKILL.md 不再说着已经修好的毛病（本仓库一路在治的假绿）。

跑法：`python tests/test_caption_layer.py`（容器内 `PYTHONPATH=/app`）。
读不到 `remotion/` 或 SKILL.md 的那几节**记未验证并列出**，不折算成通过。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import code_spec as cs

_checks = 0
_fails = 0
_unverified: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def unverified(label: str, why: str) -> None:
    _unverified.append(f"{label}——{why}")
    print(f"  ~ 未验证：{label}（{why}）")


def _remotion_dir() -> Path | None:
    """定位 `remotion/`：容器里是 `/app/remotion`，工作树里是仓库根的 `remotion/`。"""
    env = os.environ.get("REMOTION_DIR")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    backend = Path(__file__).resolve().parents[1]
    for cand in (backend / "remotion", backend.parent / "remotion"):
        if cand.is_dir():
            return cand
    return None


def _skill_md() -> Path:
    return (Path(__file__).resolve().parents[1]
            / "examples/retired/motion_explainer/SKILL.md")


#: 三件元件的名字（新导出）。改这里的名字等于换 API，别只改一处。
NEW_EXPORTS = ("CaptionLayer", "CaptionText", "useCaptionTiming")


def _between(text: str, start: str, end: str) -> str:
    """取 `start` 到 `end` 之间那一段。找不到标记就返回空串（下面的 check 会红）。"""
    i = text.find(start)
    j = text.find(end, i + 1) if i >= 0 else -1
    return text[i:j] if i >= 0 and j > i else ""


def _strip_comments(text: str) -> str:
    """抹掉注释再判「这段里有没有出现某个标签」。

    不抹的话，「别这么写」的注释会被当成「这么写了」——母版正文里就有好几处
    在注释里提 `<CaptionLayer/>`（讲这件元件怎么用）。判据必须看代码，不看话。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _set_literal(src: str, name: str) -> set[str]:
    """从 `lint-rules.mjs` 里读 `const NAME = new Set([...])` 的那串字面量。

    解析失败返回空集——调用方**必须**把空集当失败：假绿比不报更坏
    （规则被改名后这里静默返回空集，撞名检查就永远通过了）。
    """
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\)", src, re.S)
    return set(re.findall(r"'([^']+)'", m.group(1))) if m else set()


# ---------------------------------------------------------------------------
# ① 一条渲染路径
# ---------------------------------------------------------------------------
def test_single_render_path(remo: Path | None) -> None:
    print("\n=== ① 镜头代码摆的字与母版那条字，是同一份实现 ===")
    if not remo or not (remo / "src" / "FilmLayout.tsx").is_file():
        unverified("一条渲染路径", "读不到 remotion/src/FilmLayout.tsx")
        return
    src = (remo / "src" / "FilmLayout.tsx").read_text(encoding="utf-8")

    film = _strip_comments(_between(src, "export const FilmLayout", "const Caption: React.FC<{"))
    pill = _strip_comments(_between(src, "const Caption: React.FC<{",
                                    "export function charStartFrames"))
    text_c = _strip_comments(_between(src, "export const CaptionText: React.FC<CaptionTextProps>",
                                      "export interface CaptionLayerProps"))
    layer = (_strip_comments(src[src.index("export const CaptionLayer"):])
             if "export const CaptionLayer" in src else "")

    check(bool(film) and bool(pill) and bool(text_c) and bool(layer),
          "四段都能取到（母版 / 纸条 / 字 / 整件）——取不到就是结构被改过，下面的判据都作废")

    # 纸条（母版那张圆角条）里的字必须走 CaptionText：母版那条与镜头代码那条共用一个实现。
    check("<CaptionText" in pill,
          "母版纸条里的字走 <CaptionText/>——不是纸条里自己再排一遍")
    # 片段切法只有一处（useCaptionTiming）。纸条里再切一遍就多了一个「高亮词怎么认」的出处。
    check("splitByHighlights(" not in pill,
          "撕纸高亮的片段切法不在纸条里再来一遍（只此一处：useCaptionTiming）")
    check(src.count("splitByHighlights(") == 2,
          f"splitByHighlights 全篇只两处（定义 + useCaptionTiming 一次调用）："
          f"实得 {src.count('splitByHighlights(')} 处")
    # 逐字点亮那档「未读到的字淡显」的透明度：只写一次，第二处就是第二份实现。
    check(text_c.count("0.38") == 1 and src.count("0.38") == 1,
          f"逐字点亮「未读到」那一档的透明度只有一处实现：全篇 {src.count('0.38')} 处")
    # 整件（CaptionLayer）必须复用纸条那个组件，而不是自己再搭一套纸条/字号/动画。
    check("<Caption\n" in layer or "<Caption " in layer,
          "整件 <CaptionLayer/> 复用纸条组件 <Caption/>，没有第二套纸条")
    check("borderRadius" not in layer,
          "整件里没有自己的纸条圆角——纸条只有一份（在 <Caption/> 里）")
    # 字号只有一处：纸条不给字**另求**一个 CSS 的 fontSize（另求就是两处可能算出两个数；
    # `cap.fontSize` 只用来换算上浮距离，那是几何不是字号本身）。
    # 2026-10-24：这条原来写「pill 里一个 `fontSize:` 都不许有」，中英双语（2026-10-21）
    # 之后不再成立——英文那一行也要有字号，而它用的是 `enSize`，**就是 `zhSize` 本身**
    # （`const enSize = zhSize`），同一个数分给两行，不是第二次求。
    # 所以判据从「不许出现」收准成「不许另求」：字号只在 pill 里求**一次**
    # （`zhSize` 那一处），唯一那处 `fontSize:` 只能是 `enSize`。
    check(re.search(r"\bfontSize\s*:(?!\s*enSize\b)", pill) is None
          and pill.count("fitCaptionFontSize(") == 1,
          f"纸条只求一次字号（zhSize），唯一那处 fontSize 只能是 enSize——英文行与中文行"
          f"共用同一个数：实得 fontSize: {len(re.findall(r'fontSize\\s*:', pill))} 处、"
          f"求字号 {pill.count('fitCaptionFontSize(')} 次")
    check("fitCaptionFontSize(" in text_c,
          "自动字号在 <CaptionText/> 里（它才是「字」的那一件）")


# ---------------------------------------------------------------------------
# ② 母版一个字都不许变
# ---------------------------------------------------------------------------
def test_master_pixels_untouched(remo: Path | None) -> None:
    print("\n=== ② 母版自己那处调用点原样（老片子一个像素不变） ===")
    if not remo or not (remo / "src" / "FilmLayout.tsx").is_file():
        unverified("母版调用点", "读不到 remotion/src/FilmLayout.tsx")
        return
    src = (remo / "src" / "FilmLayout.tsx").read_text(encoding="utf-8")
    film = _strip_comments(_between(src, "export const FilmLayout",
                                    "const Caption: React.FC<{"))
    # 母版那次调用必须是 <Caption …>（实参原样），不是绕一手 <CaptionLayer/>：
    # 绕一手虽然今天画得一样，但它把「母版那条字幕」变成了「元件的一个特例」，
    # 以后改元件的默认值就会连母版一起改——而母版那条是不许动的。
    check("<Caption\n" in film,
          "母版仍直接调 <Caption/>（不是绕一手 <CaptionLayer/>）")
    check(re.search(r"<\s*CaptionLayer\b", film) is None,
          "母版内部不出现 <CaptionLayer/>（它的自守是给镜头代码的，母版那条不走它）")
    # 2026-10-16 起三个旧旋钮（mode / position / anim）折进两张查找表，母版那次调用
    # 改为把**整档配方**传下去：`look={CAPTION_LOOK[…]} / hlLook={HIGHLIGHT_LOOK[…]}`。
    # 传配方而不是传档名，母版这一层就再也拼不出「档名表里没有的组合」。
    for arg in ("text={text}", "highlight={highlight}", "words={words}",
                "look={CAPTION_LOOK[captionStyle]}", "hlLook={HIGHLIGHT_LOOK[highlightStyle]}",
                "opacity={captionIn}", "geo={geo}", "bottom={captionBottom}", "tone={tone}",
                "hl={hlColor}", "accent={accentColor}"):
        check(arg in film, f"母版那十一项实参原样：{arg}")


# ---------------------------------------------------------------------------
# ③ 三件真的导出、且不撞名
# ---------------------------------------------------------------------------
def test_exports_and_name_collisions(remo: Path | None) -> None:
    print("\n=== ③ 三件从 @dg/film 导出，且不撞既有导出名 ===")
    if not remo or not (remo / "src" / "FilmLayout.tsx").is_file():
        unverified("导出与撞名", "读不到 remotion/src/FilmLayout.tsx")
        return
    src = (remo / "src" / "FilmLayout.tsx").read_text(encoding="utf-8")
    for name in NEW_EXPORTS:
        check(re.search(rf"^export (const|function) {name}\b", src, re.M) is not None,
              f"{name} 是 @dg/film 的顶层导出（镜头代码 import 得到）")
    # 名字撞车：lint 的字段记账规则认 F / Field 这两个名字；动效、打磨两族另有一批导出名。
    check(not ({"F", "Field"} & set(NEW_EXPORTS)),
          "三件不叫 F / Field（字段记账规则认这两个名字，撞了会串味）")

    rules = remo / "lint-rules.mjs"
    if not rules.is_file():
        unverified("撞名（动效/打磨导出集合）", "读不到 remotion/lint-rules.mjs")
        return
    rsrc = rules.read_text(encoding="utf-8")
    motion = _set_literal(rsrc, "MOTION_EXPORTS")
    polish = _set_literal(rsrc, "POLISH_EXPORTS")
    allowed = _set_literal(rsrc, "ALLOWED_IMPORTS")
    check(len(motion) >= 5 and len(polish) >= 5,
          f"两个导出集合解析出内容（动效 {len(motion)} 个 / 打磨 {len(polish)} 个）"
          "——解析成空集就是假绿，那等于没查撞名")
    check(not (set(NEW_EXPORTS) & (motion | polish)),
          f"三件不与 @dg/motion、@dg/polish 的导出撞名："
          f"{sorted(set(NEW_EXPORTS) & (motion | polish))}")
    check("@dg/film" in allowed,
          "白名单里有 @dg/film（加导出不必改白名单：它是模块级的）")


# ---------------------------------------------------------------------------
# ④ 闸㉜：红法、放行、归属
# ---------------------------------------------------------------------------
def test_gate_caption_ownership() -> None:
    print("\n=== ④ 闸㉜ 字幕归属：红法、两处放行、在硬闸名单里 ===")
    plain = ("import { CaptionLayer, FilmLayout } from '@dg/film';\n"
             "export default () => <FilmLayout><CaptionLayer/></FilmLayout>;\n")

    red, warns, und = cs.check_caption_ownership(
        [{"id": "s03", "caption_style": "none", "code": plain}])
    check(len(red) == 1 and "㉜" in red[0] and "s03" in red[0],
          f"申报了 caption_style=none 却摆着整条字幕 → 红，且点名到镜："
          f"{red[0][:60] if red else '（没红）'}…")
    check(red and '"none"' in red[0],
          "说的话里带上那一档（模型才知道自己申报的是什么）")
    check(not warns and not und, "闸㉜ 只出硬判（不掺提醒、不掺未判）")

    ok1, _, _ = cs.check_caption_ownership([{"id": "s03", "code": plain}])
    check(ok1 == [],
          "摆了整条字幕、档位不申报 → 放行（摆放不必申报：扫到就注入 captionFromCode，"
          "母版那条让位——这一条旧版判红，现在结构上已不可能同屏两条）")

    ok2, _, _ = cs.check_caption_ownership(
        [{"id": "s04", "caption_style": "paper", "code": plain}])
    check(ok2 == [], "申报了要画的那一档 + 摆了整条字幕 → 放行")

    ok3, _, _ = cs.check_caption_ownership(
        [{"id": "s05", "caption_style": "paper",
          "code": "<FilmLayout><CaptionText/></FilmLayout>"}])
    check(ok3 == [],
          "用 <CaptionText/> 摆画面里的字 → 放行（typography 那一族就该这么写，"
          "这条闸一旦连它一起禁，整族都被误伤）")

    ok4, _, _ = cs.check_caption_ownership(
        [{"id": "s06", "caption_style": "none",
          "code": "<FilmLayout><div>手搓的字</div></FilmLayout>"}])
    check(ok4 == [],
          "手搓文字不判（文本扫描分不清「没画」与「用别的方式画了」）——所以㉜ 是单向的")

    check(cs.check_caption_ownership in cs.WHOLE_FILM_GATES,
          "㉜ 在 WHOLE_FILM_GATES 里（三处调用点自动都生效）")
    check(cs.check_caption_ownership not in cs.SOFT_GATES,
          "㉜ 是硬闸不是提醒（它挡的是「代码里那段字幕永远不出现」，不是品味）")
    check(cs.WHOLE_FILM_GATES[-1] is cs.check_caption_ownership,
          "㉜ 排在整片层判据的最后一位（新闸接在尾上，不插队）")


# ---------------------------------------------------------------------------
# ⑤ 文档与样张底稿：能力教会了模型，预览也不再手搓
# ---------------------------------------------------------------------------
def test_docs_and_preview_sample() -> None:
    print("\n=== ⑤ SKILL.md 与样张底稿：不再说着已修好的毛病、不再手搓 ===")
    skill = _skill_md()
    if not skill.is_file():
        unverified("SKILL.md", f"读不到 {skill}")
    else:
        s = skill.read_text(encoding="utf-8")
        check("下它不会自动生效" not in s,
              "SKILL.md 不再写着「inline 下逐词点亮不会自动生效」——那句是这次修掉的毛病，"
              "文档还留着它就是把模型赶回旧做法")
        for name in NEW_EXPORTS:
            check(name in s, f"SKILL.md 提到 {name}（白名单里有、正文里没有 = 模型手里没有）")
        check(re.search(r"^\|\s*㉜\s*\|", s, re.M) is not None,
              "闸表里有 ㉜ 一行（模型不知道的闸等于没有）")
        check("闸㉜" in s, "字幕那一节点名了闸㉜（摆了整条的镜要知道那条自相矛盾的判据）")

    # 模型「有得抄」的那条链是：索引里选得到卡 → 卡的参考实现指得到 demo 文件。
    # 缺一环就等于这一式只存在于源码里：选卡看不到它、取材拿不到 `derived_from`。
    shots = _skill_md().parent / "shots"
    card = shots / "typography/caption-in-frame.md"
    index = shots / "INDEX.md"
    if not card.is_file() or not index.is_file():
        unverified("选卡与取材链", f"读不到 {card} 或 {index}")
    else:
        card_src = card.read_text(encoding="utf-8")
        check(card_src.lstrip().startswith("---") and
              re.search(r"^name:\s*caption-in-frame\s*$", card_src, re.M) is not None,
              "卡头 frontmatter 的 name 与文件名同源（判据按这个 id 认卡）")
        check(re.search(r"^##\s*参考实现\s*$", card_src, re.M) is not None
              and "demos/typography/caption-in-frame" in card_src,
              "卡的「参考实现」指到这一式的 demo 目录（没有它，取材轮只拿到一个卡名）")
        # 索引的 demo 栏印的是**相对 `demos/` 的文件名**（不带头那个前缀），照抄即可。
        line = [ln for ln in index.read_text(encoding="utf-8").splitlines()
                if ln.startswith("- `typography/caption-in-frame`")]
        check(len(line) == 1 and "typography/caption-in-frame/TextOverArt.tsx" in line[0]
              and re.search(r"demo `\?`| 0 B$", line[0]) is None,
              "索引里有这一张卡、且 demo 栏是**盘上真有的文件名与字节数**："
              f"{line[0][-40:] if line else '（索引里没有这一行）'}")

    sample = cs.sample_shot_code()
    if not sample:
        unverified("样张底稿", "读不到 remotion/src/preview/caption-sample.tsx")
        return
    check("<CaptionLayer" in sample or "<CaptionText" in sample,
          "样张底稿用母版的字幕元件排字（<CaptionLayer/> 或 <CaptionText/>），不手搓")
    check("splitByHighlights(" not in sample and "charStartFrames(" not in sample,
          "样张底稿里没有自己再实现一遍片段切分/逐字时间（那就是第二份出处）")


def main() -> int:
    remo = _remotion_dir()
    if not remo:
        unverified("remotion/ 下的全部判据", "找不到 remotion 目录")
    test_single_render_path(remo)
    test_master_pixels_untouched(remo)
    test_exports_and_name_collisions(remo)
    test_gate_caption_ownership()
    test_docs_and_preview_sample()

    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _unverified:
        print(f"未验证 {len(_unverified)} 项（不算通过）：")
        for u in _unverified:
            print(f"  · {u}")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
