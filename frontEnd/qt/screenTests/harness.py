#!/usr/bin/env python

#*****************************************************************************
#
# harness.py
#
# Verification harness for the ported PySide6 screens.
#
# Run it after converting a screen, and before handing the conversion to
# anyone:
#
#     venv\Scripts\python.exe -m frontEnd.qt.screenTests.harness
#
# It exits non-zero if anything fails, so it can gate a batch of conversions.
#
# What it is for: a converted screen can import cleanly, compile cleanly and
# pass review while still being broken, because the two things that actually
# go wrong -- a widget objectName that does not match the .ui, and a layout
# that clips its contents -- are invisible to every check except building the
# widget and looking at it.  So this builds every screen for real and renders
# it to a PNG.
#
#*****************************************************************************
#
#
# Copyright 2013-2022 Sighthound, Inc.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
# Alternative licensing available from Sighthound, Inc.
# by emailing opensource@sighthound.com
#
# This file is part of the Sighthound Video project which can be found at
# https://github.com/sighthoundinc/SighthoundVideo
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; using version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02111, USA.
#
#
#*****************************************************************************

# Python imports...
import importlib
import os
import pkgutil
import sys
import threading
import traceback

# A screen that pops a modal dialog the harness did not anticipate would hang
# forever and wedge a build.  Nothing here should take more than a few seconds,
# so bail out loudly instead.
_kWatchdogSecs = 180.0

def _watchdog():
    sys.stderr.write(
        "\nHARNESS WATCHDOG: still running after %d seconds -- a screen is "
        "almost certainly blocked in a modal event loop.\n" % _kWatchdogSecs
    )
    sys.stderr.flush()
    os._exit(3)

_watchdogTimer = threading.Timer(_kWatchdogSecs, _watchdog)
_watchdogTimer.daemon = True    # must not keep the process alive on the way out
_watchdogTimer.start()

# Rendering uses the real platform plugin, because the offscreen one ships no
# fonts and draws every label as empty boxes -- which defeats the point of
# producing a PNG someone is meant to look at.  No window is ever shown:
# QWidget.grab() paints into a pixmap without mapping the widget.  Set
# SV_QT_HARNESS_OFFSCREEN=1 on a machine with no display (CI) to switch back.
if os.environ.get("SV_QT_HARNESS_OFFSCREEN", "") == "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Common 3rd-party imports...
from PySide6.QtCore import Qt, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QMessageBox

# Local imports...
from frontEnd.qt.QtCompat import getQApplication, loadUi, uiPath, kUiDir


# Where rendered screenshots land, for eyeballing a batch of conversions.
kRenderDir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_renders")

# How far a rendered screen may drift from its recorded size before the
# harness calls it a layout change.  A couple of pixels of font rounding is
# normal; anything more means the shape actually changed.
_kSizeSlackPx = 3

# Qt messages that are noise rather than a defect.
_kIgnoredQtMessages = (
    "QWindowsWindow::setGeometry",      # offscreen platform quirk
    "Cannot find font directory",       # offscreen platform ships no fonts
)


##############################################################################
class _Recorder(object):
    """Collects Qt warnings and the message boxes a screen tried to show."""

    def __init__(self):
        self.qtMessages = []
        self.messageBoxes = []

    def reset(self):
        del self.qtMessages[:]
        del self.messageBoxes[:]


_recorder = _Recorder()


##############################################################################
def _qtMessageHandler(msgType, context, message):
    """Capture Qt's own warnings so a screen cannot fail quietly."""
    if msgType in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg,
                   QtMsgType.QtFatalMsg):
        if not any(ignore in message for ignore in _kIgnoredQtMessages):
            _recorder.qtMessages.append(message)


##############################################################################
def _neutraliseMessageBoxes():
    """Stop QMessageBox from blocking, and record what it was asked to show.

    Every error path in a ported screen ends in a modal QMessageBox.  Left
    alone those block forever with no display attached, so the harness swaps
    them for recorders.  This is why a screen's validation can be exercised at
    all -- and it needs no cooperation from the screen, so it works for any
    conversion without a naming convention.
    """
    for name in ("critical", "warning", "information", "question", "about"):
        def recorder(*args, _name=name, **kwargs):
            # Signature is (parent, title, text, ...); text is what matters.
            text = args[2] if len(args) > 2 else ""
            title = args[1] if len(args) > 1 else ""
            _recorder.messageBoxes.append((_name, str(title), str(text)))
            return QMessageBox.StandardButton.Ok
        setattr(QMessageBox, name, staticmethod(recorder))


