# -*- coding: utf-8 -*-
"""双模型前端可配(主模型覆盖 + 判断模型三件套)的离线验证。

运行：  python tests/test_user_models.py   # 内存替身 + 假 SDK client,不联网

钉住六件事：
① resolve_model_base：无覆盖回落默认,有覆盖按用户生效;
② _resolve_request：覆盖后 with_options 换 base_url、complete 用的 model 是覆盖值;
③ 判断模型 _cfg_for：按用户三件套生效,未配置回落 env/None;
④ /settings/model 端点：保存 → GET /settings 反映 → 清除恢复;
⑤ /settings/judge 端点：成对校验(base/model 缺一 400)、api_key 掩码;
⑥ 主模型 complete 走覆盖后模型的回归(假 client 记录 create 的 model)。

注意：secrets 模块的存储句柄是**进程级绑定**(最后 start 者生效),所以本文件
全部用例共用同一个 storage 实例,避免跨 part 串台。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.auth import Authenticator
from agent_framework.identity import use_identity
from agent_framework.llm_openai import OpenAICompatClient
from agent_framework.secrets import (
    resolve_model_base, MAIN_MODEL_KEY, MAIN_BASE_KEY,
    JUDGE_BASE_KEY, JUDGE_MODEL_KEY, JUDGE_API_KEY_NAME,
)
from agent_framework.storage import build_storage

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class Recorder:
    """假 SDK:记录 with_options 与 create 的 model。"""

    def __init__(self, api_key="k0", base_url="http://b0"):
        self.api_key = api_key
        self.base_url = base_url
        self.with_options_calls: list = []
        self.created_models: list = []

    def with_options(self, **opts):
        self.with_options_calls.append(dict(opts))
        clone = Recorder(opts.get("api_key", self.api_key),
                         opts.get("base_url", self.base_url))
        clone.with_options_calls = self.with_options_calls
        clone.created_models = self.created_models
        return clone


def make_completions(recorder):
    class _Completions:
        async def create(_self, **kwargs):
            recorder.created_models.append(kwargs.get("model"))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="ok", tool_calls=None))], usage=None)
    return _Completions()


class RecordingClient(OpenAICompatClient):
    """真 client 骨架 + 假 SDK:验证覆盖值真的进了 with_options 与 create。"""

    with_options_calls: list = []
    created_models: list = []

    def _build_real_client(self):
        recorder = self

        class _Sdk:
            base_url = self.base_url

            def with_options(_self, **opts):
                recorder.with_options_calls.append(dict(opts))
                clone = _Sdk()
                clone.base_url = opts.get("base_url", _self.base_url)
                return clone

        class _Completions:
            async def create(_self, **kwargs):
                recorder.created_models.append(kwargs.get("model"))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="ok", tool_calls=None))], usage=None)

        outer_base = self.base_url

        class _Chat:
            def __init__(self):
                self.base_url = outer_base
                self.chat = _Completions()
                self.chat.completions = self.chat  # create() 直接挂在 chat 上

            def with_options(_self, **opts):
                recorder.with_options_calls.append(dict(opts))
                clone = _Chat()
                clone.base_url = opts.get("base_url", self.base_url)
                return clone

        return _Chat()


ST = None   # 全文件共享的 storage(secrets 句柄是进程级绑定)


async def setup_all() -> None:
    global ST, AUTH
    ST = build_storage("memory")
    await ST.start()                        # secrets 句柄在此绑定(进程级)
    AUTH = Authenticator(ST)
    await ST.users.provision("default")     # JWT 密钥落库的归属身份


async def part1_resolve_model_base() -> None:
    print("\n[1] resolve_model_base：无覆盖回落,有覆盖按用户")
    m, b = await resolve_model_base(fallback_model="deepseek-chat",
                                    fallback_base="http://default")
    check((m, b) == ("deepseek-chat", "http://default"), "无覆盖回落默认")

    await ST.secrets.put("u1", MAIN_MODEL_KEY, "glm-4.7")
    await ST.secrets.put("u1", MAIN_BASE_KEY, "http://glm")
    with use_identity("u1", "c1"):
        m, b = await resolve_model_base(fallback_model="deepseek-chat",
                                        fallback_base="http://default")
    check((m, b) == ("glm-4.7", "http://glm"), f"按用户覆盖生效：{(m, b)}")


async def part2_resolve_request() -> None:
    print("\n[2] _resolve_request：覆盖后换 base_url、model 是覆盖值")
    await ST.secrets.put("u2", MAIN_MODEL_KEY, "glm-4.7")
    await ST.secrets.put("u2", MAIN_BASE_KEY, "http://glm:9000")
    client = RecordingClient(model="m0", api_key="k0", base_url="http://b0")
    with use_identity("u2", "c2"):
        c2, model = await client._resolve_request()
    check(model == "glm-4.7", f"返回覆盖后的模型名:{model}")
    check(c2.base_url == "http://glm:9000", f"client 换了地址:{c2.base_url}")
    check(client.created_models == [], "resolve 本身不发请求")


async def part3_judge_cfg() -> None:
    print("\n[3] 判断模型 _cfg_for：按用户三件套,缺一回落")
    from agent_framework import judge as judge_mod
    with use_identity("u3", "c3"):
        check(await judge_mod._cfg_for("u3") is None, "未配置 → None(env 也没配)")
        await ST.secrets.put("u3", JUDGE_BASE_KEY, "http://j")
        await ST.secrets.put("u3", JUDGE_MODEL_KEY, "jev-1")
        await ST.secrets.put("u3", JUDGE_API_KEY_NAME, "jk")
        cfg = await judge_mod._cfg_for("u3")
        check(cfg == {"base": "http://j", "model": "jev-1", "key": "jk"},
              f"三件套齐 → 按用户配置:{cfg}")
        await ST.secrets.drop("u3", JUDGE_API_KEY_NAME)
        check(await judge_mod._cfg_for("u3") is None, "缺 key → 回落(None)")


async def part4_endpoints() -> None:
    print("\n[4] 端点:/settings/model 与 /settings/judge")
    from fastapi.testclient import TestClient
    from agent_framework.context import ContextBuilder
    from agent_framework.llm import ScriptedLLM as _S
    from agent_framework.agent import Agent
    from agent_framework.mq import InMemoryMessageQueue
    from agent_framework.server import create_app
    from agent_framework.session import SessionManager
    from agent_framework.tool import ToolRegistry

    agent = Agent(llm=_S(steps=[("answer", "好")]), registry=ToolRegistry(),
                  session_manager=SessionManager(ST),
                  context_builder=ContextBuilder("测试"), config=None, storage=ST)
    app = create_app(agent, InMemoryMessageQueue(), storage=ST)
    with TestClient(app) as c:
        reg = c.post("/auth/register",
                     json={"username": "配置员", "password": "pw123456"}).json()
        h = {"Authorization": "Bearer " + reg["token"]}

        r = c.post("/settings/model", json={"model": "glm-4.7", "base_url": "http://glm"},
                   headers=h)
        check(r.status_code == 200 and r.json()["main_override"]["model"] == "glm-4.7",
              "主模型覆盖保存")
        v = c.get("/settings", headers=h).json()
        check(v["main_override"] == {"model": "glm-4.7", "base_url": "http://glm"},
              "GET /settings 反映覆盖")

        r = c.post("/settings/judge", json={"base_url": "http://j"}, headers=h)
        check(r.status_code == 400, f"base/model 缺一 400({r.json().get('detail')})")
        r = c.post("/settings/judge", json={"base_url": "http://j", "model": "jev-1",
                                            "api_key": "jk"}, headers=h)
        check(r.status_code == 200 and r.json()["judge"]["configured"] is True,
              "判断模型三件套保存")
        v = c.get("/settings", headers=h).json()
        check(v["judge"]["configured"] is True and v["judge"]["masked"],
              f"judge 配置在 GET 可见({v['judge']['masked']})")
        r = c.post("/settings/judge", json={}, headers=h)
        check(r.status_code == 200, "清除判断模型")
        v = c.get("/settings", headers=h).json()
        check(v["judge"]["configured"] is False, "清除后未配置")
        c.post("/settings/model", json={}, headers=h)   # 清主模型覆盖
        v = c.get("/settings", headers=h).json()
        check(v["main_override"] == {"model": "", "base_url": ""}, "主模型覆盖可清除")


async def part6_complete_uses_override() -> None:
    print("\n[6] complete 走覆盖后模型的回归(假 client 记录 create 的 model)")
    await ST.secrets.put("u6", MAIN_MODEL_KEY, "glm-4.7")
    client = RecordingClient(model="m0", api_key="k0", base_url="http://b0")
    from agent_framework.identity import use_identity
    with use_identity("u6", "c6"):
        resp = await client.complete([{"role": "user", "content": "在吗"}])
    check(resp.content == "ok", "假 client 正常返回")
    check(RecordingClient.created_models[-1] == "glm-4.7",
          f"create 用的 model 是覆盖值:{RecordingClient.created_models[-1]}")


async def main() -> None:
    await setup_all()
    await part1_resolve_model_base()
    await part2_resolve_request()
    await part3_judge_cfg()
    await part4_endpoints()
    await part6_complete_uses_override()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
