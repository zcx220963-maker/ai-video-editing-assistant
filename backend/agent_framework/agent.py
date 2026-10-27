"""Agent 循环核心：AgentOnceRun（内层 ReAct）与 Agent（外层入口）。

设计对应：
- Main Loop：常驻消费各 Session 的用户请求。**进程里只有一处**——MQ 那条路
  （``SessionConsumer`` 订阅 chat topic，key=session_id 分区；见 mq.py/consumer.py），
  ``Agent`` 这一层只提供同步语义的 ``handle`` / ``plan`` / ``resume`` / ``fork`` /
  ``approve``，每个入口按 session 加锁（``_lock_for``）。这里原先还自带一套
  进程内队列的 ``while True``，没有调用方，已删（见类尾那段注释）。
- AgentOnceRun：单次请求内、带最大迭代保护的 ReAct 循环
  （LLM → 需要工具？→ Registry 执行 → 回填 → 再 LLM …… → 最终结果）。
- 工具并发：被 LLM 选中的工具二次判定 concurrency_safe，只读安全的进 Batch
  用 asyncio.gather 并发，其余按序独占执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

from . import cost as cost_mod
from . import judge as judge_mod
from .ask_gate import (looks_like_asking_user, looks_like_plan_invite,
                       no_popup_note, plan_card_nudge, popup_nudge)
from .checkpoint import Checkpoint, CheckpointManager
from .catalog import get_catalog
from .compress import (DEFAULT_FOLD_AT_BYTES, DEFAULT_KEEP_RECENT_RESULTS,
                       DEFAULT_RESULT_MAX_BYTES, FOLD_MARKER, ToolResultGate,
                       _repair_orphans, fold_tool_schemas)
from .skill import INDEX_ONLY_NOTE
from .context import (ContextBuilder, LEDGER_ENTRY_TYPE, Section,
                      check_tools_budget, tools_box)
from .hooks import AgentHook, AgentHookContext, CompositeHook, _current_hook_ctx, _current_hooks
from .identity import (current_identity, rebind_artifact_scope,
                       storyline_session_id, use_identity, use_identity_or_inherit)
from .llm import LLMClient, LLMResponse
from .messages import ToolCall, assistant, system, tool_result, user
from .motion_prefs import MOTION_KNOB_NODES
from .motion_prefs import pin_tool_calls as pin_motion_prefs
from .prompts import prompt_text, read_prompts_file
from .plan import (SubmitPlanTool, ConfirmPlanTool, claims_plan_card, claims_step_executed,
                        pending_continuation, planning_section_blocks,
                        preload_skills, probe_capabilities, reconcile,
                        render_capability_notes, render_injections,
                        sections_for_plan,
                        ROUND_FILTER, SCOPE_CONDITION_KEY, SCOPE_SECTION,
                        plan_nodes, registry_for_round, scope_lines, trimmed_nodes)
from .plan_retro import (PlanRetrospective, plan_card_lines, rework_line,
                         revise_line)
from .render_gate import (ADJUST_OPTION, CONFIRM_OPTION,
                          KEEP_FULL_OPTION, MOTION_KNOBS, MOTION_NODE, RENDER_NODE,
                          RENDER_NODES,
                          TRUNCATE_OPTION, build_render_ask,
                          decision_is_confirm, decision_text,
                          ungated_render_nodes)
from .run_state import RunState, tool_names_in
from .session import Session, SessionManager
from .tool import ToolError, ToolRegistry, is_tool_error, plan_batches


#: 节点校验打回的标记语：带着「照清单改」的错误是**可执行的反馈**，不是工具坏了。
#: 剪辑服务端的分镜/出片节点打回时一律带其中一句（motion_plan / motion_code_nodes）。
#: 真机 run d27b37bf1419：同一份分镜四次都是「改完再交」的清单（lint 一次、对比度三次），
#: 却被当成「工具连续失败 4 次」熔断，模型只好换去 render_web 出了一条 HTML 幻灯片。
_ACTIONABLE_MARKERS = ("没有入库", "照清单改", "改完重调", "改完再调", "改完先重调",
                       "一帧都没开始渲")
#: 反馈类打回的上限：防的是「改来改去总不对」的死循环，给得比真失败宽得多。
_MAX_FEEDBACK_ROUNDS = 8


def _call_fingerprint(tc: Any) -> str:
    """一次工具调用的参数指纹——用来判「这次提交和上次是不是同一个东西」。

    只取参数、不取 ``id``（``id`` 每次调用都新，拿它比等于永远不重复）。
    排序键，免得同一个 dict 因键序不同算出两个指纹。
    """
    args = getattr(tc, "arguments", None)
    if not isinstance(args, str):
        args = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(args.encode("utf-8")).hexdigest()[:16]


def _last_render_failed(messages: Sequence[Any], node: str) -> bool:
    """这条出片通道最近一次的结果（它自己的回执，或 render_status 的终态）是不是失败。"""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        name = str(m.get("name") or "")
        if name != node and not name.endswith("render_status"):
            continue
        text = str(m.get("content") or "")
        return (text.startswith("Error") or '"status": "failed"' in text
                or "渲染失败" in text)
    return False


def _is_actionable_feedback(r: Any) -> bool:
    text = str(getattr(r, "detail", "") or r)
    return any(m in text for m in _ACTIONABLE_MARKERS)


# 同一个工具连续失败多少次后，本轮不再真执行（成功一次即清零）。
# 真机规划轮靠「猜键名读产物」连着错六十次烧穿迭代预算，提示词里的「请勿重试」拦不住。
_MAX_CONSECUTIVE_FAILURES = 4

#: 撞上 ``_MAX_CONSECUTIVE_FAILURES`` 后**从工具清单里摘掉**，不再给模型发 schema。
#: 只靠「拒绝执行」拦不住：真机 2026-10-19 run `1488197dd8a0`（娱乐史 14 镜）里
#: ``web_search`` 同一句查询被拒 5 次，模型仍然原样再打，最后按原地打转停成 failed，
#: 白烧 ¥0.24、一张计划卡都没出。把选项拿掉，它只能换工具或交卡。
#: 这几个永远保留：规划轮的出口不能被摘，否则模型无处交付。
_NEVER_HIDE_TOOLS = frozenset({"submit_plan", "ask_user"})

#: 工具面 schema 的字节预算（R11 第一级：工具元信息折叠）。
#:
#: 超预算时，**没在本轮用过、也不在计划里**的工具只留一行描述（参数结构不动，
#: 否则模型不知道该传什么）。0 = 关闭折叠，永远发全量。
#: 兜底出口：`get_definitions(user_id=...)` / `GET /tools` / 剪辑侧 `dag_contract`。
_TOOL_SCHEMA_MAX_BYTES = int(os.getenv("TOOL_SCHEMA_MAX_BYTES", "60000"))

#: 原地打转停跑之前，给「摘掉死工具、强制交付」留几轮。
#:
#: 为什么需要：摘 schema 发生在**叫模型之前**，而原地打转守卫也在叫模型之前；
#: 真机 run `b0476d367741`（2026-10-19，同一份娱乐史提示词）里，第 4 次连续失败
#: 与第 6 轮打转落在**同一轮**——守卫先停，摘工具那一轮永远没发生，于是模型至死
#: 都在原样重打 ``web_search``，白烧 ¥0.24、零张计划卡。留一轮的好处是：停跑前
#: 模型终于看见一张没有 ``web_search`` 的工具面，只能拿已有信息交卡或问用户。
#: 只给 1 轮：摘掉之后还打不出交付物，就是这个模型确实走不动了，再给只是白烧。
_MAX_STALL_RESCUES = 1

# ---- 无进展重复调用熔断 ----
#
# 上面那道守卫只数**失败**。真机实测的另一族打转它一次都没抓到：一条规划轮里
# ``web_search`` 用同一句查询打了 20+ 次，每次都**成功**（免 key 链回了一篇毫无关系的
# 维基页面），40 迭代烧掉 1,953,686 token、``plan_run_id`` 为空＝一张计划卡都没出。
#
# 判据取「同一份参数打出逐字相同的结果」而不是「参数相同」——后者会误杀轮询：
# 外部状态在变时结果字节就在变，streak 归 1，永远攒不到闸（读产物、查进度照跑）。
# 第二条闸按参数计次，兜的是「每次结果略有不同但同参重复到这个数目」那类抖动型打转。
_MAX_SAME_RESULT_REPEATS = 2   # 同参同结果最多真跑 2 次，第 3 次不再打后端
_MAX_SAME_ARGS_ATTEMPTS = 5    # 同参（结果在变，可能有进展）最多真跑 5 次
_MAX_REFUSALS_BEFORE_FAILURE = 2

#: **滑动窗口**防循环（2026-10-23）。上面两道闸都只数**连续**相同的调用，于是
#: 「在两个变体之间交替」永远撞不到上限。真机 run（09:34-09:42）：`pip install X`
#: 被工具拒绝后，模型交替着重试不同写法，200 轮打满、1541 万 token、¥1.13，零产出。
#: 这里按「最近 N 次工具调用里，同一签名出现过几次」记，与连不连续无关。
_REPEAT_WINDOW = 16              # 看最近这么多次工具调用
_REPEAT_WINDOW_LIMIT = 5         # 同一签名在窗口里出现这么多次就拦   # 同样的事实告过两次还照打 → 改记真失败，让老守卫接手

# ---- 读卡上限（家族级熔断）----
#
# 上面两条判据都按**参数**计数，于是真机 2026-10-14 那两次失败一次都没被拦下：
# 模型是一份一份地读**不同的** shot 卡——每份参数都是新的、每次调用都成功，
# 逐字相同那两条闸永远攒不满。实测 run `a3d8ff08098a` / `10292c4bf616` 各 40 轮
# 撞上 ``max_iterations`` failed，后者 158 次工具调用里 **144 次是 load_skill**、
# 读了 45 张不同的卡，**一次都没调用 submit_plan**，零张计划卡。
#
# 而这个上限不是新纪律，是 SKILL.md 早就写死、只是没人执行的那一条：
# 「**不要读超过 30 张卡。** 133 张卡不需要全看，INDEX.md 的一句话描述足够选卡。」
# 索引够用这句禁令才成立——2026-10-14 上半场索引是空的（§9-Q140），禁令就只剩
# 「凭卡名猜」，模型只能逐张打开；下半场索引修好了，模型**照样**逐张打开。
# 所以这条闸不能只靠「把索引修好」的道德劝说，要在执行层拦住。
#
# 2026-10-16 改成**两档**（原来只有一档硬拦，30 就是它）：软线到了**照跑**、只在越过
# 的那一刻往回执之后补一句提醒；硬线 60 才是老行为（不执行 + 升级成真失败，让
# `_MAX_CONSECUTIVE_FAILURES` 接手）。为什么不硬停：模型读几张卡是它的选择（§2.2），
# 服务端该做的是把「索引够选卡了」说给它听，而不是替它把路封掉；而「无限读下去」
# 那个真事故（144 次 load_skill / 零张计划卡）由硬线接住。
# 软线取 30 是为了与 SKILL.md 那句话同一个数——正文说 30、代码拦在别处，模型读到的
# 就是两份规矩（15 那一版已经踩过：正文写 15、代码拦在 15，看着对，改一处就散）。
_MAX_SHOT_CARDS = 30
_MAX_SHOT_CARDS_HARD = 60

# ---- 读技能正文的分页上限（同一族的第二条家族级闸）----
#
# 读卡闸治不了的那一维是**同一份正文的重叠窗口**。真机 run `c364585634f2`
# （2026-10-15，「已达最大迭代次数 40」）里 26 份**参数各不相同**的 load_skill，
# 19 份打在 motion_explainer 的 SKILL.md 上：offset 220 取了三个窗口（limit 60/180/300）、
# 531 先 limit=190 再 limit=180、715 与 720、895 与 896 各差一两行——
# 每份参数都是新的、每一枪都成功，参数级那两条闸一次都没响。
#
# 上限不能压到「只许整份取一次」：正文过 24 KB 会被压缩闸从中间切掉，
# 按大段续读（limit≈100 行）是 compress.py `_RECOVERY_HINT["load_skill"]` 教的
# 正确形状——教的是大段，不是一行一行。
# 两档同上一族的理由，取值按「一份千行正文老实翻完要几页」定：
# 软线 16 页（每页一两百行够翻完整份）过了就提醒一句「再换起始行不会有新信息」；
# 硬线 32 页只用来接住打转——真机那条 26 枪的形状（19 个重叠窗口）在软线之下，
# 今天一枪不拦，只多一句提醒，这正是 §2.2 要的「读多少由模型定」。
_MAX_SKILL_BODY_PAGES = 16
_MAX_SKILL_BODY_PAGES_HARD = 32

# ---- 读**附件**的分页上限（同一族的第三条家族级闸）----
#
# 正文闸与读卡闸都接不到的那一维：模型对**同一份附件**按行磨。真机 2026-10-17 的
# 「薄肌理论」run（293 行开场 demo）与 2026-10-18 的 run `31af0aeaeda8`（397 行
# `pipeline.md`）是同一种形状：`offset=N, limit=1` 一路加一，每枪参数都不同、每枪
# 都成功——参数级两条闸全程没响，200 轮迭代全烧在翻行上，一个计划都没交。
#
# 取值与正文同族的理由：附件通常比正文短，但同样的「重叠窗口」病会发作。软线只
# 提醒、硬线才拒；**整份一次取回（不带 offset/limit）不占窗口额度**——那正是
# 硬线回执给出的出路，也是这个工具最省事的一条路。（它仍受「同一段已经给过、回执
# 还在上下文里」那一条约束，见 `_page_covered`；被折叠收走之后整份重取照常放行。）
_MAX_ATTACH_PAGES = 8
_MAX_ATTACH_PAGES_HARD = 16

# ---- 整 run 的技能阅读总量（同族第四条家族级闸）----
#
# 前三条家族级闸都按**一份文件**计数：正文、附件、shot 卡各有自己的窗口上限。真机
# 2026-10-19 run `8f43a2e43fde`（auto 入口直接进执行轮，35 轮 failed）接不到这一维：
# 模型先把 motion_explainer 的正文按 9 个窗口读完（≈104 KB），再把 6 份附件逐份整取
# 或分页读完（又 ≈90 KB）——**每一份都在自己的上限之内、每一枪都成功，三条闸一条
# 都没响**。随后它把 already-read 的 `aesthetic-rules.md` 第 1–190 行按
# `1:60 / 61:60 / 121:70` 又取一遍，第三遍被「同参同结果」闸拦下后连打 6 次，
# 整条 run 按「原地打转」停下，用户看到的是「又卡死了」。
#
# 这一条只看**本 run 经 load_skill 走了几枪**（与联网取材那道「全 run 总枪数」同一
# 口径），不看单份：
#   软线 到了照跑，只插一句 system：材料已够，去动作；
#   硬线 起不再执行，回执点名「停止阅读、用读到的内容推进」。
# `section=` 单取**永不受这条限制**——它是硬线之后唯一该走的路（按块取、一次几百到
# 几 KB），也是这条闸不能堵死的那条缝。跨文件不设「整份重读也拦」的绝对禁令：
# 早先的回执被折叠闸收走时，重取是恢复路径（见 `_page_covered` 的 visible 参数）。
#
# 取值按「一份千行正文 + 六份附件老实读完」估：正文 9 页 + 附件 6 次 ≈ 15 枪。
# 软线 10 枪比「认真读完」早几枪，正好在它开始重复之前把话说给模型听；硬线 20 枪
# 兜住「读完再读一遍」那种打转，又不至于把还没读完的附件真正卡住。
_MAX_SKILL_READS = 10
_MAX_SKILL_READS_HARD = 20

# ---- 整 run 的技能阅读**字节**（同族第五条，与上面那条枪数互补）----
#
# 枪数闸数的是「几枪」，接不到「每枪都很大」：真机 2026-10-21 run `96b6132df10b`
# 只用了 27 枪，可每一枪都是整块正文或整页附件，累计 ≈170 KB——单次请求的 prompt
# 冲到 19.5 万 token（上下文 128K），供应商直接把连接掐了。接着压缩闸把早先的回执
# 折叠成存根，模型拿不到内容就回头重取，重取又被「同参同结果」闸拒——
# **折叠 → 重取 → 被拒 → 再重取**，六轮之后整条 run 按「原地打转」停下。
# 用户看到的是「还是卡死」。
#
# 所以这一条只数**新内容**：同一份内容重取（折叠之后的正路）不计字节、也不受硬线
# 约束；只有「还没读过的新材料」才往里加。这样它既能兜住上下文爆窗，又不会把恢复
# 路径堵死（「堵死比白烧更坏」）。
#
# 取值：规划侧真需要的只有预注入的默认集（≈25 KB，不走工具、不计入）+ 色板那类
# 整片字段（≈11 KB）+ 一两份流程附件。
#
# **2026-10-24 重算：跟着模型窗口走，不再写死。**
#
# 原来那组 80/110 KB 是按 **128K 上下文**校准的（170 KB 技能材料 → prompt 19.5 万 token
# → 供应商掐连接）。但这条链的模型窗口现在是 **1M token**——`compress.DEFAULT_FOLD_AT_BYTES`
# 早就按 1M 校准过了（1 token ≈ 2.7 字节，1 MB 工具结果 ≈ 37 万 token）。
# 110 KB ≈ 4 万 token，只有窗口的 4%——在「手册还没读完」的位置就把路封掉，
# 用户看到的是「读不完整还花钱」。
#
# 现在按折叠闸的比例算：技能材料最多占折叠线的 **50% / 70%**
# （500 KB / 700 KB ≈ 18.5 万 / 26 万 token），剩下的留给模型自己写的代码
# （真机一条约 67 万字节 ≈ 25 万 token）与历史。
# 折叠线本身随模型窗口变，所以这两个数也会跟着变，不用再手改。
#
# 想自己定：环境变量 `SKILL_BYTES_SOFT` / `SKILL_BYTES_HARD`（单位 KB）。
def _env_kb(name: str, default_kb: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default_kb * 1000
    try:
        return max(0, int(float(raw))) * 1000
    except ValueError:
        return default_kb * 1000


_MAX_SKILL_BYTES = _env_kb("SKILL_BYTES_SOFT",
                           int(DEFAULT_FOLD_AT_BYTES * 0.5) // 1000)
_MAX_SKILL_BYTES_HARD = _env_kb("SKILL_BYTES_HARD",
                                int(DEFAULT_FOLD_AT_BYTES * 0.7) // 1000)

# ---- 流式调用断线的重发 ----
#
# 2026-10-21 真机 run `006aa898d4fa`：模型连着读了 19 轮技能手册（135 万 prompt tokens，
# 一次计划卡都没交），第 20 轮请求发出去后供应商把流掐了——
# `peer closed connection without sending complete message body (incomplete chunked read)`，
# 被 openai 包成 `APIConnectionError: Connection error.`，界面上一句红字「Connection error.」。
# 一次断线就把二十多分钟的 run 判死；而它读进去的东西全在上下文里，本来接着往下走就行。
#
# 只重发「一个 delta 都还没吐出来」的断线：吐过字的再重发会让同一段回答在界面上出现
# 两遍（hook 已经把前面那些字推给前端了）。断线若发生在吐字之后仍按失败上报，交续跑接。
_STREAM_RETRIES = 2
_STREAM_RETRY_SLEEP = 2.0
#: 流里**多久没有新块**就判它卡死并重发（秒）。供应商的连接会一直开着、心跳照发，
#: 可内容半天不来——SDK 的 60 秒读超时按「有字节」算，心跳让它永远不响。真机
#: 2026-10-21 run `270be6a9866b`：第 7 轮工具回执落库之后，进程 CPU 0.15%、状态
#: `S (sleeping)`，21 分钟没有下一条记录，界面上就是「分镜这么久」。
#: 取 120 秒：十几万 token 的 prompt 首块偶尔要等一分多钟，再短会把正常调用掐掉。
_STREAM_STALL_SEC = 120.0
#: 按异常类名判：这一层不 import 供应商包（`openai` / `httpx` 的类会在不同版本里搬家，
#: 而这个文件不该为了 `isinstance` 把一个可选依赖变成硬依赖）。4xx 由 `status_code` 排除。
_TRANSIENT_STREAM_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError", "RemoteProtocolError", "ReadError",
    "ReadTimeout", "ConnectError", "ConnectionError", "ConnectionResetError",
    "TimeoutError", "InternalServerError",
})


def _transient_stream_error(exc: BaseException) -> bool:
    """断线/超时这类「重发一次就好」的错误；4xx 与业务错误不放行（重发也是同一句）。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    for _ in range(4):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        status = getattr(cur, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500:
            return False
        if type(cur).__name__ in _TRANSIENT_STREAM_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _digest(obj: Any) -> str:
    """按 JSON 字节算的稳定摘要。

    必须按字节而不是 ``==``：字典相等会放过「缺省 44 与显式 44.0」那类归一化差异，
    而缓存键、逐镜比较正是按字节判等的（真机踩过一次假合同）。
    """
    try:
        payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(obj)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _repeat_key(name: str, arguments: Any) -> str:
    return f"{name}\x00{_digest(arguments if arguments is not None else {})}"


#: 一张 shot 卡：`shots/<类别>/<卡名>.md`。
_SHOT_CARD_RE = re.compile(r"^shots/[^/]+/[^/]+\.md$")


def _call_args(arguments: Any) -> dict[str, Any] | None:
    """工具参数取成字典；拿不到就回 None（调用方把这枪当「不认识」放行，不报错）。

    参数到这一层可能还是 JSON 字符串（模型直出、未过解析那一路），所以两种形状都得认。
    """
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _norm_path(value: Any) -> str:
    path = str(value or "").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _shot_card_path(name: str, arguments: Any) -> str:
    """这一枪读的是不是某张 shot 卡；是就返回归一化后的路径，否则空串。

    **只认 `load_skill` + `path=shots/<类别>/<卡>.md` 这一种形状，索引不在其中。**
    `shots/INDEX.md` 恰恰是「一次调用顶掉全部读卡」的那一份：把它也限进上限，
    等于把模型唯一的选卡依据也拿走——2026-10-14 上半场正是因为索引是空的才逼出
    逐张读卡（§9-Q140），这道闸要是顺手把索引也拦掉，就是把那个坑再挖一遍。

    归一化只做两件必要的事：去掉首尾空白与 `./` 前缀，好让 `./shots/a/b.md`
    与 `shots/a/b.md` 算同一张卡（否则换个写法就绕开了计数）。
    """
    if name != "load_skill":
        return ""
    args = _call_args(arguments)
    if args is None:
        return ""
    path = _norm_path(args.get("path"))
    return path if _SHOT_CARD_RE.match(path) else ""


def _skill_body_page(name: str, arguments: Any) -> tuple[str, str] | None:
    """这一枪读的是不是**某技能正文**的某一页；是就返回 ``(技能名, 页键)``，否则 None。

    「缺省取正文」和 ``path="SKILL.md"`` 是同一件事（见 skill.py ``LoadSkillTool.execute``
    的分支），两种写法都得算正文，否则换个写法就绕开计数。附件（``path`` 是别的相对
    路径）**不算**：shot 卡有读卡上限那一族闸，其余附件是「一次取一份不同文件」，
    重复取同一份归参数级那两条闸管。

    页键只认 ``(起始行, 行数)``，缺省 offset 记成 1、缺省 limit 记成 ``whole``：
    「整份」与「offset=1 整份」是同一页。行号按整数归一，好让 ``"220"`` 与 ``220`` 同键。
    """
    if name != "load_skill":
        return None
    args = _call_args(arguments)
    if args is None:
        return None
    skill = str(args.get("name") or "").strip()
    if not skill:
        return None
    if _norm_path(args.get("path")).lower() not in ("", "skill.md"):
        return None
    # `section=` 取的是**分段块**，不是行窗口：做成 `section:<键名>` 单独记账，否则
    # 它会被当成 `1:whole`（缺省 offset/limit）——读了一块之后，整份正文就被判成
    # 「已经给过」，反过来把还没读的部分也堵死。
    section = str(args.get("section") or "").strip()
    if section:
        return (skill, f"section:{section}")

    return (skill, _page_key(args))


def _page_key(args: dict[str, Any]) -> str:
    """调用参数 → 页键 ``起始行:行数``（缺省 offset=1、limit=``whole``）。

    行号按整数归一，好让 ``"220"`` 与 ``220`` 同键。
    """
    def _line(value: Any, default: int | str) -> int | str:
        if value is None:
            return default
        try:
            return max(1, int(float(str(value).strip())))
        except (TypeError, ValueError):
            return default

    return f"{_line(args.get('offset'), 1)}:{_line(args.get('limit'), 'whole')}"


def _page_span(key: str) -> tuple[int, int | None] | None:
    """页键 → ``(起始行, 结束行)``；``结束行=None`` 表示到文件末尾。

    ``section:`` 键不是行窗口，返回 None（由 `_page_covered` 按等值判）。
    """
    if key.startswith("section:"):
        return None
    off, _, lim = key.partition(":")
    try:
        start = int(off)
    except (TypeError, ValueError):
        return None
    if lim == "whole":
        return (start, None)
    try:
        return (start, start + int(lim))
    except (TypeError, ValueError):
        return None


def _page_label(key: str) -> str:
    """页键的人话：``220:180`` → 「220 行起 ×180 行」，``1:whole`` → 「1 行起整份」。"""
    if key.startswith("section:"):
        return f"section={key.split(':', 1)[1]}"
    off, _, lim = key.partition(":")
    return f"{off} 行起" + ("整份" if lim == "whole" else f"×{lim} 行")


def _page_covered(pages: Mapping[str, str], key: str,
                  visible: set[str] | None = None) -> bool:
    """这一页是不是**整段**都落在本 run 已经给过的某一页里。

    ``pages`` 是 ``{页键: 那一枪的 tool_call_id}``。``visible`` 非空时，只认结果
    **还在上下文里**的那些页：被折叠闸收走的回执（存根只有 200 字符）不算「给过」
    ——否则模型真把内容弄丢之后再取就被判成重读，两条机制顶牛（见
    `test_tool_failure_guard` 的 ㉔ 那条 run d433）。
    """
    span = _page_span(key)
    if span is None:
        for page, cid in pages.items():
            if page == key and (visible is None or cid in visible):
                return True
        return False
    start, end = span
    for page, cid in pages.items():
        if visible is not None and cid not in visible:
            continue
        prior = _page_span(page)
        if prior is None:
            continue
        p_start, p_end = prior
        if p_start <= start and (p_end is None or (end is not None and end <= p_end)):
            return True
    return False


def _skill_attach_page(name: str, arguments: Any) -> tuple[str, str, str] | None:
    """这一枪读的是不是某个**附件**的某一页；是就返回 ``(技能名, 附件路径, 页键)``。

    与 `_skill_body_page` 分工：正文（缺省或 ``SKILL.md``）归那一条，这里只认别的
    相对路径。整份取也返回页键 ``1:whole``——**由闸那条豁免**（整份取是硬线之后
    唯一的出路，不能把它封死），这样「什么算附件」只在这一个函数里定义一次。

    shot 卡也归这一族：读卡闸按**张**计数，看不见同一张卡被取了几十个行窗口；
    两条闸各数各的一维，不冲突。

    真机形状（2026-10-18 run ``31af0aeaeda8``）：模型对 ``pipeline.md`` 连打
    ``offset=N, limit=1``，N 一路加一——每枪参数都不同、每枪都成功，参数级判据
    一次都不会响，必须由这一层按**文件 + 窗口**计数。
    """
    if name != "load_skill":
        return None
    args = _call_args(arguments)
    if args is None:
        return None
    skill = str(args.get("name") or "").strip()
    path = _norm_path(args.get("path"))
    if not skill or path.lower() in ("", "skill.md"):
        return None
    return (skill, path, _page_key(args))


# 单次工具调用硬超时（秒）：MCP tool_timeout=600s 在此之前先触发并给出更具体的错误；
# 这里兜底的是本地工具或 MCP 传输层自身卡死的情况——不给这层就会永远挂住。
_TOOL_CALL_TIMEOUT = 650.0

# 少数工具**合法地**比一次工具决策慢得多，给它们单开一档。
#
# 这一档的来历：2026-10-18 真机 run 8c5e161ddb20（32 镜）在 600 s 处被 MCP 口掐断，
# 服务端其实还在写；模型看到「超时」连着重发了三次，同一份分镜的钱花了三遍。
# 掐断**不等于**服务端停了——这类链一次调用就该跑完，所以把这一档放到整片真的写完为止。
# 其他工具仍走 650 s：慢的是一次调用跑完整条的形状，不是全体的形状。
#
# 现在走这一档的是简洁视频线的三个：`make_short_video` 是"写 15 镜 + 质检 +
# 渲 1800 帧"，实测 15 分钟（一次调用跑完整条）；`reshoot_shot` 只重写一镜 +
# 2 张静帧，900 s 够到三倍余量；`render_short_film` 不调模型、只重渲，3600 s。
# （原先这里还有 `plan_motion`，该图形科普链已于 2026-10-25 整条删除。）
_TOOL_CALL_TIMEOUTS: dict[str, float] = {
    # 180 秒那条：生成 ~10 分钟 + 渲染 ~30 分钟。给一小时。
    "make_short_video": 3600.0,
    "render_short_film": 3600.0,
    "reshoot_shot": 900.0,
}

# 渲染代查期间的心跳间隔（秒）：租约 600s，留出足够余量再续一次，
# 避免长渲染代查跑到一半被别的实例判定为「租约过期」而抢走同一条 run。
_RENDER_BEAT_SEC = 120.0

# 规划轮里假称「计划卡已提交」时的补救：循环先追一句核对、再给它一次真出卡的机会；
# 预算用尽仍在声称，就在终答末尾补上服务端核对到的事实。
#
# 文案在 prompts/（2026-10-14 从本文件搬出）：`no_card_nudge.md` / `no_card_note.md` /
# `no_card_structural_nudge.md`。本文件不再留内联副本——那几个 `*_TEXT` 全局在下面
# 直接读盘初始化，`_refresh_prompt_texts` 负责按提示词库热替换。
#
# 结构判据那条（no_card_structural_nudge）的来由：上面按**措辞**判「假称已出卡」，
# 真机换一种说法就绕过去了。实测「已为你准备好 3 个候选方案……你在计划卡上选一版确认即可」
# 与「已经给你出了 3 个候选方案……」两种变体都判不中——规划轮以一句口头方案收尾、
# 服务端零张卡、用户点不到确认，剪辑流程死在这里。所以再加一条**与措辞无关**的
# 结构判据：规划轮要收尾，就必须有 submit_plan 的成功记录，或明确说清「给不出计划」。
# 给不出的正路：模型明确说明原因时不该被反复打回。命中这些词就认它是在如实回绝。
_DECLINES_PLAN = re.compile(
    r"(给不出|无法(给出|生成|提交)?(计划|方案)|不能(给出|生成|提交)(计划|方案)"
    r"|需要你(先)?(提供|补充|确认|明确|告诉)|请(先)?(提供|上传|补充|明确)"
    r"|素材(还)?没|还没有素材|没有素材|无法完成|做不到)")

# 「卡面形状」的答复：列了多个带标号的可选方案，并让用户挑一个确认。
# 这是真机那种「口头交了计划卡」的形态——模型把卡的内容全说出来、却一次 submit_plan
# 都没调，界面上零张卡，用户无从点确认。措辞千变万化（「候选方案」/「候选计划」/
# 「版本」…），所以判据取**结构**：≥2 个方案标号 + 让用户挑选/确认的措辞。
_PLAN_OPTION_LABEL = re.compile(r"(方案\s*[一二三四1-9]|[pP][1-9]\b|版本\s*[一二三四1-9])")
_PLAN_ASK_TO_PICK = re.compile(r"(选一(个|版)|选哪(个|一)|确认哪|挑一(个|版)|选择一(个|版)"
                               r"|确认即可|点确认|告诉我(你)?选|你(倾向|想要)哪)")


def looks_like_plan_card(text: Any) -> bool:
    """这句答复是不是「把计划卡的内容用文字说了一遍」而没有真出卡。

    与 ``claims_plan_card`` 的分工：那个认「已提交/已生成」这类**完成措辞**，
    容易被换词绕开；这个认**卡面形状**（多个带标号的方案 + 让用户挑一个）。

    为什么不能放宽到「规划轮没出卡就不许收尾」：规划轮的出口本来就允许是
    **直接回答**（用户只是问「这条素材能用吗」「需要我出计划卡吗」）。那种轮次里
    模型一次 submit_plan 都不调是完全正确的，硬打回只会把正常咨询也变成多跑一轮。
    所以只针对「看起来已经把卡说完了」的答复。
    """
    body = "" if text is None else str(text)
    if len(_PLAN_OPTION_LABEL.findall(body)) < 2:
        return False
    return bool(_PLAN_ASK_TO_PICK.search(body))


# 执行轮的同一副面孔：确认帧落地后本轮**一次计划步骤都没调用**（Storyline 没收到任何
# 请求，界面上不会多出新的产物），终答却写着「时间线重排完成，时长 8.64 秒」——那是会话
# 历史里上一轮的旧产物被复述成本轮产出（真机实测：2 秒收尾、iteration 0、零工具调用）。
# 文案在 prompts/step_nudge.md / step_note.md；本文件不留内联副本（见上面那段说明）。
# step_nudge 里的 `{first}` 由调用点 `.replace("{first}", …)` 填。

# 上面这几段提示文本对外暴露一份名字，供启动期一致性检查读取（run_server 的 (f) 段）：
# 它们都会整段进模型上下文，写错工具名同样会让模型去调一个不存在的工具。
#
# ``_refresh_prompt_texts`` 会把它们换成磁盘上的版本（prompts/*.md）——默认走仓库自带
# prompts/ 目录，改文案不必再动代码。**没有内联回落**了：文件缺失时 read_prompts_file
# 直接抛（见下面五个全局的初始化），不再静默退化成旧文本。
def _refresh_prompt_texts(library: Any) -> None:
    """按提示词库刷新本模块的提示文本（就地改模块全局）。

    ``prompt_text``：库里有就用库里的（热替换目录），没有就从仓库自带 prompts/ 读——
    **不再回落内联常量**（那层重复已经拆掉了）。
    """
    if library is None:
        return
    global NO_CARD_NUDGE_TEXT, NO_CARD_NOTE_TEXT, NO_CARD_STRUCTURAL_TEXT
    global STEP_NUDGE_TEXT, STEP_NOTE_TEXT
    NO_CARD_NUDGE_TEXT = prompt_text(library, "no_card_nudge.md")
    NO_CARD_NOTE_TEXT = prompt_text(library, "no_card_note.md")
    NO_CARD_STRUCTURAL_TEXT = prompt_text(library, "no_card_structural_nudge.md")
    STEP_NUDGE_TEXT = prompt_text(library, "step_nudge.md")
    STEP_NOTE_TEXT = prompt_text(library, "step_note.md")


# 五个 `*_TEXT` 的初值直接读盘（文案就在 prompts/，不在本文件）。
# 缺文件即抛——提示词随代码分发，缺了是部署坏了，不该静默变成空提示。
NO_CARD_NUDGE_TEXT = read_prompts_file("no_card_nudge.md")
NO_CARD_NOTE_TEXT = read_prompts_file("no_card_note.md")
NO_CARD_STRUCTURAL_TEXT = read_prompts_file("no_card_structural_nudge.md")
STEP_NUDGE_TEXT = read_prompts_file("step_nudge.md")
STEP_NOTE_TEXT = read_prompts_file("step_note.md")

# HITL 审批断点：执行撞上「需人工审批」的工具时挂起，本轮先结束、不发终答（status 停
# awaiting_approval）；用户批准/拒绝后再从同一一致点续跑。这句是给前端看的过渡说明。
_APPROVAL_PAUSE_ANSWER = "已暂停：以下操作需要你确认后才会执行。"
# 用户决策回喂时的口径：批准就照常跑，拒绝要模型换方案或跳过（别原样重试同一工具）。
_APPROVAL_DECISION_TEXT = {
    "approve": "用户已批准执行，继续跑下去。",
    "reject": "用户拒绝了这一步，请换方案或跳过，不要再原样重试同一工具。",
}
# 渲染前确认门的决策（见 render_gate）。这四个 key 必须与 render_gate 里的常量一致，
# 所以直接 import 过来拼装，不写重复字面量——写重复了会静默失配（用户点了没反应）。
_APPROVAL_DECISION_TEXT.update({
    CONFIRM_OPTION: "用户已确认编排结果，可以开始渲染。",
    ADJUST_OPTION: ("用户选择「我还要改」，尚未确认渲染。**不要渲染**，"
                    "先按用户的要求重做编排，做完把新的编排结果再交用户确认。"),
    KEEP_FULL_OPTION: ("用户要求保内容完整、不要把话截断。请把成片时长放宽到能容纳全部内容，"
                       "重做时间线让画面补足，再把新的编排结果交用户确认。"),
    TRUNCATE_OPTION: ("用户接受按原定时长截断末尾。按原时长重做/确认时间线即可；"
                      "末尾少一句话这件事用户已拍板，不必再问。"),
})


def _planning_run(cp: Any) -> bool:
    """这条 run 开的时候是不是**规划轮**——续跑要接着用同一张注册表、同一套硬保证。

    真机事故：规划轮里主动提问（问 group_clips 该怎么分组）→ 用户答一句 → 从断点
    续跑。轮次身份原先只活在 ``run(planning=…)`` 这个形参里，没人落盘，续跑时它已经
    丢了，于是回退到全量注册表——``submit_plan`` 消失、剪辑节点回来，而 checkpoint 里
    的 system 段还写着「本轮是规划轮」。模型两头都不认，只能回「执行轮但剪辑节点未
    注册」，把锅原样抛回给用户：一张确认过的计划就此续不上。

    所以身份跟着快照走（``Checkpoint.scope["planning"]``，由 ``run`` 落盘）。
    """
    return bool((getattr(cp, "scope", None) or {}).get("planning"))


def _open_budget(state: RunState, *, resuming: bool,
                 messages: Sequence[Any] | None = None) -> None:
    """给这条 run 开花费账（见 `cost.py`）。

    第一次进来填上每条的上限；**续跑**时若已经撞过上限（用户看过「花了多少、停下了」
    那句话、点了「继续」），再往后延一份——与 `_iteration_limit` 续跑时往后延同一个道理。
    没撞过上限的续跑（渲染确认门、弹窗提问之后）不延：那半截还在原来那份预算里。
    上限是 0 = 关掉封顶。
    """
    per = cost_mod.run_budget_yuan()
    if per <= 0:
        state.budget_yuan = 0.0
        return
    if state.budget_yuan <= 0:
        state.budget_yuan = per
        # 新开的账：节点花费只从**这条 run 往后**的回执里读——上下文里带进来的旧对话
        # 回执是别的 run 花的，记过一次了（见 `_charge_nodes`）。
        if messages is not None and not resuming:
            state.node_scan = len(messages)
    elif resuming and state.spent_yuan >= state.budget_yuan:
        state.budget_yuan = state.spent_yuan + per


#: 连续这么多轮「每一次调用都失败、而且都是之前失败过的同一个调用」就停下这条 run。
#:
#: 2026-10-18 真机 run edd4ddb5808d：模型自己编了两个不存在的 demo 名，系统回「无此附件」
#: 之后，它把同样两个调用又发了 150 多次——每次被拒都是完整的一轮、照样计费，而撞上
#: 「连续失败 N 次，不再执行」那道闸也只是又回一句话，没有任何东西让这条 run 停下来。
#: 判据刻意定窄：只认**重复**的失败。照着打回清单反复改分镜（每次报错不同、参数也不同）
#: 是在推进，一轮都不算——那正是「堵死比白烧更坏」要护住的那一种。
#:
_STALL_ROUNDS = 6

#: 交付前允许连续预览几次（软线 / 硬线）。**预览是自查，不是交付**。
#:
#: 2026-10-19 真机 run 32cd75d71fee（菊花一镜）：写手连续 6 轮「改代码 → preview_shot」，
#: 每一版都换了花瓣画法，`_stuck_rounds` 按「参数不同就是推进」判，一次都没响；
#: 直到迭代 27 还在调花瓣，始终没交付。预览回执里的 `next` 它看见了，
#: 但没有把它当成「下一步」。软线到了用 system 消息说一遍；硬线起 `preview_shot`
#: 不再执行（不是拦交付——恰恰是逼它走交付那一步）。
_PREVIEW_SOFT = 3
_PREVIEW_HARD = 6

#: 一执行就算「已经交付/已经进入下一步」的工具：预览计数清零。
#: 规划轮的 `submit_plan` 也算——交完计划再改草稿预览，是新一轮的自查，不该沿用旧账。
#: 简洁线的出片工具不走 preview_shot，所以不需要列进来。
_DELIVERY_TOOLS = frozenset({"submit_plan"})


#: 写文件类工具：比较「是不是重复」时**忽略写到哪个文件**、只看写了什么。
#:
#: 2026-10-18 真机 run 7054bfa3f503：模型说要做 26 镜，s01~s15 认真写完之后输出退化，
#: 把同一段 4011 字符的代码原样写进 s16、s17……一直写到 s60 还没停——每一次都是「成功」、
#: 路径也各不相同，只认重复**失败**的老判据一条都抓不到。同一段内容第二次写进别的文件，
#: 就是在重复，不是在推进。
_WRITE_TOOLS = frozenset({"write_file"})

#: 编辑类工具：比较「是不是重复」时不看整串参数，只看**这次新增了什么**。
#:
#: 2026-10-19 真机 run 02cbcfb2440e：模型每轮把同一段约 380 字的光带 `<path>` 再追加
#: 一份，`old_string` 恰好等于前面 N 份（唯一匹配）——参数逐轮变长，按整串 JSON 比
#: 永远认不出重复，40 多轮把 7KB 的镜头文件叠到 20KB 还没停。
#: 签名取「只增不减时新增的那一段」的哈希：同一段反复叠 → 同一个签名；
#: 正常补码（每次新增的内容不同）→ 签名不同，不会误判。
_EDIT_TOOLS = frozenset({"edit_file"})
#: 成功但重复也算「不推进」的工具。轮询（`render_status`）不在其中：
#: 同参同结果的轮询是等在途任务，不是打转。
_REPEAT_TOOLS = _WRITE_TOOLS | _EDIT_TOOLS

#: 不按「重复」拦的工具：联网检索。
#:
#: 2026-10-22 真机 run `1b0170119f18`：同一个查询连打 6 次，每次都被 `_blocked_repeat`
#: 拒掉；这些拒绝本身以 `Error:` 开头，又被 `_stuck_rounds` 数成「没有进展」，6 轮之后
#: 整条 run 在规划轮被判打转停下——一分钱没换来一份计划。检索本来就是「搜到就算数」：
#: 要不要换词、搜到什么时候够，是模型的判断，服务端不替它封路（§2.2）。多跑一次检索
#: 只多花一次搜索费，按「堵死比白烧更坏」选放行。
_REPEAT_EXEMPT_TOOLS = frozenset({"web_search"})


def _call_sig(name: Any, arguments: Any) -> str:
    if name in _WRITE_TOOLS:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except (ValueError, TypeError):
            args = {}
        body = str(args.get("content") or "")
        return f"{name}:content#{hashlib.md5(body.encode('utf-8')).hexdigest()}"
    if name in _EDIT_TOOLS:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except (ValueError, TypeError):
            args = {}
        path = str(args.get("path") or "")
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if old and new.startswith(old):
            delta = new[len(old):]
            return (f"{name}:{path}:grow#"
                    f"{hashlib.md5(delta.encode('utf-8')).hexdigest()}")
        return (f"{name}:{path}#"
                f"{hashlib.md5((old + chr(0) + new).encode('utf-8')).hexdigest()}")
    return f"{name}:{arguments}"


def _call_rounds(messages: Sequence[Any]) -> list[list[tuple[str, str, str]]]:
    """消息序列 → 每一轮工具调用的 ``[(调用签名, 结果开头, tool_call_id)]``。"""
    rounds: list[list[tuple[str, str, str]]] = []
    results = {str(m.get("tool_call_id")): str(m.get("content") or "")
               for m in messages if isinstance(m, Mapping) and m.get("role") == "tool"}
    for m in messages:
        if not (isinstance(m, Mapping) and m.get("role") == "assistant" and m.get("tool_calls")):
            continue
        row = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc.get("function"), Mapping) else tc
            sig = _call_sig(fn.get("name"), fn.get("arguments"))
            row.append((sig, results.get(str(tc.get("id")), "")[:200], str(tc.get("id"))))
        rounds.append(row)
    return rounds


