"""R3 §5.2 入库配方 (a)–(d) 的执行体：从 CC0/PD 源批量下载素材，写 asset_library + MinIO。

每行必须带 license + attribution_required（§5.2 硬约束）。
提供 --offline-dir 档（§5.2 降级第 3 档）：不出网，只吃本地目录。

用法（在 creation-app 容器内运行，或宿主机配好 PG_DSN/MINIO_* 环境变量）：
    python scripts/asset_ingest.py --source wikimedia-commons --limit 50
    python scripts/asset_ingest.py --source iconify --limit 200
    python scripts/asset_ingest.py --source all
    python scripts/asset_ingest.py --offline-dir assets/manual/ --source manual
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 让 asset_common 可导入


def _add_backend_to_path() -> None:
    """把 `agent_framework` 放上 import 路径（R11-T50：源名与实测口径都在那边）。

    为什么要试两个候选：宿主布局是 `<仓库根>/scripts` + `<仓库根>/backend`，容器里
    这两个目录落在**两个根**上（`/scripts` 与 `/app`，见 Dockerfile 那一段注释）。
    写死一个，另一个布局就变成 `ModuleNotFoundError`——而那正是「容器里跑不了，
    跳过吧」的来源。
    """
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "backend", Path("/app")):
        if (cand / "agent_framework").is_dir():
            sys.path.insert(0, str(cand))
            return


_add_backend_to_path()

from agent_framework.asset_scale import measure_asset          # noqa: E402
from agent_framework.asset_sources import (                    # noqa: E402
    ASSET_SOURCES, asset_role_of, ingest_names, role_facts)

UA = "CodeArts-AssetIngest/1.0 (https://codearts.huaweicloud.com)"
ALLOWED_SPDX = {"MIT", "Apache-2.0", "CC0", "CC0-1.0", "BSD", "ISC", "BSD-2-Clause", "BSD-3-Clause"}
PD_LICENSES = {"PD", "PD-old", "PD-old-100", "PD-US", "PD-US-expired", "CC0", "cczero", "CC0-1.0"}

_pending: list[tuple[dict, bytes]] = []

WIKIMEDIA_SEARCHES = [
    ("bird PD", "animal", "bird"),
    ("portrait public domain", "portrait", ""),
    ("astronomy public domain", "object", "astronomy"),
    ("chemistry public domain", "object", "chemistry"),
    ("map public domain", "map", ""),
    ("building public domain", "building", ""),
    ("plant public domain", "plant", ""),
    ("mineral public domain", "object", "mineral"),
    ("scientific instrument public domain", "object", ""),
    ("historical photograph public domain", "photo", "history"),
]


def _fetch(url: str, *, timeout: int = 30, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read()


def _fetch_json(url: str, *, timeout: int = 30) -> Any:
    return json.loads(_fetch(url, timeout=timeout).decode("utf-8"))


def _mw_api(params: dict) -> Any:
    """MediaWiki API 调用：| 不编码（API 要求字面管道符）。"""
    query = urllib.parse.urlencode(params).replace("%7C", "|")
    return _fetch_json(f"https://commons.wikimedia.org/w/api.php?{query}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext_from_mime(mime: str) -> str:
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/svg+xml": ".svg",
        "image/gif": ".gif", "image/webp": ".webp",
    }.get(mime, ".bin")


def _guess_mime(url: str) -> str:
    low = url.lower().split("?")[0]
    if low.endswith(".svg"):
        return "image/svg+xml"
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


# ── 失败回执（T10）：不许把「源不可达」报成「源里没素材」 ─────────────
# 判据与探测脚本共用一份，见 `scripts/asset_common.py`（同一件事的两半：这边入库、
# 那边探可达性）。
from asset_common import (  # noqa: E402  —— 同目录脚本，sys.path 由 main 的入口补
    get_failures, record_failure, record_parse_failure, reset_failures)


# ── (a) Iconify ──────────────────────────────────────────────────────

def ingest_iconify(*, storage: Any, limit: int = 500, dry_run: bool = False) -> list[dict]:
    """装 @iconify/json，逐集合 SPDX 过滤，导出 SVG + 光栅化 PNG。"""
    print("[iconify] 获取 @iconify/json 包信息...", file=sys.stderr)
    meta = _fetch_json("https://registry.npmjs.org/@iconify/json/latest")
    tarball_url = meta["dist"]["tarball"]
    print(f"[iconify] tarball: {tarball_url}", file=sys.stderr)

    import tempfile, tarfile
    tar_bytes = _fetch(tarball_url, timeout=120)
    results = []
    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            tar.extractall(tmpdir)

        collections_json = Path(tmpdir) / "package/json"
        if not collections_json.exists():
            collections_json = Path(tmpdir) / "package" / "json"

        collection_files = sorted(collections_json.glob("*.json"))
        print(f"[iconify] {len(collection_files)} 个集合", file=sys.stderr)

        count = 0
        for cf in collection_files:
            if count >= limit:
                break
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            info = data.get("info", {})
            license_spdx = (info.get("license", {}) or {}).get("spdx", "")
            if license_spdx not in ALLOWED_SPDX:
                continue
            prefix = data.get("prefix", cf.stem)
            icons = data.get("icons", {})
            for name, icon_data in icons.items():
                if count >= limit:
                    break
                body = icon_data.get("body", "")
                if not body:
                    continue
                svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {data.get("height",24)} {data.get("height",24)}">{body}</svg>'
                svg_bytes = svg.encode("utf-8")
                sha = _sha256(svg_bytes)
                src_id = f"{prefix}:{name}"
                object_key = f"assets/iconify/{prefix}/{name}.svg"
                row = {
                    "sha256": sha, "bytes_": len(svg_bytes), "mime": "image/svg+xml",
                    "ext": ".svg", "object_key": object_key, "source": "iconify",
                    "src_id": src_id, "license": license_spdx,
                    "attribution_required": False, "license_url": "",
                    "subject": "icon", "style_family": "icon",
                    "tags": json.dumps([prefix]), "caption": f"{prefix} {name}",
                    "era": "", "width": data.get("height", 24), "height": data.get("height", 24),
                }
            if not dry_run:
                _pending.append((row, svg_bytes))
            results.append(row)
            count += 1
        print(f"[iconify] 入库 {count} 个图标（SPDX 过滤后）", file=sys.stderr)
    return results


# ── (b) Wikimedia Commons ────────────────────────────────────────────

def ingest_wikimedia(*, storage: Any, limit: int = 50, dry_run: bool = False) -> list[dict]:
    """用 search API 找 PD/CC0 图片 → 过滤 → 取 800px JPEG 缩略图。"""
    results = []
    count = 0
    for search_query, subject, tag in WIKIMEDIA_SEARCHES:
        if count >= limit:
            break
        print(f"[wikimedia] 搜索 '{search_query}'...", file=sys.stderr)
        try:
            data = _mw_api({
                "action": "query", "list": "search",
                "srsearch": search_query, "srnamespace": "6",
                "srlimit": "20", "format": "json",
            })
        except Exception as e:
            record_failure("wikimedia-commons", "search", e)
            continue
        search_results = data.get("query", {}).get("search", []) or []
        titles = [r["title"] for r in search_results if r.get("title")]
        if not titles:
            continue
        # 批量取 imageinfo + extmetadata
        try:
            data2 = _mw_api({
                "action": "query", "prop": "imageinfo",
                "iiprop": "extmetadata|mime|size|url", "iiurlwidth": "800",
                "titles": "|".join(titles[:20]), "format": "json",
            })
        except Exception as e:
            record_failure("wikimedia-commons", "metadata", e)
            continue
        pages = (data2.get("query", {}).get("pages", {}) or {}).values()
        for page in pages:
            if count >= limit:
                break
            ii = (page.get("imageinfo") or [{}])[0]
            if not ii:
                continue
            extmeta = ii.get("extmetadata", {}) or {}
            license_name = (extmeta.get("LicenseShortName", {}).get("value", "") or "").strip()
            attr_required = (extmeta.get("AttributionRequired", {}).get("value", "") or "").strip().lower()
            if license_name not in PD_LICENSES and "public domain" not in license_name.lower():
                continue
            if attr_required in ("true", "yes", "1"):
                continue
            thumb_url = ii.get("thumburl", "")
            if not thumb_url:
                continue
            try:
                img_bytes = _fetch(thumb_url, timeout=30)
            except Exception as e:
                record_failure("wikimedia-commons", "download", e,
                               url=thumb_url, detail=str(page.get("title") or ""))
                continue
            sha = _sha256(img_bytes)
            title = page.get("title", "").replace("File:", "")
            ext = Path(title).suffix or ".jpg"
            object_key = f"assets/wikimedia-commons/{sha[:12]}{ext}"
            tags = [tag] if tag else []
            row = {
                "sha256": sha, "bytes_": len(img_bytes), "mime": _guess_mime(thumb_url),
                "ext": ext, "object_key": object_key, "source": "wikimedia-commons",
                "src_id": page.get("title", ""), "license": license_name,
                "attribution_required": False,
                "license_url": (extmeta.get("LicenseUrl", {}).get("value", "") or ""),
                "subject": subject, "style_family": "photo",
                "tags": json.dumps(tags), "caption": title,
                "era": "", "width": ii.get("thumbwidth"), "height": ii.get("thumbheight"),
            }
            if not dry_run:
                _pending.append((row, img_bytes))
            results.append(row)
            count += 1
        print(f"[wikimedia] '{search_query}' 入库 {count} 张（累计）", file=sys.stderr)
    print(f"[wikimedia] 总计入库 {count} 张", file=sys.stderr)
    return results


# ── (c) Poly Haven + ambientCG ──────────────────────────────────────
# 这三条腿 2026-10-14 之前**一根都没走过**（T52 的立卷证据），当天第一次真跑，
# 三条全表现成「HTTP 200、总计入库 0 个、一条失败回执都没有」。逐个查下来不是「源没货」，
# 是腿读不懂站点，三处各自的形状：
#   · polyhaven 的列表项里那栏叫 `thumbnail_url`，代码读的是 `thumbnail`；`caption` 同理
#     （API 给 `name`）。猜键名 = 恒空。
#   · ambientcg 的 `/list` 现在是 htmx 异步壳，HTML 里没有 `details.php?id=`，
#     真列表在 `/hx/asset-list?category=<Cat>`，而且**要带 `HX-Request: true`** 才回件。
#   · open-peeps 是 Webflow 站，件挂在 CDN、href 是**绝对 URL**，旧式子只匹配站内相对路径。
# 所以这三段现在的写法是「照着响应读」，并且**解析出 0 件必留回执**——下次站点再改版，
# 看见的是 EmptyParse，不是又一次「跑完了，0 个」。

#: Poly Haven `/assets` 里 `type` 是**数字码**，而站上没有随包声明它对应什么。
#: 2026-10-14 容器内实测：按码分组看类别与样本 id —— 0=997 件（Abandoned & Ruins/…，环境）、
#: 1=869 件（Asphalt & Bitumen/…，材质）、2=521 件（ArmChair_01、Barrel_01，道具）。
#: 认不出来的码一律留空 subject（= 未判），不许「大概是模型吧」猜一档进去污染按角色检索。
POLYHAVEN_TYPE_CODES: dict[Any, tuple[str, str]] = {
    0: ("hdri", "photo"), 1: ("texture", "texture"), 2: ("3d-model", "3d"),
}

#: 质感图取哪一档分辨率：先 2k 再 1k（4k/8k 单张 10~30 MB，策展库要的是「能铺底」不是「能打印」，
#: 而 `_BG_MIN_LONG_SIDE` 是 1600 px —— 2k 刚好过线，取它就有出处）。
POLYHAVEN_TEXTURE_RES = ("2k", "1k", "4k")
POLYHAVEN_TEXTURE_FMT = ("jpg", "png")

#: ambientCG 缩略图的可取尺寸档位（`/media/thumbnail/<档>/<id>.webp`）。
#: 2026-10-14 实测 256/512/1024-WEBP 三档都在，2k-JPG 那一档 404 —— 所以取 1024：
#: 长边 1024 < 1600，落 `scene`；真要铺满屏的底仍归 polyhaven 的 2k 质感图。
AMBIENTCG_THUMB_SIZE = "1024-WEBP"

#: 许可的**出处页**与必须读到的标记。写死 `license="CC0"` 是 T51 留在注册表注释里的那条
#: 未决项（「这一栏因此只承诺入库时怎么核，不承诺站上说它是什么」）——这里就是「怎么核」：
#: 读不到标记 → 整条腿不收（宁可 0 条），也不带着没核过的许可进库。
LICENSE_STATEMENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "polyhaven": ("https://polyhaven.com/license", ("CC0", "Creative Commons Zero", "publicdomain/zero")),
    "ambientcg": ("https://ambientcg.com/license", ("CC0", "Creative Commons Zero", "publicdomain/zero")),
    "open-peeps": ("https://openpeeps.com", ("CC0", "Creative Commons Zero", "publicdomain/zero")),
}


def _read_license_statement(source: str, *, html: str = "") -> tuple[str, str]:
    """从**源自己的声明页**读许可 → `(许可值, 命中的原话)`；读不到 → `("", "")`。

    `html` 传进来是为了让 open-peeps 复用已经取到的首页，不重复出网。
    """
    url, needles = LICENSE_STATEMENTS[source]
    try:
        text = html or _fetch(url, timeout=30).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        record_failure(source, "license", e, url=url)
        return "", ""
    for needle in needles:
        cands = [" ".join(m.group(0).split())[:120]
                 for m in re.finditer(
                     rf'[^>\n]{{0,60}}{re.escape(needle)}[^<\n]{{0,60}}', text, re.I)]
        # 只带文件名的那条不算证据（`src="../_media/cc0.png"` 证明站上有张 cc0 的图，
        # 不证明这一页写了许可）。
        cands = [c for c in cands
                 if not re.search(r'\.(png|jpe?g|gif|svg|webp)\b', c, re.I)]
        if cands:
            evidence = cands[0]
            print(f"[{source}] 许可出处 {url} → 读到「{evidence}」", file=sys.stderr)
            return "CC0", evidence
    record_parse_failure(source, "license", url=url,
                         detail=f"声明页里没读到 {'/'.join(needles)} 任何一个标记",
                         advice="源的许可声明页与代码里的假设不一致：先去页上确认许可，"
                                "再改 `LICENSE_STATEMENTS`，不许先把 CC0 写回代码")
    return "", ""


def _image_dims(data: bytes) -> tuple[int | None, int | None, str, str]:
    """字节 → (宽, 高, mime, ext)。Pillow 认不出就四个都留空 = **未判**，不猜。"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            # Pillow 打开后的格式在 `im.format` 这个**属性**上；`getformat()` 是
            # 11.x 里不存在的方法，写成它会让每次量尺寸都以 AttributeError 收场，
            # 再被下面那个 except 吞成「未判」——库里那些 width=NULL 就是这么来的。
            fmt = (getattr(im, "format", "") or "").upper()
    except Exception:       # noqa: BLE001 —— 量不出宽高不是失败，是这一格没判
        return None, None, "", ""
    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
            "GIF": "image/gif"}.get(fmt, "")
    return int(w), int(h), mime, _ext_from_mime(mime) if mime else ""


