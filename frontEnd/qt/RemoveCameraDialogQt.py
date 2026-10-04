#!/usr/bin/env python

#*****************************************************************************
#
# RemoveCameraDialogQt.py
#
# PySide6 port of frontEnd/RemoveCameraDialog.py.
#
# The layout lives in ui/RemoveCameraDialog.ui and is edited in Qt Designer:
#
#     venv\Scripts\pyside6-designer.exe frontEnd\qt\ui\RemoveCameraDialog.ui
#
# Nothing is generated from that file -- it is parsed at run time -- so a
# layout change is live as soon as Designer saves it.  The contract between
# the two halves is the objectName of each widget; see _initUiWidgets().
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
import sys

# Common 3rd-party imports...
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)

# Toolbox imports...

# Local imports...
from appCommon.CommonStrings import kImportSuffix, kImportDisplaySuffix
from frontEnd.qt.QtCompat import (
    execModalOverWx, findWidget, getQApplication, keepWrappedTextVisible,
    loadUi, uiPath,
)


# Constants...
_kUiFileName = "RemoveCameraDialog.ui"

_kRemoveButtonStr = "Remove"

# NOTE: the wx original built this string from the raw camera name, without
# the kImportSuffix -> kImportDisplaySuffix swap that the "inactive camera"
# question below it (and the post-removal progress dialog in
# RemoveCameraDialog.removeCamera()) both apply.  That meant an *active*
# imported camera's confirmation prompt showed the internal " <imported>"
# marker instead of the friendly " (imported)" used everywhere else.  Fixed
# here by applying the same replace() in both branches; see _populateText().
_kActiveQuestionStr = (
    'Do you want to remove "%s" from your list of camera locations?'
)
_kHelpStr = (
    "Also delete all videos recorded at this location and rules used\n"
    "by this location\n\n"
    "(If you do not check this box, the camera location will continue to "
    "appear in the Search view as long as the video clips remain.)"
)
_kInactiveQuestionStr = (
    'Do you want to remove "%s" and all video and rules associated with '
    'this location?'
)


