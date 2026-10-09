#! /usr/local/bin/python

#*****************************************************************************
#
# ResponseRunner.py
#     Process responsible for generating responses to triggers (email, push notifications, etc)
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
from queue import Empty as QueueEmpty
import ftplib
import os, sys
import shutil
import time
import urllib.parse
import threading
import traceback
import queue

# Common 3rd-party imports...

# Toolbox imports...
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from vitaToolbox.networking.SimpleEmail import sendSimpleEmail
from vitaToolbox.networking.HttpClient import HttpClient
from vitaToolbox.windows.winUtils import registerForForcedQuitEvents
from vitaToolbox.strUtils.EnsureUnicode import ensureUtf8, ensureUnicode
from vitaToolbox.sysUtils.TimeUtils import getTimeAsString, formatTime

# Local imports...
from appCommon.CommonStrings import kFtpProtocol
from appCommon.CommonStrings import kLocalExportProtocol
from appCommon.CommonStrings import kGatewayHost
from appCommon.CommonStrings import kGatewayPath
from appCommon.CommonStrings import kVersionString
from appCommon.CommonStrings import kGatewayTimeoutSecs
from appCommon.CommonStrings import kDefaultNotificationSubject
from appCommon.ResponseSubstitution import substituteResponseVars, faceNameForObjs

from appCommon.hostedServices.ServicesClient import ServicesClient

from .ClipManager import ClipManager
from .DataManager import DataManager
# _kOldClipHours is private, but the blocked-queue report quotes it and the
# two must not drift apart: it is the deadline after which a blocked queue
# silently discards everything waiting in it.
from .ResponseDbManager import ResponseDbManager, _kOldClipHours
from appCommon.hostedServices.IftttClient import IftttClient
from .DebugLogManager import DebugLogManager

from . import MessageIds

# Private, but _isExpectedVideoWait's quiet window must not drift from it: the
# recorder will not cut a segment younger than this, so footage for an event
# can take this long to reach the archive.
from videoLib2.python.StreamReader import _kRemuxMinSegAgeForCutSecs

# Constants...
_kLogName = "Response.log"

# We'll name temporary clips like this, with %d as the milliseconds.  It's not
# super important for this to be unique (we only create one at a time), so I'm
# not too worried about managing to create two clips within the same clock tick.
# The only reason to include time in the filename is that if, somehow, someone
# makes one of the files busy (by mucking around in our temp directory), it
# won't impede our ability to make future clips.
_kClipTemplateMap = {
    kFtpProtocol:         "Clip-%d.mp4",
    kLocalExportProtocol: "Clip-%d.mp4",
}

# Set to True for a bit more debugging info...
_kDebug = False


# How long we'll keep trying to get video for the email...
# NOTE: clips finalize on 2-minute boundaries (StreamReader.flush is a no-op),
# so a clip can take up to ~120s to become available.  The video timeout must
# exceed that or local/FTP export fails for events early in a clip window.
_kGetImageRetrySleep = .5
# With on-demand flush (StreamReader finalizes the current clip when a response
# asks for it), the clip becomes available in ~2-4s; allow first-attempt success.
_kGetImageTimeoutSeconds = 10
_kGetVideoTimeoutSeconds = 130


# How long we'll keep trying to send...
_kSendEmailRetrySleepSeconds = (60 * 2)
_kSendEmailNumTries          = 3



# The frequency at which we ping the back end to inform that we're still alive
_kPingSecInterval = 120

# How long we'll sleep waiting for a message; we won't retry anything faster
# than this unless another message comes in...
_kQueueSleepSeconds = 60


# Limit timeout to 30 seconds, 10 wasn't always enough on Windows.
_kFtpSocketTimeout = 30.0

# We'll delay processing things from the response DB by this long when we get
# a failed 'send clip'...
_kDelayForFailedSendClip = 60.0

# A clip that fails to send does NOT get its row advanced -- processAt is not
# bumped and failures is not incremented (ResponseDbManager.clipFailed does
# exactly that, and has deliberately never been given a caller).  Since
# getNextClipToSend orders by uid, the same clip comes back every poll and
# holds up every later clip for its protocol until _clearOldClips discards the
# whole backlog at _kOldClipHours.  That is the intended design -- the
# assumption is that the link is down and the others would fail too -- but it
# used to be entirely silent.  These control how loudly we say it is
# happening; they do not change the retrying itself.
_kBlockedReportAttempts = 3         # report the first N attempts, then...
_kBlockedReportInterval = (60 * 15) # ...at most this often, in seconds.
_kBlockedEscalateAfter  = (60 * 60) # blocked longer than this logs as ERROR.


# Default size of image to email / send.
_kDefaultResponseRes = (320, 240)

# Note that this is run through strftime first, then clipInfoDict (hence
# the %% for the clipInfoDict codes).  Also note the %L extension to strftime
# for milliseconds, which isn't standard.
# IMPORTANT: This name must be unique enough that clips won't clobber each
# other...
_kFtpNameTemplate = "%Y-%m-%d-%H%M%S-%L-%%(ruleName)s"
_kStrftimeMsCode = "%L"


_kEmailBodySingle = (
    """The rule "%s" triggered a video event at %s on %s.\n\n"""
)
_kEmailBodyMultiple = (
    """The rule "%s" triggered %d video events at %s on %s.\n\n"""
)

_kEmailErrorFormatStr   = """Error sending email for the rule "%s": "%s". Exception: %s"""
_kEmailWarningFormatStr = (
    """Error sending email for the rule "%s": "%s". Will retry %d time(s). Exception: %s"""
)

_kEmailNotConfiguredErrorStr = (
    """Email response requested, but email is not configured."""
)

# This mapping is used in error messages, since we often share code (and error
# messages) between the different "send clips" pieces...
_kSendClipProtocolToName = {
    kFtpProtocol: "FTP",
    kLocalExportProtocol: "Local Export",
}
_kSendClipErrorFormatStr = 'Error uploading via %s for the rule "%s" (%s, %s).'

# Notification content, as shown to the user on reception on a device. There is
# more (structured) data sent along in a JSON container.
_kNotificationFormatStr = 'Alert for rule "%s"'

# Notification retry sleep times.
_kNotificationRetries = [2, 4, 20, 90]

# Outcome of the pre-fetch check for one instant of video.
_kVideoCovered = 'covered'   # a clip covers the instant -- go ahead
_kVideoPending = 'pending'   # the recorder has not reached it yet -- retry
_kVideoMissing = 'missing'   # the recorder is past it, nothing covers it

# Matches ClipManager.getFileAt()'s own default, so that asking "is this
# instant covered?" and then fetching the frame agree on the answer.
_kFrameSearchToleranceMs = 3000

# How far past the wanted instant the recorder must have registered before a
# miss is called permanent rather than "not yet".  Segments normally register
# in order, but a gap-black-fill re-encode can hold one back for as long as its
# 30s budget, so leave that much margin before declaring a hole.
_kHoleConfirmMs = 30000

# Number of seconds to wait between clip sender DB polls.
_kClipSenderPollInterval = 5

# Number of seconds to wait between purging stored push notifications.
_kPushNotificationsPurgeIntervalSecs = 3600

# Maximum number of push notifications to purge at once.
_kMaxPushNotificationsPurge = 10000

# Maximum age a stored push notification should have (in seconds, 10 days).
_kPushNotificationMaxAgeSecs = 10 * 24 * 3600

_kExecutorPollTime = 0.1
_kExecutorRetryTime = 5 # Retry after 5 seconds
_kExecutorMaxAllocAttempts = 1 / _kExecutorPollTime # do not stall the main loop for more than 1 sec
# How many times a message may be re-queued because no worker was free before we
# abandon it.  These re-queues deliberately do NOT advance tryNum (a failed
# allocation is not an attempt), so without a separate ceiling a permanently
# saturated pool would retry the same message forever.  20 is ~100s of solid
# saturation at _kExecutorRetryTime.
_kMaxExecutorRetries = 20

###############################################################
def runResponseRunner(backEndQueue, responseQueue, clipMgrPath, dataMgrPath,
                      responseDbMgrPath, videoDir, tmpDir, logDir, configDir,
                      ftpSettings, localSettings, notificationSettings,
                      servicesToken):
    """Create and start a ResponseRunner process.

    @param  backEndQueue         A queue to add back end messages to.
    @param  responseQueue        A queue to listen for control messages on.
    @param  clipMgrPath          Path to the clip database.
    @param  dataMgrPath          Path to the object database.
    @param  responseDbMgrPath    Path to the response database manager.
    @param  videoDir             Path to the folder where clips are stored.
    @param  tmpDir               Path to a place to store temporary files.
    @param  logDir               Directory where log files should be stored.
    @param  configDir            Directory to search for config files.
    @param  ftpSettings          Dictionary of FTP settings.
    @param  localSettings        Dictionary of local export settings.
    @param  notificationSettings Dictionary for notification settings.
    @param  servicesToken        The current services token or None.
    """
    responseRunner = ResponseRunner(backEndQueue, responseQueue, clipMgrPath,
            dataMgrPath, responseDbMgrPath, videoDir, tmpDir, logDir, configDir,
            ftpSettings, localSettings, notificationSettings, servicesToken)
    responseRunner.run()


