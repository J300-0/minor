@echo off
REM ============================================================
REM  setup.bat — Install dependencies for AI Paper Formatter
REM  Run this ONCE before using run.bat
REM ============================================================

setlocal

echo.
echo  [ai-paper-formatter] Setup
echo  ================================

REM ── Locate Python ───────────────────────────────────────────
set PYTHON=
if exist "%~dp0venv\Scripts\python.exe" (
    set PYTHON=%~dp0venv\Scripts\python.exe
    echo  Using venv: %PYTHON%
) else (
    where python >nul 2>&1 && set PYTHON=python
    if "%PYTHON%"=="" (
        where python3 >nul 2>&1 && set PYTHON=python3
    )
)

if "%PYTHON%"=="" (
    echo  Error: Python not found. Install Python 3.9+ from https://python.org
    exit /b 1
)

REM ── Install core dependencies ────────────────────────────────
echo.
echo  Installing core dependencies...
"%PYTHON%" -m pip install --upgrade pip
"%PYTHON%" -m pip install pymupdf pdfplumber python-docx jinja2

REM ── Optional: pix2tex (equation OCR) ────────────────────────
echo.
set /p INSTALL_PIX2TEX=Install pix2tex for equation OCR? [y/N]
if /i "%INSTALL_PIX2TEX%"=="y" (
    echo  Installing pix2tex (this downloads ~300 MB model on first run)...
    "%PYTHON%" -m pip install pix2tex
)

REM ── Check pdflatex ──────────────────────────────────────────
echo.
where pdflatex >nul 2>&1
if errorlevel 1 (
    echo  [!] pdflatex NOT found.
    echo      Install MiKTeX: https://miktex.org/download
    echo      or TeX Live:    https://tug.org/texlive/
) else (
    echo  [OK] pdflatex found.
)

echo.
echo  Setup complete. Run: run.bat input\paper.pdf
endlocal
