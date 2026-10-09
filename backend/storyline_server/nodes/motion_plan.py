"""零素材图形科普片的**分镜**节点：把模型写的 spec 校验、归一、落进 Store，并回一份账。

为什么单有一个节点（而不是让模型直接调 render_motion_video）：

* **校验要有落点**：出片只认 Store 里那一份 ``motion_spec``。没有这一步，spec 要么每次
  调用重传一遍（模型容易漏、两处版本会漂），要么偷偷在出片节点里现场校验——而那时候
  已经站在烧像素的门口了。
* **账要在烧像素之前给人看**：每镜几个字、估算多少秒、离目标差多远，这些决定「删镜还是
  加镜」，属于分镜阶段的事，不该由一次十分钟的渲染来揭晓。
* **改文案不该重新渲一次片**：分镜与出片是两次决定，各自一个工具，才谈得上「只改第 3 镜
  再渲一次」。

口径与 ``motion/spec.py`` 一致：**镜头时长不由模型给**——有旁白时时钟归 TTS 实测，
模型写的 ``duration_sec`` 在这里就降级成 ``min_duration_sec``（下限参考）。
"""

from __future__ import annotations

from typing import Any, Mapping

from agent_framework.orchestration import NodeState
from agent_framework.retrieval import (cite_fields, claims_verified, ledger,
                                       matches_ledger)

from ..motion import spec as mspec
from .core_nodes import StoryNode, _obj, _resolve_target_duration

#: 分镜卡上每镜文案最多显示这么多字——界面不是文稿编辑器，全文在 motion_spec 里。
PLAN_TEXT_PREVIEW_CHARS = 24


async def _source_errors(session_id: str, shots: list[dict[str, Any]]) -> list[str]:
    """出处闸：声称「查证过」的 source，必须能对上一笔真检索。

    为什么需要它（真机事故 2026-10-08）：检索被人机验证挡在门外之后，模型照样写完了
    分镜，每镜 ``source`` 填的是它自己的记忆。画面与逐镜账如实转录了这些出处，观众读到
    的却是「像查过的一样」的猜测——「机器不判真伪、只如实转录」这条合同被架空了。
    闸不判史实（判不了），只判**这一笔在本会话里发生过吗**。

    账本不可用（没绑存储）时整体放行：没地方记命中，就拿「没记过」定罪会退化成
    「所有出处一律打回」，那是另一种假。
    """
    rows = await ledger(session_id)
    if rows is None:
        return []
    errors: list[str] = []
    for shot in shots:
        source = shot.get("source") or {}
        if not claims_verified(source) or matches_ledger(source, rows):
            continue
        title, url = cite_fields(source)
        errors.append(
            f"{shot['id']}：出处「{title or url}」写成了查证过的样子，但本会话的检索账里"
            f"没有这一笔（web_search / fetch_url 都没真打开过它"
            + ("，一次检索都没成功过" if not rows else "")
            + "）。要么先去查，要么改成「未核实：<依据一句话>」"
            "——模型记忆不许冒充查证过的出处")
    return errors


