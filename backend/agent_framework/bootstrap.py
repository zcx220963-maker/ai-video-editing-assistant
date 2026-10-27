"""Bootstrap Files：启动期就进 system 的那几段说明（设计 §12）。

与 Skill 的区别：Skill 是**按需加载**的能力包；Bootstrap 是**每轮都在**的底座
（身份、工作规矩、视频规矩）。它们不参与触发判断，所以放在 system 的最前面，
且只读一次盘、按内容指纹缓存。

为什么单独一个模块而不是塞进 context.py：读盘/编码/截断这三件事跟上下文装配
没关系，装配侧只该拿到「一段文本」。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger("agent_framework")

#: 所有 bootstrap 文件合计的字节上限。超了只截断并告警——底座不能把上下文吃光。
MAX_BYTES = 16_000

_cache: dict[str, tuple[str, str]] = {}   # path -> (fingerprint, text)


def load_bootstrap(directory: str | Path | None) -> str:
    """读 `directory/*.md`（按文件名排序）拼成一段；目录不存在返回空串。"""
    if not directory:
        return ""
    root = Path(directory)
    if not root.is_dir():
        return ""
    parts: list[str] = []
    used = 0
    for path in sorted(root.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("bootstrap 文件读不出来：%s", path)
            continue
        if not text.strip():
            continue
        fp = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        cached = _cache.get(str(path))
        if cached and cached[0] == fp:
            text = cached[1]
        else:
            _cache[str(path)] = (fp, text)
        block = f"<bootstrap file=\"{path.name}\">\n{text.strip()}\n</bootstrap>"
        if used + len(block.encode("utf-8")) > MAX_BYTES:
            logger.warning("bootstrap 超预算（%d B），已截断：%s", MAX_BYTES, path.name)
            break
        parts.append(block)
        used += len(block.encode("utf-8"))
    return "\n\n".join(parts)