##############################################################################
def _discoverSpecs():
    """Import every screen spec module sitting next to this file.

    @return specs  List of (name, module), sorted by name.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    specs = []
    for _, name, _ in pkgutil.iter_modules([here]):
        if name.startswith("_") or name == "harness":
            continue
        module = importlib.import_module(
            "frontEnd.qt.screenTests.%s" % name)
        specs.append((name, module))
    return sorted(specs)


##############################################################################
def _findClippedWidgets(dlg):
    """Find widgets whose text does not fit in the space the layout gave them.

    Labels, buttons and checkboxes size themselves to their text and are not
    supposed to be squeezed below that, so anything narrower than its own
    sizeHint is being visibly cut off.  Input widgets are excluded: a
    QLineEdit shrinking below its hint is normal and not a defect.

    @param  dlg      The screen, already laid out.
    @return clipped  List of human-readable descriptions, empty if all is well.
    """
    from PySide6.QtWidgets import (
        QCheckBox, QLabel, QPushButton, QRadioButton,
    )

    clipped = []
    for widgetClass in (QLabel, QPushButton, QCheckBox, QRadioButton):
        for widget in dlg.findChildren(widgetClass):
            if not widget.isVisibleTo(dlg):
                continue

            name = widget.objectName() or "<unnamed>"

            # A wrapping label has no single sizeHint -- its height depends on
            # the width it ends up with, and its sizeHint reports the height
            # for some *other* width.  Comparing against that flags every
            # correctly-wrapped label, so ask what height it needs at the width
            # it actually got, and never judge its width at all.
            if isinstance(widget, QLabel) and widget.wordWrap():
                needed = widget.heightForWidth(widget.width())
                if needed > 0 and widget.height() < needed - 1:
                    clipped.append("%s %r needs %dpx of height at %dpx wide, "
                                   "has %d" % (widgetClass.__name__, name,
                                               needed, widget.width(),
                                               widget.height()))
                continue

            hint = widget.sizeHint()
            # One pixel of slack for font rounding.
            if (widget.width() < hint.width() - 1 or
                    widget.height() < hint.height() - 1):
                clipped.append("%s %r needs %dx%d, has %dx%d" % (
                    widgetClass.__name__, name,
                    hint.width(), hint.height(),
                    widget.width(), widget.height()))
    return clipped


##############################################################################
class _Result(object):
    """One screen's worth of pass/fail lines."""

    def __init__(self, name):
        self.name = name
        self.checks = []      # (label, ok, detail)

    def add(self, label, ok, detail=""):
        self.checks.append((label, bool(ok), detail))
        return ok

    @property
    def failed(self):
        return [c for c in self.checks if not c[1]]


