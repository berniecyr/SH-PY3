#!/usr/bin/env python

"""Replay archived clips through the motion pipeline for tuning and regression.

This is the engine behind the front end's Developer > Detection Test Suite.  It
takes a camera and a time range, finds the archived clips, and runs them through
the SAME VideoPipeline the live capture path uses -- with whatever sensitivity /
shadow / minimum-travel settings you want to try -- then reports how many objects
came out and what shape they were.  Optionally it also asks the running
DetectionService to classify each object, so you can see the end result rather
than just the motion count.

The point is to answer "does this setting calm the false detections without
losing the real ones?" against real footage in minutes, instead of changing a
setting and waiting a night to find out.

No wx here; this is importable from either process.


FOUR THINGS THAT MAKE A REPLAY LIE
==================================

Every one of these fails silently -- the run completes, the numbers are just
wrong -- so they are all handled here and all worth understanding before
trusting a result.

1.  THE TRACKER'S CLOCK.  VideoPipeline ages tracks out using _kMotionCooldown
    (3 s) and _kCandidateTimeout (0.6 s) read from a clock.  Live, that clock is
    wall time and one second of wall time is one second of video.  Replaying as
    fast as the disk allows, 3 s of wall time can be minutes of video, so tracks
    never expire and every blob in the range merges into one object -- counts
    collapse to near zero.  We pass VideoPipeline a timeFn driven by the frame
    timestamps instead.

2.  THE BACKGROUND MODEL MUST BE WARM.  Live MOG2 has hours of history and a
    tight per-pixel variance, so a small change reads as foreground.  A cold
    model starts with wide variance and flags far less.  Measured 2026-08-26 on
    09_Jungle's 00:07:43 clip at level 3: replayed cold, 2 frames of 870 carried
    a blob and NO object was ever promoted (_kPromoteHits needs 3 in a row);
    primed with the preceding 2 minutes, 51 frames carried a blob and the
    largest more than doubled (836 -> 1860 px^2).  A cold replay under-reports
    false positives by more than an order of magnitude while looking perfectly
    healthy.  We prime with _kDefaultPrimeMs of preceding footage and report
    only objects that start inside the requested window.

3.  THE ARCHIVE IS NOT THE ANALYSIS FRAME.  The recorded mp4 is camera-native
    (1920x1080 on 09_Jungle) while analysis runs at 640x360, and clipdb's
    procWidth/procHeight column records the ANALYSIS size, not the file's.
    Decode natively and the area thresholds scale against the wrong frame.  We
    ask ClipReader to emit at the clip's procSize, so videoLib2 does the same
    downscale the recorder does.

4.  THE FRAME BUDGET MUST NOT BE SPENT ON THE PRIME.  Priming and caching are
    measured in the same currency -- decoded frames -- and until 2026-09-01 the
    two constants collided exactly: _kMaxCachedFrames was 1200 and the prime is
    120 s, which at _kAnalysisFps 10 is also 1200 frames.  decodeClips fills
    from the oldest frame forward, so the sweep's cache was full BEFORE the
    requested window began and every row reported zero objects.  Measured on
    05_Gate_lr for 12:29-12:30 on 2026-09-01: decoding stopped at 12:28:20, 40 s
    short of the window, while the Search screen showed a person at 12:29:44
    (objdb2 uid 629417, detConf 0.86).  The same cap made replayRange retain the
    FIRST 1200 frames for the YOLO path, so a real person was classified against
    a frame 89 s earlier and came back "unknown".

    So: the window is never what gets cut.  Priming footage older than primeMs
    is skipped without being decoded, decoding stops at the end of the window,
    and what a run could not reach is reported (framesInWindow / framesScored)
    rather than quietly returning zero.  Keep _kMaxCachedFrames comfortably
    above (primeMs/1000 + window seconds) * analysisFps, and never tune it
    without re-checking it against _kDefaultPrimeMs.
"""

import os
import shutil
import sqlite3
import tempfile
import time

from .VideoPipeline import VideoPipeline
from appCommon.CommonStrings import kVideoFolder


# Seconds of footage decoded before the requested window purely to converge the
# background model.  See note 2 above -- this is not optional.  120 s is ~1700
# frames at 14 fps, comfortably past MOG2's history=500.
_kDefaultPrimeMs = 120 * 1000

