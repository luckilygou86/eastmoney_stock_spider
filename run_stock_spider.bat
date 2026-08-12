@echo off
cd /d "%~dp0"
python stock_spider.py
if errorlevel 1 pause
