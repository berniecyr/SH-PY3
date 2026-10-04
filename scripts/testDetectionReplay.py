#! /usr/bin/env python
#*****************************************************************************
#
# testDetectionReplay.py
#     Regression check for the Detection Test Suite's replay engine.
#
#*****************************************************************************

"""Check that a replay actually analyses the window it was asked about.

WHY THIS EXISTS
On 2026-09-01 the Detection Test Suite returned zero results for 05_Gate_lr
12:29-12:30 while the Search screen showed a person at 12:29:44 (objdb2 uid
629417, detConf 0.86).  Nothing was wrong with the motion pipeline: the sweep's
decode cache (_kMaxCachedFrames, 1200 frames) held exactly as much footage as
the prime consumed (_kDefaultPrimeMs, 120 s, at _kAnalysisFps 10), so decoding
stopped at 12:28:20 and not one frame of the requested window was ever scored.
Every row reported 0, which is indistinguishable from a quiet camera.

The same cap made replayRange retain the FIRST 1200 frames for the YOLO path,
so that person was classified against a frame 89 seconds earlier and came back
"unknown" -- a wrong answer that looks exactly like a right one.

Both failures are silent, and neither is visible in an object count alone.  So
this checks the two things that were never checked: that frames land INSIDE the
window, and that the frame chosen for classification is near the object it is
supposed to describe.

METHOD NOTES (HANDOFF section 8)
  * "A test that never saw red proves nothing."  The failure this check exists
    to catch was measured directly against the unmodified engine on 2026-09-01,
    before anything was changed: decodeClips returned 1200 frames spanning
    12:26:20-12:28:20 and framesInWindow was 0 for the 12:29-12:30 request.
    Check 1 asserts exactly that quantity.  To see it red again, set
    _kMaxCachedFrames back to 1200 AND drop the skipBeforeMs argument sweep()
    passes to decodeClips -- the head trim alone moves the decode forward by
    most of a minute, so the constant by itself no longer reproduces it.  (The
    old sweep() has no stageFn/yoloConf parameters, so this script cannot be
    pointed at an untouched pre-fix tree without a TypeError.)
  * "Don't anchor a regression check to live data."  The window is not
    hardcoded.  A pool of candidate detections is queried from objdb2, filtered
    to those whose clips are still on disk, and the newest is used.  If the pool
    is empty -- the disk cleaner has been through, or the fleet has been quiet --
    this SKIPS with a clear message rather than failing, because a red result
    has to mean the code is broken.

USAGE
    venv\\Scripts\\python.exe scripts\\testDetectionReplay.py
    venv\\Scripts\\python.exe scripts\\testDetectionReplay.py \\
        --camera 05_Gate_lr --start "2026-09-01 12:29" --stop "2026-09-01 12:30"

Exit codes: 0 pass, 1 fail, 2 skipped (no usable footage), 3 bad invocation.
Nothing is written to any database; clipdb is copied aside before it is read.
"""

import argparse
import datetime
import os
import pickle
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from appCommon.CommonStrings import kVideoFolder


# Exit codes.
_kPass, _kFail, _kSkip, _kUsage = 0, 1, 2, 3

# A candidate must be a real, moving detection -- travel is the stat that
# separates real tracks from night noise by ~18x on this fleet (HANDOFF 5.6).
# Quoted in ANALYSIS pixels, which is what objects.minCx..maxCy hold.
_kMinTravel = 200

# How far back to look for a candidate.  Beyond the retention window the clips
# are gone and every candidate would be skipped anyway.
_kMaxAgeHours = 24

# The replayed track must line up with the recorded one.  Generous on purpose:
# the point is to catch "the window was never decoded", not to re-litigate
# tracker tuning, and promotion timing legitimately shifts a start by seconds.
_kStartToleranceSec = 8.0
_kTravelTolerance = 0.35


###############################################################################
def _fmt(ms):
    return datetime.datetime.fromtimestamp(ms / 1000.0).strftime(
        "%Y-%m-%d %H:%M:%S")


###############################################################################
def _openRo(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                           uri=True)


