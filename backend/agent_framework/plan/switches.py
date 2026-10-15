"""**一个开关上的取值**怎么判、它的毛病怎么措辞——从 ``validate._check_params`` 切出来。

切出来的理由是这两件事都不是「步骤」级的判据，而是「取值」级的：它们拿的是
``(source, 被拒的值, 选中的候选, user_says)``，不认识步骤、节点、tag，也不问词表。
留在 validate.py 里那个文件就长回 400 行以上的样子（这条有护栏钉着，见
``tests/test_plan_layout.py`` ①）。

措辞里的两条真机教训原样搬过来，别当注释删：
- 一个开关上的几处毛病**合成一条**（分几行报＝挤占别的判据的清单窗口）；
- **用户已指名要的那一项不算「没有选择余地」**（14 镜那条 run 死在这上面）。
"""

from __future__ import annotations

from typing import Any

from .support import num

__all__ = ["USER_SAYS_MAX", "switch_problems", "value_rejected"]

# ``user_says``（「这一项是用户在消息里指名要的」）的长度上限。卡上要原样显示这句话让
# 用户核对，所以它得是**原话的一两个词**（「横屏」「15 帧」「we never」），不是整句转述。
USER_SAYS_MAX = 24


def value_rejected(source: tuple[str, Any], text: str) -> str:
    """取值能不能从它声明的枚举源里反查回来；能则返回空串，不能则返回**原因**。"""
    kind, data = source
    if kind == "number":
        value = num(text)
        if value is None:
            return "不是数值"
        low, high = data
        if low is not None and value < low:
            return f"低于节点下界 {low:g}"
        if high is not None and value > high:
            return f"超过节点上界 {high:g}"
        return ""
    if text in data:
        return ""
    if kind == "label":
        # 曲库标签这一档的「身份」从来不是整串标题：执行层 ``search_media`` 按 2-gram
        # 命中文件名（``run_server.bgm_enum_source`` 的注释自己写着「带不带后缀都搜得到」）。
        # 计划门比执行层还严，就会出现「卡上过不了、真去取却取得到」——真机那条
        # ``DJariium, DJAQ - We Never (高燃鼓點版)`` 死于模型漏抄了艺人名前缀。
        # 放宽到**唯一命中**：写全名、写后半截、少写后缀都行；一句「邓紫棋」同时
        # 命中两首仍然打回——防臆造别名靠的是「命中不了」，不是「必须逐字」。
        probe = _fold(text)
        hits = sorted(c for c in data if isinstance(c, str) and probe
                      and (probe in _fold(c) or _fold(c) in probe))
        if len(hits) == 1:
            return ""
        if len(hits) > 1:
            return ("反查不到枚举源：「%s」同时命中 %d 条候选（%s），"
                    "请写得更具体到只剩一条" % (
                        text, len(hits), "｜".join(f"「{h}」" for h in hits)))
    # 候选自身可能含 ", "（曲库文件名 "DJariium, DJAQ - We Never (高燃鼓點版)"），
    # 裸拼会让**一条**读成**两条**——真机模型据此认定「我写的明明在可用清单里」，
    # 于是判定闸在抽风，回头又问用户一遍。逐条加「」并用全角竖线分隔。
    joined = "｜".join(f"「{t}」" for t in sorted(data))
    for cand in sorted(data):
        if not (isinstance(cand, str) and text and text != cand):
            continue
        if cand.endswith(text):
            return (f"反查不到枚举源：你写的「{text}」是候选「{cand}」的后半截，"
                    f"缺前缀「{cand[: -len(text)]}」——这一档要整串逐字一致")
        if cand.startswith(text):
            return (f"反查不到枚举源：你写的「{text}」是候选「{cand}」的前半截，"
                    f"缺后缀「{cand[len(text):]}」——这一档要整串逐字一致")
    return f"反查不到枚举源（可用：{joined[:160] or '无'}）"


def _fold(s: str) -> str:
    """比对用的归一形：只去首尾空白与统一大小写，不碰标点（标点参与子串判定）。"""
    return s.strip().casefold()


def switch_problems(bad: list[tuple[str, str]],
                    values: list[dict[str, str]],
                    source: tuple[str, Any], *,
                    user_says: str = "") -> list[str]:
    """同一个开关上的毛病 → 一串独立的分句（调用方决定怎么串成一句话）。

    四处容易漏的：
    - **每个被拒的值都要点名**（连原因）。原先一个开关列 5 个都不合法就是 5 条
      几乎一样的话、每条还各带一遍可用清单——它们占的是**别的**判据的位置。
    - 值被拒光时不再补一句「没得选」：那是同一个毛病的回声，判据是「这些取值
      不合规」，不是「候选太少」。
    - 只剩一个候选（数值区间/布尔/枚举这三类取值范围本来就存在的）才提「没有
      选择余地」；`label`（曲库标签）给一个是对的，那是用户指名要的那一首。
    - **用户已经指名要的那一项不算「没有选择余地」**（``user_says`` 是他那句原话）：
      这一项的取值本来就是用户定的，卡上显示成「已定」让他核对，再凑一个他没提过
      的候选反而是瞎编。真机那条 14 镜的 run 就是死在这一条上（原因见调用方
      ``validate._check_params`` 里那段注释）。
    """
    out: list[str] = []
    if bad:
        if len(bad) == 1:
            text, reason = bad[0]
            out.append(f"的值「{text}」{reason}")
        else:
            pairs = "；".join(f"值「{text}」{reason}" for text, reason in bad)
            out.append(f"有 {len(bad)} 个值反查不过：{pairs}")
    if (values and len(values) < 2 and source[0] in ("number", "bool", "enum")
            and not user_says):
        hint = ""
        if source[0] == "number":
            low, high = source[1]
            hint = (f"该参数是数值区间 "
                    f"{'?' if low is None else f'{low:g}'}~"
                    f"{'?' if high is None else f'{high:g}'}，"
                    f"请给 2~4 个有真实差异的候选（如 0.1/0.2/0.3/0.5）")
        elif source[0] == "bool":
            hint = "给 true 与 false 两个候选，让用户自己选"
        else:
            hint = "请把该参数可用的取值多列几个，让用户有得挑"
        # 出路写两条，各自说清代价：**从卡上删掉不等于「按用户的取值跑」**——
        # compile 只把卡上列出的键传给节点，删了就按节点自己的默认值跑
        # （aspect 的默认是 portrait 竖屏，正好与用户要的相反）。
        out.append(f"只给了 1 个候选值「{values[0]['value']}」，用户没有选择余地。"
                   f"{hint}；如果这一个值就是用户消息里指名要的，把用户的原话写进"
                   f"这一项的 user_says——卡上会显示成「已定」让用户核对，也随时能改；"
                   f"如果它是你替用户挑的、又不打算让他改，就别把它做成开关"
                   f"（但**别从卡上删掉一个用户点名要的取值**：删了它就传不到执行，"
                   f"节点会按自己的默认值跑）")
    return out
