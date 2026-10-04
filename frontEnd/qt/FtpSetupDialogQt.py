#!/usr/bin/env python

#*****************************************************************************
#
# FtpSetupDialogQt.py
#
# PySide6 port of frontEnd/FtpSetupDialog.py.
#
# The layout lives in ui/FtpSetupDialog.ui and is edited in Qt Designer:
#
#     venv\Scripts\pyside6-designer.exe frontEnd\qt\ui\FtpSetupDialog.ui
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
import ftplib
import io
import socket
import sys
import time

# Common 3rd-party imports...
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLineEdit,
    QMessageBox, QProgressDialog, QPushButton, QVBoxLayout,
)

# Toolbox imports...

# Local imports...
from appCommon.CommonStrings import kAppName
from frontEnd.qt.QtCompat import (
    execModalOverWx, findWidget, getQApplication, loadUi, uiPath,
)


# Constants...
_kUiFileName = "FtpSetupDialog.ui"

_kHostDescStr = "FTP Server"
_kBadCharErrorTitleStr = "FTP settings"
_kBadCharErrorStr = "The '%s' field cannot contain the '%s' character."

_kPasswordsMustMatchStr = "The two password fields must match."
_kPasswordsMustMatchTitleStr = "FTP settings"

_kBadPortErrorTitleStr = "FTP settings"
_kBadPortErrorStr = "Invalid FTP port number: \"%s\"."
_kNoPortErrorStr = "You must specify an FTP port number."

_kBadSettingsErrorTitleStr = "FTP settings"
_kBadSettingsErrorStr = (
    """You must at least specify a server and a directory."""
)

_kTestProgressTitle = "Uploading test file"

_kTestErrorTitleStr = "FTP settings"
_kTestErrorStr = "There was a problem while %s:\n\n%s"

_kLoginErrorTitleStr = "FTP settings"
_kLoginErrorStr = \
    "Login to server failed. Please check your user ID and password.\n\n%s"

_kUnknownHostErrorTitleStr = "FTP settings"
_kUnknownHostErrorStr = "FTP server not found: %s"

_kSuccessTitleStr = "FTP settings"
_kSuccessStr = "Success! A test file has been uploaded to your FTP server."

_kTestFtpFileContents = (
    "%s uploaded this test file to your FTP server at %%(timeNow)s."
) % (kAppName)

_kTestFtpFileName = kAppName + " Test.txt"

# Limit timeout to 30 seconds, 10 wasn't always enough on Windows.
_kSocketTimeout = 30.0

_kOpeningConnection   = (0, "opening connection to %s:%s...")
_kLoggingIn           = (1, "logging in...")
_kChangingDirectories = (2, "changing directories...")
_kUploadingFile       = (3, "uploading...")
_kDone                = (4, "done uploading")
_kNumProgressSteps    =  4
_kProgressInitialSpacing = (
    "                                                                          "
)


