"""R11 供给面巡检：把 ㉔ / T53 / T54 三处的读数打在同一张回执上。

**为什么要一个入口。** 这三处判的是同一件事的三个面：**模型看到的供给面**（㉔：schema、
描述、活库三者对不对得上）、**库里的件能不能被按角色找到**（T53：`asset_role` 标没标全）、
**被找到的件缩到片子里会不会糊**（T54：最细笔画实测）。分三个脚本跑，就会出现
「㉔ 绿了、但 part 档一件都没有」这种各说各话的局面——本卷要的正是把三处并排看。

**它读库，所以它有时需要真机**（`docker exec creation-app python /scripts/asset_gate.py`）。
库连不上时 ㉔ 的那一半记**未判**（不是过），退出码 2——**未判不许被当成绿**
（D-未判不绿），但也别报成红：没量到与不合格是两件事。

退出码：0 全绿 / 1 有打回 / 2 只有未判（有打回时以 1 为准）。

用法：
    python scripts/asset_gate.py                # 读活库，打一张完整回执
    python scripts/asset_gate.py --json         # 同上，机器可读
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 让 asset_common 可导入


def _add_backend_to_path() -> None:
    """把 `agent_framework` 放上 import 路径（同 `asset_ingest.py` 里的那一份：
    宿主是 `<仓库根>/backend`，容器是 `/app`）。"""
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "backend", Path("/app")):
        if (cand / "agent_framework").is_dir():
            sys.path.insert(0, str(cand))
            return


_add_backend_to_path()

from agent_framework.asset_scale import scale_facts_summary           # noqa: E402
from agent_framework.asset_sources import (                           # noqa: E402
    role_facts, supply_surface_report)


async def run(*, as_json: bool = False) -> int:
    from agent_framework.storage import build_storage
    storage = build_storage("pg_minio")
    await storage.start()
    try:
        report = await supply_surface_report(storage)
        try:
            rows = await storage.assets.list_assets(enabled=True, limit=100000)
        except Exception as e:      # noqa: BLE001 —— 取不到行不阻断㉔的结论，如实记一句
            rows = []
            report["undetermined"] = list(report["undetermined"]) + [
                f"库行没读到（{type(e).__name__}: {e}）——T53/T54 两张账今天判不了"]
        report["roles"] = role_facts(rows)
        report["scale"] = scale_facts_summary(rows)
    finally:
        await storage.close()

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(report)
    if report["errors"]:
        return 1
    return 2 if report["undetermined"] else 0


def _print(report: dict) -> None:
    print("=== R11 供给面巡检（㉔ / T53 / T54）===")
    print(f"schema 里的源   ：{'、'.join(report['schema_sources']) or '（空）'}")
    print(f"可声明的源      ：{'、'.join(report['declared_sources']) or '（空）'}")
    print(f"注册表里的源    ：{'、'.join(report['registered_sources'])}")
    counts = report.get("live_counts")
    if counts is None:
        print("活库行数        ：**读不到**（㉔ 的「腿通不通」那一半记未判）")
    else:
        print("活库行数        ：" + "、".join(f"{k}={v}" for k, v in sorted(counts.items())))
    roles = report["roles"]
    print(f"asset_role 分布 ：" + "、".join(f"{k}={v}" for k, v in roles["counts"].items())
          + f"（未判 {roles['undetermined']} / 共 {roles['total']}）")
    scale = report["scale"]
    print(f"scale_safe 分布 ：过 {scale['safe']} / 不过 {scale['unsafe']} / 未判 "
          f"{scale['undetermined']}（阈值 {scale['min_stroke_px']} px @ 目标 {scale['target_px']} px）")
    if scale["unsafe_sample"]:
        print(f"  不过的件（前几件）：{'、'.join(scale['unsafe_sample'])}")
    for line in report["errors"]:
        print(f"✗ {line}")
    for line in report["undetermined"]:
        print(f"… {line}")
    print("结论：" + ("红（有打回）" if report["errors"]
                     else ("未判（没打回，但有一半没量到）" if report["undetermined"] else "绿")))


def main() -> int:
    # 判词里带「㉔」「✗」这类字符：Windows 宿主控制台默认 GBK，直接 print 会
    # UnicodeEncodeError——巡检自己炸掉会把「没读到」伪装成「读到了没问题」。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:       # noqa: BLE001 —— 老解释器没有 reconfigure 就算了
        pass
    parser = argparse.ArgumentParser(description="R11 供给面巡检（㉔ / T53 / T54）")
    parser.add_argument("--json", action="store_true", help="输出机器可读回执")
    args = parser.parse_args()
    return asyncio.run(run(as_json=args.json))


if __name__ == "__main__":
    sys.exit(main())
