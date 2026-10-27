# -*- coding: utf-8 -*-
"""简洁短片线的 DAG 节点：分镜 → 写镜 → 渲染。

## 为什么拆

原来这里只有一个 `simple_film` 节点，内部调 `pipeline.run()` 把分镜/写码/质检/渲染
全干完——**设计 §21「流程原子化」没做到**，差距登记在 §13 X1/X2。

现在按编排层的约定拆成三个原子节点：

    plan_short_film  ──required_nodes──▶  write_short_shots  ──▶  render_short_film

| 节点 | 一件事 | 耗时 | 进 Store 的产物 |
|---|---|---|---|
| `plan_short_film` | 导演：出 `tone` + `shots[{sec,text,plan}]` | ~30 秒 | `plan` / `shots` / `total_sec` |
| `write_short_shots` | 逐镜写码 + 抽帧 + 评审回路 | 1~3 分钟 | `film_id` / `secs` / `audit` |
| `render_short_film` | 渲画面 + 合音轨 + 交付 | 5~30 分钟 | `mp4_url`(ephemeral) / `object_key` |

三点与设计对齐：

1. **`required_nodes` 声明 DAG 边**，拦截器递归补齐上游（`Interceptor`）。
2. **产物进 Store**，`read_node_history` 能查到每一镜的画法——黑盒时代查不到。
3. **渲染是分钟级**，所以 `render_short_film` 登记进 `server.LONG_RUNNING_NODES`，
   走现成的「提交 → 内联等一会 → 回 queued → `render_status` 轮询」，
   不再让一次 JSON-RPC 往返等一整部片子。

## 与原生工具的关系

`plan_short_film` / `render_short_film` 这两个名字**只能有一个实现**：原来是
`agent_framework/tools/short_video.py` 里的原生工具，现在是这里的节点。原生那两个
已删除——同一个名字两套实现，注册表里就是「后注册的悄悄盖掉前一个」，
出事时看不出是哪份在跑。

`make_short_video`（一句话出全片）与 `reshoot_shot`（改一镜）仍是原生工具：
它们是「一步到位」与「局部修补」，不是 DAG 里的一环。
"""

from __future__ import annotations

from typing import Any

from agent_framework.orchestration import BaseNode, NodeConfig, NodeState

from ..simple import deliver, kernel, pipeline, project, showrunner
from .core_nodes import _obj

#: 每镜抽帧比例（与 `showrunner.JUDGE_ROUNDS` 一起决定质检成本）。
#: 渲染前不抽帧——静帧按需在 `render=false` 时单独出。


class _ShortFilmNode(BaseNode):
    """三个节点共用的构造签名与工具方法。

    不继承 `StoryNode`：那会与 `core_nodes` 形成循环导入（core_nodes 里没有
    simple 的东西，simple_nodes 里也不该反向依赖它的业务方法）。
    构造签名与 `build_real_registry` 的工厂对齐：`cls(settings, providers, storage, registry=reg)`。
    """

    def __init__(self, cfg: Any = None, *legacy: Any, registry: Any = None,
                 **_: Any) -> None:
        # `registry` 必须转交给 BaseNode：吞掉它，节点建了却没进注册表，
        # 白名单里有、MCP 工具面上永远没有（TestServer 的「白名单 ↔ 实现」等式会抓）。
        super().__init__(registry=registry)
        # 两种构造都收：`_ShortFilmNode(cfg)`（文档写法）与
        # `_ShortFilmNode(settings, providers, storage, ...)`（历史写法）。
        if not isinstance(cfg, NodeConfig):
            cfg = NodeConfig(cfg,
                             legacy[0] if legacy else None,
                             legacy[1] if len(legacy) > 1 else None)
        self.settings = cfg.settings
        self.providers = cfg.providers
        self.storage = cfg.storage
        self.registry = registry

    # 三个节点都用同一条「默认主 LLM + 显式身份」的路：`kernel._complete` 内部
    # 走 `use_identity(user_id, conversation_id)`，不显式传身份就解析不到这位用户的键
    # （真机表现是「未配置模型密钥」，而键其实配着）。
    @staticmethod
    def _ident(state: NodeState) -> tuple[str, str]:
        return (str(getattr(state, "user_id", "") or ""),
                str(getattr(state, "conversation_id", "") or ""))


