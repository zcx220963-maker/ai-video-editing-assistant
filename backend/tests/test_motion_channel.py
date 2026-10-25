# -*- coding: utf-8 -*-
"""图形科普片这条通道：计划卡开关、分镜账、渲染确认门必须**同源且不串通道**。

前半（①~⑨）钉的是**与链无关**的那批约定，后半（⑩~⑮）钉的是「旧链删掉之后，
谁替它保管那份保证」。两份的差别值得说清楚：

① 名单同源。渲染确认门（``agent_framework``）和分镜契约（``storyline_server``）
   各自持有一份开关名单，跨包 import 会让主服务装配剪辑实现，所以只能靠约定。
   约定会漂——漂了的表现是「卡上有十一个开关、服务端只认十个」，用户勾的那项静默失效。
② 开关必须在**顶层**。计划卡的参数是从 ``input_schema.properties`` 反查出来的
   （``plan/vocab.py::props_for``），埋在 ``spec`` 里面的字段卡片永远看不见；
   十一个开关因此同时声明为顶层入参，并在校验前盖进 spec。
③ 用户勾的选项覆盖模型写的 spec，且**覆盖之后仍要过校验**。
   否则「横屏」这个勾只是界面装饰。
④ 确认卡摘要认两种来源（分镜产物 / 手写 spec），通道名走单源词表。
⑤ 一批里出现两条出片通道时，确认一条不等于批准另一条。
⑥ 图形科普片单独走门：题面用分镜账，不借时间线预览。
⑦ 技能正文点名的取值与产出字段都要有真实来源，否则启动期一致性检查挂假告警。
⑧ 十一个开关真的走到计划卡：MCP 声明只带类型，枚举靠 DAG 契约带回来，
   再由 ``knob_facts`` 写进规划轮提示、由 ``PlanValidator._check_params`` 反查放行。
⑨ 契约带回的「取值 → 人话」标签要一路走到确认卡：门必须注入进 ``AgentOnceRun``，
   挂在 ``Agent`` 上的 ``plan_gate`` 对 Runner 而言是一次空读——不报错，只是卡面
   悄悄退回 ``zh-CN-YunjianNeural`` 这种机器名。

⑩~⑮ 是**旧声明式链下线时失去唯一出处的那六件事**，各自为什么留在这里：

⑩ 出处对账的**判定与降级**归 ``tests/test_code_gates.py``（那是闸的本体），这里只补它
   没覆盖的凭据语义：没身份不记账、只有标题也算、换会话查不到、inline 那一路降级在
   开任务行之前、字符串 source 直接丢。判据与闸同体，测试拆两处会漂移。
⑪ 局部改「读哪一版」：``_bundle`` / ``_base_spec`` / ``_fork`` 住在与链无关的基类里，
   注册出来的工具只有一个会走到它们，别处没有第二个调用点。
⑫ 命中表与代表帧的发布（``_publish_hitmap`` / ``_publish_frames``）同上；表条目形状
   用真实生产者 ``code_render._shot_table`` 现做，不手抄——手抄的那份会随生产者漂移。
⑬ 按镜切片缓存的**对象存储实现**（``_ObjectShotCache``）与质感滤镜
   （``render.apply_texture``——代码链按每一镜的切片各跑一趟，不在母带上）。缓存键的算法归 ``tests/test_code_spec.py`` 按字节断言，
   这里管的是收发协议：缺一个文件即整体未命中、写缺文件当场抛。
⑭ JSON 指针工具（``hitmap``）：``apply_patch`` 只被代码链的 ``code_patch`` 用在
   code/text/source 三条上，字段那条按 name 寻址不走它——两侧各认一半，得有人说清。
⑮ 词时间轴贴字符区间（``render.align_words``）：TTS 改读、漏词、尾部标点这三类塌法
   都只在这一个函数里收口。

跑法：容器内 ``PYTHONPATH=/app python tests/test_motion_channel.py``（离线，不起渲染容器）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import sys as _s  # noqa: E402
from unittest import mock  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework import render_gate  # noqa: E402
from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.catalog import ToolCatalog, get_catalog, set_catalog  # noqa: E402
from agent_framework.checkpoint import CheckpointManager  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import LLMResponse, ScriptedLLM  # noqa: E402
from agent_framework.messages import ToolCall  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402
from storyline_server.motion import code_patch as mcode  # noqa: E402
from storyline_server.motion import code_render as m_code  # noqa: E402
from storyline_server.motion import remotion_client as m_client  # noqa: E402
from storyline_server.motion import code_spec as cspec  # noqa: E402
from storyline_server.motion import hitmap as mhit  # noqa: E402
from storyline_server.motion import render as rpipe  # noqa: E402
from storyline_server.nodes import motion_nodes as m_nodes  # noqa: E402
from storyline_server.nodes.motion_code_nodes import (  # noqa: E402
    PatchCodeMotionVideoNode, RenderCodeMotionVideoNode)
from storyline_server.nodes.motion_plan import (PlanMotionNode,  # noqa: E402
                                                plan_payload)
from storyline_server.providers import Providers  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

WORK = Path("/tmp") / "motion_channel_work"

#: 一段「闸① 之外全都认得」的镜头代码。这一份离线用例**不送容器**（渲染层归
#: ``tests/test_code_gates.py`` 的假服务），它的职责只有两个：非空、是字符串，
#: 于是契约不会因为「这一镜没交代码」而退回到别的原因上去；以及过 P1 注入契约
#: （2026-10-17 起）——读 ``getInputProps``、用到 ``text``、摆 ``<CaptionLayer/>``，
#: 三条各堵住一个「spec 过不了校验」的岔路，让用例的红只可能来自它自己要判的那件事。
_CODE = ('import { FilmLayout, CaptionLayer } from "@dg/film";\n'
         'export default function S() { const { text } = getInputProps(); '
         'return <FilmLayout><rect />{text}<CaptionLayer /></FilmLayout>; }')

# 生产装配里这份名单走 Storyline 节点 display_name → MCP title → 主服务词表；
# 隔离用例没有 MCP 通道，按同一条声明自行灌入，否则题面只能退回机器名。
set_catalog(ToolCatalog({n.name: n.display_name
                         for n in (PlanMotionNode, RenderCodeMotionVideoNode)}))

_fails = 0
_checks = 0


def _module_defined(*mods) -> set[str]:
    """技能正文第三种合法来源：**在这个模块里定义的**函数与类（闸表会点它们的名）。

    按 `__module__` 归属判定而不是 `dir()` 全量——`dir()` 会把模块 import 进来的
    别人的名字也算成"本机认识"，那等于给臆造名字开了一张越滚越大的白名单。
    名字在这里读不到就报红，所以真改了函数名，文档还是会被抓住。
    """
    return {n for mod in mods for n, obj in vars(mod).items()
            if not n.startswith("_")
            and getattr(obj, "__module__", None) == mod.__name__}


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class _SilentTool(Tool):
    """只记录被调用了几次的假节点工具（真渲染要烧几分钟像素，离线用例不碰）。"""

    def __init__(self, node: str) -> None:
        self._node = node
        self.calls = 0

    @property
    def name(self) -> str:
        return self._node

    @property
    def description(self) -> str:
        return f"假 {self._node}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kw):
        self.calls += 1
        return '{"node": "%s", "render": {"status": "running"}}' % self._node


def _knobs(schema: dict) -> dict:
    return {k: v for k, v in (schema.get("properties") or {}).items()
            if k in cspec.KNOB_FIELDS}


async def main() -> int:
    print("=== ① 开关名单：门与契约同源，两个节点都把它摊在顶层 ===")
    check(tuple(render_gate.MOTION_KNOBS) == tuple(cspec.KNOB_FIELDS),
          f"MOTION_KNOBS == KNOB_FIELDS（门 {render_gate.MOTION_KNOBS} / "
          f"契约 {cspec.KNOB_FIELDS}）")
    check(render_gate.RENDER_NODES == {render_gate.RENDER_NODE,
                                       render_gate.MOTION_NODE,
                                       render_gate.WEB_NODE},
          f"出片通道一族认三个名字（图形链只剩代码镜一条，"
          f"实际 {sorted(render_gate.RENDER_NODES)}）")
    check(render_gate.MOTION_NODE == "render_motion_code"
          and render_gate.PATCH_NODES == frozenset({"patch_motion_code",
                                                    "patch_video"}),
          f"门点名的两个工具名与节点声明一致（出片 {render_gate.MOTION_NODE} / "
          f"局部改 {sorted(render_gate.PATCH_NODES)}）")

    want = cspec.knob_props()
    for node_cls in (PlanMotionNode, RenderCodeMotionVideoNode):
        got = _knobs(node_cls.input_schema)
        check(set(got) == set(cspec.KNOB_FIELDS),
              f"{node_cls.name} 的顶层入参含全部 {len(cspec.KNOB_FIELDS)} 个开关"
              f"（实际 {sorted(got)}）")
        for key, prop in want.items():
            exp = prop.get("enum")
            check(got.get(key, {}).get("enum") == exp,
                  f"{node_cls.name}.{key} 的枚举与契约一致（{exp}）")
    check(not ({"style", "card", "visual", "theme", "overlay"} & set(want)),
          "旧链那几栏（版式 / 卡片 / visual / theme / overlay）一个都不上卡——"
          "画面现在出自代码、版式出自母版，卡上留一格就是骗人去填一个没人读的字段")

    # 同一件事在两张卡上必须叫同一个名字：计划卡的标签来自分镜契约
    # （``cspec.KNOB_VALUE_LABELS``），确认卡的标签来自 ``render_gate``。
    # 两份名单跨包、不能 import，只能像开关名单一样靠约定 + 这条闸。
    for gate_map, knob in ((render_gate._ASPECT_LABELS, "aspect"),
                           (render_gate._CAPTION_STYLE_LABELS, "caption_style"),
                           (render_gate._HIGHLIGHT_STYLE_LABELS, "highlight_style"),
                           (render_gate._TEXTURE_LABELS, "texture"),
                           # 2026-10-17：整片色板。它最需要这一条——八套色板的中文名
                           # 在两张卡上是各写一遍的（跨包不能 import），
                           # 「牛皮纸」「复古牛皮纸」这种一字之差没有任何机器判据看得见。
                           (render_gate._PALETTE_LABELS, "palette"),
                           # R7-T22：转场与进出场四栏。这四条最需要这条闸——两个取值
                           # （entrance/exit 的 rise）在两张卡上是**反向**的措辞
                           # （「自下浮入」对「向上浮出」），抄错一边就会在确认卡上
                           # 写反方向，而那是纯文字，没有任何机器判据看得见。
                           (render_gate._TRANSITION_LABELS, "transition"),
                           (render_gate._TRANSITION_SEC_LABELS, "transition_sec"),
                           (render_gate._ENTRANCE_LABELS, "entrance"),
                           (render_gate._EXIT_LABELS, "exit")):
        labels = cspec.KNOB_VALUE_LABELS[knob]
        check(all(gate_map.get(v) == labels.get(v) for v in gate_map),
              f"确认卡与计划卡对 {knob} 的措辞一致"
              f"（{ {k: (gate_map.get(k), labels.get(k)) for k in gate_map} }）")

    print()
    print("=== ② / ③ 顶层开关盖进 spec：用户勾的算数，且盖完仍过校验 ===")
    base = {"aspect": "portrait", "fps": 15, "narration": True,
            "caption_style": "paper", "highlight_style": "karaoke",
            "shots": [{"text": "第一句文案", "code": _CODE,
                       "card": "typography/paper-title-card",
                       # 闸⑯：主体具名 + 自报的通道要有证据。`_CODE` 里画的是 `<rect />`，
                       # 所以通道是 vector（见 code_spec.shot_all_channels）。
                       "subject": "活字盘", "subject_channel": "vector",
                       "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]},
                      {"text": "第二句文案", "code": _CODE,
                       "card": "typography/paper-title-card",
                       "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}]}
    merged = cspec.overlay_knobs(dict(base), {"aspect": "landscape",
                                              "fps": 30,
                                              "voice": "zh-CN-XiaoxiaoNeural"})
    check(merged["aspect"] == "landscape" and merged["fps"] == 30,
          "顶层值覆盖 spec 同名字段（卡面勾的是横屏 30fps）")
    check(merged["voice"] == "zh-CN-XiaoxiaoNeural",
          "spec 里没有的开关也能加进来（音色由卡面决定）")
    check(merged["caption_style"] == "paper" and merged["highlight_style"] == "karaoke",
          "没传的开关保留 spec 原值（字幕那两维也一样）")

    blank = cspec.overlay_knobs(dict(base), {"aspect": "", "fps": None,
                                             "narration": "   "})
    check(blank["aspect"] == "portrait" and blank["fps"] == 15
          and blank["narration"] is True,
          f"空串/None/空白当「没勾」，不拿空值盖掉 spec（实际 {blank['aspect']!r}/"
          f"{blank['fps']!r}/{blank['narration']!r}）")

    # 卡片布尔是以字符串 arrives 的：bool("false") 为真，不转就把「不要旁白」读成「要旁白」
    coerced = cspec.overlay_knobs(dict(base), {"narration": "false"})
    check(coerced["narration"] is False,
          f'narration="false" 归并成真布尔 False（实际 {coerced["narration"]!r}）')
    check(cspec.overlay_knobs(dict(base), {"narration": "True"})["narration"] is True,
          'narration="True" 归并成 True（大小写不敏感）')

    _bogus, errors = cspec.validate_code_spec(
        cspec.overlay_knobs(dict(base), {"aspect": "diagonal", "fps": 90}))
    check(bool(errors),
          f"卡面选了枚举外的值仍在渲染前报错，不静默回落（错误 {len(errors)} 条）")
    check(any("aspect" in e for e in errors) and any("fps" in e for e in errors),
          f"报的就是这两个字段（{errors}）")

    _no_code, errs2 = cspec.validate_code_spec(
        {"shots": [{"text": "这一镜没交代码", "code": "   "}]})
    check(any(e.startswith("s01：") and "既没有 brief 也没有 code" in e for e in errs2),
          f"既没代码也没画面描述的镜带镜号退回（{errs2}）")

    def _fits(aspect: str, n: int) -> bool:
        return not any("字幕条放不下" in e for e in
                       cspec.validate_code_spec(
                           {"aspect": aspect,
                            "shots": [{"code": _CODE, "text": "字" * n,
                                       "card": "typography/paper-title-card",
                                       "subject": "活字盘", "subject_channel": "vector",
                                       "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}]})[1])

    check(_fits("portrait", cspec.MAX_TEXT_CHARS)
          and not _fits("portrait", cspec.MAX_TEXT_CHARS + 1),
          f"竖屏字幕带容量正好卡在 {cspec.MAX_TEXT_CHARS} 字")
    check(_fits("landscape", cspec.MAX_TEXT_CHARS_WIDE)
          and not _fits("landscape", cspec.MAX_TEXT_CHARS_WIDE + 1),
          f"横屏那条居中横排更宽：卡在 {cspec.MAX_TEXT_CHARS_WIDE} 字")

    print()
    print("=== ④ 确认卡摘要：分镜产物与手写 spec 两种形状都要认 ===")
    plan_shape = {"shot_count": 7, "char_count": 233, "estimated_sec": [96.0, 128.0],
                  "chain": "remotion", "aspect": "landscape", "fps": 15,
                  "narration": True, "voice": "zh-CN-YunjianNeural", "rate": "+10%",
                  "caption_style": "paper", "highlight_style": "karaoke",
                  "texture": "film",
                  "bgm": {"ref": "obj:bgm/uploads/轻快-钢琴.mp3"}}
    chips = render_gate.motion_summary(plan_shape)
    check(chips.get("镜头") == "7 个" and chips.get("文案") == "233 字",
          f"分镜账：镜数/字数（{chips}）")
    check("96" in chips.get("预计时长", "") and "128" in chips.get("预计时长", ""),
          f"分镜账：预计时长区间（{chips.get('预计时长')}）")
    check("横屏" in chips.get("画面", ""), f"画幅给人话标签不是 portrait（{chips.get('画面')}）")
    check(chips.get("帧率") == "15 fps", f"帧率（{chips.get('帧率')}）")
    check("旁白" in chips.get("声音", "") and "YunjianNeural" in chips.get("声音", ""),
          f"声音（{chips.get('声音')}）")
    check("点亮" in chips.get("字幕", ""), f"字幕两维（{chips.get('字幕')}）")
    check("钢琴" in chips.get("配乐", ""), f"配乐取文件名（{chips.get('配乐')}）")
    check(chips.get("通道") == "代码镜（渲染容器逐帧渲）",
          f"账上写了 chain=remotion 才给通道 chip（{chips.get('通道')}）")
    check("颗粒" in chips.get("质感（整片默认）", ""),
          f"整片默认档上卡（{chips.get('质感（整片默认）')}）")
    check(not any("质感" in k for k in render_gate.motion_summary(
        {**plan_shape, "texture": "none"})),
          "texture=none 不占一格——「没加质感」不是一条需要显示出来的决定")

    # 逐镜点名（B6）：卡上那一格是**整片默认**，一镜也能换一档、也能点名不要。
    rows = [{"id": "s01", "texture": "film"},        # 跟随整片（等于默认）→ 不算换过
            {"id": "s02", "texture": "tv"},          # 点名换档
            {"id": "s03", "texture": "none"}]        # 点名不要
    per = render_gate.motion_summary({**plan_shape, "plan_table": rows})
    check("2 镜点名换过档" in per.get("质感（整片默认）", ""),
          f"整片默认档 + 逐镜点名一起上卡（换过几镜要说得出）：{per.get('质感（整片默认）')}")
    none_film = render_gate.motion_summary({**plan_shape, "texture": "none",
                                            "plan_table": rows})
    check(none_film.get("质感（逐镜点名）") == "整片不加，2 镜自己点了档",
          f"整片不加但有镜点名 → 仍要占一格，不许显示成「没加质感」："
          f"{none_film.get('质感（逐镜点名）')}")

    hand = {"shots": [{"text": "一二三四五"}, {"text": "六七八九十"},
                      {"text": "第三句"}],
            "narration": "false", "caption_style": "none"}
    chips2 = render_gate.motion_summary(hand)
    hand_chars = sum(len(str(s["text"])) for s in hand["shots"])
    check(chips2.get("镜头") == "3 个" and chips2.get("文案") == f"{hand_chars} 字",
          f"手写 spec：从 shots 现数（{chips2}）")
    check(chips2.get("声音") == "无旁白（按阅读节奏驻留）",
          f'手写 spec："false" 也读成无旁白（{chips2.get("声音")}）')
    check("预计时长" not in chips2,
          "手写 spec 不重算估算区间（那是剪辑服务端的口径，抄一遍迟早漂移）")
    check("通道" not in chips2,
          "手写 spec 的账里没有 chain 这一栏，就不许替它宣称「画面出自代码链」——"
          f"chip 只转录账上真有的字（{sorted(chips2)}）")
    check(chips2.get("配乐") == "纯人声（无配乐）", f"没配乐就说没配乐（{chips2.get('配乐')}）")
    check(render_gate.motion_summary(None) == {} and render_gate.motion_summary({}) == {},
          "读不到账就空着，弹窗照样能问")

    label_table = {k: (cspec.knob_props()[k].get("value_labels") or {})
                   for k in cspec.KNOB_FIELDS}
    said = render_gate.motion_summary(plan_shape, labels=label_table)
    check(said.get("声音") == "旁白 男声·普通话 稍快",
          f"传了契约标签就回显人话，不再露 zh-CN-YunjianNeural（{said.get('声音')}）")
    check(said.get("帧率") == "15 帧·推荐",
          f"帧率标签把「贵在哪」一起说给人看（{said.get('帧率')}）")
    check(said.get("字幕") == "纸条底·画面下沿，逐词点亮"
          and said.get("画面") == "横屏",
          f"字幕两维/画幅同样走标签（{said.get('字幕')} / {said.get('画面')}）")
    check(said.get("质感（整片默认）") == "老胶片·颗粒与暗角",
          f"质感走契约标签（{said.get('质感（整片默认）')}）")
    no_gate = render_gate.motion_summary(
        {**plan_shape, "fps": 24}, labels={"fps": {}})
    check(no_gate.get("帧率") == "24 fps" and "YunjianNeural" in no_gate.get("声音", ""),
          f"标签缺失时退回本模块的小表与原值，不是退回空白（{no_gate}）")

    ask = render_gate.build_render_ask(
        None, motion={**plan_shape, "target_warnings": ["估算 128s 比目标 90s 长出 38s"]},
        title="接下来要出的是「图形科普片出片」，确认开始渲染吗？")
    check(ask.get("preview") == chips, "确认卡顶部挂的是分镜摘要")
    check(any("比目标" in w for w in ask.get("warnings") or []),
          f"分镜阶段算出的时长偏差进了题面（{ask.get('warnings')}）")
    check("横屏" in (ask.get("custom_hint") or ""),
          f"图形科普片那一路的自定义提示给的是本通道的例子（{ask.get('custom_hint')}）")
    timeline_ask = render_gate.build_render_ask({"ready": False}, motion=None)
    check("横屏" not in (timeline_ask.get("custom_hint") or ""),
          "时间线那一路没被串上图形科普片的例子")

    print()
    print("=== ⑤ 一批两条通道：只批问到的那条，另一条不执行 ===")
    storage = build_storage("memory")
    await storage.start()
    try:
        tv, mt = _SilentTool(render_gate.RENDER_NODE), _SilentTool(render_gate.MOTION_NODE)
        reg = ToolRegistry()
        reg.register(tv)      # type: ignore[arg-type]
        reg.register(mt)      # type: ignore[arg-type]
        mgr = CheckpointManager(storage)
        both = LLMResponse(tool_calls=[
            ToolCall(id="call-tv", name=render_gate.RENDER_NODE, arguments={}),
            ToolCall(id="call-mt", name=render_gate.MOTION_NODE,
                     arguments={"spec": dict(base, aspect="portrait")})])
        llm = ScriptedLLM([both, ("answer", "先等确认")])
        runner = AgentOnceRun(
            llm, reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6), checkpoint=mgr, storage=storage)
        sess = Session(user_id="u", conversation_id="c_motion_two_channel")
        await runner.run(sess, "把这两条都渲出来", run_id="run-two")

        cp = await mgr.load("run-two")
        check(cp is not None and cp.status == "awaiting_approval",
              f"两条通道都被拦下（status={getattr(cp, 'status', '?')}）")
        check(tv.calls == 0 and mt.calls == 0,
              f"挂起时两条都没执行（render_video {tv.calls} / motion {mt.calls}）")
        asked = (getattr(cp, "approval", None) or {}).get("ask") or {}
        channel = asked.get("render_node")
        check(channel in render_gate.RENDER_NODES,
              f"ask 上写清了问的是哪条通道（render_node={channel!r}）")
        check(channel == render_gate.RENDER_NODE,
              f"一次只问一条，这批先问 {render_gate.RENDER_NODE}")

        await runner.approve(cp, sess, decision=render_gate.CONFIRM_OPTION)
        check(tv.calls == 1, f"确认的那条真执行了（render_video {tv.calls} 次）")
        check(mt.calls == 0, f"没问过的那条**没有**顺带执行（motion {mt.calls} 次）")

        cp2 = await mgr.load("run-two")
        msgs = list(getattr(cp2, "messages", None) or [])
        mt_results = [m for m in msgs if isinstance(m, dict)
                      and m.get("role") == "tool" and m.get("name") == render_gate.MOTION_NODE]
        check(bool(mt_results), f"给未批准的通道补了回执（{len(mt_results)} 条）")
        if mt_results:
            body = str(mt_results[-1].get("content") or "")
            check("Error" in body or "没有执行" in body,
                  f"回执是失败形态，不冒充成功（{body[:60]}…）")
            check("确认门" in body or "不可逆" in body,
                  "回执说明了为什么：那是另一笔不可逆的算力")
    finally:
        await storage.close()

    print()
    print("=== ⑥ 图形科普片单独走门：题面用的是分镜账，不是时间线预览 ===")
    storage2 = build_storage("memory")
    await storage2.start()
    try:
        mt2 = _SilentTool(render_gate.MOTION_NODE)
        reg2 = ToolRegistry()
        reg2.register(mt2)    # type: ignore[arg-type]
        mgr2 = CheckpointManager(storage2)
        llm2 = ScriptedLLM([
            ("tool", render_gate.MOTION_NODE,
             {"spec": dict(base, shots=[{"text": "只有两句", "code": _CODE,
                                          "card": "typography/paper-title-card",
                                          "subject": "活字盘", "subject_channel": "vector",
                                          "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]},
                                        {"text": "第二句文案", "code": _CODE,
                                         "card": "typography/paper-title-card",
                                         "subject": "活字盘", "subject_channel": "vector",
                                         "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}],
                           aspect="portrait"), "aspect": "landscape",
              "narration": "false"}),
            ("answer", "完成")])
        runner2 = AgentOnceRun(
            llm2, reg2, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6), checkpoint=mgr2, storage=storage2)
        sess2 = Session(user_id="u", conversation_id="c_motion_gate")
        await runner2.run(sess2, "零素材出片", run_id="run-motion")
        cp_a = await mgr2.load("run-motion")
        asked2 = (getattr(cp_a, "approval", None) or {}).get("ask") or {}
        check(asked2.get("render_node") == render_gate.MOTION_NODE,
              f"门认代码镜这条通道（{asked2.get('render_node')!r}）")
        check("横屏" in str((asked2.get("preview") or {}).get("画面", "")),
              f"卡面顶层的横屏盖过了 spec 里的竖屏（{(asked2.get('preview') or {}).get('画面')!r}）")
        check((asked2.get("preview") or {}).get("镜头") == "2 个",
              f"摘要取的是这次调用要渲的分镜（{(asked2.get('preview') or {}).get('镜头')!r}）")
        check("无旁白" in str((asked2.get("preview") or {}).get("声音", "")),
              f"顶层 narration=false 上卡（{(asked2.get('preview') or {}).get('声音')!r}）")
        title = str(asked2.get("title") or "")
        motion_label = get_catalog().label(render_gate.MOTION_NODE)
        check(motion_label == RenderCodeMotionVideoNode.display_name,
              f"题面的通道名走单源词表（{motion_label} ← BaseNode.display_name）")
        check(motion_label in title and render_gate.RENDER_NODE not in title,
              f"题面说的是这条通道、没串到时间线那条（{title!r}）")
        check("渲染吗" in title, "题面仍是确认渲染的问句")
        await runner2.approve(cp_a, sess2, decision=render_gate.CONFIRM_OPTION)
        check(mt2.calls == 1, f"确认后 motion 通道真执行（{mt2.calls} 次）")
    finally:
        await storage2.close()

    print()
    print("=== ⑦ 技能正文点名的取值与产出字段，不能挂成「臆造工具」 ===")
    # 启动期那条一致性检查按「schema 里出现过的键名与枚举取值 + 产出字段」豁免文案。
    # 这里用**同一批真实节点类**当来源：技能写的是 paper / min_duration_sec，
    # 而它们只在代码镜那条路的 schema 与产物里存在——来源少一个就会挂假告警，
    # 假告警比不报更坏（它教人忽略这个检查本身）。
    from agent_framework import consistency as cons  # noqa: PLC0415
    from agent_framework.plan.support import enum_of as cons_enum_of  # noqa: PLC0415
    from agent_framework.skill import _parse_frontmatter  # noqa: PLC0415
    from storyline_server.nodes import core_nodes  # noqa: PLC0415
    from storyline_server.nodes.web_nodes import RenderWebNode  # noqa: PLC0415

    # 这一份必须与 ``core_nodes.build_real_registry`` 的组装**同构**（它们一起拼出生产
    # 注册表）。2026-10-16 之前这里还并着 ``ASSET_NODE_CLASSES`` / ``IMAGE_NODE_CLASSES``
    # 两族——取材那三条路拆掉之后，注册表里就只剩下面这些，来源跟着一起收窄：
    # 留着已删的节点当来源，等于给这台检查喂一份比生产更宽的白名单（假绿）。
    classes = [*core_nodes.REAL_NODE_CLASSES, RenderWebNode,
               PlanMotionNode, RenderCodeMotionVideoNode, PatchCodeMotionVideoNode]

    class _Schema:
        def __init__(self, schema: dict) -> None:
            self.parameters = schema

    # 节点那一路**必须按 MCP 通道的形状**造：真机上模型手里的剪辑工具是 MCP 声明
    # （``server.py::_signature_for`` 把 input_schema 翻成函数签名），**只剩顶层类型**
    # ——拿 ``cls.input_schema`` 整个塞进去是替被测链路作弊，而这条链要验的恰恰是
    # 「顶层那两份来源都收不到嵌套键」。契约参数那一份单独走 ``node_props``。
    class _McpDecl:
        def __init__(self, props: dict) -> None:
            self.parameters = {"type": "object",
                               "properties": {k: {"type": v.get("type")}
                                              for k, v in props.items()}}

    node_tools = [_McpDecl((cls.input_schema or {}).get("properties") or {})
                  for cls in classes]
    # 剪辑服务端除了 DAG 节点，还注册三个自检工具（`server.py::_register_selfcheck`：
    # check_shot_code / check_film / preview_shot）。它们与节点走**同一条 MCP 通道**
    # 进主服务注册表，所以技能正文（注入契约、风格拆法、节奏自查那几节）点到它们时，
    # 这里必须认得出——少收这一份的后果与 §9-Q91 同形：正文里的真工具被误报成臆造。
    node_names = ({cls.name for cls in classes}
                  | {"render_status", "check_shot_code", "check_film", "preview_shot"})
    # 会话公共参数由剪辑服务端注册工具时注入（storyline_server/server.py::_signature_for），
    # 节点自己的 input_schema 里没有——来源少这一份就会把 artifact_id 判成臆造的名字。
    common = {"session_id", "artifact_id", "user_request", "lang", "mode",
              "user_id", "conversation_id"}
    node_keys = set(common)
    enum_vals: set[str] = set()
    for cls in classes:
        props = (cls.input_schema or {}).get("properties") or {}
        node_keys |= {str(k) for k in props}
        for spec in props.values():
            enum_vals |= cons_enum_of(spec)
    # 本地工具名（== run_server.run 启动时真正注册进主注册表的那些）。
    # 这份清单必须与生产同构：漏一个真名就会把技能正文里**正确**的工具名误报成
    # 「臆造工具」——假告警比不报更坏，它教人忽略这台检查本身（见 consistency.py 头注）。
    local_names = {"web_search", "fetch_url", "fetch_media", "load_skill", "ask_user",
                   "submit_plan", "confirm_plan", "rerun_from", "read_node_history",
                   "render_status", "update_memory", "read_memory", "spawn_subagent",
                   "read_file", "write_file", "edit_file", "grep",
                   "read_inbox", "send_message",
                   "create_cron_job", "delete_cron_job", "list_cron_jobs",
                   # 2026-10-25：简洁视频线的四个原生工具。它们不进 DAG，但模型能在
                   # 技能正文里看到、也会真的去调——不列进来，新技能正文里**正确**的
                   # 工具名就会被误报成「臆造工具」（这条用例上面刚说过那个道理）。
                   "make_short_video", "reshoot_shot", "render_short_film",
                   "list_short_films"}
    known = node_names | local_names | _module_defined(m_code, m_client)
    submit_schema = {"type": "object", "properties": {
        "plans": {"type": "array", "items": {"type": "object", "properties": {
            "steps": {"type": "array", "items": {"type": "object",
                                                 "properties": {"param_options": {}}}}}}}}}
    # 与生产同构：run_server 传的是 param_keys() | enum_values()（键名 + 声明过的取值）
    # **外加契约参数那一份 node_props**。少了后者就复现了 2026-10-13 真机上那条假告警：
    # 技能正文讲的是「`shots[]` 里的 `derived_from` 怎么填」，这是**嵌套**键，顶层那两份
    # 都收不到——`param_keys()` 按自己的契约只收顶层，MCP 声明也只剩类型。
    # 「测试全绿而真机挂告警」的形状就是来源两处各算各的（§9-Q91）。
    node_props = [(cls.input_schema or {}).get("properties") or {} for cls in classes]
    fields = cons.skill_field_names(
        node_param_keys=node_keys | enum_vals,
        node_props=node_props,
        tools=[node_tools, [_Schema(submit_schema)]])
    # 原生工具的入参名也是「合法字段」：新技能正文里写着 `film_id` / `mp4_url` /
    # `note` 这些，而它们既不是 DAG 节点参数、也不在 submit 的 schema 里。
    # 生产侧不会漏（`vocab.param_keys()` 是把**注册表里所有工具**的入参合起来），
    # 这里手工列一份，否则这条用例会拿真机上不存在的假告警来定罪。
    # **改这几个工具的入参时要同步这张表**（`test_short_video` 会钉住入参名）。
    fields |= {"prompt", "seconds", "aspect", "render", "film_id", "shot", "note",
               "stills", "mp4_url", "coverage", "audit", "shots", "calls",
               "verdict", "vivid", "content", "hint"}
    # D-红先：把 node_props 抽掉（= 真机修好之前那份来源），判据必须**红**并且精确点名
    # 那一批嵌套字段——一支从不红的判据没有资格说「对齐了」。
    thin = cons.skill_field_names(
        node_param_keys=node_keys | enum_vals,
        tools=[node_tools, [_Schema(submit_schema)]])

    # 2026-10-25：图形科普链退役，`motion_explainer` 的正文移到 `examples/retired/`。
    # 于是「模型能选到的技能」只剩 `examples/skills/` 下那几份——这一栏只扫它们。
    # 退役那份正文仍然要用：它是复现「真机那条假告警」的唯一样本（thin_bad 那一条），
    # 所以下面单独把它读进来，但不参与「技能点名了不存在的工具」这条检查。
    bodies: dict[str, str] = {}
    for md in sorted((Path(__file__).resolve().parents[1]
                      / "examples" / "skills").glob("*/SKILL.md")):
        meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        bodies[meta.get("name") or md.parent.name] = body
    check(any(n == "short_video_skill" for n in bodies),
          f"示例技能都读到了（{len(bodies)} 份：{sorted(bodies)}）")
    bad = cons.unknown_tools_in_skills(bodies, known, param_keys=fields)
    check(not bad, f"技能正文点名的工具/取值/字段都在真实来源里（误报：{bad}）")
    _retired = (Path(__file__).resolve().parents[1] / "examples" / "retired"
                / "motion_explainer" / "SKILL.md")
    thin_bad = cons.unknown_tools_in_skills(
        {"motion_explainer_skill": _retired.read_text(encoding="utf-8")},
        known, param_keys=thin)
    check(any("recurring_prop" in b or "highlight_color" in b for b in thin_bad),
          f"抽掉 node_props 就复现真机那条假告警（实得 {thin_bad}）")
    still = cons.unknown_tools_in_skills(
        {"x": "先调 motion_render_video 出片，再按 ink 排字幕"},
        known, param_keys=fields)
    check(still == ["motion_render_video"],
          f"真写错的名字照样被抓（ink 是声明过的取值，不算）：{still}")

    print()
    print(f"=== ⑧ {len(cspec.KNOB_FIELDS)} 个开关真走到计划卡：契约 → 词表 knob_facts → 校验器 ===")
    # 这一段钉的是「接进计划门」的最后一环。链路上有两个来源：MCP 的工具声明
    # （``_signature_for`` 把 input_schema 翻成函数签名，**只有类型过得去**）和
    # DAG 契约里的 params（description / enum / 上下界只能走这条路）。
    # 只验节点 schema 有枚举是不够的——卡读的是 props_for()，它可能拿不到。
    from agent_framework.editing_contract import EditingContract, NodeContract  # noqa: PLC0415
    from agent_framework.plan.support import as_card_value  # noqa: PLC0415
    from agent_framework.plan.validate import PlanValidator  # noqa: PLC0415
    from agent_framework.plan.vocab import PlanVocabulary  # noqa: PLC0415

    pair = (PlanMotionNode, RenderCodeMotionVideoNode)
    motion_params = {cls.name: dict((cls.input_schema or {}).get("properties") or {})
                     for cls in pair}

    class _McpTool(Tool):
        """按 MCP 通道真实形状复刻的声明：只剩类型，别的带不过来。

        这里**不能**偷懒直接用 ``node.input_schema`` 当工具声明——那是替被测链路
        作弊，契约的作用就验不出来了。
        """

        def __init__(self, node: str, props: Mapping[str, Any]) -> None:
            self._node = node
            self._props = {k: {"type": v.get("type")} for k, v in props.items()}
            self.contract = NodeContract(name=node, params=props)

        @property
        def name(self) -> str:
            return self._node

        @property
        def description(self) -> str:
            return f"{self._node}（MCP 声明只带类型）"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": self._props, "required": []}

        async def execute(self, **kw):
            return "{}"

    reg = ToolRegistry()
    for cls in pair:
        reg.register(_McpTool(cls.name, motion_params[cls.name]))
    vocab = PlanVocabulary(
        registry=reg,
        contract=EditingContract(nodes={
            cls.name: NodeContract(name=cls.name, params=motion_params[cls.name])
            for cls in pair}))

    decl = ((reg.get(PlanMotionNode.name).parameters or {}).get("properties") or {})
    merged_props = vocab.props_for(PlanMotionNode.name)
    check("enum" not in decl.get("aspect", {}),
          "MCP 声明侧的 aspect 确实只剩类型（枚举过不了 _signature_for）")
    check(merged_props.get("aspect", {}).get("enum") == list(cspec.ASPECTS),
          f"枚举由契约带回来：{merged_props.get('aspect', {}).get('enum')}")

    facts = {f["node"]: {k["key"]: k for k in f["knobs"]}
             for f in await vocab.knob_facts()}
    card = facts.get(PlanMotionNode.name) or {}
    check(set(card) >= set(cspec.KNOB_FIELDS),
          f"plan_motion 的卡面开关含全部 {len(cspec.KNOB_FIELDS)} 个（实际 {sorted(card)}）")
    for key in cspec.KNOB_FIELDS:
        prop = cspec.knob_props()[key]
        labels = prop.get("value_labels") or {}
        want_v = {as_card_value(v) for v in (prop.get("enum") or ["true", "false"])}
        knob = card.get(key) or {}
        listed = {str(x).split(" ")[0] for x in str(knob.get("values", "")).split("、")}
        check(listed == want_v,
              f"{key} 的候选取值与契约一致（{knob.get('values')}）")
        check(all(" " in x for x in str(knob.get("values", "")).split("、")),
              f"{key} 的每个取值都带着中文标签一起进提示（模型照抄就抄到人话）")
        check(want_v <= set(labels),
              f"{key} 的取值全部有标签（缺 {sorted(want_v - set(labels))}）——"
              f"漏一个卡面就露出机器名")
        check(knob.get("default") == as_card_value(prop.get("default")),
              f"{key} 的默认值上卡（{knob.get('default')}）")
    check("target_duration_sec" not in card,
          "目标秒数**不是**开关（无上下界声明，反查不到枚举源）——它走「其他」")
    check(not ({"style", "card", "visual"} & set(merged_props)),
          "旧链那几栏不在顶层入参里，卡上因此不可能造出这些开关")

    check(set(vocab.enum_values()) >= {as_card_value(v)
                                       for k in cspec.KNOB_FIELDS
                                       for v in (cspec.knob_props()[k].get("enum") or [])},
          f"{len(cspec.KNOB_FIELDS)} 个开关的取值全在 enum_values() 里（与⑦的文案豁免同一个来源）")

    def _opt(key: str, options: list | None = None) -> dict:
        prop = cspec.knob_props()[key]
        return {"key": key,
                "options": [{"value": v}
                            for v in (options if options is not None
                                      else prop.get("enum") or ["true", "false"])],
                "default": prop.get("default")}

    validator = PlanValidator(vocab)
    plans = {"plans": [{
        "plan_id": "p1", "label": "零素材科普片", "goal": "一句话出片",
        "steps": [{"seq": 1, "node": PlanMotionNode.name,
                   "why": "先出分镜，把画幅与配音这些决定交给用户挑",
                   "expectation": f"卡面上这 {len(cspec.KNOB_FIELDS)} 个开关都能改",
                   "param_options": [_opt(k) for k in cspec.KNOB_FIELDS]}]}]}
    good, issues = await validator.validate(plans)
    check(not issues.errors,
          f"{len(cspec.KNOB_FIELDS)} 个开关的计划过校验（打回：{issues.errors}）")
    on_card = (good[0]["steps"][0]["param_options"] if good else [])
    check({c["key"] for c in on_card} == set(cspec.KNOB_FIELDS),
          f"归一化后卡上仍是 {len(cspec.KNOB_FIELDS)} 个（实际 {sorted({c['key'] for c in on_card})}）")
    check(all(len(c["options"]) >= 2 for c in on_card),
          "每个开关都给了两个以上候选（单值开关等于没给用户选择权）")

    # 卡面文字：模型没写 display 时**必须由参数声明的标签接住**，
    # 否则用户看到的 radio 是 portrait / zh-CN-XiaoxiaoNeural 这种机器名。
    labels_by_key = {k: (cspec.knob_props()[k].get("value_labels") or {})
                     for k in cspec.KNOB_FIELDS}
    shown = {c["key"]: {o["value"]: o["display"] for o in c["options"]}
             for c in on_card}
    check(all(all(v for v in one.values()) for one in shown.values()),
          f"{len(cspec.KNOB_FIELDS)} 个开关的每个候选都有卡面文字（没有空 display）")
    check(shown["aspect"] == {"portrait": "竖屏", "landscape": "横屏",
                             "square": "方屏"},
          f"回落用的是声明里的中文标签（{shown['aspect']}）")
    check(all(d not in labels_by_key[c["key"]] for c in on_card
              for o in c["options"] for d in [o["display"]]),
          "卡面文字是标签而不是取值本身（不会把 portrait 当文字显示）")

    # 服务端兜底：零素材那条链现在是一个**原生工具**（`make_short_video`，一次调用
    # 跑完整条链，没有 DAG 形状），模型交上来的 hint 就是空的。
    # 2026-10-25 之前这里用的是 `plan_motion`（DAG 节点）；那条链退役后换成它，
    # 顺带钉住两件事：① 声明了 `plan_node = True` 的原生工具**能上计划卡**
    # （`vocab.whitelist()` 的第二类）；② 它照样按 `_IMPLICIT_SKILLS` 预注入技能。
    class _ShortVideoTool(Tool):
        plan_node = True

        @property
        def name(self) -> str:
            return "make_short_video"

        @property
        def description(self) -> str:
            return "一句话出片"

        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "object",
                    "properties": {"prompt": {"type": "string"},
                                   "seconds": {"type": "number"}},
                    "required": ["prompt"]}

        async def execute(self, **kw: Any) -> str:
            return "{}"

    reg.register(_ShortVideoTool())

    class _ShortVideoSkill:
        name = "short_video_skill"
        display = "图形科普短片（零素材出片）"
        available = True

    async def _installed():
        return {"short_video_skill": _ShortVideoSkill()}

    bare_plan = {"plans": [{"plan_id": "p1", "label": "零素材科普片", "goal": "一句话出片",
                            "steps": [{"seq": 1, "node": "make_short_video",
                                       "why": "一句话出片", "expectation": "回一条链接"}]}]}
    filled, _ = await PlanValidator(vocab, skills=_installed).validate(bare_plan)
    check(filled and filled[0]["steps"][0]["skills_hint"] == ["short_video_skill"],
          f"卡上没写 skills_hint：校验器按工具把这份技能补进卡面"
          f"（{filled[0]['steps'][0]['skills_hint'] if filled else '整份被打回'}）")
    no_lib, _ = await PlanValidator(vocab, skills=None).validate(bare_plan)
    check(no_lib and no_lib[0]["steps"][0]["skills_hint"] == [],
          "技能库没接/这份没装时不补——卡上不能挂一个点开是空的技能")

    async def _card_with_display(key: str, text: str) -> list[dict[str, Any]]:
        values = (cspec.knob_props()[key].get("enum") or ["true", "false"])
        step = {"seq": 1, "node": PlanMotionNode.name, "why": "先出分镜",
                "expectation": "卡面上能改开关",
                "param_options": [{
                    "key": key, "default": cspec.knob_props()[key].get("default"),
                    "options": [{"value": v, "display": text} for v in values]}]
        }
        good2, _ = await validator.validate({"plans": [{
            "plan_id": "p1", "label": "零素材科普片", "goal": "一句话出片",
            "steps": [step]}]})
        return good2[0]["steps"][0]["param_options"] if good2 else []

    mine = await _card_with_display("aspect", "我自己写的说法")
    check(all(o["display"] == "我自己写的说法"
              for c in mine if c["key"] == "aspect" for o in c["options"]),
          f"模型写了 display 时以模型的为准（{[o['display'] for c in mine for o in c['options']]}）")

    async def _errors(opts: list[dict]) -> list[str]:
        payload = {"plans": [{
            "plan_id": "p1", "label": "零素材科普片", "goal": "一句话出片",
            "steps": [{"seq": 1, "node": PlanMotionNode.name,
                       "why": "先出分镜", "expectation": "卡面上能改开关",
                       "param_options": opts}]}]}
        _, iss = await validator.validate(payload)
        return list(iss.errors)

    got = await _errors([_opt("aspect", ["portrait", "landscape", "cinema"])])
    check(any("cinema" in e for e in got),
          f"卡面出现契约里没有的取值照样打回（{got}）")
    got = await _errors([{"key": "style",
                          "options": [{"value": "archive"}, {"value": "magazine"}]}])
    check(any("style" in e and "没有参数" in e for e in got),
          f"未声明的开关造不出来（{got}）")
    got = await _errors([_opt("caption_style", ["none"])])
    check(any("只给了 1 个候选值" in e for e in got),
          f"单值开关被打回，要求给用户真实候选（{got}）")

    thin = PlanVocabulary(registry=reg, contract=None)
    left = {k["key"] for f in await thin.knob_facts()
            if f["node"] == PlanMotionNode.name for k in f["knobs"]}
    check(left == {"narration"},
          f"契约缺失时只剩布尔那一个开关（实际 {sorted(left)}）——"
          f"枚举的唯一来源就是契约，这一环断了卡面会静默变薄")

    print()
    print("=== ⑨ 标签真走到确认卡：Runner 手里必须有门，否则悄悄退回机器名 ===")
    # _motion_value_labels 跑在 AgentOnceRun 上，而 plan_gate 原本只挂在 Agent 上——
    # 那条 ``getattr(self, "plan_gate")`` 是永远为 None 的空读：不报错，只是卡面
    # 悄悄少了一层人话。所以这里按**真实装配**验一次：门从构造函数进来，确认卡拿到人话。
    from types import SimpleNamespace  # noqa: PLC0415

    runner_with_gate = AgentOnceRun(
        ScriptedLLM([]), ToolRegistry(), ContextBuilder("BASE"),
        hooks=CompositeHook([]), plan_gate=SimpleNamespace(vocab=vocab))
    check(runner_with_gate._motion_value_labels().get("voice", {}).get(
              "zh-CN-YunjianNeural") == "男声·普通话",
          f"门注入后 Runner 能从词表取到标签（{sorted(runner_with_gate._motion_value_labels())}）")
    bare = AgentOnceRun(ScriptedLLM([]), ToolRegistry(), ContextBuilder("BASE"),
                        hooks=CompositeHook([]))
    check(bare._motion_value_labels() == {},
          "没接门（子代理、独立装配）时如实回空表，由确认卡自带的小表兜着——不是崩")

    storage9 = build_storage("memory")
    await storage9.start()
    try:
        mt9 = _SilentTool(render_gate.MOTION_NODE)
        reg9 = ToolRegistry()
        reg9.register(mt9)    # type: ignore[arg-type]
        mgr9 = CheckpointManager(storage9)
        llm9 = ScriptedLLM([
            ("tool", render_gate.MOTION_NODE,
             {"spec": dict(base, voice="zh-CN-YunjianNeural", rate="+10%"),
              "narration": "true", "fps": 15, "caption_style": "paper",
              "highlight_style": "karaoke"}),
            ("answer", "完成")])
        runner9 = AgentOnceRun(
            llm9, reg9, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6), checkpoint=mgr9, storage=storage9,
            plan_gate=SimpleNamespace(vocab=vocab))
        await runner9.run(Session(user_id="u", conversation_id="c_motion_labels"),
                          "零素材出片", run_id="run-labels")
        cp9 = await mgr9.load("run-labels")
        chips9 = ((getattr(cp9, "approval", None) or {}).get("ask") or {}).get("preview") or {}
        check(chips9.get("声音") == "旁白 男声·普通话 稍快",
              f"确认卡回显的是计划卡上那句话，不是音色机器名（{chips9.get('声音')}）")
        check(chips9.get("帧率") == "15 帧·推荐"
              and chips9.get("字幕") == "纸条底·画面下沿，逐词点亮",
              f"帧率/字幕同一条链（{chips9.get('帧率')} / {chips9.get('字幕')}）")
    finally:
        await storage9.close()

    # =====================================================================
    # ⑩~⑮：旧声明式链下线后，这六件事在别处没有第二个家
    # =====================================================================

    print()
    print("=== ⑩ 出处对账的凭据语义：账只由真跑起来的检索写 ===")
    # 判定与降级（哪些 source 算冒充）归 ``tests/test_code_gates.py``——那是闸的本体。
    # 这里补的是它没覆盖的**记账侧**语义：没有身份、只有标题、换会话、inline 那一路、
    # source 形状给错。判据只有一处（``motion_plan.gate_state`` / ``settle_sources``），
    # 测试拆两处会漂。
    from agent_framework import retrieval
    from agent_framework.identity import use_identity
    from agent_framework.orchestration import NodeState
    from agent_framework import secrets as af_secrets

    def _cshot(cid: str, text: str, **kw: Any) -> dict[str, Any]:
        s: dict[str, Any] = {"id": cid, "text": text, "code": _CODE,
                             "card": "typography/paper-title-card",
                             "subject": "活字盘", "subject_channel": "vector",
                             "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}
        s.update(kw)
        return s

    gate_state = NodeState(session_id="u:gate-u:c:gate-c")

    def _gate_node() -> PlanMotionNode:
        # 分镜与出处两道闸都不碰 storage/workspace（不烧像素），离线直调给 None
        return PlanMotionNode(Settings(), Providers(), None)     # type: ignore[arg-type]

    async def gate_plan(shots: list[dict[str, Any]]) -> dict[str, Any]:
        return await _gate_node().process(gate_state, {"spec": {"shots": shots}})

    async def gate_error(shots: list[dict[str, Any]]) -> str:
        try:
            await gate_plan(shots)
        except ValueError as e:
            return str(e)
        return ""

    cited = [{"id": "s01", "text": "一茶匙中子星物质约十亿吨。", "code": _CODE,
              "card": "typography/paper-title-card",
              "subject": "中子星", "subject_channel": "vector",
              "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"],
              "source": {"标题": "Neutron star",
                         "链接": "https://en.wikipedia.org/wiki/Neutron_star"}}]

    af_secrets.unbind_storage()          # 账本压根不存在（离线直调 / 没装配存储）
    check(await retrieval.ledger("u:gate-u:c:gate-c") is None,
          "没绑存储时 ledger 回 None：是「没有账本」，不是「一条都没查过」")
    check(await gate_error(cited) == "",
          "没有账本时闸整体放行——拿「没记过」定罪会退化成所有出处一律打回")

    g10 = build_storage("memory")
    await g10.start()                    # Storage.start() 里 bind_storage，两边共用同一条路径
    try:
        check(await retrieval.ledger("u:gate-u:c:gate-c") == [],
              "空账回 []（与 None 是两个意思：这里确实一次都没查到过）")
        # 2026-10-19 真机后：对不上账不再打回整份分镜，就地降级成「未核实：…」，
        # 提醒随回执带回去（理由见 motion_plan._mark_source_unverified）。
        demoted = await gate_plan(cited)
        check(demoted["plan_table"][0]["source_gate"] == "declared_unverified"
              and str(demoted["plan_table"][0]["source"]).startswith("未核实"),
              f"空账 + 声称查过的出处 → 入库但标成未核实："
              f"{demoted['plan_table'][0].get('source')}")
        check(any("s01" in n and "未核实" in n for n in demoted.get("autofix_notes") or []),
              "降级这件事带镜号写进回执——账上仍是一句「未核实」，不是悄悄放行")

        honest = [_cshot("s01", "一茶匙约十亿吨。",
                         source={"标题": "未核实：模型记忆里的数量级"})]
        check(await gate_error(honest) == "",
              "自标未核实的出处放行——闸只拦「冒充查过」，不罚诚实")
        check(await gate_error([_cshot("s01", "一茶匙约十亿吨。")]) == "",
              "source 留空不算声称查证，闸不响")

        await retrieval.note_hit("https://no-identity.example/x")
        check(all("no-identity" not in str(r["url"])
                  for r in await retrieval.ledger("u:gate-u:c:gate-c")),
              "不在任何 run 之内（没有身份）就不记账——账只由真跑起来的检索写")

        with use_identity("gate-u", "gate-c"):
            await retrieval.note_hit("https://EN.wikipedia.org/wiki/Neutron_star/",
                                     "Neutron star", backend="wikipedia")
        check(await gate_error(cited) == "",
              "URL 归一后对得上账（大小写与尾斜杠不该成为降级的理由）")
        pulsar = await gate_plan([_cshot("s01", "十亿吨。",
                                         source={"链接":
                                                 "https://en.wikipedia.org/wiki/Pulsar"})])
        check(pulsar["plan_table"][0]["source_gate"] == "declared_unverified",
              "同站不同条目不算查到过——降级到未核实，而不是前缀匹配放行")
        titled = await gate_plan([_cshot("s01", "十亿吨。",
                                         source={"标题": "维基百科 Neutron star 条目",
                                                 "链接": ""})])
        check(titled["plan_table"][0]["source_gate"] == "verified",
              "只有标题、账里那条标题对上了也算查证过（检索回来常常只有标题没有可用链接）")

        with use_identity("gate-u", "gate-c"):
            await retrieval.note_hits([{"url": "https://a.example/x", "title": "甲"},
                                       {"url": "", "title": "没有链接的行"}])
            rows = await retrieval.ledger("")
            check(any(r["url"] == "https://a.example/x" for r in rows or []),
                  f"note_hits 按当前身份入账，缺 url 的行跳过（{len(rows or [])} 条）")

        result = await gate_plan(cited)
        check(result["plan_table"][0]["source"] == "Neutron star",
              f"过了闸的出处照常如实进逐镜账（{result['plan_table'][0]['source']}）")
        check(result["chain"] == "remotion",
              f"分镜产物自报链身份，逐镜账与命中表才认得这是代码镜那一路（{result['chain']}）")
        check(result["plan_table"][0]["code_chars"] == len(_CODE),
              f"分镜账只报代码字节数不报正文（一张卡装不下一段 TSX）："
              f"{result['plan_table'][0]['code_chars']}")

        await g10.retrieval.drop_for_user("gate-u")   # 账清空：只可能因为「作用域」而查不到
        other = NodeState(session_id="u:gate-u:c:other-conv")
        cross = await _gate_node().process(other, {"spec": {"shots": cited}})
        check(cross["plan_table"][0]["source_gate"] == "declared_unverified",
              "换个会话就查不到那一笔——账是按会话计的，不是全局白名单")
        check((await gate_plan(cited))["plan_table"][0]["source_gate"]
              == "declared_unverified",
              "同一份分镜把账清空后立刻降级：凭据只有这一笔账，没有第二份影子状态")

        # 同一道对账也要走到**出片节点那条旁路**：read_spec_input 的第一层来路是入参里的
        # inline spec，它盖在 Store 那份上面、从没过分镜闸。这里直接调 `_gate_inline`：
        # 判的是「降级发生在开任务行之前」，不真起渲染（完整出片路径归 test_code_gates）。
        inline_state = NodeState(session_id="u:gate-u:c:gate-c")
        render10 = RenderCodeMotionVideoNode(Settings(), Providers(), g10)
        inline_spec = {"shots": [dict(s) for s in cited]}
        await render10._gate_inline(inline_state, {"spec": inline_spec},
                                    inline_spec, [])
        check(any("s01" in n and "未核实" in n for n in inline_state.summary.notes),
              "绕过 plan_motion 直接塞 inline 分镜，同一个 settle_sources 也标成未核实")
        check(await g10.render_jobs.get("u:gate-u:c:gate-c", "_default") is None,
              "降级发生在开任务行之前：这一步不碰渲染，也不该留下 running 行")

        bad_shape = await gate_error([{"id": "s01", "text": "", "code": "",
                                       "source": {"标题": "某本书"}}])
        check("校验未通过" in bad_shape,
              "分镜校验闸在前、出处对账在后：一条错误清单就够，不必再判出处")

        dropped = await gate_plan([_cshot("s01", "十亿吨。", source="维基百科")])
        check(dropped["motion_spec"]["shots"][0]["source"] == {},
              "source 给成字符串时归一直接丢掉——消失的出处不会冒充成一条引用")
    finally:
        await g10.close()
        af_secrets.unbind_storage()

    print()
    print("=== ⑩-b P4 整份替换说清差集 + P2 承诺未判如实报账 ===")
    # 真机事故：模型以为第二次调用是「追加 14 镜」，实际把前 6 镜覆盖掉了，而回执
    # 一句话都没说。判据本体（差集怎么算、话术逐字）在 ``motion_plan._shots_replacement``；
    # 这里先钉纯函数，再走一遍真 ``process`` 看它真的附进回执——只报不拦。
    from storyline_server.nodes import motion_plan as mplan

    repl = mplan._shots_replacement(
        {"shots": [{"id": "s01"}, {"id": "s02"}, {"id": "s03"}]},
        [{"id": "s01"}, {"id": "s03"}])
    check(repl is not None and repl["removed"] == ["s02"] and repl["added"] == [],
          f"id 差集两栏：去掉了哪些、多了哪些各自成列（{repl}）")
    check("整份替换（上一版 3 镜 → 本次 2 镜）" in repl["note"],
          "第一句先定性：这是整份替换、不是追加——两边镜数都摆在明面上")
    check("少了：s02" in repl["note"], "少了哪些逐个点出来，不让人自己数")
    check(mplan._shots_replacement({"shots": [{"id": "s01"}]},
                                   [{"id": "s01"}]) is None,
          "一模一样的一版不啰嗦：没有差集也没有怪号就回 None")
    check(mplan._shots_replacement(None, [{"id": "s01"}]) is None,
          "没有上一版草稿就没有「替换」可言（首次提交）")
    jump = mplan._shots_replacement(
        {"shots": [{"id": "s01"}, {"id": "s02"}]},
        [{"id": "s02"}, {"id": "s04"}])
    check(any("不从 s01 开始" in n for n in jump["id_notes"]),
          f"镜号不从 s01 起要明说（{jump['id_notes']}）")
    check(any("断号" in n and "s03" in n for n in jump["id_notes"]),
          f"断号缺哪几个也逐个点出来（{jump['id_notes']}）")

    # 走真 process：第二次整份提交时，回执必须带着那一栏；容器离线时
    # `style_facts` 如实记「未判」——未判不是通过（与 D-未判不绿 同一条纪律）。
    p4_state = NodeState(session_id="u:p4-u:c:p4-c")

    async def p4_plan(shots: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return await PlanMotionNode(Settings(), Providers(), None).process(   # type: ignore[arg-type]
            p4_state, {"spec": {"shots": shots, **extra}})

    async def _down() -> bool:
        return False

    with mock.patch.object(mplan, "_remotion_up", _down):
        first = await p4_plan([_cshot("s01", "一茶匙约十亿吨。"), _cshot("s02", "压缩后更重。"),
                               _cshot("s03", "密度惊人。")])
        check("shots_replacement" not in first,
              "首次提交没有「上一版」，回执不带替换那一栏")
        second = await p4_plan([_cshot("s01", "一茶匙约十亿吨。"),
                                _cshot("s03", "密度惊人。",
                                       caption_note="本镜自排字幕：画面右上角就是标题",
                                       style_note="满幅海报，边距刻意压到 2%")],
                               style_facts={"colors_max": 3})
        note = str((second.get("shots_replacement") or {}).get("note") or "")
        check("整份替换（上一版 3 镜 → 本次 2 镜）" in note and "少了：s02" in note,
              f"第二次整份提交带着 id 差集进回执：{note[:70]}")
        # 2026-10-18：可测承诺（style_facts）那套读数已并入静帧美术评审——没有预览
        # 就没有读数，回执不另外造一栏「未判」。这里钉住的是同一条纪律的另一面：
        # 没跑预览时不伪造任何风格结论。`style_facts` 输入在校验层就不收（不是静默
        # 宣称通过，而是这一栏已经不存在了）。
        check("style_promise_unjudged" not in second and "style_facts" not in second,
              "容器离线时不伪造风格读数（度量已并入静帧评审）")
        check("visual_review" not in second,
              "没跑预览就没有评审栏——未判不是通过")
        # 2026-10-18：逐镜的 caption_note/style_note 申报栏已随字幕契约那块一起摘掉，
        # 卡面改读闸自己量出来的事实（style / subject / subject_channel）。这里钉住
        # 「卡面给的是机器读数，不是模型自报的整齐话」。
        rows = second["plan_table"]
        check([r["id"] for r in rows] == ["s01", "s03"],
              f"逐镜账按交上来的顺序：{[r['id'] for r in rows]}")
        check(all(r["style"] in cspec.DRAWN_CHANNELS for r in rows),
              f"每镜的 style 栏是代码里真认出的画法通道：{[r['style'] for r in rows]}")
        check(all(r["subject_channel"] in ("", *cspec.SUBJECT_CHANNELS) for r in rows)
              and [r["subject_channel"] for r in rows] == ["vector", "vector"],
              "subject_channel 栏原样带出申报值（白名单外的在校验层就拦掉了）")

    print()
    print("=== ⑪ 局部改「读哪一版」：编辑包 → 指纹 → 分叉换号 ===")
    # 这三个方法（``_bundle`` / ``_base_spec`` / ``_fork``）住在与链无关的基类里，
    # 而注册出来的工具只有一个 patch_motion_code 会走到它们——别处没有第二个调用点。

    class _Objs:
        """假对象存储：按 key 存字节；head 回 None 表示「这个键不存在」。"""

        def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
            self.blobs = dict(blobs or {})

        async def head(self, key: str):
            return self.blobs.get(key)

        async def get_stream(self, key: str):
            data = self.blobs[key]              # 缺键 → KeyError，与真存储的失败同形
            for i in range(0, len(data), 64):
                yield data[i:i + 64]

    class _Scope:
        """``storage.artifacts(sess, scope)`` 的替身：只记读写，不碰库。"""

        def __init__(self, log: list, sess: str, scope: str,
                     blobs: dict[str, Any] | None = None) -> None:
            self.log, self.sess, self.scope = log, sess, scope
            self.blobs: dict[str, Any] = dict(blobs or {})

        async def get(self, node: str):
            return self.blobs.get(node)

        async def put(self, node: str, payload: Any) -> None:
            self.blobs[node] = payload
            self.log.append(("put", self.scope, node))

        async def clone_from(self, other: "_Scope") -> None:
            self.log.append(("clone", self.scope, other.scope))
            for k, v in other.blobs.items():
                self.blobs.setdefault(k, v)

    class _Repo:
        def __init__(self) -> None:
            self.log: list[Any] = []
            self.repos: dict[tuple[str, str], _Scope] = {}

        def artifacts(self, sess: str, scope: str) -> _Scope:
            key = (sess, str(scope or ""))
            if key not in self.repos:
                self.repos[key] = _Scope(self.log, sess, key[1])
            return self.repos[key]

    class _Store(_Repo):
        """局部改这一段用到的存储面：objects + artifacts（不碰 workspace，不烧像素）。"""

        def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
            super().__init__()
            self.objects = _Objs(blobs)

    #: 链身份三件（数据源、要点名工具名的文案）由代码链子类给出。这里显式写一遍，
    #: 是为了让「基类不认任何链」这件事在测试里也成立——链身份是被测数据，不是环境。
    def _bundle_stub(**over):
        kw: dict[str, Any] = {"patch_chain": "remotion",
                              "patch_render_node": "render_motion_code",
                              "patch_validator": staticmethod(cspec.validate_code_spec),
                              "payload_builder": staticmethod(plan_payload),
                              "patch_next_hint":
                                  PatchCodeMotionVideoNode.patch_next_hint}
        kw.update(over)
        return kw

    def _bundle_node_stub(store: Any, **over):
        """只交出 ``_bundle`` / ``_base_spec`` / ``_fork`` 要读的那几样链数据源。

        用 setattr 装到实例上而不是造一个假子类：假子类会绕过
        ``MotionPatchBase`` 真正继承的那一份，测试就不在测那条流水线了。
        """
        node = PatchCodeMotionVideoNode(Settings(), Providers(), store)
        for k, v in _bundle_stub(**over).items():
            setattr(node, k, v)
        return node

    _KEY = "renders/u_c/base9/hitmap.json"

    async def _reject(coro) -> str:
        try:
            await coro
        except ValueError as e:
            return str(e)
        return ""

    missing = await _reject(_bundle_node_stub(_Store({}))._bundle("u:c", "base9"))
    check("重新出片" in missing and "render_motion_code" in missing and _KEY in missing,
          f"读不到表就拒这次局部改，并把那个键与出路一起说清（{missing[:70]}…）")

    old_chain = await _reject(_bundle_node_stub(
        _Store({_KEY: json.dumps({"chain": "declarative"}).encode()}))._bundle("u:c", "base9"))
    check("已经下线" in old_chain and "plan_motion" in old_chain,
          f"旧链的产物认出链身份后要求重新走一遍分镜（{old_chain[:70]}…）")

    no_chain = await _reject(_bundle_node_stub(
        _Store({_KEY: json.dumps({"spec": {}}).encode()}))._bundle("u:c", "base9"))
    check("没有链身份" in no_chain,
          f"链身份那一栏是后来才写的，缺失的老产物同样不能说清改错了地方（{no_chain[:60]}…）")

    not_obj = await _reject(_bundle_node_stub(
        _Store({_KEY: b"[1, 2]"}))._bundle("u:c", "base9"))
    check("不是一个对象" in not_obj, f"表形状不对也照样拒（{not_obj[:50]}…）")

    node_ok = _bundle_node_stub(_Store({_KEY: json.dumps({"chain": "remotion"}).encode()}))
    check((await node_ok._bundle("u:c", "base9"))["chain"] == "remotion", "链身份对上的表放行")

    # 一份真过契约的分镜：s01 交两个可改旋钮，其中一个本次量到了盒、另一个压根没自报
    _rt, _errs = cspec.validate_code_spec({"aspect": "portrait", "fps": 15, "shots": [
        {"id": "s01", "code": _CODE, "text": "一茶匙约十亿吨。",
         "card": "typography/paper-title-card",
         "subject": "中子星", "subject_channel": "vector",
         "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"],
         "highlight": ["十亿吨"], "min_duration_sec": 3,
         "fields": [{"name": "radius", "type": "number", "label": "半径",
                     "value": 12, "min": 1, "max": 100, "step": 1, "unit": "km"},
                    {"name": "notch", "type": "number", "label": "刻口", "value": 2}]},
        {"id": "s02", "code": _CODE, "text": "换个写法。",
         "card": "typography/paper-title-card",
         "subject": "中子星", "subject_channel": "vector",
         "derived_from": ["ui-entrance/list-stack-press/ListStackPress.tsx"]}]})
    check(not _errs, f"夹具本身必须过真契约，否则红的是夹具（{_errs}）")
    _fp = mhit.fingerprint(_rt)

    node = _bundle_node_stub(_Store())
    got, whence, fp = await node._base_spec("u:c", "base9",
                                            {"spec": copy.deepcopy(_rt),
                                             "spec_fingerprint": _fp})
    check(got == _rt and fp == _fp and "自带" in whence,
          f"编辑包自带的 spec 是第一来路（{whence}）")

    store11 = _Store()
    node = _bundle_node_stub(store11)
    store11.artifacts("u:c", "base9").blobs["plan_motion"] = {"motion_spec":
                                                              copy.deepcopy(_rt)}
    got2, whence2, _ = await node._base_spec("u:c", "base9",
                                             {"spec_fingerprint": _fp})   # 老片子：表不带 spec
    check("作用域 base9" in whence2 and got2["shots"][0]["id"] == "s01",
          f"老片子退回产物作用域那一份，而不是会话当前（{whence2}）")

    stale = await _reject(node._base_spec("u:c", "base9", {"spec_fingerprint": "0" * 16}))
    check("重新出片" in stale and "指针" in stale,
          f"表与分镜不是同一版 → 拒收，指针会把新分镜的字认错（{stale[:70]}…）")

    nowhere = await _reject(_bundle_node_stub(_Store())._base_spec(
        "u:c", "base9", {"spec_fingerprint": _fp}))
    check("plan_motion" in nowhere, f"两层都读不到就点名要重新分镜（{nowhere[:60]}…）")

    # 分叉：新版本必须换号，并把改动同步回「模型下一次读的」那份
    class _AS:
        """``ArtifactStore.open`` 的替身——_fork 只要它把 repo 接住。"""

        @staticmethod
        async def open(repo: _Scope, sess: str, artifact: str) -> _Scope:
            return repo

    async def fork_case(state_artifact: str, base: str) -> tuple[str, list, Any]:
        store = _Store()
        node = _bundle_node_stub(store)
        st = NodeState(session_id="u:c", artifact_id=state_artifact)
        with mock.patch.object(m_nodes, "ArtifactStore", _AS):
            new = await node._fork(st, sess="u:c", base=base, spec=copy.deepcopy(_rt),
                                   target=None, gate={"s01": "verified"})
        return new, store.log, st

    new_a, log_a, st_a = await fork_case("", "base9")
    check(new_a.startswith("p") and len(new_a) == 9 and new_a != "base9",
          f"没有产物号时现铸一个 p########（{new_a}）")
    check(("clone", new_a, "base9") in log_a,
          f"上游产物整份带进新作用域，拦截器才只跑这一步（{log_a}）")
    check(("put", new_a, "plan_motion") in log_a and ("put", "", "plan_motion") in log_a,
          f"新版与「会话当前」两处都写，模型下一次重渲不会把编辑丢掉（{log_a}）")
    check(st_a.artifact_id == new_a, f"state 跟着换号，后续落位都读新作用域（{st_a.artifact_id}）")

    new_b, log_b, _ = await fork_case("base9", "base9")
    check(new_b != "base9" and ("clone", new_b, "base9") in log_b,
          f"传号等于 base 也必须换号——沿用就是把旧版覆盖掉，而旧版是唯一退路"
          f"（换号后来源仍是 base9 而不是 _default：{new_b} / {log_b}）")

    new_c, log_c, _ = await fork_case("p11223344", "base9")
    check(new_c == "p11223344", f"已经是别的号就不换（{new_c}）")
    check(("clone", "p11223344", "base9") in log_c, "带上游仍然照做")

    print()
    print("=== ⑫ 命中表与代表帧的发布：表是自足的 ===")
    # 表条目形状由**真实生产者** ``code_render._shot_table`` 现做。手抄一份形状的后果
    # 正是这条链下线过一次的原因：生产者改了字段，抄的那份照样绿。

    class _Pub:
        """workspace 的替身：记 (键, content_type)，可切成「发布必炸」。"""

        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []
            self.fail = False

        async def publish(self, path: str, key: str, *, content_type: str = "") -> str:
            if self.fail:
                raise RuntimeError("桶挂了")
            self.published.append((key, content_type))
            return f"obj:{key}"

        def dir_for(self, sess: str, artifact: str, *dirs: str) -> Path:
            return WORK.joinpath(str(sess).replace(":", "_"), str(artifact or ""), *dirs)

    class _Art:
        def __init__(self, pub: _Pub) -> None:
            self.workspace = pub
            self.objects = _Objs()

    _tbl = m_code._shot_table(
        "s01", 0, (1080, 1920), shot=_rt["shots"][0],
        checks={"radius": {"status": "ok", "expected": 12.0, "reported": 12.0,
                           "box": {"x": 100, "y": 200, "w": 300, "h": 80}},
                "notch": {"status": "unreported", "expected": 2.0}})
    entries = _tbl["entries"]
    check(entries[0]["kind"] == "shot" and entries[0]["box"] == [0, 0, 1080, 1920]
          and entries[0]["pointer"] == "/shots/0/code",
          f"整镜一条是底色而非假设：盒铺满画面、指针落在 code（{entries[0]}）")
    field_rows = [e for e in entries if e["kind"] == "field"]
    check([e["name"] for e in field_rows] == ["radius", "notch"],
          f"每个**声明出来的**字段各出一条（{[e['name'] for e in field_rows]}）")
    check(field_rows[0]["pointer"] == "/shots/0/fields/radius/value"
          and field_rows[0]["box"] == [100.0, 200.0, 300.0, 80.0]
          and field_rows[0]["value"] == 12.0 and field_rows[0]["reported"] == 12.0,
          f"量到的字段：指针按 name、盒是成片像素、注入值与自报值都在表上（{field_rows[0]}）")
    check(field_rows[1]["box"] is None and field_rows[1]["field_status"] == "unreported",
          f"量不到盒照样出一条（box=null + 状态说明为什么），不许留一片空白给前端猜"
          f"（{field_rows[1]['name']}）")
    check(field_rows[0]["min"] == 1.0 and field_rows[0]["max"] == 100.0
          and field_rows[0]["step"] == 1.0 and field_rows[1]["min"] is None,
          "控件范围跟着条目走：声明里没给就发 null，前端据此退成纯数字框而不是自己编上界")

    _out = {"shots": [
        {"id": "s01", "sec": 3.5, "speech_sec": 3.1, "cached": True,
         "cache_key": "k" * 16, "chain": "remotion"},
        {"id": "s02", "sec": 2.0, "speech_sec": None, "cached": False,
         "cache_key": "", "chain": "remotion"}],
        "hitmap": {"s01": _tbl},
        "master_hash": "m" * 16, "lockfile_hash": "l" * 16, "gl": "swiftshader",
        "width": 1080, "height": 1920, "fps": 15.0, "duration": 5.5,
        "shot_files": {}}

    pub12 = _Pub()
    node12 = RenderCodeMotionVideoNode(Settings(), Providers(), _Art(pub12))
    frames12 = await node12._publish_frames(_out, sess="u:c", artifact="ab12")
    check(frames12 == {} and not pub12.published,
          "没有切片文件时一帧都不发，也不编出键来")

    WORK.mkdir(parents=True, exist_ok=True)
    png = WORK / "s01.png"
    png.write_bytes(b"\x89PNG fake")
    missing_png = WORK / "s02.png"
    if missing_png.exists():
        missing_png.unlink()
    _out["shot_files"] = {"s01": {"frame": str(png)}, "s02": {"frame": str(missing_png)}}
    frames13 = await node12._publish_frames(_out, sess="u:c", artifact="ab12")
    check(frames13 == {"s01": "renders/u_c/ab12/frames/s01.png"},
          f"代表帧按**产物作用域**发；缓存里那份键随改字就换，不能当缩略图地址（{frames13}）")
    check(any("s02：没有代表帧" in e for e in _out.get("hitmap_errors") or []),
          "缺的那张如实记账，不静默少一格")

    _out["hitmap_errors"] = []
    key12 = await node12._publish_hitmap(
        NodeState(session_id="u:c", artifact_id="ab12"), _rt, _out,
        sess="u:c", artifact="ab12", frames=frames13)
    check(key12 == "renders/u_c/ab12/hitmap.json", f"→ 对象键（{key12}）")
    check(("renders/u_c/ab12/hitmap.json", "application/json") in pub12.published,
          f"content_type 跟着走，浏览器与用例都认得这一份是 JSON（{pub12.published}）")
    blob = json.loads((WORK / "u_c" / "ab12" / "motion"
                       / "hitmap.json").read_text(encoding="utf-8"))
    check(blob["schema"] == "motion-code-hitmap/1" and blob["chain"] == "remotion",
          f"表自报 schema 与链身份（{blob['schema']} / {blob['chain']}）")
    check(blob["spec"] == _rt and blob["spec_fingerprint"] == mhit.fingerprint(_rt),
          "表带着它所量的那一版分镜 + 指纹：局部改那一头比对的就是这两个")
    check(blob["hit_mode"] == "space"
          and blob["editable_pointers"] == list(cspec.EDITABLE_POINTERS),
          f"可改指针白名单写在表上，前端与模型不必再猜（{blob['editable_pointers']}）")
    check(blob["controls"] == cspec.knob_props(),
          "整片开关的控件清单同源随表走（前端表单只有一个出处）")
    check(blob["identities"] == {"master_hash": "m" * 16, "lockfile_hash": "l" * 16,
                                 "gl": "swiftshader"},
          f"渲染身份三项进表：缓存键里有的东西表上就得有（{blob['identities']}）")
    check((blob["width"], blob["height"], blob["fps"], blob["duration"])
          == (1080, 1920, 15.0, 5.5), "像素与时钟按成片实测值进表，不是按 spec 要求")
    tl = blob["timeline"]
    check([r["start_sec"] for r in tl] == [0.0, 3.5]
          and tl[0]["frame_at_sec"] == 3.5 and tl[0]["cached"] is True
          and tl[0]["cache_key"] == "k" * 16 and tl[0]["chain"] == "remotion"
          and tl[0]["frame"] == "renders/u_c/ab12/frames/s01.png",
          f"逐镜时间轴：起点累加、代表帧取末段、缓存与链身份逐镜都在（{tl[0]}）")

    pub12b = _Pub()
    node12b = RenderCodeMotionVideoNode(Settings(), Providers(), _Art(pub12b))
    no_tab = {**_out, "hitmap": {}, "hitmap_errors": []}
    check(await node12b._publish_hitmap(
        NodeState(session_id="u:c", artifact_id="ab12"), _rt, no_tab,
        sess="u:c", artifact="ab12") == "" and not pub12b.published,
          "这一版压根没有命中层（shots 表为空）→ 空串、不发对象，而不是发一份空表冒充有")

    pub12c = _Pub()
    pub12c.fail = True
    node12c = RenderCodeMotionVideoNode(Settings(), Providers(), _Art(pub12c))
    boom = {**_out, "hitmap_errors": []}
    check(await node12c._publish_hitmap(
        NodeState(session_id="u:c", artifact_id="ab12"), _rt, boom,
        sess="u:c", artifact="ab12") == ""
          and any("命中表没落进对象存储" in e for e in boom["hitmap_errors"]),
          "发布失败只牵连「这次能不能选着改」，成片照常——并如实记一笔原因")

    print()
    print("=== ⑬ 按镜切片缓存的收发协议 + 质感滤镜串 ===")
    # 缓存键的算法归 tests/test_code_spec.py 按字节断言；这里管的是**对象存储那一半**：
    # 四个文件同进同出、缺一个即整体未命中、写缺文件当场抛。
    class _Ws:
        def __init__(self) -> None:
            self.objects = _Objs()
            self.published: list[tuple[str, str]] = []

        async def publish(self, path: str, key: str, *, content_type: str = "") -> str:
            self.published.append((key, content_type))
            self.objects.blobs[key] = Path(path).read_bytes()
            return f"obj:{key}"

    ws13 = _Ws()
    cache13 = m_nodes._ObjectShotCache(ws13, "u:x:c:y")
    src_dir = WORK / "src13"
    src_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    for name, data in (("clip.mp4", b"clip-bytes"), ("frame.png", b"\x89PNG frame"),
                       ("meta.json", b'{"id": "s01"}'), ("hitmap.json", b'{"entries": []}')):
        p = src_dir / name
        p.write_bytes(data)
        files[name] = p
    await cache13.store("k1", files)
    keys = [k for k, _ in ws13.published]
    check(len(keys) == 4 and all(k.startswith("motion-shots/u_x_c_y/k1/") for k in keys),
          f"四个文件全在同一个会话前缀下——session_gc 的级联回收认的就是这个前缀（{keys}）")
    check({ct for _, ct in ws13.published} == {"video/mp4", "image/png", "application/json"},
          f"content_type 按文件分（{sorted(set(ws13.published))}）")
    hit = await cache13.fetch("k1", WORK / "dst13")
    check(bool(hit) and len(hit or {}) == 4
          and (hit or {})["clip.mp4"].read_bytes() == b"clip-bytes",
          f"命中就把四个文件交回本地（{sorted(hit or {})}）")
    check(await cache13.fetch("nope", WORK / "dst13b") is None, "没写过的键：head 直接 None")

    ws13.objects.blobs["motion-shots/u_x_c_y/k1/frame.png"] = b""
    check(await cache13.fetch("k1", WORK / "dst13c") is None,
          "零字节的 frame.png 当未命中——半截切片拿去拼接就是「画面少一镜」那种谎")
    del ws13.objects.blobs["motion-shots/u_x_c_y/k1/frame.png"]
    check(await cache13.fetch("k1", WORK / "dst13d") is None,
          "读异常同样当未命中（重渲一遍即可），不改抛出片失败")

    partial = {k: v for k, v in files.items() if k != "hitmap.json"}
    store_err = ""
    try:
        await cache13.store("k2", partial)
    except FileNotFoundError as e:
        store_err = str(e)
    check("hitmap.json" in store_err,
          f"写缓存缺文件必须当场抛，不能悄悄存一份「下次命中就少一张表」的切片（{store_err}）")

    _cmds: list[tuple] = []

    def _tcmds(*a: Any, **kw: Any) -> None:
        _cmds.append(a)

    orig_ffmpeg = rpipe.mediaops.ffmpeg
    rpipe.mediaops.ffmpeg = _tcmds
    try:
        srcm = Path(str(src_dir / "clip.mp4"))
        chain = rpipe.apply_texture(srcm, WORK / "master_film.mp4", "film")
        check(chain == rpipe.TEXTURE_FILTERS["film"]
              and any("-filter_complex" in c for c in _cmds),
              "film 档：返回**实际消费的**滤镜串，账上那句「跑了但看不出来」才有出处")
        rpipe.apply_texture(srcm, WORK / "master_none.mp4", "none")
        check(_cmds[-1][:3] == ("-i", str(srcm), "-c") and "-filter_complex"
              not in _cmds[-1],
              f"none 档走 -c copy：不加质感的片子不该再编一次（{_cmds[-1][:5]}）")
        check(rpipe.apply_texture(srcm, WORK / "master_bad.mp4", "retro8mm") == ""
              and len(_cmds) == 3,
              "契约外的档位同样退成拷贝并回空串——由证据那一行说「选了档却没拿到滤镜串」")
        check(set(rpipe.TEXTURE_FILTERS) == set(cspec.TEXTURES) - {"none"},
              f"滤镜表与契约清单同源（界面上有这档、渲出来必须是这档）："
              f"{sorted(set(cspec.TEXTURES) ^ (set(rpipe.TEXTURE_FILTERS) | {'none'}))}")
    finally:
        rpipe.mediaops.ffmpeg = orig_ffmpeg

    print()
    print("=== ⑭ 指针工具：只认镜头子树，落笔不改入参 ===")
    check(mhit._segs("/shots/0/fields/radius/value")
          == ["shots", "0", "fields", "radius", "value"], "指针拆段")
    check(mhit._segs("/shots/0/panel/重量~1体积")[-1] == "重量/体积",
          "先解 JSON pointer 转义再比对：面板键里带「/」时不转义会把一个键拆成两段")
    check(mhit._segs("/shots/0/a~0b")[-1] == "a~b", "~0 解回波浪号")

    patched = mhit.apply_patch(_rt, "/shots/0/text", "换成另一句话。")
    # 夹具的原文过契约时句尾的「。」已被 `strip_caption_periods` 去掉（用户要求
    # 句尾不画那个圈），所以这里断的是**归一化之后**的那一句——断带句号的原文
    # 会把「返回新对象」这条判据永远钉成红。
    check(patched["shots"][0]["text"] == "换成另一句话。"
          and _rt["shots"][0]["text"] == "一茶匙约十亿吨",
          "返回**新对象**：分镜是已入库的产物，就地改会把旧版的编辑包也换掉")
    check(mhit.apply_patch(_rt, "/shots/0/source", {"标题": "未核实：x"})
          ["shots"][0]["source"] == {"标题": "未核实：x"},
          "/shots/i/source 那条指针落在已有字段上")

    refused_map: dict[str, str] = {}
    for pointer in ("/aspect", "/shots/9/text", "/shots/0/panel/新键", "/shots/0",
                    "/shots/x/text", "/shots/0/fields/radius/value"):
        try:
            mhit.apply_patch(_rt, pointer, 1)
            refused_map[pointer] = ""
        except (ValueError, IndexError, KeyError) as e:
            refused_map[pointer] = str(e)
    check(all(refused_map[p] for p in refused_map),
          f"越界/不存在/新键/数组按名字寻址的一律拒（{refused_map}）")
    check("radius" in refused_map["/shots/0/fields/radius/value"],
          f"字段那条指针**故意**不走通用补丁器：它把 fields 当数组、按 int(seg) 走，"
          f"于是把 name 段当数字解析就炸（按 name 寻址归 code_patch._apply_field）："
          f"{refused_map['/shots/0/fields/radius/value'][:50]}")
    _shot14 = copy.deepcopy(_rt["shots"][0])
    check(mcode._apply_field(_shot14, "radius", 40, "/shots/0/fields/radius/value") is None
          and _shot14["fields"][0]["value"] == 40.0,
          f"同一格在装配器那一侧按 name 落得下去（{_shot14['fields'][0]['value']}）")
    check("不在这一镜的清单里" in (mcode._apply_field(
              _shot14, "nope", 1, "/shots/0/fields/nope/value") or ""),
          "没声明的旋钮不能凭空改：打回时把这一镜现有清单一起交出去")

    check(mhit.fingerprint({"b": 2, "a": 1}) == mhit.fingerprint({"a": 1, "b": 2}),
          "指纹与键序无关（同一份分镜换写法不该换指纹）")
    _v2 = copy.deepcopy(_rt)
    _v2["shots"][0]["fields"][0]["value"] = 13.0
    check(mhit.fingerprint(_rt) != mhit.fingerprint(_v2),
          "改一个字段值就换指纹：拿着旧表改新片必须被撞出来")

    print()
    print("=== ⑮ 词时间轴贴到字符区间：漏词跳过、尾部并入，不留亮不起来的洞 ===")
    _txt = "一茶匙中子星，约十亿吨。"
    aligned = rpipe.align_words(_txt, [
        {"text": "一茶匙", "start_ms": 0.0, "duration_ms": 600.0},
        {"text": "中子星", "start_ms": 600.0, "duration_ms": 500.0},
        {"text": "于是", "start_ms": 1100.0, "duration_ms": 400.0},   # 文案里没有：TTS 多读
        {"text": "约十亿吨", "start_ms": 1600.0, "duration_ms": 700.0}])
    check([a["token"] for a in aligned] == ["一茶匙", "中子星", "约十亿吨"],
          f"落不进文案的词跳过，而不是让字幕空一段（{[a['token'] for a in aligned]}）")
    check(aligned[0]["char_start"] == 0 and aligned[0]["char_end"] == 3,
          f"按出现顺序定位字符区间（{aligned[0]}）")
    check(aligned[-1]["char_end"] == len(_txt),
          f"尾部字符（逗号句号）并入最后一个词：整句都被时间轴盖住（{aligned[-1]}）")
    check(all(aligned[i]["char_start"] >= aligned[i - 1]["char_end"]
              for i in range(1, len(aligned))),
          "区间互不重叠——重叠会让母版那一趟按后一个词把前一个盖掉")
    check((aligned[0]["start_ms"], aligned[0]["end_ms"], aligned[2]["start_ms"])
          == (0.0, 600.0, 1600.0),
          f"毫秒刻度与 durationInFrames 同一把尺（{aligned[0]['start_ms']}"
          f"/{aligned[0]['end_ms']}/{aligned[2]['start_ms']}）")
    check(rpipe.align_words(_txt, []) == [], "TTS 没回词就回空表，字幕退化成整条显示")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
