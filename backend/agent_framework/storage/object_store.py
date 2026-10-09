"""对象存储抽象层：媒体字节与技能附件的唯一去处。

  - MinioObjectStore：线上用，官方 minio SDK（同步）经 asyncio.to_thread 下线程——
    与 storyline_server「阻塞工作一律下线程」的约定一致，绝不卡事件循环。
  - MemoryObjectStore：进程内替身，字节存字典，`localize` 仍写出真实文件，
    所以离线测试能拿它喂 ffmpeg/ffprobe。

两个引擎共享 `ContentCache`：本地目录只是**可丢弃缓存**，按 sha256 内容寻址，
超出容量按最后使用时间逐出。对象键一律由调用方按 spec §4 的布局拼好传入，
本模块不参与业务命名。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

from .db import StorageUnavailable
from .media import mime_of          # media 只依赖标准库，不成环

SHA_META = "sha256"                    # 写入 x-amz-meta-sha256，取回时免再算一遍
_CHUNK = 1 << 20                       # 1 MiB 流式块


def _meta_sha256(meta: Mapping[str, Any]) -> str:
    """从对象元数据里取内容指纹。

    写入时给 SDK 的是裸键 'sha256'，回读时它已是完整头名 x-amz-meta-sha256
    （大小写随网关变化），所以按后缀匹配而不是查固定键。
    """
    for k, v in (meta or {}).items():
        if str(k).lower().endswith(SHA_META):
            return str(v or "")
    return ""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    bytes: int
    sha256: str
    content_type: str = ""
    last_modified: float = 0.0


class ObjectStore(ABC):
    """七方法接口。put/get_stream/head/delete/list_prefix/presign_get/localize。"""

    bucket: str

    @abstractmethod
    async def put(self, key: str, chunks: AsyncIterator[bytes], *,
                  content_type: str = "", size: int | None = None) -> ObjectInfo: ...

    @abstractmethod
    async def get_stream(self, key: str, *, start: int = 0,
                         end: int | None = None) -> AsyncIterator[bytes]: ...

    @abstractmethod
    async def head(self, key: str) -> ObjectInfo | None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        """按前缀列出对象（键升序）：字节数必填，sha256 引擎拿得到就填。

        「哪些分片已经在桶里」必须问桶、不能问某个进程的内存——续传要能撑过
        刷新页面与换副本，状态就得只有一个出处。
        """

    @abstractmethod
    async def presign_get(self, key: str, ttl_sec: int = 3600) -> str: ...

    @abstractmethod
    async def localize(self, key: str, dst_dir: Path) -> Path: ...

    async def ensure_ready(self) -> None:
        """启动期校验（建桶等）。子类按需覆盖，默认无操作。"""

    async def close(self) -> None: ...


# --------------------------------------------------------------------------
# 本地内容缓存（两个引擎共用）
# --------------------------------------------------------------------------


class ContentCache:
    """sha256 寻址的本地缓存：objects/{hex[:2]}/{hex}，超容量按 mtime 逐出。

    本类是「objects/ 这一层」的**唯一**归属：调用方传进来的 cache_root 只是根，
    不应自带 objects 段。早先配置把根写成 `.../cache/objects` 又叠上这一层，
    落成 objects/objects/（README §6，已在配置侧修掉）。缓存按设计可丢弃，
    遗留的双层条目这里不做迁移——直接命中不到、由 localize 回源重算即可。

    用 mtime 而非 atime 记「最后使用时间」——Windows 上 atime 更新不可靠，
    命中时统一 os.utime() 打点，逐出时按 mtime 排序。
    """

    def __init__(self, root: str | Path, max_bytes: int = 20 * 1024 ** 3) -> None:
        self.root = Path(root)
        self.dir = self.root / "objects"
        self.max_bytes = max_bytes

    def path_for(self, sha: str) -> Path:
        return self.dir / sha[:2] / sha

    def touch(self, path: Path) -> None:
        try:
            os.utime(path, None)
        except OSError:
            pass

    async def store(self, tmp_path: Path, sha: str) -> Path:
        dst = self.path_for(sha)
        if dst.exists():
            tmp_path.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 下载中的临时文件在 /tmp，缓存卷可能压根不是同一个文件系统（容器里
            # / 是 overlay、.runtime 是挂载卷）→ 直接 rename 必然 EXDEV。
            # 先挪进目标目录里的同名 .part 再 replace：这一步永远同卷，且不留半成品
            # 被后来的命中当成完整文件。
            staged = dst.with_name(dst.name + ".part")
            try:
                os.replace(tmp_path, staged)
            except OSError:
                await asyncio.to_thread(shutil.copyfile, tmp_path, staged)
                tmp_path.unlink(missing_ok=True)
            os.replace(staged, dst)
        self.touch(dst)
        await asyncio.to_thread(self.evict)
        return dst

    async def fetch_to(self, sha: str, dst_dir: Path, name: str) -> Path | None:
        src = self.path_for(sha)
        if not src.is_file():
            return None
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / name
        await asyncio.to_thread(shutil.copyfile, src, dst)
        self.touch(src)
        return dst

    def evict(self) -> int:
        """按 mtime 从旧到新删，直到总量不超容量。返回逐出文件数。"""
        if not self.dir.is_dir():
            return 0
        entries = [(p.stat().st_mtime, p.stat().st_size, p)
                   for p in self.dir.rglob("*") if p.is_file()]
        total = sum(sz for _, sz, _ in entries)
        if total <= self.max_bytes:
            return 0
        dropped = 0
        for _, sz, p in sorted(entries):
            p.unlink(missing_ok=True)
            dropped += 1
            total -= sz
            if total <= self.max_bytes:
                break
        return dropped


def _strip_scheme(endpoint: str) -> str:
    """SDK 的 endpoint 只吃 host:port，配置里常带 scheme —— 剥掉。"""
    return endpoint.removeprefix("https://").removeprefix("http://")


def _safe_basename(key: str) -> str:
    """对象键 → 本地文件名：只取最后一段，丢弃所有路径分隔，防穿越。"""
    name = key.replace("\\", "/").rstrip("/").split("/")[-1] or "object.bin"
    return os.path.basename(name).replace("\x00", "")


async def _collect_to_tempfile(chunks: AsyncIterator[bytes]) -> tuple[Path, int, str]:
    """边收流边算 sha256，落临时文件（内存有界，不等整文件到齐）。"""
    h = hashlib.sha256()
    total = 0
    fd, tmp = tempfile.mkstemp(prefix="objput_")
    path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                fh.write(chunk)
                h.update(chunk)
                total += len(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, total, h.hexdigest()


# --------------------------------------------------------------------------
# 内存替身
# --------------------------------------------------------------------------


class MemoryObjectStore(ObjectStore):
    """测试替身：字节在字典里，localize 仍落真实文件供 ffmpeg 使用。"""

    def __init__(self, cache_root: str | Path = Path(".runtime/object_cache"),
                 bucket: str = "creation-assets", *, presign_base: str = "memory://",
                 cache_max_bytes: int = 64 * 1024 ** 2) -> None:
        self.bucket = bucket
        self.presign_base = presign_base
        self.data: dict[str, tuple[bytes, str, str]] = {}      # key -> (bytes, content_type, sha)
        self.cache = ContentCache(cache_root, max_bytes=cache_max_bytes)
        self.calls: list[str] = []                              # 契约测试用
        self.closed = False

    def _log(self, op: str) -> None:
        if self.closed:
            raise StorageUnavailable("MemoryObjectStore 已 close()")
        self.calls.append(op)

    async def put(self, key: str, chunks: AsyncIterator[bytes], *,
                  content_type: str = "", size: int | None = None) -> ObjectInfo:
        self._log("put")
        buf = bytearray()
        h = hashlib.sha256()
        async for chunk in chunks:
            buf += chunk
            h.update(chunk)
        self.data[key] = (bytes(buf), content_type, h.hexdigest())
        return ObjectInfo(key=key, bytes=len(buf), sha256=h.hexdigest(),
                          content_type=content_type, last_modified=0.0)

    async def get_stream(self, key: str, *, start: int = 0,
                         end: int | None = None) -> AsyncIterator[bytes]:
        self._log("get_stream")
        got = self.data.get(key)
        if got is None:
            raise StorageUnavailable(f"对象不存在：{key}")
        blob = got[0][start:] if end is None else got[0][start:end + 1]
        for i in range(0, max(len(blob), 1), _CHUNK):
            yield blob[i:i + _CHUNK]

    async def head(self, key: str) -> ObjectInfo | None:
        self._log("head")
        got = self.data.get(key)
        if got is None:
            return None
        return ObjectInfo(key=key, bytes=len(got[0]), sha256=got[2], content_type=got[1])

    async def delete(self, key: str) -> None:
        self._log("delete")
        self.data.pop(key, None)

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        self._log("list_prefix")
        return [ObjectInfo(key=k, bytes=len(v[0]), sha256=v[2], content_type=v[1])
                for k, v in sorted(self.data.items()) if k.startswith(prefix)]

    async def presign_get(self, key: str, ttl_sec: int = 3600) -> str:
        self._log("presign_get")
        if key not in self.data:
            raise StorageUnavailable(f"对象不存在：{key}")
        return f"{self.presign_base}{self.bucket}/{key}?ttl={ttl_sec}"

    async def localize(self, key: str, dst_dir: Path) -> Path:
        self._log("localize")
        got = self.data.get(key)
        if got is None:
            raise StorageUnavailable(f"对象不存在：{key}")
        name = _safe_basename(key)
        cached = await self.cache.fetch_to(got[2], dst_dir, name)
        if cached:
            return cached
        fd, tmp = tempfile.mkstemp(prefix="objmem_")
        with os.fdopen(fd, "wb") as fh:
            fh.write(got[0])
        await self.cache.store(Path(tmp), got[2])
        out = await self.cache.fetch_to(got[2], dst_dir, name)
        assert out is not None
        return out

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# MinIO 引擎
# --------------------------------------------------------------------------


class MinioObjectStore(ObjectStore):
    """真线上引擎。

    minio SDK 是同步的：每个调用点包 asyncio.to_thread，事件循环绝不被阻塞
    （渲染期一次 put 是几百 MB 的读写，卡住就会冻住全部会话的流式输出）。
    SDK 延迟到 __init__ 内 import：未安装时 import 本模块不报错。
    """

    # MinIO I/O 超时（秒）：SDK 自带 urllib3 不设 read timeout，
    # 连接 stalled 时 resp.stream() 在线程里永远阻塞——这里兜一层。
    _HEAD_TIMEOUT = 30.0
    _DOWNLOAD_TIMEOUT = 300.0

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str,
                 *, cache_root: str | Path = Path(".runtime/object_cache"),
                 secure: bool = False, region: str | None = None,
                 cache_max_bytes: int = 20 * 1024 ** 3,
                 public_endpoint: str | None = None,
                 public_secure: bool | None = None) -> None:
        if not (endpoint and access_key and secret_key):
            raise ValueError("MinioObjectStore 需 endpoint/access_key/secret_key"
                             "（全部来自环境变量，不落盘不打印）")
        from minio import Minio                     # 延迟导入，同 KafkaMessageQueue
        from minio.error import S3Error             # noqa: F401  （异常翻译用）

        self.bucket = bucket
        self.secure = secure
        self._S3Error = S3Error
        self._client = Minio(_strip_scheme(endpoint),
                             access_key=access_key, secret_key=secret_key,
                             secure=secure, region=region)
        # public_endpoint：给**浏览器**签直链用的主机名。容器内的 endpoint 是 Docker
        # 网络里的服务名，浏览器解析不到；而 presigned URL 的签名含 Host
        # （SignedHeaders=host），事后替换主机名会得到 403——只能按浏览器那边的
        # 主机名重新签。签名是纯本地计算，这个地址从容器内可达与否都不影响。
        # region 必须显式给：否则 SDK 会为了查桶区域去连这个公网地址。
        self._presign_client = self._client
        if public_endpoint:
            self._presign_client = Minio(
                _strip_scheme(public_endpoint), access_key=access_key,
                secret_key=secret_key,
                secure=(public_secure if public_secure is not None
                        else public_endpoint.lower().startswith("https")),
                region=region or "us-east-1")
        self.cache = ContentCache(cache_root, max_bytes=cache_max_bytes)

    # ---- SDK 包装 ----

    def _run(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except self._S3Error as e:
            raise StorageUnavailable(f"MinIO 调用失败：{e._method or ''} {e}") from e

    async def ensure_ready(self) -> None:
        """启动期校验：端点可达 + 桶存在（不存在则建）。失败即抛，不带病服务。"""
        def _ensure() -> None:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        try:
            await asyncio.to_thread(_ensure)
        except StorageUnavailable:
            raise
        except OSError as e:
            raise StorageUnavailable(f"MinIO 端点不可达：{e}") from e

    async def put(self, key: str, chunks: AsyncIterator[bytes], *,
                  content_type: str = "", size: int | None = None) -> ObjectInfo:
        path, total, sha = await _collect_to_tempfile(chunks)
        try:
            def _upload() -> None:
                with path.open("rb") as fh:
                    self._client.put_object(
                        self.bucket, key, fh, length=size if size is not None else total,
                        content_type=content_type or "application/octet-stream",
                        part_size=min(8 * 1024 * 1024, max(5 * 1024 * 1024, total or 1)),
                        metadata={SHA_META: sha})
            await asyncio.to_thread(_upload)
        finally:
            path.unlink(missing_ok=True)
        return ObjectInfo(key=key, bytes=total, sha256=sha, content_type=content_type)

    async def head(self, key: str) -> ObjectInfo | None:
        def _stat() -> ObjectInfo | None:
            try:
                st = self._client.stat_object(self.bucket, key)
            except self._S3Error as e:
                if st_not_found(e):
                    return None
                raise StorageUnavailable(f"MinIO stat 失败：{e}") from e
            meta = st.metadata or {}
            return ObjectInfo(key=key, bytes=int(st.size or 0),
                              sha256=_meta_sha256(meta),
                              content_type=st.content_type or "",
                              last_modified=st.last_modified.timestamp() if st.last_modified else 0.0)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_stat), timeout=self._HEAD_TIMEOUT)
        except TimeoutError:
            raise StorageUnavailable(
                f"MinIO stat 超时（{self._HEAD_TIMEOUT:.0f}s）：{key}") from None

    async def _download(self, key: str, *, start: int = 0,
                        end: int | None = None) -> tuple[bytes, str]:
        h = hashlib.sha256()
        buf = bytearray()

        def _get() -> None:
            try:
                resp = self._client.get_object(self.bucket, key, offset=start or None,
                                               length=None if end is None else end - start + 1)
            except self._S3Error as e:
                raise StorageUnavailable(f"MinIO 取回失败 {key}：{e}") from e
            try:
                for chunk in resp.stream(_CHUNK):
                    buf.extend(chunk)
                    h.update(chunk)
            finally:
                resp.close()
                resp.release_conn()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_get), timeout=self._DOWNLOAD_TIMEOUT)
        except TimeoutError:
            raise StorageUnavailable(
                f"MinIO 下载超时（{self._DOWNLOAD_TIMEOUT:.0f}s）：{key}") from None
        return bytes(buf), h.hexdigest()

    async def get_stream(self, key: str, *, start: int = 0,
                         end: int | None = None) -> AsyncIterator[bytes]:
        """整段取回后分块下发：MinIO SDK 同步流无法安全地跨 await 持有连接。"""
        blob, _ = await self._download(key, start=start, end=end)
        for i in range(0, max(len(blob), 1), _CHUNK):
            yield blob[i:i + _CHUNK]

    async def presign_get(self, key: str, ttl_sec: int = 3600) -> str:
        def _p() -> str:
            from datetime import timedelta

            return self._presign_client.presigned_get_object(
                self.bucket, key, expires=timedelta(seconds=ttl_sec))
        return await asyncio.to_thread(_p)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._run, self._client.remove_object, self.bucket, key)

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        """一次列举问回整段前缀：include_user_meta 让写入时的 x-amz-meta-sha256 随
        列表回来（MinIO 扩展），免得为「已有哪几片」逐片 stat。拿不到指纹就留空——
        调用方按字节数续传仍然成立，只是少一道校验。"""
        def _ls() -> list[ObjectInfo]:
            try:
                got = list(self._client.list_objects(self.bucket, prefix=prefix,
                                                     recursive=True,
                                                     include_user_meta=True))
            except self._S3Error as e:
                raise StorageUnavailable(f"MinIO 列举失败 {prefix}：{e}") from e
            out = [ObjectInfo(key=o.object_name, bytes=int(o.size or 0),
                              sha256=_meta_sha256(o.metadata or {}),
                              content_type=o.content_type or "",
                              last_modified=o.last_modified.timestamp() if o.last_modified else 0.0)
                   for o in got if o.object_name]
            out.sort(key=lambda i: i.key)
            return out
        return await asyncio.to_thread(_ls)

    async def _download_retry(self, key: str, attempts: int = 3) -> tuple[bytes, str]:
        """localize 的取回重试（spec §9）：瞬时故障 2 次、指数退避；put 不重试。"""
        last: Exception | None = None
        for i in range(attempts):
            if i:
                await asyncio.sleep(0.2 * (2 ** (i - 1)))
            try:
                return await self._download(key)
            except StorageUnavailable as e:
                last = e
        raise last                      # 三次都失败：原样抛出，不降级到本地磁盘

    async def localize(self, key: str, dst_dir: Path) -> Path:
        name = _safe_basename(key)
        info = await self.head(key)
        if info is None:
            raise StorageUnavailable(f"对象不存在：{key}")
        sha = info.sha256
        if sha:
            hit = await self.cache.fetch_to(sha, dst_dir, name)
            if hit:
                return hit
        blob, computed = await self._download_retry(key)
        fd, tmp = tempfile.mkstemp(prefix="objdl_")
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        await self.cache.store(Path(tmp), computed)
        out = await self.cache.fetch_to(computed, dst_dir, name)
        assert out is not None
        return out

    async def close(self) -> None:
        return None


def st_not_found(err: Any) -> bool:
    """把「对象不存在」与真故障分开：不同 minio 版本的异常字段名不一致。"""
    code = getattr(err, "_code", None) or getattr(err, "code", None)
    status = getattr(err, "status", None) or getattr(err, "http_status", None)
    return code in ("NoSuchKey", "NoSuchBucket") or status == 404


def scoped_dir(root: str | Path, *segs: str) -> Path:
    """工作区根下的某一层目录：逐段清洗非法字符、建出来再返回。

    只依赖根路径就能定位「谁的哪一层」，所以不需要对象存储的工具（如文件工具）
    也能复用同一套会话目录规则，不会出现两套越界口径。
    """
    p = Path(root)
    for s in segs:
        p = p / _seg(s)
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------
# 持久化产物里的文件引用
# --------------------------------------------------------------------------
#
# artifacts.payload 与 checkpoint_entries.payload 活得比工作区长：它们要跨进程重启、
# 跨副本、跨 sweep_stale 回收被读回来。所以里面**不能出现工作区绝对路径**——那种
# 路径换个实例、或目录被回收之后就是空气，而且把本机目录结构写进了共享库。
# 落进 payload 的只有 `obj:{对象键}`，谁要用字节谁现取（Workspace.localize_ref）。

REF_PREFIX = "obj:"


def to_ref(object_key: str) -> str:
    return f"{REF_PREFIX}{object_key}"


def ref_key(value: Any) -> str | None:
    """引用 → 对象键；不是引用（遗留绝对路径等）返回 None。"""
    s = str(value or "")
    return s[len(REF_PREFIX):] if s.startswith(REF_PREFIX) else None


@dataclass
class Workspace:
    """渲染工作区：对象键 ↔ 本地临时文件的唯一出入口，节点结束即整目录回收。"""

    root: Path
    objects: ObjectStore
    # localize_ref 的回源锁（按目标文件定名）：镜头并行时同一份素材会被同时索取
    _ref_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def dir_for(self, session_id: str, artifact_id: str, *sub: str) -> Path:
        return scoped_dir(self.root, session_id, artifact_id or "_default", *sub)

    async def localize(self, key: str, dst_dir: Path) -> Path:
        return await self.objects.localize(key, dst_dir)

    async def localize_material(self, material: dict[str, Any], dst_dir: Path) -> Path:
        return await self.objects.localize(material["object_key"], dst_dir)

    async def localize_ref(self, value: Any, dst_dir: str | Path) -> Path:
        """产物里的文件引用 → 本地可读文件（要字节的人唯一的取字节入口）。

        slot 目录按对象键定名，所以**同一引用在同一目录里只落一次**：20 个镜头共用
        一份素材字节，而不是拷 20 遍；不同对象键即使同名（两个产物各有一个
        ``placeholder_bgm.wav``）也各占各的 slot，不会互相盖。
        """
        key = ref_key(value)
        if key is None:
            p = Path(str(value))
            if p.is_file():
                return p    # 遗留 payload：改写之前落下的本机绝对路径，本机还在就接着用
            raise StorageUnavailable(
                f"产物引用的字节不在本机：{value}——工作区是临时的，请重跑产出它的节点")
        slot = Path(dst_dir) / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        named = slot / _safe_basename(key)
        if named.is_file():
            return named
        # 同一份字节的并发回源只发一次：镜头并行的节点里 20 个任务会撞同一个文件
        async with self._ref_locks.setdefault(str(named), asyncio.Lock()):
            if not named.is_file():
                await self.objects.localize(key, slot)
        return named

    def derived_key(self, session_id: str, artifact_id: str, stage: str, name: str) -> str:
        """节点自产文件的对象键：``derived/{会话}/{产物}/{环节}/{文件名}``。

        与 ``renders/`` 同一个道理——键里带作用域，同产物重跑覆盖同一条，不产生孤儿。
        """
        return "/".join(("derived", _seg(session_id), _seg(artifact_id or "_default"),
                         _seg(stage), _safe_basename(name)))

    async def publish_derived(self, path: str | Path, *, session_id: str,
                              artifact_id: str, stage: str) -> str:
        """本地自产文件 → 对象存储，返回能写进 payload 的引用。

        配音/转场/占位 BGM 这类文件一旦进了持久化产物，就必须比工作区长：工作区按
        设计可丢弃，留下绝对路径等于把一条注定断的引用写进共享库。
        """
        p = Path(path)
        key = self.derived_key(session_id, artifact_id, stage, p.name)
        await self.publish(p, key, content_type=mime_of(p.name))
        return to_ref(key)

    async def publish(self, path: str | Path, key: str, *,
                      content_type: str = "video/mp4") -> ObjectInfo:
        """本地成片 → 对象存储，返回 ObjectInfo（调用方随后写 render_jobs）。"""
        p = Path(path)

        def _chunks() -> AsyncIterator[bytes]:
            async def gen() -> AsyncIterator[bytes]:
                with p.open("rb") as fh:
                    while True:
                        b = await asyncio.to_thread(fh.read, _CHUNK)
                        if not b:
                            break
                        yield b
            return gen()

        return await self.objects.put(key, _chunks(), content_type=content_type,
                                      size=p.stat().st_size)

    def cleanup(self, session_id: str, artifact_id: str, *sub: str) -> int:
        """删掉某次产物（或其中一个子目录）的中间文件；返回删除的文件数。

        给了 sub 段就只清那一层：渲染失败只该收回 render/。material/ 与 voiceover/
        留着不是为了守住谁的引用（产物里存的是对象引用，字节随时能重取），而是省下同
        一会话重跑时的重复下载与重复合成。
        """
        d = self.root / _seg(session_id) / _seg(artifact_id or "_default")
        for s in sub:
            d = d / _seg(s)
        if not d.exists():
            return 0
        n = sum(1 for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        if not sub:
            try:
                d.parent.rmdir()             # 会话目录空了就一并删掉
            except OSError:
                pass
        return n

    def sweep_stale(self, max_age_sec: float) -> int:
        """回收崩溃/异常退出留下的过期工作区目录（按目录 mtime）。"""
        if not self.root.is_dir():
            return 0
        now = _now_ms() / 1000
        n = 0
        for sess in self.root.iterdir():
            if not sess.is_dir():
                continue
            for art in sess.iterdir():
                if art.is_dir() and now - art.stat().st_mtime > max_age_sec:
                    n += sum(1 for f in art.rglob("*") if f.is_file())
                    shutil.rmtree(art, ignore_errors=True)
            try:
                sess.rmdir()                     # 会话目录空了就一并删掉（与 cleanup 对齐）
            except OSError:
                pass
        return n


_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _seg(v: str) -> str:
    return _ILLEGAL_FS.sub("_", v or "_")


def _now_ms() -> int:
    return int(time.time() * 1000)
