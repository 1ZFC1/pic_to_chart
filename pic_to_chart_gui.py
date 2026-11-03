#!/usr/bin/env python3
"""图片 → Rizline 谱面 图形界面入口。

    python pic_to_chart_gui.py            # 直接打开
    python pic_to_chart_gui.py 图片.png    # 打开并载入这张图
    python pic_to_chart_gui.py --selftest # 不开界面，跑一遍完整流程（打包验证用）

也可以双击同目录的 ``图片转谱面_GUI.bat``（它用 pythonw，不弹控制台），
或者用打包好的 ``图片转谱面.exe``。

注意：``pythonw`` / 打包成窗口程序后 ``sys.stdout`` / ``sys.stderr`` 是 **None**，
所以这里所有和输出流有关的操作都要先判空；任何启动期异常都会写日志并弹一个
Windows 消息框，避免“双击后只弹个黑框、没有下文”。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

#: 打包后日志写到 exe 旁边，源码运行时写到脚本旁边
HERE = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent)
LOG_PATH = HERE / "gui_error.log"
SELFTEST_LOG = HERE / "selftest.log"
SILENT_ALERT = os.environ.get("PIC2CHART_NO_ALERT") == "1"


def _alert(title: str, message: str) -> None:
    """没有控制台时用 Windows 消息框把错误显示出来。"""
    if SILENT_ALERT:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:  # noqa: BLE001 - 消息框只是尽力而为
        pass


def _log_exception(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        LOG_PATH.write_text(text, encoding="utf-8")
    except OSError:
        pass
    _alert("图片转谱面 GUI 启动失败",
           f"{exc_type.__name__}: {exc}\n\n详细信息已写入：\n{LOG_PATH}")


def _fix_streams() -> None:
    """窗口程序下输出流是 None：补一个 devnull，并把重定向时的编码统一成 UTF-8。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _selftest() -> int:
    """不开界面跑一遍：图片 → 折线 → 谱面 → 导出 → 读回，并实例化一次主窗口。

    结果写到 exe 旁边的 ``selftest.log``（窗口程序没有控制台），成功返回 0。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines: list[str] = []
    ok = True
    try:
        from PIL import Image, ImageDraw

        from pic2chart import chartbuild, pipeline

        image = Image.new("RGBA", (96, 96), (255, 255, 255, 255))
        ImageDraw.Draw(image).ellipse([16, 16, 80, 80], fill=(20, 20, 20, 255))
        ImageDraw.Draw(image).rectangle([34, 34, 62, 62], fill=(255, 255, 255, 255))

        # 四种模式都跑一遍：包瘦身后最容易缺的就是 skimage/scipy 的惰性模块
        for mode in ("scan", "column", "outline", "thin"):
            result = pipeline.convert(
                image, pipeline.ConvertOptions(mode=mode, smooth=1.0)
            )
            lines.append(f"{mode:>7}: {result.line_count} 线 / "
                         f"{result.node_count} 节点 / 画布 {result.canvas_index}")
            if result.line_count == 0:
                ok = False
                lines.append(f"!! {mode} 没有产出折线")

        # 再走一遍导出 + 读回 + 预览渲染（预览会用到 PIL 的绘图）
        result = pipeline.convert(image, pipeline.ConvertOptions(mode="outline"))
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "selftest.json"
            chartbuild.export_chart(result.chart, out)
            reloaded = chartbuild.load_chart(out)
            lines.append(f"export: {out.stat().st_size} 字节，"
                         f"读回 {len(reloaded['lines'])} 条线")
            if len(reloaded["lines"]) != len(result.chart["lines"]):
                ok = False
                lines.append("!! 导出的线数与内存里的不一致")

            png = Path(tmp) / "preview.png"
            from pic2chart import preview

            preview.render_chart(result.chart, png,
                                 speed_const=result.speed_const,
                                 line_width_world=0.04)
            lines.append(f"preview: {png.stat().st_size} 字节")

            # 图片编解码（PIL 插件）在瘦身包里最容易丢，这里真实落盘再读回
            from pic2chart import imageproc

            for suffix in (".png", ".jpg"):
                sample = Path(tmp) / f"sample{suffix}"
                image.convert("RGB").save(sample, quality=90)
                loaded = imageproc.load_image(sample)
                lines.append(f"image{suffix}: {loaded.width}×{loaded.height} 读回正常")

        from PyQt5 import QtWidgets

        from pic2chart.gui import MainWindow

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = MainWindow()
        window._silent = True
        opts = window._collect_options()
        lines.append(f"gui: 主窗口可用，BPM={opts.bpm:g} 模式={opts.mode}")
        window.close()
        app.processEvents()
    except Exception:  # noqa: BLE001
        ok = False
        lines.append("!! 自检失败：\n" + traceback.format_exc())

    text = "\n".join(lines) + f"\n结论：{'通过' if ok else '失败'}\n"
    try:
        SELFTEST_LOG.write_text(text, encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(text)
        except Exception:  # noqa: BLE001
            pass
    return 0 if ok else 1


def main() -> int:
    sys.excepthook = _log_exception
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    if "--selftest" in sys.argv[1:]:
        return _selftest()

    try:
        from PyQt5 import QtCore, QtWidgets
    except ImportError:
        _alert("缺少 PyQt5",
               "图形界面需要 PyQt5。\n\n请在命令行执行：\n"
               "    python -m pip install PyQt5\n\n然后重新双击启动。")
        return 2

    from pic2chart.gui import MainWindow

    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("图片 → Rizline 谱面")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()

    if args:
        image = Path(args[0])
        if image.exists():
            QtCore.QTimer.singleShot(0, lambda: window.load_image(image))

    return app.exec_()


if __name__ == "__main__":
    _fix_streams()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 兜底：写日志 + 弹框
        _log_exception(*sys.exc_info())
        raise SystemExit(1)
