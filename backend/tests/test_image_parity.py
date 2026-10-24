# -*- coding: utf-8 -*-
"""T21：镜像 COPY 完整性——判据自己的红与绿（`scripts/image_parity.py`）。

被考的脚本在**宿主上**跑（它要连 docker：`docker exec <容器> md5sum`），所以这份用例
分两层，两层各自能判的都判、判不了的**记未判**（D-未判不绿）：

① **纯逻辑层**（两个布局都判得到）：Dockerfile 解析、`.dockerignore` 逐条套用、目录与
   glob 展开、目的路径怎么算（WORKDIR / 目录 dest / 改名 dest）、`--from=` 阶段产物怎么
   归。这一层全部按 D-红先 造样本：**该被拒的那几份先证红**，再证一份该放的。
② **两处布局各自的证据**：宿主上有真 `Dockerfile` 时，把「sanctioned 环境本该判得动却
   判不了」的那些件逐件从**真清单**里点名；容器里没有 Dockerfile，但**有它的产物**——
   于是容器这一侧改成「镜像里真带着这些件」。

为什么不把真 md5 对账也写进这份用例：它需要 docker，而 sanctioned 环境（容器**里面**）
没有 docker 客户端。那一腿的证据是脚本本身的一趟运行（§7 记输出、§9 记结论），
用例这一层只钉「清单算得对」——清单算错，那一趟的输出就是好看的错数。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
#: 仓库根：宿主是 `<仓库根>/backend/tests/…`，镜像里是 `/app/tests/…`（→ `/`）。
_REPO = _HERE.parents[2]
for _p in (_HERE.parents[1], _REPO / "backend", _REPO, _REPO / "scripts"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from image_parity import (Plan, is_ignored, load_ignore,  # noqa: E402
                          parse_stages, plan)

_fails = 0
_checks = 0
_unverified: list[str] = []


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


#: 「sanctioned 环境本该判得动」的那些件：镜像里必须带着，缺一件就有用例从「判」退化成
#: 「未判」或直接红。逐件都指得出谁在用它（右边那句是**名字**层面的理由，不是猜测）。
SENTINELS: tuple[tuple[str, str], ...] = (
    ("/app/remotion/src/tokens.ts", "test_code_spec ⑤：契约比 tokens 的字幕档位与画幅"),
    ("/app/remotion/src/FilmLayout.tsx", "test_code_spec ⑤：画面带几何与母版同源"),
    ("/app/remotion/src/themes/palettes.json", "闸⑪：palette 取色要解析成真实色号"),
    ("/app/remotion/lint-rules.mjs", "test_code_spec ⑤：闸① 的 import 白名单"),
    ("/app/remotion/package.json", "test_delivery_hygiene ⑤：已声明的 @remotion 依赖"),
    ("/app/remotion/server.mjs", "test_delivery_hygiene：完整源码树的哨兵之一"),
    # server.mjs 的 import（`lint.mjs` 自己再 import `lint-rules.mjs`）：不带它们，
    # 镜像里就躺着一段连 import 都过不了的 server.mjs，而它正是「这是一棵完整
    # 源码树」的哨兵之一。2026-10-16 这里原来是 `assetgate.mjs` / `imagecheck.mjs`，
    # 随闸⑬ 与投递腿一起删了，换成 server.mjs 今天真正 import 的这两件。
    ("/app/remotion/lint.mjs", "server.mjs 的 import（lintShotCode）"),
    ("/app/remotion/motionsample.mjs", "server.mjs 的 import（motionDensity）"),
    ("/app/remotion/src/preview/caption-sample.tsx",
     "「文字库」面板的字幕样张底稿：code_spec.sample_shot_code() 读它"),
    ("/app/remotion/tsconfig.json", "test_code_spec：@dg/themes/* → src/themes/* 的别名对账"),
)


def _tree(root: Path, files: dict[str, str]) -> None:
    """造一棵假构建上下文（内容随便，这一层判的是清单不是字节）。"""
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")


def section_pure_logic(tmp: Path) -> None:
    print("\n① 红：COPY 的源在宿主上不存在 → 记进缺源清单（不许静默跳过这一条）")
    _tree(tmp / "a", {"Dockerfile": "FROM x\nCOPY gone.txt /app/\n",
                      "keep.txt": "k"})
    pl = plan((tmp / "a" / "Dockerfile").read_text("utf-8"), tmp / "a")
    check(any("gone.txt" in s for s in pl.missing_sources),
          f"源不存在 → 缺源清单里点名它（{pl.missing_sources}）")
    check(pl.pairs == [], "缺源那一件不进对账清单——它连宿主字节都没有，拿什么比")

    print("\n② 红：点名的 glob 一条都没匹配 → 也红（声明要拷、却没有可拷的）")
    _tree(tmp / "b", {"Dockerfile": "FROM x\nCOPY lock.json* /app/\n"})
    pl = plan((tmp / "b" / "Dockerfile").read_text("utf-8"), tmp / "b")
    check(any("lock.json*" in g for g in pl.empty_globs),
          f"空 glob → 红（{pl.empty_globs}）")
    _tree(tmp / "c", {"Dockerfile": "FROM x\nCOPY lock.json* /app/\n",
                      "lock.json": "{}"})
    pl = plan((tmp / "c" / "Dockerfile").read_text("utf-8"), tmp / "c")
    check(pl.empty_globs == [] and [p[1] for p in pl.pairs] == ["lock.json"],
          "同一句话、盘上有那一件 → 绿（证明红不是因为「glob 一概不认」，而是真没匹配到）")

    print("\n③ `.dockerignore`：四种形态各排一件，认不出来的形态**不排除**（安全侧）")
    pats = [".git", "**/__pycache__", "**/node_modules", "backend/.smoke",
            "frontend/dist", "openchatcut-src/", "*.tar"]
    for rel, why in ((".git/config", "根级名字"),
                     ("backend/a/__pycache__/x.pyc", "** 前缀的名字"),
                     ("backend/a/node_modules/p/x.js", "任意层的 node_modules"),
                     ("backend/.smoke/shot.png", "带斜杠的目录"),
                     ("backend/out.tar", "*.ext")):
        check(is_ignored(rel, pats), f"该排除：{rel}（{why}）")
    check(not is_ignored("backend/a/node_modules_scanner/x.py", pats),
          "名字撞前缀不算（`node_modules_scanner` 不是 `node_modules`）——"
          "宽松匹配会把该查的件静默漏掉")
    check(not is_ignored("backend/a/weird!pattern", ["[unclosed"]),
          "认不出来的模式**不排除**：多查一件最坏是多一条看得见的红，"
          "「认不出来就排除」则会静默少查一件")

    print("\n④ 目录源：`COPY src/ /dest/` 展开成逐件，落到 /dest/<相对尾巴>（子目录照搬）")
    _tree(tmp / "d", {"Dockerfile": "FROM x\nWORKDIR /app\nCOPY src/ /dest/\n",
                      "src/a.py": "a", "src/sub/b.py": "b", "src/sub/deep/c.py": "c",
                      "outside.py": "o"})
    pl = plan((tmp / "d" / "Dockerfile").read_text("utf-8"), tmp / "d")
    image = {p[2] for p in pl.pairs}
    check(image == {"/dest/a.py", "/dest/sub/b.py", "/dest/sub/deep/c.py"},
          f"目录源按内容展开：{sorted(image)}")
    check("outside.py" not in {p[1] for p in pl.pairs},
          "源目录之外的文件一件都不进清单")

    print("\n⑤ 文件源：多个源进目录 dest 用 basename；单源 + 非斜杠结尾 dest 就是那个名字")
    _tree(tmp / "e", {"Dockerfile": "FROM x\nWORKDIR /app\n"
                                    "COPY one.txt two.txt /dst/\n"
                                    "COPY one.txt /renamed.txt\n",
                      "one.txt": "1", "two.txt": "2"})
    pl = plan((tmp / "e" / "Dockerfile").read_text("utf-8"), tmp / "e")
    image = {p[2] for p in pl.pairs}
    check(image == {"/dst/one.txt", "/dst/two.txt", "/renamed.txt"},
          f"两种目的写法都要对：{sorted(image)}")

    print("\n⑥ `--from=` 与构建阶段：一个只查存在、一个压根不进最终清单")
    _tree(tmp / "f", {"Dockerfile": "FROM node AS web\nWORKDIR /web\nCOPY fe/ ./\n"
                                    "FROM py\nWORKDIR /app\nCOPY be/ ./\n"
                                    "COPY --from=web /web/dist /app/dist/\n",
                      "fe/index.js": "f", "be/main.py": "b"})
    pl = plan((tmp / "f" / "Dockerfile").read_text("utf-8"), tmp / "f")
    check(sorted(p[2] for p in pl.pairs) == ["/app/main.py"],
          f"只有最终阶段的构建上下文 COPY 参与 md5 对账：{[p[2] for p in pl.pairs]}")
    check(pl.presence == ["/app/dist"], f"--from= 的产物进「该存在」那一栏：{pl.presence}")
    check(len(pl.staged) == 1 and "phase web" not in " ".join(pl.staged),
          f"构建阶段那条只报数、不进清单：{pl.staged}")
    check(pl.workdir == "/app", "WORKDIR 读的是**最终阶段**那一个（前一阶段的 /web 不算）")

    print("\n⑦ WORKDIR 相对目的：`COPY a.txt out/` 在 WORKDIR=/srv 下是 /srv/out/a.txt")
    _tree(tmp / "g", {"Dockerfile": "FROM x\nWORKDIR /srv\nCOPY a.txt out/\n",
                      "a.txt": "a"})
    pl = plan((tmp / "g" / "Dockerfile").read_text("utf-8"), tmp / "g")
    check([p[2] for p in pl.pairs] == ["/srv/out/a.txt"], f"{[p[2] for p in pl.pairs]}")
    stages = parse_stages("FROM a AS one\nFROM b\nRUN x\n")
    check([s.name for s in stages] == ["one", ""] and len(stages) == 2,
          f"AS 名字认得出、无名阶段留空：{[s.name for s in stages]}")
    check(parse_stages("# 注释\nFROM a\\\n  AS z\nCOPY x y\n")[0].name == "z",
          "续行与整行注释都按 Dockerfile 的规则拼（`FROM a \\` 换行接 `AS z`）")

    print("\n⑧ 未判与红的退出码不许混：缺 Dockerfile = 未判（2），不是绿（0）")
    from image_parity import main as parity_main
    rc = parity_main(["--dockerfile", str(tmp / "nope" / "Dockerfile"),
                      "--context", str(tmp), "--container", "no-such-container"])
    check(rc == 2, f"读不到 Dockerfile → 退出码 2（未判），拿到 {rc}")


def section_generated(tmp: Path) -> None:
    """⑪ 「构建期重写」这一类：偏松的检测 + 逐条点名（§9-Q87）。"""
    from image_parity import generated_basenames, parse_stages as _parse

    print("\n⑪ 红：RUN 真**写**过的文件不进 md5 对账（但必须点名），写之外的件一件都不许被挪走")
    _tree(tmp / "h", {
        "Dockerfile": "FROM node\nWORKDIR /srv\n"
                      "COPY font-list.txt ./\n"
                      "COPY whitelist.json ./\n"
                      "RUN node -e \"const fs=require('fs');"
                      " fs.writeFileSync('whitelist.json', JSON.stringify(l))\n",
        "font-list.txt": "f", "whitelist.json": "{}"})
    pl = plan((tmp / "h" / "Dockerfile").read_text("utf-8"), tmp / "h")
    gen = " ".join(pl.generated)
    check("whitelist.json" in gen and "第 5 行" in gen,
          f"被 RUN 写过的件进「构建期重写」并带上 RUN 行号（{pl.generated}）")
    check("/srv/whitelist.json" not in {p[2] for p in pl.pairs},
          "它不进 md5 对账——构建期重写的东西与宿主本来就该不同，比出来是假红")
    check("/srv/font-list.txt" in {p[2] for p in pl.pairs},
          "同一份 Dockerfile 里**没被写过**的那件照旧进对账"
          "（否则这一节只是「整条 RUN 出现了就把这文件夹抬走」的托词）")

    print("\n⑪-b 红：两个假阳的构造样本——它们**必须不被挪走**（否则是静默少查一件）")
    # 这两个坑都是实测踩过的，记在 image_parity.py 的注释里；这里把它们钉成用例。
    _tree(tmp / "i", {
        "Dockerfile": "FROM node\nWORKDIR /srv\n"
                      "COPY lint.mjs ./\n"
                      "COPY package.json ./\n"
                      "COPY font-whitelist.json ./\n"
                      "RUN node -e \"const fs=require('fs');"
                      " const w = fs.writeFileSync('font-whitelist.json', '[]');"
                      " const q = list.filter((m) => m.name);"
                      " const c = require('./lint.mjs');"
                      " const p = require('./package.json');\n",
        "lint.mjs": "l", "package.json": "{}", "font-whitelist.json": "[]"})
    pl = plan((tmp / "i" / "Dockerfile").read_text("utf-8"), tmp / "i")
    gen = " ".join(pl.generated)
    check("font-whitelist.json" in gen,
          f"真被 writeFileSync 写的那个仍在名单里（{pl.generated}）")
    check("./lint.mjs" not in gen and "./package.json" not in gen,
          "箭头函数 `=>` 与 `require('./x')` 都**不算写**——"
          "前者的 `>` 曾被子串匹配命中、窗口一滑就捞到后者，于是两件都被静默挪出对账")
    check({"/srv/lint.mjs", "/srv/package.json"} <= {p[2] for p in pl.pairs},
          "被读的那两件照旧逐件对账（其中 lint-rules.mjs 正是好几条用例的被测件）")

    print("\n⑪-c 绿：真重定向 `> out.json` 要认出来（证明这一支不是「一律不认」）")
    _tree(tmp / "j", {
        "Dockerfile": "FROM node\nWORKDIR /srv\n"
                      "COPY a.mjs ./\nCOPY out.json ./\nRUN node a.mjs > out.json\n",
        "a.mjs": "a", "out.json": "{}"})
    pl = plan((tmp / "j" / "Dockerfile").read_text("utf-8"), tmp / "j")
    check("out.json" in " ".join(pl.generated), f"{pl.generated}")
    check("/srv/a.mjs" in {p[2] for p in pl.pairs}, "重定向左边的输入件照旧对账")

    print("\n⑪-d 红：`--generated` 补名必须查得到依据，否则是万能钥匙")
    from image_parity import main as parity_main2
    common = ["--dockerfile", str(tmp / "i" / "Dockerfile"),
              "--context", str(tmp / "i"), "--container", "no-such-container",
              "--list-only"]
    bogus = parity_main2(common + ["--generated", "never-mentioned.json"])
    check(bogus == 1,
          f"RUN 原文里查不到这个名字 → 红（1），拿到 {bogus}——"
          "否则这个口子就成了「凡是对不上就写进来」")
    ok = parity_main2(common + ["--generated", "font-whitelist.json"])
    check(ok == 0, f"查得到 → 收下并照常绿（0），拿到 {ok}")

    print("\n⑪-e 单源钉：真 remotion/Dockerfile 的名单**正好一件**（多了少了都要看得见）")
    rdf = _REPO / "remotion" / "Dockerfile"
    if not rdf.is_file():
        _unverified.append(f"⑪-e 真 {rdf} 不可达——「名单收敛成一件」这一条未验证")
        print(f"  ⚠ 未判：{rdf} 不在")
        return
    rpl = plan(rdf.read_text("utf-8"), _REPO / "remotion",
               load_ignore(_REPO / "remotion" / ".dockerignore"))
    names = sorted({g.split("（")[0].rsplit("/", 1)[-1].strip() for g in rpl.generated})
    check(names == ["font-whitelist.json"],
          f"remotion 那腿的构建期重写名单收敛成 {names}——"
          "与「只有这一件是构建期生成的」这个事实相符；多一件说明检测又放宽了")
    st = _parse("FROM a\n# 注释\nWORKDIR /s\nRUN x\n\nRUN y\n")[0]
    check([n for n, _t in st.runs] == [4, 6],
          f"RUN 的行号从整份 Dockerfile 数出来、注释与空行也占号（{st.runs}）——"
          "「第几行重写过它」这句话才点得准")


def section_runtime(tmp: Path) -> None:
    """⑫ 「运行期改写」这一类：跑过一次服务它的字节就变了（§9-Q89）。"""
    from image_parity import (parse_stages as _parse, plan, runtime_written,
                              serve_entry)

    print("\n⑫ 红：服务运行期**写**的件不进 md5 对账，但必须点名、且仍然要在")
    _tree(tmp / "k", {
        "Dockerfile": 'FROM node\nWORKDIR /srv\nCOPY . .\nCMD ["node", "server.mjs"]\n',
        "server.mjs": (
            "import fs from 'node:fs';\n"
            "import path from 'node:path';\n"
            "const ROOT = path.dirname('x');\n"
            "const SHOT = path.join(ROOT, 'src', 'user-shots', 'shot.tsx');\n"
            "const LOCK = path.join(ROOT, 'package-lock.json');\n"
            "export const go = async (code) => {\n"
            "  await fs.writeFile(SHOT, code, 'utf8');\n"
            "  fs.readFileSync(LOCK);\n"
            "};\n"),
        "src/user-shots/shot.tsx": "// 起手模板\n",
        "package-lock.json": "{}"})
    pl = plan((tmp / "k" / "Dockerfile").read_text("utf-8"), tmp / "k")
    rt = " ".join(pl.runtime_files)
    check("src/user-shots/shot.tsx" in rt and "服务运行期会覆写它" in rt,
          f"被服务写过的件进「运行期改写」并说明依据（{pl.runtime_files}）")
    check(pl.runtime_paths == ["/srv/src/user-shots/shot.tsx"],
          f"它仍然要判一次「在不在」（{pl.runtime_paths}）——不比字节不等于不管它")
    check("/srv/src/user-shots/shot.tsx" not in {p[2] for p in pl.pairs},
          "它不进 md5 对账——容器跑过一次渲染，这一件的字节就已经不是宿主那一份了")
    check("/srv/package-lock.json" in {p[2] for p in pl.pairs},
          "同一个 path.join(ROOT, …) 家族里**只被读**的那件照旧对账"
          "（否则这一节就成了「凡是从 ROOT 拼出来的都抬走」的托词）")

    print("\n⑫-b 红：「运行期改写」的名字不手写，从 CMD 点名的那个服务入口现读")
    check(serve_entry(_parse('FROM a\nCMD ["node", "server.mjs"]\n')[0]) == "server.mjs",
          "exec 形式的 CMD 认得出服务入口")
    check(serve_entry(_parse('FROM a\nCMD python run_server.py\n')[0]) == "",
          "非 node 起的（app 那条 `CMD python run_server.py`）返回空串、"
          "**不去猜一个文件名**——猜错了就是凭空多一栏假红")
    check(serve_entry(_parse('FROM a\nCMD node a.mjs b.mjs\n')[0]) == "a.mjs",
          "带实参的 CMD 取**紧跟解释器的那个脚本**，不是最后一个实参——"
          "`b.mjs` 是传给脚本的参数，拿它当入口会读到一份不相干的东西")
    check(serve_entry(_parse('FROM a\nCMD node --experimental-x server.mjs\n')[0])
          == "server.mjs",
          "解释器与脚本之间的开关跳过（`node --flag server.mjs`）")
    check(runtime_written("const A = path.join(ROOT, 'x.tsx');\nA;\n") == [],
          "只出现、没被写的常量不算「运行期改写」"
          "（与「构建期重写」挂在**写**上是同一条纪律）")
    check(runtime_written("const A = path.join(ROOT, 'x.tsx');\nfs.unlink(A);\n")
          == ["x.tsx"],
          "删也算写（`unlink`）——删掉的件同样「此刻的字节」无意义")

    print("\n⑫-c 单源钉：真 remotion/server.mjs 读出来的**正好一件**")
    rsm = _REPO / "remotion" / "server.mjs"
    if not rsm.is_file():
        _unverified.append(f"⑫-c 真 {rsm} 不可达——「运行期改写收敛成一件」未验证")
        print(f"  ⚠ 未判：{rsm} 不在")
        return
    got = runtime_written(rsm.read_text("utf-8"))
    check(got == ["src/user-shots/shot.tsx"],
          f"真 server.mjs 的「运行期改写」收敛成 {got}——与「服务只往这一个槽里写」"
          "这个事实相符；多出来说明检测又放宽了")


def section_host_layout() -> bool:
    """宿主布局：真 Dockerfile 在仓库根才判得到。→ 判到了吗。"""
    print("\n⑨ 宿主布局：真 Dockerfile 的最终清单必须覆盖那些哨兵件")
    df = _REPO / "Dockerfile"
    if not df.is_file():
        _unverified.append(f"⑨ 真 Dockerfile 不可达（{df}）——「清单覆盖哨兵件」这一节未验证")
        print(f"  ⚠ 未判：{df} 不在——这一节留给宿主那一趟（见本文件开头）")
        return False
    pl = plan(df.read_text("utf-8"), _REPO,
              load_ignore(_REPO / ".dockerignore"))
    check(pl.missing_sources == [] and pl.empty_globs == [],
          f"真 Dockerfile 的每一条 COPY 源都落在盘上（缺源 {pl.missing_sources}、"
          f"空 glob {pl.empty_globs}）")
    image = {p[2] for p in pl.pairs}
    want = {p for p, _why in SENTINELS if p.startswith("/app/") or p.startswith("/scripts/")}
    missing = sorted(want - image)
    check(not missing, f"最终阶段的清单里逐件都在（缺：{missing}）")
    check(pl.presence == ["/frontend/dist"],
          f"`--from=web` 的前端产物被判成「该存在」而不是 md5 对账：{pl.presence}")
    check(any("frontend/" in s for s in pl.staged),
          "web 阶段那两条 COPY 落在「构建阶段」那一栏（它们不进最终镜像）")
    # 反向的一件：清单是**从 Dockerfile 现读**的，不是人手维护的表——把哨兵那一行 COPY
    # 抽掉，同一份盘上的树必须报缺源/缺件。这一条钉的是「表不会漂」。
    text = df.read_text("utf-8")
    # 2026-10-16：锚点原来是 `COPY scripts/ /scripts/`——那一行连同整个 scripts/
    # 的 COPY 一起在「仓库只留构建与启动所需文件」那一轮出库了，这条用例随之红。
    # 换成一条今天真在的单行 COPY；判的东西不变：清单是**从 Dockerfile 现读**的，
    # 不是人手维护的表（抽掉那一行，它写入的镜像路径必须立刻从清单里消失）。
    keep = "COPY remotion/src/themes/palettes.json /app/remotion/src/themes/palettes.json"
    check(keep in text, "抽掉-再放回那一支要用的锚点句在真 Dockerfile 里")
    cut = plan(text.replace(keep, ""), _REPO, load_ignore(_REPO / ".dockerignore"))
    cut_image = {p[2] for p in cut.pairs}
    check("/app/remotion/src/themes/palettes.json" not in cut_image,
          "把那条 `COPY … palettes.json …` 抽掉 → 它立刻从清单里消失"
          "（清单是现读的，不是抄来的）")
    return True


def host_source_of(image_path: str) -> Path | None:
    """镜像内路径 → 它在构建上下文里的源（`/app/x` → `<仓库根>/x`、`/scripts/x` → 同）。
    只覆盖本 Dockerfile 实际用到的两个落点；认不出来返回 None（不猜）。"""
    for prefix in ("/app/", "/scripts/"):
        if image_path.startswith(prefix):
            return _REPO / image_path[len(prefix):] if prefix == "/app/" \
                else _REPO / "scripts" / image_path[len("/scripts/"):]
    return None


def section_container_layout() -> bool:
    """容器布局：没有 Dockerfile，但有它的**产物**。→ 判到了吗。"""
    print("\n⑩ 容器布局：镜像里真带着那些哨兵件（没有 Dockerfile 时的另一半证据）")
    if (_REPO / "Dockerfile").is_file():
        # 宿主布局：这一节判「源文件在盘上」，镜像那一侧由 ⑨ 的清单覆盖。
        missing_src = [p for p, _why in SENTINELS
                       if (s := host_source_of(p)) is not None and not s.is_file()]
        check(not missing_src, f"哨兵件的源在宿主上也逐件都在（缺：{missing_src}）")
        return True
    absent = [p for p, _why in SENTINELS if not Path(p).exists()]
    check(not absent, f"镜像里逐件都在（缺：{absent}）")
    empty = [p for p in ("/app/remotion/src/tokens.ts", "/app/remotion/server.mjs")
             if Path(p).stat().st_size == 0]
    check(not empty, f"在的那几件不是空文件（空文件会让「读到了」变成假的）：{empty}")
    return True


def main() -> int:
    print(f"§0 夹具自检：脚本在 sys.path 上、哨兵表非空、仓库根锚点指得对（{_REPO}）")
    check(callable(plan) and callable(is_ignored) and isinstance(Plan(), Plan),
          "image_parity 的纯逻辑层import 得到")
    check(len(SENTINELS) >= 10 and len({p for p, _ in SENTINELS}) == len(SENTINELS),
          f"哨兵表 {len(SENTINELS)} 件、无重复")

    tmp = Path(tempfile.mkdtemp(prefix="imgparity_"))
    try:
        section_pure_logic(tmp)
        section_generated(tmp)
        section_runtime(tmp)
        host_judged = section_host_layout()
        ctr_judged = section_container_layout()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n未验证清单（跳过 ≠ 通过）：")
    print("  · 真 md5 对账那一趟不在本文件里：它要连 docker，而 sanctioned 环境"
          "（容器里面）没有 docker 客户端——那一腿是 `python scripts/image_parity.py`"
          " 的一趟运行，输出记在 §7/§9")
    for item in _unverified:
        print(f"  · {item}")
    print(f"\n{_checks - _fails}/{_checks} 通过"
          f"（宿主布局判到：{host_judged}；容器布局判到：{ctr_judged}）")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
