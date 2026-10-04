#! /usr/local/bin/python

#*****************************************************************************
#
# NetworkMessageServer.py
#     XML RPC gateway to Sighthound Video API. Used mostly by web server/remote client endpoint
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
Contains the NetworkMessageServer class.
"""

# Python imports...
import pickle
import datetime
import json
import logging
import operator
import os
import time
from queue import Empty as QueueEmpty
from socketserver import TCPServer, BaseRequestHandler
import urllib.parse
import xmlrpc.client
import sys, threading, secrets, string, uuid, traceback
import subprocess

from xmlrpc.server import SimpleXMLRPCRequestHandler

# Common 3rd-party imports...

# Toolbox imports...
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.networking.HttpClient import HttpClient
from vitaToolbox.path.PathUtils import normalizePath
from vitaToolbox.dictUtils.MemStore import MemStore
from vitaToolbox.windows.winUtils import registerForForcedQuitEvents
from vitaToolbox.strUtils.EnsureUnicode import ensureUtf8, simplifyString
from vitaToolbox.sysUtils.FileUtils import writeObjectToFile, writeStringToFile
from vitaToolbox.sysUtils.TimeUtils import getTimeAsMs, formatTime
from vitaToolbox.process.ProcessUtils import getMemoryStats, getCpuSeconds
from vitaToolbox.process.ProcessUtils import getSystemMemoryStats

# Local imports...
from appCommon.CommonStrings import kImportSuffix
from appCommon.CommonStrings import kSoundResponse
from appCommon.CommonStrings import kPortFileName
from appCommon.CommonStrings import kRuleDir, kRuleExt, kQueryExt, kBackupExt
from appCommon.CommonStrings import kPrefsFile, kCamDbFile
from appCommon.CommonStrings import kAnyCameraStr
from appCommon.CommonStrings import kCameraOn, kCameraOff
from appCommon.CommonStrings import kCameraConnecting, kCameraFailed
from appCommon.CommonStrings import kMagicCommKey
from appCommon.CommonStrings import kBuiltInRules
from appCommon.CommonStrings import kInactiveSuffix
from appCommon.CommonStrings import kVideoFolder
from appCommon.CommonStrings import kRemoteFolder
from appCommon.CommonStrings import kGatewayHost
from appCommon.CommonStrings import kGatewayPath
from appCommon.CommonStrings import kGatewayTimeoutSecs
from appCommon.CommonStrings import kMemStoreLicenseData
from appCommon.CommonStrings import kMemStoreRulesLock
from appCommon.CommonStrings import kVersionString
from appCommon.CommonStrings import kApiVersion
from appCommon.CommonStrings import kReservedMarkerSvcArg
from appCommon.CommonStrings import kExecAlertThreshold
from appCommon.LicenseUtils import kCamerasField
from appCommon.LicenseUtils import kEditionField
from appCommon.LicenseUtils import hasPaidEdition
from appCommon.SearchUtils import getSearchResults
from appCommon.SearchUtils import getSearchResultsBetweenTimes
from appCommon.SearchUtils import SearchConfig
from appCommon.XmlRpcClientIdWrappers import XMLRPCServerWithClientId
from appCommon.XmlRpcClientIdWrappers import CrossDomainXMLRPCRequestHandler

from .BackEndPrefs import BackEndPrefs, kClipQualityProfile, kClipResolution, kClipMergeThreshold, kHardwareAccelerationDevice
from .CameraManager import CameraManager
from .ClipManager import ClipManager
from .DataManager import DataManager
from . import MessageIds
from .RealTimeRule import RealTimeRule, scheduleIconState
from .ResponseDbManager import ResponseDbManager
from .SavedQueryDataModel import convertOld2NewSavedQueryDataModel
from .DebugLogManager import DebugLogManager
# Path helper only -- DetectionServiceClient is deliberately import-light and
# pulls in no ML packages.
from .DetectionServiceClient import modelsFilePath
from .triggers.TargetTrigger import getQueryForDefaultRule
from .WebServer import make_auth, user_from_auth, REALM
from vitaToolbox.threading.ThreadPoolMixIn import ThreadPoolMixIn
from vitaToolbox.threading.PriorityLock import PriorityLock


def OB_ASID(a): return a


# Constants...
_kXmlrpcPortList = [7040, 7041, 7042, 7043, 7044, 7045, 7046, 7047, 7048, 7049,
                    7847, 7848, 7849, 7850, 7851, 7852, 7853, 7854, 7855, 7856,
                    9500, 9501, 9502, 9503, 9504, 9505, 9506, 9507, 9508, 9509]

# The name of our log...
_kLogName = "NetworkMessageServer.log"

# If we don't hear from the backend for 10 mins we assume it died and we'll exit.
_kBackEndPingTimeout = 600

# The switch for HTTP digest authentication. None to use basic authentication.
# Notice that this does not change the webserver, but just the way how the
# auth information is created (basic or digest). The web server itself can
# detect which is which and then configure the method which fits. A side effect
# could be that if e.g. you switch to digest an old basic auth expression stored
# in the preferences would still be active until the credentials get changed.
_kUseDigestAuth = False


#_kRemoteNotAuthorized = "You must have the Basic or Professional edition of " \
#                        "%s to use this product." % kAppName
_kRemoteGenericError = "The requested operation could not be performed."
_kRemoteExceptionError = "An exception occurred during the requested operation."
_kCouldNotLoadClip = "The requested clip could not be loaded."

# MIME types for media requests
_kMimeTypeVideoH264 = "video/h264"
_kMimeTypeJpeg = "image/jpeg"

# URL path format for the streaming and jpeg
_kLivePathFormat = "/live/%s"

_kExecAlertThreshold = float(kExecAlertThreshold)

# Size of the thread pool for concurrent TCP connection to an XML/RPC server.
# Notice that we run a hybrid server with two actual instance, so double this
# value for the actual maximum number of parallel connections possible.
_kThreadPoolSize = 23

# --- Dispatch-lock watchdog -------------------------------------------------
# Every method not in _kThreadSafeFunctions is serialized behind one
# process-wide PriorityLock (see dispatchWithPriority).  If a handler wedges
# while holding it, every other camera-status / live-view RPC queues behind
# it and the back end's main thread blocks for its full 300s client timeout
# -- which is how the app died on 2026-09-01.  Nothing in this log said who
# was holding it: the per-dispatch trace is .debug and never written, and the
# existing "took N sec" warning only fires once a call COMPLETES, so a call
# that never completes is invisible.  The watchdog below samples from the
# outside instead, so a stuck holder reports itself while it is still stuck.
#
# How often the watchdog samples the lock.
_kLockWatchdogIntervalSecs = 5
# Warn once a single holder has held the lock this long.
_kLockHeldWarnSecs = 10
# While it is still held, repeat the warning this often so the log shows the
# hold growing rather than one orphaned line.
_kLockHeldRepeatSecs = 30
# Dump the holding thread's stack once it has held the lock this long.  This
# is the line that says WHAT it is stuck on, not merely which RPC it serves.
_kLockStackDumpSecs = 30
# Warn when merely waiting for the lock took this long.
_kLockWaitWarnSecs = 10

# How far to search before and after when retrieving a notification clip.
_kNotifClipRewindMs = 2*60*1000
_kNotifClipExtendMs = 15*60*1000

# Functions for which calling and timing information will not be logged.
_kUnloggedFunctions = ["getMessage", "updateCameraProgress"]

# Functions which can be executed on demand and do not need to be serialized.
_kThreadSafeFunctions = [
    "getMessage", "addMessage", "backEndPing", "quit", "getWebPort",
    "getWebUser", "isPortOpenerEnabled", "getMaxStorageSize", "shutdown",
    "getCacheDuration", "getStorageLocation", "getVideoLocation",
    "getEmailSettings", "getFtpSettings", "getArmSettings",
    "setLiveViewParams", "getCameraStatus", "deleteVideo", "getUpnpDictRevNum",
    "getOnvifDictRevNum",
    "activeCameraSearch", "setTestCameraFailed", "testCameraFailed",
    "sendCorruptDbMessage",
    # Face enrollment: the several-second harvest must not hold the dispatch
    # lock (would stall camera-status / live-view RPCs); it only touches the
    # baseline files + read-only clip DBs, so concurrent execution is safe.
    "getBaselinePeople", "enrollFaceFromClip", "enrollFaceFromObject",
    "harvestFaceFromClip", "harvestFaceFromObject", "commitFaceHarvest",
    "harvestFaceFromUserMedia",
    "rebuildKnownFaces", "deleteBaselineImages", "moveBaselineImages",
    "renameBaselinePerson", "deleteBaselinePerson",
    "memstorePut", "memstoreGet", "memstoreRemove",
    "userLogin", "refreshLicenseList", "acquireLicense", "getLicenseSettings",
    "getVersion", "getTimePreferences",
    "sendIftttMessage", "launchedByService", "remoteSubmitClipToSighthound"
]

_kRemoteWhitelist = [
    'getVersion',
    'getCameraStatusAndEnabled',
    'remoteGetRulesForCamera',
    'remoteGetRulesForCamera2',
    'remoteGetBuiltInRules',
    'remoteGetDetailedRulesForCamera',
    'remoteGetCameraUri',
    'remoteGetThumbnailUris',
    'remoteGetClipsBetweenTimes',
    'remoteGetClipsBetweenTimes2',
    'ping',
    'getRuleInfo',
    'enableCamera',
    'remoteGetLiveCameras',
    'remoteGetNotifications',
    'remoteGetNotificationClip',
    'remoteRegisterDevice',
    'remoteRegisterDevice2',
    'remoteGetClipUri',
    'remoteGetClipUriForDownload',
    'remoteGetLastNotificationUID',
    'remoteGetCameraNames',
    'remoteGetCameraDetailsAndRules',
    'remoteGetAllCamerasDetailsAndRules',
    'remoteEnableRule',
    'remoteGetClipsForRule',
    'remoteGetClipsForRule2',
    'remoteUnregisterDevice',
    'remoteUnregisterDevice2',
    'remoteGetClipInfo',
    'enableNotifications',
    'remoteSubmitClipToSighthound'
]

# The version we send to our SV notification server.  This is new at about
# Sighthound Video 5.5 and (more importantly) iOS and Android app versions
# newer than about July 2017.  Those implement a new notification receiver
# using Google Firebase instead of Pushwoosh.
_kNotifyVersion_2_NoPushwoosh = 2


###############################################################
def runNetworkMessageServer(msgQueue, localDataDir, camProcessQueue,
                            clipMgrPath, dataMgrPath, responseDbPath,
                            licenseData, hwDevices):
    """Create and start a NetworkMessageServer

    @param  msgQueue         A queue to add received commands to.
    @param  localDataDir     The directory in which to store application data.
    @param  camProcessQueue  A queue used to track processing progress.
    @param  clipMgrPath      A path to the clip manager database.
    @param  dataMgrPath      A path to the data manager database.
    @param  responseDbPath   A path to the response database.
    @param  licenseData      The (initial) license data to use.
    """
    server = NetworkMessageServer(msgQueue, localDataDir, camProcessQueue,
                                  clipMgrPath, dataMgrPath, responseDbPath,
                                  licenseData, hwDevices)
    server.run()
    logging.shutdown()


##############################################################################
class XMLPRCServer(ThreadPoolMixIn, XMLRPCServerWithClientId):
    """Multithreaded XMLRPC server with priority locking. """

    daemon_threads = True       # avoids stalling on shutdown
    allow_reuse_address = True  # make sure we can restart quickly

    ###########################################################
    def __init__(self, priority, logger, *args, **kwargs):
        """XMLPRCServer constructor.

        @param  priority  The priority this server's requests should be handled.
                          This does not make much sense on the first glance, but
                          is useful if multiple instance with different
                          priorities get created and then get all routed into
                          one singular dispatch point of a primary instance.
        @param  logger    The shared logger to use.
        """
        pfx = "xmlrpc-%d" % priority
        # Priority 0 is the SECONDARY, low-priority server -- the retired
        # remote-access surface.  It used to get the whole pool while the
        # primary (whose port goes in the port file, and which carries the
        # front end, BackEndApp, DiskCleaner, the cameras and the web server)
        # ran on two threads.  Since every non-thread-safe call serialises on
        # the shared dispatch lock either way, those two threads only ever
        # capped the ~48 _kThreadSafeFunctions that deliberately skip the lock
        # -- including the face-enrollment calls exempted precisely because a
        # harvest takes seconds.
        threadPoolSize = 2 if 0 == priority else _kThreadPoolSize
        self._logger = logger
        ThreadPoolMixIn.__init__(self, threadPoolSize, threadNamePrefix=pfx,
                                 logger=logger)
        self._priority = priority
        self._lock = PriorityLock()
        # Dispatch-lock watchdog state.  _lockHolderMutex guards only the
        # three fields below and is never held across anything that can
        # block, so it cannot itself become a second wedge point.
        self._lockHolderMutex = threading.Lock()
        self._lockHolder = None      # dict describing the current holder
        self._lastLockHolder = None  # the one before it, and how long it held
        self._lockWaiters = 0        # how many calls are queued behind it
        self._lockWatchdogThread = None
        XMLRPCServerWithClientId.__init__(self, *args, **kwargs)


    ###########################################################
    def _dispatch(self, method, params):
        """ Overridden dispatch method, so the priority can be passed to the
        actual dispatch point. Does not make much sense in this class alone,
        but is needed if another instance will be pointed to latter.

        @param method The method name.
        @param params Call parameters.
        """
        return self.dispatchWithPriority(method, params, self._priority)


    ###########################################################
    def dispatchWithPriority(self, method, params, priority):
        """Shareable dispatch method. Puts a lock around the original dispatch
        call and thus enforces serial execution, with priority ordering.

        @param  method    The method name.
        @param  params    Call parameters.
        @param  priority  The call's priority.
        """
        threadName = threading.current_thread().name
        needsLock = method not in _kThreadSafeFunctions
        logCall = method not in _kUnloggedFunctions
        timeStart = time.time()

        if needsLock:
            with self._lockHolderMutex:
                self._lockWaiters += 1
            try:
                lockToken = self._lock.acquire(priority)
            finally:
                with self._lockHolderMutex:
                    self._lockWaiters -= 1
            waited = time.time() - timeStart
            if waited > _kLockWaitWarnSecs:
                # Whoever we were behind has released by definition -- we now
                # hold the lock -- so name them from the departing-holder
                # record, not the current one.
                self._logger.warning(
                    "dispatch lock: %s (thread %s, priority %d) waited "
                    "%.1f sec to acquire; was behind %s" %
                    (method, threadName, priority, waited,
                     self._describePreviousLockHolder()))
            self._noteLockAcquired(method, threadName, priority)

        try:
            timeLock = time.time() - timeStart
            if logCall:
                self._logger.debug("(%s) p:%d, ltm:%d, %s%s" %
                    (threadName, priority, timeLock, method, str(params)))

            # Only allow white-listed functions to be called externally
            if priority == 0 and method not in _kRemoteWhitelist:
                self._logger.warning("external method call to '%s'" % method)
                return None

            result = XMLRPCServerWithClientId._dispatch(self, method, params)
            timeComplete = time.time() - timeStart
            if timeComplete > _kExecAlertThreshold:
                self._logger.warning("%s took %.2f sec (%slock in %.2f sec), priority=%d" % (method, timeComplete, "" if needsLock else "no ", timeLock, priority))
            return result
        except:
            self._logger.error("(%s) UNCAUGHT ERROR: %s" %
                               (threadName, sys.exc_info()[1]))
            self._logger.info(traceback.format_exc())
            raise
        finally:
            if needsLock:
                # Clear the holder record first: a brief window where the
                # lock is held but unattributed is fine (the watchdog treats
                # it as idle), whereas reporting a holder that has already
                # released would be a lie.
                self._noteLockReleased()
                self._lock.release(lockToken)
            methodTime = time.time() - timeStart
            if logCall:
                self._logger.debug("(%s) mtm:%d" % (threadName, methodTime))


    ###########################################################
    def _noteLockAcquired(self, method, threadName, priority):
        """Record who now holds the dispatch lock, for the watchdog.

        @param  method      The method name now holding the lock.
        @param  threadName  Name of the holding thread.
        @param  priority    The call's priority.
        """
        with self._lockHolderMutex:
            self._lockHolder = {
                'method': method,
                'thread': threadName,
                'threadId': threading.get_ident(),
                'priority': priority,
                'since': time.time(),
                'warnedAt': 0.0,
                'dumped': False,
            }


    ###########################################################
    def _noteLockReleased(self):
        """Clear the dispatch-lock holder record, remembering who it was.

        The departing holder is kept because by the time a queued call gets
        the lock the holder it was waiting on has necessarily released -- so
        "who was I behind?" can only be answered from this.
        """
        with self._lockHolderMutex:
            holder = self._lockHolder
            if holder is not None:
                self._lastLockHolder = {
                    'method': holder['method'],
                    'thread': holder['thread'],
                    'held': time.time() - holder['since'],
                }
            self._lockHolder = None


    ###########################################################
    def _describePreviousLockHolder(self):
        """Return a short string naming the last thread to hold the lock.

        @return  desc  Holder description, or "(unknown)" if there was none.
        """
        with self._lockHolderMutex:
            prev = dict(self._lastLockHolder) if self._lastLockHolder else None
        if not prev:
            return "(unknown)"
        return "%s (thread %s, held %.1f sec)" % (
            prev['method'], prev['thread'], prev['held'])


    ###########################################################
    def _describeLockHolder(self):
        """Return a short string naming the current dispatch-lock holder.

        @return  desc  Holder description, or "(none)" if the lock is free.
        """
        with self._lockHolderMutex:
            holder = dict(self._lockHolder) if self._lockHolder else None
        if not holder:
            return "(none)"
        return "%s (thread %s, priority %d, held %.1f sec)" % (
            holder['method'], holder['thread'], holder['priority'],
            time.time() - holder['since'])


    ###########################################################
    def startLockWatchdog(self):
        """Start the dispatch-lock watchdog thread.

        Called once, on the server that actually owns the shared lock.  The
        thread is a daemon and swallows every exception: an observer that can
        take the process down is worse than no observer at all.
        """
        if self._lockWatchdogThread is not None:
            return
        self._lockWatchdogThread = threading.Thread(
            target=self._lockWatchdogLoop, name="nms-lock-watchdog")
        self._lockWatchdogThread.daemon = True
        self._lockWatchdogThread.start()
        self._logger.info(
            "dispatch-lock watchdog started (warn at %gs, stack dump at %gs)"
            % (_kLockHeldWarnSecs, _kLockStackDumpSecs))


    ###########################################################
    def _lockWatchdogLoop(self):
        """Sample the dispatch lock and report a holder that will not let go."""
        while True:
            try:
                time.sleep(_kLockWatchdogIntervalSecs)
                self._checkLockHolder()
            except Exception:
                try:
                    self._logger.error("lock watchdog error: %s" %
                                       sys.exc_info()[1])
                except Exception:
                    pass


    ###########################################################
    def _checkLockHolder(self):
        """One watchdog pass: warn, and eventually dump a stack, on a long hold.
        """
        now = time.time()
        with self._lockHolderMutex:
            holder = self._lockHolder
            if holder is None:
                return
            held = now - holder['since']
            if held < _kLockHeldWarnSecs:
                return
            # Warn on the first crossing, then at the slower repeat interval.
            due = (holder['warnedAt'] == 0.0 or
                   now - holder['warnedAt'] >= _kLockHeldRepeatSecs)
            if due:
                holder['warnedAt'] = now
            wantDump = (not holder['dumped'] and held >= _kLockStackDumpSecs)
            if wantDump:
                holder['dumped'] = True
            method = holder['method']
            threadName = holder['thread']
            threadId = holder['threadId']
            priority = holder['priority']
            waiters = self._lockWaiters

        if due:
            self._logger.warning(
                "dispatch lock HELD %.1f sec by %s (thread %s, priority %d); "
                "%d call(s) queued behind it" %
                (held, method, threadName, priority, waiters))
        if wantDump:
            self._logger.warning(
                "dispatch lock: stack of holding thread %s, in %s:\n%s" %
                (threadName, method, self._formatThreadStack(threadId)))


    ###########################################################
    def _formatThreadStack(self, threadId):
        """Return a formatted stack for one thread id.

        This is what identifies WHAT the holder is blocked on -- a database
        call, a socket read, a subprocess -- rather than only which RPC it
        was serving.

        @param  threadId  Thread identity, as from threading.get_ident().
        @return text      Formatted stack, or a note explaining its absence.
        """
        try:
            frame = sys._current_frames().get(threadId)
            if frame is None:
                return "  (thread not found -- it may have just finished)"
            return "".join(traceback.format_stack(frame))
        except Exception:
            return "  (stack unavailable: %s)" % sys.exc_info()[1]


##############################################################################
class XMLPRCServerThread(threading.Thread):
    """Thread to drive an XMLRPC server. We use it to run the secondary, low
    priority server, while the main one's executed by the process itself. """
    def __init__(self, server, logger):
        """XMLPRCServerThread constructor.

        @param server The server to run the accept loop for.
        @param logger Shared logger instance.
        """
        threading.Thread.__init__(self)
        self._server = server
        self._logger = logger
        self.running = True
    def run(self):
        """The thread run (server accept loop)."""
        try:
            while self.running:
                self._server.handle_request()
        except:
            self._logger.error("server loop error (%s)" % sys.exc_info()[1])


