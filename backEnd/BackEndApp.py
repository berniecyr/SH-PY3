#! /usr/local/bin/python

#*****************************************************************************
#
# BackEndApp.py
#   Core orchestration process.
#   Responsible for spawning and communicating with all the other processes of the app.
#   Receives analytics data from camera processes, and persists it in the database,
#   as well as performs real-time searches and initiates responses.
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


"""
## @file
Contains the BackEndApp class.
"""

# Python imports...
import bisect
import pickle
import errno
import logging
import operator
from queue import Empty as QueueEmpty
from multiprocessing import Pipe, Queue
from queue import PriorityQueue
import os
from signal import SIGTERM
from socket import timeout as sockettimeout
import shutil
from sqlite3 import DatabaseError
import sys
import time
import traceback
import xmlrpc.client
import multiprocessing
import hashlib
import ssl

from collections import deque

# Toolbox imports...
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.networking.SanitizeUrl import sanitizeUrl
from vitaToolbox.networking.XmlRpcUtils import TimeoutTransport
from vitaToolbox.networking.Upnp import ControlPointManager
from vitaToolbox.networking.Upnp import extractUsnFromUpnpUrl
from vitaToolbox.networking.Upnp import isUpnpUrl
from vitaToolbox.networking.Upnp import realizeUpnpUrl
from vitaToolbox.networking.Onvif import OnvifDeviceManager
from vitaToolbox.networking.Onvif import extractUuidFromOnvifUrl
from vitaToolbox.networking.Onvif import isOnvifUrl
from vitaToolbox.networking.Onvif import realizeOnvifUrl
from vitaToolbox.path.PathUtils import normalizePath
from vitaToolbox.path.VolumeUtils import getVolumeNameAndType
from vitaToolbox.path.GetDiskSpaceAvailable import checkFreeSpace
from vitaToolbox.windows.winUtils import registerForForcedQuitEvents
from vitaToolbox.sysUtils.MachineId import machineId
from vitaToolbox.sysUtils.TimeUtils import getTimeAsMs
from vitaToolbox.strUtils.EnsureUnicode import ensureUnicode
from vitaToolbox.threading.ThreadPool import ThreadPool
from vitaToolbox.process.ProcessUtils import listChildProcessesOfPID
from vitaToolbox.profiling.QueueStats import QueueStats

# Local imports...
from appCommon.InstallPaths import exportDataDir
from appCommon.CommonStrings import kPortFileName, isLocalCamera
from appCommon.CommonStrings import kRuleDir, kRuleExt, kQueryExt
from appCommon import KeepAwake
from appCommon.CommonStrings import kPrefsFile, kCamDbFile
from appCommon.CommonStrings import kAnyCameraStr
from appCommon.CommonStrings import kCameraUndefined, kCameraOn, kCameraOff
from appCommon.CommonStrings import kCameraConnecting, kCameraFailed
from appCommon.CommonStrings import kEmailResponse, kRecordResponse
from appCommon.CommonStrings import kSoundResponse, kCommandResponse
from appCommon.CommonStrings import kFtpResponse, kPushResponse
from appCommon.CommonStrings import kIftttResponse
from appCommon.CommonStrings import kWebhookResponse, kSnapshotResponse, kIHostResponse
from appCommon.CommonStrings import kTapoResponse
from appCommon.CommonStrings import kFtpProtocol
from appCommon.CommonStrings import kLocalExportProtocol
from appCommon.CommonStrings import kLocalExportResponse
from appCommon.CommonStrings import kTestLiveFileName
from appCommon.CommonStrings import kCorruptDbErrorStrings
from appCommon.CommonStrings import kVideoFolder, kRemoteFolder
from appCommon.CommonStrings import kWebDirName
from appCommon.CommonStrings import kWebDirEnvVar
from appCommon.CommonStrings import kMemStoreBackendReady
from appCommon.CommonStrings import kMemStoreRulesLock
from appCommon.CommonStrings import kSupportEmail
import sqlite3
from vitaToolbox.sql.TimedCursor import getCorruptionReports, noteCorruption
from appCommon.DbRecovery import writeCorruptionFlag, readCorruptionFlag
from appCommon.DbRecovery import clearCorruptionFlag
from appCommon.DbRecovery import kStallFlagSource
from appCommon.CommonStrings import kClipDbFile
from appCommon.CommonStrings import kResponseDbFile
from appCommon.CommonStrings import kObjDbFile
from appCommon.CommonStrings import kDefaultRecordSize, kMaxRecordSize
from appCommon.CommonStrings import kMatchSourceSize
from appCommon.CommonStrings import kExecAlertThreshold
from appCommon.CommonStrings import kTargetSettingToLabel
from appCommon.CommonStrings import kOpenSourceVersion
from appCommon.LicenseUtils import hasPaidEdition
from appCommon.LicenseUtils import kCamerasField
from appCommon.SearchUtils import parseSearchResults
from appCommon.XmlRpcClientIdWrappers import ServerProxyWithClientId
from appCommon.DbRecovery import getCorruptDatabaseStatus
from appCommon.DbRecovery import runDatabaseRecovery
from appCommon.DbRecovery import kStatusRecover
from appCommon.DbRecovery import kStatusReset
from appCommon.DebugPrefs import getDebugPrefAsInt, getDebugPrefAsFloat
from appCommon.DebugPrefs import getDebugTracer
from .BackEndPrefs import BackEndPrefs
from .BackEndPrefs import kKeepSystemAwake
from .BackEndPrefs import kLiveMaxBitrate
from .BackEndPrefs import kLiveEnableTimestamp
from .BackEndPrefs import kLiveMaxResolution
from .BackEndPrefs import kClipResolution
from .BackEndPrefs import kLiveEnableFastStart
from .BackEndPrefs import kGenThumbnailResolution
from .BackEndPrefs import kFpsLimit
from .BackEndPrefs import kRecordInMemory, kClipMergeThreshold, kHardwareAccelerationDevice
from .BackEndProcessJumper import startCapture
from .BackEndProcessJumper import startDiskCleaner
from .BackEndProcessJumper import startNetworkMessageServer
from .BackEndProcessJumper import startResponseRunner
from .BackEndProcessJumper import startDetectionService
from .BackEndProcessJumper import startStream
from .BackEndProcessJumper import startWebServer
from .BackEndProcessJumper import startPacketCapture
from .BackEndProcessJumper import startPlatformHTTPWrapper
from .CameraManager import CameraManager
from .ClipManager import ClipManager
from .DataManager import DataManager
from .DebugLogManager import DebugLogManager
if kOpenSourceVersion:
    from .LicenseManagerOSS import LicenseManager
else:
    from LicenseManager import LicenseManager
from .ClipUploader import ClipUploader
from . import MessageIds
from .ResponseDbManager import ResponseDbManager
from .responses.CommandResponse import CommandResponse
from .responses.EmailResponse import EmailResponse
from .responses.WebhookResponse import WebhookResponse
from .responses.PushResponse import PushResponse
from .responses.IftttResponse import IftttResponse
from .responses.RecordResponse import RecordResponse
from .responses.SoundResponse import SoundResponse
from .responses.SnapshotResponse import SnapshotResponse
from .responses.IHostResponse import IHostResponse
from .responses.TapoResponse import TapoResponse
from vitaToolbox.networking.TapoControl import tapoHostFromUri
from .responses.SendClipResponse import SendClipResponse
from videoLib2.python.ClipReader import getMsList
from videoLib2.python.StreamReader import getLocalCameraNames as strmGetLocalCameraNames
from videoLib2.python.StreamReader import getHardwareDevicesList as getHardwareDevicesList
from .WebServer import killWebServerProcesses
from launch.Launch import Launch
from launch.Launch import serviceAvailable
from appCommon.hostedServices.IftttClient import IftttClient
from .SavedQueryDataModel import convertOld2NewSavedQueryDataModel
from .NetworkScanner import NetworkScanner, OnvifNetworkScanner



# We need to check for instances of WindowsError but it's not defined on osx.
try:
    WindowsError #PYCHECKER OK: Line does have effect in context of surrounding.
except NameError:
    WindowsError = None


# Constants...

_kTmpFolder = 'tmp'
_kCameraCheckInterval = 10

# Restart-storm backoff: a camera whose process died within _kFastDeathSecs of
# starting is failing at startup (bad DB, bad config, crash), not streaming.
# After the second consecutive quick death, delay restarts exponentially
# (_kRestartBackoffBaseSecs doubling up to _kRestartBackoffMaxSecs) instead of
# hammering: a storm burns CPU, floods logs, and (2026-07-26) can grind the
# whole app down for a night.  One healthy run resets the count.
_kFastDeathSecs = 60
_kRestartBackoffBaseSecs = 15
_kRestartBackoffMaxSecs = 300
# How often the main loop may ask the service whether a shutdown is pending.
# The service publishes that state every 2s, so checking faster cannot learn
# anything new -- it only burns SCM/registry/file I/O and collides with the
# service's own rewrite of the file.  See checkForServiceShutdown().
_kServiceShutdownCheckSecs = 2.0
_kLogName = "BackEndApp.log"
_kLogSize = 1024*1024*5
# Startup/crash breadcrumb trace; written only when enabled via debugPrefs.
_kTraceName = "be_trace.txt"
_kOnvifLogName = "Onvif.log"
_kOnvifLogSize = 1024 * 1024 * 2
_kUpnpLogName = "Upnp.log"
_kUpnpLogSize = 1024 * 1024 * 2
_kPipeCleanupWait = 60*5
_kMinimumSearchDelayMs = 1000

# After stopping a camera, we'll tell responses to flush after this many secs.
_kResponseFlushTime = 60

_kMaxIdleDelay = 15

# If we've had a pending search for longer than this many seconds, we'll do it.
_kStaleRealtimeSearchSeconds = 5

# If a camera hasn't responded in longer than this many seconds we assume it
# has eternally stalled.
_kCameraTimeout = 240

# How long a camera may go without its processed-data timestamp advancing
# before we treat it as wedged and restart it.
#
# _kCameraTimeout above only covers a camera that stopped PINGING.  A camera
# whose reporting pipeline has stalled goes on pinging, decoding and recording
# perfectly while delivering nothing -- on 2026-08-29 05_Gate_lr sat like that
# for five hours, at 19fps, and the watchdog never looked at it.  Progress is
# the thing worth watching, so watch it directly.
#
# Safe against a genuinely quiet scene: CameraCapture forces a notify every
# _kMaxNoNotifyMs regardless of whether anything moved, so a healthy camera
# refreshes this well inside the window even staring at an empty driveway.
_kCameraNoProgressTimeout = 300

# Warn when cameras are recording but nothing has reached the clip database for
# this long.  Segments are 60s (15s on an unstable camera), so ten minutes is
# far outside normal jitter yet catches the failure long before a night's worth
# of footage is stranded.  On 2026-08-04 this condition ran undetected for 1h45m
# and left 253 unregistered clips for the orphan sweep to delete.
_kClipRegistrationWarnSecs = 600

# Source recorded on the corruption flag when the watchdog above raises it.
# Lives in DbRecovery because DiskCleaner reads it too: it has to tell a stall
# apart from real damage before describing the flag to anyone.
_kStallFlagSource = kStallFlagSource

# If a response runner hasn't responded in longer than this many seconds we
# assume it has eternally stalled.  Go a little on the long end here since
# the ResponseRunner has some long sleeps in it (TODO: needed?)
_kResponseRunnerTimeout = 300

# We'll ping the NMS every X seconds to let it know we're still around.
_kMessageServerPingTime = 120

# We'll delete rules associated with a camera this long after we remove the
# camera to give any pending information time to be processed.
_kRuleCleanupTimeout = 120

# If a temp video file could not be moved we'll try again for this many seconds
# before giving up and deleting it.
_kTmpFileLifetime = 20*60

# We won't give UPNP time more than every this many seconds...
_kFastestUpnpPoll = 2.0

# UPnP shouldn't take longer than this...
_kSlowUpupTime = _kFastestUpnpPoll / 2

# We won't give ONVIF time more than every this many seconds...
_kFastestOnvifPoll = 2.0

# ONVIF shouldn't take longer than this...
_kSlowOnvifTime = _kFastestOnvifPoll / 2

# How long after the Add-Camera wizard's last search request we keep on-demand
# ONVIF/UPnP discovery alive before tearing it down.  The wizard re-requests a
# search every _kMinorActiveSearchPeriod (~7s) while open, so this is comfortably
# larger to avoid stopping mid-wizard.
_kDiscoveryIdleTimeout = 20.0

# Timeouts for waiting for quit during cleanup...
_kCamQuitTimeout = 5
_kDiskCleanerQuitTimeout = 22  # Just lower than front end's timeout
_kWebServerQuitTimeout = 5

# Number of worker threads for the (global) thread-pool.
_kThreadPoolSize = multiprocessing.cpu_count() * 4

# Timeout for communicating with the network message server. If we exceed this
# we assume it is hung and destroy the world.
_kNetworkMessageServerTimeout = 300

# Timeout to deliver the shutdown message to the NMS.
_kNmsShutdownTimeout = 2

# Current logging configuration
_kDebugConfig = None

# Globals...

# Keep a reference to the current instance, for the forced quit callback to use.
_app = None


_kFakeMessageIdDataManagerIdleProcessing = 90000
_kFakeMessageIdRealTimeSearch            = 90001
_kFakeMessageIPCUtility                  = 90002
_kFakeMessageIPCCamera                   = 90003

# Queue statistics constants
_kStatsDefaultInterval = 60*60 # by default, log stats every hour
_kStatsMaxQueue = 50        # consider queue size over 50 an error
_kStatsMaxExecTime = 0.2    # consider any event processing over 0.2s an error
_kStatsAlertInterval = 10   # log an alert every 10s at most

##############################################################################
class NetworkScannerCallback(object):
    def __init__(self, msgId, q):
        self._msgId = msgId
        self._queue = q

    def onUpdate(self, allDevices, changedDevices, goneDevices):
        self._queue.put([self._msgId, allDevices, changedDevices, goneDevices])