def _polyhaven_thumb(url: str, size: int = 1024) -> str:
    """把 `thumbnail_url` 的 CDN 查询参数改成大尺寸 jpg（同一路径只换 query，2026-10-14 实测通）。

    不改的话拿到的是 256×256 webp —— 那条 `asset_role` 判成 scene、`_BG_MIN_LONG_SIDE`
    够不着，等于花了一次请求买了张缩略图。
    """
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    q.update({"width": str(size), "height": str(size), "format": "jpg"})
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


def _polyhaven_texture_url(asset_id: str) -> str:
    """质感件取**真实漫反射图**的 URL（列表里的缩略图是渲染预览，不是可铺底的质感图）。"""
    try:
        files = _fetch_json(f"https://api.polyhaven.com/files/{asset_id}", timeout=30)
    except Exception as e:  # noqa: BLE001
        record_failure("polyhaven", "files", e, detail=asset_id)
        return ""
    diffuse = files.get("Diffuse") or files.get("diff") or {}
    for res in POLYHAVEN_TEXTURE_RES:
        for fmt in POLYHAVEN_TEXTURE_FMT:
            entry = (diffuse.get(res) or {}).get(fmt) or {}
            if entry.get("url"):
                return str(entry["url"])
    return ""


def ingest_polyhaven(*, storage: Any, limit: int = 20, dry_run: bool = False) -> list[dict]:
    """Poly Haven（HDRI / 质感图 / 模型渲染图），许可从站内声明页读出来再入库。"""
    print("[polyhaven] 核许可...", file=sys.stderr)
    license_name, _prov = _read_license_statement("polyhaven")
    if not license_name:
        return []
    print("[polyhaven] 列资产...", file=sys.stderr)
    try:
        data = _fetch_json("https://api.polyhaven.com/assets")
    except Exception as e:  # noqa: BLE001
        record_failure("polyhaven", "list", e)
        return []
    if not isinstance(data, dict) or not data:
        record_parse_failure("polyhaven", "list", detail=f"响应是 {type(data).__name__}，没有资产条目")
        return []

    results: list[dict] = []
    count = 0
    for asset_id, info in data.items():
        if count >= limit:
            break
        if not isinstance(info, dict):
            continue
        thumb = str(info.get("thumbnail_url") or "")
        if not thumb:
            continue
        subject, family = POLYHAVEN_TYPE_CODES.get(info.get("type"), ("", ""))
        url = (_polyhaven_texture_url(asset_id) if subject == "texture"
               else _polyhaven_thumb(thumb)) or _polyhaven_thumb(thumb)
        try:
            img_bytes = _fetch(url, timeout=60)
        except Exception as e:  # noqa: BLE001
            record_failure("polyhaven", "download", e, url=url, detail=str(asset_id))
            continue
        w, h, mime, ext = _image_dims(img_bytes)
        mime = mime or "image/jpeg"
        ext = ext or ".jpg"
        sha = _sha256(img_bytes)
        cats = info.get("categories") or []
        row = {
            "sha256": sha, "bytes_": len(img_bytes), "mime": mime,
            "ext": ext, "object_key": f"assets/polyhaven/{sha[:12]}{ext}",
            "source": "polyhaven", "src_id": str(asset_id), "license": license_name,
            "attribution_required": False, "license_url": LICENSE_STATEMENTS["polyhaven"][0],
            "subject": subject, "style_family": family,
            "tags": json.dumps(cats if isinstance(cats, list) else [str(cats)]),
            "caption": str(info.get("name") or asset_id), "era": "",
            "width": w, "height": h,
        }
        if not dry_run:
            _pending.append((row, img_bytes))
        results.append(row)
        count += 1
    if not results:
        record_parse_failure("polyhaven", "parse",
                             detail=f"列表 {len(data)} 项，取到 0 件（type/键名/URL 三处任一都可能已改）")
    print(f"[polyhaven] 入库 {count} 个（列表共 {len(data)} 项）", file=sys.stderr)
    return results


