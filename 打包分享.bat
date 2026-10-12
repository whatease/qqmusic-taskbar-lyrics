@echo off
rem 打包分享版 zip(自动排除日志/缓存/个人配置)
cd /d "%~dp0"
python tools\package_release.py
pause
