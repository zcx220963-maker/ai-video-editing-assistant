# -*- coding: utf-8 -*-
"""两条编辑链的 HTTP 口：读表 / 取帧 / 提交局部改（前端「✂ 选区改」唯一走得通的路）。

为什么值得单独钉：这三对口是**前端直连**的，模型不参与，所以离线套件里那 90 个文件
一个都没碰到过它们——真机缺陷就是这么钻进来的：图形链换成代码链之后，`/motion/patch`
仍然去找注册表里已经不存在的 ``patch_motion_video``，每次提交都回 503「局部改工具不可用」；
同一批改动还把 ``/timeline/*`` 三口整个删掉了（读表与取帧本来是两条链共用一份实现），
前端在 hit_mode=track 时把 api 切成 "/timeline" 之后全是 404。两种都是「界面点了没反应、
后端一句错都不报给用户」的那一类。

这份用例钉的是**形状**而不是渲染：
① 三口在两处都在（路由集合里点名，删一条就红）；
② 提交时**点名的节点名跟着真实节点走**——桩子按真实节点类的 ``name`` 注册，
   路由里那个字符串一旦与真实节点脱钩，桩子就对不上，直接 503；
③ body 里的键原样透传到 args（含 texture/bgm_volume/bgm_duck 这些「一镜都不重烧」的档），
   版号一定换新（沿用 base 号等于覆盖旧版，退路就没了）；
④ 缺 base 号 400、节点没注册 503、提交侧的失败只有一档 500（注册表把节点异常统一包成
   ToolError，所以**没有**「提交前同步校验回 422」这一档，真校验在轮询端）；
⑤ 读表与取帧按调用者身份拼对象键：别的会话的产物在这里是 404，两族口拿到的是同一份字节。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework.agent import Agent, AgentConfig
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.identity import storyline_session_id
from agent_framework.llm import ScriptedLLM
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.orchestration import _safe
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import build_storage
from agent_framework.tool import Tool, ToolError, ToolRegistry
from storyline_server.nodes.core_nodes import PatchVideoNode

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class _StubPatch(Tool):
    """局部改节点的替身：把收到的 kwargs 原样记下来，回执形状照真节点（顶层 artifact_id）。

    名字**不写在这里**——由调用方从真实节点类取（见 ``_names``），这样路由里那个字符串
    与真实节点任何一边改名都会让这一对失配，接口用例立刻红。
    """

    def __init__(self, name: str, record: dict) -> None:
        self._name = name
        self.record = record

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return f"替身·{self._name}"

    @property
    def description(self) -> str:
        return "离线用例的局部改替身：只记账，不烧像素。"

    @property
    def parameters(self) -> dict:
        # 注册表对**未知键**是严格的（R5：模型自创键名会被拦下，表现成「工具没反应」）。
        # 所以替身必须像真节点一样把会收到的键全部声明出来——真节点那边由服务端
        # ``_signature_for`` 统一给每个节点补 user_id / conversation_id / artifact_id，
        # 下面这组就是它实际收到的形状。
        return {"type": "object", "properties": {
            "base_artifact_id": {"type": "string"},
            "user_id": {"type": "string"},
            "conversation_id": {"type": "string"},
            "artifact_id": {"type": "string"},
            "wait_sec": {"type": "number"},
            "edits": {"type": "array"},
            "shot_sets": {"type": "array"},
            "remove_shots": {"type": "array"},
            "reorder": {"type": "array"},
            "bgm": {"type": "string"},
            "bgm_volume": {"type": "number"},
            "bgm_duck": {"type": "boolean"},
            "texture": {"type": "string"},
            "film": {"type": "object"},
            "target_duration_sec": {"type": "number"},
        }, "required": ["base_artifact_id"]}

    async def execute(self, **kwargs) -> dict:
        self.record.clear()
        self.record.update(kwargs)
        return {"status": "queued", "artifact_id": kwargs["artifact_id"],
                "tool": self._name}


class _RaisePatch(_StubPatch):
    """节点抛 ValueError：注册表把它包成 ToolError，所以这一档最终透出成 500，不是 422。"""

    async def execute(self, **kwargs):
        raise ValueError("第 3 镜的代码没通过闸① ：import 了不存在的包")


class _FailPatch(_StubPatch):
    """节点自己回 ToolError（隔 MCP 时的失败形状）→ 500。"""

    async def execute(self, **kwargs):
        return ToolError(self._name, "Error: 这一版的命中表与分镜指纹不一致")


def _names() -> tuple[str, str]:
    """真实局部改节点名——单源，不在这儿重打一遍。

    2026-10-25：图形科普链的 `patch_motion_code` 节点随那条链退役了
    （`nodes/motion_code_nodes.py` 已删），但**前端那条 `/motion/patch` 路由还在**——
    它派发时按节点名找工具，所以这里仍要一个名字。用字面量而不是 import 已删模块：
    这两条用例测的是"路由能不能把参数派发到节点、错误怎么透出"，不是节点实现。
    """
    return "patch_motion_code", PatchVideoNode.name


def _build(storage, registry):
    app = create_app(
        Agent(llm=ScriptedLLM(steps=[("answer", "x")]), registry=registry,
            session_manager=SessionManager(storage),
            context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
            config=AgentConfig(max_iterations=2), storage=storage),
        InMemoryMessageQueue(), storage=storage)
    return app


def main() -> None:
    motion_name, track_name = _names()
    check(motion_name == "patch_motion_code" and track_name == "patch_video",
          f"真实节点名就是这两枚（图形链换过代码链后不能还叫旧名）："
          f"{motion_name} / {track_name}")

    # ---- ① 路由集合：三口在两处都在 ----
    with tempfile.TemporaryDirectory() as td:
        st = build_storage("memory", cache_root=Path(td) / "cache",
                           workspace_root=Path(td) / "ws")
        reg = ToolRegistry()
        seen_motion: dict = {}
        seen_track: dict = {}
        reg.register(_StubPatch(motion_name, seen_motion))
        reg.register(_StubPatch(track_name, seen_track))
        app = _build(st, reg)
        paths = {r.path for r in app.routes}
        for p in ("/motion/hitmap", "/motion/frame", "/motion/patch",
                  "/timeline/hitmap", "/timeline/frame", "/timeline/patch"):
            check(p in paths, f"路由表里有 {p}（删一口前端就整条 404）")

        with TestClient(app) as client:
            reg_info = client.post("/register", json={}).json()
            uid = reg_info["user_id"]
            hdr = {"Authorization": f"Bearer {reg_info['token']}"}
            conv = "c_patch"

            # 表与帧的字节：对象键按会话身份拼，两族口读到的是同一份
            sid = storyline_session_id(uid, conv)
            bundle = {"schema": "shot-hitmap/1", "hit_mode": "boxes",
                      "items": [{"pointer": "/shots/0/fields/radius/value",
                                 "shot": "s01"}]}
            png = b"\x89PNG\r\n\x1a\n" + b"offline-fixture" * 7

            async def _put():
                key_h = f"renders/{_safe(sid)}/{_safe('artA')}/hitmap.json"
                key_f = f"renders/{_safe(sid)}/{_safe('artA')}/frames/s01.png"
                for k, data, ct in ((key_h, json.dumps(bundle).encode(), "application/json"),
                                    (key_f, png, "image/png")):
                    async def chunks(payload=data):
                        yield payload
                    await st.objects.put(k, chunks(), content_type=ct)
            asyncio.run(_put())

            h_m = client.get("/motion/hitmap", headers=hdr,
                             params={"artifact_id": "artA", "conv_id": conv})
            h_t = client.get("/timeline/hitmap", headers=hdr,
                             params={"artifact_id": "artA", "conv_id": conv})
            check(h_m.status_code == 200 and h_t.status_code == 200,
                  f"两口都读到表（图形 {h_m.status_code} / 口播 {h_t.status_code}）")
            check(h_m.json() == h_t.json() == bundle,
                  "两族口拿到的是**同一份**表：读表实现只有一份")

            f_m = client.get("/motion/frame", headers=hdr,
                             params={"artifact_id": "artA", "shot": "s01", "conv_id": conv})
            f_t = client.get("/timeline/frame", headers=hdr,
                             params={"artifact_id": "artA", "shot": "s01", "conv_id": conv})
            check(f_m.status_code == 200 and f_m.content == png
                  and f_t.content == png,
                  f"两口拿到同一张代表帧字节（{f_m.status_code}/{f_t.status_code}）")

            miss = client.get("/timeline/hitmap", headers=hdr,
                              params={"artifact_id": "artA", "conv_id": "c_other"})
            check(miss.status_code == 404,
                  f"别的会话的表在这里是 404（按调用者身份拼键）：{miss.status_code}")
            noframe = client.get("/timeline/frame", headers=hdr,
                                 params={"artifact_id": "artA", "shot": "s99",
                                         "conv_id": conv})
            check(noframe.status_code == 404, "没留代表帧的那一段也是 404，不回空图")

            # ---- ②③ 提交：节点名跟着真实节点 + body 原样透传 ----
            body = {"base_artifact_id": "artA", "conv_id": conv, "wait_sec": 0,
                    "edits": [{"pointer": "/shots/0/fields/radius/value",
                               "value": 88, "shot": "s01"}],
                    "shot_sets": [{"shot": "s02", "set": {"text": "换一句"}}],
                    "remove_shots": ["s03"], "reorder": ["s01", "s02", "s04"],
                    "bgm": "mat-7c8ba3", "bgm_volume": 0.2, "bgm_duck": True,
                    "texture": "grain", "target_duration_sec": 42.0}
            r = client.post("/motion/patch", headers=hdr, json=body)
            check(r.status_code == 200, f"图形链提交 200（503 就是节点名对不上）："
                  f"{r.status_code} {r.json().get('detail') if r.status_code != 200 else ''}")
            got = seen_motion
            check(got.get("base_artifact_id") == "artA" and got.get("user_id") == uid,
                  "base 号与调用者身份都带上了")
            check(got.get("artifact_id") and got["artifact_id"] != "artA",
                  f"新版本号现拼（沿用 artA 等于覆盖旧版，退路没了）：{got.get('artifact_id')}")
            check(got.get("conversation_id") == conv and got.get("wait_sec") == 0.0,
                  "conv_id 与 wait_sec 走的是会话/内联等待那一栏")
            for k in ("edits", "shot_sets", "remove_shots", "reorder", "bgm",
                      "bgm_volume", "bgm_duck", "texture", "target_duration_sec"):
                check(k in got and got[k] == body[k], f"{k} 原样透传：{got.get(k)!r}")
            check(client.post("/motion/patch", headers=hdr,
                              json={"conv_id": conv}).status_code == 400,
                  "缺 base_artifact_id 直接 400")

            t_body = {"base_artifact_id": "artA", "conv_id": conv, "wait_sec": 0,
                      "edits": [{"pointer": "/segments/w2/text", "value": "改字不改时间",
                                 "segment_id": "w2"}],
                      "shot_sets": [{"shot": "s01", "set": {"text": "不该出现"}}]}
            rt = client.post("/timeline/patch", headers=hdr, json=t_body)
            check(rt.status_code == 200,
                  f"口播链提交 200：{rt.status_code} "
                  f"{rt.json().get('detail') if rt.status_code != 200 else ''}")
            check(seen_track.get("edits") == t_body["edits"],
                  "指针补丁（带 segment_id）转到了 patch_video")
            check("shot_sets" not in seen_track and "reorder" not in seen_track,
                  "这条链的合同不改总长也不换序：那些键即使前端误传也不进 args")

        # ---- ④ 503 / 422 / 500 三种失败形状 ----
        st2 = build_storage("memory", cache_root=Path(td) / "cache2",
                            workspace_root=Path(td) / "ws2")
        empty = ToolRegistry()          # 一个局部改节点都没注册：整条编辑口不可用
        with TestClient(_build(st2, empty)) as c2:
            info = c2.post("/register", json={}).json()
            h2 = {"Authorization": f"Bearer {info['token']}"}
            for path in ("/motion/patch", "/timeline/patch"):
                rr = c2.post(path, headers=h2,
                             json={"base_artifact_id": "artX", "edits": []})
                check(rr.status_code == 503,
                      f"{path}：注册表里没有那个节点就 503，不假装已提交（{rr.status_code}）")

        st3 = build_storage("memory", cache_root=Path(td) / "cache3",
                            workspace_root=Path(td) / "ws3")
        reg3 = ToolRegistry()
        reg3.register(_RaisePatch(motion_name, {}))
        reg3.register(_FailPatch(track_name, {}))
        with TestClient(_build(st3, reg3)) as c3:
            info = c3.post("/register", json={}).json()
            h3 = {"Authorization": f"Bearer {info['token']}"}
            bad = c3.post("/motion/patch", headers=h3,
                          json={"base_artifact_id": "artX", "edits": [{"pointer": "p",
                                                                      "value": 1}]})
            # 节点抛 ValueError，注册表把它包成 ToolError 回执——提交侧没有「422 = 你传的
            # 补丁不合规」这一档：真的合规校验随节点体进了后台，拒绝落在轮询端的 error 里。
            check(bad.status_code == 500 and "闸①" in bad.json().get("detail", ""),
                  f"节点判定的拒绝（ValueError→ToolError）透出成 500，正文不丢："
                  f"{bad.status_code} {bad.json().get('detail')}")
            toolerr = c3.post("/timeline/patch", headers=h3,
                              json={"base_artifact_id": "artX",
                                    "edits": [{"pointer": "p", "value": "v"}]})
            check(toolerr.status_code == 500 and "指纹" in str(toolerr.json()),
                  f"节点自己回 ToolError → 500（不退成 200 空回执）："
                  f"{toolerr.status_code}")

        asyncio.run(st.close())
        asyncio.run(st2.close())
        asyncio.run(st3.close())

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