##############################################################################
class NetworkMessageServer(object):
    """A class to receive messages from external processes."""
    ###########################################################
    def __init__(self, msgQueue, localDataDir, camProcessQueue,
                 clipMgrPath, dataMgrPath, responseDbPath, licenseData,
                 hardwareDevices):
        """Initialize NetworkMessageServer.

        @param  msgQueue         A queue to add received commands to.
        @param  localDataDir     The directory in which to store application
                                 data.
        @param  camProcessQueue  A queue used to track processing progress.
        @param  clipMgrPath      A path to the clip manager database.
        @param  dataMgrPath      A path to the data manager database.
        @param  responseDbPath   A path to the response database.
        @param  licenseData      The (initial) license data to use.
        @param  hardwareDevices  Available hardware devices
        """
        # Call the superclass constructor.
        super(NetworkMessageServer, self).__init__()

        # Save data dir and setup logging...  SHOULD BE FIRST!
        self._localDataDir = localDataDir
        self._logDir = os.path.join(self._localDataDir, "logs")
        self._logger = getLogger(_kLogName, self._logDir)
        self._logger.grabStdStreams()

        self._debugLogManager = DebugLogManager("NMS", localDataDir)

        assert type(localDataDir) == str

        self._foundServiceMarkerArg = kReservedMarkerSvcArg in sys.argv
        self._logger.info("service-launched: %s" % self._foundServiceMarkerArg)

        self._memstore = MemStore()
        self._memstore.put(kMemStoreRulesLock, False)
        self._memstore.put(kMemStoreLicenseData, licenseData)

        self._xmlrpcServer = None
        self._xmlrpcServerLowPrio = None
        self._xmlrpcServerLowPrioThread = None
        self._queue = msgQueue
        self._camMgr = None
        self._prefs = BackEndPrefs(os.path.join(localDataDir, kPrefsFile))
        self._ruleDir = os.path.join(localDataDir, kRuleDir)

        # Open databases to respond to queries from remote clients.
        self._clipMgr = ClipManager(self._logger)
        self._clipMgr.open(clipMgrPath)
        _videoLoc = self._getVideoLocation() or os.path.join(localDataDir, "videos")
        self._dataMgr = DataManager(self._logger, self._clipMgr,
                                    os.path.join(_videoLoc, kVideoFolder))
        self._dataMgr.open(dataMgrPath)
        self._responseDb = ResponseDbManager(self._logger)
        self._responseDb.open(responseDbPath)

        # A dictionary tracking the current status of configured cameras.
        # key = camera name
        # value in [kCameraOn, kCameraOff, kCameraConnecting, kCameraFailed]
        self._cameraStatus = {}
        # A dictionary tracking the last known WSGI server ports of configured
        # cameras.
        # key = camera name
        # value = port number
        self._cameraWsgiPort = {}
        self._testFailure = False

        # A dictionary of (lastSearchedMs, lastTaggedMs) for each camera.
        self._cameraUpdateTimes = {}
        self._camProcessQueue = camProcessQueue

        # --- System-health state (see _getSystemHealth) ---------------------
        # Other processes already compute these but only emit them as one-shot
        # events; they push the latest value here so the health view can poll.
        self._healthDisk = None         # dict from DiskCleaner
        self._healthClockOffset = None  # seconds, from DiskCleaner/ClockSync
        self._healthProcs = {}          # friendly name -> pid, from BackEndApp
        self._healthDatabase = {}       # corruption/registration, BackEndApp
        self._healthStartedMs = None    # back end start, from BackEndApp
        self._cameraReconnects = {}     # camera -> restart count
        self._cameraHealth = {}        # camera -> latest StreamReader snapshot
        # Loaded ML model names, read from the file the detection service
        # publishes.  Cached on mtime so a 2-second poll isn't a reparse.
        self._healthModels = None
        self._healthModelsMtime = None
        # pid -> (wallClock, cpuSeconds) from the previous poll, so successive
        # health requests can turn the CPU-time counter into a percentage.
        self._cpuSamples = {}
        # GPU sampling (nvidia-smi) runs in a lazy background thread so a
        # health poll NEVER waits on a subprocess; see _gpuSamplerLoop.
        self._gpuSample = None       # last parsed nvidia-smi dict
        self._gpuSampleTime = 0.0    # when it was taken
        self._gpuFails = 0           # consecutive failures -> disable
        self._gpuDisabled = False    # no NVIDIA GPU / nvidia-smi missing
        self._gpuThread = None       # started on first getSystemHealth
        self._lastHealthPoll = 0.0   # sampler idles when nobody polls

        # A pickled dictionary of UPNP devices discovered.
        #   Key: usn
        #   Value: a UpnpDevice (see vitaToolbox.networking.Upnp.py)
        self._pickledUpnpDeviceDict = pickle.dumps({})

        # The revision number of the dictionary; updates ever time it's changed.
        self._upnpDictRevNum = 0

        # A pickled dictionary of ONVIF devices discovered.
        #   Key: uuid
        #   Value: an OnvifDevice (see vitaToolbox.networking.Onvif.py)
        self._pickledOnvifDeviceDict = pickle.dumps({})

        # The revision number of the dictionary; updates ever time it's changed.
        self._onvifDictRevNum = 0

        # The current status of a packet capture stream in progess. It will be
        # a pickled empty dictionary when the packet capturing is inactive.
        # Once the backend is told to start capturing, this will be set to an
        # initialized dictionary containing the keys, "pcapEnabled" and
        # "pcapStatus", both set to None. During the life of the pcap process,
        # the values will change to reflect its progress or errors.
        self._pickledPacketCaptureInfo = pickle.dumps({})

        # A cache of USB webcams...
        self._localCameraNames = []

        # Queue of messages sent by the back end for the front end.
        self._frontEndMessageQueue = FrontEndMessageQueue()

        # Import-related messages
        self._importMessages = []

        # Finished, success for location change operations.
        self._locationChangeStatus = (True, False)

        # Face-harvest previews, keyed by token; see _faceHarvestStore.  The
        # harvest RPCs are thread safe (they run without the dispatch lock),
        # so the purge needs a lock of its own.
        self._faceHarvests = {}
        self._faceHarvestLock = threading.Lock()

        # Devices available for decoding
        self._hardwareDevices = hardwareDevices


    ###########################################################
    def __del__(self):
        """Free resources used by NetworkMessageServer"""
        # This isn't usually called, since (at the moment), BackEndApp usually
        # terminates us.  ...but putting the print here just in case we quit
        # for some other weird reason...
        self._logger.info("NetworkMessageServer exiting")


    ###########################################################
    def run(self): #PYCHECKER OK: OK to have too many lines here...
        """Open an xmlrpc server and begin listening for messages."""
        self.__callbackFunc = registerForForcedQuitEvents()

        # Find the port file destination
        try:
            os.makedirs(self._localDataDir)
        except Exception:
            pass

        # Find the first two open ports on our list.
        ports = []
        portFilePath = None
        for port in _kXmlrpcPortList:
            try:
                # Under windows (xp and vista) it is possible to create two
                # SimpleXMLRPCServer processes on the same port without error.
                # To check if another user is running VDV on the port we're
                # attempting we create a different server type.  If this works
                # we know our xmlrpc server will be the only one running.
                # NOTE: Thankfully SimpleXMLRPCServer will fail if anything
                #       else is running on the same port.  It only succeeds
                #       when the 'anything else' is another SimpleXMLRPCServer.
                tcpServer = TCPServer(('0.0.0.0', port), BaseRequestHandler)
                tcpServer.server_close()
                if 0 == len(ports):
                    requestHandlerClass = SimpleXMLRPCRequestHandler
                else:
                    requestHandlerClass = CrossDomainXMLRPCRequestHandler
                newXmlrpcServer = XMLPRCServer(1 - len(ports), self._logger,
                    ("0.0.0.0", port), logRequests=False, allow_none=True,
                    requestHandler=requestHandlerClass
                )

                # Uncomment this to log requests:
                # That was useful in bug #1337 and #2089
                #origDispatch = self._xmlrpcServer._dispatch
                #def newDispatch(method, params):
                #    print "Method '%s' called " % (method)
                #    return origDispatch(method, params)
                #self._xmlrpcServer._dispatch = newDispatch

                ports.append(port)
                if 2 == len(ports):
                    # HACK: bend the dispatch call from the secondary, low
                    #       priority server over to the actual one, where all
                    #       of the calls then meet, but get ordered for
                    #       execution depending on their priorities; the
                    #       secondary server stays an empty shell and does not
                    #       even get any methods associated with ...
                    newXmlrpcServer.dispatchWithPriority = \
                        self._xmlrpcServer.dispatchWithPriority
                    self._xmlrpcServerLowPrio = newXmlrpcServer

                    # we got both servers needed, done
                    break

                # we have the original, primary (high priority) server, which is
                # also related with the port file (read by other IPC parties)
                self._xmlrpcServer = newXmlrpcServer
                try:
                    portFilePath = os.path.join(self._localDataDir,
                                                kPortFileName)
                    portFile = open(portFilePath, 'wb')
                    pickle.dump(port, portFile)
                    portFile.close()
                except:
                    self._logger.critical("cannot write port file %s (%s)" % \
                        (str(portFilePath), sys.exc_info()[1]))
                    return

            except Exception:
                self._logger.error("server launch on port %d failed: (%s)" %
                                   (port, sys.exc_info()[1]))

        # Notify of success or failure
        opened = 2 == len(ports)
        self._queue.put([MessageIds.msgIdXMLRPCStarted, opened, ports])
        if not opened:
            # If we couldn't bind to any ports, exit
            self._logger.error("Couldn't bind to any ports")
            return

        self._logger.info("Running on port %i, pid: %d, low prio port: %i" %
                          (ports[0], os.getpid(), ports[1]))

        # Open the camera manager
        camDb = os.path.join(self._localDataDir, kCamDbFile)
        self._camMgr = CameraManager(self._logger, camDb)

        # Register functions
        rpcMethods = [
            # General
            (self._quit, "quit"),
            (self._getMagic, "ping"),
            (self._backEndPing, "backEndPing"),
            (self._getVersion, "getVersion"),
            (self._shutdown, "shutdown"),

            # Camera viewing
            (self._enableLiveView, "enableLiveView"),
            (self._flushVideo, "flushVideo"),
            (self._setLiveViewParams, "setLiveViewParams"),

            # Camera status
            (self._setCameraStatus, "setCameraStatus"),
            (self._getCameraStatus, "getCameraStatus"),
            (self._getCameraStatusAndReason, "getCameraStatusAndReason"),
            (self._getCameraStatusAndEnabled, "getCameraStatusAndEnabled"),
            (self._getCameraStatusEnabledAndReason, "getCameraStatusEnabledAndReason"),
            (self._setTestCameraFailed, "setTestCameraFailed"),
            (self._testCameraFailed, "testCameraFailed"),
            (self._setPacketCaptureInfo, "setPacketCaptureInfo"),
            (self._getPacketCaptureInfo, "getPacketCaptureInfo"),

            # System health
            (self._setHealthInfo, "setHealthInfo"),
            (self._noteCameraReconnect, "noteCameraReconnect"),
            (self._getSystemHealth, "getSystemHealth"),

            # Camera editing
            (self._addCamera, "addCamera"),
            (self._editCamera, "editCamera"),
            (self._editCameraFrameStorageSize, "editCameraFrameStorageSize"),
            (self._removeCamera, "removeCamera"),
            (self._enableCamera, "enableCamera"),
            (self._camMgr.getCameraLocations, "getCameraLocations"),
            (self._camMgr.getCameraSettings, "getCameraSettings"),
            (self._getLocalCameraNames, "getLocalCameraNames"),
            (self._setLocalCameraNames, "setLocalCameraNames"),
            (self._startCameraTest, "startCameraTest"),
            (self._stopCameraTest, "stopCameraTest"),
            (self._startPacketCapture, "startPacketCapture"),
            (self._stopPacketCapture, "stopPacketCapture"),
            (self._deleteVideo, "deleteVideo"),
            (self._setUpnpDevices, "setUpnpDevices"),
            (self._getUpnpDevices, "getUpnpDevices"),
            (self._getUpnpDictRevNum, "getUpnpDictRevNum"),
            (self._setOnvifDevices, "setOnvifDevices"),
            (self._getOnvifDevices, "getOnvifDevices"),
            (self._getOnvifDictRevNum, "getOnvifDictRevNum"),
            (self._setOnvifSettings, "setOnvifSettings"),
            (self._activeCameraSearch, "activeCameraSearch"),

            # Queries and Rules
            (self._editQueryRpc, "editQuery"),
            (self._getQueryRpc, "getQuery"),
            (self._sendIftttRulesAndCameras, "sendIftttRulesAndCameras"),
            (self._addRuleRpc, "addRule"),
            (self._deleteRule, "deleteRule"),
            (self._getRuleNames, "getRuleNames"),
            (self._getRuleRpc, "getRule"),
            (self._enableRule, "enableRule"),
            (self._setRuleSchedule, "setRuleSchedule"),
            (self._getRuleInfoForLocation, "getRuleInfoForLocation"),
            (self._getRuleInfo, "getRuleInfo"),
            (self._getScheduleIconStatesForLocation,
             "getScheduleIconStatesForLocation"),
            (self._getActiveResponseTypes, "getActiveResponseTypes"),

            # Storage settings
            (self._setMaxStorageSize, "setMaxStorageSize"),
            (self._getMaxStorageSize, "getMaxStorageSize"),
            (self._setSummarySettings, "setSummarySettings"),
            (self._getSummarySettings, "getSummarySettings"),
            (self._setStorageLocation, "setStorageLocation"),
            (self._getStorageLocation,"getStorageLocation"),
            (self._setVideoLocation, "setVideoLocation"),
            (self._setVideoLocationChangeStatus, "setVideoLocationChangeStatus"),
            (self._getVideoLocationChangeStatus, "getVideoLocationChangeStatus"),
            (self._getVideoLocation, "getVideoLocation"),
            (self._setCacheDuration, "setCacheDuration"),
            (self._getCacheDuration, "getCacheDuration"),
            (self._setRecordInMemory, "setRecordInMemory"),
            (self._getRecordInMemory, "getRecordInMemory"),

            # Email settings...
            (self._getEmailSettings, "getEmailSettings"),
            (self._setEmailSettings, "setEmailSettings"),

            # Sending clip-related settings...
            (self._getFtpSettings, "getFtpSettings"),
            (self._setFtpSettings, "setFtpSettings"),
            (self._getArmSettings, "getArmSettings"),
            (self._setArmSettings, "setArmSettings"),
            (self._getPendingClipInfo, "getPendingClipInfo"),
            (self._purgePendingClips, "purgePendingClips"),

            # Sending runtime settings
            (self._setDebugConfiguration, "setDebugConfiguration"),
            (self._setClipMergeThreshold, "setClipMergeThreshold"),
            (self._getClipMergeThreshold, "getClipMergeThreshold"),
            (self._getHardwareDevicesList, "getHardwareDevicesList"),
            (self._getHardwareDevice, "getHardwareDevice"),
            (self._setHardwareDevice, "setHardwareDevice"),




            # Back end functions
            (self._updateCameraProgress, "updateCameraProgress"),

            # Functions for remote applications.
            (self._remoteGetCameraNames, "remoteGetCameraNames"),
            (self._remoteGetAllCamerasDetailsAndRules, "remoteGetAllCamerasDetailsAndRules"),
            (self._remoteGetCameraDetailsAndRules, "remoteGetCameraDetailsAndRules"),
            (self._remoteGetRulesForCamera, "remoteGetRulesForCamera"),
            (self._remoteGetRulesForCamera2, "remoteGetRulesForCamera2"),
            (self._remoteGetBuiltInRules, "remoteGetBuiltInRules"),
            (self._remoteGetDetailedRulesForCamera, "remoteGetDetailedRulesForCamera"),
            (self._remoteEnableRule, "remoteEnableRule"),
            (self._remoteGetClipsForRule, "remoteGetClipsForRule"),
            (self._remoteGetClipsForRule2, "remoteGetClipsForRule2"),
            (self._remoteGetClipsForRuleBetweenTimes, "remoteGetClipsBetweenTimes"),
            (self._remoteGetClipsForRuleBetweenTimes2, "remoteGetClipsBetweenTimes2"),
            (self._remoteGetNotificationClip, "remoteGetNotificationClip"),
            (self._remoteGetThumbnailUris, "remoteGetThumbnailUris"),
            (self._remoteGetClipInfo, "remoteGetClipInfo"),
            (self._remoteGetLiveCameras, "remoteGetLiveCameras"),
            (self._remoteGetClipUri, "remoteGetClipUri"),
            (self._remoteGetClipUriForDownload, "remoteGetClipUriForDownload"),
            (self._remoteGetCameraUri, "remoteGetCameraUri"),
            (self._remoteRegisterDevice, "remoteRegisterDevice"),
            (self._remoteRegisterDevice2, "remoteRegisterDevice2"),
            (self._remoteUnregisterDevice, "remoteUnregisterDevice"),
            (self._remoteUnregisterDevice2, "remoteUnregisterDevice2"),
            (self._remoteGetNotifications, "remoteGetNotifications"),
            (self._remoteGetLastNotificationUID, "remoteGetLastNotificationUID"),

            # Notifications
            (self._enableNotifications, "enableNotifications"),
            (self._sendIftttMessage, "sendIftttMessage"),

            # Back end to front end communication
            (self._addMessage, "addMessage"),
            (self._getMessage, "getMessage"),

            # Web server settings
            (self._getWebPort, "getWebPort"),
            (self._setWebPort, "setWebPort"),
            (self._getVideoSetting, "getVideoSetting"),
            (self._setVideoSetting, "setVideoSetting"),
            (self._getTimestampEnabledForClips, "getTimestampEnabledForClips"),
            (self._setTimestampEnabledForClips, "setTimestampEnabledForClips"),
            (self._getBoundingBoxesEnabledForClips, "getBoundingBoxesEnabledForClips"),
            (self._setBoundingBoxesEnabledForClips, "setBoundingBoxesEnabledForClips"),
            (self._getWebUser, "getWebUser"),
            (self._setWebAuth, "setWebAuth"),
            (self._enablePortOpener, "enablePortOpener"),
            (self._isPortOpenerEnabled, "isPortOpenerEnabled"),

            # Face enrollment from footage (desktop Search window)
            (self._getBaselinePeople, "getBaselinePeople"),
            (self._enrollFaceFromClip, "enrollFaceFromClip"),
            (self._enrollFaceFromObject, "enrollFaceFromObject"),
            (self._harvestFaceFromClip, "harvestFaceFromClip"),
            (self._harvestFaceFromObject, "harvestFaceFromObject"),
            (self._commitFaceHarvest, "commitFaceHarvest"),
            (self._harvestFaceFromUserMedia, "harvestFaceFromUserMedia"),

            # Face-baseline management (Options -> AI Detection)
            (self._rebuildKnownFaces, "rebuildKnownFaces"),
            (self._deleteBaselineImages, "deleteBaselineImages"),
            (self._moveBaselineImages, "moveBaselineImages"),
            (self._renameBaselinePerson, "renameBaselinePerson"),
            (self._deleteBaselinePerson, "deleteBaselinePerson"),

            # Memstore access
            (self._memstorePut, "memstorePut"),
            (self._memstoreGet, "memstoreGet"),
            (self._memstoreRemove, "memstoreRemove"),

            # License and account functions
            (self._userLogin, "userLogin"),
            (self._userLogout, "userLogout"),
            (self._refreshLicenseList, "refreshLicenseList"),
            (self._acquireLicense, "acquireLicense"),
            (self._unlinkLicense, "unlinkLicense"),
            (self._getLicenseSettings, "getLicenseSettings"),
            (self._setLicenseSettings, "setLicenseSettings"),

            # External services integration
            (self._remoteSubmitClipToSighthound, "remoteSubmitClipToSighthound"),

            # Error cases...
            (self._sendCorruptDbMessage, "sendCorruptDbMessage"),

            # Time preferences
            (self._getTimePreferences, "getTimePreferences"),

            # Service things
            (self._launchedByService, "launchedByService"),
        ] # rpcMethods

        for f in rpcMethods:
            self._xmlrpcServer.register_function(f[0], f[1])

        self._lastBackEndPing = time.time()


        # allow system.listmethods to work
        #self._xmlrpcServer.register_introspection_functions
        # Commented out above as it has been a noop [no () at EOL] and I'm
        # not sure it's something we actually want to drop in right now.

        # Watch the shared dispatch lock.  Both servers funnel into the
        # primary server's dispatchWithPriority (see the HACK above), so the
        # primary owns the only lock that matters and one watchdog covers
        # both.
        self._xmlrpcServer.startLockWatchdog()

        # Make the low priority server accepting requests
        self._xmlrpcServerLowPrioThread = XMLPRCServerThread(
            self._xmlrpcServerLowPrio, self._logger)
        self._xmlrpcServerLowPrioThread.start()

        # Begin listening for requests
        self._running = True
        while self._running:
            self._xmlrpcServer.handle_request()
            self._expireMemstoreItems()

        # Shut down the XML/RPC servers.
        self._logger.info("Shutting down...")
        self._xmlrpcServer.shutdown(True)
        self._xmlrpcServerLowPrio.shutdown(True)
        # shutdown() only drains the thread pool; the listening sockets stay
        # bound until they are closed explicitly.
        for server in (self._xmlrpcServer, self._xmlrpcServerLowPrio):
            try:
                server.server_close()
            except Exception:
                self._logger.warning("server_close failed (%s)" %
                                     sys.exc_info()[1])
        try:
            os.remove(portFilePath)
        except:
            pass
        self._logger.info("Shutdown done.")


    ###########################################################
    def _expireMemstoreItems(self):
        """Expires memstore items.
        """
        msexpired = self._memstore.expire()
        if msexpired:
            self._logger.info("%d memstore item(s) expired" % msexpired)


    ###########################################################
    def _quit(self):
        """Signal the application to quit. It's also a hint for us to get out.

        NOTE: This function is thread safe. Ensure any changes preserve that.
        """
        self._queue.put([MessageIds.msgIdQuit])


    ###########################################################
    def _shutdown(self):
        """Shuts down the server. Called by the back-end.

        NOTE: This function is thread safe. Ensure any changes preserve that.
        """
        self._running = False
        self._xmlrpcServerLowPrioThread.running = False
        self._logger.info("Shutdown request, server loops about to exit...")


    ###########################################################
    def _getVersion(self):
        """ Returns the version number of the APP and the application/release.

        @return [API version, app version]
        """
        return [kApiVersion, kVersionString]


    ###########################################################
    def _getMagic(self):
        """Return the magic word.

        @return magic  The magic word.
        """
        self._logger.info("Pinged")

        if time.time() < self._lastBackEndPing+_kBackEndPingTimeout:
            return kMagicCommKey
        self._running = False
        self._xmlrpcServerLowPrioThread.running = False
        self._logger.warning("Back end timed out, exiting")
        return 'dead'


    ###########################################################
    def _backEndPing(self):
        """Handle a notification that the back end is still running.

        NOTE: This function is thread safe. Ensure any changes preserve that.
        """
        self._lastBackEndPing = time.time()


    ###########################################################
    def _maxCameras(self):
        """Convenience to get the maximum number of cameras from the license.

        @return Maximum number of cameras. 1 if license data is n/a.
        """
        result = self._memstoreGet(kMemStoreLicenseData, 0)
        if result is None:
            self._logger.critical("no license data!?")
            return 1
        return int(result[0].get(kCamerasField, 1))


    ###########################################################
    def _addCamera(self, camLocation, camType, camUri, extra=None):
        """Add a new camera

        @param  camLocation  The location of the camera
        @param  camType      The camera's type
        @param  camUri       The uri used to access the camera
        @param  extra        An optional extra dictionary of settings.
        """
        extra = dict(extra or {})

        if self._areRulesLocked():
            self._logger.error("Couldn't add, rules locked")
            return

        # UI will block individuals from trying to add extra cameras. Silently
        # fail here if someone tries to add one via API.
        maxCams = self._maxCameras()

        configuredCams = len(self._camMgr.getCameraLocations())
        if maxCams != -1 and configuredCams >= maxCams:
            return

        self._camMgr.addCamera(camLocation, camType, camUri, extra)
        self._queue.put([MessageIds.msgIdCameraAdded, camLocation, camUri,
                         extra])
        self._loadExistingRulesForLocation(camLocation)
        self._setCameraStatus(camLocation, kCameraConnecting)


    ###########################################################
    def _loadExistingRulesForLocation(self, camLocation):
        """Load any rules for a location that already exist.

        @param  camLocation  The location of the camera.
        """
        # Check to see if there were any existing rules for this location.
        existingRules = self._getRuleInfoForLocation(camLocation)

        for ruleName, queryName, _, _, _ in existingRules:
            try:
                pickledRule = self._getRule(ruleName)
                pickledQuery = self._getQuery(queryName)
                query = pickle.loads(pickledQuery)
                if query.getVideoSource().getLocationName() != camLocation:
                    # Update as needed to reflect the new camera name caps.
                    query.getVideoSource().setLocationName(camLocation)
                    queryFilename = os.path.join(self._ruleDir,
                                                  queryName+kQueryExt)
                    if not writeObjectToFile(queryFilename, query, self._logger):
                        self._logger.error("Failed to write query to " + ensureUtf8(queryFilename))
                    # Re-pickle, so the message below carries the new location
                    # name rather than the stale one we read from disk.
                    pickledQuery = pickle.dumps(query)

                self._queue.put([MessageIds.msgIdRuleAdded, ruleName,
                                 pickledRule, pickledQuery])
            except Exception:
                self._logger.warning("Load rules exception", exc_info=True)


    ###########################################################
    def _editCamera(self, origLocation, camLocation, camType, camUri,
                    changeTime=-1, extra=None):
        """Edit a camera.

        @param  origLocation  The original location of the camera.
        @param  camLocation   The current of the camera.
        @param  camType       The camera's type.
        @param  camUri        The uri used to access the camera.
        @param  changeTime    The time the camera changed, in seconds. If this
                              is -1 the camera will be treated as a new location
                              and existing rules will be left at the previous
                              location.
        @param  extra         An optional extra dictionary of settings.
        """
        extra = dict(extra or {})

        _, _, enabled, _ = self._camMgr.getCameraSettings(origLocation)

        self._camMgr.addCamera(camLocation, camType, camUri, extra)
        self._camMgr.enableCamera(camLocation, enabled)
        self._queue.put([MessageIds.msgIdCameraEdited, origLocation,
                         camLocation, camUri, extra, changeTime])
        if origLocation != camLocation:
            self._camMgr.removeCamera(origLocation)
            self._forgetCamera(origLocation)

            # Update ARM settings to account for new name.
            armSettings = self._getArmSettings()
            camerasNotToArm = armSettings.setdefault('camerasNotToArm', [])
            if origLocation in camerasNotToArm:
                camerasNotToArm.remove(origLocation)
                camerasNotToArm.append(camLocation)
                self._setArmSettings(armSettings)

            # If we renamed it to a location that had existing rules we
            # need to load those.
            self._loadExistingRulesForLocation(camLocation)

            # The camera will be temporarily off.  If the camera should be
            # turned on again, this will switch to the connecting screen in
            # BackEndApp _openCamera.
            self._setCameraStatus(camLocation, kCameraOff)

        # If the location changed we need to update queries and rules, as well
        # as mark old videos with the new name.  Otherwise we're done already.
        if origLocation == camLocation or changeTime != -1:
            return

        for name, _, _, enabled, _ in \
                                   self._getRuleInfoForLocation(origLocation):
            try:
                query = pickle.loads(self._getQuery(name))
                query.getVideoSource().setLocationName(camLocation)
                rule = pickle.loads(self._getRule(name))
                self._addRule(pickle.dumps(query), enabled, name, False)
                self._setRuleSchedule(query.getName(), rule.getSchedule())
            except Exception:
                self._logger.warning("Edit camera exception", exc_info=True)

    ###########################################################
    def _editCameraFrameStorageSize(self, camLocation, frameSize):
        """Edit a camera initial frame storage size.

        @param  camLocation   The location of the camera.
        @param  frameSize     frameSize
        """

        # we only need to update the config -- BackEnd sent us this message,
        # so it doesn't need to be updated
        self._camMgr.editCameraFrameStorageSize(camLocation, frameSize)

    ###########################################################
    def _forgetCamera(self, camName):
        """Drop every per-camera side-table entry for camName.

        Called on removal, and on rename for the OLD name.  Without it a
        camera that later reuses a name inherits its predecessor's reconnect
        count and health snapshot -- which is exactly the wrong thing to show
        an operator who is looking at the health view BECAUSE a camera is
        unstable.  The entries also just accumulate for the life of the
        process.

        @param  camName  The camera location to forget.
        """
        self._cameraStatus.pop(camName, None)
        self._cameraWsgiPort.pop(camName, None)
        self._cameraReconnects.pop(camName, None)
        self._cameraHealth.pop(camName, None)
        self._cameraUpdateTimes.pop(camName, None)


    ###########################################################
    def _removeCamera(self, camName, removeData=False):
        """Remove a camera.

        @param  camName     The name of the camera to remove.
        @param  removeData  If true all queries and saved video associated with
                            this camera will be deleted.
        """
        self._camMgr.removeCamera(camName, False)
        self._queue.put([MessageIds.msgIdCameraDeleted, camName, removeData])
        self._forgetCamera(camName)

        # Unfreeze 1+ cameras, if there are any actually
        maxCameras = self._maxCameras()
        unfrozen = self._camMgr.freezeCameras(maxCameras)[1]
        for camLoc in unfrozen:
            self._logger.info("unfreezing '%s', triggered by removal" % camLoc)
            _, uri, _, extra = self._camMgr.getCameraSettings(camLoc)
            self._queue.put([MessageIds.msgIdCameraEdited,
                             camLoc, camLoc, uri, extra, -1])

        # Remove or disable rules and queries for the removed location
        for name, _, _, _, _ in self._getRuleInfoForLocation(camName):
            if removeData:
                self._deleteRule(name, True)
            else:
                self._enableRule(name, False)


    ###########################################################
    def _enableCamera(self, camName, enable=True):
        """Enable or disable a camera.

        @param  camName  The name of the camera to edit.
        @param  enable   True if the camera is enabled.
        """
        self._camMgr.enableCamera(camName, enable)

        if enable:
            self._queue.put([MessageIds.msgIdCameraEnabled, camName])
            self._setCameraStatus(camName, kCameraConnecting)
        else:
            self._queue.put([MessageIds.msgIdCameraDisabled, camName])
            self._setCameraStatus(camName, kCameraOff)


    ###########################################################
    def _editQueryRpc(self, origName, pickledQueryBin, postMessage=True):
        """Edit a query.  Wrapper for RPC calls.

        @param  origName         The original query name.
        @param  pickledQueryBin  xmlrpclib.Binary-wrapped
                                 pickled SavedQueryDataModel
        @param  postMessage  True if the back end should be notified.
        """
        self._editQuery(origName, pickledQueryBin.data, postMessage)


    ###########################################################
    def _editQuery(self, origName, pickledQuery, postMessage=True):
        """Edit a query.

        @param  origName      The original query name.
        @param  pickledQuery  A pickled SavedQueryDataModel.
        @param  postMessage  True if the back end should be notified.
        """
        if self._areRulesLocked():
            return
        try:
            rule = pickle.loads(self._getRule(origName))
            query = pickle.loads(pickledQuery)
            if not postMessage:
                self._logger.info(
                    "Deleting rule %s without notifying the backend" % origName
                )
            self._addRule(pickledQuery, rule.isEnabled(), origName, postMessage)
            self._setRuleSchedule(query.getName(), rule.getSchedule())
        except Exception:
            self._logger.warning("Edit query exception", exc_info=True)


    ###########################################################
    def _getQueryRpc(self, queryName):
        """Retrieve a saved query. Wrapper for RPC calls.

        @param  queryName        The name of the query to retrieve.
        @return pickledQueryBin  xmlrpclib.Binary-wrapped
                                 pickled SavedQueryDataModel or None
        """
        return xmlrpc.client.Binary(self._getQuery(queryName))


    ###########################################################
    def _getQuery(self, queryName, unpickled=False):
        """Retrieve a saved query.

        @param  queryName     The name of the query to retrieve.
        @param  unpickled     True if the requested query should be returned
                              unpickled. By default, this method returns a
                              pickled query.
        @return pickledQuery  A pickled SavedQueryDataModel or None. Can be
                              unpickled if 'unpickled' is set to True.
        """
        filePath = os.path.join(self._ruleDir, queryName+kQueryExt)

        # Ensure the query file exists
        if not os.path.isfile(filePath):
            self._logger.error("Query %s doesn't exist" % queryName)
            return None

        # Read the query from disk
        queryFile = open(filePath, 'rb')
        pickledQuery = queryFile.read()
        queryFile.close()

        # Unpickle so we can upgrade and/or verify it.
        try:
            queryModel = pickle.loads(pickledQuery)
        except Exception:
            self._logger.error(
                "The query " + str(queryName) + " could not be unpickled for validation",
                exc_info=True,
            )
            return None

        # Ensure query has a valid coordinate space, which can be missing if
        # we are loading a query from an older version of the app...
        convertOld2NewSavedQueryDataModel(self._dataMgr, queryModel)

        # Verify its state.
        if not queryModel.isOk():
            self._logger.error(
                "Invalid file path contained in responses <%s>" %
                (queryModel.getResponses(),)
            )

        # Before the dumps, not after: a caller asking for the object threw
        # the bytes away, and a pickling failure turned a request that had
        # already succeeded into None.
        if unpickled:
            return queryModel

        try:
            pickledQuery = pickle.dumps(queryModel)
        except Exception:
            self._logger.error(
                "The query data model could not be pickled",
                exc_info=True,
            )
            return None

        return pickledQuery


    ###########################################################
    def _sendIftttRulesAndCameras(self, extraRules, extraCameras):
        """No-op: state relay to ifttt.sighthound.com is no longer used."""
        pass


    ###########################################################
    def _setDebugConfiguration(self,config):
        self._queue.put([MessageIds.msgIdSetDebugConfig, config])
        self._debugLogManager.SetLogConfig(config)

    ###########################################################
    def _addRuleRpc(self, pickledQueryBin, enabled):
        """Add or replace a rule. Wrapper for RPC calls.

        @param  pickledQueryBin  xmlrpclib.Binary-wrapped
                                 pickled SavedQueryDataModel
        @param  enabled          True if the rule should be enabled.
        @return success          True if the rule was added successfully.
        """
        return self._addRule(pickledQueryBin.data, enabled)

    ###########################################################
    def _safeRename(self, src, dst):
        """ Safely move file (attempt to remove destination first)
        """
        try:
            os.remove(dst)
        except:
            pass
        os.rename(src, dst)


    ###########################################################
    def _addRule(self, pickledQuery, enabled, origName=None, postDeleteMessage=False):
        """Add or replace a rule

        @param  pickledQuery  A pickled SavedQueryDataModel.
        @param  enabled       True if the rule should be enabled.

        @return success       True if the rule was added successfully.
        """
        if self._areRulesLocked():
            return False

        isEdit = origName is not None

        # Ensure our rule directory exists
        if not os.path.isdir(self._ruleDir):
            os.mkdir(self._ruleDir)

        try:
            query = pickle.loads(pickledQuery)
        except Exception:
            self._logger.error("The query could not be unpickled")
            return False

        name = query.getName()
        camLoc = query.getVideoSource().getLocationName()

        if not query.isOk():
            self._logger.error(
                "Invalid file path contained in responses <%s>" %
                (query.getResponses(),)
            )
            query.fixIfInvalid()

        ruleFilename = os.path.join(self._ruleDir, name+kRuleExt)
        queryFilename = os.path.join(self._ruleDir, name+kQueryExt)

        # Create a backup of the files if needed
        if isEdit:
            origRuleFilename = os.path.join(self._ruleDir, origName+kRuleExt)
            origQueryFilename = os.path.join(self._ruleDir, origName+kQueryExt)
            try:
                self._safeRename(origRuleFilename,  origRuleFilename+kBackupExt)
                self._safeRename(origQueryFilename, origQueryFilename+kBackupExt)
            except:
                self._logger.error("Failed to back up rule and query files for '" + ensureUtf8(origName) +
                                    ": " + traceback.format_exc())
                try:
                    os.remove(origRuleFilename+kBackupExt)
                    os.remove(origQueryFilename+kBackupExt)
                except:
                    pass
                return False

        # Create a rule
        rule = RealTimeRule(name, camLoc)
        rule.setEnabled(enabled)
        pickledRule = pickle.dumps(rule)
        pickledQuery = pickle.dumps(query)

        # Write the rule and query to disk
        success = False
        if not writeStringToFile(ruleFilename, pickledRule, self._logger):
            self._logger.error("Failed to write rule '" + ensureUtf8(name) + "' to " + ensureUtf8(ruleFilename))
        elif not writeStringToFile(queryFilename, pickledQuery, self._logger):
            self._logger.error("Failed to write query '" + ensureUtf8(name) + "' to " + ensureUtf8(queryFilename))
        else:
            success = True

        if success:
            if camLoc != kAnyCameraStr:
                if isEdit and postDeleteMessage:
                    self._queue.put([MessageIds.msgIdRuleDeleted, origName])
                self._queue.put([MessageIds.msgIdRuleAdded, name, pickledRule,
                                 pickledQuery])

            self._sendIftttRulesAndCameras([],[])
            if isEdit:
                try:
                    os.remove(origRuleFilename+kBackupExt)
                    os.remove(origQueryFilename+kBackupExt)
                except:
                    pass
        else:
            # Attempt to recover the old rule
            if isEdit:
                try:
                    self._safeRename(origRuleFilename+kBackupExt,  origRuleFilename)
                    self._safeRename(origQueryFilename+kBackupExt, origQueryFilename)
                except:
                    self._logger.error("Failed to restore rule and query files for '" + ensureUtf8(origName) +
                                        ": " + ": " + traceback.format_exc() )

        return success

    ###########################################################
    def _deleteRule(self, ruleName, postMessage=True):
        """Delete an existing rule

        @param  ruleName     The name of the rule to delete.
        @param  postMessage  True if the back end should be notified.
        """
        if self._areRulesLocked():
            return
        try:
            os.remove(os.path.join(self._ruleDir, ruleName + kRuleExt))
        except Exception:
            self._logger.warning("Delete rule exception", exc_info=True)

        try:
            os.remove(os.path.join(self._ruleDir, ruleName + kQueryExt))
        except Exception:
            self._logger.warning("Delete rule exception", exc_info=True)

        if postMessage:
            self._queue.put([MessageIds.msgIdRuleDeleted, ruleName])


    ###########################################################
    def _getRuleNames(self):
        """Get the names of all saved rules.

        @return ruleNames  The names of the rules.
        """
        ruleNames = []

        # Ensure the query directory exists
        if not os.path.isdir(self._ruleDir):
            return ruleNames

        # Retrieve the query names
        dirFiles = os.listdir(self._ruleDir)
        for f in dirFiles:
            f = normalizePath(f)
            name, ext = os.path.splitext(f)
            if ext == kRuleExt:
                ruleNames.append(name)

        return ruleNames


    ###########################################################
    def _getRuleRpc(self, ruleName):
        """Retrieve a saved rule. Wrapper for RPC calls.

        @param  ruleName        The name of the rule to retrieve.
        @return pickledRuleBin  A pickled RuleDataModel or None,
                                xmlrpclib.Binary() encoded.
        """
        return xmlrpc.client.Binary(self._getRule(ruleName))


    ###########################################################
    def _getRule(self, ruleName):
        """Retrieve a saved rule.

        @param  ruleName     The name of the rule to retrieve.
        @return pickledRule  A pickled RuleDataModel or None.
        """
        filePath = os.path.join(self._ruleDir, ruleName+kRuleExt)

        # Ensure the rule fileexists
        if not os.path.isfile(filePath):
            self._logger.error("Rule %s doesn't exist" % filePath)
            return None

        # Read the rule from disk
        ruleFile = open(filePath, 'rb')
        pickledRule = ruleFile.read()
        ruleFile.close()

        return pickledRule


    ###########################################################
    def _setRuleSchedule(self, ruleName, schedule):
        """Set the schedule of an existing saved rule.

        @param  ruleName  The name of the rule to update.
        @param  schedule  A schedule dict to apply to the rule.
        """
        if self._areRulesLocked():
            return

        filePath = os.path.join(self._ruleDir, ruleName+kRuleExt)
        if not os.path.isfile(filePath):
            self._logger.error("Rule %s doesn't exist" % filePath)
            return

        ruleFile = open(filePath, 'rb')
        rule = pickle.load(ruleFile)
        ruleFile.close()

        ruleFile = open(filePath, 'wb')
        rule.setSchedule(schedule)
        pickle.dump(rule, ruleFile)
        ruleFile.close()

        self._queue.put([MessageIds.msgIdRuleScheduleUpdated, ruleName,
                         schedule])


    ###########################################################
    def _enableRule(self, ruleName, enable=True):
        """Set whether a rule is enabled or not.

        @param  ruleName  The name of the rule to update.
        @param  enable    True if the rule should be enabled.
        """
        if self._areRulesLocked():
            return

        filePath = os.path.join(self._ruleDir, ruleName+kRuleExt)
        if not os.path.isfile(filePath):
            self._logger.error("Rule %s doesn't exist" % filePath)
            return

        ruleFile = open(filePath, 'rb')
        rule = pickle.load(ruleFile)
        ruleFile.close()

        ruleFile = open(filePath, 'wb')
        rule.setEnabled(enable)
        pickle.dump(rule, ruleFile)
        ruleFile.close()

        self._queue.put([MessageIds.msgIdRuleEnabled, ruleName, enable])


    ###########################################################
    def _getRuleInfoForLocation(self, location):
        """Retrieve a list of information about rules for a given location.

        @param  location  The name of the location to retrieve information on.
                          If None, gets for all locations...
        @return infoList  A list of (ruleName, queryName, scheduleString,
                          isEnabled, responseNames) for each rule at location.
        """
        infoList = []

        for name in self._getRuleNames():
            try:
                rule = pickle.loads(self._getRule(name))
            except Exception:
                self._logger.error("Rule %s could not be loaded" % name)
                continue

            if (location is not None) and \
               (rule.getCameraLocation().lower() != location.lower()):
                continue

            responseTypes = self._getActiveResponseTypes(name)
            if responseTypes:
                scheduleSummary = rule.getScheduleSummary(
                        self._prefs.getPref('timePref12'))
            else:
                scheduleSummary = "No Responses."
            infoList.append((name, rule.getQueryName(), scheduleSummary,
                             rule.isEnabled(), responseTypes))

        return infoList


    ###########################################################
    def _getRuleInfo(self, ruleName):
        """Retrieve a list of information about a rule.

        @param  ruleName  The name of the rule to retrieve information on.
        @return info      (ruleName, queryName, scheduleString, isEnabled,
                          responseNames) for the given rule or None.
        """
        try:
            rule = pickle.loads(self._getRule(ruleName))
        except Exception:
            self._logger.error("Rule %s could not be loaded" % ruleName)
            return None

        responseTypes = self._getActiveResponseTypes(ruleName)
        if responseTypes:
            scheduleSummary = rule.getScheduleSummary(
                        self._prefs.getPref('timePref12'))
        else:
            scheduleSummary = "No Responses."
        return (ruleName, rule.getQueryName(), scheduleSummary,
                rule.isEnabled(), responseTypes)


    ###########################################################
    def _getScheduleIconStatesForLocation(self, location):
        """Per-rule clock-icon state for the Search screen.

        Returns a dict {ruleName: bool} containing ONLY rules whose schedule is
        non-default (anything other than 24 hours / Every day): True = the
        schedule can run (enabled clock), False = it can never run (disabled
        clock).  Rules with the default schedule are omitted (no clock icon).
        A bool-only dict serializes cleanly over XML-RPC.

        @param  location  Camera location to filter by; None for all.
        @return states    {ruleName: bool}
        """
        states = {}
        for name in self._getRuleNames():
            try:
                rule = pickle.loads(self._getRule(name))
            except Exception:
                continue
            if (location is not None) and \
               (rule.getCameraLocation().lower() != location.lower()):
                continue
            iconState = scheduleIconState(rule.getSchedule())
            if iconState is not None:
                states[name] = iconState
        return states


    ###########################################################
    def _setMaxStorageSize(self, maxSize):
        """Specify the maximum disk space to be used for video storage

        @param  maxSize  The maximum size in bytes to use for video storage
        """
        self._prefs.setPref('maxStorageSize', maxSize)
        self._queue.put([MessageIds.msgIdSetMaxStorage, maxSize])


    ###########################################################
    def _getMaxStorageSize(self):
        """Get the maximum disk space to be used for video storage.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return maxSize  The maximum size in bytes to use for video storage.
        """
        return self._prefs.getPref('maxStorageSize')


    ###########################################################
    def _setSummarySettings(self, enabled, path):
        """Persist the daily-summary-video settings.

        Stored in backEndPrefs; the DiskCleaner reads them directly each pass.

        @param  enabled  Whether to generate a daily summary video per camera.
        @param  path     Root folder to write the summaries into.
        """
        self._prefs.setPref('summaryEnabled', bool(enabled), save=False)
        self._prefs.setPref('summaryDir', path)


    ###########################################################
    def _getSummarySettings(self):
        """Return the daily-summary-video settings.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return settings  {'enabled': bool, 'outputDir': str}
        """
        return {'enabled': bool(self._prefs.getPref('summaryEnabled')),
                'outputDir': self._prefs.getPref('summaryDir') or ''}


    ###########################################################
    def _setCacheDuration(self, cacheDuration):
        """Specify the number of hours of cache to store.

        @param  cacheDuration  The number of hours of cache to store.
        """
        self._prefs.setPref('cacheDuration', cacheDuration)
        self._queue.put([MessageIds.msgIdSetCacheDuration, cacheDuration])


    ###########################################################
    def _getCacheDuration(self):
        """Get the maximum disk space to be used for video storage.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  cacheDuration  The number of hours of cache to store.
        """
        return self._prefs.getPref('cacheDuration')

    ###########################################################
    def _setRecordInMemory(self, recordInMemory):
        """Specify the number of hours of cache to store.
        """
        self._prefs.setPref('recordInMemory', recordInMemory)
        self._queue.put([MessageIds.msgIdSetRecordInMemory, recordInMemory])


    ###########################################################
    def _getRecordInMemory(self):
        """Get the maximum disk space to be used for video storage.

        NOTE: This function is thread safe. Ensure any changes preserve that.
        """
        return self._prefs.getPref('recordInMemory')

    ###########################################################
    def _setClipMergeThreshold(self, value):
        """Specify how clips will be merged during search
        """
        self._prefs.setPref(kClipMergeThreshold, value)
        # Update the local ClipManager's cache ... it may not be the exact value that
        # will be written to db by the BackEndApp, but is close enough for local queries
        self._clipMgr.setClipMergeThreshold(getTimeAsMs(), value, False)
        self._queue.put([MessageIds.msgIdSetClipMergeThreshold, value])


    ###########################################################
    def _getClipMergeThreshold(self):
        """Return threshold determining whether clips will be merged during search in seconds
        """
        return self._prefs.getPref(kClipMergeThreshold)


    ###########################################################
    def _getHardwareDevicesList(self):
        return self._hardwareDevices

    ###########################################################
    def _getHardwareDevice(self):
        return self._prefs.getPref(kHardwareAccelerationDevice)

    ###########################################################
    def _setHardwareDevice(self, dev):
        self._prefs.setPref(kHardwareAccelerationDevice, dev)
        self._logger.info("Hardware acceleration settings had changed to '" + dev + "'. Restarting the cameras ...")
        camMgrState = self._camMgr.dump()
        self._queue.put([MessageIds.msgIdHardwareAccelerationSettingUpdated, camMgrState, dev])


    ###########################################################
    def _setStorageLocation(self, location):
        """Specify the location to store the databases.

        @param  location  The path at which to store the databases.
        """
        self._prefs.setPref('dataDir', location)
        self._queue.put([MessageIds.msgIdSetStorageLocation, location])


    ###########################################################
    def _getStorageLocation(self):
        """Retrieve the location specified to store the databases.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return location  The path at which to store the databases.
        """
        return self._prefs.getPref('dataDir') or self._localDataDir


    ###########################################################
    def _setVideoLocation(self, location, preserveData=True,
                          keepExisting=False):
        """Specify the location to store recorded video.

        @param  location      The path at which to store recorded video.
        @param  preserveData  If True existing data should be moved to the new
                              location.
        @param  keepExisting  If True and preserveData is False, data at the
                              new location will not be removed and the
                              databases will not be reset.
        """
        self._locationChangeStatus = (False, False)
        self._queue.put([MessageIds.msgIdSetVideoLocation, location,
                         preserveData, keepExisting])


    ###########################################################
    def _setVideoLocationChangeStatus(self, location, success):
        """Set the results of a video location change operation.

        @param  location  The attempted path change for recorded video.
        @param  success   True if the operation succeeded, False if failed.
        """
        if success:
            self._prefs.setPref('videoDir', location)
            self._dataMgr.setVideoStoragePath(os.path.join(location,
                                                           kVideoFolder))

        self._locationChangeStatus = (True, success)


    ###########################################################
    def _getVideoLocationChangeStatus(self):
        """Get the status of a video location change operation.

        @return finished  True if the operation has completed.
        @return success   True if the operation succeeded, False if failed.
        """
        return self._locationChangeStatus


    ###########################################################
    def _getVideoLocation(self):
        """Retrieve the location specified to store recorded video.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return location  The path at which to store recorded video.
        """
        return self._prefs.getPref('videoDir') or os.path.join(self._localDataDir, "videos")


    ###########################################################
    def _setEmailSettings(self, emailSettings):
        """Set settings related to email.

        @param  emailSettings  A dict describing email settings; see
                               BackEndPrefs for details.
        """
        self._prefs.setPref('emailSettings', emailSettings)
        self._queue.put([MessageIds.msgIdSetEmailSettings, emailSettings])


    ###########################################################
    def _getEmailSettings(self):
        """Retrieve settings related to email.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return emailSettings  A dict describing email settings; see
                               BackEndPrefs for details.
        """
        return self._prefs.getPref('emailSettings')


    ###########################################################
    def _setFtpSettings(self, ftpSettings):
        """Set settings related to ftp.

        @param  ftpSettings  A dict describing ftp settings; see
                             BackEndPrefs for details.
        """
        self._prefs.setPref('ftpSettings', ftpSettings)
        self._queue.put([MessageIds.msgIdSetFtpSettings, ftpSettings])


    ###########################################################
    def _getFtpSettings(self):
        """Retrieve settings related to ftp.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return ftpSettings  A dict describing ftp settings; see
                             BackEndPrefs for details.
        """
        return self._prefs.getPref('ftpSettings')


    ###########################################################
    def _setArmSettings(self, armSettings):
        """Set settings related to arming a camera.

        @param  armSettings  A dict describing arm settings; see
                             BackEndPrefs for details.
        """
        self._prefs.setPref('armSettings', armSettings)


    ###########################################################
    def _getArmSettings(self):
        """Retrieve settings related to arming a camera.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return armSettings  A dict describing arm settings; see
                             BackEndPrefs for details.
        """
        return self._prefs.getPref('armSettings')


    ###########################################################
    def _getPendingClipInfo(self, protocol):
        """Retrieve info about pending clips.

        @param  protocol       The protocol to query about.
        @return queueLength    The length of the queue of pending clips.
        @return startTime      The start time of the last clip sent; or None.
        @return stopTime       The stop time of the last clip sent; or None.
        @return processAtTime  The time that the last clip was requested to
                               be processed at (currently the time the clip
                               was intended to be put in the queue); or None.
        @return sentAtTime     The time that the last clip was marked as sent;
                               or None.
        @return ruleName       The name of the rule that was used to send
                               the last clip; or None.
        """
        queueLength = self._responseDb.countQueueLength(protocol)
        lastSentInfo = self._responseDb.getLastSentInfo(protocol)
        if lastSentInfo is None:
            lastSentInfo = (None, None, None, None, None)
        startTime, stopTime, processAt, sentAt, ruleName = lastSentInfo

        result = (queueLength, startTime, stopTime, processAt, sentAt, ruleName)

        # Pickle, since XMLRPC doesn't handle long ints...
        pickledResult = pickle.dumps(result, pickle.HIGHEST_PROTOCOL)
        return xmlrpc.client.Binary(pickledResult)


    ###########################################################
    def _purgePendingClips(self, protocol):
        """Purge all pending clips.

        @param  protocol       The protocol to purge.
        """
        self._responseDb.purgePendingClips(protocol)


    ###########################################################
    def _enableLiveView(self, cameraLocation, enable=True):
        """Enable or disable the live viewing of a camera

        @param  cameraLocation  The camera to enable or disable viewing for.
        @param  enable          True if viewing should be enabled.
        """
        if enable:
            self._queue.put([MessageIds.msgIdEnableLiveView, cameraLocation])
        else:
            self._queue.put([MessageIds.msgIdDisableLiveView, cameraLocation])


    ###########################################################
    def _setLiveViewParams(self, cameraLocation, width, height, audioVolume,
                            fps):
        """Set the camera currently active in the monitor view.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  cameraLocation  The camera active in the monitor view.
        @param  width           The width requested for the large view frames.
        @param  height          The height requested for the large view frames.
        @param  audioVolume     Volume to render audio at, or 0 to mute,
        @param  fps             Desired frame rate. Zero (default) for
                                exclusivity, meaning only one camera will be
                                running in this monitor mode.
        """
        self._queue.put([MessageIds.msgIdSetLiveViewParams, cameraLocation,
                width, height, audioVolume, fps])


    ###########################################################
    def _flushVideo(self, cameraLocation):
        """Force a camera to formalize any temporary files.

        @param  cameraLocation   The camera to flush.
        @return lastProcessedSec Seconds of last time processed on this camera.
        @return lastProcessedMs  Ms of hte last time processed on this camera.
        @return lastTaggedSec    Ms of the last time tagged on this camera.
        @return lastTaggedMs     Ms of the last time tagged on this camera.
        """
        self._updateCameraProgress()
        self._queue.put([MessageIds.msgIdFlushVideo, cameraLocation])
        processed, tagged = self._cameraUpdateTimes.get(cameraLocation, (0,0))
        pMs = processed % 1000
        tMs = tagged % 1000

        return (processed-pMs)//1000, pMs, (tagged-tMs)//1000, tMs


    ###########################################################
    def _setCameraStatus(self, cameraLocation, status, wsgiPort=None, reason=None):
        """Set the status of a camera.

        @param  cameraLocation  The camera to update.
        @param  status          The status of the camera.
        @param  wsgiPort        The (optional) WSGI server port of the camera.
        """

        if status is None:
            # get got some other information about the camera; status is unchanged
            if wsgiPort is not None:
                self._logger.info("camera '%s' WSGI now on port %d" %
                                  (cameraLocation, wsgiPort))
                self._cameraWsgiPort[cameraLocation] = wsgiPort
            return

        if status == kCameraOff:
            self._cameraWsgiPort.pop(cameraLocation, None)

        assert status in [kCameraOn, kCameraOff,
                          kCameraConnecting, kCameraFailed]

        self._cameraStatus[cameraLocation] = ( status, reason )

    ###########################################################
    def _getCameraStatus(self, cameraLocation):
        """Get the status of a camera.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  cameraLocation  The camera to update.
        @return status          The status of the camera.
        """
        return self._cameraStatus.get(cameraLocation, (kCameraOff, None))[0]

    ###########################################################
    def _getCameraStatusAndReason(self, cameraLocation):
        """Get the status of a camera and reason for failure, if applicable.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  cameraLocation  The camera to update.
        @return status          The (status,statusReason) of the camera.
        """
        return self._cameraStatus.get(cameraLocation, (kCameraOff, None))


    ###########################################################
    def _getCameraStatusAndEnabled(self, cameraLocation):
        """Get the status of a camera, and whether it's enabled.

        This function exists to reduce the number of calls to the back end,
        since that can waste a lot of network sockets.

        @param  cameraLocation  The camera to update.
        @return status          The status of the camera.
        @return isEnabled       True if the camera is enabled.
        """
        _, _, isEnabled, _ = self._camMgr.getCameraSettings(cameraLocation)
        return (self._getCameraStatus(cameraLocation), isEnabled)

    ###########################################################
    def _getCameraStatusEnabledAndReason(self, cameraLocation):
        """Get the status of a camera, reason for status, and whether it's enabled.

        This function exists to reduce the number of calls to the back end,
        since that can waste a lot of network sockets.

        @param  cameraLocation  The camera to update.
        @return status          The status of the camera.
        @return reason          Failure reason if applicable
        @return isEnabled       True if the camera is enabled.
        """
        _, _, isEnabled, _ = self._camMgr.getCameraSettings(cameraLocation)
        status, reason = self._getCameraStatusAndReason(cameraLocation)
        return (status, reason, isEnabled)

    ###########################################################
    def _setHealthInfo(self, kind, data):
        """Store a system-health value pushed in by another process.

        These numbers are already computed elsewhere (disk space every cleanup
        cycle, clock offset by the daily NTP check) but were only ever emitted
        as one-shot alert events.  Stashing the latest here makes them pollable
        by the health view without duplicating the work.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  kind  One of 'disk', 'clock', 'procs', 'database', 'started'.
        @param  data  Value for that kind (see _getSystemHealth).
        """
        if kind == 'disk':
            self._healthDisk = data
        elif kind == 'clock':
            self._healthClockOffset = data
        elif kind == 'procs':
            self._healthProcs = data or {}
        elif kind == 'started':
            # Epoch ms the back end started, as a float (32-bit XML-RPC ints
            # cannot hold epoch milliseconds).
            self._healthStartedMs = data
        elif kind == 'database':
            # Corruption reports + clip-registration liveness.  Detection
            # only -- nothing here triggers a repair (2026-08-04).
            self._healthDatabase = data or {}
        elif kind == 'camera':
            # data is [cameraLocation, healthDict] from CameraCapture.
            try:
                camera, health = data
                if camera:
                    self._cameraHealth[camera] = dict(health or {})
            except Exception:
                self._logger.warning("Invalid camera health payload: %r", data,
                                     exc_info=True)
        return True


    ###########################################################
    def _noteCameraReconnect(self, cameraLocation):
        """Count a camera process restart (the back end restarts dead cameras).

        NOTE: This function is thread safe. Ensure any changes preserve that.
        """
        self._cameraReconnects[cameraLocation] = \
            self._cameraReconnects.get(cameraLocation, 0) + 1
        return True


    ###########################################################
    def _sampleGpuOnce(self):
        """Run nvidia-smi once and return the parsed sample dict, or None."""
        # NOTE: utilization.gpu is the GRAPHICS/COMPUTE (SM) engine only -- it
        # does NOT reflect the video-decode engine.  With NVDEC camera decode
        # the decoder can be pegged while utilization.gpu reads low, so the
        # decoder/encoder engines are queried explicitly too.
        cmd = ['nvidia-smi',
               '--query-gpu=name,utilization.gpu,memory.used,memory.total,'
               'temperature.gpu,power.draw,utilization.decoder,'
               'utilization.encoder',
               '--format=csv,noheader,nounits']
        # CREATE_NO_WINDOW stops a console flashing on Windows every time we
        # sample; the flag does not exist on the other platforms.
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=2,
                             creationflags=getattr(subprocess,
                                                   'CREATE_NO_WINDOW', 0))
        if out.returncode != 0:
            return None
        # e.g. "NVIDIA GeForce GTX 1650 Ti, 39, 2869, 4096, 67, 9.13"
        parts = [p.strip() for p in
                 out.stdout.decode('utf-8', 'replace').splitlines()[0].split(',')]
        if len(parts) < 6:
            return None
        def _num(s, cast):
            try:
                return cast(float(s))
            except Exception:
                return None      # "[N/A]" etc.
        return {
            'name':       parts[0],
            'utilPct':    _num(parts[1], int),
            'memUsedMB':  _num(parts[2], int),
            'memTotalMB': _num(parts[3], int),
            'tempC':      _num(parts[4], int),
            'powerW':     _num(parts[5], float),
            'decoderPct': _num(parts[6], int) if len(parts) > 6 else None,
            'encoderPct': _num(parts[7], int) if len(parts) > 7 else None,
        }


    ###########################################################
    def _gpuSamplerLoop(self):
        """Background GPU sampling for the health view.

        Kept OFF the request path: spawning nvidia-smi takes ~100ms (and can
        block longer), while the front end polls getSystemHealth from its UI
        thread -- the poll must only ever read a cached value.  The loop
        samples every ~5s but idles whenever nobody has polled recently, so
        it costs nothing while the System view isn't open.
        """
        _kSampleEverySecs = 5.0
        _kIdleAfterSecs = 15.0
        _kMaxConsecutiveFails = 3
        while True:
            try:
                if self._gpuDisabled:
                    return
                if time.time() - self._lastHealthPoll > _kIdleAfterSecs:
                    time.sleep(1.0)
                    continue
                sample = None
                try:
                    sample = self._sampleGpuOnce()
                except Exception:
                    sample = None
                if sample is None:
                    self._gpuFails += 1
                    if self._gpuFails >= _kMaxConsecutiveFails:
                        self._gpuDisabled = True
                        self._logger.info(
                            'GPU sampling disabled (nvidia-smi unavailable '
                            'or failing)')
                        return
                else:
                    self._gpuFails = 0
                    self._gpuSample = sample
                    self._gpuSampleTime = time.time()
                time.sleep(_kSampleEverySecs)
            except Exception:
                # Never let the sampler thread die noisily.
                time.sleep(_kSampleEverySecs)


    ###########################################################
    def _getLoadedModels(self):
        """Names of the ML models the detection service actually has loaded.

        Read from the file the service publishes after each load rather than
        asked for over its socket: a health poll must never block on a service
        that is busy or wedged.  Re-parsed only when the file's mtime moves.

        @return  List of names in display order, or None when it can't be
                 trusted (no file yet, unreadable, or left over from a
                 detection service that has since died).
        """
        try:
            path = modelsFilePath()
            mtime = os.path.getmtime(path)
            if mtime != self._healthModelsMtime:
                with open(path, 'r') as f:
                    data = json.load(f)
                self._healthModelsMtime = mtime
                self._healthModels = data
            data = self._healthModels or {}
            # A file from a dead service would report models nobody is
            # running.  The process map is authoritative about which pid the
            # detection service has right now.
            livePid = (self._healthProcs or {}).get('Detection service')
            if livePid and data.get('pid') != livePid:
                return None
            models = data.get('models')
            return list(models) if models is not None else None
        except Exception:
            return None


    ###########################################################
    def _getSystemHealth(self):
        """Aggregate a snapshot of overall system health for the health view.

        Everything here is either already tracked in this process or pushed in
        by the owning process, so a poll is cheap -- the only real work is
        sampling per-process memory/CPU, which is a handful of Win32 calls.

        CPU percentages are derived from the gap between successive polls, so
        the first call after startup reports None for cpuPct.

        @return health  Dict of camera rows plus system-wide values.
        """
        nowMs = getTimeAsMs()
        now = time.time()

        # Wake/start the GPU sampler (it idles when nobody polls; the poll
        # itself must never wait on nvidia-smi, so it only reads the cache).
        self._lastHealthPoll = now
        if self._gpuThread is None and not self._gpuDisabled:
            self._gpuThread = threading.Thread(target=self._gpuSamplerLoop,
                                               name='gpuSampler', daemon=True)
            self._gpuThread.start()

        cameras = []
        try:
            locations = self._camMgr.getCameraLocations()
        except Exception:
            locations = list(self._cameraStatus.keys())
        for loc in sorted(locations):
            status, reason = self._cameraStatus.get(loc, (kCameraOff, None))
            try:
                _, _, isEnabled, _ = self._camMgr.getCameraSettings(loc)
            except Exception:
                isEnabled = None
            lastMs = None
            times = self._cameraUpdateTimes.get(loc)
            if times:
                lastMs = max(t for t in times if t) if any(times) else None
            cameras.append({
                'name':        loc,
                'status':      status,
                'reason':      reason or '',
                'enabled':     isEnabled,
                'lastFrameAgeSecs': ((nowMs - lastMs) / 1000.0
                                     if lastMs else None),
                'reconnects':  self._cameraReconnects.get(loc, 0),
                'wsgiPort':    self._cameraWsgiPort.get(loc),
                'health':      dict(self._cameraHealth.get(loc, {})),
            })

        # Per-process memory + CPU%.  CPU needs two samples, so it's derived
        # from the previous poll's counter.
        procs = []
        livePids = set()
        for name, pid in sorted((self._healthProcs or {}).items()):
            if not pid:
                continue
            livePids.add(pid)
            memMB = None
            try:
                mem = getMemoryStats(pid)
                if mem and mem.get('priv'):
                    memMB = int(round(mem['priv'] / 1024.0))
            except Exception:
                pass
            cpuPct = None
            try:
                cpuSecs = getCpuSeconds(pid)
                if cpuSecs is not None:
                    prev = self._cpuSamples.get(pid)
                    if prev:
                        dt = now - prev[0]
                        if dt > 0.05:   # ignore rapid double-polls
                            cpuPct = max(0.0, (cpuSecs - prev[1]) / dt * 100.0)
                    self._cpuSamples[pid] = (now, cpuSecs)
            except Exception:
                pass
            procs.append({'name': name, 'pid': pid,
                          'memMB': memMB, 'cpuPct': cpuPct})
        # Forget samples for processes that have gone away (camera restarts
        # recycle PIDs, so a stale entry would produce a nonsense percentage).
        for pid in list(self._cpuSamples.keys()):
            if pid not in livePids:
                del self._cpuSamples[pid]

        gpu = None
        if self._gpuSample is not None:
            gpu = dict(self._gpuSample)
            gpu['sampleAgeSecs'] = round(now - self._gpuSampleTime, 1)

        return {
            # NOTE: float, not int -- XML-RPC ints are 32-bit and epoch
            # milliseconds overflow that ("int exceeds XML-RPC limits").
            'timestampMs':     float(nowMs),
            'startedMs':       self._healthStartedMs,
            'models':          self._getLoadedModels(),
            'cameras':         cameras,
            'processes':       procs,
            'disk':            self._healthDisk,
            'database':        self._healthDatabase,
            'clockOffsetSecs': self._healthClockOffset,
            'cpuCount':        os.cpu_count() or 1,
            'memory':          getSystemMemoryStats(),
            'gpu':             gpu,
        }


    ###########################################################
    def _getActiveResponseTypes(self, ruleName):
        """Get the status of a camera.

        @param  ruleName        Name of the rule to fetch.
        @return activeReponses  A list of active response types.
        """
        activeResponses = []

        query = self._getQuery(ruleName, True)

        try:
            if query is not None:
                responseList = query.getResponses()
                # "Speak text" (TTS) has its own enable inside the Sound
                # response config — count it as active even without "Play
                # sound" checked.
                activeResponses = [name for (name, config) in responseList
                                if config.get('isEnabled') or
                                   (name == kSoundResponse and
                                    config.get('ttsEnabled'))]
            else:
                self._logger.error("Failed to load query " + str(ruleName))
        except Exception:
            self._logger.error("Get response type exception", exc_info=True)

        return activeResponses


    ###########################################################
    def _getLocalCameraNames(self):
        """Return a list of local camera names.

        This list is cached by NetworkMessageServer and is updated (upon
        request) by the back end.  To request an (asynchronous) update, call
        activeCameraSearch.

        @return localCameraNames  The local camera names.
        """
        return self._localCameraNames


    ###########################################################
    def _setLocalCameraNames(self, localCameraNames):
        """Set local camera names.

        This is called by the backend and updates our cache.

        @param  localCameraNames  The local camera names.
        """
        self._localCameraNames = localCameraNames


    ###########################################################
    def _startCameraTest(self, uri, forceTCP):
        """Begin streaming from a camera.

        @param  uri       The uri of the camera to stream
        @param  forceTCP  True to force TCP.
        """
        self._queue.put([MessageIds.msgIdCameraTestStart, uri, forceTCP])
        self._testFailure = False


    ###########################################################
    def _stopCameraTest(self):
        """Stop any camera testing."""
        self._queue.put([MessageIds.msgIdCameraTestStop])
        self._testFailure = False


    ###########################################################
    def _startPacketCapture(self, cameraLocation, delaySeconds, pcapDir):
        """Begin capturing packets from camera.

        @param  cameraLocation  The camera to capture packets from.
        """
        self._queue.put(
            [MessageIds.msgIdPacketCaptureStart, cameraLocation, delaySeconds, pcapDir]
        )


    ###########################################################
    def _stopPacketCapture(self):
        """Stop any packet capturing."""
        self._queue.put([MessageIds.msgIdPacketCaptureStop])


    ###########################################################
    def _deleteVideo(self, cameraLocation, startSec, stopSec, quick):
        """Remove all stored data from a camera between two times.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  cameraLocation  The camera location to delete from.
        @param  startSec        The start time in seconds to begin delting.
        @param  stopSec         The stop time in seconds to stop deleting.
        @param  quick           True if a quick delete should be performed.
        """
        self._queue.put([MessageIds.msgIdDeleteVideo, cameraLocation,
                         startSec, stopSec, quick])


    ###########################################################
    def _setUpnpDevices(self, pickledUpnpDeviceDictBin):
        """Set the UPNP devices.

        It is intended that the back end call this whenever devices show up,
        disappear, or otherwise change in a significant way.  Note that the
        back end _WILL NOT_ call this just to update the expiration date of
        a device.  ...thus, clients (the front end) shouldn't rely on the
        "isExpired" call of the devices to work.

        TODO: If this gets really big, maybe the backend will have to just
        send diffs over the wire?

        @param  pickledUpnpDeviceDictBin  A pickled dict of UpnpDevice objects
                                          keyed by usn. Encoded using
                                          xmlrpclib.Binary().
        """
        self._pickledUpnpDeviceDict = pickledUpnpDeviceDictBin.data
        self._upnpDictRevNum += 1


    ###########################################################
    def _getUpnpDevices(self):
        """Get the UPNP devices.

        Return all of the UPNP devices found.  See _setUpnpDevices() for
        details.  One word of note from that function's comments: don't use
        'isExpired' on these devices--it may not be valid.

        @return pickedUpnpDeviceDict  A pickled dict of UpnpDevice objects.
                                      Keyed by usn.
        """
        return xmlrpc.client.Binary(self._pickledUpnpDeviceDict)


    ###########################################################
    def _getUpnpDictRevNum(self):
        """Get the revision number of the UPNP device dict.

        This can be useful so we don't have to transfer quite as much data
        while polling.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return upnpDictRevNum  The revision of the UPNP device dict.
        """
        return self._upnpDictRevNum


    ###########################################################
    def _setOnvifDevices(self, pickledOnvifDeviceDictBin):
        """Set the ONVIF devices.

        It is intended that the back end call this whenever devices show up,
        disappear, or otherwise change in a significant way.

        TODO: If this gets really big, maybe the backend will have to just
        send diffs over the wire?

        @param  pickledOnvifDeviceDictBin  A pickled dict of OnvifDevice objects
                                           keyed by uuid. Encoded using
                                           xmlrpclib.Binary().
        """
        self._pickledOnvifDeviceDict = pickledOnvifDeviceDictBin.data
        self._onvifDictRevNum += 1


    ###########################################################
    def _getOnvifDevices(self):
        """Get the ONVIF devices.

        Return all of the ONVIF devices found.  See _setOnvifDevices() for
        details.

        @return pickedOnvifDeviceDict  A pickled dict of OnvifDevice objects.
                                      Keyed by uuid.
        """
        return xmlrpc.client.Binary(self._pickledOnvifDeviceDict)


    ###########################################################
    def _getOnvifDictRevNum(self):
        """Get the revision number of the ONVIF device dict.

        This can be useful so we don't have to transfer quite as much data
        while polling.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return onvifDictRevNum  The revision of the ONVIF device dict.
        """
        return self._onvifDictRevNum


    ###########################################################
    def _setOnvifSettings(self, uuid, selectedIp, username, password):
        """Sets the authentication information for an ONVIF device so we can
        retrieve its stream URI's and profiles.

        @param uuid        The unique identifier of the ONVIF device.
        @param selectedIp  The device's IP address that we want to communicate with.
        @param username    The username that has access to the stream URI's and
                           profiles that we want.
        @param password    Password to the username.
        """
        self._queue.put([MessageIds.msgIdSetOnvifSettings, \
                         uuid, selectedIp, username, password])


    ###########################################################
    def _activeCameraSearch(self, isMajor=False):
        """Actively search for cameras.

        This will actively search the computer for more cameras.  After this is
        called, it's likely more UPNP and ONVIF cameras or local cameras will show up
        in other calls.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  isMajor  If True, do a more major, heavyweight search. This is
                         done once when the "Edit Camera" dialog comes up. After
                         that, we'll do more minor searches once every 5 seconds
        """
        self._queue.put([MessageIds.msgIdActiveCameraSearch, isMajor])


    ###########################################################
    def _setTestCameraFailed(self, failed=True):
        """Mark whether the test camera failed connecting or not.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  failed  If True the test camera is marked as failed.
        """
        self._testFailure = failed


    ###########################################################
    def _testCameraFailed(self):
        """Retrieve whether the test camera failed connecting or not.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return failed  True if the test camera is marked as failed.
        """
        return self._testFailure


    ###########################################################
    def _setPacketCaptureInfo(self, infoDict):
        """Set the current status of the packet capture stream.

        @param  status  A pickled list where the first element is a msgID from
                        MessageIds.py.  The input can also be an empty list or
                        None if the stream is not active.
        """
        self._pickledPacketCaptureInfo = infoDict


    ###########################################################
    def _getPacketCaptureInfo(self):
        """Get the current status of the packet capture stream.

        @return status  A pickled list where the first element is a msgID from
                        MessageIds.py.  The input can also be an empty list or
                        None if the stream is not active.
        """
        return self._pickledPacketCaptureInfo


    ###########################################################
    def _updateCameraProgress(self):
        """Update the stored camera progress based on items in the queue."""
        while True:
            # Only QueueEmpty ends the drain.  get(False) has already popped
            # the item by the time anything below could raise, so a catch-all
            # here silently dropped that camera's update AND abandoned the
            # rest of the batch, with nothing in the log.  Anything else now
            # reaches dispatchWithPriority, which logs the traceback.
            try:
                camName, lastProcessedMs, lastTaggedMs = \
                                            self._camProcessQueue.get(False)
            except QueueEmpty:
                return
            prevProcessed, prevTagged = self._cameraUpdateTimes.get(camName,
                                                                    (0, 0))
            self._cameraUpdateTimes[camName] = (max(lastProcessedMs,
                                                    prevProcessed),
                                                max(lastTaggedMs,
                                                    prevTagged))


    ###########################################################
    def _addMessage(self, message):
        """Add a message to the message list.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  message  A list, the first entry of which is from MessageIds.
        """
        if message[0] == MessageIds.msgIdLicenseChanged:
            self._logger.info("intercepting license-changed message...")
            self._licenseChanged(message[1], message[2])
        dup = not self._frontEndMessageQueue.enqueue(message)
        self._logger.info("enqueued message (type=%d, dup=%s, qlen=%d)" %
                          (message[0], dup, len(self._frontEndMessageQueue)))


    ###########################################################
    def _getMessage(self):
        """Get the next pending message.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return message  A list, the first entry of which is from MessageIds.
                         Or an empty list if no message is pending.
        """
        # NOTE: this used to short-circuit on the presence of err-corruptdb and
        # return msgIdDatabaseCorrupt, which closed the front end.  Two things
        # were wrong with that.  It reported damage from a FILE rather than from
        # any check, so it fired for as long as the file existed; and because it
        # returned early it starved the queue -- while that file was there, no
        # other message could reach the front end at all.
        #
        # err-corruptdb now means only "the user asked for a repair", which is
        # not something to interrupt them about.  Damage is reported once, from
        # evidence, via BackEndApp._raiseCorruptionFlag -> msgIdDatabaseDamaged.
        return self._frontEndMessageQueue.dequeue()


    ###########################################################
    def _remoteGetCameraNames(self):
        """Retrieve a list of all searchable camera names.

        @return success   True if the operation was successful.  If not
                          successful, the only additional return will be a
                          string explaining the error.
        @return camNames  A list of all searchable camera names.
        """
        try:
            # Cameras that are currently setup; may or may not have recorded
            # video. Some might be frozen though.
            camLocs = self._camMgr.getCameraLocations()
            activeCams = []
            frozenCams = []
            for camLoc in camLocs:
                if self._camMgr.isCameraFrozen(camLoc):
                    frozenCams.append(camLoc + kInactiveSuffix)
                else:
                    activeCams.append(camLoc)

            # A list of cameras that have recorded video; may or may not be
            # currently setup...
            allCams = self._clipMgr.getCameraLocations()

            # Get inactive cameras out, ignoring imported ones...
            inactiveCams = [
                camName + kInactiveSuffix
                for camName in allCams if (
                     not camName.endswith(kImportSuffix) and
                     camName not in camLocs
                )
            ]
            names = activeCams+frozenCams+inactiveCams
            names.sort(key=lambda x: x.lower())

            # If we only have one camera location hide "Any camera".
            if len(names) != 1:
                names = [kAnyCameraStr] + names

            return True, names
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError


    ###########################################################
    def _remoteGetCameraDetailsAndRules(self, cameraName, sessionId):
        """Retrieve a dict of details and rules for a given cameras.

        @return success   True if the operation was successful.  If not
                          successful, the only additional return will be a
                          string explaining the error.
        @return camNames  A list dict containing the camera's details and rules
        """
        try:
            statusAndEnabled = self._getCameraStatusAndEnabled(cameraName)
            liveJpegUri = self._remoteGetCameraUri(cameraName, sessionId, None, _kMimeTypeJpeg)[1]
            liveH264Uri = self._remoteGetCameraUri(cameraName, sessionId, None, _kMimeTypeVideoH264)[1]
            allRules = self._getRuleInfoForLocation(cameraName)
            cameraRules = []
            if len(allRules):
                for rule in allRules:
                    cameraRules.append({'name': rule[0], 'schedule': rule[2], 'enabled': rule[3]})

            camerasDict = {
                'name': cameraName,
                'active': True,
                'enabled': statusAndEnabled[1],
                'status': statusAndEnabled[0],
                'rules': cameraRules,
                'liveH264Uri': liveH264Uri,
                'liveJpegUri': liveJpegUri
            }

            if self._camMgr.isCameraFrozen(cameraName):
                camerasDict['name'] = cameraName + kInactiveSuffix
                camerasDict['frozen'] = True

            return True, camerasDict
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError


    ###########################################################
    def _remoteGetAllCamerasDetailsAndRules(self, sessionId):
        """Retrieve a list of details and rules for all cameras.

        @return success   True if the operation was successful.  If not
                          successful, the only additional return will be a
                          string explaining the error.
        @return cameras  A list of dicts contains camera details and rules
        """
        try:
            # Cameras that are currently setup; may or may not have recorded
            # video. Some might be frozen though.
            camLocs = self._camMgr.getCameraLocations()

            # A list of cameras that have recorded video; may or may not be
            # currently setup...
            clipCams = self._clipMgr.getCameraLocations()

            camerasDict = {};

            for camLoc in camLocs:
                statusAndEnabled = self._getCameraStatusAndEnabled(camLoc)
                liveJpegUri = self._remoteGetCameraUri(camLoc, sessionId, None, _kMimeTypeJpeg)[1]
                liveH264Uri = self._remoteGetCameraUri(camLoc, sessionId, None, _kMimeTypeVideoH264)[1]
                allRules = self._getRuleInfoForLocation(camLoc)
                cameraRules = []
                if len(allRules):
                    for rule in allRules:
                        cameraRules.append({'name': rule[0], 'schedule': rule[2], 'enabled': rule[3]})

                camerasDict[camLoc] = {
                    'name': camLoc,
                    'active': True,
                    'enabled': statusAndEnabled[1],
                    'status': statusAndEnabled[0],
                    'rules': cameraRules,
                    'liveH264Uri': liveH264Uri,
                    'liveJpegUri': liveJpegUri
                }

                if self._camMgr.isCameraFrozen(camLoc):
                    camerasDict[camLoc]['name'] = camLoc + kInactiveSuffix
                    camerasDict[camLoc]['frozen'] = True

            # Get inactive cameras out, ignoring imported ones...
            for camName in clipCams:
                if not camName.endswith(kImportSuffix) and camName not in list(camerasDict.keys()):
                    allClipCamRules = self._getRuleInfoForLocation(camName)
                    clipCamRules = []
                    if len(allClipCamRules):
                        for clipRule in allClipCamRules:
                            clipCamRules.append({'name': clipRule[0], 'schedule': clipRule[2], 'enabled': clipRule[3]})

                    camerasDict[camName] = {
                        'name': camName + kInactiveSuffix,
                        'active': False,
                        'rules': clipCamRules
                    }

            return True, list(camerasDict.values());
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError

    ###########################################################
    def _remoteGetBuiltInRules(self, version=None):
        """ Get built-in rules for specific version of SV
        """
        if version is None:
            return []
        return kBuiltInRules.get(version, kBuiltInRules["current"])

    ###########################################################
    def _remoteGetRulesForCamera(self, cameraName):
        """ Legacy version of _remoteGetRulesForCamera2
            Assumes the need to return 5.1-compatible built-in rules
        """
        return self._remoteGetRulesForCamera2(cameraName, "5.1")

    ###########################################################
    def _remoteGetRulesForCamera2(self, cameraName, version=None):
        """Retrieve a list of rule names for a given camera.

        @param  cameraName  The name of the camera to retrieve rules for.
        @param  version     The major.minor version of SV to get built-in rules for
        @return success     True if the operation was successful.  If not
                            successful, the only additional return will be a
                            string explaining the error.
        @return rules       A list of rules for the given camera.
        """
        try:
            cameraName = cameraName.rsplit(kInactiveSuffix)[0]
            if cameraName == kAnyCameraStr:
                cameraName = None

            rules = [info[0] for info in self._getRuleInfoForLocation(cameraName)]
            return True, self._remoteGetBuiltInRules(version) + rules
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError



    ###########################################################
    def _remoteGetDetailedRulesForCamera(self, cameraName):
        """Retrieve a list of dicts with rule name, schedule, and status for given camera.

        @param  cameraName  The name of the camera to retrieve rules for.
        @return success     True if the operation was successful.  If not
                            successful, the only additional return will be a
                            string explaining the error.
        @return rules       A list of dicts with name, schedule and enabled for camera.
        """
        try:
            cameraName = cameraName.rsplit(kInactiveSuffix)[0]
            if cameraName == kAnyCameraStr:
                cameraName = None

            rules = [{'name': info[0], 'schedule': info[2], 'enabled': info[3]} for info in self._getRuleInfoForLocation(cameraName)]
            return True, rules
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError


    ###########################################################
    def _remoteEnableRule(self, ruleName, enable):
        """Enable or disable a given rule.

        @param  ruleName    The name of the rule to turn off or on.
        @param  enable      True if the rule should be enabled.
        @return success     True if the operation was successful.  If not
                            successful, the only additional return will be a
                            string explaining the error.
        """
        self._logger.info("rule: %s, enabled=%s" % (ruleName, enable))
        try:
            self._enableRule( ruleName, enable)
            return True
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError

    ###########################################################
    def _remoteGetClipsForRule(self, cameraName, ruleName, searchTime=0,
                               numClips=25, firstClip=0, oldestFirst=True):
        """ Legacy version, retrieving only the list of objects, without information about those.
            See _remoteGetClipsForRule2 for more information
        """
        return self._remoteGetClipsForRule2(cameraName, ruleName, searchTime,
                                numClips, firstClip, oldestFirst, False)

    ###########################################################
    def _remoteGetClipsForRule2(self, cameraName, ruleName, searchTime=0,
                               numClips=25, firstClip=0, oldestFirst=True, objInfo=True):
        """Retrieve a list of clips for a search.

        @param  cameraName  The camera whose video should be searched.
        @param  ruleName    The rule to use to search.
        @param  searchTime  A time in seconds since unix epoch that occurs on
                            the day to be searched.
        @param  numClips    The maximum number of clips to return.
        @param  firstClip   The clip number to start from.
        @param  oldestFirst If true, 0 indexes the oldest clip.
        @param  objInfo     If true, returns object types as part of object list
        @return success     True if the operation was successful.  If not
                            successful, the only additional return will be a
                            string explaining the error.
        @return clipList    A list of (camName, startTime, stopTime, thumbTime,
                            desc, objList) for each clip found.
        @return numClips    The number of total clips found.
        """
        try:
            searchQuery = None
            totalNumClips = 0

            # Get the query and camera list.
            searchQuery, camList = self._getSearchInfo(cameraName, ruleName)
            if not searchQuery:
                errorStr = "Couldn't load rule " + str(ruleName)
                self._logger.error(errorStr)
                return False, errorStr

            # Get the search date.
            searchDate = datetime.date.fromtimestamp(searchTime)

            # Perform the search.
            clipList = []

            def flushFunc(cam):
                pS, pMs, tS, tMs = self._flushVideo(cam)
                return pS*1000+pMs, tS*1000+tMs

            # preserve old behavior - for now
            searchConfig = SearchConfig()

            _, clips = getSearchResults(searchQuery, camList, searchDate,
                                        self._dataMgr, self._clipMgr, searchConfig, flushFunc)

            # Sort the result list by file start time
            clips.sort(key=operator.attrgetter(OB_ASID('startTime')))

            totalNumClips = len(clips)

            if firstClip >= totalNumClips:
                return True, [], totalNumClips

            if not oldestFirst:
                firstClip = totalNumClips-numClips-firstClip
                if firstClip < 0:
                    numClips += firstClip
                    firstClip = 0

            clips = clips[firstClip:firstClip+numClips]

            if not oldestFirst:
                clips.reverse()

            objects = None
            if objInfo:
                objects = self._getObjectInfoForClips(clips)

            for clipInfo in clips:
                clipList.append(self._getClipInfoFromMatchingObject(clipInfo, objects))

            return True, clipList, totalNumClips #PYCHECKER OK: Return types inconsistent
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError

    ###########################################################
    def _remoteGetClipsForRuleBetweenTimes(self, cameraName, ruleName, startSec,
            endSec, slopSec=0, numClips=25, firstClip=0, oldestFirst=True):
        """ Legacy version, retrieving only the list of objects, without information about those.
            See _remoteGetClipsForRuleBetweenTimes2 for more information
        """
        return self._remoteGetClipsForRuleBetweenTimes2(cameraName, ruleName, startSec,
            endSec, slopSec, numClips, firstClip, oldestFirst, False)

    ###########################################################
    def _remoteGetClipsForRuleBetweenTimes2(self, cameraName, ruleName, startSec,
            endSec, slopSec=0, numClips=25, firstClip=0, oldestFirst=True, objInfo=True):
        """Retrieve a list of clips for a search between the given times.

        Note: To match exactly what the desktop application will display for a
              given day, search from 12:00am to 12:00am the next day with a
              slopSec of 300.

        @param  cameraName  The camera whose video should be searched.
        @param  ruleName    The rule to use to search.
        @param  startSec    A unix epoch time to start the search.
        @param  endSec      A unix epoch time to stop the search.
        @param  slopSec     The amount of time to search before or after for
                            clips that may be ongoing or extend past the
                            startSec and endSec.
        @param  numClips    The maximum number of clips to return.
        @param  firstClip   The clip number to start from.
        @param  oldestFirst If true, 0 indexes the oldest clip.
        @param  objInfo     If true, returns object types as part of object list
        @return success     True if the operation was successful.  If not
                            successful, the only additional return will be a
                            string explaining the error.
        @return clipList    A list of (camName, startTime, stopTime, thumbTime,
                            desc, objList) for each clip found.
        @return numClips    The number of total clips found.
        """
        try:
            searchQuery = None
            totalNumClips = 0

            # Get the query and camera list.
            searchQuery, camList = self._getSearchInfo(cameraName, ruleName)
            if not searchQuery:
                errorStr = "Couldn't load rule " + str(ruleName)
                self._logger.error(errorStr)
                return False, errorStr

            # Perform the search.
            clipList = []

            def flushFunc(cam):
                pS, pMs, tS, tMs = self._flushVideo(cam)
                return pS*1000+pMs, tS*1000+tMs

            # preserve old behavior - for now
            searchConfig = SearchConfig()

            _, clips = getSearchResultsBetweenTimes(searchQuery, camList,
                    startSec*1000, endSec*1000, slopSec*1000, self._dataMgr,
                    self._clipMgr, searchConfig, flushFunc)

            # Sort the result list by file start time
            clips.sort(key=operator.attrgetter(OB_ASID('startTime')))

            totalNumClips = len(clips)

            if firstClip >= totalNumClips:
                return True, [], totalNumClips

            if not oldestFirst:
                firstClip = totalNumClips-numClips-firstClip
                if firstClip < 0:
                    numClips += firstClip
                    firstClip = 0

            clips = clips[firstClip:firstClip+numClips]

            if not oldestFirst:
                clips.reverse()

            objects = None
            if objInfo:
                objects = self._getObjectInfoForClips(clips)

            for clipInfo in clips:
                clipList.append(self._getClipInfoFromMatchingObject(clipInfo, objects))

            return True, clipList, totalNumClips #PYCHECKER OK: Return types inconsistent
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError


    ###########################################################
    def _getObjectInfoForClips(self, clips):
        """ Retrieve information about objects making appearance in the set of clips
        """
        if clips is None or len(clips) == 0:
            return None

        minId = -1
        maxId = -1
        for clipInfo in clips:
            for objId in clipInfo.objList:
                if minId < 0 or objId < minId:
                    minId = objId
                if maxId < 0 or objId > maxId:
                    maxId = objId
        return self._dataMgr.getObjectsInfoForRange(minId, maxId)

    ###########################################################
    def _remoteGetThumbnailUris(self, thumbInfoList, mimeType=_kMimeTypeJpeg,
            extras=None):
        """Retrieve a list of thumbnail URIs.

        @param  thumbInfoList  A list of (camName, thumbMs) for which to return
                               thumbnail URIs.
        @param  mimeType       The mime type. Currently must be "image/jpeg".
        @param  extras         An optional dictionary of extra parameters:
                               'maxSize' - a (w,h) of the desired max size.
        @return success        True if the operation was successful.  If not
                               successful, the only additional return will be a
                               string explaining the error.
        @return uriList        A list of thumbnail URIs.
        """
        if mimeType != _kMimeTypeJpeg:
            return False, "Invalid mime type. Try " + _kMimeTypeJpeg

        extras = extras or {}
        uriList = []
        saveDir = os.path.join(self._localDataDir, kRemoteFolder)

        if not os.path.isdir(saveDir):
            try:
                os.makedirs(saveDir)
            except OSError:
                pass

        for cam, msPair in thumbInfoList:
            ms = msPair[0]*1000+msPair[1]
            jpeg = cam + str(ms) + ".jpg"
            savePath = os.path.abspath(os.path.join(saveDir, jpeg))
            self._dataMgr.makeThumbnail(cam, ms, savePath,
                    extras.get('maxSize', (-1, -1)))

            # Construct a URI for the clip.
            uriList.append("/%s/%s" % (kRemoteFolder, jpeg))

        return True, uriList


    ###########################################################
    def _remoteGetClipInfo(self, cameraName, startTime, stopTime):
        """Retrieve the information needed to play a clip.

        @param  cameraName   The name of the camera to retrieve a clip from.
        @param  startTime    A (startSecond, startMs) of the absolute ms the
                             clip begins from.
        @param  stopTime     A (stopSecond, stopMs) of the absolute ms the
                             clip stops at.
        @return success      True if the operation was successful.  If not
                             successful, the only additional return will be a
                             string explaining the error.
        @return fileList     An ordered list of (fileName, offset) that comprise
                             the clip, where offset is the ms distance of the
                             start of a file from the start of the first file.
        @return startOffset  Offset in ms to begin playing at in the first file.
        @return stopOffset   Offset in ms to stop playing at in the last file.
        """
        try:
            fileList = []
            startOffset = 0
            stopOffset = 0

            startTime = startTime[0]*1000+startTime[1]
            stopTime = stopTime[0]*1000+stopTime[1]

            curFile = self._clipMgr.getFileAt(cameraName, startTime,
                                              stopTime-startTime)
            if not curFile:
                return False, _kCouldNotLoadClip

            videoDir = os.path.join(self._getVideoLocation(), kVideoFolder)

            fileList = [(os.path.join(videoDir, curFile), 0)]

            firstStart, lastStop = self._clipMgr.getFileTimeInformation(curFile)
            curStart = firstStart
            while lastStop < stopTime:
                curFile = self._clipMgr.getNextFile(curFile)
                if not curFile:
                    break
                curStart, curStop = self._clipMgr.getFileTimeInformation(curFile)
                if curStart == -1:
                    break

                lastStop = curStop
                fileList.append((os.path.join(videoDir, curFile),
                                 curStart-firstStart))

            stopTime = min(stopTime, lastStop)
            startTime = max(startTime, firstStart)

            startOffset = startTime-firstStart
            stopOffset = stopTime-curStart

            return True, fileList, startOffset, stopOffset #PYCHECKER OK: Function return types are inconsistent
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError


    ###########################################################
    def _remoteGetLiveCameras(self):
        """Retrieve currently configured cameras.

        @return success  True if the operation was successful.  If not
                         successful, the only additional return will be a
                         string explaining the error.
        @return camList  A list of (camName, <unused>) pairs.
        """
        try:
            names = self._camMgr.getCameraLocations()
            names.sort(key=lambda x: x.lower())
            settings = []
            for name in names:
                # This is legacy - we don't want to be sending these outside the network.
                settings.append((name, ''))

            return True, settings
        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError


    ###########################################################
    def _remoteGetNotificationClip(self, cameraName, ruleName, ms):
        """Retrieve the clip corresponding to a push notification.

        @param  cameraName The camera name included in the push.
        @param  ruleName   The rule name included in the push.
        @param  ms         The ms as a (sec, ms) tuple included in the push.
        @return clipInfo   A (camName, startTime, stopTime, thumbTime,
                            descLine1, descLine2) tuple for the clip.
        """
        try:
            ms = ms[0]*1000+ms[1]
            query, camList = self._getSearchInfo(cameraName, ruleName)
            if not query:
                errorStr = "Couldn't load rule " + str(ruleName)
                self._logger.error(errorStr)
                return False, errorStr

            startMs = ms-_kNotifClipRewindMs
            endMs = ms+_kNotifClipExtendMs

            def flushFunc(cam):
                pS, pMs, tS, tMs = self._flushVideo(cam)
                return pS*1000+pMs, tS*1000+tMs

            # For notifications, we don't want to merge clips (so to preserve 1:1 clip-to-notification ratio)
            # TODO: is that always true?
            searchConfig = SearchConfig()
            searchConfig.disableClipMerging()

            _, clips = getSearchResultsBetweenTimes(query, camList,
                    startMs, endMs, 0, self._dataMgr, self._clipMgr, searchConfig, flushFunc)

            # Sort the result list by file start time
            clips.sort(key=operator.attrgetter(OB_ASID('startTime')))

            for clipInfo in clips:
                if ms in clipInfo.startList:
                    return True, self._getClipInfoFromMatchingObject(clipInfo)

        except Exception:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError

        self._logger.error("get notification clip found no match: " +
                str((cameraName, ruleName, ms)))
        return False, None


    ###########################################################
    def _getClipInfoFromMatchingObject(self, matchingObject, objTypeMap=None):
        """Extract returnable info from a MatchingClipInfo object.

        @param  matchingObject The MatchingClipInfo object to convert.
        @param  objTypeMap     A mapping of objectIds to object types.
        @reutrn infoTuple      A tuple of (camName, startTime, stopTime,
                               thumbTime, description, objectInfo), where objectInfo
                               is a list of objectIds if objTypeMap is None, or
                               a list of (objId, objType) tuples otherwise.
        """
        cam = matchingObject.camLoc
        start = matchingObject.startTime
        stop = matchingObject.stopTime
        tbMs = matchingObject.previewMs

        # Construct the description strings.
        timeStruct = time.localtime(start/1000.)
        desc = formatTime('%I:%M %p', timeStruct).swapcase()
        if desc[0] == '0':
            desc = desc[1:]

        objInfo = None
        if objTypeMap is None:
            objInfo = matchingObject.objList
        else:
            objInfo = [ (x,objTypeMap.get(x,'unknown')) for x in matchingObject.objList ]

        return (cam, (start//1000, start%1000), (stop//1000, stop%1000),
                (tbMs//1000, tbMs%1000), desc, objInfo)


    ###########################################################
    def _getSearchInfo(self, cameraName, ruleName):
        """Retrieve usable information for passing to getSearchResults*

        @param  cameraName The camera whose video should be searched.
        @param  ruleName   The rule to use to search.
        @return query      The searchable query.
        @return camList    A list of cameras to search.
        """
        # Strip the inactive suffix from the camera name.
        cameraName = cameraName.rsplit(kInactiveSuffix)[0]

        searchQuery = getQueryForDefaultRule(self._dataMgr, ruleName)
        if searchQuery is None:
            # Load the requested query.
            query = self._getQuery(ruleName, True)
            if not query:
                return None, []
            searchQuery = query.getUsableQuery(self._dataMgr)
            if cameraName == kAnyCameraStr:
                cameraName = query.getVideoSource().getLocationName()

        camList = [cameraName]
        if cameraName == kAnyCameraStr:
            success, cameraNames = self._remoteGetCameraNames()
            if not success:
                self._logger.error("%s (could not list cameras)" %
                                   _kRemoteGenericError)
                return None, []
            camList = [camName.rsplit(kInactiveSuffix)[0] for camName
                       in cameraNames[1:]]

        return searchQuery, camList

    ###########################################################
    def _remoteGetClipUriForDownload(self, cameraName, startTime, stopTime,
            sessionId, mimeType=_kMimeTypeVideoH264, extras=None):
        """ Generate clip for download (e.g. MP4 file)

            This could have been signaled through mimeType, but we want to cut
            over to HLS for playback without forcing a client upgrade.
            Therefore, the default implementation will ignore MIME type, and generate
            HLS, and this new one will return .MP4 like prev implementation did.
        """
        return self._remoteGetClipUriBase(cameraName, startTime, stopTime,
                        sessionId, False, 0, mimeType, extras)

    ###########################################################
    def _remoteGetClipUri(self, cameraName, startTime, stopTime,
            sessionId, mimeType=_kMimeTypeVideoH264, extras=None):
        """ Generate clip for playback (aka HLS sequence)

            This could have been signaled through mimeType, but we want to cut
            over to HLS for playback without forcing a client upgrade.
            Therefore, this (default) implementation will ignore MIME type, and generate
            HLS, and the newly introduced one will return .MP4 like prev implementation did.
        """
        return self._remoteGetClipUriBase(cameraName, startTime, stopTime,
                        sessionId, True, 10, mimeType, extras)

    ###########################################################
    def _remoteGetClipUriBase(self, cameraName, startTime, stopTime,
            sessionId, useHls, fps, mimeType=_kMimeTypeVideoH264, extras=None):
        """Creates a clip from the given information and returns an access URI.

        @param  cameraName   The name of the camera to retrieve a clip from.
        @param  startTime    A (startSecond, startMs) of the absolute ms the
                             clip begins from.
        @param  stopTime     A (stopSecond, stopMs) of the absolute ms the
                             clip stops at.
        @param  sessionId    A unique identifier for this session.
        @param  useHls       Whether to export to HLS or MP4
        @param  fps          FPS limit, or 0 for none
        @param  mimeType     The mime type. Currently must be "video/h264".
        @param  extras       A dictionary of extra values. Currently supports:
                             objectIds - optional list of object ids that should
                             have tracking boxes displayed for.
                             max_bit_rate - int, attempt to keep the clip bit
                             rate under this value.
                             maxSize - A (width, height) max size request.
        @return success      True if the operation was successful.  If not
                             successful, the only additional return will be a
                             string explaining the error.
        @return clipUri      A URI to be used to access the clip.
        @return realStart    The start time of the clip in epoch milliseconds
        @return realStop     The stop time of the clip in epoch millisecondss
        """
        clipUri = ""
        # We add our own keys below, so work on a copy - never on the caller's
        # dictionary (which for a parameter-less call is the default argument).
        extras = dict(extras or {})
        try:
            if mimeType != _kMimeTypeVideoH264:
                raise Exception("Invalid mime type " + str(mimeType) + ". Try " + _kMimeTypeVideoH264)

            # Load the clip.
            startTime = startTime[0]*1000+startTime[1]
            stopTime = stopTime[0]*1000+stopTime[1]

            # This only sets up the set of filenames and offsets for future call
            # to saveCurrentClip ... make sure the file is opened in the most lightweight
            # matter possible (no thread, no audio, etc)
            resolution = self._prefs.getPref(kClipResolution)
            maxSize = (0, resolution if resolution >= 0 else 0)
            realStart, realStop = self._dataMgr.setupMarkedVideo(cameraName,
                    startTime, stopTime, startTime, extras.get("objectIds", []),
                    maxSize, False, False)
            if realStart == -1 or realStop == -1:
                raise Exception("The requested clip could not be opened.")

            # Export the clip.
            saveDir = os.path.join(self._localDataDir, kRemoteFolder)
            try:
                os.makedirs(saveDir)
            except OSError:
                pass

            use12Hour, useUSDate = self._getTimePreferences()
            enableTimestamps = self._prefs.getPref('clipTimestamps')
            extras["use12HrTime"] = enableTimestamps and use12Hour
            extras["useUSDate"] = enableTimestamps and useUSDate
            extras["format"] = "hls" if useHls else "mp4"

            if kClipQualityProfile not in extras:
                # If clip quality setting is not in extras, override with a default
                extras[kClipQualityProfile] = self._prefs.getPref(kClipQualityProfile)

            # Only set parameters that'd prevent us from remuxing, if configured
            # video quality isn't 'original'
            extras["fps"] = 0
            extras["maxSize"] = (0,0)
            extras["drawBoxes"] = False
            extras["enableTimestamps"] = False
            if extras[kClipQualityProfile] != 0:
                extras["fps"] = fps
                extras["maxSize"] = maxSize
                extras["drawBoxes"] = self._prefs.getPref('clipBoundingBoxes')
                extras["enableTimestamps"] = enableTimestamps


            ext = "m3u8" if useHls else "mp4"
            filename = str(sessionId) + "." + ext
            savePath = os.path.join(saveDir, filename)
            success = self._dataMgr.saveCurrentClip(savePath, realStart,
                    realStop, self._localDataDir, extras)
            if not success:
                raise Exception("Clip could not be created.")

            # Construct a URI for the clip.
            clipUri = "/%s/%s" % (kRemoteFolder, filename)

        except Exception as e:
            self._logger.error("Remote exception", exc_info=True)
            return False, _kRemoteExceptionError + ": " + str(e)

        return True, clipUri, (realStart//1000, realStart%1000),\
                            (realStop//1000, realStop%1000)


    ###########################################################
    def _remoteGetCameraUri(self, cameraName, sessionId, options,
            mimeType=_kMimeTypeJpeg):
        """ Gets the Uri for either live JPEG images or HLS video streams
        (via M3U8 descriptors).

        To fetch the JPEGs from a camera you might want to consider using a
        simple state machine. The states are

        1. NOT_AVAILABLE - this is the initial state and the one you transition
        back to if the _remoteGetCameraUri() returns an empty URI (meaning the
        camera's not able to serve anything at the moment (because it's turned
        off or just starting up, etc). By repeatedly calling
        _remoteGetCameraUri() you should then be able to transition to ACTIVE
        eventually.

        2. ACTIVE - entered if remoteGetCameraUri() gave back a URL. This URL
        is used to fetch JPEGs. The frequency of polling is up to the client,
        meaning it could max out the camera server if it wanted to (and had the
        bandwidth). Hence use it with caution and delay if necessary,
        preferably with a fixed timer to avoid jitter. If the connection seems
        to be slow the client might transition back into NOT_AVAILABLE and get
        a new URL using a smaller image dimension (width/height parameter) to
        create smaller images and then use upscaling in the browser.

        3. ERROR - if a JPEG image could not be loaded for whatever reason.
        This could be because of a connection hiccup or because the camera
        server got shut down, crashed or reconfigured. Usually latter should be
        the case, so it's recommended to retry in this state for a few times,
        and only then transition back to NOT_AVAILABLE to check for a
        potentially new URL.

        The same applies also for the M3U8 streaming in principal, yet the lack
        of getting knowing about a player error or the missing width/height
        feature pretty much reduces it to get-the-url-and-start-the-player at
        this moment.

        @param  cameraName   The name of the camera to retrieve a clip from.
        @param  sessionId    A unique identifier for this session.
        @param  options      Optional parameters as a struct (dictionary).
                             Supported right now are "width" and "height"
                             (integers) to get the media scaled to custom
                             dimensions. Aspect ratio gets preserved, and
                             scaling up is prohibited.
        @param  mimeType     The MIME type. Currently must be "image/jpeg" or
                             "video/h264".
        @return success      True if the operation was successful.  If not
                             successful, the only additional return will be a
                             string explaining the error.
        @return camUri       A URI to be used to access the camera's media. An
                             empty string means that the camera is unknown or
                             media access is momentarily unavailable. The client
                             might retry the request.
        """
        camUri = ""
        _, _, isEnabled, _ = self._camMgr.getCameraSettings(cameraName)
        cameraStatus = self._cameraStatus.get(cameraName, (None, None))[0]
        cameraWsgiPort = self._cameraWsgiPort.get(cameraName, None)
        queryString = ""
        if options is not None:
            queryString = urllib.parse.urlencode(options)
            if 0 < len(queryString):
                queryString = "?" + queryString
        if cameraStatus == kCameraConnecting and cameraWsgiPort is not None:
            if mimeType == _kMimeTypeVideoH264:
                # The client we point to a virtual M3U8 file, which is an HTTP
                # request handler in the camera process' WSGI server, doing the
                # actual file read and also modifying it to point to the
                # right MP4 pieces. The camera process manages the lifetime of
                # the HLS generation, meaning if the client or its embedded
                # player respectively stops requesting the M3U8 file generation
                # will cease.
                fileName = simplifyString(cameraName) + ".m3u8" + queryString
            elif mimeType == _kMimeTypeJpeg:
                # Same deal for JPEG fetching, however here there is no lifetime
                # management in the camera process at all. Every JPEG load
                # attempt is individual.
                fileName = simplifyString(cameraName) + ".jpg" + queryString
            else:
                return False, "invalid MIME type"
            camUri = _kLivePathFormat % fileName
        else:
            if isEnabled and cameraStatus != kCameraOff:
                self._logger.warning("no media (camera status: %s, WSGI port: %s)"
                                     % (cameraStatus, cameraWsgiPort));
        self._logger.debug("camera URI is '%s'" % camUri);
        return True, camUri

    ###########################################################
    def _callNotificationGateway(self, params, login=True, resetOn403=True):
        """ Generic, internal HTTPS request maker to the gateway.

        @param  params     The data to POST to the gateway.
        @param  login      True if this call should be authenticated.
        @param  resetOn403 True if the credentials should be reset (new signup)
                           in case the server returns a 403, which means
                           authentication failure and usually indicates that the
                           account information got lost/expired/etc.
        @return success    True if the call was successful
        @return body       The body of the response on success, an error string
                           on error.
        """
        resetAttempts = 0
        while True:
            settings = self._prefs.getPref("notificationSettings")
            if login:
                guid        = settings.get('gatewayGUID', None)
                password    = settings.get('gatewayPassword', None)
                if not guid or not password:
                    return False, "gateway account not available"
                params['guid']      = guid
                params['password']  = password

            params[ 'svversionstring' ] = kVersionString

            headers = { 'Content-type': 'application/x-www-form-urlencoded',
                        'Accept': 'text/plain' }
            self._logger.info( "NMS.callNotificationGateway: calling notification gateway params=%s" % ( str( params )))
            hc      = HttpClient(kGatewayTimeoutSecs, self._logger)
            url     = "https://%s%s" % (kGatewayHost, kGatewayPath)
            payload = urllib.parse.urlencode(params)
            status, body, _ = hc.post(url, payload, headers)
            status = 500 if status is None else status
            self._logger.info("response %d: %s" % (status, body))
            if resetOn403 and 403 == status and 0 == resetAttempts:
                resetAttempts += 1
                # NOTE: this is a serious condition, we're going to loose
                #       all of the device associations; however we also
                #       count on device registration calls to actually fix
                #       a situation of an account loss, since this will be
                #       the only way for recovery - the response runner in
                #       comparsion or its potential bouncing off the server
                #       due to bad credentials will not trigger a reset,
                #       mainly because we cannot easily share the
                #       preferences but also naturally because if device
                #       registrations are gone there's nothing to send
                #       notifications to anyway ...
                self._logger.warning("resetting gateway credentials ...")
                reset, err = self._enableNotifications(True, True)
                if reset:
                    self._logger.info("reset successful, trying again...")
                    continue
                self._logger.error("reset failed (%s), giving up..." % err)
                return False, err
            success = 200 == status
            return success, body

    ###########################################################
    def _enableNotifications(self, enable, force=False):
        """ Enables notifications, which is just a configuration flag. However
        if we haven't signed up at the gateway yet it will happen right then.

        @param  enable    True to enable notifications, False to disable.
        @param  force     True to force a signup. If it succeeds the old account
                          on the gateway will be abandoned and with it former
                          device registrations will be lost. We don't support
                          transferring devices from an old to a new account.
                          Forcing a new account should be thus considered just
                          a recovery method if somehow the old account vanished,
                          which would indicate a severe problem on the server
                          (and thus is more a debugging tool).
        @return  success  True if the operation succeeded.
        @return  data     The user GUID on success, otherwise an error message.
        """
        settings = self._prefs.getPref("notificationSettings")
        if settings is None or not settings['gatewayGUID'] or force:
            self._logger.info("signing up at gateway...")
            name = "user%s" % uuid.uuid4().hex
            cset = string.ascii_letters + string.digits
            # This is a credential, so it comes from the OS CSPRNG.
            password = "".join(secrets.choice(cset) for _ in range(12))
            params = { 'action' : 'signup',
                       'name' : name,
                       'password' : password }
            success, data = self._callNotificationGateway(params, login=False,
                                                          resetOn403=False)
            if not success:
                return False, data

            data = urllib.parse.parse_qs(data)
            guids = data.get('guid', None);
            if guids is None or 1 != len(guids):
                return False, "gateway did not return (a single) GUID!?"
            guid = guids[0]
            self._logger.info("created new account (GUID=%s, name=%s)" %
                              (guid, name))
            settings = { 'gatewayGUID'    : guid,
                         'gatewayPassword': password,
                         'gatewayUserName': name }
            self._queue.put([MessageIds.msgIdSetNotificationSettings, settings])
        else:
            guid = settings['gatewayGUID']
        settings['enabled'] = enable
        self._prefs.setPref('notificationSettings', settings)
        return True, guid

    ###########################################################
    def _remoteRegisterDevice(self, deviceID, deviceType, hardwareID):
        """ To register a device for notifications.

        @param  deviceID    The device identifier. Individual per app instance.
        @param  deviceType  Number 1 for iOS, 3 for Android.
        @param  hardwareID  Unique ID per physical device.
        @return success     True if the operation succeeded.
        @return status      Printable status/error message.
        """
        params = { 'action' : 'registerDevice',
                   'deviceID': deviceID,
                   'deviceType': str(deviceType),
                   'hardwareID': hardwareID }
        self._logger.info( "NMS.remoteRegisterDevice: params=%s" % ( str( params )) )
        return self._callNotificationGateway( params )

    ###########################################################
    # This is for apps that no longer use Pushwoosh
    def _remoteRegisterDevice2( self, deviceID, deviceType, hardwareID ) :
        """ To register a device for notifications.

        @param  deviceID    The device identifier. Individual per app instance.
        @param  deviceType  Number 1 for iOS, 3 for Android.
        @param  hardwareID  Unique ID per physical device.
        @return success     True if the operation succeeded.
        @return status      Printable status/error message.
        """
        params  = { 'action' : 'registerDevice',
                    'deviceID' : deviceID,
                    'deviceType' : str( deviceType ),
                    'hardwareID' : hardwareID,
                    'notifyVersion' : _kNotifyVersion_2_NoPushwoosh }
        self._logger.info( "NMS.remoteRegisterDevice2: params=%s" % (str( params )))
        return self._callNotificationGateway( params )


    ###########################################################
    def _remoteUnregisterDevice(self, hardwareID):
        """ To unregister a device for notifications.

        @param  hardwareID  Unique ID of the (physical) device.
        @return success     True if the operation succeeded.
        @return status      Printable status/error message.
        """
        params = { 'action' : 'unregisterDevice',
                   'hardwareID': hardwareID }
        self._logger.info( "NMS.remoteUnregisterDevice: params=%s" % ( str( params )))
        return self._callNotificationGateway( params )

    ###########################################################
    # This is for apps that no longer use Pushwoosh
    def _remoteUnregisterDevice2( self, hardwareID ) :
        """ To unregister a device for notifications.

        @param  hardwareID  Unique ID of the (physical) device.
        @return success     True if the operation succeeded.
        @return status      Printable status/error message.
        """
        params  = { 'action' : 'unregisterDevice',
                    'hardwareID' : hardwareID,
                    'notifyVersion' : _kNotifyVersion_2_NoPushwoosh }
        self._logger.info( "NMS.remoteUnregisterDevice2: params=%s" % (str( params )) )
        return self._callNotificationGateway( params )


    ###########################################################
    def _remoteGetNotifications(self, lastUID, limit):
        """To get a list of push notifications.

        @param  lastUID  The identifier of the last push notification seen by
                         the client. Everything above will be returned.
        @param  limit    Maximum number of items to return. The returned list is
                         in ascending order (lowest UID first), thus the call
                         needs to be repeated to fetch everything if the
                         returned list is the same size as this limit.
        @return          List of notifications (uid,content,data). UID is an
                         integer, content a simple string to display and data a
                         JSON document (as it would have been received over the
                         real notification pipeline).
        """
        result = self._responseDb.getPushNotifications(lastUID, limit)
        return result

    ###########################################################
    def _remoteGetLastNotificationUID(self):
        """To get the identifier of the last notification stored by the system.
        This allows clients which have not gotten a single notification yet to
        synchronize themselves and from then on be able to poll for new things.

        @return The latest UID, or -1 if there are no notifications stored at
                all. In such a case the client should use 0 for the next poll.
        """
        result = self._responseDb.getLastPushNotificationUID()
        return result


    ###########################################################
    def _sendCorruptDbMessage(self):
        """Notify the back end of a corrupt database.

        NOTE: This function is thread safe. Ensure any changes preserve that.
        """
        self._queue.put([MessageIds.msgIdDatabaseCorrupt])


    ###########################################################
    def _setWebPort(self, newPort):
        """Specify the port number to use for the web server.

        @param  newPort  The updated port number.
        """
        newPort = int(newPort)
        self._prefs.setPref('webPort', newPort)
        self._queue.put([MessageIds.msgIdWebServerSetPort, newPort])


    ###########################################################
    def _getWebPort(self):
        """Gets the currently configured port number for the web server.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return The web server port.
        """
        return self._prefs.getPref('webPort')


    ###########################################################
    def _getVideoSetting(self, name):
        """Retrieve a named video setting

        @param  name         Name of the setting
        @return value        Value of the setting
        """
        return self._prefs.getPref(name)


    ###########################################################
    def _setVideoSetting(self, name, value):
        """Set a named video setting

        @param  name         Name of the setting
        @param  value        Value of the setting
        """
        self._prefs.setPref(name, value)
        self._queue.put([MessageIds.msgIdSetVideoSetting, name, value])

    ###########################################################
    def _getTimestampEnabledForClips(self):
        """Retrieve the remote access setting

        @return value        value of the setting
        """
        return self._prefs.getPref('clipTimestamps')


    ###########################################################
    def _setTimestampEnabledForClips(self, val):
        """Set the remote access setting.

        @param  val          Value of the setting
        """
        self._prefs.setPref('clipTimestamps', val)


    ###########################################################
    def _getBoundingBoxesEnabledForClips(self):
        """Retrieve the remote access setting

        @return value        value of the setting
        """
        return self._prefs.getPref('clipBoundingBoxes')


    ###########################################################
    def _setBoundingBoxesEnabledForClips(self, val):
        """Set the remote access setting.

        @param  val          Value of the setting
        """
        self._prefs.setPref('clipBoundingBoxes', val)



    ###########################################################
    def _setWebAuth(self, user, passw):
        """Specify the user name and password for web server access.

        @param user  The user name.
        @param passw The password.
        """
        realm = REALM if _kUseDigestAuth else None
        auth = make_auth(user, passw, realm)
        self._prefs.setPref('webAuth', auth)
        self._queue.put([MessageIds.msgIdWebServerSetAuth, auth])


    ###########################################################
    def _getWebUser(self):
        """Gets the currently configured user name for the web server.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return The user name.
        """
        return user_from_auth(self._prefs.getPref('webAuth'))


    ###########################################################
    def _enablePortOpener(self, enabled):
        """Enables or disables the port opener.

        @param enabled True to enable the opener, false to disable.
        """
        enabled = bool(enabled)
        self._prefs.setPref('portOpenerEnabled', enabled)
        self._queue.put([MessageIds.msgIdWebServerEnablePortOpener, enabled])


    ###########################################################
    def _isPortOpenerEnabled(self):
        """Check if the port opener is enabled.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return True if enabled and (potentially) running. False if not.
        """
        return bool(self._prefs.getPref('portOpenerEnabled'))


    ###########################################################
    def _getBaselinePeople(self):
        """List the people already enrolled in the face baseline.

        NOTE: Thread-safe (read-only directory listing).

        @return  A list of {'folder', 'name'} dicts.
        """
        from backEnd.FaceEnrollment import listBaselinePeople
        return listBaselinePeople()


    ###########################################################
    def _resolveClipFor(self, cameraName, absMs):
        """Resolve the recorded clip covering cameraName @ absMs.

        @return (absPath, firstMs, None) on success,
                (None, None, errorDict) on failure.
        """
        absMs = int(absMs)
        curFile = self._clipMgr.getFileAt(cameraName, absMs, tolerance=None)
        if not curFile:
            return None, None, {"error": "no recorded clip covers this moment"}
        firstMs, lastMs = self._clipMgr.getFileTimeInformation(curFile)
        if firstMs is None or firstMs == -1:
            return None, None, {"error": "could not locate the clip on disk"}
        videoDir = os.path.join(self._getVideoLocation(), kVideoFolder)
        return os.path.join(videoDir, curFile), firstMs, None


    ###########################################################
    def _enrollFaceAt(self, cameraName, absMs, name, gender):
        """Worker: enroll a face from the clip covering cameraName @ absMs.

        Resolves the clip on disk, then delegates to the shared FaceEnrollment
        core (same code the LAN record viewer uses): samples frames around the
        moment, re-finds the person via the shared DetectionService, saves the
        best face crops into Baseline/<Person>/ and appends their embeddings
        to known_faces.dat.  Camera processes hot-reload that file, so the new
        face matches within seconds.
        """
        from backEnd.FaceEnrollment import enrollFaceFromClipFile
        absMs = int(absMs)
        absPath, firstMs, err = self._resolveClipFor(cameraName, absMs)
        if err is not None:
            return err
        return enrollFaceFromClipFile(absPath, firstMs, absMs, cameraName,
                                      name, gender, self._logger)


    ###########################################################
    def _enrollFaceFromClip(self, cameraName, absMs, name, gender):
        """Add a face to the baseline from a camera + moment.

        Invoked from the desktop Search window's playback right-click.

        NOTE: Thread-safe by design (registered in _kThreadSafeFunctions) so
        the several-second harvest does NOT hold the dispatch lock and stall
        camera status / live-view RPCs.  All state it touches (clip DBs read
        via ClipManager, the baseline files) tolerates concurrent access.

        @param  cameraName  The camera location.
        @param  absMs       The absolute ms of the moment to enroll around.
        @param  name        Person's display name (existing or new).
        @param  gender      'M' / 'F' / '' (only used when creating a new one).
        @return             A result dict: {'ok': True, 'name', 'folder',
                            'added', 'totalEncodings'} or {'error': ...}.
        """
        try:
            return self._enrollFaceAt(cameraName, absMs, name, gender)
        except Exception:
            self._logger.error("enrollFaceFromClip failed", exc_info=True)
            return {"error": "enrollment failed (see BackEnd log)"}


    ###########################################################
    def _enrollFaceFromObject(self, objId, name, gender):
        """Add a face to the baseline from a specific detection.

        Invoked when the user clicks a face entry in the Search detection
        panel (e.g. "Unknown (F, ~32, det 66%)").  The detection's own camera
        and time span pinpoint the moment, which is more precise than the
        clip midpoint.  Same thread-safety notes as _enrollFaceFromClip.

        @param  objId   The detection's object id.
        @param  name    Person's display name (existing or new).
        @param  gender  'M' / 'F' / '' (only used when creating a new one).
        @return         A result dict (see _enrollFaceFromClip).
        """
        try:
            info = self._dataMgr.getObjectInfo(int(objId))
            if not info:
                return {"error": "this detection is no longer in the database"}
            camLoc, timeStart, timeStop = info
            centerMs = (int(timeStart) + int(timeStop)) // 2
            return self._enrollFaceAt(camLoc, centerMs, name, gender)
        except Exception:
            self._logger.error("enrollFaceFromObject failed", exc_info=True)
            return {"error": "enrollment failed (see BackEnd log)"}


    ###########################################################
    def _faceHarvestStore(self):
        """The in-memory harvest-token store (purges entries > 5 min old)."""
        with self._faceHarvestLock:
            cutoff = time.time() - 300
            for tok in [t for t, v in self._faceHarvests.items()
                        if v[0] < cutoff]:
                del self._faceHarvests[tok]
            return self._faceHarvests


    ###########################################################
    def _harvestFaceAt(self, cameraName, absMs):
        """Worker: harvest quality-gated face candidates for a preview.

        Nothing is persisted; the candidates are held under a token until
        the user confirms via commitFaceHarvest (or 5 minutes pass).

        @return {'ok': True, 'token', 'candidates': [{det, w, h, jpegB64}]}
                or {'error': ...}.
        """
        import base64
        import uuid
        from backEnd.FaceEnrollment import harvestFaceCandidates

        absMs = int(absMs)
        absPath, firstMs, err = self._resolveClipFor(cameraName, absMs)
        if err is not None:
            return err
        result = harvestFaceCandidates(absPath, firstMs, absMs, cameraName,
                                       self._logger)
        if not result.get("ok"):
            return result

        token = uuid.uuid4().hex
        store = self._faceHarvestStore()
        store[token] = (time.time(), cameraName, absMs, result["candidates"])
        return {"ok": True, "token": token,
                "candidates": [
                    {"det": c["det"], "w": c["w"], "h": c["h"],
                     "jpegB64": base64.b64encode(c["jpeg"]).decode('ascii')}
                    for c in result["candidates"]]}


    ###########################################################
    def _harvestFaceFromClip(self, cameraName, absMs):
        """Harvest face-enrollment candidates from a camera + moment.

        NOTE: Thread-safe (registered in _kThreadSafeFunctions) — same
        rationale as enrollFaceFromClip.
        """
        try:
            return self._harvestFaceAt(cameraName, absMs)
        except Exception:
            self._logger.error("harvestFaceFromClip failed", exc_info=True)
            return {"error": "harvest failed (see BackEnd log)"}


    ###########################################################
    def _harvestFaceFromObject(self, objId):
        """Harvest face-enrollment candidates from a specific detection."""
        try:
            info = self._dataMgr.getObjectInfo(int(objId))
            if not info:
                return {"error": "this detection is no longer in the database"}
            camLoc, timeStart, timeStop = info
            centerMs = (int(timeStart) + int(timeStop)) // 2
            return self._harvestFaceAt(camLoc, centerMs)
        except Exception:
            self._logger.error("harvestFaceFromObject failed", exc_info=True)
            return {"error": "harvest failed (see BackEnd log)"}


    ###########################################################
    def _harvestFaceFromUserMedia(self, path, detectionId, analyzedMs):
        """Prepare an Image-view face; reuse the existing preview/commit store.

        Local desktop RPC only (not on the remote allowlist). Read usermedia.db
        through a private read-only connection; never add user files to clipdb.
        """
        import base64
        import uuid
        from backEnd.UserMediaDb import getUserMediaDbPath
        from backEnd.UserMediaFaceEnrollment import harvestUserMediaFace
        try:
            result = harvestUserMediaFace(
                getUserMediaDbPath(self._localDataDir), path,
                detectionId, analyzedMs, self._logger)
            if not result.get("ok"):
                return result
            token = uuid.uuid4().hex
            store = self._faceHarvestStore()
            # Unique source tag prevents different photos enrolled in the same
            # second from overwriting one another's baseline JPEGs.
            with self._faceHarvestLock:
                store[token] = (time.time(), 'image_' + token, int(time.time() * 1000),
                                result["candidates"])
            return {"ok": True, "token": token, "candidates": [
                {"det": c["det"], "w": c["w"], "h": c["h"],
                 "jpegB64": base64.b64encode(c["jpeg"]).decode('ascii')}
                for c in result["candidates"]]}
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}
        except Exception:
            self._logger.error("harvestFaceFromUserMedia failed", exc_info=True)
            return {"error": "Could not prepare this face. Check the back end "
                             "and detection service logs."}


    ###########################################################
    def _commitFaceHarvest(self, token, selectedIndices, name, gender):
        """Enroll the user-confirmed subset of a previous harvest.

        @param  token            Token returned by the harvest call.
        @param  selectedIndices  Indices into the harvest's candidate list.
        @param  name             Person's display name (existing or new).
        @param  gender           'M' / 'F' / '' (new-person folder suffix).
        @return                  Result dict as enrollFaceFromClip.
        """
        from backEnd.FaceEnrollment import commitFaceEnrollment
        try:
            entry = self._faceHarvestStore().pop(token, None)
            if entry is None:
                return {"error": "this face preview has expired — please "
                                 "click the face again"}
            _t, camLoc, centerMs, candidates = entry
            chosen = [candidates[int(i)] for i in selectedIndices
                      if 0 <= int(i) < len(candidates)]
            return commitFaceEnrollment(name, gender, chosen, camLoc,
                                        centerMs, self._logger)
        except Exception:
            self._logger.error("commitFaceHarvest failed", exc_info=True)
            return {"error": "enrollment failed (see BackEnd log)"}


    ###########################################################
    def _rebuildKnownFaces(self):
        """Rebuild known_faces.dat from the Baseline folders (via the
        DetectionService).  Thread-safe by design."""
        from backEnd.FaceEnrollment import rebuildKnownFaces
        try:
            return rebuildKnownFaces(self._logger)
        except Exception:
            self._logger.error("rebuildKnownFaces failed", exc_info=True)
            return {"error": "rebuild failed (see BackEnd log)"}


    ###########################################################
    def _deleteBaselineImages(self, relPaths):
        """Delete baseline images (Baseline-relative paths) + rebuild."""
        from backEnd.FaceEnrollment import deleteBaselineImages
        try:
            return deleteBaselineImages(list(relPaths or []), self._logger)
        except Exception:
            self._logger.error("deleteBaselineImages failed", exc_info=True)
            return {"error": "delete failed (see BackEnd log)"}


    ###########################################################
    def _moveBaselineImages(self, relPaths, targetFolder):
        """Move baseline images to another person's folder + rebuild."""
        from backEnd.FaceEnrollment import moveBaselineImages
        try:
            return moveBaselineImages(list(relPaths or []), targetFolder,
                                      self._logger)
        except Exception:
            self._logger.error("moveBaselineImages failed", exc_info=True)
            return {"error": "move failed (see BackEnd log)"}


    ###########################################################
    def _renameBaselinePerson(self, folder, newName):
        """Rename a baseline person (folder) + rebuild."""
        from backEnd.FaceEnrollment import renameBaselinePerson
        try:
            return renameBaselinePerson(folder, newName, self._logger)
        except Exception:
            self._logger.error("renameBaselinePerson failed", exc_info=True)
            return {"error": "rename failed (see BackEnd log)"}


    ###########################################################
    def _deleteBaselinePerson(self, folder):
        """Delete a baseline person (folder + images) + rebuild."""
        from backEnd.FaceEnrollment import deleteBaselinePerson
        try:
            return deleteBaselinePerson(folder, self._logger)
        except Exception:
            self._logger.error("deleteBaselinePerson failed", exc_info=True)
            return {"error": "delete failed (see BackEnd log)"}


    ###########################################################
    def _memstorePut(self, key, data, ttl):
        """ Puts data in the in-memory store.

        @param key The key (string) under which to store the data.
        @param data The data to store.
        @param ttl Expiration time, in seconds. -1 to turn off expiration.
        @return The version number. If the item wasn't stored before (or has
        expired) it will be zero. 0+ for any subsequent update.
        """
        return self._memstore.put(key, data, ttl)


    ###########################################################
    def _memstoreGet(self, key, timeout=0, oldVersion=-1):
        """ Gets data in the in-memory store.

        @param key The key (string) to look up the data.
        @param timeout How long to wait for the item to appear or to be updated.
        The value is in seconds, the function will block up to that time in
        the worst case. Set it to zero to turn off any waiting.
        @param oldVersion Former version number to see what would qualitfy as
        an update. Set this to -1 to get the current value unconditionally.
        @return Tuple (data, version), or None if not found/expired/updated.
        """

        try:
            item = self._memstore.get(key, timeout, oldVersion)
            return (item[2], item[0])
        except:
            return None


    ###########################################################
    def _memstoreRemove(self, key):
        """ Deletes data in the in-memory store.

        @param key The key (string) for which to delete data.
        @return True if delete, False if not found.
        """
        return self._memstore.remove(key) is not None


    ###########################################################
    def _userLogin(self, user, password):
        """ Initiate a new login attempt by user-provided credentials.

        @param user The user name.
        @param password The password.
        @return Identifier to return the result via the memstore.
        """
        opid = uuid.uuid4().hex
        self._queue.put([MessageIds.msgIdUserLogin, opid, user, password])
        return opid


    ###########################################################
    def _userLogout(self):
        """Logs out the current user, reverts license to starter."""
        self._queue.put([MessageIds.msgIdUserLogout])


    ###########################################################
    def _refreshLicenseList(self):
        """ Starts a request to refresh a list of all available licenses.

        @return Identifier to return the result via the memstore.
        """
        opid = uuid.uuid4().hex
        self._queue.put([MessageIds.msgIdRefreshLicenseList, opid])
        return opid


    ###########################################################
    def _acquireLicense(self, serial):
        """ Initiates the acquisition of a license.

        @param serial The serial number of the license.
        @return Identifier to return the result via the memstore.
        """
        opid = uuid.uuid4().hex
        self._queue.put([MessageIds.msgIdAcquireLicense, opid, serial])
        return opid


    ###########################################################
    def _unlinkLicense(self):
        """ Unlinks the current license, goes back to starter.

        @return Identifier to return the result via the memstore.
        """
        opid = uuid.uuid4().hex
        self._queue.put([MessageIds.msgIdUnlinkLicense, opid])
        return opid


    ###########################################################
    def _getLicenseSettings(self):
        """ Gets the license (manager) settings.

        @return Dictionary with the settings.
        """
        return self._prefs.getPref('licenseSettings')


    ###########################################################
    def _setLicenseSettings(self, licenseSettings):
        """ Saves the license (manager) settings.

        @param licenseSettings Dictionary with the settings.
        """
        self._prefs.setPref('licenseSettings', licenseSettings)

    ###########################################################
    def _remoteSubmitClipToSighthound(self, camLocation, note, startTime, stopTime):
        """ Queues the upload of a clip defined by the set of parameters to Sighthound for analysis
        @param  cameraName   The name of the camera to retrieve a clip from.
        @param  note         Optional text to accompany the video
        @param  startTime    A (startSecond, startMs) of the absolute ms the
                             clip begins from.
        @param  stopTime     A (stopSecond, stopMs) of the absolute ms the
                             clip stops at.
        """
        realStartTime = startTime[0]*1000 + startTime[1]
        realStopTime = stopTime[0]*1000 + stopTime[1]
        duration = realStopTime - realStartTime
        if duration <= 0:
            self._logger.error("Invalid time interval specified for the clip (%s->%s)" % (str(startTime), str(stopTime)))
            return

        self._queue.put([MessageIds.msgIdSubmitClipToSighthound, camLocation, note, realStartTime, duration])

    ###########################################################
    def _areRulesLocked(self):
        """ To check if the rules are currently locked, due to a backend reset
        caused by a license change.

        @return True if rules are locked and no changes must be made.
        """
        result = self._memstore.get(kMemStoreRulesLock)
        if not result:
            return False
        return result[2]


    ###########################################################
    def _licenseChanged(self, oldLicense, newLicense):
        """ Called if a license change was reported by the backend or the
        license manager respectively. Does the whole system adaption to the new
        situation.

        @param oldLicense Old license dictionary.
        @param newLicense New license dictionary.
        """
        oldEdition = oldLicense[kEditionField]
        newEdition = newLicense[kEditionField]
        oldCameras = oldLicense[kCamerasField]
        newCameras = newLicense[kCamerasField]
        if oldEdition == newEdition and oldCameras == newCameras:
            return

        self._logger.info("license changed from '%s' (%s) to '%s' (%s) ..." %
                          (oldEdition, oldCameras, newEdition, newCameras))
        maxCameras = int(newCameras)
        maxCameras = sys.maxsize if maxCameras < 1 else maxCameras
        self._logger.info("maximum number of cameras is %d" % maxCameras)

        self._camMgr.freezeCameras(maxCameras)
        self._camMgr.logLocations(self._logger)

        # lock the rules, so while the backend does the things below no updates
        # are possible - the backend will then, after the rule reloading
        # set the flag back to False, unconditionally
        self._memstore.put(kMemStoreRulesLock, True)

        # reload the rules and also sync the cameras, meaning they will be
        # temporarily be turned on and off, latter whatever the new camera
        # situation caused of the license allows to happen ...
        camMgrState = self._camMgr.dump()
        self._queue.put([MessageIds.msgIdRuleReloadAll, camMgrState])

        # (re)activate formerly used remote access or turn it off if unpaid for
        if hasPaidEdition(newLicense):
            webPort = self._prefs.getPref('webPort')
        else:
            webPort = -1
        self._logger.info("changing web port to %d ..." % webPort)
        self._queue.put([MessageIds.msgIdWebServerSetPort, webPort])


    ###########################################################
    def _getTimePreferences(self):
        """Retrieve settings related to time display.

        DELIBERATELY FIXED at 24-hour time and ISO (YYYY-MM-DD) dates.  These
        are returned as literals rather than read from prefs because the
        format is a product decision here, not a per-user setting: there is no
        UI that changes it, and the setter that used to write it has been
        removed (it hardcoded the time half anyway and persisted the date half
        to 'datePref', a key nothing has ever read).

        The date value is a TRI-STATE STRING, not a bool -- 'us', 'intl' or
        'iso'.  Every consumer branches on all three and falls through to ISO:
        see vitaToolbox/wx/VitaDatePickerCtrl.useUSDateFormat,
        frontEnd/RuleListPanel._handleTimePrefChange, and
        videoLib2/python/ClipUtils._draw_timestamp (the burned-in overlay).
        Reading it as a truthy/falsy bool is wrong and will silently select US
        format -- that misreading has caused two wrong conclusions already.

        The prefs keys 'timePref12' and 'datePrefUS' still exist and are read
        by BackEndApp for the video-overlay path; they currently hold
        False/False, which yields the same 24-hour ISO result.  Nothing in the
        UI can change them.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @return use12Hour  Always False -- 24-hour time.
        @return useUSDate  Always 'iso' -- see above; a tri-state, not a bool.
        """
        return False, 'iso'


    ###########################################################
    def _sendIftttMessage(self, camera, rule, triggerTime,
                          iftttKey='', iftttEventName=''):
        """Queue an IFTTT Webhooks trigger message.

        NOTE: This function is thread safe. Ensure any changes preserve that.

        @param  camera         The camera name to use for the trigger.
        @param  rule           The rule name to use for the trigger.
        @param  triggerTime    The time in epoch seconds to use for the trigger.
        @param  iftttKey       IFTTT Webhooks key.
        @param  iftttEventName IFTTT event name.
        """
        msg = [MessageIds.msgIdTriggerIfttt, camera, rule, triggerTime,
               iftttKey, iftttEventName]
        self._queue.put(msg)

    ###########################################################
    def _launchedByService(self):
        """ Checks whether the NMS (and thus the backend) got launched by the
        service.

        @return  True if the service launched this instance.
        """
        return self._foundServiceMarkerArg


###############################################################################
class FrontEndMessageQueue:
    """ Threadsafe queue for our messages, with the possibility to have certain
    message types only to appear once in the queue, to avoid things piling up
    when the frontend is not running.
    """

    ###########################################################
    def __init__(self, universalMessageTypes=None):
        """ Constructor.

        @param universalMessageTypes  List of message identifiers for which
                                      not more than one instance should ever
                                      appear in the queue. Set it to None to
                                      make this happen for all types.
        """
        self._list = []
        self._lock = threading.RLock()
        self._universalMessageTypes = universalMessageTypes

    ###########################################################
    def __len__(self):
        """ Returns the number of messages in the queue.

        @return  Message queue length.
        """
        return len(self._list)


    ###########################################################
    def enqueue(self, message):
        """ Adds a message to the .

        @param message  The message to enqueue. Might replace an existing, older
                        one of the same type if it matches the ID list passed
                        in the constructor. The message will take the same
                        spot in the queue where the old message was.
        @return  True if the message was added at the end of the queue, False if
                 it replaced an older instance of the same type.
        """
        self._lock.acquire()
        try:
            if self._universalMessageTypes is None or \
               message[0] in self._universalMessageTypes:
                for i, m in enumerate(self._list):
                    if m[0] == message[0]:
                        self._list[i] = message
                        return False
            self._list.append(message)
            return True
        finally:
            self._lock.release()


    ###########################################################
    def dequeue(self):
        """ Removes the oldest message from the queue.

        @return  The oldest message. Might be not truely the oldest if the slot
                 in the queue got replace by newer messages with the same
                 identifier (if there was a match). An empty list is returned
                 if there is nothing in the queue.
        """
        self._lock.acquire()
        try:
            if 0 == len(self._list):
                return []
            return self._list.pop(0)
        finally:
            self._lock.release()
