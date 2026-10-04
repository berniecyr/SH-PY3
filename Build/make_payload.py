#*****************************************************************************
#
# make_payload.py
#   Stages everything the Inno Setup installer ships, into build\stage\payload.
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" Build the installable image of Sighthound Video Py3.

The installer ships a COMPLETE program: a private CPython 3.12.10, every pinned
dependency (including the CUDA torch build), the AI model files, and the app
sources. Nothing is downloaded or compiled on the target machine, and the user
is never asked to install Python -- which is the whole point of this script.

    py -3.12 build\\make_payload.py                # everything
    py -3.12 build\\make_payload.py --stage app    # just re-copy app sources
    py -3.12 build\\make_payload.py --stage runtime

Stages:
    runtime   private Python + pip install + models + renamed/stamped exes
              (slow: ~6 GB of wheels, needs a working internet connection)
    app       app sources + the launcher exe (fast; re-run after code edits)

The staged image is the install directory as it will exist on the target:

    payload\\
        SighthoundVideoPy3.exe      launcher (cx_Freeze; what the shortcut runs)
        lib\\                        ... and the runtime it needs beside it
        python\\                     private CPython + all site-packages
            SighthoundPy3.exe       pythonw copy -- every app process
            SighthoundPy3c.exe      python copy  -- console/admin use
        models\\                     insightface + kokoro TTS model files
        frontEnd\\ backEnd\\ ...      the app itself

Why a private Python instead of a venv: a venv still needs the base interpreter
installed on the machine, so shipping one means running the Python installer on
the target and hoping nothing else touches it. A copied CPython tree is fully
relocatable (it finds its stdlib relative to the executable), needs no registry
entries, and cannot be disturbed by -- or disturb -- any other Python on the
box. The bundled python-3.12.10-amd64.exe is still used as the SOURCE of that
tree when this machine has no matching install.

Process names: Task Manager shows the image name and the version resource's
FileDescription, so a plain python.exe reads as "Python". Every executable that
ends up in the process list is copied to a Sighthound name and re-stamped as
"Sighthound Py3" -- the interpreter, the service host and ffmpeg.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile


###############################################################################

kThisDir = os.path.dirname(os.path.abspath(__file__))
kRepoRoot = os.path.dirname(kThisDir)

kStageDir = os.path.join(kThisDir, "stage")
kPayloadDir = os.path.join(kStageDir, "payload")
kPythonDir = os.path.join(kPayloadDir, "python")
kModelsDir = os.path.join(kPayloadDir, "models")

# The Python we ship. Must match the bundled installer, since that is the
# fallback source for the runtime tree.
kPythonVersion = "3.12.10"
kBundledPythonInstaller = os.path.join(kRepoRoot, "python-3.12.10-amd64.exe")

# Executable names as they appear in Task Manager once installed.
kInterpreterGui = "SighthoundPy3.exe"       # pythonw.exe: app processes
kInterpreterConsole = "SighthoundPy3c.exe"  # python.exe: CLI / service admin
kServiceHostExe = "SighthoundPy3Service.exe"
kFfmpegExe = "SighthoundPy3-ffmpeg.exe"

kProcessDescription = "Sighthound Py3"
kProductName = "Sighthound Video Py3"
kCompany = "Sighthound Video Py3"

# Pins that Start.bat applies AFTER requirements.txt; kept identical here so the
# installed program is the same stack the source tree runs (see Start.bat for
# why each one exists -- they are not arbitrary).
kOpenCvHeadless = "opencv-python-headless==4.13.0.92"
kOnnxRuntimeGpu = "onnxruntime-gpu==1.22.0"
kTorchPins = ["torch==2.12.1", "torchvision==0.27.1"]
kTorchIndexUrl = "https://download.pytorch.org/whl/cu126"

# Stdlib pieces no shipped app needs; dropping them keeps the payload honest.
kPythonPrunePaths = [
    os.path.join("Lib", "test"),
    os.path.join("Lib", "idlelib"),
    os.path.join("Lib", "turtledemo"),
    "Doc",
    "Tools",
    "include",
    "libs",
]

