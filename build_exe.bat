@echo off
rem Builds dist\EM31Mapper\EM31Mapper.exe — a folder you copy to the Mesa2 (no Python needed there).
rem Needs Python 3.10-3.12 from python.org on THIS build PC only.
setlocal
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
    echo.
    echo Python was not found on this PC. Install Python 3.12 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH", then run this again.
    echo ^(Or use the prebuilt EM31Mapper folder - no build needed.^)
    pause & exit /b 1
)
if not exist .venv (%PY% -m venv .venv || goto :fail)
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller || goto :fail
.venv\Scripts\pyinstaller --noconfirm EM31Mapper.spec || goto :fail
.venv\Scripts\python -c "print('Running self-test...')"
start "" /wait dist\EM31Mapper\EM31Mapper.exe --selftest --quiet
if errorlevel 1 (echo SELF-TEST FAILED - see Documents\EM31Mapper\selftest.txt & goto :fail)
echo.
echo Done: dist\EM31Mapper\EM31Mapper.exe   (copy the whole dist\EM31Mapper folder)
pause & exit /b 0
:fail
echo.
echo BUILD FAILED - see the messages above.
pause & exit /b 1
