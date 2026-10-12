@echo off
rem Double-click to install shuto from this folder (Windows).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo.
    echo The install did not finish. Read the message above, then press any key to close.
) else (
    echo.
    echo Finished. Press any key to close this window.
)
pause >nul
