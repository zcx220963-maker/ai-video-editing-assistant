# -*- coding: utf-8 -*-
"""把 artifacts 表里的遗留本机绝对路径改写成对象引用（`obj:`）——payload 引用契约的补齐迁移。

代码侧已经不再往持久化产物里写工作区路径（见 ``agent_framework.storage.object_store``
的 ``to_ref`` / ``ref_key`` 与 ``tests/test_payload_refs.py``），但**改动之前落库的那些行**
还带着 ``C:\\...\\.storyline\\workspace\\...`` 这样的本机路径：工作区按设计可丢弃，
``sweep_stale`` 一收、或换一台机器，那些产物就指向空气。本脚本把能救的救回来：

* ``.../material/mat-xxxx.mp4`` → 按 material_id 查 materials 表，换成它登记的对象键；
* 仍在本机存在的自产文件（配音 / 转场 / 占位 BGM）→ 先 publish 进对象存储
  （``derived/{会话}/{产物}/{环节}/{文件名}``），再把路径换成引用；
* 字节已经没了的 → **不动**，如实报数：那只能靠重跑产出它的节点，编一个引用就是造假。

只改有绝对路径的行，改完的 payload 与代码现写的形状完全一致；幂等——再跑一遍零改动。
默认 dry-run，``--apply`` 才写库。

跑法（需 docker compose up -d、.env 五项已填）：
    python -u scripts/backfill_payload_refs.py --config examples/storyline/config.toml
    python -u scripts/backfill_payload_refs.py --config examples/storyline/config.toml --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import build_storage, ref_key, to_ref  # noqa: E402
from agent_framework.storage.object_store import _safe_basename  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

# Windows 盘符路径与 POSIX 绝对路径都算；带扩展名才像文件（避免误伤标题里的一句话）
ABS_PATH = re.compile(r"(?:^[a-zA-Z]:[\\/]|^/)[^\n]*?\.[A-Za-z0-9]{1,5}$")
MATERIAL_ID = re.compile(r"^(mat-[0-9a-f]+)\.[A-Za-z0-9]{1,5}$")
# 工作区里节点自产的那几层：目录名即 derived 键里的「环节」。render/ 不在里面——
# 那是渲染期现取的副本，另存一份进 derived/ 只会多出一堆重复字节，留着如实报数。
STAGES = ("voiceover", "ai_transitions", "bgm")


def _walk(v: Any):
    """产出 (容器, 键, 值) 三件套，覆盖 dict/list 里嵌到任意深的字符串。"""
    if isinstance(v, dict):
        items = list(v.items())
        for k, x in items:
            if isinstance(x, str):
                yield v, k, x
            else:
                yield from _walk(x)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            if isinstance(x, str):
                yield v, i, x
            else:
                yield from _walk(x)


def looks_like_local_path(s: str) -> bool:
    return bool(ABS_PATH.match(s)) and ref_key(s) is None


def stage_of(s: str) -> str:
    parts = re.split(r"[\\/]", s)
    for seg in reversed(parts[:-1]):
        if seg in STAGES:
            return seg
        if seg == "src" and len(parts) >= 3 and parts[-3] in STAGES:
            return parts[-3]      # .../render/src/<hash>/<name>
    return ""


async def resolve(storage, sess: str, art: str, value: str) -> tuple[str | None, str]:
    """一个本机路径 → (引用, 依据)；救不回来时引用为 None，依据说明为什么。"""
    name = _safe_basename(value)
    m = MATERIAL_ID.match(name)
    if m:
        row = await storage.db.get_by_pk("materials", {"id": m.group(1)})
        key = (row or {}).get("object_key")
        if key and await storage.objects.head(key) is not None:
            return to_ref(key), "materials 表登记的对象键"
        return None, f"素材行或其字节已不在（{m.group(1)}）"
    stage = stage_of(value)
    p = Path(value)
    if not stage or not p.is_file():
        return None, "字节不在本机，也没登记过对象键" if not p.is_file() else "不在自产环节目录里"
    return await storage.workspace.publish_derived(
        p, session_id=sess, artifact_id=art, stage=stage), f"本机文件已 publish 进 derived/{stage}"


async def rewrite_row(storage, sess: str, art: str, node: str, payload: Any,
                      *, apply: bool, report: "Report") -> Any:
    changed = False
    for container, k, v in list(_walk(payload)):
        if not looks_like_local_path(v):
            continue
        ref, why = await resolve(storage, sess, art, v)
        if ref is None:
            report.note_unresolved(node, v, why)
            continue
        report.note_fixed(node, v, ref)
        if apply:
            container[k] = ref
            changed = True
    if apply and changed:
        await storage.artifacts(sess, art).put(node, payload)   # 主键与 updated_at 由仓储定
    return payload


class Report:
    def __init__(self) -> None:
        self.rows = 0
        self.rows_with_paths = 0
        self.fixed: list[tuple[str, str, str]] = []
        self.unres: list[tuple[str, str, str]] = []

    def note_fixed(self, node: str, old: str, ref: str) -> None:
        self.fixed.append((node, old, ref))

    def note_unresolved(self, node: str, old: str, why: str) -> None:
        self.unres.append((node, old, why))

    def dump(self, apply: bool) -> None:
        print(f"扫过产物行 {self.rows}，其中 {self.rows_with_paths} 行带本机绝对路径；"
              f"命中 {len(self.fixed)} 处可改写、{len(self.unres)} 处救不回来")
        by_node: dict[str, int] = {}
        for node, _, _ in self.fixed:
            by_node[node] = by_node.get(node, 0) + 1
        print(f"  可改写（按节点）：{by_node or '—'}")
        why_node: dict[str, int] = {}
        for node, _, why in self.unres:
            why_node[f"{node}：{why}"] = why_node.get(f"{node}：{why}", 0) + 1
        print(f"  救不回来（按节点+原因）：{why_node or '—'}")
        for node, old, ref in self.fixed[:3]:
            print(f"    [改] {node}: {old[:70]} → {ref[:70]}")
        for node, old, why in self.unres[:3]:
            print(f"    [留] {node}: {old[:70]}（{why}）")
        if not apply:
            print("  （dry-run：没有写库。加 --apply 才落。）")


async def backfill(storage, *, apply: bool = False, limit: int = 0) -> Report:
    """扫全表逐行改写。``limit`` 只用于抽样试跑（按 session 排序截断）。"""
    rep = Report()
    rows = await storage.db.select("artifacts", order_by=["session_id", "artifact_id", "node"])
    if limit:
        rows = rows[:limit]
    for r in rows:
        rep.rows += 1
        payload = r.get("payload") or {}
        paths = [v for _, _, v in _walk(payload) if looks_like_local_path(v)]
        if not paths:
            continue
        rep.rows_with_paths += 1
        await rewrite_row(storage, r["session_id"], r.get("artifact_id") or "",
                          r["node"], payload, apply=apply, report=rep)
    return rep


async def main() -> int:
    ap = argparse.ArgumentParser(description="把 artifacts 表里的本机绝对路径改写成对象引用")
    ap.add_argument("--config", default="examples/storyline/config.toml")
    ap.add_argument("--apply", action="store_true", help="写库（缺省只扫不写）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 行（抽样试跑）")
    a = ap.parse_args()
    settings = Settings.load(Path(a.config))
    storage = build_storage(settings.storage.backend,
                            cache_root=settings.storage.cache_root,
                            workspace_root=settings.storage.workspace_root,
                            cache_max_gb=settings.storage.workspace_max_gb)
    await storage.start()
    try:
        rep = await backfill(storage, apply=a.apply, limit=a.limit)
        rep.dump(a.apply)
        # 幂等复核：改写过就再扫一遍，第二遍必须零命中
        if a.apply and rep.fixed:
            again = await backfill(storage, apply=False)
            print(f"  复核（第二遍）：带路径的行 {again.rows_with_paths}、"
                  f"命中 {len(again.fixed)} 处、救不回来 {len(again.unres)} 处")
            return 1 if again.fixed else 0
        return 0
    finally:
        await storage.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