##############################################################################
def _jsonEncodeDict(d):
    """ Quick and dirty JSON encoding for simple dictionaries containing
    numbers and strings (Unicode supported). UTF-8 strings won't be touched,
    so the receiver needs to be aware of proper encoding of the whole
    JSON document. This was written due to the lack of a JSON library in
    Python 2.5 and should be replaced as soon as possible. Too primitive to
    move into the toolbox btw, do NOT be tempted.

    @param d The dictionary to encode.
    @return The JSON expression.
    """
    result = ""
    for k, v in d.items():
        item = '"%s":' % k
        if isinstance(v, (int, int, float, complex)):
            item += str(v)
        elif isinstance(v, str):
            v = v.replace('"', '\\"')
            enc = ""
            for c in v:
                o = ord(c)
                enc += ("\\u%04X" % o) if o > 127 else c
            item += '"%s"' % enc
        else:
            v = str(v).replace('"', '\\"')
            item += '"%s"' % v
        if "" == result:
            result = "{%s" % item
        else:
            result += ",%s" % item
    result += "}"
    return result


###########################################################
def _probeVideoAt(clipMgr, camLoc, ms, requireCoverage):
    """ Ask the clip db about one instant of video.

    Without requireCoverage this is the historical test: MAX(lastMs) for the
    camera >= ms.  That only asks whether the camera has ANY footage at or
    after ms -- not whether ms is covered -- so it opens as soon as a LATER
    segment registers across a recording hole, which is how a retryable wait
    turned into an immediate, unretryable "no frame available".

    @param  clipMgr          ClipManager object.
    @param  camLoc           Name of the camera.
    @param  ms               Time (in ms) we want video for.
    @param  requireCoverage  If True, demand a clip that actually covers ms.
    @return state            One of _kVideoCovered/_kVideoPending/_kVideoMissing.
    """
    recentMs = clipMgr.getMostRecentTimeAt(camLoc)
    if not requireCoverage:
        return _kVideoCovered if recentMs >= ms else _kVideoPending

    if clipMgr.getFileAt(camLoc, ms, _kFrameSearchToleranceMs):
        return _kVideoCovered

    # Nothing covers ms.  If the recorder has since registered footage well
    # past it, no later arrival is going to fill it in: it is a hole.
    if recentMs >= ms + _kHoleConfirmMs:
        return _kVideoMissing
    return _kVideoPending


###########################################################
def _waitForVideoAt(clipMgr, event, needFlush, camLoc, ms, queue, logger,
                    maxDelay, pollDelay, requireCoverage=False):
    """ Wait until we know timestamp is accessible in clip db

    @param clipMgr          ClipManager object
    @param camLoc           Name of the camera.
    @param event            Shutdown event object
    @param ms               Time (in ms) we need to be in the db
    @param requireCoverage  See _probeVideoAt().
    @return state           One of _kVideoCovered/_kVideoPending/_kVideoMissing.
    """
    state = _probeVideoAt(clipMgr, camLoc, ms, requireCoverage)
    if state == _kVideoPending:
        if needFlush:
            # TODO: we might need one lock per camera to avoid multiple flush
            #       requests triggered by different threads ...
            logger.info("requesting flush for camera '%s' ..." % camLoc)
            queue.put([MessageIds.msgIdFlushVideo, camLoc, ms])
        time1 = time.time()
        while (time.time() - time1) < maxDelay:
            state = _probeVideoAt(clipMgr, camLoc, ms, requireCoverage)
            if state == _kVideoCovered:
                logger.debug("waited for %.2f s for clip to become available" % (time.time() - time1) )
                return state
            if state == _kVideoMissing:
                # A confirmed hole never fills; stop burning the poll window.
                return state
            if event is None:
                time.sleep(pollDelay)
            else:
                event.wait(pollDelay)
                if (event.is_set()):
                    return _kVideoPending
    return state


###########################################################
def _waitUntilVideoAvailable(clipMgr, event, needFlush, camLoc, ms, queue, logger, maxDelay, pollDelay):
    """ Bool form of _waitForVideoAt(), for the callers that only need go/no-go.

    @return videoAvailable  True if the camera has footage at or after ms.
    """
    return _waitForVideoAt(clipMgr, event, needFlush, camLoc, ms, queue,
                           logger, maxDelay, pollDelay) == _kVideoCovered


###########################################################
def _nextRetryTime(tryNum, logger, what):
    """Schedule the next attempt of a retryable response action.

    tryNum starts at 1, and _kNotificationRetries[tryNum-1] is the wait BEFORE
    attempt tryNum+1 -- so the table is spent only once tryNum exceeds its
    length.  The old per-site guard was `tryNum >= len(...)`, which gave up one
    attempt early and left the final 90s rung permanently unreachable.
    Measured 2026-08-21 (tools/snapgap_audit.py): of 114 snapshot failures
    that day, 82 had video that did register later, needing a median of 7.3s
    and at most 35s beyond the point where the ladder gave up -- i.e. entirely
    inside that dead rung.

    @param  tryNum      The try number that just failed; starts at 1.
    @param  logger      Logger for the give-up message.
    @param  what        Action name for the give-up message, e.g. "snapshot".
    @return retryAfter  Time of the next attempt, or None to give up.
    """
    if tryNum > len(_kNotificationRetries):
        logger.error("maximum number of retries, giving up this %s" % what)
        return None
    return time.time() + _kNotificationRetries[tryNum - 1]


###########################################################
def _isExpectedVideoWait(wantMs):
    """Whether "no video yet" for wantMs is still the normal state of affairs.

    The recorder will not cut a segment younger than
    _kRemuxMinSegAgeForCutSecs, so until that long after the wanted instant its
    footage may simply not have rolled into the archive yet.  At 60 (a whole
    segment) the on-demand cut is effectively off, and nearly every snapshot
    fails its first three attempts before the fourth, ~60s in, succeeds.
    Measured 2026-09-11: 21 warnings for 11 snapshots, every one ending within
    42s of the event.  Those misses are logged at debug; one past the window
    still warns.

    @param  wantMs    The instant (in ms) whose video is being waited for.
    @return expected  True while still inside the window.
    """
    return (time.time() * 1000 - wantMs) < _kRemuxMinSegAgeForCutSecs * 1000



##############################################################################
class SynchronizedQueue:
    """ Wrapper around an IPC queue to make the essential calls thread-safe. """
    ###########################################################
    def __init__(self, instance):
        """TODO"""
        self._instance = instance
        self._lock = threading.RLock()

    ###########################################################
    def put(self, *args):
        """TODO"""
        self._lock.acquire()
        try:
            self._instance.put(*args)
        finally:
            self._lock.release()


##############################################################################
class SynchronizedResponseDbManager:
    """ Wrapper around the response DB manager to make it thread-safe. All of
    the sender threads share the same instance, hence this protection. We could
    give each sender its own instance, but since they all poll if might cause
    some issues with actual DB locking, hence we don't do it right now until we
    now better and have time to prove that it works and has benefits.

    NOTE: only the methods actually called by this module are protected!

    @see ResponseDbManager.ResponseDbManager
    """
    ###########################################################
    def __init__(self, instance):
        """Constructor for wrapping an instance.

        @param instance The instance to wrap"""
        self._instance = instance
        self._lock = threading.RLock()
    ###########################################################
    def areResponsesPending(self, *args):
        self._lock.acquire()
        try:
            return self._instance.areResponsesPending(*args)
        finally:
            self._lock.release()
    ###########################################################
    def getNextClipToSend(self, *args):
        self._lock.acquire()
        try:
            return self._instance.getNextClipToSend(*args)
        finally:
            self._lock.release()
    ###########################################################
    def countQueueLength(self, *args):
        self._lock.acquire()
        try:
            return self._instance.countQueueLength(*args)
        finally:
            self._lock.release()
    ###########################################################
    def clipDone(self, *args):
        self._lock.acquire()
        try:
            self._instance.clipDone(*args)
        finally:
            self._lock.release()
    ###########################################################
    def addPushNotification(self, *args):
        self._lock.acquire()
        try:
            return self._instance.addPushNotification(*args)
        finally:
            self._lock.release()
    ###########################################################
    def purgePushNotifications(self, *args):
        self._lock.acquire()
        try:
            return self._instance.purgePushNotifications(*args)
        finally:
            self._lock.release()
    ###########################################################
    def lockForever(self):
        """ Locks the instance, never releases it. Only used for shutdown. """
        self._lock.acquire()


