@echo off
title RIA OCR Corrector - Setup
echo ============================================================
echo  RIA OCR Corrector  ^|  Setup
echo ============================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found.
    echo  Download it from https://www.python.org/downloads/
    echo  Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo  Found: %%i
echo.

:: Install dependencies
echo  Installing dependencies...
echo.
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: pip install failed. See above for details.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  API Key Setup
echo ============================================================
echo.
echo  You need an API key to use this tool.
echo  Contact @JordanTrpeski on GitHub to request access.
echo.

:: Check if .env already has a key
if exist .env (
    findstr /C:"MISTRAL_API_KEY" .env >nul 2>&1
    if not errorlevel 1 (
        echo  API key already saved in .env
        goto :folders
    )
)

:: Prompt for key
set /p "APIKEY= Paste your Mistral API key and press Enter: "
if "%APIKEY%"=="" (
    echo  No key entered. You can add it later by editing the .env file.
) else (
    echo MISTRAL_API_KEY=%APIKEY%> .env
    echo  Key saved to .env
)

:folders
echo.
echo ============================================================
echo  Creating folders
echo ============================================================
echo.
if not exist input   mkdir input   && echo  Created: input\
if not exist output  mkdir output  && echo  Created: output\
if exist input  echo  OK: input\
if exist output echo  OK: output\

echo.
echo ============================================================
echo  Setup complete!
echo ============================================================
echo.
echo  HOW TO USE:
echo.
echo   GUI  (batch files, point-and-click):
echo        Double-click ocr_app.py  or  python ocr_app.py
echo.
echo   CLI  (with OCR correction):
echo        1. Drop your PDFs into the input\ folder
echo        2. Run:  python ria.py
echo        3. Pick up corrected PDFs from the output\ folder
echo.
pause