def _ambientcg_categories() -> list[str]:
    """分类清单从 **sitemap 的 `<loc>…/c/<Cat></loc>`** 读（站点的 `/list` 页是 htmx 壳，HTML 里没有件）。"""
    try:
        xml = _fetch("https://ambientcg.com/sitemap.xml", timeout=40).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        record_failure("ambientcg", "sitemap", e)
        return []
    return sorted(set(re.findall(r'ambientcg\.com/c/([A-Za-z0-9]+)</loc>', xml)))


def ingest_ambientcg(*, storage: Any, limit: int = 20, dry_run: bool = False) -> list[dict]:
    """ambientCG CC0 质感底：sitemap 拿分类 → `/hx/asset-list` 拿件 → 取 1024 缩略图。

    那个 `HX-Request: true` 不是装饰：不带它，端点只回推广块（2026-10-14 实测同一 URL
    23889B/带件 对 3416B/无件）。写死一个分类也不成立——分类清单本身要从站点读。
    """
    print("[ambientcg] 核许可...", file=sys.stderr)
    license_name, _prov = _read_license_statement("ambientcg")
    if not license_name:
        return []
    cats = _ambientcg_categories()
    if not cats:
        record_parse_failure("ambientcg", "sitemap", detail="sitemap 里没有 /c/<分类> 条目")
        return []
    print(f"[ambientcg] {len(cats)} 个分类，逐类取件...", file=sys.stderr)

    results: list[dict] = []
    count = 0
    thumb_re = (r'https://acg-media\.struffelproductions\.com/file/ambientCG-Web'
                r'/media/thumbnail/(?:\d+)-WEBP/([A-Za-z0-9_-]+)\.webp')
    for cat in cats:
        if count >= limit:
            break
        url = f"https://ambientcg.com/hx/asset-list?category={urllib.parse.quote(cat)}"
        try:
            frag = _fetch(url, timeout=30, headers={"HX-Request": "true",
                                                    "Referer": "https://ambientcg.com/"}
                          ).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            record_failure("ambientcg", "list", e, url=url, detail=cat)
            continue
        for texture_id in re.findall(thumb_re, frag):
            if count >= limit:
                break
            img_url = ("https://acg-media.struffelproductions.com/file/ambientCG-Web/media/thumbnail/"
                       f"{AMBIENTCG_THUMB_SIZE}/{texture_id}.webp")
            try:
                img_bytes = _fetch(img_url, timeout=40)
            except Exception as e:  # noqa: BLE001
                record_failure("ambientcg", "download", e, url=img_url, detail=texture_id)
                continue
            w, h, mime, ext = _image_dims(img_bytes)
            mime = mime or "image/webp"
            ext = ext or ".webp"
            sha = _sha256(img_bytes)
            row = {
                "sha256": sha, "bytes_": len(img_bytes), "mime": mime,
                "ext": ext, "object_key": f"assets/ambientcg/{sha[:12]}{ext}",
                "source": "ambientcg", "src_id": texture_id, "license": license_name,
                "attribution_required": False, "license_url": LICENSE_STATEMENTS["ambientcg"][0],
                "subject": "texture", "style_family": "texture",
                "tags": json.dumps([cat]), "caption": texture_id, "era": "",
                "width": w, "height": h,
            }
            if not dry_run:
                _pending.append((row, img_bytes))
            results.append(row)
            count += 1
    if not results:
        record_parse_failure("ambientcg", "parse",
                             detail=f"{len(cats)} 个分类读完，取到 0 件（缩略图路径或 htmx 端点已改）")
    print(f"[ambientcg] 入库 {count} 个", file=sys.stderr)
    return results