##############################################################################
class ClipSender(threading.Thread):
    """ Base class to achieve asynchronous execution of response tasks. Runs
    as a single thread, getting the items to send via a queue.
    """
    ###########################################################
    def __init__(self, protocol, logger, backEndQueue, execContext,
                 configDir, tmpDir, cameraResolutions, responseDbMgr,
                 initialSettings):
        """ Creates a new sender thread. Must be started manually though.

        @param protocol             The name of the protocol used for sending.
        @param logger               The logger instance to use.
        @param backEndQueue         The queue to talk to the backend.
        @param execContext          The context for data manager/clip manager
        @param dataMgrPath          Path to the object database.
        @param videoDir             Path to the folder where clips are stored.
        @param configDir            Directory to search for config files.
        @param tmpDir               Directory for temporary stuff.
        @param cameraResolutions    Shared dictionary to determine camera
                                    resolutions. Only for simple gets.
        @param responseDbMgr        Response DB access.
        @param initialSettings      The initial settings specific to the type.
        """
        threading.Thread.__init__(self)
        self.protocol = protocol
        self._logger = logger
        self._backEndQueue = backEndQueue
        self._executionContext = execContext
        self._configDir = configDir
        self._tmpDir = tmpDir
        self._cameraResolutions = cameraResolutions
        self._responseDbMgr = responseDbMgr
        self._delayResponsesUntil = 0
        self._settings = initialSettings
        self.shutdown = threading.Event()

        # Head-of-line tracking; see _noteClipBlocked.  Touched only by this
        # sender's own thread.
        self._blockedUid = None
        self._blockedSince = 0.0
        self._blockedAttempts = 0
        self._blockedLastLog = 0.0

    ###########################################################
    def updateSettings(self, newSettings):
        """ Update settings. The settings will be access get-only, hence in a
        thread-safe fashion.

        @param newSettings The new settings to use.
        """
        self._settings = newSettings
        self._delayResponsesUntil = 0

    ###########################################################
    def _send(self, clipPath, ruleName, startTime, stopTime):
        """ To be implemented by the inherited classes. NOP for this class.

        CONTRACT -- both subclasses must honour this, and for a long time they
        did not agree:

          - Return True only if the clip really reached its destination.
          - Return False for a failure that waiting cannot fix (a rule with no
            target configured, say).  The caller gives up on the clip.
          - RAISE for a failure that might fix itself (network down, share
            unmounted, disk full).  The caller retries, which blocks this
            protocol's queue until it succeeds -- see _noteClipBlocked.

        Returning normally after a failed delivery is the one thing that must
        not happen: the caller treats it as a successful send, records it in
        lastSentInfo, deletes the queue row and unlinks the temp clip.

        @param clipPath File path of the clip.
        @param ruleName The name of the rule for which the item got created.
        @param startTime When the event started.
        @param stopTime When the event stopped.
        @return sent    True if the clip was delivered.
        """
        return False


    ###########################################################
    def _processClip(self, uid, camLoc, ruleName, startTime, stopTime,
                     playStart, previewMs, objList, startList):
        """ Process a single clip to send.

        @param camLoc    Name of the camera.
        @param ruleName  Name of the rule.
        @param startTime Start time (in ms) of the clip to send.
        @param stopTime  Stop time (in ms) of the clip to send.
        @param playStart Time (in ms) that the clip should start playing.
        @param previewMs Time (in ms) that the thumbnail should show.
        @param objList   List of DB IDs in the clip.
        @param startList List of start times of triggers in the clip.
        @param uid       ID of the clip in the response database (for removal).
        """

        clipMgr = self._executionContext.getClipMgr()
        dataMgr = self._executionContext.getDataMgr()

        canProceed = _waitUntilVideoAvailable(clipMgr, self.shutdown, True,
                                                camLoc, stopTime,
                                                self._backEndQueue, self._logger,
                                                _kGetVideoTimeoutSeconds, _kGetImageRetrySleep)
        if not canProceed:
            if self.shutdown.is_set():
                return
            # Not an error.  Why?  ...this often happens when you turn off
            # your camera.  We want to add some padding to the last clip,
            # but probably won't be able to get all of our padding.
            # ...we'll just hit the timeout, then make the best clip we can
            #self._logger.warning("Not all video was available to send")
            #return True

        clipTemplate = _kClipTemplateMap[self.protocol]
        clipPath = os.path.join(self._tmpDir,
                                clipTemplate % int(time.time() * 1000))
        wantRetry = False
        wasSent = False
        try:
            res = self._cameraResolutions.get(camLoc, _kDefaultResponseRes)
            realStartTime, realStopTime = dataMgr.openMarkedVideo(camLoc,
                startTime, stopTime, playStart, objList, res, False, False)
            if (realStartTime == -1) or (realStopTime == -1):
                self._logger.error("Error opening video: (%s, %d, %d)" % (
                                   camLoc, startTime, stopTime))
                # Don't retry--just give up; error will not fix itself.
            else:
                success = dataMgr.saveCurrentClip(clipPath, realStartTime,
                        realStopTime, self._configDir)
                if success:
                    # Stamp QuickTime create-date atoms (MediaCreateDate +
                    # TrackCreateDate) from startTime -- the same event time the
                    # dated filename in _send() uses -- so the clip's metadata
                    # matches its yyyy-mm-dd-hhmmss name.  Best-effort: a failure
                    # never blocks delivery.
                    try:
                        from videoLib2.python.ClipUtils import stampMp4CreationTime
                        stampMp4CreationTime(clipPath, startTime,
                                             self._logger.warning)
                    except Exception:
                        pass
                    # bool(): a subclass that forgets to return anything
                    # gets "not sent", never a false success.
                    wasSent = bool(self._send(clipPath, ruleName, startTime,
                                              stopTime))
                    if not wasSent:
                        self._logger.error(
                            "Clip for rule '%s' was NOT delivered via %s and "
                            "will not be retried; giving up on it." %
                            (ruleName, self.protocol))
                else:
                    self._logger.error("Error making clip: (%s, %s, %d, %d)" % (
                                       camLoc, clipPath, realStartTime,
                                       realStopTime))
                    # Don't retry--just give up; error will not fix itself.
        except:
            self._logger.error(_kSendClipErrorFormatStr % (
                           _kSendClipProtocolToName[self.protocol],
                           ruleName, sys.exc_info()[1], traceback.format_exc()), exc_info=True)
            wantRetry = True
        finally:
            try:
                if os.path.exists(clipPath):
                    os.unlink(clipPath)
            except Exception:
                self._logger.warning("Unable to delete '%s'" % clipPath)

        if wantRetry:
            self._delayResponsesUntil = time.time() + _kDelayForFailedSendClip
            self._noteClipBlocked(uid, ruleName, camLoc)
        else:
            self._noteClipUnblocked()
            self._responseDbMgr.clipDone(uid, wasSent)


    ###########################################################
    def _noteClipBlocked(self, uid, ruleName, camLoc):
        """Log that a clip is failing and holding up everything behind it.

        Reporting only -- the retry behaviour is unchanged.  See the note on
        _kBlockedReportAttempts for why a failing clip blocks its protocol's
        whole queue rather than stepping aside.

        Rate-limited so a genuine multi-hour outage leaves a readable trail
        rather than a line a minute, and escalated to ERROR once it has gone
        on long enough that it is not just a blip.

        @param  uid       The uid of the clip that failed.
        @param  ruleName  The rule that produced it.
        @param  camLoc    The camera it came from.
        """
        now = time.time()
        if uid != self._blockedUid:
            # A different clip than last time: either the queue moved on, or
            # this is the first failure.  Start counting again.
            self._blockedUid = uid
            self._blockedSince = now
            self._blockedAttempts = 0
            self._blockedLastLog = 0.0

        self._blockedAttempts += 1
        blockedFor = now - self._blockedSince

        if (self._blockedAttempts > _kBlockedReportAttempts and
                (now - self._blockedLastLog) < _kBlockedReportInterval):
            return
        self._blockedLastLog = now

        try:
            queued = self._responseDbMgr.countQueueLength(self.protocol)
            behind = ", %d clip(s) waiting behind it" % (queued - 1) \
                     if queued > 1 else ""
        except Exception:
            behind = ""

        msg = ("%s send queue is BLOCKED: clip uid=%s (rule '%s', camera "
               "'%s') has failed %d time(s) over %.0f min and is retried "
               "every %.0fs%s.  Nothing else will be sent via %s until it "
               "succeeds or ages out after %d hours." %
               (self.protocol, uid, ruleName, camLoc, self._blockedAttempts,
                blockedFor / 60.0, _kDelayForFailedSendClip, behind,
                self.protocol, _kOldClipHours))

        if blockedFor >= _kBlockedEscalateAfter:
            self._logger.error(msg)
        else:
            self._logger.warning(msg)


    ###########################################################
    def _noteClipUnblocked(self):
        """Clear the head-of-line tracking, and say so if it had complained."""
        if self._blockedUid is not None:
            if self._blockedAttempts > _kBlockedReportAttempts:
                self._logger.warning(
                    "%s send queue is moving again after %.0f min blocked on "
                    "clip uid=%s (%d failed attempts)." %
                    (self.protocol, (time.time() - self._blockedSince) / 60.0,
                     self._blockedUid, self._blockedAttempts))
            self._blockedUid = None
            self._blockedSince = 0.0
            self._blockedAttempts = 0
            self._blockedLastLog = 0.0

    ###########################################################
    def run(self):
        """ Thread main loop. Polls on the response database asking for clips
        of the particular protocol. If it gets one it tries to send it.
        """
        self._logger.info("sender '%s' ready" % self.protocol)
        while not self.shutdown.is_set():
            # check if there are any responses, if not wait
            if not self._responseDbMgr.areResponsesPending(self.protocol):
                self.shutdown.wait(_kClipSenderPollInterval)
                continue
            # get the next response, wait in the unlikely case of nothingness
            clip = self._responseDbMgr.getNextClipToSend(self.protocol)
            if clip is None:
                self.shutdown.wait(_kClipSenderPollInterval)
                continue
            # delay if some former operation recommended some idle time
            delay = max(0, self._delayResponsesUntil - time.time())
            self.shutdown.wait(delay)
            if self.shutdown.is_set():
                break
            # now try to get the clip material and then send it out ...
            self._processClip(*clip)

        self._logger.info("sender '%s' exited" % self.protocol)


##############################################################################
class ResponseWorkerThread(threading.Thread):
    def __init__(self, owner, execContext, msgId):
        threading.Thread.__init__(self)
        self._queue = queue.Queue()
        self._owner = owner
        self._msgId = msgId
        self._executionContext = execContext
        self._creationTime = time.time()

    def logState(self):
        action = self._executionContext._currentAction
        actionState = "undefined"
        if action:
            actionState = action.getProgressStr()

        self._owner._logger.info("Worker thread has been processing msgId=%d for %.2f, currently on %s" % (self._msgId, time.time()-self._creationTime, actionState) )

    def getType(self):
        return self._msgId

    def queueAction(self, tryNum, msg):
        self._queue.put((msg, tryNum))

    def run(self):
        try:
            msg, tryNum = self._queue.get(True, timeout=_kQueueSleepSeconds)
            self._owner._processMessage(self._executionContext, msg, tryNum,
                                        False)
        finally:
            # This context is ours alone -- _allocateExecutor cloned it for us
            # -- so the connections go when we do.  Without this they lingered
            # until the collector happened to run.
            self._executionContext.close()


