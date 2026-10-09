# -*- coding: utf-8 -*-
"""元素命中表：把「画面上这一块」对回「spec 的哪一个字段」。

为什么要这张表：用户想改的是屏幕上看到的那个东西，而分镜 spec 是一棵 JSON 树。
中间这一层若不由机器建，就只能让用户去猜字段名——那是把排版系统的复杂度推给用户。

怎么建（两条口径决定了整个实现）：

* **框由浏览器量，不由我们算**。版式是 CSS 定位 + SVG viewBox 自适应 + ``--u`` 等比缩放
  三层叠出来的，我们在 Python 里算不出真实像素框（横屏的档案卡按自身像素落进画面带、
  图示卡按带高 ``meet`` 缩放后两侧留纸——两套规则都在 CSS 里）。所以让页面自己量：
  ``?probe=1`` 时把每个可见元素的 ``getBoundingClientRect`` 归一化到 ``.stage`` 倒出来。
  归一化之后，前端叠在播放器上的那一层与成片分辨率、与设计空间全都无关。
* **字段由文本反查，不靠版式构建器配合**。11 种卡型 + 内置图示 + 模型自画 SVG 全都要
  能选，若逐个构建器埋 ``data-field`` 就等于把命中能力绑死在"将来每加一种卡都得记得埋"
  上。反查规则是：按 role 圈定候选指针的范围（字幕只可能是 ``text``、面板值只可能是
  ``panel.*``、画面里的只可能在 ``visual``/``stamp``），再在这个范围里按文本比对。

比对必然有对不上的时候（版式会改写数字：``50`` 排成「50 米」、``count`` 动效排成
「约 5.8 万亿」）。这类条目**照实标 confidence="none"**，前端只给"改这一镜"的入口，
不给"直接改这个字段"——指错了字段比指不到字段严重得多。
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

from .. import mediaops

#: 一镜最多倒出这么多条目。模型自画的 SVG 可能有几百个文本节点，全存下来既没人看
#: 也把产物撑爆；超了就截断并在账上记一笔，不静默丢。
MAX_ENTRIES_PER_SHOT = 400

_PRE_RE = re.compile(r'<pre id="__hitmap__"[^>]*>(.*?)</pre>', re.DOTALL)
_ERR_RE = re.compile(r'<pre id="__hitmap_err__"[^>]*>(.*?)</pre>', re.DOTALL)


# ---------------------------------------------------------------------------
# 回读：跑一次 headless 的 --dump-dom，把探针倒出来的 JSON 取回来
# ---------------------------------------------------------------------------

def probe_shot(chrome: str, page: Path, win_w: int, win_h: int) -> dict[str, Any]:
    """一镜的页面 → 命中表。失败不抛：返回 ``{"error": …}``，让出片照跑。

    探针跑在**真正渲染的那一页**上（同一份字节，只是多个 ?probe=1），量的又是
    代表帧同一时刻（URL 没带 t 时页面自己缺省到 ``settle_ms``），所以框与落墨对得上。
    框一律归一化到 ``.stage``，于是与视口尺寸、与 ``--u`` 缩放无关——实测同一元素在
    1898×982 与 1920×1080 两种视口下归一化坐标一位不差，前端拿它乘成片宽高即可。
    代价是多一次浏览器启动（实测与一次截图同量级）。
    """
    from ..nodes.web_nodes import chrome_container_flags      # 延迟引：nodes 包比本模块重
    uri = page.resolve().as_uri() + "?probe=1"
    base = ["--disable-gpu", "--hide-scrollbars", "--no-first-run",
            *chrome_container_flags(),
            f"--window-size={win_w},{win_h}", "--virtual-time-budget=1200",
            "--dump-dom", uri]
    last = ""
    for headless in ("--headless=new", "--headless"):
        try:
            proc = mediaops.run([chrome, headless, *base], timeout=90)
        except Exception as exc:                              # noqa: BLE001 - 探针失败不改判渲染
            last = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        dom = proc.stdout or ""
        err = _ERR_RE.search(dom)
        if err:
            return {"error": f"探针脚本自己报了：{html.unescape(err.group(1))[:200]}"}
        m = _PRE_RE.search(dom)
        if not m:
            last = "页面里没有回读出命中表（探针没跑成或被 CSP 拦了）"
            continue
        try:
            body = html.unescape(m.group(1))
            # 前缀是给人在 dump 里认行的，JSON 本身不含它
            if body.startswith("HITMAP::"):
                body = body[len("HITMAP::"):]
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            return {"error": f"命中表不是合法 JSON：{exc}"}
        entries = list(payload.get("entries") or [])
        truncated = len(entries) > MAX_ENTRIES_PER_SHOT
        return {"shot": str(payload.get("shot") or ""),
                "settle_ms": payload.get("settle_ms"),
                "entries": entries[:MAX_ENTRIES_PER_SHOT],
                "truncated": truncated, "dropped": max(0, len(entries) - MAX_ENTRIES_PER_SHOT)}
    return {"error": f"两次 headless 都没回读到命中表：{last}"}


# ---------------------------------------------------------------------------
# 反查：条目文本 → spec 字段指针
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value if value is not None else ""))


def _seg(key: Any) -> str:
    """字典键 → JSON pointer 段（``~``→``~0``、``/``→``~1``）。"""
    return str(key).replace("~", "~0").replace("/", "~1")


def _walk(node: Any, pointer: str) -> list[tuple[str, str]]:
    """一棵子树 → [(指针, 文本)]，只收能显示成字的东西。"""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, val in node.items():
            out += _walk(val, f"{pointer}/{_seg(key)}")
    elif isinstance(node, list):
        for i, val in enumerate(node):
            out += _walk(val, f"{pointer}/{i}")
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        text = _norm(node)
        if text:
            out.append((pointer, text))
    return out


def _candidates(shot: dict[str, Any], index: int) -> dict[str, list[tuple[str, str]]]:
    """按 role 圈出各自的候选范围。圈窄比圈宽错得少。"""
    root = f"/shots/{index}"
    visual = shot.get("visual") or {}
    in_visual = _walk(visual, f"{root}/visual")
    panel = shot.get("panel") or {}
    stamp = shot.get("stamp") or {}
    return {
        "caption": [(f"{root}/text", _norm(shot.get("text")))] if shot.get("text") else [],
        "panel-value": [(f"{root}/panel/{_seg(k)}", _norm(v)) for k, v in panel.items()],
        "panel-key": [(f"{root}/panel/{_seg(k)}", _norm(k)) for k in panel.keys()],
        "label": [(f"{root}/label/{_seg(k)}", _norm(v))
                  for k, v in (shot.get("label") or {}).items()],
        "art-text": in_visual + _walk(stamp, f"{root}/stamp"),
        "art-block": in_visual + _walk(stamp, f"{root}/stamp"),
        "anim": in_visual + _walk(stamp, f"{root}/stamp"),
    }


def _match(entry_text: str, pool: list[tuple[str, str]],
           *, allow_partial: bool = True) -> tuple[str | None, str, bool]:
    """→ (指针, confidence, 是否歧义)。exact > contains(值被排进更长的一句) >
    contained(值只是画面文本的一段，多为版式加了单位/前缀)。

    ``allow_partial=False`` 留给容器：它倒出来的"文本"是子里所有字的拼接
    （「12020244602026」），按片段反查必然能蹭到某个字段——那种指认比指不到更糟。
    """
    if not entry_text:
        return None, "none", False
    exact = [p for p, text in pool if text == entry_text]
    if exact:
        return exact[0], "exact", len(exact) > 1
    if not allow_partial:
        return None, "none", False
    contains = [p for p, text in pool if text and text in entry_text]
    if contains:
        return contains[0], "contains", len(contains) > 1
    contained = [p for p, text in pool if text and entry_text in text]
    if contained:
        return contained[0], "contained", len(contained) > 1
    return None, "none", False


def resolve(shot: dict[str, Any], index: int, entries: list[dict[str, Any]]) -> None:
    """就地给每个条目补 ``field`` / ``confidence`` / ``ambiguous``。

    页面自己写明指针的那些条目（``data-fields`` → 探针带出 ``field``）**不参与反查**：
    它是模板逐字写下的地址，不是靠画面上的字猜出来的，所以恒为 exact。背景、色块、
    图片这些没有字的元素就靠这一条入口变得可点——只按文本反查的话它们永远选不中。
    """
    pool = _candidates(shot, index)
    for entry in entries:
        if entry.get("field"):
            entry["confidence"] = "exact"
            continue
        text = _norm(entry.get("text"))
        role = str(entry.get("role"))
        field, confidence, ambiguous = _match(
            text, pool.get(role) or [], allow_partial=role != "art-block")
        entry["field"] = field
        entry["confidence"] = confidence
        if ambiguous:
            entry["ambiguous"] = True


# ---------------------------------------------------------------------------
# 出口：给前端/存储用的紧凑形状
# ---------------------------------------------------------------------------

#: 同一块字被倒成多条时留哪条：越具体越靠前（能定到字段的优先）。
_ROLE_PRIORITY = ("caption", "style", "panel-value", "panel-key", "label",
                  "art-text", "anim", "art-block")


def _dedupe(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按「框 + 文本 + 字段」折叠重复条目 → (留下的条目, 被折叠掉的条数)。

    一个元素常常同时满足"是个块"和"有字"（印章的内层、带 data-anim 的数值），
    父节点还会把子的 textContent 回显一遍。留着它们，前端点一下就要问用户
    "这两个一模一样的框你要改哪个"。幸存者取角色更具体的那条（id 与 role 才
    对得上），另一条独有的字段并进来——anim 的元数据不能丢，改时长靠它。

    键里带上 field 是「整页可编辑」的前提：一条字幕的框和文本完全相同，却同时是
    文案、墨色、字号、字体四个可改的东西。不带 field 就会剩一条，而剩下那条
    的指针是文本——用户点中字号却没有输入框。
    """
    def rank(e: dict[str, Any]) -> int:
        try:
            return _ROLE_PRIORITY.index(str(e.get("role")))
        except ValueError:
            return len(_ROLE_PRIORITY)

    def key(e: dict[str, Any]) -> tuple:
        b = e.get("box") or {}
        return (round(float(b.get("x") or 0), 3), round(float(b.get("y") or 0), 3),
                round(float(b.get("w") or 0), 3), round(float(b.get("h") or 0), 3),
                str(e.get("text") or ""), str(e.get("field") or ""))

    kept: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    dropped = 0
    for entry in entries:
        k = key(entry)
        cur = kept.get(k)
        if cur is None:
            kept[k] = entry
            order.append(k)
            continue
        dropped += 1
        winner, loser = (entry, cur) if rank(entry) < rank(cur) else (cur, entry)
        for p, v in loser.items():
            winner.setdefault(p, v)
        kept[k] = winner
    return [kept[k] for k in order], dropped


