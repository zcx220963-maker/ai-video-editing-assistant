# -*- coding: utf-8 -*-
"""闸㉛ 指名权（R13 §5）：用户上传的图，**只有用户指名才能摆进镜**。

「能摆进镜」与「该不该摆进镜」是两件事。T65 修的是前者（上传即入索引行，
``get_by_sha`` 找得到字节）；这一道闸判后者：索引里**有**这张图，不代表模型
**可以用**它。

**它封的是哪条路。** 模型凭记忆（或凭 sha 前缀猜）往 ``shots[].assets`` 里填一个
用户上传图的 sha——查得到（字节在、索引行在），于是闸⑬ 与渲染腿一路放行，
而用户从没说过「这一镜用我这张图」。这与 R13 §0.4 那条 ``MediaError`` 同族：
一个是「查不到」，一个是「查得到但不是你该用的」。R13 §4 硬话 2 / Q160：
①④ 都归「用户触发」，模型不许自动走。

**一处必须如实说明的偏差**（写在这里而不是只写在报告里）：R13 §5 原文要的判据是
「本次 run 的 ``attachments``」，但 ``attachments`` **到不了 Storyline 进程**
（``grep attachments backend/storyline_server`` 0 命中）。可行且今天会红的替代
判据是**会话域**双集合：

    uploaded_shas = asset_library 里 source='user-upload' 的行（节点侧拿得到）
    allowed_shas  = materials.list_visible(user_id, conv_id, kinds=("image",))

两个集合都读不到 → 未判；本片没有引用上传件 → 未判（「本片没用用户上传的图」）；
引用了上传件但不在允许集合 → **打回**，文案点名「这一镜的图不是用户指名的那一件」。

为什么会话域这个替代不算放宽：闸要防的是「模型**凭记忆**填一个 sha」。会话域里
可见的位图 = 用户在这条会话里真带进来/真上传的（``materials`` 的 owner 隔离 +
会话归属两重校验，``repositories.MaterialsRepo._visible``）——凭记忆填的 sha
**不会**落在里面，于是照样打回。防的正是同一件事。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: 用户上传件在 ``asset_library`` 里的 source 值。Q155/T65 钉死的枚举，与出图产物的
#: ``art`` 分开：这一闸只判「用户自己带进来的」那一档，不碰出图产物。
UPLOAD_SOURCE = "user-upload"

#: 闸㉛ 的打回理由。三件事都点名：哪一件（sha 前 16 位）、为什么不行（不在本会话
#: 可见集合）、出路是什么（让用户指名，或换一件本会话里的图）。只有 sha 前半截——
#: 全 64 位会把打回消息撑长，而前半截足够在素材册里定位那一件。
_DESIGNATED_REJECT = (
    "闸㉛ 指名权：{shas} 是用户上传的图，但它不在本会话的素材册可见集合里——"
    "上传件摆进镜**只有用户指名才能用**（R13 §4 硬话 2 / Q160）。"
    "要么请用户在消息里点名（「第 N 镜用我这张图」），要么换一件本会话素材册里"
    "看得见的图；也**不许凭记忆填一个 sha**——那是这道闸要封的那条路"
)


def check_user_designated_asset(assets: Sequence[Mapping[str, Any]] | None, *,
                                uploaded_shas: set[str] | None,
                                allowed_shas: set[str] | None) -> tuple[str, str]:
    """闸㉛ → (打回理由, 未判理由)。两者都空 = 过。**纯函数，一次 I/O 都不做。**

    三个出口，各自对应一种「没判过 ≠ 通过」（P5 / D-未判不绿）：

    * **未判**：两个集合任一读不到（存储不可达）——拿「没读到」当「没问题」是假绿；
    * **未判**：本片没有引用任何上传件——这道闸本轮无事可判（回执上如实写
      「本片没用用户上传的图」，不许折算成通过）；
    * **打回**：引用了上传件而它不在允许集合里；
    * **过**：引用的上传件全在允许集合里。

    ``assets`` 是 ``shots[].assets`` 那一栏（一项一份 ``{name, sha256, ext}``）；
    传 None 或空 = 没有资产声明，按「没有引用上传件」走未判。
    """
    if uploaded_shas is None or allowed_shas is None:
        return "", ("闸㉛ 指名权：上传件名册或素材册可见集合读不到（存储不可达）——"
                    "这道闸本轮**没判过**（未判不是通过）")
    used: list[str] = []
    for a in assets or ():
        if not isinstance(a, Mapping):
            continue
        sha = str(a.get("sha256") or "").strip().lower()
        if sha and sha in uploaded_shas:
            used.append(sha)
    if not used:
        return "", ("闸㉛ 指名权：本片没有引用用户上传的图——"
                    "这道闸本轮无事可判（未判不是通过）")
    bad = [s for s in used if s not in allowed_shas]
    if bad:
        return _DESIGNATED_REJECT.format(
            shas="、".join(f"sha256={s[:16]}…" for s in sorted(set(bad)))), ""
    return "", ""


async def designated_asset_sets(storage: Any, *, user_id: str, conv_id: str
                                ) -> tuple[set[str] | None, set[str] | None]:
    """取闸㉛ 的两个集合。**任何一边读不到 → 那一边是 None（未判），不是空集。**

    为什么 None 与空集必须分得开：空集会被读成「库里没有上传件」→ 判据走向
    「本片没用用户上传的图」那一档；而事实是「这一趟读不到」。两种处境在回执上
    必须是两句话（与 ``capability.py`` 的探活纪律同一条）。

    两个集合都**不按 enabled 过滤**（``list_assets`` 的缺省是 ``enabled=True``，
    而上传件按 Q155 是 ``enabled=False``）——这一闸要的是「这一档料存在吗」，
    不是「它可不可被检索」。
    """
    uploaded: set[str] | None = None
    allowed: set[str] | None = None
    try:
        rows = await storage.assets.list_assets(source=UPLOAD_SOURCE,
                                                enabled=False, limit=1000)
        uploaded = {str(r.get("sha256") or "").strip().lower() for r in rows or ()}
    except Exception:                      # noqa: BLE001 —— 读不到是未判，不是空
        uploaded = None
    if user_id:
        try:
            rows = await storage.materials.list_visible(
                user_id, conv_id or None, kinds=("image",))
            allowed = {str(r.get("sha256") or "").strip().lower() for r in rows or ()}
        except Exception:                  # noqa: BLE001 —— 同上
            allowed = None
    return uploaded, allowed


async def check_shots_designated(storage: Any, shots: Sequence[Mapping[str, Any]],
                                 *, user_id: str, conv_id: str
                                 ) -> tuple[list[str], list[str]]:
    """整片那一趟的接线形状（plan 入库前 / 烧像素前两处共用）：逐镜判 → (打回, 未判)。

    两个集合在**整片只取一次**（不是逐镜取）：闸要的是「本会话的上传件名册」，
    它在一趟渲染里不会变；逐镜取会让一次存储抖动在十镜上放大成十条未判。
    """
    uploaded, allowed = await designated_asset_sets(
        storage, user_id=user_id, conv_id=conv_id)
    errors: list[str] = []
    undetermined: list[str] = []
    for shot in shots or ():
        sid = str(shot.get("id") or "?")
        why, unjudged = check_user_designated_asset(
            shot.get("assets"), uploaded_shas=uploaded, allowed_shas=allowed)
        if why:
            errors.append(f"{sid}：{why}")
        if unjudged:
            undetermined.append(f"{sid}：{unjudged}")
    return errors, undetermined


__all__ = [
    "UPLOAD_SOURCE", "check_user_designated_asset", "designated_asset_sets",
    "check_shots_designated",
]
