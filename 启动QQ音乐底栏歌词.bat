@echo off
rem Start QQMusic taskbar lyrics (hidden console)
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw taskbar_lyrics.py
) else (
    start "" python taskbar_lyrics.py
)
