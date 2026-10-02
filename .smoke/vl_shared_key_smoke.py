"""真机冒烟：视觉理解走**主 LLM 那把 key**，打真 DeepSeek，出真描述。

运行：  python -u .smoke/vl_shared_key_smoke.py
前提：仓库工作区里还有那两条 B 站素材的本地副本（load_media 落的临时工作区文件）。
全程不打印任何密钥值。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server import mediaops  # noqa: E402
from storyline_server.providers import build_providers  # noqa: E402
from storyline_server.settings import Settings  # noqa: E402

WS = Path(".storyline/workspace/"
          "u_u-34f07290e10f_c_b5c005b4-8556-4d09-bc1f-2c0f749fd950/_default/material")
SOURCES = {"演讲 mat-c14d61": WS / "mat-c14d61.mp4", "海景 mat-0b0fe2": WS / "mat-0b0fe2.mp4"}

_checks = 0
_fails = 0


def check(cond: bool, label: str, extra: str = "") -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' → ' + extra) if extra else ''}")
    if not cond:
        _fails += 1


def main() -> None:
    # 关键前提：证明走的是回退路径，而不是环境变量里另有一把 key
    popped = os.environ.pop("OPENAI_API_KEY", None)
    settings = Settings.load(Path("examples/storyline/config.toml"))
    caps = settings.caps
    print(f"vl_base={caps.vl_base} vl_model={caps.vl_model} "
          f"vl_key_env={caps.vl_key_env}（该变量当前未设置）预算={caps.vl_max_tokens}")
    check(caps.vl_model == "deepseek-flash", "配置指向 deepseek-flash")

    prov = build_providers(caps)
    frames = []
    out = Path(".smoke/_vl_frames")
    out.mkdir(exist_ok=True)
    for name, src in SOURCES.items():
        if not src.is_file():
            check(False, f"缺少本地素材副本 {name}")
            continue
        f = out / f"{src.stem}_t60.jpg"
        mediaops.extract_frame(src, 60.0, f, caps.proxy_max_side)
        check(f.is_file() and f.stat().st_size > 2000, f"{name} 抽帧成功",
              f"{f.stat().st_size // 1024}KB")
        frames.append((name, f))

    if len(frames) < 2:
        print("\n素材不在盘上，无法做真机 VL 冒烟")
        sys.exit(1)

    print("\n=== 单帧逐条描述（understand_clips 的调用形状）===")
    caps_out = []
    for name, f in frames:
        text = prov.vision([f], "用一句话客观描述这帧画面里有什么，包括人物/景物/字幕文字。")
        caps_out.append(text)
        print(f"  {name}: {text[:120]}")
        check(len(text.strip()) > 10, f"{name} 拿到非占位描述")
        check("降级" not in text and "占位" not in text, f"{name} 不是降级占位文本")

    joined = " ".join(caps_out)
    check(any(k in caps_out[0] for k in ("人", "讲", "字幕", "台", "麦", "舞", "背景")),
          "演讲帧确实认出了人物/舞台类要素", caps_out[0][:60])
    check(any(k in caps_out[1] for k in ("海", "水", "浪", "天", "岸", "空")),
          "海景帧确实认出了海/天类要素", caps_out[1][:60])

    print("\n=== 三帧一次请求（批量调用的形状，B 项要用）===")
    batch = prov.vision([frames[0][1], frames[1][1], frames[1][1]],
                        "按顺序分别用一句话描述这三帧画面，逐条编号。")
    print(f"  {batch[:160]}")
    check(len(batch.strip()) > 30, "一次多图请求也拿回描述")
    check("1" in batch or "①" in batch or "、" in batch, "回复里能看出逐条结构")

    if popped is not None:
        os.environ["OPENAI_API_KEY"] = popped
    print(f"\n{_checks - _fails}/{_checks} 通过")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