##############################################################################
class FtpSetupDialog(QDialog):
    """A dialog for setting up ftp upload."""

    ###########################################################
    def __init__(self, parent, ftpConfig):
        """FtpSetupDialog constructor.

        @param  parent     Our parent widget, or None.
        @param  ftpConfig  See BackEndPrefs for details.
        """
        super().__init__(parent)

        self._oldFtpConfig = dict(ftpConfig)

        # The .ui describes a QDialog, which is what makes Designer lay it out
        # and preview it as a dialog.  Loading it as our child would otherwise
        # give us a second floating window, because QDialog sets the
        # Qt::Dialog window flag in its constructor -- clearing the flags turns
        # it back into an ordinary child widget that we can embed.
        self._ui = loadUi(uiPath(_kUiFileName), self)
        self._ui.setWindowFlags(Qt.Widget)

        self.setWindowTitle(self._ui.windowTitle())

        hostLayout = QVBoxLayout(self)
        hostLayout.setContentsMargins(0, 0, 0, 0)
        hostLayout.addWidget(self._ui)

        self._initUiWidgets()
        self._putFtpConfigToUi(ftpConfig)

        self.adjustSize()


    ###########################################################
    def _initUiWidgets(self):
        """Bind the widgets Designer created, and hook up their signals.

        Every name below is the "objectName" of a widget in the .ui file.  If
        you rename one in Designer you must rename it here too -- findWidget()
        will tell you exactly which name went missing if you forget.
        """
        self._hostField           = findWidget(self._ui, QLineEdit, "hostField")
        self._directoryField      = findWidget(self._ui, QLineEdit, "directoryField")
        self._userField           = findWidget(self._ui, QLineEdit, "userField")
        self._passwordField       = findWidget(self._ui, QLineEdit, "passwordField")
        self._verifyPasswordField = findWidget(self._ui, QLineEdit, "verifyPasswordField")
        self._portCombo           = findWidget(self._ui, QComboBox, "portCombo")
        self._passiveCheckbox     = findWidget(self._ui, QCheckBox, "passiveCheckbox")
        self._testButton          = findWidget(self._ui, QPushButton, "testButton")
        self._buttonBox           = findWidget(self._ui, QDialogButtonBox, "buttonBox")

        # The .ui deliberately holds no signal/slot connections: wiring lives
        # here so that the validation in OnOK() cannot be bypassed by a
        # connection someone adds in Designer by accident.
        self._buttonBox.accepted.connect(self.OnOK)
        self._buttonBox.rejected.connect(self.reject)
        self._testButton.clicked.connect(self.OnTestUpload)

        okButton = self._buttonBox.button(QDialogButtonBox.Ok)
        okButton.setDefault(True)
        okButton.setAutoDefault(True)


    ###########################################################
    def _showError(self, title, message):
        """Show a modal error box, the equivalent of wx.ICON_ERROR.

        @param  title    Title bar text.
        @param  message  Body text.
        """
        QMessageBox.critical(self, title, message)


    ###########################################################
    def getFtpConfig(self, isBlankOk=True):
        """Read out an ftp configuration dict from our UI.

        @param  isBlankOk  If True, it's OK for settings to be "blank"
        @return ftpConfig  See BackEndPrefs for details; may be None if an
                           error was found (and reported to the user).
        """
        # Init config with the old one.  This allows us to save settings that
        # are in the default, but that we can't (currently) configure via the
        # UI...
        ftpConfig = dict(self._oldFtpConfig)

        # The host has to survive being put on the wire as ASCII.  Unlike the
        # wx version we keep it as str rather than storing the encoded bytes:
        # BackEndPrefs defaults it to "", and ResponseRunner hands it straight
        # to ftplib, which is happy with either.
        hostText = self._hostField.text()
        try:
            hostText.encode('ascii', 'strict')
        except UnicodeEncodeError as e:
            self._hostField.setFocus()
            self._hostField.selectAll()
            self._showError(_kBadCharErrorTitleStr,
                            _kBadCharErrorStr % (_kHostDescStr,
                                                 e.object[e.start:e.start+1]))
            return None
        ftpConfig['host'] = hostText.strip()

        # Get fields that are OK to be unicode.
        ftpConfig['directory'] = self._directoryField.text().strip()
        ftpConfig['user'] = self._userField.text().strip()
        ftpConfig['password'] = self._passwordField.text().strip()
        verifyPassword = self._verifyPasswordField.text().strip()

        if ftpConfig['password'] != verifyPassword:
            self._verifyPasswordField.setFocus()
            self._verifyPasswordField.setText("")
            self._showError(_kPasswordsMustMatchTitleStr,
                            _kPasswordsMustMatchStr)
            return None

        # Convert the port--it must be an int and within range...
        portText = self._portCombo.currentText().strip()
        try:
            port = int(portText)
            if (port < 1) or (port > 65535):
                raise ValueError()
        except ValueError:
            if portText:
                self._showError(_kBadPortErrorTitleStr,
                                _kBadPortErrorStr % (portText,))
            else:
                self._showError(_kBadPortErrorTitleStr, _kNoPortErrorStr)
            return None
        ftpConfig['port'] = port

        ftpConfig['isPassive'] = bool(self._passiveCheckbox.isChecked())

        # If they've specified something, they must specify host / directory.
        host = ftpConfig['host']
        directory = ftpConfig['directory']
        user = ftpConfig['user']
        password = ftpConfig['password']
        if (not isBlankOk) or host or directory or user or password:
            if not (host and directory):
                self._showError(_kBadSettingsErrorTitleStr,
                                _kBadSettingsErrorStr)
                return None

        # If we're here, we're OK...
        return ftpConfig


    ###########################################################
    def _putFtpConfigToUi(self, ftpConfig):
        """The opposite of getFtpConfig().

        @param  ftpConfig  See BackEndPrefs for details.
        """
        self._hostField.setText(ftpConfig.get('host', ""))
        self._directoryField.setText(ftpConfig.get('directory', ""))
        self._userField.setText(ftpConfig.get('user', ""))
        self._passwordField.setText(ftpConfig.get('password', ""))
        self._verifyPasswordField.setText(ftpConfig.get('password', ""))

        # The combo is editable, so setCurrentText() both selects a matching
        # item and fills the edit field when the port is a non-standard one.
        self._portCombo.setCurrentText(str(ftpConfig.get('port', "")))

        self._passiveCheckbox.setChecked(bool(ftpConfig.get('isPassive', True)))

        # No equivalent of the wx version's fixSelection() call is needed:
        # that worked around a Mac wx quirk where text fields opened with
        # their contents selected.


    ###########################################################
    def OnOK(self):
        """Respond to the user pressing OK."""
        # Get the ftp config; if it's completely invalid, we'll get back
        # None (and the user will already have been shown an error message).
        ftpConfig = self.getFtpConfig()
        if ftpConfig is None:
            return

        self.accept()


    ###########################################################
    def _updateProgress(self, progressDlg, stage, msg):
        """Advance the test-upload progress dialog one step.

        @param  progressDlg    The QProgressDialog.
        @param  stage          Step number, 0.._kNumProgressSteps.
        @param  msg            Label to show.
        @return wantContinue   False if the user hit Cancel.
        """
        progressDlg.setLabelText(msg.capitalize())
        progressDlg.setValue(stage)

        # The FTP calls below block this thread, so nothing repaints unless we
        # ask for it.  (This is the same trade the wx version made; a proper
        # fix is to move the transfer to a worker thread.)
        QApplication.processEvents()

        return not progressDlg.wasCanceled()


    ###########################################################
    def OnTestUpload(self):
        """Respond to the user pressing the "Test" button."""
        # Get the ftp config; if it's completely invalid, we'll get back
        # None (and the user will already have been shown an error message).
        ftpConfig = self.getFtpConfig(False)
        if ftpConfig is None:
            return

        loggedIn = False

        progressDlg = QProgressDialog(_kProgressInitialSpacing, "Cancel",
                                      0, _kNumProgressSteps, self)
        progressDlg.setWindowTitle(_kTestProgressTitle)
        progressDlg.setWindowModality(Qt.ApplicationModal)
        progressDlg.setMinimumDuration(0)
        progressDlg.setAutoClose(True)
        progressDlg.setAutoReset(True)

        ftpObj = ftplib.FTP(timeout=_kSocketTimeout)
        try:
            stage, msg = _kOpeningConnection
            msg = msg % (ftpConfig['host'], ftpConfig['port'])
            if not self._updateProgress(progressDlg, stage, msg):
                return
            ftpObj.connect(ftpConfig['host'], int(ftpConfig['port']))

            stage, msg = _kLoggingIn
            if not self._updateProgress(progressDlg, stage, msg):
                return
            ftpObj.login(ftpConfig['user'], ftpConfig['password'])
            loggedIn = True

            # Set to passive mode if user wants it...
            ftpObj.set_pasv(ftpConfig['isPassive'])

            stage, msg = _kChangingDirectories
            if not self._updateProgress(progressDlg, stage, msg):
                return
            ftpObj.cwd(ftpConfig['directory'])

            stage, msg = _kUploadingFile
            if not self._updateProgress(progressDlg, stage, msg):
                return

            # NOTE: the wx version built this with io.StringIO, which cannot
            # work on Python 3 -- storlines() compares each line against the
            # bytes b"\r\n" and raises TypeError on a str, so the Test button
            # always failed.  Bytes are what storlines() wants.
            fileToSend = io.BytesIO(
                (_kTestFtpFileContents % {'timeNow': time.asctime()})
                .encode('utf-8')
            )
            ftpObj.storlines('STOR %s' % _kTestFtpFileName, fileToSend)

            stage, msg = _kDone
            self._updateProgress(progressDlg, stage, msg)
        except socket.gaierror:
            progressDlg.close()
            progressDlg = None
            self._showError(_kUnknownHostErrorTitleStr,
                            _kUnknownHostErrorStr % ftpConfig['host'])
            return
        except ftplib.error_perm as e:
            progressDlg.close()
            progressDlg = None
            if not loggedIn:
                self._showError(_kLoginErrorTitleStr, _kLoginErrorStr % str(e))
            else:
                self._showError(_kTestErrorTitleStr,
                                _kTestErrorStr % (msg.rstrip('.'), str(e)))
            return
        except Exception as e:
            progressDlg.close()
            progressDlg = None
            self._showError(_kTestErrorTitleStr,
                            _kTestErrorStr % (msg.rstrip('.'), str(e)))
            return
        finally:
            try:
                ftpObj.quit()
            except Exception:
                # Ignore errors to quit, just in case...
                pass

            if progressDlg is not None:
                progressDlg.close()

        QMessageBox.information(self, _kSuccessTitleStr, _kSuccessStr)


