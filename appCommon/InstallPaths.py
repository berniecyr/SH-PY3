#*****************************************************************************
#
# InstallPaths.py
#   Where things live once Sighthound Video Py3 is an installed program.
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" Layout of an installed build, for code that must work both ways.

The app runs from two very different places:

  * a source checkout, with a venv beside it (Start.bat), and
  * an installed program under Program Files, with a private Python, bundled
    model files and Sighthound-named executables (build\\make_payload.py).

Everything that differs between those two lives here, and every accessor
degrades to "not installed" by returning None, so a checkout keeps behaving
exactly as it did before there was an installer.

Deliberately imports nothing but the standard library: the service, the
launcher and the back end all reach for it long before wx or the app's own
config machinery exist.
"""

import os
import shutil
import sys


###############################################################################

# What build\\make_payload.py names the executables it stages. Task Manager
# shows the image name, so nothing the user sees should read "python".
kInterpreterGui = "SighthoundPy3.exe"       # pythonw copy: every app process
kInterpreterConsole = "SighthoundPy3c.exe"  # python copy: CLI / service admin
kServiceHostExe = "SighthoundPy3Service.exe"  # pywin32's service host
kFfmpegExe = "SighthoundPy3-ffmpeg.exe"

# ExifTool, shipped under tools\exiftool so the Image view can read and
# write metadata on the user's own files.  NOT under win\ -- that whole
# directory is in make_payload.py's exclude list, so a binary put there
# would silently never ship while the build still reported success.
kExifToolExe = "exiftool.exe"
kToolsDirName = "tools"

# Written by the installer, holding the absolute path of the data directory it
# configured. Lets the front end agree with the service about where the
# databases are even when the service is not running.
kDataDirFile = "datadir.txt"

# Environment variable carrying the data directory to every child process. The
# front end and the service both export it once they know the answer, which is
# what keeps a back end started by a SERVICE account reading and writing the
# user's data directory instead of its own profile.
kDataDirEnvVar = "SV_DATA_DIR"

# Data directory name under %LOCALAPPDATA%, when nothing else says otherwise.
kDataDirName = "Sighthound Video Py3"

# Subdirectory of the install root holding the bundled AI models.
kModelsDirName = "models"

# Subdirectory of the install root holding the sounds a rule's "Play this
# sound" list offers.  Every playable .wav dropped in here is listed.
kSoundsDirName = "sounds"

# Per-family subdirectories of <root>\models holding single weight FILES (as
# opposed to InsightFace, which wants a whole directory tree). These are what
# the AI Detection model pickers choose between.
kYoloDirName = "yolo"
kNudeNetDirName = "nudenet"

_kRootMarker = "FrontEndLaunchpad.py"


###############################################################################
def getInstallRoot():
    """The directory the app is installed (or checked out) in.

    Derived from this file's location -- appCommon is always one level below the
    root -- so it is correct for the front end, the back end, every spawned
    camera process and the service alike.

    @return  Absolute path to the install root.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


###############################################################################
def getPrivatePythonDir():
    """The private interpreter directory of an installed build.

    @return  Absolute path to <root>\\python, or None in a source checkout.
    """
    candidate = os.path.join(getInstallRoot(), "python")
    if os.path.isfile(os.path.join(candidate, kInterpreterConsole)):
        return candidate
    return None


###############################################################################
def getInterpreter(gui=True):
    """The interpreter the app should spawn its own processes with.

    @param  gui  True for the windowless copy (what the app itself uses), False
                 for the console copy (service administration, diagnostics).
    @return      Absolute path to an interpreter; falls back to the running one.
    """
    pythonDir = getPrivatePythonDir()
    if pythonDir:
        return os.path.join(pythonDir,
                            kInterpreterGui if gui else kInterpreterConsole)

    if not gui:
        return sys.executable
    # In a checkout, prefer pythonw.exe beside the running python.exe so a
    # spawned child does not flash a console window.
    windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return windowless if os.path.isfile(windowless) else sys.executable


###############################################################################
def isInstalledBuild():
    """@return  True when running from an installed program, not a checkout."""
    return getPrivatePythonDir() is not None


###############################################################################
def getFfmpegExe():
    """The ffmpeg binary to spawn.

    An installed build ships imageio-ffmpeg's binary under a Sighthound name so
    the recorders do not show up as "ffmpeg-win-x86_64-v7.1" in Task Manager --
    one per camera. Falls back to imageio-ffmpeg's own copy, then to whatever
    is on PATH, which is what a source checkout has always used.

    @return  Path to an ffmpeg executable (possibly just "ffmpeg").
    """
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"

    renamed = os.path.join(os.path.dirname(exe), kFfmpegExe)
    return renamed if os.path.isfile(renamed) else exe