# Never copied into the app image. The native-build leftovers (win\, mac\) are
# hundreds of MB of Python 2 era tarballs; .git is not a runtime asset.
#
# `models` is here for a REASON that is easy to undo by accident: stageModels()
# has already filled payload\models with ~600 MB of InsightFace and Kokoro
# weights by the time stageApp() runs, and stageApp() rmtree's the destination
# before copying each directory.  Windows treats payload\Models and
# payload\models as the same directory, so without this entry a models\ folder
# in the checkout DELETES everything stageModels() just staged -- and the build
# still reports success, with the shipped app silently downloading models on
# first use.  stageModels() copies what we want from models\ explicitly.
kAppExcludeNames = {
    ".git", ".github", ".gitignore", ".gitattributes", ".vs", ".vscode",
    ".idea",
    "__pycache__", "venv", "build", "win", "mac", "setupEnvironment",
    "Microsoft.VC90.CRT.x86_64", "CodeResources.plist", "buildSV.sh",
    "Makefile", "PackageSources.py", "models",
    os.path.basename(kBundledPythonInstaller),
}
# Compared case-INSENSITIVELY against os.listdir(), which reports the casing on
# disk: a stray `Models\` must be excluded just as surely as `models\`.
kAppExcludeLower = {name.lower() for name in kAppExcludeNames}
# Matched with .endswith(), so this -- not kAppExcludeNames -- is where a whole
# FILE TYPE is excluded.  An extension listed in the names set above would only
# ever match a file literally called ".bat".
#
# .bat: none of the batch files belong in an installed program.  Start.bat and
# its StartBackend/StartFrontend siblings drive a SOURCE CHECKOUT -- they expect
# a venv beside them, and Start.bat would try to build one under
# %ProgramFiles%, which it cannot write to (it says so itself).  The installed
# app is launched by SighthoundVideoPy3.exe instead.
kAppExcludeSuffixes = (".pyc", ".pyo", ".tgz", ".dmg", ".sh", ".bat")

# Model assets we bundle so a fresh machine never has to download a model.
# (source on this build machine, destination under payload\models)
kInsightFaceModel = "buffalo_l"
kTtsModelFiles = ["kokoro-v1.0.onnx", "voices-v1.0.bin"]

# Model families copied verbatim from <repo>\models\<name> to the same name
# under payload\models.  (insightface is staged separately: it keeps its
# ~\.insightface cache and zip as fallbacks for a build machine with no repo
# copy, and the Kokoro voices still come from this machine's data directory.)
#
# NudeNet's 320n.onnx is deliberately absent: it ships inside the nudenet wheel
# and is already staged with the interpreter.  Only the optional high-resolution
# model needs bundling.
kRepoModelDirs = ["nudenet", "yolo"]


###############################################################################
def log(msg):
    """Print a progress line with an elapsed-time prefix.

    @param  msg  Text to print.
    """
    print("[%7.1fs] %s" % (time.time() - _kStartedAt, msg), flush=True)


_kStartedAt = time.time()


###############################################################################
def run(argv, cwd=None, check=True):
    """Run a command, streaming its output.

    @param  argv   Command line.
    @param  cwd    Working directory.
    @param  check  Raise on a non-zero exit.
    @return        The exit code.
    """
    log("run: %s" % " ".join('"%s"' % a if " " in a else a for a in argv))
    rc = subprocess.call(argv, cwd=cwd)
    if check and rc != 0:
        raise SystemExit("command failed (%d): %s" % (rc, argv))
    return rc


###############################################################################
def appVersion():
    """Read the app version out of appCommon/CommonStrings.py.

    Kept in one place (the app's own constant) so the installer, the file
    version resources and the app itself can never disagree.

    The four-part form KEEPS the constant's zero padding: "2026.08.01" becomes
    "2026.08.01.0", not "2026.8.1.0".  Windows holds the numeric FILEVERSION
    fields as integers, where 08 and 8 are the same number -- but win32verstamp
    writes the string it was handed straight into the FileVersion and
    ProductVersion resource STRINGS, and those are what Explorer's Details tab
    and Task Manager show.  Padding there keeps them reading like the version
    everywhere else.

    @return  (displayVersion, fourPartVersion)
             e.g. ("2026.08.01", "2026.08.01.0").
    """
    path = os.path.join(kRepoRoot, "appCommon", "CommonStrings.py")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        match = re.search(r'^kVersionString\s*=\s*"([^"]+)"', f.read(),
                          re.MULTILINE)
    display = match.group(1) if match else "0.0.0"
    parts = re.findall(r"\d+", display)[:4]
    while len(parts) < 4:
        parts.append("0")
    return display, ".".join(parts)


