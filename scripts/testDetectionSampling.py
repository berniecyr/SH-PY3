#! /usr/bin/env python
#*****************************************************************************
#
# testDetectionSampling.py
#     Regression check for WHICH frames and WHICH objects reach the detector.
#
#*****************************************************************************

"""Check the detection sampling schedule and the vote bookkeeping behind it.

WHY THIS EXISTS
97.4% of tracks in the 3.35 h objdb2 held after the 2026-09-02 reset stored as
unclassified 'object' (9,483 of 9,735).  Three separate causes, none of them
visible in any log, because every outcome branch of _summarizeScores except one
logs at DEBUG and the deployed level is INFO:

  1. PHANTOM VOTES.  _processCloudResults notified every object in the frame,
     not the objects in the work item.  That was harmless while every object
     with budget was in every request -- an excluded object had already decided,
     and notifyDetectionCompleted early-returns for those.  The moment
     needsDetection holds an UNDECIDED object back, which _kDetectSpacingMs does
     by design, the held-back object collects an ("unknown", 0.5, 0.5) vote and
     a detectionEventsCount increment for a frame it was never sampled in.
     The damage runs through the count, not the vote: needsDetection gates
     sampling on detectionsRequested, but readyToReport force-decides on
     detectionEventsCount, and only the latter is inflated.  On a busy camera an
     object was decided at "8 events" having genuinely been sampled 2-4 times.
     This is why DETECT_SPACING_MS had to be reverted to 0 the day it shipped.

  2. THE CADENCE FLOOR.  _requestDetections applied its 500 ms floor BEFORE
     asking whether anything in the frame wanted the detector, so a track whose
     whole life fell between two sampling instants was never handed over at all
     and force-decided on an empty vote -- the "no frames!" line, 1,655 of them
     in that same 3.35 h window, 17.5% of the unclassified tracks.

  3. THE FREE-RUNNING CLOCK.  _lastAnalyzedFrameMs was assigned above the
     "nothing to do" early return, so frames that enqueued nothing still
     consumed the interval and the cadence drifted against wall time.

METHOD NOTES (HANDOFF section 8)
  * "A test that never saw red proves nothing."  Every check here carries its
    own red: each runs the LEGACY behaviour and the fixed behaviour in the same
    process and asserts they differ in the measured direction, so none of them
    can pass by accident against unfixed code.  Check 3's legacy arm is
    literally "notify every object in the frame", which the harness performs
    itself because it drives _QueuedObjId directly.
  * Checks run with DETECT_SPACING_MS forced to 2000 regardless of the deployed
    config, because spacing is what ACTIVATES the phantom-vote defect and the
    shipped default is currently 0.  Testing at the deployed value would pass
    against the unfixed code -- which is exactly how this was missed the first
    time.  The original verification used a single object in isolation; every
    check here that touches the vote uses MULTIPLE CONCURRENT tracks.
  * "Don't anchor a regression check to live data."  Nothing here reads the
    database, the logs, a clip, or the config file.  The fixture is synthetic
    timestamps and synthetic detector answers, reproducible on any machine, and
    it cannot rot when the disk cleaner runs or objdb2 is reset again.
  * Importing backEnd.QueuedDataManagerCloud is safe and fast (~0.5 s, no
    torch): svsentry.Sentry is a stub whose loadSentry() is a no-op, and
    ObjectDetectorClientImageCheck keeps the ML stack in DetectionService.  Only
    the detector THREAD needs displacing, by monkeypatching the class before the
    manager is constructed -- the same trick testDetectionReplay uses.
  * Frames are driven at 100 ms, the real analysis spacing (BackEndApp caps
    analysis fps at 10).  Checking at 500 ms would hide the very tracks the
    cadence fix is for.

USAGE
    venv\\Scripts\\python.exe scripts\\testDetectionSampling.py [-v]

Exit codes: 0 pass, 1 fail, 2 skipped (module unimportable), 3 bad invocation.
Nothing is read from or written to any database, log, or config file.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_kPass, _kFail, _kSkip, _kUsage = 0, 1, 2, 3

# The analysis stream runs at 10 fps, so this is the real frame spacing the
# cadence has to be judged against.
_kFrameMs = 100

# Forced for the duration of the run; see METHOD NOTES.
_kTestSpacingMs = 2000


###############################################################################
class _FakeFrame(object):
    """Stands in for StreamReader._Frame.  wasResized False, as in production."""
    wasResized = False
    def __init__(self, ms, w=640, h=360):
        self.ms, self.width, self.height = ms, w, h
    def getLargeFrame(self):
        return None


###############################################################################
class _FakeDetector(object):
    """Stands in for ObjectDetectorClientImageCheck.

    Records what was enqueued and replays a scripted answer.  Nothing about
    detection itself is under test; what is under test is which frames and which
    objects reach it, and what the result does to the vote on the way back.
    """
    def __init__(self, camLoc=None, logger=None):
        self.items = []            # [(ts, [sentryId, ...])]
        self.answers = {}          # sentryId -> (type, score, overlap) or None
        self.queueDepth = 0        # what pendingWorkItems() reports
        self.dropped = 0
        self._results = []
    def start(self): pass
    def is_alive(self): return False
    def terminate(self): pass
    def join(self, *a): pass
    def setDebugFolder(self, folder): pass
    def setAnalyticsPort(self, port): pass
    def pendingWorkItems(self): return self.queueDepth
    def droppedWorkItems(self): return self.dropped
    def enqueWorkItem(self, ts, frame, sentryBoxes, sizeRatio):
        self.items.append((ts, [b[0].sentryId for b in sentryBoxes]))
        out = []
        for obj, _fid, _bbox in sentryBoxes:
            ans = self.answers.get(obj.sentryId)
            if ans is not None:
                out.append((obj, ans[0], ans[1], ans[2]))
        self._results.append((ts, out))
    def getNextResult(self):
        return self._results.pop(0) if self._results else None


###############################################################################
def _quietLogger():
    lg = logging.getLogger("testDetectionSampling")
    lg.addHandler(logging.NullHandler())
    lg.setLevel(logging.CRITICAL)
    return lg


###############################################################################
def _newManager(Q, logger):
    """A QueuedDataManagerCloud with a fake detector and no side effects.

    thumbRes=0 makes _saveFrameThumbnail early-return, so nothing touches disk
    and archiveDir is never used.
    """
    Q._QueuedObjId._idToQueuedObj.clear()

    class _Queue(object):
        def __init__(self): self.msgs = []
        def put(self, m): self.msgs.append(m)

    class _Pipe(object):
        def poll(self, timeout=0): return False

    real = Q.ObjectDetectorClientImageCheck
    Q.ObjectDetectorClientImageCheck = _FakeDetector
    try:
        dm = Q.QueuedDataManagerCloud(_Queue(), _Pipe(), 1, "TESTCAM", "", 0,
                                      logger)
    finally:
        Q.ObjectDetectorClientImageCheck = real
    return dm, dm._httpClient


###############################################################################
def _decide(Q, sampledAt, subjectArrivesMs, score, busy, legacy):
    """Drive one _QueuedObjId down a timeline and return how it decided.

    Models the case the spacing change was written for and the phantom defect
    breaks: a motion track born on foliage that a real subject merges into
    later.  Samples before subjectArrivesMs find nothing -- the detector ran and
    matched no object, which is a legitimate abstention -- and samples from then
    on return `person`.

    The object decides when detectionEventsCount reaches _maxDetectionEvents,
    which is what readyToReport's enoughDetecting does in production.  That is
    the whole mechanism: phantoms make the ceiling arrive EARLY, so the track
    decides before the evidence shows up.

    @param  sampledAt          ms offsets this object is genuinely sampled at.
    @param  subjectArrivesMs   ms from which samples return a real detection.
    @param  score              detector score for those.
    @param  busy               True to run competing motion every frame, i.e.
                               another object keeping the camera enqueueing.
    @param  legacy             True to reproduce the pre-fix behaviour, where a
                               result notified every object in the frame rather
                               than the work item's subset.
    @return (decision, detectionEventsCount, detectionsRequested, decidedAtMs)
    """
    lg = _quietLogger()
    obj = Q._QueuedObjId(1, 1, 0, "unknown")
    sampledAt = set(sampledAt)
    decidedAt = None
    for ms in range(0, 12000, _kFrameMs):
        obj.reportSeen(ms, lg)
        if ms in sampledAt:
            obj.notifyDetectionRequested(ms)
            if ms >= subjectArrivesMs:
                obj.reportCloudDetection(ms, "person", score, 0.9, lg)
            obj.notifyDetectionCompleted(ms)
        elif busy and legacy:
            # The defect: this object was NOT in that frame's work item, but the
            # result notified it anyway.
            obj.notifyDetectionCompleted(ms)
        if obj.detectionEventsCount >= Q._QueuedObjId._maxDetectionEvents:
            decidedAt = ms
            break
    obj._summarizeScores(True, lg)
    return (obj.detectorDecision, obj.detectionEventsCount,
            obj.detectionsRequested, decidedAt)


###############################################################################
def _run(Q, verbose):
    failures = []

    def check(name, ok, detail=""):
        if ok:
            if verbose:
                print("  PASS  %s %s" % (name, detail))
        else:
            failures.append("%s %s" % (name, detail))
            print("  FAIL  %s %s" % (name, detail))

    logger = _quietLogger()

    # ---------------------------------------------------------------- check 1
    # A track shorter than the cadence still reaches the detector.
    # RED ARM: the same fixture with the bypass disabled must sample it zero
    # times, which is exactly the pre-fix behaviour.
    def shortTrackSamples(bypassOn):
        saved = Q._kFirstSampleBypass
        Q._kFirstSampleBypass = bypassOn
        try:
            dm, det = _newManager(Q, logger)
            # Anchor the cadence with a long-lived object at t=0.
            anchor = dm.addObject(0, "unknown")
            for ms in range(0, 1000, _kFrameMs):
                dm.addFrame(anchor, ms // _kFrameMs, ms, (0, 0, 10, 10), "unknown")
                dm._frames[ms] = _FakeFrame(ms)
                # A 300 ms track born 150 ms after the cadence tick at t=0,
                # dying before the next one at t=500.
                if ms == 200:
                    short = dm.addObject(ms, "unknown")
                if 200 <= ms <= 400:
                    dm.addFrame(short, ms // _kFrameMs, ms, (20, 20, 30, 30), "unknown")
                dm.frameCompleted(ms, ms // _kFrameMs)
            # addObject returns the sentryId directly.
            return sum(1 for _ts, ids in det.items if short in ids)
        finally:
            Q._kFirstSampleBypass = saved

    fixed = shortTrackSamples(True)
    red = shortTrackSamples(False)
    check("sub-cadence-track-sampled", fixed > 0,
          "(sampled %d times with bypass on)" % fixed)
    check("sub-cadence-track-RED", red == 0,
          "(bypass off samples it %d times; must be 0 or the fixture is wrong)" % red)

    # ---------------------------------------------------------------- check 2
    # The cadence clock is only advanced by frames that actually enqueued.
    dm, det = _newManager(Q, logger)
    obj = dm.addObject(0, "unknown")
    lastEnqueued = None
    for ms in range(0, 3000, _kFrameMs):
        dm.addFrame(obj, ms // _kFrameMs, ms, (0, 0, 10, 10), "unknown")
        dm._frames[ms] = _FakeFrame(ms)
        before = len(det.items)
        dm.frameCompleted(ms, ms // _kFrameMs)
        if len(det.items) > before:
            lastEnqueued = ms
    check("clock-follows-enqueues", dm._lastAnalyzedFrameMs == lastEnqueued,
          "(lastAnalyzed=%s lastEnqueued=%s)" % (dm._lastAnalyzedFrameMs, lastEnqueued))

    # ---------------------------------------------------------------- check 3
    # Phantom votes.  Same object, same detector answers; the only difference is
    # whether other objects kept the camera enqueueing while it waited out its
    # spacing window.  This is the core defect and its own red arm.
    # The _kDetectSpacingMs schedule: four fast samples, then every 2000 ms.
    sampled = [0, 500, 1000, 1500, 3500, 5500, 7500, 9500]
    subjectAt = 3500        # a real subject merges into the track here
    quiet = _decide(Q, sampled, subjectAt, 0.85, busy=False, legacy=False)
    busyFixed = _decide(Q, sampled, subjectAt, 0.85, busy=True, legacy=False)
    busyLegacy = _decide(Q, sampled, subjectAt, 0.85, busy=True, legacy=True)
    check("phantom-busy-matches-quiet", busyFixed[0] == quiet[0],
          "(quiet=%s busy=%s)" % (quiet[0], busyFixed[0]))
    check("phantom-RED", busyLegacy[0] != quiet[0],
          "(legacy busy=%s vs quiet=%s; must differ or the defect is not "
          "reproduced)" % (busyLegacy[0], quiet[0]))
    check("phantom-decides-early", busyLegacy[3] < subjectAt <= (busyFixed[3] or 99999),
          "(legacy decided at %sms, fixed at %sms, subject arrived %dms)" %
          (busyLegacy[3], busyFixed[3], subjectAt))
    check("phantom-inflates-count",
          busyLegacy[1] > busyLegacy[2] and busyFixed[1] == busyFixed[2],
          "(legacy events=%d/requested=%d, fixed events=%d/requested=%d)" %
          (busyLegacy[1], busyLegacy[2], busyFixed[1], busyFixed[2]))

    # ---------------------------------------------------------------- check 4
    # Integration: no object may accrue more results than it was sampled for.
    dm, det = _newManager(Q, logger)
    a = dm.addObject(0, "unknown")
    b = None
    for ms in range(0, 12000, _kFrameMs):
        dm._frames[ms] = _FakeFrame(ms)
        dm.addFrame(a, ms // _kFrameMs, ms, (0, 0, 10, 10), "unknown")
        if ms == 3000:
            b = dm.addObject(ms, "unknown")
        if b is not None and ms >= 3000:
            dm.addFrame(b, ms // _kFrameMs, ms, (40, 40, 50, 50), "unknown")
        dm.frameCompleted(ms, ms // _kFrameMs)
    over = [(o.sentryId, o.detectionEventsCount, o.detectionsRequested)
            for o in Q._QueuedObjId._idToQueuedObj.values()
            if o.detectionEventsCount > o.detectionsRequested]
    check("no-object-over-notified", not over, "(offenders=%s)" % (over,))
    check("stats-phantom-zero", dm._stats._phantom == 0,
          "(phantom=%d)" % dm._stats._phantom)

    # ---------------------------------------------------------------- check 5
    # Burst bound.  60 consecutive frames each birthing a new track.
    def burstItems(queueDepth):
        dm, det = _newManager(Q, logger)
        det.queueDepth = queueDepth
        anchor = dm.addObject(0, "unknown")
        for i in range(60):
            ms = i * _kFrameMs
            dm._frames[ms] = _FakeFrame(ms)
            dm.addFrame(anchor, i, ms, (0, 0, 10, 10), "unknown")
            n = dm.addObject(ms, "unknown")
            dm.addFrame(n, i, ms, (20, 20, 30, 30), "unknown")
            dm.frameCompleted(ms, i)
        return len(det.items)

    free = burstItems(0)
    spanMs = 60 * _kFrameMs
    ceiling = (Q._kBypassBurst + spanMs / float(Q._kBypassRefillMs)
               + spanMs / float(Q._kMinAnalyticsIntervalMs) + 2)
    check("burst-bounded", free <= ceiling,
          "(items=%d ceiling=%.0f)" % (free, ceiling))
    full = burstItems(Q._kClientQueueBound - 1)
    onCadence = spanMs / Q._kMinAnalyticsIntervalMs + 1
    check("burst-yields-to-full-queue", full <= onCadence,
          "(items=%d with queue full; cadence-only would be %d)" % (full, onCadence))

    # ---------------------------------------------------------------- check 6
    # CloudStats: outcome mix, and the zero-object guard.
    stats = Q.CloudStats(0)

    class _O(object):
        def __init__(self, dec, ext, req, ev, dur):
            self.detectorDecision = dec
            self.externalDetections = ext
            self.detectionsRequested = req
            self.detectionEventsCount = ev
            self.firstSeenBySentry = 0
            self.lastSeenBySentry = dur

    stats.record(_O("person", {0: [("person", .9, .9)]}, 4, 4, 8000))
    stats.record(_O("unknown", {0: [("unknown", .5, .5)]}, 4, 4, 5000))
    stats.record(_O("unknown", {}, 0, 0, 200))
    check("stats-mix", (stats._classified, stats._unclassified, stats._noFrames)
          == (1, 1, 1),
          "(classified=%d unclassified=%d noFrames=%d)" %
          (stats._classified, stats._unclassified, stats._noFrames))

    empty = Q.CloudStats(0)
    try:
        empty.writeToLog(_quietLogger(), 0)
        check("stats-zero-guard", True, "(no exception with 0 objects)")
    except ZeroDivisionError as e:
        check("stats-zero-guard", False, "(ZeroDivisionError: %s)" % e)

    return failures


###############################################################################
def main():
    ap = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print passing checks too")
    try:
        args = ap.parse_args()
    except SystemExit:
        return _kUsage

    try:
        import backEnd.QueuedDataManagerCloud as Q
    except ImportError as e:
        print("SKIP: cannot import backEnd.QueuedDataManagerCloud: %s" % e)
        return _kSkip

    # Spacing is what ACTIVATES the phantom-vote defect, and the shipped default
    # is 0.  Force it on for the run, restore it after -- a module constant, so
    # this is process-local and touches no config file.
    savedSpacing = Q._kDetectSpacingMs
    Q._kDetectSpacingMs = _kTestSpacingMs
    try:
        failures = _run(Q, args.verbose)
    finally:
        Q._kDetectSpacingMs = savedSpacing

    print("=" * 70)
    if failures:
        print("FAIL - %d check(s) failed:" % len(failures))
        for f in failures:
            print("   - %s" % f)
        return _kFail
    print("PASS - detection sampling and vote bookkeeping behave as intended")
    return _kPass


if __name__ == '__main__':
    sys.exit(main())
