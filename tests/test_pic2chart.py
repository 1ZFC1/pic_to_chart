"""pic2chart 的最小单元测试。

    cd pic_to_chart
    python -m unittest discover -s tests -v

只依赖标准库 + Pillow / numpy / scikit-image（与工具本体一致）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "rizChartFunction_Library" / "src"))

from pic2chart import chartbuild, imageproc, mapping, modes, pipeline, preview  # noqa: E402
from pic2chart.cli import main  # noqa: E402


def _image_with_disc(size: int = 64, hole: bool = False) -> Image.Image:
    """白底黑圆（可选中心挖孔），用来测轮廓。"""
    image = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    margin = size // 6
    draw.ellipse([margin, margin, size - margin, size - margin], fill=(0, 0, 0, 255))
    if hole:
        inner = size // 3
        draw.ellipse([inner, inner, size - inner, size - inner], fill=(255, 255, 255, 255))
    return image


def _image_with_alpha(size: int = 32) -> Image.Image:
    """左半不透明红、右半完全透明。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size // 2 - 1, size - 1], fill=(255, 0, 0, 255))
    return image


class MappingTest(unittest.TestCase):
    def test_flow_formula(self):
        # S1 = 7*(S0-1)/9 + 6，摄像机缩放 1 时
        self.assertAlmostEqual(mapping.speed_const_from_flow(1.5), 6.3889, places=3)
        self.assertAlmostEqual(mapping.speed_const_from_flow(5.5), 9.5, places=6)
        self.assertAlmostEqual(mapping.speed_const_from_flow(1.0), 6.0, places=6)
        # 反解回 S0
        self.assertAlmostEqual(mapping.flow_from_speed_const(6.3889), 1.5, places=3)

    def test_ticks_per_x_matches_editor_constants(self):
        # 5.625 世界单位/x 单位 ÷ (60/175 × 6.3889) 世界单位/tick
        self.assertAlmostEqual(mapping.ticks_per_x(175.0), 2.5679, places=3)
        self.assertAlmostEqual(mapping.world_per_tick(175.0), 2.1905, places=3)

    def test_visible_window_defaults(self):
        # 实测：一屏 1.0 单位宽 × 6.9 世界高 -> 175 BPM / S0=1.5 下约 3.15 拍
        self.assertAlmostEqual(mapping.DEFAULT_WIDTH_X, 1.0)
        self.assertAlmostEqual(mapping.DEFAULT_VIEW_HEIGHT_WORLD, 6.9)
        self.assertAlmostEqual(mapping.visible_height_ticks(175.0), 3.15, places=2)

    def test_picture_map_is_isometric(self):
        cmap = mapping.PictureMap(
            width_px=200, height_px=100, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        self.assertAlmostEqual(cmap.dx, 0.005)
        self.assertAlmostEqual(cmap.dy, 0.0125)
        self.assertAlmostEqual(cmap.x_of_px(0), -0.5)
        self.assertAlmostEqual(cmap.x_of_px(200), 0.5)
        self.assertAlmostEqual(cmap.px_of_x(cmap.x_of_px(37)), 37)
        # 图片顶部（row=0）对应最大 tick
        self.assertGreater(cmap.tick_of_row(0), cmap.tick_of_row(100))
        self.assertAlmostEqual(cmap.tick_of_row(0), 8.0 + 100 * 0.0125)

    def test_fit_screen_shrinks_tall_image(self):
        wide = mapping.fit_width_x(100, 10, bpm=175.0, ticks_per_x=2.524,
                                   max_width_x=1.0)
        tall = mapping.fit_width_x(10, 100, bpm=175.0, ticks_per_x=2.524,
                                   max_width_x=1.0)
        self.assertAlmostEqual(wide, 1.0)
        self.assertLess(tall, wide)


class MaskTest(unittest.TestCase):
    def test_alpha_mask(self):
        arr = imageproc.as_array(_image_with_alpha())
        mask = imageproc.build_mask(arr)
        self.assertEqual(mask.shape, (32, 32))
        self.assertTrue(mask[:, :16].all())
        self.assertFalse(mask[:, 16:].any())

    def test_ink_mask_auto_threshold(self):
        arr = imageproc.as_array(_image_with_disc())
        mask = imageproc.build_mask(arr)
        self.assertTrue(mask.any())
        self.assertFalse(mask[0, 0])  # 白底不算墨迹
        self.assertTrue(mask[32, 32])  # 圆心算墨迹

    def test_invert(self):
        arr = imageproc.as_array(_image_with_disc())
        mask = imageproc.build_mask(arr, invert=True)
        self.assertTrue(mask[0, 0])
        self.assertFalse(mask[32, 32])


class ScanModeTest(unittest.TestCase):
    def _cmap(self, image: Image.Image) -> mapping.PictureMap:
        return mapping.PictureMap(
            width_px=image.width, height_px=image.height, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )

    def test_transparent_half_only_draws_left(self):
        image = _image_with_alpha()
        cmap = self._cmap(image)
        polylines, stats = modes.scan_polylines(image, cmap, cols=16, rows=16)
        self.assertEqual(stats.lines, len(polylines))
        self.assertEqual(len(polylines), 16)  # 每行一条左半段
        for line in polylines:
            self.assertGreaterEqual(len(line), 2)
            for tick, x, color in line:
                # 重采样会让第 9 列沾到一点 alpha，放宽到半格
                self.assertLessEqual(x, 0.07)
                self.assertGreaterEqual(tick, 8.0)
                self.assertGreaterEqual(color[3], 0)
        # 行是按时间递增的（顶部先出现）
        first_row_tick = polylines[0][0][0]
        last_row_tick = polylines[-1][0][0]
        self.assertAlmostEqual(first_row_tick, cmap.tick_of_row_sample(0, 16))
        self.assertGreater(first_row_tick, last_row_tick)

    def test_color_simplification_keeps_endpoints(self):
        nodes = [
            (8.0, -0.5, [255, 0, 0, 255]),
            (8.0, -0.25, [168, 0, 86, 255]),
            (8.0, 0.0, [87, 0, 169, 255]),
            (8.0, 0.5, [0, 0, 255, 255]),
        ]
        flat = modes._simplify_colors(nodes, 8.0)
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0][1], -0.5)
        self.assertEqual(flat[-1][1], 0.5)

    def test_whole_row_keeps_every_row(self):
        image = _image_with_alpha()
        cmap = self._cmap(image)
        polylines, _ = modes.scan_polylines(
            image, cmap, cols=8, rows=8, whole_row=True, color_tolerance=0
        )
        self.assertEqual(len(polylines), 8)
        # 整行模式会带上透明节点
        alphas = {color[3] for line in polylines for *_, color in line}
        self.assertIn(0, alphas)


