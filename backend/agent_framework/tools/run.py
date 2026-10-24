"""``run`` —— 在会话工作区里执行命令。

## 为什么要有它（2026-10-23）

用户的原话：「我要的就是原生的 agent，导入 skill 和 claude、codex 那样直接复用 skill
的全部能力，是动态的」。

把这条链和 Claude Code 摆在一起比，缺的只有一样东西：

| 能力 | Claude Code | 加这个文件之前 |
|---|---|---|
| 发现技能 / 读全文 / 读附件 | ✅ | ✅ `find_skill` `load_skill` `read_skill_file` |
| 读写文件 / 搜内容 | ✅ Write/Edit/Grep | ✅ `write_file` `edit_file` `grep` |
| **跑脚本** | ✅ **Bash** | ❌ 没有 |

技能包里那些 `.py` / `.mjs` / `.sh` 只能读不能跑，所以「导入即用」只到「读」那一半。
这个工具补的就是那一半。

## 四道防线（按重要性排）

1. **环境变量清空**。这是最要紧的一条：如果继承进程环境，技能脚本能读到
   `OPENAI_API_KEY`、PG 密码、MinIO 密钥——一句 `print(os.environ)` 就全漏了。
   只给 `PATH` / `HOME` / `LANG` / `TZ` / `PYTHONIOENCODING`。
2. **工作区围栏**。`cwd` 解析后必须仍在会话沙箱内；任何**绝对路径参数**直接拒。
   与 `read_file` / `write_file` 同一套 `_resolve`，不另写一份。
3. **解释器白名单**。只放行跑代码与看文件的那几个；`sudo` `mount` `docker` `kill`
   `rm` `rmdir` `chmod` `chown` 一律拒——技能脚本不需要它们，而它们一次误用就能把
   容器弄坏。
4. **超时 + 输出上限**。默认 60 秒、上限 300 秒；stdout/stderr 各截到 8 KB。
   一个死循环不该把整个会话拖住。

## 装依赖：只准装进工作区（2026-10-23 深夜）

原来 `pip` / `npm` 在 `BANNED_EXE` 里，理由是「运行期装的东西不进渲染容器」。
这条纪律对**渲染容器**成立，对「在 app 容器里跑脚本、自己调 ffmpeg/chromium 出片」
的技能（xilo / dashi / huashu）没影响。而外部技能的依赖没有上界（`cv2`、`three`、
`scipy`…），不可能全预装。

所以把它细化为：**允许装，但只准装进工作区**。

    pip  →  --target <工作区内任意目录>      （`-t` 同义；推荐 pylibs）
    npm  →  --prefix <工作区内任意目录>      （推荐 nodelibs）

装在工作区里：在命名卷上（重建容器不丢）、不在系统 site-packages（不污染镜像）、
随工作区一起被 TTL 清理。`_child_env` 再把 `pylibs` / `nodelibs` prepend 进
`PYTHONPATH` / `NODE_PATH`，技能脚本 `import cv2` 时自动找到，**一行都不用改**。

为什么落点不写死成 `pylibs` / `nodelibs`：**项目型技能**（自带 `package.json` 的，
比如 video-shotcraft 的 `template/`）必须把 `node_modules` 装在自己目录里——node
解析模块是从脚本所在目录逐级向上的，装到统一的 `nodelibs` 里，remotion / webpack
那类 bundler 根本不看 `NODE_PATH`。工作区里换个子目录，安全性一模一样。

为什么不用「直接放行 pip」：`pip install X` 写的是镜像层里的 site-packages，
重建就没了；而且那是「装了什么」无从审计的地方。落点固定在工作区，等于把
「每个会话装了什么」摊在磁盘上，看得见也删得掉。

## 产物接入：跑出来的图怎么进画面

跑完会扫一遍工作区里这一趟新写出的图片（`png/jpg/jpeg/webp/svg`），推到渲染容器的
`POST /assets`，落在 `public/assets/`，然后在回执里给出可直接粘的写法：

    - sun.svg → `staticFile('assets/1851f1-sun.svg')`
      用法：`<Img src={staticFile('assets/1851f1-sun.svg')} />`

放在 `run` 里做、不另开一个工具，是因为模型跑完脚本最关心的就是「我画出来的东西怎么
放进镜头」——答案直接出现在回执里，它下一句就能用。另起一个工具，它得先知道有那个
工具、再记住名字、再传对参数，三步都可能漏。

配套改了两处：渲染容器加了 `POST /assets`（名字与大小校验、原子落盘），lint 的
`dg/staticfile-font-only` 从「只许 `fonts/`」放宽成「`fonts/` 或 `assets/*.png|jpg|webp|svg`」
——`.html` / `.json` 仍然拒，它们不是画出来的东西。

## 断网：尽力，不是保证

* 先探 `unshare -n`：能建独立网络命名空间就真断（子进程连 PG / MinIO 都看不到）。
  容器默认没有 `CAP_SYS_ADMIN`，实测多数环境建不了。
* 建不了就退到「代理变量指向死端口」：能拦住 `requests` / `urllib` 这类认代理的库，
  **拦不住 Node 的 `fetch`**。
* 回执里每次都明说这一趟到底断没断——别让人以为断了。

真要硬断网，在 compose 层做，别在进程里做：

    services:
      app:
        networks: [internal]        # internal: true 的网络没有出口
      # PG / MinIO 放在同一个 internal 网络里，app 照样连得到它们
    networks:
      internal:
        internal: true

## 不能保证确定性

脚本里一个 `Date.now()` 或 `random()` 就毁了缓存键与八道闸。这是 `run` 与「出片」之间
那道坎——跑出来的东西要进片子，得自己保证可复现。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .file import FileToolBase, RootProvider
from .skill_deps import (
    NODELIBS_DIR,
    PYLIBS_DIR,
    deps_report,
    format_report,
    has_work,
    libs_env,
)

#: 允许执行的程序。只放「跑代码」与「看文件」两类。
#:
#: 为什么把 `rm` / `rmdir` / `chmod` / `chown` / `mv` 挡在外面：技能脚本用不到它们，
#: 而它们一次误用就能把容器或工作区弄坏。要删东西让脚本用 `os.remove`——那至少
#: 得先写代码，不会是一行手滑。
#: 2026-10-23 晚加回 `ffmpeg` / `ffprobe`：七个外部技能（mg-styles-15、wtt、
#: dashi、xilo-opus-video…）**全部**用 ffmpeg 合成 mp4，而它一直不在白名单里——
#: 二进制容器里有（/usr/bin/ffmpeg，7.1.5），只是被这一行挡着。ffmpeg 只写文件、
#: 不执行代码，与 rm/mv 那类不是一回事。
#:
#: 2026-10-23 深夜再加 `pip` / `pip3` / `npm` / `npx`：外部技能的依赖无上界
#: （`cv2`、`three`、`scipy`…），不可能全预装进镜像。放开的**不是**「装到系统」，
#: 而是「装到工作区」——`_check_install` 只认 `--target <工作区>/pylibs` 与
#: `--prefix <工作区>/nodelibs` 两种形式，别的一律拒。这样既不污染镜像层，
#: 装进去的东西又在命名卷上，重建容器不丢。
ALLOWED_EXE: frozenset[str] = frozenset({
    "python", "python3", "node", "sh", "bash", "ffmpeg", "ffprobe",
    "ls", "cat", "head", "tail", "wc", "grep", "find", "sort", "uniq",
    "cut", "sed", "awk", "jq", "file", "du", "mkdir", "cp", "printf",
    "echo", "test", "true", "false", "date", "pwd", "which", "env",
    "pip", "pip3", "npm", "npx",
})

#: 明确拒绝的程序（命中时给一句「为什么不行」，而不是笼统的「不在白名单」）。
#: 被禁/未放行程序的**等价写法提示**（2026-10-23）。真机 run：模型执行裸 `pip`
#: 被拒，拒绝语只说「不允许」不说「可以怎么写」，于是它交替着重试不同写法，
#: 200 轮打满烧掉 ¥1.13。同一个可执行文件往往有放行的调用形式，说清楚它就不用瞎试。
#:
#: 装包相关的提示现在统一指向「带 --target / --prefix 的写法」——裸 `pip install X`
#: 仍然不行（那会写系统 site-packages），但改成带落点的写法就能过。
EXE_HINTS: dict[str, str] = {
    "pip": "装到工作区：`python3 -m pip install --target pylibs <包>`（落点必须是工作区里的 pylibs）",
    "pip3": "装到工作区：`python3 -m pip install --target pylibs <包>`",
    "npm": "装到工作区：`npm install --prefix nodelibs <包>`（落点必须是工作区里的 nodelibs）",
    "npx": "用 `npm install --prefix nodelibs <包>` 装好后 `node <脚本>.mjs`；"
           "确需 npx 时要带 `--prefix nodelibs`",
    "yarn": "用 `npm install --prefix nodelibs <包>` 代替",
    "pnpm": "用 `npm install --prefix nodelibs <包>` 代替",
}

BANNED_EXE: dict[str, str] = {
    "rm": "要删文件请在脚本里用 os.remove / fs.unlink；rm 一次手滑就能毁掉工作区",
    "rmdir": "同上，用脚本删",
    "mv": "改名请在脚本里做（os.rename），这样至少留下可审的代码",
    "chmod": "容器里不需要改权限",
    "chown": "容器里不需要改属主",
    "sudo": "没有提权通道，也不需要",
    "mount": "不能挂载",
    "docker": "容器里没有 docker",
    "kill": "不要动别的进程",
    "curl": "这条链离线运行，没有网络",
    "wget": "这条链离线运行，没有网络",
    "yarn": "只保留 npm 一条装包路径（落点固定在 nodelibs，yarn 的落点不可控）",
    "pnpm": "同上，用 npm install --prefix nodelibs",
}


#: 子进程环境：只给这几项。**绝不继承 os.environ**（见模块注释第 1 条）。
_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")

#: 「把代码当参数传」的那几个开关，一律拒。
#:
#: **这是这条白名单唯一的真实缺口**：`python -c "import os; os.system('rm -rf /')"`
#: 里那个 `-c` 的载荷不碰路径检查，白名单就变成了摆设。技能脚本本来就都是文件，
#: 所以要求「写成文件再跑」既不损失能力，又让每一段被执行的代码都留在磁盘上可审。
INLINE_CODE_FLAGS: frozenset[str] = frozenset({
    "-c", "-e", "--eval", "-p", "--print", "--exec",
})

MAX_TIMEOUT = 300
DEFAULT_TIMEOUT = 60
OUTPUT_CAP = 8_000


def _child_env(root: Path | None = None, no_net: bool = False) -> dict[str, str]:
    """子进程环境。

    `root` 给了就把工作区的依赖落点拼进 `PYTHONPATH` / `NODE_PATH`——这是
    「装一次就能 import」的关键一步：技能脚本里写的是 `import cv2`，不该为了
    迁就我们的落点改成 `sys.path.append(...)`。

    **只 prepend，不覆盖**：原有值留在后面，容器里预装的包照旧找得到。
    """
    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", "/tmp")
    # 编码固定：脚本 print 中文时不该因为容器 locale 崩掉
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if root is not None:
        env = libs_env(root, env)
    if no_net:
        # 退路：`unshare -n` 用不了时，把代理指向一个必然连不上的本地端口。
        # 能拦住 requests / urllib 这类认代理变量的库，**拦不住 Node 的 fetch**
        # ——所以它是「尽力」，不是保证。真断网要在 compose 层做（见模块注释）。
        dead = "http://127.0.0.1:9"
        env.update({"HTTP_PROXY": dead, "HTTPS_PROXY": dead,
                    "http_proxy": dead, "https_proxy": dead,
                    "ALL_PROXY": dead, "NO_PROXY": "", "no_proxy": ""})
    return env


#: `unshare -n` 能不能用（容器默认没有 CAP_SYS_ADMIN，实测多数环境不能）。
#: 只在第一次调用时探一次；结果缓存，避免每条命令都多起一个进程。
_NET_NS: list[bool | None] = [None]


def _net_namespace_available() -> bool:
    if _NET_NS[0] is not None:
        return bool(_NET_NS[0])
    try:
        got = subprocess.run(["unshare", "-n", "true"], capture_output=True, timeout=5)
        _NET_NS[0] = got.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _NET_NS[0] = False
    return bool(_NET_NS[0])


#: 脚本产出里能被画面直接用的扩展名。只收图片：`.html` 要浏览器截图、`.json` 不是画。
ASSET_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".svg")
ASSET_MAX_FILES = 8
_REMOTION_URL = "REMOTION_URL"
_REMOTION_DEFAULT = "http://remotion:8010"


def _is_install_command(argv: list[str]) -> bool:
    """这条命令是不是在**装包**——装包是唯一需要联网的那类命令。

    为什么单独认它：断网那一档（`unshare -n` 建不了时退到「代理指向死端口」）会
    把 `HTTP_PROXY` 指向 `127.0.0.1:9`，而 pip / npm **认代理**，于是装包必然失败
    （实测：`ProxyError: Cannot connect to proxy` → `No matching distribution found`）。
    装依赖本来就是要联网的事，对它放行网络是对的；别的命令照旧按断网档跑。
    """
    exe = Path(argv[0]).name
    if exe in ("npm", "npx"):
        return True
    if exe in ("pip", "pip3") or (exe in ("python", "python3") and argv[1:3] == ["-m", "pip"]):
        return any(a in ("install", "download", "wheel") for a in argv)
    return False


def _remotion_base() -> str:
    return (os.environ.get(_REMOTION_URL) or _REMOTION_DEFAULT).rstrip("/")


def _asset_name(path: Path) -> str:
    """素材名：`<6 位指纹>-<洗过的文件名>`。指纹防两个会话的同名文件互相覆盖。"""
    stem = path.stem.lower()
    clean = "".join(c if (c.isalnum() or c in "._-") else "-" for c in stem).strip("-_")
    clean = clean[:48] or "asset"
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:6]
    return f"{digest}-{clean}{path.suffix.lower()}"[:61]


def _clip(text: str, cap: int = OUTPUT_CAP) -> str:
    text = text or ""
    if len(text) <= cap:
        return text
    head = text[: int(cap * 0.7)]
    tail = text[-int(cap * 0.25):]
    return f"{head}\n…（中间省略 {len(text) - cap} 字符）…\n{tail}"


class RunTool(FileToolBase):
    """在会话工作区里执行一条命令（带解释器白名单、超时、输出截断）。"""

    def __init__(self, root_provider: RootProvider, skill_loader: Any = None) -> None:
        super().__init__(root_provider)
        self._skills = skill_loader

    @property
    def name(self) -> str:
        return "run"

    @property
    def display_name(self) -> str:
        return "运行命令"

    @property
    def description(self) -> str:
        return (
            "在本会话的工作区里执行一条命令，返回 stdout/stderr 与退出码。"
            "用来跑技能包里带的脚本（`.py` / `.mjs` / `.sh`）——传 `skill` 会先把那个技能的"
            "全部附件落到工作区 `skills/<技能名>/` 并把工作目录设到那里，"
            "所以 `run(command=\"python pipeline/build.py\", skill=\"某个技能\")` 一步就能跑。"
            "技能缺依赖时，回执开头会有一段「依赖预检」，给出确切的安装命令，照着跑一次即可："
            "`python3 -m pip install --target pylibs <包>` / `npm install --prefix nodelibs <包>`"
            "（只能装到会话工作区内，装完自动进 PYTHONPATH / NODE_PATH；"
            "技能自带 package.json 的，落点指到它自己的目录）。"
            "限制：只放行 python/node/sh/ffmpeg/包管理器与常见只读工具；cwd 与参数不许越出工作区；"
            "超时 60 秒（最多 300）；输出截到 8KB。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令，如 `python main.py --out a.png`",
                },
                "skill": {
                    "type": "string",
                    "description": "（可选）技能名。给了就先把该技能的文件落到工作区，"
                                   "并把工作目录设到 `skills/<技能名>/`",
                },
                "cwd": {
                    "type": "string",
                    "description": "（可选）相对工作区的目录；缺省是工作区根（或 skill 目录）",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": f"超时秒数，缺省 {DEFAULT_TIMEOUT}，上限 {MAX_TIMEOUT}",
                },
            },
            "required": ["command"],
        }

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        """会改磁盘，独占一批跑——两个脚本同时写同一个目录没人管得住。"""
        return True

    # ---------------------------------------------------------------- 校验

    #: pip 的写操作子命令。只有这些需要 `--target`；`pip list` / `pip show`
    #: 这类只读动作照旧放行（它们不写盘，也没有落点问题）。
    _PIP_WRITE_SUBS = frozenset({"install", "download", "wheel"})

    @staticmethod
    def _flag_value(argv: list[str], *names: str) -> tuple[int | None, str | None, bool]:
        """找 `--target X` / `--target=X` / `-t X`。返回 (下标, 值, 是否内联)。"""
        for i, arg in enumerate(argv):
            for name in names:
                if arg == name:
                    return i, (argv[i + 1] if i + 1 < len(argv) else None), False
                if arg.startswith(name + "="):
                    return i, arg[len(name) + 1:], True
        return None, None, False

    def _check_install(self, argv: list[str], root: Path | None,
                       work: Path | None = None) -> tuple[list[str], frozenset[int]] | None:
        """装包命令的落点校验；不是装包命令返回 `None`。

        **放开的不是「装包」，是「装到工作区里」**：

        - `pip … --target <工作区内任意目录>`（`-t` 同义，推荐 `pylibs`）
        - `npm … --prefix <工作区内任意目录>`（推荐 `nodelibs`）

        别的形式一律拒。理由是落点：装进系统 site-packages 会污染镜像层，
        而且重建容器就没了；装进工作区则在命名卷上，持久、可被 `PYTHONPATH`
        精确指向，还随工作区一起被 TTL 清理。

        为什么允许「任意子目录」而不只是 `pylibs` / `nodelibs`：项目型技能自带
        `package.json`，它的 `node_modules` 必须落在**项目目录里**——node 解析模块
        是从脚本所在目录逐级向上的，装到统一的 `nodelibs` 里 bundler 不认
        （`NODE_PATH` 对 webpack/remotion 那类 bundler 无效）。工作区内换个子目录，
        安全性完全一样。

        返回的第二个值是被放行的绝对路径下标——`--target` 的值天然是个绝对路径，
        不单独豁免的话会被下一道「参数不许是绝对路径」拦掉（那道检查是为了防止
        读写工作区之外的文件，而这里的目标已经在工作区里，性质不同）。
        """
        exe = Path(argv[0]).name
        if exe in ("pip", "pip3") or (exe in ("python", "python3") and argv[1:3] == ["-m", "pip"]):
            return self._check_pip(argv, root, work)
        if exe in ("npm", "npx"):
            return self._check_npm(argv, root, work)
        return None

    def _check_pip(self, argv: list[str], root: Path | None,
                   work: Path | None = None) -> tuple[list[str], frozenset[int]]:
        # `pip install …` 的子命令在下标 1；`python3 -m pip install …` 在下标 3。
        # 这两个偏移写错一次就整道闸失效（`pip` 会被当成子命令名，进而判成只读动作
        # 直接放行）——2026-10-23 的测试就是靠 `python3 -m pip install requests`
        # 没被拒抓出来的。
        is_module = (Path(argv[0]).name in ("python", "python3")
                     and argv[1:3] == ["-m", "pip"])
        head = 3 if is_module else 1
        sub = next((a for a in argv[head:] if not a.startswith("-")), "")
        if sub not in self._PIP_WRITE_SUBS:
            return list(argv), frozenset()          # pip list / show / freeze …
        if root is None:
            raise ValueError("装包要落在会话工作区里，但现在定位不到工作区。")
        want = (root / PYLIBS_DIR).resolve()
        idx, value, inline = self._flag_value(argv, "--target", "-t")
        if idx is None or not value:
            raise ValueError(
                "pip 装包必须指定落点：`python3 -m pip install --target pylibs <包>`。"
                f"（不带 --target 会写进系统 site-packages，污染镜像且重建即丢；"
                f"本会话的落点是 `{want}`）")
        target = Path(value)
        resolved = (target if target.is_absolute() else (work or root) / target).resolve()
        # 落点只要在**会话工作区内**就放行（默认推荐 `pylibs`）。
        # 为什么不再限死 `pylibs`：项目型技能（自带 `package.json` / `requirements.txt`）
        # 要的是「在它自己那个目录里装」——node 解析模块是从脚本所在目录逐级向上的，
        # 装到统一的 `nodelibs` 里，bundler 根本不认。2026-10-23 拿 video-shotcraft
        # 的 `template/` 试出来的。工作区内的任何路径，安全性完全一样（都在同一个沙箱、
        # 同一块命名卷、同一套 TTL），只是落点更自由。
        if not (resolved == root or root in resolved.parents):
            raise ValueError(
                f"pip 的落点必须在会话工作区内，收到的是 `{value}`。"
                f"常用写法：`python3 -m pip install --target {root / PYLIBS_DIR} <包>`；"
                f"技能自带的项目也可以指到它自己的目录（如 `--target skills/<名>/vendor`）。")
        out = list(argv)
        if inline:
            out[idx] = f"--target={resolved}"
            return out, frozenset({idx})
        out[idx + 1] = str(resolved)
        return out, frozenset({idx + 1})

    def _check_npm(self, argv: list[str], root: Path | None,
                   work: Path | None = None) -> tuple[list[str], frozenset[int]]:
        if root is None:
            raise ValueError("装包要落在会话工作区里，但现在定位不到工作区。")
        want = (root / NODELIBS_DIR).resolve()
        idx, value, inline = self._flag_value(argv, "--prefix")
        if idx is None or not value:
            raise ValueError(
                "npm 必须指定落点：`npm install --prefix nodelibs <包>`。"
                f"（默认会装到 `$HOME` 下，重建容器即丢；本会话的落点是 `{want}`）")
        target = Path(value)
        resolved = (target if target.is_absolute() else (work or root) / target).resolve()
        # 同上：工作区内任意目录都行。技能自带 `package.json` 时必须装到它自己的目录，
        # 否则 `node_modules` 不在模块解析路径上（`NODE_PATH` 对 bundler 不生效）。
        if not (resolved == root or root in resolved.parents):
            raise ValueError(
                f"npm 的落点必须在会话工作区内，收到的是 `{value}`。"
                f"常用写法：`npm install --prefix {root / NODELIBS_DIR} <包>`；"
                f"项目型技能指到它自己的目录（如 `--prefix skills/<名>/template`）。")
        out = list(argv)
        if inline:
            out[idx] = f"--prefix={resolved}"
            return out, frozenset({idx})
        out[idx + 1] = str(resolved)
        return out, frozenset({idx + 1})

    def _check_command(self, command: str, root: Path | None = None,
                       work: Path | None = None) -> list[str]:
        """校验一条命令。`work` 是这条命令的 cwd（缺省 = 工作区根）。

        `work` 只影响装包命令的相对落点：模型在 `skills/<名>/` 里跑
        `npm install --prefix template`，它心里的「当前目录」是技能目录；
        按工作区根去解析会静默装到 `<ws>/template`（那里没有 package.json，
        npm 什么都不装也不报错）。**相对落点一律按 cwd 解析**，才对得上它的直觉。
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"命令没法解析（引号没闭合？）：{exc}") from exc
        if not argv:
            raise ValueError("命令是空的")
        exe = Path(argv[0]).name
        if exe in BANNED_EXE:
            _hint = EXE_HINTS.get(exe)
            if _hint:
                raise ValueError(
                    f"`{exe}` 不允许执行：{BANNED_EXE[exe]}"
                    f"\n可以这样达到同样的事：{_hint}")
            raise ValueError(f"`{exe}` 不允许执行：{BANNED_EXE[exe]}")
        if exe not in ALLOWED_EXE:
            _hint = EXE_HINTS.get(exe)
            if _hint:
                raise ValueError(
                    f"`{exe}` 不在许可清单里。可以这样达到同样的事：{_hint}")
            raise ValueError(
                f"`{exe}` 不在允许清单里。可用的有："
                + "、".join(sorted(ALLOWED_EXE))
                + "。要用别的程序请在镜像构建期装好，并把它加进 `tools/run.py` 的 ALLOWED_EXE。")
        # 装包命令先过落点校验：它会把 `--target` / `--prefix` 的值改写成工作区里的
        # 绝对路径，那一个参数要免掉下面那道「不许绝对路径」的检查。
        if root is None:
            try:
                root = self.root()
            except Exception:            # noqa: BLE001 - 拿不到工作区时交给下面报错
                root = None
        exempt: frozenset[int] = frozenset()
        got = self._check_install(argv, root, work or root)
        if got is not None:
            argv, exempt = got
        bad: list[str] = []
        for i, arg in enumerate(argv[1:], start=1):
            if i in exempt:
                continue
            if arg.startswith("/") and not arg.startswith("/dev/null"):
                bad.append(arg)
            elif ".." in Path(arg).parts:
                bad.append(arg)
        if bad:
            raise ValueError(
                "这些参数是绝对路径或带 `..`，越出了会话工作区："
                + "、".join(bad[:3])
                + "。命令只能在当前工作区里读写文件。")
        # 内联代码开关：`-c` / `-e` 里的载荷不经过上面那两道检查，等于把白名单变成摆设。
        inline = [a for i, a in enumerate(argv[1:], start=1)
                  if i not in exempt and a in INLINE_CODE_FLAGS]
        if inline:
            raise ValueError(
                f"不许用 `{inline[0]}` 把代码当参数传：它绕过了路径与程序两道检查。"
                "用 write_file 把它写成一个文件（比如 `tmp/x.py`），再 `run(\"python tmp/x.py\")`。"
                "好处是这段代码留在磁盘上，下一次还能看、还能改。")
        return argv

    # ---------------------------------------------------------------- 技能物化

    async def _unpack_skill(self, name: str, root: Path) -> Path:
        """把技能的全部附件写到 `<工作区>/skills/<name>/`，返回那个目录。

        为什么需要这一步：技能附件在 MinIO 里，`read_skill_file` 是按需取字节；
        而脚本要的是磁盘上的一整个目录（`pipeline/render.mjs` 会 import 同目录的
        `./动效部件.js`）。不落地就永远只能读单个文件，跑不起来。
        """
        if self._skills is None:
            raise ValueError("本服务没接技能库，`skill` 参数不可用")
        skill = await self._skills.get(name)
        if skill is None:
            raise ValueError(f"没有这个技能：{name}")
        base = (root / "skills" / name).resolve()
        if root not in base.parents and base != root:
            raise ValueError(f"技能名不合法：{name}")
        for f in (skill.files or []):
            rel = str((f or {}).get("relpath") or "").replace("\\", "/").lstrip("./")
            if not rel or rel.split("/")[0] == "..":
                continue
            target = (base / rel).resolve()
            if base not in target.parents and target != base:
                continue                      # 越界条目直接跳过，不抛（一个坏条目不该毁整包）
            try:
                data = await self._skills.read_attachment(skill, rel)
            except Exception:                 # noqa: BLE001 - 单个附件读不到就跳过
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # SKILL.md 也落一份，脚本里常按它定位包根
        try:
            (base / "SKILL.md").write_text(str(skill.body or ""), encoding="utf-8")
        except OSError:
            pass
        # 解包完顺手做一次依赖预检：从**刚落盘的目录**读源码（不再从对象存储拉一遍），
        # 报告跟着执行结果一起回给模型。第一次跑脚本就看到「缺什么、跑哪条命令」，
        # 而不是撞了 ModuleNotFoundError 再靠猜。
        try:
            report = await deps_report(skill, root, base=base)
        except Exception:                     # noqa: BLE001 - 预检失败不该挡住执行
            report = {}
        return base, report

    # ---------------------------------------------------------------- 执行

    async def execute(self, command: str, skill: str = "", cwd: str = "",
                      timeout_sec: int = DEFAULT_TIMEOUT) -> str:
        root = self.root()
        work = root
        report: dict[str, Any] = {}
        if skill:
            work, report = await self._unpack_skill(str(skill).strip(), root)
        if cwd:
            work = self._resolve(cwd, work)
        if not work.is_dir():
            raise ValueError(f"工作目录不存在：{cwd or work}")
        # 校验放在确定 cwd 之后：装包命令的相对落点要按 cwd 解析（见 `_check_command`）。
        argv = self._check_command(command, root, work)
        # 依赖落点先建好：`pip --target` 自己会建，但 `NODE_PATH` 指向一个不存在的
        # 目录时 node 会直接忽略它，脚本报 `Cannot find module` 时根本看不出是路径问题。
        for libs in (PYLIBS_DIR, NODELIBS_DIR):
            try:
                (root / libs).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

        timeout = max(1, min(int(timeout_sec or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        # 断网：能建独立网络命名空间就真断（子进程连 PG/MinIO 都看不到）；建不了就退到
        # 「代理指向死端口」这一档，并在输出里明说——**别让人以为断了网**。
        # 装包命令例外：它必须联网，见 `_is_install_command`。
        isolated = _net_namespace_available()
        installing = _is_install_command(argv)
        run_argv = (["unshare", "-n", "--", *argv] if isolated else list(argv))
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *run_argv,
                cwd=str(work),
                env=_child_env(root=root, no_net=not isolated and not installing),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"找不到程序 `{argv[0]}`：{exc}") from exc

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return (f"Error: 命令超过 {timeout} 秒还没结束，已经杀掉。"
                    "长任务要拆小：先跑一步看输出，再跑下一步。")

        code = proc.returncode or 0
        stdout = _clip(out.decode("utf-8", "replace"))
        stderr = _clip(err.decode("utf-8", "replace"))
        parts = [f"$ {command}", f"（cwd: {work.relative_to(root) if work != root else '.'}）",
                 f"exit={code}"]
        # 依赖预检放在最前面：这一趟为什么可能失败，读第一段就知道，不用翻到 stderr 去猜。
        # 装包命令本身不附预检——那会变成「说缺 cv2」和「Successfully installed cv2」
        # 同时出现在一份回执里，读的人只会以为装失败了。
        if not installing and has_work(report):
            head = format_report(report)
            if head:
                parts.insert(0, "--- 依赖预检（"
                             + str(report.get("skill") or skill)
                             + f"，扫了 {report.get('scanned', 0)} 个源文件）---\n" + head)
        if stdout:
            parts.append("--- stdout ---\n" + stdout)
        if stderr:
            parts.append("--- stderr ---\n" + stderr)
        if not stdout and not stderr:
            parts.append("（没有任何输出）")
        if code != 0:
            parts.append("退出码非 0：先读 stderr 那一行，通常就是原因。"
                         "缺依赖照上面「依赖预检」那条命令装（装进工作区，不要装系统）；"
                         "预检没报缺而 stderr 里仍是 ModuleNotFoundError，说明这个包是"
                         "运行时才 import 的，用 `python3 -m pip install --target "
                         f"{root / PYLIBS_DIR} <包>` 补装。")
        parts.append("网络：" + ("已隔离（独立网络命名空间，连不上任何地址）" if isolated
                               else ("装包命令，按需要联网（装完不影响其它命令的断网档）"
                                     if installing else
                                     "**没有硬隔离**——代理变量指向死端口，认代理的库会失败，"
                                     "Node 的 fetch 仍可能出去。要真断网请在 compose 层做。")))
        # 产物接入：把这一趟新生成的图推给渲染容器，写手就能 staticFile('assets/…') 取到。
        pushed = await self._publish_assets(work, started)
        if pushed:
            parts.append("--- 这一趟产出的图，已可用于画面 ---\n" + "\n".join(pushed))
        return "\n".join(parts)

    async def _publish_assets(self, work: Path, started: float) -> list[str]:
        """把 `work` 下这一趟新写出的图片推到渲染容器的 `public/assets/`。

        为什么在 `run` 里做、不另开一个工具：模型跑完脚本最关心的是「我画出来的东西
        怎么放进镜头」，这时候把答案直接放在回执里，它下一句就能用；另起一个工具，
        它得先知道有那个工具、再记住名字、再传对参数——三步都可能漏。

        失败只记账、不抛：图没推上去不该让「脚本跑成功了」这件事变成失败。
        """
        try:
            files = [p for p in work.rglob("*")
                     if p.is_file() and p.suffix.lower() in ASSET_EXTS
                     and p.stat().st_mtime >= started - 1][:ASSET_MAX_FILES]
        except OSError:
            return []
        if not files:
            return []
        out: list[str] = []
        for path in files:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if not data or len(data) > 8 * 1024 * 1024:
                continue
            name = _asset_name(path)
            payload = json.dumps({"name": name,
                                  "b64": base64.b64encode(data).decode("ascii")}).encode("utf-8")
            req = urllib.request.Request(
                _remotion_base() + "/assets", data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    got = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
                out.append(f"- {path.name}：没推上去（{str(exc)[:80]}）")
                continue
            if not got.get("ok"):
                out.append(f"- {path.name}：没推上去（{got.get('error')}）")
                continue
            rel = str(got.get("path") or f"assets/{name}")
            out.append(f"- {path.name} → `staticFile('{rel}')`"
                       f"　用法：`<Img src={{staticFile('{rel}')}} />`")
        return out


def register_run_tool(registry: Any, root_provider: RootProvider,
                      skill_loader: Any = None) -> None:
    registry.register(RunTool(root_provider, skill_loader))
