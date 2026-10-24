"""图形科普片的**分镜**节点：把模型写的代码分镜校验、归一、落进 Store，并回一份账。

为什么单有一个节点（而不是让模型直接调 render_motion_code）：

* **校验要有落点**：出片只认 Store 里那一份 ``motion_spec``。没有这一步，spec 要么每次
  调用重传一遍（模型容易漏、两处版本会漂），要么偷偷在出片节点里现场校验——而那时候
  已经站在烧像素的门口了。
* **账要在烧像素之前给人看**：每镜几个字、估算多少秒、离目标差多远、这一镜交出几个可改
  的旋钮，这些决定「删镜还是加镜」，属于分镜阶段的事，不该由一次十分钟的渲染来揭晓。
* **改文案不该重新渲一次片**：分镜与出片是两次决定，各自一个工具，才谈得上「只改第 3 镜
  再渲一次」。

口径与 ``motion/code_spec.py`` 一致：**镜头时长不由模型给**——有旁白时时钟归 TTS 实测，
模型写的 ``min_duration_sec`` 只当下限。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_framework.orchestration import NodeState, _safe
from agent_framework.skill import _parse_frontmatter, derive_display
from agent_framework.retrieval import (UNVERIFIED_MARKS, cite_fields, claims_verified,
                                       ledger, matches_ledger)

import os

from ..motion import art_director
from ..motion import code_render as crender
from ..motion import code_spec as cspec
from ..motion import design_review
from ..motion import remotion_client as rc
from ..motion import recipe_index
from ..motion import shot_writer
from .core_nodes import StoryNode, _flag_on, _obj, _resolve_target_duration

#: 分镜卡上每镜文案最多显示这么多字——界面不是文稿编辑器，全文在 motion_spec 里。
PLAN_TEXT_PREVIEW_CHARS = 24


#: 一镜的出处在这一刻的检索账上算什么。**机器不判真伪**，这里只把「这一笔在不在账上」
#: 如实标出来——观众看到画面上有出处，得知道那三个字是查过的、是模型自陈没查过的、
#: 还是压根没填。
GATE_STATES = ("verified", "declared_unverified", "no_source", "ledger_absent")


def gate_state(source: Any, rows: list[Any] | None) -> str:
    """一镜出处的判定（纯函数：判据与闸本体逐字同源，绝不另写一遍）。

    ``claimed_unmatched``（声称查证过但对不上账）是**判断结果**，不是终态：
    :func:`settle_sources` 会把它改写成明写的未核实再入库，因此入库的分镜里只会出现
    ``GATE_STATES`` 那四种。
    """
    if not source:
        return "no_source"
    if not claims_verified(source):
        return "declared_unverified"        # 写了「未核实：…」——不冒充，也就不必降级
    if rows is None:
        return "ledger_absent"              # 没绑账本：拿「没记过」定罪等于一律打回，那是另一种假
    return "verified" if matches_ledger(source, rows) else "claimed_unmatched"


#: 降级时写在标题前的字面。`claims_verified` 认这两个字，分镜卡上因此显示「未核实」。
UNVERIFIED_PREFIX = "未核实："
LEDGER_MISS_NOTE = "本会话检索账里没有这一笔"


def _mark_source_unverified(source: Any) -> Any:
    """把一条「对不上账却写成查证过」的出处改写成明写的未核实；返回改后的 source。

    为什么改写而不是打回（真机 2026-10-19）：模型被出处闸拦下后连抓 23 个页面、
    同一份 24 镜分镜重交 4 次，十分钟卡在分镜这一步；而闸要保的只有一件事——
    「模型记忆不许冒充查证过的出处」。就地标成「未核实」同样保住这件事（分镜卡、
    逐镜账、成片里的凭据都变成明写的未核实），却不再把「补一次未必查得到的检索」
    当成入库前提。

    代价：一条其实查过、只是标题写法与检索账没对上的出处（如多写了一个期号）
    也会被标成未核实——宁可多标一句，也不让一条片子卡在重试里烧掉整轮预算。
    要让它算查证过，模型下一版把标题/链接按检索结果原样填即可。
    """
    if isinstance(source, Mapping):
        marked = dict(source)
        key = next((k for k in marked
                    if str(k).strip().lower() in ("title", "标题", "来源", "source")), None)
        if key is not None:
            text = str(marked[key] or "").strip()
            if text and not any(m in text for m in UNVERIFIED_MARKS):
                marked[key] = f"{UNVERIFIED_PREFIX}{text}（{LEDGER_MISS_NOTE}）"
            return marked
        marked.setdefault("核实", f"{UNVERIFIED_PREFIX}{LEDGER_MISS_NOTE}")
        return marked
    if isinstance(source, str) and source.strip():
        text = source.strip()
        if not any(m in text for m in UNVERIFIED_MARKS):
            return f"{UNVERIFIED_PREFIX}{text}（{LEDGER_MISS_NOTE}）"
    return source


async def settle_sources(session_id: str, shots: list[dict[str, Any]], *,
                         audit_ids: set[str] | None = None
                         ) -> tuple[list[str], dict[str, str]]:
    """→ (提醒清单, 每镜判定)。**只降级，不打回**——分镜入库口、局部改、inline 渲染口
    调的都是这一个函数。

    为什么需要它（真机事故 2026-10-08）：检索被人机验证挡在门外之后，模型照样写完了
    分镜，每镜 ``source`` 填的是它自己的记忆。画面与逐镜账如实转录了这些出处，观众读到
    的却是「像查过的一样」的猜测——「机器不判真伪、只如实转录」这条合同被架空了。
    闸不判史实（判不了），只判**这一笔在本会话里发生过吗**。

    2026-10-19 起，「这一笔不在账上」不再把整份分镜退回去重交：就地降级成
    「未核实：…」并回一条提醒（见 :func:`_mark_source_unverified` 里的代价）。
    判据本体仍是 :func:`gate_state`；这里多出来的只是改写与记账。

    ``audit_ids`` 只缩小**降级**的范围（局部改不替整片重审旧账），判定仍逐镜给全：
    两次读账不如一次读完，而标要标全——漏标的镜在账上就成了「没出处」，那是另一种假。

    账本不可用（没绑存储）时整体放行：没地方记命中，就拿「没记过」定罪会退化成
    「所有出处一律打回」，那是另一种假。
    """
    rows = await ledger(session_id)
    states: dict[str, str] = {}
    notes: list[str] = []
    for shot in shots:
        sid = str(shot.get("id") or "")
        source = shot.get("source") or {}
        st = gate_state(source, rows)
        if st == "claimed_unmatched" and (audit_ids is None or sid in audit_ids):
            title, url = cite_fields(source)
            shot["source"] = _mark_source_unverified(source)
            notes.append(
                f"{sid}：出处「{title or url}」对不上本会话的检索账，系统已按「未核实」"
                f"标注（链接保留）。要让它算查证过，先真正检索/打开那一页再交；"
                f"现在照常入库，分镜卡上显示未核实"
                + ("（本会话一次检索都没成功过）" if not rows else ""))
            st = "declared_unverified"
        states[sid] = st
    return notes, states


#: 会过期的数字：「近几年的年份 + 带单位的数量」同时出现在一句文案里。
#: 只认带单位的数——「2024 年发布了 X」是历史事件，不是统计值；「2024 年，人均每天 156 分钟」
#: 才是会随新报告变化的统计值。只报不拦（见 `freshness_warnings`）。
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*年")
_QTY_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|亿|万|千万|百万|千|分钟|小时|秒|天|周|"
                     r"人|次|元|美元|块|倍|个百分点|条|个|家|所|座|公里|千米|米|吨|"
                     r"GB|TB|MHz|GHz)")


def freshness_warnings(shots: list[dict[str, Any]], *,
                       now: datetime | None = None) -> list[str]:
    """分镜里「会过期的数字」的时效提醒（**只报不拦**，与 style_warnings 同一档）。

    真机事故（2026-10-18）：一条片子在 2026 年写「2024 年，中国短视频用户人均每天刷 156
    分钟」——数字对得上当年那期报告，但已经不是最新一期。出处闸只问「这一页真打开过吗」，
    不问「有没有更新一版」；模型凭记忆写下的旧数因此一路过闸、上了屏。

    这里不判真伪也不联网（判不了），只把「文案自报的数据期已经比当前年份落后两年以上」
    如实摆到确认卡上：有更新就换、没有就在 source 里写明数据期。它不进任何打回清单——
    讲历史的片子引用旧年份是正常写法，这条判据宁可多提醒一句，也不许把正常内容拦下来。
    """
    year_now = (now or datetime.now()).year
    warnings: list[str] = []
    for shot in shots:
        text = str(shot.get("text") or "")
        if not _QTY_RE.search(text):
            continue
        sid = str(shot.get("id") or "?")
        years = [int(y) for y in _YEAR_RE.findall(text)]
        # 只看近五年：更早的年份是历史叙述，不是「今天还在用的统计值」。
        recent = [y for y in years if year_now - 4 <= y <= year_now]
        stale = [y for y in recent if year_now - y >= 2]
        if stale:
            warnings.append(
                f"{sid}：这一镜的数字标的是 {min(stale)} 年（现在 {year_now} 年）——"
                f"先查一下最新一期是多少；有更新就换最新一版、文案里的年份一起改，"
                f"没有就在 source 里写「数据期：{min(stale)} 年」，别让旧数看起来像现值")
        elif recent and not shot.get("source"):
            warnings.append(
                f"{sid}：这一镜是 {max(recent)} 年的数字，却没填 source——"
                f"查最新一期并填出处（标题 + 链接，数据期写进 source）")
    return warnings


def _try_rescue_json(text: str) -> Any:
    """尝试 json.loads；失败时用截断修复（括号栈+字符串状态）再试一次。"""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        from agent_framework.llm_openai import _repair_truncated_json
        repaired = _repair_truncated_json(text)
        if repaired:
            return json.loads(repaired)
    except Exception:
        pass
    return None


#: 风格包正文进 spec 的上限。它是给写手看的说明书，不是分镜本身；
#: 超过这个长度只截断、不报错——风格包写得啰嗦不该让整份分镜交不上。
MAX_STYLE_BODY_CHARS = 12000
#: 多份技能合并时，**每一份**最多贡献多少字（2026-10-24）。
#: 十份 skill 的正文合计八万多字，全量塞进写手 prompt 会把它淹掉——每份只给开头
#: 这一段（够看清它主张什么画法），要更细的做法看它自己的 `references/`。
SKILL_BUNDLE_PER_MAX = 6000
#: 动态下限：挂 10 份时每份仍至少给这么多。
SKILL_BUNDLE_PER_MIN = 1200
#: 合并的上限份数。再多就只是噪音。
SKILL_BUNDLE_MAX_SKILLS = 10


def _clean_phrase(text: Any) -> str:
    """把一句话压成「只留汉字字母数字」的形态，用来做子串匹配。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(text or ""))


#: 用户在这句话里明确表态「不要风格包」——自动挑到这就整段让路。
#: 宣传片、能力混搭片必须能说这句话，否则「剪影」「手绘」这些词一定会误挂一套上去。
_STYLE_OPT_OUT = ("风格留自动", "风格自动", "不套风格", "不挂风格", "不要风格包",
                  "不套模板", "不挂模板", "不用风格包", "不要套模板")


