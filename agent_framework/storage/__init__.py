"""上线存储层：PostgreSQL 元数据 + MinIO 媒体字节。

`build_storage(backend)` 是唯一分派点，形状照 `agent_framework/mq.py` 的
`build_message_queue("memory"|"kafka")`：重依赖在分支内延迟 import，未装 `minio` /
`asyncpg` 时 import 本包不报错；后端名未知即抛 ValueError。

后端只有两个：
- `pg_minio`：生产。五项配置（PG_DSN / MINIO_ENDPOINT / MINIO_ACCESS_KEY /
  MINIO_SECRET_KEY / MINIO_BUCKET）**只从环境变量读**，不落盘、不打印、不写日志。
- `memory`：测试注入用的内存替身（D3）。不校验环境变量，语义与前者逐条对齐，
  由 `test_storage_contract.py` 用同一套用例跑两遍钉住。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import (
    Cond,
    Datastore,
    IntegrityConflict,
    MemoryDatastore,
    StorageUnavailable,
    eq,
    ge,
    gt,
    in_,
    is_null,
    le,
    lt,
    ne,
    not_null,
)
from .object_store import (
    ObjectInfo,
    ObjectStore,
    MemoryObjectStore,
    MinioObjectStore,
    Workspace,
    ref_key,
    to_ref,
)
from .repositories import (
    ArtifactsRepo,
    CheckpointsRepo,
    ConversationsRepo,
    InboxRepo,
    JobsRepo,
    MaterialsRepo,
    MemoriesRepo,
    MessagesRepo,
    RenderJobsRepo,
    SecretsRepo,
    TimelinesRepo,
    SkillsRepo,
    SubagentsRepo,
    TasksRepo,
    UploadSessionsRepo,
    UsersRepo,
    mask,
    new_id,
    rank_by_request,
    token_hash,
)

__all__ = [
    "Storage", "build_storage", "ObjectInfo", "ObjectStore", "Datastore",
    "StorageUnavailable", "IntegrityConflict", "Cond", "eq", "ne", "lt", "le",
    "gt", "ge", "in_", "is_null", "not_null", "Workspace", "new_id", "token_hash",
    "to_ref", "ref_key",
    "rank_by_request", "INTERNAL_IDENTITIES", "SecretsRepo", "TimelinesRepo", "mask",
]

ENV_KEYS = ("PG_DSN", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
            "MINIO_BUCKET")
DEFAULT_BUCKET = "creation-assets"
# 两个本地目录都可随时删光（spec §6）：内容缓存 localize 的落点、会话工作区的临时产物
DEFAULT_CACHE_ROOT = Path(".runtime/object_cache")
DEFAULT_WORKSPACE_ROOT = Path(".runtime/workspace")
DEFAULT_CACHE_MAX_GB = 20.0
# 进程内部身份：不来自 /register，但 conversations/materials/memories 的外键要它们先存在。
# cron = 定时任务投递的执行身份；default = 未进入任何 run 时的缺省作用域（离线直调、demo）。
INTERNAL_IDENTITIES = ("default", "cron")


def _cache_max_bytes(gb: Any) -> int:
    return int(float(gb) * 1024 ** 3)


def _age_sec(ts: Any) -> float | None:
    """行的 updated_at 距今多少秒：轮询端据此看出「进度卡了多久」。

    无时区标注按 UTC 处理（内存替身写的是 naive datetime），拿不到时间就回 None，
    不猜一个 0——0 会被读成「刚刚推进过」。
    """
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - ts).total_seconds(), 1)


class Storage:
    """一次进程装配的存储句柄：两个引擎 + 渲染工作区 + 全部表级仓储。"""

    def __init__(self, backend: str, db: Datastore, objects: ObjectStore,
                 workspace: Workspace) -> None:
        self.backend = backend
        self.db = db
        self.objects = objects
        self.workspace = workspace
        self.users = UsersRepo(db)
        self.conversations = ConversationsRepo(db)
        self.messages = MessagesRepo(db)
        self.materials = MaterialsRepo(db)
        self.upload_sessions = UploadSessionsRepo(db)
        self.render_jobs = RenderJobsRepo(db)
        self.checkpoints = CheckpointsRepo(db)
        self.inbox = InboxRepo(db)
        self.tasks = TasksRepo(db)
        self.subagents = SubagentsRepo(db)
        self.jobs = JobsRepo(db)
        self.memories = MemoriesRepo(db)
        self.skills = SkillsRepo(db)
        self.secrets = SecretsRepo(db)
        self.timelines = TimelinesRepo(db)

    def artifacts(self, session_id: str, artifact_id: str = "") -> ArtifactsRepo:
        """FileStore 的落点：一次渲染一份，(session_id, artifact_id) 定作用域。"""
        return ArtifactsRepo(self.db, session_id, artifact_id)

    async def render_view(self, session_id: str, artifact_id: str,
                          *, ttl_sec: int = 3600) -> dict[str, Any] | None:
        """一次渲染的对外视图：进度行 +（done 时）终态产物与现签直链。

        「提交 + 轮询」有两个轮询口——Storyline 的 ``render_status`` 工具、主服务的
        ``GET /render_status``。两处都走本方法，形状只有一份。未提交过的作用域返回 None
        （区别于「排队中」：那是一行 status='queued'）。
        """
        row = await self.render_jobs.get(session_id, artifact_id or "_default")
        if row is None:
            return None
        view: dict[str, Any] = {
            "status": row["status"], "stage": row["stage"], "percent": row["percent"],
            "session_id": session_id,
            "artifact_id": row.get("artifact_id") or artifact_id or "_default",
            "seconds_since_update": _age_sec(row.get("updated_at")),
        }
        if row.get("error"):
            view["error"] = row["error"]
        key = row.get("video_object_key")
        if row["status"] == "done" and key:
            view.update(row.get("result") or {})
            view["video"] = key
            if view.get("duration") is None:
                view["duration"] = row.get("duration_sec")
            view["media_url"] = await self.objects.presign_get(key, ttl_sec=ttl_sec)
        elif row["status"] == "failed":
            # 失败也要把节点存下来的信息带给调用方：渲染失败时会回一组**回退方案**
            # （简化渲染/降分辨率/分段渲染），存进 result 才到得了模型。
            # 原先失败只回 error，模型拿不到选项，"失败了给用户选"这条设计走不到。
            saved = row.get("result") or {}
            for k, v in saved.items():
                if k not in view:
                    view[k] = v
        return view

    async def start(self) -> None:
        """启动即校验（spec §9）：连不上就抛，绝不带病服务、不退回本地磁盘。"""
        await self.db.ping()
        await self.db.ensure_schema()
        await self.objects.ensure_ready()
        # 两个服务（:8000 主服务 / :8001 Storyline）都走这里启动，所以密钥热读的
        # 存储句柄绑在这一行就够了——LLM 客户端不需要自己拿 storage。
        from ..secrets import bind_storage
        bind_storage(self)

    async def provision_internal(self) -> list[str]:
        """登记进程内部身份（幂等），启动时一次；业务写入路径此后不再自建用户行。"""
        for uid in INTERNAL_IDENTITIES:
            await self.users.provision(uid)
        return list(INTERNAL_IDENTITIES)

    async def close(self) -> None:
        await self.db.close()
        await self.objects.close()


def _load_env_file(path: Path = Path(".env")) -> None:
    """把 .env 里的 KEY=VALUE 落进环境变量（不覆盖已有值、不打印任何值）。

    项目不引 python-dotenv：只需 KEY=VALUE 一种形态，十余行自足以让
    「copy .env.example .env 后直接起服务」这条运行方式成立（spec §12）。
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _require_env() -> dict[str, str]:
    _load_env_file()
    missing = [k for k in ENV_KEYS if k != "MINIO_BUCKET" and not _env(k)]
    if missing:
        raise StorageUnavailable(
            f"pg_minio 后端缺环境变量 {', '.join(missing)}（只从环境变量读，"
            f"参见 .env.example）"
        )
    return {k: _env(k) for k in ENV_KEYS}


