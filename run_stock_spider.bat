@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" stock_spider.py %*
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 stock_spider.py %*
    ) else (
        python stock_spider.py %*
    )
)
if errorlevel 1 pause
