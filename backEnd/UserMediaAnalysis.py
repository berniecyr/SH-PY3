#! /usr/local/bin/python

#*****************************************************************************
#
# UserMediaAnalysis.py
#     Run the production detectors over one of the user's own files.
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
The user's own photos and videos, through the production detectors.

**No wx here; this is importable from either process** -- same rule as
backEnd/DetectionReplay.py, and for the same reason: the front end runs this
directly rather than paying for an RPC round trip through the back end.

Nothing in this module loads a model.  Inference is three calls on the
already-running DetectionService (backEnd/DetectionServiceClient.py:
yolo/face/nudity, each taking a numpy image over a localhost socket), which is
what makes "run my photos through the same detectors" cost no extra process, no
second model load and no second CUDA context.  DetectionReplay already drives
the service this way from the front end; this is the same seam.

Settings come from ImageCheckConfig -- the same imagecheck_config.json the
cameras read -- so "the same settings" is true by construction rather than by
copying constants that then drift.

### How this differs from the camera path, on purpose

ObjectDetectorClientImageCheck._processSingleImage returns the FULL-CROP rect
for every detection, because the boxes that matter there come from MOG2 motion
blobs and the crop has already been cut to one.  There are no motion blobs
here, so this keeps the real YOLO boxes, normalised 0..1 -- which is what lets
the detail pane draw them on a photo of any size or aspect ratio.

### What this deliberately does NOT do

