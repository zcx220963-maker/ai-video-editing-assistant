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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 让 asset_common 可导入

from asset_common import classify_failure  # noqa: E402

UA = "CodeArts-AssetProbe/1.0 (https://codearts.huaweicloud.com)"

PROBES = [
    ("wikimedia-commons", "api",
     "https://commons.wikimedia.org/w/api.php?action=query&list=categorymembers"
     "&cmtitle=Category:Photographs&cmtype=file&format=json&cmlimit=1"),
    ("wikimedia-commons", "file",
     "https://commons.wikimedia.org/wiki/Special:FilePath/Example.jpg?width=800"),
    ("iconify", "npm",
     "https://registry.npmjs.org/@iconify/json/latest"),
    ("polyhaven", "api",
     "https://api.polyhaven.com/assets"),
    ("ambientcg", "index",
     "https://ambientcg.com/get?category=2-Pass&format=jpg&size=2048"),
    ("open-peeps", "site",
     "https://openpeeps.com"),
    ("humaaans", "site",
     "https://humaaans.com"),
    ("kenney", "site",
     "https://kenney.nl"),
    ("quaternius", "site",
     "https://quaternius.com"),
]


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