"""前端自配置密钥的离线专测：PG app_secrets 存取、运行期热读优先级、请求期换 key、
/settings 三端点、连通性自检的口径与「明文不出门」这条硬约束。

运行：  python tests/test_model_settings.py

离线纪律：storage 用 memory:// 替身（同一份 repo 代码），LLM client 注入假对象，
自检的带图请求把 `_post_chat` 换成假网关——全程不起真连接、不碰真密钥。
真机那一遍在 .smoke/settings_key_smoke.py。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

import agent_framework.model_probe as MP
import agent_framework.secrets as SEC
from agent_framework import secrets
from agent_framework.agent import Agent, AgentConfig
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.identity import use_identity
from agent_framework.llm import ScriptedLLM
from agent_framework.llm_openai import OpenAICompatClient, get_default_llm
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry
from storyline_server.providers import ProviderError, build_providers
from storyline_server.settings import Capabilities

ENV = "OPENAI_API_KEY"
PG_KEY = "sk-from-app-secrets-PG0001"
ENV_KEY = "sk-from-environment-ENV0002"
FALLBACK_KEY = "sk-from-fallback-slot-F0003"
OTHER_KEY = "sk-another-user-key-OTHER04"

_checks = 0
_fails = 0


def clear_env_keys() -> dict[str, str | None]:
    """把环境变量层的**所有**名字摘干净，返回原值供还原。

    这一层现在认三个名字（OPENAI_API_KEY → DEEPSEEK_API_KEY → SILICONFLOW_API_KEY）。
    凡是判「环境变量没配时落到哪一层」的用例都必须一起 pop 掉——只 pop 首选那把，
    机器上残留的别名会让判据看环境脸色（改之前不存在这个问题，因为只认一个名字）。
    """
    return {n: os.environ.pop(n, None) for n in SEC.ENV_KEY_NAMES}


def restore_env_keys(saved: dict[str, str | None]) -> None:
    for name, value in saved.items():
        os.environ.pop(name, None)
        if value is not None:
            os.environ[name] = value


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class CountingStorage:
    """包一层真 memory storage，只数 app_secrets 被读了几次（验 TTL 命中）。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.reads = 0
        self.secrets = CountingSecrets(inner.secrets, self)
        self.backend = inner.backend

    def reset(self) -> None:
        self.reads = 0


class CountingSecrets:
    def __init__(self, repo, holder) -> None:
        self.repo = repo
        self.holder = holder

    async def get(self, user_id, key_name):
        self.holder.reads += 1
        return await self.repo.get(user_id, key_name)

    async def row(self, user_id, key_name):
        return await self.repo.row(user_id, key_name)

    async def put(self, user_id, key_name, value):
        await self.repo.put(user_id, key_name, value)

    async def drop(self, user_id, key_name):
        return await self.repo.drop(user_id, key_name)


def fresh_storage():
    st = build_storage("memory", cache_root=Path(tempfile.mkdtemp()) / "cache",
                       workspace_root=Path(tempfile.mkdtemp()) / "ws")
    run(st.start())
    return st


# ---------------------------------------------------------------------------
# 1. 表与仓储
# ---------------------------------------------------------------------------

def case_repo_roundtrip() -> None:
    st = fresh_storage()
    run(st.users.provision("u-a"))
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
    check(run(st.secrets.get("u-a", SEC.API_KEY_NAME)) == PG_KEY, "put 之后 get 拿回原值")
    row = run(st.secrets.row("u-a", SEC.API_KEY_NAME))
    check(isinstance(row.get("updated_at"), datetime), "row 带 updated_at（前端显示上次修改）")
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, OTHER_KEY))   # 同一行覆盖，不多插
    check(run(st.secrets.get("u-a", SEC.API_KEY_NAME)) == OTHER_KEY, "再次保存是覆盖不是新增")
    check(run(st.secrets.drop("u-a", SEC.API_KEY_NAME)) == 1, "drop 删掉这一行")
    check(run(st.secrets.get("u-a", SEC.API_KEY_NAME)) == "", "删干净之后读为空串")
    check(run(st.secrets.get("nobody", SEC.API_KEY_NAME)) == "", "没配过的身份读到空串而不是报错")