def _bigrams(text: Any) -> set[str]:
    s = _clean_phrase(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _identity_phrases(row: Mapping[str, Any]) -> list[str]:
    """这套风格「自称」的那几个词：中文名 + `auto_select` + 简介短句 + 能力名。

    全部来自文件本身，不需要人写——用户导入新技能时零维护：

    * `display`（缺了就从正文一级标题派生，见 `skill.derive_display`）；
    * `style_manifest.auto_select`（`auto_select` 这个字段以前是死的，没有任何代码读它，
      它本来就是为这件事写的）；
    * 简介里 4~12 字的中文片段；
    * **附件里的中文目录名 / 不重复的文件名**（`skill.capability_phrases`）——
      社区技能几乎都是「一种能力一个目录」，这些名字就是它的能力清单。

    为什么不用 `##` 小标题：实测七份里小标题几乎全是流程（「第 0 步：检查环境」
    「工作目录」「遇到这些情况」），当触发词只会把匹配搞脏。
    """
    from agent_framework.skill import capability_phrases, derive_display

    fm = row.get("frontmatter") or {}
    out: list[str] = []
    display = _clean_phrase(derive_display(fm, str(row.get("body") or ""),
                                           str(row.get("name") or "")))
    if len(display) >= 2:
        out.append(display)
    raw = fm.get("auto_select")
    items = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    for item in items:
        p = _clean_phrase(item)
        if 2 <= len(p) <= 12:
            out.append(p)
    desc = str(fm.get("description") or "")
    for chunk in re.split(r"[^\u4e00-\u9fff]+", desc):
        if 4 <= len(chunk) <= 12:
            out.append(chunk)
    for p in capability_phrases(fm, str(row.get("body") or ""),
                                row.get("files") or [], limit=10):
        out.append(_clean_phrase(p))
    seen: list[str] = []
    for p in out:
        # 上限从 12 放宽到 40：导入的 skill 大量是英文名（`dashi-animationstyle`
        # 18 个字符、`huashu-art-motion` 15 个），旧的 12 字上限会把它们整条过滤掉，
        # 结果是「点名字永远挂不上」——2026-10-24 实测七个导入包里四个中招。
        # `_phrase_hit` 本来就只认 ≥4 字的公共片段，长名字不会变松。
        if 2 <= len(p) <= 40 and p not in seen:
            seen.append(p)
    return seen

def _phrase_hit(phrase: str, req: str) -> str:
    """短语里**最长的那一段**出现在请求里，就回那一段；没有回空串。

    为什么不要求整句命中：风格包的 display 是「平涂剪影四层景」，用户嘴里说的是
    「纯平涂剪影」——整句永远对不上，而「平涂剪影」这 4 个字是强信号。反过来，
    只按 2 字重叠又太松（「动画」「设计」谁都撞）。所以取「≥4 字的最长公共片段」：
    够长才算数，且报出来的那一段能直接写进回执给用户看。
    """
    L = len(phrase)
    floor = 3 if L == 3 else 4
    for n in range(L, floor - 1, -1):
        for i in range(L - n + 1):
            sub = phrase[i:i + n]
            if sub in req:
                return sub
    return ""


async def _auto_pick_own_skill(storage: Any, user_id: str,
                               request: str) -> tuple[str, str]:
    """用户没点名风格时，看他自己的话是不是正好指向**他自己导入**的某个技能。

    返回 ``(技能名, 给人看的一句话)``。没命中回 ``("", "")``；命中多份回
    ``("", 说明)``——那时候**不替他选**，但把候选说出来，让他点一下。

    ## 用户明确说了「不挂风格」就一律不猜

    宣传片那类片子要在**一部片里换五种视觉语言**，提示词里会写「风格留自动、不要挂任何
    风格包」。那种话里必然出现「剪影」「手绘」「界面」这些词，按下面的匹配逻辑一定会
    误挂一套上去——把整片拉成那一种。所以先认这句话，命中就整段让路。

    ## 为什么只认「本人导入的」

    2026-10-19 删掉过一版「按特征词自动套风格包」，理由是「那等于把视觉规则从对话
    搬回库里」。那个批评对**系统内置包**成立：库里那几套是产品带的，用户没要。
    但用户自己拖进来的技能是另一件事——他花了力气装进来，就是想让片子按那个画。
    所以这里的边界是 `owner_user_id == user_id`：系统包一个都不碰。

    ## 为什么并列时不猜

    并列第一说明这段话同时像两套（他库里确实有两套剪影）。这时候挂哪一个都是赌，
    而赌错的代价是整片按错的形式渲完。所以回一句「都对得上，你指一下」——
    把一次猜测换成一次点击，比默默选错便宜得多。
    """
    plain = str(request or "")
    if any(k in plain for k in _STYLE_OPT_OUT):
        return "", ""
    req = _clean_phrase(request)
    if len(req) < 4:
        return "", ""
    try:
        rows = await storage.skills.list(user_id)
    except Exception:      # noqa: BLE001 - 读不到技能库就当没有，不拦分镜
        return "", ""
    scored: list[tuple[int, str, str, list[str], str]] = []
    for row in rows:
        if str(row.get("owner_user_id") or "") != str(user_id or ""):
            continue                      # 只认本人导入的，系统内置包一律不碰
        fm = row.get("frontmatter") or {}
        hits = [h for h in (_phrase_hit(p, req) for p in _identity_phrases(row)) if h]
        # 机器名单独算一条：必须**整段**出现在请求里。
        # 不能交给 `_phrase_hit`——它认任意 ≥4 字片段，`motion_explainer_skill`
        # 里的 "skill" 会让「参考 nope-skill-xyz」误挂到科普片上（2026-10-24 实测）。
        name_p = _clean_phrase(row.get("name") or "")
        if len(name_p) >= 4 and name_p in req and name_p not in hits:
            hits.append(name_p)
        if not hits:
            continue
        longest = max(len(h) for h in hits)
        overlap = len(_bigrams(req) & _bigrams(str(row.get("body") or "")[:800]))
        display = str(fm.get("display") or "") or derive_display(
            fm, str(row.get("body") or ""), str(row.get("name") or ""))
        scored.append((longest * 3 + len(hits) * 2 + overlap,
                       str(row.get("name") or ""), display,
                       sorted(set(hits), key=len, reverse=True)[:3],
                       str(row.get("updated_at") or "")))
    if not scored:
        return "", ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[0]
    rivals = [s for s in scored[1:] if s[0] == top[0]]
    if rivals:
        names = "、".join(f"《{s[2]}》" for s in [top, *rivals[:2]])
        return "", (f"你这段话同时像 {names} 这几套，没替你选——"
                    "把风格指一下（或从模板库里挑一套）我就按它画")
    _, name, display, hits, _ = top
    return name, (f"你这段话里的「{'、'.join(hits)}」对上了你自己导入的《{display}》，"
                  f"这一片按它的画面契约画（不想这样就把风格改成「自动」）")


async def _auto_pick_own_skills(storage: Any, user_id: str,
                                request: str) -> tuple[list[str], str]:
    """把用户原话里**所有**对得上的、他自己导入的技能名都取回来（按分排序）。

    与 `_auto_pick_own_skill`（只取唯一命中）的区别：那条路用在「只能挂一份」的
    旧口径上；2026-10-24 起支持一份片子挂多份技能，所以这里回列表。

    仍然不猜：一份都没命中回 `([], "")`；命中的**全部**按分数从高到低返回，
    由调用方按预算裁剪——不再因为「有两套都对得上」就一份都不挂。
    """
    plain = str(request or "")
    if any(k in plain for k in _STYLE_OPT_OUT):
        return [], ""
    req = _clean_phrase(request)
    if len(req) < 4:
        return [], ""
    try:
        rows = await storage.skills.list(user_id)
    except Exception:      # noqa: BLE001 - 读不到技能库就当没有，不拦分镜
        return [], ""
    scored: list[tuple[int, str]] = []
    # **显式点名优先**（2026-10-24）：原话里出现了某份技能的机器名（整段命中），
    # 就只挂被点名的那几份，不再做模糊匹配。
    #
    # 为什么必须这样：模糊匹配会拿 description 里的 4~12 字中文片段去撞，v8 提示词
    # 里写了「十二个机制」，就把 `system-diagram-animation` 也拉了进来——用户只点了
    # 两份，实际挂了第三份，那一份的正文还会覆盖掉前两份的画法。
    explicit: list[str] = []
    for row in rows:
        if str(row.get("owner_user_id") or "") != str(user_id or ""):
            continue
        name_p = _clean_phrase(row.get("name") or "")
        if len(name_p) >= 4 and name_p in req:
            explicit.append(str(row.get("name") or ""))
    if explicit:
        return sorted(set(explicit)), f"你点名了 {len(set(explicit))} 份技能"

    for row in rows:
        if str(row.get("owner_user_id") or "") != str(user_id or ""):
            continue                      # 只认本人导入的，系统内置包一律不碰
        fm = row.get("frontmatter") or {}
        hits = [h for h in (_phrase_hit(p, req) for p in _identity_phrases(row)) if h]
        name_p = _clean_phrase(row.get("name") or "")
        if len(name_p) >= 4 and name_p in req and name_p not in hits:
            hits.append(name_p)
        if not hits:
            continue
        longest = max(len(h) for h in hits)
        overlap = len(_bigrams(req) & _bigrams(str(row.get("body") or "")[:800]))
        scored.append((longest * 3 + len(hits) * 2 + overlap,
                       str(row.get("name") or "")))
    if not scored:
        return [], ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    names = [n for _, n in scored[:SKILL_BUNDLE_MAX_SKILLS]]
    return names, f"这段话对上了你导入的 {len(names)} 份技能"


async def _attach_skill_contract(storage: Any, raw: Mapping[str, Any], name: str,
                                  user_id: str) -> tuple[dict[str, Any], str]:
    """按名字取**用户导入的 skill**，注入 ``spec["skill_contract"]``。→ (新 spec, 说明)

    2026-10-24：这里原来叫 `_attach_style_pack`，同一个载体同时服务「风格包」与
    「导入的 skill」。风格包通道整条删除之后只剩后者，所以：

    * **不再读 `style_manifest`**（配色/构图/模板/槽位那些键已经不产出了）
    * **不再找模板、不再绑槽位**（模板通道已删）
    * **`kind=style` 直接拒绝**——项目里不允许再有风格包这种东西

    三条纪律照旧：只读本人或系统内置；失败只降级不拦；别人的按不存在处理。
    """
    fm: Mapping[str, Any] = {}
    body = ""
    owner: str | None = None
    row: Mapping[str, Any] | None = None
    try:
        row = await storage.skills.get(name)
    except Exception:      # noqa: BLE001 - 存储不可达时走磁盘种子
        row = None
    if row:
        fm = row.get("frontmatter") or {}
        body = str(row.get("body") or "")
        owner = row.get("owner_user_id")
    else:
        # 磁盘兜底：editor 可能比 app 先起（系统种子还没同步进 PG）。
        seed = (Path(__file__).resolve().parents[2]
                / "examples" / "skills" / name / "SKILL.md")
        if not seed.is_file():
            return dict(raw), f"技能「{name}」不存在，按默认视觉继续"
        try:
            meta, body = _parse_frontmatter(seed.read_text(encoding="utf-8"))
        except OSError:
            return dict(raw), f"技能「{name}」读不出来，按默认视觉继续"
        fm, owner = meta, None
    kind = str(fm.get("kind") or "workflow").strip().lower()
    if kind == "style":
        return dict(raw), f"「{name}」是风格包，这条通道已删除，按默认视觉继续"
    if owner is not None and owner != user_id:
        return dict(raw), f"技能「{name}」不可用，按默认视觉继续"
    display = str(fm.get("display") or name)
    spec = dict(raw)
    spec["skill_contract"] = {
        "name": name,
        "display": display,
        "body": body[:MAX_STYLE_BODY_CHARS],
        #: 这一片挂了哪几份技能——写手的「可读参考」清单按它筛。
        "parts": [name],
    }
    return spec, f"已把导入的技能《{display}》作为这一片的画面契约（{name}）"


async def _attach_skill_contracts(storage: Any, raw: Mapping[str, Any],
                                  names: Sequence[str],
                                  user_id: str) -> tuple[dict[str, Any], str]:
    """把**多份**导入的技能合并成一个画面契约（`spec["skill_contract"]`）。

    为什么合并而不是列表：`code_spec` / `shot_writer` / `art_director` /
    `design_review` 四处都只认「一个契约」，改成列表要动四个模块和它们的校验。
    合并成一个载体，形状不变、下游零改动，而写手看到的是分节的多份正文。

    **预算**：每份最多 :data:`SKILL_BUNDLE_PER_MAX` 字，合计不超过
    :data:`MAX_STYLE_BODY_CHARS`。十份 skill 的正文合计八万多字，全量注入会把它淹掉。

    只挂一份时退回单份形状（`name`/`display` 就是它自己的），回执也一样——
    这样「只点一个」的老行为逐字不变。
    """
    wanted = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not wanted:
        return dict(raw), ""

    got: list[tuple[str, str, str]] = []       # (name, display, sliced body)
    skipped: list[str] = []
    budget = MAX_STYLE_BODY_CHARS
    for name in wanted[:SKILL_BUNDLE_MAX_SKILLS]:
        spec_one, note = await _attach_skill_contract(storage, raw, name, user_id)
        contract = spec_one.get("skill_contract")
        if not isinstance(contract, Mapping):
            skipped.append(name)
            continue
        body = str(contract.get("body") or "")
        # 每份给多少：总预算 ÷ 份数，夹在 [MIN, MAX] 之间。
        # 固定 1600 时，只挂 2 份也各给 1600——huashu 的 11162 字只看到 14%。
        per = max(SKILL_BUNDLE_PER_MIN,
                  min(SKILL_BUNDLE_PER_MAX,
                      MAX_STYLE_BODY_CHARS // max(1, len(wanted))))
        keep = min(per, budget)
        if keep <= 0:
            skipped.append(name)
            continue
        got.append((str(contract.get("name") or name),
                    str(contract.get("display") or name),
                    body[:keep]))
        budget -= min(len(body), keep)

    if not got:
        return dict(raw), ("这些技能都没挂上（" + "、".join(skipped) + "），按默认视觉继续"
                           if skipped else "")
    if len(got) == 1:
        name, display, body = got[0]
        spec = dict(raw)
        spec["skill_contract"] = {"name": name, "display": display, "body": body}
        return spec, f"已把导入的技能《{display}》作为这一片的画面契约（{name}）"

    parts = [f"### {display}（{name}）\n{body}".rstrip()
             for name, display, body in got]
    spec = dict(raw)
    spec["skill_contract"] = {
        "name": "skill_bundle",
        "display": "、".join(d for _, d, _ in got),
        "body": "\n\n".join(parts),
        "parts": [n for n, _, _ in got],
    }
    note = (f"已挂上 {len(got)} 份技能：" + "、".join(f"《{d}》" for _, d, _ in got)
            + f"（每份取正文前 {SKILL_BUNDLE_PER_MAX} 字）")
    if skipped:
        note += "；没挂上：" + "、".join(skipped)
    return spec, note

#: 渲染环境自检的进程内缓存：`{"c": (时间戳, 说明)}`。
#: 2026-10-24 补回：删风格包那一段时被连带删掉了（原来紧贴在 `_preflight_note` 上面），
#: 结果是每次 plan_motion 都炸 `name '_PREFLIGHT_CACHE' is not defined`。
_PREFLIGHT_CACHE: dict[str, tuple[float, str]] = {}
_PREFLIGHT_TTL_SEC = 600.0


async def _preflight_note() -> str:
    """渲染环境自检（按进程缓存 10 分钟）。返回空串＝没发现问题。

    只查两件**写码前就能知道、且一定会让整片白烧**的事：/healthz 是否可达、容器里
    有没有 chromium。gl 档与哈希不在这里判——它们更像「质量/版本漂移」，不是「渲不出来」，
    而前者已经会让第一镜真渲失败并自己报出来。

    **只报不拦**：返回一句话给回执，不抛。`MOTION_PREFLIGHT_STRICT=1` 由调用方升级。
    """
    import time

    now = time.monotonic()
    hit = _PREFLIGHT_CACHE.get("c")
    if hit and now - hit[0] < _PREFLIGHT_TTL_SEC:
        return hit[1]
    note = ""
    try:
        got = await asyncio.to_thread(rc.health, timeout=5)
    except Exception as exc:  # noqa: BLE001 —— 连不上不该炸掉分镜
        note = (f"渲染环境自检：连不上 creation-remotion（{type(exc).__name__}）——"
                "继续写码可能白烧，先确认渲染容器在跑。")
    else:
        if not got.get("ok"):
            note = "渲染环境自检：/healthz 报 not ok——继续写码可能白烧。"
        elif not got.get("chromium"):
            note = ("渲染环境自检：容器里找不到 chromium——写出来的镜头渲不出帧，"
                    "继续写码会白烧。")
    _PREFLIGHT_CACHE["c"] = (now, note)
    return note


class PlanMotionNode(StoryNode):
    name = "plan_motion"
    display_name = "图形科普片分镜"
    description = (
        "零素材出片的第一站：交一份**分镜**，校验、归一并存成本会话的 motion_spec，"
        "同时回一份「将要渲成什么样」的账（镜头数、总字数、按字数估的秒数区间、"
        "与目标时长的偏差提醒、每镜交出的字段清单）。本节点**不渲染、不烧像素**。"
        # ─── 第一道门：需求文档（2026-10-23）────────────────────────────────
        # 真机 run（15:0x）：模型把卡面上的中文标签（「关键词换色」「男声·沉稳纪录片
        # 解说」）当机器名传进来，四次报错、整次分镜作废，一次重跑烧 407 秒。
        # 根因是「理解需求」与「填 30 个字段」挤在同一次调用里。所以在调本工具
        # **之前**，先用一段文字把需求写下来，停下等用户确认——用户改一段话，
        # 比改 12 镜的代码便宜两个数量级。
        "**调用本工具之前，先用一段文字输出一份《需求文档》并停下来等用户确认。**"
        "用户确认或修改之后，才调本工具。**不要在同一条回复里既给文档又调本工具**——"
        "那样文档等于没写，用户没有机会在花掉写码时间之前改它。"
        "文档写六节，每节一两句："
        "① 主题与一句话主线；② 受众与语气（给谁看、多正式）；"
        "③ 时长与节奏；④ 必须出现的具体事实（带数字，注明数据期）；"
        "⑤ 视觉方向（要什么、不要什么）；⑥ 禁区（用户明确不要的）。\n"
        # 2026-10-24 真机：用户给了「151 分钟」，模型联网查到 162 之后**静默替换**，
        # 用户看到的字幕已经变了而没人告诉他。口径该不该更新是用户的事，
        # 不是模型替他决定的——所以：照抄 + 只提示、不改数。
        "**用户已经给了具体数字，就照抄，不要联网替换成别的口径。**"
        "如果你查到更新的数据或不同口径，**只在回执里说一句**"
        "（例如「你给的 151 分钟是 2024 年报告口径，2025 年报告为 156 分钟」），"
        "让用户自己决定改不改；**不要擅自把数字换掉**——改了又不说明，是最坏的一种。\n"
        "**文档不许承诺下面这份能力边界之外的任何东西**——承诺了做不到的，"
        "比不写文档更糟（用户会以为被骗）：\n"
        f"· 画面全部是**代码逐帧画**出来的（React/TSX + SVG / `<canvas>` / roughjs / "
        "katex / three.js），没有实拍、没有真人、没有照片，人物也做不出表情；\n"
        f"· 转场只由**拼接层**提供（{'、'.join(cspec.TRANSITION_STYLES)}），"
        "镜头代码里没有转场可写；\n"
        f"· 模板通道已关闭（2026-10-23）：没有预置组件可套，每一镜的画法由写手自己画，"
        "所以「每镜长得一样」不靠代码保证，靠风格不变量与全片画法账；\n"
        f"· 画幅 {'、'.join(cspec.ASPECTS)}、帧率 {cspec.FPS_CHOICES}、"
        f"质感 {'、'.join(cspec.TEXTURES)}、色板 {'、'.join(cspec.PALETTE_CHOICES)}、"
        f"字幕样式 {'、'.join(cspec.CAPTION_STYLES)}、高亮样式 "
        f"{'、'.join(cspec.HIGHLIGHT_STYLES)}、音色 6 个（男声/女声，无方言）；\n"
        "· 时长由**旁白字数**决定：要卡准目标秒数，先算字数再定镜数，别指望后面裁。\n"
        "如果用户的原话越过了这条边界（「要实拍感」「要 3D 电影镜头」），"
        "在文档里如实写成「这个做不到，替代方案是…」，不要默默降级、也不要原样承诺。\n"
        # ────────────────────────────────────────────────────────────────────
        "每镜最少交三样：一句 `text`（字幕文案）、一个 `highlight`（必须是文案子串）、"
        "一句 `brief`（这一镜画什么、怎么构图、字幕排在构图的哪一处、怎么分几拍动）——"
        "**`code` 可省**：不写就由系统按 brief、整片 style 与这一镜文案**单独写**这一镜的"
        "React/TSX（每镜一次短调用，写完自动过 lint，最多改两次）。写了 `code` 的镜以你"
        "写的为准。这样几十镜的代码不会挤在主对话上下文里。"
        "画面全部来自代码逐帧画出来——裸 SVG、`<canvas>`、roughjs、three.js、katex 都行，"
        "不联网、不引用任何外部图片。每镜的代码是**自包含**的：顶层 `<AbsoluteFill>` 铺满"
        "自己的画布（没写片壳时是整帧；写了片壳时是 `useBody()` 报的画面带），"
        "**先把这一镜文案里能被画出来的东西圈出来**——一个具体物件、一个人物、一个动作、"
        "一组关系，画面就是它的舞台或特写；**这一镜单独截一帧下来要是一幅画，不是一页排版**"
        "（大标题 + 色块 + 几行字那种）。画法不限制：`@dg/lib/draw/*` 与 `@dg/lib/brush` "
        "只是可选参考件，用不上就自己画。"
        "用户对风格只给了一点点方向时，**在那句方向之上自己组合**媒介、笔触、光线、构图与配色，"
        "把整片的美术语言补齐——是往上加，不是照单执行；整片所有镜共用这一套语言。"
        "**`recurring_prop` 是可选的**：只有用户明确要『同一件东西贯穿全片』时才给一件"
        "在画面世界里反复出现的具体道具；用户没要求就不写，让每一镜按自己的文案画。"
        "写了它，它就会出现在每一镜写码的提示词里，届时全片只此一件。"
        "**用户只给一个选题/一句话时，自己补全**：受众、时长（默认 3~4 分钟）、"
        "叙事结构（现象→机制→此刻→收束）与画法都自己定，不要反问一串；"
        "只有『要不要配音』『配乐用哪一首』这类真正影响成片、又无法从资料里判断的事才问一次。"
        "**用户明确给的参数与禁忌优先于默认**：时长、画幅、配音、配乐曲目、受众、"
        "『不要红色』『不要出现人』这类话，用户说了就照办，不要用默认值稀释；"
        "没提到的维度才由默认补位。风格方向是在用户那句之上继续加，不是覆盖他的原话。"
        "**字幕也是画面的一部分**："
        "每镜的代码要把这一镜的整句排进构图——缺省用 `<CaptionFrame>{(art) => …}</CaptionFrame>` "
        "先分带、再把画面排进 `art` 那一格（字与图由结构保证不重叠）；要咬合版面时用 "
        "`<CaptionText/>` 套定位盒子自己摆；整屏都是画面、找不到能排字的干净区域时，"
        "才用整条 `<CaptionLayer/>`。不许每一镜都在底部丢一个整条预设。"
        "**整片选了片壳（`shell`）时这一条不适用**：纸底、色条、钟、期号与底部字幕由片壳统一画，"
        "每镜只画画面带里的主体与场景（画布用 `useBody()`），brief 也只写主体与场景。"
        "**brief 里要写清两件事：这一镜文案里哪几个词变成了画面上的什么东西（怎么摆、光从哪来），"
        "以及字幕排在哪、和画面里什么元素对齐。**"
        "**每镜还要交 `motion`（按发生顺序写两条）**：写手看不到别的，它就照这两条排拍子；"
        "系统渲一版草稿按像素数变化段，数得出来才算过。每条写清「是什么动效」，"
        "锚用 `subtitle:<本镜第几个词>`（最稳）或 `fields.<本镜字段名>`。"
        "两条都要是**够大的一处变化**——满宽色带擦入、面板展开收拢、成块的东西整体位移；"
        "只做淡入淡出、只让细线或小图标动，像素差数不出来，这一镜会被判成「动了一下就停了」，"
        "写手要重写一遍。两条的起点也要分开（隔开一秒以上最好）。"
        "整片的底色与字幕统一由**顶层入参**承载（见下），每一镜用 "
        "`resolveFilmStyle(getInputProps())` 取那一份，不要写死颜色。"
        "出处要过一道账：本会话被 web_search / fetch_url 真打开过的页面才算查证过，"
        "凭记忆写的出处要标「未核实：…」，冒充查过的带镜号退回。"
        "**会过期的数字**（用户规模、使用时长、份额、价格、排名、政策口径）先查**最新一期**"
        "再取数：检索词带当前年份或「最新/第 N 次报告」，拿到报告先看数据期（出版年不等于"
        "数据年）；文案里写数据期（「2025 年，…」而不是「目前，…」），一类数字只用同一期"
        "口径。最新一期查不到，就用能拿到的最新一版并在 source 里写「数据期：…」；"
        "两期都拿不到，换个不带这个数的说法，别凭记忆填。"
        f"比例：{'、'.join(cspec.ASPECTS)}；帧率白名单：{cspec.FPS_CHOICES}；"
        f"字幕样式：{'、'.join(cspec.CAPTION_STYLES)}；"
        f"高亮样式：{'、'.join(cspec.HIGHLIGHT_STYLES)}；"
        f"每镜最多 {cspec.MAX_FIELDS_PER_SHOT} 个可改旋钮。"
        "有旁白时不要写每镜时长——镜头长度由真实语音时长决定。"
        f"画幅/帧率/旁白开关/音色/语速/字幕两维/质感档/整片色板同时提供**顶层入参**"
        f"（计划卡给用户勾选的位），"
        f"质感可选：{'、'.join(cspec.TEXTURES)}、色板可选：{'、'.join(cspec.PALETTE_CHOICES)}"
        f"（顶层是整片默认档，每一镜可用 shots[].texture / 代码里绑的色板 / 申报的 bg 覆盖它；"
        f"换质感只重跑滤镜、不重烧镜头，换色板要重烧）。"
        "传了会覆盖 spec 里的同名字段。")
    required_nodes: list[str] = []
    require_explicit_call = True
    input_schema = _obj("图形科普片分镜（代码镜）", {
        "spec": {
            **cspec.spec_schema(),
            "description": "分镜 spec：每镜 text + highlight + brief + 可选 code / fields / "
                           "subject / source（逐字段含义见结构）。**code 可省**——缺省的镜由"
                           "系统按 brief 单独写代码；写了 code 的镜以你写的为准。"
                           "本节点是**校验闸**：文案超出字幕容量、高亮词不是文案子串、"
                           "字段声明越界都会带镜号退回，改完重调即可，不烧像素。"
                           "顶层 style 是整片美术语言的起点（用户只给一点方向就在上面加）；"
                           "recurring_prop 可选——只有用户要求贯穿全片时才写。"
                           "source 请如实填出处——机器不判真伪，它会如实出现在分镜卡与逐镜账上。"
                           "**出处还要过一道账**：本会话被 web_search / fetch_url 真打开过的页面"
                           "才算查证过，对不上账的会被打回（没查到就写「未核实：…」）。"
                           "**会过期的数字**先查最新一期；查到的版本不是最新一期时，在 source 里"
                           "写「数据期：<年/月>」，文案里也带上这个数据期。"
                           "aspect/fps/narration/voice/rate/caption_style/highlight_style/texture 也可以走下面的"
                           "顶层开关，顶层的优先（那是用户在计划卡上勾的，不是建议）。"
                           "校验通过后调 render_motion_code 出片。",
        },
        **cspec.knob_props(),
        "target_duration_sec": {
            "type": "number",
            "description": "用户要的目标秒数（每次都要带）。无旁白时系统按比例把各镜驻留"
                           "对齐到它；偏离大到对不齐时在这里打回并给出镜数建议",
        },
        "shots_patch": {
            "type": "array", "items": {"type": "object"},
            "description": "只交要改的那几镜（按 id 替换上一版分镜里的同号镜，新 id 追加到末尾），"
                           "不传 spec。上一版被打回后改几镜就用它，不必把几十镜整份重交。"
                           "只给 {id, brief} 而不再给 code 的镜＝**重画这一镜**（旧代码会被丢掉，"
                           "由系统按新 brief 重写）；要给明确的代码就带上 code",
        },
        # 只在本节点声明：主 Agent 的 MCPTool 只给声明了该参数的工具注入，
        # 其余节点根本收不到这个键。模型不填也能跑（服务端从 state 里补）。
        "style_skill": {
            "type": "string",
            "description": "用户选中的风格包名（可空）。由系统注入，模型不要自己编。",
        },
        # 同上：只在入口勾了「直接出片」时由系统注入为真。模型不填就是普通流程
        # （先出预渲静帧给人看、出片前再确认一次）。
        "go_straight": {
            "type": "boolean",
            # 系统注入的布尔项**不上计划卡**：布尔在 `knob_facts_for` 里默认就会变成
            # 一个可勾的开关，模型照着清单抄一次 `go_straight: true`，这一轮就不出
            # 预渲静帧、也不等确认了。这不是用户点名要的开关，是入口那个「直接出片」
            # 传下来的状态。键名记在 `plan/vocab.py` 的 `SYSTEM_INJECTED_PARAMS`，
            # 计划层按那份名单跳过它（值仍随计划步骤传得到节点）。
            "description": "用户这一轮选了直接出片：不出预渲静帧、出片前不等确认。"
                           "由系统注入，模型不要自己设。",
        },
    })

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        raw = inputs.get("spec")
        # fallback 1：_parse_tool_args 解析失败时返回 {"_raw": 原始串}，
        # 尝试再救一次——原始串可能是整份 {"spec": {...}} 或直接就是 spec 本体。
        if raw is None and isinstance(inputs.get("_raw"), str):
            rescued = _try_rescue_json(inputs["_raw"])
            if isinstance(rescued, dict):
                raw = rescued.get("spec", rescued)
        # fallback 2：LLM 把 spec 内容直接放在顶层（没包 {"spec": ...}），
        # 有 shots 键就当 spec 用。
        if raw is None and "shots" in inputs:
            raw = inputs
        if isinstance(raw, str) and raw.strip():
            raw = _try_rescue_json(raw) or raw
        # 补丁轮：只交改过的那几镜，与本会话上一版分镜（不论那一版是否过了闸）按 id 合并。
        #
        # **`spec` 与 `shots_patch` 同时出现时，两个都当补丁合并**，不是「有 spec 就丢掉
        # patch」。2026-10-18 真机 run 8c5e161ddb20 就栽在这里：模型想「改 6 镜 + 补 16 镜」，
        # 把 6 镜放进 `spec`、16 镜放进 `shots_patch`；旧写法只认 `spec`，patch 被静默丢弃，
        # 于是 32 镜的片子被当成 6 镜去算时长（66s vs 220s）打回。模型看到的报错只说
        # 「偏离超过闸值」，根本看不出是自己交的两份没被合并——它随后改用 patch_motion_code，
        # 又被「必须指定 base_artifact_id」挡住，整条片子白等了二十分钟。
        #
        # 合并顺序固定为「patch 先、spec 的 shots 后」：同一镜在两边都出现时，`spec` 是
        # 模型这一轮更完整的那份，按它覆盖。
        patch = inputs.get("shots_patch")
        # 补丁轮只重渲 / 重评改动过的镜（见下面预览那一段）。整份重交＝全片（None）。
        changed_ids: set[str] | None = None
        if isinstance(patch, list) and patch:
            changed_ids = {str(p.get("id")) for p in patch
                           if isinstance(p, Mapping) and p.get("id")}
            if isinstance(raw, Mapping):
                changed_ids |= {str(s.get("id")) for s in (raw.get("shots") or [])
                                if isinstance(s, Mapping) and s.get("id")}
            base = _DRAFTS.get(state.session_id)
            if base is None:
                raise ValueError(
                    "plan_motion：shots_patch 找不到上一版分镜（服务重启过或本会话还没交过），"
                    "这一次请交完整的 spec")
            raw = merge_submission(base, raw, patch)
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                "plan_motion：spec 必须是对象且非空——每镜至少交一句文案和一句画面 brief"
                "（或一段自己写的 code）")
        # 取材轮排在**一切校验之前**：它根本不是一份分镜（没有 code、没有 aspect），
        # 放进 validate_code_spec 只会得到一屏「code 为空」的清单。判定只看一件事——
        # 这一轮有没有交 code（见 cspec.forage_scan）。
        # 导入的技能：按名字从 PG 取（磁盘种子兜底），把正文注入 spec.skill_contract。
        # 排在 expand/复用之前——复用要比对风格包名，补缺省也要在契约校验之前完成。
        # 取不到、不是 style、不属于本人，都只写一句回执继续，不拦分镜。
        style_skill = str(inputs.get("style_skill")
                          or getattr(state, "style_skill", "") or "").strip()
        # 「直接出片」：入口上勾了就不预渲静帧（省下每镜一次 0.75 倍尺寸的整镜渲染，
        # 以及随之而来的带图视觉评审）。代价写在预览那一段：没有静帧给人看，也没有
        # 「红镜带意见重写一遍」——画面好不好只能等成片。开关是用户在入口上显式选的，
        # 所以这里照办，不再二次确认。
        go_straight = _flag_on(inputs.get("go_straight")
                               if inputs.get("go_straight") is not None
                               else getattr(state, "go_straight", False))
        style_note = ""
        # 「风格：自动」= 空串：**不套任何风格包、不套模板**。用户没点名风格时，
        # 画面只由对话框里那段话决定（`user_request` 原样进每一镜的提示词）。
        # 从前这里会按特征词自动套一份风格包，那等于把视觉规则从对话搬回库里——
        # 用户要的是「我写成什么样，就画成什么样」，所以那一层删了。
        #
        # 2026-10-23 补回来一版，口径收紧到三条（`_auto_pick_own_skill`）：
        #   ① 只在**用户自己导入的**技能里找——系统内置包一个都不碰，所以不会
        #      「不管你说什么都给你套上库里那几套」；
        #   ② 只在**唯一命中**且命中词落在「中文名 / auto_select」上时才挂——不猜；
        #   ③ 挂上了要**写在回执里**，用户看得见是哪一句把他带过去的。
        # 这跟被删掉那版的区别是「谁的话说了算」：那版是库里的规则赢，这版仍然是
        # 用户那句话赢，只是把他自己带进来的那份说明书认了出来。
        # 2026-10-24：从「只挂唯一命中」改成「挂所有对得上的」——用户要一份片子用
        # 多份技能。**代码里不写任何具体技能名**：谁被挂上完全由用户那句话里出现了
        # 哪些名字决定，预算只按字数裁（`SKILL_BUNDLE_PER_MAX` / `MAX_STYLE_BODY_CHARS`）。
        picked_names: list[str] = []
        if style_skill:
            picked_names = [style_skill]
        else:
            picked_names, auto_note = await _auto_pick_own_skills(
                self.storage, state.user_id,
                getattr(state, "user_request", "") or "")
            if auto_note:
                # 「对上了几份」也要落地：用户得知道是哪一句话把技能带进来的。
                state.summary.notes.append(auto_note)
        if picked_names:
            raw, attached_note = await _attach_skill_contracts(
                self.storage, raw, picked_names, state.user_id)
            style_note = "；".join(n for n in (style_note, attached_note) if n)
            if style_note:
                state.summary.notes.append(style_note)
        # `code_from` 先展开：复用别镜代码的那一镜算「带了 code」，不该被判成取材/写码混交。
        raw = cspec.expand_code_refs(raw)
        # 整份重交时复用上一版已写好的代码（brief 逐字相同才复用；补丁轮不走这里）。
        # 必须在 `forage_scan` 之前：复用回来的 code 让这一镜算「带了 code」。
        reused_ids: list[str] = []
        if not (isinstance(patch, list) and patch):
            raw, reused_ids = reuse_coded(_DRAFTS.get(state.session_id), raw)
        mode, forage = cspec.forage_scan(raw)
        # 2026-10-18：`DEMOS_ENABLED=False` 时**没有取材轮**，也没有「混交」要分。
        # 不带 code 的镜不再打回——它们带着 `brief` 进来，由 `shot_writer` 逐镜写代码
        # （见下面 `write_shot_code` 那一段）。`forage` / `mixed` 这两个模式因此只在
        # demo 语料还开着的部署里有意义。
        if not cspec.DEMOS_ENABLED:
            pass
        elif mode == "mixed":
            ids = "、".join(f"{r['id']}（{r['card'] or '未声明卡'}）" for r in forage)
            raise ValueError(
                "plan_motion：这次调用有的镜交了 code、有的没交（没交的：" + ids + "）"
                "——服务端分不出「漏填」与「想先取材」，所以这两种意图要各调一次："
                "取材轮**所有镜都不带 code**（带 card 与 derived_from），"
                "分镜轮**所有镜都带 code**。没入库，改完重调。")
        if cspec.DEMOS_ENABLED and mode == "forage":
            account, errs = cspec.collect_demos(forage)
            if errs:
                raise ValueError(
                    "取材轮未通过，一份 demo 的字节都没回（照清单改完再调本工具）：\n"
                    + "\n".join(f"  · {e}" for e in errs[:12])
                    + (f"\n  · …另有 {len(errs) - 12} 条同类错误" if len(errs) > 12 else ""))
            f_target = _resolve_target_duration(state, inputs, None)
            if f_target is None and raw.get("target_duration_sec"):
                f_target = max(3.0, float(raw["target_duration_sec"]))
            # 取材轮也按常见写法归一：`16:9` 直接拿去查 `CAPTION_ZONE_PX` 会静默落到
            # 默认档，字幕带留白的提示就按错画幅说了一遍。
            aspect = (cspec.norm_aspect(inputs.get("aspect") or raw.get("aspect"))
                      or "landscape")
            return forage_payload(account, target=f_target, shots=len(forage),
                                  aspect=aspect,
                                  shell=str(raw.get("shell") or ""))
        # 顶层开关先并进来再校验：卡上勾了 landscape 就得渲成横屏，模型在 spec 里写的
        # portrait 让位；并早并晚都要过校验器，越界照样回清单。
        # 质检档：spec 没写就按产品缺省（MOTION_QC，缺省 preview——见 cspec.QC_MODES）。
        # 整份重交时，与上一版比一下镜号：少了的、断号的都要说出来（见 `_shots_replacement`）。
        # 必须在 `_remember_draft` 覆盖上一版**之前**取。
        replace = (None if isinstance(patch, list) and patch else
                   _shots_replacement(_DRAFTS.get(state.session_id), raw))
        replace_notes = ([] if not replace else
                         ([replace["note"]] if replace.get("note") else [])
                         + list(replace.get("id_notes") or []))
        # 写码轮的这一版先记成草稿：被下面任何一处打回，下一次都可以只用 shots_patch 改那几镜。
        _remember_draft(state.session_id, raw)
        # 读不出的字色由系统就地修好（纯算术，不该为它打回重写整份分镜）。
        raw, contrast_notes = cspec.autofix_ink_contrast(raw)
        # 这里拦的是**契约**（字段/容量/类型/归一）——「这份 spec 能不能用」。
        # 整片判据（每镜必画、字色对比度、派生相似、动效锚…）**不在这里拦**了
        # （2026-10-16）：它们在下面以 `style_findings` 附进正常返回，模型交活前
        # 可以用 `check_film` 自查同一份判据。契约不满足时照旧抛——那不是判据，
        # 是输入本身不成立，硬渲下去只会把「少写了一镜的代码」拖到烧完像素之后才现形。
        spec, errors = cspec.validate_code_spec(
            cspec.overlay_knobs(cspec.with_default_qc(raw), inputs))
        if errors:
            raise ValueError(
                "代码分镜校验未通过，没有入库（照清单改完再调本工具；只改了几镜就用"
                " shots_patch 只交那几镜）：\n"
                + "\n".join(f"  · {e}" for e in errors[:12])
                + (f"\n  · …另有 {len(errors) - 12} 条同类错误" if len(errors) > 12 else ""))
        # **时长先判（2026-10-23）**：写手是分钟级开销，等它写完才说「太长」= 白烧一轮。
        # 真机 run（15:0x）：407 秒写完 12 镜，然后才发现估算 94~146 秒、目标 60 秒。
        # 判据是纯算术（文案字数 ÷ 语速），一分钱不花，所以**必须排在写手之前**。
        #
        # 阈值取 1.35（下面入库前那一道是 1.6）：1.6 让「94 vs 96」正好漏过去——
        # 差 2 秒不拦，等于这一道形同虚设。1.35 与 `check_target` 的提醒线同值。
        _early_target = _resolve_target_duration(state, inputs, None)
        if _early_target is None and raw.get("target_duration_sec"):
            _early_target = max(3.0, float(raw["target_duration_sec"]))
        if _early_target:
            spec, _fit_notes = cspec.fit_duration(spec, _early_target)
            _early_blocked = cspec.duration_block(spec, _early_target, long_factor=1.35)
            if _early_blocked:
                raise ValueError(
                    _early_blocked
                    + " 改完重调本工具——**分镜没有入库，也还没开始写代码**，"
                      "所以这一次不算白跑；别等写完 12 镜再说。")
        # 贯穿人物没写 `protagonist`：只报不拦。写码马上要开始，这句排在写手之前，
        # 模型看到回执还来得及补交顶层这一栏（主角约定进复用键，补了会整片重写）。
        if not spec.get("protagonist"):
            note = missing_protagonist_note(spec)
            if note:
                state.summary.notes.append(note)
        # 环境自检（2026-10-19）：在**开始花钱写码之前**确认渲染容器还活着。
        # 教程里那句「环境摩擦可能比模型能力更致命」在真机上是成立的：chromium 起不来
        # 时写手照样会把二十几镜写完，等第一镜真渲才发现——那才是真正的白烧。
        # 默认只报不拦（「堵死比白烧更坏」）：把代价写在回执最前面，让模型自己决定
        # 要不要先修；只有显式 `MOTION_PREFLIGHT_STRICT=1` 才把它升级成打回。
        env_note = await _preflight_note()
        if env_note and os.environ.get("MOTION_PREFLIGHT_STRICT", "") == "1":
            raise ValueError(env_note + " 已设 MOTION_PREFLIGHT_STRICT=1，这一轮不写码；"
                                       "先修好渲染环境再重试。")
        # 逐镜写代码（2026-10-18）：主对话只决定「这一镜画什么」（`brief`），每一镜的
        # TSX 由 `shot_writer` 用**短上下文单独一次调用**写。排在契约校验**之后**——
        # 一份文案超长、字段越界的 spec 不值得花写手的钱；排在 lint/预览**之前**，
        # 后面所有闸看到的就是同一份完整代码。
        # 已经带了 `code` 的镜（局部改、或模型自己写的）一个字节都不动。
        writer_notes: list[str] = []
        writer_cost = 0.0
        # 2026-10-24：**全片都带 code 时，这三步一个都不该跑。**
        # 导演与写手的 `todo` 都按「没有 code 的镜」筛，空表就直接返回；而
        # `recipe_index.assign()` 不筛，它会照跑一次、把配方卡写进
        # `film_ledger.recipes`——但那一份只有写手的 `_shot_block` 会读，
        # 写手没跑就等于白花一次调用（真机 2026-10-24：12 镜全带 code，
        # 分配照样跑了）。
        needs_code = any(not str(s.get("code") or "").strip()
                         for s in (spec.get("shots") or []) if isinstance(s, Mapping))
        if not needs_code:
            state.summary.notes.append(
                "全片每一镜都已带 code，跳过美术导演 / 配方卡分配 / 逐镜写手"
                "（这三步只服务「只有 brief 的镜」）。")
        if not cspec.DEMOS_ENABLED and needs_code:
            # 美术导演（2026-10-18）：一次纯文本调用，把整片风格拆成可执行做法，
            # 并把每一镜的 brief 补成「画什么物件、在哪、多大、光从哪来、字排哪」。
            # 排在写手之前——真机上「不像像素赛博朋克」的 14 镜，根因是 brief 太抽象，
            # 写手没有落点。这一步让第一次落笔就有具体画面；失败不拦，原 brief 照用。
            directed = await art_director.concretize(
                spec, user_id=state.user_id or "",
                conversation_id=state.conversation_id or "",
                user_request=getattr(state, "user_request", "") or "")
            # 配方卡分配（2026-10-23）：`shots/` 下 136 篇做法文档被 SKILL.md 的
            # `exclude: demos/, shots/` 排除掉了，写手只看得到正文的**原则**、看不到
            # **手法**（实测后果：s01/s09 被闸⑭ 数出 0 个变化段，整镜是一张静帧）。
            # 一次调用把配方卡分到各镜，写手照着它的节拍与参数拍。
            # **排在导演之后**：导演补过的 brief 更具体，分配更准。
            # 分配失败不影响写手——那只是退回「只有原则没有手法」的老路。
            rec = await recipe_index.assign(
                spec, storage=self.storage, user_id=state.user_id or "",
                conversation_id=state.conversation_id or "")
            if rec["assigned"]:
                directed["notes"].append(
                    f"配方卡：{len(rec['assigned'])} 镜配到了具体手法"
                    f"（从 {len(recipe_index.names())} 篇做法文档里挑的），"
                    "已随这一镜的 prompt 交给写手。")
            elif rec["note"]:
                directed["notes"].append(f"配方卡：{rec['note']}")
            wrote = await shot_writer.write_shot_code(
                spec, user_id=state.user_id or "",
                conversation_id=state.conversation_id or "",
                user_request=getattr(state, "user_request", "") or "",
                storage=self.storage)
            # 导演的账不能丢：它排在写手之前；若直接用写手那一份覆盖，导演的花费与
            # 「未判/补了美术方向」就永远到不了回执（2026-10-19 修）。
            writer_notes = list(directed["notes"]) + list(wrote["notes"])
            writer_cost = (float(directed["cost_yuan"] or 0.0)
                           + float(wrote["cost_yuan"] or 0.0)
                           + float(rec["cost_yuan"] or 0.0))
            if reused_ids:
                head = "、".join(reused_ids[:8]) + ("…" if len(reused_ids) > 8 else "")
                writer_notes.append(
                    f"复用：{len(reused_ids)} 镜（{head}）的 brief 与本会话上一版逐字相同，"
                    "直接沿用了已写好的代码——没有重写、也没有重渲草稿。"
                    "要重画其中某一镜，用 shots_patch 只交那一镜的新 brief。")
            # **先存草稿再判失败**（2026-10-24 修）：原来这一句在「有几镜没写成」的
            # raise 之后，于是**失败的那一轮草稿被丢掉**——重试时 12 镜全重写，
            # 写手那 0.5 元白烧一遍。真机 run `3cddbe30dbef`：s02/s03 三试不过，
            # 整份打回，下一轮又从 s01 开始写。
            # 存了之后 `reuse_coded` 会命中已写成的 10 镜，只重写缺的那两镜。
            _remember_draft(state.session_id, spec)
            if wrote["unwritten"]:
                raise ValueError(
                    "逐镜写代码时有几镜没写成，分镜没有入库（"
                    + "、".join(wrote["unwritten"]) + "）：\n"
                    + "\n".join(f"  · {n}" for n in writer_notes[:12])
                    + "\n**已写成的镜已经存进草稿**，重调本工具时它们会直接复用、"
                    "不会再花一次写手的钱。把没写成的这几镜的 brief 写得更具体"
                    "（画什么、怎么构图、怎么动），或者直接交这一镜的 code。")
            # 草稿换成**带代码**的这一版：下一次 shots_patch 只改动过的那几镜，其余镜
            # 的代码直接复用，不会整片重写（那会把写手的钱再花一遍）。
            _remember_draft(state.session_id, spec)
            # 闸⑯ 的回写环（2026-10-23）：申报了 three/katex/rough/canvas 却写成手写
            # SVG 的镜，带着「必须真用上」这条意见重写。**排在预览之前**——它不烧像素、
            # 只判代码字节，是最便宜的一环；等到预览之后才发现就白渲了一轮静帧。
            chan = await self._channel_loop(state, spec)
            if chan["rewritten"]:
                writer_notes.append(
                    f"画法通道：{'、'.join(sorted(chan['rewritten']))} 申报了高级通道，"
                    f"代码里却只有手写 SVG，已按「申报必须有证据」重写"
                    f"（{len(chan['rounds'])} 轮）。")
            if chan["skipped"]:
                writer_notes.append(
                    f"画法通道：{'、'.join(sorted(set(chan['skipped'])))} 重写没成，"
                    "保留上一版——这几镜的申报与实现仍不一致，出片前最好手工看一眼。")
            writer_cost += float(chan["cost_yuan"] or 0.0)
        # 目标时长：本次入参 > 会话 flags > spec 里写的那一份（模型常在取材轮带上、
        # 写码轮漏掉——真机 run 79dcdc0ec0a8 就是写码轮 target=None，时长闸被推迟到确认之后）。
        target = _resolve_target_duration(state, inputs, None)
        if target is None and raw.get("target_duration_sec"):
            target = max(3.0, float(raw["target_duration_sec"]))
        # 时长在**入库之前**对齐与把关：无旁白时系统自己把驻留算到目标上；
        # 算不到（或有旁白偏离太多）就在这里打回——而不是等用户点了确认、渲染口才拦。
        spec, duration_notes = cspec.fit_duration(spec, target)
        blocked = cspec.duration_block(spec, target)
        if blocked:
            raise ValueError(blocked + "改完重调本工具（分镜没有入库）")
        spec["target_duration_sec"] = target
        # 对不上账的「查证过」出处**就地降级**成明写的未核实，不再把整份分镜退回去
        # 重交（2026-10-19 真机：模型连抓 23 页、重交 4 次，十分钟卡在这一步）。
        source_notes, gate = await settle_sources(state.session_id, spec["shots"])
        # 要上字幕却没摆 `<CaptionLayer/>` 的镜，系统就地补上（见 `cspec.autofix_caption_layer`）。
        # 排在 lint **之前**：补进去的那一行 import 与元件要和模型写的代码一起过闸①。
        # 排在校验**之后**：生效的字幕样式要等卡面勾选（`overlay_knobs`）并进来才定。
        spec, caption_notes = cspec.autofix_caption_layer(spec)
        # `<CaptionFrame>` 自己画了字幕，又摆一份 `<CaptionText/>` 就会叠成重影
        # （2026-10-19 真机）。这一步只删第二份，不改判据、不打回。
        spec, dup_notes = cspec.autofix_duplicate_caption(spec)
        # 整屏 `<feTurbulence>` 颗粒改成 `<Grain/>`（2026-10-20 提速）。原来的写法每帧都
        # 把整幅 1920×1080 的噪声重算一遍——真机一镜 292 帧、每帧 ~0.8 秒，一半以上烧在
        # 这层颗粒上；`<Grain/>` 用固定种子的噪声贴图，只栅格化一次。画面参数（浓度、
        # 种子）照搬，不改判据、不打回。
        spec, grain_notes = cspec.autofix_film_grain(spec)
        # 整片字幕的缺省收敛到「白字黑边」（2026-10-20 用户点名）。排在卡面勾选之后：
        # 用户自己钉的那一档不在这里，只有模型顺手写的 `on_picture`（无描边）被换掉。
        spec, cap_default_notes = cspec.autofix_caption_default(
            spec, user_request=getattr(state, "user_request", "") or "")
        # 叠化 + 逐镜烧进画面的字幕 = 两行字叠在一起（2026-10-22 真机）。只把
        # 「两侧都有字幕」的接缝改硬接；其余接缝照用户选的那一档。排在补字幕之后：
        # 这一步要知道哪几镜真的有字。
        spec, cap_trans_notes = cspec.autofix_caption_transition(spec)
        caption_notes = (list(caption_notes) + list(dup_notes) + list(grain_notes)
                         + list(cap_default_notes) + list(cap_trans_notes))
        # 闸① 提前到入库前：原先 lint 只在渲染口里跑，一镜代码写错要等前面几镜都烧完、
        # 渲到它才现形，然后整份重写重提。这里所有镜并行过一遍（不烧像素，每镜几百毫秒）。
        # **这一道照旧拦**：它不是判据，是"这段字节编译不编译得过"——一份过不了闸① 的
        # 代码渲不出任何一帧，放它入库只是把失败推到烧完像素之后（与「闸变工具」正交：
        # 模型交活前有 `check_shot_code` 自查同一份）。
        # 容器连不上／预检这一趟没量到时**不拦**，但也不许读成"过了"——那几镜进
        # `lint_unjudged`，如实写在回执上（未判 ≠ 通过）。
        up = await _remotion_up()
        lint_errs, lint_unjudged = await _prelint(spec["shots"]) if up else (
            [], [f"闸① 未判——渲染容器这一趟不可达，{len(spec['shots'])} 镜的代码"
                 f"一段都没过 lint。**这不是通过**，渲染口还会再判一次。"])
        if lint_errs:
            raise ValueError(
                "闸① lint 未通过，分镜没有入库（只改报错的那几镜的 code，用 shots_patch "
                "只交那几镜再调本工具）：\n"
                + "\n".join(f"  · {e}" for e in lint_errs[:12])
                + (f"\n  · …另有 {len(lint_errs) - 12} 条同类错误"
                   if len(lint_errs) > 12 else ""))
        # 没申报动效的镜要**在烧像素之前**点名：闸㉑ 判在出片那一步，像素上数得出
        # ≥2 段而这一镜一条 motion 都没写时整镜退回（`check_motion_windows`）——
        # 那时前面几镜已经烧完。这里只报不改（申报是模型决定的事，系统替它编锚只会
        # 让闸㉑ 的账变成「永远未判」），模型看到回执还来得及用 shots_patch 补。
        undeclared = [str(s.get("id") or f"s{i + 1:02d}")
                      for i, s in enumerate(spec["shots"])
                      if not (isinstance(s.get("motion"), (list, tuple)) and s["motion"])]
        motion_notes = ([f"动效申报：{'、'.join(undeclared)} 这一镜一条 motion 都没写——"
                         "出片时闸㉑ 会按像素数变化段，数得出 ≥2 段就退。每处动效补一条 "
                         "{kind, anchors}（anchors 写 `fields.<本镜字段>` / "
                         "`subtitle:<第几个词>` / `beat:<第几拍>`）。"]
                        if undeclared else [])
        payload = plan_payload(spec, target=target, gate=gate)
        if env_note:
            # 放在最前面：模型读回执是从上往下读的，环境问题排在账目后面就会被跳过。
            payload = {"env_warning": env_note, **payload}
        # 导入的技能只报名字：正文是给写手/导演的，不必再回一遍主对话上下文。
        contract = (spec.get("skill_contract")
                    if isinstance(spec.get("skill_contract"), Mapping) else {})
        if contract:
            payload["skill_contract"] = {
                "name": str(contract.get("name") or ""),
                "display": str(contract.get("display") or ""),
            }
        if style_note:
            payload["style_note"] = style_note
        if lint_unjudged:
            payload["lint_unjudged"] = lint_unjudged
        if duration_notes:
            payload["duration_notes"] = duration_notes
        if contrast_notes or caption_notes or writer_notes or motion_notes or source_notes:
            payload["autofix_notes"] = (
                source_notes + contrast_notes + caption_notes + writer_notes + motion_notes)
        if writer_cost:
            # 美术导演 + 逐镜写手的钱记进这条 run 的预算（`agent._charge_nodes` 读
            # `node_cost_yuan`）。与视觉评审的花费合并报，不互相覆盖。
            payload["node_cost_yuan"] = round(
                float(payload.get("node_cost_yuan") or 0.0) + writer_cost, 4)
        if replace_notes:
            payload["replace_notes"] = replace_notes
        if replace:
            # 结构化那一份（去掉了哪些、多了哪些分开成两栏），给卡面与回执直接读。
            payload["shots_replacement"] = replace
        # 预览静帧（主流的「抽几帧看」）：每镜一张小图，渲染前的计划确认卡上给人看。
        # 容器不通、或关了这一档（MOTION_PREVIEW_STILLS=0）就没有图——卡照样弹。
        # 「直接出片」也走这一条：用户选了不预览，这几分钟本来只为了让他在确认卡上
        # 看一眼；连带省掉的带图视觉评审就是那个选择的价格（少一次红镜重写）。
        if go_straight and up:
            state.summary.notes.append(
                "直接出片：这一版没有预渲静帧，画面以成片为准。")
        if (up and not go_straight
                and os.environ.get("MOTION_PREVIEW_STILLS", "1") not in ("0", "false", "no")):
            # 补丁轮只渲改动过的镜：上一版的预览图还在 `_PREVIEW_REFS` 里，
            # 合并后卡面仍是全片。整份重交＝全片重渲（并清掉旧缓存）。
            repo_key = (state.session_id, state.artifact_id or "_default")
            only = (changed_ids
                    if changed_ids and _PREVIEW_REFS.get(repo_key) else None)
            if only is None:
                _PREVIEW_REFS.pop(repo_key, None)
            refs, notes, layouts, files, failures = await self._publish_previews(
                state, spec, only=only)
            # 代码编译不过的镜：趁卡还没弹，让写手照着渲染器的报错重画一版再渲一张。
            # 缺一格预览图只是难看；带一段编译不过的代码进渲染是整片中停——而用户
            # 看到「无预览」时并不知道自己该做什么。只修这一类，超时那类不碰。
            broken = sorted(sid for sid, f in failures.items()
                            if str(f.get("kind") or "") == "final")
            if broken:
                fixed, repair_notes, repair_cost = await self._repair_broken_shots(
                    state, spec, failures, broken)
                notes.extend(repair_notes)
                if repair_cost:
                    payload["node_cost_yuan"] = round(
                        float(payload.get("node_cost_yuan") or 0.0) + repair_cost, 4)
                if fixed:
                    refs2, notes2, layouts2, files2, bad2 = await self._publish_previews(
                        state, spec, only=set(fixed))
                    refs.update(refs2)
                    files.update(files2)
                    layouts.update(layouts2)
                    notes.extend(notes2)
                    for sid in fixed:
                        failures.pop(sid, None)
                    failures.update(bad2)
            if failures:
                # 结构化的一份：确认卡按它写「哪几镜的画面没能生成、继续会在那里停」。
                payload["preview_failures"] = {
                    sid: str(f.get("kind") or "transient")
                    for sid, f in failures.items()}
            merged = _remember_previews(
                state.session_id, state.artifact_id or "_default", refs, spec)
            if merged:
                payload["preview_frames"] = merged
            if notes:
                payload["preview_notes"] = notes[:12]
            # 预览这一帧上量到的字压字 / 字压图 / 出画：**只报不拦**，但写明改法——
            # 这是烧像素之前最后一次便宜的修改机会（`shots_patch` 只交那几镜）。
            findings = crender.preview_layout_findings(layouts)
            if findings:
                payload["preview_layout"] = findings[:16]
                payload["preview_layout_hint"] = (
                    f"预览帧上量到 {len(findings)} 处版面问题（字压字 / 字压图 / 出画）。"
                    "**不影响入库，可直接出片。**要改就用 shots_patch 只交这几镜："
                    "把撞上的那块字或图挪开，画面最底下那一条留给字幕；"
                    "确属刻意的叠压可以不改。")
            # 视觉闭环：一次带图调用同时判「这一帧作为一张画够不够好」与「像不像
            # 整片要的那套风格」，红镜带着评审意见和它自己那张静帧重写，只重渲改过的
            # 镜，**再看一轮**（`_visual_loop`，最多 `review_rounds` 轮）。
            # **只报不拦**——评审红绝不打回分镜。
            #
            # 2026-10-18 把原来的 `selfcheck.style_review` 并进这里：同一张静帧分两次问，
            # 每镜多一次多模态往返；更贵的是风格不符的镜当时没有重写入口，清单只能交回
            # 主对话，主对话再走一整轮 plan_motion（全片重写码、重渲、重评）。
            if files and design_review.review_enabled(spec):
                art = spec.get("art") if isinstance(spec.get("art"), Mapping) else {}
                loop = await self._visual_loop(
                    state, spec, refs=refs, files=files, notes=notes,
                    failures=failures, art=art)
                spent = float(loop["cost_yuan"])
                visual = loop["first"]
                # 评审那一轮可能**升级了申报**（`_bump_channel_by_review`：评审说「该有
                # 体积」，于是把这一镜从 vector 升成 three）。升级之后如果代码还是 SVG，
                # 闸⑯ 就会报「申报无证据」——再跑一次回写环，让升级真的落到代码上。
                # 只跑 1 轮：这一趟是收尾，不是新一轮迭代。
                chan2 = await self._channel_loop(state, spec, rounds=1)
                if chan2["rewritten"]:
                    notes.append(
                        f"通道升级后重写：{'、'.join(sorted(chan2['rewritten']))}"
                        "（评审说画面该有体积/该是公式，申报已升级，代码已按新通道重画）。")
                if chan2["skipped"]:
                    notes.append(
                        f"通道升级后仍没写成：{'、'.join(sorted(set(chan2['skipped'])))}"
                        "——这几镜的申报比代码高，出片前请手工看一眼。")
                spent += float(chan2["cost_yuan"] or 0.0)
                fixed_all = sorted(sid for sid, r in loop["rewritten"].items()
                                   if r.get("ok"))
                payload["preview_frames"] = _remember_previews(
                    state.session_id, state.artifact_id or "_default", refs, spec)
                payload["preview_notes"] = notes[:12]
                if failures:
                    payload["preview_failures"] = {
                        sid: str(f.get("kind") or "transient")
                        for sid, f in failures.items()}
                if fixed_all:
                    # plan_table 里 code_chars 是重写前算的；改过的镜要刷新，否则
                    # 卡面上的字节数还指着上一版。
                    payload["plan_table"] = plan_payload(
                        spec, target=target, gate=gate)["plan_table"]
                last = loop["last"] if len(loop["rounds"]) > 1 else {}
                payload["visual_review"] = {
                    # 补丁轮只复查改动过的镜（省掉全片重渲重评）；整份重交是全片。
                    "scope": "changed" if only else "all",
                    "checked": visual["checked"],
                    "passed": visual["passed"][:24],
                    "failed": visual["failed"][:16],
                    "unjudged": visual["unjudged"][:24],
                    "rewritten": loop["rewritten"],
                    "skipped": loop["skipped"],
                    # 每一轮各看了几镜、改了哪几镜、哪几镜是同一处意见停了手。
                    "rounds": [{
                        "round": row["round"],
                        "checked": row["checked"],
                        "passed": row["passed"][:24],
                        "failed": [str(f.get("shot")) for f in row["failed"]][:16],
                        "fixed": row.get("fixed", []),
                        "stale": row.get("stale", []),
                        "cost_yuan": row["cost_yuan"],
                    } for row in loop["rounds"]],
                    "after": ({"passed": last.get("passed", []),
                               "failed": last.get("failed", [])[:16],
                               "unjudged": last.get("unjudged", [])}
                              if last else {}),
                }
                if visual["failed"]:
                    # 头一句必须是「已入库、可出片」：真机 run 901597a97570 里模型先读到
                    # 33 镜「仍红」，再把 37 镜的 brief 整份重交了一遍（9217 补全 token，
                    # 约 21 分钟），画面并没有变好——红的是写码层与评审口径，不是 brief
                    # 少写了一句描述。它原来的位置在清单末尾，等于没说。
                    lines = ["**分镜已入库，可以直接出片；这一轮不需要再交 plan_motion。**"]
                    lines.append(f"带图看了 {visual['checked']} 镜，{len(visual['failed'])} 镜"
                                 "按五条评审没通过（画面作为一张画还不成立）。")
                    if fixed_all:
                        lines.append(f"已自动按评审意见重写并重渲（共 {len(loop['rounds'])} 轮"
                                     f"看片）：{'、'.join(fixed_all)}；"
                                     "改后的结果见 visual_review.after。")
                    if loop["still_red"]:
                        lines.append(f"仍在红：{'、'.join(loop['still_red'])}——照交，"
                                     "**不影响入库**。")
                    if loop["stale"]:
                        lines.append(f"同一句意见改过一次还是同一句，已停手："
                                     f"{'、'.join(sorted(set(loop['stale'])))}"
                                     "（再改一轮只是同一个来回）。")
                    if loop["skipped"]:
                        lines.append(f"没改成的镜：{'、'.join(loop['skipped'])}"
                                     "（重写没写成，已保留上一版能跑的代码）。")
                    lines.append("**不要再整份重交分镜**：整份重交＝逐镜重写代码、重渲静帧、"
                                 "重评一轮，红镜不会因为 brief 换一种说法就变好。"
                                 "要改只改你确实要换内容的那几镜（shots_patch 只交那几镜），"
                                 "其余镜原样留着；成片后也可以用 patch_motion_code 单镜重画。")
                    payload["visual_review_hint"] = "\n".join(lines)
                if spent:
                    payload["node_cost_yuan"] = round(
                        float(payload.get("node_cost_yuan") or 0.0) + spent, 4)
        # 主对话只拿瘦身版：motion_spec 里的逐镜 code 留在库里供渲染，交回模型的是
        # 「哪一镜几个字节」——二十镜代码整份回到上下文，正是要避免的那件事。
        payload["__model_view__"] = plan_model_view(payload)
        return payload

    async def _publish_previews(self, state: NodeState, spec: dict[str, Any],
                                *, only: set[str] | None = None
                                ) -> tuple[dict[str, str], list[str], dict[str, Any],
                                           dict[str, Any], dict[str, dict[str, str]]]:
        """渲每镜预览静帧并发布 → ({镜号: 对象键}, 说明, {镜号: 版面账}, {镜号: 本地图片},
        {镜号: {kind, detail}})。

        ``only`` 只渲这几镜（视觉评审改过的镜要重渲，不必把整片再渲一遍）。
        """
        sess = state.session_id
        artifact = state.artifact_id or "_default"
        target = spec
        if only is not None:
            target = {**dict(spec), "shots": [
                s for s in (spec.get("shots") or [])
                if str(s.get("id") or "") in only]}
        files, notes, layouts, failures = await crender.preview_stills(
            target, client=rc.endpoints(), work=self._work(state, "motion", "preview"),
            storage=self.storage)
        refs: dict[str, str] = {}
        for sid, path in files.items():
            key = f"renders/{_safe(sess)}/{_safe(artifact)}/preview/{_safe(sid)}.jpg"
            try:
                await self.storage.workspace.publish(str(path), key,
                                                     content_type="image/jpeg")
                refs[sid] = key
            except Exception as exc:  # noqa: BLE001 - 缺一张预览不拦
                notes.append(f"{sid}：预览图没发布（{type(exc).__name__}）")
        return refs, notes, layouts, files, failures

    async def _channel_loop(self, state: NodeState, spec: dict[str, Any],
                            *, rounds: int = 2) -> dict[str, Any]:
        """闸⑯ 的回写环：申报了画法通道却没真用上，就让写手带着这条意见重写那几镜。

        为什么要有它（2026-10-23）：`whole_film_gate_results` 的出口**一个都不抛**
        （那是「闸变工具」的设计），所以闸⑯ 判出「申报 three 而代码里是 vector」
        也只是回执里的一句话——12 镜全写成手写 SVG，分镜照样入库。

        而实测表明这**不是能力问题**：同一份 s07 brief（真 3D 心脏），用 three 写出来
        是「有体积、有光照、有接触阴影」的画面，手写 SVG 只是「扁平色块 + 一个白点」。
        写手不写 three，是因为「不写」没有后果。

        **这一环不烧像素**：判据只看代码字节（`shot_used_channels`），一次重写就是一次
        纯文本调用，比视觉评审便宜一个量级。所以排在预览之前——最便宜的那一环先做。

        → ``{rewritten, skipped, cost_yuan, rounds}``。只报不抛。
        """
        out: dict[str, Any] = {"rewritten": {}, "skipped": [], "cost_yuan": 0.0,
                               "rounds": []}
        for round_no in range(1, max(1, rounds) + 1):
            rows = [s for s in (spec.get("shots") or []) if isinstance(s, dict)]
            errs, _w, _u = cspec.whole_film_gate_results(rows)
            bad: dict[str, tuple[str, str]] = {}
            for e in errs:
                m = re.search(r"闸⑯ 主体具名：(s\w+)：申报 subject_channel=(\w+)，"
                              r"而这一镜代码里用的是([\w、]+)", e)
                if m:
                    bad[m.group(1)] = (m.group(2), m.group(3))
            if not bad:
                break
            by_id = {str(s.get("id")): s for s in rows}
            sem = asyncio.Semaphore(4)

            async def one(sid: str, declared: str, actual: str):
                shot = by_id.get(sid)
                if not shot:
                    return sid, None
                critique = (
                    f"这一镜申报的 `subject_channel` 是 `{declared}`，但代码里一条 "
                    f"{declared} 的 API 都没用上（实际只用了 {actual}）。\n"
                    f"**这一版必须真的用 {declared} 画主体。**骨架就在这份 prompt 的"
                    f"「另外四条画法通道：能抄的骨架」那一段，照它写："
                    + _CHANNEL_HINT.get(declared, "") + "\n"
                    f"注意：`subject_channel` 是分镜字段，你改不了它（这一趟只回代码）。"
                    f"所以「把申报改成 {actual}」不是出路——**必须让代码里真的出现 "
                    f"{declared}**：import 了还得在代码体里真用上，只 import 一行不算。\n"
                    f"画面主体就是这一镜的 `subject`；用它该有的体积、光照、材质去画，"
                    f"不要退回到「一圈描边 + 一块平涂」。")
                async with sem:
                    got = await shot_writer.rewrite_shot_code(
                        shot, critique=critique, spec=spec,
                        user_id=state.user_id or "",
                        conversation_id=state.conversation_id or "",
                        user_request=getattr(state, "user_request", "") or "")
                return sid, got

            results = await asyncio.gather(
                *(one(sid, d, a) for sid, (d, a) in bad.items()))
            row: dict[str, Any] = {"round": round_no, "targets": sorted(bad),
                                   "ok": [], "failed": [], "cost_yuan": 0.0}
            for sid, r in results:
                if not r:
                    out["skipped"].append(sid)
                    continue
                row["cost_yuan"] += float(r.get("cost_yuan") or 0.0)
                if r.get("ok"):
                    row["ok"].append(sid)
                    out["rewritten"][sid] = str(r.get("note") or "")
                else:
                    row["failed"].append(sid)
                    if sid not in out["skipped"]:
                        out["skipped"].append(sid)
            out["cost_yuan"] += row["cost_yuan"]
            out["rounds"].append(row)
            if not row["ok"]:
                break
        return out

    async def _visual_loop(self, state: NodeState, spec: dict[str, Any], *,
                           refs: dict[str, str], files: dict[str, Any],
                           notes: list[str],
                           failures: dict[str, dict[str, str]],
                           art: Mapping[str, Any]) -> dict[str, Any]:
        """渲完静帧之后：看 → 按意见改 → 重渲那几镜 → **再看**，最多 `review_rounds` 轮。

        为什么要有第二眼：`rewrite_shot_code` 只保证「改出来的代码过闸①」，不保证
        「画面变好了」——2026-10-18 真机上 34 镜按文字意见重写、重评仍有 26 镜红；
        个别镜还会越改越差。只有把改后的图再看一眼，才知道这一轮是不是白改。

        反过来，同一句意见上一轮已经照着改过、重渲之后评审还是同一句：那是写手
        理解不了这条意见（或这条意见在要求一件代码画不出的东西），再改一轮只是
        同一个来回。这种情况停手，把镜号如实写进回执。

        只报不拦，也不抛。原地更新 ``refs`` / ``files`` / ``notes`` / ``failures``
        （调用方还要拿它们写回执与卡面），返回每一轮的账。
        """
        cap = design_review.review_rounds(spec)
        out: dict[str, Any] = {
            "rounds": [], "rewritten": {}, "skipped": [], "first": {}, "last": {},
            "reviewed": [], "cost_yuan": 0.0, "stale": [], "still_red": []}
        todo = dict(files)
        prev_fix: dict[str, str] = {}
        stale: set[str] = set()
        left = design_review.MAX_REWRITES
        for round_no in range(1, cap + 1):
            if not todo:
                break
            review = await design_review.review_stills(
                shots=spec["shots"], stills=todo, art=art,
                prop=str(spec.get("recurring_prop") or ""),
                style=str(spec.get("style") or ""),
                grammar=str(spec.get("style_grammar") or ""),
                skill_contract=spec.get("skill_contract"),
                user_id=state.user_id or "", conversation_id=state.conversation_id or "")
            out["cost_yuan"] += float(review["cost_yuan"])
            if not out["first"]:
                out["first"] = review
                out["reviewed"] = sorted(str(sid) for sid in todo)
            out["last"] = review
            row: dict[str, Any] = {
                "round": round_no, "checked": review["checked"],
                "passed": review["passed"],
                "failed": [f for f in review["failed"]
                           if str(f.get("shot")) in todo],
                "unjudged": review["unjudged"], "cost_yuan": review["cost_yuan"]}
            out["rounds"].append(row)
            fresh = []
            for item in row["failed"]:
                sid = str(item.get("shot"))
                if design_review.same_fix(prev_fix.get(sid), item.get("fix")):
                    if sid not in stale:
                        stale.add(sid)
                        out["stale"].append(sid)
                    continue
                fresh.append(item)
            if stale:
                row["stale"] = sorted(stale)
            if not fresh:
                break
            # 评审说「该有体积 / 该是公式 / 该是手绘」时，把这一镜的申报升上去
            # （只升不降）。**必须排在 `_feed_review_lessons` 与 `rewrite_failed` 之前**：
            # 升级要往 `fix` 里追加「这一镜现在是 three，必须真用上」，而 `fix` 就是
            # 交给写手的那段批评——晚一步，写手只会照原来的意见再修一遍 SVG。
            bumped = _bump_channel_by_review(spec, fresh)
            if bumped:
                row["channels_bumped"] = bumped
            # 评审说「太空 / 只有主体 / 没有背景」时补一份构图骨架（2026-10-24）。
            # 排在 `rewrite_failed` 之前，理由同上：它要往 `fix` 里追加解释，
            # 而 `fix` 就是交给写手的那段批评。
            patched = _bump_density_by_review(spec, fresh)
            if patched:
                row["density_patched"] = patched
            # 评审的教训要跨镜：逐镜意见说的是「这一类画法」的病，不是这一镜的病。
            # 写进全片账之后，**这一轮重写的每一镜**都会在自己的 prompt 里读到它
            # （`shot_writer._ledger_block` 的 fixes 那一段）。2026-10-23 之前只重写
            # 被点名的镜，其余镜一条教训都收不到，下一轮换一种说法再犯一遍。
            if _feed_review_lessons(spec, fresh):
                row["lessons_fed"] = True
            got = await design_review.rewrite_failed(
                spec=spec, failed=fresh, stills=todo,
                user_id=state.user_id or "", conversation_id=state.conversation_id or "",
                user_request=getattr(state, "user_request", "") or "",
                limit=(None if left is None else max(0, left)))
            out["cost_yuan"] += float(got["cost_yuan"])
            row["rewritten"] = dict(got["rewritten"])
            row["skipped"] = list(got["skipped"])
            out["rewritten"].update(got["rewritten"])
            for sid in got["skipped"]:
                if sid not in out["skipped"]:
                    out["skipped"].append(sid)
            if left is not None:
                left = max(0, left - len(got["rewritten"]))
            fixed = sorted(sid for sid, r in got["rewritten"].items() if r.get("ok"))
            row["fixed"] = fixed
            if not fixed:
                break
            refs2, notes2, _layouts2, files2, bad2 = await self._publish_previews(
                state, spec, only=set(fixed))
            refs.update(refs2)
            files.update(files2)
            notes.extend(notes2)
            if bad2:
                # 重写后仍编译不过：与前面那一轮同一个出口，卡上要能看到。
                failures.update(bad2)
            todo = dict(files2)
            prev_fix = {str(f.get("shot")): str(f.get("fix") or "")
                        for f in row["failed"]}
        # 「仍红」= 最后一次看片还红、且之后没有再改过的镜。轮数用完时最后一轮
        # 改过但没再看的那几镜不算红——我们不知道它现在长什么样，不该替它下结论。
        tail_fixed = set(out["rounds"][-1].get("fixed") or []) if out["rounds"] else set()
        out["still_red"] = sorted({str(f.get("shot"))
                                   for f in (out["last"].get("failed") or [])}
                                  - tail_fixed)
        # 「没改成」按**最后一次**的结论算：第一轮没改成、第二轮改成了的镜不该留在
        # 这一栏（回执里说它「没改成」会让人以为那一版还是旧的）。
        done = {sid for sid, r in out["rewritten"].items() if r.get("ok")}
        out["skipped"] = sorted({sid for sid in out["skipped"] if sid not in done})
        out["cost_yuan"] = round(out["cost_yuan"], 4)
        return out

    async def _repair_broken_shots(self, state: NodeState, spec: dict[str, Any],
                                   failures: Mapping[str, Mapping[str, str]],
                                   broken: Sequence[str]
                                   ) -> tuple[list[str], list[str], float]:
        """编译不过的镜：让写手照着渲染器的报错重画一版 → (改成了的镜, 回执, 花费)。

        为什么由编排自己修、而不是交回主对话：这段错在预览打包时才暴露，而它**一定**
        会让渲染在那一镜中停（同一份代码、同一个打包器）。交回主对话要再走一轮
        `shots_patch`，中间用户看到的只是卡上一格「无预览」——他不知道这是要改的。

        只做一轮、只对 `final` 这一类开口：`rewrite_shot_code` 自己会带 lint 报错重试
        `REWRITE_ATTEMPTS` 次；这里再套循环就会在一镜上反复烧。超时那类是机器忙，
        重画会把一份本来能跑的代码换掉（真机上四张全是一次重渲就出的）。
        """
        items = []
        for sid in broken:
            detail = str((failures.get(sid) or {}).get("detail") or "（渲染器没给原因）")
            items.append({
                "shot": sid,
                "why": detail[:400],
                "fix": "把这段代码改到能编译：重复声明、没闭合的括号、写错的标识符都清掉；"
                       "画面、构图、字幕文案按原来的分镜保留，别换内容。",
            })
        try:
            got = await design_review.rewrite_failed(
                spec=spec, failed=items, prefix="这一镜的代码编译不过",
                user_id=state.user_id or "", conversation_id=state.conversation_id or "",
                user_request=getattr(state, "user_request", "") or "")
        except Exception as exc:  # noqa: BLE001 —— 自动修失败不带塌整份分镜
            return [], [f"自动重画编译不过的镜时出错了（{type(exc).__name__}），"
                        "这一轮先照原样交：继续渲染会在那几镜停下。"], 0.0
        fixed = sorted(sid for sid, r in (got.get("rewritten") or {}).items()
                       if r.get("ok"))
        notes: list[str] = []
        if fixed:
            # 只说重画了；**是否渲得出来由紧跟着的那次重渲回执说**——重画过 lint 但
            # 打包仍可能炸（另一处错），在这里提前写「预览已重出」会自相矛盾。
            notes.append("代码编译不过、已自动重画：" + "、".join(fixed) + "。")
        left = [sid for sid in broken if sid not in set(fixed)]
        if left:
            notes.append("代码编译不过、自动重画一次仍未成：" + "、".join(sorted(left))
                         + "——照原样继续渲染会在这一镜停下；要改就重画这一镜，"
                         "或把这镜的内容换一种说法。")
        return fixed, notes, float(got.get("cost_yuan") or 0.0)


#: 每个会话最近一版写码轮分镜（补丁轮的底稿）。进程内、有上限：重启后丢了就请模型交整份。
_DRAFTS: dict[str, dict[str, Any]] = {}
_DRAFTS_MAX = 64

#: 每个会话最近一轮的预览静帧对象键。补丁轮只重渲改动过的镜，
#: 但计划确认卡上仍要看到全片——所以这里留一份，按当前镜号合并后再交给卡面。
#: 键带上 artifact：同一会话可以先后做两条片，镜号会重复，按会话单键会串图。
_PREVIEW_REFS: dict[tuple[str, str], dict[str, str]] = {}
_PREVIEW_REFS_MAX = 64


def _remember_previews(session_id: str, artifact_id: str, refs: Mapping[str, str],
                       spec: Mapping[str, Any]) -> dict[str, str]:
    """把本轮渲出来的预览图并进这一会话/这一片的缓存 → 当前镜号的那一份。"""
    key = (session_id, artifact_id or "_default")
    cur = _PREVIEW_REFS.pop(key, {})
    cur.update({str(k): str(v) for k, v in refs.items()})
    keep = {sid: cur[sid] for sid in _shot_ids(spec) if sid in cur}
    _PREVIEW_REFS[key] = keep
    while len(_PREVIEW_REFS) > _PREVIEW_REFS_MAX:
        _PREVIEW_REFS.pop(next(iter(_PREVIEW_REFS)))
    return dict(keep)


def _remember_draft(session_id: str, raw: Mapping[str, Any]) -> None:
    _DRAFTS.pop(session_id, None)
    _DRAFTS[session_id] = dict(raw)
    while len(_DRAFTS) > _DRAFTS_MAX:
        _DRAFTS.pop(next(iter(_DRAFTS)))


#: 闸⑯ 回写环里，按通道给写手的一句「怎么真用上」。骨架在
#: `shot_writer._channel_samples_block()` 里（每镜 prompt 都带），这里只是点名。
_CHANNEL_HINT: dict[str, str] = {
    "three": "`import { ThreeCanvas } from '@remotion/three'`，里面用 `<mesh>` + "
             "`<sphereGeometry>/<boxGeometry>/<coneGeometry>/<cylinderGeometry>` + "
             "`<meshStandardMaterial>`（要 roughness/metalness，**别用 meshBasicMaterial**"
             "——那等于没开灯）；再配一盏 `directionalLight`（主光）和一盏 `pointLight`"
             "（边光），底部加一块水平暗面接阴影。",
    "katex": "`import katex from 'katex'` 与 `import 'katex/dist/katex.min.css'`（**css 不能少**，"
             "否则字形崩成豆腐块），`katex.renderToString(tex, {displayMode:true, "
             "throwOnError:false})` 配 `dangerouslySetInnerHTML`。",
    "rough": "`import rough from 'roughjs'`，`rough.canvas(canvasRef.current)` 之后调 "
             "`rc.circle/rc.path/rc.rectangle`；手绘感主要来自 `fillStyle:'hachure'` 的斜纹"
             "填充，不是把线抖一点。",
    "canvas": "`<canvas ref={…}>` + `useEffect` 里 `getContext('2d')` 逐帧重画"
              "（依赖 `[frame]`），适合点阵/粒子这类几百个小元素——**它不会自己动**，每帧都要重画。",
}


#: 评审意见 → 该升到哪条通道。**只认画面里说得出来的物理特征**，不认风格词。
#:
#: 为什么要这张表（2026-10-23 晚）：`subject_channel` 是模型在 plan 阶段自己写的申报，
#: 而「降低申报」比「写 60 行 three」容易太多 —— 把 s07 申报成 `vector`，闸⑯ 立刻放行，
#: 画面还是扁平。这一环把视觉评审看到的「该有体积」回灌成「改申报」，那个漏洞才堵上。
#:
#: 判据故意窄：只有出现「厚度/粗糙度/受光面」这类**只有真 3D 才做得出**的词才升
#: `three`。写「不够精致」「有点空」这种话不升——那是构图问题，不是通道问题。
_CHANNEL_UP_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("three", ("体积", "厚度", "立体", "3d", "粗糙度", "受光面",
               "背光面", "冷边光", "材质", "透视", "遮挡", "实心体")),
    ("katex", ("公式", "字形", "上下标", "数学符号", "渲染成豆腐块")),
    ("rough", ("手绘", "铅笔", "斜纹", "hachure", "笔触", "线头", "抖动")),
    ("canvas", ("点阵", "粒子", "几百个", "大量小元素", "网格里每一个")),
)

#: **反向排除**：这些词说的是「画漏了 / 没画」，不是「画法不对」。
#:
#: 2026-10-23 实测踩到：s12 的评审写「整帧几乎全黑，刻度线、倒数读数、3px 端点都没
#: 画出来」，靠「实心/接触阴影」这类词被判成「该升 three」——可它的 brief 本来就是
#: 「黑场 + 一根 2px 刻度线」，用 three 画一根线是荒谬的。**缺失和画法是两种病**：
#: 前者要补，后者要换笔。命中这里就整条跳过，不升通道。
_CHANNEL_MISSING_HINTS: tuple[str, ...] = (
    "没画出来", "没画", "看不到", "缺失", "空白", "几乎全黑", "一片黑",
    "完全没", "看不见", "只有黑底",
)


def _bump_channel_by_review(spec: dict[str, Any],
                            failed: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """评审说「该有体积 / 该是公式 / 该是手绘」时，把那一镜的申报升上去。→ {镜号: 新通道}。

    只升不降：已经是 `three`/`katex`/`rough`/`canvas` 的镜一律不动——它可能是「写了但
    没写好」，那是重写的事，不是换通道的事。只把 `vector`（或空）往上提。

    升级同时**往 `fix` 里追加一段话**：`rewrite_failed` 拿 `fix` 当批评交给写手，
    所以「这一镜申报已改为 three，必须真用上」这句话必须跟着这条意见一起过去——
    否则写手只会照原来的意见再修一遍 SVG。

    改了申报而重写没成，闸⑯ 会在下一轮报「申报无证据」——那是**如实记账**，
    不是 bug：这一镜的申报确实没兑现，回执里该看得见。
    """
    rows = {str(s.get("id")): s for s in (spec.get("shots") or [])
            if isinstance(s, dict)}
    bumped: dict[str, str] = {}
    for item in failed:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("shot") or "")
        shot = rows.get(sid)
        if not shot:
            continue
        cur = str(shot.get("subject_channel") or "").strip().lower()
        if cur and cur != "vector":
            continue          # 已经是高级通道：问题在实现，不在选择
        text = (str(item.get("why") or "") + " " + str(item.get("fix") or "")).lower()
        # 「画漏了」不是「画法不对」：命中缺失类词就整条跳过，不升通道。
        if any(w in text for w in _CHANNEL_MISSING_HINTS):
            continue
        for chan, words in _CHANNEL_UP_HINTS:
            if not any(w.lower() in text for w in words):
                continue
            shot["subject_channel"] = chan
            bumped[sid] = chan
            item["fix"] = (
                str(item.get("fix") or "")
                + f"\n\n【通道升级】这一镜的申报已从 `{cur or '空'}` 升为 `{chan}`："
                f"评审说的是「{str(item.get('why') or '')[:60]}」——"
                f"那不是一份手写 SVG 能画出来的。**这一版必须真的用 {chan} 画主体**，"
                f"骨架就在这份 prompt 的「另外四条画法通道：能抄的骨架」那一段。"
                + _CHANNEL_HINT.get(chan, ""))
            break
    return bumped


#: 评审意见里「画面太空」的那一类词。命中就给这一镜补一份构图骨架，然后重写。
#:
#: 为什么要有它（2026-10-24）：2026-10-23《一秒》12 镜评审**全不过**，看图确认是
#: 同一个病——每镜只有「一个主体 + 一块底色」，四角全空。这件事以前**没有任何一层
#: 负责**：导演被要求「别硬凑实体」，写手被告知「brief 是最高事实」，评审看得出来
#: 但只能写一段话。所以在这里把它接上：评审说「太空」→ 补构图 → 重写。
#:
#: 与 `_CHANNEL_UP_HINTS` 的分工：那一条判「画法选错了」，这一条判「画得不够满」。
#: 两者可以同时命中（一颗扁平的球既该换通道、也该补背景），互不冲突。
_DENSITY_HINTS: tuple[str, ...] = (
    "太空", "空荡", "大片空", "留白太多", "只有主体", "只有一块", "孤零零", "漂在",
    "没有背景", "缺少背景", "缺背景", "没有层次", "同明度", "无明暗", "无亮区",
    "看不见", "看不清", "缩成", "稀疏", "没占满", "没铺满", "没有铺满",
    "太简单", "单薄", "信息量不足", "扁平横条", "像表格行", "只是一团", "只是几片",
    "没有铺开", "四角",
    # 色彩（2026-10-24 加）：实测我们的画面饱和度只有 1.6/255（参考片 6.5~81），
    # 本质是「灰蓝底 + 暖白粉彩」＝黑白片。评审说得出这几种话时一并补配色。
    "发灰", "灰蒙", "灰蒙蒙", "闷", "寡淡", "没颜色", "没有颜色", "缺色",
    "单色", "黑白", "像黑白", "饱和度", "不够鲜", "不鲜艳", "灰暗",
)
#: 这一镜的 brief 本身就声明了「极简/黑场/剪影」时**不补**——那是设计，不是缺陷。
#: 踩过：s12 的 brief 是「黑场 + 一根 2px 刻度线」，被当成「太空」补满 HUD 就荒谬了。
_DENSITY_MINIMAL_HINTS: tuple[str, ...] = (
    "极简", "黑场", "只留", "留白", "剪影", "单线", "纯字", "只画一根", "只有一根",
)
#: 补上去的构图骨架。五段与 `art_director._SYSTEM` 里要求导演写的那一行同构，
#: 写手那一侧由 `shot_writer._frame_floor_block` 读。
_DENSITY_PATCH = (
    "背景：底色（不用纯黑）+ 细网格 + 暗角 + 一层雾/光晕"
    "｜中景：主体群至少 3 件、最大者占画面高 35~55%"
    "｜前景：一条刻度尺 + 两个读数 + 一条标注线/括号"
    "｜配色：底色 #0B1220 + 三个高饱和色相 #FF6B2C / #22D3EE / #FFC93C，"
    "饱和色块合计占画面 ≥25%（不许只出现在细线与小圆点上）"
    "｜动：至少 4 个元素在动，两处变化各覆盖画面三分之一以上"
    "｜运镜：推近 10%，整幅在动"
)


def _bump_density_by_review(spec: dict[str, Any],
                            failed: Sequence[Mapping[str, Any]]) -> list[str]:
    """评审说「太空 / 只有主体 / 没有背景」时，给那一镜补一份构图骨架。→ 补了的镜号。

    补的是 `shot["frame_note"]`（不是 brief 的尾巴——brief 有 400 字硬上限，追加会被
    截掉，而截掉的正好是这段）。`shot_writer._frame_floor_block` 优先读它。

    同时往 `fix` 里追加一句，让写手知道「这一版为什么突然多了一段构图要求」——
    `rewrite_failed` 拿 `fix` 当批评，不解释的话写手会当成又一次泛泛的「再精致一点」。
    """
    rows = {str(s.get("id")): s for s in (spec.get("shots") or [])
            if isinstance(s, dict)}
    patched: list[str] = []
    for item in failed:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("shot") or "")
        shot = rows.get(sid)
        if not shot or shot.get("frame_note"):
            continue              # 已经补过，别在第二轮再叠一层
        text = (str(item.get("why") or "") + " " + str(item.get("fix") or "")).lower()
        if not any(w in text for w in _DENSITY_HINTS):
            continue
        brief = str(shot.get("brief") or "")
        # 「画漏了」有两种：一种是真的空，一种是 brief 本来就要极简。后者不补。
        if any(w in brief for w in _DENSITY_MINIMAL_HINTS):
            continue
        # 导演已经为这一镜写过 `【构图】` 时**不覆盖**：那一份是按这一镜的画面写的，
        # 比下面这段通用的强。只在 `fix` 里点一句「照那段画」。
        has_frame = "【构图】" in brief
        if not has_frame:
            shot["frame_note"] = _DENSITY_PATCH
            patched.append(sid)
        item["fix"] = (
            str(item.get("fix") or "")
            + "\n\n【构图补丁】评审说这一镜「"
            + str(item.get("why") or "")[:60]
            + "」——那不是「再精致一点」能修的，是画面里**根本没有背景层和前景信息**。"
            + ("这一版已给这一镜补了一份构图骨架（见 prompt 里 `【构图】` 那一段），"
               if not has_frame else
               "这一镜的 brief 里已经有 `【构图】` 那一段，")
            + "**五段都要真的画出来**：背景网格与暗角、主体群、刻度尺与读数、四处以上的动、"
            "一次整幅运镜。只画主体、四角全空，会原样再被打回。")
    return patched


