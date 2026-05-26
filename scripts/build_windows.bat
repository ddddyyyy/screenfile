@echo off
setlocal

set ROOT=%~dp0..
cd /d "%ROOT%"

if not exist ".venv" (
  py -3.10 -m venv .venv
)

set PYTHON_EXE=%ROOT%\.venv\Scripts\python.exe

"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" -m pip install -e ".[build]"
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" scripts\build_executable.py
if errorlevel 1 exit /b 1

echo.
echo Windows executable created at:
echo   %ROOT%\dist\screenfile.exe
