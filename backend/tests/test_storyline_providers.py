# -*- coding: utf-8 -*-
"""providers.ASR 设备回落离线测试：用假 faster-whisper 复现「CUDA 缺库」真实现状。

本机没有 CUDA 运行库，而 CTranslate2 的 cublas64_12.dll 缺失要到**真正转写时**才
暴露（模型构造期看不出来）。配置里写着「auto→cuda，失败回落 cpu+int8」，这条回落
必须在 transcribe 阶段真发生，否则产品配置下语音识别永远拿不到结果。断言：尝试顺序
（cuda→cpu）、两种设备各自的 compute_type、空文本段丢弃、时间戳取整、可用设备被记住
（后续调用不再重复踩坑）、显式指定设备时不擅自越权回落、依赖缺失时报 ProviderError。
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
import types

sys.stdout.reconfigure(encoding="utf-8")

from storyline_server import providers
from storyline_server.providers import ProviderError, Providers
from storyline_server.settings import Capabilities

FAILS = 0
CALLS: list[dict] = []
KWARGS: list[dict] = []
FLAGS = {"fail_cuda": True}


def check(cond, label):
    global FAILS
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILS += 1


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeWhisper:
    """假 WhisperModel：cuda 设备一转写就抛 cublas 缺失，cpu 正常返回三段（含空文本）。"""

    def __init__(self, model, device="auto", compute_type=None):
        self.device = device
        CALLS.append({"model": model, "device": device, "compute_type": compute_type})

    def transcribe(self, wav, **kw):
        KWARGS.append({"wav": str(wav), "device": self.device, **kw})
        if self.device == "cuda" and FLAGS["fail_cuda"]:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return [_Seg(0.4, 2.0, " 你好世界 "), _Seg(2.6, 4.0, "   "),
                _Seg(4.1234, 5.0, "第二段")], object()


def install_fake(*, fail_cuda: bool = True, whisper_module=FakeWhisper) -> None:
    """装上假 faster-whisper 并清空进程级模型缓存/已探明设备。

    whisper_module=None 时把 None 塞进 sys.modules——这正是「包没装」的语义，
    import 会抛 ImportError。
    """
    if whisper_module is None:
        sys.modules["faster_whisper"] = None
    else:
        mod = types.ModuleType("faster_whisper")
        mod.WhisperModel = whisper_module
        sys.modules["faster_whisper"] = mod
    FLAGS["fail_cuda"] = fail_cuda
    providers._WHISPER_CACHE.clear()
    providers._ASR_DEVICE[0] = None
    CALLS.clear()
    KWARGS.clear()


def main() -> None:
    wav = "any.wav"

    # ---------- ① auto：cuda 失败必须真回落到 cpu+int8 ----------
    install_fake()
    caps = Capabilities()                      # asr_device=auto, asr_compute=auto
    segs = Providers(caps).transcribe(wav)
    check([k["device"] for k in KWARGS] == ["cuda", "cpu"]
          and [c["device"] for c in CALLS] == ["cuda", "cpu"],
          f"auto 依次尝试 cuda→cpu（{[c['device'] for c in CALLS]}）")
    check([c["compute_type"] for c in CALLS] == ["float16", "int8"],
          f"cuda 用 float16、回落 cpu 用 int8（{[c['compute_type'] for c in CALLS]}）")
    check(segs == [{"start": 0.4, "end": 2.0, "text": "你好世界"},
                   {"start": 4.123, "end": 5.0, "text": "第二段"}],
          f"空文本段丢弃 + 时间戳取整（{segs}）")
    check(KWARGS[0]["vad_parameters"] == {"min_silence_duration_ms": 1000}
          and KWARGS[0]["wav"] == wav,
          "VAD 静音阈值按 [capabilities].max_pause_sec 下发")
    check(providers._ASR_DEVICE[0] == "cpu", "可用设备被记住（_ASR_DEVICE=cpu）")

    # ---------- ② 记住之后不再重复踩坑 ----------
    CALLS.clear(); KWARGS.clear()
    Providers(caps).transcribe(wav)
    check([k["device"] for k in KWARGS] == ["cpu"],
          f"第二次调用直接从可用设备开始（{[k['device'] for k in KWARGS]}）")

    # ---------- ③ 本机真有 CUDA 时不该多载一份 CPU 模型 ----------
    install_fake(fail_cuda=False)
    Providers(caps).transcribe(wav)
    check([c["device"] for c in CALLS] == ["cuda"]
          and providers._ASR_DEVICE[0] == "cuda",
          f"cuda 可用时不加载 cpu 模型（{[c['device'] for c in CALLS]}）")

    # ---------- ④ 显式指定设备：不擅自越权回落 ----------
    install_fake()
    try:
        Providers(Capabilities(asr_device="cuda")).transcribe(wav)
        check(False, "cuda 失败且配置写死 cuda 时必须报错")
    except ProviderError as e:
        check("依次试过 ['cuda']" in str(e) and [c["device"] for c in CALLS] == ["cuda"],
              f"显式设备只试该设备，报错说明尝试过什么（{str(e)[:70]}…）")

    # ---------- ⑤ 依赖缺失 → ProviderError（节点侧走降级） ----------
    install_fake(whisper_module=None)
    try:
        Providers(Capabilities()).transcribe(wav)
        check(False, "faster-whisper 缺失时必须抛 ProviderError")
    except ProviderError as e:
        check("未安装" in str(e), f"依赖缺失也归一为 ProviderError（{e}）")

    print()
    if FAILS:
        print(f"FAILED ({FAILS} failures)")
        sys.exit(1)
    print("ALL PASSED (0 failures)")


if __name__ == "__main__":
    main()
