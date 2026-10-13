# -*- coding: utf-8 -*-
"""T21：镜像 COPY 完整性——Dockerfile 声明的每一件，逐件比宿主与镜像内的字节。

**这条判据治的是哪一类病**（2026-10-13 一天里撞了两次）：镜像 tag 移了、容器不动
（§9-Q81），于是 `docker exec creation-app python scripts/run_tests.py` 那一趟跑的是
几天前那份代码——`md5sum: No such file or directory`，而它长得像环境问题（「容器里没
这棵树」），最容易被记成未判。手工比五个文件的 md5 能救那一次，救不了下一次：
**没有清单，就没有「都查过了」这句话**。

所以清单**从 Dockerfile 里现读**，不从人手维护的表里读：`COPY` 的源按构建上下文展开
（目录递归、glob 展开、`.dockerignore` 逐条套用），落到镜像里的绝对路径由 `WORKDIR`
与目的路径算出来，然后逐件 `hashlib.md5` 对 `docker exec <容器> md5sum`。

四档结论（**未判不绿**）：

- 宿主在、镜像在、md5 相等 —— 这一件对上了；
- 镜像里没有 —— 红（COPY 漏了，或容器跑的是别的镜像）；
- 两边 md5 不等 —— 红（更刺眼的一档：文件在、内容是另一份）；
- docker 不可用 / 容器没起 —— **未判**，单独一栏，**不**计成对上（退出码 2）。

另有两类**不进对账但必须点名**，都是「比 md5 没有意义」，不是「不用管」：

- **构建期由 `RUN` 写出来的件**（`remotion/Dockerfile` 第 61 行生成
  `font-whitelist.json`）。它与宿主本来就该不同；
- **服务运行期会覆写的件**（`remotion/server.mjs` 每次渲染前把镜头代码写进
  `src/user-shots/shot.tsx`）。跑过任何一次渲染的容器里，这一件的字节就已经不是
  宿主那一份了——它是个**缓存槽**，不是构建产物。

两类都落成**单独一栏、逐条打印**（而不是静默跳过，那又是「少查一件」）。检测宁可偏松
（多报一行看得见的），不许偏紧（漏认一件就少查一件）：细节与两个踩过的坑见
`generated_basenames` 上方那段注释，口径见 §9-Q87、§9-Q89。
**两份名单都不手写**——「构建期重写」从最终阶段的 `RUN` 原文现认，「运行期改写」从
最终阶段 `CMD` 点名的那个服务入口源码里现读。手写的名单会在服务改了落点之后静静过期。

运行期那一栏还多一条：**它仍然要在**。`src/entry.tsx` 静态 import 着
`./user-shots/shot`，缺了这一件连 bundle 都起不来——所以「不比字节」不等于「不管它」。

用法：
    python scripts/image_parity.py --dockerfile Dockerfile --context . \\
        --container creation-app --ignore .dockerignore
    python scripts/image_parity.py --dockerfile remotion/Dockerfile --context remotion \\
        --container creation-remotion --ignore remotion/.dockerignore --batch 200

退出码：0 = 逐件对上且无未判；1 = 有红（缺文件、内容不一致、运行期改写那一栏缺件）；
2 = **只有未判**（docker 不可用、容器没起）——「没查过」与「查过都对」不许打成同一个码。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 一条 `docker exec` 里塞几个路径。命令行长度是有上限的，而 `COPY backend/ ./` 这一条
#: 展开出来近千件——分批既躲开上限，也让某一批超时不影响别的批。
_MD5_BATCH = 120

#: 认不出来的 `.dockerignore` 模式怎么办：**不排除**。多查一件最坏是多一条看得见的红，
#: 「认不出来就当排除」则会静默少查一件——而静默少查正是这个脚本要消灭的东西。
#: 支持的形态（本仓两处 `.dockerignore` 的实际用法，覆盖到了）：`name`、`dir/sub`、
#: `dir/`、`**/name`、`*.ext`、`a/*`；不支持的写法按「不排除」处理。
_IGNORE_DOC = "支持 name / dir/sub / dir/ / **/name / *.ext / a/*；其余写法按不排除处理"


# ---------------------------------------------------------------------------
# 解析：Dockerfile → 阶段与 COPY 指令（纯函数，离线可测）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Copy:
    """一条 `COPY`（或 `ADD`）指令。`stage` 是它**所在**阶段的序号。"""

    stage: int
    srcs: tuple[str, ...]      # 源（构建上下文内，或 `--from=` 那个阶段的路径）
    dest: str                  # 目的（镜像内，可能是相对 WORKDIR 的相对路径）
    from_stage: str            # `--from=web` 的 web；空串表示源来自构建上下文
    line: int


@dataclass
class Stage:
    index: int
    base: str
    name: str = ""
    workdir: str = "/"
    copies: list[Copy] = field(default_factory=list)
    #: 本阶段每一条 RUN 的（行号, 原文）。用来认「构建期重写」——见 `plan` 的 `generated`。
    runs: list[tuple[int, str]] = field(default_factory=list)
    #: 本阶段的 `CMD`（原文实参）。用来找服务入口，再从那句源码里现读「运行期改写的件」。
    cmd: str = ""


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """按 Dockerfile 的续行规则拼逻辑行：反斜杠续行、整行注释丢弃、保留行号。"""
    out: list[tuple[int, str]] = []
    buf = ""
    first = 0
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            if not buf:
                first = no
            buf += line[:-1] + " "
            continue
        if not buf:
            first = no
        out.append((first, (buf + line).strip()))
        buf = ""
    if buf.strip():
        out.append((first or 1, buf.strip()))
    return out


def _split_tokens(rest: str) -> tuple[dict[str, str], list[str]]:
    """切出 `--flag=value` 与剩下的实参（路径里带空格时按 `"` 包起来）。"""
    flags: dict[str, str] = {}
    args: list[str] = []
    for tok in rest.split():
        if tok.startswith("--") and "=" in tok:
            key, _, val = tok.partition("=")
            flags[key[2:]] = val
        else:
            args.append(tok)
    return flags, args


def parse_stages(text: str) -> list[Stage]:
    """Dockerfile 文本 → 阶段表（含每个阶段的 COPY）。

    只认 `FROM`/`COPY`/`ADD`/`WORKDIR`/`CMD` 五种指令：这个脚本要的只有「哪几条 COPY
    落进最终镜像」「它们落在哪儿」、以及「哪个文件是服务入口」（后者的用处是现读
    「运行期改写的件」那一栏）；其余指令（RUN/ENV/LABEL…）一律忽略——**派生的拷贝
    不是 COPY**（`RUN cp` 之类），这一条要判的是「构建期写明的那些件」有没有进镜像。
    """
    stages: list[Stage] = []
    for no, line in _logical_lines(text):
        head, _, rest = line.partition(" ")
        head = head.upper()
        if head == "FROM":
            toks = rest.split()
            name = ""
            if len(toks) >= 3 and toks[-2].upper() == "AS":
                name = toks[-1]
            stages.append(Stage(index=len(stages), base=toks[0] if toks else "", name=name))
            continue
        if not stages:
            continue                      # FROM 之前没有可归属的指令
        stage = stages[-1]
        if head == "WORKDIR":
            stage.workdir = rest.strip().strip('"')
        elif head == "CMD":
            stage.cmd = rest.strip()
        elif head == "RUN":
            stage.runs.append((no, rest.strip()))
        elif head in ("COPY", "ADD"):
            flags, args = _split_tokens(rest)
            if len(args) < 2:
                continue
            stage.copies.append(Copy(stage=stage.index, srcs=tuple(args[:-1]),
                                     dest=args[-1], from_stage=flags.get("from", ""),
                                     line=no))
    return stages


# ---------------------------------------------------------------------------
# .dockerignore
# ---------------------------------------------------------------------------

def load_ignore(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        p = raw.strip()
        if not p or p.startswith("#"):
            continue
        out.append(p)
    return out


def is_ignored(rel_posix: str, patterns: list[str]) -> bool:
    """构建上下文内的相对路径要不要按 `.dockerignore` 排除（见 `_IGNORE_DOC`）。

    注意 Docker 的语义：模式命中一个**目录**时，整棵子树一起排除。所以下面除了比
    自身，还把每一级父路径都比一遍。
    """
    parts = rel_posix.split("/")
    for pat in patterns:
        p = pat.rstrip("/")
        if not p:
            continue
        if p.startswith("**/"):
            tail = p[3:]
            if any(fnmatch.fnmatch(part, tail) for part in parts):
                return True
            continue
        if fnmatch.fnmatch(rel_posix, p):
            return True
        for i in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[:i]), p):
                return True
    return False


# ---------------------------------------------------------------------------
# 展开：COPY 指令 → （宿主文件, 镜像内路径）清单
# ---------------------------------------------------------------------------

def _join_image(dest: str, workdir: str, tail: str = "") -> str:
    """目的路径 + 相对尾巴 → 镜像内的绝对路径。

    Docker 的规则：`dest` 以 `/` 结尾（或本来就是目录）表示「放进这个目录里」，
    否则是「就叫这个名字」。目的以 `/` 开头就是绝对路径，否则相对本阶段的 WORKDIR。
    """
    base = dest if dest.startswith("/") else (
        workdir.rstrip("/") + "/" + dest.lstrip("./"))
    if dest in (".", "./"):
        base = workdir
    if tail:
        return base.rstrip("/") + "/" + tail.lstrip("/")
    return base if base.startswith("/") else "/" + base


def _dest_is_dir(dest: str, src_count: int) -> bool:
    """目的是不是「一个目录」：以 `/` 结尾、`./`、`.`、或一次 COPY 多个源（必然如此）。"""
    return dest.endswith("/") or dest in (".", "./") or src_count > 1


@dataclass
class Plan:
    pairs: list[tuple[Path, str, str]] = field(default_factory=list)   # (宿主, 上下文相对, 镜像内)
    presence: list[str] = field(default_factory=list)                  # 该在镜像里的目的（--from）
    staged: list[str] = field(default_factory=list)                     # 只活在构建阶段的 COPY
    #: 构建期**重写**过的件：`COPY` 带进来，随后的 `RUN` 又把它写了一遍。逐条打印、
    #: 不进对账——它本来就该与宿主不同（`font-whitelist.json` 是 `fc-list` 的产物）。
    #: **但它必须是「打印出来的一栏」而不是「静默跳过的一件」**：SkipList 一旦不出声，
    #: 下次真有一件该查的被写进这个名单，没人看得出来。
    generated: list[str] = field(default_factory=list)
    #: **服务运行期**会覆写的件（`src/user-shots/shot.tsx`）。同「构建期重写」一栏：
    #: 不进 md5 对账、逐条打印；但它**仍然要在**（`runtime_files` 里的镜像路径会
    #: 单独判一次存在），缺了连 bundle 都起不来。
    runtime: list[str] = field(default_factory=list)
    runtime_files: list[str] = field(default_factory=list)
    runtime_paths: list[str] = field(default_factory=list)
    serve_entry: str = ""
    missing_sources: list[str] = field(default_factory=list)
    empty_globs: list[str] = field(default_factory=list)
    excluded: int = 0
    workdir: str = "/"


#: 「构建期**写**这个文件」的样子，只认两种：
#: ① 一个**写调用**（`writeFileSync`/`tee`/`cp`…）后面跟的**被引号括起来的文件名**；
#: ② 一个**重定向目标**（`cmd > out.txt`，要求 `>` 前面有空白）。
#: 为什么必须挂在「写」上，而不是「RUN 里出现过这个名字」：`remotion/Dockerfile` 那条
#: 1574 字符的 RUN 里 `require('./package.json')`、`import('./lint.mjs')`、
#: `import('./lint-rules.mjs')` 都是**读**——按「出现过」认，这三件会被判成「构建期重写」
#: 而**静默退出 md5 对账**（`lint-rules.mjs` 正是好几条用例的被测件）。
#: 两处踩过的坑都记在这儿，免得有人再放宽一次：
#:   · 把 `>` 当子串找 → JS 的箭头函数 `(m) => {` 里的 `>` 命中，窗口一滑就捞到
#:     下一个 `require('./package.json')`；所以重定向必须是**正则要求 `>` 前有空白**。
#:   · 只看「带引号的点号文件名」→ `execSync(..., {encoding:'utf8'})` 这类参数也算，
#:     所以引号那一支必须挂在写调用之后。
#: 漏认的后果是「多报一条内容不一致」（人看得见的一条红，可用 `--generated` 补名，
#: 且补名要在 RUN 原文里查得到，见 `main`），不是静默少查一件。
_WRITE_CALL_RE = re.compile(
    r"\b(?:writeFileSync|writeFile|appendFileSync|createWriteStream|tee|touch)\b"
    r"|(?<=\s)(?:cp|mv|sed -i)\s")
#: 写调用之后这个窗口里找文件名。够跨过 `fs.writeFileSync(` 与 `JSON.stringify(`，
#: 又不至于把整条 RUN（几百字符）都吞进来。
_WRITE_WINDOW = 240
_QUOTED_NAME_RE = re.compile(r"""['"]([\w.\-/]+\.\w+)['"]""")
_REDIRECT_RE = re.compile(r"\s>>?\s*([\w.\-/]+\.\w+)")