# ── (d) Open Peeps / Humaaans / Kenney / Quaternius ─────────────────

#: Open Peeps 的件挂在 Webflow CDN 上，`href` 是**绝对 URL**（旧式子只匹配站内相对路径，
#: 于是 200 一页读完、一件不剩）。收集与「0 件」回执共用这一条，两边不许各写一份。
OPENPEEPS_SVG_HREF = r'href="(https://[^"]+\.svg)"'


def ingest_openpeeps(*, storage: Any, limit: int = 50, dry_run: bool = False) -> list[dict]:
    """Open Peeps CC0 人物 SVG（部件档目前唯一的在线腿）。

    件在 Webflow CDN 上、`href` 是绝对 URL，页面上还混着 logo/hero 那类装饰 SVG——
    所以按 `peep` 过滤，只收人物本体，装饰件收了也没人搜。
    """
    print("[open-peeps] 下载首页...", file=sys.stderr)
    try:
        html = _fetch("https://openpeeps.com", timeout=25).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        record_failure("open-peeps", "site", e)
        return []
    license_name, _prov = _read_license_statement("open-peeps", html=html)
    if not license_name:
        return []

    svg_urls: list[str] = []
    seen: set[str] = set()
    for u in re.findall(OPENPEEPS_SVG_HREF, html):
        name = Path(urllib.parse.urlsplit(u).path).name
        if "_peep-" not in name and "peep" not in name.lower():
            continue
        if u not in seen:
            seen.add(u)
            svg_urls.append(u)
    results: list[dict] = []
    count = 0
    for url in svg_urls:
        if count >= limit:
            break
        name = Path(urllib.parse.urlsplit(url).path).name
        src_id = name[:-4] if name.endswith(".svg") else name
        try:
            svg_bytes = _fetch(url, timeout=25)
        except Exception as e:  # noqa: BLE001
            record_failure("open-peeps", "download", e, url=url, detail=src_id)
            continue
        if not svg_bytes.lstrip().startswith((b"<", b"<?xml")):
            record_parse_failure("open-peeps", "download", url=url,
                                 detail=f"{src_id} 回来的不是 SVG 文本（前 40B：{svg_bytes[:40]!r}）")
            continue
        sha = _sha256(svg_bytes)
        row = {
            "sha256": sha, "bytes_": len(svg_bytes), "mime": "image/svg+xml",
            "ext": ".svg", "object_key": f"assets/open-peeps/{sha[:12]}.svg",
            "source": "open-peeps", "src_id": src_id, "license": license_name,
            "attribution_required": False, "license_url": LICENSE_STATEMENTS["open-peeps"][0],
            "subject": "person", "style_family": "illustration",
            "tags": "[]", "caption": src_id, "era": "",
            "width": None, "height": None,      # 矢量件没有像素宽高，留未判（T54 量的是笔画）
        }
        if not dry_run:
            _pending.append((row, svg_bytes))
        results.append(row)
        count += 1
    if not results:
        record_parse_failure("open-peeps", "parse",
                             detail=f"首页 {len(html)}B、绝对 .svg 链接 {len(re.findall(OPENPEEPS_SVG_HREF, html))} 个，"
                                    f"其中人物件 {len(svg_urls)} 个，取到 0 件")
    print(f"[open-peeps] 入库 {count} 个（站内人物 SVG {len(svg_urls)} 个）", file=sys.stderr)
    return results


