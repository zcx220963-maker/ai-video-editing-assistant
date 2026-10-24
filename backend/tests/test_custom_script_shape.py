# -*- coding: utf-8 -*-
"""文案生成节点对 custom_script 的归一化：不许把 Python 字面量塞进字幕。

真机事故（用户原话「全部做完后没有反应了」）：
  模型调用 generate_script 时传了
      custom_script = [{"group": "group_0001", "text": "小白兔，遇见了它的春天。"},
                       {"group": "group_0002", "text": "风很轻，云很白，陪着你就好。"}]
  而当时的归一化是 ``"\\n".join(str(x) for x in raw)``——``str()`` 把每个 dict 变成
  Python 字面量串：``"{'group': 'group_0001', 'text': '小白兔…'}"``（单引号 + 花括号）。
  脏字符一路进了字幕（artifacts 里实测到 raw_text 就是这个形状），
  模型自己也报「文案生成节点坏了」，用户看到一屏 {'group': ...}，渲染反复失败。

这条用例钉住四种输入形状都归一成干净的「每行一句」。
不依赖真机：直接调节点里那个归一逻辑（与 process 同一处代码路径）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def normalize(raw_custom):
    """直接调用生产代码里的归一函数。

    不做「测试与生产各写一份」：从 storyline_server 里 import 同一个函数。
    曾经试过用 inspect 抠 process 的源码片段再 exec——那既脆（缩进/结构一变就炸）
    又正是漂移的来源，所以改成把逻辑提成模块级函数、两边共用。
    """
    from storyline_server.nodes.core_nodes import normalize_custom_script

    return normalize_custom_script(raw_custom)


def main() -> int:
    print("=== ① 对象数组（真机事故的形状）===")
    got = normalize([
        {"group": "group_0001", "text": "小白兔，遇见了它的春天。"},
        {"group": "group_0002", "text": "风很轻，云很白，陪着你就好。"},
    ])
    check("{" not in got, f"没有花括号残留：{got!r}")
    check("'" not in got, f"没有单引号残留：{got!r}")
    check("group_0001" not in got, f"没有 group id 残留：{got!r}")
    check("小白兔，遇见了它的春天。" in got, "第一句在")
    check("风很轻，云很白，陪着你就好。" in got, "第二句在")
    check(len(got.splitlines()) == 2, f"两行：{got.splitlines()}")

    print("\n=== ② 纯字符串数组 ===")
    got2 = normalize(["第一句", "第二句"])
    check(got2 == "第一句\n第二句", f"原样换行拼接：{got2!r}")

    print("\n=== ③ 单个字符串（按空行分段的手写文本）===")
    got3 = normalize("第一段\n\n第二段")
    check(got3 == "第一段\n\n第二段", f"原样保留：{got3!r}")

    print("\n=== ④ 整串就是 Python 字面量（走 else 分支的兜底）===")
    literal = ("{'group': 'group_0001', 'text': '小白兔，遇见了它的春天。'}\n"
               "{'group': 'group_0002', 'text': '风很轻，云很白，陪着你就好。'}")
    got4 = normalize(literal)
    check("{" not in got4 and "'" not in got4, f"脏字符被清掉：{got4!r}")
    check("小白兔，遇见了它的春天。" in got4, "抠回了第一句")
    check("风很轻，云很白，陪着你就好。" in got4, "抠回了第二句")

    print("\n=== ⑤ 空 / None 不炸 ===")
    check(normalize([]) == "", "空数组 -> 空串")
    check(normalize(None) == "", "None -> 空串")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
