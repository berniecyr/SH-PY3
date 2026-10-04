#!/usr/bin/env python

#*****************************************************************************
#
# ResponseConfigPanel.py
#
#
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
import copy
import os
import shlex
import shutil
import subprocess
from subprocess import Popen, PIPE
import sys
import time
import re
import json
import wave

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.path.PathUtils import existsInPath
from vitaToolbox.dictUtils.OrderedDict import OrderedDict
from vitaToolbox.wx.FixedTimeCtrl import FixedTimeCtrl, EVT_TIMEUPDATE
from vitaToolbox.wx.FontUtils import makeFontDefault
from vitaToolbox.wx.TextCtrlUtils import setHyperlinkColors
from vitaToolbox.wx.AutoWrapStaticText import AutoWrapStaticText
from vitaToolbox.wx.FileBrowseButtonFixed import DirBrowseButton
from vitaToolbox.wx.FileBrowseButtonFixed import FileBrowseButton
from vitaToolbox.strUtils.EnsureUnicode import ensureUtf8

# Local imports...
from appCommon.CommonStrings import kCommandResponse, kCommandResponseLookup
from appCommon.CommonStrings import kEmailResponse
from appCommon.CommonStrings import kIftttResponse
from appCommon.CommonStrings import kWebhookResponse
from appCommon.CommonStrings import kPushResponse
from appCommon.CommonStrings import kRecordResponse
from appCommon.CommonStrings import kSoundResponse
from appCommon.CommonStrings import kFtpResponse
from appCommon.CommonStrings import kLocalExportResponse
from appCommon.CommonStrings import kSnapshotResponse
from appCommon.CommonStrings import kIHostResponse
from appCommon.CommonStrings import kTapoResponse
from appCommon.CommonStrings import kDefaultPreRecord
from appCommon.CommonStrings import kIftttHelpUrl
from appCommon.CommonStrings import kFrontEndLogName
from appCommon.InstallPaths import getSoundsDir
from appCommon.InstallPaths import portableSoundPath
from appCommon.InstallPaths import resolveSoundPath
from backEnd.RealTimeRule import _kDefaultRuleSchedule
from appCommon.LicenseUtils import hasPaidEdition
from frontEnd.EmailSetupDialog import EmailSetupDialog
from frontEnd.FtpSetupDialog import FtpSetupDialog
from frontEnd.OptionsDialog import OptionsDialog
from frontEnd.FrontEndUtils import promptUserIfRemotePathEvtHandler
from .ConfigPanel import ConfigPanel
import backEnd.IHostConfig as IHostCfg

# Constants...

_kPanelTitle = "If seen"

# The flow chart blocks the error messages below send the user back to, named
# as the chart titles them.  Saving and acting were two tabs ("Save clips" and
# "Take actions") of a single "If seen" block until that block was split in
# two, and the messages kept pointing at the tabs.
_kSaveBlockName = "Save clip"
_kActionBlockName = "Take action"

# The three pages this panel shows, one per flow chart block.  The values are
# also the component names the construction view stores in the query's
# "last edited" field, so don't rename them without a migration.
kSchedulePage = 'schedule'
kSaveClipPage = 'saveClip'
kTakeActionPage = 'takeAction'

_kPageTitles = {
    kSchedulePage:   "Action schedule",
    kSaveClipPage:   _kPanelTitle,
    kTakeActionPage: "Take action",
}

_kPageIcons = {
    kSchedulePage:   "frontEnd/bmps/clock_small.png",
    kSaveClipPage:   "frontEnd/bmps/Block_Icon_If_Seen.png",
    kTakeActionPage: "frontEnd/bmps/Block_Icon_Mult.png",
}

# Tapo camera siren / spotlight.  These act on the rule's own camera, so there
# is no camera to pick; the account is global (Options -> Tapo).
_kTapoBoxLabel = "Tapo camera"
_kTapoSirenStr = "Sound this camera's siren"
_kTapoLightStr = "Switch on this camera's spotlight"
_kTapoHintStr = (
    "Acts on the camera this rule watches.  Sign in under "
    "Tools → Options → Tapo first.\n"
    "The camera stops the siren and the spotlight on its own timers.")

_kSaveHelpStr = (
"""Mark this clip to be saved on:"""
)

_kExportHelpStr = (
"""Export this clip to:"""
)

_kSaveHelp2Str = (
"""Tip: You can create new rules to find events in video that has already """
"""been recorded."""
)

_kSettingsButtonLabel = "Settings..."

_kRunCommandStr = "Run the command:"
_kRunTestStr = "Test"

_kWebhookStr = "Execute a webhook:"

_kRecordEventStr = "My computer"

_kFtpStr = "My FTP server"
_kLocalStr = "A local folder"
_kSnapshotStr = "Save a snapshot (events folder)"

_kSendEmailStr = "Send an email"
_kPlaySoundStr = "Play this sound:"
_kIftttStr = "Send an "
_kIftttHelpStr = "IFTTT event"
_kIftttTestStr = "Test"
_kIHostStr = "Send iHost command"

_kCustomSoundLabel = "Custom"

# TTS voice labels (UI display) → Kokoro voice codes
_kTtsVoiceMap = {
    'Heart':  'af_heart',
    'Nicole': 'af_nicole',
    'Dora':   'ef_dora',
    'Emma':   'bf_emma',
    'Onyx':   'am_onyx',
}
_kTtsVoiceNames = list(_kTtsVoiceMap.keys())
_kTtsVoiceCodeToName = {v: k for k, v in _kTtsVoiceMap.items()}


_kNoEmailAddrTitleStr = "Email notification"
_kNoEmailAddrStr = (
"""You have selected email notification for this rule without providing an """
"""email address to send the alerts.  Return to the Rule Editor and enter """
"""an address, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kSendEmailStr, _kActionBlockName)

_kBadEmailAddrTitleStr = "Email notification"
_kBadEmailAddrStr = (
"""You have selected email notification for this rule but the email address """
"""to send the alerts contains an invalid character (%%s).  Return to the """
"""Rule Editor and correct the address, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kSendEmailStr, _kActionBlockName)

_kNoEmailAccountTitleStr = "Email notification"
_kNoEmailAccountStr = (
"""You have selected email notification for this rule but have not provided """
"""your email account information.  Return to the Rule Editor and enter """
"""settings, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kSendEmailStr, _kActionBlockName)

_kPushRegFailedTitleStr = "Mobile notification"
_kPushRegFailedStr = (
"""Your computer could not be registered for mobile notifications. Please """
"""ensure that you have internet connectivity and try again."""
)

_kNoCommandTitleStr = "Command notification"
_kNoCommandStr = (
"""You have selected to run a custom command as a notification for this rule """
"""but have not provided the command to execute.  Return to the Rule Editor """
"""and enter a command, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kRunCommandStr, _kActionBlockName)

_kCommandErrorTitleStr = "Command notification"
_kCommandErrorStr = (
"""There was an error executing the command.  Please check that the path and """
"""parameters are correct and try again."""
)
_kCommandNotFoundErrorStr = (
"""The command was not found.  Please check that the path is """
"""correct and try again."""
)

_kFtpErrorTitleStr = "FTP response"
_kFtpErrorStr = (
"""You have selected to upload video clips saved by this rule """
"""but have not provided FTP site information.  Return to the Rule Editor """
"""and enter settings, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kFtpStr, _kSaveBlockName)

_kLocalExportSelectTitle = "Local Export"
_kLocalExportSelectLabel = (
"""Click Browse to select a directory in which to export clips matching """
"""this rule."""
)

_kPathDoesntExistTitle = "Path doesn't exist"
_kPathDoesntExistLabel = "The specified directory does not exist."

_kLocalExportErrorTitle = "Local export response"
_kLocalExportErrorLabel = (
"""You have selected to export video clips saved by this rule to a local """
"""directory but that directory does not exist. Return to the Rule Editor """
"""and enter settings, or clear the checkbox labeled "%s" in the "%s" """
"""block."""
) % (_kLocalStr, _kSaveBlockName)

_kSnapshotErrorTitle = "Snapshot response"
_kNoSnapshotFolderStr = (
"""You have selected to save a snapshot for this rule but have not chosen """
"""the folder to save it in.  Return to the Rule Editor and click Browse to """
"""pick a folder, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kSnapshotStr, _kSaveBlockName)

_kBadSnapshotFolderStr = (
"""You have selected to save a snapshot for this rule but the folder "%%s" """
"""does not exist.  Return to the Rule Editor and choose an existing """
"""folder, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kSnapshotStr, _kSaveBlockName)

_kIHostErrorTitle = "iHost command"
_kNoIHostDeviceStr = (
"""You have selected to send an iHost command for this rule but have not """
"""chosen the device to control.  Return to the Rule Editor and click """
"""Edit... to pick a device, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kIHostStr, _kActionBlockName)

_kIftttErrorTitle = "IFTTT event"
_kNoIftttSettingsStr = (
"""You have selected to send an IFTTT event for this rule but have not """
"""entered your IFTTT Webhooks key and event name.  Return to the Rule """
"""Editor and click Settings... to enter them, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kIftttStr + _kIftttHelpStr, _kActionBlockName)

_kWebhookErrorTitle = "Webhook response"
_kNoWebhookStr = (
"""You have selected to execute a webhook for this rule but have not """
"""entered the webhook URL.  Return to the Rule Editor and click Edit... """
"""to enter it, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kWebhookStr, _kActionBlockName)

_kBadWebhookStr = (
"""You have selected to execute a webhook for this rule but it is not set """
"""up correctly:\n\n%%s\n\nReturn to the Rule Editor and click Edit... to """
"""correct it, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kWebhookStr, _kActionBlockName)

_kSoundErrorTitle = "Sound response"
_kNoSoundFileStr = (
"""You have selected to play a sound for this rule but have not chosen the """
"""sound file.  Return to the Rule Editor and pick a sound from the list or """
"""click Browse to choose a .wav file, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kPlaySoundStr, _kActionBlockName)

