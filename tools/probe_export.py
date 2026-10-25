"""验导出：把一条已有工程导成可跑的 Remotion 工程，检查文件齐全、别名换对了。

    docker cp tools/probe_export.py creation-app:/app/tools_probe_export.py
    docker exec creation-app python /app/tools_probe_export.py [film_id]

不跑 npm（容器里没网/没装 node 依赖），只验"该有的文件都在、@dg 别名已换掉、
时长/画幅/帧率对得上"。真正的 `npm i && npm run render` 要在用户机器上跑。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from storyline_server.simple import export, project

fid = sys.argv[1] if len(sys.argv) > 1 else ""
if not fid:
    rows = [r for r in project.list_recent(8) if r.get("mp4_url")]
    if not rows:
        print("没有已出片的工程")
        raise SystemExit(1)
    fid = rows[0]["id"]

folder = export.export_remotion(fid)
doc = project.load(fid)
print("工程目录", folder)
print("工程读数", {k: doc.get(k) for k in ("seconds", "aspect", "fps")},
      "镜数", len(doc.get("bodies") or []))

want = ["package.json", "remotion.config.ts", "tsconfig.json", "README.md",
        "src/index.ts", "src/Root.tsx", "src/film.tsx",
        "src/dg/motion.ts", "src/dg/tokens.ts"]
have = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
missing = [w for w in want if w not in have]
print("文件齐全" if not missing else f"XX 缺 {missing}")
for w in want:
    p = folder / w
    if p.exists():
        print(f"  {w:22} {len(p.read_text(encoding='utf-8').splitlines()):>5} 行")

code = (folder / "src" / "film.tsx").read_text(encoding="utf-8")
print("@dg 别名残留", len(re.findall(r"@dg/", code)), "处（应为 0）")
print("相对路径 import",
      len(re.findall(r"from '\./dg/(motion|tokens)'", code)), "处（应为 2）")

root = (folder / "src" / "Root.tsx").read_text(encoding="utf-8")
m = re.search(r"durationInFrames=\{(\d+)\}", root)
fps = re.search(r"fps=\{(\d+)\}", root)
size = re.search(r"width=\{(\d+)\}\s*\n\s*height=\{(\d+)\}", root)
print("Composition", {"frames": m.group(1) if m else "?",
                      "fps": fps.group(1) if fps else "?",
                      "size": size.groups() if size else "?"})

zip_path = export.zip_dir(folder, Path("/tmp") / f"{fid}-remotion.zip")
print("zip", zip_path, round(zip_path.stat().st_size / 1024), "KB")
print("\nOK")
