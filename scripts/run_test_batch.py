import subprocess, sys

NAMES = [
    "test_asset_index.py",
    "test_t68_imagegen_visibility.py",
    "test_plan_layout.py",
    "test_planning_prompt_golden.py",
    "test_context.py",
    "test_imagegen.py",
    "test_asset_sources.py",
    "test_bgm_library.py",
    "test_plan_gate.py",
]

out = []
failed = []
for n in NAMES:
    r = subprocess.run([sys.executable, f"tests/{n}"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    tail = (r.stdout or "").strip().splitlines()
    last = tail[-1] if tail else "(无输出)"
    out.append(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {n:42} {last}")
    if r.returncode != 0:
        failed.append(n)
        out.append("      " + "\n      ".join((r.stderr or "")[-1500:].splitlines()[-12:]))

print("\n".join(out))
print(f"\nBATCH_FAILED={len(failed)}" + (f" -> {failed}" if failed else ""))