# The sweep replays its cached frames once per configuration, so this cache is
# the memory ceiling of the whole feature.  640x360 grayscale is 225 KiB a frame;
# prime (120 s) plus a one-minute window is 1800 frames at _kAnalysisFps, i.e.
# ~414 MiB here and ~552 MiB on the 640x480 doorbell.  2000 leaves margin without
# letting a long range run the front end out of memory.
#
# THIS MUST STAY COMFORTABLY ABOVE (_kDefaultPrimeMs/1000 + _kSweepWindowMs/1000)
# * _kAnalysisFps.  It was 1200, which equalled the prime exactly, and the sweep
# could therefore never decode a single frame of the window it was asked about.
# See note 4 in the module docstring.
_kMaxCachedFrames = 2000

# The most footage a single sweep will analyse.  A sweep runs ten configurations
# over cached frames, so both memory and runtime scale with the window; a longer
# range is swept for its first minute and the dialog says so, rather than
# silently costing half an hour and a gigabyte.
_kSweepWindowMs = 60 * 1000

# How far a retained frame may sit from an object's midpoint and still be
# accepted as that object's frame to classify.  Two analysis frames at 10 fps is
# 200 ms, so 2 s is generous; beyond it the answer would be about different
# footage, and _classify says 'no frame' instead of returning a confident-looking
# 'unknown'.
_kClassifyToleranceMs = 2000

# Sensitivity levels the sweep walks, and the shadow settings for each.
_kSweepLevels = (1, 2, 3, 4, 5)

# Frames per second fed to the pipeline.  The recorded mp4 runs faster than the
# live analysis path actually manages: 09_Jungle's archive is 14.3 fps encoded,
# while its _profileMotion line reports 9-10 fps sustained.  Nothing throttles it
# deliberately -- BackEndApp sets extra['fpsLimit'] = 10 but no consumer reads it
# -- the capture thread simply polls StreamReader._latest_frame and drops what it
# cannot keep up with.
#
# Replaying every encoded frame would therefore feed the tracker ~40% more frames
# per second of video than production ever sees, reaching _kPromoteHits sooner and
# inflating both object counts and every per-object frame total.  Subsampling to
# the observed live rate keeps the cadence honest, and skipping the rest through
# grab() rather than read() makes the run substantially faster as well.
#
# Set to None to feed every frame -- the upper bound on what motion detection
# would find, not what this fleet actually runs.
_kAnalysisFps = 10.0


