#!/usr/bin/env python

#*****************************************************************************
#
# RuleListPanel.py
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
import time
import locale

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.wx.BackgroundStyleUtils import kBackgroundStyle
from vitaToolbox.wx.BetterScrolledWindow import BetterScrolledWindow
from vitaToolbox.wx.BitmapFromFile import bitmapFromFile
from vitaToolbox.wx.BorderImagePanel import BorderImagePanel
from vitaToolbox.wx.GradientEndedLine import GradientEndedLine
from vitaToolbox.wx.HoverBitmapButton import HoverBitmapButton
from vitaToolbox.wx.HoverButton import HoverButton
from vitaToolbox.wx.HoverButton import kHoverButtonNormalColor_Plate
from vitaToolbox.wx.HoverButton import kHoverButtonDisabledColor_Plate
from vitaToolbox.wx.HoverButton import kHoverButtonPressedColor_Plate
from vitaToolbox.wx.HoverButton import kHoverButtonHoverColor_Plate
from vitaToolbox.wx.OverlapSizer import OverlapSizer
from vitaToolbox.wx.TextSizeUtils import makeFontDefault
from vitaToolbox.wx.TranslucentStaticText import TranslucentStaticText
from vitaToolbox.sysUtils.TimeUtils import formatTime
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.networking.TapoControl import TapoController
from vitaToolbox.networking.TapoControl import parseTapoTarget
from vitaToolbox.networking.TapoControl import isAuthError
from vitaToolbox.networking.TapoControl import kOpSirenOn, kOpSirenOff
from vitaToolbox.networking.TapoControl import kOpLightOn, kOpLightOff
from vitaToolbox.networking.TapoControl import kOpLightState

# Local imports...
from appCommon.CommonStrings import kCommandResponse
from appCommon.CommonStrings import kEmailResponse
from appCommon.CommonStrings import kIftttResponse
from appCommon.CommonStrings import kWebhookResponse
from appCommon.CommonStrings import kPushResponse
from appCommon.CommonStrings import kRecordResponse
from appCommon.CommonStrings import kSearchViewDefaultRules
from appCommon.CommonStrings import kSoundResponse
from appCommon.CommonStrings import kFtpResponse
from appCommon.CommonStrings import kLocalExportResponse
from appCommon.CommonStrings import kSnapshotResponse
from appCommon.CommonStrings import kIHostResponse
from appCommon.CommonStrings import kTapoResponse
from appCommon.CommonStrings import kFrontEndLogName
from backEnd.SavedQueryDataModel import SavedQueryDataModel
from .FrontEndPrefs import getTapoCredentials
from .QueryEditorDialog import QueryEditorDialog
from .RuleScheduleDialog import RuleScheduleDialog


_kCtrlPadding = 8
_kShadowSize = 4
_kDividerEdgeColorWin = (171, 214, 245, 0)
_kDividerColorWin = (171, 214, 245, 255)
_kDividerEdgeColorMac = (180, 180, 180, 0)
_kDividerColorMac = (180, 180, 180, 255)
_kMaxGradientWidth = 98
_kDividerHeight = 2
_kMinHeight = 140

_kResponseBitmapMap = {
    kCommandResponse : ("frontEnd/bmps/Response_Command_Enabled.png",
                        "frontEnd/bmps/Response_Command_Disabled.png"),
    kEmailResponse :   ("frontEnd/bmps/Response_Email_Enabled.png",
                        "frontEnd/bmps/Response_Email_Disabled.png"),
    kIftttResponse :    ("frontEnd/bmps/Response_Email_Enabled.png",
                         "frontEnd/bmps/Response_Email_Disabled.png"),
    kWebhookResponse :  ("frontEnd/bmps/Response_Email_Enabled.png",
                         "frontEnd/bmps/Response_Email_Disabled.png"),
    kPushResponse :    ("frontEnd/bmps/Response_Email_Enabled.png",
                        "frontEnd/bmps/Response_Email_Disabled.png"),
    kRecordResponse :  ("frontEnd/bmps/Response_Save_Enabled.png",
                        "frontEnd/bmps/Response_Save_Disabled.png"),
    kSoundResponse :   ("frontEnd/bmps/Response_Sound_Enabled.png",
                        "frontEnd/bmps/Response_Sound_Disabled.png"),
    kFtpResponse:      ("frontEnd/bmps/Response_SendClip_Enabled.png",
                        "frontEnd/bmps/Response_SendClip_Disabled.png"),
    kLocalExportResponse:  ("frontEnd/bmps/Response_SendClip_Enabled.png",
                            "frontEnd/bmps/Response_SendClip_Disabled.png"),
    kSnapshotResponse:     ("frontEnd/bmps/Response_Save_Enabled.png",
                            "frontEnd/bmps/Response_Save_Disabled.png"),
    kIHostResponse:        ("frontEnd/bmps/Response_Command_Enabled.png",
                            "frontEnd/bmps/Response_Command_Disabled.png"),
    kTapoResponse:         ("frontEnd/bmps/Response_Command_Enabled.png",
                            "frontEnd/bmps/Response_Command_Disabled.png"),
}

# Tapo camera control -- the siren and white-spotlight buttons in the header
# row.  See vitaToolbox/networking/TapoControl.py for the camera side.
#
# The artwork carries the on/off state; the labels beside the buttons are
# static and only say which button is which.
_kSirenLabel = "Siren"
_kLightLabel = "Light"