def generated_basenames(stage: Stage) -> dict[str, int]:
    """本阶段 `RUN` 里**写**过的文件名（basename）→ 那一行的行号。"""
    out: dict[str, int] = {}
    for no, text in stage.runs:
        for m in _WRITE_CALL_RE.finditer(text):
            q = _QUOTED_NAME_RE.search(text[m.end():m.end() + _WRITE_WINDOW])
            if q:
                out.setdefault(q.group(1).rsplit("/", 1)[-1], no)
        for m in _REDIRECT_RE.finditer(text):
            out.setdefault(m.group(1).rsplit("/", 1)[-1], no)
    return out


#: 服务**运行期**写进自己源码树里的件：`server.mjs` 里
#: `const SHOT_FILE = path.join(ROOT, 'src', 'user-shots', 'shot.tsx')`，随后
#: `fs.writeFile(SHOT_FILE, code)` 把它覆写成这一镜的镜头代码。于是**跑过任何一次渲染的
#: 容器里**，这一件必然与宿主不同——比出来是一条假红；而静默跳过又是「少查一件」。
#: 第三条路（同「构建期重写」那一栏）：**不进对账、但逐条点名**，而且**仍然要求它在**
#: （`src/entry.tsx` 静态 import 它，缺了这一件连 bundle 都起不来）。
#:
#: 名单**不手写**，从 Dockerfile 的 `CMD` 指到的那个服务入口里现读——和清单本身从
#: COPY 现读是同一个理由：手写的名单会在服务改了落点之后静静地过期。
_RUNTIME_CONST_RE = re.compile(
    r"const\s+([A-Za-z_$][\w$]*)\s*=\s*path\.join\(\s*ROOT\s*,"
    r"\s*((?:['\"][^'\"]+['\"]\s*,\s*)*['\"][^'\"]+['\"])\s*\)")
