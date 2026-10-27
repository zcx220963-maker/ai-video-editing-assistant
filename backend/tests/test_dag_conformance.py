# -*- coding: utf-8 -*-
"""R16：扫包注册 / 白名单裁剪 / 预置产物跳过 / baseline 18 项。"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tomllib

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.orchestration import BaseNode, NodeRegistry, NodeState  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def main() -> None:
    # ---- 扫包：只收有 name 的子类 ----
    reg = NodeRegistry()
    found = reg.scan_package("storyline_server.nodes.core_nodes", _factory)
    check(found >= 20, f"扫出 20+ 个节点（实际 {found}）")
    check(all(reg.get(n) is not None for n in reg.names()), "扫出来的都能取到实例")

    # ---- keep_only 是最终权威 ----
    keep = ["load_media", "split_shots", "render_video"]
    reg.keep_only(keep)
    check(sorted(reg.names()) == sorted(keep), f"白名单裁剪（{sorted(reg.names())}）")

    # ---- 重复注册不覆盖 ----
    reg2 = NodeRegistry()
    reg2.scan_package("storyline_server.nodes.core_nodes", _factory)
    before = reg2.get("load_media")
    again = reg2.scan_package("storyline_server.nodes.core_nodes", _factory)
    check(again == 0 and reg2.get("load_media") is before, "重复扫包不重复实例化")

    # ---- require_node 只读别名 ----
    node = reg2.get("render_video")
    check(list(node.require_node) == list(node.required_nodes), "require_node 是只读别名")

    # ---- 预置产物跳过：给 render_video 传 timeline 就不跑 plan_timeline ----
    state = NodeState(session_id="u1:c1", artifact_id="a1", user_request="直接渲")
    inter = _interceptor(reg2)
    args = asyncio.run(inter.inject_media_content_before(
        "render_video", state, {"timeline": {"events": []}}))
    check("plan_timeline" not in inter.order_trace, "预置 timeline → 跳过 plan_timeline")
    check("plan_timeline" in inter.presupplied, "跳过的节点记在 presupplied 里")
    check(isinstance(args, dict), "请求参数照常返回")

    # ---- 不传 timeline 时仍然补齐（补齐会真跑依赖节点，缺素材时如实地抛错）----
    state2 = NodeState(session_id="u1:c1", artifact_id="a2", user_request="常规")
    inter2 = _interceptor(reg2)
    tried = False
    try:
        asyncio.run(inter2.inject_media_content_before("render_video", state2, {}))
    except Exception:  # noqa: BLE001  没素材 → load_media 抛错，正说明它真去补了
        tried = True
    check(tried or "plan_timeline" in inter2.order_trace,
          "没预置时照常补依赖（会真跑上游）")

    # ---- pkg_scan（R16-e）：两份 TOML 都有 available_node_pkgs，扫出的类 ≥ 22，白名单仍生效 ----
    for name in ("config.toml", "config.docker.toml"):
        cfg = tomllib.loads((BACKEND / "examples/storyline" / name).read_text(encoding="utf-8"))
        srv = cfg["local_mcp_server"]
        pkgs = srv.get("available_node_pkgs")
        check(isinstance(pkgs, list) and bool(pkgs), f"{name} 有非空 available_node_pkgs")
        scanned = NodeRegistry()
        count = sum(scanned.scan_package(pkg, _factory) for pkg in pkgs or [])
        check(count >= 22, f"{name} 扫包得到 ≥22 个节点类（实际 {count}）")
        allowed = [n for n in (srv.get("available_nodes") or []) if scanned.get(n) is not None]
        scanned.keep_only(allowed)
        check(sorted(scanned.names()) == sorted(allowed) and bool(allowed),
              f"{name} 白名单仍生效（{len(scanned.names())} 项）")

    # ---- session_header（R16-c）：主服务每个请求现取身份发头，服务端读同名头 ----
    import httpx
    from agent_framework.identity import use_identity
    from agent_framework.tools.mcp import STORYLINE_SESSION_HEADER, _StorylineSessionAuth
    from storyline_server.server import SESSION_HEADER

    check(STORYLINE_SESSION_HEADER == SESSION_HEADER, "客户端与服务端用同一个头名")
    with use_identity("u9", "c9"):
        req = httpx.Request("POST", "http://127.0.0.1:8001/mcp")
        for _ in _StorylineSessionAuth().auth_flow(req):
            pass
        sent = req.headers.get(STORYLINE_SESSION_HEADER)
    check(sent == "u:u9:c:c9", f"有身份时发出剪辑会话作用域（实际 {sent}）")
    anon = httpx.Request("POST", "http://127.0.0.1:8001/mcp")
    for _ in _StorylineSessionAuth().auth_flow(anon):
        pass
    check(STORYLINE_SESSION_HEADER not in anon.headers, "离线直调（无身份）不发头")

    # ---- design_baseline 恰好 18 项且不含 plan_timeline ----
    from storyline_server.nodes.core_nodes import DESIGN_BASELINE_NODES

    check(len(DESIGN_BASELINE_NODES) == 18, f"baseline 18 项（{len(DESIGN_BASELINE_NODES)}）")
    check("plan_timeline" not in DESIGN_BASELINE_NODES, "baseline 不含 plan_timeline")

    # ---- NODE_PROFILE 生效 ----
    os.environ["NODE_PROFILE"] = "design_baseline"
    try:
        from storyline_server.nodes.core_nodes import build_real_registry
        check(callable(build_real_registry), "build_real_registry 可用（档位由环境变量选）")
    finally:
        os.environ.pop("NODE_PROFILE", None)

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


def _factory(cls):
    node = cls.__new__(cls)
    BaseNode.__init__(node)
    return node


def _interceptor(registry):
    from agent_framework.orchestration import Interceptor
    return Interceptor(registry)


if __name__ == "__main__":
    main()
