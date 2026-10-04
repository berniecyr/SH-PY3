#!/usr/bin/env python

#*****************************************************************************
#
# OptionsDialog.py
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
import json, os, time, pickle, sys, threading, socket, random
from socketserver import TCPServer, BaseRequestHandler

# Common 3rd-party imports...
import wx
import wx.adv     # HyperlinkCtrl; this module relied on someone else importing it

# Local imports...
from appCommon.CommonStrings import kVideoFolder
from appCommon.CommonStrings import kWebDirName
from appCommon.CommonStrings import kWebDirEnvVar
from appCommon.CommonStrings import kStatusFile
from appCommon.CommonStrings import kStatusKeyVerified
from appCommon.CommonStrings import kStatusKeyNumber
from appCommon.CommonStrings import kStatusKeyPort
from appCommon.CommonStrings import kStatusKeyPortOpenerState
from appCommon.CommonStrings import kStatusKeyRemotePort
from appCommon.CommonStrings import kStatusKeyRemoteAddress
from appCommon.CommonStrings import kStatusKeyCertificateId
from appCommon.CommonStrings import kStorageDetailUrl
from appCommon.CommonStrings import kRemoteAccessUrl
from appCommon.LicenseUtils import hasPaidEdition
from .LocateVideoDialog import LocateVideoDialog
from .MoveVideoDialog import MoveVideoDialog
from vitaToolbox.wx.TextSizeUtils import makeFontDefault

from launch.Launch import serviceAvailable

from .FrontEndUtils import setServiceStartsBackend, getServiceStartsBackend
from .FrontEndUtils import setServiceAutoStart, getServiceAutoStart
from .FrontEndUtils import getUserLocalDataDir
from .FrontEndUtils import promptUserIfAutoStartEvtHandler
from .FrontEndUtils import determineGridViewCameras
from .FrontEndPrefs import getFrontEndPref, setFrontEndPref
from .FrontEndPrefs import getTapoCredentials
import backEnd.BackEndPrefs as Prefs
import backEnd.ImageCheckConfig as AICfg
import backEnd.IHostConfig as IHostCfg
import backEnd.TapoConfig as TapoCfg

# vitaToolbox imports...
from vitaToolbox.path.GetDiskSpaceAvailable import getDiskSpaceAvailable
from vitaToolbox.path.VolumeUtils import getVolumeNameAndType
from vitaToolbox.path.VolumeUtils import getStorageSizeStr
from vitaToolbox.wx.AppColors import systemBackgroundColour
from vitaToolbox.wx.TextCtrlUtils import setHyperlinkColors, CharValidator
from vitaToolbox.strUtils.EnsureUnicode import ensureUtf8, ensureUnicode
from vitaToolbox.networking.TapoControl import TapoController
from vitaToolbox.networking.TapoControl import parseTapoTarget
from vitaToolbox.networking.TapoControl import isAuthError, kOpProbe
from vitaToolbox.networking.TapoControl import tapoHostFromUri


# Globals...
_kPaddingSize = 4
_kBorderSize = 20
_kSpaceSize1 = 12
_kSpaceSize2 = 16

if wx.Platform == '__WXMSW__':
    _kDialogTitle = "Options"
else:
    _kDialogTitle = "Preferences"


_kVideoLocLabel = "Video is stored on %s (%s)"
_kDiskSpaceLabel = "Available disk space on %s (%s): %s"
_kStorageNote = (
"""Note: All video is saved for the duration specified above. Video\n"""
"""not matching "save" rules is then deleted and the resulting clips\n"""
"""are kept until the disk space quota is reached.\n\n"""
"""Temporary video is useful in the event something occurs that did\n"""
"""not match a rule but the video is still desired. Most users should\n"""
"""set the temporary storage as low as is acceptable and the disk \n"""
"""space allocation as high as possible."""
)

_kRemoteDescription = (
"""Remote access allows you to view clips and videos from the web \n"""
"""browser of any computer or mobile device. For access outside\n"""
"""your local network you will need to configure port forwarding on\n"""
"""your router."""
)

_kEnableRemoteLabel = "Enable remote access"
_kUsernameLabel = "Login user ID: "
_kPasswordLabel = "Login password: "
_kVerifyLabel = "Verify password: "
_kPortLabel = "Remote access port: "
_kPortOpenerEnabledLabel = "Open this port in my router"
_kFakePassword = "----------"
_kDefaultPort = 8848

_kMinPasswordLength = 6
_kMaxUserPassLength = 128
_kErrorTitle = "Error"
_kNonAsciiBody = "Username and password cannot contain international characters."
_kInvalidUsernameBody = "You must specify a username."
_kPasswordsMatchBody = "The password fields must match."
_kPasswordsLengthBody = "Your password must be at least %i characters." % \
        _kMinPasswordLength
_kPortErrorBody = "Port must be an integer between 1025 and 65535."
_kPortNABody = "Port %d is in use by a different application. Please choose another."

_kNotebookGeneral = "General";
_kNotebookStorage = "Storage";
_kNotebookRemoteAccess = "Remote Access";
_kNotebookGrid = "Grid";
_kNotebookAI = "AI Detection";
_kNotebookIHost = "iHost";

# Reference for the hub's LAN API -- the discovery, token and device calls on
# this tab all come from here.
_kIHostDocUrl = "https://ewelink.cc/ewelink-cube/introduce-open-api/document/"

# The hub only hands out a token after somebody presses Done on its own web
# console, and it holds that window open for about five minutes.
_kIHostTokenWaitSecs = 300
_kIHostTokenPollSecs = 2.0
_kNotebookSavedEvents = "Saved Events";
_kNotebookColors = "Colors";
_kNotebookTapo = "Tapo";

# The Tapo tab.  The wording matters here: everyone's first assumption is that
# the account already configured for the video stream should work, and it
# doesn't -- so say which account this is, up front.
_kTapoIntro = (
"""Sound the siren and switch the spotlight on Tapo cameras, using the Siren """
"""and Light buttons beside the camera name on the monitor screen.

Sign in with your TP-LINK ACCOUNT -- the email address and password you use """
"""for the Tapo app.  The camera account that streams your video is refused """
"""here; the cameras check these against your TP-Link account instead.""")

# wx.StaticText does not wrap on its own, and the paragraphs above are long
# enough to push the whole dialog wide, so they are wrapped explicitly.  This
# is roughly the width the other tabs' content settles at.
_kTapoTextWrap = 520

_kTapoStorageNote = (
    "Saved in this computer's preferences file for your Windows account.")

_kTapoNeedBoth = "Enter your TP-Link account and password first."
_kTapoNoCameras = "No camera with a network address to test against."
_kTapoTesting = "Contacting %s..."
_kTapoTestOk = "Success -- %s answered (%s)."
_kTapoTestAuthFailed = (
    "%s refused these credentials.  Use the email address and password you "
    "sign in to the Tapo app with, not the camera account your streams use.")
_kTapoTestFailed = "%s could not be reached: %s"

_kStorageSizeGBMin = 1
_kStorageSizeGBMax = 999999
_kCacheSizeMinutesMin = 1
_kCacheSizeMinutesMax = 9999


_kGridRowsLabel = "Rows: "
_kGridColsLabel = "Columns: "
_kGridOrderLabel = "Order:"
_kGridFpsLabel = "Framerate: "
_kGridMoveUp = "Move up"
_kGridMoveDown = "Move Down"

kWebServerStatusLabel = "Server status: "
kWebServerStatusNA = "Unknown"
kWebServerStatusOff = "Off"
kWebServerStatusNotVerified = "Starting..."
kWebServerStatusOn = "Running"
kWebServerStatusUpdating = "Updating..."

kWebServerLocURL = "Local address: "
kWebServerIntURL = "Internal address: "
kWebServerExtURL = "External address: "
kWebServerExtStatusOff = "Unable to automatically verify"
kWebServerExtStatusNA = "Unable to automatically verify"
kWebServerCertificateId = "SSL Fingerprint: "

_kMbps = 1024*1024
_kBitrates =      [-1, .5*_kMbps, _kMbps,   2*_kMbps, 3*_kMbps, 4*_kMbps, 5*_kMbps]
_kBitrateLabels = ["No limit", ".5 Mbps", "1 Mbps", "2 Mbps", "3 Mbps", "4 Mbps", "5 Mbps"]
_kVideoQualityProfile = [ 0, 10, 20, 30 ]
_kVideoQualityProfileLabels = [ "Original", "High", "Medium", "Low" ]
_kVideoResolutions = [ -1, 1080, 720, 480, 240 ]
_kVideoResolutionLabels = [ "Original", "1080p", "720p", "480p", "240p" ]

# Interval for web server status polling, in milliseconds.
kWebServerStatusRefresh = 1000

_kLaunchOnStartupLabel = "Run Sighthound Video at system startup"
_kRunAsServiceLabel = "Run the back end as a Windows service"
_kRunAsServiceTip = (
    "When enabled, the SHLaunchPY3 service owns the back end: cameras keep "
    "recording and rules keep firing after you close this window, and the back "
    "end comes back by itself if it stops unexpectedly.\n\n"
    "When disabled, this application starts the back end itself and closing "
    "the application stops recording."
)
_kServiceChangedTitle = "Restart needed"
_kServiceChangedBody = (
    "The SHLaunchPY3 service will %s own the back end.\n\n"
    "This takes effect the next time the back end starts. Restart Sighthound "
    "Video (or the service) to apply it now."
)
_kServiceNotInstalledHint = (
    "The SHLaunchPY3 service is not installed, so these two options do "
    "nothing yet. It is installed by the Sighthound Video Py3 installer; "
    "this copy is running from a source checkout. See README.md, "
    "\"Running the back end as a service\"."
)
_kIsWin = wx.Platform == '__WXMSW__'

# To generate the remote access URLs
kHttpScheme = "https"
kHttpAddress = kHttpScheme + "://%s:%d"
kHttpLocalHost = "127.0.0.1"

_kGridShowInactiveCameras = "Show inactive cameras"
_kGridMoveInactiveCameras = "Move inactive cameras to the end of the list"


###############################################################
class GetInternalIPThread(threading.Thread):
    """ Tries to the determine the most likely internal/intranet address of the
    the machine. If we're connected directly to the Internet (no NAT) then this
    would be not very internal of course.
    """
    def __init__(self, logger):
        threading.Thread.__init__(self)
        self._logger = logger
        # the IPv4 address, or None if we haven't gotten anything (yet)
        self.result = None
    def run(self):
        attempts = 3   # retry, in case some UDP ports are problematic(?)
        while 0 < attempts:
            attempts -= 1
            port = 50000 + random.randrange(0,9999)
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setblocking(0)
                s.settimeout(0.5)
                # the target IP is the DNS server of Google, yet no traffic is
                # ever going to hit it since we won't send a single packet...
                s.connect(('8.8.8.8', port))
                self.result = s.getsockname()[0]
                return
            except:
                self._logger.warn("port %d failed (%s)" %
                                  (port, sys.exc_info()[1]))
                continue
            finally:
                if s is not None:
                    try: s.close()
                    except: pass

###############################################################

# Custom event mechanism to transport the web server status from the gathering
# thread to the options dialog ...
myEVT_WEBSERVERSTATUS = wx.NewEventType()
EVT_WEBSERVERSTATUS = wx.PyEventBinder(myEVT_WEBSERVERSTATUS, 1)
class WebServerStatusEvent(wx.CommandEvent):
    """ Carries the web server status from the polling thread to the dialog.

    The Clone() override below is load-bearing, NOT boilerplate: wx.PostEvent
    clones the event, and a plain wx.CommandEvent subclass whose Clone() is
    missing gets cloned by the base class instead, arriving at the handler as a
    bare CommandEvent with every Python attribute gone.  That is exactly how
    ExportProgressDialog's progress event used to fail ("'CommandEvent' object
    has no attribute 'percentage'").  This one works because Clone() rebuilds
    the subclass -- verified across threads on wxPython 4.2.5 / wxWidgets
    3.2.9.  Don't delete it; if it ever needs to go, switch the base class to
    wx.PyCommandEvent (which does the same thing automatically) or hand the
    status over with wx.CallAfter, as the export dialog now does.
    """
    def __init__(self, status):
        wx.CommandEvent.__init__(self, myEVT_WEBSERVERSTATUS, -1)
        self._status = status
    def GetStatus(self):
        """ Returns the web server status, or None if the status file could not
        be opened (or does not exist yet). """
        return self._status
    def Clone(self):
        return WebServerStatusEvent(self._status)

# TODO: could be done via wx workers instead, given that they have the same
#       possibilities for early termination/interruption ...

