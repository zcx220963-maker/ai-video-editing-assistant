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
from typing import Any, Mapping

from agent_framework import style_authoring
from agent_framework.orchestration import NodeState, _safe
from agent_framework.skill import _parse_frontmatter, _parse_manifest
from agent_framework.style_templates import as_pack_template, ensure_remote, visible
from agent_framework.retrieval import (UNVERIFIED_MARKS, cite_fields, claims_verified,
                                       ledger, matches_ledger)

import os

from ..motion import art_director
from ..motion import code_render as crender
from ..motion import code_spec as cspec
from ..motion import design_review
from ..motion import remotion_client as rc
from ..motion import shot_writer
from .core_nodes import StoryNode, _obj, _resolve_target_duration

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


async def _bind_slots(storage: Any, template: dict[str, Any],
                      overrides: Any, notes: list[str]) -> None:
    """把 `style_overrides` 解成槽位上的 ``bound`` 记录（组件 → 替换件模块）。

    只认**已登记**的替换件（模板 `manifest.variants` 里的 name/module/label）；
    没登记的自由描述不在这里生成——那要先调 ``/variants`` 换件，运行时按默认
    组件画并在回执里说明。这样写手拿到的永远是一条能 import 的路径。
    """
    slots = template.get("slots")
    if not isinstance(slots, list) or not isinstance(overrides, Mapping):
        return
    for comp, want in overrides.items():
        comp = str(comp or "").strip()
        slot = next((s for s in slots
                     if isinstance(s, dict) and str(s.get("component") or "") == comp),
                    None)
        if slot is None:
            notes.append(f"风格包没有槽位「{comp}」，忽略这条替换")
            continue
        if isinstance(want, Mapping):
            want = want.get("variant") or want.get("name") or want.get("label") or ""
        want = str(want or "").strip()
        if not want or want in (comp, "default"):
            # 模板可能把最新替换件标成默认（换完即用）；这里显式回退到自带组件。
            slot.pop("bound", None)
            continue
        options = [v for v in (slot.get("variants") or []) if isinstance(v, Mapping)]
        hit = next((v for v in options
                    if want in (str(v.get("name") or ""), str(v.get("module") or ""),
                                str(v.get("label") or ""))), None)
        if hit is None:
            have = "、".join(f"「{v.get('label') or v.get('name')}」" for v in options[:6])
            notes.append(f"「{comp}」没有替换件「{want}」"
                         + (f"（现有：{have}）" if have else
                            "；先调换件接口给这个槽位做一件")
                         + "，按默认组件画")
            continue
        vname = str(hit.get("name") or "")
        try:
            ok, why = await ensure_remote(storage, vname)
        except Exception as exc:  # noqa: BLE001 - 存储/渲染抖动只降级
            ok, why = False, str(exc)[:120]
        if not ok:
            notes.append(f"替换件「{vname}」暂不可用（{why}），「{comp}」按默认组件画")
            continue
        slot["bound"] = {
            "name": vname,
            "module": str(hit.get("module") or ""),
            "component": comp,
            "label": str(hit.get("label") or ""),
            "contract": [str(p) for p in (hit.get("contract") or []) if str(p).strip()],
            # 替换件可能给联合类型加了取值（更多时代/场景）；写手要用它驱动逐镜变化。
            "contract_text": str(hit.get("contract_text") or ""),
            "hash": str(hit.get("hash") or ""),
        }
        notes.append(f"槽位「{comp}」换成替换件「{hit.get('label') or vname}」")


