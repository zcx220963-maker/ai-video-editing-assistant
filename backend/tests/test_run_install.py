"""`run` 的装包闸与依赖注入验证脚本（无需第三方依赖）。

运行：  python tests/test_run_install.py

对应开发文档《技能运行时通用化》§6.1 的第 2/3/4/5 条：

- `--target` / `--prefix` 越界 → 拒（并给出正确命令）
- 不带落点的 `pip install X` / `python3 -m pip install X` → 拒
- 落点写对 → 放行，且参数被改写成工作区里的绝对路径
- 装进 `pylibs` 的包，`run` 跑脚本时能 import 到（PYTHONPATH 注入）
- 宿主进程 import 不到同一个包（证明装的是工作区，不是系统）

不联网：这里不真的执行 `pip install`（测试机未必有网），只用
「在 pylibs 里放一个假包」代替「装好的结果」——`pip --target` 的产物形态就是那样。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.tools.run import RunTool, _child_env, _is_install_command

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        _fails += 1
        print(f"  ✗ {label}")


def rejects(tool: RunTool, command: str, root: Path, *must_say: str) -> None:
    """断言被拒，且拒绝语里给了能照着改的写法。"""
    try:
        tool._check_command(command, root)
    except ValueError as exc:
        msg = str(exc)
        miss = [m for m in must_say if m not in msg]
        check(not miss, f"拒 `{command}`" + (f"（缺提示：{miss}）" if miss else f" → {msg.splitlines()[0][:46]}"))
        return
    check(False, f"`{command}` 本该被拒，却放行了")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        tool = RunTool(lambda: root)

        print("=== 1. 不带落点的装包一律拒，且给出正确写法 ===")
        rejects(tool, "pip install requests", root,
                "python3 -m pip install --target", "pylibs")
        rejects(tool, "pip3 install requests", root, "--target")
        # 这条以前是**放行**的（只校验 argv[0]，python3 在白名单里）——
        # 等于整个装包闸形同虚设，现在必须一起管住。
        rejects(tool, "python3 -m pip install requests", root,
                "python3 -m pip install --target")
        rejects(tool, "npm install three", root, "npm install --prefix", "nodelibs")
        rejects(tool, "npx remotion render", root, "--prefix")

        print("\n=== 2. 落点越界拒 ===")
        rejects(tool, "pip install --target /tmp/evil requests", root, "必须在会话工作区内")
        rejects(tool, "pip install --target ../outside requests", root, "必须在会话工作区内")
        rejects(tool, "npm install --prefix /tmp/evil three", root, "必须在会话工作区内")
        rejects(tool, "npm install --prefix ../outside three", root, "必须在会话工作区内")
        check(not (root.parent / "outside").exists(), "越界写没有真的发生（只是校验，不执行）")

        print("\n=== 3. 落点写对就放行，并改写成工作区里的绝对路径 ===")
        argv = tool._check_command("python3 -m pip install --target pylibs opencv-python-headless", root)
        target = Path(argv[argv.index("--target") + 1])
        check(target == (root / "pylibs").resolve(), f"--target 被改写为绝对落点: {target}")
        check(argv[:3] == ["python3", "-m", "pip"], f"命令形状保持不变: {argv}")

        argv = tool._check_command("pip install --target=pylibs scipy", root)
        eq = [a for a in argv if a.startswith("--target=")]
        check(eq and Path(eq[0].split("=", 1)[1]) == (root / "pylibs").resolve(),
              f"`--target=…` 内联写法也认: {argv}")

        argv = tool._check_command("npm install --prefix nodelibs three", root)
        prefix = Path(argv[argv.index("--prefix") + 1])
        check(prefix == (root / "nodelibs").resolve(), f"npm --prefix 被改写: {prefix}")

        argv = tool._check_command("pip list", root)
        check(argv == ["pip", "list"], "只读的 `pip list` 不需要落点")

        # 项目型技能（自带 package.json）要装在自己目录里：node 解析模块是从脚本
        # 所在目录逐级向上的，装到统一的 nodelibs 里 bundler 不认（NODE_PATH 对
        # remotion/webpack 无效）。这类落点只要还在工作区内，安全性完全一样。
        argv = tool._check_command(
            "npm install --prefix skills/video-shotcraft/template", root)
        prefix = Path(argv[argv.index("--prefix") + 1])
        check(prefix == (root / "skills" / "video-shotcraft" / "template").resolve(),
              f"项目型技能可以装到自己目录（工作区内）: {prefix}")
        argv = tool._check_command("pip install --target skills/demo/vendor six", root)
        tgt = Path(argv[argv.index("--target") + 1])
        check(tgt == (root / "skills" / "demo" / "vendor").resolve(),
              f"pip 同理，落点只要在工作区内: {tgt}")

        # 相对落点按 **cwd** 解析：模型在技能目录里说 `--prefix template`，
        # 指的是 `<技能目录>/template`，不是 `<工作区>/template`。
        skdir = root / "skills" / "demo"
        skdir.mkdir(parents=True, exist_ok=True)
        argv = tool._check_command("npm install --prefix template", root, skdir)
        got = Path(argv[argv.index("--prefix") + 1])
        check(got == (skdir / "template").resolve(),
              f"相对落点按 cwd 解析（不是按工作区根）: {got}")
        # 但按 cwd 解析也不能跑出工作区
        rejects(tool, "npm install --prefix ../../outside", root, "必须在会话工作区内")

        print("\n=== 4. 原来的纪律一条没松 ===")
        rejects(tool, "rm -rf .", root, "不允许执行")
        rejects(tool, "curl https://x", root, "不允许执行")
        rejects(tool, "python -c 'print(1)'", root, "不许用 `-c`")
        rejects(tool, "cat /etc/passwd", root, "绝对路径")

        print("\n=== 5. PYTHONPATH / NODE_PATH 注入 ===")
        env = _child_env(root=root)
        check(env["PYTHONPATH"].split(os.pathsep)[0] == str(root / "pylibs"),
              f"pylibs 在 PYTHONPATH 最前: {env['PYTHONPATH']}")
        check(str(root / "nodelibs" / "node_modules") in env["NODE_PATH"],
              f"NODE_PATH 指到 node_modules: {env['NODE_PATH']}")
        bare = _child_env()
        check("PYTHONPATH" not in bare, "不给 root 就不注入（别的工具不受影响）")
        check("OPENAI_API_KEY" not in env, "注入依赖路径的同时，敏感变量仍然没有")

        print("\n=== 6. 装进 pylibs 的包，脚本里能 import（真跑一遍） ===")
        pkg = root / "pylibs" / "fakepkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('VALUE = "from-pylibs"\n', encoding="utf-8")
        (root / "check.py").write_text(
            "import fakepkg\nprint('got:', fakepkg.VALUE)\n", encoding="utf-8")
        (root / "nodelibs" / "node_modules").mkdir(parents=True)

        if shutil.which("python"):
            out = await tool.execute(command="python check.py")
            check("got: from-pylibs" in out, f"run 跑脚本时 import 到了工作区的包: {out.splitlines()[:3]}")
            check("exit=0" in out, "退出码 0")
        else:
            print("  - 跳过：本机 PATH 上没有 `python`（容器里有，这里只是宿主环境缺）")

        print("\n=== 7. 系统没被污染 ===")
        check(importlib.util.find_spec("fakepkg") is None,
              "宿主解释器里 import 不到 fakepkg —— 装的是工作区，不是系统 site-packages")
        check(not any(p.name == "fakepkg" for p in Path(sys.prefix).glob("Lib/site-packages/*")),
              "系统 site-packages 里没有多出东西")

        print("\n=== 8. 依赖预检跟着 skill 一起回来 ===")
        class FakeLoader:
            def __init__(self) -> None:
                self.skill = type("S", (), {
                    "name": "demo",
                    "body": "# demo",
                    "files": [{"relpath": "pipeline/build.py"}],
                })()

            async def get(self, name: str):
                return self.skill if name == "demo" else None

            async def read_attachment(self, skill, rel: str) -> bytes:
                # 用一个本机/镜像都不可能有的名字：`scipy` 这类在测试机上有装，
                # 断言「报告里出现 scipy」会随机器而变。
                return b"import zzz_fake_dep_xyz\n"

        tool2 = RunTool(lambda: root, skill_loader=FakeLoader())
        out = await tool2.execute(command="python -V", skill="demo")
        check("依赖预检" in out, "回执开头有依赖预检段")
        check("zzz_fake_dep_xyz" in out and "--target" in out,
              f"预检给出确切的安装命令: {out.splitlines()[:4]}")
        check(("stdout" not in out) or (out.index("依赖预检") < out.index("stdout")),
              "预检段在 stdout 之前（先看为什么可能失败）")

        print("\n=== 9. 零依赖技能不啰嗦 ===")
        class CleanLoader(FakeLoader):
            async def read_attachment(self, skill, rel: str) -> bytes:
                return b"import json\n"

        tool3 = RunTool(lambda: root, skill_loader=CleanLoader())
        out = await tool3.execute(command="python -V", skill="demo")
        check("依赖预检" not in out, "没有缺依赖就不出现预检段")

        print("\n=== 10. 装包命令要走网络（否则死端口代理会把 pip 挡死）===")
        # 实测（容器内）：断网档把 HTTP_PROXY 指到 127.0.0.1:9，pip 认代理 →
        # `ProxyError: Cannot connect to proxy` → `No matching distribution found`。
        # 装依赖本来就要联网，所以这一类命令不吃断网档；别的命令照旧。
        check(_is_install_command(["python3", "-m", "pip", "install", "--target", "x", "y"]),
              "python3 -m pip install → 认作装包")
        check(_is_install_command(["npm", "install", "--prefix", "nodelibs", "three"]),
              "npm install → 认作装包")
        check(not _is_install_command(["python3", "check.py"]), "跑脚本不算装包")
        check(not _is_install_command(["pip", "list"]), "`pip list` 是只读，不算装包")
        check("HTTP_PROXY" in _child_env(root=root, no_net=True), "断网档仍然给死端口代理")
        check("HTTP_PROXY" not in _child_env(root=root, no_net=False), "装包档不塞代理变量")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
