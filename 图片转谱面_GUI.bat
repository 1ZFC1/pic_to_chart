@echo off
chcp 65001 >nul
setlocal
rem 优先用 pythonw（不弹控制台）；没有就退回 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0pic_to_chart_gui.py" %*
) else (
    python "%~dp0pic_to_chart_gui.py" %*
)
