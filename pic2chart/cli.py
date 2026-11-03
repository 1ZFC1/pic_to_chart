"""命令行入口：图片 → Rizline 谱面。"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from . import chartbuild, imageproc, modes, pipeline, preview
from .mapping import (
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pic_to_chart",
        description="把图片转成 Rizline 谱面（用引导线“画”出图片）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("image", nargs="?", help="输入图片（PNG/JPG/BMP…）")
    parser.add_argument("-o", "--output", default="picture_chart.json", help="输出谱面 JSON")

    layout = parser.add_argument_group("位置与比例")
    layout.add_argument("--bpm", type=float, default=175.0, help="新建谱面时的 BPM")
    layout.add_argument("--chart", help="追加到已有谱面，而不是新建（沿用其 BPM）")
    layout.add_argument("--canvas", type=int, default=None,
                        help="目标画布索引（负数从末尾数）；"
                             "追加到 --chart 时默认接在现有画布之后")
    layout.add_argument("--start-beat", type=float, default=8.0,
                        help="图片底部（最小 tick）所在的拍")
    layout.add_argument("--width-x", type=float, default=0.0,
                        help="图片横向占用的 xPosition 宽度；0 表示按 --max-width-x 自动")
    layout.add_argument("--max-width-x", type=float, default=DEFAULT_WIDTH_X,
                        help="自动宽度；默认 1.0，正好是一屏（10 格）宽")
    layout.add_argument("--fit-screen", action="store_true",
                        help="自动宽度让整张图一屏能看全（等价于 --screens 1）")
    layout.add_argument("--screens", type=float, default=0.0,
                        help="让图片竖着正好占 N 屏高（0 = 关闭，用 --max-width-x）")
    layout.add_argument("--visible-ticks", type=float, default=0.0,
                        help="一屏能看到多少拍（标定得到）；>0 时优先用它算一屏高度")
    layout.add_argument("--view-height", type=float, default=DEFAULT_VIEW_HEIGHT_WORLD,
                        help="一屏对应的世界高度（默认按 1.0 x 单位宽 + 16:9 推算）")
    layout.add_argument("--zoom", type=float, default=1.0,
                        help="编辑器“整体缩放”倍数（100%% = 1.0）；窗口可见范围按 1/zoom 变化")
    layout.add_argument("--center-x", type=float, default=0.0, help="图片横向中心")
    layout.add_argument("--aspect", type=float, default=0.0,
                        help="1 个 xPosition 单位等于多少 tick；0 表示按 BPM 自动推算")
    layout.add_argument("--flow", type=float, default=0.0,
                        help="编辑器里的播放流速（如 1.5 / 5.5）；给定时按实测曲线换算 --speed-const")
    layout.add_argument("--speed-const", type=float, default=DEFAULT_SPEED_CONST,
                        help="纵向常数（世界单位/秒），直接控制图形比例")

    scan = parser.add_argument_group("scan 模式（逐行扫描，适合照片）")
    scan.add_argument("--cols", type=int, default=0, help="横向采样列数；0 = 按宽高比自动")
    scan.add_argument("--rows", type=int, default=0,
                      help="纵向采样行数；0 = 让行距等于 --line-width（自动）")
    scan.add_argument("--alpha-cut", type=int, default=8, help="alpha 低于该值视为透明")
    scan.add_argument("--whole-row", action="store_true",
                      help="每行只出一条整行线，空隙用透明节点（线数少、节点多）")
    scan.add_argument("--color-tolerance", type=float, default=8.0,
                      help="颜色误差容差，越大节点越少；0 保留全部采样点")

    outline = parser.add_argument_group("outline / thin 模式（线稿、logo、剪影）")
    outline.add_argument("--ink", type=float, default=None,
                         help="亮度阈值：比它暗的像素算墨迹（不给则按 alpha 判断）")
    outline.add_argument("--invert", action="store_true", help="反相：亮的算墨迹（白字黑底）")
    outline.add_argument("--tolerance", type=float, default=1.0,
                         help="轮廓简化容差（像素），越大节点越少")
    outline.add_argument("--smooth", type=float, default=0.0,
                         help="轮廓平滑强度（像素 σ），0=关；1.0~1.5 比较稳")
    outline.add_argument("--simplify", type=float, default=0.8,
                         help="thin 模式的骨架简化容差（像素）")
    outline.add_argument("--layers", type=int, default=1, help="描边层数，>1 时加粗")
    outline.add_argument("--layer-gap", type=int, default=2, help="每层向外扩张的像素数")
    outline.add_argument("--min-length", type=float, default=4.0,
                         help="短于该长度（像素）的轮廓/骨架丢弃")
    outline.add_argument("--color", help="固定描边颜色，如 #FF3355；默认从图片取色")

    out = parser.add_argument_group("输出与预览")
    out.add_argument("--preview", help="输出渲染预览 PNG")
    out.add_argument("--compare", help="输出“原图 | 谱面渲染”对比图")
    out.add_argument("--bg", help="预览底色，如 #FFFFFF；默认按线条颜色自动选择")
    out.add_argument("--show-screens", action="store_true",
                     help="预览里每隔“一屏”画一条灰线，看出要滚几屏")
    out.add_argument("--line-width", type=float, default=DEFAULT_LINE_WIDTH_WORLD,
                     help="引导线视觉线宽（世界单位，实测 0.04）；预览粗细 + scan 自动行距都用它")
    out.add_argument("--preview-width", type=int, default=1200, help="预览图最大宽度（像素）")
    out.add_argument("--max-size", type=int, default=1600, help="读取时限制图片最长边")
    out.add_argument("--calibrate", metavar="JSON",
                     help="生成标定谱面（正方形 + 圆），用来在编辑器里核对比例")
    mode_group = parser.add_argument_group("模式")
    mode_group.add_argument("--mode", choices=("scan", "column", "outline", "thin"),
                            default="scan",
                            help="转换方式：逐行扫描 / 逐列扫描 / 轮廓描边 / 骨架中心线")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.calibrate:
            return _run_calibration(args)
        if not args.image:
            parser.error("需要输入图片（或用 --calibrate 生成标定谱面）")
        return _run_convert(args)
    except (imageproc.ImageError, chartbuild.ChartError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------- 转换流程
def _run_convert(args: argparse.Namespace) -> int:
    """图片 → 谱面（换算、折线化、画布处理都在 pipeline 里，和 GUI 共用）。"""
    image = imageproc.load_image(args.image, max_size=args.max_size)

    base_chart = None
    workshop_db = None
    if args.chart:
        workshop_db = chartbuild.read_workshop_db(args.chart)
        base_chart = chartbuild.ensure_canvas(chartbuild.load_chart(args.chart))

    try:
        color = imageproc.hex_to_rgba(args.color) if args.color else None
    except imageproc.ImageError as exc:
        raise chartbuild.ChartError(str(exc)) from exc

    screens = args.screens
    if screens <= 0 and args.fit_screen:
        screens = 1.0

    # 追加到已有谱面且没显式给 --canvas 时，接在现有画布之后（不覆盖原内容）
    canvas_index = args.canvas
    if canvas_index is None:
        canvas_index = ((chartbuild.load_library().getCanvasCount(base_chart) or 0)
                        if base_chart is not None else 0)

    opts = pipeline.ConvertOptions(
        mode=args.mode,
        bpm=args.bpm,
        start_beat=args.start_beat,
        canvas=canvas_index,
        width_x=args.width_x,
        max_width_x=args.max_width_x,
        screens=screens,
        visible_ticks=args.visible_ticks,
        view_height=args.view_height,
        zoom=args.zoom,
        flow=args.flow,
        speed_const=args.speed_const,
        aspect=args.aspect,
        center_x=args.center_x,
        cols=args.cols,
        rows=args.rows,
        color_tolerance=args.color_tolerance,
        alpha_cut=args.alpha_cut,
        whole_row=args.whole_row,
        ink=args.ink,
        invert=args.invert,
        tolerance=args.tolerance,
        simplify=args.simplify,
        layers=args.layers,
        layer_gap=args.layer_gap,
        min_length=args.min_length,
        color=color,
        smooth=args.smooth,
        line_width=args.line_width,
    )
    result = pipeline.convert(image, opts, base_chart)
    chartbuild.export_chart(result.chart, args.output, extra_db=workshop_db)

    if args.preview or args.compare:
        target = args.preview or _sibling(args.compare, "_chart.png")
        background = None
        if args.bg:
            rgb = imageproc.hex_to_rgba(args.bg)
            background = (rgb[0], rgb[1], rgb[2])
        preview.render_chart(
            result.chart, target,
            speed_const=result.speed_const,
            line_width_world=args.line_width,
            max_width=args.preview_width,
            background=background,
            screen_world=result.view_height if args.show_screens else None,
        )
        if args.compare:
            preview.side_by_side(image, target, args.compare,
                                 background=background or (255, 255, 255))

    if not args.quiet:
        print(f"图片：{image.width}×{image.height}px → {args.output}")
        for line in pipeline.summary_lines(result, opts):
            print(line)
        print(pipeline.chart_stats_text(result.chart))
        if args.preview:
            print(f"预览：{args.preview}")
        if args.compare:
            print(f"对比图：{args.compare}")
    return 0


# --------------------------------------------------------------------- 标定谱面
def _run_calibration(args: argparse.Namespace) -> int:
    """生成“量比例 + 量一屏拍数 + 量线宽”用的标定谱面。

    几何全部在 :func:`pic2chart.pipeline.calibration_polylines` 里，
    这里只负责建谱面、导出、预览和打印说明。
    """
    bpm = float(args.bpm)
    speed_const = speed_const_from_flow(args.flow) if args.flow > 0 else args.speed_const
    view_height = args.view_height / max(1e-6, args.zoom)
    start = float(args.start_beat)

    layout = pipeline.calibration_polylines(
        bpm=bpm, speed_const=speed_const, view_height=view_height, start_beat=start
    )
    chart = chartbuild.make_chart(bpm)
    line_count, node_count = chartbuild.add_polylines(
        chart, layout.polylines, canvas_index=args.canvas or 0
    )
    chartbuild.export_chart(chart, args.calibrate)
    if args.preview:
        preview.render_chart(
            chart, args.preview,
            speed_const=speed_const,
            line_width_world=args.line_width,
            max_width=args.preview_width,
            screen_world=view_height,
        )
    if not args.quiet:
        aspect = ticks_per_x(bpm, speed_const)
        wt = world_per_tick(bpm, speed_const)
        print(f"标定谱面：{args.calibrate}")
        print(f"BPM：{bpm:g}    1 个 x 单位 = {aspect:.4f} tick"
              f"（1 tick = {wt:.4f} 世界单位，纵向常数 V={speed_const:.3f}）")
        print("图形：红框 = 1x1（数值），绿框 = 1 x 单位见方（按换算），"
              "蓝圆 = 半径 0.5 x 单位")
        print(f"刻度：横向 -{layout.ruler_end:g} … {layout.ruler_end:g}"
              f"（0.5 长刻度 + 0.1 短刻度，可见区是 ±{FADE_END_X:g}）；"
              f"纵向每 0.5 拍一根、整拍白线，共 {layout.vruler_beats} 拍；"
              f"橙色线宽梯 0.02 … 0.20 世界单位")
        print(f"写入：{line_count} 条线 / {node_count} 个节点；"
              f"拍 {start:g} … {layout.ladder_top_tick:g}")
        print(f"当前假设：一屏 ≈ {layout.screen_ticks:.2f} 拍"
              f"（V={speed_const:.2f}、一屏世界高度 {view_height:.2f}、"
              f"一屏宽 {DEFAULT_WIDTH_X:g} 个 x 单位）")
        print("三件事一次量完：")
        print("  0) 浅绿色【方括号】= 工具预测的一屏高度：它在编辑器里应该正好顶到屏幕上下"
              "边缘；只占屏幕一半就说明实际一屏是这个数的两倍，把 --visible-ticks "
              "或 --view-height 按比例改掉（比例参数不用动）。")
        print("  1) 绿框是正方形、蓝圆是正圆 → 比例对；否则调 --aspect / --speed-const / --flow；")
        print("  2) 数一屏里能看见几条【白线】→ 一屏多少拍，填给 --visible-ticks；")
        print("  3) 橙色梯里“刚好糊成一条”的那对间距 = 编辑器真实线宽，填给 --line-width。")
        print("换窗口大小/整体缩放后，可见区会按 1/zoom 变化：用 --zoom 或直接 --visible-ticks。")
    return 0
