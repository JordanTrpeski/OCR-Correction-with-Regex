@echo off
title RIA OCR Corrector
cd /d "%~dp0"

echo ============================================================
echo  RIA OCR Corrector
echo ============================================================
echo.

:: Check setup has been run
if not exist .env (
    echo  ERROR: Not set up yet.
    echo.
    echo  Please run setup.bat first.
    echo.
    goto :end
)

:: Check input folder exists
if not exist input\ (
    mkdir input
)

:: Count PDFs in input folder
set COUNT=0
for %%f in (input\*.pdf) do set /a COUNT+=1

if %COUNT%==0 (
    echo  Input folder is empty.
    echo.
    echo  Drop your PDF files into the input\ folder and try again.
    echo.
    echo  Location:
    echo  %~dp0input\
    echo.
    goto :end
)

echo  Found %COUNT% PDF(s) in input\ — starting...
echo.

:: Run the corrector
python ria.py
echo.

:end
echo ============================================================
echo  Press any key to close...
echo ============================================================
pause >nul
