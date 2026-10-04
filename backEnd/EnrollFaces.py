"""
EnrollFaces.py

Scans a "baseline" folder of per-person sub-directories, runs InsightFace
buffalo_l to extract face embeddings, and writes known_faces.dat.

Baseline folder layout
    <BaselineFolder>/
        Alice/          <- display name "Alice"
        Bob/
        Ignore/         <- skipped (case-insensitive)
        ...

Subfolder names may use "Name-Suffix" format (e.g. "Bernie-M", "Rosemary-F").
Only the part before the first "-" is used as the display name.

The resulting known_faces.dat is a pickle:
    {"encodings": [np.array, ...], "names": ["Alice", "Bob", ...]}
One entry per image that contains a detectable face; recognition uses
max-similarity across all stored embeddings so multiple images per person
improve robustness.

Can be used:
  - as a module: call enrollFaces(baselineFolder, outputPath, progressCallback)
  - as a CLI script: python -m backEnd.EnrollFaces [baselineFolder] [outputPath]
"""

import os
import pickle
import logging

import numpy as np

# Data-dir path (same as KNOWN_FACES_DAT default), resolved through
# InstallPaths so a service-launched back end agrees with the front end.
try:
    from appCommon.InstallPaths import getUserDataDir as _getUserDataDir
    _kSighthoundDataDir = _getUserDataDir()
except Exception:
    _kSighthoundDataDir = os.path.join(
        os.path.expanduser('~'), 'AppData', 'Local', 'Sighthound Video Py3'
    )
_kDefaultOutputPath = os.path.join(_kSighthoundDataDir, 'known_faces.dat')
_kDefaultBaseline   = os.path.join(_kSighthoundDataDir, 'Baseline')

# Subfolder names that are always skipped
_kIgnoreNames = {'ignore'}


def _displayName(folderName):
    """'Bernie-M' -> 'Bernie', 'Alice' -> 'Alice'."""
    return folderName.split('-')[0]


def _prepareImage(img_bgr):
    """Pre-process a BGR image for InsightFace detection.

    Two problems common in close-up portrait sets:
    1. Face fills the entire frame — RetinaFace needs surrounding context.
       Fix: pad 40% of the smaller dimension on all sides.
    2. Very small image (< 200 px tall) — too few pixels for the detector.
       Fix: upscale to at least 320 px tall before detection.
    """
    import cv2
    h, w = img_bgr.shape[:2]

    # Upscale very small images
    if max(h, w) < 200:
        scale = 320.0 / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_LINEAR)
        h, w = img_bgr.shape[:2]

    # Pad close-up crops so the detector has face-boundary context.
    # Add 40% of the shorter dimension as a grey border on all four sides.
    pad = int(min(h, w) * 0.4)
    img_bgr = cv2.copyMakeBorder(img_bgr, pad, pad, pad, pad,
                                 cv2.BORDER_CONSTANT, value=(128, 128, 128))
    return img_bgr


