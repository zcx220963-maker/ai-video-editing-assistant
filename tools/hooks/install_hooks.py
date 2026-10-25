#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把仓库里的钩子装进 `.git/hooks/`（T0.2）。

为什么要有这个脚本：`.git/hooks/` 不入库，所以「提交前跑体检」这件事如果不写成
一份**可重复安装**的源文件，换一台机器克隆下来就没了——而体检的价值全在「每次都有」。

用法：
    python tools/hooks/install_hooks.py            # 装
    python tools/hooks/install_hooks.py --check    # 只检查装没装（CI/自检用）
    python tools/hooks/install_hooks.py --remove   # 卸

它只做一件事：把 `tools/hooks/<name>` 拷到 `.git/hooks/<name>` 并给可执行位。
**不覆盖别人的钩子**：目标已存在且内容不是我们的，先备份成 `<name>.bak`。
"""
from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tools" / "hooks"
DST = ROOT / ".git" / "hooks"
HOOKS = ("pre-commit",)
MARK = "selfcheck.py --fast"


def installed(name: str) -> bool:
    dst = DST / name
    try:
        return MARK in dst.read_text("utf-8", errors="replace")
    except OSError:
        return False


def install(name: str) -> str:
    src, dst = SRC / name, DST / name
    if not src.exists():
        return f"{name}: 源文件不存在（{src}）"
    DST.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not installed(name):
        bak = dst.with_suffix(dst.suffix + ".bak")
        shutil.copy2(dst, bak)
        note = f"（原钩子已备份到 {bak.name}）"
    else:
        note = ""
    shutil.copy2(src, dst)
    mode = dst.stat().st_mode
    dst.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"{name}: 已安装{note}"


def main(argv: list[str]) -> int:
    if not DST.parent.exists():
        print(f"这里不是 git 仓库（找不到 {DST.parent}）")
        return 1
    if "--check" in argv:
        missing = [n for n in HOOKS if not installed(n)]
        for n in HOOKS:
            print(f"{'ok ' if installed(n) else 'XX '}{n}")
        return 1 if missing else 0
    if "--remove" in argv:
        for n in HOOKS:
            dst = DST / n
            if dst.exists() and installed(n):
                dst.unlink()
                print(f"{n}: 已卸载")
            else:
                print(f"{n}: 没装（或不是我们的），不动")
        return 0
    for n in HOOKS:
        print(install(n))
    print("\n现在每次 git commit 前会跑一次快速体检（约 1~2 分钟，超 90 秒只警告）。")
    print("全量体检：python selfcheck.py（约 15~20 分钟）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
