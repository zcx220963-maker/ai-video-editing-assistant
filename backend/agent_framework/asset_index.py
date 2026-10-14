"""用户带来的图 → 「按 sha 找字节」那张索引，**唯一实现**（R13-T65 / 缺口 A）。

**它解决的是什么。** 渲染侧只认一张表：`shots[].assets[].sha256` → `asset_library.get_by_sha`
→ `object_key` → 取字节投给渲染容器（`code_render._stage_shot_assets`）。而用户上传走的是
`materials` 表（归 owner、可删、能在素材册里看见）——**两张表之间缺一条腿**：传完
`get_by_sha` 查不到，于是表现成「传了图也用不上，渲染前必然报错找不到这张图」。
本模块就是那条腿，且只做这一件事：**保证索引里有这一行**。

**为什么写 `enabled=False`。** `AssetLibraryRepo.search()` 只返回 `enabled=True` 的行，
而 `search_illustration` 正好读它。用户自己的图若以默认的 `enabled=True` 入库，
**别人**在自己会话里检索时会搜到它。关掉之后：按 sha 取字节照旧取得到
（`get_by_sha` **不看**这一列），检索面上看不见。这与 imagegen 产物是同一条隐私理由。

**为什么 `object_key` 复用 materials 那个对象。** 同一段字节在桶里存两份没有任何好处；
而 Q155 明写「删除 `materials` 行时索引行与对象**都保留**」——索引行指着 materials 的对象
键，正好把这条规矩变成自然结果（`MaterialsRepo.drop` 只删表行、不动桶）。

**幂等**：同一 sha 上已有一行就复用它。`asset_library.sha256` 上还有一条 UNIQUE，
DB 那一层也拦得住——**同一张图传两次不得造第二行**。

**失败只回空串，绝不抛**：`materials` 那一行已经写成了，用户在自己素材册里看得到这张图；
索引少一行只影响「它能不能当镜头素材上屏」。这是**两级保证**，不该用同一句话报
（与 `imagegen_library` 那条注释同一口径）。

**`source` 取值**：`user-upload`。上传（`origin='upload'`）与链接取料（`origin='url'`）
都归这一档——两者都是**用户自己带进来的**，而闸㉛ 要的正是「这一件必须由本次 run 的
`attachments` 指名」这一个判据；upload / url 的区别留在 `materials.origin` 里，不在这儿
分叉（分叉会让闸㉛ 长出第二个判据，那是清单漂移的老路）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .storage import IntegrityConflict, StorageUnavailable

#: 用户带来的图的来源档。与 imagegen 产物的 `imagegen`、外部源的 `wikimedia-commons` /
#: `iconify` 并列，四者互不相等。
SOURCE_USER_UPLOAD = "user-upload"

#: 许可档。写空串会让「每行必须带 license」那条硬约束形同虚设；
#: 准确描述是「用户自己带进来的，权利由上传者声明」——**不是** CC0。
LICENSE_USER_UPLOAD = "user-uploaded"

#: 只有位图能上屏（`shots[].assets` 那三键是给渲染容器落盘用的）。
#: 视频/音频走别的路（`clips` / `bgm.ref`），不进这张索引。
_INDEX_KINDS = ("image",)


def _ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def _sha16(sha256: str) -> str:
    return sha256[:16]


async def ensure_index_row(
    storage: Any, *,
    sha256: str,
    ext: str,
    mime: str = "",
    name: str = "",
    object_key: str = "",
    data: bytes | None = None,
    bytes_: int | None = None,
    width: int | None = None,
    height: int | None = None,
    source: str = SOURCE_USER_UPLOAD,
    license: str = LICENSE_USER_UPLOAD,
    caption: str = "",
    enabled: bool = False,
) -> str:
    """保证「按 sha 找字节」的索引里有这一行，回它的 id；做不到就回空串（**不抛**）。

    字节来源二选一，且必须给一个：

    * ``object_key`` —— 字节**已经在桶里**（上传件就是这样：materials 那一步已经落好了）。
      这一条路不复制字节，只登记一行。
    * ``data`` —— 字节在手上（imagegen 产物就是这样）。会写到
      ``assets/<source>/<sha2>/<sha>.<ext>`` 再登记。

    两个都不给 = 没有字节可索引，回空串并**不改判**任何事。
    """
    sha256 = str(sha256 or "").strip().lower()
    ext = str(ext or "").strip().lstrip(".").lower()
    if len(sha256) != 64 or not ext:
        return ""
    if not object_key and data is None:
        return ""

    try:
        row = await storage.assets.get_by_sha(sha256)
        if row:
            return str(row["id"])          # 同一段字节只有一行（sha256 UNIQUE 也拦得住）
    except Exception:                      # noqa: BLE001 - 查不到与「库里没有」分开处理
        pass

    key = object_key
    size = bytes_
    if not key:
        payload = bytes(data or b"")
        key = f"assets/{source}/{sha256[:2]}/{sha256}.{ext}"
        size = len(payload)
        try:
            await storage.objects.put(key, _one_chunk(payload),
                                      content_type=mime or "")
        except StorageUnavailable:
            return ""

    try:
        got = await storage.assets.add(
            sha256=sha256, bytes_=int(size or 0), mime=mime or "", ext=ext,
            object_key=key, source=source, license=license,
            caption=(caption or name or "")[:200],
            src_id=(name or f"{source}-{_sha16(sha256)}"),
            width=width, height=height, enabled=enabled)
        return str(got["id"])
    except IntegrityConflict:
        # 竞态：两个请求同时走到这里，另一个已经插进去了。**这不是失败**——
        # 目标状态（索引里有这一行）已经成立，回那一行。
        try:
            again = await storage.assets.get_by_sha(sha256)
            return str(again["id"]) if again else ""
        except Exception:                  # noqa: BLE001
            return ""
    except Exception:                      # noqa: BLE001 - 索引腿失败不改判「素材已入库」
        return ""


async def index_material(storage: Any, row: Mapping[str, Any], *,
                         object_key: str = "") -> str:
    """一条 `materials` 行 → 索引行（非位图直接回空串，**这不是失败**）。

    `materials.register` 的返回值直接喂进来即可。为什么把「从 materials 行里挑哪几个字段」
    收在这一个函数里：那是**这一条腿的口径**，散在调用点上就会出现「这里传 filename、
    那里传 name」的漂移——而漂移的表现是闸⑬ 的「三处 sha16 不一致」，最难查的那一类。
    """
    kind = str(row.get("kind") or "")
    if kind not in _INDEX_KINDS:
        return ""
    fname = str(row.get("filename") or row.get("name") or "")
    key = object_key or str(row.get("object_key") or "")
    return await ensure_index_row(
        storage,
        sha256=str(row.get("sha256") or ""),
        ext=_ext_of(fname),
        mime=str(row.get("mime") or ""),
        name=fname,
        object_key=key,
        bytes_=row.get("bytes"),
        width=row.get("width"),
        height=row.get("height"),
        caption=str(row.get("caption") or fname or ""))


async def _one_chunk(data: bytes):
    yield data
