"""计划期探活：出计划卡**之前**先看一眼这台机器手上真有什么。

**为什么要有这个模块。** 规划轮今天只看得到「白名单里有 `imagegen`」这一件事，
于是它照样把 `imagegen` 排进 steps——而本机没配出图后端（`app_secrets` 里
`image_*` 一条都没有），那一镜一路烧到第 9、10 步才停下来问用户。用户看到的是
一张「跑到一半才问」的选项卡，根因是**计划期没有人告诉模型本机的真实能力**。

三条事实，**只读、只描述，不拦计划**：

① 出图后端配没配 —— 判据与 ``image_backend.generate()`` 实际会红的那两处**同源**
   （`api_base` / `default_model`），不在这里另判一套；
② 用户手上有几件可上屏的位图 —— 走 ``materials.list_visible(kinds=("image",))``，
   与素材册、附件解析同一条可见性腿；
③ 检索库里有多少件**可检索** —— ``enabled=True`` 且 ``attribution_required=False``，
   与 ``AssetLibraryRepo.search()`` 的 where **逐字同源**，只数检索面。

**读不到 = 未判，不是 0。** 库里读不出来时如实写「读不到」，不许写「0 件」——
「这台机器没有图」与「我查不出来」是两句不同的话，而前者会驱动模型换一条路
（把「查不出来」说成「没有」，正是 D-未判不绿 反对的那类假绿）。

**不拦计划。** 这一段是**事实**，不是判据：计划该不该排 `imagegen` 由 T68 的
可见面收口去管（把 `imagegen` 从模型可见工具面摘掉）。这里只保证一件事——
**模型看到的事实与机器的事实是同一个**。

**值由谁递进来。** 本模块**不**自己去算它该不该发：`planning_section_blocks` 收一个
``capability_notes`` 参数，值由 `agent.plan()` 现查活库后递进来。理由是这一段要读
**活库与密钥**，而 `prompt.py` 那一层今天连 storage 都拿不到——把它塞进去等于让
「拼文案」这个纯函数长出 I/O。递空串（离线装配 / 启动期自检 / 没接 storage）时
整块不出现，与加这一段之前**逐字相同**（金样 9 条里有 8 条正是靠这条保持不变的）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import image_backend, secrets
from .support import clean

__all__ = ["NOTES_HEAD", "image_backend_ready", "probe_capabilities",
           "render_capability_notes"]

#: 这一段的标题行。它是 `prompts/planning_round.md` 里那一块的**静态**首行，
#: **不由本模块产出**：守卫只管「整块发不发」，标题恒在块里。写在这儿是为了让
#: 断言能引用它（「模板里真有这一段」与「本模块真出这一段」是两件事，各断言各的）。
NOTES_HEAD = "本机能力实测"

#: 检索库一次最多数多少行。与 `asset_sources.live_source_counts` 同口径（100000）：
#: 这两处读的是同一张表、同一个面，上限不同会让「有多少件可检索」出现两个答案。
_COUNT_LIMIT = 100000


async def image_backend_ready(*, storage: Any = None,
                              user_id: str = "") -> tuple[bool, str]:
    """→ (能不能出图, 人话理由)。

    判据只取 ``image_backend.generate()`` **真会抛**的那两处，顺序也照它：
    ① `api_base(provider, base)` 为空 → `build_request` 抛「未知出图后端」；
    ② `default_model(provider, model)` 为空 → `generate` 抛「没有默认模型」。
    provider 本身为空是「压根没配」这一档，与「配了但缺模型名」分开报——
    前者用户只要去设置里选一家，后者还要再填一个模型 id，修的动作不同。

    **不打印任何密钥值**：这里只读 `resolve_image_config` 的四元组，key 那一位
    连名字都不进文案（与 `secrets` 的纪律一致）。
    """
    provider, _key, base, _src = await secrets.resolve_image_config(
        user_id, storage=storage)
    model, _size, _msrc = await secrets.resolve_image_options(
        user_id, storage=storage)
    if not provider:
        return False, "设置里的 `image_provider` 是空的"
    if not image_backend.api_base(provider, base):
        return False, f"配了 {provider}，但解析不出 base_url"
    if not image_backend.default_model(provider, model):
        return False, f"配了 {provider}，但没填模型名、这一家也没有默认模型"
    return True, f"已配 {provider}"


async def probe_capabilities(*, storage: Any = None, user_id: str = "",
                             conv_id: str | None = None) -> dict[str, Any]:
    """现查三件事实。**任何一件读不到 → 那一件记 None（未判），绝不抛。**

    返回形状固定四键（``image_backend`` / ``image_backend_why`` / ``uploads`` /
    ``library``），因为 `render_capability_notes` 要按它逐条出话，少一栏就少一行。
    """
    ready, why = await image_backend_ready(storage=storage, user_id=user_id)

    uploads: int | None = None
    if storage is not None and user_id:
        try:
            rows = await storage.materials.list_visible(
                user_id, conv_id, kinds=("image",))
            uploads = len(rows)
        except Exception:      # noqa: BLE001 - 探活是增益，读不到只记未判
            uploads = None

    library: int | None = None
    if storage is not None:
        try:
            rows = await storage.assets.list_assets(enabled=True, limit=_COUNT_LIMIT)
            library = sum(1 for r in rows if not r.get("attribution_required"))
        except Exception:      # noqa: BLE001 - 同上
            library = None

    return {"image_backend": ready, "image_backend_why": why,
            "uploads": uploads, "library": library}


def _count_line(label: str, n: int | None, *, tail: str) -> str:
    """件数那一行。``None`` 写「读不到（未判）」，**不写 0**。"""
    if n is None:
        return f"- {label}：读不到（未判）——库里查不出来，不代表一件都没有。"
    return f"- {label}：{n} 件。{tail}"


def render_capability_notes(facts: Any) -> str:
    """三件事实 → 给模型看的几行中文。空表 / 坏形状 → 空串（整块不出现）。

    **为什么是「几行中文」而不是几个键值**：这一段要拦的那个坏法是「模型不知道
    本机能力，照白名单字面排一条断腿」。键值对拦不住它（模型会照排），一句能读的
    「别排 `imagegen`，它今天必然失败」才拦得住。
    """
    if not isinstance(facts, Mapping):
        return ""
    ready = bool(facts.get("image_backend"))
    why = clean(facts.get("image_backend_why"))
    if ready:
        line1 = f"- 出图后端：**已配置**（{why or '已配'}）——`imagegen` 这条路今天走得通，但它花钱、要联网，且是供给次序里的**最后一档**（见技能正文的供给优先级）。"
    else:
        line1 = (f"- 出图后端：**未配置**（{why or '没配'}）——本轮**不要排 `imagegen`**，"
                 "那条路今天必然失败，一路烧到后面才停下来问用户是白烧。")
    rows = [
        line1,
        _count_line(
            "你手上可上屏的位图", facts.get("uploads"),
            tail="用户自己上传的图**能**直接进镜头，但**只有用户指名**才能用"
                 "（模型不许从附件里自动挑，也不许凭记忆填 sha）。"),
        _count_line(
            "检索库可检索的素材", facts.get("library"),
            tail="`search_illustration` 能搜到的就是这些；再按角色"
                 "（`asset_role`）过滤只会更少，不是更多。"),
    ]
    return "\n".join(rows)
