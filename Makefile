# 一键体检的两个入口（T0.2）。Windows 上等价的是 `check.bat`——两个入口调同一条命令。
#
#   make check    全量体检 + 5 个像素探针
#   make fast     提交前那一步（与 .git/hooks/pre-commit 同一条命令）
#   make probe    只跑探针（容器不在记 UNVERIFIED，不算红）
#   make hooks    把 tools/hooks/ 装进 .git/hooks/
#
# PY 可以覆盖：make check PY=/path/to/python（必须是能 import pytest 的那个解释器，
# selfcheck 内部用 `sys.executable -m pytest`）。

PY ?= python

.PHONY: check fast probe hooks

check:
	$(PY) selfcheck.py
	$(PY) selfcheck.py --probe

fast:
	$(PY) selfcheck.py --fast

probe:
	$(PY) selfcheck.py --probe

hooks:
	$(PY) tools/hooks/install_hooks.py