##############################################################################
class BackEndApp(object):
    """The main application class for the back end."""
    ###########################################################
    def __init__(self, userLocalDataDir):
        """Initialize BackEndApp.

        @param  userLocalDataDir  The directory in which to store app data.
        """
        # Call the superclass constructor.
        super(BackEndApp, self).__init__()

        self._cleanedUp = False

        # When this back end started, for the System tab's "Running since".
        # Stamped here rather than in run() so it covers the whole process,
        # and pushed to the message server with the health process map.
        self._startTimeSecs = time.time()

        self._upnpDevices = {}
        self._upnpScanner = None
        self._upnpLogger = None
        self._onvifDevices = {}
        self._onvifScanner = None
        self._onvifLogger = None

        # ONVIF/UPnP discovery is off by default and started ON DEMAND while the
        # Add-Camera wizard is open (it re-requests a search every few seconds),
        # then torn down once it goes idle.  The "persistent" flags are set when
        # the user opts into always-on discovery via an enable* marker file, in
        # which case we never auto-stop.  _lastCameraSearchTime arms the idle
        # teardown (0.0 = disarmed).
        self._onvifPersistent = False
        self._upnpPersistent = False
        self._lastCameraSearchTime = 0.0


        self._netMsgServerProc = None
        self._diskCleanupProc = None
        self._diskCleanupQueue = None
        self._webServerProc = None
        self._webServerQueue = None
        # Last port/auth the web server was started with; kept current by the
        # msgIdWebServerSet* handlers so the watchdog can restart it correctly.
        self._webPort = None
        self._webAuth = None
        self._platformHTTPWrapperProc = None
        self._responseRunnerProc = None
        self._detectionServiceProc = None
        self._responseRunnerQueue = None

        # Setup logging...  SHOULD BE FIRST!
        self._userLocalDataDir = userLocalDataDir
        self._logDir = os.path.join(self._userLocalDataDir, "logs")
        # Startup breadcrumbs, off unless 'trace' is set in debugPrefs -- these
        # run before the logger exists, so they're the only visibility we have
        # if we die in here.
        _dbg = getDebugTracer(_kTraceName, self._userLocalDataDir)
        self._dbg = _dbg
        _dbg(f"BackEndApp.__init__ start, userLocalDataDir={userLocalDataDir!r}")
        self._logger = getLogger(_kLogName, self._logDir, _kLogSize)
        _dbg("logger created")
        self._logger.grabStdStreams()
        _dbg("grabStdStreams done")

        assert type(self._userLocalDataDir) == str, f"Expected str, got {type(self._userLocalDataDir)}"
        _dbg("assert passed")

        self._threadPool = ThreadPool(_kThreadPoolSize)

        self._iftttStatePending = None  # state to be sent
        self._iftttStateSending = False # some state is currently being sent
        self._iftttStateCleared = None  # state got set to empty on the server
        self._iftttLastStateOut = None  # the last state sent (successfully)

        # Key = Camera Location
        # value = (process, cameraPipe, dataMgrPipeId, lastPingTime)
        self._captureStreams = {}
        # Restart-storm protection (incident 2026-07-26: camera processes died
        # at startup all night while the supervisor restarted them instantly).
        # A camera that keeps dying QUICKLY gets an increasing delay before its
        # next restart; one healthy run resets it.  Manual enable clears it.
        self._cameraStartTimes = {}    # loc -> time.time() of last start
        self._cameraFastFails = {}     # loc -> consecutive quick deaths
        self._cameraRestartAfter = {}  # loc -> earliest time to retry
        # Key = id, value = data manager pipe
        self._dataMgrPipes = {}
        self._nextPipeId = 0
        self._analyticsPort = None

        self._disableDiskCleanup = os.path.exists(
                                        os.path.join(self._userLocalDataDir,
                                                     'nocleanup'))
        # Key = Camera Location
        # value = (uri, isEnabled, isBeingMonitored, extraDict)
        self._cameraInfo = {}

        # When True, recording is paused on all cameras because the video drive
        # is critically low on free space (set from the DiskCleaner safety net).
        # Cameras are held stopped until space recovers, without touching their
        # user "enabled" flags.
        self._lowDiskPaused = False

        # Key = Camera Location
        # value = (procWidth, procHeight)
        self._cameraProcSizes = {}

        # Key = Camera Location
        # value = {key=sessionUid, value=lastPing}
        self._cameraJpegList = {}

        # Key = camera location, value = ruleDict:
        #    key = ruleName, value = (rule, isScheduled, nextSchedChange, query,
        #                             responseList)
        self._ruleDicts = {}
        # Key = camera location, value = last time a search was run
        self._lastSearchTimes = {}

        # We don't want to remove data manager pipes until we're sure that they
        # are completely done.  This is a list of pipe ids that are potentially
        # dead, and the time that we feel safe removing them.
        self._deadPipes = {}

        self._clipManager = None
        self._dataManager = None
        self._childProcQueue = None
        self._childProcLocalQueue = deque()
        self._delayedMessagesQueue = PriorityQueue()
        interval = getDebugPrefAsInt("backEndQueueStats", _kStatsDefaultInterval, userLocalDataDir)
        maxQueueSize = getDebugPrefAsInt("backEndQueueMaxSize", _kStatsMaxQueue, userLocalDataDir)
        maxExecTime = getDebugPrefAsFloat("backEndQueueMaxExecTime", float(kExecAlertThreshold), userLocalDataDir)
        self._childProcQueueStats = QueueStats(self._logger, interval, _kStatsAlertInterval, maxQueueSize, maxExecTime)

        self._lastCameraCheck = 0

        # The time.time() of the last time we did a realtime search...
        # Note that we use to make sure that data doesn't get left unsearched
        # even if no new motion data is coming in.
        self._lastRealtimeSearch = 0

        # A list of pending "add frame" requests.
        # We buffer these up and add them at idle time (obviously before doing
        # any real time searches)
        self._pendingAddFrames = deque()

        # A dictionary of pending real-time searches.  When we receive the
        # msgIdStreamProcessedData message, we'll set:
        #  self._pendingRealTimeSearches[camName] = ms
        # ...then, when we're idle, we'll do searches.  This keeps searches
        # from backlogging and also lowers their priority.
        self._pendingRealTimeSearches = {}

        # A dictionary of the maximum processed time for all running cameras.
        # Key = camera name, value = maximum time processed in the pipeline.
        self._maxProcessedTime = {}

        # When each camera's _maxProcessedTime last MOVED, as time.time().
        # _maxProcessedTime itself is a camera-clock ms, so it cannot answer
        # "how long has this been stuck?" on its own -- that needs our clock.
        # Read by the no-progress watchdog in run().
        self._cameraProgressTime = {}

        # Here we keep track of when we'd like to call flush on responses
        # that we might have...
        # Key = camLoc
        # Value = (list of responses, timeCamTurnedOff)
        self._responsesToFlush = {}

        # A way to map temporary IDs used by the queued data manager to dbId.
        # Key: pipeId (can be used to index into self._dataMgrPipes)
        # Value: A list of (camObjId, dbId) tuples, oldest first.
        # ...things will be deleted from this list as soon as the queued
        # data manager stops using the temp Id.
        self._tempIdMap = {}

        # A process handle to the current test camera, or None; also keep URI
        # and the extras it was started with.  The extras have to be kept: the
        # ONVIF/UPnP change handlers restart this stream, and without them they
        # reached for whatever `extras` the preceding loop happened to leave
        # bound -- an unrelated camera's settings, or nothing at all when no
        # cameras are configured.
        self._testCamProc = None
        self._testCamUri = None
        self._testCamExtras = None

        # A process handle to the current packet capture, or None. We also keep
        # track of the status of the pcap process using a dictionary with two
        # keys, 'pcapEnabled' and 'pcapStatus', both set to None when
        # initialized and when the process is dead.
        self._pcapCamProc = None
        self._pcapInfo = {"pcapEnabled":None, "pcapStatus":None}

        # Track the last time we sent a ping to the network message server.
        self._lastMessageServerPing = 0

        # Track the last time we got a ping from the response runner.
        self._lastResponseRunnerPing = 0

        # Key = camera name, value = target cleanup time
        self._pendingRuleCleanupDict = {}

        # A queue for record responses to append messages to.  RecordResponse
        # only ever appends, so a deque is a drop-in and drains in O(1).
        self._recordResponseMsgs = deque()

        # Key = camera name, value = highest tagged time for that location
        self._lastTaggedTimes = {}

        # Key = target file name, value = camLoc, time to delete the file.
        self._pendingFileMoves = {}

        # A tuple of (fail time, proc, msg) for a pending rename operation.
        self._pendingRenameMsg = None

        # A list of (process, time stopped) tuples for cameras that have
        # been told to quit.
        self._deadCameras = []

        # A list of camera locations that we should add saved times for no
        # matter what.  Used to avoid missed tagging when we know a camera
        # process will no longer receive messages but still appears alive.
        self._selfAddSavedTimes = []

        # Exit flag to indicate that the back-end must not be restarred.
        self.wantQuit = False

        # When we last asked the service whether a shutdown is pending.  The
        # check is not free (an SCM query, a registry read and a file read) and
        # the main loop below spins as fast as messages arrive, so it is
        # throttled rather than run on every pass.
        self._lastServiceShutdownCheck = 0.0

        # To avoid multiple cleanup attempts.
        self._cleanedUp = False

        # List of cameras and their status
        self._cameras = {}

        self._clipUploader = None

        # cached live view requests while the camera was connecting
        self._pendingLiveViewStatus = {}
        self._pendingLiveViewSettings = {}

        # set up debug logging, if needed
        self._debugLogManager = DebugLogManager("BackEnd", self._userLocalDataDir)



    ###########################################################
    def __del__(self):
        """Destructor for BackEndApp."""
        self._logger.info("Beginning back end shutdown, in dtor.")
        self.cleanup()
        self._logger.info("Finished back end shutdown, in dtor.")


    ###########################################################
    def _terminateCameraProcess(self, proc):
        """Terminate the process, and all of its children too, if any.

        @param proc process to kill.
        """
        childrenPIDs = listChildProcessesOfPID(proc.pid)

        for childPID in childrenPIDs:
            try:
                os.kill(childPID, SIGTERM)
            except OSError:
                # We expect this if the child was already stopped or killed...
                pass

        proc.terminate()


    ###########################################################
    def cleanup(self):
        """Free resources used by BackEndApp."""
        if self._cleanedUp:
            return
        self._cleanedUp = True

        self._logger.info("Beginning cleanup")

        # Bring down the thread pool. Don't wait though.
        self._threadPool.shutdown()

        if self._upnpScanner:
            self._upnpScanner.shutdown()
        if self._onvifScanner:
            self._onvifScanner.shutdown()

        # Bring down the web server first, since it depends on everything else.
        if self._webServerProc:
            webServerQuitTime = time.time()
            self._putMsgWS([MessageIds.msgIdQuit])

        # Stop the xmlrpc server
        self._logger.info("Stopping NMS...")
        if self._netMsgServerProc:
            portFilePath = os.path.join(self._userLocalDataDir, kPortFileName)
            tmout = time.time() + _kNmsShutdownTimeout
            try:
                nmsClient = self._getXMLRPCClient(_kNmsShutdownTimeout)
                if nmsClient is not None:
                    nmsClient.shutdown()
                while time.time() < tmout:
                    if not os.path.exists(portFilePath):
                        self._logger.info("NMS shutdown completed")
                        break
                    time.sleep(.1)
            except:
                self._logger.error("cannot send shutdown to NMS (%s)" %
                                   sys.exc_info()[1])
            self._netMsgServerProc.terminate()
            try:
                os.remove(portFilePath)
                self._logger.info("port file removed")
            except:
                self._logger.warning("removing port file failed (%s)" %
                                     sys.exc_info()[1])

        # Stop the disk cleanup process
        self._logger.info("Stopping disk monitor...")
        if self._diskCleanupProc:
            diskCleanQuitTime = time.time()
            self._putMsgDC([MessageIds.msgIdQuit])

        # Stop the response process
        self._logger.info("Stopping responses...")
        if self._responseRunnerProc:
            self._putMsgRR([MessageIds.msgIdQuit])

        # Signal the camera capture processes to cleanup
        cameraProcesses = []
        self._logger.info("Stopping cameras...")
        for camLoc in self._captureStreams:
            proc, camPipe, _, _ = self._captureStreams[camLoc]
            self._sendMsg(camPipe, [MessageIds.msgIdQuit], camLoc)
            cameraProcesses.append(proc)
        self._captureStreams = {}

        # Make sure that dead cameras get killed too...
        for (proc, _) in self._deadCameras:
            cameraProcesses.append(proc)
        self._deadCameras = []

        # If there was a test camera going, kill it
        self._logger.info("Stopping test cam...")
        self._stopTestCamera()

        startTime = time.time()
        anyAlive = True
        self._logger.info("Waiting for cameras...")
        while anyAlive and (time.time()-startTime < _kCamQuitTimeout):
            for proc in cameraProcesses:
                if proc.is_alive():
                    time.sleep(1)
                    break
            else:
                anyAlive = False

        # Ensure all camera streams are killed
        self._logger.info("Terminating cameras...")
        for proc in cameraProcesses:
            self._terminateCameraProcess(proc)

        # Stop the detection service.  It's stateless (no DB/files held), so
        # a hard terminate is safe and instant; the stale port file is
        # overwritten on next start.
        #
        # MUST come after the cameras are gone.  Killing it first left the
        # still-running cameras issuing detection requests at a dead socket
        # for the whole camera-quit window, which logged a WinError 10054
        # (reset in flight) followed by a 10061 (refused) every couple of
        # seconds -- making every ordinary restart look like a failure.
        self._logger.info("Stopping detection service...")
        if self._detectionServiceProc is not None:
            try:
                self._detectionServiceProc.terminate()
            except Exception:
                pass
            self._detectionServiceProc = None

        # Handle any last minute messages
        self._logger.info("Handling last messages...")
        for n in range(1,1000):
            try:
                msg, _ = self._getQueueMessage(timeout=1)
                self._logger.info("last message #%d: %s" % (n, str(msg[0])))
                self._processQueueMessage(msg)
            except QueueEmpty:
                break
            except:
                self._logger.error(traceback.format_exc())
                break

        # Force flush any responses.  Note that the response runner is gone, but
        # that's OK.  The only thing that needs this is the SendClip response,
        # which writes to a database...
        self._logger.info("Flushing responses...")
        try:
            self._flushResponses(True)
        except:
            self._logger.error("failed (%s)" % sys.exc_info()[1])

        # Ensure any last minute additions are saved
        if self._dataManager:
            # Finish up any idle processing
            try:
                self._flushIdleQueue()
            except:
                self._logger.error("final idle queue flush failed (%s)" %
                                   sys.exc_info()[1])

        # Make sure that the disk cleaner is gone; give a longer timeout
        # than for cameras, since killing it can cause data loss...
        self._logger.info("Waiting for disk monitor...")
        if self._diskCleanupProc:
            while (self._diskCleanupProc.is_alive())                       and \
                  (time.time() - diskCleanQuitTime < _kDiskCleanerQuitTimeout):
                time.sleep(1)
            self._diskCleanupProc.terminate()
            self._diskCleanupProc = None

        if self._platformHTTPWrapperProc:
            self._platformHTTPWrapperProc.terminate()
            self._platformHTTPWrapperProc = None

        # And finally, in case it got stuck, remove the web server.
        self._logger.info("Waiting for web server...")
        if self._webServerProc:
            while (self._webServerProc.is_alive()) and \
                  (time.time() - webServerQuitTime < _kWebServerQuitTimeout):
                time.sleep(1)
            self._webServerProc.terminate()
            # Make sure all of the associated web server processes are gone too,
            # at this moment we do not trust the web server logic itself to
            # always take them down (and there's no harm in trying anyway) ...
            killWebServerProcesses(self._logger)
            self._webServerProc = None

        # Check if the thread pool is really down.
        if not self._threadPool.shutdown():
            self._logger.warning("Thread pool is still active.")

        if self._clipUploader is not None:
            self._logger.info("Shutting down clip uploader")
            self._clipUploader.shutdown()
            self._clipUploader = None

        self._logger.info("Back end cleanup complete")

        # In case the service is waiting for us, but we came here through a
        # different path this call will release the service from waiting ...
        self.checkForServiceShutdown()


    ###########################################################
    def checkForServiceShutdown(self):
        """ Opens the exchange to the service via shared memory and checks if.
        a shutdown request is pending. We copy the signal and clear it, so the
        service knows that there was a taker and it can continue.

        Throttled to _kServiceShutdownCheckSecs: this costs an SCM query, a
        registry read and a file read, and the main loop calls it on every pass
        -- which under load meant hundreds of them a second, all contending with
        the service's own 2s rewrite of the very file being read.

        @return  True if shutdown got detected.
        """
        now = time.time()
        if (now - self._lastServiceShutdownCheck) < _kServiceShutdownCheckSecs:
            return False
        self._lastServiceShutdownCheck = now

        if serviceAvailable():
            try:
                launch = Launch()
                if launch.open():
                    return 1 == launch.shutdown()
            except:
                pass
            finally:
                try:
                    launch.close()
                except:
                    pass
        return False


    ###########################################################
    def forceQuit(self):
        """Mark the app as done."""
        self._logger.warning("Forced quit - Windows logout or similar event.")
        self.wantQuit = True
        # NOTE: we called cleanup() before, but this is not right because we
        #       might be in a different thread, hence we just setting the quit
        #       flag is enough to let the run() loop exit

    ###########################################################
    def _setCameraStatus(self, camLocation, camStatus, wsgiPort=None, reason=None):
        # Determine what is the value to use for wsgi port?
        if camStatus == kCameraOff:
            wsgiPortSet = None
        elif wsgiPort is not None:
            wsgiPortSet = wsgiPort
        elif camLocation in self._cameras:
            wsgiPortSet = self._cameras[camLocation][1]
        else:
            wsgiPortSet = None

        # Check if update is actually needed
        if camLocation in self._cameras and \
            self._cameras[camLocation][0] == camStatus and \
            self._cameras[camLocation][1] == wsgiPortSet and \
            self._cameras[camLocation][2] == reason:
            return

        # Update the internal state.  A None status means "port change only,
        # keep the status" -- but the only sender (msgIdWsgiPortChanged) guards
        # on _captureStreams, which is a different dict from _cameras, so the
        # entry is not guaranteed to be here.
        if camStatus is None:
            existing = self._cameras.get(camLocation)
            if existing is None:
                self._logger.warning(
                    "Ignoring port update for camera '%s': no status recorded "
                    "for it yet" % camLocation)
                return
            realCamStatus = existing[0]
        else:
            realCamStatus = camStatus
        self._cameras[camLocation]=(realCamStatus, wsgiPortSet, reason)

        # Update the NMS
        self._netMsgServerClient.setCameraStatus(camLocation, camStatus, wsgiPort, reason)

        # Update the web server
        self._putMsgWS([MessageIds.msgIdWsgiPortChanged, camLocation, wsgiPortSet])

    ###########################################################
    def _checkDatabaseIntegrity(self):
        """PRAGMA quick_check each database at startup.  Report, never repair.

        The 2026-08-04 clipdb failure was a single damaged index
        (IDX_CLIPS_FILENAME_CAMLOC).  Nothing noticed: the corruption marker
        file is only written once something has already thrown, so a database
        that rots between runs starts up looking fine and fails later, in the
        middle of ordinary use.  quick_check is the cheap version of
        integrity_check -- it skips the expensive index-vs-table cross
        validation but still catches damaged pages -- so it is affordable on
        every start.

        Deliberately does NOT recover.  A blind rebuild destroys the evidence
        that makes a cheap repair possible (which tree is damaged, how many
        rows still read), and the user asked for checks WITHOUT automatic
        fixes on restart.
        """
        checks = (('clipdb', self._clipDbPath),
                  ('objdb', self._objDbPath),
                  ('responsedb', self._responseDbPath))
        bad = []
        for name, path in checks:
            if not path or not os.path.exists(path):
                self._logger.info("integrity check: %s not present yet (%s)"
                                  % (name, path))
                continue
            t0 = time.time()
            try:
                conn = sqlite3.connect(path)
                try:
                    rows = [r[0] for r in conn.execute("PRAGMA quick_check(5)")]
                finally:
                    conn.close()
            except Exception as e:
                rows = ["check itself failed: %s" % e]
            took = time.time() - t0
            if rows == ['ok']:
                self._logger.info("integrity check: %s ok (%.1fs)"
                                  % (name, took))
            else:
                detail = "; ".join(rows[:5])
                bad.append((name, path, detail))
                self._logger.critical(
                    "INTEGRITY CHECK FAILED for %s (%s): %s.  Starting anyway "
                    "-- nothing will be repaired automatically.  Stop the app "
                    "and inspect before this costs you footage." %
                    (name, path, detail))
                noteCorruption(path, detail, 'PRAGMA quick_check',
                               logger=None)
        self._dbIntegrityAtStart = bad

        # The flag is what stops DiskCleaner destroying unregistered footage,
        # so raise it on any failure -- and clear it ONLY on a clean sweep of
        # every database, which is the one piece of evidence that says the
        # files are sound again.  Never clear it because a symptom stopped.
        if bad:
            self._raiseCorruptionFlag(
                "; ".join("%s: %s" % (n, d) for n, _p, d in bad),
                source='startup integrity check')
        elif readCorruptionFlag(self._userLocalDataDir):
            self._logger.info(
                "all databases pass integrity check; clearing the corruption "
                "flag and re-enabling the orphan sweep")
            clearCorruptionFlag(self._userLocalDataDir, self._logger)
        return bad


    ###########################################################
    def _raiseCorruptionFlag(self, detail, source, tellUser=True):
        """Raise the cross-process 'do not delete anything' flag.

        Detection stays detection: this writes a flag and tells the user what
        to do.  It does not repair, and it does not stop recording -- footage
        keeps landing on disk so nothing is lost while the operator decides.

        @param  detail    What went wrong, in one line.
        @param  source    Which check noticed; recorded on the flag.
        @param  tellUser  False to raise the flag WITHOUT claiming damage on
                          screen.  The flag does two jobs -- it is DiskCleaner's
                          interlock as well as the damage report -- and they do
                          not always coincide.  A stalled clip registration
                          means unregistered footage is at risk and the orphan
                          sweep has to stop, but it is a suspicion, not
                          evidence: telling the user their database is damaged
                          when quick_check says it is fine is simply wrong, and
                          it sent them to repair_clipdb.py for nothing.
        """
        writeCorruptionFlag(self._userLocalDataDir, {
            'error': detail,
            'source': source,
            'orphanSweepSuspended': True,
        }, self._logger)

        if not tellUser:
            self._logger.warning(
                "orphan sweep SUSPENDED (%s): %s.  The databases pass their "
                "integrity check, so this is not being reported as damage -- "
                "but nothing unregistered will be deleted until clips are "
                "landing again." % (source, detail))
            return

        # Once per RUN, not once per flag file.  The flag survives a restart by
        # design, so keying off "was the file already there" means a user who
        # restarts into a still-damaged database is told nothing at all -- which
        # is exactly what happened on 2026-08-08.
        first = not self._corruptionAlerted
        self._corruptionAlerted = True
        if first:
            self._logger.critical(
                "DATABASE PROBLEM (%s): %s.  Footage is still being recorded. "
                "DiskCleaner's orphan sweep is now SUSPENDED so nothing "
                "unregistered gets deleted, which means disk use will grow "
                "until this is repaired.  To repair: stop the app and run "
                "scripts\\repair_clipdb.py (it rebuilds into a fresh file and "
                "is lossless -- the last two incidents recovered 48,261 and "
                "40,869 rows with none lost)." % (source, detail))
            # ...and put it ON SCREEN.  A CRITICAL in a log file is not an
            # alert: on 2026-08-08 detection fired correctly at 05:06 and the
            # first anyone knew of it was hours later.  The message pipe is the
            # same one the out-of-disk-space warning uses.
            self._tellUserDatabaseDamaged(detail, source)


    ###########################################################
    def _clearStallFlagIfSound(self):
        """Lower a flag the stall watchdog raised, once the files prove sound.

        Evidence lowers this flag, never the symptom going quiet -- that is the
        rule clearCorruptionFlag() states in appCommon/DbRecovery.py.  So this
        does not delete anything itself: it re-runs the real check and lets
        _checkDatabaseIntegrity() decide, which already clears the flag on a
        clean sweep of every database and re-raises it on a dirty one.

        Without this the flag outlives the problem it describes.  The only
        other thing that lowers it is the check that runs at back-end START, so
        a stall which resolves on its own leaves the flag raised until the next
        restart -- and the front end greets the user with "a database is
        damaged" on every launch in between.  A reboot reproduces it: on
        2026-08-23 the cameras took 21 minutes to land their first clip after a
        restart, the flag went up at 20:42:19, clips resumed 72 seconds later,
        and the dialog kept appearing for hours with both databases verified
        intact by quick_check, integrity_check and foreign_key_check.

        Only touches a flag THIS symptom raised.  A live query failure or a
        failed startup check records its own source and has to stand until
        somebody actually deals with it.

        Note this refreshes _dbIntegrityAtStart, so the health view starts
        reporting the newest full check rather than the one from start-up.
        """
        flag = readCorruptionFlag(self._userLocalDataDir)
        if not flag:
            return

        source = flag.get('source')
        if source != _kStallFlagSource:
            self._logger.info(
                "clips are flowing again, but the corruption flag came from "
                "'%s' -- leaving it raised", source)
            return

        self._logger.info(
            "clips are flowing again; re-checking the databases before "
            "lowering the corruption flag")
        if self._checkDatabaseIntegrity():
            self._logger.critical(
                "clips are flowing again but a database still fails its "
                "integrity check -- the corruption flag stays up")


    ###########################################################
    def _tellUserDatabaseDamaged(self, detail, source):
        """Raise an on-screen alert about a damaged database.

        NOT msgIdDatabaseCorrupt -- that one closes the app and leads to the
        recover/reset dialog, and Reset deletes every clip.  Nothing here
        changes any file; it only tells the user what happened and what to run.
        """
        # One line is all a dialog can usefully show; quick_check's tail is a
        # long list of "never used" pages that says nothing extra.
        firstLine = str(detail).replace('*** in database main ***', '')
        firstLine = [ln for ln in firstLine.splitlines() if ln.strip()]
        firstLine = firstLine[0].strip() if firstLine else str(detail)

        # The startup integrity check runs long before _netMsgServerClient
        # exists, so the most important alert of all -- "you just restarted and
        # the database is STILL damaged" -- would otherwise be thrown away.
        # Hold it and let _pushDatabaseHealth send it on the liveness timer.
        self._pendingDamageAlert = (firstLine, str(source))
        self._sendPendingDamageAlert()


    ###########################################################
    def _sendPendingDamageAlert(self):
        """Deliver a held on-screen alert, if the message client is up yet."""
        pending = getattr(self, '_pendingDamageAlert', None)
        # getattr, not a plain attribute: the client is only assigned once IPC
        # comes up, and the startup check can fire before that.
        if not pending or getattr(self, '_netMsgServerClient', None) is None:
            return
        try:
            self._netMsgServerClient.addMessage(
                [MessageIds.msgIdDatabaseDamaged, pending[0], pending[1]])
            self._pendingDamageAlert = None
        except Exception:
            # Keep it pending: the next tick tries again.
            self._logger.warning("could not send the on-screen database alert",
                                 exc_info=True)


    ###########################################################
    def _pushDatabaseHealth(self):
        """Surface database damage to the health view, on the normal timer.

        Two sources: the startup quick_check, and anything TimedCursor tripped
        over since (which is how a database that goes bad WHILE running gets
        noticed -- the case that cost ~2 hours of unregistered clips).
        """
        self._sendPendingDamageAlert()
        try:
            reports = getCorruptionReports()
            # Anything we tripped over while RUNNING must raise the flag too --
            # the startup check cannot see a database that goes bad later, and
            # that is the case that cost 6.28 GB of unregistered footage.
            # Reports never expire, so only act when the SET of damaged files
            # changes: this runs on the liveness timer and must not rewrite the
            # flag file every few seconds forever.
            if reports and frozenset(reports) != self._corruptionFlagRaisedFor:
                self._corruptionFlagRaisedFor = frozenset(reports)
                self._raiseCorruptionFlag(
                    "; ".join("%s: %s" % (os.path.basename(p),
                                          r.get('error', '?'))
                              for p, r in list(reports.items())[:3]),
                    source='live query failure')
            flag = readCorruptionFlag(self._userLocalDataDir)
            info = {
                'flagged': bool(flag),
                'orphanSweepSuspended': bool(flag),
                'flagDetail': (flag or {}).get('error', ''),
                'corrupt': [
                    {'path': p,
                     'error': r.get('error', ''),
                     'errors': r.get('count', 0),
                     'sinceMs': float(r.get('first', 0) * 1000.0)}
                    for p, r in reports.items()
                ],
                'checkedAtStart': [n for n, _p, _d in
                                   getattr(self, '_dbIntegrityAtStart', [])],
                'lastClipAddedMs': float(self._lastClipRegisteredMs or 0),
            }
            self._netMsgServerClient.setHealthInfo('database', info)
        except Exception:
            pass


    ###########################################################
    def _checkClipRegistration(self):
        """Warn when cameras are recording but nothing reaches the clip DB.

        This is the symptom that went unnoticed on 2026-08-04: writes started
        failing at 10:32, recording carried on for nearly two hours, and 253
        finished clips (2.2 GB) sat in the archive unregistered -- invisible to
        Search, and due to be deleted by DiskCleaner's nightly orphan sweep.
        Any cause produces the same symptom, so watch the symptom.
        """
        running = len([1 for entry in self._captureStreams.values()
                       if entry and entry[0] is not None])
        if not running:
            # Nothing is recording, so "no clips arrived" is the expected
            # state, not a symptom -- keep the baseline AT NOW.  Pinning it to
            # the first idle tick instead (the old `or`) counted the whole
            # disarmed period as silence, so the alarm fired the moment the
            # cameras came back: on 2026-08-09 the app sat disarmed from
            # 16:03 to 16:16, and a false "database is damaged" popped up six
            # seconds after Arm Cameras, with the clipdb verified intact.
            self._lastClipRegisteredMs = int(time.time() * 1000)
            return
        nowMs = int(time.time() * 1000)
        newest = None
        for location in list(self._captureStreams.keys()):
            try:
                t = self._clipManager.getMostRecentTimeAt(location)
            except Exception:
                # A failing query here is itself the signal, and TimedCursor
                # has already recorded it -- don't let it kill the timer.
                continue
            if t and t > 0 and (newest is None or t > newest):
                newest = t
        if newest:
            self._lastClipRegisteredMs = max(self._lastClipRegisteredMs or 0,
                                             int(newest))
        if not self._lastClipRegisteredMs:
            self._lastClipRegisteredMs = nowMs
            return

        # Never let the baseline predate this RUN.  getMostRecentTimeAt()
        # reports the newest clip in the DATABASE, which on a fresh start is
        # one from the previous session -- so the silence it measures includes
        # the whole time the app was not running.  On 2026-08-25 an install
        # took the app down for twenty minutes and the alarm fired NINE SECONDS
        # after the back end came up, on a system that then registered its
        # first clip a minute later, perfectly healthy.  Same shape as the
        # disarmed-period bug handled above, and it fires after any restart
        # that took longer than _kClipRegistrationWarnSecs -- every install,
        # every overnight shutdown.
        #
        # This hides downtime only.  Silence AFTER we started still counts, so
        # a back end that has been up for half an hour with nothing registered
        # is reported exactly as before.
        startedMs = int(self._startTimeSecs * 1000)
        if self._lastClipRegisteredMs < startedMs:
            self._lastClipRegisteredMs = startedMs

        quietSecs = (nowMs - self._lastClipRegisteredMs) / 1000.0
        if quietSecs > _kClipRegistrationWarnSecs:
            if not self._clipRegistrationWarned:
                self._clipRegistrationWarned = True
                self._logger.critical(
                    "NO CLIP has reached the database in %.0f minutes while %d "
                    "camera(s) are recording.  Footage is being written to the "
                    "archive but never registered -- it will not appear in "
                    "Search." % (quietSecs / 60.0, running))
                # A stall is a SUSPICION, not evidence.  It is worth acting on
                # -- a camera process has its own database handle, so it can be
                # the one hitting damage while this process' queries still
                # succeed, and then this is the only signal we get -- but the
                # way to act on it is to LOOK, not to announce.  This used to
                # tell the user their database was damaged on the strength of
                # the symptom alone; on 2026-08-23 a reboot took 21 minutes to
                # land the first clip and produced exactly that claim, with all
                # three databases verified intact and the user sent to
                # repair_clipdb.py for nothing.
                #
                # _checkDatabaseIntegrity() raises the flag itself, with the
                # real source, when a database actually is damaged -- and
                # clears a stale one when they all pass.
                if not self._checkDatabaseIntegrity():
                    # Sound files, so this is not damage.  Footage IS going
                    # unregistered though, and the orphan sweep would delete it
                    # (2026-08-04: 253 clips), so hold the interlock quietly.
                    self._raiseCorruptionFlag(
                        "no clip registered in %.0f minutes while %d cameras "
                        "record" % (quietSecs / 60.0, running),
                        source=_kStallFlagSource, tellUser=False)
        elif self._clipRegistrationWarned:
            self._clipRegistrationWarned = False
            self._logger.info("clips are reaching the database again")
            self._clearStallFlagIfSound()


    ###########################################################
    def _pushHealthProcessMap(self):
        """Tell the NMS which PIDs make up the app, for the health view.

        Only the map is sent; the NMS samples memory/CPU itself when the view
        asks, so nothing is measured unless someone is looking.

        Our start time rides along on the same tick.  It never changes, but
        re-sending it costs nothing and means the value re-establishes itself
        if the message server is ever restarted under us.
        """
        procs = {'Back end': os.getpid()}
        for label, proc in (('Response runner', self._responseRunnerProc),
                            ('Detection service', self._detectionServiceProc),
                            ('Disk cleaner', self._diskCleanupProc),
                            ('Web server', self._webServerProc),
                            ('Message server', self._netMsgServerProc)):
            try:
                if proc is not None and proc.pid:
                    procs[label] = proc.pid
            except Exception:
                pass
        for location, entry in list(self._captureStreams.items()):
            try:
                proc = entry[0]
                if proc is not None and proc.pid:
                    procs['Camera: %s' % location] = proc.pid
            except Exception:
                pass
        try:
            self._netMsgServerClient.setHealthInfo('procs', procs)
        except Exception:
            pass
        try:
            # float, not int: XML-RPC ints are 32-bit and epoch ms overflow.
            self._netMsgServerClient.setHealthInfo(
                'started', float(self._startTimeSecs * 1000.0))
        except Exception:
            pass


    ###########################################################
    def _enableDiskLogging(self, enable):
        if enable:
            self._logger.enableDiskLogging()
            if self._onvifLogger:
                self._onvifLogger.enableDiskLogging()
            if self._upnpLogger:
                self._upnpLogger.enableDiskLogging()
        else:
            self._logger.disableDiskLogging()
            if self._onvifLogger:
                self._onvifLogger.disableDiskLogging()
            if self._upnpLogger:
                self._upnpLogger.disableDiskLogging()

    ###########################################################
    def _updateAnalyticsPort(self, msg):
        """ Update camera processes with the new analytics port
        """
        self._analyticsPort = msg[1]
        self._broadcastMsg(msg)

    ###########################################################
    def _getQueueMessage(self, timeout):
        # Attempt to empty all of the shared queue first
        while True:
            try:
                msg = self._childProcQueue.get(False)
                msgEx = (msg, time.time())
                self._childProcLocalQueue.append(msgEx)
            except QueueEmpty:
                break

        while self._delayedMessagesQueue.qsize()>0:
            procTime, msg = self._delayedMessagesQueue.queue[0]
            if getTimeAsMs() >= procTime:
                # re-get the item, to remove it, and deposit for processing
                procTime, msg = self._delayedMessagesQueue.get()
                self._childProcLocalQueue.append((msg, procTime/1000.))
            else:
                # do not make delayed messages wait longer than necessary
                timeout = min(timeout, (procTime - getTimeAsMs())/1000.)
                # no point in processing the rest of them
                break

        # If we have at least one message in the local queue, get it
        if len(self._childProcLocalQueue) > 0:
            msg, depositTime = self._childProcLocalQueue.popleft()
            return (msg, time.time() - depositTime)

        # Time to wait for a message from the far end
        return ( self._childProcQueue.get(timeout=timeout), 0 )

    ###########################################################
    def _getQueueSize(self):
        return len(self._childProcLocalQueue)

    ###########################################################
    def run(self): #PYCHECKER too many lines OK
        """Run the back end application."""
        self._dbg("run() called")
        self._logger.info("Starting the back end, pid: %d" % os.getpid())
        self._logger.info("SSL version " + str(ssl.OPENSSL_VERSION))

        if self.alreadyRunning():
            self._logger.info("Back end already running.")
            self.wantQuit = True
            self._cleanedUp = True  # nothing to clean up
            return



        # Open a messaging queue for child processes to post to
        self._childProcQueue = Queue(0)
        self._dbg("childProcQueue created")

        # We load prefs and rules before we call _openIPC because once we
        # create the NetworkMessageServer it isn't guaranteed to be safe to
        # open these files.
        self._dbg("loading prefs...")
        prefs = BackEndPrefs(os.path.join(self._userLocalDataDir, kPrefsFile))
        self._dbg("prefs loaded")
        self._maxStorage = prefs.getPref("maxStorageSize")
        self._cacheDuration = prefs.getPref("cacheDuration")
        self._recordInMemory = prefs.getPref(kRecordInMemory)
        self._clipMergeThreshold = prefs.getPref(kClipMergeThreshold)
        self._hardwareDevice = prefs.getPref(kHardwareAccelerationDevice)
        self._dataStorageLocation = prefs.getPref('dataDir')
        self._timePrefs = ( prefs.getPref('timePref12'), prefs.getPref('datePrefUS') )
        if type(self._dataStorageLocation) == bytes:
            self._dataStorageLocation = self._dataStorageLocation.decode('utf-8')

        if self._dataStorageLocation is None:
            self._dataStorageLocation = os.path.join(self._userLocalDataDir,
                                                     "videos")
            prefs.setPref("dataDir", self._dataStorageLocation)
        try:
            os.makedirs(self._dataStorageLocation)
        except Exception:
            pass

        # Database health state (detection only -- see _checkDatabaseIntegrity)
        self._dbIntegrityAtStart = []
        self._lastClipRegisteredMs = 0
        self._clipRegistrationWarned = False
        self._corruptionFlagRaisedFor = frozenset()
        self._corruptionAlerted = False

        self._webPort = prefs.getPref("webPort")
        self.videoSettings = {
            kLiveMaxBitrate:        prefs.getPref(kLiveMaxBitrate),
            kLiveEnableTimestamp:   prefs.getPref(kLiveEnableTimestamp),
            kLiveEnableFastStart:   prefs.getPref(kLiveEnableFastStart),
            kLiveMaxResolution:     prefs.getPref(kLiveMaxResolution),
            kClipResolution:        prefs.getPref(kClipResolution),
            kGenThumbnailResolution:prefs.getPref(kGenThumbnailResolution),
        }

        videoStorageLocation = prefs.getPref('videoDir')
        if type(videoStorageLocation) == bytes:            videoStorageLocation = videoStorageLocation.decode('utf-8')
        if videoStorageLocation is None:
            videoStorageLocation = os.path.join(self._userLocalDataDir,
                                                "videos")
            prefs.setPref("videoDir", videoStorageLocation)
        try:
            os.makedirs(videoStorageLocation)
        except Exception:
            pass

        self._emailSettings = prefs.getPref('emailSettings')
        self._ftpSettings = prefs.getPref('ftpSettings')
        self._notificationSettings = prefs.getPref('notificationSettings')
        self._localExportSettings = {}

        self._clipDbPath = os.path.join(self._dataStorageLocation, kClipDbFile)
        self._objDbPath = os.path.join(self._dataStorageLocation, kObjDbFile)
        self._responseDbPath = os.path.join(self._dataStorageLocation,
                                            kResponseDbFile)
        self._videoDir = os.path.join(videoStorageLocation, kVideoFolder)
        self._tmpDir = os.path.join(self._dataStorageLocation, _kTmpFolder)
        self._remoteDir = os.path.join(self._userLocalDataDir, kRemoteFolder)

        # Clean up data that shouldn't be around anymore
        self._removeTmpFiles()

        self._removeEmptyDirs(self._tmpDir)
        self._removeEmptyDirs(self._remoteDir)

        try:
            os.makedirs(self._remoteDir)
        except Exception:
            pass

        # Run the database recovery, but only if it has been confirmed.
        dbCorruptStatus = getCorruptDatabaseStatus(self._userLocalDataDir,
                                                   self._logger)
        if dbCorruptStatus:
            self._logger.info("DB corruption status %s" % str(dbCorruptStatus))
            if kStatusRecover == dbCorruptStatus[0]:
                runDatabaseRecovery(self._userLocalDataDir,
                                    self._dataStorageLocation,
                                    self._logger,
                                    False)
            elif kStatusReset == dbCorruptStatus[0]:
                runDatabaseRecovery(self._userLocalDataDir,
                                    self._dataStorageLocation,
                                    self._logger,
                                    True)
        else:
            # No corruption marker from a previous run -- but that marker is
            # only written when something already blew up.  Actually LOOK at
            # the files, which is what the old TODO here asked for.  Detection
            # only: a bad result is logged and surfaced, never repaired behind
            # the user's back (2026-08-04).
            self._checkDatabaseIntegrity()

        # Get the databases ready.
        self._dbg("opening responseDb...")
        self._responseDb = ResponseDbManager(self._logger)
        self._responseDb.open(self._responseDbPath)
        self._dbg("responseDb opened")

        try:
            self._dbg("openDatabases...")
            self._openDatabases()
            self._dbg("openDatabases done")
        except DatabaseError as e:
            self._logger.error("Couldn't open databases.", exc_info=True)
            if str(e) in kCorruptDbErrorStrings:
                self._handleCorruptDatabase()
            else:
                raise

        # Make response runner queue.  Must be done before loading rules...
        self._responseRunnerQueue = Queue(0)

        # Get the camera configuration. We just need the state right here, the
        # actual read/write manager instance lives in the NMS.
        self._dbg("CameraManager...")
        camDb = os.path.join(self._userLocalDataDir, kCamDbFile)
        cm = CameraManager(self._logger, camDb)
        self._dbg("CameraManager done")

        # Start the licensing manager.
        licenseSettings = prefs.getPref('licenseSettings')
        self._licenseManager = self._openLicenseManager(licenseSettings)

        # Make sure the cameras comply with the license.
        self._syncCamerasWithLicense(cm)

        # Open IPC.  Must be done before response runner, since the RR might
        # stick something in our queue (and so can Platform wrapper)...
        self._dbg("Opening IPC...")
        self._logger.info("Opening IPC")
        ipcOK, ipcPorts = self._openIPC()
        self._dbg(f"_openIPC returned: ipcOK={ipcOK}, ports={ipcPorts}")
        if not ipcOK:
            return
        # Start the analytics
        self._dbg("initPlatformHTTPWrapper...")
        self._initPlatformHTTPWrapper()
        self._dbg("setNmsClient...")
        # Now we link the NMS to the license manager, so it gets told about
        # what happened during license setup...
        self._licenseManager.setNmsClient(self._netMsgServerClient)
        self._dbg("loadRules...")
        # Now we have enough things together to load the rules.
        self._loadRules(cm)
        self._dbg("initResponseRunner...")
        # Startup the response runner.
        self._initResponseRunner()
        self._dbg("initDetectionService...")
        # Start the shared ML detection service (one model copy / one CUDA
        # context serving all cameras — cameras no longer load models).
        self._initDetectionService()
        self._dbg("memstorePut ready...")
        # Declare ourselves to be ready for the frontend.
        try:
            self._netMsgServerClient.memstorePut(kMemStoreBackendReady, True, -1)
            self._dbg("backend READY")
        except Exception:
            import traceback as _tb
            self._dbg("memstorePut FAILED: " + _tb.format_exc())
            raise

        # Initialize the video streams and disk cleanup
        self._dbg("initVideoStreams...")
        self._initVideoStreams(cm)
        self._dbg("initDiskCleanup...")
        self._initDiskCleanup(self._maxStorage)

        # Initialize the LAN record-viewer web server.  This is a self-hosted,
        # LAN-only feature in this build (no cloud, no NAT traversal), so it is
        # governed solely by the user's port setting rather than the paid-
        # edition gate that guarded the old internet remote-access product.
        webPort = prefs.getPref('webPort')

        self._dbg("initWebServer...")
        self._initWebServer(webPort, prefs.getPref('webAuth'))
        self._dbg("initWebServer done")

        # No idles are pending...
        idlePendingSince = None

        # Debugging info for slow loops...
        loopCount = 0
        curTime = time.time()

        # ONVIF LAN camera discovery is OFF by default.  It's a constant
        # background network scan (wakes every couple seconds, probes the LAN,
        # polls every device it finds) whose ONLY purpose is auto-listing
        # cameras in the Add-Camera wizard -- it is NOT needed for recording
        # (cameras stream over RTSP independently) and left on it continuously
        # grows Onvif.log.  So it's started ON DEMAND when the wizard runs (see
        # msgIdActiveCameraSearch) and torn down when idle.  Opt into always-on
        # discovery by creating an empty file named "enableOnvif" in the user
        # data dir.
        self._dbg("initOnvif...")
        self._onvifPersistent = os.path.isfile(
            os.path.join(self._userLocalDataDir, "enableOnvif"))
        if self._onvifPersistent:
            self._startOnvifScanner()
        else:
            self._logger.info("ONVIF scanner off by default; starts on demand "
                              "for the Add-Camera wizard (create 'enableOnvif' "
                              "to keep it always on)")
        self._dbg("initOnvif done")

        # UPnP camera discovery: same story as ONVIF above -- off by default,
        # started on demand for the wizard, always-on via an "enableUpnp" file.
        self._dbg("initUpnp...")
        self._upnpPersistent = os.path.isfile(
            os.path.join(self._userLocalDataDir, "enableUpnp"))
        if self._upnpPersistent:
            self._startUpnpScanner()
        else:
            self._logger.info("UPNP scanner off by default; starts on demand "
                              "for the Add-Camera wizard (create 'enableUpnp' "
                              "to keep it always on)")
        self._dbg("initUpnp done")
        self._dbg("entering main loop")

        while not self.wantQuit:
            loopCount += 1
            try:
                queueMsg = None

                if self.checkForServiceShutdown():
                    self._logger.info("Shutdown flag found set in the service.")
                    self.wantQuit = True
                    continue

                isIdleNeeded = self._isIdleTimeNeeded()
                isIdle = False

                if isIdleNeeded:
                    timeout = 0
                else:
                    timeout = 2

                msgId = -1
                try:
                    queueMsg, timeInQueue = self._getQueueMessage(timeout=timeout)
                    qsize = self._getQueueSize()
                    msgId = queueMsg[0]
                    start = time.time()
                    self._processQueueMessage(queueMsg)
                    timeToProcess = time.time() - start
                    self._childProcQueueStats.update(qsize, msgId, timeInQueue, timeToProcess)
                except DatabaseError as e:
                    self._logger.error("Process message exception: %s", traceback.format_exc())
                    if str(e) in kCorruptDbErrorStrings:
                        raise
                except QueueEmpty:
                    if timeout == 0:
                        isIdle = True
                except Exception:
                    self._logger.error("Process message exception: %s", traceback.format_exc())

                lastTime = curTime
                curTime = time.time()

                self._sendIftttState(prefs)

                if isIdleNeeded:
                    if not isIdle:
                        if idlePendingSince is None:
                            idlePendingSince = curTime
                        elif (curTime - idlePendingSince > _kMaxIdleDelay):
                            self._logger.warning(
                                "BackEnd never idle; forced: %.2f "
                                "(%d loops, last ID: %d, last loop: %.2f)" %
                                (curTime - idlePendingSince, loopCount,
                                 msgId, curTime - lastTime))
                            isIdle = True
                    if isIdle:
                        idlePendingSince = None
                        self._doIdleProcessing()

                # Flush any responses that might be waiting...
                self._flushResponses()

                # Tear down on-demand ONVIF/UPnP discovery once the Add-Camera
                # wizard stops asking for searches.
                self._maybeStopIdleScanners(curTime)

                # Restart any cameras that have unexpectedly terminated
                if curTime > self._lastCameraCheck+_kCameraCheckInterval:
                    locations = list(self._captureStreams.keys())

                    # If we've been sleeping we don't want to terminate cameras
                    # immediately on awake.
                    if self._lastCameraCheck and \
                       curTime > self._lastCameraCheck+_kCameraTimeout/2:
                        # If we haven't run through here in more than half of
                        # the camera timeout we've either been sleeping or we're
                        # really really behind on messages...increase the ping
                        # times for our cameras so they don't get shut down.
                        delay = curTime - self._lastCameraCheck
                        self._logger.warning("Big delay (%.1f sec) in main loop. Was asleep?" % ( delay ) )
                        for location in locations:
                            p, pipe, pipeId, _ = self._captureStreams[location]
                            self._captureStreams[location] = (p, pipe, pipeId, curTime)
                            # Same reasoning for the no-progress watchdog: no
                            # camera reported while we were away, so every one
                            # of them looks stalled.  Give them all a fresh
                            # start rather than restarting the whole fleet at
                            # once on wake.  Only ones already armed (a real
                            # timestamp) -- None means "has never reported",
                            # and must stay that way.
                            if self._cameraProgressTime.get(location) is not None:
                                self._cameraProgressTime[location] = curTime

                        self._lastResponseRunnerPing = curTime

                    self._lastCameraCheck = curTime
                    deadCameras = []

                    if not self._responseRunnerProc.is_alive() or \
                       self._processTimedOut(None):
                        if self._responseRunnerProc.is_alive():
                            self._responseRunnerProc.terminate()
                            self._logger.warning("terminated ResponseRunner")
                        else:
                            self._logger.info("ResponseRunner not alive, restarting")
                        self._initResponseRunner()

                    if self._detectionServiceProc is not None and \
                       not self._detectionServiceProc.is_alive():
                        self._logger.info(
                            "DetectionService not alive, restarting")
                        self._initDetectionService()

                    for location in locations:
                        p, _, _, _ = self._captureStreams[location]
                        # A camera that has reported at least once and then
                        # stopped advancing is wedged, even though it is alive
                        # and still pinging.  Only cameras with a recorded
                        # progress time are eligible: one that has never
                        # reported is still starting up (or failing, which
                        # _processTimedOut already owns).
                        stalledFor = 0
                        lastProgress = self._cameraProgressTime.get(location)
                        if lastProgress is not None:
                            stalledFor = curTime - lastProgress
                        noProgress = stalledFor > _kCameraNoProgressTimeout
                        if noProgress:
                            self._logger.warning(
                                "%s alive but has reported no processed data "
                                "for %.0f sec; restarting wedged camera"
                                % (ensureUnicode(location), stalledFor))
                        if not p.is_alive() or self._processTimedOut(location) \
                           or noProgress:
                            if p.is_alive():
                                self._terminateCameraProcess(p)
                                self._logger.warning("terminated %s" % ensureUnicode(location))
                            self._delCaptureStream(location)
                            deadCameras.append(location)
                            self._logger.info("%s not alive, restarting" % ensureUnicode(location))
                            # Tally it so the health view can show which cameras
                            # are flaky (previously this was only ever logged).
                            try:
                                self._netMsgServerClient.noteCameraReconnect(location)
                            except Exception:
                                pass
                            self._noteCameraDeath(location, curTime)

                    # Refresh the PID map the health view samples RAM/CPU from.
                    # Done on the same slow timer as the liveness check, since
                    # PIDs only change when a process restarts.
                    self._pushHealthProcessMap()

                    # Watch for a database that has gone bad WHILE running --
                    # the startup quick_check cannot see that.  Detection only;
                    # nothing here repairs anything (2026-08-04).
                    self._checkClipRegistration()
                    self._pushDatabaseHealth()

                    for location in self._cameraInfo:
                        self._syncCameraStateWithSchedule(location)

                    # If a camera was dead we don't want the 'connecting...'
                    # screen to display in the monitor view, as it might be in a
                    # dead loop. Skip straight to "could not connect" instead.
                    for cam in deadCameras:
                        self._setCameraStatus(cam, kCameraFailed)

                    pipeIds = list(self._deadPipes.keys())
                    for pipeId in pipeIds:
                        if self._deadPipes[pipeId] < curTime:
                            del self._deadPipes[pipeId]
                            if pipeId in self._dataMgrPipes:
                                del self._dataMgrPipes[pipeId]
                                del self._tempIdMap[pipeId]

                    # Ensure that the disk cleaner is still running
                    if not self._diskCleanupProc.is_alive():
                        self._logger.warning("Disk cleaner not running, restarting")
                        self._initDiskCleanup(self._maxStorage)

                    # Ensure that the platform wrapper is still running
                    if not self._platformHTTPWrapperProc or \
                        not self._platformHTTPWrapperProc.is_alive():
                        self._logger.warning("Platform wrapper is not running, restarting")
                        self._initPlatformHTTPWrapper()

                    # Ensure that the LAN record viewer is still running.  Every
                    # other child gets this; the web server was the one omission,
                    # so a crash left the viewer dark for the rest of the run.
                    if not self._webServerProc or \
                        not self._webServerProc.is_alive():
                        self._logger.warning("Web server is not running, restarting")
                        self._initWebServer(self._webPort, self._webAuth)

                    # Give the license manager opportunity to do things.
                    try:
                        self._licenseManager.run()
                    except:
                        self._logger.error("license manager run error (%s)" %
                                           sys.exc_info()[1])
                        self._logger.error(traceback.format_exc())

                    # Periodically force the network message server to clear out
                    # the pending process updated queue.
                    try:
                        self._netMsgServerClient.updateCameraProgress()
                    except sockettimeout:
                        raise
                    except Exception as e:
                        # Not sure what we were catching here before, or whether
                        # it is still relevant... We now have threaded xmlrpc
                        # servers and timeout sockets... want to take this out
                        # but should ensure it isn't still necessary first.
                        #
                        # Follow up 10/8/2014 - ticket 11457
                        #     <class 'socket.error'>:
                        #         (10055, 'No buffer space available')
                        self._logger.error(
                                "Tell Ryan if you see this - eating " + str(e))

                    # Clean up any camera data hanging around as necessary.
                    for loc in list(self._pendingRuleCleanupDict.keys()):
                        if self._pendingRuleCleanupDict[loc] < curTime:
                            self._cleanupCameraData(loc)

                    # Clean up any dead camera processes
                    for i in range(len(self._deadCameras)-1, -1, -1):
                        proc, quitTime = self._deadCameras[i]
                        if quitTime + _kRuleCleanupTimeout < curTime:
                            if proc.is_alive():
                                self._terminateCameraProcess(proc)
                            self._deadCameras.pop(i)

                    # If a camera was supposed to alert us to a rename but was
                    # frozen and never did, execute it now.
                    if self._pendingRenameMsg and \
                        self._pendingRenameMsg[0] < curTime:
                            self._logger.warning("Rename never processed, "
                                                 "forcing now")
                            _, proc, msg = self._pendingRenameMsg
                            # Execute the rename.
                            self._processQueueMessage(msg)
                            # Terminate the old process.  We assume it never
                            # quit since it never gave us back the rename.
                            self._terminateCameraProcess(proc)

                if curTime>self._lastMessageServerPing+_kMessageServerPingTime:
                    self._lastMessageServerPing = curTime
                    self._netMsgServerClient.backEndPing()
                    self._moveTmpFiles()

            except DatabaseError as e:
                if str(e) in kCorruptDbErrorStrings:
                    self._handleCorruptDatabase()
                else:
                    raise


    ###########################################################
    def _delCaptureStream(self, location):
        """Safely delete the given capture stream.

        This makes sure to add the pipe to self._deadPipes so we don't get
        any leaks.

        @param location  The location to delete.
        """
        _, _, pipeId, _ = self._captureStreams[location]

        if pipeId in self._dataMgrPipes:
            self._deadPipes[pipeId] = time.time() + _kPipeCleanupWait

        del self._captureStreams[location]

        # Forget when this camera last made progress.  The replacement process
        # starts with nothing reported, and the no-progress watchdog treats a
        # missing entry as "not eligible yet" -- without this, the stale
        # timestamp from the process we just killed would still be stalled on
        # the next pass and would restart the fresh camera immediately, over
        # and over.
        if location in self._cameraProgressTime:
            del self._cameraProgressTime[location]


    ###########################################################
    def _isIdleTimeNeeded(self, force=False):
        """Return true if idle time is needed for processing.

        @param  force             If True, we'll return True if there are any
                                  searches pending, even if we've done one
                                  recently.
        @return isIdleTimeNeeded  True if idle time processing is needed.
        """
        return self._pendingAddFrames or self._wantRealtimeSearch(force)


    ###########################################################
    def _wantRealtimeSearch(self, force=False):
        """Return true if we'd like to do a realtime search now.

        @param  force               If True, we'll return True if there are any
                                    searches pending, even if we've done one
                                    recently.
        @return wantRealtimeSearch  True if idle time is needed.
        """
        if self._pendingRealTimeSearches:
            # We normally wait until we've accumulated 1 second of motion data
            # before doing a search.  This is for efficiency reasons...
            for camName, ms in self._pendingRealTimeSearches.items():
                lastSearchTime = self._lastSearchTimes.get(camName, 0)
                dt = ms - lastSearchTime
                if dt > _kMinimumSearchDelayMs:
                    return True

            # We don't get given any new motion data after an object has left
            # the scene; thus, we need some extra logic here to catch the case
            # where we haven't seen motion data in a while...
            nowTime = time.time()
            timeSinceLast = nowTime - self._lastRealtimeSearch
            if timeSinceLast > _kStaleRealtimeSearchSeconds:
                return True

            # Also return True if we're forced.  Check this last, since it's
            # uncommon...
            if force:
                return True

        return False

    ###########################################################
    def _updateCameraUri(self, camLoc, protocol):
        uri, _, _, _ = self._cameraInfo[camLoc]
        newUri = self._realizeUri(uri)
        if newUri is None or newUri == "":
            # log a warning, but do not disrupt camera that is potentially running
            self._logger.warning(protocol + " search for '" + camLoc + "' returned empty URI")
        else:
            # Send a notification to camera process, so it can update the URI upon next connection
            self._logger.debug(protocol + " URI for '" + camLoc + "' is updated from " + uri + " to " + newUri)
            self._sendMsgLoc([MessageIds.msgIdCameraUriUpdated, newUri], camLoc)

    ###########################################################
    def _updateOnvif(self, allDevices, changedUuids, goneUuids):
        """Update ONVIF when the scanner tells us to
        """
        self._onvifDevices = allDevices
        # for dev in allDevices.keys():
        #     self._logger.info("Got device %s" % str(allDevices[dev]))

        if changedUuids or goneUuids:

            self._netMsgServerClient.setOnvifDevices(xmlrpc.client.Binary(pickle.dumps(allDevices)))

            self._logger.info("ONVIF UUIDs changed: %s, gone: %s" %
                              (str(changedUuids), str(goneUuids)))

            # Add gone UUIDs to changed ones...
            changedUuids |= goneUuids

            # Disable / reenable any affected cameras...
            for camLoc, _ in self._cameraInfo.items():
                uri, enabled, _, _ = self._cameraInfo[camLoc]

                if enabled and isOnvifUrl(uri):
                    uuid = extractUuidFromOnvifUrl(uri)
                    if uuid in changedUuids:
                        self._updateCameraUri(camLoc, "ONVIF")
            # Restart test camera if it's running...
            if self._testCamProc is not None:
                uri = self._testCamUri
                assert uri is not None

                if isOnvifUrl(uri):
                    uuid = extractUuidFromOnvifUrl(uri)
                    if uuid in changedUuids:
                        # Read before _stopTestCamera clears it.
                        testExtras = self._testCamExtras or {}
                        self._stopTestCamera()
                        self._startTestCamera(uri, testExtras)


    ###########################################################
    def _updateUpnp(self, allDevices, changedUsns, goneUsns):
        """Update Upnp when the scanner tells us to
        """
        self._upnpDevices = allDevices
        # for dev in allDevices.keys():
        #     self._logger.info("Got device %s" % str(allDevices[dev]))

        if changedUsns or goneUsns:
            self._netMsgServerClient.setUpnpDevices(xmlrpc.client.Binary(pickle.dumps(self._upnpDevices)))

            self._logger.debug("UPNP USNs changed: %s, gone: %s" % (str(changedUsns), str(goneUsns)))

            # Add gone USNs to changed ones...
            changedUsns |= goneUsns

            # Disable / reenable any affected cameras...
            for camLoc, _ in self._cameraInfo.items():
                uri, enabled, _, _ = self._cameraInfo[camLoc]

                if enabled and isUpnpUrl(uri):
                    usn = extractUsnFromUpnpUrl(uri)
                    if usn in changedUsns:
                        self._updateCameraUri(camLoc, "UPNP")
            # Restart test camera if it's running...
            if self._testCamProc is not None:
                uri = self._testCamUri
                assert uri is not None

                if isUpnpUrl(uri):
                    usn = extractUsnFromUpnpUrl(uri)
                    if usn in changedUsns:
                        # Read before _stopTestCamera clears it.
                        testExtras = self._testCamExtras or {}
                        self._stopTestCamera()
                        self._startTestCamera(uri, testExtras)

    ###########################################################
    def _doIdleProcessing(self, force=False):
        """Do any processing that should happen at idle time.

        This will also be called periodically even if the system isn't idle.

        This won't do _all_ queued up work that we need to do--just a piece.
        See self._flushIdleQueue().

        @param  force             If True, we'll return True if there are any
                                  searches pending, even if we've done one
                                  recently.
        """
        # Always add all pending frames.  It's important to do this before the
        # search...
        start = time.time()
        while self._pendingAddFrames:
            addFrameArgs = self._pendingAddFrames.popleft()
            self._dataManager.addFrame(*addFrameArgs)
        self._dataManager.save()
        self._childProcQueueStats.update(None, _kFakeMessageIdDataManagerIdleProcessing, None, time.time()-start)

        if self._wantRealtimeSearch(force):
            # Keep track of the fact that we've now done a realtime search...
            self._lastRealtimeSearch = time.time()

            # Pop off the camera with the earliest ms value.  We will
            # eventually get to everything this way and in approx the
            # order they came in...
            camName, ms = min(iter(self._pendingRealTimeSearches.items()), key=operator.itemgetter(1))
            self._pendingRealTimeSearches.pop(camName)

            self._doRealTimeSearch(camName, ms)
            self._childProcQueueStats.update(None, _kFakeMessageIdRealTimeSearch, None, time.time()-self._lastRealtimeSearch)



    ###########################################################
    def _flushIdleQueue(self):
        """Make sure any things buffered to do at idle time are done."""
        while self._isIdleTimeNeeded(True):
            self._doIdleProcessing(True)


    ###########################################################
    def _cleanupCameraData(self, cameraLocation):
        """Remove any information tracked for a camera.

        @param  cameraLocation  The camera location to cleanup.
        """
        self._logger.info("Cleaning up remaining data for %s" % cameraLocation)
        if cameraLocation in self._pendingRuleCleanupDict:
            del self._pendingRuleCleanupDict[cameraLocation]
        if cameraLocation in self._ruleDicts:
            del self._ruleDicts[cameraLocation]
        if cameraLocation in self._lastTaggedTimes:
            del self._lastTaggedTimes[cameraLocation]
        if cameraLocation in self._lastSearchTimes:
            del self._lastSearchTimes[cameraLocation]
        if cameraLocation in self._maxProcessedTime:
            del self._maxProcessedTime[cameraLocation]
        if cameraLocation in self._cameraProgressTime:
            del self._cameraProgressTime[cameraLocation]
        if cameraLocation in self._cameraProcSizes:
            del self._cameraProcSizes[cameraLocation]


    ###########################################################
    def _loadRules(self, cameraManager):
        """Load real time rules.

        @param  cameraManager  The camera manager.
        """
        configuredCams = cameraManager.getCameraLocations()

        ruleDir = os.path.join(self._userLocalDataDir, kRuleDir)
        if not os.path.isdir(ruleDir):
            return
        fileNames = os.listdir(ruleDir)
        for fileName in fileNames:
            fileName = normalizePath(fileName)
            name, ext = os.path.splitext(fileName)
            if ext == kRuleExt:
                try:
                    # Load the rule
                    ruleFilePath = os.path.join(ruleDir, fileName)
                    ruleFile = open(ruleFilePath, 'rb')
                    rule = pickle.load(ruleFile)
                    ruleFile.close()

                    # Load it's associated query
                    queryFilePath = os.path.join(ruleDir,
                                                 rule.getQueryName()+kQueryExt)
                    queryFile = open(queryFilePath, 'rb')
                    queryModel = pickle.load(queryFile)
                    queryFile.close()

                    # First, convert old queries to have coordinate spaces.
                    convertOld2NewSavedQueryDataModel(
                        self._dataManager, queryModel
                    )

                    query = queryModel.getUsableQuery(self._dataManager)

                    # Remove rules created prior to the addition of responses
                    # in the saved query data model.
                    if not hasattr(queryModel, '_responses'):
                        self._logger.warning("Removing old rule %s" % ruleFile)
                        os.remove(queryFilePath)
                        os.remove(ruleFilePath)
                        continue

                    # Get the camera location and current schedule status
                    camLoc = queryModel.getVideoSource().getLocationName()
                    if camLoc == kAnyCameraStr:
                        continue

                    # Ensure that this rule is associated with a currently
                    # configured camera AND it's using the correct caps.
                    if camLoc not in configuredCams:
                        continue

                    isScheduled, nextSchedChange = rule.getScheduleInfo()

                    # Get the responses.  Pass the URI: _cameraInfo isn't
                    # populated until the cameras are opened, long after this.
                    _, camUri, _, _ = cameraManager.getCameraSettings(camLoc)
                    responses = self._loadResponses(queryModel, camLoc, query,
                            name.lower(), camUri)

                    # Add to the rules dict
                    if camLoc not in self._ruleDicts:
                        self._ruleDicts[camLoc] = {}

                    self._ruleDicts[camLoc][name.lower()] = \
                        (rule, isScheduled, nextSchedChange, query, responses)
                except Exception:
                    self._logger.error("Load rules exception", exc_info=True)


    ###########################################################
    def _loadResponses(self, query, camLoc, usableQuery, ruleName, camUri=None):
        """Load responses for a real time rule.

        @param  query        The SavedQueryDataModel to load responses from.
        @param  camLoc       The camera location of the rule.
        @param  usableQuery  The result of calling getUsableQuery() on the query
        @param  ruleName     The name of the corresponding rule.
        @param  camUri       The camera's stream URI, for responses that need to
                             reach the camera itself.  Callers running before
                             _cameraInfo is populated (startup: _loadRules runs
                             long before the cameras are opened) must pass it;
                             later callers can leave it None and it is looked up.
        @return responses    A list of responses.
        """
        responses = []
        responseConfigList = query.getResponses()

        # "Look for" description backing the {SvRuleLookFor} substitution
        # variable, e.g. "People" or "Faces: Bernie, Alice"...
        lookForStr = ""
        try:
            target = query.getTargets()[0]
            lookForStr = kTargetSettingToLabel.get(target.getTargetName(),
                                                   target.getTargetName())
            if target.getTargetName() == 'face':
                names = target.getFaceNames()
                if names:
                    lookForStr += ": " + ", ".join(names)
        except Exception:
            pass

        paid = hasPaidEdition(self._licenseManager.licenseData())

        for responseName, config in responseConfigList:
            if not config.get('isEnabled'):
                # "Speak text" (TTS) lives inside the Sound response config
                # but has its own enable — a rule may speak WITHOUT playing a
                # WAV.  Load the Sound response for TTS-only rules, clearing
                # the WAV path so only the speech happens.
                if responseName == kSoundResponse and config.get('ttsEnabled'):
                    config['soundPath'] = ''
                else:
                    continue

            # configs here are deep copies; injecting helper keys is the
            # established pattern (see kRecordResponse below).
            config['svLookFor'] = lookForStr

            if responseName == kRecordResponse:
                config['msgList'] = self._recordResponseMsgs
                config['camLoc'] = camLoc
                responses.append(RecordResponse(config))
            elif responseName == kEmailResponse:
                responses.append(EmailResponse(query.getName(), camLoc,
                                               self._emailSettings,
                                               self._childProcQueue,
                                               self._responseRunnerQueue,
                                               config))
            elif responseName == kPushResponse and paid:
                startOffset, stopOffset = usableQuery.getClipLengthOffsets()
                responses.append(PushResponse(camLoc, query.getName(),
                            usableQuery.shouldCombineClips(),
                            startOffset, stopOffset, self._responseRunnerQueue))
            elif responseName == kIftttResponse and paid:
                iftttKey = config.get('iftttKey', '')
                iftttEventName = config.get('iftttEventName', '')
                if iftttKey and iftttEventName:
                    startOffset, stopOffset = usableQuery.getClipLengthOffsets()
                    responses.append(IftttResponse(camLoc, query.getName(),
                                usableQuery.shouldCombineClips(),
                                startOffset, stopOffset,
                                self._responseRunnerQueue,
                                iftttKey, iftttEventName))
            elif responseName == kWebhookResponse and paid:
                responses.append(WebhookResponse(query.getName(), camLoc,
                                               self._responseRunnerQueue,
                                               config))
            elif responseName == kSoundResponse:
                responses.append(SoundResponse(config,
                                               ruleName=query.getName(),
                                               camLoc=camLoc,
                                               dataMgr=self._dataManager))
            elif responseName == kSnapshotResponse:
                responses.append(SnapshotResponse(query.getName(), camLoc,
                                                  self._childProcQueue,
                                                  self._responseRunnerQueue,
                                                  config))
            elif responseName == kIHostResponse:
                responses.append(IHostResponse(query.getName(), camLoc,
                                               self._responseRunnerQueue, config))
            elif responseName == kTapoResponse:
                if camUri is None:
                    camUri = (self._cameraInfo.get(camLoc) or (None,))[0]
                tapoHost = tapoHostFromUri(camUri) if camUri else None
                if tapoHost is None:
                    self._logger.error(
                        "Rule '%s': camera '%s' has no address the Tapo siren "
                        "and light can be sent to" % (ruleName, camLoc))
                else:
                    config['tapoHost'] = tapoHost
                    responses.append(TapoResponse(query.getName(), camLoc,
                                                  self._responseRunnerQueue,
                                                  config))
            elif responseName == kCommandResponse and paid:
                responses.append(CommandResponse(config))
            elif responseName == kFtpResponse and paid:
                playTimeOffset, preservePlayOffset = \
                    usableQuery.getPlayTimeOffset()
                responses.append(SendClipResponse(
                    self._logger,
                    kFtpProtocol,
                    query.getName(),
                    camLoc,
                    self._responseDb,
                    self._childProcQueue,
                    self._responseRunnerQueue, config,
                    playTimeOffset,
                    usableQuery.getClipLengthOffsets(),
                    usableQuery.shouldCombineClips(),
                    preservePlayOffset
                ))
            elif responseName == kLocalExportResponse and paid:
                playTimeOffset, preservePlayOffset = \
                    usableQuery.getPlayTimeOffset()
                responses.append(SendClipResponse(
                    self._logger,
                    kLocalExportProtocol,
                    query.getName(),
                    camLoc,
                    self._responseDb,
                    self._childProcQueue,
                    self._responseRunnerQueue, config,
                    playTimeOffset,
                    usableQuery.getClipLengthOffsets(),
                    usableQuery.shouldCombineClips(),
                    preservePlayOffset
                ))
                self._localExportSettings[ruleName] = config['exportPath']

        if not query.isOk():
            self._logger.error(
                "Invalid file path contained in responses <%s>" %
                (responseConfigList,)
            )

        return responses


    ###########################################################
    def _flushResponses(self, force=False):
        """Flush the responses.

        This is called at idle time, and during shutdown.

        @param  force   If True, we'll force a flush; else we'll only flush if
                        it's been long enough.
        """
        # If responses were added to the dict added before flushTime,
        # we'll flush the responses.
        timeNow = time.time()

        # Iterate over copy of keys, so we can delete...
        for camLoc in list(self._responsesToFlush.keys()):
            allResponses, timeStopped = self._responsesToFlush[camLoc]
            if (timeStopped + _kResponseFlushTime <= timeNow) or force:
                for response in allResponses:
                    response.flush()
                del self._responsesToFlush[camLoc]


    ###########################################################
    def _openDatabases(self):
        """Open any databases needed by the back end."""
        self._clipManager = ClipManager(self._logger, self._clipMergeThreshold)
        self._clipManager.open(self._clipDbPath)
        self._dataManager = DataManager(self._logger,
                                        self._clipManager,
                                        self._videoDir)
        self._dataManager.open(self._objDbPath)


    ###########################################################
    def _reopenDatabases(self):
        """Reopen databases needed by the back end.

        Doesn't recreate the data manager and clip manager, since others may
        have pointers to them.
        """
        self._clipManager.open(self._clipDbPath)
        self._dataManager.open(self._objDbPath)


    ###########################################################
    def _openIPC(self):
        """Open routes of communication to the back end app.

        @return success  True if all IPC was successfully started.
        """
        # Begin a process for IPC from the front end
        self._processedTimesQueue = Queue(0)
        self._enableDiskLogging(False)
        try:
            licenseData = self._licenseManager.licenseData()
            hardwareDevices = getHardwareDevicesList()
            self._netMsgServerProc = startNetworkMessageServer(
                self._childProcQueue, self._userLocalDataDir,
                self._processedTimesQueue, self._clipDbPath, self._objDbPath,
                self._responseDbPath, licenseData, hardwareDevices
            )
        finally:
            self._enableDiskLogging(True)

        # Wait up to 10 seconds for the server to start
        try:
            msg = self._childProcQueue.get(True, 10)
        except Exception:
            self._logger.error("NetworkMessageServer was not started")
            return False, None

        self._netMsgServerClient = self._getXMLRPCClient()

        assert msg[0] == MessageIds.msgIdXMLRPCStarted, \
               "Expected MessageIds.msgIdXMLRPCStarted, not %s" % (msg[0])
        return msg[1], msg[2]


    ###########################################################
    def _initVideoStreams(self, cameraManager):
        """Start a process for each configured video stream.

        @param  cameraManager  The camera manager.
        """
        # Reset everything we knew about cameras.
        self._cameraInfo = {}

        locations = cameraManager.getCameraLocations()

        # Remove any old camera logs that are hanging around
        try:
            lowercaseLocations = [s.lower() for s in locations]
            cameraLogDir = os.path.join(self._logDir, "cameras")
            for logFile in os.listdir(cameraLogDir):
                logFile = normalizePath(logFile)
                if logFile.endswith('.log'):
                    logCam = logFile[:-4]
                    if logCam.lower() not in lowercaseLocations:
                        os.remove(os.path.join(cameraLogDir, logFile))
        except Exception:
            # We're just trying to do a bit of housekeeping, we really don't
            # care too much about this.
            pass

        # Load settings for configured cameras and start them if enabled.
        for location in locations:
            _, uri, enabled, extra = \
                                    cameraManager.getCameraSettings(location)
            self._cameraInfo[location] = (uri, enabled, False, extra)

            if not cameraManager.isCameraFrozen(location):
                isScheduled, _ = self._getCameraScheduleStatus(location)

                if enabled and isScheduled:
                    self._openCamera(location)


    ###########################################################
    def _removeTmpFiles(self):
        """Remove any files hanging around."""
        liveDir = os.path.join(self._userLocalDataDir, "live")
        if os.path.isdir(liveDir):
            liveFiles = os.listdir(liveDir)
            for liveFile in liveFiles:
                liveFile = normalizePath(liveFile)
                fullPath = os.path.join(liveDir, liveFile)
                try:
                    # The live dir now holds transient DIRECTORIES too — the
                    # recorder's per-camera snapshot rings (<cam>.snaps) — not
                    # just the .live/.audio mmap files.  os.remove() can't
                    # delete a directory, so those stale rings piled up and
                    # logged a warning each at every startup; handle both.
                    if os.path.isdir(fullPath):
                        shutil.rmtree(fullPath)
                    else:
                        os.remove(fullPath)
                except Exception:
                    self._logger.warning("Couldn't remove %s" % liveFile)


    ###########################################################
    def _startOnvifScanner(self):
        """Start the ONVIF discovery thread if it isn't already running.

        Safe to call repeatedly (no-op when already running).  Used both at
        startup for opt-in always-on discovery and on demand while the
        Add-Camera wizard is open.
        """
        if self._onvifScanner is not None:
            return
        if self._onvifLogger is None:
            self._onvifLogger = getLogger(_kOnvifLogName, self._logDir,
                                          _kOnvifLogSize)
        self._onvifScanner = OnvifNetworkScanner(
            self._onvifLogger,
            NetworkScannerCallback(MessageIds.msgIdUpdateOnvif,
                                   self._childProcQueue),
            _kFastestOnvifPoll,
            OnvifDeviceManager(self._onvifLogger, self._threadPool),
            "ONVIF")
        self._logger.info("ONVIF scanner started")


    ###########################################################
    def _stopOnvifScanner(self):
        """Stop the ONVIF discovery thread if it's running."""
        if self._onvifScanner is None:
            return
        try:
            self._onvifScanner.shutdown()
        except Exception:
            self._logger.warning("Error stopping ONVIF scanner: %s"
                                 % traceback.format_exc())
        self._onvifScanner = None
        self._logger.info("ONVIF scanner stopped (wizard idle)")


    ###########################################################
    def _startUpnpScanner(self):
        """Start the UPnP discovery thread if it isn't already running."""
        if self._upnpScanner is not None:
            return
        if self._upnpLogger is None:
            self._upnpLogger = getLogger(_kUpnpLogName, self._logDir,
                                         _kUpnpLogSize)
        self._upnpScanner = NetworkScanner(
            self._upnpLogger,
            NetworkScannerCallback(MessageIds.msgIdUpdateUpnp,
                                   self._childProcQueue),
            _kFastestUpnpPoll,
            ControlPointManager(self._upnpLogger),
            "UPNP")
        self._logger.info("UPNP scanner started")


    ###########################################################
    def _stopUpnpScanner(self):
        """Stop the UPnP discovery thread if it's running."""
        if self._upnpScanner is None:
            return
        try:
            self._upnpScanner.shutdown()
        except Exception:
            self._logger.warning("Error stopping UPNP scanner: %s"
                                 % traceback.format_exc())
        self._upnpScanner = None
        self._logger.info("UPNP scanner stopped (wizard idle)")


    ###########################################################
    def _maybeStopIdleScanners(self, curTime):
        """Tear down on-demand discovery once the Add-Camera wizard is gone.

        The wizard re-requests a search every few seconds while open; when it
        closes those requests stop, so if we haven't seen one in
        _kDiscoveryIdleTimeout we shut the (non-persistent) scanners back down.
        Persistent scanners (enable* marker file) are left running.
        """
        if self._lastCameraSearchTime <= 0.0:
            return                        # disarmed (no on-demand search active)
        if (curTime - self._lastCameraSearchTime) <= _kDiscoveryIdleTimeout:
            return
        if not self._onvifPersistent:
            self._stopOnvifScanner()
        if not self._upnpPersistent:
            self._stopUpnpScanner()
        # Disarm until the next active search re-arms us.
        self._lastCameraSearchTime = 0.0


    ###########################################################
    def _noteCameraDeath(self, camLocation, now):
        """Track a camera-process death for restart-storm backoff.

        A death shortly after start means the process is failing at STARTUP
        (bad DB, crash on init) -- restarting it instantly just storms.  After
        the second consecutive quick death, delay the next restart with
        exponential backoff.  A run longer than _kFastDeathSecs resets.

        @param  camLocation  The camera that died.
        @param  now          time.time() of the death check.
        """
        started = self._cameraStartTimes.get(camLocation)
        runtime = (now - started) if started else None
        if runtime is not None and runtime < _kFastDeathSecs:
            fails = self._cameraFastFails.get(camLocation, 0) + 1
            self._cameraFastFails[camLocation] = fails
            if fails >= 2:
                delay = min(_kRestartBackoffBaseSecs * (2 ** (fails - 2)),
                            _kRestartBackoffMaxSecs)
                self._cameraRestartAfter[camLocation] = now + delay
                self._logger.warning(
                    "%s died %d times within %ds of starting; delaying next "
                    "restart %ds" % (ensureUnicode(camLocation), fails,
                                     _kFastDeathSecs, delay))
        else:
            self._cameraFastFails.pop(camLocation, None)
            self._cameraRestartAfter.pop(camLocation, None)


    ###########################################################
    def _clearCameraBackoff(self, camLocation):
        """Forget restart backoff for a camera (manual enable = start NOW)."""
        self._cameraFastFails.pop(camLocation, None)
        self._cameraRestartAfter.pop(camLocation, None)


    ###########################################################
    def _openCamera(self, camLocation):
        """Start a process for a camera stream.

        @param  camLocation  The camera's location.
        @return camState     The new camera state if it changed, 'None'
                             otherwise.
        """
        assert camLocation in self._cameraInfo
        uri, enabled, monitored, extra = self._cameraInfo[camLocation]

        if self._lowDiskPaused:
            # Recording paused for critically low disk — don't start cameras.
            return None

        if extra.get('frozen', False):
            # If the camera is frozen then ignore this wish.
            return None

        if camLocation in self._captureStreams:
            # If the camera is already open don't do anything.
            return None

        if time.time() < self._cameraRestartAfter.get(camLocation, 0):
            # Restart-storm backoff: this camera keeps dying right after
            # start; wait out the delay set by _noteCameraDeath.
            return None

        # Remove it from the forced back end save times list.
        if camLocation in self._selfAddSavedTimes:
            self._selfAddSavedTimes.remove(camLocation)

        # Don't need to flush anymore--we're gonna get more data...
        self._responsesToFlush.pop(camLocation, None)

        # Inform any loaded rules for this camera about the new session.
        ruleDict = self._ruleDicts.get(camLocation, {})
        for _, _, _, _, responses in list(ruleDict.values()):
            for response in responses:
                response.startNewSession()

        if not enabled:
            # Don't open the camera if it is disabled
            return None

        newURI = self._realizeUri(uri)

        if newURI == "" or newURI == None:
            if uri == "" or uri is None:
                # This means that the device could not be found, since its uri could
                # not be "real-ized".  Log this as a warning so that it stands out
                # in the logs, and then return.
                self._logger.warning(
                    "Cannot open '%s' - the camera could not be found." %
                    (camLocation,)
                )
                self._setCameraStatus(camLocation, kCameraFailed)
                return kCameraFailed
            else:
                self._logger.info("No ONVIF/UPNP results are available for " + \
                                camLocation + ". Using previously stored URI " + sanitizeUrl(uri))
        else:
            uri = newURI

        # If the camera already exists there could be pending searches, we don't
        # want to cause some data to not be considered.
        if camLocation not in self._lastSearchTimes:
            self._lastSearchTimes[camLocation] = time.time()*1000

        dmPipe1, dmPipe2 = Pipe()
        camPipe1, camPipe2 = Pipe()
        pipeId = self._nextPipeId
        self._nextPipeId += 1
        recSize = extra.get('recordSize', kDefaultRecordSize)

        # Shouldn't be able to get here with a large resolution if not licensed
        # for it, but check anyway.
        if not hasPaidEdition(self._licenseManager.licenseData()):
            if ((recSize[0] > kMaxRecordSize[0]) or
                (recSize[1] > kMaxRecordSize[1]) or
                (recSize == kMatchSourceSize)):

                extra['recordSize'] = kMaxRecordSize
                recSize = kMaxRecordSize

        elif not isLocalCamera(uri):

            # Match source resolution for all IP cameras with licensed app.
            extra['recordSize'] = kMatchSourceSize
            recSize = kMatchSourceSize

        # Always write recSize back so CameraCapture/StreamReader see an explicit value.
        extra['recordSize'] = recSize
        self._putMsgRR([MessageIds.msgIdSetCamResolution, camLocation, recSize[0], recSize[1]])

        extra[kLiveMaxBitrate] = self.videoSettings[kLiveMaxBitrate]
        extra['enableTimestamps'] = self.videoSettings[kLiveEnableTimestamp]
        extra[kLiveEnableFastStart] = self.videoSettings[kLiveEnableFastStart]
        extra[kLiveMaxResolution] = self.videoSettings[kLiveMaxResolution]
        extra[kGenThumbnailResolution] = self.videoSettings[kGenThumbnailResolution]
        # This value determines analytics FPS. Take care when changing --
        # always take performance into consideration
        extra[kFpsLimit] = 10
        extra[kRecordInMemory] = self._recordInMemory
        extra[kClipMergeThreshold] = self._clipMergeThreshold
        extra['useUSDate'] = self._timePrefs[1]
        extra['use12HrTime'] = self._timePrefs[0]
        extra[kHardwareAccelerationDevice] = self._hardwareDevice
        if _kDebugConfig is not None:
            extra['debugConfig'] = _kDebugConfig

        # Let the new camera process know what moves are still pending
        pendingMoves = []
        for targetPath, (loc, _) in list(self._pendingFileMoves.items()):
            if loc == camLocation:
                pendingMoves.append(os.path.basename(targetPath))
        extra['pendingMoves'] = pendingMoves

        self._enableDiskLogging(False)
        try:
            p = startCapture(self._childProcQueue, camPipe2, dmPipe2, pipeId,
                             camLocation, uri, self._clipDbPath, self._tmpDir,
                             self._videoDir, self._userLocalDataDir, extra)
        finally:
            self._enableDiskLogging(True)

        self._captureStreams[camLocation] = (p, camPipe1, pipeId, time.time())
        self._cameraStartTimes[camLocation] = time.time()
        # Disarm the no-progress watchdog for the new process rather than
        # leaving that to whatever tore the last one down.  A progress time
        # must never outlive the process it described: _stopCamera clears it
        # via _delCaptureStream, but ONLY when the camera was still in
        # _captureStreams, so a camera DISABLED and later re-enabled kept its
        # pre-disable timestamp.  Measured 2026-08-30: 09_WestTerrace was
        # disabled at 00:06 and re-enabled at 07:32, and the watchdog killed
        # the brand-new process five seconds later for a "26800 sec" stall.
        #
        # None, not now(): the watchdog only arms once a camera has actually
        # reported progress, so a camera that has never delivered (unreachable,
        # still connecting) stays its own problem -- _processTimedOut and the
        # connecting/failed status own that case, and restarting it on a timer
        # would just add churn.
        self._cameraProgressTime[camLocation] = None
        self._dataMgrPipes[pipeId] = dmPipe1
        self._tempIdMap[pipeId] = []

        self._setCameraStatus(camLocation, kCameraConnecting)

        if monitored:
            self._pendingLiveViewStatus[camLocation] = \
                                [MessageIds.msgIdEnableLiveView, camLocation]

        if _kDebugConfig is not None:
            self._sendMsg(camPipe1, [MessageIds.msgIdSetDebugConfig, _kDebugConfig], camLocation)
        if self._analyticsPort is not None:
            self._sendMsg(camPipe1, [MessageIds.msgIdAnalyticsPortChanged, self._analyticsPort], camLocation)

        # Update the disk cleaner with the number of active cameras
        self._putMsgDC([MessageIds.msgIdSetNumCameras, len(self._captureStreams)])

        return kCameraConnecting


    ###########################################################
    def _stopCamera(self, camLocation):
        """Stop a process for a camera stream.

        @param  camLocation  The camera's location.
        @return camState     The new camera state if it changed, 'None'
                             otherwise.
        """
        # If the camera isn't actually running we have nothing to do.
        if camLocation not in self._captureStreams:
            return None

        proc, pipe, _, _ = self._captureStreams[camLocation]
        self._sendMsg(pipe, [MessageIds.msgIdQuit], camLocation)

        # Update the disk cleaner with the number of active cameras
        self._putMsgDC([MessageIds.msgIdSetNumCameras, len(self._captureStreams)-1])

        self._setCameraStatus(camLocation, kCameraOff)

        self._delCaptureStream(camLocation)

        # Add the camera process to a list so we ensure it actually quits
        # or is terminated.
        self._deadCameras.append((proc, time.time()))

        # Note the fact that we'd like to flush the responses before too long...
        ruleDict = self._ruleDicts.get(camLocation, {})
        allResponses = []
        for _, _, _, _, responses in ruleDict.values():
            allResponses.extend(responses)
        self._responsesToFlush[camLocation] = (allResponses, time.time())

        return kCameraOff

    ###########################################################
    def _realizeUri(self, uri):
        # We'll try to realize any UPNP/ONVIF URLs into real URLs...
        # ...we'll also kick off an active search; prolly too late for this
        # time, but if we try again (which we should), it may help...
        try:
            # The call is the guard: it raises ValueError for a non-UPnP uri,
            # which is what skips the realize below.  The result is unused.
            extractUsnFromUpnpUrl(uri)
            uri = realizeUpnpUrl(self._upnpDevices, uri)
        except ValueError:
            pass # Expect this for non-upnp uris...
        try:
            extractUuidFromOnvifUrl(uri)
            uri = realizeOnvifUrl(self._onvifDevices, uri)
        except ValueError:
            pass # Expect this for non-onvif uris...

        return uri

    ###########################################################
    def _startTestCamera(self, uri, extras):
        """Start streaming to test a camera uri.

        @param  uri     The camera's uri.
        @param  extras  The extras dict for the camera.
        """
        # Ensure that we don't have another test running.
        self._stopTestCamera()

        # Save test camera URI _before_ realizing it as UPNP/ONVIF...
        uri = self._realizeUri(uri)
        self._testCamUri = uri
        # Keep them so a scanner-driven restart can reuse THIS stream's
        # settings rather than borrowing another camera's.
        self._testCamExtras = extras

        self._enableDiskLogging(False)
        extras[kLiveEnableFastStart] = False
        try:
            liveDataDir = os.path.join(self._userLocalDataDir, 'live')
            self._testCamProc = startStream(uri, liveDataDir, self._logDir,
                    self._userLocalDataDir, self._childProcQueue, extras)
        finally:
            self._enableDiskLogging(True)


    ###########################################################
    def _stopTestCamera(self):
        """Stop a process for a camera stream."""
        # If there is an existing process terminate it.
        if self._testCamProc is not None:
            if self._testCamProc.is_alive():
                self._terminateCameraProcess(self._testCamProc)
        self._testCamProc = None
        self._testCamUri = None
        self._testCamExtras = None

        # Remove live file
        liveFilePath = os.path.join(self._userLocalDataDir, 'live',
                                    kTestLiveFileName)
        if os.path.exists(liveFilePath):
            try:
                os.remove(liveFilePath)
            except Exception:
                self._logger.info("Couldn't remove the test live file")


    ###########################################################
    def _startPacketCapture(self, cameraLocation, delaySeconds, pcapDir):
        """Start streaming to test a camera uri.

        @param  cameraLocation  The camera's name.
        @param  delaySeconds    The time alloted for packet capture.
        """
        # Ensure that we don't have another packet capture running.
        self._stopPacketCapture()

        uri, enabled, monitored, extras = self._cameraInfo[cameraLocation]

        uri = self._realizeUri(uri)

        liveDataDir = os.path.join(self._userLocalDataDir, 'live')

        self._enableDiskLogging(False)
        try:
            self._pcapCamProc = startPacketCapture(
                uri, liveDataDir, pcapDir, self._userLocalDataDir,
                self._childProcQueue, extras, delaySeconds
            )
        finally:
            self._enableDiskLogging(True)


    ###########################################################
    def _stopPacketCapture(self):
        """Stop a process for a camera stream."""
        # If there is an existing process terminate it.
        if self._pcapCamProc is not None:
            if self._pcapCamProc.is_alive():
                self._terminateCameraProcess(self._pcapCamProc)
        self._pcapCamProc = None
        self._pcapInfo = {"pcapEnabled":None, "pcapStatus":None}


    ###########################################################
    def _createCertificateData(self):
        """ Creates contact information and name fields for the certificate to
        be used for remote access via HTTPS. Originally we wanted to put in real
        user information (name, e-mail), but that would have exposed it in the
        certificate to the public. For now we derive some identifier from the
        machine ID, so there is something static, but yet somewhat recognizable
        without the need for persistence.

        @return (contact,name)  The certificate data to use.
        """
        mid = machineId(True, self._logger)
        for _ in range(0, 32): # scramble the machine ID, don't expose it!
            mid = hashlib.sha1(mid.encode("UTF-8")).hexdigest()
        name = "remote" + mid[0:8]  # (and only return a portion of it)
        return (kSupportEmail, name)


    ###########################################################
    def _initWebServer(self, webPort, auth):
        """Start the LAN record-viewer web server process.

        The rebuilt web server is a self-contained pure-Python HTTP server
        (see WebServer.py) that reads the object/clip databases read-only and
        streams recorded clips.  It needs no XML/RPC bridge, remote media dir,
        port opener, or SSL cert -- those belonged to the retired nginx/XNAT
        remote-access stack.

        @param webPort: The initial HTTP port number to use (-1 = off).
        @param auth:    The stored authentication expression (see make_auth).
        """
        # Reuse the queue across restarts (see _initDiskCleanup): a fresh one
        # would strand any port/auth change the dead process had not read.
        if self._webServerQueue is None:
            self._webServerQueue = Queue(0)
        # Remember what we started it with, so the watchdog in run() can
        # restart it with the CURRENT settings rather than the startup ones.
        self._webPort = webPort
        self._webAuth = auth
        self._enableDiskLogging(False)
        try:
            webDir = os.environ.get(kWebDirEnvVar)
            if not webDir:
                webDir = os.path.join(self._userLocalDataDir, kWebDirName)
            self._webServerProc = startWebServer(
                self._childProcQueue,
                self._webServerQueue,
                self._logDir,
                webDir,
                webPort,
                auth,
                self._videoDir,
                self._clipDbPath,
                self._objDbPath,
                # The saved rule/query pickles the desktop Search
                # screen searches by; handed over (read-only) so the
                # viewer can offer the same rules.  Passed explicitly
                # rather than derived from webDir, which the
                # SIGHTHOUND_WEBDIR override can relocate.
                os.path.join(self._userLocalDataDir, kRuleDir))
        finally:
            self._enableDiskLogging(True)
        self._logger.info("web directory: %s" % webDir)

    ###########################################################
    def _initPlatformHTTPWrapper(self):
        """Start a process to run the web server and all the care it needs.
        """
        self._logger.info("Starting platform wrapper")
        self._enableDiskLogging(False)
        error = ""
        try:
            os.environ["SIO_INFERENCE_RUNTIME"] = "D3V"
            if hasattr(sys, 'frozen'):
                os.environ["SIO_ENV_IE_CONFIG_FILE"] = "./share/D3VConfig.json" if "win32" == sys.platform else "./Resources/share/D3VConfig.json"


            self._platformHTTPWrapperProc = startPlatformHTTPWrapper(
                self._childProcQueue,
                self._userLocalDataDir,
                0)
            self._logger.info("Successfully launched platform HTTP wrapper")
        except:
            error = traceback.format_exc()
        finally:
            self._enableDiskLogging(True)
            if len(error) > 0:
                self._logger.error("Failed to launch platform HTTP wrapper:" + error)

    ###########################################################
    def _initDiskCleanup(self, maxStorage):
        """Start a process to monitor and manage disk usage.

        @param  maxStorage  The maximum amount of space to use in bytes.
        """
        # Reuse the queue across restarts, the way _responseRunnerQueue is.
        # A fresh Queue() here is addressed to nobody: any one-shot command the
        # dead process had not drained (msgIdRemoveDataAtLocation,
        # msgIdDeleteFile, msgIdSetDebugConfig) goes with it, and the first of
        # those is the only thing that unregisters a deleted camera's clips.
        if self._diskCleanupQueue is None:
            self._diskCleanupQueue = Queue(0)

        self._enableDiskLogging(False)
        try:
            tmpDir = os.path.join(self._dataStorageLocation, "tmp")
            self._diskCleanupProc = startDiskCleaner(
                self._childProcQueue, self._diskCleanupQueue, self._clipDbPath,
                self._objDbPath, len(self._captureStreams), maxStorage,
                self._videoDir, tmpDir, self._logDir, self._userLocalDataDir,
                self._remoteDir, self._disableDiskCleanup, self._cacheDuration
            )
        finally:
            self._enableDiskLogging(True)


    ###########################################################
    def _sendIftttState(self, prefs):
        """ Sends pending IFTTT state, given that there is nothing being sent
        at this very moment.

        @param  prefs  Preference to persist a flag indicating that we don't
                       have anything for IFTTT and that there is no reason for
                       sending things out.
        """

        if self._iftttStateCleared:
            self._iftttStateCleared = False
            prefs.setPref("iftttIdle", True)

        if self._iftttStateSending or \
           self._iftttStatePending is None:
            return

        empty = self._iftttStatePending == ([],[])

        idle = prefs.getPref("iftttIdle")
        if empty:
            if idle:
                self._iftttStatePending = None
                return
        else:
            if idle:
                prefs.setPref("iftttIdle", False)

        class IftttStateSender:
            def __init__(self, backEndApp, authToken, state):
                self._authToken = authToken
                self._backEndApp = backEndApp
                self._state = state
            def run(self):
                backEndApp = self._backEndApp
                logger = backEndApp._logger
                if backEndApp._iftttLastStateOut == self._state:
                    logger.info("no change in IFTTT state, no need to send")
                    backEndApp._iftttStateSending = False
                    sent = True
                else:
                    logger.info("sending IFTTT state %s ..." % str(self._state))
                    try:
                        ic = IftttClient(logger, self._authToken)
                        sent = ic.sendState(*self._state)
                    finally:
                        backEndApp._iftttStateSending = False
                if sent:
                    backEndApp._iftttLastStateOut = self._state
                    if self._state == ([],[]):
                        backEndApp._iftttStateCleared = True

        # Pretend to be sending right away, since the thread might be faster.
        self._iftttStateSending = True
        authToken = self._licenseManager.getAuthToken()
        # If the thread pool is blocked we will try later. Must not get stuck.
        self._logger.info("scheduling IFTTT state sending...")
        sender = IftttStateSender(self, authToken, self._iftttStatePending)
        if self._threadPool.schedule(sender, False):
            self._iftttStatePending = None
        else:
            self._iftttStateSending = False


    ###########################################################
    def _openLicenseManager(self, settings):
        """Open the license manager and schedule needed operations.

        @paramm settings  The (initial) license settings.
        @return           License manager instance.
        """
        machid = machineId(True, self._logger)
        self._logger.info("machine ID: %s" % machid)
        return LicenseManager(settings, machid, self._userLocalDataDir,
                              self._logger)


    ###########################################################
    def _syncCamerasWithLicense(self, camMgr):
        """Ensure that the camera (state)s are in compliance with the license.

        @param camMgr  Camera manager.
        """
        camFld = self._licenseManager.licenseData()[kCamerasField]
        maxCameras = int(camFld)
        res = camMgr.freezeCameras(maxCameras, True)
        self._logger.info(
            "cameras synched with license (max=%d) - frozen: %d, unfrozen: %d" %
            (maxCameras, len(res[0]), len(res[1])))
        camMgr.logLocations(self._logger)


    ###########################################################
    def _initDetectionService(self):
        """Start the shared ML detection service process.

        One process owns YOLO/InsightFace/NudeNet and serves every camera
        over localhost (see DetectionService.py).  Camera processes talk to
        it via DetectionServiceClient and degrade to no-detections while it
        is down/restarting.
        """
        self._enableDiskLogging(False)
        try:
            self._detectionServiceProc = startDetectionService(
                self._userLocalDataDir, self._logDir)
        finally:
            self._enableDiskLogging(True)


    ###########################################################
    def _initResponseRunner(self):
        """Start a process to handle doing slow responses."""
        tmpDir = os.path.join(self._dataStorageLocation, "tmp")
        self._lastResponseRunnerPing = time.time()
        self._enableDiskLogging(False)
        try:
            self._responseRunnerProc = startResponseRunner(
                self._childProcQueue, self._responseRunnerQueue, self._clipDbPath,
                self._objDbPath, self._responseDbPath, self._videoDir, tmpDir,
                self._logDir, self._userLocalDataDir, self._ftpSettings,
                self._localExportSettings, self._notificationSettings,
                self._licenseManager.getAuthToken()
            )
        finally:
            self._enableDiskLogging(True)


    ###########################################################
    def _quit(self):
        """Terminate the application."""
        self._logger.info("User quit requested")
        self.wantQuit = True

    ###########################################################
    def _putMsg(self, q, msg, location):
        if q:
            start = time.time()
            q.put(msg)
            self._childProcQueueStats.update(None, _kFakeMessageIPCUtility, None, time.time()-start, location)

    ###########################################################
    def _putMsgDC(self, msg):
        self._putMsg(self._diskCleanupQueue, msg, "diskCleaner")

    ###########################################################
    def _putMsgRR(self, msg):
        self._putMsg(self._responseRunnerQueue, msg, "responseRunner")

    ###########################################################
    def _putMsgWS(self, msg):
        self._putMsg(self._webServerQueue, msg, "webServer")

    ###########################################################
    def _sendMsg(self, pipe, msg, location):
        start = time.time()
        try:
            pipe.send(msg)
        except (BrokenPipeError, EOFError, OSError) as e:
            # The target camera's pipe is transiently closed -- it's mid-restart
            # or still starting (common right after launch).  That's expected
            # churn, not a fault: log one concise line instead of letting it
            # propagate to the top-level message loop as an ERROR traceback.
            self._logger.warning("dropped msg %s to '%s': pipe closed (%s)"
                                 % (msg[0] if msg else '?', location,
                                    type(e).__name__))
            return
        self._childProcQueueStats.update(None, _kFakeMessageIPCCamera, None, time.time()-start, location)

    ###########################################################
    def _sendMsgLoc(self, msg, location):
        if location in self._captureStreams:
            _, pipe, _, _ = self._captureStreams[location]
            self._sendMsg(pipe, msg, location)

    ###########################################################
    def _broadcastMsg(self, msg):
        for camLoc in self._captureStreams:
            try:
                _, pipe, _, _ = self._captureStreams[camLoc]
                self._sendMsg(pipe, msg, camLoc)
            except Exception:
                self._logger.error(
                    "Failed to deliver message %s to camera '%s': %s" %
                    (msg[0], camLoc, traceback.format_exc()))

    ###########################################################
    def _setLiveViewStatus(self, msg, delayed):
        cameraLocation = msg[1]
        op = "Processing" if delayed else "Received"
        value = msg[0] == MessageIds.msgIdEnableLiveView
        msgId = "msgIdEnableLiveView" if value else "msgIdDisableLiveView"
        self._logger.info("%s %s, loc: %s" % (op, msgId, cameraLocation))
        self._sendMsgLoc(msg, cameraLocation)
        if cameraLocation in self._cameraInfo:
            uri, enabled, _, extra = self._cameraInfo[cameraLocation]
            self._cameraInfo[cameraLocation] = (uri, enabled, value, extra)
        self._pendingLiveViewStatus.pop(cameraLocation, None)

    ###########################################################
    def _setLiveViewParams(self, msg, delayed):
        cameraLocation = msg[1]
        width = msg[2]
        height = msg[3]
        audioVolume = msg[4]
        fps = msg[5]
        op = "Processing" if delayed else "Received"
        self._logger.info("%s msgIdSetLiveViewParams " \
           "loc: %s, width: %d, height: %d, audio: %d, fps: %d" %
           (op, cameraLocation, width, height, audioVolume, fps))
        self._sendMsgLoc([MessageIds.msgIdSetMmapParams, True, width, height, fps], cameraLocation)
        self._sendMsgLoc([MessageIds.msgIdSetAudioVolume, audioVolume], cameraLocation)
        self._pendingLiveViewSettings.pop(cameraLocation, None)

    ###########################################################
    def _doRealTimeSearch(self, cameraLocation, ms):
        """Perform an incremental search for a camera.

        @param  cameraLocation  The camera location to perform the search on.
        @param  ms              The most recent time in milliseconds to search.
        """
        try:
            self._dataManager.setCameraFilter([cameraLocation])

            lastSearchTime = self._lastSearchTimes.get(cameraLocation, 0)
            for ruleName in self._ruleDicts.get(cameraLocation, {}):
                rule, scheduled, schedChange, query, responses = \
                                    self._ruleDicts[cameraLocation][ruleName]

                # Update if the rule is currently schedule or not if necessary.
                curScheduled = False
                if schedChange is not None and ms/1000 > schedChange:
                    curScheduled, nextSchedChange = rule.getScheduleInfo()
                    self._ruleDicts[cameraLocation][ruleName] = \
                        (rule, curScheduled, nextSchedChange, query, responses)
                else:
                    curScheduled = scheduled

                # If the rule is enabled, has responses, and was scheduled for
                # part of this time segment perform a search.
                if rule.isEnabled() and (scheduled or curScheduled) and \
                                                                    responses:
                    self._logger.debug("Searching %s with %s from %f, %f" %
                                (cameraLocation, ruleName, lastSearchTime, ms))
                    procSize = self._cameraProcSizes.get(cameraLocation, None)
                    procSizesMsRange = None
                    if procSize is not None:
                        procSizesMsRange = [(procSize[0], procSize[1], lastSearchTime, ms+1)]
                    results = list(query.search(lastSearchTime+1, ms, 'realtime', procSizesMsRange))
                    results.sort()
                    rangeDict = parseSearchResults(results,
                                                   query.shouldCombineClips())

                    # Always fire the responses, since they may need to do
                    # processing even if no current results...
                    for response in responses:
                        response.addRanges(ms, rangeDict)

                if scheduled and not curScheduled:
                    query.reset()

            self._lastSearchTimes[cameraLocation] = ms

            while len(self._recordResponseMsgs):
                msg = self._recordResponseMsgs.popleft()
                self._processQueueMessage(msg)

            tagged = self._lastTaggedTimes.get(cameraLocation, 0)
            self._processedTimesQueue.put([cameraLocation, ms, tagged])
        except DatabaseError as e:
            self._logger.error("Real time search database exception",
                               exc_info=True)
            if str(e) in kCorruptDbErrorStrings:
                self._handleCorruptDatabase()
        except Exception:
            self._logger.error("Real time search exception", exc_info=True)
        finally:
            self._dataManager.setCameraFilter(None)


    ###########################################################
    def _processTimedOut(self, location=None):
        """ Determine if a process timed out

        @param location             camera location associated with the process
                                    ResponseRunner, if None
        """
        isResponseRunner = location is None
        if isResponseRunner:
            timeout = _kResponseRunnerTimeout
            lastPingTime = self._lastResponseRunnerPing
            location = "ResponseRunner"
        else:
            timeout = _kCameraTimeout
            p, camPipe, dataMgrPipe, lastPingTime = self._captureStreams[location]

        # Check if we've timed out based on the last ping
        if lastPingTime+timeout > time.time():
            return False

        # Last ping may still be sitting in the queue, if we get backed up for some reason
        for (msg, depositTime) in self._childProcLocalQueue:
            if (msg[0] == MessageIds.msgIdResponseRunnerPing and
               isResponseRunner) or \
               (msg[0] == MessageIds.msgIdCameraCapturePing and \
                msg[1] == location and not isResponseRunner):
                lastPingTime = depositTime
                if depositTime+timeout > time.time():
                    self._logger.info("Process %s had timed out, but a ping was deposited to the queue %.1f sec ago and not yet processed" %
                            ( location, time.time() - lastPingTime ) )

                    # Update last ping time, so we won't keep scanning the queue
                    # for each message we process
                    if isResponseRunner:
                        self._lastResponseRunnerPing = lastPingTime
                    else:
                        self._captureStreams[location] = (p, camPipe, dataMgrPipe, lastPingTime)
                    return False

        # Nope, we did time out
        self._logger.debug("Process %s had timed out, last ping %.1f sec ago" %
                ( location, time.time() - lastPingTime ) )
        return True

    ###########################################################
    def _isCameraConnected(self, loc):
        """ Determines whether we can send messages to a camera
        """
        status, port, reason = self._cameras.get(loc, (kCameraUndefined, -1, "unknown"))
        return status == kCameraConnecting and port is not None


    ###########################################################
    def _processPendingLiveViewRequests(self, cam):
        """ If any live view requests are outstanding for the camera,
            process them
        """
        if not self._isCameraConnected(cam):
            return

        if cam in self._pendingLiveViewSettings:
            self._setLiveViewParams(self._pendingLiveViewSettings[cam], True)

        if cam in self._pendingLiveViewStatus:
            self._setLiveViewStatus(self._pendingLiveViewStatus[cam], True)

    ###########################################################
    def _processQueueMessage(self, msg): #PYCHECKER too many lines OK
        """Process a message from the queue.

        @param  msg  A list where the first entry is the message id and any
                     additional are parameters as described in MessageIds.py.
        """
        msgId = msg[0]

        if msgId == MessageIds.msgIdQuit:
            self._quit()

        elif msgId == MessageIds.msgIdAnalyticsPortChanged:
            # handle analytics port change here and now
            self._logger.info("Analytics port had changed to %d" % msg[1] )
            self._updateAnalyticsPort(msg)

        # Data manager messages
        elif msgId == MessageIds.msgIdDataAddObject:
            # Newer messages carry a detection-attributes dict; tolerate the
            # older attribute-less message for backward compatibility.
            attrs = None
            if len(msg) > 6:
                (pipeId, camObjId, addTime, objType, location, attrs) = msg[1:]
            else:
                (pipeId, camObjId, addTime, objType, location) = msg[1:]

            dbId = self._dataManager.addObject(addTime, objType, location)

            # Persist detection attributes (face name / demographics / nudity).
            if attrs:
                try:
                    self._dataManager.setObjectAttributes(dbId, **attrs)
                except Exception:
                    self._logger.warning("Failed to set object attributes",
                                         exc_info=True)

            self._logger.info("Received msgIdDataAddObject, loc: %s, time: %i,"
                              "type: %s, dbId: %i, attrs: %s"
                              % (location, addTime, objType, dbId, attrs))

            # A message can outlive its sender: the camera process dies or
            # restarts while its messages are still in the queue, and the
            # reaper has already dropped that pipeId's map and pipe (see
            # _deadPipes / msgIdPipeFinished).  Nothing is left to answer, so
            # skip the temp-id handshake -- the object itself is real and stays
            # in the database.  Letting either the missing key or the closed
            # pipe raise here kills the whole message in run()'s handler.
            tempIds = self._tempIdMap.get(pipeId)
            if tempIds is None:
                self._logger.info("temp id %d from retired pipe %s ('%s'); "
                                  "object kept, id map skipped"
                                  % (camObjId, pipeId, location))
            else:
                tempIds.append((camObjId, dbId))
                dmPipe = self._dataMgrPipes.get(pipeId)
                if dmPipe is not None and location in self._captureStreams:
                    try:
                        dmPipe.send((camObjId, dbId))
                    except (BrokenPipeError, EOFError, OSError) as e:
                        self._logger.warning(
                            "dropped temp-id reply to '%s': pipe closed (%s)"
                            % (location, type(e).__name__))
        elif msgId == MessageIds.msgIdDataAddFrame:
            (pipeId, dbId, frame, frameTime, bbox, objType, action) = msg[1:]
            dbId = self._mapDbId(pipeId, dbId)

            if isinstance(dbId, tuple):
                self._logger.error("Received unknown temp ID: %s" % str(dbId))
            else:
                self._logger.debug("Received msgIdDataAddFrame, dbId: %i"
                                   ", time: %i, bbox: %s, type: %s"
                                   % (dbId, frameTime, bbox, objType))
                self._pendingAddFrames.append((dbId, frame, frameTime, bbox,
                                               objType, action))
        elif msgId == MessageIds.msgIdDataUpdateObjectAttrs:
            # Late attribute update for an already-reported object (e.g. a face
            # recognized after the type vote).  INSERT OR REPLACE semantics —
            # the camera sends the complete, current attrs dict.
            (pipeId, dbId, attrs) = msg[1:]
            dbId = self._mapDbId(pipeId, dbId)

            if isinstance(dbId, tuple):
                self._logger.error("Received unknown temp ID for attr "
                                   "update: %s" % str(dbId))
            elif attrs:
                try:
                    self._dataManager.setObjectAttributes(dbId, **attrs)
                    self._logger.info("Updated attributes, dbId: %i, "
                                      "attrs: %s" % (dbId, attrs))
                except Exception:
                    self._logger.warning("Failed to update object attributes",
                                         exc_info=True)

        # Camera capture messages
        elif msgId == MessageIds.msgIdStreamOpenSucceeded:
            cam = msg[1]
            procSize = msg[2]
            self._logger.info("Received msgIdStreamOpenSucceeded from %s" % cam)
            if cam in self._captureStreams:
                p, camPipe, dataMgrPipe, _ = self._captureStreams[cam]
                self._captureStreams[cam] = (p, camPipe, dataMgrPipe,
                                             time.time())
                # NOTE: These terms should be revised. This message added so
                #       wsgi knows we're no longer in failed state (if we were)
                self._setCameraStatus(cam, kCameraConnecting)

            # Store the processing size for later use, like when new rules are
            # added, edited, or enabled.
            procWidth, procHeight = procSize
            if procWidth > 0 and procHeight > 0:
                self._cameraProcSizes[cam] = (procWidth, procHeight)

            # The camera could be opening with a new processing size; make sure
            # the rules are updated with the new coordinate space.
            if cam in self._cameraProcSizes:
                for ruleName in self._ruleDicts.get(cam, {}):
                    rule, _, _, query, _ = self._ruleDicts[cam][ruleName]
                    if rule.isEnabled():
                        query.setProcessingCoordSpace(self._cameraProcSizes[cam])

            self._processPendingLiveViewRequests(cam)

        elif msgId == MessageIds.msgIdStreamOpenFailed:
            cam = msg[1]
            reason = msg[2]
            self._logger.info("Received msgIdStreamOpenFailed from %s (%s)" % (cam, ensureUnicode(reason)))
            if cam in self._captureStreams:
                p, camPipe, dataMgrPipe, _ = self._captureStreams[cam]
                self._captureStreams[cam] = (p, camPipe, dataMgrPipe,
                                             time.time())
                self._setCameraStatus(cam, kCameraFailed, None, reason)

            # If this camera is a UPnP camera, initiate an active search for it,
            # just to make sure...
            if cam in self._cameraInfo:
                uri, _, _, _ = self._cameraInfo[cam]
                uri = self._realizeUri(uri)

        elif msgId == MessageIds.msgIdCameraCapturePing:
            cam = msg[1]
            self._logger.debug("Received msgIdCameraCapturePing from %s" % cam)
            if cam in self._captureStreams:
                p, camPipe, dataMgrPipe, _ = self._captureStreams[cam]
                self._captureStreams[cam] = (p, camPipe, dataMgrPipe,
                                             time.time())
        elif msgId == MessageIds.msgIdStreamUpdateFrameSize:
            cam = msg[1]
            size = msg[2]
            if cam in self._cameraInfo:
                uri, enabled, monitored, extra = self._cameraInfo[cam]
                prevSize = extra.get('initFrameSize', 0)
                self._logger.debug("Received msgIdStreamUpdateFrameSize from %s oldSize=%d size=%d"
                                % (cam, prevSize, size))
                if prevSize < size:
                    extra['initFrameSize'] = size
                    self._cameraInfo[cam] = (uri, enabled, monitored, extra)
                    # Update the server to propagate to the persistent configuration
                    self._netMsgServerClient.editCameraFrameStorageSize(cam, size)
            else:
                self._logger.debug("Received msgIdStreamUpdateFrameSize from unknown camera %s"
                                % cam)
        elif msgId == MessageIds.msgIdResponseRunnerPing:
            self._logger.debug("Received msgIdResponseRunnerPing")
            self._lastResponseRunnerPing = time.time()
        elif msgId == MessageIds.msgIdPipeFinished:
            self._logger.info("Received msgIdPipeFinished")
            if msg[1] in self._dataMgrPipes:
                del self._dataMgrPipes[msg[1]]
                del self._tempIdMap[msg[1]]
        elif msgId == MessageIds.msgIdStreamProcessedData:
            self._logger.debug("Received msgIdStreamProcessedData, cam: %s, "
                               "ms: %i" % (msg[1], msg[2]))
            self._pendingRealTimeSearches[msg[1]] = msg[2]
            if msg[2] > self._maxProcessedTime.get(msg[1], 0):
                self._cameraProgressTime[msg[1]] = time.time()
            self._maxProcessedTime[msg[1]] = msg[2]
        elif msgId == MessageIds.msgIdAddSavedTimes:
            cam, timeRanges = msg[1:]
            self._logger.debug("Received msgIdAddSavedTimes, loc: %s, times: %s"
                               % (cam, str(timeRanges)))
            # Track the highest tagged times for this camera
            prevTagged = self._lastTaggedTimes.get(cam, 0)
            for _, stop in timeRanges:
                prevTagged = max(prevTagged, stop)
            self._lastTaggedTimes[cam] = prevTagged

            if cam in self._captureStreams:
                _, pipe, _, _ = self._captureStreams[cam]
                # Ensure the camera can handle AddSavedTimes messages.
                if cam not in self._selfAddSavedTimes:
                    self._sendMsg(pipe, msg, cam)
                    return

            # If the camera didn't exist anymore or if we marked it terminated
            # will add the saved times to the database.

            retry = self._clipManager.markTimesAsSaved(cam, msg[2], True)
            if retry:
                self._delayedMessagesQueue.put((int(retry*1000), msg))

        elif msgId == MessageIds.msgIdTestCameraFailed:
            self._logger.info("Received msgIdTestCameraFailed")
            self._netMsgServerClient.setTestCameraFailed(True)
        elif msgId == MessageIds.msgIdSetCamCanTerminate:
            # Upon receiving this message we will no longer send AddSaveTime
            # messages to the camera process, and will send it a confirmation
            # that we received this.  When it recieves that message it will
            # tell us that it is ready to be killed.  This avoids a 'not saved
            # by a rule' occurance in certain timings.
            location = msg[1]
            self._logger.info("Received msgIdSetCamCanTerminate, loc: %s"
                              % location)
            if location in self._captureStreams:
                _, pipe, _, _ = self._captureStreams[location]
                self._selfAddSavedTimes.append(location)
                self._sendMsg(pipe, msg, location)
        elif msgId == MessageIds.msgIdSetTerminate:
            # Upon receiving this message we know the camera process is
            # waiting to be killed.  We will set its ping time to zero
            # causing it to be soon terminated by the run loop.
            location = msg[1]
            self._logger.info("Received msgIdSetTerminate, loc: %s"
                              % location)
            if location in self._captureStreams:
                p, pipe, pipeId, _ = self._captureStreams[location]
                self._captureStreams[location] = (p, pipe, pipeId, 0)
        elif msgId == MessageIds.msgIdWsgiPortChanged:
            # The camera process' web server is now operating on a new (or just
            # different) port, so we have to remember that.
            location = msg[1]
            port = msg[2]
            self._logger.info("Received msgIdWsgiPortChanged, loc: %s, port: %d" % (location, port))
            if location in self._captureStreams:
                self._setCameraStatus(location, None, port)
            self._processPendingLiveViewRequests(location)

        # Camera management messages
        elif msgId == MessageIds.msgIdCameraAdded:
            loc = msg[1]
            self._logger.info("Received msgIdCameraAdded, loc: %s, uri: %s"
                              % (loc, sanitizeUrl(msg[2])))

            # If we still have data from a prior cam with this name, remove it.
            self._cleanupCameraData(loc)

            # Save the camera URI
            self._cameraInfo[loc] = (msg[2], True, False, msg[3])
            self._syncCameraStateWithSchedule(loc)

            # If this location previously existed with alternate name
            # capitalization rename the old locations.
            dmNames = self._clipManager.getCameraLocations()
            for name in dmNames:
                if (name.lower() == loc.lower()) and (name != loc):
                    self._dataManager.updateLocationName(name, loc, 0)
                    self._clipManager.updateLocationName(name, loc, 0,
                            self._videoDir, self._userLocalDataDir)
                    self._dataManager.save()
                    self._clipManager.save()

        elif msgId == MessageIds.msgIdCameraEdited:
            origLoc = msg[1]
            newLoc = msg[2]
            uri = msg[3]
            extra = msg[4]
            changeSecs = msg[5]
            self._logger.info("Received msgIdCameraEdited, origLoc: %s, "
                              "curLoc: %s, uri: %s, extra: %s time: %s"
                              % (origLoc, newLoc, sanitizeUrl(uri), str(extra),
                                 str(changeSecs)))

            queuedMsg = [MessageIds.msgIdNone]

            if (changeSecs == -1) and (origLoc == newLoc):
                self._cameraInfo[newLoc] = (uri, self._cameraInfo[origLoc][1],
                                            self._cameraInfo[origLoc][2],
                                            extra)
            else:
                # For the rename at a given time we want to stop the old camera
                # and not yet run the new.  We'll set enabled to false now and
                # enable it after the rename is complete.
                self._cameraInfo[newLoc] = (uri, False,
                                            self._cameraInfo[origLoc][2],
                                            extra)
                if self._cameraInfo[origLoc][1]:
                    queuedMsg = [MessageIds.msgIdCameraEnabled, newLoc]

            if origLoc != newLoc:
                renameMessage = [MessageIds.msgIdRenameCamera, origLoc, newLoc, changeSecs, queuedMsg]
                cleanupTime = time.time() + _kRuleCleanupTimeout
                if origLoc in self._captureStreams:
                    proc, pipe, _, _ = self._captureStreams[origLoc]
                    self._sendMsg(pipe, [MessageIds.msgIdQuitWithResponse, renameMessage], origLoc)
                    self._pendingRenameMsg = (cleanupTime+10, proc, renameMessage)
                else:
                    # If the camera isn't running we won't be getting back the
                    # rename message so we need to send it to ourselves.
                    self._processQueueMessage(renameMessage)
                if origLoc in self._cameraInfo:
                    del self._cameraInfo[origLoc]
                if origLoc in self._ruleDicts:
                    self._pendingRuleCleanupDict[origLoc] = cleanupTime

                # If we still have data from a prior cam with this name, remove it.
                self._cleanupCameraData(newLoc)

            self._stopCamera(origLoc)

            # Open the camera with the new information if it is scheduled
            self._syncCameraStateWithSchedule(newLoc)
        elif msgId == MessageIds.msgIdCameraDeleted:
            camLoc = msg[1]
            self._logger.info("Received msgIdCameraDeleted, loc: %s" % camLoc)
            proc = None
            if camLoc in self._captureStreams:
                proc, _, _, _ = self._captureStreams[camLoc]
            self._stopCamera(camLoc)
            if camLoc in self._cameraInfo:
                del self._cameraInfo[camLoc]
            if camLoc in self._ruleDicts:
                self._pendingRuleCleanupDict[camLoc] = \
                                        time.time()+_kRuleCleanupTimeout
            if msg[2]:
                # We want to ensure that no more data will be added from this
                # location.  Since we don't care about anything pending or that
                # it will do, we just kill it.
                if proc:
                    self._terminateCameraProcess(proc)
                self._putMsgDC([MessageIds.msgIdRemoveDataAtLocation, camLoc])
                self._dataManager.removeCameraLocation(camLoc)
        elif msgId == MessageIds.msgIdCameraEnabled:
            camLoc = msg[1]
            self._logger.info("Received msgIdCameraEnabled, loc: %s" % (camLoc))
            # A manual enable means "start it NOW" -- drop any restart backoff.
            self._clearCameraBackoff(camLoc)
            # Update the enabled status.
            self._cameraInfo[camLoc] = (self._cameraInfo[camLoc][0], True,
                                        self._cameraInfo[camLoc][2],
                                        self._cameraInfo[camLoc][3])
            # Run the camera if it is currently scheduled.
            camState = self._syncCameraStateWithSchedule(camLoc)
            if camLoc not in self._captureStreams:
                if camState is None:
                    camState=kCameraOn
                self._setCameraStatus(camLoc, camState)
        elif msgId == MessageIds.msgIdCameraDisabled:
            camLoc = msg[1]
            self._logger.info("Received msgIdCameraDisabled, loc: %s" % camLoc)
            # Update the enabled status.
            self._cameraInfo[camLoc] = (self._cameraInfo[camLoc][0], False,
                                        self._cameraInfo[camLoc][2],
                                        self._cameraInfo[camLoc][3])
            # Stop the camera if it was running
            self._stopCamera(camLoc)
        elif msgId == MessageIds.msgIdRenameCamera:
            self._logger.info("Received msgIdRenameCamera, old: %s, new: %s, "
                              "changeTime: %s" % (msg[1], msg[2], str(msg[3])))
            # Convert to ms from seconds
            changeMs = msg[3]*1000

            # Remove any pending rename message.
            self._pendingRenameMsg = None

            # Ensure that objects and tagged times have been committed.
            self._flushIdleQueue()

            # TODO: This is slow....might need to fix this.  Unfortunately what
            #       follows is also slow (splitting clips, updating both
            #       databases...not sure where a 'good' place to put all this
            #       would really be...
            changeClip = self._clipManager.getFileAt(msg[1], changeMs, 1000)
            if changeMs != 0 and changeClip:
                # If we're going to be cutting a clip we need to find an exact
                # ms that occurred so we can properly update the created clips
                # and database objects.
                msList = getMsList(os.path.join(self._videoDir, changeClip), self._logger.getCLogFn())

                first, _ = self._clipManager.getFileTimeInformation(changeClip)

                # Clamp to the LAST index, not one past it -- bisect_left
                # returns len(msList) for a time at or after the end of the
                # file, and an unreadable clip returns an empty list.  Both
                # reach this line; both used to raise IndexError and abandon
                # the rename half-done.  Matches ClipManager/DiskCleaner.
                if msList:
                    bisectIndex = bisect.bisect_left(msList, changeMs-first)
                    bisectIndex = max(0, min(bisectIndex, len(msList)-1))
                    changeMs = msList[bisectIndex]+first
                else:
                    self._logger.warning(
                        "No frame times for %s; renaming at the requested time "
                        "%d without snapping to a frame" % (changeClip, changeMs))

            self._dataManager.updateLocationName(msg[1], msg[2], changeMs)
            self._clipManager.updateLocationName(msg[1], msg[2], changeMs,
                    self._videoDir, self._userLocalDataDir)
            self._dataManager.save()
            self._clipManager.save()

            if len(msg) > 4:
                self._processQueueMessage(msg[4])
        elif msgId == MessageIds.msgIdRuleReloadAll or \
             msgId == MessageIds.msgIdHardwareAccelerationSettingUpdated:
            if msgId == MessageIds.msgIdHardwareAccelerationSettingUpdated:
                self._hardwareDevice = msg[2]
            try:
                self._logger.info("turning off all cameras...")
                for cameraLocation in list(self._captureStreams.keys()):
                    self._stopCamera(cameraLocation)
                self._logger.info("loading updated camera info...")
                camMgr = CameraManager(self._logger)
                camMgr.load(msg[1])
                self._ruleDicts = {}
                self._logger.info("rules cleared, reloading them...")
                self._loadRules(camMgr)
                self._logger.info("restarting cameras...")
                self._initVideoStreams(camMgr)
            except:
                self._logger.error("rules reloading failed (%s)" %
                                   str(sys.exc_info()[1]))
                self._logger.error(traceback.format_exc())
            finally:
                self._netMsgServerClient.memstorePut(kMemStoreRulesLock,
                                                     False, -1)
                self._logger.info("rules got unlocked")
        elif msgId == MessageIds.msgIdCameraTestStart:
            uri = msg[1]
            forceTCP = msg[2]
            self._logger.info("Received msgIdCameraTestStart, uri: %s, tcp: %s" %
                              (sanitizeUrl(uri), forceTCP))
            self._startTestCamera(uri, {'forceTCP' : forceTCP})
        elif msgId == MessageIds.msgIdCameraTestStop:
            self._logger.info("Received msgIdCameraTestStop")
            self._stopTestCamera()
        elif msgId == MessageIds.msgIdPacketCaptureStart:
            cameraLocation = msg[1]
            delaySeconds = msg[2]
            pcapDir = msg[3]
            self._logger.info(
                "Received msgIdPacketCaptureStart, camLoc: %s, delaySeconds: %s, pcapDir: %s" %
                (cameraLocation, delaySeconds, pcapDir)
            )
            self._startPacketCapture(cameraLocation, delaySeconds, pcapDir)
            self._netMsgServerClient.setPacketCaptureInfo(
                    pickle.dumps(self._pcapInfo)
            )
        elif msgId == MessageIds.msgIdPacketCaptureStop:
            self._logger.info("Received msgIdPacketCaptureStop")
            self._stopPacketCapture()
            self._netMsgServerClient.setPacketCaptureInfo(
                    pickle.dumps({})
            )
        elif msgId == MessageIds.msgIdPacketCaptureStatus:
            code = msg[1]
            description = msg[2]
            self._pcapInfo['pcapStatus'] = code
            self._netMsgServerClient.setPacketCaptureInfo(
                    pickle.dumps(self._pcapInfo)
            )
            self._logger.info(
                "Received msgIdPacketCaptureStatus, code: %s, description: %s" %
                (code, description)
            )
        elif msgId == MessageIds.msgIdPacketCaptureEnabled:
            code = msg[1]
            self._pcapInfo['pcapEnabled'] = code
            self._netMsgServerClient.setPacketCaptureInfo(
                    pickle.dumps(self._pcapInfo)
            )
            self._logger.info("Received msgIdPacketCaptureEnabled, code: %s" %
                              code)
        elif msgId == MessageIds.msgIdDeleteVideo:
            camLoc = msg[1]
            startMs = msg[2]*1000
            stopMs = msg[3]*1000
            quick = msg[4]

            if camLoc in self._captureStreams and \
               camLoc in self._maxProcessedTime and \
               self._maxProcessedTime[camLoc] < stopMs:
                # If the camera is still running and hasn't finished processing
                # through our delete time we have to delay the delete, otherwise
                # it is possible that data will later come in and we'll wind up
                # with video not found errors.
                self._logger.info("Received msgIdDeleteVideo, loc: %s, start: %d, "
                                "stop: %d, cur: %d" % (camLoc, startMs, stopMs, self._maxProcessedTime[camLoc]))
                kDeletionRetryInterval = 1000
                self._delayedMessagesQueue.put((getTimeAsMs()+kDeletionRetryInterval,msg))
                return

            self._logger.info("Received msgIdDeleteVideo, loc: %s, start: %s, "
                              "stop: %s" % (camLoc, str(startMs), str(stopMs)))

            # Ensure that objects and tagged times have been committed.
            self._flushIdleQueue()

            self._dataManager.deleteCameraLocationDataBetween(camLoc, startMs,
                                                              stopMs)
            if quick:
                # If we're only doing a quick delete we can skip the clip
                # manager cleanup.
                return

            failedDeletes = self._clipManager.deleteCameraLocationDataBetween(
                                        camLoc, startMs, stopMs, self._videoDir,
                                        self._userLocalDataDir)
            for path in failedDeletes:
                self._logger.info("Queuing file for deletion: %s" % path)
                self._putMsgDC([MessageIds.msgIdDeleteFile, path])
        elif msgId == MessageIds.msgIdSetOnvifSettings:
            (uuid, selectedIp, username, password) = msg[1:]
            # Applying credentials is part of the Add-Camera wizard flow, so
            # keep on-demand discovery alive here (and start it if it somehow
            # got torn down while the user lingered on the credentials page).
            self._lastCameraSearchTime = time.time()
            self._startOnvifScanner()
            if self._onvifScanner is not None:
                self._onvifScanner.setDeviceSettings(
                    uuid, (username, password), selectedIp)
            else:
                self._logger.warning(
                    "Could not start ONVIF scanner to apply device settings")
        elif msgId == MessageIds.msgIdActiveCameraSearch:
            (isMajor, ) = msg[1:]
            self._logger.debug("Received msgIdActiveCameraSearch: %s" %
                               str(isMajor))

            # The Add-Camera wizard is the only consumer of ONVIF/UPnP
            # discovery, and it re-requests a search every few seconds while
            # open.  Discovery is off by default, so spin it up on demand here
            # (no-op if already running / persistent) and arm the idle-teardown
            # timer -- the main loop stops it again once the wizard goes quiet.
            self._lastCameraSearchTime = time.time()
            self._startUpnpScanner()
            self._startOnvifScanner()

            # Do an active search on a major request; this is called once when
            # the camera wizard comes up...
            if isMajor:
                if not self._upnpScanner is None:
                    self._upnpScanner.force()
                if not self._onvifScanner is None:
                    self._onvifScanner.force()

            # Always do webcam search...
            # ...this is not super fast; hopefully we don't take up too much
            # back end time doing this...
            localCameraNames = strmGetLocalCameraNames(self._logger.getCLogFn())
            self._logger.info("Active camera search, found " +
                    str(localCameraNames))
            self._netMsgServerClient.setLocalCameraNames(localCameraNames)
        elif msgId == MessageIds.msgIdFileMoveFailed:
            self._logger.info("Received msgIdFileMoveFailed, target: %s"
                              % msg[1])
            self._pendingFileMoves[msg[1]] = (msg[2],
                                              time.time()+_kTmpFileLifetime)

        # Storage setting messages
        elif msgId == MessageIds.msgIdSetMaxStorage:
            self._logger.info("Received msgIdSetMaxStorage, size: %i" % msg[1])
            self._putMsgDC(msg)
            self._maxStorage = msg[1]
        elif msgId == MessageIds.msgIdSetVideoLocation:
            videoDir = msg[1]
            moveData = msg[2]
            preserveExisting = msg[3]
            self._logger.info("Received msgIdSetVideoLocation, loc: %s, "
                              "move: %s, preserveExisting: %s"
                              % (videoDir, moveData, preserveExisting))
            self._setNewVideoLocation(videoDir, moveData, preserveExisting)
        # Storage setting messages
        elif msgId == MessageIds.msgIdSetCacheDuration:
            self._logger.info("Received msgIdSetCacheDuration, hours: %i" % msg[1])
            self._putMsgDC(msg)
            self._cacheDuration = msg[1]
        elif msgId == MessageIds.msgIdSetRecordInMemory:
            self._logger.info("Received msgIdSetRecordInMemory, value: %s" % str(msg[1]))
            restart = (msg[1] != self._recordInMemory)
            self._recordInMemory = msg[1]
            # propagate this setting to camera processes
            if restart:
                for camLoc in self._captureStreams:
                    self._childProcLocalQueue.append(([MessageIds.msgIdCameraDisabled, camLoc], time.time()))
                    self._childProcLocalQueue.append(([MessageIds.msgIdCameraEnabled, camLoc], time.time()))
        elif msgId == MessageIds.msgIdSetClipMergeThreshold:
            value = msg[1]
            effectiveTime = getTimeAsMs()
            self._logger.info("Modifying clip merge preferences to %d at %d" % (value, effectiveTime))
            self._clipMergeThreshold = value
            self._clipManager.setClipMergeThreshold(effectiveTime, value, True)
            self._broadcastMsg([MessageIds.msgIdSetClipMergeThreshold, effectiveTime, value])
        elif msgId == MessageIds.msgIdSetEmailSettings:
            # Update existing settings so that response gets updated right away.
            self._emailSettings.update(msg[1])
            self._logger.info("Received msgIdSetEmailSettings")

        elif msgId == MessageIds.msgIdSetFtpSettings:
            # Forward onto the response runner queue...
            self._putMsgRR(msg)
            self._ftpSettings.update(msg[1])
            self._logger.info("Received msgIdSetFtpSettings")

        elif msgId == MessageIds.msgIdSetNotificationSettings:
            # Forward onto the response runner queue...
            self._putMsgRR(msg)
            self._notificationSettings.update(msg[1])
            self._logger.info("Received msgIdSetNotificationSettings")

        elif msgId == MessageIds.msgIdSetTimePrefs:
            self._logger.info("Received msgIdSetTimePrefs")
            self._timePrefs = (msg[1], msg[2])
            self._broadcastMsg([MessageIds.msgIdSetTimePrefs, msg[1], msg[2]])

        elif msgId == MessageIds.msgIdSetVideoSetting:
            self._logger.info("Received msgIdSetVideoSetting")
            self.videoSettings[msg[1]] = msg[2]
            # propagate live video quality setting to camera processes
            self._broadcastMsg(msg)

        elif msgId == MessageIds.msgIdSetDebugConfig:
            self._broadcastMsg(msg)
            self._putMsgDC(msg)
            self._putMsgRR(msg)
            self._putMsgWS(msg)
            global _kDebugConfig
            _kDebugConfig = msg[1]
            self._debugLogManager.SetLogConfig(_kDebugConfig)

        # Rule messages
        elif msgId == MessageIds.msgIdRuleAdded:
            ruleName = msg[1].lower()
            self._logger.info("Received msgIdRuleAdded, rule: %s" % ruleName)

            # Retrieve the new rule, query, schedule information, and responses
            rule = pickle.loads(msg[2])
            queryModel = pickle.loads(msg[3])
            query = queryModel.getUsableQuery(self._dataManager)
            camLoc = queryModel.getVideoSource().getLocationName()
            isScheduled, nextSchedChange = rule.getScheduleInfo()
            responses = self._loadResponses(queryModel, camLoc, query, ruleName)

            self._putMsgRR([MessageIds.msgIdSetLocalExportSettings, self._localExportSettings])

            # Tell the query about the processing size if the camera is open.
            if camLoc in self._cameraProcSizes:
                procSize = self._cameraProcSizes[camLoc]
                query.setProcessingCoordSpace(procSize)

            # Add to the rule dict
            if camLoc not in self._ruleDicts:
                self._ruleDicts[camLoc] = {}
            self._ruleDicts[camLoc][ruleName] = \
                        (rule, isScheduled, nextSchedChange, query, responses)

            # Ensure the related camera is now running if scheduled.
            self._syncCameraStateWithSchedule(camLoc)

        elif msgId == MessageIds.msgIdRuleScheduleUpdated:
            # Find the edited rule in ruleDicts
            ruleName = msg[1].lower()
            schedule = msg[2]

            self._logger.info("Received msgIdRuleScheduleUpdated, rule: %s"
                              % ruleName)

            for camLoc in self._ruleDicts:
                if ruleName in self._ruleDicts[camLoc]:
                    # Update the rule schedule
                    rule, _, _, query, responses = \
                                            self._ruleDicts[camLoc][ruleName]
                    rule.setSchedule(schedule)
                    isScheduled, nextChange = rule.getScheduleInfo()
                    self._ruleDicts[camLoc][ruleName] = \
                            (rule, isScheduled, nextChange, query, responses)
                    # Tell the query about the processing size if the camera is open.
                    if camLoc in self._cameraProcSizes:
                        procSize = self._cameraProcSizes[camLoc]
                        query.setProcessingCoordSpace(procSize)
                    if not isScheduled:
                        query.reset()

                    # Ensure the related camera is now running if scheduled.
                    self._syncCameraStateWithSchedule(camLoc)
        elif msgId == MessageIds.msgIdRuleDeleted:
            # Delete the specified rule
            ruleName = msg[1].lower()
            self._logger.info("Received msgIdRuleDeleted, rule: %s" % ruleName)

            for camLoc in self._ruleDicts:
                if ruleName in self._ruleDicts[camLoc]:
                    del self._ruleDicts[camLoc][ruleName]

                    # Ensure the related camera is stopped if not scheduled.
                    self._syncCameraStateWithSchedule(camLoc)

            if ruleName in self._localExportSettings:
                del self._localExportSettings[ruleName]

        elif msgId == MessageIds.msgIdRuleEnabled:
            # Enable or disable the specified rule
            ruleName = msg[1].lower()
            enabled = msg[2]

            self._logger.info("Received msgIdRuleEnabled, rule: %s, "
                              "enabled: %s" % (ruleName, str(enabled)))

            for camLoc in self._ruleDicts:
                if ruleName in self._ruleDicts[camLoc]:
                    rule, _, _, query, responses = \
                                            self._ruleDicts[camLoc][ruleName]
                    # Tell the query about the processing size if the camera is open.
                    if camLoc in self._cameraProcSizes:
                        procSize = self._cameraProcSizes[camLoc]
                        query.setProcessingCoordSpace(procSize)

                    rule.setEnabled(enabled)
                    if not enabled:
                        query.reset()

                    # Ensure the related camera is running if scheduled.
                    self._syncCameraStateWithSchedule(camLoc)

        # Camera viewing messages
        elif msgId == MessageIds.msgIdEnableLiveView:
            cameraLocation = msg[1]
            if not self._isCameraConnected(cameraLocation):
                self._logger.info("Delaying enabling live view until %s is connected" % cameraLocation)
                self._pendingLiveViewStatus[cameraLocation] = msg
            else:
                self._setLiveViewStatus(msg, False)
        elif msgId == MessageIds.msgIdDisableLiveView:
            cameraLocation = msg[1]
            if not self._isCameraConnected(cameraLocation):
                self._logger.info("Delaying disabling live view until %s is connected" % cameraLocation)
                self._pendingLiveViewStatus[cameraLocation] = msg
            else:
                self._setLiveViewStatus(msg, False)
        elif msgId == MessageIds.msgIdFlushVideo:
            cameraLocation = msg[1]
            if self._isCameraConnected(cameraLocation):
                self._logger.debug("Received msgIdFlushVideo, loc: %s" % cameraLocation)
                self._sendMsgLoc(msg, cameraLocation)

                # This likely means a search is about to occur.  Try to move any
                # pending files now to avoid video not founds.
                self._moveTmpFiles()
        elif msgId == MessageIds.msgIdSetLiveViewParams:
            cameraLocation = msg[1]
            if not self._isCameraConnected(cameraLocation):
                self._logger.debug("Delaying setting live view params until %s is connected"  % ensureUnicode(cameraLocation))
                self._pendingLiveViewSettings[cameraLocation] = msg
            else:
                self._setLiveViewParams(msg, False)
        # Error messages
        elif msgId == MessageIds.msgIdInsufficientSpace:
            self._logger.warning("Received msgIdInsufficientSpace — disk cleaner could not free sufficient space")
            self._netMsgServerClient.addMessage([MessageIds.msgIdOutOfDiskSpace,
                                                 self._videoDir])
        elif msgId == MessageIds.msgIdCriticalDiskSpace:
            critical = bool(msg[1])
            pctFree = msg[2]
            drive = msg[3]
            if critical and not self._lowDiskPaused:
                self._lowDiskPaused = True
                self._logger.warning("Critically low disk (%d%% free on %s) — "
                                     "pausing recording on all cameras" %
                                     (pctFree, drive))
                # Stop every running camera.  _syncCameraStateWithSchedule and
                # _openCamera honor _lowDiskPaused, so nothing restarts them
                # until we clear the flag; user "enabled" flags are untouched.
                for camLoc in list(self._captureStreams.keys()):
                    self._stopCamera(camLoc)
                self._netMsgServerClient.addMessage(
                    [MessageIds.msgIdOutOfDiskSpace, drive, pctFree, True])
            elif (not critical) and self._lowDiskPaused:
                self._lowDiskPaused = False
                self._logger.warning("Disk recovered (%d%% free on %s) — "
                                     "resuming recording" % (pctFree, drive))
                # Restart cameras per their enabled state + schedule.
                for camLoc in list(self._cameraInfo.keys()):
                    self._syncCameraStateWithSchedule(camLoc)
                self._netMsgServerClient.addMessage(
                    [MessageIds.msgIdOutOfDiskSpace, drive, pctFree, False])
        elif msgId == MessageIds.msgIdHealthInfo:
            # Latest health value from a worker.  CameraCapture uses the same
            # existing health channel with kind='camera' and supplies the
            # camera name as msg[2].
            try:
                if len(msg) >= 4 and msg[1] == 'camera':
                    self._netMsgServerClient.setHealthInfo(
                        'camera', [msg[2], msg[3]])
                else:
                    self._netMsgServerClient.setHealthInfo(msg[1], msg[2])
            except Exception:
                self._logger.warning("Could not store health info %r" % (msg[1],))
        elif msgId == MessageIds.msgIdClockSkew:
            offset = float(msg[1])
            self._logger.warning(
                "PC clock is %.2fs %s real time — recorded times will not "
                "match the cameras' own timestamps; notifying user" %
                (abs(offset), "behind" if offset > 0 else "ahead of"))
            self._netMsgServerClient.addMessage(
                [MessageIds.msgIdClockOutOfSync, offset])
        elif msgId == MessageIds.msgIdDatabaseCorrupt:
            self._logger.info("Received msgIdDatabaseCorrupt")
            self._handleCorruptDatabase()

        # Webserver messages
        elif msgId == MessageIds.msgIdWebServerSetPort:
            # LAN record viewer is a free, self-hosted feature here; no paid-
            # edition gate (that guarded the retired cloud remote-access path).
            newPort = msg[1]
            self._logger.info("web server port changing to %d ..." % newPort)
            # Kept so a watchdog restart uses the new port, not the old one.
            self._webPort = newPort
            self._putMsgWS([msg[0], newPort])
        elif msgId == MessageIds.msgIdWebServerSetAuth:
            self._logger.info("web server auth changed")
            self._webAuth = msg[1]
            self._putMsgWS([msg[0], msg[1]])
        elif msgId == MessageIds.msgIdWebServerEnablePortOpener:
            self._logger.info("port opener enabled flag changed (%s)" % msg[1])
            self._putMsgWS([msg[0], msg[1]])
        elif msgId == MessageIds.msgIdWebServerPing:
            pass

        # Licensing messages
        elif msgId == MessageIds.msgIdUserLogin:
            self._licenseManager.userLogin(msg[1], msg[2], msg[3])
            self._putMsgRR([MessageIds.msgIdSetServicesAuthToken, self._licenseManager.getAuthToken()])
        elif msgId == MessageIds.msgIdRefreshLicenseList:
            self._licenseManager.listRefresh(msg[1])
        elif msgId == MessageIds.msgIdAcquireLicense:
            self._licenseManager.acquire(msg[1], msg[2])
        elif msgId == MessageIds.msgIdUnlinkLicense:
            self._licenseManager.unlink(msg[1])
        elif msgId == MessageIds.msgIdUserLogout:
            self._licenseManager.userLogout()

        # Test IFTTT response
        elif msgId == MessageIds.msgIdTriggerIfttt:
            self._putMsgRR(msg)
        elif msgId == MessageIds.msgIdSendIftttState:
            self._iftttStatePending = (msg[1], msg[2])
        elif msgId == MessageIds.msgIdUpdateUpnp:
            self._updateUpnp(msg[1], msg[2], msg[3])
        elif msgId == MessageIds.msgIdUpdateOnvif:
            self._updateOnvif(msg[1], msg[2], msg[3])
        elif msgId == MessageIds.msgIdSubmitClipToSighthound:
            camera = msg[1]
            note = msg[2]
            startTime = int(msg[3])
            duration = int(msg[4])
            self._logger.info("Got a request to upload clip from %s at [%d-%d]" % \
                        (camera, startTime, startTime+duration))
            if self._clipUploader is None:
                self._clipUploader = ClipUploader(self._logger,
                                            self._licenseManager.getAccountId(),
                                            self._licenseManager.getAuthToken(),
                                            self._clipDbPath,
                                            self._objDbPath,
                                            self._videoDir,
                                            self._userLocalDataDir)
            self._clipUploader.queueUpload(camera, note, startTime, duration)
        else:
            self._logger.error("unknown message identifier %d" % msgId)


    ###########################################################
    def _mapDbId(self, pipeId, dbId):
        """Handle the fact that the queued data manger might give a temp ID.

        If the queued data manager needs to add object frames before it has
        received the real dbId on the pipe, it will use a temporary ID that
        is a tuple of (camId, camObjId).  This function will take in whatever
        type of ID the queued data manager provides and will return a real dbId.

        This function will also do maintenance on the self._tempIdMap.
        Specifically:
        - If the queued data manager provides us with a real dbId that is in
          our map, it means that the queued data manager has received the
          real ID on the pipe and will no longer be using the temp ID.  We
          can delete it from the map.  We also delete anything older just as
          a general cleanup task, since we know that the queued data manager
          always receives things in order.
        - If the queued data manager provides us with something that is in
          our map, we do the mapping.
        - If the queued data manager provides us with another ID, we assume it's
          a real dbId and just return.

        @param  pipeId    The pipeId ID.
        @param  dbId      The database ID from the queued data manager; may be
                          a real dbId or a temp one.
        @return realDbId  A dbId that is guaranteed to be real, unless there is
                          a serious error (in which case it might still be a
                          tuple).
        """
        # This shouldn't happen, but better to be paranoid...
        if pipeId not in self._tempIdMap:
            return dbId

        i = 0
        for (thisCamObjId, thisDbId) in self._tempIdMap[pipeId]:
            if dbId == thisDbId:
                self._logger.debug("Real ID (%d) used; deleting (%d, %d)" % (
                                   thisDbId, pipeId, thisCamObjId))

                # They're using the real DB ID.  Delete old temp mappings...
                del self._tempIdMap[pipeId][:i+1]
                break
            elif dbId == (pipeId, thisCamObjId):
                self._logger.debug("Temp ID (%d, %d) used; returning (%d)" % (
                                   pipeId, thisCamObjId, thisDbId))
                dbId = thisDbId
                break
            i += 1

        return dbId


    ###########################################################
    def alreadyRunning(self):
        """Determine whether an instance is already running.

        @return alreadyRunning  True if another instance is running.
        """
        client = self._getXMLRPCClient()
        if client is None:
            return False

        # if there is a port file but we can't connect to the server, assume
        # the previous server terminated improperly.
        try:
            if 'dead' != client.ping(): #PYCHECKER OK: Function exists on xmlrpc server
                return True
        except Exception:
            pass

        # Wait and try again in the off chance the server was still starting
        time.sleep(1)
        try:
            if 'dead' != client.ping(): #PYCHECKER OK: Function exists on xmlrpc server
                return True
        except Exception:
            pass

        return False


    ###########################################################
    def _getXMLRPCClient(self, timeout=_kNetworkMessageServerTimeout):
        """Return a connection to the xmlrpc server.

        @param  timeout  Connection timeout, in milliseconds.
        @return client   A client connection to the xmlrpc server, or None.
        """
        # If there is no port file, we're not running
        try:
            portFile = open(
                os.path.join(self._userLocalDataDir, kPortFileName), 'rb')
            port = pickle.load(portFile)
            portFile.close()
        except Exception:
            return None

        return ServerProxyWithClientId('http://127.0.0.1:%s' % str(port),
                TimeoutTransport(timeout), allow_none=True)


    ###########################################################
    def _getCameraScheduleStatus(self, cameraLocation, useCache=True):
        """Retrieve a camera's scheduled status

        @param  cameraLocation  The name of the camera to retrieve info for.
        @param  useCache        True if cached values should be used, False to
                                requery each rule.
        @return isScheduled     True if the camera is currently scheduled
        @return nextChange      The next time in ms the status will change, or
                                None.
        """
        ruleDict = self._ruleDicts.get(cameraLocation, {})

        isScheduled = False
        nextChange = None

        for ruleName in ruleDict:
            rule, scheduled, change, query, responses = ruleDict[ruleName]
            if (not rule.isEnabled()) or (not responses):
                continue

            if not useCache:
                scheduled, change = rule.getScheduleInfo()
                ruleDict[ruleName] = (rule, scheduled, change, query, responses)

            isScheduled = isScheduled or scheduled
            if change and (not nextChange or (nextChange > change)):
                nextChange = change

        return isScheduled, nextChange


    ###########################################################
    def _syncCameraStateWithSchedule(self, cameraLocation):
        """Ensure a camera is in the state dictated by its current schedule.

        @param  cameraLocation  The camera to check.
        @return camState        The new camera state if it changed, 'None'
                                otherwise.
        """
        camState = None
        if cameraLocation not in self._cameraInfo:
            return camState

        # Recording is paused for critically low disk — keep every camera
        # stopped regardless of enabled/schedule until space recovers.
        if self._lowDiskPaused:
            return self._stopCamera(cameraLocation)

        _, enabled, _, extra = self._cameraInfo[cameraLocation]

        if not enabled or extra.get('frozen', False):
            # If we're not currently enabled or frozen, ensure we're not running.
            camState = self._stopCamera(cameraLocation)

        # If we're in auto record mode, check the schedule and act accordingly.
        scheduled, changeTime = self._getCameraScheduleStatus(cameraLocation)

        if changeTime and changeTime < time.time():
            # If this is true we need to update the cache.
            scheduled, changeTime = self._getCameraScheduleStatus(
                cameraLocation, False)

        if scheduled:
            camState = self._openCamera(cameraLocation) or camState
        else:
            camState = self._stopCamera(cameraLocation) or camState

        return camState


    ###########################################################
    def _setNewVideoLocation(self, location, preserveData=True,
                             keepExisting=False):
        """Set a new video location to store archived video.

        @param  location      The path at which to store video.
        @param  preserveData  If True existing data should be moved to the new
                              location.
        @param  keepExisting  If True and preserveData is False, data at the
                              new location will not be removed and the
                              databases will not be reset.
        """
        if type(location) == bytes:            location = location.decode('utf-8')
        success = False
        try:
            self._logger.info("Shutting down processes.")
            # Stop cameras, disk cleaner, and response runner.
            procs = [streamInfo[0] for streamInfo in list(self._captureStreams.values())]
            for cameraLocation in list(self._captureStreams.keys()):
                self._stopCamera(cameraLocation)

            if self._diskCleanupProc:
                procs.append(self._diskCleanupProc)
                self._putMsgDC([MessageIds.msgIdQuit])

            if self._responseRunnerProc:
                procs.append(self._responseRunnerProc)
                self._putMsgRR([MessageIds.msgIdQuit])

            # We'll wait for a while to let them quit gracefully, but we don't
            # want it to be forever...
            startTime = time.time()
            anyAlive = True
            while anyAlive and (time.time()-startTime < 45):
                for proc in procs:
                    if proc.is_alive():
                        time.sleep(1)
                        break
                else:
                    anyAlive = False

            self._logger.info("Terminating any remaining processes.")

            # Ensure all processes are dead in case they didn't quit themselves.
            for proc in procs:
                self._terminateCameraProcess(proc)

            destination = os.path.join(location, kVideoFolder)
            if preserveData:
                # Move data.
                try:
                    if os.path.isdir(self._videoDir):
                        self._logger.info("Beginning move from %s to %s" % (self._videoDir, destination))
                        try:
                            origVolName, origVolType = getVolumeNameAndType(self._videoDir)
                            self._logger.info("Source drive = %s %s" % (origVolName, origVolType))
                            newVolName, newVolType = getVolumeNameAndType(location)
                            self._logger.info("Target drive = %s %s" % (newVolName, newVolType))
                        except Exception:
                            self._logger.error("Get volume information failed.", exc_info=True)
                        shutil.move(self._videoDir, destination)
                        self._logger.info("Finished move.")
                    else:
                        self._logger.info("Source video directory did not exist.")
                    success = True
                except Exception as e:
                    if WindowsError and (isinstance(e, WindowsError) and e.errno == errno.EACCES):
                        if os.path.isdir(destination):
                            # Windows error 32 = remove failed, file in use.
                            self._logger.warning("Access error, but we assume transfer completed.")
                            success = True
                        else:
                            # Windows error 5? = access is denied.
                            self._logger.error("Access error, couldn't move files.", exc_info=True)
                    elif (isinstance(e, OSError) and e.errno == errno.ENOTEMPTY):
                        success = True
                    else:
                        self._logger.error("Moving data failed.", exc_info=True)

                    if success:
                        # The move succeeded, but the fact that there was an exception signifies
                        # that the source could not be completely removed.
                        self._netMsgServerClient.addMessage(
                                        [MessageIds.msgIdDirectoryRemoveFailed,
                                         self._videoDir])
                        self._logger.warning("Not all data could be deleted "
                                             "from the previous video location.", exc_info=True)
            else:
                # Ensure the new directory can be created.
                try:
                    os.makedirs(destination)
                except Exception:
                    pass
                if os.path.isdir(destination):
                    success = True
                    if not keepExisting:
                        self._dataManager.reset()
                        self._clipManager.reset()

                        # Remove the existing data.
                        try:
                            if os.path.isdir(self._videoDir):
                                shutil.rmtree(self._videoDir)
                        except Exception:
                            self._logger.error("Removing data failed.",
                                               exc_info=True)
                        if os.path.isdir(self._videoDir):
                            self._logger.warning("Not all data could be deleted "
                                                 "from the previous video location.")
                            self._netMsgServerClient.addMessage(
                                        [MessageIds.msgIdDirectoryRemoveFailed,
                                         self._videoDir])
                else:
                    self._logger.warning("The new directory could not be created, "
                                         "aborting location change.")
                    self._netMsgServerClient.addMessage(
                        [MessageIds.msgIdDirectoryCreateFailed, destination])

            # If we succeeded commit the change to the prefs file.
            if success:
                self._videoDir = destination
                self._dataManager.setVideoStoragePath(destination)
                # _clipUploader is created lazily on first upload; it may be None.
                if self._clipUploader is not None:
                    self._clipUploader.updateVideoStoragePath(destination)

            self._logger.info("Move status: %s.  Restarting processes." % str(success))

            # Restart the disk cleaner.
            self._initDiskCleanup(self._maxStorage)

            # Restart the response runner.
            self._initResponseRunner()

            # Restart any cameras that should be running.
            for cameraName in self._cameraInfo:
                self._syncCameraStateWithSchedule(cameraName)
        finally:
            # Ensure that no matter what errors occur we always let the
            # front end know to stop blocking.
            self._netMsgServerClient.setVideoLocationChangeStatus(location,
                                                                  success)


    ###########################################################
    def _moveTmpFiles(self):
        """Move any pending tmp videos to their archive location."""
        now = time.time()

        kMinFreePercentageSys = 1          # Require at least 1% free drive space on system drive (start trim)
                                           # The idea is to start trimming pending moves, before camera processes deem
                                           # storage situation critical (which happens at 1GB)
        kMinFreeSpaceMB       = 2*1024     # require at least 2GB left, before we remove files
                                           # which previously failed to move to permanent storage location


        for targetPath, (loc, lastTime) in list(self._pendingFileMoves.items()):
            src = os.path.join(self._tmpDir, loc, os.path.basename(targetPath))
            dst = os.path.join(self._videoDir, targetPath)
            moved = False
            try:
                if os.path.isfile(src):
                    shutil.move(src, dst)
                    moved = True
                else:
                    self._logger.error("Pending move item " + ensureUnicode(src) + " does not exist")
            except Exception as e:
                self._logger.error("Failed to move file (" + ensureUnicode(src) + "->" + ensureUnicode(dst) + "): " + str(e))

            if moved:
                # If it was successfully relocated remove it from the dict.
                self._logger.info("Moved tmp file %s" % ensureUnicode(src))
                del self._pendingFileMoves[targetPath]
            else:
                diskCritical = not checkFreeSpace(self._tmpDir, kMinFreeSpaceMB, kMinFreePercentageSys, None)

                if now > lastTime:
                    reason = "move timeout expired"
                elif diskCritical:
                    reason = "insufficient local storage"
                else:
                    reason = ""

                if now > lastTime or diskCritical:
                    # If it hasn't been moved in the time we allocated, delete the
                    # file and remove the associated time period from the databases.
                    self._logger.info("Removing tmp file %s due to %s" % (ensureUnicode(src), reason))
                    del self._pendingFileMoves[targetPath]
                    try:
                        os.remove(src)
                    except Exception:
                        self._logger.info("Queuing file for deletion: %s" % ensureUnicode(src))
                        self._putMsgDC([MessageIds.msgIdDeleteFile, src])
                    start, stop = \
                        self._clipManager.getFileTimeInformation(targetPath.lower())
                    if start != -1:
                        # Remove these times from the object database.
                        self._dataManager.deleteCameraLocationDataBetween(
                                                                loc, start, stop)
                        # Remove this file from the clip database.
                        self._clipManager.removeClip(targetPath.lower())


    ###########################################################
    def _handleCorruptDatabase(self):
        """Another process hit a genuine SQLite corruption error.

        This is real evidence, so it goes down the same road as a failed
        integrity check: raise the flag, say so once, keep recording.

        It used to write err-corruptdb and then quit the whole back end.  That
        cost the user their recording the moment anything touched a bad page,
        and the file it left behind drove a SECOND, different dialog at the
        next launch offering Recover or Reset -- where Reset silently deletes
        every clip.  err-corruptdb is now only ever a repair REQUEST that the
        user made deliberately; nothing writes it to report a problem.
        """
        self._logger.error("Corrupt database reported by another process")
        self._raiseCorruptionFlag(
            "a component reported a corrupt database while reading or writing",
            source='sqlite corruption error')


    ###########################################################
    def _removeEmptyDirs(self, dirName):
        """Remove any empty directories we've left around.

        NOTE: It would be nice if this could happen periodically, but I'm a
              bit of afraid of conflicts with a camera process about to move a
              file and the destination directory is deleted from under it...
              At least folders don't really take up any disk space so deleting
              them only when the back end starts seems ok.

        @param dirName  The directory in which to remove empty directories.
        """
        _kMaxCleanupTime = 30 # Front-end will timeout after 90s ... lets not run this cleanup for longer than 30s
        _kWarnCleanupTime = 10

        status = "completed"
        start = time.time()
        for path, dirs, files in os.walk(dirName, False):
            # Note: we skip normalizePath() here since we do no
            # string comparisons.
            if not dirs and not files and path != dirName:
                try:
                    self._logger.info("Removing folder %s" % ensureUnicode(path))
                    os.rmdir(path)
                except Exception:
                    self._logger.warning("Couldn't remove %s" % ensureUnicode(path))
            diff = time.time() - start
            if diff > _kMaxCleanupTime:
                status = "aborted"
                break
        diff = time.time() - start
        if diff > _kWarnCleanupTime:
            self._logger.info("Finished empty folder cleanup in %d seconds, %s" % (int(diff), status))



