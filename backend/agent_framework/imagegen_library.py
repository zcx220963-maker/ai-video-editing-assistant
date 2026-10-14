"""R6-T24 出图产物入库：一次出图的**字节**怎么变成链上可见的一份素材。

形制照 ``render_library.register_render`` 与 ``library_pick.pick_library_asset``：
回 ``(material_id, asset_id, filename, 说明)``，说明非空即这次没入成，由调用方并进
``notes`` 对用户明说——**出图失败与入库失败是两件事**，前者是这次没图，后者是图烧出来
了但链上找不到它。混成一句话的话，排障的人分不清该去查后端还是查存储。

``filename`` 也一并回：它就是镜头代码里 ``staticFile("<name>")`` 要引用的那个名字，
调用方不该自己再起一遍——**两个名字必须同一个**，否则「这张图在哪」会有两个答案。

为什么一次写**两张表**（这不是「造两套真相」，两张表回答的是两个不同的问题）：

- ``materials(origin='art')`` —— 「**谁的**什么样的图」。§5.2 把 ``'art'`` 定为出图产物
  专用档；这张表带 ``owner_user_id``，是用户在自己素材册里能看到、能复用、能删的那一份。
- ``asset_library(source='imagegen', enabled=False)`` —— 「**这段字节**在哪」。镜头侧
  ``assets: [{name, sha256, ext}]`` 的投递腿（``code_render._stage_shot_assets``）解析的是
  ``asset_library.get_by_sha``：它是全仓唯一一处「按内容哈希找字节」的索引，且**无归属**
  （§5.2 明写它装不下 ``owner_user_id``）。出图产物要能上屏，就得在这张索引里有一行。

  ``enabled=False`` 是**故意的**：``AssetLibraryRepo.search`` 只返回 ``enabled=True``
  的行，而 ``search_illustration`` 正好读它——若出图产物以默认的 ``enabled=True`` 入库，
  **别人**在自己会话里检索时会搜到用户 A 刚生成的图。关掉之后：按 sha 取字节照旧能取到
  （``get_by_sha`` 不看这一列），检索面上看不见。策展库的检索面与「字节索引」面由此分开。

**幂等**：同一 ``(owner, conv, sha256)`` 上已有一行 ``origin='art'`` 就复用，不重复占字节；
库里那行按 id 是 ``ast-<sha12>``，同一张图再出一次也是同一行（upsert）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import asset_index
from .identity import parse_storyline_session
from .ingest import safe_segment
from .storage import IntegrityConflict, StorageUnavailable, new_id
from .storage.media import kind_of, mime_of

ORIGIN = "art"
SOURCE = "imagegen"
#: 出图产物的许可档。写一个空串会让 §5.2「每行必须带 license」那条硬约束形同虚设；
#: 「用户自己生成的」就是这一档的准确描述——**不是** CC0，也不该被检索当成可自由复用的底本。
LICENSE = "generated-by-user"

_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg",
                "jpeg": "image/jpeg", "webp": "image/webp"}


def _clean(text: str, limit: int) -> str:
    """去掉不能进文件名/``staticFile`` 的字符。留可打印的，其余删；空了回空串。"""
    return "".join(ch for ch in (text or "").strip()[:limit]
                   if ch not in '/\\:*?"<>|\n\r\t' and ch.isprintable()).strip()


def _file_name(given: str, prompt: str, sha16: str, ext: str) -> str:
    """落盘名。调用方给了就用它（补上扩展名），没给才按提示词起名。

    文件名会进 ``staticFile("<name>")``（渲染容器按名落盘）与素材册展示，所以不能带
    ``/`` ``\\`` 等字符，也不能为空。自动起名时取提示词前若干字 + sha16：既保证同名
    不同图不会互相覆盖，也让「这一镜哪来的图」在产物目录里一眼可读。
    """
    want = _clean(given, 120)
    if want:
        return want if want.lower().endswith(f".{ext.lower()}") else f"{want}.{ext}"
    return f"{_clean(prompt, 24) or 'imagegen'}-{sha16}.{ext}"


def _safe_name(prompt: str, sha16: str, ext: str) -> str:
    """``<提示词前若干字>-<sha16>``（不含扩展名）：给索引行的 ``src_id`` 用。"""
    return f"{_clean(prompt, 24) or 'imagegen'}-{sha16}"


async def store_generated(storage: Any, session_id: str, result: Any, *,
                          prompt: str = "",
                          filename: str = "",
                          conversation_id: str | None = None,
                          ) -> tuple[str, str, str, str]:
    """把一次出图的 ``result``（``image_backend.ImageResult``）落进库，回四元组。

    ``result`` 只要有 ``data`` / ``ext`` / ``sha256`` / ``width`` / ``height`` 即可，
    不 import 那个类——这样用例可以拿一份手搓的产物来测入库这条腿，不必真出网。

    ``filename`` 显式给定时就用它（调用方要拿这个名字去 ``staticFile(name)``）；
    不给才按提示词起名。**两个名字必须是同一个**：镜头代码引用的名字与素材册里那行
    若不是同一个，排障时「这张图在哪」就会有两个答案。
    """
    user_id, conv_id = parse_storyline_session(session_id)
    if conversation_id is not None:
        conv_id = conversation_id or None
    if not user_id:
        return "", "", "", f"会话键 {session_id!r} 拆不出用户，这张图没进素材库"

    data = bytes(getattr(result, "data", b"") or b"")
    ext = str(getattr(result, "ext", "") or "").lstrip(".").lower()
    if not data or not ext:
        return "", "", "", "出图产物为空（没有字节或没有扩展名），没有可入库的东西"
    sha256 = str(getattr(result, "sha256", "") or "") \
        or hashlib.sha256(data).hexdigest()
    sha16 = sha256[:16]
    mime = _MIME_BY_EXT.get(ext, "") or mime_of(f"x.{ext}")

    # 素材册里的那一行（用户级、可见、可删）。
    try:
        existing = await storage.materials.db.select(
            storage.materials.table,
            where={"owner_user_id": user_id, "conv_id": conv_id,
                   "sha256": sha256, "origin": ORIGIN},
            limit=1)
    except StorageUnavailable:
        existing = []
    if existing:
        mid = str(existing[0]["id"])
        asset_id = await _ensure_index(storage, data=data, ext=ext, mime=mime,
                                       sha256=sha256, prompt=prompt,
                                       width=getattr(result, "width", None),
                                       height=getattr(result, "height", None))
        # 复用旧行时回**旧行的文件名**，不是这次想要的那个名字：字节是同一份，
        # 名字也该是同一份——回新名字会让调用方以为落了第二份素材。
        return mid, asset_id, str(existing[0].get("filename") or ""), ""

    mid = new_id("mat", 6)
    fname = _file_name(filename, prompt, sha16, ext)
    kind = kind_of(fname) or "image"
    dst_key = (f"users/{safe_segment(user_id, 'u')}/convs/"
               f"{safe_segment(conv_id or '_', 'c')}/{mid}.{ext}")
    try:
        info = await storage.objects.put(
            dst_key, _one_chunk(data), content_type=mime)
    except StorageUnavailable as exc:
        return "", "", "", f"出图字节写不进对象存储（{type(exc).__name__}: {str(exc)[:120]}）"

    try:
        if conv_id:
            await storage.conversations.ensure(user_id, conv_id)
        row = await storage.materials.register(
            user_id, conv_id, dst_key, fname, kind,
            bytes_=info.bytes, sha256=info.sha256 or sha256, mime=mime,
            width=getattr(result, "width", None),
            height=getattr(result, "height", None),
            origin=ORIGIN, material_id=mid)
    except (IntegrityConflict, StorageUnavailable) as exc:
        await storage.objects.delete(dst_key)      # 登记不成立：不留孤儿字节
        return "", "", "", f"出图产物入素材库失败（{type(exc).__name__}: {str(exc)[:120]}）"

    asset_id = await _ensure_index(storage, data=data, ext=ext, mime=mime,
                                   sha256=sha256, prompt=prompt,
                                   width=getattr(result, "width", None),
                                   height=getattr(result, "height", None))
    return str(row["id"]), asset_id, fname, ""


async def _ensure_index(storage: Any, *, data: bytes, ext: str, mime: str,
                        sha256: str, prompt: str, width: Any, height: Any) -> str:
    """保证「按 sha 找字节」这条索引里有这一行。失败只回空串——**它不改判入库成功**。

    为什么失败不抛：素材册那一行已经写成了，用户能在自己册子里看到这张图；索引少一行
    只影响「它能不能当镜头素材上屏」。这是两级保证，不该用同一句话报。

    R13-T65 起这一条腿**只有一份实现**（`asset_index.ensure_index_row`）——上传件与出图
    产物走的是同一个「sha → object_key」登记动作，两处各写一份的话，两份会在
    「查重、失败姿态、object_key 布局」上慢慢走岔，而走岔的表现是闸⑬ 那种
    「三处 sha16 对不上」，最难查的一类。
    """
    return await asset_index.ensure_index_row(
        storage, sha256=sha256, ext=ext, mime=mime,
        name=_safe_name(prompt, sha256[:16], ext),
        data=data, bytes_=len(data), width=width, height=height,
        source=SOURCE, license=LICENSE, caption=prompt or "")


async def _one_chunk(data: bytes):
    yield data
