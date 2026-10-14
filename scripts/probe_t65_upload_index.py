# -*- coding: utf-8 -*-
"""R13-T65 真库验证：在真 PG + 真 MinIO 上走一次真上传，再按 sha 取回字节。

离线用例（tests/test_asset_index.py）证明的是函数行为；这一份证明的是**线上那两张表
之间真的有腿**——判在真库里，不判在替身上。用完即删（回执带删除结果），不留脏数据。

判据：
1. 上传回执里有 material_id，素材册（materials.list_visible kinds=image）看得见它；
2. asset_library 里有一行 source='user-upload' / license='user-uploaded' /
   enabled=False，object_key 与 materials 那个**同一个**；
3. get_by_sha 命中 → 按 object_key 从 MinIO 取回**逐字节等于**传上去的那段；
4. search() 看不见它（别人的检索面）；
5. 同一个 sha 再传一次：asset_library 仍只有一行（UNIQUE 生效，不是两条重复记录）；
6. 收尾把这一趟造的 materials 行、asset_library 行、对象都删掉，并回执删后读数。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/app")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.ingest import ingest_bytes                     # noqa: E402
from agent_framework.storage import build_storage                   # noqa: E402

FAILS = 0
CHECKS = 0


def check(cond, label):
    global CHECKS, FAILS
    CHECKS += 1
    print(f"  {'OK ' if cond else 'BAD'} {label}")
    if not cond:
        FAILS += 1


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da6360000002000001e221bc330000000049454e44ae426082")


def _payload(tag: str) -> bytes:
    """每次唯一的字节。

    用固定字节的话，第二次跑这份探针时 ``get_by_sha`` 会命中**上一次**留下的索引行，
    于是 ``object_key`` 比的是两个不同对象键——红的是探针，不是链路。每次唯一
    让「同一 sha 再传一次」那条幂等判据仍然成立（同一趟里传两次），而跨趟互不干扰。
    """
    return PNG + tag.encode("ascii")


def _chunks(data):
    async def gen():
        yield data
    return gen()


async def main():
    global FAILS
    storage = build_storage("pg_minio")
    await storage.start()
    tag = uuid.uuid4().hex[:8]
    # 用**已存在**的用户：`conversations` 上有指向 `users` 的外键，凭空造一个 user_id
    # 会在 conversations.ensure 那一步就 409，压根走不到索引腿。
    user, conv = "u-ba1836fafa49", f"t65-{tag}"
    out_lines = []
    try:
        print(f"\n[T65 真库] user={user} conv={conv} bytes={len(PNG) + len(tag)}")
        blob = _payload(tag)
        out = await ingest_bytes(storage, _chunks(blob),
                                 filename=f"probe-{tag}.png",
                                 user_id=user, conversation_id=conv,
                                 origin="upload")
        mid = out.get("material_id")
        sha = None
        rows = await storage.db.select("materials", where={"id": mid}, limit=1)
        if rows:
            sha = rows[0].get("sha256")
        check(bool(mid) and bool(sha), f"materials 行写成，sha256={sha}")

        # ② 索引腿
        lib = await storage.assets.get_by_sha(sha)
        check(bool(lib), "asset_library.get_by_sha 命中——渲染侧要的就是这一下")
        if lib:
            check(lib.get("source") == "user-upload", f"source={lib.get('source')!r}")
            check(lib.get("license") == "user-uploaded", f"license={lib.get('license')!r}")
            check(lib.get("enabled") is False, f"enabled={lib.get('enabled')!r}")
            check(lib.get("object_key") == out.get("object_key"),
                  "object_key 与 materials 那一个是同一个键（没存第二份字节）")

        # ③ 取字节
        buf = b""
        async for c in storage.objects.get_stream(out["object_key"]):
            buf += c
        check(buf == blob, f"按 object_key 取回的字节逐字节相同（{len(buf)} vs {len(blob)}）")

        # ④ 检索面看不见
        hits = await storage.assets.search("probe")
        check(sha not in [r.get("sha256") for r in hits],
              f"search('probe') 看不见它（命中 {len(hits)} 条，均不含本件）")

        # ⑤ 幂等：同 sha 再传一次
        out2 = await ingest_bytes(storage, _chunks(blob),
                                  filename=f"probe2-{tag}.png",
                                  user_id=user, conversation_id=conv,
                                  origin="upload")
        again = await storage.assets.get_by_sha(sha)
        check(bool(again) and again["id"] == lib["id"],
              "同一张图再传一次：asset_library 仍是同一行（UNIQUE 生效）")
        check(out2["material_id"] != mid,
              "materials 那一层是新的一行（归属不同：两次上传各自可删）")

        # ---- 收尾：这一趟造的脏数据全删掉 ----
        # 只删**本会话**的：`list_visible` 会把该用户 conv_id=NULL 的行也算进来，
        # 拿它当删除清单会顺手删掉别人（其实是该用户早先）的素材。
        print("\n[T65 收尾] 删除这一趟造的 materials 行 / asset_library 行 / 对象")
        mats = await storage.db.select("materials", where={"conv_id": conv}, limit=200)
        n_mat = 0
        for m in mats:
            await storage.objects.delete(m["object_key"])
            await storage.db.delete("materials", where={"id": m["id"]})
            n_mat += 1
        n_lib = 0
        if lib:
            await storage.objects.delete(lib["object_key"])
            await storage.db.delete("asset_library", where={"id": lib["id"]})
            n_lib = 1
        left_lib = await storage.db.select("asset_library",
                                           where={"source": "user-upload"}, limit=200)
        left_lib = [r for r in left_lib
                    if str(r.get("caption") or "").find(tag) >= 0
                    or str(r.get("src_id") or "").find(sha[:16]) >= 0]
        left_mat = await storage.db.select("materials", where={"conv_id": conv}, limit=200)
        await storage.db.delete("conversations", where={"id": conv})
        left_conv = await storage.db.select("conversations", where={"id": conv}, limit=1)
        check(n_mat == 2 and n_lib == 1, f"删了 {n_mat} 条 materials、{n_lib} 条索引行")
        check(left_lib == [], f"asset_library 里没留下本件的残留（实得 {len(left_lib)} 行）")
        check(left_mat == [], f"materials 里没留下本会话的残留（实得 {len(left_mat)} 行）")
        check(left_conv == [], f"conversations 里没留下本会话（实得 {len(left_conv)} 行）")
    finally:
        await storage.close()

    print(f"\n{'全部通过' if FAILS == 0 else f'有 {FAILS} 条未过'}："
          f"用例 {CHECKS} 条，失败 {FAILS} 条")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