class OutlineModeTest(unittest.TestCase):
    def _cmap(self, image: Image.Image) -> mapping.PictureMap:
        return mapping.PictureMap(
            width_px=image.width, height_px=image.height, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )

    def test_disc_contour(self):
        image = _image_with_disc(64)
        cmap = mapping.PictureMap(
            width_px=64, height_px=64, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        polylines, stats = modes.outline_polylines(image, cmap, tolerance_px=0.5)
        self.assertGreater(stats.lines, 0)
        for line in polylines:
            times = [node[0] for node in line]
            # 每段必须按 tick 单调（升序或降序都行，addLine 会稳定排序）
            ascending = all(b >= a for a, b in zip(times, times[1:]))
            descending = all(b <= a for a, b in zip(times, times[1:]))
            self.assertTrue(ascending or descending)
            self.assertTrue(all(node[2][3] == 255 for node in line))

    def test_border_cut_band_gives_two_open_edges(self):
        """墨迹横贯整幅（贴着左右边界）：只应有上下两条水平边、各自通到两侧边界。"""
        image = Image.new("RGBA", (60, 40), (255, 255, 255, 255))
        ImageDraw.Draw(image).rectangle([0, 10, 59, 29], fill=(0, 0, 0, 255))
        cmap = self._cmap(image)

        polylines, _ = modes.outline_polylines(image, cmap, ink_threshold=128)
        self.assertEqual(len(polylines), 2)
        rows = []
        for line in polylines:
            self.assertEqual(len({round(node[0], 6) for node in line}), 1)   # 水平
            rows.append(round(cmap.row_of_tick(line[0][0]), 1))
            xs = [cmap.px_of_x(node[1]) for node in line]
            self.assertLess(min(xs), 0.6)                                    # 通到左边界
            self.assertGreater(max(xs), 58.4)                                # 通到右边界
        self.assertEqual(sorted(rows), [9.5, 29.5])

    def test_cut_off_circle_has_no_chord_along_border(self):
        """被上边界截断的圆：轮廓停在边界上，不能沿边界补出一条横线。"""
        image = Image.new("RGBA", (60, 40), (255, 255, 255, 255))
        ImageDraw.Draw(image).ellipse([18, -26, 42, 12], fill=(0, 0, 0, 255))
        cmap = self._cmap(image)

        polylines, _ = modes.outline_polylines(image, cmap, ink_threshold=128)
        self.assertGreater(len(polylines), 0)
        for line in polylines:
            for left, right in zip(line, line[1:]):
                y0 = cmap.row_of_tick(left[0])
                y1 = cmap.row_of_tick(right[0])
                x0 = cmap.px_of_x(left[1])
                x1 = cmap.px_of_x(right[1])
                self.assertFalse(max(y0, y1) < 1.0 and abs(x1 - x0) > 5.0,
                                 "出现了沿上边界的假边（强行闭合的弦）")

    def test_tall_cut_off_shape_has_no_long_chord(self):
        """上下都被截断的弯曲剪影：每条线段都不该是贯通全高的闭合弦。"""
        import math

        w, h = 60, 120
        image = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        for y in range(h):
            left = 18 + 7 * math.sin(y / 9.0)
            right = 42 + 7 * math.sin(y / 9.0 + 1.4)
            draw.line([(left, y), (right, y)], fill=(0, 0, 0, 255), width=1)
        cmap = self._cmap(image)

        polylines, _ = modes.outline_polylines(image, cmap, ink_threshold=128)
        self.assertGreater(len(polylines), 0)
        longest = 0.0
        for line in polylines:
            for left, right in zip(line, line[1:]):
                dx = cmap.px_of_x(left[1]) - cmap.px_of_x(right[1])
                dy = cmap.row_of_tick(left[0]) - cmap.row_of_tick(right[0])
                longest = max(longest, math.hypot(dx, dy))
        self.assertLess(longest, 45.0, "出现了长距离闭合弦（应停在边界上）")

    def test_smoothing_keeps_corners(self):
        """平滑不能把直角磨圆（角点保护）。"""
        image = Image.new("RGBA", (80, 80), (255, 255, 255, 255))
        ImageDraw.Draw(image).rectangle([20, 20, 60, 60], fill=(0, 0, 0, 255))
        cmap = mapping.PictureMap(
            width_px=80, height_px=80, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )

        polylines, _ = modes.outline_polylines(
            image, cmap, ink_threshold=128, tolerance_px=0.5, smooth_px=1.5,
        )
        pts = [(cmap.px_of_x(n[1]), cmap.row_of_tick(n[0]))
               for line in polylines for n in line]
        self.assertTrue(pts)
        for corner in ((19.5, 19.5), (60.5, 19.5), (19.5, 60.5), (60.5, 60.5)):
            nearest = min(((px - corner[0]) ** 2 + (py - corner[1]) ** 2) ** 0.5
                          for px, py in pts)
            self.assertLess(nearest, 1.5, f"角点 {corner} 被磨掉了")

    def test_smoothing_removes_speckles(self):
        """smooth>0 时，孤立 1px 小点会被抹掉，线数明显减少。"""
        image = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle([30, 30, 70, 70], fill=(0, 0, 0, 255))
        for x, y in ((20, 20), (80, 25), (25, 78), (78, 80)):
            draw.rectangle([x, y, x, y], fill=(0, 0, 0, 255))
        cmap = mapping.PictureMap(
            width_px=100, height_px=100, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        kwargs = dict(ink_threshold=128, tolerance_px=0.8, min_length_px=0.0)

        raw, raw_stats = modes.outline_polylines(image, cmap, **kwargs)
        smooth, smooth_stats = modes.outline_polylines(
            image, cmap, smooth_px=1.5, **kwargs
        )
        self.assertGreater(raw_stats.lines, smooth_stats.lines)
        self.assertLessEqual(smooth_stats.lines, 3)      # 只剩方块自身的边

    def test_smoothing_keeps_ticks_monotonic(self):
        """平滑后每条折线仍然 tick 单调（addLine 会按时间排序）。"""
        image = _image_with_disc(64)
        cmap = mapping.PictureMap(
            width_px=64, height_px=64, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        polylines, _ = modes.outline_polylines(
            image, cmap, tolerance_px=0.3, smooth_px=1.2,
        )
        self.assertTrue(polylines)
        for line in polylines:
            ticks = [node[0] for node in line]
            self.assertTrue(
                all(b >= a for a, b in zip(ticks, ticks[1:]))
                or all(b <= a for a, b in zip(ticks, ticks[1:]))
            )

    def test_smoothing_off_is_default(self):
        """默认 smooth=0：和加参数之前完全一致。"""
        image = _image_with_disc(64)
        cmap = mapping.PictureMap(
            width_px=64, height_px=64, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        default_lines, default_stats = modes.outline_polylines(image, cmap)
        zero_lines, zero_stats = modes.outline_polylines(image, cmap, smooth_px=0.0)
        self.assertEqual(default_stats.nodes, zero_stats.nodes)
        self.assertEqual(len(default_lines), len(zero_lines))

    def test_layers_thicken(self):
        image = _image_with_disc(64)
        cmap = mapping.PictureMap(
            width_px=64, height_px=64, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        one, _ = modes.outline_polylines(image, cmap, layers=1)
        three, _ = modes.outline_polylines(image, cmap, layers=3)
        self.assertGreater(len(three), len(one))

    def test_empty_mask_raises(self):
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        cmap = mapping.PictureMap(
            width_px=16, height_px=16, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )
        with self.assertRaises(imageproc.ImageError):
            modes.outline_polylines(image, cmap)


class ThinModeTest(unittest.TestCase):
    def _cmap(self, image: Image.Image) -> mapping.PictureMap:
        return mapping.PictureMap(
            width_px=image.width, height_px=image.height, width_x=1.0,
            center_x=0.0, start_tick=8.0, ticks_per_x=2.5,
        )

    def test_line_art_skeleton(self):
        # 一条横线 + 一条竖线，交叉成一个十字
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle([4, 30, 60, 34], fill=(0, 0, 0, 255))
        draw.rectangle([30, 4, 34, 60], fill=(0, 0, 0, 255))
        polylines, stats = modes.thin_polylines(image, self._cmap(image),
                                                simplify_px=0.8)
        self.assertGreater(stats.lines, 0)
        self.assertGreater(stats.nodes, 0)
        for line in polylines:
            self.assertGreaterEqual(len(line), 2)
            times = [node[0] for node in line]
            ascending = all(b >= a for a, b in zip(times, times[1:]))
            descending = all(b <= a for a, b in zip(times, times[1:]))
            self.assertTrue(ascending or descending)
            # 骨架取的是掩膜内部颜色，描边必须是实色
            self.assertTrue(all(node[2][3] == 255 for node in line))

    def test_empty_image_raises(self):
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        with self.assertRaises(imageproc.ImageError):
            modes.thin_polylines(image, self._cmap(image))


def _image_with_strokes(size: int = 64) -> Image.Image:
    """透明底上画一个加号（粗线），用来测骨架/轮廓（实心圆细化后几乎没有骨架）。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle([4, size // 2 - 2, size - 4, size // 2 + 2], fill=(0, 0, 0, 255))
    draw.rectangle([size // 2 - 2, 4, size // 2 + 2, size - 4], fill=(0, 0, 0, 255))
    return image


class PipelineTest(unittest.TestCase):
    """GUI 与 CLI 共用的 pipeline.convert。"""

    def test_convert_scan_uses_auto_grid(self):
        image = _image_with_disc(48)
        result = pipeline.convert(image, pipeline.ConvertOptions(mode="scan"))
        self.assertGreater(result.line_count, 0)
        self.assertGreater(result.cols, 0)
        self.assertEqual(result.cols, result.rows)      # 正方形图 → 方形网格
        self.assertAlmostEqual(result.cmap.width_x, mapping.DEFAULT_WIDTH_X)

    def test_convert_thin_and_outline(self):
        image = _image_with_strokes(64)
        thin = pipeline.convert(image, pipeline.ConvertOptions(mode="thin"))
        outline = pipeline.convert(image, pipeline.ConvertOptions(mode="outline"))
        self.assertGreater(thin.line_count, 0)
        self.assertGreater(outline.line_count, 0)
        # 笔画的骨架是一条线；轮廓则是包围它的闭合环（切成两段）
        self.assertGreater(thin.line_count, outline.line_count)

    def test_screens_option_fits_window(self):
        image = _image_with_strokes(64)
        result = pipeline.convert(image, pipeline.ConvertOptions(mode="thin", screens=1.0))
        screens = result.cmap.height_ticks / result.screen_ticks
        self.assertLessEqual(screens, 1.0 + 0.02)

    def test_visible_ticks_overrides_window(self):
        image = _image_with_strokes(64)
        result = pipeline.convert(
            image, pipeline.ConvertOptions(mode="thin", visible_ticks=1.0)
        )
        self.assertAlmostEqual(result.screen_ticks, 1.0, places=3)
        # 一屏拍数给得越大，同一张图占的屏数越少
        big = pipeline.convert(image, pipeline.ConvertOptions(mode="thin", visible_ticks=4.0))
        self.assertLess(big.cmap.height_ticks / big.screen_ticks,
                        result.cmap.height_ticks / result.screen_ticks)

    def test_zoom_doubles_window(self):
        image = _image_with_strokes(64)
        normal = pipeline.convert(image, pipeline.ConvertOptions(mode="thin"))
        zoomed = pipeline.convert(image, pipeline.ConvertOptions(mode="thin", zoom=0.5))
        self.assertAlmostEqual(zoomed.screen_ticks, normal.screen_ticks * 2, places=2)

    def test_start_beat_must_be_non_negative(self):
        image = _image_with_strokes(64)
        with self.assertRaises(chartbuild.ChartError):
            pipeline.convert(image, pipeline.ConvertOptions(mode="thin", start_beat=-1))

    def test_append_to_existing_chart(self):
        image = _image_with_strokes(64)
        base = chartbuild.make_chart(220.0)
        result = pipeline.convert(image, pipeline.ConvertOptions(mode="thin"), base)
        self.assertEqual(result.chart_bpm, 220.0)       # 沿用已有谱面的 BPM
        self.assertGreater(len(result.chart["lines"]), 0)

    def test_column_mode_vertical_lines(self):
        image = _image_with_alpha(32)
        result = pipeline.convert(image, pipeline.ConvertOptions(mode="column"))
        self.assertGreater(result.line_count, 0)
        # 每列一条竖线：同一列里 x 固定、tick 递增
        for line in result.polylines:
            xs = {round(node[1], 6) for node in line}
            self.assertEqual(len(xs), 1)
            ticks = [node[0] for node in line]
            self.assertEqual(ticks, sorted(ticks))
        # 列距按 1/16 细分：0.1/16 个 x 单位
        expected = int(round(result.cmap.width_x / mapping.COLUMN_STEP_X))
        self.assertEqual(result.cols, expected)

    def test_canvas_index_auto_extend(self):
        image = _image_with_strokes(64)
        result = pipeline.convert(image, pipeline.ConvertOptions(mode="thin", canvas=2))
        self.assertEqual(result.canvas_index, 2)
        self.assertEqual(len(result.chart["canvasMoves"]), 3)
        self.assertTrue(any("新增" in note for note in result.notes))
        # 节点确实写到了 2 号画布
        canvases = {node["canvasIndex"]
                    for line in result.chart["lines"] for node in line["linePoints"]}
        self.assertEqual(canvases, {2})

    def test_canvas_index_rejects_negative_out_of_range(self):
        image = _image_with_strokes(64)
        with self.assertRaises(chartbuild.ChartError):
            pipeline.convert(image, pipeline.ConvertOptions(mode="thin", canvas=-5))

    def test_convert_reports_progress(self):
        image = _image_with_strokes(64)
        stages: list[tuple[str, int, int]] = []
        pipeline.convert(image, pipeline.ConvertOptions(mode="scan"),
                         on_progress=lambda s, d, t: stages.append((s, d, t)))
        names = [stage for stage, _d, _t in stages]
        self.assertIn("提取折线", names)
        self.assertIn("写入谱面", names)
        # 写入阶段要给出确定的进度（有总数、能到 100%）
        writes = [(d, t) for stage, d, t in stages if stage == "写入谱面"]
        self.assertTrue(writes)
        self.assertEqual(writes[-1][0], writes[-1][1])

    def test_convert_cancel_raises(self):
        import threading

        image = _image_with_strokes(64)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(pipeline.ConvertCancelled):
            pipeline.convert(image, pipeline.ConvertOptions(mode="thin"),
                             cancel=cancel)

    def test_summary_lines_mention_screen(self):
        image = _image_with_strokes(64)
        opts = pipeline.ConvertOptions(mode="thin")
        result = pipeline.convert(image, opts)
        lines = pipeline.summary_lines(result, opts)
        self.assertTrue(any("一屏" in line for line in lines))

    def test_calibration_layout(self):
        layout = pipeline.calibration_polylines(
            bpm=175.0, speed_const=mapping.DEFAULT_SPEED_CONST,
            view_height=mapping.DEFAULT_VIEW_HEIGHT_WORLD,
        )
        self.assertEqual(layout.vruler_beats, 20)
        self.assertGreater(len(layout.polylines), 50)
        self.assertAlmostEqual(layout.screen_ticks,
                               mapping.visible_height_ticks(175.0), places=3)
        brace_lo, brace_hi = layout.brace
        self.assertAlmostEqual(brace_hi - brace_lo, layout.screen_ticks, places=6)
        # 每段折线在 tick 上必须单调（否则 addLine 会连错）
        for line in layout.polylines:
            ticks = [node[0] for node in line]
            ascending = all(b >= a - 1e-9 for a, b in zip(ticks, ticks[1:]))
            descending = all(b <= a + 1e-9 for a, b in zip(ticks, ticks[1:]))
            self.assertTrue(ascending or descending)


class ChartBuildTest(unittest.TestCase):
    def test_export_and_reload(self):
        polylines = [
            [(8.0, -0.5, [255, 0, 0, 255]), (8.0, 0.5, [0, 0, 255, 255])],
            [(8.5, -0.5, [0, 255, 0, 255]), (9.5, 0.3, [0, 0, 0, 0])],
        ]
        chart = chartbuild.make_chart(175.0)
        self.assertEqual(len(chart["canvasMoves"]), 1)
        lines, nodes = chartbuild.add_polylines(chart, polylines)
        self.assertEqual((lines, nodes), (2, 4))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chart.json"
            chartbuild.export_chart(chart, path)
            self.assertTrue(path.exists())
            reloaded = chartbuild.load_chart(path)
            self.assertEqual(len(reloaded["lines"]), 2)
            rcf = chartbuild.load_library()
            self.assertIsNone(rcf.check(reloaded))
            # 判定环颜色应为全透明，避免画面上出现判定圈
            ring = reloaded["lines"][0]["judgeRingColor"][0]
            self.assertEqual(ring["startColor"]["a"], 0)

    def test_single_node_polyline_is_skipped(self):
        chart = chartbuild.make_chart(175.0)
        lines, nodes = chartbuild.add_polylines(
            chart, [[(8.0, 0.0, [255, 255, 255, 255])]]
        )
        self.assertEqual((lines, nodes), (0, 0))

    def test_canvas_index_validated(self):
        chart = chartbuild.make_chart(175.0)
        with self.assertRaises(chartbuild.ChartError):
            chartbuild.add_polylines(chart, [[(8.0, 0.0, [0, 0, 0, 255])] * 2],
                                     canvas_index=5)

    def test_append_to_existing_chart(self):
        first = [
            [(8.0, -0.2, [255, 255, 255, 255]), (9.0, 0.2, [255, 255, 255, 255])],
        ]
        chart = chartbuild.make_chart(175.0)
        chartbuild.add_polylines(chart, first)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            chartbuild.export_chart(chart, base)
            loaded = chartbuild.load_chart(base)
            chartbuild.add_polylines(loaded, first)
            self.assertEqual(len(loaded["lines"]), 2)
            self.assertEqual(len(loaded["canvasMoves"]), 1)

    def test_workshop_db_is_preserved(self):
        """编辑器的谱面带 __db（工坊信息），追加后要贴回去。"""
        chart = chartbuild.make_chart(175.0)
        chartbuild.add_polylines(chart, [
            [(8.0, -0.2, [255, 255, 255, 255]), (9.0, 0.2, [255, 255, 255, 255])],
        ])
        db = {"steam_workshop_title": "test", "steam_workshop_level": "17"}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            chartbuild.export_chart(chart, base, extra_db=db)
            self.assertEqual(json.loads(base.read_text(encoding="utf-8"))["__db"], db)
            self.assertEqual(chartbuild.read_workshop_db(base), db)
            # 重新读取再导出时依然保留
            loaded = chartbuild.load_chart(base)
            out = Path(tmp) / "out.json"
            chartbuild.export_chart(loaded, out,
                                    extra_db=chartbuild.read_workshop_db(base))
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["__db"], db)

    def test_read_workshop_db_without_field(self):
        chart = chartbuild.make_chart(175.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.json"
            chartbuild.export_chart(chart, path)
            self.assertIsNone(chartbuild.read_workshop_db(path))


class PreviewTest(unittest.TestCase):
    def test_render_writes_png(self):
        polylines = [
            [(8.0, -0.5, [255, 255, 255, 255]), (8.0, 0.5, [255, 255, 255, 255])],
            [(8.2, -0.5, [255, 255, 255, 255]), (8.2, 0.5, [255, 0, 0, 255])],
        ]
        chart = chartbuild.make_chart(175.0)
        chartbuild.add_polylines(chart, polylines)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preview.png"
            preview.render_chart(chart, out, max_width=200)
            self.assertTrue(out.exists())
            with Image.open(out) as image:
                self.assertLessEqual(image.width, 202)

    def test_canvas_x_at_interpolates(self):
        chart = chartbuild.make_chart(175.0)
        chart["canvasMoves"][0]["xPositionKeyPoints"] = [
            {"time": 0.0, "value": 0.0, "easeType": 0, "floorPosition": 0.0},
            {"time": 4.0, "value": 1.0, "easeType": 0, "floorPosition": 0.0},
        ]
        self.assertAlmostEqual(preview.canvas_x_at(chart, 0, 0.0), 0.0)
        self.assertAlmostEqual(preview.canvas_x_at(chart, 0, 2.0), 0.5)
        self.assertAlmostEqual(preview.canvas_x_at(chart, 0, 9.0), 1.0)

    # --- 预览只画“目标画布 + 这张图的拍数范围” -------------------------
    @staticmethod
    def _two_canvas_chart() -> dict:
        """0 号画布在 0~2 拍，1 号画布在 20~22 拍（离得很远）。"""
        chart = chartbuild.make_chart(175.0)
        rcf = chartbuild.load_library()
        if (rcf.getCanvasCount(chart) or 0) < 2:
            rcf.addCanvas(chart)
        ring = rcf.createRingPoint(0.0, [0, 0, 0, 0])
        for canvas, start in ((0, 0.0), (1, 20.0)):
            rcf.addLine(chart, [
                rcf.createLinePoint(start, -0.3, canvas, 0, "#FF0000"),
                rcf.createLinePoint(start + 2.0, 0.3, canvas, 0, "#00FF00"),
            ], ring)
        return chart

    def test_chart_segments_filter_by_canvas(self):
        chart = self._two_canvas_chart()
        both = preview.chart_segments(chart)
        first = preview.chart_segments(chart, canvas_index=0)
        second = preview.chart_segments(chart, canvas_index=1)
        self.assertGreater(len(both), len(first))
        self.assertGreater(len(both), len(second))
        # 每个画布只拿到自己那段（y 跨度约 2 拍 = 0.68 s，不会跨到另一段）
        for segments in (first, second):
            ys = [seg[1] for seg in segments]
            self.assertLess(max(ys) - min(ys), 3.0)

    def test_tick_range_crops_preview_to_image(self):
        chart = self._two_canvas_chart()
        whole = preview.render_chart_image(chart, max_width=400)
        cropped = preview.render_chart_image(
            chart, max_width=400, canvas_index=1, tick_range=(20.0, 22.0)
        )
        # 整张谱面跨越 0~22 拍，裁到 2 拍后高度应该小一个数量级
        self.assertLess(cropped.height, whole.height / 4)

    def test_view_width_keeps_horizontal_center_visible(self):
        """横向中心偏移在预览里要看得出来（内容不居中，但也不被裁掉）。"""
        image = Image.new("RGBA", (80, 60), (255, 255, 255, 255))
        ImageDraw.Draw(image).rectangle([30, 20, 50, 40], fill=(0, 0, 0, 255))

        def ink_center(center_x: float) -> float:
            result = pipeline.convert(
                image, pipeline.ConvertOptions(mode="scan", width_x=0.6,
                                               center_x=center_x)
            )
            rendered = preview.render_chart_image(
                result.chart, speed_const=result.speed_const,
                max_width=400, background=(255, 255, 255),
                canvas_index=result.canvas_index,
                tick_range=(result.cmap.start_tick, result.cmap.end_tick),
                view_width_world=5.625,
            )
            pixels = rendered.convert("RGB").load()
            total = sx = 0
            for y in range(rendered.height):
                for x in range(rendered.width):
                    r, g, b = pixels[x, y]
                    if r + g + b < 300:
                        total += 1
                        sx += x
            self.assertGreater(total, 0)
            return sx / total / rendered.width

        centered = ink_center(0.0)
        shifted = ink_center(0.25)
        self.assertAlmostEqual(centered, 0.5, delta=0.12)
        self.assertGreater(shifted, centered + 0.1)


class _AppendHelpers:
    """CLI 与 GUI 追加测试共用的构造/断言工具（非 TestCase）。"""

    @staticmethod
    def _base_chart(path: Path, *, bpm: float = 150.0, canvases: int = 2) -> Path:
        """造一个“已有谱面”：bpm + 恰好 ``canvases`` 个画布 + 0 号画布上一条红线。"""
        rcf = chartbuild.load_library()
        chart = rcf.new(bpm)
        ring = rcf.createRingPoint(0.0, [0, 0, 0, 0])
        rcf.addLine(chart, [
            rcf.createLinePoint(0.0, -0.4, 0, 0, "#FF0000"),
            rcf.createLinePoint(2.0, 0.4, 0, 0, "#00FF00"),
        ], ring)
        for _ in range(max(0, canvases - 1)):
            rcf.addCanvas(chart)
        chartbuild.export_chart(chart, path)
        return path

    @staticmethod
    def _canvas_ids(data: dict) -> set[int]:
        return {node["canvasIndex"]
                for line in data["lines"] for node in line["linePoints"]}

    def _run_cli(self, image_path: Path, output: Path, *extra: str) -> int:
        return main([str(image_path), "--mode", "outline", "--ink", "128",
                     "-o", str(output), "--quiet", *extra])


class AppendTest(_AppendHelpers, unittest.TestCase):
    """追加到已有谱面：新内容接在现有画布之后、原内容不动、导出写回所选谱面。"""

    def test_cli_appends_on_canvas_after_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            image_path = tmp / "in.png"
            _image_with_disc(48).convert("RGB").save(image_path)
            base = self._base_chart(tmp / "base.json", canvases=2)
            out = tmp / "out.json"

            self.assertEqual(self._run_cli(image_path, out, "--chart", str(base)), 0)
            data = json.loads(out.read_text(encoding="utf-8"))

            self.assertEqual(data["bPM"], 150.0)                 # 沿用原 BPM
            self.assertEqual(len(data["canvasMoves"]), 3)        # 0/1 原有 + 2 新增
            ids = self._canvas_ids(data)
            self.assertIn(0, ids)                                # 原内容还在 0 号
            self.assertIn(2, ids)                                # 新内容接在最后
            # 0 号画布上那条红线没被碰
            on_zero = [line for line in data["lines"]
                       if {n["canvasIndex"] for n in line["linePoints"]} == {0}]
            self.assertEqual(len(on_zero), 1)
            self.assertEqual(len(on_zero[0]["linePoints"]), 2)

    def test_cli_explicit_canvas_zero_overlaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            image_path = tmp / "in.png"
            _image_with_disc(48).convert("RGB").save(image_path)
            base = self._base_chart(tmp / "base.json", canvases=2)
            out = tmp / "out.json"

            code = self._run_cli(image_path, out, "--chart", str(base), "--canvas", "0")
            self.assertEqual(code, 0)
            data = json.loads(out.read_text(encoding="utf-8"))

            self.assertEqual(len(data["canvasMoves"]), 2)        # 没有新增画布
            self.assertEqual(self._canvas_ids(data), {0})        # 全部叠在 0 号

    def test_cli_new_chart_starts_at_canvas_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            image_path = tmp / "in.png"
            _image_with_disc(48).convert("RGB").save(image_path)
            out = tmp / "out.json"

            self.assertEqual(self._run_cli(image_path, out), 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(data["canvasMoves"]), 1)
            self.assertEqual(self._canvas_ids(data), {0})


# --- GUI：需要 PyQt5，没有就跳过 -----------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:                                                       # noqa: SIM105
    from PyQt5 import QtCore, QtWidgets

    _QT_IMPORT_ERROR: str | None = None
except Exception as exc:                                   # noqa: BLE001
    QtCore = QtWidgets = None                              # type: ignore[assignment]
    _QT_IMPORT_ERROR = str(exc)


_QT_APP = None


def _qt_app():
    global _QT_APP
    if QtWidgets is None:
        raise unittest.SkipTest(f"没有 PyQt5：{_QT_IMPORT_ERROR}")
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    _QT_APP = app              # 持有引用，别被 GC 掉
    return app


@unittest.skipIf(QtWidgets is None, "没有 PyQt5")
class GuiAppendTest(_AppendHelpers, unittest.TestCase):
    """GUI 层的追加：默认写回所选谱面、画布自动顺延、覆盖前要确认。"""

    def _window(self):
        _qt_app()
        from pic2chart import gui as pic_gui

        win = pic_gui.MainWindow()
        win._silent = True
        self.addCleanup(self._destroy_window, win)
        return win

    @staticmethod
    def _destroy_window(win) -> None:
        win._timer.stop()
        win._pool.clear()
        win._pool.waitForDone(20_000)
        win.close()

    @staticmethod
    def _wait(win, timeout_ms: int = 30_000) -> None:
        clock = QtCore.QElapsedTimer()
        clock.start()
        while win._result is None and not win._status.text().startswith("⚠"):
            _qt_app().processEvents(QtCore.QEventLoop.AllEvents, 50)
            if clock.elapsed() > timeout_ms:
                raise AssertionError(f"转换超时：{win._status.text()}")

    @staticmethod
    def _wait_idle(win, timeout_ms: int = 60_000) -> None:
        """等后台导出结束（导出是异步的）。"""
        clock = QtCore.QElapsedTimer()
        clock.start()
        while getattr(win, "_busy", False):
            _qt_app().processEvents(QtCore.QEventLoop.AllEvents, 50)
            if clock.elapsed() > timeout_ms:
                raise AssertionError(f"后台任务超时：{win.progress_label.text()}")
        for _ in range(5):
            _qt_app().processEvents()

    def _prepared_window(self, tmp: Path, base: Path, *, auto: bool = True):
        image_path = tmp / "in.png"
        _image_with_disc(48).convert("RGB").save(image_path)
        win = self._window()
        win.load_image(image_path)
        self._wait(win)
        win.append_edit.setText(str(base))
        win.append_check.setChecked(True)
        win.append_auto_check.setChecked(auto)
        win.canvas_spin.setValue(0)
        win._result = None
        win._start_convert()
        self._wait(win)
        return win

    def test_gui_default_export_path_is_the_appended_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base = self._base_chart(tmp / "base.json", canvases=1)
            win = self._window()
            win.append_edit.setText(str(base))
            win.append_check.setChecked(True)
            self.assertEqual(Path(win._default_export_path()), base)

            win.append_check.setChecked(False)
            win._image_path = tmp / "photo.png"
            self.assertEqual(win._default_export_path().name, "photo_chart.json")

    def test_gui_append_writes_into_new_canvas(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base = self._base_chart(tmp / "base.json", canvases=2)
            win = self._prepared_window(tmp, base)
            self.assertEqual(win._result.canvas_index, 2)     # 接在现有画布之后

            win._export(str(base))
            self._wait_idle(win)
            data = json.loads(base.read_text(encoding="utf-8"))
            self.assertEqual(data["bPM"], 150.0)
            self.assertEqual(len(data["canvasMoves"]), 3)
            ids = self._canvas_ids(data)
            self.assertIn(0, ids)
            self.assertIn(2, ids)

    def test_gui_append_auto_canvas_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base = self._base_chart(tmp / "base.json", canvases=2)
            win = self._prepared_window(tmp, base, auto=False)
            self.assertEqual(win._result.canvas_index, 0)     # 用“目标画布”= 0

            win._export(str(base))
            self._wait_idle(win)
            data = json.loads(base.read_text(encoding="utf-8"))
            self.assertEqual(len(data["canvasMoves"]), 2)     # 没新增画布
            self.assertEqual(self._canvas_ids(data), {0})

    def test_gui_overwrite_confirm_rejects_keeps_file(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base = self._base_chart(tmp / "base.json", canvases=2)
            before = base.read_bytes()
            win = self._window()
            win._silent = False                                # 让确认框真的被问到

            with mock.patch.object(QtWidgets.QMessageBox, "question",
                                   return_value=QtWidgets.QMessageBox.No):
                self.assertFalse(win._confirm_overwrite(base, append_mode=True))
                win._export(str(base))                         # 被拒绝 → 不写
            self.assertEqual(base.read_bytes(), before)

            with mock.patch.object(QtWidgets.QMessageBox, "question",
                                   return_value=QtWidgets.QMessageBox.Yes):
                self.assertTrue(win._confirm_overwrite(base, append_mode=True))

            win._silent = True
            self.assertTrue(win._confirm_overwrite(base, append_mode=True))


class CliTest(unittest.TestCase):
    def test_scan_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "in.png"
            _image_with_disc(48).convert("RGB").save(image_path)
            chart_path = Path(tmp) / "out.json"
            preview_path = Path(tmp) / "out.png"
            code = main([
                str(image_path), "--mode", "scan", "--cols", "24",
                "-o", str(chart_path), "--preview", str(preview_path),
                "--quiet",
            ])
            self.assertEqual(code, 0)
            self.assertTrue(chart_path.exists())
            self.assertTrue(preview_path.exists())
            data = json.loads(chart_path.read_text(encoding="utf-8"))
            self.assertEqual(data["bPM"], 175.0)
            self.assertGreater(len(data["lines"]), 0)

    def test_outline_end_to_end_with_ink(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "in.png"
            _image_with_disc(48).convert("RGB").save(image_path)
            chart_path = Path(tmp) / "out.json"
            code = main([
                str(image_path), "--mode", "outline", "--ink", "128",
                "--tolerance", "0.8", "-o", str(chart_path), "--quiet",
            ])
            self.assertEqual(code, 0)
            data = json.loads(chart_path.read_text(encoding="utf-8"))
            self.assertGreater(len(data["lines"]), 0)

    def test_calibrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart_path = Path(tmp) / "cal.json"
            code = main(["--calibrate", str(chart_path), "--quiet"])
            self.assertEqual(code, 0)
            data = json.loads(chart_path.read_text(encoding="utf-8"))
            self.assertGreater(len(data["lines"]), 5)

    def test_missing_image_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                code = main([str(Path(tmp) / "nope.png"),
                             "-o", str(Path(tmp) / "o.json"), "--quiet"])
            self.assertEqual(code, 2)

    def test_transparent_image_produces_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "empty.png"
            Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(image_path)
            with contextlib.redirect_stderr(io.StringIO()):
                code = main([
                    str(image_path), "--mode", "outline",
                    "-o", str(Path(tmp) / "o.json"), "--quiet",
                ])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