###############################################################################
class CountingCollector(object):
    """The ObjectCollector contract VideoPipeline needs, minus the database.

    VideoPipeline touches exactly six members; this implements them and records
    enough geometry per object to judge a setting.  Nothing is written anywhere.
    """

    ###########################################################
    def __init__(self, logger, cameraLocation, windowMs=None):
        self._logger        = logger
        self.cameraLocation = cameraLocation
        self.objects        = {}
        self._nextId        = 0
        self.frames         = 0
        # Frames inside the requested window, as opposed to the priming footage
        # ahead of it.  Counted here rather than kept as a list of timestamps,
        # so a whole-night run costs one integer.
        self.framesInWindow = 0
        self._window        = windowMs


    ###########################################################
    def reportFrame(self, ms, frameObj=None):
        self.frames += 1
        if self._window is None or self._window[0] <= ms < self._window[1]:
            self.framesInWindow += 1


    ###########################################################
    def addObject(self, timeStart, objType="unknown"):
        oid = self._nextId
        self._nextId += 1
        self.objects[oid] = {
            'uid': oid, 'firstMs': timeStart, 'lastMs': timeStart,
            'type': objType, 'nFrames': 0, 'boxes': [],
        }
        return oid


    ###########################################################
    def addFrame(self, objId, frameId, time, bbox, objType):
        o = self.objects.get(objId)
        if o is None:
            return
        o['lastMs'] = max(o['lastMs'], time)
        o['firstMs'] = min(o['firstMs'], time)
        o['nFrames'] += 1
        o['boxes'].append(bbox)


    ###########################################################
    def frameCompleted(self, msTimestamp, frameNum):
        pass


    ###########################################################
    def summarize(self, startMs=None, stopMs=None):
        """Geometry per object, restricted to those starting in [startMs, stopMs).

        Returns a list of dicts with the metrics that actually separate real
        detections from noise on this fleet -- span above all (measured 18x
        between night false positives and real person/animal tracks, against
        3.8x for blob area).
        """
        out = []
        for o in self.objects.values():
            if not o['boxes']:
                continue
            if startMs is not None and o['firstMs'] < startMs:
                continue
            if stopMs is not None and o['firstMs'] >= stopMs:
                continue
            cxs = [(b[0] + b[2]) / 2.0 for b in o['boxes']]
            cys = [(b[1] + b[3]) / 2.0 for b in o['boxes']]
            areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in o['boxes']]
            # 'travel' is the centroid SPAN -- the same quantity DataManager
            # stores per object and MinTravelTrigger filters on, so a number
            # read here means the same thing as the one in the search filter.
            # 'netDispl' (first frame to last) is kept for diagnosis only; it
            # cancels to nearly zero for a subject that walks out and back, which
            # is why it is not what anything filters on.
            travel = (max(cxs) - min(cxs)) + (max(cys) - min(cys))
            netDispl = ((cxs[-1] - cxs[0]) ** 2 + (cys[-1] - cys[0]) ** 2) ** 0.5
            out.append({
                'uid':      o['uid'],
                'firstMs':  o['firstMs'],
                'lastMs':   o['lastMs'],
                'durSec':   (o['lastMs'] - o['firstMs']) / 1000.0,
                'nFrames':  o['nFrames'],
                'travel':   travel,
                'netDispl': netDispl,
                'meanArea': sum(areas) / float(len(areas)),
                'maxArea':  max(areas),
                'cx':       sum(cxs) / float(len(cxs)),
                'cy':       sum(cys) / float(len(cys)),
                'box':      o['boxes'][len(o['boxes']) // 2],
                'type':     o['type'],
            })
        out.sort(key=lambda r: r['firstMs'])
        return out


###############################################################################
class _FrameShim(object):
    """Minimal frame object: VideoPipeline reads ._data, and nothing else."""
    __slots__ = ('_data', 'ms', 'width', 'height')

    def __init__(self, data, ms):
        self._data = data
        self.ms = ms
        self.height, self.width = data.shape[0], data.shape[1]


###############################################################################
def openClipDbReadOnly(dataDir, logger=None):
    """Copy clipdb aside and open it read-only.

    The live clipdb is WAL-mode with the back end writing to it, and
    ClipManager._openImpl will delete a -shm it judges poisoned -- a write to the
    production database from a test harness.  Copying costs a few MB and removes
    the whole class of problem.  Returns (sqlite3.Connection, tempDirToClean).
    """
    tmpDir = tempfile.mkdtemp(prefix='svreplay-')
    src = os.path.join(dataDir, 'clipdb')
    dst = os.path.join(tmpDir, 'clipdb')
    shutil.copyfile(src, dst)
    for suffix in ('-wal', '-shm'):
        if os.path.exists(src + suffix):
            try:
                shutil.copyfile(src + suffix, dst + suffix)
            except (IOError, OSError):
                # A torn -wal copy just means we see slightly older rows.
                pass
    conn = sqlite3.connect(dst)
    conn.execute('PRAGMA query_only=1')
    return conn, tmpDir


###############################################################################
def listClips(conn, camLoc, startMs, stopMs):
    """Archived clips overlapping [startMs, stopMs), oldest first.

    Returns [(relativeFilename, firstMs, lastMs, procW, procH), ...].  The
    filename is relative and posix-separated, as ClipManager stores it.
    """
    rows = conn.execute(
        'SELECT filename, firstMs, lastMs, procWidth, procHeight FROM clips '
        'WHERE camLoc=? AND lastMs>=? AND firstMs<? ORDER BY firstMs',
        (camLoc, startMs, stopMs)).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


###############################################################################
def iterClipFrames(clips, videoDir, procSize, analysisFps=_kAnalysisFps,
                   progressFn=None, cancelFn=None, logFn=None, problems=None,
                   skipBeforeMs=None, stopAtMs=None):
    """Yield (grayFrame, absoluteMs) for each clip frame at the analysis size/rate.

    A generator, so a single run never holds more than one frame at a time -- a
    640x360 grey frame is 225 KB, and a whole night is six figures of them.  Only
    the sweep materialises frames, and only up to _kMaxCachedFrames.

    Frames are subsampled to analysisFps -- see _kAnalysisFps.  Skipped frames go
    through grab() without retrieve(), which avoids the colour convert and the
    resize and is where the time saved comes from.

    skipBeforeMs and stopAtMs trim the two ends that listClips necessarily
    overshoots: clips are whole segments, so the first one starts up to a minute
    before (startMs - primeMs) and the last runs up to a minute past stopMs.
    Neither end can contribute an object that survives summarize(), so decoding
    them costs memory and time for nothing -- and on the sweep, whose cache is
    finite, the head overshoot comes straight out of the window's budget.
    Trimming the head also makes the prime exactly primeMs rather than varying
    with wherever the segment boundary happened to fall, so a Run and a Sweep of
    the same window see the same amount of priming footage.

    `problems`, if given, is a dict this fills in with clips that were missing,
    unopenable, or that stopped decoding early.  Archived clips DO go bad -- an
    h264 "Invalid NAL unit size" was hit while testing this -- and a clip that
    dies halfway silently shortens the footage analysed.  Fewer frames means
    fewer objects, which reads exactly like a quiet camera.  Callers surface this
    so a truncated run is never mistaken for a clean one.
    """
    import cv2

    procW, procH = procSize
    nYielded = 0
    archiveRoot = os.path.join(videoDir, kVideoFolder)
    if problems is None:
        problems = {}
    problems.setdefault('missing', [])
    problems.setdefault('unopenable', [])
    problems.setdefault('truncated', [])

    for (relName, firstMs, lastMs, _w, _h) in clips:
        if cancelFn is not None and cancelFn():
            break
        path = os.path.join(archiveRoot, relName.replace('/', os.sep))
        if not os.path.exists(path):
            problems['missing'].append(relName)
            if logFn is not None:
                logFn('replay: missing clip %s' % path)
            continue
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            problems['unopenable'].append(relName)
            if logFn is not None:
                logFn('replay: could not open %s' % path)
            continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 14.0
        # What the clipdb row says this file should hold, so a decode that dies
        # early can be told apart from a clip that is simply short.
        expectedFrames = max(0, int((lastMs - firstMs) / 1000.0 * fps))
        # Keep every frame whose index crosses the next analysis-rate boundary.
        step = 1.0 if not analysisFps else max(1.0, fps / float(analysisFps))
        idx = 0
        nextWanted = 0.0
        reachedStop = False
        try:
            while True:
                if not cap.grab():
                    break
                frameMs = int(firstMs + idx * 1000.0 / fps)
                if stopAtMs is not None and frameMs >= stopAtMs:
                    reachedStop = True
                    break
                if idx >= nextWanted:
                    # The head trim rides the grab()-only path, so a skipped
                    # frame costs no retrieve, no colour convert and no resize.
                    if skipBeforeMs is not None and frameMs < skipBeforeMs:
                        # Still advance the cadence, so the first kept frame sits
                        # on the same grid the rest of the run uses.
                        nextWanted += step
                        idx += 1
                        continue
                    ok, img = cap.retrieve()
                    if not ok:
                        break
                    if img.shape[1] != procW or img.shape[0] != procH:
                        img = cv2.resize(img, (procW, procH),
                                         interpolation=cv2.INTER_AREA)
                    # Timestamps come from the frame's real position in the file,
                    # so a subsampled stream still carries true inter-frame gaps
                    # -- which is all the tracker's cooldowns need.
                    yield (cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), frameMs)
                    nYielded += 1
                    nextWanted += step
                idx += 1
        finally:
            cap.release()
        # 10% slack: container durations are approximate and the last GOP is
        # often short.  Anything beyond that is a real decode failure -- but
        # stopping at stopAtMs is deliberate and must not be reported as one.
        if expectedFrames and idx < expectedFrames * 0.9 and not reachedStop:
            problems['truncated'].append((relName, idx, expectedFrames))
            if logFn is not None:
                logFn('replay: %s stopped decoding at frame %d of ~%d - the '
                      'footage analysed is shorter than the range asked for'
                      % (relName, idx, expectedFrames))
        if progressFn is not None:
            progressFn(nYielded)
        if reachedStop:
            break
        if cancelFn is not None and cancelFn():
            break


