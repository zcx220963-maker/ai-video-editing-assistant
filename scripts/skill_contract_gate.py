"""R11-T57 巡检：技能正文教给模型的那些**名字**，在 schema 侧是不是真存在。

**这条闸为什么必须存在（而不是「写完肉眼看一遍」）。** T36 已经吃过一次这个亏：
正文里教了 `subject_terms`、教了「纯文字镜不得连续 3 镜」，而 schema 里既没有那一栏、
也没有那条判据——把没实现的规矩教成会被拦的规矩，比不教更坏（模型会照着不存在的合同
自报，执行轮的 schema 又会把它退回来，两边各说各话，§9-Q106 ④⑤）。R11-T57 新加的两节
（`asset-parts` / `scale-safe`）点名的字段全是这一批新落的（`asset_role`、`scale_safe`），
所以这份巡检判的就是**双向对账**：

① 正文里点名的每个名字 **在 schema 侧存在**（节点入参 / 库列 / 常量，各有各的真相源）；
② schema 侧真有的那几个**在正文里都教到了**（枚举多一档而正文没写 → 红）；
③ 正文里点的数（0.18、194 px、2 px）与常量**同值**——文案会漂，常量不会自己跟着改；
④ 新加的两节**不在默认注入集里**，且只有 `motion + image` 两条腿同时在计划里才发。

第 ④ 条是 T36 立的那条形状（「默认注入集字节不涨」），本卷复用，不另写。

**它不连库、不烧像素**，宿主上就能跑：真相源是本地文件（节点类、schema.sql、
migrations.sql、`asset_scale.py` 常量）与 `SKILL.md` 正文。

用法：
    python scripts/skill_contract_gate.py            # 逐条打 ok/XX
    python scripts/skill_contract_gate.py --json     # 末尾附机器可读的失败清单

退出码：0 全绿 / 1 有不合预期。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _add_paths() -> None:
    """把 `agent_framework` 与 `storyline_server` 放上 import 路径（宿主与容器两种布局）。"""
    for cand in (ROOT / "backend", Path("/app")):
        if (cand / "agent_framework").is_dir():
            sys.path.insert(0, str(cand))
            return


_add_paths()

from agent_framework.asset_scale import (                        # noqa: E402
    CANVAS_SHORT_SIDE, MIN_STROKE_PX, PART_BAND_SHARE)
from agent_framework.asset_sources import ASSET_ROLES            # noqa: E402
from agent_framework.plan.skills import (                        # noqa: E402
    SUPPLY_SECTIONS, sections_for_plan)
from agent_framework.skill import split_sections                 # noqa: E402

SKILL = ROOT / "backend/examples/skills/motion_explainer/SKILL.md"
NODES_PY = ROOT / "backend/storyline_server/nodes/asset_nodes.py"
SCHEMAS = (ROOT / "backend/agent_framework/storage/schema.sql",
           ROOT / "backend/agent_framework/storage/migrations.sql")

#: 新加的那几节（T57 两节 + R12-T62 一节）。巡检只读这几节的正文——别节的账各自有主
#: （T36 那三节）。
NEW_SECTIONS = ("asset-parts", "scale-safe", "whiteboard-imagegen")

#: 回执栏那一类名字的真相源：**节点源码里的字面键**。
#: 这一条机制比别的弱（回执没有 schema 可读），所以如实写在这里：它拦的是「改名忘了
#: 改正文」，拦不了「回执少了一栏」——后者由 `SelectIllustrationNode.process` 自己的
#: 断言与 `scripts/asset_gate.py` 的活库回执管。
RECEIPT_KEYS = ("found", "reason", "note", "asset_id", "scale_note", "sha256")

FAILURES: list[str] = []


def say(ok: bool, msg: str) -> None:
    if not ok:
        FAILURES.append(msg)
    print(f"{'  ok ' if ok else '  XX '} {msg}")


def _body() -> str:
    """SKILL.md → 正文（去掉 frontmatter）。"""
    raw = SKILL.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    return parts[2] if len(parts) >= 3 else raw


def _frontmatter() -> dict[str, str]:
    raw = SKILL.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    out: dict[str, str] = {}
    if len(parts) < 3:
        return out
    for line in parts[1].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _section_text(body: str, name: str) -> str:
    m = re.search(rf"<!-- section: {re.escape(name)} -->(.*?)<!-- /section -->", body, re.S)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# 四组
# ---------------------------------------------------------------------------

def case_declared_vs_found(body: str) -> None:
    """frontmatter `sections:` 的声明 == 正文里真有的分段（两向对不上都是漂移）。"""
    declared = [s.strip() for s in _frontmatter().get("sections", "").split(",") if s.strip()]
    _, found = split_sections(body)
    say(sorted(declared) == sorted(found),
        f"frontmatter 声明的分段与正文里真有的逐名相等（声明 {len(declared)} / 正文 {len(found)}"
        + (f"；差异 {sorted(set(declared) ^ set(found))}" if sorted(declared) != sorted(found) else "")
        + "）")
    for name in NEW_SECTIONS:
        say(name in found, f"`{name}` 是**真的分段块**（能按名单独取到，§5.7 第②条）")


def case_not_in_default(body: str) -> None:
    """④ 新加的两节不在默认注入集里（T36 立的形状）。"""
    base, _ = split_sections(body)
    for name in NEW_SECTIONS:
        got = _section_text(body, name)
        head = next((ln.strip() for ln in got.splitlines() if ln.strip().startswith("#")), "")
        say(bool(head) and head.lstrip("# ").strip() not in base,
            f"`{name}` 的标题**不在默认注入集里**（误挂进默认集 → 默认集字节上涨 → ㉓ 该看见）")


def case_fields_exist(body: str) -> None:
    """① 正文点名的名字，schema 侧存在。"""
    from storyline_server.nodes.asset_nodes import (SearchIllustrationNode,
                                                    SelectIllustrationNode)
    props = SearchIllustrationNode.input_schema.get("properties") or {}
    enum = (props.get("asset_role") or {}).get("enum") or []
    say(list(enum) == list(ASSET_ROLES) and bool(enum),
        f"`asset_role` 是 `search_illustration` 的入参，枚举与 `ASSET_ROLES` 同源"
        f"（{list(enum)}）")
    say("query" in props and "query" in (SearchIllustrationNode.input_schema.get("required") or []),
        "`query` 是 `search_illustration` 的必填入参")
    say("asset_id" in (SelectIllustrationNode.input_schema.get("properties") or {}),
        "`asset_id` 是 `select_illustration` 的入参")
    # 写法上别省括号：`a in b is False` 在 Python 里是**链式比较**（`(a in b) and (b is False)`），
    # 少了括号这句话会永远判假（本闸第一次跑就是这么红的）。
    select_props = SelectIllustrationNode.input_schema.get("properties") or {}
    say("scale_safe" not in select_props,
        "`scale_safe` **不是**入参（它只能是实测产物，T54 的验收就是这条——入参就是人工传值的入口）")

    ddl = "\n".join(p.read_text(encoding="utf-8") for p in SCHEMAS if p.exists())
    say(re.search(r"\bscale_safe\s+boolean", ddl) is not None,
        "`scale_safe` 是 `asset_library` 的真列（boolean，可空＝未判）")
    say(re.search(r"\basset_role\s+text", ddl) is not None,
        "`asset_role` 是 `asset_library` 的真列")

    src = NODES_PY.read_text(encoding="utf-8")
    missing = [k for k in RECEIPT_KEYS if f'"{k}"' not in src]
    say(not missing,
        f"正文点名的回执栏在节点源码里都有（缺 {missing or '无'}）")

    # ② 反向：枚举里有几档，正文就教几档。
    body_roles = set(re.findall(r"`(icon|part|scene|texture|bg)`", body))
    say(body_roles == set(ASSET_ROLES),
        f"`asset_role` 的五档正文一档不落（正文 {sorted(body_roles)} / 常量 {list(ASSET_ROLES)}）")


def case_numbers_match(body: str) -> None:
    """③ 正文里抄的数与常量同值（文案会漂，常量不会自己跟着改）。"""
    share = f"{PART_BAND_SHARE:g}"
    px = round(PART_BAND_SHARE * CANVAS_SHORT_SIDE)
    stroke = f"{MIN_STROKE_PX:g}"
    say(share in body, f"正文写的部件档最小占地（`{share}`）与 `PART_BAND_SHARE` 同值")
    say(str(px) in body, f"正文写的目标像素（`{px} px`）与常量算出来的一致")
    say(f"≥{stroke} px" in body or f"≥ {stroke} px" in body,
        f"正文写的最细笔画判据（`≥{stroke} px`）与 `MIN_STROKE_PX` 同值")
    say(str(CANVAS_SHORT_SIDE) in body,
        f"正文写的画布短边（`{CANVAS_SHORT_SIDE}`）与 `CANVAS_SHORT_SIDE` 同值")


def case_gating(body: str) -> None:
    """④ 通道条件：`motion` 且 `image` 两条腿同时在计划里才发（T57 原文）。"""
    def plan(*nodes: str) -> dict:
        return {"steps": [{"node": n, "params": {}} for n in nodes]}

    both = sections_for_plan(plan("plan_motion", "search_illustration",
                                  "render_motion_code"))
    motion_only = sections_for_plan(plan("plan_motion", "render_motion_code"))
    image_only = sections_for_plan(plan("search_illustration"))
    none = sections_for_plan(plan("plan_timeline"))
    say(set(SUPPLY_SECTIONS) <= set(both),
        f"两条腿都在 → 发 {list(SUPPLY_SECTIONS)}")
    say(not set(SUPPLY_SECTIONS) & set(motion_only),
        f"只有写代码腿 → **不发**（教 `asset_role` 而没有供给腿＝教一条今天调不到的路）：{list(motion_only)}")
    say(not set(SUPPLY_SECTIONS) & set(image_only),
        f"只有供给腿 → **不发**（本轮没人写镜头代码，「一件放大＝一枚徽章」这句话没有读者）：{list(image_only)}")
    say(none == (),
        f"两条都没有 → 空表（`preload_skills` 也不含这份技能，字节数 0）：{list(none)}")


def case_whiteboard(body: str) -> None:
    """R12-T62：出图规范那一节 —— 四条硬规矩在正文里，通道条件只认 `imagegen`。"""
    from agent_framework.plan.skills import WHITEBOARD_SECTIONS

    sec = _section_text(body, "whiteboard-imagegen")
    for want, why in (("#F5EBD7", "纸底色号"), ("纯白", "禁纯白那条令"),
                      ("深灰", "深灰线描"), ("三色", "点缀色上限"),
                      ("零文字", "画面内零文字的写法")):
        say(want in sec, f"出图规范那节点名了{why}（`{want}`）")

    # ④ 的 T62 形状：这一节只认 `imagegen`（出图腿），**不认**素材库那两条腿——
    # 库里现成的件观感不由我们定，把出图规范发给那一轮＝教一条执行不了的路。
    def plan(*nodes: str) -> dict:
        return {"steps": [{"node": n, "params": {}} for n in nodes]}

    has_gen = sections_for_plan(plan("plan_motion", "imagegen", "render_motion_code"))
    lib_only = sections_for_plan(plan("plan_motion", "search_illustration",
                                      "render_motion_code"))
    say(set(WHITEBOARD_SECTIONS) <= set(has_gen),
        f"计划里有出图腿 → 发 {list(WHITEBOARD_SECTIONS)}")
    say(not set(WHITEBOARD_SECTIONS) & set(lib_only),
        f"只有素材库那两条腿（无 `imagegen`）→ **不发**：{list(lib_only)}")

    # 「画面内零文字」这一格今天只有提醒——正文必须把这件事说成**没有判据**，
    # 而不是说成「会被打回」。写反了就等于教模型去防一条不存在的闸（§9-Q119）。
    say("Q119" in sec and "提醒" in sec and "text_share" in sec,
        "正文如实写明：`text_share` 分不清画面内文字与字幕层，这一格今天**只能提醒**"
        "（带 Q119 编号，不是一句含糊话）")

    # 出图那一路的节点名真在注册表里（`prompt` 是它唯一的必填入参——规范要写进去的就是它）。
    from storyline_server.nodes.imagegen_nodes import GenerateImageNode
    props = GenerateImageNode.input_schema.get("properties") or {}
    say(GenerateImageNode.name == "imagegen" and "prompt" in props,
        "`imagegen` 是真节点名，且它的入参里有 `prompt`（规范要写进去的那一栏）")


def run(*, as_json: bool = False) -> int:
    # 判词里带「↔」这类字符：Windows 宿主控制台默认 GBK，直接 print 会 UnicodeEncodeError
    # ——巡检自己炸掉会把「有一条没跑」伪装成「这一条跑过了」。容器里本来就是 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:       # noqa: BLE001 —— 老解释器没有 reconfigure 就算了
        pass
    print("=== R11-T57 / R12-T62 巡检：技能正文 ↔ schema 双向对账 + 默认集不涨 ===")
    body = _body()
    case_declared_vs_found(body)
    case_not_in_default(body)
    case_fields_exist(body)
    case_numbers_match(body)
    case_gating(body)
    case_whiteboard(body)
    if as_json:
        print(json.dumps({"failures": FAILURES}, ensure_ascii=False, indent=2))
    print("结论：" + ("绿（正文点名的都在 schema 里，新节没进默认集，通道条件对得上）"
                     if not FAILURES else f"红（{len(FAILURES)} 条不合预期）"))
    return 1 if FAILURES else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="R11-T57 技能正文 ↔ schema 巡检")
    parser.add_argument("--json", action="store_true", help="末尾附机器可读的失败清单")
    args = parser.parse_args()
    return run(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
