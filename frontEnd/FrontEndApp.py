#!/usr/bin/env python

#*****************************************************************************
#
# FrontEndApp.py
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
import os
import re
import shutil
from subprocess import Popen, PIPE
import sys
import tempfile
import time

# When this module started loading.  Everything below it -- wx, and everything
# it drags in -- is import cost the user waits through with nothing on screen,
# so the clock has to start before any of it.  This is as close to process
# start as our own code can observe.
_kAppStartTime = time.time()

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.mvc.AbstractModel import AbstractModel
from vitaToolbox.windows.winUtils import registerForForcedQuitEvents
from vitaToolbox.windows.winUtils import setAppUserModelId
from vitaToolbox.wx.LookForOtherInstances import lookForOtherInstances
from vitaToolbox.sysUtils.FileUtils import safeRemove


# Local imports...
from .BackEndClient import BackEndClient

from appCommon.CommonStrings import kAppName
from appCommon.CommonStrings import kWindowsAppUserModelId
from appCommon.CommonStrings import kLegacyAppName
from appCommon.CommonStrings import kFrontEndLogName
from appCommon.CommonStrings import kMaxRecordSize, kMatchSourceSize
from appCommon.CommonStrings import kBackendMarkerArg
from appCommon.CommonStrings import kReservedMarkerArg
from appCommon.CommonStrings import kMemStoreBackendReady
from appCommon.CommonStrings import kDefaultRecordSize
from appCommon.LicenseUtils import hasPaidEdition
# NOTE: appCommon.DbRecovery is imported inside PostInit() too, and for the
# same reason as FrontEndFrame: it reaches backEnd.ClipManager ->
# videoLib2.python.ClipReader, which imports cv2, numpy and PIL at module
# scope.  That is most of a second of loading video codecs before we are even
# allowed to draw a window, for the sake of a database check that happens well
# after the window is up.

try:
    from BugReportDialog import BugReportDialog
    kHasBugReportDialog = True
except:
    kHasBugReportDialog = False
from .GetLaunchParameters import getLaunchParameters

# NOTE: FrontEndFrame is imported inside PostInit(), not here.  Importing it
# pulls in every view, and through backEnd.DataManager it pulls in cv2, numpy
# and PIL, and through MonitorView/GridView it pulls in OpenGL -- none of which
# is needed to put a window on screen.  Loading it at module scope meant the
# user waited through all of it before anything could be drawn.  It is also
# safer late: several backEnd modules resolve the data directory at import
# time, and PostInit knows the data directory by the time it imports them.
from .StartupWindow import StartupWindow
from .FrontEndPrefs import getFrontEndPref, setFrontEndPref
from .FrontEndUtils import getUserLocalDataDir
from appCommon.LegacyMigration import getLegacyImportSource
from appCommon.LegacyMigration import importLegacyData
from appCommon.LegacyMigration import markLegacyImportChecked
from .FrontEndUtils import getServiceStartsBackend
from .FrontEndUtils import getRemotePathsFromSettings
from .FrontEndUtils import setServiceStartsBackend, setServiceAutoStart

from .LicensingHelpers import getLicense

from launch.Launch import serviceAvailable
from launch.Launch import Launch
from launch.Launch import launchCheckMac
from launch.Launch import launchCheckWin
from launch.Launch import launchLog


# Constants...

# NOTE: there is no start-dialog delay any more.  We used to wait 5 seconds
# before putting anything on screen, and even then only if some retry loop
# happened to call Pulse() -- so on a fast connect the user saw nothing at all
# until the main window appeared.  StartupWindow now goes up immediately.

# ...we'll wait this many seconds to connect to the backend after starting it...
_kConnectTimeout = 90

# ...we'll wait this many seconds for to backend to be operationally ready...
_kReadyTimeout = 15

# ...when we're testing for an already running backend, we'll wait this long...
# Be generous here.  This decides whether we treat the back end as dead, and
# on an installed build "dead" means we ask the service to restart it and then
# block for up to _kConnectTimeout waiting for the replacement.  A healthy but
# briefly busy back end failing a one-second probe used to cost the user the
# entire restart cycle on every launch.
_kTestConnectTimeout = 5

# Socket timeout for a SINGLE back-end probe (see _handleOldBackends).  A live
# local back end answers in milliseconds, so this only bounds the case where
# nothing is listening -- which is what lets the poll actually honour
# _kTestConnectTimeout instead of running ~40x longer than it says.
_kProbeConnectTimeout = 0.5

# We wait this many seconds for a normal quit to take effect...
_kQuitTimeout = 15

# We wait this many seconds for a force quit to take effect...
_kForceQuitTimeout = 3

# Used to describe the build # of the app...
_kBuildTemplateStr    = "Build %s"

# The file that build info is stored in.
_kBuildFile = "build.txt"

# Number of seconds to wait before giving up on license data to get loaded.
_kLicenseLoadTimeout=30


# Offer to move data...
_kMoveDataOffer = (
'''Would you like to import your existing data into Sighthound Video?'''
'''\n\n'''
'''This data will no longer be accessible by Sighthound Video. Please '''
'''first ensure that no Sighthound Video processes are running before '''
'''continuing, and make a backup of your existing data.'''
)
_kMoveDataOfferTitle = "Sighthound Video installation detected"

_kMoveFailedText = (
'''Couldn't move existing data.'''
'''\n\n'''
'''This might be because a file is open in another application.'''
'''Hit OK to try again, or Cancel to exit the application.\n\n'''
'''Message: %s'''
)
_kMoveFailedTitle = "Couldn't move"

_kVideoMoveFailedText = (
'''Couldn't move existing video files.'''
'''\n\n'''
'''Sighthound Video could not migrate the existing video files. Hit OK '''
'''to continue launching the application and begin from scratch, or Cancel '''
'''to exit and attempt to manually back up existing video files. They may '''
'''be located at "%s" or "%s".\n\n'''
'''Message: %s'''
)
_kVideoMoveFailedTitle = "Couldn't move"