###############################################################################
def getExifToolExe():
    """The ExifTool binary to spawn, or None if it is not present.

    Unlike ffmpeg there is no fallback: nothing else in the tree bundles
    ExifTool, and imageio has no equivalent.  Returning None rather than a
    bare "exiftool" is deliberate -- the caller can then disable the metadata
    UI with a reason, instead of every edit failing at the moment the user
    clicks Save.

    @return  Absolute path to exiftool.exe, or None.
    """
    candidate = os.path.join(getInstallRoot(), kToolsDirName, "exiftool",
                             kExifToolExe)
    if os.path.isfile(candidate):
        return candidate

    # A developer who has it on PATH should not have to stage it to work on
    # this screen.
    found = shutil.which("exiftool")
    return found or None


###############################################################################
def isOurFfmpegProcess(processName):
    """Whether a process name looks like one of our ffmpeg children.

    Callers sweep up orphaned recorders by name; the bundled binary is renamed,
    so matching "ffmpeg*" alone would miss every one of them on an installed
    build (and leave the camera's RTSP session held open).

    @param  processName  Process image name, e.g. from psutil.
    @return              True if it could be one of ours.
    """
    lowered = (processName or "").lower()
    return lowered.startswith("ffmpeg") or lowered.startswith(
        os.path.splitext(kFfmpegExe)[0].lower())


###############################################################################
def getBundledModelDir(name):
    """A model directory shipped inside the install.

    @param  name  Subdirectory of <root>\\models, e.g. "insightface" or "tts".
    @return       Absolute path, or None when this build bundles no such models.
    """
    candidate = os.path.join(getInstallRoot(), kModelsDirName, name)
    return candidate if os.path.isdir(candidate) else None


###############################################################################
def getSoundsDir():
    """The sounds a rule can play by name: <root>\\sounds.

    @return  Absolute path (not checked for existence).
    """
    return os.path.join(getInstallRoot(), kSoundsDirName)


###############################################################################
def portableSoundPath(path):
    """The form a rule should store its sound path in.

    A file inside the install root is stored relative to it (a sound from the
    list becomes "sounds\\Bells.wav"), so moving or reinstalling the program
    doesn't break every rule that plays one.  Rules used to store the full
    path: on 2026-09-11 the dev machine's rules named seven different copies of
    the app, and 64 of their 69 sound paths led to files that were gone.
    Anything else -- a sound kept elsewhere on the disk -- is stored as given.

    @param  path  An absolute path, or one already in stored form; may be "".
    @return       The path to store.
    """
    if not path or not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, getInstallRoot())
    except ValueError:
        # On another drive, so not inside the install.
        return path
    if relative == os.curdir or relative == os.pardir or \
       relative.startswith(os.pardir + os.sep):
        return path
    return relative


###############################################################################
def resolveSoundPath(path):
    """The file to play for a rule's stored sound path.

    The inverse of portableSoundPath: a relative path is under the install
    root.  A full path that no longer exists but names a file in a folder
    called "sounds" was a sound from the list in some other copy of the app --
    moved, reinstalled, or from before the folder moved out of
    <root>\\frontEnd\\sounds -- so the same file in this install's sounds
    folder is used instead.  That keeps rules saved before 2026-09-11 playing
    without anyone having to open and re-save them.

    @param  path  The stored path, relative or absolute; may be "".
    @return       Absolute path to play (it may not exist), or "" for none.
    """
    if isinstance(path, bytes):
        # Rules pickled by the Python 2 app.
        path = path.decode("utf-8", "replace")
    if not path:
        return ""
    if not os.path.isabs(path):
        path = os.path.join(getInstallRoot(), path)
    if os.path.isfile(path):
        return path

    folder, fileName = os.path.split(path)
    if os.path.basename(folder).lower() == kSoundsDirName:
        candidate = os.path.join(getSoundsDir(), fileName)
        if os.path.isfile(candidate):
            return candidate
    return path


###############################################################################
def getInsightFaceRoot():
    """Root to hand InsightFace so it uses the models we ship.

    InsightFace looks for <root>\\models\\<name>; without this it downloads
    ~280 MB into the *service account's* home directory on first use.

    @return  Absolute path, or None when the models are not bundled.
    """
    root = getBundledModelDir("insightface")
    if root and os.path.isdir(os.path.join(root, "models")):
        return root
    return None


