#!/usr/bin/env python

#*****************************************************************************
#
# QtCompat.py
#
# Plumbing shared by every PySide6 screen, and the bridge that lets a Qt
# dialog be shown from the (still wx) front end.
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
import os
import sys

# Common 3rd-party imports...
from PySide6.QtCore import QEvent, QFile, QIODevice, QObject
from PySide6.QtGui import QWindow
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QApplication

# Toolbox imports...

# Local imports...
from appCommon.CommonStrings import kAppName


# Qt's "no maximum" sentinel (QWIDGETSIZE_MAX), which is what a widget reports
# when nothing has capped its width.
kUnboundedWidth = 16777215

# Directory holding the .ui files that Qt Designer edits.  From a source
# checkout that is simply the "ui" directory inside this package.
kUiDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# In a frozen build the Python modules live in a zip, so __file__ is not a real
# directory.  py2exe drops data_files relative to the dist dir -- i.e. next to
# the .exe -- so the .ui files come along as "frontEnd/qt/ui" from there.  See
# frontEnd/setup-Win.py; that entry still needs adding before a build ships a
# Qt screen.
_kFrozenUiDir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                             "frontEnd", "qt", "ui")


##############################################################################
def uiPath(uiFileName):
    """Return the full path of a .ui file belonging to this package.

    @param  uiFileName  Bare file name, e.g. "FtpSetupDialog.ui".
    @return path        Absolute path to the file.
    """
    if hasattr(sys, 'frozen'):
        frozenPath = os.path.join(_kFrozenUiDir, uiFileName)
        if os.path.isfile(frozenPath):
            return frozenPath

    return os.path.join(kUiDir, uiFileName)


##############################################################################
def getQApplication():
    """Return the process-wide QApplication, creating it if needed.

    Qt allows exactly one QApplication per process and does not survive having
    it destroyed and rebuilt, so we create it lazily and then keep it forever.
    That is safe to do from inside the running wx app: the two toolkits each
    own their own widgets, and on Windows they share the thread's message
    queue, so whichever event loop is spinning dispatches for both.

    @return app  The QApplication instance.
    """
    app = QApplication.instance()
    if app is None:
        # Qt parses argv for its own switches (-style, -platform, ...).  The
        # front end's argv is not meant for Qt, so hand it the program name
        # only.
        app = QApplication(sys.argv[:1])
        app.setApplicationName(kAppName)
        app.setOrganizationName(kAppName)

        # The front end never calls app.exec(), so there is no Qt main loop
        # running between dialogs; without this, closing the last Qt window
        # would try to quit the (non-existent) Qt application.
        app.setQuitOnLastWindowClosed(False)
    return app


##############################################################################
def loadUi(path, parent=None):
    """Load a Qt Designer .ui file and return the widget it describes.

    This is the run-time alternative to pyside6-uic: the XML is parsed on every
    launch, so a layout change saved in Designer shows up the next time the
    screen is opened, with no build step and no generated Python to keep in
    sync.

    @param  path    Full path to the .ui file (see uiPath()).
    @param  parent  Widget to load into, or None for a free-standing window.
    @return widget  The widget described by the file's top-level element.
    """
    uiFile = QFile(path)
    if not uiFile.open(QIODevice.ReadOnly):
        raise IOError("Cannot open UI file %s: %s" %
                      (path, uiFile.errorString()))

    loader = QUiLoader()
    try:
        widget = loader.load(uiFile, parent)
    finally:
        uiFile.close()

    if widget is None:
        raise RuntimeError("Cannot parse UI file %s: %s" %
                           (path, loader.errorString()))
    return widget


##############################################################################
def findWidget(root, widgetClass, objectName):
    """Look up a widget that Designer created, by the name set in Designer.

    Using this rather than a bare findChild() means that renaming a widget in
    Designer and forgetting to rename it here fails immediately, with a message
    naming the widget, instead of an AttributeError on None much later.

    @param  root         Widget returned by loadUi().
    @param  widgetClass  Expected class, e.g. QLineEdit.
    @param  objectName   The "objectName" shown in Designer's property editor.
    @return widget       The widget.
    """
    widget = root.findChild(widgetClass, objectName)
    if widget is None:
        raise RuntimeError(
            "UI file has no %s named %r (check the objectName in Qt Designer)"
            % (widgetClass.__name__, objectName)
        )
    return widget