##############################################################################
def showFtpSetupDialog(wxParent, ftpConfig):
    """Show the Qt FTP dialog from the wx front end.

    This is the entry point ResponseConfigPanel uses; it hides the fact that
    the dialog is Qt from its wx caller.

    @param  wxParent   The wx window to sit over, or None.
    @param  ftpConfig  See BackEndPrefs for details.
    @return ftpConfig  The new config if the user hit OK, else None.
    """
    getQApplication()

    # No deleteLater() here on purpose: it only runs when a Qt event loop next
    # spins, and the front end has none between dialogs, so the dialog would
    # linger until the *next* one opened.  The dialog has no Qt parent, so it
    # belongs to Python, and dropping this reference on return frees it.
    dlg = FtpSetupDialog(None, ftpConfig)
    if execModalOverWx(dlg, wxParent) != QDialog.Accepted:
        return None

    return dlg.getFtpConfig()


##############################################################################
def test_main():
    """OB_REDACT
       Contains various self-test code.
    """
    getQApplication()

    # A stand-in for BackEndClient.getFtpSettings(), so that this screen can be
    # opened and poked at without a back end running.
    ftpSettings = {
        'host':      "",
        'port':      21,
        'isPassive': True,
        'directory': "",
        'user':      "",
        'password':  "",
    }

    dlg = FtpSetupDialog(None, ftpSettings)
    if dlg.exec() == QDialog.Accepted:
        print("OK: %r" % (dlg.getFtpConfig(),))
    else:
        print("Cancelled.")


##############################################################################
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_main()
    else:
        print("Try calling with 'test' as the argument.")
