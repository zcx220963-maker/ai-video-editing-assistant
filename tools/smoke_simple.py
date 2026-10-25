"""端到端冒烟：/studio 能打开 -> 提交一句提示 -> 轮询到成片。

**在容器里跑**（要连 localhost:8000 与 PG）：

    docker cp tools/smoke_simple.py creation-app:/app/tools_smoke.py
    docker exec creation-app python /app/tools_smoke.py [user_id]

会花一次模型调用 + 一次渲染（约 40 秒、几分钱），所以不进自动测试套件。
边界（不许 import 分镜链 / 内核体积）由 `tests/test_simple_boundary.py` 守。
"""
import asyncio
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

BASE = "http://localhost:8000"
USER = sys.argv[1] if len(sys.argv) > 1 else "u-ebbfe7487e9a"


async def mint_token() -> str:
    """用服务端同一条路径签 token（Authenticator.issue_jwt），避免自己猜密钥。"""
    from agent_framework.auth import Authenticator
    from agent_framework.storage import build_storage
    st = build_storage("pg_minio")
    await st.start()
    try:
        row = await st.users.get(USER)
        print("用户存在:", bool(row), "| id:", (row or {}).get("id"))
        return await Authenticator(st).issue_jwt(USER)
    finally:
        await st.close()


def req(path, data=None, token="", timeout=30):
    url = BASE + path
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method="POST" if data is not None else "GET")
    if body:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


async def main() -> None:
    token = await mint_token()
    print("token:", token[:20], "...")

    st, html = req("/studio")
    print("GET /studio ->", st, "| 含标题:", "一句话出片" in html, "| 长度", len(html))

    st, raw = req("/simple/video", {
        "prompt": "一只鲸鱼缓缓穿过夜晚的城市，楼房的窗户亮着橙色的灯，鲸鱼身上有蓝色的发光条纹",
        "seconds": 6, "aspect": "portrait", "takes": 1}, token=token)
    print("POST /simple/video ->", st, raw[:120])
    job_id = json.loads(raw)["job_id"]

    t0 = time.time()
    last = ""
    while time.time() - t0 < 300:
        await asyncio.sleep(3)
        st, raw = req("/simple/job/" + job_id, token=token)
        j = json.loads(raw)
        line = f"  [{int(time.time()-t0):3d}s] {j['status']:9s} stage={j['stage']:8s} calls={j['calls']}"
        if line != last:
            print(line)
            last = line
        if j["status"] in ("done", "failed"):
            print("\n最终:", json.dumps({k: j[k] for k in
                  ("status", "stage", "error", "calls", "receipt", "ms")},
                  ensure_ascii=False)[:700])
            print("mp4:", (j.get("mp4") or "")[:130], "...")
            return
    print("超时未完成")


asyncio.run(main())
