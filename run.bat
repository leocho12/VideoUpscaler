@echo off
rem DVD Upscaler launcher (double-click to start)
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%~dp0dvd_upscaler.py"
) else (
    py "%~dp0dvd_upscaler.py"
)