# ── 离线导入 ────────────────────────────────────────────────────────

def ingest_offline(*, storage: Any, offline_dir: str, dry_run: bool = False) -> list[dict]:
    """从本地目录导入素材（§5.2 降级第 3 档）。目录里要有 manifest.jsonl。"""
    manifest_path = Path(offline_dir) / "manifest.jsonl"
    if not manifest_path.exists():
        print(f"[offline] {manifest_path} 不存在", file=sys.stderr)
        return []
    results = []
    count = 0
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        file_path = Path(offline_dir) / entry.get("filename", "")
        if not file_path.exists():
            print(f"[offline] 跳过 {file_path}（不存在）", file=sys.stderr)
            continue
        data = file_path.read_bytes()
        sha = _sha256(data)
        row = {
            "sha256": sha, "bytes_": len(data), "mime": entry.get("mime", _guess_mime(str(file_path))),
            "ext": entry.get("ext", file_path.suffix), "object_key": entry.get("object_key", f"assets/manual/{sha[:12]}{file_path.suffix}"),
            "source": entry.get("source", "manual"), "src_id": entry.get("src_id", file_path.name),
            "license": entry.get("license", ""), "attribution_required": entry.get("attribution_required", False),
            "license_url": entry.get("license_url", ""), "subject": entry.get("subject", ""),
            "style_family": entry.get("style_family", ""), "tags": json.dumps(entry.get("tags", [])),
            "caption": entry.get("caption", file_path.name), "era": entry.get("era", ""),
            "width": entry.get("width"), "height": entry.get("height"),
        }
        if not row["license"]:
            print(f"[offline] 跳过 {file_path}（无 license）", file=sys.stderr)
            continue
        if not dry_run:
            _pending.append((row, data))
        results.append(row)
        count += 1
    print(f"[offline] 入库 {count} 个", file=sys.stderr)
    return results