###############################################################################
def _resolvePaths(args):
    """-> (dataDir, videoDir), from the back end's own prefs unless overridden."""
    dataDir, videoDir = args.data_dir, args.video_dir
    if dataDir and videoDir:
        return dataDir, videoDir

    # InstallPaths, NOT frontEnd.FrontEndUtils.getUserLocalDataDir: that one
    # reaches wx, and a bare wx call with no live wx.App pops a modal wxWidgets
    # Debug Alert that blocks the process forever with no output (HANDOFF 8 --
    # this script hung on exactly that before the import was changed).
    # InstallPaths imports only os and sys.
    from appCommon.InstallPaths import getUserDataDir
    userDir = getUserDataDir()
    prefsPath = os.path.join(userDir, "backEndPrefs")
    prefs = {}
    if os.path.exists(prefsPath):
        try:
            with open(prefsPath, "rb") as f:
                prefs = pickle.load(f)
        except Exception as e:
            print("warning: could not read backEndPrefs (%s)" % e)

    dataDir = dataDir or prefs.get("dataDir") or os.path.join(userDir, "videos")
    videoDir = videoDir or prefs.get("videoDir") or userDir
    return dataDir, videoDir


###############################################################################
def _findCandidate(dataDir, videoDir, args):
    """-> a dict describing one recorded detection to replay, or None.

    Deliberately a POOL query with a disk check, not a fixed window: an anchored
    check goes red for reasons that have nothing to do with this code.
    """
    if args.camera and args.start and args.stop:
        startMs = int(time.mktime(time.strptime(args.start,
                                                "%Y-%m-%d %H:%M")) * 1000)
        stopMs = int(time.mktime(time.strptime(args.stop,
                                               "%Y-%m-%d %H:%M")) * 1000)
        objDb = _openRo(os.path.join(dataDir, "objdb2"))
        try:
            row = objDb.execute(
                "SELECT uid, camLoc, timeStart, timeStop, type, "
                "       (maxCx-minCx)+(maxCy-minCy) FROM objects "
                "WHERE camLoc=? AND timeStop>=? AND timeStart<=? AND maxCx>=0 "
                "ORDER BY (maxCx-minCx)+(maxCy-minCy) DESC LIMIT 1",
                (args.camera, startMs, stopMs)).fetchone()
        finally:
            objDb.close()
        if row is None:
            print("No recorded object on %s between %s and %s to check against."
                  % (args.camera, args.start, args.stop))
            return None
        return {'uid': row[0], 'camLoc': row[1], 'timeStart': row[2],
                'timeStop': row[3], 'type': row[4], 'travel': row[5],
                'startMs': startMs, 'stopMs': stopMs}

    sinceMs = int((time.time() - _kMaxAgeHours * 3600) * 1000)
    objDb = _openRo(os.path.join(dataDir, "objdb2"))
    clipDb = _openRo(os.path.join(dataDir, "clipdb"))
    archiveRoot = os.path.join(videoDir, kVideoFolder)
    try:
        rows = objDb.execute(
            "SELECT uid, camLoc, timeStart, timeStop, type, "
            "       (maxCx-minCx)+(maxCy-minCy) AS travel FROM objects "
            "WHERE timeStart>=? AND maxCx>=0 AND type IN ('person','vehicle') "
            "  AND (maxCx-minCx)+(maxCy-minCy) > ? "
            "ORDER BY timeStart DESC LIMIT 200",
            (sinceMs, _kMinTravel)).fetchall()
        print("candidate pool: %d recorded person/vehicle tracks with travel > %d"
              " in the last %d h" % (len(rows), _kMinTravel, _kMaxAgeHours))

        for (uid, camLoc, tStart, tStop, oType, travel) in rows:
            # The window is the minute containing the track, so the check
            # exercises the same shape of request a user makes.
            startMs = tStart - (tStart % 60000)
            stopMs = startMs + 60000
            # Both the window and its whole prime must still be on disk, or a
            # legitimate cleaner deletion would read as a regression.
            need = clipDb.execute(
                "SELECT filename FROM clips WHERE camLoc=? AND lastMs>=? "
                "AND firstMs<? ORDER BY firstMs",
                (camLoc, startMs - 120000, stopMs)).fetchall()
            if not need:
                continue
            if any(not os.path.exists(os.path.join(
                    archiveRoot, r[0].replace('/', os.sep))) for r in need):
                continue
            return {'uid': uid, 'camLoc': camLoc, 'timeStart': tStart,
                    'timeStop': tStop, 'type': oType, 'travel': travel,
                    'startMs': startMs, 'stopMs': stopMs}
    finally:
        objDb.close()
        clipDb.close()
    return None