_RUNTIME_SEG_RE = re.compile(r"['\"]([^'\"]+)['\"]")


def serve_entry(stage: Stage) -> str:
    """最终阶段 `CMD` 里的服务入口（`CMD ["node", "server.mjs"]` → `server.mjs`）。

    只认 **exec 形式**（`CMD ["node", "server.mjs"]`）与「node 起一个脚本」这一种：
    app 那条 `CMD python run_server.py` 返回空串——那一腿的服务不往自己源码树里写文件，
    这一栏就该是空的，而不是去猜一个文件名回来。
    """
    cmd = stage.cmd.strip()
    if cmd.startswith("["):
        toks = [t.strip().strip('"\'') for t in cmd.strip("[]").split(",")]
    else:
        toks = cmd.split()
    if len(toks) >= 2 and toks[0].rsplit("/", 1)[-1] in ("node", "nodejs"):
        # 取**紧跟解释器的那个脚本**，不是最后一个实参：`node a.mjs b.mjs` 里
        # `b.mjs` 是传给脚本的参数，把它当入口去读源码，读到的是一份不相干的东西
        # （这里是**读得到就看得见**的错，真落到「运行期改写」那一栏就成了假红）。
        for tok in toks[1:]:
            if tok.startswith("-"):
                continue
            return tok if tok.endswith((".js", ".mjs", ".cjs")) else ""
    return ""