def _feed_review_lessons(spec: dict[str, Any],
                         failed: Sequence[Mapping[str, Any]]) -> int:
    """把视觉评审挑出的问题写进**全片画法账**，让重写时的每一镜都读得到。→ 新增条数。

    为什么要有它：评审是**逐镜**的，但那条意见说的往往是**一类画法**的毛病。
    2026-10-23《一秒》实跑：评审挑了 3 镜——s07「心脏是扁平卡通剪影，没有厚度、
    粗糙度、接触阴影」、s11「一片极暗竖点阵，看不出火柴人」、s04「最亮处不唯一」。
    这三条其实是同一件事（实体没有体积、没有光位、没有接触阴影），s02 的金属腔、
    s05 的地球、s10 的太阳犯的是同一个错。但系统只重写了这 3 镜，**其余 8 镜一条
    教训都没收到**，下一轮换一种说法再犯一遍。

    写进去的形状就是 `shot_writer._ledger_block` 读的那一份（`{ids, what, fix}`），
    它在每镜 prompt 里以「全片复核挑出的问题（改这一镜时一并避开）」出现。
    所以重写 s02 的时候，写手会看到 s07 那条「没有厚度/接触阴影」的教训。

    条数按 `_ledger_block` 的上限截（8 条）；同一条 `fix` 只留一次（跨轮也不重复）。
    """
    led = spec.get("film_ledger")
    if not isinstance(led, dict):
        led = {}
        spec["film_ledger"] = led
    fixes = led.get("fixes")
    if not isinstance(fixes, list):
        fixes = []
        led["fixes"] = fixes
    seen = {re.sub(r"\s+", "", str(f.get("fix") or ""))[:60]
            for f in fixes if isinstance(f, Mapping)}
    added = 0
    for item in failed:
        if not isinstance(item, Mapping):
            continue
        fix = str(item.get("fix") or "").strip()
        if not fix:
            continue
        key = re.sub(r"\s+", "", fix)[:60]
        if key in seen:
            continue
        seen.add(key)
        sid = str(item.get("shot") or "").strip()
        fixes.append({
            "ids": [sid] if sid else [],
            "what": str(item.get("why") or "").strip(),
            "fix": fix,
        })
        added += 1
    del fixes[8:]
    return added


