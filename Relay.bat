@echo off
rem Opens the Relay app. The first run creates a virtual environment and installs the dependencies.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo First run: setting up Relay. This takes a minute.
  where py >nul 2>nul
  if errorlevel 1 ( set "PY=python" ) else ( set "PY=py -3" )
  !PY! -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Relay needs Python 3.11 or newer.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" in the installer.
    pause
    exit /b 1
  )
  !PY! -m venv .venv
  if errorlevel 1 ( echo Could not create the virtual environment. & pause & exit /b 1 )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Install failed. Scroll up for the error.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

if not exist ".env" copy ".env.example" ".env" >nul
start "" ".venv\Scripts\pythonw.exe" dashboard.py
