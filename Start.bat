@echo off
setlocal enabledelayedexpansion

echo Starting Sighthound Video Py3

rem 1. Get the current script directory and strip the trailing backslash
set "CURRENT_DIR=%~dp0"
if "%CURRENT_DIR:~-1%"=="\" set "CURRENT_DIR=%CURRENT_DIR:~0,-1%"

set "VENV_DIR=%CURRENT_DIR%\venv"
set "LOCATION_FILE=%VENV_DIR%\.venv_location"

rem Everything below assumes the working directory is the install root (pip -r,
rem the module launch at the end). Don't inherit the caller's cwd.
rem Failures below jump to a label and exit there rather than calling "exit /b"
rem inside a parenthesised block: in a block the exit code gets swallowed and
rem the script reports success. setlocal restores the cwd on exit, so no popd.
pushd "%CURRENT_DIR%"
if errorlevel 1 (
    echo [Setup] ERROR: cannot enter "%CURRENT_DIR%".
    goto :fail
)

rem 2. Verify existing Venv location
if exist "%VENV_DIR%\Scripts\activate.bat" (
    if exist "%LOCATION_FILE%" (
        rem Read the saved location from the marker file
        set /p SAVED_LOCATION=<"%LOCATION_FILE%"
        
        if not "!SAVED_LOCATION!"=="%CURRENT_DIR%" (
            echo [Setup] Folder moved or renamed! 
            echo [Setup] Old path: !SAVED_LOCATION!
            echo [Setup] New path: %CURRENT_DIR%
            echo [Setup] Rebuilding virtual environment for new location...
            rmdir /s /q "%VENV_DIR%"
        )
    ) else (
        echo [Setup] Unverified venv detected. Rebuilding to guarantee portability...
        rmdir /s /q "%VENV_DIR%"
    )
)

rem 3. Create and configure the Venv if it is missing
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [Setup] Creating new virtual environment for Python 3.12...

    rem An ordinary user cannot write under %ProgramFiles%, so a venv can never
    rem be built in place there. Say so instead of failing halfway through.
    copy /y nul "%CURRENT_DIR%\.write_test" >nul 2>&1
    if not exist "%CURRENT_DIR%\.write_test" (
        echo [Setup] ERROR: "%CURRENT_DIR%" is not writable, and no venv is
        echo [Setup] present, so one cannot be created here.
        echo [Setup] Re-run build\install.ps1 WITHOUT -SkipVenv from an elevated
        echo [Setup] prompt so the prebuilt venv is copied to the install dir.
        goto :fail
    )
    del "%CURRENT_DIR%\.write_test" >nul 2>&1

    rem Pin to 3.12 via the py launcher so a leftover 3.11 on PATH can't be used;
    rem fall back to whatever 'python' is if the launcher/version isn't present.
    rem Absolute target: the venv belongs beside this script, never in the cwd.
    py -3.12 -m venv "%VENV_DIR%" || python -m venv "%VENV_DIR%"

    rem Stop here on failure. Carrying on would run every pip install below
    rem against the SYSTEM Python, because activate.bat never ran.
    if not exist "%VENV_DIR%\Scripts\activate.bat" (
        echo [Setup] ERROR: failed to create the virtual environment at
        echo [Setup] "%VENV_DIR%". Aborting so the system Python is left alone.
        goto :fail
    )

    rem Write the current path into the marker file - no trailing spaces
    >"%LOCATION_FILE%" echo %CURRENT_DIR%

    echo [Setup] Activating...
    call "%VENV_DIR%\Scripts\activate.bat"
    echo [Setup] venv interpreter:
    python --version
    
    echo [Setup] Upgrading core build tools...
    python -m pip install --upgrade pip setuptools wheel
    
    echo [Setup] Installing dependencies from requirements.txt...
    pip install -r requirements.txt

    rem Normalize OpenCV to a single, clean HEADLESS build. ultralytics pulls in
    rem the full opencv-python transitively, so requirements.txt ends up installing
    rem BOTH opencv-python and opencv-python-headless -- they share the cv2\ folder
    rem and leave a mixed set of native DLLs that corrupts the heap and crashes the
    rem app. A bare "uninstall opencv-python" can delete shared files and break the
    rem headless build, so: uninstall both, wipe orphans, reinstall headless only.
    echo [Setup] Normalizing OpenCV to a single headless build...
    pip uninstall -y opencv-python opencv-python-headless
    rmdir /s /q "%VENV_DIR%\Lib\site-packages\cv2" 2>nul
    pip install opencv-python-headless==4.13.0.92

    rem GPU onnxruntime for face/nudity in the DetectionService (one shared
    rem process). nudenet pulls in the CPU-only 'onnxruntime'; swap it for the
    rem CUDA-12 build (1.22.x) that pairs with PyTorch cu126's bundled DLLs —
    rem onnxruntime-gpu 1.27+ is built for CUDA 13 and will NOT load. Sessions
    rem fall back to CPU automatically if the GPU is unavailable. (pip check
    rem will note nudenet wants 'onnxruntime' - benign, same as ultralytics.)
    pip uninstall -y onnxruntime onnxruntime-gpu
    pip install onnxruntime-gpu==1.22.0

    rem Install the CUDA build of PyTorch so YOLO inference runs on the GPU.
    rem The default PyPI torch wheel is CPU-only and starves the video capture
    rem loop (causing low fps / frame drops / time desync). cu126 wheels work
    rem with the installed NVIDIA driver. --no-deps so only torch/torchvision
    rem swap to the CUDA build; everything else stays as pinned above.
    echo [Setup] Installing CUDA PyTorch for GPU inference...
    pip install torch==2.12.1 torchvision==0.27.1 --index-url https://download.pytorch.org/whl/cu126 --force-reinstall --no-deps

    rem Sanity check that the critical native deps resolve before first launch.
    echo [Setup] Verifying cv2 + ffmpeg + CUDA...
    python -c "import cv2, imageio_ffmpeg, torch, os; print('cv2', cv2.__version__, 'ffmpeg', os.path.exists(imageio_ffmpeg.get_ffmpeg_exe()), 'cuda', torch.cuda.is_available())"

    echo [Setup] Complete!
) else (
    rem Venv exists and location is verified
    call "%VENV_DIR%\Scripts\activate.bat"
)