def case_mask_shape() -> None:
    m = SEC.mask("sk-abcdefghij1234")
    check(m.startswith("sk-") and m.endswith("1234"), "掩码留前缀与末 4 位")
    check("abcdefghij" not in m, "掩码不含中间正文（抄不回可用值）")
    check(SEC.mask("") == "" and SEC.mask("short") == "*****", "空串与短值都不漏内容")


# ---------------------------------------------------------------------------
# 2. 运行期解析优先级与热读
# ---------------------------------------------------------------------------

def case_resolve_priority() -> None:
    saved = clear_env_keys()
    st = CountingStorage(fresh_storage())
    run(st.inner.users.provision("u-a"))
    SEC.bind_storage(st)
    try:
        check(run(SEC.resolve_api_key("u-a", fallback=FALLBACK_KEY))
              == (FALLBACK_KEY, SEC.SOURCE_FALLBACK), "库里还没有时落到回落位")
        run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
        SEC.invalidate()
        key, src = run(SEC.resolve_api_key("u-a", fallback=FALLBACK_KEY))
        check(key == PG_KEY and src == SEC.SOURCE_PG, "库里有值时前端配置优先于回落位")
        os.environ[ENV] = ENV_KEY
        key, src = run(SEC.resolve_api_key("u-a", fallback=FALLBACK_KEY))
        check(key == PG_KEY and src == SEC.SOURCE_PG, "前端配置也压过环境变量")
        key, src = run(SEC.resolve_api_key("u-none", fallback=FALLBACK_KEY))
        check(key == ENV_KEY and src == SEC.SOURCE_ENV, "没配过的身份退回环境变量")
        os.environ.pop(ENV, None)
        key, src = run(SEC.resolve_api_key("u-none", fallback=FALLBACK_KEY))
        check(key == FALLBACK_KEY and src == SEC.SOURCE_FALLBACK, "环境变量也没了才用回落位")
        key, src = run(SEC.resolve_api_key("u-none"))
        check(key == "" and src == SEC.SOURCE_NONE, "三层都空时明确报「未配置」而不是硬凑")
    finally:
        restore_env_keys(saved)
        SEC.unbind_storage()


def case_env_alias_names() -> None:
    """环境变量层认三个名字（审计第 7 条）：原先 DEEPSEEK/SILICONFLOW 没人读，
    用户在 .env 里填了、静默无效，还以为「填了就生效」。

    钉四件事：① 顺序即优先级；② 来源名报出**实际命中的那一个**（诊断据此说得清读到了哪把）；
    ③ 空白串不算填了；④ 客户端构造期与请求期走同一套判据（不能一处认三把、另一处只认一把）。
    """
    aliases = ("DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY")
    ALIAS_DS = "sk-from-deepseek-alias-D0005"
    ALIAS_SF = "sk-from-siliconflow-alias-S0006"
    saved = clear_env_keys()
    st = CountingStorage(fresh_storage())
    SEC.bind_storage(st)
    try:
        check(SEC.ENV_KEY_NAMES[0] == ENV and SEC.ENV_KEY_NAMES[1:] == aliases,
              f"环境变量层的名字与顺序：{SEC.ENV_KEY_NAMES}")
        os.environ[aliases[0]] = ALIAS_DS
        key, src = run(SEC.resolve_api_key("u-none"))
        check(key == ALIAS_DS and src == f"环境变量 {aliases[0]}",
              "只有 DEEPSEEK_API_KEY 时它生效（改之前这一把压根没人读）")
        os.environ[ENV] = ENV_KEY
        key, src = run(SEC.resolve_api_key("u-none"))
        check(key == ENV_KEY and src == SEC.SOURCE_ENV,
              "OPENAI_API_KEY 有值时压过后两把（首选地位不变）")
        os.environ.pop(ENV, None)
        os.environ[ENV] = "   "
        key, src = run(SEC.resolve_api_key("u-none"))
        check(key == ALIAS_DS and src == f"环境变量 {aliases[0]}",
              "空白串不算填了，跳过它取下一把")
        os.environ.pop(ENV, None)
        os.environ.pop(aliases[0], None)
        os.environ[aliases[1]] = ALIAS_SF
        key, src = run(SEC.resolve_api_key("u-none"))
        check(key == ALIAS_SF and src == f"环境变量 {aliases[1]}",
              "第三把也只在前两把都空时才用")
        check(OpenAICompatClient(client=object()).api_key == ALIAS_SF,
              "构造期读同一套判据（不会一处认三把、另一处只认 OPENAI_API_KEY）")
        check(ALIAS_SF not in src and ALIAS_DS not in src,
              "来源名只带变量名，不带任何一把 key 的值")
        masked = SEC.mask(ALIAS_SF)
        check(masked.endswith(ALIAS_SF[-4:]) and "siliconflow" not in masked,
              "值要出门仍必须先过掩码（中间正文抄不回）")
    finally:
        restore_env_keys(saved)
        SEC.unbind_storage()


