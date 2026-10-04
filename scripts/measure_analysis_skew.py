#! /usr/bin/env python
#*****************************************************************************
#
# measure_analysis_skew.py
#     Measure how far detection timestamps sit from recording timestamps.
#
#*****************************************************************************

"""Measure the skew between the analysis clock and the recording clock.

WHY THIS EXISTS
A detection box drawn over recorded video does not land on its subject.  The
same detection is rendered in four places and only one of them is right:

    results-list preview   analysis thumbnail + analysis box   correct
    playback overlay       recorded frame     + analysis box   box trails subject
    event snapshot         recorded frame     + analysis box   same, smaller on HR
    playback scrubber      analysis event times - recording clip start

The preview is right only by accident: a thumbnail is written from the analysis
pipeline's own frame buffer and saved under the analysis timestamp
(QueuedDataManagerCloud._saveFrameThumbnail), so its image and its boxes share a
timebase.  Everything else pairs an analysis box with a recorded frame, and the
two clocks disagree by 0.5-2s -- enough to walk a person clean out of their own
box.  The placement maths are not at fault; all three renderers compute an
identical position for the same box.

The cause is in StreamReader: analysis frames are piped from the recorder's
ffmpeg as headerless `rawvideo` over TCP, carrying no timestamps at all, so the
receiver stamps each frame when it comes off the socket -- after decode, scale,
hwdownload and the socket itself.  The archive is dated at demux.  The gap
between those two points is what this tool measures.

WHAT THIS TOOL IS FOR
It does NOT produce a correction to apply.  StreamReader already has correction
machinery (_analysisLagMs -> _stampFrameMs, behind SV_ANALYSIS_LAG_CORRECT) and
it is switched off because its ESTIMATOR -- counting frames -- cannot tell a
frame still in flight from one the socket never received, so its estimate creeps
upward with run age.  That comment's own remedy is "drop frame-counting for a
time-domain match", which is what this does.

But the skew is not a constant either, so calibrating one number per camera is
not the fix.  This tool exists to characterise the DISTRIBUTION -- how far the
skew moves, with what, and over what timescale -- so that the case for fixing it
per-frame at capture rests on numbers; and to be the instrument that later proves
residual skew went to zero.

HOW IT MEASURES
The evidence is already on disk, so nothing needs to be running:

    1. take a thumbnail; its filename IS the analysis timestamp
    2. find the recorded segment(s) covering that time
    3. decode frames across a window either side, comparing each against the
       thumbnail (small, greyscale, contrast-normalised)
    4. the offset of the best-matching frame is that sample's skew

Recorded frame times come from the MP4 sample tables (Mp4Index), not
ClipReader's int(idx*1000/fps) ladder -- that ladder drifts up to ~105ms on
these files, which is a large fraction of what is being measured.

A measurement is only worth having if it refuses when it cannot tell.  On a
static night scene every frame matches every other equally well, and an ungated
version of this returned -4.05s and +3.51s on exactly such footage.  Two gates
below (scene motion, and a sharp minimum) reject those instead of reporting
them; rejections are always shown with their reason.

USAGE
    python scripts/measure_analysis_skew.py
    python scripts/measure_analysis_skew.py --cameras 08_FrontStep_lr,09_Jungle
    python scripts/measure_analysis_skew.py --day 2026-08-30 --samples 12
    python scripts/measure_analysis_skew.py --cameras 06_Garage_lr --dump out/

Read-only: opens the databases read-only and never writes to the archive.
"""

import argparse
import glob
import os
import pickle
import statistics
import sys
import time

# Run from the repo root without installing anything.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
from PIL import Image

from appCommon.CommonStrings import kClipDbFile, kPrefsFile, kVideoFolder
from appCommon.CommonStrings import kThumbsSubfolder
from appCommon.InstallPaths import getUserDataDir
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from videoLib2.python.Mp4Index import videoTrackIndex


###############################################################################
# Matching

# Frames are compared at this size, in greyscale.  Small on purpose: we are
# identifying WHICH frame this is, not judging image quality, and a small
# normalised image is dominated by layout rather than by sensor noise or the
# JPEG/H.264 difference between the two encodings of the same moment.
_kCompareSize = (96, 54)

