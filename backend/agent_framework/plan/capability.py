"""计划期探活：出计划卡**之前**先看一眼这台机器手上真有什么。

**为什么要有这个模块。** 规划轮今天只看得到「白名单里有这个节点」这一件事，
于是它照样把「取一张图」排进 steps——而本机根本没有可取的图，那一镜一路烧到第 9、10 步
才停下来问用户。用户看到的是那张「跑到一半才问」的选项卡，根因是**计划期没有人告诉
模型本机的真实能力**。

一条事实，**只读、只描述，不拦计划**：

- 用户手上有几件可上屏的位图 —— 走 ``materials.list_visible(kinds=("image",))``，
  与素材册、附件解析同一条可见性腿。

**读不到 = 未判，不是 0。** 库里读不出来时如实写「读不到」，不许写「0 件」——
「这台机器没有图」与「我查不出来」是两句不同的话，而前者会驱动模型换一条路
（把「查不出来」说成「没有」，正是 D-未判不绿 反对的那类假绿）。

**2026-10-16 去掉的两条事实。** 这一段原本还报三件事：① 出图后端配没配
（``imagegen_ready``）、③ 检索库里有多少件可检索。两者连同 ``search_illustration`` /
``select_illustration`` / ``imagegen`` 三个节点一并删了——图形片不再有取材通道，
画面只有一路：模型逐镜写代码画（见技能正文「每镜必画」）。留下一句「出图后端已配置」
会驱动模型把 ``imagegen`` 排进 steps，而那个工具今天不存在。

**不拦计划。** 这一段是**事实**，不是判据。这里只保证一件事——
**模型看到的事实与机器的事实是同一个**。

**值由谁递进来。** 本模块**不**自己去算它该不该发：`planning_section_blocks` 收一个
``capability_notes`` 参数，值由 `agent.plan()` 现查活库后递进来。理由是这一段要读
**活库**，而 `prompt.py` 那一层今天连 storage 都拿不到——把它塞进去等于让
「拼文案」这个纯函数长出 I/O。递空串（离线装配 / 启动期自检 / 没接 storage）时
整块不出现，与加这一段之前**逐字相同**（金样里那几条正是靠这条保持不变的）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .support import clean

__all__ = ["NOTES_HEAD", "probe_capabilities", "render_capability_notes"]

#: 这一段的标题行。它是 `prompts/planning_round.md` 里那一块的**静态**首行，
#: **不由本模块产出**：守卫只管「整块发不发」，标题恒在块里。写在这儿是为了让
#: 断言能引用它（「模板里真有这一段」与「本模块真出这一段」是两件事，各断言各的）。
NOTES_HEAD = "本机能力实测"


async def probe_capabilities(*, storage: Any = None, user_id: str = "",
                             conv_id: str | None = None) -> dict[str, Any]:
    """现查这件事实。**读不到 → 记 None（未判），绝不抛。**

    返回形状固定一（``uploads``），因为 `render_capability_notes` 要按它逐条出话，
    少一栏就少一行。
    """
    uploads: int | None = None
    if storage is not None and user_id:
        try:
            rows = await storage.materials.list_visible(
                user_id, conv_id, kinds=("image",))
            uploads = len(rows)
        except Exception:      # noqa: BLE001 - 探活是增益，读不到只记未判
            uploads = None

    return {"uploads": uploads}


def _count_line(label: str, n: int | None, *, tail: str) -> str:
    """件数那一行。``None`` 写「读不到（未判）」，**不写 0**。"""
    if n is None:
        return f"- {label}：读不到（未判）——库里查不出来，不代表一件都没有。"
    return f"- {label}：{n} 件。{tail}"


def render_capability_notes(facts: Any) -> str:
    """这件事实 → 给模型看的一行中文。空表 / 坏形状 → 空串（整块不出现）。

    **为什么是「几行中文」而不是几个键值**：这一段要拦的那个坏法是「模型不知道
    本机能力，照白名单字面排一条断腿」。键值对拦不住它（模型会照排），一句能读的
    「这台机器手上有几张图」才拦得住。
    """
    if not isinstance(facts, Mapping):
        return ""
    return _count_line(
        "你手上可上屏的位图", facts.get("uploads"),
        tail="用户自己上传的图**能**进片子，但**只有用户指名**才能用"
             "（模型不许从附件里自动挑）。图形片里没有取材通道——那一类的画面由"
             "模型逐镜写代码画出来。")
