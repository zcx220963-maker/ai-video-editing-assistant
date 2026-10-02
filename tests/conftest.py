"""pytest 与仓库既有测试形态之间的桥。

背景（这是历史包袱，不是谁写错了用例）：

``tests/test_*.py`` 共 53 个文件，**全部**是自带 ``asyncio.run(main())`` 的独立脚本，
用自己的 ``check()`` 记断言数、用退出码报结果；一个真正的 pytest 用例也没有。
原始运行方式是 ``.tmp/run_all_tests.py``（子进程逐个跑）。

但 ``pytest.ini`` 里写着 ``testpaths = tests`` —— 于是 ``python -m pytest``
会去收集这 53 个脚本，把里面的 ``async def`` 判成「缺 async 插件」、把 ``tmp``
参数判成「fixture 不存在」，结论一片红。**命令能跑，结论全是假故障**：
既掩盖真实回归，也让人不敢用标准入口。

这里保留两条路都能走：

- ``python .tmp/run_all_tests.py``：原样，子进程逐个跑，结果进 ``.tmp/reg/*.log``。
- ``python -m pytest``：本文件把每个脚本收成一个用例，用同样的方式执行，
  退出码非 0 就在报告里带上该脚本的输出尾部，全量输出落 ``.tmp/reg_pytest/``。

实现说明（踩过的坑，别改回去）：

``pytest_collect_file`` **不是** firstresult hook——每个插件的返回值都会被挂到收集树上。
本插件返回 ``ScriptFile`` 的同时，pytest 内置收集器也返回了 ``Module``，
于是同一个文件既出现 5 个 async 用例、又出现 1 个脚本用例。
在 ``pytest_pycollect_makemodule`` 里返回 ``None`` 想挡掉 ``Module`` 也无效：
该 hook 经 ``hookproxy(file_path)`` 调用，子目录 conftest 的实现不参与。
所以这里不跟收集器较劲，改在收集完成之后**筛掉**那些注定假故障的用例：
只留 ``ScriptItem``，其余（脚本内部的 async def / 需要 tmp fixture 的函数）一律丢。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ROOT = Path(_ROOT)
LOG_DIR = ROOT / ".tmp" / "reg_pytest"
TIMEOUT_SEC = 420


def pytest_collect_file(file_path: Path, parent: pytest.Collector):
    """为每个 ``tests/test_*.py`` 挂一个「执行整份脚本」的收集器。"""
    if file_path.suffix != ".py" or not file_path.name.startswith("test_"):
        return None
    return ScriptFile.from_parent(parent, path=file_path)


def pytest_collection_modifyitems(config, items):
    """只留脚本级用例，丢掉 pytest 从脚本里解析出的函数级用例。

    被丢掉的那些正是「async def functions are not natively supported」与
    「fixture 'tmp' not found」的来源——它们是这个仓库的**假故障**，不是真回归。
    """
    keep = [it for it in items if isinstance(it, ScriptItem)]
    dropped = len(items) - len(keep)
    items[:] = keep
    if dropped:
        print(f"\n[tests] 已忽略 {dropped} 个由脚本内部函数解析出的假用例"
              f"（脚本本身以子进程整份执行）", flush=True)


class ScriptFile(pytest.File):
    def collect(self):
        yield ScriptItem.from_parent(self, name=self.path.name)


class ScriptItem(pytest.Item):
    """一个用例 = 一次子进程执行整份脚本（与 .tmp/run_all_tests.py 同语义）。"""

    def runtest(self) -> None:
        script = Path(str(self.path))
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=TIMEOUT_SEC, cwd=str(ROOT), env=env,
            )
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"{script.name} 超过 {TIMEOUT_SEC}s 未结束——多半在等外部服务或死锁。"
            ) from None

        out = (proc.stdout or "") + (proc.stderr or "")
        # 全量输出落盘；失败时只把尾部带回报告——脚本自己的 print 是主要可读信息源。
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"{script.stem}.log").write_text(out, encoding="utf-8")
        if proc.returncode != 0:
            tail = "\n".join(out.strip().splitlines()[-60:])
            raise AssertionError(
                f"{script.name} 退出码 {proc.returncode}"
                f"（完整输出 .tmp/reg_pytest/{script.stem}.log）\n{tail}"
            )

    def repr_failure(self, excinfo, style=None):
        return str(excinfo.value)

    def reportinfo(self):
        return self.path, 0, f"script::{self.name}"