# How far either side of the thumbnail's timestamp to look.  Fleet skews run
# 0.4-2.0s; 4s covers those with headroom without decoding half a segment.
_kDefaultWindowMs = 4000

# GATE 1 -- the scene has to move.  A true match scores 0.013-0.043 against
# these cameras; on a static scene EVERY frame scores about the same and the
# "best" one is noise.  Require the best score to beat the window's median by
# this fraction of that median.
_kMinRelief = 0.25

# GATE 2 -- the minimum has to be sharp.  Compare the best score against the
# best score found more than _kSharpExclusionMs away: if a frame two seconds
# distant is nearly as good a match, the scene repeats and the time we picked
# means nothing.  The runner-up must be at least this much worse.
_kSharpRatio = 1.15
_kSharpExclusionMs = 700

# GATE 3 -- the match must not sit against the edge of the search window.  When
# the true match lies OUTSIDE the window the best score is just the least-bad of
# a bad set, and it lands near a boundary.  A first fleet run showed exactly
# that: every extreme reading (-3.89, -3.83, -3.78, +3.96 against a 4.00s
# window) was within 0.25s of an edge, and between them they inflated one
# camera's reported spread from ~1s to 6.94s.  A measurement that cannot see
# where the truth is must say so, not report the wall it hit.
_kEdgeFraction = 0.90

# A camera whose samples are spread wider than this could never have been
# served by one calibrated constant.  Used only for the verdict line.
_kConstantViableSpreadMs = 500

# Below this many accepted samples a camera says nothing about spread, so it is
# left out of the verdict entirely rather than allowed to shape it.
_kMinSamplesForVerdict = 5


