@echo off
setlocal enabledelayedexpansion

rem ===========================================================================
rem StartFrontend.bat -- run ONLY the front end (the UI).
rem
rem Companion to StartBackend.bat.  The difference from Start.bat that matters:
rem this does NOT sweep processes.  Start.bat kills every python.exe whose
rem command line mentions this repo before it launches, which is right when it
rem owns both halves, but would kill a back end you started separately.
rem
rem If a back end is already running, the front end attaches to it: see
rem _handleOldBackends() in frontEnd\FrontEndApp.py, which connects over the RPC
rem port file and, from a source checkout, accepts any live back end -- so the
rem spawn path is skipped entirely.
rem
rem If no back end is running, the front end starts one as its own child, exactly
rem as it always has.  So this is also a fine way to just run the app.
rem
rem Closing the UI leaves the back end running ONLY if FrontEndFrame._hasCameras()
rem is true, which is stricter than "a camera exists": it needs a camera that is
rem ENABLED and not frozen AND has at least one enabled rule with responses.
rem Otherwise OnClose calls forceBackendExit() and the back end goes down with
rem the UI.  So if every camera is switched off, closing the front end stops the
rem back end -- that is stock behaviour, not a fault of this script.
rem ===========================================================================

rem 1. Work from the install root, never the caller's cwd.
set "CURRENT_DIR=%~dp0"
if "%CURRENT_DIR:~-1%"=="\" set "CURRENT_DIR=%CURRENT_DIR:~0,-1%"
set "VENV_DIR=%CURRENT_DIR%\venv"

pushd "%CURRENT_DIR%"
if errorlevel 1 (
    echo [Frontend] ERROR: cannot enter "%CURRENT_DIR%".
    exit /b 1
)

rem 2. Require the venv; never build one.  That is Start.bat's job.
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [Frontend] ERROR: no virtual environment at "%VENV_DIR%".
    echo [Frontend] Run Start.bat once to build it, then use this script.
    popd
    exit /b 1
)
call "%VENV_DIR%\Scripts\activate.bat"

rem 3. No CUDA check here.  All inference happens in the back end; the front end
rem    only draws.  StartBackend.bat does that check.

rem 4. Run attached, so tracebacks land in this console.  (build\launcher.py
rem    detaches the front end for an installed build and sends its output to a
rem    log; from a checkout, watching it is worth more.)
rem    No -s flag, for the same reason as StartBackend.bat: Start.bat has never
rem    passed it.
echo Starting Sighthound Video Py3 front end...
python -m frontEnd.FrontEndApp
set "RC=%ERRORLEVEL%"

popd
exit /b %RC%
