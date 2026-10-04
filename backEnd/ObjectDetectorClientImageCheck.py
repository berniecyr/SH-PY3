"""
ObjectDetectorClientImageCheck.py

An ObjectDetectorClient subclass that uses the ImageCheck detection stack
(YOLO + InsightFace/ArcFace + NudeNet) instead of Sighthound's proprietary
cloud/local analytics service.

Drop-in replacement: plug this in wherever ObjectDetectorClientLocal or
ObjectDetectorClientInProcess is currently selected.

Detection pipeline per crop:
  1. YOLO — finds person bounding boxes within the motion-blob crop
  2. InsightFace/ArcFace — recognises each person against the known-faces library
  3. NudeNet — flags explicit content (optional, controlled by config)

Returned object types fed into TargetTrigger:
  "person"          — YOLO person hit, identity unknown or face detection off
  "person:<Name>"   — ArcFace recognised a specific person above threshold
  "vehicle"         — YOLO motorcycle/car/truck/bicycle/bus/boat/train hit
  "animal"          — YOLO animal (COCO animal classes) hit
  "nudity"          — NudeNet flagged explicit content
  "unknown"         — no recognisable object in the motion blob

Config is read from the IMAGECHECK_CONFIG env-var (path to a JSON file) or
from the defaults below.  This keeps the integration self-contained and
doesn't require changes to SighthoundVideo's settings UI to get started.

NOTE: inference runs in the shared DetectionService process, NOT here.  This
module must stay light — it is imported by every camera process, and loading
torch/ultralytics/insightface per camera is exactly the memory blow-up the
service architecture exists to prevent.  Model calls go through
DetectionServiceClient; everything else (config, gating, overlap logic,
attribute encoding, known-face matching) stays local.
"""

import logging
import os
import pickle
import time

import numpy as np

from backEnd.ObjectDetectorClient import (
    ObjectDetectorClient, _kMinOverlap, _kMinContainment,
)
from backEnd.ImageCheckConfig import (
    DEFAULTS as _DEFAULTS, getConfigPath, loadConfig, enabledNudeThresholds,
)
from backEnd.DetectionServiceClient import DetectionServiceClient
from vitaToolbox.image.ImageConversion import convertProcFrameToPIL
from vitaToolbox.math.Rect import Rect as VitaRect


# COCO animal classes that YOLO can detect — mapped to type "animal"
_kAnimalClasses = {
    'bird', 'cat', 'dog', 'horse', 'cow',
}

# COCO vehicle classes that YOLO can detect — mapped to type "vehicle"
_kVehicleClasses = {
    'car', 'motorcycle', 'truck',
}

# Max allowed mismatch between the ANALYZED frame's timestamp and a ring
# snapshot's mtime.  Detection can run many seconds behind capture (queue
# depth), so snapshots are matched by time, not "latest"; beyond this skew
# the person has likely moved out of the scaled bounding box.
_kSnapshotMatchToleranceMs = 4000

# Hard ceiling on the full-res crop, as a multiple of the person's expected
# size in snapshot pixels.  The full-res path exists to give face/nudity MORE
# pixels ON THE PERSON; a crop that grows to most of the scene gives them a
# high-resolution picture of background instead, and both models then return
# nothing.  Measured failure it prevents (08_FrontStep 2026-08-03 08:56:47):
# crops of 1428x1402 / 1571x2160 from a 3840x2160 snapshot for a person about
# 130x480 -- no face found, and nudity=0 on a genuinely nude subject.
_kMaxFullresCropFactor = 3.0

# A motion fragment may take a PERSON label from a box it sits inside, when the
# person is confidently detected and the fragment is a real piece of it.  The
# general rule stays one-way (detection inside blob; see the matching loop):
# the reverse was removed because IR-noise specks inside a PARKED car's box
# inherited 'vehicle'.  People split into fragments instead -- on 075_FirePit_lr
# 2026-09-23 16:01:40 a walker scored 0.66-0.90 while every motion blob on him
# was a 20-50 px piece at Jaccard 0.03-0.20 and 100% inside his box, so all of
# them voted 'unknown'.  Person-only, with a score floor and an area floor so a
# speck cannot ride on a marginal detection.
_kFragmentPersonMinScore = 0.6      # YOLO person confidence required
_kFragmentMinInside = 0.9           # share of the blob inside the person box
_kFragmentMinAreaFrac = 0.05        # blob area / person box area

# ---------------------------------------------------------------------------
# Known-faces library — loaded locally (pure pickle+numpy); matching against
# service-returned embeddings happens client-side, same as it always did.
# ---------------------------------------------------------------------------
_known_encodings: list = []
_known_names:    list = []