def enrollFaces(baselineFolder, outputPath=None, progressCallback=None, logger=None):
    """Scan baselineFolder, build embeddings, write known_faces.dat.

    @param  baselineFolder    Path to the folder of per-person sub-directories.
    @param  outputPath        Where to write known_faces.dat.  Defaults to the
                              standard Sighthound data directory.
    @param  progressCallback  Optional callable(current, total, message).
                              Called on the calling thread; for wx use
                              wx.CallAfter to post to the UI thread.
    @param  logger            Optional logging.Logger instance.
    @return (enrolledNames, skippedCount, errorMessage)
    """
    if outputPath is None:
        outputPath = _kDefaultOutputPath
    if logger is None:
        logger = logging.getLogger(__name__)

    # ---- 1. Load InsightFace -----------------------------------------------
    try:
        import insightface
        from insightface.app import FaceAnalysis
    except ImportError:
        return [], 0, "InsightFace is not installed."

    try:
        # Use the models bundled in an installed build rather than downloading
        # them into the running account's home (see DetectionService.load).
        faceKwargs = {}
        try:
            from appCommon.InstallPaths import getInsightFaceRoot
            bundledRoot = getInsightFaceRoot()
            if bundledRoot:
                faceKwargs["root"] = bundledRoot
        except Exception:
            pass
        # Ask only for providers onnxruntime really has: requesting CUDA without
        # an NVIDIA GPU can take the process down natively, which the except
        # below cannot catch (see ImageCheckConfig.onnxRuntimeTargets).
        from backEnd.ImageCheckConfig import onnxRuntimeTargets
        providers, ctxId = onnxRuntimeTargets()
        faceApp = FaceAnalysis(
            name="buffalo_l",
            providers=providers,
            **faceKwargs
        )
        # det_thresh=0.2 catches close-up portraits that score below the 0.5
        # default; padding in _prepareImage provides the context RetinaFace
        # needs when the face fills the whole frame.
        faceApp.prepare(ctx_id=ctxId, det_size=(640, 640), det_thresh=0.2)
    except Exception as e:
        return [], 0, "Failed to load InsightFace model: %s" % e

    # ---- 2. Collect sub-directories ----------------------------------------
    if not os.path.isdir(baselineFolder):
        return [], 0, "Baseline folder not found: %s" % baselineFolder

    subFolders = sorted([
        d for d in os.listdir(baselineFolder)
        if os.path.isdir(os.path.join(baselineFolder, d))
        and d.lower() not in _kIgnoreNames
    ])

    if not subFolders:
        return [], 0, "No person sub-folders found in: %s" % baselineFolder

    # ---- 3. Count total images for progress --------------------------------
    imagePaths = {}
    for sf in subFolders:
        folder = os.path.join(baselineFolder, sf)
        imgs = [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))
        ]
        if imgs:
            imagePaths[sf] = imgs

    totalImages = sum(len(v) for v in imagePaths.values())
    if totalImages == 0:
        return [], 0, "No images found in baseline folder."

    # ---- 4. Extract embeddings ---------------------------------------------
    import cv2

    encodings = []
    names     = []
    skipped   = 0
    processed = 0
    enrolledNames = []

    for sf, imgs in imagePaths.items():
        displayName = _displayName(sf)
        personEncodings = []

        for imgPath in imgs:
            if progressCallback:
                progressCallback(processed, totalImages,
                                 "Processing %s (%d/%d)" % (displayName, processed + 1, totalImages))
            try:
                img_bgr = cv2.imread(imgPath)
                if img_bgr is None:
                    skipped += 1
                    processed += 1
                    continue
                img_bgr = _prepareImage(img_bgr)
                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                faces = faceApp.get(img_rgb)
                if not faces:
                    skipped += 1
                    processed += 1
                    continue
                # Use the largest face (by bbox area) if multiple detected
                best = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
                if best.embedding is None:
                    skipped += 1
                    processed += 1
                    continue
                enc = best.embedding.flatten().astype(np.float32)
                enc /= (np.linalg.norm(enc) + 1e-9)  # L2-normalise
                personEncodings.append(enc)
            except Exception as ex:
                logger.warning("EnrollFaces: skipping %s: %s" % (imgPath, ex))
                skipped += 1
            processed += 1

        if personEncodings:
            # Store every individual embedding (max-sim matching is more robust
            # than a single averaged embedding, especially for small sets).
            for enc in personEncodings:
                encodings.append(enc)
                names.append(displayName)
            enrolledNames.append(displayName)
            logger.info("EnrollFaces: %s — %d/%d faces extracted" %
                        (displayName, len(personEncodings), len(imgs)))
        else:
            logger.warning("EnrollFaces: no usable faces found for %s" % displayName)

    if not encodings:
        return [], skipped, "No faces could be extracted from the baseline images."

    # ---- 5. Write known_faces.dat ------------------------------------------
    os.makedirs(os.path.dirname(outputPath), exist_ok=True)
    with open(outputPath, 'wb') as f:
        pickle.dump({"encodings": encodings, "names": names}, f)

    if progressCallback:
        progressCallback(totalImages, totalImages,
                         "Done — %d people enrolled." % len(enrolledNames))

    logger.info("EnrollFaces: wrote %d embeddings (%d people) to %s" %
                (len(encodings), len(enrolledNames), outputPath))
    return enrolledNames, skipped, None  # None error = success


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    baseDir = sys.argv[1] if len(sys.argv) > 1 else _kDefaultBaseline
    outPath = sys.argv[2] if len(sys.argv) > 2 else _kDefaultOutputPath

    def progress(cur, total, msg):
        pct = int(100 * cur / total) if total else 0
        print("\r[%3d%%] %s" % (pct, msg), end='', flush=True)

    print("Enrolling faces from: %s" % baseDir)
    enrolled, skipped, err = enrollFaces(baseDir, outPath, progressCallback=progress)
    print()  # newline after progress

    if err:
        print("ERROR:", err)
        sys.exit(1)
    print("Enrolled: %s" % ', '.join(enrolled))
    print("Skipped images (no face detected): %d" % skipped)
    print("Written to: %s" % outPath)
