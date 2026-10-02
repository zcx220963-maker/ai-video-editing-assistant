"""出口接线验证（不联网）：中文名替换器确实挂在「唯一的出路口」上，而不是某个 handler 里。

块 A 的契约是「一张词表 + 一个全局出口」，所以这里判的都是**接线**而不是替换算法
（算法在 test_tool_catalog.py）：

* HTTP：FastAPI 的 `default_response_class` 是 `CatalogJSONResponse`，报错通道共用它；
* WS：`ConnectionManager` 里唯一的 `send_json` 站点在 `_emit` 里，别处不许再直接写连接；
* 装配：`run_server` 在本地工具注册后与 `_startup` 末尾各同步一次词表；
* 前端：`toolLabel` 的第一来源是 display（帧旁路 → /tools 词表），本地表退为兜底，
  界面上不再常驻英文原名，改为「复制机器名」钮。

运行：  python tests/test_display_outlets.py
"""

from __future__ import annotations

import re
import sys as _sys
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_root))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---- HTTP 出口 --------------------------------------------------------------

def case_http() -> None:
    text = src("agent_framework/server.py")
    check("default_response_class=CatalogJSONResponse" in text,
          "FastAPI 默认响应类就是词表出口（新端点自动过，不靠逐个包）")
    check("class CatalogJSONResponse(JSONResponse)" in text
          and "catalog.rewrite_obj(content)" in text,
          "render() 在序列化之前替换对象")
    check("ensure_ascii=False" in text, "中文不被转成 \\uXXXX 出去")
    handlers = re.findall(r"@app\.exception_handler\(([^)]*)\)", text)
    check(any("IntegrityConflict" in h for h in handlers)
          and any("StarletteHTTPException" in h for h in handlers)
          and "app.add_exception_handler(HTTPException, http_error)" in text,
          "报错通道（含 FastAPI 的 HTTPException）共用同一个响应类，没有漏网口")
    check("return JSONResponse(" not in text,
          "没有任何 handler 绕开默认响应类手工 new JSONResponse")
    check('"params_display": get_catalog().params_display()' in text,
          "/tools 整张导出参数标签单源表：界面那张同名表退为兜底")


# ---- WS 出口 --------------------------------------------------------------

def case_ws() -> None:
    cm = src("agent_framework/connection_manager.py")
    sends = [m.start() for m in re.finditer(r"\.send_json\(", cm)]
    body = cm[cm.index("async def _emit"):]
    outside = sum(1 for p in sends if p < cm.index("async def _emit"))
    check(outside == 0 and len(sends) == 4,
          f"Connection Manager 的 send_json 全部在 _emit 之内（共 {len(sends)} 处）")
    check("await self._emit(" in cm and cm.count("await self._emit(ws, payload)") >= 1,
          "route/replay_to 都改走 _emit（直播与刷新重放同一条出口）")
    check("self.sent.append(payload)" in cm and "self._buffer_append(session_id, payload)" in cm,
          "记账与环形缓冲存原帧（出口只改写发出去的那一份）")
    check("StreamRewriter" in cm and "rewriter.flush()" in cm,
          "每条连接各持一个挂起缓冲，stream_end 之前 flush 尾巴")
    emit_body = cm[cm.index("async def _emit"):cm.index("async def replay_to")]
    check('kind == "tool_call"' in emit_body and "catalog.arg_labels(" in emit_body,
          "参数标签挂在同一个出帧站：tool_call 帧整帧带 arg_labels，前端不必再猜")
    server = src("agent_framework/server.py")
    direct = re.findall(r"await websocket\.send_json\(\{", server)
    check(len(direct) == 1, "server.py 里只有建连回执那一帧直发（不含业务文案）")


# ---- 装配接线 --------------------------------------------------------------

def case_assembly() -> None:
    text = src("run_server.py")
    check("from agent_framework.catalog import get_catalog" in text
          and "catalog.update(registry.displays())" in text,
          "词表在装配期由注册表填（三源单汇的那一环）")
    check(text.count("sync_catalog(") >= 3,
          "本地注册后与 _startup 末尾各同步一次（远程节点的 title 要到那一步才拿到）")
    check("skill_displays(skill_loader)" in text, "技能 frontmatter 的 display 也进词表")
    check("参数标签" in text and "params_display()" in text,
          "启动日志报出参数标签表的条数：单源装没装上，运维看得见")


# ---- 前端接线 --------------------------------------------------------------

def case_frontend() -> None:
    text = src("frontend/src/App.vue")
    body = text[text.index("function toolLabel("):]
    body = body[:body.index("\n}\n")]
    check("if (display) return display;" in body,
          "toolLabel 第一来源是调用点带来的 display（帧里的 *_display）")
    check("catalogLabels.value[" in body, "其次查 /tools 灌进来的服务端词表")
    check("TOOL_LABELS[name] || TOOL_LABELS[bare]" in body,
          "本地 TOOL_LABELS 退为兜底，最后才露机器名")
    check("async function ensureCatalog()" in text and "ensureCatalog();" in text,
          "onMounted 预热词表：进度卡不必等用户打开工具库抽屉")
    check('if (k.endsWith("_display")) continue;' in text,
          "参数摘要把旁路位当值用，不额外成一行英文键")
    check("frameLabels[k] || paramLabels.value[k] || ARG_LABELS[k] || k" in text,
          "参数标签三级：帧里的 arg_labels → /tools 单源表 → 本地 ARG_LABELS 兜底 → 参数名")
    check("j.params_display" in text and "arg_labels: p.arg_labels || null" in text,
          "单源表与帧标签都接得住：/tools 灌一次、每条 tool_call 帧自带的优先")
    check("summarizeArgs(m)" in text and "summarizeArgs(m.args)" not in text,
          "模板把整条消息交给摘要函数（只传 args 就拿不到帧标签）")
    check("tool_display: p.tool_display" in text and "pipeTouch(cid, p.tool, { state: \"running\" }, p.tool_display)" in text,
          "tool_call/tool_result 帧把 tool_display 收进气泡与进度卡")
    check('String(n).replace(/^storyline_/, "")' in text and "PIPE_ORDER.includes(n)" in text,
          "分叉/流水线的匹配仍走裸机器名（键位不动，只是显示换掉）")
    check("<span class=\"tt-raw\">" not in text and '{{ t.name }}</span>' not in text,
          "界面上不再常驻英文原名（工具卡片与工具库抽屉的 tt-raw/td-name 已撤）")
    check("copyMachine(" in text and "navigator.clipboard" in text,
          "豁免口径的另一半：代码位不翻译，但配了复制钮取回原文")
    dist = list((ROOT / "frontend" / "dist" / "assets").glob("index-*.js"))
    built = any("tt-copy" in p.read_text(encoding="utf-8")
                and "arg_labels" in p.read_text(encoding="utf-8") for p in dist)
    check(built, "frontend/dist 已按新源码重新构建（含 arg_labels 接线，不是只改了 src）")


def main() -> None:
    for fn in (case_http, case_ws, case_assembly, case_frontend):
        print(f"\n=== {fn.__name__.replace('case_', '')} ===")
        fn()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