_kStartError = \
"The application could not be started.\nPlease wait a minute and try again."

_kServiceMissingWin32 = \
"""The service is not running.\nPlease check the Sighthound Video Launch """
"""entry in the 'Services' section of """
"""the Control Panel, or reinstall the application."""

_kRunningFromContainer = \
"%s cannot run from a container, or any external storage" % kAppName

_kServiceMissingMac = \
"""The service is not running.\nPlease reinstall the application."""

_kCrashTitle = "Error Report"
_kLaunchError = ('''It appears that %s previously had trouble starting. '''
'''Would you like to submit the error for analysis?''' % kAppName)
_kCrashError = ('''It appears that %s experienced a crash. Would you like '''
'''to submit the error for analysis?''' % kAppName)


_kDbReset = "Resetting databases ..."
_kDbRecovery = "Recovering databases ..."
_kDbRecoveryProgress = "Recovering databases (%d%%) ..."
_kDbRecoveryPollSecs = .5

_kDefaultStartDialogMessage = "Just a moment..."

# What the startup window says while each phase runs.  These exist because the
# phases they name used to run with nothing at all on screen -- the difference
# between "this app is broken" and "this app is working" is mostly just saying
# out loud what is happening.
_kCheckingServiceMessage = "Checking the Sighthound Video service..."
_kCheckingBackendMessage = "Checking for a running video engine..."
_kPreparingDataMessage   = "Preparing your data folder..."
_kImportingLegacyMessage = "Importing your existing settings..."
_kStartingBackendMessage = "Starting the video engine..."
_kConnectingMessage      = "Connecting to the video engine..."
_kWaitingReadyMessage    = "Waiting for your cameras..."
_kCheckingLicenseMessage = "Checking your license..."
_kLoadingWindowMessage   = "Loading the main window..."

# How long to wait for the service to get ready after activation steps.
_kLaunchCheckTimeout = 10


##############################################################################
def _logStartupMark(logger, label):
    """Log how far into startup we are.

    There was no startup instrumentation anywhere in the tree, and the numbers
    we actually need come from installed machines we cannot attach a profiler
    to -- so the log is the measurement.  Times are measured from the top of
    this module, which is as close to process start as our own code can see;
    the interpreter's own boot is not included.

    @param  logger  The logger to write to.
    @param  label   What just finished.
    """
    try:
        logger.info("startup: %s at %dms", label,
                    int((time.time() - _kAppStartTime) * 1000))
    except Exception:
        # Instrumentation must never be the reason a launch fails.
        pass