###############################################################################
def _prep(bgr):
    """Reduce a BGR frame to the small contrast-normalised array used for matching.

    Normalising kills the exposure/gamma difference between the thumbnail's
    JPEG and the recorded H.264 of the same instant, which would otherwise
    swamp the much smaller difference between adjacent frames.

    cv2 rather than PIL because this runs on every decoded frame in the search
    window -- PIL's LANCZOS resize of a 720p frame costs more than decoding it.
    Both sides of the comparison go through this same function; processing them
    differently would bias the match.

    @param  bgr  Frame as a BGR uint8 ndarray.
    @return arr  float32 array of _kCompareSize, zero mean and unit variance.
    """
    small = cv2.resize(bgr, _kCompareSize, interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return (grey - grey.mean()) / (grey.std() + 1e-6)


###############################################################################
def _prepFile(path):
    """_prep() for an image on disk (the thumbnail side of the comparison)."""
    bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    return None if bgr is None else _prep(bgr)


###############################################################################
def _thumbMsFromName(path):
    """The analysis timestamp a thumbnail was saved under, or None.

    Two naming schemes are in the archive: the current epoch-ms name, and a
    legacy '%Y-%m-%d-%H%M%S' one.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        return int(stem)
    except ValueError:
        pass
    try:
        return int(time.mktime(time.strptime(stem, '%Y-%m-%d-%H%M%S')) * 1000)
    except ValueError:
        return None


###############################################################################
class SkewMeasurer(object):
    """Measures analysis-vs-recording skew for one installation."""

    def __init__(self, clipMgr, videoDir, windowMs=_kDefaultWindowMs):
        self._clipMgr = clipMgr
        self._videoDir = videoDir
        self._windowMs = windowMs
        self._indexCache = {}

    ###########################################################
    def _frameTimes(self, fileName):
        """Absolute ms of every frame in a segment, from its MP4 sample table.

        @param  fileName  Archive-relative path of the segment.
        @return times     Ascending absolute ms, one per frame; [] if unknown.
        """
        if fileName in self._indexCache:
            return self._indexCache[fileName]

        fileStart, _ = self._clipMgr.getFileTimeInformation(fileName)
        index = videoTrackIndex(os.path.join(self._videoDir, fileName))
        times = [fileStart + t for t in index[0]] if index else []
        self._indexCache[fileName] = times
        return times

    ###########################################################
    def _segmentsCovering(self, camLoc, firstMs, lastMs):
        """The segments holding any part of [firstMs, lastMs], in order.

        The window routinely straddles a segment boundary -- a 2s skew near the
        end of a 60s segment lands in the next one -- and a search confined to
        the segment containing the thumbnail would simply fail to find the
        match there, or worse, find a false one at the edge.
        """
        fileName = self._clipMgr.getFileAt(camLoc, firstMs, self._windowMs)
        if not fileName:
            fileName = self._clipMgr.getFileAt(camLoc, lastMs, self._windowMs)
        if not fileName:
            return []

        # Walk back to the first segment that reaches into the window...
        while True:
            prev = self._clipMgr.getPrevFile(fileName)
            if not prev:
                break
            _, prevStop = self._clipMgr.getFileTimeInformation(prev)
            if prevStop < firstMs:
                break
            fileName = prev

        # ...then forward, collecting everything up to the window's end.
        out = []
        while fileName:
            start, stop = self._clipMgr.getFileTimeInformation(fileName)
            if start > lastMs:
                break
            if stop >= firstMs and os.path.exists(
                    os.path.join(self._videoDir, fileName)):
                out.append(fileName)
            fileName = self._clipMgr.getNextFile(fileName)
        return out

    ###########################################################
    def measure(self, camLoc, thumbPath):
        """Measure one sample.

        @param  camLoc     The camera location.
        @param  thumbPath  Full path to a thumbnail JPEG.
        @return result     A dict with 'skewMs' and the matched time on
                           success, or 'reject' naming why it could not be
                           measured.
        """
        thumbMs = _thumbMsFromName(thumbPath)
        if thumbMs is None:
            return {'reject': 'unparseable thumbnail name'}
        ref = _prepFile(thumbPath)
        if ref is None:
            return {'reject': 'unreadable thumbnail'}

        firstMs = thumbMs - self._windowMs
        lastMs = thumbMs + self._windowMs
        segments = self._segmentsCovering(camLoc, firstMs, lastMs)
        if not segments:
            return {'reject': 'no recorded video around this time'}

        scores = []          # (score, absMs)
        for fileName in segments:
            times = self._frameTimes(fileName)
            if not times:
                continue
            cap = cv2.VideoCapture(os.path.join(self._videoDir, fileName),
                                   cv2.CAP_FFMPEG)
            if not cap.isOpened():
                continue
            try:
                # Seek to the first frame inside the window rather than
                # decoding the whole segment.
                idx = 0
                while idx < len(times) and times[idx] < firstMs:
                    idx += 1
                if idx >= len(times):
                    continue
                if idx:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                while idx < len(times) and times[idx] <= lastMs:
                    ok, bgr = cap.read()
                    if not ok:
                        break
                    scores.append((float(np.mean(np.abs(_prep(bgr) - ref))),
                                   times[idx]))
                    idx += 1
            finally:
                cap.release()

        if len(scores) < 5:
            return {'reject': 'too few recorded frames in the window'}

        best, bestMs = min(scores)
        median = statistics.median(s for s, _ in scores)

        # GATE 1: did the scene move enough to tell frames apart at all?
        if median <= 0 or (median - best) / median < _kMinRelief:
            return {'reject': 'static scene (no usable relief)'}

        # GATE 2: is this minimum sharp, or does a distant frame match too?
        rivals = [s for s, ms in scores if abs(ms - bestMs) > _kSharpExclusionMs]
        if rivals and min(rivals) < best * _kSharpRatio:
            return {'reject': 'ambiguous match (scene repeats)'}

        # GATE 3: did we actually bracket the match, or hit the window wall?
        if abs(bestMs - thumbMs) > self._windowMs * _kEdgeFraction:
            return {'reject': 'match at window edge (widen --window)'}

        return {'skewMs': bestMs - thumbMs, 'thumbMs': thumbMs,
                'matchMs': bestMs, 'score': best, 'relief': (median - best) / median,
                'fileName': segments[0] if len(segments) == 1 else None,
                'thumbPath': thumbPath,
                'segment': self._clipMgr.getFileAt(camLoc, bestMs, 0)}
    ###########################################################
    def dumpMatch(self, camLoc, result, outDir):
        """Write a side-by-side image so a human can confirm the match.

        A number nobody can check is how the previous estimator shipped wrong.
        """
        from PIL import ImageDraw
        try:
            os.makedirs(outDir, exist_ok=True)
            thumb = Image.open(result['thumbPath']).convert('RGB')
            seg = result.get('segment')
            if not seg:
                return
            times = self._frameTimes(seg)
            idx = min(range(len(times)),
                      key=lambda i: abs(times[i] - result['matchMs']))
            cap = cv2.VideoCapture(os.path.join(self._videoDir, seg),
                                   cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, bgr = cap.read()
            cap.release()
            if not ok:
                return
            match = Image.fromarray(bgr[:, :, ::-1])
            h = 240
            thumb.thumbnail((10000, h), Image.Resampling.LANCZOS)
            match.thumbnail((10000, h), Image.Resampling.LANCZOS)
            sheet = Image.new('RGB', (thumb.width + match.width + 12, h + 22),
                              (24, 24, 24))
            sheet.paste(thumb, (0, 22))
            sheet.paste(match, (thumb.width + 12, 22))
            d = ImageDraw.Draw(sheet)
            d.text((2, 4), 'analysis thumb %s' % time.strftime(
                '%H:%M:%S', time.localtime(result['thumbMs'] / 1000.)),
                fill=(255, 230, 120))
            d.text((thumb.width + 14, 4),
                   'best recorded match %s  (skew %+.2fs)' % (time.strftime(
                       '%H:%M:%S', time.localtime(result['matchMs'] / 1000.)),
                       result['skewMs'] / 1000.0), fill=(255, 230, 120))
            sheet.save(os.path.join(outDir,
                                    '%s-%d.png' % (camLoc, result['thumbMs'])))
        except Exception as e:
            print('   (dump failed: %r)' % (e,))


# Thumbnails are written on a timer, but far more often while something is
# moving -- the 12:43 walk-past produced one a second against a normally sparse
# background.  So a small gap to the previous thumbnail is a reliable, free
# signal that the scene was busy, which is exactly the footage the matcher can
# measure.  Anything at or under this counts as a burst.
_kBurstGapMs = 4000


###############################################################################
def _pickThumbs(videoDir, camLoc, day, count, dense=False,
                afterMs=None, beforeMs=None):
    """Choose thumbnails to sample: spread across the day, but where it moved.

    With dense=True, take `count` CONSECUTIVE thumbnails from the busiest run
    instead.  Spread sampling puts every sample in a different segment, so it
    can only show how the skew varies BETWEEN segments; dense sampling is what
    shows whether it holds steady WITHIN one.  The two answer different halves
    of "is a constant per camera good enough".

    Spread matters because a cluster would measure one moment's backlog over and
    over and say nothing about how the skew behaves across a day.  Activity
    matters because the matcher can only work where the scene changes -- picking
    blind wastes most samples on static night footage, which the gates then
    (correctly) refuse.

    So: divide the available span into `count` buckets and take the busiest
    thumbnail in each.  A bucket with no activity still contributes its middle
    thumbnail rather than being skipped -- a quiet hour that cannot be measured
    is a fact worth reporting, not one to hide.

    @return paths  Up to `count` thumbnail paths, in time order.
    """
    pattern = os.path.join(videoDir, camLoc.lower(), day or '*',
                           kThumbsSubfolder, '*.jpg')
    stamped = []
    for path in glob.glob(pattern):
        ms = _thumbMsFromName(path)
        if ms is None:
            continue
        # A window matters for verifying a change: after one, the same day's
        # folder holds footage from both sides of it, and sampling across the
        # boundary would average the old behaviour into the new.
        if afterMs is not None and ms < afterMs:
            continue
        if beforeMs is not None and ms > beforeMs:
            continue
        stamped.append((ms, path))
    stamped.sort()
    if len(stamped) <= count:
        return [p for _, p in stamped]

    # Gap to the previous thumbnail: small means the camera was busy.
    gaps = [_kBurstGapMs + 1] + [stamped[i][0] - stamped[i - 1][0]
                                 for i in range(1, len(stamped))]

    if dense:
        # The tightest run of `count` thumbnails is the busiest stretch the
        # camera saw, and the one the matcher can actually measure.
        best = min(range(len(stamped) - count + 1),
                   key=lambda i: stamped[i + count - 1][0] - stamped[i][0])
        return [p for _, p in stamped[best:best + count]]

    firstMs, lastMs = stamped[0][0], stamped[-1][0]
    span = max(1, lastMs - firstMs)
    picked = []
    for b in range(count):
        lo = firstMs + span * b // count
        hi = firstMs + span * (b + 1) // count
        inBucket = [i for i, (ms, _) in enumerate(stamped) if lo <= ms < hi]
        if not inBucket:
            continue
        best = min(inBucket, key=lambda i: gaps[i])
        if gaps[best] > _kBurstGapMs:
            best = inBucket[len(inBucket) // 2]
        picked.append(stamped[best][1])
    return picked


###############################################################################
def _report(camLoc, results, rejects):
    """Print one camera's distribution, and what it implies."""
    if not results:
        reasons = ', '.join('%dx %s' % (n, r) for r, n in sorted(
            rejects.items(), key=lambda kv: -kv[1]))
        print('%-24s NO USABLE SAMPLES  (%s)' % (camLoc, reasons or 'none tried'))
        return None

    vals = sorted(r['skewMs'] / 1000.0 for r in results)
    med = statistics.median(vals)
    # Report the full range, but judge on the p10-p90 span: the verdict should
    # not swing on a single sample that slipped past the gates.
    lo = vals[int(0.1 * (len(vals) - 1))]
    hi = vals[int(round(0.9 * (len(vals) - 1)))]
    spread = hi - lo
    print('%-24s median %+6.2fs   range %+.2f..%+.2f   p10-p90 spread %.2fs'
          '   n=%d%s'
          % (camLoc, med, vals[0], vals[-1], spread, len(vals),
             '   rejected %d' % sum(rejects.values()) if rejects else ''))
    if rejects:
        for reason, n in sorted(rejects.items(), key=lambda kv: -kv[1]):
            print('%-24s     %dx %s' % ('', n, reason))

    # Within a segment vs between segments: is it steady while a segment lasts?
    bySeg = {}
    for r in results:
        bySeg.setdefault(r.get('segment'), []).append(r['skewMs'] / 1000.0)
    inSeg = [max(v) - min(v) for v in bySeg.values() if len(v) > 1]
    if inSeg:
        segMeds = [statistics.median(v) for v in bySeg.values()]
        print('%-24s     within a segment: worst spread %.2fs over %d segment(s); '
              'between segments: %.2fs'
              % ('', max(inSeg), len(inSeg), max(segMeds) - min(segMeds)))

    # Against time of day, which is where load and run age show up.
    byHour = {}
    for r in results:
        byHour.setdefault(
            time.localtime(r['thumbMs'] / 1000.).tm_hour, []).append(
                r['skewMs'] / 1000.0)
    if len(byHour) > 1:
        cells = ['%02dh %+.2f' % (h, statistics.median(v))
                 for h, v in sorted(byHour.items())]
        print('%-24s     by hour: %s' % ('', '  '.join(cells)))
    return {'camLoc': camLoc, 'median': med, 'spread': spread, 'n': len(vals)}


###############################################################################
def main():
    parser = argparse.ArgumentParser(
        description='Measure analysis-vs-recording timestamp skew.')
    parser.add_argument('--cameras', default='',
                        help='comma-separated camera locations (default: all)')
    parser.add_argument('--day', default='',
                        help='archive day folder, e.g. 2026-08-30 (default: all)')
    parser.add_argument('--samples', type=int, default=8,
                        help='thumbnails to try per camera (default 8)')
    parser.add_argument('--window', type=int, default=_kDefaultWindowMs,
                        help='search +/- this many ms (default %d)' % _kDefaultWindowMs)
    parser.add_argument('--after', default='',
                        help='only sample footage after this local time, '
                             'HH:MM or YYYY-MM-DD HH:MM (use to measure one '
                             'side of a change)')
    parser.add_argument('--before', default='',
                        help='only sample footage before this local time')
    parser.add_argument('--dense', action='store_true',
                        help='sample consecutive thumbnails from the busiest '
                             'run, to see whether the skew holds steady within '
                             'one segment (default: spread across the day)')
    parser.add_argument('--dump', default='',
                        help='write side-by-side match images to this directory')
    args = parser.parse_args()

    # A fleet run takes minutes; block-buffered output would show nothing at
    # all until the end when redirected to a file, which is how it is usually
    # run.  Line buffering makes each camera appear as it finishes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    def _clock(text):
        """Local 'HH:MM' or 'YYYY-MM-DD HH:MM' as epoch ms, or None."""
        if not text:
            return None
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%H:%M', '%H:%M:%S'):
            try:
                t = time.strptime(text, fmt)
            except ValueError:
                continue
            if fmt.startswith('%H'):
                today = time.localtime()
                t = time.struct_time((today.tm_year, today.tm_mon, today.tm_mday,
                                      t.tm_hour, t.tm_min, t.tm_sec,
                                      0, 0, -1))
            return int(time.mktime(t) * 1000)
        raise SystemExit('cannot parse time %r' % text)

    afterMs = _clock(args.after)
    beforeMs = _clock(args.before)

    dataDir = getUserDataDir()
    with open(os.path.join(dataDir, kPrefsFile), 'rb') as f:
        prefs = pickle.load(f)
    videoDir = os.path.join(prefs['videoDir'], kVideoFolder)
    clipDb = os.path.join(prefs['dataDir'], kClipDbFile)

    print('data dir : %s' % prefs['dataDir'])
    print('archive  : %s' % videoDir)
    print()

    from backEnd.ClipManager import ClipManager
    clipMgr = ClipManager(getLogger('skew'))
    clipMgr.open(clipDb, readOnly=True)
    try:
        cameras = [c.strip() for c in args.cameras.split(',') if c.strip()] \
            or sorted(clipMgr.getCameraLocations())
        measurer = SkewMeasurer(clipMgr, videoDir, args.window)

        print('skew = (recorded frame that matches) - (analysis timestamp)')
        print('negative means the recording is stamped EARLIER than the analysis'
              ' stream,')
        print('i.e. a detection is stamped later than the frame it describes.')
        print()

        summaries = []
        t0 = time.time()
        samples = 0
        for camLoc in cameras:
            results, rejects = [], {}
            for thumbPath in _pickThumbs(videoDir, camLoc, args.day,
                                         args.samples, args.dense,
                                         afterMs, beforeMs):
                samples += 1
                r = measurer.measure(camLoc, thumbPath)
                if 'reject' in r:
                    rejects[r['reject']] = rejects.get(r['reject'], 0) + 1
                else:
                    results.append(r)
                    if args.dump:
                        measurer.dumpMatch(camLoc, r, args.dump)
            summary = _report(camLoc, results, rejects)
            if summary:
                summaries.append(summary)

        elapsed = time.time() - t0
        print()
        print('%d samples in %.1fs (%.2fs each)'
              % (samples, elapsed, elapsed / max(1, samples)))

        # The question this tool exists to answer.
        print()
        print('VERDICT')
        # Only cameras with enough accepted samples can say anything about
        # spread; one or two points cannot distinguish steady from wandering.
        solid = [s for s in summaries if s['n'] >= _kMinSamplesForVerdict]
        if not solid:
            print('  Not enough accepted samples to judge (need %d per camera).'
                  % _kMinSamplesForVerdict)
            print('  Re-run with more --samples, or over a busier part of the day:')
            print('  the gates reject static scenes, which is most of the night.')
        else:
            worst = max(solid, key=lambda s: s['spread'])
            spreads = [s['spread'] for s in solid]
            meds = [s['median'] for s in solid]
            if len(solid) > 1:
                print('  per-camera medians range %+.2fs .. %+.2fs over %d cameras,'
                      ' so the skew is camera-specific.'
                      % (min(meds), max(meds), len(solid)))
            else:
                print('  only %s had enough samples; run more cameras to compare.'
                      % solid[0]['camLoc'])
            print('  worst within-camera p10-p90 spread is %.2fs (%s, n=%d).'
                  % (worst['spread'], worst['camLoc'], worst['n']))
            if max(spreads) > _kConstantViableSpreadMs / 1000.0:
                print('  A single calibrated constant per camera would be wrong by'
                      ' up to %.2fs,' % (max(spreads) / 2.0))
                print('  so correction by calibration is NOT viable; the fix has to'
                      ' be per-frame at capture.')
            else:
                print('  Spreads stay under %.2fs here.  That is not yet a case FOR'
                      % (_kConstantViableSpreadMs / 1000.0))
                print('  calibration -- check more cameras and a wider span of the'
                      ' day before concluding.')
    finally:
        clipMgr.close()


###############################################################################
if __name__ == '__main__':
    main()
