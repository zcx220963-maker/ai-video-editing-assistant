# -*- coding: utf-8 -*-
"""一句话出一条片子——**独立于分镜链**的最小内核。

这个包存在的理由只有一个：用户要的「填个 key、写一句话、出片」不该走 `plan_motion`
那条 20 多步的链。两条线的边界写在 `docs/设计-简洁链路-2026-10-24.md`：

- 本包**不许 import** `code_spec` / `nodes` / `storage.repositories`；
- 不产生 spec、不进 DAG、不进 `available_nodes`；
- 只留两道躲不掉的硬闸：lint（跑不起来）与时长上限（这是钱）。

边界由 `tests/test_simple_boundary.py` 钉住（随第一版一起落地）——那是可执行的，比文档管用。
"""
