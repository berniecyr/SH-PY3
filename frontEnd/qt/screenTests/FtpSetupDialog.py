#!/usr/bin/env python
#
# Verification spec for frontEnd/qt/FtpSetupDialogQt.py.
#
# THIS FILE IS THE TEMPLATE.  Copy it when porting a screen and change the
# five pieces below; harness.py picks it up automatically.  A spec needs:
#
#   kUiFile        the layout this screen loads
#   build()        construct the screen with sample data -> (dialog, sample)
#   readBack(dlg)  whatever the screen produces, or None if it rejected input
#   reset(dlg, s)  put the screen back to the sample state between cases
#   cases()        (label, mutate the screen, should it be accepted?)
#
# Keep the cases honest: one per rule the screen enforces, plus at least one
# that should be *accepted*, so a screen that rejects everything cannot pass.

# Local imports...
from frontEnd.qt.FtpSetupDialogQt import FtpSetupDialog


kUiFile = "FtpSetupDialog.ui"

# The rendered size last eyeballed and approved, from
# screenTests/_renders/FtpSetupDialog.png.  If the harness says this drifted,
# open that PNG: either the layout regressed, or the change was deliberate and
# this number needs updating.  Nothing else notices a screen changing shape.
kExpectedSize = (521, 256)

_kSample = {
    'host':      "ftp.example.com",
    'port':      21,
    'isPassive': True,
    'directory': "/sighthound/clips",
    'user':      "bernie",
    'password':  "hunter2",
}


###########################################################
def build():
    """Construct the screen with known-good data.

    @return (dialog, sample)  The screen, and the data it was given.
    """
    sample = dict(_kSample)
    return FtpSetupDialog(None, sample), sample


###########################################################
def readBack(dlg):
    """Read the screen's output.

    @param  dlg     The screen.
    @return config  The config dict, or None if the screen rejected its input.
    """
    return dlg.getFtpConfig()


###########################################################
def reset(dlg, sample):
    """Return the screen to the sample state, between validation cases.

    @param  dlg     The screen.
    @param  sample  The data build() used.
    """
    dlg._putFtpConfigToUi(sample)


###########################################################
def cases():
    """The rules this screen is supposed to enforce.

    @return cases  List of (label, mutate(dlg), shouldBeAccepted).
    """
    def blankAll(dlg):
        for field in (dlg._hostField, dlg._directoryField, dlg._userField,
                      dlg._passwordField, dlg._verifyPasswordField):
            field.setText("")

    return [
        ("good settings accepted",
         lambda dlg: None, True),

        ("mismatched passwords",
         lambda dlg: dlg._verifyPasswordField.setText("nope"), False),

        ("port above 65535",
         lambda dlg: dlg._portCombo.setCurrentText("99999"), False),

        ("port below 1",
         lambda dlg: dlg._portCombo.setCurrentText("0"), False),

        ("empty port",
         lambda dlg: dlg._portCombo.setCurrentText(""), False),

        ("non-numeric port",
         lambda dlg: dlg._portCombo.setCurrentText("ftp"), False),

        # The host goes on the wire as ASCII.
        ("non-ascii host",
         lambda dlg: dlg._hostField.setText("ftp.éxample.com"), False),

        ("host with no directory",
         lambda dlg: dlg._directoryField.setText(""), False),

        ("directory with no host",
         lambda dlg: dlg._hostField.setText(""), False),

        # Blank is how the user turns FTP off, so it has to be allowed.
        ("everything blank",
         blankAll, True),

        # A 5-digit port must still be readable -- this caught a combo that
        # was sized for "21" and clipped anything longer.
        ("5-digit port survives round trip",
         lambda dlg: dlg._portCombo.setCurrentText("65535"), True),
    ]
