# -*- coding: utf-8 -*-
"""取材轮（T20 / P9）的离线用例：模型「读得到卡、读不到码」这一条要真被堵上。

判据的原话是「注入内容里含被引用 demo 的字节，且 ``load_skill(path=)`` 不截断；
一镜引用 2 个 demo → 上下文里真有那 2 份全文」。所以这一份盯的是**字节**，不是字段名：

1. **全文、逐字节**。取材轮回执里的 ``forage_demos[i]["text"]``，按 UTF-8 编码回去
   必须**逐字节等于盘上那份文件**。比较的对象是**当下读到的文件**，不是
   ``MANIFEST.json`` 里那个 ``bytes`` 数——清单是 LF 语义录的，而 Windows 工作区
   是 CRLF 检出（``core.autocrlf=true``，实测 158 份全部差一个「行数」那么多字节），
   拿清单的数当期望值会让这条判据在开发机上红、在容器里绿，而它两边应该一样。
2. **要么全文要么没有**。超线的那几件进 ``forage_oversize`` 名单，**不是**被切短。
   一份断在中间的 TSX 比不给更坏：模型会顺着断口把剩下半段写完，而闸⑫ 比的正是骨架，
   「半份 + 自己补的另一半」照样能过线。
3. **取材轮不落库**。它确实产出了东西（模型上下文里的源码），但那不是本会话的
   ``motion_spec``；落进去就成了「这一步已经有分镜了」的假象，而出片节点读的正是那一格。
4. **三态分清**。全带码 = 分镜轮、全不带码且有声明 = 取材轮、混着 = 打回。第三档必须打回
   而不是猜：服务端分不出「漏填」与「想先取材」，猜错的代价是整片烧完才现形。

外加两条``load_skill`` 侧的钉子：拿**真** ``SkillLoader``（内存后端 + 真技能目录）走一遍，
最大那份 demo（36 KB）必须一字不少地回来——这是「不截断」唯一有意义的验法，拿小文件验
等于没验；以及**技能名从 frontmatter 读**（它不等于目录名），免得提示里写了个
``load_skill`` 查不到的名字。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.skill import LoadSkillTool, SkillLoader              # noqa: E402
from agent_framework.storage import build_storage                          # noqa: E402
from storyline_server.motion import code_spec as cs                        # noqa: E402
from storyline_server.nodes import motion_plan as mp                       # noqa: E402

_fails = 0
_checks = 0

SKILL_DIR = _BACKEND / "examples" / "skills" / "motion_explainer"
DEMOS = SKILL_DIR / "demos"


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _skeleton(code: str | None = None, **shot) -> dict:
    base = {"id": "s01", "text": "第一镜的文案", "card": "opening/fracture",
            "derived_from": ["opening/fracture/Fracture.tsx"]}
    base.update(shot)
    if code is not None:
        base["code"] = code
    return base


def _disk(rel: str) -> bytes:
    """盘上那份文件的字节（**当前工作区**的，不是 MANIFEST 里记的）。"""
    return (DEMOS / rel).read_bytes()


def main() -> int:
    # ── ① 三态：哪些调用算取材轮 ─────────────────────────────────────────
    print("\n[① 判据只认「有没有 code」这一件事]")
    full = {"shots": [_skeleton(code="export const A = 1;")]}
    check(cs.forage_scan(full) == ("code", []),
          f"每镜都带码 → 分镜轮（实得 {cs.forage_scan(full)}）")
    skel = {"shots": [_skeleton()]}
    mode, rows = cs.forage_scan(skel)
    check(mode == "forage", f"一镜没带码、但声明了卡与出处 → 取材轮（实得 {mode}）")
    check(rows == [{"id": "s01", "card": "opening/fracture",
                    "derived_from": ["opening/fracture/Fracture.tsx"]}],
          f"逐镜清单照原样带出来（谁引用谁）（实得 {rows}）")
    little = {"shots": [{"id": "s01", "text": "只有文案和卡", "card": "opening/fracture",
                         "derived_from": ["opening/fracture/Fracture.tsx"],
                         "highlight": ["文案"]}]}
    check(cs.forage_scan(little)[0] == "forage",
          "骨架里多写 highlight 不改变这一次调用的性质")
    mixed = {"shots": [_skeleton(code="export const A = 1;"),
                       _skeleton(id="s02")]}
    m_mode, m_rows = cs.forage_scan(mixed)
    check(m_mode == "mixed" and [r["id"] for r in m_rows] == ["s02"],
          f"有的带码有的没带 → mixed，并点名是哪几镜（实得 {m_mode} / {m_rows}）")
    check(cs.forage_scan({"shots": [{"id": "s01", "text": "什么都没声明"}]})[0] == "none",
          "一镜没码也没声明 → none（交给校验器报「code 为空」，不猜它是取材）")
    check(cs.forage_scan({})[0] == "none" and cs.forage_scan({"shots": []})[0] == "none",
          "没 shots / shots 是空的 → none")
    check(cs.forage_scan({"shots": ["不是对象"]})[0] == "none",
          "镜不是对象 → none（形状坏到判不出意图，交给校验器）")
    check(set(cs.FORAGE_MODES) == {"forage", "code", "mixed", "none"},
          f"四档写在常量里，测试按集合断言而不是抄字符串（实得 {cs.FORAGE_MODES}）")

    # ── ② T20 的正题：一镜引用 2 个 demo → 上下文里真有那 2 份全文 ────────
    print("\n[② 全文：回执里的字节就是盘上的字节]")
    two = {"shots": [_skeleton(derived_from=["opening/fracture/Fracture.tsx",
                                             "camera/basic-3d-scene/Basic3DScene.tsx"])]}
    account, errs = cs.collect_demos(cs.forage_scan(two)[1])
    check(not errs, f"两份都在白名单里，零报错（实得 {errs}）")
    got = {d["relpath"]: d for d in account["forage_demos"]}
    check(set(got) == {"opening/fracture/Fracture.tsx",
                       "camera/basic-3d-scene/Basic3DScene.tsx"},
          f"一镜引用的两份都给了（实得 {sorted(got)}）")
    for rel, d in got.items():
        raw = _disk(rel)
        check(d["text"].encode("utf-8") == raw,
              f"{rel}：text 按 UTF-8 编回去**逐字节**等于盘上那份（{len(raw)} 字节）")
        check(d["bytes"] == len(raw), f"{rel}：bytes 是实测的 {len(raw)}，不是抄清单的")
        check(d["sha16"] == hashlib.sha256(raw).hexdigest()[:16],
              f"{rel}：sha16 = 该字节的 sha256 前 16 位（逐镜账要能指名版本）")
    check(account["forage_bytes"] == sum(d["bytes"] for d in account["forage_demos"]),
          "forage_bytes 是真给出去的字节之和（不含没给的）")
    check(all(set(d) == {"relpath", "bytes", "sha16", "text"}
              for d in account["forage_demos"]),
          "逐件的键是固定的那一组（实得 "
          f"{sorted(account['forage_demos'][0])}）")

    dup = {"shots": [_skeleton(id="s01"), _skeleton(id="s02")]}
    d2, _ = cs.collect_demos(cs.forage_scan(dup)[1])
    check(len(d2["forage_demos"]) == 1 and len(d2["forage_plan"]) == 2,
          "两镜引用同一份 → 字节只给一次，逐镜账仍记两条（省的是重复，不是出处）")

    # ── ③ 白名单与归一：两种写法、以及「示例必须真的是白名单里的」 ────────
    print("\n[③ 白名单单源 + relpath 两种写法归一]")
    bad = {"shots": [_skeleton(derived_from=["opening/根本不存在/Nope.tsx"])]}
    b_acct, b_errs = cs.collect_demos(cs.forage_scan(bad)[1])
    check(b_errs and "不在 demo 语料白名单中" in b_errs[0] and "s01" in b_errs[0],
          f"不在册的 relpath 带镜号打回（实得 {b_errs[:1]}）")
    check(b_acct["forage_demos"] == [],
          "打回时**一份字节都不给**（给了就等于承认了一个不存在的出处）")
    missing = {"shots": [{"id": "s01", "text": "只有文案"}]}
    check(cs.forage_scan(missing)[0] == "none",
          "没声明出处的骨架不算取材轮——它会走校验器那条路（见 ①）")
    # —— 两栏都是**选填**（2026-10-16 拆「必须照卡派生」）——
    #    取材轮的语义没变：它问的就是「你引用了哪几份 demo，把全文给你」。什么都没引用
    #    的一行，答案就是"没有字节可给"，不是一条错——原来那句「两栏各报一条」是把
    #    **分镜轮**的必填口径抄进了取材轮。
    _e_acct, empty_errs = cs.collect_demos([{"id": "s01", "card": "", "derived_from": []}])
    check(empty_errs == [] and _e_acct["forage_demos"] == [],
          f"取材轮遇到空出处：不报错、也不回字节（实得 {empty_errs}）")

    for written, want in [("opening/fracture/Fracture.tsx", "opening/fracture/Fracture.tsx"),
                          ("demos/opening/fracture/Fracture.tsx", "opening/fracture/Fracture.tsx"),
                          ("./demos/opening/fracture/Fracture.tsx",
                           "opening/fracture/Fracture.tsx")]:
        errs_of = []
        _c, norm = cs._norm_cite({"card": "opening/fracture", "derived_from": [written]},
                                 "s01", errs_of)
        check(norm == [want] and not errs_of,
              f"`{written}` → `{want}`（两种写法收成一种，逐镜账才不会分不清引的是哪一份）")

    # 这一条是**回归钉**：修之前 schema 的示例写的是带 demos/ 前缀的那个，而白名单里
    # 是 c 一个——照示例抄的模型会收到一整片「不在白名单中」。这条断言把「正文给的例子
    # 必须真的是白名单里的名字」变成机器判据，而不是靠下一次有人眼尖。
    import json as _json                                                  # noqa: PLC0415
    import re as _re                                                      # noqa: PLC0415
    spec_doc = _json.dumps(cs.spec_schema(), ensure_ascii=False)
    examples = set(_re.findall(r'\[\\?"([^"\\]+\.tsx)\\?"\]', spec_doc))
    check(bool(examples), f"schema 里确实写着 derived_from 的例子（实得 {examples}）")
    known = cs._known_demo_relpaths()
    check(examples <= set(known),
          "例子逐条都真的在白名单里（照抄不会被假打回）——"
          f"漏的：{sorted(examples - set(known))}")

    # ── ④ 超线：点名，绝不截断 ──────────────────────────────────────────
    print("\n[④ 超线不截断：没给的那几件要点名]")
    old_cap = cs.FORAGE_MAX_BYTES
    try:
        cs.FORAGE_MAX_BYTES = 10_000                          # 只想让第一件过
        cap_acct, cap_errs = cs.collect_demos(cs.forage_scan(two)[1])
    finally:
        cs.FORAGE_MAX_BYTES = old_cap
    check(not cap_errs, "超线不是错误——那几件是**没给**不是取不到")
    given = [d["relpath"] for d in cap_acct["forage_demos"]]
    over = [o["relpath"] for o in cap_acct["forage_oversize"]]
    check(len(given) == 1 and given[0] == "opening/fracture/Fracture.tsx"
          and over == ["camera/basic-3d-scene/Basic3DScene.tsx"],
          f"第一件照给、超线的那件进名单（实得 given={given} over={over}）")
    check(all(d["text"].encode("utf-8") == _disk(d["relpath"])
              for d in cap_acct["forage_demos"]),
          "给了的那件仍是**全文**（超线裁掉的是文件，不是文件的内容）")
    check(cap_acct["forage_bytes"] == cap_acct["forage_demos"][0]["bytes"],
          "forage_bytes 只算真给出去的那件")
    over_rows = {o["relpath"]: o["bytes"] for o in cap_acct["forage_oversize"]}
    check(over_rows == {"camera/basic-3d-scene/Basic3DScene.tsx":
                        len(_disk("camera/basic-3d-scene/Basic3DScene.tsx"))},
          "名单里带它的字节数：人话里「这一件多大」要能对着账核")
    hint = mp.forage_payload(cap_acct)["next"]
    check("load_skill" in hint and "camera/basic-3d-scene/Basic3DScene.tsx" in hint,
          f"名单要出现在下一步提示里并给出取回方式（实得 {hint[-90:]}）")
    try:
        cs.FORAGE_MAX_BYTES = 0
        zero, _ = cs.collect_demos(cs.forage_scan(two)[1])
    finally:
        cs.FORAGE_MAX_BYTES = old_cap
    check(len(zero["forage_demos"]) == 1,
          "上限小到 0 也照给第一件——一份全文都比一份都不给有用，"
          "这个上限本来防的是「一轮塞太多」")

    # ── ⑤ 节点行为：取材轮不落库、混合形状打回 ──────────────────────────
    print("\n[⑤ 节点：取材轮回字节但不入库；混合形状当场打回]")
    # 三个依赖都传 None：取材轮在**取数与落库之前**就返回了，它不该碰这三样。
    # 这不是省事——它是一条结构断言：哪一天有人在取材轮里顺手读了一次 storage，
    # 这条用例会当场炸，而不是等到某个真机上才发现「取材也要连 MinIO」。
    node = mp.PlanMotionNode(None, None, None)                              # type: ignore[arg-type]
    payload = asyncio.run(node.process(None, {"spec": two}))                # type: ignore[arg-type]
    check(payload.get("__no_store__") is True,
          "回执带 __no_store__——它是「这一次调用的产物」，不是本会话的 motion_spec")
    check(payload.get("stored") is False, "并且把「什么都没入库」明写在回执里")
    check(len(payload.get("forage_demos") or []) == 2,
          "节点这一层也真把两份全文带出来了（不是只在 code_spec 里对）")
    check("motion_spec" not in payload,
          "取材轮的产物里**没有** motion_spec 这一格（有它就成了「这步已有分镜」）")
    try:
        asyncio.run(node.process(None, {"spec": mixed}))                    # type: ignore[arg-type]
        check(False, "混合形状应当打回，却回了产物")
    except ValueError as e:
        msg = str(e)
        check("s02" in msg and "各调一次" in msg and "没入库" in msg,
              f"混合形状的报错点名镜号并说清怎么改（实得 {msg[:80]}…）")
    try:
        asyncio.run(node.process(None, {"spec": bad}))                      # type: ignore[arg-type]
        check(False, "不在册的 relpath 应当打回，却回了产物")
    except ValueError as e:
        check("白名单" in str(e), f"取材轮的白名单打回走的是同一份清单（实得 {str(e)[:70]}…）")

    # ── ⑥ load_skill(path=) 不截断：拿最大那份真验一遍 ──────────────────
    print("\n[⑥ load_skill(path=) 不截断（真 SkillLoader + 真技能目录）]")
    biggest = max((p for p in DEMOS.rglob("*.tsx")), key=lambda p: p.stat().st_size)
    rel_big = biggest.relative_to(DEMOS).as_posix()
    asyncio.run(_load_skill_case(biggest, rel_big))

    # ── ⑦ 口径钉：技能名、白名单、两处正文 ──────────────────────────────
    print("\n[⑦ 口径钉：技能名从 frontmatter 读、键进白名单、正文教了两步走]")
    check(cs.motion_skill_name() == "motion_explainer_skill",
          f"技能名读自 SKILL.md（它**不等于**目录名 motion_explainer）（实得 "
          f"{cs.motion_skill_name()!r}）")
    from agent_framework.plan import support as plan_support            # noqa: PLC0415
    missing_keys = {"forage_plan", "forage_demos", "forage_bytes",
                    "forage_oversize"} - set(plan_support.OUTPUT_FIELD_NAMES)
    check(not missing_keys,
          f"模型侧白名单收得下这四个键（缺 {sorted(missing_keys)} 就是假告警："
          "技能正文教模型按 forage_oversize 点名的那几件去 load_skill）")
    desc = mp.PlanMotionNode.description
    check("取材轮" in desc and "不带 code" in desc,
          "工具描述里写了这一轮怎么交（模型是从这里知道有这条路的）")
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    check("取材轮" in skill_md and "forage_oversize" in skill_md
          and "先取材，再写码" in skill_md,
          "SKILL.md 的适配步骤把这一步排在写码**之前**（顺序是这条判据的要点）")
    check("demos/effects/aurora-bloom-bg-flip/AuroraBloomBgFlip.tsx" not in skill_md,
          "正文里不再拿「带 demos/ 前缀的 relpath」当 derived_from 的例子")
    check(mp.forage_payload({"forage_demos": [], "forage_bytes": 0})["next"],
          "没有超线件时也给出下一步（「现在去写 code」），不是一句空话")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


async def _load_skill_case(biggest: Path, rel_big: str) -> None:
    """真 loader 走一遍最大那份 demo —— 「不截断」唯一有意义的验法。"""
    storage = build_storage("memory")
    await storage.start()
    try:
        loader = SkillLoader(storage)
        await loader.sync_from_dir(SKILL_DIR.parent)
        tool = LoadSkillTool(loader)
        raw = biggest.read_bytes()
        out = await tool.execute(cs.motion_skill_name(), f"demos/{rel_big}")
        check(out.startswith(f"[skill:{cs.motion_skill_name()} file=demos/{rel_big}]"),
              f"附件回执点名了是哪一份（实得 {out[:60]!r}…）")
        text = out.split("\n", 1)[1] if "\n" in out else ""
        check(text.encode("utf-8") == raw,
              f"最大那份 demo（{rel_big}，{len(raw)} 字节）一字不少地回来"
              "——拿小文件验这条等于没验")
        check(len(text) > 10_000,
              f"这一份确实大到能验出截断（{len(text)} 字符）")
        miss = await tool.execute(cs.motion_skill_name(), "demos/根本不存在.tsx")
        check(miss.startswith("Error:") and "无附件" in miss,
              f"不在案的路径给的是人话错误，不是字节（实得 {miss[:50]}…）")

        # 取正文的那条岔路也要钉：截断在这里的表现是「提示让模型读 SKILL.md，
        # 模型读到了，但适配步骤那一节被切掉了」——它不报错，只是静悄悄地少了半页。
        # 判据取**最后一行**：任何位置的截断都会把它切掉。
        body_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        last = [ln for ln in body_md.splitlines() if ln.strip()][-1].strip()
        whole = await tool.execute(cs.motion_skill_name())
        check(len(last) > 8 and last in whole,
              f"取正文时 SKILL.md 也是全文（末行 «{last[:36]}…» 在，截断会把它切掉）")
        check("取材轮" in whole,
              "正文回执里带着「取材轮」那一节（模型是照这一节走两步的）")
    finally:
        await storage.close()


if __name__ == "__main__":
    sys.exit(main())
