# -*- coding: utf-8 -*-
"""零素材图形科普片的分镜契约（motion spec）。

这类片子**没有视频素材**：画面是排版渲染出来的，所以「素材」在这条链路里的含义是
**文案 + 数据 + 版式模板**。本模块定义中间产物长什么样、以及服务端替模型把关哪些事。

一条贯穿全模块的口径：**镜头时长不由模型给**。
无旁白时它可以给（阅读节奏）；有旁白时时钟归语音时长，模型写死的秒数只会造成
「话没说完画面已切走」。所以 `duration_sec` 在这里降级成**下限参考**，
最终时长在渲染阶段由 `tts_timed` 的实测结果回填（见 render.py）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import art as m_art
from . import diagrams
from . import templates as m_tpl

# ---------------------------------------------------------------------------
# 枚举：模型只能在这些取值里选，越界一律拒（枚举是承诺，不是建议）
# ---------------------------------------------------------------------------

#: 版式风格的总名：真正的版式由 ``card``（画什么）与 ``aspect``（怎么排）共同决定，
#: 这里只留一个扩展位，将来出第二种纸质感时才添候选。
STYLES = ("archive_card",)

#: 画面比例 → 像素尺寸。**横屏有独立的第二套版式**（1920×1080 设计空间），
#: 竖屏与方图共用 1080×1920 的竖构图，方图只是四周多留纸。
ASPECTS: dict[str, tuple[int, int]] = {
    "portrait": (1080, 1920),
    "landscape": (1920, 1080),
    "square": (1080, 1080),
}

#: 帧率白名单：逐帧截图按帧计费，帧率是这条链路最大的成本项，不给连续值
FPS_CHOICES = (10, 15, 24, 30)

#: 卡片类型 = 版式组件。每个都对应模板里一段真实存在的排版，没有的就是编的。
#: 后两组是新开的两条通道：图示卡按数据画图（SVG viewBox，横竖都自适应），
#: custom 把画笔交给模型自己画一张示意图——SVG 文本必须先过 motion/art.py 的净化闸。
CARD_KINDS = (
    "plain",        # 只有字幕条，背景留白（转场/呼吸镜）
    "archive",      # 档案卡：编号 + 出处 + 红框（参考片主力版式）
    "dict_entry",   # 词条卡：大字英文词条，可叠红印章
    "stamp",        # 印章特写：整张卡就是「尚未存在 / NOT YET INVENTED」
    "book",         # 翻开书页 / 封面（书、报刊、出版物）
    "print",        # 版画与叠印纸（复制术时代的画面）
    "theatre",      # 银幕 + 座椅剪影（影院）
    "webpage",      # 浏览器骨架 + 图表（互联网时代）
    "silhouette",   # 暗场人物剪影 + 屏幕光（当下）
    "title",        # 标题/收尾卡
    "flow",         # 流程链：2~8 步依次入场（做法、工序、因果链）
    "compare",      # 左右对照：两栏各一个标题 + 若干要点
    "timeline",     # 时间线：2~8 个节点按序排开
    "levels",       # 层级/能级：一条条水平台阶，右侧数字滚动
    "chart",        # 柱状/折线：一组 label+value，带单位
    "scatter",      # 散点：x/y 两个轴，最多 30 个点
    "orbit",        # 环绕：中心体 + 若干轨道体（连续运动，整镜都要逐帧截）
    "custom",       # 自定义画面：visual.svg 内联一整段 SVG
)

#: 图示卡单源在 diagrams.py——清单漂了就会有一张卡没有 builder。
DIAGRAM_KINDS = tuple(diagrams.BUILDERS)

MAX_SHOTS = 120
MAX_TEXT_CHARS = 60          # 竖屏撕纸条两行的容量
MAX_TEXT_CHARS_WIDE = 110    # 横屏那条居中横排字幕：一行更宽，两行装得下更多
MAX_HIGHLIGHTS_PER_SHOT = 4

#: 中文字色白名单（edge-tts --list-voices 实测 14 个；值随服务商变，故也放行合规格式）
VOICES = (
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiNeural", "zh-CN-YunxiaNeural",
    "zh-CN-YunyangNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-HsiaoYuNeural", "zh-TW-YunJheNeural",
)

#: 字幕形态：撕纸条 + 逐词高亮是参考片的读法
SUBTITLE_MODES = ("torn_highlight", "torn", "bottom", "none")

MAX_SHOTS = 120
MAX_TEXT_CHARS = 60          # 竖屏撕纸条两行的容量
MAX_HIGHLIGHTS_PER_SHOT = 4

_VOICE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,})?-[A-Za-z]+Neural$")
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")

#: 语速档位（前端下拉直接给这几个，比让用户填百分号友好）
RATE_CHOICES = ("-20%", "-10%", "+0%", "+10%", "+20%", "+30%")

#: 开关取值的中文标签。**每个取值都要有一句**：卡面上的 radio 文字优先用模型写的
#: ``display``，模型没写就回落到这里（见 ``plan/validate.py::_check_params``）——
#: 漏一个取值，界面上就露出 ``portrait`` / ``zh-CN-XiaoxiaoNeural`` 这种机器名。
#: 措辞与主服务的确认卡标签同源（``agent_framework/render_gate.py``），两份漂了由
#: ``tests/test_motion_channel.py`` 拦下。
KNOB_VALUE_LABELS: dict[str, dict[str, str]] = {
    "aspect": {"portrait": "竖屏", "landscape": "横屏", "square": "方屏"},
    # 帧率是这条链路最大的成本项，标签要把「贵在哪」一起说给人看
    "fps": {"10": "10 帧·最省", "15": "15 帧·推荐",
            "24": "24 帧·更顺", "30": "30 帧·最贵"},
    "narration": {"true": "有人声旁白", "false": "无旁白（只看字）"},
    "voice": {
        "zh-CN-XiaoxiaoNeural": "女声·普通话",
        "zh-CN-XiaoyiNeural": "女声·普通话（偏年轻）",
        "zh-CN-YunjianNeural": "男声·普通话",
        "zh-CN-YunxiNeural": "男声·普通话（偏年轻）",
        "zh-CN-YunxiaNeural": "男声·少年音",
        "zh-CN-YunyangNeural": "男声·播报腔",
        "zh-CN-liaoning-XiaobeiNeural": "女声·东北口音",
        "zh-CN-shaanxi-XiaoniNeural": "女声·陕西方言",
        "zh-HK-HiuGaaiNeural": "女声·粤语",
        "zh-HK-HiuMaanNeural": "女声·粤语（偏软）",
        "zh-HK-WanLungNeural": "男声·粤语",
        "zh-TW-HsiaoChenNeural": "女声·台湾口音",
        "zh-TW-HsiaoYuNeural": "女声·台湾口音（偏慢）",
        "zh-TW-YunJheNeural": "男声·台湾口音",
    },
    "rate": {"-20%": "很慢", "-10%": "稍慢", "+0%": "正常",
             "+10%": "稍快", "+20%": "较快", "+30%": "很快"},
    "subtitle_mode": {"torn_highlight": "撕纸条·逐词点亮", "torn": "撕纸条",
                      "bottom": "底部字幕", "none": "不上字幕"},
}

#: 每字朗读秒数的经验区间：估算总时长用，不是承诺值。
#: 实测 zh-CN 男声 +0% 约 0.22~0.28 秒/字（含标点停顿），比 edge-tts 老兜底的
#: 0.22 宽一档，因为科普片会读标点。
SEC_PER_CHAR_LOW = 0.20
SEC_PER_CHAR_HIGH = 0.30


# ---------------------------------------------------------------------------
# 用户开关（计划卡上那一排 radio）
# ---------------------------------------------------------------------------

#: 顶层入参名 = 落进 spec 的字段名。为什么要复制到**顶层**再合回来：计划卡的开关
#: 只能从节点顶层入参反查枚举源（``plan/vocab.py`` 的 ``props_for`` 只看
#: ``input_schema.properties``），嵌在 spec 里的字段对卡面不存在——模型写了卡也出不来。
#:
#: ``style`` 故意不在这里：当前只实现了一套版式，单值枚举等于没有选择权，
#: 硬凑第二个候选就是瞎编。多一套模板时再补进这份清单。
KNOB_FIELDS = ("aspect", "fps", "narration", "voice", "rate", "subtitle_mode")


def knob_props() -> dict[str, Any]:
    """各开关的 JSON Schema 片段。枚举值取自本模块的清单，单源不再抄第二遍。

    末尾统一挂上 ``value_labels``：取值与它的人话标签同一处声明，随 DAG 契约一起
    带回主服务，卡面就不需要谁再抄一份对照表。
    """
    props = {
        "aspect": {
            "type": "string", "enum": list(ASPECTS), "default": "portrait",
            "description": "画幅比例：portrait 竖屏(短视频平台) / landscape 横屏 / square 方屏，"
                           "决定成片像素尺寸",
        },
        "fps": {
            "type": "integer", "enum": list(FPS_CHOICES), "default": 15,
            "description": "帧率。逐帧截图按帧计费，这是这条链路最大的成本项："
                           "15 帧够流畅又便宜，30 帧最顺但截图时间翻倍",
        },
        "narration": {
            "type": "boolean", "default": True,
            "description": "是否有人声朗读。开了镜头长度就归真实语音时长（话没说完画面不切走），"
                           "关了按阅读节奏驻留、且 voice/rate 失效",
        },
        "voice": {
            "type": "string", "enum": list(VOICES), "default": "zh-CN-YunjianNeural",
            "description": "旁白音色（edge-tts 中文音色，narration=true 才有意义）。"
                           "Xiaoxiao/Xiaoyi 女声，Yunjian/Yunxi/Yunyang 男声，"
                           "liaoning/shaanxi 是方言，HK/TW 为粤语与台湾口音",
        },
        "rate": {
            "type": "string", "enum": list(RATE_CHOICES), "default": "+0%",
            "description": "语速档位。语速越快整片越短，与目标时长对账时先看它",
        },
        "subtitle_mode": {
            "type": "string", "enum": list(SUBTITLE_MODES), "default": "torn_highlight",
            "description": "字幕形态：torn_highlight 撕纸条+逐词点亮(参考片读法) / "
                           "torn 只撕纸条 / bottom 底部常规字幕 / none 不上字幕",
        },
    }
    for key, spec in props.items():
        spec["value_labels"] = dict(KNOB_VALUE_LABELS.get(key) or {})
    return props


def overlay_knobs(spec: dict[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """把顶层用户开关并进 spec，返回新副本；**卡上勾选的值赢过模型写在 spec 里的值**。

    为什么是覆盖而不是「spec 优先」：一次勾选是人对这条片子的决定（要竖屏就是要竖屏），
    而 spec 里那份是模型自己填的默认。两者不一致时按人的来，否则卡面就成了装饰。

    ``narration=false`` 必须照样生效，所以判「有没有传」用的是 None 哨兵而不是真值。
    值本身不在这儿校验——合完还要过 ``validate_spec``，越界照样回错误清单。
    """
    out = dict(spec)
    for key in KNOB_FIELDS:
        value = inputs.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key == "narration" and isinstance(value, str):
            # 卡面上的布尔终值是字符串 "true"/"false"，而 bool("false") 是 True——
            # 用户关掉旁白却渲出一整条配音片，这叫把开关读反了。
            text = value.strip().lower()
            if text in ("true", "false"):
                value = text == "true"
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# 入参结构（给模型看的那份 schema）
# ---------------------------------------------------------------------------

#: ``visual`` 的字段合同。**这一大段是要给模型读的**，所以每条都写到「填了会画成什么」：
#: 只说「按模板取」等于让模型猜，猜错就是一轮白跑。
#: 开头那句是整段的前提：``text`` 只会出现在字幕条上，版式画面画的是这里的字段——
#: 不写清这一层，模型会把内容全塞进 ``text``，出片就是一堆只有边框的空壳
#: （真机第一版模型自写分镜 12 镜里空了 7 张卡）。空壳现在由 ``_check_visual_content``
#: 带镜号退回，不再静默出片。
_VISUAL_DOC = (
    "**text 只进字幕条，不进版式画面**——下面这些字段才是画面上看得见的东西，"
    "所选 card 需要的字段没填齐会带镜号退回，**清单外的键也一样退回**"
    "（这张卡不读的键不会出现在画面上，别把它当内容）：\n"
    "· archive：inv 编号 / line1~line3 挂签三行（第三行做弱色）\n"
    "· dict_entry：word 大字词条 / note 词头小注\n"
    "· stamp 与 title：zh、en 两行文字；at_ms 落章时刻（省略则按镜头长度自动排）\n"
    "· book：lines 页码行数（≤14）\n"
    "· print：title 版名\n"
    "· webpage：year 年份 / bars 收入柱高度数组（0~100，最多 6 根）\n"
    "· flow：steps 数组，每项 \"文字\" 或 {label, note}，2~8 步；可加 title、note、caption\n"
    "· compare：left/right 各 {title, points:[…]}（每边 ≤4 条，超出按前面截断）、vs 中间圆牌文字\n"
    "· timeline：marks 数组 {year, label}，2~8 个\n"
    "· levels：levels 数组 {name, value}，2~8 层（值决定台阶长度，数字会滚动到位）\n"
    "· chart：series 数组 {label, value}，2~8 项；mode 取 bar（默认）或 line；unit 单位\n"
    "  levels/chart 的**值写原数**，别自己先除以一万：≥1 万的读数会自动收成 万/亿/万亿 "
    "后缀（否则 13 位数字会捅出画面）。最大最小差到 100 倍以上会自动换**对数刻度**并在"
    "题注写明（条长不再等比，读数仍是原数）；想强制等比就写 scale: \"linear\"，"
    "差得不远也想用对数就写 scale: \"log\"\n"
    "· scatter：points 数组 {x, y, label?}，≤30 个；x-max/y-max 轴上限；x-label/y-label 轴名\n"
    "· orbit：center {label}；parts 数组 {label, r 半径, period-ms 一圈毫秒}，≤5 个。"
    "**这是连续运动：整镜每帧都要真截**，别在长镜头上用\n"
    "· custom：svg＝一整段自画示意图，规则见下\n"
    "\n【custom 的 SVG 合同】"
    f"根节点必须是 <svg> 且带 viewBox（横竖画幅共用一套坐标全靠它，建议 0 0 1000 560；"
    f"横屏的画面带是 1760×658，要铺满那条带就写宽幅，例如 0 0 1600 600），"
    f"整段 ≤{m_art.MAX_SVG_CHARS} 字、≤{m_art.MAX_ELEMENTS} 个元素。"
    "只放行绘图元素与绘图属性：<script>/<foreignObject>/<image>/外链 href 一律拒；"
    "<animate>/<animateTransform>/<set> 这类 SMIL 动画也拒——它们按墙钟走，"
    "逐帧截图推不动，画面会冻在起始帧。"
    "要动就写 data-anim（" + m_art.anim_table() + "），"
    "配 data-at（毫秒）、data-dur（毫秒）、data-dist（像素，可负）、"
    "data-count-to 与 data-dp（小数位）、data-num-unit（数字后缀，如 万/亿）、"
    "data-period（一圈毫秒，用于 orbit/pulse/pan）；"
    "带 data-anim 的元素**自身不要再写 transform 属性**，CSS 会盖掉它，需要位移就外面套一层 <g>。"
    "同理，<text> 上写 fill=\"#fff\" 也**不生效**——字级样式挂在 .dg 的 CSS 规则上，"
    "CSS 盖得过呈现属性；深色形状上要压白字就写 style='fill:#f3ecd9'。"
    "可用的字级类只有 h（图题）/ t（正文）/ s（小注）/ num（等宽数字），别的类名没有样式。"
    "净化是服务端做的，所以不合格会带着原因退回，而不是悄悄画少一块。"
)


def spec_schema() -> dict[str, Any]:
    """``spec`` 这一个参数的 JSON Schema——结构与取值都从本模块的常量出。

    为什么不满足于散文描述（旧版只在 description 里列了一遍）：

    1. **模型读结构比读散文可靠。** 真机与冒烟里这条链路最常见的返工是版式组件名
       编错（``title_card`` / ``quote_card`` 这类不存在的名字）——把清单摆在字段的
       ``enum`` 上，比写在一段话里少一次白跑。
    2. **取值要有机器可读的出处。** 启动一致性检查按「schema 里出现过的字段名与枚举
       取值」判定技能正文里的标识符是不是造工具名；只有散文时，技能引用
       ``dict_entry`` / ``torn_highlight`` 会挂成假告警，而假告警教人忽略检查本身。

    校验仍在 ``validate_spec`` 里做主——它给的是带镜号的中文错误清单，比 JSON Schema
    的通用报错可用；这里的边界（maxLength / maxItems）只是把最贵的那几类错提前到文案里。
    """
    return {
        "type": "object",
        "properties": {
            "style": {"type": "string", "enum": list(STYLES), "default": STYLES[0],
                      "description": "版式模板，当前只实现档案卡一套"},
            **knob_props(),
            "title": {"type": "string", "maxLength": 80,
                      "description": "片子标题（出现在片头与界面，不影响画面排布）"},
            "bgm": {
                "type": "object",
                "description": "背景音乐。给本人素材库/曲库里的 material_id，或 obj: 引用；"
                               "取不到字节会直接失败，不会静默出成无声片",
                "properties": {
                    "ref": {"type": "string",
                            "description": "曲库/附件的 material_id，或 select_BGM 给的 obj: 引用"},
                    "volume": {"type": "number", "default": 0.18,
                               "description": "配乐相对人声的音量，0~0.6"},
                    "duck": {"type": "boolean", "default": True,
                             "description": "有人声时压低配乐"},
                },
            },
            "shots": {
                "type": "array", "minItems": 1, "maxItems": MAX_SHOTS,
                "description": "镜头清单，按播放顺序",
                "items": {
                    "type": "object",
                    "required": ["card", "text"],
                    "properties": {
                        "id": {"type": "string",
                               "description": "镜号，省略则按顺序补 s01/s02…"},
                        "card": {"type": "string", "enum": list(CARD_KINDS),
                                 "default": "archive",
                                 "description": "版式组件。档案系（横屏时按自身尺寸落在画面带里，两侧留纸）："
                                                "plain 只有字幕条 / archive 档案卡 / dict_entry 词条卡 / "
                                                "stamp 印章页 / book 书页 / print 印刷页 / theatre 剧场 / "
                                                "webpage 网页 / silhouette 剪影 / title 封面卡。"
                                                "图示系与自定义（SVG viewBox，横竖都装得下，横屏按画面带高度缩放）："
                                                "flow 流程链 / compare 左右对照 / timeline 时间线 / "
                                                "levels 层级 / chart 柱状折线 / scatter 散点 / orbit 环绕 / "
                                                "custom 自画一张示意图（visual.svg）。清单外的版式是不存在的排版"},
                        "text": {"type": "string", "maxLength": MAX_TEXT_CHARS_WIDE,
                                 "description": f"这一镜要说的一句话。容量按画幅："
                                                f"竖屏 ≤{MAX_TEXT_CHARS} 字、横屏 ≤{MAX_TEXT_CHARS_WIDE} 字"
                                                "（超了请拆镜而不是裁句子）"},
                        "highlight": {"type": "array", "items": {"type": "string"},
                                      "maxItems": MAX_HIGHLIGHTS_PER_SHOT,
                                      "description": "逐词点亮的词，每个都必须是 text 的子串，"
                                                     "每镜 ≤4 个；差一个字画面上就永远不亮"},
                        "label": {"type": "object",
                                  "description": "档案卡的信息栏，如 {\"时间\": \"公元 79 年\", "
                                                 "\"地点\": \"庞贝\"}"},
                        "panel": {"type": "object",
                                  "description": "档案卡右栏数据，如 {\"复制份数\": \"1\"}"},
                        "stamp": {"type": "object",
                                  "description": "红印章两行：{\"zh\": \"尚未存在\", "
                                                 "\"en\": \"NOT YET INVENTED\"}"},
                        "visual": {"type": "object", "description": _VISUAL_DOC},
                        "source": {"type": "object",
                                   "description": "出处 {\"标题\": …, \"链接\": …}。机器不判史实，"
                                                  "它会如实出现在**分镜卡与逐镜账**上（版式不自动排它）；"
                                                  "要让它进画面，写进所选 card 的 visual 字段"
                                                  "（如 archive 的 line3）。"
                                                  "**出处闸**：写成查证过的出处必须对得上本会话的检索账"
                                                  "（web_search / fetch_url 真打开过的页面），"
                                                  "对不上就退回——凭记忆的内容请写「未核实：…」"},
                        "duration_sec": {"type": "number",
                                         "description": "**有旁白时不要写**：镜头长度归真实语音时长，"
                                                        "写了只会被当下限参考（min_duration_sec）"},
                    },
                },
            },
        },
        "required": ["shots"],
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _err(items: list[str], msg: str) -> None:
    items.append(msg)


# ---------------------------------------------------------------------------
# 空壳闸：版式有没有「画得出来」的内容
# ---------------------------------------------------------------------------
#
# 模板不会拒绝空输入——它照画：封面卡没有片名就是一块黑纸板，左右对照没有标题就是
# 两个空框，levels 的 value 写成「极高」就是三条等长短横加一排 0。这些在 ``ok=True``
# 的回包里看不见，只有抽出成片帧才看得见，而那时像素已经烧完了。
#
# 清单里没有 plain / book / theatre / silhouette / custom：前四种靠图形本身撑画面
# （plain 是刻意的留白转场），custom 走 ``art.clean_svg`` 那道闸。

#: 这些字段任一填了就算有内容（否则画面只剩边框）
_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "archive": ("inv", "line1", "line2", "line3"),
    "dict_entry": ("word",),
    "print": ("title",),
    "webpage": ("year", "bars"),
    "stamp": ("zh",),
    "title": ("zh",),
}

#: 数组字段 → 最少项数（项数少于模板能画的，出来就是空图）
_LIST_FIELDS: dict[str, tuple[tuple[str, ...], int]] = {
    "flow": (("steps", "items"), 2),
    "timeline": (("marks", "events"), 2),
    "levels": (("levels", "rows"), 1),
    "chart": (("series", "bars", "data"), 2),
    "scatter": (("points",), 1),
    "orbit": (("parts",), 1),
}

#: 这些版式的数值字段必须是**真数字**：``diagrams._num`` 转换失败会静默按 0 画
_NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "levels": ("value",),
    "chart": ("value",),
    "scatter": ("x", "y"),
}


def _filled(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return value is not None


def _items_of(visual: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        raw = visual.get(key)
        if isinstance(raw, (list, tuple)):
            return list(raw)
    return []


def _number_of(item: Any, key: str) -> float | None:
    """取数值字段；缺字段或非数字 → None（渲染层会把这一项按 0 画）。"""
    if not isinstance(item, dict):
        return None
    for k, v in item.items():
        if str(k).strip().lower() == key:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _check_visual_content(sid: str, card: str, visual: dict[str, Any],
                          errors: list[str], *, label: Any = None,
                          panel: Any = None) -> None:
    hint = "text 只进字幕条，版式画面认的是 visual"
    accepted = m_tpl.ART_VISUAL_KEYS.get(card)
    if accepted is not None:
        # 键名按 strip().lower() 归一：diagrams 取值走的就是这条口径（_field/_num），
        # 闸比取值严会让 "X-Max" 这种写法白挨一次退回。
        dead = sorted(str(k) for k in visual
                      if str(k).strip().lower() not in accepted)
        if dead:
            reads = ("只读 " + "、".join(accepted) if accepted
                     else "不读 visual 的任何键（画面全靠版式自绘）")
            _err(errors, f"{sid}：{card} 卡不读 visual 里的 {'、'.join(dead)}——"
                         f"这张卡{reads}，"
                         f"多写的键不会出现在画面上（真机：archive 卡写 visual.kind/rows，"
                         f"红框里只剩模板默认的 INV. 00000）；{hint}")
    if card in _TEXT_FIELDS:
        keys = _TEXT_FIELDS[card]
        filled = any(_filled(visual.get(k)) for k in keys)
        if not filled and card == "archive":
            # 档案卡的信息栏与右栏也排在画面上，只填了它们不算空壳
            filled = _filled(label) or _filled(panel)
        if not filled:
            _err(errors, f"{sid}：{card} 卡的 visual 里 {'/'.join(keys)} 一个都没填——"
                         f"画面上是一张只有边框的空壳；{hint}")
        return
    if card == "compare":
        for side in ("left", "right"):
            raw = visual.get(side)
            if isinstance(raw, dict):
                ok = _filled(raw.get("title")) or _filled(raw.get("points"))
            else:
                ok = _filled(raw)
            if not ok:
                _err(errors, f"{sid}：compare 卡的 visual.{side} 既没 title 也没 points——"
                             f"画面上是一个空框；{hint}")
        return
    rule = _LIST_FIELDS.get(card)
    if rule is None:
        return
    keys, need = rule
    items = _items_of(visual, keys)
    if len(items) < need:
        _err(errors, f"{sid}：{card} 卡的 visual.{keys[0]} 只有 {len(items)} 项，"
                     f"少于这一版式能画的 {need} 项；{hint}")
        return
    for nk in _NUMERIC_FIELDS.get(card, ()):
        for n, item in enumerate(items, 1):
            if _number_of(item, nk) is None:
                got = item.get(nk) if isinstance(item, dict) else item
                _err(errors, f"{sid}：{card} 卡第 {n} 项的 {nk}={got!r} 不是数字——"
                             f"条子会缩成零、滚动数字会停在 0；单位写进 label 或 unit，"
                             f"值本身只给数字")


def _norm_shot(raw: dict[str, Any], i: int, errors: list[str], *,
               max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    sid = str(raw.get("id") or f"s{i + 1:02d}")
    card = str(raw.get("card") or "archive")
    if card not in CARD_KINDS:
        _err(errors, f"{sid}：card={card!r} 不在版式组件清单里（可用：{', '.join(CARD_KINDS)}）")
        card = "archive"
    text = str(raw.get("text") or "").strip()
    if not text:
        _err(errors, f"{sid}：text 为空——每一镜必须有一句要说的话")
    if len(text) > max_chars:
        _err(errors, f"{sid}：文案 {len(text)} 字 > {max_chars}，这一档画幅的字幕条放不下，请拆镜")

    highlights = [str(h) for h in (raw.get("highlight") or []) if str(h).strip()]
    if len(highlights) > MAX_HIGHLIGHTS_PER_SHOT:
        _err(errors, f"{sid}：高亮词 {len(highlights)} 个 > {MAX_HIGHLIGHTS_PER_SHOT}")
        highlights = highlights[:MAX_HIGHLIGHTS_PER_SHOT]
    for h in highlights:
        if h not in text:
            _err(errors, f"{sid}：高亮词 {h!r} 不是文案的子串，画面上永远亮不起来")

    # 时长：有旁白时它归声音，模型给的一律降级为下限参考
    key = "duration_sec"
    want = raw.get("duration_sec")
    if want is None:
        # 局部改那一路喂回来的是**已归一**的 spec（分镜产物里那份，时长下限叫
        # min_duration_sec）。只认 duration_sec 的读法会把每镜的下限抹掉——
        # 用户改一个数字，别的镜被顺手剪短，那不叫局部改。
        key = "min_duration_sec"
        want = raw.get(key)
    dur = None
    if want is not None:
        try:
            dur = max(0.5, min(float(want), 30.0))
        except (TypeError, ValueError):
            _err(errors, f"{sid}：{key}={want!r} 不是数字")

    label = raw.get("label")
    if label is not None and not isinstance(label, dict):
        _err(errors, f"{sid}：label 必须是对象（时间/地点两个字段）")
        label = None
    panel = raw.get("panel")
    if panel is not None and not isinstance(panel, dict):
        _err(errors, f"{sid}：panel 必须是对象（复制份数/距离两个字段）")
        panel = None
    stamp = raw.get("stamp")
    if stamp is not None and not isinstance(stamp, dict):
        _err(errors, f"{sid}：stamp 必须是对象（中/英两行）")
        stamp = None

    visual = raw.get("visual") if isinstance(raw.get("visual"), dict) else {}
    if card == "custom":
        # 自定义画面走的是净化闸：通过的 SVG 以清洗后的版本入库，
        # 渲染层拿到的永远是这里出去的字节，而不是模型原文。
        clean, svg_errors = m_art.clean_svg(visual.get("svg"))
        for item in svg_errors:
            _err(errors, f"{sid}：{item}")
        visual = {**visual, "svg": clean if not svg_errors else ""}

    _check_visual_content(sid, card, visual, errors, label=label, panel=panel)

    return {
        "id": sid,
        "card": card,
        "text": text,
        "highlight": highlights,
        "label": label or {},
        "panel": panel or {},
        "stamp": stamp or {},
        "visual": visual,
        "source": raw.get("source") if isinstance(raw.get("source"), dict) else {},
        "min_duration_sec": dur,
    }


def validate_spec(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """→ (规范化后的 spec, 错误清单)。错误清单非空即**不可渲染**。

    只把「模型一定会写错、且错了画面必坏」的几件事拦在这里：版式组件是否存在、
    高亮词是否真的在文案里、比例/帧率/音色是否在枚举内、文案是否超出字幕容量、
    以及**所选版式有没有撑得住画面的内容**（``_check_visual_content``——空壳不是
    降级，是一张看不出错的好看纸片，只有抽帧才看得见，而那时像素已经烧完）。
    内容对不对（史实、数字）不在本模块职责内——那是 source 字段与证据账的事。
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["spec 必须是对象"]

    style = str(raw.get("style") or "archive_card")
    if style not in STYLES:
        _err(errors, f"style={style!r} 没有对应模板（已实现：{', '.join(STYLES)}）")
        style = STYLES[0]

    aspect = str(raw.get("aspect") or "portrait")
    if aspect not in ASPECTS:
        _err(errors, f"aspect={aspect!r} 不支持（可用：{', '.join(ASPECTS)}）")
        aspect = "portrait"
    width, height = ASPECTS[aspect]
    # 字幕容量归画幅：横屏那条居中横排一行更宽，硬套竖屏的 60 字只会把片子拆碎
    max_chars = MAX_TEXT_CHARS_WIDE if width > height else MAX_TEXT_CHARS

    try:
        fps = int(raw.get("fps") or 15)
    except (TypeError, ValueError):
        fps = 15
        _err(errors, "fps 不是整数")
    if fps not in FPS_CHOICES:
        _err(errors, f"fps={fps} 不在白名单 {FPS_CHOICES}（逐帧截图按帧计费，不给连续值）")
        fps = 15

    narration = bool(raw.get("narration", True))
    voice = str(raw.get("voice") or "zh-CN-YunjianNeural")
    rate = str(raw.get("rate") or "+0%")
    if narration:
        if voice not in VOICES and not _VOICE_RE.match(voice):
            _err(errors, f"voice={voice!r} 不是合法音色名")
        if not _RATE_RE.match(rate):
            _err(errors, f"rate={rate!r} 要写成 +10% / -20% 这种形式")
        else:
            pct = int(rate[:-1])
            if not -50 <= pct <= 50:
                _err(errors, f"rate={rate} 超出 ±50%")

    sub_mode = str(raw.get("subtitle_mode") or "torn_highlight")
    if sub_mode not in SUBTITLE_MODES:
        _err(errors, f"subtitle_mode={sub_mode!r} 不支持（可用：{', '.join(SUBTITLE_MODES)}）")
        sub_mode = "torn_highlight"

    raw_shots = raw.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        _err(errors, "shots 为空——没有镜头就没有片子")
        raw_shots = []
    if len(raw_shots) > MAX_SHOTS:
        _err(errors, f"镜头数 {len(raw_shots)} > {MAX_SHOTS}，超出单条片子上限")
        raw_shots = raw_shots[:MAX_SHOTS]

    shots = [_norm_shot(s, i, errors, max_chars=max_chars)
             for i, s in enumerate(raw_shots) if isinstance(s, dict)]
    if len(shots) != len(raw_shots):
        _err(errors, "有镜头不是对象，已丢弃")

    bgm = raw.get("bgm") if isinstance(raw.get("bgm"), dict) else {}
    try:
        bgm_volume = float(bgm.get("volume", 0.18))
    except (TypeError, ValueError):
        bgm_volume = 0.18
    bgm_volume = max(0.0, min(bgm_volume, 0.6))

    spec = {
        "style": style,
        "aspect": aspect,
        "width": width,
        "height": height,
        "fps": fps,
        "narration": narration,
        "voice": voice,
        "rate": rate,
        "subtitle_mode": sub_mode,
        "shots": shots,
        "bgm": {"ref": str(bgm.get("ref") or ""), "volume": bgm_volume,
                "duck": bool(bgm.get("duck", True))},
        "title": str(raw.get("title") or "")[:80],
    }
    return spec, errors


