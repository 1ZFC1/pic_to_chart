"""图片 → 谱面 的“一次性”流程，供 GUI 和 CLI 共用。

CLI 还负责打印摘要、追加到已有谱面等，但**换算与折线化**都在这里，
避免两条代码路径的默认值走偏。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

from PIL import Image

from . import chartbuild, imageproc, modes
from .mapping import (
    COLUMN_STEP_X,
    DEFAULT_LINE_WIDTH_WORLD,
    DEFAULT_SPEED_CONST,
    DEFAULT_VIEW_HEIGHT_WORLD,
    DEFAULT_WIDTH_X,
    FADE_END_X,
    FADE_START_X,
    PictureMap,
    fit_width_x,
    speed_const_from_flow,
    ticks_per_x,
    view_height_from_ticks,
    visible_height_ticks,
    world_per_tick,
)


class ConvertCancelled(RuntimeError):
    """转换被用户取消。"""


@dataclass
class ConvertOptions:
    """一次转换的全部参数（GUI 的控件 ↔ 这里的字段一一对应）。"""

    # 模式与数值都在这里决定（GUI/CLI 共用）
    mode: str = "scan"                      # scan / column / outline / thin
    bpm: float = 175.0
    start_beat: float = 8.0
    canvas: int = 0                         # 目标画布索引（负数从末尾数）

    # 位置与比例
    width_x: float = 0.0                    # 0 = 自动
    max_width_x: float = DEFAULT_WIDTH_X
    screens: float = 0.0                    # >0：竖着正好占 N 屏高
    visible_ticks: float = 0.0              # >0：一屏拍数（实测）
    view_height: float = DEFAULT_VIEW_HEIGHT_WORLD
    zoom: float = 1.0
    flow: float = 0.0                       # >0：用编辑器流速换算 V
    speed_const: float = DEFAULT_SPEED_CONST
    aspect: float = 0.0                     # >0：直接给 tick / x 单位
    center_x: float = 0.0

    # scan
    cols: int = 0
    rows: int = 0
    color_tolerance: float = 8.0
    alpha_cut: int = 8
    whole_row: bool = False

    # outline / thin
    ink: float | None = None
    invert: bool = False
    tolerance: float = 1.0
    simplify: float = 0.8
    layers: int = 1
    layer_gap: int = 2
    min_length: float = 4.0
    color: list[int] | None = None
    smooth: float = 0.0             # outline 平滑强度（像素，σ）；0 = 关

    # 预览/线宽
    line_width: float = DEFAULT_LINE_WIDTH_WORLD


@dataclass
class ConvertResult:
    """转换结果：谱面 + 折线 + 中间量（GUI 用来显示状态）。"""

    chart: dict
    polylines: list
    cmap: PictureMap
    speed_const: float
    view_height: float
    width_x: float
    cols: int
    rows: int
    line_count: int
    node_count: int
    canvas_index: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def screen_ticks(self) -> float:
        return visible_height_ticks(self.chart_bpm, self.speed_const, self.view_height)

    @property
    def chart_bpm(self) -> float:
        return float(self.chart.get("bPM") or 120.0)


def scan_grid(opts: ConvertOptions, image: Image.Image, cmap: PictureMap,
              bpm: float, speed_const: float) -> tuple[int, int]:
    """决定 scan 模式的采样网格：行距 = 线宽，列数按图片宽高比。"""
    rows = opts.rows
    if rows <= 0:
        height_world = cmap.height_ticks * world_per_tick(bpm, speed_const)
        rows = int(round(height_world / max(1e-6, opts.line_width)))
        rows = max(16, min(400, rows))
    cols = opts.cols
    if cols <= 0:
        cols = max(16, int(round(rows * image.width / image.height)))
        cols = min(600, cols)
    return cols, rows


def column_grid(opts: ConvertOptions, image: Image.Image, cmap: PictureMap,
                bpm: float, speed_const: float) -> tuple[int, int]:
    """逐列扫描的网格：列距按编辑器水平细分 1/16（=COLUMN_STEP_X），行数按宽高比。"""
    cols = opts.cols
    if cols <= 0:
        cols = int(round(cmap.width_x / COLUMN_STEP_X))
        cols = max(16, min(600, cols))
    rows = opts.rows
    if rows <= 0:
        rows = max(16, int(round(cols * image.height / image.width)))
        rows = min(400, rows)
    return cols, rows


def resolve_view(opts: ConvertOptions, bpm: float, speed_const: float) -> float:
    """算出“一屏世界高度”：实测拍数 > zoom 缩放 > 默认值。"""
    view_height = opts.view_height / max(1e-6, opts.zoom)
    if opts.visible_ticks > 0:
        view_height = view_height_from_ticks(opts.visible_ticks, bpm, speed_const)
    return view_height


def resolve_speed_const(opts: ConvertOptions) -> float:
    return speed_const_from_flow(opts.flow) if opts.flow > 0 else opts.speed_const


def build_polylines(image: Image.Image, cmap: PictureMap, opts: ConvertOptions,
                    *, bpm: float, speed_const: float) -> tuple[list, int, int]:
    """按模式把图片折线化，返回 (折线, 列数, 行数)。"""
    if opts.mode == "scan":
        cols, rows = scan_grid(opts, image, cmap, bpm, speed_const)
        polylines, _ = modes.scan_polylines(
            image, cmap,
            cols=cols, rows=rows,
            alpha_cut=opts.alpha_cut,
            whole_row=opts.whole_row,
            color_tolerance=opts.color_tolerance,
        )
        return polylines, cols, rows
    if opts.mode == "column":
        cols, rows = column_grid(opts, image, cmap, bpm, speed_const)
        polylines, _ = modes.column_polylines(
            image, cmap,
            cols=cols, rows=rows,
            alpha_cut=opts.alpha_cut,
            color_tolerance=opts.color_tolerance,
        )
        return polylines, cols, rows
    if opts.mode == "thin":
        polylines, _ = modes.thin_polylines(
            image, cmap,
            alpha_cut=opts.alpha_cut,
            ink_threshold=opts.ink,
            invert=opts.invert,
            simplify_px=opts.simplify,
            min_length_px=opts.min_length,
            color=opts.color,
        )
        return polylines, 0, 0
    polylines, _ = modes.outline_polylines(
        image, cmap,
        alpha_cut=opts.alpha_cut,
        ink_threshold=opts.ink,
        invert=opts.invert,
        tolerance_px=opts.tolerance,
        layers=opts.layers,
        layer_gap_px=opts.layer_gap,
        min_length_px=opts.min_length,
        color=opts.color,
        smooth_px=opts.smooth,
    )
    return polylines, 0, 0


def convert(image: Image.Image, opts: ConvertOptions,
            chart: dict | None = None, *,
            on_progress: "Callable[[str, int, int], None] | None" = None,
            cancel=None) -> ConvertResult:
    """完整转换：图片 → 折线 → 谱面 dict（不落盘）。

    ``chart`` 给定时追加到这份已有谱面（沿用它的 BPM）。

    ``on_progress(阶段, 已完成, 总数)`` 用于进度显示；``cancel`` 是
    ``threading.Event``，置位后抛 :class:`ConvertCancelled`。
    """

    def report(stage: str, done: int = 0, total: int = 0) -> None:
        if on_progress is not None:
            on_progress(stage, done, total)

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise ConvertCancelled("转换已取消")

    report("解析图片")
    if chart is None:
        bpm = float(opts.bpm)
    else:
        bpm = float(chart.get("bPM") or 120.0)

    speed_const = resolve_speed_const(opts)
    aspect = opts.aspect if opts.aspect > 0 else ticks_per_x(bpm, speed_const)
    view_height = resolve_view(opts, bpm, speed_const)

    width_x = opts.width_x
    if width_x <= 0:
        if opts.screens > 0:
            width_x = fit_width_x(
                image.width, image.height,
                bpm=bpm, ticks_per_x=aspect,
                max_width_x=opts.max_width_x / max(1e-6, opts.zoom),
                speed_const=speed_const,
                view_height_world_value=view_height,
                screens=opts.screens,
            )
        else:
            width_x = opts.max_width_x / max(1e-6, opts.zoom)

    cmap = PictureMap(
        width_px=image.width, height_px=image.height, width_x=width_x,
        center_x=opts.center_x, start_tick=opts.start_beat, ticks_per_x=aspect,
    )
    if cmap.start_tick < 0:
        raise chartbuild.ChartError("起始拍必须 ≥ 0")

    check_cancel()
    report("提取折线")
    # scan 的自动网格要用“已解析”的 bpm / speed_const
    polylines, cols, rows = build_polylines(
        image, cmap, opts, bpm=bpm, speed_const=speed_const
    )
    if not polylines:
        raise chartbuild.ChartError("没有生成任何引导线：检查阈值、透明度或图片内容")

    check_cancel()
    target = chart if chart is not None else chartbuild.make_chart(bpm)
    before = chartbuild.load_library().getCanvasCount(target) or 0
    chartbuild.ensure_canvas(target)
    canvas_index = chartbuild.ensure_canvas_index(target, opts.canvas, extend=True)
    after = chartbuild.load_library().getCanvasCount(target) or 0
    report("写入谱面", 0, len(polylines))
    line_count, node_count = chartbuild.add_polylines(
        target, polylines, canvas_index=canvas_index,
        on_progress=lambda done, total: report("写入谱面", done, total),
        cancel=cancel,
    )

    notes = []
    if after > before:
        notes.append(f"画布 {canvas_index} 不存在，已自动新增 {after - before} 个画布")
    edge = max(abs(cmap.left_x), abs(cmap.right_x))
    if edge > DEFAULT_WIDTH_X / max(1e-6, opts.zoom) + 1e-9:
        notes.append("宽度超过一屏，屏幕外的线会被淡出")
    elif edge > FADE_START_X:
        notes.append("左右两端进入淡出区")
    if opts.mode == "scan" and rows:
        row_world = cmap.height_ticks * world_per_tick(bpm, speed_const) / rows
        if row_world > opts.line_width * 1.2:
            notes.append(f"行距 {row_world:.3f} 大于线宽 {opts.line_width:g}，可能出现横条纹")
    if opts.mode == "column" and cols:
        col_world = cmap.width_x * 5.625 / cols
        if col_world > opts.line_width * 1.2:
            notes.append(f"列距 {col_world:.3f} 大于线宽 {opts.line_width:g}，可能出现竖条纹")

    return ConvertResult(
        chart=target, polylines=polylines, cmap=cmap,
        speed_const=speed_const, view_height=view_height,
        width_x=width_x, cols=cols, rows=rows,
        line_count=line_count, node_count=node_count, notes=notes,
        canvas_index=canvas_index,
    )


def summary_lines(result: ConvertResult, opts: ConvertOptions) -> list[str]:
    """给 GUI 状态栏/CLI 用的一行行摘要。"""
    cmap = result.cmap
    bpm = result.chart_bpm
    seconds = cmap.height_ticks * 60.0 / bpm
    screens = cmap.height_ticks / result.screen_ticks if result.screen_ticks else 0.0
    canvases = len(result.chart.get("canvasMoves") or [])
    lines = [
        f"BPM {bpm:g}   V {result.speed_const:.2f}   1 个 x 单位 = {cmap.ticks_per_x:.3f} tick",
        f"横向 {cmap.left_x:+.3f} … {cmap.right_x:+.3f}（宽 {cmap.width_x:.3f}）",
        f"纵向 {cmap.start_tick:.2f} … {cmap.end_tick:.2f}（{cmap.height_ticks:.2f} 拍 ≈ {seconds:.2f} 秒）",
        f"一屏约 {result.screen_ticks:.2f} 拍 → 整张图 {screens:.1f} 屏高",
        f"写入画布 {result.canvas_index}（共 {canvases} 个）："
        f"{result.line_count} 条线 / {result.node_count} 个节点"
        + (f"（{result.cols} 列 × {result.rows} 行）" if result.cols else ""),
    ]
    return lines + result.notes


def chart_stats_text(chart: dict) -> str:
    stats = chartbuild.chart_stats(chart)
    return (f"谱面总计：{stats['lines']} 条线、{stats['nodes']} 个节点、"
            f"{stats['canvases']} 个画布")


# --------------------------------------------------------------------- 标定谱面
@dataclass
class CalibrationLayout:
    """标定谱面的折线 + 几个供输出/显示用的中间量。"""

    polylines: list
    screen_ticks: float
    vruler_beats: int
    ruler_end: float
    ladder_top_tick: float
    brace: tuple[float, float]


def calibration_polylines(*, bpm: float, speed_const: float, view_height: float,
                          start_beat: float = 8.0) -> CalibrationLayout:
    """生成“量比例 / 量一屏拍数 / 量线宽”的标定谱面折线。

    从下往上依次是：

    1. 红框：数值上 1 x 单位宽、1 tick 高（参照物）；
    2. 绿框：1 x 单位宽、``aspect`` tick 高（按换算应为正方形）；
    3. 蓝圆：半径 0.5 x 单位（应为正圆）；
    4. 白色横向刻度：x = ±0.7，0.5 长刻度 + 0.1 短刻度（可见区 ±0.5）；
    5. 纵向刻度：每 0.5 拍一根、整拍白线，共 20 拍（数一屏里的白线数）；
    6. 橙色线宽梯：0.02…0.20 世界单位的成对水平线（糊成一条的那档 = 线宽）；
    7. 浅绿色方括号：高度 = 工具预测的一屏高度（应正好顶到上下边缘）。
    """
    if bpm <= 0:
        raise ValueError("bpm 必须大于 0")
    if start_beat < 0:
        raise chartbuild.ChartError("起始拍必须 ≥ 0")

    aspect = ticks_per_x(bpm, speed_const)
    view_height_value = view_height
    wt = world_per_tick(bpm, speed_const)
    center = 0.0
    half = 0.5
    polylines: list[list[tuple[float, float, list[int]]]] = []

    def add(points, color):
        if len(points) >= 2:
            polylines.append([(tick, x, color) for tick, x in points])

    # 1) 1 x 单位 × 1 tick
    square_1 = [
        (start_beat, center - half), (start_beat + 1.0, center - half),
        (start_beat + 1.0, center + half), (start_beat, center + half),
    ]
    for index in range(4):
        add([square_1[index], square_1[(index + 1) % 4]], [255, 80, 80, 255])

    # 2) 1 x 单位见方
    y0 = start_beat + 2.0
    square_2 = [
        (y0, center - half), (y0 + aspect, center - half),
        (y0 + aspect, center + half), (y0, center + half),
    ]
    for index in range(4):
        add([square_2[index], square_2[(index + 1) % 4]], [80, 255, 80, 255])

    # 3) 半径 0.5 x 单位的圆（四象限，保证每段 tick 单调）
    circle_center = y0 + aspect + 1.0
    radius_tick = half * aspect
    steps = 45
    for quarter in range(4):
        points = []
        for step in range(steps + 1):
            angle = math.pi / 2 * (quarter + step / steps)
            points.append((circle_center + radius_tick * math.sin(angle),
                           center + half * math.cos(angle)))
        add(points, [80, 160, 255, 255])

    # 4) 横向刻度
    ruler_end = 0.7
    ruler_tick = circle_center + radius_tick + 1.0
    add([(ruler_tick, -ruler_end), (ruler_tick, ruler_end)], [200, 200, 200, 255])
    x = -ruler_end
    while x <= ruler_end + 1e-9:
        add([(ruler_tick - 0.45, round(x, 4)), (ruler_tick + 0.45, round(x, 4))],
            [200, 200, 200, 255])
        x += 0.5
    x = -ruler_end
    while x <= ruler_end + 1e-9:
        if abs(round(x * 10) % 5) > 1e-6:
            add([(ruler_tick - 0.22, round(x, 4)), (ruler_tick + 0.22, round(x, 4))],
                [150, 150, 150, 255])
        x += 0.1

    # 5) 纵向刻度（整拍白线，半拍彩线）
    vruler_t0 = ruler_tick + 1.2
    vruler_beats = 20
    half_palette = [
        [255, 120, 120, 255], [255, 190, 90, 255], [200, 255, 110, 255],
        [110, 255, 190, 255], [110, 200, 255, 255], [170, 140, 255, 255],
        [255, 120, 220, 255], [190, 190, 190, 255],
    ]
    for step in range(vruler_beats * 2 + 1):
        tick = vruler_t0 + step * 0.5
        color = ([255, 255, 255, 255] if step % 2 == 0
                 else half_palette[(step // 2) % len(half_palette)])
        add([(tick, -FADE_START_X), (tick, FADE_START_X)], color)

    # 6) 线宽梯
    ladder_tick = vruler_t0 + vruler_beats + 0.4
    gaps = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.16, 0.20]
    slot = 2 * FADE_END_X / len(gaps)
    for index, gap in enumerate(gaps):
        left = -FADE_END_X + index * slot
        right = left + slot * 0.7
        for tick in (ladder_tick, ladder_tick + gap / wt):
            add([(tick, left), (tick, right)], [255, 160, 60, 255])

    # 7) 一屏高度方括号
    screen_ticks = visible_height_ticks(bpm, speed_const, view_height_value)
    brace_lo = ladder_tick + 0.8 + 0.2 / wt
    brace_hi = brace_lo + screen_ticks
    brace_x = FADE_START_X * 0.9
    for tick in (brace_lo, brace_hi):
        add([(tick, -brace_x), (tick, brace_x)], [120, 255, 120, 255])
    add([(brace_lo, -brace_x), (brace_hi, -brace_x)], [120, 255, 120, 255])
    add([(brace_lo, brace_x), (brace_hi, brace_x)], [120, 255, 120, 255])

    return CalibrationLayout(
        polylines=polylines, screen_ticks=screen_ticks,
        vruler_beats=vruler_beats, ruler_end=ruler_end,
        ladder_top_tick=ladder_tick + 0.2 / wt, brace=(brace_lo, brace_hi),
    )
