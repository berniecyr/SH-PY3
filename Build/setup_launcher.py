#*****************************************************************************
#
# setup_launcher.py
#   cx_Freeze build of SighthoundVideoPy3.exe (the installed front-end shortcut).
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" Build the launcher executable.

    venv\\Scripts\\python.exe build\\setup_launcher.py build

Output lands in build\\exe.win-amd64-3.12\\SighthoundVideoPy3.exe. The installer
copies that single exe into the install root; everything else it needs is the
tree and the venv already there.

This replaces frontEnd/setup-Win.py, which is py2exe on top of distutils and
cannot run at all on Python 3.12 (distutils was removed in 3.12).

Note this freezes build/launcher.py ONLY -- a stdlib-only script. The app itself
is deliberately not frozen; see the docstring in launcher.py for why.
"""

import os
import sys

from cx_Freeze import setup, Executable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from appCommon.CommonStrings import kVersionString, kOemName, kAppName


###############################################################################

_kIconCandidates = [
    os.path.join("icons", "SmartVideoApp.ico"),
    os.path.join("icons", "InstallerIcon-win.ico"),
]


def _findIcon():
    """@return  Path to an app icon, or None when none of the candidates exist."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in _kIconCandidates:
        full = os.path.join(root, candidate)
        if os.path.isfile(full):
            return full
    return None


###############################################################################

buildOptions = {
    # Nothing from the app is frozen, so keep the bundle minimal. Excluding the
    # heavyweights matters: cx_Freeze would otherwise notice them in the venv
    # and pull gigabytes of CUDA into a launcher that imports none of it.
    "excludes": [
        "tkinter", "unittest", "email", "html", "http", "xml", "pydoc_data",
        "wx", "numpy", "cv2", "torch", "torchvision", "ultralytics",
        "insightface", "onnxruntime", "nudenet", "PIL", "scipy", "skimage",
    ],
    "includes": [],
    "include_msvcr": True,
    "optimize": 1,
}

executables = [
    Executable(
        script=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "launcher.py"),
        base="Win32GUI",            # no console window
        target_name="SighthoundVideoPy3.exe",
        icon=_findIcon(),
        copyright="Copyright %s" % kOemName,
    ),
]

setup(
    name="SighthoundVideoPy3",
    version=kVersionString if kVersionString else "1.0",
    description="%s launcher" % kAppName,
    options={"build_exe": buildOptions},
    executables=executables,
)