def _shot_ids(spec: Mapping[str, Any] | None) -> list[str]:
    shots = (spec or {}).get("shots") if isinstance(spec, Mapping) else None
    return [str(s.get("id") or "") for s in (shots or []) if isinstance(s, Mapping)
            and str(s.get("id") or "")]


def _shots_replacement(previous: Any, current: Any) -> dict[str, Any] | None:
    """整份重交分镜时，与上一版比镜号 → 一份结构化的差集（无可说的就 `None`）。**只报不拦**。

    2026-10-17 真机片（`rj-e0d34b53`）：模型先交了 s01–s06，再交 s07–s20，以为是「追加」，
    实际 `shots` 是**整份替换**——前 6 镜被覆盖掉，成片从 s07 开头，片子没有开场，
    而回执一句没说。改镜数是合法操作，所以不拦；但「少了哪几镜」必须让模型和人看见。
    断号（不从 s01 起、中间缺号）同理：它多半就是「以为在追加」的那个形状。

    `previous` / `current` 都可以是一份分镜（带 `shots`）或直接是一个镜列表。
    回 `{"removed", "added", "note", "id_notes"}`：前两栏给前端/回执做结构化展示，
    `note` 是那段「整份替换」的整句，`id_notes` 是镜号本身的几条提醒。
    """
    def _ids(v: Any) -> list[str]:
        if isinstance(v, Mapping):
            return _shot_ids(v)
        return [str(s.get("id") or "") for s in (v or [])
                if isinstance(s, Mapping) and str(s.get("id") or "")]

    now = _ids(current)
    if not now:
        return None
    before = _ids(previous)
    removed = [s for s in before if s not in set(now)]
    added = [s for s in now if s not in set(before)]
    note = ""
    # 只有**少了镜**才叫「整份替换」：往末尾加镜是正常操作，不该被说成覆盖。
    if before and removed:
        note = (f"本次分镜是整份替换（上一版 {len(before)} 镜 → 本次 {len(now)} 镜）")
        note += f"，少了：{'、'.join(removed)}"
        if added:
            note += f"，多了：{'、'.join(added)}"
        note += ("。`shots` 每次交的都是完整的一份、不是追加——少了的那几镜若不是有意删掉，"
                 "就把它们一并交上来，或用 shots_patch 只改动过的那几镜。")
    id_notes: list[str] = []
    nums = [int(m.group(1)) for s in now for m in [re.fullmatch(r"s(\d+)", s)] if m]
    if nums and len(nums) == len(now):
        missing = sorted(set(range(1, max(nums) + 1)) - set(nums))
        if 1 in missing:
            id_notes.append(
                "镜号不连续（不从 s01 开始）：这一份里最小的是 "
                f"s{min(nums):02d}。成片按交上来的顺序拼，若本意是接着上一版往后加，"
                "缺的那几镜不会出现。")
        gaps = [n for n in missing if n != 1]
        if gaps:
            id_notes.append(
                f"镜号不连续（断号）：缺 {'、'.join(f's{n:02d}' for n in gaps[:12])}"
                + ("…" if len(gaps) > 12 else "")
                + "。成片按交上来的顺序拼，缺的那几镜不会出现——确认是有意的。")
    if not note and not id_notes:
        return None
    return {"removed": removed, "added": added, "note": note, "id_notes": id_notes}


