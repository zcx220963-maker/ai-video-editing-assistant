# -*- coding: utf-8 -*-
"""简洁视频线的**边界**测试——钉住"它不会长成第二条屎山"。

这个文件不测功能（功能由 `tools/smoke_simple.py` 端到端跑），只测三件事：

1. `simple/` 下任何文件**不许** import 分镜链（`code_spec` / `nodes` / `storage.repositories`）；
2. `simple/` 下**不许**出现闸调用（`check_*`）；
3. 内核三个文件合计**不超过 500 行**。

第 1 条用 **AST 读 import 语句**，不用子串匹配——`__init__.py` 的说明文字里就写着
"不许 import code_spec"，子串匹配会把注释当成违规（第一版正是这么误报的）。

为什么值得单列一个测试文件：这三条是**可执行**的边界。架构文档管不住人，
`assert` 管得住——哪天有人想让简洁线"顺手复用一下 spec"，这里会先红。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SIMPLE = BACKEND / "storyline_server" / "simple"
API = BACKEND / "agent_framework" / "simple_api.py"

#: 内核三件的行数预算。2026-10-25 实测 301 行，给到 500 是留足余量。
#: 这条数不是拍脑袋：超了通常意味着有人在往里塞"顺手也做一下"的东西。
#: 2026-10-26 提到 510：`kernel.stills_pairs` 要把渲染时浏览器控制台的 error/warning
#: 一并带回（`warnings`），给"静帧一片黑"一个可定位到 file:line 的线索——这不是
#: "顺手也做一下"，是 18/23 空镜那轮唯一能查根因的通道。kernel 由 368 → 371。
CORE_LINE_BUDGET = 510

#: Python 逻辑（不含 system.py 的 TSX 载荷）预算。
#:
#: 2026-10-25 四次数：加逐帧质检后 1,043；加转场 + 饱和度判据后 1,235；
#: 加"工程落盘 + 改一镜"后 1,614；加音轨（`voice.py` 与 film 的旁白分支）后 1,957，
#: 预算提到 2,050。
#: 这条数的意义不是"越小越好"，是"超了就得解释"。最近这 722 行买了什么：
#:   ① `TransitionSeries` 的时间表算术（重叠帧 / 切点中点 / 四舍五入零头）
#:   ② 饱和度判据、`ensure_vivid` 主色提纯、元素数判据（`sparse`）
#:   ③ `project.py`：工程落盘 —— **没有它就只能整条重跑**，改一镜是 24 次调用
#:   ④ `film.reshoot` / `shot_stills`：只重写一镜 + 只渲这一镜的静帧
#:   ⑤ `voice.py`：TTS → 按语音定镜长 → 铺时间轴 → 与画面合流（有声/无声是两个物种）
#: 2026-10-25 五次数：加"插画叙事"这一整套后 2,799，预算提到 3,000。
#: 最近这 750 行买了什么：
#:   ⑥ `plate.py`（出图通道：三种后端 + 目录供图 + 推进渲染容器）
#:   ⑦ `film` 的 story 模式：叙事分镜、`write_scene_story_code`（每镜写完立刻 lint）、
#:      `wrap_story_scene`（外层统一套运镜与文字层）、`safe_story_scene`（兜底三套）、
#:      `_caption_fit`（长句字幕按字数调字号）
#:   ⑧ `qa.verdict(story=True)` 的另一套判据（不判饱和度与层次）
#:   ⑨ `voice` 的响度标准化与 `project` 的音轨字段
#: 2026-10-25 六次数：加"曲库配乐"后 3,238，预算提到 3,350。最近这 107 行买了什么：
#:   ⑩ `mix.py`（配乐：挑曲 + 铺满 + **侧链闪避** + 淡入淡出）+ `scene.py`（从 story
#:      拆出来的包装层）。顺带修掉一个真 bug：`amix` 缺省 `normalize=1` 把旁白除了
#:      2——"成片声音小"根因在这里，不在 `loudnorm`。
#: 2026-10-25 七次数：加"补留白 + ffmpeg 去重"后 3,351，预算提到 3,400。
#:   ⑪ `ffmpeg.py`（voice 与 mix 各抄一份 `_ffmpeg`，合成一份）+ `voice.fill_to()`
#:      （要 100 秒出来 69 秒，缺口摊成留白）。净增很少：去重抵掉了大半。
#: 2026-10-25 八次数：加"出处账"后 3,4xx，预算提到 3,500。
#:   ⑫ `facts.py`（每个数字能不能说清从哪来）。视觉自检看不出"这个数是编的"，
#:      而编一个数字是这条链路里最贵的错。**不做闸只做账**：不拦片，报告里如实写。
#:   ⑬ `scene.GRADE_OPACITY`（整片提亮层）。提示词写"亮度 ≥38/255"、模型稳定做到 27
#:      ——它对绝对亮度没有直觉，跟"对画面占多少没有直觉"是同一个毛病。补在最后一道
#:      确定性代码里，实测 YAVG 27.0 → 39.9（参考片 41.6）。
#: 2026-10-25 九次数：加"字幕三层"后 3,615，预算提到 3,700。
#:   ⑭ `caption.py`（字幕：字号按短边 + 五档位置 + overlay/drawn/none 三模式）。
#:      修的是三个真毛病：字号按宽度算导致横竖屏差三倍（1280 宽 48.6px vs 参考片 34px）、
#:      底部 36% 黑条把画面割开、要不要字幕外层说了算。
#: 2026-10-25 十次数：加"对照表 + 分阶段确认"后 3,723，预算提到 3,800。
#:   ⑮ `table.py`（对照表数据校验）+ `director.py`（两套分镜提示词从 film.py 搬出来）
#:      + `system.CompareTable`（设计系统第 29 件）。
#:      对照表是"数据→坐标"的映射，让模型每次重画必然歪（实测两次 `<text>` 都是 0 个，
#:      它只会把数据念进字幕）；而它是知识类视频信息密度最高的形态。
#:      提示词搬出去是因为 film.py 顶破了 1,115——拆的边界是"文本资产 vs 编排代码"。
#: 下一次再超，得说清买了什么才准提。
#: 2026-10-26 十一次数：加「读数回写环」后 3,856，预算提到 3,900。
#:   买的是：`qa.readout_fix()` 把 content/mean_lum 翻成写手能执行的诊断，
#:   写镜循环每轮把诊断并进 notes。此前评审 hint 限 40 字，传不出「填充色只差 8/255」，
#:   三轮重写后 26 镜仍然全近黑（content 0.02~0.18），成片 70% 是死镜。
#: 2026-10-26 十二次数：加「重写不许把画面写坏」后 3,916，预算提到 3,950。
#:   买的是：重写后只渲那一镜复检，变空就退回原版。此前 31 次重写把 23/27 镜
#:   写成 content=0.0 的纯背景（没被重写的 4 镜全正常），整片报废。
#: 2026-10-26 十三次数：定位到 18/23 空镜的**真根因**后 4,022，预算提到 4,060。
#:   买的是两件事：
#:   ⑯ `fixes.fix_svg_frame()`（约 45 行）：满幅 `<svg>` 补 `position:absolute; inset:0`。
#:      `<AbsoluteFill>` 是 flex 列容器，直接塞进去的 `<svg>` 是 flex item，
#:      width/height 表现属性在 flex 里不给它撑主尺寸 → 高度塌 0，整帧只剩背景。
#:      实测：把 s01 背景 rect 换亮红仍 content 0.0（整只 svg 没渲）；display:block 无效；
#:      只有 position:absolute 把 content 0.0 → 0.09。这就是 18/23 镜全空的根因。
#:   ⑰ 浏览器控制台回传链（server.mjs onBrowserLog → remotion_client.with_warnings →
#:      kernel.stills_pairs → shot_evidence.warnings → qa.readout_fix）：静帧渲黑时
#:      把 console.error 连同 file:line 带回，给模型和人都能直接定位。
PACKAGE_LINE_BUDGET = 4060

#: 不许出现在 import 里的东西。
BANNED = ("code_spec", "storyline_server.nodes", "storage.repositories",
          "review_loop", "design_review", "judge_channel", "art_director")

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    if cond:
        print(f"  OK  {label}")
    else:
        _FAILS += 1
        print(f"  XX  {label}")


def py_files() -> list[Path]:
    return sorted(p for p in SIMPLE.rglob("*.py") if not p.name.startswith("."))


def imported_modules(src: str) -> set[str]:
    """只读 import 语句——注释与 docstring 里的同名字不算违规。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"<parse-error>"}
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out.add(mod)
            for alias in node.names:
                out.add(f"{mod}.{alias.name}" if mod else alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
    return out


def main() -> int:
    files = py_files()
    print("\n[1] 不许 import 分镜链（读 import 语句，不读注释）")
    check(len(files) >= 4, f"simple/ 下有 {len(files)} 个 .py")
    for p in files:
        mods = imported_modules(p.read_text(encoding="utf-8", errors="replace"))
        hit = sorted({b for b in BANNED for m in mods if b in m})
        check(not hit, f"{p.name} 的 import 干净（违规：{hit or '无'}）")

    print("\n[2] 不许出现闸调用")
    for p in files:
        src = p.read_text(encoding="utf-8", errors="replace")
        calls = re.findall(r"\bcheck_[a-z0-9_]+\s*\(", src)
        check(not calls, f"{p.name} 没有闸调用（命中：{sorted(set(calls)) or '无'}）")

    print("\n[3] 内核体积")
    core = [p for p in files if p.name in ("kernel.py", "takes.py", "deliver.py")]
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in core)
    check(len(core) == 3, f"内核三件都在：{sorted(p.name for p in core)}")
    check(total < CORE_LINE_BUDGET,
          f"内核三件合计 {total} 行 < {CORE_LINE_BUDGET}")
    whole = sum(len(p.read_text(encoding="utf-8").splitlines())
                for p in files if p.name != "system.py")
    check(whole < PACKAGE_LINE_BUDGET,
          f"Python 逻辑合计 {whole} 行 < {PACKAGE_LINE_BUDGET}"
          "（system.py 是 TSX 载荷，单独算）")

    # **单文件上限**：总量预算管得住"加起来多少"，管不住"一个文件塞成屎山"。
    # 旧链就是 7 千行一个 code_spec.py。这里给每个文件一道硬顶（= 当前行数 + 很少余量），
    # 顶到了就必须**拆文件**，而不是把预算往上抬。
    #
    # 2026-10-25 第一次触发：film.py 1,363 行 → 拆出 story.py（叙事片的画法），
    # 第二次触发：加"视觉自检 + 导出工程"后 story/project 又顶到顶 →
    # 拆出 review.py（看自己画的一帧）与 export.py（导出可跑的 Remotion 工程）。
    # 这就是这道闸该有的样子：**长功能就拆文件，不改上限**。
    # 2026-10-25 第五次触发：`film.py` 加完"重写也要走这条片子自己的画法"之后
    # 1,178 行，顶破 1,165 → 拆出 `edit.py`（改一镜：读工程 → 重写第 N 镜 → 重新拼装
    # → 只复核第 N 镜）。**每次都是拆，没有一次抬上限。**
    print("\n[3a] 单文件上限（防「一个文件塞成屎山」）")
    for name, cap in (("film.py", 1115), ("edit.py", 115), ("story.py", 225),
                      ("scene.py", 215), ("review.py", 170), ("export.py", 230),
                      ("morph.py", 110), ("kernel.py", 380), ("qa.py", 310),
                      ("voice.py", 230), ("mix.py", 100), ("ffmpeg.py", 55),
                      ("caption.py", 135), ("table.py", 70), ("director.py", 100),
                      ("facts.py", 135), ("project.py", 160),
                      # system.py 是 TSX 载荷（29 件组件），加一件必然长——
                      # 它不是"塞屎山"，是"设计系统多了一件"。
                      ("system.py", 1150), ("takes.py", 60), ("deliver.py", 70)):
        p = next((x for x in files if x.name == name), None)
        if p is None:
            continue
        n = len(p.read_text(encoding="utf-8").splitlines())
        check(n <= cap, f"{name} {n} 行 ≤ {cap}（超了要拆文件，不许抬预算）")

    print("\n[3b] 设计系统的清单与实现必须一致")
    sys.path.insert(0, str(BACKEND))
    from storyline_server.simple import system as sysmod
    names = [m["name"] for m in sysmod.MANIFEST]
    missing = [n for n in names
               if f"const {n} =" not in sysmod.SYSTEM_TSX]
    check(not missing,
          f"{len(names)} 件组件都在 SYSTEM_TSX 里实现了（缺：{missing or '无'}）")
    check(len(names) >= 10, f"组件数 {len(names)} ≥ 10")

    print("\n[4] 接口层也只借不抄")
    src = API.read_text(encoding="utf-8", errors="replace")
    mods = imported_modules(src)
    check(not any("code_spec" in m for m in mods), "simple_api 不 import code_spec")
    check(not any("storyline_server.nodes" in m for m in mods),
          "simple_api 不 import 节点层")
    joined = src + "".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
    for mod, why in (("remotion_client", "渲染走现有容器"),
                     ("objects", "产物进现有对象存储"),
                     ("storage", "密钥/身份走现有存储层")):
        check(mod in joined, f"确实是借不是抄：{why}（{mod}）")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
