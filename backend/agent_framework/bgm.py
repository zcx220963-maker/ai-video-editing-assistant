# -*- coding: utf-8 -*-
"""曲库配乐：从 `materials` 表里挑一首，回源到本地，交给 ffmpeg 混。

## 为什么这件事必须在这个文件里，不在 `simple/`

`tests/test_simple_boundary.py` 钉着一条硬边界：`simple/` 下任何文件**不许** import
`storage.repositories`。那条边界不是为了洁癖——它挡的是"简洁线顺手复用一下分镜链"的
第一步。所以选曲留在 `agent_framework/` 这一侧，两边各管一件事：

    simple/voice.py   只管 ffmpeg：铺时间轴、淡入淡出、闪避、响度
    bgm.py            只管"哪首歌"：曲库查询 + 对象存储回源

`voice.pick_bgm()` 读本地目录（`SIMPLE_BGM_DIR`）那版**留着**当离线兜底：开发机上
没有曲库、对象存储也没起时，扔几个 mp3 进目录就能出片。

## 三级取用，从"最贴题"退到"有的就行"

    ① `origin="bgm"` 且文件名匹配主题   ← 用户在音乐库面板导入的
    ② `origin="bgm"` 全部                ← 曲库有歌，只是名字跟主题对不上
    ③ 本人任意音频                        ← 没建曲库，但对话里传过 mp3

三级都空就返回 None，**不合成占位音**。旧链 `select_BGM` 在没有曲库时会
`tone_wav()` 造一段 196Hz 正弦垫底——那东西在成片里听着像设备故障，比静音难听。
静音至少还是"这条片子没配乐"。

## 挑哪一首：先看能不能一次铺完

曲库里 40 秒的短片循环铺 120 秒，接缝处会有一次硬切。所以候选里**优先挑时长够盖住
整片的**；一首都盖不住时取最长的（循环次数最少，接缝最少）。这一步排在相关性之后
——先按文件名相关性取前 5 条，再在这 5 条里挑"够长的那条"，不是拿长度压相关性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: 曲库标记。`server.py` 的 `/bgm/import` 落库时写的就是这个值。
ORIGIN_LIBRARY = "bgm"
#: 一次取几条候选。5 条足够：再多也只是"更不贴题的"。
POOL = 5
#: 一首歌要盖住整片的多少才算"一次铺完"。0.95 是给容器时长与 probe 的误差留的缝。
COVER_RATIO = 0.95


def _dur(row: dict[str, Any]) -> float:
    try:
        return float(row.get("duration_sec") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def choose(rows: list[dict[str, Any]], need_sec: float = 0.0) -> dict[str, Any] | None:
    """在候选里挑一首。**纯函数**，不碰 IO，所以能单独测。

    规则：`need_sec` 给了就优先挑"时长盖得住"的第一条（候选已按相关性排序），
    一条都盖不住时取最长的；`need_sec` 为 0 就直接取第一条（最相关）。
    """
    if not rows:
        return None
    if need_sec <= 0:
        return rows[0]
    for row in rows:
        if _dur(row) >= need_sec * COVER_RATIO:
            return row
    return max(rows, key=_dur)


async def pick(storage: Any, user_id: str, prompt: str, *, work_dir: Path,
               need_sec: float = 0.0, force: str = "") -> dict[str, Any] | None:
    """按主题（或按**点名**）从曲库挑一首并回源到 `work_dir`。返回 dict 或 None。

    `force` 给了就**只在这首里挑**，并且**不看时长**——用户点名的曲子比"盖不盖得住"
    重要：短了就循环铺满（`mix.add_bgm` 的 `-stream_loop -1`），不该被换成另一首。
    这一条以前没有，于是 150 秒的片子点 We Never（90 秒）会被悄悄换成库里最长的那首。

    取不到一律返回 None（曲库空、对象存储没起、回源失败），**不抛**——配乐是锦上添花，
    没有它这条片子照出。调用方把 None 记进报告就行。
    """
    rows = await _candidates(storage, user_id, prompt, force=force)
    # 点名时取第一条（搜不到就是 None，**不降级**）；没点名才按时长挑。
    row = (rows[0] if rows else None) if force else choose(rows, need_sec)
    if row is None:
        return None
    try:
        path = await storage.objects.localize(row["object_key"], Path(work_dir))
    except Exception:  # noqa: BLE001 —— 对象存储不可用不该让出片失败
        return None
    return {
        "path": Path(path),
        "filename": row.get("filename") or Path(path).name,
        "material_id": row.get("id") or "",
        "origin": row.get("origin") or "upload",
        "duration": _dur(row),
    }


async def _candidates(storage: Any, user_id: str, prompt: str, *,
                      force: str = "") -> list[dict[str, Any]]:
    """三级取用。每一级都可能因为表不存在 / 存储没起而抛，所以逐级吞掉。

    三级用同一份 `search()`，差别只在 `(origin, query)`：
    传空 query 时 `search` 只做可见性过滤 + 按创建时间倒序，不做 2-gram 排序。

    `force` 给了就**只认这一个**，两种写法都收：
      ① **material_id**——用户在对话里把音频挂成附件，传过来的就是这串 id；
      ② 曲名（如「We Never」）——按文件名 2-gram 搜。
    两种都搜不到就返回空（不再降级到"随便挑一首"）：点名的意思就是"别的都不要"。
    """
    if force:
        # ① 先当 material_id 查。附件挂上来的 id 在曲库里未必有文件名匹配，
        # 所以不能只靠 2-gram——那样"挂了附件却没配乐"会很难查。
        try:
            row = await storage.materials.get(force)
        except Exception:  # noqa: BLE001 —— 表查不了就当不是 id
            row = None
        if row and str(row.get("owner_user_id") or "") == user_id \
                and str(row.get("kind") or "") == "audio":
            return [row]
        tiers: tuple[tuple[Any, str], ...] = ((ORIGIN_LIBRARY, force),)
    else:
        tiers = (
            (ORIGIN_LIBRARY, prompt),   # ① 曲库里名字对得上主题的
            (ORIGIN_LIBRARY, ""),       # ② 曲库里所有的（名字对不上也算，总比没有强）
            (None, ""),                 # ③ 本人任意音频（对话里传的 mp3 也算）
        )
    for origin, query in tiers:
        try:
            rows = await storage.materials.search(
                user_id, query, conv_id=None, kinds=("audio",),
                origin=origin, limit=POOL)
        except Exception:  # noqa: BLE001
            return []
        if rows:
            return rows
    return []


async def mix_into(track: Path, dst: Path, *, storage: Any, user_id: str,
                   prompt: str, total_sec: float, work_dir: Path,
                   bgm_name: str = ""
                   ) -> tuple[Path, dict[str, Any] | None]:
    """给一条旁白轨加上配乐，返回 ``(音轨, 配乐说明)``。

    这是**唯一**该被调用的入口：两条出片路径（网页 `/simple/video`、对话里的
    `make_short_video`）都走它，所以"有没有配乐"这件事只有一个答案。

    **`bgm_name` 为空就不配乐**（2026-10-26 改）。以前是按 prompt 自动挑，
    于是"用户压根没提配乐"也会被塞一首——那首歌跟片子没关系，只是名字凑巧
    和主题撞上了几个字。现在只有用户**点名**了才配，点的那首**循环铺满整长**
    （`mix.add_bgm` 用 `-stream_loop -1`），短了也不换。

    降级：曲库（materials.origin=bgm） → 本地目录（SIMPLE_BGM_DIR） → 不加。
    任何一级失败都只是"这条片子没配乐"，不抛。
    """
    from storyline_server.simple import mix as mx

    name = str(bgm_name or "").strip()
    if not name:
        return track, None
    got = await pick(storage, user_id, prompt, work_dir=work_dir,
                     need_sec=total_sec, force=name) if storage is not None else None
    if got is None:
        local = mx.pick_local(name)
        if local is not None:
            got = {"path": local, "filename": local.name, "material_id": "",
                   "origin": "local_dir", "duration": 0.0}
    if got is None:
        return track, None
    try:
        out = mx.add_bgm(track, dst, total_sec=total_sec, bgm=got["path"])
    except Exception:  # noqa: BLE001 —— 混音失败就用原轨，不拿配乐换掉整条片子
        return track, None
    return out, got

