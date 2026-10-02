# -*- coding: utf-8 -*-
"""一次性数据迁移（spec §10 / 批次 B5）：把 B1–B4 之前落在本地磁盘的运行态灌进 PG + MinIO。

跑法：
    docker compose up -d
    python -u run_migrate.py                      # 默认迁 .runtime 与 .storyline 两处遗留
    python -u run_migrate.py --dry-run            # 只扫只算，不写一行
    python -u run_migrate.py --emit-credentials legacy-credentials.txt
    python -u run_migrate.py --emit-credentials legacy-credentials.txt --reissue-unclaimed
                                        # 库已迁完（只剩占位哈希）时补发可登录凭证

幂等是这一层的硬要求（spec 出口判据「迁移跑两遍一致」）。每一类各有自己的自然键，
第二遍只会看到「跳过」而不是重复行，且**不覆盖库里已有的行**——库里那份可能更新：
    users           遗留 id 本身（无行才补；有行不重发凭证）
    messages        qa.migrated_from = "sessions/{user}/{conv}.jsonl#{行号}"
    checkpoints     run_id（主键）+ 链首 seq=0 的 full 一致点（正文不再只存在于指针行）
    scheduled_jobs  job id（主键）+ name（唯一）
    memories        (user_id, category)
    artifacts       (session_id, artifact_id, node)
    materials       (owner, sha256)：同一份字节不二次入库
    render_jobs     (session_id, artifact_id)

遗留身份（B5 之前浏览器自造的 user_id）没有凭证，反查不到 token 就登不进来。默认给它们
写一个**不可登录**的 token_hash 占位（身份存在 ≠ 可登录），历史先落地；运维若要把这些
老用户接回来，用 `--emit-credentials` 现发真凭证并交给本人——token 只写进那个文件，
不打印到标准输出，不写进数据库（库里永远只有 sha256）。

**已经迁完的库要补凭证**得再加 `--reissue-unclaimed`：那时身份行早就带着占位哈希在了，
`users()` 见行即跳，光给 `--emit-credentials` 会一类零迁入、凭证文件也不生成。加了它才把
「磁盘上有遗留目录、库里只有占位、而凭证文件里没记过」的那几行的哈希轮换成真 token。
库里只有一串 sha256，认不出它是占位还是真发过凭证，所以**那份文件就是唯一的发放记录**：
文件里写过的 id 永不轮换——换哈希等于把本人手上那把当场作废。

不迁移的（如实列出，不当成已完成）：`.runtime/workspace`、`.storyline/workspace`、
`.storyline/cache/objects` 与 `.storyline/cache/{session}` 都是**临时工作区与本地内容缓存**，
spec §6 明确它们不入对象存储、可随时删光；A2A 收件箱 / 任务板 / 子 Agent 的旧文件形态
（`{user}/{conv}/{agent}.jsonl`、`task_*.json`、`subagents.json`）在现存磁盘上零样本，
无 schema 可依据，因此不做猜测式兼容。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# README 写的跑法是 `python scripts/run_migrate.py`，此时 sys.path[0] 是 scripts/，
# 不把仓库根加进来就导不到 agent_framework。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework.ingest import _chunks_from_file, ingest_local_file
from agent_framework.memory import CATEGORIES
from agent_framework.storage import Storage, build_storage, new_id
from agent_framework.storage.media import probe
from agent_framework.storage.repositories import token_hash

# 附件引用：B2 之后的 .jsonl 把 material_id 写在话里（「用附件 mat-xxxx 剪…」）
_MAT_ID = re.compile(r"\bmat-[0-9a-zA-Z]{3,}\b")
_LEGACY_ID = re.compile(r"[0-9A-Za-z_.\-]{1,64}")


@dataclass
class Report:
    """一类数据的一次迁移账：扫到多少、迁入多少、跳过哪些及原因。"""

    kind: str
    scanned: int = 0
    migrated: int = 0
    skipped: list[str] = field(default_factory=list)

    def skip(self, ident: Any, reason: str) -> None:
        self.skipped.append(f"{ident}：{reason}")

    def line(self) -> str:
        tail = f"，跳过 {len(self.skipped)}" if self.skipped else ""
        return f"{self.kind:<16} 扫到 {self.scanned:>4}  迁入 {self.migrated:>4}{tail}"


def _iter_files(root: Path, pattern: str = "*") -> Iterator[Path]:
    if not root.is_dir():
        return iter(())
    return (p for p in sorted(root.rglob(pattern)) if p.is_file())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"读不出 JSON：{exc}") from exc


class Migrator:
    def __init__(self, storage: Storage, *, runtime_dir: Path, storyline_dir: Path,
                 dry_run: bool = False, credentials_path: Path | None = None,
                 max_bytes: int = 1024 ** 3, reissue_unclaimed: bool = False) -> None:
        self.s = storage
        self.runtime = Path(runtime_dir)
        self.storyline = Path(storyline_dir)
        self.dry = dry_run
        self.credentials_path = credentials_path
        self.max_bytes = max_bytes
        self.reissue = reissue_unclaimed
        self.issued: list[tuple[str, str]] = []      # (user_id, 明文 token)，只写文件不外泄
        self._cred_ids: set[str] | None = None

    def _cred_recorded(self) -> set[str]:
        """凭证文件里已经发过哪些身份。发过的就不再轮换——上一轮交给本人的那把 token
        会当场失效，而文件是这件事唯一的记录（库里只有 sha256，认不出它是占位还是真发过）。
        """
        if self._cred_ids is None:
            ids: set[str] = set()
            if self.credentials_path is not None and self.credentials_path.exists():
                for line in self.credentials_path.read_text(encoding="utf-8").splitlines():
                    uid = line.split("\t", 1)[0].strip()
                    if uid:
                        ids.add(uid)
            self._cred_ids = ids
        return self._cred_ids

    async def run(self) -> list[Report]:
        """跑一遍全部八类。存储由调用方构造并负责 close（内存替身 close 即失效）。"""
        await self.s.start()
        reports = [
            await self.users(),
            await self.sessions(),
            await self.checkpoints(),
            await self.scheduled_jobs(),
            await self.memories(),
            await self.artifacts(),
            await self.uploads(),
            await self.renders(),
        ]
        self._write_credentials()
        return reports

    # ---- 1. 身份：遗留 user_id 先有行，后面的外键才落得下 -------------------

    def _legacy_user_ids(self) -> list[str]:
        ids: set[str] = set()
        for root in (self.runtime / "sessions", self.runtime / "memory",
                     self.storyline / "uploads"):
            if not root.is_dir():
                continue
            for p in sorted(root.iterdir()):
                if p.is_dir():
                    ids.add(p.name)
        return sorted(ids)

    async def users(self) -> Report:
        rep = Report("users")
        for uid in self._legacy_user_ids():
            rep.scanned += 1
            if not _LEGACY_ID.fullmatch(uid):
                rep.skip(uid, "id 形态不合命名规范，跳过（不猜归属）")
                continue
            if await self.s.users.get(uid) is not None:
                if (self.reissue and self.credentials_path is not None
                        and uid not in self._cred_recorded()):
                    if self.dry:
                        rep.skip(uid, "dry-run：占位哈希可轮换成真凭证")
                        continue
                    token = secrets.token_urlsafe(32)
                    await self.s.db.update("users", {"token_hash": token_hash(token)},
                                           where={"id": uid})
                    self.issued.append((uid, token))
                    self._cred_recorded().add(uid)
                    rep.migrated += 1
                    continue
                rep.skip(uid, "身份行已存在（不重发凭证）")
                continue
            if self.dry:
                rep.skip(uid, "dry-run")
                continue
            if self.credentials_path is not None:
                token = secrets.token_urlsafe(32)
                await self._add_user(uid, token_hash(token))
                self.issued.append((uid, token))
            else:
                # 占位哈希：随机串 → sha256，对不上任何可用凭证（身份存在 ≠ 可登录）。
                await self._add_user(uid, token_hash(new_id("unclaimed", 24)))
            rep.migrated += 1
        return rep

    async def _add_user(self, user_id: str, hash_: str) -> None:
        await self.s.db.insert("users", {"id": user_id, "token_hash": hash_})

    def _write_credentials(self) -> None:
        if self.dry or self.credentials_path is None or not self.issued:
            return
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        with self.credentials_path.open("a", encoding="utf-8") as fh:
            for uid, token in self.issued:
                fh.write(f"{uid}\t{token}\n")
        try:
            self.credentials_path.chmod(0o600)
        except OSError:
            pass        # Windows 上权限位是装饰性的，尽力而为

    # ---- 2. 会话历史：.jsonl 的一行 = 一问一答两行 --------------------------

    async def sessions(self) -> Report:
        rep = Report("messages")
        root = self.runtime / "sessions"
        for f in _iter_files(root, "*.jsonl"):
            user, conv = f.parent.name, f.stem
            rel = f.relative_to(root).as_posix()
            lines = self._lines(f)
            rep.scanned += len(lines)
            have = {m["qa"].get("migrated_from")
                    for m in await self.s.messages.history(user, conv)
                    if isinstance(m.get("qa"), dict)}
            todo, done = [], []
            for no, line in enumerate(lines):
                (done if f"{rel}#{no}" in have else todo).append((no, line))
            for no, _ in done:
                rep.skip(f"{rel}#{no}", "已迁入（qa.migrated_from 命中）")
            if self.dry:
                for no, _ in todo:
                    rep.skip(f"{rel}#{no}", "dry-run")
                continue
            if not todo:
                continue        # 没有要写的行就不碰会话：updated_at 不该被重跑刷新
            try:
                await self.s.conversations.ensure(user, conv)
            except Exception as exc:                      # noqa: BLE001 - 归属不成立就不迁
                rep.skip(rel, f"会话不可写入（{exc}）")
                continue
            for no, line in todo:
                tag = f"{rel}#{no}"
                try:
                    await self._append_qa(user, conv, line, tag)
                except Exception as exc:                  # noqa: BLE001 - 一行坏不拖垮整类
                    rep.skip(tag, f"入库失败（{exc}）")
                    continue
                rep.migrated += 2                          # user + assistant 成对
        return rep

    @staticmethod
    def _lines(path: Path) -> list[dict[str, Any]]:
        out = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue        # 崩溃时写坏的尾行：跳过，历史不必因它全丢
            if isinstance(d, dict) and "question" in d:
                out.append(d)
        return out

    async def _append_qa(self, user: str, conv: str, qa: dict[str, Any],
                         tag: str) -> None:
        parts = qa.get("answer") or []
        if isinstance(parts, str):
            parts = [{"type": "answer", "content": parts}]
        question = str(qa.get("question") or "")
        text = "\n".join(str(p.get("content") or "") for p in parts
                         if isinstance(p, dict) and p.get("type") == "answer").strip()
        # 只认库里真有、且归属本人的 id：悬空引用会让重建的上下文说「无法使用」
        ids = [m for m in dict.fromkeys(_MAT_ID.findall(question))
               if (row := await self.s.materials.get(m)) and row["owner_user_id"] == user]
        await self.s.messages.append(user, conv, "user", content=question,
                                     attachments=ids,
                                     qa={"parts": [], "migrated_from": tag})
        await self.s.messages.append(user, conv, "assistant", content=text,
                                     qa={"parts": parts, "migrated_from": tag})

    # ---- 3. Checkpoint：指针行按 run_id 幂等，整段 messages 落成链首 full 一致点 ----

    async def checkpoints(self) -> Report:
        """老文件一行 = 一个全量快照；新形态是「指针行 + checkpoint_entries 增量链」。

        所以一条遗留 run 迁出来是两件事：指针行（head_seq 指向链首）+ 那条 seq=0 的
        full 基准。缺了后者，历史 run 就只剩「跑到第几轮」而正文没了，时间旅行无从谈起。
        """
        rep = Report("checkpoints")
        for f in _iter_files(self.runtime / "checkpoints", "*.json"):
            rep.scanned += 1
            try:
                row = _read_json(f)
                run_id = str(row["run_id"])
            except (ValueError, KeyError, TypeError) as exc:
                rep.skip(f.name, f"文件形态不对（{exc}）")
                continue
            if await self.s.checkpoints.load(run_id) is not None:
                rep.skip(run_id, "已存在（不覆盖库里较新的运行态）")
                continue
            if row.get("status") != "completed":
                rep.skip(run_id, f"status={row.get('status')}：陈旧未完成 run 一旦入库，"
                                 f"下次启动的自动恢复会重放它")
                continue
            if self.dry:
                rep.skip(run_id, "dry-run")
                continue
            msgs = row.get("messages") or []
            await self.s.checkpoints.save({**row, "head_seq": 0 if msgs else -1})
            if msgs:
                await self.s.checkpoints.append_entry(
                    run_id, kind="full", payload=msgs,
                    iteration=int(row.get("iteration") or 0), parent_seq=None)
            rep.migrated += 1
        return rep

    # ---- 4. 定时任务与心跳：同表两份文件 -----------------------------------

    async def scheduled_jobs(self) -> Report:
        rep = Report("scheduled_jobs")
        for name in ("heartbeat.json", "cron_jobs.json"):
            f = self.runtime / name
            if not f.is_file():
                continue
            try:
                jobs = (_read_json(f) or {}).get("jobs") or []
            except (ValueError, AttributeError) as exc:
                rep.skip(name, f"文件形态不对（{exc}）")
                continue
            for job in jobs:
                rep.scanned += 1
                jid = str(job.get("id") or f"mig-{hashlib.sha1(name.encode()).hexdigest()[:8]}")
                if not job.get("name"):
                    rep.skip(jid, "缺 name（scheduled_jobs.name 唯一且非空）")
                    continue
                if (await self.s.jobs.get(jid) is not None
                        or await self.s.jobs.get_by_name(job["name"]) is not None):
                    rep.skip(job["name"], "同名/同 id job 已存在")
                    continue
                if self.dry:
                    rep.skip(job["name"], "dry-run")
                    continue
                await self.s.jobs.upsert({**job, "id": jid})
                rep.migrated += 1
        return rep

    # ---- 5. 长期记忆：{user}/{Category}.md → memories 表 -------------------

    async def memories(self) -> Report:
        rep = Report("memories")
        root = self.runtime / "memory"
        for f in _iter_files(root, "*.md"):
            category = f.stem.lower()
            user = f.parent.name if f.parent != root else "default"
            rep.scanned += 1
            key = f"{user}/{category}"
            if category not in CATEGORIES:
                rep.skip(key, f"分类不在白名单 {sorted(CATEGORIES)}")
                continue
            content = f.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                rep.skip(key, "空文件")
                continue
            if await self.s.memories.read(user, category):
                rep.skip(key, "库里该分类已有内容（不覆盖）")
                continue
            if self.dry:
                rep.skip(key, "dry-run")
                continue
            if await self.s.users.get(user) is None:
                await self._add_user(user, token_hash(new_id("unclaimed", 24)))
            await self.s.memories.write(user, category, content)
            rep.migrated += 1
        return rep

    # ---- 6. 节点产物：FileStore 目录树 → artifacts 表 ----------------------

    async def artifacts(self) -> Report:
        rep = Report("artifacts")
        root = self.storyline / "cache" / "sessions"
        for f in _iter_files(root, "*.json"):
            rep.scanned += 1
            try:
                row = _read_json(f)
                session_id, artifact_id = str(row["session_id"]), str(row["artifact_id"])
                node = str(row["node"])
                repo = self.s.artifacts(session_id, artifact_id)
            except (ValueError, KeyError, TypeError) as exc:
                rep.skip(f.name, f"文件形态不对（{exc}）")
                continue
            key = f"{session_id}/{artifact_id}/{node}"
            if await repo.has(node):
                rep.skip(key, "已存在")
                continue
            if self.dry:
                rep.skip(key, "dry-run")
                continue
            await repo.put(node, row.get("payload"))
            rep.migrated += 1
        return rep

    # ---- 7. 素材：.storyline/uploads/{user}/{conv}/* → MinIO + materials --

    async def uploads(self) -> Report:
        rep = Report("materials")
        root = self.storyline / "uploads"
        for f in _iter_files(root):
            dirs = f.relative_to(root).parts[:-1]
            user = dirs[0] if dirs else "default"
            conv = dirs[1] if len(dirs) > 1 else "legacy"
            rep.scanned += 1
            sha = _sha256(f)
            rows = await self.s.db.select("materials", where={"owner_user_id": user,
                                                              "sha256": sha}, limit=1)
            if rows:
                rep.skip(f.name, f"同内容素材已在库（{rows[0]['id']}）")
                continue
            if f.stat().st_size > self.max_bytes:
                rep.skip(f.name, f"超过 {self.max_bytes // (1024 * 1024)}MB 上限")
                continue
            if self.dry:
                rep.skip(f.name, "dry-run")
                continue
            if await self.s.users.get(user) is None:
                await self._add_user(user, token_hash(new_id("unclaimed", 24)))
            try:
                await ingest_local_file(self.s, f, user_id=user, conversation_id=conv,
                                        origin="upload", max_bytes=self.max_bytes)
            except Exception as exc:                      # noqa: BLE001 - 单文件坏不毁整批
                rep.skip(f.name, f"入库失败（{exc}）")
                continue
            rep.migrated += 1
        return rep

    # ---- 8. 成片：.storyline/out/{session}/{artifact}.mp4 → 桶 + render_jobs

    async def renders(self) -> Report:
        rep = Report("render_jobs")
        root = self.storyline / "out"
        for f in _iter_files(root, "*.mp4"):
            parts = f.relative_to(root).parts[:-1]
            session = "/".join(parts) or "legacy"
            artifact = f.stem
            rep.scanned += 1
            key = f"renders/{session}/{artifact}.mp4"
            if await self.s.render_jobs.get(session, artifact) is not None:
                rep.skip(key, "同一 (会话, 产物) 已有渲染行")
                continue
            if self.dry:
                rep.skip(key, "dry-run")
                continue
            await self.s.objects.put(key, _chunks_from_file(f))
            try:
                duration = (await probe(f)).get("duration_sec")
            except Exception:                             # noqa: BLE001 - 探测失败留空
                duration = None
            await self.s.render_jobs.open(session, artifact)
            await self.s.render_jobs.succeed(session, artifact, key, duration)
            rep.migrated += 1
        return rep


def _report(reports: list[Report], *, dry: bool) -> None:
    print()
    print("迁移对账表" + ("（dry-run：只统计，未写入）" if dry else ""))
    print("-" * 66)
    for r in reports:
        print("  " + r.line())
        for s in r.skipped[:12]:
            print(f"      跳过 {s}")
        if len(r.skipped) > 12:
            print(f"      …另有 {len(r.skipped) - 12} 条跳过")
    print("-" * 66)
    print(f"  {'合计':<14} 迁入 {sum(r.migrated for r in reports):>4}")


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把本地磁盘遗留数据迁入 PG + MinIO（幂等）")
    ap.add_argument("--storage", default="pg_minio", choices=("pg_minio", "memory"),
                    help="目标后端（默认 pg_minio；memory 供测试与演练）")
    ap.add_argument("--runtime-dir", default=".runtime", help="遗留运行时目录")
    ap.add_argument("--storyline-dir", default=".storyline", help="遗留 storyline 目录")
    ap.add_argument("--cache-root", default=None,
                    help="本地内容缓存根（默认 .runtime/object_cache）")
    ap.add_argument("--workspace-root", default=None,
                    help="临时工作区根（默认 .runtime/workspace）")
    ap.add_argument("--dry-run", action="store_true", help="只扫只算，不写一行")
    ap.add_argument("--emit-credentials", default=None,
                    help="为遗留身份现发可登录 token 并追加写进该文件（值不落标准输出）；"
                         "不传则只补不可登录的占位哈希")
    ap.add_argument("--reissue-unclaimed", action="store_true",
                    help="配合 --emit-credentials：库里那些**只写了占位哈希**的遗留身份"
                         "（早先迁完、当时没发凭证）现在轮换成可登录 token。"
                         "已经写进那份凭证文件的 id 一律不动——换了哈希就等于把"
                         "本人手上那把当场作废")
    ap.add_argument("--max-upload-mb", type=int, default=1024,
                    help="单个遗留素材的字节上限（超限跳过而非截断）")
    args = ap.parse_args(argv)

    kwargs: dict[str, Any] = {}
    if args.cache_root:
        kwargs["cache_root"] = Path(args.cache_root)
    if args.workspace_root:
        kwargs["workspace_root"] = Path(args.workspace_root)
    storage = build_storage(args.storage, **kwargs)

    migrator = Migrator(
        storage,
        runtime_dir=Path(args.runtime_dir), storyline_dir=Path(args.storyline_dir),
        dry_run=args.dry_run,
        credentials_path=Path(args.emit_credentials) if args.emit_credentials else None,
        reissue_unclaimed=args.reissue_unclaimed,
        max_bytes=args.max_upload_mb * 1024 * 1024,
    )
    print(f"迁移目标：storage={args.storage} 源={args.runtime_dir} + {args.storyline_dir}"
          + ("  [dry-run]" if args.dry_run else ""))
    try:
        reports = await migrator.run()
    finally:
        await storage.close()
    _report(reports, dry=args.dry_run)
    if migrator.issued:
        print(f"  已为 {len(migrator.issued)} 个遗留身份签发凭证 → {args.emit_credentials}"
              f"（token 值不落标准输出，库里只有 sha256）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
