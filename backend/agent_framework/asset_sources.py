# -*- coding: utf-8 -*-
"""素材供给面的**唯一注册表**（R11-T50/§12-P36/㉔）：schema 里能出现的源名从这里长出来。

**为什么必须有这个文件。** 在这之前，「有哪些素材源」这件事在仓库里写了**三份**：
`scripts/asset_ingest.py` 的 `SOURCES`（5 个）、`scripts/probe_asset_sources.py` 的
`PROBES`（9 个）、`storyline_server/nodes/asset_nodes.py` 里 `source` 那一栏的**手写
描述**（8 个）。三份互不相等，于是模型看得到 8 个源、命令行走得到 5 个、活库里真有的
是 2 个——模型照着 schema 说「我要 humaaans」，检索回空，它只会得出「这库不行」。
这不是「文档过期」，是**供给面对模型撒谎**（P36 说的违反形态就是这一条）。

**这个文件的形状。** 一行 = 一条腿（一个源），每行必须回答四件事：**怎么取**
（`ingest`）、**能不能让模型点**（`state` → `declared`）、**许可姿态**（署名要不要、
是 CC0 还是 PD）、**为什么是这个决定**（`decision`，编号决定 Q108/Q109/Q110）。
四件事写在**同一行**上，是因为它们只在「这条腿到底成不成」这一个判断里同时成立：
把「能不能声明」挪到 schema 那边去写，就又是第二份真相。

**谁读它。** 入库脚本（`--source` 的取值域）、探测脚本（探哪些端点）、检索节点的
`schema`（枚举与描述）、㉔ 那道闸（schema ⊆ 注册表 ∩ 活库有行）。**一处改，五处跟着动。**

**允许来源（硬约束，§5.2）**：只有 CC0 / 公有领域。署名制（CC-BY 一类）要收就必须先
证明「库里那一栏存在**且**入库真写了值」——见 `humaaans` 那行的 `decision`（Q108），
**缺一即摘除**，不许先把源名放进 schema 再补栏位。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 素材在**片子里的角色**（R11-T53/Q114）。五档，与「格子」一一对应：
#: icon 一枚符号、part 拼图部件、scene 一整幅画面、texture 质感底、bg 铺满屏的底图。
#: 空串 = 机器没标出来（**未判**，不是「没有角色」）——空串不进检索过滤分母，
#: 但会在回执里单列一个数（见 `role_facts`）。
ASSET_ROLES: tuple[str, ...] = ("icon", "part", "scene", "texture", "bg")

#: 腿的状态。**只有 `live` 会被写进 schema**，其余三档各自是一种「今天不给模型点」的理由：
STATE_LIVE = "live"            # 腿跑过、活库有行 → 可以让模型声明
STATE_PENDING = "pending-leg"  # 腿写好了、没跑过（T52）→ 声明了必空手而归，先不给
STATE_OFFLINE = "offline-only"  # 只走 `--offline-dir` 那条取件路（zip 分发），无在线腿
STATE_OUT = "out-of-scope"     # 射程外：本卷明确不做（不是「禁止」，见 Q110）

#: 能出现在 schema 里的状态集合。**放在这里而不是各调用点各判一次**：
#: ㉔ 的①那一半（schema ⊆ 注册表）与「哪些进枚举」必须是同一个判断。
DECLARABLE_STATES = (STATE_LIVE,)


@dataclass(frozen=True)
class SourceLeg:
    """一条供给腿。字段顺序 = 回答问题的顺序：叫什么 → 怎么取 → 能不能点 → 许可 → 为什么。"""

    name: str
    ingest: str                     # `scripts/asset_ingest.py` 里的入库函数名（"" = 没有在线腿）
    state: str
    license_posture: str            # 人话：许可是什么、要不要署名
    attribution_required: bool
    decision: str                   # 编号决定与理由（Q108/Q109/Q110…）
    probe: tuple[tuple[str, str], ...] = ()   # ((端点名, URL), …)，探测脚本按这个探
    subject_hints: tuple[str, ...] = ()       # 这条腿产出的 `subject` 取值
    role_hint: str = ""                       # 这条腿默认落哪个 `asset_role`
    license_url: str = ""

    @property
    def declared(self) -> bool:
        """**这一行今天能不能出现在 schema 里**。唯一判据是状态，不是调用方的心情。"""
        return self.state in DECLARABLE_STATES


# ---------------------------------------------------------------------------
# 探测回执（T51 的输入：判「收不收」之前先看这个源通不通）
# ---------------------------------------------------------------------------
# 2026-10-13 两个位置各一次，回执落在 `assets/receipts/`：
#
#   宿主 `probe_host.json`：8/8 可达（注册表里的 8 条，quaternius 已按 Q110 摘出清单）
#   容器 `probe_container.json`：**两次读数不一样**——第一次 iconify/npm 报
#     `RemoteDisconnected`（0 字节），第二次 9/9 全通。两次都留档，因为「一次失败」
#     本身是有用的读数（npm 那条腿在容器侧会偶发断连，重试一遍就好），
#     掐掉第一次才是把事实修好看。
#
# 活库那一半（㉔ 的第②半）2026-10-13 在真机上读过一次（`creation-pg`，只读 SELECT）：
#
#   iconify 98 行（subject=icon / style_family=icon，宽度 12~2048）
#   wikimedia-commons 34 行（animal 20 + portrait 14，都是 800px 的栅格）
#   其余 7 条腿 0 行
#
# 于是今天**声明为 live 的两个源都有行**（㉔ ②在这一天是绿的，不是靠删断言的绿），
# 另外 7 条各自因为「腿没跑 / 只走离线包 / 射程外」而不在枚举里。
# 这 132 行也正是 T53 要标角色的全部输入：按下面的规则推，98 行是 icon、34 行是 scene
# （800px < 1600 铺不满屏），**part 档 0 件**——这条空清单本身就是证据：
# 缺的是采购（库里根本没有拼图部件），不是标签口径没定。
#
# **这批读数判不了「许可」**（T51 明说：许可以**随包声明**为准，不许照抄印象）。
# 下面的 `license_posture` 一栏因此只承诺「入库时怎么核」，不承诺「站上说它是什么」：
# wikimedia/iconify 是**逐包读声明**（extmetadata / SPDX），polyhaven/ambientcg/open-peeps
# 目前是**写死 CC0**（它们的在线腿里没有读许可那一步）——那三条腿真跑之前，
# 这一栏只是一句话，**入库回执里的许可值才是事实**。这是 T51 留在这里的一条未决项。
ASSET_SOURCES: tuple[SourceLeg, ...] = (
    # 排在前面的两条是**活库今天真有行**的（§12 验收口径①：`origin='library'` > 0 靠这两条）；
    # 中间三条腿写好了、没跑过（T52）；后面三条各有一种「为什么现在不给模型点」。
    SourceLeg(
        name="wikimedia-commons", ingest="ingest_wikimedia", state=STATE_LIVE,
        license_posture="公有领域（PD / PD-US / CC0），入库前逐张核对许可与 AttributionRequired",
        attribution_required=False,
        decision="Q107：已有在线腿且活库有行，保留在 schema。许可由 MediaWiki extmetadata "
                 "逐张核（PD_LICENSES 白名单），署名制的一律不收——「收进来再补署名」等于入库即违约。",
        probe=(("api", "https://commons.wikimedia.org/w/api.php?action=query&list="
                       "categorymembers&cmtitle=Category:Photographs&cmtype=file&format=json&cmlimit=1"),
               ("file", "https://commons.wikimedia.org/wiki/Special:FilePath/Example.jpg?width=800")),
        subject_hints=("portrait", "animal", "object", "map", "building", "plant", "photo"),
        role_hint="scene", license_url="https://commons.wikimedia.org/wiki/Commons:Licensing",
    ),
    SourceLeg(
        name="iconify", ingest="ingest_iconify", state=STATE_LIVE,
        license_posture="逐集合读 SPDX，只收 ALLOWED_SPDX 里那几种（MIT/Apache-2.0/CC0/BSD/ISC）",
        attribution_required=False,
        decision="Q107：已有在线腿且活库有行，保留在 schema。SPDX 过滤在集合粒度、"
                 "跟着集合许可走——图标集里混一个 CC-BY 的图标，整集被挡在外面。",
        probe=(("npm", "https://registry.npmjs.org/@iconify/json/latest"),),
        subject_hints=("icon",), role_hint="icon",
        license_url="https://iconify.design/docs/icons/",
    ),
    SourceLeg(
        name="polyhaven", ingest="ingest_polyhaven", state=STATE_PENDING,
        license_posture="CC0（全站）",
        attribution_required=False,
        decision="Q107/T52：腿已写（取 API 列表 + thumbnail），**没跑过**→ 活库 0 行。"
                 "声明了却拿不到字节，对模型就是撒谎，所以先不进 schema；T52 跑完这条腿"
                 "（或 T52 记未跑、把行数写进回执）再把它翻成 live。不摘除，因为它的"
                 "问题只是「没跑」而不是「取不到」。",
        probe=(("api", "https://api.polyhaven.com/assets"),),
        subject_hints=("3d-model",), role_hint="scene",
        license_url="https://polyhaven.com/license",
    ),
    SourceLeg(
        name="ambientcg", ingest="ingest_ambientcg", state=STATE_PENDING,
        license_posture="CC0（全站）",
        attribution_required=False,
        decision="Q107/T52：同 polyhaven，腿已写、没跑过 → 先不进 schema。它的产物是"
                 "**质感底**（`asset_role='texture'`），刚好是部件档与背景档都要用的那一格，"
                 "所以 T52 优先跑它。",
        probe=(("index", "https://ambientcg.com/get?category=2-Pass&format=jpg&size=2048"),),
        subject_hints=("texture",), role_hint="texture",
        license_url="https://ambientcg.com/license",
    ),
    SourceLeg(
        name="open-peeps", ingest="ingest_openpeeps", state=STATE_PENDING,
        license_posture="CC0（全站），人物手绘 SVG",
        attribution_required=False,
        decision="Q107/T52：腿已写（抓站内 .svg 链接）、没跑过 → 先不进 schema。"
                 "它是**部件档**（`asset_role='part'`）唯一的现成来源，T52 三腿里它的优先级"
                 "与 ambientcg 并列第一。",
        probe=(("site", "https://openpeeps.com"),),
        subject_hints=("person",), role_hint="part",
        license_url="https://openpeeps.com",
    ),
    SourceLeg(
        name="humaaans", ingest="", state=STATE_PENDING,
        license_posture="署名制（非 CC0）：CC-BY 一类，**必须**带署名才能用",
        attribution_required=True,
        decision="Q108：**收，但有前置**。前置两条——① 库里要有安放许可/署名的栏位；"
                 "② 入库真写了值。今天 ① 成立（`asset_library.license` + "
                 "`attribution_required` + `license_url` 三栏都在，见 storage/schema.sql），"
                 "② **证不了**（没有腿、没有一份入库过的样本可查），而 §5.2 的硬约束是"
                 "「需署名的不进检索结果」——`AssetLibraryRepo.search()` 那一行 "
                 "`where['attribution_required']=False` 是同一件事的另一半。"
                 "② 未证 → 按 Q108 的「缺一即摘除」处理：**先不进 schema**，"
                 "等它的取件包 + 署名落在回执里再翻 live。摘除写在**这里**（有编号、有理由），"
                 "不是为了让哪条断言闭嘴。"
                 "T51 的探测回执：2026-10-13 宿主与容器两次都 200/8192B——**它通**，"
                 "所以「不收」的理由是许可前置未证，不是取不到。"
                 "另一条硬事实：`AssetLibraryRepo.search()` 只返回 "
                 "`attribution_required=False` 的行，所以按今天的口径，署名制素材"
                 "**入了库也搜不到**——真要收它，得先有一条「署名随件回执」的通道，"
                 "那是另一件事，本卷不做。",
        probe=(("site", "https://humaaans.com"),),
        subject_hints=("person",), role_hint="part",
        license_url="https://humaaans.com",
    ),
    SourceLeg(
        name="kenney", ingest="", state=STATE_OFFLINE,
        license_posture="CC0（站内资源包），zip 分发",
        attribution_required=False,
        decision="Q109：**走 `ingest_offline`**。它按整包 zip 发，在线批量取不成立（一条腿"
                 "要能把「取哪个文件」写成 URL，这里只能先下 zip 再挑），而离线那条路"
                 "（`asset_ingest.py --offline-dir`，manifest.jsonl 驱动）**今天就是现成的**。"
                 "所以它的 `ingest` 留空、`state=offline-only`：不进 schema（包没放进来之前"
                 "点了也是空的），但保留在探测清单里（可达性读数照收，那是「将来能不能补"
                 "自动腿」的依据）。"
                 "T51 的探测回执：2026-10-13 宿主与容器两次都 200——站是通的，缺的只是"
                 "**那份取件包**（zip 分发这条路没有自动入口，正是 Q109 说的那件事）。",
        probe=(("site", "https://kenney.nl"),),
        subject_hints=("icon", "texture"), role_hint="",
        license_url="https://kenney.nl/assets",
    ),
    SourceLeg(
        name="quaternius", ingest="", state=STATE_OUT,
        license_posture="CC0（站内包），glTF/.glb 场景包",
        attribution_required=False,
        decision="Q110：**本卷不收，登记为「射程外·未做」，不是「禁止」**，理由在资产形态上"
                 "而不在画法上（`three` 是合法画法通道，原稿写的「3D 没有格子」是错的）："
                 "① 现有导出形态只有 SVG 与栅格 PNG，glTF 是二进制场景包，收它等于为 R11 "
                 "再造第三种资产形态；② T54 的 `scale_safe` 定义为「最细笔画实测 ≥2 px」，"
                 "对 `.glb` **无定义**——没有这条实测，部件档就退化成人工声明。"
                 "`SUBJECT_CHANNELS` 里的 `three` 通道本卷一根手指不动。"
                 "**从探测清单摘除**（Q109 那条规矩的另一半）：射程外的源不必再量它通不通。"
                 "宿主那一次探测是在摘除**之前**跑的，回执里 `quaternius/site` 那一条"
                 "（200/8192B）留着不动——**摘除之后不再探**，但那一次读数不必抹掉："
                 "它是「站是通的、不做是因为形态」这句话的证据。",
        probe=(),
        subject_hints=(), role_hint="",
        license_url="https://quaternius.com",
    ),
)


# ---------------------------------------------------------------------------
# 派生视图（调用方一律走这些函数，别自己去翻 ASSET_SOURCES）
# ---------------------------------------------------------------------------

def source_names() -> tuple[str, ...]:
    """注册表里的全部源名（= 入库脚本 `--source` 的取值域）。"""
    return tuple(leg.name for leg in ASSET_SOURCES)


def declared_sources() -> tuple[str, ...]:
    """**今天可以让模型声明的源**（= schema 里 `source` 那一栏的枚举）。"""
    return tuple(leg.name for leg in ASSET_SOURCES if leg.declared)


def ingest_names() -> tuple[str, ...]:
    """有在线腿的源名（= 入库脚本真能跑的那些）。"""
    return tuple(leg.name for leg in ASSET_SOURCES if leg.ingest)


def leg(name: str) -> SourceLeg | None:
    for item in ASSET_SOURCES:
        if item.name == name:
            return item
    return None


def probe_entries() -> list[tuple[str, str, str]]:
    """探测清单（源名 / 端点名 / URL），**由注册表生成**。

    与 schema 无关：探测量的是「这个源今天通不通」，与「模型能不能点它」是两件事——
    没跑过的腿照样要探（它正是 T52 的输入），射程外的源不探（量它没有用途）。
    """
    return [(l.name, ep, url) for l in ASSET_SOURCES for ep, url in l.probe]


#: T50 的验收口径（原文）：「工具描述里出现的源名**全部**可在活库检索到 ≥1 行」。
#: 所以描述里**只出现今天真能检索到行的源名**——不可用的那几条在描述里**不点名**，
#: 它们的名字出现在枚举之外就是撒谎（模型会照着描述去点它）。模型怎么知道有别的源？
#: 靠**回执**（Q111 的三态）：写了枚举外的源名 → `source_not_available` 那一条会
#: 逐字告诉它「这个源今天不在可声明集合里，可用的是这几个」。**静态描述只写真的，
#: 动态回执负责解释。** 这一分工正是 `check_description_names` 守的东西。
def source_spec() -> dict[str, Any]:
    """`search_illustration` 的 `source` 那一栏的 **schema 形状（由注册表生成）**。"""
    live = list(declared_sources())
    return {
        "type": "string", "enum": live,
        "description": (
            f"素材源，取值只能是枚举里的这几个（= 今天**真能在库里检索到行**的源，"
            f"由注册表生成）：{'/'.join(live)}。不传＝在这些源里一起搜。"
            "**枚举里没有的源名不要写**——它们要么腿还没跑通、要么只走离线取件包、"
            "要么在射程外，今天点不到。写了会收到 `reason=source_not_available`，"
            "那**不是**「库里没有这类素材」。三态回执读 `reason`："
            "`hit` 命中 / `no_match` 库内确无此类目 / `source_not_available` 该源不在"
            "可声明集合里（只有这一态才该换源，前两态该换关键词）。"),
    }


# ---------------------------------------------------------------------------
# T53/Q114：`asset_role` 的**机器初值**（人只做抽检，不先标 500 件）
# ---------------------------------------------------------------------------
# 「先人工标一遍再上线」被 Q114 明确否决：那是把一个闸的成本转移成一轮永远做不完的作业。
# 所以这里只用既有栏位（subject / style_family / mime / 长边）推，**规则全在这一个函数里**，
# 判不出来就留空（= 未判），不许「猜一个最像的档」——猜错的那一档会进检索过滤器，
# 表现是「明明有素材却搜不到」，比空着难查得多。

#: 质感底的长边下限（像素）。不是审美：整屏铺底要从分辨率里来，低于这个数的图
#: 铺满后必然被插值糊掉，而「糊」正是 T54 那一格要拦的东西。
_BG_MIN_LONG_SIDE = 1600


def asset_role_of(*, subject: str = "", style_family: str = "",
                  mime: str = "", ext: str = "",
                  width: int | None = None, height: int | None = None) -> tuple[str, str]:
    """→ (角色, **判据原话**)。角色为空串＝机器认不出（未判），判据原话照样要写。

    顺序有意图：越靠前的越具体（先说「这就是个图标」，最后才按尺寸分 scene/bg）。
    """
    sub = (subject or "").strip().lower()
    fam = (style_family or "").strip().lower()
    mime_l = (mime or "").strip().lower()
    ext_l = (ext or "").strip().lower().lstrip(".")
    is_vector = mime_l == "image/svg+xml" or ext_l == "svg"

    if sub == "icon" or fam == "icon":
        return "icon", f"subject/style_family 标了 icon（subject={sub or '-'}，family={fam or '-'}）"
    if sub == "texture" or fam == "texture":
        return "texture", f"subject/style_family 标了 texture（subject={sub or '-'}，family={fam or '-'}）"
    if fam == "illustration":
        return "part", f"style_family=illustration：整幅手绘插图，进片子时是**部件**（可拆可挪）"
    if is_vector and fam != "photo":
        return "part", "矢量图（SVG）且不属于上面几档：一幅可缩放的图形，按部件档收"
    if fam in ("photo", "3d") or mime_l.startswith("image/"):
        long_side = max(int(width or 0), int(height or 0))
        if long_side >= _BG_MIN_LONG_SIDE:
            return "bg", (f"栅格图长边 {long_side}px ≥ {_BG_MIN_LONG_SIDE}：够铺满屏当底图"
                          "（低于这个数铺上去要放大插值，会被 T54 那格拦）")
        if long_side:
            return "scene", f"栅格图长边 {long_side}px < {_BG_MIN_LONG_SIDE}：够做一整幅画面，不够铺底"
        return "scene", "栅格图但库行里没有尺寸——按「一整幅画面」收，尺寸限制留给 T54 实测"
    return "", ("机器认不出这一行的角色：subject/style_family/mime 全是空的或者都不在规则表里，"
                "按未判处理（不猜档——猜错会进检索过滤器，表现成「明明有素材却搜不到」）")


def role_facts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一批库行 → `asset_role` 的账（几档各几行、几行未判）。检索回执与巡检脚本共用。"""
    counts = {r: 0 for r in ASSET_ROLES}
    undetermined = 0
    for row in rows:
        got = str(row.get("asset_role") or "")
        if got in counts:
            counts[got] += 1
        else:
            undetermined += 1
    return {"counts": counts, "undetermined": undetermined, "total": len(rows)}