def estimate_sec(spec: dict[str, Any]) -> tuple[float, float]:
    """按字数估总时长区间（低, 高），用于出片前判断是否偏离目标。

    这是**估算**，只用来在计划阶段提示「比目标长一倍，删几镜吧」；真正落地的每镜
    时长由 TTS 实测回填。无旁白时按阅读速度（每字 0.16 秒 + 0.8 秒驻留）另算。
    """
    shots = spec.get("shots") or []
    chars = sum(len(str(s.get("text") or "")) for s in shots)
    if spec.get("narration"):
        rate_pct = 0
        try:
            rate_pct = int(str(spec.get("rate") or "+0%")[:-1])
        except ValueError:
            rate_pct = 0
        speed = 1.0 + rate_pct / 100.0          # 语速越快，秒数越少
        factor = max(0.4, speed)
        low = chars * SEC_PER_CHAR_LOW / factor + 0.6 * len(shots)
        high = chars * SEC_PER_CHAR_HIGH / factor + 1.2 * len(shots)
    else:
        low = chars * 0.16 + 0.8 * len(shots)
        high = chars * 0.24 + 1.4 * len(shots)
    return round(low, 1), round(high, 1)


def check_target(spec: dict[str, Any], target_sec: float | None) -> list[str]:
    """目标时长对账：只出提醒，不硬拒——删镜还是加速是人的决定。"""
    if not target_sec or target_sec <= 0:
        return []
    low, high = estimate_sec(spec)
    if high < target_sec * 0.75:
        return [f"按当前文案估算只有 {low:.0f}~{high:.0f} 秒，比目标 {target_sec:.0f} 秒短不少："
                f"加镜或放慢语速"]
    if low > target_sec * 1.35:
        return [f"按当前文案估算 {low:.0f}~{high:.0f} 秒，比目标 {target_sec:.0f} 秒长不少："
                f"删镜或缩短每镜文案（当前 {len(spec.get('shots') or [])} 镜）"]
    return []