class PlanShortFilmNode(_ShortFilmNode):
    """一句话 → 分镜表。**不渲染**，30 秒级。"""

    name = "plan_short_film"
    display_name = "短片分镜"
    description = (
        "把一句话变成一份**分镜表**：片名、整片画法基调、每镜多长、每镜画什么、用什么技术。"
        "不渲染，几十秒。**镜数与镜长由模型按内容定**，不由公式算。"
        "拿到 `plan` 后：要给用户看讲法就直接列 `shots`；要出片就调 "
        "`write_short_shots`（它会把 plan 从 Store 里读出来，不用你重传）。"
    )
    required_nodes: list[str] = []
    # 没有上游：这是这条线的起点。
    output_keys = ("plan",)

    input_schema = _obj("主题", {
        "prompt": {"type": "string",
                   "description": "要讲什么。可以带讲法（「从瑞利散射讲到日落变红」）"},
        "seconds": {"type": "number",
                    "description": "目标时长（秒），8~240，默认 30。镜数由内容定，不按这个数均分"},
        "aspect": {"type": "string", "enum": ["portrait", "landscape", "square"],
                   "description": "画幅，默认 portrait（竖屏）"},
    }, required=["prompt"])

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        prompt = str(inputs.get("prompt") or state.user_request or "").strip()
        if len(prompt) < 4:
            raise ValueError("prompt 太短，至少给一个完整主题（例如「为什么天空是蓝色的」）")
        seconds = float(inputs.get("seconds") or 30.0)
        aspect = kernel.norm_aspect(inputs.get("aspect"))
        user_id, conv_id = self._ident(state)
        plan_obj, calls = await showrunner.plan(
            prompt, seconds=seconds, aspect=aspect,
            user_id=user_id, conversation_id=conv_id)
        shots = plan_obj["shots"]
        return {
            "plan": plan_obj,
            "shots": len(shots),
            "total_sec": round(sum(float(s["sec"]) for s in shots), 2),
            "calls": calls,
            # 给模型看的那一份：每镜一句话，够它转述讲法；整份 plan 在 Store 里。
            "__model_view__": {
                "title": plan_obj.get("title") or "",
                "tone": plan_obj.get("tone") or "",
                "shots": [{"sec": s["sec"], "text": s["text"]} for s in shots],
                "total_sec": round(sum(float(s["sec"]) for s in shots), 2),
            },
        }


class WriteShortShotsNode(_ShortFilmNode):
    """按分镜逐镜写码 + 抽帧评审。**不渲染**，1~3 分钟。"""

    name = "write_short_shots"
    display_name = "写镜与质检"
    description = (
        "按分镜逐镜写画面代码，渲帧给模型自己看、不合格的只重画那几镜（≤3 轮），"
        "然后拼成一条完整片子存成工程。**不渲成片**，1~3 分钟。"
        "上游 `plan_short_film` 的分镜会自动从 Store 读；也可以直接传 `scenes` 覆盖。"
        "出片前想先看画面，传 `preview=true` 会回每镜一张静帧链接。"
    )
    required_nodes = ["plan_short_film"]
    output_keys = ("film_id",)

    input_schema = _obj("写镜", {
        "scenes": {"type": "object",
                   "description": "自定义分镜（`plan_short_film` 的 plan 原样传回）；"
                                  "不传就用 Store 里上游那一份"},
        "seconds": {"type": "number", "description": "目标时长（秒）；不传用上游的"},
        "voice": {"type": "boolean", "description": "是否配旁白（默认 true）"},
        "preview": {"type": "boolean",
                    "description": "true=额外回每镜一张静帧链接（多花约 30 秒）"},
    })

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        user_id, conv_id = self._ident(state)
        # `Store.get` 是 async 的（落点可能是 artifacts 表）——不 await 拿到的是
        # coroutine，`scenes` 就成了"有值但不可用"，报错会在很远的地方。
        upstream = inputs.get("plan") or (await state.store.get(self.required_nodes[0])) or {}
        scenes = inputs.get("scenes") or upstream.get("plan") or upstream
        if not scenes:
            raise ValueError("没有分镜：先调 plan_short_film，或在 scenes 里直接给一份")

        aspect = kernel.norm_aspect(inputs.get("aspect"))
        prompt = str(inputs.get("prompt") or state.user_request or "").strip()
        seconds = float(inputs.get("seconds")
                        or (upstream.get("total_sec") if isinstance(upstream, dict) else 0)
                        or 30.0)

        await kernel.ensure_storage()
        got = await showrunner.make(
            prompt, seconds=seconds, aspect=aspect,
            user_id=user_id, conversation_id=conv_id,
            voice=bool(inputs.get("voice", True)), scenes=scenes)

        film_id = project.new_id()
        try:
            project.save(film_id, code=got["code"], prompt=prompt, seconds=seconds,
                         aspect=aspect, fps=kernel.DEFAULT_FPS, plan=got["plan"],
                         secs=got["secs"], kinds=got["transitions"],
                         bodies=got["bodies"], audit=got.get("audit") or {},
                         coverage=got.get("coverage") or {}, calls=got["calls"],
                         rewritten=got.get("rewritten"),
                         fallback=got.get("fallback"), mode=got.get("mode") or "")
            (project.dir_of(film_id) / "film.tsx").write_text(got["code"],
                                                              encoding="utf-8")
        except OSError:
            # 落盘失败不影响继续渲，但改一镜就没工程可读了——如实说。
            film_id = film_id or ""

        out: dict[str, Any] = {
            "film_id": film_id,
            "title": str((got.get("plan") or {}).get("title") or ""),
            "shots": got["shots"],
            "secs": got["secs"],
            "total_sec": got["total_sec"],
            "calls": got["calls"],
            "rewritten": got.get("rewritten") or [],
            "audit": {"ratio": (got.get("audit") or {}).get("ratio", 0),
                      "bad": (got.get("audit") or {}).get("bad") or []},
            "variety": got.get("variety"),
        }
        if inputs.get("preview"):
            out["stills"] = await _publish_stills(got, aspect=aspect, user_id=user_id,
                                                 storage=self.storage)
        out["__model_view__"] = {k: v for k, v in out.items() if k != "secs"}
        return out


