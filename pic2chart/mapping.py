"""像素坐标 → 谱面坐标（xPosition / tick）的映射。

这里只处理“图片平面”和“谱面绘图平面”之间的换算，不涉及画布、floorPosition
和编辑器的内部状态；那些交给 ``rizChartFunction`` 和编辑器。

坐标事实（来自 ``EDITOR_AXES_REFERENCE.md``）：

* 世界 x = xPosition × 5.625 + 画布 frame；线节点实测斜率约 **5.625**/x 单位。
* 纵向：编辑器把 1 秒映射为 ``project.speed`` 个世界单位（默认流速 6.5），
  而画布流速（speedKeyPoints）对时间做加权积分；本工具生成的谱面画布流速固定为 1，
  所以 1 tick 的视觉高度 = ``60 / BPM × speed_const`` 个世界单位。
* 因此“1 个 xPosition 单位”和“1 tick”的视觉比例为：

      ticks_per_x = 5.625 / (60 / BPM × speed_const)  =  5.625 × BPM / (60 × speed_const)

  注意 ``rizChartFunction`` 文档里的 ``timemultiple=1.65`` 只是该作者在某个 BPM
  下量出的单点经验值（相当于 BPM ≈ 114），并不能直接套到别的 BPM；所以这里
  按 BPM 现算，同时允许用参数覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 线节点实测斜率：世界单位 / xPosition 单位
WORLD_X_PER_X_POSITION = 5.625

#: 编辑器设置里的“播放流速” S0 与**游戏真实流速** S1 的换算（摄像机缩放 = 1 时）：
#:
#:     S1 = 7 × (S0 - 1) / 9 + 6      (= (7·S0 + 47) / 9)
#:
#: 世界纵向坐标就是 ``S1 × floorPosition``（floorPosition = ∫画布流速 d(秒)），
#: 所以每 tick 的世界高度 = ``S1 × 60/BPM × 画布流速``、一屏拍数 = ``H / 每 tick 高度``。
#:
#: 交叉验证：S0 = 1.5 → S1 = 57.5/9 = **6.389**，而截图三处独立测量（红框、
#: 绿方括号宽度/高度、方括号贴合回归）都落在 6.37–6.40；早先按“数白线”读到的
#: 4.0 / 3.4 / 3.0 与此公式的预测（3.15 / 2.66 / 2.12）相差恒定的约 +0.8 根，
#: 是计数时多算了一根（多半是半拍彩线或边缘线）。公式与实测一致。
FLOW_REFERENCE = 1.5
FLOW_SLOPE = 7.0 / 9.0
FLOW_OFFSET = 47.0 / 9.0          # 6 - 7/9

#: 默认流速常数：S0 = 1.5 时的 S1（6.3889）。
FLOW_REFERENCE_SPEED = FLOW_SLOPE * FLOW_REFERENCE + FLOW_OFFSET
DEFAULT_SPEED_CONST = FLOW_REFERENCE_SPEED

#: 编辑器“整体缩放 100%”时，一屏（实际可见区域，含渐隐）的尺寸——**已定稿**。
#:
#: 由编辑器截图逐像素量得（绿色方括号 0.72 单位 × 3.05 tick 做标尺，98 px/世界）：
#: 可见 5.57 世界宽（= 0.99 个 x 单位）× 6.93 世界高（175 BPM、V=6.4 时约 3.17 拍）。
#: 回归编辑器复核：按 1.0 / 6.9 画出的绿色方括号“基本贴合”上下边缘（<1%）。
#: 可见区是“竖长”的（宽高比 ≈ 0.80），不是 16:9——Rizline 是竖屏游戏，
#: 编辑器露出的就是竖版 playfield。
DEFAULT_WIDTH_X = 1.0

#: 一屏（可见区）对应的世界高度：6.9
DEFAULT_VIEW_HEIGHT_WORLD = 6.9

#: 可见区宽高比（宽 / 高）≈ 0.83
VIEW_ASPECT = DEFAULT_WIDTH_X * 5.625 / DEFAULT_VIEW_HEIGHT_WORLD

#: 兼容旧名字
SCREEN_ASPECT = VIEW_ASPECT

#: 线条开始淡出的 |xPosition|（可见区半宽 0.5，外侧 0.4–0.5 渐隐）
FADE_START_X = 0.4

#: 线条完全不可见的 |xPosition|（= 可见区左右边缘）
FADE_END_X = 0.5

#: 编辑器里引导线的视觉线宽（世界单位）。
#:
#: 实测（标定谱面的橙色线宽梯）：间距 0.04 世界单位时两条线刚好贴合，
#: 0.02 / 0.03 已经糊成一条，0.05 还能分出缝。scan 模式默认让行距等于这个值，
#: 这样图片在编辑器里是连续的、没有横条纹。
DEFAULT_LINE_WIDTH_WORLD = 0.04

#: 谱面横向基础格距（局部坐标，世界 0.5625）。
BASE_GRID_X = 0.1

#: 逐列扫描的列间距细分数：实测编辑器里水平细分到 **1/16** 时，相邻两条竖线
#: 恰好紧贴（0.1 / 16 = 0.00625 个 xPosition 单位 = 0.035 世界单位，
#: 和 0.04 的线宽基本相等）。所以列模式的默认列距就用它。
COLUMN_SUBDIVISION = 16
COLUMN_STEP_X = BASE_GRID_X / COLUMN_SUBDIVISION      # 0.00625


def speed_const_from_flow(flow: float) -> float:
    """把编辑器设置里的“播放流速” S0 换算成游戏真实流速 S1（世界单位/秒）。

    ``S1 = 7·(S0 - 1)/9 + 6``，Camera 缩放 1 时生效。S0 = 1.5 → 6.389。

    这个 S1 就是纵向比例：``世界 y = S1 × floorPosition``，于是
    ``每 tick 世界高度 = S1 × 60/BPM × 画布流速``。
    """
    if flow <= 0:
        raise ValueError("流速必须大于 0")
    return FLOW_SLOPE * float(flow) + FLOW_OFFSET


def flow_from_speed_const(speed: float) -> float:
    """反解：已知 S1 求编辑器里该填的 S0（``S0 = (S1 - 47/9) × 9/7``）。"""
    return (float(speed) - FLOW_OFFSET) / FLOW_SLOPE


def ticks_per_x(bpm: float, speed_const: float = DEFAULT_SPEED_CONST,
                canvas_speed: float = 1.0) -> float:
    """计算“1 个 xPosition 单位 = 多少 tick”的视觉比例。"""
    if bpm <= 0:
        raise ValueError("bpm 必须大于 0")
    if speed_const <= 0:
        raise ValueError("speed_const 必须大于 0")
    if canvas_speed <= 0:
        raise ValueError("canvas_speed 必须大于 0")
    world_per_tick = (60.0 / bpm) * speed_const * canvas_speed
    return WORLD_X_PER_X_POSITION / world_per_tick


def world_per_tick(bpm: float, speed_const: float = DEFAULT_SPEED_CONST,
                   canvas_speed: float = 1.0) -> float:
    """1 tick 对应的世界高度。"""
    return (60.0 / bpm) * speed_const * canvas_speed


def view_height_world(width_x: float = DEFAULT_WIDTH_X) -> float:
    """一屏（16:9）对应的世界高度。"""
    return width_x * WORLD_X_PER_X_POSITION / SCREEN_ASPECT


def visible_height_ticks(bpm: float, speed_const: float = DEFAULT_SPEED_CONST,
                         view_height_world_value: float = DEFAULT_VIEW_HEIGHT_WORLD) -> float:
    """编辑器一屏能放下多少 tick（拍）。

    ``一屏世界高度 / 每 tick 世界高度``，而每 tick 世界高度与 BPM 成反比，
    所以**一屏拍数与 BPM 成正比、与流速成反比**：

        一屏拍数 = H · BPM / (60 · 流速 · 画布流速)

    这也是为什么高 BPM 的歌一屏能塞下更多拍。
    """
    return view_height_world_value / world_per_tick(bpm, speed_const)


def view_height_from_ticks(visible_beats: float, bpm: float,
                           speed_const: float = DEFAULT_SPEED_CONST) -> float:
    """由“一屏能看到多少拍”反推一屏世界高度（用于把标定结果固定下来）。"""
    if visible_beats <= 0:
        raise ValueError("一屏拍数必须大于 0")
    return float(visible_beats) * world_per_tick(bpm, speed_const)


def fit_width_x(
    image_width: int,
    image_height: int,
    *,
    bpm: float,
    ticks_per_x: float,
    max_width_x: float = DEFAULT_WIDTH_X,
    speed_const: float = DEFAULT_SPEED_CONST,
    view_height_world_value: float = DEFAULT_VIEW_HEIGHT_WORLD,
    screens: float = 1.0,
) -> float:
    """求“竖着占 ``screens`` 屏、且不超过 ``max_width_x``”的图片宽度。

    ``screens=1`` 就是“一屏看全”；``screens=2`` 表示允许两屏高（滚动揭幕）。
    """
    if image_width <= 0 or image_height <= 0:
        raise ValueError("图片尺寸必须为正")
    if screens <= 0:
        raise ValueError("screens 必须大于 0")
    limit_ticks = visible_height_ticks(bpm, speed_const, view_height_world_value) * screens
    aspect_hw = image_height / image_width
    by_height = limit_ticks / (aspect_hw * ticks_per_x)
    return max(0.05, min(max_width_x, by_height))


@dataclass
class PictureMap:
    """把图片像素映射到谱面 (xPosition, tick) 平面的仿射映射。

    映射是各向同性的：横向 ``dx`` 个 x 单位/像素，纵向 ``dx * ticks_per_x``
    个 tick/像素，所以图片不会被拉长或压扁。
    """

    width_px: int
    height_px: int
    width_x: float
    center_x: float
    start_tick: float
    ticks_per_x: float

    def __post_init__(self) -> None:
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("图片尺寸必须为正")
        if self.width_x <= 0:
            raise ValueError("width_x 必须大于 0")
        if self.ticks_per_x <= 0:
            raise ValueError("ticks_per_x 必须大于 0")

    # ------------------------------------------------------------------ 尺寸
    @property
    def dx(self) -> float:
        """横向：每个像素对应的 xPosition 单位。"""
        return self.width_x / self.width_px

    @property
    def dy(self) -> float:
        """纵向：每个像素对应的 tick。"""
        return self.dx * self.ticks_per_x

    @property
    def height_ticks(self) -> float:
        """整张图片占用的 tick 高度。"""
        return self.height_px * self.dy

    @property
    def left_x(self) -> float:
        return self.center_x - self.width_x / 2.0

    @property
    def right_x(self) -> float:
        return self.center_x + self.width_x / 2.0

    @property
    def end_tick(self) -> float:
        return self.start_tick + self.height_ticks

    # ------------------------------------------------------------------ 换算
    def x_of_px(self, px: float) -> float:
        """像素列中心（可带小数）→ xPosition。"""
        return self.center_x + (px - self.width_px / 2.0) * self.dx

    def px_of_x(self, x: float) -> float:
        return (x - self.center_x) / self.dx + self.width_px / 2.0

    def tick_of_row(self, row: float) -> float:
        """像素行（0 = 图片顶部，带小数）→ tick。图片顶部对应较大的 tick。"""
        return self.start_tick + (self.height_px - row) * self.dy

    def row_of_tick(self, tick: float) -> float:
        return self.height_px - (tick - self.start_tick) / self.dy

    def x_of_col_sample(self, index: int, count: int) -> float:
        """把图片横向切成 ``count`` 份时，第 ``index`` 份的中心 x。"""
        if count <= 0:
            raise ValueError("count 必须大于 0")
        return self.x_of_px((index + 0.5) * self.width_px / count)

    def tick_of_row_sample(self, index: int, count: int) -> float:
        """把图片纵向切成 ``count`` 份时，第 ``index`` 份（从顶部数）的中心 tick。"""
        if count <= 0:
            raise ValueError("count 必须大于 0")
        return self.tick_of_row((index + 0.5) * self.height_px / count)
