"""查服务端**真实注册**的工具（确认"写好了"和"装上了"是同一件事）。

    docker cp tools/check_tools.py creation-app:/app/tools_check_tools.py
    docker exec creation-app python /app/tools_check_tools.py short

为什么要单独一个脚本：`run_server.py` 少一行 `register_xxx_tools`，代码再对，
模型也看不见它——而这在启动日志里只表现为"工具总数少 4 个"，没人会去数。

`/tools` 要鉴权，所以这里用服务端**同一个** JWT 密钥签一个（`Authenticator.issue_jwt`
读的是同一份 `app_secrets` / `JWT_SECRET`），不需要密码。
"""
import asyncio
import json
import sys
import urllib.request

sys.path.insert(0, "/app")

BASE = "http://localhost:8000"
KEY = sys.argv[1] if len(sys.argv) > 1 else ""
USER = sys.argv[2] if len(sys.argv) > 2 else "u-ebbfe7487e9a"


async def main() -> int:
    from agent_framework.auth import Authenticator
    from agent_framework.storage import build_storage

    st = build_storage("pg_minio")
    await st.start()
    tok = await Authenticator(st).issue_jwt(USER)

    req = urllib.request.Request(f"{BASE}/tools",
                                 headers={"authorization": f"Bearer {tok}"})
    doc = json.loads(urllib.request.urlopen(req, timeout=15).read())

    # 响应有两种形状：`{"groups": {组名: [工具…]}}`（按来源分组，给人看的界面用）
    # 与扁平列表（早期版本）。两种都收，免得这个脚本自己成为下一个"看不出来"的东西。
    rows: list[dict] = []
    if isinstance(doc, dict) and isinstance(doc.get("groups"), dict):
        for group in doc["groups"].values():
            rows.extend(x for x in group if isinstance(x, dict))
    elif isinstance(doc, dict):
        rows = [x for x in (doc.get("tools") or []) if isinstance(x, dict)]
    else:
        rows = [x for x in doc if isinstance(x, dict)]

    names = sorted(str(r.get("name") or (r.get("function") or {}).get("name") or r)
                   for r in rows)
    print(f"共 {len(names)} 个工具")
    hit = [n for n in names if KEY in n] if KEY else names
    print("\n".join(hit) if hit else "（没有匹配）")
    return 0


raise SystemExit(asyncio.run(main()))
