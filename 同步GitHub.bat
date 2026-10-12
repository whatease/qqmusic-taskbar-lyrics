@echo off
rem 与 GitHub 远端对齐本地历史(网络恢复后运行一次即可)
cd /d "%~dp0"
echo 正在连接 GitHub ...
for /L %%i in (1,1,10) do (
    git fetch origin main 2>nul && goto ok
    echo 第 %%i 次失败,3秒后重试...
    timeout /t 3 >nul
)
echo 网络仍不可用,请稍后重试。
pause
exit /b 1
:ok
git reset --hard origin/main
echo.
echo 本地已与 GitHub 完全对齐。
pause
