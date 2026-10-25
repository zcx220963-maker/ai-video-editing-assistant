"""诊断 2：把 collect_demos 的循环在外部复刻一遍，看它到底在哪一步丢的。"""
import sys

sys.path.insert(0, "/app")

from storyline_server.motion import code_spec as cs

rels = ["opening/fracture/Fracture.tsx", "camera/basic-3d-scene/Basic3DScene.tsx"]
shot = {"id": "s01", "text": "测试", "code": "", "card": "",
        "subject": "测试", "subject_channel": "vector", "derived_from": rels}
mode, plan = cs.forage_scan({"shots": [shot]})
print("mode", mode)

errors: list[str] = []
rows = []
for row in plan:
    sid = str(row.get("id") or "")
    card, derived = cs._norm_cite(row, sid, errors)
    rows.append({"id": sid, "card": card, "derived_from": derived})
print("rows", rows, "errors", errors)

known = cs._known_demo_relpaths()
print("FORAGE_MAX_BYTES", cs.FORAGE_MAX_BYTES, "known", len(known))
for r in rows:
    for rel in r["derived_from"]:
        data = cs.demo_bytes(rel)
        print(f"  rel={rel!r} data={'None' if data is None else len(data)} "
              f"in_known={rel in known}")