class WebServerStatusThread(threading.Thread):
    """ Background thread trying to read the web server status with a certain
    delay of in between each attempt.
    @param dialog The associated dialog or parent frame respectively.
    @param pollSecs The interval delay in seconds.
    """
    def __init__(self, dialog, pollIntvl=1):
        threading.Thread.__init__(self)
        self._dialog = dialog
        self._pollIntvl = pollIntvl
        self._evt = threading.Event()

    def stop(self):
        """ Signals the thread to stop and wait for such to happen. """
        self._evt.set()
        self.join()

    def run(self):
        """ Runs the main thread loop, getting the status now and then. """
        while not self._evt.isSet():
            if not self.runSync():
                break
            self._evt.wait(self._pollIntvl)

    def runSync(self):
        """ Runs the blocking portion of the thread. And also the part where we
        send events to the parent. This is useful e.g. for some initial
        determination of the status.
        @return False if a thread stop got detected. """
        s = self._readWebServerStatusFile()
        if self._evt.isSet():
            return False
        wx.PostEvent(self._dialog, WebServerStatusEvent(s))
        return True

    def _readWebServerStatusFile(self, timeout=.5):
        """ To read the web server status from the file it emits on start and
        every time here is a change. Since it might be written/renamed at the
        same time we do our readout there might be multiple attempts needed to
        be successful.
        @param timeout Number of seconds to wait until giving up.
        @return The web server status information as a dictionary or None if
        the status file didn't appear, or its information couldn't be parsed
        or if we the thread is going down.
        """
        webDir = os.environ.get(kWebDirEnvVar)
        if not webDir:
            userLocalDataDir = getUserLocalDataDir()
            webDir = os.path.join(userLocalDataDir, kWebDirName)
        statusFile = os.path.join(webDir, kStatusFile)
        end = time.time() + timeout
        while not self._evt.isSet():
            h = None
            try:
                h = open(statusFile, "rb")
                return pickle.load(h)
            except:
                if time.time() > end or self._evt.isSet():
                    return None
                time.sleep(.1)
            finally:
                if h is not None:
                    try: h.close()
                    except: pass
        return None