def _stuck_rounds(messages: Sequence[Any], *, floor: int = 0) -> int:
    """末尾连续几轮是「全部失败、且全是重复的失败调用」（只看第 ``floor`` 条消息之后的轮次）。"""
    rounds = _call_rounds(messages)
    seen_before = _call_rounds(messages[:floor]) if floor else []
    tried = {sig for row in seen_before for sig, _, _ in row}
    flags = []
    for row in rounds[len(seen_before):]:
        # 一轮算「打转」：每一次调用都是之前出现过的同一个调用，并且要么失败了、要么是把
        # 同样的内容又写/叠了一遍（写文件与编辑文件那两类成功也不算推进，见 `_REPEAT_TOOLS`）。
        stuck = bool(row) and all(
            sig in tried and (res.startswith("Error") or sig.split(":", 1)[0] in _REPEAT_TOOLS)
            for sig, res, _ in row)
        flags.append(stuck)
        tried.update(sig for sig, _, _ in row)
    n = 0
    for f in reversed(flags):
        if not f:
            break
        n += 1
    return n


def _last_failed_call(messages: Sequence[Any]) -> str:
    """停下时那句话里点名的「在重复什么」：重复写同样的内容，或某个失败调用。"""
    for row in reversed(_call_rounds(messages)):
        for sig, res, _ in row:
            if sig.split(":", 1)[0] in _WRITE_TOOLS:
                return "把同一段内容反复写进不同的文件"
            if sig.split(":", 1)[0] in _EDIT_TOOLS:
                return "把同一段内容反复叠进同一个文件"
            if res.startswith("Error"):
                return sig[:120]
    return "工具调用"


def _dead_tools(fail_counts: Mapping[str, int]) -> set[str]:
    """连续失败到上限、该从工具面上摘掉的工具名（出口工具永不摘）。"""
    return {str(name) for name, n in fail_counts.items()
            if int(n) >= _MAX_CONSECUTIVE_FAILURES
            and str(name) not in _NEVER_HIDE_TOOLS}


def _tool_ok(messages: Sequence[Any], name: str) -> bool:
    """这条 run 里 ``name`` 最后一次的真实回执是不是成功（工具错误统一以 ``Error`` 开头）。

    看回执而不是只看「调用过」：失败的调用也进 ``calls_executed``，
    拿它当「已交付」会让收尾守卫把一句失败当成该接着做下一步。
    """
    for msg in reversed(list(messages or [])):
        if (isinstance(msg, Mapping) and msg.get("role") == "tool"
                and str(msg.get("name") or "") == name):
            return not str(msg.get("content") or "").lstrip().startswith("Error")
    return False


#: 出片一族：回执里带成片对象键（``"video": "renders/…"``）或 ``status=done``
#: 即这一版已经交付到用户手上。
_RENDER_TOOLS = frozenset({"render_video", "render_status"})


def _render_finished(messages: Sequence[Any]) -> bool:
    """这条 run 里是否已经有成片交付（渲染节点的回执里带 ``video`` / ``status=done``）。

    为什么要问这件事：真机 run ``490e71e20a19`` 出片完成后，模型在答复末尾写了
    「修 s05 / 加衬底，要哪一项说一声」——这是**已交付后的可选后续**，不是开工前的
    提问。弹窗守卫却把它判成「该弹窗却写成了正文」，于是打回并告诉模型「这些你能
    自己定，直接接着做」。模型照做：连试五次同一个被拒的工具，最后整份重做了一遍——
    用户没要求，钱和时间都白花。

    交付之后的「要不要再改」属于用户的选择（改哪几镜、值不值再等十分钟），
    不该由这条守卫推着模型自己替用户决定。所以：有已完成的成片，就**不**因为
    「像提问」而打回，也不附「这句本该是个弹窗」。
    """
    for msg in reversed(list(messages or [])):
        if not (isinstance(msg, Mapping) and msg.get("role") == "tool"
                and str(msg.get("name") or "") in _RENDER_TOOLS):
            continue
        body = str(msg.get("content") or "")
        if '"video":' in body or '"video": ' in body or '"status": "done"' in body:
            return True
    return False


def _visible_tool_definitions(reg: Any, *, user_id: str | None,
                              fail_counts: Mapping[str, int],
                              only: set[str] | None = None,
                              ) -> list[dict[str, Any]]:
    """本轮发给模型的工具面：连续失败到上限的工具不再出现在 schema 里。

    拒绝执行是「说了不跑」，模型仍可以原样再打（真机 ``1488197dd8a0`` 连打 5 次
    ``web_search``）。摘掉 schema 是结构性的：它没有这一项可选，只能换工具或交卡。
    出口工具（``submit_plan`` / ``ask_user``）永远保留，见 ``_NEVER_HIDE_TOOLS``。

    ``only`` 是「让位轮」用的白名单（见 ``_MAX_STALL_RESCUES``）：那一轮只剩出口，
    模型没有别的可打，必须把已有的东西交出来。
    """
    defs = reg.get_definitions(user_id=user_id)
    dead = _dead_tools(fail_counts)
    if not dead and only is None:
        return defs
    kept: list[dict[str, Any]] = []
    for d in defs:
        fn = d.get("function") if isinstance(d, Mapping) else None
        name = str((fn or {}).get("name") or "")
        if name in dead or (only is not None and name not in only):
            continue
        kept.append(d)
    return kept


def _stop_reason(state: RunState, messages: Sequence[Any]) -> str:
    """这一轮叫模型之前该不该停：``budget`` 花费到线 / ``stalled`` 原地打转 / 空串＝接着跑。"""
    if _over_budget(state):
        return "budget"
    if _stuck_rounds(messages, floor=state.stall_floor) >= _STALL_ROUNDS:
        # 记下停在哪：用户点「继续」之后只看这之后的新轮次——否则末尾那几轮失败还在，
        # 续跑第一轮就又被判成打转、立刻停下。
        state.stall_floor = len(messages)
        return "stalled"
    return ""


def _over_budget(state: RunState) -> bool:
    """这条 run 的花费到线了没有（`_drive` 每轮叫模型之前问一次）。

    迭代上限按**轮数**数，而一轮可以是 2 万 token 也可以是 30 万——真机 run 32affa23c933
    第 185 轮单轮就是 30.7 万输入。只有按**钱**数，「再出问题最多亏掉多少」才是一个确定的数。
    """
    return state.budget_yuan > 0 and state.spent_yuan >= state.budget_yuan


def _charge(state: RunState, usage: Mapping[str, Any] | None) -> None:
    """一次模型调用记进这条 run 的账。供应商没回缓存数时按全价算（`cost.cost_yuan`）。

    **只记主循环**：节点里自己调模型的那几处（预览自查、带图判官）跑在另一个容器里，
    钱不经过这里——它们今天都是按次、量小、默认关着的；哪天要开大量的节点内调用，
    得让节点把 usage 随回执带回来再记到这里。
    """
    if not usage:
        return
    state.spent_yuan += cost_mod.cost_yuan(usage, model=str(usage.get("model") or ""))


# 两种写法都认：节点 JSON 回执里的 `"node_cost_yuan": 0.12`，与联网工具文本回执末尾的
# `[计费 node_cost_yuan=0.005]`（DeepSeek 官方 Search / Fetch，见 `tools/web.py`）。
_NODE_COST_RE = re.compile(r'"?node_cost_yuan"?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)')


def _charge_nodes(state: RunState, messages: Sequence[Any]) -> None:
    """把节点回执里报的花费（``"node_cost_yuan": 0.12``）记进这条 run 的账。

    节点跑在另一个容器里，它自己调模型（出片前的带图风格对照）花的钱不经过主循环的
    `_charge`；它把数字写进回执，这里从**新进来的**回执里读出来。``node_scan`` 记着读到
    第几条，跨挂起不重复记。
    """
    start = min(state.node_scan, len(messages))
    for m in messages[start:]:
        if isinstance(m, Mapping) and m.get("role") == "tool":
            got = _NODE_COST_RE.search(str(m.get("content") or ""))
            if got:
                state.spent_yuan += float(got.group(1))
    state.node_scan = len(messages)


def _iteration_limit(max_iterations: int, start_iteration: int, resuming: bool) -> int:
    """本轮迭代上限：**续跑时是「再给一轮」，不是「接着数」**。

    真机事故（用户原话「[已达最大迭代次数 40，提前结束]…还是不行啊」）：
    撞上限那条 run 的 ``iteration`` 就等于 ``max_iterations``，用户点「继续」时
    从 ``cp.iteration`` 续跑，而循环条件写的是 ``while iteration < max_iterations``
    → **一进循环就为假**，一个字都没干又打印一次同样的话，用户永远无法前进。

    「继续」是用户的明确指令，意思是"再做点事"——预算应当按续跑点**往后延**，
    而不是继承一个已经用光的计数。

    做成模块级函数而不是写在 ``_drive`` 里：``_drive`` 有"只剩骨架"的结构守卫
    （见 tests/test_plan_layout.py），逻辑该切出去就切出去，不该靠放宽阈值绕过。
    """
    if resuming and start_iteration >= max_iterations:
        return start_iteration + max(1, max_iterations)
    return max_iterations


#: 过程摘要里归到「已写」的工具：会改磁盘的那些。只用于撞上限那句话的措辞，
#: 不参与任何权限判定——判的是「这一轮到底有没有留下产物」，不是工具是否只读。
#: **名字不能叫 `_WRITE_TOOLS`**：上面那个是打转判据的集合（只放 ``write_file``，
#: 因为 ``edit_file`` 的签名要按「新增了哪一段」算）。2026-10-27 我在这里用了同名，
#: 模块级覆盖后 ``edit_file`` 被当成 content 型写文件——签名全撞成
#: ``edit_file:content#<空串哈希>``，「每次追加不同内容」被误判成打转。
_SUMMARY_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def _limit_summary(calls: list[str]) -> str:
    """把这一轮调过的工具压成一句过程摘要，接在「已达最大迭代次数」后面。

    为什么不是只回一句「已达最大迭代次数」：真机 P5（2026-10-17）里子 Agent 撞限
    返回的那句话把**过程全丢了**——主 Agent 拿着它既不知道读没读到东西，也不知道
    有没有留下产物，只能当失败重派一次。摘要给的是这两件事实：
    ``过程摘要：已调用：read_file ×4、grep ×1；已写：无``。

    顺序按**第一次出现**（`dict` 的插入序），不按次数排：读者想看的是"它先干了什么"。
    """
    seen: dict[str, int] = {}
    for name in calls:
        key = str(name or "")
        if key:
            seen[key] = seen.get(key, 0) + 1

    def _fmt(names: list[str]) -> str:
        return "、".join(f"{n} ×{seen[n]}" for n in names) or "无"

    return ("过程摘要：已调用：" + _fmt([n for n in seen if n not in _SUMMARY_WRITE_TOOLS])
            + "；已写：" + _fmt([n for n in seen if n in _SUMMARY_WRITE_TOOLS]))


def _answers_text(answers: Sequence[Mapping[str, Any]]) -> str:
    """多题弹窗的结构化答案 → 给模型看的一句话（每题一行）。

    为什么优先用它而不是前端拼的 ``message``：结构化答案里带着**选项 key**，
    服务端与模型都能确定「第 2 题选的是 keep_original_audio 那支」；
    而前端拼出来的中文散文是不可反查的，换个措辞就丢了对应关系。
    单题时返回空串，让调用方回退到 note（保持原有行为）。
    """
    rows = [a for a in (answers or ()) if isinstance(a, Mapping)]
    if len(rows) < 2:
        return ""
    lines = []
    for i, a in enumerate(rows, start=1):
        ans = str(a.get("answer") or a.get("custom") or "").strip()
        if not ans:
            continue
        title = str(a.get("title") or "").strip()
        lines.append(f"{i}. {title} → {ans}" if title else f"{i}. {ans}")
    if len(lines) < 2:
        return ""
    return "用户在弹窗里逐题做的选择：\n" + "\n".join(lines)


