#! /usr/local/bin/python

#*****************************************************************************
#
# DiskCleaner.py
#    Core disk maintenance utility.
#    Running as a separate process and performing cleanup of video/image files
#    according to the retention policy.
#    If this module fails to run, or runs too slow, the video storage device will run out of space.
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
import bisect
import datetime
import gc

def _isDateFolder(name):
    try:
        datetime.datetime.strptime(name, '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False

def _secsToFolder(t):
    """A day-folder name for epoch seconds `t`.

    KNOWN MISMATCH, recorded 2026-09-06, not yet fixed: this returns a UTC
    name, but the folders it is compared against are named by LOCAL date
    (StreamReader._registerRemuxSegment slices the date out of ffmpeg's
    local-time segment filename).  For the four hours of local 20:00-23:59 at
    UTC-4 this therefore reports tomorrow as "today".

    Both callers are advisory rather than destructive to footage:
    _canDeleteFolder only governs removal of EMPTY folders (recreated on demand
    by the next segment move), and _shouldProcessDir only decides whether to
    rescan a folder for the thumbnail-size cache.  So the effect is an empty
    current-day folder removed a few hours early, and a slightly stale thumb
    cache, during that window.  Left alone pending a decision rather than
    changed silently.
    """
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime('%Y-%m-%d')

def _enumDateFolders(start_ms, stop_ms):
    """Day folders that may hold thumbnails for [start_ms, stop_ms].

    Used only by _deleteThumbs.  Thumbnails moved to LOCAL day folders on
    2026-09-06 (see QueuedDataManagerCloud._msToFolder), so the local range is
    what new thumbnails use -- but every thumbnail written before that is
    under a UTC name, and at UTC-4 anything recorded between 20:00 and 23:59
    local sits in the FOLLOWING day's folder.  Enumerating only the local
    folders would leave those permanently undeletable: measured 29,351 files,
    440 MB, on this archive at the time of the change.

    So return the union.  It is safe to look in a folder that holds
    thumbnails outside the range, because _deleteThumbs filters every file on
    the timestamp in its own name and keeps the rest; the union only widens
    where it looks, never what it deletes.  A missing folder costs one
    os.path.isdir.  Once the pre-change thumbnails age out the extra folders
    are simply absent.

    NOTE: this is the THUMB folder enumeration.  Clip day folders are LOCAL
    and always have been -- StreamReader._registerRemuxSegment takes the date
    straight from ffmpeg's local-time segment filename.  _secsToFolder in this
    module still computes a UTC name and is compared against those local
    folder names in _canDeleteFolder and _shouldProcessDir; see the note on
    _secsToFolder.
    """
    folders = []
    seen = set()
    for tz in (None, datetime.timezone.utc):
        d = datetime.datetime.fromtimestamp(start_ms / 1000.0, tz).date()
        last = datetime.datetime.fromtimestamp(stop_ms / 1000.0, tz).date()
        while d <= last:
            name = d.strftime('%Y-%m-%d')
            if name not in seen:
                seen.add(name)
                folders.append(name)
            d += datetime.timedelta(days=1)
    return folders
import os
import fnmatch
import pickle
from queue import Empty
from sqlite3 import DatabaseError
import time
import traceback
from bisect import bisect_left

# Common 3rd-party imports...

# Toolbox imports...
from vitaToolbox.path.GetDiskSpaceAvailable import getDiskSpaceAvailable, getDiskUsage
from vitaToolbox.path.PathUtils import normalizePath
from vitaToolbox.path.VolumeUtils import getStorageSizeStr
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.windows.winUtils import registerForForcedQuitEvents
from vitaToolbox.process.ProcessUtils import setProcessPriority, kPriorityLow, checkMemoryLimit
from vitaToolbox.strUtils.EnsureUnicode import ensureUnicode
from vitaToolbox.profiling.MarkTime import TimerLogger

# Local imports...
from appCommon.CommonStrings import kCorruptDbErrorStrings, kMinFreeSysDriveSpaceMB, kThumbsSubfolder, kPrefsFile
from appCommon.DbRecovery import readCorruptionFlag, kCorruptionFlagFile
from appCommon.DbRecovery import kStallFlagSource
from .ClockSync import measureClockOffset
from .ClipManager import kCacheStatusNonCache
from .ClipManager import ClipManager
from .DataManager import DataManager
from .DebugLogManager import DebugLogManager
from . import MessageIds
from videoLib2.python.ClipReader import ClipReader, getMsList, getDuration
import videoLib2.python.ClipUtils as ClipUtils

# Constants...

# We'll delay 20 seconds at startup before we start processing.  That way, if
# back end decides to quit us right away, we'll be responsive.
_kInitialDelaySeconds = 20

_kMaxUnresponsiveTime = 10              # We'll go look for messages after this many seconds.

_kMbToBytes = 1024*1024
_kGbToBytes = 1024*_kMbToBytes

_kMinSpacePerCam = _kGbToBytes
_kTargetFreeSpacePerCam = _kGbToBytes/2
# we'll target _kMinFreeSpaceStayAheadRatio*kMinFreeSysDriveSpaceMB min free drive space
_kMinFreeSpaceStayAheadRatio = 2

_kMinCacheBlockPerCam = _kGbToBytes
_kFirstClipBlockPerCam = _kGbToBytes*2

# Daily summary video (built from the per-camera thumbs).  Checked at most this
# often; each pass encodes at most one (camera, day) to keep CPU bounded.  We
# look back a few completed local days so a day still gets made if the app was
# off overnight.
_kSummaryPeriodSecs = 30 * 60
_kSummaryBackfillDays = 3
_kSummaryFps = 10
# Give up on a (camera, day) after this many failed encodes.  Each pass does at
# most one encode and then returns, so without a cap a single day that cannot
# be encoded is retried first on every pass forever and nothing else is ever
# reached.
_kSummaryMaxAttempts = 3

_kMbToBytesF = 1024.0 * 1024.0

# Windows complains if the total free disk space dips below 200MB, so we'll try
# to keep that much space free.  We go ahead and do it on OSX too since it's
# probably a decent idea in general...
# We'll try and keep a gig free at all times
_kReservedDiskSpace = 1024 * _kMbToBytesF

# Hard low-disk safety net, independent of the configured storage budget:
# external factors (other apps, other drives sharing the volume) can fill the
# video drive even when SV is within its own limit.  Stop recording when the
# ACTUAL free space on the video drive drops below _kCriticalFreePct, and resume
# once it recovers past _kResumeFreePct (hysteresis avoids on/off flapping).
_kCriticalFreePct = 10
_kResumeFreePct = 15

# Clock-sync watchdog.  Every clip and event time is stamped from the PC clock,
# while the cameras burn their own (NTP-synced) clock into the picture -- so a
# drifting PC clock silently makes recorded times disagree with what a camera
# shows, with nothing else looking broken.  Measure against public NTP and warn.
_kClockCheckPeriod = 60*60*24           # Measure once a day (and at startup).
_kClockSkewAlertSecs = 3.0              # Warn past this much error.  Cameras
                                        # display whole seconds, so anything
                                        # under a couple of seconds is unseeable.
_kClockReAlertPeriod = 60*60*24         # Don't nag more often than this...
_kClockWorsenedFactor = 2.0             # ...unless it got materially worse.

_kLogName = "DiskCleaner.log"

_kDatabaseTimeoutSecs = 120

# We'll check for orphaned files to clean up a few times a day.
_kOrphanFileCleanupPeriod = 60*60*24

# A lowercase list of non-video files it's alright to remove
_kRemovableFiles = ['.ds_store', 'thumbs.db']

# If we see a file in the tmp dir longer than this we'll try to remove it.
_kTmpFileLifespan = 60*60*1

# Try to remove remote files that haven't been modified in longer than this.
_kRemoteFileLifespan = 60*5

# Maximum amount of time to keep files in the size cache.
#
# Theoretically this should be infinity, but being cautious about leaking
# memory ATM. Introducing cache to reduce *insanely* frequent re-querying
# of 500k files for user with 30 camera system (case 12238).
#
# Ideally this would be infinite (and we'd persist file size info in the db)
# but for now we just want to ensure we aren't constantly requerying file size
# info during a given cleanup as we were before - _doCleanup is re-entrant for
# a given cycle.
_kFileSizeCacheLifespan = 60*120
# Stagger renewal of file size cache items, to make sure we don't rescan all
# of the files at once (except the first time)
_kMaxFileCacheExpirationsAtOnce = 1000

_kMicrosecInMsec = 1000

# Note that it isn't the same functionality as merging the event-generated clips,
# based on user-specified max time distance.
# This is strictly to avoid fragmenting on-disk, physical clip files.
_kMergeClipThresholdMs = 4000

# Minimum lastMs shortfall worth a WARNING when correcting a new clip's end
# time.  The two sides of that comparison come from DIFFERENT duration
# estimators -- remuxSubClip's return is anchored on ffmpeg's container duration
# (10ms granularity) while getDuration is cv2 frame_count/fps -- so they
# disagree by tens of ms on most clips by construction.  Measured over 4 days:
# 98.6% of corrections are under 1s.  Below this it is rounding, not a fault.
_kLastMsCorrectionWarnMs = 1000

# How often to tidy object table. Doesn't need to happen often.
_kMinTidyObjectTablePeriod = 60*60*24

# Globals...

# Keep a reference to the current instance, for the forced quit callback to use.
_cleaner = None


###############################################################
def runDiskCleaner(backEndQueue, cleanerQueue, clipMgrPath, dataMgrPath, #PYCHECKER OK: Too many arguments
                   numCameras, maxStorage, videoDir, tmpDir, logDir, configDir,
                   remoteDir, infiniteMode, maxCache):
    """Create and start a DiskCleaner process.

    @param  backEndQueue  A queue to add back end messages to.
    @param  cleanerQueue  A queue to listen for control messages on.
    @param  clipMgrPath   A path to the clip manager database.
    @param  dataMgrPath   A path to the data manager database.
    @param  numCameras    The number of cameras being recorded.
    @param  maxStorage    The maximum GB to be used.
    @param  videoDir      The top level directory where videos are stored.
    @param  tmpDir        The top level directory where temporary video
                          files are stored.
    @param  logDir        Directory where log files should be stored
    @param  configDir     Directory to search for config files.
    @param  remoteDir     Directory where remote access files are stored.
    @param  infiniteMode  If True the disk cleaner will pretend it has
                          infinite disk space.
    @param  maxCache      The maximum number of hours of cache to keep.
    """
    global _cleaner
    _cleaner = DiskCleaner(backEndQueue, cleanerQueue, clipMgrPath, dataMgrPath,
                           numCameras, maxStorage, videoDir, tmpDir, logDir,
                           configDir, remoteDir, infiniteMode, maxCache)
    _cleaner.run()
    _cleaner = None


##############################################################################
class DiskCleaner(object):
    """A class for regulating disk space usage."""
    ###########################################################
    def __init__(self, backEndQueue, cleanerQueue, clipMgrPath, dataMgrPath, #PYCHECKER OK: Too many arguments
                 numCameras, maxStorage, videoDir, tmpDir, logDir, configDir,
                 remoteDir, infiniteMode, maxCache):
        """Initialize CameraCapture.

        @param  backEndQueue  A queue to add back end messages to.
        @param  cleanerQueue  A queue to listen for control messages on.
        @param  clipMgrPath   A path to the clip manager database.
        @param  dataMgrPath   A path to the data manager database.
        @param  numCameras    The number of cameras being recorded
        @param  maxStorage    The maximum GB to be used.
        @param  videoDir      The top level directory where videos are stored.
        @param  tmpDir        The top level directory where temporary video
                              files are stored.
        @param  logDir        Directory where log files should be stored
        @param  configDir     Directory to search for config files.
        @param  remoteDir     Directory where remote access files are stored.
        @param  infiniteMode  If True the disk cleaner will pretend it has
                              infinite disk space.
        @param  maxCache      The maximum number of hours of cache to keep.
        """
        # Call the superclass constructor.
        super(DiskCleaner, self).__init__()

        # Setup logging...  SHOULD BE FIRST!
        self._logDir = logDir
        self._logger = getLogger(_kLogName, logDir, 1024*1024*5)
        self._logger.grabStdStreams()

        self._backEndQueue = backEndQueue
        self._commandQueue = cleanerQueue
        # num cameras should never be 0 or all files will be deleted
        self._numCameras = max(1, numCameras)
        self._maxStorage = maxStorage*_kGbToBytes
        self._videoDir = videoDir
        self._tmpDir = tmpDir
        self._configDir = configDir
        self._remoteDir = remoteDir
        self._infiniteMode = infiniteMode
        self._maxCacheDuration = maxCache*60*60*1000

        # A set, not a list: _removeRemoteFiles re-adds every file it failed
        # to delete on each pass, so a list grows without bound until the
        # memory watchdog kills the process.  Also makes the membership test
        # in _removeOrphanFiles O(1) instead of O(n).
        self._pendingDeletes = set()
        self._tmpFileDict = {}
        self._lastOrphanFileCleanup = 0
        self._lastSummaryRun = 0
        # (camera, 'yyyy-mm-dd') -> count of failed encode attempts.
        self._summaryFailures = {}
        # Rate-limits the "database is flagged" notice in _doCleanup.
        self._lastCorruptionWarn = 0
        self._thumbsSize = 0
        self._thumbsCount = 0
        self._thumbsPartial = False
        self._thumbStats = {}
        self._cleanupCycleStartTime = time.time()
        self._cleanupCycleLastInterruptCheckTime = time.time()
        self._lastTidyObjectTableTime = time.time()

        # Whether we've told the back end to stop recording due to critically
        # low free space (drives the hysteresis in _checkCriticalDiskSpace).
        # None until the first check.  The back end's _lowDiskPaused latch is
        # cleared ONLY by a resume message from here, so if a previous instance
        # of this process exited while that latch was set, nothing else will
        # ever clear it -- see _checkCriticalDiskSpace.
        self._lowDiskCritical = None

        # Clock-sync watchdog state (see _checkClockSync).  Zero means "check
        # on the first cleanup cycle", which gives us a free check at startup.
        self._lastClockCheck = 0
        self._lastClockAlert = 0
        self._lastClockAlertOffset = 0.0

        # A dictionary of file size information to avoid frequent expensive
        # getsize calls. Key = file name relative to root storage dir,
        # Value = (filesize, cachedtime)
        self._fileSizeCache = {}

        self._clipMgr = ClipManager(self._logger)
        self._clipMgr.open(clipMgrPath, _kDatabaseTimeoutSecs)

        self._dataMgr = DataManager(self._logger)
        self._dataMgr.open(dataMgrPath, _kDatabaseTimeoutSecs)

        self._logger.info("DiskCleaner initialized, pid: %d" % os.getpid())
        assert type(self._videoDir) == str
        assert type(self._tmpDir) == str
        assert type(self._logDir) == str

        self._debugLogManager = DebugLogManager("DiskCleaner", self._configDir)

        if self._infiniteMode:
            self._logger.warning("Disk cleaning disabled")


    ###########################################################
    def __del__(self):
        """Free resources used by DiskCleaner"""
        self._logger.info("DiskCleaner exiting")


    ###########################################################
    def _markDone(self):
        """Set the running flag to False."""
        self._running = False


    ###########################################################
    def run(self):
        """Run a disk cleaner process."""
        startTime = time.time()

        # In general disk cleanup should be a background event and never pull
        # CPU away from anything else. This can be a particular problem if the
        # the app is closed and not run for several days - on the next launch
        # there will be several days of cache ready to be clipped at once. Set
        # a lower priority to help avoid this.
        setProcessPriority(kPriorityLow)

        # Enter the main loop
        self._running = True
        while(self._running):
            # default timeout; will be changed in some circumstances
            timeout = 1

            # Run the cleanup
            try:
                self._logger.info("Starting cleanup loop")
                timeSinceStart = time.time() - startTime
                if timeSinceStart > _kInitialDelaySeconds:
                    memoryUnderLimit, memStats = checkMemoryLimit(os.getpid())

                    if not memoryUnderLimit:
                        self._logger.error("Quitting disk cleaner process due to excessive memory consumption:" + str(memStats))
                        self._processMessage([MessageIds.msgIdQuit])
                        continue

                    moreToDo = self._doCleanup()
                    # Daily summary videos — isolated so a failure here can
                    # never take down the cleaner's real work.
                    try:
                        self._maybeGenerateSummaries()
                    except Exception:
                        self._logger.error("summary pass failed",
                                           exc_info=True)
                    if not moreToDo:
                        # Collect garbage if we're gonna sleep...
                        gc.collect()
                        timeout = 30
                else:
                    timeout = _kInitialDelaySeconds - timeSinceStart
            except DatabaseError as e:
                if str(e) in kCorruptDbErrorStrings:
                    # If the database is corrupt, notify the back end and exit.
                    self._backEndQueue.put([MessageIds.msgIdDatabaseCorrupt])
                self._logger.error("Database error", exc_info=True)
                return
            except Exception:
                # If we get an uenexpected exception log it and exit.
                self._logger.error("Disk cleaner unexpected exception",
                                   exc_info=True)
                return

            # Process pending messages.  Reading the queue and handling the
            # message are deliberately separate: they used to share one
            # handler, so a failure inside _processMessage was indistinguishable
            # from an empty queue -- the message was dropped with no log, and
            # the rest of the drain abandoned until the next cycle.
            msg = self._getNextMessage(timeout)
            while msg is not None:
                self._handleMessage(msg)
                # Get all pending messages before doing a new cleanup
                msg = self._getNextMessage(1)


    ###########################################################
    def _getNextMessage(self, timeout=None):
        """Pull the next control message off the queue.

        @param  timeout  Seconds to wait, or None to poll without blocking.
        @return msg      The message, or None if none was available.
        """
        try:
            if timeout is None:
                return self._commandQueue.get(False)
            return self._commandQueue.get(timeout=timeout)
        except Empty:
            return None
        except Exception:
            self._logger.error("Error reading the command queue",
                               exc_info=True)
            return None


    ###########################################################
    def _handleMessage(self, msg):
        """Process a message, logging rather than propagating any failure.

        @param  msg  The received message.
        """
        try:
            self._processMessage(msg)
        except Exception:
            self._logger.error("Error handling message %s" % str(msg),
                               exc_info=True)


    ###########################################################
    def _processMessage(self, msg):
        """Process an incoming message.

        @param  msg  The received message.
        """
        msgId = msg[0]

        if msgId == MessageIds.msgIdQuit:
            self._logger.info("Received quit message")
            self._running = False
        elif msgId == MessageIds.msgIdSetMaxStorage:
            self._logger.info("Changing max storage to %i" % msg[1])
            self._maxStorage = msg[1]*_kGbToBytes
        elif msgId == MessageIds.msgIdSetNumCameras:
            self._numCameras = max(1, msg[1])
            self._logger.info("Changing num cams to %i" % self._numCameras)
        elif msgId == MessageIds.msgIdRemoveDataAtLocation:
            location = msg[1]
            # After this message finishes we'll immediately go into _doCleanup
            # which will delete files from _pendingDeletes
            indexPaths = self._clipMgr.getAllFilesFromLocation(location)
            fullPaths = [os.path.join(self._videoDir, path)
                         for path in indexPaths]
            self._pendingDeletes.update(fullPaths)
            self._clipMgr.deleteLocation(location)
        elif msgId == MessageIds.msgIdDeleteFile:
            self._pendingDeletes.add(msg[1])
        elif msgId == MessageIds.msgIdSetCacheDuration:
            self._logger.info("Changing cache duration to %i" % msg[1])
            self._maxCacheDuration = msg[1]*60*60*1000
        elif msgId == MessageIds.msgIdSetDebugConfig:
            self._debugLogManager.SetLogConfig(msg[1])


    ###########################################################
    def _removeRemoteFiles(self):
        """Remove any remote files that are no longer necessary."""
        now = time.time()

        for base, dirs, files in os.walk(self._remoteDir):
            for f in files:
                path = os.path.join(base, f)
                age = now-os.path.getmtime(path)
                if age > _kRemoteFileLifespan:
                    self._pendingDeletes.add(path)

        finished = time.time()
        self._logger.info("Remote files cleanup took %d seconds" % int(finished-now))


    ###########################################################
    def _listSearch(self, alist, item):
        'Locate the leftmost value exactly equal to item'
        i = bisect_left(alist, item)
        if i != len(alist) and alist[i] == item:
            return i
        return -1

    ###########################################################
    def _removeOrphanFiles(self):
        """Remove any files on disk that shouldn't be there."""
        now = time.time()

        if now < self._lastOrphanFileCleanup+_kOrphanFileCleanupPeriod:
            return

        # Only run orphan scan between 2am and 5am
        currentHour = datetime.datetime.now().hour
        kMinOrphanScanHour = 2
        kMaxOrphanScanHour = 5
        if currentHour < kMinOrphanScanHour or currentHour > kMaxOrphanScanHour:
            return

        self._lastOrphanFileCleanup = now
        disablePath = os.path.join(self._configDir, "disableOrphanScan")
        if os.path.isfile(disablePath):
            self._logger.info("Skipping orphan file detection, %s is present" % disablePath)
            return

        # REFUSE TO RUN while a database is flagged as damaged.
        #
        # This sweep deletes every .mp4 in the archive that has no row in the
        # clips table.  That is safe only when the clips table can be trusted.
        # When the database is broken, recording carries on but registration
        # fails, so "no row" stops meaning "not wanted" and starts meaning
        # "we could not write it down" -- and this sweep becomes the mechanism
        # that turns a recoverable database fault into permanently deleted
        # footage.  Measured 2026-08-08: 1,566 clips / 6.28 GB were unregistered
        # within 90 minutes of the clipdb going bad, all of it deletable here.
        #
        # Refusing costs disk space, which is recoverable.  Sweeping costs
        # footage, which is not.
        flag = readCorruptionFlag(self._configDir)
        if flag:
            # Say which it is.  The flag means "do not delete anything", and
            # that is raised both by real damage and by clips simply not
            # arriving -- and in the second case the databases have just passed
            # their integrity check, so calling them damaged is a lie that
            # sends the user to repair a database with nothing wrong with it.
            if flag.get('source') == kStallFlagSource:
                self._logger.warning(
                    "REFUSING to run the orphan file sweep: clips are not "
                    "reaching the database (%s). The databases pass their "
                    "integrity check, so this is not damage -- but footage "
                    "recorded meanwhile is unregistered and is being KEPT "
                    "until clips are landing again. Flag file: %s"
                    % (flag.get('error', 'no detail'),
                       os.path.join(self._configDir, kCorruptionFlagFile)))
            else:
                self._logger.warning(
                    "REFUSING to run the orphan file sweep: a database is "
                    "flagged as damaged (%s). Unregistered footage is being "
                    "KEPT, so disk use will grow until the database is "
                    "repaired. Flag file: %s"
                    % (flag.get('error', 'no detail'),
                       os.path.join(self._configDir, kCorruptionFlagFile)))
            return

        filesScanned = 0
        markedForDeletion = 0
        allCameras = self._clipMgr.getCameraLocations()
        currentCameraName = ""
        camFiles = []
        fileEntryStart = len(self._videoDir)+1

        # Remove any movie files that aren't in the database.
        for path, _, files in os.walk(self._videoDir):
            if os.path.dirname(path) == self._videoDir:
                # We're in a camera subfolder
                currentCameraName = os.path.basename(path).lower()
                # Camera name is case sensitive, folder names may not be
                for cam in allCameras:
                    if cam.lower() == currentCameraName:
                        camFiles = self._clipMgr.getAllFilesFromLocation(cam)
                        camFiles.sort()
                        self._logger.info("Processing folder for '%s' with %d files in database" % (currentCameraName, len(camFiles)))
                        break

            for curFile in files:
                indexPath = normalizePath(os.path.join(path, curFile))
                if curFile.lower() in _kRemovableFiles:
                    self._pendingDeletes.add(indexPath)
                    markedForDeletion += 1
                    continue

                if not curFile.lower().endswith('.mp4'):
                    continue

                searchPath = indexPath[fileEntryStart:]
                filesScanned += 1
                filePresent = (self._listSearch(camFiles, searchPath) >= 0)

                # We grab the full list of file for the camera early on, and use it
                # for the majority files that are currently in database.
                # For the few that may need to be deleted, or may have been added in the
                # last few moments, we perform individual queries
                if not filePresent:
                    startTime, _ = self._clipMgr.getFileTimeInformation(searchPath)
                    if startTime == -1:
                        self._pendingDeletes.add(indexPath)
                        markedForDeletion += 1

        for dirPath, _, files in os.walk(self._tmpDir, True):
            for filename in files:
                filesScanned += 1
                filename = normalizePath(os.path.join(dirPath, filename))
                if filename not in self._tmpFileDict:
                    self._tmpFileDict[filename] = now

        # Add old files to the pending deletes list
        dictFiles = list(self._tmpFileDict.keys())
        for filename in dictFiles:
            filesScanned += 1
            if self._tmpFileDict[filename]+_kTmpFileLifespan < now:
                # If this file has expired, add it to the pending deletes list.
                del self._tmpFileDict[filename]
                if filename not in self._pendingDeletes:
                    self._pendingDeletes.add(filename)
                    markedForDeletion += 1
        finished = time.time()
        self._logger.info("Orphan files cleanup scanned %d files in %d seconds, %d marked for deletion" % (filesScanned, int(finished-now), markedForDeletion))

    ###########################################################
    def _canDeleteFolder(self, name):
        """ Determine is a folder can be deleted.
        """
        # we'd let the folder be 60 seconds in the past, before allowing to delete it
        _kSafeDeletionDistance = 60

        folderName = os.path.basename(name)
        if len(folderName) == 5 and folderName.isdigit():
            # Legacy 5-digit unix-seconds-prefix format
            currentFolder = str(time.time() - _kSafeDeletionDistance)[:5]
            return int(folderName) < int(currentFolder)
        if _isDateFolder(folderName):
            # yyyy-mm-dd format; lexicographic comparison is correct for date strings
            currentFolder = _secsToFolder(time.time() - _kSafeDeletionDistance)
            return folderName < currentFolder
        # folders we manage only have digits or yyyy-mm-dd names
        return False


    ###########################################################
    def _removeEmptyFolder(self, name):
        """ Remove empty folder we may have creeated
            This method will only remove folders named "thumbs" or similar to "15123"
            (e.g. first five characters of the timestamp).
            In the latter case, it also won't remove the folder if it corresponds to the current
            or recent (within the last few seconds) timestamp.
        """
        if len(os.listdir(name)) != 0:
            return

        if os.path.basename(name) == kThumbsSubfolder:
            pathToCheck = os.path.dirname(name)
        else:
            pathToCheck = name

        if self._canDeleteFolder(pathToCheck):
            try:
                self._logger.debug("Removing folder " + ensureUnicode(name))
                os.rmdir(name)
            except:
                self._logger.warning("Couldn't remove %s: %s" % (ensureUnicode(name), traceback.format_exc()))

    ###########################################################
    def _getThumbsStats(self, camID, timeID):
        """ For a pair of cameraID/timeID returns a tuple of (size,count,needsUpdate)
            of the cached values for the corresponding thumbs subfolder.
            If no cached value found, (0,0,True) will be returned
        """
        result = (0, 0, True)
        camThumbs = self._thumbStats.get(camID, None)
        if camThumbs is not None:
            timeThumbs = camThumbs.get(timeID, None)
            if timeThumbs is not None:
                result = timeThumbs
        return result

    ###########################################################
    def _updateThumbsStats(self, camID, timeID, size, count):
        """ Store cached values for thumbs size and count, given
            the corresponding cameraID/timeID.
            The cache only gets populated, if the timeID does not match
            the current time;  in other words, only if we'd stopped
            writing to this thumbs location
        """
        try:
            # updating only makes sense if we've finished writing to this folder
            if timeID.isdigit() or timeID < _secsToFolder(time.time()):
                if size > 0:
                    self._thumbStats.setdefault(camID, {})[timeID] = (size, count, False)
                else:
                    try:
                        del self._thumbStats[camID][timeID]
                    except:
                        pass
        except:
            self._logger.error("Error updating thumb stats: %s" % traceback.format_exc())

    ###########################################################
    def _updateRemovedThumbsStats(self, camID, timeID, deletedSize, deletedCount):
        """ Update the cached thumbs statistics, to reflect the deleted thumb files
        """
        if deletedCount == 0:
            return

        timeThumbs = self._getThumbsStats(camID, timeID)
        if not timeThumbs[2]:
            if timeThumbs[1] - deletedCount <= 0 or \
               timeThumbs[0] - deletedSize <= 0:
                try:
                    del self._thumbStats[camID][timeID]
                except:
                    pass
            else:
                newVal = (timeThumbs[0]-deletedSize,
                          timeThumbs[1]-deletedCount,
                          False)
                self._thumbStats[camID][timeID] = newVal

    ###########################################################
    def _shouldProcessDir(self, root, dirname):
        """ Determine if the subfolder should be scanned in order to
            populate thumbs storage cache, and check whether the subfolder is empty
        """
        # never skip processing of camera dirs
        if root == self._videoDir:
            return True

        # Process thumbs folders, if not found in cache
        if dirname == kThumbsSubfolder:
            timeSubfolder = os.path.dirname(root)
            timeID = os.path.basename(timeSubfolder)
            camSubfolder = os.path.dirname(timeSubfolder)
            camID = os.path.basename(camSubfolder)

            _, _, needsUpdate = self._getThumbsStats(camID, timeID)
            return needsUpdate

        # "time"-based folders only need to be processed once, as long as:
        # 1.  They're not currently being written to, and
        # 2a. Their corresponding thumbs subfolder doesn't exist, or
        # 2b. Their corresponding thumbs subfolder is in cache
        if (len(dirname) == 5 and dirname.isdigit()) or _isDateFolder(dirname):
            currentFolder = str(time.time())[:5] if (len(dirname) == 5 and dirname.isdigit()) else _secsToFolder(time.time())
            if dirname == currentFolder:
                # Ignore "current" folders while they're still being written to
                # We will be 10-40MB off per camera in size calculations,
                # But the saved runtime makes it worth the discrepancy.
                return False
            timeID = dirname
            camID = os.path.basename(root)
            thumbsSize, thumbsCount, needsUpdate = self._getThumbsStats(camID, timeID)
            if needsUpdate:
                # no cache entry
                thumbsPath = os.path.join(root, dirname, kThumbsSubfolder)
                if not os.path.isdir(thumbsPath):
                    # no thumbs subfolder ... store the cached value for thumbs,
                    # so we don't rescan this folder in the future,
                    # but rescan it this once, in case it's empty
                    # and needs to be deleted
                    self._updateThumbsStats(camID, timeID, 1, 0)

            # Update the totals using cached values (will be 0's if not in cache)
            self._thumbsSize += thumbsSize
            self._thumbsCount += thumbsCount
            return needsUpdate

        # Process everything else
        return True



    ###########################################################
    def _scanVideoStorage(self):
        """ Scan video storage, and perform housekeeping tasks like:
            -- detect and eliminate empty dirs
            -- calculate the size of thumbnails
            -- remove orphaned thumbnails
        """
        kMaxRuntime = 5000
        timerLogger = TimerLogger("Video storage scan")
        thumbsDirsScanned = 0
        nonThumbDirsScanned = 0
        orphanedThumbs = []

        self._thumbsPartial = False
        self._thumbsSize = 0
        self._thumbsCount = 0

        for root, dirnames, filenames in os.walk(self._videoDir):
            # Pare down subfolders, so we don't keep re-checking the same ones twice
            # This will also count all the cached thumb folders
            dirnames[:] = [d for d in dirnames if self._shouldProcessDir(root, d)]

            dirsCount = len(dirnames)
            filesCount = len(filenames)

            # self._logger.error("Processing " + root + " " + str(dirsCount) + " dirs,  " + str(filesCount) + " files")

            # Process folders with no files
            if filesCount == 0:
                # no files in the currrent dir
                if dirsCount == 0 and root != self._videoDir:
                    # no folders either, and it's not the rootdir ... attempt to delete
                    self._removeEmptyFolder(root)
                elif dirsCount == 1 and dirnames[0] == kThumbsSubfolder:
                    # there's only thumbs folder, left behind somehow
                    # schedule it for cleanup, unless the video folder is still "current"
                    self._logger.debug("Found an orphaned dir " + ensureUnicode(root))
                    rootName = os.path.basename(root)
                    currentFolder = str(time.time())[:5] if (len(rootName) == 5 and rootName.isdigit()) else _secsToFolder(time.time())
                    if rootName != currentFolder:
                        orphanedThumbs.append(os.path.join(root,kThumbsSubfolder))

            # Process thumbs folders
            if os.path.basename(root) == kThumbsSubfolder:
                timeSubfolder = os.path.dirname(root)
                timeID = os.path.basename(timeSubfolder)
                camSubfolder = os.path.dirname(timeSubfolder)
                camID = os.path.basename(camSubfolder)
                orphaned = root in orphanedThumbs

                folderThumbsSize, folderThumbsCount, folderNeedsUpdate = self._getThumbsStats(camID, timeID)

                if orphaned or folderNeedsUpdate:
                    folderThumbsCount = 0
                    folderThumbsSize = 0

                    if orphaned:
                        # thumbs with no corresponding videos ... delete all, and delete folder if empty
                        orphanedThumbs.remove(root)
                        deleted = 0
                        for filename in fnmatch.filter(filenames, '*.jpg'):
                            fullPath = os.path.join(root, filename)
                            try:
                                os.remove(fullPath)
                                deleted += 1
                            except:
                                self._logger.warning("Couldn't remove %s: %s" % (ensureUnicode(fullPath), traceback.format_exc()))
                        # schedule this thumbs folder for deletion, if empty
                        if deleted == filesCount and dirsCount == 0:
                            self._removeEmptyFolder(root)
                            # should try to delete the parent as well, now it's empty
                            self._removeEmptyFolder(os.path.dirname(root))
                    else:
                        for filename in fnmatch.filter(filenames, '*.jpg'):
                            folderThumbsSize += os.path.getsize(os.path.join(root, filename))
                            folderThumbsCount += 1

                    self._updateThumbsStats(camID, timeID, folderThumbsSize, folderThumbsCount)
                    thumbsDirsScanned += 1

                    self._thumbsSize += folderThumbsSize
                    self._thumbsCount += folderThumbsCount

            else:
                nonThumbDirsScanned += 1


            if timerLogger.diff_ms() > kMaxRuntime:
                self._thumbsPartial = True
                self._logger.info("Aborting storage scan: thumbsDirsScanned=" + str(thumbsDirsScanned) + \
                                    " nonThumbDirsScanned=" + str(nonThumbDirsScanned))
                break

        self._logger.info(timerLogger.status())

    ###########################################################
    def _populateFileSizeCacheItem(self, filePath, deleteIfNotFound, currentTime):
        fullPath = os.path.join(self._videoDir, filePath)
        try:
            fileSize = os.path.getsize(fullPath)
        except OSError:
            # Gone -- including the case where it vanished between an existence
            # check and the stat, which is routine while recording and deleting
            # run concurrently.  Callers depend on this never raising: the
            # renewal loop in _getFileListSize is not itself wrapped, and an
            # escape from there exits the whole process.
            if deleteIfNotFound:
                self._fileSizeCache.pop(filePath, None)
            return 0
        self._fileSizeCache[filePath] = (fileSize, currentTime)
        return fileSize


    ###########################################################
    def _getFileListSize(self, fileList):
        """Calculate the amount of disk space used by some files.

        @param  fileList  A list of (file, _,  _, _) tuples.
        @return size      The size in bytes used by the files in fileList.
        """
        now = time.time()

        size = 0

        cached = 0
        nonCached = 0
        expired = 0

        expiredItems = {}

        for filePath, _, _, _ in fileList:
            try:
                fileSize, cacheTime = self._fileSizeCache.get(filePath, (None,None))
                if not fileSize is None:
                    itemExpired = (now-cacheTime > _kFileSizeCacheLifespan)
                    if itemExpired:
                        # organized expired items into buckets based on the expiration time
                        expiredItems.setdefault(cacheTime,[]).append(filePath)
                    size += fileSize
                    cached += 1
                else:
                    size += self._populateFileSizeCacheItem(filePath, False, now)
                    nonCached += 1
            except Exception:
                self._logger.warning("Couldn't get size of %s" % filePath)

        # Renew expired items, oldest first. Limit renewals to _kMaxFileCacheExpirationsAtOnce items
        expirationTimes = sorted(expiredItems.keys())
        for expirationTime in expirationTimes:
            for expiredItem in expiredItems[expirationTime]:
                if expired >= _kMaxFileCacheExpirationsAtOnce:
                    break
                # NOT filePath: that is the leftover binding from the loop
                # above, so renewing it refreshed one arbitrary file over and
                # over while every expired entry stayed stale forever.
                self._populateFileSizeCacheItem(expiredItem, True, now)
                expired += 1
            if expired >= _kMaxFileCacheExpirationsAtOnce:
                break



        self._logger.info("Querying list of size " + str(len(fileList)) + " took " + str(time.time()-now) + "s; " + str(cached) + " cached, " + str(nonCached) + " non-cached, " + str(expired) + " expired items")
        return size


    ###########################################################
    def _deleteFile(self, file, camLoc, firstMs, lastMs, allowClips=False):
        """Delete a file.

        @param  file         The file to delete.
        @param  camLoc       The camera location of the file.
        @param  firstMs      The ms of the first frame in the file.
        @param  lastMs       The ms of the last frame in the file.
        @param  allowClips   If true, allow clips to be made.
        @return fileSize     The size in bytes freed by deleting the file.
        @return spaceGained  The fileSize - the size of any clips created
                             from the file.
        @return clipList     A list of (clip, camLoc, firstMs, lastMs) created
                             when deleting the file.
        """
        fileSize = 0
        clipSizes = 0
        clipList = []
        fullPath = os.path.join(self._videoDir, file)
        timesToRemove = [(firstMs, lastMs)]
        clipsAdded = 0

        # Save any information we need as we're about to remove it
        saveTimeList = self._clipMgr.getSaveTimeList(file)
        saveTimeList.sort()
        prevFile = self._clipMgr.getPrevFile(file)
        nextFile = self._clipMgr.getNextFile(file)
        procWidth, procHeight = self._clipMgr.getProcSize(file)

        # Remove the file from the clip db
        self._clipMgr.removeClip(file)

        self._logger.info("Removing ./%s, allowClips=%s saveTimeList=%s firstMs=%s lastMs=%s" %
                          (file, str(allowClips), str(saveTimeList), str(firstMs), str(lastMs)))

        # Check if we need to make any clips
        if allowClips and saveTimeList:
            # Retrieve the msList of the original file
            origFileMsList = getMsList(fullPath, self._logger.getCLogFn())

            if not origFileMsList:
                self._logger.warning("Couldn't retrieve msList for file %s, "
                                     "aborting." % fullPath)
                saveTimeList = []

            saveTimeListCopy = saveTimeList
            saveTimeList = []
            i = 0
            while i<len(saveTimeListCopy):
                if ( i+1 < len(saveTimeListCopy) and
                    saveTimeListCopy[i][1] < saveTimeListCopy[i+1][0] and
                    saveTimeListCopy[i][1] + _kMergeClipThresholdMs >= saveTimeListCopy[i+1][0] ):
                    # The two ranges are within 4s of each other. Merge the two ranges, and skip the next entry
                    self._logger.debug("file="+file+" i="+str(i)+" merging ranges ["+
                                        str(saveTimeListCopy[i][0])+","+
                                        str(saveTimeListCopy[i][1])+"] and ["+
                                        str(saveTimeListCopy[i+1][0])+","+
                                        str(saveTimeListCopy[i+1][1])+"]")
                    saveTimeList.append((saveTimeListCopy[i][0], saveTimeListCopy[i+1][1]))
                    i+=1
                else:
                    saveTimeList.append(saveTimeListCopy[i])
                i+=1

            i = 0
            for (saveStart, saveStop) in saveTimeList:
                origSaveStart = saveStart
                origSaveStop = saveStop

                # A range that doesn't overlap this file at all can only produce
                # a zero-length cut.  Historically these came from addClip
                # attaching a pending save range to the first clip registered
                # after a camera dropout; clipdb rows written before that fix
                # still carry them, so skip them here rather than letting them
                # reach ffmpeg.
                if saveStop < firstMs or saveStart > lastMs:
                    self._logger.info(
                        "Skipping save range %d-%d for %s: outside the file's "
                        "%d-%d span, nothing to cut" %
                        (saveStart, saveStop, file, firstMs, lastMs))
                    continue

                # Find the actual file times closest to our desired save times
                msListLen = len(origFileMsList)
                bisectIndex = bisect.bisect_left(origFileMsList,
                                                 (saveStart-firstMs))
                if bisectIndex == msListLen:
                    self._logger.warning("Save requested for times not in "
                                         "file. Requested, file: %s" %
                                         str((saveStart-firstMs,
                                              saveStop-firstMs,
                                              origFileMsList[0],
                                              origFileMsList[msListLen-1])))
                    bisectIndex -= 1
                startOffset = origFileMsList[bisectIndex]
                saveStart = firstMs + startOffset
                bisectIndex = bisect.bisect_left(origFileMsList,
                                                 (saveStop-firstMs))
                if bisectIndex == msListLen:
                    bisectIndex -= 1
                stopOffset = origFileMsList[bisectIndex]
                saveStop = firstMs + stopOffset

                # Both bisects can still land on the same frame -- the
                # msListLen clamp above pins either end to the last entry, and a
                # file registered for longer than it really holds puts the whole
                # range past that.  remuxSubClip would just return -1; say why
                # instead of reporting it as an ffmpeg failure.
                if stopOffset <= startOffset:
                    self._logger.info(
                        "Skipping save range %d-%d for %s: collapsed to a "
                        "zero-length cut at offset %d (file holds %d-%dms)" %
                        (origSaveStart, origSaveStop, file, startOffset,
                         origFileMsList[0], origFileMsList[msListLen-1]))
                    continue

                newClipName = file[:-4] + "-%i.mp4" % i
                newClipPath = os.path.join(self._videoDir, newClipName)


                # actual offset may vary, as we search backwards for keyframe
                actualStartOffset = ClipUtils.remuxSubClip(fullPath, newClipPath, startOffset,
                        stopOffset, self._configDir, self._logger.getCLogFn())
                if actualStartOffset < 0:
                    self._logger.warning("Couldn't create clip %s from %s, %s"
                                         %(fullPath, newClipPath, str(
                                           (saveStart, saveStop, startOffset, stopOffset))))
                    continue

                if startOffset > actualStartOffset:
                    startOffsetAdjustment = startOffset-actualStartOffset
                else:
                    startOffsetAdjustment = 0
                saveStart = saveStart - startOffsetAdjustment


                # If the new clip is at the beginning or end of the original
                # clip, maintain any prev/next links
                prev = ''
                next = ''
                if saveStart == firstMs and prevFile:
                    prev = prevFile
                if saveStop == lastMs and nextFile:
                    next = nextFile


                # If the last timestamp is off (we've seen mostly off-by-1 errors),
                # correct based on the actual duration of the file
                clipDuration = getDuration(newClipPath, self._logger.getCLogFn())
                if saveStart+clipDuration < saveStop:
                    # Sub-second shortfalls are the two duration estimators
                    # disagreeing (see _kLastMsCorrectionWarnMs), not a fault --
                    # correct silently.  A larger one means the clip really is
                    # short of what was asked for, and is worth seeing.
                    shortfallMs = saveStop - (saveStart+clipDuration)
                    logFn = (self._logger.warning
                             if shortfallMs >= _kLastMsCorrectionWarnMs
                             else self._logger.debug)
                    logFn("Correcting last timestamp value of " + str(saveStop) +
                          " to " + str(saveStart+clipDuration) + " based on duration of " +
                          str(clipDuration) + " (short by " + str(shortfallMs) + "ms)")
                    saveStop = saveStart+clipDuration

                self._logger.debug("Created new clip %s from %s: startMs=%d/%d/%d stopMs=%d/%d/%d duration=%d/%d/%d startOffset=%d actualStartOffset=%d startOffsetAdjustment=%d"
                                     %(newClipName, file,
                                     saveStart, origSaveStart, saveStart-origSaveStart,
                                     saveStop, origSaveStop, saveStop-origSaveStop,
                                     saveStop-saveStart, origSaveStop-origSaveStart, saveStop-saveStart-origSaveStop+origSaveStart,
                                     startOffset, actualStartOffset, startOffsetAdjustment))


                # Add the new clip to the clipMgr
                # self._logger.warning("Adding a clip at '" + newClipName + "'" +
                #                     " rangeRequested=["+str(origStart)+","+str(origStop)+","+str(origStop-origStart)+"]" +
                #                     " rangeSaved=["+str(saveStart)+","+str(saveStop)+","+str(saveStop-saveStart)+"]" +
                #                     " rangeVerified=["+str(saveStart+firstNewFileMs)+","+str(saveStart+lastNewFileMs)+","+str(lastNewFileMs-firstNewFileMs)+"]" );
                self._clipMgr.addClip(newClipName, camLoc, saveStart, saveStop,
                                      prev, next, kCacheStatusNonCache,
                                      procWidth, procHeight, False)
                clipsAdded += 1

                try:
                    # NOT fileSize: that name holds the deleted file's size,
                    # which is this function's return value.
                    newClipSize = os.path.getsize(newClipPath)
                    self._fileSizeCache[newClipName] = (newClipSize,
                                                        time.time())
                except Exception:
                    self._logger.error("Failed to update file size for " + newClipName)

                # Get the new clip file size and add to clipSizes
                try:
                    clipSizes += os.path.getsize(newClipPath)
                except Exception:
                    self._logger.warning("Couldn't get size of %s"
                                         % newClipPath)

                # Remove the span of the new clip from the times to delete
                lastTimeSet = timesToRemove.pop()
                if lastTimeSet[0] < saveStart:
                    timesToRemove.append((lastTimeSet[0], saveStart-1))
                if lastTimeSet[1] > saveStop:
                    timesToRemove.append((saveStop+1, lastTimeSet[1]))

                # Add the clip to the clip list
                clipList.append((newClipName, camLoc, saveStart, saveStop))

                i += 1

        for start, stop in timesToRemove:
            self._dataMgr.deleteCameraLocationDataBetween(camLoc, start, stop)
            self._deleteThumbs(camLoc, start, stop)

        # Not being cached is a routine outcome (_populateFileSizeCacheItem
        # skips files that were briefly missing), so it is not an error.
        self._fileSizeCache.pop(file, None)

        try:
            fileSize = os.path.getsize(fullPath)
            os.remove(fullPath)
            if clipsAdded == 0:
                # don't even attempt to delete folder if clips were added
                dirname = os.path.dirname(fullPath)
                self._removeEmptyFolder(dirname)
        except Exception:
            # Nothing was freed, so say so.  The trim loops add this to their
            # running free-space total; crediting space that was not actually
            # reclaimed makes them stop early with the disk still full.
            fileSize = 0
            self._logger.warning("Couldn't remove %s: %s" % (fullPath, traceback.format_exc()))
            self._pendingDeletes.add(fullPath)

        return fileSize, fileSize-clipSizes, clipList

    ###########################################################
    def _deleteThumbs(self, camLoc, start, stop):
        """ Removes thumbnail files for camera in a specific time range
        """
        camLoc = camLoc.lower() # camera name is always converted to lower case when creating paths
        subfolders = _enumDateFolders(start, stop)
        totalFilesDeleted = 0
        self._logger.debug("Removing thumbs between " + str(start) + " and " + str(stop) + "; folders=" + ensureUnicode(str(subfolders)))
        for folder in subfolders:
            thumbFolder = os.path.join(self._videoDir, camLoc, folder, kThumbsSubfolder )

            # Thumbs folder may not exist, check for it first
            if not os.path.isdir(thumbFolder):
                continue

            keptFiles = 0
            deletedSize = 0
            deletedCount = 0

            for file in os.listdir(thumbFolder):
                try:
                    fileMs = int(os.path.splitext(os.path.basename(file))[0])
                except (ValueError, OverflowError):
                    # Thumbnail has a non-integer name (legacy datetime format);
                    # delete it unconditionally since it's in this date folder.
                    fullFilePath = os.path.join(thumbFolder, file)
                    try:
                        os.remove(fullFilePath)
                    except Exception:
                        pass
                    continue
                if fileMs >= start and fileMs <= stop:
                    fullFilePath = os.path.join(thumbFolder, file)
                    try:
                        size = os.path.getsize(fullFilePath)
                        os.remove(fullFilePath)
                        self._logger.debug("Deleted " + ensureUnicode(fullFilePath))
                        deletedSize += size
                        deletedCount += 1
                    except:
                        self._logger.error("Failed to delete " + ensureUnicode(fullFilePath))
                else:
                    keptFiles += 1

            if keptFiles == 0:
                self._removeEmptyFolder(thumbFolder)

            totalFilesDeleted += deletedCount
            self._updateRemovedThumbsStats(camLoc, folder, deletedSize, deletedCount)

        if totalFilesDeleted > 0:
            self._logger.debug("Deleted " + str(totalFilesDeleted) + " thumb files")

    ###########################################################
    def _checkForInterrupts(self, currentOperation):
        if (time.time()-self._cleanupCycleLastInterruptCheckTime) > _kMaxUnresponsiveTime:
            self._cleanupCycleLastInterruptCheckTime = time.time()
            # If there's a message pending, process it and exit the cleanup loop; otherwise proceed as needed
            msg = self._getNextMessage()
            if msg is not None:
                self._logger.info("Reached time limit while " + currentOperation + ". Ran uninterrupted for " + str(int(time.time() - self._cleanupCycleStartTime)) + "sec")
                self._handleMessage(msg)
                return True
        return False


    ###########################################################
    def _readSummaryPrefs(self):
        """Read (enabled, outputDir) from backEndPrefs.

        Reads the pickle directly (NMS is the single writer; we're a read-only
        consumer) so we don't import BackEndPrefs/wx into this headless process.
        A torn read or missing file just yields (False, "") for this pass.
        """
        try:
            with open(os.path.join(self._configDir, kPrefsFile), 'rb') as f:
                prefs = pickle.load(f)
            if isinstance(prefs, dict):
                return (bool(prefs.get('summaryEnabled', False)),
                        prefs.get('summaryDir', '') or '')
        except Exception:
            pass
        return (False, '')

    @staticmethod
    def _sanitizeFilename(name):
        """Strip characters illegal in a Windows filename (keep spaces)."""
        out = name
        for ch in '\\/:*?"<>|':
            out = out.replace(ch, '_')
        return out.strip() or 'camera'

    @staticmethod
    def _thumbMs(name):
        """Absolute epoch-ms encoded in a thumb filename, or None.

        Current writer emits '<epoch-ms>.jpg'; older builds used
        'YYYY-MM-DD-HHMMSS.jpg'.
        """
        stem = name[:-4]
        if stem.isdigit():
            try:
                return int(stem)
            except ValueError:
                return None
        try:
            dt = datetime.datetime.strptime(stem, '%Y-%m-%d-%H%M%S')
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return None

    def _summaryOutputPath(self, outDir, camLoc, localDate):
        """<outDir>/yyyy/mm/yyyy-mm-dd/video summary/video-yyyy-mm-dd-<cam>.mp4"""
        d = localDate.strftime('%Y-%m-%d')
        cam = self._sanitizeFilename(camLoc)
        return os.path.join(outDir, localDate.strftime('%Y'),
                            localDate.strftime('%m'), d, 'video summary',
                            'video-%s-%s.mp4' % (d, cam))

    def _thumbsForCameraLocalDate(self, camLoc, localDate):
        """Sorted thumb paths for a camera whose LOCAL date == localDate.

        Thumbs are bucketed into UTC-named date folders on disk but named by
        absolute epoch-ms, so we scan the UTC folders that can overlap the local
        day ({D-1, D, D+1}) and keep frames whose LOCAL calendar date matches.
        """
        camDir = os.path.join(self._videoDir, camLoc.lower())
        found = []   # (ms, path)
        for off in (-1, 0, 1):
            utcName = (localDate + datetime.timedelta(days=off)).strftime(
                '%Y-%m-%d')
            thumbsDir = os.path.join(camDir, utcName, kThumbsSubfolder)
            if not os.path.isdir(thumbsDir):
                continue
            try:
                names = os.listdir(thumbsDir)
            except Exception:
                continue
            for name in names:
                if not name.lower().endswith('.jpg'):
                    continue
                ms = self._thumbMs(name)
                if ms is None:
                    continue
                if datetime.datetime.fromtimestamp(ms / 1000.0).date() \
                        == localDate:
                    found.append((ms, os.path.join(thumbsDir, name)))
        found.sort(key=lambda x: x[0])
        return [p for _, p in found]

    def _maybeGenerateSummaries(self):
        """Build one missing daily-summary video per pass, if enabled.

        For each camera and each recently-completed LOCAL day, if the summary
        file doesn't already exist and that day has thumbs, stitch them into a
        low-res montage.  Stops after one encode attempt to bound CPU; the rest
        get picked up on later passes.
        """
        now = time.time()
        if now < self._lastSummaryRun + _kSummaryPeriodSecs:
            return
        self._lastSummaryRun = now

        enabled, outDir = self._readSummaryPrefs()
        if not enabled or not outDir:
            return

        today = datetime.date.today()
        try:
            cameras = self._clipMgr.getCameraLocations()
        except Exception:
            return

        # Drop failure records for days that have aged out of the backfill
        # window, so the dict cannot grow without bound.
        oldest = (today - datetime.timedelta(
            days=_kSummaryBackfillDays)).strftime('%Y-%m-%d')
        for key in [k for k in self._summaryFailures if k[1] < oldest]:
            del self._summaryFailures[key]

        for camLoc in cameras:
            for back in range(1, _kSummaryBackfillDays + 1):
                localDate = today - datetime.timedelta(days=back)
                failKey = (camLoc, localDate.strftime('%Y-%m-%d'))
                if self._summaryFailures.get(failKey, 0) >= \
                        _kSummaryMaxAttempts:
                    # Given up on this one -- keep it from blocking the rest.
                    continue
                outPath = self._summaryOutputPath(outDir, camLoc, localDate)
                if os.path.exists(outPath):
                    continue
                paths = self._thumbsForCameraLocalDate(camLoc, localDate)
                if not paths:
                    continue
                self._logger.info("Generating daily summary %s (%d thumbs)"
                                  % (outPath, len(paths)))
                try:
                    n = ClipUtils.makeSummaryVideo(
                        paths, outPath, _kSummaryFps, self._logger.getCLogFn())
                except Exception:
                    self._logger.error("summary encode failed: %s" % outPath,
                                       exc_info=True)
                    n = -1
                if n > 0:
                    self._summaryFailures.pop(failKey, None)
                    self._logger.info("Daily summary written: %s (%d frames)"
                                      % (outPath, n))
                    # Stamp create-date metadata (MediaCreateDate +
                    # TrackCreateDate) to the summary's day at 23:59 local --
                    # the summary spans a whole day with no single capture
                    # time, so pin it to end-of-day.  Best-effort.
                    summaryMs = int(datetime.datetime(
                        localDate.year, localDate.month, localDate.day,
                        23, 59, 0).timestamp() * 1000)
                    ClipUtils.stampMp4CreationTime(
                        outPath, summaryMs, self._logger.warning)
                else:
                    fails = self._summaryFailures.get(failKey, 0) + 1
                    self._summaryFailures[failKey] = fails
                    if fails >= _kSummaryMaxAttempts:
                        self._logger.warning(
                            "Daily summary produced nothing: %s -- giving up "
                            "after %d attempts so it stops blocking the "
                            "remaining summaries" % (outPath, fails))
                    else:
                        self._logger.warning(
                            "Daily summary produced nothing: %s (attempt %d "
                            "of %d)" % (outPath, fails, _kSummaryMaxAttempts))
                # One encode attempt per pass (success or failure).
                return

    def _checkCriticalDiskSpace(self):
        """Hard safety net: stop/resume recording based on ACTUAL free space.

        Runs every cleanup cycle, independent of the storage budget and of
        infinite mode, so external drive fill still trips it.  Sends a
        transition message to the back end (stop below the critical threshold,
        resume once recovered past the higher threshold).
        """
        if not os.path.isdir(self._videoDir):
            return
        try:
            total, used, free, pctFree = getDiskUsage(self._videoDir)
        except Exception:
            self._logger.error("Could not read free space from %s for the "
                               "low-disk safety check" % self._videoDir,
                               exc_info=True)
            return

        # Publish the numbers we just computed so the health view can show them
        # (this check already runs every cleanup cycle, so it costs nothing).
        try:
            self._backEndQueue.put([MessageIds.msgIdHealthInfo, 'disk', {
                'path':    self._videoDir,
                'totalGB': round(total / (1024.0 ** 3), 1),
                'freeGB':  round(free / (1024.0 ** 3), 1),
                'pctFree': int(pctFree),
                'criticalPct': _kCriticalFreePct,
            }])
        except Exception:
            pass

        isCritical = pctFree < _kCriticalFreePct

        if self._lowDiskCritical is None:
            # First check since this process (re)started.  The back end only
            # leaves its paused state on a resume message from here, so if a
            # previous instance exited while it was paused and the disk has
            # since recovered, neither transition below would ever fire and
            # every camera would stay stopped indefinitely.  Announce the real
            # state once; the back end acts only on a change of its own latch,
            # so this is a no-op whenever the two already agree.
            self._lowDiskCritical = isCritical
            self._logger.info(
                "Disk state sync after start: %d%% free on %s, back end "
                "should be %s" % (pctFree, self._videoDir,
                                  "PAUSED" if isCritical else "RECORDING"))
            self._backEndQueue.put([MessageIds.msgIdCriticalDiskSpace,
                                    isCritical, int(pctFree), self._videoDir])
        elif (not self._lowDiskCritical) and isCritical:
            self._lowDiskCritical = True
            self._logger.warning(
                "CRITICAL: only %d%% free on video drive %s (< %d%%) — "
                "signaling stop-recording" %
                (pctFree, self._videoDir, _kCriticalFreePct))
            self._backEndQueue.put([MessageIds.msgIdCriticalDiskSpace, True,
                                    int(pctFree), self._videoDir])
        elif self._lowDiskCritical and pctFree >= _kResumeFreePct:
            self._lowDiskCritical = False
            self._logger.warning(
                "Video drive %s recovered to %d%% free (>= %d%%) — "
                "signaling resume-recording" %
                (self._videoDir, pctFree, _kResumeFreePct))
            self._backEndQueue.put([MessageIds.msgIdCriticalDiskSpace, False,
                                    int(pctFree), self._videoDir])


    def _checkClockSync(self):
        """Warn the user when the PC clock has drifted away from real time.

        Every clip and event time comes from the PC clock, while the cameras
        stamp their own NTP-synced clock into the picture.  A drifting PC clock
        therefore makes recorded times silently disagree with the timestamps a
        camera displays, without anything appearing broken -- so it's worth
        surfacing rather than leaving to be rediscovered frame-by-frame.

        Entirely best-effort: a machine with no internet simply skips the
        check, since a warning must never be manufactured from a network
        failure.  Runs daily, and re-warns at most daily unless the error has
        materially worsened, so the dialog can't become background noise.
        """
        now = time.time()

        # If the clock stepped BACKWARDS (someone just resynced it, which is
        # exactly what this check asks for) our deadlines are in the future
        # and would wedge the check.  Re-arm instead.
        if now < self._lastClockCheck:
            self._lastClockCheck = 0
            self._lastClockAlert = 0

        if (now - self._lastClockCheck) < _kClockCheckPeriod:
            return
        self._lastClockCheck = now

        try:
            offset = measureClockOffset(logger=self._logger)
        except Exception:
            self._logger.warning("Clock check failed", exc_info=True)
            return
        if offset is None:
            return          # No NTP reachable; already logged.

        # Publish every reading (not just the over-threshold ones) so the health
        # view can show the current drift even when it's healthy.
        try:
            self._backEndQueue.put([MessageIds.msgIdHealthInfo, 'clock',
                                    float(offset)])
        except Exception:
            pass

        if abs(offset) < _kClockSkewAlertSecs:
            self._logger.info("Clock check: PC clock is within %.3fs of real "
                              "time" % offset)
            return

        direction = "behind" if offset > 0 else "ahead of"

        # Over threshold.  Stay quiet if we've already said so recently, unless
        # the drift has grown a lot since that warning.
        worsened = abs(offset) > (abs(self._lastClockAlertOffset) *
                                  _kClockWorsenedFactor)
        if (now - self._lastClockAlert) < _kClockReAlertPeriod and not worsened:
            self._logger.warning("Clock check: PC clock is %.2fs %s real time "
                                 "(already warned)" % (abs(offset), direction))
            return

        self._lastClockAlert = now
        self._lastClockAlertOffset = offset
        self._logger.warning("Clock check: PC clock is %.2fs %s real time — "
                             "warning the user" % (abs(offset), direction))
        self._backEndQueue.put([MessageIds.msgIdClockSkew, float(offset)])


    def _doCleanup(self):
        """Perform disk cleanup.

        @return  moreToDo  If True, we'd like to be called again, if possible.
        """
        # Hard low-disk safety net — runs before everything else and even in
        # infinite mode, since external factors can fill the drive regardless
        # of our own storage budget.
        self._checkCriticalDiskSpace()

        # Normal age-based eviction is NOT suspended while a database is
        # flagged: it only ever deletes clips the database knows about, and
        # stopping it would let the drive fill -- which is its own outage. The
        # orphan sweep is the dangerous one and that is refused outright (see
        # _removeOrphanFiles). Say so periodically, because with the sweep off
        # the archive will grow beyond its usual footprint.
        flag = readCorruptionFlag(self._configDir)
        if flag:
            now = time.time()
            if (now - self._lastCorruptionWarn) > 1800:
                self._lastCorruptionWarn = now
                if flag.get('source') == kStallFlagSource:
                    self._logger.warning(
                        "clips are not reaching the database (%s): the orphan "
                        "sweep is suspended so unregistered footage is kept, "
                        "but normal age-based eviction continues. The "
                        "databases pass their integrity check -- this clears "
                        "itself once clips are registering again."
                        % flag.get('error', 'no detail'))
                else:
                    self._logger.warning(
                        "a database is flagged as damaged (%s): the orphan "
                        "sweep is suspended so unregistered footage is kept, "
                        "but normal age-based eviction continues. Repair the "
                        "database to return to normal."
                        % flag.get('error', 'no detail'))

        # Warn if the PC clock has drifted (rate-limited internally to daily).
        self._checkClockSync()

        # Cleanup orphan files if necessary
        self._removeOrphanFiles()

        # Cleanup any remote files hanging around
        self._removeRemoteFiles()

        # Attempt to remove any pending deletes before calculating disk space.
        for path in list(self._pendingDeletes):
            try:
                os.remove(path)
                self._pendingDeletes.discard(path)
            except Exception:
                if not os.path.exists(path):
                    self._pendingDeletes.discard(path)

        if self._infiniteMode:
            # If we're running with 'infinite' disk space we skip all the
            # cache/clip cleanup code.
            return

        # Get the current state of the disk.
        try:
            os.makedirs(self._videoDir)
        except Exception:
            pass

        if not os.path.isdir(self._videoDir):
            self._logger.warning("Video directory %s could not be found." %
                                 self._videoDir)
            return


        diskFree = 0
        diskPctFree = 0
        systemDiskFree = 0
        systemDiskPctFree = 0

        try:
            diskUsageTupleArchive = getDiskUsage(self._videoDir)
            diskFree = diskUsageTupleArchive[2] - _kReservedDiskSpace
            diskPctFree = diskUsageTupleArchive[3]
        except Exception:
            self._logger.error("Could not retrieve disk space from %s" %
                               self._videoDir, exc_info=True)

        try:
            diskUsageTupleArchive = getDiskUsage(self._tmpDir)
            systemDiskFree = diskUsageTupleArchive[2]
            systemDiskPctFree = diskUsageTupleArchive[3]
        except Exception:
            self._logger.error("Could not retrieve disk space from %s" %
                               self._tmpDir, exc_info=True)

        cacheFiles = self._clipMgr.getCacheFiles()
        clipFiles = self._clipMgr.getNonCacheFiles()
        unmanagedFiles = self._clipMgr.getUnmanagedFiles()
        cacheSpaceUsed = self._getFileListSize(cacheFiles)
        clipSpaceUsed = self._getFileListSize(clipFiles)
        self._scanVideoStorage() # determines thumbs size, and processes empty dirs
        unmanagedSpaceUsed = self._getFileListSize(unmanagedFiles)
        usedSpace = cacheSpaceUsed + clipSpaceUsed + self._thumbsSize

        # The amount we are allowed to use is the minimum of the user setting
        # and the maximum possible.
        totalUsableSpace = min(self._maxStorage, usedSpace+diskFree)

        # Verify that we have enough space available to operate.
        minRequiredSpace = _kMinSpacePerCam*self._numCameras
        self._logger.debug("Max space %d, min space %d, numCameras %i" %
                           (totalUsableSpace, minRequiredSpace,
                            self._numCameras))

        targetFreeSpace = max(_kTargetFreeSpacePerCam*self._numCameras,
                            kMinFreeSysDriveSpaceMB*_kMbToBytes*_kMinFreeSpaceStayAheadRatio)
        curFree = totalUsableSpace-usedSpace

        self._logger.info(("Free: %s (%d%%) disk, %s cur, %s tgt; "
                           "Used: %s cache, %s clip, %s/%d/%s thumbs, %s xtra, %s cfg; "
                           "TmpFree: %s (%d%%) disk") % (
                           getStorageSizeStr(diskFree),
                           diskPctFree,
                           getStorageSizeStr(curFree),
                           getStorageSizeStr(targetFreeSpace),
                           getStorageSizeStr(cacheSpaceUsed),
                           getStorageSizeStr(clipSpaceUsed),
                           getStorageSizeStr(self._thumbsSize),
                           self._thumbsCount,
                           "p" if self._thumbsPartial else "f",
                           getStorageSizeStr(unmanagedSpaceUsed),
                           getStorageSizeStr(self._maxStorage),
                           getStorageSizeStr(systemDiskFree),
                           systemDiskPctFree ))
        if minRequiredSpace > totalUsableSpace:
            # If not, notify the back end and quit.
            self._logger.warning("Insufficient space: %i MB per camera expected"
                               % (_kMinSpacePerCam/1024/1024))
            self._backEndQueue.put([MessageIds.msgIdInsufficientSpace])

        # Start timing now; that means that if the above is slow we'll be
        # unresponsive for longer, but at least we can be guaranteed that
        # we'll get a decent amount done.  Hopefully the above isn't slow...
        self._cleanupCycleStartTime = time.time()
        self._cleanupCycleLastInterruptCheckTime = self._cleanupCycleStartTime

        # Trim cache files over the specified number of hours, check space
        # Note: works on newer files first...
        lowestTime = time.time()*1000-self._maxCacheDuration
        #lowestTime = time.time()*1000-2*60*1000 # run after 2 min ... useful for debugging
        i = len(cacheFiles)-1
        preTrimFree = curFree
        while i > -1:
            curFile, camLoc, firstMs, lastMs = cacheFiles[i]
            i -= 1
            if lastMs < lowestTime:
                fileSize, spaceGained, clipList = self._deleteFile(curFile,
                                                                   camLoc,
                                                                   firstMs,
                                                                   lastMs, True)
                curFree += spaceGained
                cacheSpaceUsed -= fileSize
                clipFiles.extend(clipList)

                if self._checkForInterrupts("cleaning cache"):
                    return True

        if preTrimFree != curFree:
            self._logger.info("Post cache trim %.1fM free" % (curFree / _kMbToBytesF))

        if curFree > targetFreeSpace:
            self._logger.debug("No further work necessary")
            self._tidyObjectTable()
            # if we have partial thumbs stats, run again immediately
            return self._thumbsPartial

        usableSpacePerCam = totalUsableSpace/self._numCameras

        if usableSpacePerCam > _kMinCacheBlockPerCam:
            if usableSpacePerCam > _kMinCacheBlockPerCam+_kFirstClipBlockPerCam:
                self._logger.info("Trimming clips to min size")
                # Delete clips down to _kFirstClipBlockPerCam
                while (curFree < targetFreeSpace) and \
                      (clipSpaceUsed > _kFirstClipBlockPerCam*self._numCameras)\
                      and clipFiles:

                    curFile, camLoc, firstMs, lastMs = clipFiles.pop(0)
                    fileSize, _, _ = self._deleteFile(curFile, camLoc, firstMs,
                                                      lastMs)
                    clipSpaceUsed -= fileSize
                    curFree += fileSize
                    self._logger.debug("%d free, %d cacheUsed, %d clipUsed" %
                                       (curFree, cacheSpaceUsed, clipSpaceUsed))

                    if self._checkForInterrupts("trimming clips"):
                        return True


            self._logger.info("Trimming cache to min size")
            # Delete cache down to _kMinCacheBlockPerCam
            while (curFree < targetFreeSpace) and \
                  (cacheSpaceUsed > _kMinCacheBlockPerCam*self._numCameras) \
                  and cacheFiles:

                curFile, camLoc, firstMs, lastMs = cacheFiles.pop(0)
                fileSize, spaceGained, clipList = self._deleteFile(curFile,
                                                                   camLoc,
                                                                   firstMs,
                                                                   lastMs, True)
                curFree += spaceGained
                cacheSpaceUsed -= fileSize
                clipFiles.extend(clipList)
                self._logger.debug("%d free, %d cacheUsed, %d clipUsed" %
                                   (curFree, cacheSpaceUsed, clipSpaceUsed))

                if self._checkForInterrupts("trimming to min cache size"):
                    return True

            # If we're still not free enough, delete clips.  If we started by
            # deleting clips this is relevant as deleting cache may have
            # produced clips, so some previously skipped now need to go.
            self._logger.info("Trimming clips again as necessary")
            while (curFree < targetFreeSpace) and clipFiles:
                curFile, camLoc, firstMs, lastMs = clipFiles.pop(0)
                spaceGained, _, _ = self._deleteFile(curFile, camLoc, firstMs,
                                                     lastMs)
                curFree += spaceGained
                self._logger.debug("%d free" % curFree)

                if self._checkForInterrupts("trimming clips (round 2)"):
                    return True

        else:
            # Dire space constraints...Delete clips, then delete cache files
            # WITHOUT making clips for marked times.
            self._logger.info("Low space - removing clips")
            while (curFree < targetFreeSpace) and clipFiles:
                curFile, camLoc, firstMs, lastMs = clipFiles.pop(0)
                fileSize, _, _ = self._deleteFile(curFile, camLoc, firstMs,
                                                  lastMs)
                curFree += fileSize
                self._logger.debug("%d free" % curFree)

                # Dire crunch, allow more time
                if self._checkForInterrupts("removing clips on low space"):
                    return True

            self._logger.info("Low space - removing cache")
            while (curFree < targetFreeSpace) and cacheFiles:
                curFile, camLoc, firstMs, lastMs = cacheFiles.pop(0)
                fileSize, _, _ = self._deleteFile(curFile, camLoc, firstMs,
                                                  lastMs)
                curFree += fileSize
                self._logger.debug("%d free" % curFree)

                # Dire crunch, allow more time
                if self._checkForInterrupts("removing cache on low space"):
                    return True

        self._tidyObjectTable()

        self._logger.info("Finished cleaning")
        return False

    ###########################################################
    def _tidyObjectTable(self):
        # This is a very expensive operation (took 30s on my system), that seems to not
        # come across many things to tidy. Make sure not to run it too often
        if time.time()-self._lastTidyObjectTableTime > _kMinTidyObjectTablePeriod:
            # Tidy up the object table in case there's anything we missed
            # Ideally this shouldn't do anything, but it pays to be paranoid...
            self._dataMgr.tidyObjectTable()
            self._lastTidyObjectTableTime = time.time()


##############################################################################
def _forcedQuitCallback():
    """A callback to notify the current app if a force quit ever happens.

    This is done here so that we don't keep registering if we restart; also
    doing things this way keeps anyone from holding a reference to the app.
    """
    if _cleaner is not None:
        _cleaner._markDone()
__callbackFunc = registerForForcedQuitEvents(_forcedQuitCallback) #PYCHECKER Not intended to be used; just here to keep refCount
