"""把折线写进 Rizline 谱面（借 ``rizChartFunction``）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

TRANSPARENT = [0, 0, 0, 0]


class ChartError(RuntimeError):
    """谱面生成失败。"""


def find_library_src() -> Path:
    """定位 ``rizChartFunction_Library/src``。

    依次尝试：环境变量 ``RIZCHARTFUNCTION_SRC``、打包后的 ``_MEIPASS``、
    与本文件同级的仓库副本、上一级目录里的仓库副本。
    """
    candidates = []
    env = Path(__import__("os").environ.get("RIZCHARTFUNCTION_SRC", ""))
    if str(env):
        candidates.append(env)
    # 打包成 exe 之后，源码位置是临时解包目录；库会作为数据带在它旁边
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "rizChartFunction_Library" / "src")
        candidates.append(Path(bundle) / "rizChartFunction" / "src")
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "rizChartFunction_Library" / "src")
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "rizChartFunction_Library" / "src")
    candidates.append(here.parents[3] / "rizChartFunction_Library" / "src")
    candidates.append(Path.cwd() / "rizChartFunction_Library" / "src")

    for candidate in candidates:
        if (candidate / "rizChartFunction" / "__init__.py").exists():
            return candidate
    raise ChartError(
        "找不到 rizChartFunction 库；请先克隆仓库，"
        "或用环境变量 RIZCHARTFUNCTION_SRC 指定 src 目录。"
    )


def load_library() -> ModuleType:
    """导入 ``rizChartFunction``。

    打包后库被直接编译进 exe（PyInstaller 的 hidden import），所以先试直接
    ``import``；只有在开发环境里才需要把 ``src`` 目录塞进 ``sys.path``。
    """
    try:
        import rizChartFunction  # noqa: WPS433  (延迟导入，避免无库时直接崩溃)

        return rizChartFunction
    except ImportError:
        pass

    src = find_library_src()
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import rizChartFunction  # noqa: WPS433

    return rizChartFunction


def make_chart(basebpm: float = 175.0) -> dict:
    """新建一份谱面，并保证至少有 1 个画布。

    ``new()`` 默认已经带一个 index=0 的画布，这里只在缺失时补一个，
    避免出现两个画布（多出来的空画布会让追加逻辑和编辑器显示都变乱）。
    """
    rcf = load_library()
    chart = rcf.new(float(basebpm))
    return ensure_canvas(chart)


def ensure_canvas(chart: dict) -> dict:
    """保证谱面至少有一个画布（已有时不动）。"""
    rcf = load_library()
    if not rcf.getCanvasCount(chart):
        if rcf.addCanvas(chart) is None:
            raise ChartError("addCanvas() 失败")
    return chart


def load_chart(path: str | Path) -> dict:
    """读取并清洗已有谱面。"""
    rcf = load_library()
    chart = rcf.load(str(path))
    if chart is None:
        raise ChartError(f"无法读取谱面：{path}")
    return chart


def read_workshop_db(path: str | Path) -> dict | None:
    """取出 ``__db``（Steam 创意工坊元数据）。

    ``rizChartFunction.load()`` 会把标准格式之外的键清掉，``export()`` 也拒绝
    写回 ``__db``；但编辑器自己保存的谱面是带这个键的，追加内容时最好保留，
    否则工坊信息会丢。这里把原始 ``__db`` 记下来，导出后再贴回去。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            raw = json.load(file)
    except (OSError, ValueError):
        return None
    value = raw.get("__db")
    return value if isinstance(value, dict) else None


def ensure_canvas_index(chart: dict, index: int, *, extend: bool = False) -> int:
    """校验画布索引；``extend=True`` 时按需新增画布，返回可用的非负索引。

    负数按“从末尾数”解析（``-1`` = 最后一个画布）。新画布由库的
    ``addCanvas()`` 追加，默认 x 位置 0、流速 1，不会影响已有内容。
    """
    rcf = load_library()
    count = rcf.getCanvasCount(chart) or 0
    if index < 0:
        index += count
        if index < 0:
            raise ChartError(f"画布索引太小：负数索引超出范围（共 {count} 个画布）")
    if index < count:
        return index
    if not extend:
        raise ChartError(f"画布索引越界：{index}（共 {count} 个画布）")
    while (rcf.getCanvasCount(chart) or 0) <= index:
        if rcf.addCanvas(chart) is None:
            raise ChartError("addCanvas() 失败：无法新增画布")
    return index


def add_polylines(
    chart: dict,
    polylines,
    *,
    canvas_index: int = 0,
    ring_color: list[int] | None = None,
    extend_canvas: bool = False,
    on_progress=None,
    cancel=None,
) -> tuple[int, int]:
    """把折线逐条写成引导线，返回 ``(线数, 节点数)``。

    每条引导线的判定环颜色节点统一为全透明（与 ``drawPolygon`` / ``drawText``
    的做法一致），避免画面上出现判定圈。``extend_canvas=True`` 时目标画布
    不存在会自动补齐，否则抛 :class:`ChartError`。

    ``on_progress(done, total)`` 用于进度显示；``cancel``（threading.Event）
    置位后抛 :class:`pipeline.ConvertCancelled`。
    """
    from .pipeline import ConvertCancelled

    rcf = load_library()
    canvas_index = ensure_canvas_index(chart, canvas_index, extend=extend_canvas)

    ring = rcf.createRingPoint(0.0, ring_color or TRANSPARENT)
    if ring is None:
        raise ChartError("createRingPoint() 失败")

    total = len(polylines) if hasattr(polylines, "__len__") else 0
    step = max(1, total // 100) if total else 1
    line_count = 0
    node_count = 0
    for index, polyline in enumerate(polylines):
        if cancel is not None and cancel.is_set():
            raise ConvertCancelled("转换已取消")
        points = []
        for tick, x, color in polyline:
            if tick < 0:
                raise ChartError(f"第 {index} 条线出现负时间 {tick}")
            point = rcf.createLinePoint(float(tick), float(x), canvas_index, 0, color)
            if point is None:
                raise ChartError(f"第 {index} 条线的节点非法：time={tick}, x={x}, color={color}")
            points.append(point)
        if len(points) < 2:
            continue
        if rcf.addLine(chart, points, ring) is None:
            raise ChartError(f"第 {index} 条线写入失败")
        line_count += 1
        node_count += len(points)
        if on_progress is not None and total and (index % step == 0 or index + 1 == total):
            on_progress(index + 1, total)
    if on_progress is not None and total:
        on_progress(total, total)
    return line_count, node_count


def export_chart(chart: dict, path: str | Path, extra_db: dict | None = None) -> None:
    """严格校验后导出；失败时抛出带诊断信息的异常。

    ``extra_db`` 会在校验通过后作为 ``__db`` 写回文件（保留工坊元数据）。
    """
    rcf = load_library()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rcf.export(chart, str(path)):
        issues = rcf.check(chart)
        raise ChartError(f"谱面校验未通过：{issues}")
    if not path.exists():
        raise ChartError(f"导出失败：{path}")

    if extra_db is not None:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        data["__db"] = extra_db
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))


def chart_stats(chart: dict) -> dict:
    """汇总谱面规模，供命令行输出。"""
    lines = chart.get("lines", [])
    return {
        "bpm": chart.get("bPM"),
        "canvases": len(chart.get("canvasMoves", [])),
        "lines": len(lines),
        "nodes": sum(len(line.get("linePoints", [])) for line in lines),
        "notes": sum(len(line.get("notes", [])) for line in lines),
    }