###############################################################################
def findBasePython(explicit=None):
    """Locate a CPython 3.12.10 install to copy the private runtime from.

    Order: an explicit --base-python, then any 3.12 install the py launcher
    knows about whose version matches exactly, then the bundled installer (run
    into a scratch directory, then uninstalled again).

    @param  explicit  Directory given on the command line, or None.
    @return           Path to a Python home directory (holding python.exe).
    """
    if explicit:
        exe = os.path.join(explicit, "python.exe")
        if not os.path.isfile(exe):
            raise SystemExit("--base-python has no python.exe: %s" % explicit)
        _requireVersion(exe)
        return explicit

    candidates = []
    try:
        out = subprocess.check_output(["py", "-0p"], text=True,
                                      stderr=subprocess.STDOUT)
        for line in out.splitlines():
            match = re.search(r"(\S:\\\S.*python\.exe)", line, re.IGNORECASE)
            if match and "-3.12" in line:
                candidates.append(match.group(1).strip())
    except Exception:
        pass
    candidates += [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Python312", "python.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python312", "python.exe"),
    ]

    for exe in candidates:
        try:
            if os.path.isfile(exe) and _pythonVersionOf(exe) == kPythonVersion:
                log("base python: %s" % exe)
                return os.path.dirname(exe)
        except Exception:
            continue

    return _installBasePython()


