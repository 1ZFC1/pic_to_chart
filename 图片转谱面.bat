@echo off
chcp 65001 >nul
setlocal
if "%~1"=="" (
    echo 用法：把图片直接拖到这个 bat 上，或者
    echo   python pic_to_chart.py 图片.png [参数]
    echo 参数说明见同目录 README.md
    echo.
    pause
    exit /b 1
)

set "IMG=%~1"
set "OUT=%~dpn1_chart.json"
set "PREVIEW=%~dpn1_preview.png"
set "COMPARE=%~dpn1_compare.png"

python "%~dp0pic_to_chart.py" "%IMG%" -o "%OUT%" --preview "%PREVIEW%" --compare "%COMPARE%"
if errorlevel 1 (
    echo.
    echo 转换失败，见上面的错误信息。
    pause
    exit /b 1
)

echo.
echo 谱面：%OUT%
start "" "%PREVIEW%"
start "" "%COMPARE%"
pause
