import math
import time as _time

try:
    import cv2
    import numpy as np
    _kCV2_AVAILABLE = True
except ImportError:
    _kCV2_AVAILABLE = False


# Motion-sensitivity presets.  Each camera stores a "Motion sensitivity" level
# (1=Very Low .. 5=Very High) in camdb (extra['sensitivity']); the UI exposes it
# as a slider.  Higher level = catches more motion (lower thresholds); lower level
# = fewer false "unknown" detections from wind / shadow / vegetation.
#   varThreshold  — MOG2 sensitivity; higher ignores subtle shadow/lighting motion
#   minBBoxArea   — px² at _kThresholdRefSize; drop blobs with a smaller bounding box
#   minMotionArea — px² at _kThresholdRefSize; minimum filled contour area
# Shadow rejection is a SEPARATE per-camera option (extra['ignoreShadows']).
_kSensitivityLevels = {
    5: {'varThreshold': 18, 'minBBoxArea':  600, 'minMotionArea':  400},  # Very High
    4: {'varThreshold': 28, 'minBBoxArea': 1200, 'minMotionArea':  600},  # High
    3: {'varThreshold': 40, 'minBBoxArea': 2000, 'minMotionArea':  800},  # Medium (default)
    2: {'varThreshold': 55, 'minBBoxArea': 3500, 'minMotionArea': 1200},  # Low
    1: {'varThreshold': 70, 'minBBoxArea': 5500, 'minMotionArea': 1800},  # Very Low
}
_kDefaultSensitivity = 3

# The two AREA thresholds above are quoted at this frame size and scaled to
# whatever the camera actually analyses (see VideoPipeline.__init__).  They were
# chosen when cameras analysed a main stream; since the single-stream rebuild
# every camera analyses at 640x360, where the unquoted numbers reject people
# outright.  Measured 2026-08-14 on four 2026-08-13 clips (05_Gate_lr 07:01:52,
# 03_Hill 19:11:21, 09_Jungle 19:04:26, 12_Ravine_lr 19:06:20) in which YOLO
# scores the person 0.5-0.9 on the very analysis frame the gate refused to
# forward: at level 3 unscaled, 0 of ~59 seconds of each person's transit
# reached the detector.
#
# 1280x720 is the reference because, against those four clips plus a people-free
# control (12_Ravine_lr 13:03:10, dappled sun through moving foliage — the
# fleet's worst MOG2 case), level 3 scaled from it beats level 5 unscaled on
# BOTH axes: recall 12/12/11/4 s vs 12/8/9/6 s, background noise 16 s vs 20 s of
# the control's 59.  Raising the slider was the workaround; this is the fix.
_kThresholdRefSize = (1280, 720)

# Minimum centroid travel, in px at _kThresholdRefSize, before a tracked blob is
# reported as an object at all.  Unlike the two AREA thresholds this is a LENGTH,
# so it scales with the linear ratio (sqrt of the area ratio) -- at 640x360 a
# stored 80 becomes 40.  0 disables the gate, and 0 is the default: no camera
# changes behaviour until it is set deliberately.
#
# Measured 2026-08-26 on 09_Jungle, the night that produced 106 tracked objects
# between 00:02 and 06:00, 105 of them stored as "unknown".  Against 713 real
# person/animal objects from 08-21..08-26 on the same camera, the medians are:
#
#                 centroid span   net travel   blob area   duration
#   night false        14 px          6 px       0.57%       2.5 s
#   real              251 px        128 px       2.17%       6.7 s
#
# Span separates them ~18x.  SIZE DOES NOT: the night blobs run ~1200 px^2, the
# same as a real subject at distance, which is why the sensitivity slider barely
# helps -- dropping 09_Jungle from level 3 to level 1 removes only 39% of the
# noise, and does it in daylight too.  At 40 px scaled (80 stored) this gate drops
# 61% of that night's objects while keeping 96% of real EVENTS (157 of 163,
# grouping objects more than 60 s apart as separate events).
#
# 36% of that night was a single near-stationary blob at the left frame edge
# (cx 12.5 +- 3.9, 24x50 px, 3 s, net travel dx 3 dy 14) that no size threshold
# can reach.  It is exactly what this gate is for.
_kDefaultMinTravel = 0