# ── 存储 ────────────────────────────────────────────────────────────

def _fill_facts(row: dict, data: bytes | None) -> dict:
    """入库前把 R11-T53/T54 那两格算出来（角色、最细笔画实测）。

    **算不出来就留空**（role 空串 / scale None）：Q114 否决的正是「先人工标一遍」，
    而这里的替代品不是「猜一个最像的档」，是**如实留未判**。留空的代价是这一件不进
    按角色/按缩放安全的过滤，看得见、数得出来；猜错的代价是「明明有素材却搜不到」，
    查起来贵得多。

    就地改 `row` 并返回它——这个函数只被 `_store_asset` 调，形态上就是「写库前那一趟」。
    """
    if not row.get("asset_role"):
        if data and not row.get("width") and not row.get("height"):
            # 尺寸没随件进来的行（旧入库带过的、或读不到尺寸的）从字节里量一次再判角色：
            # `asset_role_of` 只看「长边够不够 bg 那一档」，缺尺寸时它只能按「一整幅画面」
            # 收——那不是判断，是没量。
            w, h, mime, ext = _image_dims(data)
            if w:
                row["width"], row["height"] = w, h
            if mime and not row.get("mime"):
                row["mime"], row["ext"] = mime, ext
        role, why = asset_role_of(
            subject=str(row.get("subject") or ""), style_family=str(row.get("style_family") or ""),
            mime=str(row.get("mime") or ""), ext=str(row.get("ext") or ""),
            width=row.get("width"), height=row.get("height"))
        row["asset_role"] = role
        # 判据原话进 scale_facts 的同一份回执里（这一栏叫 facts 就是「机器读出来的事实」）：
        # 出问题时能看见「这件的角色是凭什么标出来的」，不必回去读代码。
        row.setdefault("_role_why", why)
    facts = measure_asset(data, mime=str(row.get("mime") or ""), ext=str(row.get("ext") or ""))
    if row.get("_role_why"):
        facts["role_why"] = row.pop("_role_why")
    row["scale_facts"] = facts
    # **不往 row 里写 `scale_safe`**：库仓储不收这个入参（T54：任何写入路径都不接受
    # 人工传入的值），它由 `scale_facts['verdict']` 那条唯一投影算出来。
    return row


async def _store_asset(storage: Any, row: dict, data: bytes) -> None:
    """写 MinIO + asset_library 表。"""
    object_key = row["object_key"]
    _fill_facts(row, data)          # R11-T53/T54：角色与最细笔画两格，写库前算

    async def _chunks() -> AsyncIterator[bytes]:
        yield data

    try:
        await storage.objects.put(object_key, _chunks(),
                                  content_type=row.get("mime", ""),
                                  size=len(data))
    except Exception as e:
        record_failure("store", "minio_put", e, detail=object_key)
        return
    try:
        await storage.assets.add(**row)
    except Exception as e:
        record_failure("store", "asset_library_add", e, detail=str(row.get("src_id")) or "")


