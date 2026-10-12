#!/A
"""R1: 标定 DERIVED_MIN_SIM —— 正样本(卡→自己demo) vs 负样本(卡→异类demo) 的 token 相似度分布。"""
import json, random, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from storyline_server.motion.code_spec import canonical_shape, shape_similarity

SHOTS = ROOT / "backend/examples/skills/motion_explainer/shots"
DEMOS = ROOT / "backend/examples/skills/motion_explainer/demos"
MANIFEST = DEMOS / "MANIFEST.json"

def load_card_demos():
    manifest = {m["relpath"]: m for m in json.loads(MANIFEST.read_text("utf-8"))}
    card_demos = {}
    for cat_dir in sorted(SHOTS.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith("."):
            continue
        for card in sorted(cat_dir.glob("*.md")):
            text = card.read_text("utf-8")
            import re
            demo_m = re.search(r'参考实现\s*\n(demos/[^\n]+)', text)
            if not demo_m:
                continue
            demo_rel = demo_m.group(1).strip().rstrip("/")
            if demo_rel.startswith("demos/"):
                demo_rel = demo_rel[6:]  # strip "demos/" prefix
            card_id = f"{cat_dir.name}/{card.stem}"
            demo_codes = []
            for rel, m in manifest.items():
                if rel.startswith(demo_rel + "/") or rel == demo_rel:
                    f = DEMOS / rel
                    if f.is_file():
                        demo_codes.append((rel, f.read_text("utf-8")))
            if demo_codes:
                card_demos[card_id] = demo_codes
    return card_demos

def main():
    card_demos = load_card_demos()
    print(f"加载 {len(card_demos)} 张卡→demo 映射")

    shapes = {}
    for card_id, demos in card_demos.items():
        for rel, code in demos:
            if rel not in shapes:
                shapes[rel] = canonical_shape(code)

    cards_by_cat = {}
    for card_id in card_demos:
        cat = card_id.split("/")[0]
        cards_by_cat.setdefault(cat, []).append(card_id)

    rng = random.Random(42)
    pos_sims = []
    neg_sims = []
    for card_id, demos in card_demos.items():
        for rel, _ in demos:
            if rel in shapes:
                pos_sims.append(1.0)
                cat = card_id.split("/")[0]
                other_cats = [c for c in cards_by_cat if c != cat]
                if other_cats:
                    other_cat = rng.choice(other_cats)
                    other_card = rng.choice(cards_by_cat[other_cat])
                    for other_rel, _ in card_demos.get(other_card, []):
                        if other_rel in shapes and other_rel != rel:
                            neg_sims.append(shape_similarity(shapes[rel], shapes[other_rel]))
                            break

    pos_sims.sort()
    neg_sims.sort()
    print(f"正样本: {len(pos_sims)} 对, 全部 = {pos_sims[0]:.3f}~{pos_sims[-1]:.3f}")
    print(f"负样本: {len(neg_sims)} 对, 范围 = {neg_sims[0]:.3f}~{neg_sims[-1]:.3f}, 中位 = {neg_sims[len(neg_sims)//2]:.3f}")

    best = None
    for t in [x / 1000 for x in range(0, 1001)]:
        pos_pass = sum(1 for s in pos_sims if s >= t) / len(pos_sims) if pos_sims else 0
        neg_pass = sum(1 for s in neg_sims if s >= t) / len(neg_sims) if neg_sims else 0
        if pos_pass >= 0.95 and neg_pass <= 0.02:
            best = t
    if best is not None:
        print(f"\n标定结果: DERIVED_MIN_SIM = {best:.3f}")
        print(f"  正样本召回率 = {sum(1 for s in pos_sims if s >= best)/len(pos_sims)*100:.1f}%")
        print(f"  负样本通过率 = {sum(1 for s in neg_sims if s >= best)/len(neg_sims)*100:.1f}%")
    else:
        for t in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
            pos_pass = sum(1 for s in pos_sims if s >= t) / len(pos_sims) if pos_sims else 0
            neg_pass = sum(1 for s in neg_sims if s >= t) / len(neg_sims) if neg_sims else 0
            print(f"  t={t:.2f}: pos={pos_pass*100:.1f}% neg={neg_pass*100:.1f}%")
        print("\n无法找到同时满足 recall≥95% 且 fp≤2% 的阈值，需按族降档")

if __name__ == "__main__":
    main()