##############################################################################
class FrontEndApp(wx.App):
    """The main application class for the front end gui."""
    ###########################################################
    def __init__(self, logger):
        """FrontEndApp constructor.

        @param  logger  Our logger.
        """
        self._logger = logger

        # Call the superclass constructor.  This will call OnInit().
        wx.App.__init__(self, redirect=False)

        self._debugModeModel = _DebugModeModel()

    ###########################################################
    def OnInit(self):
        """Init the application for wx.App to function correctly.

        This is called by our superclass's constructor, and is generally
        considered the place to create the main window (called a Frame) for
        the app.  However, we do not create the Frame here.  There are many
        checks that take place before the Frame is created.  Some of those
        checks involve showing message boxes/dialogs to the user.  If a popup
        is shown to the user during OnInit, wx.App will forcefully close it
        because it hasn't finished initializing. So we just set the app's name
        and register the callback function for forced quit events here. After
        initialization, we must manually call PostInit where warnings and info
        popups can be shown to the user, and where the Frame may be created and
        shown.

        @return success  True if the init was successful. If False is returned,
                         wx.App will close the python interpreter.
        """
        # Set our app name before doing anything else, so that our wx paths
        # get set properly...
        self.SetAppName(kAppName)

        self.__callbackFunc = registerForForcedQuitEvents()

        self._closingApp = False
        self._closedWins = []
        self._startDlg = None
        self._foundLiveBackend = False

        return True


    ###########################################################
    def _destroyStartupWindow(self):
        """Take the startup window down, if it is still up.

        Idempotent, because PostInit destroys it explicitly once the real frame
        is showing and then again from its finally clause on the way out.
        """
        if self._startDlg is not None:
            try:
                # Hide before Destroy: wx defers the actual delete to idle
                # time, so without this the startup window can sit on top of
                # the main one until the event loop gets a turn.
                self._startDlg.Hide()
                self._startDlg.Destroy()
            except Exception:
                self._logger.warning("could not destroy the startup window",
                                     exc_info=True)
            self._startDlg = None


    ###########################################################
    def PostInit(self):
        """Init the rest of the application.

        This must be called manually directly _after_ this class has been
        instantiated. This is where warning and info popups can be shown to the
        user, and where the Frame is created and shown.

        @return success  True if the post-init was successful
        """
        isOSX = sys.platform == "darwin"

        if isOSX and \
           (sys.argv[0].startswith("/Volumes/") or sys.argv[0].startswith("/private/")):
            wx.MessageBox(_kRunningFromContainer, "Error",
                          wx.ICON_ERROR | wx.OK, None)
            return False

        # Check for other instances, on Windows. Mac inherently
        # prevents multiple instances of an app from running.
        #
        # This goes first, ahead of the service check, so a duplicate launch
        # never flashes a startup window before bowing out.  It depends on
        # nothing below it.  Note that it can now do its job properly: it
        # raises the other instance by looking for a window titled with the app
        # name, and until StartupWindow existed there was no such window during
        # startup -- so a second launch simply vanished, which is exactly what
        # an impatient user does when the first launch shows nothing.
        if (wx.Platform == "__WXMSW__"):
            self._singleInstanceChecker = lookForOtherInstances()
            if self._singleInstanceChecker is None:
                return True

        # Get a window on screen before doing anything slow.  Everything from
        # here to frame.Show() used to run with nothing visible; now each phase
        # below says what it is doing, and this window stays up until the real
        # frame replaces it.
        self._startDlg = StartupWindow(_kDefaultStartDialogMessage)
        self._startDlg.showAndPaint()
        _logStartupMark(self._logger, "startup window shown")

        ready = False
        try:
            if serviceAvailable():
                self._startDlg.Pulse(_kCheckingServiceMessage)
                try:
                    if isOSX:
                        buildStr = wx.GetApp().getAppBuildStr()
                        m = re.search('[0-9]+', buildStr)
                        if not m:
                            launchLog("no build number found in '%s'" % buildStr)
                            return False
                        build = "r%s" % m.group()
                        legacyDataDir = wx.StandardPaths.Get().GetUserLocalDataDir()
                        legacyDataDir = os.path.join(os.path.dirname(legacyDataDir),
                                                     kAppName)
                        checked = False
                        try:
                            checked = launchCheckMac(build, legacyDataDir,
                                                     _kLaunchCheckTimeout)
                        except:
                            pass
                        if not checked:
                            wx.MessageBox(_kServiceMissingMac, "Error",
                                          wx.ICON_ERROR | wx.OK, None)
                            return False
                    else:
                        if not launchCheckWin():
                            wx.MessageBox(_kServiceMissingWin32, "Error",
                                          wx.ICON_ERROR | wx.OK, None)
                            return False
                except:
                    launchLog("UNCAUGHT LAUNCH CHECK ERROR (%s)" %
                              sys.exc_info()[1])
                    return False

            if isOSX:
                # We once used a hack to enable Retina support on the older wx
                # we were using. Not necessary anymore, but we want to make
                # sure to clean up our old stuff to avoid any possible trouble.
                #
                # TODO: remove eventually ...
                #
                try:
                    retinaPatch = os.path.join(os.path.expanduser("~"), "Library",
                        "Preferences", "com.sighthound.sighthoundvideo.plist")
                    if os.path.exists(retinaPatch):
                        os.remove(retinaPatch)
                except:
                    pass

            # Quit any old copies of the backend that are running
            self._startDlg.Pulse(_kCheckingBackendMessage)
            isRightVersionRunning = self._handleOldBackends()
            _logStartupMark(self._logger, "old back ends handled")

            # Offer to move the user's data directory if they are upgrading
            # from VDV to Sighthound Video. Must happen after setting app name
            # but before we do anything with the data directory...
            if not self._offerMoveDataDir():
                return False

            # Ensure we have the correct build file for the running app.
            self._copyBuildFile()
            _logStartupMark(self._logger, "build file copied")

            # Now that we have an app name, we can find our data dir and point
            # our logger there.
            self._startDlg.Pulse(_kPreparingDataMessage)
            userLocalDataDir = getUserLocalDataDir()
            logDir = os.path.join(userLocalDataDir, "logs")
            self._logger.setLogDirectory(logDir)
            self._logger.enableDiskLogging()
            _logStartupMark(self._logger, "data directory resolved")

            # One-time upgrade: if this is a fresh Py3 install and a previous
            # (Python 2) install is present, offer to import its cameras and
            # rules before anything reads config.  Never blocks launch.
            try:
                if getLegacyImportSource(userLocalDataDir) is not None:
                    self._startDlg.Pulse(_kImportingLegacyMessage)
                    if self._offerLegacyImport():
                        importLegacyData(userLocalDataDir, self._logger)
                    else:
                        self._logger.info("User chose a fresh install; "
                                          "skipping legacy import")
                    # Remember the choice either way so we don't ask again.
                    markLegacyImportChecked(userLocalDataDir)
            except Exception:
                self._logger.warning("Legacy data import failed", exc_info=True)

            # If the database got corrupted we ask the user for consent first.
            #
            # Imported here rather than at module scope: this reaches
            # videoLib2's ClipReader and so loads cv2, numpy and PIL.  None of
            # that is needed to put a window on screen, and at module scope the
            # user waited through it before anything could be drawn.
            from appCommon.DbRecovery import getCorruptDatabaseStatus
            from appCommon.DbRecovery import setCorruptDatabaseStatus
            from appCommon.DbRecovery import kStatusRecover
            from appCommon.DbRecovery import kStatusReset
            _logStartupMark(self._logger, "database support imported")

            # The status file is a repair REQUEST now, not a damage report.
            #
            # This used to greet the user with a Recover / Reset / Cancel
            # dialog whenever the file existed -- a second, differently worded
            # database dialog on top of the one the back end raises while the
            # app is running, offering a Reset that deletes every clip.  Damage
            # is reported once, from evidence, by the back end
            # (msgIdDatabaseDamaged), and the repair is accepted there.  All
            # that is left to do here is notice which repair was asked for, so
            # the progress messages below say the right thing.
            databaseResetOnly = False
            dbStatus = getCorruptDatabaseStatus(userLocalDataDir, self._logger)
            if dbStatus:
                self._logger.info("DB status file says %s" % str(dbStatus))
                if dbStatus[0] in (kStatusRecover, kStatusReset):
                    databaseResetOnly = (dbStatus[0] == kStatusReset)
                else:
                    # A detection marker written by an older version, which
                    # reported damage by dropping this file.  Nothing writes it
                    # that way any more.  Drop it and let the back end's
                    # startup integrity check rule on the evidence: if a
                    # database really is damaged it raises the flag and the
                    # user gets the one dialog, with a repair they can accept.
                    self._logger.info(
                        "clearing a legacy corruption marker; the back end "
                        "will re-check the databases on the evidence")
                    setCorruptDatabaseStatus(None, userLocalDataDir,
                                             self._logger)

            self._logger.info("Starting %s..." % kAppName)

            # On Mac built app, redirect stdout/stderr from C modules to
            # /dev/null so that they don't pollute the console.  This matches
            # Windows and seems like the best we can come up with for now.
            # See bug #503.
            frozen = hasattr(sys, "frozen")
            if frozen and (wx.Platform == "__WXMAC__"):
                self._cStdStreams = open("/dev/null", "a")
                os.dup2(self._cStdStreams.fileno(), 1)
                os.dup2(self._cStdStreams.fileno(), 2)

            def showStartError():
                wx.MessageBox(_kStartError, "Error", wx.ICON_ERROR|wx.OK, None)

            # For now, service on Windows is unable to reach any network paths
            # (whether it is by UNC or letter drive), so if any of the settings
            # contain network paths, we MUST _NOT_ allow the service to start
            # the backend. If the settings do not contain any network paths,
            # then we explicitly set the configuration file to allow service to
            # start the backend.
            serviceStartsBackend = getServiceStartsBackend()
            if frozen:
                netPathsUsed = getRemotePathsFromSettings()
                isSetSVCConfig = setServiceStartsBackend(not netPathsUsed)

                if netPathsUsed:
                    serviceStartsBackend = False

                # Disable autostart if service will not start the backend on
                # this run...
                if not serviceStartsBackend:
                    setServiceAutoStart(False)

                self._logger.info(
                    "Service %s launch the backend, because network paths %s "
                    "found in the settings: %s",
                    "should" if (not netPathsUsed) else "should NOT",
                    "were" if netPathsUsed else "were NOT",
                    netPathsUsed
                )

                if isSetSVCConfig:
                    self._logger.info("Service config file was set successfully...")
                else:
                    self._logger.error("Service config file could not be set!!!")

            if not isRightVersionRunning:
                self._startDlg.Pulse(_kStartingBackendMessage)

                # To run from source we still need to support the 'old' way of
                # launching the back-end, through forking for OSX that is ...
                if not serviceStartsBackend and wx.Platform == '__WXMAC__':
                    self._logger.info("About to fork back-end ...")
                    pid = os.fork()
                else:
                    pid = 0

                # If the backend runs as a service we just signal it to
                # (re)start everything ...
                if serviceStartsBackend:
                    self._logger.info("signaling service...")
                    launch = Launch()
                    if not launch.open():
                        self._logger.error("cannot connect to launch service")
                        showStartError()
                        return False

                    # killFirst asks the service to tear the back end down
                    # before starting it (kControlRestartBackend rather than
                    # kControlStartBackend).  Only ask for that if
                    # _handleOldBackends() actually found a back end to tear
                    # down.  It always used to, and on the common path there is
                    # nothing running -- so every launch paid for a needless
                    # restart and then blocked below for up to _kConnectTimeout
                    # waiting for the replacement to come up.  This is only
                    # reached when the running back end was the wrong version
                    # (already quit above) or when none answered at all.
                    killFirst = self._foundLiveBackend
                    self._logger.info("signaling service (killFirst=%s)",
                                      killFirst)
                    launchResult = launch.do(killFirst=killFirst)
                    launch.close()
                    if launchResult is None:
                        self._logger.error("launch via service failed")
                        showStartError()
                        return False
                    else:
                        self._logger.info("launch initiated (x%08x, x%08x)" %
                                          launchResult)
                # If src/win or the forking child on src/mac...
                elif pid == 0:
                    self._logger.info("launching backend...")
                    openParams = getLaunchParameters()
                    openParams.extend(["--backEnd",
                                       userLocalDataDir if isinstance(userLocalDataDir, str) else userLocalDataDir.decode('utf-8'),
                    # mark the backend progress via an argument which is not
                    # used directly but picked up through process enumeration
                    # where we examine the command lines ...
                                       kBackendMarkerArg,
                    # this parameter is a placeholder, as a matter of fact it
                    # actually vanishes when we spawn a web server, as if there
                    # is a bug always dropping the last command line argument;
                    # thus it guarantees that the actual backend marker will
                    # make it and can then be replaced by nginx to the other
                    # marker value (kNginxMarkerArg) ...
                                       kReservedMarkerArg])

                    # I'm not sure if all of the closing of FDs is all that
                    # important on Mac now that we're running from a fork, but
                    # I don't think it hurts...
                    self._logger.disableDiskLogging()
                    _launchCwd = os.path.dirname(os.path.abspath(__file__))
                    _launchCwd = os.path.dirname(_launchCwd)  # up from frontEnd/ to root
                    subProc = Popen(openParams, stdin=PIPE, stdout=PIPE,
                                    stderr=PIPE, cwd=_launchCwd,
                                    close_fds=(wx.Platform == "__WXMAC__"))
                    self._logger.enableDiskLogging()
                    subProc.stdin.close()
                    subProc.stdout.close()
                    subProc.stderr.close()

                    # On Mac, if we're the forking child, we've done our duty
                    # now that we've opened our subprocess.  Exit.  Use the
                    # special os._exit (not os.exit)
                    if wx.Platform == '__WXMAC__':
                        os._exit(0)
                else:
                    # Wait for our forked process to exit (Mac only)...
                    os.waitpid(pid, 0)

            _logStartupMark(self._logger, "back end launched")

            # Import the main window now, while the back end is booting.
            #
            # This is the expensive import in the whole app -- it pulls in
            # every view, and through backEnd.DataManager it pulls in cv2,
            # numpy and PIL, and through MonitorView/GridView it pulls in
            # OpenGL.  It used to happen at module scope, so the user waited
            # through all of it before anything could appear on screen, and it
            # ran strictly before the back end was even asked to start.  Here
            # it overlaps the back end's own startup instead, and it happens
            # after the data directory is known -- which matters, because
            # several backEnd modules resolve their paths at import time.
            from .FrontEndFrame import FrontEndFrame
            _logStartupMark(self._logger, "main window module imported")

            # If database recovery is going we need to report it.
            self._logger.info("Checking DB status...")
            while True:
                status = getCorruptDatabaseStatus(userLocalDataDir)
                if status is None:
                    msg = _kDefaultStartDialogMessage
                else:
                    time.sleep(_kDbRecoveryPollSecs)
                    if status and "progress" == status[0]:
                        if databaseResetOnly:
                            msg = _kDbReset
                        else:
                            msg = _kDbRecoveryProgress % int(status[1])
                    else:
                        msg = _kDbRecovery
                self._startDlg.Pulse(msg)
                if status is None:
                    break

            # Wait until we can connect to the NMS.
            self._logger.info("Waiting for backend connection ...")
            self._startDlg.Pulse(_kConnectingMessage)

            backEndClient = BackEndClient()
            for _ in range(_kConnectTimeout * 10):
                if backEndClient.connect():
                    break
                time.sleep(.1)
                self._startDlg.Pulse()
            _logStartupMark(self._logger, "back end connected")

            if backEndClient.isConnected():
                self._logger.info("Waiting for backend readiness ...")
                self._startDlg.Pulse(_kWaitingReadyMessage)
                try:
                    for _ in range(_kReadyTimeout * 10):
                        item = backEndClient.memstoreGet(kMemStoreBackendReady,
                                                         .1, -1)
                        if item and item[0]:
                            ready = True
                            break
                        else:
                            self._startDlg.Pulse()
                except Exception as e:
                    self._logger.error(
                        "Waiting for backend readiness failed: " + str(e))
            _logStartupMark(self._logger, "back end ready")

            if not ready and not backEndClient.isConnected():
                showStartError()
                return False

            # Assure that we have a good license.  If not, we'll bail right now.
            # NOTE: If upgrading from beta (AKA: downgrading num cameras allowed),
            # there all cameras will continue recording while this dialog is up.
            # TODO: OK?
            #
            # This blocks for up to _kLicenseLoadTimeout.  It used to run after
            # the progress dialog had already been destroyed, so it was pure
            # dead air; the startup window now stays up through it.
            self._startDlg.Pulse(_kCheckingLicenseMessage)
            lic = getLicense(backEndClient, None, _kLicenseLoadTimeout)
            _logStartupMark(self._logger, "license checked")
            if not lic:
                backEndClient.quit(_kQuitTimeout)
                backEndClient.forceQuit(_kForceQuitTimeout)
                return True

            # Check for any previous crashes and prompt user to send any
            # available logs.
            #
            # This scans the whole of %TEMP%, so it is tempting to defer until
            # after the window is up -- don't.  It maintains the launchFailures
            # counter: it increments on entry, and PostInit resets it to 0 once
            # we get as far as showing the window.  Run it after that reset and
            # a perfectly healthy launch is left looking like a failed one, and
            # two launches later the bug-report dialog fires on its own.  It
            # also has to run BEFORE the frame is built, because a crash while
            # building the frame is exactly the launch failure it is counting.
            # It is no longer dead air in any case: the startup window is up.
            self._startDlg.Pulse()
            self._checkForCrashReports(backEndClient)

            # Get and cache whether the backend was launched by service or not.
            # The reason why we do this is so that all UI components of the
            # entire app can call wx.App.Get().isBackendLaunchedByService()
            # without needing to have a reference to the backend client. This
            # value makes sense to cache here because this property of the
            # backend will not change during the lifetime of the frontend.
            self._isBackendLaunchedByService = backEndClient.launchedByService()

            self._startDlg.Pulse(_kLoadingWindowMessage)
            frame = FrontEndFrame(kAppName, backEndClient)
            _logStartupMark(self._logger, "main frame constructed")
            self.SetTopWindow(frame)

            frame.Show(True)
            _logStartupMark(self._logger, "main frame shown")

            # Only now is it safe to take the startup window away: the real
            # window is already up, so the user never sees a bare desktop in
            # between.  The finally below is just a safety net for the failure
            # paths -- _destroyStartupWindow() is idempotent.
            self._destroyStartupWindow()
            frame.Raise()

            setFrontEndPref('launchFailures', 0)

            return True

        finally:
            self._destroyStartupWindow()


    ############################################################
    def isBackendLaunchedByService(self,loop):
        """Gets whether or not the backend was launched by service.

        @return  bool  True if the backend was launched by service, and False
                       otherwise.
        """
        return self._isBackendLaunchedByService


    ############################################################
    def OnEventLoopEnter(self, loop):
        #loop = wx.EventLoopBase.GetActive()
        #print("loop=%s" % (loop,))
        if self._closingApp:
            self._CloseAppManually()


    ############################################################
    def OnEventLoopExit(self, loop):
        #loop = wx.EventLoopBase.GetActive()
        #print("loop=%s" % (loop,))
        pass


    ############################################################
    def IsAppClosingAutomatically(self):
        return self._closingApp


    ############################################################
    def CloseAppManually(self):
        if not self._closingApp:
            self._logger.info("Begin closing the app automatically...")
            self._closingApp = True
            self._CloseAppManually()


    ############################################################
    def _CloseAppManually(self):
        topWins = list(wx.GetTopLevelWindows())
        currLoop = wx.EventLoopBase.GetActive()

        if not currLoop:
            return

        # print(
        #     "currLoop=%s, numTopWins=%s, topWins=%s"
        #     % (currLoop, len(topWins), topWins)
        # )

        while len(topWins) > 0:
            topWin = topWins.pop()
            if topWin not in self._closedWins:
                self._closedWins.append(topWin)
                topWin.Close(True)
                #print("Ran close for frame or dialog...")
                break
            # else:
            #     print("IsBeingDeleted: %s" % (topWin.IsBeingDeleted(),))

        if currLoop and currLoop.IsMain():
            self._closedWins = []


    ############################################################
    def MacReopenApp(self):
        """Called when the dock icon is clicked, and ???

        I'm not sure if we actually need this. It seems to work OK without
        on 10.10 at least, but wary to remove. Fixed behavior to restore
        proper window.
        """
        # Saw self.GetTopWindow() be None in case 12035.
        focusControl = None
        if self.GetTopWindow():
            focusControl = self.GetTopWindow().FindFocus()

        # Raise the app to the foreground, attempting to ensure that our
        # highest level windowremains on top.
        windows = wx.GetTopLevelWindows()
        for win in windows:
            if focusControl and win == focusControl.GetTopLevelParent():
                win.Raise()
                return

        # If nothing had focus (common) or we can't find the focus owner
        # (not sure what would cause that)
        if windows:
            windows[len(windows)-1].Raise()


    ###########################################################
    def _offerLegacyImport(self):
        """Ask whether to import cameras/rules from a previous install.

        Shown once on a fresh install when a legacy (Python 2) "Sighthound
        Video" folder is detected.

        @return  True if the user chose to import, False to start fresh.
        """
        msg = (
            "A previous version of %s was found on this computer.\n\n"
            "Would you like to import its cameras and rules into this version?\n\n"
            "•  Your existing settings and folders will not be changed - "
            "they are only read and copied into the new settings folder.\n\n"
            "•  Videos and previously recorded detections will not be "
            "imported.\n\n"
            "Choose Import to bring over your cameras and rules, or Start Fresh "
            "to begin with a clean setup."
        ) % kLegacyAppName

        dlg = wx.MessageDialog(None, msg, "Import Previous Settings?",
                               wx.YES_NO | wx.ICON_QUESTION)
        try:
            dlg.SetYesNoLabels("Import Cameras && Rules", "Start Fresh")
            return dlg.ShowModal() == wx.ID_YES
        finally:
            dlg.Destroy()


    ###########################################################
    def _offerMoveDataDir(self):
        """Offer to move the user's data directory if they've upgraded.

        @returns  shouldRun  False if the app should abort launching.
        """
        if getFrontEndPref("vdvImportOffered"):
            return True

        dataDir = getUserLocalDataDir()

        # Look for old data in "Sighthound Video" and offer to move it...
        # Only offer migration when we are the canonical "Sighthound Video" install;
        # skip if we are a variant (e.g. "Sighthound Video Py3") to avoid
        # accidentally consuming the production install's data.
        oldAppDir = os.path.join(os.path.split(dataDir)[0], "Sighthound Video" + os.sep)
        _canonicalDir = os.path.join(os.path.split(dataDir)[0], "Sighthound Video" + os.sep)
        if os.path.isdir(oldAppDir) and os.path.normcase(dataDir + os.sep) == os.path.normcase(_canonicalDir):
            choice = wx.MessageBox(_kMoveDataOffer, _kMoveDataOfferTitle,
                                   wx.YES_NO | wx.YES_DEFAULT, None)
            if choice == wx.YES:
                while True:
                    try:
                        os.rename(oldAppDir, dataDir)
                        setFrontEndPref("vdvImportOffered", True)
                    except Exception as e:
                        # If we can't just rename, give an error message.
                        choice = wx.MessageBox(_kMoveFailedText % str(e),
                                               _kMoveFailedTitle,
                                               wx.ICON_ERROR |
                                               wx.OK | wx.CANCEL, None)
                        if choice == wx.CANCEL:
                            return False
                    else:
                        # Video storage directories are stored as absolute
                        # paths in the prefs file. If they pointed to somewhere
                        # within the old data directory we need to update them.

                        # Guess at likely options in case of extreme failure.
                        oldVideoDir = os.path.join(oldAppDir, "videos")
                        newVideoDir = os.path.join(dataDir, "videos")
                        try:
                            from backEnd.BackEndPrefs import BackEndPrefs
                            from appCommon.CommonStrings import kPrefsFile
                            prefs = BackEndPrefs(os.path.join(dataDir, kPrefsFile))

                            oldVideoDir = prefs.getPref('videoDir')
                            if type(oldVideoDir) == bytes:                                oldVideoDir = oldVideoDir.decode('utf-8')
                            if oldVideoDir is not None:
                                prefix = os.path.commonprefix([oldVideoDir, oldAppDir])
                                if prefix == oldAppDir:
                                    newVideoDir = os.path.join(dataDir, oldVideoDir[len(prefix):])
                                    prefs.setPref('videoDir', newVideoDir)

                            storageDir = prefs.getPref('dataDir')
                            if storageDir is not None:
                                prefix = os.path.commonprefix([storageDir, oldAppDir])
                                if prefix == oldAppDir:
                                    prefs.setPref('dataDir',
                                            os.path.join(dataDir, storageDir[len(prefix):]))

                        except Exception as e:
                            choice = wx.MessageBox(_kVideoMoveFailedText %
                                                   (oldVideoDir, newVideoDir, str(e)),
                                                   _kVideoMoveFailedTitle,
                                                   wx.ICON_ERROR |
                                                   wx.OK | wx.CANCEL, None)
                            if choice == wx.CANCEL:
                                return False
                        break

        setFrontEndPref("vdvImportOffered", True)
        return True


    ###########################################################
    def _copyBuildFile(self):
        dataDir = getUserLocalDataDir()
        buildFilePath = os.path.join(dataDir, _kBuildFile)

        # Copy the build file for next time...
        if os.path.exists(_kBuildFile):
            if not os.path.isdir(dataDir):
                os.makedirs(dataDir)
            shutil.copy(_kBuildFile, buildFilePath)


    ###########################################################
    def _handleOldBackends(self):
        """Find / kill old backends.

        As a side effect, this will also detect whether the right version of
        the backend is already running...

        @return isRightVersionRunning  True if the right version of the backend
                                       is already running.
        """
        self._foundLiveBackend = False

        client = BackEndClient()

        # Bound this by WALL CLOCK, not by iteration count.  The old
        # `range(_kTestConnectTimeout * 10)` with a 0.1 s sleep was a
        # 5-second poll only if connect() returned instantly -- it does not.
        # Each connect() is an XML-RPC ping, and with nothing listening it
        # pays a full refused-connect (~2 s measured on this machine, which
        # does not RST loopback immediately), doubled by the retry in
        # _BackEndTransport.request().  That turned this into a ~208 s stall
        # on every launch with no back end running.  The short per-attempt
        # timeout keeps one probe cheap; the deadline keeps the total honest
        # even if an attempt is slower than expected.
        didConnect = False
        deadline = time.time() + _kTestConnectTimeout
        while time.time() < deadline:
            didConnect = client.connect(timeout=_kProbeConnectTimeout)
            if didConnect:
                break
            time.sleep(.1)
            self._startDlg.Pulse()

        if not didConnect:
            # Nothing wants to talk to us.  Do a force quit anyway in case
            # the back end crashed but other processes are still running.
            client.forceQuit(_kForceQuitTimeout, self._startDlg.Pulse)
            return False

        # Somebody answered.  Remember that, so the launch path below knows
        # whether there is actually a back end that needs tearing down.
        self._foundLiveBackend = True

        # No need to reconnect: the probe timeout only gates the reachability
        # check inside _BackEndTransport, it is never applied to the RPC, so
        # the calls below (isConnected, quit) are already uncapped.

        frozen = hasattr(sys, "frozen")
        if client.isConnected(frozen):
            # The right version is already there...
            return True

        # Wrong version is there...
        # ...quit it
        didQuit = client.quit(_kQuitTimeout, self._startDlg.Pulse)
        if not didQuit:
            client.forceQuit(_kForceQuitTimeout, self._startDlg.Pulse)

        return False


    ###########################################################
    def getDebugModeModel(self):
        """Return the data model for debug mode.

        @return debugModeModel  The data model for debug mode.
        """
        return self._debugModeModel


    ###########################################################
    def getAppBuildStr(self, lookInDir='.'):
        """Return a string describing the build of app.

        @param  lookInDir  The dir to look in for 'build.txt'.  The CWD by default.
        @return appVerStr  The app version string.
        """
        buildFilePath = os.path.join(lookInDir, _kBuildFile)

        try:
            buildStr = file(buildFilePath).read().split()[-1].strip()
        except Exception:
            buildStr = "unknown"
        return _kBuildTemplateStr % (buildStr)


    ############################################################
    def _checkForCrashReports(self, backEndClient):
        """If any hard crashes or failed launches, prompt the user to submit."""
        lastCheck = getFrontEndPref('lastCrashCheck')
        if lastCheck == 0:
            # Go back 6 hours by default if we've never checked before.
            lastCheck = time.time()-6*60*60

        launchFailures = getFrontEndPref('launchFailures')

        # Try to avoid any clock oddities.
        now = int(max(time.time(), lastCheck))

        try:
            prompt = _kLaunchError
            crashes = []
            logsToSend = []

            # OSX and WIN can check for failed launch files.
            tmpDir = tempfile.gettempdir()
            launchLogs = os.listdir(tmpDir)
            launchLogs = [os.path.join(tmpDir, log) for log in launchLogs
                    if log.startswith('sighthound')]

            # OSX can check for hard crashes.
            if wx.Platform == "__WXMAC__":
                # TODO: After OSX service, check global dir too?
                osDir = os.path.expanduser("~/Library/Logs/DiagnosticReports")
                crashes = os.listdir(osDir)
                crashes = [os.path.join(osDir, crash) for crash in crashes if
                    crash.startswith('Sighthound')]

                if len(crashes):
                    prompt = _kCrashError

            # The first time we see we've failed to launch we'll allow a retry.
            # If it occurs again we'll force the display dialog even if we have
            # no crash/launch failure files to attach. Something is still wrong
            # and we want to get them in contact with support without them
            # needing to look up contact or forum info.
            forcePrompt = False
            if launchFailures > 1:
                forcePrompt = True
                prompt = _kLaunchError
            else:
                # Increment the launch failure count. It'll be reset to 0 if we
                # continue and open.
                setFrontEndPref('launchFailures', launchFailures+1)

            # Include up to 3 recent logs from each category. Err on the side
            # of including info that might have been seen before rather than
            # missing it for "launch" files.
            launchLogs.sort(key = lambda x: os.path.getmtime(x))
            crashes = \
                [log for log in crashes if os.path.getmtime(log) >= lastCheck]
            logsToSend += launchLogs[-3:]
            logsToSend += crashes[-3:]

            if (len(crashes) or forcePrompt) and kHasBugReportDialog:
                # Reset the count, as we'll want a little buffer before
                # re-prompting again.
                setFrontEndPref('launchFailures', 0)

                # We have logs we'd like to submit ask the user and then send.
                submit = wx.MessageBox(prompt, _kCrashTitle,
                        wx.ICON_ERROR | wx.YES_NO)
                if wx.YES == submit:
                    dlg = BugReportDialog(None, self._logger, backEndClient, logsToSend)
                    try:
                        dlg.ShowModal()
                    finally:
                        dlg.Destroy()

                # Should we try to remove files we submitted?
                # Until then, need to set lastCheck to max(lastCheck, maxmtime)
                # in case anyone had their clock in the future and got a crash.
                for log in crashes:
                    now = max(now, os.path.getmtime(log))


        except Exception as e:
            self._logger.error("Error checking for crash reports - " + str(e))

        # Update the 'last checked' time.
        setFrontEndPref('lastCrashCheck', now)





