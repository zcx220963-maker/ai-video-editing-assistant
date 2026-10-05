"""Storyline MCP Server 启动器：python run_storyline.py [--config examples/storyline/config.toml]"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import subprocess as _sp
    _orig_popen_init = _sp.Popen.__init__
    _NO_WIN = 0x08000000
    def _popen_init_no_window(self, *a, **kw):
        kw["creationflags"] = kw.get("creationflags", 0) | _NO_WIN
        _orig_popen_init(self, *a, **kw)
    _sp.Popen.__init__ = _popen_init_no_window

import argparse
from pathlib import Path

from storyline_server.server import make_server
from storyline_server.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Storyline 视频剪辑 MCP Server")
    parser.add_argument("--config", default="examples/storyline/config.toml")
    args = parser.parse_args()
    make_server(Settings.load(Path(args.config))).run()


if __name__ == "__main__":
    main()
