"""网页渲染出片节点（Hyperframes 通道）：HTML/CSS/JS → 逐帧截图 → ffmpeg 合成视频。

分工口径（README §5.2）：本节点是「能力」，Skill 只负责教模型什么时候选它。
与 render_video 的关系：**另一条独立的终点路径**——不依赖任何剪辑节点（起点即终点），
适合把动态网页/数据大屏/图表动画直接出成片；时间线剪辑仍走 plan_timeline → render_video。

实现要点：
* 截帧用 headless 浏览器的 ``--virtual-time-budget``：把页面虚拟时钟快进到第 t 毫秒再截图。
  **但别指望 rAF 动画在这条路上是确定性的**——实测该预算下 Chrome 只发得出 1~2 个
  ``requestAnimationFrame`` 回调就饿死（``--disable-gpu-vsync``、``--run-all-compositor-
  stages-before-draw`` 都救不回来），且不报 JS 错误。所以动画必须做成「给定时刻的纯函数」：
  时刻从 ``?t=毫秒`` 传进页面、解析期同步应用（motion/ 那条通道就是这么做的）；
  靠 rAF 自走的页面在这里只会截到第一帧；
* 新旧两种 headless 模式各试一次（老版 Chrome 只认 ``--headless``）；
* 采集器做成**可注入**（``capture_backend`` 类属性）：离线测试用一个写纯色 PNG 的
  替身跑通「截图 → 合成 → probe → publish」整条管线，真浏览器路径留给真机；
* 字节经 ``workspace.publish`` 进对象存储，payload 只留对象键；``media_url`` 是
  会过期的 presigned 直链，标 ephemeral 只回调用方——与 render_video 同一条契约。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_framework.orchestration import NodeState, _safe

from .. import mediaops
from ..mediaops import MediaError
from .core_nodes import StoryNode, _obj

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd: list[str], timeout: float) -> None:
    """阻塞式子进程统一包装：非零退出码 → 带上 stderr 尾巴的 MediaError。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{cmd[0]} 超时（{timeout}s）") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "ignore")[-300:]
        raise MediaError(f"{cmd[0]} 失败：{tail}")


def _find_chrome() -> str | None:
    """定位一个 chromium 系浏览器（Chrome/Edge 都行，headless 截图能力等价）。"""
    env = os.environ.get("STORYLINE_CHROME") or os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lac = os.environ.get("LOCALAPPDATA", "")
        cands: list[str] = []
        for root in (pf, pf86, lac):
            if root:
                cands += [str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                          str(Path(root) / "Microsoft/Edge/Application/msedge.exe")]
        hits = [c for c in cands if c and Path(c).exists()]
        return hits[0] if hits else None
    if sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
        hits = [c for c in cands if Path(c).exists()]
        return hits[0] if hits else None
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def chrome_container_flags() -> list[str]:
    """容器里跑 headless 的豁免参数——两条，一条都不多余。

    * ``--disable-dev-shm-usage``：docker 默认给 ``/dev/shm`` 64MB，chromium 把页面
      共享内存用满就直接 SIGBUS（表现为"截图没落盘"，不是代码错）。
    * ``--no-sandbox``：只在 **root** 下补。非 root 一律留着沙箱——我们截的是模型
      写的 SVG 和网上抓来的页面，沙箱是唯一那道隔离，能保就保。
    """
    flags = ["--disable-dev-shm-usage"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        flags.insert(0, "--no-sandbox")
    return flags


def _assemble(work: Path, n: int, fps: int, out: Path) -> None:
    """帧序列 → mp4（yuv420p + faststart，浏览器内联播放友好）。"""
    cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(work / "f%04d.png"),
           "-frames:v", str(n), "-vf", "format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart",
           str(out)]
    _run(cmd, timeout=max(120.0, n * 2.0))


def _chrome_frame(chrome: str, target: str, t_ms: int, png: Path,
                  w: int, h: int) -> None:
    """截一帧：虚拟时钟快进到 t_ms 再截图（确定性动画帧）。"""
    png.parent.mkdir(parents=True, exist_ok=True)
    base = [chrome, "--disable-gpu", "--hide-scrollbars", *chrome_container_flags(),
            f"--window-size={w},{h}", f"--screenshot={png}",
            f"--virtual-time-budget={max(1, t_ms)}",
            "--default-background-color=00000000"]
    last = ""
    # 新 headless 优先；老内核不认 --headless=new 时退回旧参数再试一次
    for headless in ("--headless=new", "--headless"):
        cmd = [chrome, headless, *base[1:], target]
        try:
            _run(cmd, timeout=60.0)
        except MediaError as e:
            last = str(e)
            continue
        # 退出码 0 却没落盘是真会发生的：渲染进程崩了、/dev/shm 用满。不在这儿拦下来，
        # 就是几分钟后 ffmpeg 读到一张不存在的 PNG，错误里连是哪个时刻都说不清。
        if png.exists() and png.stat().st_size > 0:
            return
        last = f"{png.name} 没落盘（浏览器退出码 0）"
    raise MediaError(f"headless 截图失败（新旧两种 headless 模式都试过）：{last}")