##############################################################################
class _WrappedLabelFixer(QObject):
    """Keeps one word-wrapped label tall enough for the text it holds."""

    def eventFilter(self, label, event):
        if event.type() == QEvent.Type.Resize:
            needed = label.heightForWidth(label.width())
            if needed > 0 and label.minimumHeight() != needed:
                # Changing the minimum re-negotiates the parent layout, which
                # is the whole point: it is how the dialog learns it must grow.
                label.setMinimumHeight(needed)
        return False


##############################################################################
def keepWrappedTextVisible(*labels):
    """Stop word-wrapped labels from being squeezed until their text is cut off.

    Qt computes a layout's minimum height from each child's minimumSize, and
    minimumSize does NOT consult heightForWidth().  A word-wrapped QLabel is
    exactly the case where those disagree: its required height depends on the
    width it is given, so the layout happily makes a dialog too short and the
    last line of text simply disappears under whatever sits below it.  Worse,
    whether it happens at all depends on how the particular string wraps, so a
    dialog can look right for one camera name and be broken for another.

    This pins each label's minimumHeight to the height its text actually needs
    at its current width, and keeps it pinned as the width changes.

    Call it once, after the labels have been given their text.

    @param  labels  The word-wrapped QLabels to protect.
    """
    for label in labels:
        # Parent the filter to the label so it lives exactly as long.
        label.installEventFilter(_WrappedLabelFixer(label))

        # Called before the first layout pass, label.width() is still a
        # placeholder, and measuring against it pins a wildly too-tall minimum
        # that the dialog never shrinks back out of.  A capped width is the
        # honest answer to "how wide will this end up?"; fall back to the
        # current width only when there is no cap.
        width = label.width()
        if label.maximumWidth() < kUnboundedWidth:
            width = label.maximumWidth()

        needed = label.heightForWidth(width)
        if needed > 0:
            label.setMinimumHeight(needed)


##############################################################################
def _attachToWxParent(qtDialog, wxParent):
    """Make a Qt dialog behave like a child window of a wx window.

    Qt has no idea what a wx window is, but on Windows both are plain HWNDs, so
    we can wrap the wx window in a QWindow and hand that to Qt as the transient
    parent.  That is what keeps the dialog in front of the wx frame instead of
    letting it get lost behind it.

    Best effort: if any of this fails we show an unparented dialog, which is
    cosmetically worse but perfectly usable.

    @param  qtDialog  The QDialog about to be shown.
    @param  wxParent  A wx.Window, or None.
    """
    if wxParent is None:
        return

    try:
        handle = int(wxParent.GetHandle())
        if not handle:
            return

        # Qt only has a windowHandle() once the native window exists.
        qtDialog.winId()

        foreign = QWindow.fromWinId(handle)
        if foreign is None:
            return

        # Keep the wrapper alive as long as the dialog is; if Python collects
        # it while Qt still refers to it, Qt follows a dangling pointer.
        qtDialog._foreignParentWindow = foreign

        qtDialog.windowHandle().setTransientParent(foreign)
    except Exception:
        # Cosmetic only -- never let window parenting stop the dialog opening.
        pass


##############################################################################
def execModalOverWx(qtDialog, wxParent=None):
    """Run a Qt dialog modally from inside the wx front end.

    Qt's exec() spins its own event loop, which leaves wx's loop parked for the
    duration.  On Windows that still repaints and dispatches input for the wx
    windows -- both toolkits pull from the same thread message queue -- but
    work that only wx's own loop performs (idle events, wx timers) is suspended
    until the dialog closes.  That is fine for a short-lived modal settings
    dialog; it would not be fine for a long-lived one.

    We also disable the wx windows for the duration, which is what gives the
    "application modal" feel that wx's own ShowModal() provides.

    @param  qtDialog  The QDialog to run.
    @param  wxParent  wx.Window to sit over and disable, or None.
    @return result    QDialog.Accepted or QDialog.Rejected.
    """
    getQApplication()
    _attachToWxParent(qtDialog, wxParent)

    disabler = None
    try:
        import wx
        disabler = wx.WindowDisabler()
    except Exception:
        # Not running under wx (e.g. the standalone test harness) -- fine.
        pass

    try:
        return qtDialog.exec()
    finally:
        del disabler