rem 4. Verify the CUDA PyTorch build on EVERY launch, not just at venv creation.
rem    A manual or transitive pip install can silently replace the cu126 build
rem    with the CPU-only PyPI wheel (requirements.txt pins plain torch==2.12.1,
rem    which IS the CPU wheel) - that pushes YOLO + face inference onto the CPU,
rem    causing detection-service timeouts, low fps and frame drops.  Exit codes:
rem    0 = CUDA build working, 1 = CUDA build but driver unavailable (warn only),
rem    2 = CPU wheel detected (auto-repair).
echo [Check] Verifying CUDA PyTorch...
python -c "import torch,sys; v=torch.__version__; ok=torch.cuda.is_available(); print('[Check] torch', v, '- cuda available:', ok); sys.exit(2 if '+cpu' in v else (0 if ok else 1))"
if errorlevel 2 (
    echo [Repair] CPU-only torch wheel detected - reinstalling the CUDA build...
    pip install torch==2.12.1 torchvision==0.27.1 --index-url https://download.pytorch.org/whl/cu126 --force-reinstall --no-deps
    python -c "import torch; print('[Repair] torch is now', torch.__version__, '- cuda available:', torch.cuda.is_available())"
) else (
    if errorlevel 1 (
        echo [WARN] torch is the CUDA build but CUDA is unavailable - check the
        echo [WARN] NVIDIA driver. Continuing on CPU - detection will be slow.
    )
)

rem 5. Kill stale processes from a previous run.  A relaunch while the old
rem    instance is still dying leaves orphaned camera processes + ffmpeg
rem    recorders (they squat on camera RTSP sessions -> 'Operation not
rem    permitted' reconnect failures) and a front end attached to a dead
rem    back end (every RPC fails with WinError 10061).  Targeted kill:
rem    python processes launched from THIS repo or their multiprocessing
rem    spawn children, plus the bundled imageio ffmpeg recorders.
echo [Check] Sweeping stale Sighthound processes...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -and ($_.CommandLine -like '*%CURRENT_DIR%*' -or $_.CommandLine -like '*multiprocessing.spawn*') } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('  killed stale python ' + $_.ProcessId) } catch {} }; Get-Process 'ffmpeg-win-x86_64*' -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; Write-Host ('  killed stale ffmpeg ' + $_.Id) } catch {} }"