###############################################################################
def decodeClips(clips, videoDir, procSize, maxFrames=None, analysisFps=_kAnalysisFps,
                progressFn=None, cancelFn=None, logFn=None, problems=None,
                skipBeforeMs=None, stopAtMs=None):
    """Materialise up to maxFrames from iterClipFrames, for the sweep.

    Returns (frames, msList).  Only the sweep needs this -- it runs many
    configurations over the same footage, and decoding is what costs.
    """
    frames, msList = [], []
    for gray, ms in iterClipFrames(clips, videoDir, procSize, analysisFps,
                                   progressFn, cancelFn, logFn, problems,
                                   skipBeforeMs=skipBeforeMs, stopAtMs=stopAtMs):
        frames.append(gray)
        msList.append(ms)
        if maxFrames is not None and len(frames) >= maxFrames:
            break
    return frames, msList


###############################################################################
def runPipelineStream(frameIter, procSize, sensitivity, ignoreShadows,
                      minTravel, logger=None, camLoc='replay', cancelFn=None,
                      keepFrames=None, windowMs=None, closePx=0):
    """Run one configuration over a stream of (grayFrame, ms) pairs.

    Returns (collector, illumEvents, nFrames, keptFrames, keptMs).  Frames are
    retained only when keepFrames is set (an int cap), which the YOLO path needs
    to go back and crop; otherwise nothing accumulates and the range can be as
    long as you like.

    Retention starts at the WINDOW, never at the first frame decoded.  Only
    objects starting inside the window are ever reported (summarize discards the
    rest), so a priming frame can never be the right one to classify -- and
    keeping the first N meant a real detection was classified against footage
    from over a minute earlier.  See note 4 in the module docstring.
    """
    import numpy as np

    collector = CountingCollector(logger, camLoc, windowMs)
    keptFrames, keptMs = [], []

    # See note 1 in the module docstring: the tracker must age tracks by VIDEO
    # time, not wall time, or nothing ever expires and the counts collapse.
    clock = {'t': 0.0}
    pipeline = VideoPipeline(
        camLoc, collector,
        sensitivity=sensitivity, ignoreShadows=ignoreShadows,
        minTravel=minTravel, frameSize=procSize,
        timeFn=lambda: clock['t'], closePx=closePx)

    n = 0
    for gray, ms in frameIter:
        if cancelFn is not None and (n & 0x3F) == 0 and cancelFn():
            break
        clock['t'] = ms / 1000.0
        # VideoPipeline greyscales internally via COLOR_RGB2GRAY; feeding an
        # already-grey frame stacked to 3 channels keeps that path exact.
        pipeline.processClipFrame(
            _FrameShim(np.repeat(gray[:, :, None], 3, axis=2), ms), ms)
        if keepFrames is not None and len(keptFrames) < keepFrames \
                and (windowMs is None or ms >= windowMs[0]):
            keptFrames.append(gray)
            keptMs.append(ms)
        n += 1

    return (collector, getattr(pipeline, '_illumEvents', 0), n, keptFrames, keptMs)


