# -*- coding: utf-8 -*-
"""R3/T14：素材库字节的只读巡检——「引用计数为 0 且超 30 天」清单。

**这个脚本只读，没有 `--delete`，这是决定不是遗漏。** 库素材是全局共享的
（`assets/` 命名空间，`asset_library` 是唯一登记口）：一条字节可能被几十个会话
引用过，删掉谁都补不回来。§9-Q26 因此把这件事定成人工判读——脚本负责把账摊开
说清楚，删不删由人看着办。

它和 `session_gc.py` 是**同一条边界的两侧**：那边删会话时故意不列 `assets/`
（会话回收清的是工作区，不是用户已经拥有的东西；见 `session_gc.OBJECT_PREFIXES`
的注释），这边负责回答「那 `assets/` 自己会不会越攒越多」。答案是会，所以要有
人来看，但不能自动删。

四栏输出：

- ``dangling``    库里有行、桶里字节没了。检索能搜到、渲染到那一镜才炸——最该先修。
- ``orphan``      桶里有字节、`asset_library` 里没有行指向它，且年龄判得出、超过阈值。
                  最可能是某次入库半途崩了（字节先落、行后写），或入库后行被删。
- ``young``       同上，但不满阈值。列出来只为说明「这批还在生长期」，不催人处理。
- ``unknown_age`` 桶里有字节、库里没行，但对象存储没给 `last_modified`。
                  **不计入可清理项**：说不清年龄就不许当成「年老」——D-未判不绿
                  在这条链上的样子。离线 `memory` 后端就是这个形态（put 一律回
                  `last_modified=0.0`），所以自检永远不会把「判不出」谎报成「超龄」。

用法：
    python scripts/asset_gc_report.py                      # 巡检线上（pg_minio）
    python scripts/asset_gc_report.py --days 30 --json out.json
    python scripts/asset_gc_report.py --backend memory      # 不连网，看链路通不通

退出码：0 = 没有要人看的项；1 = `orphan` 或 `dangling` 非空（可挂告警）。
未判的 `unknown_age` **不算**非空——它不构成一个结论。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
for _p in (Path(__file__).resolve().parent, _REPO / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 素材库的命名空间。库素材一律落在它下面（`scripts/asset_ingest.py` 各源的 object_key）。
ASSET_PREFIX = "assets/"
DEFAULT_DAYS = 30
#: 人读那栏最多列几条，多出来的只报数——清单是给人看的，不是给人滚屏的。
HEAD_LIMIT = 20


async def scan(storage: Any, *, prefix: str = ASSET_PREFIX,
               days: int = DEFAULT_DAYS, now: float | None = None) -> dict[str, Any]:
    """按前缀巡检：**只读**，回一份账。函数里没有任何 delete 调用。

    年龄判据是 ``now - last_modified >= days*86400``；``last_modified <= 0`` 表示
    对象存储没给时间戳，走 `unknown_age` 一栏，绝不并进 `orphan`。
    """
    now = time.time() if now is None else float(now)
    cutoff = days * 86400

    objs = await storage.objects.list_prefix(prefix)
    rows = await storage.assets.db.select("asset_library")
    registered = {str(r["object_key"]) for r in rows if r.get("object_key")}
    in_bucket = {i.key for i in objs}

    orphan: list[dict[str, Any]] = []
    young: list[dict[str, Any]] = []
    unknown_age: list[dict[str, Any]] = []
    for info in objs:
        if info.key in registered:
            continue
        age = now - float(info.last_modified or 0.0)
        item = {"key": info.key, "bytes": int(info.bytes),
                "age_days": (round(age / 86400, 1) if info.last_modified else None)}
        if not info.last_modified:
            unknown_age.append(item)
        elif age >= cutoff:
            orphan.append(item)
        else:
            young.append(item)

    # 反方向：库指向的字节不在桶里。这一栏的存在是为了让「引用计数」这个说法成立——
    # 只查一个方向的话，两边一起烂掉（行和字节同时没了）永远看不出来。
    dangling = [{"asset_id": str(r.get("id", "")), "object_key": str(r["object_key"]),
                 "source": str(r.get("source", ""))}
                for r in rows if r.get("object_key") and str(r["object_key"]) not in in_bucket]

    orphan.sort(key=lambda x: -x["age_days"])
    return {
        "prefix": prefix, "days": days, "scanned_at": now,
        "objects_scanned": len(objs), "registered_keys": len(registered),
        "orphan": orphan, "young": young, "unknown_age": unknown_age,
        "dangling": dangling, "read_only": True,
    }


def _print_list(title: str, items: list[dict[str, Any]], fmt) -> None:
    if not items:
        print(f"  {title}: 0")
        return
    print(f"  {title}: {len(items)}")
    for it in items[:HEAD_LIMIT]:
        print(f"      {fmt(it)}")
    if len(items) > HEAD_LIMIT:
        print(f"      … 还有 {len(items) - HEAD_LIMIT} 条（--json 看全）")


def render(rep: dict[str, Any]) -> None:
    print(f"素材库巡检：前缀 {rep['prefix']}  阈值 {rep['days']} 天  "
          f"扫到 {rep['objects_scanned']} 个对象，其中 {rep['registered_keys']} 个键被库登记",
          file=sys.stderr)
    _print_list("dangling（库里有行、桶里没字节）", rep["dangling"],
                lambda d: f"{d['asset_id']} → {d['object_key']}  [{d['source']}]")
    _print_list(f"orphan（没人指向且 ≥{rep['days']} 天）", rep["orphan"],
                lambda d: f"{d['key']}  {d['bytes']}B  {d['age_days']} 天")
    _print_list("young（没人指向、未满阈值）", rep["young"],
                lambda d: f"{d['key']}  {d['bytes']}B  {d['age_days']} 天")
    _print_list("unknown_age（没人指向，但对象存储没给时间戳——未判，不作为可清理项）",
                rep["unknown_age"], lambda d: f"{d['key']}  {d['bytes']}B")
    actionable = len(rep["orphan"]) + len(rep["dangling"])
    print(f"\n要人看的：{actionable} 条（未判 {len(rep['unknown_age'])} 条不计入）。"
          f"本脚本只读，删除请人工确认后单独执行（§9-Q26）。", file=sys.stderr)


async def main_async(args: argparse.Namespace) -> int:
    from agent_framework.storage import build_storage
    storage = build_storage(args.backend)
    await storage.start()
    try:
        rep = await scan(storage, prefix=args.prefix, days=args.days)
    finally:
        await storage.close()

    render(rep)
    blob = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(blob, encoding="utf-8", newline="\n")
        print(f"回执已写入 {args.json}", file=sys.stderr)
    else:
        print(blob)
    return 1 if (rep["orphan"] or rep["dangling"]) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="素材库字节只读巡检（不做自动删除）")
    ap.add_argument("--backend", default="pg_minio",
                    help="存储后端：pg_minio（线上）/ memory（不连网自检）")
    ap.add_argument("--prefix", default=ASSET_PREFIX, help="巡检命名空间前缀")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="「超龄」阈值（天），默认 30")
    ap.add_argument("--json", default="", help="把回执写成 JSON 文件（默认 stdout）")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
