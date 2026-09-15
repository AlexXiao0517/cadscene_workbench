@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0hotfix\apply-hotfix.ps1"
if errorlevel 1 pause
if errorlevel 1 exit /b 1
echo.
echo 修复完成，可以关闭此窗口。
pause