def _plan_style_skill(plan: Mapping[str, Any],
                      row: Mapping[str, Any] | None) -> str:
    """这张计划卡执行时用哪个风格包：候选优先，老卡退回规划轮 scope。

    两处都没有 = 空串 = 不注入（缺省视觉）。为什么要有「退回」这一步：加上候选字段
    之前落库的卡没有这一位，而 checkpoint scope 从一开始就记了本轮选的包——不退回，
    老卡重放会静默换成默认视觉。
    """
    scope = (row or {}).get("scope")
    return str(plan.get("style_skill")
               or (scope if isinstance(scope, Mapping) else {}).get("style_skill")
               or "")


def _style_content_bits(items: Any) -> str:
    """零件内容清单 → 一句话；`values` 是能传的字面量，`reference` 只是参考片叫法。

    与 `shot_writer` 里那段同口径（两处都直接写进模型提示词，没有共享模块可以放：
    `storyline_server/motion/*` 刻意不依赖 `agent_framework`）。
    """
    bits: list[str] = []
    for c in (items or []):
        if not isinstance(c, Mapping):
            continue
        prop = str(c.get("prop") or "")
        values = [str(x) for x in (c.get("values") or []) if str(x).strip()]
        ref = [str(x) for x in (c.get("reference") or []) if str(x).strip()]
        if values:
            bit = f"`{prop}` 可传 {' / '.join(values)}"
            if ref:
                bit += f"（参考片里对应：{' / '.join(ref)}）"
            bits.append(bit)
        elif ref:
            bits.append(f"`{prop}` 参考片用过 {' / '.join(ref)}"
                        "（按这一镜的需要传合法值）")
    return "；".join(bits)


def _annotate_requires(compiled: dict[str, Any], gate: Any) -> None:
    """把每个计划步骤的直接前置补进编译结果（原地改 ``compiled``）。

    依赖只有契约知道，编译层拿不到，所以在 execute_plan 这一层补一次。
    契约不可用时留空——下游据此退化成"不自动并批"，不会出错，只是慢。
    """
    try:
        contract = getattr(gate, "contract", None)
        rmap = contract.required_map() if contract is not None else {}
    except Exception:  # noqa: BLE001 - 取不到就不标注，退化成串行
        rmap = {}
    for step in compiled.get("steps") or []:
        if isinstance(step, dict):
            step.setdefault("requires", list(rmap.get(str(step.get("node") or ""), ())))


def _with_ready_siblings(
    calls: list[Any], completed: set[str], required_map: Mapping[str, Sequence[str]],
    nodes: Sequence[str], *, resolve: Any, make_call: Any,
) -> list[Any]:
    """把「同层就绪、且与本次调用互不依赖」的兄弟节点补进同一批。

    为什么要有这个：``plan_batches`` 只能对**同一条回复里的调用**并批，
    于是能不能并行取决于模型是否恰好把两个独立节点写在一起——真机实测它就
    分开写了（先 asr、拿到结果才 split_shots），本可并行的链路退化成一前一后。
    并行与否是**图的性质**，不该由模型的输出习惯决定，所以在这里按图补全。

    严格条件（宁可不补，也不补错）：
      · 候选必须在**这次的执行计划里**（``nodes``）——不碰计划外的节点；
      · 候选的前置必须**全部已完成**（``completed``）——否则会跑出错误结果；
      · 候选与本次调用、以及候选彼此之间**依赖互不包含**——有先后就不能同批；
      · 候选尚未执行过。
    任何一条不满足就跳过它，最终仍由 ``plan_batches`` 按读写集切成安全的批。
    """
    if not calls or not nodes:
        return calls
    # 同层判定：前置集合完全相同（都只依赖已完成的那批）
    def reqs(n: str) -> frozenset[str]:
        return frozenset(str(x) for x in required_map.get(n, ()))

    called_names = {str(getattr(c, "name", "")) for c in calls}
    have = {n for n in applied_names(called_names) if n in nodes}

    def depends(a: str, b: str) -> bool:
        """a 是否（直接或间接）依赖 b。"""
        seen: set[str] = set()
        stack = list(reqs(a))
        while stack:
            x = stack.pop()
            if x == b:
                return True
            if x in seen:
                continue
            seen.add(x)
            stack.extend(reqs(x))
        return False

    added: list[Any] = []
    for cand in nodes:
        if cand in have or cand in completed:
            continue
        cand_reqs = reqs(cand)
        if not cand_reqs or not cand_reqs <= completed:
            continue                      # 前置没做完：绝不能提前跑
        # 与本次已调用的每一个、以及已加入的候选，都不能有依赖关系
        peers = [n for n in have] + [str(getattr(c, "name", "")) for c in added]
        if any(depends(cand, p) or depends(p, cand) for p in peers if p):
            continue
        # 同层：前置集合与本次调用的某个节点一致（同一批上游的兄弟）
        if not any(reqs(cand) == reqs(p) for p in peers if p):
            continue
        tool = resolve(cand) if resolve else None
        if tool is None or not getattr(tool, "concurrency_safe", False):
            continue
        call = make_call(cand) if make_call else None
        if call is None:
            continue
        added.append(call)
    return [*calls, *added]


def applied_names(names: Iterable[str]) -> set[str]:
    """调用名 → 契约里的节点名（MCP 工具名可能带 ``storyline_`` 前缀）。"""
    out: set[str] = set()
    for n in names:
        s = str(n or "")
        if not s:
            continue
        out.add(s)
        if "_" in s:
            out.add(s.split("_", 1)[1])
    return out


def _detect_fallback_options(messages: list, tool_calls: list) -> dict | None:
    """检查最近一批工具结果里有没有 __fallback_options__ 标记。"""
    import json
    tc_ids = {tc.id for tc in tool_calls}
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if msg.get("tool_call_id") not in tc_ids:
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or "__fallback_options__" not in content:
            continue
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("__fallback_options__"):
                return data
        except Exception:  # noqa: BLE001
            pass
    return None


def _no_step_called(plan: Any, calls: Sequence[str]) -> bool:
    """本轮是否一次计划步骤都没调用（与对账钩子同一套判据：计划 − 实际 = 全部未履行）。"""
    steps = (plan or {}).get("steps") or []
    if not steps:
        return False
    return len(reconcile(steps, calls)["unfulfilled"]) == len(steps)


def declines_plan(text: Any) -> bool:
    """这句答复是否在**如实说明给不出计划**（而不是换个说法假称已出卡）。

    结构性守卫（``_NO_CARD_STRUCTURAL_NUDGE``）要把「没出卡」的轮次退回去，
    但必须放过真正的回绝：素材没到位、诉求还不明确时，模型说清楚原因是对的结局，
    反复打回只会烧完迭代预算、还让用户看不到那句该看到的追问。
    """
    return bool(_DECLINES_PLAN.search("" if text is None else str(text)))


@dataclass
class AgentConfig:
    # R4：与运行期对齐。原来是 10，而 `run_server` 一直传 200——
    # 「配置默认值」和「真跑的值」差 20 倍，任何直接用 AgentConfig() 的路径
    # （子 Agent、离线脚本）都会在第十轮被截断，且看不出是被默认值截的。
    max_iterations: int = 200
    # 「渲染未达终态不许收尾」的预算：模型收到 queued/running 后不续查时，本循环自己
    # 按 render_poll_sec 轮询 render_status，最多再等 render_follow_max_sec 秒。
    render_follow_max_sec: float = 1800.0
    render_poll_sec: float = 3.0
    # 规划轮声称已出卡、实际没有 submit_plan 成功记录时，循环把这句核对退回去的次数。
    plan_claim_nudges: int = 1
    # 执行轮声称某步已跑完、实际本轮一次计划步骤都没调用时，同样的退回次数。
    step_claim_nudges: int = 1
    # 渲染前确认门：编排就绪、即将渲染时先把编排结果给用户看，确认后才渲。
    # 用户明确要求「不确认绝不渲染」，所以默认为 True；置 False 即回到直接渲染的老行为。
    gate_render: bool = True
    # 「向用户提问必须走弹窗」的硬保证：模型准备收尾时若检测到它在向用户提问、
    # 却没调 ask_user，就打回一次要求它改成弹窗（见 ask_gate）。
    # 置 False 即关闭这道保证（回到「模型自觉决定要不要弹窗」的老行为）。
    require_popup_questions: bool = True
    popup_question_nudges: int = 1
    # 「计划必须出卡」的硬保证：模型把整套方案写成正文、末尾要用户"回复确认"
    # 时打回一次，要求改用 submit_plan 出卡（见 ask_gate.looks_like_plan_invite）。
    # 置 False 即关闭这道保证。
    plan_card_nudges: int = 1
    # 工具结果的体积闸门（见 compress.ToolResultGate）。这三个数是这次补的：
    # 在此之前工具结果**一条都没有闸门**，整条 run 的历史结果每轮原样重发，
    # 真机首发提示词因此到过 258K token。0 = 关闭对应那一档（截断 / 折叠）。
    # 默认值指回 compress 的常量，不在这份配置里另抄一份数：真机 run 97c1289eba35
    # 把 compress 的 24_000 改成 60_000 之后改了三轮没生效，根因就是这里把旧数
    # 写死了——它传给 ToolResultGate 时把模块默认整个盖掉，改常量的那几轮全是空转。
    tool_result_max_bytes: int = DEFAULT_RESULT_MAX_BYTES
    tool_results_fold_at_bytes: int = DEFAULT_FOLD_AT_BYTES
    tool_results_keep_recent: int = DEFAULT_KEEP_RECENT_RESULTS


@dataclass
class _Round:
    """一次 ``_drive`` 的循环账：原先散在三百行里的局部量收敛成一份有名字的状态。

    ``state`` 是**跨挂起仍成立**的那份可恢复状态（``RunState``，随指针行落盘）；
    这里其余字段都是本轮调用私有的（打回预算、终答、当轮注册表），落盘反而失真。
    """

    ctx: AgentHookContext
    state: RunState
    reg: ToolRegistry
    messages: list
    iteration: int
    cp: Checkpoint | None
    run_id: str | None = None
    planning: bool = False
    interactive: bool = True
    approval_decision: str | None = None
    approval_note: str = ""
    card_nudges: int = 0
    step_nudges: int = 0
    popup_nudges: int = 0
    # 本轮**是否已经用过 ask_user**：必须按「整轮」而不是「当前这一批」算。
    # 反例（真机实测）：模型调 ask_user 提问 → 循环挂起并把「已暂停等你选择」写成终答 →
    # 那句解释里带着「本轮不可用，你希望怎么处理」的措辞，会被判据认成「在提问却没调
    # ask_user」，于是又逼它问一遍，用户看到两张一模一样的卡。
    # 从提问断点续跑时靠 ``_settle_pending`` 的返回值补回来：本轮确实是 ask_user 弹的卡，
    # 不能因为「这次 _drive 还没调过」就判它没用弹窗（真机实测：续跑的每一轮都吃一条
    # 「但你没有调用 ask_user」的假打回）。
    asked_this_run: bool = False
    plan_submitted: bool = False
    # 「计划必须出卡」的打回预算。是否**已经**交过计划不看这里，看
    # ``state.plan_candidates``——那才是「submit_plan 真成功过」的权威记录，
    # 另加一个字段只会多一处可能与它不同步的状态。
    plan_nudges: int = 0
    # 本轮的**有效**迭代上限：续跑时会往后延（见 _drive 里的说明），
    # 收尾那句提示要按它写，否则会说一个与实际不符的数字。
    iter_limit: int = 0
    final: str | None = None
    reached_limit: bool = True
    # 这一次是因为什么提前停下的（不是迭代数）：``budget`` 花费到线 / ``stalled`` 原地打转。
    # 收尾那句要按它写（见 `_stop_reason`）。
    stop_reason: str = ""
    # 打转停跑前已经用掉几轮「摘掉死工具、强制交付」的机会（见 `_MAX_STALL_RESCUES`）。
    stall_rescues: int = 0
    #: 下一轮是让位轮：工具面只留出口（`_NEVER_HIDE_TOOLS`），叫完即清。
    rescue_pending: bool = False


