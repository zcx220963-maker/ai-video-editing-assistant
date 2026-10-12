"""闸⑨ 阈值的标定：把 226 个 demo 按「剥掉字符串与颜色后的 token 多重集 Jaccard」两两比。

只读不写。看两件事：
  1. 真不同的两份代码最远能远到什么程度（决定打回线）；
  2. 故意制造的「只换文案与色相」那份拷贝相似度是多少（决定这条闸拦不拦得住）。
"""
import re
import sys
from collections import Counter
from pathlib import Path

STR_RE = re.compile(r"""//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""", re.S)
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
IMP_RE = re.compile(r"""^\s*(?:import|export)[\s\S]{0,400}?from\s*['"][^'"]+['"];?\s*$""", re.M)
TOK_RE = re.compile(r"[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\sA-Za-z0-9_$]")


def canon(code: str) -> tuple:
    code = IMP_RE.sub("", code)
    code = STR_RE.sub("S", code)
    code = HEX_RE.sub("C", code)
    code = NUM_RE.sub("N", code) if "NUM" in sys.argv else code
    return tuple(TOK_RE.findall(code))


def jac(a: tuple, b: tuple) -> float:
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return inter / union if union else 0.0


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    files = sorted(p for p in (root / "src" / "demos").rglob("*.tsx")
                   if "_fixtures" not in p.parts and "_legacy" not in p.parts)
    data = [(p, canon(p.read_text(encoding="utf-8"))) for p in files]
    data = [(p, c) for p, c in data if len(c) > 40]
    print(f"参与比对的 demo 文件：{len(data)}（剥后仍不足 40 个 token 的不算）")

    sims = []
    for i in range(len(data)):
        best = (0.0, None)
        for j in range(len(data)):
            if i == j:
                continue
            s = jac(data[i][1], data[j][1])
            if s > best[0]:
                best = (s, data[j][0].name)
        sims.append((best[0], data[i][0].name, best[1]))
    sims.sort(reverse=True)
    vals = [s for s, _, _ in sims]
    for q in (0, 1, 5, 10, 25, 50):
        idx = int(len(vals) * q / 100)
        print(f"  最近邻相似度 p{q:<2} = {vals[idx]:.3f}")
    print("  最像的 8 对（不同文件之间）：")
    seen = set()
    shown = 0
    for s, a, b in sims:
        key = frozenset((a, b))
        if key in seen or b is None:
            continue
        seen.add(key)
        print(f"    {s:.3f}  {a} ↔ {b}")
        shown += 1
        if shown >= 8:
            break

    # 合成用例：同一份代码只换字符串与颜色（真缺陷的形状）
    p, c = data[0]
    src = p.read_text(encoding="utf-8")
    mut = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", lambda m: m.group(0)[0] + "换掉的文案" + m.group(0)[0], src)
    mut = HEX_RE.sub("#123456", mut)
    print(f"\n同文件只换文案与色相：{jac(c, canon(mut)):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
