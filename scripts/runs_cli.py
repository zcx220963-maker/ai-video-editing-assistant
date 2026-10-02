# -*- coding: utf-8 -*-
"""执行记录 / 时间旅行的命令行客户端：不开浏览器也能列链、选点、分叉、续跑、看到回投。

跑法（服务得先起着；token 只从环境或文件读，**绝不打印**）：
    set AGENT_TOKEN=…                      # Windows；或每次带 --token / --token-file
    python scripts/runs_cli.py runs --conversation c1
    python scripts/runs_cli.py history --run 9f3c1a2b4d5e
    python scripts/runs_cli.py fork --run 9f3c1a2b4d5e --at-seq 3 --message "BGM 换一首" --rerun-node select_BGM --wait
    python scripts/runs_cli.py resume --run 9f3c1a2b4d5e --at-seq 2 --wait
    python scripts/runs_cli.py chat --conversation c1 --message "一句话剪个海边日落" --wait --frames
    python scripts/runs_cli.py register --conversation c1      # 新身份：token 只写进 --token-file

为什么要有这个脚本：fork/resume 是**服务端 HTTP 契约**（`POST /runs/{id}/fork`），前端只是它
的一个消费者。没有独立客户端，这条契约就只能靠浏览器点或靠离线用例证明——两者都不算「从另一条
路也走得通」。这里刻意只用公开端点 + Bearer token，不 import 框架内部，跑的就是外部进程真正
会发出去的那份请求。

分叉点怎么选才有意义：`history` 的每个一致点带 `tools`（那一步刚落地的节点名），
`fork --before-node select_BGM` 直接把「该节点还没执行过」的那个 seq 算出来——服务端
`seq_before_tool` 本来就是这个口径，模型自己调 `rerun_from` 时也走它。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    # `raise SystemExit("…")` 走的是 stderr：只配 stdout 的话，报错那行在 GBK 控制台上是乱码。
    sys.stderr.reconfigure(encoding="utf-8")

import httpx

TERMINAL = ("completed", "failed", "superseded")


# ---------------------------------------------------------------- 凭证读取
# token 的三条来路按「越不方便落纸越好」排：环境变量 → 文件 → 命令行（最后一档只给一次性
# 调试用，注意它会出现在进程列表里）。文件既接受 `user_id<TAB>token`（run_migrate 的凭证
# 格式），也接受一行裸 token。任何一条路都不打印值，只在需要时报长度。

def read_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    env = os.getenv("AGENT_TOKEN", "").strip()
    if env:
        return env
    path = Path(args.token_file) if args.token_file else None
    if path is None:
        raise SystemExit("没有凭证：给 --token / --token-file，或设环境变量 AGENT_TOKEN")
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2 and (not args.user or parts[0] == args.user):
            return parts[-1].strip()
        if len(parts) == 1 and not args.user:
            return parts[0]
    raise SystemExit(f"{path} 里找不到{'用户 ' + args.user + ' 的' if args.user else '可用的'}凭证")


# ---------------------------------------------------------------- 输出

def emit(args: argparse.Namespace, obj: Any, human: str) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if args.json else human)


def fmt_run(row: dict[str, Any]) -> str:
    fork = ""
    if row.get("forked_from"):
        fork = f"  ← 分叉自 {row['forked_from']}@seq{row.get('forked_at_seq')}"
    return (f"{row['run_id']}  {row.get('status')}  iter={row.get('iteration')}"
            f"  会话={row.get('session_id')}{fork}\n    话：{str(row.get('message'))[:60]}")


def fmt_points(points: list[dict[str, Any]]) -> str:
    if not points:
        return "（这条 run 还没有一致点）"
    out = []
    for p in points:
        tools = "、".join(p.get("tools") or []) or "—"
        out.append(f"  seq={p['seq']:<3} iter={p.get('iteration'):<2} {p.get('kind'):<5} "
                   f"消息 {p.get('messages')} 条  落点节点：{tools}")
    return "\n".join(out)


def point_before_tool(points: list[dict[str, Any]], node: str) -> int:
    """分叉点 = 「该节点的结果第一次落下」那个一致点的前一格。

    与服务端 `Checkpoint.seq_before_tool` 同一条规则，不是「链上最后一个不含它的点」——
    后者会选到该节点**之后**的位置，那等于跳过它而不是重做它。
    """
    names = {t for p in points for t in (p.get("tools") or [])}
    if node not in names:
        raise SystemExit(f"这条链上没有「{node}」的执行记录；已有工具结果：{sorted(names) or '（空）'}")
    for p in points:
        if node in (p.get("tools") or []):
            if int(p["seq"]) == 0:
                raise SystemExit(f"「{node}」的结果落在链首 seq0，之前没有可复用的一致点；"
                                 f"要重做全部流程请直接发起新一轮。")
            return int(p["seq"]) - 1
    raise SystemExit("不该走到这里：链上第一次命中之前已经 return")


# ---------------------------------------------------------------- 调用

def client(args: argparse.Namespace, token: str) -> httpx.Client:
    return httpx.Client(base_url=args.base_url, timeout=args.timeout,
                        headers={"Authorization": f"Bearer {token}"})


def do_wait(c: httpx.Client, args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    """轮询到终态。fork/resume 都是投一帧 MQ 消息，返回时执行才刚排队——不等就等于没验。"""
    deadline = time.time() + args.wait_timeout
    row: dict[str, Any] = {}
    while time.time() < deadline:
        row = (c.get(f"/runs/{run_id}").json() or {}).get("run") or {}
        if row.get("status") in TERMINAL:
            return row
        time.sleep(args.poll_sec)
    raise SystemExit(f"等 {run_id} 到终态超时（{args.wait_timeout}s），最后状态 {row.get('status')}")


def do_frames(args: argparse.Namespace, token: str, conversation: str, produce) -> str:
    """边跑边看回投：先连 WS 再触发执行，否则最前面几段 delta 已经错过了。

    等到 answer 帧才收——`/chat` 返回时执行才刚排队，这时候松手就等于什么都没看到。
    """
    import asyncio

    import websockets

    async def _run() -> str:
        url = args.base_url.replace("http://", "ws://").replace("https://", "wss://")
        async with websockets.connect(f"{url}/ws/{conversation}?token={token}") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 20))
            print(f"[ws] {first.get('type')} session={first.get('session_id')}", flush=True)

            async def watch() -> None:
                while True:
                    msg = json.loads(await ws.recv())
                    t = msg.get("type")
                    if t == "delta":
                        print(msg.get("text") or "", end="", flush=True)
                    elif t == "stream_end":
                        print(flush=True)
                    else:
                        brief = str(msg.get("answer") or msg.get("name")
                                   or msg.get("title") or msg.get("status") or "")[:80]
                        print(f"[ws] {t} {brief}".rstrip(), flush=True)
                    if t == "answer":
                        return

            run_id = produce()
            try:
                await asyncio.wait_for(watch(), args.wait_timeout)
            except asyncio.TimeoutError:
                print(f"\n[ws] {args.wait_timeout}s 内没等到 answer 帧", flush=True)
            return run_id

    return asyncio.run(_run())


# ---------------------------------------------------------------- 子命令

def cmd_runs(args: argparse.Namespace) -> int:
    token = read_token(args)
    with client(args, token) as c:
        r = c.get(f"/convs/{args.conversation}/runs")
        if r.status_code != 200:
            raise SystemExit(f"{r.status_code} {r.text[:200]}")
        runs = r.json().get("runs") or []
        emit(args, runs, f"会话 {args.conversation} 共 {len(runs)} 条执行（最近的在前）:\n"
                         + ("\n".join(fmt_run(x) for x in runs) or "（无）"))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    token = read_token(args)
    with client(args, token) as c:
        r = c.get(f"/runs/{args.run}/history")
        if r.status_code != 200:
            raise SystemExit(f"{r.status_code} {r.text[:200]}")
        points = r.json().get("points") or []
        if args.before_node:
            print(f"--before-node {args.before_node} → 分叉点 seq="
                  f"{point_before_tool(points, args.before_node)}")
        emit(args, points, f"run {args.run} 的一致点链（{len(points)} 个点）:\n{fmt_points(points)}")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    token = read_token(args)
    with client(args, token) as c:
        at_seq = args.at_seq
        if at_seq is None:
            if not args.before_node:
                raise SystemExit("要么给 --at-seq，要么给 --before-node（分叉点不能靠猜）")
            pts = (c.get(f"/runs/{args.run}/history").json() or {}).get("points") or []
            at_seq = point_before_tool(pts, args.before_node)
            print(f"分叉点按「{args.before_node} 的结果第一次落下」算出：seq={at_seq}")
        body = {"at_seq": at_seq, "message": args.message or "",
                "rerun_nodes": args.rerun_node or []}
        r = c.post(f"/runs/{args.run}/fork", json=body)
        if r.status_code != 200:
            raise SystemExit(f"{r.status_code} {r.text[:200]}")
        child = r.json()["run_id"]
        print(f"已投分叉：父 {args.run}@seq{at_seq} → 子 run {child}"
              + (f"，重做节点 {args.rerun_node}" if args.rerun_node else ""))
        if args.wait:
            row = do_wait(c, args, child)
            print(fmt_run(row))
            pts = (c.get(f"/runs/{child}/history").json() or {}).get("points") or []
            print(fmt_points(pts))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    token = read_token(args)
    with client(args, token) as c:
        r = c.post(f"/runs/{args.run}/resume",
                   json={"at_seq": args.at_seq} if args.at_seq is not None else {})
        if r.status_code != 200:
            raise SystemExit(f"{r.status_code} {r.text[:200]}")
        print(f"已投续跑：{args.run}" + (f"@seq{args.at_seq}" if args.at_seq is not None else "（取链尾）"))
        if args.wait:
            print(fmt_run(do_wait(c, args, r.json()["run_id"])))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    token = read_token(args)
    def produce(c: httpx.Client) -> str:
        r = c.post("/chat", json={"conversation_id": args.conversation,
                                  "message": args.message})
        if r.status_code != 200:
            raise SystemExit(f"{r.status_code} {r.text[:200]}")
        return r.json()["run_id"]
    with client(args, token) as c:
        if args.frames:
            rid = do_frames(args, token, args.conversation, lambda: produce(c))
            print(f"run_id={rid}")
            if args.wait:
                print(fmt_run(do_wait(c, args, rid)))
            return 0
        rid = produce(c)
        print(f"已投一句话：会话 {args.conversation} → run {rid}")
        if args.wait:
            print(fmt_run(do_wait(c, args, rid)))
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    """新身份：token 明文只在那一次响应里出现，这里直接写进文件，屏幕上只有 user_id。"""
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as c:
        r = c.post("/register", json={"device_name": args.device_name or "runs-cli"})
        if r.status_code != 200:
            raise SystemExit(f"{r.status_code} {r.text[:200]}")
        body = r.json()
    if not args.token_file:
        raise SystemExit("不落文件就没法保住凭证：--token-file 给一个路径（建议 0600）")
    path = Path(args.token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 追加而不是覆写：--token-file 同时是「读凭证」的那个入口（比如 run_migrate 的遗留
    # 凭证文件），register 一次就把别人的 token 抹掉是不可接受的副作用。
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{body['user_id']}\t{body['token']}\n")
    try:
        path.chmod(0o600)      # Windows 上是装饰性的，尽力而为
    except OSError:
        pass
    print(f"已登记身份 {body['user_id']}，token 追加进 {path}"
          f"（{len(body['token'])} 字符，值不落屏幕）")
    return 0


def _common(target: argparse.ArgumentParser, *, defaults: bool) -> None:
    """全局项挂两遍（主解析器 + 各子命令）。

    子命令那份用 `argparse.SUPPRESS` 作默认值：否则「写在子命令前面的 --json/--token-file」
    会被子解析器的默认值覆盖掉——同一面旗在两处出现时，后解析的那份说了算。
    """
    d = (lambda v: v) if defaults else (lambda v: argparse.SUPPRESS)
    target.add_argument("--base-url", default=d(os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000")))
    target.add_argument("--token", default=d(None), help="一次性调试用：会出现在进程列表里")
    target.add_argument("--token-file", default=d(os.getenv("AGENT_TOKEN_FILE", "")),
                        help="一行 `user_id<TAB>token`（run_migrate 的凭证格式）或裸 token")
    target.add_argument("--user", default=d(None), help="凭证文件里取哪个身份的 token")
    target.add_argument("--timeout", type=float, default=d(60.0))
    target.add_argument("--json", action="store_true", default=d(False),
                        help="原样输出 JSON，便于脚本再接一手")


def _exec_opts(target: argparse.ArgumentParser) -> None:
    target.add_argument("--wait", action="store_true",
                        help="等这条执行到终态（fork/resume 只是投一帧 MQ，不等就没结果可看）")
    target.add_argument("--wait-timeout", type=float, default=900.0)
    target.add_argument("--poll-sec", type=float, default=3.0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    _common(p, defaults=True)
    glob = argparse.ArgumentParser(add_help=False)
    _common(glob, defaults=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("runs", parents=[glob], help="列本会话的全部执行（含分叉出来的）")
    a.add_argument("--conversation", required=True)
    a.set_defaults(fn=cmd_runs)

    h = sub.add_parser("history", parents=[glob], help="一条 run 走过的一致点链")
    h.add_argument("--run", required=True)
    h.add_argument("--before-node", default=None, help="顺带算出该节点第一次落结果前的分叉点 seq")
    h.set_defaults(fn=cmd_history)

    f = sub.add_parser("fork", parents=[glob], help="回到某个一致点开一条新执行")
    f.add_argument("--run", required=True)
    f.add_argument("--at-seq", type=int, default=None)
    f.add_argument("--before-node", default=None, help="与 --at-seq 二选一：按节点名反推分叉点")
    f.add_argument("--message", default="", help="分叉后带进去的新诉求（空 = 沿用父 run 原话）")
    f.add_argument("--rerun-node", action="append", default=[],
                   help="要真重做的节点，可重复；服务端会连带其下游产物一起作废")
    _exec_opts(f)
    f.set_defaults(fn=cmd_fork)

    r = sub.add_parser("resume", parents=[glob], help="续跑这条执行（可先回到某个一致点）")
    r.add_argument("--run", required=True)
    r.add_argument("--at-seq", type=int, default=None)
    _exec_opts(r)
    r.set_defaults(fn=cmd_resume)

    ch = sub.add_parser("chat", parents=[glob], help="投一句话并（可选）当场看回投帧")
    ch.add_argument("--conversation", required=True)
    ch.add_argument("--message", required=True)
    ch.add_argument("--frames", action="store_true", help="先连 WS 再投，逐帧打印 delta/工具/成片卡")
    _exec_opts(ch)
    ch.set_defaults(fn=cmd_chat)

    g = sub.add_parser("register", parents=[glob], help="登记新身份并把 token 只写进文件")
    g.add_argument("--device-name", default="runs-cli")
    g.set_defaults(fn=cmd_register)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