##############################################################################
class _DebugModeModel(AbstractModel):
    """A simple data model for keeping track of debug mode.

    Always just does a broadcast update, since there's only one member.
    """

    ###########################################################
    def __init__(self):
        """_DebugModeModel constructor."""
        super(_DebugModeModel, self).__init__()
        self._debugMode = None

    ###########################################################
    def _debugFileLocation(self):
        userLocalDataDir = getUserLocalDataDir()
        return None if userLocalDataDir is None else os.path.join(userLocalDataDir, "debugMode")

    ###########################################################
    def isDebugMode(self):
        """Return whether we're in debug mode.

        @return isDebugMode  True if we're in debug mode; False otherwise.
        """
        if self._debugMode is None:
            debugFile = self._debugFileLocation()
            if debugFile is None:
                # we aren't in the state where we can query debug mode yet
                return False
            self._debugMode = os.path.isfile(debugFile)
        return self._debugMode

    ###########################################################
    def setDebugMode(self, wantDebugMode):
        """Set debug mode and update our listeners.

        @param  wantDebugMode  True if we want debug mode; False otherwise.
        """
        # Force to a True bool, just to be paranoid (since we compare with ==)
        wantDebugMode = bool(wantDebugMode)
        if self._debugMode != wantDebugMode:
            if wantDebugMode:
                with open(self._debugFileLocation(), "w") as f:
                    f.write("Debug mode is on!")
            else:
                safeRemove(self._debugFileLocation())
                if os.path.isfile(self._debugFileLocation()):
                    # we've failed to remove the file -- and disable debug mode
                    wantDebugMode = True
            self._debugMode = wantDebugMode
            self.update()

