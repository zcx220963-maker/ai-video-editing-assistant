# -*- coding: utf-8 -*-
"""指针工具：把「/shots/0/fields/glow/value」这一串地址落到 spec 树上。

三个函数，一条链在用（Remotion 代码链）：
* `code_patch` 拿 `_segs` 判形状、拿 `apply_patch` 落笔；
* `motion_code_nodes` 拿 `fingerprint` 把命中表与它量出来的那一版分镜绑成对。

指针一律只认 `/shots/<i>/...`：命中表本来就只从这棵子树里生成，越界说明构造方在
瞎填，与其改到别处不如直接拒绝。数组下标在代码链上没有意义（`fields` 按 name 寻址，
见 `code_patch` 的说明），这里不替它发明第二套地址。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


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


def fingerprint(spec: dict[str, Any]) -> str:
    """这一版分镜的指纹——命中表必须知道自己是**哪一版** spec 量出来的。

    命中表与 spec 版本必须成对使用：用户对着旧表选了一块区域，而分镜在这之间被改过，
    按指针落笔就会改到别的字上。靠前端自律不管用（它会拿着查到的表就往里写），
    所以把指纹写进表里，局部改的那一头比对不上就拒收，让重新出片成为唯一出路。
    """
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
