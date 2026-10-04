#!/usr/bin/env python
#
# Verification spec for frontEnd/qt/RemoveCameraDialogQt.py.
#
# This screen is a confirmation prompt, not a settings form: there is no input
# to reject, so cases() is empty and the real assertions live in checks().
# See FtpSetupDialog.py for the config-form shape.

# Local imports...
from appCommon.CommonStrings import kImportDisplaySuffix, kImportSuffix
from frontEnd.qt.QtCompat import getQApplication
from frontEnd.qt.RemoveCameraDialogQt import RemoveCameraDialog


kUiFile = "RemoveCameraDialog.ui"

# See FtpSetupDialog.py for what this is and when to change it.  401px wide
# matches the wx original, which created the dialog at 400 and wrapped its text
# at 350 -- the two maximumSize properties in the .ui are what reproduce that.
kExpectedSize = (404, 184)

_kActiveCameraName = "Front door"
_kInactiveCameraName = "Old shed"


##############################################################################
class _FakeBackEndClient(object):
    """Stands in for BackEndClient, which needs a running back end."""

    def __init__(self, locations):
        self._locations = list(locations)

    def getCameraLocations(self):
        return self._locations


###########################################################
def _activeDialog(cameraName=_kActiveCameraName):
    """Build a dialog for a camera that is still an active location."""
    return RemoveCameraDialog(
        None, cameraName, _FakeBackEndClient([cameraName]))


###########################################################
def _inactiveDialog(cameraName=_kInactiveCameraName):
    """Build a dialog for a camera that is no longer an active location."""
    return RemoveCameraDialog(None, cameraName, _FakeBackEndClient([]))


###########################################################
def build():
    """Construct the screen in its ordinary state: an active camera.

    @return (dialog, sample)  The screen, and what readBack() should produce.
    """
    # An active camera starts with the box unchecked, so confirming without
    # touching it must keep the recorded video.
    return _activeDialog(), {'deleteData': False}


###########################################################
def readBack(dlg):
    """Confirm the dialog and report what it decided.

    Goes through OnOK() rather than reading the checkbox directly, so the
    decision logic is what gets exercised.

    @param  dlg      The screen.
    @return outcome  Dict describing the decision.
    """
    dlg.OnOK()
    return {'deleteData': dlg.deleteData}


###########################################################
def reset(dlg, sample):
    """Return the screen to the state build() left it in.

    @param  dlg     The screen.
    @param  sample  The data build() produced.
    """
    dlg._deleteCheckbox.setChecked(False)
    dlg.deleteData = not dlg._activeCamera
    dlg._populateText(_kActiveCameraName)


###########################################################
def cases():
    """No input to validate -- see checks() instead."""
    return []


###########################################################
def checks():
    """The rules this screen is supposed to enforce.

    @return checks  List of (label, fn(dlg) -> (ok, detail)).
    """
    def uncheckedKeepsData(dlg):
        dlg._deleteCheckbox.setChecked(False)
        dlg.OnOK()
        ok = dlg.deleteData is False
        return ok, "" if ok else "deleteData=%r, wanted False" % (dlg.deleteData,)

    def checkedDeletesData(dlg):
        dlg._deleteCheckbox.setChecked(True)
        dlg.OnOK()
        ok = dlg.deleteData is True
        return ok, "" if ok else "deleteData=%r, wanted True" % (dlg.deleteData,)

    def activeShowsChoice(dlg):
        # An active location can be kept in the Search view, so the user is
        # offered the choice.
        shown = dlg._deleteCheckbox.isVisibleTo(dlg)
        return shown, "" if shown else "checkbox missing for an active camera"

    def inactiveHidesChoice(_dlg):
        # An inactive camera has nothing to keep, so the wx original did not
        # build the checkbox at all and always deleted the data.
        dlg = _inactiveDialog()
        hidden = not dlg._deleteCheckbox.isVisibleTo(dlg)
        always = dlg.deleteData is True
        ok = hidden and always
        return ok, "" if ok else (
            "checkboxHidden=%r deleteData=%r, wanted True/True"
            % (hidden, dlg.deleteData))

    def inactiveWordingDiffers(_dlg):
        dlg = _inactiveDialog()
        text = dlg._questionLabel.text()
        ok = "all video and rules" in text
        return ok, "" if ok else "unexpected wording: %r" % (text,)

    def importedNameIsFriendly(_dlg):
        # The wx original showed the internal marker here, unlike everywhere
        # else in the flow.  Both branches must now use the friendly form.
        name = "Driveway" + kImportSuffix
        for label, dlg in (("active", _activeDialog(name)),
                           ("inactive", _inactiveDialog(name))):
            text = dlg._questionLabel.text()
            if kImportSuffix in text:
                return False, "%s branch leaked %r into %r" % (
                    label, kImportSuffix, text)
            if kImportDisplaySuffix not in text:
                return False, "%s branch dropped %r from %r" % (
                    label, kImportDisplaySuffix, text)
        return True, ""

    def helpTextClickTogglesBox(dlg):
        # wx bound EVT_LEFT_DOWN on the help text to the checkbox.
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        before = dlg._deleteCheckbox.isChecked()
        event = QMouseEvent(QEvent.Type.MouseButtonPress,
                            QPointF(5, 5), QPointF(5, 5),
                            Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        dlg.eventFilter(dlg._helpLabel, event)
        after = dlg._deleteCheckbox.isChecked()
        ok = after != before
        return ok, "" if ok else "checkbox did not toggle on a help-text click"

    def noNameLengthClipsText(_dlg):
        # This is the regression that shipped: both labels word-wrap, so how
        # much height they need depends on the camera name, and the dialog
        # looked correct for "Front door" while cutting the last line off for
        # "z_test2".  One name proves nothing -- sweep a range.
        from PySide6.QtCore import Qt

        names = ["A", "z_test2", "Front door", "Back Yard Camera",
                 "Driveway North East Corner Camera Number Four",
                 "z_test2" + kImportSuffix]

        for isActive in (True, False):
            for name in names:
                dlg = _activeDialog(name) if isActive else _inactiveDialog(name)
                # Lay out as showing would, without a visible window.
                dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                dlg.show()
                getQApplication().processEvents()

                for label, tag in ((dlg._questionLabel, "question"),
                                   (dlg._helpLabel, "help")):
                    if not label.isVisibleTo(dlg):
                        continue
                    needed = label.heightForWidth(label.width())
                    if needed > 0 and label.height() < needed - 1:
                        return False, (
                            "%s label cut off by %dpx for %r (%s camera)"
                            % (tag, needed - label.height(), name,
                               "active" if isActive else "inactive"))
                dlg.close()
        return True, ""

    def removeButtonIsLabelled(dlg):
        from PySide6.QtWidgets import QDialogButtonBox
        text = dlg._buttonBox.button(QDialogButtonBox.Ok).text()
        ok = text == "Remove"
        return ok, "" if ok else "OK button says %r, wanted 'Remove'" % (text,)

    return [
        ("unchecked box keeps recorded video", uncheckedKeepsData),
        ("checked box deletes recorded video", checkedDeletesData),
        ("active camera is offered the choice", activeShowsChoice),
        ("inactive camera always deletes", inactiveHidesChoice),
        ("inactive camera is worded differently", inactiveWordingDiffers),
        ("imported name shown in friendly form", importedNameIsFriendly),
        ("clicking help text toggles the box", helpTextClickTogglesBox),
        ("no camera name clips the text", noNameLengthClipsText),
        ("OK button is labelled Remove", removeButtonIsLabelled),
    ]
