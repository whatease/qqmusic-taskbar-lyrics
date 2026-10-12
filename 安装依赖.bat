@echo off
rem Install dependencies for QQMusic taskbar lyrics
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Retry with Tsinghua mirror...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo.
echo Done. You can now run: 启动QQ音乐底栏歌词.bat
pause