###############################################################################
def runPipeline(frames, msList, procSize, sensitivity, ignoreShadows,
                minTravel, logger=None, camLoc='replay', cancelFn=None):
    """Run one configuration over already-decoded frames (the sweep's path).

    Returns (collector, illumEvents).  Cheap relative to decoding, which is what
    makes sweeping worthwhile.
    """
    coll, illum, _n, _kf, _km = runPipelineStream(
        zip(frames, msList), procSize, sensitivity, ignoreShadows, minTravel,
        logger=logger, camLoc=camLoc, cancelFn=cancelFn)
    return coll, illum


###############################################################################
###############################################################################
def replayRange(dataDir, videoDir, camLoc, startMs, stopMs,
                sensitivity=3, ignoreShadows=False, minTravel=0,
                primeMs=_kDefaultPrimeMs, yoloConf=None, analysisFps=_kAnalysisFps,
                logger=None, progressFn=None, cancelFn=None):
    """Replay one camera/time range under one configuration.

    @param  dataDir       Folder holding clipdb (the app's data dir).
    @param  videoDir      Video storage root; clips live under <videoDir>/archive.
    @param  camLoc        Camera location name, as stored in clipdb.
    @param  startMs       Window start, epoch ms.
    @param  stopMs        Window end, epoch ms.
    @param  sensitivity   Motion sensitivity level 1-5.
    @param  ignoreShadows Shadow rejection on/off.
    @param  minTravel     Minimum centroid travel, px at 1280x720.
    @param  primeMs       Footage decoded before startMs to warm the background.
    @param  yoloConf      If not None, classify each object at this confidence.
    @param  analysisFps   Frames per second fed to the pipeline; see _kAnalysisFps.
    @return results       Dict of objects, counts and timings.
    """
    t0 = time.time()
    conn, tmpDir = openClipDbReadOnly(dataDir, logger)
    try:
        clips = listClips(conn, camLoc, startMs - primeMs, stopMs)
    finally:
        conn.close()
        shutil.rmtree(tmpDir, ignore_errors=True)

    if not clips:
        return {'objects': [], 'error': 'No archived clips for %s in that range.'
                % camLoc, 'framesDecoded': 0}

    # procSize comes from the clips themselves -- it is the ANALYSIS size, which
    # is what the pipeline's thresholds scale against, not the file's own size.
    procW = clips[0][3] or 640
    procH = clips[0][4] or 360

    logFn = logger.info if logger is not None else None

    # Stream: a single run never holds the footage, so the range can be a whole
    # night without the memory going with it.  Only the YOLO path keeps frames,
    # because it has to go back and crop them, and even then only up to the cap.
    problems = {}
    frameIter = iterClipFrames(clips, videoDir, (procW, procH),
                               analysisFps=analysisFps, progressFn=progressFn,
                               cancelFn=cancelFn, logFn=logFn, problems=problems,
                               skipBeforeMs=startMs - primeMs, stopAtMs=stopMs)
    collector, illumEvents, nFrames, keptFrames, keptMs = runPipelineStream(
        frameIter, (procW, procH), sensitivity, ignoreShadows, minTravel,
        logger=logger, camLoc=camLoc, cancelFn=cancelFn, windowMs=(startMs, stopMs),
        keepFrames=(_kMaxCachedFrames if yoloConf is not None else None))
    if not nFrames:
        return {'objects': [], 'error': 'No frames decoded.', 'framesDecoded': 0}

    # Only objects that START inside the requested window count; anything before
    # it belongs to the priming footage that exists purely to warm MOG2.
    objects = collector.summarize(startMs, stopMs)

    if yoloConf is not None:
        _classify(objects, keptFrames, keptMs, (procW, procH), yoloConf, logger)

    scored = collector.framesInWindow
    # True when the window held more frames than the YOLO path could retain, so
    # the objects past that point had no frame near enough to classify.  Said out
    # loud rather than left to look like a detector result.
    classifyTruncated = bool(yoloConf is not None and keptMs and
                             len(keptMs) >= _kMaxCachedFrames)
    return {
        'objects':       objects,
        'nObjects':      len(objects),
        'illumEvents':   illumEvents,
        'framesDecoded': nFrames,
        'framesScored':  scored,
        'framesPrimed':  nFrames - scored,
        'classifiedToMs':    keptMs[-1] if keptMs else None,
        'classifyTruncated': classifyTruncated,
        'procSize':      (procW, procH),
        'nClips':        len(clips),
        'elapsedSec':    time.time() - t0,
        'sensitivity':   sensitivity,
        'ignoreShadows': ignoreShadows,
        'minTravel':     minTravel,
        'analysisFps':   analysisFps,
        'problems':      problems,
        'error':         None,
    }