def case_hot_reload_and_ttl() -> None:
    """保存即生效：本进程靠 invalidate 立刻看到，另一进程靠 TTL 到期后重读。"""
    st = CountingStorage(fresh_storage())
    run(st.inner.users.provision("u-a"))
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
    SEC.bind_storage(st)
    try:
        st.reset()
        check(run(SEC.resolve_api_key("u-a"))[0] == PG_KEY, "第一次读打到库")
        check(st.reads == 1, f"确实查了一次库（reads={st.reads}）")
        run(st.secrets.put("u-a", SEC.API_KEY_NAME, OTHER_KEY))
        check(run(SEC.resolve_api_key("u-a"))[0] == PG_KEY, "缓存未过期时读到旧值（TTL 语义）")
        check(st.reads == 1, "TTL 内不再打库")
        SEC.invalidate("u-a")
        check(run(SEC.resolve_api_key("u-a"))[0] == OTHER_KEY, "invalidate 之后立刻看到新值")
        check(st.reads == 2, "作废后重新回源一次")
        with use_identity("u-a", "c-1"):
            check(run(SEC.resolve_api_key())[0] == OTHER_KEY,
                  "不传 user_id 时按当前执行身份解析（Agent 运行内生效）")
        with use_identity("u-b", "c-1"):
            check(run(SEC.resolve_api_key())[0] == "", "别人的身份拿不到 u-a 的密钥")
    finally:
        SEC.unbind_storage()


def case_start_binds_storage() -> None:
    """Storage.start() 里那一行绑定是两边热读的唯一入口——没它整条链就断。"""
    SEC.unbind_storage()
    st = fresh_storage()
    check(secrets.bound_storage() is st, "storage.start() 自动把自己绑进 secrets")
    SEC.unbind_storage()


# ---------------------------------------------------------------------------
# 3. 请求期换 key（主 LLM 通道）
# ---------------------------------------------------------------------------

class Completions:
    def __init__(self, sink) -> None:
        self.sink = sink

    async def create(self, **kw) -> dict:
        self.sink.append(("create", kw))
        return {"choices": [{"message": {"content": "好的"}}]}


class Chat:
    """真实 SDK 的嵌套是 client.chat.completions.create，假对象少一层就会假绿。"""

    def __init__(self, sink) -> None:
        self.completions = Completions(sink)


class FakeClient:
    """假 AsyncOpenAI：with_options 产出带新 key 的副本，create 记录调用。"""

    def __init__(self, api_key: str, sink: list) -> None:
        self.api_key = api_key
        self.sink = sink

    def with_options(self, *, api_key: str) -> "FakeClient":
        self.sink.append(("with_options", api_key))
        return FakeClient(api_key, self.sink)

    @property
    def chat(self):
        return Chat(self.sink)