###############################################################################
def _pythonVersionOf(exe):
    """@param  exe  A python.exe.  @return  Its version string, e.g. 3.12.10."""
    out = subprocess.check_output(
        [exe, "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
        text=True)
    return out.strip()


###############################################################################
def _requireVersion(exe):
    """Fail loudly when an interpreter is not the version we ship.

    @param  exe  A python.exe.
    """
    got = _pythonVersionOf(exe)
    if got != kPythonVersion:
        raise SystemExit("expected Python %s, found %s at %s" %
                         (kPythonVersion, got, exe))


###############################################################################
def _installBasePython():
    """Last resort: install the bundled Python into a scratch dir and copy it.

    Only reached on a build machine with no matching 3.12.10. Installs
    per-user, into our own scratch directory, with nothing registered on PATH,
    then uninstalls it once the tree has been copied out.

    @return  Path to a Python home directory.
    """
    if not os.path.isfile(kBundledPythonInstaller):
        raise SystemExit(
            "no Python %s on this machine and %s is missing -- pass "
            "--base-python <dir>" % (kPythonVersion, kBundledPythonInstaller))

    scratch = os.path.join(kStageDir, "basepython")
    if os.path.isdir(scratch):
        shutil.rmtree(scratch, ignore_errors=True)
    log("installing bundled Python %s into %s" % (kPythonVersion, scratch))
    run([kBundledPythonInstaller, "/quiet", "InstallAllUsers=0",
         "TargetDir=%s" % scratch, "AssociateFiles=0", "Shortcuts=0",
         "Include_launcher=0", "Include_test=0", "Include_doc=0",
         "PrependPath=0", "InstallLauncherAllUsers=0"])
    if not os.path.isfile(os.path.join(scratch, "python.exe")):
        raise SystemExit("bundled Python install produced no python.exe")
    return scratch


###############################################################################
def stageRuntime(basePython, skipModels=False):
    """Build payload\\python: interpreter, dependencies, models, renamed exes.

    @param  basePython  Python home to copy the interpreter tree from.
    @param  skipModels  Skip bundling the AI model files.
    """
    if os.path.isdir(kPythonDir):
        log("clearing previous runtime stage")
        shutil.rmtree(kPythonDir, ignore_errors=True)
    os.makedirs(kPythonDir, exist_ok=True)

    # 1. The interpreter itself, minus anything user-installed: the payload has
    #    to be reproducible, not a snapshot of this machine's site-packages.
    log("copying interpreter from %s" % basePython)
    shutil.copytree(basePython, kPythonDir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("site-packages", "Scripts",
                                                  "__pycache__", "*.pyc"))
    for relative in kPythonPrunePaths:
        shutil.rmtree(os.path.join(kPythonDir, relative), ignore_errors=True)
    os.makedirs(os.path.join(kPythonDir, "Lib", "site-packages"), exist_ok=True)

    python = os.path.join(kPythonDir, "python.exe")
    _requireVersion(python)

    # 2. pip, then the pinned stack. -s keeps the build honest: without it pip
    #    and every verification import would see this machine's per-user
    #    site-packages (%APPDATA%\Python), which do not ship.
    pip = [python, "-s", "-m", "pip", "install", "--no-warn-script-location"]
    run([python, "-s", "-m", "ensurepip", "--upgrade"])
    run(pip + ["--upgrade", "pip", "setuptools", "wheel"])
    run(pip + ["-r", os.path.join(kRepoRoot, "requirements.txt")])

    # 3. Exactly the corrections Start.bat makes after requirements.txt.
    #    OpenCV: ultralytics drags in the full build, and having both it and
    #    the headless build share cv2\ corrupts the heap.
    log("normalizing OpenCV to a single headless build")
    run([python, "-s", "-m", "pip", "uninstall", "-y", "opencv-python",
         "opencv-python-headless"], check=False)
    shutil.rmtree(os.path.join(kPythonDir, "Lib", "site-packages", "cv2"),
                  ignore_errors=True)
    run(pip + [kOpenCvHeadless])

    log("swapping onnxruntime for the CUDA-12 build")
    run([python, "-s", "-m", "pip", "uninstall", "-y", "onnxruntime",
         "onnxruntime-gpu"], check=False)
    run(pip + [kOnnxRuntimeGpu])

    log("installing the CUDA build of PyTorch")
    run(pip + kTorchPins + ["--index-url", kTorchIndexUrl,
                            "--force-reinstall", "--no-deps"])

    if not skipModels:
        stageModels(python)

    renameExecutables()
    stageVcRuntime()
    verifyRuntime()


###############################################################################
def stageModels(python):
    """Bundle the AI models so a fresh machine never downloads one.

    Left alone, InsightFace fetches buffalo_l (~280 MB) into ~\\.insightface on
    first use and kokoro-onnx fetches its voice model (~330 MB) into the data
    directory. Both would need internet on first run -- and under the service
    the "home" they download into is not even the user's. We ship them instead;
    the app falls back to downloading only if a model is missing.

    Sources differ by family: buffalo_l, the YOLO weights and the optional
    NudeNet model all live in the repo, while the Kokoro voices are still taken
    from this machine's data directory.

    @param  python  The staged interpreter (used to find nudenet's own model).
    """
    log("bundling model files")
    os.makedirs(kModelsDir, exist_ok=True)

    # InsightFace: same layout it uses itself, so root= just works.
    #
    # The REPO copy is preferred over the per-user ~\.insightface cache, so a
    # build reproduces from the checkout rather than from whatever happens to
    # be on the build machine.  The cache and its zip stay as fallbacks for a
    # machine that has run the app but has no repo copy yet.
    faceCacheRoot = os.path.join(os.path.expanduser("~"), ".insightface",
                                 "models")
    faceRepoRoot = os.path.join(kRepoRoot, "models", "insightface", "models")
    faceDstRoot = os.path.join(kModelsDir, "insightface", "models")
    dst = os.path.join(faceDstRoot, kInsightFaceModel)
    repoSrc = os.path.join(faceRepoRoot, kInsightFaceModel)
    cacheSrc = os.path.join(faceCacheRoot, kInsightFaceModel)
    if os.path.isdir(repoSrc):
        shutil.copytree(repoSrc, dst, dirs_exist_ok=True)
        log("  insightface/%s bundled" % kInsightFaceModel)
    elif os.path.isdir(cacheSrc):
        shutil.copytree(cacheSrc, dst, dirs_exist_ok=True)
        log("  insightface/%s bundled (from ~\\.insightface)"
            % kInsightFaceModel)
    elif os.path.isfile(cacheSrc + ".zip"):
        os.makedirs(dst, exist_ok=True)
        with zipfile.ZipFile(cacheSrc + ".zip") as zf:
            zf.extractall(faceDstRoot)
        log("  insightface/%s bundled (from zip)" % kInsightFaceModel)
    else:
        log("  WARNING: %s not found on this machine -- the installed app "
            "will download it on first use" % repoSrc)

    # Kokoro TTS.
    ttsSrc = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          kProductName, "TtsModels")
    ttsDst = os.path.join(kModelsDir, "tts")
    os.makedirs(ttsDst, exist_ok=True)
    for name in kTtsModelFiles:
        source = os.path.join(ttsSrc, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(ttsDst, name))
            log("  tts/%s bundled" % name)
        else:
            log("  WARNING: %s not found -- TTS will download it on first use"
                % source)

    # YOLO and NudeNet weights, which live in the repo rather than in a
    # per-user cache.  These are what the AI Detection model pickers choose
    # between; a missing file is not fatal, the detector falls back to its
    # default model and says so in DetectionService.log.
    for family in kRepoModelDirs:
        source = os.path.join(kRepoRoot, "models", family)
        if not os.path.isdir(source):
            log("  WARNING: %s not found -- the installed app will fall back "
                "to its default model" % source)
            continue
        destination = os.path.join(kModelsDir, family)
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in sorted(os.listdir(destination)):
            log("  %s/%s bundled" % (family, name))