##############################################################################
def _getFtpName(clipPath, ruleName, startTime, stopTime):
    """ Create a more readable output name to store FTP clips.

    @param  clipPath      Path to the source clip that was created that
                          we wish to send.  We will send this via FTP,
                          though we'll give it a different name based
                          on the _kFtpNameTemplate
    @param  ruleName      Name of the rule.
    @param  startTime     Start time (in ms) of the clip to send.
    @param  stopTime      Stop time (in ms) of the clip to send.
    @return startTimeStr  A string representing the start time.
    @return stopTimeStr   A string representing the stop time.
    @return dstName       A name for the destination file.
    """
    startTimeStr = getTimeAsString(startTime)
    stopTimeStr = getTimeAsString(stopTime)

    # Resolve the template name into a real name.
    startTimeSec, startTimeMsec = divmod(startTime, 1000)
    dstName = _kFtpNameTemplate
    dstName = dstName.replace(_kStrftimeMsCode, str(startTimeMsec))
    dstName = formatTime(dstName, time.localtime(startTimeSec))
    dstName = (dstName % {'ruleName': ruleName}) + \
              os.path.splitext(clipPath)[1]

    return startTimeStr, stopTimeStr, dstName


##############################################################################
class LocalClipSender(ClipSender):
    """ Sender which simply moves the clips into a different directory on the
    local file system. """
    ###########################################################
    def __init__(self, *args):
        """TODO"""
        ClipSender.__init__(self, kLocalExportProtocol, *args)

    ###########################################################
    def _send(self, clipPath, ruleName, startTime, stopTime):
        """TODO"""
        # Log that we're queuing this up for sending.
        startTimeStr, stopTimeStr, dstName = \
            _getFtpName(clipPath, ruleName, startTime, stopTime)

        logInfo = "Sending clip via local copy for rule \"%s\": %s - %s" % (
                ruleName, startTimeStr, stopTimeStr
            )

        targetDir = self._settings.get(ruleName.lower())
        if not targetDir:
            # Configuration, not weather: retrying cannot help, so report the
            # clip as undelivered rather than blocking the queue on it.
            self._logger.error("%s: No directory configured for rule %s" %
                    (logInfo, str(self._settings)))
            return False

        if not os.path.exists(targetDir):
            try:
                os.makedirs(targetDir)
            except OSError:
                # If we're here, it's most likely because os.path.exists returned
                # false even though the directory does exist. According to the
                # Python docs, this can happen if this calling process does not
                # have permission to check existence of the that dir. So, we
                # log the error, as well as permission information to make sure
                # this is the case, or if a deeper issue is taking place here.
                # We shouldn't have to worry about os.access raising an
                # exception, even if the targetDir doesn't exist, so it should
                # be safe to call inside the exception handler.
                self._logger.error(
                    "%s: Permissions on '%s': existence=%s, read=%s, write=%s" %
                    (
                        logInfo,
                        targetDir,
                        os.access(targetDir, os.F_OK),
                        os.access(targetDir, os.R_OK),
                        os.access(targetDir, os.W_OK),
                    ),
                    exc_info=True
                )

        targetPath = os.path.join(targetDir, dstName)

        # Try a move, followed by a copy if that fails.  A move failure on its
        # own is not fatal -- the copy is the fallback -- but if neither gets
        # the file there, that MUST reach our caller.  It used to be swallowed,
        # which had the caller record the clip as sent and then delete it.
        try:
            shutil.move(clipPath, targetPath)
        except Exception as e:
            self._logger.warning("%s: move failed, trying a copy - %s" %
                                 (logInfo, str(e)))

        if not os.path.exists(targetPath):
            shutil.copy(clipPath, targetPath)

        if not os.path.exists(targetPath):
            # Neither call raised, yet nothing arrived.  Should not happen;
            # refuse to call it a success if it does.
            raise IOError("%s: nothing arrived at '%s'" %
                          (logInfo, targetPath))

        self._logger.info( "%s: success" % logInfo )
        return True


##############################################################################
class FtpClipSender(ClipSender):
    """ Sender which takes clips and uploads them to an FTP site. """
    ###########################################################
    def __init__(self, *args):
        """TODO"""
        ClipSender.__init__(self, kFtpProtocol, *args)

    ###########################################################
    def _send(self, clipPath, ruleName, startTime, stopTime):
        """TODO"""
        # Log that we're queuing this up for sending.
        startTimeStr, stopTimeStr, dstName = \
            _getFtpName(clipPath, ruleName, startTime, stopTime)
        self._logger.info(
            "Sending clip via FTP for rule \"%s\": %s - %s" % (
                ruleName, startTimeStr, stopTimeStr
            )
        )
        # Send via FTP.  Any exceptions that happen will be propagated up to
        # our caller, who will handle retrying. The only exceptions that we do
        # swallow here are any that come from the `quit()` method from the `FTP`
        # object, because there is a chance it might use a socket in an invalid
        # state.
        ftpConfig = self._settings
        ftpObj = ftplib.FTP(timeout=_kFtpSocketTimeout)
        try:
            # Prepare for file delivery...
            ftpObj.connect(ftpConfig['host'], int(ftpConfig['port']))
            ftpObj.login(ftpConfig['user'], ftpConfig['password'])
            ftpObj.cwd(ftpConfig['directory'])
            ftpObj.set_pasv(ftpConfig['isPassive'])

            # Send the file...
            clipFp = open(ensureUtf8(clipPath), 'rb')
            try:
                ftpObj.storbinary('STOR %s' % (dstName,), clipFp)
            finally:
                clipFp.close()

            # Tell the server we are finished...
            try:
                ftpObj.quit()
            except Exception:
                # The ftp object attempts to send a command to the server when
                # quitting; if the socket it tries to use is not connected or
                # doesn't exist, it will throw an exception. This exception
                # is safe to ignore. We will, however, need to call `close()`
                # on this object in a `finally` clause later to ensure proper
                # cleanup.
                pass
            self._logger.info(
                "...sent clip via FTP for rule \"%s\": %s - %s" % (
                    ruleName, startTimeStr, stopTimeStr
                )
            )
        finally:
            ftpObj.close()

        # Anything that went wrong above propagated; getting here means the
        # STOR completed.  Stated explicitly because the caller now uses the
        # return value rather than assuming success -- see ClipSender._send.
        return True

##############################################################################
class ExecutionContext(object):
    """ Since DataManager and ClipManager aren't thread-safe, each threaded entity
        needs to have its own copy of these two.
        Whenever new thread that needs to use those is created, we will clone the
        existing context from the creating thread.
    """
    ###########################################################
    def __init__(self, logger, clipMgrPath, dataMgrPath, videoDir):
        self._clipMgr = ClipManager(logger)
        self._clipMgr.open(clipMgrPath)
        self._dataMgr = DataManager(logger, self._clipMgr,
                                    videoDir)
        self._dataMgr.open(dataMgrPath)

        self._clipMgrPath = clipMgrPath
        self._dataMgrPath = dataMgrPath
        self._logger = logger
        self._currentAction = None
        self._videoDir = videoDir

    ###########################################################
    def clone(self):
        return ExecutionContext(self._logger, self._clipMgrPath, self._dataMgrPath, self._videoDir)

    ###########################################################
    def close(self):
        """Close both managers' database connections.

        Every worker thread clones a context, and each clone opens a
        ClipManager and a DataManager.  They used to be dropped on the floor
        for the garbage collector, which meant an unpredictable number of live
        SQLite connections against a WAL database that several processes
        already share.  Safe to call more than once.
        """
        for mgr in (self._dataMgr, self._clipMgr):
            if mgr is None:
                continue
            try:
                mgr.close()
            except Exception:
                self._logger.warning("Failed to close a manager",
                                     exc_info=True)
        self._dataMgr = None
        self._clipMgr = None

    ###########################################################
    def getDataMgr(self):
        return self._dataMgr

    ###########################################################
    def getClipMgr(self):
        return self._clipMgr