async def _attach_style_pack(storage: Any, raw: Mapping[str, Any], name: str,
                             user_id: str) -> tuple[dict[str, Any], str]:
    """按名字取风格包，注入 ``spec.style_pack`` 并补齐视觉缺省。

    三条纪律：
    * **风格包是数据，不是流程**：这里只读写 manifest/正文，不执行任何用户代码。
    * **只读本人或系统内置**：别人的包一律当不存在（回执里也不泄露它的存在）。
    * **失败只降级**：不存在 / 不是 style / manifest 坏了 / 无权限，都回一句说明继续，
      不让一份写坏了的风格包把整次分镜拦死（「堵死比白烧更坏」）。
    """
    fm: Mapping[str, Any] = {}
    body = ""
    owner: str | None = None
    row: Mapping[str, Any] | None = None
    try:
        row = await storage.skills.get(name)
    except Exception:      # noqa: BLE001 - 存储不可达时走磁盘种子，不在这里判死
        row = None
    if row:
        fm = row.get("frontmatter") or {}
        body = str(row.get("body") or "")
        owner = row.get("owner_user_id")
    else:
        # 磁盘兜底：editor 可能比 app 先起（系统种子还没同步进 PG），或离线直调。
        # 运行时仍以 PG 为准——这里只是让「同一个镜像里的种子」立刻可用。
        seed = (Path(__file__).resolve().parents[2]
                / "examples" / "skills" / name / "SKILL.md")
        if not seed.is_file():
            return dict(raw), f"风格包「{name}」不存在，按默认视觉继续"
        try:
            meta, body = _parse_frontmatter(seed.read_text(encoding="utf-8"))
        except OSError:
            return dict(raw), f"风格包「{name}」读不出来，按默认视觉继续"
        fm, owner = meta, None
    kind = str(fm.get("kind") or "workflow").strip().lower()
    if kind != "style":
        return dict(raw), f"「{name}」不是风格包（kind={kind}），按默认视觉继续"
    if owner is not None and owner != user_id:
        # 与素材同一口径：别人的东西按不存在处理，不在回执里确认它存在。
        return dict(raw), f"风格包「{name}」不可用，按默认视觉继续"
    manifest = _parse_manifest(fm.get("style_manifest"))
    if not manifest:
        return dict(raw), f"风格包「{name}」的 manifest 读不出来，按默认视觉继续"
    display = str(fm.get("display") or name)
    spec = dict(raw)
    spec["style_pack"] = {
        "name": name,
        "display": display,
        "body": body[:MAX_STYLE_BODY_CHARS],
        "manifest": manifest,
    }
    # 模板是**可执行的那一层**：风格包只写了名字，真正的 import 路径、组件清单与
    # 源码指纹从模板库取。取不到 / 没推成功就**不注入**——写手会按 manifest 自己画，
    # 而不是 import 一个注定打包失败的模块（那会白烧一整镜）。系统内置模板没有源码，
    # `ensure_remote` 直接放行（镜像里已经烤好）。
    template_name = str(manifest.get("template") or "").strip()
    note = f"已注入风格包《{display}》（{name}）"
    if template_name:
        try:
            trow = await storage.style_templates.get(template_name)
        except Exception:      # noqa: BLE001 - 存储抖动不该拦分镜
            trow = None
        if trow is None:
            note += f"；模板「{template_name}」未在库里登记，按正文自行画"
        elif not visible(trow, user_id):
            note += f"；模板「{template_name}」不可用，按正文自行画"
        else:
            ok, why = await ensure_remote(storage, template_name)
            if ok:
                spec["style_pack"]["template"] = as_pack_template(trow)
            else:
                note += f"；模板「{template_name}」暂不可用（{why}），按正文自行画"
    # 槽位绑定：`style_overrides` 把某个组件换成该槽位已登记的替换件；不传就用
    # 模板自己的组件（零参数可渲）。绑定失败只降级成默认组件并写回执，不拦分镜。
    pack_tmpl = spec["style_pack"].get("template")
    bound_notes: list[str] = []
    if isinstance(pack_tmpl, Mapping):
        await _bind_slots(storage, pack_tmpl, raw.get("style_overrides"),
                          bound_notes)
    if bound_notes:
        note += "".join(f"；{n}" for n in bound_notes)
    # 缺省补齐：分镜/卡面显式写了的优先，风格包只填空格。
    cap = manifest.get("caption") if isinstance(manifest.get("caption"), Mapping) else {}
    if not spec.get("caption_style"):
        declared = str(manifest.get("caption_style") or "").strip()
        if declared in cspec.CAPTION_STYLES:
            spec["caption_style"] = declared
        elif str(cap.get("look") or "") == "overlay":
            spec["caption_style"] = "on_picture"
    if not spec.get("recurring_prop") and manifest.get("recurring_prop"):
        spec["recurring_prop"] = str(manifest["recurring_prop"])
    if not spec.get("style") and display:
        spec["style"] = display
    if not spec.get("style_grammar") and manifest.get("recurring_pose"):
        spec["style_grammar"] = str(manifest["recurring_pose"])
    if manifest.get("template"):
        spec["style_template"] = str(manifest["template"])
    return spec, note


