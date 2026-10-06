"""Web 接口层（FastAPI）：架构图最上方的「Web 接口层 ↔ Message Queue ↔ 会话」。

create_app 把 Agent + MQ（+ 可选 Heartbeat）装配成一个 FastAPI 应用：
  - POST /register    签发身份：回 user_id + 一次性明文 token（库里只存 sha256）
  - GET  /whoami      这张凭证反查出的 user_id（前端不再自存身份）
  - POST /chat        投递到 MQ InBound，立即返回 run_id（异步语义，对应图里 Web→MQ→会话）
  - POST /chat/sync   直接 await Agent.handle 返回最终答复（最小闭环 / 测试）
  - WS   /ws/{conversation_id}?token=…  会话窗口长连接：Agent 结果经
                      MQ OutBound → Connection Manager 按 session_id 回投（文档第 6 节）
  - GET  /sessions    当前活跃会话列表（只列本人与自己相关的）
  - GET  /convs       本人会话列表（标题 + 最近更新；换浏览器只凭 token 找回）
  - GET  /convs/{conversation_id}/messages   本人某会话的历史（含附件展示）
  - POST /convs/rename 改本人会话标题
  - DELETE /convs/{conversation_id} 删本人会话（连带其历史）
  - POST /upload      素材上传（原始字节流）→ 直写对象存储 + 登记 materials，回 material_id
  - POST /upload/init · PUT /upload/part · GET /upload/status
    POST /upload/complete · POST /upload/abort
                      大文件分片续传：整文件一次 POST 传不动的素材切成等长分片，一片一问，
                      进度以桶里的列举为准（刷新/断网/换副本都能接着传）；收齐后按序拼流
                      走同一条 ingest_bytes 入库
  - POST /fetch_media 按链接取素材（直链/页面嗅探/yt-dlp）→ 同一条入库路径，回 material_id
  - GET  /settings    模型配置现状：密钥掩码 + 生效来源 + 模型地址（明文不出门）
  - POST /settings/api-key 保存/清除模型密钥（写进 PG 即生效，两个服务热读，不重启）
  - POST /settings/test    连通性自检：真发一次文本 + 一次带图，回耗时与摘要
  - GET  /health      存活探针（与 /register 一起是仅有的两个不鉴权端点）

除 /health 与 /register 外全部要求凭证（spec §7）：HTTP 带 `Authorization: Bearer <token>`，
浏览器 WebSocket 用 `?token=`；**请求里不再有 user_id 这个字段**，它由 token 反查得出。

生命周期（lifespan）：启动时注册消费端（chat + outbound）+ 启动 MQ 与 Heartbeat，
再执行可选 on_startup（MCP 外部工具接入、checkpoint 自动恢复）；关闭时先 on_shutdown
（关闭 MCP 连接、取消常驻子 Agent）再优雅停止。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.parse
import re
import uuid

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from . import model_probe, secrets, uploads
from .agent import Agent
from .auth import Authenticator
from .catalog import get_catalog
from .connection_manager import ConnectionManager
from .broadcast import OUTBOUND_CHANNEL, OutboundBroadcaster
from .storage import Cond
from .consumer import CHAT_TOPIC, SessionConsumer, session_key
from .heartbeat import Heartbeat
from .ingest import IngestRejected, ingest_bytes
from .identity import storyline_session_id
from . import judge as judge_mod
from .llm_openai import (DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_THINKING,
                         get_default_llm)
from .media_fetch import FetchPolicy, FetchRejected, fetch_media
from .mcp_manager import McpManagerError
from .mq import MessageQueue
from .tools.mcp import MCPServerConfig
from .storage import IntegrityConflict, Storage
from .tool import is_tool_error
from .checkpoint import STATUS_AWAITING_APPROVAL, CheckpointManager
from .skill_flows import (clean_steps, draft_body, extract_steps,
                          first_user_request, merge_runs, pick_skill_name)


def _mask_credentials_in_access_logs() -> None:
    """访问日志里的 ``?token=`` 打码：那是身份凭证，不该躺在日志文件里。

    WS 只能把凭证放在查询串上（浏览器不让 WS 握手带自定义头），于是 uvicorn 的访问日志
    每接一条连接就写一行明文 token——本机 ``.tmp/*.log`` 翻一翻就能冒名开会话。
    装在 lifespan 里而不是导入时：uvicorn 启动会 ``dictConfig`` 一遍日志，那一步清掉
    logger 上已有的 filter，导入时装等于白装。
    """
    import logging
    import re

    pat = re.compile(r"(token=)[A-Za-z0-9+/_\-\.]{8,}")

    class _Mask(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001 - 格式化不了就别改它
                return True
            masked = pat.sub(r"\1***", msg)
            if masked != msg:
                record.msg, record.args = masked, ()
            return True

    for name in ("uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _Mask) for f in lg.filters):
            lg.addFilter(_Mask())


class RegisterRequest(BaseModel):
    device_name: str = ""         # spec §7 的可选字段；users 表无对应列，只透传不落地


class ChatRequest(BaseModel):
    conversation_id: str
    message: str
    attachments: list[str] = []   # 已上传素材的 material_id 列表（/upload 返回值）
    resume: bool = False          # True = 续跑本会话在途的 run，而不是开新 run


class RunForkRequest(BaseModel):
    at_seq: int                   # 回退到哪个一致点（含）
    message: str = ""             # 分叉后带进去的指令（空 = 沿用父 run 的原话）
    rerun_nodes: list[str] = []   # 这次要真正重做的节点（自动连带其下游产物一起作废）


class RunResumeRequest(BaseModel):
    at_seq: int | None = None     # 给定则回到该一致点续跑（时间旅行），否则取链尾


class RunApproveRequest(BaseModel):
    """HITL 审批帧：用户对挂起的工具调用做出批准或拒绝。"""
    decision: str = "approve"     # "approve" 放行续跑；"reject" 跳过该批工具继续
    message: str = ""             # 随审批带进去的一句话（可空，仅留痕）
    # 多题弹窗的结构化答案：``[{page, title, answer, key, custom}, …]``。
    # 原先只声明 decision/message，前端发的 answers 被 pydantic 静默忽略——
    # 靠前端把它拼成中文句子塞进 message 才没丢信息。那等于把结构化数据降级成
    # 一段散文：服务端再也无法知道「用户在第 2 题选了哪个 key」。
    answers: list[dict[str, Any]] = []


class PlanConfirmRequest(BaseModel):
    """计划卡上的点击结果。**只有选择，没有计划本体**——那份由服务端按 plan_run_id 取回。"""

    selected_plan: str                    # 选中那张卡的 plan_id
    param_finals: list[dict[str, Any]] = []   # [{step_seq, key, value}]，值必须还在卡面选项里
    skips: list[Any] = []                 # [step_seq] 或 [{step_seq}]，只允许 skippable 的步
    overrides: list[dict[str, Any]] = []  # [{step_seq?, key?, value}]，「其他 / 整体补充」的自由文本
    message: str = ""                     # 随确认带进去的一句话（可空）


class PlanReviseRequest(BaseModel):
    feedback: str = ""                    # 「换一版」的理由：规划轮据此做出可辨别的差异


class FetchMediaRequest(BaseModel):
    conversation_id: str
    url: str                      # 待取料链接：直链 / 含视频的网页 / 站点播放页


class RenameRequest(BaseModel):
    conversation_id: str
    title: str


class ApiKeyRequest(BaseModel):
    api_key: str = ""             # 留空 = 清除已配置的密钥，回落到环境变量


GD_MUSIC_API = "https://music-api.gdstudio.xyz/api.php"



def _gd_music_get(params: dict, timeout: float = 15.0) -> list | dict:
    """通过 curl 调 GD Studio 音乐 API（Python urllib SSL 与 Cloudflare 不兼容，用 curl 绕过）。"""
    url = f"{GD_MUSIC_API}?{urllib.parse.urlencode(params)}"
    r = subprocess.run(["curl", "-s", url], capture_output=True, timeout=timeout,
                       creationflags=_NO_WINDOW)
    return json.loads(r.stdout.decode("utf-8"))


def _gd_download(url: str, timeout: float = 60.0) -> bytes:
    """同步下载音频字节（curl）。"""
    r = subprocess.run(["curl", "-sL", url], capture_output=True, timeout=timeout,
                       creationflags=_NO_WINDOW)
    return r.stdout


async def _attachment_views(storage: Storage, user_id: str, conv_id: str,
                            material_ids: list[str]) -> list[dict[str, Any]]:
    """历史里的 material_id → 展示用附件（含 presigned URL）。不可见的 id 静默跳过。"""
    if not material_ids:
        return []
    rows, _denied = await storage.materials.resolve(list(material_ids), user_id=user_id,
                                                     conv_id=conv_id)
    return [{"material_id": m["id"], "filename": m["filename"], "kind": m["kind"],
             "bytes": m["bytes"], "duration": m["duration_sec"],
             "url": await storage.objects.presign_get(m["object_key"])} for m in rows]


async def _render_media_views(storage: Storage, qa: Any) -> list[dict[str, Any]]:
    """assistant 历史里的成片持久链接 → 可播卡片（与 MediaCardHook 当轮回投同形状）。

    链接在渲染完成当轮由 MediaCardHook 落进 qa.parts 的 media 片段（存对象键，见 §6 修复）。
    presigned 直链读取时现签，所以刷新后照样能播；形状与实时 WS 的 media 帧一致
    （media_url/title/duration），前端复用同一张卡片组件，无需第二套渲染。
    """
    parts = (qa or {}).get("parts") if isinstance(qa, dict) else None
    out: list[dict[str, Any]] = []
    for p in (parts or []):
        if not isinstance(p, dict) or p.get("type") != "media":
            continue
        key = p.get("video_object_key")
        if not isinstance(key, str) or not key:
            continue
        out.append({
            "media_url": await storage.objects.presign_get(key),
            "title": p.get("title") or "",
            "duration": p.get("duration"),
            "artifact_id": p.get("artifact_id"),
            # 证据分级随卡片一起持久在片段里：刷新后这张卡仍然说得出「哪几条验过」
            "evidence": list(p.get("evidence") or []),
        })
    return out


def _plan_views(qa: Any) -> list[dict[str, Any]]:
    """assistant 历史里的计划卡片段 → 可确认的计划（与 WS 的 plan 帧同形状）。

    待确认的那一份必须能从库里重放出来：用户刷新页面不代表放弃这一步。
    ``plan_run_id`` 一并回给前端——确认帧要发往的就是这条规划 run。
    """
    parts = (qa or {}).get("parts") if isinstance(qa, dict) else None
    out: list[dict[str, Any]] = []
    for p in (parts or []):
        if not isinstance(p, dict) or p.get("type") != "plan":
            continue
        plans = p.get("plans") or []
        if not plans:
            continue
        out.append({"plan_run_id": p.get("plan_run_id"), "plans": plans,
                    "warnings": list(p.get("warnings") or [])})
    return out


def _plan_audit_view(qa: Any) -> dict[str, Any] | None:
    """assistant 历史里的对账片段 → 角标数据（计划外步骤 / 声明了没调 / 偏离理由）。

    与实时 WS 的 ``plan reconciliation`` 帧同形状；一份 run 只有一条终态结论，
    链上多条（轮询中途的中间态）时取最后一条。
    """
    parts = (qa or {}).get("parts") if isinstance(qa, dict) else None
    audit = None
    for p in (parts or []):
        if isinstance(p, dict) and p.get("type") == "plan reconciliation":
            audit = {k: v for k, v in p.items() if k != "type"}
    return audit


class ChatQueuedResponse(BaseModel):
    run_id: str
    status: str


class ChatSyncResponse(BaseModel):
    run_id: str
    answer: str
    # 挂起待确认时一并回这两项：非 WS 客户端（CLI/脚本/自动化）拿不到实时帧，
    # 只从 answer 里读那句「已暂停」是没法把选项卡渲染出来的。
    # 形状与 WS 的 approval 帧一致，也与 /convs/{id}/runs/active 的 approval 一致。
    status: str = ""
    approval: dict[str, Any] | None = None


_TOOL_CATEGORIES = {
    "load_media": "剪辑工具", "search_media": "剪辑工具", "split_shots": "剪辑工具",
    "understand_clips": "剪辑工具", "filter_clips": "剪辑工具", "group_clips": "剪辑工具",
    "asr": "剪辑工具", "correct_transcript": "剪辑工具",
    "speech_rough_cut": "剪辑工具", "script_template_rec": "剪辑工具",
    "generate_script": "剪辑工具", "generate_ai_transition": "剪辑工具",
    "transition_rec": "剪辑工具", "text_rec": "剪辑工具", "generate_voiceover": "剪辑工具",
    "select_BGM": "剪辑工具", "plan_timeline": "剪辑工具", "plan_timeline_pro": "剪辑工具",
    "plan_timeline_ai_transition": "剪辑工具", "render_video": "剪辑工具",
    "read_node_history": "剪辑工具", "render_status": "剪辑工具",
    "write_file": "文件工具", "read_file": "文件工具", "edit_file": "文件工具", "grep": "文件工具",
    "fetch_url": "联网工具", "web_search": "联网工具", "fetch_media": "联网工具",
    "spawn_subagent": "子助手", "load_skill": "技能管理",
    "update_memory": "记忆", "read_memory": "记忆",
    "send_message": "团队协作", "read_inbox": "团队协作",
    "plan_editing_team": "团队协作", "list_tasks": "团队协作", "claim_task": "团队协作",
    "complete_task": "团队协作", "team_status": "团队协作", "start_subagent": "团队协作",
    "create_cron_job": "定时任务", "list_cron_jobs": "定时任务", "delete_cron_job": "定时任务",
    "rerun_from": "执行控制",
}


def _tool_category(name: str) -> str:
    return _TOOL_CATEGORIES.get(name, "其他")


class CatalogJSONResponse(JSONResponse):
    """HTTP 侧唯一的 JSON 出口：序列化前过一遍中文名词表（见 ``catalog.py``）。

    挂在这一层而不是每个 handler 里，是为了让「新端点忘了包」不可能发生——
    凡按默认响应类出去的 JSON 都自动带 ``*_display``、自由文本已换中文。
    词表为空（Storyline 没连上 / 离线测试）时完全直通，不付递归的钱。
    """

    def render(self, content: Any) -> bytes:
        catalog = get_catalog()
        if catalog:
            content = catalog.rewrite_obj(content)
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          indent=None, separators=(",", ":")).encode("utf-8")


def reject_stale_answer(row: Mapping[str, Any], decision: str,
                        answers: list[dict[str, Any]]) -> None:
    """答案必须属于**当前这道题**，否则拒绝——这是「没选完就继续」的守口。

    真机实测的坏情形（模型自己都发现了，原话「我这边一直只收到「治愈系慢节奏」
    这一句，时长选项始终没被选中」）：客户端拿上一题的 key 去答下一题
    ——问「成片时长想控制在多少」，回的却是风格题的 key。
    服务端原先照单全收，于是同一道题被反复"答"了四次、run 原地打转。

    判据（只在能判定时判，判不了就放行，避免误伤）：
      · 当前挂起带结构化 ask 且列出了 options 时：
          - decision 是 approve/reject（通用审批门的语义）→ 放行；
          - decision 是某个 option 的 key → 放行；
          - 否则 → 409，并明确告诉用户刷新（前端据此重新拉当前题）。
      · 多题 answers 里每条 `key` 同理必须能对上；用户自己写的（custom 有值）
        永远放行——那是人打的字，不可能是"陈旧 key"。
      · 没有 ask / 没有 options（通用审批门、兜底选项）→ 放行，不猜。
    """
    ask = ((row.get("approval") or {}).get("ask") or {}) if row else {}
    options = [o for o in (ask.get("options") or []) if isinstance(o, Mapping)]
    if not options:
        return
    allowed = {str(o.get("key") or "") for o in options}
    if not allowed or allowed == {""}:
        return

    d = str(decision or "")
    if d in ("approve", "reject", ""):
        return
    if d not in allowed and not _is_custom_answer(answers, d):
        raise HTTPException(
            409, "你选的这一项不属于当前这道题（可能是页面还停在上一题）。"
                 "请刷新页面，按当前弹出的问题重新选择。")

    for a in answers or []:
        if not isinstance(a, Mapping):
            continue
        if str(a.get("custom") or "").strip():
            continue                     # 用户自己写的，放行
        k = str(a.get("key") or a.get("answer") or "")
        if k and k not in allowed:
            raise HTTPException(
                409, "选项不属于当前这道题（可能是页面还停在上一题）。"
                     "请刷新页面，按当前弹出的问题重新选择。")


def _is_custom_answer(answers: list[dict[str, Any]], decision: str) -> bool:
    """decision 是不是用户在弹窗里自己写的那句（自由文本）。"""
    for a in answers or []:
        if not isinstance(a, Mapping):
            continue
        custom = str(a.get("custom") or "").strip()
        if custom and (custom == decision or str(a.get("answer") or "") == decision):
            return True
    return False


def readable_decision(row: Mapping[str, Any], decision: str, message: str,
                      answers: list[dict[str, Any]]
                      ) -> tuple[str, str, list[dict[str, Any]]]:
    """把弹窗选项的**机器 key** 换成人话，填进**给用户看**的那条消息。

    为什么必须在这一层换：前端点选后回传的是 ``o.key``（``d180`` / ``key_only``），
    它会作为 ``message`` 原样进消息链，用户就在对话里看到一串 ``d180``。
    真机实测用户的原话：「之前的弹窗按钮点击后显示的那些 d180、key_only 等
    能不能不要在前端显示」——他要的是自己点过的那句中文。

    为什么在服务端换而不是只改前端：CLI / 脚本 / 老前端也一并受益，一处修三处一致。

    **``decision`` 必须保持原样不动**：它是语义键，下游靠它判分支
    （``decision_is_confirm`` 认的是 ``confirm_render`` 这个 key，换成中文标签就
    判不出来了，渲染确认会失效）。所以这里只改 ``message`` 与 ``answers`` 里的
    ``answer``——那两样是写进对话、给模型和用户看的。

    放在**模块级**而不是 create_app 内：这样用例可以直接 import 它。
    埋在闭包里就只能靠 exec 抠源码来测，那正是「测试与生产各写一份」的来源。
    """
    ask = ((row.get("approval") or {}).get("ask") or {}) if row else {}
    by_key = {str(o.get("key") or ""): str(o.get("label") or "")
              for o in (ask.get("options") or []) if isinstance(o, Mapping)}

    out: list[dict[str, Any]] = []
    for a in answers or []:
        if not isinstance(a, Mapping):
            continue
        item = dict(a)
        key = str(item.get("key") or item.get("answer") or "")
        label = by_key.get(key) or ""
        custom = str(item.get("custom") or "").strip()
        # custom 优先：用户自己写的那句比任何预设标签都更贴切
        if custom:
            item["answer"] = custom
        elif label:
            item["answer"] = label
        out.append(item)

    # 只把 message 换成人话。换不出来就保留原文，不编造。
    new_message = message
    if message:
        new_message = by_key.get(str(message).strip()) or message
    elif out:
        new_message = str(out[0].get("answer") or "")
    return decision, new_message, out


_SKILLS_UI_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>技能库管理</title>
<style>
 body{font-family:system-ui,"Segoe UI","Microsoft YaHei",sans-serif;max-width:860px;
      margin:32px auto;padding:0 16px;color:#1f2328}
 h1{font-size:22px} .row{display:flex;gap:8px;align-items:center;margin:12px 0}
 button{padding:6px 14px;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa;
        cursor:pointer} button:hover{background:#eef1f4}
 table{border-collapse:collapse;width:100%;margin-top:12px}
 th,td{border:1px solid #d8dee4;padding:6px 10px;text-align:left;font-size:14px}
 th{background:#f6f8fa} .ok{color:#1a7f37}.bad{color:#cf222e}
 #msg{margin-left:8px;font-size:13px;color:#656d76}
 input[type=file]{font-size:14px}
</style></head><body>
<h1>技能库管理 <span id="msg"></span></h1>
<div class="row">
  <button onclick="reload()">重新扫描导入目录</button>
  <input type="file" id="zip" accept=".zip">
  <button onclick="upload()">上传技能包(.zip)</button>
  <span style="font-size:12px;color:#656d76">删除不可撤销</span>
  <a href="/" style="margin-left:auto;font-size:13px;color:#0969da">← 返回会话</a>
</div>
<table id="tbl"><thead><tr>
  <th>名称</th><th>中文名</th><th>常驻</th><th>可用</th><th>描述</th><th>附件</th><th>操作</th>
</tr></thead><tbody></tbody></table>
<script>
const msg = t => document.getElementById("msg").textContent = t;
const auth = () => ({ "Authorization": "Bearer " + (localStorage.getItem("ca.token") || "") });
async function load() {
  try {
    const r = await fetch("/skills", { headers: auth() });
    if (r.status === 401) { msg("凭证无效:请先在主页面登录"); return; }
    const data = await r.json();
    const tb = document.querySelector("#tbl tbody");
    tb.innerHTML = "";
    for (const s of data.skills) {
      const tr = document.createElement("tr");
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${s.name}</td><td>${s.display || "—"}</td>
        <td>${s.always ? "是" : "否"}</td>
        <td class="${s.available ? "ok" : "bad"}">${s.available ? "可用" : (s.unavailable_reason || "不可用")}</td>
        <td>${(s.description || "").slice(0, 80)}</td><td>${(s.files || []).join(", ") || "—"}</td>
        <td><button data-n="${s.name}" onclick="del(this.dataset.n)"
             style="padding:2px 8px;font-size:12px;color:#cf222e">删除</button></td>`;
      tb.appendChild(tr);
    }
    msg(`共 ${data.skills.length} 个技能`);
  } catch (e) { msg("加载失败:" + e); }
}
async function del(name) {
  if (!confirm("删除技能 " + name + "？附件一并删除，不可撤销。")) return;
  const r = await fetch("/skills/" + encodeURIComponent(name),
                        { method: "DELETE", headers: auth() });
  const d = await r.json().catch(() => ({}));
  msg(r.ok ? "已删除:" + d.deleted : ("失败:" + (d.detail || r.status)));
  load();
}
async function reload() {
  const r = await fetch("/skills/reload", { method: "POST", headers: auth() });
  const d = await r.json().catch(() => ({}));
  msg(r.ok ? "已重扫:" + (d.imported || []).join(", ") : ("失败:" + (d.detail || r.status)));
  load();
}
async function upload() {
  const f = document.getElementById("zip").files[0];
  if (!f) { msg("先选择 .zip 文件"); return; }
  const r = await fetch("/skills/upload?filename=" + encodeURIComponent(f.name),
                        { method: "POST", headers: auth(), body: await f.arrayBuffer() });
  const d = await r.json().catch(() => ({}));
  msg(r.ok ? "已导入:" + (d.imported || []).join(", ") : ("失败:" + (d.detail || r.status)));
  load();
}
load();
</script></body></html>"""


class McpServerRequest(BaseModel):
    """POST /mcp/servers 请求体：name + MCPServerConfig 字面量（字段见 tools/mcp.py）。"""

    name: str
    config: dict = {}
    enabled: bool = False


class SkillUpsertRequest(BaseModel):
    """PUT /skills/{name} 请求体：弹窗表单直接编辑技能的 frontmatter 与正文。"""

    display: str = ""
    always: bool = False
    description: str = ""
    requires: str = ""       # 如 "CLI: curl, ENV: KEY"
    body: str = ""


class AuthCredentials(BaseModel):
    """账号密码登录/注册请求体。"""

    username: str
    password: str

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{2,32}$")

def _check_credentials(username: str, password: str) -> tuple[str, str]:
    username = (username or "").strip()
    password = password or ""
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "用户名 2~32 位，仅限中英文、数字、_ -")
    if len(password) < 6 or len(password) > 128:
        raise HTTPException(400, "密码至少 6 位（至多 128）")
    return username, password



class MainModelRequest(BaseModel):
    """POST /settings/model:主模型名与地址的按用户覆盖(空串 = 清除恢复默认)。"""

    model: str = ""
    base_url: str = ""


class JudgeModelRequest(BaseModel):
    """POST /settings/judge:判断模型(Jev 类)三件套。api_key 传空 = 清除。"""

    base_url: str = ""
    model: str = ""
    api_key: str = ""


class SkillFromRunRequest(BaseModel):
    """POST /skills/from_run 请求体：run_id=沉淀单条执行；conversation_id=整会话合并沉淀。"""

    run_id: str = ""
    conversation_id: str = ""
    name: str = ""
    display: str = ""


def create_app(
    agent: Agent,
    mq: MessageQueue,
    *,
    heartbeat: Heartbeat | None = None,
    topic: str = CHAT_TOPIC,
    static_dir: str | Path | None = None,
    on_startup: Any | None = None,   # async callable()：MCP 接入 / checkpoint 自动恢复等
    on_shutdown: Any | None = None,  # async callable()：关闭外部连接 / 常驻子 Agent
    storage: Storage,                 # 必填：鉴权要读 users 表，/upload 与 /fetch_media 落点也在这
    max_upload_mb: int = 1024,
    upload_ttl_sec: float = uploads.DEFAULT_SESSION_TTL_SEC,
    upload_sweep_sec: float = 600.0,       # 放弃的分片多久扫一次；<=0 关掉后台清扫（测试用）
    fetch_policy: FetchPolicy | None = None,          # /fetch_media 的护栏（上限/超时/yt-dlp）
    skill_loader: Any | None = None,  # SkillLoader：/tools 端点用
    skills_dir: str | Path | None = None,  # 技能导入目录：/skills/reload 热同步用
    mcp_manager: Any | None = None,   # McpManager：/mcp/servers 动态注册端点用
    editing_contract: Any | None = None,   # ContractSlot：/runs/*/fork 展开 rerun_nodes 的下游
    broadcast_url: str | None = None,      # Redis DSN：多副本时把 OutBound 帧广播给各实例
    broadcast_channel: str = OUTBOUND_CHANNEL,
    broadcast_client_factory: Any | None = None,  # 假 redis 客户端入口（离线单测用）
) -> FastAPI:
    consumer = SessionConsumer(agent, mq, topic=topic)
    connections = ConnectionManager(mq)
    broadcaster = (
        OutboundBroadcaster(
            mq,
            connections,
            url=broadcast_url,
            channel=broadcast_channel,
            client_factory=broadcast_client_factory,
        )
        if broadcast_url
        else None
    )
    auth = Authenticator(storage)

    async def _reap_uploads() -> None:
        """回收「再没人接着传」的分片：桶里那些字节只有这里会清。

        启动先扫一次（与两个服务的启动对账同形），之后按间隔再扫——实例可以几天不重启，
        只靠启动扫描等于让中途放弃的上传一直占着桶。扫不动只是垃圾多留一轮，
        不该拖垮服务，所以异常只记进 app.state 一次，不外抛。
        """
        while True:
            try:
                app.state.upload_sweep = await uploads.sweep_upload_sessions(
                    storage, max_age_sec=upload_ttl_sec)
            except Exception as e:  # noqa: BLE001 - 存储抖动：下一轮再试
                app.state.upload_sweep = {"error": str(e)[:200]}
            await asyncio.sleep(upload_sweep_sec)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _mask_credentials_in_access_logs()   # uvicorn 已配好日志，此刻装才不会被 dictConfig 冲掉
        consumer.start()          # 先注册回调
        if broadcaster is not None:
            await broadcaster.start()   # OutBound → Redis 频道 → 各实例本地注册表
        else:
            connections.start()   # OutBound → Connection Manager（单副本直连）
        await mq.start()          # 再启动消费循环
        # on_startup 必须先跑：它负责 storage.start()（建表 / 迁移）。
        # 依赖存储表的两件事都得排在它后面：
        #   · heartbeat.start() 第一件事就是查 scheduled_jobs 表；
        #   · _reap_uploads() 启动即扫一遍 upload_sessions 表。
        # 空库上这两个都会撞 UndefinedTableError。heartbeat 会直接冒泡出 lifespan
        # （uvicorn 起不来），upload 清扫则被自身 try 吞掉、退化成「第一次什么都没扫到」
        # ——后者正是 test_upload_resume 第 ⑫ 条抓到的：app.state.upload_sweep 恒为 0。
        if on_startup is not None:
            await on_startup()
        if heartbeat is not None:
            await heartbeat.start()
        sweeper = (asyncio.create_task(_reap_uploads())
                   if upload_sweep_sec > 0 else None)
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
            if on_shutdown is not None:
                try:
                    await on_shutdown()
                except Exception:  # noqa: BLE001 - 关闭阶段不因外部连接异常而中断
                    pass
            if heartbeat is not None:
                heartbeat.stop()
            if broadcaster is not None:
                await broadcaster.stop()
            await mq.stop()

    app = FastAPI(title="智能创作助手", lifespan=lifespan,
                  default_response_class=CatalogJSONResponse)

    # HTML 不缓存（确保浏览器加载最新 index.html → 最新 hash JS），带 hash 的 JS/CSS 照常缓存。
    class _NoCacheHTML(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct:
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp

    app.add_middleware(_NoCacheHTML)
    app.state.agent = agent
    app.state.mq = mq
    app.state.consumer = consumer
    app.state.connections = connections
    app.state.broadcaster = broadcaster
    app.state.heartbeat = heartbeat
    app.state.storage = storage
    app.state.auth = auth
    app.state.upload_sweep = None   # 上一次分片清扫的结果（后台任务的唯一可见出口）

    @app.exception_handler(IntegrityConflict)
    async def integrity_conflict(_: Request, exc: IntegrityConflict) -> JSONResponse:
        """存储层抛出的归属冲突一律 409：写别人名下的会话不该变成 500 栈。"""
        return CatalogJSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request,
                         exc: StarletteHTTPException) -> JSONResponse:
        """报错文案与成功响应走同一个出口：中文映射不许留一条「错误通道不替」的缺口。

        形状与 FastAPI 默认那条一致（``{"detail": …}`` + 原状态码/响应头），
        只把序列化换成挂了词表的那一个。
        """
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc.detail)
        resp = CatalogJSONResponse(status_code=getattr(exc, "status_code", 500) or 500,
                                   content={"detail": detail})
        for k, v in (getattr(exc, "headers", None) or {}).items():
            resp.headers[k] = v
        return resp

    app.add_exception_handler(HTTPException, http_error)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/tools")
    async def list_tools(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """返回系统当前注册的全部工具、技能和 MCP 节点，供前端工具库面板展示。"""
        reg = getattr(getattr(agent, "runner", agent), "registry", None)
        groups: dict[str, list[dict[str, str]]] = {}
        if reg is not None:
            for schema in reg.get_definitions():
                fn = schema.get("function", schema)
                name = fn.get("name", "")
                desc = fn.get("description", "")
                cat = _tool_category(name)
                groups.setdefault(cat, []).append({"name": name, "desc": desc,
                                                   "params": fn.get("parameters")})
        skills_list: list[dict[str, str]] = []
        if skill_loader is not None:
            try:
                for sk in await skill_loader.discover(user_id):
                    skills_list.append({
                        "name": sk.name,
                        "name_display": sk.display or "",
                        "desc": sk.description or "",
                        "always": "是" if sk.always else "否",
                        "available": "可用" if sk.available else f"不可用：{sk.unavailable_reason or ''}",
                    })
            except Exception:
                pass
        return {"groups": groups, "skills": skills_list,
                # 参数标签的单源：前端每次渲染入参先查这张表，本地那份退为兜底
                "params_display": get_catalog().params_display()}

    # ---- 技能库管理：文件夹热同步 / zip 上传注册 / 可视面板 ----

    async def _sync_skill_catalog() -> None:
        """技能增删后把中文名并回出口词表（best-effort：取不到只退机器名）。"""
        if skill_loader is None:
            return
        try:
            displays = {s.name: s.display
                        for s in await skill_loader.discover() if s.display}
            if displays:
                get_catalog().update(displays)
        except Exception:
            pass

    @app.get("/skills")
    async def skills_list(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """技能库清单：名称/中文名/描述/常驻/可用性，供管理面板展示。
        只返回系统内置(NULL) + 本人添加的技能。"""
        skills = await skill_loader.discover(user_id) if skill_loader else []
        return {"skills": [
            {"name": s.name, "display": s.display, "description": s.description,
             "always": s.always, "available": s.available,
             "unavailable_reason": s.unavailable_reason,
             "files": [f["relpath"] for f in s.files]}
            for s in skills]}

    @app.post("/skills/reload")
    async def skills_reload(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """重新扫描技能导入目录（同启动期 sync_from_dir，幂等覆盖）——
        往文件夹里丢一个新技能目录后调它即生效，不必重启服务。"""
        if skill_loader is None:
            raise HTTPException(409, "技能库未启用（--no-skills）")
        if not skills_dir:
            raise HTTPException(409, "本服务未配置技能导入目录")
        imported = await skill_loader.sync_from_dir(skills_dir)
        await _sync_skill_catalog()
        return {"imported": imported}

    @app.post("/skills/upload")
    async def skills_upload(request: Request,
                            filename: str = "skill.zip",
                            user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """上传技能包（zip 原始字节流，与 /upload 同款不做 multipart）。

        包内 SKILL.md 的 frontmatter name 即技能名；解析失败/路径越界/超限一律 400，
        不留半截入库。
        """
        if skill_loader is None:
            raise HTTPException(409, "技能库未启用（--no-skills）")
        data = await request.body()
        try:
            imported = await skill_loader.import_zip(filename, data,
                                                     owner_user_id=user_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - 坏包统一 400，不泄漏栈
            raise HTTPException(400, f"技能包解析失败：{exc}")
        await _sync_skill_catalog()
        return {"imported": imported}

    @app.delete("/skills/{name}")
    async def skills_delete(name: str,
                            user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """删除技能：删行的同时清附件对象（best-effort，删不掉不阻断删行）。

        只能删本人添加的技能（owner_user_id = 当前用户），不能删系统内置的。
        """
        if skill_loader is None:
            raise HTTPException(409, "技能库未启用（--no-skills）")
        row = await storage.skills.get(name)
        if row is None:
            raise HTTPException(404, f"技能 {name!r} 不存在")
        if row.get("owner_user_id") != user_id:
            raise HTTPException(403, "系统内置技能不能删除")
        await skill_loader.drop(name)
        return {"deleted": name}

    @app.get("/skills/{name}")
    async def skill_detail(name: str,
                           user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """技能详情（含 SKILL.md 正文与依赖），详情弹窗的数据源。"""
        if skill_loader is None:
            raise HTTPException(409, "技能库未启用（--no-skills）")
        sk = await skill_loader.get(name)
        if sk is None:
            raise HTTPException(404, f"技能 {name!r} 不存在")
        return {"name": sk.name, "display": sk.display, "description": sk.description,
                "always": sk.always, "requires": sk.requires, "available": sk.available,
                "unavailable_reason": sk.unavailable_reason, "body": sk.body,
                "files": [f["relpath"] for f in sk.files]}

    @app.put("/skills/{name}")
    async def skill_upsert(name: str, req: SkillUpsertRequest,
                           user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """创建/更新技能（upsert）：正文与 frontmatter 直接由弹窗表单编辑保存。

        附件清单原样保留（编辑文案不动附件；附件管理仍走 zip 上传/重扫）。
        frontmatter 由表单字段现拼，库里与磁盘上不再有两份真相。
        """
        if skill_loader is None:
            raise HTTPException(409, "技能库未启用（--no-skills）")
        name = (name or "").strip()
        if not name or len(name) > 64 or not all(c.isalnum() or c in "_-" for c in name):
            raise HTTPException(400, "技能名只允许字母数字与 _ -（1~64 位）")
        prev = await skill_loader.get(name)
        files = [dict(f) for f in (prev.files if prev else [])]
        fm = {"name": name, "display": req.display, "description": req.description,
              "always": "true" if req.always else "false", "requires": req.requires}
        await storage.skills.upsert(name, req.body, description=req.description,
                                    frontmatter=fm, files=files)
        await _sync_skill_catalog()
        return {"saved": name, "attachments_kept": len(files)}

    @app.post("/skills/from_run")
    async def skill_from_run(req: SkillFromRunRequest,
                             user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """把一条执行流水沉淀为 WORKFLOW SKILL。

        数据源是该 run 的 checkpoint 消息链（工具调用按序配对结果）；有模型密钥时
        由 LLM 润色成文，失败退回确定性模板。生成的技能可在前端弹窗里继续编辑。
        """
        if skill_loader is None:
            raise HTTPException(409, "技能库未启用（--no-skills）")
        cm = CheckpointManager(storage)
        runs_steps: list[list[dict[str, Any]]] = []
        user_request = ""
        scope_label = ""
        if req.conversation_id:
            # 会话级：该会话所有**已完成**执行，按时间正序逐条净化后合并——
            # 跨 run 同名工具只保留最后一次（后一次代表用户修正后的做法），
            # 「改了 N 次才出成片」的试错链由此收敛成一条干净主流程。
            rows = await cm.list_for_session(session_key(user_id, req.conversation_id))
            for row in reversed(rows):          # list_for_session 最近在前 → 反转成正序
                if row.get("status") != "completed":
                    continue
                cp = await cm.load(row["run_id"])
                if cp is None:
                    continue
                cleaned, _ = clean_steps(extract_steps(cp.messages))
                if cleaned:
                    runs_steps.append(cleaned)
                if not user_request:
                    user_request = first_user_request(cp.messages)
            scope_label = f"会话 {req.conversation_id}"
            if not runs_steps:
                raise HTTPException(400, "这个会话没有已完成的、含工具调用的执行可沉淀")
            steps, dropped = merge_runs(runs_steps)
        else:
            if not req.run_id:
                raise HTTPException(400, "run_id 与 conversation_id 至少给一个")
            cp = await cm.load(req.run_id)
            if cp is None:
                raise HTTPException(404, f"执行 {req.run_id!r} 不存在")
            # 归属核对（session_id = "{user_id}:{conv_id}"，与 _owned_run_row 同口径）：
            # 别人的 run 一律按 404（不泄露存在性）
            if not str(cp.session_id).startswith(f"{user_id}:"):
                raise HTTPException(404, f"执行 {req.run_id!r} 不存在")
            raw = extract_steps(cp.messages)
            steps, dropped = clean_steps(raw)
            user_request = first_user_request(cp.messages)
            scope_label = f"执行 {req.run_id[:8]}"
            if not steps:
                raise HTTPException(400, "这条执行没有可沉淀的创作步骤"
                                         "（失败尝试与问询/查询类调用不计入）")

        try:
            llm = _runtime_llm()
        except Exception:
            llm = None
        body, polished = await draft_body(steps, user_request, llm=llm)

        base = (req.name or (f"conv_flow_{req.conversation_id[:6]}"
                             if req.conversation_id else f"run_flow_{req.run_id[:6]}")).strip()
        existing = {sk.name for sk in await skill_loader.discover()}
        name = pick_skill_name(base, existing)
        display = (req.display or "会话流程沉淀").strip()
        description = (f"【WORKFLOW SKILL】由{scope_label}沉淀："
                       f"{user_request[:80]}")
        await storage.skills.upsert(name, body, description=description,
                                    frontmatter={"name": name, "display": display,
                                                 "description": description,
                                                 "always": "false", "requires": ""},
                                    files=[])
        await _sync_skill_catalog()
        return {"name": name, "display": display, "steps": len(steps),
                "dropped": dropped, "llm_polished": polished}

    @app.get("/skills-ui", response_class=None)
    async def skills_ui() -> HTMLResponse:
        """技能管理页（免构建的独立小页）：列出/上传/重扫，凭证走 localStorage 的 ca.token。"""
        return HTMLResponse(_SKILLS_UI_HTML)

    # ---- MCP 动态注册：配置落库，热连/热断 ----

    @app.get("/mcp/servers")
    async def mcp_servers_list(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """动态 MCP Server 清单：配置（凭证掩码）+ 启停状态 + 在线工具名。
        只返回系统部署期(NULL) + 本人添加的 MCP 服务。"""
        if mcp_manager is None:
            raise HTTPException(409, "MCP 动态注册未启用")
        rows = await storage.mcp_servers.list(user_id)
        live = mcp_manager.live_servers()
        servers: list[dict[str, Any]] = []
        for r in rows:
            cfg = dict(r.get("config") or {})
            for sect in ("headers", "env"):
                h = cfg.get(sect)
                if isinstance(h, dict):
                    cfg[sect] = {
                        k: (secrets.mask(str(v))
                            if any(t in k.lower() for t in ("auth", "token", "key", "secret"))
                            else v)
                        for k, v in h.items()}
            updated = r.get("updated_at")
            servers.append({
                "name": r["name"], "config": cfg, "enabled": bool(r.get("enabled")),
                "live": r["name"] in live, "tools": live.get(r["name"], []),
                "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else "",
            })
        return {"servers": servers}

    @app.post("/mcp/servers")
    async def mcp_servers_upsert(req: McpServerRequest,
                                 user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """登记/更新一个 MCP Server（只写配置，不改变连接状态——启停走 enable/disable）。"""
        if mcp_manager is None:
            raise HTTPException(409, "MCP 动态注册未启用")
        name = (req.name or "").strip()
        if not name or len(name) > 64 or not all(c.isalnum() or c in "_-" for c in name):
            raise HTTPException(400, "名称只允许字母数字与 _ -（1~64 位）")
        try:
            cfg = MCPServerConfig.from_dict(name, req.config)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"配置形状不对：{exc}")
        if not cfg.url and not cfg.command:
            raise HTTPException(400, "url 与 command 至少给一个")
        await storage.mcp_servers.upsert(name, req.config, enabled=req.enabled,
                                         owner_user_id=user_id)
        return {"saved": name, "enabled": req.enabled,
                "live": mcp_manager.is_live(name), "tools": mcp_manager.tool_names(name)}

    @app.post("/mcp/servers/{name}/enable")
    async def mcp_servers_enable(name: str,
                                 user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """热连：按库里配置连接并注册工具（幂等；撞名/连不上如实报错，不落半截状态）。"""
        if mcp_manager is None:
            raise HTTPException(409, "MCP 动态注册未启用")
        row = await storage.mcp_servers.get(name)
        if row is None:
            raise HTTPException(404, f"MCP server {name!r} 未注册")
        if row.get("owner_user_id") != user_id:
            raise HTTPException(403, "只能操作本人添加的 MCP 服务")
        try:
            names = await mcp_manager.connect(name)
        except McpManagerError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - 远端不可达等
            raise HTTPException(502, f"连接失败：{exc}")
        await storage.mcp_servers.set_enabled(name, True)
        return {"name": name, "live": True, "tools": names}

    @app.post("/mcp/servers/{name}/disable")
    async def mcp_servers_disable(name: str,
                                  user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """热断：注销该 server 的全部工具并关闭连接（配置行保留）。"""
        if mcp_manager is None:
            raise HTTPException(409, "MCP 动态注册未启用")
        row = await storage.mcp_servers.get(name)
        if row is None:
            raise HTTPException(404, f"MCP server {name!r} 未注册")
        if row.get("owner_user_id") != user_id:
            raise HTTPException(403, "只能操作本人添加的 MCP 服务")
        removed = await mcp_manager.disconnect(name)
        await storage.mcp_servers.set_enabled(name, False)
        return {"name": name, "live": False, "removed": removed}

    @app.delete("/mcp/servers/{name}")
    async def mcp_servers_delete(name: str,
                                 user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """删除：先断开（若在线）再删配置行。只能删本人添加的。"""
        if mcp_manager is None:
            raise HTTPException(409, "MCP 动态注册未启用")
        row = await storage.mcp_servers.get(name)
        if row is None:
            raise HTTPException(404, f"MCP server {name!r} 未注册")
        if row.get("owner_user_id") != user_id:
            raise HTTPException(403, "只能删除本人添加的 MCP 服务")
        await mcp_manager.disconnect(name)
        await storage.mcp_servers.drop(name)
        return {"deleted": name}

    @app.post("/auth/register")
    async def auth_register(req: AuthCredentials) -> dict[str, str]:
        """账号密码注册：创建身份并直接签发 JWT（与登录同形，省一次往返）。

        REGISTER_OPEN=0 时关闭自助注册——已有账号登录不受影响。"""
        if os.environ.get("REGISTER_OPEN", "1") in ("0", "false", "no"):
            raise HTTPException(403, "自助注册已关闭（REGISTER_OPEN=0）；请联系管理员开通账号")
        username, password = _check_credentials(req.username, req.password)
        try:
            uid = await auth.register_user(username, password)
        except IntegrityConflict as exc:
            raise HTTPException(409, str(exc))
        token = await auth.issue_jwt(uid)
        return {"user_id": uid, "username": username, "token": token}

    @app.post("/auth/login")
    async def auth_login(req: AuthCredentials) -> dict[str, str]:
        """账号密码登录：成功签发 JWT（7 天有效）。失败统一 401，不区分用户不存在与密码错。"""
        username, password = _check_credentials(req.username, req.password)
        token = await auth.login(username, password)
        if token is None:
            raise HTTPException(401, "用户名或密码不对")
        row = await auth._users.find_by_username(username)
        return {"user_id": row["id"], "username": username, "token": token}

    @app.post("/register")
    async def register(req: RegisterRequest | None = None) -> dict[str, str]:
        if os.environ.get("REGISTER_OPEN", "1") in ("0", "false", "no"):
            raise HTTPException(403, "匿名注册通道已关闭（REGISTER_OPEN=0）；请使用账号登录")
        """签发新身份：明文 token 只在这一次响应里出现，PG 里只有它的 sha256。

        这是（连同 /health）唯一不鉴权的端点——否则新客户端无从取得第一份凭证。
        """
        user_id, token = await auth.register(req.device_name if req else "")
        return {"user_id": user_id, "token": token}

    @app.get("/whoami")
    async def whoami(user_id: str = Depends(auth.http_user_id)) -> dict[str, str]:
        """这张凭证是谁：前端不再自存 user_id（spec §7 废弃 ca.user），需要显示时问服务端。"""
        row = await auth._users.get(user_id)
        username = (row or {}).get("username") or ""
        return {"user_id": user_id, "username": username}

    @app.post("/chat", response_model=ChatQueuedResponse)
    async def chat(req: ChatRequest,
                   user_id: str = Depends(auth.http_user_id)) -> ChatQueuedResponse:
        # 入队前先认领会话 id：别人名下的会话要在这里就回 409，
        # 不能等消费者取到帧才炸——那时前端只看到一个永远不结束的 run。
        await storage.conversations.claim(user_id, req.conversation_id)
        run_id = uuid.uuid4().hex[:12]
        if req.resume:
            run_id = await _resumed_run_id(user_id, req.conversation_id) or run_id
        await mq.publish(
            topic,
            session_key(user_id, req.conversation_id),
            {
                "user_id": user_id,
                "conversation_id": req.conversation_id,
                "message": req.message,
                "run_id": run_id,
                "attachments": req.attachments,
                "resume": req.resume,
            },
        )
        return ChatQueuedResponse(run_id=run_id, status="queued")

    @app.post("/chat/sync", response_model=ChatSyncResponse)
    async def chat_sync(req: ChatRequest,
                        user_id: str = Depends(auth.http_user_id)) -> ChatSyncResponse:
        await storage.conversations.claim(user_id, req.conversation_id)
        run_id = uuid.uuid4().hex[:12]
        if req.resume:
            run_id = await _resumed_run_id(user_id, req.conversation_id) or run_id
        answer = await agent.handle(
            user_id, req.conversation_id, req.message, run_id=run_id,
            attachments=req.attachments, resume=req.resume
        )
        # 挂起态如实回给非 WS 客户端：answer 仍然保留（旧客户端照旧能读），
        # 但把结构化提问一并给出，脚本/CLI 才拿得到选项。
        status, approval = await _approval_view_for(agent, user_id, req.conversation_id)
        return ChatSyncResponse(run_id=run_id, answer=answer,
                                status=status, approval=approval)

    async def _resumed_run_id(user_id: str, conversation_id: str) -> str | None:
        """resume 语义下真正在跑的是那条在途 run：回给前端的 run_id 必须是它，
        否则流式帧按 run_id 归并时接不上，界面上就是一个永不结束的幽灵 run。"""
        cp_mgr = agent.runner.checkpoint
        if cp_mgr is None:
            return None
        cp = await cp_mgr.pending_for_session(session_key(user_id, conversation_id))
        return cp.run_id if cp is not None else None

    @app.get("/sessions")
    async def sessions(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """活跃会话（进程内 SessionManager 的视角）——只报本人名下的那几条。"""
        mine = [s for s in agent.session_manager.ids() if s.startswith(f"{user_id}:")]
        return {"sessions": mine}

    @app.get("/convs")
    async def convs(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """本人的会话列表（服务端是唯一真相，换浏览器只凭 token 就能找回）。"""
        rows = await storage.conversations.list_for(user_id)
        return {"conversations": [{"conversation_id": r["id"], "title": r["title"],
                                   "updated_at": r["updated_at"]} for r in rows]}

    @app.get("/convs/{conversation_id}/messages")
    async def conv_messages(conversation_id: str,
                            user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """回填某个会话的历史：非本人直接回空（不报 403，避免会话存在性探测）。"""
        rows = await storage.messages.history(user_id, conversation_id)
        out = []
        for r in rows:
            entry = {"role": r["role"], "text": r["content"],
                     "attachments": await _attachment_views(
                         storage, user_id, conversation_id, r["attachments"])}
            media = await _render_media_views(storage, r.get("qa"))
            if media:
                entry["media"] = media
            plan_cards = _plan_views(r.get("qa"))
            if plan_cards:
                entry["plan"] = plan_cards
            audit = _plan_audit_view(r.get("qa"))
            if audit:
                entry["plan_audit"] = audit
            out.append(entry)
        return {"messages": out}

    @app.get("/preview/{conversation_id}")
    async def conversation_preview(conversation_id: str, artifact_id: str = "",
                                   user_id: str = Depends(auth.http_user_id)
                                   ) -> dict[str, Any]:
        """渲染前的「编排预览」：把该会话已产出的节点产物整理成可渲染结构。

        只读、不触发任何剪辑、不调 LLM。前端在渲染前展示它，让用户先看编排结果
        （提取内容 / 切分 / 分镜 / 画面 / 声音）再决定是改还是渲。

        ``artifact_id`` 留空时**不要**盲取 ``_default``：``rerun_from`` 分叉会给子 run
        生成 ``art-xxxx`` 作用域（checkpoint.fork），子 run 的产物写在自己那份里。
        盲取 ``_default`` 的后果是分叉后预览显示父 run 的旧编排（或直接空），
        而用户以为看的是这一次的编排——正是「不许偷偷替用户定事」要防的那种误导。

        所以留空时的口径是「本会话**最近写过产物**的那个作用域」：
        按 artifacts.updated_at 找出最新的一份，就是当前正在生效的编排。
        显式给了 artifact_id 就照它读，不做猜测。
        """
        from .preview import build_preview
        sid = storyline_session_id(user_id, conversation_id)

        async def _snapshot(art: str) -> dict[str, Any]:
            return await storage.artifacts(sid, art).snapshot()

        try:
            if artifact_id:
                arts = await _snapshot(artifact_id)
            else:
                arts = await _snapshot("_default")
                if not arts:
                    # 回退：找本会话里最近被写过的那个作用域
                    rows = await storage.db.select(
                        "artifacts", where=[Cond("session_id", "eq", sid)],
                        order_by=["-updated_at"], limit=1)
                    latest = str(rows[0].get("artifact_id") or "") if rows else ""
                    if latest and latest != "_default":
                        arts = await _snapshot(latest)
        except Exception as exc:  # noqa: BLE001 - 读不到就当还没编排
            return {"ready": False, "warnings": [f"读取产物失败：{exc}"],
                    "summary": {}, "material": {}, "shots": {}, "story": [],
                    "timeline": {}, "audio": {}}
        return build_preview(arts)

    def _approval_payload(cp: Any) -> dict[str, Any] | None:
        """挂起态 → 审批视图。**一处拼装**，三个出口共用，形状不会漂。

        为什么要抽出来：这套结构原先只在 `/convs/{id}/runs/active` 里拼一次，
        而 `/chat/sync`（CLI/脚本用）和前端重连都需要同一份。各写一遍必然漂移，
        漂了以后「界面上弹得出来、脚本里拿不到」这种问题很难查。
        形状与 WS 的 ``type=approval`` 帧一致，前端可以拿同一段代码渲染。
        """
        approval = getattr(cp, "approval", None) or {}
        if not approval:
            return None
        return {
            "run_id": cp.run_id,
            "reason": approval.get("reason") or "",
            "calls": approval.get("pending_calls") or [],
            "fallback_options": approval.get("fallback_options") or [],
            "ask": approval.get("ask") or None,
        }

    async def _approval_view_for(ag: Any, user_id: str,
                                 conversation_id: str) -> tuple[str, dict[str, Any] | None]:
        """会话当前的 (status, approval)；没有在途 run 回 ("", None)。

        `/chat/sync` 用它把挂起态回给非 WS 客户端——answer 里那句「已暂停」不够，
        脚本拿不到选项就等于没法答。
        """
        cp_mgr = ag.runner.checkpoint
        if cp_mgr is None:
            return "", None
        sid = session_key(user_id, conversation_id)
        cp = await cp_mgr.pending_for_session(sid)
        if cp is None:
            cp = await _awaiting_approval_for_session(cp_mgr, sid)
        if cp is None:
            return "", None
        return str(cp.status or ""), _approval_payload(cp)

    @app.get("/convs/{conversation_id}/runs/active")
    async def conv_active_run(conversation_id: str,
                              user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """查某会话是否有在途 run（前端刷新后判断是否显示"任务运行中"）。

        挂起态的 run 也在这里回：``approval`` 字段带上待批动作与结构化提问。
        前端刷新/重连后靠它把选项卡**重新弹出来**——否则用户会看到一个
        「等待确认」但没有任何可点东西的界面（审批原先不落库，刷新即丢）。
        """
        cp_mgr = agent.runner.checkpoint
        if cp_mgr is None:
            return {"run": None}
        sid = session_key(user_id, conversation_id)
        cp = await cp_mgr.pending_for_session(sid)
        if cp is None:
            # 挂起态不属于「running」，所以要单独再看一眼有没有等确认的 run
            cp = await _awaiting_approval_for_session(cp_mgr, sid)
        if cp is None:
            return {"run": None}
        view: dict[str, Any] = {"run_id": cp.run_id, "status": cp.status,
                                "iteration": cp.iteration, "message": cp.message}
        payload = _approval_payload(cp)
        if payload is not None:
            view["approval"] = payload
        return {"run": view}

    async def _awaiting_approval_for_session(cp_mgr: Any, sid: str) -> Any:
        """会话里正等着用户确认的 run（挂起态不在 running 里，要单独查一眼）。"""
        try:
            return await cp_mgr.awaiting_approval_for_session(sid)
        except Exception:  # noqa: BLE001 - 查询失败不该让这个只读端点 500
            return None

    # ---- 执行记录 / 时间旅行 -------------------------------------------------
    #
    # 一个 run = 一次 AgentOnceRun。指针行答「到哪了」，entries 链答「走过哪些一致点」，
    # 这两条只读端点就是前端/CLI 的分叉点选择面；fork/resume 不在这里执行，而是投一帧带
    # action 的 MQ 消息——流式回投、同会话串行、错误回执全部复用 /chat 那一条路。

    def _run_view(row: dict[str, Any]) -> dict[str, Any]:
        plan = row.get("plan") or {}
        return {"run_id": row["run_id"], "session_id": row["session_id"],
                "status": row.get("status"), "iteration": int(row.get("iteration") or 0),
                "head_seq": int(row.get("head_seq") or 0),
                "message": row.get("message", ""),
                "artifact_id": (row.get("scope") or {}).get("artifact_id", ""),
                "forked_from": row.get("forked_from"),
                "forked_at_seq": row.get("forked_at_seq"),
                # 计划门谱系：执行 run 指回规划 run；规划 run 带待确认的卡；
                # 对账结论挂在执行 run 自己的行上，面板按 run 取角标数据。
                "plan_run_id": row.get("plan_run_id"),
                "has_plan_cards": bool(plan.get("candidates")),
                "plan_audit": plan.get("audit") or {},
                "created_at_ms": int(row.get("created_at_ms") or 0),
                "updated_at_ms": int(row.get("updated_at_ms") or 0)}

    async def _owned_run_row(run_id: str, user_id: str) -> dict[str, Any]:
        """取本人名下的 run 指针行。别人的 run 按不存在处理——归属校验不该泄露它的存在性。"""
        cp_mgr = agent.runner.checkpoint
        if cp_mgr is None:
            raise HTTPException(503, "未配置 checkpoint，没有执行记录可查")
        row = await cp_mgr.row(run_id)
        if row is None or not str(row["session_id"]).startswith(f"{user_id}:"):
            raise HTTPException(404, f"没有这个执行记录：{run_id}")
        return row

    @app.get("/convs/{conversation_id}/runs")
    async def conv_runs(conversation_id: str,
                        user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """本会话的全部执行（含已完成的与分叉出来的），最近的在前——时间旅行面板的数据源。"""
        cp_mgr = agent.runner.checkpoint
        if cp_mgr is None:
            return {"runs": []}
        rows = await cp_mgr.list_for_session(session_key(user_id, conversation_id))
        return {"runs": [_run_view(r) for r in rows]}

    @app.get("/runs/{run_id}")
    async def run_detail(run_id: str,
                         user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        return {"run": _run_view(await _owned_run_row(run_id, user_id))}

    @app.get("/runs/{run_id}/history")
    async def run_history(run_id: str,
                          user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """这个 run 走过的一致点链（seq / iteration / delta 还是 full / 增量条数）。"""
        await _owned_run_row(run_id, user_id)
        return {"run_id": run_id,
                "points": await agent.runner.checkpoint.history(run_id)}

    @app.post("/runs/{run_id}/fork", response_model=ChatQueuedResponse)
    async def run_fork(run_id: str, req: RunForkRequest,
                       user_id: str = Depends(auth.http_user_id)) -> ChatQueuedResponse:
        """回到 at_seq 开一条新执行：换新产物作用域，上游产物整份复制后按需作废要重跑的节点。

        ``rerun_nodes`` 给了剪辑节点名时，其全部下游产物一并作废——否则服务端拦截器会
        认为它们已完成，恰好跳过用户要求重做的那一步。契约没接上时退化成只作废自己。
        """
        row = await _owned_run_row(run_id, user_id)
        sid = str(row["session_id"])
        _, conversation_id = sid.split(":", 1)
        invalidate: set[str] = set()
        for node in req.rerun_nodes:
            invalidate |= (editing_contract.downstream(node) if editing_contract is not None
                           else {node})
        child_run_id = uuid.uuid4().hex[:12]
        await mq.publish(topic, sid, {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message": req.message or row.get("message", ""),
            "run_id": child_run_id,
            "action": {"op": "fork", "run_id": run_id, "at_seq": req.at_seq,
                       "invalidate": sorted(invalidate)},
        })
        return ChatQueuedResponse(run_id=child_run_id, status="queued")

    @app.post("/runs/{run_id}/resume", response_model=ChatQueuedResponse)
    async def run_resume(run_id: str, req: RunResumeRequest,
                         user_id: str = Depends(auth.http_user_id)) -> ChatQueuedResponse:
        """续跑这条未完成的执行；at_seq 给定则先回到那个一致点（崩溃恢复与时间旅行同一入口）。"""
        row = await _owned_run_row(run_id, user_id)
        sid = str(row["session_id"])
        _, conversation_id = sid.split(":", 1)
        await mq.publish(topic, sid, {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message": row.get("message", ""),
            "run_id": run_id,
            "action": {"op": "resume", "run_id": run_id, "at_seq": req.at_seq},
        })
        return ChatQueuedResponse(run_id=run_id, status="queued")

    @app.post("/runs/{run_id}/approve", response_model=ChatQueuedResponse)
    async def run_approve(run_id: str, req: RunApproveRequest,
                         user_id: str = Depends(auth.http_user_id)) -> ChatQueuedResponse:
        """HITL 审批：对挂起待批的工具调用做出批准/拒绝，从断点续跑同一条 run。

        与 resume 同构——这里只核归属与状态，投一帧带 action 的 MQ 消息，
        真正的续跑在 consumer 里调 ``Agent.approve``。状态不是 awaiting_approval
        说明没有挂起的审批（已批过/已结束/从未挂起），回 409 让前端刷新。
        """
        row = await _owned_run_row(run_id, user_id)
        if str(row.get("status") or "") != STATUS_AWAITING_APPROVAL:
            raise HTTPException(
                409, f"执行 {run_id} 当前不在待审批状态"
                     f"（可能已审批过或已结束），请刷新页面查看最新进展。")
        # 硬校验：答案必须属于**当前这道题**。
        #
        # 真机实测的坏情形（模型自己都发现了，原话「我这边一直只收到「治愈系慢节奏」
        # 这一句，时长选项始终没被选中」）：客户端拿上一题的 key 去答下一题
        # ——问「成片时长想控制在多少」，回的却是风格题的 `healing_*`。
        # 服务端原先照单全收，于是同一道题被反复"答"了四次、run 原地打转，
        # 用户看到的就是「没选完就继续」「做完没反应」。
        #
        # 为什么把守口放在服务端，而不是只修客户端：客户端无论哪个版本、哪条路径
        # （重连、陈旧状态、旧构建产物）出问题，这里都能把「不是这道题的答案」挡住，
        # 并明确告诉用户刷新——而不是静默地让模型空转。
        reject_stale_answer(row, req.decision, req.answers)
        sid = str(row["session_id"])
        _, conversation_id = sid.split(":", 1)
        decision, message, answers = readable_decision(
            row, req.decision, req.message, req.answers)
        await mq.publish(topic, sid, {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message": message,
            "run_id": run_id,
            "action": {"op": "approve", "run_id": run_id,
                       "decision": decision,
                       # 结构化答案原样带上：执行侧据此知道「第几题选了哪个 key」，
                       # 不必再去猜前端拼的那句中文散文。
                       "answers": answers},
        })
        return ChatQueuedResponse(run_id=run_id, status="queued")

    # ---- 计划门（块 B）：候选计划取回 / 确认 / 换一版 --------------------------
    #
    # 与 fork、resume 同一套路：这里不执行任何东西，只投一帧带 action 的 MQ 消息，
    # 于是流式回投、同会话串行、错误回执全部复用聊天那条链路。
    # 关键口径：**计划本体一律从服务端的指针行取**，前端回传的只有「选了哪张卡、
    # 动了哪些开关、另填了什么」——用户无从伪造一份没过四重校验的承诺。

    def _conv_of(row: dict[str, Any]) -> str:
        sid = str(row["session_id"])
        _, conversation_id = sid.split(":", 1)
        return conversation_id

    @app.get("/plans/{plan_run_id}")
    async def plan_detail(plan_run_id: str,
                          user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """这条规划 run 产出的候选计划（刷新后重放计划卡的数据源）。"""
        row = await _owned_run_row(plan_run_id, user_id)
        plan = row.get("plan") or {}
        return {"plan_run_id": plan_run_id, "status": row.get("status"),
                "message": row.get("message", ""),
                "plans": plan.get("candidates") or [],
                "warnings": plan.get("warnings") or []}

    @app.post("/plans/{plan_run_id}/confirm", response_model=ChatQueuedResponse)
    async def plan_confirm(plan_run_id: str, req: PlanConfirmRequest,
                           user_id: str = Depends(auth.http_user_id)) -> ChatQueuedResponse:
        """按确认帧开一条执行 run（Run B）：这里只核归属与「选中的卡存不存在」，
        枚举反查、跳过合法性、自定义上限与转义全部在 ``PlanGate.validate_execute`` 里做。"""
        row = await _owned_run_row(plan_run_id, user_id)
        candidates = (row.get("plan") or {}).get("candidates") or []
        if not candidates:
            raise HTTPException(
                409, f"规划 {plan_run_id} 没有已校验的候选计划"
                     f"（规划轮未提交成功或该 run 已被清理）：请重新描述诉求。")
        ids = [str(c.get("plan_id") or "") for c in candidates]
        if req.selected_plan not in ids:
            raise HTTPException(400, f"选中的计划「{req.selected_plan}」不在候选里"
                                     f"（可选：{ids}）")
        run_id = uuid.uuid4().hex[:12]
        await mq.publish(topic, str(row["session_id"]), {
            "user_id": user_id,
            "conversation_id": _conv_of(row),
            "message": req.message,
            "run_id": run_id,
            "action": {"op": "execute_plan", "plan_run_id": plan_run_id,
                       "frame": {"selected_plan": req.selected_plan,
                                 "param_finals": req.param_finals,
                                 "skips": req.skips,
                                 "overrides": req.overrides}},
        })
        return ChatQueuedResponse(run_id=run_id, status="queued")

    @app.post("/plans/{plan_run_id}/revise", response_model=ChatQueuedResponse)
    async def plan_revise(plan_run_id: str, req: PlanReviseRequest,
                          user_id: str = Depends(auth.http_user_id)) -> ChatQueuedResponse:
        """「换一版」：带着用户的原话再跑一轮规划，新卡与旧卡必须有可辨别的差异。

        输入沿用那条规划 run 的原话（不是前端回传）——否则换一版等于让用户重打一遍。
        ``revise_of`` 指着被改的那张卡：新规划轮读的是会话历史，那里只有上一轮的**文字**
        摘要、没有卡面步骤，不带上它就无从做出「可辨别的差异」（真机实测于是只口头描述
        另一版而不再出卡）。归属已由 ``_owned_run_row`` 核过。
        """
        row = await _owned_run_row(plan_run_id, user_id)
        run_id = uuid.uuid4().hex[:12]
        await mq.publish(topic, str(row["session_id"]), {
            "user_id": user_id,
            "conversation_id": _conv_of(row),
            "message": row.get("message", ""),
            "run_id": run_id,
            "action": {"op": "plan", "feedback": req.feedback,
                       "revise_of": plan_run_id},
        })
        return ChatQueuedResponse(run_id=run_id, status="queued")

    @app.post("/convs/rename")
    async def conv_rename(req: RenameRequest,
                          user_id: str = Depends(auth.http_user_id)) -> dict[str, str]:
        """命名/改名。第一条消息之前也可能先起名，所以行不存在时顺手记下归属。"""
        await storage.conversations.ensure(
            user_id, req.conversation_id, (req.title or "").strip()[:60] or "新对话")
        return {"status": "ok"}

    @app.delete("/convs/{conversation_id}")
    async def conv_drop(conversation_id: str,
                        user_id: str = Depends(auth.http_user_id)) -> dict[str, str]:
        await storage.conversations.claim(user_id, conversation_id)
        await storage.db.delete("messages", where={"conv_id": conversation_id})
        await storage.conversations.drop(user_id, conversation_id)
        return {"status": "ok"}

    @app.post("/upload")
    async def upload(request: Request, conversation_id: str, filename: str,
                     user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """素材上传：字节直写对象存储 + materials 登记，回 material_id 与可播放 URL。

        请求体为文件本体（非 multipart，免 python-multipart 依赖）。入库本身走
        `ingest.ingest_bytes`——链接取料与 Agent 工具共用同一份实现（spec §5 步骤 1-4），
        所以后续 load_media 只能凭 material_id 取字节，路径不再是身份证。
        归属人由 token 反查，客户端再怎么写都改不掉。
        """
        try:
            return await ingest_bytes(
                storage, request.stream(), filename=filename,
                user_id=user_id, conversation_id=conversation_id,
                origin="upload", max_bytes=max_upload_mb * 1024 * 1024)
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e

    # ---- 分片续传：整文件一次 POST 传不动的大素材，切成等长分片搬运 ----

    @app.post("/upload/init")
    async def upload_init(conversation_id: str, filename: str, size: int,
                          part_size: int | None = None, sha256: str = "",
                          user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """定下切法并回 upload_id（含权威 part_size/part_count）。

        同参数重复调用会**接回**上次那条还在传的会话并告知已有哪几片——这就是续传的
        起手式：刷新页面或断网重连后客户端只需重发一次 init。
        """
        try:
            return await uploads.init_upload(
                storage, user_id=user_id, conversation_id=conversation_id,
                filename=filename, size=size, part_size=part_size, sha256=sha256,
                max_bytes=max_upload_mb * 1024 * 1024)
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e

    @app.put("/upload/part")
    async def upload_part(request: Request, upload_id: str, part: int,
                          sha256: str = "",
                          user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """收第 N 片：请求体是这一片的原始字节（与 /upload 同样不做 multipart）。

        片长由账本推算并当场核验，客户端可选带该片 sha256；不符则退回 422 要求重传
        这一片，绝不留下坏片等着拼成坏素材。
        """
        try:
            return await uploads.put_part(
                storage, user_id=user_id, upload_id=upload_id, part=part,
                chunks=request.stream(), sha256=sha256)
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e

    @app.get("/upload/status")
    async def upload_status_ep(upload_id: str,
                               user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """进度与缺口：missing_parts 就是「接着该传哪几片」，以桶的列举为准。"""
        try:
            return await uploads.upload_status(storage, user_id=user_id, upload_id=upload_id)
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e

    @app.post("/upload/complete")
    async def upload_complete(request: Request, upload_id: str,
                              user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """收齐后拼成一条素材：按序取回分片喂 ingest_bytes，与 /upload 同一个入库出口。

        请求体可选 ``{"parts_sha256": ["<64hex>", …]}``：客户端**本地文件**按序逐片算出
        的摘要，条数必须等于 part_count。给了就逐片比对，对不上作废那几片并回 422；
        不给（脚本、老客户端）则只核片长与总长。缺片回 409（账本不动，补齐可再试）；
        成功后分片即刻删除，重复调用幂等回同一条素材。
        """
        parts: list[str] | None = None
        raw = await request.body()
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError as e:
                raise HTTPException(400, f"complete 的请求体要么为空，要么是 JSON：{e}") from e
            if not isinstance(body, dict):
                raise HTTPException(400, 'complete 的请求体要是 {"parts_sha256": […]} 这样的对象')
            got = body.get("parts_sha256")
            if got is not None:
                if not isinstance(got, list) or any(not isinstance(x, str) for x in got):
                    raise HTTPException(400, "parts_sha256 要是字符串数组（每项 64 位十六进制）")
                parts = got
        try:
            return await uploads.complete_upload(
                storage, user_id=user_id, upload_id=upload_id, parts_sha256=parts,
                max_bytes=max_upload_mb * 1024 * 1024)
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e

    @app.post("/upload/abort")
    async def upload_abort(upload_id: str,
                           user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """用户取消：把已传的分片从桶里删净并作废这条账，不留占位的孤儿字节。"""
        try:
            return await uploads.abort_upload(storage, user_id=user_id, upload_id=upload_id)
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e

    @app.get("/materials")
    async def list_materials(user_id: str = Depends(auth.http_user_id),
                             conversation_id: str | None = None) -> dict[str, Any]:
        """列出当前用户的所有素材（素材库面板用）。可选按会话过滤。"""
        rows = await storage.materials.list_visible(
            user_id, conversation_id or None, kinds=("video", "audio", "image"))
        items = []
        for r in rows:
            try:
                url = await storage.objects.presign_get(r["object_key"])
            except Exception:  # noqa: BLE001
                url = None
            items.append({
                "material_id": r["id"], "filename": r["filename"], "kind": r["kind"],
                "bytes": r["bytes"], "duration": r.get("duration_sec"),
                "url": url, "origin": r.get("origin", "upload"),
            })
        return {"materials": items}

    @app.delete("/materials/{material_id}")
    async def delete_material(material_id: str,
                              user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """删除一个素材：先删 MinIO 对象，再删 PG 行（归属校验在 drop 里）。"""
        rows = await storage.materials.db.select(
            storage.materials.table, where={"id": material_id, "owner_user_id": user_id})
        if not rows:
            raise HTTPException(404, "素材不存在或不属于当前用户")
        object_key = rows[0].get("object_key")
        if object_key:
            try:
                await storage.objects.delete(object_key)
            except Exception:  # noqa: BLE001
                pass  # 对象删不掉不阻断：PG 行删了就不会再被引用
        n = await storage.materials.drop(user_id, material_id)
        return {"deleted": n, "material_id": material_id}

    @app.get("/bgm")
    async def list_bgm(q: str | None = None,
                       user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """BGM 曲库列表（音乐库面板用）：只返回本人导入的歌曲。"""
        rows = await storage.materials.list_visible(
            user_id, None, origin="bgm", kinds=("audio",))
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r["filename"] or "").lower()]
        items = []
        for r in rows:
            try:
                url = await storage.objects.presign_get(r["object_key"])
            except Exception:  # noqa: BLE001
                url = None
            items.append({
                "material_id": r["id"], "filename": r["filename"],
                "bytes": r["bytes"], "duration": r.get("duration_sec"), "url": url,
            })
        return {"bgm": items, "total": len(items)}

    @app.delete("/bgm/{material_id}")
    async def delete_bgm(material_id: str,
                         user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """删除本人导入的 BGM 曲库中的一首歌。"""
        rows = await storage.materials.db.select(
            storage.materials.table,
            where={"id": material_id, "origin": "bgm", "owner_user_id": user_id})
        if not rows:
            raise HTTPException(404, "曲库中没有这首歌或不属于你")
        object_key = rows[0].get("object_key")
        if object_key:
            try:
                await storage.objects.delete(object_key)
            except Exception:  # noqa: BLE001
                pass
        n = await storage.materials.drop(user_id, material_id)
        return {"deleted": n, "material_id": material_id}

    # 音乐源优先级：joox 首选，netease 降级，bilibili 兜底
    _BGM_SOURCES = ["joox", "netease", "bilibili"]

    def _gd_normalize_tracks(data, source: str) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            return []
        return [{"track_id": t.get("id", ""), "name": t.get("name", ""),
                 "artist": ", ".join(t.get("artist", [])) if isinstance(t.get("artist"), list) else str(t.get("artist", "")),
                 "album": t.get("album", ""), "pic_id": t.get("pic_id", ""),
                 "lyric_id": t.get("lyric_id", ""), "source": t.get("source", source)}
                for t in data]

    async def _gd_get_url_fallback(track_id: str, source: str, name: str = "",
                                   artist: str = "") -> str:
        """获取播放 URL，当前源为空时自动降级到其他源重新搜索同名歌曲。"""
        data = await asyncio.to_thread(_gd_music_get, {
            "types": "url", "source": source, "id": track_id, "br": 320})
        url = data.get("url", "") if isinstance(data, dict) else ""
        if url:
            return url
        # 当前源 URL 为空，降级到其他源搜同名歌
        if name:
            query = f"{name} {artist}".strip() if artist else name
            for alt in _BGM_SOURCES:
                if alt == source:
                    continue
                try:
                    sdata = await asyncio.to_thread(_gd_music_get, {
                        "types": "search", "source": alt,
                        "name": query, "count": 3, "pages": 1})
                    for t in (sdata if isinstance(sdata, list) else []):
                        tid = t.get("id", "")
                        if not tid:
                            continue
                        udata = await asyncio.to_thread(_gd_music_get, {
                            "types": "url", "source": alt, "id": tid, "br": 320})
                        u = udata.get("url", "") if isinstance(udata, dict) else ""
                        if u:
                            return u
                except Exception:  # noqa: BLE001
                    continue
        return ""

    @app.get("/bgm/search")
    async def bgm_search(q: str = "",
                         user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """在线搜索音乐（多源聚合：joox 首选 + netease 降级）。"""
        q = (q or "").strip()
        if not q:
            return {"tracks": [], "total": 0, "hint": "请输入歌名或歌手名搜索"}
        try:
            all_tracks: list[dict[str, Any]] = []
            for src in _BGM_SOURCES:
                try:
                    data = await asyncio.to_thread(_gd_music_get, {
                        "types": "search", "source": src,
                        "name": q, "count": 10, "pages": 1})
                    all_tracks.extend(_gd_normalize_tracks(data, src))
                except Exception:  # noqa: BLE001
                    continue
                if len(all_tracks) >= 20:
                    break
            return {"tracks": all_tracks[:20], "total": len(all_tracks[:20]),
                    "hint": f"搜索「{q}」"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"搜索失败：{e}") from e

    @app.get("/bgm/url")
    async def bgm_url(track_id: str,
                      source: str = "joox",
                      name: str = "",
                      artist: str = "",
                      user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """获取歌曲播放 URL（当前源为空时自动降级到其他源）。"""
        try:
            url = await _gd_get_url_fallback(track_id, source, name, artist)
            if not url:
                raise HTTPException(404, "所有源均无法获取播放链接，该曲目可能受版权限制")
            return {"url": url}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"获取播放链接失败：{e}") from e

    @app.post("/bgm/import")
    async def bgm_import(request: Request,
                         user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """下载一首曲子并导入本人的 BGM 曲库（自动多源降级获取播放链接）。

        导入后的歌曲归属当前用户，其他用户不可见。
        """
        body = await request.json()
        track_id = body.get("track_id", "")
        source = body.get("source", "joox")
        name = body.get("name", "unknown")
        artist = body.get("artist", "")
        if not track_id:
            raise HTTPException(400, "缺少 track_id")
        filename = f"{artist} - {name}.mp3" if artist else f"{name}.mp3"
        try:
            play_url = await _gd_get_url_fallback(track_id, source, name, artist)
            if not play_url:
                raise HTTPException(400, "所有源均无法获取播放链接，该曲目可能受版权限制")
            raw = await asyncio.to_thread(_gd_download, play_url)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"下载失败：{e}") from e

        async def _chunks():
            yield raw

        try:
            result = await ingest_bytes(
                storage, _chunks(),
                filename=filename,
                user_id=user_id,
                conversation_id=None,
                origin="bgm",
            )
        except IngestRejected as e:
            raise HTTPException(e.status, e.message) from e
        return {
            "material_id": result["material_id"],
            "filename": filename,
            "kind": result.get("kind", "audio"),
            "bytes": result.get("bytes", 0),
            "duration": result.get("duration"),
            "url": result.get("url"),
        }

    @app.post("/fetch_media")
    async def fetch_media_ep(req: FetchMediaRequest,
                             user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """按链接取素材：直链/页面嗅探/yt-dlp 兜底 → 与 /upload 同构的素材响应。

        落库走 `media_fetch.fetch_media`，与 Agent 的 fetch_media 工具是同一份实现，
        所以爬来的素材和上传的素材在 materials 里只差一个 origin 标记。
        """
        try:
            return await fetch_media(
                storage, req.url, user_id=user_id,
                conversation_id=req.conversation_id, policy=fetch_policy)
        except FetchRejected as e:
            raise HTTPException(e.status, e.message) from e

    def _runtime_llm() -> Any:
        """自检要打在真正在用的那个 client 上：Agent 注入了就用它，否则用共享默认。

        Agent 本身不存 llm——构造时就交给了逐轮复用的 runner，所以顺着 runner 找；
        只看 agent.llm 会永远拿到 None，面板于是报的是另一个 client 的配置。
        """
        llm = getattr(getattr(agent, "runner", agent), "llm", None)
        return llm if hasattr(llm, "complete") else get_default_llm()

    async def _settings_view(user_id: str) -> dict[str, Any]:
        """密钥现状的对外形态：只有掩码与来源，明文一次都不出门。"""
        llm = _runtime_llm()
        row = await storage.secrets.row(user_id, secrets.API_KEY_NAME)
        stored = (row or {}).get("value", "")
        key, source = await secrets.resolve_api_key(user_id,
                                                    fallback=getattr(llm, "api_key", ""))
        updated = (row or {}).get("updated_at")
        main_model = await secrets.get_user_key(user_id, secrets.MAIN_MODEL_KEY)
        main_base = await secrets.get_user_key(user_id, secrets.MAIN_BASE_KEY)
        judge_base = await secrets.get_user_key(user_id, secrets.JUDGE_BASE_KEY)
        judge_model = await secrets.get_user_key(user_id, secrets.JUDGE_MODEL_KEY)
        judge_key_row = await storage.secrets.row(user_id, secrets.JUDGE_API_KEY_NAME)
        judge_key = (judge_key_row or {}).get("value", "")
        return {
            "api_key": {
                "configured": bool(stored),
                "masked": secrets.mask(stored),
                "updated_at": updated.isoformat() if updated else "",
                "effective": bool(key),
                "source": source,
            },
            "main_override": {
                "model": main_model or "",
                "base_url": main_base or "",
            },
            "judge": {
                "base_url": judge_base or "",
                "model": judge_model or "",
                "configured": bool(judge_base and judge_model and judge_key),
                "masked": secrets.mask(judge_key) if judge_key else "",
            },
            "model": getattr(llm, "model", DEFAULT_MODEL),
            "base_url": getattr(llm, "base_url", DEFAULT_BASE_URL),
            # 思考模式是「同一条链路上的速度开关」——面板里得看得见当前值
            "thinking": bool(getattr(llm, "thinking", DEFAULT_THINKING)),
            "hot_reload_sec": secrets.CACHE_TTL_SEC,
            "backend": storage.backend,
        }

    @app.get("/settings")
    async def settings_get(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """模型配置现状（密钥掩码 + 生效来源 + 模型地址）。"""
        return await _settings_view(user_id)

    @app.post("/settings/api-key")
    async def settings_api_key(req: ApiKeyRequest,
                               user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """保存/清除模型密钥：写进 PG 即生效，两个服务各自热读，不重启任何进程。

        值只在写入与掩码比较时存在，响应里出去的一律是掩码。
        """
        value = (req.api_key or "").strip()
        if value:
            if any(c.isspace() for c in value):
                raise HTTPException(400, "密钥格式不对：不能包含空格或换行")
            if len(value) > 300:
                raise HTTPException(400, "密钥过长：请确认只粘贴了 key 本身")
            await storage.secrets.put(user_id, secrets.API_KEY_NAME, value)
        else:
            await storage.secrets.drop(user_id, secrets.API_KEY_NAME)
        secrets.invalidate(user_id)          # 本进程立即生效，另一进程等 TTL 过期
        view = await _settings_view(user_id)
        view["status"] = "saved" if value else "cleared"
        return view

    @app.post("/settings/test")
    async def settings_test(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """连通性自检：主模型真发文本+带图两路;判断模型(若配置)真发一次裁决。"""
        out = await model_probe.self_check(storage, user_id, _runtime_llm())
        out["judge"] = await judge_mod.test_judge()
        return out

    @app.post("/settings/model")
    async def settings_model(req: MainModelRequest,
                             user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """主模型名/地址的按用户覆盖：空串 = 清除恢复默认。保存即生效(热读)。"""
        for key, value in ((secrets.MAIN_MODEL_KEY, req.model.strip()),
                           (secrets.MAIN_BASE_KEY, req.base_url.strip())):
            if value:
                await secrets.put_user_key(user_id, key, value)
            else:
                await secrets.drop_user_key(user_id, key)
        secrets.invalidate(user_id)
        return await _settings_view(user_id)

    @app.post("/settings/judge")
    async def settings_judge(req: JudgeModelRequest,
                             user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """判断模型(Jev 类)三件套：base_url/model 必填成对;api_key 空串 = 清除。"""
        base = req.base_url.strip().rstrip("/")
        model = req.model.strip()
        if bool(base) != bool(model):
            raise HTTPException(400, "base_url 与 model 必须成对设置或成对清除")
        if base:
            await secrets.put_user_key(user_id, secrets.JUDGE_BASE_KEY, base)
            await secrets.put_user_key(user_id, secrets.JUDGE_MODEL_KEY, model)
            if req.api_key.strip():
                await secrets.put_user_key(user_id, secrets.JUDGE_API_KEY_NAME,
                                           req.api_key.strip())
        else:
            await secrets.drop_user_key(user_id, secrets.JUDGE_BASE_KEY)
            await secrets.drop_user_key(user_id, secrets.JUDGE_MODEL_KEY)
            await secrets.drop_user_key(user_id, secrets.JUDGE_API_KEY_NAME)
        jbase = await secrets.get_user_key(user_id, secrets.JUDGE_BASE_KEY)
        jmodel = await secrets.get_user_key(user_id, secrets.JUDGE_MODEL_KEY)
        jrow = await storage.secrets.row(user_id, secrets.JUDGE_API_KEY_NAME)
        jkey = (jrow or {}).get("value", "")
        return {"judge": {"base_url": jbase, "model": jmodel,
                          "configured": bool(jbase and jmodel and jkey),
                          "masked": secrets.mask(jkey) if jkey else ""}}

    @app.post("/settings/test2")

    @app.websocket("/ws/{conversation_id}")
    async def ws_endpoint(websocket: WebSocket, conversation_id: str) -> None:
        """会话窗口长连接：注册进 Connection Manager，接收 OutBound 回投的流式结果。

        浏览器 WebSocket 不能带自定义头，所以凭证走 `?token=`；user_id 由它反查，
        路径里不再有 user_id 这一段——客户端再也无法指名别人的身份。

        新连接注册后重放缓冲事件——用户刷新页面后能补看到断连期间错过的进度。
        """
        user_id = await auth.ws_user_id(websocket)
        if user_id is None:
            return                      # 握手已被拒绝，不 accept
        await websocket.accept()
        sid = session_key(user_id, conversation_id)
        connections.connect(sid, websocket)
        try:
            await websocket.send_json({"type": "connected", "session_id": sid})
            await connections.replay_to(sid, websocket)
            while True:
                # 客户端心跳/消息暂不处理，仅维持连接（断开由 receive 抛异常感知）。
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):  # noqa: PERF203
            pass
        finally:
            connections.disconnect(sid, websocket)

    # ---- 时间线编辑器：持久化 + 直接渲染（不走 AI） ----

    def _epoch(value: Any) -> Any:
        """timestamptz（PG 读回 datetime / 内存引擎读回 aware datetime）→ epoch 浮点。

        原实现把转换塞在 SQL 里（``extract(epoch from updated_at)::float8``），
        那是 PG 专有写法，也是这几个端点不能在内存引擎上跑的原因之一。
        挪到 Python 侧后两个引擎同一份代码。
        """
        try:
            return value.timestamp()
        except AttributeError:
            return value

    @app.get("/timelines")
    async def timelines_list(user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        rows = await storage.timelines.list_for_user(user_id)
        out = [{k: v for k, v in r.items()
                if k in ("id", "name", "conv_id", "video_url", "duration_sec",
                         "updated_at")}
               for r in rows]
        for r in out:
            r["updated_at"] = _epoch(r.get("updated_at"))
        return {"timelines": out}

    @app.post("/timelines")
    async def timeline_save(request: Request,
                            user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        body = await request.json()
        tl_id = body.get("id") or str(uuid.uuid4())
        saved = await storage.timelines.put(
            tl_id=str(tl_id), user_id=user_id, conv_id=body.get("conv_id"),
            name=body.get("name", "未命名时间线"),
            payload=body.get("payload") or {},
            video_url=body.get("video_url"), duration_sec=body.get("duration_sec"))
        return {"id": str(saved.get("id") or tl_id), "status": "saved"}

    @app.get("/timelines/{tl_id}")
    async def timeline_get(tl_id: str,
                           user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        row = await storage.timelines.get_for_user(tl_id, user_id)
        if row is None:
            raise HTTPException(404, "时间线不存在")
        row = dict(row)
        row["updated_at"] = _epoch(row.get("updated_at"))
        row.pop("user_id", None)
        return row

    @app.delete("/timelines/{tl_id}")
    async def timeline_delete(tl_id: str,
                              user_id: str = Depends(auth.http_user_id)) -> dict[str, str]:
        await storage.timelines.drop_for_user(tl_id, user_id)
        return {"status": "deleted"}

    @app.post("/render_direct")
    async def render_direct(request: Request,
                            user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """直接调 render_video 提交渲染，不走 AI Agent。

        与工具面同一条路：请求不再等整部片子跑完，返回体是渲染任务视图
        （``render.status`` 为 queued/running 时前端改轮询 ``GET /render_status``）。
        body 可选 ``wait_sec``：短片想让一次请求直接拿到成片时，内联等这么久。
        """
        body = await request.json()
        timeline = body.get("timeline")
        conv_id = body.get("conv_id", "")
        if not timeline:
            raise HTTPException(400, "缺少 timeline")
        reg = getattr(getattr(agent, "runner", agent), "registry", None)
        if reg is None or "render_video" not in reg:
            raise HTTPException(503, "渲染工具不可用")
        artifact_id = str(uuid.uuid4())[:8]
        try:
            try:
                wait_sec = float(body.get("wait_sec", 0) or 0)
            except (TypeError, ValueError):
                wait_sec = 0.0
            raw = await reg.execute("render_video", {
                "timeline": timeline,
                "user_id": user_id,
                "conversation_id": conv_id,
                "artifact_id": artifact_id,
                "wait_sec": wait_sec,
            })
        except Exception as e:
            raise HTTPException(500, f"渲染失败：{e}") from e
        if is_tool_error(raw):
            raise HTTPException(500, str(raw))
        result = json.loads(raw) if isinstance(raw, str) else raw
        return result

    @app.get("/render_status")
    async def render_status(artifact_id: str, conv_id: str = "",
                            user_id: str = Depends(auth.http_user_id)
                            ) -> dict[str, Any]:
        """渲染任务轮询口：进度、以及 done 时的成片（与当轮卡片同形状）。

        作用域按调用者身份找回（与 Storyline 侧一致），所以别人会话里的渲染任务
        在这里查不到——传错 artifact_id 与无权访问是同一个 404，不区分。
        """
        sid = storyline_session_id(user_id, conv_id)
        view = await storage.render_view(sid, artifact_id)
        if view is None:
            raise HTTPException(404, "没有这个渲染任务")
        return view

    @app.get("/latest_timeline")
    async def latest_timeline(conv_id: str = "",
                              user_id: str = Depends(auth.http_user_id)) -> dict[str, Any]:
        """从 artifacts 表读当前会话最新的时间线 JSON。"""
        if not conv_id:
            raise HTTPException(400, "缺少 conv_id")
        sid = storyline_session_id(user_id, conv_id)
        # 走通用 repo（原来用 storage.db._run，那是 PG 专有，memory 后端必 500）。
        # 需要的是「这个会话里最近写入的一条时间线产物」，所以按 updated_at 倒序取第一。
        rows = await storage.db.select(
            "artifacts",
            where=[Cond("session_id", "eq", sid),
                   Cond("node", "in", ["plan_timeline_pro", "plan_timeline",
                                       "plan_timeline_ai_transition"])],
            order_by=["-updated_at"], limit=1)
        if not rows:
            raise HTTPException(404, "当前会话没有时间线产物")
        payload = rows[0]["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        node = rows[0]["node"]
        if isinstance(payload, dict) and "events" not in payload:
            for k in (node, "timeline_pro", "timeline", "timeline_ai_transition"):
                if k in payload and isinstance(payload[k], dict):
                    payload = payload[k]
                    break
        return {"timeline": payload, "source": node}

    # 前端构建产物（Vite → frontend/dist）存在时由本服务托管；
    # mount 放在最后：API 路由先匹配，其余路径落到静态站点（html=True 提供 index.html）。
    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")

    return app