##############################################################################
class ActionContext(object):
    """ Action context keeps track of an outstanding action, and its corresponding
        execution context. This allows us to pass control from ResponseRunner's
        _processMessage to a threaded executor and back to ResponseRunner's
        _processMessage (but in the context of a worker thread, this time)

        The class also keeps track of requsts statistics and result, allowing us
        to log some messages with telemetry.
    """
    ###########################################################
    def __init__(self):
        self._executionContext = None
        self._actionName = None
        self._cameraName = None
        self._ruleName = None
        self._eventTime = None
        self._eventDuration = None
        self._attemptNumber = None
        self._uri = None
        self._actionStartTime = None
        self._success = None
        self._descr = None
        self._warnOnly = False
        self._quiet = False
        self._progressStr = "initializing..."

    ###########################################################
    def init(self, actionName, cameraName, ruleName, eventTime, eventDuration, attemptNumber, uri):
        self._actionName = actionName
        self._cameraName = cameraName
        self._ruleName = ruleName
        self._eventTime = eventTime
        self._eventDuration = eventDuration
        self._attemptNumber = attemptNumber
        self._uri = uri
        self._actionStartTime = int(time.time()*1000)
        self._success = None
        self._descr = ""
        self._warnOnly = False
        self._quiet = False

    ###########################################################
    def setProgressStr(self, pstr):
        self._progressStr = pstr
        return True

    ###########################################################
    def getProgressStr(self):
        return self._progressStr

    ###########################################################
    def setStatus(self, success, descr="", warnOnly=False, quiet=False):
        self._success = success
        self._descr = descr
        # warnOnly: this "failure" is an expected, retryable condition (e.g.
        # video not available yet) rather than a real error -- _onActionEnd
        # logs it at warning level instead of error.
        self._warnOnly = warnOnly
        # quiet: expected so early that it is not worth a line at all (see
        # _isExpectedVideoWait) -- _onActionEnd logs it at debug, which the
        # response log does not record unless debug logging is on.
        self._quiet = quiet

    ###########################################################
    def format(self):
        duration = int(time.time()*1000) - self._actionStartTime
        status = "completed successfully" if self._success else "failed"
        evtDuration = " evtDuration=%d" % self._eventDuration if self._eventDuration else ""
        uri = " uri=%s" % self._uri if self._uri else ""
        # Some actions (e.g. iHost commands) carry no event time; guard the
        # subtraction so building this log line never raises and kills the
        # worker thread.
        if self._eventTime is not None and self._actionStartTime is not None:
            triggerDelay = self._actionStartTime - self._eventTime
        else:
            triggerDelay = "n/a"
        msg = "%s (%d) for %s in %s has %s in %dms. triggerDelay=%s%s%s %s" % (self._actionName, self._attemptNumber, self._ruleName, self._cameraName, status, duration,
                                triggerDelay, evtDuration, uri, self._descr)
        return msg


