@echo off
rem 一键发布到 GitHub(需先完成 gh 登录,脚本会引导)
cd /d "%~dp0"
python tools\publish_github.py %*
pause
