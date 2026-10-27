"""渲染前确认门：编排完成、正要渲染时，先把编排结果给用户看，等确认再渲。

**为什么需要它（用户原话的意思）。** 真机跑完一次才暴露问题：用户看到成片才发现
「最后一句被截断了」。用户的诉求是——渲染前就把编排结果（提取内容、切分、分镜、
画面、声音怎么排的）摆出来，让用户决定「改」还是「直接渲」；改则重做计划再弹一次，
直到满意为止。另有一条硬要求：「**不确认绝不渲染**」。

**为什么是一个独立模块。** 「要不要拦、拦什么、帧长什么样」是策略，
agent 循环只该负责「按策略挂起」。把它抽出来就能单独测，也避免继续往已经很长的
``agent.py`` 里塞判断。

**为什么复用 ask/审批机制。** 渲染确认本质上就是「问用户一道题」，与 ``ask_user``
同一形态：编号选项 + 推荐徽标 + 铅笔自定义。复用同一套挂起/续跑/落盘，
好处是「刷新后重弹」「多题分页」「自定义输入」这些能力一次性对两条路径都生效。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

# 触发确认的节点名。做成常量而不是散落的字面量：将来要给别的节点加门只需改这里。
RENDER_NODE = "render_video"
#: 零素材图形科普片的出片通道（代码分镜 → 渲染容器逐镜渲 → 母带）。
#: 一次按下烧掉分钟级到十分钟级的不可逆算力，与 ``render_video`` 同一族——
#: 门认的是这一族，不是某一条实现。
MOTION_NODE = "render_motion_code"
#: 网页（HTML/URL）逐帧截屏出片的通道。
WEB_NODE = "render_web"
#: 所有「一按就烧像素、且不可逆」的出片通道。门认的是**这一族**，不是某一个名字——
#: 只认 render_video 等于给图形科普片那条路留了个「不确认就渲」的口子。
#: render_web 也在这一族：它同样是一次几分钟的不可逆出片（headless 逐帧截图 + 合成）。
#: 它不是 submit+poll 的长任务，所以只有门覆盖它，进度条那一路仍按长任务节点认。
RENDER_NODES = frozenset({RENDER_NODE, MOTION_NODE, WEB_NODE})

#: **局部改那一族**：``patch_motion_code``（图形科普片）与 ``patch_video``（口播/素材片）。
#: 名字在这里出现但**不进** ``RENDER_NODES``，是有意的：
#: 门要拦的是「用户还没看过任何东西，模型就按下了一次几分钟、不可逆的算力」。
#: 走到局部改时用户手里已经有一版成片，改哪一格是他自己点的（前端选区 → POST /motion/patch
#: 或 /timeline/patch），而且这一版只重烧受影响的那几镜/那几个窗口——缓存命中的切片一个像素都不重烧。
#: 要重做整片仍然只能走 ``render_motion_code`` / ``render_video``，那两道门照旧开着。
PATCH_NODES = frozenset({"patch_motion_code", "patch_video"})

# 渲染确认的选项。key 会作为 decision 回喂给服务端并进模型上下文，
# 所以用自解释的短词而不是 opt1/opt2。
CONFIRM_OPTION = "confirm_render"
ADJUST_OPTION = "adjust_plan"
# 时长冲突专用：保内容完整（放宽时长）/ 就按原时长截断
KEEP_FULL_OPTION = "keep_full_sentence"
TRUNCATE_OPTION = "truncate_as_planned"


def _dry_run_call(tc: Any) -> bool:
    """这一次 render_video 调用是不是 dry_run（只要账、不出片）。

    参数可能是真布尔，也可能是模型写出的字符串 "true"/"false"，所以逐字判一下：
    ``bool("false")`` 为真，照原样判会把「不 dry」读成「dry」。
    """
    args = getattr(tc, "arguments", None) or {}
    if not isinstance(args, Mapping):
        return False
    value = args.get("dry_run")
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off", "否", "不")
    return bool(value)


def ungated_render_nodes(tool_calls: Any, *, enabled: bool,
                         already_asked: Any = (), go_straight: bool = False) -> list[str]:
    """这一批调用里，**门还该拦得住**的出片通道名（去重、保持出现顺序）。

    四条判据都收在这一个函数里，调用方只管「名单非空就挂起」：

    * ``enabled`` —— 门开关在配置里，不在这里硬编；关着就一律放行。
    * ``go_straight`` —— 用户这一轮在入口上选了「直接出片」：不等确认。
      代价写在这里：少一次人工验收，画面不满意可能已经烧完。但那个开关的意义
      就是**不打断**；拦下来等于把它变成假开关。
    * 看参数：``dry_run`` 的那一次不出片，它本身就是给用户看的那一眼编排结果——
      再拦一道「确认渲染吗」是把同一道题问两遍。
    * 看**通道**：同一条 run 里用户答过 ``render_video`` 那道题，不等于批准了
      ``render_motion_code``——那是另一笔不可逆的算力，所以记名不记一位布尔。
      同一通道答过就不再重问（真机事故：同一道题问了几十遍，见
      ``RunState.render_gate_asked`` 的说明）。
    """
    if not enabled or go_straight:
        return []
    asked = set(already_asked or ())
    out: list[str] = []
    for tc in tool_calls or ():
        name = getattr(tc, "name", None)
        # 认整族：图形科普片那条路也是「一按就烧像素、不可逆」的出片，
        # 只认 render_video 等于给它留了个「不确认就渲」的口子。
        if name not in RENDER_NODES or name in asked or _dry_run_call(tc):
            continue
        if name not in out:
            out.append(name)
    return out


def _fmt(seconds: Any) -> str:
    try:
        return f"{float(seconds):.0f}s"
    except (TypeError, ValueError):
        return "?"


def duration_conflict(preview: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """成片时长会不会把话截断？会的话给出「放宽到多少秒才够」。

    这正是真机那次的问题：用户说 90 秒，模型发现最后一句金句会被截断，
    却选择直接截断、只在事后「告知偏差」。用户要求这种时候停下来问。

    判据取自预览里**已经算好**的那条告警，不在这里重算：
    两处各算一遍一定会漂移，而漂移的表现就是「界面说有冲突、题面说的却是另一个数」。
    """
    if not preview:
        return None
    tl = preview.get("timeline") or {}
    plan_dur = tl.get("duration")
    if not plan_dur:
        return None
    hit = next((str(w) for w in (preview.get("warnings") or [])
                if "截断" in str(w)), None)
    if hit is None:
        return None
    m = re.search(r"到\s*([0-9.]+)\s*s", hit)
    if not m:
        return None
    try:
        need = float(m.group(1))
    except ValueError:
        return None
    if need <= float(plan_dur):
        return None
    # 向上取整到 5 秒，留一点余量（句子结尾通常还有一点静音）
    suggest = int(need // 5 * 5 + 5)
    return {"planned": float(plan_dur), "needed": need, "suggest": suggest,
            "warning": hit}


def preview_summary(preview: Mapping[str, Any] | None) -> dict[str, str]:
    """预览 → 弹窗顶部那排小标签（用户不用开侧边栏也能看个大概）。"""
    if not preview or not preview.get("ready"):
        return {}
    s = preview.get("summary") or {}
    tl = preview.get("timeline") or {}
    out: dict[str, str] = {}
    if tl.get("duration"):
        out["时长"] = _fmt(tl["duration"])
    if s.get("resolution"):
        out["画面"] = str(s["resolution"])
    if s.get("groups"):
        out["段落"] = f"{s['groups']} 段"
    if s.get("clips"):
        out["镜头"] = f"{s['clips']} 个"
    audio = preview.get("audio") or {}
    if audio.get("original_audio"):
        out["声音"] = "保留原声"
    elif audio.get("voiceover_count"):
        out["声音"] = f"配音 {audio['voiceover_count']} 段"
    if audio.get("bgm"):
        out["配乐"] = str((audio["bgm"] or {}).get("filename") or "已选")[:18]
    if tl.get("subtitles"):
        out["字幕"] = f"{tl['subtitles']} 条"
    if tl.get("overlay_events"):
        out["覆盖画面"] = f"{tl['overlay_events']} 层"
    return out


#: 图形科普片在计划卡上的开关（名单与 ``open_storyline/motion/code_spec.py`` 的
#: ``KNOB_FIELDS`` 同源；两份名单漂移由 tests/test_code_spec.py 拦下）。
#: 为什么不在这里 import 那个模块：主服务不装配剪辑服务端的实现，而这里读错的后果
#: 只是摘要少显示一个字段——它不参与任何执行判定。
MOTION_KNOBS = ("aspect", "fps", "narration", "voice", "rate", "caption_style",
                "highlight_style",
                "palette", "texture", "transition", "transition_sec", "entrance", "exit")

#: 卡面要给人看的词，不是枚举字面量（用户不需要背 portrait / paper）。
#: 这三张表只是**拿不到契约 value_labels 时的兜底**，取值与措辞都必须和
#: ``code_spec.KNOB_VALUE_LABELS`` 逐字一致——不一致时确认卡会把同一个决定显示成
#: 两副面孔。契约在时走 ``props_for(节点名)``，这里不参与判定。
_ASPECT_LABELS = {"portrait": "竖屏", "landscape": "横屏", "square": "方屏"}
_CAPTION_STYLE_LABELS = {"on_picture": "贴画面·无底条", "outlined": "白字黑边",
                         "paper": "纸条底·画面下沿", "film_line": "整句电影感·下沿淡浮",
                         "big_center": "大字压屏·画面正中", "top_banner": "顶部横幅",
                         "none": "不上字幕"}
_HIGHLIGHT_STYLE_LABELS = {"color": "关键词换色", "karaoke": "逐词点亮",
                           "block": "底色块", "ink": "撕纸高亮",
                           "plain": "不强化·整句一个样"}
_TEXTURE_LABELS = {"none": "不加（原片直出）", "film": "老胶片·颗粒与暗角",
                   "tv": "老电视·扫描线与色偏", "glow": "柔光·高光处泛开",
                   "bleach": "漂白·硬对比低饱和"}
# 整片底色那一格（2026-10-17）。与 ``code_spec.KNOB_VALUE_LABELS["palette"]`` 逐字一致
# （那份是分镜契约侧、这里兜底用）——八套色板的中文名两边各写一遍是既有规矩：
# 主服务不装配剪辑服务端的实现，而漂了就是在两张卡上对同一档说两句话。
# `none` 的措辞与文字库面板的那一档同源（`code_spec.PALETTE_DEFAULT_LABEL`）。
_PALETTE_LABELS = {"none": "默认（片子的底色）",
                   "vintage-kraft": "复古牛皮纸", "deep-ocean": "深海蓝",
                   "obsidian-violet": "黑曜紫", "modern-light": "现代浅色",
                   "midnight": "午夜暗场", "solar-pop": "阳光波普",
                   "coral-burst": "珊瑚撞色", "color-play": "彩块拼接"}
# R7-T22 的四条同规矩。**进出场分成两张表**：同一个取值「rise」在入场里是
# 「自下浮入」、在出场里是「向上浮出」，共用一张表就会在确认卡上写出一句反话。
_TRANSITION_LABELS = {"none": "硬切（镜镜直接接）",
                      "fade": "淡叠·前一镜淡出后一镜淡入",
                      "fadeblack": "黑场·先暗到黑再从黑里亮起来",
                      "dissolve": "溶解·两边互相化开",
                      "wipeleft": "左擦·新画面从右往左推过来",
                      "wiperight": "右擦·新画面从左往右推过去",
                      "slideup": "上滑·新画面自下而上顶入",
                      "circleopen": "圆开·从中心扩开",
                      "circleclose": "圆收·旧画面向中心收拢",
                      "zoomin": "推进·画面放大着换过去",
                      "hblur": "横向拖影·画面被速度拉开",
                      "radial": "涟漪·圆形波纹从一点荡开",
                      "smoothleft": "柔推·缓动横移换过去"}
_TRANSITION_SEC_LABELS = {"0.2": "0.2 秒·一闪而过", "0.4": "0.4 秒·轻叠（默认）",
                          "0.6": "0.6 秒·看得出", "0.9": "0.9 秒·很缓"}
_ENTRANCE_LABELS = {"none": "直接开始（不动作）", "fade": "淡入",
                    "rise": "自下浮入", "drop": "自上落入",
                    "zoom": "轻推近·由小放大入画"}
_EXIT_LABELS = {"none": "直接结束（不动作）", "fade": "淡出",
                "rise": "向上浮出", "drop": "向下落出",
                "zoom": "轻拉远·缩小出画"}


def motion_summary(source: Mapping[str, Any] | None,
                   labels: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, str]:
    """图形科普片的「将要渲成什么样」——确认卡顶部那排小标签。

    ``source`` 认两种形状：``plan_motion`` 的产物（账已经算好，含 estimated_sec），
    以及模型跳过分镜、直接传给 ``render_motion_code`` 的手写 spec（这时只有结构事实，
    估算区间**不在这儿重算**——那是剪辑服务端的口径，抄一遍迟早漂移）。

    调用方（``agent._load_motion_plan``）已把这次调用带的顶层开关盖进 source，
    所以卡上勾了横屏就不会还写竖屏。

    ``labels`` 是契约带回来的「取值 → 人话」表（``{开关名: {取值: 标签}}``，
    源在剪辑服务端的参数声明）。为什么要传：确认卡回显的正是用户刚在计划卡上勾的那
    一项，计划卡上写「男声·普通话」而这里回显 ``zh-CN-YunjianNeural``，同一个决定在
    两张卡上换了一副面孔。拿不到契约时退回本模块那两张小表，再退回原值。
    """
    if not isinstance(source, Mapping) or not source:
        return {}

    def word(key: str, value: Any, fallback: Mapping[str, str] | None = None) -> str:
        text = str(value if value is not None else "")
        if not text:
            return ""
        table = (labels or {}).get(key) or {}
        return str(table.get(text) or (fallback or {}).get(text) or text)

    src = dict(source)
    if isinstance(src.get("narration"), str) \
            and src["narration"].strip().lower() in ("true", "false"):
        src["narration"] = src["narration"].strip().lower() == "true"

    shots = src.get("shots") if isinstance(src.get("shots"), list) else None
    out: dict[str, str] = {}
    shot_count = src.get("shot_count") if src.get("shot_count") else (
        len(shots) if shots is not None else 0)
    if shot_count:
        out["镜头"] = f"{shot_count} 个"
    if str(src.get("chain") or "") == "remotion":
        # 这一行不再用来区分两条链（旧的那条已下线），它给用户的是「这次按下去烧的是
        # 谁的算力、这一版能不能按字段改」：画面出自渲染容器里的代码，局部改能改代码与字段。
        out["通道"] = "代码镜（渲染容器逐帧渲）"
    chars = src.get("char_count") or (
        sum(len(str(s.get("text") or "")) for s in shots if isinstance(s, Mapping))
        if shots else 0)
    if chars:
        out["文案"] = f"{chars} 字"
    est = src.get("estimated_sec")
    if isinstance(est, (list, tuple)) and len(est) == 2:
        try:
            out["预计时长"] = f"约 {float(est[0]):g}~{float(est[1]):g}s"
        except (TypeError, ValueError):
            pass
    res = str(src.get("resolution") or "")
    aspect = word("aspect", src.get("aspect"), _ASPECT_LABELS)
    if res or aspect:
        out["画面"] = " ".join(x for x in (res, aspect) if x)
    if src.get("fps"):
        fps = str(src["fps"])
        table = (labels or {}).get("fps") or {}
        out["帧率"] = str(table.get(fps) or f"{fps} fps")
    if "narration" in src:
        if src["narration"]:
            voice = word("voice", src.get("voice"))
            rate = word("rate", src.get("rate"))
            out["声音"] = "旁白 " + " ".join(x for x in (voice, rate) if x)
        else:
            out["声音"] = "无旁白（按阅读节奏驻留）"
    sub = word("caption_style", src.get("caption_style"), _CAPTION_STYLE_LABELS)
    if sub:
        hl = word("highlight_style", src.get("highlight_style"), _HIGHLIGHT_STYLE_LABELS)
        out["字幕"] = "，".join(x for x in (sub, hl) if x)
    texture = word("texture", src.get("texture"), _TEXTURE_LABELS)
    # 逐镜点名（B6）：整片那一档只是**默认**，一镜能点一档、也能点名不要。确认卡不许把
    # 「整片不加、第 3 镜老电视」显示成「没加质感」——那是用户正要批准的一个观感决定。
    # 两种来路共用一条判据：分镜产物的 plan_table 行带的是**实际生效**的档，手写 spec 的
    # shots 行带的是**声明**的档（空串＝跟随，天然不等于任何档）。与整片不同才算换过。
    film_tex = str(src.get("texture") or "").strip().lower() or "none"
    rows = src.get("plan_table") if isinstance(src.get("plan_table"), list) else shots
    n_override = len([r for r in (rows or []) if isinstance(r, Mapping)
                      and (k := str(r.get("texture") or "").strip().lower())
                      and k != film_tex])
    if texture and film_tex != "none":
        out["质感（整片默认）"] = texture + (
            f"，{n_override} 镜点名换过档" if n_override else "")
    elif n_override:
        out["质感（逐镜点名）"] = f"整片不加，{n_override} 镜自己点了档"
    # 整片底色那一格（2026-10-17）。只在**真选了一套**时才出一行：「默认（片子的底色）」
    # 是缺省，每张确认卡上都写一句只会把真正要批的那几行淹掉（与「硬切」那一行同理）。
    # 逐镜覆盖数**不数**：色板没有逐镜的那一栏（它是写在那一镜代码里的），
    # 数不出来就不写一个估的数——「几镜换过档」在质感那一行是真的，这里不是。
    palette = str(src.get("palette") or "").strip().lower()
    if palette and palette != "none":
        out["底色"] = word("palette", palette, _PALETTE_LABELS)
    # 模型从用户的话里读出来的整片风格（一句话，2026-10-17）。风格由用户的要求定、不是
    # 枚举，所以这里原样回显——确认卡是用户在烧像素之前唯一能看出「它理解歪了」的地方。
    style = str(src.get("style") or "").strip()
    if style:
        out["风格"] = style[:80] + ("…" if len(style) > 80 else "")
    # R7-T22：接缝与进出场。只在**不是硬切/不动**时才出一行——「硬切」是默认，
    # 每张确认卡上都写一句「镜镜直接接」只会把真正要批的那几行淹掉。
    trans_style = str(src.get("transition") or "none").strip().lower()
    if trans_style and trans_style != "none":
        trans = word("transition", trans_style, _TRANSITION_LABELS)
        sec = word("transition_sec", src.get("transition_sec"),
                   _TRANSITION_SEC_LABELS)
        n_gaps = max(0, (shot_count or len(shots or [])) - 1)
        out["接缝"] = "；".join(x for x in (
            trans, f"每处叠 {sec}" if sec else "",
            f"共 {n_gaps} 处" if n_gaps else "") if x)
    enters = [(key, label) for key, label in (("entrance", _ENTRANCE_LABELS),
                                              ("exit", _EXIT_LABELS))
              if str(src.get(key) or "none").strip().lower() not in ("", "none")]
    if enters:
        out["每镜进出"] = " / ".join(
            f"{'入' if key == 'entrance' else '出'}："
            f"{word(key, src.get(key), label)}" for key, label in enters)
    bgm = src.get("bgm") if isinstance(src.get("bgm"), Mapping) else {}
    ref = str(bgm.get("ref") or "") if bgm else ""
    # 去掉扩展名再截：`DJariium, DJAQ - We Never (高燃鼓點版).mp3` 从第 18 个字截，
    # 用户正好读到「DJariium, DJAQ - W」——曲名那一截全被截掉了，按确认前核对不出来。
    name = Path(ref).name if ref else ""
    name = name.rsplit(".", 1)[0] if "." in name else name
    out["配乐"] = name[:24] if name else "纯人声（无配乐）"
    return out


#: 确认卡上逐镜列出的最大镜数与每镜字数：卡是给人扫一眼的，不是分镜脚本全文。
STORYBOARD_MAX_SHOTS = 40
STORYBOARD_TEXT_CHARS = 40


def motion_storyboard(source: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """渲染前计划卡的逐镜清单：镜号 + 这一镜说的话 + 一句画法备注。

    认两种来路（与 ``motion_summary`` 同）：分镜产物的 ``plan_table``（已截好的预览文字、
    读出来的画法通道），或手写 spec 的 ``shots``。拿不到就空表——卡上少一块，门照样拦。
    """
    if not isinstance(source, Mapping):
        return []
    rows = source.get("plan_table") if isinstance(source.get("plan_table"), list)         else source.get("shots")
    thumbs = source.get("preview_frames") if isinstance(source.get("preview_frames"),
                                                        Mapping) else {}
    out: list[dict[str, str]] = []
    for r in (rows or [])[:STORYBOARD_MAX_SHOTS]:
        if not isinstance(r, Mapping):
            continue
        text = str(r.get("text") or "")
        if len(text) > STORYBOARD_TEXT_CHARS:
            text = text[:STORYBOARD_TEXT_CHARS] + "…"
        note = "·".join(str(x) for x in (r.get("card") or r.get("subject"), r.get("style"))
                        if x)
        sid = str(r.get("id") or len(out) + 1)
        row = {"id": sid, "text": text, "note": note}
        if thumbs.get(sid):
            # 对象键；调用方（agent）现签成可直接展示的 URL 写进 thumb。
            row["thumb_key"] = str(thumbs[sid])
        out.append(row)
    return out


def build_render_ask(preview: Mapping[str, Any] | None, *,
                     motion: Mapping[str, Any] | None = None,
                     title: str = "",
                     labels: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """把编排预览变成一道确认题。

    普通情况两个选项就够：确认渲 / 我还要改。但**如果发现时长会把话截断**，
    就把它升级成一道专门的题（保完整 / 按原时长截断 / 自己指定秒数）——
    那时用户要决定的不是「渲不渲」，而是「宁可长一点还是宁可短一点」。
    这正是用户举的那个例子：模型不该替用户决定把话截掉。
    """
    warnings = list((preview or {}).get("warnings") or [])
    if motion:
        # 分镜阶段算出的「比目标长/短不少」是这条通道唯一能提前给出的偏差——
        # 它必须出现在题面上：删镜还是加速，是用户在**开渲之前**该做的决定。
        warnings += [str(w) for w in (motion.get("target_warnings") or [])]
        # 分镜阶段那些**只提醒**的条目（文案具体项、色板/卡族/原创占比这些多样性建议）
        # 也上这一张卡：它们故意不做硬闸（词法认不出专名、审美选择不该被机器判死，
        # 误伤的代价是一整轮重规划），而「不打回」不等于「用户看不见」——开渲之前是
        # 唯一还能一句「这几镜太空，重写」的时机，出了片再改就是重烧。
        # 闸⑦「每镜必画」不在这里：它是硬闸，没画画的镜根本走不到确认卡这一步。
        warnings += [str(w) for w in (motion.get("style_warnings") or [])]
        # 预览没出成也分两种：机器忙的那类不影响出片，**代码本身编译不过**的那类
        # 一定会在那一镜停下（同一份代码、同一个打包器）。后者已经自动重画过一轮，
        # 还停在这里就是「等你决定」：卡上必须写清，别让用户点完继续才发现。
        pf = motion.get("preview_failures")
        if isinstance(pf, Mapping):
            stuck = sorted(str(k) for k, v in pf.items() if str(v) == "final")
            if stuck:
                warnings.append(
                    f"{'、'.join(stuck)} 这一镜的画面没能生成：继续渲染会在这里停下。"
                    "想先看到这一镜，就选「我还要改」，说一下这一镜要画成什么样。")
            elif pf and not (motion.get("preview_frames") or {}):
                # 一类是「机器忙」的瞬时失败：不影响出片，但卡面上一张图都没有时，
                # 用户看到的是「这一版没做预览」——而那会让他以为预览坏了或自己漏选了什么。
                # 真机 2026-10-21 run `1ff6f17ee840`：渲染服务渲第一镜时崩了，随后
                # 42 镜全部 Connection refused，卡上却只有质感/结构两条提醒。
                # 只报不拦：写清「图没出来、不影响出片、想先看就重出一版」。
                warnings.append(
                    f"这一版的预览图没有生成（渲染服务中途中断过一次）："
                    f"{len(pf)} 镜都没出图，不影响出片。想先看到画面，就选「我还要改」，"
                    "说一句「内容不动，先把这一版的预览图补齐再给我确认」。")
    conflict = duration_conflict(preview)

    if conflict:
        planned, suggest = conflict["planned"], conflict["suggest"]
        head = (f"按现在的编排，成片 {planned:.0f} 秒，但内容到 "
                f"{conflict['needed']:.0f} 秒——最后的话会被截断。怎么办？")
        ask: dict[str, Any] = {
            "title": head,
            "options": [
                {"key": KEEP_FULL_OPTION,
                 "label": f"保内容完整，时长放宽到约 {suggest} 秒",
                 "description": "不截断句子；画面按内容补足，成片会长一点",
                 "recommended": True},
                {"key": TRUNCATE_OPTION,
                 "label": f"就按 {planned:.0f} 秒，末尾截断",
                 "description": "保持原定时长，接受最后一句被截断"},
            ],
            "allow_custom": True,
            "custom_hint": "其他（直接写你要多少秒，例如「100 秒」）",
            "warnings": warnings,
            "conflict": conflict,
        }
    else:
        head = title or "这是即将渲染的编排结果，确认开始渲染吗？"
        if warnings:
            # 有偏差时把偏差顶到问题里：那正是用户最需要据此决定的地方
            head = f"{head}（有 {len(warnings)} 处需要你留意）"
        ask = {
            "title": head,
            "options": [
                {"key": CONFIRM_OPTION, "label": "就这样，开始渲染",
                 "description": "按上面的编排渲染成片",
                 "recommended": not warnings},
                {"key": ADJUST_OPTION, "label": "我还要改",
                 "description": "先不渲染；告诉我要改什么，我重做一版编排再给你确认",
                 "recommended": bool(warnings)},
            ],
            "allow_custom": True,
            "custom_hint": ("其他（直接写要改什么，例如「改成横屏」「换个女声」「压到 60 秒以内」）"
                            if motion else
                            "其他（直接写要改什么，例如「出镜比例调到 30%」）"),
        }
        if warnings:
            ask["warnings"] = warnings

    summary = preview_summary(preview) or motion_summary(motion, labels)
    if summary:
        ask["preview"] = summary
    board = motion_storyboard(motion) if motion else []
    if board:
        ask["storyboard"] = board
    return ask


def decision_is_confirm(decision: str) -> bool:
    """用户的心意是不是「原样渲染」。

    认不出来的 decision 一律当「不确认」——渲染是不可逆的贵操作，
    宁可多问一次也不要在没看清选择时直接开渲。
    注意时长冲突那两个选项都**不**算「原样渲」：选保完整要先放宽时长，
    选截断要按原时长重做，两者都得让模型再动一次参数，所以都返回 False
    （接下来做什么由 ``decision_text`` 说清）。
    """
    return str(decision or "").strip() == CONFIRM_OPTION


def decision_text(decision: str, custom: str = "") -> str:
    """把用户的选择变成一句给模型看的话（进 messages，作为这一轮的答复）。"""
    d = str(decision or "").strip()
    if custom.strip():
        return (f"用户对编排结果提出修改：{custom.strip()}\n"
                f"请不要渲染，先按这个要求重做编排（没说到的地方保持原样），"
                f"做完再发起渲染——系统会再弹一次计划确认卡给用户过目。")
    if decision_is_confirm(d):
        return "用户已确认编排结果，可以开始渲染。"
    if d == ADJUST_OPTION:
        return ("用户选择「我还要改」，尚未确认渲染。请不要渲染；"
                "用户没写具体要改什么，就用 ask_user 问一句要改哪方面"
                "（给几个常见方向：文案、画面风格、时长、配音、字幕），拿到答复再重做编排。")
    if d == KEEP_FULL_OPTION:
        return ("用户要求**保内容完整**：不要把话截断。请把成片时长放宽到能容纳全部内容"
                "（约到那条内容的结束时间），重做时间线让画面补足，"
                "然后把新的编排结果再交用户确认，确认后才渲染。")
    if d == TRUNCATE_OPTION:
        return ("用户接受按原定时长截断末尾。请按原时长重做/确认时间线；"
                "注意成片末尾会少一句话——这件事用户已经拍板，渲染前不必再问。")
    return f"用户对编排结果的选择是「{d or '未选择'}」，未确认渲染，不要开始渲染。"