def shot_table(shot: dict[str, Any], index: int, probed: dict[str, Any]) -> dict[str, Any]:
    """一镜的命中表（探针结果 + 字段回指 + 如实的失败说明）。"""
    if probed.get("error"):
        return {"shot": str(shot.get("id") or f"s{index + 1:02d}"),
                "entries": [], "error": probed["error"]}
    entries = [dict(e) for e in (probed.get("entries") or [])]
    entries, merged = _dedupe(entries)
    resolve(shot, index, entries)
    out = {"shot": str(shot.get("id") or f"s{index + 1:02d}"),
           "settle_ms": probed.get("settle_ms"), "entries": entries,
           "resolved": sum(1 for e in entries if e.get("field"))}
    if merged:
        out["merged"] = merged
    if probed.get("truncated"):
        out["truncated"] = int(probed.get("dropped") or 0)
    return out


def _segs(pointer: str) -> list[str]:
    """``/shots/0/panel/序号`` → ``["shots","0","panel","序号"]``，并解 JSON pointer 转义。

    必须先解转义再比对：面板键与图示标签里出现 ``/`` 时（「重量/体积」这类），
    不转义会把一个键拆成两段，于是指针落在一个不存在的中段上——或者更糟，
    恰好落在另一个存在的字段上，改错了地方还不报错。
    """
    raw = pointer.split("/")[1:]
    return [seg.replace("~1", "/").replace("~0", "~") for seg in raw]


