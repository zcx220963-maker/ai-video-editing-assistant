"""批量导入本地音频文件到 BGM 曲库（materials 表 origin='bgm'）。

用法：
  python import_bgm.py /path/to/music              # 导入目录下所有音频
  python import_bgm.py /path/to/music --dry-run    # 只列出会导入的文件，不写库
  python import_bgm.py /path/to/song.mp3           # 导入单个文件

导入后所有用户的 select_BGM 都能检索到这些音乐（origin='bgm' 共享可见）。
推荐的开源音乐来源：
  - Pixabay Music (https://pixabay.com/music/) — CC0，免登录下载
  - Free Music Archive (https://freemusicarchive.org/) — CC BY
  - Incompetech (https://incompetech.com/) — CC BY (Kevin MacLeod)
  - YouTube Audio Library — 需 YouTube 账号
下载后放到一个目录，跑本脚本即可。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework.ingest import ingest_bytes
from agent_framework.storage import build_storage

BGM_USER_ID = "u-bgm-library"
BGM_CONV_ID = "c-bgm-library"
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}


async def _chunks_from_file(path: Path):
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            yield b


async def import_one(storage, path: Path, dry_run: bool) -> dict:
    info = {
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
    }
    if dry_run:
        return info
    result = await ingest_bytes(
        storage,
        _chunks_from_file(path),
        filename=path.name,
        user_id=BGM_USER_ID,
        conversation_id=BGM_CONV_ID,
        origin="bgm",
    )
    info["material_id"] = result["material_id"]
    return info


async def main(paths: list[str], dry_run: bool) -> None:
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files.extend(
                f for f in pp.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS
            )
        elif pp.is_file() and pp.suffix.lower() in AUDIO_EXTS:
            files.append(pp)
        else:
            print(f"跳过（非音频文件或不存在）：{p}")

    if not files:
        print("没有找到可导入的音频文件。")
        print(f"支持的格式：{', '.join(sorted(AUDIO_EXTS))}")
        return

    print(f"找到 {len(files)} 个音频文件{'（dry-run，不写库）' if dry_run else ''}：")
    if dry_run:
        storage = None
    else:
        storage = build_storage("pg_minio")
        await storage.start()
        await storage.users.provision(BGM_USER_ID)
        await storage.conversations.ensure(BGM_USER_ID, BGM_CONV_ID)

    ok, fail = 0, 0
    try:
        for f in files:
            try:
                info = await import_one(storage, f, dry_run)
                mid = info.get("material_id", "")
                print(f"  ✓ {f.name} ({info['size'] // 1024}KB){f' → {mid}' if mid else ''}")
                ok += 1
            except Exception as e:
                print(f"  ✗ {f.name} — {e}")
                fail += 1
    finally:
        if storage:
            await storage.close()

    print(f"\n完成：成功 {ok}，失败 {fail}")
    if not dry_run and ok > 0:
        print("曲库已导入，select_BGM 会按用户请求关键词从曲库中匹配。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量导入本地音频到 BGM 曲库")
    parser.add_argument("paths", nargs="+", help="音频文件或目录路径")
    parser.add_argument("--dry-run", action="store_true", help="只列出文件不写库")
    args = parser.parse_args()
    asyncio.run(main(args.paths, args.dry_run))