###############################################################################
def sweep(dataDir, videoDir, camLoc, startMs, stopMs, minTravel=0,
          primeMs=_kDefaultPrimeMs, levels=_kSweepLevels, shadowOptions=(False, True),
          analysisFps=_kAnalysisFps, yoloConf=None, logger=None, progressFn=None,
          cancelFn=None, stageFn=None):
    """Decode once, then run every sensitivity x shadow combination over it.

    This is the comparison that actually answers the tuning question: the same
    footage under every setting, so a drop in false positives can be read against
    what it costs in real ones.

    The window is clamped to _kSweepWindowMs.  Each configuration replays the
    whole cache, so both memory and runtime scale with the range asked for, and
    an hour-long sweep would cost a gigabyte and most of an afternoon; a longer
    request is swept for its first minute and 'windowClamped' says so, which the
    dialog surfaces.  `stageFn(text)` reports which configuration is running --
    decoding is a small fraction of a sweep, so without it the progress dialog
    sits still for most of the run and reads as a hang.
    """
    t0 = time.time()
    sweepStopMs = min(stopMs, startMs + _kSweepWindowMs)
    windowClamped = sweepStopMs < stopMs

    conn, tmpDir = openClipDbReadOnly(dataDir, logger)
    try:
        clips = listClips(conn, camLoc, startMs - primeMs, sweepStopMs)
    finally:
        conn.close()
        shutil.rmtree(tmpDir, ignore_errors=True)

    if not clips:
        return {'rows': [], 'error': 'No archived clips for %s in that range.'
                % camLoc}

    procW = clips[0][3] or 640
    procH = clips[0][4] or 360
    logFn = logger.info if logger is not None else None

    if stageFn is not None:
        stageFn('Decoding footage')
    problems = {}
    frames, msList = decodeClips(clips, videoDir, (procW, procH),
                                 maxFrames=_kMaxCachedFrames,
                                 analysisFps=analysisFps, progressFn=progressFn,
                                 cancelFn=cancelFn, logFn=logFn, problems=problems,
                                 skipBeforeMs=startMs - primeMs,
                                 stopAtMs=sweepStopMs)
    if not frames:
        return {'rows': [], 'error': 'No frames decoded.'}

    # The number whose absence hid the cache-versus-prime collision for good:
    # a sweep can decode thousands of frames and still never reach the window it
    # was asked about, and every row then reports zero exactly as a quiet camera
    # would.  Treat it as an error, not a footnote.
    framesInWindow = sum(1 for m in msList if startMs <= m < sweepStopMs)
    if not framesInWindow:
        return {'rows': [], 'framesDecoded': len(frames), 'framesInWindow': 0,
                'error': 'Decoded %d frames of %s but none inside %s - %s, so '
                         'there is nothing to sweep.%s'
                         % (len(frames), camLoc, _fmtMs(startMs),
                            _fmtMs(sweepStopMs), describeProblems(problems))}

    # Truncation now means the WINDOW was cut short, not merely that the cache
    # filled: the prime is trimmed to exactly primeMs, so anything short of a
    # full window means the cap bit into footage that was asked for.
    expectedInWindow = (sweepStopMs - startMs) / 1000.0 * (analysisFps or 14.0)
    truncated = (len(frames) >= _kMaxCachedFrames and
                 framesInWindow < expectedInWindow * 0.9)

    client, clientReason = (_openDetectionClient(logger) if yoloConf is not None
                            else (None, None))
    try:
        rows = []
        nConfigs = len(levels) * len(shadowOptions)
        # One flag rather than a bare inner break: breaking the shadow loop
        # alone left the level loop running, so a cancelled sweep still walked
        # every remaining configuration -- cheap before, but each one now also
        # carries its own YOLO calls.
        stopped = False
        for level in levels:
            if stopped:
                break
            for shadows in shadowOptions:
                if cancelFn is not None and cancelFn():
                    stopped = True
                    break
                if stageFn is not None:
                    stageFn('Level %d, shadows %s (%d of %d)'
                            % (level, 'on' if shadows else 'off',
                               len(rows) + 1, nConfigs))
                collector, illum = runPipeline(
                    frames, msList, (procW, procH), level, shadows, minTravel,
                    logger=logger, camLoc=camLoc, cancelFn=cancelFn)
                objs = collector.summarize(startMs, sweepStopMs)
                if yoloConf is not None and client is not None:
                    _classify(objs, frames, msList, (procW, procH), yoloConf,
                              logger, client=client)
                elif yoloConf is not None:
                    # One failed connect, reported on every row -- not ten
                    # connects and ten identical warnings.
                    for o in objs:
                        o['yolo'] = clientReason
                rows.append({
                    'sensitivity':   level,
                    'ignoreShadows': shadows,
                    'minTravel':     minTravel,
                    'nObjects':      len(objs),
                    'illumEvents':   illum,
                    'objects':       objs,
                    'yoloSummary':   summarizeLabels(objs) if yoloConf is not None
                                     else '',
                })
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    return {
        'rows':           rows,
        'framesDecoded':  len(frames),
        'framesInWindow': framesInWindow,
        'procSize':       (procW, procH),
        'truncated':      truncated,
        'windowClamped':  windowClamped,
        'startMs':        startMs,
        'stopMs':         sweepStopMs,
        'requestedStopMs': stopMs,
        'analysisFps':    analysisFps,
        'yoloConf':       yoloConf,
        'problems':       problems,
        'elapsedSec':     time.time() - t0,
        'error':          None,
    }