async def _store_batch(storage: Any, items: list[tuple[dict, bytes]]) -> None:
    """批量写 MinIO + asset_library。"""
    for row, data in items:
        await _store_asset(storage, row, data)


def _write_manifest(results: list[dict], path: str) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for row in results:
            entry = {k: v for k, v in row.items() if k != "bytes_"}
            entry["bytes"] = row.get("bytes_", 0)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_failures(rows: list[dict], path: str) -> None:
    """失败回执落盘（每行一条 JSON）。与成功 manifest 分开写：两份口径不同，
    混在一起会让「入库 N 条」这个数里混进 N 条没入的。"""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── 主入口 ──────────────────────────────────────────────────────────

#: 函数名 → 实现。注册表里每行写的是**函数名字符串**（`agent_framework.asset_sources`
#: 不能 import 本脚本——那是倒过来的依赖），所以这里把名字翻译成实现，翻译表**必须齐**：
#: 少一个名字，`--source` 就少一条腿，而「注册表说有、命令行没有」正是 T50 要灭的形状。
_INGEST_FUNCS = {
    "ingest_wikimedia": ingest_wikimedia,
    "ingest_iconify": ingest_iconify,
    "ingest_polyhaven": ingest_polyhaven,
    "ingest_ambientcg": ingest_ambientcg,
    "ingest_openpeeps": ingest_openpeeps,
}

#: `--source` 的取值域，**由注册表生成**（R11-T50）。顺序跟注册表走：注册表把
#: 「活库真有行」的两条排在最前面。
SOURCES = {l.name: _INGEST_FUNCS[l.ingest] for l in ASSET_SOURCES if l.ingest}
_missing_legs = [l.name for l in ASSET_SOURCES if l.ingest and l.ingest not in _INGEST_FUNCS]
if _missing_legs:       # 注册表与实现脱钩时**当场炸**，不静默少一条腿
    raise RuntimeError(f"注册表里的源 {_missing_legs} 指向本脚本里不存在的入库函数")


async def backfill_facts(storage: Any, *, limit: int = 100000,
                         force: bool = False) -> dict:
    """给**已在库**的行补 R11-T53/T54 那两格（角色 / 最细笔画实测）。

    为什么要单独一条路：入库脚本只在**新入**素材时算这两格，而这两格是后加的——
    库里已有的行（iconify 那批 SVG 就是最典型的一批）不补，`asset_role` 与
    `scale_safe` 在真机上就永远是空的，「按角色找件」与「部件档最小尺寸」两条
    会一起落空。补标只**读**对象（不重传、不重算 sha），所以可以在任意时刻重跑。

    幂等：已经有 `scale_facts` 的行跳过（`--force` 才重算）。重算会覆盖，
    因为这只是一个函数的输出，不是人工数据——但改判据之前要看清楚它会覆盖什么。
    """
    rows = await storage.assets.list_assets(enabled=True, limit=limit)
    stats = {"total": len(rows), "roles_filled": 0, "scale_measured": 0,
             "scale_undetermined": 0, "skipped": 0, "bytes_missing": 0,
             "dims_filled": 0}
    # T53 的验收里点名要的那一份「icon→part 的候选清单」：即便**空**也是证据——
    # 它证明缺的是采购（库里没有拼图部件），而不是标签口径没定。
    part_candidates: list[str] = []
    final_roles: list[dict] = []
    for row in rows:
        has_facts = bool((row.get("scale_facts") or {}) if isinstance(row.get("scale_facts"), dict)
                         else row.get("scale_facts"))
        if not force and row.get("asset_role") and has_facts:
            stats["skipped"] += 1
            continue
        data = await _object_bytes(storage, str(row.get("object_key") or ""))
        if data is None:
            stats["bytes_missing"] += 1
        fresh: dict = {"subject": row.get("subject") or "",
                       "style_family": row.get("style_family") or "",
                       "mime": row.get("mime") or "", "ext": row.get("ext") or "",
                       "bytes_": row.get("bytes") or 0,
                       "width": row.get("width"), "height": row.get("height")}
        if force:
            # `_fill_facts` 见到非空 role 会沿用旧标签；`--force` 要的是「判据改了，
            # 重新量一遍」，沿用等于没重算。
            fresh["asset_role"] = ""
        _fill_facts(fresh, data)
        role = str(fresh.get("asset_role") or "")
        facts = fresh.get("scale_facts") or {}
        if role and role != str(row.get("asset_role") or ""):
            stats["roles_filled"] += 1
        if facts.get("verdict") is None:
            stats["scale_undetermined"] += 1
        else:
            stats["scale_measured"] += 1
        await storage.assets.set_facts(
            str(row["id"]),
            asset_role=role or str(row.get("asset_role") or ""),
            scale_facts=facts,
            width=fresh.get("width"), height=fresh.get("height"))
        if fresh.get("width") and not row.get("width"):
            stats["dims_filled"] += 1
        final_roles.append({"asset_role": role or str(row.get("asset_role") or "")})
        if (role or row.get("asset_role")) == "part":
            part_candidates.append(str(row.get("src_id") or row.get("id") or ""))
    tally = role_facts(final_roles)
    print(f"[backfill] {stats}", file=sys.stderr)
    print(f"[backfill] 角色分布 {tally['counts']}（未判 {tally['undetermined']} / 共 {tally['total']}）",
          file=sys.stderr)
    print(f"[backfill] part 档候选 {len(part_candidates)} 件"
          + (f"：{', '.join(part_candidates[:20])}" if part_candidates
             else "（**一件都没有**——缺的是采购，不是标签口径）"), file=sys.stderr)
    stats["roles"] = tally
    stats["part_candidates"] = part_candidates
    return stats