# ---------------------------------------------------------------------------
# ㉔ 供给面契约（schema ⊆ 注册表 ∩ 活库有行）
# ---------------------------------------------------------------------------

def check_schema_subset(schema_sources: Sequence[str]) -> tuple[list[str], list[str]]:
    """㉔ 第①半：**schema 里的源名必须全部来自注册表，且都是可声明的那几个**。

    → (打回清单, 未判清单)。这一半**不碰数据库**，所以它两向都能判死：
    多一个名字（手写的、注册表外的）＝红；少一个（声明集合里有源没进枚举）＝红。
    `schema_sources` 由调用方从**真发出的** schema 里取（节点 `input_schema` 的 enum），
    不是把注册表再传一遍——传注册表等于自己跟自己比，永远绿。
    """
    errors: list[str] = []
    undetermined: list[str] = []
    got = [str(s) for s in schema_sources]
    known = set(source_names())
    allowed = set(declared_sources())
    # **一个源一条**：合成一句「有 3 个源不对」在回执上就分不出是哪 3 个，
    # 而修的动作恰恰是逐源做的（跑腿 / 摘除 / 补进枚举）——修的人需要点名。
    for s in got:
        if s not in known:
            errors.append(
                f"㉔ 供给面：schema 里的源 {s} 不在注册表里——供给面必须由注册表生成"
                f"（P36），不许手写。schema 里每个名字都要能在 "
                f"agent_framework/asset_sources.py 里找到对应的腿，"
                f"否则它就是一个「模型能点、代码没腿」的源")
        elif s not in allowed:
            l = leg(s)
            errors.append(
                f"㉔ 供给面：schema 里的源 {s} 今天**未声明**（状态 {l.state if l else '?'}）——"
                "模型点了必空手而归。要么把腿跑起来（T52）把状态翻成 live，"
                "要么把它从枚举里去掉")
    for s in declared_sources():
        if s not in set(got):
            errors.append(
                f"㉔ 供给面：声明为 live 的源 {s} 没进 schema 枚举——库里明明有行、"
                f"模型却点不到，等于供给面少了一条腿（与「多一个名字」是同一个契约的两半，"
                f"两半都判死）")
    if not got:
        undetermined.append("㉔ 供给面：schema 里一个源名都没读到（枚举为空或缺 `source` 栏）"
                            "——这一条今天判不了")
    return errors, undetermined