def build_storage(backend: str = "pg_minio", **kwargs: Any) -> Storage:
    """按后端名构造 Storage：'pg_minio' → 真线上，'memory' → 测试替身。

    kwargs 只有三个本地目录/容量项（cache_root / workspace_root / cache_max_gb），
    供调用方与测试注入；密钥与桶一律走环境变量，不接受把凭证当参数传进代码路径。
    """
    cache_root = Path(kwargs.pop("cache_root", DEFAULT_CACHE_ROOT))
    workspace_root = Path(kwargs.pop("workspace_root", DEFAULT_WORKSPACE_ROOT))
    max_bytes = _cache_max_bytes(kwargs.pop("cache_max_gb", DEFAULT_CACHE_MAX_GB))
    if backend == "memory":
        objects = MemoryObjectStore(
            cache_root,
            bucket=kwargs.pop("bucket", DEFAULT_BUCKET),
            presign_base=kwargs.pop("presign_base", "memory://"),
            cache_max_bytes=max_bytes,
        )
        return Storage("memory", MemoryDatastore(), objects,
                       Workspace(root=workspace_root, objects=objects))
    if backend == "pg_minio":
        cfg = _require_env()
        from .db import PgDatastore     # 延迟导入：未装 asyncpg 时 import 本包不报错

        objects = MinioObjectStore(
            cfg["MINIO_ENDPOINT"], cfg["MINIO_ACCESS_KEY"], cfg["MINIO_SECRET_KEY"],
            cfg["MINIO_BUCKET"] or DEFAULT_BUCKET,
            cache_root=cache_root,
            secure=_env("MINIO_SECURE", "0") in ("1", "true", "yes"),
            cache_max_bytes=max_bytes,
        )
        db = PgDatastore(cfg["PG_DSN"], pool_size=int(_env("PG_POOL_SIZE", "5")))
        return Storage(
            "pg_minio", db, objects,
            Workspace(root=workspace_root, objects=objects),
        )
    raise ValueError(f"未知 storage backend={backend!r}，仅支持 'pg_minio' / 'memory'")