##############################################################################
def validateOSVersion(logger):
    """
    Check against known problematic OSX version.
    If failing to retrieve version, or in other unexpected scenarios, err
    on side of caution, and proceed with the attempt to start the software.
    """
    isOSX = sys.platform == "darwin"
    if not isOSX:
        return True

    import platform
    ver = platform.mac_ver()
    if ver is None:
        logger.error("Could not retrieve OSX version")
        return True

    release = ver[0]
    if release is None or len(release) == 0:
        logger.error("OSX version release string is empty")
        return True

    releaseArr = release.split(".")
    if len(releaseArr) < 2:
        logger.error("OSX version release string value is unexpected:" + release)
        return True

    major = int(releaseArr[0])
    minor = int(releaseArr[1])
    hasPatch = len(releaseArr) > 2
    patch = 0 if not hasPatch else int(releaseArr[2])

    errStr = None;
    if major < 10 or (major == 10 and minor < 10):
        errStr = "Unsupported macOS version. macOS 10.10 or greater is required"
    elif major == 10 and minor == 12 and patch < 4:
        errStr = """Unsupported macOS version %s. This macOS version has a known problem, """\
                 """which prevents Sighthound Video from functioning correctly. """\
                 """Please upgrade macOS to 10.12.4 or later version.""" % release

    if errStr is not None:
        dlg = wx.MessageDialog(None,
                        errStr,
                        "Cannot start Sighthound Video",
                        wx.OK | wx.ICON_ERROR)
        dlg.ShowModal()
        return False
    return True

