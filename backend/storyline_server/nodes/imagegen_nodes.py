"""R6-T24 出图节点：链上**第一个**真的会去出图的节点——供给次序的第④档。

**本模块只讲它自己在次序里的位置。** 供给优先级的唯一真源是
``agent_framework.plan.supply_order`` 那张四档表（R13 §4；散文面与它逐字双向
比对，见 ``tests/test_imagegen.py`` 的 T67 判据）。表在本文档下方逐字嵌入：

① 用户指名的上传件（只有用户触发）：attachments 里的 material_id → 索引行 → shots[].assets
② 库检索命中（模型触发）：search_illustration → select_illustration
③ 代码自己画（模型触发）：rough / three / katex / vector 四条画法通道
④ 出图 API（只有用户明确点名触发）：imagegen（配好 provider 与密钥才出得了图）
降级允许且必须可见：没配出图后端时明说降级（回执写明「图像通道降级为素材库检索，本片 N 镜无图」），静默出片仍然是失败——降级本身不是失败，把没有的图写成有才是

**出图是第④档**（最后一路、联网、花钱、只有用户明确点名触发）。它与检索的关系
由闸㉙（``plan.supply_order.check_supply_order``）在执行层判，不由散文强调：
第④档要真去出图，本 run 的产物总线里必须先有一次 ``search_illustration`` 返回
``no_match`` 的留痕。R13 §5 末条明写「不许把它做成提示词里再强调一遍」——
强调过三遍而执行层零判据，就是 R13 §0.10 那条现状（次序与用户拍板相反）。

**为什么本节点不抛异常**（与大多数节点不同）：没配后端是**常态**（这仓今天就没有任何
``image_*`` 配置），抛异常会把整条规划/出片流程打断在一件「本来就有降级路径」的事情上。
所以三种结局都回结构化回执：``ok:True`` 有图、``ok:False`` 且 ``degraded=True`` 没配
后端、``ok:False`` 且 ``degraded=False`` 配了但这次没成。**降级可见 ≠ 失败，静默出片才
是失败**——回执里的 ``message`` 就是给用户看的那句话，模型与页面都读得到。
闸㉙ 打回也走这条回执路（``ok:False`` + 点名），而不是抛：抛会让整条 run 死在
一个本来可以「先去检索」就能继续的分叉上。

**密钥**：只由 ``secrets.resolve_image_config`` 读出来交给 ``image_backend``，本模块
不落日志、不进返回值（T24：只验存在，绝不打印）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_framework import image_backend, secrets
from agent_framework.identity import storyline_session_id
from agent_framework.imagegen_library import store_generated
from agent_framework.plan.supply_order import (
    check_supply_order, render_supply_order, supply_order_undetermined_note,
    traced_no_match_of)

from .core_nodes import StoryNode, _obj


class GenerateImageNode(StoryNode):
    """出图：提示词 → 一张位图 → 落进素材册与「按 sha 找字节」的索引。

    供给次序的第④档。**次序的唯一真源是**
    ``agent_framework.plan.supply_order`` 那张四档表（R13 §4），本描述与
    ``imagegen_nodes`` 模块头、``motion_explainer/SKILL.md`` 供给节三处散文面
    与它逐字双向比对（T67）：表里有而散文没有 → 红；散文里有而表里没有 → 也红。
    """

    name = "imagegen"
    display_name = "出图"
    description = (
        "按提示词生成一张配图（需要页面「设置 → 出图后端」配好 provider 与密钥）——"
        "它是供给次序的第④档（最后一路、联网、花钱、只有用户明确点名才调）。"
        "供给优先级（唯一真源：agent_framework.plan.supply_order）：\n"
        + render_supply_order()
        + "\n闸㉙：本 run 必须先有一次 search_illustration 返回 no_match 的留痕，"
        "本节点才允许出图；没有留痕时打回并给出路（不抛异常）。"
        "产物落在当前会话的素材册里，回执带 asset 三键（name/sha256/ext），"
        "可直接填进 shots[].assets。")
    input_schema = _obj("出图", {
        "prompt": {"type": "string",
                   "description": "画面描述。写「能被认出的东西」——主体是什么、什么构成、"
                                  "什么视角，别写抽象词（闸⑯ 对 subject 有同一把尺）"},
        "name": {"type": "string",
                 "description": "落盘文件名（镜头代码里 staticFile(\"<name>\") 引用它）；"
                                "留空则按提示词自动起名"},
        "size": {"type": "string", "description": "尺寸，如 1024x1024；留空用配置或默认值"},
        "model": {"type": "string", "description": "模型名；留空用配置或后端默认"},
        "caption": {"type": "string", "description": "素材册里的说明文字（用于以后检索）"},
        "shots_needing_image": {"type": "integer",
                                "description": "本片还需要图的镜头数——只用于把降级回执说具体"
                                               "（「本片 N 镜无图」），不影响出图行为"},
    }, required=["prompt"])

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        prompt = str(inputs.get("prompt") or "").strip()
        if not prompt:
            # 空提示词是本节点的**用法错误**（不是降级），如实回执，不去打后端：
            # 各家对空 prompt 的报错措辞都不同，而真正的原因在我们这边。
            return {"ok": False, "degraded": False, "image": None, "asset": None,
                    "message": "imagegen：prompt 为空——出图要一句画面描述"}

        provider, key, base, source = await secrets.resolve_image_config(
            state.user_id or "", storage=self.storage)
        cfg_model, cfg_size, opt_source = await secrets.resolve_image_options(
            state.user_id or "", storage=self.storage)
        model = str(inputs.get("model") or "").strip() or cfg_model
        size = str(inputs.get("size") or "").strip() or cfg_size
        need = inputs.get("shots_needing_image")

        if not (provider or key or base):
            # §9-Q27：没配后端就走检索降级，并把这件事**说出来**。这句 message 会被
            # 模型读进上下文，于是它有据可依地去 search_illustration，而不是自己编一个
            # 「图中显示了……」的镜头说明。
            more = (f"，本片 {int(need)} 镜无图" if isinstance(need, int) and need > 0 else "")
            return {
                "ok": False, "degraded": True, "image": None, "asset": None,
                "provider": "", "model": "", "size": "",
                "message": ("未配出图后端，图像通道降级为素材库检索"
                            f"（请在「设置 → 出图后端」配 provider 与密钥）{more}"),
            }
        if not provider:
            return {"ok": False, "degraded": True, "image": None, "asset": None,
                    "provider": "", "model": model, "size": size,
                    "message": "填了密钥或实例地址但没选后端，无法判断该把 key 交给谁"}

        # 闸㉙ 取材次序（R13 §5）：第④档要真去出图，本 run 的产物总线里必须先有一次
        # ``search_illustration`` 返回 ``no_match`` 的留痕。**判据在执行层（这里），
        # 不在提示词里**——R13 §5 末条明写「不许把它做成提示词里再强调一遍」：强调过
        # 三遍而执行层零判据，就是 §0.10 那条现状（次序与用户拍板相反，只是因为这台
        # 部署没配出图 key 才没烧钱——那是偶然，不是规矩）。
        #
        # 留痕从产物总线读（``search_illustration`` 执行完自动进总线），是**机器记的**
        # 而不是模型自报的「我检索过了」——自报的留痕会被模型记错，而记错的表现正是
        # 本闸要防的那个序列。
        why, _unjudged = check_supply_order(
            traced_no_match=await traced_no_match_of(state.store),
            used_imagegen=True)
        if why:
            # 打回也走回执路而不是抛（本节点不抛异常的理由见模块 docstring）：抛会让
            # 整条 run 死在一个本来「先去检索再回来」就能继续的分叉上。回执带
            # ``gate`` 一栏，回执层可以按它把这道闸的账与降级的账分开。
            return {"ok": False, "degraded": False, "image": None, "asset": None,
                    "provider": provider, "model": model, "size": size,
                    "gate": "supply_order",
                    "message": why}

        try:
            result = await asyncio.to_thread(
                image_backend.generate, provider=provider, key=key, base=base,
                prompt=prompt, model=model, size=size)
        except image_backend.ImageBackendError as exc:
            return {"ok": False, "degraded": False, "image": None, "asset": None,
                    "provider": provider, "model": model, "size": size,
                    "source": source,
                    "message": f"出图失败：{exc}"}

        session_id = storyline_session_id(state.user_id, state.conversation_id) \
            if state.user_id and state.conversation_id else state.session_id
        # 名字在**入库之前**定下来并传进去：调用方给了就用它（镜头代码里
        # `staticFile("<name>")` 要引用同一个名字），否则由入库侧按提示词起。
        # 回执里的 `asset.name` 与素材册那一行的 filename 因此必然是同一个——
        # 两处分头起名的话，排障时「这张图在哪」会有两个答案。
        material_id, asset_id, name, note = await store_generated(
            self.storage, session_id, result, prompt=prompt,
            filename=str(inputs.get("name") or "").strip(),
            conversation_id=state.conversation_id)
        if not name:            # 入库失败时也得给一个可引用的名字，不然镜头引谁去
            name = f"imagegen-{result.sha16}.{result.ext}"

        notes: list[str] = list(result.notes)
        if note:
            notes.append(note)
        if not asset_id:
            notes.append("这张图没进「按 sha 找字节」的索引，镜头侧 assets 投递会找不到它")
        return {
            "ok": True, "degraded": False,
            "provider": provider, "model": result.model, "size": result.size,
            "source": source, "options_source": opt_source,
            "material_id": material_id, "asset_id": asset_id,
            "name": name, "sha256": result.sha256, "sha16": result.sha16,
            "ext": result.ext, "bytes": result.bytes,
            "width": result.width, "height": result.height,
            "latency_ms": result.latency_ms,
            # 供规划轮**原样贴进** `shots[].assets` 的三键（§5.1 冻结的形状）。
            "asset": {"name": name, "sha256": result.sha256, "ext": result.ext},
            "notes": notes,
            "message": (f"出图成功：{result.provider}/{result.model} "
                        f"{result.width or '?'}×{result.height or '?'} "
                        f"{result.bytes} 字节 sha16={result.sha16}"),
        }


IMAGE_NODE_CLASSES = [GenerateImageNode]
