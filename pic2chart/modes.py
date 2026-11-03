"""两种“图片 → 折线”策略。

所有折线都是 ``[(tick, xPosition, [r, g, b, a]), ...]``，可直接交给
``chartbuild`` 生成引导线。tick 递增或相同（``addLine`` 会按 time 稳定排序）。

* ``scan``：逐行扫描。把图片重采样成网格，每行里连续的可见像素拼成一条水平
  引导线，节点颜色就是像素颜色。适合照片、渐变、多色插画。
* ``column``：逐列扫描。同上但转置：每列连续像素拼成一条**竖线**。列距按编辑器
  水平细分 1/16 取，相邻列刚好紧贴。
* ``outline``：轮廓描边。先做掩膜，用 marching squares 提轮廓，再按行单调性
  切段，一段一条引导线。适合线稿、logo、纯色剪影。
* ``thin``：骨架线。先做掩膜、再骨架化（skeletonize），把骨架走成一条条
  中心线。适合真正“一笔一画”的线稿，同一条笔画只出一条线。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from PIL import Image

from . import imageproc
from .mapping import PictureMap

Polyline = list[tuple[float, float, list[int]]]


@dataclass
class BuildStats:
    """生成结果的统计信息，用于命令行输出。"""

    lines: int = 0
    nodes: int = 0

    def add(self, polyline: Sequence) -> None:
        self.lines += 1
        self.nodes += len(polyline)


# --------------------------------------------------------------------------- 扫描
def scan_polylines(
    image: Image.Image,
    cmap: PictureMap,
    *,
    cols: int = 128,
    rows: int | None = None,
    alpha_cut: int = 8,
    whole_row: bool = False,
    min_run: int = 1,
    color_tolerance: float = 8.0,
) -> tuple[list[Polyline], BuildStats]:
    """逐行扫描，返回水平引导线列表。

    ``whole_row=True`` 时每行只出一条整行线，用 ``alpha=0`` 的节点表示空隙
    （和 ``drawPolygon`` 的线性填充同一种做法）；默认只画可见段。

    ``color_tolerance`` 会在颜色足够接近时丢掉中间节点，能显著减少节点数；
    设为 0 则保留每个采样点。
    """
    arr = imageproc.resample_array(image, cols, rows or _rows_for(image, cols))
    h, w = arr.shape[:2]
    stats = BuildStats()
    result: list[Polyline] = []
    cell = cmap.width_x / w

    for r in range(h):
        tick = cmap.tick_of_row_sample(r, h)
        row = arr[r]

        if whole_row:
            nodes = []
            for c in range(w):
                color = [int(v) for v in row[c]]
                if color[3] <= alpha_cut:
                    color[3] = 0
                nodes.append((tick, cmap.x_of_col_sample(c, w), color))
            nodes = _simplify_colors(nodes, color_tolerance)
            if len(nodes) >= 2:
                result.append(nodes)
                stats.add(nodes)
            continue

        start = None
        for c in range(w + 1):
            visible = c < w and int(row[c][3]) > alpha_cut
            if visible and start is None:
                start = c
            elif not visible and start is not None:
                run = list(range(start, c))
                if len(run) >= min_run:
                    nodes = [(tick, cmap.x_of_col_sample(i, w),
                              [int(v) for v in row[i]]) for i in run]
                    nodes = _simplify_colors(nodes, color_tolerance)
                    # 让线段覆盖整格宽度，避免相邻列之间出现缝
                    tick0, x_first, c_first = nodes[0]
                    tick1, x_last, c_last = nodes[-1]
                    nodes[0] = (tick0, x_first - cell / 2.0, c_first)
                    nodes[-1] = (tick1, x_last + cell / 2.0, c_last)
                    if len(nodes) == 1:
                        nodes.append(nodes[0])
                    result.append(nodes)
                    stats.add(nodes)
                start = None
    return result, stats


def column_polylines(
    image: Image.Image,
    cmap: PictureMap,
    *,
    cols: int,
    rows: int,
    alpha_cut: int = 8,
    color_tolerance: float = 8.0,
    min_run: int = 1,
) -> tuple[list[Polyline], BuildStats]:
    """逐列扫描：每列里连续的可见像素串成一条**竖线**（x 固定、tick 变化）。

    和 ``scan`` 是转置关系。列距默认取 ``mapping.COLUMN_STEP_X``
    （0.00625 个 xPosition 单位，即编辑器水平细分 1/16），这样相邻两列
    在编辑器里刚好紧贴、不会露缝。
    """
    arr = imageproc.resample_array(image, cols, rows)
    h, w = arr.shape[:2]
    stats = BuildStats()
    result: list[Polyline] = []
    cell_tick = cmap.height_ticks / h

    for col in range(w):
        x = cmap.x_of_col_sample(col, w)
        start = None
        for row in range(h + 1):
            visible = row < h and int(arr[row, col][3]) > alpha_cut
            if visible and start is None:
                start = row
            elif not visible and start is not None:
                run = list(range(start, row))
                if len(run) >= min_run:
                    # 先按“从下往上”排好，tick 就是递增的
                    nodes = [
                        (cmap.tick_of_row_sample(index, h), x,
                         [int(v) for v in arr[index, col]])
                        for index in reversed(run)
                    ]
                    nodes = _simplify_colors(nodes, color_tolerance)
                    if len(nodes) == 1:
                        tick, _, color = nodes[0]
                        nodes = [(tick - cell_tick / 2.0, x, color),
                                 (tick + cell_tick / 2.0, x, color)]
                    else:
                        # 首尾各外扩半格，覆盖整格高度
                        t0, _, c0 = nodes[0]
                        t1, _, c1 = nodes[-1]
                        nodes[0] = (t0 - cell_tick / 2.0, x, c0)
                        nodes[-1] = (t1 + cell_tick / 2.0, x, c1)
                    result.append(nodes)
                    stats.add(nodes)
                start = None
    return result, stats


def _simplify_colors(nodes: Polyline, tolerance: float) -> Polyline:
    """按颜色误差丢掉多余的中间节点（分段线性近似）。

    递归二分：只要区间内某个节点的实际颜色和“两端线性插值”差距超过
    ``tolerance``（0-255），就保留它并继续细分。对逐行/逐列两种模式都适用
    （前者沿 x 变化、后者沿 tick 变化）。
    """
    if tolerance <= 0 or len(nodes) <= 2:
        return nodes

    keep = {0, len(nodes) - 1}
    stack = [(0, len(nodes) - 1)]
    while stack:
        left, right = stack.pop()
        if right - left < 2:
            continue
        t0, _, c0 = nodes[left]
        t1, _, c1 = nodes[right]
        span = t1 - t0
        worst = -1.0
        worst_index = -1
        for index in range(left + 1, right):
            tick, _, color = nodes[index]
            ratio = (tick - t0) / span if abs(span) > 1e-12 else \
                (index - left) / (right - left)
            error = max(abs(color[k] - (c0[k] + (c1[k] - c0[k]) * ratio))
                        for k in range(4))
            if error > worst:
                worst = error
                worst_index = index
        if worst > tolerance and worst_index > 0:
            keep.add(worst_index)
            stack.append((left, worst_index))
            stack.append((worst_index, right))

    return [nodes[index] for index in sorted(keep)]


def _rows_for(image: Image.Image, cols: int) -> int:
    """按图片宽高比推算行数，避免网格被拉变形。"""
    return max(1, round(cols * image.height / image.width))


# --------------------------------------------------------------------------- 轮廓
def _smooth_field(mask: np.ndarray, sigma: float) -> np.ndarray:
    """把二值掩膜变成浮点场再做高斯模糊，供 0.5 等值线提轮廓。

    这样能消掉 marching squares 在斜边上的 1 px 阶梯。``mode="nearest"`` 很关键：
    贴边的东西不能因为补零而缩进去。
    """
    if sigma <= 0:
        return mask.astype(np.float32)
    from scipy.ndimage import gaussian_filter

    return gaussian_filter(mask.astype(np.float32), sigma=float(sigma), mode="nearest")


def _corner_flags(points: np.ndarray, *, window: int = 3,
                  angle_deg: float = 60.0) -> np.ndarray:
    """标出轮廓上的角点（转向角超过 ``angle_deg`` 的点）与首尾端点。"""
    n = len(points)
    flags = np.zeros(n, dtype=bool)
    if n == 0:
        return flags
    flags[0] = flags[-1] = True
    window = max(1, int(window))
    if n < 2 * window + 3:
        flags[:] = True
        return flags

    left = points[:n - 2 * window]
    mid = points[window:n - window]
    right = points[2 * window:]
    d1 = mid - left
    d2 = right - mid
    n1 = np.hypot(d1[:, 0], d1[:, 1])
    n2 = np.hypot(d2[:, 0], d2[:, 1])
    valid = (n1 > 1e-9) & (n2 > 1e-9)

    cos_turn = np.ones(len(mid), dtype=np.float64)
    cos_turn[valid] = ((d1[valid, 0] * d2[valid, 0] + d1[valid, 1] * d2[valid, 1])
                       / (n1[valid] * n2[valid]))
    flags[window:n - window] = cos_turn < np.cos(np.deg2rad(angle_deg))
    return flags


def _chaikin(run: np.ndarray, passes: int) -> np.ndarray:
    """Chaikin 切角，一遍把折线磨圆一点（点数翻倍，后面会重新简化）。"""
    pts = run
    for _ in range(max(0, int(passes))):
        if len(pts) < 3:
            break
        out = [pts[0]]
        for a, b in zip(pts[:-1], pts[1:]):
            out.append(0.75 * a + 0.25 * b)
            out.append(0.25 * a + 0.75 * b)
        out.append(pts[-1])
        pts = np.asarray(out, dtype=np.float64)
    return pts


def _smooth_contour_points(points: np.ndarray, *, passes: int = 1,
                           window: int = 3, angle_deg: float = 60.0) -> np.ndarray:
    """沿轮廓做保角点的平滑：角点与端点钉住，其余每段单独 Chaikin。"""
    if passes <= 0 or len(points) < 5:
        return points
    flags = _corner_flags(points, window=window, angle_deg=angle_deg)
    anchors = np.flatnonzero(flags)
    if anchors[0] != 0:
        anchors = np.concatenate(([0], anchors))
    if anchors[-1] != len(points) - 1:
        anchors = np.concatenate((anchors, [len(points) - 1]))

    pieces: list[np.ndarray] = []
    for start, stop in zip(anchors[:-1], anchors[1:]):
        run = points[start:stop + 1]
        smoothed = _chaikin(run, passes) if len(run) >= 3 else run
        if pieces:
            smoothed = smoothed[1:]          # 接点已经在前一段末尾
        pieces.append(smoothed)
    return np.vstack(pieces) if pieces else points


def outline_polylines(
    image: Image.Image,
    cmap: PictureMap,
    *,
    alpha_cut: int = 8,
    ink_threshold: float | None = None,
    invert: bool = False,
    tolerance_px: float = 1.0,
    layers: int = 1,
    layer_gap_px: int = 2,
    min_length_px: float = 4.0,
    color: list[int] | None = None,
    smooth_px: float = 0.0,
    smooth_passes: int = 1,
    corner_deg: float = 60.0,
) -> tuple[list[Polyline], BuildStats]:
    """提取轮廓并输出折线（每条折线在 tick 上单调）。

    ``smooth_px > 0`` 时先对掩膜做同 σ 的高斯平滑再提 0.5 等值线（消阶梯），
    随后沿轮廓做保角点的 Chaikin 平滑（``corner_deg`` 以上的转角被钉住）。
    """
    from skimage import measure, morphology

    arr = imageproc.as_array(image)
    mask = imageproc.build_mask(
        arr, alpha_cut=alpha_cut, ink_threshold=ink_threshold, invert=invert
    )
    if not mask.any():
        raise imageproc.ImageError("掩膜为空：没有可绘制的像素（试试 --ink 或 --invert）")

    stats = BuildStats()
    result: list[Polyline] = []
    seen: set[tuple[int, int, int, int]] = set()

    for layer in range(max(1, layers)):
        current = mask
        if layer:
            radius = max(1, layer_gap_px) * layer
            current = morphology.binary_dilation(mask, morphology.disk(radius))

        # 场平滑：消掉斜边上的 1 px 阶梯（对贴边掩膜用 nearest 补边，不会缩进去）
        field = _smooth_field(current, smooth_px)
        contours = measure.find_contours(field, 0.5)
        if smooth_px > 0 and not contours:
            contours = measure.find_contours(current.astype(np.float32), 0.5)
        for contour in contours:
            contour = _prepare_contour(contour)
            if len(contour) < 4:
                continue
            if smooth_px > 0 and smooth_passes > 0:
                contour = _smooth_contour_points(
                    contour, passes=smooth_passes, window=3, angle_deg=corner_deg
                )
            simplified = measure.approximate_polygon(contour, tolerance=tolerance_px)
            if len(simplified) < 2:
                continue
            for segment in _split_monotonic(simplified):
                nodes = _segment_nodes(
                    segment, cmap, arr, mask, color=color, min_length_px=min_length_px
                )
                if len(nodes) < 2:
                    continue
                key = _polyline_key(nodes)
                if key in seen:
                    continue
                seen.add(key)
                result.append(nodes)
                stats.add(nodes)
    return result, stats


def _prepare_contour(contour: np.ndarray) -> np.ndarray:
    """闭合轮廓：去掉重复收尾点、旋转到行号最小处、再闭合；开放轮廓原样返回。

    ``find_contours`` 对**贴到图像边界**的区域返回开放路径（首尾直接落在边界上，
    例如被画框截断的人像）。这种轮廓绝对不能闭合：一旦从末点连回首点，就会画出
    一条横跨画面、根本不存在的“边”。被截断的地方就该断在边界上。
    """
    if len(contour) <= 1:
        return contour
    if not np.allclose(contour[0], contour[-1]):
        return contour                       # 开放轮廓：保持开放
    loop = contour[:-1]
    if len(loop) == 0:
        return contour
    start = int(np.argmin(loop[:, 0]))
    rotated = np.roll(loop, -start, axis=0)
    return np.vstack([rotated, rotated[:1]])


def _split_monotonic(points: np.ndarray, eps: float = 1e-9) -> Iterable[np.ndarray]:
    """把折线按“行号单调”切成若干段。

    谱面里 ``addLine`` 会按 time 排序，非单调折线会被重新连线，所以闭合轮廓
    必须先切成上下两半（以及局部极值处的更多段）。
    """
    if len(points) < 2:
        return
    current = [points[0]]
    direction = 0
    for point in points[1:]:
        delta = float(point[0] - current[-1][0])
        if abs(delta) > eps:
            new_direction = 1 if delta > 0 else -1
            if direction and new_direction != direction:
                yield np.asarray(current)
                current = [current[-1]]
            direction = new_direction
        current.append(point)
    if len(current) >= 2:
        yield np.asarray(current)


def _segment_nodes(
    segment: np.ndarray,
    cmap: PictureMap,
    arr: np.ndarray,
    mask: np.ndarray,
    *,
    color: list[int] | None,
    min_length_px: float,
) -> Polyline:
    """把一段轮廓转成线节点；过短或退化的段直接丢弃。"""
    rows = segment[:, 0]
    cols = segment[:, 1]
    length = float(np.hypot(np.diff(cols), np.diff(rows)).sum())
    if length < min_length_px:
        return []

    nodes: Polyline = []
    for row, col in zip(rows, cols):
        tick = cmap.tick_of_row(float(row))
        x = cmap.x_of_px(float(col))
        if color is not None:
            node_color = list(color)
        else:
            node_color = imageproc.inner_stroke_color(arr, mask, col, row)
            # 轮廓本身在抗锯齿边缘，取到的 alpha 往往不足 255；描边应该是实色
            node_color[3] = 255
        nodes.append((tick, x, node_color))

    # 去掉相邻重复节点
    cleaned: Polyline = []
    for node in nodes:
        if cleaned and abs(node[0] - cleaned[-1][0]) < 1e-9 and \
                abs(node[1] - cleaned[-1][1]) < 1e-9:
            continue
        cleaned.append(node)
    return cleaned


def _polyline_key(nodes: Polyline) -> tuple[int, int, int, int]:
    """用首尾节点（像素级取整）做去重键。"""
    first, last = nodes[0], nodes[-1]
    return (
        int(round(first[0] * 1e4)),
        int(round(first[1] * 1e4)),
        int(round(last[0] * 1e4)),
        int(round(last[1] * 1e4)),
    )


# --------------------------------------------------------------------------- 骨架
def thin_polylines(
    image: Image.Image,
    cmap: PictureMap,
    *,
    alpha_cut: int = 8,
    ink_threshold: float | None = None,
    invert: bool = False,
    simplify_px: float = 0.8,
    min_length_px: float = 4.0,
    color: list[int] | None = None,
) -> tuple[list[Polyline], BuildStats]:
    """把掩膜骨架化后走线，得到“一笔一画”的中心线。

    适合线条清晰、背景干净的线稿：一条笔画只会生成一条引导线，而不是
    轮廓模式下的内外两条边。
    """
    from skimage import morphology

    arr = imageproc.as_array(image)
    mask = imageproc.build_mask(
        arr, alpha_cut=alpha_cut, ink_threshold=ink_threshold, invert=invert
    )
    if not mask.any():
        raise imageproc.ImageError("掩膜为空：没有可绘制的像素（试试 --ink 或 --invert）")

    skeleton = morphology.skeletonize(mask)
    if not skeleton.any():
        raise imageproc.ImageError("骨架为空：掩膜太细或阈值不合适")

    stats = BuildStats()
    result: list[Polyline] = []
    seen: set[tuple[int, int, int, int]] = set()

    for path in _skeleton_paths(skeleton):
        if len(path) < 2:
            continue
        points = np.asarray(path, dtype=np.float64)  # (row, col)
        if simplify_px > 0 and len(points) > 3:
            from skimage import measure

            points = measure.approximate_polygon(points, tolerance=simplify_px)
        if len(points) < 2:
            continue
        for segment in _split_monotonic(points):
            nodes = _segment_nodes(
                segment, cmap, arr, mask, color=color, min_length_px=min_length_px
            )
            if len(nodes) < 2:
                continue
            key = _polyline_key(nodes)
            reversed_key = (key[2], key[3], key[0], key[1])
            if key in seen or reversed_key in seen:
                continue
            seen.add(key)
            result.append(nodes)
            stats.add(nodes)
    return result, stats


def _skeleton_paths(skeleton: np.ndarray) -> list[list[tuple[int, int]]]:
    """把骨架像素图走成若干条像素路径（8 邻域）。

    端点 / 分叉点是路径边界；没有端点（纯闭环）时从任意未访问边出发。
    """
    pixels = {(int(r), int(c)) for r, c in np.argwhere(skeleton)}
    neighbours: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for row, col in pixels:
        around = []
        for drow in (-1, 0, 1):
            for dcol in (-1, 0, 1):
                if drow == 0 and dcol == 0:
                    continue
                candidate = (row + drow, col + dcol)
                if candidate in pixels:
                    around.append(candidate)
        neighbours[(row, col)] = around

    degree = {pixel: len(around) for pixel, around in neighbours.items()}
    nodes = {pixel for pixel, value in degree.items() if value != 2}
    visited: set[frozenset] = set()

    def walk(start: tuple[int, int], following: tuple[int, int]) -> list[tuple[int, int]]:
        path = [start, following]
        visited.add(frozenset((start, following)))
        previous, current = start, following
        while current not in nodes and current != start:
            options = [
                candidate for candidate in neighbours[current]
                if candidate != previous
                and frozenset((current, candidate)) not in visited
            ]
            if not options:
                break
            if len(options) > 1:  # 理论上不该发生（分叉点已在 nodes 里）
                options.sort()
            target = options[0]
            visited.add(frozenset((current, target)))
            path.append(target)
            previous, current = current, target
        return path

    paths: list[list[tuple[int, int]]] = []
    for node in sorted(nodes):
        for following in sorted(neighbours[node]):
            if frozenset((node, following)) in visited:
                continue
            paths.append(walk(node, following))

    # 纯闭环：所有像素度数都是 2，上面走不到
    for pixel in sorted(pixels):
        for following in sorted(neighbours[pixel]):
            if frozenset((pixel, following)) in visited:
                continue
            paths.append(walk(pixel, following))
    return paths