async def _object_bytes(storage: Any, key: str) -> bytes | None:
    """从对象存储读回一件素材的字节（补标用）。读不到**记失败并返回 None**——
    「对象不在桶里」与「这件量不出来」是两件事，回执上要分得开。"""
    if not key:
        return None
    try:
        chunks = [c async for c in storage.objects.get_stream(key)]
    except Exception as e:      # noqa: BLE001
        record_failure("backfill", "object_get", e, detail=key)
        return None
    return b"".join(chunks)


async def main_async(args: argparse.Namespace) -> int:
    from agent_framework.storage import build_storage
    storage = build_storage("pg_minio")
    await storage.start()
    reset_failures()                                # 每次运行一份干净的回执

    all_results: list[dict] = []
    manifest_path = args.manifest or "assets/manifest.jsonl"

    if getattr(args, "backfill", False):
        # 补标这一路**不取件、不写 manifest**（它改的是库里已有的行，没有「新入 N 条」
        # 这个数）。跑完就把两格的账打出来，然后照常走失败回执那一段。
        stats = await backfill_facts(storage, limit=args.limit, force=args.force)
        if (stats["total"] and not stats["roles_filled"]
                and not stats["scale_measured"] and not stats["dims_filled"]):
            print("[backfill] 没有任何一格被改动——库里的行都已经标过了（或是读不到字节）",
                  file=sys.stderr)
        await storage.close()
        return 1 if get_failures() and not stats["total"] else 0

    if args.offline_dir:
        results = ingest_offline(storage=storage, offline_dir=args.offline_dir,
                                 dry_run=args.dry_run)
        all_results.extend(results)
    elif args.source == "all":
        for name, func in SOURCES.items():
            results = func(storage=storage, limit=args.limit, dry_run=args.dry_run)
            all_results.extend(results)
    elif args.source in SOURCES:
        results = SOURCES[args.source](storage=storage, limit=args.limit,
                                       dry_run=args.dry_run)
        all_results.extend(results)
    else:
        print(f"未知源: {args.source}（可用: {', '.join(SOURCES)}）", file=sys.stderr)
        return 1

    if all_results and not args.dry_run:
        await _store_batch(storage, _pending)
        _write_manifest(all_results, manifest_path)
        print(f"manifest 追加 {len(all_results)} 行 → {manifest_path}", file=sys.stderr)

    # T10：失败回执与成功回执一起落盘，「不可达」和「没素材」从此分得开。
    if get_failures():
        fail_path = str(Path(manifest_path).with_name("failures.jsonl"))
        _write_failures(get_failures(), fail_path)
        hard = [f for f in get_failures() if not f["fixable"]]
        print(f"\n⚠ {len(get_failures())} 处失败（其中 {len(hard)} 处「代码修不了」，"
              f"见回执 {fail_path}）：", file=sys.stderr)
        for f in get_failures():
            print(f"    - [{f['source']}/{f['stage']}] {f['error']} → {f['advice']}",
                  file=sys.stderr)

    print(f"\n总计入库 {len(all_results)} 个素材", file=sys.stderr)
    await storage.close()
    # 「一条没进 + 有失败」是唯一要外壳调用方也看得见的形态：0 条本身不是错误，
    # 但「0 条且不是因为没素材」必须非零退出，否则又落回「跑完了，0 条」那种静默。
    if not all_results and get_failures():
        return 1
    return 0


def main() -> int:
    # 回执里带「⚠」这类字符：Windows 宿主控制台默认 GBK，直接 print 会 UnicodeEncodeError
    # ——入库脚本自己炸在最后一行，会让「取件失败 N 件」的账看不出到底跑没跑到收尾。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:       # noqa: BLE001 —— 老解释器没有 reconfigure 就算了
        pass
    parser = argparse.ArgumentParser(description="R3 素材入库")
    parser.add_argument("--source", default="all",
                        help=f"素材源: {', '.join(SOURCES)}, all, manual")
    parser.add_argument("--limit", type=int, default=50,
                        help="每个源的最大入库数（默认 50）")
    parser.add_argument("--offline-dir", default="",
                        help="离线导入目录（§5.2 降级第 3 档）")
    parser.add_argument("--manifest", default="",
                        help="manifest.jsonl 输出路径（默认 assets/manifest.jsonl）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只探测不写库")
    parser.add_argument("--backfill", action="store_true",
                        help="不取件：给库里已有的行补算 asset_role 与 scale_safe（R11-T53/T54）")
    parser.add_argument("--force", action="store_true",
                        help="配合 --backfill：连**已经标过**的行一起重算（判据改了才用，默认跳过）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())