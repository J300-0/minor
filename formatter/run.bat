@echo off
REM ============================================================
REM  run.bat — AI Paper Formatter (Windows)
REM  Usage:
REM    run.bat input\paper.pdf
REM    run.bat input\paper.pdf ieee
REM    run.bat input\paper.pdf acm
REM    run.bat input\paper.docx springer
REM ============================================================

setlocal

REM ── Defaults ────────────────────────────────────────────────
set INPUT=%~1
set TEMPLATE=%~2
if "%TEMPLATE%"=="" set TEMPLATE=ieee

REM ── Validation ──────────────────────────────────────────────
if "%INPUT%"=="" (
    echo.
    echo  Usage: run.bat input\paper.pdf [template]
    echo  Templates: ieee  acm  springer  elsevier  apa  arxiv
    echo.
    exit /b 1
)

if not exist "%INPUT%" (
    echo  Error: Input file not found: %INPUT%
    exit /b 1
)

REM ── Locate Python (venv first, then system) ─────────────────
set PYTHON=
if exist "%~dp0venv\Scripts\python.exe" (
    set PYTHON=%~dp0venv\Scripts\python.exe
) else (
    where python >nul 2>&1 && set PYTHON=python
)
if "%PYTHON%"=="" (
    where python3 >nul 2>&1 && set PYTHON=python3
)
if "%PYTHON%"=="" (
    echo  Error: Python not found. Install Python 3.9+ or activate your venv.
    exit /b 1
)

REM ── Check pdflatex ──────────────────────────────────────────
where pdflatex >nul 2>&1
if errorlevel 1 (
    echo  Warning: pdflatex not found on PATH.
    echo  Install MiKTeX from https://miktex.org/download
    echo  or TeX Live from https://tug.org/texlive/
    echo.
)

REM ── Run pipeline ────────────────────────────────────────────
echo.
echo  [ai-paper-formatter]
echo  Input    : %INPUT%
echo  Template : %TEMPLATE%
echo  Python   : %PYTHON%
echo.

"%PYTHON%" "%~dp0main.py" "%INPUT%" --template "%TEMPLATE%"

if errorlevel 1 (
    echo.
    echo  Pipeline failed. Check logs\pipeline_latest.log for details.
    exit /b 1
)

echo.
echo  Output saved to: output\generated_%TEMPLATE%.pdf
endlocal
