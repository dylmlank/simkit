@echo off
REM SimKit - run the standalone agent against the four engineering applications.
REM
REM   start_simkit.bat "Model a 50 mm sphere in FreeCAD and find its Cd at 15 m/s"
REM
REM With no argument it prints the connector status instead, which is the
REM quickest way to see what is installed and reachable.

cd /d "%~dp0"

set UV=C:\Users\%USERNAME%\.local\bin\uv.exe
if not exist "%UV%" set UV=uv

if "%~1"=="" (
    "%UV%" run --python 3.12 --with jpype1 python -c "import sys; sys.path.insert(0,'.'); from core.tools import survey; print(survey())"
) else (
    "%UV%" run --python 3.12 --with anthropic --with jpype1 python agent.py %*
)

pause