##############################################################################
class RemoveCameraDialog(QDialog):
    """A remove camera confirmation dialog."""

    ###########################################################
    def __init__(self, parent, cameraName, backEndClient):
        """RemoveCameraDialog constructor.

        @param  parent         Our parent widget, or None.
        @param  cameraName     The name of the camera being removed.
        @param  backEndClient  An object for communicating with the back end.
        """
        super().__init__(parent)

        self._activeCamera = \
                cameraName in backEndClient.getCameraLocations()

        # Matches the wx original: an inactive camera always deletes its
        # data (there's no checkbox offering a choice); an active one starts
        # out False until the user checks the box and hits Remove.
        self.deleteData = not self._activeCamera

        # Kept for callers/tests that want to know which camera this dialog
        # is talking about; kept current by _populateText().
        self._cameraName = cameraName

        # The .ui describes a QDialog, which is what makes Designer lay it out
        # and preview it as a dialog.  Loading it as our child would otherwise
        # give us a second floating window, because QDialog sets the
        # Qt::Dialog window flag in its constructor -- clearing the flags turns
        # it back into an ordinary child widget that we can embed.
        self._ui = loadUi(uiPath(_kUiFileName), self)
        self._ui.setWindowFlags(Qt.Widget)

        self.setWindowTitle(self._ui.windowTitle())

        mainLayout = QVBoxLayout(self)
        mainLayout.setContentsMargins(0, 0, 0, 0)
        mainLayout.addWidget(self._ui)

        self._initUiWidgets()
        self._populateText(cameraName)

        self.adjustSize()


    ###########################################################
    def _initUiWidgets(self):
        """Bind the widgets Designer created, and hook up their signals.

        Every name below is the "objectName" of a widget in the .ui file.  If
        you rename one in Designer you must rename it here too -- findWidget()
        will tell you exactly which name went missing if you forget.
        """
        self._questionLabel  = findWidget(self._ui, QLabel, "questionLabel")
        self._deleteCheckbox = findWidget(self._ui, QCheckBox, "deleteCheckbox")
        self._helpLabel      = findWidget(self._ui, QLabel, "helpLabel")
        self._buttonBox      = findWidget(self._ui, QDialogButtonBox, "buttonBox")

        # The .ui deliberately holds no signal/slot connections: wiring lives
        # here so that OnOK() cannot be bypassed by a connection someone adds
        # in Designer by accident.
        self._buttonBox.accepted.connect(self.OnOK)
        self._buttonBox.rejected.connect(self.reject)

        # wx let a click anywhere on the help text toggle the checkbox too
        # (OnToggleCheck); an event filter reproduces that without needing a
        # promoted widget class in Designer.
        self._helpLabel.installEventFilter(self)

        okButton = self._buttonBox.button(QDialogButtonBox.Ok)
        okButton.setText(_kRemoveButtonStr)
        okButton.setDefault(True)
        okButton.setAutoDefault(True)


    ###########################################################
    def _populateText(self, cameraName):
        """Fill in the parts of the dialog that depend on the camera name.

        @param  cameraName  The name of the camera being removed.
        """
        self._cameraName = cameraName

        # Imported cameras carry an internal " <imported>" marker on their
        # name; every user-visible message uses the friendly " (imported)"
        # form instead.  See the NOTE above _kActiveQuestionStr.
        displayName = cameraName.replace(kImportSuffix, kImportDisplaySuffix)

        if self._activeCamera:
            self._questionLabel.setText(_kActiveQuestionStr % (displayName,))
            self._helpLabel.setText(_kHelpStr)
        else:
            self._questionLabel.setText(_kInactiveQuestionStr % (displayName,))

            # No checkbox to offer when there's no active location to keep
            # around -- the wx original didn't create these widgets at all in
            # this case.
            self._deleteCheckbox.setVisible(False)
            self._helpLabel.setVisible(False)

        # Both labels word-wrap, so the dialog has to be told how tall their
        # text really is -- otherwise the layout is free to squeeze the last
        # line out of sight, and whether it does depends on how this
        # particular camera name happens to wrap.  See QtCompat.
        keepWrappedTextVisible(self._questionLabel, self._helpLabel)


    ###########################################################
    def eventFilter(self, obj, event):
        """Toggle the checkbox when the help text is clicked, like wx did.

        @param  obj      The watched object.
        @param  event    The event.
        @return handled  True if we handled the event ourselves.
        """
        if obj is self._helpLabel and event.type() in (
                QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            self._deleteCheckbox.setChecked(not self._deleteCheckbox.isChecked())
            self._deleteCheckbox.setFocus()
            return True
        return super().eventFilter(obj, event)


    ###########################################################
    def OnOK(self):
        """Respond to the user pressing the Remove button."""
        if self._activeCamera:
            self.deleteData = self._deleteCheckbox.isChecked()

        self.accept()


##############################################################################
def showRemoveCameraDialog(wxParent, cameraName, backEndClient):
    """Show the Qt remove-camera confirmation dialog from the wx front end.

    This is the entry point RemoveCameraDialog.removeCamera() uses; it hides
    the fact that the dialog is Qt from its wx caller.  The back-end call and
    the "waiting for data to be deleted" progress dialog stay in the wx
    wrapper, same as before -- this only replaces the confirmation prompt.

    @param  wxParent       The wx window to sit over, or None.
    @param  cameraName     The name of the camera to remove.
    @param  backEndClient  An object for communicating with the back end.
    @return deleteData     True or False for whether to also delete the
                           camera's recorded data, if the user confirmed
                           removal; None if the user canceled.
    """
    getQApplication()

    # No deleteLater() here on purpose: it only runs when a Qt event loop next
    # spins, and the front end has none between dialogs, so the dialog would
    # linger until the *next* one opened.  The dialog has no Qt parent, so it
    # belongs to Python, and dropping this reference on return frees it.
    dlg = RemoveCameraDialog(None, cameraName, backEndClient)
    if execModalOverWx(dlg, wxParent) != QDialog.Accepted:
        return None

    return dlg.deleteData


##############################################################################
def test_main():
    """OB_REDACT
       Contains various self-test code.
    """
    getQApplication()

    class _FakeBackEndClient(object):
        """A stand-in for BackEndClient, so this screen can be opened and
        poked at without a back end running.
        """
        def getCameraLocations(self):
            return ["Front door"]

    dlg = RemoveCameraDialog(None, "Front door", _FakeBackEndClient())
    if dlg.exec() == QDialog.Accepted:
        print("OK: deleteData=%r" % (dlg.deleteData,))
    else:
        print("Cancelled.")


##############################################################################
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_main()
    else:
        print("Try calling with 'test' as the argument.")
