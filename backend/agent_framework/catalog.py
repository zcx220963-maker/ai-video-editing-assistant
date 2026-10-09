"""机器名 → 中文名的**单汇词表**与全局出口替换器（块 A）。

为什么不是「让 LLM 说中文」：模型可以把 ``split_shots`` 写成「镜头切分」，也就可以
不写；靠提示词守一条界面文案规则，等于把契约交给概率。这里全程无 LLM：
词表在装配期由三处声明汇总（见 ``tests/test_display_name_sources.py``），
出口处按名字整词替换。

**双轨而不是改写**：机器名是键，不是文案——
``store:{node}`` 是状态总线键、``artifacts.node`` 是产物表主键的一部分、
DAG 契约的 dict 键、fork 的 ``rerun_nodes``、前端 ``PIPE_ORDER`` 的比对值。
把它们换成中文会连带打断复用/作废/排序。所以：

* 键在 ``KEEP_KEYS`` 里 → 原值保留，同一层挂一条 ``{键}_display`` 给人看，
  前端读 ``*_display``、机器名只用于匹配；
* 键在 ``OPAQUE_KEYS`` 里（文件名、对象键、链接、id）→ 那是**用户内容**，
  整条不动：拖进来叫 ``render_video.mp4`` 的文件必须原名还回去；
* 其余字符串 → 就地文本替换（自由文本里机器名不该出现）。

参数位（``material_ids`` 这类**键名**）走第四种处理：不进文本替换——它们是普通词，
进候选会把正文里的「按 name 排序」也换掉。单源在 ``PARAM_LABELS``，出帧时整帧挂一条
``arg_labels``，前端把它当第一来源、本地表退为兜底。

文本替换的三条边界，写死、不做「部分替部分不替」：
1. **整词**：前后不能贴 ASCII 标识符字符或路径分隔符，所以 ``store:split_shots``、
   ``/x/split_shots.mp4``、``grep.log`` 都不会被误伤；大小写敏感，正文里的
   ``ASR`` 不会被当成节点 ``asr``。
2. **长名优先**：``plan_timeline_pro`` 必须排在 ``plan_timeline`` 之前试，
   否则短名会把长名咬掉一半。
3. **代码段与链接不替**：反引号 / 三反引号包住的内容、URL 与本地路径原样送出——
   排障要看得到真名，界面上配复制按钮。

流式走 ``StreamRewriter``：``split_sho`` + ``ts`` 分两帧到达时第一帧不许把残片
发出去（扣到能判定为止），代码段状态跨帧续着判，run 结束必须 ``flush()`` 兜底。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

__all__ = ["ToolCatalog", "StreamRewriter", "KEEP_KEYS", "OPAQUE_KEYS",
           "PARAM_LABELS", "get_catalog", "set_catalog"]

# 值本身是机器名（或机器名列表）的键：保留原值，另挂 *_display。
KEEP_KEYS = frozenset({
    "tool", "tools", "node", "nodes", "rerun_nodes", "source", "sources",
    "requires", "required_nodes", "upstream", "downstream",
    "skill", "skills", "skills_hint", "stage", "provider",
    # /tools、产物行、计划卡都拿 name 装机器名：整值查表，
    # 用户文件名（带后缀、不是任何工具名）查不到，自然原样留着。
    "name", "names",
})

# 值是用户内容或标识符的键：整条不替。
OPAQUE_KEYS = frozenset({
    "file_name", "filename", "original_name", "object_key", "key", "keys",
    "path", "paths", "url", "urls", "media_url", "session_id", "run_id",
    "artifact_id", "material_id", "material_ids", "id", "ids",
    "conversation_id", "user_id", "topic", "prompt",
})

_HEAD = re.compile(r"[A-Za-z0-9_.:/\\$\-]")       # 名字前面不许贴这些
_TAIL_CH = re.compile(r"[A-Za-z0-9_\-.]")          # 名字后面不许贴这些
_FENCE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_URL = re.compile(r"(?:https?|ftp|file|ws|wss)://[^\s<>\"'）)，。；、]+")
_MAX_DEPTH = 24
# 流式里代码段迟迟不闭合时的兜底：扣了这么多字还没结束就当普通文本发出去
_STREAM_HOLD_LIMIT = 4096

# 参数名 → 中文短标签：**单源在这张表**，前端那份同名表退为兜底（词表没装上时用）。
# 为什么不并进 _displays：参数名大多是普通词（name / key / path / task / mode），
# 进文本替换的候选就把正文里这些词也换掉了——「按 name 排序」变成「按 名称 排序」。
# 参数只作为**键位**出现，所以另建一张查表，在出帧那一步整帧挂一条 arg_labels。
PARAM_LABELS: dict[str, str] = {
    # 身份与作用域（每个剪辑节点都声明这四个公共参数）
    "session_id": "服务端会话", "artifact_id": "产物",
    "user_id": "用户", "conversation_id": "会话", "user_request": "原始诉求",
    # 素材与内容
    "material_id": "素材", "material_ids": "素材", "media": "素材",
    "clips": "镜头", "shot": "镜头", "shots": "镜头", "clip": "待剪片段",
    "groups": "分组", "script": "文案", "custom_script": "指定文案",
    "bgm": "配乐", "voiceover": "配音", "timeline": "时间线",
    "overlay_events": "画面覆盖层", "dry_run": "只出计划不渲染",
    "corrections": "修字表",
    "highlight": "高光", "keep_original_audio": "保留原声",
    "keep_segments": "保留片段", "target_duration_sec": "目标时长（秒）",
    "wait_sec": "等待时长（秒）", "duration": "时长（秒）",
    # 图形科普片（零素材出片）：这批键会原样出现在计划卡与工具气泡里
    "spec": "分镜", "style": "版式", "aspect": "画幅", "fps": "帧率",
    "narration": "人声朗读", "voice": "音色", "rate": "语速",
    "subtitle_mode": "字幕形态", "title": "标题",
    # 检索与读写
    "query": "关键词", "text": "文本", "key": "键名", "pattern": "检索式",
    "glob": "文件通配", "path": "路径", "paths": "路径", "content": "内容",
    "old_string": "原文", "new_string": "替换为", "replace_all": "全部替换",
    "offset": "起始位置", "limit": "返回上限", "max_results": "条数上限",
    "max_bytes": "大小上限", "case_insensitive": "忽略大小写",
    "url": "链接", "urls": "链接", "name": "名称", "kind": "类型",
    "category": "分类", "mode": "模式",
    # 团队 / 子 Agent / 定时
    "task": "任务", "result": "结论", "context": "背景", "prompt": "任务说明",
    "allow_tools": "允许工具", "owner": "归属", "to": "发送给", "from": "来自",
    "run_at": "执行时间", "every_seconds": "间隔（秒）",
    "delete_after_run": "跑完即删", "task_id": "任务", "items": "条目",
    # 计划门（submit_plan 的入参会原样出现在工具气泡里）
    "goal": "目标", "steps": "步骤", "plans": "计划", "plan_id": "计划",
    "node": "节点", "nodes": "节点", "seq": "序号", "label": "标签",
    "display": "显示名", "value": "取值", "options": "选项",
    "param_options": "参数选项", "why": "理由", "reason": "原因",
    "expectation": "预期", "skills_hint": "技能提示", "skill": "技能",
    "skills": "技能", "run_id": "运行", "instruction": "指令",
}


class ToolCatalog:
    """一张词表 + 三种出口形状（文本 / 对象 / 流）的替换器。空表 = 全程原样。"""

    def __init__(self, displays: Mapping[str, str] | None = None,
                 params: Mapping[str, str] | None = None) -> None:
        self._displays: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None
        self._rev: dict[str, str] = {}
        # 参数标签不随 Storyline 连不连上变化：单源表始终在场（显式传 {} 才是真空表）
        self._params: dict[str, str] = dict(PARAM_LABELS if params is None else params)
        if displays:
            self.update(displays)

    # ---- 词表 ----

    def update(self, displays: Mapping[str, str]) -> None:
        """并入一批 {机器名: 中文名}；空中文名或原样抄写的忽略（未声明即退回机器名）。"""
        dirty = False
        for name, disp in (displays or {}).items():
            name, disp = str(name or ""), str(disp or "")
            if not name or not disp or name == disp:
                continue
            if self._displays.get(name) != disp:
                self._displays[name] = disp
                if disp in self._rev and self._rev[disp] != name:
                    # 两个机器名撞同一个中文：反向表留着会歧义，直接不建
                    self._rev.pop(disp, None)
                else:
                    self._rev[disp] = name
                dirty = True
        if dirty:
            self._build()

    def add(self, name: str, display: str) -> None:
        self.update({name: display})

    def _build(self) -> None:
        if not self._displays:
            self._pattern = None
            return
        # 长名在前：re 的择一是「先匹配先赢」，短名在前会把长名咬断。
        alts = sorted((re.escape(n) for n in self._displays),
                      key=lambda s: (-len(s), s))
        # 括起来是必须的：`(?<!x)A|B(?!y)` 的边界只各自管到第一/最后一个分支。
        self._pattern = re.compile(
            rf"(?<!{_HEAD.pattern})(?:" + "|".join(alts) + rf")(?!{_TAIL_CH.pattern})")

    @property
    def names(self) -> list[str]:
        return list(self._displays)

    def __bool__(self) -> bool:
        return bool(self._displays)

    def display(self, name: str) -> str:
        """这条机器名的中文名；没声明过回空串（调用方自行退回机器名）。"""
        return self._displays.get(str(name), "")

    def label(self, name: str) -> str:
        """给人看的那一版：查不到就原样给回（宁可露真名，也不编一个）。"""
        return self._displays.get(str(name), str(name))

    def machine_name(self, display: str) -> str:
        """反查：中文 → 机器名（用户按计划卡选项回填参数时用）。查不到回空串。"""
        return self._rev.get(str(display), "")

    def displays(self) -> dict[str, str]:
        return dict(self._displays)

    # ---- 参数标签（键位的中文，不碰值） ----

    def param_label(self, name: str) -> str:
        """参数名的中文短标签；没声明过原样给回（宁可露真名，也不编一个）。"""
        return self._params.get(str(name), str(name))

    def params_display(self) -> dict[str, str]:
        return dict(self._params)

    def arg_labels(self, arguments: Any) -> dict[str, str] | None:
        """一次调用的入参 → {参数名: 中文标签}，只收查得到的；一个都没有回 None。

        入参可能是 dict，也可能是工具帧里那条已经序列化的 ``arguments`` JSON 文本：
        两种都认，解析不了就当没有参数（不出错、也不阻塞出帧）。
        """
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                return None
        if not isinstance(arguments, dict):
            return None
        got = {str(k): self._params[str(k)] for k in arguments if str(k) in self._params}
        return got or None

    # ---- 出口 1：自由文本 ----

    def rewrite_text(self, text: str) -> str:
        if not text or self._pattern is None:
            return text
        spans: list[str] = []

        def _mask(rx: re.Pattern[str], seg: str) -> str:
            def _sub(m: re.Match[str]) -> str:
                spans.append(m.group(0))
                return f"\x00{len(spans) - 1}\x00"
            return rx.sub(_sub, seg)

        seg = _mask(_FENCE, text)
        seg = _mask(_URL, seg)
        seg = self._pattern.sub(lambda m: self._displays[m.group(0)], seg)
        return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], seg)

    # ---- 出口 2：结构化对象 ----

    def rewrite_obj(self, payload: Any, *, _key: str = "", _depth: int = 0) -> Any:
        """递归出一个**新**对象：键位决定「保机器名挂旁路」/「不动」/「换中文」。"""
        if _depth > _MAX_DEPTH:
            return payload
        if isinstance(payload, dict):
            out: dict[str, Any] = {}
            extra: dict[str, Any] = {}
            for k, v in payload.items():
                ks = str(k)
                if k in OPAQUE_KEYS:
                    out[k] = v
                elif k in KEEP_KEYS:
                    # 这一位的值就是键本身：原样留着，中文名另挂旁路
                    out[k] = v
                    label = self._labels_of(v)
                    if label is not None:
                        extra[f"{ks}_display"] = label
                else:
                    out[k] = self.rewrite_obj(v, _key=ks, _depth=_depth + 1)
            for k, v in extra.items():
                out.setdefault(k, v)
            return out
        if isinstance(payload, (list, tuple)):
            return [self.rewrite_obj(x, _key=_key, _depth=_depth + 1)
                    for x in payload]
        if isinstance(payload, str) and _key not in OPAQUE_KEYS:
            return self.rewrite_text(payload)
        return payload

    def _labels_of(self, value: Any) -> Any:
        """KEEP_KEYS 侧的中文名：单值 → 字符串，列表 → 等长列表，全未声明 → None。"""
        if isinstance(value, str):
            return self.display(value) or None
        if isinstance(value, (list, tuple)):
            got = [self.display(v) if isinstance(v, str) else "" for v in value]
            if not any(got):
                return None
            return [g or (v if isinstance(v, str) else "")
                    for v, g in zip(value, got)]
        return None

    # ---- 出口 3：名单 → 带标签的结构 ----

    def labeled(self, items: Iterable[str]) -> list[dict[str, str]]:
        """[机器名] → [{key, label}]：计划卡、工具链角标这类结构吃这个形状。"""
        return [{"key": n, "label": self.label(n)} for n in items]


class StreamRewriter:
    """一条流式连接的挂起缓冲：残片不发、代码段跨帧也不误替、收尾 flush 兜底。

    词表共享、状态不共享——每个连接各 new 一个。
    """

    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog
        self._buf = ""
        self._state = "text"        # text / fence / inline

    @property
    def buffered(self) -> str:
        return self._buf

    def feed(self, chunk: str) -> str:
        if not self._catalog:
            out, self._buf = self._buf + chunk, ""
            return out
        self._buf += chunk
        parts: list[str] = []
        i = 0
        n = len(self._buf)
        while i < n:
            if self._state == "text":
                j = self._buf.find("`", i)
                if j < 0:
                    # 没有代码标记：可发到底，只是帧尾可能压着半个机器名
                    safe = n - self._holdback(self._buf[i:])
                    if safe <= i:
                        break
                    parts.append(self._catalog.rewrite_text(self._buf[i:safe]))
                    i = safe
                    break
                # 名字后面紧跟反引号时边界已定，不必扣；只需发到标记之前
                if j > i:
                    parts.append(self._catalog.rewrite_text(self._buf[i:j]))
                    i = j
                if j + 1 >= n:
                    break                   # 帧尾一个孤立反引号：等下一帧再判
                if self._buf[j:j + 3] == "```":
                    parts.append("```")
                    i = j + 3
                    self._state = "fence"
                elif j + 2 >= n:
                    break                   # 可能是被切断的 ```，扣住
                else:
                    parts.append("`")
                    i = j + 1
                    self._state = "inline"
            elif self._state == "fence":
                k = self._buf.find("```", i)
                if k < 0:
                    if n - i > _STREAM_HOLD_LIMIT:
                        parts.append(self._buf[i:])
                        i = n
                    break
                parts.append(self._buf[i:k + 3])
                i = k + 3
                self._state = "text"
            else:                        # inline：反引号成对，遇换行就当普通文本
                k = self._buf.find("`", i)
                nl = self._buf.find("\n", i)
                if k < 0 and nl < 0:
                    if n - i > _STREAM_HOLD_LIMIT:
                        parts.append(self._buf[i:])
                        i = n
                    break
                if 0 <= nl < (k if k >= 0 else n + 1):
                    parts.append(self._buf[i:nl + 1])
                    i = nl + 1
                    self._state = "text"
                else:
                    parts.append(self._buf[i:k + 1])
                    i = k + 1
                    self._state = "text"
        self._buf = self._buf[i:]
        return "".join(parts)

    def flush(self) -> str:
        """流结束：扣着的尾巴发出去。断在代码段里就原样，否则按文本规则换完。"""
        out, self._buf = self._buf, ""
        in_code = self._state != "text"
        self._state = "text"
        if not out or in_code or self._catalog._pattern is None:
            return out
        return self._catalog.rewrite_text(out)

    def _holdback(self, seg: str) -> int:
        """段尾有多少字符可能是「还没写完的机器名」——扣住，等下一帧再判。"""
        hold = 0
        for name in self._catalog._displays:
            for k in range(min(len(name), len(seg)), 0, -1):
                if seg[-k:] != name[:k]:
                    continue
                start = len(seg) - k
                if start > 0 and _HEAD.match(seg[start - 1]):
                    break               # 前面贴着标识符字符，永远长不成这个名字
                hold = max(hold, k)
                break
        return hold


# ---- 装配期填、出口处读：不把 catalog 一路透传进每个 handler ----

_catalog = ToolCatalog()


def get_catalog() -> ToolCatalog:
    return _catalog


def set_catalog(catalog: ToolCatalog) -> ToolCatalog:
    global _catalog
    _catalog = catalog
    return catalog