###############################################################################
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--camera", help="camera location, as stored in clipdb")
    parser.add_argument("--start", help='window start, "YYYY-MM-DD HH:MM"')
    parser.add_argument("--stop", help='window end, "YYYY-MM-DD HH:MM"')
    parser.add_argument("--data-dir", help="folder holding clipdb / objdb2")
    parser.add_argument("--video-dir", help="video storage root")
    parser.add_argument("--no-yolo", action="store_true",
                        help="skip the classification checks")
    args = parser.parse_args()

    if any([args.camera, args.start, args.stop]) and not all(
            [args.camera, args.start, args.stop]):
        print("--camera, --start and --stop must be given together.")
        return _kUsage

    dataDir, videoDir = _resolvePaths(args)
    print("dataDir : %s" % dataDir)
    print("videoDir: %s" % videoDir)
    if not os.path.exists(os.path.join(dataDir, "clipdb")):
        print("SKIP: no clipdb at %s" % dataDir)
        return _kSkip

    cand = _findCandidate(dataDir, videoDir, args)
    if cand is None:
        print("\nSKIP: no recorded detection with its footage still on disk."
              "\n      Not a failure -- retention or a quiet fleet.  Re-run"
              " after some activity,\n      or pass --camera/--start/--stop.")
        return _kSkip

    print("\ncandidate: %s uid=%d type=%s %s -> %s travel=%d (analysis px)"
          % (cand['camLoc'], cand['uid'], cand['type'],
             _fmt(cand['timeStart']), _fmt(cand['timeStop']), cand['travel']))
    print("window   : %s -> %s"
          % (_fmt(cand['startMs']), _fmt(cand['stopMs'])))

    from backEnd import DetectionReplay

    print("\nconstants: primeMs=%d  maxCachedFrames=%d  analysisFps=%s"
          " sweepWindowMs=%d"
          % (DetectionReplay._kDefaultPrimeMs, DetectionReplay._kMaxCachedFrames,
             DetectionReplay._kAnalysisFps, DetectionReplay._kSweepWindowMs))
    budget = ((DetectionReplay._kDefaultPrimeMs +
               DetectionReplay._kSweepWindowMs) / 1000.0 *
              (DetectionReplay._kAnalysisFps or 14.0))
    print("           prime + sweep window needs %.0f frames; cache holds %d"
          % (budget, DetectionReplay._kMaxCachedFrames))

    failures = []

    # ---- check 1: the sweep reaches the window at all ----------------------
    print("\n[1] sweep() decodes footage inside the window")
    t0 = time.time()
    sweepRes = DetectionReplay.sweep(
        dataDir, videoDir, cand['camLoc'], cand['startMs'], cand['stopMs'],
        stageFn=lambda t: sys.stdout.write("    %s\r" % t.ljust(60)))
    sys.stdout.write(" " * 66 + "\r")
    if sweepRes.get('error'):
        print("    FAIL: %s" % sweepRes['error'])
        failures.append("sweep returned an error")
    else:
        inWindow = sweepRes.get('framesInWindow', 0)
        print("    decoded %d frames, %d inside the window, %.1fs"
              % (sweepRes['framesDecoded'], inWindow, time.time() - t0))
        if inWindow <= 0:
            print("    FAIL: not one frame of the requested window was decoded."
                  "\n          The frame budget is being spent on the prime -- see"
                  " note 4 in\n          DetectionReplay's docstring.")
            failures.append("no frames decoded inside the window")
        else:
            print("    ok")

    # ---- check 2: some configuration finds the recorded object -------------
    print("\n[2] at least one sensitivity level reports the recorded object")
    best = None
    if sweepRes.get('rows'):
        counts = ", ".join("L%d/%s=%d" % (r['sensitivity'],
                                          "sh" if r['ignoreShadows'] else "--",
                                          r['nObjects'])
                           for r in sweepRes['rows'])
        print("    %s" % counts)
        for row in sweepRes['rows']:
            for o in row['objects']:
                gap = abs(o['firstMs'] - cand['timeStart']) / 1000.0
                if best is None or gap < best[0]:
                    best = (gap, o, row)
    if best is None:
        print("    FAIL: no configuration produced any object in the window.")
        failures.append("no objects at any sensitivity level")
    else:
        gap, obj, row = best
        print("    closest: level %d shadows %s, object at %s (%.1fs from the"
              " recorded track), travel %.0f vs %d recorded"
              % (row['sensitivity'], "on" if row['ignoreShadows'] else "off",
                 _fmt(obj['firstMs']), gap, obj['travel'], cand['travel']))
        if gap > _kStartToleranceSec:
            print("    FAIL: nothing started within %.0fs of the recorded track."
                  % _kStartToleranceSec)
            failures.append("replayed object does not line up in time")
        elif cand['travel'] and abs(obj['travel'] - cand['travel']) > \
                cand['travel'] * _kTravelTolerance:
            print("    FAIL: travel differs from the recording by more than"
                  " %.0f%%." % (100 * _kTravelTolerance))
            failures.append("replayed travel disagrees with the recording")
        else:
            print("    ok")

    # ---- check 3: the YOLO path keeps frames from the WINDOW ---------------
    if args.no_yolo:
        print("\n[3] skipped (--no-yolo)")
    else:
        print("\n[3] replayRange retains window frames for classification")
        conf = 0.25
        try:
            from backEnd import ImageCheckConfig
            conf = float(ImageCheckConfig.loadConfig().get(
                'YOLO_CONF_THRESHOLD',
                ImageCheckConfig.DEFAULTS['YOLO_CONF_THRESHOLD']))
        except Exception:
            pass

        # Intercept the frame choice rather than trusting the label: with the
        # service down every object reads "service down", which would let a
        # broken retention window pass unnoticed.
        seen = []
        realClassify = DetectionReplay._classify

        def spyClassify(objects, frames, msList, procSize, c, logger=None,
                        client=None, toleranceMs=DetectionReplay
                        ._kClassifyToleranceMs):
            seen.append((list(msList[:1]), list(msList[-1:]), len(msList),
                         [(o['firstMs'], o['lastMs']) for o in objects]))
            return realClassify(objects, frames, msList, procSize, c, logger,
                                client, toleranceMs)

        DetectionReplay._classify = spyClassify
        try:
            runRes = DetectionReplay.replayRange(
                dataDir, videoDir, cand['camLoc'], cand['startMs'],
                cand['stopMs'], yoloConf=conf)
        finally:
            DetectionReplay._classify = realClassify

        if runRes.get('error'):
            print("    FAIL: %s" % runRes['error'])
            failures.append("replayRange returned an error")
        elif not seen:
            print("    FAIL: classification never ran.")
            failures.append("classification never ran")
        else:
            first, last, nKept, objs = seen[0]
            print("    kept %d frames spanning %s .. %s"
                  % (nKept, _fmt(first[0]) if first else "-",
                     _fmt(last[0]) if last else "-"))
            if first and first[0] < cand['startMs']:
                print("    FAIL: retained a priming frame (%s) from before the"
                      " window (%s)." % (_fmt(first[0]), _fmt(cand['startMs'])))
                failures.append("YOLO retained priming frames")
            else:
                # Distance from each object's midpoint to the nearest retained
                # frame is the quantity that was silently wrong: the old code
                # snapped to whatever was closest, 89 s away, and reported
                # 'unknown'.  _classify now refuses beyond its tolerance, so a
                # 'no frame' on an object inside the window means retention is
                # still picking the wrong frames.
                worstSec = 0.0
                for (fMs, lMs) in objs:
                    midMs = (fMs + lMs) // 2
                    if first and last and not (first[0] <= midMs <= last[0]):
                        worstSec = max(worstSec,
                                       min(abs(midMs - first[0]),
                                           abs(midMs - last[0])) / 1000.0)
                print("    worst object midpoint outside the retained span:"
                      " %.1fs" % worstSec)
                stranded = [o for o in runRes['objects']
                            if o.get('yolo') == 'no frame']
                if stranded:
                    print("    FAIL: %d object(s) inside the window had no frame"
                          " near enough to classify." % len(stranded))
                    failures.append("objects inside the window had no frame")
                else:
                    labels = ", ".join(str(o.get('yolo')) for o in
                                       runRes['objects'][:5]) or "(none)"
                    print("    labels: %s" % labels)
                    print("    ok")

    # ---- verdict -----------------------------------------------------------
    print("\n" + "=" * 70)
    if failures:
        print("FAIL (%d): %s" % (len(failures), "; ".join(failures)))
        return _kFail
    print("PASS")
    return _kPass


if __name__ == '__main__':
    sys.exit(main())