###############################################################################
def renameExecutables():
    """Give every process the app spawns a Sighthound name and description.

    Copies (never renames -- pip, the service installer and cx_Freeze all still
    want the originals) and re-stamps the version resource, because Task
    Manager's Processes tab shows FileDescription, not the file name.
    """
    log("creating Sighthound-named executables")
    _, fileVersion = appVersion()

    copies = [
        (os.path.join(kPythonDir, "pythonw.exe"),
         os.path.join(kPythonDir, kInterpreterGui)),
        (os.path.join(kPythonDir, "python.exe"),
         os.path.join(kPythonDir, kInterpreterConsole)),
    ]

    site = os.path.join(kPythonDir, "Lib", "site-packages")
    serviceHost = os.path.join(site, "win32", "pythonservice.exe")
    if os.path.isfile(serviceHost):
        # Into python\, NOT next to the original in site-packages\win32: the
        # host imports python312.dll, and Windows searches the executable's own
        # directory first. Left in site-packages it only starts when a Python
        # happens to be on PATH -- true on a developer box, false on the
        # machines this installer targets, where the service would then fail to
        # start with an undebuggable error. Verified both ways with a stripped
        # PATH.
        copies.append((serviceHost, os.path.join(kPythonDir, kServiceHostExe)))

    ffmpeg = _findBundledFfmpeg(site)
    if ffmpeg:
        copies.append((ffmpeg, os.path.join(os.path.dirname(ffmpeg),
                                            kFfmpegExe)))

    for source, target in copies:
        shutil.copy2(source, target)
        _stamp(target, fileVersion)
        log("  %s" % os.path.basename(target))

    # pythonservice.exe needs pythoncom312.dll / pywintypes312.dll on the DLL
    # search path. pywin32's postinstall copies them into System32; putting them
    # next to the exe instead gets the same result without touching the system
    # (and an installer that scribbles in System32 is an installer that breaks
    # some other Python's pywin32).
    dllSource = os.path.join(site, "pywin32_system32")
    if os.path.isdir(dllSource):
        for name in os.listdir(dllSource):
            if name.lower().endswith(".dll"):
                shutil.copy2(os.path.join(dllSource, name),
                             os.path.join(site, "win32", name))
                shutil.copy2(os.path.join(dllSource, name),
                             os.path.join(kPythonDir, name))
        log("  pywin32 DLLs placed next to the interpreter and service host")


###############################################################################
def stageVcRuntime():
    """Put the Visual C++ runtime beside the interpreter.

    Python itself ships vcruntime140*.dll in its own directory, but the wheels
    do not: wx, OpenCV and scikit-learn want msvcp140.dll and vcomp140.dll from
    the VC++ 2015-2022 redistributable, and a fresh Windows install does not
    necessarily have it. Deploying those DLLs app-locally (which their license
    allows) is better than running a redist installer as a side effect of ours:
    nothing machine-wide changes, and the app cannot be broken later by another
    program's redist.

    DLL search order puts the loading executable's directory first, and every
    app process is python\\SighthoundPy3.exe, so python\\ is where they go.
    """
    names = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
             "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
             "vcruntime140.dll", "vcruntime140_1.dll",
             "vcruntime140_threads.dll", "concrt140.dll", "vcomp140.dll",
             "vccorlib140.dll", "vcamp140.dll")
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32")

    copied = 0
    for name in names:
        target = os.path.join(kPythonDir, name)
        if os.path.isfile(target):
            continue
        for source in (os.path.join(kPayloadDir, name),
                       os.path.join(system32, name)):
            if os.path.isfile(source):
                shutil.copy2(source, target)
                copied += 1
                break
    log("VC++ runtime: %d DLLs staged beside the interpreter" % copied)