def runtime_written(js_text: str) -> list[str]:
    """服务入口的源码里，**运行期会被写**的那些文件（相对入口文件所在目录的 posix 路径）。

    只认「`const X = path.join(ROOT, 'a', 'b')` **且** X 出现在一个写调用里」——
    与「构建期重写」同一套纪律：挂在**写**上，不挂在「出现过这个名字」上
    （`ROOT` 上还挂着 `src/FilmLayout.tsx`、`package-lock.json` 这些只读件）。
    """
    out: list[str] = []
    for m in _RUNTIME_CONST_RE.finditer(js_text):
        name, segs = m.group(1), m.group(2)
        if not re.search(r"\b(?:writeFile|writeFileSync|appendFile|appendFileSync"
                         r"|createWriteStream|rm|unlink)\s*\(\s*"
                         + re.escape(name) + r"\b", js_text):
            continue
        parts = _RUNTIME_SEG_RE.findall(segs)
        if parts:
            out.append("/".join(parts))
    return out


def plan(dockerfile_text: str, context: Path,
         ignore: list[str] | None = None) -> Plan:
    """Dockerfile + 构建上下文 → 对账清单。**只读盘，不碰 docker**（离线可测）。"""
    ignore = ignore or []
    stages = parse_stages(dockerfile_text)
    out = Plan()
    if not stages:
        out.missing_sources.append("Dockerfile 里一条 FROM 都没有")
        return out
    final = stages[-1]
    out.workdir = final.workdir
    gen = generated_basenames(final)

    # 运行期改写那一栏：从最终阶段 CMD 点名的服务入口现读。
    runtime: set[str] = set()
    entry = serve_entry(final)
    out.serve_entry = entry
    if entry:
        entry_host = context / entry
        if entry_host.is_file():
            runtime = set(runtime_written(entry_host.read_text(encoding="utf-8")))
            out.runtime = sorted(runtime)
        else:
            out.missing_sources.append(
                f"CMD 点名的服务入口在宿主上找不到：{entry}——「运行期改写」那一栏"
                f"没法现读，这一趟就少了那一栏的判据")

    def _add(host: Path, rel_ctx: str, image: str) -> None:
        if rel_ctx in runtime:
            # 一件都不许再进 md5 对账，但它**仍然要在**（存在性另判，见 main）。
            out.runtime_files.append(f"{rel_ctx} → {image}（服务运行期会覆写它："
                                     f"{out.serve_entry} 里那个 path.join(ROOT, …)）")
            out.runtime_paths.append(image)
            return
        line = gen.get(host.name)
        if line is not None:
            out.generated.append(f"{rel_ctx} → {image}（最终阶段第 {line} 行的 RUN 重写过它）")
            return
        out.pairs.append((host, rel_ctx, image))

    for stage in stages:
        for cp in stage.copies:
            where = f"{stage.base or 'FROM'}#{stage.index}:{cp.line}"
            if stage is not final:
                # 构建阶段的 COPY 只活在中间层：进不了最终镜像，也就不参与 md5 对账。
                # 但它们不是「没查」——它们是「不属于这一趟的判据」，单独报数。
                out.staged.append(f"{where} COPY {' '.join(cp.srcs)} → {cp.dest}")
                continue
            if cp.from_stage:
                # 存在性那一栏去掉尾部斜杠：`/app/dist/` 与 `/app/dist` 是同一个目录，
                # 而 `test -d /app/dist/` 与 `test -d /app/dist` 也是一回事——把它留着
                # 只会让「同一个目录判了两遍」这种假差异混进清单。
                out.presence.append(_join_image(cp.dest, final.workdir).rstrip("/") or "/")
                continue
            multi = _dest_is_dir(cp.dest, len(cp.srcs))
            for src in cp.srcs:
                globbed = any(ch in src for ch in "*?[")
                matches = sorted(context.glob(src)) if globbed else [context / src]
                if globbed and not matches:
                    out.empty_globs.append(f"{where} COPY {src}：宿主上一条都没匹配到")
                    continue
                for match in matches:
                    if not match.exists():
                        out.missing_sources.append(f"{where} COPY 源不存在：{src}")
                        continue
                    if match.is_dir():
                        for f in sorted(match.rglob("*")):
                            if not f.is_file():
                                continue
                            rel_ctx = f.relative_to(context).as_posix()
                            if is_ignored(rel_ctx, ignore):
                                out.excluded += 1
                                continue
                            tail = f.relative_to(match).as_posix()
                            _add(f, rel_ctx, _join_image(cp.dest, final.workdir, tail))
                    else:
                        rel_ctx = match.relative_to(context).as_posix()
                        if is_ignored(rel_ctx, ignore):
                            out.excluded += 1
                            continue
                        tail = match.name if multi else ""
                        _add(match, rel_ctx, _join_image(cp.dest, final.workdir, tail))
    return out