# (normal, pressed, hovered) for each control in each state.  A missing "off"
# file falls back to the "on" one -- see _loadTapoBmps.
_kSirenBmps = {
    True:  ('frontEnd/bmps/Siren_On_Enabled.png',
            'frontEnd/bmps/Siren_On_Pressed.png',
            'frontEnd/bmps/Siren_On_Hover.png'),
    False: ('frontEnd/bmps/Siren_Off_Enabled.png',
            'frontEnd/bmps/Siren_Off_Pressed.png',
            'frontEnd/bmps/Siren_Off_Hover.png'),
}
_kLightBmps = {
    True:  ('frontEnd/bmps/Light_On_Enabled.png',
            'frontEnd/bmps/Light_On_Pressed.png',
            'frontEnd/bmps/Light_On_Hover.png'),
    False: ('frontEnd/bmps/Light_Off_Enabled.png',
            'frontEnd/bmps/Light_Off_Pressed.png',
            'frontEnd/bmps/Light_Off_Hover.png'),
}

_kSirenOffTip = ("Sound this camera's siren.\n"
                 "The camera stops on its own after the alarm duration set in "
                 "the Tapo app.")
_kSirenOnTip  = "This camera's siren is sounding.  Click to silence it."
_kLightOffTip = "Switch this camera's white spotlight on."
_kLightOnTip  = "This camera's spotlight is on.  Click to switch it off."

_kNoCameraTip = "Select a camera to control its siren and spotlight."
_kNotTapoTip  = ("This camera has no usable address and credentials for local "
                 "control.")

_kControlFailTitle = "Camera control failed"

# Shown when the camera answers but refuses the credentials.  Worth spelling
# out: the account that streams RTSP genuinely is not the account that controls
# the camera, and no amount of retrying will change that.
_kAuthFailMsg = (
"""%s refused these credentials for control.

The camera account that streams your video is not accepted here.  Enter the """
"""email address and password you sign in to the Tapo app with, under """
"""Tools -> Options -> Tapo, then try again.

Camera reported: %s""")


##############################################################################
def _loadTapoBmps(bmpPaths, logger):
    """Load the on/off bitmap triples for one of the camera-control buttons.

    A control needs six files and it is easy to land only three of them, so a
    file that isn't there falls back to its opposite-state counterpart rather
    than taking the whole front end down at startup (bitmapFromFile goes
    through PIL, which raises on a missing path).  The button then works, it
    just can't show its state until the art arrives.

    @param  bmpPaths  {isOn: (normalPath, pressedPath, hoveredPath)}.
    @param  logger    Logger to report substitutions to.
    @return bmps      {isOn: (normalBmp, pressedBmp, hoveredBmp)}.
    """
    bmps = {}
    for isOn, paths in bmpPaths.items():
        loaded = []
        for i, path in enumerate(paths):
            try:
                loaded.append(bitmapFromFile(path))
            except Exception:
                fallback = bmpPaths[not isOn][i]
                logger.warning("Missing camera control bitmap %s; using %s"
                               % (path, fallback))
                loaded.append(bitmapFromFile(fallback))
        bmps[isOn] = tuple(loaded)
    return bmps


_kUS12 = "%B %d, %Y - %I:%M:%S %p"
_kUS24 = "%B %d, %Y - %H:%M:%S"
_kNonUS12 = "%d %B %Y - %I:%M:%S %p"
_kNonUS24 = "%d %B %Y - %H:%M:%S"
_kISO24 = "%Y-%m-%d - %H:%M:%S"


