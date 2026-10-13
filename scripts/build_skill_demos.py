#!/A
"""R1: 把 remotion/src/demos/ 的非 _legacy demo 复制到技能目录，改写 import 为别名。

重跑幂等：先清目标目录再复制。产出 demos/MANIFEST.json（relpath ↔ bytes ↔ card）。
"""
import json, re, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "remotion/src/demos"
DST = ROOT / "backend/examples/skills/motion_explainer/demos"

IMPORT_MAP = {
    "../../_fixtures/Motion": "@dg/motion",
    "../../_fixtures/Polish": "@dg/polish",
    "../../_fixtures/Brand": "@dg/brand",
    "../../_fixtures/Look": "@dg/fixtures/Look",
    "../../_fixtures/Fixtures": "@dg/fixtures/Fixtures",
    "../../_fixtures/PageCam2D": "@dg/fixtures/PageCam2D",
    "../../../assets/lib/ClipCard": "@dg/lib/ClipCard",
}

def rewrite_imports(content: str) -> str:
    for old, new in IMPORT_MAP.items():
        content = content.replace(f"from '{old}'", f"from '{new}'")
        content = content.replace(f'from "{old}"', f'from "{new}"')
    return content

def main():
    if not SRC.exists():
        print(f"源目录不存在: {SRC}", file=sys.stderr)
        sys.exit(1)

    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)

    manifest = []
    for f in sorted(SRC.rglob("*.tsx")):
        if "_legacy" in f.parts:
            continue
        rel = f.relative_to(SRC)
        dst_file = DST / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        content = f.read_text(encoding="utf-8")
        content = rewrite_imports(content)
        # newline="\n" 是必须的：不写它，Windows 上 write_text 会把 \n 翻成 \r\n，
        # 于是「盘上写出的字节」比下面记的 `len(content.encode())` 多出一个行数
        # —— 清单里那个数就成了一枚说谎的常量（158/158 全差）。钉死 LF 之后，
        # 写出的字节、记下的字节、`.gitattributes` 的 `eol=lf` 三处一致。
        dst_file.write_text(content, encoding="utf-8", newline="\n")
        manifest.append({
            "relpath": str(rel).replace("\\", "/"),
            "bytes": len(content.encode("utf-8")),
        })

    man = DST / "MANIFEST.json"
    man.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"复制 {len(manifest)} 个 demo 到 {DST}")
    print(f"MANIFEST.json: {man}")

if __name__ == "__main__":
    main()