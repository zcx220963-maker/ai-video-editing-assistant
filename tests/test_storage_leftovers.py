"""存储层三处遗留问题的离线验证（README §6）。

运行：  python tests/test_storage_leftovers.py

不联网、不起容器，全部跑在内存替身 + 本地临时目录上，逐条钉死三个修复：
① 主服务启动清扫：run_server._sweep_workspace 收回过期会话工作区，失败只告警不阻断；
② 技能删除清对象：SkillLoader.drop 删行的同时删附件对象，对象存储抖动留 best-effort；
③ 内容缓存双前缀：cache_root 只是根，objects/ 那一层归 ContentCache，端到端只有一层。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.skill import SkillLoader
from agent_framework.storage import StorageUnavailable, build_storage
from agent_framework.storage.object_store import ContentCache
from run_server import _sweep_workspace

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _write_skill(root: Path, name: str, files: dict[str, bytes] | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\n正文",
                                encoding="utf-8")
    for rel, data in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


# --------------------------------------------------------------------------
# ① 主服务启动清扫会话工作区
# --------------------------------------------------------------------------


async def test_sweep(tmp: Path) -> None:
    s = build_storage("memory", cache_root=tmp / "sweep_c", workspace_root=tmp / "sweep_ws")
    ws = s.workspace.root
    old = ws / "sess_old" / "art_old"          # {会话}/{产物} 两层，sweep 逐产物比 mtime
    old.mkdir(parents=True, exist_ok=True)
    (old / "x.tmp").write_bytes(b"stale")
    fresh = ws / "sess_new" / "art_new"
    fresh.mkdir(parents=True, exist_ok=True)
    (fresh / "y.tmp").write_bytes(b"keep")
    expired = time.time() - 600
    os.utime(old, (expired, expired))

    n = _sweep_workspace(s, 300)               # TTL 300s：art_old 过期、art_new 未过期
    check(n == 1 and not (ws / "sess_old").exists(),
          f"sweep 收回过期会话工作区目录（{n} 个文件）")
    check(fresh.exists(), "未过期工作区目录不受影响")

    # 根不存在（首次启动，还没建过任何工作区）→ 返回 0，不抛
    s2 = build_storage("memory", cache_root=tmp / "sweep_c2",
                       workspace_root=tmp / "nope_missing")
    check(_sweep_workspace(s2, 300) == 0, "工作区根不存在时返回 0、不抛")

    # sweep_stale 抛错 → 只告警、返回 0，绝不抛穿启动
    s3 = build_storage("memory", cache_root=tmp / "sweep_c3", workspace_root=tmp / "sweep_ws3")

    def _boom(*a, **k):
        raise RuntimeError("模拟清扫失败")

    s3.workspace.sweep_stale = _boom           # type: ignore[method-assign]
    check(_sweep_workspace(s3, 300) == 0, "sweep_stale 抛错时 _sweep_workspace 只告警返回 0")


# --------------------------------------------------------------------------
# ② 技能删除连带清理附件对象
# --------------------------------------------------------------------------


async def test_skill_drop(tmp: Path) -> None:
    storage = build_storage("memory", cache_root=tmp / "sk_c", workspace_root=tmp / "sk_w")
    await storage.start()
    try:
        loader = SkillLoader(storage)

        _write_skill(tmp / "skills", "withfile", {"notes.md": b"hello"})
        await loader.sync_from_dir(tmp / "skills")
        check(await storage.objects.head("skills/withfile/notes.md") is not None,
              "带附件技能：对象确实在对象存储里")
        check(await loader.drop("withfile") == 1, "drop 返回删除的行数")
        check(await loader.get("withfile") is None, "drop 后技能行消失")
        check(await storage.objects.head("skills/withfile/notes.md") is None,
              "drop 连带删除附件对象（不留孤儿字节）")

        # 对象删除失败时 best-effort：仍删行、不把异常抛出 drop
        _write_skill(tmp / "skills2", "flaky", {"a.txt": b"x"})
        await loader.sync_from_dir(tmp / "skills2")

        async def _boom(key):
            raise StorageUnavailable("模拟对象存储删除失败")

        storage.objects.delete = _boom         # type: ignore[assignment]
        raised = False
        n = -1
        try:
            n = await loader.drop("flaky")
        except Exception:                      # noqa: BLE001
            raised = True
        check(not raised, "对象删除失败也不把异常抛出 drop")
        check(n == 1 and await loader.get("flaky") is None,
              "对象删不掉时仍删掉技能行（不留悬而不删的行）")
    finally:
        await storage.close()


# --------------------------------------------------------------------------
# ③ 内容缓存 objects/objects/ 双层前缀
# --------------------------------------------------------------------------


async def test_cache_prefix(tmp: Path) -> None:
    cfg_path = (Path(__file__).resolve().parent.parent
                / "examples" / "storyline" / "config.toml")
    cache_root = tomllib.loads(cfg_path.read_text(encoding="utf-8"))["storage"]["cache_root"]
    check(Path(cache_root).name != "objects",
          f"配置里 cache_root 不再自带 objects 段（{cache_root}）")

    # 端到端：配置值交给 ContentCache 后，路径里只有一层 objects
    cache = ContentCache(cache_root)           # 纯路径构造，不落盘
    depth = sum(1 for p in cache.dir.parts if p == "objects")
    check(depth == 1, f"内容缓存路径只有一层 objects（实得 {cache.dir}）")
    check(cache.path_for("a" * 64) == cache.dir / "aa" / ("a" * 64),
          "path_for 落在 objects/{hex[:2]}/{hex}，无 objects/objects")

    # 缓存可丢弃：遗留的双层条目按 miss 处理、由 localize 回源重算（不写迁移）
    c2 = ContentCache(tmp / "cache")
    legacy = c2.dir / "objects" / "bb" / ("b" * 64)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"old")
    hit = await c2.fetch_to("b" * 64, tmp / "out", "f.bin")
    check(hit is None, "遗留 objects/objects 条目命中不到（视为 miss）")
    check(c2.path_for("b" * 64) != legacy, "新单层路径与遗留双层路径不是同一个落点")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="storage_leftovers_"))
    for title, fn in [
        ("① 主服务启动清扫工作区", test_sweep),
        ("② 技能删除清附件对象", test_skill_drop),
        ("③ 内容缓存双层前缀", test_cache_prefix),
    ]:
        print(f"\n[{title}]")
        await fn(tmp)

    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