class RuleListPanel(BorderImagePanel):
    """Implements a panel for displaying and configuring rules."""
    ###########################################################
    def __init__(self, parent, backEndClient, dataManager, searchFunc,
                 cameraEnabledModel):
        """The initializer for RuleListPanel.

        @param  parent              The parent Window.
        @param  backEndClient       A connection to the back end app.
        @param  dataManager         The data manager for the app.
        @param  searchFunc          The function to call when a search is
                                    requested.  Takes camera location & query
                                    name as a parameter.
        @param  cameraEnabledModel  A data model that provides updates when
                                    cameras are enabled or disabled.
        """
        # Call the base class initializer
        super(RuleListPanel, self).__init__(parent, -1,
                                    'frontEnd/bmps/RaisedPanelBorder.png', 8)

        self._backEndClient = backEndClient
        self._dataManager = dataManager
        self._searchFunc = searchFunc
        self._cameraEnabledModel = cameraEnabledModel

        self._logger = getLogger(kFrontEndLogName)

        # Register with the data model.
        self._cameraEnabledModel.addListener(self._handleCameraEnable,
                                             wantKeyParam=True)

        # Register for time preference changes.
        self.GetTopLevelParent().getUIPrefsDataModel().addListener(
                self._handleTimePrefChange, key='time')

        self._timeFormatString = _kNonUS24

        # Referenced (and guarded against) by OnDateTimer(), which _initUi()
        # calls once below before the timer itself is created.
        self._dateTimer = None

        # Siren / spotlight state for the selected camera.  Set before
        # _initUi(), which builds the buttons that read it.
        #
        # The camera has no reliable "is the manual alarm sounding" query, so
        # _sirenOn is OUR record of what we asked for -- and the camera stops
        # on its own after its configured duration, so it can read On after the
        # camera has gone quiet.  Clicking again sends a stop either way, which
        # an already-stopped camera is happy to accept.  _lightOn does have a
        # real query behind it and is refreshed on every camera selection.
        self._tapoTarget = None     # (host, user, password) for _curLocation
        self._sirenOn = {}          # camera location -> bool
        self._lightOn = {}          # camera location -> bool, or absent
        self._tapoBusy = False

        # Set before _initUi() rather than after it, because the siren/light
        # buttons it builds read this to pick their tooltip.
        self._curLocation = None

        # Initialize the UI controls
        self._initUi()

        self._controls = {}

        # Create a timer to update the time label
        self._dateTimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnDateTimer)
        self._dateTimer.Start(1000, False)

        # _dateTimer repeats continuously and is never explicitly stopped
        # elsewhere, so it must be stopped here or it fires into this panel
        # after it's destroyed -- a native access violation (same bug class
        # fixed in CameraSetupWizard.py and QueryConstructionView.py).
        self.Bind(wx.EVT_WINDOW_DESTROY, self._onDateTimerDestroy)

        minw, minh = self.GetMinSize()
        self.SetMinSize((minw, max(minh, _kMinHeight)))


    ###########################################################
    def _initUi(self):
        """Initialize the UI controls."""
        # Horizontal sizer with Record Icon/Camera Name/Date/Responses
        topSizer = wx.BoxSizer(wx.HORIZONTAL)
        self._recordingEnabledButton = \
            HoverBitmapButton(self, wx.ID_ANY,
                                'frontEnd/bmps/Monitor_On_Enabled.png',
                                wx.EmptyString,
                                'frontEnd/bmps/Monitor_On_Pressed.png',
                                'frontEnd/bmps/Monitor_Off_Enabled.png',
                                'frontEnd/bmps/Monitor_On_Hover.png')
        self._recordingDisabledButton = \
            HoverBitmapButton(self, wx.ID_ANY,
                                'frontEnd/bmps/Monitor_Off_Enabled.png',
                                wx.EmptyString,
                                'frontEnd/bmps/Monitor_Off_Pressed.png',
                                'frontEnd/bmps/Monitor_Off_Enabled.png',
                                'frontEnd/bmps/Monitor_Off_Hover.png')
        self._recordingDisabledButton.Show(False)
        self._recordingEnabledButton.Disable()
        self._recordingDisabledButton.Disable()
        self._offText = TranslucentStaticText(self, -1, "Off")
        self._offText.Hide()
        self._onText = TranslucentStaticText(self, -1, "On")
        makeFontDefault(self._offText, self._onText)

        # Siren / spotlight for the selected camera.  Both start in the "off"
        # artwork, which doubles as the disabled bitmap the way the record
        # button above uses Monitor_Off_Enabled: HoverBitmapButton falls back
        # to the NORMAL bitmap when given no disabled one, which would leave a
        # dead button looking exactly like a live one.
        self._sirenBmps = _loadTapoBmps(_kSirenBmps, self._logger)
        self._lightBmps = _loadTapoBmps(_kLightBmps, self._logger)
        sirenOff = self._sirenBmps[False]
        lightOff = self._lightBmps[False]
        self._sirenButton = \
            HoverBitmapButton(self, wx.ID_ANY, sirenOff[0], wx.EmptyString,
                              sirenOff[1], sirenOff[0], sirenOff[2])
        self._lightButton = \
            HoverBitmapButton(self, wx.ID_ANY, lightOff[0], wx.EmptyString,
                              lightOff[1], lightOff[0], lightOff[2])
        self._sirenButton.Disable()
        self._lightButton.Disable()
        self._sirenText = TranslucentStaticText(self, -1, _kSirenLabel)
        self._lightText = TranslucentStaticText(self, -1, _kLightLabel)
        makeFontDefault(self._sirenText, self._lightText)

        self._camLocLabel = TranslucentStaticText(self, -1,
                                                  "No Camera Selected",
                                                  style=wx.ST_ELLIPSIZE_END |
                                                        wx.ALIGN_CENTER)
        self._camLocLabel.SetMinSize((1, -1))
        font = self._camLocLabel.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self._camLocLabel.SetFont(font)
        self._dateLabel = TranslucentStaticText(self, -1, "")
        makeFontDefault(self._dateLabel)

        hSizer = wx.BoxSizer(wx.HORIZONTAL)
        overlapSizer = OverlapSizer(True)
        overlapSizer.Add(self._recordingEnabledButton)
        overlapSizer.Add(self._recordingDisabledButton)
        hSizer.Add(overlapSizer, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL,
                   _kCtrlPadding//2)
        hSizer.Add(self._offText, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
                   _kCtrlPadding)
        hSizer.Add(self._onText, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL,
                   _kCtrlPadding)
        hSizer.Add(self._sirenButton, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL,
                   _kCtrlPadding//2)
        hSizer.Add(self._sirenText, 0, wx.LEFT | wx.RIGHT |
                   wx.ALIGN_CENTER_VERTICAL, _kCtrlPadding)
        hSizer.Add(self._lightButton, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL,
                   _kCtrlPadding//2)
        hSizer.Add(self._lightText, 0, wx.LEFT | wx.RIGHT |
                   wx.ALIGN_CENTER_VERTICAL, _kCtrlPadding)
        topSizer.Add(hSizer, 0)
        topSizer.Add(self._camLocLabel, 1, wx.ALL |
                     wx.ALIGN_CENTER_VERTICAL, _kCtrlPadding)
        hSizer2 = wx.BoxSizer(wx.HORIZONTAL)
        hSizer2.Add(self._dateLabel, 0, wx.TOP | wx.LEFT | wx.BOTTOM |
                    wx.ALIGN_CENTER_VERTICAL, _kCtrlPadding)
        hSizer2.AddSpacer(10+_kShadowSize)
        topSizer.Add(hSizer2, 0, wx.EXPAND )

        # Add the dividing line
        isWin = wx.Platform == '__WXMSW__'
        if isWin:
            dividingLine = GradientEndedLine(self, _kDividerColorWin,
                                             _kDividerEdgeColorWin,
                                             _kDividerHeight,
                                             _kMaxGradientWidth)
        else:
            dividingLine = GradientEndedLine(self, _kDividerColorMac,
                                             _kDividerEdgeColorMac,
                                             _kDividerHeight,
                                             _kMaxGradientWidth)

        # A scrolling window to contain the actual list of rules
        self._ruleWin = BetterScrolledWindow(self, -1, osxFix=(not isWin),
                                             style=wx.TRANSPARENT_WINDOW,
                                             redrawFix=isWin)
        self._ruleWin.SetBackgroundStyle(kBackgroundStyle)
        self._ruleSizer = wx.FlexGridSizer(cols=5, vgap=_kCtrlPadding//2,
                                           hgap=_kCtrlPadding//2)
        self._ruleSizer.AddGrowableCol(1, 1)
        self._ruleSizer.AddGrowableCol(2, 1)
        self._ruleWin.SetSizer(self._ruleSizer)

        # A new rule link for when a camera has none.
        self._newHyperlink = HoverButton(self, "Add new rule...",
                                         style=wx.ALIGN_LEFT)
        makeFontDefault(self._newHyperlink)
        hyperlinkSizer = wx.FlexGridSizer(2, 2, 0, 0)
        hyperlinkSizer.AddGrowableCol(1)
        hyperlinkSizer.AddGrowableRow(1)
        hyperlinkSizer.Add(self._newHyperlink)

        # Set the main sizer
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(topSizer, 0, wx.EXPAND)
        hSizer = wx.BoxSizer(wx.HORIZONTAL)
        hSizer.Add(dividingLine, 1)
        hSizer.AddSpacer(_kShadowSize)
        sizer.Add(hSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        overlapSizer = OverlapSizer(True)

        overlapSizer.Add(hyperlinkSizer, 1, wx.EXPAND | wx.ALL, _kCtrlPadding)
        overlapSizer.Add(self._ruleWin, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(overlapSizer, 1, wx.EXPAND)
        self.SetSizer(sizer)

        # The following will be used for the popup menu we create when
        # the user selects a rule.
        self._editId = wx.NewId()
        self._newId = wx.NewId()
        self._deleteId = wx.NewId()
        self.Bind(wx.EVT_MENU, self.OnEdit, id=self._editId)
        self.Bind(wx.EVT_MENU, self.OnNew, id=self._newId)
        self.Bind(wx.EVT_MENU, self.OnDelete, id=self._deleteId)
        self._ruleMenu = wx.Menu()
        self._ruleMenu.Append(self._editId, "Edit Rule...")
        self._ruleMenu.Append(self._newId, "New Rule...")
        self._ruleMenu.Append(self._deleteId, "Delete Rule...")

        self._newHyperlink.Bind(wx.EVT_BUTTON, self.OnNew)

        self._recordingEnabledButton.Bind(wx.EVT_BUTTON, self.OnEnableDisable)
        self._recordingDisabledButton.Bind(wx.EVT_BUTTON, self.OnEnableDisable)

        self._sirenButton.Bind(wx.EVT_BUTTON, self.OnSiren)
        self._lightButton.Bind(wx.EVT_BUTTON, self.OnLight)
        self._updateTapoUi()

        # Initialize the rules lists
        self._rulesCache = {}
        self.updateRulesCache()

        # Initialize the date text
        self.OnDateTimer()


    ###########################################################
    def _onDateTimerDestroy(self, event):
        """Stop our date timer when we are destroyed.

        @param  event  The EVT_WINDOW_DESTROY event.
        """
        if event.GetWindow() is self:
            if self._dateTimer is not None:
                self._dateTimer.Stop()
        event.Skip()


    ###########################################################
    def OnDateTimer(self, event=None):
        """Update the UI to reflect the current time.

        @param event  The timer event (ignored).

        NOTE: called once from _initUi() (via __init__) before self._dateTimer
        exists, so this must not guard on self._dateTimer being set -- only
        on window liveness.
        """
        # Belt-and-suspenders: bail if we're gone (see _onDateTimerDestroy).
        if not self:
            return

        prevText = self._dateLabel.GetLabel()
        newText = formatTime(self._timeFormatString)
        if prevText == newText:
            return

        # This fires once a SECOND -- every supported time format carries %S.
        # It used to follow SetLabel with self.Layout(), re-laying out the
        # WHOLE panel (rule rows and all) 60 times a minute, which is what made
        # the rules area flicker.  A relayout is only needed when the new text
        # wants more ROOM than the label already has, which the ticking seconds
        # digit essentially never does.
        needed = self._dateLabel.GetTextExtent(newText)[0]
        self._dateLabel.SetLabel(newText)
        if needed > self._dateLabel.GetSize().width:
            self.Layout()
        else:
            # TranslucentStaticText is a TRANSPARENT_WINDOW that suppresses its
            # own background erase, so the PARENT owns the pixels behind the
            # text: repaint just that rectangle, or the old digits show through
            # the new ones.
            self.RefreshRect(self._dateLabel.GetRect())


    ###########################################################
    def setCameraLocation(self, cameraLocation):
        """Update the UI to reflect a given camera location.

        @param cameraLocation  The name of the camera to display rules for.
        """
        self._ruleSizer.Clear(True)
        self._curLocation = cameraLocation

        # If no camera is selected exit
        if not cameraLocation:
            self._newHyperlink.Hide()
            self._camLocLabel.SetLabel("No Camera Selected")
            self._offText.Hide()
            self._onText.Show(True)
            self._recordingEnabledButton.Disable()
            self._recordingDisabledButton.Disable()
            self._tapoTarget = None
            self._updateTapoUi()
            self.Layout()
            return

        self._camLocLabel.SetLabel(cameraLocation)

        _, camUri, enabled, _ = \
                        self._backEndClient.getCameraSettings(cameraLocation)

        # Work out whether this camera can be shouted through.  Pure string
        # work -- selecting a camera must not touch the network.
        # Re-read the credentials on every selection rather than caching them,
        # so an account just entered on the Options dialog's Tapo tab takes
        # effect without restarting the app.
        # requireUriAuth because the query below is speculative: a placeholder
        # entry carrying no camera account of its own is not a camera anyone
        # can control, and probing it costs a connect timeout on every visit to
        # this screen.  Refusing it here greys both buttons and says why.
        controlUser, controlPassword = getTapoCredentials()
        self._tapoTarget = parseTapoTarget(camUri, controlUser, controlPassword,
                                           requireUriAuth=True)
        self._updateTapoUi()
        if self._tapoTarget is not None:
            # The spotlight is the one piece of state the camera will tell us,
            # so ask -- coalesced, since clicking down a list of cameras leaves
            # only the last answer worth having.
            self._submitTapo(kOpLightState, coalesceKey=kOpLightState,
                             blocking=False)

        self._onText.Show(enabled)
        self._offText.Show(not enabled)
        self._recordingEnabledButton.Show(enabled)
        self._recordingDisabledButton.Show(not enabled)
        self._recordingEnabledButton.Enable()
        self._recordingDisabledButton.Enable()

        # Retrieve information about rules at this location
        self.updateRulesCache(cameraLocation)
        self._controls = {}
        for ruleName, queryName, schedStr, enabled, responses in \
                self._rulesCache.get(cameraLocation, []):
            self._getRuleControls(ruleName, queryName, schedStr, enabled,
                                  responses)
        self._addControls()


    ###########################################################
    def _getRuleControls(self, ruleName, queryName, schedStr, enabled,
                         responses):
        """Create controls for a rule.

        @param  ruleName   The name of the rule.
        @param  queryName  The name of the rule's query.
        @param  schedStr   A string representation of the rule's schedule.
        @param  enabled    True if the rule is enabled.
        @param  responses  A list of names of enabled responses.
        """
        # Create a shortcut to the search
        searchIcon = HoverBitmapButton(self._ruleWin, wx.ID_ANY,
                                   'frontEnd/bmps/Search_link_enabled.png',
                                    wx.EmptyString,
                                   'frontEnd/bmps/Search_link_pressed.png',
                                   'frontEnd/bmps/Search_link_disabled.png',
                                   'frontEnd/bmps/Search_link_hover.png',
                                   useMask=False)
        # Create a check box indicating whether the control is enabled
        checkBox = wx.CheckBox(self._ruleWin, name=ruleName)
        checkBox.SetValue(enabled)
        checkBox.Show(len(responses) != 0)
        # Create a control for accessing operations for the rule
        nameButton = \
            HoverButton(self._ruleWin, queryName,
                        kHoverButtonNormalColor_Plate,
                        kHoverButtonDisabledColor_Plate,
                        kHoverButtonPressedColor_Plate,
                        kHoverButtonHoverColor_Plate, -1, style=wx.ALIGN_LEFT,
                        ignoreExtraSpace=True)
        nameButton.SetMinSize((1, -1))
        nameButton.SetName(ruleName)
        makeFontDefault(nameButton)
        nameButton.SetMenu(self._ruleMenu)
        # Create a control for accessing the schedule
        schedButton = HoverButton(self._ruleWin, schedStr, style=wx.ALIGN_LEFT,
                                  ignoreExtraSpace=True)
        schedButton.SetMinSize((1, -1))
        makeFontDefault(schedButton)
        schedButton.Show(len(responses) != 0)
        # Create controls for displaying information about responses
        responseControls = []
        alreadyAdded = set()
        for response in responses:
            enabledBmp, disabledBmp = _kResponseBitmapMap[response]
            if ((enabledBmp, disabledBmp)) not in alreadyAdded:
                button = HoverBitmapButton(self._ruleWin, wx.ID_ANY, enabledBmp, wx.EmptyString,
                                           bmpDisabled=disabledBmp, useMask=False)
                button.Enable(enabled)
                responseControls.append(button)

            # Keep track of bitmaps we've already added.  If two responses
            # use the same icon, we don't want to add twice.
            alreadyAdded.add((enabledBmp, disabledBmp))

        self._controls[ruleName] = [searchIcon, checkBox, nameButton,
                                    schedButton, responseControls]

        # Bind to events
        searchIcon.Bind(wx.EVT_BUTTON, self.OnSearch)
        checkBox.Bind(wx.EVT_CHECKBOX, self.OnCheckBox)
        schedButton.Bind(wx.EVT_BUTTON, self.OnScheduleButton)
        for control in responseControls:
            control.Bind(wx.EVT_BUTTON, self.OnEdit)


    ###########################################################
    def _addControls(self):
        """Add all controls to the list in a sorted order."""
        self._ruleSizer.Clear(False)

        names = list(self._controls.keys())
        names.sort(key=lambda x: x.lower())

        enabled = []
        disabled = []
        noResponses = []

        for name in names:
            _, checkBox, _, _, _ = self._controls[name]
            if not checkBox.IsShown():
                noResponses.append(name)
            elif checkBox.GetValue():
                enabled.append(name)
            else:
                disabled.append(name)

        names = enabled + disabled + noResponses
        for name in names:
            searchIcon, checkBox, nameButton, schedButton, responses =\
                    self._controls[name]
            self._ruleSizer.Add(searchIcon, 0, wx.ALIGN_CENTER_VERTICAL)
            self._ruleSizer.Add(nameButton, 1, wx.ALIGN_LEFT | wx.EXPAND |
                                wx.ALIGN_CENTER_VERTICAL)
            self._ruleSizer.Add(schedButton, 1, wx.ALIGN_LEFT | wx.EXPAND |
                                wx.ALIGN_CENTER_VERTICAL)
            hSizer = wx.BoxSizer(wx.HORIZONTAL)
            for control in responses:
                hSizer.Add(control, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT |
                           wx.RIGHT, 2)
            self._ruleSizer.Add(hSizer, 0,
                                wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
            hSizer = wx.BoxSizer(wx.HORIZONTAL)
            hSizer.Add(checkBox, 0)
            hSizer.AddSpacer(_kShadowSize)
            self._ruleSizer.Add(hSizer, 0, wx.ALIGN_CENTER_VERTICAL)

        self._ruleWin.Layout()
        self._ruleWin.FitInside()

        numRules = len(self._rulesCache.get(self._curLocation, []))
        self._ruleWin.Show(numRules > 0)
        if self._curLocation:
            self._newHyperlink.Show(numRules == 0)

        self.Layout()


    ###########################################################
    def OnSize(self, event=None):
        """Respond to a resize event.

        @param event  The size event (ignored).
        """
        # On windows we need to refresh on size events or we get trails from
        # the rounded edges.
        self.Refresh()
        event.Skip()


    ###########################################################
    def OnCheckBox(self, event):
        """Respond to a check box toggle.

        @param event  The checkbox event.
        """
        checkBox = event.GetEventObject()
        ruleName = checkBox.GetName()
        self._backEndClient.enableRule(ruleName, checkBox.GetValue())

        _, _, _, schedButton, responses = self._controls[ruleName]
        if checkBox.GetValue():
            _, _, schedStr, _, _ = self._backEndClient.getRuleInfo(ruleName)
            schedButton.SetLabel(schedStr)
        else:
            schedButton.SetLabel("Disabled.")

        for control in responses:
            control.Enable(checkBox.GetValue())

        self._addControls()

        self.updateRulesCache(self._curLocation)


    ###########################################################
    def OnNew(self, event=None):
        """Create a new rule.

        @param event  The menu event (ignored).
        """
        wx.CallAfter(self._doOnNew)


    ###########################################################
    def _doOnNew(self):
        ruleCreated = False

        # Launch the query editor dialog
        newQuery = SavedQueryDataModel("")
        # Set the coordinate space of the new query to that of the video from
        # the current camera location, if possible.  We have a second chance
        # in the QueryConstructionView when an image is loaded in the video
        # window.
        procSize = self._dataManager.getProcSize(self._curLocation)
        if procSize != (0, 0):
            newQuery.setCoordSpace(procSize)
        newQuery.getVideoSource().setLocationName(self._curLocation)

        dlg = QueryEditorDialog(self.GetTopLevelParent(), self._dataManager,
                                self._backEndClient,
                                newQuery,
                                kSearchViewDefaultRules +
                                self._backEndClient.getRuleNames(),
                                [self._curLocation])

        try:
            result = dlg.ShowModal()

            # If the user cancels the dialog, do nothing
            if result == wx.ID_OK:
                # Save the new rule
                self._backEndClient.addRule(newQuery, True)
                self._backEndClient.setRuleSchedule(
                    newQuery.getName(), dlg.getSchedule())
                ruleCreated = True

        finally:
            dlg.Destroy()

        if ruleCreated:
            info = self._backEndClient.getRuleInfo(newQuery.getName())
            if not info:
                return
            curRules = self._rulesCache.get(self._curLocation, [])
            curRules.append(info)
            self._rulesCache[self._curLocation] = curRules
            self._getRuleControls(*info)
            self._addControls()


    ###########################################################
    def OnEdit(self, event):
        """Edit an existing rule.

        @param event  The menu or button event.
        """
        responseEdit = False
        queryName = ''
        eventObj = event.GetEventObject()

        if isinstance(eventObj, HoverBitmapButton) or \
           isinstance(eventObj, HoverButton):

            for name in self._controls:
                _, _, _, schedButton, responses = self._controls[name]
                if eventObj in responses or eventObj == schedButton:
                    responseEdit = True
                    queryName = name
                    break
        else:
            queryName = eventObj.GetInvokingWindow().GetName()
            if not queryName:
                return
        wx.CallAfter(self._doOnEdit, queryName, responseEdit)


    ###########################################################
    def _doOnEdit(self, queryName, responseEdit):
        """Edit an existing rule.

        @param  queryName     The name of the query to edit.
        @param  responseEdit  True if we should jump to the response block.
        """
        query = self._backEndClient.getQuery(queryName)
        origLocation = query.getVideoSource().getLocationName()

        if responseEdit:
            query.setLastEdited('response', None)
        dlg = QueryEditorDialog(self.GetTopLevelParent(), self._dataManager,
                                self._backEndClient, query,
                                set(kSearchViewDefaultRules +
                                    self._backEndClient.getRuleNames()) -
                                set([query.getName()]),
                                [origLocation])
        try:
            result = dlg.ShowModal()
            pendingSchedule = dlg.getSchedule() if result != wx.ID_CANCEL else None
        finally:
            dlg.Destroy()

        if result == wx.ID_CANCEL:
            return

        # Update the schedule on the existing rule file first; _editQuery reads
        # and propagates the rule's current schedule to the renamed/rebuilt rule,
        # so writing it before editQuery is the reliable way to preserve changes.
        self._backEndClient.setRuleSchedule(queryName, pendingSchedule)
        self._backEndClient.editQuery(query, queryName)

        # Anything could have changed...name, schedule, responses...
        # Rebuild the UI, but delay it slightly so we don't delete an object
        # that's being used on the callstack...
        wx.CallAfter(self.setCameraLocation, self._curLocation)


    ###########################################################
    def OnDelete(self, event):
        """Delete a rule.

        @param event  The menu event.
        """
        ruleName = event.GetEventObject().GetInvokingWindow().GetName()
        wx.CallAfter(self._doOnDelete, ruleName)


    ###########################################################
    def _doOnDelete(self, ruleName):
        """Delete a rule.

        @param  ruleName  The name of the rule to delete.
        """
        if wx.NO == wx.MessageBox("Delete the rule \"%s\"?" % ruleName,
                                  "Delete rule",
                                  wx.YES_NO | wx.ICON_QUESTION,
                                  self.GetTopLevelParent()):
            return

        self._backEndClient.deleteRule(ruleName)

        # Remove the rule info from _rules
        self.updateRulesCache(self._curLocation)

        # Retrieve and destroy the related controls
        searchIcon, checkBox, nameButton, schedButton, responses = \
                self._controls[ruleName]
        searchIcon.Destroy()
        checkBox.Destroy()
        wx.CallAfter(nameButton.Destroy)
        schedButton.Destroy()
        for control in responses:
            control.Destroy()
        del self._controls[ruleName]

        # Reorganize the list
        self._addControls()


    ###########################################################
    def OnScheduleButton(self, event):
        """Respond to a schedule button click.

        @param event  The hyperlink event.
        """
        button = event.GetEventObject()

        for name in self._controls:
            if button in self._controls[name]:
                wx.CallAfter(self._showScheduleDialog, name)


    ###########################################################
    def _showScheduleDialog(self, name):
        use12, _ = self.GetTopLevelParent().getUIPrefsDataModel(
                ).getTimePreferences()
        dlg = RuleScheduleDialog(self.GetTopLevelParent(), name,
                                 self._backEndClient, not use12)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

        _, _, schedStr, _, _ = self._backEndClient.getRuleInfo(name)
        # We'd prefer to just update the button's text here, but it may
        # have been destroyed while we were in the dialog.
        self.setCameraLocation(self._curLocation)
        self._ruleWin.Layout()
        return



    ###########################################################
    def OnSearch(self, event):
        """Respond to a search request.

        @param event  The button event.
        """
        button = event.GetEventObject()

        for name in self._controls:
            searchButton, checkBox, _, _, _ = self._controls[name]
            if button == searchButton:
                queryName = checkBox.GetName()
                self._searchFunc(self._curLocation, queryName)
                return


    ###########################################################
    def _handleTimePrefChange(self, uiModel):
        """Handle a change to time display preferences.

        @param  resultsModel  The UIPrefsDataModel.
        """
        _, useUS = uiModel.getTimePreferences()

        if useUS == 'us':
            self._timeFormatString = _kUS24
        elif useUS == 'intl':
            self._timeFormatString = _kNonUS24
        else:
            self._timeFormatString = _kISO24

        self.OnDateTimer()

        self.setCameraLocation(self._curLocation)


    ###########################################################
    def _handleCameraEnable(self, enableModel, camera):
        """Handle a change camera enable state.

        @param  resultsModel  Should be self._cameraEnabledModel
        @param  camera        The camera that was enabled.
        """
        assert enableModel == self._cameraEnabledModel
        assert camera is not None, "Shouldn't ever have general updates!"

        isEnabled = self._cameraEnabledModel.isEnabled(camera)
        if self._curLocation == camera:
            self._recordingEnabledButton.Show(isEnabled)
            self._recordingDisabledButton.Show(not isEnabled)
            self._onText.Show(isEnabled)
            self._offText.Show(not isEnabled)
        self.Layout()


    ###########################################################
    def OnEnableDisable(self, event):
        """Handle a request to enable or disable the current camera.

        @param  event  The EVT_BUTTON event.
        """
        assert self._curLocation

        if not self._curLocation:
            return

        needEnable = (event.GetEventObject() == self._recordingDisabledButton)
        self._backEndClient.enableCamera(self._curLocation, needEnable)

        # Technically, not needed, but makes UI update faster...
        self._cameraEnabledModel.enableCamera(self._curLocation, needEnable)


    ###########################################################
    def OnSiren(self, event):
        """Sound or silence the selected camera's siren.

        @param  event  The EVT_BUTTON event.
        """
        wantOn = not self._sirenOn.get(self._curLocation, False)
        self._submitTapo(kOpSirenOn if wantOn else kOpSirenOff)


    ###########################################################
    def OnLight(self, event):
        """Switch the selected camera's white spotlight on or off.

        @param  event  The EVT_BUTTON event.
        """
        wantOn = not self._lightOn.get(self._curLocation, False)
        self._submitTapo(kOpLightOn if wantOn else kOpLightOff)


    ###########################################################
    def _submitTapo(self, op, coalesceKey=None, blocking=True):
        """Send one camera-control operation, off the UI thread.

        @param  op           A TapoControl kOp* constant.
        @param  coalesceKey  Passed through to TapoController.submit().
        @param  blocking     True for something the user asked for, which grey
                             out both buttons until it lands.  False for
                             background work like the spotlight state query,
                             which must neither disable the buttons nor be
                             skipped because a command is in flight.  Such work
                             is also marked speculative, so an unreachable
                             camera costs the connect timeout once rather than
                             on every selection.
        """
        if self._tapoTarget is None:
            return
        if blocking and self._tapoBusy:
            return

        location = self._curLocation
        target = self._tapoTarget

        def _done(ok, value):
            # Called on the controller's worker thread; hop to the UI thread,
            # where the panel may well be gone by now (see _onTapoResult).
            wx.CallAfter(self._onTapoResult, location, op, ok, value)

        if blocking:
            self._tapoBusy = True
            self._updateTapoUi()
        TapoController.instance(self._logger).submit(
            target, op, _done, coalesceKey, speculative=not blocking)


    ###########################################################
    def _onTapoResult(self, location, op, ok, value):
        """Fold a finished camera-control operation back into the UI.

        @param  location  The camera the operation was for.
        @param  op        The kOp* that ran.
        @param  ok        True if the camera did as it was told.
        @param  value     The result, or a message fit to show the user.
        """
        # The worker thread outlives this panel, so a reply can arrive after
        # we're destroyed -- same guard OnDateTimer needs.
        if not self:
            return

        isQuery = (op == kOpLightState)
        if not isQuery:
            self._tapoBusy = False

        if ok:
            if op == kOpSirenOn:
                self._sirenOn[location] = True
            elif op == kOpSirenOff:
                self._sirenOn[location] = False
            elif op == kOpLightOn:
                self._lightOn[location] = True
            elif op == kOpLightOff:
                self._lightOn[location] = False
            elif isQuery:
                self._lightOn[location] = bool(value)
        elif isQuery:
            # We asked and got nothing back, so stop claiming to know: fall
            # back to off rather than keep showing a stale answer.  A failed
            # COMMAND needs no such handling -- we only ever record the state
            # the camera confirmed.
            self._lightOn.pop(location, None)

        self._updateTapoUi()

        # A failed query isn't worth a dialog; the user finds out when they
        # press the button.
        if ok or isQuery:
            return

        if isAuthError(value):
            message = _kAuthFailMsg % (location, value)
        else:
            message = "%s could not be controlled.\n\n%s" % (location, value)

        wx.MessageBox(message, _kControlFailTitle, wx.OK | wx.ICON_ERROR,
                      self.GetTopLevelParent())


    ###########################################################
    def _updateTapoUi(self):
        """Bring the siren and light buttons in line with what we know."""
        usable = (self._tapoTarget is not None) and not self._tapoBusy
        self._sirenButton.Enable(usable)
        self._lightButton.Enable(usable)

        sirenOn = self._sirenOn.get(self._curLocation, False)
        lightOn = self._lightOn.get(self._curLocation, False)
        self._setTapoBitmaps(self._sirenButton, self._sirenBmps, sirenOn)
        self._setTapoBitmaps(self._lightButton, self._lightBmps, lightOn)

        if self._tapoTarget is None:
            tip = _kNoCameraTip if not self._curLocation else _kNotTapoTip
            self._sirenButton.SetToolTip(tip)
            self._lightButton.SetToolTip(tip)
        else:
            self._sirenButton.SetToolTip(_kSirenOnTip if sirenOn
                                         else _kSirenOffTip)
            self._lightButton.SetToolTip(_kLightOnTip if lightOn
                                         else _kLightOffTip)


    ###########################################################
    def _setTapoBitmaps(self, button, bmps, isOn):
        """Show one of the camera-control buttons in its on or off artwork.

        Only the three drawn states are swapped, not the disabled one: a
        disabled button should read as off whatever we last asked the camera
        for.

        NOTE: HoverBitmapButton builds its hit-test mask once, from the hovered
        bitmap it was constructed with, and does not rebuild it here.  The on
        and off art differ only in colour -- their alpha silhouettes are
        pixel-identical -- so the mask stays correct.  Art whose outline
        changes between states would need the mask rebuilt too.

        @param  button  The HoverBitmapButton to update.
        @param  bmps    That button's {isOn: (normal, pressed, hovered)} map.
        @param  isOn    True to show the "on" artwork.
        """
        normal, pressed, hovered = bmps[bool(isOn)]
        button.SetBitmap(normal)
        button.SetBitmapPressed(pressed)
        button.SetBitmapCurrent(hovered)


    ###########################################################
    def updateRulesCache(self, location=None):
        """Update the rules cache.

        @param  location  The name of the location to update, none for all.
        """
        if not location:
            self._rulesCache = {}
            locations = self._backEndClient.getCameraLocations()
        else:
            locations = [location]

        for loc in locations:
            self._rulesCache[loc] = \
                    self._backEndClient.getRuleInfoForLocation(loc)


    ###########################################################
    def getRulesForLocation(self, location):
        """Return the rules for a given location.

        @param  location  The name of the location to retrieve rules for.
        @return rules     A list of rules for location or []
        """
        return self._rulesCache.get(location, [])