def _load_known_faces(cfg: dict, logger: logging.Logger) -> None:
    """Read the ArcFace known-faces cache produced by ImageCheck."""
    global _known_encodings, _known_names
    dat = cfg.get("KNOWN_FACES_DAT", _DEFAULTS["KNOWN_FACES_DAT"])
    if not os.path.isfile(dat):
        logger.warning(f"[ImageCheck] known_faces.dat not found at {dat!r}; face recognition disabled")
        return
    with open(dat, "rb") as f:
        data = pickle.load(f)
    _known_encodings = data.get("encodings", [])
    _known_names     = data.get("names", [])
    logger.info(f"[ImageCheck] Loaded {len(_known_names)} known identities from {dat!r}")


# ---------------------------------------------------------------------------
# Client subclass
# ---------------------------------------------------------------------------

class ObjectDetectorClientImageCheck(ObjectDetectorClient):
    """ObjectDetectorClient backed by the ImageCheck detection stack."""

    def __init__(self, id: str, logger: logging.Logger) -> None:
        super().__init__(id, logger)

        # Load config via the shared loader: env-var override (IMAGECHECK_CONFIG)
        # takes precedence, otherwise the fixed path the Options dialog writes to.
        # loadConfig() merges over DEFAULTS (incl. per-class NUDE_THRESHOLDS) and
        # never raises.
        self._cfg = loadConfig()
        self._logger.info("[ImageCheck] Config loaded from %s" % getConfigPath())
        self._rpc = None   # created in run() (detector-thread context)

        # Ring of full-resolution keyframe snapshots the recorder keeps for
        # this camera (id == camera location).  Face/nudity pick the snapshot
        # matching the analyzed frame's TIMESTAMP, so they see main-stream
        # pixels from the right moment even when detection lags capture.
        self._snapshotDir = os.path.join(
            os.path.dirname(getConfigPath()), 'live', "%s.snaps" % id)
        self._lastSnapDiffMs = None    # skew of the last matched snapshot
        self._knownFacesMtime = None   # for hot-reloading new enrollments
        self._nextKnownFacesCheck = 0.0
        self._detFailures = 0          # consecutive detection-service failures

    def _noteDetectionFailure(self, what, exc):
        """Log a detection-service failure without flooding the log.

        Every back-end restart takes the service down while this camera is
        still running, so failures arrive one per analyzed frame for several
        seconds -- previously hundreds of identical WARNING lines that looked
        like a fault rather than a restart.  The first failure and the
        recovery stay at WARNING so a genuine outage is still obvious; the
        repeats in between drop to DEBUG.
        """
        self._detFailures += 1
        if self._detFailures == 1:
            self._logger.warning(
                "[ImageCheck] detection service error (%s): %s" % (what, exc))
        else:
            self._logger.debug(
                "[ImageCheck] detection service error (%s), failure #%d: %s"
                % (what, self._detFailures, exc))

    def _noteDetectionOk(self):
        """Clear the failure streak, reporting how long it ran."""
        if self._detFailures:
            self._logger.warning(
                "[ImageCheck] detection service recovered after %d failed"
                " request(s)" % self._detFailures)
            self._detFailures = 0

    def run(self) -> None:
        """Connect to the shared detection service, then run the work loop."""
        self._rpc = DetectionServiceClient(self._logger)
        if self._cfg.get("RUN_FACE", _DEFAULTS["RUN_FACE"]):
            try:
                _load_known_faces(self._cfg, self._logger)
                self._knownFacesMtime = self._knownFacesStat()
            except Exception:
                self._logger.error("[ImageCheck] known-faces load failed",
                                   exc_info=True)
        super().run()

    def _knownFacesStat(self):
        try:
            dat = self._cfg.get("KNOWN_FACES_DAT",
                                _DEFAULTS["KNOWN_FACES_DAT"])
            return os.path.getmtime(dat)
        except OSError:
            return None

    def _maybeReloadKnownFaces(self):
        """Hot-reload known_faces.dat when it changes (new enrollments from
        the record viewer or the Options re-enroll button) so recognition
        picks up new faces WITHOUT a camera restart.  Time-gated stat; the
        writer replaces the file atomically."""
        now = time.time()
        if now < self._nextKnownFacesCheck:
            return
        self._nextKnownFacesCheck = now + 5.0
        mtime = self._knownFacesStat()
        if mtime is None or mtime == self._knownFacesMtime:
            return
        try:
            _load_known_faces(self._cfg, self._logger)
            self._knownFacesMtime = mtime
            self._logger.info("[ImageCheck] known faces reloaded (updated)")
        except Exception:
            self._logger.error("[ImageCheck] known-faces reload failed",
                               exc_info=True)

    # ------------------------------------------------------------------
    # ObjectDetectorClient abstract methods
    # ------------------------------------------------------------------

    def _doRefreshHTTPConnection(self) -> None:
        """No HTTP connection needed — models run in-process."""
        self._httpConn = True   # truthy so _mustRefreshHTTPConnection stays False

    def _mustRefreshHTTPConnection(self) -> bool:
        return self._httpConn is None

    def _callDetectionAPI(self, params) -> list:
        raise NotImplementedError("ImageCheck client does not use HTTP")

    def _processWorkItem(self, timestamp, fullSizeFrame, sentryBoxes, sizeRatio):
        """Override: run YOLO once on the full frame for accurate vehicle/animal detection.

        The base class crops 300×300 around each MOG2 blob before running YOLO.
        For small or edge-of-frame objects (e.g. a motorcycle at the corner), the
        300×300 crop strips the spatial context YOLO needs and confidence collapses
        below threshold even when the full-frame confidence is well above it.

        Running on the full frame is free here (models are in-process) and gives
        YOLO the context it was trained on.
        """
        image = convertProcFrameToPIL(fullSizeFrame)
        frame_rgb = np.array(image)
        scaledUpBoxes = self._scaleUpSentryBoxes(sentryBoxes, sizeRatio)

        if self._cfg.get("RUN_FACE", _DEFAULTS["RUN_FACE"]):
            self._maybeReloadKnownFaces()

        try:
            yolo_all = self._rpc.yolo(
                frame_rgb,
                self._cfg.get("YOLO_CONF_THRESHOLD", _DEFAULTS["YOLO_CONF_THRESHOLD"]))
        except Exception as e:
            self._noteDetectionFailure("full frame", e)
            self._resultsQueue.put((timestamp, []))
            return
        self._noteDetectionOk()

        # Collect person/vehicle/animal hits from the full frame
        yolo_dets = [
            (label, score, x1, y1, x2, y2)
            for (label, score, x1, y1, x2, y2) in yolo_all
            if label == 'person' or label in _kVehicleClasses
            or label in _kAnimalClasses
        ]

        self._logger.debug(
            "[ImageCheck] full-frame YOLO ts=%d dets=%s"
            % (timestamp, str([(d[0], round(d[1], 3)) for d in yolo_dets])))

        output = []
        for obj, frame_id, sentry_rect in scaledUpBoxes:
            # sentry_rect is a VitaRect(x, y, w, h) in full-frame coords.
            # Find the highest-scoring YOLO detection that overlaps this blob.
            best_type = None
            best_label = None
            best_score = 0.0
            best_person_box = None

            for label, score, dx1, dy1, dx2, dy2 in yolo_dets:
                det_rect = VitaRect(dx1, dy1, dx2 - dx1, dy2 - dy1)
                overlap = sentry_rect.overlap(det_rect, 'jaccard')
                # Containment is deliberately ONE-WAY: the detection inside
                # the blob, i.e. a subject within a larger motion region.
                #
                # The reverse (blob inside detection) was added 2026-08-14 to
                # rescue a partial motion mask, and REMOVED 2026-08-19 because it
                # cannot work: when a blob lies entirely inside a detection,
                # intersection = blob area and union = det area, so
                # jaccard == blob/det area EXACTLY.  The reverse term is
                # therefore only ever decisive when jaccard is already below
                # _kMinOverlap -- precisely the cases the overlap floor exists to
                # reject.  Measured on real footage: it rescued 0 of 196
                # person/animal matches across 12 clips, while producing 5 of the
                # 6 false `vehicle` matches on 8 clips of a PARKED car, where a
                # speck of IR noise inside the car's box scores blob-in-det=1.000
                # at jaccard 0.016-0.181 and inherits the car's label.
                # The 09_Jungle person it was meant to save was in fact rescued
                # by the type-vote change (_kMinRealTypeScore), not by this.
                if (overlap < _kMinOverlap
                        and det_rect.containment(sentry_rect) < _kMinContainment
                        and not self._isPersonFragment(
                            label, score, sentry_rect, det_rect)):
                    continue

                if label == 'person':
                    det_type = 'person'
                elif label in _kVehicleClasses:
                    det_type = 'vehicle'
                else:
                    det_type = 'animal'

                if score > best_score:
                    best_score = score
                    best_type = det_type
                    best_label = label   # specific YOLO class (dog/car/...)
                    if det_type == 'person':
                        best_person_box = (dx1, dy1, dx2, dy2)

            if best_type is None:
                # No positive YOLO hit; notifyDetectionCompleted adds the "unknown"
                # fallback automatically — do not append anything here.
                continue

            if best_type == 'person' and best_person_box is not None:
                dx1, dy1, dx2, dy2 = best_person_box
                person_crop = frame_rgb[max(0, dy1):dy2, max(0, dx1):dx2]

                # Precision/perf gate: only run the expensive face + nudity stage
                # when the person detection is confident enough.  A weak,
                # low-confidence "person" (shadow, animal misread as a person at
                # the 0.25 detection floor) must NOT spawn face/nudity attributes
                # — that is what produced phantom "Bernie"/"nudity" tags on empty
                # frames.  The person detection itself is still emitted below so
                # the categorical vote is unaffected; only the attributes gate.
                person_conf_floor = self._cfg.get(
                    "PERSON_CONF_FOR_ATTRS", _DEFAULTS["PERSON_CONF_FOR_ATTRS"])
                do_attrs = best_score >= person_conf_floor

                # Prefer main-stream pixels for the expensive attribute
                # models: the analysis stream is small (faces beyond a few
                # meters drop under MIN_FACE_SIZE there), the snapshot is
                # full camera resolution.
                attr_crop = person_crop
                if do_attrs:
                    fullres = self._fullres_person_crop(
                        best_person_box,
                        frame_rgb.shape[1], frame_rgb.shape[0],
                        timestamp)
                    if fullres is not None:
                        attr_crop = fullres

                enc_type = best_type
                if (do_attrs
                        and self._cfg.get("RUN_FACE", _DEFAULTS["RUN_FACE"])
                        and attr_crop.size > 0):
                    # SCRFD's det score is resolution-dependent: the tiny
                    # analysis crop is UPSCALED to the 640 det size (big blurry
                    # face -> det 0.7-0.8) while the 2K snapshot crop is
                    # DOWNSCALED (smaller, sharper face -> det 0.45-0.6), so a
                    # uniform floor silently vetoes the full-res path exactly
                    # when its higher-detail embedding matters most.  The
                    # full-res crop is already person-verified (YOLO re-detect
                    # inside the snapshot) and the recognition threshold
                    # rejects junk (impostors score ~0.0-0.2), so relax the
                    # detection floor there.
                    if attr_crop is not person_crop:
                        cfg_floor = self._cfg.get(
                            "FACE_DET_CONF", _DEFAULTS["FACE_DET_CONF"])
                        info = self._analyse_face(
                            attr_crop, det_floor=max(0.40, cfg_floor - 0.15))
                    else:
                        info = self._analyse_face(attr_crop)
                    # The snapshot's content can be up to a GOP (~seconds)
                    # older than the analysis frame, so a moving person may
                    # not be inside the scaled crop at all.  If the full-res
                    # attempt found no face, retry on the analysis-stream
                    # crop — otherwise close-range faces that the analysis
                    # crop WOULD catch are lost to a stale snapshot.
                    if (info.get("faceDet") is None
                            and attr_crop is not person_crop
                            and person_crop.size > 0):
                        fullres_raw = info.get("rawDet")
                        info = self._analyse_face(person_crop)
                        self._logger.info(
                            "[ImageCheck] face: fullres crop %dx%d no face "
                            "(raw det=%s, snap skew %sms); "
                            "analysis crop %dx%d -> det=%s name=%r" % (
                                attr_crop.shape[1], attr_crop.shape[0],
                                "none" if fullres_raw is None
                                else "%.2f" % fullres_raw,
                                self._lastSnapDiffMs,
                                person_crop.shape[1], person_crop.shape[0],
                                info.get("faceDet"), info.get("name")))
                    else:
                        self._logger.info(
                            "[ImageCheck] face: %s crop %dx%d -> det=%s "
                            "name=%r" % (
                                "fullres" if attr_crop is not person_crop
                                else "analysis",
                                attr_crop.shape[1], attr_crop.shape[0],
                                info.get("faceDet"), info.get("name")))
                    enc_type = self._encode_face_attrs(best_type, info, best_score)
                else:
                    enc_type = self._encode_attrs(
                        best_type, ["dc=%.2f" % best_score])

                nudity_type = None
                if (do_attrs
                        and self._cfg.get("RUN_NUDITY", _DEFAULTS["RUN_NUDITY"])
                        and attr_crop.shape[0] >= 100
                        and attr_crop.shape[1] >= 100):
                    nudity_type = self._check_nudity(attr_crop)
                    # Same reasoning as the face retry above: a full-res crop
                    # taken from a snapshot up to seconds old can miss the
                    # person, and NudeNet then reports nothing on a subject the
                    # tighter analysis crop would have caught.  Face already
                    # retried; nudity silently did not, which is how a nude
                    # subject logged nudity=0 on 08_FrontStep 2026-08-03 08:56.
                    if (not nudity_type
                            and attr_crop is not person_crop
                            and person_crop.shape[0] >= 100
                            and person_crop.shape[1] >= 100):
                        nudity_type = self._check_nudity(person_crop)
                        if nudity_type:
                            self._logger.info(
                                "[ImageCheck] nudity: fullres crop %dx%d found "
                                "nothing; analysis crop %dx%d did"
                                % (attr_crop.shape[1], attr_crop.shape[0],
                                   person_crop.shape[1], person_crop.shape[0]))

                if not do_attrs:
                    self._logger.debug(
                        "[ImageCheck] person conf %.2f < floor %.2f; "
                        "skipping face/nudity" % (best_score, person_conf_floor))

                output.append((obj, enc_type, best_score, 1.0))
                if nudity_type:
                    output.append((obj, nudity_type, best_score, 1.0))
            else:
                # Vehicle / animal: keep the specific YOLO class as subtype.
                enc_type = self._encode_attrs(
                    best_type,
                    ["st=%s" % best_label if best_label else None,
                     "dc=%.2f" % best_score])
                output.append((obj, enc_type, best_score, 1.0))

        self._resultsQueue.put((timestamp, output))

    @staticmethod
    def _isPersonFragment(label, score, sentry_rect, det_rect) -> bool:
        """Is this blob a piece of a confidently detected person?

        See _kFragmentPersonMinScore.

        @param  label        YOLO class name.
        @param  score        YOLO confidence.
        @param  sentry_rect  Motion blob (VitaRect, full-frame coords).
        @param  det_rect     YOLO box (VitaRect, full-frame coords).
        @return isFragment   True if the blob may take the person label.
        """
        if label != 'person' or score < _kFragmentPersonMinScore:
            return False
        detArea = det_rect.area()
        if detArea <= 0 or sentry_rect.area() <= 0:
            return False
        if sentry_rect.area() / float(detArea) < _kFragmentMinAreaFrac:
            return False
        return sentry_rect.containment(det_rect) >= _kFragmentMinInside

    def _processSingleImage(self, image) -> list:
        """
        Run the ImageCheck stack on a PIL image crop.

        @param  image    PIL.Image (RGB), a crop around one motion blob
        @return objects  list of (objectType:str, rect:VitaRect, score:float)
                         matching the contract expected by _processJSONResult
        """
        # PIL → numpy BGR (OpenCV convention expected by InsightFace/NudeNet)
        frame_rgb = np.array(image)
        if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            return []

        output = []

        # ---- 1. YOLO person detection ------------------------------------
        try:
            yolo_all = self._rpc.yolo(frame_rgb,
                                      self._cfg.get("YOLO_CONF_THRESHOLD",
                                                    _DEFAULTS["YOLO_CONF_THRESHOLD"]))
        except Exception as e:
            self._noteDetectionFailure("person pass", e)
            return []
        self._noteDetectionOk()

        person_boxes  = []
        animal_boxes  = []
        vehicle_boxes = []
        for label, score, x1, y1, x2, y2 in yolo_all:
            if label == "person":
                person_boxes.append((x1, y1, x2, y2, score))
            elif label in _kAnimalClasses:
                animal_boxes.append((x1, y1, x2, y2, score, label))
            elif label in _kVehicleClasses:
                vehicle_boxes.append((x1, y1, x2, y2, score, label))

        self._logger.debug(
            f"[ImageCheck] YOLO crop=({image.size[0]}, {image.size[1]}) "
            f"persons={len(person_boxes)} animals={len(animal_boxes)} "
            f"vehicles={len(vehicle_boxes)}"
        )

        if not person_boxes and not animal_boxes and not vehicle_boxes:
            return [("unknown", VitaRect(0, 0, image.size[0], image.size[1]), 0.0)]

        # Use the full-crop rect for all detections so the Jaccard overlap check
        # in _processJSONResult always passes.  The stored bounding box in the DB
        # comes from the MOG2 sentry rect, not from the YOLO box, so we lose nothing.
        full_crop = VitaRect(0, 0, image.size[0], image.size[1])

        # ---- 2. Animal detections ----------------------------------------
        for x1, y1, x2, y2, score, label in animal_boxes:
            enc = self._encode_attrs("animal",
                                     ["st=%s" % label, "dc=%.2f" % score])
            output.append((enc, full_crop, score))

        # ---- 3. Vehicle detections ----------------------------------------
        for x1, y1, x2, y2, score, label in vehicle_boxes:
            enc = self._encode_attrs("vehicle",
                                     ["st=%s" % label, "dc=%.2f" % score])
            output.append((enc, full_crop, score))

        # ---- 3. Per-person: face recognition + nudity --------------------
        person_conf_floor = self._cfg.get(
            "PERSON_CONF_FOR_ATTRS", _DEFAULTS["PERSON_CONF_FOR_ATTRS"])
        for x1, y1, x2, y2, yolo_score in person_boxes:
            person_crop = frame_rgb[max(0, y1):y2, max(0, x1):x2]
            box_rect    = full_crop
            obj_type    = "person"

            # Only run face + nudity when the person detection is confident
            # enough (see _processWorkItem for the rationale).
            do_attrs = yolo_score >= person_conf_floor

            # Face recognition + demographics
            if (do_attrs
                    and self._cfg.get("RUN_FACE", _DEFAULTS["RUN_FACE"])
                    and person_crop.size > 0):
                info = self._analyse_face(person_crop)
                obj_type = self._encode_face_attrs(obj_type, info, yolo_score)
            else:
                obj_type = self._encode_attrs(obj_type,
                                              ["dc=%.2f" % yolo_score])

            # Nudity check
            nudity_type = None
            if (do_attrs
                    and self._cfg.get("RUN_NUDITY", _DEFAULTS["RUN_NUDITY"])
                    and person_crop.shape[0] >= 100 and person_crop.shape[1] >= 100):
                nudity_type = self._check_nudity(person_crop)

            output.append((obj_type, box_rect, yolo_score))
            if nudity_type:
                output.append((nudity_type, box_rect, yolo_score))

        return output

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fullres_person_crop(self, person_box, frame_w: int, frame_h: int,
                             frame_ts_ms):
        """Crop the person region from the time-matched full-res snapshot.

        @param  person_box   (x1, y1, x2, y2) in ANALYSIS-frame coordinates.
        @param  frame_w/h    Analysis frame dimensions (coordinate scaling).
        @param  frame_ts_ms  The analyzed frame's capture timestamp (ms).
                             Detection may run several seconds behind capture,
                             so the snapshot is chosen by TIME MATCH against
                             this — using "the latest snapshot" cropped at the
                             frame's coordinates systematically misses moving
                             people.
        @return RGB ndarray crop from the matched snapshot, or None when
                disabled/no time-matched snapshot/unreadable/no resolution
                gain — callers then fall back to the analysis-stream crop.
        """
        if not self._cfg.get("FULLRES_ATTRS", _DEFAULTS["FULLRES_ATTRS"]):
            return None
        why = "?"
        self._lastSnapDiffMs = None    # diagnostic: matched snapshot's skew
        try:
            import cv2
            best, best_diff = None, None
            try:
                for f in os.listdir(self._snapshotDir):
                    p = os.path.join(self._snapshotDir, f)
                    try:
                        diff = abs(os.path.getmtime(p) * 1000.0 - frame_ts_ms)
                    except OSError:
                        continue
                    if best_diff is None or diff < best_diff:
                        best, best_diff = p, diff
            except OSError:
                self._logger.info("[ImageCheck] fullres skip: no ring dir")
                return None
            if best is None or best_diff > _kSnapshotMatchToleranceMs:
                self._logger.info(
                    "[ImageCheck] fullres skip: no time match (nearest %s ms)"
                    % ("none" if best_diff is None else int(best_diff)))
                return None
            self._lastSnapDiffMs = int(best_diff)
            why = "unreadable snapshot"
            img = cv2.imread(best)   # None on partial/corrupt write
            if img is None or img.ndim != 3:
                self._logger.info("[ImageCheck] fullres skip: %s" % why)
                return None
            sh, sw = img.shape[:2]
            if sw <= frame_w or sh <= frame_h:
                self._logger.info(
                    "[ImageCheck] fullres skip: snapshot %dx%d <= frame %dx%d"
                    % (sw, sh, frame_w, frame_h))
                return None
            snap_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

            # RE-DETECT the person inside the snapshot instead of trusting
            # geometry: the person may have walked between the snapshot's
            # moment and the analyzed frame, and widening the crop margin to
            # cover that motion shrinks the face below detectability — which
            # forfeits the whole point of the full-res path.  One extra YOLO
            # (~20ms on the service GPU) finds where they actually ARE; the
            # crop is then tight around the real person, preserving the zoom.
            why = "service yolo failed"
            dets = self._rpc.yolo(
                snap_rgb, self._cfg.get("YOLO_CONF_THRESHOLD",
                                        _DEFAULTS["YOLO_CONF_THRESHOLD"]))
            x1, y1, x2, y2 = person_box
            sx = sw / float(frame_w)
            sy = sh / float(frame_h)
            want_cx = (x1 + x2) / 2.0 * sx
            want_cy = (y1 + y2) / 2.0 * sy
            # Expected size of this person in snapshot pixels.  Nearest-centre
            # alone is not enough: a spurious or merged box gets picked, the
            # crop balloons to most of the scene, and BOTH attribute models
            # then look mostly at background.  Measured 2026-08-03 08:56 on
            # 08_FrontStep: crops of 1428x1402 and 1571x2160 out of a 3840x2160
            # snapshot for a person only ~130x480 -- face found nothing and
            # nudity returned nothing on a genuinely nude subject.  So a
            # candidate must also be a plausible SIZE for what we tracked.
            want_w = max(1.0, (x2 - x1) * sx)
            want_h = max(1.0, (y2 - y1) * sy)
            best_box, best_d = None, None
            for label, score, dx1, dy1, dx2, dy2 in dets:
                if label != 'person':
                    continue
                bw, bh = (dx2 - dx1), (dy2 - dy1)
                if bw <= 0 or bh <= 0:
                    continue
                # Generous, because the analysis box and the snapshot are
                # seconds apart and a person's box changes as they turn --
                # but tight enough to reject "half the frame".
                if not (0.3 <= (bw / want_w) <= 3.0 and
                        0.3 <= (bh / want_h) <= 3.0):
                    continue
                cx, cy = (dx1 + dx2) / 2.0, (dy1 + dy2) / 2.0
                d = (cx - want_cx) ** 2 + (cy - want_cy) ** 2
                if best_d is None or d < best_d:
                    best_box, best_d = (dx1, dy1, dx2, dy2), d

            if best_box is not None:
                bx1, by1, bx2, by2 = best_box
                mx = (bx2 - bx1) * 0.4
                my = (by2 - by1) * 0.4
            elif best_diff <= 1500:
                # Nobody re-detected, but the snapshot is nearly the same
                # moment as the frame — trust geometry with a modest margin.
                bx1, by1 = x1 * sx, y1 * sy
                bx2, by2 = x2 * sx, y2 * sy
                mx = (bx2 - bx1) * 0.6
                my = (by2 - by1) * 0.6
            else:
                self._logger.info(
                    "[ImageCheck] fullres skip: no person in snapshot "
                    "(skew %dms)" % int(best_diff))
                return None

            # Final backstop on the crop itself.  Whatever route produced the
            # box, the point of this path is a ZOOMED view of one person -- so
            # cap it at a few times the size we expected.  Without this the
            # geometric fallback's skew-scaled margin can still open the crop
            # out to most of the frame, which is the failure this whole block
            # exists to avoid.
            maxw, maxh = want_w * _kMaxFullresCropFactor, \
                want_h * _kMaxFullresCropFactor
            ccx, ccy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
            halfw = min((bx2 - bx1) / 2.0 + mx, maxw / 2.0)
            halfh = min((by2 - by1) / 2.0 + my, maxh / 2.0)
            nx1 = max(0, int(ccx - halfw))
            ny1 = max(0, int(ccy - halfh))
            nx2 = min(sw, int(ccx + halfw))
            ny2 = min(sh, int(ccy + halfh))
            if nx2 - nx1 < 32 or ny2 - ny1 < 32:
                self._logger.info("[ImageCheck] fullres skip: crop too small")
                return None
            return snap_rgb[ny1:ny2, nx1:nx2]
        except Exception as e:
            self._logger.info("[ImageCheck] fullres skip: %s: %r" % (why, e))
            return None

    def _analyse_face(self, person_crop_rgb: np.ndarray,
                      det_floor: float = None) -> dict:
        """
        Run InsightFace on the person crop and return a dict of attributes:

            {
                "name":    matched name or "" if unknown/below threshold,
                "conf":    recognition cosine similarity for the match (float)
                           or None if unrecognized,
                "gender":  "M" / "F" or None,
                "age":     estimated age (int) or None,
                "faceDet": face detection score (how sure it's a face) or None,
            }

        gender and age come from the genderage model that InsightFace already
        runs on every get() call — capturing them adds no extra inference.
        Returns an empty-name dict with None fields if no face is found.
        """
        empty = {"name": "", "conf": None, "gender": None, "age": None,
                 "faceDet": None, "rawDet": None}
        try:
            faces = self._rpc.face(person_crop_rgb)
        except Exception as e:
            self._logger.debug(f"[ImageCheck] face service error: {e}")
            return empty

        if not faces:
            return empty

        # Best raw score BEFORE the floor — kept for diagnostics so a
        # "no face" outcome distinguishes 'nothing detected' from 'detected
        # but under the floor'.
        try:
            empty["rawDet"] = max(
                float(getattr(f, "det_score", 0.0) or 0.0) for f in faces)
        except Exception:
            pass

        # Reject low-confidence face *detections* before trusting anything.
        # "Is this actually a face?" — a face-shaped patch of gravel/shadow on an
        # empty driveway scores low here; without this floor it would still be
        # run through recognition and could weakly match a known identity.
        # Callers may pass a relaxed det_floor for crops that are already
        # person-verified (the full-res snapshot path).
        face_det_floor = det_floor if det_floor is not None else \
            self._cfg.get("FACE_DET_CONF", _DEFAULTS["FACE_DET_CONF"])
        faces = [f for f in faces
                 if float(getattr(f, "det_score", 0.0) or 0.0) >= face_det_floor]
        if not faces:
            return empty

        # Use the most-confident face for demographics.
        faces = sorted(faces, key=lambda f: getattr(f, "det_score", 0.0),
                       reverse=True)
        primary = faces[0]

        face_det = None
        try:
            if getattr(primary, "det_score", None) is not None:
                face_det = float(primary.det_score)
        except Exception:
            pass

        gender = None
        age = None
        try:
            if getattr(primary, "sex", None):
                gender = primary.sex                       # 'M' / 'F'
            elif getattr(primary, "gender", None) is not None:
                gender = "M" if int(primary.gender) == 1 else "F"
            if getattr(primary, "age", None) is not None:
                age = int(primary.age)
        except Exception:
            pass

        # Recognition across all faces — keep the best match above threshold.
        threshold = self._cfg.get("FACEMATCH_CONF", _DEFAULTS["FACEMATCH_CONF"])
        name = ""
        best_conf = None
        if _known_encodings:
            for face in faces:
                if face.embedding is None:
                    continue
                enc = face.embedding.flatten()
                sims = [
                    float(np.dot(k, enc) / (np.linalg.norm(k) * np.linalg.norm(enc) + 1e-9))
                    for k in _known_encodings
                ]
                idx = int(np.argmax(sims))
                conf = sims[idx]
                if conf >= threshold and (best_conf is None or conf > best_conf):
                    best_conf = conf
                    name = _known_names[idx].split(" (")[0]

        return {"name": name, "conf": best_conf, "gender": gender, "age": age,
                "faceDet": face_det, "rawDet": empty["rawDet"]}

    @staticmethod
    def _encode_attrs(base_type: str, attrs: list) -> str:
        """Append a "|key=val|key=val" suffix to a type string.

        The QueuedDataManagerCloud vote engine strips this suffix back to the
        clean category before voting; the attributes are stored separately.
        """
        attrs = [a for a in attrs if a]
        if attrs:
            return base_type + "|" + "|".join(attrs)
        return base_type

    @classmethod
    def _encode_face_attrs(cls, base_type: str, info: dict,
                           det_conf: float = None) -> str:
        """Encode face attributes onto a person type string.

        Produces e.g. "person:Bernie|fc=0.78|fd=0.95|g=M|a=34|dc=0.88".
        Recognition confidence (fc) is only attached when a name matched.
        dc is the YOLO person-detection confidence.
        """
        if info["name"]:
            base_type = "person:%s" % info["name"]
        attrs = []
        if info["name"] and info["conf"] is not None:
            attrs.append("fc=%.2f" % info["conf"])
        if info["faceDet"] is not None:
            attrs.append("fd=%.2f" % info["faceDet"])
        if info["gender"]:
            attrs.append("g=%s" % info["gender"])
        if info["age"] is not None:
            attrs.append("a=%d" % info["age"])
        if det_conf is not None:
            attrs.append("dc=%.2f" % det_conf)
        return cls._encode_attrs(base_type, attrs)

    def _check_nudity(self, person_crop_rgb: np.ndarray) -> str:
        """
        Run NudeNet on the crop.

        Returns an encoded type string of the form
            "nudity:<CLASS>=<score>,<CLASS>=<score>,..."
        listing every class that exceeded its threshold (highest score first),
        or empty string if nothing was flagged.  The detail is preserved so the
        search UI can show which classes fired and at what confidence.
        """
        # Only the classes the user left enabled (and their thresholds) fire.
        thresholds = enabledNudeThresholds(self._cfg)
        if not thresholds:
            return ""
        try:
            import cv2
            crop_bgr = cv2.cvtColor(person_crop_rgb, cv2.COLOR_RGB2BGR)
            detections = self._rpc.nudity(crop_bgr)
        except Exception as e:
            self._logger.debug(f"[ImageCheck] nudity service error: {e}")
            return ""

        matched = []
        for det in detections:
            cls   = det.get("class", "").upper()
            score = det.get("score", 0.0)
            if cls in thresholds and score >= thresholds[cls]:
                matched.append((cls, score))

        if not matched:
            return ""

        matched.sort(key=lambda cs: cs[1], reverse=True)
        detail = ",".join("%s=%.2f" % (cls, score) for cls, score in matched)
        return "nudity:" + detail