###############################################################
class OptionsDialog(wx.Dialog):
    """A dialog for configuring app settings."""
    ###########################################################
    def __init__(self, parent, backEndClient, dataManager, logger,
            uiPrefsModel):
        """Initializer for OptionsDialog.

        @param  parent         The parent window.
        @param  backEndClient  An object for communicating with the back end.
        @param  dataManager    An interface to the database and video files.
        @param  logger         The caller's log instance to share.
        @param  uiPrefsModel   The caller's log instance to share.
        """
        wx.Dialog.__init__(self, parent, -1, _kDialogTitle, size=(400, -1))

        try:
            self._backEndClient = backEndClient
            self._dataMgr = dataManager
            self._logger = logger
            self._uiPrefsModel = uiPrefsModel

            self._hwDevices = self._backEndClient.getHardwareDevicesList()
            self._hwDevice = self._backEndClient.getHardwareDevice()

            try:
                self._hasPaid = hasPaidEdition(backEndClient.getLicenseData())
            except Exception as e:
                self._logger.error(str(e))
                self._hasPaid = False

            # The LAN record viewer (Remote Access tab) is a free, self-hosted
            # feature in this build -- it is not the old cloud/NAT-traversal
            # product -- so its panel and settings are always available,
            # independent of the paid-edition gate used elsewhere.
            self._webAllowed = True

            # try to get the internal IP address as quickly as possible...
            self._internalIP = GetInternalIPThread(self._logger)
            self._internalIP.start()

            self._origStorageSize = \
                self._backEndClient.getMaxStorageSize()
            self._origCacheSize = self._backEndClient.getCacheDuration()

            # Create the top sizer.
            mainSizer = wx.BoxSizer(wx.VERTICAL)
            self.SetSizer(mainSizer)
            self._notebook = wx.Notebook(self, -1)

            defaultPanel = wx.Panel(self._notebook, -1)

            sizer = wx.BoxSizer(wx.VERTICAL)

            # Create the options controls.
            s = wx.FlexGridSizer(0, 2, _kPaddingSize, _kPaddingSize)

            storageLabel = wx.StaticText(
                defaultPanel, -1, "For clips and temporary video, use up to:"
            )
            self._storageSizeCtrl = wx.TextCtrl(defaultPanel, -1, "", size=(80,-1), validator=CharValidator(CharValidator.kAllowDigits))
            self._storageSizeCtrl.SetMaxLength(len(str(_kStorageSizeGBMax)))
            self._storageSizeCtrl.SetValue(str(self._origStorageSize))
            storageSizer = wx.BoxSizer(wx.HORIZONTAL)
            storageSizer.Add(self._storageSizeCtrl, 0, wx.EXPAND)
            storageSizer.Add(wx.StaticText(defaultPanel, -1, "GB"), 0, wx.EXPAND | wx.ALL, _kPaddingSize)

            cacheLabel = wx.StaticText(defaultPanel, -1,
                                       "Temporarily keep all video for up to:")
            self._cacheSizeCtrl= wx.TextCtrl(defaultPanel, -1, "", size=(64,-1), validator=CharValidator(CharValidator.kAllowDigits))
            self._cacheSizeCtrl.SetValue(str(self._origCacheSize))
            self._cacheSizeCtrl.SetMaxLength(len(str(_kCacheSizeMinutesMax)))
            cacheSizer= wx.BoxSizer(wx.HORIZONTAL)
            cacheSizer.Add(self._cacheSizeCtrl, 0, wx.ALIGN_CENTER_VERTICAL)
            cacheSizer.Add(wx.StaticText(defaultPanel, -1, "Hours"), 0,
                           wx.ALL | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)

            videoLocation = self._backEndClient.getVideoLocation()
            volumeName = "Unknown"
            volumeType = "Unknown"
            bytesFree = -1
            try:
                volumeName, volumeType = getVolumeNameAndType(videoLocation)
                bytesFree = getDiskSpaceAvailable(videoLocation)
            except Exception:
                import traceback
                self._logger.warning("Except: " + traceback.format_exc())
                pass

            self._locLabel = wx.StaticText(defaultPanel, -1,
                                            ensureUnicode(_kVideoLocLabel
                                                        % (volumeType, volumeName)))
            locButton = wx.Button(defaultPanel, -1, "Move video...")
            locButton.Bind(wx.EVT_BUTTON, self.OnMoveVideo)

            sizeStr = getStorageSizeStr(bytesFree)
            self._spaceFree = wx.StaticText(defaultPanel, -1,
                                            ensureUnicode(_kDiskSpaceLabel %
                                                    (volumeType, volumeName, sizeStr)))

            storageNotice = wx.StaticText(defaultPanel, -1, _kStorageNote)
            storageNotice2 = wx.StaticText(defaultPanel, -1, "For further details click ")
            storageLink = wx.adv.HyperlinkCtrl(defaultPanel, wx.ID_ANY, "here", kStorageDetailUrl)
            setHyperlinkColors(storageLink)
            storageNotice3 = wx.StaticText(defaultPanel, -1, ".")

            s.AddMany([(storageLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                        _kPaddingSize),
                       (storageSizer, 1, wx.EXPAND),
                       (cacheLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                        _kPaddingSize),
                       (cacheSizer, 1, wx.EXPAND),
                       (self._locLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT |
                        wx.RIGHT, _kPaddingSize),
                       (locButton, 0, wx.LEFT | _kPaddingSize),
                       ])

            sizer.AddSpacer(_kSpaceSize1)
            sizer.Add(s, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kSpaceSize1)
            sizer.Add(self._spaceFree, 0, wx.LEFT | wx.RIGHT, _kBorderSize)

            sizer.AddSpacer(_kSpaceSize1)
            sizer.AddStretchSpacer(1)
            sizer.Add(storageNotice, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
            sizer.AddStretchSpacer(1)
            sizer.AddSpacer(_kSpaceSize1)

            helpSizer = wx.BoxSizer(wx.HORIZONTAL)
            helpSizer.Add(storageNotice2)
            helpSizer.Add(storageLink)
            helpSizer.Add(storageNotice3)
            sizer.Add(helpSizer, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
            sizer.AddSpacer(_kSpaceSize2)

            defaultPanel.SetSizer(sizer)

            generalPanel = self._createGeneralPanel()
            gridPanel = self._createGridPanel()
            aiPanel = self._createAIDetectionPanel()
            ihostPanel = self._createIHostPanel()
            tapoPanel = self._createTapoPanel()
            savedEventsPanel = self._createSavedEventsPanel()
            colorsPanel = self._createColorsPanel()
            self._notebook.AddPage(generalPanel, _kNotebookGeneral)
            self._notebook.AddPage(defaultPanel, _kNotebookStorage)
            self._notebook.AddPage(gridPanel, _kNotebookGrid)
            self._notebook.AddPage(aiPanel, _kNotebookAI)
            self._notebook.AddPage(ihostPanel, _kNotebookIHost)
            self._notebook.AddPage(tapoPanel, _kNotebookTapo)
            self._notebook.AddPage(savedEventsPanel, _kNotebookSavedEvents)
            self._notebook.AddPage(colorsPanel, _kNotebookColors)
            if self._webAllowed:
                remotePanel = self._createRemoteAccessPanel()
                self._notebook.AddPage(remotePanel, _kNotebookRemoteAccess)

            mainSizer.Add(self._notebook, 1,
                    wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _kSpaceSize2)

            # Add the ok button.
            self._okButton = wx.Button(self, -1, "OK")
            self._okButton.Bind(wx.EVT_BUTTON, self.OnOk)
            okSizer = wx.BoxSizer(wx.HORIZONTAL)
            okSizer.AddStretchSpacer(1)
            okSizer.Add(self._okButton, 0, wx.EXPAND)
            mainSizer.Add(okSizer, 0, wx.EXPAND | wx.ALL, _kSpaceSize2)

            self.SetEscapeId(wx.ID_CANCEL)
            wx.Button(self, wx.ID_CANCEL, "", size=(0, 0), style=wx.NO_BORDER)

            self.Fit()
            self.CenterOnParent()


        except: # All exceptions, not just Exception subclasses
            # Make absolutely sure that we are destroyed, even if we crash
            # in the above...
            self.Destroy()
            raise

    ###########################################################
    def _createGeneralPanel(self):
        """ Create the general panel (time settings etc).

        @return  New panel instance.
        """

        panel = wx.Panel(self._notebook, -1)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._clipMergeLimit = self._backEndClient.getClipMergeThreshold()

        self._wasAutoStart = getServiceAutoStart()

        hasHwControls = len(self._hwDevices)>0
        hasHwChoice = len(self._hwDevices)>1
        hwEnabled = self._hwDevice != "none"

        if hasHwControls:
            self._enableHardwareAcceleration = wx.CheckBox(panel, -1, "Enable hardware-accelerated decoder")
            self._enableHardwareAcceleration.SetValue(hwEnabled)
        else:
            self._enableHardwareAcceleration = None

        if hasHwChoice:
            devList = ["auto"] + self._hwDevices
            self._hwDevicesChoice = wx.Choice(panel, -1, choices=devList)
            if self._hwDevice in self._hwDevices:
                self._hwDevicesChoice.SetSelection(self._hwDevicesChoice.FindString(self._hwDevice))
            else:
                self._hwDevicesChoice.SetSelection(0)
            self._hwDevicesChoice.Enable(hwEnabled)
        else:
            self._hwDevicesChoice = None

        self._launchOnStartup = wx.CheckBox(panel, -1, _kLaunchOnStartupLabel)
        self._launchOnStartup.SetValue(self._wasAutoStart)

        # Back end as a Windows service (SHLaunchPY3). Both this and the
        # autostart box above only mean anything once the service is installed,
        # so when it isn't we disable them and say why rather than letting the
        # user set something that silently does nothing.
        self._serviceInstalled = serviceAvailable()
        self._wasServiceStartsBackend = (getServiceStartsBackend()
                                         if self._serviceInstalled else False)
        self._runBackendAsService = wx.CheckBox(panel, -1,
                                               _kRunAsServiceLabel)
        self._runBackendAsService.SetValue(self._wasServiceStartsBackend)
        self._runBackendAsService.SetToolTip(_kRunAsServiceTip)

        if not self._serviceInstalled:
            self._runBackendAsService.Enable(False)
            self._launchOnStartup.Enable(False)
        self._serviceHintLabel = wx.StaticText(
            panel, -1,
            "" if self._serviceInstalled else _kServiceNotInstalledHint)
        makeFontDefault(self._serviceHintLabel)
        self._serviceHintLabel.SetForegroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        self._combineCheckbox = wx.CheckBox(panel, -1, label="Join clips that are within ")
        self._combineCheckbox.SetValue( self._clipMergeLimit > 0 )
        kMaxClipPaddingSec = 15 # we allow maximum of 15s distance for clips to be bridged
        self._combineTimeLimit = wx.Choice(panel, -1, choices=[str(x) for x in range(0,kMaxClipPaddingSec+1)])
        self._combineTimeLimit.SetSelection(self._clipMergeLimit)
        self._combineTimeLimit.Enable( self._clipMergeLimit > 0 )
        unit = wx.StaticText(panel, -1, " seconds of each other")


        sizer.AddStretchSpacer(1)

        combineClipsSizer = wx.BoxSizer(wx.HORIZONTAL)
        combineClipsSizer.Add(self._combineCheckbox, 0, wx.LEFT)
        combineClipsSizer.Add(self._combineTimeLimit, 0, wx.LEFT)
        combineClipsSizer.Add(unit, 0, wx.LEFT)
        sizer.AddSpacer(_kSpaceSize1)
        sizer.Add(combineClipsSizer, 0, wx.LEFT | wx.RIGHT, _kBorderSize)


        if hasHwControls:
            hwControlSizer = wx.BoxSizer(wx.HORIZONTAL)
            hwControlSizer.Add(self._enableHardwareAcceleration, 0, wx.LEFT)
            if hasHwChoice:
                hwControlSizer.Add(self._hwDevicesChoice, 0, wx.LEFT)
            sizer.AddSpacer(_kSpaceSize1)
            sizer.Add(hwControlSizer, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
            sizer.AddSpacer(_kSpaceSize1)


        sizer.Add(self._runBackendAsService, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)
        sizer.Add(self._launchOnStartup, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)
        sizer.Add(self._serviceHintLabel, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        panel.SetSizer(sizer)

        self.Bind(wx.EVT_CHECKBOX, promptUserIfAutoStartEvtHandler, self._launchOnStartup)
        self._combineCheckbox.Bind(wx.EVT_CHECKBOX, self.OnCombineClips)
        if hasHwChoice:
            self._enableHardwareAcceleration.Bind(wx.EVT_CHECKBOX, self.OnHwAccelerationChange)


        return panel

    ##########################################################
    def _createGridPanel(self):
        """ Create the panel where the grid view can be configured.

        @return  New panel instance.
        """

        panel = wx.Panel(self._notebook, -1)

        # The main sizer to contain it all.
        mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.AddSpacer(_kSpaceSize1)

        # Grid sizer for most of the layout stuff here.
        flexGridSizer = wx.FlexGridSizer(0, 2, _kPaddingSize, _kPaddingSize)

        # Add the dimension (rows/columns) controls.
        rowsLabel = wx.StaticText(panel, -1, _kGridRowsLabel)
        colsLabel = wx.StaticText(panel, -1, _kGridColsLabel)
        orderLabel = wx.StaticText(panel, -1, _kGridOrderLabel)
        choices = list(map(lambda i: str(i), range(1, 10)))
        style = wx.CB_DROPDOWN + wx.CB_READONLY
        cols = str(getFrontEndPref("gridViewCols"))
        rows = str(getFrontEndPref("gridViewRows"))
        self._gridColsComboBox = wx.ComboBox(panel, value=cols, choices=choices, style=style)
        self._gridRowsComboBox = wx.ComboBox(panel, value=rows, choices=choices, style=style)
        flexGridSizer.AddMany([
            (colsLabel, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize),
            (self._gridColsComboBox, 1),
            (rowsLabel, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize),
            (self._gridRowsComboBox, 1)])

        # Get the camera names and overlay them with former order preferences.
        cameras = self._backEndClient.getCameraLocations()
        order = getFrontEndPref("gridViewOrder")
        choices = determineGridViewCameras(cameras, order)

        # Create a list box to show these camera names.
        self._gridOrder = wx.ListBox(panel, choices=choices)
        # Need to spy on all mouse events because we don't get an EVT_LISTBOX
        # when an item gets deselected.
        self._gridOrder.Bind(wx.EVT_MOUSE_EVENTS, self.OnGridOrderChanged)
        self._gridOrder.Bind(wx.EVT_LISTBOX, self.OnGridOrderChanged)
        # TODO: need to make it expand in a cooperative way, somehow ...
        self._gridOrder.SetMinSize((320, -1))

        # Add buttons to move camera names up or down.
        self._gridMoveUpButton = wx.Button(panel, -1, _kGridMoveUp)
        self._gridMoveUpButton.Bind(wx.EVT_BUTTON, self.OnGridMoveUp)
        self._gridMoveDownButton = wx.Button(panel, -1, _kGridMoveDown)
        self._gridMoveDownButton.Bind(wx.EVT_BUTTON, self.OnGridMoveDown)
        moveSizer = wx.BoxSizer(wx.HORIZONTAL)
        moveSizer.Add(self._gridMoveUpButton)
        moveSizer.AddSpacer(_kPaddingSize)
        moveSizer.Add(self._gridMoveDownButton)

        # Put both the camera list and the move buttons into a separate sizer.
        gridOrderSizer = wx.BoxSizer(wx.VERTICAL)
        gridOrderSizer.Add(self._gridOrder)
        gridOrderSizer.AddSpacer(_kPaddingSize)
        gridOrderSizer.Add(moveSizer)

        # Add all things camera ordering to the grid.
        flexGridSizer.AddSpacer(_kPaddingSize)
        flexGridSizer.AddSpacer(_kPaddingSize)
        flexGridSizer.AddMany([
            (orderLabel, 0, wx.LEFT | wx.EXPAND, _kPaddingSize),
            (gridOrderSizer, 1)])
        flexGridSizer.AddSpacer(_kPaddingSize)
        flexGridSizer.AddSpacer(_kPaddingSize)

        # Create the framerate controls and add it to the grid.
        fpsValue = getFrontEndPref("gridViewFps")
        fpsLabel = wx.StaticText(panel, -1, _kGridFpsLabel)
        choices = list(map(lambda i: str(i), (2, 5, 10, 30)))
        self._gridFpsComboBox = wx.ComboBox(panel, value=str(fpsValue), choices=choices, style=style)
        flexGridSizer.AddMany([
            (fpsLabel, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize),
            (self._gridFpsComboBox, 1)])

        # Add the grid to the main sizer.
        mainSizer.Add(flexGridSizer, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, _kSpaceSize2)
        panel.SetSizer(mainSizer)

        # Add the bitmap flag on the bottom, since it won't fit the grid style.
        showInactiveMode = getFrontEndPref("gridViewShowInactive")
        self._gridShowInactiveCheckbox = wx.CheckBox(panel, label=_kGridShowInactiveCameras)
        self._gridShowInactiveCheckbox.SetValue(showInactiveMode>0)
        self._gridShowInactiveCheckbox.Bind(wx.EVT_CHECKBOX, self.OnShowInactiveCameras)
        self._gridMoveInactiveCheckbox = wx.CheckBox(panel, label=_kGridMoveInactiveCameras)
        self._gridMoveInactiveCheckbox.SetValue(showInactiveMode>1)
        self._gridMoveInactiveCheckbox.Enable(showInactiveMode>0)
        mainSizer.AddStretchSpacer()
        mainSizer.Add(self._gridShowInactiveCheckbox, 0, wx.LEFT, _kSpaceSize2)
        mainSizer.Add(self._gridMoveInactiveCheckbox, 0, wx.LEFT, _kSpaceSize2)
        mainSizer.AddSpacer(_kSpaceSize1)

        # Make sure that the move buttons for ordering cameras not enabled yet.
        self.OnGridOrderChanged()

        return panel

    ###########################################################
    def OnShowInactiveCameras(self, event=None):
        self._gridMoveInactiveCheckbox.Enable(self._gridShowInactiveCheckbox.GetValue()>0)

    ###########################################################
    def _createRemoteAccessPanel(self):
        """ Create the remote access panel. Has its own [Apply] button where
        things can be activated without the options panel being closed.

        @return  New panel instance.
        """

        self._origWebUser = self._backEndClient.getWebUser()
        self._origPassword = _kFakePassword
        origWebPort = self._backEndClient.getWebPort()
        self._origWebEnabled = origWebPort > 0
        self._origPortOpenerEnabled = self._backEndClient.isPortOpenerEnabled()

        remotePanel = wx.Panel(self._notebook, -1)
        remoteSizer = wx.BoxSizer(wx.VERTICAL)

        remoteDescriptionLabel = wx.StaticText(remotePanel, -1,
                _kRemoteDescription)
        self._webEnableCheckbox = wx.CheckBox(remotePanel, -1,
                _kEnableRemoteLabel)
        self._webEnableCheckbox.SetValue(self._origWebEnabled)

        advancedButton = wx.Button(remotePanel, -1, "Advanced")
        advancedButton.Bind(wx.EVT_BUTTON, self.OnAdvanced)

        remoteSizer.AddSpacer(_kSpaceSize1)
        remoteSizer.Add(remoteDescriptionLabel, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        remoteSizer.AddSpacer(_kSpaceSize1)

        hSizer = wx.BoxSizer(wx.HORIZONTAL)
        hSizer.Add(self._webEnableCheckbox, 0, wx.ALIGN_CENTER_VERTICAL)
        hSizer.AddStretchSpacer(1)
        hSizer.Add(advancedButton, 0, wx.ALIGN_CENTER_VERTICAL)
        remoteSizer.Add(hSizer, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, _kBorderSize)

        usernameLabel = wx.StaticText(remotePanel, -1, _kUsernameLabel)
        self._userField = wx.TextCtrl(remotePanel, -1)
        self._userField.SetMaxLength(_kMaxUserPassLength)
        passwordLabel = wx.StaticText(remotePanel, -1, _kPasswordLabel)
        self._passField = wx.TextCtrl(remotePanel, -1, style=wx.TE_PASSWORD)
        self._passField.SetMaxLength(_kMaxUserPassLength)
        verifyLabel = wx.StaticText(remotePanel, -1, _kVerifyLabel)
        self._verifyField = wx.TextCtrl(remotePanel, -1,
                style=wx.TE_PASSWORD)
        self._verifyField.SetMaxLength(_kMaxUserPassLength)
        portLabel = wx.StaticText(remotePanel, -1, _kPortLabel)
        self._portCtrl = wx.TextCtrl(remotePanel, -1)
        if self._origWebEnabled:
            self._portCtrl.SetValue(str(origWebPort))
            self._userField.SetValue(self._origWebUser)
            if self._origWebUser:
                self._passField.SetValue(_kFakePassword)
                self._verifyField.SetValue(_kFakePassword)
        else:
            self._portCtrl.SetValue(str(_kDefaultPort))
        self._portOpenerEnabled = wx.CheckBox(remotePanel, -1,
                _kPortOpenerEnabledLabel)
        self._portOpenerEnabled.SetValue(self._origPortOpenerEnabled)

        portSizer = wx.BoxSizer(wx.HORIZONTAL)
        height = self._passField.GetSize()[1]
        width = self._portCtrl.GetTextExtent("00000")[0] + height
        self._portCtrl.SetMinSize((width, height))
        self._portCtrl.SetMaxSize((width, height))
        portSizer.Add(self._portCtrl)
        portSizer.Add(self._portOpenerEnabled, 0,  wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)

        webServerStatusLabel = wx.StaticText(remotePanel, -1, kWebServerStatusLabel)
        self._webServerStatusNotice = wx.StaticText(remotePanel, -1, "")
        webServerLocalNotice = wx.StaticText(remotePanel, -1, kWebServerLocURL)
        self._webServerLocalLink = wx.adv.HyperlinkCtrl(remotePanel, wx.ID_ANY, ".", ".")
        webServerInternalNotice = wx.StaticText(remotePanel, -1, kWebServerIntURL)
        self._webServerInternalLink = wx.adv.HyperlinkCtrl(remotePanel, wx.ID_ANY, ".", ".")
        webServerExternalNotice = wx.StaticText(remotePanel, -1, kWebServerExtURL)
        self._webServerExternalLink = wx.adv.HyperlinkCtrl(remotePanel, wx.ID_ANY, ".", ".")
        webServerCertificateId = wx.StaticText(remotePanel, -1, kWebServerCertificateId)
        self._webServerCertificateId = wx.StaticText(remotePanel, -1, "", )
        setHyperlinkColors(self._webServerLocalLink)
        setHyperlinkColors(self._webServerInternalLink)
        setHyperlinkColors(self._webServerExternalLink)

        # The hyperlink left align flag also adds a depth border on win so
        # we must unfortunately add an extra set of sizers and spacers, and
        # update the layout periodically ourselves.
        self._localSizer = wx.BoxSizer(wx.HORIZONTAL)
        self._localSizer.Add(self._webServerLocalLink)
        self._localSizer.AddStretchSpacer(1)
        self._internalSizer = wx.BoxSizer(wx.HORIZONTAL)
        self._internalSizer.Add(self._webServerInternalLink)
        self._internalSizer.AddStretchSpacer(1)
        self._externalSizer = wx.BoxSizer(wx.HORIZONTAL)
        self._externalSizer.Add(self._webServerExternalLink)
        self._externalSizer.AddStretchSpacer(1)

        self._applyButton = wx.Button(remotePanel, -1, "Apply")
        self._applyButton.Bind(wx.EVT_BUTTON, self.OnApply)

        accountSizer = wx.FlexGridSizer(0, 2, _kPaddingSize, _kPaddingSize)
        accountSizer.AddGrowableCol(1, 1)
        accountSizer.AddMany([
                (usernameLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._userField, 0, wx.EXPAND),
                (passwordLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._passField, 0, wx.EXPAND),
                (verifyLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._verifyField, 0, wx.EXPAND),
                (portLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (portSizer, 0, 0),
            ])
        accountSizer.AddSpacer(8)
        accountSizer.AddSpacer(8)
        accountSizer.AddMany([
                (webServerStatusLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._webServerStatusNotice, 0, wx.EXPAND),
                (webServerLocalNotice, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._localSizer, 0, wx.EXPAND),
                (webServerInternalNotice, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._internalSizer, 0, wx.EXPAND),
                (webServerExternalNotice, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._externalSizer, 0, wx.EXPAND),
                (webServerCertificateId, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                    _kPaddingSize), (self._webServerCertificateId, 0, wx.EXPAND),
                ])
        accountSizer.AddSpacer(1)
        accountSizer.Add(self._applyButton, 0, wx.RIGHT | wx.ALIGN_RIGHT)

        remoteSizer.AddSpacer(_kSpaceSize1)
        remoteSizer.Add(accountSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
        remoteSizer.AddSpacer(_kSpaceSize1)
        remotePanel.SetSizer(remoteSizer)

        self._userField.Bind(wx.EVT_TEXT, self.OnRemoteUserChange)
        self._passField.Bind(wx.EVT_TEXT, self.OnRemoteItemChange)
        self._verifyField.Bind(wx.EVT_TEXT, self.OnRemoteItemChange)
        self._portCtrl.Bind(wx.EVT_TEXT, self.OnRemoteItemChange)
        self._userField.SetInsertionPointEnd()
        self._passField.SetInsertionPointEnd()
        self._verifyField.SetInsertionPointEnd()
        self._portCtrl.SetInsertionPointEnd()

        self._currentPort = None
        self._lastStatusNumber = None
        self.Bind(EVT_WEBSERVERSTATUS, self.OnWebServerStatus)
        self._webServerStatusThread = WebServerStatusThread(self)
        self._webServerStatusThread.runSync()  # get initial status
        self._webServerStatusThread.start()

        self.Bind(wx.EVT_WINDOW_DESTROY, self.OnDestroy)

        return remotePanel


    ###########################################################
    def OnDestroy(self, event):
        """ Make sure that the web server status thread goes down with us. """
        if self._webAllowed:
            self._webServerStatusThread.stop()

    ###########################################################
    def OnAdvanced(self, event=None):
        """Display the advanced remote settings dialog.

        @param  event  Ignored.
        """
        dlg = AdvancedDialog(self, self._backEndClient, self._logger)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()


    ###########################################################
    def OnApply(self, event=None):
        self._validateAndSetRemoteSettings(False)


    ###########################################################
    def OnOk(self, event=None):
        """Close the dialog applying any changes.

        @param  event  The button event.
        """
        curStorageSize = int(self._storageSizeCtrl.GetValue())
        if curStorageSize < _kStorageSizeGBMin or curStorageSize > _kStorageSizeGBMax:
            wx.MessageBox("Invalid value %d for storage size, please enter values between %d and %d" % \
                    (curStorageSize, _kStorageSizeGBMin, _kStorageSizeGBMax),
                    _kErrorTitle, wx.OK | wx.ICON_ERROR, self)
            return

        if self._origStorageSize != curStorageSize:
            self._backEndClient.setMaxStorageSize(curStorageSize)

        curCacheSize = int(self._cacheSizeCtrl.GetValue())
        if curCacheSize < _kCacheSizeMinutesMin or curCacheSize > _kCacheSizeMinutesMax:
            wx.MessageBox("Invalid value %d for cache size, please enter values between %d and %d" % \
                    (curCacheSize, _kCacheSizeMinutesMin, _kCacheSizeMinutesMax),
                    _kErrorTitle, wx.OK | wx.ICON_ERROR, self)
            return
        if self._origCacheSize != curCacheSize:
            self._backEndClient.setCacheDuration(curCacheSize)

        # Check if the "run the back end as a service" checkbox changed...
        if (self._serviceInstalled and
            self._wasServiceStartsBackend != self._runBackendAsService.GetValue()):

            wantService = self._runBackendAsService.GetValue()
            if not setServiceStartsBackend(wantService):
                self._logger.error(
                    "Unable to store service launch config: "
                    "serviceStartBackend=%s" % wantService)
            else:
                # Which process owns the back end only changes on the next back
                # end start, so say so rather than leaving the user wondering
                # why nothing happened.
                wx.MessageBox(
                    _kServiceChangedBody % (
                        "now" if wantService else "no longer"),
                    _kServiceChangedTitle,
                    wx.OK | wx.ICON_INFORMATION, self)

        # Check if the autostart checkbox value changed...
        if self._wasAutoStart != self._launchOnStartup.GetValue():

            # If autostart checkbox value changed, save this configuration, and
            # log any errors...
            if not setServiceAutoStart(not self._wasAutoStart):
                self._logger.error(
                    "Unable to store service launch config: autostart=%s"
                    % (not self._wasAutoStart)
                )

            # If the user enabled autostart, make sure the service launches the
            # backend, and log any errors...
            if self._launchOnStartup.GetValue():
                if not setServiceStartsBackend(True):
                    self._logger.error(
                        "Unable to store service launch config: "
                        "serviceStartBackend=True"
                    )
                elif hasattr(self, '_runBackendAsService'):
                    self._runBackendAsService.SetValue(True)

        # TODO: Update remote values if different
        if not self._validateAndSetRemoteSettings():
            return

        self._setGridPrefs()


        value = self._combineTimeLimit.GetSelection() if self._combineCheckbox.GetValue() else 0
        if value != self._clipMergeLimit:
            self._backEndClient.setClipMergeThreshold(value)

        if self._enableHardwareAcceleration:
            device = "none"
            if self._enableHardwareAcceleration.GetValue():
                device = "auto"
                if self._hwDevicesChoice:
                    device = self._hwDevicesChoice.GetString(self._hwDevicesChoice.GetSelection())
            if device != self._hwDevice:
                self._backEndClient.setHardwareDevice("" if device == "auto" else device)

        self._saveAISettings()
        self._saveIHostSettings()
        self._saveTapoSettings()
        self._saveSavedEventsSettings()
        self._saveColorsSettings()

        self.EndModal(wx.OK)


    ###########################################################
    def _getAIConfigPath(self):
        """Path the detector reads from (shared single source of truth)."""
        return AICfg.getConfigPath()

    ###########################################################
    def _loadAIConfig(self):
        """Return the full imagecheck config, merged over the baseline defaults.

        Delegates to the shared loader so every key (incl. per-class nudity
        thresholds) is always present and matches what the detector sees.
        """
        return AICfg.loadConfig()

    ###########################################################
    def _aiModelChoices(self, catalog, resolver):
        """Build the (names, labels) pair backing a model picker.

        A model this machine does not carry is still LISTED -- the config stays
        forward-compatible if the file turns up later, and the detector falls
        back on its own -- but its label says so.

        @param  catalog   AICfg.YOLO_MODELS or AICfg.NUDE_MODELS.
        @param  resolver  name -> absolute path or None.
        @return           (bare file names, display labels), index-aligned.
        """
        names, labels = [], []
        for entry in catalog:
            name, label = entry[0], entry[1]
            if resolver(name) is None:
                label += "  (not installed)"
            names.append(name)
            labels.append(label)
        return names, labels

    ###########################################################
    def _aiSelectModel(self, choice, names, value):
        """Select the entry for a stored config value, defaulting to the first.

        Falling back to index 0 mirrors what the detector does with a name it
        does not recognise, so the dialog never shows something other than what
        would actually run.
        """
        try:
            choice.SetSelection(names.index(str(value)))
        except ValueError:
            choice.SetSelection(0)

    ###########################################################
    def _aiChosenModel(self, choice, names):
        """The bare file name a picker currently has selected, or None.

        Read back by INDEX rather than GetString(): the labels carry a
        "(not installed)" suffix, so the string is not the config value.
        """
        index = choice.GetSelection()
        if 0 <= index < len(names):
            return names[index]
        return None

    ###########################################################
    def _createAIDetectionPanel(self):
        """Create the AI Detection settings panel for the Options notebook."""
        panel = wx.Panel(self._notebook, -1)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddSpacer(_kSpaceSize1)

        # Make sure a config file exists so the detector and this UI agree from
        # the first run (no-op if one is already present).
        try:
            AICfg.ensureDefaults()
        except Exception:
            pass

        cfg = self._loadAIConfig()

        def _pct(key, fallback):
            return int(round(cfg.get(key, fallback) * 100))

        # --- Object detection model ---
        # Above the confidence row on purpose: the model decides what that
        # confidence is even applied to.
        self._yoloModelNames, yoloModelLabels = self._aiModelChoices(
            AICfg.YOLO_MODELS, AICfg.resolveYoloModelPath)
        yoloModelRow = wx.BoxSizer(wx.HORIZONTAL)
        yoloModelRow.Add(wx.StaticText(panel, -1, "Object detection model:"),
                         0, wx.ALIGN_CENTER_VERTICAL)
        self._yoloModelChoice = wx.Choice(panel, -1, choices=yoloModelLabels)
        self._aiSelectModel(self._yoloModelChoice, self._yoloModelNames,
                            cfg.get('YOLO_MODEL',
                                    AICfg.DEFAULTS['YOLO_MODEL']))
        self._yoloModelChoice.Bind(wx.EVT_CHOICE, self._onAIYoloModel)
        yoloModelRow.Add(self._yoloModelChoice, 0,
                         wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(yoloModelRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        self._yoloModelHelp = wx.StaticText(panel, -1, "")
        self._yoloModelHelp.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(self._yoloModelHelp, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        # --- YOLO detection confidence ---
        yoloRow = wx.BoxSizer(wx.HORIZONTAL)
        yoloRow.Add(wx.StaticText(panel, -1, "Object detection confidence:"),
                    0, wx.ALIGN_CENTER_VERTICAL)
        self._yoloConfCtrl = wx.SpinCtrl(
            panel, -1, str(_pct('YOLO_CONF_THRESHOLD', 0.25)),
            min=5, max=95, size=(60, -1))
        yoloRow.Add(self._yoloConfCtrl, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        yoloRow.Add(wx.StaticText(panel, -1, "%  (lower = more detections)"),
                    0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(yoloRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # --- Person confidence floor for the face/nudity stage ---
        personRow = wx.BoxSizer(wx.HORIZONTAL)
        personRow.Add(wx.StaticText(
            panel, -1, "Run face && nudity only when person confidence ≥"),
            0, wx.ALIGN_CENTER_VERTICAL)
        self._personConfCtrl = wx.SpinCtrl(
            panel, -1, str(_pct('PERSON_CONF_FOR_ATTRS', 0.50)),
            min=5, max=95, size=(60, -1))
        personRow.Add(self._personConfCtrl, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        personRow.Add(wx.StaticText(panel, -1, "%"),
                      0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(personRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        personHelp = wx.StaticText(
            panel, -1,
            "    Faces and nudity are only analysed once a confident person is "
            "found (higher = fewer false detections).")
        personHelp.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(personHelp, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        # --- Face recognition ---
        self._faceEnableCtrl = wx.CheckBox(panel, -1, "Enable face recognition")
        self._faceEnableCtrl.SetValue(bool(cfg.get('RUN_FACE', True)))
        self._faceEnableCtrl.Bind(wx.EVT_CHECKBOX, self._onAIFaceToggle)
        sizer.Add(self._faceEnableCtrl, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # Face *detection* confidence ("is this actually a face?")
        faceDetRow = wx.BoxSizer(wx.HORIZONTAL)
        faceDetRow.AddSpacer(_kBorderSize)
        faceDetRow.Add(wx.StaticText(panel, -1, "Face detection confidence:"),
                       0, wx.ALIGN_CENTER_VERTICAL)
        self._faceDetConfCtrl = wx.SpinCtrl(
            panel, -1, str(_pct('FACE_DET_CONF', 0.60)),
            min=10, max=95, size=(60, -1))
        faceDetRow.Add(self._faceDetConfCtrl, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        faceDetRow.Add(wx.StaticText(panel, -1, "%   (is it a face?)"),
                       0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(faceDetRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # Face *match* confidence ("is it this person?")
        faceRow = wx.BoxSizer(wx.HORIZONTAL)
        faceRow.AddSpacer(_kBorderSize)
        faceRow.Add(wx.StaticText(panel, -1, "Face match confidence:"),
                    0, wx.ALIGN_CENTER_VERTICAL)
        self._faceConfCtrl = wx.SpinCtrl(
            panel, -1, str(_pct('FACEMATCH_CONF', 0.40)),
            min=10, max=90, size=(60, -1))
        faceRow.Add(self._faceConfCtrl, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        faceRow.Add(wx.StaticText(panel, -1, "%   (is it this person?)"),
                    0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(faceRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # Baseline folder is fixed to the data dir — not exposed in the UI.
        self._baselineFolderFixed = os.path.join(os.path.expanduser('~'),
                                                  'AppData', 'Local',
                                                  'Sighthound Video Py3', 'Baseline')

        enrollRow = wx.BoxSizer(wx.HORIZONTAL)
        enrollRow.AddSpacer(_kBorderSize)
        self._enrollBtn = wx.Button(panel, -1, "Re-enroll faces now")
        self._enrollBtn.Bind(wx.EVT_BUTTON, self.OnAIEnroll)
        enrollRow.Add(self._enrollBtn, 0)
        self._manageFacesBtn = wx.Button(panel, -1, "Manage enrollments...")
        self._manageFacesBtn.Bind(wx.EVT_BUTTON, self.OnAIManageFaces)
        enrollRow.Add(self._manageFacesBtn, 0, wx.LEFT, _kPaddingSize)
        self._enrollStatusLabel = wx.StaticText(panel, -1, "")
        enrollRow.Add(self._enrollStatusLabel, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(enrollRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)

        # Show existing enrollment summary if known_faces.dat exists
        self._refreshEnrollSummary()

        sizer.AddSpacer(_kSpaceSize1)
        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        # --- NudeNet ---
        self._nudeEnableCtrl = wx.CheckBox(panel, -1,
            "Enable nudity detection")
        self._nudeEnableCtrl.SetValue(bool(cfg.get('RUN_NUDITY', False)))
        self._nudeEnableCtrl.Bind(wx.EVT_CHECKBOX, self._onAINudeToggle)
        sizer.Add(self._nudeEnableCtrl, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # Nudity model, indented under the master checkbox.
        self._nudeModelNames, nudeModelLabels = self._aiModelChoices(
            AICfg.NUDE_MODELS, AICfg.resolveNudeModelPath)
        nudeModelRow = wx.BoxSizer(wx.HORIZONTAL)
        nudeModelRow.AddSpacer(_kBorderSize)
        nudeModelRow.Add(wx.StaticText(panel, -1, "Nudity model:"),
                         0, wx.ALIGN_CENTER_VERTICAL)
        self._nudeModelChoice = wx.Choice(panel, -1, choices=nudeModelLabels)
        self._aiSelectModel(self._nudeModelChoice, self._nudeModelNames,
                            cfg.get('NUDE_MODEL',
                                    AICfg.DEFAULTS['NUDE_MODEL']))
        self._nudeModelChoice.Bind(wx.EVT_CHOICE, self._onAINudeModel)
        nudeModelRow.Add(self._nudeModelChoice, 0,
                         wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)
        sizer.Add(nudeModelRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        self._nudeModelHelp = wx.StaticText(panel, -1, "")
        self._nudeModelHelp.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(self._nudeModelHelp, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        catLabel = wx.StaticText(panel, -1, "    Detect these categories:")
        catLabel.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(catLabel, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # Per-category checkbox + threshold spinner.
        nudeThr = cfg.get('NUDE_THRESHOLDS', {})
        nudeEnabledList = cfg.get('NUDE_ENABLED',
                                  [k for k, _ in AICfg.NUDE_CATEGORIES])
        nudeEnabledSet = {e.upper() for e in nudeEnabledList}
        self._nudeCatChecks = {}
        self._nudeCatSpins = {}
        catGrid = wx.FlexGridSizer(0, 2, _kPaddingSize, _kPaddingSize)
        for key, label in AICfg.NUDE_CATEGORIES:
            chk = wx.CheckBox(panel, -1, label)
            chk.SetValue(key in nudeEnabledSet)
            chk.Bind(wx.EVT_CHECKBOX, self._onAINudeToggle)
            self._nudeCatChecks[key] = chk

            thrPct = int(round(nudeThr.get(key,
                         AICfg.DEFAULTS['NUDE_THRESHOLDS'].get(key, 0.40)) * 100))
            spin = wx.SpinCtrl(panel, -1, str(thrPct), min=5, max=95, size=(60, -1))
            self._nudeCatSpins[key] = spin

            spinRow = wx.BoxSizer(wx.HORIZONTAL)
            spinRow.Add(spin, 0, wx.ALIGN_CENTER_VERTICAL)
            spinRow.Add(wx.StaticText(panel, -1, "%  threshold"),
                        0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, _kPaddingSize)

            catGrid.Add(chk, 0, wx.ALIGN_CENTER_VERTICAL)
            catGrid.Add(spinRow, 0, wx.ALIGN_CENTER_VERTICAL)

        catIndent = wx.BoxSizer(wx.HORIZONTAL)
        catIndent.AddSpacer(_kBorderSize)
        catIndent.Add(catGrid, 0)
        sizer.Add(catIndent, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        # --- Notes ---
        rescanNote = wx.StaticText(panel, -1,
            "Note: Adjusting these settings does not re-scan previously "
            "recorded events.\nChanges only affect new detections going forward.")
        rescanNote.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(rescanNote, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        # Cameras and the detector are different processes: the shared
        # DetectionService is started once and only respawned if it dies, so
        # restarting cameras does NOT pick up a different model file.
        restartNote = wx.StaticText(panel, -1,
            "Threshold changes take effect after restarting cameras.\n"
            "Changing a model takes effect after restarting the back end.")
        restartNote.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(restartNote, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        panel.SetSizer(sizer)

        # Set initial enabled/disabled state of dependent controls, and seed
        # the model help lines.
        self._onAIFaceToggle()
        self._onAINudeToggle()
        self._onAIYoloModel()
        self._onAINudeModel()
        return panel

    ###########################################################
    def _onAIFaceToggle(self, event=None):
        """Enable/disable the face sub-controls with the master face checkbox."""
        on = self._faceEnableCtrl.GetValue()
        self._faceDetConfCtrl.Enable(on)
        self._faceConfCtrl.Enable(on)
        self._enrollBtn.Enable(on)
        self._manageFacesBtn.Enable(on)


    ###########################################################
    def _refreshEnrollSummary(self):
        """Show a summary of known_faces.dat in the enroll status label."""
        dataDir = getUserLocalDataDir()
        if dataDir is None:
            dataDir = os.path.join(os.path.expanduser('~'),
                                   'AppData', 'Local', 'Sighthound Video Py3')
        datPath = os.path.join(dataDir, 'known_faces.dat')
        if not os.path.isfile(datPath):
            return
        try:
            with open(datPath, 'rb') as f:
                dat = pickle.load(f)
            names = dat.get('names', [])
            unique = sorted(set(names))
            mtime = time.strftime('%Y-%m-%d %H:%M',
                                  time.localtime(os.path.getmtime(datPath)))
            summary = "%d people enrolled (%s)  last updated: %s" % (
                len(unique), ', '.join(unique), mtime)
        except Exception:
            summary = "known_faces.dat exists (could not parse)"
        self._enrollStatusLabel.SetLabel(summary)


    ###########################################################
    def OnAIManageFaces(self, event=None):
        """Open the enrollment manager, then refresh the summary."""
        from frontEnd.ManageFacesDialog import ManageFacesDialog
        dlg = ManageFacesDialog(self, self._backEndClient)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self._refreshEnrollSummary()

    ###########################################################
    def _onAINudeToggle(self, event=None):
        """Enable/disable nudity category controls.

        Categories are editable only when the master nudity checkbox is on;
        each threshold spinner is editable only when its own category is ticked.
        The model picker hangs off the MASTER checkbox only -- it selects the
        engine, while the categories filter what that engine reports.
        """
        master = self._nudeEnableCtrl.GetValue()
        self._nudeModelChoice.Enable(master)
        self._nudeModelHelp.Enable(master)
        for key in self._nudeCatChecks:
            chk = self._nudeCatChecks[key]
            chk.Enable(master)
            self._nudeCatSpins[key].Enable(master and chk.GetValue())

    ###########################################################
    def _onAIYoloModel(self, event=None):
        """Update the help line under the object-detection model picker."""
        name = self._aiChosenModel(self._yoloModelChoice,
                                   self._yoloModelNames)
        if name is None:
            return
        if AICfg.resolveYoloModelPath(name) is None:
            text = ("    %s is not installed on this machine - %s will be "
                    "used instead." % (name, AICfg.DEFAULTS['YOLO_MODEL']))
        elif name.startswith('yolo26'):
            text = ("    YOLO26 needs no duplicate-box filtering, so its "
                    "timing stays steady on noisy scenes (rain, IR grain, "
                    "moving foliage).")
        else:
            text = ("    YOLO11 costs about the same as the YOLO26 of the "
                    "same size; useful mainly for comparison.")
        if name.endswith('s.pt'):
            text += "  The small model detects distant subjects better."
        self._yoloModelHelp.SetLabel(text)

    ###########################################################
    def _onAINudeModel(self, event=None):
        """Update the help line under the nudity model picker."""
        name = self._aiChosenModel(self._nudeModelChoice,
                                   self._nudeModelNames)
        if name is None:
            return
        if AICfg.resolveNudeModelPath(name) is None:
            text = ("    %s is not installed on this machine - %s will be "
                    "used instead." % (name, AICfg.DEFAULTS['NUDE_MODEL']))
        elif AICfg.nudeModelResolution(name) >= 640:
            text = ("    Higher resolution: better on small or distant "
                    "subjects, but roughly 4x slower per check and "
                    "noticeably more GPU memory.")
        else:
            text = "    Fast and low memory.  Recommended for most systems."
        self._nudeModelHelp.SetLabel(text)

    ###########################################################
    def OnAIEnroll(self, event=None):
        """Rebuild known_faces.dat from the Baseline folders.

        Runs on the BACK END via RPC (the shared DetectionService already
        holds the face model) — the front end no longer loads InsightFace
        into its own process for this.
        """
        baselineFolder = self._baselineFolderFixed
        if not os.path.isdir(baselineFolder):
            wx.MessageBox("Baseline folder not found:\n%s" % baselineFolder,
                          "Error", wx.OK | wx.ICON_ERROR, self)
            return

        self._enrollBtn.Disable()
        self._enrollStatusLabel.SetLabel("Enrolling, please wait...")
        self.Update()

        def _run():
            try:
                result = self._backEndClient.rebuildKnownFaces()
            except Exception as e:
                result = {"error": str(e)}
            wx.CallAfter(self._onEnrollDone, result)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    ###########################################################
    def _onEnrollDone(self, result):
        self._enrollBtn.Enable()
        if not result or not result.get("ok"):
            err = (result or {}).get("error", "enrollment failed")
            self._enrollStatusLabel.SetLabel("Error: " + err)
            wx.MessageBox(err, "Enrollment failed", wx.OK | wx.ICON_ERROR, self)
        else:
            self._enrollStatusLabel.SetLabel(
                "%d people, %d encodings enrolled  (skipped: %d images)" % (
                    result.get("people", 0), result.get("totalEncodings", 0),
                    result.get("skipped", 0)))

    ###########################################################
    def _saveAISettings(self):
        """Write imagecheck_config.json from the AI Detection tab controls."""
        try:
            cfg = self._loadAIConfig()
            cfg['YOLO_CONF_THRESHOLD']   = round(self._yoloConfCtrl.GetValue() / 100.0, 2)
            cfg['PERSON_CONF_FOR_ATTRS'] = round(self._personConfCtrl.GetValue() / 100.0, 2)
            cfg['RUN_FACE']              = self._faceEnableCtrl.GetValue()
            cfg['FACE_DET_CONF']         = round(self._faceDetConfCtrl.GetValue() / 100.0, 2)
            cfg['FACEMATCH_CONF']        = round(self._faceConfCtrl.GetValue() / 100.0, 2)
            cfg['BASELINE_FOLDER']       = self._baselineFolderFixed
            cfg['RUN_NUDITY']            = self._nudeEnableCtrl.GetValue()

            # Model pickers.  Guarded against wx.NOT_FOUND (-1) so an empty
            # selection can never index backwards into the catalog and save a
            # model the user did not choose.
            yoloModel = self._aiChosenModel(self._yoloModelChoice,
                                            self._yoloModelNames)
            if yoloModel is not None:
                cfg['YOLO_MODEL'] = yoloModel
            nudeModel = self._aiChosenModel(self._nudeModelChoice,
                                            self._nudeModelNames)
            if nudeModel is not None:
                cfg['NUDE_MODEL'] = nudeModel

            # Per-category nudity thresholds + which categories are enabled.
            # Thresholds for unticked categories are still persisted so the
            # value is remembered if the user re-enables them later.
            thresholds = dict(cfg.get('NUDE_THRESHOLDS', {}))
            enabled = []
            for key, _label in AICfg.NUDE_CATEGORIES:
                thresholds[key] = round(self._nudeCatSpins[key].GetValue() / 100.0, 2)
                if self._nudeCatChecks[key].GetValue():
                    enabled.append(key)
            cfg['NUDE_THRESHOLDS'] = thresholds
            cfg['NUDE_ENABLED']    = enabled

            if not AICfg.saveConfig(cfg):
                self._logger.error("Failed to save AI settings (saveConfig)")
        except Exception as e:
            self._logger.error("Failed to save AI settings: %s" % e)

    ###########################################################
    def _createIHostPanel(self):
        """Create the iHost (eWeLink CUBE) settings panel for the notebook."""
        panel = wx.Panel(self._notebook, -1)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddSpacer(_kSpaceSize1)

        try:
            IHostCfg.ensureDefaults()
        except Exception:
            pass
        cfg = IHostCfg.loadConfig()

        # Set while a search / token wait is running, so the same button can
        # stop it and a second click can't start a second worker.
        self._ihostSearchCancel = None
        self._ihostTokenCancel = None

        intro = wx.StaticText(
            panel, -1,
            "Global connection to your iHost / eWeLink CUBE hub.  Rules use the "
            "\"Send iHost command\" action to control devices by name.")
        intro.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(intro, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1)

        def _row(label, ctrl):
            grid.Add(wx.StaticText(panel, -1, label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.EXPAND)

        docLink = wx.adv.HyperlinkCtrl(
            panel, -1, "eWeLink CUBE Open API documentation", _kIHostDocUrl,
            style=wx.NO_BORDER | wx.adv.HL_CONTEXTMENU | wx.adv.HL_ALIGN_LEFT)
        sizer.Add(docLink, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        def _rowWithButton(label, ctrl, button):
            """A field the user can still type into, with a helper beside it."""
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
            row.Add(button, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
            _row(label, row)

        self._ihostIpCtrl = wx.TextCtrl(panel, -1, str(cfg.get("ip", "")))
        self._ihostTokenCtrl = wx.TextCtrl(panel, -1, str(cfg.get("token", "")))
        self._ihostLatCtrl = wx.TextCtrl(panel, -1, str(cfg.get("latitude", "")))
        self._ihostLonCtrl = wx.TextCtrl(panel, -1, str(cfg.get("longitude", "")))

        self._ihostSearchButton = wx.Button(panel, -1, "Search...")
        self._ihostSearchButton.SetToolTip(
            "Look for the hub on this network")
        self._ihostSearchButton.Bind(wx.EVT_BUTTON, self._onIHostSearch)

        self._ihostTokenButton = wx.Button(panel, -1, "Get token...")
        self._ihostTokenButton.SetToolTip(
            "Ask the hub for an API token (needs confirmation on the hub)")
        self._ihostTokenButton.Bind(wx.EVT_BUTTON, self._onIHostGetToken)

        # Same location controls as the rule editor's action schedule: detect
        # from the public IP when the fields are blank, and a city list for
        # when there's no internet.  Shared code, so the two screens can't
        # drift apart -- see ScheduleLocationPicker.
        self._ihostPickCityButton = wx.Button(panel, -1, "Pick city...")
        self._ihostPickCityButton.SetToolTip(
            "Choose from a built-in city list (works offline)")
        self._ihostPickCityButton.Bind(wx.EVT_BUTTON, self._onIHostPickCity)

        latLonRow = wx.BoxSizer(wx.HORIZONTAL)
        latLonRow.Add(wx.StaticText(panel, -1, "Lat:"), 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        latLonRow.Add(self._ihostLatCtrl, 1, wx.ALIGN_CENTER_VERTICAL)
        latLonRow.Add(wx.StaticText(panel, -1, "Lon:"), 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 6)
        latLonRow.Add(self._ihostLonCtrl, 1, wx.ALIGN_CENTER_VERTICAL)
        latLonRow.Add(self._ihostPickCityButton, 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)

        _rowWithButton("Hub IP address:", self._ihostIpCtrl,
                       self._ihostSearchButton)
        _rowWithButton("API token:", self._ihostTokenCtrl,
                       self._ihostTokenButton)
        _row("Location (for night-only):", latLonRow)

        self._ihostLocationHint = wx.StaticText(
            panel, -1, "(decimal degrees, e.g. 45.4, -75.7)")
        self._ihostLocationHint.SetForegroundColour(wx.Colour(100, 100, 100))
        grid.Add((0, 0))
        grid.Add(self._ihostLocationHint, 0, wx.EXPAND)
        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        refreshRow = wx.BoxSizer(wx.HORIZONTAL)
        self._ihostRefreshButton = wx.Button(panel, -1, "Refresh devices")
        self._ihostRefreshButton.Bind(wx.EVT_BUTTON, self._onIHostRefresh)
        refreshRow.Add(self._ihostRefreshButton, 0, wx.ALIGN_CENTER_VERTICAL)
        self._ihostStatusLabel = wx.StaticText(
            panel, -1, "%d device(s) cached" % len(cfg.get("devices", []) or []))
        refreshRow.Add(self._ihostStatusLabel, 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        sizer.Add(refreshRow, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        self._ihostDeviceList = wx.ListBox(
            panel, -1, style=wx.LB_SINGLE,
            choices=[d.get("name", "") for d in (cfg.get("devices", []) or [])])
        sizer.Add(self._ihostDeviceList, 1,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorderSize)

        panel.SetSizer(sizer)

        # Nothing stored yet: work the location out from the public IP, the way
        # the action schedule does.  Only when BOTH are blank, so a hand-entered
        # position is never overwritten.
        if not self._ihostLatCtrl.GetValue().strip() and \
           not self._ihostLonCtrl.GetValue().strip():
            wx.CallAfter(self._ihostAutoDetectLocation)

        return panel

    ###########################################################
    def _createTapoPanel(self):
        """Create the Tapo camera-control settings panel for the notebook."""
        panel = wx.Panel(self._notebook, -1)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddSpacer(_kSpaceSize1)

        tapoUser, tapoPassword = getTapoCredentials()

        intro = wx.StaticText(panel, -1, _kTapoIntro)
        intro.Wrap(_kTapoTextWrap)
        intro.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(intro, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1)

        def _row(label, ctrl):
            grid.Add(wx.StaticText(panel, -1, label), 0,
                     wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.EXPAND)

        self._tapoUserCtrl = wx.TextCtrl(panel, -1, tapoUser)
        self._tapoPasswordCtrl = wx.TextCtrl(panel, -1, tapoPassword,
                                             style=wx.TE_PASSWORD)
        _row("TP-Link account:", self._tapoUserCtrl)
        _row("Password:", self._tapoPasswordCtrl)

        # Test against a real camera, because the only way to know these
        # credentials are right is to have a camera accept them.
        self._tapoCameras = self._tapoTestableCameras()
        self._tapoCameraChoice = wx.Choice(
            panel, -1, choices=[loc for loc, _ in self._tapoCameras])
        if self._tapoCameras:
            self._tapoCameraChoice.SetSelection(0)
        self._tapoTestButton = wx.Button(panel, -1, "Test")
        self._tapoTestButton.Bind(wx.EVT_BUTTON, self._onTapoTest)
        self._tapoTestButton.Enable(bool(self._tapoCameras))

        testRow = wx.BoxSizer(wx.HORIZONTAL)
        testRow.Add(self._tapoCameraChoice, 1, wx.ALIGN_CENTER_VERTICAL)
        testRow.Add(self._tapoTestButton, 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        _row("Test on camera:", testRow)

        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kPaddingSize)

        self._tapoStatusLabel = wx.StaticText(
            panel, -1, "" if self._tapoCameras else _kTapoNoCameras)
        sizer.Add(self._tapoStatusLabel, 0, wx.LEFT | wx.RIGHT, _kBorderSize)

        sizer.AddStretchSpacer(1)
        storageNote = wx.StaticText(panel, -1, _kTapoStorageNote)
        storageNote.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(storageNote, 0,
                  wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorderSize)

        panel.SetSizer(sizer)
        return panel


    ###########################################################
    def _tapoTestableCameras(self):
        """Cameras with a network address we could try these credentials on.

        @return cams  [(location, uri)], in the order the back end lists them.
        """
        cams = []
        try:
            for location in self._backEndClient.getCameraLocations():
                _, uri, _, _ = self._backEndClient.getCameraSettings(location)
                # Credentials come from the fields on this tab; all we need to
                # know here is that the URI names a real camera on the network
                # rather than a webcam or a local test stream.
                if tapoHostFromUri(uri) is not None:
                    cams.append((location, uri))
        except Exception as e:
            self._logger.error("Could not list cameras for the Tapo tab: %s" % e)
        return cams


    ###########################################################
    def _onTapoTest(self, event=None):
        """Try the entered credentials against the chosen camera."""
        user = self._tapoUserCtrl.GetValue().strip()
        password = self._tapoPasswordCtrl.GetValue()
        if not user or not password:
            self._tapoStatusLabel.SetLabel(_kTapoNeedBoth)
            return

        index = self._tapoCameraChoice.GetSelection()
        if index == wx.NOT_FOUND or not self._tapoCameras:
            self._tapoStatusLabel.SetLabel(_kTapoNoCameras)
            return
        location, uri = self._tapoCameras[index]

        target = parseTapoTarget(uri, user, password)
        if target is None:
            self._tapoStatusLabel.SetLabel(_kTapoNoCameras)
            return

        self._tapoTestButton.Disable()
        self._tapoStatusLabel.SetLabel(_kTapoTesting % location)

        def _done(ok, value):
            # Runs on the controller's worker thread.
            wx.CallAfter(self._onTapoTestDone, location, ok, value)

        TapoController.instance(self._logger).submit(target, kOpProbe, _done)


    ###########################################################
    def _onTapoTestDone(self, location, ok, value):
        """Report the result of a credentials test.

        @param  location  The camera that was tried.
        @param  ok        True if it accepted the credentials.
        @param  value     The camera's description, or a failure message.
        """
        # The worker thread outlives this dialog, so a reply can land after the
        # user has closed it.
        if not self:
            return

        self._tapoTestButton.Enable()
        if ok:
            self._tapoStatusLabel.SetLabel(_kTapoTestOk % (location, value))
        elif isAuthError(value):
            self._tapoStatusLabel.SetLabel(_kTapoTestAuthFailed % location)
        else:
            self._tapoStatusLabel.SetLabel(_kTapoTestFailed % (location, value))

        # SetLabel above has already replaced any newlines a previous Wrap
        # inserted, so wrapping to the fixed width can't compound.
        self._tapoStatusLabel.Wrap(_kTapoTextWrap)
        self._tapoStatusLabel.GetParent().Layout()


    ###########################################################
    def _saveTapoSettings(self):
        """Write the Tapo tab's credentials to tapo_config.json."""
        try:
            if not TapoCfg.saveConfig({
                    "user": self._tapoUserCtrl.GetValue().strip(),
                    "password": self._tapoPasswordCtrl.GetValue()}):
                self._logger.error("Failed to save Tapo settings (saveConfig)")
        except Exception as e:
            self._logger.error("Failed to save Tapo settings: %s" % e)


    ###########################################################
    def _ihostAutoDetectLocation(self):
        """Fill the location from IP geolocation if it is still blank."""
        try:
            from .ScheduleLocationPicker import schedAutoDetectLocation
            schedAutoDetectLocation(self._ihostLatCtrl, self._ihostLonCtrl,
                                    self._ihostLocationHint, self)
        except Exception as e:
            self._logger.error("iHost location auto-detect failed: %s" % e)

    ###########################################################
    def _onIHostPickCity(self, event=None):
        """Offline fallback: choose a city from the built-in list."""
        from .ScheduleLocationPicker import schedOnPickCity
        schedOnPickCity(self, self._ihostLatCtrl, self._ihostLonCtrl,
                        self._ihostLocationHint)

    ###########################################################
    def _onIHostSearch(self, event=None):
        """Find the hub on this network and fill in its address.

        Runs off the UI thread: the hostname probe is quick, but the fallback
        sweeps every address on this machine's subnets.
        """
        import appCommon.IHostClient as IHostClient

        if self._ihostSearchCancel is not None:      # already searching
            self._ihostSearchCancel.set()
            return

        cancel = threading.Event()
        self._ihostSearchCancel = cancel
        self._ihostSearchButton.SetLabel("Stop")
        self._ihostStatusLabel.SetLabel("Searching for hub...")

        def _progress(done, total):
            wx.CallAfter(self._ihostStatusLabel.SetLabel,
                         "Searching for hub... %d%%" % (100 * done / total))

        def _work():
            try:
                hubs = IHostClient.discoverHubs(progressFn=_progress,
                                                cancelFn=cancel.is_set)
                err = None
            except Exception as e:
                hubs, err = [], e
            wx.CallAfter(self._onIHostSearchDone, hubs, err, cancel)

        threading.Thread(target=_work, daemon=True).start()

    ###########################################################
    def _onIHostSearchDone(self, hubs, err, cancel):
        """Back on the UI thread with whatever the search turned up."""
        self._ihostSearchCancel = None
        self._ihostSearchButton.SetLabel("Search...")

        if err is not None:
            self._ihostStatusLabel.SetLabel("Search failed")
            wx.MessageBox("Could not search the network:\n%s" % err,
                          "iHost", wx.OK | wx.ICON_ERROR, self)
            return
        if cancel.is_set() and not hubs:
            self._ihostStatusLabel.SetLabel("Search stopped")
            return
        if not hubs:
            self._ihostStatusLabel.SetLabel("No hub found")
            wx.MessageBox(
                "No iHost / eWeLink CUBE hub answered on this network.\n\n"
                "Check that the hub is powered on and on the same network, "
                "or enter its address by hand.",
                "iHost", wx.OK | wx.ICON_INFORMATION, self)
            return

        hub = hubs[0]
        if len(hubs) > 1:
            labels = ["%s  (%s)" % (h["ip"], h.get("name") or "iHost")
                      for h in hubs]
            dlg = wx.SingleChoiceDialog(self, "More than one hub answered:",
                                        "Choose a hub", labels)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    self._ihostStatusLabel.SetLabel("Search cancelled")
                    return
                hub = hubs[dlg.GetSelection()]
            finally:
                dlg.Destroy()

        self._ihostIpCtrl.SetValue(hub["ip"])
        self._ihostStatusLabel.SetLabel(
            "Found %s%s" % (hub.get("name") or "iHost",
                            " fw %s" % hub["fw_version"]
                            if hub.get("fw_version") else ""))

    ###########################################################
    def _onIHostGetToken(self, event=None):
        """Ask the hub for an API token.

        The hub refuses until somebody presses Done on its own web console, so
        this polls for as long as that confirmation window stays open.
        """
        import appCommon.IHostClient as IHostClient

        if self._ihostTokenCancel is not None:       # already waiting
            self._ihostTokenCancel.set()
            return

        ip = self._ihostIpCtrl.GetValue().strip()
        if not ip:
            wx.MessageBox("Enter the hub address first, or use Search.",
                          "iHost", wx.OK | wx.ICON_INFORMATION, self)
            return

        if wx.MessageBox(
                "The hub will only give out a token after you confirm it.\n\n"
                "1. Open the hub's web console at http://%s\n"
                "2. When the pop-up appears, press Done\n\n"
                "Waiting starts now and lasts up to %d minutes." %
                (ip, _kIHostTokenWaitSecs // 60),
                "Get iHost token", wx.OK | wx.CANCEL | wx.ICON_INFORMATION,
                self) != wx.OK:
            return

        cancel = threading.Event()
        self._ihostTokenCancel = cancel
        self._ihostTokenButton.SetLabel("Stop")

        def _work():
            deadline = time.time() + _kIHostTokenWaitSecs
            token, lastMsg, err = None, "", None
            while time.time() < deadline and not cancel.is_set():
                try:
                    token, lastMsg = IHostClient.requestAccessToken(ip)
                except Exception as e:
                    err = e
                    break
                if token:
                    break
                left = int(deadline - time.time())
                wx.CallAfter(self._ihostStatusLabel.SetLabel,
                             "Waiting for confirmation on the hub... %ds" % left)
                cancel.wait(_kIHostTokenPollSecs)
            wx.CallAfter(self._onIHostTokenDone, token, lastMsg, err, cancel)

        threading.Thread(target=_work, daemon=True).start()

    ###########################################################
    def _onIHostTokenDone(self, token, lastMsg, err, cancel):
        """Back on the UI thread with the token, or with why there isn't one."""
        self._ihostTokenCancel = None
        self._ihostTokenButton.SetLabel("Get token...")

        if err is not None:
            self._ihostStatusLabel.SetLabel("Could not reach the hub")
            wx.MessageBox("Could not reach the hub:\n%s" % err,
                          "Get iHost token", wx.OK | wx.ICON_ERROR, self)
            return
        if token:
            self._ihostTokenCtrl.SetValue(token)
            self._ihostStatusLabel.SetLabel("Token received")
            wx.MessageBox("Got a token from the hub.\n\n"
                          "Use \"Refresh devices\" to load its device list.",
                          "Get iHost token", wx.OK | wx.ICON_INFORMATION, self)
            return
        if cancel.is_set():
            self._ihostStatusLabel.SetLabel("Stopped")
            return
        self._ihostStatusLabel.SetLabel("No token")
        wx.MessageBox(
            "The hub did not confirm in time%s\n\n"
            "Press Done on the hub's web console while this is waiting, then "
            "try again." % (":\n%s" % lastMsg if lastMsg else "."),
            "Get iHost token", wx.OK | wx.ICON_INFORMATION, self)

    ###########################################################
    def _onIHostRefresh(self, event=None):
        """Fetch and cache the hub's device list from the current IP/token."""
        import appCommon.IHostClient as IHostClient
        ip = self._ihostIpCtrl.GetValue().strip()
        token = self._ihostTokenCtrl.GetValue().strip()
        if not ip or not token:
            wx.MessageBox("Enter the hub IP and API token first.",
                          "iHost", wx.OK | wx.ICON_INFORMATION, self)
            return
        try:
            devices = IHostClient.listDevices(ip, token)
        except Exception as e:
            wx.MessageBox("Could not reach the hub:\n%s" % e,
                          "Refresh failed", wx.OK | wx.ICON_ERROR, self)
            return

        cfg = IHostCfg.loadConfig()
        cfg["ip"] = ip
        cfg["token"] = token
        cfg["devices"] = [{"name": d["name"], "id": d["id"]} for d in devices]
        IHostCfg.saveConfig(cfg)

        self._ihostDeviceList.Set([d["name"] for d in cfg["devices"]])
        self._ihostStatusLabel.SetLabel(
            "%d device(s) cached" % len(cfg["devices"]))

    ###########################################################
    def _saveIHostSettings(self):
        """Write ihost_config.json from the iHost tab controls."""
        try:
            cfg = IHostCfg.loadConfig()
            cfg["ip"] = self._ihostIpCtrl.GetValue().strip()
            cfg["token"] = self._ihostTokenCtrl.GetValue().strip()
            for key, ctrl in (("latitude", self._ihostLatCtrl),
                              ("longitude", self._ihostLonCtrl)):
                val = ctrl.GetValue().strip()
                if val:
                    try:
                        cfg[key] = float(val)
                    except ValueError:
                        pass
            if not IHostCfg.saveConfig(cfg):
                self._logger.error("Failed to save iHost settings (saveConfig)")
        except Exception as e:
            self._logger.error("Failed to save iHost settings: %s" % e)

    ###########################################################
    def _createSavedEventsPanel(self):
        """Create the "Saved Events" (daily summary video) settings panel."""
        from vitaToolbox.wx.FileBrowseButtonFixed import DirBrowseButton

        panel = wx.Panel(self._notebook, -1)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddSpacer(_kSpaceSize1)

        try:
            settings = self._backEndClient.getSummarySettings() or {}
        except Exception:
            settings = {}

        intro = wx.StaticText(
            panel, -1,
            "Create a small, low-resolution daily summary video for each "
            "camera from the day's activity thumbnails.  One file per camera "
            "per day is written the following day.")
        intro.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(intro, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        self._summaryEnabledCheck = wx.CheckBox(
            panel, -1, "Create a daily summary video for each camera")
        self._summaryEnabledCheck.SetValue(bool(settings.get("enabled", False)))
        sizer.Add(self._summaryEnabledCheck, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        sizer.Add(wx.StaticText(panel, -1, "Save summaries to:"), 0,
                  wx.LEFT | wx.RIGHT, _kBorderSize)
        self._summaryDirField = DirBrowseButton(
            panel, -1, labelText="", changeCallback=lambda evt: None)
        self._summaryDirField.SetValue(str(settings.get("outputDir", "") or ""))
        sizer.Add(self._summaryDirField, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorderSize)

        panel.SetSizer(sizer)
        return panel

    ###########################################################
    def _createColorsPanel(self):
        """Create the "Colors" panel: the main window's background colour."""
        panel = wx.Panel(self._notebook, -1)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddSpacer(_kSpaceSize1)

        intro = wx.StaticText(
            panel, -1,
            "Choose the background colour for the main window's Monitor, "
            "Search, Grid and System Health views.  Text is adjusted "
            "automatically so labels stay readable on darker colours.")
        intro.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(intro, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        current = self._uiPrefsModel.getBackgroundColor()
        startColour = wx.Colour(*current) if current \
                      else systemBackgroundColour()

        rowSizer = wx.BoxSizer(wx.HORIZONTAL)
        rowSizer.Add(wx.StaticText(panel, -1, "Background colour:"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPaddingSize)
        self._bgColorPicker = wx.ColourPickerCtrl(panel, -1, startColour)
        rowSizer.Add(self._bgColorPicker, 0, wx.ALIGN_CENTER_VERTICAL)
        self._bgColorResetButton = wx.Button(panel, -1, "Use default")
        self._bgColorResetButton.Bind(wx.EVT_BUTTON, self.OnResetColors)
        rowSizer.Add(self._bgColorResetButton, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.LEFT, _kSpaceSize1)
        sizer.Add(rowSizer, 0, wx.LEFT | wx.RIGHT, _kBorderSize)
        sizer.AddSpacer(_kSpaceSize1)

        # Say this out loud rather than let it look like a bug: the view tabs
        # are bitmaps with grey baked into the artwork, so they can't follow.
        note = wx.StaticText(
            panel, -1,
            "Note: the view tabs along the top and the pop-up dialogs keep "
            "the standard system colour.\nVideo areas stay black.")
        note.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(note, 0, wx.LEFT | wx.RIGHT, _kBorderSize)

        panel.SetSizer(sizer)
        return panel

    ###########################################################
    def OnResetColors(self, event=None):
        """Put the picker back to the system default colour."""
        self._bgColorPicker.SetColour(systemBackgroundColour())

    ###########################################################
    def _saveColorsSettings(self):
        """Persist the chosen colour and apply it to the running UI."""
        try:
            colour = self._bgColorPicker.GetColour()
            # Matching the system colour means "no preference", so the app
            # keeps following the OS if that ever changes.  This is also what
            # "Use default" ends up doing, since it just sets the picker.
            if colour == systemBackgroundColour():
                self._uiPrefsModel.setBackgroundColor(None)
            else:
                self._uiPrefsModel.setBackgroundColor(
                    (colour.Red(), colour.Green(), colour.Blue()))
        except Exception as e:
            self._logger.error("Failed to save color settings: %s" % e)

    ###########################################################
    def _saveSavedEventsSettings(self):
        """Persist the Saved Events (daily summary) settings via the back end."""
        try:
            self._backEndClient.setSummarySettings(
                bool(self._summaryEnabledCheck.GetValue()),
                self._summaryDirField.GetValue().strip())
        except Exception as e:
            self._logger.error("Failed to save Saved Events settings: %s" % e)

    ###########################################################
    def OnRemoteItemChange(self, event=None):
        """Ensure the 'enable' box is checked when other settings change.

        @param  event  The change event.
        """
        self._webEnableCheckbox.SetValue(True)


    ###########################################################
    def OnCombineClips(self, event=None):
        self._combineTimeLimit.Enable(self._combineCheckbox.GetValue())

    ###########################################################
    def OnHwAccelerationChange(self, event=None):
        self._hwDevicesChoice.Enable(self._enableHardwareAcceleration.GetValue())

    ###########################################################
    def OnRemoteUserChange(self, event=None):
        """Respond to a username change.

        If the username is updated we need to force the user to enter a new
        password as we don't store their old, so clear the password fields
        if they haven't already been updated.

        @param  event  The change event.
        """
        if (self._passField.GetValue() == _kFakePassword):
            self._passField.SetValue("")
            self._verifyField.SetValue("")
        self.OnRemoteItemChange(event)


    ###########################################################
    def OnGridOrderChanged(self, event=None):
        """ Ensures proper controls' state when the grid order view potentially
        changed. At this moment it's about enabling and disabling the move
        buttons.

        @param  event  The event causing the invocation, if any.
        """
        selection = self._gridOrder.GetSelection()
        if selection == wx.NOT_FOUND:
            self._gridMoveDownButton.Enable(False)
            self._gridMoveUpButton.Enable(False)
        else:
            count = self._gridOrder.GetCount()
            self._gridMoveDownButton.Enable(selection < count - 1)
            self._gridMoveUpButton.Enable(selection > 0)
        if not event is None:
            event.Skip()


    ###########################################################
    def OnGridMove(self, inc):
        """ Moves a selected item in the grid order list up or down. Does
        nothing if no item is selected or moving it would be out of bounds.

        @param  inc  The direction to move, either -1 or +1.
        """
        selection = self._gridOrder.GetSelection()
        if selection == wx.NOT_FOUND:
            return
        selection += inc
        if selection < 0:
            return
        a = self._gridOrder.GetString(selection)
        b = self._gridOrder.GetString(selection - inc)
        self._gridOrder.SetString(selection, b)
        self._gridOrder.SetString(selection - inc, a)
        self._gridOrder.SetSelection(selection)
        self.OnGridOrderChanged()


    ###########################################################
    def OnGridMoveUp(self, event=None):
        """ Move a selected grid order item up one level. """
        self.OnGridMove(-1)


    ###########################################################
    def OnGridMoveDown(self, event=None):
        """ Move a selected grid order item down one level. """
        self.OnGridMove(1)


    ###########################################################
    def _setGridPrefs(self):
        """ Store the current grid view settings.
        """
        rows = self._gridRowsComboBox.GetValue()
        cols = self._gridColsComboBox.GetValue()
        fps = self._gridFpsComboBox.GetValue()
        order = self._gridOrder.GetStrings()
        showInactiveMode = 0 if not self._gridShowInactiveCheckbox.GetValue() else \
                           1 if not self._gridMoveInactiveCheckbox.GetValue() else \
                           2

        setFrontEndPref("gridViewShowInactive", showInactiveMode)
        setFrontEndPref("gridViewRows", int(rows))
        setFrontEndPref("gridViewCols", int(cols))
        setFrontEndPref("gridViewOrder", order)
        setFrontEndPref("gridViewFps", int(fps))
        self._logger.info(
            "saved grid settings: %sx%s, fps=%s, order=(%s)" %
            (rows, cols, fps, ",".join(order)))
        self._uiPrefsModel.updateGridViewSettings(
            rows, cols, order, fps, showInactiveMode)


    ###########################################################
    def _validateAndSetRemoteSettings(self, closing=True):
        """Ensure the remote settings are valid, will display error UI if not.

        @return valid  True if valid, else false.
        """
        if not self._webAllowed:
            return True

        # If web is not enabled, no real validation needed.
        if not self._webEnableCheckbox.GetValue():
            if self._origWebEnabled:
                self._backEndClient.setWebPort(-1)
                self._origWebEnabled = False
                self._userField.SetValue("")
                self._passField.SetValue("")
                self._verifyField.SetValue("")
                self._origWebUser = ""
                self._origPassword = ""
                self._webEnableCheckbox.SetValue(False)
            return True

        updateUser = False
        updatePort = False

        newUser = self._userField.GetValue()
        newPass = self._passField.GetValue()

        if 0 == len(newUser):
            wx.MessageBox(_kInvalidUsernameBody, _kErrorTitle,
                    wx.OK | wx.ICON_ERROR, self)
            return False
        if 0 == len(newPass):
            wx.MessageBox(_kPasswordsLengthBody, _kErrorTitle,
                    wx.OK | wx.ICON_ERROR, self)
            return False

        if not all(ord(c) < 128 for c in newUser) or \
           not all(ord(c) < 128 for c in newPass):
            wx.MessageBox(_kNonAsciiBody, _kErrorTitle,
                    wx.OK | wx.ICON_ERROR, self)
            return False

        if (self._origWebUser != newUser) or (self._origPassword != newPass):
            if not len(newUser):
                wx.MessageBox(_kInvalidUsernameBody, _kErrorTitle,
                        wx.OK | wx.ICON_ERROR, self)
                return False
            if len(newPass) < _kMinPasswordLength:
                wx.MessageBox(_kPasswordsLengthBody, _kErrorTitle,
                        wx.OK | wx.ICON_ERROR, self)
                return False
            if newPass != self._verifyField.GetValue():
                wx.MessageBox(_kPasswordsMatchBody, _kErrorTitle,
                        wx.OK | wx.ICON_ERROR, self)
                return False
            updateUser = True

        newPort = 0
        try:
            newPort = int(self._portCtrl.GetValue())
            if (newPort <= 1024) or (newPort > 65535):
                raise Exception()
            if newPort != self._currentPort:
                if not self._portCheck(newPort):
                    wx.MessageBox(_kPortNABody % newPort, _kErrorTitle,
                                  wx.OK | wx.ICON_ERROR, self)
                    return False
                updatePort = True
        except Exception:
            wx.MessageBox(_kPortErrorBody, _kErrorTitle, wx.OK | wx.ICON_ERROR,
                    self)
            return False

        if updateUser or updatePort:
            self._updateStatus(kWebServerStatusUpdating)
            self._applyButton.Enable(False)
        if updateUser:
            self._backEndClient.setWebAuth(newUser, newPass)
        if updatePort:
            self._backEndClient.setWebPort(newPort)

        self._backEndClient.enablePortOpener(
            self._portOpenerEnabled.GetValue())

        self._origWebEnabled = True
        self._origPassword = newPass
        self._origWebUser = newUser

        return True

    ###########################################################
    def OnMoveVideo(self, event=None):
        """Display the move video dialog.

        @param  event  The button event.
        """
        if not os.path.isdir(os.path.join(
                    self._backEndClient.getVideoLocation(), kVideoFolder)):
            # If the current video folder can't be found show the locate dialog.
            dlg = LocateVideoDialog(self, self._backEndClient, self._dataMgr)
        else:
            # If the current video folder does exist show the move dialog.
            dlg = MoveVideoDialog(self, self._backEndClient, self._dataMgr,
                                  self._logger)
        try:
            result = dlg.ShowModal()
            if result == wx.OK:
                volumeName = "Unknown"
                volumeType = "Unknown"
                bytesFree = -1
                try:
                    videoLocation = self._backEndClient.getVideoLocation()
                    volumeName, volumeType = getVolumeNameAndType(videoLocation)
                    bytesFree = getDiskSpaceAvailable(videoLocation)
                except Exception:
                    pass
                self._locLabel.SetLabel(ensureUnicode(_kVideoLocLabel %
                                        (volumeType, volumeName)))
                sizeStr = getStorageSizeStr(bytesFree)
                self._spaceFree.SetLabel(ensureUnicode(_kDiskSpaceLabel %
                                         (volumeType, volumeName, sizeStr)))
                self.Fit()
        finally:
            dlg.Destroy()


    ###########################################################
    def OnWebServerStatus(self, event=None):
        if self._webAllowed:
            self.showWebServerStatus(event.GetStatus())


    ###########################################################
    def _updateStatus(self, status, localURL="", internalURL="",
            externalLabel="", externalURL="", certificateId = ""):
        """Update the web server status.

        @param  status        Text describing the web server status.
        @param  localURL      A URL to use for machine local access.
        @param  internal      A URL to use for local network access.
        @param  externalLabel A url or status message for external access.
        @param  externalURL   A URL to use for external access.
        @param  certificateId SHA-1 of the SSL certificate, in hex.
        """
        if not self._webAllowed:
            return

        certificateIdShort = ''
        certificateIdLong = ''
        if certificateId:
            certificateId = certificateId.upper()
            for i in range(0, len(certificateId)//2):
                certificateIdLong += \
                    '\n' if i == len(certificateId)//4 else ':' if i else ''
                certificateIdLong += certificateId[i*2:i*2+2]
            certificateIdShort = certificateIdLong[0:23]

        self._webServerStatusNotice.SetLabel(status)
        self._webServerLocalLink.SetLabel(localURL)
        self._webServerLocalLink.SetURL(localURL)
        self._webServerInternalLink.SetLabel(internalURL)
        self._webServerInternalLink.SetURL(internalURL)
        self._webServerExternalLink.SetLabel(externalLabel)
        self._webServerExternalLink.SetURL(externalURL)
        self._webServerCertificateId.SetLabel(certificateIdShort)
        self._webServerCertificateId.SetToolTip(wx.ToolTip(certificateIdLong))
        self._localSizer.Layout()
        self._internalSizer.Layout()
        self._externalSizer.Layout()
        self._applyButton.Enable()


    ###########################################################
    def showWebServerStatus(self, wss):
        """ Processes the web server status and displays it. Checks the status
        number and compares it to the last one to avoid unnecessary updates.
        @param wss The web server status.
        """
        if not self._webAllowed:
            return

        status, locu, intu,  = "", "", ""
        externalLabel, externalURL = "", ""
        certificateId = ""
        while True:
            if wss is None:
                status = kWebServerStatusNA
                break
            snum = wss[kStatusKeyNumber]
            if self._lastStatusNumber == snum:
                return
            self._lastStatusNumber = snum;
            self._currentPort = wss[kStatusKeyPort]
            if -1 == self._currentPort:
                status = kWebServerStatusOff
                break
            if not wss[kStatusKeyVerified]:
                status = kWebServerStatusNotVerified
                break
            status = kWebServerStatusOn
            locu = kHttpAddress % (kHttpLocalHost, self._currentPort)
            intIP = self._internalIP.result
            if intIP is None:
                intu = locu # better than showing nothing, no?
            else:
                intu = kHttpAddress % (intIP, self._currentPort)
            pos = wss.get(kStatusKeyPortOpenerState, None)
            if pos is None:
                externalLabel = kWebServerExtStatusOff
                externalURL = kRemoteAccessUrl
            else:
                rport = wss[kStatusKeyRemotePort]
                raddr = wss[kStatusKeyRemoteAddress]
                if -1 == rport:
                    externalLabel = kWebServerExtStatusNA
                    externalURL = kRemoteAccessUrl
                else:
                    externalLabel = externalURL = kHttpAddress % (raddr, rport)
            certificateId = wss[kStatusKeyCertificateId]
            break

        self._updateStatus(status, locu, intu, externalLabel, externalURL,
                           certificateId)


    ###########################################################
    def _portCheck(self, port):
        """ Checks if a server port can be opened by just opening it ourselves
        and then immediately releasing it.
        @param port The port to check.
        @return True if the port can be used or not (False).
        """
        if not self._webAllowed:
            return

        tm = time.time()
        try:
            TCPServer(('0.0.0.0', port), BaseRequestHandler).server_close()
            return True
        except:
            self._logger.warn("check for port %d failed (%s)" %
                              (port, sys.exc_info()[1]))
            return False
        finally:
            self._logger.info("check for port %d took %.3f seconds" %
                              (port, time.time() - tm))


###############################################################
class AdvancedDialog(wx.Dialog):
    """A dialog for advanced remote access settings."""

    ###########################################################
    def __init__(self, parent, backEndClient, logger):
        """Initializer for AdvancedDialog.

        @param  parent         The parent window.
        @param  backEndClient  An object for communicating with the back end.
        """
        wx.Dialog.__init__(self, parent, -1, "Advanced")

        try:
            self._backEndClient = backEndClient
            self._logger = logger

            # Create the main sizer.
            sizer = wx.BoxSizer(wx.VERTICAL)
            self.SetSizer(sizer)
            sizer.AddSpacer(_kSpaceSize2)

            # Create the controls.

            clipLabel = wx.StaticText(self, -1, "Clip Video Quality:")
            self._clipChoices = wx.Choice(self, -1, choices=_kVideoQualityProfileLabels)
            self._clipChoices.Bind( wx.EVT_CHOICE, self.OnClipChoiceSelection )

            liveResLabel = wx.StaticText(self, -1, "Maximum Live Video Resolution:")
            self._liveResChoices = wx.Choice(self, -1, choices=_kVideoResolutionLabels)

            clipResLabel = wx.StaticText(self, -1, "Maximum Clip Video Resolution:")
            self._clipResChoices = wx.Choice(self, -1, choices=_kVideoResolutionLabels)

            self._origLiveTimestampEnabled = self._backEndClient.getVideoSetting(Prefs.kLiveEnableTimestamp)
            self._timestampEnabledForLiveViewCheckbox = wx.CheckBox(self, -1,
                                        "Overlay timestamp on live view")
            self._timestampEnabledForLiveViewCheckbox.SetValue(self._origLiveTimestampEnabled)

            self._origEnableLiveFastStart = self._backEndClient.getVideoSetting(Prefs.kLiveEnableFastStart)
            self._fastStartEnabledCheckbox = wx.CheckBox(self, -1,
                                        "Enable live stream fast start (may consume more memory)")
            self._fastStartEnabledCheckbox.SetValue(self._origEnableLiveFastStart)

            self._origClipsTimestampEnabled = self._backEndClient.getTimestampEnabledForClips()
            self._timestampEnabledForClipsCheckbox = wx.CheckBox(self, -1,
                                        "Overlay timestamp on clips")
            self._timestampEnabledForClipsCheckbox.SetValue(self._origClipsTimestampEnabled)
            self._origBoundingBoxesEnabled = self._backEndClient.getBoundingBoxesEnabledForClips()
            self._boundingBoxesEnabledCheckbox = wx.CheckBox(self, -1,
                                        "Overlay bounding boxes on clips")
            self._boundingBoxesEnabledCheckbox.SetValue(self._origBoundingBoxesEnabled)

            hSizer = wx.BoxSizer(wx.HORIZONTAL)
            hSizer.Add(liveResLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                    _kPaddingSize)
            hSizer.Add(self._liveResChoices, 0, wx.ALIGN_CENTER_VERTICAL)
            sizer.Add(hSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kPaddingSize*2)
            sizer.Add(self._timestampEnabledForLiveViewCheckbox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kPaddingSize*2)
            sizer.Add(self._fastStartEnabledCheckbox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)

            sizer.AddSpacer(_kPaddingSize*8)

            hSizer = wx.BoxSizer(wx.HORIZONTAL)
            hSizer.Add(clipLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                    _kPaddingSize)
            hSizer.Add(self._clipChoices, 0, wx.ALIGN_CENTER_VERTICAL)
            sizer.Add(hSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kPaddingSize*2)

            hSizer = wx.BoxSizer(wx.HORIZONTAL)
            hSizer.Add(clipResLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                    _kPaddingSize)
            hSizer.Add(self._clipResChoices, 0, wx.ALIGN_CENTER_VERTICAL)
            sizer.Add(hSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kPaddingSize*2)

            sizer.Add(self._boundingBoxesEnabledCheckbox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kPaddingSize*2)

            sizer.Add(self._timestampEnabledForClipsCheckbox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kSpaceSize2)
            sizer.AddSpacer(_kPaddingSize*2)

            buttonSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            sizer.Add(buttonSizer, 0, wx.BOTTOM | wx.EXPAND, _kSpaceSize1)

            self.FindWindowById(wx.ID_OK, self).Bind(wx.EVT_BUTTON, self.OnOk)
            self.FindWindowById(wx.ID_CANCEL, self).SetDefault()

            self.Fit()
            self.CenterOnParent()

            self._videoSettings = {
                #                            orig default                               control                 values
                Prefs.kClipQualityProfile: ( -1,  Prefs.kClipQualityProfileDefault,     self._clipChoices,      _kVideoQualityProfile ),
                Prefs.kLiveMaxResolution : ( -1,  Prefs.kLiveMaxResolutionDefault,      self._liveResChoices,   _kVideoResolutions    ),
                Prefs.kClipResolution    : ( -1,  Prefs.kClipResolutionDefault,         self._clipResChoices,   _kVideoResolutions    )
            }

            # Fetch current prefs and set controls
            for key in self._videoSettings:
                self._initVideoQualitySetting(key)
            self.OnClipChoiceSelection(None)

        except: # All exceptions, not just Exception subclasses
            # Make absolutely sure that we are destroyed, even if we crash
            # in the above...
            self.Destroy()
            raise

    ###########################################################
    def _initVideoQualitySetting(self, name):
        current = self._backEndClient.getVideoSetting(name)
        original, default, ctrl, options = self._videoSettings[name]
        if current not in options:
            current = default
        ctrl.SetSelection(options.index(current))
        self._videoSettings[name] = ( current, default, ctrl, options )

    ###########################################################
    def _saveVideoQualitySetting(self, name):
        original, default, ctrl, options = self._videoSettings[name]
        current = ctrl.GetSelection()
        if current != original:
            self._backEndClient.setVideoSetting(name, options[current])

    ###########################################################
    def OnClipChoiceSelection(self, event=None):
        current = self._clipChoices.GetSelection()
        enableFilters = (_kVideoQualityProfile[current] != 0)
        self._boundingBoxesEnabledCheckbox.Enable( enableFilters )
        self._timestampEnabledForClipsCheckbox.Enable( enableFilters )
        self._clipResChoices.Enable( enableFilters )

    ###########################################################
    def OnOk(self, event=None):
        """Close the dialog applying any changes.

        @param  event  The button event.
        """
        # If any changes, propagate to back end.
        for key in self._videoSettings:
            self._saveVideoQualitySetting(key)

        val = self._boundingBoxesEnabledCheckbox.GetValue()
        if val != self._origBoundingBoxesEnabled:
            self._backEndClient.setBoundingBoxesEnabledForClips(val)

        val = self._timestampEnabledForClipsCheckbox.GetValue()
        if val != self._origClipsTimestampEnabled:
            self._backEndClient.setTimestampEnabledForClips(val)

        val = self._timestampEnabledForLiveViewCheckbox.GetValue()
        if val != self._origLiveTimestampEnabled:
            self._backEndClient.setVideoSetting(Prefs.kLiveEnableTimestamp, val)

        val = self._fastStartEnabledCheckbox.GetValue()
        if val != self._origEnableLiveFastStart:
            self._backEndClient.setVideoSetting(Prefs.kLiveEnableFastStart, val)

        self.EndModal(wx.OK)


