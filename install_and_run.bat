@echo off
rem First run: creates a private Python environment next to this file, then starts the app.
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || (echo Python 3.10+ not found. Install it from python.org first. & pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw run_em31mapper.py %*