#: 自动补件是「整片落地检查」——同一时刻只让一份分镜改模板，避免两个
#: plan_motion 并发写 `manifest.variants`（app 侧换件有 `_deposit_lock`，
#: 编辑器进程是另一个进程，只能各自兜住自己的并发）。
_AUTO_FIT_LOCK = asyncio.Lock()


def _storyboard_digest(spec: Mapping[str, Any]) -> str:
    """分镜 → 落地检查器看的几行：每镜要画什么，比整份 spec 小得多。"""
    lines: list[str] = []
    for s in (spec.get("shots") or []):
        if not isinstance(s, Mapping):
            continue
        text = str(s.get("text") or "").strip()
        brief = str(s.get("brief") or "").strip()
        if not (text or brief):
            continue
        line = f"- 镜{s.get('id') or '?'}：{brief[:400]}"
        if text:
            line += f"｜字幕：{text[:80]}"
        lines.append(line)
    return "\n".join(lines)


async def _auto_fit_template(
        storage: Any, spec: Mapping[str, Any], user_id: str, *,
        user_request: str = "", conversation_id: str = "") -> tuple[str, bool]:
    """分镜要画的内容模板还画不了时，自动给对应槽位补一件。

    用户不该先学会「换件面板」：他在对话框里说完整片，逐镜 `brief` 与
    这一轮的原话已经写清「洞穴、客厅、地铁」「篝火、电视、手机」。这里让
    模型对着槽位清单判断现有取值够不够；不够就按换件同一套闸补一件，
    只补缺、不重写整份模板。

    返回 ``(回执, 是否有零件补成功)``。任何一步失败都只写回执，
    **不拦分镜**——自动补件是省事，不是出片的前提。
    """
    pack = spec.get("style_pack")
    if not isinstance(pack, Mapping):
        return "", False
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    record = pack.get("template") if isinstance(pack.get("template"), Mapping) else {}
    base = str(manifest.get("template") or record.get("name") or "").strip()
    slots = record.get("slots")
    if not base or not isinstance(slots, list) or not slots:
        return "", False
    storyboard = _storyboard_digest(spec)
    request = (str(user_request or "").strip()
               + ("\n\n" if user_request and storyboard else "") + storyboard).strip()
    if not request:
        return "", False
    try:
        row = await storage.style_templates.get(base)
    except Exception:  # noqa: BLE001 - 存储抖动只代表这次不补件
        return "", False
    if not isinstance(row, Mapping) or row.get("owner_user_id") is None:
        # 系统内置模板是共享行，不能往里登记用户替换件。
        return "", False
    try:
        from agent_framework.llm_openai import get_default_llm
        llm = get_default_llm()
    except Exception:  # noqa: BLE001 - 没配模型就按现有取值画
        return "模板落地检查：没有可用的模型配置，按现有取值画", False
    # 这是剪辑服务端（另一个进程），身份 contextvar 不过 HTTP：不显式绑，
    # `resolve_api_key` 拿到的用户是空串，模型键永远解析不出来。与 shot_writer /
    # selfcheck 同一条纪律。
    from agent_framework.identity import use_identity
    done: list[str] = []
    failed: list[str] = []
    try:
        with use_identity(user_id, conversation_id):
            edits = await style_authoring.fit_storyboard(
                llm, base_row=row, slots=slots, storyboard=request,
                identity_user=user_id, conversation_id=conversation_id)
            if not edits:
                return "", False
            async with _AUTO_FIT_LOCK:
                for edit in edits[:4]:
                    comp = str(edit.get("component") or "").strip()
                    if not comp:
                        continue
                    target = str(edit.get("spec") or edit.get("label") or request)
                    try:
                        item = await style_authoring.author_variant(
                            storage, base_name=base, component=comp,
                            target=target[:600], user_id=user_id, llm=llm,
                            identity_user=user_id,
                            conversation_id=conversation_id, intent=edit)
                    except Exception as exc:  # noqa: BLE001 - 一件失败不拖垮整片
                        failed.append(f"{comp}：{str(exc)[:80]}")
                        continue
                    done.append(f"{comp}「{item.get('label') or item.get('name')}」")
    except Exception as exc:  # noqa: BLE001 - 检查失败不代表有缺口
        return ("模板落地检查没跑完（" + str(exc)[:80] + "），按现有取值画"
                if not done else ""), bool(done)
    note = ""
    if done:
        note = ("模板缺的内容已自动补件：" + "、".join(done)
                + "。这一片会直接用补好的零件画；不用再去换件面板。")
    if failed:
        note += ("；" if note else "") + ("自动补件没成功：" + "；".join(failed[:4])
                                        + "（这几样按模板现有取值画）")
    return note, bool(done)


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
        "每镜最少交三样：一句 `text`（字幕文案）、一个 `highlight`（必须是文案子串）、"
        "一句 `brief`（这一镜画什么、怎么构图、字幕排在构图的哪一处、怎么分几拍动）——"
        "**`code` 可省**：不写就由系统按 brief、整片 style 与这一镜文案**单独写**这一镜的"
        "React/TSX（每镜一次短调用，写完自动过 lint，最多改两次）。写了 `code` 的镜以你"
        "写的为准。这样几十镜的代码不会挤在主对话上下文里。"
        "画面全部来自代码逐帧画出来——裸 SVG、`<canvas>`、roughjs、three.js、katex 都行，"
        "不联网、不引用任何外部图片。每镜的代码是**自包含**的：顶层 `<AbsoluteFill>` 铺满整帧，"
        "**先把这一镜文案里能被画出来的东西圈出来**——一个具体物件、一个人物、一个动作、"
        "一组关系，画面就是它的舞台或特写；**这一镜单独截一帧下来要是一幅画，不是一页排版**"
        "（大标题 + 色块 + 几行字那种）。画法不限制：`@dg/lib/draw/*` 与 `@dg/lib/brush` "
        "只是可选参考件，用不上就自己画。"
        "用户对风格只给了一点点方向时，**在那句方向之上自己组合**媒介、笔触、光线、构图与配色，"
        "把整片的美术语言补齐——是往上加，不是照单执行；整片所有镜共用这一套语言。"
        "顶层再给一件 `recurring_prop`：**一个**在画面世界里反复出现的具体道具，"
        "让抽象概念在它身上看得见（如一枚随叙述变化的读数表、一面映出数字的镜子）；"
        "它出现在每一镜写码的提示词里，合适就摆、不硬塞，全片只此一件。"
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
        "**brief 里要写清两件事：这一镜文案里哪几个词变成了画面上的什么东西（怎么摆、光从哪来），"
        "以及字幕排在哪、和画面里什么元素对齐。**"
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
                           "顶层 style 是整片美术语言的起点（用户只给一点方向就在上面加），"
                           "recurring_prop 是全片唯一那件贯穿道具。"
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
        # 风格包：按名字从 PG 取（磁盘种子兜底），把 manifest + 正文注入 spec.style_pack。
        # 排在 expand/复用之前——复用要比对风格包名，补缺省也要在契约校验之前完成。
        # 取不到、不是 style、不属于本人，都只写一句回执继续，不拦分镜。
        style_skill = str(inputs.get("style_skill")
                          or getattr(state, "style_skill", "") or "").strip()
        style_note = ""
        if style_skill:
            raw, style_note = await _attach_style_pack(
                self.storage, raw, style_skill, state.user_id)
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
                                  aspect=aspect)
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
        # 自动落地检查（2026-10-19）：用户只在对话框说片子，不该先学换件面板。
        # 分镜里明确要的「洞穴/客厅/地铁」「篝火/电视/手机」若模板现在画不了，
        # 先按换件同一套闸补一件，再重新解析槽位——后面的写手直接用新件。
        # 排在校验之后：字段都不成立的分镜不值得花补件的钱；失败只写回执不拦。
        if style_skill:
            fit_note, refit = await _auto_fit_template(
                self.storage, spec, state.user_id,
                user_request=getattr(state, "user_request", "") or "",
                conversation_id=getattr(state, "conversation_id", "") or "")
            if fit_note:
                state.summary.notes.append(fit_note)
            if refit:
                spec, rebind_note = await _attach_style_pack(
                    self.storage, spec, style_skill, state.user_id)
                if rebind_note and rebind_note != style_note:
                    state.summary.notes.append(rebind_note)
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
        # 风格包**显式要求**用模板时，模型自己交的代码也过一次「模板 import」这把尺
        # （2026-10-19）。默认不要求：画面由写手自己画，模板只是可参考的现成画法。
        # 写手那条路会退回重写；这里只记账不重写、不拦：分镜已经成型，回执说清楚就够了。
        pack_tpl = ((spec.get("style_pack") or {}).get("template")
                    if isinstance(spec.get("style_pack"), Mapping) else None)
        if isinstance(pack_tpl, Mapping):
            for s in spec.get("shots") or []:
                if not isinstance(s, Mapping) or not str(s.get("code") or "").strip():
                    continue
                miss = shot_writer.template_import_missing(spec, str(s.get("code") or ""))
                if miss:
                    writer_notes.append(f"{s.get('id') or '?'}：{miss}")
        if not cspec.DEMOS_ENABLED:
            # 美术导演（2026-10-18）：一次纯文本调用，把整片风格拆成可执行做法，
            # 并把每一镜的 brief 补成「画什么物件、在哪、多大、光从哪来、字排哪」。
            # 排在写手之前——真机上「不像像素赛博朋克」的 14 镜，根因是 brief 太抽象，
            # 写手没有落点。这一步让第一次落笔就有具体画面；失败不拦，原 brief 照用。
            directed = await art_director.concretize(
                spec, user_id=state.user_id or "",
                conversation_id=state.conversation_id or "")
            wrote = await shot_writer.write_shot_code(
                spec, user_id=state.user_id or "",
                conversation_id=state.conversation_id or "")
            # 导演的账不能丢：它排在写手之前；若直接用写手那一份覆盖，导演的花费与
            # 「未判/补了美术方向」就永远到不了回执（2026-10-19 修）。
            writer_notes = list(directed["notes"]) + list(wrote["notes"])
            writer_cost = (float(directed["cost_yuan"] or 0.0)
                           + float(wrote["cost_yuan"] or 0.0))
            if reused_ids:
                head = "、".join(reused_ids[:8]) + ("…" if len(reused_ids) > 8 else "")
                writer_notes.append(
                    f"复用：{len(reused_ids)} 镜（{head}）的 brief 与本会话上一版逐字相同，"
                    "直接沿用了已写好的代码——没有重写、也没有重渲草稿。"
                    "要重画其中某一镜，用 shots_patch 只交那一镜的新 brief。")
            if wrote["unwritten"]:
                raise ValueError(
                    "逐镜写代码时有几镜没写成，分镜没有入库（"
                    + "、".join(wrote["unwritten"]) + "）：\n"
                    + "\n".join(f"  · {n}" for n in writer_notes[:12])
                    + "\n把这几镜的 brief 写得更具体（画什么、怎么构图、怎么动），"
                    "或者自己直接交这一镜的 code，再调一次本工具。其余镜不必重交——"
                    "用 shots_patch 只交这几镜。")
            # 草稿换成**带代码**的这一版：下一次 shots_patch 只改动过的那几镜，其余镜
            # 的代码直接复用，不会整片重写（那会把写手的钱再花一遍）。
            _remember_draft(state.session_id, spec)
        # 模板文字槽位的内容不能靠缺省值：缺省值是参考片那一镜的原文。真机
        # 2026-10-19 菊花镜：`<StyleCaption>` 没传 `text`，预览左上角显示的是参考片
        # 台词「到山西，下雪了。新靴，还是没舍得穿。」。这里只点名、不拦——分镜已经
        # 成型，用户可以接受；但缺的是哪一镜、哪个组件、哪个 prop 必须说清楚。
        if isinstance(pack_tpl, Mapping):
            slots = cspec.style_template_slots(spec)
            for s in spec.get("shots") or []:
                if not isinstance(s, Mapping):
                    continue
                code = str(s.get("code") or "")
                if not code.strip():
                    continue
                sid = str(s.get("id") or "?")
                for comp, prop, text_kind in cspec.missing_template_content_props(
                        code, slots):
                    role = {"caption": "主字幕", "inscription": "日期铭文"}.get(
                        text_kind, "文字")
                    writer_notes.append(
                        f"{sid}：模板的{role}组件 `{comp}` 没传 `{prop}`——"
                        "模板的缺省值是参考片那一镜的原文，不传就会把参考片的字带上屏。"
                        f"要改就用 shots_patch 只交这一镜，给 `{comp}` 显式传 "
                        f"`{prop}={{这一镜自己的文字}}`。")
                style = str(s.get("caption_style")
                            or spec.get("caption_style") or "")
                if style == "none" and cspec.template_caption_in_code(code, slots):
                    writer_notes.append(
                        f"{sid}：申报了 caption_style=\"none\"（不上字幕），代码里却"
                        "渲染着模板的主字幕组件——它不读这个开关，字仍会上屏。"
                        "要么把这一镜的字幕样式改回要画的那一档，要么别渲染那个组件。")
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
        caption_notes = list(caption_notes) + list(dup_notes)
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
        # 风格包只报名字与模板：正文是给写手/导演的，不必再回一遍主对话上下文。
        pack = spec.get("style_pack") if isinstance(spec.get("style_pack"), Mapping) else {}
        if pack:
            manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
            payload["style_pack"] = {
                "name": str(pack.get("name") or ""),
                "display": str(pack.get("display") or ""),
                "template": str(manifest.get("template") or ""),
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
        if up and os.environ.get("MOTION_PREVIEW_STILLS", "1") not in ("0", "false", "no"):
            # 补丁轮只渲改动过的镜：上一版的预览图还在 `_PREVIEW_REFS` 里，
            # 合并后卡面仍是全片。整份重交＝全片重渲（并清掉旧缓存）。
            repo_key = (state.session_id, state.artifact_id or "_default")
            only = (changed_ids
                    if changed_ids and _PREVIEW_REFS.get(repo_key) else None)
            if only is None:
                _PREVIEW_REFS.pop(repo_key, None)
            refs, notes, layouts, files = await self._publish_previews(
                state, spec, only=only)
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
            # 视觉评审（M2）：一次带图调用同时判「这一帧作为一张画够不够好」与
            # 「像不像整片那句风格」，红镜带着评审意见重写一遍，只重渲改过的镜。
            # **只报不拦**——评审红绝不打回分镜。
            #
            # 2026-10-18 把原来的 `selfcheck.style_review` 并进这里：同一张静帧分两次问，
            # 每镜多一次多模态往返；更贵的是风格不符的镜当时没有重写入口，清单只能交回
            # 主对话，主对话再走一整轮 plan_motion（全片重写码、重渲、重评）。
            if files and design_review.review_enabled(spec):
                art = spec.get("art") if isinstance(spec.get("art"), Mapping) else {}
                visual = await design_review.review_stills(
                    shots=spec["shots"], stills=files, art=art,
                    prop=str(spec.get("recurring_prop") or ""),
                    style=str(spec.get("style") or ""),
                    grammar=str(spec.get("style_grammar") or ""),
                    style_pack=spec.get("style_pack"),
                    user_id=state.user_id or "", conversation_id=state.conversation_id or "")
                spent = float(visual["cost_yuan"])
                # `files` 是评审看过的那批静帧：把每镜自己那张图一并交给写手，
                # 让它改的是真渲出来的画面，而不是文字描述在它脑子里的投影。
                rewritten = await design_review.rewrite_failed(
                    spec=spec, failed=visual["failed"], stills=files,
                    user_id=state.user_id or "", conversation_id=state.conversation_id or "")
                spent += float(rewritten["cost_yuan"])
                fixed = [sid for sid, r in rewritten["rewritten"].items() if r.get("ok")]
                after: dict[str, Any] = {}
                if fixed:
                    refs2, notes2, _layouts2, files2 = await self._publish_previews(
                        state, spec, only=set(fixed))
                    files.update(files2)
                    notes.extend(notes2)
                    payload["preview_frames"] = _remember_previews(
                        state.session_id, state.artifact_id or "_default",
                        refs2, spec)
                    payload["preview_notes"] = notes[:12]
                    # 重写后的镜再看一眼（只花这几镜的钱）。仍红的照交，由模型决定
                    # 出片前要不要用 shots_patch 再改——不在这里做第二轮自动重写。
                    after = await design_review.review_stills(
                        shots=spec["shots"], stills=files2, art=art,
                        prop=str(spec.get("recurring_prop") or ""),
                        style=str(spec.get("style") or ""),
                        grammar=str(spec.get("style_grammar") or ""),
                        style_pack=spec.get("style_pack"),
                        user_id=state.user_id or "",
                        conversation_id=state.conversation_id or "")
                    spent += float(after["cost_yuan"])
                    # plan_table 里 code_chars 是重写前算的；改过的镜要刷新，否则
                    # 卡面上的字节数还指着上一版。
                    payload["plan_table"] = plan_payload(
                        spec, target=target, gate=gate)["plan_table"]
                payload["visual_review"] = {
                    # 补丁轮只复查改动过的镜（省掉全片重渲重评）；整份重交是全片。
                    "scope": "changed" if only else "all",
                    "checked": visual["checked"],
                    "passed": visual["passed"][:24],
                    "failed": visual["failed"][:16],
                    "unjudged": visual["unjudged"][:24],
                    "rewritten": rewritten["rewritten"],
                    "skipped": rewritten["skipped"],
                    "after": ({"passed": after.get("passed", []),
                               "failed": after.get("failed", [])[:16],
                               "unjudged": after.get("unjudged", [])}
                              if after else {}),
                }
                if visual["failed"]:
                    lines = [f"带图看了 {visual['checked']} 镜，{len(visual['failed'])} 镜"
                             "按五条评审没通过（画面作为一张画还不成立）。"]
                    if fixed:
                        lines.append(f"已自动按评审意见重写并重渲：{'、'.join(sorted(fixed))}；"
                                     "重写后的结果见 visual_review.after。")
                    left = [str(f.get("shot")) for f in after.get("failed", []) if f.get("shot")]
                    if left:
                        lines.append(f"重写后仍红：{'、'.join(left)}——照交，**不影响入库**。")
                    if rewritten["skipped"]:
                        lines.append(f"没改成的镜：{'、'.join(rewritten['skipped'])}"
                                     "（重写没写成，已保留上一版能跑的代码）。")
                    lines.append("**可以直接出片，不必为了这几镜再交一轮分镜。**"
                                 "要微调就用 shots_patch 只交那几镜；成片后也可以用 "
                                 "patch_motion_code 单镜重画。")
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
                                ) -> tuple[dict[str, str], list[str], dict[str, Any], dict[str, Any]]:
        """渲每镜预览静帧并发布 → ({镜号: 对象键}, 说明, {镜号: 版面账}, {镜号: 本地图片})。

        ``only`` 只渲这几镜（视觉评审改过的镜要重渲，不必把整片再渲一遍）。
        """
        sess = state.session_id
        artifact = state.artifact_id or "_default"
        target = spec
        if only is not None:
            target = {**dict(spec), "shots": [
                s for s in (spec.get("shots") or [])
                if str(s.get("id") or "") in only]}
        files, notes, layouts = await crender.preview_stills(
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
        return refs, notes, layouts, files


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


def _style_key(raw: Any) -> tuple[str, str]:
    """风格包的复用键：`(包名, 模板名)`。空元组表示「没有风格包」。

    只认这两项：它们决定代码长什么样。manifest 里的颜色/构图微调不参与——
    调一个色号不该让整片重写；但要换风格包或换模板，旧代码就不再成立。
    """
    if not isinstance(raw, Mapping):
        return ("", "")
    pack = raw.get("style_pack")
    if not isinstance(pack, Mapping):
        return ("", "")
    manifest = pack.get("manifest") if isinstance(pack.get("manifest"), Mapping) else {}
    return (str(pack.get("name") or ""),
            str(manifest.get("template") or raw.get("style_template") or ""))


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
    def lint(code: str, names: list[str], assets: list[Any]) -> dict[str, Any]:
        return rc.lint(code, declared_fields=names, assets=assets, timeout=PRELINT_TIMEOUT)
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
                got = await asyncio.to_thread(lint, code, names,
                                              list(shot.get("assets") or []))
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
                   shots: int = 0, aspect: str = "landscape") -> dict[str, Any]:
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
