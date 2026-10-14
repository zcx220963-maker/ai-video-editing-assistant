#!/A
"""重写 shots/INDEX.md：每张卡一行——**类别/卡名** + 能量 + 时长 + 一句话 + 适用 + demo 字节。

**这份索引不是给人看的文档，是选卡的判据。** SKILL.md:56/65 对模型说得很硬：
「先读 `shots/INDEX.md`（一次加载技能调用）——它列了全部 124 张卡的卡名、能量、
一句话描述，扫完就能选卡，**不要逐张调「加载技能」读卡全文**」「不要读超过 15 张卡」。
模型因此**不打开卡**就选 —— 索引里那一句话必须真的在。索引一空，那句禁令就变成
「凭卡名猜」，模型唯一的活路只剩逐张打开，于是 124 张卡按 2 张/轮 读，撞上
`max_iterations=40` 当场 failed。

2026-10-14 真机就是这么炸的：run `a3d8ff08098a` 第 13–40 轮**全是 `load_skill` 读卡**，
烧掉 3,412,468 token、一张计划卡都没出。查下来是**本脚本**按一份已经不存在的格式解析：
它找 markdown 列表行 `- **卡名** [x] — 描述`，而真实的 124 张卡早就改成了 YAML
frontmatter（`一句话:` / `适用:` / `时长:` / `能量:` / 可选 `标签:`）。三处因此同时坏掉：

  ① `一句话` 取不到 → 描述列 124/124 全空（**这是循环的直接原因**）
  ② `能量` 那一处 `re.search(r'\\[(.+?)\\]', text)` 抓的是全文**第一个方括号**，
     抓到的是正文里的帧号数组（`[236,236,236]`），不是能量
  ③ demo 路径按 `demos/` 前缀去比 `MANIFEST.json` 的 relpath，而 MANIFEST 的 relpath
     **不带**这个前缀 → 字节数 124/124 为 0

**为什么没人发现**：全仓没有一条断言读过 INDEX.md（`tests/` 里零处引用）。
判据（`code_spec._known_card_ids`）是自己扫 `shots/` 目录的，索引坏了不影响它——
于是「模型拿不到选卡信息」这件事**没有任何闸在读**。所以本脚本配一条断言，
见 `backend/tests/test_shot_index.py`：索引退化（描述空、卡名对不上卡库）当场红。

用法：`python -I scripts/build_index.py`（改完卡或 demo 后重跑；索引是产物，别手改）。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "backend/examples/skills/motion_explainer"
SHOTS = SKILL / "shots"
MANIFEST = SKILL / "demos/MANIFEST.json"

#: demo 路径在卡里写着 `demos/…`（相对技能目录），而 MANIFEST 的 relpath 相对 `demos/`。
DEMO_PREFIX = "demos/"

#: 一句话/适用的截断长度。留着长句的好处有限（选卡只要认出「这张卡是干什么的」），
#: 而这份文件是**整份**进上下文的：124 行 × 每行多 40 字 ≈ 5 KB，值不值要算。
DESC_MAX = 72
FIT_MAX = 34


def _frontmatter(text: str) -> dict[str, str]:
    """取出卡头的 YAML frontmatter（`---` 之间的 `键: 值`）。只认单层、不引第三方 YAML。

    为什么手写而不上 `yaml`：全仓没有 YAML 依赖，而为四个键引一个解析器不值当；
    这里的形状是卡片自己定的、由 `tests/test_shot_index.py` 盯着（每个键都必须取到）。
    """
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _clip(s: str, limit: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _demo_ref(text: str) -> str:
    """`## 参考实现` 那一节里的第一个 `demos/…` 路径；没有就返回空串。

    为什么扫**整节**而不只看第一行：卡的第一行未必是 demo。实测两种写法：
    `transition/shot-transitions` 先写母版组件（`A 式：FilmLayout.tsx flash-cut 层`）、
    `ui-entrance/page-waterfall-wall` 先写随镜像发的公共库
    （`assets/lib/VerticalTicker.tsx`）——真 demo 都在**下一行**才出现。只看第一行的
    旧版对这两张卡算出 0 B，读起来像「这张卡没有 demo」，而 demo 是有的。
    一行里可能有多个路径（`A；B；C`），所以取的是节内第一个 `demos/` 开头的记号，
    不是行内第一个记号。中文标点当分隔符切，别把 `；` 粘进路径。
    """
    m = re.search(r"^##\s*参考实现\s*\n(.*?)(?=\n##\s|\Z)", text, re.M | re.S)
    if not m:
        return ""
    for tok in re.findall(r"[^\s；;，,。、（）()：:]+", m.group(1)):
        if tok.startswith(DEMO_PREFIX):
            return tok
    return ""


def _demo_dir(demo_rel: str) -> str:
    """卡里那行 demo 路径 → 它在 `demos/` 下的**目录**（用来汇总字节）。

    卡有时给文件（`demos/typography/brand-ink-open/BrandInkOpen.tsx`）、有时给目录
    （`demos/data/chart-live-moves/`），两种都是「这张卡的参考实现在这儿」。
    统一取到目录：文件取它的父目录。取不到目录就退回原名（下面按键相等匹配）。
    """
    rel = demo_rel[len(DEMO_PREFIX):] if demo_rel.startswith(DEMO_PREFIX) else demo_rel
    rel = rel.rstrip("/")
    if "." in rel.rsplit("/", 1)[-1]:        # 末段带扩展名 = 是文件，退到父目录
        rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return rel


def _energy(fm: dict[str, str]) -> str:
    """`能量: 中（每次转场有空间惊喜…）` → `中`：括号里是解释，选卡时不需要。"""
    raw = fm.get("能量") or ""
    raw = re.split(r"[（(]", raw, maxsplit=1)[0]
    return _clip(raw, 8) or "?"


def main() -> int:
    manifest = {m["relpath"]: m["bytes"] for m in json.loads(MANIFEST.read_text("utf-8"))}
    lines = ["# shot 卡索引（124 张，按类别）", ""]
    lines.append("每行：`<类别>/<卡名>` [能量] 时长 — 一句话描述 | 适用 | demo 目录 + 字节数")
    lines.append("")
    lines.append("**选卡看这一份就够**：把 `card` 一栏照抄成行首那个 `<类别>/<卡名>`，")
    lines.append("`derived_from` 填 demo 目录下的文件名（`demos/` 前缀可有可无）。")
    lines.append("不要再逐张打开卡全文——SKILL.md 写明「不要读超过 15 张卡」。")
    lines.append("")

    total = 0
    cards_seen = 0
    empty_desc: list[str] = []
    for cat_dir in sorted(SHOTS.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith((".", "_")):
            continue
        cards = sorted(cat_dir.glob("*.md"))
        if not cards:
            continue
        lines.append(f"## {cat_dir.name}（{len(cards)} 张）")
        lines.append("")
        for card in cards:
            text = card.read_text("utf-8")
            fm = _frontmatter(text)
            card_id = f"{cat_dir.name}/{fm.get('name') or card.stem}"
            desc = _clip(fm.get("一句话") or "", DESC_MAX)
            fit = _clip(fm.get("适用") or "", FIT_MAX)
            dur = _clip(fm.get("时长") or "", 22)
            if not desc:
                empty_desc.append(card_id)

            # demo：卡里写在 `## 参考实现` 那一节（可能是文件也可能只给目录），
            # 取法见 `_demo_ref`。
            demo_rel = _demo_ref(text)
            demo_root = _demo_dir(demo_rel) if demo_rel else ""
            demo_bytes = 0
            if demo_root:
                for rel, sz in manifest.items():
                    if rel == demo_root or rel.startswith(demo_root + "/"):
                        demo_bytes += sz
            total += demo_bytes
            cards_seen += 1

            # 顺序按**选卡时眼睛的走法**排：先认卡（id + 能量），再读它是干什么的
            # （一句话），时长/适用/字节都是二级信息，跟在后面。旧版把时长塞在
            # 一句话前面，扫起来先撞上一串 `（225f@30fps；四站…）`，选卡的信息被淹。
            bits = [f"- `{card_id}` [{_energy(fm)}] — {desc or '（缺一句话描述）'}"]
            if dur:
                bits.append(f"| 时长 {dur}")
            if fit:
                bits.append(f"| 适用 {fit}")
            bits.append(f"| demo `{demo_root or demo_rel or '?'}` {demo_bytes:,} B")
            lines.append(" ".join(bits))
        lines.append("")

    lines.append(f"总计 demo 字节：{total:,}")
    out = SHOTS / "INDEX.md"
    out.write_text("\n".join(lines) + "\n", "utf-8")
    print(f"INDEX.md 重写完成：{out}")
    print(f"  卡片 {cards_seen} 张；一句话描述缺 {len(empty_desc)} 张"
          + (f"：{', '.join(empty_desc[:5])}" if empty_desc else ""))
    print(f"  demo 字节合计 {total:,}")
    # 描述缺失是**退化**、不是「某张卡有点特殊」：索引的全部用处就是那一句话。
    # 当场报红（退出码 1），别让一份空索引安静地进镜像。
    return 1 if empty_desc else 0


if __name__ == "__main__":
    sys.exit(main())