def _with_t(target: str, t_ms: int) -> str:
    """把「这一帧是第几毫秒」拼进 URL：``?t=`` 或 ``&t=``，有 #fragment 时插在它之前。

    为什么必须显式给时刻而不只靠 ``--virtual-time-budget``：那个预算只喂得动
    setTimeout/CSS 过渡，rAF 在预算下会饿死（见模块 docstring）——页面读不到 t，
    就只能停在第一帧。
    """
    head, _, frag = target.partition("#")
    sep = "&" if "?" in head else "?"
    return f"{head}{sep}t={int(t_ms)}{('#' + frag) if frag else ''}"


class RenderWebNode(StoryNode):
    name = "render_web"
    display_name = "网页渲染出片"
    description = ("把一段网页(完整 HTML/CSS/JS 或 URL)渲染成视频：headless 浏览器"
                   "按虚拟时钟逐帧截图后由 ffmpeg 合成。独立终点节点,不依赖剪辑链;"
                   "适合数据大屏/图表动画/动效演示直接出片")
    required_nodes: list[str] = []
    ephemeral = ("media_url",)
    input_schema = _obj(
        "网页出片", {
            "html": {"type": "string",
                     "description": "完整 HTML 文档(内联 CSS/JS 动画);与 url 二选一。"
                                    "**动画必须写成「给定时刻的纯函数」**：每帧的 URL 会带上 "
                                    "?t=毫秒，页面要在解析期读 location.search 的 t 直接把画面"
                                    "摆成那个时刻的样子；不要靠 requestAnimationFrame 自走——"
                                    "headless 的虚拟时钟下 rAF 只回调一两次就饿死，那样整片会"
                                    "停在第一帧"},
            "url": {"type": "string", "description": "网页地址(与 html 二选一)"},
            "duration_sec": {"type": "number", "description": "成片时长(秒),默认 5,0.5~60"},
            "fps": {"type": "integer", "description": "帧率,默认 10(1~30)"},
            "width": {"type": "integer", "description": "画布宽,默认 1280"},
            "height": {"type": "integer", "description": "画布高,默认 720"},
            "title": {"type": "string", "description": "成片标题"},
        })

    # 测试注入位：签名与 _chrome_frame 相同 (chrome, target, t_ms, png, w, h)。
    # 生产为 None → 用真 chrome；离线测试覆写成纯色 PNG 生成器跑通整条管线。
    capture_backend: Any = None
    _chrome_bin: str | None = None

    async def _capture_frame(self, target: str, t_ms: int, png: Path,
                             w: int, h: int) -> None:
        if type(self).capture_backend is not None:
            await asyncio.to_thread(type(self).capture_backend,
                                    target, t_ms, png, w, h)
            return
        chrome = type(self)._chrome_bin
        if chrome is None:
            chrome = type(self)._chrome_bin = _find_chrome()
        if chrome is None:
            raise MediaError(
                "找不到 headless 浏览器（chrome/edge）："
                "设 STORYLINE_CHROME 指到可执行文件后重试")
        await asyncio.to_thread(_chrome_frame, chrome, _with_t(target, t_ms),
                                t_ms, png, w, h)

    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        html = str(inputs.get("html") or "").strip()
        url = str(inputs.get("url") or "").strip()
        if not html and not url:
            raise ValueError("render_web：html 与 url 至少给一个")

        fps = min(max(int(inputs.get("fps") or 10), 1), 30)
        dur = min(max(float(inputs.get("duration_sec") or 5.0), 0.5), 60.0)
        frames = min(int(round(dur * fps)), 600)      # 上限防失控:600 帧 = 60s@10fps
        frames = max(frames, 1)
        w = min(max(int(inputs.get("width") or 1280), 64), 3840)
        h = min(max(int(inputs.get("height") or 720), 64), 2160)
        title = str(inputs.get("title") or "网页出片")[:80]

        work = self._work(state, "web_render")
        if html:
            page = work / "page.html"
            page.write_text(html, encoding="utf-8")
            # 绝对化后才转 URI：workspace_root 在配置里可以是相对路径
            target = page.resolve().as_uri()
        else:
            target = url

        for i in range(frames):
            await self._capture_frame(target, int(i * 1000 / fps),
                                      work / f"f{i:04d}.png", w, h)
        out = work / "out.mp4"
        await asyncio.to_thread(_assemble, work, frames, fps, out)
        info = await asyncio.to_thread(mediaops.probe, out)

        object_key = (f"renders/{_safe(state.session_id)}/"
                      f"{_safe(state.artifact_id)}/web.mp4")
        await self.storage.workspace.publish(out, object_key, content_type="video/mp4")
        return {"video": object_key, "duration": info["duration"],
                "width": info["width"], "height": info["height"], "title": title,
                "web_render": {"frames": frames, "fps": fps,
                               "planned_sec": dur,
                               "source": "url" if not html else "html"},
                "media_url": await self.storage.objects.presign_get(object_key)}
