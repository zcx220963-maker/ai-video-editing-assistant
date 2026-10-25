"""诊断：技能目录搬家后，`collect_demos` 为什么一份都取不回来。"""
import sys

sys.path.insert(0, "/app")

from storyline_server.motion import code_spec as cs

print("_SKILL_DIR =", cs._SKILL_DIR, "exists=", cs._SKILL_DIR.is_dir())
print("demos exists =", (cs._SKILL_DIR / "demos").is_dir())
known = cs._known_demo_relpaths()
print("MANIFEST 条数 =", len(known))

rels = ["opening/fracture/Fracture.tsx", "camera/basic-3d-scene/Basic3DScene.tsx"]
for r in rels:
    print(f"  {r}: in_known={r in known} bytes={len(cs.demo_bytes(r) or b'')}")

shot = {"id": "s01", "text": "测试", "code": "", "card": "",
        "subject": "测试", "subject_channel": "vector", "derived_from": rels}
mode, plan = cs.forage_scan({"shots": [shot]})
print("mode =", mode, "plan =", plan)
acc, errs = cs.collect_demos(plan)
print("errs =", errs)
print("forage_demos =", [(d.get("relpath"), d.get("bytes")) for d in acc["forage_demos"]])