class PlanMotionNode(StoryNode):
    name = "plan_motion"
    display_name = "图形科普片分镜"
    description = (
        "零素材出片的第一站：把分镜 spec（每镜一句文案 + 版式组件 + 高亮词 + 出处）"
        "校验、归一并存成本会话的 motion_spec，同时回一份「将要渲成什么样」的账"
        "（镜头数、总字数、按字数估的秒数区间、与目标时长的偏差提醒）。"
        "本节点**不渲染、不烧像素**：改文案只需重调本工具，确认无误再调 render_motion_video。"
        f"版式组件可用：{', '.join(mspec.CARD_KINDS)}；比例："
        f"{', '.join(mspec.ASPECTS)}；帧率白名单：{mspec.FPS_CHOICES}；"
        f"字幕形态：{', '.join(mspec.SUBTITLE_MODES)}。"
        "有旁白时不要写每镜 duration_sec——镜头长度由真实语音时长决定，写了只会被当下限。"
        f"画幅/帧率/旁白开关/音色/语速/字幕形态/质感档同时提供**顶层入参**（计划卡给用户勾选的位），"
        f"质感可选：{', '.join(mspec.TEXTURES)}（挂在母带一层，换它不重烧镜头）。"
        "传了会覆盖 spec 里的同名字段。")
    required_nodes: list[str] = []
    require_explicit_call = True
    input_schema = _obj("图形科普片分镜", {
        "spec": {
            **mspec.spec_schema(),
            "description": "分镜 spec：每镜一句文案 + 版式组件 + 高亮词 + 出处（逐字段含义见结构）。"
                           "本节点是**校验闸**：card 不在清单里、高亮词不是文案子串、"
                           "文案超出字幕容量都会带镜号退回，改完重调即可，不烧像素。"
                           "source 请如实填出处——机器不判真伪，它会如实出现在分镜卡与逐镜账上"
                           "（版式不自动排它，要进画面得写进所选 card 的 visual 字段）。"
                           "**出处还要过一道账**：本会话被 web_search / fetch_url 真打开过的页面"
                           "才算查证过，对不上账的出处会被打回（没查到就写「未核实：…」）。"
                           "aspect/fps/narration/voice/rate/subtitle_mode/texture 也可以走下面的"
                           "顶层开关，"
                           "顶层的优先（那是用户在计划卡上勾的，不是建议）",
        },
        **mspec.knob_props(),
        "target_duration_sec": {
            "type": "number",
            "description": "用户要的目标秒数。只出提醒（估算偏离过大时建议删镜/加镜），"
                           "本节点不会因此拒绝",
        },
    }, required=["spec"])

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        raw = inputs.get("spec")
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                "plan_motion：spec 必须是对象且非空——先把每镜文案与版式写进来")
        # 顶层开关先并进来再校验：卡上勾了 landscape 就得渲成横屏，模型在 spec 里写的
        # portrait 让位；并早并晚都要过 validate_spec，所以合在闸门前，越界照样回清单。
        spec, errors = mspec.validate_spec(mspec.overlay_knobs(dict(raw), inputs))
        if errors:
            # 带病入库比报错更贵：render_motion_video 读的就是这一份，
            # 于是错误会在**烧完像素之后**才现形。
            raise ValueError(
                "分镜校验未通过，没有入库（照清单改完再调本工具）：\n"
                + "\n".join(f"  · {e}" for e in errors[:12])
                + (f"\n  · …另有 {len(errors) - 12} 条同类错误" if len(errors) > 12 else ""))

        target = _resolve_target_duration(state, inputs, None)
        cited = await _source_errors(state.session_id, spec["shots"])
        if cited:
            # 校验过的分镜仍可能带着「像查过的一样」的猜测出处入库；一旦出了片，
            # 那些字面就在成片与逐镜账里替模型背了书。拦在入库前。
            raise ValueError(
                "出处闸未通过，分镜没有入库（本会话真打开过的页面才算出处）：\n"
                + "\n".join(f"  · {e}" for e in cited[:12])
                + (f"\n  · …另有 {len(cited) - 12} 条同类错误" if len(cited) > 12 else ""))
        return plan_payload(spec, target=target)


def plan_payload(spec: dict[str, Any], *, target: float | None = None,
                 next_hint: str = "") -> dict[str, Any]:
    """分镜产物的形状（归一后的 spec + 那份要给人看的账）——**单源**。

    为什么抽出来：``patch_motion_video`` 把一版局部改写回 ``plan_motion`` 时，必须写
    成与这里**一模一样**的形状（``render_motion_video`` 与确认卡读的都是这几栏）。
    两处各抄一遍，迟早出现「局部改之后卡面上 shot_count 还是旧数字」这种谎。
    """
    low, high = mspec.estimate_sec(spec)
    shots = spec["shots"]
    chars = sum(len(str(s["text"])) for s in shots)

    def _row(s: dict[str, Any]) -> dict[str, Any]:
        # 出处用闸同一把钥匙读：契约写的是「标题/链接」，只认英文键的读法会让
        # 一条过了闸的中文出处在逐镜账上显示成空白。
        cite_title, cite_url = cite_fields(s["source"])
        text = s["text"]
        return {
            "id": s["id"], "card": s["card"],
            "text": (text[:PLAN_TEXT_PREVIEW_CHARS] + "…")
                    if len(text) > PLAN_TEXT_PREVIEW_CHARS else text,
            "chars": len(text),
            "highlight": s["highlight"],
            "source": cite_title or cite_url,
            "min_sec": s["min_duration_sec"],
        }

    return {
        "motion_spec": spec,
        "shot_count": len(shots),
        "char_count": chars,
        "estimated_sec": [low, high],
        "target_duration_sec": target,
        "target_warnings": mspec.check_target(spec, target),
        "aspect": spec["aspect"],
        "resolution": f"{spec['width']}x{spec['height']}",
        "fps": spec["fps"],
        "narration": spec["narration"],
        "voice": spec["voice"] if spec["narration"] else "",
        "rate": spec["rate"] if spec["narration"] else "",
        "subtitle_mode": spec["subtitle_mode"],
        "texture": spec.get("texture") or "none",
        "bgm": spec["bgm"],
        "title": spec["title"],
        "plan_table": [_row(s) for s in shots],
        "next": next_hint or ("确认这份分镜（或按用户要求改完）之后调 render_motion_video 出片；"
                              "它会先把编排结果交用户确认，不会静默开渲"),
    }