class AgentOnceRun:
    """处理单个用户请求的内层 ReAct 循环。"""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        context_builder: ContextBuilder | None = None,
        hooks: AgentHook | None = None,
        config: AgentConfig | None = None,
        checkpoint: CheckpointManager | None = None,
        storage: Any | None = None,
        plan_gate: Any | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.context_builder = context_builder or ContextBuilder()
        self.hooks = hooks or CompositeHook([])
        self.config = config or AgentConfig()
        self.checkpoint = checkpoint
        # storage：附件鉴权与 messages 落库的入口；未注入时附件一律视为不可用（D1：无本地兜底）。
        self.storage = storage
        # plan_gate：执行循环跑在这个实例上，门却挂在 Agent 上——想让 Runner 读词表
        # （确认卡回显开关取值的人话标签）就必须显式递进来，靠 ``getattr(self, "plan_gate")``
        # 是永远读不到的空读：静默回 {}，卡面就悄悄退回机器名。没接门时照旧为 None。
        self.plan_gate = plan_gate
        # 已经过用户「确认渲染」的 **(这条 run, 哪个出片通道)**：确认过的通道在同一条
        # run 里不再重问、也不再重复提交（由 _suppress_repeat_render 回填「已经在渲了」）。
        # 按通道记账而不是只记 run：用户批准剪素材那条路，不等于批准零素材那条路——
        # 那是另一笔不可逆的算力。
        # 放内存即可：它只影响「同一进程、同一条 run 内要不要再弹一次」，
        # 跨重启后重弹一次无害（用户顶多再看一眼同一份编排），不值得为它加一列。
        #
        # 第三位是**这一次提交的参数指纹**，不是多余的精确度，是这条守卫能不能自救的关键。
        # 只按 (run, 通道) 记账时的真机事故（2026-10-16，run b7df827ebed4「薄肌理论」）：
        # 第一次 render_web 出了片——**但是个 20 秒全黑的废片**（html 参数填成了文件路径）。
        # 模型看出来了，改好 HTML 重交 → 这里回「已经确认并提交，无需再次提交」→
        # 再改名重交 → 还是同一句。`_last_render_failed` 那道失败放行口救不了它：
        # 上一次**返回的是成功**，只不过产物是垃圾。于是模型改好了代码却一个字节都送不进去，
        # 整条 run 只能停在那里向用户解释。
        #
        # 判据改成「同一通道 + 逐字相同的参数」才是重复提交。参数变了就是**另一个东西**，
        # 该让它跑——用户已经批准过「这条路出片」，他批准的不是「只准出一次」。
        self._render_confirmed: set[tuple[str, str, str]] = set()
        # 工具结果闸门：一次 run 内部每轮都把全部历史结果重发，那段此前完全没有闸门
        # （压缩器只在 ``ContextBuilder.build()`` 里跑，那时工具结果一条都还没有）。
        # 它只改消息**内容**、不删消息，所以 tool_calls 的配对不受影响。
        self.tool_gate = ToolResultGate(
            result_max_bytes=self.config.tool_result_max_bytes,
            fold_at_bytes=self.config.tool_results_fold_at_bytes,
            keep_recent=self.config.tool_results_keep_recent,
        )
        # 渲染确认门「问过没有」记在 ``RunState.render_gate_asked``（随指针行落盘），
        # 不再放内存：内存版本会被进程重启与续跑换代打穿，
        # 真机实测因此又把同一道题问了两遍。见 _tool_round 里的完整说明。

    async def run(
        self, session: Session, message: str, *, run_id: str | None = None,
        stream: bool = False, attachments: Sequence[str] = (),
        registry: ToolRegistry | None = None,
        extra_sections: Sequence[str] = (),
        approved_plan: dict[str, Any] | None = None,
        plan_run_id: str | None = None,
        planning: bool = False,
        interactive: bool = True,
        style_skill: str = "",
        user_request: str = "",
        go_straight: bool = False,
    ) -> str:
        """正常发起一次执行；配置了 checkpoint 时先落一个初始快照。

        计划门把这一入口分出三种轮次，差别只在这几个参数上，循环本身不感知：
        ``registry`` 换成规划轮的过滤注册表（剪辑节点物理不在里面）、
        ``extra_sections`` 带进 ``<planning_round>`` 或 ``<approved_plan>`` 等本轮专属段、
        ``approved_plan`` 让对账钩子有本、``plan_run_id`` 记执行轮从哪次规划来、
        ``planning`` 让循环认领规划轮的那条硬保证（没出卡就不许声称已出卡）。

        ``interactive=False``（到点自动跑的定时任务）：所有「拦下来问用户」的门
        一概跳过，否则没人能来点确认，run 会永久挂起。
        """
        # 身份要覆盖整条 await 链，**建上下文也算在内**：素材按 owner 过滤靠它，而历史超过
        # 压缩阈值时 context_builder 里的摘要同样是一次 LLM 调用——密钥按当前身份从
        # app_secrets 热读，绑定晚一步就取到空值，整轮死在「未配置模型密钥」上
        # （真机 run=95ebb22a2be6：规划轮历史短没触发压缩，确认后的执行轮一上来就压）。
        with use_identity_or_inherit(session.user_id, session.conversation_id,
                                     style_skill=style_skill,
                                     user_request=user_request or message,
                                     go_straight=go_straight):
            ids = [str(m) for m in attachments if str(m).strip()]
            rows, rejected = await self._resolve_attachments(session, ids)
            recall_terms = self._recall_terms(approved_plan)
            messages = await self.context_builder.build(
                session, message, attachments=rows, rejected_attachments=rejected,
                extra_sections=extra_sections,
                recall_terms=recall_terms,
            )
            # 召回词留给每轮记忆刷新（MemoryIterationHook）：装配期注入一次之后，
            # 中间几十轮要用**同一个** query 重渲染，否则计划步骤名那一半会丢。
            recall_query = f"{message}\n{recall_terms}".strip() if recall_terms else message
            # 账面**就地取走**，不留到后面从 builder 上读：ContextBuilder 是跨 run 共享的，
            # `last_ledger` 是最后写者赢——两条 run 交错时读到的是别人的账。
            context_ledger = self.context_builder.last_ledger
            # 附件随 user 行入库（spec §5 步骤 7）：先落库再进循环，崩溃恢复时这轮信息不丢。
            await self._persist_user_message(session, message, ids)
            cp: Checkpoint | None = None
            if self.checkpoint is not None:
                cp = await self.checkpoint.begin(
                    session.session_id, message, messages, run_id,
                    scope={"storyline_session": storyline_session_id(
                        session.user_id, session.conversation_id), "artifact_id": "",
                    # 轮次身份必须**落盘**：续跑（提问作答 / 审批 / 崩溃恢复）时没人再传
                    # ``planning`` 这个形参，只能从快照里读（见 ``_planning_run``）。
                    "planning": bool(planning),
                    # 风格包同一道理：计划卡与审批断点续跑时，浏览器不会再带一遍
                    # 用户当初选的那个包，只能从 scope 里读回。
                    "style_skill": str(style_skill or ""),
                    # 用户原话也落盘。执行轮（确认计划卡之后）的 message 是系统拼的
                    # 「按已确认的计划执行：…」，不是用户说的话；续跑时若从 cp.message
                    # 取，逐镜写手收到的「用户原话」就变成了这句系统文案——正好丢掉
                    # 这条片子唯一的事实来源。
                    "user_request": str(user_request or message or ""),
                    # 直接出片同一道理：确认门挂起过一次之后再续跑，浏览器不会再带这个
                    # 开关，只能从 scope 读回来——否则那半截会重新弹确认卡。
                    "go_straight": bool(go_straight)},
                    plan_run_id=plan_run_id,
                )
            return await self._drive(
                session, message, messages, 0, cp, stream, resuming=False, run_id=run_id,
                registry=registry, approved_plan=approved_plan, planning=planning,
                interactive=interactive, context_ledger=context_ledger,
                go_straight=go_straight, recall_query=recall_query,
            )

    async def _resolve_attachments(
        self, session: Session, ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """附件 id → (可见素材行, 无法使用的 id)；越权 id 不报错，交给上下文如实说明。"""
        if not ids:
            return [], []
        if self.storage is None:
            return [], ids
        return await self.storage.materials.resolve(
            ids, user_id=session.user_id, conv_id=session.conversation_id or None
        )

    async def _persist_timeline(self, ctx: AgentHookContext, timeline: Any) -> None:
        """LLM 修正后的时间线存回 plan_timeline artifact，让后续轮次能读到修正版本。

        执行轮里 LLM 经常人工修正时间线（加采访画面穿插、补字幕等），但修正只存在
        checkpoint 的 LLM 对话记录里，没存回 artifact 表。新轮的 Agent 用
        read_node_history 读 plan_timeline 时读到的是自动生成的原始版本，于是误判
        「没有采访画面」。这里在工具调用成功后把入参里的 timeline 存回 artifact。
        """
        if self.storage is None or not timeline:
            return
        try:
            sid = storyline_session_id(
                ctx.session.user_id, ctx.session.conversation_id)
            repo = self.storage.artifacts(sid)
            await repo.put("plan_timeline", {"timeline": timeline})
        except Exception:
            pass

    async def _persist_user_message(
        self, session: Session, message: str, ids: list[str]
    ) -> None:
        if self.storage is None or not session.user_id or not session.conversation_id:
            return
        await self.storage.conversations.ensure(session.user_id, session.conversation_id)
        await self.storage.messages.append(
            session.user_id, session.conversation_id, "user", content=message,
            attachments=ids)

    async def resume(self, cp: Checkpoint, session: Session, *, stream: bool = False,
                     registry: ToolRegistry | None = None,
                     style_skill: str = "", go_straight: bool = False) -> str:
        """续跑一个快照：只重新发起 LLM 调用，不重放已提交工具。

        快照从哪来不在这里管——崩溃恢复取最新一致点，分叉重跑取历史某一界且已带着
        新的产物作用域，两者到这儿都是「一个 Checkpoint」。

        待批的 tool_calls 要作为 keep 传进 ``_repair_orphans``：resume 也可能落到一个
        「等确认」的快照上，那时它们同样没有 tool 结果（详见 ``approve`` 的说明）。

        ``registry`` 由调用方按快照里的轮次身份给（见 ``_planning_run``）：续跑必须
        接着用**同一张**工具表，否则规划轮会在半路上换成执行轮的表。
        """
        pending_ids = [str(c.get("id") or "")
                       for c in ((cp.approval or {}).get("pending_calls") or [])]
        # 显式传入的包名优先（用户在「继续」时可能刚换过），否则用快照里当初那个。
        style = str(style_skill or (cp.scope or {}).get("style_skill") or "")
        # 用户原话同理，且**优先于 cp.message**：执行轮的 cp.message 是系统拼的
        # 「按已确认的计划执行：…」。scope 里没有这一键（旧快照）才退回 cp.message。
        request = str((cp.scope or {}).get("user_request") or cp.message or "")
        # 续跑时入口上刚勾的开关优先：这条 run 当初被确认门拦下，用户这次说「直接出片」，
        # 续跑就不该再问一次。快照里没有这一键（旧快照）就按当时的来。
        straight = bool(go_straight or (cp.scope or {}).get("go_straight"))
        with use_identity_or_inherit(session.user_id, session.conversation_id,
                                     cp.artifact_id, style,
                                     user_request=request, go_straight=straight):
            return await self._drive(
                session, cp.message,
                _repair_orphans(list(cp.messages), keep_ids=pending_ids),
                cp.iteration, cp, stream,
                resuming=True, run_id=cp.run_id, registry=registry,
                planning=_planning_run(cp), go_straight=straight,
            )

    async def approve(
        self, cp: Checkpoint, session: Session, *, decision: str, stream: bool = False,
        note: str = "", answers: Sequence[Mapping[str, Any]] = (),
        registry: ToolRegistry | None = None,
    ) -> str:
        """从审批断点续跑：按用户决策把挂起的那批工具收口，再继续正常循环。

        ``decision`` 是 "approve"/"reject"、兜底方案的 key、或弹窗选项的 key。
        ``note`` 是用户在弹窗里选/写的原话（多题汇总或自定义文本），一并带进上下文。
        ``answers`` 是多题弹窗的**结构化**答案（``[{page,title,answer,key,custom}]``）：
        有了它就不必依赖前端拼的那句中文散文——服务端自己知道每题选了什么。

        注意这里**不能**用 ``_repair_orphans`` 裸修：挂起点上待批的 tool_calls 本来
        就没有 tool 结果，会被当成孤儿剥掉，续跑时 ``_settle_pending`` 再补一条 tool
        回执就成了孤儿 tool 消息，LLM API 直接 400。所以把待批 id 作为 keep 传进去。

        ``registry`` 与 ``_planning_run`` 的理由同 ``resume``：**规划轮里主动提问、
        用户答一句**走的正是这条入口，换了工具表就等于把这一轮的性质换掉了。
        """
        pending_ids = [str(c.get("id") or "")
                       for c in ((cp.approval or {}).get("pending_calls") or [])]
        # 审批断点一律沿用快照里的包名：用户在这张卡上确认的是当初那一版视觉。
        style = str((cp.scope or {}).get("style_skill") or "")
        request = str((cp.scope or {}).get("user_request") or cp.message or "")
        straight = bool((cp.scope or {}).get("go_straight"))
        with use_identity_or_inherit(session.user_id, session.conversation_id,
                                     cp.artifact_id, style,
                                     user_request=request, go_straight=straight):
            return await self._drive(
                session, cp.message,
                _repair_orphans(list(cp.messages), keep_ids=pending_ids),
                cp.iteration, cp, stream,
                resuming=True, run_id=cp.run_id, registry=registry,
                planning=_planning_run(cp),
                approval_decision=decision,
                approval_note=_answers_text(answers) or note,
                go_straight=straight,
            )

    async def _drive(
        self,
        session: Session,
        message: str,
        messages: list,
        start_iteration: int,
        cp: Checkpoint | None,
        stream: bool,
        resuming: bool,
        run_id: str | None = None,
        registry: ToolRegistry | None = None,
        approved_plan: dict[str, Any] | None = None,
        planning: bool = False,
        approval_decision: str | None = None,
        approval_note: str = "",
        interactive: bool = True,
        context_ledger: dict[str, Any] | None = None,
        go_straight: bool = False,
        recall_query: str = "",
    ) -> str:
        """一次执行的骨架：开账 → 循环 → 交付。

        循环本身只做四件事：在一致点落盘、叫 LLM、把切出去的四块串起来——
        ``_tool_round``（审批/渲染/提问三类挂起与分叉交接）、``_nudge_back``
        （收尾前的四条硬保证）、``_deliver``（终答的兜底核对与落库）、
        ``_answer_round``（叫一次模型并按回答分流）。
        ``context_ledger`` 只有刚装配过上下文的那条入口传得出来（见 ``_open_run``）。
        """
        r = self._open_run(
            session, messages, start_iteration, cp, run_id=run_id, registry=registry,
            approved_plan=approved_plan, planning=planning,
            approval_decision=approval_decision, approval_note=approval_note,
            interactive=interactive, context_ledger=context_ledger,
            go_straight=go_straight, recall_query=recall_query)
        # 暴露当前 ctx 与 hooks 给工具（如 SpawnTool）触发子 Agent 生命周期节点。
        hooks_token = _current_hooks.set(self.hooks)
        ctx_token = _current_hook_ctx.set(r.ctx)
        limit = _iteration_limit(self.config.max_iterations, start_iteration, resuming)
        r.iter_limit = limit
        _open_budget(r.state, resuming=resuming, messages=r.messages)
        try:
            try:
                while r.iteration < limit:
                    if stop := _stop_reason(r.state, r.messages):   # 花费封顶 / 原地打转
                        note = await self._stall_gate(r, stop)
                        if note is None:
                            break
                        r.messages.append(system(note))
                    elif (r.stall_rescues
                          and _stuck_rounds(r.messages,
                                            floor=r.state.stall_floor) > 0):
                        # 摘掉坏工具之后还是原样打转：机会已经给过了，别再白烧一轮。
                        r.state.stall_floor = len(r.messages)
                        r.stop_reason = "stalled"
                        break
                    # HITL：从审批断点续跑——先把挂起的那批 tool_calls 按用户决策收口。
                    if r.approval_decision is not None:
                        r.asked_this_run |= await self._settle_pending(
                            r.ctx, r.cp, r.reg, r.approval_decision, r.approval_note,
                            state=r.state)
                        r.approval_decision = None
                        r.approval_note = ""
                        r.iteration += 1
                        continue
                    # 上下文瘦身 + 一致点落盘（为什么这个顺序、为什么统计每轮都写，
                    # 都写在 `_pass_point` 那一节里）
                    await self._pass_point(r)
                    r.ctx.iteration = r.iteration
                    await self.hooks.before_iteration(r.ctx)

                    # 连续失败的工具有些已经「说了不跑」；把它的 schema 也摘掉，
                    # 否则弱模型会原样再打（见 `_visible_tool_definitions`）。
                    # 让位轮执行完即清：只留出口，逼它交付（见 `_MAX_STALL_RESCUES`）。
                    resp_pause = await self._answer_round(r, stream, resuming)
                    if resp_pause is not None:
                        return resp_pause
                    if r.final is not None:
                        break
                    continue
            except BaseException:
                # 真实进程被 kill 时此处不执行，但上一致点已落盘，仍可恢复。
                if r.cp is not None:
                    r.state.persist(r.cp)
                    await self.checkpoint.mark_failed(
                        r.cp, iteration=r.iteration, messages=r.messages)
                raise

            return await self._deliver(r, message)
        finally:
            _current_hooks.reset(hooks_token)
            _current_hook_ctx.reset(ctx_token)

    async def _stall_gate(self, r: "_Round", stop: str) -> str | None:
        """花费封顶 / 原地打转的那一格：要么就此停（回 None），要么让位一轮（回那句提示）。

        打转停跑与「摘掉死工具」是同一轮判的（见 `_MAX_STALL_RESCUES`）：真机上第 4 次
        连续失败正好落在第 6 轮打转那一轮，守卫先停，摘工具的那一轮永远没发出去。
        所以停跑先让位一轮，模型终于看见一张没有坏工具的工具面，只能交付或问用户。

        为什么单独一块：它判的是「这一轮还发不发出得去」，而 `_drive` 的循环只该管
        「落盘 → 叫模型 → 把三块串起来」。塞在循环里那 29 行让 `_drive` 又长回 131 行
        （`test_plan_layout` ④ 钉的上限是 90）。
        """
        dead = _dead_tools(r.ctx.extras.get("_fail_counts") or {})
        rescue = (stop == "stalled"
                  and r.stall_rescues < _MAX_STALL_RESCUES
                  and bool(dead)
                  and r.reg.get("submit_plan",
                                user_id=r.ctx.session.user_id) is not None)
        if not rescue:
            r.stop_reason = stop
            return None
        r.stall_rescues += 1
        r.rescue_pending = True
        return (f"最后给你一轮：工具「{'、'.join(sorted(dead))}」已经从这一轮的"
                "工具清单里摘掉，再调它也不会执行。现在用**已经拿到的信息**"
                "交付——规划轮调用 `submit_plan`（没核实的数字删掉，不要为了"
                "一个数停在原地）；确实没有可交的方案，就调用 `ask_user` 说明"
                "卡在哪。")

    def _round_schema(self, r: "_Round") -> list:
        """这一轮真正发给模型的工具面。

        连续失败的工具有些已经「说了不跑」；把它的 schema 也摘掉，否则弱模型会原样再打
        （见 `_visible_tool_definitions`）。让位轮只留出口（`_NEVER_HIDE_TOOLS`），
        逼它交付；执行完即清（见 `_MAX_STALL_RESCUES`）。
        """
        defs = _visible_tool_definitions(
            r.reg, user_id=r.ctx.session.user_id,
            fail_counts=r.ctx.extras.get("_fail_counts") or {},
            only=_NEVER_HIDE_TOOLS if r.rescue_pending else None)
        # 第一级压缩：工具元信息折叠（R11）。只压描述、不动参数结构；
        # 「本轮已经用过 / 计划里点名 / 出口工具」保持完整，其余压成一行。
        # 兜底出口：真要全量 schema 走 get_definitions / GET /tools / dag_contract。
        keep = set(_NEVER_HIDE_TOOLS) | {
            str(n) for n in (r.ctx.extras.get("_executed_nodes") or ())}
        return fold_tool_schemas(defs, keep=keep, max_bytes=_TOOL_SCHEMA_MAX_BYTES)

    async def _answer_round(self, r: "_Round", stream: bool, resuming: bool) -> str | None:
        """叫一次模型并按它的回答分流：要调工具 → 工具轮；否则 → 收尾前的硬保证。

        回值是「这一轮就挂起」时要直接返回给调用方的那句（审批 / 提问 / 分叉交接），
        None 表示这一轮正常走完——调用方看 ``r.final`` 是否已落位来决定继续还是收尾
        （``plan_submitted`` 那一支与「模型直接给终答」那一支都落 ``r.final``，
        被 ``_nudge_back`` 打回的那一支不落，于是循环继续）。

        为什么单独一块：连同 ``_round_schema`` 一起切出去，``_drive`` 才回到
        「落盘 → 叫模型 → 串起来」的骨架形状（`test_plan_layout` ④ 钉的上限是 90 行）。
        """
        resp = await self._invoke(
            r.ctx, r.messages, self._round_schema(r), stream, resuming)
        r.rescue_pending = False
        _charge(r.state, resp.usage)
        if resp.wants_tools:
            pause = await self._tool_round(r, resp)
            if pause is not None:
                return pause
            if r.plan_submitted:
                r.final = resp.content or ""
                r.reached_limit = False
            return None
        if await self._nudge_back(r, resp):
            return None
        r.final = resp.content
        r.reached_limit = False
        return None

    async def _pass_point(self, r: "_Round") -> None:
        """循环里的一致点：**先**折叠更早的工具结果，**再**落盘状态与消息。

        本轮的工具结果在进链那一刻已按单条上限截断过（见 `_run_tool_round`），这里
        补的是**累计体积**：不折的话每轮都重发全部历史结果，第 40 轮的提示词就是几十万
        token——真机 run a3d8ff08098a 的 258K/5.7M 就是这么来的。

        顺序不能倒：瘦身放在落盘之前，快照里存的才是瘦身后的那份，续跑不会把体积带回来。
        统计每轮都写（即使一条都没折）：`total_bytes` 是「这一轮的窗口里工具结果占了多少」
        的直接读数，出问题时要能查到折了多少。

        状态与消息一起落（即将调用 LLM 之前）：崩溃恢复取到的调用清单/候选计划/q 片段
        必须和这一条一致点上的 messages 同批，否则对账会多算或漏算；从这里续跑也避开了
        重放工具。
        """
        # 节点自己调模型花的钱（回执里的 `node_cost_yuan`）记进这条 run 的账——**先于**折叠：
        # 折过的回执正文没了，数字也跟着没了。
        _charge_nodes(r.state, r.messages)
        r.ctx.extras["context_gate"] = self.tool_gate.shrink(r.messages)
        if r.cp is not None:
            r.state.persist(r.cp)
            await self.checkpoint.save_progress(r.cp, iteration=r.iteration,
                                                messages=r.messages)

    def _open_run(self, session: Session, messages: list, start_iteration: int,
                  cp: Checkpoint | None, *, run_id: str | None,
                  registry: ToolRegistry | None,
                  approved_plan: dict[str, Any] | None, planning: bool,
                  approval_decision: str | None, approval_note: str,
                  interactive: bool,
                  context_ledger: dict[str, Any] | None = None,
                  go_straight: bool = False,
                  recall_query: str = "") -> "_Round":
        """开一本循环账：恢复可恢复状态、装配本轮 ctx、发三道打回预算。"""
        reg = registry if registry is not None else self.registry
        # 跨挂起仍然成立的事实从指针行恢复（没有 checkpoint 时就是全新的）：执行轮在渲染
        # 确认门/弹窗上停过一次之后，批准的计划、已经跑过的调用、攒下的 qa 片段都还在，
        # 不必从零开始——这就是「续跑之后对账丢了」那类现象的根因所在。
        state = RunState.restore(cp)
        # 「直接出片」只增不减：入口带了就置上（没有 checkpoint 的离线调用也认这一位），
        # 续跑那半截则从指针行读回来。不允许在同一个 run 里改回「要确认」——用户
        # 这一轮选的是不打断，中途再弹一张卡就是违背。
        if go_straight:
            state.go_straight = True
        ctx = AgentHookContext(session=session, messages=messages, state=state)
        # Hook（如 OutboundStreamHook）按 run_id 标注这段流属于哪次执行，供回投/前端聚合。
        ctx.extras["run_id"] = run_id
        # 每轮记忆刷新要沿用装配期那套召回词（含计划步骤名）；续跑入口没有装配过
        # 上下文，留空让 hook 退回「最后一条用户消息」。
        if recall_query:
            ctx.extras["recall_query"] = recall_query
        # 分叉工具（rerun_from）要能读到「当前这个 run 走到哪了、快照管理器是谁」，
        # 才能就地开一条新 run 并把本轮接过去。没有 checkpoint 时这两项为空，工具会如实回绝。
        ctx.extras["checkpoint"] = cp
        ctx.extras["checkpoint_manager"] = self.checkpoint
        # 执行轮带着批准计划进来：对账钩子据此算「计划外 / 未履行」，只观测不拦截。
        # 调用方显式给的优先——同一 run 不会带着两份承诺跑，但 fork 出来的子 run 要认新的。
        if approved_plan is not None:
            state.approved_plan = dict(approved_plan)
        # 首条记 prompt 指纹：eval 回归据此把结果归到具体版本的 prompt。
        # 只在全新的 state 上记——续跑那半截不再补一条，否则历史里同一次执行有两个指纹。
        if not state.qa_parts:
            state.qa_parts.append(
                {"type": "prompt_fingerprint",
                 "fingerprint": self.context_builder.fingerprint()})
        # 上下文面账面（R9-T37，§5.7 第①条）：与指纹同一条留档通道、同样只记一次
        # （续跑那半截沿用快照里那份，别在历史里留两本同一轮的账）。
        self._record_context_ledger(
            state, reg, user_id=session.user_id, ledger=context_ledger,
            # 「这一轮的工具面是按什么裁的」：规划轮是规划门那张表，执行轮是 T41 按计划
            # 裁的那张（``registry is not None`` 就是「带了一张自己的表进来」），
            # 普通轮是全量（None）。账面 filter 那一栏因此与真发出去的东西一一对应。
            tools_filter=("planning_registry" if planning
                          else (ROUND_FILTER if registry is not None else None)))
        r = _Round(
            ctx=ctx, state=state, reg=reg, messages=messages, iteration=start_iteration,
            cp=cp, run_id=run_id, planning=planning, interactive=interactive,
            approval_decision=approval_decision, approval_note=approval_note,
            card_nudges=max(0, self.config.plan_claim_nudges),
            step_nudges=max(0, self.config.step_claim_nudges),
            # 「向用户提问必须走弹窗」的打回预算（见 ask_gate）。默认 1 次。
            popup_nudges=max(0, self.config.popup_question_nudges),
            # 「计划必须出卡」的打回预算：模型把方案写成正文再要用户"回复确认"
            # 时打回一次，要求改用 submit_plan 出卡。
            plan_nudges=max(0, self.config.plan_card_nudges))
        state.persist(cp)
        return r

    @staticmethod
    def _recall_terms(approved_plan: Mapping[str, Any] | None) -> str:
        """批准计划 → 本轮额外的召回词（R9-T39，§5.7 第④条）。

        给的是**步骤的机器名与中文名两样**：机器名（``render_video``）跟记忆里的工具
        教训同词，中文名（「渲染成片」）跟用户偏好同词——只给一样，另一半记忆就召不回来。
        计划里没有的字段一律跳过，不猜、不补。

        为什么由 agent 侧算而不是让 ``memory`` 自己去看计划：``ContextSource`` 只拿得到
        用户消息，计划在 ``extra_sections`` 里、渲染顺序在 source 之后——让每个 source
        自己去翻计划就是第二份「谁在装配」的真相。
        """
        if not approved_plan:
            return ""
        words: list[str] = []
        for step in approved_plan.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            node = str(step.get("node") or "").strip()
            if not node:
                continue
            words.append(node)
            label = str(get_catalog().label(node) or "").strip()
            if label and label != node:
                words.append(label)
            for name in step.get("skills_hint") or []:
                if str(name).strip():
                    words.append(str(name).strip())
        return " ".join(words)

    def _record_context_ledger(self, state: RunState, reg: ToolRegistry, *,
                               user_id: str | None, tools_filter: str | None,
                               ledger: dict[str, Any] | None) -> None:
        """把本轮的上下文面账面写进 qa_parts（R9-T37，§5.7 第①条）。

        两栏各有出处，别混：
        - ``sections`` 来自装配出口（``ContextBuilder.build`` 返回前算的那份），
          只有**刚装配过上下文**的那条入口拿得到；
        - ``tools`` 只有这里算得对——装配出口不认识工具注册表，而
          ``sent``/``total_registered`` 这两个数正是「按轮裁剪省了多少」的判据（T41）。

        ``tools_filter`` 说明这一轮的工具面是按什么裁的（``planning_registry`` /
        ``registry_for_round`` / None=全量）。它同时决定 ``tools`` 断值判不判：
        只有**裁过**的那两档判（普通轮发的是全量能力面，见 ``tools_box`` 的注释）。

        ``ledger`` 为 None（断点续跑那三条入口）时**什么都不写**：那半截没有重新装配，
        现算一个「这轮的工具面」只会与快照里那份 sections 不同批。
        """
        if ledger is None:
            return
        entry: dict[str, Any] = {"type": LEDGER_ENTRY_TYPE, **ledger}
        entry["tools"] = tools_box(
            reg.get_definitions(user_id=user_id),
            total_registered=len(self.registry.get_definitions(user_id=user_id)),
            filter_name=tools_filter)
        # 断值在这里判，而且是**请求发出之前**：工具面是本轮要发给模型的 schema，
        # 顶穿了就没有「先跑跑看」这一说（同逐块那条闸的口径）。
        check_tools_budget(entry["tools"])
        state.qa_parts.append(entry)

    async def _session_prefs(self, r: "_Round") -> dict[str, Any]:
        """本对话在「文字库」里挑过的整片开关。读不到、没连库、没挑过都回空表。

        每次要用的那一轮读一次（**不缓存**）：读的是一次主键查询，而缓存的收益是
        省下每轮不到一毫秒、代价是「用户改了偏好看不到」——一个会被用户当成 bug
        的、只在长 run 上复现的错。只在**这一轮真有出片调用**时才读，所以整条 run
        通常只发生个位数次。
        """
        conv = getattr(r.ctx.session, "conversation_id", "") or ""
        if not conv or self.storage is None:
            return {}
        try:
            return await self.storage.conversations.prefs(conv)
        except Exception as exc:  # noqa: BLE001 - 偏好是加成，读不到不该拦住出片
            logger.warning("会话偏好读取失败（按「没挑过」继续）：%s", exc)
            return {}

    async def _tool_round(self, r: "_Round", resp: LLMResponse) -> str | None:
        """模型这一轮要调工具：记账 → 三类挂起 → 真执行 → 分叉交接或推进迭代。

        返回非 ``None`` 就是本轮的终答（那句「已暂停」过渡话术），循环直接交付；
        返回 ``None`` 表示接着跑下一轮（迭代计数已在里面推进）。
        """
        qa_parts = r.state.qa_parts
        # 钉偏好**必须排在这里**：记账（下面那个循环）与渲染确认门读的都是同一份
        # ``resp.tool_calls``，钉在它们之前，账、卡、真执行的参数才是同一个值。
        # 放到 ``before_tool_call`` 钩子里（执行阶段）会出现「卡上写 A、实际渲 B」，
        # 而那正是这一步存在的理由。详见 ``motion_prefs`` 的模块注释。
        if any(getattr(tc, "name", None) in MOTION_KNOB_NODES for tc in resp.tool_calls):
            prefs = await self._session_prefs(r)
            pinned = pin_motion_prefs(resp.tool_calls, prefs)
            if pinned:
                logger.info("本对话在文字库里挑过 %s——已盖进这一轮的出片参数",
                            "、".join(sorted(set(pinned))))
        if resp.content:
            qa_parts.append({"type": "think", "content": resp.content})
        for tc in resp.tool_calls:
            qa_parts.append({
                "type": "tool call",
                "name": tc.name,
                "arguments": tc.arguments,
            })
            r.state.note_attempted(tc.name)
        r.messages.append(assistant(resp.content, resp.tool_calls))

        if r.cp is not None:
            # 确认过渲染之后，模型若又发起一次渲染：如实告诉它「已经在渲了」，
            # 不重复提交。真机里模型收尾时不会这么干，但一次误判就是白烧几分钟
            # 算力，而且两次渲染写同一个 artifact 作用域，代价不对称。
            if await self._suppress_repeat_render(r, resp):
                return None
            # 通用审批门（--approve-tools / 工具自声明 requires_approval）。
            #
            # 排在这里、并且在命中时**直接返回**，所以它天然优先于下面的渲染确认门。
            # 这是有意的：谁显式配了 `--approve-tools render_video`，
            # 谁就该拿到那道门的语义（逐条列出待批工具、批准/拒绝），
            # 而不是被渲染门的编排预览卡取代。
            # 反过来，默认配置下没有任何工具需要审批，于是渲染一律走下面那道
            # 带编排预览的门——「渲染前让用户看到编排结果」这个需求不受影响。
            if any(r.reg.needs_approval(tc.name) for tc in resp.tool_calls):
                return await self._pause(r, resp.tool_calls,
                                         reason="以下操作需要你确认后才会执行")
            # 渲染确认门：马上要烧像素出片时，先把编排结果给用户看，等确认再渲。
            # 用户明确要求「不确认绝不渲染」。
            #
            # 拦不拦、拦哪个通道，判据全在 ``ungated_render_nodes``（门开关、dry_run、
            # 这条 run 答过没有）；这里只负责「按名单挂起」。
            #
            # 但 `interactive=False`（到点自动跑的定时任务）必须放行：
            # 那条 run 的 user_id 是 "cron"，浏览器身份永远过不了归属校验，
            # 而 awaiting_approval 刻意不在 _UNFINISHED 里、自动续跑也捞不到——
            # 拦下来就是**永久挂起**。它的投递方本来就已显式声明"直接执行"。
            # 这一轮用户选了「直接出片」：这道门整条关掉。关门的代价是**少一次人工
            # 验收**——片子可能带着不满意的画面烧完；但用户已经在入口上明确选了
            # 不打断，再拦一次等于把那个开关变成假开关。见 `RunState.go_straight`。
            gated = ungated_render_nodes(resp.tool_calls,
                                         enabled=self.config.gate_render,
                                         already_asked=r.state.render_gate_asked,
                                         go_straight=r.state.go_straight)
            if r.interactive and gated:
                # 这道门题**对同一条 run 的同一个通道只问一次**，而且这一位要**落盘**。
                #
                # 真机事故：用户看到同一道题被问了几十遍。机制是——
                # ``decision_is_confirm`` 只认 ``confirm_render`` 这一个 key，
                # 用户选「保内容完整」「音乐时长内」都不是它，于是门认为"你没确认"
                # → 模型重渲 → 再拦 → 再问同一道题，无限循环。
                # 而模型其实是对的：它把时长放到了音乐长度、句子也没截断，
                # 用户的要求确实已经满足。
                #
                # 用户的原则：「答了就是答了」。所以问过一次就记住，之后按用户的
                # 选择继续走。**记在 state 而不是内存**：内存版本会被进程重启
                # 和续跑换代打穿（真机实测正是这样又问了两次）。
                # 一批里若同时出现两个出片通道，**一次只问一条**：确认了时间线那条
                # 不等于批准了图形科普片那条——那是另一笔不可逆的算力。没问到的那条
                # 这一轮不执行（``_settle_pending`` 按 ask 上的通道名分账），下一轮单独过门。
                first = gated[0]
                r.state.note_render_gate_asked(first)
                # 摘要按通道给：时间线预览就是 ``render_video`` 要渲的那份编排，拿它去问
                # 「确认渲图形科普片吗」是让用户确认一个不是自己要渲的东西；图形科普片
                # 那条路读的是分镜账（镜数/字数/估算时长/画幅/旁白/字幕/配乐）。
                sess = r.ctx.session
                preview = motion = None
                if first == RENDER_NODE:
                    preview = await self._load_preview(sess)
                elif first == MOTION_NODE:
                    # 确认卡读的是分镜账（镜数/字数/估算/开关），标签来自这个节点的契约：
                    # 主服务不再抄一份枚举表，否则两张卡会对同一个取值说两种话。
                    motion = await self._load_motion_plan(sess, resp.tool_calls, first)
                ask = build_render_ask(
                    preview, motion=motion,
                    labels=self._motion_value_labels(first) if motion else None,
                    title="" if first == RENDER_NODE else
                    f"渲染前计划确认：即将按下面的分镜出「{get_catalog().label(first)}」，开始渲染吗？")
                ask["render_node"] = first
                await self._sign_thumbs(ask)
                return await self._pause(r, resp.tool_calls, reason=ask["title"], ask=ask)

        await self._execute_tool_calls(r.ctx, resp.tool_calls, r.reg)

        # submit_plan 成功后立即收尾：计划卡已回投给用户，轮次到此为止。
        # 不收尾的话模型下一轮还会调 ask_user 弹提问卡，与计划卡叠在一起——
        # 用户还没确认计划，不该再被问新问题。
        if (any(tc.name == "submit_plan" for tc in resp.tool_calls)
                and r.state.plan_candidates):
            r.plan_submitted = True
            return None

        # 主动提问：模型调了 ask_user 就把本轮停在这里，等问题卡片
        # 上用户点选之后从同一断点续跑（与审批共用挂起/续跑机制）。
        # 为什么在这里拦而不是在工具里抛：工具只负责把问题整理成形，
        # 「停下来」是循环的职责——工具不该自己决定一整个轮次的生命周期。
        asked = self._take_asked_question(r.reg, resp.tool_calls)
        if asked is not None:
            r.asked_this_run = True
            if r.cp is not None:
                return await self._pause(r, resp.tool_calls, reason=asked["title"],
                                         ask=asked)
        fb = _detect_fallback_options(r.messages, resp.tool_calls)
        if fb is not None and r.cp is not None:
            return await self._pause(
                r, resp.tool_calls,
                reason=fb.get("error", "渲染需要你选择方案"),
                fallback_options=fb.get("options", []))

        handover = r.ctx.extras.pop("handover", None)
        if handover is not None:
            r.cp, r.messages, r.iteration = await self._adopt_fork(r.ctx, handover)
            r.reg = self.registry       # 交接后是正常执行轮，用回全量注册表
        else:
            r.iteration += 1
        return None

    async def _suppress_repeat_render(self, r: "_Round", resp: LLMResponse) -> bool:
        """本轮里的重复渲染：逐条回填「已经在渲了」，不再走任何一道门。

        认的是 **(这条 run, 这个通道, 这一次提交的参数)**：确认过 ``render_video``
        只压制它自己，模型若改提另一个出片通道，那是一次未确认的新渲染，该交给门。

        参数为什么要进键：只认前两位时，**渲染成功了但产物是废片**这条路上模型是死的
        ——改好代码重交也被回「已经提交渲染」（真机 run b7df827ebed4，见
        ``_render_confirmed`` 的注释）。参数变了就是另一个东西，放它跑。

        **这笔的代价如实记**：参数只要变一个字符就能再烧一次像素，而门那一位
        （``render_gate_asked``）已经记成"问过了"，所以这一次**不会再弹确认卡**。
        换句话说，一个把参数改来改去、又没真改对东西的模型，能在一次 run 里连烧好几版。
        换来的是一条更要紧的性质：模型自己看出废片之后**有路可走**（那次它没有，
        整条 run 死在解释上）。用户的原话是「老是处处碰壁」，所以这一边是刻意选的：
        白烧几分钟算力可恢复，交不出去不可恢复。若真机上出现连烧多版，该谈的是
        「同一通道一版之内只放行一次改动」，而不是把模型重新堵死。
        """
        # 上一次确认过的渲染**失败了**就不算「已经在渲」：放行这次重交（门已答过，不再问）。
        # 真机 run d27b37bf1419：render_web 第一次失败（file:// 取不到），模型改成内联 HTML
        # 重交，被这里回了一句「已经提交渲染」——其实什么都没在渲，进度永远是 0。
        # 失败时把这条通道的**全部**指纹一起撤掉（不只是当前这一个）：失败意味着上一次
        # 那份也没成，模型接下来交的哪一版都该能进去。
        for tc in resp.tool_calls:
            if (tc.name in RENDER_NODES
                    and _last_render_failed(r.messages, tc.name)):
                self._render_confirmed = {
                    k for k in self._render_confirmed
                    if not (k[0] == r.run_id and k[1] == tc.name)}
        repeat = [tc for tc in resp.tool_calls
                  if tc.name in RENDER_NODES
                  and (r.run_id, tc.name, _call_fingerprint(tc)) in self._render_confirmed]
        if not repeat:
            return False
        repeat_ids = {tc.id for tc in repeat}
        for tc in resp.tool_calls:
            if tc.id in repeat_ids:
                r.messages.append(tool_result(
                    tc.id, tc.name,
                    "本次编排已经确认并提交渲染，无需再次提交。"
                    "请直接向用户汇报渲染已开始。"))
            elif tc.name in RENDER_NODES:
                # 未确认过的另一个出片通道：**不能说它也已经提交**——它没有。
                # 老实写 ToolError，模型下一轮重新提它时还会经过确认门。
                r.messages.append(tool_result(
                    tc.id, tc.name, ToolError(
                        tc.name,
                        "这一条**没有执行**：本轮已有一次确认过的渲染在跑，"
                        "这个出片通道用户还没确认，不能顺带开渲。"
                        "确实需要它请下一轮单独提出，届时会先请用户确认编排结果。")))
            else:
                r.messages.append(tool_result(
                    tc.id, tc.name,
                    "本次渲染已提交，这一条随渲染一并跳过。"))
        await self.hooks.after_execute_tools(r.ctx)
        r.iteration += 1
        return True

    async def _pause(self, r: "_Round", calls: list, *, reason: str,
                     ask: dict | None = None,
                     fallback_options: list | None = None) -> str:
        """四个挂起点的同一种收口：状态先落盘 → 指针行落 awaiting_approval →
        发审批帧 → 本轮以过渡话术结束（终答与落库留给续跑那一次）。

        ``state.persist`` 必须在 ``await_approval`` 之前：审批帧发出去、用户可能几天后才点，
        其间进程随时会重启，恢复取的指针行得带上这一轮真发生过的调用与候选计划。
        """
        pending = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                   for tc in calls]
        r.state.persist(r.cp)
        await self.checkpoint.await_approval(
            r.cp, iteration=r.iteration, messages=r.messages,
            pending_calls=pending, reason=reason, ask=ask,
            fallback_options=fallback_options)
        await self.hooks.on_approval_required(r.ctx, pending, reason, ask=ask,
                                              fallback_options=fallback_options)
        return _APPROVAL_PAUSE_ANSWER

    async def _nudge_back(self, r: "_Round", resp: LLMResponse) -> bool:
        """收尾前的四条硬保证：把「说得出但没发生」的终答退回一次，并吃掉那道预算。

        每条都是**先退回、预算用尽再如实交付**（末尾补服务端核到的事实，见 ``_deliver``），
        而不是把模型的话砍掉：界面上到底有没有那张卡、这条 run 到底有没有跑过步骤，
        服务端说了算，可这句话该由模型自己收回去。
        """
        if (r.planning and not r.state.plan_candidates
                and claims_plan_card(resp.content) and r.card_nudges > 0):
            # 规划轮里「卡已提交」而没有 submit_plan 成功记录＝界面上没有卡：
            # 用户点不到确认，这一轮是死胡同。先给它一次真出卡或改口的机会。
            r.card_nudges -= 1
            r.messages.append(assistant(resp.content))
            r.messages.append(system(NO_CARD_NUDGE_TEXT))
            r.iteration += 1
            return True

        if (r.planning and not r.state.plan_candidates
                and looks_like_plan_card(resp.content)
                and not declines_plan(resp.content) and r.card_nudges > 0):
            # 上面那条措辞守卫按**文本**判「假称已出卡」，而真机上模型换一种说法
            # 就绕过去了：实测「已为你准备好 3 个候选方案……你在计划卡上选一版
            # 确认即可」两种变体都判不中 → 守卫不触发 → 规划轮以一句口头方案收尾、
            # 服务端零张卡、用户永远点不到确认。这就是「剪辑流程跑不起来」的断点。
            # 这条按**卡面形状**判（≥2 个方案标号 + 让用户挑），与具体措辞无关；
            # 纯咨询回答（问素材能不能用、要不要出卡）不具这个形状，不会误伤。
            # 候选清单挂在可恢复状态上：规划轮弹过问题再续跑，卡确实在界面上，
            # 不能因为「这次 _drive 没跑过 submit_plan」就再打回一次。
            r.card_nudges -= 1
            r.messages.append(assistant(resp.content))
            r.messages.append(system(NO_CARD_STRUCTURAL_TEXT))
            r.iteration += 1
            return True

        approved = r.state.approved_plan
        if (approved and _no_step_called(approved, r.state.calls_attempted)
                and claims_step_executed(resp.content) and r.step_nudges > 0):
            # 执行轮里「某步已跑完」而这条 run 零步骤调用＝Storyline 没收到请求，
            # 界面上不会多出任何新产物：那句「已完成」是从历史里复述的旧结果。
            # 调用清单按整条 run 算（跨挂起仍成立），否则在渲染确认门之前真跑过
            # 步骤、续跑后收尾的那句会被判成假称——与 asked_this_run 同一个病灶。
            # 先退回去要一次真调用（或一次如实说明），别把旧产物当本轮产出交出去。
            r.step_nudges -= 1
            first = (approved.get("steps") or [{}])[0].get("node") or "?"
            r.messages.append(assistant(resp.content))
            # 用 replace 而不是 str.format：提示词是磁盘文件，里面可能
            # 出现 JSON 示例（{"plans": …}），format 会拿它当占位符炸掉。
            r.messages.append(system(STEP_NUDGE_TEXT.replace("{first}", str(first))))
            r.iteration += 1
            return True

        if (self.config.require_popup_questions and r.popup_nudges > 0
                and not r.asked_this_run
                # 出片之后的可选后续（「修不修这几镜 / 要不要加衬底」）是用户的选择，
                # 不是开工前的提问：不推着模型自己拍板（见 _render_finished）。
                and not _render_finished(r.messages)
                and looks_like_asking_user(resp.content)):
            # 硬保证：向用户提问必须走弹窗。判据与守卫都在 ask_gate，
            # 只打回一次（用尽后如实交付并在末尾附服务端核对）。
            # 为什么放在最后一道（收尾前）而不是每轮都查：只有「准备收尾」
            # 才是真的把问题交给用户；中途文字里带个问号只是思考过程。
            r.popup_nudges -= 1
            r.messages.append(assistant(resp.content))
            r.messages.append(system(popup_nudge(r.planning)))
            r.iteration += 1
            return True
        # 计划必须出卡，不能写成正文要用户「回复确认」。
        #
        # 为什么要有这条硬保证：原先只靠提示词说「用 submit_plan 交候选计划」，
        # 模型完全可以把整套方案写成散文、末尾来一句「确认这个方案就回复我」——
        # 用户既没卡可点（看不到也可改不了那些开关），还被告知要打字。
        # 真机实测就是这样：弹窗问了时长和出镜分布，最后却用正文收尾要人打字确认。
        #
        # 为什么排在上面那条之后、而且出路是「出卡」不是「再弹窗」：
        # 用户要的是**能改参数的计划卡**，不是一个"要不要继续"的窗。
        # 所以认出这类收尾时，正确动作是把方案改写成 submit_plan。
        if (r.plan_nudges > 0 and not r.state.plan_candidates
                and looks_like_plan_invite(resp.content)):
            r.plan_nudges -= 1
            r.messages.append(assistant(resp.content))
            r.messages.append(system(plan_card_nudge(r.planning)))
            r.iteration += 1
            return True

        # —— 判断模型第二意见(可选,如 Jev 类"系统一"模型)——
        # 词面判据零成本但认不出绕开措辞;配置了 JUDGE_* 环境变量时,词面没拦的
        # 收尾答复交给判断模型裁决:高置信判违规 → 与词面拦截同一处理(吃同一份预算)。
        # 未配置/超时/低置信一律 None = 维持现状,绝不阻塞交付。
        verdict = await self._judge_second_opinion(r, resp)
        if verdict is not None:
            kind, nudge_text, note = verdict
            if kind == "card":
                r.card_nudges -= 1
            elif kind == "step":
                r.step_nudges -= 1
            else:
                r.popup_nudges -= 1
            r.messages.append(assistant(resp.content))
            r.messages.append(system(nudge_text + note))
            r.iteration += 1
            return True
        return False

    async def _judge_second_opinion(self, r: "_Round", resp: LLMResponse):
        """判断模型复核:词面判据没拦时,把收尾答复交给判断模型裁决。

        预算用尽就不调(省一次注定无效的调用);判定违规返回 (类别, 打回文案, 依据注),
        否则 None。事实由服务端提供,判断模型只做"文本 vs 事实"的一致性裁决。
        """
        if await judge_mod.get_judge() is None:
            return None             # 没有任何裁决通道：完全不启用，行为与词面判据一致

        if (r.planning and not r.state.plan_candidates and r.card_nudges > 0):
            try:
                v = await judge_mod.judge(
                    "这条收尾答复是否向用户声称:已经生成/提交了候选计划卡,或给出了"
                    "可供挑选确认的剪辑方案?(事实上本轮没有任何计划卡提交成功)",
                    text=resp.content,
                    facts={"plan_candidates": len(r.state.plan_candidates),
                           "submit_plan_called": False})
            except Exception as exc:  # noqa: BLE001 - 判断模型故障不阻塞交付
                logger.warning("判断模型裁决失败(退回词面判据):%s", exc)
                v = None
            if judge_mod.actionable(v):
                return ("card", NO_CARD_NUDGE_TEXT,
                        f"\n(判断模型置信 {v['confidence']:.2f}:{v.get('reason', '')})")

        approved = r.state.approved_plan
        if (approved and _no_step_called(approved, r.state.calls_attempted)
                and r.step_nudges > 0):
            try:
                v = await judge_mod.judge(
                    "这条收尾答复是否声称:剪辑步骤/渲染已经执行或完成?"
                    "(事实上本轮一次计划内的步骤工具都没有被调用)",
                    text=resp.content,
                    facts={"plan_steps": len(approved.get("steps", approved) or [])
                           if isinstance(approved, dict) else 0,
                           "step_tool_calls": 0})
            except Exception as exc:  # noqa: BLE001
                logger.warning("判断模型裁决失败(退回词面判据):%s", exc)
                v = None
            if judge_mod.actionable(v):
                first = (approved.get("steps") or [{}])[0].get("node") or "?"
                return ("step", STEP_NUDGE_TEXT.replace("{first}", str(first)),
                        f"\n(判断模型置信 {v['confidence']:.2f}:{v.get('reason', '')})")

        if (self.config.require_popup_questions and r.popup_nudges > 0
                and not r.asked_this_run):
            try:
                v = await judge_mod.judge(
                    "这条收尾答复是否在向用户索要信息/决策,需要用户回复才能继续?"
                    "(事实上本轮没有调用弹窗提问工具,用户界面上不会出现任何选项卡)",
                    text=resp.content,
                    facts={"ask_user_called": False})
            except Exception as exc:  # noqa: BLE001
                logger.warning("判断模型裁决失败(退回词面判据):%s", exc)
                v = None
            if judge_mod.actionable(v):
                return ("popup", popup_nudge(r.planning),
                        f"\n(判断模型置信 {v['confidence']:.2f}:{v.get('reason', '')})")
        return None

    async def _deliver(self, r: "_Round", message: str) -> str:
        """收尾：终答定形 → 三条「核对用尽后补事实」→ 指针行落终态 → 回会话历史落库。"""
        session = r.ctx.session
        qa_parts = r.state.qa_parts
        final = r.final
        if r.stop_reason == "budget":
            per = cost_mod.run_budget_yuan()
            final = (f"[这一条已经花了 {cost_mod.fmt_yuan(r.state.spent_yuan)}，到了每条 "
                     f"{cost_mod.fmt_yuan(r.state.budget_yuan)} 的花费上限，先停下。"
                     "已经跑出来的产物都还在；点「继续」会再给 "
                     f"{cost_mod.fmt_yuan(per)} 接着跑，不会从头重来。]")
        elif r.stop_reason == "stalled":
            final = (f"[最近 {_STALL_ROUNDS} 轮一直在重复同样的调用（"
                     f"{_last_failed_call(r.messages)}），没有任何进展，先停下免得白花钱"
                     f"（这一条已花 {cost_mod.fmt_yuan(r.state.spent_yuan)}）。已经跑出来的产物"
                     "都还在；点「继续」会接着试，也可以直接告诉我换个做法。]")
        elif r.reached_limit:
            # 撞限那句后面接**过程摘要**（设计 P5）：只回「已达最大迭代次数 N」
            # 等于把这轮读没读到、写没写下全丢了——主 Agent/用户无从判断该重派
            # 还是接着做。摘要只说事实，不替模型编结论。
            final = (f"[已达最大迭代次数 {r.iter_limit or self.config.max_iterations}"
                     f"，提前结束]\n" + _limit_summary(r.state.calls_attempted))

        final = self.hooks.finalize_content(r.ctx, final)

        # 「提问必须是弹窗」的打回机会用尽仍在提问：如实交付，但补一句服务端核对到的事实，
        # 让用户知道这句本该是个弹窗（与 NO_CARD_NOTE_TEXT 同一套兜底思路）。
        if (self.config.require_popup_questions and not r.asked_this_run
                and not _render_finished(r.messages)
                and looks_like_asking_user(final)):
            final = (final or "") + no_popup_note()

        if (r.planning and not r.state.plan_candidates
                and (claims_plan_card(final) or looks_like_plan_card(final))):
            # 核对机会用尽还在声称/还在把卡面内容当交付：界面上不会多出卡，这句不能
            # 原样进历史——末尾补一条服务端核到的事实，让用户知道该重新发起。
            # 判据与循环里那条守卫同源：纯咨询回答（问素材能不能用）不具卡面形状，
            # 原样交付，不被这句多余的事实更正污染。
            final = (final or "") + NO_CARD_NOTE_TEXT

        approved = r.state.approved_plan
        if (approved and _no_step_called(approved, r.state.calls_attempted)
                and claims_step_executed(final)):
            # 同一件事在执行轮的版本：核对机会用尽还在声称某步跑完了，就把「这条 run
            # 零步骤调用、界面上不会多出任何新产物」这条事实补在答复末尾——留原样进历史，
            # 用户读到的是上一轮的旧时长冒充这一轮的产出。
            final = (final or "") + STEP_NOTE_TEXT

        # 终答进收尾一致点：回看/分叉取这条 run 时，缺最后一句就不是一轮的终态。
        if r.cp is not None:
            if r.reached_limit:
                # 撞迭代上限是**被打断**，不是成功收尾。原先这里照样 complete()，
                # 于是 checkpoint 落成 completed、_UNFINISHED={running,failed} 把它排除，
                # /runs/active 查不到、consumer 的自动续跑也捞不回来——用户看到
                # 「[已达最大迭代次数 N，提前结束]」，刷新后这条 run 再也接不上。
                # 真机会话里已经有一批这样收尾的记录。标记成 failed 让「继续」能接。
                r.state.persist(r.cp)
                await self.checkpoint.mark_failed(
                    r.cp, iteration=r.iteration,
                    messages=[*r.messages, assistant(final or "")])
            else:
                # 完成的 run 不再被恢复：指针行只留「认过的承诺」（分叉要继承它），
                # transcript 与调用清单撤下——前者已经在 assistant 行的 qa 字段里。
                RunState.retain_residue(r.cp, r.state)
                await self.checkpoint.complete(
                    r.cp, messages=[*r.messages, assistant(final or "")])

        # 落回会话历史：流式模式下这也是把攒出的完整答复写进会话的最简单情形。
        qa_parts.append({"type": "answer", "content": final or ""})
        session.add(user(message))
        session.add(assistant(final))
        session.add_qa(message, qa_parts)
        await self._persist_answer_message(session, final or "", qa_parts)
        return final or ""


    async def _adopt_fork(self, ctx: AgentHookContext,
                          child: Checkpoint) -> tuple[Checkpoint, list, int]:
        """把本轮执行接到分叉出的新 run 上：上下文回退、作用域换绑、迭代计数归位。

        为什么在这里做而不是在工具里：cp 的所有权属于本循环，工具只能**申请**分叉；
        换 run、换产物作用域、改迭代计数这三件事必须同时发生，否则快照与身份会错位。
        （父 run 的让位由 ``CheckpointManager.fork`` 就地做，这里不必重复置状态。）
        """
        messages = _repair_orphans(list(child.messages))
        rebind_artifact_scope(child.artifact_id)
        ctx.extras["checkpoint"] = child
        ctx.extras["run_id"] = child.run_id
        ctx.messages = messages
        # 子 run 有自己的状态：承诺仍然跟着它（同一份确认的计划继续管着这次重跑），
        # 但调用清单按子 run 上下文里**真有的**回执重建——分叉点之后那些步骤已作废，
        # 照父 run 的清单对账会恰好把「用户要求重跑的那几步」判成已履行。
        already = tool_names_in(messages)
        st = ctx.state
        st.calls_attempted = list(already)
        st.calls_executed = list(already)
        st.audit_pushed = None
        st.plan_audit = None
        st.persist(child)
        messages.append(system(
            f"（已回到一致点 seq={child.forked_at_seq} 并开新执行 run={child.run_id}："
            f"该 run 之前的步骤结果保持有效，剪辑产物已换到新作用域 "
            f"{child.artifact_id or '（未分叉）'}，其后的步骤结果已作废。"
            f"请按用户最新的诉求从那里继续。）"))
        return child, messages, child.iteration

    async def _persist_answer_message(
        self, session: Session, content: str, qa_parts: list[dict[str, Any]]
    ) -> None:
        """assistant 行与 user 行配对入库；恢复路径同样补齐，所以崩溃那轮不会缺最终答复。"""
        if self.storage is None or not session.user_id or not session.conversation_id:
            return
        # 恢复路径没有第二条 user 消息可顺带建会话，这里补幂等 ensure：
        # 否则 owner 校验抛 IntegrityConflict，答复会静默不进历史。
        await self.storage.conversations.ensure(session.user_id, session.conversation_id)
        await self.storage.messages.append(
            session.user_id, session.conversation_id, "assistant", content=content,
            qa={"parts": qa_parts})

    async def _invoke(
        self,
        ctx: AgentHookContext,
        messages: list,
        tools: list,
        stream: bool,
        resuming: bool,
    ) -> LLMResponse:
        """发起一次 LLM 调用。stream 且后端支持流式时逐块回调 on_stream，并聚合为整条响应。"""
        if stream and hasattr(self.llm, "complete_stream"):
            delays = [0.0] + [_STREAM_RETRY_SLEEP * (i + 1)
                              for i in range(_STREAM_RETRIES)]
            parts: list[str] = []
            tool_calls: list[ToolCall] | None = None
            usage: dict[str, int] | None = None
            for attempt, delay in enumerate(delays):
                # 重发必须从干净的一份开始：上一枪吐到一半的字不能带进来。
                parts, tool_calls, usage = [], None, None
                if delay:
                    await asyncio.sleep(delay)
                agen = self.llm.complete_stream(messages, tools)
                ait = agen.__aiter__()
                finished = False
                try:
                    while True:
                        # 逐块等，且**每块**都有上限：连接有心跳、内容不来时，
                        # SDK 的读超时永远不响（见 `_STREAM_STALL_SEC`）。
                        try:
                            chunk = await asyncio.wait_for(
                                ait.__anext__(), timeout=_STREAM_STALL_SEC)
                        except StopAsyncIteration:
                            finished = True
                            break
                        if chunk.delta:
                            parts.append(chunk.delta)
                            await self.hooks.on_stream(ctx, chunk.delta)
                        if chunk.tool_calls is not None:
                            tool_calls = chunk.tool_calls
                        if chunk.usage is not None:
                            usage = chunk.usage
                except Exception as exc:  # noqa: BLE001 —— 只对「断线/超时」重发
                    # 吐过字的不能重发：界面上会出现两遍同一段回答。吐过字之后断线
                    # 就按失败上报，让续跑去接（上下文与已执行步骤都还在）。
                    if (parts or attempt == len(delays) - 1
                            or not _transient_stream_error(exc)):
                        raise
                    logger.warning("流式调用断线（%s: %s），第 %d 次重发",
                                   type(exc).__name__, str(exc)[:120], attempt + 1)
                finally:
                    # 每一枪结束都关流：被 wait_for 取消的那一枪若不关，连接会留在
                    # 池子里半死不放（重发时再开一条，旧的那条没人收）。
                    aclose = getattr(agen, "aclose", None)
                    if aclose is not None:
                        try:
                            await aclose()
                        except Exception:  # noqa: BLE001 —— 关流失败不该盖住真错因
                            pass
                if finished:
                    break
            await self.hooks.on_stream_end(ctx, resuming=resuming)
            resp = LLMResponse(content="".join(parts) or None, tool_calls=tool_calls or [],
                               usage=usage)
            if usage is not None:
                await self.hooks.on_llm_usage(ctx, usage)
            return resp

        resp = await self.llm.complete(messages, tools)
        if stream:
            # 后端不支持流式：整段作为一次 delta，保证 hook 语义一致。
            if resp.content:
                await self.hooks.on_stream(ctx, resp.content)
            await self.hooks.on_stream_end(ctx, resuming=resuming)
        if resp.usage is not None:
            await self.hooks.on_llm_usage(ctx, resp.usage)
        return resp

    async def _settle_pending(
        self, ctx: AgentHookContext, cp: Checkpoint | None, reg: ToolRegistry,
        decision: str, note: str = "", state: RunState | None = None,
    ) -> bool:
        """把挂起的那批 tool_calls 按用户决策收口（批准=执行，拒绝=回喂拒绝结果）。

        恢复路径专用：挂起时 messages 末尾已是 assistant(tool_calls)，这里只补它的
        tool 回执——之后循环自然进入下一轮 LLM，既不重发工具、也不丢这批调用。

        fallback_options 场景：decision 是用户选的方案 key（如 "ffmpeg"），
        注入 render_mode=decision 到工具参数后重新执行。

        ``note`` 是用户在弹窗里选/写的**原话**（多题时是「题 → 选择」汇总，
        自定义时是他打的那句）。渲染确认门与主动提问靠它把用户意图带进上下文；
        原先只转发 decision，用户填的自定义内容会被丢掉——而「选项之外还能说人话」
        正是这套弹窗必须留的一条路，所以必须传到底。
        """
        if cp is None:
            return False
        approval = cp.approval or {}
        pending = list(approval.get("pending_calls") or [])
        fallback_options = approval.get("fallback_options") or []
        asked = approval.get("ask")
        # 这道门问的是哪条出片通道（普通主动提问没有这一位，撤回就是空操作）。
        #
        # 只有下面**非批准**的那几条出口才用它，而且只用在一种情形上：**用户要的是
        # 「改」而不是「答」**。批准那几条出口**不许**碰它——那正是「渲染失败后重交
        # 同一通道不再弹第二张卡」所依赖的记录。见 ``RunState.forget_render_gate_asked``。
        gated_node = str((asked or {}).get("render_node") or "")
        # 用户这一答是「我还要改」，还是「答了这道门」？
        #
        # 这个区分就是这条记录的全部要害（真机代价 2026-10-16，run 9272c7f59501 那 20 镜）：
        #   · 答的是出片门自己那几句（确认渲 / 保完整 / 按原时长截断）→ **答了就是答了**。
        #     这一位留着，模型重交同一条通道不再弹第二张卡——那正是用户骂过的
        #     「为什么老是问我这个问题，我回答无数遍了」。
        #   · 「我还要改」或用户自己写的自由文本 → **这次提问作废**。模型重做完必须
        #     重新过门，否则用户再也看不到确认卡。``decision_text`` 对这两种答复都
        #     白纸黑字写着「再弹一次计划确认卡给用户过目」——修复前那句话是假的。
        #
        # 自由文本的判别很直接：前端 ``qSubmit`` 在用户自己打字时把**原文**当 decision
        # 发过来（`decideApproval(c.approval, first.answer, …)`，见 App.vue），
        # 所以它必然既不在卡片的选项 key 里、也不在出片门的词汇表里。
        #
        # 认「出片门的词汇表」而不是只认卡片选项，是因为时长冲突那两个选项
        # （``KEEP_FULL_OPTION`` / ``TRUNCATE_OPTION``）只在**有冲突时**才上卡；
        # 不认它就会出现「用户明明点着卡上的按钮答的，却被当成没答」。
        _gate_answered = {CONFIRM_OPTION, KEEP_FULL_OPTION, TRUNCATE_OPTION}
        # ``asked`` 可能是 None（这条 run 挂着的是别的挂起点 ／ 审批已经收过口）——
        # 那种情况下 ``gated_node`` 本来就是空串，撤回是空操作，这里取空表即可。
        _offered = {str(o.get("key") or "") for o in ((asked or {}).get("options") or [])}
        _wants_change = (decision == ADJUST_OPTION) or (
            decision not in _offered and decision not in _gate_answered)
        cp.approval = {}
        calls = [ToolCall(id=str(c.get("id") or ""), name=str(c.get("name") or ""),
                          arguments=c.get("arguments") or {}) for c in pending]
        # 主动提问 / 渲染确认门的收口方式取决于用户选了什么：
        #
        # · 确认类决策（渲染确认 / 批准）→ **执行**挂起的那批调用。
        #   为什么不能只回喂一句话：模型看到「用户已确认」后会**再调一次**渲染，
        #   于是又被门拦住，弹第二张一模一样的卡——真机实测就这样循环。
        #   确认之后直接执行原批调用，才是「用户点了一下，片子开始渲」。
        # · 其余（要改 / 自定义 / 未识别）→ 回喂用户原话，让模型重做编排，
        #   绝不执行（render_video 就在这批里，跑掉就等于没确认就渲了）。
        if asked is not None and not fallback_options:
            # 这批调用里有没有 ask_user。渲染确认门也走这一分支（它同样带 ask 结构），
            # 但只有 ask_user 那一路才是「本轮已经问过」的证据。
            asked_now = any(tc.name == "ask_user" for tc in calls)
            # 只给「还没有回执」的那几条补结果。
            #
            # 为什么必须查重：主动提问那一路（ask_user）在挂起**之前**就已经执行过了，
            # 它的 tool 回执已经在 messages 里。这时再补一条同 tool_call_id 的结果，
            # 就成了「一个 id 两条回执」——LLM API 直接 400，整条 run failed。
            # （渲染确认门那一路是先拦后执行，所以它确实需要补；两种情形在这里统一处理。）
            already = {m.get("tool_call_id") for m in ctx.messages
                       if isinstance(m, dict) and m.get("role") == "tool"}
            remaining = [tc for tc in calls if tc.id not in already]
            if not remaining:
                # 这批调用全都已经有回执了——主动提问那一路正是这样：ask_user 在挂起
                # 之前就执行完，回执（「问题已发给用户，本轮到此为止」）已经在链里。
                # 这时必须把用户点选/写的那句话以 user 角色补进去。原先两条分支在
                # remaining 为空时都不写入任何东西（确认类只记一个渲染标记就 return，
                # 非确认类的补写被 `if remaining:` 挡住），于是模型永远只看到
                # 「问题已发给用户」而等不到答案，只能一遍遍重问同一道题
                # （真机实测：连问 6 题，第 6 题还在重问第 1 题）。
                text = note or decision_text(decision, "")
                if text:
                    ctx.messages.append(user(text))
                return asked_now
            if decision_is_confirm(decision) and calls:
                # 记下「这条 run 的**这一个出片通道**已确认」：同通道再出现不再拦、
                # 也不再重复提交；未确认过的其它通道照旧过门。
                run_id = str(cp.run_id or "")
                approved = str((asked or {}).get("render_node") or "")
                batch: list[ToolCall] = []
                deferred: list[ToolCall] = []
                for tc in remaining:
                    # 一次只批一条通道。ask 上带着这一道题问的是哪条；老 checkpoint
                    # 没这一位（升级前挂起的），按整批处理，不让它卡死在确认不了的状态。
                    if approved and tc.name in RENDER_NODES and tc.name != approved:
                        deferred.append(tc)
                    else:
                        batch.append(tc)
                self._render_confirmed.update(
                    (run_id, tc.name, _call_fingerprint(tc))
                    for tc in batch if tc.name in RENDER_NODES)
                for tc in deferred:
                    ctx.messages.append(tool_result(
                        tc.id, tc.name, ToolError(
                            tc.name,
                            "这一条**没有执行**：用户刚才确认的是另一个出片通道的编排，"
                            "这个通道还没有过确认门——那是另一笔不可逆的算力，不能顺带批准。"
                            "确实需要它请下一轮单独提出，届时会先把它的编排结果交用户确认。")))
                await self._execute_tool_calls(ctx, batch, reg)
                return asked_now
            # ⚠️ 关键：这个 tool 结果**不是**工具的返回值，而是「本轮没执行、先把用户
            # 的话交给你」。真机事故：用户对渲染确认门选了「要改」并写了自由文本，
            # 这里直接把那句话当成 ``render_video`` 的结果写回，模型于是看到
            # 「render_video 返回了『和音乐长度一致…』」——以为渲染已受理，回话
            # 「渲染已提交，正在跑」，而服务端从未创建渲染任务，整条链路就断在最后一步。
            # 所以文本必须以 ToolError 的形态说明「没跑」，而不是冒充成功回执。
            text = note or decision_text(decision, "")
            for tc in remaining:
                ctx.messages.append(tool_result(
                    tc.id, tc.name,
                    ToolError(tc.name,
                              f"【本轮未执行】用户没有确认这一步，而是给出了新的要求："
                              f"{text}\n"
                              f"「{tc.name}」**没有运行、没有产生任何产物**"
                              f"（服务端没有登记任务）。"
                              f"请先按上面的要求完成该做的事，再重新调用它；"
                              f"不要声称它已经提交或完成。")))
            if state is not None and _wants_change:
                state.forget_render_gate_asked(gated_node)
            await self.hooks.after_execute_tools(ctx)
            return asked_now
        if fallback_options and decision in {o.get("key") for o in fallback_options}:
            calls = [ToolCall(id=tc.id, name=tc.name,
                              arguments={**tc.arguments, "render_mode": decision})
                     for tc in calls]
            await self._execute_tool_calls(ctx, calls, reg)
            return False
        if decision == "approve":
            await self._execute_tool_calls(ctx, calls, reg)
            return False
        for tc in calls:
            ctx.messages.append(tool_result(
                tc.id, tc.name, ToolError(tc.name, _APPROVAL_DECISION_TEXT["reject"])))
        # 明确的「拒绝」同样不是批准：这次提问作废，模型重做后要重新过门。
        if state is not None and _wants_change:
            state.forget_render_gate_asked(gated_node)
        await self.hooks.after_execute_tools(ctx)
        return False

    async def _execute_tool_calls(
        self, ctx: AgentHookContext, tool_calls: list[ToolCall],
        registry: ToolRegistry | None = None,
    ) -> None:
        results = await self._run_tool_round(ctx, tool_calls, registry)
        # 结构性并批补进来的调用（id 前缀 ``auto-``）**不能**写工具结果消息：
        # 它们不在模型那条 assistant(tool_calls) 里，凭空插一条 tool 消息会被
        # LLM API 拒掉（400：tool 消息必须是某条 tool_calls 的回应）。
        # 它们的产物已经落在产物表里，下一轮模型需要时自己读得到——不回填无损失。
        origin = [tc for tc in tool_calls if not str(tc.id).startswith("auto-")]
        # 分叉交接（rerun_from）已经把本轮搬到新 run 上，旧作用域的在途渲染不该再由
        # 这条循环追着查——新 run 的上下文里会有它自己的渲染。
        if ctx.extras.get("handover") is None:
            await self._follow_inflight_renders(
                ctx, [(tc.name, results.get(tc.id)) for tc in origin], registry)

    async def _expand_ready_siblings(
        self, ctx: AgentHookContext, tool_calls: list[ToolCall], reg: ToolRegistry,
    ) -> list[ToolCall]:
        """按计划依赖图，把同层就绪的独立兄弟节点补进本批（结构性地并行）。

        只在**有执行计划**时生效（规划轮/闲聊没有图，原样返回）。
        补进来的调用与模型自己发起的走完全同一条执行路径——同样的
        before/after 钩子、同样的结果回填，只是并行跑。
        """
        nodes = list(getattr(self, "_exec_plan_nodes", None) or ())
        if not nodes or not tool_calls:
            return tool_calls
        contract = getattr(self.plan_gate, "contract", None)
        try:
            required_map = contract.required_map() if contract is not None else {}
        except Exception:  # noqa: BLE001 - 取不到图就退化成原样（只是不并批）
            return tool_calls
        # 已完成的节点：从产物表按当前作用域取值（与 read_node_history 同一处口径），
        # 再加上本 run 这一轮之前已经执行过的调用名。
        completed: set[str] = applied_names(ctx.extras.get("_executed_nodes") or ())
        try:
            if self.storage is not None:
                ident = current_identity()
                sid = storyline_session_id(
                    ctx.session.user_id, ctx.session.conversation_id)
                art = (ident.artifact_id if ident is not None else "") or ""
                completed |= applied_names(await self.storage.artifacts(sid, art).executed())
        except Exception:  # noqa: BLE001 - 读不到就当没有已完成节点，退化成不并批
            pass

        def _resolve(name: str) -> Any:
            # 只有既安全又可并发的才补：写工具若没声明读写集，concurrency_safe=False
            return reg.get(name, user_id=ctx.session.user_id)

        def _make(name: str) -> ToolCall | None:
            # 参数留空：剪辑节点的公共入参（session_id/artifact_id/…）由拦截器
            # 在请求前统一补齐（见 orchestration 的 before_request），
            # 与模型自己调用时的路径完全一致。
            return ToolCall(id=f"auto-{name}", name=name, arguments={})

        return _with_ready_siblings(
            list(tool_calls), completed, required_map, nodes,
            resolve=_resolve, make_call=_make)

    async def _run_tool_round(
        self, ctx: AgentHookContext, tool_calls: list[ToolCall],
        registry: ToolRegistry | None = None,
    ) -> dict[str, Any]:
        """按状态键冲突分批：组内 gather 并发，组间按序，回填仍按 LLM 给的原始顺序。

        分批规则在 ``tool.plan_batches``：两个调用只要写集相交、或一方写集撞上另一方
        读集，就不能同批——这是工具自己声明的契约（``Tool.reads`` / ``Tool.writes``），
        不再靠「这批里出现过被依赖的节点名就整批串行」那种一次性推理。
        """
        reg = registry if registry is not None else self.registry
        # 逐调用恢复点（R10）：本轮开始前取出这个 run 里「已经有结果」的 call_id。
        # 崩溃恢复时它非空——已完成的调用直接跳过，在途的那些才会重放。
        cp_mgr = getattr(self, "checkpoint", None)
        # run_id 住在 extras（_open_run 写入），不是 ctx 的字段：只读属性名会让
        # cp_run_id 恒为空，逐调用条目永远不落盘——R10 的端到端用例正是这么抓到的。
        cp_run_id = str((ctx.extras or {}).get("run_id")
                        or getattr(ctx, "run_id", "") or "")
        done_ids: set[str] = ctx.extras.get("_completed_calls") or set()
        if not done_ids and cp_mgr is not None and cp_run_id:
            try:
                done_ids = await cp_mgr.completed_calls(cp_run_id)
            except Exception:  # noqa: BLE001  读不到就按「都没完成」，不能因此不干活
                logger.warning("读取已完成调用失败，按未完成处理", exc_info=True)
            ctx.extras["_completed_calls"] = done_ids
        fail_counts: dict[str, int] = ctx.extras.setdefault("_fail_counts", {})
        # 本 run 的重复调用账：key → {n, same, last, refused}。
        # n=同参数真跑次数，same=连续逐字相同结果的次数，last=上次结果的字节摘要。
        repeats: dict[str, dict[str, Any]] = ctx.extras.setdefault("_repeat_tracker", {})
        # 本 run 读过哪些 shot 卡（路径集合）与告过几次。上限见 `_MAX_SHOT_CARDS`：
        # 按**卡**计数，不按参数——这正是参数级那两条闸看不见的那一维。
        cards_read: set[str] = ctx.extras.setdefault("_shot_cards_read", set())
        # 本 run 每个技能正文取过哪些行窗口（技能名 → {页键: 那一枪的 tool_call_id}）。
        # 同样按**窗口**计次而不是按参数：上限见 `_MAX_SKILL_BODY_PAGES`。值存调用 id
        # 是为了分辨「还在上下文里」与「已被折叠收走」（见 `_page_covered`）。
        body_pages: dict[str, dict[str, str]] = \
            ctx.extras.setdefault("_skill_body_pages", {})
        # 本 run 每份**附件**取过哪些行窗口（(技能名, 路径) → {页键: 调用 id}）。
        # 与正文分开计数：同一份技能的正文与附件是两份互不重叠的文本。
        attach_pages: dict[tuple[str, str], dict[str, str]] = \
            ctx.extras.setdefault("_skill_attach_pages", {})
        # 本 run 已经放行的 load_skill 枪数（整 run 总量，见 `_MAX_SKILL_READS`）。
        # 与上面两个窗口账分开：那两条按**一份文件**数，这条按**整条 run**数。
        skill_reads: int = ctx.extras.setdefault("_skill_reads", 0)
        # 上一次交付（`submit_plan`）之后连续预览了几次。
        # 见 `_PREVIEW_SOFT` / `_PREVIEW_HARD`：花瓣这类审美微调会让「每版代码都不同」
        # 的写—预览循环一直算「有进展」，只有这个计数能把它引向交付。
        previews_since_delivery: int = ctx.extras.setdefault(
            "_previews_since_delivery", 0)
        # 这一轮相对**本条 run 的消息链**里还看得见的回执。折叠过的正文只剩存根，
        # 不能再算「给过」。
        folded_ids = {str(m.get("tool_call_id")) for m in ctx.messages
                      if isinstance(m, Mapping) and m.get("role") == "tool"
                      and str(m.get("content") or "").startswith(f"[{FOLD_MARKER}")}
        visible_ids = {str(m.get("tool_call_id")) for m in ctx.messages
                       if isinstance(m, Mapping) and m.get("role") == "tool"
                       and str(m.get("tool_call_id")) not in folded_ids}

        # 结构性并批：把**同层就绪、互不依赖**的兄弟节点自动补进本批。
        #
        # 为什么要做在这一层：``plan_batches`` 只能对同一条回复里的调用并批，
        # 于是并行与否取决于模型的输出习惯——真机实测它把 asr 与 split_shots
        # 分成两轮（拿到 ASR 结果才切镜），本可并行的链路退化成一前一后，
        # 白等一整段 ASR（237 秒）。**并行是图的性质，不该由模型自觉决定。**
        # 判据全在图里：前置已完成 + 与本次调用无依赖 + 同层 + 在计划内。
        tool_calls = await self._expand_ready_siblings(ctx, tool_calls, reg)

        # 同一条回复里逐字相同的第二次：并批会真打两次后端，结果必然同一份。
        # 在分批前先标出来（gather 并发，靠计数器先到先得拦不住）。
        dup_ids: set[str] = set()
        _intra: dict[str, int] = {}
        for tc in tool_calls:
            k = _repeat_key(tc.name, tc.arguments)
            _intra[k] = _intra.get(k, 0) + 1
            if _intra[k] > 1:
                dup_ids.add(str(tc.id))

        repeat_warnings: list[str] = []
        # 两道**软线**的提醒（都在家族级闸里攒、在全部回执之后一并插一条 system）：
        # 过了软线照跑不拦，只把「够了」这句话说到模型眼前（§2.2：读几张卡是它的选择，
        # 服务端不该替它封路；硬线仍拦，见 `_shot_card_gate` 的注释）。
        over_cards: list[str] = []
        over_pages: list[str] = []
        # 整 run 阅读总量的软线提醒（见 `_MAX_SKILL_READS`）。只说越过软线那一刻，
        # 与 `over_pages` 同一条通道（全部回执之后插一条 system）。
        over_reads: list[str] = []
        # 软线提醒里点名「下一步该调哪些工具」：从注册表读，不写死节点名——
        # 规划轮与执行轮的工具面不同，同一条话在两轮里要落到各自真实存在的名字上。
        _action_names = [t.name for t in reg.all_tools()
                         if not t.read_only and t.name != "load_skill"][:6]
        action_hint = "、".join(f"`{n}`" for n in _action_names) or "本轮的下一步工具"

        def _refused(tc: ToolCall, times: int) -> ToolError:
            return ToolError(
                tc.name,
                f"Error: 「{tc.name}」已连续失败 {times} 次，这次没有再执行——"
                f"原样重试只会得到同一个错误。换参数或换工具，"
                f"或者直接告诉用户卡在哪、给个方案（规划轮就调用 submit_plan）。")

        def _blocked_repeat(tc: ToolCall) -> ToolError | None:
            # ---- 滑动窗口记账（2026-10-23）。每一趟都记，连放行的工具也算——
            # 否则窗口里少记一次，「最近 N 次」就不是最近 N 次了。
            # 存在 ctx.extras 里（与 _repeat_tracker 同寿），跨迭代不丢。
            _seq = ctx.extras.setdefault("_call_seq", [0])
            _seq[0] += 1
            _wst = repeats.setdefault(
                _repeat_key(tc.name, tc.arguments),
                {"n": 0, "same": 0, "last": "", "refused": 0, "hist": []})
            _hist = _wst.setdefault("hist", [])
            _hist.append(_seq[0])
            del _hist[:-_REPEAT_WINDOW]
            _wst["w"] = len(_hist)
            """这一枪是不是无进展的重复。命中就根本不打后端，只回喂事实。"""
            # 检索类一律放行：搜到就算数，不替模型判断「这个词搜过没有」。
            if tc.name in _REPEAT_EXEMPT_TOOLS:
                return None
            if str(tc.id) in dup_ids:
                return ToolError(
                    tc.name,
                    f"Error: 同一次回复里「{tc.name}」这条调用（参数逐字相同）出现了两次，"
                    f"第二次不执行——一次已经足够，重复只会拿到同一份结果。"
                    f"要拿别的东西就换参数或换工具。",
                    counts_as_failure=False)
            st = repeats.get(_repeat_key(tc.name, tc.arguments))
            if st is None:
                return None
            # 上次那份回执已经被折叠闸收走（上下文里只剩存根）：这一枪是**恢复**，
            # 不是打转。放行——不折叠时照旧按重复拦。
            # 真机 2026-10-21 run `96b6132df10b` 就死在这一格：12 个 section 读完
            # 被压缩收走 → 回头重取 → 全被拒 → 六轮之后整条 run 判「原地打转」。
            #
            # 计数**不归零**：折叠收走的是那份可见副本，不是「这份文件读过几次」这个
            # 事实。补回同一份内容之后，紧接着再打同一份仍是逐字相同，该由下面那道
            # 闸拦住（否则折叠一次就白送一次重读，真机 d433 的形状会多烧一枪）。
            if str(st.get("last_id") or "") in folded_ids:
                return None
            # 写文件是**幂等**的：同样的路径与内容再写一遍，磁盘状态不会变。对它回
            # 「重复、不执行」会把模型推向更坏的路——真机 2026-10-19 run
            # `ff8b2d9ed640`：同一份内容被判重复后，它换名新建 `shots/shot01.tsx`
            # → `final01.tsx` → `v2_01.tsx`，一条 run 留下四套镜头文件。
            # 取舍按「堵死比白烧更坏」：多跑一次本地覆盖几乎不花钱，拦下它却让模型
            # 去改文件名。上限仍在：`_stuck_rounds` 把「同样的写」按打转计，连续
            # 6 轮仍会停。**成功**写重复放行；上次是错误时照下面的规则拦。
            if tc.name in _WRITE_TOOLS and not st.get("last_error"):
                return None
            # 告过两次还照打：改成「记失败」，让 _MAX_CONSECUTIVE_FAILURES 那道老闸接手，
            # 否则模型可以无限刷这条便宜回执。
            harsh = st["refused"] >= _MAX_REFUSALS_BEFORE_FAILURE
            if st["same"] >= _MAX_SAME_RESULT_REPEATS:
                return ToolError(
                    tc.name,
                    f"Error: 「{tc.name}」这一份参数已经真跑过 {st['n']} 次，"
                    f"每次返回的结果**逐字相同**，这次不再执行。"
                    f"再打同一份请求不会得到任何新信息：换参数（换关键词、换节点名）、"
                    f"换工具，或者直接说明卡在哪、给个方案（规划轮就调用 submit_plan）。",
                    counts_as_failure=harsh)
            # 滑动窗口：同一签名在最近 _REPEAT_WINDOW 次调用里出现
            # _REPEAT_WINDOW_LIMIT 次以上。**与连不连续无关**——上面两道闸数的是
            # 「连续」，交替变体绕得过；这一道绕不过。
            if st.get("w", 0) >= _REPEAT_WINDOW_LIMIT:
                return ToolError(
                    tc.name,
                    f"Error: {tc.name} 这同一份调用在最近 {_REPEAT_WINDOW} 次里已经出现 "
                    f"{st['w']} 次，这次不再执行。隔着别的调用穿插着重试同样算打转——"
                    f"这条路走不通。换工具、换参数，或者直接告诉用户卡在哪、给方案"
                    f"（规划轮就调用 submit_plan）。",
                    counts_as_failure=harsh)
            if st["n"] >= _MAX_SAME_ARGS_ATTEMPTS:
                return ToolError(
                    tc.name,
                    f"Error: 「{tc.name}」同一份参数已经真跑过 {st['n']} 次，这次不再执行。"
                    f"每次返回或许略有不同，但同参重复到这个数目就不是推进而是打转——"
                    f"换参数或换工具，或告诉用户卡在哪（规划轮就调用 submit_plan）。",
                    counts_as_failure=harsh)
            return None

        def _shot_card_gate(tc: ToolCall) -> ToolError | None:
            """读卡上限**两档**：软线到了照跑、只提醒；硬线才不执行。

            记账放在放行这一刻、而不是跑完之后：一次回复里的几枪是并发打的
            （实测一轮 4–5 枪），「先查后记」会让同批里每一张都以为自己是第 30 张，
            一轮就把上限撑破。这一小段没有 await，不会交错。

            为什么软线不拦（2026-10-16，§2.2）：读几张卡、读到什么时候够，是**模型的
            选择**——服务端该做的是把「索引够选卡了」这句话说到它眼前，而不是替它把路
            封掉。越过软线的那一刻记**一句**（判据是「正好第 SOFT+1 张」，不是「超过
            SOFT 张」），全部回执之后再插一条 system 提示（与「无进展重复」同一条通道）。
            说一次而不是每张一句：`over_cards` 每轮重建，按「超过」判就会每轮都重说一遍，
            同一句话在上下文里堆三十遍只会挤掉别的东西。

            硬线仍是老行为（不执行 + 按 `_MAX_REFUSALS_BEFORE_FAILURE` 升级成真失败，
            让 `_MAX_CONSECUTIVE_FAILURES` 接手）——守的是那个真事故：144 次 load_skill、
            一张计划卡都没出。**提醒拦不住的那种模型，只有硬线拦得住。**
            """
            card = _shot_card_path(tc.name, tc.arguments)
            if not card:
                return None                        # 不是卡（索引/技能正文/别的工具）
            if card in cards_read:
                return None                        # 重读同一张：归参数级那两条闸管
            if len(cards_read) < _MAX_SHOT_CARDS_HARD:
                cards_read.add(card)
                if len(cards_read) == _MAX_SHOT_CARDS + 1:
                    # 正好越过软线这一张：照跑，但把话说给模型听。只说这一次——
                    # 每多读一张就说一遍的话，上下文里会堆满同一句话。
                    over_cards.append(
                        f"这一轮已经打开 {len(cards_read)} 张 shot 卡（超过 SKILL.md 里"
                        f"那句「不要读超过 {_MAX_SHOT_CARDS} 张卡」）。"
                        f"索引 `shots/INDEX.md` 每行都带着卡名、能量、一句话描述、时长、适用，"
                        f"以及 demo 的**文件名**与字节——选卡、填 `derived_from` 看那一份就够。"
                        f"手上这几张看完就交计划（`submit_plan`）：把选中的卡名照抄进行首的"
                        f"`<类别>/<卡名>` 填进 `card`。"
                        f"到 {_MAX_SHOT_CARDS_HARD} 张就不再执行了。")
                return None
            n = ctx.extras["_shot_card_refusals"] = ctx.extras.get("_shot_card_refusals", 0) + 1
            return ToolError(
                tc.name,
                f"Error: 这一轮已经读过 {len(cards_read)} 张 shot 卡，到上限了"
                f"（SKILL.md：「不要读超过 {_MAX_SHOT_CARDS} 张卡」，硬线 {_MAX_SHOT_CARDS_HARD}），"
                f"`{card}` 这次不执行。索引 `shots/INDEX.md` 每行都带着卡名、能量、"
                f"一句话描述、时长、适用，以及 demo 的**文件名**与字节——"
                f"**选卡、填 `derived_from` 都看那一份就够**（`derived_from` 照抄 demo 栏里的"
                f"relpath：文件名与卡名多数对不上，别拿卡名去拼），不必逐张打开卡全文。"
                f"现在就交计划（规划轮调用 submit_plan）："
                f"把选中的卡名照抄进行首的 `<类别>/<卡名>` 填进 `card`。",
                counts_as_failure=n > _MAX_REFUSALS_BEFORE_FAILURE)

        def _skill_body_gate(tc: ToolCall) -> ToolError | None:
            """同一份技能正文的重叠窗口上限：**两档**，理由与 `_shot_card_gate` 同。

            回执必须把「已经取过哪些窗口」摆出来再打回：这一枪不是工具坏了，而是
            模型在同一段里换行数重来一遍。只说「到上限了」它只会换个 offset 再试——
            真机那条 run 就是这么从 220 换到 222、从 531 换到 535 的。
            """
            page = _skill_body_page(tc.name, tc.arguments)
            if not page:
                return None
            skill, key = page
            pages = body_pages.setdefault(skill, {})
            if key in pages:
                return None                        # 同一窗口重取：归参数级那两条闸管
            # 整段都落在**还看得见**的旧窗口里 → 不再重发（跨文件打转真正的形状：
            # 换个起始行/行数把同一段再取一遍，页键不同，上面那条判据接不到）。
            if _page_covered(pages, key, visible_ids):
                return ToolError(
                    tc.name,
                    f"Error: 技能 `{skill}` 的正文这一页（{_page_label(key)}）本 run 已经"
                    f"给过，回执还在更早的上下文里——这次不再重复发送，"
                    f"重读同一段不会带来新信息。\n"
                    f"本 run 已取过的窗口：{'、'.join(_page_label(p) for p in pages)}。\n"
                    f"要往下做就直接用已经读到的内容（规划轮就交计划）；"
                    f"只缺某一节，按分段目录用 load_skill(name, section=键名) 单取那一块；"
                    f"早先那份真被折叠收走了，就用一个**更宽**的窗口一次取回。",
                    counts_as_failure=False)
            if len(pages) < _MAX_SKILL_BODY_PAGES_HARD:
                pages[key] = str(tc.id)
                if len(pages) == _MAX_SKILL_BODY_PAGES + 1:
                    # 只说越过软线的那一次，理由同 `_shot_card_gate`。
                    over_pages.append(
                        f"技能 `{skill}` 的正文这一轮已经按 {len(pages)} 个不同的行窗口取过"
                        f"（超过 {_MAX_SKILL_BODY_PAGES} 个）。这些窗口合起来基本就是整份正文，"
                        f"**再换一个起始行不会带来新信息**——直接用已经读到的内容往下走；"
                        f"确实缺某一节，就照整份加载时给的那份**章节目录**取行号，"
                        f"或用分段目录里的 section 键名单独取那一块。"
                        f"到 {_MAX_SKILL_BODY_PAGES_HARD} 个窗口就不再执行了。")
                return None
            n = ctx.extras["_skill_body_refusals"] = \
                ctx.extras.get("_skill_body_refusals", 0) + 1
            covered = "、".join(_page_label(p) for p in pages)
            return ToolError(
                tc.name,
                f"Error: 技能 `{skill}` 的正文这一轮已经按 {len(pages)} 个不同的行窗口"
                f"取过（{covered}），到上限了（一份正文最多 {_MAX_SKILL_BODY_PAGES} 个窗口，"
                f"硬线 {_MAX_SKILL_BODY_PAGES_HARD} 个），"
                f"这次不再执行。"
                f"这些窗口合起来基本就是整份正文——**再换一个起始行不会带来新信息**。"
                f"请直接用已经读到的内容交计划（规划轮调用 submit_plan）；"
                f"确实知道缺哪一节，就照整份加载时给的那份**章节目录**取行号，"
                f"limit 只给到下一节的前一行，别取一个和上面重叠的大窗口。",
                counts_as_failure=n > _MAX_REFUSALS_BEFORE_FAILURE)

        def _attach_gate(tc: ToolCall) -> ToolError | None:
            """同一份附件的重叠/递增窗口上限：两档，理由与 `_skill_body_gate` 同。

            这一枪的出路必须**当场可执行**：回执里写「整份一次取回」并说明它不受
            这条限制——整份取不计页（见 `_attach_page`），所以它永远是硬线之后能
            走通的那条路。只说「别读了」会把模型逼回逐行磨。
            """
            page = _skill_attach_page(tc.name, tc.arguments)
            if not page:
                return None
            skill, path, key = page
            pages = attach_pages.setdefault((skill, path), {})
            label = f"技能 `{skill}` 的附件 `{path}`"
            if key in pages:
                return None                        # 同一窗口重取：归参数级那两条闸管
            # 整段落在还看得见的旧窗口里 → 不再重发（2026-10-19 run 8f43a2e43fde：
            # `aesthetic-rules.md` 按 1:60 / 61:60 / 121:70 重取刚给过的 1–190 行）。
            # 「整份一次取回」这条出路照样受它约束——已经给过且还看得见，就不必再给；
            # 早先那份被折叠收走了，`visible_ids` 里没有它，整份重取照常放行。
            if _page_covered(pages, key, visible_ids):
                return ToolError(
                    tc.name,
                    f"Error: {label}这一页（{_page_label(key)}）本 run 已经给过，"
                    f"回执还在更早的上下文里——这次不再重复发送，"
                    f"重读同一段不会带来新信息。\n"
                    f"本 run 已取过的窗口：{'、'.join(_page_label(p) for p in pages)}。\n"
                    f"要往下做就直接用已经读到的内容（该交计划就交计划）；"
                    f"只缺某一节，按分段目录用 load_skill(name, section=键名) 单取那一块；"
                    f"早先那份真被折叠收走了，就用一个**更宽**的窗口一次取回。",
                    counts_as_failure=False)
            if key == "1:whole":
                return None                        # 整份取：不计数（它就是硬线之后的出路）
            if len(pages) < _MAX_ATTACH_PAGES_HARD:
                pages[key] = str(tc.id)
                if len(pages) == _MAX_ATTACH_PAGES + 1:
                    # 只说越过软线的那一次，理由同 `_shot_card_gate`。
                    over_pages.append(
                        f"{label}这一轮已经按 {len(pages)} 个不同的行窗口取过"
                        f"（超过 {_MAX_ATTACH_PAGES} 个）。这些窗口合起来基本就是整份文件，"
                        f"**再换一个起始行不会带来新信息**——**整份一次取回**"
                        f"（不带 offset/limit）不受这条限制，也是最快的一条路；"
                        f"或者直接用已经读到的内容往下走。"
                        f"到 {_MAX_ATTACH_PAGES_HARD} 个窗口就不再执行了。")
                return None
            n = ctx.extras["_attach_refusals"] = \
                ctx.extras.get("_attach_refusals", 0) + 1
            covered = "、".join(_page_label(p) for p in pages)
            return ToolError(
                tc.name,
                f"Error: {label}这一轮已经按 {len(pages)} 个不同的行窗口取过（{covered}），"
                f"到上限了（一份附件最多 {_MAX_ATTACH_PAGES} 个窗口，"
                f"硬线 {_MAX_ATTACH_PAGES_HARD} 个），这次不再执行。"
                f"这些窗口合起来基本就是整份文件——**再换一个起始行不会带来新信息**。"
                f"直接**整份一次取回**（不带 offset/limit）不受这条限制；"
                f"或者用已经读到的内容往下走（该交计划就交计划）。",
                counts_as_failure=n > _MAX_REFUSALS_BEFORE_FAILURE)

        def _skill_read_gate(tc: ToolCall) -> ToolError | None:
            """整 run 的 load_skill 总量：软线提醒、硬线拦（见 `_MAX_SKILL_READS`）。

            这是第四维，前三条按**一份文件**数的闸一条都接不到：
            正文读完再逐份读附件，每一份都不超自己的窗口上限。真机
            2026-10-19 run `8f43a2e43fde` 就这样读了 29 枪、≈190 KB，随后在同一段
            上打转，整条 run 按「原地打转」停下——用户看到的是「又卡死了」。

            记账放在**放行这一刻**（与 `_shot_card_gate` 同一理由：一批里的几枪并发，
            跑完再记会让同批每一枪都以为自己是最后一枪）。被前面三条闸拦下的枪走不到
            这里，不计入总量。

            `section=` 单取**永不拦**：它是硬线之后唯一该走的路，也是这条闸不能
            堵死的那条缝（按块取一次几百到几 KB，不会再把整份技能重发一遍）。
            """
            nonlocal skill_reads
            if tc.name != "load_skill":
                return None
            args = _call_args(tc.arguments)
            if args is None:
                return None                        # 认不出的参数：放行，不当闸
            if str(args.get("section") or "").strip():
                return None
            if skill_reads >= _MAX_SKILL_READS_HARD:
                n = ctx.extras["_skill_read_refusals"] = \
                    ctx.extras.get("_skill_read_refusals", 0) + 1
                return ToolError(
                    tc.name,
                    f"Error: 本 run 已经用 load_skill 取了 {skill_reads} 次技能材料，"
                    f"到上限了（{_MAX_SKILL_READS} 次后只提醒，"
                    f"{_MAX_SKILL_READS_HARD} 次起不再执行），这次不执行。"
                    f"再翻下去不会带来新信息，只会烧钱：现在用已经读到的内容往下做——"
                    f"调用下一步工具（{action_hint}；规划轮就交计划）。"
                    f"确实缺某一节，用 load_skill(name, section=键名) 只取那一块。",
                    counts_as_failure=n > _MAX_REFUSALS_BEFORE_FAILURE)
            skill_reads += 1
            ctx.extras["_skill_reads"] = skill_reads
            if skill_reads == _MAX_SKILL_READS + 1:
                # 只说越过软线的那一次（与 `over_pages` 同一条通道、同一理由）。
                over_reads.append(
                    f"本 run 已经用 load_skill 取了 {skill_reads} 次技能材料。"
                    f"读得够不够，不看还能翻多少页，而看手上这些能不能开工——"
                    f"**现在应当停读、去动作**：调用下一步工具（{action_hint}；"
                    f"规划轮就交计划）。确实还缺某一节，用 "
                    f"load_skill(name, section=键名) 只取那一块；"
                    f"整份/整页重读不会带来新信息。"
                    f"到 {_MAX_SKILL_READS_HARD} 次 load_skill 起不再执行。")
            return None

        def _skill_byte_gate(tc: ToolCall) -> ToolError | None:
            """整 run 阅读**字节**硬线（见 `_MAX_SKILL_BYTES`）。

            与上面那条枪数闸互补：那条按次数、这条按体量——27 枪可以是 27 KB，
            也可以是 170 KB，而撑爆上下文的是后者。只拦「还没读过的新材料」：
            同参重取（折叠之后的恢复路径）不受约束，否则上下文一折叠就无路可走。
            """
            if tc.name != "load_skill":
                return None
            total = int(ctx.extras.get("_skill_bytes", 0) or 0)
            if total < _MAX_SKILL_BYTES_HARD:
                return None
            if _repeat_key(tc.name, tc.arguments) in ctx.extras.get("_skill_seen", {}):
                return None                    # 读过的那份：重取是恢复，不是新开销
            n = ctx.extras["_skill_byte_refusals"] = \
                ctx.extras.get("_skill_byte_refusals", 0) + 1
            return ToolError(
                tc.name,
                f"Error: 本 run 已经从技能里取了约 {total // 1000} KB 材料，到上限了"
                f"（软线 {_MAX_SKILL_BYTES // 1000} KB、硬线 "
                f"{_MAX_SKILL_BYTES_HARD // 1000} KB），这次不再放新的。"
                f"再翻下去只会把上下文撑爆——真机就是在这个位置被供应商掐断连接。"
                f"**如果你还差某一节，改用 `section=名字` 只取那一段**"
                f"（比按字节翻整本省得多；分段目录在整份加载时给过）。"
                f"否则用已经读到的内容往下做：调用下一步工具（{action_hint}；"
                f"规划轮就交计划）。**已经读过的那份重取不受这条限制。**",
                counts_as_failure=n > _MAX_REFUSALS_BEFORE_FAILURE)

        def _preview_gate(tc: ToolCall) -> ToolError | None:
            """交付前的预览硬线：到上限就不再渲静帧，把模型推向交付。

            为什么这条闸是必要的：写手每轮改几个数值，参数就变了，`_stuck_rounds`
            的「重复调用」判据一次都不会响（真机 2026-10-19 run 32cd75d71fee：到迭代
            27 还在调花瓣，一镜都没交出去）。预览本身不产出片子；超过几次之后它只是
            把同一个审美判断反复问一遍。硬线**不拦交付**：它只关预览，模型仍然可以
            把这一镜交给片子、出片。
            """
            if tc.name != "preview_shot":
                return None
            if previews_since_delivery < _PREVIEW_HARD:
                return None
            n = ctx.extras["_preview_refusals"] = \
                ctx.extras.get("_preview_refusals", 0) + 1
            return ToolError(
                tc.name,
                f"Error: 上一次交付之后已经预览了 {previews_since_delivery} 次，"
                f"够多了（软线 {_PREVIEW_SOFT} 次、硬线 {_PREVIEW_HARD} 次），"
                "这次不再渲静帧。**预览是自查，不是交付**：画面已经过闸①，"
                "现在把已经做好的这一镜交出去——零素材片子用 `reshoot_shot` 改一镜、"
                "`render_short_film` 重渲整片；剪辑片子用 `render_video` 出片。"
                "继续改草稿文件不会让片子开始渲染。",
                counts_as_failure=n > _MAX_REFUSALS_BEFORE_FAILURE)

        def _note_repeat(tc: ToolCall, result: Any) -> None:
            """跑完记账：结果字节和上次一样就把「无进展」连续数加一，不一样就归一。

            真失败（``counts_as_failure=True``）不入这本账：那条路归连续失败守卫管——
            它按工具名计次（换参数也照数）、有自己的「已连续失败」话术与上限。两本账
            各数各的，否则同一次失败被两道闸重复定性，回执里的数字就对不上事实。
            """
            if is_tool_error(result) and result.counts_as_failure:
                return
            key = _repeat_key(tc.name, tc.arguments)
            digest = _digest(result)
            st = repeats.setdefault(key, {"n": 0, "same": 0, "last": "", "refused": 0})
            st["n"] += 1
            st["same"] = st["same"] + 1 if digest == st["last"] else 1
            st["last"] = digest
            # 记下这一枪的调用 id：下回同参再来时，要靠它判断「上次那份内容还在不在
            # 上下文里」——被折叠闸收走之后重取是恢复路径，不该按重复拦（见 _blocked_repeat）。
            st["last_id"] = str(tc.id)
            # 技能材料的**新内容**记账（同参重取不计）：见 `_MAX_SKILL_BYTES`。
            if tc.name == "load_skill" and not is_tool_error(result):
                seen = ctx.extras.setdefault("_skill_seen", {})
                if key not in seen:
                    seen[key] = digest
                    size = len(str(result).encode("utf-8"))
                    ctx.extras["_skill_bytes"] = \
                        int(ctx.extras.get("_skill_bytes", 0) or 0) + size
                    total = ctx.extras["_skill_bytes"]
                    if (total >= _MAX_SKILL_BYTES
                            and ctx.extras.get("_skill_bytes_noticed") is None):
                        ctx.extras["_skill_bytes_noticed"] = total
                        over_reads.append(
                            f"本 run 已经从技能里取了约 {total // 1000} KB 材料"
                            f"（软线 {_MAX_SKILL_BYTES // 1000} KB）。"
                            f"读得够不够，不看还能翻多少，而看手上这些能不能开工——"
                            f"**现在应当停读、去动作**：调用下一步工具（{action_hint}；"
                            f"规划轮就交计划）。到 "
                            f"{_MAX_SKILL_BYTES_HARD // 1000} KB 起不再放新材料。")
            # 给 `_blocked_repeat` 判「幂等写」用：上一次到底是成功还是报错。
            st["last_error"] = is_tool_error(result)
            st["refused"] = 0
            # 写文件不在 `_blocked_repeat` 里拦（幂等，见那里），检索类也不拦
            # （`_REPEAT_EXEMPT_TOOLS`），所以都不能放这句「下一次不会再执行」——
            # 说了却不兑现，模型会以为闸坏了。
            if (st["same"] == _MAX_SAME_RESULT_REPEATS
                    and tc.name not in _WRITE_TOOLS
                    and tc.name not in _REPEAT_EXEMPT_TOOLS):
                repeat_warnings.append(
                    f"「{tc.name}」最近 {st['same']} 次调用参数与返回结果逐字相同。"
                    f"下一次同样的参数不会再执行——现在就换参数、换工具，或如实说明卡点。")
            # 写/改同一份内容的第 2、5 次：不只在内部记「无进展」，还要用**系统消息**
            # 把交付路径说一遍。真机 2026-10-19 run `e2ec9a6969ec`：模型在草稿文件上
            # 来回 20 多轮，`preview_shot` 回执里的 `next` 它没有采信；系统消息是它
            # 更会照做的通道。第 5 次之后不再提醒，交给末尾的无进展守卫（6 轮）停。
            if tc.name in _REPEAT_TOOLS and st["n"] in (2, 5):
                repeat_warnings.append(
                    f"同一份文件内容已经被写/改到第 {st['n']} 次，画面不会因此有任何变化。"
                    "文件只是草稿，不是交付物：停下手上的重复编辑，把已经做好的内容交给"
                    "**流程的下一步工具**——零素材片子就是 `reshoot_shot`（改一镜）或"
                    "`render_short_film`（重渲整片），剪辑片子就是 `render_video`。"
                    "这一步不做，片子永远不会开始渲染。")

        async def _exec_one(tc: ToolCall) -> Any:
            if tc.id in done_ids:
                # 崩溃前已经跑完并落了结果：不再执行，也不再记一次。
                return f"[已执行过] 这一步在中断前已完成，直接沿用当时的结果（call_id={tc.id}）。"
            await self.hooks.before_tool_call(ctx, tc.name, tc.arguments, call_id=tc.id)
            t0 = time.monotonic()
            if fail_counts.get(tc.name, 0) >= _MAX_CONSECUTIVE_FAILURES:
                # 撞上限还重试：不再打后端。一次规划轮里「猜键名读产物」能连着错六十次，
                # 光靠提示词里的「请勿重试」拦不住（真机实测就是这么烧穿迭代预算的）。
                r = _refused(tc, fail_counts[tc.name])
                await self.hooks.after_tool_call(
                    ctx, tc.name, tc.arguments, None, 0.0, error=r, call_id=tc.id)
                return r
            # 先过三道家族级闸（读卡上限、正文窗口上限、附件窗口上限），再过参数级那两条：
            # 一次规划轮里读 45 张**不同的**卡、或同一份正文取 19 个**重叠的**窗口时，
            # 参数级判据一次都不会响（每份参数都是新的），必须由这一层拦。
            blocked = (_shot_card_gate(tc) or _skill_body_gate(tc)
                       or _attach_gate(tc) or _blocked_repeat(tc)
                       # 总量闸排最后：被前面三条拦下的枪没真读，不该计入「本 run 读了
                       # 几次」——否则打回几次就把整份阅读预算吃掉了。
                       or _skill_read_gate(tc)
                       # 体量闸：枪数够少但每枪都很大时，只有这一条接得住。
                       or _skill_byte_gate(tc)
                       # 预览硬线：只关预览，不关交付（见 `_preview_gate`）。
                       or _preview_gate(tc))
            if blocked is not None:
                # 熔断不是「工具坏了」：默认 counts_as_failure=False，别把「换个思路再来」
                # 再压一层失败计数（第二次起才升级，见 _blocked_repeat）。
                repeats_key = _repeat_key(tc.name, tc.arguments)
                st = repeats.setdefault(
                    repeats_key, {"n": 0, "same": 0, "last": "", "refused": 0})
                if str(tc.id) not in dup_ids:
                    st["refused"] += 1
                await self.hooks.after_tool_call(
                    ctx, tc.name, tc.arguments, None, 0.0, error=blocked, call_id=tc.id)
                return blocked
            try:
                call_timeout = _TOOL_CALL_TIMEOUTS.get(tc.name, _TOOL_CALL_TIMEOUT)
                if cp_mgr is not None and cp_run_id:
                    try:
                        await cp_mgr.record_tool_call(
                            cp_run_id, call_id=tc.id, name=tc.name,
                            arguments=tc.arguments,
                            iteration=int(getattr(ctx, "iteration", 0) or 0))
                    except Exception:  # noqa: BLE001  恢复点写失败不该拦工具
                        logger.warning("写 tool_call 恢复点失败", exc_info=True)
                r = await asyncio.wait_for(
                    reg.execute(tc.name, tc.arguments), timeout=call_timeout)
            except TimeoutError:
                r = ToolError(
                    tc.name,
                    f"Error: 工具「{tc.name}」执行超时（{call_timeout:.0f}s）——"
                    f"可能是下游服务卡死或 I/O 阻塞。请换参数或换工具，"
                    f"或告诉用户卡在哪、给个方案。")
            except ToolError as e:  # 已经定性过的失败，原样回喂
                r = e
            except Exception as e:  # noqa: BLE001 - Registry 之外再兜一层，循环不该炸
                r = ToolError(tc.name, f"Error executing {tc.name}: {e}")
            err = r if is_tool_error(r) else None
            if cp_mgr is not None and cp_run_id:
                try:
                    await cp_mgr.record_tool_result(
                        cp_run_id, call_id=tc.id, name=tc.name,
                        status="error" if err else "ok",
                        iteration=int(getattr(ctx, "iteration", 0) or 0))
                except Exception:  # noqa: BLE001
                    logger.warning("写 tool_result 恢复点失败", exc_info=True)
            # 「这一页已经给过」只在正文**真发出去**之后才算数。正文窗口闸得在调用前
            # 记账（它要先判这一枪执不执行），两种结果必须当场撤销：
            # ① 调用出错（越界等）——一页都没出去；
            # ② 大正文的整份加载只回索引——回执里写明了「正文没有随附」
            #    （run a1f66c02f3c9：把「只给了目录」记成「1 行起整份已给过」，
            #    之后每次按行取正文都被判成重复，模型连打 6 枪同一调用，整条 run
            #    按原地打转停成 failed）。
            if tc.name == "load_skill":
                page = _skill_body_page(tc.name, tc.arguments)
                if page is not None and (
                        err is not None
                        or (page[1] == "1:whole" and INDEX_ONLY_NOTE in str(r))):
                    body_pages.get(page[0], {}).pop(page[1], None)
            _note_repeat(tc, r)
            # 预览/交付计数：见 `_PREVIEW_SOFT` / `_PREVIEW_HARD`。只在**真跑成功**时
            # 记账；被硬线拦下的预览不算（它一帧都没渲），否则计数会自己把自己推高。
            nonlocal previews_since_delivery
            if err is None and tc.name in _DELIVERY_TOOLS:
                previews_since_delivery = 0
                ctx.extras["_previews_since_delivery"] = 0
            elif err is None and tc.name == "preview_shot":
                previews_since_delivery += 1
                ctx.extras["_previews_since_delivery"] = previews_since_delivery
                if previews_since_delivery == _PREVIEW_SOFT:
                    # 软线只说一次。真机里 `preview_shot` 回执末尾的 `next` 模型看见了
                    # 也不照做（run 32cd75d71fee）；system 消息是它更会采信的通道。
                    repeat_warnings.append(
                        f"这一镜已经连续预览了 {previews_since_delivery} 次。预览是"
                        "**自查，不是交付**：画面已经过闸①，再改几个像素也不会让片子"
                        "开始渲染。现在把做好的这一镜交出去（零素材片子用 "
                        "`reshoot_shot` 改一镜、`render_short_film` 重渲整片；剪辑片子用 "
                        "`render_video` 出片）。再预览到 "
                        f"{_PREVIEW_HARD} 次，预览通道会先关上。")
            if err is None and isinstance(tc.arguments, dict) and "timeline" in tc.arguments:
                await self._persist_timeline(ctx, tc.arguments["timeline"])
            await self.hooks.after_tool_call(
                ctx, tc.name, tc.arguments, None if err else r,
                time.monotonic() - t0, error=err, call_id=tc.id
            )
            return r

        results: dict[str, object] = {}
        _uid = ctx.session.user_id
        for batch in plan_batches(tool_calls, lambda n: reg.get(n, user_id=_uid)):
            # 一批并发里任何一个工具抛错，都不能取消同批的其他工具（R6）。
            # `_exec_one` 内部已经兜了 Registry 与超时，这里再兜一层是为了
            # 钩子（before/after_tool_call）与闸函数本身抛错的情况——
            # 那些异常以前会让 gather 直接取消整批，同批工具连结果都拿不到。
            gathered = await asyncio.gather(
                *(_exec_one(tc) for tc in batch), return_exceptions=True)
            for tc, r in zip(batch, gathered):
                if isinstance(r, ToolError):
                    # `_exec_one` 把失败当**返回值**交给调用方（工具错误本来就是回喂给
                    # 模型的数据），而 ``return_exceptions=True`` 会把**抛出**的异常也包成
                    # 值——两者在这里同型。ToolError 值必须原样保留：重套一层会丢掉
                    # ``counts_as_failure=False``（反馈类打回立刻被算成真失败、两次就熔断），
                    # 还会让回执变成「Error executing X: Error: …」的双前缀，并给每一次
                    # 普通工具失败都打一条「未捕获异常」的栈（真机上就是一片假告警）。
                    results[tc.id] = r
                    continue
                if isinstance(r, BaseException):
                    logger.exception(
                        "工具 %s 抛出未捕获异常，按失败回执继续", tc.name, exc_info=r)
                    r = ToolError(tc.name, f"Error executing {tc.name}: {r}")
                results[tc.id] = r
        # 记下本 run 已执行过的节点：下一轮的 _expand_ready_siblings 据此判断
        # 「谁的产物已经有了」，从而决定还能并谁。
        done = ctx.extras.setdefault("_executed_nodes", set())
        done.update(tc.name for tc in tool_calls)

        # 按 LLM 给出的原始顺序回填，保证上下文一致性。
        # 守卫提示必须排在本批**全部**回执之后：assistant(tool_calls) 与它的逐条
        # tool 回执之间插任何别的角色，OpenAI 兼容口径直接 400（tool_calls 未收口）。
        nagging: list[tuple[str, int]] = []
        # 一批并发里同一个工具失败几条都只记**一次**：4 条并发取文件同时失败是「一次没取对」，
        # 不是「连续失败 4 次」——真机里正是这样把还没试过的那批正确路径一起熔断了。
        bumped: set[str] = set()
        feedback_counts: dict[str, int] = ctx.extras.setdefault("_feedback_counts", {})
        for tc in tool_calls:
            r = results[tc.id]
            # 「可执行的反馈」不算故障：submit_plan 被校验打回是让模型改输入，重交是
            # 正路。算进来的话，守卫两次就叫停，把「计划还没写对」升级成「工具坏了，
            # 去问用户怎么办」——真机实测的岔路就在这里。
            if is_tool_error(r):
                failure = r.counts_as_failure
                if failure and _is_actionable_feedback(r):
                    # 节点给了改法清单：按反馈处理，超过上限才升级成失败
                    feedback_counts[tc.name] = feedback_counts.get(tc.name, 0) + 1
                    failure = feedback_counts[tc.name] > _MAX_FEEDBACK_ROUNDS
                if failure and tc.name not in bumped:
                    bumped.add(tc.name)
                    fail_counts[tc.name] = fail_counts.get(tc.name, 0) + 1
                    if fail_counts[tc.name] >= 2 and tc.name not in {n for n, _ in nagging}:
                        nagging.append((tc.name, fail_counts[tc.name]))
                # 反馈类错误（counts_as_failure=False）既不加一也不清零：它说明的是
                # 「输入还没写对」，与这个工具先前有没有真跑坏无关。
            else:
                fail_counts[tc.name] = 0
            # 结构性并批补进来的调用不回填：它们不在模型的 assistant(tool_calls) 里，
            # 插一条 tool 消息会让 API 报 400。失败计数照记（上面已记），
            # 只是不往消息链里写——产物已在产物表里。
            if str(tc.id).startswith("auto-"):
                continue
            # 进链那一刻收口（单条上限）：结果**对象**不动，只截喂给模型的那份文本
            # ——`_follow_inflight_renders` 还要拿原对象去读 job 状态。
            ctx.messages.append(tool_result(
                tc.id, tc.name, self.tool_gate.text(tc.name, r)))
        for name, times in nagging:
            ctx.messages.append(system(
                f"⚠️ 工具「{name}」已连续失败 {times} 次。请立即停止原样重试该工具，"
                f"自己改走替代方案（换参数、换素材、换工具、跳过非关键步骤或降级），"
                f"并在答复里说明；确实无路可走时才向用户说明原因并给出建议方案。连续失败到 {_MAX_CONSECUTIVE_FAILURES} 次后该工具不再执行。"
            ))
        # 无进展重复的预告：闸已经攒到第二次，下一次就不真执行了。
        # 放在全部回执之后——理由同上面那批守卫提示（tool_calls 未收口时插别的角色直接 400）。
        for note in dict.fromkeys([*repeat_warnings, *over_cards, *over_pages,
                                   *over_reads]):
            ctx.messages.append(system(f"⚠️ {note}"))

        await self.hooks.after_execute_tools(ctx)
        return results

    async def _load_preview(self, session: Session) -> dict[str, Any] | None:
        """读本次会话已产出的剪辑产物，整理成「编排预览」。

        渲染前确认门用它把编排结果摆给用户看。取不到（没接存储 / 还没产物）返回 None，
        门照样拦——只是弹窗里少一块摘要，**不会因为读不到预览就放行渲染**。

        作用域键必须用 ``storyline_session_id``（``u:{user}:c:{conv}``）：
        剪辑产物存在 Storyline 那一侧，键由那个函数统一拼装。用 ``session.session_id``
        （``{user}:{conv}``）会查不到任何产物，表现是「预览永远为空」而没有任何报错。

        artifact_id 必须取**当前生效的作用域**而不是固定的 ``_default``：
        ``rerun_from`` 分叉会给子 run 生成 ``art-xxxx`` 作用域（checkpoint.fork），
        子 run 的产物写在自己的作用域里。写死 ``_default`` 的话，分叉后那张渲染确认卡
        显示的是父 run 的旧编排（或直接空），而用户以为看到的是这次的编排。
        """
        if self.storage is None:
            return None
        try:
            from .preview import build_preview
            ident = current_identity()
            sid = storyline_session_id(session.user_id, session.conversation_id)
            art = (ident.artifact_id if ident is not None else "") or ""
            arts = await self.storage.artifacts(sid, art).snapshot()
            return build_preview(arts)
        except Exception:  # noqa: BLE001 - 预览是增强信息，读失败不该阻断确认流程
            logger.exception("读取编排预览失败（渲染确认门继续，只是少一块摘要）")
            return None

    async def _load_motion_plan(self, session: Session, tool_calls: Sequence[Any],
                                node: str) -> Mapping[str, Any] | None:
        """图形科普片确认卡的账本：优先用 Store 里的分镜产物，其次用调用自带的手写 spec。

        读**这次调用**的参数而不是 Store 的那一份，是因为模型可以跳过分镜直接出片；
        两者都有时以参数为准——服务端用的就是它，摘要显示旧值就是骗人。
        顶层开关（用户在计划卡上改的画幅/帧率/音色）最后盖上去，理由同一条。

        读不到就回 None：门照样拦，只是弹窗少一块摘要（与 ``_load_preview`` 同口径）。
        """
        args: Mapping[str, Any] = {}
        for tc in tool_calls or ():
            if getattr(tc, "name", None) == node:
                candidate = getattr(tc, "arguments", None)
                if isinstance(candidate, Mapping):
                    args = candidate
                break
        source: dict[str, Any] | None = None
        stored: Mapping[str, Any] | None = None
        if self.storage is not None:
            try:
                ident = current_identity()
                sid = storyline_session_id(session.user_id, session.conversation_id)
                art = (ident.artifact_id if ident is not None else "") or ""
                row = (await self.storage.artifacts(sid, art)
                       .snapshot()).get("plan_motion")
                if isinstance(row, Mapping) and row:
                    stored = row
            except Exception:  # noqa: BLE001 - 摘要读失败不该拦住确认流程
                logger.exception("读取分镜产物失败（渲染确认门继续，只是少一块摘要）")
        spec = args.get("spec")
        if isinstance(spec, Mapping) and spec:
            source = dict(spec)
            # 手写 spec 那一路也带上分镜轮渲好的预览图（镜号对得上的才用）。
            if stored and isinstance(stored.get("preview_frames"), Mapping):
                source["preview_frames"] = dict(stored["preview_frames"])
        elif stored:
            source = dict(stored)
        if source is None:
            return None
        merged = {**source, **{k: v for k, v in args.items()
                                if k in MOTION_KNOBS and v not in (None, "")}}
        # 配乐走的是调用自带的 `bgm`（字符串 material_id / 曲名），不在 `MOTION_KNOBS` 里，
        # 而分镜产物里的 `bgm.ref` 只在分镜轮写过才有。2026-10-22 真机：模型在
        # `render_motion_code` 里传了 `bgm: mat-1518cc`，卡上却显示「纯人声（无配乐）」
        # ——用户以为曲子没挂上。这里把调用参数折成 `motion_summary` 读得懂的
        # `{ref: ...}` 再上卡；取不到字节仍由渲染那一步报错，不在这里判。
        bgm_arg = args.get("bgm")
        if isinstance(bgm_arg, str) and bgm_arg.strip():
            ref = bgm_arg.strip()
            # 素材号对用户没有信息量：卡上这行字是他按确认前唯一能核对「是不是这首」
            # 的地方，能查到文件名就显示文件名（查不到就照原样，不是错）。
            if ref.startswith("mat-") and self.storage is not None:
                try:
                    row = await self.storage.materials.get(ref)
                except Exception:  # noqa: BLE001 - 查不到名字不该拦住确认
                    row = None
                name = str((row or {}).get("filename") or "").strip()
                if name:
                    ref = name
            prev = merged.get("bgm") if isinstance(merged.get("bgm"), Mapping) else {}
            merged["bgm"] = {**prev, "ref": ref}
        return merged

    async def _sign_thumbs(self, ask: dict[str, Any]) -> None:
        """确认卡分镜行的预览图：对象键 → 现签 URL（签不出就不带图，卡照样弹）。"""
        objects = getattr(self.storage, "objects", None)
        if objects is None:
            return
        for row in ask.get("storyboard") or []:
            key = row.pop("thumb_key", None)
            if not key:
                continue
            try:
                row["thumb"] = await objects.presign_get(key, ttl_sec=6 * 3600)
            except Exception:  # noqa: BLE001 - 缺一张预览不拦确认
                logger.debug("预览图签名失败：%s", key)

    def _motion_value_labels(self, node: str = MOTION_NODE) -> dict[str, dict[str, str]]:
        """确认卡回显开关取值时用的「人话」表：``{开关名: {取值: 标签}}``。

        为什么从词表读而不是在主服务再抄一份对照表：这份标签的源在剪辑服务端的参数
        声明里，随 DAG 契约一起带回来（``props_for`` 已经把契约的 params 并进工具声明）。
        抄一份就是第三份名单，迟早和源不一致——而它错的后果是用户看见两张卡说两种话。
        读不到（没连剪辑服务端、契约还没填）就回空表，确认卡退回它自带的小表。
        """
        gate = self.plan_gate
        vocab = getattr(gate, "vocab", None)
        if vocab is None:
            return {}
        try:
            props = vocab.props_for(node) or {}
        except Exception:  # noqa: BLE001 - 标签只是好看，读不到不该拦住确认流程
            return {}
        out: dict[str, dict[str, str]] = {}
        for key, spec in props.items():
            labels = spec.get("value_labels") if isinstance(spec, Mapping) else None
            if isinstance(labels, Mapping) and labels:
                out[str(key)] = {str(k): str(v) for k, v in labels.items()}
        return out

    def _take_asked_question(
        self, reg: ToolRegistry, tool_calls: Sequence[Any],
    ) -> dict[str, Any] | None:
        """本轮有没有 ``ask_user`` 调用；有就把它整理好的问题取出来。

        取一次就清掉：同一道问题不该在续跑后又被当成新问题弹一遍。
        """
        for tc in tool_calls:
            if tc.name != "ask_user":
                continue
            tool = reg.get("ask_user")
            question = getattr(tool, "last_question", None)
            if question:
                tool.last_question = None
                return dict(question)
        return None

    async def _follow_inflight_renders(
        self, ctx: AgentHookContext, calls: Sequence[tuple[str, Any]],
        registry: ToolRegistry | None = None,
    ) -> None:
        """渲染未达终态不许收尾：模型拿到 queued/running 就收尾时，由本循环自己轮到 done/failed。

        为什么在循环里做而不是靠提示词：成片卡片的两处出口（``MediaCardHook`` 的回投与
        ``record_rendered_media`` 的 qa.parts 落库）都只认**工具结果里出现的 media_url**，
        而渲染改成提交+轮询之后，「查到 done 才收尾」变成对模型情绪的依赖（README §6 记着
        这条代价）。这里把同一件事改成硬保证：轮询仍走真实工具调用（回投、进度、历史落库
        与前几轮的工具帧完全同形），只是发起方从模型换成循环本身。

        预算用尽仍没跑完时**不假称成功**：追加一条 system 说明渲染仍在进行，让模型如实
        告知用户稍后用 render_status 追，而不是回一句看不出破绽的收尾。
        """
        reg = registry if registry is not None else self.registry
        poll_name = next((n for n in reg.tool_names
                          if n == "render_status" or n.endswith("render_status")), "")
        if not poll_name:
            return                      # 无剪辑装配：没有可轮的对象，行为与改动前一致
        pending: list[str] = []
        for _, result in calls:
            art = _inflight_render_id(result)
            if art is not None and art not in pending:
                pending.append(art)
        if not pending:
            return

        deadline = time.monotonic() + max(0.0, self.config.render_follow_max_sec)
        unresolved: list[str] = []
        n_poll = 0
        last_beat = time.monotonic()
        cp = ctx.extras.get("checkpoint")
        while pending:
            art = pending.pop(0)
            if time.monotonic() >= deadline:
                unresolved.append(art)
                continue
            await asyncio.sleep(max(0.1, self.config.render_poll_sec))
            n_poll += 1
            # 心跳：一次长渲染代查可以持续几十分钟，而**租约只有 10 分钟**
            # （CheckpointManager.lease_sec）——不在这里续租，多副本部署下这条 run
            # 会被另一个实例判定为「无主/租约过期」而抢走，同一部片子被渲两遍。
            # 顺带把当前消息链落一次盘：中途被 kill 时恢复点不至于退回到代查之前。
            now = time.monotonic()
            if cp is not None and (now - last_beat) >= _RENDER_BEAT_SEC:
                last_beat = now
                try:
                    await self.checkpoint.save_progress(
                        cp, iteration=ctx.iteration, messages=ctx.messages)
                except Exception:  # noqa: BLE001 - 续租失败不该炸掉整轮执行
                    pass
            tc = ToolCall(id=f"render_follow_{n_poll}", name=poll_name,
                          arguments={"artifact_id": art})
            # 中间态只发进度帧、不进上下文：一次长渲染按秒级轮询，把每次「还在跑」都
            # 拼进 messages 会撑爆上下文，也只会让模型复读同一句话。
            await self.hooks.before_tool_call(ctx, tc.name, tc.arguments, call_id=tc.id)
            t0 = time.monotonic()
            try:
                # 必须打在本轮**实际**用的注册表上：poll_name 是从 reg 里挑出来的，
                # 而规划轮/子 Agent 用的是一张过滤过的注册表。打到 self.registry 上，
                # 子 Agent 与团队路径的「渲染未达终态不许收尾」会静默失效。
                # 超时兜底同 _execute_tool_calls：轮询挂死不能连整条分区一起挂住。
                r = await asyncio.wait_for(
                    reg.execute(tc.name, tc.arguments), timeout=_TOOL_CALL_TIMEOUT)
            except ToolError as e:
                r = e
            except asyncio.TimeoutError:
                r = ToolError(tc.name, f"Error executing {tc.name}: 超时"
                                       f"（{_TOOL_CALL_TIMEOUT:g}s 未返回）")
            except Exception as e:  # noqa: BLE001 - 轮询失败不该炸掉整轮执行
                r = ToolError(tc.name, f"Error executing {tc.name}: {e}")
            err = r if is_tool_error(r) else None
            await self.hooks.after_tool_call(
                ctx, tc.name, tc.arguments, None if err else r,
                time.monotonic() - t0, error=err, call_id=tc.id)
            again = _inflight_render_id(r)
            if again is not None:
                pending.append(again)   # 仍在跑：下一轮接着查这一片
                continue
            # 终态才进上下文：MediaCardHook 只认 messages 里的工具结果，成片卡片与
            # qa.parts 的持久链接都在这一步落地（与模型自己调 render_status 时同形）。
            ctx.messages.append(assistant(None, [tc]))
            ctx.messages.append(tool_result(tc.id, tc.name, r))
            await self.hooks.after_execute_tools(ctx)
        if unresolved:
            ctx.messages.append(system(
                f"⏳ 渲染在预算的 {self.config.render_follow_max_sec:g} 秒内仍未达终态"
                f"（artifact_id={', '.join(unresolved)}）。"
                f"不要声称成片已完成：如实告诉用户渲染仍在进行，"
                f"可稍后用 render_status 查这条 artifact_id。"))


