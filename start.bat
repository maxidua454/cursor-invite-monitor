@echo off
echo ========================================
echo  Cursor Invite Link Monitor v3
echo  SeleniumBase + Cloudflare Bypass
echo ========================================
echo.

:: Check deps
python -c "import seleniumbase" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    pip install seleniumbase colorama
)

echo Starting monitor... (Press Ctrl+C to stop)
echo.
python monitor.py
pause
