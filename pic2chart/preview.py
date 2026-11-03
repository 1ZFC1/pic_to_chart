"""把谱面渲染成 PNG，用来在打开编辑器之前先看效果。

渲染用的是“世界坐标”，而不是简单的 (x, tick) 平面：

    X = (xPosition + 画布 x) × 5.625
    Y = floorPosition × speed_const

``floorPosition`` 本身已经包含画布流速的积分，所以画布变速的谱面也能画对；
``speed_const`` 必须和 ``mapping.ticks_per_x()`` 使用同一个值，否则预览和
实际编辑器里的比例会对不上。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .mapping import DEFAULT_LINE_WIDTH_WORLD, DEFAULT_SPEED_CONST, WORLD_X_PER_X_POSITION


def canvas_x_at(chart: dict, canvas_index: int, tick: float) -> float:
    """按关键点线性插值取画布 x（忽略 easeType，预览足够）。"""
    for move in chart.get("canvasMoves") or []:
        if move.get("index") != canvas_index:
            continue
        points = sorted(move.get("xPositionKeyPoints") or [], key=lambda p: p["time"])
        if not points:
            return 0.0
        if tick <= points[0]["time"]:
            return float(points[0]["value"])
        for left, right in zip(points, points[1:]):
            if tick <= right["time"]:
                span = right["time"] - left["time"]
                if span <= 0:
                    return float(right["value"])
                ratio = (tick - left["time"]) / span
                return float(left["value"] + (right["value"] - left["value"]) * ratio)
        return float(points[-1]["value"])
    return 0.0


def _node_floor(chart: dict, node: dict) -> float:
    """取节点的高度（秒）；缺失或 NaN 时按 tick 估算。"""
    value = node.get("floorPosition")
    if isinstance(value, (int, float)) and value == value:  # 排除 NaN
        return float(value)
    bpm = float(chart.get("bPM") or 120.0)
    return float(node.get("time", 0.0)) * 60.0 / bpm


def chart_segments(chart: dict, speed_const: float = DEFAULT_SPEED_CONST,
                   canvas_index: int | None = None):
    """谱面 → 世界坐标线段列表 ``(x0, y0, x1, y1, rgba)``。

    ``canvas_index`` 给定时只取这个画布上的线（预览一次只看一张画布）。
    """
    segments = []
    for line in chart.get("lines") or []:
        nodes = sorted(line.get("linePoints") or [], key=lambda p: p["time"])
        if not nodes:
            continue
        if canvas_index is not None:
            canvases = {int(node.get("canvasIndex", 0)) for node in nodes}
            if canvases != {int(canvas_index)}:
                continue
        world = []
        for node in nodes:
            tick = float(node["time"])
            canvas = int(node.get("canvasIndex", 0))
            x = (float(node["xPosition"]) + canvas_x_at(chart, canvas, tick)) * WORLD_X_PER_X_POSITION
            y = _node_floor(chart, node) * speed_const
            color = node.get("color") or {}
            if isinstance(color, dict):
                rgba = (color.get("r", 255), color.get("g", 255), color.get("b", 255),
                        color.get("a", 255))
            else:
                values = list(color) + [255] * (4 - len(color))
                rgba = tuple(int(v) for v in values[:4])
            world.append((x, y, rgba))
        for left, right in zip(world, world[1:]):
            if left[2][3] <= 8 and right[2][3] <= 8:
                continue
            segments.append((left[0], left[1], right[0], right[1], left[2]))
    return segments


def _auto_background(segments) -> tuple[int, int, int]:
    """按线条平均亮度自动选背景：深色线用白底，亮色线用黑底。"""
    total = 0.0
    weight = 0.0
    for *_, rgba in segments:
        if rgba[3] <= 8:
            continue
        lum = 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]
        total += lum
        weight += 1
    if weight == 0:
        return (0, 0, 0)
    return (255, 255, 255) if total / weight < 110 else (8, 8, 12)


def render_chart_image(
    chart: dict,
    *,
    speed_const: float = DEFAULT_SPEED_CONST,
    line_width_world: float = DEFAULT_LINE_WIDTH_WORLD,
    max_width: int = 1200,
    max_height: int = 4000,
    background: tuple[int, int, int] | None = None,
    judge_tick: float | None = None,
    screen_world: float | None = None,
    canvas_index: int | None = None,
    tick_range: tuple[float, float] | None = None,
    view_width_world: float | None = None,
) -> Image.Image:
    """把整张“画面”渲染成 PIL 图片（GUI 用内存图像，CLI 再落盘）。

    ``background=None`` 时按线条颜色自动选底色（深色线 → 白底）。
    ``screen_world`` 给定时，每隔这么高画一条灰线，标出“一屏”的边界。

    两个“只看一块”的参数（GUI 预览用）：

    * ``canvas_index``：只画这个画布上的线（多画布/追加的谱面里不会混进别的内容）；
    * ``tick_range``：纵向只取这段拍（按世界高度切），画出来正好是这张图本身；
    * ``view_width_world``：横向至少留这么宽（世界单位），以 x=0 居中——
      所以“横向中心”偏移在预览里看得见；内容更宽时自动撑开，不会裁掉。
    """
    segments = chart_segments(chart, speed_const=speed_const,
                              canvas_index=canvas_index)
    if not segments and canvas_index is not None:
        # 画布号对不上（例如追加时改了编号）时退回“全部”，别让预览空着
        segments = chart_segments(chart, speed_const=speed_const)
    if not segments:
        raise ValueError("谱面里没有可渲染的线段")
    if background is None:
        background = _auto_background(segments)

    xs = [value for seg in segments for value in (seg[0], seg[2])]
    ys = [value for seg in segments for value in (seg[1], seg[3])]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    if tick_range is not None:
        # 纵向按拍数取：这张图从 start_tick 画到 start_tick + height_ticks
        start_tick, end_tick = sorted(float(v) for v in tick_range)
        if end_tick - start_tick > 1e-9:
            min_y = _tick_to_world(chart, start_tick, speed_const)
            max_y = _tick_to_world(chart, end_tick, speed_const)

    if view_width_world is not None:
        # 横向：至少留一屏宽、以 x=0 为中心（这样“横向中心”的偏移看得见）；
        # 内容更宽/更偏时把窗口撑开，保证不裁掉东西
        half = max(float(view_width_world), (max_x - min_x) * 1.05) / 2.0
        margin = max(0.05, (max_x - min_x) * 0.02)
        min_x = min(-half, min_x - margin)
        max_x = max(half, max_x + margin)

    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)

    scale = max_width / span_x
    if span_y * scale > max_height:
        scale = max_height / span_y
    width = max(8, int(round(span_x * scale)) + 2)
    height = max(8, int(round(span_y * scale)) + 2)

    # 超采样再缩回来：PIL 的粗线在拐点处会有硬接缝，缩一次能抹平，
    # 效果更接近编辑器里带抗锯齿的线。
    ss = 2 if width * height * 4 <= 16_000_000 else 1
    big = Image.new("RGBA", (width * ss, height * ss), background + (255,))
    draw = ImageDraw.Draw(big)
    line_px = max(1, int(round(line_width_world * scale * ss)))
    radius = max(0, line_px // 2 - 1)

    def to_px(x: float, y: float) -> tuple[float, float]:
        return ((x - min_x) * scale + 1, height - 1 - (y - min_y) * scale)

    def to_big(x: float, y: float) -> tuple[float, float]:
        return ((x - min_x) * scale * ss + ss, (height - 1 - (y - min_y) * scale) * ss)

    for x0, y0, x1, y1, rgba in segments:
        alpha = rgba[3]
        if alpha < 255:
            # 预览不叠半透明：与背景混合
            ratio = alpha / 255.0
            color = tuple(int(round(background[i] + (rgba[i] - background[i]) * ratio))
                          for i in range(3))
        else:
            color = (rgba[0], rgba[1], rgba[2])
        point_a = to_big(x0, y0)
        point_b = to_big(x1, y1)
        draw.line([point_a, point_b], fill=color + (255,), width=line_px)
        if radius:
            # 拐点补圆点，消掉粗线的缺口
            for px, py in (point_a, point_b):
                draw.ellipse([px - radius, py - radius, px + radius, py + radius],
                             fill=color + (255,))

    if ss > 1:
        # BOX（面积平均）而不是 LANCZOS：后者有负瓣，会在密集横线之间产生暗边
        big = big.resize((width, height), Image.BOX)
        draw = ImageDraw.Draw(big)
    image = big

    if screen_world and screen_world > 0:
        # 每“一屏”画一条灰线，直观看出这张图要滚多少屏
        lines = int((max_y - min_y) // screen_world) + 1
        for index in range(lines + 1):
            world_y = min_y + index * screen_world
            py = to_px(0.0, world_y)[1]
            if 0 <= py < height:
                draw.line([(0, py), (width, py)], fill=(130, 130, 130, 255), width=1)

    if judge_tick is not None:
        y = _tick_to_world(chart, judge_tick, speed_const)
        py = to_px(0.0, y)[1]
        if 0 <= py < height:
            draw.line([(0, py), (width, py)], fill=(120, 120, 120, 255), width=1)

    return image.convert("RGB")


def render_chart(
    chart: dict,
    out_path: str | Path,
    *,
    speed_const: float = DEFAULT_SPEED_CONST,
    line_width_world: float = DEFAULT_LINE_WIDTH_WORLD,
    max_width: int = 1200,
    max_height: int = 4000,
    background: tuple[int, int, int] | None = None,
    judge_tick: float | None = None,
    screen_world: float | None = None,
    canvas_index: int | None = None,
    tick_range: tuple[float, float] | None = None,
    view_width_world: float | None = None,
) -> Path:
    """把整张“画面”渲染成 PNG，返回输出路径。"""
    image = render_chart_image(
        chart,
        speed_const=speed_const,
        line_width_world=line_width_world,
        max_width=max_width,
        max_height=max_height,
        background=background,
        judge_tick=judge_tick,
        screen_world=screen_world,
        canvas_index=canvas_index,
        tick_range=tick_range,
        view_width_world=view_width_world,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return out_path


def _tick_to_world(chart: dict, tick: float, speed_const: float) -> float:
    """近似把 tick 换成世界高度（画布流速恒定时才准确）。"""
    bpm = float(chart.get("bPM") or 120.0)
    canvas_speed = 1.0
    for move in chart.get("canvasMoves") or []:
        points = sorted(move.get("speedKeyPoints") or [], key=lambda p: p["time"])
        if not points:
            continue
        canvas_speed = float(points[0]["value"])
        for point in points:
            if point["time"] <= tick:
                canvas_speed = float(point["value"])
        break
    return (tick * 60.0 / bpm) * canvas_speed * speed_const


def combine_images(source: Image.Image, rendered_image: Image.Image,
                   background: tuple[int, int, int] = (255, 255, 255),
                   max_height: int = 1200) -> Image.Image:
    """把原图和渲染结果并排拼成一张图（内存版本，GUI 用）。"""
    height = max(240, min(rendered_image.height, max_height))
    src = source.convert("RGBA")
    flat = Image.new("RGBA", src.size, background + (255,))
    src = Image.alpha_composite(flat, src)
    scale = height / src.height
    src = src.resize((max(1, int(round(src.width * scale))), height), Image.LANCZOS)
    dst = rendered_image.resize(
        (max(1, int(round(rendered_image.width * height / rendered_image.height))), height),
        Image.LANCZOS,
    )
    canvas = Image.new("RGB", (src.width + dst.width + 8, height), (24, 24, 24))
    canvas.paste(src.convert("RGB"), (0, 0))
    canvas.paste(dst, (src.width + 8, 0))
    return canvas


def side_by_side(source: Image.Image, rendered: Path, out_path: str | Path,
                 background: tuple[int, int, int] = (255, 255, 255)) -> Path:
    """把原图和渲染结果并排拼接，方便肉眼对比。"""
    canvas = combine_images(source, Image.open(rendered).convert("RGB"), background)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path