def _inflight_render_id(result: Any) -> str | None:
    """工具结果 → 在途渲染的 artifact_id；不是「未达终态的渲染视图」就回 None。

    判据只认 ``render.status`` 为 queued/running 的形状（``render_jobs.tool_view`` 的
    非终态输出）：done/failed 带的是 output 终态产物，此时回 None，调用方按收尾处理。
    """
    if isinstance(result, dict):
        view = result
    elif isinstance(result, str):
        try:
            view = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        return None
    if not isinstance(view, dict):
        return None
    render = view.get("render")
    if not isinstance(render, dict):
        return None
    if str(render.get("status") or "") not in ("queued", "running"):
        return None
    art = view.get("artifact_id")
    return str(art) if art else "_default"


class Agent:
    """外层常驻循环：调度 AgentOnceRun，并隔离各 Session 的执行。

    同一 Session 串行、不同 Session 并行的顺序保证，在生产环境由 MQ 的
    session_id → Partition 路由提供；此处用 per-session 锁近似该语义。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        session_manager: SessionManager | None = None,
        context_builder: ContextBuilder | None = None,
        hooks: AgentHook | None = None,
        config: AgentConfig | None = None,
        checkpoint: CheckpointManager | None = None,
        storage: Any | None = None,
        plan_gate: Any | None = None,
        skill_loader: Any | None = None,
        memory_store: Any | None = None,
    ) -> None:
        # 注意：SessionManager 定义了 __len__，空实例是 falsy，必须用 is None 判断。
        self.session_manager = (
            session_manager if session_manager is not None else SessionManager(storage)
        )
        self.runner = AgentOnceRun(
            llm, registry, context_builder, hooks, config, checkpoint,
            storage=storage, plan_gate=plan_gate
        )
        self.storage = storage
        # 计划门（块 B）：没接上就是普通服务，plan() 如实退回 handle()。
        self.plan_gate = plan_gate
        self.skill_loader = skill_loader
        # 复盘进记忆（E）：没接记忆存储就没有落点，本功能如实不启用。
        self.retro = (PlanRetrospective(memory_store)
                      if memory_store is not None else None)
        self._planning: ToolRegistry | None = None
        self._planning_sig: tuple[str, ...] | None = None
        # 执行轮按计划裁过的工具面（T41）与它的签名缓存，见 _round_registry。
        self._round: ToolRegistry | None = None
        self._round_sig: tuple[Any, ...] | None = None
        # 每个 session 一把锁：同一会话串行、异会话并行。生产那一层由 MQ 的
        # key=session_id 分区语义保证（见 mq.py），这把锁是同一语义的兜底。
        self._locks: dict[str, asyncio.Lock] = {}

    def _planning_registry(self, gate: Any) -> ToolRegistry:
        """规划轮注册表：按主注册表的名字清单当签名缓存，外部工具接入后自动换一张。

        MCP/Storyline 的工具要到 ``_startup`` 才注册进来，「启动后第一句话」和
        「接入之后」的规划轮工具集本来就不该是同一份——签名一变了就重建，
        否则会出现「节点已接入、规划轮里却查不到」。
        """
        signature = tuple(self.runner.registry.tool_names)
        if self._planning is None or self._planning_sig != signature:
            self._planning = gate.planning_registry(
                submit_tool=SubmitPlanTool(gate),
                confirm_tool=ConfirmPlanTool())
            self._planning_sig = signature
            # submit_plan 只活在这张独立注册表里，主注册表那次同步看不见它：
            # 不并进来，出口替换器换不出中文，界面上就是一句机器名。
            get_catalog().update(self._planning.displays())
        return self._planning

    def _resume_registry(self, cp: Any) -> ToolRegistry | None:
        """续跑这条快照时该用哪张注册表：规划轮那张 / 执行轮按计划裁过的那张 / 全量。

        ``None`` = 用全量注册表（普通轮、以及没接计划门时的所有轮次）。
        为什么要在**这里**，而不是让 ``Runner`` 自己判：注册表是从计划门现建的，
        Runner 手里没有门；轮次身份在快照里，Agent 两边都够得着。

        T41 之后执行轮也有一张自己的表（按计划裁的），所以续跑要按**同一条口径**取回来：
        在渲染确认门/弹窗上停过一次之后再接着跑，如果回退成全量注册表，那个 run 前后
        两半的工具面就不是同一张——账面记的 ``sent`` 与模型手里真有的东西会对不上，
        而且「被裁的节点第二半又能调了」这件事没有任何地方说得清。计划本体从
        ``RunState`` 里取（与 ``approved_plan`` 同一份，不另存一份步骤清单）。
        """
        gate = self.plan_gate
        if _planning_run(cp):
            if gate is None or not gate.whitelist():
                # 没有可规划的节点 = ``plan()`` 当初直接退回 ``handle`` 的那种进程状态：
                # 这轮本来就不是规划轮，别硬塞一张没有剪辑节点的表。
                return None
            return self._planning_registry(gate)
        plan = RunState.restore(cp).approved_plan or {}
        steps = plan.get("steps") or []
        if not steps:
            return None
        return self._round_registry(steps)

    def _round_registry(self, plan_steps: Iterable[Any]) -> ToolRegistry:
        """执行轮的工具面：这张计划的节点 + 通用件（T41，§5.7 第⑥条）。

        **签名必须含工具子集身份**：主注册表的名字清单只说明「有哪些工具可用」，
        说明不了「这一轮裁出了哪几张」——两张不同的计划卡前后脚执行时，只按主注册表
        当签名会命中同一份缓存，第二条计划就会拿到第一条的工具面（它需要的那几个节点
        不在里面，模型只能撞上去才知道）。缓存键因此是「主注册表清单 + 计划节点集合」
        两样一起，缺一样都是假命中（判据见 tests/test_context_hygiene 的 T41-②）。

        工具实例与主注册表共享（不复制、不重建工具本身）：裁剪只影响**发给模型的
        schema**，不影响执行、不影响并发分批、不影响出口的中文名。
        """
        signature = (tuple(self.runner.registry.tool_names),
                     tuple(sorted(plan_nodes(plan_steps))))
        if self._round is None or self._round_sig != signature:
            self._round = registry_for_round(self.runner.registry, plan_steps)
            self._round_sig = signature
        return self._round

    def _scope_section(self, plan_steps: Iterable[Any]) -> list[Section]:
        """「如何请求扩面」那一小节（T41-②）：被裁的节点名与怎么要回它们。

        它是**执行轮专属**的一段（计划卡不同、被裁的节点就不同），所以走
        ``extra_sections`` 而不是常驻 ContextSource——常驻的话每一轮都要重算一遍，
        而且「本轮裁了谁」这个事实只有装配这一侧知道。

        空表（没裁任何节点）就整节不发：写一段「你可以请求扩面」会让模型以为工具被裁了。
        """
        lines = scope_lines(trimmed_nodes(self.runner.registry, plan_steps))
        if not lines:
            return []
        return [Section(name=SCOPE_SECTION,
                        text=f"<{SCOPE_SECTION}>\n" + "\n".join(lines)
                             + f"\n</{SCOPE_SECTION}>",
                        kind="conditional", condition_key=SCOPE_CONDITION_KEY,
                        # 这一段的读者是「发现自己缺工具」的那一步：要回来只有两条路
                        # ——问用户（ask_user）或者取技能（load_skill）看还有没有别的走法。
                        referenced_by=("ask_user", "load_skill"))]

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def handle(
        self, user_id: str, conversation_id: str, message: str, *, run_id: str | None = None,
        stream: bool = False, attachments: Sequence[str] = (), resume: bool = False,
        interactive: bool = True, style_skill: str = "",
        go_straight: bool = False,
    ) -> str:
        """直接处理一条消息并返回结果（同步语义，便于调用与测试）。

        ``resume=True`` 才续跑该会话最近一个未完成的 run；默认不劫持——带进来的
        是一条新消息，就按新消息开新 run。「这条消息是不是『继续』」是入口处的
        意图判断（consumer / server），不是一个未落盘的快照该替用户决定的事。

        ``interactive=False`` 表示**没有活人在等**（到点自动跑的定时任务）。
        凡是「拦下来问用户」的门都必须跳过：否则这条 run 会永久挂在等确认上——
        没有人来点，而浏览器身份也过不了它的归属校验（{user_id} 是 "cron"）。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        async with self._lock_for(session.session_id):
            if resume and self.runner.checkpoint is not None:
                cp = await self.runner.checkpoint.pending_for_session(session.session_id)
                if cp is not None:
                    return await self.runner.resume(
                        cp, session, stream=stream,
                        registry=self._resume_registry(cp),
                        style_skill=style_skill,
                        # 既是「继续」也可以顺手把开关带上：用户上一条是被这道门拦下的，
                        # 这次在入口上勾了直接出片，续跑就不该再问。
                        go_straight=go_straight)
            sections: list[Section] = []
            return await self.runner.run(
                session, message, run_id=run_id, stream=stream,
                attachments=attachments, interactive=interactive,
                extra_sections=sections,
                style_skill=style_skill,
                go_straight=go_straight,
            )

    async def plan(
        self, user_id: str, conversation_id: str, message: str, *,
        run_id: str | None = None, stream: bool = False,
        attachments: Sequence[str] = (), feedback: str = "",
        revise_of: str = "", style_skill: str = "",
    ) -> str:
        """Run A 规划轮：注册表里物理不含剪辑节点，出口只有 ``submit_plan`` 或直接回答。

        没接计划门、或当前进程一个剪辑节点都没有（Storyline 未连通）时退回 ``handle``——
        没有可规划的节点，硬走规划轮只会白拦一道。

        ``revise_of`` 是「换一版」指回来的那条旧规划 run：卡面步骤只活在旧 run 的指针行里
        （会话历史中只有那一轮的文字摘要），不带上它，新一轮既无从做出「与旧卡可辨别的
        差异」，也往往干脆口头描述另一版而不再出卡。
        """
        # 「换一版」的原话是一句直接偏好，先落进记忆再重出卡（无计划门时也一样落）。
        await self._record_retro([revise_line(feedback)],
                                 user_id=user_id, conversation_id=conversation_id)
        gate = self.plan_gate
        if gate is None or not gate.whitelist():
            return await self.handle(user_id, conversation_id, message, run_id=run_id,
                                     stream=stream, attachments=attachments,
                                     style_skill=style_skill)
        prior = await self._prior_plans(revise_of)
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        # 续跑依据：上一条确认过的计划有没有跑完。「换一版」那一轮本来就在重做卡，
        # 再叠一段「立刻出续跑卡」只会两张卡打架。
        pending = None if (revise_of or feedback) else await pending_continuation(
            self.runner.checkpoint, session.session_id)
        # 同会话 checkpoint 历史：让新轮的 LLM 看到之前的计划卡和执行状态，
        # 不再说「计划没落到服务端」「素材没到位」。
        session_history = await self._session_checkpoint_brief(session.session_id)
        session_artifacts = await self._session_artifacts_brief(user_id, conversation_id)
        has_pending_plan = False
        if self.runner.checkpoint is not None:
            try:
                has_pending_plan = (
                    await self.runner.checkpoint.pending_plan_for_session(
                        session.session_id) is not None
                )
            except Exception:
                has_pending_plan = False
        async with self._lock_for(session.session_id):
            # R13：出计划卡**之前**先看一眼这台机器手里真有什么。这一段是**事实**，
            # 不是判据——它不拦计划，只保证「模型看到的事实与机器的事实是同一个」。
            # 今天它只剩一条读数（用户手上有几件可上屏的位图）：取材那三条路
            # （检索库 / 出图 / 上传件上屏）已从产品里拿掉，见 `plan/capability.py` 模块头。
            #
            # 只读、读不到就记未判（`capability.py` 里逐条兜住），所以它不会让规划轮变脆；
            # 递空串时模板里那一块整块不出现，与加这一段之前**逐字相同**。
            try:
                capability_notes = render_capability_notes(
                    await probe_capabilities(storage=self.storage, user_id=user_id,
                                             conv_id=conversation_id))
            except Exception:      # noqa: BLE001 - 探活是增益，失败不该让规划轮拼不出提示
                capability_notes = ""
            # 账面要按子块读字节（`knobs` 在 §5.4 末列单列一行），所以走带子块的出口：
            # 整段文本与 `planning_section` 逐字相同，只是多带一份「子块 → 文本」。
            plan_text, plan_blocks = await planning_section_blocks(
                gate, feedback=feedback, prior=prior, pending=pending,
                session_history=session_history, session_artifacts=session_artifacts,
                has_pending_plan=has_pending_plan,
                capability_notes=capability_notes)
            extra_sections = [Section(
                name="planning_round", text=plan_text, kind="conditional",
                condition_key="planning_round", referenced_by=("submit_plan",),
                sub_blocks=plan_blocks)]
            return await self.runner.run(
                session, message, run_id=run_id, stream=stream, attachments=attachments,
                registry=self._planning_registry(gate), planning=True,
                extra_sections=extra_sections,
                style_skill=style_skill,
            )

    async def _prior_plans(self, revise_of: str) -> list[dict[str, Any]]:
        """换一版时要给模型看的旧卡：只有真跑过的那一份（指针行里已校验过的候选）。"""
        if not revise_of or self.runner.checkpoint is None:
            return []
        cp = await self.runner.checkpoint.load(revise_of)
        return list((cp.plan or {}).get("candidates") or []) if cp else []

    async def _session_checkpoint_brief(self, session_id: str) -> list[dict[str, Any]]:
        """同会话最近几条 checkpoint 的摘要，让新轮的 LLM 看到之前的规划与执行历史。

        checkpoint 按 ``session_id`` 绑定了会话窗口，但新轮的 LLM 只看得到 messages 表的
        对话文字，看不到 checkpoint 里的计划卡内容和执行状态。这里把最近 6 条 checkpoint
        的关键信息提出来，由 ``planning_section`` 注入 system 段。
        """
        if self.runner.checkpoint is None:
            return []
        rows = await self.runner.checkpoint.list_for_session(session_id)
        briefs: list[dict[str, Any]] = []
        for row in rows[:6]:
            plan = row.get("plan") or {}
            candidates = plan.get("candidates") or []
            audit = plan.get("audit") or {}
            briefs.append({
                "run_id": row.get("run_id", ""),
                "status": row.get("status", ""),
                "message": (row.get("message") or "")[:80],
                "iteration": row.get("iteration", 0),
                "is_planning": bool(candidates) and not row.get("plan_run_id"),
                "is_execution": bool(row.get("plan_run_id")),
                "plan_run_id": row.get("plan_run_id") or "",
                "candidates": [
                    {"plan_id": c.get("plan_id", ""), "label": c.get("label", ""),
                     "steps": [s.get("node", "") for s in (c.get("steps") or [])]}
                    for c in candidates
                ],
                "audit_plan_id": audit.get("plan_id", ""),
                "audit_unfulfilled": audit.get("unfulfilled") or [],
            })
        return briefs

    async def _session_artifacts_brief(self, user_id: str, conversation_id: str) -> list[str]:
        """同会话已产出的节点名列表，让新轮的 LLM 知道可以用 read_node_history 读什么。"""
        if self.storage is None:
            return []
        try:
            sid = storyline_session_id(user_id, conversation_id)
            repo = self.storage.artifacts(sid)
            return await repo.executed()
        except Exception:
            return []

    async def _record_retro(self, entries: Sequence[tuple[str, str] | None], *,
                            user_id: str, conversation_id: str) -> list[str]:
        """把复盘行写进记忆；写失败只告警，不拖垮这条 run。

        为什么不向上抛：记忆是旁路（下一轮的输入之一），它坏了不该让用户此刻的
        渲染失败。但也不能静默——丢一句「写记忆失败」的告警，界面上看不出来
        偏好为什么没攒下。

        为什么要在这里显式绑身份：``MemoryStore`` 是进程级单例、按执行身份解析 user_id，
        而这两处调用都在 ``runner.run`` **之前**——那时上下文里还没有身份，不绑就会
        把 A 用户的偏好写进 "default" 名下（记忆跟着身份走是这张表的底线）。
        """
        if self.retro is None or not entries:
            return []
        try:
            with use_identity(user_id, conversation_id):
                return await self.retro.record(entries)
        except Exception as exc:
            logger.warning("复盘记忆写入失败（不影响本轮执行）：%s", exc)
            return []

    async def _last_user_text(self, user_id: str, conversation_id: str) -> str:
        """最近一条用户消息的原话：退回重做时，它就是「上一版哪里不行」的证据。"""
        if self.storage is None:
            return ""
        try:
            history = await self.storage.messages.history(
                user_id, conversation_id, limit=20)
        except Exception:
            return ""
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            text = str(msg.get("message") or "").strip()
            if text:
                return text
        return ""

    async def execute_plan(
        self, user_id: str, conversation_id: str, plan_run_id: str,
        frame: Mapping[str, Any], *, run_id: str | None = None,
        stream: bool = False, message: str = "",
    ) -> str:
        """Run B 执行轮：把计划卡上的点击帧编译成两段注入，然后是一次普通 ReAct。

        计划本体只认服务端自己存的那一份：按 ``plan_run_id`` 从指针行取回
        ``submit_plan`` 当轮通过四重校验的候选，浏览器回传的只有「选了哪张卡、
        点了哪些开关、另填了什么」。选不到对应卡就是错——用户无从伪造承诺。
        """
        gate = self.plan_gate
        if gate is None:
            raise RuntimeError("未启用计划门，无法按确认帧执行")
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，取不回候选计划")
        row = await self.runner.checkpoint.row(plan_run_id)
        candidates = ((row or {}).get("plan") or {}).get("candidates") or []
        wanted = str(frame.get("selected_plan") or "").strip()
        plan = next((c for c in candidates
                     if str(c.get("plan_id") or "") == wanted), None)
        if plan is None:
            raise KeyError(
                f"计划 {plan_run_id} 里没有选中的那张卡「{wanted or '(空)'}」"
                f"（可选：{[c.get('plan_id') for c in candidates] or '无'}）。")
        # 风格包：计划卡候选优先（规划轮用 SubmitPlanTool 挂上去的），老卡没有就
        # 退回规划轮的 checkpoint scope。两处都没有 = 不注入，走缺省视觉。
        style_skill = _plan_style_skill(plan, row)
        compiled, issues = gate.validate_execute(plan, frame)
        if not issues.ok:
            raise ValueError("确认帧未通过校验：" + "；".join(issues.errors))
        # 富化依赖图：把每个计划步骤的直接前置（契约里的 requires）补进编译结果。
        # 为什么要有它：执行器要据此**自动**把同一层里互不依赖的节点并成一批并发跑
        # （见 _with_ready_siblings），而不是指望模型自己把两次调用放进同一条回复。
        # 依赖只有契约知道（编译层拿不到），所以在这一层补——一处富化、下游只管用。
        _annotate_requires(compiled, gate)
        # 把依赖图交给执行器：它据此自动把同层就绪的独立节点并成一批并发跑，
        # 不再依赖模型是否把它们写进同一条回复（见 _with_ready_siblings）。
        self._exec_plan_nodes = [str(s.get("node") or "")
                                 for s in (compiled.get("steps") or [])
                                 if isinstance(s, Mapping)]
        # 复盘进记忆（E）：用户在这张卡上动过的地方 + 「本会话已有成片还要再渲」的退回信号。
        # 为什么写在跑之前而不是跑完：确认帧是既成事实，与本轮跑不跑得动无关；
        # 跑到渲染中途崩了，用户「跳过 BGM」的偏好也不该跟着丢。
        prior_nodes = await self._session_artifacts_brief(user_id, conversation_id)
        await self._record_retro(
            plan_card_lines(plan, frame)
            + [rework_line(prior_nodes, self._exec_plan_nodes,
                           message or await self._last_user_text(
                               user_id, conversation_id))],
            user_id=user_id, conversation_id=conversation_id)
        sections = list(render_injections(compiled))
        # 技能正文**按本片计划取块**（T42/§5.7②）：默认集随这份发，其余块只进一行
        # **分段目录**并标出「本轮计划用得上哪几块」，读不读由模型自己点
        # （``load_skill(name, section=…)``）——§2.2「技能的视角还给模型」。
        # 卡面一并传进去，只为一件事——读 `param_options[].default` 判「整片开关动没动过」
        # （编译结果看不出用户勾没勾）；拿不到卡面时按「动过」算（见 plan.skills 的注释）。
        #
        # 这里曾经现查一次「这台机器这一轮调不调得到出图」并把它当 `imagegen_visible`
        # 传进去（R13-T68），好让没配后端的那一轮连供给正文一起不发。出图节点与那几块
        # 正文 2026-10-16 都删了，这个现查随之删掉——它判的那件事已经不存在了。
        sections += await preload_skills(
            self.skill_loader, compiled.get("skills_hint") or (),
            sections=sections_for_plan(compiled, plan))
        # 工具面按这张计划裁（T41）：只发计划用得到的剪辑节点 + 通用件，被裁的节点名
        # 进「如何请求扩面」那一小节（裁的是 schema 字节，不是可发现性，Q56）。
        steps = compiled.get("steps") or []
        sections += self._scope_section(steps)
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        # 从规划轮的用户消息中提取附件 ID，传递到执行轮——
        # 否则 Agent 会忘记用户已上传的素材（如配乐），跑去网上重新下载。
        attachments: list[str] = []
        if self.storage is not None:
            try:
                history = await self.storage.messages.history(user_id, conversation_id, limit=20)
                for msg in reversed(history):
                    if msg.get("role") == "user" and msg.get("attachments"):
                        attachments = list(msg["attachments"])
                        break
            except Exception:
                pass
        # 用户原话取**规划那一轮**的：执行轮的 message 常常是空的（用户只是点了张卡），
        # 退化成系统拼的「按已确认的计划执行：…」。逐镜写手把这段当最高事实，所以
        # 要一路追回用户真说过的话；再退一步才是最近一条用户消息。
        user_request = str((row or {}).get("message") or "").strip()
        if not user_request:
            user_request = message or await self._last_user_text(user_id, conversation_id)
        async with self._lock_for(session.session_id):
            return await self.runner.run(
                session, message or f"按已确认的计划执行：{compiled.get('label') or wanted}",
                run_id=run_id, stream=stream, attachments=attachments,
                registry=self._round_registry(steps),
                extra_sections=sections,
                approved_plan=compiled, plan_run_id=plan_run_id,
                style_skill=style_skill,
                user_request=user_request,
            )

    async def resume(
        self, run_id: str, user_id: str, conversation_id: str, *, stream: bool = False,
        at_seq: int | None = None,
    ) -> str:
        """恢复一次未完成的执行（崩溃恢复入口）。

        at_seq 给定时回到该 run 的某个一致点再续跑——同一把入口既做崩溃恢复
        也做时间旅行，区别只在调用方要不要指到某一界。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，无法恢复执行")
        async with self._lock_for(session.session_id):
            cp = await self.runner.checkpoint.resume(run_id, at_seq)
            if cp is None:
                raise KeyError(f"没有可恢复的 checkpoint: {run_id}")
            return await self.runner.resume(cp, session, stream=stream,
                                            registry=self._resume_registry(cp))

    async def approve(
        self, run_id: str, user_id: str, conversation_id: str, *, decision: str,
        stream: bool = False, note: str = "",
        answers: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        """用户对一条挂起审批的执行做出批准/拒绝，从断点续跑。

        ``decision`` 为 "approve" / "reject" / 兜底方案 key / 弹窗选项 key；
        ``note`` 是用户在弹窗里选或写的原话，带进上下文供模型据此继续。
        计划门产出的执行轮（Run B）与普通执行轮都可能挂起，这里不再分轮次
        ——只认那条 awaiting_approval 的 run。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，无法处理审批")
        async with self._lock_for(session.session_id):
            cp = await self.runner.checkpoint.pending_approval(run_id)
            if cp is None:
                raise KeyError(f"没有待审批的 checkpoint: {run_id}")
            return await self.runner.approve(cp, session, decision=decision,
                                            stream=stream, note=note,
                                            answers=answers,
                                            registry=self._resume_registry(cp))

    async def fork(
        self, run_id: str, at_seq: int, user_id: str, conversation_id: str, *,
        stream: bool = False, run_id_new: str | None = None,
        invalidate: Sequence[str] = (), message: str = "",
    ) -> str:
        """从某个一致点分叉重跑：换一份剪辑产物作用域，上游产物整份复用。

        典型用法是「换 BGM 从 plan_timeline 重跑」：只重放分叉点之后的节点，
        切镜/ASR/画面理解的结果从父 run 复制过来，服务端拦截器据此跳过补齐。
        ``invalidate`` 是这次要真重做的节点集（``checkpoint.fork`` 会把这些产物删掉，
        否则拦截器认为已完成、恰好跳过用户要求重跑的那一步）。
        """
        session = await self.session_manager.get_or_create(user_id, conversation_id)
        if self.runner.checkpoint is None:
            raise RuntimeError("未配置 checkpoint，无法分叉执行")
        async with self._lock_for(session.session_id):
            child = await self.runner.checkpoint.fork(
                run_id, at_seq, run_id_new=run_id_new, invalidate=invalidate,
                message=message)
            # fork 复制父快照的 scope，所以「是不是规划轮」跟着过来——分叉重跑
            # 不该把一轮规划变成执行轮。
            return await self.runner.resume(child, session, stream=stream,
                                            registry=self._resume_registry(child))

    async def pending_runs(self) -> list[Checkpoint]:
        """所有未正常结束的执行（服务启动时补跑或对账用）。"""
        if self.runner.checkpoint is None:
            return []
        return await self.runner.checkpoint.pending()

    # 这里原先还有一套「外层常驻循环」：`submit()` 往进程内 `asyncio.Queue` 投递、
    # `run_forever()` 起个 `while True` 取出来跑、`stop()` 收摊。**全仓没有一个调用方**
    # ——生产的外层循环是 MQ 那条路（`SessionConsumer` 订阅 chat topic，按
    # key=session_id 分区，同一会话在同一个分区里 FIFO 串行，见 mq.py/consumer.py），
    # `run_server.py` 起的是它。两套并存的坏处不只是多一份死代码：那个 `while True`
    # 里 `asyncio.create_task` 起的任务没人 await，单条跑挂只会落一句
    # "Task exception was never retrieved"，而调用方早已拿到（或永远等不到）一个空结果。
    # 删掉它，外层循环只有一条路，语义（同会话串行/异会话并行）也只有一个出处。
