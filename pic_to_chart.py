#!/usr/bin/env python3
"""图片 → Rizline 谱面 命令行长脚本。

用法示例：

    python pic_to_chart.py pic.png -o chart.json --preview preview.png

    python pic_to_chart.py logo.png --mode outline --ink 160 --layers 2 \
        --preview logo_preview.png

    python pic_to_chart.py --calibrate calibrate.json

详细说明见同目录 README.md。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 被重定向/管道时统一按 UTF-8 输出；交互式终端保持系统编码（cmd 的 cp936 也能正确显示中文）。
# 注意 pythonw 下 sys.stdout 是 None，这里必须先判空。
for _stream in (sys.stdout, sys.stderr):
    if _stream is None:
        continue
    try:
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from pic2chart.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