_kMissingSoundFileStr = (
"""You have selected to play a sound for this rule but the sound file "%%s" """
"""does not exist.  Return to the Rule Editor and pick a sound from the list """
"""or click Browse to choose a .wav file, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kPlaySoundStr, _kActionBlockName)

_kBadSoundFileStr = (
"""You have selected to play a sound for this rule but "%%s" is not a """
"""sound file this program can play.  Return to the Rule Editor and pick a """
"""sound from the list or click Browse to choose a .wav file, """
"""or clear the checkbox labeled "%s" in the "%s" block."""
) % (_kPlaySoundStr, _kActionBlockName)

kTextPlain = "text/plain"
kApplicationJSON = "application/json"

##############################################################################
def _isPlayableSound(path):
    """Return True if path is a sound file a rule can play.

    Rules play sounds with SoundResponse.playSound, which reads the file with
    the wave module, and casting to a speaker sends it as audio/wav -- so a
    file only plays if it is a WAV that the wave module can open.  Anything
    else (an .mp3, say) is saved without complaint and then silently fails
    every time the rule fires.

    @param  path        The path to check.
    @return isPlayable  True if the file opens as a WAV.
    """
    try:
        with wave.open(path, 'rb') as waveFile:
            return waveFile.getnchannels() > 0 and waveFile.getframerate() > 0
    except Exception:
        return False


##############################################################################
def _getDefaultSounds():
    """Scan the app's sounds folder, <install>\\sounds, for the sounds to list.

    Bells, Person Detected and Ping ship there, and any playable .wav dropped
    in beside them is listed the next time the Rule Editor opens.

    @return sounds  A dict of display name -> absolute path, in name order.
                    The display name is the file name without ".wav", which
                    is what rules saved before this scan store as their
                    soundName ("Bells", "Person Detected", "Ping").
    """
    soundDir = getSoundsDir()
    try:
        fileNames = os.listdir(soundDir)
    except OSError:
        return {}

    sounds = {}
    for fileName in sorted(fileNames, key=lambda s: s.lower()):
        name, ext = os.path.splitext(fileName)
        if ext.lower() != '.wav':
            continue
        # A file called Custom.wav must not hide the Custom choice.
        if name.lower() == _kCustomSoundLabel.lower():
            name = fileName
        path = os.path.join(soundDir, fileName)
        if os.path.isfile(path) and _isPlayableSound(path):
            sounds[name] = path
    return sounds


##############################################################################
def _validateWebhook(uri, contentType, content):
    if contentType == kApplicationJSON:
        # validate JSON
        try:
            json.loads(content)
        except Exception as e:
            return ("Please make sure your input is valid JSON!\n" +
                    "Error: " + str(e) + "\n" +
                    "JSON: " + content)
    elif contentType != kTextPlain:
        return "Unsupported content type: " + contentType

    # Django checker, borrowed from https://stackoverflow.com/questions/7160737/python-how-to-validate-a-url-in-python-malformed-or-not
    regex = re.compile(
            r'^(?:http)s?://' # http:// or https://
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|' #domain...
            r'localhost|' #localhost...
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})' # ...or ip
            r'(?::\d+)?' # optional port
            r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    if not re.match(regex, uri):
        return "Please specify a valid webhook URL."

    return None

##############################################################################
class WebhookEditor(wx.Dialog):
    kSupportedTypes = [
        kTextPlain,
        kApplicationJSON
    ]
    ###########################################################
    def __init__(self, parent, uri, contentType, content):
        """WebhookEditor constructor.

        @param  parent         Our parent UI element.
        """
        # Call our super
        super(WebhookEditor, self).__init__(
            parent, title="Data to send ...",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self._contentTypeCtrl = wx.Choice(self, choices=self.kSupportedTypes)
        self._contentTypeCtrl.SetSelection( 1 if contentType == kApplicationJSON else 0 )
        contentTypeLabel = wx.StaticText(self, -1, "Send as")

        self._descField = wx.TextCtrl(self, -1, "", style=wx.TE_MULTILINE)
        self._descField.SetMinSize((100, 200))
        self._descField.SetValue( content )
        # self._descField.OSXEnableAutomaticQuoteSubstitution(False)
        self._descField.SetFocus()

        mainSizer = wx.BoxSizer(wx.VERTICAL)

        webhookUriLabel = wx.StaticText(self, -1, "URI")
        self._webhookUriField = wx.TextCtrl(self, -1)
        self._webhookUriField.SetMinSize((100, -1))
        self._webhookUriField.SetValue(uri)

        hint = wx.StaticText(self, -1, "You can use the following substitution variables:\n{SvRuleName}, {SvCameraName}, {SvEventTime},\n{SvRuleLookFor}, {SvRuleFace}")

        uriSizer = wx.BoxSizer(wx.HORIZONTAL)
        uriSizer.Add(webhookUriLabel, 0, wx.ALIGN_CENTER_VERTICAL)
        uriSizer.Add(self._webhookUriField, 1, wx.ALIGN_CENTER_VERTICAL)

        choicesSizer = wx.BoxSizer(wx.HORIZONTAL)
        choicesSizer.Add(contentTypeLabel, 0, wx.ALIGN_CENTER_VERTICAL)
        choicesSizer.Add(self._contentTypeCtrl, 1, wx.ALIGN_CENTER_VERTICAL)

        mainSizer.Add(uriSizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        mainSizer.Add(choicesSizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        mainSizer.Add(hint, 0, wx.EXPAND | wx.BOTTOM, 10)
        mainSizer.Add(self._descField, 1, wx.EXPAND)

        buttonSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        borderSizer = wx.BoxSizer(wx.VERTICAL)
        borderSizer.Add(mainSizer, 1, wx.EXPAND | wx.ALL, 12)
        borderSizer.Add(buttonSizer, 0, wx.EXPAND | wx.BOTTOM, 12)

        self.SetSizer(borderSizer)

        self.Bind(wx.EVT_BUTTON, self.OnOK, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self.OnCancel, id=wx.ID_CANCEL)

        self.CenterOnParent()

    ###########################################################
    def Content(self):
        content = self._descField.GetValue()
        # Replacing fancy UTF-8 quotes, which unfortunately aren't a valid JSON ...
        # Reference:
        # https://www.cl.cam.ac.uk/~mgk25/ucs/quotes.html
        # https://stackoverflow.com/questions/28977618/how-to-convert-utf-8-fancy-quotes-to-neutral-quotes
        content = re.sub('\u201c','"',content)
        content = re.sub('\u201d','"',content)
        # And we'll do a '-', too, but only because Kevin ran into it
        content = re.sub('\u2014','-',content)
        # Blanket replace things we do not understand with question marks.
        # Must decode back to str \u2014 encode() returns bytes in Python 3 which
        # breaks all downstream str.replace() calls (variable substitution).
        content = content.encode('ascii', 'replace').decode('ascii')
        return content

    ###########################################################
    def URI(self):
        return self._webhookUriField.GetValue()

    ###########################################################
    def ContentType(self):
        selection = self._contentTypeCtrl.GetSelection()
        return self._contentTypeCtrl.GetString(selection)

    ###########################################################
    def OnOK(self, event):
        error = _validateWebhook(self.URI(), self.ContentType(), self.Content() )
        if error is not None:
            wx.MessageBox(error, "Invalid webhook params.", wx.OK | wx.ICON_ERROR, self)
            return

        self.EndModal(wx.OK)

    ###########################################################
    def OnCancel(self, event):
        self.EndModal(wx.CANCEL)


##############################################################################
class IHostEditor(wx.Dialog):
    """Per-rule iHost command editor: device (by name), command, auto-off, night."""

    # (label shown to user, value stored in config)
    kCommands = [("Turn on", "on"), ("Turn off", "off"), ("Toggle", "toggle")]

    ###########################################################
    def __init__(self, parent, deviceId, deviceName, command, timeout, nightOnly):
        super(IHostEditor, self).__init__(
            parent, title="iHost command",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        # Cached device list from the global iHost settings.
        self._devices = IHostCfg.loadConfig().get("devices", []) or []

        mainSizer = wx.BoxSizer(wx.VERTICAL)

        # --- Device picker (by name) ---
        devRow = wx.BoxSizer(wx.HORIZONTAL)
        devRow.Add(wx.StaticText(self, -1, "Device:"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._deviceChoice = wx.Choice(
            self, -1, choices=[d.get("name", "") for d in self._devices])
        devRow.Add(self._deviceChoice, 1, wx.ALIGN_CENTER_VERTICAL)
        self._refreshButton = wx.Button(self, -1, "Refresh")
        devRow.Add(self._refreshButton, 0, wx.LEFT, 6)
        mainSizer.Add(devRow, 0, wx.EXPAND | wx.BOTTOM, 10)

        # Preselect the stored device (match by id, else by name).
        for i, d in enumerate(self._devices):
            if d.get("id") == deviceId or (deviceName and
                                           d.get("name") == deviceName):
                self._deviceChoice.SetSelection(i)
                break

        if not self._devices:
            hint = wx.StaticText(
                self, -1,
                "No devices cached yet.  Set the hub IP + token in\n"
                "Options -> iHost, then click Refresh.")
            hint.SetForegroundColour(wx.Colour(120, 120, 120))
            mainSizer.Add(hint, 0, wx.EXPAND | wx.BOTTOM, 10)

        # --- Command ---
        cmdRow = wx.BoxSizer(wx.HORIZONTAL)
        cmdRow.Add(wx.StaticText(self, -1, "Command:"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._commandChoice = wx.Choice(
            self, -1, choices=[label for label, _ in self.kCommands])
        commands = [c for _, c in self.kCommands]
        self._commandChoice.SetSelection(
            commands.index(command) if command in commands else 0)
        cmdRow.Add(self._commandChoice, 0, wx.ALIGN_CENTER_VERTICAL)
        mainSizer.Add(cmdRow, 0, wx.EXPAND | wx.BOTTOM, 10)

        # --- Auto-off timeout ---
        toRow = wx.BoxSizer(wx.HORIZONTAL)
        toRow.Add(wx.StaticText(self, -1, "Auto-off after:"), 0,
                  wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._timeoutCtrl = wx.SpinCtrl(self, -1, str(int(timeout or 0)),
                                        min=0, max=86400, size=(80, -1))
        toRow.Add(self._timeoutCtrl, 0, wx.ALIGN_CENTER_VERTICAL)
        toRow.Add(wx.StaticText(self, -1,
                                "seconds  (0 = stay on; applies to 'Turn on')"),
                  0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        mainSizer.Add(toRow, 0, wx.EXPAND | wx.BOTTOM, 10)

        # --- Night only ---
        self._nightOnlyCheck = wx.CheckBox(self, -1, "Only at night")
        self._nightOnlyCheck.SetValue(bool(nightOnly))
        mainSizer.Add(self._nightOnlyCheck, 0, wx.BOTTOM, 10)

        buttonSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        borderSizer = wx.BoxSizer(wx.VERTICAL)
        borderSizer.Add(mainSizer, 1, wx.EXPAND | wx.ALL, 12)
        borderSizer.Add(buttonSizer, 0, wx.EXPAND | wx.BOTTOM, 12)
        self.SetSizer(borderSizer)
        self.Fit()

        self._refreshButton.Bind(wx.EVT_BUTTON, self.OnRefresh)
        self.Bind(wx.EVT_BUTTON, self.OnOK, id=wx.ID_OK)
        self.CenterOnParent()

    ###########################################################
    def OnRefresh(self, event):
        """Fetch the hub's device list and cache it in the global settings."""
        import appCommon.IHostClient as IHostClient
        cfg = IHostCfg.loadConfig()
        ip = cfg.get("ip", "")
        token = cfg.get("token", "")
        if not ip or not token:
            wx.MessageBox("Set the hub IP and token in Options -> iHost first.",
                          "iHost not configured",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        try:
            devices = IHostClient.listDevices(ip, token)
        except Exception as e:
            wx.MessageBox("Could not reach the hub:\n%s" % e,
                          "Refresh failed", wx.OK | wx.ICON_ERROR, self)
            return

        cfg["devices"] = [{"name": d["name"], "id": d["id"]} for d in devices]
        IHostCfg.saveConfig(cfg)
        self._devices = cfg["devices"]

        prevName = self._deviceChoice.GetStringSelection()
        self._deviceChoice.Set([d["name"] for d in self._devices])
        if prevName:
            idx = self._deviceChoice.FindString(prevName)
            if idx != wx.NOT_FOUND:
                self._deviceChoice.SetSelection(idx)
        wx.MessageBox("Found %d device(s)." % len(self._devices),
                      "Refresh", wx.OK | wx.ICON_INFORMATION, self)

    ###########################################################
    def OnOK(self, event):
        if self._deviceChoice.GetSelection() == wx.NOT_FOUND:
            wx.MessageBox("Please choose a device.", "No device selected",
                          wx.OK | wx.ICON_ERROR, self)
            return
        self.EndModal(wx.ID_OK)

    ###########################################################
    def GetValues(self):
        """Return the edited selection as a dict."""
        idx = self._deviceChoice.GetSelection()
        device = self._devices[idx] if 0 <= idx < len(self._devices) else {}
        _, command = self.kCommands[self._commandChoice.GetSelection()]
        return {
            "device":    device.get("id", ""),
            "name":      device.get("name", ""),
            "command":   command,
            "timeout":   self._timeoutCtrl.GetValue(),
            "nightOnly": self._nightOnlyCheck.GetValue(),
        }


##############################################################################
class ResponseConfigPanel(ConfigPanel):
    """The block configuration panel for a camera."""

    ###########################################################
    def __init__(self, parent, dataModel, backEndClient, dataMgr,
                 initialSchedule=None):
        """ResponseConfigPanel constructor.

        @param  parent           Our parent UI element.
        @param  dataModel        The SavedQueryDataModel.
        @param  backEndClient    Client to the back end.
        @param  dataMgr          The data manager for the app.
        @param  initialSchedule  Optional schedule dict to seed the schedule
                                 controls with (used when duplicating a rule,
                                 whose backend copy doesn't exist yet under the
                                 new name).  When None, the schedule is loaded
                                 from the backend rule by name (edit case) or
                                 defaults (new rule).
        """
        # Call our super
        super(ResponseConfigPanel, self).__init__(parent)

        # Keep track of params...
        self._dataModel = dataModel
        self._backEndClient = backEndClient
        self._dataMgr = dataMgr
        self._initialSchedule = initialSchedule

        # Create a logger
        self._logger = getLogger(kFrontEndLogName)

        self._toggledIfttt = False
        self._iftttKey = ''
        self._iftttEventName = ''
        self._hasPaidVersion = hasPaidEdition(backEndClient.getLicenseData())

        # Load schedule data before building UI (day checkboxes need _schedActiveDays).
        self._initSchedule()

        # Create our UI elements...
        # Three sibling panels, one shown at a time, driven by the flow chart's
        # Schedule / Save clip / Take action blocks.  These used to be two tabs
        # of a wx.Notebook under a single "If seen" block, with the Action
        # Schedule crammed into the bottom of the second one -- which left no
        # room to add anything.  Everything about how the controls are saved and
        # loaded is unchanged: this class still owns all of them, because
        # OnUiChange writes every response at once and _handleModelChange reads
        # them all back.
        savePanel = wx.Panel(self, -1)

        saveHelpLabel = wx.StaticText(savePanel, -1, _kSaveHelpStr)

        self._recordCheckbox = wx.CheckBox(savePanel, -1, _kRecordEventStr)
        self._recordSettingsButton = wx.Button(savePanel, -1,
                                               _kSettingsButtonLabel)

        self._snapshotCheckbox = wx.CheckBox(savePanel, -1, _kSnapshotStr)
        self._snapshotFolderField = DirBrowseButton(savePanel, -1, labelText='',
                changeCallback=self.OnSnapshotFolderChange)
        self._snapshotSubfolderLabel = wx.StaticText(savePanel, -1, "Optional subfolder:")
        self._snapshotSubfolderField = wx.TextCtrl(savePanel, -1, "")
        self._snapshotBboxCheckbox = wx.CheckBox(savePanel, -1, "Include bounding box")

        if self._hasPaidVersion:
            self._ftpCheckbox = wx.CheckBox(savePanel, -1, _kFtpStr)
            self._ftpSettingsButton = wx.Button(savePanel, -1,
                                                _kSettingsButtonLabel)

            self._localExportField = DirBrowseButton(savePanel, -1, labelText='',
                    changeCallback=self.OnLocalExportConfig)
            self._localExportCheckbox = wx.CheckBox(savePanel, -1, _kLocalStr)

        saveHelp2Label = AutoWrapStaticText(savePanel, -1, _kSaveHelp2Str)
        makeFontDefault(saveHelp2Label)
        saveHelp2Label.SetMinSize((1, saveHelp2Label.GetBestSize()[1]))

        actionPanel = wx.Panel(self, -1)
        schedPanel = wx.Panel(self, -1)

        self._emailCheckbox = wx.CheckBox(actionPanel, -1, _kSendEmailStr)
        self._emailSettingsButton = wx.Button(actionPanel, -1,
                                              _kSettingsButtonLabel)
        if self._hasPaidVersion:
            self._iftttCheckbox = wx.CheckBox(actionPanel, -1, _kIftttStr)
            iftttHelpLink = wx.adv.HyperlinkCtrl(actionPanel, wx.ID_ANY, _kIftttHelpStr, kIftttHelpUrl)
            setHyperlinkColors(iftttHelpLink)
            self._iftttSettingsButton = wx.Button(actionPanel, -1, _kSettingsButtonLabel)
            self._iftttTestButton = wx.Button(actionPanel, -1, _kIftttTestStr)

            self._webhookCheckbox = wx.CheckBox(actionPanel, -1, _kWebhookStr)
            self._webhookEditButton = wx.Button(actionPanel, -1, "Edit...")

            self._commandCheckbox = wx.CheckBox(actionPanel, -1,
                                                _kRunCommandStr)
            self._commandField = wx.TextCtrl(actionPanel, -1)
            self._commandTestButton = wx.Button(actionPanel, -1, _kRunTestStr)

        # ── Sound + Speak-text response, boxed together (like Action Schedule).
        # These controls are parented to the StaticBox so the frame draws around
        # them correctly on MSW.
        soundBox = wx.StaticBox(actionPanel, -1, "Sound")

        self._soundCheckbox = wx.CheckBox(soundBox, -1, _kPlaySoundStr)
        # Every playable sound in the app's sounds folder, by name, then Custom.
        self._defaultSounds = _getDefaultSounds()
        choices = list(self._defaultSounds.keys())
        choices.append(_kCustomSoundLabel)
        self._soundChoice = wx.Choice(soundBox, -1, choices=choices)
        self._soundChoice.SetSelection(0)
        self._soundChoice.SetMinSize((1, -1))       # Needed to keep sizing OK.
        # Shows the full path of whichever sound is chosen; only editable for
        # Custom.
        self._customSoundField = FileBrowseButton(soundBox, -1, labelText='',
                                                  fileMask="*.wav",
                                                  startDirectory=getSoundsDir(),
                                                  changeCallback=
                                                  self.OnCustomSoundChange)
        self._customSoundField.SetMinSize((1, -1))  # Needed to keep sizing OK.
        # The last path chosen under Custom, so trying a sound from the list
        # and then going back to Custom gives the user's own file back.
        self._lastCustomSoundPath = ''

        # Where to play the sound: local machine or the shared network speaker.
        # Starts its own radio group (RB_GROUP), independent of the TTS group.
        self._soundOnLabel    = wx.StaticText(soundBox, -1, "Play on:")
        self._soundLocalRadio = wx.RadioButton(soundBox, -1, "Local",
                                               style=wx.RB_GROUP)
        self._soundCcRadio    = wx.RadioButton(soundBox, -1, "Speaker")

        # ── TTS controls (children of soundBox, see above) ──────────────────
        self._ttsCheckbox   = wx.CheckBox(soundBox, -1, "Speak text:")
        self._ttsTextField  = wx.TextCtrl(soundBox, -1, "Alert")
        self._ttsTextField.SetToolTip(
            "Substitution variables: {SvRuleName}, {SvCameraName}, "
            "{SvEventTime}, {SvRuleLookFor}, {SvRuleFace}")
        self._ttsVoiceLabel = wx.StaticText(soundBox, -1, "Voice:")
        self._ttsVoiceChoice = wx.Choice(soundBox, -1, choices=_kTtsVoiceNames)
        self._ttsVoiceChoice.SetSelection(0)
        self._ttsSpeedLabel = wx.StaticText(soundBox, -1, "Speed:")
        self._ttsSpeedCtrl  = wx.SpinCtrlDouble(soundBox, -1, value="1.0",
                                                 min=0.5, max=2.0, inc=0.1,
                                                 size=(65, -1))
        self._ttsSpeedCtrl.SetDigits(1)
        # Per-rule cooldown (minutes, fractional) for the whole Sound response
        # (played sound and/or spoken text).  0 = fire every time.
        self._ttsCooldownLabel = wx.StaticText(soundBox, -1,
                                               "Repeat at most once every:")
        self._ttsCooldownCtrl  = wx.SpinCtrlDouble(soundBox, -1, value="0.0",
                                                   min=0.0, max=1440.0, inc=0.5,
                                                   size=(70, -1))
        self._ttsCooldownCtrl.SetDigits(1)
        self._ttsCooldownCtrl.SetToolTip(
            "Minutes between firings of this sound action (the played sound "
            "and/or spoken text) when the rule triggers repeatedly "
            "(0 = every time; 0.5 = 30 seconds; 1440 = once a day).  Does not "
            "affect other actions such as webhook or email.")
        self._ttsCooldownUnits = wx.StaticText(soundBox, -1, "min")
        # Where to speak: local machine or the shared network speaker.  Starts
        # its own radio group (RB_GROUP), independent of the sound group above.
        self._ttsOnLabel    = wx.StaticText(soundBox, -1, "Speak on:")
        self._ttsLocalRadio = wx.RadioButton(soundBox, -1, "Local",
                                              style=wx.RB_GROUP)
        self._ttsCcRadio    = wx.RadioButton(soundBox, -1, "Speaker")
        self._ttsTestButton = wx.Button(soundBox, -1, "Test")
        self._ttsTestButton.SetMinSize((80, -1))
        # ── Shared network speaker (used by "Play on" and "Speak on" whenever
        # either selects Speaker).  Named _ttsCc* for history; shared now. ──
        self._speakerLabel  = wx.StaticText(soundBox, -1, "Speaker:")
        self._ttsCcIpCtrl   = wx.TextCtrl(soundBox, -1, "", size=(130, -1))
        # Discover speakers on the network to pick from (fills the IP field).
        self._ttsSearchButton = wx.Button(soundBox, -1, "Search")
        self._ttsSearchButton.SetMinSize((70, -1))
        # Wide enough to show "Friendly name (192.168.x.x)" without clipping.
        self._ttsDeviceChoice = wx.Choice(soundBox, -1, size=(280, -1))

        self._ttsVarsHint = wx.StaticText(soundBox, -1,
            "Type any of these into the text; each is replaced when the rule "
            "fires:\n"
            "{SvRuleName}, {SvCameraName}, {SvEventTime}, {SvRuleLookFor}, {SvRuleFace}")
        makeFontDefault(self._ttsVarsHint)
        self._ttsVarsHint.SetForegroundColour(wx.Colour(120, 120, 120))

        # ── iHost smart-home command (ungated) ──────────────────────────────
        self._ihostCheckbox = wx.CheckBox(actionPanel, -1, _kIHostStr)
        self._ihostEditButton = wx.Button(actionPanel, -1, "Edit...")

        # ── Tapo camera siren / spotlight, boxed like the Sound group ───────
        # These act on the rule's OWN camera, so there is nothing to pick.
        tapoBox = wx.StaticBox(actionPanel, -1, _kTapoBoxLabel)
        self._tapoSirenCheckbox = wx.CheckBox(tapoBox, -1, _kTapoSirenStr)
        self._tapoLightCheckbox = wx.CheckBox(tapoBox, -1, _kTapoLightStr)
        self._tapoHint = wx.StaticText(tapoBox, -1, _kTapoHintStr)
        makeFontDefault(self._tapoHint)
        self._tapoHint.SetForegroundColour(wx.Colour(120, 120, 120))

        # One page per flow chart block; setActivePage picks which is shown.
        self._pagePanels = {
            kSchedulePage:   schedPanel,
            kSaveClipPage:   savePanel,
            kTakeActionPage: actionPanel,
        }
        self._activePage = kTakeActionPage

        # Throw our stuff into our sizer...
        mainSizer = wx.BoxSizer(wx.VERTICAL)
        for _page in (kSchedulePage, kSaveClipPage, kTakeActionPage):
            mainSizer.Add(self._pagePanels[_page], 1, wx.EXPAND | wx.TOP, 5)

        saveSizer = wx.BoxSizer(wx.VERTICAL)
        saveSizer.Add(saveHelpLabel, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 10)

        saveGSizer = wx.FlexGridSizer(rows=0, cols=2, vgap=12, hgap=5)
        saveGSizer.AddGrowableCol(0)

        saveGSizer.Add(self._recordCheckbox, 0, wx.ALIGN_CENTER_VERTICAL)
        saveGSizer.Add(self._recordSettingsButton, 0, wx.ALIGN_CENTER_VERTICAL)
        saveGSizer.Add(self._snapshotCheckbox, 0, wx.ALIGN_CENTER_VERTICAL)
        saveGSizer.AddSpacer(1)
        saveSizer.Add(saveGSizer, 0, wx.EXPAND | wx.BOTTOM, 5)
        self._snapshotFolderField.SetMinSize((1, -1))
        saveSizer.Add(self._snapshotFolderField, 0, wx.EXPAND)
        snapshotSubSizer = wx.BoxSizer(wx.HORIZONTAL)
        snapshotSubSizer.Add(self._snapshotSubfolderLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        snapshotSubSizer.Add(self._snapshotSubfolderField, 1, wx.EXPAND)
        saveSizer.Add(snapshotSubSizer, 0, wx.EXPAND | wx.TOP, 3)
        saveSizer.Add(self._snapshotBboxCheckbox, 0, wx.TOP, 3)
        saveSizer.AddSpacer(5)

        exportGSizer = wx.FlexGridSizer(rows=0, cols=2, vgap=12, hgap=5)
        exportGSizer.AddGrowableCol(0)
        if self._hasPaidVersion:
            exportHelpLabel = wx.StaticText(savePanel, -1, _kExportHelpStr)
            saveSizer.Add(exportHelpLabel, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 10)
            exportGSizer.Add(self._ftpCheckbox, 0, wx.ALIGN_CENTER_VERTICAL)
            exportGSizer.Add(self._ftpSettingsButton, 0, wx.ALIGN_CENTER_VERTICAL)
            exportGSizer.Add(self._localExportCheckbox, 0, wx.ALIGN_CENTER_VERTICAL)
            exportGSizer.AddSpacer(1)

        saveSizer.Add(exportGSizer, 0, wx.EXPAND | wx.BOTTOM, 12)
        if self._hasPaidVersion:
            self._localExportField.SetMinSize((1, -1))
            saveSizer.Add(self._localExportField, 0, wx.EXPAND)
            saveSizer.AddSpacer(10)

        saveSizer.Add(saveHelp2Label, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 5)

        saveBorderSizer = wx.BoxSizer()
        saveBorderSizer.Add(saveSizer, 1, wx.EXPAND | wx.ALL, 5)
        savePanel.SetSizer(saveBorderSizer)

        actionSizer = wx.GridBagSizer(vgap=10, hgap=5)
        actionSizer.AddGrowableCol(0)

        row=0
        actionSizer.Add(self._emailCheckbox, pos=(row, 0), flag=wx.EXPAND)
        actionSizer.Add(self._emailSettingsButton, pos=(row, 1), flag=wx.EXPAND)
        row += 1

        actionSizer.Add(self._ihostCheckbox, pos=(row, 0), flag=wx.EXPAND)
        actionSizer.Add(self._ihostEditButton, pos=(row, 1), flag=wx.EXPAND)
        row += 1

        if self._hasPaidVersion:
            box = wx.BoxSizer(wx.HORIZONTAL)
            box.Add(self._iftttCheckbox, 0, wx.EXPAND)
            box.Add(iftttHelpLink, 1, wx.ALIGN_LEFT | wx.EXPAND)
            actionSizer.Add(box, pos=(row, 0))
            iftttBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
            iftttBtnSizer.Add(self._iftttSettingsButton, 0, wx.RIGHT, 4)
            iftttBtnSizer.Add(self._iftttTestButton, 0)
            actionSizer.Add(iftttBtnSizer, pos=(row, 1), flag=wx.ALIGN_CENTER_VERTICAL)
            row += 1

            actionSizer.Add(self._webhookCheckbox, pos=(row, 0))
            actionSizer.Add(self._webhookEditButton, pos=(row, 1), flag=wx.EXPAND)
            row += 1

            actionSizer.Add(self._commandCheckbox, pos=(row, 0), span=(1, 2))
            row += 1
            actionSizer.Add(self._commandField, pos=(row, 0), flag=wx.EXPAND)
            actionSizer.Add(self._commandTestButton, pos=(row, 1), flag=wx.EXPAND)
            row += 1

        # The Sound response (played sound + Speak text) gets its own titled
        # frame, mirroring the Action Schedule box.  Rows go into this
        # StaticBoxSizer rather than the actionSizer grid.
        soundOuterSizer = wx.StaticBoxSizer(soundBox, wx.VERTICAL)
        _kSoundPad = 4

        soundSizer = wx.BoxSizer(wx.HORIZONTAL)
        soundSizer.Add(self._soundCheckbox, 0, wx.ALIGN_CENTER_VERTICAL |
                       wx.RIGHT, 5)
        soundSizer.Add(self._soundChoice, 1, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(soundSizer, 0, wx.EXPAND | wx.ALL, _kSoundPad)

        soundOuterSizer.Add(self._customSoundField, 0,
                            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                            _kSoundPad)

        soundOnSizer = wx.BoxSizer(wx.HORIZONTAL)
        soundOnSizer.AddSpacer(20)
        soundOnSizer.Add(self._soundOnLabel, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        soundOnSizer.Add(self._soundLocalRadio, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        soundOnSizer.Add(self._soundCcRadio, 0, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(soundOnSizer, 0,
                            wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)

        # ── TTS rows ─────────────────────────────────────────────────────────
        ttsTopSizer = wx.BoxSizer(wx.HORIZONTAL)
        ttsTopSizer.Add(self._ttsCheckbox, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        ttsTopSizer.Add(self._ttsTextField, 1, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(ttsTopSizer, 0,
                            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                            _kSoundPad)

        ttsHintSizer = wx.BoxSizer(wx.HORIZONTAL)
        ttsHintSizer.AddSpacer(20)
        ttsHintSizer.Add(self._ttsVarsHint, 1, wx.EXPAND)
        soundOuterSizer.Add(ttsHintSizer, 0,
                            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                            _kSoundPad)

        ttsOptSizer = wx.BoxSizer(wx.HORIZONTAL)
        ttsOptSizer.AddSpacer(20)
        ttsOptSizer.Add(self._ttsVoiceLabel, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        ttsOptSizer.Add(self._ttsVoiceChoice, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        ttsOptSizer.Add(self._ttsSpeedLabel, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        ttsOptSizer.Add(self._ttsSpeedCtrl, 0, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(ttsOptSizer, 0,
                            wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)

        ttsCooldownSizer = wx.BoxSizer(wx.HORIZONTAL)
        ttsCooldownSizer.AddSpacer(20)
        ttsCooldownSizer.Add(self._ttsCooldownLabel, 0,
                             wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        ttsCooldownSizer.Add(self._ttsCooldownCtrl, 0,
                             wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        ttsCooldownSizer.Add(self._ttsCooldownUnits, 0,
                             wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(ttsCooldownSizer, 0,
                            wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)

        ttsOutSizer = wx.BoxSizer(wx.HORIZONTAL)
        ttsOutSizer.AddSpacer(20)
        ttsOutSizer.Add(self._ttsOnLabel, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        ttsOutSizer.Add(self._ttsLocalRadio, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        ttsOutSizer.Add(self._ttsCcRadio, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        ttsOutSizer.Add(self._ttsTestButton, 0, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(ttsOutSizer, 0,
                            wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)

        # ── Shared network-speaker target (for either "Play on" or "Speak on"
        # set to Speaker) ──
        speakerIpSizer = wx.BoxSizer(wx.HORIZONTAL)
        speakerIpSizer.AddSpacer(20)
        speakerIpSizer.Add(self._speakerLabel, 0,
                           wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        speakerIpSizer.Add(self._ttsCcIpCtrl, 0,
                           wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        speakerIpSizer.Add(self._ttsSearchButton, 0, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(speakerIpSizer, 0,
                            wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)

        speakerPickSizer = wx.BoxSizer(wx.HORIZONTAL)
        speakerPickSizer.AddSpacer(20)
        speakerPickSizer.Add(self._ttsDeviceChoice, 0, wx.ALIGN_CENTER_VERTICAL)
        soundOuterSizer.Add(speakerPickSizer, 0,
                            wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)

        # ── Tapo camera box ─────────────────────────────────────────────────
        tapoOuterSizer = wx.StaticBoxSizer(tapoBox, wx.VERTICAL)
        tapoOuterSizer.Add(self._tapoSirenCheckbox, 0, wx.ALL, _kSoundPad)
        tapoOuterSizer.Add(self._tapoLightCheckbox, 0,
                           wx.LEFT | wx.RIGHT | wx.BOTTOM, _kSoundPad)
        tapoOuterSizer.Add(self._tapoHint, 0,
                           wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                           _kSoundPad)

        # ── Action Schedule embedded in the Take Actions tab ──
        _kPad = 6
        schedBox = wx.StaticBox(schedPanel, -1, "Action Schedule")
        schedOuterSizer = wx.StaticBoxSizer(schedBox, wx.VERTICAL)

        daysRow = wx.BoxSizer(wx.HORIZONTAL)
        daysRow.Add(wx.StaticText(schedBox, -1, "Days:"), 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        self._schedDayChecks = OrderedDict()
        for _day in ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']:
            _cb = wx.CheckBox(schedBox, -1, _day)
            _cb.SetValue(_day in self._schedActiveDays)
            self._schedDayChecks[_day] = _cb
            daysRow.Add(_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)

        quickRow = wx.BoxSizer(wx.HORIZONTAL)
        quickRow.AddSpacer(44)
        for _label in ['All', 'Weekdays', 'Weekends', 'None']:
            _btn = wx.Button(schedBox, -1, _label, style=wx.BU_EXACTFIT)
            _btn.Bind(wx.EVT_BUTTON, self._schedOnQuickSelect)
            quickRow.Add(_btn, 0, wx.RIGHT, 4)

        self._schedAllDayRadio = wx.RadioButton(schedBox, -1, "All day",
                                                 style=wx.RB_GROUP)
        self._schedAllDayRadio.Bind(wx.EVT_RADIOBUTTON, self._schedOnTimeType)

        self._schedFixedRadio = wx.RadioButton(schedBox, -1, "")
        self._schedFixedRadio.Bind(wx.EVT_RADIOBUTTON, self._schedOnTimeType)

        self._schedStartTime = FixedTimeCtrl(schedBox, -1, value='08:00:00',
                                              size=wx.DefaultSize, format='24HHMM')
        _, _timeH = self._schedStartTime.GetSize()
        self._schedStartSpin = wx.SpinButton(schedBox, -1, size=(-1, _timeH),
                                              style=wx.SP_VERTICAL | wx.SP_WRAP)
        self._schedStartTime.BindSpinButton(self._schedStartSpin)
        self._schedStartTime.Bind(EVT_TIMEUPDATE,
                                   lambda e: self._schedFixedRadio.SetValue(True))
        self._schedStopTime = FixedTimeCtrl(schedBox, -1, value='18:00:00',
                                             size=wx.DefaultSize, format='24HHMM')
        self._schedStopSpin = wx.SpinButton(schedBox, -1, size=(-1, _timeH),
                                             style=wx.SP_VERTICAL | wx.SP_WRAP)
        self._schedStopTime.BindSpinButton(self._schedStopSpin)
        self._schedStopTime.Bind(EVT_TIMEUPDATE,
                                  lambda e: self._schedFixedRadio.SetValue(True))

        fixedRow = wx.BoxSizer(wx.HORIZONTAL)
        fixedRow.Add(self._schedFixedRadio, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        fixedRow.Add(self._schedStartTime, 0, wx.ALIGN_CENTER_VERTICAL)
        fixedRow.Add(self._schedStartSpin, 0, wx.ALIGN_CENTER_VERTICAL)
        fixedRow.Add(wx.StaticText(schedBox, -1, "to"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, _kPad)
        fixedRow.Add(self._schedStopTime, 0, wx.ALIGN_CENTER_VERTICAL)
        fixedRow.Add(self._schedStopSpin, 0, wx.ALIGN_CENTER_VERTICAL)

        self._schedSolarRadio = wx.RadioButton(schedBox, -1, "")
        self._schedSolarRadio.Bind(wx.EVT_RADIOBUTTON, self._schedOnTimeType)

        _solarChoices = ["Sunrise", "Sunset"]
        self._schedSolarStartType = wx.Choice(schedBox, -1, choices=_solarChoices)
        self._schedSolarStartOffset = wx.SpinCtrl(schedBox, -1, value="0",
                                                   min=-240, max=240, size=(58, -1))
        self._schedSolarStopType = wx.Choice(schedBox, -1, choices=_solarChoices)
        self._schedSolarStopOffset = wx.SpinCtrl(schedBox, -1, value="0",
                                                  min=-240, max=240, size=(58, -1))

        solarRow = wx.BoxSizer(wx.HORIZONTAL)
        solarRow.Add(self._schedSolarRadio, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        solarRow.Add(self._schedSolarStartType, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "+/-"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(self._schedSolarStartOffset, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "min  to"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        solarRow.Add(self._schedSolarStopType, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "+/-"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(self._schedSolarStopOffset, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "min"), 0, wx.ALIGN_CENTER_VERTICAL)

        self._schedLatCtrl = wx.TextCtrl(schedBox, -1, "", size=(72, -1))
        self._schedLonCtrl = wx.TextCtrl(schedBox, -1, "", size=(72, -1))
        self._schedPickCityBtn = wx.Button(schedBox, -1, "Pick city...",
                                           style=wx.BU_EXACTFIT)
        self._schedPickCityBtn.Bind(wx.EVT_BUTTON, self._schedOnPickCity)

        locationRow = wx.BoxSizer(wx.HORIZONTAL)
        locationRow.AddSpacer(20)
        locationRow.Add(wx.StaticText(schedBox, -1, "Lat:"), 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        locationRow.Add(self._schedLatCtrl, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        locationRow.Add(wx.StaticText(schedBox, -1, "Lon:"), 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        locationRow.Add(self._schedLonCtrl, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        locationRow.Add(self._schedPickCityBtn, 0, wx.ALIGN_CENTER_VERTICAL)

        self._schedLocationHint = wx.StaticText(schedBox, -1,
            "(decimal degrees, e.g. 45.4, -75.7)")
        _hintFont = self._schedLocationHint.GetFont()
        _hintFont.MakeItalic()
        self._schedLocationHint.SetFont(_hintFont)
        locationHintRow = wx.BoxSizer(wx.HORIZONTAL)
        locationHintRow.AddSpacer(20)
        locationHintRow.Add(self._schedLocationHint, 0, wx.ALIGN_CENTER_VERTICAL)

        _noteText = AutoWrapStaticText(schedBox, -1,
            "Note: This schedule only affects when actions fire (send email, "
            "play sound, etc.) — it does not affect the rule’s "
            "detection or search capabilities.")
        _noteFont = _noteText.GetFont()
        _noteFont.MakeItalic()
        _noteText.SetFont(_noteFont)
        _noteText.SetMinSize((1, _noteText.GetBestSize()[1]))

        schedOuterSizer.Add(daysRow,         0, wx.EXPAND | wx.ALL, _kPad)
        schedOuterSizer.Add(quickRow,        0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        schedOuterSizer.Add(self._schedAllDayRadio, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        schedOuterSizer.Add(fixedRow,        0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        schedOuterSizer.Add(solarRow,        0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        schedOuterSizer.Add(locationRow,     0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        schedOuterSizer.Add(locationHintRow, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        schedOuterSizer.Add(_noteText,       0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)

        self._schedPopulateFromData()

        actionBorderSizer = wx.BoxSizer(wx.VERTICAL)
        actionBorderSizer.AddSpacer(6)
        actionBorderSizer.Add(actionSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        actionBorderSizer.Add(soundOuterSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        actionBorderSizer.Add(tapoOuterSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        actionPanel.SetSizer(actionBorderSizer)

        schedBorderSizer = wx.BoxSizer(wx.VERTICAL)
        schedBorderSizer.AddSpacer(6)
        schedBorderSizer.Add(schedOuterSizer, 0,
                             wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        schedPanel.SetSizer(schedBorderSizer)

        self.SetSizer(mainSizer)
        self.setActivePage(self._activePage)

        # Bind...
        self._recordCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
        self._recordSettingsButton.Bind(wx.EVT_BUTTON, self.OnRecordConfig)
        self._snapshotCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
        self._snapshotSubfolderField.Bind(wx.EVT_TEXT, self.OnUiChange)
        self._snapshotBboxCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
        self._emailCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
        self._emailSettingsButton.Bind(wx.EVT_BUTTON, self.OnEmailConfig)
        if self._hasPaidVersion:
            self._iftttCheckbox.Bind(wx.EVT_CHECKBOX, self.OnIftttCheck)
            self._iftttSettingsButton.Bind(wx.EVT_BUTTON, self.OnIftttSettings)
            self._webhookCheckbox.Bind(wx.EVT_CHECKBOX, self.OnWebhookCheck)
        self._soundCheckbox.Bind(wx.EVT_CHECKBOX, self._onSoundCheck)
        self._soundChoice.Bind(wx.EVT_CHOICE, self.OnSoundChoice)
        self._soundLocalRadio.Bind(wx.EVT_RADIOBUTTON, self.OnUiChange)
        self._soundCcRadio.Bind(wx.EVT_RADIOBUTTON, self.OnUiChange)
        self._ttsCheckbox.Bind(wx.EVT_CHECKBOX, self._onTtsCheck)
        self._ttsTextField.Bind(wx.EVT_TEXT, self.OnUiChange)
        self._ttsVoiceChoice.Bind(wx.EVT_CHOICE, self.OnUiChange)
        self._ttsSpeedCtrl.Bind(wx.EVT_SPINCTRLDOUBLE, self.OnUiChange)
        self._ttsSpeedCtrl.Bind(wx.EVT_TEXT, self.OnUiChange)
        self._ttsCooldownCtrl.Bind(wx.EVT_SPINCTRLDOUBLE, self.OnUiChange)
        self._ttsCooldownCtrl.Bind(wx.EVT_TEXT, self.OnUiChange)
        self._ttsLocalRadio.Bind(wx.EVT_RADIOBUTTON, self._onTtsOutputChange)
        self._ttsCcRadio.Bind(wx.EVT_RADIOBUTTON, self._onTtsOutputChange)
        self._ttsCcIpCtrl.Bind(wx.EVT_TEXT, self._onTtsIpEdit)
        self._ttsSearchButton.Bind(wx.EVT_BUTTON, self._onTtsSearchSpeakers)
        self._ttsDeviceChoice.Bind(wx.EVT_CHOICE, self._onTtsDevicePick)
        self._ttsTestButton.Bind(wx.EVT_BUTTON, self._onTtsTest)

        self._ihostCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
        self._ihostEditButton.Bind(wx.EVT_BUTTON, self.OnIHostEdit)

        self._tapoSirenCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
        self._tapoLightCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)

        if self._hasPaidVersion:
            self._ftpCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
            self._ftpSettingsButton.Bind(wx.EVT_BUTTON, self.OnFtpConfig)

            self._localExportCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
            #self._localExportButton.Bind(wx.EVT_BUTTON, self.OnLocalExportConfig)

            self._commandCheckbox.Bind(wx.EVT_CHECKBOX, self.OnUiChange)
            self._commandField.Bind(wx.EVT_TEXT, self.OnCommandChange)
            self._commandTestButton.Bind(wx.EVT_BUTTON, self.OnCommandTest)
            self._iftttTestButton.Bind(wx.EVT_BUTTON, self.OnIftttTest)
            self._webhookEditButton.Bind(wx.EVT_BUTTON, self.OnWebhookEdit)

        # those will be updated momentarily
        self._webhookURI = None
        self._webhookContentType = None
        self._webhookContent = None

        # TTS Chromecast friendly name (saved alongside the IP so a moved
        # speaker can be re-discovered at fire time).  _settingTtsIp guards the
        # IP field's EVT_TEXT so a programmatic SetValue doesn't clear the name.
        self._ttsCcName = ''
        self._settingTtsIp = False

        # iHost per-rule selection (updated from the model / editor dialog)
        self._ihostDevice = ''
        self._ihostDeviceName = ''
        self._ihostCommand = 'on'
        self._ihostTimeout = 300
        self._ihostNightOnly = False

        # Listen for changes.
        self._dataModel.addListener(self._handleModelChange, False, 'responses')

        # Update everything...
        self._ignoreCustomFieldUpdate = True
        self._handleModelChange(self._dataModel)
        self._ignoreCustomFieldUpdate = False


    ###########################################################
    def __del__(self):
        pass


    ###########################################################
    def setActivePage(self, page):
        """Show one of our three pages.

        This one panel backs three flow chart blocks, so the construction view
        registers the same instance under all three of its keys and calls this
        before showing it.

        @param  page  One of the k*Page constants.
        """
        assert page in self._pagePanels, "Unknown page %s" % page
        self._activePage = page
        for name, panel in self._pagePanels.items():
            self.GetSizer().Show(panel, show=(name == page))
        self.Layout()


    ###########################################################
    def getIcon(self):
        """Return the path to the bitmap associated with this panel.

        @return bmpPath  The path to the bitmap.
        """
        return _kPageIcons[self._activePage]


    ###########################################################
    def getTitle(self):
        """Return the title associated with this panel.

        @return title  The title
        """
        return _kPageTitles[self._activePage]


    ###########################################################
    def _updateSoundControls(self):
        """Enable/disable the Sound-box sub-controls based on the two checkboxes.

        "Play this sound" and "Speak text" each gate their own controls, but the
        shared network-speaker widgets (IP / Search / device picker) are enabled
        whenever either action is on.
        """
        soundOn = self._soundCheckbox.GetValue()
        ttsOn   = self._ttsCheckbox.GetValue()

        # Sound output selector.
        for ctrl in (self._soundOnLabel, self._soundLocalRadio,
                     self._soundCcRadio):
            ctrl.Enable(soundOn)

        # TTS sub-controls.
        for ctrl in (self._ttsTextField, self._ttsVoiceLabel,
                     self._ttsVoiceChoice, self._ttsSpeedLabel,
                     self._ttsSpeedCtrl, self._ttsOnLabel, self._ttsLocalRadio,
                     self._ttsCcRadio, self._ttsTestButton):
            ctrl.Enable(ttsOn)

        # The cooldown throttles the whole Sound response, so it stays available
        # regardless of the Speak-text toggle (the _ttsCooldown* widgets are
        # left enabled).

        # The shared speaker target applies to either action, so enable it
        # whenever the sound OR the speech is on.
        speakerOn = soundOn or ttsOn
        for ctrl in (self._speakerLabel, self._ttsCcIpCtrl,
                     self._ttsSearchButton, self._ttsDeviceChoice):
            ctrl.Enable(speakerOn)


    ###########################################################
    def _onSoundCheck(self, event):
        self.OnUiChange(event)
        self._updateSoundControls()


    ###########################################################
    def _onTtsCheck(self, event):
        self.OnUiChange(event)
        self._updateSoundControls()


    ###########################################################
    def _onTtsOutputChange(self, event):
        self.OnUiChange(event)
        self._updateSoundControls()


    ###########################################################
    def _onTtsIpEdit(self, event):
        """Handle edits to the Chromecast IP field.

        A hand-typed IP no longer corresponds to a discovered speaker, so we
        drop the saved friendly name (used only as a rediscovery hint) and clear
        the device dropdown selection.  Programmatic SetValue() calls guard this
        via _settingTtsIp / _ignoreCustomFieldUpdate so they don't wipe a name
        we just set.
        """
        if not self._settingTtsIp and not self._ignoreCustomFieldUpdate:
            self._ttsCcName = ''
            self._ttsDeviceChoice.SetSelection(wx.NOT_FOUND)
        self.OnUiChange(event)


    ###########################################################
    def _onTtsSearchSpeakers(self, event):
        """Discover Chromecast/Google speakers on the LAN and fill the dropdown.

        Discovery takes a few seconds, so run it on a worker thread and update
        the UI via wx.CallAfter.
        """
        self._ttsSearchButton.Enable(False)
        self._ttsSearchButton.SetLabel("Searching...")
        self._ttsDeviceChoice.Clear()

        def _worker():
            speakers = []
            err = None
            try:
                from backEnd.responses.TtsManager import discover_speakers
                speakers = discover_speakers(logger=self._logger)
            except Exception as e:
                err = str(e)
            wx.CallAfter(_finish, speakers, err)

        def _finish(speakers, err):
            self._ttsSearchButton.Enable(True)
            self._ttsSearchButton.SetLabel("Search")
            if err:
                wx.MessageBox("Speaker search failed:\n%s" % err,
                              "Speaker Search", wx.OK | wx.ICON_ERROR,
                              self.GetTopLevelParent())
                return
            self._ttsDeviceChoice.Clear()
            for s in speakers:
                label = "%s (%s)" % (s['name'], s['ip'])
                self._ttsDeviceChoice.Append(label, s)
            if not speakers:
                wx.MessageBox(
                    "No speakers were found on the network.\n\n"
                    "Make sure the speaker is powered on and on the same "
                    "network, then try again.",
                    "Speaker Search", wx.OK | wx.ICON_INFORMATION,
                    self.GetTopLevelParent())
            self._updateSoundControls()

        import threading
        threading.Thread(target=_worker, daemon=True,
                         name="TtsSpeakerSearch").start()


    ###########################################################
    def _onTtsDevicePick(self, event):
        """Fill the IP field from the picked speaker and remember its name."""
        sel = self._ttsDeviceChoice.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        info = self._ttsDeviceChoice.GetClientData(sel)
        if not info:
            return
        # The speaker target is shared between "Play on" and "Speak on", so
        # picking one only sets the shared IP/name — it does not force either
        # action's output radio to Speaker.
        self._ttsCcName = info['name']
        self._settingTtsIp = True
        try:
            self._ttsCcIpCtrl.SetValue(info['ip'])
        finally:
            self._settingTtsIp = False
        self.OnUiChange(event)
        self._updateSoundControls()


    ###########################################################
    def _previewSubstitute(self, text):
        """Resolve {Sv*} variables for the TTS Test preview.

        The Test button speaks immediately without going through the rule's
        response, so substitute here using this rule's own context (its camera,
        name, and "Look for" target) plus the current time, so the preview
        matches what a real trigger would say.  {SvRuleFace} shows a sample
        name.  Falls back to the literal text on any error.
        """
        if '{Sv' not in text:
            return text
        try:
            from appCommon.ResponseSubstitution import substituteResponseVars
            from appCommon.CommonStrings import kTargetSettingToLabel
            targetModel = self._dataModel.getTargets()[0]
            targetName = targetModel.getTargetName()
            lookFor = kTargetSettingToLabel.get(targetName, targetName)
            faceNames = targetModel.getFaceNames()
            if targetName == 'face' and faceNames:
                lookFor += ": " + ", ".join(faceNames)
                faceSample = faceNames[0]
            elif targetName == 'face':
                faceSample = "Unknown"
            else:
                faceSample = ""
            return substituteResponseVars(
                text, self._dataModel.getName(),
                self._dataModel.getVideoSource().getLocationName(),
                int(time.time() * 1000), lookFor, faceSample)
        except Exception:
            return text


    ###########################################################
    def _onTtsTest(self, event):
        text = self._ttsTextField.GetValue().strip()
        if not text:
            wx.MessageBox("Please enter text to speak.", "TTS Test",
                          wx.OK | wx.ICON_INFORMATION,
                          self.GetTopLevelParent())
            return
        text = self._previewSubstitute(text)
        voiceName = self._ttsVoiceChoice.GetStringSelection()
        voice = _kTtsVoiceMap.get(voiceName, 'af_heart')
        speed = self._ttsSpeedCtrl.GetValue()
        output = 'chromecast' if self._ttsCcRadio.GetValue() else 'local'
        ccIp = self._ttsCcIpCtrl.GetValue().strip()
        ccName = self._ttsCcName

        # Disable button and show progress — model download can take a moment
        self._ttsTestButton.Enable(False)
        self._ttsTestButton.SetLabel("Working...")
        parent = self.GetTopLevelParent()

        def _done(err):
            def _ui():
                self._ttsTestButton.Enable(True)
                self._ttsTestButton.SetLabel("Test")
                if err:
                    wx.MessageBox("TTS failed:\n%s" % err, "TTS Error",
                                  wx.OK | wx.ICON_ERROR, parent)
            wx.CallAfter(_ui)

        try:
            from backEnd.responses.TtsManager import get_tts_manager
            mgr = get_tts_manager(self._logger)
            mgr.queue_tts(text, voice, speed, output, ccIp,
                          cc_name=ccName, done_callback=_done)
        except Exception as e:
            _done(str(e))


    ###########################################################
    def OnSoundChoice(self, event):
        """Play the sound selected by the user.

        @param  event  The event (ignored).
        """
        if self._soundChoice.GetStringSelection() == _kCustomSoundLabel and \
           self._lastCustomSoundPath:
            # Back to Custom: give back the file the user had chosen.  With
            # nothing chosen yet, the path of the sound they were on stays, as
            # a starting point for Browse.  callBack=0 because the OnUiChange
            # and the play below already do what OnCustomSoundChange would.
            self._customSoundField.SetValue(self._lastCustomSoundPath, 0)
        self._soundCheckbox.SetValue(True)
        self.OnUiChange(event)
        wx.CallLater(100, self._playSound)


    ##########################################################
    def _playSound(self):
        """Play the currently configured sound."""
        responseConfigList = self._dataModel.getResponses()
        for responseName, config in responseConfigList:
            if responseName == kSoundResponse:
                soundPath = resolveSoundPath(config.get('soundPath', ''))
                if not soundPath:
                    # If the user chose 'custom' but hasn't entered a path yet
                    # we don't want to complain about files not existing.
                    return
                try:
                    from backEnd.responses.SoundResponse import playSound
                    playSound(soundPath, False)
                except Exception as e:
                    wx.MessageBox("The sound file could not be played.",
                                  "Error", wx.OK | wx.ICON_ERROR,
                                  self.GetTopLevelParent())
                    self._logger.warn(
                            "The sound file (%s) couldn't be played - %s %s"
                            % (soundPath, str(type(e)), str(e)))


#    ###########################################################
#    def _handleRuleNameChange(self, dataModel):
#        """Handle a change in our data model.
#
#        @param  dataModel  The changed data model.
#        """
#        if self._iftttCheckbox.GetValue():
#            self._sendIftttState()


    ###########################################################
    def OnIftttCheck(self, event=None):
        """Handle a toggle of the IFTTT checkbox.

        @param  event  The event (ignored).
        """
        self._toggledIfttt = True
        self.OnUiChange(event)
        self._sendIftttState()

    ###########################################################
    def OnWebhookCheck(self, event=None):
        """Handle a toggle of the webhook checkbox.

        @param  event  The event (ignored).
        """
        if self._ignoreCustomFieldUpdate or not self._hasPaidVersion:
            return

        self.OnUiChange(event)

    ###########################################################
    def _sendIftttState(self, allowExtras=True):
        """Send the current IFTTT rule/camera status to the server.

        This includes additive temporary changes, but will not remove
        anything from the current saved reality - temporary changes should
        never break currently running IFTTT rules which removing would do.

        @param  allowTemporaryState  If True, allow temp state to be sent.
        """
        # TODO: Listen for camera location and rule name changes and call this
        #       function in response? Too much chatter to do that in order to
        #       cover those edge cases? If desired, add the following to init:
        # self._dataModel.addListener(self._handleRuleNameChange, False, 'name')

        rules = []
        cameras = []

        if allowExtras and self._iftttCheckbox.GetValue():
            cameras = [self._dataModel.getVideoSource().getLocationName()]
            rules = [self._dataModel.getName()]

        self._backEndClient.sendIftttRulesAndCameras(rules, cameras)


    ###########################################################
    def OnUiChange(self, event=None):
        """Handle various UI events and update our model.

        @param  event  The event (ignored).
        """
        # Never write widgets back into the model while we are loading the model
        # INTO the widgets.  _handleModelChange sets fields whose SetValue()
        # fires EVT_TEXT -> OnUiChange; during the initial load the widgets for
        # responses not yet processed (iHost is last in the list) are still at
        # their unchecked defaults, so an early OnUiChange would clobber them
        # back to disabled.  _ignoreCustomFieldUpdate is True for exactly that
        # load window (see __init__), matching how the field handlers guard.
        if self._ignoreCustomFieldUpdate:
            return
        responseConfigList = self._dataModel.getResponses()
        for responseName, config in responseConfigList:
            if responseName == kRecordResponse:
                config['isEnabled'] = bool(self._recordCheckbox.GetValue())
            elif responseName == kPushResponse:
                config['isEnabled'] = False  # feature removed
            elif responseName == kEmailResponse:
                config['isEnabled'] = bool(self._emailCheckbox.GetValue())

            elif responseName == kWebhookResponse:
                if self._hasPaidVersion:
                    config['isEnabled'] = bool(self._webhookCheckbox.GetValue())
                    config['webhookUri'] = self._webhookURI
                    config['webhookContent'] = self._webhookContent
                    config['webhookContentType'] = self._webhookContentType

            elif responseName == kIftttResponse:
                if self._hasPaidVersion:
                    config['isEnabled'] = bool(self._iftttCheckbox.GetValue())
                    config['iftttKey'] = self._iftttKey
                    config['iftttEventName'] = self._iftttEventName

            elif responseName == kCommandResponse:
                if self._hasPaidVersion:
                    config['isEnabled'] = bool(self._commandCheckbox.GetValue())
                    config[kCommandResponseLookup] = self._commandField.GetValue()
                else:
                    config['isEnabled'] = False
                    config[kCommandResponseLookup] = ''

            elif responseName == kSoundResponse:
                config['isEnabled'] = bool(self._soundCheckbox.GetValue())
                soundName = self._soundChoice.GetStringSelection()
                if soundName in self._defaultSounds:
                    soundPath = self._defaultSounds[soundName]
                else:
                    soundPath = self._customSoundField.GetValue()
                # Relative to the install when the file is inside it, so the
                # rule still plays after the program moves.
                config['soundPath'] = portableSoundPath(soundPath)
                config['soundName'] = soundName
                config['soundOutput'] = ('chromecast'
                                         if self._soundCcRadio.GetValue()
                                         else 'local')
                config['ttsEnabled'] = bool(self._ttsCheckbox.GetValue())
                config['ttsText']    = self._ttsTextField.GetValue()
                config['ttsVoice']   = _kTtsVoiceMap.get(
                    self._ttsVoiceChoice.GetStringSelection(), 'af_heart')
                config['ttsSpeed']   = self._ttsSpeedCtrl.GetValue()
                config['ttsOutput']  = ('chromecast'
                                        if self._ttsCcRadio.GetValue()
                                        else 'local')
                config['ttsChromecastIP'] = self._ttsCcIpCtrl.GetValue()
                config['ttsChromecastName'] = self._ttsCcName
                config['ttsCooldownMins'] = self._ttsCooldownCtrl.GetValue()

            elif responseName == kFtpResponse:
                if self._hasPaidVersion:
                    config['isEnabled'] = bool(self._ftpCheckbox.GetValue())
                else:
                    config['isEnabled'] = False

            elif responseName == kLocalExportResponse:
                if self._hasPaidVersion:
                    config['isEnabled'] = bool(
                            self._localExportCheckbox.GetValue())
                    config['exportPath'] = self._localExportField.GetValue()
                else:
                    config['isEnabled'] = False

            elif responseName == kSnapshotResponse:
                config['isEnabled'] = bool(self._snapshotCheckbox.GetValue())
                config['snapshotPath'] = self._snapshotFolderField.GetValue()
                config['snapshotSubfolder'] = self._snapshotSubfolderField.GetValue().strip()
                config['drawBoundingBox'] = bool(self._snapshotBboxCheckbox.GetValue())

            elif responseName == kIHostResponse:
                config['isEnabled'] = bool(self._ihostCheckbox.GetValue())
                config['ihostDevice'] = self._ihostDevice
                config['ihostDeviceName'] = self._ihostDeviceName
                config['ihostCommand'] = self._ihostCommand
                config['ihostTimeout'] = self._ihostTimeout
                config['ihostNightOnly'] = self._ihostNightOnly

            elif responseName == kTapoResponse:
                wantSiren = bool(self._tapoSirenCheckbox.GetValue())
                wantLight = bool(self._tapoLightCheckbox.GetValue())
                # One response carrying two switches: it is only "on" if at
                # least one of them is, so an untouched rule stays off.
                config['isEnabled'] = wantSiren or wantLight
                config['tapoSiren'] = wantSiren
                config['tapoLight'] = wantLight

            else:
                assert False, "Unknown response %s" % (responseName)

        self._dataModel.setResponses(responseConfigList)


    ###########################################################
    def OnRecordConfig(self, event):
        """Handle a user request to configure recording.

        @param  event  The event (ignored).
        """
        parent = self.GetTopLevelParent()
        frame = parent.GetParent()
        dlg = OptionsDialog(parent, self._backEndClient, self._dataMgr,
                self._logger, frame.getUIPrefsDataModel())
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()


    ###########################################################
    def OnEmailConfig(self, event):
        """Handle a user request to configure email.

        @param  event  The event (ignored).
        """
        emailSettings = self._backEndClient.getEmailSettings()
        responseConfigList = self._dataModel.getResponses()

        responseConfig = None
        for responseName, config in responseConfigList:
            if responseName == kEmailResponse:
                responseConfig = config
                break

        assert responseConfig is not None
        if not responseConfig:
            self._logger.error("Couldn't retrieve email response config");
            return

        dlg = EmailSetupDialog(self.GetTopLevelParent(), emailSettings,
                               responseConfig)
        try:
            result = dlg.ShowModal()
            if result == wx.ID_OK:
                self._backEndClient.setEmailSettings(dlg.getEmailConfig())
                self._dataModel.setResponses(responseConfigList)
        finally:
            dlg.Destroy()


    ###########################################################
    def OnFtpConfig(self, event):
        """Handle a user request to configure FTP.

        @param  The event (ignored).
        """
        ftpSettings = self._backEndClient.getFtpSettings()

        # Opt in to the PySide6 port of this screen with SV_QT_FTP_DIALOG=1.
        # See frontEnd/qt/README.md.  The import is deliberately inside the
        # branch: a front end running the wx dialog never loads Qt at all.
        if os.environ.get('SV_QT_FTP_DIALOG', '') == '1':
            from frontEnd.qt.FtpSetupDialogQt import showFtpSetupDialog
            newSettings = showFtpSetupDialog(self.GetTopLevelParent(),
                                             ftpSettings)
            if newSettings is not None:
                self._backEndClient.setFtpSettings(newSettings)

                # Turn on FTP response, since they hit "OK" from settings
                # (that implies that they wanted FTP)...
                self._ftpCheckbox.SetValue(1)
                self.OnUiChange()
            return

        dlg = FtpSetupDialog(self.GetTopLevelParent(), ftpSettings)
        try:
            result = dlg.ShowModal()
            if result == wx.ID_OK:
                self._backEndClient.setFtpSettings(dlg.getFtpConfig())

                # Turn on FTP response, since they hit "OK" from settings
                # (that implies that they wanted FTP)...
                self._ftpCheckbox.SetValue(1)
                self.OnUiChange()
        finally:
            dlg.Destroy()


    ###########################################################
    def OnLocalExportConfig(self, event):
        """Handle a user request to configure local export.

        @param  The event.
        """
        if self._ignoreCustomFieldUpdate or not self._hasPaidVersion:
            return

        promptUserIfRemotePathEvtHandler(event)

        exportPath = self._localExportField.GetValue()

        evtObj = event.GetEventObject()

        if (evtObj.GetValue() == '') or (evtObj.GetValue() != exportPath):
            self._localExportCheckbox.SetValue(False)
            self.OnUiChange(event)

        elif os.path.isdir(exportPath):
            if not self._localExportCheckbox.GetValue():
                self._localExportCheckbox.SetValue(True)
            self.OnUiChange(event)


    ###########################################################
    def OnSnapshotFolderChange(self, event):
        """Handle a change to the custom snapshot folder path."""
        if self._ignoreCustomFieldUpdate:
            return
        promptUserIfRemotePathEvtHandler(event)
        self.OnUiChange(event)

    ###########################################################
    def OnIHostEdit(self, event):
        """Handle a user request to configure the iHost command."""
        dlg = IHostEditor(self.GetTopLevelParent(),
                          self._ihostDevice, self._ihostDeviceName,
                          self._ihostCommand, self._ihostTimeout,
                          self._ihostNightOnly)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                vals = dlg.GetValues()
                self._ihostDevice = vals["device"]
                self._ihostDeviceName = vals["name"]
                self._ihostCommand = vals["command"]
                self._ihostTimeout = vals["timeout"]
                self._ihostNightOnly = vals["nightOnly"]
                # Choosing a device implies enabling the action.
                self._ihostCheckbox.SetValue(True)
                self.OnUiChange()
        finally:
            dlg.Destroy()

    ###########################################################
    def _handleModelChange (self, dataModel):
        """Handle a change in our data model.

        @param  event  The event (ignored).
        """
        assert dataModel == self._dataModel

        responseConfigList = self._dataModel.getResponses()
        for responseName, config in responseConfigList:
            if responseName == kRecordResponse:
                self._recordCheckbox.SetValue(config.get('isEnabled', False))

            elif responseName == kPushResponse:
                pass  # feature removed — no UI to update

            elif responseName == kIftttResponse:
                if self._hasPaidVersion:
                    self._iftttCheckbox.SetValue(config.get('isEnabled', False))
                    self._iftttKey = config.get('iftttKey', '')
                    self._iftttEventName = config.get('iftttEventName', '')

            elif responseName == kWebhookResponse:
                if self._hasPaidVersion:
                    self._webhookCheckbox.SetValue(config.get('isEnabled', False))
                    self._webhookURI = config.get('webhookUri', '')
                    self._webhookContentType = config.get('webhookContentType', '')
                    self._webhookContent = config.get('webhookContent', '')

            elif responseName == kEmailResponse:
                # Set the checkbox; do this after setting the field, since
                # settings the field may cause an event to go which will
                # check the checkbox...
                self._emailCheckbox.SetValue(config.get('isEnabled', False))

            elif responseName == kCommandResponse:
                if self._hasPaidVersion:
                    # Set the field if needed...
                    # ...only if needed to avoid loop (SetValue fires an event)
                    command = config.get(kCommandResponseLookup, '')
                    if self._commandField.GetValue() != command:
                        self._commandField.SetValue(command)

                    # Set the checkbox; do this after setting the field, since
                    # settings the field may cause an event to go which will
                    # check the checkbox...
                    self._commandCheckbox.SetValue(config.get('isEnabled',
                                                              False))


            elif responseName == kSoundResponse:
                self._soundCheckbox.SetValue(config.get('isEnabled', False))
                soundName = config.get('soundName')
                storedPath = config.get('soundPath', '')
                fieldPath = self._customSoundField.GetValue()
                if soundName in self._defaultSounds:
                    # Where the sound is in this install.  (Older rules store
                    # the full path of whatever copy of the app saved them;
                    # checkResponses stores it the portable way on save.)
                    soundPath = self._defaultSounds[soundName]
                elif soundName != _kCustomSoundLabel and not storedPath:
                    # Nothing chosen yet (a new rule): the first in the list.
                    soundName = self._soundChoice.GetString(0)
                    soundPath = self._defaultSounds.get(soundName, '')
                else:
                    # Custom -- or a listed sound whose file has since left
                    # the sounds folder, shown as the custom path it now is,
                    # so the user can see the path and fix it.
                    soundName = _kCustomSoundLabel
                    if portableSoundPath(fieldPath) == storedPath:
                        # The rule holds what the field holds: the user is
                        # typing in it.  Don't expand the path under them.
                        soundPath = fieldPath
                    else:
                        soundPath = resolveSoundPath(storedPath)
                    self._lastCustomSoundPath = soundPath
                self._soundChoice.SetStringSelection(soundName)
                # Always show the full path of the sound that will play.  Only
                # when it differs, so the cursor doesn't jump while the user
                # types; callBack=0 so the model's own value isn't handed back
                # to OnCustomSoundChange to write again and play.
                if soundPath != fieldPath:
                    self._customSoundField.SetValue(soundPath, 0)
                if config.get('soundOutput', 'local') == 'chromecast':
                    self._soundCcRadio.SetValue(True)
                else:
                    self._soundLocalRadio.SetValue(True)
                # TTS fields
                self._ttsCheckbox.SetValue(config.get('ttsEnabled', False))
                ttsText = config.get('ttsText', 'Alert')
                if self._ttsTextField.GetValue() != ttsText:
                    self._ttsTextField.SetValue(ttsText)
                voiceCode = config.get('ttsVoice', 'af_heart')
                voiceName = _kTtsVoiceCodeToName.get(voiceCode, 'Heart')
                self._ttsVoiceChoice.SetStringSelection(voiceName)
                self._ttsSpeedCtrl.SetValue(config.get('ttsSpeed', 1.0))
                if config.get('ttsOutput', 'local') == 'chromecast':
                    self._ttsCcRadio.SetValue(True)
                else:
                    self._ttsLocalRadio.SetValue(True)
                ccIp = config.get('ttsChromecastIP', '')
                if self._ttsCcIpCtrl.GetValue() != ccIp:
                    # Guard so the IP field's EVT_TEXT doesn't clear the name
                    # we're about to load.
                    self._settingTtsIp = True
                    try:
                        self._ttsCcIpCtrl.SetValue(ccIp)
                    finally:
                        self._settingTtsIp = False
                # Set the name AFTER the IP field so it survives regardless.
                self._ttsCcName = config.get('ttsChromecastName', '')
                self._ttsCooldownCtrl.SetValue(config.get('ttsCooldownMins',
                                                          0.0))
                self._updateSoundControls()

            elif responseName == kFtpResponse:
                if self._hasPaidVersion:
                    self._ftpCheckbox.SetValue(config.get('isEnabled', False))

            elif responseName == kLocalExportResponse:
                if self._hasPaidVersion:
                    self._localExportCheckbox.SetValue(config.get('isEnabled', False))
                    exportPath = config.get('exportPath', "")
                    if exportPath != self._localExportField.GetValue():
                        self._localExportField.SetValue(exportPath)

            elif responseName == kSnapshotResponse:
                self._snapshotCheckbox.SetValue(config.get('isEnabled', False))
                snapshotPath = config.get('snapshotPath', '')
                if snapshotPath != self._snapshotFolderField.GetValue():
                    self._snapshotFolderField.SetValue(snapshotPath)
                self._snapshotSubfolderField.SetValue(config.get('snapshotSubfolder', ''))
                self._snapshotBboxCheckbox.SetValue(config.get('drawBoundingBox', False))

            elif responseName == kIHostResponse:
                self._ihostCheckbox.SetValue(config.get('isEnabled', False))
                self._ihostDevice = config.get('ihostDevice', '')
                self._ihostDeviceName = config.get('ihostDeviceName', '')
                self._ihostCommand = config.get('ihostCommand', 'on')
                self._ihostTimeout = config.get('ihostTimeout', 300)
                self._ihostNightOnly = config.get('ihostNightOnly', False)

            elif responseName == kTapoResponse:
                self._tapoSirenCheckbox.SetValue(config.get('tapoSiren', False))
                self._tapoLightCheckbox.SetValue(config.get('tapoLight', False))

            else:
                assert False, "Unknown response %s" % (responseName)

        self._customSoundField.Enable(
                self._soundChoice.GetStringSelection() == _kCustomSoundLabel)
        self._updateSoundControls()


    # ── Schedule methods ────────────────────────────────────────────────────

    ###########################################################
    def _initSchedule(self):
        """Load schedule data from a seed schedule, the backend rule, or defaults."""
        self._schedData = copy.deepcopy(_kDefaultRuleSchedule)
        if self._initialSchedule is not None:
            # Seeded (e.g. duplicating a rule): use the original's schedule.
            self._schedData = copy.deepcopy(self._initialSchedule)
        elif self._backEndClient is not None:
            try:
                rule = self._backEndClient.getRule(self._dataModel.getName())
                if rule is not None:
                    self._schedData = rule.getSchedule()
            except Exception:
                pass

        dayType = self._schedData.get('dayType', 'Every day')
        if dayType == 'Every day':
            self._schedActiveDays = set(['Sun','Mon','Tue','Wed','Thu','Fri','Sat'])
        elif dayType == 'Weekdays':
            self._schedActiveDays = set(['Mon','Tue','Wed','Thu','Fri'])
        elif dayType == 'Weekends':
            self._schedActiveDays = set(['Sat','Sun'])
        else:
            self._schedActiveDays = set(self._schedData.get('customDays', []))


    ###########################################################
    def _schedPopulateFromData(self):
        """Populate schedule controls from self._schedData."""
        data = self._schedData
        startType = data.get('startType', 'fixed')
        stopType  = data.get('stopType',  'fixed')

        if data.get('is24Hours', True):
            targetRadio = self._schedAllDayRadio
        elif startType in ('sunrise', 'sunset') or \
             stopType in ('sunrise', 'sunset'):
            targetRadio = self._schedSolarRadio
        else:
            targetRadio = self._schedFixedRadio

        self._schedStartTime.SetValue('%02d:%02d:00' % (
            data.get('startHour', 8), data.get('startMin', 0)))
        self._schedStopTime.SetValue('%02d:%02d:00' % (
            data.get('stopHour', 18), data.get('stopMin', 0)))

        _ch = ["Sunrise", "Sunset"]
        self._schedSolarStartType.SetSelection(
            _ch.index(startType.capitalize())
            if startType in ('sunrise', 'sunset') else 0)
        self._schedSolarStopType.SetSelection(
            _ch.index(stopType.capitalize())
            if stopType in ('sunrise', 'sunset') else 1)
        self._schedSolarStartOffset.SetValue(data.get('startOffset', 0))
        self._schedSolarStopOffset.SetValue(data.get('stopOffset',  0))

        lat = data.get('latitude')
        lon = data.get('longitude')
        self._schedLatCtrl.SetValue(str(lat) if lat is not None else '')
        self._schedLonCtrl.SetValue(str(lon) if lon is not None else '')

        targetRadio.SetValue(True)
        self._schedOnTimeType()
        wx.CallAfter(targetRadio.SetValue, True)
        wx.CallAfter(self._schedOnTimeType)


    ###########################################################
    def _schedUpdateTimeControls(self):
        """Enable/disable time controls based on the active radio."""
        isFixed = self._schedFixedRadio.GetValue()
        isSolar = self._schedSolarRadio.GetValue()
        for ctrl in (self._schedStartTime, self._schedStartSpin,
                     self._schedStopTime, self._schedStopSpin):
            ctrl.Enable(isFixed)
        for ctrl in (self._schedSolarStartType, self._schedSolarStartOffset,
                     self._schedSolarStopType, self._schedSolarStopOffset,
                     self._schedLatCtrl, self._schedLonCtrl,
                     self._schedPickCityBtn):
            ctrl.Enable(isSolar)


    ###########################################################
    def _schedOnTimeType(self, event=None):
        self._schedUpdateTimeControls()
        if self._schedSolarRadio.GetValue() and \
                not self._schedLatCtrl.GetValue().strip() and \
                not self._schedLonCtrl.GetValue().strip():
            from frontEnd.ScheduleLocationPicker import schedAutoDetectLocation
            schedAutoDetectLocation(self._schedLatCtrl, self._schedLonCtrl,
                                    self._schedLocationHint, self)


    ###########################################################
    def _schedOnPickCity(self, event=None):
        from frontEnd.ScheduleLocationPicker import schedOnPickCity
        schedOnPickCity(self, self._schedLatCtrl, self._schedLonCtrl,
                        self._schedLocationHint)


    ###########################################################
    def _schedOnQuickSelect(self, event):
        label = event.GetEventObject().GetLabel()
        wkdays = {'Mon', 'Tue', 'Wed', 'Thu', 'Fri'}
        wkends = {'Sat', 'Sun'}
        for day, cb in self._schedDayChecks.items():
            if label == 'All':
                cb.SetValue(True)
            elif label == 'Weekdays':
                cb.SetValue(day in wkdays)
            elif label == 'Weekends':
                cb.SetValue(day in wkends)
            else:
                cb.SetValue(False)


    ###########################################################
    def getSchedule(self):
        """Return the schedule dict as currently configured in the UI."""
        schedule = {}
        activeDays = [d for d, cb in self._schedDayChecks.items()
                      if cb.GetValue()]
        activeSet = set(activeDays)
        if activeSet == {'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'}:
            schedule['dayType'] = 'Every day'
            schedule['customDays'] = []
        elif activeSet == {'Mon', 'Tue', 'Wed', 'Thu', 'Fri'}:
            schedule['dayType'] = 'Weekdays'
            schedule['customDays'] = []
        elif activeSet == {'Sat', 'Sun'}:
            schedule['dayType'] = 'Weekends'
            schedule['customDays'] = []
        else:
            schedule['dayType'] = 'Custom...'
            schedule['customDays'] = activeDays

        if self._schedAllDayRadio.GetValue():
            schedule.update({'is24Hours': True,
                             'startHour': 0, 'startMin': 0,
                             'stopHour': 0,  'stopMin': 0,
                             'startType': 'fixed', 'startOffset': 0,
                             'stopType':  'fixed', 'stopOffset':  0,
                             'latitude': None, 'longitude': None})
        elif self._schedFixedRadio.GetValue():
            sp = self._schedStartTime.GetValue().split(':')
            ep = self._schedStopTime.GetValue().split(':')
            schedule.update({'is24Hours': False,
                             'startHour': int(sp[0]), 'startMin': int(sp[1][:2]),
                             'stopHour':  int(ep[0]), 'stopMin':  int(ep[1][:2]),
                             'startType': 'fixed', 'startOffset': 0,
                             'stopType':  'fixed', 'stopOffset':  0,
                             'latitude': None, 'longitude': None})
        else:
            _ch = ["Sunrise", "Sunset"]
            sType = _ch[self._schedSolarStartType.GetSelection()].lower()
            eType = _ch[self._schedSolarStopType.GetSelection()].lower()
            latStr = self._schedLatCtrl.GetValue().strip()
            lonStr = self._schedLonCtrl.GetValue().strip()
            try:
                lat = float(latStr) if latStr else None
            except ValueError:
                lat = None
            try:
                lon = float(lonStr) if lonStr else None
            except ValueError:
                lon = None
            schedule.update({'is24Hours': False,
                             'startHour': 0, 'startMin': 0,
                             'stopHour':  0, 'stopMin': 0,
                             'startType': sType,
                             'startOffset': self._schedSolarStartOffset.GetValue(),
                             'stopType':  eType,
                             'stopOffset':  self._schedSolarStopOffset.GetValue(),
                             'latitude': lat, 'longitude': lon})
        return schedule


    ###########################################################
    def OnCustomSoundChange(self, event):
        """Handle a change to the custom sound path.

        @param  event  The event (ignored).
        """
        if self._ignoreCustomFieldUpdate:
            return

        promptUserIfRemotePathEvtHandler(event)

        customPath = self._customSoundField.GetValue()
        self._lastCustomSoundPath = customPath

        # Record whatever the field shows, even a path that doesn't exist.
        # This used to keep the last good path in the rule while the field
        # showed a bad one, so the rule saved with a sound the user could no
        # longer see; now checkResponses sees the bad path and says so.
        self.OnUiChange(event)
        if os.path.isfile(resolveSoundPath(customPath)):
            wx.CallLater(100, self._playSound)


    ###########################################################
    def OnEmailAddrChange(self, event):
        """Handle a change in the email address field.

        We just make sure that the checkbox is checked if the user types in
        an email address.

        @param  event  The event.
        """
        if self._ignoreCustomFieldUpdate:
            return

        emailFieldValue = self._emailField.GetValue()
        if emailFieldValue:
            self._emailCheckbox.SetValue(1)

        self.OnUiChange(event)


    ###########################################################
    def OnCommandChange(self, event):
        """Handle a change in the command field.

        We just make sure that the checkbox is checked if the user types in
        a command.

        @param  event  The event.
        """
        if self._ignoreCustomFieldUpdate:
            return

        if self._commandField.GetValue():
            self._commandCheckbox.SetValue(1)

        self.OnUiChange(event)

    ############################################################
    def OnWebhookEdit(self, event):
        """Test the specified webhook.

        @param  event  The button event.
        """
        dlg = WebhookEditor(self, self._webhookURI, self._webhookContentType, self._webhookContent)
        try:
            if dlg.ShowModal() == wx.OK:
                self._webhookURI = dlg.URI()
                self._webhookContentType = dlg.ContentType()
                self._webhookContent = dlg.Content()
                self.OnUiChange(event)
        finally:
            dlg.Destroy()


    ############################################################
    def OnCommandTest(self, event):
        """Test the specified command.

        @param  event  The button event.
        """
        command = ''
        responseConfigList = self._dataModel.getResponses()
        for responseName, config in responseConfigList:
            if responseName == kCommandResponse:
                # Set the field if needed...
                # ...only do if needed to avoid loop (SetValue fires an event)
                command = config.get(kCommandResponseLookup, '')
                break

        if not command:
            wx.MessageBox("You must enter a command to test.", _kCommandErrorTitleStr,
                          wx.OK | wx.ICON_ERROR, self.GetTopLevelParent())
            return

        # shlex expects str in Python 3 — no encode() needed.
        lex = shlex.shlex(command, posix=(sys.platform=="darwin"))
        lex.whitespace_split = True
        lex.commenters = ''
        commandList = list(lex)

        if not shutil.which(commandList[0]):
            wx.MessageBox(_kCommandNotFoundErrorStr, _kCommandErrorTitleStr,
                          wx.OK | wx.ICON_ERROR, self.GetTopLevelParent())
            return

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        try:
            Popen(commandList,
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL,
                  close_fds=(sys.platform=='darwin'),
                  creationflags=flags)
        except Exception:
            wx.MessageBox(_kCommandErrorStr, _kCommandErrorTitleStr,
                          wx.OK | wx.ICON_ERROR, self.GetTopLevelParent())


    ############################################################
    def OnIftttSettings(self, event):
        """Open IFTTT Webhooks configuration dialog."""
        dlg = _IftttSettingsDialog(self.GetTopLevelParent(),
                                   self._iftttKey, self._iftttEventName)
        if dlg.ShowModal() == wx.ID_OK:
            self._iftttKey, self._iftttEventName = dlg.getValues()
            # Into the rule now, not whenever some other control next changes:
            # otherwise saving right after this still finds the old, empty
            # key and event name and refuses the rule.
            self.OnUiChange()
        dlg.Destroy()


    ############################################################
    def OnIftttTest(self, event):
        """Test the IFTTT Webhooks response."""
        if not self._iftttKey or not self._iftttEventName:
            wx.MessageBox(
                "Please click Settings to enter your IFTTT Webhooks key and "
                "event name before testing.",
                "IFTTT not configured", wx.OK | wx.ICON_INFORMATION,
                self.GetTopLevelParent())
            return
        camera = self._dataModel.getVideoSource().getLocationName()
        rule = self._dataModel.getName()
        seconds = int(time.time())
        self._backEndClient.sendIftttMessage(camera, rule, seconds,
                                             self._iftttKey, self._iftttEventName)


##############################################################################
class _IftttSettingsDialog(wx.Dialog):
    """Dialog for configuring IFTTT Webhooks key and event name."""

    def __init__(self, parent, iftttKey, iftttEventName):
        super(_IftttSettingsDialog, self).__init__(
            parent, title="IFTTT Webhooks Settings",
            style=wx.DEFAULT_DIALOG_STYLE)

        instructions = (
            "To set up IFTTT Webhooks:\n"
            "1. Go to ifttt.com → Services → Webhooks → Settings\n"
            "2. Copy the URL shown (or just the key) into Webhook Key below.\n"
            "3. Create an Applet using the 'Receive a web request' trigger.\n"
            "4. Enter the Event Name you chose for that applet.\n\n"
            "When a rule fires, the event is sent with:\n"
            "  value1 = camera name,  value2 = rule name,  value3 = timestamp"
        )
        instrLabel = wx.StaticText(self, -1, instructions)

        keyLabel = wx.StaticText(self, -1, "Webhook Key:")
        self._keyCtrl = wx.TextCtrl(self, -1, iftttKey, size=(320, -1))

        eventLabel = wx.StaticText(self, -1, "Event Name:")
        self._eventCtrl = wx.TextCtrl(self, -1, iftttEventName, size=(320, -1))

        grid = wx.FlexGridSizer(rows=2, cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1)
        grid.Add(keyLabel, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._keyCtrl, 0, wx.EXPAND)
        grid.Add(eventLabel, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._eventCtrl, 0, wx.EXPAND)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(instrLabel, 0, wx.ALL, 12)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        sizer.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(sizer)
        self.Fit()
        self.CenterOnParent()

        self.Bind(wx.EVT_BUTTON, self._onOK, id=wx.ID_OK)

    def _onOK(self, event):
        if not self._keyCtrl.GetValue().strip():
            wx.MessageBox("Please enter your IFTTT Webhooks key.",
                          "Missing key", wx.OK | wx.ICON_WARNING, self)
            return
        if not self._eventCtrl.GetValue().strip():
            wx.MessageBox("Please enter the IFTTT event name.",
                          "Missing event name", wx.OK | wx.ICON_WARNING, self)
            return
        self.EndModal(wx.ID_OK)

    def getValues(self):
        """Return (iftttKey, iftttEventName)."""
        return (self._keyCtrl.GetValue().strip(),
                self._eventCtrl.GetValue().strip())


##############################################################################
def checkResponses(dataModel, backEndClient, topLevelParent):
    """Check to see if the responses are OK.

    NOTE: This will also update responses based on the current triggers, and
    store the sound's path relative to the install when the file is inside it
    (see InstallPaths.portableSoundPath).

    We'll display a message to the user if it's not.

    @param  dataModel       The SavedQueryDataModel.
    @param  backEndClient   Client to the back end.
    @param  topLevelParent  We'll use this as the parent for any wx.MessageBox
                            errors we show.
    @return isOk            True if OK, False if not.
    """
    triggers = dataModel.getTriggers()
    responseConfigList = dataModel.getResponses()
    for responseName, config in responseConfigList:
        if (responseName == kRecordResponse):
            # Ensure the preRecord is set correctly.
            prevPreRecord = config['preRecord']
            config['preRecord'] = kDefaultPreRecord
            for trigger in triggers:
                if hasattr(trigger, 'getWantMoreThan'):
                    if trigger.getWantMoreThan():
                        config['preRecord'] = \
                            kDefaultPreRecord+trigger.getMoreThanValue()
            if config['preRecord'] != prevPreRecord:
                dataModel.setResponses(responseConfigList)

        elif (responseName == kPushResponse) and config.get('isEnabled'):
            if hasPaidEdition(backEndClient.getLicenseData()):
                if not backEndClient.enableNotifications():
                    wx.MessageBox(_kPushRegFailedStr, _kPushRegFailedTitleStr,
                                  wx.OK | wx.ICON_ERROR, topLevelParent)
                    return False

        # IFTTT and webhooks only have controls in the paid edition, and the
        # back end ignores them without it.  A rule switched on under a paid
        # licence can't be switched off without one, so don't refuse to save
        # it over something the user has no way to fix.
        elif (responseName == kIftttResponse) and config.get('isEnabled') and \
             hasPaidEdition(backEndClient.getLicenseData()):
            # The back end drops the response unless it has both.
            if not config.get('iftttKey') or not config.get('iftttEventName'):
                wx.MessageBox(_kNoIftttSettingsStr, _kIftttErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kWebhookResponse) and config.get('isEnabled') and \
             hasPaidEdition(backEndClient.getLicenseData()):
            uri = config.get('webhookUri') or ''
            content = config.get('webhookContent') or ''
            contentType = config.get('webhookContentType') or ''
            if not uri.strip():
                wx.MessageBox(_kNoWebhookStr, _kWebhookErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False
            error = _validateWebhook(uri, contentType, content)
            if error is not None:
                wx.MessageBox(_kBadWebhookStr % error, _kWebhookErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kEmailResponse) and config.get('isEnabled'):
            # Get address, handling old-style rules...
            toAddrs = config.get('toAddrs', None)
            if toAddrs is None:
                # Loaded old-style rule.  Grab from back end settings...
                emailSettings = backEndClient.getEmailSettings()
                toAddrs = emailSettings.get('toAddrs', "")

            try:
                toAddrs = toAddrs.encode('ascii', 'strict')
            except UnicodeEncodeError as e:
                wx.MessageBox(_kBadEmailAddrStr % (e.object[e.start:e.start+1]),
                              _kBadEmailAddrTitleStr,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

            if not toAddrs:
                wx.MessageBox(_kNoEmailAddrStr, _kNoEmailAddrTitleStr,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

            # Just check 'fromAddr' to make sure it's configured...
            # ...the EmailSetupDialog should ensure that if fromAddr is there
            # that the rest is OK...
            emailSettings = backEndClient.getEmailSettings()
            if not emailSettings.get('fromAddr'):
                wx.MessageBox(_kNoEmailAccountStr, _kNoEmailAccountTitleStr,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False
        elif (responseName == kCommandResponse) and config.get('isEnabled'):
            command = config.get(kCommandResponseLookup)
            if not command:
                wx.MessageBox(_kNoCommandStr, _kNoCommandTitleStr,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kFtpResponse) and config.get('isEnabled'):
            # We just check to see whether 'host' is defined.  If it's not,
            # then we've got a problem.  If it is, we know we must be OK since
            # the config dialog won't let you get away with partially configing.
            ftpSettings = backEndClient.getFtpSettings()
            if not ftpSettings.get('host'):
                wx.MessageBox(_kFtpErrorStr, _kFtpErrorTitleStr,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kLocalExportResponse) and config.get('isEnabled'):
            # Ensure a valid path is defined.
            if not config.get('exportPath') or \
               not os.path.isdir(config.get('exportPath')):
                wx.MessageBox(_kLocalExportErrorLabel, _kLocalExportErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kSnapshotResponse) and config.get('isEnabled'):
            # With no folder the back end drops every snapshot (see
            # DataManager.saveEventSnapshot); a missing one is refused the way
            # the local export folder is.
            snapshotPath = config.get('snapshotPath', '')
            if not snapshotPath:
                wx.MessageBox(_kNoSnapshotFolderStr, _kSnapshotErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False
            if not os.path.isdir(snapshotPath):
                wx.MessageBox(_kBadSnapshotFolderStr % snapshotPath,
                              _kSnapshotErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kIHostResponse) and config.get('isEnabled'):
            # Ticking the box doesn't open Edit..., and with no device the
            # back end quietly sends nothing.
            if not config.get('ihostDevice'):
                wx.MessageBox(_kNoIHostDeviceStr, _kIHostErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

        elif (responseName == kSoundResponse) and config.get('isEnabled'):
            # The file that will actually play: a sound from the list by name
            # (the path the Rule Editor showed), anything else from its path.
            soundName = config.get('soundName')
            storedPath = config.get('soundPath', '')
            soundPath = None
            if soundName != _kCustomSoundLabel:
                soundPath = _getDefaultSounds().get(soundName)
            if not soundPath:
                soundPath = resolveSoundPath(storedPath)

            # Store it the portable way -- relative to the install when it is
            # inside it -- which also mends rules still holding the full path
            # of an older copy of the app.  Only the rule changes here; the
            # file is checked below.
            portablePath = portableSoundPath(soundPath)
            if portablePath != storedPath:
                config['soundPath'] = portablePath
                dataModel.setResponses(responseConfigList)

            if not soundPath:
                wx.MessageBox(_kNoSoundFileStr, _kSoundErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False
            if not os.path.isfile(soundPath):
                wx.MessageBox(_kMissingSoundFileStr % soundPath,
                              _kSoundErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False
            if not _isPlayableSound(soundPath):
                wx.MessageBox(_kBadSoundFileStr % soundPath, _kSoundErrorTitle,
                              wx.OK | wx.ICON_ERROR, topLevelParent)
                return False

    return True


##############################################################################
def hasResponses(dataModel):
    """Check to see if any responses are configured.

    @param  dataModel     The SavedQueryDataModel.
    @return hasResponses  True if any responses are configured.
    """
    responseConfigList = dataModel.getResponses()
    for _, config in responseConfigList:
        if config.get('isEnabled'):
            return True

    return False


##############################################################################
def test_main():
    """OB_REDACT
       Contains various self-test code.
    """
    print("NO TESTS")


##############################################################################
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_main()
    else:
        print("Try calling with 'test' as the argument.")