def check_registry_live(live_counts: Mapping[str, int] | None) -> tuple[list[str], list[str]]:
    """㉔ 第②半：**每个声明为 live 的源必须在活库里有行**。

    `live_counts`：源名 → 活库（`asset_library`，`enabled=true`）行数。
    **`None` = 读不到活库 → 未判，不是过**（D-未判不绿）：库连不上时「0 行」与
    「问不出来」是两件事，把后者报成前者会让一条腿被冤枉摘除。
    """
    errors: list[str] = []
    undetermined: list[str] = []
    if live_counts is None:
        return [], [f"㉔ 供给面：活库读不到（{len(declared_sources())} 个声明源的行数都没量到）"
                    "——「腿通不通」这一半今天记未判，不是过"]
    for name in declared_sources():
        n = int(live_counts.get(name, 0))
        if n <= 0:
            l = leg(name)
            errors.append(
                f"㉔ 供给面：源 {name} 声明为 live（schema 里可选），活库里却有 0 行——"
                f"模型点了它必然空手而归。{l.decision if l else ''}"
                "先跑这条腿入库，或把它的状态改回 pending-leg 再重生成 schema")
    # 库里**没声明**的源行数不作判据：那条腿大概正在跑（T52），schema 已经没给模型点了。
    return errors, undetermined