It does not use VideoPipeline.  MOG2 needs history: _kWarmupFrames is 30 and
_kPromoteHits is 3, so a single frame can never promote an object.  A still
photo through the motion pipeline detects nothing, always.  Video here is
sampled frames run through the detectors directly, which finds "a person is
visible around 00:42" but is not object tracking and must not be described as
such.
"""

# Python imports...
import os
import time

# Common 3rd-party imports...
import numpy as np

# Toolbox imports...

# Local imports...
from backEnd import ImageCheckConfig
from backEnd import KnownFaces


# Constants...

# COCO labels we map to a category.  Imported from the live detector so the
# two can never disagree -- this mapping is narrower than the README claims
# (no bicycle/bus/boat/train/sheep/bear), and for a photo library that is a
# real limitation rather than a rounding error.  Widening it is a decision
# about the CAMERA path too, so it is not made here.
from backEnd.ObjectDetectorClientImageCheck import (_kAnimalClasses,
                                                    _kVehicleClasses)

# Default seconds between sampled video frames, and the hard ceiling on how
# many we will take from one file.  The ceiling is load-bearing on a CPU-only
# machine, where a single YOLO pass is measured in seconds: without it a long
# video is an unbounded job.
kDefaultSampleSecs = 2.0
kMaxSamples = 300

# Extensions we will decode.  HEIC is absent because nothing in this tree can
# read it (no pillow-heif, and Pillow here has no HEIC support).
kImageExts = ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff',
              '.webp')
kVideoExts = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.mpg', '.mpeg',
              '.wmv', '.webm')


##############################################################################
def isImage(path):
    """@return  True if this looks like a still we can decode."""
    return path.lower().endswith(kImageExts)


##############################################################################
def isVideo(path):
    """@return  True if this looks like a video we can decode."""
    return path.lower().endswith(kVideoExts)


##############################################################################
def modelSignature(cfg):
    """A short string identifying the analysis configuration.

    Stored with every result so a file analysed under a different model or
    threshold can be spotted and offered a re-run, instead of a folder quietly
    mixing results from several configurations.

    @param  cfg  The ImageCheckConfig dict.
    @return str
    """
    parts = [
        str(cfg.get("YOLO_MODEL")),
        "conf=%.3f" % float(cfg.get("YOLO_CONF_THRESHOLD", 0.25)),
        "attrs=%.3f" % float(cfg.get("PERSON_CONF_FOR_ATTRS", 0.5)),
        "face=%d" % (1 if cfg.get("RUN_FACE") else 0),
        "nud=%d" % (1 if cfg.get("RUN_NUDITY") else 0),
    ]
    if cfg.get("RUN_NUDITY"):
        parts.append(str(cfg.get("NUDE_MODEL")))
    return "|".join(parts)


##############################################################################
def optionalModelsRunning(client, cfg):
    """Which optional models an analysis right now would actually run.

    A model counts only when the config turns it on AND the detection service
    has it loaded; otherwise the service quietly returns nothing for it.

    @param  client  A DetectionServiceClient.
    @param  cfg     The ImageCheckConfig dict.
    @return dict    {"faceModelRan": bool, "nudityModelRan": bool}
    """
    try:
        caps = client.ping() or {}
    except Exception:
        caps = {}
    return {
        "faceModelRan": bool(cfg.get("RUN_FACE")) and bool(caps.get("face")),
        "nudityModelRan": (bool(cfg.get("RUN_NUDITY"))
                           and bool(ImageCheckConfig.enabledNudeThresholds(cfg))
                           and bool(caps.get("nudity"))),
    }


##############################################################################
def loadConfig():
    """@return  The same config dict the cameras are using right now."""
    return ImageCheckConfig.loadConfig()


##############################################################################
def openDetectionClient(logger=None):
    """Connect to the running DetectionService.

    @param  logger  Optional logger.
    @return client  A DetectionServiceClient, or None when the service is not
                    reachable (the back end is not running, typically).
    """
    try:
        from backEnd.DetectionServiceClient import DetectionServiceClient
    except Exception as e:
        if logger is not None:
            logger.warning("UserMediaAnalysis: no DetectionServiceClient: %s"
                           % e)
        return None
    try:
        client = DetectionServiceClient(logger)
        client.ping()
        return client
    except Exception as e:
        if logger is not None:
            logger.warning("UserMediaAnalysis: detection service "
                           "unreachable: %s" % e)
        return None


##############################################################################
def _readImageRgb(path):
    """Decode a still to an RGB ndarray.

    cv2 first (it is what the rest of the tree uses and handles the common
    cases fastest), Pillow as the fallback for what it cannot read.  cv2 also
    silently returns None for a non-ASCII path on Windows, which is exactly
    the kind of path a personal photo library contains -- hence the fallback
    being tried on None, not only on an exception.

    @param  path  Absolute path.
    @return       (h, w, 3) uint8 RGB array.
    @raise  ValueError if it cannot be decoded.
    """
    try:
        import cv2
        # np.fromfile rather than cv2.imread: imread cannot open a path with
        # non-ASCII characters on Windows and gives no error when it fails.
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is not None:
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    except Exception:
        pass

    from PIL import Image
    with Image.open(path) as pil:
        return np.asarray(pil.convert("RGB"))


##############################################################################
def _clampBox(x1, y1, x2, y2, width, height):
    """Normalise a pixel box to 0..1 and keep it inside the frame.

    @return  (x1, y1, x2, y2) as floats in 0..1.
    """
    if width <= 0 or height <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    nx1 = min(max(float(x1) / width, 0.0), 1.0)
    ny1 = min(max(float(y1) / height, 0.0), 1.0)
    nx2 = min(max(float(x2) / width, 0.0), 1.0)
    ny2 = min(max(float(y2) / height, 0.0), 1.0)
    return (min(nx1, nx2), min(ny1, ny2), max(nx1, nx2), max(ny1, ny2))


##############################################################################
def _cropPerson(frameRgb, box, width, height):
    """Cut a person out of the frame for the face and nudity passes.

    @param  frameRgb  The full frame, RGB.
    @param  box       (x1, y1, x2, y2) in pixels.
    @param  width     Frame width.
    @param  height    Frame height.
    @return           An RGB crop, or None when the box is degenerate.
    """
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(x1 + 1, min(x2, width))
    y2 = max(y1 + 1, min(y2, height))
    crop = frameRgb[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    return np.ascontiguousarray(crop)


##############################################################################
def analyzeFrame(frameRgb, client, cfg, atMs=0, logger=None):
    """Run the detectors over one frame.

    @param  frameRgb  (h, w, 3) uint8 RGB array.
    @param  client    A DetectionServiceClient.
    @param  cfg       The ImageCheckConfig dict.
    @param  atMs      Offset into the source, stored with each detection.
                      0 for a still.
    @param  logger    Optional logger.
    @return list      Detection dicts, ready for UserMediaDb.saveResult.
    """
    # The shared detector RPC caps serialized requests at 64 MiB. A 24 MP
    # photograph alone is 72 MB as RGB. Bound only the inference copy; callers
    # retain the original dimensions, and output boxes remain normalized.
    maxPixels = 16000000  # 48 MB RGB, leaving room for the RPC envelope.
    height, width = frameRgb.shape[:2]
    if height * width > maxPixels:
        from PIL import Image
        scale = (maxPixels / float(height * width)) ** 0.5
        frameRgb = np.asarray(Image.fromarray(frameRgb).resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS))
        height, width = frameRgb.shape[:2]
    out = []

    conf = float(cfg.get("YOLO_CONF_THRESHOLD", 0.25))
    detections = client.yolo(frameRgb, conf)

    personBoxes = []
    for label, score, x1, y1, x2, y2 in detections:
        nb = _clampBox(x1, y1, x2, y2, width, height)
        if label == "person":
            personBoxes.append(((x1, y1, x2, y2), score, nb))
        elif label in _kAnimalClasses:
            out.append({"atMs": atMs, "type": "animal", "subType": label,
                        "conf": float(score),
                        "x1": nb[0], "y1": nb[1], "x2": nb[2], "y2": nb[3]})
        elif label in _kVehicleClasses:
            out.append({"atMs": atMs, "type": "vehicle", "subType": label,
                        "conf": float(score),
                        "x1": nb[0], "y1": nb[1], "x2": nb[2], "y2": nb[3]})

    if not personBoxes:
        return out

    attrFloor = float(cfg.get("PERSON_CONF_FOR_ATTRS", 0.5))
    runFace = bool(cfg.get("RUN_FACE"))
    if runFace:
        # Matching uses a process-local cache. Load it on first analysis and
        # check for enrollment updates on each sampled frame; unchanged
        # libraries reuse the cached matrix without re-reading the pickle.
        from appCommon.InstallPaths import getUserDataDir
        KnownFaces.loadKnownFaces(
            os.path.join(getUserDataDir(), "known_faces.dat"), logger)
    runNudity = bool(cfg.get("RUN_NUDITY"))
    faceDetFloor = float(cfg.get("FACE_DET_CONF", 0.6))
    faceMatchConf = float(cfg.get("FACEMATCH_CONF", 0.32))
    minFacePx = int(cfg.get("MIN_FACE_SIZE", 20))
    nudeThresholds = ImageCheckConfig.enabledNudeThresholds(cfg)

    for box, score, nb in personBoxes:
        det = {"atMs": atMs, "type": "person", "subType": "person",
               "conf": float(score),
               "x1": nb[0], "y1": nb[1], "x2": nb[2], "y2": nb[3]}

        # Attributes are only worth the extra two inference calls on a
        # confident person; the camera path uses the same gate.
        if float(score) >= attrFloor and (runFace or runNudity):
            crop = _cropPerson(frameRgb, box, width, height)
            if crop is not None:
                if runFace:
                    _addFaceAttrs(det, crop, client, faceDetFloor,
                                  faceMatchConf, minFacePx, logger)
                if runNudity and nudeThresholds:
                    _addNudityAttrs(det, crop, client, nudeThresholds, logger)

        out.append(det)

    return out


##############################################################################
def _addFaceAttrs(det, cropRgb, client, detFloor, matchConf, minFacePx,
                  logger=None):
    """Run InsightFace over a person crop and record what it found.

    @param  det        The detection dict to update in place.
    @param  cropRgb    RGB crop of one person.
    @param  client     A DetectionServiceClient.
    @param  detFloor   Minimum face detection score to believe.
    @param  matchConf  Minimum cosine similarity to name someone.
    @param  minFacePx  Smallest face worth trusting, in pixels.
    @param  logger     Optional logger.
    """
    try:
        faces = client.face(cropRgb)
    except Exception as e:
        if logger is not None:
            logger.info("UserMediaAnalysis: face pass failed: %s" % e)
        return

    usable = []
    for face in faces or []:
        if float(getattr(face, "det_score", 0.0) or 0.0) < detFloor:
            continue
        bbox = getattr(face, "bbox", None)
        if bbox is not None and len(bbox) >= 4:
            if (abs(bbox[2] - bbox[0]) < minFacePx
                    or abs(bbox[3] - bbox[1]) < minFacePx):
                continue
        usable.append(face)

    if not usable:
        return

    best = max(usable, key=lambda f: float(getattr(f, "det_score", 0.0) or 0.0))
    det["faceDetConf"] = float(getattr(best, "det_score", 0.0) or 0.0)

    sex = getattr(best, "sex", None)
    if sex in ("M", "F"):
        det["gender"] = sex
    age = getattr(best, "age", None)
    if age is not None:
        det["age"] = int(age)

    name, conf = KnownFaces.matchBest(usable, matchConf)
    if name:
        det["faceName"] = name
        det["faceConf"] = conf
    det["subType"] = "person"


##############################################################################
def _addNudityAttrs(det, cropRgb, client, thresholds, logger=None):
    """Run NudeNet over a person crop and record what cleared its threshold.

    Per-class thresholds, because they are not interchangeable -- the shipped
    defaults range from 0.15 to 0.7 -- and a single global number would either
    flood or silence depending on which class fired.

    @param  det         The detection dict to update in place.
    @param  cropRgb     RGB crop of one person.
    @param  client      A DetectionServiceClient.
    @param  thresholds  {class name: minimum score} for enabled classes only.
    @param  logger      Optional logger.
    """
    try:
        # NudeNet wants BGR; everything else here is RGB.
        results = client.nudity(np.ascontiguousarray(cropRgb[:, :, ::-1]))
    except Exception as e:
        if logger is not None:
            logger.info("UserMediaAnalysis: nudity pass failed: %s" % e)
        return

    hits = []
    for item in results or []:
        cls = item.get("class")
        score = float(item.get("score", 0.0))
        floor = thresholds.get(cls)
        if floor is not None and score >= floor:
            hits.append((cls, score))

    if hits:
        hits.sort(key=lambda h: -h[1])
        det["nudity"] = True
        det["nudityDetail"] = ",".join("%s=%.2f" % h for h in hits)


##############################################################################
def analyzeStill(path, client, cfg, logger=None):
    """Analyse one photo.

    @param  path    Absolute path.
    @param  client  A DetectionServiceClient.
    @param  cfg     The ImageCheckConfig dict.
    @param  logger  Optional logger.
    @return dict    kind/width/height/captureMs/modelSig/error/detections,
                    plus faceModelRan/nudityModelRan.
    """
    result = {"kind": "image", "width": None, "height": None,
              "durationMs": None, "captureMs": None,
              "modelSig": modelSignature(cfg), "error": None,
              "detections": [], "elapsedMs": 0,
              "faceModelRan": False, "nudityModelRan": False}

    started = time.time()
    try:
        frame = _readImageRgb(path)
    except Exception as e:
        result["error"] = "Could not decode: %s" % e
        if logger is not None:
            logger.info("UserMediaAnalysis: cannot decode %s: %s" % (path, e))
        return result

    result["height"], result["width"] = frame.shape[:2]
    result["captureMs"] = _captureTimeMs(path)

    try:
        running = optionalModelsRunning(client, cfg)
        result["detections"] = analyzeFrame(frame, client, cfg, 0, logger)
        result.update(running)
    except Exception as e:
        result["error"] = "Detection failed: %s" % e
        if logger is not None:
            logger.warning("UserMediaAnalysis: detection failed on %s: %s"
                           % (path, e))

    result["elapsedMs"] = int((time.time() - started) * 1000)
    return result


##############################################################################
def analyzeVideo(path, client, cfg, sampleSecs=kDefaultSampleSecs,
                 maxSamples=kMaxSamples, progressFn=None, cancelFn=None,
                 logger=None):
    """Analyse a video by sampling frames.

    Sampling, not tracking.  The result says "a person is visible around
    00:42"; it does not say how long they were there or how far they moved,
    because nothing here follows an object between samples.

    @param  path        Absolute path.
    @param  client      A DetectionServiceClient.
    @param  cfg         The ImageCheckConfig dict.
    @param  sampleSecs  Seconds between sampled frames.
    @param  maxSamples  Hard ceiling on frames taken from this file.
    @param  progressFn  Optional f(done, total) -- called per sample.
    @param  cancelFn    Optional f() -> True to stop early.
    @param  logger      Optional logger.
    @return dict        Same shape as analyzeStill, plus durationMs.
    """
    result = {"kind": "video", "width": None, "height": None,
              "durationMs": None, "captureMs": None,
              "modelSig": modelSignature(cfg), "error": None,
              "detections": [], "elapsedMs": 0, "sampled": 0,
              "truncated": False,
              "faceModelRan": False, "nudityModelRan": False}

    started = time.time()
    try:
        import cv2
    except Exception as e:
        result["error"] = "OpenCV unavailable: %s" % e
        return result

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        result["error"] = "Could not open this video."
        return result

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frameCount = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        result["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or None
        result["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or None
        if fps > 0 and frameCount > 0:
            result["durationMs"] = int((frameCount / fps) * 1000)
        result["captureMs"] = _captureTimeMs(path)

        durationMs = result["durationMs"] or 0
        stepMs = max(1, int(sampleSecs * 1000))
        if durationMs > 0:
            planned = int(durationMs // stepMs) + 1
        else:
            # An unreadable duration is common in a file that was trimmed or
            # partly downloaded.  Sample until the decoder stops rather than
            # refusing outright.
            planned = maxSamples
        total = min(planned, maxSamples)
        result["truncated"] = planned > maxSamples

        running = optionalModelsRunning(client, cfg)
        detections = []
        for index in range(total):
            if cancelFn is not None and cancelFn():
                break
            atMs = index * stepMs
            cap.set(cv2.CAP_PROP_POS_MSEC, atMs)
            ok, frameBgr = cap.read()
            if not ok or frameBgr is None:
                break
            frameRgb = cv2.cvtColor(frameBgr, cv2.COLOR_BGR2RGB)
            try:
                detections.extend(
                    analyzeFrame(frameRgb, client, cfg, atMs, logger))
            except Exception as e:
                result["error"] = "Detection failed: %s" % e
                if logger is not None:
                    logger.warning("UserMediaAnalysis: detection failed on "
                                   "%s at %d ms: %s" % (path, atMs, e))
                break
            result["sampled"] = index + 1
            if progressFn is not None:
                progressFn(index + 1, total)

        result["detections"] = detections
        # Ran means at least one sampled frame went through the model.
        if result["sampled"]:
            result.update(running)
    finally:
        cap.release()

    result["elapsedMs"] = int((time.time() - started) * 1000)
    return result


##############################################################################
def analyzeFile(path, client, cfg, logger=None, **kwargs):
    """Analyse a photo or a video, whichever this is.

    @param  path    Absolute path.
    @param  client  A DetectionServiceClient.
    @param  cfg     The ImageCheckConfig dict.
    @param  logger  Optional logger.
    @return dict    The result, or one carrying `error` when unsupported.
    """
    if isVideo(path):
        return analyzeVideo(path, client, cfg, logger=logger, **kwargs)
    if isImage(path):
        return analyzeStill(path, client, cfg, logger=logger)
    return {"kind": None, "width": None, "height": None, "durationMs": None,
            "captureMs": None, "modelSig": modelSignature(cfg),
            "error": "Not a file type we can read.", "detections": [],
            "elapsedMs": 0, "faceModelRan": False, "nudityModelRan": False}


##############################################################################
def _captureTimeMs(path):
    """When the file was captured, in epoch milliseconds.

    EXIF DateTimeOriginal when there is one, file mtime otherwise.  A photo
    library is usually sorted by when the picture was TAKEN, which is not when
    the file was last written -- a copy or an edit moves mtime and leaves the
    capture time alone.

    @param  path  Absolute path.
    @return int   Epoch milliseconds, or None.
    """
    if isImage(path):
        try:
            from PIL import Image
            with Image.open(path) as img:
                exif = img.getexif()
                # DateTimeOriginal lives in the Exif sub-IFD (0x8769), not
                # IFD0 -- the same trap documented in DataManager's snapshot
                # writer, where putting it at the top level left it
                # unreadable to Windows and ExifTool alike.
                sub = exif.get_ifd(0x8769) if exif else None
                raw = None
                if sub:
                    raw = sub.get(36867) or sub.get(36868)
                if not raw and exif:
                    raw = exif.get(306)
                if raw:
                    parsed = time.strptime(str(raw).strip(),
                                           "%Y:%m:%d %H:%M:%S")
                    return int(time.mktime(parsed) * 1000)
        except Exception:
            # No EXIF, unreadable EXIF, or a camera that writes a malformed
            # date.  mtime below is the answer in all three cases.
            pass

    try:
        return int(os.path.getmtime(path) * 1000)
    except OSError:
        return None
