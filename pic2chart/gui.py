"""PyQt5 图形界面：选图 → 调参 → 实时预览 → 导出谱面。

入口是同目录下的 ``pic_to_chart_gui.py``（或双击 ``图片转谱面_GUI.bat``）。

界面分左右两栏：左边是参数，右边是预览（可以切换“谱面渲染 / 原图对比”）。
参数一改就会在后台线程重算，不会卡界面。
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path

from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets

from . import chartbuild, imageproc, pipeline, preview
from .mapping import (
    DEFAULT_LINE_WIDTH_WORLD,
    DEFAULT_SPEED_CONST,
    DEFAULT_VIEW_HEIGHT_WORLD,
    DEFAULT_WIDTH_X,
    WORLD_X_PER_X_POSITION,
    speed_const_from_flow,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
PREVIEW_MAX_WIDTH = 1100


# --------------------------------------------------------------------- 小工具
def pil_to_pixmap(image: Image.Image) -> QtGui.QPixmap:
    """PIL → QPixmap（手动转 RGB888，避免依赖 ImageQt 的缓冲区生命周期）。"""
    if image.mode != "RGB":
        image = image.convert("RGB")
    data = image.tobytes("raw", "RGB")
    qimage = QtGui.QImage(data, image.width, image.height,
                          image.width * 3, QtGui.QImage.Format_RGB888)
    return QtGui.QPixmap.fromImage(qimage.copy())


class PreviewLabel(QtWidgets.QLabel):
    """按窗口大小自动缩放的预览区。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumSize(360, 300)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Expanding)
        self.setStyleSheet("background: #1b1b20;")
        self._pixmap: QtGui.QPixmap | None = None

    def set_pixmap(self, pixmap: QtGui.QPixmap | None) -> None:
        self._pixmap = pixmap
        self._refresh()

    def clear_preview(self) -> None:
        self._pixmap = None
        self.setText("还没有预览\n\n把图片拖进来，或点左上角“选择图片…”")
        self.setStyleSheet("background: #1b1b20; color: #8a8a95; font-size: 14px;")

    def _refresh(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        self.setStyleSheet("background: #1b1b20;")
        self.setText("")
        area = self.size()
        scaled = self._pixmap.scaled(
            max(64, area.width() - 16), max(64, area.height() - 16),
            QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event):  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._refresh()


class _Signals(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, str, int, int)
    finished = QtCore.pyqtSignal(int, object)
    failed = QtCore.pyqtSignal(int, str)
    cancelled = QtCore.pyqtSignal(int)


class _ConvertJob(QtCore.QRunnable):
    """后台任务：图片 → 谱面 →（预览图 | 导出谱面）。"""

    def __init__(self, generation: int, image: Image.Image,
                 opts: pipeline.ConvertOptions, base_chart: dict | None,
                 relay: _Signals, *, compare: bool = False,
                 screen_overlay: bool = False,
                 export_path: str | None = None,
                 workshop_db: dict | None = None,
                 export_preview: bool = False,
                 cancel=None):
        super().__init__()
        self.generation = generation
        self.image = image
        self.opts = opts
        self.base_chart = base_chart
        self.relay = relay
        self.compare = compare
        self.screen_overlay = screen_overlay
        self.export_path = export_path
        self.workshop_db = workshop_db
        self.export_preview = export_preview
        self.cancel = cancel

    def _report(self, stage: str, done: int = 0, total: int = 0) -> None:
        self.relay.progress.emit(self.generation, stage, int(done), int(total))

    def _preview_view_width(self, result: pipeline.ConvertResult) -> float:
        """预览横向至少留一屏宽（按缩放折算），这样“横向中心”的偏移看得见。"""
        zoom = max(1e-6, self.opts.zoom)
        screen = WORLD_X_PER_X_POSITION / zoom
        content = max(1e-6, result.width_x) * WORLD_X_PER_X_POSITION
        return max(screen, content * 1.1)

    def run(self) -> None:  # noqa: D102
        try:
            result = pipeline.convert(
                self.image, self.opts, self.base_chart,
                on_progress=self._report, cancel=self.cancel,
            )
            if self.export_path:
                self._report("写入谱面")
                chartbuild.export_chart(result.chart, self.export_path,
                                        extra_db=self.workshop_db)
                saved = [self.export_path]
                if self.export_preview:
                    png = Path(self.export_path).with_name(
                        Path(self.export_path).stem + "_preview.png")
                    self._report("渲染预览图")
                    preview.render_chart(
                        result.chart, png,
                        speed_const=result.speed_const,
                        line_width_world=self.opts.line_width,
                        screen_world=result.view_height,
                        canvas_index=result.canvas_index,
                        tick_range=(result.cmap.start_tick, result.cmap.end_tick),
                        view_width_world=self._preview_view_width(result),
                    )
                    saved.append(str(png))
                self.relay.finished.emit(self.generation,
                                         ("export", result, saved))
                return

            self._report("渲染预览")
            # 预览只画“目标画布 + 这张图的拍数范围”，不把整张谱面都画出来
            rendered = preview.render_chart_image(
                result.chart,
                speed_const=result.speed_const,
                line_width_world=self.opts.line_width,
                max_width=PREVIEW_MAX_WIDTH,
                screen_world=result.view_height if self.screen_overlay else None,
                canvas_index=result.canvas_index,
                tick_range=(result.cmap.start_tick, result.cmap.end_tick),
                view_width_world=self._preview_view_width(result),
            )
            if self.compare:
                rendered = preview.combine_images(self.image, rendered)
            self.relay.finished.emit(self.generation,
                                     ("preview", result, rendered))
        except pipeline.ConvertCancelled:
            self.relay.cancelled.emit(self.generation)
        except Exception as exc:  # noqa: BLE001 - 统一回报给界面
            detail = f"{exc}"
            if not str(exc).strip():
                detail = traceback.format_exc(limit=1)
            self.relay.failed.emit(self.generation, detail)


# --------------------------------------------------------------------- 主窗口
class MainWindow(QtWidgets.QMainWindow):
    """图片转谱面的主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("图片 → Rizline 谱面")
        self.resize(1180, 780)
        self.setAcceptDrops(True)

        self._image_path: Path | None = None
        self._image: Image.Image | None = None
        self._result: pipeline.ConvertResult | None = None
        self._generation = 0
        self._jobs: list[_ConvertJob] = []
        self._silent = False        # True 时不弹消息框（离屏测试用）
        self._busy = False
        self._cancel = threading.Event()
        # 信号中继交给 QApplication 持有：窗口销毁后后台线程也不会 emit 到野对象
        self._relay = _Signals()
        _app = QtCore.QCoreApplication.instance()
        if _app is not None:
            self._relay.setParent(_app)
        self._relay.progress.connect(self._on_progress)
        self._relay.finished.connect(self._on_finished)
        self._relay.failed.connect(self._on_failed)
        self._relay.cancelled.connect(self._on_cancelled)
        self._pool = QtCore.QThreadPool()
        self._pool.setMaxThreadCount(1)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(280)
        self._timer.timeout.connect(self._start_convert)

        self._build_ui()
        self._preview.clear_preview()
        self._set_status("把图片拖进窗口，或用左上角“选择图片…”")

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self._build_controls())
        self._preview = PreviewLabel()
        splitter.addWidget(self._preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 750])
        layout.addWidget(splitter, 1)
        layout.addWidget(self._build_progress_row())
        self.setCentralWidget(central)

        bar = QtWidgets.QStatusBar()
        self.setStatusBar(bar)
        self._status = QtWidgets.QLabel("")
        bar.addWidget(self._status, 1)

    def _build_progress_row(self) -> QtWidgets.QWidget:
        """整窗宽度的进度条：转换/导出在后台跑，可取消。"""
        self.progress_box = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.progress_box)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)
        self.progress_label = QtWidgets.QLabel("就绪")
        self.progress_label.setMinimumWidth(200)
        self.progress_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMinimumHeight(16)
        self.progress_bar.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                        QtWidgets.QSizePolicy.Fixed)
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.cancel_button.setFixedWidth(64)
        self.cancel_button.clicked.connect(self._cancel_job)
        row.addWidget(self.progress_label)
        row.addWidget(self.progress_bar, 1)
        row.addWidget(self.cancel_button)
        self.progress_box.setVisible(False)
        return self.progress_box

    def _build_controls(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 6, 0)
        outer.setSpacing(8)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(inner)
        box.setSpacing(10)
        scroll.setWidget(inner)

        # --- 图片 ----
        image_group, image_form = self._group(box, "图片")
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("拖入图片，或点“选择图片…”")
        pick = QtWidgets.QPushButton("选择图片…")
        pick.clicked.connect(self._pick_image)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.path_edit, 1)
        row.addWidget(pick)
        image_form.addRow(row)
        self.info_label = QtWidgets.QLabel("—")
        self.info_label.setStyleSheet("color:#777;")
        image_form.addRow(self.info_label)

        self.append_box = QtWidgets.QGroupBox("追加到已有谱面（可选）")
        append_layout = QtWidgets.QVBoxLayout(self.append_box)
        self.append_check = QtWidgets.QCheckBox("追加到已有谱面，而不是新建")
        self.append_check.toggled.connect(self._schedule)
        append_row = QtWidgets.QHBoxLayout()
        self.append_edit = QtWidgets.QLineEdit()
        self.append_edit.setPlaceholderText("选择要追加的谱面 JSON")
        append_pick = QtWidgets.QPushButton("…")
        append_pick.setFixedWidth(32)
        append_pick.clicked.connect(self._pick_chart)
        append_row.addWidget(self.append_edit, 1)
        append_row.addWidget(append_pick)
        self.append_auto_check = QtWidgets.QCheckBox("自动接在现有画布之后")
        self.append_auto_check.setToolTip("勾上：新内容接在现有画布之后，不覆盖原谱面；\n"
                                          "关掉：改用“位置与比例”里的“目标画布”")
        self.append_auto_check.setChecked(True)
        self.append_auto_check.toggled.connect(self._schedule)
        append_layout.addWidget(self.append_check)
        append_layout.addLayout(append_row)
        append_layout.addWidget(self.append_auto_check)
        box.addWidget(self.append_box)

        # --- 模式 ---
        mode_group, mode_form = self._group(box, "模式")
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("逐行扫描（照片/插画，横线）", "scan")
        self.mode_combo.addItem("逐列扫描（照片/插画，竖线）", "column")
        self.mode_combo.addItem("轮廓描边（线稿/logo）", "outline")
        self.mode_combo.addItem("骨架中心线（一笔一画）", "thin")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_form.addRow("转换方式", self.mode_combo)

        # --- 位置与比例 ---
        pos_group, pos_form = self._group(box, "位置与比例")
        self.bpm_spin = self._dspin(1.0, 600.0, 175.0, 0.1, pos_form, "BPM")
        self.beat_spin = self._dspin(0.0, 99999.0, 8.0, 1.0, pos_form, "起始拍")
        self.width_spin = self._dspin(0.0, 20.0, 0.0, 0.05, pos_form, "宽度（0=自动）")
        self.screens_spin = self._dspin(0.0, 10.0, 1.0, 0.1, pos_form, "占几屏高（0=不用）")
        self.center_spin = self._dspin(-5.0, 5.0, 0.0, 0.05, pos_form, "横向中心")
        self.flow_spin = self._dspin(0.1, 20.0, 1.5, 0.1, pos_form, "播放流速")
        self.flow_spin.setToolTip("编辑器里设定的播放流速（默认 1.5）。\n"
                                  "实测它对一屏拍数只有几个百分点的影响。")
        self.zoom_spin = self._dspin(0.2, 3.0, 1.0, 0.05, pos_form, "编辑器缩放")
        self.canvas_spin = QtWidgets.QDoubleSpinBox()
        self.canvas_spin.setRange(0, 63)
        self.canvas_spin.setDecimals(0)
        self.canvas_spin.setValue(0)
        self.canvas_spin.valueChanged.connect(self._schedule)
        self.canvas_spin.setToolTip("线节点写到哪个画布（canvasIndex）。\n"
                                    "目标画布不存在时会自动补齐；追加到已有谱面时\n"
                                    "注意别写了不存在的编号。")
        pos_form.addRow("目标画布", self.canvas_spin)
        self.screen_label = QtWidgets.QLabel("—")
        self.screen_label.setStyleSheet("color:#666;")
        pos_form.addRow("一屏拍数", self.screen_label)

        # --- 扫描参数 ---
        self.scan_group = QtWidgets.QGroupBox("扫描参数")
        scan_form = QtWidgets.QFormLayout(self.scan_group)
        self.cols_spin = self._dspin(0, 600, 0, 1, scan_form, "列数（0=自动）")
        self.rows_spin = self._dspin(0, 400, 0, 1, scan_form, "行数（0=自动）")
        self.tolerance_spin = self._dspin(0.0, 128.0, 8.0, 1.0, scan_form, "颜色容差")
        self.alpha_spin = self._dspin(0, 255, 8, 1, scan_form, "透明阈值")
        self.whole_row_check = QtWidgets.QCheckBox("整行模式（线少节点多）")
        self.whole_row_check.toggled.connect(self._schedule)
        scan_form.addRow(self.whole_row_check)
        box.addWidget(self.scan_group)

        # --- 线稿参数 ---
        self.ink_group = QtWidgets.QGroupBox("线稿参数")
        ink_form = QtWidgets.QFormLayout(self.ink_group)
        self.ink_spin = QtWidgets.QDoubleSpinBox()
        self.ink_spin.setRange(-1.0, 255.0)
        self.ink_spin.setDecimals(1)
        self.ink_spin.setValue(-1.0)
        self.ink_spin.setSpecialValueText("自动")
        self.ink_spin.valueChanged.connect(self._schedule)
        ink_form.addRow("墨迹亮度阈值", self.ink_spin)
        self.invert_check = QtWidgets.QCheckBox("反相（黑底白线）")
        self.invert_check.toggled.connect(self._schedule)
        ink_form.addRow(self.invert_check)
        self.outline_tolerance = self._dspin(0.1, 8.0, 1.0, 0.1, ink_form, "轮廓容差(px)")
        self.smooth_spin = self._dspin(0.0, 4.0, 0.0, 0.1, ink_form, "轮廓平滑(px)")
        self.smooth_spin.setToolTip("0 = 关；1.0~1.5 能去掉像素阶梯/小噪点，\n"
                                    "角点会被保住，轮廓可能整体差半个像素")
        self.simplify_spin = self._dspin(0.0, 8.0, 0.8, 0.1, ink_form, "骨架简化(px)")
        self.layers_spin = self._dspin(1, 8, 1, 1, ink_form, "描边层数")
        self.layer_gap_spin = self._dspin(1, 20, 2, 1, ink_form, "层间距(px)")
        self.min_len_spin = self._dspin(0.0, 200.0, 4.0, 1.0, ink_form, "最短长度(px)")
        self.color_edit = QtWidgets.QLineEdit()
        self.color_edit.setPlaceholderText("留空 = 从图片取色，如 #FF3355")
        self.color_edit.textChanged.connect(self._schedule)
        ink_form.addRow("固定描边色", self.color_edit)
        box.addWidget(self.ink_group)

        # --- 预览 ---
        view_group, view_form = self._group(box, "预览")
        self.view_combo = QtWidgets.QComboBox()
        self.view_combo.addItem("谱面渲染", "chart")
        self.view_combo.addItem("原图 | 谱面渲染", "compare")
        self.view_combo.currentIndexChanged.connect(self._schedule)
        view_form.addRow("显示", self.view_combo)
        self.line_width_spin = self._dspin(0.01, 0.4, DEFAULT_LINE_WIDTH_WORLD, 0.005,
                                           view_form, "线宽（世界单位）", decimals=3)
        self.screens_check = QtWidgets.QCheckBox("画出每屏分界线")
        self.screens_check.toggled.connect(self._schedule)
        view_form.addRow(self.screens_check)
        self.export_preview_check = QtWidgets.QCheckBox("导出谱面时同时存预览图")
        self.export_preview_check.setChecked(False)      # 默认不存，需要时再勾
        view_form.addRow(self.export_preview_check)

        outer.addWidget(scroll, 1)

        # --- 按钮 ---
        buttons = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("刷新预览 (F5)")
        self.refresh_button.clicked.connect(self._start_convert)
        self.export_button = QtWidgets.QPushButton("导出谱面… (Ctrl+S)")
        self.export_button.clicked.connect(self._export)
        self.calibrate_button = QtWidgets.QPushButton("生成标定谱面…")
        self.calibrate_button.clicked.connect(self._calibrate)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.calibrate_button)
        outer.addLayout(buttons)

        self._on_mode_changed()
        return panel

    def _group(self, parent_layout, title):
        group = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(group)
        parent_layout.addWidget(group)
        return group, form

    def _dspin(self, low, high, value, step, form, label, decimals=2):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.valueChanged.connect(self._schedule)
        form.addRow(label, spin)
        return spin

    def _on_mode_changed(self, *_args) -> None:
        mode = self.mode_combo.currentData()
        self.scan_group.setVisible(mode in ("scan", "column"))
        self.ink_group.setVisible(mode in ("outline", "thin"))
        outline_only = mode == "outline"
        self.outline_tolerance.setEnabled(outline_only)
        self.layers_spin.setEnabled(outline_only)
        self.layer_gap_spin.setEnabled(outline_only)
        self.smooth_spin.setEnabled(outline_only)
        self.simplify_spin.setEnabled(mode == "thin")
        self._schedule()

    # ------------------------------------------------------------------ 事件
    def dragEnterEvent(self, event):  # noqa: N802
        for url in event.mimeData().urls():
            if Path(url.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES:
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):  # noqa: N802
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in IMAGE_SUFFIXES:
                self.load_image(path)
                return

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == QtCore.Qt.Key_F5:
            self._start_convert()
        elif (event.key() == QtCore.Qt.Key_S
              and event.modifiers() & QtCore.Qt.ControlModifier):
            self._export()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):  # noqa: N802
        """关窗口时先取消后台任务，避免工作线程回调到已销毁的对象。"""
        self._timer.stop()
        self._cancel.set()
        self._pool.clear()
        self._pool.waitForDone(20_000)
        super().closeEvent(event)

    # ------------------------------------------------------------------ 载入
    def _pick_image(self) -> None:
        start = str(self._image_path.parent) if self._image_path else ""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择图片", start,
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif);;所有文件 (*)",
        )
        if path:
            self.load_image(Path(path))

    def _pick_chart(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择要追加的谱面", "", "谱面 (*.json);;所有文件 (*)"
        )
        if path:
            self.append_edit.setText(path)
            self.append_check.setChecked(True)
            self._schedule()

    def load_image(self, path: Path) -> None:
        try:
            image = imageproc.load_image(path, max_size=1600)
        except imageproc.ImageError as exc:
            self._show_error(str(exc))
            return
        self._image_path = path
        self._image = image
        self.path_edit.setText(str(path))
        alpha_min = image.getchannel("A").getextrema()[0] if image.mode == "RGBA" else 255
        has_alpha = alpha_min < 250
        self.info_label.setText(
            f"{image.width}×{image.height}px"
            + ("（含透明）" if has_alpha else "（不透明，线稿请设亮度阈值）")
        )
        self._start_convert()

    # ------------------------------------------------------------------ 转换
    def _schedule(self, *_args) -> None:
        if self._image is None:
            return
        self._timer.start()

    def _collect_options(self) -> pipeline.ConvertOptions:
        def float_or(value, fallback):
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        ink = self.ink_spin.value()
        color_text = self.color_edit.text().strip()
        color = None
        if color_text:
            try:
                color = imageproc.hex_to_rgba(color_text)
            except imageproc.ImageError as exc:
                raise chartbuild.ChartError(str(exc)) from exc

        return pipeline.ConvertOptions(
            mode=self.mode_combo.currentData(),
            bpm=self.bpm_spin.value(),
            start_beat=self.beat_spin.value(),
            width_x=self.width_spin.value(),
            max_width_x=DEFAULT_WIDTH_X,
            screens=self.screens_spin.value(),
            visible_ticks=0.0,
            zoom=self.zoom_spin.value(),
            center_x=self.center_spin.value(),
            flow=self.flow_spin.value(),
            speed_const=DEFAULT_SPEED_CONST,
            view_height=DEFAULT_VIEW_HEIGHT_WORLD,
            canvas=int(round(self.canvas_spin.value())),
            cols=int(self.cols_spin.value()),
            rows=int(self.rows_spin.value()),
            color_tolerance=self.tolerance_spin.value(),
            alpha_cut=int(self.alpha_spin.value()),
            whole_row=self.whole_row_check.isChecked(),
            ink=None if ink < 0 else ink,
            invert=self.invert_check.isChecked(),
            tolerance=self.outline_tolerance.value(),
            simplify=self.simplify_spin.value(),
            layers=int(self.layers_spin.value()),
            layer_gap=int(self.layer_gap_spin.value()),
            min_length=self.min_len_spin.value(),
            smooth=self.smooth_spin.value(),
            color=color,
            line_width=self.line_width_spin.value(),
        )

    def _start_convert(self) -> None:
        if self._image is None:
            return
        try:
            opts = self._collect_options()
        except chartbuild.ChartError as exc:
            self._show_error(str(exc))
            return

        base_chart = None
        if self.append_check.isChecked():
            chart_path = self.append_edit.text().strip()
            if not chart_path:
                self._show_error("勾选了“追加到已有谱面”，但还没选谱面文件")
                return
            try:
                base_chart = chartbuild.ensure_canvas(chartbuild.load_chart(chart_path))
            except chartbuild.ChartError as exc:
                self._show_error(str(exc))
                return
            if self.append_auto_check.isChecked():
                # 接在现有画布之后，别覆盖原谱面的内容
                count = chartbuild.load_library().getCanvasCount(base_chart) or 0
                opts.canvas = count

        self._generation += 1
        self._cancel.clear()
        self.cancel_button.setEnabled(True)
        compare = self.view_combo.currentData() == "compare"
        job = _ConvertJob(self._generation, self._image, opts, base_chart, self._relay,
                          compare=compare,
                          screen_overlay=self.screens_check.isChecked(),
                          cancel=self._cancel)
        self._launch(job, "预览")

    def _launch(self, job: _ConvertJob, label: str) -> None:
        job.setAutoDelete(False)          # 由 self._jobs 持有，槽函数里再释放
        self._jobs.append(job)
        self._set_busy(True, f"{label}：准备中…")
        self._pool.clear()
        self._pool.start(job)

    def _set_busy(self, busy: bool, label: str = "") -> None:
        """忙碌时禁用按钮、显示进度条与取消按钮。"""
        self._busy = busy
        self.refresh_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy)
        self.progress_box.setVisible(busy)
        self.cancel_button.setVisible(busy)
        if busy:
            self.progress_label.setText(label)
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_label.setText("就绪")
            self.cancel_button.setEnabled(True)

    def _cancel_job(self) -> None:
        if not self._busy:
            return
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.progress_label.setText("正在取消…")

    @QtCore.pyqtSlot(int, str, int, int)
    def _on_progress(self, generation: int, stage: str, done: int, total: int) -> None:
        if generation != self._generation or not self._busy:
            return
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(done, total))
            percent = (done * 100 // total) if total else 0
            self.progress_label.setText(f"{stage} {done}/{total}（{percent}%）")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(stage or "处理中…")

    def _release_job(self, generation: int) -> None:
        self._jobs = [job for job in self._jobs if job.generation != generation]

    @QtCore.pyqtSlot(int, object)
    def _on_finished(self, generation: int, payload) -> None:
        self._release_job(generation)
        if generation != self._generation:
            return          # 已经有更新的参数了，丢弃这次结果
        mode, result, extra = payload
        self._set_busy(False)
        if mode == "export":
            self._finish_export(result, extra)
            return
        rendered = extra
        self._result = result
        self._preview.set_pixmap(pil_to_pixmap(rendered))
        lines = pipeline.summary_lines(result, self._collect_options())
        self._set_status("    ".join(lines[:4]))
        self._detail = "\n".join(lines)
        self.screen_label.setText(
            f"{result.screen_ticks:.2f} 拍（V {result.speed_const:.2f}）"
        )

    @QtCore.pyqtSlot(int, str)
    def _on_failed(self, generation: int, message: str) -> None:
        self._release_job(generation)
        if generation != self._generation:
            return
        self._set_busy(False)
        self._result = None
        self._preview.clear_preview()
        self._show_error(message)

    @QtCore.pyqtSlot(int)
    def _on_cancelled(self, generation: int) -> None:
        self._release_job(generation)
        if generation != self._generation:
            return
        self._set_busy(False)
        self._set_status("已取消")

    def _finish_export(self, result: pipeline.ConvertResult, saved: list[str]) -> None:
        append_mode = (self.append_check.isChecked()
                       and bool(self.append_edit.text().strip()))
        action = "已追加到" if append_mode else "已导出"
        self._set_status(
            f"{action}：" + " + ".join(Path(p).name for p in saved)
            + f"（画布 {result.canvas_index}，{result.line_count} 条线）")
        if not self._silent:
            detail = (f"写入画布 {result.canvas_index}，{result.line_count} 条线 / "
                      f"{result.node_count} 节点")
            if append_mode:
                detail = (f"追加到：{Path(self.append_edit.text().strip()).name}\n"
                          + detail)
            QtWidgets.QMessageBox.information(
                self, "完成", "\n".join(saved) + "\n\n" + detail)

    # ------------------------------------------------------------------ 输出
    def _default_export_path(self) -> Path:
        if self.append_check.isChecked() and self.append_edit.text().strip():
            return Path(self.append_edit.text().strip())
        if self._image_path:
            return self._image_path.with_name(self._image_path.stem + "_chart.json")
        return Path.cwd() / "chart.json"

    def _confirm_overwrite(self, target: Path, append_mode: bool) -> bool:
        """目标文件已存在时的确认（``_silent`` 下自动通过）。"""
        if self._silent:
            return True
        if append_mode:
            text = (f"将把这张图追加进：\n{target}\n\n"
                    f"该文件会被覆盖：原有内容保留，新内容接在后面。\n继续？")
        else:
            text = f"文件已存在，将被覆盖：\n{target}\n\n继续？"
        answer = QtWidgets.QMessageBox.question(
            self, "覆盖确认", text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return answer == QtWidgets.QMessageBox.Yes

    def _export(self, path: str | None = None) -> None:
        """导出谱面（后台线程，带进度与取消）；``path`` 为空时弹文件对话框。"""
        if self._result is None:
            self._show_error("还没有可导出的谱面：换个参数或先刷新预览")
            return
        if self._busy:
            self._show_error("正在处理，等它跑完（或点“取消”）再导出")
            return
        append_mode = (self.append_check.isChecked()
                       and bool(self.append_edit.text().strip()))
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "追加到所选谱面" if append_mode else "导出谱面",
                str(self._default_export_path()), "谱面 (*.json)"
            )
        if not path:
            return
        target = Path(path)
        if target.exists() and not self._confirm_overwrite(target, append_mode):
            return
        workshop_db = None
        if append_mode:
            workshop_db = chartbuild.read_workshop_db(self.append_edit.text().strip())

        self._generation += 1
        self._cancel.clear()
        self.cancel_button.setEnabled(True)
        job = _ConvertJob(
            self._generation, self._image, self._collect_options(),
            self._result.chart, self._relay,
            export_path=str(target), workshop_db=workshop_db,
            export_preview=self.export_preview_check.isChecked(),
            cancel=self._cancel,
        )
        self._launch(job, "导出")

    def _calibrate(self, path: str | None = None) -> None:
        """生成标定谱面；``path`` 为空时弹文件对话框。"""
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "生成标定谱面", str(Path.cwd() / "calibrate.json"), "谱面 (*.json)"
            )
        if not path:
            return
        try:
            bpm = self.bpm_spin.value()
            speed_const = speed_const_from_flow(self.flow_spin.value())
            view_height = DEFAULT_VIEW_HEIGHT_WORLD / max(1e-6, self.zoom_spin.value())
            layout = pipeline.calibration_polylines(
                bpm=bpm, speed_const=speed_const, view_height=view_height,
                start_beat=max(0.0, self.beat_spin.value()),
            )
            chart = chartbuild.make_chart(bpm)
            chartbuild.add_polylines(chart, layout.polylines)
            chartbuild.export_chart(chart, path)
            png = Path(path).with_name(Path(path).stem + "_preview.png")
            preview.render_chart(
                chart, png, speed_const=speed_const,
                line_width_world=self.line_width_spin.value(),
                screen_world=view_height, max_width=700,
            )
        except (chartbuild.ChartError, ValueError) as exc:
            self._show_error(str(exc))
            return
        self._set_status(f"标定谱面已生成：{Path(path).name}"
                         f"（一屏约 {layout.screen_ticks:.2f} 拍）")
        if not self._silent:
            QtWidgets.QMessageBox.information(
                self, "标定谱面已生成",
                f"{path}\n{png}\n\n"
                f"按当前参数：一屏 ≈ {layout.screen_ticks:.2f} 拍、"
                f"V = {speed_const:g}、一屏高 {view_height:.2f} 世界单位。\n"
                "在编辑器里看：绿框应为正方形、绿色方括号应顶到屏幕上下边缘、"
                "线宽梯里糊成一条的那档就是线宽。",
            )

    # ------------------------------------------------------------------ 状态
    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _show_error(self, message: str) -> None:
        self._status.setText("⚠ " + message.replace("\n", " "))
        self.statusBar().setStyleSheet("color: #d9534f;")
        QtCore.QTimer.singleShot(
            4000, lambda: self.statusBar().setStyleSheet("")
        )


def run(argv: list[str] | None = None) -> int:
    """启动 GUI。"""
    app = QtWidgets.QApplication(argv or [])
    app.setApplicationName("图片 → Rizline 谱面")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec_()