class RenderShortFilmNode(_ShortFilmNode):
    """按工程渲成片。**分钟级**——由 `LONG_RUNNING_NODES` 转成提交+轮询。"""

    name = "render_short_film"
    display_name = "渲染短片"
    description = (
        "按工程渲成片并合上音轨，回一条能直接播的链接。**不再调用模型**，所以不花钱，"
        "但要 5~30 分钟（纯 CPU 逐帧渲）。"
        "分钟级：本工具会先等一小段，没渲完就回 `status=queued/running`——"
        "**须继续调 `render_status`**（同一个 artifact_id）直到 done/failed 再收尾。"
    )
    required_nodes = ["write_short_shots"]
    output_keys = ("mp4_url",)
    # presigned 直链会过期：只回给调用方，不写进共享库（读侧按对象键现签）。
    ephemeral = ("mp4_url",)

    input_schema = _obj("渲染", {
        "film_id": {"type": "string",
                    "description": "工程 id；不传就用上游 `write_short_shots` 的那一个"},
    })

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        user_id, _ = self._ident(state)
        up = await state.store.get(self.required_nodes[0])
        film_id = str(inputs.get("film_id") or (up or {}).get("film_id") or "").strip()
        if not film_id:
            raise ValueError("没有工程：先调 write_short_shots，或传 film_id")
        proj = project.load(film_id)
        # 渲 + 合音轨 + 上传**只有一份实现**（`pipeline.render_and_publish`）：
        # 这段原来在 HTTP、原生工具、节点里各抄了一遍，改一处忘另一处。
        got = await pipeline.render_and_publish(
            film_id, storage=self.storage, user_id=user_id)
        return {
            **got,
            "title": str((proj.get("plan") or {}).get("title") or ""),
            "__model_view__": {k: got[k] for k in
                               ("film_id", "duration", "shots", "voice", "size_mb")},
        }


async def _publish_stills(got: dict[str, Any], *, aspect: str, user_id: str,
                          storage: Any, limit: int = 16) -> list[str]:
    """每镜一张静帧并签名。`preview=true` 时用——把人的判断放在渲染之前。"""
    import base64

    from ..simple import film as simple_film

    urls: list[str] = []
    tl = got.get("timeline") or {}
    n = int(got.get("shots") or 0)
    for i in range(min(n, limit)):
        try:
            blobs = await simple_film.shot_stills(got["code"], tl, i, aspect=aspect,
                                                  fps=kernel.DEFAULT_FPS,
                                                  at=(0.55,), scale=0.35)
        except Exception:  # noqa: BLE001 —— 这一镜出不来就跳过
            continue
        for b64 in (blobs or [])[:1]:
            try:
                pub = await deliver.publish_bytes(
                    base64.b64decode(b64), suffix="jpg", content_type="image/jpeg",
                    storage=storage, user_id=user_id)
                urls.append(pub["url"])
            except Exception:  # noqa: BLE001
                continue
    return urls