##############################################################################
def _checkScreen(name, spec):
    """Run every check for one ported screen.

    @param  name    Spec module name, e.g. "FtpSetupDialog".
    @param  spec    The imported spec module.
    @return result  A _Result.
    """
    result = _Result(name)
    _recorder.reset()

    # 1. The .ui file exists and Qt can parse it on its own.
    path = uiPath(spec.kUiFile)
    if not result.add("ui file exists", os.path.isfile(path), path):
        return result
    try:
        standalone = loadUi(path, None)
        result.add("ui parses", standalone is not None)
        standalone.deleteLater()
    except Exception as e:
        result.add("ui parses", False, str(e))
        return result

    # 2. The screen builds.  This is the check that matters most: constructing
    #    the dialog runs every findWidget() lookup, so an objectName that was
    #    renamed in Designer but not in Python fails right here.
    try:
        dlg, sample = spec.build()

        # Lay the screen out exactly as showing it would, but never put a
        # window on screen.  This matters more than it looks: a widget that
        # has only been adjustSize()d has NOT been through a real layout pass,
        # and its children still report placeholder geometry -- which silently
        # made every geometry check below meaningless.  WA_DontShowOnScreen
        # gives a full layout with no visible window.
        dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dlg.show()
        getQApplication().processEvents()

        result.add("builds (all objectNames bind)", True)
    except Exception as e:
        result.add("builds (all objectNames bind)", False,
                   "%s: %s" % (type(e).__name__, e))
        return result

    # 3. Data survives the trip in and back out.
    try:
        got = spec.readBack(dlg)
        result.add("config round-trips", got == sample,
                   "" if got == sample else "got %r, wanted %r" % (got, sample))
    except Exception as e:
        result.add("config round-trips", False,
                   "%s: %s" % (type(e).__name__, e))

    # 4. Validation cases: each mutates the screen then says whether the
    #    result should be accepted or rejected.
    for label, mutate, expectValid in getattr(spec, "cases", lambda: [])():
        _recorder.messageBoxes[:] = []
        try:
            spec.reset(dlg, sample)
            mutate(dlg)
            isValid = spec.readBack(dlg) is not None
            shown = (_recorder.messageBoxes[0][2]
                     if _recorder.messageBoxes else "")
            result.add("case: %s" % label, isValid == expectValid,
                       "" if isValid == expectValid else
                       "expected %s" % ("accept" if expectValid else "reject"))
            if isValid == expectValid and not expectValid and not shown:
                result.add("case: %s explains itself" % label, False,
                           "rejected silently -- no message shown to the user")
        except Exception as e:
            result.add("case: %s" % label, False,
                       "%s: %s" % (type(e).__name__, e))

    # 4b. Free-form assertions, for screens whose behaviour is not a config
    #     dict -- a confirmation dialog has no "rejected" path for cases() to
    #     describe, but it still has rules worth pinning down.
    for label, check in getattr(spec, "checks", lambda: [])():
        try:
            spec.reset(dlg, sample)
            ok, detail = check(dlg)
            result.add("check: %s" % label, ok, detail)
        except Exception as e:
            result.add("check: %s" % label, False,
                       "%s: %s" % (type(e).__name__, e))

    # 5. Render it.  A layout that clips its contents passes every other check.
    try:
        spec.reset(dlg, sample)
        dlg.adjustSize()
        getQApplication().processEvents()
        if not os.path.isdir(kRenderDir):
            os.makedirs(kRenderDir)
        out = os.path.join(kRenderDir, "%s.png" % name)
        result.add("renders", dlg.grab().save(out),
                   "%dx%d -> %s" % (dlg.width(), dlg.height(), out))
    except Exception as e:
        result.add("renders", False, "%s: %s" % (type(e).__name__, e))

    # 5b. Nothing has its text cut off.
    try:
        clipped = _findClippedWidgets(dlg)
        result.add("nothing clipped", not clipped, "; ".join(clipped[:3]))
    except Exception as e:
        result.add("nothing clipped", False, "%s: %s" % (type(e).__name__, e))

    # 5c. The overall size still matches what was last eyeballed and approved.
    #     This is the check that catches a layout quietly changing shape --
    #     nothing else here notices a dialog that shrank.
    expected = getattr(spec, "kExpectedSize", None)
    if os.environ.get("SV_QT_HARNESS_OFFSCREEN", "") == "1":
        # Recorded sizes come from the real font stack; offscreen has none, so
        # its geometry is not comparable and the check would only cry wolf.
        result.add("layout size unchanged", True, "skipped (offscreen)")
    elif expected:
        okW = abs(dlg.width() - expected[0]) <= _kSizeSlackPx
        okH = abs(dlg.height() - expected[1]) <= _kSizeSlackPx
        result.add(
            "layout size unchanged", okW and okH,
            "" if (okW and okH) else
            "now %dx%d, spec says %dx%d -- look at the render, then update "
            "kExpectedSize if the change was intended"
            % (dlg.width(), dlg.height(), expected[0], expected[1]))
    else:
        result.add("layout size unchanged", False,
                   "spec has no kExpectedSize; set it to (%d, %d) once the "
                   "render looks right" % (dlg.width(), dlg.height()))

    # 6. Qt itself must not have complained.
    result.add("no Qt warnings", not _recorder.qtMessages,
               "; ".join(_recorder.qtMessages[:3]))

    dlg.deleteLater()
    return result


##############################################################################
def main():
    """Check every ported screen.  Returns a process exit code."""
    getQApplication()
    qInstallMessageHandler(_qtMessageHandler)
    _neutraliseMessageBoxes()

    specs = _discoverSpecs()
    if not specs:
        print("No screen specs found in frontEnd/qt/screenTests/.")
        return 1

    results = [_checkScreen(name, spec) for name, spec in specs]

    # Every layout in ui/ must have a spec, or a screen could ship untested.
    tested = set(spec.kUiFile for _, spec in specs
                 if hasattr(spec, "kUiFile"))
    onDisk = set(f for f in os.listdir(kUiDir) if f.endswith(".ui"))
    untested = sorted(onDisk - tested)

    for result in results:
        print("\n%s" % result.name)
        for label, ok, detail in result.checks:
            print("  [%s] %-38s %s" % ("PASS" if ok else "FAIL", label, detail))

    failures = sum(len(r.failed) for r in results)
    print("\n" + "=" * 70)
    print("%d screen(s) checked, %d check(s) failed" % (len(results), failures))
    if untested:
        print("UNTESTED LAYOUTS (add a spec for each): %s" % ", ".join(untested))
    print("renders: %s" % kRenderDir)

    return 1 if (failures or untested) else 0


##############################################################################
if __name__ == '__main__':
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 2

    # Qt's teardown from a bare script is unreliable, so leave abruptly -- but
    # flush first, because os._exit() does not.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