class Resp:
    content = "好的"
    tool_calls = []


def case_llm_uses_pg_key() -> None:
    st = fresh_storage()
    run(st.users.provision("u-a"))
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
    SEC.bind_storage(st)
    sink: list = []
    saved_parse = OpenAICompatClient.__dict__["_parse"]
    saved_env: dict[str, str | None] = {}
    OpenAICompatClient._parse = staticmethod(lambda _r: Resp())
    try:
        c = OpenAICompatClient(api_key=FALLBACK_KEY, client=FakeClient(FALLBACK_KEY, sink))
        check(c._injected is True, "注入的假客户端被标记：离线测试不掺密钥逻辑")
        c._injected = False                     # 下面验的就是真链路的密钥决策
        with use_identity("u-a", "c-1"):
            used = run(c._client_for_request())
        check(used.api_key == PG_KEY, "有前端配置时请求期换成 PG 里的 key")
        with use_identity("u-none", "c-1"):
            used = run(c._client_for_request())
        check(used.api_key == FALLBACK_KEY, "没配置时原样复用实例 key，不多做一次 with_options")
        saved_env = clear_env_keys()
        bare = OpenAICompatClient(api_key="", client=FakeClient("", sink))
        bare.api_key = ""                    # 构造期会被回落位补齐；这里模拟回落位本身为空
        bare._injected = False
        try:
            run(bare._client_for_request())
            check(False, "三层都没有时应报错而不是拿空 key 出网")
        except RuntimeError as e:
            check("设置" in str(e) and PG_KEY not in str(e),
                  "报错指向页面「设置」且不带任何密钥值")
        run(c.complete(messages=[{"role": "user", "content": "hi"}], tools=None))
        check(any(k == "create" for k, _ in sink), "complete() 走的是解析后的 client")
    finally:
        OpenAICompatClient._parse = saved_parse
        restore_env_keys(saved_env)
        SEC.unbind_storage()


def case_build_without_key() -> None:
    """构造期不再拦空 key：用户可能就是先在页面上配好才启动的。"""
    saved = clear_env_keys()
    try:
        c = OpenAICompatClient(api_key=FALLBACK_KEY, client=object())
        c.api_key = ""                      # 模拟三层都没有的启动时刻
        built = c._build_real_client()
        check(getattr(built, "api_key", None) == "pending-frontend-settings",
              "空 key 也能构造出真 client（占位串，绝不出网）")
    finally:
        restore_env_keys(saved)


# ---------------------------------------------------------------------------
# 4. /settings 三端点
# ---------------------------------------------------------------------------

def make_app(storage):
    agent = Agent(
        llm=ScriptedLLM(steps=[("answer", "ok")]),
        registry=ToolRegistry(),
        session_manager=SessionManager(storage),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=1),
        storage=storage,
    )
    return create_app(agent, InMemoryMessageQueue(), storage=storage), agent


def register(client: TestClient) -> tuple[str, dict[str, str]]:
    j = client.post("/register", json={}).json()
    return j["user_id"], {"Authorization": f"Bearer {j['token']}"}