###############################################################################
def _findBundledFfmpeg(sitePackages):
    """@param  sitePackages  site-packages dir.
       @return  imageio-ffmpeg's binary, or None."""
    binaries = os.path.join(sitePackages, "imageio_ffmpeg", "binaries")
    if not os.path.isdir(binaries):
        return None
    for name in sorted(os.listdir(binaries)):
        lowered = name.lower()
        if lowered.startswith("ffmpeg") and lowered.endswith(".exe"):
            return os.path.join(binaries, name)
    return None


###############################################################################
def _stamp(exePath, fileVersion):
    """Rewrite an executable's version resource, so it reads as Sighthound Py3.

    Done by the STAGED interpreter, not this one: pywin32's win32verstamp needs
    win32api.pyd and pywintypes312.dll, and the ones that matter are the ones we
    just installed. Runs through python.exe (never through a Sighthound-named
    copy) because BeginUpdateResource cannot write to a running executable.

    @param  exePath      Executable to stamp.
    @param  fileVersion  Four-part version string.
    """
    script = (
        "import sys, types, win32verstamp;"
        "win32verstamp.stamp(sys.argv[1], types.SimpleNamespace("
        "version=sys.argv[2], description=sys.argv[3], company=sys.argv[4],"
        "product=sys.argv[5], copyright=sys.argv[6], trademarks='',"
        "comments=sys.argv[7], internal_name=None, original_filename=None,"
        "dll=False, debug=False, verbose=False))"
    )
    rc = subprocess.call(
        [os.path.join(kPythonDir, "python.exe"), "-s", "-c", script, exePath,
         fileVersion, kProcessDescription, kCompany, kProductName,
         "Licensed under the GNU GPLv3", "Sighthound Video Py3 runtime"])
    if rc != 0:
        log("  WARNING: could not stamp %s (exit %d); it keeps Python's "
            "version resource" % (os.path.basename(exePath), rc))


###############################################################################
def verifyRuntime():
    """Import the whole native stack out of the staged runtime.

    A payload that cannot import cv2/torch/wx is worse than no payload: the
    failure would otherwise surface on the user's machine, after a 2 GB
    download and an install.
    """
    log("verifying the staged runtime")
    checks = (
        "import sys, os;"
        "import wx, cv2, numpy, torch, PIL, psutil;"
        "import imageio_ffmpeg;"
        "import win32serviceutil, win32service;"
        "print('python     ', sys.version.split()[0]);"
        "print('wx         ', wx.version());"
        "print('cv2        ', cv2.__version__);"
        "print('numpy      ', numpy.__version__);"
        "print('torch      ', torch.__version__, 'cuda', torch.cuda.is_available());"
        "print('ffmpeg     ', os.path.basename(imageio_ffmpeg.get_ffmpeg_exe()));"
    )
    run([os.path.join(kPythonDir, kInterpreterConsole), "-s", "-c", checks])

    # onnxruntime is imported separately: it must be the GPU build, and the
    # import must survive without torch having pre-loaded the CUDA DLLs.
    run([os.path.join(kPythonDir, kInterpreterConsole), "-s", "-c",
         "import onnxruntime as ort;"
         "print('onnxruntime', ort.__version__, ort.get_available_providers())"])


