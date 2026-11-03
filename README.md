# pic_to_chart

把**一张图片**转成 **Rizline 谱面**里的引导线（guideline）。

> 本仓库**只含源码与文档**：不含第三方库、不含美术/音乐素材、不含二进制或生成物。

![轮廓描边效果](docs/demo-outline.png)

![逐行扫描效果](docs/demo-scan.png)

## 功能

| 模式 | 适合 | 说明 |
| --- | --- | --- |
| `scan` 逐行扫描 | 照片、插画、渐变 | 每行一条水平引导线，颜色取像素颜色 |
| `column` 逐列扫描 | 同上，但偏好竖线 | 每列一条竖线，列距按编辑器水平细分 1/16 |
| `outline` 轮廓描边 | 线稿、logo、剪影、透明底贴图 | marching squares 提轮廓，可选平滑与加粗 |
| `thin` 骨架中心线 | \ | \ |

此外：

- **GUI（PyQt5）**：实时预览、拖拽载入、后台转换 + 进度条 + 取消、追加到已有谱面；
- **标定谱面**：一键生成用于核对比例/线宽/流速的测试谱面；\

## 安装

需要 **Python 3.10+**（Windows 上开发与验证）。

```powershell
git clone https://github.com/1ZFC1/pic_to_chart.git
cd pic_to_chart
python -m pip install -r requirements.txt
```

### 依赖：rizChartFunction_Library（不在本仓库）

谱面的读写（JSON 结构、`floorPosition`、画布/关键点 API）依赖另一个项目
**[rizChartFunction_Library](https://github.com/xp00000002/rizChartFunction_Library)**。

两种获取方式，任选一种：

```powershell
# 方式 A：克隆到工具旁边的目录（脚本会放好位置），工具会自动找到
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-library.ps1

# 方式 B：克隆到任意位置，用环境变量指定 src 目录
git clone --depth 1 https://github.com/xp00000002/rizChartFunction_Library.git D:\libs\rizChartFunction_Library
$env:RIZCHARTFUNCTION_SRC = 'D:\libs\rizChartFunction_Library\src'
```

查找顺序：`RIZCHARTFUNCTION_SRC` → 工具同级的 `rizChartFunction_Library\src` → 上一级目录 →
当前目录；找不到时会在启动时报错并提示。

## 运行

```powershell
# 图形界面（推荐）
python pic_to_chart_gui.py
# 也可以直接双击 图片转谱面_GUI.bat

# 命令行：一张图 → 一份谱面 JSON
python pic_to_chart.py 图片.png -o chart.json --preview chart.png --compare compare.png

# 追加到已有谱面（默认接在现有画布之后，不覆盖原内容）
python pic_to_chart.py 图片.png --chart 已有谱面.json -o 合并.json
```

常用参数：`--mode scan|column|outline|thin`、`--bpm`、`--start-beat`、`--width-x`、
`--screens`、`--center-x`、`--flow`、`--smooth`（轮廓平滑）、`--canvas`（目标画布）。
完整参数、坐标换算、标定方法与常见问题见 **[docs/manual.md](docs/manual.md)**。

## 在编辑器里使用

1. 用 Rizline 编辑器打开生成的 JSON（或把它放进谱面目录）；
2. 跳到 `--start-beat` 前后播放，图片会自下而上出现；
3. 生成的谱面**只有引导线**，没有 Note / Riztime / 主题配色——这些在编辑器里继续做。

## 已知限制

- 输出只有引导线，不含判定环/Note 等游戏元素。

## 许可

本仓库的**代码与文档**采用 [MIT License](LICENSE)。

第三方声明：

- `rizChartFunction_Library`（谱面读写库）不属于本仓库，版权归其作者，按其自身许可使用；
- `docs/demo-*.png` 由本工具在**纯合成图形**上生成，不含第三方素材。

## 仓库内容

```text
pic2chart/            核心包（换算、掩码、四种模式、流水线、谱面写入、预览）
pic_to_chart.py       命令行入口
pic_to_chart_gui.py   GUI 入口
*.bat                 Windows 快捷方式
tests/                单元测试（pytest 风格之外，用标准库 unittest）
tools/                辅助脚本（取依赖 / 发布镜像）
docs/manual.md        完整使用手册（开发仓库 README 的镜像）
requirements.txt      Python 依赖
```