##############################################################################
class ResponseRunner(object):
    """A class for running slow responses."""
    ###########################################################
    def __init__(self, backEndQueue, responseQueue, clipMgrPath, dataMgrPath,
                 responseDbMgrPath, videoDir, tmpDir, logDir, configDir,
                 ftpSettings, localSettings, notificationSettings,
                 servicesToken):
        """Initialize ResponseRunner.

        @param  backEndQueue         A queue to add back end messages to.
        @param  responseQueue        A queue to listen for control messages on.
        @param  clipMgrPath          Path to the clip database.
        @param  dataMgrPath          Path to the object database.
        @param  responseDbMgrPath    Path to the response database manager.
        @param  videoDir             Path to the folder where clips are stored.
        @param  tmpDir               Path to a place to store temporary files.
        @param  logDir               Directory where log files should be stored.
        @param  configDir            Directory to search for config files.
        @param  ftpSettings          Dictionary of FTP settings.
        @param  localSettings        Dictionary of local export settings.
        @param  notificationSettings Dictionary for notification settings.
        @param  servicesToken        The current services token or None.
        """
        # Call the superclass constructor.
        super(ResponseRunner, self).__init__()

        # Setup logging...  SHOULD BE FIRST!
        self._logDir = logDir
        self._logger = getLogger(_kLogName, logDir)
        self._logger.grabStdStreams()

        assert type(clipMgrPath) == str
        assert type(dataMgrPath) == str
        assert type(responseDbMgrPath) == str
        assert type(videoDir) == str
        assert type(tmpDir) == str
        assert type(logDir) == str
        assert type(configDir) == str

        self._cameraResolutions = {}

        self._backEndQueue = SynchronizedQueue(backEndQueue)
        self._commandQueue = responseQueue
        self._notificationSettings = notificationSettings
        self._nextPushNotificationPurge = 0

        self._executionContext = ExecutionContext(self._logger, clipMgrPath, dataMgrPath, videoDir)

        self._responseDbMgr = ResponseDbManager(self._logger)
        self._responseDbMgr.open(responseDbMgrPath)
        self._responseDbMgr = SynchronizedResponseDbManager(self._responseDbMgr)

        self._servicesClient = ServicesClient(self._logger, servicesToken)
        # Lazy-initialised on the first iHost message.  msgIdSendIHost runs
        # with maxExecutors=4, so the construction needs the lock: two
        # concurrent triggers could otherwise each build an IHostController,
        # and the loser's auto-off sweeper thread would never be shut down
        # (shutdown() below only ever sees the one that won).
        self._ihostController = None
        self._ihostControllerLock = threading.Lock()

        # A list of tuples: (retryAfter, tryNum, allocFails, msg)
        # ...this is messages that need to be "retried" at a later time.
        # retryAfter is ABSOLUTE (time.time()+N) in every case.  allocFails
        # counts re-queues caused by there being no free worker thread; those
        # do not advance tryNum, so they need their own ceiling.
        self._retryList = []
        self._retryListLock = threading.RLock()

        # A mapping of message IDs to processing code...
        self._dispatchTable = {
            MessageIds.msgIdQuit:                       ( 0,  self._processQuit ),
            MessageIds.msgIdSendEmail:                  ( 32, self._processSendEmail ),
            MessageIds.msgIdSendPush:                   ( 32, self._processSendPush ),
            MessageIds.msgIdSetCamResolution:           ( 0,  self._processSetCamResolution ),
            MessageIds.msgIdSendClip:                   ( 0,  self._processSendClip ),
            MessageIds.msgIdSetFtpSettings:             ( 0,  self._processSetFtpSettings),
            MessageIds.msgIdSetLocalExportSettings:     ( 0,  self._processSetLocalExportSettings),
            MessageIds.msgIdSetNotificationSettings:    ( 0,  self._processSetNotificationSettings),
            MessageIds.msgIdTriggerIfttt:               ( 32, self._processIfttt),
            MessageIds.msgIdSetServicesAuthToken:       ( 0,  self._setAuthToken),
            MessageIds.msgIdSendWebhook:                ( 32, self._processWebhook),
            MessageIds.msgIdSendIHost:                  ( 4,  self._processSendIHost),
            MessageIds.msgIdTapoAction:                 ( 4,  self._processTapoAction),
            MessageIds.msgIdSetDebugConfig:             ( 0,  self._setDebugConfig),
            MessageIds.msgIdSaveSnapshot:               ( 9,  self._processSaveSnapshot),
        }

        self._executorCounts = {}

        # Create the senders and launch them
        self._senders = {}

        for senderType in ((LocalClipSender, localSettings),
                           (FtpClipSender  , ftpSettings)):

            sender = senderType[0](self._logger, self._backEndQueue,
                self._executionContext.clone(), configDir, tmpDir,
                self._cameraResolutions, self._responseDbMgr,
                senderType[1])

            self._senders[sender.protocol] = sender
            sender.daemon = True
            sender.setName("sender_%s" % sender.protocol)
            sender.start()

        # Track the we last pinged the back end
        self._lastPingTime = 0

        self._workerThreads = []

        self._debugLogManager = DebugLogManager("Response", configDir)

        self._logger.info("ResponseRunner initialized, pid: %d" % os.getpid())


    ###########################################################
    def __del__(self):
        """Free resources used by ResponseRunner"""
        self._logger.info("ResponseRunner exiting")


    ###########################################################
    def run(self):

        """Run a response manager process."""
        self.__callbackFunc = registerForForcedQuitEvents()

        # Enter the main loop
        self._running = True
        while(self._running):

            # Ping the back end if necessary
            now = time.time()
            if now > self._lastPingTime+_kPingSecInterval:
                self._lastPingTime = now
                self._backEndQueue.put([MessageIds.msgIdResponseRunnerPing])

            # Calculate timeout
            currentTime=time.time()
            latestWakeup=_kQueueSleepSeconds+currentTime
            # Under the lock like every other access: _processMessage appends
            # from worker threads.  Snapshot the wakeup times and get out --
            # nothing slow happens in here.
            with self._retryListLock:
                for entry in self._retryList:
                    latestWakeup = min(latestWakeup, entry[0])
            queueTimeout = latestWakeup-currentTime if latestWakeup>currentTime else 0

            # Process pending messages
            try:
                msg = self._commandQueue.get(timeout=queueTimeout)
            except QueueEmpty:
                pass
            else:
                if len(msg):
                    try:
                        self._processMessage(self._executionContext, msg, 1, True)
                    except Exception:
                        self._logger.error("Response exception:" + traceback.format_exc())

            # See if there's anything in our retry list that needs to be
            # tried again...

            # Walk through in forward order (most predictable to user)
            # retrying; but keep track of indices to delete (if we actually
            # retried them)...
            # Take the due entries out first, under the lock, then run them
            # outside it: _processMessage can append to this same list (and
            # takes its own locks), so holding it across the call would be
            # both a long hold and a re-entrancy hazard.  Removing by index
            # afterwards was also unsafe -- a worker appending mid-pass
            # shifted nothing, but a future insert would have.
            due = []
            now = time.time()
            with self._retryListLock:
                keep = []
                for entry in self._retryList:
                    if now >= entry[0]:
                        due.append(entry)
                    else:
                        keep.append(entry)
                if due:
                    self._retryList[:] = keep

            for (retryAfter, tryNum, allocFails, msg) in due:
                try:
                    self._processMessage(self._executionContext, msg, tryNum,
                                         True, allocFails)
                except Exception:
                    self._logger.error("Process message exception",
                                       exc_info=True)

            # Do some little push notification purging.
            self._purgePushNotifications()


        # Stop the iHost auto-off sweeper, if it was started.
        if self._ihostController is not None:
            self._ihostController.shutdown()

        # Bring down the senders
        for _, sender in self._senders.items():
            sender.shutdown.set()

        # Wait a little bit on each sender to exit, this is mostly useful to
        # just let idle threads exit and clean up properly.
        for _, sender in self._senders.items():
            sender.join(1)
        # Prevent the response DB from getting corrupted, a still existing
        # sender thread will then block on this and (because of its daemon
        # nature) be killed at process exit.
        self._responseDbMgr.lockForever()

        # Wait for worker threads
        self._waitForExecutors()

        self._logger.info("all senders are down now")


    ###########################################################
    def _waitForExecutors(self):
        """Block until every worker thread has finished, complaining slowly."""
        _kTimeoutWarning = 30
        counter = 0
        for thrd in self._workerThreads:
            while thrd.is_alive():
                counter += 1
                # `counter % 30` is truthy for 1..29 and falsy AT 30, so the
                # test used to fire 29 seconds out of every 30 -- the exact
                # opposite of the throttle the constant name promises, and a
                # flood in the one situation where the log matters.
                if 0 == counter % _kTimeoutWarning:
                    self._logger.warning("Executor thread still alive, waiting for %d executors for %d seconds!" % (len(self._workerThreads), counter))
                time.sleep(1)

    ###########################################################
    def _cleanUpExecutors(self, logState=False):
        for thrd in list(self._workerThreads):
            if not thrd.is_alive():
                msgId = thrd.getType()
                self._executorCounts[msgId] = self._executorCounts[msgId] - 1
                self._workerThreads.remove(thrd)
            else:
                if logState:
                    thrd.logState()

    ###########################################################
    def _allocateExecutor(self, msgId, tryNum, maxExecutors):
        """ Allocate worker thread.
            Initial implementation: just create a new one. Add pooling later.
        """
        attempt = 0

        while self._executorCounts.get(msgId, 0) > maxExecutors:
            self._cleanUpExecutors(attempt == _kExecutorMaxAllocAttempts)
            attempt += 1
            time.sleep(_kExecutorPollTime)
            if attempt > _kExecutorMaxAllocAttempts:
                self._logger.warning("Failed to allocate executor: messageId=%d, tryNum=%d, maxExecutors=%d" % (msgId, tryNum, maxExecutors))
                # ABSOLUTE, like every other retryAfter in this file.  Returning
                # the bare constant made the run loop's `time.time() >=
                # retryAfter` trivially true, so the backoff was really zero --
                # the entry came straight back on the next pass -- and
                # `min(latestWakeup, 5)` drove the loop's own queue timeout to 0
                # as well, so it stopped blocking on its command queue.
                return None, time.time() + _kExecutorRetryTime

        self._executorCounts[msgId] = self._executorCounts.get(msgId, 0) + 1
        res = ResponseWorkerThread(self, self._executionContext.clone(), msgId)
        self._workerThreads.append( res )
        res.start()
        return res, None


    ###########################################################
    def _processMessage(self, execContext, msg, tryNum, allowAsync,
                        allocFails=0):
        """Process an incoming message.

        @param  msg         The received message.
        @param  tryNum      The attempt # for processing this message; starts at 1.
        @param  allocFails  How many times this message has already been
                            re-queued for want of a free worker thread.
        """
        msgId = msg[0]

        retryAfter = None
        # Whether the handler actually ran.  A message that could not be given a
        # worker thread was never attempted, so it must NOT spend a rung of the
        # retry ladder -- measured 2026-08-22: 551 "Failed to allocate executor"
        # a day, and snapshots giving up at try5-try8 only 12-25s after the
        # event, i.e. the whole ladder consumed by allocation failures while the
        # action never ran once.
        ranTheAction = True

        # Dispatch out messages using dispatch table, passing all of the
        # parameters (except the message ID) as parameters.
        maxExecutors, fn = self._dispatchTable.get(msgId, (0, None))
        if fn is not None:
            if maxExecutors>0 and allowAsync:
                thread, retryAfter = self._allocateExecutor(msgId, tryNum, maxExecutors)
                if not thread is None:
                    thread.queueAction(tryNum, msg)
                else:
                    ranTheAction = False
            else:
                actionCtx = ActionContext()
                actionCtx._executionContext = execContext
                execContext._currentAction = actionCtx
                try:
                    retryAfter = fn(actionCtx, tryNum, *msg[1:])
                finally:
                    execContext._currentAction = None
                self._onActionEnd(actionCtx)
        else:
            self._logger.warning("Unexpected message: %d" % msgId)

        if retryAfter:
            if ranTheAction:
                nextTry, nextAllocFails = tryNum + 1, 0
            else:
                nextTry, nextAllocFails = tryNum, allocFails + 1
            if nextAllocFails > _kMaxExecutorRetries:
                self._logger.error(
                    "no worker thread for messageId=%d after %d attempts over "
                    "~%ds; abandoning it (tryNum=%d was never used)" %
                    (msgId, nextAllocFails, nextAllocFails * _kExecutorRetryTime,
                     tryNum))
            else:
                self._retryListLock.acquire()
                self._retryList.append((retryAfter, nextTry, nextAllocFails, msg))
                self._retryListLock.release()
                # if we've just appended an item to the retry list, processing queue timeout
                # may have changed, and we need to wake it up
                if not allowAsync:
                    self._commandQueue.put([])

        # Collect completed worker threads
        if allowAsync:
            # This ensures thread GC isn't called from worker threads
            self._cleanUpExecutors()


    ###########################################################
    def _processQuit(self, actionCtx, tryNum):
        """Process MessageIds.msgIdQuit.

        @param  tryNum      The attempt number--ignored.
        @return retryAfter  Always returns None; we never retry quit.
        """
        _ = tryNum

        self._logger.info("Received quit message")
        self._running = False

        return None


    ###########################################################
    def _processSetFtpSettings(self, actionCtx, tryNum, ftpSettings):
        """Process MessageIds.msgIdSetFtpSettings.

        @param  tryNum      The attempt number--ignored.
        @param  ftpSettings The FTP settings dictionary.
        @return retryAfter  Always returns None; we never retry this.
        """
        _ = tryNum
        self._senders[kFtpProtocol].updateSettings(ftpSettings)
        return None


    ###########################################################
    def _processSetLocalExportSettings(self, actionCtx, tryNum, exportSettings):
        """Process MessageIds.msgIdSetLocalExportSettings.

        @param  tryNum          The attempt number--ignored.
        @param  exportSettings  The local export settings dictionary.
        @return retryAfter      Always returns None; we never retry this.
        """
        _ = tryNum
        self._senders[kLocalExportProtocol].updateSettings(exportSettings)
        return None


    ###########################################################
    def _processSetNotificationSettings(self, actionCtx, tryNum, notificationSettings):
        """Process MessageIds.msgIdSetNotificationSettings.

        @param  tryNum                The attempt number--ignored.
        @param  notificationSettings  The local export settings dictionary.
        @return retryAfter            Always returns None; we never retry this.
        """
        _ = tryNum
        self._notificationSettings = notificationSettings
        return None


    ###########################################################
    def _processSetCamResolution(self, actionCtx, tryNum, loc, width, height):
        """Process MessageIds.msgIdSetCamResolution.

        @param  tryNum      The attempt number--ignored.
        @param  loc         The camera location.
        @param  width       The width to set the resolution to.
        @param  height      The height to set the resolution to.
        @return retryAfter  Always returns None; we never retry this.
        """
        _ = tryNum

        self._logger.info("Received camera resolution of %dx%d for %s"
                          % (width, height, loc))
        self._cameraResolutions[loc] = (width, height)

        return None


    ###########################################################
    def _processSendClip(self, actionCtx, tryNum):
        """Process MessageIds.msgIdSendClip.

        This is a no-op and is just sent to wake up the ResponseRunner.  We
        actually get our information and handle retries using the response
        database.

        @param  tryNum      The attempt number--ignored.
        @return retryAfter  Always returns None.
        """
        _ = tryNum
        return None


    ###########################################################
    def _setAuthToken(self, actionCtx, tryNum, authToken):
        """Update the user's auth token.

        @param  authToken  The new auth token.
        """
        self._servicesClient.updateToken(authToken)

    ###########################################################
    def _setDebugConfig(self, actionCtx, tryNum, debugConfig):
        """Update the user's auth token.

        @param  debugConfig  The new debugConfig
        """
        self._debugLogManager.SetLogConfig(debugConfig)

    ###########################################################
    def _processIfttt(self, actionCtx, tryNum, camLoc, ruleName, triggerTime,
                      iftttKey='', iftttEventName=''):
        """Send an IFTTT Webhooks trigger for the given rule.

        @param  tryNum         The try number; starts at 1.
        @param  camLoc         The camera location.
        @param  ruleName       The name of the rule containing this response.
        @param  iftttKey       IFTTT Webhooks key.
        @param  iftttEventName IFTTT event name.
        @return retryAfter     If non-None, retry after time.time() > this.
        """
        self._onActionBegin(actionCtx, "IFTTT trigger", camLoc, ruleName, triggerTime, None, tryNum, None)

        result = False
        if not iftttKey or not iftttEventName:
            actionCtx.setStatus(False, "IFTTT key or event name not configured")
        else:
            ic = IftttClient(self._logger, iftttKey, iftttEventName)
            result = ic.trigger(camLoc, ruleName, triggerTime)
            actionCtx.setStatus(result)

        if result:
            return None

        return _nextRetryTime(tryNum, self._logger, "IFTTT trigger")

    ###########################################################
    def _processWebhook(self, actionCtx, tryNum, camLoc, ruleName, uri, ms, contentType, content, obj):
        self._onActionBegin(actionCtx, "webhook trigger", camLoc, ruleName, ms, None, tryNum, None)

        # Resolve the per-event face variable at fire time (the triggering
        # object's attributes land in the DB shortly after first detection).
        if '{SvRuleFace}' in content:
            dataMgr = actionCtx._executionContext.getDataMgr()
            content = content.replace('{SvRuleFace}',
                                      faceNameForObjs(dataMgr, [obj[0]]))

        headers = { 'Content-Type': contentType,
                    'Accept': 'text/plain' }
        hc = HttpClient(kGatewayTimeoutSecs, self._logger)
        status, body, _ = hc.post(uri, content, headers)

        if status is not None:
            if 200 == status:
                actionCtx.setStatus(True)
            else:
                actionCtx.setStatus(False, "%d: %s (%s)" % (status, body, content))
        else:
            actionCtx.setStatus(False)

        # Never retry webhooks
        return None

    ###########################################################
    def _processSendIHost(self, actionCtx, tryNum, camLoc, ruleName,
                          deviceId, deviceName, command, timeout, nightOnly):
        """Send an iHost / eWeLink CUBE command via the IHostController."""
        self._onActionBegin(actionCtx, "ihost command", camLoc, ruleName,
                            None, None, tryNum, None)
        try:
            if self._ihostController is None:
                with self._ihostControllerLock:
                    # Re-check inside the lock: whoever waited here may have
                    # been waiting for the thread that built it.
                    if self._ihostController is None:
                        from backEnd.IHostController import IHostController
                        self._ihostController = IHostController(self._logger)
            self._ihostController.trigger(deviceId, deviceName, command,
                                          timeout, nightOnly)
            actionCtx.setStatus(True)
        except Exception as e:
            self._logger.error("iHost error: %s" % e)
            actionCtx.setStatus(False, str(e))
        return None

    ###########################################################
    def _processTapoAction(self, actionCtx, tryNum, camLoc, ruleName,
                           host, wantSiren, wantLight):
        """Sound the siren and/or light the spotlight on a Tapo camera.

        The account is global and read here rather than carried on the queue.
        TapoController runs the calls on its own single worker thread, so this
        blocks until they finish -- which is what the ResponseRunner's own
        worker expects.
        """
        self._onActionBegin(actionCtx, "tapo camera", camLoc, ruleName,
                            None, None, tryNum, None)
        try:
            from backEnd.TapoConfig import getCredentials
            from vitaToolbox.networking.TapoControl import (
                TapoController, parseTapoTarget, kOpSirenOn, kOpLightOn)

            user, password = getCredentials()
            if not user or not password:
                raise Exception(
                    "No Tapo account configured -- set one in Options -> Tapo")

            # parseTapoTarget wants a URI; the host is all we were given, and
            # the credentials are the override, so a bare rtsp:// URL is enough
            # to reuse its validation (scheme, loopback and hostname checks).
            target = parseTapoTarget("rtsp://%s/" % host, user, password)
            if target is None:
                raise Exception("%s is not a controllable camera address"
                                % host)

            controller = TapoController.instance(self._logger)
            errors = []
            done = threading.Event()
            pending = [op for op, want in ((kOpSirenOn, wantSiren),
                                           (kOpLightOn, wantLight)) if want]
            remaining = [len(pending)]

            def _done(ok, value):
                if not ok:
                    errors.append(str(value))
                remaining[0] -= 1
                if remaining[0] <= 0:
                    done.set()

            for op in pending:
                controller.submit(target, op, _done)

            if pending and not done.wait(60):
                raise Exception("timed out talking to %s" % host)
            if errors:
                raise Exception("; ".join(dict.fromkeys(errors)))

            actionCtx.setStatus(True)
        except Exception as e:
            self._logger.error("Tapo error: %s" % e)
            actionCtx.setStatus(False, str(e))
        return None

    ###########################################################
    def _onActionBegin(self, actionCtx, actionName, camName, ruleName, ms, duration, attempt, uri):
        actionCtx.init(actionName, camName, ruleName, ms, duration, attempt, uri)

    ###########################################################
    def _onActionEnd(self, actionCtx):
        if actionCtx._actionName is None:
            # hasn't been initialized
            return

        if actionCtx._success:
            method = self._logger.info
        elif actionCtx._quiet:
            method = self._logger.debug
        elif actionCtx._warnOnly:
            method = self._logger.warning
        else:
            method = self._logger.error
        method( actionCtx.format() )

    ###########################################################
    def _processSendPush(self, actionCtx, tryNum, camLoc, ruleName, ms):
        """Send a push notification for the given rule.

        @param  tryNum      The try number; starts at 1.
        @param  camLoc      The camera location.
        @param  ruleName    The name of the rule containing this response.
        @param  ms          The ms to include in the push metadata.
        @return retryAfter  If non-None, we'll retry after time.time()
                            returns a value greater than this.
        """

        if not self._notificationSettings.get("enabled", False):
            self._logger.info("notifications disabled")
            return None

        self._onActionBegin(actionCtx, "push notification", camLoc, ruleName, ms, None, tryNum, None)

        # Initiate flush, if needed, but only on the first retry ... do not wait for the video to become avaialble
        clipMgr = actionCtx._executionContext.getClipMgr()
        canProceed = _waitUntilVideoAvailable(clipMgr, None, tryNum == 1,
                                                    camLoc, ms,
                                                    self._backEndQueue, self._logger,
                                                    _kGetImageTimeoutSeconds, _kGetImageRetrySleep)
        if not canProceed:
            # Video isn't available yet ... fail this operation, and schedule a retry
            retryAfter = _nextRetryTime(tryNum, self._logger, "push")
            actionCtx.setStatus(False, "image isn't available yet", warnOnly=True,
                                quiet=(retryAfter is not None and
                                       _isExpectedVideoWait(ms)))
            return retryAfter



        guid = self._notificationSettings.get('gatewayGUID', None)
        password = self._notificationSettings.get('gatewayPassword', None)

        if not guid or not password:
            actionCtx.setStatus(False, "missing gateway credentials!?")
            return None

        # limit the content, see below for why ...
        def limit_text(text, maxLen, ending="..."):
            if len(text) <= maxLen:
                return text
            result = text[0:maxLen] + ending
            return result
        content = ensureUtf8(_kNotificationFormatStr % limit_text(ruleName, 64))

        data = { 'camLoc'  : camLoc,
                 'ruleName': ruleName,
                 'ms': ms }
        jsdata = _jsonEncodeDict(data)
        try:
            uid = self._responseDbMgr.addPushNotification(ensureUnicode(content), \
                                                        ensureUnicode(jsdata))
        except:
            actionCtx.setStatus(False, "error storing push notification: %s" %
                               sys.exc_info()[1])
            return None

        # send the pointer (UID) along, since the actual JSON data could exceed
        # the maximum notification limit (on iOS around 255 chars), what gets
        # send to the client (full set or just the UID) is decided at the
        # gateway...
        data['uid'] = uid
        jsdata = _jsonEncodeDict(data)

        params = { 'action':     'createMessage',
                   'iosBadges': '+1',
                   'content':    ensureUtf8(content),
                   'data':       ensureUtf8(jsdata),
                   'guid':       guid,
                   'password':   password,
                   'svversionstring': kVersionString }

        url = "https://%s%s" % (kGatewayHost, kGatewayPath)
        payload = urllib.parse.urlencode(params)
        headers = { 'Content-Type': 'application/x-www-form-urlencoded;' +
                                    'charset=utf-8',
                    'Accept': 'text/plain' }
        hc = HttpClient(kGatewayTimeoutSecs, self._logger)
        status, body, _ = hc.post(url, payload, headers)

        if status is not None:
            if 200 == status:
                actionCtx.setStatus(True)
                return None
            actionCtx.setStatus(False, "sending failed, %d: %s" % (status, body))
            if 500 != status:
                # something fundamental is wrong, sadly no need to retry
                return None
        else:
            actionCtx.setStatus(False, "invalid API response")

        return _nextRetryTime(tryNum, self._logger, "push")


    ###########################################################
    def _processSaveSnapshot(self, actionCtx, tryNum, ruleName, camLoc,
                             objList, firstMs, lastMs, snapshotPath='',
                             snapshotSubfolder='', drawBoundingBox=False):
        """Save an annotated snapshot to the events folder.

        @param  tryNum             The try number; starts at 1.
        @param  ruleName           The name of the rule.
        @param  camLoc             The camera location.
        @param  objList            Object IDs to draw bounding boxes for.
        @param  firstMs            First ms of the detection range.
        @param  lastMs             Last ms of the detection range.
        @param  snapshotPath       Custom output directory.
        @param  snapshotSubfolder  Optional named subfolder inside the dated dir.
        @param  drawBoundingBox    If True, draw a box around the detection.
        """
        previewMs = (firstMs + lastMs) // 2

        self._onActionBegin(actionCtx, "save snapshot", camLoc, ruleName,
                            firstMs, lastMs - firstMs, tryNum, None)

        clipMgr = actionCtx._executionContext.getClipMgr()
        dataMgr = actionCtx._executionContext.getDataMgr()

        # Flush on the FIRST attempt only -- same rule the push-notification
        # path above already follows.  A flush cycles this camera's ffmpeg and
        # cuts the in-progress segment, and registration takes ~58s here while
        # this wait allows 10s, so a snapshot essentially never succeeds on
        # attempt 1: flushing on every retry meant ~3 recorder cycles per
        # event.  Measured 2026-08-10: 502 snapshot failures, 232 successes and
        # 626 flush requests in a day (190 on 09_Jungle, 188 on 08_FrontStep).
        # Under detection load that closed a feedback loop -- flushes starved
        # the decoders, which timed out, which restarted recorders, which
        # queued gap-fill re-encodes -- and took the whole fleet to 97% CPU
        # during a walk.  The retry itself is unchanged; it still walks the
        # ladder out, just without cycling the recorder to get there.
        videoState = _waitForVideoAt(clipMgr, None, tryNum == 1,
                                     camLoc, previewMs,
                                     self._backEndQueue, self._logger,
                                     _kGetImageTimeoutSeconds,
                                     _kGetImageRetrySleep,
                                     requireCoverage=True)

        if videoState == _kVideoPending:
            retryAfter = _nextRetryTime(tryNum, self._logger, "snapshot")
            if retryAfter is not None:
                actionCtx.setStatus(False, "image isn't available yet",
                                    warnOnly=True,
                                    quiet=_isExpectedVideoWait(previewMs))
                return retryAfter
            # Ladder spent and previewMs still isn't covered.  Rather than give
            # up empty-handed, make the best snapshot the event window allows.
            videoState = _kVideoMissing

        img = None
        if videoState == _kVideoMissing:
            # previewMs sits in a recording hole.  Measured 2026-08-21: of 114
            # failed snapshots that day, 15 had no video at previewMs but did
            # have video elsewhere in the same event -- e.g. 09_Jungle 15:35:24
            # -> 15:35:36, where the camera delivered nothing for 10.2s around
            # previewMs but the next segment starts 1.3s before the event ends.
            # Widen the search to half the event span, which is exactly the
            # event window either side of previewMs, and let getSingleFrame
            # clamp onto the real frame at the edge of what was recorded.
            tolerance = max(_kFrameSearchToleranceMs, (lastMs - firstMs) // 2)
            fileName = clipMgr.getFileAt(camLoc, previewMs, tolerance)
            if not fileName:
                actionCtx.setStatus(False, "no video recorded for this event")
                return None
            fileStart, fileStop = clipMgr.getFileTimeInformation(fileName)
            usedMs = min(fileStop, max(fileStart, previewMs))
            self._logger.warning(
                "no video at %d on %s; using the nearest frame in the event, "
                "%+.1fs away in %s" %
                (previewMs, camLoc, (usedMs - previewMs) / 1000.0, fileName))
            img = dataMgr.getSingleMarkedFrame(camLoc, previewMs, objList,
                                               (0, 0), tolerance=tolerance)
            note = " nearestFrame=%+.1fs" % ((usedMs - previewMs) / 1000.0)
        else:
            img = dataMgr.getSingleMarkedFrame(camLoc, previewMs, objList,
                                               (0, 0))
            note = ""

        if img is None:
            actionCtx.setStatus(False, "no frame available")
            return None

        if drawBoundingBox:
            img = dataMgr.markBoundingBoxes(camLoc, img, previewMs, objList)

        savedPath = dataMgr.saveEventSnapshot(camLoc, previewMs, img, snapshotPath, snapshotSubfolder)
        if savedPath:
            actionCtx.setStatus(True, "saved to %s objs=%s%s" %
                                (savedPath, str(objList), note))
            # The camera has already analysed this frame, so the Images tab
            # gets the snapshot with those results rather than re-running it.
            from backEnd.UserMediaSnapshotImport import recordSnapshot
            recordSnapshot(dataMgr, camLoc, savedPath, previewMs, img,
                           objList, self._logger)
        else:
            actionCtx.setStatus(False, "failed to write snapshot")
        return None

    ###########################################################
    def _processSendEmail(self, actionCtx, tryNum, ruleName, camLoc, emailSettings,
                          configDict, numTriggers, objList, firstMs, lastMs,
                          messageId):
        """Send the email for the given object.

        This is imported / used by the response runner.

        @param  tryNum         The try number; starts at 1.
        @param  ruleName       The name of the rule containing this response.
        @param  camLoc         The camera location.
        @param  emailSettings  A dictionary of email settings; see BackEndPrefs.
        @param  configDict     A dictionary of config info relating to this
                               particular rule.
        @param  numTriggers    The number of times the rule was triggered.
        @param  objList        List of objects to highlight.
        @param  firstMs        The first ms that the object was seen.
        @param  lastMs         The last ms that the object was seen.
        @param  messageId      The messageID to use.
        @return retryAfter     If non-None, we'll retry after time.time()
                               returns a value greater than this.
        """
        previewMs = (lastMs + firstMs) // 2
        self._logger.debug("Want to send email: %s, %ld, %ld, %ld" %
                           (str(objList), firstMs, lastMs, previewMs ))

        # Get the 'toAddrs'.  First priority is the configDict.  If it's not
        # there, fall back to emailSettings (the site-wide setting, which was
        # used in the betas.
        toAddrs = configDict.get('toAddrs', emailSettings.get('toAddrs', ""))
        if isinstance(toAddrs, bytes):
            toAddrs = toAddrs.decode('utf-8')
        subject = configDict.get('subject', kDefaultNotificationSubject)
        if isinstance(subject, bytes):
            subject = subject.decode('utf-8')

        self._onActionBegin(actionCtx, "send email", camLoc, ruleName, firstMs, lastMs-firstMs, tryNum, toAddrs)

        if not toAddrs.strip():
            actionCtx.setStatus(False, _kEmailNotConfiguredErrorStr)
            return None

        startTime = int(time.time()*1000)

        clipMgr = actionCtx._executionContext.getClipMgr()
        dataMgr = actionCtx._executionContext.getDataMgr()

        # Apply substitution variables to the subject line...
        if '{Sv' in subject:
            subject = substituteResponseVars(subject, ruleName, camLoc,
                                             firstMs,
                                             configDict.get('svLookFor'),
                                             faceNameForObjs(dataMgr, objList))

        actionCtx.setProgressStr("waiting for thumb")
        canProceed = _waitUntilVideoAvailable(clipMgr, None, False,
                                                    camLoc, previewMs,
                                                    self._backEndQueue, self._logger,
                                                    _kGetImageTimeoutSeconds, _kGetImageRetrySleep)
        if not canProceed:
            # Video isn't available yet ... fail this operation, and schedule a retry
            retryAfter = _nextRetryTime(tryNum, self._logger, "email")
            actionCtx.setStatus(False, "image isn't available yet", warnOnly=True,
                                quiet=(retryAfter is not None and
                                       _isExpectedVideoWait(previewMs)))
            return retryAfter

        hasVideoTime = int(time.time()*1000)

        actionCtx.setProgressStr("generating a thumb")
        imgRes = configDict.get('maxRes', 320)
        img = dataMgr.getSingleMarkedFrame(camLoc, previewMs, objList,
                                           (0, imgRes))
        if img is not None:
            self._logger.debug("Got an image")
            attachName = time.strftime('%Y-%m-%d-%H%M%S', time.localtime(previewMs / 1000.0)) + '-' + camLoc + '.jpg'
            imgList = [(attachName, img)]
        else:
            self._logger.warning('Failed to get image to email: rule="%s" cam="%s" ts=%ld.' %
                                 (ruleName, camLoc, previewMs) )
            imgList = []


        frameAquiredTime = int(time.time()*1000)

        try:
            timeStruct = time.localtime(firstMs / 1000)
            timeStr = getTimeAsString(firstMs)
            dateStr = formatTime('%x', timeStruct)

            if numTriggers == 1:
                body = _kEmailBodySingle % (ruleName, timeStr, dateStr)
            else:
                body = _kEmailBodyMultiple % (ruleName, numTriggers,
                                              timeStr, dateStr)

            textInline = emailSettings.get('textInline', False)
            imageInline = emailSettings.get('imageInline', True)

            def _es(key, default=""):
                v = emailSettings.get(key, default)
                return v.decode('utf-8') if isinstance(v, bytes) else v

            actionCtx.setProgressStr("preparing to send email")
            sendSimpleEmail(body,
                            _es('fromAddr'),
                            toAddrs, subject,
                            _es('host'), _es('user'),
                            _es('password'), emailSettings['port'],
                            _es('encryption'), imgList, [],
                            lambda val, msg: actionCtx.setProgressStr(msg + "(" + str(val) + ")"),
                            _kDebug, messageId,
                            textInline,
                            imageInline )
            msg = "imgWait=%d imgRetrieval=%d imgSending=%d objs=%s" % ( hasVideoTime-startTime, frameAquiredTime-hasVideoTime, int(time.time()*1000)-frameAquiredTime, str(objList) )
            actionCtx.setStatus(True, msg)
        except Exception as e:
            if tryNum < _kSendEmailNumTries:
                triesLeft = (_kSendEmailNumTries - tryNum)
                actionCtx.setStatus(False, _kEmailWarningFormatStr % (ruleName, str(e), triesLeft, traceback.format_exc()))
                return time.time() + _kSendEmailRetrySleepSeconds
            else:
                actionCtx.setStatus(False, _kEmailErrorFormatStr % (ruleName, str(e), traceback.format_exc()))
        return None

    ###########################################################
    def _purgePushNotifications(self):
        """ Purges notifications"""
        now = time.time()
        if now > self._nextPushNotificationPurge:
            try:
                purgeCount = self._responseDbMgr.purgePushNotifications(
                    _kPushNotificationMaxAgeSecs,
                    _kMaxPushNotificationsPurge)

                self._logger.info("%d notifications purged" % purgeCount)
                # only get comfortable if we were able to remove all of the
                # notifications, otherwise we will be back as soon as possible
                if purgeCount < _kMaxPushNotificationsPurge:
                    self._nextPushNotificationPurge = now + \
                        _kPushNotificationsPurgeIntervalSecs
            except:
                self._logger.error("notification purge failed (%s)" %
                                   sys.exc_info()[1])
                # Back off after a failure too, so a persistent problem (lock
                # contention or a damaged responseDb) doesn't retry on every
                # loop iteration and flood the log.
                self._nextPushNotificationPurge = now + \
                    _kPushNotificationsPurgeIntervalSecs