class VideoPipeline(object):
    """Motion-detection pipeline that feeds ObjectDetectorClientImageCheck.

    Replaces the svsentry/AI-analytics pipeline that is not available in this
    environment.  Uses OpenCV background subtraction to find motion blobs, then
    calls the standard ObjectCollector callbacks so QueuedDataManagerCloud can
    route crops to the YOLO-based ImageCheck detector.

    Multiple blobs per frame are tracked independently so that a person and an
    animal in the same scene each get their own objId and their own YOLO pass.
    """

    _kWarmupFrames    = 30     # frames before motion detection starts
    _kMotionCooldown  = 3.0    # s of no-motion before expiring a tracked object
    _kMaxBlobs        = 5      # maximum concurrent motion blobs to track
    _kBlobMatchRadius = 200    # px — blobs within this radius are the same object
    _kPromoteHits     = 3      # frames a blob must persist before it becomes a tracked
                               # object.  Debounces brief wind gusts that would otherwise
                               # be recorded as short-lived "unknown" events.
    _kCandidateTimeout = 0.6   # s — drop an unconfirmed candidate blob not seen recently

    # Global-illumination guard.  A step change in scene lighting -- an IR
    # illuminator kicking in, auto-exposure hunting, garden lights switching,
    # lightning -- makes MOG2 report most of the FRAME as foreground, which the
    # blob path then reads as several huge simultaneous objects.  Measured on
    # 09_Jungle 2026-08-15 03:00-03:30 (IR night, empty patio): ~227 tracked
    # objects in 30 min, essentially all of it lighting, in bursts whose
    # foreground fraction runs 0.41-0.85 of the frame.  A real subject never
    # comes close -- the worst measured across three cameras with people walking
    # through was 0.115 -- so 0.35 sits ~3x clear of genuine motion.
    #
    # Neither shadow rejection nor the sensitivity slider touches this: the
    # blobs are the whole frame, so no size threshold excludes them, and the
    # pixels are brighter (not shadow) on a flash.
    _kIllumFgFraction   = 0.35   # fraction of frame foreground that means "lighting"
    _kIllumSettleFrames = 5      # frames to keep suppressing while MOG2 re-converges
    _kIllumLearningRate = 0.25   # forced rate during settle; the automatic rate is
                                 # ~1/history (0.002), far too slow to absorb a step
    _kIllumMaxRun       = 60     # consecutive suppressed frames before failing OPEN.
                                 # A step change settles in a handful of frames; a view
                                 # that stays saturated (heavy rain, a branch filling the
                                 # lens, a hunting exposure loop) is not one.  A noisy
                                 # camera is recoverable, a silently blind one is not.

    # A track still under minTravel after this many buffered frames is not "on
    # its way somewhere", it is stationary noise.  Stop buffering there rather
    # than growing without bound -- but keep TRACKING it, so it does not fall out
    # of _activeObjects and get re-promoted as a fresh candidate every cooldown,
    # which would put the events straight back.
    _kMaxDeferredFrames = 300

    def __init__(self, videoPath, objectCollector,
                 sensitivity=_kDefaultSensitivity, ignoreShadows=False,
                 logFn=None, frameSize=None, minTravel=_kDefaultMinTravel,
                 timeFn=None, closePx=0):
        # Clock the tracker reads for its cooldown/timeout windows.  Production
        # leaves this None and gets wall time.  Replaying an archive faster than
        # real time must pass a clock driven by the frame timestamps instead:
        # 3 s of WALL clock spans far more than 3 s of VIDEO, so tracks would
        # never expire and every blob would merge into one long-lived object.
        # Nothing errors when that happens -- the counts are just silently wrong.
        self._timeFn         = timeFn if timeFn is not None else _time.time
        self._videoPath      = videoPath
        self.objectCollector = objectCollector
        self._frameId        = 0
        self._lastMs         = -1   # guards against backward timestamps
        self._lastMotionTime = 0.0

        # Motion-cost profiling (see _profileMotion): MOG2 runs per frame on
        # every camera, so it's one of the candidates for the capture-path
        # CPU that cameras dominate.  Cheap: one perf_counter pair per frame.
        self._logFn          = logFn
        self._motionAcc      = 0.0
        self._motionFrames   = 0
        self._motionT0       = _time.time()

        # Global-illumination guard state (see _kIllumFgFraction).
        self._illumSettle    = 0    # frames left to suppress
        self._illumEvents    = 0    # triggers since the last profile line
        self._illumRun       = 0    # consecutive suppressed frames (fail-open counter)

        # Resolve motion tuning from the per-camera sensitivity level (camdb).
        try:
            level = int(sensitivity)
        except (TypeError, ValueError):
            level = _kDefaultSensitivity
        preset = _kSensitivityLevels.get(level,
                                         _kSensitivityLevels[_kDefaultSensitivity])
        # Scale the two AREA thresholds from _kThresholdRefSize to the frame this
        # camera actually analyses.  varThreshold is a per-pixel variance and does
        # NOT scale.  An unknown frame size falls back to the reference, i.e. the
        # values exactly as tabled.
        try:
            frameW, frameH = int(frameSize[0]), int(frameSize[1])
        except (TypeError, ValueError, IndexError):
            frameW = frameH = 0
        refW, refH = _kThresholdRefSize
        if frameW > 0 and frameH > 0:
            areaScale = (frameW * frameH) / float(refW * refH)
        else:
            areaScale = 1.0

        self._sensitivity   = level
        self._frameSize     = (frameW, frameH)
        self._areaScale     = areaScale
        self._varThreshold  = preset['varThreshold']
        self._minBBoxArea   = max(1, int(round(preset['minBBoxArea'] * areaScale)))
        self._minMotionArea = max(1, int(round(preset['minMotionArea'] * areaScale)))
        self._detectShadows = bool(ignoreShadows)

        # minTravel is a LENGTH, so it takes the linear ratio, not areaScale.
        try:
            minTravel = max(0, int(minTravel))
        except (TypeError, ValueError):
            minTravel = _kDefaultMinTravel
        self._minTravel = int(round(minTravel * math.sqrt(areaScale)))
        # Morphological close on the motion mask before contours are cut.  A
        # LENGTH, like minTravel, so it takes the linear ratio.  Without it a
        # person against a busy background splits into several contours: on
        # 075_FirePit_lr 2026-09-23 16:01:40 one walker became four tracks of
        # 20-50 px inside an ~85x250 YOLO person box, each at Jaccard 0.03-0.20
        # against it -- under ObjectDetectorClient._kMinOverlap, so every sample
        # voted 'unknown' while YOLO scored the person 0.66-0.90.  0 disables.
        try:
            closePx = max(0, int(closePx))
        except (TypeError, ValueError):
            closePx = 0
        closeSize = int(round(closePx * math.sqrt(areaScale)))
        if closeSize >= 2 and _kCV2_AVAILABLE:
            closeSize |= 1   # odd, so the kernel has a centre pixel
            self._closeKernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (closeSize, closeSize))
        else:
            closeSize = 0
            self._closeKernel = None
        self._closeSize = closeSize

        # handle -> gate state for tracks held back until they prove they move.
        # Only populated when _minTravel > 0; see _beginObject/_addObjectFrame.
        self._deferred    = {}
        self._nextLocalId = 0

        # Log resolved tuning so the camera log confirms what's in effect.  The
        # scaled values are what the gate actually applies, so print those plus
        # the frame they were scaled to — reading the slider alone is not enough
        # to know why a blob was rejected.
        try:
            log = getattr(objectCollector, '_logger', None)
            if log is not None:
                log.info("[motion] %s sensitivity=%d varThreshold=%d minBBoxArea=%d "
                         "minMotionArea=%d minTravel=%d close=%d ignoreShadows=%s "
                         "frame=%dx%d scale=%.3f (ref %dx%d: %d/%d/%d)"
                         % (videoPath, self._sensitivity, self._varThreshold,
                            self._minBBoxArea, self._minMotionArea,
                            self._minTravel, self._closeSize, self._detectShadows,
                            frameW, frameH, areaScale, refW, refH,
                            preset['minBBoxArea'], preset['minMotionArea'],
                            minTravel))
        except Exception:
            pass

        # List of [cx, cy, objId, last_seen_wall_time] for each active tracked blob
        self._activeObjects  = []
        # List of [cx, cy, hits, last_seen_wall_time] for unconfirmed candidates
        # that have not yet persisted long enough to become tracked objects.
        self._candidates     = []

        if _kCV2_AVAILABLE:
            # Higher varThreshold + longer history = less sensitive to subtle
            # changes (wind, lighting, shadows) while still catching real objects.
            # varThreshold and detectShadows are per-camera (see _kCameraTuning).
            self._bgSub = cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=self._varThreshold,
                detectShadows=self._detectShadows)
        else:
            self._bgSub = None


    # ------------------------------------------------------------------
    def _profileMotion(self, secs, nBlobs):
        """Accumulate motion-detection CPU; log a summary once a minute.

        Also tracks the worst blob count seen: MOG2 itself is fairly steady,
        but a noisy image (IR grain, rain, moving foliage) explodes the
        contour count, and findContours + the per-contour work scales with it
        -- that's what separates a 4 ms camera from a 300 ms one.
        """
        self._motionAcc += secs
        self._motionFrames += 1
        self._motionMaxBlobs = max(getattr(self, '_motionMaxBlobs', 0), nBlobs)
        elapsed = _time.time() - self._motionT0
        if elapsed < 60.0 or not self._motionFrames:
            return
        if self._logFn is not None:
            try:
                self._logFn(
                    'profile: motion %.1f ms/frame cpu over %.1f fps '
                    '(%.0f%% of a core), max blobs %d, illum events %d' %
                    (self._motionAcc * 1000.0 / self._motionFrames,
                     self._motionFrames / elapsed,
                     self._motionAcc * 100.0 / elapsed,
                     self._motionMaxBlobs,
                     self._illumEvents))
            except Exception:
                pass
        self._motionAcc = 0.0
        self._motionFrames = 0
        self._motionMaxBlobs = 0
        self._illumEvents = 0
        self._motionT0 = _time.time()

    # ------------------------------------------------------------------
    def _beginObject(self, ms, cx, cy):
        """Start tracking a newly promoted blob; return the handle to address it by.

        With the travel gate off this is just addObject, and the handle is the
        collector's own object id.  With the gate on nothing is reported yet:
        the handle is a local id and the object only reaches the collector once
        it has actually moved (see _addObjectFrame).
        """
        if self._minTravel <= 0:
            return self.objectCollector.addObject(ms, "unknown")

        self._nextLocalId -= 1          # negative, so it cannot collide with a
        localId = self._nextLocalId     # collector id if the two ever mix
        self._deferred[localId] = {
            'minX': cx, 'maxX': cx, 'minY': cy, 'maxY': cy,
            'firstMs': ms, 'buf': [], 'realId': None, 'stopped': False,
        }
        return localId


    # ------------------------------------------------------------------
    def _addObjectFrame(self, handle, frameId, ms, blob, cx, cy):
        """Record one frame of a tracked object, honouring the travel gate."""
        if self._minTravel <= 0:
            self.objectCollector.addFrame(handle, frameId, ms, blob, "unknown")
            return

        d = self._deferred.get(handle)
        if d is None:
            # Shouldn't happen, but a missing entry must not lose the frame.
            self.objectCollector.addFrame(handle, frameId, ms, blob, "unknown")
            return

        if d['realId'] is not None:
            self.objectCollector.addFrame(d['realId'], frameId, ms, blob, "unknown")
            return

        d['minX'] = min(d['minX'], cx); d['maxX'] = max(d['maxX'], cx)
        d['minY'] = min(d['minY'], cy); d['maxY'] = max(d['maxY'], cy)
        span = (d['maxX'] - d['minX']) + (d['maxY'] - d['minY'])

        if span >= self._minTravel:
            # It moved.  Report it from its FIRST frame, not from here, so the
            # clip covers the whole track, then replay whatever we held back.
            realId = self.objectCollector.addObject(d['firstMs'], "unknown")
            d['realId'] = realId
            for (bFrameId, bMs, bBlob) in d['buf']:
                self.objectCollector.addFrame(realId, bFrameId, bMs, bBlob, "unknown")
            d['buf'] = []
            self.objectCollector.addFrame(realId, frameId, ms, blob, "unknown")
        elif not d['stopped']:
            d['buf'].append((frameId, ms, blob))
            if len(d['buf']) >= self._kMaxDeferredFrames:
                # Stationary.  Release the buffer but keep the track alive, so it
                # is not re-promoted as a fresh candidate on the next cooldown.
                # If it ever does move it still reports, just without the history.
                d['stopped'] = True
                d['buf'] = []


    # ------------------------------------------------------------------
    def _pruneDeferred(self):
        """Drop gate state for tracks the tracker has expired."""
        if not self._deferred:
            return
        live = set(o[2] for o in self._activeObjects)
        for handle in [h for h in self._deferred if h not in live]:
            del self._deferred[handle]


    # ------------------------------------------------------------------
    def updateVideoPath(self, videoPath):
        self._videoPath = videoPath
        if self.objectCollector is not None:
            try:
                self.objectCollector.cameraLocation = videoPath
            except Exception:
                pass

    # ------------------------------------------------------------------
    def processClipFrame(self, frame, ms):
        self._frameId += 1
        blobs = []
        illumSuppress = False   # set by the global-illumination guard below
        # CPU time, not wall: this runs on the camera's main thread and we
        # want its burn, not any time lost to scheduling.
        _motionT0 = _time.thread_time()

        if self._bgSub is not None:
            try:
                if hasattr(frame, '_data'):
                    img_np = frame._data
                else:
                    from vitaToolbox.image.ImageConversion import convertProcFrameToPIL
                    img_np = np.array(convertProcFrameToPIL(frame))

                gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)

                # Global-illumination guard.  While settling, force a high
                # learning rate so the model absorbs the new light level instead
                # of reporting the whole frame as foreground for minutes.
                if self._illumSettle > 0:
                    fgmask = self._bgSub.apply(gray, None, self._kIllumLearningRate)
                    self._illumSettle -= 1
                    illumSuppress = True
                else:
                    fgmask = self._bgSub.apply(gray)
                    # Measure on the RAW mask, before shadow rejection: MOG2 labels
                    # a lighting DROP as shadow (127), so thresholding first would
                    # hide half the events this guard exists to catch.
                    if self._frameId > self._kWarmupFrames:
                        fgFrac = cv2.countNonZero(fgmask) / float(fgmask.size)
                        if fgFrac >= self._kIllumFgFraction:
                            self._illumSettle = self._kIllumSettleFrames
                            self._illumEvents += 1
                            illumSuppress = True

                if illumSuppress:
                    self._illumRun += 1
                    if self._illumRun > self._kIllumMaxRun:
                        # Fail OPEN -- see _kIllumMaxRun.  Let the blobs through
                        # and let the size thresholds deal with them.
                        illumSuppress = False
                        self._illumSettle = 0
                        if self._logFn is not None and \
                                self._illumRun == self._kIllumMaxRun + 1:
                            try:
                                self._logFn(
                                    '[motion] illumination guard held for %d frames '
                                    '- failing open so the camera is not blind'
                                    % self._kIllumMaxRun)
                            except Exception:
                                pass
                else:
                    self._illumRun = 0

                if self._detectShadows:
                    # MOG2 tags shadows as 127; keep only true foreground (255).
                    fgmask = cv2.threshold(fgmask, 200, 255, cv2.THRESH_BINARY)[1]

                if not illumSuppress and self._frameId > self._kWarmupFrames:
                    if self._closeKernel is not None:
                        fgmask = cv2.morphologyEx(
                            fgmask, cv2.MORPH_CLOSE, self._closeKernel)
                    contours, _ = cv2.findContours(
                        fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    for c in contours:
                        if cv2.contourArea(c) < self._minMotionArea:
                            continue
                        x, y, w, h = cv2.boundingRect(c)
                        # Suppress very small objects (per-camera bounding-box area).
                        if w * h < self._minBBoxArea:
                            continue
                        blobs.append((x, y, x + w, y + h))

                    # Largest blobs first, cap at _kMaxBlobs
                    blobs.sort(key=lambda b: (b[2]-b[0])*(b[3]-b[1]), reverse=True)
                    blobs = blobs[:self._kMaxBlobs]

            except Exception:
                pass

        self._profileMotion(_time.thread_time() - _motionT0, len(blobs))

        # The tracker's clock -- see _timeFn.  NOT the profiler's clock above,
        # which measures real CPU burn per wall second and must stay wall time.
        now = self._timeFn()

        # Guard against backward timestamps
        if ms < self._lastMs:
            return
        self._lastMs = ms

        # Register this frame so _requestDetections can retrieve it for YOLO.
        self.objectCollector.reportFrame(ms, frame)

        if blobs:
            self._lastMotionTime = now
            new_active     = []
            new_candidates = []
            used_active    = set()
            used_cand      = set()

            for blob in blobs:
                cx = (blob[0] + blob[2]) // 2
                cy = (blob[1] + blob[3]) // 2

                # 1) Match to nearest unmatched already-promoted object
                best_idx  = None
                best_dist = float('inf')
                for i, (ox, oy, oid, ts) in enumerate(self._activeObjects):
                    if i in used_active:
                        continue
                    d = math.sqrt((cx - ox)**2 + (cy - oy)**2)
                    if d < self._kBlobMatchRadius and d < best_dist:
                        best_dist = d
                        best_idx  = i

                if best_idx is not None:
                    used_active.add(best_idx)
                    oid = self._activeObjects[best_idx][2]
                    new_active.append((cx, cy, oid, now))
                    self._addObjectFrame(oid, self._frameId, ms, blob, cx, cy)
                    continue

                # 2) Match to nearest unmatched candidate (unconfirmed) blob
                best_idx  = None
                best_dist = float('inf')
                for i, (ox, oy, hits, ts) in enumerate(self._candidates):
                    if i in used_cand:
                        continue
                    d = math.sqrt((cx - ox)**2 + (cy - oy)**2)
                    if d < self._kBlobMatchRadius and d < best_dist:
                        best_dist = d
                        best_idx  = i

                if best_idx is not None:
                    used_cand.add(best_idx)
                    hits = self._candidates[best_idx][2] + 1
                    if hits >= self._kPromoteHits:
                        # Persisted long enough — promote to a tracked object
                        oid = self._beginObject(ms, cx, cy)
                        new_active.append((cx, cy, oid, now))
                        self._addObjectFrame(oid, self._frameId, ms, blob, cx, cy)
                    else:
                        new_candidates.append((cx, cy, hits, now))
                    continue

                # 3) Brand-new candidate — not tracked until it persists
                new_candidates.append((cx, cy, 1, now))

            # Carry forward unmatched promoted objects still within cooldown
            for i, (ox, oy, oid, ts) in enumerate(self._activeObjects):
                if i not in used_active and now - ts < self._kMotionCooldown:
                    new_active.append((ox, oy, oid, ts))

            # Carry forward unmatched candidates still within their short window
            for i, (ox, oy, hits, ts) in enumerate(self._candidates):
                if i not in used_cand and now - ts < self._kCandidateTimeout:
                    new_candidates.append((ox, oy, hits, ts))

            self._activeObjects = new_active
            self._candidates    = new_candidates
            self._pruneDeferred()

        elif not illumSuppress:
            # No motion — expire objects past cooldown, drop all candidates
            if now - self._lastMotionTime >= self._kMotionCooldown:
                self._activeObjects = []
                self._pruneDeferred()
            self._candidates = []
        else:
            # Illumination transient: we have NO observation of this frame, which
            # is not the same as having observed that nothing moved.  Hold the
            # tracker's state rather than ageing it out, so a subject present
            # across the transient stays ONE object instead of being dropped and
            # re-promoted as a second.  _lastMotionTime is left alone too, so the
            # cooldown resumes from the last frame we could actually see.
            pass

        # Always advance the Sentry clock so tracked objects eventually get reported.
        self.objectCollector.frameCompleted(ms, self._frameId)

    # ------------------------------------------------------------------
    def flush(self):
        pass
