# -*- coding: utf-8 -*-
"""Storyline MCP Server 注册层测试：FastMCP 工具契约 + 会话路由 + 服务端 Store 闭环。

无网络、无外部 client：直接对 make_server() 的 mcp.list_tools()/call_tool() 断言。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.video_editing import ALL_NODE_CLASSES
from agent_framework.storage import build_storage, new_id
from agent_framework.storage.media import kind_of, mime_of
from storyline_server import mediaops
from storyline_server.server import SESSION_HEADER, StorylineServer, make_server
from storyline_server.settings import Settings

FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def _tool_text(res):
    """FastMCP call_tool 的返回 → 文本（不同版本是 [TextContent] 或 (content, ...)）。"""
    blocks = res[0] if isinstance(res, tuple) else res
    return blocks[0].text


def settings_with(tmp: Path, extra: str = "") -> Settings:
    cfg = tmp / "config.toml"
    cfg.write_text(
        f"""
[local_mcp_server]
server_name = "storyline"
port = 8098
{extra}

[storage]
backend = "memory"
workspace_root = "{(tmp / 'workspace').as_posix()}"
cache_root = "{(tmp / 'object_cache').as_posix()}"
""", encoding="utf-8")
    return Settings.load(cfg)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="storyline_srv_"))
    settings = settings_with(tmp)
    storage = build_storage("memory",
                            cache_root=settings.storage.cache_root,
                            workspace_root=settings.storage.workspace_root)
    srv = make_server(settings, storage)

    tools = await srv.mcp.list_tools()
    by_name = {t.name: t for t in tools}
    mock_names = [n.name for n in ALL_NODE_CLASSES]
    check(all(n in by_name for n in mock_names),
          f"{len(mock_names)} 个真实节点全部注册为 MCP 工具（与 mock 同名同 DAG）")
    check("read_node_history" in by_name, "服务端 read_node_history 工具在册")
    check(len(tools) == len(mock_names) + 3,
          f"工具总数 = {len(mock_names)} 节点 + read_node_history + render_status + dag_contract"
          f"（实际 {len(tools)}）")

    gs = by_name["generate_script"].inputSchema["properties"]
    check("custom_script" in gs, "generate_script 契约参数 custom_script 进入 inputSchema")
    for nm in ("session_id", "artifact_id", "user_request"):
        check(all(nm in (by_name[n].inputSchema.get("properties") or {}) for n in mock_names),
              f"所有节点声明公共参数 {nm}")
    check(all("kwargs" not in (t.inputSchema.get("properties") or {}) for t in tools),
          "inputSchema 无杂散 kwargs 字段")
    check(by_name["render_video"].description.startswith("根据时间线渲染成片"),
          "工具描述沿用节点契约")
    check("plan_timeline" in by_name["render_video"].description,
          "描述携带 DAG 前置依赖提示")

    # 中文名上的是 MCP 标准 title：主服务据此建词表，服务端不留第二份作者
    check(by_name["split_shots"].title == "镜头切分"
          and by_name["render_video"].title == "成片渲染",
          f"节点工具带中文 title（实际 {by_name['split_shots'].title!r}/"
          f"{by_name['render_video'].title!r}）")
    check(all(t.title for t in tools if t.name != "dag_contract"),
          "除契约查询口外每个工具都有中文名")
    check(by_name["render_status"].title == "渲染进度查询"
          and by_name["read_node_history"].title == "读取节点产物",
          "两个控制面工具也有中文名（它们会出现在工具链帧里）")

    # 白名单模式：available_nodes 生效
    srv2 = make_server(settings_with(tmp, 'available_nodes = ["load_media", "split_shots"]'),
                       storage)
    tools2 = await srv2.mcp.list_tools()
    names2 = {t.name for t in tools2}
    # 白名单只裁剪辑节点；三个控制面工具（读历史、轮询渲染、取契约）不受它管辖——
    # 契约被裁掉的话主 Agent 侧就连依赖图都不知道了，render_status 被裁掉则模型
    # 收到「去轮询」的指示却没有工具可轮。
    check(names2 == {"load_media", "split_shots", "read_node_history",
                     "render_status", "dag_contract"},
          f"available_nodes 只裁节点、保留控制面工具（实际 {sorted(names2)}）")

    lm_props = by_name["load_media"].inputSchema["properties"]
    lm_ids = lm_props.get("material_ids", {})
    check(lm_ids.get("type") == "array"
          or any(b.get("type") == "array" for b in lm_ids.get("anyOf", [])),
          "load_media 契约参数 material_ids（不再有本地 paths）")
    check("paths" not in lm_props, "inputSchema 里已无 paths：素材只按 material_id 定位")
    check(all("user_id" in (by_name[n].inputSchema.get("properties") or {})
              and "conversation_id" in (by_name[n].inputSchema.get("properties") or {})
              for n in mock_names),
          "所有节点声明 user_id/conversation_id（主 Agent 侧注入身份）")

    # call_tool 闭环：素材进对象存储 → load_media(material_ids) → read_node_history 读回
    one = tmp / "one.mp4"
    mediaops.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-an", str(one))
    mid = new_id("mat", 6)
    key = f"users/u9/convs/c9/{mid}.mp4"
    blob = one.read_bytes()

    async def chunks():
        yield blob

    info = await storage.objects.put(key, chunks(), content_type=mime_of(one.name))
    await storage.users.provision("u9")
    await storage.conversations.ensure("u9", "c9")
    await storage.materials.register("u9", "c9", key, "one.mp4", kind_of(one.name),
                                     bytes_=info.bytes, sha256=info.sha256,
                                     mime=mime_of(one.name), origin="upload",
                                     material_id=mid)
    res = await srv.mcp.call_tool("load_media", {
        "material_ids": [mid],
        "session_id": "u9:c9", "artifact_id": "artX", "user_request": "接一个素材",
        "user_id": "u9", "conversation_id": "c9",
    })
    packed = json.loads(res[0][0].text if isinstance(res, tuple) else res[0].text)
    check(packed["node"] == "load_media" and packed["artifact_id"] == "artX",
          "wrapper 打包契约 {node, artifact_id, output}")
    check(packed["output"]["clips"][0]["width"] == 320,
          "真实 probe 元数据进入 clips（320x240）")
    check(packed["output"]["clips"][0]["material_id"] == mid,
          "clips 回带 material_id（渲染链路只认 id）")

    # 越权：他人 material_id 混在入参里也只会被跳过并回报
    other = tmp / "other.mp4"
    mediaops.ffmpeg("-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=25:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-an", str(other))
    mid_foreign = new_id("mat", 6)
    key_f = f"users/u8/convs/c8/{mid_foreign}.mp4"
    blob_f = other.read_bytes()

    async def chunks_f():
        yield blob_f

    info_f = await storage.objects.put(key_f, chunks_f(), content_type="video/mp4")
    await storage.users.provision("u8")
    await storage.conversations.ensure("u8", "c8")
    await storage.materials.register("u8", "c8", key_f, "other.mp4", "video",
                                     bytes_=info_f.bytes, sha256=info_f.sha256,
                                     mime="video/mp4", origin="upload",
                                     material_id=mid_foreign)
    res_mix = await srv.mcp.call_tool("load_media", {
        "material_ids": [mid, mid_foreign], "session_id": "u9:c9",
        "artifact_id": "artMix", "user_id": "u9", "conversation_id": "c9"})
    mix = json.loads(res_mix[0][0].text if isinstance(res_mix, tuple) else res_mix[0].text)
    check(mix["output"]["skipped_unauthorized"] == [mid_foreign]
          and {m["material_id"] for m in mix["output"]["media"]} == {mid},
          "他人 material_id 经 MCP 层传入也取不到（只记 skipped_unauthorized）")

    try:
        res_denied = await srv.mcp.call_tool(
            "load_media", {"material_ids": [mid_foreign], "session_id": "u9:c9",
                           "artifact_id": "artNone", "user_id": "u9",
                           "conversation_id": "c9"})
        denied_txt = (res_denied[0][0].text if isinstance(res_denied, tuple)
                      else res_denied[0].text)
    except Exception as e:             # FastMCP 把节点异常包成 ToolError 外抛
        denied_txt = str(e)
    check("越权" in denied_txt or "没有任何可读出" in denied_txt,
          "全部 material_id 被拒时 load_media 直接报错，不静默出空片")

    art_row = await storage.db.get_by_pk("artifacts", {"session_id": "u9:c9",
                                                       "node": "load_media",
                                                       "artifact_id": "artX"})
    check(art_row is not None and art_row["payload"]["media"],
          "响应拦截器把产物写进 artifacts 表（session/node/artifact 三元主键）")

    # 未传 session_id：作用域由注入身份推出，且不随模型逐轮改写的 user_request 漂移
    res_scope = await srv.mcp.call_tool("load_media", {
        "material_ids": [mid], "user_request": "第一句话这么说",
        "user_id": "u9", "conversation_id": "c9"})
    txt_scope = res_scope[0][0].text if isinstance(res_scope, tuple) else res_scope[0].text
    check(json.loads(txt_scope)["node"] == "load_media",
          f"匿名 session_id 时按身份建作用域（{json.loads(txt_scope)['artifact_id']!r}）")
    res_next = await srv.mcp.call_tool("split_shots", {
        "user_request": "第二句话换了个说法", "user_id": "u9", "conversation_id": "c9"})
    txt_next = res_next[0][0].text if isinstance(res_next, tuple) else res_next[0].text
    nxt = json.loads(txt_next)
    check(nxt["output"]["shot_count"] >= 1,
          "换 user_request 不换作用域：split_shots 仍从同一作用域读回 load_media，"
          "不再补齐重跑根节点而报缺 material_ids")
    scoped = await storage.db.select("artifacts", where={"session_id": "u:u9:c:c9"})
    check(sorted({r["node"] for r in scoped}) == ["load_media", "split_shots"]
          and {r["artifact_id"] for r in scoped} == {"_default"},
          f"两个节点产物同落 u:u9:c:c9 作用域：{sorted(scoped and {r['node'] for r in scoped})}")
    res2 = await srv.mcp.call_tool("read_node_history", {"key": "load_media",
                                                         "session_id": "u9:c9",
                                                         "artifact_id": "artX"})
    txt = res2[0][0].text if isinstance(res2, tuple) else res2[0].text
    got = json.loads(txt)
    check(got["node"] == "load_media" and got["payload"]["media"][0]["fps"] > 0,
          "跨调用从 artifacts 表读回上游产物（会话隔离生效）")

    res3 = await srv.mcp.call_tool("read_node_history", {"key": "asr",
                                                         "session_id": "ghost:c", "artifact_id": ""})
    txt3 = res3[0][0].text if isinstance(res3, tuple) else res3[0].text
    check("error" in json.loads(txt3), "陌生会话读不到他人产物（隔离负例）")

    check(SESSION_HEADER == "X-Storyline-Session-Id", "文档模板规定的会话头名")

    # ---------- MediaCardHook：render_video 工具结果 → type=media 回投（前端播放卡片） ----------
    from agent_framework.hooks import AgentHookContext, MediaCardHook
    from agent_framework.messages import tool_result
    from agent_framework.session import Session

    class CapturingMQ:
        def __init__(self):
            self.frames = []

        async def publish(self, topic, key, payload):
            self.frames.append((topic, key, payload))

    mq = CapturingMQ()
    hook = MediaCardHook(mq)
    ctx = AgentHookContext(session=Session("media_u", "media_c"))
    ctx.extras["run_id"] = "r-media"
    PRESIGNED = ("http://127.0.0.1:9000/creation/renders/x/a1.mp4"
                 "?X-Amz-Credential=sk&X-Amz-Signature=deadbeef")
    ctx.messages = [
        tool_result("t1", "render_video", json.dumps({
            "node": "render_video", "artifact_id": "a1",
            "output": {"video": "renders/x/a1.mp4",
                       "media_url": PRESIGNED,
                       "title": "城市夜景", "duration": 12.3},
        }, ensure_ascii=False)),
        tool_result("t2", "load_media", json.dumps({"node": "load_media", "output": {}})),
        tool_result("t3", "render_video", json.dumps({
            "node": "render_video", "artifact_id": "a2",
            "output": {"video": "C:/.storyline/out/x/a2.mp4",
                       "media_url": "C:/.storyline/out/x/a2.mp4"},
        }, ensure_ascii=False)),
    ]
    await hook.after_execute_tools(ctx)
    await hook.after_execute_tools(ctx)  # 去重：同一 media_url 只推一次
    media_frames = [f for f in mq.frames if f[2]["type"] == "media"]
    check(len(media_frames) == 1 and media_frames[0][2]["media_url"] == PRESIGNED
          and media_frames[0][2]["session_id"] == "media_u:media_c",
          "MediaCardHook 捕获 presigned media_url 并按 session 回投 type=media（且去重）")
    check(all("a2.mp4" not in json.dumps(f[2], ensure_ascii=False) for f in mq.frames),
          "裸本地路径（无 scheme）不再被当成可回放媒体")

    # ---------- 渲染改成「提交 + 轮询」：一次调用不再等一部片子 ----------
    from storyline_server.render_jobs import RenderDispatcher, tool_view

    rv_props = by_name["render_video"].inputSchema["properties"]
    check("render_video" in by_name and "render_status" in by_name,
          "服务端 render_status 工具在册（模型收到「去轮询」的指示时得有工具可轮）")
    check("wait_sec" in rv_props, "render_video 声明 wait_sec（可要求内联等待，服务端另有上限）")
    check("render_status" in by_name["render_video"].description,
          "render_video 的描述把「没跑完就轮询」写进契约，而不是只改行为")

    rsess, rt = "u:u9:c:poll", "artPoll"
    rkey = "renders/u_u9_c_poll/artPoll.mp4"

    async def one_chunk():
        yield b"fake rendered bytes"

    await storage.objects.put(rkey, one_chunk(), content_type="video/mp4")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_render():
        started.set()
        await release.wait()
        await storage.render_jobs.succeed(
            rsess, rt, rkey, 7.5,
            result={"video": rkey, "duration": 7.5,
                    "width": 320, "height": 240, "title": "轮询验证"})
        return {"node": "render_video"}

    disp = RenderDispatcher(storage, poll_interval_sec=0.01)
    snap = await disp.submit(rsess, rt, slow_render)
    check(snap["status"] == "queued" and snap["percent"] == 0,
          "提交即刻回 queued 视图（调用不再被渲染时长绑住）")
    await asyncio.wait_for(started.wait(), 2)
    await disp.submit(rsess, rt, slow_render)
    check(disp.inflight == 1, "同一产物重复提交不重开渲染，也不把在跑的那次复位")
    mid = await disp.wait(rsess, rt, 0.05)
    check(mid["status"] == "queued", "内联等待到点就回进度视图，不会无限等")
    release.set()
    done = await disp.wait(rsess, rt, 2.0)
    check(done["status"] == "done" and done["duration"] == 7.5
          and "://" in (done.get("media_url") or ""),
          "跑完后轮询拿到成片与现签直链（终态产物从 render_jobs.result 重建）")
    view = tool_view(done)
    check(view["node"] == "render_video" and view["artifact_id"] == rt
          and view["output"]["title"] == "轮询验证" and "hint" not in view,
          "终态视图沿用 wrapper 打包契约 {node, artifact_id, output}（Hook 认这个形状）")
    live = json.loads(_tool_text(
        await srv.mcp.call_tool("render_status", {"session_id": rsess,
                                                  "artifact_id": rt})))
    check(live["render"]["status"] == "done"
          and live["output"]["media_url"] == done["media_url"],
          "render_status 不认「谁提交的」：另一份 dispatcher 跑完的渲染照样查得到")
    ghost = json.loads(_tool_text(
        await srv.mcp.call_tool("render_status", {"session_id": "ghost:nope",
                                                  "artifact_id": "no"})))
    check(ghost["render"]["status"] == "none" and ghost["output"] is None,
          "没提交过的作用域回 status=none（区别于 queued），并指回 render_video")

    async def boom():
        raise RuntimeError("渲染进程炸了")

    await disp.submit("u:u9:c:crash", "artCrash", boom)
    crashed = await disp.wait("u:u9:c:crash", "artCrash", 1.0)
    check(crashed["status"] == "failed" and "炸了" in (crashed.get("error") or ""),
          "后台任务抛错时补写 failed：轮询端不会停在谎报的 queued/running")

    class _StubNode:
        name = "render_video"

    hold = asyncio.Event()

    async def slow_invoke(name, state, **args):
        await hold.wait()
        return {"node": name}

    srv.interceptor.invoke = slow_invoke
    srv.settings.caps.render_wait_max_sec = 0.05
    cap_state = await srv._make_state("u:u9:c:cap", "artCap")
    t0 = time.perf_counter()
    capped = json.loads(await srv._invoke_long(_StubNode(), cap_state, {}, 9999))
    cost = time.perf_counter() - t0
    hold.set()
    await srv.renders.cancel_all()
    check(cost < 2.0,
          f"wait_sec 被 render_wait_max_sec 夹住（要了 9999s，实际 {cost:.2f}s 就回句柄）")
    check(capped["render"]["status"] in ("queued", "running")
          and "render_status" in capped.get("hint", ""),
          f"未跑完时回任务句柄 + 指向 render_status 的下一步：{capped.get('hint', '')[:40]}")
    await disp.cancel_all()

    # ---------- 启停挂在进程上，不是挂在会话上 ----------
    # mcp SDK 只用 FastMCP(lifespan=...) 喂 low-level Server，而后者由 streamable HTTP 的
    # session manager 每个客户端会话跑一遍。旧写法因此有两个真机后果：主服务重启关掉旧会话时
    # finally 会 cancel_all() 杀掉正在跑的渲染、关掉连接池；而启动对账要等第一个客户端连上
    # 才发生——进程已经带着坏存储开始监听端口了。
    starts, shutdowns = [], []
    real_start = srv.storage.start
    real_shutdown = srv._shutdown

    async def spy_start():
        starts.append(1)
        return await real_start()

    async def spy_shutdown():
        shutdowns.append(1)
        return await real_shutdown()

    srv.storage.start = spy_start
    srv._shutdown = spy_shutdown

    stuck = asyncio.Event()

    async def never_ends():
        await stuck.wait()
        return {"node": "render_video"}

    await srv.renders.submit("u:u9:c:live", "artLive", never_ends)
    await asyncio.sleep(0)
    check(srv.renders.inflight == 1, "先放一个不会结束的渲染（下面要证明它不会被会话拆除杀掉）")

    async with srv._lifespan(None):
        pass                     # 客户端连上又断开
    async with srv._lifespan(None):
        pass                     # 重连：不该重做对账，也不该收进程级的活儿
    check(len(starts) == 1, f"会话反复建/拆只跑一次启动对账（实际 {len(starts)} 次）")
    check(srv._booted and srv.renders._watchdog is not None,
          "会话退出没有停掉看门狗、也没有复位启动闩")
    check(srv.renders.inflight == 1 and not stuck.is_set(),
          "客户端断开不取消在跑的渲染（旧写法在这里 cancel_all + storage.close）")

    srv._mount_process_lifespan()
    factory = srv.mcp.streamable_http_app
    srv._mount_process_lifespan()
    check(srv.mcp.streamable_http_app is factory
          and getattr(factory, "_storyline_mounted", False),
          "重复挂载不会把 lifespan 套成两层")

    app = srv.mcp.streamable_http_app()
    async with app.router.lifespan_context(app):
        check(len(starts) == 1 and shutdowns == [],
              "app 级启动即校验存储层：一个客户端都没连上也已经对账完")
        check(srv.renders.inflight == 1, "进程级启动不重开对账，也不误杀在跑的渲染")
    check(shutdowns == [1] and srv.renders.inflight == 0,
          "只有 app 退出（进程收尾）才停看门狗、取消后台渲染、关存储")

    print()
    print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
