# -*- coding: utf-8 -*-
"""R3 素材源共用的失败回执（T10）：把「源不可达」和「源里没素材」分成两件事。

`asset_ingest.py`（入库）与 `probe_asset_sources.py`（可达性探测）是同一件事的两半，
两边都要回答同一个问题：**这次失败，代码还救不救得了**。所以判据只写一份，两边共用。

为什么「静默 0 条」是缺陷：跑完只看到「总计入库 0 个」，分不清是「这个源今天没有新
素材」（什么都不用做）还是「这个源今天把我拒了」（要换源或改 UA 策略）。每处失败都带
`fixable` 一栏把这句话说明白，回执就不再是可以被忽略的 stderr 噪声。
"""

from __future__ import annotations

import sys
import urllib.error

#: 本次运行里每一处「探测/下载失败」的结构化回执，由 `main_async` 落盘。
FAILURES: list[dict] = []


def reset_failures() -> None:
    FAILURES.clear()


def get_failures() -> list[dict]:
    return list(FAILURES)


def classify_failure(exc: BaseException) -> dict:
    """把一次探测/下载的异常翻成可解释的回执。

    `fixable` 的判法是「**这一段代码**还有没有救」，不是「这个源好不好」：
      - 400/404/410 → True（多半是我们拼的 URL/参数不对，改代码能救）
      - 401/403     → False（对面按 UA/风控拒了，改我们自己的代码救不了，只能换源/改 UA 策略）
      - 429         → False（限流，等；不是代码问题）
      - 5xx         → False（对端故障）
      - 超时/连接失败 → False（网络或对端）
      - 其余        → False（说不清就不许说「能救」，如实标未知）
    """
    code = None
    if isinstance(exc, urllib.error.HTTPError):
        code = int(exc.code)
        if code in (400, 404, 410):
            fixable, advice = True, "多半是 URL/参数拼错，改代码能救"
        elif code in (401, 403):
            fixable, advice = False, "对面按 UA/风控拒了，代码修不了，只能换源或改 UA 策略"
        elif code == 429:
            fixable, advice = False, "被限流，等一等再来；不是代码问题"
        elif 500 <= code < 600:
            fixable, advice = False, "对端故障，改我们这边没用"
        else:
            fixable, advice = False, f"HTTP {code}，按「不可由代码修」处理，如实登记"
    elif isinstance(exc, urllib.error.URLError):
        fixable, advice = False, f"连接层失败（{exc.reason}），网络或对端问题"
    elif isinstance(exc, (TimeoutError, OSError)):
        fixable, advice = False, "超时/IO 失败，网络或对端问题"
    else:
        fixable, advice = False, f"未分类异常（{type(exc).__name__}），按未知处理，不谎称能修"
    return {"ok": False, "kind": type(exc).__name__, "http_code": code,
            "fixable": fixable, "advice": advice, "error": str(exc)[:200]}


def record_failure(source: str, stage: str, exc: BaseException, *,
                   url: str = "", detail: str = "") -> dict:
    """记一条失败回执（并照旧打一行 stderr，人读的那份不丢）。回这条回执。"""
    rec = {"source": source, "stage": stage, "url": url, "detail": detail,
           **classify_failure(exc)}
    FAILURES.append(rec)
    tail = f"（{detail}）" if detail else ""
    print(f"[{source}] {stage}失败{tail}: {rec['error']} — {rec['advice']}",
          file=sys.stderr)
    return rec


def record_parse_failure(source: str, stage: str, *, url: str = "",
                         detail: str = "", advice: str = "") -> dict:
    """**站回来了 200、这条腿却一条都没解析出来**——单独一档，不并进上面两类。

    为什么不复用 `record_failure`：那个函数接的是异常，而这里没有异常，
    `classify_failure(ValueError(...))` 会给出「未分类异常、代码修不了」——那两句都是错的：
    ① 源是通的（它回了 200），建议「换源」是冤枉它；② 缺的是这个源里的件吗？不是，
    列表页有件，是**腿读不懂现在的站点**（字段改名、页面改成异步加载、URL 从相对变绝对），
    这**恰恰是改代码能救的那一档**。

    T52 的判据原话是「0 行且无失败不算绿」：那条判据今天要能判红，靠的就是这里。
    没有它，三条读错的腿会一直表现成「跑完了，总计入库 0 个素材」，看上去像「这个源没货」。
    """
    rec = {"source": source, "stage": stage, "url": url, "detail": detail,
           "ok": False, "kind": "EmptyParse", "http_code": 200, "fixable": True,
           "advice": advice or ("站点回了 200 但这条腿解析出 0 件：源是通的、货也在，"
                               "缺的是腿对站点现状的读法（字段名/页面结构/URL 形态），改代码能救"),
           "error": f"200 但 0 件（{detail or stage}）"}
    FAILURES.append(rec)
    print(f"[{source}] {stage} 解析出 0 件（源可达 200）: {detail} — {rec['advice']}",
          file=sys.stderr)
    return rec