def case_settings_endpoints() -> None:
    saved = clear_env_keys()
    st = fresh_storage()
    SEC.bind_storage(st)      # 生产里这行由 Storage.start() 做；TestClient 不跑 run_server 的装配
    app, agent = make_app(st)
    try:
        with TestClient(app) as client:
            uid, hdr = register(client)
            other, hdr_other = register(client)

            check(client.get("/settings").status_code == 401,
                  "不带凭证读不到设置（与其余端点同一套鉴权）")
            view = client.get("/settings", headers=hdr).json()
            check(view["api_key"]["configured"] is False, "新身份初始为未配置")
            check(view["thinking"] is False, "面板里的思考模式默认显示为关")
            agent.runner.llm.thinking = True
            check(client.get("/settings", headers=hdr).json()["thinking"] is True,
                  "读的是注入的那个 client（顺着 runner 找），不是写死的默认值")
            agent.runner.llm.thinking = False

            check(client.post("/settings/api-key", headers=hdr,
                              json={"api_key": "sk with space"}).status_code == 400,
                  "含空格/换行的值被挡在写库之前")
            check(client.post("/settings/api-key", headers=hdr,
                              json={"api_key": "x" * 400}).status_code == 400,
                  "过长的粘贴被拒（多半粘错了东西）")

            r = client.post("/settings/api-key", headers=hdr, json={"api_key": "  " + PG_KEY + "  "})
            body = r.text
            check(r.status_code == 200 and r.json()["status"] == "saved", "保存成功并回状态")
            check(PG_KEY not in body, "响应正文里没有明文密钥")
            check(r.json()["api_key"]["masked"].endswith(PG_KEY[-4:]), "回显掩码末 4 位可辨认")
            check(r.json()["api_key"]["source"] == SEC.SOURCE_PG, "生效来源立刻变成前端配置")
            check(run(st.secrets.get(uid, SEC.API_KEY_NAME)) == PG_KEY, "去空白后的值落进 app_secrets")

            check(client.get("/settings", headers=hdr_other).json()["api_key"]["configured"] is False,
                  "另一个身份看不到也用不上这条密钥")

            os.environ[ENV] = ENV_KEY
            cleared = client.post("/settings/api-key", headers=hdr, json={"api_key": ""})
            check(cleared.json()["status"] == "cleared", "传空串即清除")
            check(run(st.secrets.get(uid, SEC.API_KEY_NAME)) == "", "清除后库里没有残留")
            check(cleared.json()["api_key"]["source"] == SEC.SOURCE_ENV,
                  "清除后回落环境变量（不是回落成无）")
    finally:
        restore_env_keys(saved)
        SEC.unbind_storage()


# ---------------------------------------------------------------------------
# 5. 连通性自检
# ---------------------------------------------------------------------------

class ProbeLLM:
    model = "deepseek-flash"
    base_url = "https://api.deepseek.com"
    api_key = FALLBACK_KEY

    def __init__(self) -> None:
        self.seen = []

    async def complete(self, messages, tools=None):
        self.seen.append(messages)
        return Resp()


def case_self_check_green() -> None:
    st = fresh_storage()
    run(st.users.provision("u-a"))
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
    SEC.bind_storage(st)
    saved_post, saved_timeout = MP._post_chat, MP.PROBE_TIMEOUT_SEC
    calls = []

    def fake_post(url, key, payload):
        # /chat/completions 是真实现里在 _post_chat 内拼的，替身照拼一遍，
        # 否则断言会退化成「base_url 等于自己」。
        calls.append((url.rstrip("/") + "/chat/completions", key, payload))
        return True, "一张带渐变的测试图", 42

    MP._post_chat = fake_post
    try:
        llm = ProbeLLM()
        out = run(MP.self_check(st, "u-a", llm))
        check(out["ok"] is True, "两路都过 → ok")
        check(out["source"] == SEC.SOURCE_PG and out["masked"].endswith(PG_KEY[-4:]),
              "自检说明白用的是哪一层来源、只给掩码")
        check(out["text"]["ok"] and out["vision"]["ok"], "文本与视觉分别给结论")
        check(out["text"]["ms"] >= 0 and out["vision"]["ms"] == 42, "两路各自报耗时")
        url, key, payload = calls[0]
        check(key == PG_KEY and url == "https://api.deepseek.com/chat/completions",
              "带图请求打在运行时解析出的 key 与主通道地址上")
        content = payload["messages"][0]["content"]
        check(content[0]["type"] == "text" and content[1]["image_url"]["url"]
              .startswith("data:image/jpeg;base64,"), "带图负载形态与剪辑链一致")
        check(payload.get("thinking") == {"type": "disabled"},
              "自检的带图请求与生产同样关掉思考")
        check(out.get("thinking") is False, "自检结果报出思考开关状态")
        check(PG_KEY not in json.dumps(out, ensure_ascii=False), "自检结果整体不含明文密钥")
    finally:
        MP._post_chat = saved_post
        MP.PROBE_TIMEOUT_SEC = saved_timeout
        SEC.unbind_storage()