rem 6. Optional runtime tuning (uncomment to override defaults):
rem    SV_HW_DECODE       1 (default) = decode camera streams on the GPU video
rem                       engine (D3D11VA), per-camera software fallback;
rem                       0 = force software decode everywhere.
rem set "SV_HW_DECODE=0"

rem    SV_ANALYSIS_LAG_PROBE 1 = log how far analysis frame delivery trails the
rem                       demux, per camera, into logs\cameras\<name>.log.
rem                       MEASUREMENT ONLY - it applies no correction. Costs one
rem                       extra ffmpeg probe per 20s substream segment, so turn
rem                       it back off once the numbers have been read.
rem set "SV_ANALYSIS_LAG_PROBE=1"

rem    SV_ANALYSIS_PASSTHROUGH 1 (default) = hand the detector every demuxed
rem                       frame exactly once. 0 restores ffmpeg's old default,
rem                       which conformed the analysis stream to the rate the
rem                       camera ADVERTISES rather than the one it delivers --
rem                       measured on a 20s 07_Back_Yard sub segment, 300 real
rem                       frames left as 403 with dup=180 drop=77. Only for
rem                       bisecting a regression against the old behaviour.
rem set "SV_ANALYSIS_PASSTHROUGH=0"

rem    SV_ANALYSIS_LAG_CORRECT 0 (default) = do NOT apply the lag probe's
rem                       measurement to the analysis timestamps. Turned off
rem                       2026-08-09 after it failed its validation walk:
rem                       07_Back_Yard's box landed at +1.4s, over-corrected,
rem                       because the probe measures true delay PLUS accumulated
rem                       frame loss and the loss half only grows with run age
rem                       (Back_Yard read 1.81s fresh, 3.21s after 10 hours).
rem                       Set to 1 only after the estimator can separate frames
rem                       still in flight from frames the socket never got.
rem set "SV_ANALYSIS_LAG_CORRECT=1"

rem    SV_ANALYSIS_PTS    carry each analysis frame's own timestamp (default 0).
rem                       Off, the analysis stream is headerless rawvideo -- it
rem                       has no time of its own, so a frame is stamped when it
rem                       reaches us, AFTER decode/scale/hwdownload/socket, while
rem                       the archive is dated at demux.  Measured 2026-08-31,
rem                       that gap ran -0.72s to -2.25s per camera and swung up
rem                       to 2.2s on one camera across a day, which walks a
rem                       subject clean out of their own detection box.
rem                       On, the same frames are muxed into NUT (0.01% more
rem                       bytes) and carry their real PTS, so _stampFrameMs's
rem                       existing anchor removes the backlog by construction --
rem                       nothing estimated, nothing to calibrate.
rem                       Verify with scripts\measure_analysis_skew.py.
rem set "SV_ANALYSIS_PTS=1"
rem NOTE the below seting only applies to new footage with the setting on in conjunction with SV_ANALYSIS_PTS=1 it woudl adjust the bounding box in the playback
rem currently fleet wide 1200 is a good number for all except the High resolution front step camera which should remain at 0.00 no correction
rem this will affect audio so this is not a good fix.
rem    SV_VIDEO_LATENCY_MS fixed video latency bias in ms (default 0).
rem set "SV_VIDEO_LATENCY_MS=1200"


rem NOTE (2026-08-16): SV_SINGLE_STREAM is gone.  One main-stream ffmpeg per
rem                    camera does BOTH the stream-copy recording and the
rem                    analysis frames, and there is no longer a two-session
rem                    shape to switch back to.  The substream gap-fill archive
rem                    (SV_SUB_ARCHIVE) and cross-stream delta (SV_STREAM_DELTA)
rem                    went with it.

rem    SV_LOG_FILE_COUNT  rotated backups kept per log file (default 1, i.e.
rem                       only 10MB per camera at the 5MB rotation size).
rem                       Raised 2026-08-17 so an overnight measurement run is
rem                       not rotated away before it can be read:
rem                       08_FrontStep_hr-dnd writes ~290 KB/h and the
rem                       spider-web storm adds ~600 KB of [focus] lines,
rem                       which fills 5MB well before morning.
set "SV_LOG_FILE_COUNT=5"

set "SV_QT_FTP_DIALOG=1"
set "SV_QT_REMOVE_CAMERA_DIALOG=1"

rem 7. Run the master script
python -m frontEnd.FrontEndApp
popd
exit /b %ERRORLEVEL%

:fail
exit /b 1
