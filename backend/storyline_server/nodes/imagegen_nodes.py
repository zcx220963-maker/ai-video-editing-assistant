"""R6-T24 出图节点：链上**第一个**真的会去出图的节点。

为什么要单独一个节点（而不是让 ``search_illustration`` 顺手出图）：第五根因是
「链路上没有图像」——素材库里没有合适的图时，链上唯一的动作是「没有就没有」，
于是模型退回卡片+文字（§1.5 那支被驳回的形态的机制性根因）。出图是**另一条供给腿**，
它与检索的关系是「先检索、未命中再出图」（§9-Q27 的写死优先级）：

    检索库命中 → 用库里的（免费、不联网、可溯源）
    未命中     → 本节点出图（要配置后端）
    后端没配   → **明说降级**，绝不静默出一支没有图的片子

**为什么本节点不抛异常**（与大多数节点不同）：没配后端是**常态**（这仓今天就没有任何
``image_*`` 配置），抛异常会把整条规划/出片流程打断在一件「本来就有降级路径」的事情上。
所以三种结局都回结构化回执：``ok:True`` 有图、``ok:False`` 且 ``degraded=True`` 没配
后端、``ok:False`` 且 ``degraded=False`` 配了但这次没成。**降级可见 ≠ 失败，静默出片才
是失败**——回执里的 ``message`` 就是给用户看的那句话，模型与页面都读得到。

**密钥**：只由 ``secrets.resolve_image_config`` 读出来交给 ``image_backend``，本模块
不落日志、不进返回值（T24：只验存在，绝不打印）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_framework import image_backend, secrets
from agent_framework.identity import storyline_session_id
from agent_framework.imagegen_library import store_generated

from .core_nodes import StoryNode, _obj


class GenerateImageNode(StoryNode):
    """出图：提示词 → 一张位图 → 落进素材册与「按 sha 找字节」的索引。"""

    name = "imagegen"
    display_name = "出图"
    description = (
        "按提示词生成一张配图（需要页面「设置 → 出图后端」配好 provider 与密钥）。"
        "供给优先级是「先检索素材库、未命中再出图」；没配后端时**明确降级为检索**并在回执里"
        "说明，不会静默出一支没有图的片子。产物落在当前会话的素材册里，回执带 "
        "asset 三键（name/sha256/ext），可直接填进 shots[].assets。")
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