##############################################################################
def _forcedQuitCallback():
    """A callback to notify the current app if a force quit ever happens.

    This is done here so that we don't keep registering if we restart; also
    doing things this way keeps anyone from holding a reference to the app.

    NOTE: the service nowadays takes care about detection a shutdown taking care
          about ending the callback, so it will only work during development ...
    """
    if _app is not None and not serviceAvailable():
        _app.forceQuit()
__callbackFunc = registerForForcedQuitEvents(_forcedQuitCallback) #PYCHECKER OK: (__callbackFunc) not used

##############################################################################
def main(userDataDir, *otherArgs):
    global _app

    userDataDir = os.path.expanduser(userDataDir.decode('utf-8') if isinstance(userDataDir, bytes) else userDataDir)

    # Publish the data directory to this process and every camera / detection
    # child it spawns. Without it, modules that resolve their own paths (face
    # enrollment, AI config, iHost config, TTS) would fall back to the HOME of
    # whoever started us -- which, when that is the service, is not the user's
    # profile and would silently fork the app's state in two.
    exportDataDir(userDataDir)

    # Keep Modern Standby from pausing recording and detection for as long as
    # this process runs -- across the restart loop below, not per BackEndApp.
    # Held on THIS thread because the SetThreadExecutionState fallback is
    # per-thread.  See appCommon.KeepAwake.
    keepAwake = None
    try:
        if BackEndPrefs(os.path.join(userDataDir, kPrefsFile)).getPref(
                kKeepSystemAwake):
            keepAwake = KeepAwake.acquire()
            getLogger(_kLogName, None, _kLogSize).info(keepAwake.describe())
        else:
            getLogger(_kLogName, None, _kLogSize).info(
                "keep-awake: disabled by preference; Modern Standby can "
                "pause recording and detection when the display turns off")
    except Exception:
        getLogger(_kLogName, None, _kLogSize).warning(
            "keep-awake: could not be set up", exc_info=True)

    wantQuit = False

    try:
        # We want to continually launch the app unless it explicitly terminated.
        while not wantQuit:
            _app = BackEndApp(userDataDir)
            try:
                _app.run()
            except BaseException:
                # Catch ALL exceptions, not just subclasses of Exception.  That will
                # catch things like KeyboardInterrupt.
                #
                # ...if we don't do this and we get a keyboard interrupt, badness
                # ensues.
                import traceback as _tb_main
                getDebugTracer(_kTraceName, userDataDir)(
                    "BACKEND CRASH:\n" + _tb_main.format_exc())
                getLogger(_kLogName, None, _kLogSize).error("Unhandled exception", exc_info=True)

            # Retrieve whether or not the app meant to quit.
            wantQuit = _app.wantQuit

            try:
                # Ensure all resources used by the app are freed and destroyed
                # immediately rather than waiting for the destructor to be called.
                _app.cleanup()
            except BaseException:
                # We always want the back end to restart, so ignore any exceptions.
                getLogger(_kLogName, None, _kLogSize).error("Unhandled exception in cleanup", exc_info=True)

            _app = None
    finally:
        KeepAwake.release(keepAwake)

    # make sure logging and all of its resources get closed properly
    logging.shutdown()