###############################################################################
def summarizeLabels(objects):
    """-> "person 1, unknown 12" for a row of classified objects.

    A sweep row is one configuration and can hold a dozen objects, so the grid
    has room for counts, not labels.  Confidences are dropped here deliberately;
    the per-object labels stay on row['objects'] for anything that wants them.
    """
    counts = {}
    for o in objects:
        label = str(o.get('yolo') or 'unknown').split(' ')[0]
        counts[label] = counts.get(label, 0) + 1
    # Most numerous first, then alphabetical, so the interesting class does not
    # move around between rows.
    return ', '.join('%s %d' % (k, v) for k, v in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


###############################################################################
def _fmtMs(ms):
    """-> local "HH:MM:SS" for a message the user will read."""
    return time.strftime('%H:%M:%S', time.localtime(ms / 1000.0))


###############################################################################
def describeProblems(problems):
    """-> a short human sentence about unusable footage, or "" if all was well.

    Worth saying out loud: every one of these makes the run analyse LESS footage
    than was asked for, and less footage means fewer objects -- indistinguishable
    from a camera that was genuinely quiet.
    """
    if not problems:
        return ""
    bits = []
    if problems.get('missing'):
        bits.append("%d clip(s) missing from disk" % len(problems['missing']))
    if problems.get('unopenable'):
        bits.append("%d clip(s) could not be opened" % len(problems['unopenable']))
    if problems.get('truncated'):
        worst = problems['truncated'][0]
        bits.append("%d clip(s) stopped decoding early (e.g. %s at frame %d of ~%d)"
                    % (len(problems['truncated']), worst[0], worst[1], worst[2]))
    if not bits:
        return ""
    return ("   WARNING: %s - less footage was analysed than the range covers, so"
            " these counts are a floor." % "; ".join(bits))


###############################################################################
def _openDetectionClient(logger=None):
    """-> (client, reason).  Exactly one of the two is None.

    Separated out so the sweep opens ONE client for all ten configurations.
    Constructing and pinging per call would be ten connects, and ten identical
    "not reachable" warnings whenever the back end is down.

    The two failures stay distinct because they need different actions:
    'unavailable' is a missing module -- a packaging fault -- while 'service
    down' just means the back end is not running, which is ordinary when the
    dialog is used against a stopped app.
    """
    try:
        from .DetectionServiceClient import DetectionServiceClient
    except ImportError as e:
        if logger is not None:
            logger.warning('replay: DetectionServiceClient unavailable: %s' % e)
        return None, 'unavailable'
    client = None
    try:
        client = DetectionServiceClient(logger)
        client.ping()
        return client, None
    except Exception as e:
        if logger is not None:
            logger.warning('replay: DetectionService not reachable: %s' % e)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        return None, 'service down'


###############################################################################
def _classify(objects, frames, msList, procSize, conf, logger=None,
              client=None, toleranceMs=_kClassifyToleranceMs):
    """Label each object via the running DetectionService, in place.

    Uses DetectionServiceClient directly rather than ObjectDetectorClient: the
    latter drops any work item older than 5 s, so every archived frame would be
    discarded as stale and every object would come back unlabelled.

    Pass `client` to reuse a connection across calls (the sweep does).  Without
    one a client is opened and closed here, as before.

    An object with no retained frame within `toleranceMs` of its midpoint is
    marked 'no frame' rather than classified against whatever happened to be
    nearest.  The unguarded fallback classified a person against footage 89
    seconds earlier and reported 'unknown', which is indistinguishable from the
    detector having looked and found nothing.
    """
    if not objects:
        return
    if not msList:
        for o in objects:
            o['yolo'] = 'no frame'
        return

    ownClient = client is None
    reason = None
    if ownClient:
        client, reason = _openDetectionClient(logger)
    if client is None:
        for o in objects:
            o['yolo'] = reason or 'service down'
        return

    import numpy as np

    msIndex = {ms: i for i, ms in enumerate(msList)}
    try:
        for o in objects:
            # Classify the middle of the track: most representative frame, and
            # one call per object keeps a whole-night run tractable.
            midMs = (o['firstMs'] + o['lastMs']) // 2
            idx = msIndex.get(midMs)
            if idx is None:
                idx = min(range(len(msList)),
                          key=lambda i: abs(msList[i] - midMs))
            if abs(msList[idx] - midMs) > toleranceMs:
                o['yolo'] = 'no frame'
                if logger is not None:
                    logger.warning(
                        'replay: no retained frame within %d ms of the object at '
                        '%s (nearest is %.1f s away) - not classified'
                        % (toleranceMs, _fmtMs(o['firstMs']),
                           abs(msList[idx] - midMs) / 1000.0))
                continue
            gray = frames[idx]
            rgb = np.repeat(gray[:, :, None], 3, axis=2)
            try:
                dets = client.yolo(rgb, conf)
            except Exception as e:
                o['yolo'] = 'error'
                if logger is not None:
                    logger.warning('replay: yolo failed: %s' % e)
                continue
            o['yolo'] = _bestOverlapping(dets, o['box'])
    finally:
        if ownClient:
            try:
                client.close()
            except Exception:
                pass


###############################################################################
def _bestOverlapping(dets, box):
    """Highest-scoring detection overlapping the motion box, else 'unknown'."""
    bx1, by1, bx2, by2 = box
    best, bestScore = 'unknown', 0.0
    for det in dets:
        try:
            label, score, x1, y1, x2, y2 = det[0], det[1], det[2], det[3], det[4], det[5]
        except (IndexError, TypeError):
            continue
        ix = max(0, min(bx2, x2) - max(bx1, x1))
        iy = max(0, min(by2, y2) - max(by1, y1))
        if ix <= 0 or iy <= 0:
            continue
        if score > bestScore:
            bestScore, best = score, '%s %.2f' % (label, score)
    return best