def case_self_check_reports_breakage() -> None:
    """一路不通必须说人话：这正是过去「177 段全是占位」的入口。"""
    st = fresh_storage()
    run(st.users.provision("u-a"))
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
    SEC.bind_storage(st)
    saved = MP._post_chat
    MP._post_chat = lambda url, key, payload: (False, "HTTP 401 authentication_error", 7)
    try:
        out = run(MP.self_check(st, "u-a", ProbeLLM()))
        check(out["ok"] is False and out["vision"]["ok"] is False, "视觉不通 → 整体不通过")
        check(out["text"]["ok"] is True, "文本仍单独报可用（区分是模型不吃图还是 key 无效）")
        check("401" in out["vision"]["detail"], "把服务端的拒绝原因原样带回来")
    finally:
        MP._post_chat = saved

    SEC.unbind_storage()
    saved_env = clear_env_keys()
    try:
        out = run(MP.self_check(st, "u-a", type("NoKey", (), {"model": "m", "base_url": "b"})()))
        check(out["ok"] is False and "设置" in out["detail"], "没配 key 时直接教用户去「设置」填")
    finally:
        restore_env_keys(saved_env)


# ---------------------------------------------------------------------------
# 6. 剪辑侧（Storyline）走同一把 key
# ---------------------------------------------------------------------------

def case_storyline_resolves_pg_key() -> None:
    saved = clear_env_keys()
    # 生产代码不再硬编码回落 key：这里显式给回落位定值并重置单例，
    # 使 get_default_llm().api_key 与动态解析的 _fallback_key() 一致。
    import agent_framework.llm_openai as _L
    saved_key, saved_llm = _L.MY_API_KEY, _L._default_llm
    _L.MY_API_KEY, _L._default_llm = FALLBACK_KEY, None
    st = fresh_storage()
    run(st.users.provision("u-a"))
    run(st.secrets.put("u-a", SEC.API_KEY_NAME, PG_KEY))
    SEC.bind_storage(st)
    prov = build_providers(Capabilities())
    try:
        with use_identity("u-a", "c-1"):
            key, src = run(prov.resolve_key())
        check(key == PG_KEY and src == SEC.SOURCE_PG, "节点 await resolve_key 拿到前端配置")
        key, src = run(prov.resolve_key("u-none"))
        check(key == get_default_llm().api_key and src == SEC.SOURCE_FALLBACK,
              "该身份没配时落到主 LLM 的 key（一个 key 用到底）")
        try:
            prov.vision([], "描述", api_key="", key_source=src)
            check(False, "空 key 应抛 ProviderError 走降级")
        except ProviderError as e:
            check(PG_KEY not in str(e), "降级文案不带密钥值")
    finally:
        _L.MY_API_KEY, _L._default_llm = saved_key, saved_llm
        restore_env_keys(saved)
        SEC.unbind_storage()


def main() -> None:
    for fn in (case_repo_roundtrip, case_mask_shape, case_resolve_priority,
               case_env_alias_names,
               case_hot_reload_and_ttl, case_start_binds_storage, case_llm_uses_pg_key,
               case_build_without_key, case_settings_endpoints, case_self_check_green,
               case_self_check_reports_breakage, case_storyline_resolves_pg_key):
        print(f"\n[{fn.__name__}]")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - 用例崩了也算失败，不让后面的用例被跳过
            check(False, f"用例异常：{type(e).__name__}: {e}")
    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