###############################################################################
def stageApp():
    """Copy the app sources and build the launcher executable."""
    log("copying app sources")
    for name in sorted(os.listdir(kRepoRoot)):
        if (name.lower() in kAppExcludeLower
                or name.lower().endswith(kAppExcludeSuffixes)):
            continue
        source = os.path.join(kRepoRoot, name)
        target = os.path.join(kPayloadDir, name)
        if os.path.isdir(source):
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target,
                            ignore=shutil.ignore_patterns("__pycache__",
                                                          "*.pyc", "*.pyo"))
        else:
            os.makedirs(kPayloadDir, exist_ok=True)
            shutil.copy2(source, target)

    buildLauncher()

    # cx_Freeze stages the VC++ runtime beside the launcher; the interpreter
    # needs the same DLLs in ITS directory (see stageVcRuntime).
    stageVcRuntime()

    compileSources()

    display, _ = appVersion()
    with open(os.path.join(kPayloadDir, "VERSION.txt"), "w",
              encoding="utf-8") as f:
        f.write("Sighthound Video Py3 %s\nbuilt %s\n" %
                (display, time.strftime("%Y-%m-%d %H:%M:%S")))


###############################################################################
def compileSources():
    """Ship the bytecode, so nothing tries to write it at run time.

    The program installs under Program Files, which a normal user cannot write
    to: without this, every process -- the front end plus one per camera --
    fails to cache its bytecode and recompiles the same modules on every single
    launch. Errors are ignored on purpose; a module that will not byte-compile
    is a problem for the app to report, not for the build to die on.
    """
    log("byte-compiling the app sources")
    # --invalidation-mode unchecked-hash: accept the shipped .pyc without
    # checking it against the .py at all.  The default is to compare the
    # source's mtime, which only works if the mtime the installer leaves on
    # disk matches the one recorded here at build time -- and if it ever
    # drifts, the failure is silent and permanent: the install is read-only,
    # so every process recompiles every module on every launch and can never
    # cache the result.  That is exactly the cost this function exists to
    # avoid, so do not leave it resting on a timestamp.
    run([os.path.join(kPythonDir, kInterpreterConsole), "-s", "-m",
         "compileall", "-q", "-j", "0",
         "--invalidation-mode", "unchecked-hash", kPayloadDir,
         "-x", r"[\\/](python|lib)[\\/]"], check=False)


###############################################################################
def buildLauncher():
    """Freeze build\\launcher.py into SighthoundVideoPy3.exe.

    Built with the STAGED interpreter, so the exe is linked against the same
    python312.dll that ships. cx_Freeze needs its lib\\ folder beside the
    executable, so both land in the payload root.
    """
    python = os.path.join(kPythonDir, kInterpreterConsole)
    if not os.path.isfile(python):
        raise SystemExit("stage the runtime before the app (no %s)" % python)

    log("building the launcher executable")
    buildBase = os.path.join(kStageDir, "launcher")
    shutil.rmtree(buildBase, ignore_errors=True)
    run([python, "-s", os.path.join(kThisDir, "setup_launcher.py"),
         "build_exe", "--build-exe", buildBase], cwd=kThisDir)

    built = None
    for root, _dirs, files in os.walk(buildBase):
        if any(name.lower() == "sighthoundvideopy3.exe" for name in files):
            built = root
            break
    if built is None:
        raise SystemExit("cx_Freeze produced no SighthoundVideoPy3.exe under %s"
                         % buildBase)

    for name in os.listdir(built):
        source = os.path.join(built, name)
        target = os.path.join(kPayloadDir, name)
        if os.path.isdir(source):
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    log("  launcher staged into the payload root")


###############################################################################
def payloadSizeMb():
    """@return  Size of the staged payload in MB."""
    total = 0
    for root, _dirs, files in os.walk(kPayloadDir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024.0 * 1024.0)


###############################################################################
def main(argv=None):
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", default="all",
                        choices=["all", "runtime", "app"])
    parser.add_argument("--base-python", default=None,
                        help="Python 3.12.10 home to copy the runtime from.")
    parser.add_argument("--skip-models", action="store_true",
                        help="Do not bundle the AI model files.")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        raise SystemExit("the installer payload can only be built on Windows")

    os.makedirs(kPayloadDir, exist_ok=True)

    if args.stage in ("all", "runtime"):
        stageRuntime(findBasePython(args.base_python), args.skip_models)
    if args.stage in ("all", "app"):
        stageApp()

    log("payload ready: %s (%.0f MB)" % (kPayloadDir, payloadSizeMb()))
    return 0


###############################################################################
if __name__ == "__main__":
    sys.exit(main())