##############################################################################
_faultLogFile = None

def _armFaultHandler():
    """Arm faulthandler so a native crash (access violation / heap corruption,
    e.g. in the OpenGL rendering path or a C extension) dumps an all-thread
    Python traceback to <localAppData>\\<kAppName>\\logs\\nativeCrash.log
    instead of vanishing silently to the desktop.

    The GUI is launched via this module's own main() (not FrontEndLaunchpad),
    so it needs its own hook.  Wrapped so it can never block GUI startup.
    """
    global _faultLogFile
    try:
        import os, sys, tempfile, time, faulthandler
        try:
            from appCommon.CommonStrings import kAppName
        except Exception:
            kAppName = "Sighthound Video Py3"
        logDir = None
        localAppData = os.environ.get('LOCALAPPDATA')
        if sys.platform == 'win32' and localAppData:
            logDir = os.path.join(localAppData, kAppName, 'logs')
        if not logDir:
            logDir = tempfile.gettempdir()
        try:
            os.makedirs(logDir, exist_ok=True)
        except Exception:
            logDir = tempfile.gettempdir()
        _faultLogFile = open(os.path.join(logDir, 'nativeCrash.log'), 'a',
                             buffering=1)
        _faultLogFile.write(
            "\n===== faulthandler armed (front end) pid=%d at %s =====\n" % (
                os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S')))
        _faultLogFile.flush()
        faulthandler.enable(file=_faultLogFile, all_threads=True)
    except Exception:
        # Diagnostics must never block GUI startup.
        pass


def main():
    _armFaultHandler()

    # Claim our taskbar identity BEFORE any window exists -- Windows reads it
    # when the first window is shown and ignores it afterwards.  This is what
    # ties the running window to the installed shortcut, so "Pin to taskbar"
    # pins the app instead of the interpreter hosting it.
    setAppUserModelId(kWindowsAppUserModelId)

    # Grab the standard streams before creating the app.  This seems to be
    # needed on Windows.  Note that we can't give a log directory yet because
    # we can't use getUserLocalDataDir() until after the app is created...
    logger = getLogger(kFrontEndLogName)
    logger.grabStdStreams()

    # Everything before this was module import -- wx and what it drags in.
    # These marks are how we tell import cost from launch cost on a machine we
    # cannot attach a profiler to.  Disk logging isn't on yet, so the early
    # records buffer and flush once PostInit points the logger at the data
    # directory; the timestamps survive that.
    _logStartupMark(logger, "imports done")

    # If OnInit() returns false, the app will automatically exit.
    app = FrontEndApp(logger)
    _logStartupMark(logger, "wx app created")

    try:
        if not validateOSVersion(logger):
            return
    except:
        import traceback
        traceback.print_exc()

    # Immediately call PostInit() so that we perform our checks before the
    # application is started. If True is returned, start the MainLoop to begin
    # processing GUI events.  If false, log as an error.
    if app.PostInit():
        _logStartupMark(logger, "entering the main loop")
        app.MainLoop()
    else:
        # When OnInit() returns false, we get a similar message, so I copied
        # that, and applied it here for when PostInit returns false.
        logger.error("PostInit returned false, exiting...")
    logger.info("Front end application closed...")


if __name__ == '__main__':
    main()
