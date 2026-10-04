#*****************************************************************************
#
# launcher.py
#   The script behind SighthoundVideoPy3.exe -- the installed front-end shortcut.
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" Thin launcher for the Sighthound Video Py3 front end.

cx_Freeze turns this into SighthoundVideoPy3.exe, which is what the Start-menu
and desktop shortcuts point at.

It is deliberately TINY and imports nothing from the app: it just locates the
installed interpreter and re-launches the front end with it. Freezing the front
end itself would mean freezing wx, numpy, OpenCV, torch and the CUDA runtime --
gigabytes, slow to build, and a rich source of hidden-import bugs. Keeping the
app on a real interpreter next to the exe means what ships is exactly what was
tested from source.

Exit codes:
    0  front end launched (or --dry-run printed its plan)
    1  the installation is broken (missing interpreter / entry point)
"""

import ctypes
import os
import subprocess
import sys


###############################################################################

kAppTitle = "Sighthound Video Py3"

# Interpreter candidates, relative to the install root, best first. An
# installed build has the private runtime under python\, with a Sighthound name
# so the app does not sit in Task Manager as "python"; a source checkout has a
# venv. The windowless copy comes first in each pair -- a GUI app should not
# flash a console -- with the console one as the fallback so a broken pythonw
# still gives the user something.
_kInterpreters = [
    os.path.join("python", "SighthoundPy3.exe"),
    os.path.join("python", "SighthoundPy3c.exe"),
    os.path.join("venv", "Scripts", "pythonw.exe"),
    os.path.join("venv", "Scripts", "python.exe"),
]

# The module that actually starts the GUI (FrontEndApp.main()).
_kEntryModule = "frontEnd.FrontEndApp"

# Marker file used to recognise a real install root.
_kRootMarker = "FrontEndLaunchpad.py"


###############################################################################
def _installRoot():
    """Locate the installation directory.

    A cx_Freeze executable cannot stand alone -- it needs the lib\\ folder built
    beside it -- so the launcher is installed as its own subdirectory
    (<root>\\launcher\\SighthoundVideoPy3.exe) rather than loose in the root.
    Accept either layout: the exe's own directory if that looks like the root,
    otherwise its parent.

    From source this file lives in build/, so the root is one level up.

    @return  Absolute path to the install root.
    """
    if getattr(sys, "frozen", False):
        exeDir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.isfile(os.path.join(exeDir, _kRootMarker)):
            return exeDir
        return os.path.dirname(exeDir)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


###############################################################################
def _report(message, isError, quiet):
    """Tell someone what happened.

    Built with base="Win32GUI", so there is no console: a real user has to be
    told in a message box. Under --dry-run we must NOT pop a dialog (it would
    block whatever is testing us) -- the plan goes to stdout when there is one,
    and always to a file that a test can read.

    @param  message  Text to report.
    @param  isError  True for failures, False for the dry-run plan.
    @param  quiet    True to suppress the message box (dry-run).
    @return          1 for errors, 0 otherwise, for use as an exit code.
    """
    try:
        if sys.stdout is not None:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()
    except Exception:
        pass

    if quiet:
        try:
            planPath = os.path.join(os.environ.get("TEMP", "."),
                                    "shlaunch_launcher_plan.txt")
            with open(planPath, "w") as f:
                f.write(message + "\n")
        except Exception:
            pass
    else:
        try:
            ctypes.windll.user32.MessageBoxW(
                None, message, kAppTitle,
                (0x10 if isError else 0x40) | 0x40000)  # ERROR/INFO | TOPMOST
        except Exception:
            pass

    return 1 if isError else 0


###############################################################################
def _consoleLog(root):
    """Somewhere for the front end's stdout and stderr to go.

    A detached, windowless process has no console, so its standard handles are
    invalid: anything the app or a native library writes there is lost, and a
    stray print() can even raise. Point both at a file in the data directory
    instead -- truncated each launch, since this is a catch-all for output that
    is not already in the app's own logs.

    @param  root  The install root (holding datadir.txt on an installed build).
    @return       An open file object, or subprocess.DEVNULL.
    """
    dataDir = None
    try:
        with open(os.path.join(root, "datadir.txt"), "r") as f:
            dataDir = f.read().strip() or None
    except Exception:
        pass
    if not dataDir:
        localAppData = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
        if not localAppData:
            return subprocess.DEVNULL
        dataDir = os.path.join(localAppData, "Sighthound Video Py3")

    try:
        logDir = os.path.join(dataDir, "logs")
        os.makedirs(logDir, exist_ok=True)
        return open(os.path.join(logDir, "frontEndConsole.log"), "w")
    except Exception:
        return subprocess.DEVNULL


###############################################################################
def main(argv=None):
    """Launch the front end.

    @param  argv  Extra arguments passed through to the front end.
    @return       Process exit code.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    dryRun = "--dry-run" in argv
    if dryRun:
        argv.remove("--dry-run")

    root = _installRoot()

    if not os.path.isfile(os.path.join(root, _kRootMarker)):
        return _report(
            "%s does not look like a complete installation:\n\n%s\n\n"
            "Expected to find %s next to the program." %
            (kAppTitle, root, _kRootMarker), True, dryRun)

    interpreter = None
    for candidate in _kInterpreters:
        full = os.path.join(root, candidate)
        if os.path.isfile(full):
            interpreter = full
            break

    if interpreter is None:
        return _report(
            "%s cannot find its Python environment.\n\n"
            "Looked for:\n  %s\n\nunder:\n  %s\n\n"
            "Re-run the installer, or run Start.bat once to build the "
            "environment." %
            (kAppTitle, "\n  ".join(_kInterpreters), root), True, dryRun)

    # -s: ignore the per-user site directory (%APPDATA%\Python\...). The
    # installed runtime ships everything the app needs, and a stray package
    # there -- a second numpy, say -- would be picked up ahead of ours.
    cmd = [interpreter, "-s", "-m", _kEntryModule] + argv

    if dryRun:
        return _report("OK\nroot       : %s\ninterpreter: %s\ncommand    : %r"
                       % (root, interpreter, cmd), False, True)

    try:
        # DETACHED_PROCESS so closing the launcher never takes the app with it,
        # and the app outlives the shortcut's transient console.
        creationFlags = 0
        for flag in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            creationFlags |= getattr(subprocess, flag, 0)
        console = _consoleLog(root)
        subprocess.Popen(cmd, cwd=root, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=console, stderr=console,
                         creationflags=creationFlags)
    except Exception as e:
        return _report("%s failed to start:\n\n%r\n\nCommand:\n%s" %
                       (kAppTitle, e, " ".join(cmd)), True, dryRun)
    return 0


###############################################################################
if __name__ == "__main__":
    sys.exit(main())