###############################################################################
def _bundledModelFile(family, name, extension):
    """A single bundled weight file, e.g. <root>\\models\\yolo\\yolo26s.pt.

    getBundledModelDir cannot answer this: these families are FILES, not model
    directories, so its os.path.isdir test always fails.

    The name is reduced to a bare file name and checked for the expected
    extension before it is joined to anything.  A config file is user-editable
    and is read by the back end, which may be running as a service account --
    so `"..\\\\..\\\\anything.dll"` must resolve to None rather than to a real
    path outside the models tree.

    @param  family     Subdirectory of <root>\\models, e.g. "yolo".
    @param  name       File name, e.g. "yolo26s.pt".
    @param  extension  Lowercase extension the family expects, e.g. ".pt".
    @return            Absolute path to an existing file, or None.
    """
    leaf = os.path.basename(str(name or "").strip())
    if not leaf or not leaf.lower().endswith(extension):
        return None
    candidate = os.path.join(getInstallRoot(), kModelsDirName, family, leaf)
    return candidate if os.path.isfile(candidate) else None


###############################################################################
def getYoloModelPath(name):
    """Weights for a YOLO_MODEL config value, or None when not installed.

    Returning None rather than the bare name is the point of this function:
    handed a name it cannot find, ultralytics DOWNLOADS it from GitHub into the
    current working directory -- which, for the back end, is whatever directory
    the service account happened to start in.  A caller that gets None can log
    the problem and fall back to a model that is actually present.

    @param  name  File name, e.g. "yolo26s.pt".
    @return       Absolute path to an existing file, or None.
    """
    return _bundledModelFile(kYoloDirName, name, ".pt")


###############################################################################
def getNudeNetModelPath(name):
    """Weights for a NUDE_MODEL config value, or None when not installed.

    Falls back to the nudenet package's own directory, where the default
    320n.onnx ships inside the wheel and so is always present without being
    bundled separately.

    @param  name  File name, e.g. "640m.onnx".
    @return       Absolute path to an existing file, or None.
    """
    bundled = _bundledModelFile(kNudeNetDirName, name, ".onnx")
    if bundled:
        return bundled

    leaf = os.path.basename(str(name or "").strip())
    if not leaf or not leaf.lower().endswith(".onnx"):
        return None

    # find_spec LOCATES the package without importing it. Importing nudenet
    # drags in cv2, numpy and onnxruntime, and this module is imported by the
    # launcher and the wx front end long before any of those should exist in
    # the process (see the "standard library only" note at the top).
    try:
        import importlib.util
        spec = importlib.util.find_spec("nudenet")
        for location in (spec.submodule_search_locations or []):
            candidate = os.path.join(location, leaf)
            if os.path.isfile(candidate):
                return candidate
    except Exception:
        pass
    return None


###############################################################################
def getUserDataDir():
    """The data directory: databases, logs, enrollments, config files.

    Several back-end modules used to build this path from os.path.expanduser
    ("~"), which is correct for a front end and wrong for anything the SERVICE
    starts -- a service account's home is
    C:\\Windows\\System32\\config\\systemprofile, so the back end would keep its
    own empty set of face enrollments and AI settings while the Options dialog
    wrote to the user's.  Resolution order fixes that:

      1. %SV_DATA_DIR%          -- exported by whoever started us
      2. <install>\\datadir.txt  -- what the installer configured
      3. %LOCALAPPDATA%\\<app>   -- the historical location
      4. ~\\AppData\\Local\\<app> -- last resort

    @return  Absolute path to the data directory (not created here).
    """
    fromEnv = os.environ.get(kDataDirEnvVar)
    if fromEnv:
        return fromEnv

    configured = getConfiguredDataDir()
    if configured:
        return configured

    localAppData = os.environ.get("LOCALAPPDATA")
    if localAppData:
        return os.path.join(localAppData, kDataDirName)

    return os.path.join(os.path.expanduser("~"), "AppData", "Local",
                        kDataDirName)


###############################################################################
def exportDataDir(dataDir):
    """Publish the data directory to this process and everything it spawns.

    @param  dataDir  Absolute path to the data directory.
    """
    if dataDir:
        os.environ[kDataDirEnvVar] = os.path.abspath(dataDir)


###############################################################################
def getConfiguredDataDir():
    """The data directory the installer recorded, if any.

    @return  Absolute path, or None when nothing was recorded (a checkout, or an
             install that kept the default).
    """
    path = os.path.join(getInstallRoot(), kDataDirFile)
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except Exception:
        return None