# ---------------------------------------------------------------------------
# 取证：宿主 md5 ↔ 容器 md5
# ---------------------------------------------------------------------------

def host_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _docker(args: list[str], *, stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["docker", *args], input=stdin, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError) as e:
        return 127, "", f"docker 不可用：{e}"
    return p.returncode, p.stdout or "", p.stderr or ""


def container_alive(container: str) -> tuple[bool, str]:
    rc, out, err = _docker(["inspect", "-f", "{{.State.Running}}", container])
    if rc != 0:
        return False, (err.strip() or out.strip() or "docker inspect 失败")
    return out.strip() == "true", ""


def container_md5(container: str, paths: list[str],
                  batch: int = _MD5_BATCH) -> tuple[dict[str, str], list[str]]:
    """逐批 `md5sum`：→ (路径→md5, 容器没给出来的路径)。

    容器给的路径是绝对路径，所以与工作目录无关；`md5sum` 对不存在的文件只在 stderr
    出声、不产生那一行——所以「没出现的路径」就是缺件，不会被当成空字符串混过去。
    """
    got: dict[str, str] = {}
    for i in range(0, len(paths), batch):
        chunk = paths[i:i + batch]
        rc, out, _err = _docker(["exec", container, "md5sum", "--", *chunk])
        if rc != 0 and not out.strip():
            continue          # 这一批一件都没读出来：整批算缺件（下面按 got 的差集报）
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            digest, _, name = line.partition("  ")
            if digest and name:
                got[name.strip()] = digest.strip()
    return got, [p for p in paths if p not in got]