def shot_diff_notes(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[str]:
    """`_shots_replacement` 的扁平说法（回执里那几行）。"""
    got = _shots_replacement(previous, current)
    if not got:
        return []
    return ([got["note"]] if got.get("note") else []) + list(got.get("id_notes") or [])


def merge_submission(base: Mapping[str, Any], spec_raw: Any,
                     patch: list[Any]) -> dict[str, Any]:
    """把一次 `plan_motion` 交上来的 `spec` 与 `shots_patch` 合成一份底稿。

    两个入口都可以是补丁：模型经常「把要改的几镜放进 spec、把新镜放进 shots_patch」
    （真机 run 8c5e161ddb20 就是 6 镜 + 16 镜）。只认其中一个的写法会让另一份被
    静默丢掉，而报错只会说「时长偏离」，看不出是合并这一步丢了镜。

    - 逐镜：先并 `patch`，再并 `spec.shots`（同号时 spec 覆盖；新号追加）
    - 顶层：`spec` 里除 `shots` 之外的字段照常生效（style / palette / 时长…）
    - `spec_raw` 不是对象时（模型只交了 patch）就只用 patch
    """
    spec = dict(spec_raw) if isinstance(spec_raw, Mapping) else {}
    spec_shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    merged = merge_shots_patch(base, list(patch or []) + spec_shots)
    top = {k: v for k, v in spec.items() if k != "shots"}
    return {**merged, **top}


def _brief_key(shot: Mapping[str, Any]) -> str:
    """`brief` 的比对口径：空白归一后的整串。"""
    return " ".join(str(shot.get("brief") or "").split())


def _style_key(raw: Any) -> tuple[str, ...]:
    """复用键：`(包名, 模板名, 主角约定)`。全空表示「没有风格包、没有主角」。

    只认决定**代码长什么样**的那几项：换风格包/换模板、或改主角的几何与画法
    方向，旧代码就不再是同一个画面。manifest 里的颜色/构图微调不参与——调一个
    色号不该让整片重写。

    主角约定进键，是为了留一条**补救路径**：分镜入库后才发现没写 `protagonist`
    时，模型重交顶层这一栏、其余镜不重交，复用会因键变了而失效，整片按同一个
    主角重写——而不是「补了约定，画面一个像素都没变」。
    """
    if not isinstance(raw, Mapping):
        return ("", "", "")
    p = raw.get("protagonist") if isinstance(raw.get("protagonist"), Mapping) else {}
    protagonist = "|".join(str(p.get(k) or "") for k in
                           ("render", "height", "foot_y", "facing", "stride_sec"))
    return ("", "", protagonist)


#: 风格/分镜里出现这些词、且有好几镜时，认为片子里有一个反复出现的人物。
_RECURRING_PERSON_WORDS = ("剪影", "人物", "主角", "同一个人", "人影", "身影", "人")


def missing_protagonist_note(spec: Mapping[str, Any]) -> str:
    """有贯穿人物却没写 `protagonist` → 一句回执（不拦、不自动编）。

    真机 37 镜《火与光》的 spec 里根本没有这一栏：`style` 里写着「人物脚底
    28%/82%、身高 44%」，但没有结构化约定，写手只能每镜照 brief 自己画一个，
    切点一过身高、肩宽、步幅全换。这里只**报**——大部分片子确实没有贯穿人物，
    自动补一个主语反而会让几十镜硬塞同一件东西。
    """
    shots = [s for s in (spec.get("shots") or []) if isinstance(s, Mapping)]
    if len(shots) < 4:
        return ""
    prose = " ".join(str(spec.get(k) or "") for k in ("style", "style_grammar"))
    person_shots = sum(
        1 for s in shots
        if any(w in str(s.get("brief") or "") for w in _RECURRING_PERSON_WORDS))
    if not any(w in prose for w in _RECURRING_PERSON_WORDS) and person_shots < 3:
        return ""
    return ("**这部片没有声明贯穿主体 `protagonist`**：分镜/风格里有人物在多个镜出现，"
            "写手会每镜各画一个人（真机 37 镜就是这样：切点一过，身高、肩宽、"
            "步幅、手臂摆法全换一套）。这一版照常写码出片；**要同一个人贯穿**，"
            "就把 `protagonist` 补进 spec 顶层再交一次（`kind: \"人\"`，"
            "`height`/`foot_y`/`facing`/`stride_sec` 各给一个数，`look` 写清轮廓）"
            "——每镜按同一组比例自己画，切点才不换人。主角约定变了，"
            "旧代码不会复用，整片会按同一组比例重写。其余镜不必重交。")


def reuse_coded(base: Any, raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """整份重交时，把上一版已经写好代码、`brief` 又逐字相同的镜直接复用。

    为什么只看 `brief`：文案（`text`）和强调词（`highlight`）都是渲染时注入的 props，
    镜头代码里一个字都不含它们（见 `code_render.shot_input_props`），画面画的是
    `brief` 说的东西。所以「只改了一句旁白」不该让整片几十镜重写一遍代码——
    那是每镜一次写手调用 + 一次草稿真渲。真机 run 8c5e161ddb20 里模型因为一次
    600 s 超时把同一份 32 镜整份重交了三遍，每一遍都从零写。

    **只作用于整份重交**（没有 `shots_patch` 的那一轮）：补丁轮里「交新 `brief`
    不交 code」是**明确的重画意图**（见 `merge_shots_patch`），不能被这里悄悄改掉。
    复用了哪几镜会写进回执，模型想重画其中某一镜，单独用 `shots_patch` 交它即可。

    **风格包变了就整份不复用**：换包/换模板意味着画面契约换了，旧代码即使 brief
    一字不差也不再是同一个画面；沿用只会让「换了风格却没变」看起来像模板失效。
    """
    shots = raw.get("shots")
    if not isinstance(base, Mapping) or not isinstance(shots, list):
        return dict(raw), []
    if _style_key(base) != _style_key(raw):
        return dict(raw), []
    old = {str(s.get("id") or ""): s
           for s in (base.get("shots") or [])
           if isinstance(s, Mapping) and str(s.get("code") or "").strip()}
    out: list[Any] = []
    reused: list[str] = []
    for row in shots:
        if not isinstance(row, Mapping):
            out.append(row)
            continue
        prev = old.get(str(row.get("id") or ""))
        key = _brief_key(row)
        if (prev is not None and key and not str(row.get("code") or "").strip()
                and key == _brief_key(prev)):
            out.append({**row, "code": prev["code"]})
            reused.append(str(row["id"]))
        else:
            out.append(dict(row))
    return {**raw, "shots": out}, reused


def merge_shots_patch(base: Mapping[str, Any], patch: list[Any]) -> dict[str, Any]:
    """按镜号把补丁并进底稿：同号整镜替换，新号追加；顶层开关沿用底稿。

    **只交新 `brief`、不交 `code`** 的一镜按「重画这一镜」处理：底稿里的旧代码会被
    丢掉，交给 `shot_writer` 按新 brief 重写。不这么做，并进来的旧代码会让这一镜
    看起来「已经有 code」，画面一个像素都不会变——而模型以为自己改了。
    """
    shots = [dict(x) for x in (base.get("shots") or []) if isinstance(x, Mapping)]
    pos = {str(x.get("id") or ""): i for i, x in enumerate(shots)}
    for item in patch:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        sid = str(item["id"])
        row = dict(item)
        redraw = ("brief" in row and not str(row.get("code") or "").strip()
                  and not row.get("code_from"))
        if sid in pos:
            merged = {**shots[pos[sid]], **row}
            if redraw:
                merged.pop("code", None)
            shots[pos[sid]] = merged
        else:
            pos[sid] = len(shots)
            shots.append(row)
    return {**dict(base), "shots": shots}


async def _remotion_up() -> bool:
    """渲染容器此刻通不通（3 秒探活）。不通时预检与预览都跳过，不拖慢分镜轮。"""
    try:
        got = await asyncio.to_thread(rc.health, timeout=3)
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(got, Mapping) and got.get("ok"))


#: 入库前 lint 的并发路数（/lint 不进渲染串行链，只是起一次 eslint）。
PRELINT_CONCURRENCY = 6
#: 单镜预检超时（秒）：预检是增益不是硬闸，容器慢或不通时不该把分镜轮拖住。
PRELINT_TIMEOUT = 20


async def _prelint(shots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """所有镜的代码并行过闸① → ``(打回, 未判)``。

    **未判 ≠ 通过**（P5）。渲染容器不可达、外呼炸了、或回来的响应里**没有** `ok` 键时，
    这几镜的代码**没有经过闸①**——那与「过了闸①」是两件事。改这一处之前这三种情况
    都返回 `[]`，也就是把"没量过"记成了"量过且合格"；真机上的读法就是计划卡上写着
    「闸① 全过」，而那一趟其实一次都没连上。渲染口确实还会再 lint 一遍（那才是硬闸），
    但入库这一步的回执必须自己说清这一趟量到什么程度，不能拿下游的兜底当自己的读数。
    """
    def lint(code: str, names: list[str]) -> dict[str, Any]:
        # 不再传 `assets`：那一栏 2026-10-16 已从契约里删掉，`lint()` 也没有这个形参
        # ——带着它调用会抛 TypeError，而 `_prelint` 把任何异常都记成「未判」，
        # 于是闸① 在这条预检路上**从未真正跑过一次**，回执却只写「没连上渲染容器」。
        # （2026-10-22 三镜自检里 s01/s02/s03 全是 TypeError，渲口仍可用。）
        return rc.lint(code, declared_fields=names, timeout=PRELINT_TIMEOUT)
    gate = asyncio.Semaphore(PRELINT_CONCURRENCY)

    async def one(shot: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        code = str(shot.get("code") or "")
        sid = str(shot.get("id") or "?")
        if not code.strip():
            # 空代码轮不到闸① 说话：契约已经把它判掉了（`_norm_shot` 的「code 为空」），
            # 这里再记一条未判只是往回执里灌噪声。
            return [], []
        names = [str(f.get("name") or "") for f in (shot.get("fields") or [])
                 if f.get("name")]
        async with gate:
            try:
                got = await asyncio.to_thread(lint, code, names)
            except Exception as exc:  # noqa: BLE001 - 预检外呼失败不拦，渲染口兜底
                return [], [f"{sid}：闸① 未判——预检这一趟没连上渲染容器"
                            f"（{type(exc).__name__}）。**这不是通过**，"
                            f"渲染口还会再判一次。"]
        if not isinstance(got, Mapping):
            return [], [f"{sid}：闸① 未判——预检的响应不是对象"
                        f"（{type(got).__name__}）。**这不是通过**。"]
        if "ok" not in got:
            # 缺 `ok` 键曾经被 `got.get("ok", True)` 当成通过——这一条正是"静默通过"
            # 里最不该留着的那一半：一个键名写错的响应，读起来与合格完全一样。
            return [], [f"{sid}：闸① 未判——预检的响应里没有 `ok` 键，"
                        f"判不出这一镜过没过。**这不是通过**。"]
        if got.get("ok"):
            return [], []
        errs = [e for e in (got.get("errors") or [])
                if not (isinstance(e, Mapping) and e.get("rule") == "lint-crash")]
        return [f"{sid}：{e.get('rule') or ''} 第 {e.get('line') or '?'} 行：{e.get('msg') or e}"
                if isinstance(e, Mapping) else f"{sid}：{e}"
                for e in errs], []

    results = await asyncio.gather(*(one(s) for s in shots))
    return ([e for r in results for e in r[0]],
            [u for r in results for u in r[1]])


def forage_payload(account: dict[str, Any], *, target: float | None = None,
                   shots: int = 0, aspect: str = "landscape",
                   shell: str = "") -> dict[str, Any]:
    """取材轮的产物：被引用 demo 的**全文** + 一份字节账。**不落库、不渲任何东西**。

    ``__no_store__`` 是这条链既有的写法（见 ``render_video`` 的 dry_run）：这一次调用
    确实产出了一样东西（模型上下文里的那几份源码），但它**不是**本会话的 ``motion_spec``
    ——落库就成了「这一步已经有分镜了」的假象，而出片节点读的正是那一格。

    ``next`` 写的是下一步该干什么，不是客套：取材轮最容易出的岔路是模型拿回全文之后
    又调一次取材（换个说法问同样的事），那会白烧一轮上下文。所以这里把「现在去写 code」
    与「超线的那几份用 load_skill 取」两件事都点名。

    另外三句（2026-10-17）**只在这里说**，不写进技能正文：这是模型正要动笔写代码的那一刻，
    说给它听最管用；而同一天把类似的话以「不照做就整份打回」的口气写进正文，模型被吓得
    不敢交分镜、一连八条 run 卡死在读材料上。所以这里是**建议的口气**，不是威胁：
    镜数（镜太少会被拉长成定格）、字幕带（`<CaptionLayer />` 由系统补上，底下那条别占）、
    时间轴（动作铺满这一镜，别只跑开头几秒）。
    """
    oversize = list(account.get("forage_oversize") or [])
    # 2026-10-18：原句是「相似度低于阈值的声明会被打回」——**不是事实**（这一道早已是
    # 建议档、只报不拦，见 `cspec.SOFT_GATES`），而它把模型逼成了照抄 demo 的外观：
    # 用户要「杂志风」，挑了对比表格卡就抄成一张 PPT 表、挑了霓虹卡就抄成霓虹字，
    # 风格那一句只落到了改颜色。demo 真正值钱的是动效机制与时间轴，外观归用户的风格。
    next_hint = ("现在把每一镜的 code 写出来，连同 card 与 derived_from 一起重调本工具"
                 "（这一次所有镜都带 code）。demo 借的是它的**动效机制与时间轴**；"
                 "**外观（版式、字体层级、配色、线条、装饰）一律照整片 `style` 那一句重画**——"
                 "demo 原样的卡片墙、表格框、霓虹发光这类外观与 style 冲突时，以 style 为准。"
                 "与 demo 的骨架相似度只是提醒，不会因此打回。")
    zone = cspec.CAPTION_ZONE_PX.get(aspect, 300)
    if str(shell or "").strip():
        # 片壳模式：字幕与画面外框都归片壳，镜头代码里不许出现字幕件。
        next_hint += ("写代码时三件事：① 底部字幕与外框由片壳统一画——这一镜只画画面带里的"
                      "主体与场景，不要摆 `<CaptionLayer />` / `<CaptionText />` / "
                      "`<CaptionFrame>`，也不要自己画纸底、色条、钟或期号；"
                      "② 动作铺满这一镜：最后一个动作的结束帧放在这一镜时长的 80% 以后，"
                      "别让后半截定格；")
    else:
        next_hint += (f"写代码时三件事：① 字幕交给 `<CaptionLayer />`（从 '@dg/film' 引入；"
                      f"你没摆的话系统会替你补上），它占画面最底下 {zone}px，"
                      "那一条里别放你自己的字和主体，否则会被字幕压住；"
                      "② 动作铺满这一镜：最后一个动作的结束帧放在这一镜时长的 80% 以后，"
                      "别让后半截定格；")
    if target and shots:
        per = target / max(1, shots)
        need = max(shots, round(target / 9.0))
        if per > 12.0:
            next_hint += (f"③ 目标 {target:.0f}s、你排了 {shots} 镜，平均 {per:.0f}s 一镜偏长——"
                          f"建议补到约 {need} 镜（8~12s 一镜）。系统凑时长时每镜最多只拉到"
                          f"自己原长的 {cspec.MAX_STRETCH_RATIO:g} 倍，镜不够片子就会短。")
        else:
            next_hint += f"③ 目标 {target:.0f}s、{shots} 镜，平均 {per:.0f}s 一镜，节奏合适。"
    next_hint += ("另外把用户对画面风格的要求写成一句，放进 spec 顶层的 `style`（会显示在计划卡上）；"
                  "每一镜的版式、字体层级、配色、线条都照这一句来，整片同一种语言。")
    if oversize:
        names = "、".join(str(o.get("relpath")) for o in oversize)
        # 技能名从 SKILL.md 的 frontmatter 读（它不等于目录名），读不到就只说 path，
        # 不编一个名字出来——「照提示去 load_skill 却查无此技能」比不提这件事更坏。
        skill = cspec.motion_skill_name()
        how = (f'load_skill(name="{skill}", path="<relpath>")' if skill
               else 'load_skill（技能名见 <skills> 清单, path="<relpath>"）')
        next_hint += (f"另有 {len(oversize)} 份超出本轮上限没随回执给出（{names}）："
                      "它们是**没给**不是**没内容**，用 "
                      f"{how} 逐件取全文——不要凭记忆写这几镜的代码。")
    return {
        "__no_store__": True,
        "forage": True,
        "stored": False,
        **account,
        "next": next_hint,
    }


def plan_model_view(payload: dict[str, Any]) -> dict[str, Any]:
    """交回模型的瘦身版：``motion_spec`` 的逐镜 ``code`` 只留字节数。

    完整那份照旧入库（渲染、局部改、预览读的都是它）。这里删的**只有**源码正文：
    brief、text、fields、subject 这些「模型下一次改得动」的字段一个不动。
    """
    spec = payload.get("motion_spec")
    if not isinstance(spec, Mapping):
        return payload
    shots: list[dict[str, Any]] = []
    for s in spec.get("shots") or []:
        if not isinstance(s, Mapping):
            continue
        row = {k: v for k, v in s.items() if k != "code"}
        row["code_chars"] = len(str(s.get("code") or ""))
        shots.append(row)
    out = {**payload, "motion_spec": {**dict(spec), "shots": shots}}
    out["code_note"] = (
        "每镜代码已由系统按 brief 写好、随产物入库（这里只列 code_chars，不回源码）。"
        "要改某一镜的画面：用 shots_patch 只交那一镜的 {id, brief}（改了 brief 会重写"
        "这一镜的代码），或直接交 {id, code}。其余镜不必重交。")
    return out


def plan_payload(spec: dict[str, Any], *, target: float | None = None,
                 next_hint: str = "",
                 gate: dict[str, str] | None = None) -> dict[str, Any]:
    """``plan_motion`` 产物的形状（归一后的 spec + 那份要给人看的账）——**单源**。

    为什么抽出来：``patch_motion_code`` 把一版局部改写回 ``plan_motion`` 时，必须写成与
    这里**一模一样**的形状（出片节点与确认卡读的都是这几栏）。两处各抄一遍，
    迟早出现「局部改之后卡面上 shot_count 还是旧数字」这种谎。

    ``gate`` 是出处闸顺手算出的每镜判定。闸只拦「冒充查证过」那一档，剩下三种
    （真对上了账、作者明写没核实、压根没填出处）**都会入库**——逐镜账要是把它们
    混成一个空白格，观众就没法知道画面右上角那行字的凭据是哪一种了。

    """
    low, high = cspec.estimate_sec(spec)
    shots = spec["shots"]

    def _row(s: dict[str, Any]) -> dict[str, Any]:
        # 出处用闸同一把钥匙读：契约写的是「标题/链接」，只认英文键的读法会让
        # 一条过了闸的中文出处在逐镜账上显示成空白。
        cite_title, cite_url = cite_fields(s["source"])
        text = s["text"]
        return {
            "id": s["id"],
            "text": (text[:PLAN_TEXT_PREVIEW_CHARS] + "…")
                    if len(text) > PLAN_TEXT_PREVIEW_CHARS else text,
            "chars": len(text),
            "highlight": s["highlight"],
            "source": cite_title or cite_url,
            "source_gate": (gate or {}).get(str(s["id"])) or "unchecked",
            "min_sec": s["min_duration_sec"],
            # 代码这一栏只报字节数不报正文：一张卡装不下一段 TSX，而「这一镜有没有交代码」
            # 在闸①~⑥ 那几行账上看的是像素，不是这段字节。
            "code_chars": len(str(s["code"])),
            # 闸⑦ 的两栏机器事实，都**从代码与文案里读出来**而不是模型自报：
            # style 是这一镜真用上了哪条画法通道（`text` = 一条都没用上），
            # concrete 是文案里有没有词法看得见的具体项（数字/引号术语/拉丁缩写）。
            # 用户在卡上看见某一镜 style=text 才会说「补画面」，这一栏的全部价值在这。
            "style": facts["channels"][str(s["id"])],
            "concrete": cspec.has_concrete_item(text),
            # 闸⑯ 的两栏（申报侧）：subject 是模型自报的「这一镜画的是什么」，subject_channel
            # 是它自报的通道。**与上面那栏 style 挨着放是刻意的**——style 是代码里认得的、
            # subject_channel 是自报的，两栏不一致的镜 validate_code_spec 已经拦掉了，
            # 留下的这一份让用户在确认卡上直接看见「这一镜说它画的是什么」。
            "subject": str(s.get("subject") or ""),
            "subject_channel": facts["subject_channels"][str(s["id"])]["declared"],
            # 闸⑮ 的账：这一镜选的卡属于哪个**族**。用户在卡上看见「data×3」才会说
            # 「版式重复了」——族配额那句打回理由指的正是这一栏。
            "family": facts["card_families"][str(s["id"])],
            # 这一镜**实际生效**的质感档（跟随整片时与顶层同值，点名不要时是 none）。
            # 判档只走 `cspec.shot_texture` 那一个出处：这里再写一遍「有就用、没有问整片」，
            # 就会出现卡上写 film、画上是原片。
            "texture": cspec.shot_texture(s, spec),
            # 字段清单进账：确认卡要给用户看清「这一镜交出几个可改的旋钮」，
            # 局部改那一头也要能从同一份产物里读到声明的 name/type/value。
            "fields": [{"name": f["name"], "type": f["type"], "value": f["value"],
                        "label": f.get("label") or "", "unit": f.get("unit") or ""}
                       for f in (s.get("fields") or [])],
        }

    facts = cspec.style_facts(shots, film=spec)
    # 整片判据（闸⑦ 每镜必画、⑩ 字色对比度、⑫ 派生相似、㉑ 动效锚…）从这一个出口取，
    # **三个出口都出账**（2026-10-16 起这也是它们的**唯一**出处：并档前打回那一栏在
    # `validate_code_spec` 里拦掉了，模型压根看不到自己撞上了哪几条）。
    # 三栏分家不是措辞讲究：卡上「打回」读作「这条最好改」，「提醒」读作「不改也能出」，
    # 「未判」读作「这一档没人量过」——合成一栏就是把后两者降级成了第一个。
    quota_errs, quota_warns, quota_undet = cspec.whole_film_gate_results(shots)

    return {
        "motion_spec": spec,
        "chain": "remotion",
        "shot_count": len(shots),
        "char_count": sum(len(str(s["text"])) for s in shots),
        "estimated_sec": [low, high],
        "target_duration_sec": target,
        "target_warnings": cspec.check_target(spec, target),
        # 会过期的数字（数据期比当前年份旧两年以上、或有年份没出处）：与 target_warnings
        # 同属「提醒」档——不进打回清单，模型读回执时自己决定换不换最新一期。
        "freshness_warnings": freshness_warnings(shots),
        # 闸⑦⑨⑩ 的整片账：下面几栏是**判据自己说的账**——用了哪几种通道、
        # 哪几镜一条画法通道都没用上、哪几镜其实是同一张卡、字色在底色上读到几比一。
        # 它们不再拦入库（拦的那一处 2026-10-16 撤了）：模型交活前可以用
        # `check_film` 自查同一份，用户可以在这里看着决定要不要让它改。
        "style_findings": quota_errs,
        "style_channels": facts["variety"],
        "style_text_only": facts["text_only"],
        "style_same_shape": facts["same_shape_groups"],
        "style_contrast": facts["contrast"],
        "style_contrast_unjudged": facts["contrast_unjudged"],
        "style_warnings": quota_warns,
        # 未判档单列（T28）：这一栏非空即「本片有闸没量过」，卡面按阻断档显示，
        # 不许与上一栏（提醒）用同一种措辞与同一种颜色。卡上看得见 = 用户能在
        # 烧像素之前就叫人补底色/重写代码，而不是等出片回执再发现。
        "style_unjudged": quota_undet,
        "aspect": spec["aspect"],
        "resolution": f"{spec['width']}x{spec['height']}",
        "fps": spec["fps"],
        "narration": spec["narration"],
        "voice": spec["voice"] if spec["narration"] else "",
        "rate": spec["rate"] if spec["narration"] else "",
        "caption_style": spec["caption_style"],
        "highlight_style": spec["highlight_style"],
        "texture": spec.get("texture") or "none",
        "bgm": spec["bgm"],
        "title": spec["title"],
        # 模型从用户的话里读出来的整片风格（一句话，见 `cspec.spec_schema` 的 `style`）。
        # 放在卡上的理由只有一个：理解歪了，人一眼看得出来。
        "style": str(spec.get("style") or ""),
        # 风格的可执行拆解（美术导演补）：卡上能看出「像素赛博朋克」到底落成哪几条做法。
        "style_grammar": str(spec.get("style_grammar") or ""),
        # 整片贯穿装置：与 style 一样放在卡上给人看——「理解歪了」在这里最便宜地纠。
        "recurring_prop": str(spec.get("recurring_prop") or ""),
        "plan_table": [_row(s) for s in shots],
        "next": next_hint or (
            "确认这份分镜（或按用户要求改完）之后调 render_motion_code 出片；"
            "它会先把编排结果交用户确认，不会静默开渲"),
    }
