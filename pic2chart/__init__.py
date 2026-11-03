"""pic2chart：把图片转成 Rizline 谱面的小工具包。

模块划分：

* ``mapping``：像素 ↔ (xPosition, tick) 的换算与比例常数
* ``imageproc``：读图、重采样、掩膜、取色
* ``modes``：``scan``（逐行扫描）与 ``outline``（轮廓描边）两种折线策略
* ``chartbuild``：调用 ``rizChartFunction`` 生成并导出谱面
* ``preview``：把谱面渲染成 PNG，先看效果再进编辑器
* ``cli``：命令行入口
"""

from . import chartbuild, cli, imageproc, mapping, modes, preview

__all__ = ["chartbuild", "cli", "imageproc", "mapping", "modes", "preview"]