def container_file_exists(container: str, path: str) -> bool:
    """镜像里有没有这个文件（「运行期改写」那一栏不比字节，但**必须在**）。"""
    rc, _out, _err = _docker(["exec", container, "test", "-f", path])
    return rc == 0


def container_dir_entries(container: str, path: str) -> tuple[int, str]:
    """目录里有多少个条目（`--from=` 阶段产物那几条用它判「非空」）。"""
    rc, out, err = _docker(["exec", container, "sh", "-c",
                            f"test -d {path!r} && ls -1 {path!r} | wc -l"])
    if rc != 0:
        return -1, (err.strip() or f"{path} 不是目录")
    try:
        return int(out.strip()), ""
    except ValueError:
        return -1, f"数不出来：{out.strip()!r}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # 判词里带「✓」「✗」这类字符：Windows 宿主控制台默认 GBK，直接 print 会
    # UnicodeEncodeError——对账脚本自己炸掉会把「没比过」伪装成「全对上了」。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:       # noqa: BLE001 —— 老解释器没有 reconfigure 就算了
        pass
    ap = argparse.ArgumentParser(description="镜像 COPY 完整性：Dockerfile 声明的每一件逐件比 md5")
    ap.add_argument("--dockerfile", default="Dockerfile")
    ap.add_argument("--context", default=".", help="构建上下文（compose 的 build 目录）")
    ap.add_argument("--container", required=True, help="要对的容器名（如 creation-app）")
    ap.add_argument("--ignore", default="", help=".dockerignore 路径（默认 <context>/.dockerignore）")
    ap.add_argument("--batch", type=int, default=_MD5_BATCH)
    ap.add_argument("--generated", action="append", default=[],
                    help="追加「构建期重写」名单（可重复）。这个名字必须能在最终阶段某条 "
                         "RUN 的原文里查得到——查不到就当场红：这个口子不能变成"
                         "「把碍事的文件都写进来」的静默豁免")
    ap.add_argument("--list-only", action="store_true", help="只打印清单，不连 docker")
    args = ap.parse_args(argv)

    ctx = Path(args.context).resolve()
    df = Path(args.dockerfile)
    if not df.is_file():
        print(f"未判：Dockerfile 不在（{df}）")
        return 2
    ig = Path(args.ignore) if args.ignore else ctx / ".dockerignore"
    text = df.read_text(encoding="utf-8")
    pl = plan(text, ctx, load_ignore(ig))
    # `--generated` 的守门：名字得在最终阶段的 RUN 原文里出现过。否则这个开关就成了
    # 「凡是对不上就写进来」的万能钥匙——而万能钥匙的绿与真绿打印出来一模一样。
    run_text = "\n".join(t for _n, t in parse_stages(text)[-1].runs)
    bogus = [n for n in args.generated if n not in run_text]
    for name in args.generated:
        if name in run_text and not any(name in g for g in pl.generated):
            pl.generated.append(f"{name}（由 --generated 指名，最终阶段的 RUN 原文里查得到）")

    print(f"== 清单来自 {df}（构建上下文 {ctx}，忽略表 {ig if ig.is_file() else '（无）'}）")
    print(f"   最终阶段 WORKDIR={pl.workdir}；逐件对账 {len(pl.pairs)} 件；"
          f"该存在的目录 {len(pl.presence)} 个；构建阶段（不进最终镜像）的 COPY "
          f"{len(pl.staged)} 条；构建期重写 {len(pl.generated)} 件；"
          f"运行期改写 {len(pl.runtime_files)} 件；"
          f"按 .dockerignore 排除 {pl.excluded} 件")
    for s in pl.staged:
        print(f"   · 阶段内：{s}")
    for g in pl.generated:
        print(f"   · 构建期重写（本来就不该与宿主相同，逐条列出来给人看）：{g}")
    for r in pl.runtime_files:
        print(f"   · 运行期改写（跑过一次服务它的字节就变了，比 md5 没有意义——"
              f"但下面单独判了「它还在」）：{r}")
    bad = False
    for name in bogus:
        print(f"   ✗ --generated {name}：最终阶段的 RUN 原文里查不到这个名字——"
              f"「构建期重写」这句话没有依据，按红处理")
        bad = True
    for src in pl.missing_sources:
        print(f"   ✗ {src}")
        bad = True
    for g in pl.empty_globs:
        print(f"   ✗ {g}")
        bad = True
    if args.list_only:
        for host, rel, image in pl.pairs:
            print(f"   {rel} → {image}")
        return 1 if bad else 0

    alive, why = container_alive(args.container)
    if not alive:
        print(f"未判：容器 {args.container} 没在跑（{why}）——没查过不等于查过都对")
        return 2

    wanted = [image for _h, _r, image in pl.pairs]
    got, absent = container_md5(args.container, wanted, batch=args.batch)
    mismatch: list[str] = []
    for host, rel, image in pl.pairs:
        digest = host_md5(host)
        seen = got.get(image)
        if seen is None:
            continue                       # 缺件在 absent 那一栏报，不在这儿报两遍
        if seen != digest:
            mismatch.append(f"{rel} → {image}（宿主 {digest[:12]} ↔ 镜像 {seen[:12]}）")

    print(f"== 逐件对账：{len(pl.pairs) - len(absent) - len(mismatch)}/{len(pl.pairs)} 件对上")
    for m in mismatch[:20]:
        print(f"   ✗ 内容不一致：{m}")
    for a in absent[:20]:
        print(f"   ✗ 镜像里没有：{a}")
    if len(absent) > 20 or len(mismatch) > 20:
        print(f"   （上面只列前 20 条：不一致 {len(mismatch)} 条、缺件 {len(absent)} 条）")

    pres_bad = False
    for p in pl.presence:
        n, err = container_dir_entries(args.container, p)
        if n < 0:
            print(f"   ✗ --from= 阶段产物没落进镜像：{p}（{err}）")
            pres_bad = True
        elif n == 0:
            print(f"   ✗ --from= 阶段产物是**空目录**：{p}——「建了目录」不等于「产物进来了」")
            pres_bad = True
        else:
            print(f"   ✓ --from= 阶段产物在：{p}（{n} 个条目）")

    rt_bad = False
    for p in pl.runtime_paths:
        if not container_file_exists(args.container, p):
            print(f"   ✗ 运行期改写那一件在镜像里**没有**：{p}——它的字节不比，但它必须在"
                  f"（服务入口静态 import 它，缺了连 bundle 都起不来）")
            rt_bad = True

    red = bad or bool(absent) or bool(mismatch) or pres_bad or rt_bad
    print(f"== 结论：{'红' if red else '逐件对上'}"
          f"（缺件 {len(absent)}、内容不一致 {len(mismatch)}、"
          f"该存在的目录 {'有缺' if pres_bad else '齐'}、"
          f"运行期改写件 {'有缺' if rt_bad else '在'}）")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
