@echo off
chcp 65001 >nul
setlocal
python "%~dp0pic_to_chart.py" --calibrate "%~dp0calibrate.json" --preview "%~dp0calibrate_preview.png"
if errorlevel 1 (
    echo.
    echo 生成失败，见上面的错误信息。
    pause
    exit /b 1
)
echo.
echo 标定谱面：%~dp0calibrate.json
start "" "%~dp0calibrate_preview.png"
pause
