"""R3 第一步：逐源可达性探测，留回执。

§5.2 探测清单：宿主机 + creation-app 容器各一次。
回执格式：源 / 端点 / HTTP 码 / 实际字节数 / 发起位置 / 时延ms

用法：
    python scripts/probe_asset_sources.py                    # 宿主机探测
    python scripts/probe_asset_sources.py --location container # 容器内探测
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 让 asset_common 可导入


def _add_backend_to_path() -> None:
    """把 `agent_framework` 放上 import 路径（同 `asset_ingest.py` 里那一份：
    宿主是 `<仓库根>/backend`，容器是 `/app`）。"""
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "backend", Path("/app")):
        if (cand / "agent_framework").is_dir():
            sys.path.insert(0, str(cand))
            return


_add_backend_to_path()

from agent_framework.asset_sources import probe_entries  # noqa: E402

from asset_common import classify_failure  # noqa: E402

UA = "CodeArts-AssetProbe/1.0 (https://codearts.huaweicloud.com)"

#: 探测清单**由注册表生成**（R11-T50）。它曾经是这里手写的 9 条，与入库脚本的 5 条腿、
#: schema 里的 8 个源名互不相等——三份清单各说各的，正是「供给面撒谎」的入口。
#: 量的是「这个源今天通不通」，与「模型能不能点它」是两件事：**没跑过的腿照样探**
#: （那正是 T52 的输入），**射程外的源不探**（Q110：量它没有用途）。
PROBES = probe_entries()


def probe_one(source: str, endpoint: str, url: str, location: str) -> dict:
    started = time.monotonic()
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        resp = urllib.request.urlopen(req, timeout=20)
        ms = int((time.monotonic() - started) * 1000)
        data = resp.read(8192)
        return {
            "source": source, "endpoint": endpoint, "url": url,
            "http_code": resp.status, "bytes": len(data),
            "location": location, "latency_ms": ms,
            "ok": True, "error": "",
        }
    except Exception as e:  # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        # 以前这里 http_code 一律写 0——把「对面回 403」这个**最有信息量的一格**丢了，
        # 回执就只剩一句看不懂的异常名。改走共用的分类器（T10）：码照抄真的那个，
        # 并附一句「代码还救不救得了」。
        rec = classify_failure(e)
        return {
            "source": source, "endpoint": endpoint, "url": url,
            "http_code": rec["http_code"] or 0, "bytes": 0,
            "location": location, "latency_ms": ms,
            "ok": False, "fixable": rec["fixable"], "advice": rec["advice"],
            "error": f"{rec['kind']}: {rec['error']}",
        }


def run(location: str) -> list[dict]:
    return [probe_one(s, ep, url, location) for s, ep, url in PROBES]


def main() -> int:
    parser = argparse.ArgumentParser(description="R3 素材源可达性探测")
    parser.add_argument("--location", default="host",
                        choices=["host", "container"],
                        help="发起位置")
    parser.add_argument("--output", default="",
                        help="输出 JSON 文件路径（默认 stdout）")
    args = parser.parse_args()

    results = run(args.location)
    for r in results:
        status = "OK " if r["ok"] else "ERR"
        print(f"{status} {r['http_code']:>3}  {r['bytes']:>6}B  {r['latency_ms']:>5}ms  "
              f"{r['source']}/{r['endpoint']}  [{r['error']}]", file=sys.stderr)

    blob = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(blob, encoding="utf-8")
        print(f"回执已写入 {args.output}", file=sys.stderr)
    else:
        print(blob)
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n{ok_count}/{len(results)} 源可达", file=sys.stderr)
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())