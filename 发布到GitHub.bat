@echo off
rem 推送更新到 GitHub(git 不通时自动改走 API)
cd /d "%~dp0"
python tools\push_any.py
pause
