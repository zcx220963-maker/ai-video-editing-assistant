# -*- coding: utf-8 -*-
"""交付卫生：把「写了但没人接」的孤儿变成会红的断言。

为什么要有这个文件：这些缺陷的共性不是「代码错」，而是**没人读**——一个文件、
一个前缀、一个能力，建了、提交了、看起来能用，但链路上没有任何一处引用它。
这类东西不会被任何功能测试抓到（功能测试只走被引用的那条路），只会在下一次
人肉体检时被想起来。所以判据写成「孤儿不存在」，而不是「功能可用」。

五条（R0-T13）：
  ① `remotion/src/themes/styles.json` —— 语义已并入 palettes.json 的 preset，文件该消失；
  ② `agent_framework/storage/migrations/` 目录 —— 执行口只读 `migrations.sql`，目录无人读；
  ③ `remotion/public/` —— `staticFile` 的可落之地（字体走它：`loadFont` 强制
     `staticFile('fonts/…')`），缺它则字体读不到、镜头代码连 `import` 都过不去；
     ④ `session_gc.OBJECT_PREFIXES` 不含 `assets/` —— 库素材**故意**不 GC（Q19），
        用断言钉住，免得将来「顺手补全」把全局资产删了；
     ⑤ `remotion/` 的 `@remotion/*` 依赖必须既被声明又被装载，且许可在白名单内。

⑥（2026-10-16，§6.1）把这一轮**删掉的**三处「写了但没人接」钉住，不叫它们长回来：
  ⑥a `Agent` 自带的进程内队列（`submit` / `run_forever` / `stop`）——生产外层循环只有
     MQ 那条（`SessionConsumer`）；两套并存时那套没人 await 的 `create_task` 会把
     失败吞成一句 log，而调用方永远等不到结果；
  ⑥b 会话键的第二份拼法——`{user}:{conv}` 只许由 `identity.session_key` 拼出，
     各拼一份就会「查得到历史、查不到产物」而没有任何报错；
  ⑥c `ContextBuilder` 的 Bootstrap 注入层——`bootstrap_dir` 生产从来没传过（也正是
     prompts.py 开头点名的那个教训），它想干的事已由提示词库（带清单自检）接走。

跑法：`python tests/test_delivery_hygiene.py`（容器内 `PYTHONPATH=/app`）。
依赖 `node_modules` 的那半在缺目录时**记未验证并列出**，不折算成通过。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import json
import os
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_checks = 0
_fails = 0
_unverified: list[str] = []


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _remotion_dir() -> Path | None:
    """定位 remotion/ 目录：容器里是 /app/remotion，工作树里是仓库根的 remotion/。"""
    env = os.environ.get("REMOTION_DIR")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    backend = Path(__file__).resolve().parents[1]
    for cand in (backend / "remotion", backend.parent / "remotion"):
        if cand.is_dir():
            return cand
    return None


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_full_tree(remo: Path) -> bool:
    """这个 remotion/ 是不是**完整源码树**（而不是 app 镜像里那份 COPY 子集）？

    app 镜像只 COPY 了 `lint-rules.mjs/lint.mjs/motionsample.mjs/package.json/
    server.mjs/tsconfig.json` 与 `src/tokens.ts`、`src/FilmLayout.tsx`、
    `src/themes/palettes.json`、`src/preview/caption-sample.tsx`（Dockerfile），
    所以它**没有** `src/demos`、`src/index.ts`、`tests/`。
    拿这份子集判「某个文件在不在」会得到**假绿**（文件根本没被 COPY 进来）——
    假绿比不报更坏。所以先认「完整树」：认不出就把该条记成未验证。
    """
    return (remo / "src" / "FilmLayout.tsx").is_file() and (remo / "server.mjs").is_file()


# ---------------------------------------------------------------------------
# ① styles.json 该消失
# ---------------------------------------------------------------------------
def test_styles_json_orphan(remo: Path | None) -> None:
    print("\n=== ① remotion/src/themes/styles.json 不再是无消费者的孤儿 ===")
    if remo is None:
        _unverified.append("① remotion/ 目录不可达")
        return
    if not _is_full_tree(remo):
        _unverified.append(f"① remotion/ 不是完整源码树（{remo}）——孤儿判定未验证")
        return
    styles = remo / "src" / "themes" / "styles.json"
    palettes = remo / "src" / "themes" / "palettes.json"
    check(palettes.is_file(), f"唯一来源 palettes.json 在（{palettes}）")
    # 语义已并入 palettes.json 的 preset（R4-T8-1）。文件还在 = 孤儿还在。
    check(not styles.is_file(),
          f"styles.json 已删除（三档预设语义并入 palettes.json 的 preset）：{styles}")


# ---------------------------------------------------------------------------
# ② storage/migrations/ 目录该消失
# ---------------------------------------------------------------------------
def test_migrations_dir_orphan() -> None:
    print("\n=== ② agent_framework/storage/migrations/ 目录不再是无读取的孤儿 ===")
    d = _backend_dir() / "agent_framework" / "storage" / "migrations"
    check(not d.exists(),
          f"migrations/ 目录已废弃（内容并入 migrations.sql，唯一执行口）：{d}")
    sql = _backend_dir() / "agent_framework" / "storage" / "migrations.sql"
    check(sql.is_file(), f"migrations.sql 在（唯一执行口）：{sql}")


# ---------------------------------------------------------------------------
# ③ remotion/public 该存在
# ---------------------------------------------------------------------------
def test_public_dir_present(remo: Path | None) -> None:
    print("\n=== ③ remotion/public/ 存在（staticFile 的可落之地） ===")
    if remo is None:
        _unverified.append("③ remotion/ 目录不可达")
        return
    if not _is_full_tree(remo):
        _unverified.append(f"③ remotion/ 不是完整源码树（{remo}）——public/ 判定未验证")
        return
    pub = remo / "public"
    check(pub.is_dir(), f"public/ 目录存在：{pub}")
    # 2026-10-16：这一条原来还判 `public/assets/`（T4 的按镜投递目标）。上传件不上屏
    # 那条腿拆掉后，运行期不再往 `public/` 里投任何东西，`public/assets/` 与它的
    # `.gitkeep` 一并出库；`public/` 里只剩随仓库发的 `fonts/`。
    # 判据跟着收：**留下的是「staticFile 的落点还在」这件事本身**，而不是那个空目录。
    check((pub / "fonts").is_dir(),
          f"public/ 里那份随仓库发的字体还在（staticFile 的真正落点）：{pub / 'fonts'}")


# ---------------------------------------------------------------------------
# ④ OBJECT_PREFIXES 故意不含 assets/
# ---------------------------------------------------------------------------
def test_assets_prefix_not_gc() -> None:
    print("\n=== ④ session_gc.OBJECT_PREFIXES 不含 assets/（Q19：库素材不随会话删） ===")
    from agent_framework import session_gc

    prefixes = tuple(getattr(session_gc, "OBJECT_PREFIXES", ()))
    check(bool(prefixes), f"OBJECT_PREFIXES 读得到（{prefixes}）")
    check("assets/" not in prefixes,
          "assets/ 不在会话回收前缀里——全局库素材一旦被引用计数误判就是不可逆损失")


# ---------------------------------------------------------------------------
# ⑤ @remotion/* 依赖的声明 ↔ 装载 ↔ 许可
# ---------------------------------------------------------------------------
_OSS_LICENSES = {"MIT", "ISC", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                 "0BSD", "Unlicense", "CC0-1.0"}
# @remotion/* 的「SEE LICENSE IN LICENSE.md」指向 Remotion License（自订许可，
# 免费资格按人头，见 §3.5）——只在 @remotion 命名空间下放行，别处一律拒。
# `UNLICENSED` 也是同一档：`@remotion/effects` 与 `@remotion/transitions`（4.0.534
# 与 4.0.539 都一样）的 package.json 只写这一句、且 tarball 里**没有** LICENSE.md，
# 落到仓库根的 LICENSE.md = Remotion License。它**不是**「随便用」：商用门槛按人头
# （≤3 员工免费，超过要买公司许可），这条白名单只说明「我们知道它是哪一档」。
_REMOTION_LICENSES = {"SEE LICENSE IN LICENSE.md", "Remotion License",
                       "Remotion License https://remotion.dev/license", "UNLICENSED"}


# MIT 的正文里**不出现「MIT」这三个字母**——它的判据是那句授权语。所以兜底识别必须
# 按正文认，不能按关键词认：按关键词写会永远认不出来，而「永远认不出来」在只看
# 「有没有越界」的口上，和「全都合规」打印出来是同一个样子。
_MIT_MARK = "Permission is hereby granted, free of charge"


def _license_of(pkg_dir: Path, meta: dict) -> str:
    """一个包的许可：先信 `package.json` 的 `license` 字段，没有就按随包发的许可正文认。

    为什么必须有这一层兜底：`@remotion/animation-utils@4.0.534` 的 `package.json`
    **整个没有 `license` 字段**（上游漏填），而它随包发的 `LICENSE.md` 是标准 MIT 正文
    （Copyright 2023 Chetan Karwa）。只读字段的话这条会红，而它红得没有道理——但也
    不能因此放过：「字段缺 + 正文是 MIT」与「字段缺 + 正文不是 MIT」必须分成两句话，
    前者是上游漏填，后者才是真的许可不明。返回不可识别的字符串时，调用方把它记进
    bad_license 并原样打出来，所以「认不出来」永远不会被静默吞掉。
    """
    lic = meta.get("license")
    if isinstance(lic, str) and lic:
        return lic
    body = ""
    for name in ("LICENSE.md", "LICENSE", "LICENSE.txt", "LICENCE.md", "LICENCE"):
        f = pkg_dir / name
        if f.is_file():
            body = f.read_text(encoding="utf-8", errors="replace")
            break
    if _MIT_MARK in body:
        return "MIT"
    stripped = body.strip()
    return ("package.json 无 license 字段，且没有随包发的许可正文"
            if not stripped else f"package.json 无 license 字段；正文首行：{stripped.splitlines()[0][:60]}")


def _allowed_imports_remotion(remo: Path) -> set[str]:
    """从 lint-rules.mjs 的 ALLOWED_IMPORTS 里抠出 @remotion/* 条目。"""
    import re
    text = (remo / "lint-rules.mjs").read_text(encoding="utf-8")
    return set(re.findall(r"'(@remotion/[a-z0-9-]+)'", text))


def test_remotion_deps(remo: Path | None) -> None:
    print("\n=== ⑤ @remotion/* 依赖：已声明 + 已装载 + 许可合规 ===")
    if remo is None:
        _unverified.append("⑤ remotion/ 目录不可达")
        return
    pkg = remo / "package.json"
    if not pkg.is_file():
        check(False, f"remotion/package.json 在（{pkg}）")
        return
    data = json.loads(pkg.read_text(encoding="utf-8"))
    declared = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    decl_remo = sorted(k for k in declared if k.startswith("@remotion/"))
    check(bool(decl_remo), f"package.json 声明了 @remotion/*（{decl_remo}）")

    # (a) ALLOWED_IMPORTS 里点名的 @remotion 包必须都被声明——否则闸① 放行的 import 打包期必炸。
    allowed = _allowed_imports_remotion(remo)
    missing = sorted(allowed - set(declared))
    check(not missing, f"闸① 白名单里的 @remotion 包都已声明（缺：{missing}）")

    # (b) 已装载：node_modules 在时才判；不在就记未验证（不许当通过）。
    nm = remo / "node_modules" / "@remotion"
    if not nm.is_dir():
        _unverified.append(f"⑤ node_modules 不可达（{nm}）——「已装载 + 许可」两项未验证")
        return
    installed = {d.name for d in nm.iterdir() if d.is_dir()}
    not_installed = sorted(p.split("/", 1)[1] for p in decl_remo
                           if p.split("/", 1)[1] not in installed)
    check(not not_installed,
          f"声明的 @remotion 包都已装载（镜像可达）（缺：{not_installed}）")

    bad_license: list[str] = []
    for pkg_name in decl_remo:                      # 只查**我们声明的那几个**
        name = pkg_name.split("/", 1)[1]
        meta = nm / name / "package.json"
        if not meta.is_file():
            # 目录在、元数据不在：既不是「没装」（上面那条管）也不是「合规」，
            # 静默 continue 等于给这条口子留一个永远绿的洞。
            bad_license.append(f"{pkg_name}=（node_modules 里没有 package.json）")
            continue
        lic = _license_of(nm / name, json.loads(meta.read_text(encoding="utf-8")))
        if lic in _OSS_LICENSES or lic in _REMOTION_LICENSES:
            continue
        bad_license.append(f"{pkg_name}={lic!r}")
    check(not bad_license,
          f"声明的 @remotion 包许可都在白名单内（越界：{bad_license}）")


def test_license_resolver(tmp: Path) -> None:
    """⑤b 兜底判据自己的红：五个假包，逐个问 `_license_of` 该说什么。

    没有这一节，⑤ 的兜底只是个「让动画工具那条红变绿」的补丁——而一个只会变绿的
    补丁和一个「什么都不判、永远返回 MIT」的函数，在 ⑤ 的输出里长得一模一样。
    所以五种形态各要一句不同的回话，其中两种必须**认不出来**（认不出来才进
    bad_license，才在 ⑤ 里被喊出来）。
    """
    print("\n=== ⑤b 许可兜底判据（先证它会拒，再证它会放）===")
    cases = [
        ("字段写明 MIT → 放行", {"license": "MIT"}, None, "MIT"),
        ("字段写明 GPL → 逐字回出来（越界名单才拦得住）", {"license": "GPL-3.0"},
         None, "GPL-3.0"),
        ("字段缺 + 正文是 MIT 授权语 → 认成 MIT（上游漏填那一类）", {},
         "MIT 正文", "MIT"),
        ("字段缺 + 正文不是 MIT → 不许认成 MIT", {},
         "Copyright 2020 Someone\n\nRedistribution is forbidden.",
         None),
        ("字段缺 + 连正文都没有 → 也要有话，不许静默", {}, None, None),
    ]
    for i, (label, meta, lic_md, want) in enumerate(cases):
        d = tmp / f"p{i}"
        d.mkdir(parents=True, exist_ok=True)
        if lic_md == "MIT 正文":
            (d / "LICENSE.md").write_text(
                "Copyright 2023 Chetan Karwa\n\n"
                "Permission is hereby granted, free of charge, to any person obtaining a copy "
                "of this software and associated documentation files (the “Software”), to deal "
                "in the Software without restriction.\n", encoding="utf-8")
        elif lic_md:
            (d / "LICENSE.md").write_text(lic_md + "\n", encoding="utf-8")
        got = _license_of(d, meta)
        if want is None:
            check(got not in _OSS_LICENSES and got not in _REMOTION_LICENSES,
                  f"{label}（实得 {got!r}——它会被记进越界名单）")
        else:
            check(got == want, f"{label}（实得 {got!r}）")


def test_deleted_orphans_stay_dead() -> None:
    """⑥ 这一轮删掉的三处「写了但没人接」，不许长回来。

    判据写成「孤儿不存在」——这正是本文件存在的理由：它们全都能跑、看起来都像能力，
    但链路上没有一处引用。删了以后如果哪天又被人「顺手补全」，这条要红。
    """
    import inspect
    import re

    print("\n=== ⑥a Agent 不再自带进程内循环（外层循环只有 MQ 一条） ===")
    from agent_framework.agent import Agent
    for gone in ("submit", "run_forever", "stop", "_process"):
        check(not hasattr(Agent, gone),
              f"Agent.{gone} 已删（它的活在生产由 SessionConsumer + MQ 分区包揽）")
    check("_queue" not in inspect.getsource(Agent.__init__),
          "Agent 构造不再建进程内队列（那套 create_task 没人 await，失败只落一句 log）")

    print("\n=== ⑥b 会话键只有一个出处（identity.session_key） ===")
    from agent_framework import identity
    from agent_framework.consumer import session_key as consumer_key
    from agent_framework.session import Session

    check(consumer_key is identity.session_key,
          "consumer 的 MQ 分区 key 就是 identity.session_key（不是另拼一份）")
    check(Session(user_id="u", conversation_id="c").session_id
          == identity.session_key("u", "c") == identity.Identity("u", "c").session_id,
          "Session / Identity / MQ 三处的会话键逐字相同")
    definers = [p.name for p in (_backend_dir() / "agent_framework").rglob("*.py")
                if re.search(r"^def session_key\(", p.read_text(encoding="utf-8"),
                             flags=re.M)]
    check(definers == ["identity.py"],
          f"全仓只有一处 `def session_key`（实得 {definers}）")

    print("\n=== ⑥c ContextBuilder 不再有 Bootstrap 注入层 ===")
    from agent_framework.context import ContextBuilder
    params = set(inspect.signature(ContextBuilder.__init__).parameters)
    check("bootstrap_dir" not in params and "bootstrap_files" not in params,
          f"构造参数里没有 bootstrap*（实得 {sorted(params)}）")
    src = (_backend_dir() / "agent_framework" / "context.py").read_text(encoding="utf-8")
    check("_bootstrap_sections" not in src,
          "读取那一步也已删（留着就是「参数没人传、函数没人调」的第二层孤儿）")


def main() -> int:
    remo = _remotion_dir()
    test_styles_json_orphan(remo)
    test_migrations_dir_orphan()
    test_public_dir_present(remo)
    test_assets_prefix_not_gc()
    test_remotion_deps(remo)
    with tempfile.TemporaryDirectory() as td:
        test_license_resolver(Path(td))
    test_deleted_orphans_stay_dead()

    print()
    if _unverified:
        print("未验证清单（跳过 ≠ 通过）：")
        for s in _unverified:
            print(f"  · {s}")
    print(f"{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