def check_leg_exists() -> tuple[list[str], list[str]]:
    """㉔ 第③半：**声明的源必须有腿、许可姿态必须写明**。

    第①半只保证「schema 里的名字在注册表里」——注册表里仍然可以有一行既没有 `ingest`
    又没有离线路径的活源（今天没有，但 schema 生成之后这种行会**静默**地变成一个
    「点得到、取不到」的源）。这一半就是那个口子的守门人，纯静态、不碰库。
    """
    errors: list[str] = []
    for l in ASSET_SOURCES:
        if not l.declared:
            continue
        if not l.ingest and l.state != STATE_OFFLINE:
            errors.append(f"㉔ 供给面：源 {l.name} 声明为 live 却没有取件腿（`ingest` 是空的）——"
                          "schema 会把它发给模型，而代码里没人能取到它的字节")
        if not l.license_posture:
            errors.append(f"㉔ 供给面：源 {l.name} 没有写许可姿态——§5.2 要求每行素材带许可，"
                          "而源这一级说不清许可就没法保证下面每一行都带得住")
    return errors, []


async def live_source_counts(storage: Any) -> dict[str, int] | None:
    """活库里每个源有多少行（`enabled=true`）。读不到返回 `None`（＝未判）。

    **不抛异常**：这是一条闸的读数，不是业务路径。库连不上时闸要记「未判」并把
    读不到这件事写进回执，而不是把整个巡检打成失败——失败与未判是两种状态（§0 的口径）。
    """
    if storage is None:
        return None
    try:
        rows = await storage.assets.list_assets(enabled=True, limit=100000)
    except Exception:      # noqa: BLE001
        return None
    counts: dict[str, int] = {}
    for r in rows or ():
        name = str(r.get("source") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def check_description_names(description: str,
                            live_counts: Mapping[str, int] | None) -> tuple[list[str], list[str]]:
    """T50 的验收断言：**工具描述里出现的源名，必须条条都能在活库检索到 ≥1 行**。

    判据是「在描述文本里找到注册表里的源名」（子串匹配就够：源名是 `polyhaven` 这种
    不会撞车的词），然后逐个要求：① 它在**可声明集合**里；② 活库里它的行数 ≥ 1。
    ①不成立＝描述在替一条没开通的腿做广告（模型会照着点）；②不成立＝描述说得比库里的
    事实大（这正是「供给面撒谎」的原形）。**这一条读的就是模型真看到的那段文字**，
    不是注册表——拿注册表自己跟自己比永远绿。
    """
    errors: list[str] = []
    undetermined: list[str] = []
    text = description or ""
    hits = [l.name for l in ASSET_SOURCES if l.name in text]
    if not hits:
        return ([], ["T50 描述对账：工具描述里一个源名都没出现——这一条今天判不了"])
    for name in hits:
        if name not in declared_sources():
            errors.append(
                f"T50 供给面：工具描述里点到了源 {name}，而它今天**未声明**"
                f"（状态 {leg(name).state if leg(name) else '?'}）——"
                "描述里出现的每个源名都必须是能检索到行的；枚举之外的源名只该出现在"
                "**回执**（Q111 的三态）里，不该出现在静态描述里")
            continue
        if live_counts is None:
            undetermined.append(f"T50 供给面：源 {name} 的真源名出现在描述里，但活库读不到，"
                                "「它有 ≥1 行」这一半记未判")
            continue
        if int(live_counts.get(name, 0)) <= 0:
            errors.append(
                f"T50 供给面：工具描述里点到了源 {name}，活库里它却有 0 行——"
                "描述比库里的事实大，模型会照着去点一个必然空的源")
    return errors, undetermined


def _node_source_spec() -> dict[str, Any]:
    """从**真的**检索节点 schema 里读出 `source` 那一栏（枚举 + 描述）。

    `agent_framework` 在分层里在下面，`storyline_server` 在上面，所以这里是**函数内**
    的延迟导入（模块级导入会成环，而这一个读数只在巡检/闸里要）——读不到就返回空表，
    由 :func:`check_schema_subset` 记未判，**不假装通过**。
    """
    try:
        from storyline_server.nodes.asset_nodes import SearchIllustrationNode
    except Exception:       # noqa: BLE001
        return {}
    spec = (getattr(SearchIllustrationNode, "input_schema", {}) or {}).get("properties", {})
    got = spec.get("source")
    return dict(got) if isinstance(got, dict) else {}


def _schema_sources_from_node() -> list[str]:
    enum = _node_source_spec().get("enum")
    return [str(x) for x in enum] if isinstance(enum, list) else []


async def supply_surface_report(storage: Any = None, *,
                                schema_sources: Sequence[str] | None = None
                                ) -> dict[str, Any]:
    """㉔ 的完整回执：三个半边的结论 + 台账。巡检脚本、测试、出片回执共用一份。

    `schema_sources` 不传就从**真的** `search_illustration` 节点 schema 里取
    （`asset_nodes.SearchIllustrationNode.input_schema`）——这是刻意的：闸要判的是
    「模型看到的枚举」，不是「注册表说应该有谁」。
    """
    if schema_sources is None:
        schema_sources = _schema_sources_from_node()
    description = str(_node_source_spec().get("description") or "")
    counts = await live_source_counts(storage)
    e1, u1 = check_schema_subset(schema_sources)
    e2, u2 = check_registry_live(counts)
    e3, u3 = check_leg_exists()
    e4, u4 = check_description_names(description, counts)
    errors = e1 + e2 + e3 + e4
    undetermined = u1 + u2 + u3 + u4
    return {
        "gate": "24",
        "schema_sources": list(schema_sources),
        "declared_sources": list(declared_sources()),
        "registered_sources": list(source_names()),
        "live_counts": counts,
        "description": description,
        "errors": errors,
        "undetermined": undetermined,
        "ok": not errors and not undetermined,
    }
