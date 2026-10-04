@echo off
setlocal

rem ===========================================================================
rem Designer.bat -- open Qt Designer on a screen layout (.ui file).
rem
rem   Designer.bat                     opens Designer with no file
rem   Designer.bat FtpSetupDialog.ui   opens that file from frontEnd\qt\ui
rem   Designer.bat path\to\other.ui    opens any path you give it
rem
rem Saving in Designer is all that is needed: the .ui files are parsed at run
rem time, so just reopen the screen in the app to see the change.  There is no
rem build or code-generation step.
rem ===========================================================================

set "CURRENT_DIR=%~dp0"
if "%CURRENT_DIR:~-1%"=="\" set "CURRENT_DIR=%CURRENT_DIR:~0,-1%"
set "DESIGNER=%CURRENT_DIR%\venv\Scripts\pyside6-designer.exe"

if not exist "%DESIGNER%" (
    echo [Designer] ERROR: PySide6 is not installed in "%CURRENT_DIR%\venv".
    echo [Designer] Install it with:
    echo [Designer]     venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

if "%~1"=="" (
    start "" "%DESIGNER%"
    exit /b 0
)

rem A bare file name means "the one in frontEnd\qt\ui"; anything else is used
rem as given.
set "TARGET=%~1"
if exist "%CURRENT_DIR%\frontEnd\qt\ui\%~1" set "TARGET=%CURRENT_DIR%\frontEnd\qt\ui\%~1"

if not exist "%TARGET%" (
    echo [Designer] ERROR: no such file: %~1
    echo [Designer] Layouts live in frontEnd\qt\ui:
    dir /b "%CURRENT_DIR%\frontEnd\qt\ui\*.ui"
    exit /b 1
)

start "" "%DESIGNER%" "%TARGET%"
