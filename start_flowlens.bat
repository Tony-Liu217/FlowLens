@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "FLOWLENS_PYTHON=%~dp0runtime\python.exe"
if exist "%FLOWLENS_PYTHON%" goto check
set "FLOWLENS_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%FLOWLENS_PYTHON%" goto check
where python >nul 2>nul
if errorlevel 1 goto missing
set "FLOWLENS_PYTHON=python"
:check
"%FLOWLENS_PYTHON%" -B -X utf8 scripts\check_runtime.py
if errorlevel 1 goto failed
if not "%~1"=="" goto arguments
"%FLOWLENS_PYTHON%" -B -X utf8 backend\run_app.py --open-browser
if errorlevel 1 goto failed
exit /b 0
:arguments
"%FLOWLENS_PYTHON%" -B -X utf8 backend\run_app.py %*
exit /b %errorlevel%
:missing
echo 缺少 Python 运行环境，请按 README 创建 .venv 并安装依赖。
goto failed
:failed
echo.
echo 启动未完成。请查看上方提示；已有账本不会被清空。
pause
exit /b 1