def apply_patch(spec: dict[str, Any], pointer: str, value: Any) -> dict[str, Any]:
    """按 JSON 指针改 spec，返回**新对象**（不改入参——分镜是已入库的产物）。

    只认 ``/shots/<i>/...`` 这一种指针：命中表本来就只从这一棵子树里反查，指针越界
    说明构造方在瞎填，与其改到别处不如直接拒绝。
    """
    parts = _segs(pointer)
    if len(parts) < 3 or parts[0] != "shots":
        raise ValueError(f"指针不在镜头子树上，拒绝落笔：{pointer}")
    if not parts[1].isdigit():
        raise ValueError(f"指针里没有镜号：{pointer}")
    doc = copy.deepcopy(spec)
    shots = doc.get("shots") or []
    i = int(parts[1])
    if i >= len(shots):
        raise ValueError(f"指针指向不存在的第 {i + 1} 镜（本片只有 {len(shots)} 镜）：{pointer}")
    node: Any = shots[i]
    for seg in parts[2:-1]:
        if isinstance(node, list):
            node = node[int(seg)]
        elif isinstance(node, dict) and seg in node:
            node = node[seg]
        else:
            raise ValueError(f"指针中段不存在：{pointer}")
    leaf = parts[-1]
    if isinstance(node, list):
        node[int(leaf)] = value
    elif isinstance(node, dict):
        if leaf not in node:
            raise ValueError(f"指针末端不是已有字段（新增字段请走整镜改写）：{pointer}")
        node[leaf] = value
    else:
        raise ValueError(f"指针末端落在一个标量上：{pointer}")
    return doc


def read_pointer(spec: dict[str, Any], pointer: str) -> Any:
    """取指针当前值（属性表单要显示"现在是什么"）。不存在就抛，不静默回 None。"""
    parts = _segs(pointer)
    node: Any = spec
    for seg in parts:
        if isinstance(node, list):
            node = node[int(seg)]
        elif isinstance(node, dict) and seg in node:
            node = node[seg]
        else:
            raise KeyError(pointer)
    return node


def fingerprint(spec: dict[str, Any]) -> str:
    """这一版分镜的指纹——命中表必须知道自己是**哪一版** spec 量出来的。

    命中表与 spec 版本必须成对使用：用户对着旧表选了一块区域，而分镜在这之间被改过，
    按指针落笔就会改到别的字上。靠前端自律不管用（它会拿着查到的表就往里写），
    所以把指纹写进表里，局部改的那一头比对不上就拒收，让重新出片成为唯一出路。
    """
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
